"""
AlgoTradeKit.notify._core
~~~~~~~~~~~~~~~~~~~~~~~~~
The backend-neutral core of the notifications module (v1.2.0).

Nothing here knows about Telegram.  A backend (``TelegramNotifier``) subclasses
:class:`Notifier`, declares its limits, and turns one part of one message into one
request (:meth:`Notifier._deliver`).  The core owns the rest:

Kinds
    Every message has one: ``send_text`` sends :data:`KIND_TEXT`, and the trade
    events of ``AlgoTradeKit.trader`` send theirs, named as the events are
    (:data:`KINDS`).  A destination whose ``events`` is ``None`` gets every kind
    except ``sl_move`` — a trailing stop that moves on 70 candles would drown a
    chat.
Templates
    ``templates={kind: text | None}`` replaces a kind's default text; ``None``
    means that kind is never sent.  The ``text`` kind wraps every custom text,
    which its ``{text}`` holds.  The trade-event kinds are checked when the
    notifier is wired into trading, which knows their fields.
Destinations
    Named by a dict's keys; a chat given on its own is named by its address as text.
    Each has ``events`` (a set of kinds, or ``None``) and ``symbols`` (a set, or
    ``None`` for all).  A symbol filter keeps out *other* symbols: a message about no
    symbol passes it.  ``to=`` names destinations and overrides both filters.
The queue
    One daemon thread per notifier, started on first use.  Messages to one
    destination go out in the order they were queued, paced by the backend's
    limits.  At most 1,000 wait; past that the oldest are dropped, and one line says
    how many.  A text too long for one message is split at the last line break
    before the limit, with any open tags closed and opened again around the split.
An outage
    A network error makes the whole service unreachable.  Only the oldest waiting
    message is tried, again after 1, 5 and 15 s and then every 15 s, and the rest
    wait behind it for as long as the outage lasts.  A server error (5xx) does the
    same for its own destination only.  A message delivered a minute or more after
    it was queued starts with «⏱ 11 min late».  A call that waits (``wait=True``)
    gives up at the first failed try 21 s or more after the call, and its message
    is withdrawn, so it never arrives behind the caller's back.
Failures
    A queued message's failure never raises into the caller's code: it prints one
    line per destination, at most once a minute.  A refusal (a 4xx other than 429)
    is not tried again.
Shutdown
    ``close(timeout=10.0)`` delivers what is queued for up to *timeout* seconds, then
    stops the threads; the notifier stays usable.  At interpreter exit every open
    notifier shares 10 s, from ``threading``'s exit hook, which Python runs before
    every ``atexit`` handler: a chart's keep-alive («press Ctrl+C to exit») never
    holds the last messages back.
Daily jobs
    ``add_daily_job(job, at="00:00")`` runs a callable once a day at a wall-clock
    time in the notifier's zone, on a thread of its own.
Work for the trade-event adapter
    The trading thread only appends to an inbox (:meth:`Notifier._post`); the
    sender thread drains it, outside the lock, and the work queues the messages.
    A message can reply to an earlier one, or edit it, resolved just before it
    goes, once the earlier one's id is known; a backend without replies or edits
    sends a plain new message instead.
Pictures and files (v1.2.0)
    ``send_photo``, ``send_chart`` and ``send_report`` send a picture or a file
    through the same queue, as messages of kind ``text``; its text is the caption.
    A picture is made on the sender thread, just before its message is first tried,
    and once for every destination.  A caption too long for the backend goes as a
    reply to the picture.  A trade's picture that cannot be made lets its message
    go as text; a picture you asked for is not sent without it.  A backend that
    cannot send pictures sends the text alone.
Answers (v1.2.0)
    A backend that takes commands (Telegram's, ``notify/_commands.py``) answers in
    the chat a command came from, through a destination made for that chat beside
    the named ones: no filter, no ``to=`` and no ``send_text`` reaches it.

Lines are printed from the notifier's own threads, never the caller's:
``[AlgoTradeKit] telegram: …``.
"""

from __future__ import annotations

import atexit
import itertools
import re
import threading
import time
import weakref
from collections import deque
from collections.abc import Callable, Hashable, Iterable, Mapping
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, NamedTuple

from . import _template

#: The kind of a ``send_text`` message.
KIND_TEXT = "text"
#: The kinds of the trade events, named as ``AlgoTradeKit.trader`` names its events.
KIND_SIGNAL = "signal"
KIND_EXIT_SIGNAL = "exit_signal"
KIND_OPEN = "open"
KIND_SL_MOVE = "sl_move"
KIND_RISK_FREE = "risk_free"
KIND_TP_LEVEL = "tp_level"
KIND_CLOSE = "close"
KIND_DAILY_LOSS = "daily_loss"
KIND_RECONCILE = "reconcile"
KIND_ERROR = "error"
#: A strategy's own message (``BaseStrategy.notify``).
KIND_MESSAGE = "message"
#: A trading session's start and stop.
KIND_START = "start"
KIND_STOP = "stop"
#: The summary of a day, sent at 00:00 in the notifier's zone.
KIND_DAILY_SUMMARY = "daily_summary"
#: A price alert's message (``watch``).
KIND_ALERT = "alert"

#: Every kind, in the order an error lists them.
KINDS = (
    KIND_TEXT, KIND_SIGNAL, KIND_EXIT_SIGNAL, KIND_OPEN, KIND_SL_MOVE, KIND_RISK_FREE,
    KIND_TP_LEVEL, KIND_CLOSE, KIND_DAILY_LOSS, KIND_RECONCILE, KIND_ERROR, KIND_MESSAGE,
    KIND_START, KIND_STOP, KIND_DAILY_SUMMARY, KIND_ALERT,
)

#: Kinds a destination with ``events=None`` does not get: a trailing stop moves too often.
_NOT_BY_DEFAULT = frozenset({KIND_SL_MOVE})

#: Kinds that go out without sound, unless a chat is silent anyway.
SILENT_KINDS = frozenset({KIND_SL_MOVE, KIND_RISK_FREE, KIND_DAILY_SUMMARY})

#: The fields of a ``templates={"text": …}`` wrapper: the text, then a custom text's own.
_WRAPPER_FIELDS = (KIND_TEXT, *_template.TEXT_FIELDS)

#: Kinds that name no pair, so ``images=`` cannot picture them (``send_chart`` makes
#: a picture for a text).
_NO_PICTURE = frozenset({KIND_TEXT, KIND_START, KIND_STOP})

#: How many candles a picture shows when nothing says otherwise.
PICTURE_CANDLES = 100

#: At most this many messages wait; past it the oldest are dropped.
MAX_WAITING = 1_000

#: Pauses before trying again after a network or server error, seconds; the last
#: one repeats for as long as the trouble lasts.
_RETRY_STEPS = (1.0, 5.0, 15.0)

#: A call that waits gives up at the first failed try this long after it (1 + 5 + 15).
_GIVE_UP_SECONDS = sum(_RETRY_STEPS)

#: At most one line a minute per destination, and one for the service as a whole.
_LINE_EVERY = 60.0

#: A message delivered this long after it was queued, or longer, says how late it is.
_LATE_AFTER = 60.0

#: At interpreter exit, every open notifier shares this many seconds.
_EXIT_FLUSH_SECONDS = 10.0

#: How long ``close`` waits for a stopped sender to print its last lines: briefly when
#: it is still on a request (``close`` then prints them itself), longer when it is not.
_JOIN_GRACE = 0.5
_JOIN_IDLE = 5.0

#: The longest a daily-job thread sleeps before it reads the wall clock again, so a
#: machine that slept, or a clock that jumped, is noticed.
_JOBS_RECHECK = 30.0

_CLOSED = "notifier closed before the message was sent"
_SUPERSEDED = "replaced by a newer edit before it went"

_TAG_OR_ENTITY = re.compile(r"<[^<>]*>|&#?[0-9A-Za-z]+;")
_TAG = re.compile(r"<[^<>]*>")
_TAG_NAME = re.compile(r"</?\s*([A-Za-z][\w-]*)")
_HHMM = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")


class NotifyError(Exception):
    """A notification could not be sent, or a notifier could not be set up.

    For a call that waited, ``sent`` maps each destination that got the message
    to its message id, and ``failed`` maps each one that did not to the reason."""

    def __init__(
        self,
        message: str,
        *,
        sent: Mapping[str, Any] | None = None,
        failed: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.sent: dict[str, Any] = dict(sent or {})
        self.failed: dict[str, str] = dict(failed or {})


def _plural(count: int, noun: str) -> str:
    return f"{count:,} {noun}" if count == 1 else f"{count:,} {noun}s"


def check_kinds(names: Iterable[str]) -> None:
    """Raise ``ValueError`` for a kind nobody knows: «unknown kind 'sginal' — known: …»."""
    for name in sorted(names):
        if name not in KINDS:
            raise ValueError(f"unknown kind {name!r} — known: {', '.join(KINDS)}")


def _check_images(images: Any) -> frozenset[str]:
    """``images={kind, …}``: known kinds, each one that names a pair."""
    if images is None:
        return frozenset()
    if isinstance(images, str) or not isinstance(images, Iterable):
        raise TypeError(f'images must be a set of kinds such as {{"open", "close"}}, '
                        f"got {type(images).__name__}")
    kinds = frozenset(images)
    check_kinds(kinds)
    for kind in sorted(kinds):
        if kind == KIND_ALERT:
            raise ValueError("images: 'alert' — an alert asks for its own picture: "
                             "watch(…, image=True)")
        if kind in _NO_PICTURE:
            allowed = ", ".join(k for k in KINDS if k not in _NO_PICTURE and k != KIND_ALERT)
            raise ValueError(f"images: {kind!r} names no pair — a picture shows one pair's "
                             f"candles; pictures go on {allowed}")
    return kinds


def _check_templates(templates: Any) -> dict[str, str | None]:
    """``templates={kind: text | None}``, checked: known kinds, strings or ``None``, and
    the ``text`` wrapper's fields (the trade-event kinds are checked when wired)."""
    if templates is None:
        return {}
    if not isinstance(templates, Mapping):
        raise TypeError(
            'templates must be a dict such as {"open": "…", "sl_move": None}, '
            f"got {type(templates).__name__}"
        )
    checked: dict[str, str | None] = {}
    for kind, template in templates.items():
        if not isinstance(kind, str):
            raise TypeError(f"templates must be keyed by kind names, got {kind!r}")
        check_kinds([kind])
        if template is not None and not isinstance(template, str):
            raise TypeError(
                f"templates[{kind!r}] must be a string, or None to send nothing, "
                f"got {type(template).__name__}"
            )
        checked[kind] = template
    if checked.get(KIND_TEXT) is not None:
        _template.fields_of(checked[KIND_TEXT], _WRAPPER_FIELDS)
    return checked


# ---------------------------------------------------------------------------
# Clock
# ---------------------------------------------------------------------------

class _SystemClock:
    """Wall time, monotonic time and waiting.  Tests swap in a fake through
    ``Notifier._clock``."""

    @staticmethod
    def time() -> float:
        return time.time()

    @staticmethod
    def monotonic() -> float:
        return time.monotonic()

    @staticmethod
    def wait(condition: threading.Condition, timeout: float | None) -> None:
        condition.wait(timeout)


# ---------------------------------------------------------------------------
# Splitting rich text
# ---------------------------------------------------------------------------

def _tokens(text: str, rich: bool) -> list[str]:
    """Characters, with every tag and entity kept whole, so no cut lands inside one."""
    if not rich:
        return list(text)
    out: list[str] = []
    pos = 0
    for match in _TAG_OR_ENTITY.finditer(text):
        out.extend(text[pos:match.start()])
        out.append(match.group())
        pos = match.end()
    out.extend(text[pos:])
    return out


def _after(stack: list[tuple[str, str]], token: str) -> list[tuple[str, str]]:
    """The open tags after *token*: an opening tag is pushed, a closing one popped."""
    if len(token) < 3 or token[0] != "<" or token[-1] != ">":
        return stack
    match = _TAG_NAME.match(token)
    if match is None or token.endswith("/>"):
        return stack
    name = match.group(1).lower()
    if token[1] != "/":
        return [*stack, (name, token)]
    for index in range(len(stack) - 1, -1, -1):
        if stack[index][0] == name:
            return stack[:index]
    return stack


def _closers(stack: list[tuple[str, str]]) -> str:
    return "".join(f"</{name}>" for name, _ in reversed(stack))


def _visible(text: str, rich: bool) -> str:
    """What a reader sees of *text*: without its tags, when it is rich text."""
    return _TAG.sub("", text) if rich else text


def split_text(text: str, limit: int, *, rich: bool, measure: Callable[[str], int]) -> list[str]:
    """Cut *text* into parts of at most *limit* units, as *measure* counts them.

    Each part ends at the last line break before the limit (the break itself is
    dropped); a line longer than the limit is cut at it.  With *rich* on, a cut never
    lands inside a tag or an entity, and the tags open at a cut are closed at the end
    of one part and opened again at the start of the next, so each part is valid on
    its own."""
    if measure(text) <= limit:
        return [text]
    tokens = _tokens(text, rich)
    sizes = [measure(token) for token in tokens]
    parts: list[str] = []
    stack: list[tuple[str, str]] = []
    start, count = 0, len(tokens)
    while start < count:
        prefix = "".join(tag for _, tag in stack)
        used = measure(prefix)
        open_tags = stack
        end = start
        cut: tuple[int, list[tuple[str, str]]] | None = None
        while end < count:
            after = _after(open_tags, tokens[end]) if rich else open_tags
            if used + sizes[end] + measure(_closers(after)) > limit:
                break
            if tokens[end] == "\n" and end > start:
                cut = (end, open_tags)
            used += sizes[end]
            open_tags = after
            end += 1
        if end == count:
            parts.append(prefix + "".join(tokens[start:]))
            break
        if cut is not None:
            end, open_tags = cut
            parts.append(prefix + "".join(tokens[start:end]) + _closers(open_tags))
            stack, start = open_tags, end + 1
            continue
        if end == start:  # one tag wider than a whole message: it goes as it is
            open_tags = _after(open_tags, tokens[end]) if rich else open_tags
            end += 1
        parts.append(prefix + "".join(tokens[start:end]) + _closers(open_tags))
        stack, start = open_tags, end
    return [part for part in parts if _visible(part, rich).strip()] or [text]


# ---------------------------------------------------------------------------
# Queue items
# ---------------------------------------------------------------------------

class _Outcome:
    """What one request came to.

    ``status``: ``"sent"`` (``message_id``), ``"wait"`` (asked to slow down:
    ``retry_after``), ``"refused"`` (not worth trying again), ``"server"`` (a
    server error: try again later) or ``"unreachable"`` (the network)."""

    __slots__ = ("status", "message_id", "reason", "retry_after")

    def __init__(
        self, status: str, *, message_id: Any = None, reason: str = "", retry_after: float = 0.0
    ) -> None:
        self.status = status
        self.message_id = message_id
        self.reason = reason
        self.retry_after = retry_after


class _Sent(NamedTuple):
    """A message that went out, as ``on_done`` hears of it: the id of its first
    message, its text as it went, and where that text can be edited — the same
    message (``caption`` tells whether as a picture's caption), or the reply that
    holds a caption too long for its picture."""

    message_id: Any
    text: str | None
    text_id: Any = None
    caption: bool = False


class _Media:
    """A picture or a file that a message carries (v1.2.0).

    ``data`` is its bytes, or ``None`` until ``make()`` makes them, on the sender
    thread just before the message is first tried — once, whatever the number of
    destinations.  When it cannot be made, a ``fallback`` message goes as text,
    with one line naming ``label``; any other is not sent."""

    PHOTO = "photo"
    DOCUMENT = "document"

    __slots__ = ("kind", "data", "make", "filename", "fallback", "label", "error", "lock")

    def __init__(
        self, kind: str, *, data: bytes | None = None,
        make: Callable[[], bytes] | None = None, filename: str = "", fallback: bool = False,
        label: str = "",
    ) -> None:
        self.kind = kind
        self.data = data
        self.make = make
        self.filename = filename or ("picture.png" if kind == _Media.PHOTO else "file")
        self.fallback = fallback
        self.label = label
        self.error: str | None = None      # why it could not be made, once tried
        self.lock = threading.Lock()

    def resolve(self) -> bytes | None:
        """The data, made now if need be; ``None`` (and ``error``) when it cannot be."""
        with self.lock:
            if self.data is None and self.error is None and self.make is not None:
                try:
                    self.data = self.make()
                except Exception as exc:  # noqa: BLE001 — the message decides what to do
                    said = str(exc).strip()
                    self.error = said if said else type(exc).__name__
                finally:
                    self.make = None
            return self.data


class _Destination:
    """One named destination: the backend's address, its filters and its queue."""

    def __init__(
        self,
        name: str,
        target: Any,
        *,
        events: frozenset[str] | None,
        symbols: frozenset[str] | None,
        silent: bool,
        label: str,
        pace_key: Hashable,
        pace_rules: tuple[tuple[int, float], ...],
    ) -> None:
        self.name = name
        self.target = target              # the backend's own description of the address
        self.events = events
        self.symbols = symbols
        self.silent = silent
        self.label = label                # how lines and errors name it: "signals (@atk_signals)"
        self.pace_key = pace_key          # destinations sharing a key share its pacing
        self.pace_rules = pace_rules      # ((count, seconds), …): at most count per seconds
        self.waiting: deque[_Delivery] = deque()
        self.retry_at = 0.0               # monotonic; a server error holds it until then
        self.failures = 0                 # server errors in a row
        self.line_at: float | None = None

    def accepts(self, kind: str, symbol: str | None) -> bool:
        """Does a message of *kind* about *symbol* pass this destination's filters?"""
        if self.events is None:
            if kind in _NOT_BY_DEFAULT:
                return False
        elif kind not in self.events:
            return False
        if self.symbols is None or not symbol:
            return True
        return symbol.upper() in self.symbols


class _Pace:
    """The sends to one chat, against its rules: at most ``count`` per ``seconds``."""

    def __init__(self, rules: tuple[tuple[int, float], ...]) -> None:
        self.rules = rules
        self.sent: deque[float] = deque(maxlen=max(count for count, _ in rules))
        self.hold = 0.0                   # monotonic; set by "slow down"

    def next_at(self) -> float:
        at = self.hold
        for count, seconds in self.rules:
            if len(self.sent) >= count:
                at = max(at, self.sent[-count] + seconds)
        return at


class _Waiter:
    """A ``wait=True`` call: the outcome of each of its destinations."""

    def __init__(self, count: int, called: float) -> None:
        self.pending = count
        self.called = called              # monotonic
        self.sent: dict[str, Any] = {}
        self.failed: dict[str, str] = {}
        self.event = threading.Event()


class _Message:
    __slots__ = ("kind", "symbol", "body", "queued_at", "media")

    def __init__(
        self, kind: str, symbol: str | None, body: str, queued_at: float,
        media: _Media | None = None,
    ) -> None:
        self.kind = kind
        self.symbol = symbol
        self.body = body                  # with a picture or a file: its caption
        self.queued_at = queued_at        # wall time, for the late marker
        self.media = media


class _Delivery:
    """One message on its way to one destination.

    The trade-event adapter can give it more, all resolved on the sender thread
    just before the first part goes:

    * ``reply()`` — the id of the message to reply to, or ``None``;
    * ``edit()`` — ``(message id, text)`` to edit that message instead, or ``None``
      to send the body as a new message.  An edit the service refuses goes as a
      new message after all;
    * ``coalesce`` — a newer delivery with the same key, to the same destination,
      replaces this one while it waits;
    * ``on_done(message_id, text, text_id, caption)`` — called once it is done,
      with what a :class:`_Sent` holds when it went out, and with ``None, None,
      None, False`` when not.

    An ``edit()`` may also say ``(message id, text, True)``: the text is a
    picture's caption.
    """

    __slots__ = (
        "seq", "message", "dest", "silent", "waiter", "parts", "next_part", "first_id", "done",
        "reply", "edit", "coalesce", "on_done", "first_text", "editing", "edit_caption",
        "media", "caption", "text_id",
    )

    def __init__(
        self, seq: int, message: _Message, dest: _Destination, silent: bool,
        waiter: _Waiter | None,
    ) -> None:
        self.seq = seq
        self.message = message
        self.dest = dest
        self.silent = silent
        self.waiter = waiter
        self.parts: list[str] | None = None   # fixed once the first part is out
        self.next_part = 0
        self.first_id: Any = None
        self.done = False
        self.reply: Callable[[], Any] | None = None
        self.edit: Callable[[], tuple[Any, str] | None] | None = None
        self.coalesce: Hashable | None = None
        self.on_done: Callable[..., None] | None = None
        self.first_text: str | None = None    # the first part as it went out
        self.editing: Any = None              # the message the attempt on its way edits
        self.edit_caption = False             # … and whether that is a caption
        self.media: _Media | None = message.media   # None once it goes as text
        self.caption = False                  # the text rides as the picture's caption
        self.text_id: Any = None              # the message holding the text


class _Job:
    __slots__ = ("job", "hour", "minute", "next_at")

    def __init__(self, job: Callable[[], Any], hour: int, minute: int, next_at: float) -> None:
        self.job = job
        self.hour = hour
        self.minute = minute
        self.next_at = next_at


def _png_size(data: bytes) -> tuple[int, int] | None:
    """A PNG's width and height from its header; ``None`` for anything else."""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


# ---------------------------------------------------------------------------
# The exit flush
# ---------------------------------------------------------------------------

_open_notifiers: weakref.WeakSet[Notifier] = weakref.WeakSet()
_exit_lock = threading.Lock()
_exit_registered = False


def _register_exit_flush() -> None:
    """Once per process: flush every open notifier when the interpreter exits.

    ``threading``'s own exit hook runs before every ``atexit`` handler, and atexit
    runs its handlers last-in first-out, so a chart keep-alive registered after a
    notifier would otherwise block before the flush.  ``concurrent.futures``
    relies on the same hook; where it is missing, plain ``atexit``."""
    global _exit_registered
    with _exit_lock:
        if _exit_registered:
            return
        _exit_registered = True
    register = getattr(threading, "_register_atexit", None)
    if register is not None:
        try:
            register(_flush_at_exit)
            return
        except RuntimeError:  # the interpreter is already shutting down
            pass
    atexit.register(_flush_at_exit)


def _flush_at_exit() -> None:
    close_notifiers(list(_open_notifiers), _EXIT_FLUSH_SECONDS)


def close_notifiers(notifiers: Iterable[Notifier], timeout: float = 10.0) -> None:
    """Close several notifiers within one shared *timeout*: each delivers what it
    has queued, all at the same time, until the time runs out."""
    unique = list(dict.fromkeys(notifiers))
    deadline = time.monotonic() + max(0.0, float(timeout))
    for notifier in unique:
        notifier._begin_close()
    for notifier in unique:
        notifier._end_close(deadline)


# ---------------------------------------------------------------------------
# Notifier
# ---------------------------------------------------------------------------

class Notifier:
    """The core every backend subclasses (see the module docstring).

    A backend sets the class attributes below, builds its destinations, calls
    ``Notifier.__init__`` and implements :meth:`_deliver`."""

    #: The service's name in lines and errors: "Telegram".
    _service = "the service"
    #: The tag after ``[AlgoTradeKit]`` in lines: "telegram".
    _tag = "notify"
    #: The error a call that waits raises.
    _error: type[NotifyError] = NotifyError
    #: The longest text one message may hold, as :meth:`_measure` counts it.
    _max_length = 4096
    #: Can a message reply to an earlier one (:meth:`_deliver`'s ``reply_to``)?
    _can_reply = False
    #: Can an earlier message be edited (:meth:`_edit`)?
    _can_edit = False
    #: Can a message carry a picture or a file (:meth:`_deliver_media`)?  Without it,
    #: a picture's text goes alone.
    _can_send_media = False
    #: The longest caption a picture may hold, as :meth:`_measure` counts it.
    _max_caption = 1024
    #: The largest picture and file the service takes, in bytes; a picture's width +
    #: height, and its longer side over its shorter one.
    _max_photo_bytes = 10 * 1024 * 1024
    _max_photo_sides = 10_000
    _max_photo_ratio = 20.0
    _max_document_bytes = 50 * 1024 * 1024
    #: The clock; tests replace it.
    _clock = _SystemClock()
    #: The command listener of a backend that takes commands (v1.2.0), or ``None``.  It
    #: listens from the notifier's creation until :meth:`close`, and again from the next
    #: send, ``watch()`` or trading session.
    _commands: Any = None

    def __init__(
        self, destinations: Mapping[str, _Destination], *, zone: tzinfo | None, rich: bool,
        templates: Mapping[str, str | None] | None = None, images: Iterable[str] | None = None,
    ) -> None:
        self._destinations = dict(destinations)
        self._zone = zone
        self._rich = rich
        self._templates = _check_templates(templates)
        self._images = _check_images(images)
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)       # wakes the sender
        self._finished = threading.Condition(self._lock)   # a delivery ended
        self._paces: dict[Hashable, _Pace] = {}
        self._seq = itertools.count()
        self._order: deque[_Delivery] = deque()   # every waiting delivery, oldest first
        self._waited: set[_Delivery] = set()      # the waiting ones a call waits for
        self._waiting = 0
        self._in_flight: _Delivery | None = None
        self._lines: list[str] = []
        self._dropped = 0
        self._dropped_line_at: float | None = None
        self._thread: threading.Thread | None = None
        self._generation = 0
        self._dirty = False                   # something changed since the sender looked
        self._in_wait = False                 # the sender is waiting for work
        self._seen = 0.0                      # the clock when the sender last looked
        self._down_since: float | None = None # wall time the outage was first seen
        self._down_failures = 0
        self._probe_at = 0.0
        self._down_line_at: float | None = None
        self._inbox: deque[Callable[[], None]] = deque()   # work the sender runs
        self._callbacks: list[tuple[Callable[..., None], tuple]] = []
        self._working = False                 # the sender is running work or callbacks
        self._local = threading.local()       # .generation: on a sender thread, its own
        self._jobs_lock = threading.Lock()
        self._jobs_cond = threading.Condition(self._jobs_lock)
        self._jobs: list[_Job] = []
        self._jobs_thread: threading.Thread | None = None
        self._jobs_generation = 0
        self._jobs_in_wait = False
        self._jobs_seen = 0.0
        self._alerts: list[Any] = []                           # the price alerts (watch)
        self._alerts_changed = threading.Condition()
        #: Where answers to commands go, by the backend's key for a chat (v1.2.0).
        self._extra_destinations: dict[Hashable, _Destination] = {}

    # ------------------------------------------------------------------
    # For the backend
    # ------------------------------------------------------------------

    def _deliver(
        self, dest: _Destination, text: str, *, silent: bool, reply_to: Any = None
    ) -> _Outcome:
        """Send one part to *dest*.  Runs on the sender thread; never raises.

        ``reply_to`` (a backend with :attr:`_can_reply`) is the message this one
        answers; the core passes it only when the message is a reply."""
        raise NotImplementedError

    def _edit(self, dest: _Destination, message_id: Any, text: str) -> _Outcome:
        """Replace the text of message *message_id* in *dest* (a backend with
        :attr:`_can_edit`).  ``"sent"`` when done — an edit that changes nothing
        counts as done; ``"refused"`` makes it a new message instead."""
        raise NotImplementedError

    def _edit_caption(self, dest: _Destination, message_id: Any, text: str) -> _Outcome:
        """Replace the caption of picture *message_id* in *dest*, as :meth:`_edit`
        replaces a text."""
        raise NotImplementedError

    def _deliver_media(
        self, dest: _Destination, media: _Media, data: bytes, caption: str | None, *,
        silent: bool, reply_to: Any = None,
    ) -> _Outcome:
        """Send *data* — ``media.kind`` says a picture or a file — to *dest*, with
        *caption* (a backend with :attr:`_can_send_media`).  Never raises."""
        raise NotImplementedError

    @staticmethod
    def _measure(text: str) -> int:
        """The length the service counts for *text*."""
        return len(text)

    def _mask(self, text: str) -> str:
        """Hide anything secret in *text* before it is printed or raised."""
        return text

    def _say(self, text: str) -> None:
        print(f"[AlgoTradeKit] {self._tag}: {self._mask(text)}", flush=True)

    def _mark_unreachable(self, line: str) -> None:
        """The setup check could not reach the service: say *line*, and start the
        outage there, so the queue's own lines follow a minute later."""
        now = self._clock.monotonic()
        with self._lock:
            self._down_since = self._clock.time()
            self._down_failures = 1
            self._probe_at = now + _RETRY_STEPS[0]
            self._down_line_at = now
        self._say(line)

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    def send_text(
        self,
        text: str,
        *,
        to: str | int | list | tuple | set | None = None,
        symbol: str | None = None,
        broker: Any = None,
        silent: bool | None = None,
        wait: bool = True,
    ) -> dict[str, Any]:
        """Send *text*, with its placeholders filled in, to the chats it is for.

        * ``to=None`` sends to every chat whose filters let a ``text`` through;
          ``to="me"`` or a list of names picks chats, whatever their filters.
        * Placeholders: ``{time}`` (now), ``{date}``, ``{symbol}``, ``{price}``
          (``broker.get_ticker(symbol).last``, asked now) and ``{header}``
          («🕑 {time} · {symbol} {price}»).  ``{{`` and ``}}`` write a brace.
        * ``silent`` overrides each chat's own ``silent`` for this call.
        * ``wait=True`` (the default) returns ``{name: message id}`` once every chat
          has the message, and raises the backend's error (``TelegramError``) if any
          refused it, or if the service stayed unreachable for 21 s; that message is
          then withdrawn.  ``wait=False`` queues it and returns ``{name: None}`` at
          once.  Either way ``{}`` means no chat takes it.
        * A ``templates={"text": …}`` wrapper puts the text where its ``{text}`` is,
          with the same placeholders around it; ``{"text": None}`` sends no text.

        Mistakes in the text raise ``ValueError`` here, before anything is queued.
        """
        body = self._custom_text(text, symbol=symbol, broker=broker)
        if body is None:
            return {}   # templates={"text": None}: custom texts are not sent
        return self._enqueue(KIND_TEXT, body, symbol=symbol, to=to, silent=silent, wait=wait)

    def _custom_text(self, text: str, *, symbol: str | None, broker: Any) -> str | None:
        """*text* with its placeholders filled in, inside the ``text`` wrapper when
        there is one; ``None`` with ``templates={"text": None}``."""
        names = _template.fields_of(text, _template.TEXT_FIELDS)
        if KIND_TEXT in self._templates and self._templates[KIND_TEXT] is None:
            return None
        wrapper = self._templates.get(KIND_TEXT)
        if wrapper is not None:
            names += [
                name for name in _template.fields_of(wrapper, _WRAPPER_FIELDS)
                if name != KIND_TEXT and name not in names
            ]
        values = _template.text_values(
            names, now=self._clock.time(), zone=self._zone, symbol=symbol, broker=broker
        )
        body = _template.render(text, values, escape=self._rich)
        if wrapper is not None:
            body = _template.render(
                wrapper, {**values, KIND_TEXT: _template.Markup(body)}, escape=self._rich
            )
        return body

    # ------------------------------------------------------------------
    # Pictures and files (v1.2.0)
    # ------------------------------------------------------------------

    def send_photo(
        self,
        png: bytes,
        caption: str | None = None,
        *,
        to: str | int | list | tuple | set | None = None,
        symbol: str | None = None,
        broker: Any = None,
        silent: bool | None = None,
        wait: bool = True,
    ) -> dict[str, Any]:
        """Send a picture you made — its bytes, PNG or JPEG — to the chats it is for.

        The caption takes the placeholders a text does (``symbol=`` and ``broker=``
        serve ``{symbol}`` and ``{price}``), inside a ``templates={"text": …}``
        wrapper when there is one.  A caption too long for one picture goes as a
        reply to it.  ``to``, ``silent`` and ``wait`` and what comes back are as for
        :meth:`send_text`: with ``wait=True``, ``{name: id of the picture}``."""
        data = self._picture_bytes(png)
        body = "" if caption is None else self._custom_text(caption, symbol=symbol, broker=broker)
        if body is None:
            return {}
        media = _Media(_Media.PHOTO, data=data)
        return self._enqueue(KIND_TEXT, body, symbol=symbol, to=to, silent=silent, wait=wait,
                             media=media)

    def send_chart(
        self,
        chart: Any = None,
        start: Any = None,
        end: Any = None,
        *,
        broker: Any = None,
        symbol: str | None = None,
        timeframe: str | None = None,
        indicators: list[dict] | None = None,
        caption: str | None = None,
        to: str | int | list | tuple | set | None = None,
        silent: bool | None = None,
        wait: bool = True,
    ) -> dict[str, Any]:
        """Send a picture of a chart, from *start* to *end*, both inclusive.

        * ``send_chart(chart, start, end)`` photographs a ``Chart`` you built, with
          whatever it holds: indicators, drawings, trades.
        * ``send_chart(broker=…, symbol="BTCUSDT", timeframe="15m", start=…,
          end=…, indicators=[{"kind": "ema", "period": 20}])`` fetches the candles
          now, on this thread — a wrong symbol raises here — and photographs them.
          ``end=None`` is now and ``start=None`` 100 candles before it.

        ``start`` and ``end`` take what a chart's ``candle_range`` takes.  The time
        axis and the title line read in the notifier's zone.  The picture is made
        on the notifier's thread; a picture that cannot be made is not sent, and
        with ``wait=True`` that raises.  Needs the ``[notify]`` extra and its
        browser: ``pip install "AlgoTradeKit[notify]"``, then ``playwright install
        chromium``.  The rest is as :meth:`send_photo`."""
        from . import _pictures

        make = _pictures.chart_maker(
            self, chart, start, end, broker=broker, symbol=symbol, timeframe=timeframe,
            indicators=indicators,
        )
        body = "" if caption is None else self._custom_text(caption, symbol=symbol, broker=broker)
        if body is None:
            return {}
        media = _Media(_Media.PHOTO, make=make)
        return self._enqueue(KIND_TEXT, body, symbol=symbol, to=to, silent=silent, wait=wait,
                             media=media)

    def send_report(
        self,
        report: Any,
        *,
        title: str | None = None,
        to: str | int | list | tuple | set | None = None,
    ) -> dict[str, Any]:
        """Send a backtest's ``SimulateReport`` as three messages: its figures as
        text, its balance curve as a picture, and the HTML report as a file.

        Waits until each is sent and returns ``{name: id of the text}``; raises as
        :meth:`send_text` does.  Without a balance history there is no picture, and
        a report file over the service's limit is left out, with one line."""
        from . import _pictures

        return _pictures.send_report(self, report, title=title, to=to)

    def _picture_bytes(self, png: Any) -> bytes:
        """*png* as bytes, checked against the service's limits for a picture."""
        if isinstance(png, str):
            raise TypeError("a picture is its bytes (PNG or JPEG), not a path: "
                            "send_photo(open(path, 'rb').read())")
        if not isinstance(png, (bytes, bytearray, memoryview)):
            raise TypeError(f"a picture is its bytes (PNG or JPEG), got {type(png).__name__}")
        data = bytes(png)
        if not data:
            raise ValueError("the picture is empty")
        if len(data) > self._max_photo_bytes:
            raise ValueError(f"the picture is {len(data) / 1e6:.1f} MB — {self._service} takes "
                             f"at most {self._max_photo_bytes / 1024 / 1024:g} MB")
        size = _png_size(data)
        if size is not None:
            width, height = size
            if width + height > self._max_photo_sides:
                raise ValueError(f"the picture is {width}×{height} — {self._service} takes a "
                                 f"width + height of at most {self._max_photo_sides:,}")
            if max(width, height) > self._max_photo_ratio * max(1, min(width, height)):
                raise ValueError(f"the picture is {width}×{height} — {self._service} takes "
                                 f"one side at most {self._max_photo_ratio:g} times the other")
        return data

    def _select(self, kind: str, symbol: str | None, to: Any) -> list[_Destination]:
        if to is None:
            return [dest for dest in self._destinations.values() if dest.accepts(kind, symbol)]
        names = [to] if isinstance(to, (str, int)) else list(to)
        picked: list[_Destination] = []
        for name in names:
            key = str(name) if isinstance(name, int) and not isinstance(name, bool) else name
            dest = self._destinations.get(key) if isinstance(key, str) else None
            if dest is None:
                known = ", ".join(self._destinations)
                raise ValueError(f"unknown chat {name!r} — known: {known}")
            if dest not in picked:
                picked.append(dest)
        return picked

    def _enqueue(
        self, kind: str, body: str, *, symbol: str | None, to: Any, silent: bool | None,
        wait: bool, media: _Media | None = None,
    ) -> dict[str, Any]:
        """Queue *body* — with *media*, its caption — for every destination it is for;
        with *wait*, block until each has it."""
        dests = self._select(kind, symbol, to)
        if media is None and not _visible(body, self._rich).strip():
            raise ValueError("the text is empty")
        if not dests:
            return {}
        message = _Message(kind, symbol, body, self._clock.time(), media)
        waiter = _Waiter(len(dests), self._clock.monotonic()) if wait else None
        with self._lock:
            self._ensure_thread_locked()
            for dest in dests:
                delivery = _Delivery(
                    next(self._seq), message, dest,
                    dest.silent if silent is None else bool(silent), waiter,
                )
                dest.waiting.append(delivery)
                self._order.append(delivery)
                self._waiting += 1
                if waiter is not None:
                    self._waited.add(delivery)
            self._trim_locked()
            self._wake_locked()
        if waiter is None:
            return {dest.name: None for dest in dests}
        waiter.event.wait()
        if waiter.failed:
            failed = "; ".join(waiter.failed.values())
            sent = f"; sent to {', '.join(waiter.sent)}" if waiter.sent else ""
            raise self._error(
                self._mask(failed + sent), sent=waiter.sent,
                failed={name: self._mask(reason) for name, reason in waiter.failed.items()},
            )
        return {dest.name: waiter.sent[dest.name] for dest in dests}

    # ------------------------------------------------------------------
    # For the trade-event adapter (``AlgoTradeKit.trader``)
    # ------------------------------------------------------------------

    def _template_for(self, kind: str) -> tuple[bool, str | None]:
        """``(True, text or None)`` when ``templates=`` names *kind*; ``(False, None)``
        when the default applies."""
        if kind in self._templates:
            return True, self._templates[kind]
        return False, None

    def _recipients(self, kind: str, symbol: str | None) -> list[_Destination]:
        """The destinations whose filters let a message of *kind* about *symbol* in."""
        return self._select(kind, symbol, None)

    def _post(self, work: Callable[[], None]) -> None:
        """Have the sender thread run ``work()``, outside the lock, in the order
        posted.  The trading thread posts: this appends and returns, and never
        waits on the service."""
        with self._lock:
            self._ensure_thread_locked()
            self._inbox.append(work)
            self._wake_locked()

    def _queue_event(
        self,
        dest: _Destination,
        kind: str,
        symbol: str | None,
        body: str,
        *,
        queued_at: float,
        silent: bool,
        reply: Callable[[], Any] | None = None,
        edit: Callable[[], tuple[Any, str] | None] | None = None,
        coalesce: Hashable | None = None,
        on_done: Callable[..., None] | None = None,
        media: _Media | None = None,
    ) -> None:
        """Queue one message for one destination (see :class:`_Delivery` for
        *reply*, *edit*, *coalesce* and *on_done*; *media* is a picture the body is
        the caption of).  *queued_at* is the wall time the late marker counts from:
        when the event was received."""
        message = _Message(kind, symbol, body, queued_at, media)
        with self._lock:
            stale = getattr(self._local, "generation", None)
            if stale is not None and stale != self._generation:
                return   # work of a sender that close() already stopped
            self._ensure_thread_locked()
            if coalesce is not None:
                for older in list(dest.waiting):
                    if (older.coalesce == coalesce and older is not self._in_flight
                            and older.next_part == 0):
                        self._finish_locked(older, reason=_SUPERSEDED)
            delivery = _Delivery(next(self._seq), message, dest, silent, None)
            delivery.reply, delivery.edit = reply, edit
            delivery.coalesce, delivery.on_done = coalesce, on_done
            dest.waiting.append(delivery)
            self._order.append(delivery)
            self._waiting += 1
            self._trim_locked()
            self._wake_locked()

    def _trim_locked(self) -> None:
        """Drop the oldest waiting messages past :data:`MAX_WAITING`."""
        kept = None
        while self._waiting > MAX_WAITING and self._order:
            oldest = self._order.popleft()
            if oldest.done:
                continue
            if oldest is self._in_flight:
                kept = oldest
                continue
            self._finish_locked(oldest, reason="dropped: the queue was full; not sent")
            self._dropped += 1
        if kept is not None:
            self._order.appendleft(kept)

    # ------------------------------------------------------------------
    # The sender thread
    # ------------------------------------------------------------------

    def _wake_locked(self) -> None:
        self._dirty = True
        self._cond.notify_all()

    def _ensure_thread_locked(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        _register_exit_flush()
        _open_notifiers.add(self)
        self._thread = threading.Thread(
            target=self._run, args=(self._generation,),
            name=f"atk-notify-{self._tag}", daemon=True,
        )
        self._thread.start()
        if self._commands is not None:   # after a close(), listening starts again too
            self._commands.resume()

    def _run(self, generation: int) -> None:
        self._local.generation = generation
        while True:
            with self._lock:
                delivery = self._next_locked(generation)
                lines, self._lines = self._lines, []
                callbacks, self._callbacks = self._callbacks, []
                work: list[Callable[[], None]] = []
                if delivery is None and generation == self._generation:
                    work = list(self._inbox)
                    self._inbox.clear()
                self._working = bool(callbacks or work)
            for line in lines:
                self._say(line)
            if callbacks or work:
                self._run_work(callbacks, work)
                with self._lock:
                    self._working = False
                    self._finished.notify_all()
            if delivery is None:
                if generation != self._generation:
                    return
                continue
            try:
                outcome = self._attempt(delivery)
            except Exception as exc:  # noqa: BLE001 — the sender must outlive any one message
                outcome = _Outcome("refused", reason=f"{type(exc).__name__}: {exc}")
            with self._lock:
                self._settle_locked(delivery, outcome)

    def _run_work(
        self,
        callbacks: list[tuple[Callable[..., None], tuple]],
        work: list[Callable[[], None]],
    ) -> None:
        """The adapter's callbacks, then its work, on the sender thread, outside the
        lock: a failure prints one line and never stops the sender."""
        for callback, sent in callbacks:
            try:
                callback(*sent)
            except Exception as exc:  # noqa: BLE001 — the sender must outlive any one message
                self._say(f"could not keep track of a message ({type(exc).__name__}: {exc})")
        for item in work:
            try:
                item()
            except Exception as exc:  # noqa: BLE001
                self._say(f"could not prepare a message ({type(exc).__name__}: {exc})")

    def _next_locked(self, generation: int) -> _Delivery | None:
        """The next delivery that is due, marked in flight; ``None`` to print lines,
        to run the adapter's work or to stop.  Waits while nothing is due."""
        while True:
            self._dirty = False
            if generation != self._generation:
                return None
            now = self._clock.monotonic()
            self._seen = now
            line_at = self._drop_line_locked(now)
            if self._lines or self._callbacks or self._inbox:
                return None
            delivery, due_at = self._choose_locked(now)
            if delivery is not None:
                self._in_flight = delivery
                return delivery
            wake_at = min((at for at in (line_at, due_at) if at is not None), default=None)
            self._in_wait = True
            try:
                self._clock.wait(self._cond, None if wake_at is None else max(0.0, wake_at - now))
            finally:
                self._in_wait = False

    def _drop_line_locked(self, now: float) -> float | None:
        """Queue the «queue full» line when one is due; else when it will be."""
        if not self._dropped:
            return None
        if self._dropped_line_at is not None and now - self._dropped_line_at < _LINE_EVERY:
            return self._dropped_line_at + _LINE_EVERY
        self._lines.append(f"queue full — dropped {self._dropped:,} oldest "
                           f"{'message' if self._dropped == 1 else 'messages'}")
        self._dropped = 0
        self._dropped_line_at = now
        return None

    def _pace_of(self, dest: _Destination) -> _Pace:
        pace = self._paces.get(dest.pace_key)
        if pace is None:
            pace = self._paces[dest.pace_key] = _Pace(dest.pace_rules)
        return pace

    def _oldest_locked(self) -> _Delivery | None:
        while self._order and self._order[0].done:
            self._order.popleft()
        return self._order[0] if self._order else None

    def _choose_locked(self, now: float) -> tuple[_Delivery | None, float | None]:
        """The due delivery to send next, or the time the next one will be due."""
        if self._down_since is not None:
            oldest = self._oldest_locked()
            if oldest is None:
                return None, None
            at = max(self._probe_at, oldest.dest.retry_at, self._pace_of(oldest.dest).next_at())
            return (oldest, None) if at <= now else (None, at)
        best: _Delivery | None = None
        soonest: float | None = None
        for dest in (*self._destinations.values(), *self._extra_destinations.values()):
            if not dest.waiting:
                continue
            head = dest.waiting[0]
            at = max(dest.retry_at, self._pace_of(dest).next_at())
            if at <= now:
                if best is None or head.seq < best.seq:
                    best = head
            elif soonest is None or at < soonest:
                soonest = at
        return (best, None) if best is not None else (None, soonest)

    def _attempt(self, delivery: _Delivery) -> _Outcome:
        """Send the delivery's next part.  The first part carries the late marker,
        and the split is worked out with it, so the marker never pushes a part past
        the limit.  An edit, the message a reply answers, and a picture are resolved
        here, just before the first part goes."""
        reply_to = None
        if delivery.next_part == 0:
            delivery.editing = None
            delivery.edit_caption = False
            if delivery.edit is not None and self._can_edit:
                target = delivery.edit()
                if target is not None:
                    message_id, text, *how = target
                    delivery.editing, delivery.first_text = message_id, text
                    delivery.edit_caption = bool(how and how[0])
                    if delivery.edit_caption:
                        return self._edit_caption(delivery.dest, message_id, text)
                    return self._edit(delivery.dest, message_id, text)
            media = delivery.media
            if media is not None and not self._can_send_media:
                delivery.media = media = None            # the text goes alone
            if media is not None and media.resolve() is None:
                if not media.fallback:
                    return _Outcome("refused", reason=f"could not make the picture ({media.error})")
                self._say(f"no picture for {media.label} — {media.error}; sent as text")
                delivery.media = media = None
            body = delivery.message.body
            late = self._clock.time() - delivery.message.queued_at
            if late >= _LATE_AFTER:
                marker = f"⏱ {int(late // 60)} min late"
                body = f"{marker}\n{body}" if body else marker
            if media is not None:
                if self._measure(body) <= self._max_caption:
                    delivery.parts, delivery.caption = [body], bool(body)
                    delivery.first_text = body
                else:   # the picture alone, then its text as replies to it
                    delivery.parts = ["", *split_text(
                        body, self._max_length, rich=self._rich, measure=self._measure
                    )]
                    delivery.caption = False
                    delivery.first_text = delivery.parts[1]
            else:
                if not _visible(body, self._rich).strip():
                    return _Outcome("refused", reason=f"{self._service} takes no pictures here, "
                                                      "and this one has no caption")
                delivery.parts = split_text(
                    body, self._max_length, rich=self._rich, measure=self._measure
                )
                delivery.first_text = delivery.parts[0]
            if delivery.reply is not None and self._can_reply:
                reply_to = delivery.reply()
        assert delivery.parts is not None
        text = delivery.parts[delivery.next_part]
        if delivery.media is not None:
            if delivery.next_part == 0:
                data = delivery.media.data
                assert data is not None
                return self._deliver_media(delivery.dest, delivery.media, data, text or None,
                                           silent=delivery.silent, reply_to=reply_to)
            if self._can_reply and delivery.first_id is not None:
                return self._deliver(delivery.dest, text, silent=delivery.silent,
                                     reply_to=delivery.first_id)
            return self._deliver(delivery.dest, text, silent=delivery.silent)
        if reply_to is not None:
            return self._deliver(delivery.dest, text, silent=delivery.silent, reply_to=reply_to)
        return self._deliver(delivery.dest, text, silent=delivery.silent)

    def _settle_locked(self, delivery: _Delivery, outcome: _Outcome) -> None:
        if self._in_flight is delivery:
            self._in_flight = None
        if delivery.done:   # close() gave it up while it was on its way
            return
        now = self._clock.monotonic()
        dest = delivery.dest
        pace = self._pace_of(dest)
        if outcome.status != "unreachable" and self._down_since is not None:
            self._lines.append(
                f"{self._service} reachable again — sending "
                f"{_plural(self._waiting, 'waiting message')}"
            )
            self._down_since = None
            self._down_failures = 0
        if outcome.status == "sent":
            pace.sent.append(now)
            dest.failures = 0
            if delivery.editing is not None:   # an edit: one request, and it is done
                delivery.first_id = delivery.text_id = delivery.editing
                delivery.caption = delivery.edit_caption
                self._finish_locked(delivery)
                return
            if delivery.next_part == 0:
                delivery.first_id = outcome.message_id
                if delivery.media is None or delivery.caption:
                    delivery.text_id = outcome.message_id
            elif delivery.next_part == 1 and delivery.media is not None:
                delivery.text_id = outcome.message_id   # the reply holding the caption
            delivery.next_part += 1
            assert delivery.parts is not None
            if delivery.next_part >= len(delivery.parts):
                self._finish_locked(delivery)
        elif outcome.status == "wait":
            pace.sent.append(now)
            pace.hold = now + max(outcome.retry_after, _RETRY_STEPS[0])
        elif outcome.status == "refused" and delivery.editing is not None:
            # The message to edit is gone, or may no longer be edited: the body goes
            # as a new message, in its turn.
            pace.sent.append(now)
            dest.failures = 0
            delivery.edit = delivery.editing = delivery.first_text = None
            delivery.edit_caption = False
        elif outcome.status == "refused":
            pace.sent.append(now)
            dest.failures = 0
            self._finish_locked(delivery, reason=outcome.reason)
            if delivery.waiter is None:
                self._failure_line_locked(dest, outcome.reason, now)
        elif outcome.status == "server":
            pace.sent.append(now)
            dest.failures += 1
            dest.retry_at = now + _RETRY_STEPS[min(dest.failures, len(_RETRY_STEPS)) - 1]
            if delivery.waiter is None:
                self._failure_line_locked(dest, outcome.reason, now)
            self._give_up_locked(now, outcome.reason, only=dest)
        else:
            if self._down_since is None:
                self._down_since = self._clock.time()
                self._down_failures = 0
                self._down_line_at = None
            self._down_failures += 1
            step = _RETRY_STEPS[min(self._down_failures, len(_RETRY_STEPS)) - 1]
            self._probe_at = now + step
            if self._down_line_at is None or now - self._down_line_at >= _LINE_EVERY:
                self._down_line_at = now
                self._lines.append(
                    f"{self._service} unreachable ({outcome.reason}) — "
                    f"{_plural(self._waiting, 'message')} waiting"
                )
            self._give_up_locked(now, f"{self._service} unreachable ({outcome.reason})", only=None)

    def _failure_line_locked(self, dest: _Destination, reason: str, now: float) -> None:
        if dest.line_at is not None and now - dest.line_at < _LINE_EVERY:
            return
        dest.line_at = now
        self._lines.append(
            f"could not send to {dest.label} — {reason} "
            f"({_plural(len(dest.waiting), 'message')} waiting)"
        )

    def _give_up_locked(self, now: float, reason: str, *, only: _Destination | None) -> None:
        """Withdraw what calls that waited 21 s or more are waiting for (Q1.5 b)."""
        for delivery in list(self._waited):
            if only is not None and delivery.dest is not only:
                continue
            assert delivery.waiter is not None
            if now - delivery.waiter.called < _GIVE_UP_SECONDS - 1e-6:
                continue
            if delivery.next_part == 0:
                self._finish_locked(delivery, reason=f"{reason}; not sent")
            else:
                assert delivery.parts is not None
                self._finish_locked(
                    delivery,
                    reason=f"{reason}; only {delivery.next_part} of "
                           f"{len(delivery.parts)} parts sent",
                )

    def _finish_locked(self, delivery: _Delivery, *, reason: str | None = None) -> None:
        if delivery.done:
            return
        delivery.done = True
        try:
            delivery.dest.waiting.remove(delivery)
        except ValueError:
            pass
        self._waiting -= 1
        self._waited.discard(delivery)
        if delivery.on_done is not None:
            if reason is None:
                sent = _Sent(delivery.first_id, delivery.first_text or "", delivery.text_id,
                             delivery.caption)
            else:
                sent = _Sent(None, None, None, False)
            self._callbacks.append((delivery.on_done, tuple(sent)))
        waiter = delivery.waiter
        if waiter is not None:
            if reason is None:
                waiter.sent[delivery.dest.name] = delivery.first_id
            else:
                waiter.failed[delivery.dest.name] = (
                    f"could not send to {delivery.dest.label} — {reason}"
                )
            waiter.pending -= 1
            if waiter.pending == 0:
                waiter.event.set()
        self._finished.notify_all()

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def close(self, timeout: float = 10.0) -> None:
        """Deliver what is queued, for up to *timeout* seconds, then stop the threads.

        What is still queued then is given up, with one line: «closed with 2
        messages unsent»; a call still waiting for one of them raises.  Daily jobs
        are cancelled, and commands are no longer listened to.  The notifier stays
        usable: the next send starts it again."""
        self._begin_close()
        self._end_close(time.monotonic() + max(0.0, float(timeout)))

    def __enter__(self) -> Notifier:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _begin_close(self) -> None:
        for alert in list(self._alerts):   # alerts end first: nothing new is queued
            alert.cancel()
        if self._commands is not None:      # nor does anything new come in
            self._commands.pause()
        with self._lock:
            self._wake_locked()

    def _end_close(self, deadline: float) -> None:
        self._stop_jobs()
        with self._lock:
            thread = self._thread
            if thread is None:
                return
            if thread is not threading.current_thread():
                while (
                    self._waiting or self._inbox or self._working or self._callbacks
                ) and thread.is_alive():
                    left = deadline - time.monotonic()
                    if left <= 0:
                        break
                    self._finished.wait(left)
            unsent = [delivery for delivery in self._order if not delivery.done]
            for delivery in unsent:
                self._finish_locked(delivery, reason=_CLOSED)
            self._order.clear()
            unsent_count = len(unsent) + len(self._inbox)   # work never run: one message each
            self._inbox.clear()
            self._callbacks.clear()
            if unsent_count:
                self._lines.append(f"closed with {_plural(unsent_count, 'message')} unsent")
            on_request = self._in_flight is not None
            self._generation += 1
            self._thread = None
            self._wake_locked()
        _open_notifiers.discard(self)
        if thread is threading.current_thread():
            return
        thread.join(_JOIN_GRACE if on_request else _JOIN_IDLE)
        if thread.is_alive():   # still on a request: say it here rather than never
            with self._lock:
                lines, self._lines = self._lines, []
            for line in lines:
                self._say(line)

    # ------------------------------------------------------------------
    # Price alerts (v1.2.0)
    # ------------------------------------------------------------------

    def watch(
        self,
        broker: Any,
        symbol: str,
        *,
        above: float | None = None,
        below: float | None = None,
        move_pct: float | None = None,
        window: str | None = None,
        on_close: str | None = None,
        repeat: bool = False,
        rearm: float = 0.1,
        image: bool | str = False,
        to: str | int | list | tuple | set | None = None,
        silent: bool | None = None,
    ) -> Any:
        """Send a message when *symbol*'s price does something, with no strategy and no
        trading; returns an :class:`Alert` at once.

        * ``above=65_000`` fires when the price crosses up to 65,000, and ``below=`` when
          it crosses down; both together fire on whichever comes first.  An alert set on
          the wrong side waits for the price to come back and cross.
        * ``move_pct=-3, window="1h"`` fires when the price is 3% below the price one hour
          ago (``+3``: above).
        * The price is the last trade, from the forming 1m candle; ``on_close="1h"``
          watches closed 1h candles instead.
        * Once, unless ``repeat=True``: then again after the price has gone back past the
          level by ``rearm`` percent (a move: after a window, and back short of it by
          ``rearm``).
        * ``image=True`` adds a picture of the last 100 candles with the level (15m, or
          ``on_close``'s timeframe; ``image="1h"`` picks one).
        * ``to`` and ``silent`` as for :meth:`send_text`.  The kind is ``alert``.

        The broker's stream runs on its own thread; :meth:`wait` keeps a script alive.
        A wrong symbol, a broker that cannot stream, or a picture engine that is not
        installed raises here."""
        from . import _alerts

        if self._commands is not None:   # after a close(), listening starts again
            self._commands.resume()
        return _alerts.watch(
            self, broker, symbol, above=above, below=below, move_pct=move_pct,
            window=window, on_close=on_close, repeat=repeat, rearm=rearm, image=image,
            to=to, silent=silent,
        )

    def alerts(self) -> list[Any]:
        """The alerts of this notifier that are still armed."""
        with self._alerts_changed:
            return [alert for alert in self._alerts if alert.state == "armed"]

    def cancel(self, alert: Any) -> None:
        """End *alert*, as ``alert.cancel()`` does."""
        if getattr(alert, "notifier", None) is not self:
            raise ValueError(f"{alert!r} is not an alert of this notifier")
        alert.cancel()

    def wait(self) -> None:
        """Block until no alert is left armed — each once-alert has fired, or been
        cancelled — or until Ctrl+C, which cancels them.  Then the alerts' last messages
        get up to 10 seconds to go out."""
        from . import _alerts

        _alerts.wait(self)

    def _alert_added(self, alert: Any) -> None:
        with self._alerts_changed:
            self._alerts.append(alert)
            self._alerts_changed.notify_all()

    def _alert_ended(self, alert: Any) -> None:
        with self._alerts_changed:
            if alert in self._alerts:
                self._alerts.remove(alert)
            self._alerts_changed.notify_all()

    def _drain(self, timeout: float) -> None:
        """Until what is queued has gone out, or *timeout* seconds have passed."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._lock:
            while (self._waiting or self._inbox or self._working or self._callbacks) and (
                    self._thread is not None and self._thread.is_alive()):
                left = deadline - time.monotonic()
                if left <= 0:
                    return
                self._finished.wait(left)

    # ------------------------------------------------------------------
    # Daily jobs
    # ------------------------------------------------------------------

    def add_daily_job(self, job: Callable[[], Any], at: str = "00:00") -> Callable[[], None]:
        """Run ``job()`` every day at *at* (``"HH:MM"``) in the notifier's zone.

        Jobs run on a thread of their own, so a slow job never delays a message,
        and a job may send.  A time missed while the machine slept runs once on
        waking, not once per missed day.  Returns a callable that cancels the job;
        ``close()`` cancels them all."""
        if not callable(job):
            raise TypeError(f"add_daily_job needs a callable, got {job!r}")
        match = _HHMM.fullmatch(at) if isinstance(at, str) else None
        if match is None:
            raise ValueError(f'at must be a time of day such as "00:00" or "18:30", got {at!r}')
        hour, minute = int(match.group(1)), int(match.group(2))
        entry = _Job(job, hour, minute, self._next_daily(hour, minute, self._clock.time()))
        with self._jobs_lock:
            self._jobs.append(entry)
            if self._jobs_thread is None or not self._jobs_thread.is_alive():
                self._jobs_thread = threading.Thread(
                    target=self._run_jobs, args=(self._jobs_generation,),
                    name=f"atk-notify-{self._tag}-jobs", daemon=True,
                )
                self._jobs_thread.start()
            self._jobs_cond.notify_all()

        def cancel() -> None:
            with self._jobs_lock:
                if entry in self._jobs:
                    self._jobs.remove(entry)
                self._jobs_cond.notify_all()

        return cancel

    def _next_daily(self, hour: int, minute: int, after: float) -> float:
        """The first moment after *after* whose wall-clock time in the zone is hour:minute."""
        zone = self._zone or timezone.utc
        day = datetime.fromtimestamp(after, tz=zone).date()
        while True:
            moment = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
            if moment.timestamp() > after:
                return moment.timestamp()
            day += timedelta(days=1)

    def _run_jobs(self, generation: int) -> None:
        while True:
            with self._jobs_lock:
                if generation != self._jobs_generation:
                    return
                now = self._clock.time()
                self._jobs_seen = now
                due = [entry for entry in self._jobs if entry.next_at <= now]
                for entry in due:
                    entry.next_at = self._next_daily(entry.hour, entry.minute, now)
                if not due:
                    wake = min((entry.next_at for entry in self._jobs), default=None)
                    timeout = _JOBS_RECHECK if wake is None else min(wake - now, _JOBS_RECHECK)
                    self._jobs_in_wait = True
                    try:
                        self._clock.wait(self._jobs_cond, max(0.0, timeout))
                    finally:
                        self._jobs_in_wait = False
                    continue
            for entry in due:
                try:
                    entry.job()
                except Exception as exc:  # noqa: BLE001 — one failing job never stops the others
                    name = getattr(entry.job, "__name__", "job")
                    self._say(f"daily job {name} failed ({type(exc).__name__}: {exc})")

    def _stop_jobs(self) -> None:
        with self._jobs_lock:
            self._jobs.clear()
            self._jobs_generation += 1
            self._jobs_thread = None
            self._jobs_cond.notify_all()
