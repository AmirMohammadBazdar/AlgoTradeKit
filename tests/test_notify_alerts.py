"""
Tests for v1.2.0 Part 4 — price alerts without a strategy: ``notifier.watch(…)``.

A fake broker stands in for the venue: ``get_ticker`` and ``get_candles_df`` answer from
the test's numbers, and ``stream_candles`` delivers the prices a test pushes one at a time,
on a thread of the stream's own, as a real stream does.  The Bot API is the suite's fake
(Part 3's, which takes pictures), and the picture engine Part 3's fake.  Nothing here
touches a real venue or the real Telegram.
"""
from __future__ import annotations

import importlib.util
import queue
import threading
import time
from datetime import timedelta

import pandas as pd
import pytest
from test_notify import CHANNEL, ME, TOKEN, FakeClock
from test_notify_events import FastClock, at, drained, ms
from test_notify_pictures import FakeEngine, PictureTelegram

from AlgoTradeKit.broker import NotSupportedError, Ticker
from AlgoTradeKit.notify import (
    KIND_ALERT,
    Alert,
    TelegramChat,
    TelegramNotifier,
    _alerts,
    _core,
)
from AlgoTradeKit.visual import Chart, _snapshot

MINUTE = 60_000
COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


# ---------------------------------------------------------------------------
# The fake venue
# ---------------------------------------------------------------------------

class FeedStream:
    """One subscription: what a test pushes is delivered on this stream's own thread."""

    def __init__(self, symbol, timeframe, on_candle, closed_only) -> None:
        self.symbol, self.timeframe, self.closed_only = symbol, timeframe, closed_only
        self.on_candle = on_candle
        self.stopped = threading.Event()
        self._queue: queue.Queue = queue.Queue()
        self._idle = threading.Event()
        self._idle.set()
        self.thread = threading.Thread(target=self._run, name="feed-stream", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        while not self.stopped.is_set():
            try:
                candle = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                self.on_candle(candle)
            finally:
                if self._queue.empty():
                    self._idle.set()
        self._idle.set()   # a stopped stream delivers nothing more, as a real one

    def push(self, close: float, *, timestamp: int = 0, closed: bool | None = None) -> None:
        self._idle.clear()
        self._queue.put({"timestamp": timestamp, "open": close, "high": close, "low": close,
                         "close": close, "volume": 1.0,
                         "closed": self.closed_only if closed is None else closed})

    def settle(self, timeout: float = 5.0) -> None:
        """Until every pushed price has been delivered."""
        assert self._idle.wait(timeout), "the feed did not deliver"

    def stop(self, timeout: float = 5.0) -> None:
        self.stopped.set()

    @property
    def alive(self) -> bool:
        return not self.stopped.is_set()


class FakeVenue:
    """What alerts need of a broker, driven by the test."""

    name = "fake-venue"

    def __init__(self, start: float = 65_200.0, history: list[dict] | None = None,
                 streams: bool = True) -> None:
        self.start = start
        self.history = list(history or [])
        self.can_stream = streams
        self.asked: list[tuple] = []
        self.streams: list[FeedStream] = []

    def get_ticker(self, symbol: str) -> Ticker:
        self.asked.append(("ticker", symbol, threading.current_thread().name))
        if symbol not in ("BTCUSDT", "ETHUSDT"):
            raise ValueError("Binance rejected the request (400): code=-1121 msg=Invalid symbol.")
        return Ticker(symbol=symbol, last=self.start)

    def get_candles_df(self, symbol, timeframe, start_ms, end_ms) -> pd.DataFrame:
        self.asked.append(("candles", symbol, timeframe, start_ms, end_ms,
                           threading.current_thread().name))
        rows = [row for row in self.history if start_ms <= row["timestamp"] <= end_ms]
        return pd.DataFrame(rows, columns=COLUMNS)

    def stream_candles(self, symbol, timeframe, on_candle, *, closed_only=True):
        if not self.can_stream:
            raise NotSupportedError(f"{self.name} does not support candle streaming.")
        stream = FeedStream(symbol, timeframe, on_candle, closed_only)
        self.streams.append(stream)
        return stream

    def stream_ticker(self, symbol, on_tick):
        raise AssertionError("alerts read the last trade from the candle stream (Q4.2 b)")


def minutes(start, closes) -> list[dict]:
    """1m candles opening at *start*, one a minute, with these closes."""
    first = ms(start)
    return [{"timestamp": first + k * MINUTE, "open": c, "high": c, "low": c, "close": c,
             "volume": 1.0} for k, c in enumerate(closes)]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_proxies(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                 "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


@pytest.fixture(scope="module")
def server():
    fake = PictureTelegram()
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture
def tg(server):
    if not server.running:
        server.start()
    server.reset()
    yield server
    if not server.running:
        server.start()


@pytest.fixture
def clock():
    return FastClock(at(14, 0))


@pytest.fixture
def make(tg, clock, monkeypatch):
    """Notifiers on the fake, on the fast clock; every one is closed after the test, which
    cancels its alerts."""
    made: list[TelegramNotifier] = []

    def build(chats=None, **kwargs) -> TelegramNotifier:
        kwargs.setdefault("base_url", tg.url)
        kwargs.setdefault("timeout", 2.0)
        monkeypatch.setattr(TelegramNotifier, "_clock", clock)
        tg.clock = clock
        bot = TelegramNotifier(TOKEN, {"me": ME} if chats is None else chats, **kwargs)
        bot._clock = clock
        clock.jump_for.add(bot._cond)
        made.append(bot)
        return bot

    yield build
    for bot in made:
        bot.close(timeout=0.5)


@pytest.fixture
def engine(monkeypatch):
    fake = FakeEngine()
    monkeypatch.setattr(_snapshot, "snapshot", fake.snapshot)
    monkeypatch.setattr(_snapshot, "check_engine", lambda: None)
    return fake


def stopped_soon(stream: FeedStream, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while stream.alive and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not stream.alive, "the stream did not stop"


def push(venue: FakeVenue, *closes: float, bot=None, **options) -> None:
    """Push prices into the venue's one stream, and wait until they are delivered and what
    they fired has gone out."""
    (stream,) = [s for s in venue.streams if s.alive]
    for close in closes:
        stream.push(close, **options)
    stream.settle()
    if bot is not None:
        drained(bot)


# ===========================================================================
# What fires (Q4.1 a)
# ===========================================================================

class TestWhatFires:
    def test_q41_the_worked_example(self, make, tg, capsys):
        """At 65,200, above=65,000 waits; 64,900 then 65,050 crosses; the rest is quiet."""
        venue = FakeVenue(start=65_200.0)
        bot = make()
        alert = bot.watch(venue, "BTCUSDT", above=65_000)
        assert ("[AlgoTradeKit] alerts: BTCUSDT is already above 65,000 (65,200) — the alert "
                "fires when it crosses again") in capsys.readouterr().out
        push(venue, 64_900, 65_050, bot=bot)
        assert tg.texts(ME) == ["<b>🔔 BTCUSDT crossed above 65,000 · now 65,050</b>\n"
                                "🕑 2026-10-14 14:00 UTC · fake-venue"]
        assert (alert.state, alert.fired, alert.last_price) == ("fired", 1, 65_050)
        assert bot.alerts() == []
        stopped_soon(venue.streams[0])                 # its last alert ended: it stops

    def test_an_exact_level_counts(self, make, tg):
        venue = FakeVenue(start=64_000.0)
        bot = make()
        bot.watch(venue, "BTCUSDT", above=65_000)
        push(venue, 64_999.5, 65_000, bot=bot)
        assert tg.texts(ME)[0].startswith("<b>🔔 BTCUSDT crossed above 65,000 · now 65,000</b>")

    def test_repeat_fires_again_after_the_rearm_band(self, make, tg):
        """rearm=0.1% of 65,000: back to 64,935 or less before it fires again."""
        venue = FakeVenue(start=64_000.0)
        bot = make()
        alert = bot.watch(venue, "BTCUSDT", above=65_000, repeat=True)
        push(venue, 65_050, 64_950, 65_010, bot=bot)          # 64,950: not back far enough
        assert len(tg.texts(ME)) == 1
        push(venue, 64_930, 65_001, bot=bot)                  # re-armed, and crossed again
        texts = tg.texts(ME)
        assert len(texts) == 2 and "now 65,001" in texts[1]
        assert (alert.state, alert.fired) == ("armed", 2)

    def test_below(self, make, tg):
        venue = FakeVenue(start=61_000.0)
        bot = make()
        bot.watch(venue, "BTCUSDT", below=60_000)
        push(venue, 60_500, 59_990, bot=bot)
        assert tg.texts(ME)[0].startswith("<b>🔔 BTCUSDT crossed below 60,000 · now 59,990</b>")

    def test_both_levels_fire_on_whichever_comes_first_once(self, make, tg):
        venue = FakeVenue(start=65_000.0)
        bot = make()
        alert = bot.watch(venue, "BTCUSDT", above=66_000, below=64_000)
        assert alert.condition == "above 66,000 or below 64,000"
        push(venue, 66_100, 63_900, bot=bot)
        assert [t.splitlines()[0] for t in tg.texts(ME)] == [
            "<b>🔔 BTCUSDT crossed above 66,000 · now 66,100</b>"]
        assert alert.state == "fired"

    def test_on_close_watches_closed_candles(self, make, tg):
        venue = FakeVenue(start=64_000.0)
        bot = make()
        alert = bot.watch(venue, "BTCUSDT", above=65_000, on_close="1h")
        (stream,) = venue.streams
        assert (stream.timeframe, stream.closed_only) == ("1h", True)
        assert alert.condition == "1h close above 65,000"
        push(venue, 65_012.5, timestamp=ms(at(13)), bot=bot)
        assert tg.texts(ME)[0].startswith(
            "<b>🔔 BTCUSDT 1h closed above 65,000 · now 65,012.5</b>")


# ===========================================================================
# Which price (Q4.2 b)
# ===========================================================================

class TestWhichPrice:
    def test_q42_the_last_trade_from_the_forming_1m_candle(self, make):
        venue = FakeVenue(start=64_000.0)
        make().watch(venue, "BTCUSDT", above=65_000)
        (stream,) = venue.streams
        assert (stream.symbol, stream.timeframe, stream.closed_only) == ("BTCUSDT", "1m", False)

    def test_the_symbol_and_the_starting_side_come_from_one_ticker_at_the_call(self, make):
        venue = FakeVenue(start=64_000.0)
        make().watch(venue, "BTCUSDT", above=65_000)
        ((what, symbol, thread),) = [a for a in venue.asked if a[0] == "ticker"]
        assert (what, symbol, thread) == ("ticker", "BTCUSDT", threading.current_thread().name)


# ===========================================================================
# A move over a window (Q4.3 a)
# ===========================================================================

def the_hour() -> list[dict]:
    """1m candles 12:59 to 13:59: 65,000 at 13:00, up to 66,000 at 13:30, down to 64,000 at
    14:00 — each close stamped at its candle's end."""
    closes = ([65_000.0] + [65_000.0 + 1_000.0 * k / 30 for k in range(1, 31)]
              + [66_000.0 - 2_000.0 * k / 30 for k in range(1, 31)])
    return minutes(at(12, 59), closes)


class TestMoves:
    def test_q43_against_the_price_one_window_ago(self, make, tg, clock):
        """−1.54% against 13:00's 65,000 does not fire (−3.03% against 13:30's high would);
        63,050 at 14:00:30 is −3.0% and does."""
        clock.set(at(14, 0, 0))
        venue = FakeVenue(start=64_000.0, history=the_hour())
        bot = make()
        alert = bot.watch(venue, "BTCUSDT", move_pct=-3, window="1h")
        assert alert.condition == "-3% in 1h"
        ((_, _, timeframe, start_ms, end_ms, _),) = [a for a in venue.asked if a[0] == "candles"]
        assert timeframe == "1m" and start_ms <= ms(at(12, 59)) and end_ms == ms(at(14))
        drained(bot)
        assert tg.texts(ME) == [] and alert.state == "armed"
        clock.set(at(14, 0, 30))
        push(venue, 63_050, bot=bot)
        assert tg.texts(ME) == ["<b>🔔 BTCUSDT fell 3.0% in 1h · now 63,050</b>\n"
                                "🕑 2026-10-14 14:00 UTC · fake-venue"]

    def test_a_move_already_made_fires_at_once(self, make, tg, clock):
        clock.set(at(14, 0, 0))
        venue = FakeVenue(start=64_000.0, history=minutes(at(12, 59), [67_000.0] * 61))
        bot = make()
        alert = bot.watch(venue, "BTCUSDT", move_pct=-3, window="1h")
        drained(bot)
        assert tg.texts(ME)[0].startswith("<b>🔔 BTCUSDT fell 4.5% in 1h · now 64,000</b>")
        assert alert.state == "fired"

    def test_a_rise(self, make, tg, clock):
        clock.set(at(14, 0, 0))
        venue = FakeVenue(start=65_000.0, history=minutes(at(12, 59), [65_000.0] * 61))
        bot = make()
        bot.watch(venue, "BTCUSDT", move_pct=2, window="1h")
        clock.set(at(14, 0, 10))
        push(venue, 66_400, bot=bot)
        assert tg.texts(ME)[0].startswith("<b>🔔 BTCUSDT rose 2.2% in 1h · now 66,400</b>")

    def test_a_repeating_move_rearms_after_a_window_and_back(self, make, tg, clock):
        clock.set(at(14, 0, 0))
        venue = FakeVenue(start=65_000.0, history=minutes(at(12, 59), [65_000.0] * 61))
        bot = make()
        alert = bot.watch(venue, "BTCUSDT", move_pct=-1, window="1m", repeat=True, rearm=0.1)
        clock.set(at(14, 1, 5))
        push(venue, 64_300, bot=bot)          # −1.08% against 14:00's 65,000: fires
        clock.set(at(14, 1, 30))
        push(venue, 64_000, bot=bot)          # −1.5%, but within the window it fired in
        assert len(tg.texts(ME)) == 1
        clock.set(at(14, 2, 40))
        push(venue, 64_300, bot=bot)          # a window later, and +0.5% against 14:01:30:
        clock.set(at(14, 3, 45))              # back short of −1% by 0.1 — re-armed
        push(venue, 63_600, bot=bot)          # −1.09% against 14:02:40: fires again
        assert len(tg.texts(ME)) == 2 and alert.fired == 2 and alert.state == "armed"


# ===========================================================================
# Streams
# ===========================================================================

class TestStreams:
    def test_two_alerts_share_one_stream_which_stops_with_the_last(self, make):
        venue = FakeVenue(start=64_000.0)
        bot = make()
        high = bot.watch(venue, "BTCUSDT", above=65_000)
        low = bot.watch(venue, "BTCUSDT", below=60_000)
        (stream,) = venue.streams
        high.cancel()
        time.sleep(0.1)
        assert stream.alive and bot.alerts() == [low]
        bot.cancel(low)
        stopped_soon(stream)
        assert bot.alerts() == []

    def test_a_quiet_stream_gets_one_line_every_10_minutes(self, make, monkeypatch, capsys):
        quiet = FakeClock()
        monkeypatch.setattr(_alerts, "_clock", quiet)
        monkeypatch.setattr(_alerts, "LOOK_EVERY", 0.01)
        bot = make()
        bot.watch(FakeVenue(start=64_000.0), "BTCUSDT", above=65_000)
        line = ("[AlgoTradeKit] alerts: no BTCUSDT price from fake-venue for 60 s — the feed "
                "keeps reconnecting")

        def lines_after(seconds: float, expected: int) -> int:
            # A watchdog left from an earlier test may still be asleep for its old 5 s
            quiet.advance(seconds)
            seen, deadline = 0, time.monotonic() + (7.0 if expected else 0.5)
            while time.monotonic() < deadline:
                seen += capsys.readouterr().out.count(line)
                if expected and seen >= expected:
                    break
                time.sleep(0.02)
            return seen

        assert lines_after(30, 0) == 0
        assert lines_after(31, 1) == 1
        assert lines_after(120, 0) == 0       # once in 10 minutes
        assert lines_after(600, 1) == 1

    def test_the_stream_thread_only_compares_and_posts(self, make, tg):
        """Telegram takes 5 s a request: the stream delivers the firing price at once."""
        venue = FakeVenue(start=64_000.0)
        bot = make(check=False, timeout=10.0)
        tg.when("sendMessage", delay=5.0)
        bot.watch(venue, "BTCUSDT", above=65_000, repeat=True, rearm=0)
        (stream,) = venue.streams
        started = time.perf_counter()
        for price in (65_100, 64_900, 65_200, 64_800, 65_300):
            stream.push(price)
        stream.settle()
        assert time.perf_counter() - started < 0.5

    def test_a_broker_that_cannot_stream_raises_at_the_call(self, make):
        bot = make()
        with pytest.raises(NotSupportedError, match="does not support candle streaming"):
            bot.watch(FakeVenue(start=64_000.0, streams=False), "BTCUSDT", above=65_000)
        assert bot.alerts() == []

    def test_a_wrong_symbol_raises_at_the_call(self, make):
        venue = FakeVenue()
        bot = make()
        with pytest.raises(ValueError, match="Invalid symbol"):
            bot.watch(venue, "BTCUSTD", above=65_000)
        assert venue.streams == [] and bot.alerts() == []


# ===========================================================================
# Handles, wait() and close() (Q4.5 a, Q4.6 a)
# ===========================================================================

class TestHandles:
    def test_an_alert_handle(self, make):
        bot = make()
        alert = bot.watch(FakeVenue(start=64_000.0), "BTCUSDT", above=65_000)
        assert isinstance(alert, Alert)
        assert repr(alert) == "<Alert BTCUSDT above 65,000 · armed>"
        assert bot.alerts() == [alert]
        alert.cancel()
        assert alert.state == "cancelled" and bot.alerts() == []
        alert.cancel()                                      # twice is fine

    def test_cancel_takes_only_its_own_alerts(self, make):
        mine, other = make(), make()
        alert = other.watch(FakeVenue(start=64_000.0), "BTCUSDT", above=65_000)
        with pytest.raises(ValueError, match="is not an alert of this notifier"):
            mine.cancel(alert)

    def test_wait_returns_once_the_last_once_alert_has_fired_and_gone_out(self, make, tg):
        venue = FakeVenue(start=64_000.0)
        bot = make()
        bot.watch(venue, "BTCUSDT", above=65_000)
        threading.Timer(0.2, lambda: venue.streams[0].push(65_100)).start()
        started = time.monotonic()
        bot.wait()
        assert time.monotonic() - started < 10
        assert len(tg.texts(ME)) == 1                       # delivered before wait returned

    def test_ctrl_c_in_wait_cancels_the_alerts(self, make, monkeypatch, capsys):
        bot = make()
        venue = FakeVenue(start=64_000.0)
        first = bot.watch(venue, "BTCUSDT", above=65_000)
        second = bot.watch(venue, "BTCUSDT", below=60_000)

        def ctrl_c(condition, timeout):
            raise KeyboardInterrupt

        monkeypatch.setattr(_alerts, "_wait_for", ctrl_c)
        bot.wait()
        assert (first.state, second.state) == ("cancelled", "cancelled")
        assert "[AlgoTradeKit] alerts: Ctrl+C — 2 alerts cancelled" in capsys.readouterr().out

    def test_close_cancels_the_alerts_and_stops_their_streams(self, make):
        venue = FakeVenue(start=64_000.0)
        bot = make()
        alert = bot.watch(venue, "BTCUSDT", above=65_000)
        bot.close(timeout=0.5)
        assert alert.state == "cancelled"
        stopped_soon(venue.streams[0])


# ===========================================================================
# The message: its kind, its text, its chats
# ===========================================================================

class TestTheMessage:
    def test_alert_is_a_kind_in_the_default_set(self, make, tg):
        assert KIND_ALERT == "alert" and KIND_ALERT in _core.KINDS
        venue = FakeVenue(start=64_000.0)
        bot = make({"me": ME, "signals": TelegramChat(CHANNEL, events={"signal"})})
        bot.watch(venue, "BTCUSDT", above=65_000)
        push(venue, 65_100, bot=bot)
        assert len(tg.texts(ME)) == 1 and tg.texts(CHANNEL) == []
        assert "disable_notification" not in tg.got(ME)[0]["params"]   # it rings

    def test_to_and_silent(self, make, tg):
        venue = FakeVenue(start=64_000.0)
        bot = make({"me": ME, "signals": TelegramChat(CHANNEL, events={"signal"})})
        bot.watch(venue, "BTCUSDT", above=65_000, to="signals", silent=True)
        push(venue, 65_100, bot=bot)
        assert tg.texts(ME) == [] and len(tg.texts(CHANNEL)) == 1
        assert tg.got(CHANNEL)[0]["params"]["disable_notification"] is True

    def test_a_template_of_your_own(self, make, tg):
        venue = FakeVenue(start=64_000.0)
        bot = make(templates={"alert": "{symbol} {side} {level}: {price} ({condition}) "
                                       "{move_pct} · {source}"})
        bot.watch(venue, "BTCUSDT", above=65_000)
        push(venue, 65_100, bot=bot)
        assert tg.texts(ME) == ["BTCUSDT above 65,000: 65,100 (crossed above 65,000) — · "
                                "fake-venue"]

    def test_a_template_with_a_field_it_does_not_have_raises_at_the_call(self, make):
        bot = make(templates={"alert": "{symbol} {lvl}"})
        with pytest.raises(ValueError) as raised:
            bot.watch(FakeVenue(start=64_000.0), "BTCUSDT", above=65_000)
        assert str(raised.value) == (
            "template for 'alert' uses {lvl} — known: time, date, symbol, price, header, "
            "level, side, move_pct, window, condition, source")

    def test_template_none_fires_and_says_nothing(self, make, tg):
        venue = FakeVenue(start=64_000.0)
        bot = make(templates={"alert": None})
        alert = bot.watch(venue, "BTCUSDT", above=65_000)
        push(venue, 65_100, bot=bot)
        assert tg.texts(ME) == [] and alert.state == "fired"

    @pytest.mark.parametrize("options, error, message", [
        ({}, ValueError, r"needs a level \(above=, below=\) or a move"),
        ({"above": 65_000, "move_pct": -3, "window": "1h"}, ValueError, "not both"),
        ({"move_pct": -3}, ValueError, "a move needs window="),
        ({"above": 65_000, "window": "1h"}, ValueError, r"window= is for a move"),
        ({"above": 60_000, "below": 65_000}, ValueError, r"must be over below="),
        ({"above": 0}, ValueError, "a level must be above 0"),
        ({"above": "65000"}, TypeError, "above must be a number"),
        ({"move_pct": 0, "window": "1h"}, ValueError, "move_pct must be"),
        ({"above": 65_000, "on_close": "1M"}, ValueError, "'1M' has no fixed length"),
        ({"above": 65_000, "rearm": -1}, ValueError, "rearm must be 0 or more"),
        ({"above": 65_000, "repeat": "yes"}, TypeError, "repeat must be True or False"),
        ({"above": 65_000, "image": 3}, TypeError, "image must be True, False or a timeframe"),
        ({"above": 65_000, "silent": "x"}, TypeError, "silent must be"),
        ({"above": 65_000, "to": "nobody"}, ValueError, "unknown chat 'nobody'"),
    ])
    def test_mistakes_raise_at_the_call(self, make, options, error, message):
        venue = FakeVenue(start=64_000.0)
        bot = make()
        with pytest.raises(error, match=message):
            bot.watch(venue, "BTCUSDT", **options)
        assert venue.streams == [] and bot.alerts() == []


# ===========================================================================
# Pictures (image=True)
# ===========================================================================

def quarter_hours(count: int = 120) -> list[dict]:
    first = ms(at(14, 0) - timedelta(minutes=15 * count))
    return [{"timestamp": first + k * 900_000, "open": 64_000 + k, "high": 64_010 + k,
             "low": 63_990 + k, "close": 64_005 + k, "volume": 1.0} for k in range(count)]


class TestPictures:
    def test_image_is_the_last_100_15m_candles_with_the_level(self, make, tg, engine,
                                                               monkeypatch):
        lines: list[tuple] = []
        real = Chart.add_hline

        def add_hline(chart, price, **options):
            lines.append((price, options))
            return real(chart, price, **options)

        monkeypatch.setattr(Chart, "add_hline", add_hline)
        venue = FakeVenue(start=64_000.0, history=quarter_hours())
        bot = make()
        bot.watch(venue, "BTCUSDT", above=65_000, image=True)
        push(venue, 65_100, bot=bot)
        (picture,) = tg.pictures(ME)
        assert picture["text"].startswith("<b>🔔 BTCUSDT crossed above 65,000 · now 65,100</b>")
        ((_, _, timeframe, start_ms, end_ms, thread),) = [
            a for a in venue.asked if a[0] == "candles"]
        assert timeframe == "15m" and end_ms - start_ms == 99 * 900_000
        assert thread.startswith("atk-notify")              # made on the notifier's thread
        assert [price for price, _ in lines] == [65_000.0]
        query = engine.asked[0]["query"]
        assert query["title"].startswith("BTCUSDT · 15m · ")

    def test_image_takes_a_timeframe_and_follows_on_close(self, make, engine):
        venue = FakeVenue(start=64_000.0, history=quarter_hours())
        bot = make()
        assert bot.watch(venue, "BTCUSDT", above=65_000, image="1h").image_tf == "1h"
        assert bot.watch(venue, "BTCUSDT", above=65_000, on_close="4h",
                         image=True).image_tf == "4h"

    def test_without_the_engine_image_raises_at_the_call(self, make, monkeypatch):
        real = importlib.util.find_spec
        monkeypatch.setattr(importlib.util, "find_spec",
                            lambda name, *a: None if name == "playwright" else real(name, *a))
        bot = make()
        with pytest.raises(ImportError, match=r"need the \[notify\] extra"):
            bot.watch(FakeVenue(start=64_000.0), "BTCUSDT", above=65_000, image=True)
        assert bot.alerts() == []

    def test_a_picture_that_cannot_be_made_sends_the_text(self, make, tg, engine, capsys):
        engine.fail = _snapshot.SnapshotError(_snapshot.NO_UNPKG)
        venue = FakeVenue(start=64_000.0, history=quarter_hours())
        bot = make()
        bot.watch(venue, "BTCUSDT", above=65_000, image=True)
        push(venue, 65_100, bot=bot)
        (message,) = tg.got(ME)
        assert "media" not in message and message["text"].startswith("<b>🔔 BTCUSDT crossed")
        assert "no picture for alert BTCUSDT — the chart page could not load" in (
            capsys.readouterr().out)

    def test_images_refuses_alert(self, make, engine):
        with pytest.raises(ValueError) as raised:
            make(images={"alert"})
        assert str(raised.value) == ("images: 'alert' — an alert asks for its own picture: "
                                     "watch(…, image=True)")
