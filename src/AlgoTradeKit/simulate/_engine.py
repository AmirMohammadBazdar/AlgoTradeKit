"""
AlgoTradeKit.simulate._engine
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
``Simulate`` — the backtesting engine that replays a ``StrategyResult``
candle-by-candle and produces a ``SimulateReport`` — and
``SimulationStepper`` (v1.0.0), the step-driven core that ``Simulate``,
``run_multi`` and the live simulation all drive one closed candle at a time.

Design contract
---------------
* The engine is **pure**: it never mutates the ``StrategyResult`` or the
  underlying DataFrames.
* All time values are UTC milliseconds (matching ``AlgoTradeKit.data``).
* PnL is computed with a single unified formula that works for both
  exchange and MetaTrader instruments::

      pnl = direction_factor × (exit_price − entry_price) × pnl_per_price_unit

  where ``pnl_per_price_unit = risk_amount / sl_distance`` for risk-based
  sizing, or ``size × pip_value / pip_size`` for fixed-lot MT5 sizing.
* All position maths (sizing, SL/TP prices, close detection, PnL
  accounting, trailing / risk-free / multi-RR transitions) live in
  ``_position_math.py`` — shared with the live trader so backtest and
  live behave identically by construction (v1.0.0).

Position life-cycle per candle (in order)
------------------------------------------
1. Update trailing-SL peak prices.
2. Activate risk-free (break-even) SL when profit threshold is reached.
3. Check SL / TP close triggers for all open positions.
4. Force-close positions on ``ExitSignal`` (when configured).
5. Open new positions from entry ``Signal``s at this candle.
6. Record equity snapshot to ``balance_history``.
"""

from __future__ import annotations

from ..strategy._types import StrategyResult
from ._config import (
    REPORT_MODE_BOTH,
    REPORT_MODE_NONE,
    REPORT_MODE_SAVE,
    REPORT_MODE_WEBPAGE,
    TP_MODE_MULTI_RR,
    SimulateConfig,
)
from ._position import (
    CLOSE_REASON_EOD,
    CLOSE_REASON_FC,
    ClosedTrade,
    _InternalPosition,
)
from ._position_math import (
    SIZE_EPSILON,
    apply_partial_close,
    can_open_position,
    check_close,
    check_risk_free,
    compute_position_params,
    make_closed_trade,
)
from ._report import SimulateReport, build_report

# ---------------------------------------------------------------------------
# Keep-alive: prevent the process from exiting while browser tabs are open
# ---------------------------------------------------------------------------

_keep_alive_registered = False   # register only once per process


def _register_keep_alive() -> None:
    """
    Register a one-time ``atexit`` handler that keeps the Python process
    alive (blocking on ``while True: sleep(1)``) until the user presses
    Ctrl+C.

    **Why this is necessary**

    Both ``ChartServer`` and ``ReportServer`` run in *daemon* threads.
    Python's process exits as soon as the main thread finishes, and daemon
    threads are killed at that point — before the browser has had any
    meaningful time to connect, receive data, or let the user interact.
    This caused the chart/report to show "disconnected" immediately after
    the script finished.

    **Why atexit (not blocking inside Simulate.run)**

    Blocking inside ``Simulate.run()`` would prevent the caller from doing
    anything after the simulation — printing stats, saving CSV history, or
    running a second backtest.  An ``atexit`` handler runs *after* all user
    code has finished but *before* Python kills daemon threads, so:

    1. ``Simulate.run()`` returns immediately → caller code runs normally.
    2. When the caller's script finishes, Python queues shutdown.
    3. The atexit handler fires → the daemon server threads are still alive.
    4. The handler blocks → servers stay up, browser tabs remain interactive.
    5. User presses Ctrl+C → handler unblocks → Python kills daemon threads.

    The handler is registered at most once per process invocation, so
    multiple ``Simulate.run()`` calls (e.g., a parameter sweep) register
    only a single blocking handler.
    """
    global _keep_alive_registered
    if _keep_alive_registered:
        return
    _keep_alive_registered = True

    import atexit
    import time as _t

    def _block() -> None:
        print(
            "\n  \033[90m[AlgoTradeKit] Browser UI open — "
            "press Ctrl+C to exit\033[0m",
            flush=True,
        )
        try:
            while True:
                _t.sleep(1)
        except KeyboardInterrupt:
            print("\n  \033[90m[AlgoTradeKit] Shutting down…\033[0m", flush=True)

    atexit.register(_block)


# ---------------------------------------------------------------------------
# Step-driven core (v1.0.0)
# ---------------------------------------------------------------------------

class SimulationStepper:
    """
    Candle-by-candle simulation engine — the step-driven core.

    Holds all running simulation state (wallet, open positions, closed
    trades, equity history) and advances it one closed candle at a time.
    Everything else is a driver of this class:

    * ``Simulate.run()`` / ``Simulate._simulate_loop`` feed it a full
      recorded DataFrame — the batch backtest.
    * ``run_multi`` drives one stepper per pair, rebinding ``wallet`` /
      ``trade_id_seq`` around each step so all pairs share one wallet.
    * The live simulation (v1.0.0) feeds it closed candles from a
      real-time stream and takes ``build_report()`` snapshots between steps.

    Parameters
    ----------
    config : SimulateConfig
        Full simulation configuration (costs, sizing, TP/SL mode, etc.).
    initial_wallet : float | None
        Starting wallet.  ``None`` (default) uses ``config.initial_balance``.
    initial_trade_id : int
        First trade id to assign (sequential from there).
    record_balance_history : bool
        When True (default) every ``step()`` appends one
        ``{"timestamp", "wallet", "equity"}`` snapshot to
        ``balance_history``.  ``run_multi`` passes False and computes its
        own combined-portfolio snapshots instead.

    Attributes
    ----------
    wallet : float
        Free capital right now (margin of open positions excluded).
    trade_id_seq : int
        Next trade id to assign.
    open_positions : list[_InternalPosition]
        Live positions, in open order.  Mutated in place, never rebound.
    closed_trades : list[ClosedTrade]
        Every close event so far, in event order (partial closes included).
    balance_history : list[dict]
        One equity snapshot per stepped candle (see
        ``record_balance_history``).

    Usage
    -----
    ::

        stepper = SimulationStepper(config)
        for candle, signals, exit_signals in feed:   # closed candles only
            closed_now = stepper.step(candle, signals, exit_signals)
        stepper.finalize()                # close leftovers at last close
        report = stepper.build_report()   # also fine mid-run (snapshot)

    Purity: the stepper never mutates the candle mapping, the signals, or
    any DataFrame — state lives only on the stepper and its positions.
    """

    def __init__(
        self,
        config: SimulateConfig,
        initial_wallet: float | None = None,
        initial_trade_id: int = 0,
        *,
        record_balance_history: bool = True,
    ) -> None:
        self.config = config
        self.wallet: float = (
            initial_wallet if initial_wallet is not None else config.initial_balance
        )
        self.trade_id_seq: int = initial_trade_id
        self.open_positions: list[_InternalPosition] = []
        self.closed_trades: list[ClosedTrade] = []
        self.balance_history: list[dict] = []
        self.record_balance_history = record_balance_history
        self._symbol = config.symbol
        self._last_ts: int | None = None
        self._last_close: float | None = None
        self._finalized = False

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def finalized(self) -> bool:
        """True once ``finalize()`` has run — no further steps accepted."""
        return self._finalized

    # ------------------------------------------------------------------
    # Step — one closed candle
    # ------------------------------------------------------------------

    def step(self, candle, signals=(), exit_signals=()) -> list[ClosedTrade]:
        """
        Advance the simulation by exactly one closed candle.

        Parameters
        ----------
        candle : Mapping
            One closed candle with at least ``timestamp`` (UTC ms),
            ``open``, ``high``, ``low``, ``close`` keys — the
            library-standard candle dict works as-is (extra keys such as
            ``volume`` are ignored).  Never mutated.
        signals : Sequence[Signal]
            Entry signals generated **on this candle**, in strategy order.
        exit_signals : Sequence[ExitSignal]
            Exit signals for this candle (used only when
            ``config.force_close_on_exit_signal`` is set).

        Returns
        -------
        list[ClosedTrade]
            Trades closed during this step, in event order — partial
            multi-RR closes included.  Empty list when nothing closed.
        """
        if self._finalized:
            raise RuntimeError("SimulationStepper: step() called after finalize().")

        config = self.config
        open_positions = self.open_positions
        closed_trades = self.closed_trades
        wallet = self.wallet
        trade_id_seq = self.trade_id_seq
        symbol = self._symbol
        closed_before = len(closed_trades)

        ts       = int(candle["timestamp"])
        c_open   = float(candle["open"])
        c_high   = float(candle["high"])
        c_low    = float(candle["low"])
        c_close  = float(candle["close"])

        # -------------------------------------------------------
        # A. Update trailing peak + excursion tracking
        #    Trailing SL is applied later inside check_close, AFTER
        #    the gap-open check, so a freshly-advanced SL cannot
        #    falsely trigger a "gap" close on the same candle.
        # -------------------------------------------------------
        for pos in open_positions:
            pos.update_trailing_peak(c_high if pos.direction == "long" else c_low)
            pos.update_excursion(c_close)

        # -------------------------------------------------------
        # B. Activate risk-free SL (if enabled & not multi-RR)
        # -------------------------------------------------------
        if config.risk_free_enabled and config.tp_mode != TP_MODE_MULTI_RR:
            for pos in open_positions:
                check_risk_free(pos, c_high, c_low, config)

        # -------------------------------------------------------
        # C. Check SL / TP close for all open positions
        #    (may yield zero, one, or several partial+final events)
        # -------------------------------------------------------
        newly_closed: list[_InternalPosition] = []
        for pos in open_positions:
            events = check_close(pos, c_open, c_high, c_low, c_close, config, ts)
            for exit_price, reason, closed_size in events:
                ct = make_closed_trade(pos, exit_price, reason, ts, config, closed_size)
                wallet += ct.margin_amount + ct.gross_pnl
                closed_trades.append(ct)
                apply_partial_close(pos, closed_size)
            if pos.size <= SIZE_EPSILON:
                newly_closed.append(pos)

        for pos in newly_closed:
            open_positions.remove(pos)

        # -------------------------------------------------------
        # D. Force-close on ExitSignal
        # -------------------------------------------------------
        if config.force_close_on_exit_signal and open_positions:
            exit_sigs = exit_signals
            if exit_sigs:
                exit_price = (
                    exit_sigs[0].exit_price
                    if exit_sigs[0].exit_price is not None
                    else c_close
                )
                for pos in open_positions[:]:
                    ct = make_closed_trade(pos, exit_price, CLOSE_REASON_FC, ts, config)
                    wallet += pos.margin_amount + ct.gross_pnl
                    closed_trades.append(ct)
                open_positions.clear()

        # -------------------------------------------------------
        # E. Open new positions from entry signals at this candle
        # -------------------------------------------------------
        for sig in signals:
            if not can_open_position(sig, open_positions, config):
                continue

            params = compute_position_params(sig, config, wallet)
            if params is None:
                continue

            (margin_amount, size, pnl_pu, risk_amount,
             commission, spread_paid, tp_prices, fill_price) = params

            # Deduct margin and commission from wallet
            wallet -= margin_amount + commission

            # Store first TP as the display-level take_profit
            display_tp = tp_prices[0] if tp_prices else None

            pos = _InternalPosition(
                trade_id=trade_id_seq,
                symbol=symbol or sig.metadata.get("symbol", ""),
                direction=sig.direction,
                entry_price=fill_price,
                raw_entry_price=sig.entry_price,
                stop_loss=sig.stop_loss,
                take_profit=display_tp,
                margin_amount=margin_amount,
                risk_amount=risk_amount,
                size=size,
                open_time=ts,
                open_commission=commission,
                signal_metadata=dict(sig.metadata),
                signal_candle_index=sig.candle_index,
                tp_level_prices=tp_prices,
            )
            # Initialise SL history with the entry state (v0.7.4)
            pos.sl_history.append({
                "time": ts,
                "sl": sig.stop_loss,
                "next_tp": tp_prices[0] if tp_prices else None,
            })
            open_positions.append(pos)
            trade_id_seq += 1

        # -------------------------------------------------------
        # F. Record equity snapshot
        # -------------------------------------------------------
        if self.record_balance_history:
            unrealised = sum(p.unrealised_pnl(c_close) for p in open_positions)
            equity = (
                wallet
                + sum(p.margin_amount for p in open_positions)
                + unrealised
            )
            self.balance_history.append({
                "timestamp": ts,
                "wallet": wallet,
                "equity": equity,
            })

        self.wallet = wallet
        self.trade_id_seq = trade_id_seq
        self._last_ts = ts
        self._last_close = c_close

        return closed_trades[closed_before:]

    # ------------------------------------------------------------------
    # Finalize — end of data
    # ------------------------------------------------------------------

    def finalize(self) -> list[ClosedTrade]:
        """
        Close every remaining open position at the last stepped candle's
        close price (``CLOSE_REASON_EOD``) and freeze the stepper.

        Idempotent: a second call does nothing and returns ``[]``.  A
        stepper that never stepped finalizes to ``[]`` as well.  After
        finalizing, ``step()`` raises ``RuntimeError``.

        Returns
        -------
        list[ClosedTrade]
            The end-of-data close records, in position-open order.
        """
        if self._finalized:
            return []
        self._finalized = True

        if self._last_ts is None:
            return []

        wallet = self.wallet
        closed_before = len(self.closed_trades)

        for pos in self.open_positions:
            ct = make_closed_trade(
                pos, self._last_close, CLOSE_REASON_EOD, self._last_ts, self.config
            )
            wallet += pos.margin_amount + ct.gross_pnl
            self.closed_trades.append(ct)

        self.open_positions.clear()
        self.wallet = wallet
        return self.closed_trades[closed_before:]

    # ------------------------------------------------------------------
    # Report snapshot
    # ------------------------------------------------------------------

    def build_report(self) -> SimulateReport:
        """
        Build a full ``SimulateReport`` from the current state.

        Callable at any moment — mid-run it is a live snapshot (open
        positions appear in ``report.open_at_end``); after ``finalize()``
        it is the final batch-identical report.  The report receives
        shallow copies of the state lists, so later steps never mutate an
        already-taken snapshot.
        """
        return build_report(
            list(self.closed_trades),
            list(self.open_positions),
            list(self.balance_history),
            self.config,
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class Simulate:
    """
    Backtesting engine for a single strategy/config pair.

    Parameters
    ----------
    config : SimulateConfig
        Full simulation configuration (costs, sizing, TP/SL mode, etc.).

    Usage
    -----
    ::

        config = SimulateConfig(
            initial_balance=10_000,
            leverage=10,
            risk_per_trade=1.0,
            tp_mode="multi_rr",
            tp_levels=[1.0, 2.0, 3.0],
        )
        strategy = MyStrategy()
        result   = strategy.run(data)        # StrategyResult

        sim    = Simulate(config)
        report = sim.run(result)             # SimulateReport

    Or build from a config dict::

        cfg = {"initial_balance": 10_000, "tp_mode": "fixed_rr", "tp_rr": 2.0}
        report = Simulate(SimulateConfig(**cfg)).run(result)
    """

    def __init__(self, config: SimulateConfig) -> None:
        self.config = config

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(self, strategy_result: StrategyResult) -> SimulateReport:
        """
        Replay *strategy_result* candle-by-candle and produce a report.

        Parameters
        ----------
        strategy_result : StrategyResult
            Output of ``BaseStrategy.run(data, mode=StrategyMode.BACKTEST)``.
            Must contain the primary timeframe specified in
            ``self.config.primary_timeframe``.

        Returns
        -------
        SimulateReport
            Fully computed report (no further processing required).

        Raises
        ------
        ValueError
            When ``primary_timeframe`` is not found in ``strategy_result.data``.
        """
        config = self.config
        tf = config.primary_timeframe

        if tf not in strategy_result.data:
            available = sorted(strategy_result.data.keys())
            raise ValueError(
                f"Simulate: primary_timeframe '{tf}' not found in "
                f"strategy_result.data. Available: {available}"
            )

        primary_df = strategy_result.data[tf]

        if primary_df.empty:
            raise ValueError(
                f"Simulate: data['{tf}'] is empty — nothing to simulate."
            )

        # Validate required OHLCV columns
        _required = {"timestamp", "open", "high", "low", "close"}
        missing = _required - set(primary_df.columns)
        if missing:
            raise ValueError(
                f"Simulate: data['{tf}'] is missing columns: {sorted(missing)}"
            )

        # Index signals by candle position for O(1) lookup
        signals_by_idx: dict[int, list] = {}
        for sig in strategy_result.signals:
            signals_by_idx.setdefault(sig.candle_index, []).append(sig)

        exits_by_idx: dict[int, list] = {}
        for ex in strategy_result.exit_signals:
            exits_by_idx.setdefault(ex.candle_index, []).append(ex)

        report = self._simulate_loop(
            primary_df, signals_by_idx, exits_by_idx, config
        )

        # ------------------------------------------------------------------
        # Post-simulation rendering (v0.7.0)
        # Runs only when the caller has enabled show_chart / report_mode.
        # ------------------------------------------------------------------
        self._render_post_simulation(report, strategy_result)

        return report

    # ------------------------------------------------------------------
    # Post-simulation visualisation
    # ------------------------------------------------------------------

    def _render_post_simulation(
        self,
        report: SimulateReport,
        strategy_result: StrategyResult,
    ) -> None:
        """
        Open the candle chart and/or the report page after a simulation run.

        Called automatically by ``run()`` when the config has
        ``show_chart=True`` or ``report_mode != "none"``.

        v0.7.4 fix
        ----------
        Both servers (chart and report) run in daemon threads.  Daemon threads
        are killed the moment the Python process's main thread exits, which
        previously caused the browser tabs to show "disconnected" immediately
        after the user's script finished (e.g. after printing stats and saving
        history).

        The fix: register an ``atexit`` handler that blocks with
        ``while True: sleep(1)`` after all user code has run.  ``atexit``
        handlers execute *before* daemon threads are killed, so the servers
        stay up until the user presses Ctrl+C.
        """
        config = self.config
        tf = config.primary_timeframe

        chart = None
        chart_server = None
        report_server = None
        has_browser_ui = False

        # ---- 1. Build candle chart with positions ----
        if config.show_chart:
            chart, chart_server = self._build_simulation_chart(
                strategy_result, report, tf
            )
            has_browser_ui = True

        # ---- 2. Report page ----
        if config.report_mode != REPORT_MODE_NONE:
            report_server = self._render_report(
                report, strategy_result, chart, chart_server
            )
            # report_server is None for save-only mode (no browser tab)
            if report_server is not None:
                has_browser_ui = True

        # ---- 3. Keep process alive while browser tabs are open ----
        if has_browser_ui:
            _register_keep_alive()

    def _build_simulation_chart(
        self,
        strategy_result: StrategyResult,
        report: SimulateReport,
        tf: str,
    ):
        """
        Build and show an interactive candle chart with position boxes and
        strategy drawings.  Returns (Chart, ChartServer) tuple.
        """
        import time as _time

        from ..visual import Chart
        from ..visual.indicator_renderer import (
            add_simulation_positions,
            add_strategy_drawings,
        )

        primary_df = strategy_result.data[tf]

        chart = Chart(
            title=f"{self.config.symbol or ''} — Simulation Chart ({tf})",
        )
        chart.set_data(primary_df)

        # Pre-load caller-requested indicators (SimulateConfig.chart_indicators).
        # All maths run in the backend here, before the chart is shown; the
        # series appear automatically and stay editable from the chart toolbar.
        for spec in getattr(self.config, "chart_indicators", None) or []:
            try:
                chart.add_indicator_spec(dict(spec))
            except Exception as exc:  # a bad spec must never abort the chart
                import warnings
                warnings.warn(f"chart_indicators: skipped {spec!r} ({exc})")

        # Add strategy-defined drawings (support/resistance, signal zones, etc.)
        if strategy_result.drawings:
            add_strategy_drawings(chart, strategy_result)

        # Add position boxes (and dynamic SL/TP lines for multi_rr / trailing)
        add_simulation_positions(chart, report, opacity=0.15, config=self.config)

        # Show chart (non-blocking so report can open simultaneously)
        chart.show(block=False)

        # Give server a moment to start
        _time.sleep(0.4)

        return chart, chart._server

    def _render_report(
        self,
        report: SimulateReport,
        strategy_result: StrategyResult,
        chart,
        chart_server,
    ):
        """Build and display/save the report page.

        Returns
        -------
        ReportServer | None
            The running server when a browser tab is opened
            (``report_mode`` is ``"webpage"`` or ``"both"``);
            ``None`` for save-only mode (no browser UI to keep alive).
        """
        import time as _time

        from ..report._builder import build_report_payload
        from ..report._display import save_report_html
        from ..report._server import ReportServer

        config = self.config

        payload = build_report_payload(report)

        # Patch payload with chart connection info
        if chart is not None and chart_server is not None:
            payload["has_chart"] = True
            payload["chart_port"] = getattr(chart_server, "port", None)

        open_browser = config.report_mode in (REPORT_MODE_WEBPAGE, REPORT_MODE_BOTH)
        save_file    = config.report_mode in (REPORT_MODE_SAVE, REPORT_MODE_BOTH)

        # Callback: when user clicks a trade in the report, navigate the chart
        def _on_open_chart(trade_id: int) -> None:
            if chart is None:
                return
            marker = next(
                (m for m in report.trade_markers if m["trade_id"] == trade_id), None
            )
            if marker:
                chart.navigate_to_candle(marker["open_time"])

        report_server = None
        if open_browser:
            report_server = ReportServer(
                title=f"AlgoTradeKit Report — {config.config_id}",
                on_open_chart=_on_open_chart if chart else None,
            )
            report_server.start(open_browser=True)
            _time.sleep(0.3)
            report_server.set_report_data(payload)

        if save_file:
            out_path = save_report_html(report, path=config.report_save_path)
            print(f"[AlgoTradeKit] Report saved → {out_path}")

        return report_server

    # ------------------------------------------------------------------
    # Core loop — batch driver of the step-driven engine (v1.0.0)
    # ------------------------------------------------------------------

    @staticmethod
    def _simulate_loop(
        primary_df,
        signals_by_idx: dict[int, list],
        exits_by_idx: dict[int, list],
        config: SimulateConfig,
        initial_wallet: float | None = None,
        initial_trade_id: int = 0,
    ) -> SimulateReport:
        """
        Batch candle-by-candle simulation over a recorded DataFrame.

        Thin driver of ``SimulationStepper``: feeds every row (with the
        signals indexed at that candle position) through ``step()``, closes
        leftovers via ``finalize()``, and returns ``build_report()`` — so
        batch and step-driven results are identical by construction.
        """
        stepper = SimulationStepper(
            config,
            initial_wallet=initial_wallet,
            initial_trade_id=initial_trade_id,
        )

        for candle_idx, row in enumerate(primary_df.itertuples(index=False)):
            stepper.step(
                {
                    "timestamp": row.timestamp,
                    "open": row.open,
                    "high": row.high,
                    "low": row.low,
                    "close": row.close,
                },
                signals_by_idx.get(candle_idx, ()),
                exits_by_idx.get(candle_idx, ()),
            )

        stepper.finalize()
        return stepper.build_report()
