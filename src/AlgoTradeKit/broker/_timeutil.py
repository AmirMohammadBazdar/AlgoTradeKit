"""
Self-contained time / timeframe helpers for the ``broker`` module.

Intentionally duplicated from ``data._utils`` (a small subset) so that
``broker`` never imports from ``data``.  The dependency edge is
``data ──► broker`` only; importing the other way would create a cycle
(``data/__init__`` imports ``Collector`` which imports ``broker``).

All timestamps are UTC **milliseconds**, matching the rest of the library.
"""
from __future__ import annotations

from datetime import datetime, timezone

#: Milliseconds per fixed-length timeframe ("1M" omitted — variable length).
TIMEFRAME_MS: dict[str, int] = {
    "1m":  60_000,
    "3m":  3  * 60_000,
    "5m":  5  * 60_000,
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1h":  60 * 60_000,
    "2h":  2  * 60 * 60_000,
    "4h":  4  * 60 * 60_000,
    "6h":  6  * 60 * 60_000,
    "8h":  8  * 60 * 60_000,
    "12h": 12 * 60 * 60_000,
    "1d":  24 * 60 * 60_000,
    "3d":  3  * 24 * 60 * 60_000,
    "1w":  7  * 24 * 60 * 60_000,
}

VALID_TIMEFRAMES: frozenset[str] = frozenset(TIMEFRAME_MS) | {"1M"}

_PARSE_FORMATS = [
    "%Y/%m/%d",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
]


def normalize_timeframe(tf: str) -> str:
    """Normalise a timeframe string (``"1D"`` → ``"1d"``; ``"1M"`` preserved)."""
    aliases = {"1mo": "1M", "1month": "1M", "monthly": "1M"}
    if tf in aliases:
        tf = aliases[tf]
    elif tf != "1M":
        tf = tf.lower()
    if tf not in VALID_TIMEFRAMES:
        raise ValueError(
            f"Unsupported timeframe '{tf}'. Valid: {sorted(VALID_TIMEFRAMES)}"
        )
    return tf


def parse_to_ms(dt_input) -> int:
    """Convert str / datetime / int(ms) / float(seconds) to a UTC ms timestamp."""
    if isinstance(dt_input, bool):  # guard: bool is an int subclass
        raise TypeError("Boolean is not a valid datetime input.")
    if isinstance(dt_input, int):
        return dt_input
    if isinstance(dt_input, float):
        return int(dt_input * 1000)
    if isinstance(dt_input, datetime):
        if dt_input.tzinfo is None:
            dt_input = dt_input.replace(tzinfo=timezone.utc)
        return int(dt_input.timestamp() * 1000)
    if isinstance(dt_input, str):
        for fmt in _PARSE_FORMATS:
            try:
                dt = datetime.strptime(dt_input.strip(), fmt)
                return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)
            except ValueError:
                continue
        raise ValueError(
            f"Cannot parse datetime string '{dt_input}'. "
            "Use YYYY/MM/DD or YYYY-MM-DD (optionally with HH:MM[:SS])."
        )
    raise TypeError(f"Unsupported type for datetime: {type(dt_input)}")


def ms_to_dt(ms: int) -> datetime:
    """Convert a millisecond timestamp to a UTC ``datetime``."""
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def now_ms() -> int:
    """Current UTC time as a millisecond timestamp."""
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def next_open_ms(last_open_ms: int, timeframe: str) -> int:
    """Expected open-time of the candle AFTER ``last_open_ms`` (calendar-aware for 1M)."""
    if timeframe == "1M":
        dt = ms_to_dt(last_open_ms)
        year, month = dt.year, dt.month
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
        return int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp() * 1000)
    return last_open_ms + TIMEFRAME_MS[timeframe]
