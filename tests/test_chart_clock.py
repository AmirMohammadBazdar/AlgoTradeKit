"""
Tests for v1.3.0 — the feed's clock a chart learns from its live candles.

A page counts the time to the last candle's close on this clock, the one the
candles are stamped in: UTC on Binance, the broker's own time on MetaTrader. The
chart learns it from the moment each new candle's first update arrives, and its
server hands every page connection a fresh reading of it.
"""
from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

import AlgoTradeKit.visual.chart as chart_module
from AlgoTradeKit.visual import Chart, ChartPage

#: A minute's opening, in Unix seconds.
T = 1_793_000_040


class Clock:
    """This machine's clock, set by the test."""

    def __init__(self) -> None:
        self.ms = 0

    def __call__(self) -> int:
        return self.ms


@pytest.fixture
def clock(monkeypatch) -> Clock:
    now = Clock()
    monkeypatch.setattr(chart_module, "_now_ms", now)
    return now


def _bar(opening: int, close: float = 100.0) -> dict:
    return {"time": opening, "open": 100.0, "high": 101.0, "low": 99.0, "close": close,
            "volume": 1.0}


def _feed(chart: Chart, clock: Clock, *updates: tuple[int, int]) -> None:
    """Stream ``(candle opening in s, arrival on this machine in ms)`` in turn."""
    for opening, arrival in updates:
        clock.ms = arrival
        chart.stream(_bar(opening))


def _frame(n: int = 30, start: int = T - 30 * 60) -> pd.DataFrame:
    stamps = [(start + i * 60) * 1000 for i in range(n)]
    close = 100 + np.arange(n) * 0.1
    return pd.DataFrame({"timestamp": stamps, "open": close, "high": close + 0.5,
                         "low": close - 0.5, "close": close, "volume": 1.0})


class TestLearning:
    def test_a_new_candle_after_one_watched_forming_teaches_the_clock(self, clock):
        chart = Chart(title="c")
        _feed(chart, clock, (T - 60, (T - 60) * 1000 + 20_000), (T - 60, (T - 60) * 1000 + 21_000))
        _feed(chart, clock, (T, T * 1000 + 300))
        assert chart._clock_offset_ms is None          # one update: maybe a closed candle
        _feed(chart, clock, (T, T * 1000 + 1_300))
        assert chart._clock_offset_ms == -300          # read when its first update came

    def test_a_feed_stamped_in_the_brokers_time_gives_the_brokers_clock(self, clock):
        """MetaTrader: stamps two hours ahead of UTC (Alpari's winter time)."""
        chart = Chart(title="c")
        shift = 2 * 3600
        _feed(chart, clock, (T - 60 + shift, (T - 60) * 1000 + 5_000),
              (T - 60 + shift, (T - 60) * 1000 + 6_000),
              (T + shift, T * 1000 + 400), (T + shift, T * 1000 + 900))
        assert chart._clock_offset_ms == shift * 1000 - 400

    def test_a_feed_of_closed_candles_teaches_nothing(self, clock):
        chart = Chart(title="c")
        _feed(chart, clock, *[(T + i * 60, (T + i * 60 + 60) * 1000 + 200) for i in range(5)])
        assert chart._clock_offset_ms is None
        assert chart._clock_message() is None

    def test_the_candle_forming_when_the_stream_began_teaches_nothing(self, clock):
        """Subscribed 40 s into a candle: that one is half over.  The next is right."""
        chart = Chart(title="c")
        _feed(chart, clock, (T - 60, T * 1000 - 20_000),            # a replayed, closed one
              (T, T * 1000 + 40_000), (T, T * 1000 + 41_000))       # forming, half over
        assert chart._clock_offset_ms is None
        _feed(chart, clock, (T + 60, (T + 60) * 1000 + 500), (T + 60, (T + 60) * 1000 + 700))
        assert chart._clock_offset_ms == -500

    def test_the_best_of_the_newest_ten_samples_wins(self, clock):
        chart = Chart(title="c")
        _feed(chart, clock, (T, T * 1000), (T, T * 1000 + 100))
        delays = [2_000, 300, 900] + [1_000] * 10
        for k, delay in enumerate(delays, start=1):
            opening = T + k * 60
            _feed(chart, clock, (opening, opening * 1000 + delay),
                  (opening, opening * 1000 + delay + 50))
            if k == 3:
                assert chart._clock_offset_ms == -300       # the least late of them
        assert chart._clock_offset_ms == -1_000             # 300 has left the window

    def test_an_older_candle_is_ignored(self, clock):
        chart = Chart(title="c")
        _feed(chart, clock, (T, T * 1000), (T, T * 1000 + 100),
              (T + 60, (T + 60) * 1000 + 250), (T - 60, (T + 60) * 1000 + 260),
              (T + 60, (T + 60) * 1000 + 400))
        assert chart._clock_offset_ms == -250

    def test_on_another_timeframe_it_learns_from_the_feeds_candles(self, clock):
        """Shown on 5m, fed 1m: a new 1m candle teaches the clock, not the 5m bucket."""
        chart = Chart(title="c", source_timeframe="1m", display_timeframe="5m")
        chart.set_data(_frame())
        _feed(chart, clock, (T, T * 1000 + 100), (T, T * 1000 + 200),
              (T + 60, (T + 60) * 1000 + 350), (T + 60, (T + 60) * 1000 + 600))
        assert chart._clock_offset_ms == -350

    def test_the_message_is_made_fresh(self, clock):
        chart = Chart(title="c")
        _feed(chart, clock, (T, T * 1000), (T, T * 1000 + 100),
              (T + 60, (T + 60) * 1000 + 300), (T + 60, (T + 60) * 1000 + 400))
        clock.ms = 5_000_000
        assert chart._clock_message() == {"type": "clock", "now": 5_000_000 - 300}


class TestServers:
    def test_every_connection_gets_the_clock_after_the_init_and_the_access(self, clock):
        from websockets.sync.client import connect

        chart = Chart(title="c")
        chart.set_data(_frame())
        chart.show(open_browser=False)
        try:
            url = f"ws://127.0.0.1:{chart._server.port}/ws"
            with connect(url) as ws:                        # nothing learned yet: no clock
                assert json.loads(ws.recv(timeout=5))["type"] == "init"
                assert json.loads(ws.recv(timeout=5))["type"] == "access"
                _feed(chart, clock, (T, T * 1000), (T, T * 1000 + 100),
                      (T + 60, (T + 60) * 1000 + 300), (T + 60, (T + 60) * 1000 + 400))
                told = [json.loads(ws.recv(timeout=5)) for _ in range(5)]
                assert {"type": "clock", "now": (T + 60) * 1000 + 100} in told   # when learned
            clock.ms = 9_000_000
            with connect(url) as ws:
                kinds = [json.loads(ws.recv(timeout=5)) for _ in range(3)]
                assert [m["type"] for m in kinds] == ["init", "access", "clock"]
                assert kinds[2]["now"] == 9_000_000 - 300   # made now, not cached
        finally:
            chart.stop()

    def test_a_page_gives_each_view_its_own_charts_clock(self, clock):
        from websockets.sync.client import connect

        fed, still = Chart(title="fed"), Chart(title="still")
        for chart in (fed, still):
            chart.set_data(_frame())
        page = ChartPage(title="p")
        page.add(fed)
        page.add(still)
        _feed(fed, clock, (T, T * 1000), (T, T * 1000 + 100),
              (T + 60, (T + 60) * 1000 + 700), (T + 60, (T + 60) * 1000 + 800))
        page.show(block=False, open_browser=False)
        try:
            clock.ms = 7_000_000
            for view, expected in (("v1", 7_000_000 - 700), ("v2", None)):
                url = f"ws://127.0.0.1:{page._server.port}/ws?view={view}"
                with connect(url) as ws:
                    got = []
                    try:
                        while len(got) < 4:
                            got.append(json.loads(ws.recv(timeout=1.5)))
                    except TimeoutError:
                        pass
                clocks = [m["now"] for m in got if m.get("type") == "clock"]
                assert clocks == ([expected] if expected is not None else [])
        finally:
            page.stop()
