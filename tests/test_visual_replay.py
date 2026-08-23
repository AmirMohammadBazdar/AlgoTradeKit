"""
Tests for v1.1.0 — bar replay, Python side.

Replay itself runs in the browser: once armed, stepping must not cost a round
trip, because the browser is often at the far end of an SSH tunnel.  What
Python owns is the arming — handing over the *source* candles that sit inside
each displayed one, which is what lets a 3m and a 5m chart replay on the same
instant instead of each counting its own bars.
"""
from __future__ import annotations

import pandas as pd
import pytest

from AlgoTradeKit.visual import Chart, ChartPage

MIN_MS = 60_000
ANCHOR = 1_700_000_000_000 - (1_700_000_000_000 % 900_000)


def _one_minute_frame(n: int = 300) -> pd.DataFrame:
    return pd.DataFrame({
        "timestamp": [ANCHOR + i * MIN_MS for i in range(n)],
        "open":      [100.0 + i for i in range(n)],
        "high":      [101.0 + i for i in range(n)],
        "low":       [ 99.0 + i for i in range(n)],
        "close":     [100.5 + i for i in range(n)],
        "volume":    [10.0] * n,
    })


def _armed(chart: Chart) -> dict:
    """Run the browser's ``replay_arm`` against *chart* and return the answer."""
    sent: list[dict] = []
    chart._shown = True
    chart._server.send = sent.append
    chart._handle_browser_message({"type": "replay_arm"})
    assert sent, "arming must answer"
    return sent[-1]


class TestSourceBars:
    def test_source_bars_are_the_unresampled_candles(self):
        c = Chart(display_timeframe="5m")
        c.set_data(_one_minute_frame(300))

        assert len(c._bars) == 60             # what is displayed
        src = c.source_bars()
        assert len(src) == 300                # what it was built from
        assert src[0]["time"] == ANCHOR // 1000
        assert src[1]["time"] - src[0]["time"] == 60

    def test_they_carry_the_full_ohlcv(self):
        c = Chart(display_timeframe="5m")
        c.set_data(_one_minute_frame(10))
        first = c.source_bars()[0]
        assert first["open"] == 100.0 and first["high"] == 101.0
        assert first["low"] == 99.0 and first["close"] == 100.5
        assert first["volume"] == 10.0

    def test_none_when_the_display_is_already_the_source(self):
        """There is nothing finer to build a forming candle out of."""
        c = Chart()
        c.set_data(_one_minute_frame(300))
        assert c.source_bars() == []

    def test_none_before_any_data(self):
        assert Chart().source_bars() == []

    def test_they_survive_a_timeframe_change(self):
        c = Chart(display_timeframe="5m")
        c.set_data(_one_minute_frame(300))
        c.set_timeframe("15m")
        assert len(c.source_bars()) == 300    # still the 1m candles
        assert len(c._bars) == 20

    def test_a_streamed_candle_joins_them(self):
        c = Chart(display_timeframe="5m")
        c.set_data(_one_minute_frame(300))
        c._shown = True
        c._server.send = lambda msg: None
        c.stream_from_atk({
            "timestamp": ANCHOR + 300 * MIN_MS, "open": 1.0, "high": 2.0,
            "low": 0.5, "close": 1.5, "volume": 3.0,
        })
        src = c.source_bars()
        assert len(src) == 301
        assert src[-1]["close"] == 1.5


class TestArming:
    def test_arming_answers_with_the_source_candles(self):
        c = Chart(display_timeframe="5m")
        c.set_data(_one_minute_frame(300))
        msg = _armed(c)

        assert msg["type"] == "replay_data"
        assert msg["sourceTimeframe"] == "1m"
        assert msg["stepSeconds"] == 60
        assert len(msg["sourceBars"]) == 300

    def test_the_step_is_the_source_timeframe(self):
        df = _one_minute_frame(300)
        df["timestamp"] = [ANCHOR + i * 5 * MIN_MS for i in range(300)]    # 5m data
        c = Chart(display_timeframe="15m")
        c.set_data(df)
        assert _armed(c)["stepSeconds"] == 300

    def test_arming_at_the_source_timeframe_sends_no_candles(self):
        """Nothing finer exists, so the frontend steps whole candles instead."""
        c = Chart()
        c.set_data(_one_minute_frame(300))
        msg = _armed(c)
        assert msg["sourceBars"] == []
        assert msg["stepSeconds"] == 60

    def test_arming_does_not_disturb_the_chart(self):
        c = Chart(display_timeframe="5m")
        c.set_data(_one_minute_frame(300))
        before = list(c._bars)
        _armed(c)
        assert c._bars == before
        assert c.display_timeframe == "5m"

    def test_every_chart_on_a_page_arms_independently(self):
        page = ChartPage()
        fast = Chart(title="3m", display_timeframe="3m")
        fast.set_data(_one_minute_frame(300))
        slow = Chart(title="5m", display_timeframe="5m")
        slow.set_data(_one_minute_frame(300))
        page.add(fast)
        page.add(slow)

        seen: list[tuple[str, dict]] = []
        page._server.send = lambda vid, msg: seen.append((vid, msg))
        fast._shown = slow._shown = True

        page._server.on_message["v1"]({"type": "replay_arm"})
        page._server.on_message["v2"]({"type": "replay_arm"})

        assert [vid for vid, _ in seen] == ["v1", "v2"]
        assert all(m["type"] == "replay_data" for _, m in seen)
        # both are 1m underneath, so the page can step a minute at a time
        assert {m["stepSeconds"] for _, m in seen} == {60}


class TestFrontendWiring:
    """The shipped pages carry the replay protocol and controls."""

    @pytest.fixture(scope="class")
    def chart_html(self) -> str:
        import AlgoTradeKit.visual.server as _srv
        return (_srv.STATIC_DIR / "index.html").read_text(encoding="utf-8")

    @pytest.fixture(scope="class")
    def page_html(self) -> str:
        import AlgoTradeKit.visual.server as _srv
        return (_srv.STATIC_DIR / "page.html").read_text(encoding="utf-8")

    def test_chart_handles_the_replay_protocol(self, chart_html):
        assert "case 'replay_data'" in chart_html
        assert "type:'replay_arm'" in chart_html

    def test_chart_has_the_controls(self, chart_html):
        for element in ('id="rp-btn"', 'id="rp-bar"', 'id="rp-play"',
                        'id="rp-speed"', 'id="rp-clock"'):
            assert element in chart_html

    def test_chart_binds_the_keyboard_shortcuts(self, chart_html):
        assert "e.code === 'Space'" in chart_html
        assert "e.shiftKey && e.key === 'ArrowRight'" in chart_html
        assert "e.shiftKey && e.key === 'ArrowLeft'" in chart_html

    def test_page_drives_every_frame_from_one_cursor(self, page_html):
        assert "replay-arm" in page_html
        assert "replay-cursor" in page_html
        assert "replay-exit" in page_html
        # the page can only step as finely as its finest chart
        assert "Math.min(replay.step,  msg.step)" in page_html

    def test_chart_accepts_the_page_cursor(self, chart_html):
        assert "case 'replay-cursor'" in chart_html
        assert "function replayPageCursor" in chart_html
