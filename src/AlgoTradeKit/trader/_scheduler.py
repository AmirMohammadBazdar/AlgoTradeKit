"""
Exact candle-close scheduler (— "the late problem is very important").

:class:`CandleCloseScheduler` drives the ``candle_close`` execution mode
and provides the closed-candle arithmetic used everywhere a
"is this bar final?" guard is needed:

* **Venue clock, never the local clock** — every boundary is computed
  against ``now_ms() + broker.clock_offset_ms()``.  The offset is
  refreshed once per wait, *before* sleeping, so a TTL re-measurement can
  never land inside the fine-wait and delay a fire.
* **Fire at the boundary immediately** — coarse sleep until ``fine_wait_ms``
  before the boundary, then a fine wait (``fine_tick_ms`` steps) that fires
  the instant the venue clock reaches the boundary.  Never early (a candle
  is only final at its close), never late.
* **Tail fetches only** — a handful of candles via ``fetch_last_candles``;
  after downtime the missed range is gap-filled with one ranged
  ``fetch_candles`` call over exactly the gap — never the full history.
* **Arithmetic finality (mandatory venue quirk)** — some venues do not
  print a candle until its first trade, so at 17:35:01 the newest row may
  still be the 17:30 bar.  A candle with open time ``T`` is final once
  ``venue_now >= T + tf`` — never "a newer row appeared".  The 17:30 candle
  is evaluated at 17:35:00 even when no 17:35 row exists yet.  If the
  venue's tail response itself lags the just-closed bar, the fetch is
  retried on a growing backoff (``retry_initial_ms``, doubling up to
  ``retry_cap_ms``) until the expected bar arrives — the delay is logged.
* **Give-up + gap-fill recovery** — a bar that never prints (no trades in
  the interval: dead pair, forex weekend) must not hold the loop hostage.
  The retry ends early when a *later* bar appears (venues print bars in
  order, so a later bar proves the missing interval had no trades) or after
  ``fetch_timeout`` seconds.  An incomplete tick defers the next fire to
  the next future grid point (one bounded attempt per boundary, not a spin),
  and because the scheduler never advances past a bar that may still print,
  a late bar is recovered by the next tick's gap-fill.

**Boundary grid** — the phase comes from the venue's own candles:
boundaries are ``last_handled_open + k*tf``, re-anchored to the real open
time of every candle the scheduler returns.  MT5 server-timezone alignment
(4h/1d bars on EET) and Binance's Monday-open weeks are therefore handled
without any timezone knowledge, and a venue re-phase (DST) self-corrects as
soon as the first re-phased bar is returned.  ``"1M"`` is rejected — it has
no fixed length (``fetch_last_candles`` cannot serve it either).

Internal machinery for — not exported from ``AlgoTradeKit.trader``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..broker._timeutil import TIMEFRAME_MS, normalize_timeframe, now_ms

#: Candles per tail fetch; a gap larger than this switches to a ranged fetch.
DEFAULT_TAIL_COUNT = 5
#: Coarse sleep ends this many ms before the boundary; the fine wait covers the rest.
DEFAULT_FINE_WAIT_MS = 500
#: Fine-wait step size, ms.
DEFAULT_FINE_TICK_MS = 10
#: First retry delay when the venue's tail response lags the closed bar, ms.
DEFAULT_RETRY_INITIAL_MS = 250
#: Retry delay ceiling (backoff doubles until here), ms.
DEFAULT_RETRY_CAP_MS = 5_000
#: Total retry budget per boundary, seconds; then give up (gap-fill recovers later).
DEFAULT_FETCH_TIMEOUT = 30.0
#: Fires later than this past the boundary are logged as late, ms.
DEFAULT_LATE_WARN_MS = 1_000


# ---------------------------------------------------------------------------
# Pure boundary arithmetic (venue-agnostic; reuses these as guards)
# ---------------------------------------------------------------------------


def timeframe_ms(timeframe: str) -> int:
    """Fixed length of *timeframe* in ms.  Rejects ``"1M"`` (variable length)."""
    tf = normalize_timeframe(timeframe)
    if tf not in TIMEFRAME_MS:
        raise ValueError(
            f"Timeframe '{tf}' has no fixed length — the candle-close scheduler "
            "cannot compute boundaries for it."
        )
    return TIMEFRAME_MS[tf]


def candle_close_ms(open_ms: int, tf_ms: int) -> int:
    """Close time (= next candle's open) of the candle opening at *open_ms*."""
    return open_ms + tf_ms


def is_candle_final(open_ms: int, tf_ms: int, venue_now_ms: int) -> bool:
    """
    Arithmetic finality: the candle opening at *open_ms* is final once
    ``venue_now >= open + tf``.  Deliberately needs no candle rows — "a newer
    row appeared" is never the criterion (the mandatory venue quirk).
    """
    return venue_now_ms >= open_ms + tf_ms


def next_boundary_ms(last_open_ms: int, tf_ms: int) -> int:
    """
    Earliest future boundary once the candle opening at *last_open_ms* has
    been handled: the close of the candle *after* it (``last_open + 2*tf``).
    """
    return last_open_ms + 2 * tf_ms


def newest_final_open_ms(anchor_open_ms: int, tf_ms: int, venue_now_ms: int) -> int | None:
    """
    Open time of the newest final candle on the grid anchored at
    *anchor_open_ms* (grid = ``anchor + k*tf``, k >= 0), or ``None`` when even
    the anchor's own candle is not final yet.
    """
    k = (venue_now_ms - anchor_open_ms - tf_ms) // tf_ms
    if k < 0:
        return None
    return anchor_open_ms + k * tf_ms


# ---------------------------------------------------------------------------
# Tick result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SchedulerTick:
    """
    One fired candle-close boundary.

    boundary_ms
        The grid boundary this tick fired for (venue clock, UTC ms).
    fired_at_ms
        Venue time when the wait ended (always >= ``boundary_ms``).
    late_ms
        ``fired_at_ms - boundary_ms``; 0 = fired exactly on time.  Large
        values mean downtime or a slow caller — the candles list carries the
        whole gap-fill.
    candles
        Newly closed candles, ascending by ``timestamp``, library-standard
        dicts straight from the broker.  May be empty (interval printed no
        candle, or the fetch gave up — see ``complete``).
    retries
        Extra fetch attempts after the first (venue tail lag, item 4).
    fetch_delay_ms
        Venue-clock delay between the fire and the fetch settling; 0 when the
        first attempt had the data.
    complete
        True — nothing is outstanding: the expected newest candle was fetched
        or proven absent (a later bar exists, so its interval had no trades).
        False — the retry budget ran out with the expected candle still
        missing; the scheduler did not advance past it, so if the bar ever
        prints it arrives via the next tick's gap-fill.
    """

    boundary_ms: int
    fired_at_ms: int
    late_ms: int
    candles: tuple[dict[str, Any], ...]
    retries: int
    fetch_delay_ms: int
    complete: bool


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


class CandleCloseScheduler:
    """
    Blocking exact-time candle-close scheduler for one ``(broker, symbol,
    timeframe)`` — the worker loop calls :meth:`wait_next_close` in a loop
    and evaluates the strategy on every returned tick.

    Parameters
    ----------
    broker
        Any object with the :class:`~AlgoTradeKit.broker.BaseBroker` market-
        data surface (``clock_offset_ms``, ``fetch_last_candles``,
        ``fetch_candles``) — duck-typed like everywhere else in the library.
    symbol / timeframe
        The pair to schedule.  *timeframe* must have a fixed length
        (``"1M"`` is rejected) and is normalised (``"1H"`` → ``"1h"``).
    last_open_ms
        Open time of the newest candle the caller already handled (e.g. the
        last row of the seeded history).  It anchors the boundary grid and
        the gap-fill dedup.  ``None`` → the first wait baselines itself on
        the venue's newest *final* candle; history never fires.
    stop_event
        Optional shared :class:`threading.Event`; :meth:`stop` sets it and
        any sleeping/retrying wait returns ``None`` promptly.
    log
        ``Callable[[str], None]`` for operational messages (late fires,
        venue lag, gap-fills, give-ups).  Default prints an
        ``[AlgoTradeKit]``-prefixed line.
    Remaining keyword knobs default to the module constants above.
    """

    def __init__(
        self,
        broker: Any,
        symbol: str,
        timeframe: str,
        *,
        last_open_ms: int | None = None,
        tail_count: int = DEFAULT_TAIL_COUNT,
        fine_wait_ms: float = DEFAULT_FINE_WAIT_MS,
        fine_tick_ms: float = DEFAULT_FINE_TICK_MS,
        retry_initial_ms: float = DEFAULT_RETRY_INITIAL_MS,
        retry_cap_ms: float = DEFAULT_RETRY_CAP_MS,
        fetch_timeout: float = DEFAULT_FETCH_TIMEOUT,
        late_warn_ms: float = DEFAULT_LATE_WARN_MS,
        stop_event: threading.Event | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        if broker is None:
            raise ValueError("CandleCloseScheduler needs a broker instance.")
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("CandleCloseScheduler.symbol must be a non-empty string.")
        self._broker = broker
        self._symbol = symbol.strip()
        self._tf_ms = timeframe_ms(timeframe)  # validates + rejects "1M"
        self._timeframe = normalize_timeframe(timeframe)

        self._tail_count = int(tail_count)
        if self._tail_count < 1:
            raise ValueError(f"tail_count must be >= 1, got {tail_count!r}.")
        self._fine_wait_ms = float(fine_wait_ms)
        self._fine_tick_ms = float(fine_tick_ms)
        self._retry_initial_ms = float(retry_initial_ms)
        self._retry_cap_ms = float(retry_cap_ms)
        self._fetch_timeout = float(fetch_timeout)
        self._late_warn_ms = float(late_warn_ms)
        if self._fine_wait_ms <= 0 or self._fine_tick_ms <= 0:
            raise ValueError("fine_wait_ms / fine_tick_ms must be > 0.")
        if self._retry_initial_ms <= 0:
            raise ValueError(f"retry_initial_ms must be > 0, got {retry_initial_ms!r}.")
        if self._retry_cap_ms < self._retry_initial_ms:
            raise ValueError("retry_cap_ms must be >= retry_initial_ms.")
        if self._fetch_timeout <= 0:
            raise ValueError(f"fetch_timeout must be > 0 seconds, got {fetch_timeout!r}.")
        if self._late_warn_ms < 0:
            raise ValueError(f"late_warn_ms must be >= 0, got {late_warn_ms!r}.")
        if log is not None and not callable(log):
            raise ValueError("log must be callable (or None for the default printer).")

        self._processed_open_ms = None if last_open_ms is None else int(last_open_ms)
        self._defer_until_ms = 0
        self._stop_event = stop_event if stop_event is not None else threading.Event()
        self._log_sink = log

    # -- introspection --------------------------------------------------

    @property
    def symbol(self) -> str:
        return self._symbol

    @property
    def timeframe(self) -> str:
        """Normalised timeframe string."""
        return self._timeframe

    @property
    def last_open_ms(self) -> int | None:
        """Open time of the newest handled candle (grid anchor + dedup mark)."""
        return self._processed_open_ms

    @property
    def stopped(self) -> bool:
        return self._stop_event.is_set()

    def venue_now_ms(self) -> int:
        """Current venue time: ``now_ms() + broker.clock_offset_ms()``."""
        return now_ms() + int(self._broker.clock_offset_ms())

    def stop(self) -> None:
        """Interrupt any sleeping/retrying wait; it returns ``None`` promptly."""
        self._stop_event.set()

    # -- the one blocking call -------------------------------------------

    def wait_next_close(self) -> SchedulerTick | None:
        """
        Block until the next candle-close boundary on the **venue clock**,
        then fetch and return every newly closed candle (gap-filled after
        downtime, retried on venue lag).  Returns ``None`` when stopped.
        """
        if self._stop_event.is_set():
            return None
        # One offset refresh per wait, before sleeping — never inside the
        # fine wait (a TTL re-measurement there would blow the deadline).
        offset = int(self._broker.clock_offset_ms())
        if self._processed_open_ms is None:
            self._init_baseline(offset)

        boundary = max(
            next_boundary_ms(self._processed_open_ms, self._tf_ms),
            self._defer_until_ms,
        )
        if self._sleep_until(boundary, offset):
            return None
        fired_at = now_ms() + offset
        late = max(0, fired_at - boundary)
        if late > self._late_warn_ms:
            self._log(f"late fire: {late} ms past boundary {boundary} — catching up")

        expected = newest_final_open_ms(self._processed_open_ms, self._tf_ms, fired_at)
        candles, complete, retries, delay = self._collect_closed(expected, fired_at, offset)
        if candles is None:  # stopped mid-retry
            return None
        if candles:
            self._processed_open_ms = int(candles[-1]["timestamp"])
        elif complete:
            # Interval(s) proven trade-less: skip the grid forward.
            self._processed_open_ms = expected
        if complete:
            self._defer_until_ms = 0
        else:
            self._defer_until_ms = self._next_grid_after(now_ms() + offset)
        return SchedulerTick(
            boundary_ms=boundary,
            fired_at_ms=fired_at,
            late_ms=late,
            candles=tuple(candles),
            retries=retries,
            fetch_delay_ms=delay,
            complete=complete,
        )

    # -- internals -------------------------------------------------------

    def _log(self, message: str) -> None:
        line = f"[AlgoTradeKit] scheduler {self._symbol} {self._timeframe}: {message}"
        (self._log_sink or print)(line)

    def _wait(self, seconds: float) -> bool:
        """Stop-aware sleep; True when the stop event fired (tests patch this)."""
        return self._stop_event.wait(max(seconds, 0.0))

    def _next_grid_after(self, venue_now_ms: int) -> int:
        """Smallest grid point (``processed + k*tf``) strictly after *venue_now_ms*."""
        k = (venue_now_ms - self._processed_open_ms) // self._tf_ms + 1
        return self._processed_open_ms + k * self._tf_ms

    def _init_baseline(self, offset: int) -> None:
        """No ``last_open_ms`` given: anchor on the venue's newest final candle."""
        raw = self._broker.fetch_last_candles(self._symbol, self._timeframe, self._tail_count)
        venue_now = now_ms() + offset
        finals = [
            int(c["timestamp"])
            for c in raw
            if is_candle_final(int(c["timestamp"]), self._tf_ms, venue_now)
        ]
        if not finals:
            raise ValueError(
                f"CandleCloseScheduler: {self._symbol} {self._timeframe} returned no "
                "closed candle to baseline on — cannot schedule."
            )
        self._processed_open_ms = max(finals)
        self._log(
            f"baseline: newest closed candle open={self._processed_open_ms} — "
            "scheduling from its next boundary"
        )

    def _sleep_until(self, boundary_ms: int, offset: int) -> bool:
        """Coarse sleep to ``boundary - fine_wait``, then fine-wait to the boundary.

        Venue-clock arithmetic throughout (``local = venue - offset``).  Returns
        True when stopped.  Loops re-compute the remainder each pass, so a system
        suspend or an early wake cannot cause a premature fire.
        """
        coarse_deadline_local = boundary_ms - self._fine_wait_ms - offset
        while True:
            if self._stop_event.is_set():
                return True
            remaining_ms = coarse_deadline_local - now_ms()
            if remaining_ms <= 0:
                break
            if self._wait(remaining_ms / 1000.0):
                return True
        while True:
            if self._stop_event.is_set():
                return True
            if now_ms() + offset >= boundary_ms:
                return False
            if self._wait(self._fine_tick_ms / 1000.0):
                return True

    def _collect_closed(
        self, expected_open_ms: int, fired_at_ms: int, offset: int
    ) -> tuple[list[dict[str, Any]] | None, bool, int, int]:
        """
        Fetch every final candle in ``(processed, expected]``, retrying on venue
        lag per item 4.  Returns ``(candles, complete, retries, delay_ms)``;
        ``candles is None`` means the scheduler was stopped mid-retry.
        """
        tf = self._tf_ms
        processed = self._processed_open_ms
        missed = (expected_open_ms - processed) // tf
        deadline = fired_at_ms + int(self._fetch_timeout * 1000)
        retry_wait_s = self._retry_initial_ms / 1000.0
        retries = 0
        collected: dict[int, dict[str, Any]] = {}
        proof = False  # a bar newer than `expected` exists → missing intervals are trade-less

        while True:
            if self._stop_event.is_set():
                return None, False, retries, 0
            raw: list[dict[str, Any]] | None = None
            try:
                if missed >= self._tail_count:
                    if retries == 0:
                        self._log(
                            f"gap-fill: {missed} candle(s) missed since open={processed} "
                            "— fetching the gap range"
                        )
                    raw = self._broker.fetch_candles(
                        self._symbol, self._timeframe, processed + tf, now_ms() + offset
                    )
                else:
                    raw = self._broker.fetch_last_candles(
                        self._symbol, self._timeframe, self._tail_count
                    )
            except Exception as exc:  # one bad fetch must not kill the loop
                self._log(f"fetch failed ({exc!r}) — retrying")
            if raw is not None:
                for candle in raw:
                    ts = int(candle["timestamp"])
                    if processed < ts <= expected_open_ms:
                        collected[ts] = candle
                    elif ts > expected_open_ms:
                        proof = True
                if expected_open_ms in collected or proof:
                    break
            venue_now = now_ms() + offset
            if venue_now >= deadline:
                candles = [collected[ts] for ts in sorted(collected)]
                self._log(
                    f"expected candle open={expected_open_ms} still missing after "
                    f"{venue_now - fired_at_ms} ms — giving up; the next boundary's "
                    "gap-fill recovers it if it ever prints"
                )
                return candles, False, retries, venue_now - fired_at_ms
            if self._wait(min(retry_wait_s, (deadline - venue_now) / 1000.0)):
                return None, False, retries, 0
            retries += 1
            retry_wait_s = min(retry_wait_s * 2.0, self._retry_cap_ms / 1000.0)

        venue_now = now_ms() + offset
        delay = venue_now - fired_at_ms if retries else 0
        if retries:
            self._log(
                f"expected candle open={expected_open_ms} arrived {delay} ms after the "
                f"boundary ({retries} retries)"
            )
        if proof and expected_open_ms not in collected:
            self._log(
                f"interval(s) up to open={expected_open_ms} printed no candle "
                "(no trades) — skipped"
            )
        candles = [collected[ts] for ts in sorted(collected)]
        return candles, True, retries, delay
