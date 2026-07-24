"""
AlgoTradeKit.trader._display
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The Trader's live display bridge (v1.0.0) — "pass the market data to a
Simulate object and show it on a chart, beside the trading."  Off by default
(``TraderConfig.display=False``); internal machinery, nothing here is exported
from ``trader/__init__``.

``display_trades`` modes
------------------------------
* ``"sim"`` — theoretical strategy performance: one
  :class:`~AlgoTradeKit.simulate.LiveSimulation` per displaying pair,
  built on the auto-derived ``SimulateConfig`` (— real venue costs, real
  start balance) and a **deep copy** of the pair's strategy (the worker and
  the sim each advance their own instance — sharing one would double-advance
  its state).  The sim owns its chart + report servers; this bridge only
  drives it via ``process_closed_candle`` / ``process_forming_candle`` —
  ``sim.start()`` is never called (the trader's feed is the source).
* ``"real"`` — actual broker fills: position boxes/markers drawn from the
  real-``ClosedTrade`` pipeline (``worker.closed_trades`` +
  ``engine.open_trades`` — real entry/exit prices, matched by
  ``client_order_id``/comment) on a chart built here, and report stats
  computed from those real trades (`simulate.build_report`) over an equity
  curve of periodic ``get_account_info()`` snapshots (one per closed candle).
* ``"both"`` — real fills overlaid on the sim trades: the sim's chart carries
  both (real overlays at a higher opacity), and one report page carries both
  sections — a two-entry combined payload
  ``{"SYMBOL (sim)": …, "SYMBOL (real)": …}`` (the sim's own report server is
  suppressed via ``report_mode="none"``; the combined page has
  ``has_chart=False`` per the combined contract).

Zero impact on trading speed
----------------------------------
All display work runs on one low-priority daemon thread per pair, draining a
dedicated display queue.  The trading side only **enqueues**: the worker
hands over each committed candle after processing it, forming rows ride the
worker's existing forming/tick handlers, and real-trade drawing updates come
from an event-stream subscriber (enqueue-only).  ``candle_close`` pairs with
no forming feed of their own get a display-only
``stream_candles(closed_only=False)`` subscription feeding **only** this
queue (hybrid-display note — trading cadence and display cadence stay
independent; commits still come only from the scheduler).

The display thread drains an item **only when the pair has no pending
trading work** (the trading queue is polled before every display item —
trading always preempts).  When the display queue backs up, updates are
coalesced: closed candles are never dropped (the sim must step every candle
in order), but their per-candle report/combined pushes are suppressed —
only the newest candle of a burst pushes; consecutive forming rows skip to
the latest; equity snapshots taken during a backlog carry the previous
venue values forward (historical equity cannot be re-fetched anyway).

Real equity snapshots
---------------------
One ``{"timestamp", "wallet", "equity"}`` row per closed candle from
``broker.get_account_info()``; venues without it (plain spot — the
``AccountInfo`` scalars are 0.0 by design) fall back to
``engine.read_balance()`` for both fields.  A failed venue read carries the
previous snapshot forward and reports one ``ERROR`` event (the display must
never die on a venue hiccup).

Rolling window
-------------------
``candle_count_limit=M``: the charts trim themselves via the
``Chart(candle_count_limit=M)`` machinery; the sim report is windowed by
LiveSimulation; the real report mirrors the same rules here — balance
snapshots older than the window are dropped (baseline := the equity entering
the window) and a real trade is dropped once ``close_time`` left the window.
The display keeps its **own** trade-list copy — ``worker.closed_trades``
(the ``run()`` return value) is never trimmed.

Browser vs URL
--------------------
``display_open_browser=True`` → local browser tabs.  ``False`` (VPS) → the
chart/report URLs are printed for the user's own browser; combined with a
non-local ``chart_host`` (``"0.0.0.0"``) they are printed with a
security note — an SSH tunnel to a localhost bind stays the recommended
alternative.
"""

from __future__ import annotations

import queue
import threading
import time
import warnings
from collections import deque
from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pandas as pd

from ..broker import MARKET_FOREX, BrokerError
from ..broker._timeutil import now_ms, parse_to_ms
from ..simulate import REPORT_MODE_NONE, TP_MODE_MULTI_RR, ClosedTrade, SimulateReport
from ..simulate._live import LiveSimulation
from ..simulate._report import _build_trade_markers, build_report
from ._config import (
    DISPLAY_TRADES_BOTH,
    DISPLAY_TRADES_REAL,
    DISPLAY_TRADES_SIM,
    EXEC_CANDLE_CLOSE,
)
from ._events import (
    EVENT_CLOSE,
    EVENT_OPEN,
    EVENT_RISK_FREE,
    EVENT_SL_MOVE,
    EVENT_TP_LEVEL,
)

if TYPE_CHECKING:  # pragma: no cover
    from ..strategy import BaseStrategy
    from ._trader import _PairWorker

_LOG_PREFIX = "[AlgoTradeKit] trader"

#: Display-queue wait per poll (also the close-latency bound), seconds.
_QUEUE_POLL_SECONDS = 0.2
#: Re-check cadence while waiting for the trading queue to empty, seconds.
_IDLE_POLL_SECONDS = 0.02
#: Chart/report server settle time after start (LiveSimulation precedent).
_SERVER_SETTLE_SECONDS = 0.3
#: Final-posbox opacity: standard on a real-only chart, stronger for the real
#: overlay on the "both" chart so it stands apart from the sim boxes (0.15).
_OPACITY_STANDARD = 0.15
_OPACITY_OVERLAY = 0.30
#: Hosts whose printed URLs need no security note (loopback binds).
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
#: Event types that drive the real-trades chart/report view.
_TRADE_EVENT_TYPES = frozenset(
    {EVENT_OPEN, EVENT_SL_MOVE, EVENT_RISK_FREE, EVENT_TP_LEVEL, EVENT_CLOSE}
)
#: Standard candle keys streamed to the chart.
_CANDLE_KEYS = ("timestamp", "open", "high", "low", "close", "volume")


class _DisplayBridge:
    """
    One pair's live display: the low-priority queue + thread, the
    per-mode chart/report wiring and the real-fills view.

    Built by ``Trader.__init__`` for every ``display=True`` pair and stored
    as ``worker.display``; :meth:`start` runs inside ``worker.start()``
    after the engine is armed and the reconcile finished (so the real
    view seeds from a settled state) and **before** any producer thread —
    the seed work happens once at startup, steady-state trading only ever
    enqueues.  ``combined`` / ``combined_label`` are set by the Trader when
    a combined page is served across pairs.
    """

    def __init__(self, worker: _PairWorker, *, sim_strategy: BaseStrategy | None = None) -> None:
        self.worker = worker
        self.config = worker.config
        self.engine = worker.engine
        self.broker = worker.broker
        self.tf = worker.tf
        self.mode: str = worker.config.display_trades
        if self.mode in (DISPLAY_TRADES_SIM, DISPLAY_TRADES_BOTH) and sim_strategy is None:
            raise ValueError(
                f"_DisplayBridge: display_trades={self.mode!r} needs a sim_strategy "
                "(a dedicated strategy instance for the display LiveSimulation)."
            )
        self._sim_strategy = sim_strategy

        self._queue: queue.Queue = queue.Queue()
        self._pending: deque = deque()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._feed = None                    # display-only forming stream (owned here)
        self._started = False
        self._unsubscribe = None

        self.sim: LiveSimulation | None = None
        self.combined = None                 # _CombinedLiveReport (set by Trader)
        self.combined_label = ""

        # Real-trades view (modes "real"/"both")
        self._real_chart = None              # own chart in "real"; the sim's in "both"
        self._real_server = None             # plain page ("real") / combined page ("both")
        self._real_trades: list[ClosedTrade] = []          # display-owned copy
        self._real_slices: dict[int, list[ClosedTrade]] = {}
        self._real_drawings: dict[int, str] = {}           # trade_id -> drawing id
        self._real_balance: list[dict] = []                # {"timestamp","wallet","equity"}
        self._real_baseline = 0.0            # report baseline (equity entering the window)
        self._real_last_ts = 0               # newest candle on the real chart (forming guard)
        limit = worker.config.candle_count_limit
        self._real_window: deque | None = deque(maxlen=limit) if limit else None
        self._real_engaged = False
        self._equity_error_reported = False
        self._display_error_reported = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Seed the display (sim seed / real chart+report), print URLs when
        the browser stays closed, subscribe the event + feed sources and
        start the display thread.  Runs on the starting thread — one-time
        startup cost; after this, trading only enqueues."""
        if self._started:
            raise RuntimeError("_DisplayBridge.start() may only run once.")
        self._started = True
        if self.mode in (DISPLAY_TRADES_SIM, DISPLAY_TRADES_BOTH):
            self._start_sim()
        if self.mode in (DISPLAY_TRADES_REAL, DISPLAY_TRADES_BOTH):
            self._start_real()
        if not self.config.display_open_browser:
            self._print_urls()
        if self.mode != DISPLAY_TRADES_SIM:
            # Real-trade drawing/report updates ride the event stream;
            # the subscriber only enqueues (the display thread does the work).
            self._unsubscribe = self.worker.events.subscribe(self._on_trade_event)
        self._maybe_subscribe_feed()
        self._thread = threading.Thread(
            target=self._run,
            name=f"atk-trader-display-{self.config.symbol.lower()}",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        """Stop the display: end the display-only feed and the thread, drain
        whatever is still queued (so shutdown closes reach the page), then
        push the final report state once.  Servers stay up — the
        keep-alive holds them until one more Ctrl+C."""
        self._stop.set()
        if self._feed is not None:
            try:
                self._feed.stop()
            except Exception:  # noqa: BLE001
                pass
        if self._unsubscribe is not None:
            try:
                self._unsubscribe()
            except Exception:  # noqa: BLE001
                pass
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        # Leftover items (e.g. the on_stop close_all CLOSE events) — process
        # them inline so the final page state is complete.
        items = list(self._pending)
        self._pending.clear()
        while True:
            try:
                items.append(self._queue.get_nowait())
            except queue.Empty:
                break
        for kind, payload in items:
            try:
                self._process(kind, payload, backlog=True)
            except Exception:  # noqa: BLE001 — best-effort shutdown drain
                pass
        try:
            if self.mode != DISPLAY_TRADES_SIM:
                self._push_real_report()
            self._push_combined()
        except Exception:  # noqa: BLE001 — best-effort final push
            pass

    # ------------------------------------------------------------------
    # Producers (called from trading threads — enqueue only, never block)
    # ------------------------------------------------------------------

    def enqueue_candle(self, candle: Mapping) -> None:
        """One committed closed candle (worker hands it over post-processing)."""
        self._queue.put(("candle", dict(candle)))

    def enqueue_forming(self, candle: Mapping) -> None:
        """One forming row (worker's forming/tick handlers or the display feed)."""
        self._queue.put(("forming", dict(candle)))

    def _on_trade_event(self, event) -> None:
        if getattr(event, "event_type", None) in _TRADE_EVENT_TYPES:
            self._queue.put(("trade", event))

    def _on_display_candle(self, candle: dict) -> None:
        """Display-only feed callback: forming rows only — closed candles are
        committed by the scheduler and handed over by the worker."""
        if candle.get("closed"):
            return
        self.enqueue_forming(candle)

    def _maybe_subscribe_feed(self) -> None:
        """ hybrid-display note: a ``candle_close`` pair has no forming
        feed of its own (unless it is an MT5 multi-RR pair — the ladder
        detection feed already flows through the worker) — subscribe one
        here so forming candles still render live.  Its candles go ONLY to
        the display queue, never to the trading queue."""
        if self.config.execution != EXEC_CANDLE_CLOSE:
            return  # the worker's own forming/tick feed already flows through
        if self.config.tp_mode == TP_MODE_MULTI_RR and self.worker.market == MARKET_FOREX:
            return  # ladder-detection feed — the worker forwards its rows
        try:
            self._feed = self.broker.stream_candles(
                self.config.symbol, self.tf, self._on_display_candle, closed_only=False
            )
        except Exception as exc:  # noqa: BLE001 — display never blocks trading
            warnings.warn(
                f"display: forming-candle feed unavailable ({exc!r}) — the chart "
                "updates on closed candles only."
            )

    # ------------------------------------------------------------------
    # Display thread
    # ------------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            item = self._next_item()
            if item is None:
                continue
            self._wait_trading_idle()
            if self._stop.is_set():
                # Put the unprocessed item back for the close() drain.
                self._pending.appendleft(item)
                return
            kind, payload = item
            try:
                self._process(kind, payload, backlog=self._backlog())
            except Exception as exc:  # noqa: BLE001 — display must survive
                if not self._display_error_reported:
                    self._display_error_reported = True
                    warnings.warn(f"display: {kind} update failed ({exc!r})")
                continue
            self._display_error_reported = False

    def _next_item(self) -> tuple[str, Any] | None:
        if self._pending:
            item = self._pending.popleft()
        else:
            try:
                item = self._queue.get(timeout=_QUEUE_POLL_SECONDS)
            except queue.Empty:
                return None
        if item[0] == "forming":
            # Skip-to-latest: only consecutive forming rows collapse, so the
            # relative order against candles/trade events is preserved.
            while True:
                try:
                    nxt = self._queue.get_nowait()
                except queue.Empty:
                    break
                if nxt[0] == "forming":
                    item = nxt
                else:
                    self._pending.append(nxt)
                    break
        return item

    def _wait_trading_idle(self) -> None:
        """Trading always preempts: hold the display item until the pair's
        trading queue is drained."""
        worker = self.worker
        while not self._stop.is_set():
            if worker.queue.empty() and not worker._pending:
                return
            time.sleep(_IDLE_POLL_SECONDS)

    def _backlog(self) -> bool:
        return bool(self._pending) or not self._queue.empty()

    def _process(self, kind: str, payload: Any, *, backlog: bool) -> None:
        if kind == "candle":
            self._process_candle(payload, backlog=backlog)
        elif kind == "forming":
            self._process_forming(payload)
        elif kind == "trade":
            self._process_trade_event(payload, backlog=backlog)

    # ------------------------------------------------------------------
    # Closed candles
    # ------------------------------------------------------------------

    def _process_candle(self, candle: dict, *, backlog: bool) -> None:
        if self.sim is not None:
            # quiet=True suppresses the sim's per-candle report push while a
            # backlog exists (coalescing) — state still advances candle by
            # candle, the chart bar still streams.
            self.sim.process_closed_candle(candle, quiet=backlog)
        if self.mode != DISPLAY_TRADES_SIM:
            ts = int(candle["timestamp"])
            if self.mode == DISPLAY_TRADES_REAL and self._real_chart is not None:
                self._real_chart.stream_from_atk(self._plain_candle(candle))
            self._real_last_ts = max(self._real_last_ts, ts)
            self._snapshot_equity(ts, venue_read=not backlog)
            self._advance_real_window(ts)
            if not backlog:
                self._push_real_report()
        if not backlog:
            self._push_combined()

    def _snapshot_equity(self, ts: int, *, venue_read: bool) -> None:
        """Append one equity-curve row (: per closed candle).  During a
        backlog the previous venue values carry forward — a venue snapshot
        taken late cannot represent past candles anyway."""
        wallet = equity = None
        if venue_read:
            try:
                info = getattr(self.broker, "get_account_info", None)
                if info is not None:
                    acct = info()
                    wallet = float(acct.wallet_balance)
                    equity = float(acct.equity)
                if not wallet and not equity:
                    # Plain spot reports 0.0 scalars by design — fall
                    # back to the engine's sizing-currency balance read.
                    balance = float(self.engine.read_balance())
                    wallet = equity = balance
                self._equity_error_reported = False
            except BrokerError as exc:
                wallet = equity = None
                if not self._equity_error_reported:
                    self._equity_error_reported = True
                    self.worker._error(
                        "display",
                        f"equity snapshot failed ({exc}) — carrying the previous "
                        "value forward.",
                        will_retry=True,
                    )
        if wallet is None or equity is None:
            prev = self._real_balance[-1] if self._real_balance else None
            wallet = float(prev["wallet"]) if prev else self._real_baseline
            equity = float(prev["equity"]) if prev else self._real_baseline
        self._real_balance.append({"timestamp": ts, "wallet": wallet, "equity": equity})

    def _advance_real_window(self, ts: int) -> None:
        """ window for the real report — the exact rules: baseline :=
        equity entering the window; a trade drops once ``close_time`` left
        the window.  Only the display copy is trimmed."""
        win = self._real_window
        if win is None:
            return
        if len(win) == win.maxlen:
            self._real_engaged = True
        win.append(ts)
        if not self._real_engaged:
            return
        window_start = int(win[0])
        history = self._real_balance
        while history and int(history[0]["timestamp"]) < window_start:
            self._real_baseline = float(history.pop(0)["equity"])
        self._real_trades = [t for t in self._real_trades if t.close_time >= window_start]

    # ------------------------------------------------------------------
    # Forming rows
    # ------------------------------------------------------------------

    def _process_forming(self, candle: dict) -> None:
        if self.sim is not None:
            self.sim.process_forming_candle(candle)   # stale-guarded internally
        if self.mode == DISPLAY_TRADES_REAL and self._real_chart is not None:
            if int(candle["timestamp"]) <= self._real_last_ts:
                return  # that bar is already final on the real chart
            self._real_chart.stream_from_atk(self._plain_candle(candle))

    # ------------------------------------------------------------------
    # Real-trade events (modes "real"/"both")
    # ------------------------------------------------------------------

    def _process_trade_event(self, event, *, backlog: bool) -> None:
        etype = event.event_type
        tid = int(event.trade_id) if event.trade_id is not None else None
        if tid is None:
            return
        if etype == EVENT_OPEN:
            trade = self._find_live_trade(tid)
            if trade is not None:
                self._add_real_drawing(trade.pos)
            return
        if etype in (EVENT_SL_MOVE, EVENT_RISK_FREE, EVENT_TP_LEVEL):
            if etype == EVENT_TP_LEVEL and event.trade is not None:
                self._real_trades.append(event.trade)
                self._real_slices.setdefault(tid, []).append(event.trade)
            trade = self._find_live_trade(tid)
            drawing_id = self._real_drawings.get(tid)
            if trade is not None and drawing_id and self._real_chart is not None:
                self._real_chart.update_live_position(
                    drawing_id,
                    stop_loss=float(trade.pos.stop_loss),
                    next_tp=trade.pos.next_tp,
                )
            if etype == EVENT_TP_LEVEL and not backlog:
                self._push_real_report()
                self._push_combined()
            return
        if etype == EVENT_CLOSE:
            if event.trade is not None:
                self._real_trades.append(event.trade)
                self._real_slices.setdefault(tid, []).append(event.trade)
            self._finalize_real_drawing(tid)
            if not backlog:
                self._push_real_report()
                self._push_combined()

    def _find_live_trade(self, trade_id: int):
        for trade in self.engine.open_trades:
            if trade.pos.trade_id == trade_id:
                return trade
        return None

    def _add_real_drawing(self, pos) -> None:
        """Draw an open real position as a ``LivePosition``."""
        chart = self._real_chart
        if chart is None:
            return
        next_tp = pos.sl_history[-1]["next_tp"] if pos.sl_history else pos.next_tp
        drawing_id = chart.add_live_position(
            open_time=pos.open_time // 1000,
            entry_price=pos.entry_price,
            stop_loss=pos.stop_loss,
            direction=pos.direction,
            next_tp=next_tp,
            trade_id=pos.trade_id,
        )
        self._real_drawings[pos.trade_id] = drawing_id

    def _finalize_real_drawing(self, trade_id: int) -> None:
        """A real trade fully closed: swap its live drawing for the final
        posbox + dynamic SL/TP segments built from all its slices (the
        exact rendering; overlay opacity on the shared "both" chart)."""
        slices = self._real_slices.pop(trade_id, [])
        drawing_id = self._real_drawings.pop(trade_id, None)
        chart = self._real_chart
        if chart is None:
            return
        if drawing_id:
            chart.remove_drawing(drawing_id)
        if slices:
            from ..visual.indicator_renderer import draw_trade_group

            opacity = (
                _OPACITY_OVERLAY if self.mode == DISPLAY_TRADES_BOTH else _OPACITY_STANDARD
            )
            markers = _build_trade_markers(slices)
            draw_trade_group(chart, markers, opacity=opacity, config=self.engine.sim_config)

    # ------------------------------------------------------------------
    # Reports
    # ------------------------------------------------------------------

    def _build_real_report(self) -> SimulateReport:
        """Real-trades ``SimulateReport``: real ``ClosedTrade``
        records + live open positions + the venue equity curve, through the
        standard report maths (windowed baseline when engaged)."""
        cfg = replace(self.engine.sim_config, initial_balance=self._real_baseline)
        return build_report(
            list(self._real_trades),
            [trade.pos for trade in self.engine.open_trades],
            list(self._real_balance),
            cfg,
        )

    @property
    def current_report(self) -> SimulateReport | None:
        """This pair's combined-page contribution: the sim report for
        ``"sim"`` pairs, the real report for ``"real"``/``"both"`` (a real
        session's portfolio truth)."""
        if self.mode == DISPLAY_TRADES_SIM:
            return self.sim.last_report if self.sim is not None else None
        return self._build_real_report()

    def _push_real_report(self) -> None:
        if self._real_server is None:
            return
        if self.mode == DISPLAY_TRADES_REAL:
            # push_update carries the chart-link keys of the previous
            # payload forward — the "Open on Candle Chart" button survives.
            self._real_server.push_update(self._build_real_report())
        else:
            self._real_server.push_update(self._both_payload())

    def _both_payload(self) -> dict:
        from ..report import build_combined_report_payload

        symbol = self.config.symbol
        return build_combined_report_payload({
            f"{symbol} (sim)": self.sim.last_report,
            f"{symbol} (real)": self._build_real_report(),
        })

    def _push_combined(self) -> None:
        if self.combined is not None and self.combined_label:
            report = self.current_report
            if report is not None:
                self.combined.update(self.combined_label, report)

    # ------------------------------------------------------------------
    # Startup — sim side
    # ------------------------------------------------------------------

    def _start_sim(self) -> None:
        config = self.config
        sim_config = self.engine.sim_config   # auto-derive (engine started first)
        if self.mode == DISPLAY_TRADES_BOTH:
            # One two-section page instead of the sim's own report server.
            sim_config = replace(sim_config, report_mode=REPORT_MODE_NONE)
        sim = LiveSimulation(
            self.broker,
            self._sim_strategy,
            sim_config,
            display_candles=config.display_candles,
            display_start=config.display_start,
            min_seed_candles=config.min_candles,
            candle_count_limit=config.candle_count_limit,
            recompute_window=config.recompute_window,
            chart_host=config.chart_host,
            chart_port=config.chart_port,
            report_port=config.report_port,
            open_browser=config.display_open_browser,
            # No on_event/on_report: the [LIVE] terminal stream is the
            # trader's voice — a parallel [SIM] log would be noise.
        )
        sim.seed()
        self.sim = sim

    # ------------------------------------------------------------------
    # Startup — real side
    # ------------------------------------------------------------------

    def _start_real(self) -> None:
        self._real_baseline = float(self.engine.sim_config.initial_balance)

        if self.mode == DISPLAY_TRADES_REAL:
            df = self._fetch_seed_df()
            seed_ts = [int(v) for v in df["timestamp"]]
            self._real_chart = self._build_real_chart(df)
        else:
            # "both": the real overlay rides the sim's chart.
            self._real_chart = self.sim.chart
            frame = self.sim.data.get(self.tf)
            seed_ts = [int(v) for v in frame["timestamp"]] if frame is not None else []

        if seed_ts:
            self._real_last_ts = seed_ts[-1]

        # Seed the real view from the settled post-reconcile state:
        # closes already recorded + positions currently under management.
        # Seed BEFORE the window prefill so out-of-window records trim away.
        self._real_trades = list(self.worker.closed_trades)
        if self._real_window is not None:
            for ts in seed_ts:
                self._advance_real_window(ts)
        slices: dict[int, list[ClosedTrade]] = {}
        for trade in self._real_trades:
            slices.setdefault(trade.trade_id, []).append(trade)
        open_ids = {t.pos.trade_id for t in self.engine.open_trades}
        if self._real_chart is not None:
            opacity = (
                _OPACITY_OVERLAY if self.mode == DISPLAY_TRADES_BOTH else _OPACITY_STANDARD
            )
            from ..visual.indicator_renderer import draw_trade_group

            for tid, group in slices.items():
                if tid in open_ids:
                    continue
                markers = _build_trade_markers(group)
                draw_trade_group(
                    self._real_chart, markers, opacity=opacity,
                    config=self.engine.sim_config,
                )
        self._real_slices = {t: s for t, s in slices.items() if t in open_ids}
        for trade in self.engine.open_trades:
            self._add_real_drawing(trade.pos)

        # First equity snapshot (venue read) anchors the curve at the seed end.
        self._snapshot_equity(self._real_last_ts or now_ms(), venue_read=True)
        self._start_real_report_server()

    def _fetch_seed_df(self) -> pd.DataFrame:
        """Seed candles for the real-mode chart (``display_candles`` XOR
        ``display_start``, ≥ ``min_candles`` — the seed rules)."""
        config = self.config
        symbol, tf = config.symbol, self.tf
        if config.display_candles is not None:
            rows = self.broker.fetch_last_candles(symbol, tf, config.display_candles)
        else:
            rows = self.broker.fetch_candles(
                symbol, tf, parse_to_ms(config.display_start), now_ms()
            )
        if len(rows) < config.min_candles:
            raise ValueError(
                f"display: the seed returned {len(rows)} candles for {symbol!r} "
                f"{tf!r} — fewer than min_candles={config.min_candles} (venue "
                "history too short or display_start too recent)."
            )
        df = pd.DataFrame(
            [{key: row[key] for key in _CANDLE_KEYS} for row in rows]
        )
        df["timestamp"] = df["timestamp"].astype("int64")
        return df

    def _build_real_chart(self, df: pd.DataFrame):
        import time as _time

        from ..simulate._engine import _register_keep_alive
        from ..visual import Chart

        config = self.config
        chart = Chart(
            title=f"{config.symbol} — Live Trading ({self.tf}, real)",
            host=config.chart_host,
            port=config.chart_port,
            candle_count_limit=config.candle_count_limit,
        )
        chart.set_data(df)
        chart.show(open_browser=config.display_open_browser, block=False)
        _time.sleep(_SERVER_SETTLE_SECONDS)
        _register_keep_alive()   # same lifetime rule as the sim display
        return chart

    def _start_real_report_server(self) -> None:
        import time as _time

        from ..report._builder import build_report_payload
        from ..report._server import ReportServer

        config = self.config
        symbol = config.symbol

        if self.mode == DISPLAY_TRADES_REAL:
            payload = build_report_payload(self._build_real_report())
            chart = self._real_chart
            chart_server = getattr(chart, "_server", None) if chart is not None else None
            if chart is not None and chart_server is not None:
                payload["has_chart"] = True
                payload["chart_port"] = getattr(chart_server, "port", None)

            def _on_open_chart(trade_id: int) -> None:
                target = self._real_chart
                if target is None:
                    return
                markers = _build_trade_markers(
                    [t for t in self._real_trades if t.trade_id == trade_id]
                )
                if markers:
                    target.navigate_to_candle(markers[0]["open_time"])

            server = ReportServer(
                title=f"AlgoTradeKit Live Report — {symbol} (real)",
                host=config.chart_host,
                port=config.report_port,
                on_open_chart=_on_open_chart if chart is not None else None,
            )
            server.start(open_browser=config.display_open_browser)
            _time.sleep(_SERVER_SETTLE_SECONDS)
            server.set_report_data(payload)
        else:
            server = ReportServer(
                title=f"AlgoTradeKit Live Report — {symbol} (sim + real)",
                host=config.chart_host,
                port=config.report_port,
            )
            server.start(open_browser=config.display_open_browser)
            _time.sleep(_SERVER_SETTLE_SECONDS)
            server.set_report_data(self._both_payload())
        self._real_server = server

    # ------------------------------------------------------------------
    # URL printing (browser-vs-URL flow)
    # ------------------------------------------------------------------

    def _print_urls(self) -> None:
        symbol = self.config.symbol
        chart = self.sim.chart if self.sim is not None else self._real_chart
        if chart is None:
            chart = self._real_chart
        if chart is not None and getattr(chart, "url", None):
            print(f"{_LOG_PREFIX} {symbol} chart  → {chart.url}")
        server = (
            self.sim.report_server
            if self.mode == DISPLAY_TRADES_SIM and self.sim is not None
            else self._real_server
        )
        if server is not None and getattr(server, "url", None):
            print(f"{_LOG_PREFIX} {symbol} report → {server.url}")
        if self.config.chart_host not in _LOCAL_HOSTS:
            print(
                f"{_LOG_PREFIX} {symbol}: SECURITY — these servers are bound to "
                f"{self.config.chart_host!r} and reachable from other machines; "
                "anyone who can open the URLs sees your trading.  An SSH tunnel "
                "to a 127.0.0.1 bind is the recommended alternative."
            )

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _plain_candle(candle: Mapping) -> dict:
        return {
            "timestamp": int(candle["timestamp"]),
            "open": float(candle["open"]),
            "high": float(candle["high"]),
            "low": float(candle["low"]),
            "close": float(candle["close"]),
            "volume": float(candle.get("volume", 0.0)),
        }

    def __repr__(self) -> str:
        return (
            f"<_DisplayBridge symbol={self.config.symbol!r} mode={self.mode!r} "
            f"started={self._started}>"
        )
