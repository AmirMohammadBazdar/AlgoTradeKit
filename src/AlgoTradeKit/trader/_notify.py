"""
AlgoTradeKit.trader._notify
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Trade events → notifications (v1.2.0): the adapter between a pair's
:class:`~AlgoTradeKit.trader.EventStream` and the notifiers its
``TraderConfig.notifiers`` names — beside the terminal printer — and the
default text of every kind.

Wiring
    ``run_live`` and ``Trader`` call :func:`wire_notifiers` per pair, with a
    small read-only *view* of that pair: its symbol and timeframe, its newest
    price, and its open positions.  ``stream.subscribe(notifier)`` wires one with
    no view (:func:`stream_adapter`).  A notifier's own templates are checked
    first, against their kind's fields, so a mistake raises before any trading:
    «template for 'open' uses {fill} — known: …».
Trading never waits
    Receiving an event costs the trading thread a clock read, the pair's newest
    price and an append to the notifier's inbox.  The notifier's own thread
    formats it, replies, edits and makes the day's summary.
One trade, one thread (E1)
    A trade's *root* is its OPEN message, or its SIGNAL message in a chat that
    does not get ``open``.  Its TP levels, risk-free, SL moves and close reply to
    the root.  A root is forgotten once the trade's close has gone out; a chat
    keeps at most 500.  A root that never went out, or a trade from before the
    session, leaves its replies as plain messages.
SL moves edit (E5)
    In a chat that gets ``sl_move``, a stop that moves edits the root, which then
    reads «SL 63,800 → 64,900 (trailing) · 17:45» above its 🕑 line: from the
    trade's first SL to the current one.  Only the newest waiting edit goes.
    Without a root there, it is a new message without sound.
A trade's figures (Q2.7 b)
    A close counts the whole trade the notifier saw — every TP level and the
    close — so ``{net_pnl}`` and ``{pnl_r}`` are the trade's, not the last slice's.
Start and stop (E2)
    One message per notifier per session, naming the symbols wired to it; the
    stop goes once every one of its pairs has stopped, with their total.
The day (E3)
    At 00:00 in the notifier's zone, per pair: the trades closed that day (won or
    lost by their whole net), the day's net with its TP levels, and the
    positions open now.
Times
    ``{time}`` is when the event was received, by the machine clock (Q2.2 c).
    ``{candle_time}`` is the opening of the candle the event belongs to: an
    OPEN's signal's, any other event's own, rounded down to the pair's
    timeframe (Q2.8 b).

A new event type needs a default here, beside its printer branch and its
``ALL_EVENT_TYPES`` entry.
"""

from __future__ import annotations

import html
import secrets
import threading
import weakref
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import fields
from datetime import date, datetime, timedelta, timezone
from typing import Any, NamedTuple

from ..notify import _template
from ..notify._core import (
    KIND_CLOSE,
    KIND_DAILY_LOSS,
    KIND_DAILY_SUMMARY,
    KIND_ERROR,
    KIND_EXIT_SIGNAL,
    KIND_MESSAGE,
    KIND_OPEN,
    KIND_RECONCILE,
    KIND_RISK_FREE,
    KIND_SIGNAL,
    KIND_SL_MOVE,
    KIND_START,
    KIND_STOP,
    KIND_TEXT,
    KIND_TP_LEVEL,
    SILENT_KINDS,
    Notifier,
    _Destination,
    close_notifiers,
)
from ..strategy._base import _NOTIFY_FIELDS, _check_notify_text
from ._events import (
    EVENT_CLOSE,
    EVENT_DAILY_LOSS,
    EVENT_ERROR,
    EVENT_EXIT_SIGNAL,
    EVENT_MESSAGE,
    EVENT_OPEN,
    EVENT_RECONCILE,
    EVENT_RISK_FREE,
    EVENT_SIGNAL,
    EVENT_SL_MOVE,
    EVENT_START,
    EVENT_STOP,
    EVENT_TP_LEVEL,
    CloseEvent,
    DailyLossEvent,
    ErrorEvent,
    EventStream,
    ExitSignalEvent,
    MessageEvent,
    OpenEvent,
    ReconcileEvent,
    RiskFreeEvent,
    SignalEvent,
    SlMoveEvent,
    StartEvent,
    StopEvent,
    TpLevelEvent,
    TraderEvent,
    _duration,
)

#: The fields every trade-event message has; a strategy's own text may use them too.
COMMON_FIELDS = _NOTIFY_FIELDS

#: Event fields that hold objects: not template fields — a template is data.
_OBJECT_FIELDS = frozenset({"signal", "trade", "exit_signal"})

#: The event class of each kind.
_EVENT_CLASSES: dict[str, type[TraderEvent]] = {
    KIND_SIGNAL: SignalEvent,
    KIND_EXIT_SIGNAL: ExitSignalEvent,
    KIND_OPEN: OpenEvent,
    KIND_SL_MOVE: SlMoveEvent,
    KIND_RISK_FREE: RiskFreeEvent,
    KIND_TP_LEVEL: TpLevelEvent,
    KIND_CLOSE: CloseEvent,
    KIND_DAILY_LOSS: DailyLossEvent,
    KIND_RECONCILE: ReconcileEvent,
    KIND_ERROR: ErrorEvent,
    KIND_MESSAGE: MessageEvent,
    KIND_START: StartEvent,
    KIND_STOP: StopEvent,
}

#: Fields a kind has beyond its event's own.
_EXTRA_FIELDS: dict[str, tuple[str, ...]] = {
    KIND_CLOSE: ("direction", "reason_text", "duration"),
    KIND_STOP: ("symbols",),
}

#: The fields of the day's summary, after the common ones.
_SUMMARY_FIELDS = ("day", "trades", "won", "lost", "net", "open_positions", "open_list")

#: Close reasons in words; any other reason prints as it is.
_REASON_TEXT = {
    "tp": "take profit",
    "sl": "stop loss",
    "rf": "break-even stop",
    "force_close": "exit signal",
    "end_of_data": "end of data",
    "manual": "closed on the venue",
    "tp_rr": "take profit level",
}

#: A chat keeps at most this many trade roots; the oldest go first.
_ROOTS_PER_CHAT = 500

#: What a field with no value prints in a template of your own.
_DASH = "—"

#: A slice smaller than this part of the trade's size leaves nothing open.
_SIZE_TOLERANCE = 1e-9

_ON_STOP_CLOSE_ALL = "close_all"


def fields_of_kind(kind: str) -> tuple[str, ...]:
    """The placeholders a template for *kind* may use, in the order an error lists
    them: the common fields, then the event's own, then its extras."""
    if kind == KIND_DAILY_SUMMARY:
        return (*COMMON_FIELDS, *_SUMMARY_FIELDS)
    own = [
        f.name for f in fields(_EVENT_CLASSES[kind])
        if f.name not in COMMON_FIELDS and f.name not in _OBJECT_FIELDS
    ]
    extra = [name for name in _EXTRA_FIELDS.get(kind, ()) if name not in own]
    return (*COMMON_FIELDS, *own, *extra)


def _check_templates(notifier: Notifier) -> None:
    """Every custom trade-event template of *notifier*, against its kind's fields
    (the ``text`` wrapper was checked when the notifier was created)."""
    for kind, template in notifier._templates.items():
        if kind == KIND_TEXT or template is None:
            continue
        known = fields_of_kind(kind)
        try:
            _template.fields_of(template, known)
        except ValueError as exc:
            said = str(exc)
            if said.startswith("unknown field "):
                field = said[len("unknown field "):].split(" — ", 1)[0]
                raise ValueError(
                    f"template for {kind!r} uses {field} — known: {', '.join(known)}"
                ) from None
            raise ValueError(f"template for {kind!r}: {said}") from None
        except TypeError as exc:
            raise TypeError(f"template for {kind!r}: {exc}") from None


# ---------------------------------------------------------------------------
# What a runner hands over
# ---------------------------------------------------------------------------

class OpenPosition(NamedTuple):
    """One open position, as a notifier reads it: a plain copy."""

    trade_id: Any
    direction: str
    entry_price: float
    size: float
    stop_loss: float | None


class PairView:
    """What a runner tells its notifiers about one pair — read-only.

    :meth:`last_price` and :meth:`candle_anchor` are called on the trading
    thread, so they must be cheap and never raise; :meth:`open_positions` is
    called only from the notifier's own threads."""

    symbol = ""
    timeframe = ""
    mode = ""                     # "paper" (run_live) / "live" (Trader)
    timeframe_ms: int | None = None

    def last_price(self) -> float | None:
        """The newest price the runner has: the last closed candle's close, or
        the forming one's in a realtime mode."""
        return None

    def candle_anchor(self) -> int | None:
        """The opening of a candle of this pair (UTC ms), to round times from."""
        return None

    def open_positions(self) -> list[OpenPosition]:
        """The pair's open positions now."""
        return []


class NotifySession:
    """One run of ``run_live`` or ``Trader``, as its notifiers announce it: a new
    random id, its mode, and the adapters wired for it."""

    def __init__(self, mode: str, *, on_stop: str | None = None) -> None:
        self.id = secrets.token_hex(4)
        self.mode = mode
        self.on_stop = on_stop
        self.adapters: list[_Adapter] = []

    @property
    def notifiers(self) -> list[Notifier]:
        """Every notifier wired for this session, each once, in wiring order."""
        return list(dict.fromkeys(adapter.notifier for adapter in self.adapters))

    def adapters_of(self, notifier: Notifier) -> list[_Adapter]:
        return [adapter for adapter in self.adapters if adapter.notifier is notifier]

    def close(self, timeout: float = 10.0) -> None:
        """Deliver what the notifiers hold — the stop message last of all — sharing
        one *timeout*."""
        close_notifiers(self.notifiers, timeout)


def wire_notifiers(
    stream: EventStream, config: Any, view: PairView, session: NotifySession
) -> list[_Adapter]:
    """Subscribe one adapter per notifier of ``config.notifiers`` to *stream*.

    Every notifier's templates are checked before any is subscribed, so a
    mistake raises before trading starts and leaves nothing half-wired."""
    notifiers = list(getattr(config, "notifiers", None) or ())
    for notifier in notifiers:
        _check_templates(notifier)
    adapters = []
    for notifier in notifiers:
        adapter = _Adapter(notifier, view, session)
        session.adapters.append(adapter)
        stream.subscribe(adapter)
        adapters.append(adapter)
    return adapters


def stream_adapter(notifier: Notifier) -> _Adapter:
    """``stream.subscribe(notifier)``: an adapter with no view — no prices, no
    positions, no daily summary."""
    _check_templates(notifier)
    return _Adapter(notifier, None, None)


def message_hook(
    stream: EventStream, *, symbol: str, source: str, candle_time: Callable[[], int]
) -> Callable[[str, bool], None]:
    """What ``BaseStrategy.notify`` calls on a newly closed live candle: a
    ``MESSAGE`` event, or an ``ERROR`` event when the text has a mistake — a
    notification never costs a live candle."""

    def hook(text: str, silent: bool) -> None:
        when = candle_time()
        try:
            _check_notify_text(text)
        except (TypeError, ValueError) as exc:
            stream.emit(ErrorEvent(
                time=when, symbol=symbol, source=source, where="notify", message=str(exc),
            ))
            return
        stream.emit(MessageEvent(
            time=when, symbol=symbol, source=source, text=text, silent=silent,
        ))

    return hook


def mute_hook(text: str, silent: bool) -> None:
    """``BaseStrategy.notify`` on a forming-candle evaluation, or in the Trader's
    display copy of the strategy: nothing at all — the committed candle says it."""
    del text, silent


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------

class _Missing:
    """A field with no value: «—», whatever its format spec."""

    __slots__ = ()

    def __format__(self, spec: str) -> str:
        return _DASH

    def __str__(self) -> str:
        return _DASH


_MISSING = _Missing()


def _num(value: float | None) -> _template.Number | None:
    return None if value is None else _template.Number(value)


def _words(value: Any) -> str | None:
    """A tuple, a dict or a flag, for a person: «a, b», «key=value, …», «yes» / «no»."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, dict):
        return ", ".join(f"{key}={item}" for key, item in value.items()) or None
    if isinstance(value, (tuple, list)):
        return ", ".join(str(item) for item in value) or None
    return str(value)


def _join(*parts: str | None) -> str | None:
    """Parts of a line, « · » between them; the missing ones left out."""
    kept = [part for part in parts if part]
    return " · ".join(kept) if kept else None


def _words_of(*parts: str | None) -> str | None:
    kept = [part for part in parts if part]
    return " ".join(kept) if kept else None


def _label(label: str, value: str | None) -> str | None:
    return f"{label} {value}" if value else None


def _side(direction: Any) -> str | None:
    if direction == "long":
        return "🟢 LONG"
    if direction == "short":
        return "🔴 SHORT"
    return str(direction).upper() if direction else None


class _Text:
    """The values of one message, as a default text shows them: formatted,
    escaped for rich text, ``None`` when missing."""

    def __init__(self, values: dict[str, Any], rich: bool) -> None:
        self.values = values
        self.rich = rich

    def __getitem__(self, name: str) -> str | None:
        value = self.values.get(name)
        if value is None:
            return None
        text = format(value, "")
        if self.rich and not isinstance(value, _template.Markup):
            text = html.escape(text, quote=False)
        return text or None

    def raw(self, name: str) -> Any:
        return self.values.get(name)

    def clock(self, *extra: str | None) -> str | None:
        return _join(_label("🕑", self["time"]), self["source"], *extra)


def _with_sl_line(text: str, line: str) -> str:
    """A root's text with its SL line, on a line of its own above the 🕑 line."""
    lines = text.split("\n")
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].startswith("🕑"):
            return "\n".join([*lines[:index], line, *lines[index:]])
    return f"{text}\n{line}"


# ---------------------------------------------------------------------------
# The default texts — QUESTIONS.md Part 2, word for word, with Q2.6 (c)
# ---------------------------------------------------------------------------

def _default_signal(t: _Text) -> list[str | None]:
    return [
        _join(_words_of(_side(t.raw("direction")), t["symbol"]), t["timeframe"]),
        _join(_label("Entry", t["entry_price"]), _label("SL", t["stop_loss"]),
              _label("TP", t["take_profit"])),
        _label("RR", t["rr"]),
        t.clock(),
    ]


def _default_open(t: _Text) -> list[str | None]:
    direction = t["direction"]
    return [
        _join(f"✅ OPENED #{t['trade_id']}",
              _words_of(direction.upper() if direction else None, t["symbol"],
                        _label("@", t["fill_price"]))),
        _join(_label("Size", t["size"]), _label("Risk", t["risk_amount"]),
              _label("SL", t["stop_loss"]), _label("Next TP", t["next_tp"])),
        t.clock(_label("order", t["order_id"])),
    ]


def _default_sl_move(t: _Text) -> list[str | None]:
    return [
        _join(f"↗️ SL MOVED #{t['trade_id']}", t["symbol"]),
        _words_of(t["old_sl"], "→", t["new_sl"], f"({t['cause']})" if t["cause"] else None),
        t.clock(),
    ]


def _default_risk_free(t: _Text) -> list[str | None]:
    return [
        _join(f"🛡 RISK-FREE #{t['trade_id']}", t["symbol"]),
        _join(f"{t['rr_level']} R reached" if t["rr_level"] else None,
              _words_of("SL", t["old_sl"], "→", t["new_sl"], "(break-even)")),
        t.clock(),
    ]


def _default_tp_level(t: _Text) -> list[str | None]:
    return [
        _join(_words_of("🎯 TP", _label(t["level"], "R") if t["level"] else None,
                        f"#{t['trade_id']}"), t["symbol"]),
        _join(_label("Closed", t["fraction_closed"]), t["realized_pnl"],
              _label("new SL", t["new_sl"])),
        t.clock(),
    ]


def _default_close(t: _Text) -> list[str | None]:
    direction = t["direction"]
    head = _join(f"🏁 CLOSED #{t['trade_id']}",
                 _words_of(direction.upper() if direction else None, t["symbol"],
                           _label("@", t["exit_price"])))
    if t["reason_text"]:
        head = f"{head} — {t['reason_text']}"
    return [
        head,
        _join(_words_of(_label("Net", t["net_pnl"]),
                        f"({t['pnl_r']} R)" if t["pnl_r"] else None),
              _label("held", t["duration"])),
        t.clock(),
    ]


def _default_exit_signal(t: _Text) -> list[str | None]:
    if t.raw("action") == "force_close":
        outcome = "position closed"
    else:
        outcome = "no action (force_close_on_exit_signal is off)"
    return [
        _join("🚪 EXIT SIGNAL", t["symbol"]),
        _words_of(t["reason"], "→", outcome),
        t.clock(),
    ]


def _default_daily_loss(t: _Text) -> list[str | None]:
    return [
        _join("⛔ DAILY LOSS LIMIT", t["symbol"]),
        _join(_words_of(_label("Loss", t["loss"]), _label("of", t["limit"])),
              "no new entries until the next UTC day",
              "closing all positions" if t.raw("_closed_all") else None),
        t.clock(),
    ]


def _default_reconcile(t: _Text) -> list[str | None]:
    counts = {name: len(t.raw(f"_{name}") or ()) for name in
              ("adopted", "closed_offline", "foreign")}
    return [
        _join("🔄 RESTART RECONCILED", t["symbol"]),
        f"Adopted {counts['adopted']} · closed while offline {counts['closed_offline']} · "
        f"foreign {counts['foreign']}",
        t.clock(),
    ]


def _default_error(t: _Text) -> list[str | None]:
    detail = _words_of(f"{t['where']}:" if t["where"] else None, t["message"])
    if detail and t.raw("_will_retry"):
        detail += " (will retry)"
    return [_join("⚠️ ERROR", t["symbol"]), detail, t.clock()]


def _default_message(t: _Text) -> list[str | None]:
    return [_join(_words_of("💬", t["symbol"]), t["timeframe"]), t["text"], t.clock()]


def _mode_word(mode: Any) -> str:
    return "Paper" if mode == "paper" else "Live" if mode == "live" else str(mode).title()


def _default_start(t: _Text) -> list[str | None]:
    return [_join(f"▶️ {_mode_word(t.raw('mode'))} trading started", t["symbols"]), t.clock()]


def _default_stop(t: _Text) -> list[str | None]:
    crashed = bool(t.raw("error"))
    head = f"⏹ {_mode_word(t.raw('mode'))} trading stopped" + (" by an error" if crashed else "")
    return [
        _join(head, t["symbols"], t["error"] if crashed else t["reason"], t["_left"]),
        t.clock(),
    ]


def _default_daily_summary(t: _Text) -> list[str | None]:
    head = _join(_label("📊 DAY", t["day"]), t["symbol"])
    realized = bool(t.raw("_realized"))
    count = t.raw("open_positions") or 0
    if not realized and not count:
        return [f"{head} — no trades · no open positions", t.clock()]
    trades = (
        f"Trades {t['trades']} · won {t['won']} · lost {t['lost']} · net {t['net']}"
        if realized else "No trades"
    )
    opened = f"Open now: {count} ({t['open_list']})" if count else "No open positions"
    return [head, trades, opened, t.clock()]


_DEFAULTS: dict[str, Callable[[_Text], list[str | None]]] = {
    KIND_SIGNAL: _default_signal,
    KIND_OPEN: _default_open,
    KIND_SL_MOVE: _default_sl_move,
    KIND_RISK_FREE: _default_risk_free,
    KIND_TP_LEVEL: _default_tp_level,
    KIND_CLOSE: _default_close,
    KIND_EXIT_SIGNAL: _default_exit_signal,
    KIND_DAILY_LOSS: _default_daily_loss,
    KIND_RECONCILE: _default_reconcile,
    KIND_ERROR: _default_error,
    KIND_MESSAGE: _default_message,
    KIND_START: _default_start,
    KIND_STOP: _default_stop,
    KIND_DAILY_SUMMARY: _default_daily_summary,
}


def _left_open(mode: str, count: int | None, close_all: bool) -> str | None:
    """What a stop says is left open."""
    if count is None:
        return None
    if mode == "paper":
        if count == 0:
            return "no paper positions open"
        return f"{count} paper position{'' if count == 1 else 's'} open"
    if count == 0:
        return "all positions closed" if close_all else "no positions open"
    if count == 1:
        return "1 position left open with its venue SL/TP"
    return f"{count} positions left open with their venue SL/TP"


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------

class _Hub:
    """What every adapter of one notifier shares, on that notifier's own thread:
    the trade roots of each chat, and the sessions announced."""

    def __init__(self) -> None:
        self.roots: dict[str, OrderedDict[tuple, tuple[Any, str]]] = {}
        self.started: set[str] = set()
        self.stops: dict[str, list[StopEvent]] = {}
        self.expected: dict[str, int] = {}          # a bare stream: the pairs to stop
        self.symbols: dict[str, list[str]] = {}     # a bare stream: what its start named


_hubs: weakref.WeakKeyDictionary[Notifier, _Hub] = weakref.WeakKeyDictionary()
_hubs_lock = threading.Lock()


def _hub_of(notifier: Notifier) -> _Hub:
    with _hubs_lock:
        hub = _hubs.get(notifier)
        if hub is None:
            hub = _hubs[notifier] = _Hub()
        return hub


class _Trade:
    """What an adapter remembers of one open trade."""

    __slots__ = ("direction", "first_sl", "size", "signal_time", "net", "gross", "risk", "closed")

    def __init__(self) -> None:
        self.direction: str | None = None
        self.first_sl: float | None = None
        self.size: float | None = None
        self.signal_time: int | None = None
        self.net = 0.0        # the slices seen so far: TP levels, partial closes
        self.gross = 0.0
        self.risk = 0.0
        self.closed = 0.0


class _Day:
    """One local day of one pair, as the summary counts it."""

    __slots__ = ("trades", "won", "lost", "net", "realized")

    def __init__(self) -> None:
        self.trades = self.won = self.lost = 0
        self.net = 0.0
        self.realized = False


class _Adapter:
    """One notifier on one pair's stream (``view`` is ``None`` on a bare stream)."""

    def __init__(
        self, notifier: Notifier, view: PairView | None, session: NotifySession | None
    ) -> None:
        self.notifier = notifier
        self.view = view
        self.session = session
        self.hub = _hub_of(notifier)
        self._trades: dict[Any, _Trade] = {}
        self._days: dict[date, _Day] = {}
        self._cancel_job: Callable[[], None] | None = None

    # ------------------------------------------------------------------
    # On the trading thread: three reads and an append
    # ------------------------------------------------------------------

    def __call__(self, event: TraderEvent) -> None:
        notifier = self.notifier
        received = notifier._clock.time()
        view = self.view
        price = view.last_price() if view is not None else None
        anchor = view.candle_anchor() if view is not None else None
        notifier._post(lambda: self._handle(event, received, price, anchor))

    # ------------------------------------------------------------------
    # On the notifier's thread
    # ------------------------------------------------------------------

    def _handle(self, event: TraderEvent, received: float, price: Any, anchor: Any) -> None:
        handler = {
            EVENT_SIGNAL: self._on_signal,
            EVENT_OPEN: self._on_open,
            EVENT_SL_MOVE: self._on_sl_move,
            EVENT_RISK_FREE: self._on_risk_free,
            EVENT_TP_LEVEL: self._on_tp_level,
            EVENT_CLOSE: self._on_close,
            EVENT_EXIT_SIGNAL: self._on_plain,
            EVENT_DAILY_LOSS: self._on_plain,
            EVENT_RECONCILE: self._on_plain,
            EVENT_ERROR: self._on_plain,
            EVENT_MESSAGE: self._on_message,
            EVENT_START: self._on_start,
            EVENT_STOP: self._on_stop,
        }.get(event.event_type)
        if handler is not None:
            handler(event, received, self._common(event, received, price, anchor))

    def _common(
        self, event: TraderEvent, received: float, price: Any, anchor: Any
    ) -> dict[str, Any]:
        zone = self.notifier._zone
        view = self.view
        timeframe = getattr(event, "timeframe", "") or (view.timeframe if view else "")
        values: dict[str, Any] = {
            "time": _template.Moment(received, zone),
            "date": _template.Day(received, zone),
            "candle_time": _template.Moment(self._candle_ms(event, anchor) / 1000, zone),
            "symbol": event.symbol or None,
            "source": event.source.upper(),
            "timeframe": timeframe or None,
            "price": _num(price),
        }
        values["header"] = self._header(values)
        return values

    def _candle_ms(self, event: TraderEvent, anchor: Any) -> int:
        """The opening of the candle *event* belongs to (Q2.8 b)."""
        if isinstance(event, OpenEvent) and event.signal_time is not None:
            return int(event.signal_time)
        stamp = int(event.time)
        tf_ms = self.view.timeframe_ms if self.view is not None else None
        if not tf_ms or anchor is None:
            return stamp
        anchor = int(anchor)
        return anchor + ((stamp - anchor) // tf_ms) * tf_ms

    @staticmethod
    def _header(values: dict[str, Any]) -> str:
        price = values["price"]
        return (f"🕑 {values['time']} · {values['symbol'] or _DASH} "
                f"{_DASH if price is None else format(price, '')}")

    # ---- templates and deliveries ------------------------------------

    def _sends(self, kind: str) -> bool:
        custom, template = self.notifier._template_for(kind)
        return not (custom and template is None)

    def _gets(self, dest: _Destination, kind: str, symbol: str | None) -> bool:
        return dest.accepts(kind, symbol) and self._sends(kind)

    def _body(self, kind: str, values: dict[str, Any]) -> str | None:
        """The text of a message: the notifier's own template, or the default."""
        custom, template = self.notifier._template_for(kind)
        rich = self.notifier._rich
        if custom:
            if template is None:
                return None
            filled = {
                name: _MISSING if values.get(name) is None else values[name]
                for name in fields_of_kind(kind)
            }
            return _template.render(template, filled, escape=rich)
        lines = [line for line in _DEFAULTS[kind](_Text(values, rich)) if line]
        if rich and lines:
            lines[0] = f"<b>{lines[0]}</b>"
        return "\n".join(lines)

    def _queue(
        self,
        dest: _Destination,
        kind: str,
        symbol: str | None,
        body: str,
        received: float,
        *,
        silent: bool = False,
        reply: Callable[[], Any] | None = None,
        edit: Callable[[], tuple[Any, str] | None] | None = None,
        coalesce: Any = None,
        on_done: Callable[[Any, str | None], None] | None = None,
    ) -> None:
        self.notifier._queue_event(
            dest, kind, symbol, body,
            queued_at=received,
            silent=dest.silent or kind in SILENT_KINDS or silent,
            reply=reply, edit=edit, coalesce=coalesce, on_done=on_done,
        )

    def _send(self, kind: str, event: TraderEvent, received: float,
              values: dict[str, Any], *, silent: bool = False) -> None:
        """A message that replies to nothing and is the root of nothing."""
        symbol = event.symbol or None
        if not self._sends(kind):
            return
        dests = self.notifier._recipients(kind, symbol)
        if not dests:
            return
        body = self._body(kind, values)
        if body is None:
            return
        for dest in dests:
            self._queue(dest, kind, symbol, body, received, silent=silent)

    # ---- roots ---------------------------------------------------------

    def _root(self, dest: _Destination, symbol: str | None, trade_id: Any,
              trade: _Trade | None) -> tuple[Any, str] | None:
        """``(message id, text)`` of the trade's root in *dest*, if it went out."""
        roots = self.hub.roots.get(dest.name)
        if not roots:
            return None
        if self._gets(dest, KIND_OPEN, symbol):
            return roots.get(("open", self, trade_id))
        if trade is not None and trade.signal_time is not None and trade.direction:
            return roots.get(("signal", self, trade.signal_time, trade.direction))
        return None

    def _keep_root(self, dest: _Destination, key: tuple) -> Callable[[Any, str | None], None]:
        def keep(message_id: Any, text: str | None) -> None:
            if message_id is None or text is None:
                return   # never went out: its replies go as plain messages
            roots = self.hub.roots.setdefault(dest.name, OrderedDict())
            roots[key] = (message_id, text)
            roots.move_to_end(key)
            while len(roots) > _ROOTS_PER_CHAT:
                roots.popitem(last=False)
        return keep

    def _forget_root(self, dest: _Destination, trade_id: Any,
                     trade: _Trade) -> Callable[[Any, str | None], None]:
        def forget(message_id: Any, text: str | None) -> None:
            del message_id, text
            roots = self.hub.roots.get(dest.name)
            if roots:
                roots.pop(("open", self, trade_id), None)
                roots.pop(("signal", self, trade.signal_time, trade.direction), None)
        return forget

    def _reply_for(self, dest: _Destination, symbol: str | None, trade_id: Any,
                   trade: _Trade | None) -> Callable[[], Any]:
        def reply() -> Any:
            root = self._root(dest, symbol, trade_id, trade)
            return None if root is None else root[0]
        return reply

    def _trade(self, trade_id: Any) -> _Trade:
        trade = self._trades.get(trade_id)
        if trade is None:   # opened before the session: only what is seen from now on
            trade = self._trades[trade_id] = _Trade()
        return trade

    def _day(self, received: float) -> _Day:
        zone = self.notifier._zone or timezone.utc
        day = datetime.fromtimestamp(received, tz=zone).date()
        stats = self._days.get(day)
        if stats is None:
            stats = self._days[day] = _Day()
        return stats

    # ---- the kinds -----------------------------------------------------

    def _on_signal(self, event: SignalEvent, received: float, values: dict[str, Any]) -> None:
        values.update(
            direction=event.direction, entry_price=_num(event.entry_price),
            stop_loss=_num(event.stop_loss), take_profit=_num(event.take_profit),
            size=None if event.size is None else _template.Size(event.size),
            risk_amount=None if event.risk_amount is None else _template.Money(event.risk_amount),
            rr=None if event.rr is None else _template.Ratio(event.rr),
            metadata=_words(event.metadata),
        )
        symbol = event.symbol or None
        if not self._sends(KIND_SIGNAL):
            return
        dests = self.notifier._recipients(KIND_SIGNAL, symbol)
        body = self._body(KIND_SIGNAL, values) if dests else None
        if body is None:
            return
        # The OPEN this signal leads to names it by its timestamp (signal_time).
        stamp = int(event.signal.timestamp) if event.signal is not None else int(event.time)
        for dest in dests:
            on_done = None
            if not self._gets(dest, KIND_OPEN, symbol):
                on_done = self._keep_root(dest, ("signal", self, stamp, event.direction))
            self._queue(dest, KIND_SIGNAL, symbol, body, received, on_done=on_done)

    def _on_open(self, event: OpenEvent, received: float, values: dict[str, Any]) -> None:
        trade = self._trades[event.trade_id] = _Trade()
        trade.direction = event.direction
        trade.first_sl = event.stop_loss
        trade.size = event.size
        trade.signal_time = event.signal_time
        zone = self.notifier._zone
        values.update(
            trade_id=event.trade_id, direction=event.direction,
            fill_price=_num(event.fill_price), size=_template.Size(event.size),
            margin_amount=_template.Money(event.margin_amount),
            risk_amount=None if event.risk_amount is None else _template.Money(event.risk_amount),
            stop_loss=_num(event.stop_loss), next_tp=_num(event.next_tp),
            order_id=event.order_id,
            signal_time=(None if event.signal_time is None
                         else _template.Moment(event.signal_time / 1000, zone)),
        )
        symbol = event.symbol or None
        if not self._sends(KIND_OPEN):
            return
        dests = self.notifier._recipients(KIND_OPEN, symbol)
        body = self._body(KIND_OPEN, values) if dests else None
        if body is None:
            return
        for dest in dests:
            self._queue(dest, KIND_OPEN, symbol, body, received,
                        on_done=self._keep_root(dest, ("open", self, event.trade_id)))

    def _on_sl_move(self, event: SlMoveEvent, received: float, values: dict[str, Any]) -> None:
        trade = self._trade(event.trade_id)
        values.update(
            trade_id=event.trade_id, old_sl=_num(event.old_sl), new_sl=_num(event.new_sl),
            cause=event.cause, next_tp=_num(event.next_tp),
        )
        symbol = event.symbol or None
        if not self._sends(KIND_SL_MOVE):
            return
        dests = self.notifier._recipients(KIND_SL_MOVE, symbol)
        body = self._body(KIND_SL_MOVE, values) if dests else None
        if body is None:
            return
        line = None
        if trade.first_sl is not None:
            cause = event.cause
            if self.notifier._rich:
                cause = html.escape(cause, quote=False)
            moved = format(_template.Moment(received, self.notifier._zone), "%H:%M")
            line = (f"SL {_template.Number(trade.first_sl)} → {_template.Number(event.new_sl)} "
                    f"({cause}) · {moved}")
        for dest in dests:
            edit = None
            if line is not None:
                edit = self._edit_for(dest, symbol, event.trade_id, trade, line)
            self._queue(dest, KIND_SL_MOVE, symbol, body, received,
                        reply=self._reply_for(dest, symbol, event.trade_id, trade),
                        edit=edit, coalesce=("sl_move", self, event.trade_id))

    def _edit_for(self, dest: _Destination, symbol: str | None, trade_id: Any,
                  trade: _Trade, line: str) -> Callable[[], tuple[Any, str] | None]:
        def edit() -> tuple[Any, str] | None:
            root = self._root(dest, symbol, trade_id, trade)
            if root is None:
                return None   # no root there: the move goes as a new silent message
            message_id, text = root
            return message_id, _with_sl_line(text, line)
        return edit

    def _on_risk_free(self, event: RiskFreeEvent, received: float,
                      values: dict[str, Any]) -> None:
        values.update(
            trade_id=event.trade_id, rr_level=_template.Ratio(event.rr_level),
            old_sl=_num(event.old_sl), new_sl=_num(event.new_sl),
        )
        self._send_reply(KIND_RISK_FREE, event, received, values, self._trade(event.trade_id))

    def _on_tp_level(self, event: TpLevelEvent, received: float, values: dict[str, Any]) -> None:
        trade = self._trade(event.trade_id)
        trade.net += event.realized_pnl
        piece = event.trade
        if piece is not None:
            trade.gross += piece.gross_pnl
            trade.risk += piece.risk_amount
            trade.closed += piece.size
        day = self._day(received)
        day.net += event.realized_pnl
        day.realized = True
        values.update(
            trade_id=event.trade_id, level=_template.Ratio(event.level),
            fraction_closed=_template.Percent(event.fraction_closed),
            realized_pnl=_template.Pnl(event.realized_pnl), new_sl=_num(event.new_sl),
        )
        self._send_reply(KIND_TP_LEVEL, event, received, values, trade)

    def _on_close(self, event: CloseEvent, received: float, values: dict[str, Any]) -> None:
        trade = self._trades.get(event.trade_id) or _Trade()
        piece = event.trade
        net = trade.net + event.net_pnl
        gross = trade.gross + event.gross_pnl
        risk = trade.risk + (piece.risk_amount if piece is not None else 0.0)
        closed = trade.closed + (piece.size if piece is not None else 0.0)
        pnl_r = net / risk if piece is not None and risk > 0 else event.pnl_r
        partial = (
            trade.size is not None and piece is not None
            and trade.size - closed > _SIZE_TOLERANCE * max(1.0, abs(trade.size))
        )
        day = self._day(received)
        day.net += event.net_pnl
        day.realized = True
        if partial:   # the venue reduced the position: the trade goes on
            trade.net, trade.gross, trade.risk, trade.closed = net, gross, risk, closed
        else:
            self._trades.pop(event.trade_id, None)
            day.trades += 1
            if net > 0:
                day.won += 1
            else:
                day.lost += 1
        direction = trade.direction or (piece.direction if piece is not None else None)
        values.update(
            trade_id=event.trade_id, exit_price=_num(event.exit_price), reason=event.reason,
            gross_pnl=_template.Pnl(gross), net_pnl=_template.Pnl(net),
            pnl_r=_template.SignedRatio(pnl_r), duration_ms=event.duration_ms,
            direction=direction, reason_text=_REASON_TEXT.get(event.reason, event.reason),
            duration=_duration(event.duration_ms),
        )
        if trade.direction is None:
            trade.direction = direction
        self._send_reply(KIND_CLOSE, event, received, values, trade, final=not partial)

    def _send_reply(self, kind: str, event: Any, received: float, values: dict[str, Any],
                    trade: _Trade, *, final: bool = False) -> None:
        """A message of a trade: a reply to its root; the close forgets the root."""
        symbol = event.symbol or None
        if not self._sends(kind):
            return
        dests = self.notifier._recipients(kind, symbol)
        body = self._body(kind, values) if dests else None
        if body is None:
            return
        for dest in dests:
            self._queue(
                dest, kind, symbol, body, received,
                reply=self._reply_for(dest, symbol, event.trade_id, trade),
                on_done=self._forget_root(dest, event.trade_id, trade) if final else None,
            )

    def _on_plain(self, event: Any, received: float, values: dict[str, Any]) -> None:
        kind = event.event_type
        if kind == KIND_EXIT_SIGNAL:
            values.update(reason=event.reason, action=event.action)
        elif kind == KIND_DAILY_LOSS:
            limit = event.limit
            values.update(
                limit=limit if isinstance(limit, str) else _template.Money(limit),
                loss=_template.Money(event.loss), closed_all=_words(event.closed_all),
                _closed_all=event.closed_all,
            )
        elif kind == KIND_RECONCILE:
            values.update(
                adopted=_words(event.adopted), closed_offline=_words(event.closed_offline),
                foreign=_words(event.foreign), _adopted=event.adopted,
                _closed_offline=event.closed_offline, _foreign=event.foreign,
            )
        elif kind == KIND_ERROR:
            values.update(
                where=event.where, message=event.message, will_retry=_words(event.will_retry),
                details=_words(event.details), _will_retry=event.will_retry,
            )
        self._send(kind, event, received, values)

    def _on_message(self, event: MessageEvent, received: float, values: dict[str, Any]) -> None:
        try:   # the hook checked it already; this is the notifier's own guard
            _template.fields_of(event.text, COMMON_FIELDS)
        except (TypeError, ValueError) as exc:
            self.notifier._say(f"a strategy's message was not sent ({exc})")
            return
        filled = {name: _MISSING if values.get(name) is None else values[name]
                  for name in COMMON_FIELDS}
        text = _template.render(event.text, filled, escape=self.notifier._rich)
        values.update(text=_template.Markup(text), silent=_words(event.silent))
        self._send(KIND_MESSAGE, event, received, values, silent=event.silent)

    # ---- the session ---------------------------------------------------

    def _session_symbols(self, event: StartEvent | StopEvent) -> list[str]:
        """The symbols wired to this notifier in the session; on a bare stream, the
        ones its start named."""
        if self.session is not None:
            adapters = self.session.adapters_of(self.notifier)
            return list(dict.fromkeys(
                adapter.view.symbol for adapter in adapters if adapter.view is not None
            ))
        if isinstance(event, StartEvent):
            return list(dict.fromkeys(event.symbols))
        return self.hub.symbols.get(event.session_id, [])

    def _on_start(self, event: StartEvent, received: float, values: dict[str, Any]) -> None:
        self._days.clear()   # the day counts from the later of 00:00 and the start
        if self.view is not None and self.session is not None and self._cancel_job is None:
            self._cancel_job = self.notifier.add_daily_job(self._at_midnight)
        hub = self.hub
        session_id = event.session_id
        if self.session is None:   # a bare stream: one adapter sees every pair
            hub.expected.setdefault(session_id, max(1, len(event.symbols)))
            hub.symbols.setdefault(session_id, list(dict.fromkeys(event.symbols)))
        if session_id in hub.started:
            return   # one start message per notifier per session
        hub.started.add(session_id)
        values.update(mode=event.mode, symbols=_words(self._session_symbols(event)),
                      session_id=session_id)
        self._send(KIND_START, event, received, values)

    def _on_stop(self, event: StopEvent, received: float, values: dict[str, Any]) -> None:
        if self._cancel_job is not None:
            self._cancel_job()
            self._cancel_job = None
        hub = self.hub
        session_id = event.session_id
        stops = hub.stops.setdefault(session_id, [])
        stops.append(event)
        if self.session is not None:
            expected = len(self.session.adapters_of(self.notifier))
        else:
            expected = hub.expected.get(session_id, 1)
        if len(stops) < expected:
            return   # the stop goes once every pair of this notifier has stopped
        del hub.stops[session_id]
        hub.started.discard(session_id)
        hub.expected.pop(session_id, None)
        counts = [stop.open_positions for stop in stops if stop.open_positions is not None]
        total = sum(counts) if counts else None
        error = next((stop.error for stop in stops if stop.error), None)
        close_all = self.session is not None and self.session.on_stop == _ON_STOP_CLOSE_ALL
        values.update(
            mode=event.mode, reason=stops[0].reason, open_positions=total, error=error,
            session_id=session_id, symbols=_words(self._session_symbols(event)),
            _left=_left_open(event.mode, total, close_all),
        )
        hub.symbols.pop(session_id, None)
        self._send(KIND_STOP, event, received, values)

    # ---- the day -------------------------------------------------------

    def _at_midnight(self) -> None:
        """The daily job, on the notifier's job thread: the summary is made on the
        notifier's own thread, like every message."""
        self.notifier._post(self._summarize)

    def _summarize(self) -> None:
        view = self.view
        if view is None:
            return
        notifier = self.notifier
        zone = notifier._zone
        now = notifier._clock.time()
        day = datetime.fromtimestamp(now, tz=zone or timezone.utc).date() - timedelta(days=1)
        stats = self._days.pop(day, None) or _Day()
        for older in [past for past in self._days if past < day]:
            del self._days[older]
        positions = view.open_positions()
        source = "SIM" if view.mode == "paper" else "LIVE"
        anchor = view.candle_anchor()
        candle = int(now * 1000)
        if view.timeframe_ms and anchor is not None:
            candle = anchor + ((candle - anchor) // view.timeframe_ms) * view.timeframe_ms
        noon = datetime(day.year, day.month, day.day, 12, tzinfo=zone or timezone.utc)
        values: dict[str, Any] = {
            "time": _template.Moment(now, zone),
            "date": _template.Day(now, zone),
            "candle_time": _template.Moment(candle / 1000, zone),
            "symbol": view.symbol or None,
            "source": source,
            "timeframe": view.timeframe or None,
            "price": _num(view.last_price()),
            "day": _template.Day(noon.timestamp(), zone),
            "trades": stats.trades,
            "won": stats.won,
            "lost": stats.lost,
            "net": _template.Pnl(stats.net),
            "open_positions": len(positions),
            "open_list": ", ".join(
                f"#{position.trade_id} {str(position.direction).upper()} @ "
                f"{_template.Number(position.entry_price)}"
                for position in positions
            ) or None,
            "_realized": stats.realized,
        }
        values["header"] = self._header(values)
        symbol = view.symbol or None
        if not self._sends(KIND_DAILY_SUMMARY):
            return
        dests = notifier._recipients(KIND_DAILY_SUMMARY, symbol)
        body = self._body(KIND_DAILY_SUMMARY, values) if dests else None
        if body is None:
            return
        for dest in dests:
            self._queue(dest, KIND_DAILY_SUMMARY, symbol, body, now)
