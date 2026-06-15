"""
AlgoTradeKit.simulate._session
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Forex / crypto trading-session detection from a UTC timestamp.

Sessions (all times UTC)
------------------------
- **Sydney**  : 21:00 – 06:00 (next day)
- **Tokyo**   : 00:00 – 09:00
- **London**  : 07:00 – 16:00
- **New York**: 13:00 – 22:00

Overlap periods are detected as a set — a timestamp may belong to multiple
sessions simultaneously (e.g. London-NY overlap: 13:00 – 16:00).

Usage::

    sessions = get_sessions(timestamp_ms)
    # → frozenset of session name strings, e.g. frozenset({"london", "new_york"})

    label = get_primary_session(timestamp_ms)
    # → single session name (most liquid) or "off_hours"
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Session name constants
# ---------------------------------------------------------------------------

SESSION_SYDNEY = "sydney"
SESSION_TOKYO = "tokyo"
SESSION_LONDON = "london"
SESSION_NEW_YORK = "new_york"
SESSION_OFF_HOURS = "off_hours"

# Priority order (highest liquidity first) for get_primary_session()
_SESSION_PRIORITY = [SESSION_NEW_YORK, SESSION_LONDON, SESSION_TOKYO, SESSION_SYDNEY]

# Session windows: list of (start_hour_utc, end_hour_utc)
# End hour is exclusive.  Hours that span midnight use two ranges.
_SESSION_WINDOWS: dict[str, list[tuple[int, int]]] = {
    # Sydney: 21:00-24:00 and 00:00-06:00
    SESSION_SYDNEY:   [(21, 24), (0, 6)],
    # Tokyo: 00:00-09:00
    SESSION_TOKYO:    [(0, 9)],
    # London: 07:00-16:00
    SESSION_LONDON:   [(7, 16)],
    # New York: 13:00-22:00
    SESSION_NEW_YORK: [(13, 22)],
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_sessions(timestamp_ms: int) -> frozenset[str]:
    """
    Return the set of Forex sessions active at *timestamp_ms* (UTC ms).

    Parameters
    ----------
    timestamp_ms : int
        Candle open-time in UTC milliseconds.

    Returns
    -------
    frozenset[str]
        Session name strings.  Empty set = off-hours.
    """
    hour_utc = (timestamp_ms // 3_600_000) % 24   # extract UTC hour

    active: set[str] = set()
    for session_name, windows in _SESSION_WINDOWS.items():
        for start, end in windows:
            if start <= hour_utc < end:
                active.add(session_name)
                break

    return frozenset(active)


def get_primary_session(timestamp_ms: int) -> str:
    """
    Return the single most-liquid session active at *timestamp_ms*.

    When multiple sessions overlap, the highest-priority one is returned
    (priority: New York > London > Tokyo > Sydney).

    Parameters
    ----------
    timestamp_ms : int
        Candle open-time in UTC milliseconds.

    Returns
    -------
    str
        One of: ``"new_york"``, ``"london"``, ``"tokyo"``, ``"sydney"``,
        or ``"off_hours"``.
    """
    active = get_sessions(timestamp_ms)
    for session in _SESSION_PRIORITY:
        if session in active:
            return session
    return SESSION_OFF_HOURS


def get_weekday_name(timestamp_ms: int) -> str:
    """
    Return the UTC weekday name for *timestamp_ms*.

    Returns
    -------
    str
        One of ``"monday"``, ``"tuesday"``, ..., ``"sunday"``.
    """
    _DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    days_since_epoch = timestamp_ms // 86_400_000
    # Unix epoch (1970-01-01) was a Thursday (index 3)
    weekday = (days_since_epoch + 3) % 7
    return _DAYS[weekday]


def get_month_key(timestamp_ms: int) -> str:
    """
    Return a ``"YYYY-MM"`` string for *timestamp_ms* (UTC).

    Uses a simple integer-only calculation to avoid datetime imports.
    """
    # Days since epoch → approximate year/month via integer arithmetic
    total_days = timestamp_ms // 86_400_000
    # Gregorian calendar approximation
    year = 1970
    remaining = total_days
    while True:
        days_in_year = 366 if _is_leap(year) else 365
        if remaining < days_in_year:
            break
        remaining -= days_in_year
        year += 1

    _month_days = [31, 29 if _is_leap(year) else 28, 31, 30, 31, 30,
                   31, 31, 30, 31, 30, 31]
    month = 0
    for i, md in enumerate(_month_days):
        if remaining < md:
            month = i + 1
            break
        remaining -= md

    return f"{year:04d}-{month:02d}"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_leap(year: int) -> bool:
    return (year % 4 == 0 and year % 100 != 0) or (year % 400 == 0)
