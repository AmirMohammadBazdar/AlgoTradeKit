"""
AlgoTradeKit.notify._template
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Placeholders and formatting for notification text (v1.2.0).

A template is Python ``str.format`` syntax with **named fields only**::

    "{time} · {symbol} {price} — watching the 64k level"

``{{`` and ``}}`` write a literal brace.  Positional fields (``{}``, ``{0}``),
attribute and index access (``{a.b}``, ``{a[0]}``), conversions (``{price!r}``)
and fields inside a format spec (``{price:{width}}``) are refused: a template is
data, not code.  A name nobody knows raises at the call, so a misspelt
``{prise}`` never sends half a message.

Values print for a person to read, not for ``awk``:

* numbers (:class:`Number`) — thousands separators, the value's own decimals,
  trailing zeros trimmed, at most 8 decimals: ``64,312.5``, ``1.08345``.  An
  explicit spec formats the raw number: ``{price:.1f}`` → ``64312.5``;
* times (:class:`Moment`) — ``2026-10-14 14:03 UTC``, or in a notifier's zone
  ``2026-10-14 17:33 (UTC+03:30)``, with the offset of that moment.  A spec is a
  ``strftime`` pattern in the same zone: ``{time:%H:%M}``;
* dates (:class:`Day`) — ``2026-10-14``; ``{date:%d %b}`` → ``14 Oct``.

With rich text on, every field's value is escaped (``&``, ``<``, ``>``) and the
template's own text is left alone: its tags work, and a symbol or a venue's
message can never break the markup.

The engine is not tied to custom texts: :func:`fields_of` and :func:`render`
take any set of field names, and a custom text (``send_text``) is the case that
knows :data:`TEXT_FIELDS`.
"""

from __future__ import annotations

import html
import string
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: The fields of a custom text, in the order an error lists them.
TEXT_FIELDS = ("time", "date", "symbol", "price", "header")

#: What ``{header}`` holds: a first line with the time, the symbol and its price.
HEADER_TEMPLATE = "🕑 {time} · {symbol} {price}"

#: Most decimals a number prints with when the template gives no spec.
_MAX_DECIMALS = 8

_FORMATTER = string.Formatter()


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------

def human_number(value: float) -> str:
    """``64312.5`` → ``64,312.5``: separators, own decimals, at most 8 of them."""
    value = float(value)
    if value != value or value in (float("inf"), float("-inf")):
        return str(value)
    text = f"{value:,.{_MAX_DECIMALS}f}".rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


class Number(float):
    """A float that prints for a person (:func:`human_number`) unless a spec is given."""

    __slots__ = ()

    def __format__(self, spec: str) -> str:
        return float.__format__(self, spec) if spec else human_number(self)

    def __str__(self) -> str:
        return human_number(self)


def offset_label(moment: datetime) -> str:
    """The UTC offset of an aware datetime: ``UTC+03:30``, ``UTC-05:00``."""
    offset = moment.utcoffset() or timedelta(0)
    minutes = int(offset.total_seconds()) // 60
    sign = "+" if minutes >= 0 else "-"
    hours, rest = divmod(abs(minutes), 60)
    return f"UTC{sign}{hours:02d}:{rest:02d}"


class Moment:
    """A point in time for a person to read, in a notifier's zone (``None`` = UTC)."""

    __slots__ = ("_moment", "_utc")

    def __init__(self, epoch_seconds: float, zone: tzinfo | None) -> None:
        self._utc = zone is None
        self._moment = datetime.fromtimestamp(epoch_seconds, tz=zone or timezone.utc)

    def __format__(self, spec: str) -> str:
        if spec:
            return self._moment.strftime(spec)
        stamp = self._moment.strftime("%Y-%m-%d %H:%M")
        return f"{stamp} UTC" if self._utc else f"{stamp} ({offset_label(self._moment)})"

    def __str__(self) -> str:
        return format(self, "")


class Day(Moment):
    """The date of a moment, in the same zone: ``2026-10-14``."""

    __slots__ = ()

    def __format__(self, spec: str) -> str:
        return self._moment.strftime(spec or "%Y-%m-%d")


def parse_zone(name: str | None) -> tzinfo | None:
    """``None`` or ``"UTC"`` → ``None`` (times print «… UTC»); an IANA name → its zone.

    An unknown name raises ``ValueError``.  On Windows, ``zoneinfo`` reads the
    ``tzdata`` package, which pandas already installs there."""
    if name is None:
        return None
    if not isinstance(name, str):
        raise TypeError(f"timezone must be a zone name such as 'Asia/Tehran', got {name!r}")
    if name.strip().upper() == "UTC":
        return None
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError):
        pass
    raise ValueError(
        f"unknown time zone {name!r} — use an IANA name such as 'Asia/Tehran', or 'UTC'"
    )


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

def fields_of(template: str, known: Sequence[str]) -> list[str]:
    """The field names *template* uses, in order of first use.

    Raises ``ValueError`` for anything but a plain named field, and for a name
    outside *known*: «unknown field {prise} — known: time, date, symbol, price,
    header»."""
    if not isinstance(template, str):
        raise TypeError(f"the text must be a string, got {type(template).__name__}")
    try:
        pieces = list(_FORMATTER.parse(template))
    except ValueError:
        pieces = None
    if pieces is None:
        raise ValueError("the text has a single { or } — write {{ or }} for a literal brace")
    names: list[str] = []
    for _literal, name, spec, conversion in pieces:
        if name is None:
            continue
        shown = "{" + name + "}"
        if name == "" or name.isdigit():
            raise ValueError(f"positional field {shown} — name the field, such as {{time}}")
        if not name.isidentifier():
            raise ValueError(
                f"{shown} is not allowed — a template is data, not code: "
                "use a plain field such as {price}"
            )
        if conversion is not None:
            raise ValueError(f"{{{name}!{conversion}}} — conversions are not allowed")
        if spec and ("{" in spec or "}" in spec):
            raise ValueError(f"{{{name}:{spec}}} — a field inside a format spec is not allowed")
        if name not in known:
            raise ValueError(f"unknown field {shown} — known: {', '.join(known)}")
        if name not in names:
            names.append(name)
    return names


def render(template: str, values: Mapping[str, Any], *, escape: bool) -> str:
    """Fill *template* (already checked by :func:`fields_of`) with *values*.

    With *escape*, every value is HTML-escaped after formatting; the template's
    own text is left as written."""
    out: list[str] = []
    for literal, name, spec, _conversion in _FORMATTER.parse(template):
        out.append(literal)
        if name is None:
            continue
        text = format(values[name], spec or "")
        out.append(html.escape(text, quote=False) if escape else text)
    return "".join(out)


def text_values(
    used: Iterable[str],
    *,
    now: float,
    zone: tzinfo | None,
    symbol: str | None,
    broker: Any,
) -> dict[str, Any]:
    """The values a custom text's *used* fields need, each worked out once.

    ``{price}``, and ``{header}`` which holds it, is ``broker.get_ticker(symbol).last``,
    asked now on the caller's thread; a broker error propagates as the broker
    raised it.  A field the call cannot fill raises ``ValueError`` before anything
    is fetched."""
    used = list(used)
    for name in used:
        if name == "symbol" and not symbol:
            raise ValueError("{symbol} needs symbol=")
        if name in ("price", "header") and (broker is None or not symbol):
            raise ValueError(f"{{{name}}} needs broker= and symbol=")
    wanted = set(used)
    if "header" in wanted:
        wanted |= {"time", "symbol", "price"}
    values: dict[str, Any] = {}
    if "time" in wanted:
        values["time"] = Moment(now, zone)
    if "date" in wanted:
        values["date"] = Day(now, zone)
    if "symbol" in wanted:
        values["symbol"] = str(symbol)
    if "price" in wanted:
        values["price"] = Number(broker.get_ticker(symbol).last)
    if "header" in wanted:
        values["header"] = render(HEADER_TEMPLATE, values, escape=False)
    return values
