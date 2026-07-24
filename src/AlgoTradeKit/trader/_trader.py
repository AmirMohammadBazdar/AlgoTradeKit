"""
AlgoTradeKit.trader._trader
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
``Trader`` — the real-order live loop (v1.0.0): the per-pair worker tying
**feed → strategy → execution** in the pair's configured execution mode.

Execution modes
--------------------
* ``candle_close`` (periodic, default) — the :class:`CandleCloseScheduler`
  fires at the exact venue-clock boundary → tail fetch (gap-filled after
  downtime) → append → incremental update → ``generate_signals`` /
  ``detect_exit_signals`` → execute.
* ``candle_update`` (realtime) — additionally subscribes
  ``stream_candles(closed_only=False)``; the strategy is re-evaluated on every
  forming-candle update via ``evaluate_forming_candle`` (throwaway copy —
  committed state never advances on forming data) and entries fire mid-candle.
* ``tick`` — additionally subscribes ``stream_ticker``; each tick is
  synthesized into the forming row (first tick of an interval opens it, later
  ticks extend high/low/close; volume stays 0) and evaluated the same way.

**Closed-candle bookkeeping happens exactly once per close in every mode and
comes ONLY from the scheduler** — never from a stream's ``closed`` flag.  The
scheduler's boundary arithmetic is the guard: a venue that prints no new
bar until its first trade, or a quiet market that pushes no stream update,
cannot delay (or double-fire) the commit.  Trading cadence and display cadence
stay independent by construction (hybrid-display note): a ``candle_close``
pair with ``display=True`` still renders forming candles live through the
display bridge's own feed.

Live display
-----------------------
``config.display=True`` (off by default) serves a live chart + live report
beside the trading, per pair: ``display_trades="sim"`` shows the theoretical
strategy performance from an auto-created :class:`LiveSimulation` on
the auto-derived ``SimulateConfig``; ``"real"`` shows the actual broker fills
(real ``ClosedTrade`` records) with report stats over an equity curve
of per-candle ``get_account_info()`` snapshots; ``"both"`` overlays the real
fills on the sim chart and serves a two-section report page.  All display
work runs on a low-priority background thread per pair — the trading loop
only enqueues and never waits; display items are processed only while the
pair has no pending trading work, and backlogs coalesce (skip to latest).
``display_open_browser=False`` prints the URLs instead (VPS flow).
Mechanics live in ``trader/_display.py``.

Signal dedup (realtime modes)
-----------------------------
A strategy re-evaluated on every update/tick may re-emit the same signal.  The
trader acts **once per (pair, candle, direction)** — keyed by the evaluated
candle's open time, spanning the forming evaluations *and* the closed-candle
commit of the same candle.  Duplicates emit no order (the ``SIGNAL`` event
still prints once per evaluation burst — only the first occurrence).  Exit
signals are acted on once per candle the same way.

Venue-side close detection
--------------------------
Every close — SL hit, TP fill, partial, force, manual — is detected from the
venue and recorded as a ``ClosedTrade`` with **real fill prices**, emitting
``CLOSE`` events:

* **Binance (futures + spot)** — the private user-data stream
  (``broker.stream_user_data``): ``ORDER_TRADE_UPDATE`` / ``executionReport``
  fills matched to the tracked trades' protective order ids; the event's own
  average/last fill price is the exit price.  A filled SL maps to ``sl`` (or
  ``rf`` when the shared maths had moved it to break-even —
  ``sl_reason``, sim parity); a filled TP to ``tp``.
* **Binance futures, manual closes** — a periodic ``open_positions``
  reconcile: venue net exposure below the tracked sum means something was
  closed outside our orders → tracked trades are closed FIFO (partial-aware)
  with reason ``manual`` and the leftover protective orders are cancelled.
* **MetaTrader** — a periodic ``open_positions`` poll; a tracked ticket that
  disappeared (or shrank — manual partial close) is resolved through
  ``broker.history_deals(position=ticket)``: the exit deal's real price and
  MT5 ``DEAL_REASON_*`` (4 = SL → ``sl``/``rf``, 5 = TP → ``tp``, anything
  else → ``manual``).
* **Binance spot, manual flows** (user cancels the OCO and sells by hand) are
  only detectable as "protection vanished" — reconciled with the current
  ticker price and reason ``manual`` when the user-data stream is down;
  offline flows are reconciled at startup from the venue trade history.

Multi-RR live ladders
-------------------------------
``tp_mode="multi_rr"`` trades venue-natively: Binance futures positions get
one reduce-only limit order per ladder level (fills arrive on the user-data
stream and route to ``ExecutionEngine.record_ladder_fill``); MetaTrader
levels are **feed-detected** — ``ExecutionEngine.check_tp_levels`` runs on
every closed candle *and* on every forming update / synthesized tick row, so
the partial close fires the moment a level price is reached.  MT5 pairs in
``candle_close`` mode therefore subscribe a forming-candle stream used
**only** for ladder detection (strategy evaluation still happens exclusively
on scheduler candles).  Partial ladder slices (``TP_LEVEL`` events) land in
``closed_trades`` alongside full closes — they are real ``ClosedTrade``
records sharing the trade id.

Safety rails
------------------
Three kill switches end a session gracefully: :meth:`Trader.stop`, a signal
(Ctrl+C / SIGTERM — handlers installed while ``run()`` blocks on the main
thread, one-shot so a second signal interrupts a stuck shutdown the default
way), or touching ``kill_switch_file`` (polled every
``_KILL_SWITCH_POLL_SECONDS``; a stale file at start raises).  Shutdown stops
the feeds/scheduler first, then applies ``on_stop``: ``"keep"`` (default —
positions stay, their venue SL/TP keep protecting, nothing is cancelled)
or ``"close_all"`` (market-close everything and cancel the working orders).
``max_daily_loss`` is the per-pair daily-loss gate
(:class:`_DailyLossGuard`): realized + unrealized loss for the UTC day at or
over the threshold → ``DAILY_LOSS`` event + loud log, no new entries until
the next UTC day (SL management, ladder checks and exit closes continue);
``close_on_daily_loss=True`` also flattens at the trip.

Persistence & restart
---------------------------
Every state change is journaled to ``state_path`` (default
``./.atk_trader_state.json``; atomic writes, flushed after every processed
queue item and on shutdown).  ``run()`` reconciles the journal against the
venue before trading starts: journaled positions still on the venue are
**adopted** (trailing state, RR ladder and risk-free status resume; venue
protection lost while offline is re-armed), journaled positions that closed
while offline are recorded as ``ClosedTrade``\\ s from the venue's trade
history, and foreign (not-journaled) venue positions are warned about and
left untouched.  One ``RECONCILE`` event reports the outcome; the restored
dedup keys guarantee a journaled signal is never re-sent.  Mechanics live in
``trader/_state.py``.

Multi-pair orchestration
----------------------------------
``Trader(pairs=[TraderPair, ...])`` runs one worker per ``(broker, config,
strategy)`` entry; brokers may repeat across entries — pairs on the same
broker share that account's wallet naturally, because sizing reads the
account balance.  The same ``(broker, symbol)`` may not appear twice
(rejected at config time).  All pairs share one ``trader_id`` (explicit
> journaled > random) and one journal file — each pair under its own
key (``market:SYMBOL``, ``#2``-suffixed when the same market+symbol repeats
across brokers; keep the relative order of such duplicate pairs stable
across restarts, or their journals swap).  The safety rails are
Trader-level: ``stop()`` / Ctrl+C / SIGTERM / ``kill_switch_file`` stop
every pair, and ``on_stop`` applies to all of them.  Combined reporting
aggregates *reporting only* — cross-account risk management stays deferred
; with two or more displaying pairs the Trader serves the combined
report page across them, refreshed on every closed candle.

Division of labour: own order placement, venue SL management and the
ladder (:class:`ExecutionEngine`) — this module routes fills and feed touches
into them and adds the safety rails, the journal flush points
(the reconciliation itself lives in ``trader/_state.py``) and the
multi-pair form; ``trader/_display.py`` the live display bridge this
module hands its candles, forming rows and trade events to.

Threading: one consumer loop per pair drains that pair's queue — the calling
thread itself in the single-pair form, one ``atk-trader-pair-<symbol>``
thread per pair in the multi-pair form (the supervising caller thread then
only waits and handles signals).  Producers are the scheduler thread, the
optional forming/tick stream, the optional user-data stream and a reconcile
timer.  Strategy evaluation and every order therefore stay
**single-threaded per pair** (the engine contract).  A failing item
emits ``ERROR`` and the loop continues — one bad candle/event must never end
a live session while the venue SL protects the positions.
"""

from __future__ import annotations

import copy
import os
import queue
import secrets
import signal as signal_module
import threading
import time
from collections import deque
from typing import Any

import pandas as pd

from ..broker import (
    MARKET_FOREX,
    MARKET_FUTURES,
    MARKET_SPOT,
    POSITION_LONG,
    BrokerError,
    OrderError,
)
from ..broker._timeutil import now_ms
from ..simulate import CLOSE_REASON_TP, TP_MODE_MULTI_RR, ClosedTrade
from ..simulate._position_math import sl_reason
from ..strategy import BaseStrategy, Signal, StrategyMode
from ..strategy._incremental import advance_live_candle, evaluate_forming_candle
from ._config import (
    DISPLAY_TRADES_REAL,
    EXEC_CANDLE_CLOSE,
    EXEC_CANDLE_UPDATE,
    EXEC_TICK,
    ON_STOP_CLOSE_ALL,
    TraderConfig,
    TraderPair,
    TraderSettings,
    parse_max_daily_loss,
    validate_pairs,
)
from ._display import _DisplayBridge
from ._events import (
    EVENT_CLOSE,
    EVENT_TP_LEVEL,
    SOURCE_LIVE,
    DailyLossEvent,
    ErrorEvent,
    EventStream,
    ExitSignalEvent,
    SignalEvent,
    attach_terminal_printer,
)
from ._execution import ExecutionEngine, LiveTrade
from ._run_live import _signal_rr
from ._scheduler import CandleCloseScheduler, SchedulerTick, timeframe_ms
from ._state import (
    CLOSE_REASON_MANUAL,  # noqa: F401 — re-exported via trader/__init__.py
    DEFAULT_STATE_PATH,
    MT5_DEAL_ENTRY_IN,
    MT5_DEAL_REASON_SL,
    MT5_DEAL_REASON_TP,
    QTY_EPSILON,
    TraderStateJournal,
    build_pair_state,
    reconcile_startup,
)

_LOG_PREFIX = "[AlgoTradeKit] trader"

#: Consumer-loop wait per queue poll (also the stop-latency bound), seconds.
_QUEUE_POLL_SECONDS = 0.2
#: Kill-switch file poll cadence, seconds — the trigger-latency bound.
_KILL_SWITCH_POLL_SECONDS = 0.5
#: One UTC day in milliseconds — the daily-loss window.
_DAY_MS = 86_400_000
#: Dedup memory is pruned this many candles behind the newest committed one.
_DEDUP_KEEP_CANDLES = 50
#: Venue position reconciles run at most this often (seconds); the reconcile
#: timer uses max(candle_poll_interval, this).
_RECONCILE_MIN_INTERVAL = 1.0
# Shared with the state module (single definitions live there).
_QTY_EPSILON = QTY_EPSILON
_MT5_DEAL_REASON_SL = MT5_DEAL_REASON_SL
_MT5_DEAL_REASON_TP = MT5_DEAL_REASON_TP
_MT5_DEAL_ENTRY_IN = MT5_DEAL_ENTRY_IN

#: Queue item kinds collapsed to their newest occurrence when a backlog forms
#: (a stale forming row / tick / reconcile marker is worthless once a newer
#: one exists; candles and user-data fills are never dropped).
_COALESCE_KINDS = frozenset({"forming", "tick", "reconcile"})


class _DailyLossGuard:
    """
    Per-pair ``max_daily_loss`` gate.

    Loss for the UTC day = −(realized net PnL of the pair's trades closed
    this UTC day + current unrealised PnL of its open positions).  An
    overnight position counts its **full** open drawdown — conservative: the
    rail trips earlier when a losing position is carried across midnight.
    At/over the threshold: ``DAILY_LOSS`` event + loud unconditional log, no
    new entries until the next UTC day (``blocked``); SL management, ladder
    checks and exit-signal closes continue.  ``close_on_daily_loss=True``
    also flattens via ``engine.force_close()`` at the trip moment.

    Percent thresholds (``"2%"``) are measured against the UTC-day-start
    account balance: the engine's start balance on the first day, re-read
    from the venue at each day rollover (kept, with an ``ERROR`` event, when
    the read fails).  Time is candle time: :meth:`evaluate` is called with
    each evaluated row's timestamp and close (scheduler candles and
    forming/tick rows), so gating, the trip and the day rollover all follow
    the venue's candle timestamps — realized closes landing between marks
    are picked up by the next one.
    """

    def __init__(self, worker: _PairWorker) -> None:
        self._worker = worker
        limit = worker.config.max_daily_loss
        self.enabled = limit is not None
        self._mode, self._value = ("", 0.0)
        if self.enabled:
            self._mode, self._value = parse_max_daily_loss(limit)
        self._day: int | None = None
        self._day_base = 0.0        # percent base: account balance at UTC-day start
        self._tripped = False

    @property
    def blocked(self) -> bool:
        """True while entries are gated — the limit tripped this UTC day."""
        return self._tripped

    def threshold(self) -> float:
        """The configured limit in account dollars for the current day."""
        if self._mode == "percent":
            return self._day_base * self._value / 100.0
        return self._value

    def current_loss(self, day: int, mark_price: float) -> float:
        """Realized-today + open-unrealised loss, positive = losing."""
        realized = sum(
            trade.net_pnl
            for trade in self._worker.closed_trades
            if int(trade.close_time) // _DAY_MS == day
        )
        unrealised = sum(
            trade.pos.unrealised_pnl(mark_price)
            for trade in self._worker.engine.open_trades
        )
        return -(realized + unrealised)

    def evaluate(self, candle_ts: int, mark_price: float) -> None:
        """Mark to market for one evaluated row; trip / roll the day."""
        if not self.enabled:
            return
        day = int(candle_ts) // _DAY_MS
        if day != self._day:
            self._roll_day(day)
        if self._tripped:
            return
        loss = self.current_loss(day, mark_price)
        if loss >= self.threshold():
            self._trip(int(candle_ts), loss)

    def _roll_day(self, day: int) -> None:
        first = self._day is None
        self._day = day
        self._tripped = False
        if self._mode != "percent":
            return
        engine = self._worker.engine
        if first:
            self._day_base = engine.start_balance
            return
        try:
            self._day_base = engine.read_balance()
        except BrokerError as exc:
            self._worker._error(
                "daily_loss",
                f"UTC-day-start balance read failed ({exc}) — the percent "
                f"threshold keeps the previous base ${self._day_base:g}.",
            )

    def _trip(self, candle_ts: int, loss: float) -> None:
        worker = self._worker
        config = worker.config
        self._tripped = True
        close_all = bool(config.close_on_daily_loss)
        worker.events.emit(DailyLossEvent(
            time=candle_ts, symbol=config.symbol, source=SOURCE_LIVE,
            limit=config.max_daily_loss, loss=loss, closed_all=close_all,
        ))
        limit = config.max_daily_loss
        limit_text = limit if isinstance(limit, str) else f"${limit:g}"
        worker._log(
            f"MAX DAILY LOSS HIT — ${loss:g} lost this UTC day (limit "
            f"{limit_text}); no new entries until the next UTC day."
            + (" Closing all positions." if close_all else "")
        )
        if close_all:
            worker.engine.force_close()


class _PairWorker:
    """One pair's live machinery: queue, producers, and the consumer loop."""

    def __init__(self, pair: TraderPair, *, events: EventStream, engine: ExecutionEngine) -> None:
        self.pair = pair
        self.broker = pair.broker
        self.config: TraderConfig = pair.config
        self.strategy: BaseStrategy = pair.strategy
        self.events = events
        self.engine = engine
        self.market = getattr(pair.broker, "market", "")
        self.tf = pair.strategy.primary_timeframe
        self.tf_ms = timeframe_ms(self.tf)

        self.data: dict[str, pd.DataFrame] = {}
        self.closed_trades: list[ClosedTrade] = []
        self.stop_event = threading.Event()
        self.queue: queue.Queue = queue.Queue()
        self._pending: deque = deque()

        self._scheduler: CandleCloseScheduler | None = None
        self._scheduler_thread: threading.Thread | None = None
        self._reconcile_thread: threading.Thread | None = None
        self._feed = None            # forming-candle / ticker Stream (realtime modes)
        self._user_stream = None     # Binance user-data stream handle
        self._feed_error_reported = False
        self._reconcile_error_reported = False
        self._last_reconcile = 0.0

        self._acted: set[tuple[int, str]] = set()   # (candle_ts, direction) → entry acted
        self._exit_acted: set[int] = set()          # candle_ts → exit action taken
        self._forming: dict[str, float] | None = None   # tick-mode synthesized row
        self._last_committed_ts = 0
        self.daily_loss = _DailyLossGuard(self)     # max-daily-loss gate
        self.display = None                         # _DisplayBridge (Trader wires it)

        # persistence — armed by Trader.run() via attach_journal(); a
        # worker without a journal (unit tests, embedding) runs stateless.
        self.journal: TraderStateJournal | None = None
        self.pair_key = f"{self.market}:{self.config.symbol.upper()}"
        self._journal_error_reported = False

        # Every CLOSE event — engine-emitted (force/emergency close) or
        # detection-emitted (record_external_close) — lands in closed_trades,
        # and so does every partial ladder slice (TP_LEVEL events carry a
        # real ClosedTrade sharing the trade id).
        events.subscribe(self._collect_close)

    # ------------------------------------------------------------------
    # Event helpers
    # ------------------------------------------------------------------

    def _collect_close(self, event) -> None:
        if (
            getattr(event, "event_type", None) in (EVENT_CLOSE, EVENT_TP_LEVEL)
            and event.trade is not None
        ):
            self.closed_trades.append(event.trade)

    def _error(self, where: str, message: str, *, will_retry: bool = False,
               details: dict | None = None) -> None:
        self.events.emit(ErrorEvent(
            time=now_ms(), symbol=self.config.symbol, source=SOURCE_LIVE,
            where=where, message=message, will_retry=will_retry, details=details or {},
        ))

    def _log(self, message: str) -> None:
        print(f"{_LOG_PREFIX} {self.config.symbol}: {message}")

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------

    def attach_journal(self, journal: TraderStateJournal) -> None:
        """Arm persistence: every state change is flushed to *journal*."""
        self.journal = journal

    def _flush_state(self) -> None:
        """Journal the pair's current state (atomic; skipped when unchanged).

        Called after every processed queue item, after startup
        reconciliation and on shutdown — the crash window is one item.  A
        write failure emits ``ERROR`` once and trading continues: the venue
        SL/TP keep protecting the positions regardless of the journal.
        """
        if self.journal is None:
            return
        try:
            self.journal.write_pair(
                self.pair_key,
                build_pair_state(self.engine, self._acted, self._exit_acted),
            )
        except OSError as exc:
            if not self._journal_error_reported:
                self._journal_error_reported = True
                self._error(
                    "journal",
                    f"state journal write failed ({exc}) — restart reconciliation "
                    "would resume from the last successful flush.",
                    will_retry=True,
                )
            return
        self._journal_error_reported = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def seed(self) -> None:
        """Fetch the last ``min_candles`` and run the strategy over them.

        Builds the indicator columns and the strategy's ``setup()`` state —
        the live loop then advances it O(1) per candle.  Seed signals
        are history: discarded, never traded, no events.
        """
        symbol, count = self.config.symbol, self.config.min_candles
        rows = self.broker.fetch_last_candles(symbol, self.tf, count)
        if len(rows) < count:
            raise ValueError(
                f"Trader: {symbol} {self.tf} returned {len(rows)} candles — "
                f"min_candles={count} needed for the first strategy computation."
            )
        frame = pd.DataFrame(
            [{key: row[key] for key in ("timestamp", "open", "high", "low", "close", "volume")}
             for row in rows]
        )
        frame["timestamp"] = frame["timestamp"].astype("int64")
        for col in ("open", "high", "low", "close", "volume"):
            frame[col] = frame[col].astype(float)
        result = self.strategy.run({self.tf: frame}, mode=StrategyMode.BACKTEST)
        self.data = result.data
        self._last_committed_ts = int(frame["timestamp"].iloc[-1])

    def start(self) -> None:
        """Arm the engine, reconcile the journal, start every producer.

        Reconciliation runs **before** any producer thread exists, so no
        entry can fire against an unreconciled state; a venue read failure
        there propagates and aborts the start (trading blind past a journal
        with open positions is never safe).
        """
        self.engine.start()
        reconcile_startup(self)
        self._flush_state()

        # display: seed it after the engine + reconcile settled the state
        # and BEFORE any producer runs — startup cost once, then the trading
        # loop only ever enqueues (display never slows trading).
        if self.display is not None:
            self.display.start()

        # TraderConfig poll intervals drive polling venues (table): apply
        # them where the broker exposes the same-named knobs (MetaTrader).
        for name in ("candle_poll_interval", "tick_poll_interval"):
            if hasattr(self.broker, name):
                setattr(self.broker, name, getattr(self.config, name))

        self._scheduler = CandleCloseScheduler(
            self.broker, self.config.symbol, self.tf,
            last_open_ms=self._last_committed_ts, stop_event=self.stop_event,
        )
        self._scheduler_thread = threading.Thread(
            target=self._pump_scheduler,
            name=f"atk-trader-sched-{self.config.symbol.lower()}", daemon=True,
        )
        self._scheduler_thread.start()

        self._reconcile_thread = threading.Thread(
            target=self._pump_reconcile,
            name=f"atk-trader-reconcile-{self.config.symbol.lower()}", daemon=True,
        )
        self._reconcile_thread.start()

        if self.market in (MARKET_FUTURES, MARKET_SPOT) and hasattr(
            self.broker, "stream_user_data"
        ):
            try:
                self._user_stream = self.broker.stream_user_data(self._on_user_data)
            except Exception as exc:  # noqa: BLE001 — trade on, detection degrades
                self._error(
                    "user_data",
                    f"user-data stream unavailable ({exc}) — close detection falls "
                    "back to venue polling.",
                )

        if self.config.execution == EXEC_CANDLE_UPDATE:
            self._feed = self.broker.stream_candles(
                self.config.symbol, self.tf, self._on_forming, closed_only=False
            )
        elif self.config.execution == EXEC_TICK:
            self._feed = self.broker.stream_ticker(self.config.symbol, self._on_tick)
        elif self.config.tp_mode == TP_MODE_MULTI_RR and self.market == MARKET_FOREX:
            #: MT5 ladder levels are feed-detected — "the moment a level
            # price is reached".  candle_close pairs subscribe a forming stream
            # used ONLY for ladder checks (no forming strategy evaluation).
            self._feed = self.broker.stream_candles(
                self.config.symbol, self.tf, self._on_forming, closed_only=False
            )

    def shutdown(self, *, close_all: bool = False) -> None:
        """Stop producers and feeds, then apply the ``on_stop`` policy.

        Default (``"keep"``): open positions stay, their venue SL/TP keep
        protecting them — protective orders are **not** cancelled.
        *close_all* (``on_stop="close_all"``): market-close every tracked
        position and cancel its working orders; a close the venue refuses
        keeps that position tracked and protected (``ERROR`` emitted).
        """
        self.stop_event.set()
        if self._scheduler is not None:
            self._scheduler.stop()
        if self._feed is not None:
            try:
                self._feed.stop()
            except Exception:  # noqa: BLE001
                pass
        if self._user_stream is not None:
            try:
                self._user_stream.stop()
            except Exception:  # noqa: BLE001
                pass
        for thread in (self._scheduler_thread, self._reconcile_thread):
            if thread is not None:
                thread.join(timeout=5.0)
        if close_all and self.engine.open_trades:
            self._log(
                "on_stop='close_all' — market-closing every open position and "
                "cancelling its working orders."
            )
            self.engine.force_close()
        kept = self.engine.open_trades
        if kept:
            self._log(
                f"{len(kept)} open position(s) kept — the venue SL/TP stay armed."
            )
        if self.display is not None:
            #: drain the display leftovers (shutdown closes included) and
            # push the final page state; the servers stay up via keep-alive.
            try:
                self.display.close()
            except Exception:  # noqa: BLE001 — display must never block shutdown
                pass
        self._flush_state()   #: the final journal state of this session

    # ------------------------------------------------------------------
    # Producers (their threads only enqueue — the consumer does the work)
    # ------------------------------------------------------------------

    def _pump_scheduler(self) -> None:
        while not self.stop_event.is_set():
            tick = self._scheduler.wait_next_close()
            if tick is None:
                return
            self.queue.put(("candles", tick))

    def _pump_reconcile(self) -> None:
        interval = max(float(self.config.candle_poll_interval), _RECONCILE_MIN_INTERVAL)
        while not self.stop_event.wait(interval):
            self.queue.put(("reconcile", None))

    def _on_forming(self, candle: dict) -> None:
        if candle.get("closed"):
            return  # commits come only from the scheduler (quirk-safe)
        self.queue.put(("forming", dict(candle)))

    def _on_tick(self, ticker) -> None:
        self.queue.put(("tick", ticker))

    def _on_user_data(self, msg: dict) -> None:
        self.queue.put(("user_data", msg))

    # ------------------------------------------------------------------
    # Consumer loop
    # ------------------------------------------------------------------

    def run_loop(self) -> None:
        """Drain the queue until stopped.  Runs on the caller's thread."""
        while not self.stop_event.is_set():
            item = self._next_item()
            if item is None:
                self._check_feed_alive()
                continue
            kind, payload = item
            try:
                if kind == "candles":
                    self._process_scheduler_tick(payload)
                elif kind == "forming":
                    self._process_forming(payload)
                elif kind == "tick":
                    self._process_tick(payload)
                elif kind == "user_data":
                    self._process_user_data(payload)
                elif kind == "reconcile":
                    self._reconcile_positions()
            except Exception as exc:  # noqa: BLE001 — a live session must survive
                self._error(kind, f"processing failed: {exc!r}", will_retry=True)
            self._flush_state()   #: journal every state change (one item = one flush)

    def _next_item(self) -> tuple[str, Any] | None:
        if self._pending:
            item = self._pending.popleft()
        else:
            try:
                item = self.queue.get(timeout=_QUEUE_POLL_SECONDS)
            except queue.Empty:
                return None
        if item[0] in _COALESCE_KINDS:
            # Collapse a backlog of same-kind items to the newest one.
            while True:
                try:
                    nxt = self.queue.get_nowait()
                except queue.Empty:
                    break
                if nxt[0] == item[0]:
                    item = nxt
                else:
                    self._pending.append(nxt)
                    break
        return item

    def _check_feed_alive(self) -> None:
        if (
            self._feed is not None
            and not self._feed.alive
            and not self._feed_error_reported
        ):
            self._feed_error_reported = True
            if self.config.execution == EXEC_CANDLE_CLOSE:
                message = (
                    "ladder detection feed ended — multi-RR level detection falls "
                    "back to closed candles; trading continues on the scheduler."
                )
            else:
                message = (
                    "realtime feed ended — forming-candle evaluation stopped; "
                    "closed-candle trading continues on the scheduler."
                )
            self._error("feed", message)

    # ------------------------------------------------------------------
    # Closed candles (the scheduler is the only committer, every mode)
    # ------------------------------------------------------------------

    def _process_scheduler_tick(self, tick: SchedulerTick) -> None:
        candles = tick.candles
        newest = len(candles) - 1
        for idx, candle in enumerate(candles):
            candle_ts = int(candle["timestamp"])
            signals, exits = advance_live_candle(
                self.strategy, self.data, candle,
                recompute_window=self.config.recompute_window,
            )
            self._last_committed_ts = candle_ts
            if self._forming is not None and self._forming["timestamp"] <= candle_ts:
                self._forming = None
            # Sim per-candle order: SL management → TP-level checks →
            # exit-signal force close → new entries.  The venue's armed SL/TP
            # orders do the close-check part; their fills arrive via close
            # detection.: feed-detected ladder levels (MT5; degraded /
            # fraction-less futures levels) settle off the candle's high/low.
            self.engine.update_stops(candle)
            self.engine.check_tp_levels(
                float(candle["high"]), float(candle["low"]), candle_ts
            )
            #: mark the daily-loss gate to this candle's close before any
            # entry decision (exit closes stay allowed while gated).
            self.daily_loss.evaluate(candle_ts, float(candle["close"]))
            self._act_on_exits(exits, candle_ts)
            # Entries act only on the tick's newest candle: after a gap-fill,
            # older candles' signals are stale (the market moved past their
            # entry/SL references) — they print, but never place an order.
            self._act_on_signals(signals, candle_ts, allow_entry=idx == newest)
            if self.display is not None:
                self.display.enqueue_candle(candle)   #: hand-over, never wait
        self._prune_dedup()
        self._reconcile_positions()

    # ------------------------------------------------------------------
    # Forming evaluations (candle_update / tick modes — throwaway state)
    # ------------------------------------------------------------------

    def _process_forming(self, candle: dict) -> None:
        candle_ts = int(candle["timestamp"])
        if candle_ts <= self._last_committed_ts:
            return  # stale — that interval is already committed
        if self.display is not None:
            self.display.enqueue_forming(candle)   #: forming rows render live
        #: ladder levels settle the moment the forming price touches them
        # — no-op unless tp_mode="multi_rr".
        self.engine.check_tp_levels(
            float(candle["high"]), float(candle["low"]), candle_ts
        )
        #: forming rows mark the daily-loss gate too, so a
        # close_on_daily_loss flatten fires mid-candle, not at the boundary.
        self.daily_loss.evaluate(candle_ts, float(candle["close"]))
        if self.config.execution == EXEC_CANDLE_CLOSE:
            return  # ladder-detection feed only — no forming strategy evaluation
        signals, exits = evaluate_forming_candle(
            self.strategy, self.data, candle,
            recompute_window=self.config.recompute_window,
        )
        self._act_on_exits(exits, candle_ts)
        self._act_on_signals(signals, candle_ts, allow_entry=True)

    def _process_tick(self, ticker) -> None:
        price = (
            float(ticker.last or 0.0)
            or float(ticker.bid or 0.0)
            or float(ticker.ask or 0.0)
        )
        if price <= 0:
            return
        stamp = int(ticker.timestamp or 0) or now_ms()
        k = (stamp - self._last_committed_ts) // self.tf_ms
        if k < 1:
            return  # tick belongs to an already-committed interval
        open_ms = self._last_committed_ts + k * self.tf_ms
        forming = self._forming
        if forming is None or int(forming["timestamp"]) != open_ms:
            forming = {
                "timestamp": open_ms, "open": price, "high": price,
                "low": price, "close": price, "volume": 0.0,
            }
            self._forming = forming
        else:
            forming["high"] = max(forming["high"], price)
            forming["low"] = min(forming["low"], price)
            forming["close"] = price
        self._process_forming(dict(forming))

    # ------------------------------------------------------------------
    # Acting on strategy output (dedup lives here)
    # ------------------------------------------------------------------

    def _act_on_signals(self, signals: list[Signal], candle_ts: int, *, allow_entry: bool) -> None:
        for signal in signals:
            key = (candle_ts, signal.direction)
            duplicate = key in self._acted
            if not duplicate:
                self.events.emit(SignalEvent(
                    time=int(signal.timestamp), symbol=self.config.symbol,
                    source=SOURCE_LIVE, direction=signal.direction,
                    entry_price=signal.entry_price, stop_loss=signal.stop_loss,
                    take_profit=signal.take_profit, rr=_signal_rr(signal),
                    timeframe=signal.timeframe, metadata=dict(signal.metadata),
                    signal=signal,
                ))
            if duplicate:
                continue  # once per (pair, candle, direction)
            self._acted.add(key)
            if not allow_entry:
                self._log(
                    f"stale {signal.direction} signal from catch-up candle "
                    f"{candle_ts} skipped — entries act only on the newest candle."
                )
                continue
            if self.daily_loss.blocked:
                self._log(
                    f"{signal.direction} signal skipped — max_daily_loss hit; "
                    "no new entries until the next UTC day."
                )
                continue
            try:
                self.engine.open_position(signal)
            except OrderError as exc:
                # e.g. a short signal on Binance spot — one refused signal
                # must not end the session.
                self._error("open_position", str(exc))

    def _act_on_exits(self, exits: list, candle_ts: int) -> None:
        if not exits or candle_ts in self._exit_acted:
            return
        self._exit_acted.add(candle_ts)
        action = "force_close" if self.config.force_close_on_exit_signal else "none"
        for exit_signal in exits:
            self.events.emit(ExitSignalEvent(
                time=int(getattr(exit_signal, "timestamp", 0) or candle_ts),
                symbol=self.config.symbol, source=SOURCE_LIVE,
                reason=exit_signal.reason, action=action, exit_signal=exit_signal,
            ))
        if self.config.force_close_on_exit_signal:
            # Exits act even on stale catch-up candles: the strategy already
            # abandoned the position — flattening late beats holding it.
            self.engine.force_close()

    def _prune_dedup(self) -> None:
        horizon = self._last_committed_ts - _DEDUP_KEEP_CANDLES * self.tf_ms
        self._acted = {key for key in self._acted if key[0] >= horizon}
        self._exit_acted = {ts for ts in self._exit_acted if ts >= horizon}

    # ------------------------------------------------------------------
    # Close detection — Binance user-data stream
    # ------------------------------------------------------------------

    def _find_trade_by_order(self, order_id: str) -> tuple[LiveTrade | None, str]:
        for trade in self.engine.open_trades:
            if trade.sl_order_id and trade.sl_order_id == order_id:
                return trade, "sl"
            if trade.tp_order_id and trade.tp_order_id == order_id:
                return trade, "tp"
            if order_id in trade.ladder_order_ids:
                return trade, "ladder"   # multi-RR level order
        return None, ""

    def _process_user_data(self, msg: dict) -> None:
        event_type = msg.get("e")
        if event_type == "ORDER_TRADE_UPDATE":       # futures
            order = msg.get("o") or {}
            if order.get("X") != "FILLED":
                return
            order_id = str(order.get("i", ""))
            trade, which = self._find_trade_by_order(order_id)
            if trade is None:
                return
            exit_price = float(order.get("ap") or 0.0) or float(order.get("L") or 0.0)
            quantity = float(order.get("z") or 0.0) or float(order.get("l") or 0.0)
            close_time = int(msg.get("E") or order.get("T") or 0) or None
            if which == "ladder":
                #: a multi-RR level order filled — the engine settles the
                # ladder (slice, SL move, TP_LEVEL / CLOSE events).
                self.engine.record_ladder_fill(
                    trade, order_id, exit_price, quantity, close_time=close_time
                )
                return
            self._close_from_venue(trade, which, exit_price, quantity, close_time)
        elif event_type == "executionReport":        # spot
            if msg.get("X") != "FILLED":
                return
            order_id = str(msg.get("i", ""))
            list_id = str(msg.get("g", "") or "")
            trade: LiveTrade | None = None
            which = ""
            for candidate in self.engine.open_trades:
                if candidate.sl_order_id and candidate.sl_order_id == order_id:
                    trade, which = candidate, "sl"
                    break
                if (
                    candidate.oco_order_list_id
                    and list_id not in ("", "-1")
                    and candidate.oco_order_list_id == list_id
                ):
                    # Which OCO leg filled: LIMIT_MAKER is the TP leg.
                    which = "tp" if str(msg.get("o", "")) == "LIMIT_MAKER" else "sl"
                    trade = candidate
                    break
            if trade is None:
                return
            quantity = float(msg.get("z") or 0.0) or float(msg.get("l") or 0.0)
            exit_price = float(msg.get("L") or 0.0)
            if not exit_price and quantity:
                exit_price = float(msg.get("Z") or 0.0) / quantity
            close_time = int(msg.get("E") or 0) or None
            self._close_from_venue(trade, which, exit_price, quantity, close_time)

    def _close_from_venue(
        self,
        trade: LiveTrade,
        which: str,
        exit_price: float,
        quantity: float,
        close_time: int | None,
    ) -> None:
        """A tracked protective order filled on the venue — record it."""
        pos = trade.pos
        if exit_price <= 0:
            exit_price = self._price_fallback(pos)
        reason = sl_reason(pos) if which == "sl" else CLOSE_REASON_TP
        partial = 0.0 < quantity < pos.size - _QTY_EPSILON
        self.engine.record_external_close(
            trade, exit_price, reason,
            close_time=close_time,
            closed_size=quantity if partial else None,
        )
        if not partial:
            self._cancel_leftover_protection(trade, filled=which)

    def _price_fallback(self, pos) -> float:
        try:
            last = float(self.broker.get_ticker(self.config.symbol).last)
            if last > 0:
                return last
        except BrokerError:
            pass
        return float(pos.entry_price)

    def _cancel_leftover_protection(self, trade: LiveTrade, filled: str | None = None) -> None:
        """After a full close, cancel whatever protective order is still armed.

        Futures protection is two independent orders — the surviving one must
        be cancelled.  A filled spot OCO leg auto-cancels its sibling (venue-
        native); a manual spot close (``filled=None``) may leave the whole
        protection armed.  MT5 SL/TP are position attributes — gone with it.
        """
        symbol = self.config.symbol
        try:
            if self.market == MARKET_FUTURES:
                protective = [("sl", trade.sl_order_id), ("tp", trade.tp_order_id)]
                #: remaining ladder orders die with the position too.
                protective += [("ladder", oid) for oid in trade.ladder_order_ids]
                for label, order_id in protective:
                    if order_id and label != filled:
                        self.broker.cancel_order(order_id, symbol)
                trade.ladder_order_ids.clear()
            elif self.market == MARKET_SPOT and filled is None:
                if trade.oco_order_list_id:
                    self.broker.cancel_order_list(symbol, trade.oco_order_list_id)
                elif trade.sl_order_id:
                    self.broker.cancel_order(trade.sl_order_id, symbol)
        except BrokerError as exc:
            self._error(
                "cleanup",
                f"leftover protective order of trade #{trade.pos.trade_id} could "
                f"not be cancelled: {exc}",
                details={"trade_id": trade.pos.trade_id},
            )

    # ------------------------------------------------------------------
    # Close detection — venue position polling (MT5; futures manual closes)
    # ------------------------------------------------------------------

    def _reconcile_positions(self) -> None:
        if not self.engine.open_trades:
            return
        now = time.monotonic()
        if now - self._last_reconcile < _RECONCILE_MIN_INTERVAL / 2:
            return
        self._last_reconcile = now
        try:
            if self.market == MARKET_FOREX:
                self._reconcile_mt5()
            elif self.market == MARKET_FUTURES:
                self._reconcile_futures()
            elif self.market == MARKET_SPOT:
                self._reconcile_spot()
        except BrokerError as exc:
            if not self._reconcile_error_reported:
                self._reconcile_error_reported = True
                self._error("reconcile", f"venue poll failed: {exc}", will_retry=True)
            return
        self._reconcile_error_reported = False

    def _reconcile_mt5(self) -> None:
        positions = self.broker.open_positions(self.config.symbol)
        by_ticket = {str(p.position_id): p for p in positions}
        for trade in list(self.engine.open_trades):
            if not trade.mt5_ticket:
                continue
            venue = by_ticket.get(str(trade.mt5_ticket))
            if venue is None:
                exit_price, reason, close_time = self._mt5_exit_fill(trade)
                self.engine.record_external_close(
                    trade, exit_price, reason, close_time=close_time
                )
            elif float(venue.quantity) < trade.pos.size - _QTY_EPSILON:
                # Venue-side partial reduction (manual partial close).
                closed_size = trade.pos.size - float(venue.quantity)
                exit_price, reason, close_time = self._mt5_exit_fill(trade)
                self.engine.record_external_close(
                    trade, exit_price, reason,
                    close_time=close_time, closed_size=closed_size,
                )

    def _mt5_exit_fill(self, trade: LiveTrade) -> tuple[float, str, int]:
        """Real exit fill of an MT5 position from its deal history:
        ``(price, reason, close_time_ms)`` with ticker/now fallbacks."""
        try:
            deals = self.broker.history_deals(position=int(trade.mt5_ticket))
        except (BrokerError, TypeError, ValueError):
            deals = []
        exits = [d for d in deals if int(d.get("entry", 0)) != _MT5_DEAL_ENTRY_IN]
        if exits:
            deal = exits[-1]
            price = float(deal.get("price", 0.0) or 0.0)
            close_time = (
                int(deal.get("time_msc") or 0)
                or int(deal.get("time", 0) or 0) * 1000
                or now_ms()
            )
            deal_reason = int(deal.get("reason", -1))
            if deal_reason == _MT5_DEAL_REASON_SL:
                reason = sl_reason(trade.pos)
            elif deal_reason == _MT5_DEAL_REASON_TP:
                reason = CLOSE_REASON_TP
            else:
                reason = CLOSE_REASON_MANUAL
            if price > 0:
                return price, reason, close_time
        return self._price_fallback(trade.pos), CLOSE_REASON_MANUAL, now_ms()

    def _reconcile_futures(self) -> None:
        """Catch manual closes: venue net exposure below the tracked sum means
        something was closed outside our orders — close tracked trades FIFO
        (heuristic; the venue nets one-way positions) with reason manual."""
        positions = self.broker.open_positions(self.config.symbol)
        venue_net = sum(
            float(p.quantity) if p.side == POSITION_LONG else -float(p.quantity)
            for p in positions
        )
        tracked_net = sum(
            t.pos.size if t.pos.direction == "long" else -t.pos.size
            for t in self.engine.open_trades
        )
        diff = tracked_net - venue_net
        if abs(diff) <= _QTY_EPSILON:
            return
        direction = "long" if diff > 0 else "short"
        remaining = abs(diff)
        price = None
        for trade in list(self.engine.open_trades):
            if remaining <= _QTY_EPSILON:
                break
            if trade.pos.direction != direction:
                continue
            if price is None:
                price = self._price_fallback(trade.pos)
            take = min(trade.pos.size, remaining)
            partial = take < trade.pos.size - _QTY_EPSILON
            self.engine.record_external_close(
                trade, price, CLOSE_REASON_MANUAL,
                closed_size=take if partial else None,
            )
            if not partial:
                self._cancel_leftover_protection(trade)
            remaining -= take

    def _reconcile_spot(self) -> None:
        """Degraded-path detection only: with the user-data stream down, a
        vanished protection means the holding was closed (or unprotected) —
        recorded with the ticker price and reason manual (reconciles
        offline flows properly)."""
        if self._user_stream is not None:
            return  # the user-data stream is authoritative on spot
        orders = self.broker.open_orders(self.config.symbol)
        open_ids = {o.order_id for o in orders}
        open_lists = {str(o.raw.get("orderListId", "")) for o in orders}
        for trade in list(self.engine.open_trades):
            protected = (trade.sl_order_id in open_ids) or (
                trade.oco_order_list_id and trade.oco_order_list_id in open_lists
            )
            if protected:
                continue
            self.engine.record_external_close(
                trade, self._price_fallback(trade.pos), CLOSE_REASON_MANUAL
            )


class Trader:
    """
    Real-order live trading on one or more ``(broker, symbol)`` pairs
    (v1.0.0; multi-pair).

    "Config + strategy + broker — these three are enough"::

        from AlgoTradeKit.broker import Broker
        from AlgoTradeKit.trader import Trader, TraderConfig

        t = Trader(broker=Broker("binance-futures", api_key=..., api_secret=...),
                   strategy=MyStrategy(),
                   config=TraderConfig(symbol="BTCUSDT", min_candles=500))
        t.run()        # blocking; Ctrl+C = graceful shutdown; t.stop() from code

    Multi-pair / multi-venue — the same entry list as ``run_live``;
    brokers may repeat across entries (pairs on one broker share that
    account's wallet naturally — sizing reads the account balance)::

        t = Trader(pairs=[
                TraderPair(broker=binance, config=cfg_btc, strategy=StratA()),
                TraderPair(broker=mt5,     config=cfg_eur, strategy=StratB()),
                TraderPair(broker=mt5,     config=cfg_gbp, strategy=StratB()),
            ], on_stop="keep")
        t.run()        # one worker thread per pair; stop/Ctrl+C stops them all

    The worker loop runs each pair's ``config.execution`` mode — see the
    module docstring for the three modes, the signal dedup rule, the
    venue-side close detection and the multi-RR ladder (venue-native
    reduce-only level orders on Binance futures; feed-detected partial closes
    on MetaTrader).  Every meaningful moment emits its event tagged
    ``[LIVE]``; subscribe your own callbacks via :attr:`events` (in the
    multi-pair form an aggregate stream carrying every pair's events; the
    per-pair terminal logs keep their own ``log_event_types`` filters).

    Safety rails: the session ends gracefully via :meth:`stop`, Ctrl+C
    / SIGTERM (handlers installed while ``run()`` blocks on the main thread;
    a **second** signal falls back to the default behaviour so a stuck
    shutdown can still be interrupted) or by touching ``kill_switch_file`` —
    each of them stops **every** pair.  Shutdown then applies ``on_stop`` to
    all pairs: ``"keep"`` (default) leaves positions open with their
    venue-native SL/TP armed — a stopped (or crashed) trader never
    leaves a position unprotected; ``"close_all"`` market-closes everything
    and cancels the working orders.  ``config.max_daily_loss`` gates entries
    per pair for the rest of the UTC day once that pair's realized +
    unrealized loss reaches it (``DAILY_LOSS`` event + loud log; flatten too
    with ``close_on_daily_loss=True``).  There is no dry-run mode:
    rehearse strategy behaviour with ``run_live()`` and order plumbing
    against venue test environments (Binance ``testnet=True``, MT5 demo
    account).  ``config.display=True`` serves the live display beside
    the trading — chart + report per pair (``display_trades`` picks sim /
    real / both trade sources), URLs printed instead of a browser tab
    with ``display_open_browser=False``, and one combined report page when
    two or more pairs display; display work runs on a low-priority
    background thread and never slows the trading loop.

    Parameters
    ----------
    broker / strategy / config
        The single pair (validated exactly like a :class:`TraderPair`).  The
        broker must be authenticated and its ``market`` one of ``futures`` /
        ``spot`` / ``forex``.
    pairs
        The multi-pair form — an iterable of :class:`TraderPair`;
        mutually exclusive with the single-form arguments.  The same
        ``(broker, symbol)`` may not appear twice.  One worker (and
        one consumer thread) per entry; every pair keeps its own event
        stream, terminal log, daily-loss gate and journal key.
    on_stop / state_path / kill_switch_file
        Trader-level settings (``TraderSettings``) — they apply to every
        pair.  ``on_stop`` and ``kill_switch_file`` are live;
        ``state_path`` is the state journal (``None`` →
        ``./.atk_trader_state.json``) — journaled on every change,
        reconciled against the venue on the next ``run()``.  A
        ``kill_switch_file`` that already exists when ``run()`` starts
        raises — remove the stale file first (it is never auto-deleted); a
        corrupt journal file raises the same way.
    trader_id : str | None
        Short id embedded in every ``client_order_id``
        (``atk-<trader_id>-<PAIR>-<signal_ts>``) — ties venue orders to
        this trader for reconciliation.  Shared by every pair.
        ``None`` → reuse the journal's id when one exists, else random.
    quote_asset : str | None
        Spot only: the quote currency whose free balance funds the pair
        (``None`` → detected from the symbol suffix; applies to every spot
        pair).
    """

    def __init__(
        self,
        broker: Any = None,
        strategy: Any = None,
        config: TraderConfig | None = None,
        *,
        pairs: Any = None,
        on_stop: str = "keep",
        state_path: Any = None,
        kill_switch_file: Any = None,
        trader_id: str | None = None,
        quote_asset: str | None = None,
    ) -> None:
        if pairs is not None:
            if broker is not None or strategy is not None or config is not None:
                raise ValueError(
                    "Trader: give either (broker, strategy, config) or "
                    "pairs=[TraderPair, ...] — not both."
                )
            entries = validate_pairs(pairs)
        else:
            if broker is None or strategy is None or config is None:
                raise ValueError(
                    "Trader: broker, strategy and config are all required "
                    "(or use pairs=[TraderPair, ...])."
                )
            entries = validate_pairs(
                [TraderPair(broker=broker, config=config, strategy=strategy)]
            )
        self._multi = pairs is not None
        self.settings = TraderSettings(
            on_stop=on_stop, state_path=state_path, kill_switch_file=kill_switch_file
        )
        # One session id for every pair (: the journal stores one
        # trader_id;: client_order_ids stay uniform across pairs).
        session_id = trader_id if trader_id is not None else secrets.token_hex(3)
        self._workers: list[_PairWorker] = []
        for pair in entries:
            events = EventStream()
            attach_terminal_printer(events, pair.config)
            engine = ExecutionEngine(
                pair.broker, pair.config,
                events=events,
                trader_id=session_id,
                primary_timeframe=pair.strategy.primary_timeframe,
                quote_asset=quote_asset,
            )
            worker = _PairWorker(pair, events=events, engine=engine)
            if pair.config.display:
                # display bridge.  Sim modes get a dedicated strategy
                # instance (deep-copied pristine, before any seed): the
                # worker and the display sim each advance their own copy —
                # sharing one would double-advance its state.
                sim_strategy = None
                if pair.config.display_trades != DISPLAY_TRADES_REAL:
                    sim_strategy = copy.deepcopy(pair.strategy)
                worker.display = _DisplayBridge(worker, sim_strategy=sim_strategy)
            self._workers.append(worker)
        #: disambiguate journal keys when the same (market, symbol)
        # repeats across brokers (two accounts): first stays bare, then #2…
        # (by pair order — keep that relative order stable across restarts).
        key_counts: dict[str, int] = {}
        for worker in self._workers:
            base = worker.pair_key
            key_counts[base] = key_counts.get(base, 0) + 1
            if key_counts[base] > 1:
                worker.pair_key = f"{base}#{key_counts[base]}"
        # Multi-pair: one aggregate stream carrying every pair's events, so
        # custom subscribers (backends) need a single subscribe point.
        self._events_all: EventStream | None = None
        if self._multi:
            self._events_all = EventStream()
            for worker in self._workers:
                worker.events.subscribe(self._events_all.emit)
        self._stop_event = threading.Event()
        self._explicit_trader_id = trader_id is not None
        self._combined = None       # combined report page (multi-pair display)
        self._ran = False

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def _worker(self) -> _PairWorker:
        """The single-pair worker (internal; multi-pair code uses ``_workers``)."""
        return self._workers[0]

    @property
    def events(self) -> EventStream:
        """The event stream — subscribe custom callbacks here.

        Single-pair form: the pair's own stream.  Multi-pair form: an
        aggregate stream every pair forwards into (subscribe once, receive
        every pair's events; per-pair terminal printers keep their own
        ``log_event_types`` filters on the per-pair streams).
        """
        if self._events_all is not None:
            return self._events_all
        return self._workers[0].events

    @property
    def engines(self) -> tuple[ExecutionEngine, ...]:
        """The execution engines, one per pair, in pair order."""
        return tuple(worker.engine for worker in self._workers)

    @property
    def engine(self) -> ExecutionEngine:
        """The single pair's execution engine (single-pair form only)."""
        if self._multi:
            raise RuntimeError(
                "Trader.engine is single-pair only — use Trader.engines "
                "(one engine per pair, in pair order)."
            )
        return self._workers[0].engine

    @property
    def trader_id(self) -> str:
        """The id embedded in every ``client_order_id`` this trader places
        (shared by every pair)."""
        return self._workers[0].engine.trader_id

    @property
    def open_trades(self) -> tuple[LiveTrade, ...]:
        """Live positions currently under management (all pairs, pair order)."""
        return tuple(
            trade for worker in self._workers for trade in worker.engine.open_trades
        )

    @property
    def closed_trades(self) -> tuple[ClosedTrade, ...]:
        """Every real close recorded this session (-shaped, real fills).

        Single-pair form: in detection order.  Multi-pair form: all pairs
        merged, sorted by ``close_time`` (stable — same-time records keep
        pair order).
        """
        if not self._multi:
            return tuple(self._workers[0].closed_trades)
        merged = [
            trade for worker in self._workers for trade in worker.closed_trades
        ]
        merged.sort(key=lambda trade: trade.close_time)
        return tuple(merged)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def run(self) -> list[ClosedTrade]:
        """
        Trade until :meth:`stop`, Ctrl+C / SIGTERM, or the kill-switch file
        (blocking).

        Seeds every pair's strategy over its last ``min_candles``, arms the
        execution engines (leverage, balance, venue costs), starts the
        schedulers + each mode's feeds, then drains the worker queues —
        on this thread in the single-pair form, on one consumer thread per
        pair in the multi-pair form.  Shutdown applies the ``on_stop``
        policy to every pair.  Returns the session's real
        :class:`ClosedTrade` records — all pairs merged, sorted by
        ``close_time`` in the multi-pair form; with ``on_stop="keep"`` open
        positions stay protected by their venue SL/TP.

        A failure while seeding/starting (short history, venue read error in
        the reconcile…) aborts the start: pairs already started are shut
        down again — feeds stopped, positions **kept** protected — and
        the error propagates.
        """
        if self._ran:
            raise RuntimeError("Trader.run() may only run once per instance.")
        kill_file = self.settings.kill_switch_file
        if kill_file and os.path.exists(kill_file):
            # Refused before anything starts — the instance stays runnable
            # once the stale file is removed.
            raise ValueError(
                f"Trader: kill_switch_file {kill_file!r} already exists — a previous "
                "stop left it behind; remove the stale file before starting."
            )
        #: a corrupt journal also refuses before anything starts (and
        # before _ran is set) — fix or remove the file, then run again.
        self._prepare_journal()
        self._ran = True
        started: list[_PairWorker] = []
        try:
            for worker in self._workers:
                worker.seed()          # all seeds first — fail fast
            for worker in self._workers:
                started.append(worker)   # a mid-start failure shuts it down too
                worker.start()
        except BaseException:
            # Abort: stop whatever already started; positions stay protected
            # by their venue SL/TP — never close_all on a failed start.
            for worker in started:
                try:
                    worker.shutdown()
                except Exception:  # noqa: BLE001 — best-effort cleanup
                    pass
            raise
        #: one combined report across the displaying pairs.  Armed
        # after every pair started — a candle landing in this gap skips one
        # combined push; the next one carries the current state anyway.
        self._maybe_start_combined()
        previous_handlers = self._install_signal_handlers()
        watcher = self._start_kill_switch_watcher()
        print(f"{_LOG_PREFIX}: live trading {self._describe_pairs()} — Ctrl+C to stop.")
        threads: list[threading.Thread] = []
        try:
            if not self._multi:
                self._workers[0].run_loop()
            else:
                for worker in self._workers:
                    thread = threading.Thread(
                        target=worker.run_loop,
                        name=f"atk-trader-pair-{worker.config.symbol.lower()}",
                        daemon=True,
                    )
                    threads.append(thread)
                    thread.start()
                while any(thread.is_alive() for thread in threads):
                    for thread in threads:
                        thread.join(timeout=_QUEUE_POLL_SECONDS)
        except KeyboardInterrupt:
            print(f"{_LOG_PREFIX}: Ctrl+C — stopping.")
        finally:
            self._stop_event.set()
            for worker in self._workers:
                worker.stop_event.set()   # every pair winds down concurrently
            close_all = self.settings.on_stop == ON_STOP_CLOSE_ALL
            for worker in self._workers:
                worker.shutdown(close_all=close_all)
            for thread in threads:
                thread.join(timeout=5.0)
            if watcher is not None:
                watcher.join(timeout=5.0)
            self._restore_signal_handlers(previous_handlers)
        if not self._multi:
            return list(self._workers[0].closed_trades)
        return list(self.closed_trades)   # all pairs, sorted by close_time

    def stop(self) -> None:
        """End :meth:`run` promptly — every pair (callable from any thread /
        a subscriber)."""
        self._stop_event.set()
        for worker in self._workers:
            worker.stop_event.set()

    def _describe_pairs(self) -> str:
        """Startup-log description: ``SYMBOL (mode, tf)`` per pair."""
        return ", ".join(
            f"{worker.config.symbol} ({worker.config.execution}, {worker.tf})"
            for worker in self._workers
        )

    def _maybe_start_combined(self):
        """: the Trader's combined report page (the -deferred piece).

        Gate: multi-pair form with **two or more displaying pairs** — each
        contributes its display's primary report (``"sim"`` pairs the sim
        report, ``"real"``/``"both"`` pairs the real one); ``display=False``
        pairs have no display machinery and stay off the combined page.
        Host and open-browser-vs-print-URL behaviour follow the first
        displaying pair; labels are the ``run_live`` ``#N``-deduped symbols.
        Refreshed through each display bridge on every closed candle.
        """
        if not self._multi:
            return None
        display_workers = [w for w in self._workers if w.display is not None]
        if len(display_workers) < 2:
            return None
        from ._run_live import _combined_labels, _CombinedLiveReport

        first = display_workers[0].config
        labels = _combined_labels([w.pair for w in display_workers])
        combined = _CombinedLiveReport(
            labels,
            [w.display.current_report for w in display_workers],
            host=first.chart_host,
            open_browser=first.display_open_browser,
        )
        for label, worker in zip(labels, display_workers):
            worker.display.combined = combined
            worker.display.combined_label = label
        if not first.display_open_browser:
            print(f"{_LOG_PREFIX}: combined report → {combined.url}")
        self._combined = combined
        return combined

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------

    def _prepare_journal(self) -> TraderStateJournal:
        """Load (or create) the state journal and settle the session's
        ``trader_id``: an explicitly passed id always wins; otherwise a
        journaled id is reused so ``client_order_id``\\ s stay uniform across
        restarts.  One journal serves every pair (each under its own key).
        Raises ``ValueError`` on a corrupt journal file."""
        path = self.settings.state_path or DEFAULT_STATE_PATH
        journal = TraderStateJournal(path).load()
        if not self._explicit_trader_id and journal.trader_id:
            for worker in self._workers:
                worker.engine.trader_id = journal.trader_id
        journal.trader_id = self._workers[0].engine.trader_id
        for worker in self._workers:
            worker.attach_journal(journal)
        return journal

    # ------------------------------------------------------------------
    # kill switches
    # ------------------------------------------------------------------

    def _install_signal_handlers(self) -> dict[int, Any]:
        """SIGINT/SIGTERM → graceful :meth:`stop`.

        Handlers install only when ``run()`` blocks on the **main** thread (a
        Python restriction — elsewhere Ctrl+C keeps working through the
        ``KeyboardInterrupt`` fallback).  They are one-shot: the first signal
        requests the graceful stop and immediately restores the previous
        handlers, so a second signal interrupts a stuck shutdown the default
        way.  Returns the previous handlers for the ``finally`` restore.
        """
        previous: dict[int, Any] = {}
        if threading.current_thread() is not threading.main_thread():
            return previous

        def _handle(signum: int, frame: Any) -> None:
            self._restore_signal_handlers(previous)
            label = (
                "Ctrl+C" if signum == signal_module.SIGINT
                else signal_module.Signals(signum).name
            )
            print(f"{_LOG_PREFIX}: {label} — stopping. (again = force quit)")
            self.stop()

        for signum in (signal_module.SIGINT, signal_module.SIGTERM):
            try:
                previous[signum] = signal_module.signal(signum, _handle)
            except (ValueError, OSError):  # non-main interpreter / platform quirk
                continue
        return previous

    def _restore_signal_handlers(self, previous: dict[int, Any]) -> None:
        """Put back the pre-``run()`` handlers (idempotent, failure-safe)."""
        for signum, handler in previous.items():
            try:
                signal_module.signal(signum, handler)
            except (ValueError, OSError):
                continue

    def _start_kill_switch_watcher(self) -> threading.Thread | None:
        """Poll ``kill_switch_file`` on a daemon thread; existence →
        loud log + graceful :meth:`stop`.  The file is never deleted —
        it is the user's; ``None`` when no kill-switch file is configured."""
        path = self.settings.kill_switch_file
        if not path:
            return None

        def _watch() -> None:
            while not self._stop_event.wait(_KILL_SWITCH_POLL_SECONDS):
                if os.path.exists(path):
                    print(
                        f"{_LOG_PREFIX}: kill-switch file {path!r} detected — "
                        "stopping."
                    )
                    self.stop()
                    return

        thread = threading.Thread(
            target=_watch, name="atk-trader-killswitch", daemon=True
        )
        thread.start()
        return thread
