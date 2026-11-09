"""
Tests for v1.3.0 Part 4d — MetaTrader's clock.

MetaTrader stamps candles and ticks in the trade server's own time: the broker's
wall clock written as if it were UTC (two hours ahead in winter at many brokers).
``MetaTraderBroker.clock_offset_ms()`` reads that clock from the newest tick, so a
candle is final when it closes on the server — the trader's scheduler fired two
hours late before.  A fake terminal stands in for MT5: its ticks are two hours
ahead of this machine's (fake) clock.
"""
from __future__ import annotations

import pytest

import AlgoTradeKit.broker.base as base_module
import AlgoTradeKit.broker.metatrader._client as client_module
import AlgoTradeKit.trader._scheduler as sched_module
from AlgoTradeKit.broker import ConnectionFailed
from AlgoTradeKit.broker.base import last_closed_candles
from AlgoTradeKit.broker.metatrader import MetaTraderBroker
from AlgoTradeKit.trader._scheduler import CandleCloseScheduler

HOUR = 3_600_000
MINUTE = 60_000
TF_5M = 5 * MINUTE
#: 2026-10-30 22:45 on the trade server (its stamp) — 20:45 UTC
STAMP_2245 = 1_793_400_300_000
#: 2026-10-30 20:47:10 UTC, this machine's clock at the start of every test
UTC_START = 1_793_393_230_000


class Clock:
    """This machine's clock, moved by the test and by the code's sleeps."""

    def __init__(self, ms: int) -> None:
        self.ms = ms

    def __call__(self) -> int:
        return self.ms

    def sleep(self, seconds: float) -> None:
        self.ms += round(seconds * 1000)


class Terminal:
    """A MetaTrader terminal whose trade server runs *ahead_ms* ahead of UTC.

    While the market is open a new tick comes every *every_ms* and the newest is
    *age_ms* old; a candle prints *lag_ms* after it opens.  Shut, the last tick
    stays as it was when the market shut.
    """

    def __init__(self, clock: Clock, *, ahead_ms: int = 2 * HOUR, every_ms: int = 500,
                 age_ms: int = 3_000, lag_ms: int = 0) -> None:
        self.clock, self.ahead_ms, self.every_ms = clock, ahead_ms, every_ms
        self.age_ms, self.lag_ms = age_ms, lag_ms
        self.shut_at: int | None = None        # UTC ms when the market shut
        self.fail = False
        self.calls: list[tuple] = []

    def shut(self, ago_ms: int = 0) -> None:
        self.shut_at = self.clock.ms - ago_ms

    def open(self) -> None:
        self.shut_at = None

    def server_now(self) -> int:
        return self.clock.ms + self.ahead_ms

    def _tick(self) -> dict:
        utc = (self.shut_at if self.shut_at is not None else self.clock.ms) - self.age_ms
        utc -= utc % self.every_ms                       # the newest tick's moment
        n = utc // self.every_ms
        return {"bid": 2000 + n % 997 / 100, "ask": 2000.3 + n % 997 / 100, "last": 0.0,
                "time_ms": (utc + self.ahead_ms) // 1000 * 1000}   # whole seconds, as MT5

    def _rows(self, tf: str) -> list[dict]:
        tf_ms = {"1m": MINUTE, "5m": TF_5M}[tf]
        newest = self.server_now() - self.lag_ms
        newest -= newest % tf_ms
        return [{"timestamp": t, "open": 2000.0, "high": 2001.0, "low": 1999.0,
                 "close": 2000.5, "volume": 1.0}
                for t in range(newest - 40 * tf_ms, newest + 1, tf_ms)]

    def call(self, method, *args, **kwargs):
        self.calls.append((method, *args))
        if self.fail:
            raise ConnectionFailed("the bridge is down")
        if method == "tick":
            return self._tick()
        if method == "candles_from_count":
            symbol, tf, count = args
            return self._rows(tf)[-int(count):]
        if method == "candles_range":
            symbol, tf, start, end = args
            return [r for r in self._rows(tf) if start <= r["timestamp"] <= end]
        raise NotImplementedError(method)

    def ticks_read(self) -> int:
        return sum(1 for c in self.calls if c[0] == "tick")


@pytest.fixture
def clock(monkeypatch) -> Clock:
    now = Clock(UTC_START)
    for module in (client_module, base_module, sched_module):
        monkeypatch.setattr(module, "now_ms", now)
    monkeypatch.setattr(client_module, "_sleep", now.sleep)
    return now


def _broker(terminal: Terminal, symbol: str = "XAUUSD") -> MetaTraderBroker:
    broker = MetaTraderBroker(transport=terminal, connect=False)
    if symbol:
        broker.fetch_last_candles(symbol, "5m", 3)      # what a seed does first
    return broker


class TestTheClock:
    def test_a_server_two_hours_ahead_reads_two_hours(self, clock):
        terminal = Terminal(clock)
        broker = _broker(terminal)
        assert broker.clock_offset_ms() == 2 * HOUR
        assert broker.server_time() == clock.ms + 2 * HOUR
        assert ("tick", "XAUUSD") in terminal.calls

    @pytest.mark.parametrize("ahead_ms", [2 * HOUR, 3 * HOUR, 0, -int(3.5 * HOUR),
                                          int(5.75 * HOUR), 13 * HOUR])
    def test_any_whole_half_or_quarter_hour(self, clock, ahead_ms):
        assert _broker(Terminal(clock, ahead_ms=ahead_ms)).clock_offset_ms() == ahead_ms

    def test_a_tick_minutes_old_cannot_move_it(self, clock):
        terminal = Terminal(clock, age_ms=6 * MINUTE)    # a feed six minutes behind
        assert _broker(terminal).clock_offset_ms() == 2 * HOUR

    def test_a_quiet_market_is_read_on_the_next_look(self, clock):
        """A tick a minute: none comes in the two seconds watched, so the clock
        stays this machine's for a minute; then the tick is new since that look."""
        terminal = Terminal(clock, every_ms=MINUTE)
        broker = _broker(terminal)
        assert broker.clock_offset_ms() == 0
        clock.ms += 61_000
        assert broker.clock_offset_ms() == 2 * HOUR

    def test_it_reads_the_symbol_last_asked_for(self, clock):
        terminal = Terminal(clock)
        broker = _broker(terminal, symbol="")
        assert broker.clock_offset_ms() == 0             # nothing asked for: this machine's
        assert terminal.calls == []
        broker.get_ticker("EURUSD")
        clock.ms += 61_000
        assert broker.clock_offset_ms() == 2 * HOUR
        assert terminal.calls[-1] == ("tick", "EURUSD")

    def test_a_reading_is_kept_five_minutes(self, clock):
        terminal = Terminal(clock)
        broker = _broker(terminal)
        broker.clock_offset_ms()
        read = terminal.ticks_read()
        clock.ms += 4 * MINUTE
        assert broker.clock_offset_ms() == 2 * HOUR
        assert terminal.ticks_read() == read             # not read again
        assert broker.clock_offset_ms(force_refresh=True) == 2 * HOUR
        assert terminal.ticks_read() == read + 1         # a new tick since: no watching


class TestTheMarketShut:
    def test_while_the_market_is_shut_the_last_reading_stands(self, clock):
        terminal = Terminal(clock)
        broker = _broker(terminal)
        assert broker.clock_offset_ms() == 2 * HOUR
        terminal.shut()
        clock.ms += 10 * MINUTE                          # the reading is old
        assert broker.clock_offset_ms() == 2 * HOUR      # kept: no new tick came
        read = terminal.ticks_read()
        clock.ms += 30_000
        assert broker.clock_offset_ms() == 2 * HOUR
        assert terminal.ticks_read() == read             # looked again only after a minute
        clock.ms += 31_000
        assert broker.clock_offset_ms() == 2 * HOUR
        assert terminal.ticks_read() == read + 1         # one look, no watching

    def test_started_at_the_weekend_it_is_this_machines_clock_until_ticks_come(self, clock):
        terminal = Terminal(clock)
        terminal.shut(ago_ms=40 * HOUR)                  # Friday's last tick
        broker = _broker(terminal)
        start = clock.ms
        assert broker.clock_offset_ms() == 0
        assert clock.ms == start                         # days old: not even watched for
        terminal.open()
        clock.ms += 61_000
        assert broker.clock_offset_ms() == 2 * HOUR      # the first new tick sets it

    def test_a_tick_from_before_a_break_is_not_taken_for_the_clock(self, clock):
        """Gold's daily break: the newest tick is 40 minutes old, so its time minus
        now reads 1:20 — a quarter-hour clock of 1:15 if it were taken."""
        terminal = Terminal(clock)
        terminal.shut(ago_ms=40 * MINUTE)
        broker = _broker(terminal)
        assert broker.clock_offset_ms() == 0
        assert terminal.ticks_read() == 1 + 10           # watched two seconds, once
        terminal.open()
        clock.ms += 61_000
        assert broker.clock_offset_ms() == 2 * HOUR

    def test_a_failing_bridge_keeps_the_last_reading(self, clock):
        terminal = Terminal(clock)
        broker = _broker(terminal)
        assert broker.clock_offset_ms() == 2 * HOUR
        terminal.fail = True
        clock.ms += 6 * MINUTE
        assert broker.clock_offset_ms() == 2 * HOUR


class TestOnTheServersClock:
    def test_a_live_seed_keeps_a_closed_candle_the_server_has_not_followed_yet(self, clock):
        """Server 22:50:20, no 22:50 candle yet (it prints with its first tick): the
        22:45 candle is closed.  On this machine's clock it looked like the
        forming one and the seed dropped it."""
        terminal = Terminal(clock, lag_ms=30_000)
        broker = _broker(terminal)
        clock.ms = STAMP_2245 - 2 * HOUR + 5 * MINUTE + 20_000          # 20:50:20 UTC
        rows = last_closed_candles(broker, "XAUUSD", "5m", 3)
        assert rows[-1]["timestamp"] == STAMP_2245
        clock.ms = STAMP_2245 - 2 * HOUR + 2 * MINUTE                    # 20:47 UTC: forming
        rows = last_closed_candles(broker, "XAUUSD", "5m", 3)
        assert rows[-1]["timestamp"] == STAMP_2245 - TF_5M

    def test_the_traders_scheduler_sees_the_close_when_it_happens(self, clock):
        """The 22:45 candle closes at 22:50 on the server — 20:50 UTC.  The
        scheduler fired at 22:50 UTC, two hours late, before this part."""
        terminal = Terminal(clock)
        broker = _broker(terminal)
        lines: list[str] = []
        sched = CandleCloseScheduler(broker, "XAUUSD", "5m",
                                     last_open_ms=STAMP_2245 - TF_5M, log=lines.append)

        def wait(seconds: float) -> bool:
            clock.sleep(seconds)
            return False

        sched._wait = wait
        tick = sched.wait_next_close()
        closed_utc = STAMP_2245 + TF_5M - 2 * HOUR                       # 20:50:00 UTC
        assert 0 <= clock.ms - closed_utc < 1_000
        assert tick.boundary_ms == STAMP_2245 + TF_5M
        assert [c["timestamp"] for c in tick.candles] == [STAMP_2245]
