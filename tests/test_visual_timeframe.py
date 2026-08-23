"""
Tests for v1.1.0 — chart timeframe switching.

The chart holds its data at the *source* timeframe and resamples to whatever is
displayed.  Everything happens in Python: the browser asks for a timeframe and
receives finished candles.

Covered here: detection, the offered list, resampling, what happens to the two
kinds of indicator, streaming into a forming candle, and the protocol.
"""
from __future__ import annotations

import pandas as pd
import pytest

from AlgoTradeKit.visual import Chart

MIN_MS = 60_000
# 5m- and 15m-aligned so bucket edges are unambiguous in the assertions
ANCHOR = 1_700_000_000_000 - (1_700_000_000_000 % 900_000)


def _one_minute_frame(n: int = 300, start: int = ANCHOR) -> pd.DataFrame:
    return pd.DataFrame({
        "timestamp": [start + i * MIN_MS for i in range(n)],
        "open":      [100.0 + i for i in range(n)],
        "high":      [101.0 + i for i in range(n)],
        "low":       [ 99.0 + i for i in range(n)],
        "close":     [100.5 + i for i in range(n)],
        "volume":    [10.0] * n,
    })


# ---------------------------------------------------------------------------
# Detection and the offered list
# ---------------------------------------------------------------------------

class TestTimeframeDetection:
    def test_source_timeframe_is_detected(self):
        c = Chart()
        c.set_data(_one_minute_frame())
        assert c.source_timeframe == "1m"
        assert c.display_timeframe is None

    def test_explicit_source_timeframe_must_match_the_data(self):
        c = Chart(source_timeframe="5m")
        with pytest.raises(ValueError, match="does not match the data"):
            c.set_data(_one_minute_frame())

    def test_correct_explicit_source_timeframe_is_accepted(self):
        c = Chart(source_timeframe="1m")
        c.set_data(_one_minute_frame())
        assert c.source_timeframe == "1m"

    def test_undetectable_data_disables_switching_rather_than_raising(self):
        irregular = pd.DataFrame({
            "timestamp": [ANCHOR, ANCHOR + 7_000, ANCHOR + 90_000],
            "open": [1.0, 2.0, 3.0], "high": [2.0, 3.0, 4.0],
            "low": [0.5, 1.5, 2.5],  "close": [1.5, 2.5, 3.5],
            "volume": [1.0, 1.0, 1.0],
        })
        c = Chart()
        c.set_data(irregular)
        assert c.source_timeframe is None
        assert c.timeframes == []
        assert len(c._bars) == 3          # the chart itself still works

    def test_switching_undetectable_data_explains_itself(self):
        c = Chart()
        c.set_data(pd.DataFrame({
            "timestamp": [ANCHOR, ANCHOR + 7_000],
            "open": [1.0, 2.0], "high": [2.0, 3.0],
            "low": [0.5, 1.5], "close": [1.5, 2.5], "volume": [1.0, 1.0],
        }))
        with pytest.raises(ValueError, match="could not be detected"):
            c.set_timeframe("5m")

    def test_seconds_timestamps_are_understood(self):
        df = _one_minute_frame(60)
        df["time"] = df.pop("timestamp") // 1000        # seconds, named 'time'
        c = Chart()
        c.set_data(df)
        assert c.source_timeframe == "1m"


class TestOfferedTimeframes:
    def test_defaults_to_every_valid_multiple_with_the_source_first(self):
        c = Chart()
        c.set_data(_one_minute_frame())
        assert c.timeframes[0] == "1m"
        assert "5m" in c.timeframes and "1d" in c.timeframes
        assert "1M" not in c.timeframes          # months are not fixed-length

    def test_a_higher_source_offers_fewer(self):
        df = _one_minute_frame(200)
        df["timestamp"] = [ANCHOR + i * 4 * 3_600_000 for i in range(200)]   # 4h
        c = Chart()
        c.set_data(df)
        assert c.source_timeframe == "4h"
        assert "6h" not in c.timeframes          # 1.5x is not a whole multiple
        assert "12h" in c.timeframes and "1d" in c.timeframes

    def test_an_explicit_list_is_honoured(self):
        c = Chart(timeframes=["5m", "15m"])
        c.set_data(_one_minute_frame())
        assert c.timeframes == ["1m", "5m", "15m"]

    def test_an_impossible_entry_is_rejected(self):
        c = Chart(timeframes=["7m"])
        with pytest.raises(ValueError):
            c.set_data(_one_minute_frame())

    def test_monthly_is_rejected_explicitly(self):
        c = Chart(timeframes=["1M"])
        with pytest.raises(ValueError, match="months have no fixed length"):
            c.set_data(_one_minute_frame())


# ---------------------------------------------------------------------------
# The candles themselves
# ---------------------------------------------------------------------------

class TestResampledCandles:
    def test_display_timeframe_at_construction(self):
        c = Chart(display_timeframe="5m")
        c.set_data(_one_minute_frame(300))
        assert c.display_timeframe == "5m"
        assert len(c._bars) == 60

    def test_aggregation_is_correct(self):
        c = Chart(display_timeframe="5m")
        c.set_data(_one_minute_frame(300))
        first = c._bars[0]
        assert first["time"]   == ANCHOR // 1000
        assert first["open"]   == 100.0                 # first of the five
        assert first["high"]   == 105.0                 # max
        assert first["low"]    ==  99.0                 # min
        assert first["close"]  == 104.5                 # last
        assert first["volume"] ==  50.0                 # sum

    def test_matches_the_data_module_resampler(self):
        from AlgoTradeKit.data import resample_ohlcv

        df = _one_minute_frame(300)
        c  = Chart(display_timeframe="15m")
        c.set_data(df)
        expected = resample_ohlcv(df, "15m", drop_incomplete=False)
        assert [b["time"] for b in c._bars] == [
            int(t) // 1000 for t in expected["timestamp"]
        ]

    def test_switching_is_lossless_both_ways(self):
        df = _one_minute_frame(300)
        c  = Chart()
        c.set_data(df)
        original = list(c._bars)

        c.set_timeframe("15m")
        assert len(c._bars) == 20
        c.set_timeframe("5m")
        assert len(c._bars) == 60
        c.set_timeframe(None)
        assert c._bars == original
        assert c.display_timeframe is None

    def test_the_still_forming_candle_is_kept(self):
        # 302 minutes = 60 complete 5m candles + 2 minutes of the 61st
        c = Chart(display_timeframe="5m")
        c.set_data(_one_minute_frame(302))
        assert len(c._bars) == 61
        assert c._bars[-1]["volume"] == 20.0             # only the 2 minutes so far

    def test_a_partly_covered_first_candle_is_dropped(self):
        # start 10 minutes into a 15m bucket: that leading candle would claim
        # an open and a low it never had
        c = Chart(display_timeframe="15m")
        c.set_data(_one_minute_frame(300, start=ANCHOR + 10 * MIN_MS))
        assert c._bars[0]["time"] == (ANCHOR + 15 * MIN_MS) // 1000

    def test_set_timeframe_rejects_an_unoffered_timeframe(self):
        c = Chart(timeframes=["5m"])
        c.set_data(_one_minute_frame())
        with pytest.raises(ValueError, match="not one of this chart's timeframes"):
            c.set_timeframe("15m")

    def test_display_timeframe_before_data_is_applied_later(self):
        c = Chart()
        c.set_timeframe("5m")             # no data yet — remembered
        c.set_data(_one_minute_frame(300))
        assert c.display_timeframe == "5m"
        assert len(c._bars) == 60

    def test_candle_range_applies_to_the_displayed_candles(self):
        """candle_range is a view setting, like candle_count_limit: 'the last
        100 candles' means 100 of whatever is on screen, not 100 source rows."""
        c = Chart()
        c.set_data(_one_minute_frame(300), candle_range={"last_n": 100})
        assert len(c._bars) == 100

        c.set_timeframe("5m")
        assert len(c._bars) == 60         # all 60 five-minute candles; 100 > 60

        c.set_timeframe("3m")
        assert len(c._bars) == 100        # 300 minutes -> 100 three-minute candles

    def test_heikinashi_still_applies_after_a_switch(self):
        c = Chart(chart_type="heikinashi", display_timeframe="5m")
        c.set_data(_one_minute_frame(300))
        # HA closes are the mean of the OHLC of the resampled candle
        first = c._bars[0]
        assert first["close"] == pytest.approx((100.0 + 105.0 + 99.0 + 104.5) / 4)


# ---------------------------------------------------------------------------
# Indicators across a switch
# ---------------------------------------------------------------------------

class TestIndicatorsAcrossTimeframes:
    def test_spec_indicators_are_recomputed_not_thinned(self):
        c = Chart()
        c.set_data(_one_minute_frame(300))
        c.add_indicator_spec({"kind": "ema", "period": 10})
        at_source = c._indicators[0].payload["data"][-1]["value"]

        c.set_timeframe("15m")
        at_15m = c._indicators[0].payload["data"][-1]["value"]

        # A 10-period EMA of 15m closes is a different number, not a subsample
        assert at_15m != at_source
        assert c._indicators[0].payload.get("approx") is not True

    def test_a_multi_series_spec_is_recomputed_once(self):
        c = Chart()
        c.set_data(_one_minute_frame(600))
        c.add_indicator_spec({"kind": "macd"})
        before = len(c._indicators)

        c.set_timeframe("15m")
        assert len(c._indicators) == before          # not duplicated per series
        assert all(i.payload.get("approx") is not True for i in c._indicators)

    def test_a_recompute_that_cannot_run_falls_back_to_thinning(self):
        """A higher timeframe leaves fewer candles than some indicators need
        (MACD wants 34).  The series must survive, thinned and flagged."""
        c = Chart()
        c.set_data(_one_minute_frame(300))
        c.add_indicator_spec({"kind": "macd"})
        before = len(c._indicators)

        c.set_timeframe("15m")                       # only 20 candles left

        assert len(c._indicators) == before          # nothing lost
        assert all(i.payload.get("approx") is True for i in c._indicators)

    def test_it_recovers_when_the_timeframe_allows_it_again(self):
        c = Chart()
        c.set_data(_one_minute_frame(300))
        c.add_indicator_spec({"kind": "macd"})
        exact = c._indicators[0].payload["data"][-1]["value"]

        c.set_timeframe("15m")                       # thinned fallback
        c.set_timeframe(None)                        # recomputed again

        assert all(i.payload.get("approx") is not True for i in c._indicators)
        assert c._indicators[0].payload["data"][-1]["value"] == exact

    def test_raw_indicators_are_downsampled_and_flagged(self):
        df = _one_minute_frame(300)
        c  = Chart()
        c.set_data(df)
        c.add_indicator_from_atk(
            df.assign(value=df["close"])[["timestamp", "value"]], name="RAW"
        )
        assert len(c._indicators[0].data) == 300
        assert c._indicators[0].to_dict()["approx"] is False

        c.set_timeframe("15m")
        assert len(c._indicators[0].data) == 20
        assert c._indicators[0].to_dict()["approx"] is True

    def test_downsampling_keeps_the_last_value_of_each_bucket(self):
        df = _one_minute_frame(300)
        c  = Chart(display_timeframe="5m")
        c.set_data(df)
        c.add_indicator_from_atk(
            df.assign(value=df["close"])[["timestamp", "value"]], name="RAW"
        )
        first = c._indicators[0].data[0]
        assert first["time"]  == ANCHOR // 1000        # retimed to the bucket open
        assert first["value"] == 104.5                 # close of the 5th minute

    def test_going_back_restores_the_original_points(self):
        df = _one_minute_frame(300)
        c  = Chart()
        c.set_data(df)
        c.add_indicator_from_atk(
            df.assign(value=df["close"])[["timestamp", "value"]], name="RAW"
        )
        original = list(c._indicators[0].data)

        c.set_timeframe("15m")
        c.set_timeframe("5m")
        c.set_timeframe(None)

        assert c._indicators[0].data == original
        assert c._indicators[0].to_dict()["approx"] is False

    def test_an_indicator_added_while_switched_is_thinned_immediately(self):
        df = _one_minute_frame(300)
        c  = Chart(display_timeframe="5m")
        c.set_data(df)
        c.add_indicator_from_atk(
            df.assign(value=df["close"])[["timestamp", "value"]], name="RAW"
        )
        assert len(c._indicators[0].data) == 60

    def test_no_indicator_point_lands_before_the_first_candle(self):
        df = _one_minute_frame(300, start=ANCHOR + 10 * MIN_MS)
        c  = Chart()
        c.set_data(df)
        c.add_indicator_from_atk(
            df.assign(value=df["close"])[["timestamp", "value"]], name="RAW"
        )
        c.set_timeframe("15m")
        assert c._indicators[0].data[0]["time"] >= c._bars[0]["time"]


# ---------------------------------------------------------------------------
# Streaming into a forming candle
# ---------------------------------------------------------------------------

class TestStreamingWhileConverted:
    def _chart_with_capture(self, display="5m", n=300):
        c = Chart(display_timeframe=display)
        c.set_data(_one_minute_frame(n))
        sent: list[dict] = []
        c._shown = True
        c._server.send = sent.append
        return c, sent

    def test_source_bars_fold_into_one_display_candle(self):
        c, sent = self._chart_with_capture()
        last = ANCHOR + 299 * MIN_MS
        for k in range(1, 4):
            c.stream_from_atk({
                "timestamp": last + k * MIN_MS, "open": 200.0,
                "high": 210.0 + k, "low": 190.0, "close": 205.0, "volume": 7.0,
            })

        bars = [m["bar"] for m in sent if m["type"] == "stream"]
        assert len({b["time"] for b in bars}) == 1        # one forming candle
        assert bars[-1]["high"]   == 213.0                # running max
        assert bars[-1]["volume"] == 21.0                 # running sum
        assert bars[-1]["open"]   == 200.0                # of the first minute

    def test_a_boundary_starts_a_new_candle(self):
        c, sent = self._chart_with_capture()
        before = len(c._bars)
        last   = ANCHOR + 299 * MIN_MS                    # last minute of a bucket
        for k in range(1, 7):                             # crosses into the next
            c.stream_from_atk({
                "timestamp": last + k * MIN_MS, "open": 200.0, "high": 201.0,
                "low": 199.0, "close": 200.5, "volume": 1.0,
            })
        times = {m["bar"]["time"] for m in sent if m["type"] == "stream"}
        assert len(times) == 2
        assert len(c._bars) == before + 2

    def test_streaming_at_the_source_timeframe_is_untouched(self):
        c = Chart()
        c.set_data(_one_minute_frame(10))
        sent: list[dict] = []
        c._shown = True
        c._server.send = sent.append
        t = (ANCHOR + 10 * MIN_MS) // 1000
        c.stream({"time": t, "open": 1.0, "high": 2.0, "low": 0.5,
                  "close": 1.5, "volume": 3.0})
        assert sent[-1]["bar"] == {"time": t, "open": 1.0, "high": 2.0,
                                   "low": 0.5, "close": 1.5, "volume": 3.0}


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

class TestTimeframeProtocol:
    def test_init_payload_carries_the_timeframe_state(self):
        c = Chart(display_timeframe="5m")
        c.set_data(_one_minute_frame(300))
        init = c._build_init_payload()
        assert init["sourceTimeframe"]  == "1m"
        assert init["displayTimeframe"] == "5m"
        assert init["timeframes"][0]    == "1m"

    def test_a_chart_without_timeframes_still_sends_the_keys(self):
        c = Chart()
        c.set_data(pd.DataFrame({
            "timestamp": [ANCHOR, ANCHOR + 7_000],
            "open": [1.0, 2.0], "high": [2.0, 3.0],
            "low": [0.5, 1.5], "close": [1.5, 2.5], "volume": [1.0, 1.0],
        }))
        init = c._build_init_payload()
        assert init["sourceTimeframe"] is None
        assert init["timeframes"] == []

    def test_browser_message_switches_the_timeframe(self):
        c = Chart()
        c.set_data(_one_minute_frame(300))
        c._shown = True
        c._server.send = lambda msg: None
        c._handle_browser_message({"type": "set_timeframe", "tf": "5m"})
        assert c.display_timeframe == "5m"
        assert len(c._bars) == 60

    def test_browser_message_with_null_returns_to_the_source(self):
        c = Chart(display_timeframe="5m")
        c.set_data(_one_minute_frame(300))
        c._shown = True
        c._server.send = lambda msg: None
        c._handle_browser_message({"type": "set_timeframe", "tf": None})
        assert c.display_timeframe is None
        assert len(c._bars) == 300

    def test_a_bad_timeframe_is_reported_not_raised(self):
        c = Chart(timeframes=["5m"])
        c.set_data(_one_minute_frame(300))
        sent: list[dict] = []
        c._shown = True
        c._server.send = sent.append
        c._handle_browser_message({"type": "set_timeframe", "tf": "15m"})
        assert sent and sent[-1]["type"] == "timeframe_error"
        assert c.display_timeframe is None            # unchanged

    def test_switching_pushes_a_fresh_init(self):
        c = Chart()
        c.set_data(_one_minute_frame(300))
        sent: list[dict] = []
        c._shown = True
        c._server.send = sent.append
        c.set_timeframe("5m")
        inits = [m for m in sent if m["type"] == "init"]
        assert inits and inits[-1]["displayTimeframe"] == "5m"
        assert len(inits[-1]["bars"]) == 60


class TestFromCsv:
    def test_from_csv_accepts_the_timeframe_arguments(self, tmp_path):
        path = tmp_path / "one_minute.csv"
        _one_minute_frame(300).to_csv(path, index=False)

        c = Chart.from_csv(str(path), display_timeframe="5m")
        assert c.source_timeframe  == "1m"
        assert c.display_timeframe == "5m"
        assert len(c._bars) == 60
