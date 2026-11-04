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

# ---------------------------------------------------------------------------
# We import from the local path — adjust when running inside the real package
# ---------------------------------------------------------------------------
import sys
import time

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from AlgoTradeKit.visual import Chart
from AlgoTradeKit.visual.models import (
    Bar,
    Box,
    FibRetracement,
    HorizontalLine,
    IndicatorSeries,
    Signal,
    TextLabel,
    TrendLine,
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


# ===========================================================================
# Group 13 — v1.0.0: chart host + live push + rolling candle window
# ===========================================================================

from AlgoTradeKit.visual import LivePosition  # noqa: E402
from AlgoTradeKit.visual.models import (  # noqa: E402
    COLOR_SL_BREAKEVEN,
    COLOR_SL_LOSS,
    COLOR_SL_PROFIT,
    sl_zone_color,
)
from AlgoTradeKit.visual.server import ChartServer  # noqa: E402


class _FakeServer:
    """Records send() calls; mimics the ChartServer surface Chart uses."""

    def __init__(self):
        self.sent: list = []
        self._last_init = None
        self._last_navigate = None
        self.host = "127.0.0.1"
        self.port = 65000
        self.on_message = None

    def send(self, message):
        self.sent.append(message)
        if message.get("type") == "init":
            self._last_init = message

    def start(self, open_browser=True):
        pass

    def stop(self):
        pass


def _shown_chart(n: int = 10, **chart_kwargs) -> Chart:
    """Chart that believes it is shown, backed by a recording fake server."""
    c = Chart(title="Live", **chart_kwargs)
    c.set_data(_make_ohlcv_df(n))
    c._server = _FakeServer()
    c._shown = True
    c._send_init()          # populates the fake replay cache
    c._server.sent.clear()
    return c


def _sent(c: Chart) -> list:
    return c._server.sent


def _cached_drawing_ids(c: Chart) -> list:
    return [d["id"] for d in c._server._last_init["drawings"]]


class TestHostConfig:
    """ — configurable bind host on Chart and ChartServer."""

    def test_server_default_host_is_localhost(self):
        s = ChartServer(title="t")
        assert s.host == "127.0.0.1"
        assert s.url == f"http://127.0.0.1:{s.port}"

    def test_server_custom_host_appears_in_url(self):
        # Explicit port: a LAN IP is not bindable on this test machine, and
        # the auto-pick probe binds on the configured host by design.
        s = ChartServer(title="t", host="192.168.1.50", port=18999)
        assert s.host == "192.168.1.50"
        assert s.url == "http://192.168.1.50:18999"

    def test_probe_error_names_the_host(self):
        with pytest.raises(RuntimeError, match="10.255.255.1"):
            _find_free_port(host="10.255.255.1")   # not a local interface

    @pytest.mark.parametrize("bind_all", ["0.0.0.0", "::"])
    def test_bind_all_addresses_substituted_in_url(self, bind_all):
        s = ChartServer(title="t", host=bind_all)
        assert s.host == bind_all
        assert s.display_host == "127.0.0.1"
        assert s.url == f"http://127.0.0.1:{s.port}"

    def test_chart_passes_host_to_server(self):
        c = Chart(title="t", host="0.0.0.0")
        assert c._server.host == "0.0.0.0"
        assert c.url.startswith("http://127.0.0.1:")

    def test_chart_default_host_unchanged(self):
        c = Chart(title="t")
        assert c._server.host == "127.0.0.1"
        assert c.url == f"http://127.0.0.1:{c._server.port}"

    def test_find_free_port_accepts_host(self):
        import socket
        port = _find_free_port(host="127.0.0.1")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", port))   # must not raise

    def test_server_on_all_interfaces_serves_locally(self):
        """host='0.0.0.0' must bind and stay reachable via 127.0.0.1."""
        import urllib.request
        c = Chart(title="Bind All", host="0.0.0.0")
        c.set_data(_make_ohlcv_df(5))
        c.show(open_browser=False)
        try:
            with urllib.request.urlopen(c.url, timeout=5) as resp:
                assert resp.status == 200
                assert b"AlgoTradeKit" in resp.read()
        finally:
            c.stop()


class TestUpdateDrawingGeneric:
    """ — Chart.update_drawing(): server → browser update_drawing push."""

    def test_updates_stored_drawing_and_broadcasts_full_dict(self):
        c = _shown_chart()
        c.add_hline(105.0)
        d = c._drawings[-1]
        _sent(c).clear()

        c.update_drawing(d.id, price=120.0, label="moved")

        assert d.price == 120.0
        assert d.label == "moved"
        msg = _sent(c)[-1]
        assert msg["type"] == "update_drawing"
        # Full serialised dict, not a patch: derived keys are present
        assert msg["drawing"]["type"] == "hline"
        assert msg["drawing"]["price"] == 120.0
        assert "lineWidth" in msg["drawing"]

    def test_unknown_id_is_silent_noop(self):
        c = _shown_chart()
        out = c.update_drawing("nope_999", price=1.0)
        assert out is c
        assert _sent(c) == []

    def test_unknown_field_is_ignored(self):
        c = _shown_chart()
        c.add_hline(105.0)
        d = c._drawings[-1]
        c.update_drawing(d.id, bogus_field=42)
        assert not hasattr(d, "bogus_field")

    def test_position_box_derived_zones_recomputed(self):
        c = _shown_chart()
        c.add_position_box(
            open_time=ANCHOR_S, close_time=ANCHOR_S + 3 * ONE_DAY_S,
            entry_price=100.0, stop_loss=95.0, take_profit=110.0,
            direction="long", net_pnl=50.0,
        )
        d = c._drawings[-1]
        _sent(c).clear()

        c.update_drawing(d.id, stop_loss=97.0)

        msg = _sent(c)[-1]
        assert msg["drawing"]["stop_loss"] == 97.0
        assert msg["drawing"]["loss_bottom"] == 97.0   # derived field followed

    def test_strategy_raw_drawing_payload_updated(self):
        from types import SimpleNamespace

        from AlgoTradeKit.visual.indicator_renderer import add_strategy_drawings
        c = _shown_chart()
        add_strategy_drawings(c, SimpleNamespace(drawings=[
            {"type": "hline", "price": 42.0, "id": "strat_h1"},
        ]))
        _sent(c).clear()

        c.update_drawing("strat_h1", price=43.5)

        msg = _sent(c)[-1]
        assert msg["type"] == "update_drawing"
        assert msg["drawing"]["price"] == 43.5

    def test_not_shown_updates_state_without_sending(self):
        c = _make_chart()
        c.add_hline(105.0)
        d = c._drawings[-1]
        c.update_drawing(d.id, price=99.0)   # must not raise
        assert d.price == 99.0


class TestLivePosition:
    """ — open-position drawing: add / stream SL & TP changes / remove."""

    def test_add_returns_id_and_stores_drawing(self):
        c = _shown_chart()
        pid = c.add_live_position(ANCHOR_S, 100.0, 95.0, "long",
                                  next_tp=110.0, trade_id=7, label="L #7")
        assert isinstance(pid, str)
        d = c._drawings[-1]
        assert isinstance(d, LivePosition)
        assert d.id == pid
        payload = d.to_dict()
        assert payload["type"] == "live_position"
        assert payload["open_time"] == ANCHOR_S
        assert payload["entry_price"] == 100.0
        assert payload["stop_loss"] == 95.0
        assert payload["next_tp"] == 110.0
        assert payload["direction"] == "long"
        assert payload["trade_id"] == 7
        assert payload["label"] == "L #7"
        assert payload["locked"] is True
        assert payload["source"] == "server"

    def test_payload_is_json_serialisable(self):
        c = _shown_chart()
        c.add_live_position(ANCHOR_S, 100.0, 95.0, "short")
        json.dumps(c._drawings[-1].to_dict())   # must not raise

    @pytest.mark.parametrize("direction,sl,expected", [
        ("long",  95.0,  COLOR_SL_LOSS),       # below entry → loss zone
        ("long",  100.0, COLOR_SL_BREAKEVEN),  # at entry → break-even
        ("long",  103.0, COLOR_SL_PROFIT),     # above entry → profit
        ("short", 105.0, COLOR_SL_LOSS),
        ("short", 100.0, COLOR_SL_BREAKEVEN),
        ("short", 97.0,  COLOR_SL_PROFIT),
    ])
    def test_sl_color_matches_zone(self, direction, sl, expected):
        c = _shown_chart()
        c.add_live_position(ANCHOR_S, 100.0, sl, direction)
        assert c._drawings[-1].to_dict()["sl_color"] == expected
        assert sl_zone_color(sl, 100.0, direction) == expected

    def test_add_broadcasts_and_caches(self):
        c = _shown_chart()
        pid = c.add_live_position(ANCHOR_S, 100.0, 95.0, "long")
        msg = _sent(c)[-1]
        assert msg["type"] == "add_drawing"
        assert msg["drawing"]["type"] == "live_position"
        assert pid in _cached_drawing_ids(c)    # replay cache is fresh

    def test_update_stop_loss_recolours_line(self):
        c = _shown_chart()
        pid = c.add_live_position(ANCHOR_S, 100.0, 95.0, "long")
        _sent(c).clear()

        c.update_live_position(pid, stop_loss=100.0)   # risk-free jump
        msg = _sent(c)[-1]
        assert msg["type"] == "update_drawing"
        assert msg["drawing"]["stop_loss"] == 100.0
        assert msg["drawing"]["sl_color"] == COLOR_SL_BREAKEVEN

        c.update_live_position(pid, stop_loss=104.0)   # trailing into profit
        assert _sent(c)[-1]["drawing"]["sl_color"] == COLOR_SL_PROFIT

    def test_update_next_tp_none_clears_but_omitted_keeps(self):
        c = _shown_chart()
        pid = c.add_live_position(ANCHOR_S, 100.0, 95.0, "long", next_tp=110.0)
        d = c._drawings[-1]

        c.update_live_position(pid, stop_loss=96.0)    # next_tp untouched
        assert d.next_tp == 110.0

        c.update_live_position(pid, next_tp=115.0)     # next multi-RR level
        assert d.next_tp == 115.0

        c.update_live_position(pid, next_tp=None)      # ladder exhausted
        assert d.next_tp is None
        assert _sent(c)[-1]["drawing"]["next_tp"] is None

    def test_update_label(self):
        c = _shown_chart()
        pid = c.add_live_position(ANCHOR_S, 100.0, 95.0, "long", label="a")
        c.update_live_position(pid, label="b")
        assert c._drawings[-1].label == "b"

    def test_invalid_direction_raises(self):
        c = _shown_chart()
        with pytest.raises(ValueError):
            c.add_live_position(ANCHOR_S, 100.0, 95.0, "sideways")

    def test_update_unknown_id_is_silent_noop(self):
        c = _shown_chart()
        c.update_live_position("livepos_none", stop_loss=1.0)
        assert _sent(c) == []

    def test_remove_broadcasts_and_uncaches(self):
        c = _shown_chart()
        pid = c.add_live_position(ANCHOR_S, 100.0, 95.0, "long")
        _sent(c).clear()

        c.remove_drawing(pid)

        assert all(getattr(d, "id", None) != pid for d in c._drawings)
        assert _sent(c)[-1] == {"type": "remove_drawing", "id": pid}
        assert pid not in _cached_drawing_ids(c)


class TestCandleLimit:
    """ — rolling candle window (client trim + Python mirror)."""

    @pytest.mark.parametrize("bad", [0, -3])
    def test_non_positive_limit_raises(self, bad):
        with pytest.raises(ValueError):
            _make_chart().set_candle_limit(bad)
        with pytest.raises(ValueError):
            Chart(title="t", candle_count_limit=bad)

    def test_set_limit_trims_bars_in_place(self):
        c = _make_chart(10)
        bars_ref = c._bars
        c.set_candle_limit(4)
        assert c._bars is bars_ref              # in place — init cache shares it
        assert len(c._bars) == 4
        assert c._bars[0]["time"] == ANCHOR_S + 6 * ONE_DAY_S

    def test_constructor_limit_applies_on_set_data(self):
        c = Chart(title="t", candle_count_limit=5)
        c.set_data(_make_ohlcv_df(10))
        assert len(c._bars) == 5

    def test_under_limit_is_noop(self):
        c = _make_chart(10)
        bars_before = list(c._bars)
        c.set_candle_limit(20)
        assert c._bars == bars_before

    def test_none_disables_the_window(self):
        c = _make_chart(10)
        c.set_candle_limit(4)
        c.set_candle_limit(None)
        for i in range(10, 15):
            c.stream({"time": ANCHOR_S + i * ONE_DAY_S,
                      "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5})
        assert len(c._bars) == 9   # 4 kept + 5 streamed, no trimming

    def test_expiry_rules_per_drawing_type(self):
        c = _make_chart(10)

        def t(i):
            return ANCHOR_S + i * ONE_DAY_S
        c.add_hline(100.0)                                        # timeless — kept
        c.add_signal(t(1), "buy")                                 # old — dropped
        c.add_trendline(t(0), 100.0, t(2), 101.0)                 # old — dropped
        c.add_box(t(2), 100.0, t(8), 105.0)                       # spans window — kept
        c.add_position_box(t(0), t(1), 100.0, 95.0, 110.0,
                           "long", 5.0)                           # closed early — dropped
        c.add_position_box(t(5), t(8), 100.0, 95.0, 110.0,
                           "long", 5.0)                           # ends inside — kept
        live_id = c.add_live_position(t(0), 100.0, 95.0, "long")  # open — kept

        c.set_candle_limit(4)   # cutoff = t(6)

        kept_types = [d.to_dict()["type"] for d in c._drawings]
        assert kept_types == ["hline", "box", "position_box", "live_position"]
        assert any(getattr(d, "id", None) == live_id for d in c._drawings)

    def test_stream_enforces_limit(self):
        c = _make_chart(10)
        c.set_candle_limit(4)
        c.stream({"time": ANCHOR_S + 10 * ONE_DAY_S,
                  "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5})
        assert len(c._bars) == 4
        assert c._bars[0]["time"] == ANCHOR_S + 7 * ONE_DAY_S
        assert c._bars[-1]["time"] == ANCHOR_S + 10 * ONE_DAY_S

    def test_set_limit_broadcasts_message(self):
        c = _shown_chart()
        c.set_candle_limit(4)
        assert {"type": "set_candle_limit", "limit": 4} in _sent(c)
        c.set_candle_limit(None)
        assert {"type": "set_candle_limit", "limit": None} in _sent(c)

    def test_init_payload_carries_limit(self):
        c = _make_chart(10)
        c.set_candle_limit(4)
        init = c._build_init_payload()
        assert init["candleCountLimit"] == 4
        assert len(init["bars"]) == 4

    def test_stream_eviction_refreshes_drawing_cache(self):
        c = _shown_chart(10)
        c.add_signal(ANCHOR_S + 6 * ONE_DAY_S, "buy")     # first window bar
        c.set_candle_limit(4)                             # window = bars 6..9
        assert len(_cached_drawing_ids(c)) == 1

        # Next bar pushes the window to 7..10 → the signal expires
        c.stream({"time": ANCHOR_S + 10 * ONE_DAY_S,
                  "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5})
        assert _cached_drawing_ids(c) == []
        assert len(c._server._last_init["bars"]) == 4


class TestReplayCacheFresh:
    """ — a page refresh mid-live-session reproduces the current state."""

    def test_add_drawing_lands_in_cache(self):
        c = _shown_chart()
        c.add_hline(101.0)
        d = c._drawings[-1]
        assert d.id in _cached_drawing_ids(c)

    def test_update_drawing_reflected_in_cache(self):
        c = _shown_chart()
        c.add_hline(101.0)
        d = c._drawings[-1]
        c.update_drawing(d.id, price=222.0)
        cached = [x for x in c._server._last_init["drawings"] if x["id"] == d.id][0]
        assert cached["price"] == 222.0

    def test_remove_drawing_reflected_in_cache(self):
        c = _shown_chart()
        c.add_hline(101.0)
        d = c._drawings[-1]
        c.remove_drawing(d.id)
        assert d.id not in _cached_drawing_ids(c)

    def test_clear_drawings_reflected_in_cache(self):
        c = _shown_chart()
        c.add_hline(101.0)
        c.add_signal(ANCHOR_S, "buy")
        c.clear_drawings()
        assert _cached_drawing_ids(c) == []

    def test_browser_edit_reflected_in_cache(self):
        c = _shown_chart()
        c.add_hline(101.0)
        d = c._drawings[-1]
        c._handle_browser_message(
            {"type": "drawing_updated", "drawing": {"id": d.id, "price": 333.0}}
        )
        cached = [x for x in c._server._last_init["drawings"] if x["id"] == d.id][0]
        assert cached["price"] == 333.0

    def test_browser_delete_reflected_in_cache(self):
        c = _shown_chart()
        c.add_hline(101.0)
        d = c._drawings[-1]
        c._handle_browser_message({"type": "drawing_deleted", "id": d.id})
        assert d.id not in _cached_drawing_ids(c)

    def test_not_shown_adds_do_not_touch_cache(self):
        c = _make_chart()
        c.add_hline(101.0)      # must not raise; nothing cached
        assert c._server._last_init is None


class TestPushProtocolE2E:
    """ — real server + real WebSocket client: the live push protocol."""

    def test_live_push_and_replay_over_websocket(self):
        from websockets.sync.client import connect

        c = Chart(title="E2E", candle_count_limit=None)
        c.set_data(_make_ohlcv_df(10))
        c.show(open_browser=False)
        try:
            ws_url = f"ws://127.0.0.1:{c._server.port}/ws"
            with connect(ws_url) as ws:
                init = json.loads(ws.recv(timeout=5))
                assert init["type"] == "init"
                assert init["candleCountLimit"] is None
                assert len(init["bars"]) == 10
                # v1.3.0: every page is then told what it may change
                assert json.loads(ws.recv(timeout=5)) == {"type": "access", "edit": True}

                # 1. Trade opens → add_drawing(live_position)
                pid = c.add_live_position(ANCHOR_S + 9 * ONE_DAY_S,
                                          109.0, 104.0, "long", next_tp=119.0)
                msg = json.loads(ws.recv(timeout=5))
                assert msg["type"] == "add_drawing"
                assert msg["drawing"]["type"] == "live_position"
                assert msg["drawing"]["sl_color"] == COLOR_SL_LOSS

                # 2. Risk-free jump → update_drawing with new colour
                c.update_live_position(pid, stop_loss=109.0)
                msg = json.loads(ws.recv(timeout=5))
                assert msg["type"] == "update_drawing"
                assert msg["drawing"]["sl_color"] == COLOR_SL_BREAKEVEN

                # 3. Rolling window armed → set_candle_limit
                c.set_candle_limit(5)
                msg = json.loads(ws.recv(timeout=5))
                assert msg == {"type": "set_candle_limit", "limit": 5}

                # 4. Candles still stream (unchanged v0.3 protocol)
                c.stream_from_atk({"timestamp": (ANCHOR_S + 10 * ONE_DAY_S) * 1000,
                                   "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5})
                msg = json.loads(ws.recv(timeout=5))
                assert msg["type"] == "stream"

                # 5. Trade closes → remove_drawing
                c.remove_drawing(pid)
                msg = json.loads(ws.recv(timeout=5))
                assert msg == {"type": "remove_drawing", "id": pid}

                # 6. Final position box pushed live
                c.add_position_box(ANCHOR_S + 9 * ONE_DAY_S, ANCHOR_S + 10 * ONE_DAY_S,
                                   109.0, 104.0, 119.0, "long", 42.0)
                msg = json.loads(ws.recv(timeout=5))
                assert msg["type"] == "add_drawing"
                assert msg["drawing"]["type"] == "position_box"

            # A SECOND client (page refresh) replays the CURRENT state:
            # trimmed bars, armed window, final posbox — no stale init.
            with connect(ws_url) as ws2:
                init2 = json.loads(ws2.recv(timeout=5))
                assert init2["type"] == "init"
                assert init2["candleCountLimit"] == 5
                assert len(init2["bars"]) == 5
                types = [d["type"] for d in init2["drawings"]]
                assert "position_box" in types
                assert "live_position" not in types
        finally:
            c.stop()


class TestFrontendWiring:
    """ — the shipped frontend handles the new protocol messages."""

    @pytest.fixture()
    def html(self) -> str:
        import AlgoTradeKit.visual.server as _srv
        return (_srv.STATIC_DIR / "index.html").read_text(encoding="utf-8")

    def test_handles_set_candle_limit_message(self, html):
        assert "case 'set_candle_limit'" in html
        assert "enforceCandleLimit" in html
        assert "candleCountLimit" in html

    def test_renders_live_position_drawings(self, html):
        assert "live_position" in html
        assert "sl_color" in html
        assert "next_tp" in html

    def test_handles_update_drawing_message(self, html):
        assert "case 'update_drawing'" in html

    def test_trim_covers_drawings_and_indicators(self, html):
        assert "drawingEndTime" in html
        assert "ichimokuCloud.spanA = ichimokuCloud.spanA.filter" in html

    def test_stream_bar_enforces_window(self, html):
        # streamBar must call the window enforcement on every new bar
        stream_fn = html.split("function streamBar")[1].split("function ")[0]
        assert "enforceCandleLimit()" in stream_fn


# ===========================================================================
# Colours (v1.3.0): Chart(style=), set_style, the layout file
# ===========================================================================

import re  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from AlgoTradeKit.visual._style import check_style  # noqa: E402
from AlgoTradeKit.visual.indicator_renderer import add_strategy_drawings  # noqa: E402

_STYLE = {"background": "#101820", "up_color": "#ffffff", "down_color": "#808080",
          "grid": False}


def _refused(style, where="style") -> str:
    with pytest.raises(ValueError) as raised:
        check_style(style, where=where)
    return str(raised.value)


class TestCheckStyle:
    def test_its_three_sentences(self):
        assert _refused({"background": "black"}) == (
            "style['background']: 'black' is not a colour — use '#rrggbb'")
        assert _refused({"grid": "off"}) == "style['grid']: 'off' is not True or False"
        assert _refused({"bg": "#000000"}) == (
            "style: unknown key 'bg' — known: background, up_color, down_color, grid")

    def test_a_style_that_is_not_a_dict(self):
        assert _refused("dark") == (
            "style: 'dark' is not a dict of background, up_color, down_color and grid")

    def test_the_message_names_where_the_style_came_from(self):
        assert _refused({"background": "black"}, where="TraderConfig.chart_style") == (
            "TraderConfig.chart_style['background']: 'black' is not a colour — use '#rrggbb'")

    def test_three_six_or_eight_hex_digits_kept_as_written(self):
        style = {"background": "#ABC", "up_color": "#11223344", "down_color": "#ff0000",
                 "grid": True}
        assert check_style(style) == style
        for bad in ("#12345", "#1234567", "rgb(0,0,0)", "101820", 0x101820):
            assert "is not a colour" in _refused({"up_color": bad})

    def test_grid_is_a_bool_not_a_number(self):
        assert _refused({"grid": 1}) == "style['grid']: 1 is not True or False"

    def test_none_is_not_set(self):
        assert check_style(None) == {}
        assert check_style({"background": None, "grid": False}) == {"grid": False}


class TestChartStyle:
    def test_a_bad_style_is_refused_when_the_chart_is_made(self):
        with pytest.raises(ValueError, match=re.escape(
                "style['background']: 'black' is not a colour — use '#rrggbb'")):
            Chart(style={"background": "black"})

    def test_the_init_carries_only_the_keys_set(self):
        assert Chart(style={"background": "#101820"})._build_init_payload()["style"] == {
            "background": "#101820"}
        assert Chart()._build_init_payload()["style"] == {}

    def test_style_is_a_copy_both_ways(self):
        given = dict(_STYLE)
        c = Chart(style=given)
        given["background"] = "#000000"
        c.style["grid"] = True
        assert c.style == _STYLE

    def test_from_csv_takes_a_style(self, tmp_path):
        path = tmp_path / "candles.csv"
        _make_ohlcv_df(5, use_timestamp_col=True).to_csv(path, index=False)
        assert Chart.from_csv(str(path), style=_STYLE).style == _STYLE

    def test_set_style_merges_and_tells_the_open_page(self):
        c = _shown_chart(style={"background": "#101820"})
        c.set_style(up_color="#ffffff", grid=False)
        expected = {"background": "#101820", "up_color": "#ffffff", "grid": False}
        assert c.style == expected
        assert _sent(c) == [{"type": "style", "style": expected}]
        assert c._server._last_init["style"] == expected     # and a reload shows it

    def test_none_brings_back_the_themes_colour(self):
        c = _shown_chart(style=_STYLE)
        c.set_style(background=None)
        assert "background" not in c.style
        assert _sent(c)[-1]["style"] == {k: v for k, v in _STYLE.items() if k != "background"}

    def test_a_bad_field_changes_nothing(self):
        c = _shown_chart(style={"background": "#101820"})
        with pytest.raises(ValueError, match="unknown key 'bg'"):
            c.set_style(bg="#000000")
        assert c.style == {"background": "#101820"} and _sent(c) == []

    def test_before_show_it_only_changes_the_init(self):
        c = _make_chart()
        c._server = _FakeServer()
        c.set_style(grid=False)
        assert c._server.sent == []
        assert c._build_init_payload()["style"] == {"grid": False}

    def test_the_layout_file_keeps_the_style(self, tmp_path):
        path = tmp_path / "layout.json"
        Chart(style=_STYLE).save_layout(str(path))
        assert json.loads(path.read_text())["style"] == _STYLE
        restored = Chart()
        restored.load_layout(str(path))
        assert restored.style == _STYLE

    def test_a_layout_file_without_a_style_leaves_it(self, tmp_path):
        path = tmp_path / "old.json"
        path.write_text(json.dumps({"title": "Old", "indicators": [], "drawings": []}))
        c = Chart(style={"up_color": "#ffffff"})
        c.load_layout(str(path))
        assert c.style == {"up_color": "#ffffff"}


# ===========================================================================
# Gap 1 (v1.3.0): a strategy's drawing keeps what the browser did to it
# ===========================================================================

class TestStrategyDrawingEdits:
    @staticmethod
    def _chart_with_zone() -> Chart:
        c = _shown_chart()
        add_strategy_drawings(c, SimpleNamespace(drawings=[
            {"type": "box", "id": "zone", "time1": ANCHOR_S, "time2": ANCHOR_S + ONE_DAY_S,
             "price1": 100.0, "price2": 104.0, "color": "#26a69a", "label": "Zone"},
        ]))
        _sent(c).clear()
        return c

    def test_an_edit_reaches_a_fresh_init(self):
        c = self._chart_with_zone()
        c._handle_browser_message({"type": "drawing_updated", "drawing": {
            "id": "zone", "type": "box", "time1": ANCHOR_S, "time2": ANCHOR_S + 3 * ONE_DAY_S,
            "price1": 98.0, "price2": 104.0, "color": "#ff9800", "label": "Moved"}})
        (zone,) = c._build_init_payload()["drawings"]
        assert (zone["time2"], zone["price1"]) == (ANCHOR_S + 3 * ONE_DAY_S, 98.0)
        assert (zone["color"], zone["label"]) == ("#ff9800", "Moved")
        assert zone["id"] == "zone" and zone["source"] == "server"
        assert c._server._last_init["drawings"] == [zone]     # what a reload is sent

    def test_a_lock_change_reaches_a_fresh_init(self):
        c = self._chart_with_zone()
        c._handle_browser_message({"type": "drawing_lock_changed", "id": "zone",
                                   "locked": False})
        (zone,) = c._server._last_init["drawings"]
        assert zone["locked"] is False
        assert not hasattr(c._drawings[0], "locked")          # the payload, not the wrapper

    def test_the_librarys_own_drawings_are_edited_as_before(self):
        c = _shown_chart()
        c.add_box(ANCHOR_S, 100.0, ANCHOR_S + ONE_DAY_S, 104.0)
        box = c._drawings[-1]
        c._handle_browser_message({"type": "drawing_updated", "drawing": {
            "id": box.id, "price1": 98.0, "made_up": 1}})
        c._handle_browser_message({"type": "drawing_lock_changed", "id": box.id,
                                   "locked": True})
        assert (box.price1, box.locked) == (98.0, True)
        assert not hasattr(box, "made_up")


# ===========================================================================
# Edits on the page reach Python (v1.3.0, Part 2)
# ===========================================================================

import threading  # noqa: E402

from AlgoTradeKit.visual import ChartAction, EditRefused  # noqa: E402

_EDIT_FIELDS = ("key", "editable", "deletable", "title")


def _keyed_box_chart(**chart_kwargs):
    c = _shown_chart(**chart_kwargs)
    c.add_box(ANCHOR_S, 100.0, ANCHOR_S + ONE_DAY_S, 104.0, key="zone:1", title="Zone 1")
    _sent(c).clear()
    return c, c._drawings[-1]


def _moved(drawing, **fields) -> dict:
    """What a page sends after a drag: the whole drawing as it holds it."""
    return {"type": "drawing_updated", "drawing": {**drawing.to_dict(), **fields}}


def _refuse(why: str):
    def callback(edit):
        raise EditRefused(why)
    return callback


class TestDrawingEditFields:
    def test_every_drawing_says_how_it_may_be_changed(self):
        c = _make_chart()
        c.add_hline(1.0)
        c.add_trendline(ANCHOR_S, 1.0, ANCHOR_S + ONE_DAY_S, 2.0)
        c.add_box(ANCHOR_S, 1.0, ANCHOR_S + ONE_DAY_S, 2.0)
        c.add_signal(ANCHOR_S, "buy")
        c.add_text(ANCHOR_S, 1.0, "note")
        c.add_fib(ANCHOR_S, 1.0, ANCHOR_S + ONE_DAY_S, 2.0)
        c.add_position_box(ANCHOR_S, ANCHOR_S + ONE_DAY_S, 100.0, 95.0, 110.0, "long", 5.0)
        c.add_live_position(ANCHOR_S, 100.0, 95.0, "long")
        drawings = c._build_init_payload()["drawings"]
        assert len(drawings) == 8
        for d in drawings:
            assert {k: d[k] for k in _EDIT_FIELDS} == {
                "key": None, "editable": True, "deletable": True, "title": None}, d["type"]

    def test_the_add_methods_take_them_by_name_only(self):
        c = _make_chart()
        c.add_hline(1.0, key="level", editable=("price",), deletable=False, title="Level")
        d = c._build_init_payload()["drawings"][-1]
        assert [d[k] for k in _EDIT_FIELDS] == ["level", ["price"], False, "Level"]
        with pytest.raises(TypeError):
            c.add_hline(2.0, "#fff", 1, 0, "", "level2")      # a sixth positional argument
        c.add_box(ANCHOR_S, 1.0, ANCHOR_S + ONE_DAY_S, 2.0, editable=["price", "time"])
        assert c._drawings[-1].editable == ("price", "time")  # as JSON gives it back

    @pytest.mark.parametrize(("fields", "message"), [
        ({"editable": ("size",)}, "editable: 'size' is not 'price', 'time' or 'move'"),
        ({"editable": ()}, "editable: () names no handle — use False"),
        ({"editable": "yes"},
         "editable: 'yes' is not True, False or a tuple of 'price', 'time' and 'move'"),
        ({"key": ""}, "key: '' is not a non-empty string"),
        ({"deletable": "no"}, "deletable: 'no' is not True or False"),
        ({"title": 5}, "title: 5 is not a string"),
    ])
    def test_what_is_refused(self, fields, message):
        c = _make_chart()
        with pytest.raises(ValueError) as raised:
            c.add_box(ANCHOR_S, 1.0, ANCHOR_S + ONE_DAY_S, 2.0, **fields)
        assert str(raised.value) == message
        assert c._drawings == []

    def test_a_key_is_unique_on_its_chart(self):
        c = _make_chart()
        add_strategy_drawings(c, SimpleNamespace(drawings=[
            {"type": "box", "id": "strat_17", "key": "poi:sell:1793030100000", "time1": ANCHOR_S,
             "time2": ANCHOR_S + ONE_DAY_S, "price1": 2.0, "price2": 1.0}]))
        with pytest.raises(ValueError) as raised:
            c.add_box(ANCHOR_S, 1.0, ANCHOR_S + ONE_DAY_S, 2.0, key="poi:sell:1793030100000")
        assert str(raised.value) == (
            "key 'poi:sell:1793030100000' is already used by drawing 'strat_17'")
        c.add_hline(1.0, key="level")
        level = c._drawings[-1]
        with pytest.raises(ValueError, match="key 'level' is already used"):
            c.update_drawing("strat_17", key="level")
        c.remove_drawing(level.id)
        c.add_hline(2.0, key="level")                         # free again once it is gone

    def test_edit_key_is_a_non_empty_string(self):
        for bad in ("", 5):
            with pytest.raises(ValueError) as raised:
                Chart(edit_key=bad)
            assert str(raised.value) == f"edit_key: {bad!r} is not a non-empty string"

    def test_a_vertical_line(self, tmp_path):
        c = _make_chart()
        vid = c.add_vline(ANCHOR_S + 3600, label="Restart", style="solid", key="restart:1",
                          title="Restart 01:00")
        assert c._drawings[-1].to_dict() == {
            "type": "vline", "time": ANCHOR_S + 3600, "color": "#e3b341", "label": "Restart",
            "style": "solid", "id": vid, "locked": True, "source": "server", "key": "restart:1",
            "editable": False, "deletable": True, "title": "Restart 01:00"}
        with pytest.raises(ValueError, match="style: 'dotted' is not 'dashed' or 'solid'"):
            c.add_vline(ANCHOR_S, style="dotted")
        path = tmp_path / "layout.json"
        c.save_layout(str(path))
        restored = _make_chart()
        restored.load_layout(str(path))
        assert [d.to_dict() for d in restored._drawings] == [d.to_dict() for d in c._drawings]


class TestEditCallbacks:
    def test_edits_reach_every_callback_in_order_on_the_chart_thread(self):
        c, box = _keyed_box_chart()
        calls = []
        for name in ("first", "second"):
            c.on_drawing_edit(lambda e, name=name: calls.append(
                (name, threading.current_thread().name, e.before["price1"], e.after["price1"])))
        c._handle_browser_message(_moved(box, price1=98.0))
        c._handle_browser_message(_moved(box, price1=97.0))
        assert c._events.wait_idle(5)
        assert calls == [("first", "atk-chart-events", 100.0, 98.0),
                         ("second", "atk-chart-events", 100.0, 98.0),
                         ("first", "atk-chart-events", 98.0, 97.0),
                         ("second", "atk-chart-events", 98.0, 97.0)]

    def test_the_edit_names_its_drawing(self):
        c, box = _keyed_box_chart()
        got = []
        record = got.append
        assert c.on_drawing_edit(record) is record                 # it returns fn
        c._handle_browser_message(_moved(box, label="Moved"))
        assert c._events.wait_idle(5)
        (edit,) = got
        assert (edit.chart, edit.drawing_id, edit.key) == (c, box.id, "zone:1")
        assert (edit.before["label"], edit.after["label"]) == ("", "Moved")

    def test_a_press_that_moves_nothing_and_a_lock_report_nothing(self):
        c, box = _keyed_box_chart()
        calls = []
        c.on_drawing_edit(calls.append)
        c._handle_browser_message(_moved(box))
        c._handle_browser_message({"type": "drawing_lock_changed", "id": box.id,
                                   "locked": False})
        assert c._events.wait_idle(5)
        assert calls == [] and box.locked is False

    def test_a_drawing_without_a_key_calls_nothing(self):
        c = _shown_chart()
        c.add_box(ANCHOR_S, 100.0, ANCHOR_S + ONE_DAY_S, 104.0)
        box = c._drawings[-1]
        calls = []
        c.on_drawing_edit(calls.append)
        c.on_drawing_delete(calls.append)
        c._handle_browser_message(_moved(box, price1=98.0))
        c._handle_browser_message({"type": "drawing_deleted", "id": box.id})
        assert c._events.wait_idle(5)
        assert calls == [] and box.price1 == 98.0 and c._drawings == []

    def test_edit_refused_puts_the_drawing_back_and_tells_the_page(self):
        c, box = _keyed_box_chart()
        c.on_drawing_edit(_refuse("too tall"))
        c._handle_browser_message(_moved(box, price1=120.0))
        assert box.price1 == 120.0                              # stored at once ...
        assert c._events.wait_idle(5)
        assert box.price1 == 100.0                              # ... and put back
        update, refused = _sent(c)
        assert update["type"] == "update_drawing" and update["drawing"]["price1"] == 100.0
        assert refused == {"type": "edit_refused", "id": box.id, "message": "too tall"}
        assert c._server._last_init["drawings"][-1]["price1"] == 100.0   # and a reload

    def test_any_other_error_is_printed_and_undoes_the_edit(self, capsys):
        c, box = _keyed_box_chart()
        c.on_drawing_edit(lambda edit: 1 / 0)
        c._handle_browser_message(_moved(box, price2=110.0))
        assert c._events.wait_idle(5)
        assert box.price2 == 104.0
        assert ("[AlgoTradeKit] chart: on_drawing_edit failed — ZeroDivisionError: division by "
                "zero") in capsys.readouterr().out
        assert _sent(c)[-1] == {"type": "edit_refused", "id": box.id,
                                "message": "division by zero"}

    def test_a_strategy_drawing_is_put_back_too(self):
        c = _shown_chart()
        add_strategy_drawings(c, SimpleNamespace(drawings=[
            {"type": "box", "id": "poi", "key": "poi:sell:1", "time1": ANCHOR_S,
             "time2": ANCHOR_S + ONE_DAY_S, "price1": 104.0, "price2": 100.0}]))
        payload = c._drawings[0]._payload
        c.on_drawing_edit(_refuse("no"))
        c._handle_browser_message({"type": "drawing_updated",
                                   "drawing": {**payload, "price1": 106.0, "extra": 1}})
        assert c._events.wait_idle(5)
        assert payload["price1"] == 104.0 and "extra" not in payload

    def test_a_delete_reaches_its_callback_with_after_none(self):
        c, box = _keyed_box_chart()
        got = []
        c.on_drawing_delete(got.append)
        c._handle_browser_message({"type": "drawing_deleted", "id": box.id})
        assert box not in c._drawings                           # gone at once
        assert c._events.wait_idle(5)
        (edit,) = got
        assert (edit.drawing_id, edit.key, edit.after) == (box.id, "zone:1", None)
        assert edit.before["price1"] == 100.0

    def test_a_refused_delete_puts_the_drawing_back_where_it_was(self):
        c = _shown_chart()
        c.add_hline(1.0)
        c.add_box(ANCHOR_S, 100.0, ANCHOR_S + ONE_DAY_S, 104.0, key="zone:1")
        c.add_hline(2.0)
        box = c._drawings[1]
        c.on_drawing_delete(_refuse("keep it"))
        _sent(c).clear()
        c._handle_browser_message({"type": "drawing_deleted", "id": box.id})
        assert c._events.wait_idle(5)
        assert c._drawings[1] is box
        add, refused = _sent(c)
        assert add == {"type": "add_drawing", "drawing": box.to_dict()}
        assert refused == {"type": "edit_refused", "id": box.id, "message": "keep it"}
        assert box.id in [d["id"] for d in c._server._last_init["drawings"]]


class TestChartActions:
    def test_an_action_gets_its_candle_in_ms_on_the_chart_thread(self):
        c = _shown_chart()
        got = []
        c.add_action("Mark here", lambda a: got.append((threading.current_thread().name, a)))
        c._handle_browser_message({"type": "chart_action", "label": "Mark here",
                                   "time": ANCHOR_S, "price": 101.5})
        c._handle_browser_message({"type": "chart_action", "label": "Unknown",
                                   "time": ANCHOR_S, "price": 1.0})
        assert c._events.wait_idle(5)
        assert got == [("atk-chart-events", ChartAction(chart=c, label="Mark here",
                                                        time=ANCHOR_S * 1000, price=101.5))]

    def test_labels_are_unique_and_the_page_hears_the_menu(self):
        c = _shown_chart()
        c.add_action("Mark here", print)
        with pytest.raises(ValueError, match="action 'Mark here' is already on this chart"):
            c.add_action("Mark here", print)
        c.add_action("Restart here", print)
        assert _sent(c)[-1] == {"type": "actions", "actions": ["Mark here", "Restart here"]}
        c.remove_action("Mark here")
        assert _sent(c)[-1] == {"type": "actions", "actions": ["Restart here"]}
        assert c._server._last_init["actions"] == ["Restart here"]
        with pytest.raises(ValueError, match="no action 'Mark here' on this chart"):
            c.remove_action("Mark here")

    def test_an_action_that_raises_is_printed(self, capsys):
        c = _shown_chart()

        def broken(action):
            raise RuntimeError("no room")

        c.add_action("Mark here", broken)
        c._handle_browser_message({"type": "chart_action", "label": "Mark here",
                                   "time": ANCHOR_S, "price": None})
        assert c._events.wait_idle(5)
        assert ("[AlgoTradeKit] chart: action 'Mark here' failed — RuntimeError: no room"
                in capsys.readouterr().out)


class TestEditKey:
    def test_a_page_without_the_key_changes_nothing_anyone_sees(self):
        c, box = _keyed_box_chart()
        removed, ran = [], []
        c.on_indicator_removed = removed.append
        c.add_indicator_spec({"kind": "ema", "period": 3})
        c.add_action("Mark here", ran.append)
        _sent(c).clear()
        before = (box.to_dict(), c.display_timeframe, [i.name for i in c._indicators])
        stranger = {"_editor": False}
        for msg in ({"type": "drawing_updated", "drawing": {**box.to_dict(), "price1": 1.0}},
                    {"type": "drawing_lock_changed", "id": box.id, "locked": False},
                    {"type": "drawing_deleted", "id": box.id},
                    {"type": "chart_action", "label": "Mark here", "time": ANCHOR_S,
                     "price": 1.0},
                    {"type": "set_timeframe", "tf": "3d"},
                    {"type": "compute_indicator", "params": {"kind": "sma", "period": 3}},
                    {"type": "remove_indicator", "name": c._indicators[0].name}):
            c._handle_browser_message({**msg, **stranger})
        c._handle_browser_message({"type": "replay_arm", **stranger})   # replay is its own
        assert c._events.wait_idle(5)
        assert (box.to_dict(), c.display_timeframe, [i.name for i in c._indicators]) == before
        assert box in c._drawings and removed == [] and ran == []
        assert [m["type"] for m in _sent(c)] == ["replay_data"]

    def test_two_browsers_one_with_the_key(self):
        from websockets.sync.client import connect

        c = Chart(title="Keyed", edit_key="s3cret")
        c.set_data(_make_ohlcv_df(10))
        c.add_box(ANCHOR_S, 100.0, ANCHOR_S + ONE_DAY_S, 104.0, key="zone:1")
        box_id = c._drawings[0].id
        deleted = []
        c.on_drawing_delete(deleted.append)
        c.show(open_browser=False)
        try:
            url = f"ws://127.0.0.1:{c._server.port}/ws"
            with connect(url + "?key=s3cret") as owner, connect(url) as stranger:
                for ws in (owner, stranger):
                    assert json.loads(ws.recv(timeout=5))["type"] == "init"
                assert json.loads(owner.recv(timeout=5)) == {"type": "access", "edit": True}
                assert json.loads(stranger.recv(timeout=5)) == {"type": "access", "edit": False}

                stranger.send(json.dumps({"type": "drawing_deleted", "id": box_id}))
                stranger.send(json.dumps({"type": "replay_arm"}))     # answered after it
                for ws in (stranger, owner):
                    assert json.loads(ws.recv(timeout=5))["type"] == "replay_data"
                assert [d.id for d in c._drawings] == [box_id]        # a forged delete: nothing

                owner.send(json.dumps({"type": "drawing_deleted", "id": box_id}))
                owner.send(json.dumps({"type": "replay_arm"}))
                assert json.loads(owner.recv(timeout=5))["type"] == "replay_data"
                assert c._drawings == []
            assert c._events.wait_idle(5)
            (edit,) = deleted
            assert (edit.key, edit.after) == ("zone:1", None)
        finally:
            c.stop()

    def test_show_opens_the_address_with_the_key(self, monkeypatch):
        import webbrowser

        opened = []
        monkeypatch.setattr(webbrowser, "open", opened.append)
        c = Chart(title="Keyed", edit_key="a b&c")
        c.set_data(_make_ohlcv_df(5))
        c.show(open_browser=True)
        try:
            assert opened == [f"{c.url}/?title=Keyed&key=a%20b%26c"]
            assert c.url == f"http://127.0.0.1:{c._server.port}"      # unchanged
        finally:
            c.stop()
