"""
AlgoTradeKit.simulate._live
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
``LiveSimulation`` (v1.0.0) — the live-simulation core: run a strategy
on live market data with **simulated** positions.  It is the internal engine
behind ``run_live()`` paper trading and the Trader display; no
order/execution code is ever touched.

Life-cycle
----------
1. **Seed** — ``seed()`` fetches the primary-timeframe history from the
   broker (the last ``display_candles`` candles **or** from
   ``display_start`` until now), runs the strategy over it
   (``strategy.run`` → ``prepare_indicators`` + ``setup`` + all signals),
   replays it through a :class:`SimulationStepper` **without finalizing**
   (positions open at seed end stay open into the live phase), and produces
   the initial chart and report exactly the existing batch way —
   ``config.show_chart`` / ``config.report_mode`` / ``config.chart_indicators``
   control the display, like ``Simulate.run``.
2. **Step** — on each closed candle: append to the master data +
   incremental strategy update (:func:`advance_live_candle`) → advance
   the step-driven sim → push chart updates (bar, live SL/TP lines,
   final posbox on close —) and a report refresh
   (``ReportServer.push_update``) → emit a ``SimulateReport`` snapshot
   (``on_report``) and per-trade events (``on_event``).
3. **Window** — with ``candle_count_limit=N`` the candles are kept in a
   bounded deque; when old candles fall out, trades whose lifetime left the
   window are dropped and the report is recomputed over the window only
.  The chart trims itself to the same limit (both sides).

Driving the feed
----------------
``start()`` subscribes ``broker.stream_candles`` itself (with
``closed_only=False`` when a chart is shown so forming candles render live,
``closed_only=True`` otherwise) and routes candles by their ``closed`` flag;
``stop()`` ends the stream.  Alternatively an external driver (the Trader's
display queue) can call :meth:`process_closed_candle` /
:meth:`process_forming_candle` directly — same semantics, no built-in feed.

Sim granularity: fills / SL / TP happen on **closed candles** only (the
engine is candle-based).  Forming candles are display-only — they stream to
the chart but never touch the strategy or the stepper
(``update_indicators`` / ``evaluate_forming_candle`` are realtime *trader*
concerns).

Events (``on_event`` callback)
------------------------------
Plain dicts — the typed event stream and the terminal printer are the
``trader`` module's job (``simulate`` may not import ``trader``).
Every event carries ``type``, ``time`` (UTC ms) and ``symbol``; the rest by
type (constants re-exported from ``AlgoTradeKit.simulate``):

======================  ====================================================
``EVENT_SIGNAL``        ``signal`` (the ``Signal``)
``EVENT_EXIT_SIGNAL``   ``exit_signal`` (the ``ExitSignal``)
``EVENT_OPEN``          ``trade_id, direction, entry_price, size,
                        margin_amount, risk_amount, stop_loss, next_tp``
``EVENT_SL_MOVE``       ``trade_id, old_sl, new_sl, next_tp`` (coalesced —
                        one event per candle per position, latest state)
``EVENT_TP_LEVEL``      ``trade_id, trade`` (the partial ``ClosedTrade``
                        slice), ``levels_hit`` — position still open
``EVENT_CLOSE``         ``trade_id, trade`` (the ``ClosedTrade``) — nothing
                        of this trade remains open
======================  ====================================================

Seeding emits **no** events (the seed is historical replay, not live
activity); callback exceptions are caught and warned, never propagated.

Windowed report semantics
------------------------------
* Baseline balance = **equity entering the window**: the equity of the
  newest balance snapshot that fell out of the window (before anything has
  fallen out, ``config.initial_balance`` — the report is then the plain
  full report).
* A trade is dropped once its whole lifetime left the window
  (``close_time < window start``); a trade opened before the window but
  closed inside it stays — its realized PnL lands inside the window.
* The strategy's master DataFrame is also trimmed, but to
  ``max(candle_count_limit, recompute_window, warmup_period + 1)`` rows so
  the signal maths never degrades.  Caveat: row indices shift after
  trimming — hook strategies must not keep absolute row indices in
  ``self.*`` state.

Known limits (documented, by design)
------------------------------------
* Gap-fill after downtime is **not** done here — stale/duplicate candles
  are skipped, missed candles are appended as they arrive; exact-time
  boundary arithmetic and gap-filling belong to the trader scheduler.
* ``config.chart_indicators`` are computed once at seed time and are not
  recomputed per live candle (the in-browser toolbar still works).
* Multi-timeframe strategies: only the primary timeframe advances live
  (limitation) — other timeframes keep their seed content.
* All Python-side times are UTC milliseconds; chart times are Unix seconds.
"""

from __future__ import annotations

import threading
import warnings
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pandas as pd

from ..strategy._base import BaseStrategy
from ..strategy._incremental import advance_live_candle, default_recompute_window
from ..strategy._types import StrategyMode, StrategyResult
from ._config import (
    REPORT_MODE_BOTH,
    REPORT_MODE_NONE,
    REPORT_MODE_SAVE,
    REPORT_MODE_WEBPAGE,
    SimulateConfig,
)
from ._engine import SimulationStepper, _register_keep_alive
from ._position import ClosedTrade
from ._report import SimulateReport, _build_trade_markers, build_report

if TYPE_CHECKING:  # pragma: no cover
    from ..broker import BaseBroker, Stream

# ---------------------------------------------------------------------------
# Event-type constants (dict ``type`` values of the on_event callback)
# ---------------------------------------------------------------------------

EVENT_SIGNAL = "signal"            # strategy emitted an entry Signal
EVENT_EXIT_SIGNAL = "exit_signal"  # strategy emitted an ExitSignal
EVENT_OPEN = "open"                # sim opened a position
EVENT_SL_MOVE = "sl_move"          # trailing / risk-free / ladder SL move
EVENT_TP_LEVEL = "tp_level"        # multi-RR level partially closed (still open)
EVENT_CLOSE = "close"              # position fully closed

#: Standard candle keys stored in the window deque / streamed to the chart.
_CANDLE_KEYS = ("timestamp", "open", "high", "low", "close", "volume")


class LiveSimulation:
    """
    Live-simulation core (v1.0.0) — seed → step → window.

    Parameters
    ----------
    broker : BaseBroker
        Feed source: ``fetch_last_candles`` / ``fetch_candles`` for the
        seed, ``stream_candles`` for :meth:`start`.  Market data only — no
        order method is ever called.
    strategy : BaseStrategy
        The strategy.  Its ``primary_timeframe`` is the feed timeframe and
        must equal ``config.primary_timeframe``.
    config : SimulateConfig
        Full simulation configuration.  ``symbol`` is required (the feed
        needs an instrument); ``show_chart`` / ``report_mode`` /
        ``report_save_path`` / ``chart_indicators`` control the display
        exactly as in ``Simulate.run``.
    display_candles : int | None
        Seed with the last N candles.  Exactly one of *display_candles* /
        *display_start* must be given.
    display_start : str | datetime | int | None
        Seed from this moment until now.  Accepts the library's usual time
        inputs (``"YYYY/MM/DD"``, ``datetime``, UTC-ms int, …).
    min_seed_candles : int | None
        Minimum acceptable seed length — fewer fetched candles raise a
        ``ValueError`` at fetch time (before the strategy or any server
        runs). pass ``TraderConfig.min_candles`` here: it is the
        seed-time count check for ``display_start`` and catches a venue
        whose history is shorter than ``display_candles``.
    candle_count_limit : int | None
        Rolling window: chart and report only ever contain the last N
        candles; ``None`` disables the window.
    recompute_window : int | None
        Tail size K for the fallback recompute (strategies without the
        ``update_indicators`` hook).  ``None`` → ``max(warmup, 200)``.
    chart_host, chart_port : str, int
        Bind address / port of the chart server (0 = auto port).
    report_host : str | None
        Bind address of the report server.  ``None`` → *chart_host*.
    report_port : int
        Report server port (0 = auto).
    open_browser : bool
        Open browser tabs for chart/report.  ``False`` for headless use
        (the URL-printing flow builds on this).
    on_event : callable(dict) | None
        Per-trade event callback — see the module docstring for the
        schemas.
    on_report : callable(SimulateReport) | None
        Called with a fresh report snapshot after every closed candle.

    Threading
    ---------
    ``process_*`` run under an internal lock — the broker's stream thread
    and user threads can share the instance.  :meth:`stop` never takes the
    lock (it must be able to join a stream thread that is mid-step).
    """

    def __init__(
        self,
        broker: BaseBroker,
        strategy: BaseStrategy,
        config: SimulateConfig,
        *,
        display_candles: int | None = None,
        display_start: Any = None,
        min_seed_candles: int | None = None,
        candle_count_limit: int | None = None,
        recompute_window: int | None = None,
        chart_host: str = "127.0.0.1",
        chart_port: int = 0,
        report_host: str | None = None,
        report_port: int = 0,
        open_browser: bool = True,
        on_event: Callable[[dict], None] | None = None,
        on_report: Callable[[SimulateReport], None] | None = None,
    ) -> None:
        if (display_candles is None) == (display_start is None):
            raise ValueError(
                "LiveSimulation: exactly one of display_candles / display_start "
                "must be given (seed by count or from a start time)."
            )
        if display_candles is not None:
            display_candles = int(display_candles)
            if display_candles < 1:
                raise ValueError(
                    f"LiveSimulation: display_candles must be >= 1, got {display_candles}."
                )
        if min_seed_candles is not None:
            min_seed_candles = int(min_seed_candles)
            if min_seed_candles < 1:
                raise ValueError(
                    f"LiveSimulation: min_seed_candles must be >= 1, got {min_seed_candles}."
                )
        if candle_count_limit is not None:
            candle_count_limit = int(candle_count_limit)
            if candle_count_limit < 1:
                raise ValueError(
                    f"LiveSimulation: candle_count_limit must be >= 1, got {candle_count_limit}."
                )
        if recompute_window is not None:
            recompute_window = int(recompute_window)
            if recompute_window < 1:
                raise ValueError(
                    f"LiveSimulation: recompute_window must be >= 1, got {recompute_window}."
                )
        if not config.symbol:
            raise ValueError(
                "LiveSimulation: config.symbol is required — the live feed needs "
                "an instrument to fetch and stream."
            )
        if strategy.primary_timeframe != config.primary_timeframe:
            raise ValueError(
                f"LiveSimulation: strategy.primary_timeframe "
                f"({strategy.primary_timeframe!r}) must equal "
                f"config.primary_timeframe ({config.primary_timeframe!r})."
            )

        self.broker = broker
        self.strategy = strategy
        self.config = config

        self._tf = strategy.primary_timeframe
        self._display_candles = display_candles
        self._display_start = display_start
        self._min_seed_candles = min_seed_candles
        self._candle_count_limit = candle_count_limit
        self._recompute_window = recompute_window
        self._chart_host = chart_host
        self._chart_port = chart_port
        self._report_host = report_host if report_host is not None else chart_host
        self._report_port = report_port
        self._open_browser = open_browser
        self._on_event = on_event
        self._on_report = on_report

        # Rolling window: candles kept in a bounded deque; the strategy
        # frame keeps enough extra tail for the recompute/warmup maths.
        self._window: deque | None = (
            deque(maxlen=candle_count_limit) if candle_count_limit else None
        )
        self._window_engaged = False                      # first candle has fallen out
        self._baseline_balance = float(config.initial_balance)
        self._frame_keep: int | None = None
        if candle_count_limit:
            resolved = (
                recompute_window
                if recompute_window is not None
                else default_recompute_window(strategy)
            )
            self._frame_keep = max(
                candle_count_limit, resolved, int(strategy.warmup_period) + 1
            )

        # Runtime state
        self._data: dict[str, pd.DataFrame] = {}
        self._stepper: SimulationStepper | None = None
        self._chart = None
        self._chart_server = None
        self._report_server = None
        self._stream: Stream | None = None
        self._last_report: SimulateReport | None = None
        self._last_ts: int | None = None                  # last appended CLOSED candle ts
        self._slices: dict[int, list[ClosedTrade]] = {}   # partial closes per open trade
        self._sl_seen: dict[int, int] = {}                # sl_history entries already emitted
        self._live_drawings: dict[int, str] = {}          # trade_id -> chart drawing id
        self._seeded = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def seeded(self) -> bool:
        """True once :meth:`seed` has completed."""
        return self._seeded

    @property
    def last_report(self) -> SimulateReport | None:
        """The most recent report snapshot (seed or last closed candle)."""
        return self._last_report

    @property
    def stepper(self) -> SimulationStepper | None:
        """The underlying step-driven engine (advanced use /)."""
        return self._stepper

    @property
    def data(self) -> dict[str, pd.DataFrame]:
        """The master data dict (frames are re-bound as candles arrive)."""
        return self._data

    @property
    def chart(self):
        """The live chart, or ``None`` (``config.show_chart=False``)."""
        return self._chart

    @property
    def report_server(self):
        """The live report server, or ``None`` (no webpage report mode)."""
        return self._report_server

    @property
    def stream(self) -> Stream | None:
        """The active feed stream handle from :meth:`start`, if any."""
        return self._stream

    # ------------------------------------------------------------------
    # Seed
    # ------------------------------------------------------------------

    def seed(self) -> SimulateReport:
        """
        Fetch the seed history, run the strategy and the simulation over
        it, and produce the initial chart + report.

        Positions still open at the end of the seed are **not** closed —
        they carry into the live phase (drawn as live positions on the
        chart, listed in ``report.open_at_end``).

        Returns the initial ``SimulateReport`` snapshot (windowed when the
        seed is longer than ``candle_count_limit``).
        """
        with self._lock:
            if self._seeded:
                raise RuntimeError("LiveSimulation: seed() already ran.")

            df = self._fetch_seed_df()
            result = self.strategy.run({self._tf: df}, mode=StrategyMode.BACKTEST)
            self._data = result.data

            stepper = SimulationStepper(self.config)
            self._stepper = stepper

            sigs_by_idx: dict[int, list] = {}
            for sig in result.signals:
                sigs_by_idx.setdefault(sig.candle_index, []).append(sig)
            exits_by_idx: dict[int, list] = {}
            for ex in result.exit_signals:
                exits_by_idx.setdefault(ex.candle_index, []).append(ex)

            primary = self._data[self._tf]
            for idx, row in enumerate(primary.itertuples(index=False)):
                candle = {
                    "timestamp": int(row.timestamp),
                    "open": float(row.open),
                    "high": float(row.high),
                    "low": float(row.low),
                    "close": float(row.close),
                    "volume": float(row.volume),
                }
                closed_now = stepper.step(
                    candle, sigs_by_idx.get(idx, ()), exits_by_idx.get(idx, ())
                )
                self._record_slices(closed_now)
                self._advance_window(candle)

            self._trim_strategy_frame()

            # Keep partial-close slices only for trades still open; seed
            # emits no events — it is a historical replay, not live activity.
            open_ids = {p.trade_id for p in stepper.open_positions}
            self._slices = {t: s for t, s in self._slices.items() if t in open_ids}
            self._sl_seen = {
                p.trade_id: len(p.sl_history) for p in stepper.open_positions
            }
            self._last_ts = int(primary["timestamp"].iloc[-1])

            report = self._snapshot_report()
            self._last_report = report
            self._seeded = True

            self._render_seed(report, result)
            return report

    def _fetch_seed_df(self) -> pd.DataFrame:
        """Fetch the seed candles from the broker as a standard DataFrame."""
        from ..broker._timeutil import now_ms, parse_to_ms

        symbol, tf = self.config.symbol, self._tf
        if self._display_candles is not None:
            rows = self.broker.fetch_last_candles(symbol, tf, self._display_candles)
        else:
            start_ms = parse_to_ms(self._display_start)
            rows = self.broker.fetch_candles(symbol, tf, start_ms, now_ms())

        if not rows:
            raise ValueError(
                f"LiveSimulation: the broker returned no seed candles for "
                f"{symbol!r} {tf!r}."
            )
        if self._min_seed_candles is not None and len(rows) < self._min_seed_candles:
            raise ValueError(
                f"LiveSimulation: the seed returned {len(rows)} candles for "
                f"{symbol!r} {tf!r} — fewer than the required minimum of "
                f"{self._min_seed_candles} (venue history too short or "
                f"display_start too recent)."
            )
        df = pd.DataFrame(rows)
        missing = [c for c in _CANDLE_KEYS if c not in df.columns]
        if missing:
            raise ValueError(
                f"LiveSimulation: seed candles are missing columns {missing} — "
                f"expected the library-standard candle schema."
            )
        df = df[list(_CANDLE_KEYS)].reset_index(drop=True)
        df["timestamp"] = df["timestamp"].astype("int64")
        return df

    # ------------------------------------------------------------------
    # Feed (built-in driver)
    # ------------------------------------------------------------------

    def start(self) -> Stream:
        """
        Subscribe to the broker's candle stream and drive the simulation
        from it.  Forming candles render on the chart (when shown); closed
        candles advance the strategy + sim.  Returns the ``Stream`` handle.
        """
        if not self._seeded:
            raise RuntimeError("LiveSimulation: call seed() before start().")
        if self._stream is not None and self._stream.alive:
            raise RuntimeError("LiveSimulation: the feed is already running.")

        closed_only = self._chart is None   # forming candles are display-only
        self._stream = self.broker.stream_candles(
            self.config.symbol, self._tf, self._on_feed_candle, closed_only=closed_only
        )
        return self._stream

    def stop(self) -> None:
        """Stop the built-in feed (idempotent).  Servers stay up."""
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.stop()

    def _on_feed_candle(self, candle: Mapping) -> None:
        """Stream callback — route by the ``closed`` flag, never raise
        (one bad candle must not kill the feed; philosophy)."""
        try:
            if bool(candle.get("closed", True)):
                self.process_closed_candle(candle)
            else:
                self.process_forming_candle(candle)
        except Exception as exc:
            warnings.warn(f"LiveSimulation: candle processing failed ({exc!r})")

    # ------------------------------------------------------------------
    # Step — one closed candle
    # ------------------------------------------------------------------

    def process_closed_candle(self, candle: Mapping, *, quiet: bool = False) -> list[ClosedTrade]:
        """
        Advance strategy + simulation by one **closed** candle (the
        library-standard dict: ``timestamp`` (UTC ms), OHLCV; extra keys
        such as ``closed`` are ignored).

        Stale or duplicate candles (``timestamp`` ≤ the last appended one)
        are skipped silently — streams may replay the seed's last candle at
        subscribe time.  Gap-filling missed candles is the trader
        scheduler's job, not done here.

        ``quiet=True`` suppresses only the per-candle report-server push —
        the display queue passes it while a candle backlog drains, so
        pushes coalesce to the newest candle.  State, chart streaming,
        events and the ``on_report`` callback are unaffected.

        Returns the trades closed during this candle (partial slices
        included), like ``SimulationStepper.step``.
        """
        with self._lock:
            return self._step_closed(candle, quiet=quiet)

    def process_forming_candle(self, candle: Mapping) -> None:
        """
        Render a **forming** (unclosed) candle on the chart.  Display-only:
        the strategy state and the simulation never advance on forming
        candles.  No-op when no chart is shown or the candle is stale.
        """
        with self._lock:
            if not self._seeded:
                raise RuntimeError("LiveSimulation: call seed() first.")
            ts = int(candle["timestamp"])
            if self._last_ts is not None and ts <= self._last_ts:
                return   # that bar is already final
            if self._chart is not None:
                self._chart.stream_from_atk(self._normalize_candle(candle))

    def _step_closed(self, candle: Mapping, *, quiet: bool = False) -> list[ClosedTrade]:
        if not self._seeded:
            raise RuntimeError("LiveSimulation: call seed() first.")

        ts = int(candle["timestamp"])
        if self._last_ts is not None and ts <= self._last_ts:
            return []   # duplicate / stale delivery — skip

        symbol = self.config.symbol
        signals, exits = advance_live_candle(
            self.strategy, self._data, candle,
            recompute_window=self._recompute_window,
        )
        self._last_ts = ts

        for sig in signals:
            self._emit({"type": EVENT_SIGNAL, "time": ts, "symbol": symbol,
                        "signal": sig})
        for ex in exits:
            self._emit({"type": EVENT_EXIT_SIGNAL, "time": ts, "symbol": symbol,
                        "exit_signal": ex})

        stepper = self._stepper
        prev_open = {p.trade_id for p in stepper.open_positions}
        closed_now = stepper.step(candle, signals, exits)
        self._record_slices(closed_now)
        open_by_id = {p.trade_id: p for p in stepper.open_positions}

        norm = self._normalize_candle(candle)
        if self._chart is not None:
            self._chart.stream_from_atk(dict(norm))

        # --- closes / multi-RR level fills (engine phase C/D) ---
        for ct in closed_now:
            if ct.trade_id in open_by_id:
                pos = open_by_id[ct.trade_id]
                self._emit({"type": EVENT_TP_LEVEL, "time": ts, "symbol": symbol,
                            "trade_id": ct.trade_id, "trade": ct,
                            "levels_hit": pos.last_rr_hit})
            else:
                self._emit({"type": EVENT_CLOSE, "time": ts, "symbol": symbol,
                            "trade_id": ct.trade_id, "trade": ct})
        for tid in dict.fromkeys(ct.trade_id for ct in closed_now):
            if tid not in open_by_id:
                self._finalize_trade_drawing(tid)

        # --- new opens (engine phase E) ---
        for pos in stepper.open_positions:
            if pos.trade_id in prev_open:
                continue
            self._sl_seen[pos.trade_id] = len(pos.sl_history)
            self._emit({"type": EVENT_OPEN, "time": ts, "symbol": symbol,
                        "trade_id": pos.trade_id, "direction": pos.direction,
                        "entry_price": pos.entry_price, "size": pos.size,
                        "margin_amount": pos.margin_amount,
                        "risk_amount": pos.risk_amount,
                        "stop_loss": pos.stop_loss, "next_tp": pos.next_tp})
            if self._chart is not None:
                self._add_live_drawing(self._chart, pos)

        # --- SL moves of surviving positions (trailing / risk-free / ladder;
        #     coalesced: one event per candle with the latest state) ---
        for pos in stepper.open_positions:
            seen = self._sl_seen.get(pos.trade_id, 0)
            n = len(pos.sl_history)
            if n <= seen:
                continue
            old_sl = (
                pos.sl_history[seen - 1]["sl"] if seen > 0 else pos.initial_stop_loss
            )
            latest = pos.sl_history[-1]
            self._sl_seen[pos.trade_id] = n
            self._emit({"type": EVENT_SL_MOVE, "time": int(latest["time"]),
                        "symbol": symbol, "trade_id": pos.trade_id,
                        "old_sl": old_sl, "new_sl": latest["sl"],
                        "next_tp": latest["next_tp"]})
            if self._chart is not None:
                drawing_id = self._live_drawings.get(pos.trade_id)
                if drawing_id:
                    self._chart.update_live_position(
                        drawing_id, stop_loss=latest["sl"], next_tp=latest["next_tp"]
                    )

        # --- window + trims, then the report snapshot ---
        self._advance_window(norm)
        self._trim_strategy_frame()

        report = self._snapshot_report()
        self._last_report = report
        if self._report_server is not None and not quiet:
            self._report_server.push_update(report)
        self._emit_report(report)

        return closed_now

    # ------------------------------------------------------------------
    # Window (candle_count_limit)
    # ------------------------------------------------------------------

    def _advance_window(self, candle: dict) -> None:
        """Append one candle to the bounded deque and, once candles start
        falling out, trim the report inputs to the window: baseline balance
        := equity entering the window; drop trades whose whole lifetime
        left the window."""
        win = self._window
        if win is None:
            return
        if len(win) == win.maxlen:
            self._window_engaged = True
        win.append(candle)
        if not self._window_engaged:
            return

        window_start = int(win[0]["timestamp"])
        stepper = self._stepper

        bh = stepper.balance_history
        while bh and int(bh[0]["timestamp"]) < window_start:
            self._baseline_balance = float(bh.pop(0)["equity"])

        ct = stepper.closed_trades
        while ct and ct[0].close_time < window_start:
            ct.pop(0)

    def _trim_strategy_frame(self) -> None:
        """Bound the master primary frame (window mode only) — kept longer
        than the display window so recompute/warmup maths stay exact."""
        keep = self._frame_keep
        if keep is None:
            return
        df = self._data.get(self._tf)
        if df is not None and len(df) > keep:
            self._data[self._tf] = df.iloc[-keep:].reset_index(drop=True)

    def _snapshot_report(self) -> SimulateReport:
        """Fresh report snapshot — windowed once the window is engaged."""
        stepper = self._stepper
        if not self._window_engaged:
            return stepper.build_report()
        cfg = replace(self.config, initial_balance=self._baseline_balance)
        return build_report(
            list(stepper.closed_trades),
            list(stepper.open_positions),
            list(stepper.balance_history),
            cfg,
        )

    # ------------------------------------------------------------------
    # Display (seed render + live drawing upkeep)
    # ------------------------------------------------------------------

    def _render_seed(self, report: SimulateReport, result: StrategyResult) -> None:
        """Produce the initial chart + report the existing batch way
        (``Simulate._render_post_simulation`` pattern), plus live-position
        drawings for the positions still open at seed end."""
        config = self.config
        has_browser_ui = False

        if config.show_chart:
            self._build_chart(report, result)
            has_browser_ui = True

        if config.report_mode != REPORT_MODE_NONE:
            server = self._start_report(report)
            if server is not None:
                has_browser_ui = True

        if has_browser_ui:
            _register_keep_alive()

    def _build_chart(self, report: SimulateReport, result: StrategyResult) -> None:
        import time as _time

        from ..visual import Chart
        from ..visual.indicator_renderer import add_strategy_drawings, draw_trade_group

        config = self.config
        tf = self._tf

        chart = Chart(
            title=f"{config.symbol or ''} — Live Simulation ({tf})",
            host=self._chart_host,
            port=self._chart_port,
            candle_count_limit=self._candle_count_limit,
        )
        chart.set_data(self._data[tf])

        # Pre-load caller-requested indicators — seed-time computation, same
        # as the batch chart (not recomputed per live candle; see module doc).
        for spec in config.chart_indicators or []:
            try:
                chart.add_indicator_spec(dict(spec))
            except Exception as exc:   # a bad spec must never abort the chart
                warnings.warn(f"chart_indicators: skipped {spec!r} ({exc})")

        if result.drawings:
            add_strategy_drawings(chart, result)

        # Completed trades get the standard posbox + SL/TP segments; trades
        # still open at seed end are drawn as live positions instead.
        open_ids = {p.trade_id for p in self._stepper.open_positions}
        groups: dict[int, list[dict]] = {}
        for marker in report.trade_markers:
            groups.setdefault(marker["trade_id"], []).append(marker)
        for tid, markers in groups.items():
            if tid in open_ids:
                continue
            draw_trade_group(chart, markers, opacity=0.15, config=config)
        for pos in self._stepper.open_positions:
            self._add_live_drawing(chart, pos)

        chart.show(open_browser=self._open_browser, block=False)
        _time.sleep(0.4)   # let the server start before the report links it

        self._chart = chart
        self._chart_server = chart._server

    def _start_report(self, report: SimulateReport):
        import time as _time

        from ..report._builder import build_report_payload
        from ..report._display import save_report_html
        from ..report._server import ReportServer

        config = self.config

        open_page = config.report_mode in (REPORT_MODE_WEBPAGE, REPORT_MODE_BOTH)
        save_file = config.report_mode in (REPORT_MODE_SAVE, REPORT_MODE_BOTH)

        def _on_open_chart(trade_id: int) -> None:
            chart, current = self._chart, self._last_report
            if chart is None or current is None:
                return
            marker = next(
                (m for m in current.trade_markers if m["trade_id"] == trade_id), None
            )
            if marker:
                chart.navigate_to_candle(marker["open_time"])

        server = None
        if open_page:
            payload = build_report_payload(report)
            if self._chart is not None and self._chart_server is not None:
                payload["has_chart"] = True
                payload["chart_port"] = getattr(self._chart_server, "port", None)
            server = ReportServer(
                title=f"AlgoTradeKit Live Report — {config.config_id}",
                host=self._report_host,
                port=self._report_port,
                on_open_chart=_on_open_chart if self._chart is not None else None,
            )
            server.start(open_browser=self._open_browser)
            _time.sleep(0.3)
            server.set_report_data(payload)
            self._report_server = server

        if save_file:
            out_path = save_report_html(report, path=config.report_save_path)
            print(f"[AlgoTradeKit] Report saved → {out_path}")

        return server

    def _add_live_drawing(self, chart, pos) -> None:
        """Draw an open position as a live drawing (``LivePosition``)."""
        next_tp = pos.sl_history[-1]["next_tp"] if pos.sl_history else pos.next_tp
        drawing_id = chart.add_live_position(
            open_time=pos.open_time // 1000,
            entry_price=pos.entry_price,
            stop_loss=pos.stop_loss,
            direction=pos.direction,
            next_tp=next_tp,
            trade_id=pos.trade_id,
        )
        self._live_drawings[pos.trade_id] = drawing_id

    def _finalize_trade_drawing(self, trade_id: int) -> None:
        """A trade fully closed: drop its live drawing and draw the final
        posbox + dynamic SL/TP segments from all its slices — the same
        rendering the batch chart produces (contract)."""
        slices = self._slices.pop(trade_id, [])
        self._sl_seen.pop(trade_id, None)
        drawing_id = self._live_drawings.pop(trade_id, None)
        if self._chart is None:
            return
        if drawing_id:
            self._chart.remove_drawing(drawing_id)
        if slices:
            from ..visual.indicator_renderer import draw_trade_group

            markers = _build_trade_markers(slices)
            draw_trade_group(self._chart, markers, opacity=0.15, config=self.config)

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------

    def _record_slices(self, closed_now: list[ClosedTrade]) -> None:
        for ct in closed_now:
            self._slices.setdefault(ct.trade_id, []).append(ct)

    @staticmethod
    def _normalize_candle(candle: Mapping) -> dict:
        """Standard 6-key candle dict (drops feed extras such as ``closed``)."""
        return {
            "timestamp": int(candle["timestamp"]),
            "open": float(candle["open"]),
            "high": float(candle["high"]),
            "low": float(candle["low"]),
            "close": float(candle["close"]),
            "volume": float(candle.get("volume", 0.0)),
        }

    def _emit(self, event: dict) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event)
        except Exception as exc:
            warnings.warn(f"LiveSimulation: on_event callback failed ({exc!r})")

    def _emit_report(self, report: SimulateReport) -> None:
        if self._on_report is None:
            return
        try:
            self._on_report(report)
        except Exception as exc:
            warnings.warn(f"LiveSimulation: on_report callback failed ({exc!r})")

    def __repr__(self) -> str:
        return (
            f"<LiveSimulation symbol={self.config.symbol!r} tf={self._tf!r} "
            f"seeded={self._seeded} window={self._candle_count_limit}>"
        )
