"""
AlgoTradeKit.trader._events
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Event stream + terminal log for the v1.0.0 live-trading module.

Every meaningful live-trading moment — signal, open, trailing SL move,
risk-free touch, multi-RR level, close, daily-loss halt, restart
reconciliation, error — produces a **typed event** (one frozen dataclass per
type).  Events are published on an :class:`EventStream`; subscribers consume
them.  v1.0.0 ships one subscriber — the :class:`TerminalEventPrinter` —
printing one timestamped, grep-able line per event::

    2026-07-11 14:00:00 [LIVE][BTCUSDT] SIGNAL long @ 64250 sl=63800 tp=65000 rr=1.67 tf=1h

The printer is a subscriber, not inlined: anything the terminal log can
print, a v1.1 notification backend (Telegram bot, webhook, …) can send by
subscribing to the same stream.

Producers land in later sections: ``run_live`` bridges the
``LiveSimulation`` event dicts onto these types tagged ``SOURCE_SIM``;
the real-order ``Trader`` emits them tagged ``SOURCE_LIVE``.
Configuration comes from :class:`~AlgoTradeKit.trader.TraderConfig`:
``log_events`` (master toggle) and ``log_event_types`` (``None`` = all, or a
set of the ``EVENT_*`` constants) — wired via :func:`attach_terminal_printer`.

The six event types the sim engine already knows are **imported from**
``AlgoTradeKit.simulate`` (values shared by construction — same precedent as
``_config`` importing ``TP_MODE_SIGNAL``); the four trader-only types are
defined here.  All constants are re-exported from ``trader/__init__.py``
(library convention).

Line-format conventions (:class:`TerminalEventPrinter`)
--------------------------------------------------------
* prefix: ``YYYY-MM-DD HH:MM:SS`` (UTC, from the event's UTC-ms ``time``) +
  ``[SIM]``/``[LIVE]`` + ``[SYMBOL]``.
* numbers print plain (``64250``, no thousands separators) so lines stay
  grep/awk-friendly; money fields carry ``$``; ratios (RR, R multiples) use
  two decimals; durations are humanized (``2h 15m``).
* optional fields (``None``) are simply omitted from the line.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, ClassVar

from ..simulate import (
    EVENT_CLOSE,
    EVENT_EXIT_SIGNAL,
    EVENT_OPEN,
    EVENT_SIGNAL,
    EVENT_SL_MOVE,
    EVENT_TP_LEVEL,
    ClosedTrade,
)
from ..strategy import ExitSignal, Signal

# ---------------------------------------------------------------------------
# Constants (re-exported from trader/__init__.py)
# ---------------------------------------------------------------------------

#: Trader-only event types (the other six are shared with — and imported
#: from — ``AlgoTradeKit.simulate``: signal / exit_signal / open / sl_move /
#: tp_level / close).
EVENT_RISK_FREE = "risk_free"    # SL jumped to break-even at risk_free_at_rr
EVENT_DAILY_LOSS = "daily_loss"  # max_daily_loss tripped — trading halted
EVENT_RECONCILE = "reconcile"    # restart reconciliation results
EVENT_ERROR = "error"            # failed order / retry / stream drop

#: Source tag — who produced the event (printed as ``[SIM]`` / ``[LIVE]``).
SOURCE_SIM = "sim"    # run_live paper trading / the Trader's display sim
SOURCE_LIVE = "live"  # real trading on the venue

#: Every valid event type — the ``TraderConfig.log_event_types`` domain.
ALL_EVENT_TYPES = frozenset({
    EVENT_SIGNAL,
    EVENT_EXIT_SIGNAL,
    EVENT_OPEN,
    EVENT_SL_MOVE,
    EVENT_RISK_FREE,
    EVENT_TP_LEVEL,
    EVENT_CLOSE,
    EVENT_DAILY_LOSS,
    EVENT_RECONCILE,
    EVENT_ERROR,
})

_SOURCES = (SOURCE_SIM, SOURCE_LIVE)

#: Fields shared by every event — everything else is per-type detail.
_COMMON_FIELDS = ("time", "symbol", "source")


# ---------------------------------------------------------------------------
# Event dataclasses — one per type, common fields on the base
# ---------------------------------------------------------------------------

@dataclass(frozen=True, kw_only=True)
class TraderEvent:
    """
    Base of every trader event.

    Common fields: ``time`` (UTC milliseconds), ``symbol`` (the pair; may be
    ``""`` for pair-less moments such as a trader-level error) and ``source``
    (``SOURCE_SIM`` / ``SOURCE_LIVE`` — printed as ``[SIM]`` / ``[LIVE]``).
    ``event_type`` is a class attribute carrying the type constant, so
    subscribers can dispatch without isinstance chains.
    """

    event_type: ClassVar[str] = ""

    time: int
    symbol: str
    source: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "time", int(self.time))
        if self.source not in _SOURCES:
            raise ValueError(
                f"event source must be '{SOURCE_SIM}' or '{SOURCE_LIVE}', "
                f"got {self.source!r}."
            )


@dataclass(frozen=True, kw_only=True)
class SignalEvent(TraderEvent):
    """Strategy emitted an entry :class:`~AlgoTradeKit.strategy.Signal`.

    ``size`` / ``risk_amount`` / ``rr`` are optional — not always computable
    at signal time (the sim sizes at open; the trader sizes off the live
    balance).  ``signal`` carries the originating object for subscribers.
    """

    event_type: ClassVar[str] = EVENT_SIGNAL

    direction: str
    entry_price: float
    stop_loss: float
    take_profit: float | None = None
    size: float | None = None
    risk_amount: float | None = None
    rr: float | None = None
    timeframe: str = ""
    metadata: dict = field(default_factory=dict)
    signal: Signal | None = None


@dataclass(frozen=True, kw_only=True)
class OpenEvent(TraderEvent):
    """A position was opened — sim fill or real fill (``order_id`` set)."""

    event_type: ClassVar[str] = EVENT_OPEN

    trade_id: int | str
    direction: str
    fill_price: float
    size: float
    margin_amount: float
    risk_amount: float | None = None
    stop_loss: float | None = None
    next_tp: float | None = None
    order_id: str | None = None   # venue order id; None for sim fills


@dataclass(frozen=True, kw_only=True)
class SlMoveEvent(TraderEvent):
    """The stop-loss moved (default cause: trailing; risk-free jumps have
    their own :class:`RiskFreeEvent`, ladder moves ride
    :class:`TpLevelEvent`)."""

    event_type: ClassVar[str] = EVENT_SL_MOVE

    trade_id: int | str
    old_sl: float
    new_sl: float
    cause: str = "trailing"
    next_tp: float | None = None


@dataclass(frozen=True, kw_only=True)
class RiskFreeEvent(TraderEvent):
    """``risk_free_at_rr`` touched — SL jumped to break-even."""

    event_type: ClassVar[str] = EVENT_RISK_FREE

    trade_id: int | str
    rr_level: float
    old_sl: float
    new_sl: float


@dataclass(frozen=True, kw_only=True)
class TpLevelEvent(TraderEvent):
    """A multi-RR level was hit and a fraction of the position closed
    (position still open).  ``trade`` is the partial ``ClosedTrade`` slice."""

    event_type: ClassVar[str] = EVENT_TP_LEVEL

    trade_id: int | str
    level: float
    fraction_closed: float
    realized_pnl: float
    new_sl: float | None = None
    trade: ClosedTrade | None = None


@dataclass(frozen=True, kw_only=True)
class CloseEvent(TraderEvent):
    """The position fully closed — nothing of this trade remains open."""

    event_type: ClassVar[str] = EVENT_CLOSE

    trade_id: int | str
    exit_price: float
    reason: str
    gross_pnl: float
    net_pnl: float
    pnl_r: float
    duration_ms: int
    trade: ClosedTrade | None = None


@dataclass(frozen=True, kw_only=True)
class ExitSignalEvent(TraderEvent):
    """Strategy emitted an :class:`~AlgoTradeKit.strategy.ExitSignal`;
    ``action`` says what the engine did (``"force_close"`` / ``"none"``)."""

    event_type: ClassVar[str] = EVENT_EXIT_SIGNAL

    reason: str
    action: str = "none"
    exit_signal: ExitSignal | None = None


@dataclass(frozen=True, kw_only=True)
class DailyLossEvent(TraderEvent):
    """``max_daily_loss`` tripped — no new entries until the next UTC day
.  ``limit`` is the configured value ($ number or ``"N%"`` string)."""

    event_type: ClassVar[str] = EVENT_DAILY_LOSS

    limit: float | str
    loss: float
    closed_all: bool = False


@dataclass(frozen=True, kw_only=True)
class ReconcileEvent(TraderEvent):
    """Restart reconciliation results: journaled positions adopted /
    found closed while offline / foreign venue positions left untouched.
    Entries are position labels (client order ids / journal keys)."""

    event_type: ClassVar[str] = EVENT_RECONCILE

    adopted: tuple[str, ...] = ()
    closed_offline: tuple[str, ...] = ()
    foreign: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        super().__post_init__()
        for name in ("adopted", "closed_offline", "foreign"):
            object.__setattr__(self, name, tuple(getattr(self, name)))


@dataclass(frozen=True, kw_only=True)
class ErrorEvent(TraderEvent):
    """Something failed — order rejected, retry, stream drop… ``message``
    carries the venue's own words."""

    event_type: ClassVar[str] = EVENT_ERROR

    where: str
    message: str
    will_retry: bool = False
    details: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# EventStream — the pub/sub backbone
# ---------------------------------------------------------------------------

class EventStream:
    """
    Minimal publish/subscribe stream for :class:`TraderEvent` objects.

    ``subscribe(callback)`` registers ``callback(event)`` and returns an
    unsubscribe callable (idempotent).  ``emit(event)`` delivers to every
    subscriber in subscription order; a failing subscriber is **warned and
    skipped, never raised** — one bad notification backend must not break
    trading (same isolation rule as ``LiveSimulation``'s callbacks).
    Emission iterates a snapshot, so a subscriber may unsubscribe (itself or
    others) mid-emit safely.
    """

    def __init__(self) -> None:
        self._subscribers: list[Callable[[TraderEvent], None]] = []

    def subscribe(self, callback: Callable[[TraderEvent], None]) -> Callable[[], None]:
        """Register ``callback(event)``; returns an unsubscribe callable."""
        if not callable(callback):
            raise TypeError(f"EventStream.subscribe needs a callable, got {callback!r}.")
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            try:
                self._subscribers.remove(callback)
            except ValueError:
                pass  # already unsubscribed

        return unsubscribe

    def emit(self, event: TraderEvent) -> None:
        """Deliver ``event`` to all subscribers (exception-isolated)."""
        for callback in tuple(self._subscribers):
            try:
                callback(event)
            except Exception as exc:
                warnings.warn(f"EventStream: subscriber failed ({exc!r})")

    def __len__(self) -> int:
        return len(self._subscribers)


# ---------------------------------------------------------------------------
# Formatting helpers (module-private)
# ---------------------------------------------------------------------------

def _stamp(ms: int) -> str:
    """UTC ms → ``YYYY-MM-DD HH:MM:SS`` (UTC)."""
    return datetime.fromtimestamp(int(ms) // 1000, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _num(x: float) -> str:
    """Plain grep-able number — no separators, trailing ``.0`` dropped."""
    return format(float(x), ".10g")


def _money(x: float) -> str:
    value = float(x)
    return f"-${_num(-value)}" if value < 0 else f"${_num(value)}"


def _ratio(x: float) -> str:
    return f"{float(x):.2f}"


def _pct(fraction: float) -> str:
    return f"{_num(float(fraction) * 100)}%"


def _duration(ms: int) -> str:
    """Humanize a duration: top two units of d/h/m/s (``2h 15m``, ``30s``)."""
    seconds = max(0, int(ms) // 1000)
    parts = []
    for unit, span in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= span:
            parts.append(f"{seconds // span}{unit}")
            seconds %= span
    return " ".join(parts[:2]) if parts else "0s"


def _opt(label: str, value: float | None, fmt: Callable[[float], str] = _num) -> list[str]:
    """``["label=<fmt(value)>"]`` or ``[]`` when value is None."""
    return [] if value is None else [f"{label}={fmt(value)}"]


# ---------------------------------------------------------------------------
# Per-type line bodies — the "printed detail" column of the table
# ---------------------------------------------------------------------------

def _body_signal(e: SignalEvent) -> str:
    parts = [f"SIGNAL {e.direction} @ {_num(e.entry_price)}", f"sl={_num(e.stop_loss)}"]
    parts += _opt("tp", e.take_profit)
    parts += _opt("size", e.size)
    parts += _opt("risk", e.risk_amount, _money)
    parts += _opt("rr", e.rr, _ratio)
    if e.timeframe:
        parts.append(f"tf={e.timeframe}")
    if e.metadata:
        parts.append(f"meta={e.metadata!r}")
    return " ".join(parts)


def _body_open(e: OpenEvent) -> str:
    parts = [
        f"OPEN #{e.trade_id} {e.direction} filled @ {_num(e.fill_price)}",
        f"size={_num(e.size)}",
        f"margin={_money(e.margin_amount)}",
    ]
    parts += _opt("risk", e.risk_amount, _money)
    parts += _opt("sl", e.stop_loss)
    parts += _opt("next_tp", e.next_tp)
    if e.order_id is not None:
        parts.append(f"order={e.order_id}")
    return " ".join(parts)


def _body_sl_move(e: SlMoveEvent) -> str:
    parts = [f"SL_MOVE #{e.trade_id} sl {_num(e.old_sl)} -> {_num(e.new_sl)} ({e.cause})"]
    parts += _opt("next_tp", e.next_tp)
    return " ".join(parts)


def _body_risk_free(e: RiskFreeEvent) -> str:
    return (
        f"RISK_FREE #{e.trade_id} rr={_ratio(e.rr_level)} touched, "
        f"sl {_num(e.old_sl)} -> {_num(e.new_sl)} (break-even)"
    )


def _body_tp_level(e: TpLevelEvent) -> str:
    parts = [
        f"TP_LEVEL #{e.trade_id} rr={_ratio(e.level)} hit",
        f"closed={_pct(e.fraction_closed)}",
        f"pnl={_money(e.realized_pnl)}",
    ]
    parts += _opt("new_sl", e.new_sl)
    return " ".join(parts)


def _body_close(e: CloseEvent) -> str:
    return (
        f"CLOSE #{e.trade_id} @ {_num(e.exit_price)} reason={e.reason} "
        f"gross={_money(e.gross_pnl)} net={_money(e.net_pnl)} "
        f"r={_ratio(e.pnl_r)} duration={_duration(e.duration_ms)}"
    )


def _body_exit_signal(e: ExitSignalEvent) -> str:
    return f"EXIT_SIGNAL reason={e.reason} action={e.action}"


def _body_daily_loss(e: DailyLossEvent) -> str:
    limit = e.limit if isinstance(e.limit, str) else _money(e.limit)
    line = f"DAILY_LOSS loss={_money(e.loss)} limit={limit} trading halted"
    if e.closed_all:
        line += " (closing all positions)"
    return line


def _body_reconcile(e: ReconcileEvent) -> str:
    def fmt(labels: tuple[str, ...]) -> str:
        return "[" + ", ".join(labels) + "]"

    return (
        f"RECONCILE adopted={fmt(e.adopted)} "
        f"closed_offline={fmt(e.closed_offline)} foreign={fmt(e.foreign)}"
    )


def _body_error(e: ErrorEvent) -> str:
    line = f"ERROR {e.where}: {e.message}"
    if e.will_retry:
        line += " (will retry)"
    if e.details:
        line += f" details={e.details!r}"
    return line


def _body_generic(e: TraderEvent) -> str:
    """Fallback for unknown/future event types: ``TYPE k=v k=v``."""
    parts = [(e.event_type or type(e).__name__).upper()]
    for f in fields(e):
        if f.name in _COMMON_FIELDS:
            continue
        value = getattr(e, f.name)
        if value is None:
            continue
        parts.append(f"{f.name}={value!r}")
    return " ".join(parts)


_BODY_BUILDERS: dict[str, Callable[[Any], str]] = {
    EVENT_SIGNAL: _body_signal,
    EVENT_OPEN: _body_open,
    EVENT_SL_MOVE: _body_sl_move,
    EVENT_RISK_FREE: _body_risk_free,
    EVENT_TP_LEVEL: _body_tp_level,
    EVENT_CLOSE: _body_close,
    EVENT_EXIT_SIGNAL: _body_exit_signal,
    EVENT_DAILY_LOSS: _body_daily_loss,
    EVENT_RECONCILE: _body_reconcile,
    EVENT_ERROR: _body_error,
}


# ---------------------------------------------------------------------------
# TerminalEventPrinter — the one v1.0.0 subscriber
# ---------------------------------------------------------------------------

class TerminalEventPrinter:
    """
    Event-stream subscriber printing one human-readable line per event::

        2026-07-11 14:00:00 [LIVE][BTCUSDT] SIGNAL long @ 64250 sl=63800 ...

    Parameters
    ----------
    event_types : collection of str | None
        ``None`` (default) — print every event type; a collection of
        ``EVENT_*`` constants — print only those (the
        ``TraderConfig.log_event_types`` filter).
    out : callable(str) | None
        Line sink — called once per printed line.  ``None`` (default) →
        built-in ``print``.  Inject for tests or custom routing.
    """

    def __init__(
        self,
        event_types: Iterable[str] | None = None,
        out: Callable[[str], None] | None = None,
    ) -> None:
        self._event_types = None if event_types is None else frozenset(event_types)
        self._out: Callable[[str], None] = print if out is None else out

    def __call__(self, event: TraderEvent) -> None:
        """Subscriber protocol — filter, format, print."""
        if self._event_types is not None and event.event_type not in self._event_types:
            return
        self._out(self.format_event(event))

    def format_event(self, event: TraderEvent) -> str:
        """The full line for ``event`` (public — reusable by backends)."""
        body = _BODY_BUILDERS.get(event.event_type, _body_generic)(event)
        symbol = f"[{event.symbol}]" if event.symbol else ""
        return f"{_stamp(event.time)} [{event.source.upper()}]{symbol} {body}"


def attach_terminal_printer(
    stream: EventStream,
    config: Any,
    *,
    out: Callable[[str], None] | None = None,
) -> Callable[[], None] | None:
    """
    Wire the terminal log onto ``stream`` per ``config`` — the one call
    make.

    ``config`` is any object with ``log_events`` / ``log_event_types``
    (a :class:`~AlgoTradeKit.trader.TraderConfig`).  ``log_events=False`` →
    nothing is attached and ``None`` is returned; otherwise a
    :class:`TerminalEventPrinter` filtered by ``log_event_types`` is
    subscribed and its unsubscribe callable returned.  ``out`` passes through
    to the printer.
    """
    if not config.log_events:
        return None
    printer = TerminalEventPrinter(event_types=config.log_event_types, out=out)
    return stream.subscribe(printer)
