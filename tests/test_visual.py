"""
Tests for AlgoTradeKit.visual

Groups
------
1. Models           — to_dict() shape and values for every drawing type
2. Chart.set_data() — column handling, ms detection, timestamp alias
3. Heikin-Ashi      — mathematical correctness
4. add_indicator()  — data processing, ms/timestamp handling
5. stream()         — in-memory update vs new bar, HA streaming
6. stream_from_atk()— ms → seconds conversion
7. from_csv()       — factory classmethod
8. Drawings         — add_hline, add_signal, add_trendline, add_box, add_fib
9. save/load layout — roundtrip JSON persistence
10. Server          — port finder, server starts without browser
11. Repr            — __repr__ output
"""

import json
import os
import time
import pytest
import pandas as pd
import numpy as np

# ---------------------------------------------------------------------------
# We import from the local path — adjust when running inside the real package
# ---------------------------------------------------------------------------
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from AlgoTradeKit.visual import Chart
from AlgoTradeKit.visual.models import (
    Bar, HorizontalLine, TrendLine, Box,
    Signal, TextLabel, FibRetracement, IndicatorSeries,
)
from AlgoTradeKit.visual.server import _find_free_port


# ===========================================================================
# Shared fixtures
# ===========================================================================

ANCHOR_S  = 1704067200          # 2024-01-01 00:00 UTC in seconds
ANCHOR_MS = ANCHOR_S * 1000     # same in milliseconds
ONE_DAY_S = 86400


def _make_ohlcv_df(n: int = 10, use_timestamp_col: bool = False) -> pd.DataFrame:
    """Build a minimal OHLCV DataFrame."""
    ts = [ANCHOR_S + i * ONE_DAY_S for i in range(n)]
    df = pd.DataFrame({
        "time":   ts,
        "open":   [100.0 + i for i in range(n)],
        "high":   [110.0 + i for i in range(n)],
        "low":    [90.0  + i for i in range(n)],
        "close":  [105.0 + i for i in range(n)],
        "volume": [1000.0]   * n,
    })
    if use_timestamp_col:
        # AlgoTradeKit Collector format: milliseconds, column named 'timestamp'
        df.rename(columns={"time": "timestamp"}, inplace=True)
        df["timestamp"] = df["timestamp"] * 1000   # convert to ms
    return df


def _make_chart(n: int = 10) -> Chart:
    """Create a Chart with data loaded but no server started."""
    c = Chart(title="Test")
    c.set_data(_make_ohlcv_df(n))
    return c


# ===========================================================================
# Group 1 — Models: to_dict()
# ===========================================================================

class TestModels:

    def test_bar_to_dict(self):
        b = Bar(time=ANCHOR_S, open=100, high=110, low=90, close=105, volume=500)
        d = b.to_dict()
        assert d["time"]   == ANCHOR_S
        assert d["open"]   == 100
        assert d["high"]   == 110
        assert d["low"]    == 90
        assert d["close"]  == 105
        assert d["volume"] == 500

    def test_horizontal_line_to_dict(self):
        h = HorizontalLine(price=50000, color="#ff0000", label="Resistance")
        d = h.to_dict()
        assert d["type"]   == "hline"
        assert d["price"]  == 50000
        assert d["color"]  == "#ff0000"
        assert d["label"]  == "Resistance"
        assert d["source"] == "server"
        assert "id" in d

    def test_trendline_to_dict(self):
        t = TrendLine(time1=ANCHOR_S, price1=100, time2=ANCHOR_S + ONE_DAY_S, price2=110)
        d = t.to_dict()
        assert d["type"]   == "trendline"
        assert d["time1"]  == ANCHOR_S
        assert d["price1"] == 100
        assert d["time2"]  == ANCHOR_S + ONE_DAY_S
        assert d["price2"] == 110
        assert "id" in d

    def test_box_to_dict(self):
        b = Box(time1=ANCHOR_S, price1=100, time2=ANCHOR_S + ONE_DAY_S, price2=110)
        d = b.to_dict()
        assert d["type"]    == "box"
        assert d["opacity"] == 0.2
        assert "borderColor" in d

    def test_signal_buy_auto_color(self):
        s = Signal(time=ANCHOR_S, side="buy")
        d = s.to_dict()
        assert d["type"]  == "signal"
        assert d["side"]  == "buy"
        assert d["color"] == "#3fb950"

    def test_signal_sell_auto_color(self):
        s = Signal(time=ANCHOR_S, side="sell")
        d = s.to_dict()
        assert d["color"] == "#f85149"

    def test_signal_custom_color_overrides_auto(self):
        s = Signal(time=ANCHOR_S, side="buy", color="#aabbcc")
        d = s.to_dict()
        assert d["color"] == "#aabbcc"

    def test_text_label_to_dict(self):
        t = TextLabel(time=ANCHOR_S, price=100, text="Hello")
        d = t.to_dict()
        assert d["type"] == "text"
        assert d["text"] == "Hello"
        assert "fontSize" in d

    def test_fib_to_dict(self):
        f = FibRetracement(time1=ANCHOR_S, price1=90, time2=ANCHOR_S + ONE_DAY_S, price2=110)
        d = f.to_dict()
        assert d["type"]   == "fib"
        assert d["levels"] == [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]

    def test_each_drawing_has_unique_id(self):
        ids = {
            HorizontalLine(price=1).id,
            HorizontalLine(price=2).id,
            TrendLine(time1=0, price1=0, time2=1, price2=1).id,
            Signal(time=0, side="buy").id,
        }
        assert len(ids) == 4   # all unique

    def test_indicator_series_to_dict(self):
        ind = IndicatorSeries(name="EMA 20", data=[{"time": ANCHOR_S, "value": 100.0}])
        d   = ind.to_dict()
        assert d["name"]       == "EMA 20"
        assert d["seriesType"] == "line"
        assert d["lineWidth"]  == 1
        assert isinstance(d["data"], list)


# ===========================================================================
# Group 2 — Chart.set_data()
# ===========================================================================

class TestSetData:

    def test_standard_time_seconds(self):
        df = _make_ohlcv_df(5)
        c  = Chart(title="t")
        c.set_data(df)
        assert len(c._bars) == 5
        assert c._bars[0]["time"] == ANCHOR_S

    def test_timestamp_ms_column_accepted(self):
        """AlgoTradeKit Collector CSVs use 'timestamp' in ms — must work."""
        df = _make_ohlcv_df(5, use_timestamp_col=True)
        assert "timestamp" in df.columns
        c = Chart(title="t")
        c.set_data(df)
        assert len(c._bars) == 5
        # After conversion, time must be in seconds
        assert c._bars[0]["time"] == ANCHOR_S

    def test_ms_time_column_auto_detected(self):
        """A 'time' column with ms values must be converted to seconds."""
        df = _make_ohlcv_df(5)
        df["time"] = df["time"] * 1000   # convert to ms manually
        c = Chart(title="t")
        c.set_data(df)
        assert c._bars[0]["time"] == ANCHOR_S

    def test_uppercase_columns_accepted(self):
        df = _make_ohlcv_df(3)
        df.columns = [c.upper() for c in df.columns]
        c = Chart(title="t")
        c.set_data(df)
        assert len(c._bars) == 3

    def test_missing_required_column_raises(self):
        df = _make_ohlcv_df(3).drop(columns=["close"])
        c  = Chart(title="t")
        with pytest.raises(ValueError, match="missing required columns"):
            c.set_data(df)

    def test_no_time_column_raises(self):
        df = _make_ohlcv_df(3).drop(columns=["time"])
        c  = Chart(title="t")
        with pytest.raises(ValueError, match="'time' or 'timestamp'"):
            c.set_data(df)

    def test_volume_defaults_to_zero(self):
        df = _make_ohlcv_df(3).drop(columns=["volume"])
        c  = Chart(title="t")
        c.set_data(df)
        assert c._bars[0]["volume"] == 0.0

    def test_bars_are_floats(self):
        df = _make_ohlcv_df(3)
        c  = Chart(title="t")
        c.set_data(df)
        bar = c._bars[0]
        assert isinstance(bar["open"],  float)
        assert isinstance(bar["close"], float)

    def test_datetime_index_accepted(self):
        df = _make_ohlcv_df(5).drop(columns=["time"])
        df.index = pd.to_datetime(
            [ANCHOR_S + i * ONE_DAY_S for i in range(5)], unit="s", utc=True
        )
        c = Chart(title="t")
        c.set_data(df)
        assert len(c._bars) == 5


# ===========================================================================
# Group 3 — Heikin-Ashi
# ===========================================================================

class TestHeikinAshi:

    def test_ha_close_is_average_of_ohlc(self):
        """HA close = (O+H+L+C) / 4."""
        df = pd.DataFrame({
            "time":  [ANCHOR_S],
            "open":  [100.0],
            "high":  [120.0],
            "low":   [80.0],
            "close": [110.0],
        })
        ha = Chart._to_heikinashi(df)
        assert ha["close"].iloc[0] == pytest.approx((100 + 120 + 80 + 110) / 4)

    def test_first_ha_open_is_midpoint_of_first_candle(self):
        """First HA open = (O[0] + C[0]) / 2."""
        df = pd.DataFrame({
            "time":  [ANCHOR_S, ANCHOR_S + ONE_DAY_S],
            "open":  [100.0, 110.0],
            "high":  [120.0, 130.0],
            "low":   [80.0,  90.0],
            "close": [110.0, 115.0],
        })
        ha = Chart._to_heikinashi(df)
        assert ha["open"].iloc[0] == pytest.approx((100 + 110) / 2)

    def test_ha_high_never_below_open_or_close(self):
        df = _make_ohlcv_df(20)
        ha = Chart._to_heikinashi(df)
        for _, row in ha.iterrows():
            assert row["high"] >= row["open"]
            assert row["high"] >= row["close"]

    def test_ha_low_never_above_open_or_close(self):
        df = _make_ohlcv_df(20)
        ha = Chart._to_heikinashi(df)
        for _, row in ha.iterrows():
            assert row["low"] <= row["open"]
            assert row["low"] <= row["close"]


# ===========================================================================
# Group 4 — add_indicator()
# ===========================================================================

class TestAddIndicator:

    def test_indicator_stored_in_memory(self):
        c = _make_chart()
        ind_df = pd.DataFrame({"time": [ANCHOR_S], "value": [100.0]})
        c.add_indicator(ind_df, name="EMA 20")
        assert len(c._indicators) == 1
        assert c._indicators[0].name == "EMA 20"

    def test_nan_values_are_dropped(self):
        c   = _make_chart()
        ind = pd.DataFrame({
            "time":  [ANCHOR_S, ANCHOR_S + ONE_DAY_S, ANCHOR_S + 2 * ONE_DAY_S],
            "value": [100.0, float("nan"), 102.0],
        })
        c.add_indicator(ind, name="EMA")
        data = c._indicators[0].data
        assert len(data) == 2
        assert all(not pd.isna(p["value"]) for p in data)

    def test_add_indicator_from_atk_handles_timestamp_ms(self):
        """add_indicator_from_atk() must accept 'timestamp' in ms."""
        c   = _make_chart()
        ind = pd.DataFrame({
            "timestamp": [ANCHOR_MS, ANCHOR_MS + ONE_DAY_S * 1000],
            "value":     [100.0, 101.0],
        })
        c.add_indicator_from_atk(ind, name="EMA ATK")
        assert len(c._indicators) == 1
        # Internal data should have 'time' in seconds
        assert c._indicators[0].data[0]["time"] == ANCHOR_S

    def test_multiple_indicators_stored(self):
        c   = _make_chart()
        ind = pd.DataFrame({"time": [ANCHOR_S], "value": [100.0]})
        c.add_indicator(ind, name="A")
        c.add_indicator(ind, name="B")
        assert len(c._indicators) == 2

    def test_indicator_color_stored(self):
        c   = _make_chart()
        ind = pd.DataFrame({"time": [ANCHOR_S], "value": [99.0]})
        c.add_indicator(ind, name="X", color="#ff0000")
        assert c._indicators[0].color == "#ff0000"


# ===========================================================================
# Group 5 — stream()
# ===========================================================================

class TestStream:

    def test_new_bar_is_appended(self):
        c = _make_chart(5)
        c.stream({
            "time": ANCHOR_S + 5 * ONE_DAY_S,
            "open": 200, "high": 210, "low": 190, "close": 205, "volume": 500,
        })
        assert len(c._bars) == 6

    def test_same_time_updates_existing_bar(self):
        c = _make_chart(5)
        last_time = c._bars[-1]["time"]
        c.stream({
            "time": last_time,
            "open": 999, "high": 999, "low": 999, "close": 999,
        })
        assert len(c._bars) == 5               # still 5, not 6
        assert c._bars[-1]["close"] == 999.0

    def test_volume_defaults_to_zero_in_stream(self):
        c = _make_chart(3)
        c.stream({"time": ANCHOR_S + 10 * ONE_DAY_S, "open": 1, "high": 2, "low": 0, "close": 1})
        assert c._bars[-1]["volume"] == 0.0

    def test_pandas_series_accepted(self):
        c   = _make_chart(3)
        row = pd.Series({
            "time": ANCHOR_S + 99 * ONE_DAY_S,
            "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 100.0,
        })
        c.stream(row)
        assert c._bars[-1]["time"] == ANCHOR_S + 99 * ONE_DAY_S


# ===========================================================================
# Group 6 — stream_from_atk()
# ===========================================================================

class TestStreamFromAtk:

    def test_timestamp_ms_converted_to_seconds(self):
        c = _make_chart(3)
        c.stream_from_atk({
            "timestamp": ANCHOR_MS + 99 * ONE_DAY_S * 1000,
            "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
        })
        expected_time = ANCHOR_S + 99 * ONE_DAY_S
        assert c._bars[-1]["time"] == expected_time

    def test_standard_time_passthrough(self):
        """If 'time' (seconds) is given, it should pass through unchanged."""
        c = _make_chart(3)
        c.stream_from_atk({
            "time": ANCHOR_S + 10 * ONE_DAY_S,
            "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
        })
        assert c._bars[-1]["time"] == ANCHOR_S + 10 * ONE_DAY_S

    def test_uppercase_keys_accepted(self):
        c = _make_chart(3)
        c.stream_from_atk({
            "TIMESTAMP": ANCHOR_MS + 5 * ONE_DAY_S * 1000,
            "OPEN": 1.0, "HIGH": 2.0, "LOW": 0.5, "CLOSE": 1.5,
        })
        assert c._bars[-1]["time"] == ANCHOR_S + 5 * ONE_DAY_S


# ===========================================================================
# Group 7 — from_csv()
# ===========================================================================

class TestFromCsv:

    def test_loads_collector_csv(self, tmp_path):
        """from_csv() must load a Collector-format CSV (timestamp in ms)."""
        df = _make_ohlcv_df(10, use_timestamp_col=True)
        path = str(tmp_path / "test.csv")
        df.to_csv(path, index=False)

        c = Chart.from_csv(path)
        assert len(c._bars) == 10
        assert c._bars[0]["time"] == ANCHOR_S   # converted to seconds

    def test_title_defaults_to_filename(self, tmp_path):
        df   = _make_ohlcv_df(5)
        path = str(tmp_path / "btcusdt_1d.csv")
        df.to_csv(path, index=False)

        c = Chart.from_csv(path)
        assert c.title == "btcusdt_1d.csv"

    def test_custom_title_used(self, tmp_path):
        df   = _make_ohlcv_df(5)
        path = str(tmp_path / "data.csv")
        df.to_csv(path, index=False)

        c = Chart.from_csv(path, title="My Chart")
        assert c.title == "My Chart"

    def test_heikinashi_type_respected(self, tmp_path):
        df   = _make_ohlcv_df(10)
        path = str(tmp_path / "data.csv")
        df.to_csv(path, index=False)

        c = Chart.from_csv(path, chart_type="heikinashi")
        assert c.chart_type == "heikinashi"

    def test_raises_on_missing_file(self):
        with pytest.raises(Exception):   # FileNotFoundError from pandas
            Chart.from_csv("/nonexistent/path/to/data.csv")


# ===========================================================================
# Group 8 — Drawings
# ===========================================================================

class TestDrawings:

    def test_add_hline_stored(self):
        c = _make_chart()
        c.add_hline(50000, color="#ff0000", label="R")
        assert len(c._drawings) == 1
        assert isinstance(c._drawings[0], HorizontalLine)
        assert c._drawings[0].price == 50000

    def test_add_signal_stored(self):
        c = _make_chart()
        c.add_signal(ANCHOR_S, "buy", price=100.0, label="Entry")
        assert len(c._drawings) == 1
        assert isinstance(c._drawings[0], Signal)
        assert c._drawings[0].side == "buy"

    def test_add_trendline_stored(self):
        c = _make_chart()
        c.add_trendline(ANCHOR_S, 100, ANCHOR_S + ONE_DAY_S, 110)
        assert len(c._drawings) == 1
        assert isinstance(c._drawings[0], TrendLine)

    def test_add_box_stored(self):
        c = _make_chart()
        c.add_box(ANCHOR_S, 100, ANCHOR_S + ONE_DAY_S, 110)
        assert isinstance(c._drawings[0], Box)

    def test_add_fib_default_levels(self):
        c = _make_chart()
        c.add_fib(ANCHOR_S, 100, ANCHOR_S + ONE_DAY_S, 110)
        fib = c._drawings[0]
        assert isinstance(fib, FibRetracement)
        assert 0.618 in fib.levels

    def test_add_fib_custom_levels(self):
        c = _make_chart()
        c.add_fib(ANCHOR_S, 100, ANCHOR_S + ONE_DAY_S, 110, levels=[0, 0.5, 1.0])
        assert c._drawings[0].levels == [0, 0.5, 1.0]

    def test_remove_drawing_by_id(self):
        c = _make_chart()
        c.add_hline(50000)
        drawing_id = c._drawings[0].id
        c.remove_drawing(drawing_id)
        assert len(c._drawings) == 0

    def test_remove_nonexistent_drawing_no_error(self):
        c = _make_chart()
        c.remove_drawing("nonexistent_id")   # must not raise

    def test_clear_drawings(self):
        c = _make_chart()
        c.add_hline(1000)
        c.add_signal(ANCHOR_S, "sell")
        c.clear_drawings()
        assert len(c._drawings) == 0

    def test_multiple_drawings_stored(self):
        c = _make_chart()
        c.add_hline(1000)
        c.add_hline(2000)
        c.add_signal(ANCHOR_S, "buy")
        assert len(c._drawings) == 3

    def test_method_chaining(self):
        c = _make_chart()
        result = c.add_hline(1000).add_signal(ANCHOR_S, "buy").add_trendline(
            ANCHOR_S, 100, ANCHOR_S + ONE_DAY_S, 110
        )
        assert result is c   # all methods return self


# ===========================================================================
# Group 9 — save_layout / load_layout
# ===========================================================================

class TestLayout:

    def test_save_creates_json_file(self, tmp_path):
        c    = _make_chart()
        c.add_hline(50000, label="R")
        path = str(tmp_path / "layout.json")
        c.save_layout(path)
        assert os.path.isfile(path)

    def test_saved_json_is_valid(self, tmp_path):
        c    = _make_chart()
        path = str(tmp_path / "layout.json")
        c.save_layout(path)
        with open(path) as f:
            data = json.load(f)
        assert "title"    in data
        assert "drawings" in data

    def test_drawings_survive_roundtrip(self, tmp_path):
        c = _make_chart()
        c.add_hline(50000, label="Resistance")
        c.add_signal(ANCHOR_S, "buy")
        path = str(tmp_path / "layout.json")
        c.save_layout(path)

        c2 = Chart(title="empty")
        c2.load_layout(path)
        assert len(c2._drawings) == 2

    def test_indicators_survive_roundtrip(self, tmp_path):
        c   = _make_chart()
        ind = pd.DataFrame({"time": [ANCHOR_S, ANCHOR_S + ONE_DAY_S], "value": [100.0, 101.0]})
        c.add_indicator(ind, name="EMA 20", color="#ff0000")
        path = str(tmp_path / "layout.json")
        c.save_layout(path)

        c2 = Chart(title="empty")
        c2.load_layout(path)
        assert len(c2._indicators) == 1
        assert c2._indicators[0].name  == "EMA 20"
        assert c2._indicators[0].color == "#ff0000"

    def test_chart_type_survives_roundtrip(self, tmp_path):
        c = Chart(title="HA", chart_type="heikinashi")
        c.set_data(_make_ohlcv_df())
        path = str(tmp_path / "layout.json")
        c.save_layout(path)

        c2 = Chart(title="empty")
        c2.load_layout(path)
        assert c2.chart_type == "heikinashi"


# ===========================================================================
# Group 10 — Server
# ===========================================================================

class TestServer:

    def test_find_free_port_returns_int(self):
        port = _find_free_port()
        assert isinstance(port, int)
        assert 8700 <= port <= 8900

    def test_find_free_port_is_actually_free(self):
        import socket
        port = _find_free_port()
        # We should be able to bind to it
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", port))   # must not raise

    def test_chart_server_starts_without_browser(self):
        """The server must start cleanly without opening a browser tab."""
        c = Chart(title="No Browser Test")
        c.set_data(_make_ohlcv_df(5))
        c.show(open_browser=False)
        time.sleep(0.3)
        assert c._shown is True
        assert c.url.startswith("http://127.0.0.1:")
        c.stop()

    def test_two_charts_use_different_ports(self):
        c1 = Chart(title="Chart 1")
        c2 = Chart(title="Chart 2")
        assert c1._server.port != c2._server.port


# ===========================================================================
# Group 11 — Repr
# ===========================================================================

class TestRepr:

    def test_repr_contains_title(self):
        c = _make_chart()
        r = repr(c)
        assert "Test" in r

    def test_repr_contains_bar_count(self):
        c = _make_chart(7)
        r = repr(c)
        assert "7" in r

    def test_repr_contains_drawing_count(self):
        c = _make_chart()
        c.add_hline(1000)
        r = repr(c)
        assert "1" in r


# ===========================================================================
# Group 12 — add_indicator_spec()  (v0.8.0 backend indicator toolbar)
# ===========================================================================

def _make_big_df(n: int = 120) -> pd.DataFrame:
    """OHLCV with a `timestamp` (ms) column and enough bars for warmups."""
    ts = [ANCHOR_MS + i * 900_000 for i in range(n)]          # 15m bars (ms)
    rng = np.random.default_rng(7)
    close = 100 + np.cumsum(rng.normal(0, 0.5, n))
    return pd.DataFrame({
        "timestamp": ts,
        "open":   close + rng.normal(0, 0.1, n),
        "high":   close + np.abs(rng.normal(0.5, 0.2, n)),
        "low":    close - np.abs(rng.normal(0.5, 0.2, n)),
        "close":  close,
        "volume": np.abs(rng.normal(10, 2, n)),
    })


class TestIndicatorSpec:
    """Server-side indicator computation feeding the chart toolbar / config."""

    def _chart(self) -> Chart:
        c = Chart(title="spec")
        c.set_data(_make_big_df())
        return c

    @pytest.mark.parametrize("spec,n_series", [
        ({"kind": "ema",  "period": 20}, 1),
        ({"kind": "sma",  "period": 30}, 1),
        ({"kind": "wma",  "period": 10}, 1),
        ({"kind": "smma", "period": 15}, 1),
        ({"kind": "dema", "period": 21}, 1),
        ({"kind": "tema", "period": 9},  1),
        ({"kind": "hma",  "period": 16}, 1),
        ({"kind": "vwma", "period": 20}, 1),
        ({"kind": "atr",  "period": 14}, 1),
        ({"kind": "macd", "fast": 12, "slow": 26, "signal_period": 9}, 3),
    ])
    def test_kinds_compute_and_tag_spec(self, spec, n_series):
        c = self._chart()
        before = len(c._indicators)
        out = c.add_indicator_spec(dict(spec))
        added = c._indicators[before:]
        assert out is not None
        assert len(added) == n_series
        for ind in added:
            assert ind.payload.get("spec", {}).get("kind") == spec["kind"]

    def test_vwap_builds(self):
        c = self._chart()
        out = c.add_indicator_spec({"kind": "vwap"})
        assert out["kind"] == "vwap"
        assert any(p.payload["name"] == "VWAP" for p in c._indicators)

    def test_rsi_default_none_ma_does_not_crash(self):
        # ma_type defaults to "none"; pre-0.8.0 this raised None.upper().
        c = self._chart()
        out = c.add_indicator_spec({"kind": "rsi", "period": 14})
        assert out["ma_type"] == "none"
        rsi_lines = [p for p in c._indicators if p.payload["name"].startswith("RSI")]
        assert len(rsi_lines) == 1

    def test_rsi_with_ma_adds_two_series(self):
        c = self._chart()
        c.add_indicator_spec(
            {"kind": "rsi", "period": 14, "ma_type": "sma", "ma_period": 9}
        )
        names = [p.payload["name"] for p in c._indicators]
        assert any(n.startswith("RSI") for n in names)
        assert any(n.startswith("SMA") for n in names)

    def test_ichimoku_arg_order_matches_direct(self):
        from AlgoTradeKit.indicator import Ichimoku
        c = self._chart()
        c.add_indicator_spec({
            "kind": "ichimoku", "tenkan": 8, "kijun": 22,
            "senkou_b": 44, "displacement": 22,
        })
        df = c.df
        ichi = Ichimoku(
            df["high"], df["low"], df["close"],
            tenkan_period=8, kijun_period=22,
            senkou_b_period=44, displacement=22,
        )
        ten = [p for p in c._indicators
               if p.payload["name"] == "Tenkan(8)"][0].payload["data"]
        got = [d["value"] for d in ten]
        exp = ichi.tenkan.dropna().tolist()
        assert len(got) == len(exp)
        assert got == pytest.approx(exp, abs=1e-9)

    def test_ichimoku_displacement_kept_in_spec(self):
        c = self._chart()
        out = c.add_indicator_spec({
            "kind": "ichimoku", "tenkan": 9, "kijun": 26,
            "senkou_b": 52, "displacement": 30,
        })
        assert out["displacement"] == 30

    def test_unknown_kind_returns_none(self):
        c = self._chart()
        assert c.add_indicator_spec({"kind": "nope"}) is None

    def test_empty_chart_returns_none(self):
        c = Chart(title="empty")
        assert c.add_indicator_spec({"kind": "ema", "period": 20}) is None

    def test_spec_present_in_init_payload(self):
        c = self._chart()
        c.add_indicator_spec({"kind": "ema", "period": 50})
        init = c._build_init_payload()
        assert any("spec" in i for i in init["indicators"])

    def test_time_seconds_column_fallback(self):
        # A chart built from a 'time' (seconds) DataFrame still computes.
        c = Chart(title="t")
        c.set_data(_make_ohlcv_df(60))
        out = c.add_indicator_spec({"kind": "ema", "period": 10})
        assert out is not None


class TestChartIndicatorsConfig:
    """SimulateConfig.chart_indicators validation (v0.8.0)."""

    def test_default_is_empty_list(self):
        from AlgoTradeKit.simulate import SimulateConfig
        assert SimulateConfig().chart_indicators == []

    def test_accepts_list_of_specs(self):
        from AlgoTradeKit.simulate import SimulateConfig
        cfg = SimulateConfig(chart_indicators=[
            {"kind": "rsi", "period": 14},
            {"kind": "ema", "period": 200},
        ])
        assert len(cfg.chart_indicators) == 2

    @pytest.mark.parametrize("bad", ["x", [1], [{"period": 14}], [None]])
    def test_rejects_bad(self, bad):
        from AlgoTradeKit.simulate import SimulateConfig
        with pytest.raises(ValueError):
            SimulateConfig(chart_indicators=bad)
