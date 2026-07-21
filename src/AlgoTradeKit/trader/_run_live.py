"""
AlgoTradeKit.trader._run_live
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
``run_live()`` — paper trading (v1.0.0).

Watch a strategy trade on live market data **without opening any position**:
"how does it behave on the current market?"  Positions are filled by the sim
engine (:class:`~AlgoTradeKit.simulate.LiveSimulation`) with the venue's
real costs applied, and all position management (trailing, risk-free,
multi-RR ladder) comes from the same shared math the backtest uses — so
paper behaviour matches both the backtest and, by the construction, the
live trader.

Order/execution code is **never touched**: this module never imports
``trader._execution`` and never calls an order method on the broker — only
market-data calls (``fetch_candles`` / ``fetch_last_candles`` /
``stream_candles``) and, when costs are not overridden,
``get_trading_costs``.  Rehearse order plumbing against venue test
environments instead (Binance ``testnet=True``, MT5 demo accounts —).

Behaviour
---------
* Same :class:`~AlgoTradeKit.trader.TraderConfig` as the real-order
  ``Trader``; multi-pair form takes the same ``TraderPair`` entry list.
  Per-pair chart + report; with two or more pairs and a displaying pair the
  session also serves **one combined report** across all pairs —
  merged trade list, equity curve summed across the paper wallets, per-pair
  breakdown — rendered by the combined support and re-pushed on every
  closed candle of any pair.
* Output — both are just config fields: ``display=True`` → live chart +
  live report (browser tab, or printed URLs when
  ``display_open_browser=False``); ``log_events=True`` → per-event terminal
  report, tagged ``[SIM]``.  With **both** off, ``run_live`` warns (a
  silent paper run is useless) and still runs — the final report is
  returned.
* Seeds history (``display_candles`` / ``display_start`` when set, else the
  last ``min_candles``; the fetched count must reach ``min_candles``) →
  simulates the seed → shows chart + report → subscribes to the feed →
  advances the sim and refreshes the display on every closed candle.
* Sim fills happen on **closed candles**; forming candles still
  stream to the chart live.  ``execution`` and the poll-interval fields are
  therefore inert here (they drive the real trader); polling venues
  stream at the broker's own intervals.
* Runs blocking like ``Trader.run()``: returns on Ctrl+C or once every feed
  stream has ended (a dead feed means the session is over — a note names
  the pair).  Feeds are always stopped cleanly.  When a browser UI was
  opened, the keep-alive holds the servers up after the script ends
  until one more Ctrl+C, so the chart/report stay viewable.

Event bridging (the contract)
---------------------------------
``LiveSimulation`` emits plain dicts (``simulate`` may not import
``trader``); the :class:`_EventBridge` here turns them into the typed
events on a per-pair :class:`~AlgoTradeKit.trader.EventStream`, tagged
``SOURCE_SIM``.  The dicts carry no cause, so the bridge classifies:

* an ``sl_move`` accompanied by a ``tp_level`` for the same trade on the
  same candle is suppressed — the ladder move rides ``TpLevelEvent.new_sl``
  (: "ladder moves ride TpLevelEvent");
* a remaining ``sl_move`` whose position advanced ``last_rr_hit`` is a
  fraction-less ladder advance → ``SlMoveEvent(cause="ladder")``;
* anything else is a trailing move → ``cause="trailing"``.

Risk-free jumps never reach the dict stream (the engine does not record
them in ``sl_history``), so the bridge detects them itself: once per closed
candle it scans the stepper's open positions for ``risk_free_triggered``
flips and emits :class:`~AlgoTradeKit.trader.RiskFreeEvent`.  Known edge: a
jump whose break-even SL is hit on the very same candle prints only the
``CLOSE`` line (reason ``rf``) — the position is already gone.
"""

from __future__ import annotations

import threading
import time
import warnings
from dataclasses import dataclass
from typing import Any

from ..simulate import (
    EVENT_CLOSE,
    EVENT_EXIT_SIGNAL,
    EVENT_OPEN,
    EVENT_SIGNAL,
    EVENT_SL_MOVE,
    EVENT_TP_LEVEL,
    TP_MODE_MULTI_RR,
    SimulateReport,
)
from ..simulate._live import LiveSimulation
from ._config import DISPLAY_TRADES_SIM, TraderConfig, TraderPair, validate_pairs
from ._events import (
    SOURCE_SIM,
    CloseEvent,
    EventStream,
    ExitSignalEvent,
    OpenEvent,
    RiskFreeEvent,
    SignalEvent,
    SlMoveEvent,
    TpLevelEvent,
    attach_terminal_printer,
)

#: Cadence of the blocking wait loop's liveness check (seconds).
_WAIT_POLL_SECONDS = 0.2

_LOG_PREFIX = "[AlgoTradeKit] run_live"


def _signal_rr(signal: Any) -> float | None:
    """Reward:risk of a Signal, when it defines a TP (else ``None``)."""
    if signal.take_profit is None:
        return None
    risk = abs(float(signal.entry_price) - float(signal.stop_loss))
    if risk <= 0:
        return None
    return abs(float(signal.take_profit) - float(signal.entry_price)) / risk


class _EventBridge:
    """
    ``LiveSimulation`` dict events → typed events, tagged
    ``SOURCE_SIM`` — see the module docstring for the classification rules.

    The bridge keeps a tiny per-trade memory (last known SL,
    ``risk_free_triggered`` / ``last_rr_hit`` already seen, tp-level marks)
    fed by the events themselves; :meth:`prime` snapshots the positions
    still open after the seed (they produced no events — the seed is a
    historical replay).
    """

    def __init__(self, config: TraderConfig, stream: EventStream) -> None:
        self._config = config
        self._stream = stream
        self._sim: LiveSimulation | None = None
        self._sl_known: dict[int, float] = {}   # trade_id -> last known SL
        self._rf_known: dict[int, bool] = {}    # trade_id -> risk-free flip seen
        self._rr_known: dict[int, int] = {}     # trade_id -> last_rr_hit seen
        self._tp_marked: dict[int, int] = {}    # trade_id -> ts of tp_level event

    def bind(self, sim: LiveSimulation) -> None:
        """Attach the LiveSimulation this bridge reads position state from."""
        self._sim = sim

    def prime(self) -> None:
        """Snapshot the seed-carried open positions (call right after
        ``seed()``): their current SL / risk-free / ladder state must not
        re-fire as live events."""
        for pos in self._open_positions():
            self._sl_known[pos.trade_id] = float(pos.stop_loss)
            self._rf_known[pos.trade_id] = bool(pos.risk_free_triggered)
            self._rr_known[pos.trade_id] = int(pos.last_rr_hit)

    # ------------------------------------------------------------------
    # LiveSimulation callbacks
    # ------------------------------------------------------------------

    def on_event(self, event: dict) -> None:
        """``LiveSimulation.on_event`` — dispatch one dict event."""
        etype = event.get("type")
        if etype == EVENT_SIGNAL:
            self._on_signal(event)
        elif etype == EVENT_EXIT_SIGNAL:
            self._on_exit_signal(event)
        elif etype == EVENT_OPEN:
            self._on_open(event)
        elif etype == EVENT_TP_LEVEL:
            self._on_tp_level(event)
        elif etype == EVENT_CLOSE:
            self._on_close(event)
        elif etype == EVENT_SL_MOVE:
            self._on_sl_move(event)

    def on_report(self, report: SimulateReport) -> None:
        """``LiveSimulation.on_report`` — fires once per closed candle,
        even on candles with no trade events: detect risk-free flips
        (the engine records no ``sl_history`` entry for them, so no
        ``sl_move`` dict ever arrives)."""
        del report  # the candle timestamp comes from the master frame
        config = self._config
        if not config.risk_free_enabled or config.tp_mode == TP_MODE_MULTI_RR:
            return
        for pos in self._open_positions():
            tid = pos.trade_id
            if not pos.risk_free_triggered or self._rf_known.get(tid, False):
                continue
            old_sl = self._sl_known.get(tid, float(pos.initial_stop_loss))
            self._rf_known[tid] = True
            self._sl_known[tid] = float(pos.stop_loss)
            self._stream.emit(RiskFreeEvent(
                time=self._current_ts(),
                symbol=config.symbol,
                source=SOURCE_SIM,
                trade_id=tid,
                rr_level=config.risk_free_at_rr,
                old_sl=old_sl,
                new_sl=float(pos.stop_loss),
            ))

    # ------------------------------------------------------------------
    # Per-type handlers
    # ------------------------------------------------------------------

    def _on_signal(self, event: dict) -> None:
        sig = event["signal"]
        self._stream.emit(SignalEvent(
            time=event["time"],
            symbol=event["symbol"],
            source=SOURCE_SIM,
            direction=sig.direction,
            entry_price=sig.entry_price,
            stop_loss=sig.stop_loss,
            take_profit=sig.take_profit,
            rr=_signal_rr(sig),
            timeframe=sig.timeframe,
            metadata=dict(sig.metadata),
            signal=sig,
        ))

    def _on_exit_signal(self, event: dict) -> None:
        exit_signal = event["exit_signal"]
        action = "force_close" if self._config.force_close_on_exit_signal else "none"
        self._stream.emit(ExitSignalEvent(
            time=event["time"],
            symbol=event["symbol"],
            source=SOURCE_SIM,
            reason=exit_signal.reason,
            action=action,
            exit_signal=exit_signal,
        ))

    def _on_open(self, event: dict) -> None:
        tid = event["trade_id"]
        self._sl_known[tid] = float(event["stop_loss"])
        self._rf_known[tid] = False
        self._rr_known[tid] = 0
        self._stream.emit(OpenEvent(
            time=event["time"],
            symbol=event["symbol"],
            source=SOURCE_SIM,
            trade_id=tid,
            direction=event["direction"],
            fill_price=event["entry_price"],
            size=event["size"],
            margin_amount=event["margin_amount"],
            risk_amount=event["risk_amount"],
            stop_loss=event["stop_loss"],
            next_tp=event["next_tp"],
            order_id=None,   # sim fill — there is no venue order
        ))

    def _on_tp_level(self, event: dict) -> None:
        tid = event["trade_id"]
        trade = event["trade"]
        pos = self._find_pos(tid)

        levels = self._config.tp_levels or []
        hit = int(trade.rr_levels_hit)
        level = float(levels[hit - 1]) if 1 <= hit <= len(levels) else float(hit)
        fraction = 0.0
        new_sl = None
        if pos is not None:
            if pos.original_size:
                fraction = float(trade.size) / float(pos.original_size)
            new_sl = float(pos.stop_loss)   # the ladder already moved it (math)
            self._rr_known[tid] = int(pos.last_rr_hit)
            self._sl_known[tid] = float(pos.stop_loss)
        self._tp_marked[tid] = int(event["time"])

        self._stream.emit(TpLevelEvent(
            time=event["time"],
            symbol=event["symbol"],
            source=SOURCE_SIM,
            trade_id=tid,
            level=level,
            fraction_closed=fraction,
            realized_pnl=trade.net_pnl,
            new_sl=new_sl,
            trade=trade,
        ))

    def _on_close(self, event: dict) -> None:
        tid = event["trade_id"]
        trade = event["trade"]
        for memo in (self._sl_known, self._rf_known, self._rr_known, self._tp_marked):
            memo.pop(tid, None)
        self._stream.emit(CloseEvent(
            time=event["time"],
            symbol=event["symbol"],
            source=SOURCE_SIM,
            trade_id=tid,
            exit_price=trade.exit_price,
            reason=trade.close_reason,
            gross_pnl=trade.gross_pnl,
            net_pnl=trade.net_pnl,
            pnl_r=trade.pnl_r,
            duration_ms=trade.duration_ms,
            trade=trade,
        ))

    def _on_sl_move(self, event: dict) -> None:
        tid = event["trade_id"]
        self._sl_known[tid] = float(event["new_sl"])
        pos = self._find_pos(tid)

        if self._tp_marked.pop(tid, None) == int(event["time"]):
            # Ladder move of a partial close — it already rode the
            # TpLevelEvent's new_sl (contract); no standalone event.
            if pos is not None:
                self._rr_known[tid] = int(pos.last_rr_hit)
            return

        cause = "trailing"
        if pos is not None and int(pos.last_rr_hit) != self._rr_known.get(tid, 0):
            cause = "ladder"   # fraction-less multi-RR advance (no partial close)
            self._rr_known[tid] = int(pos.last_rr_hit)

        self._stream.emit(SlMoveEvent(
            time=event["time"],
            symbol=event["symbol"],
            source=SOURCE_SIM,
            trade_id=tid,
            old_sl=event["old_sl"],
            new_sl=event["new_sl"],
            cause=cause,
            next_tp=event["next_tp"],
        ))

    # ------------------------------------------------------------------
    # Position-state helpers
    # ------------------------------------------------------------------

    def _open_positions(self) -> list:
        sim = self._sim
        if sim is None or sim.stepper is None:
            return []
        return list(sim.stepper.open_positions)

    def _find_pos(self, trade_id: int):
        for pos in self._open_positions():
            if pos.trade_id == trade_id:
                return pos
        return None

    def _current_ts(self) -> int:
        """Timestamp of the newest closed candle in the master frame."""
        sim = self._sim
        if sim is None:
            return 0
        df = sim.data.get(sim.strategy.primary_timeframe)
        if df is None or not len(df):
            return 0
        return int(df["timestamp"].iloc[-1])


class _CombinedLiveReport:
    """
    The one-combined-report manager — deliberately source-agnostic:
    it only ever sees ``label → SimulateReport`` snapshots, so ``run_live``
    feeds it paper reports today and the Trader display bridge can drive
    the same class with its sim/real reports.

    Construction builds the initial combined payload
    (:func:`~AlgoTradeKit.report.build_combined_report_payload` — merged
    trade list, summed equity curve, per-pair breakdown), starts a
    :class:`~AlgoTradeKit.report.ReportServer` on *host* (auto port) and
    serves it; :meth:`update` swaps in one pair's fresh snapshot and
    re-pushes the rebuilt payload over the existing WebSocket
    (``push_update``) — the open page re-renders and the replay cache
    stays current.  ``update`` is thread-safe: snapshots arrive on each
    pair's feed thread.
    """

    def __init__(
        self,
        labels: list[str],
        reports: list,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        open_browser: bool = True,
        title: str = "AlgoTradeKit Combined Report",
    ) -> None:
        # Lazy import (LiveSimulation precedent): the report stack loads only
        # when a combined page is actually served.
        from ..report import ReportServer, build_combined_report_payload

        self._build = build_combined_report_payload
        self._lock = threading.Lock()
        self._reports = dict(zip(labels, reports))   # insertion order = pair order
        self.server = ReportServer(title=title, port=port, host=host)
        self.server.start(open_browser=open_browser)
        self.server.set_report_data(self._build(self._reports))

    @property
    def url(self) -> str:
        """Browsable URL of the combined report page."""
        return self.server.url

    def update(self, label: str, report) -> None:
        """Store *label*'s fresh snapshot, rebuild and push the payload."""
        with self._lock:
            self._reports[label] = report
            self.server.push_update(self._build(self._reports))


def _combined_labels(entries: list[TraderPair]) -> list[str]:
    """Per-pair labels for the combined payload, in pair order: the
    symbol, ``#2``-suffixed when the same symbol repeats across brokers
    (labels must be unique; allows one symbol on several venues)."""
    labels: list[str] = []
    seen: dict[str, int] = {}
    for pair in entries:
        base = pair.config.symbol
        count = seen.get(base, 0) + 1
        seen[base] = count
        labels.append(base if count == 1 else f"{base}#{count}")
    return labels


@dataclass
class _PaperSession:
    """One pair's wiring: LiveSimulation + event stream + bridge."""

    pair: TraderPair
    events: EventStream
    bridge: _EventBridge
    sim: LiveSimulation | None = None
    combined: _CombinedLiveReport | None = None
    combined_label: str = ""

    def on_report(self, report) -> None:
        """``LiveSimulation.on_report`` — once per closed candle: bridge
        first (risk-free detection), then the combined-report refresh."""
        self.bridge.on_report(report)
        if self.combined is not None:
            self.combined.update(self.combined_label, report)


def _build_session(pair: TraderPair, initial_balance: float) -> _PaperSession:
    """Validate one pair for paper mode and wire its LiveSimulation."""
    config = pair.config
    if config.display_trades != DISPLAY_TRADES_SIM:
        raise ValueError(
            f"run_live: display_trades={config.display_trades!r} is not available in "
            f"paper trading — there are no real fills; use '{DISPLAY_TRADES_SIM}' "
            f"({config.symbol})."
        )
    if not config.display and not config.log_events:
        warnings.warn(
            f"run_live({config.symbol}): display and log_events are both off — a "
            "silent paper run produces no visible output (it still runs; the final "
            "report is returned)."
        )

    events = EventStream()
    attach_terminal_printer(events, config)   # no-op when log_events=False
    bridge = _EventBridge(config, events)

    sim_config = config.to_simulate_config(
        pair.broker,
        initial_balance=initial_balance,
        primary_timeframe=pair.strategy.primary_timeframe,
    )

    # Seed source: honor the display seed fields whenever set (display on or
    # off); with neither — only possible when display=False — seed the
    # strategy minimum.  The fetched count must reach min_candles either way.
    display_candles = config.display_candles
    display_start = config.display_start
    if display_candles is None and display_start is None:
        display_candles = config.min_candles

    session = _PaperSession(pair=pair, events=events, bridge=bridge)
    sim = LiveSimulation(
        pair.broker,
        pair.strategy,
        sim_config,
        display_candles=display_candles,
        display_start=display_start,
        min_seed_candles=config.min_candles,
        candle_count_limit=config.candle_count_limit,
        recompute_window=config.recompute_window,
        chart_host=config.chart_host,
        chart_port=config.chart_port,
        report_port=config.report_port,
        open_browser=config.display_open_browser,
        on_event=bridge.on_event,
        on_report=session.on_report,
    )
    bridge.bind(sim)
    session.sim = sim
    return session


def _maybe_start_combined(
    entries: list[TraderPair], sessions: list[_PaperSession]
) -> _CombinedLiveReport | None:
    """ combined-report gate: two or more pairs with at least one
    displaying pair → serve the aggregate page; host and open-browser-vs-
    print-URL behaviour follow the **first** displaying pair.  Every pair
    contributes its snapshots (a log-only pair still appears in the
    breakdown); with no displaying pair the session stays log-only and no
    combined server starts."""
    if len(sessions) < 2:
        return None
    display_configs = [s.pair.config for s in sessions if s.pair.config.display]
    if not display_configs:
        return None
    first = display_configs[0]
    labels = _combined_labels(entries)
    combined = _CombinedLiveReport(
        labels,
        [session.sim.last_report for session in sessions],
        host=first.chart_host,
        open_browser=first.display_open_browser,
    )
    for label, session in zip(labels, sessions):
        session.combined = combined
        session.combined_label = label
    if not first.display_open_browser:
        print(f"{_LOG_PREFIX}: combined report → {combined.url}")
    return combined


def _print_urls(session: _PaperSession) -> None:
    """ URL flow, paper edition: display on + no browser → print the
    chart/report addresses for the user's own browser."""
    symbol = session.pair.config.symbol
    chart = session.sim.chart
    if chart is not None and getattr(chart, "url", None):
        print(f"{_LOG_PREFIX} {symbol} chart  → {chart.url}")
    server = session.sim.report_server
    if server is not None and getattr(server, "url", None):
        print(f"{_LOG_PREFIX} {symbol} report → {server.url}")


def _wait(sessions: list[_PaperSession]) -> None:
    """Block until every feed stream has ended (KeyboardInterrupt passes
    through to the caller)."""
    dead: set[int] = set()
    while True:
        alive = 0
        for idx, session in enumerate(sessions):
            stream = session.sim.stream
            if stream is not None and stream.alive:
                alive += 1
            elif idx not in dead:
                dead.add(idx)
                print(f"{_LOG_PREFIX}: feed for {session.pair.config.symbol} ended.")
        if alive == 0:
            print(f"{_LOG_PREFIX}: all feeds ended — stopping.")
            return
        time.sleep(_WAIT_POLL_SECONDS)


def run_live(
    strategy: Any = None,
    broker: Any = None,
    config: TraderConfig | None = None,
    *,
    pairs: Any = None,
    initial_balance: float = 10_000.0,
) -> SimulateReport | list[SimulateReport]:
    """
    Paper-trade one or more strategies on live market data —
    **no order is ever placed**; positions are simulated by the sim engine.

    Single pair — "config + strategy + broker, these three are enough"::

        from AlgoTradeKit.trader import run_live

        run_live(strategy=MyStrategy(),
                 broker=Broker("binance-futures"),
                 config=TraderConfig(symbol="BTCUSDT", min_candles=500,
                                     display=True, display_candles=500))

    Multi-pair — the same entry list as ``Trader``::

        run_live(pairs=[
            TraderPair(broker=binance, config=cfg_btc, strategy=StratA()),
            TraderPair(broker=mt5,     config=cfg_eur, strategy=StratB()),
        ])

    Parameters
    ----------
    strategy, broker, config
        The single-pair form (all three required together).  ``config`` is
        the **same** :class:`TraderConfig` the real-order ``Trader`` takes —
        ``display_trades`` must stay ``"sim"`` (``"real"`` / ``"both"``
        raise: there are no real fills in paper mode).
    pairs
        The multi-pair form — an iterable of :class:`TraderPair`; mutually
        exclusive with the single-form arguments.  The same
        ``(broker, symbol)`` may not appear twice.  Every pair gets its
        own chart, report and ``[SIM]`` event log; with two or more pairs
        and at least one displaying pair, **one combined report** across all
        pairs is served too — merged trade list, equity curve summed
        across the paper wallets, per-pair breakdown — refreshed on every
        closed candle (its URL is printed when the first displaying pair has
        ``display_open_browser=False``).
    initial_balance : float
        Paper wallet per pair (default 10 000).  Sizing, compounding and the
        report baseline use it exactly as in a backtest.

    Blocks until Ctrl+C or until every feed has ended, then stops the feeds
    and returns the final report snapshot(s): the single-pair form returns
    one ``SimulateReport``, the ``pairs`` form a list in pair order.  With a
    browser display, the keep-alive holds the servers up after the
    script finishes until one more Ctrl+C.
    """
    if pairs is not None:
        if strategy is not None or broker is not None or config is not None:
            raise ValueError(
                "run_live: give either (strategy, broker, config) or "
                "pairs=[TraderPair, ...] — not both."
            )
        entries = validate_pairs(pairs)
        multi = True
    else:
        if strategy is None or broker is None or config is None:
            raise ValueError(
                "run_live: strategy, broker and config are all required "
                "(or use pairs=[TraderPair, ...])."
            )
        entries = validate_pairs(
            [TraderPair(broker=broker, config=config, strategy=strategy)]
        )
        multi = False

    initial_balance = float(initial_balance)
    if initial_balance <= 0:
        raise ValueError(
            f"run_live: initial_balance must be > 0, got {initial_balance}."
        )

    sessions = [_build_session(pair, initial_balance) for pair in entries]

    for session in sessions:
        session.sim.seed()
        session.bridge.prime()
        session_config = session.pair.config
        if session_config.display and not session_config.display_open_browser:
            _print_urls(session)

    #: the one combined report across all pairs, fed per closed
    # candle through each session's on_report — armed before the feeds start
    # so no snapshot is missed.
    _maybe_start_combined(entries, sessions)

    for session in sessions:
        session.sim.start()

    symbols = ", ".join(session.pair.config.symbol for session in sessions)
    print(f"{_LOG_PREFIX}: paper trading {symbols} — Ctrl+C to stop.")

    try:
        _wait(sessions)
    except KeyboardInterrupt:
        print(f"{_LOG_PREFIX}: Ctrl+C — stopping.")
    finally:
        for session in sessions:
            session.sim.stop()

    reports = [session.sim.last_report for session in sessions]
    return reports if multi else reports[0]
