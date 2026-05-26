from __future__ import annotations
from datetime import datetime, timezone
from typing import Optional


# ---------------------------------------------------------------------------
# Timeframe constants
# ---------------------------------------------------------------------------

#: Milliseconds per fixed-length timeframe.
#: "1M" (monthly) is intentionally omitted because months have variable length.
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

#: All valid timeframe identifiers (after normalisation).
VALID_TIMEFRAMES: frozenset[str] = frozenset(TIMEFRAME_MS) | {"1M"}

#: Human-readable labels for display.
TIMEFRAME_LABELS: dict[str, str] = {
    "1m": "1 Minute",   "3m": "3 Minutes",  "5m": "5 Minutes",
    "15m": "15 Minutes","30m": "30 Minutes",
    "1h": "1 Hour",     "2h": "2 Hours",    "4h": "4 Hours",
    "6h": "6 Hours",    "8h": "8 Hours",    "12h": "12 Hours",
    "1d": "1 Day",      "3d": "3 Days",     "1w": "1 Week",
    "1M": "1 Month",
}


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def normalize_timeframe(tf: str) -> str:
    """
    Normalise a user-supplied timeframe string.

    Rules
    -----
    * ``"1M"`` → monthly (kept as-is, case-sensitive to distinguish from ``"1m"`` minute).
    * Everything else is lowercased: ``"1D"`` → ``"1d"``, ``"4H"`` → ``"4h"``.
    * ``"1mo"`` and ``"1month"`` are accepted as aliases for ``"1M"``.

    Raises
    ------
    ValueError
        If the resulting string is not a recognised timeframe.
    """
    aliases = {"1mo": "1M", "1month": "1M", "monthly": "1M"}
    if tf in aliases:
        tf = aliases[tf]
    elif tf != "1M":
        tf = tf.lower()

    if tf not in VALID_TIMEFRAMES:
        valid = sorted(VALID_TIMEFRAMES)
        raise ValueError(
            f"Unsupported timeframe '{tf}'. "
            f"Valid options: {valid}"
        )
    return tf


# ---------------------------------------------------------------------------
# Datetime helpers
# ---------------------------------------------------------------------------

_PARSE_FORMATS = [
    "%Y/%m/%d",
    "%Y-%m-%d",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
]


def parse_to_ms(dt_input) -> int:
    """
    Convert various date/time representations to a UTC millisecond timestamp.

    Accepts
    -------
    * ``str``  – ``"2020/01/01"``, ``"2020-01-01 08:30"`` etc.
    * ``datetime`` – timezone-aware or naive (assumed UTC).
    * ``int``  – already a millisecond timestamp, returned unchanged.
    * ``float``  – treated as seconds since epoch, converted to ms.

    Raises
    ------
    ValueError
        If the string cannot be parsed.
    TypeError
        If the type is not supported.
    """
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
            "Use YYYY/MM/DD or YYYY-MM-DD (optionally with HH:MM or HH:MM:SS)."
        )
    raise TypeError(f"Unsupported type for datetime: {type(dt_input)}")


def ms_to_dt(ms: int) -> datetime:
    """Convert a millisecond timestamp to a UTC ``datetime``."""
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def now_ms() -> int:
    """Current UTC time as a millisecond timestamp."""
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def floor_to_tf(ms: int, timeframe_ms: int) -> int:
    """Round ``ms`` down to the nearest timeframe boundary."""
    return (ms // timeframe_ms) * timeframe_ms


def ceil_to_tf(ms: int, timeframe_ms: int) -> int:
    """Round ``ms`` up to the nearest timeframe boundary (or return as-is if already aligned)."""
    floored = floor_to_tf(ms, timeframe_ms)
    return floored if floored == ms else floored + timeframe_ms
