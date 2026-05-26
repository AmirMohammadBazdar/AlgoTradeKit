import pandas as pd
import tempfile
import pytest
import os

from AlgoTradeKit.data._utils import (
    normalize_timeframe,
    parse_to_ms,
    ms_to_dt,
    ceil_to_tf,
    floor_to_tf,
    TIMEFRAME_MS,
)
from AlgoTradeKit.data.storage.csv_handler import CSVHandler
from AlgoTradeKit.data.sources import get_source
from AlgoTradeKit.data import Collector


# ---------------------------------------------------------------------------
# _utils
# ---------------------------------------------------------------------------

class TestNormalizeTimeframe:
    def test_lowercase(self):
        assert normalize_timeframe("1D") == "1d"
        assert normalize_timeframe("4H") == "4h"
        assert normalize_timeframe("1W") == "1w"

    def test_monthly_preserved(self):
        assert normalize_timeframe("1M") == "1M"

    def test_monthly_aliases(self):
        assert normalize_timeframe("1mo") == "1M"
        assert normalize_timeframe("1month") == "1M"

    def test_invalid_raises(self):
        with pytest.raises(ValueError, match="Unsupported timeframe"):
            normalize_timeframe("7d")

    def test_all_valid(self):
        for tf in TIMEFRAME_MS:
            assert normalize_timeframe(tf) == tf


class TestParseDatetime:
    def test_slash_format(self):
        ms = parse_to_ms("2020/01/01")
        assert ms == 1577836800000  # 2020-01-01 00:00 UTC

    def test_dash_format(self):
        assert parse_to_ms("2020-01-01") == 1577836800000

    def test_with_time(self):
        ms = parse_to_ms("2020/01/01 12:00")
        assert ms == 1577836800000 + 12 * 3600 * 1000

    def test_int_passthrough(self):
        assert parse_to_ms(1577836800000) == 1577836800000

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            parse_to_ms("not-a-date")


class TestTfAlignment:
    def test_floor(self):
        # floor 2020-01-01 12:34 to 1h grid
        ms = parse_to_ms("2020/01/01 12:34")
        tf_ms = TIMEFRAME_MS["1h"]
        result = floor_to_tf(ms, tf_ms)
        assert result == parse_to_ms("2020/01/01 12:00")

    def test_ceil(self):
        ms = parse_to_ms("2020/01/01 12:34")
        tf_ms = TIMEFRAME_MS["1h"]
        result = ceil_to_tf(ms, tf_ms)
        assert result == parse_to_ms("2020/01/01 13:00")

    def test_already_aligned(self):
        ms = parse_to_ms("2020/01/01 12:00")
        tf_ms = TIMEFRAME_MS["1h"]
        assert ceil_to_tf(ms, tf_ms) == ms
        assert floor_to_tf(ms, tf_ms) == ms


# ---------------------------------------------------------------------------
# CSVHandler
# ---------------------------------------------------------------------------

def _make_df(timestamps: list[int]) -> pd.DataFrame:
    """Helper: minimal DataFrame with given timestamps."""
    n = len(timestamps)
    return pd.DataFrame({
        "timestamp":       timestamps,
        "open":            [100.0] * n,
        "high":            [105.0] * n,
        "low":             [95.0]  * n,
        "close":           [102.0] * n,
        "volume":          [1000.0] * n,
        "close_time":      [t + 86399999 for t in timestamps],
        "quote_volume":    [100000.0] * n,
        "trades":          [500] * n,
        "taker_buy_base":  [500.0] * n,
        "taker_buy_quote": [50000.0] * n,
    })


class TestCSVHandler:
    def test_save_and_load(self, tmp_path):
        fp = str(tmp_path / "test.csv")
        handler = CSVHandler(fp)

        df = _make_df([1577836800000, 1577923200000])  # 2020-01-01, 2020-01-02
        handler.save(df)

        loaded = handler.load()
        assert loaded is not None
        assert len(loaded) == 2
        assert list(loaded["timestamp"]) == [1577836800000, 1577923200000]

    def test_no_duplicates_on_save(self, tmp_path):
        fp = str(tmp_path / "test.csv")
        handler = CSVHandler(fp)

        ts = 1577836800000
        df = _make_df([ts, ts, ts + 86400000])
        handler.save(df)

        loaded = handler.load()
        assert len(loaded) == 2

    def test_exists_false_when_missing(self, tmp_path):
        handler = CSVHandler(str(tmp_path / "nope.csv"))
        assert not handler.exists()

    def test_find_gaps_no_data_all_missing(self, tmp_path):
        fp = str(tmp_path / "empty.csv")
        handler = CSVHandler(fp)

        df = _make_df([])
        # Not saved — but test the gap method directly with an empty df
        start = parse_to_ms("2020/01/01")
        end   = parse_to_ms("2020/01/05")
        tf_ms = TIMEFRAME_MS["1d"]

        # Build an empty df manually
        import pandas as pd
        empty = pd.DataFrame(columns=[
            "timestamp","open","high","low","close","volume",
            "close_time","quote_volume","trades","taker_buy_base","taker_buy_quote"
        ])
        empty["timestamp"] = empty["timestamp"].astype("int64")

        gaps = handler.find_gaps(empty, "1d", start, end)
        # Should be one big gap covering the whole range
        assert len(gaps) == 1
        gap_start, gap_end = gaps[0]
        assert gap_start == start
        assert gap_end == end

    def test_find_gaps_tail_missing(self, tmp_path):
        fp = str(tmp_path / "partial.csv")
        handler = CSVHandler(fp)

        # Have 2020-01-01 and 2020-01-02, but want up to 2020-01-04
        ts1 = parse_to_ms("2020/01/01")
        ts2 = parse_to_ms("2020/01/02")
        df = _make_df([ts1, ts2])

        end_ms = parse_to_ms("2020/01/04")
        gaps = handler.find_gaps(df, "1d", ts1, end_ms)

        assert len(gaps) == 1
        assert gaps[0][0] == parse_to_ms("2020/01/03")

    def test_find_gaps_middle_missing(self, tmp_path):
        fp = str(tmp_path / "gap.csv")
        handler = CSVHandler(fp)

        ts1 = parse_to_ms("2020/01/01")
        ts4 = parse_to_ms("2020/01/04")  # skip 2 and 3
        df = _make_df([ts1, ts4])

        gaps = handler.find_gaps(df, "1d", ts1, ts4)

        assert len(gaps) == 1
        assert gaps[0][0] == parse_to_ms("2020/01/02")
        assert gaps[0][1] == parse_to_ms("2020/01/03")

    def test_merge_and_save(self, tmp_path):
        fp = str(tmp_path / "merge.csv")
        handler = CSVHandler(fp)

        existing = _make_df([parse_to_ms("2020/01/01")])
        new_rows = _make_df([parse_to_ms("2020/01/02")]).to_dict("records")

        combined = handler.merge_and_save(existing, new_rows)
        assert len(combined) == 2

        loaded = handler.load()
        assert len(loaded) == 2


# ---------------------------------------------------------------------------
# Sources registry
# ---------------------------------------------------------------------------

class TestGetSource:
    def test_binance_spot(self):
        src = get_source("binance-spot")
        assert src.__class__.__name__ == "BinanceSource"

    def test_binance_futures(self):
        src = get_source("binance-futures")
        assert src.__class__.__name__ == "BinanceSource"

    def test_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown source"):
            get_source("unknown-exchange")


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------

class TestCollector:
    def test_repr(self):
        c = Collector("binance-spot", "BTCUSDT", "1d")
        assert "binance-spot" in repr(c)
        assert "BTCUSDT" in repr(c)

    def test_raises_without_starttime(self):
        c = Collector("binance-spot", "BTCUSDT", "1d")
        with pytest.raises(ValueError, match="starttime"):
            c.collect()

    def test_raises_start_after_end(self):
        c = Collector("binance-spot", "BTCUSDT", "1d")
        c.starttime = "2023/01/01"
        c.endtime   = "2020/01/01"
        with pytest.raises(ValueError, match="before"):
            c.collect()

    def test_auto_filename(self):
        c = Collector("binance-futures", "ETHUSDT", "4h")
        c.starttime   = "2022/01/01"
        fp = c._resolve_filepath()
        assert "binance-futures" in fp
        assert "ETHUSDT" in fp
        assert "4h" in fp
        assert fp.endswith(".csv")

    def test_custom_filename(self):
        c = Collector("binance-spot", "BTCUSDT", "1d")
        c.outputname = "my_data.csv"
        fp = c._resolve_filepath()
        assert fp.endswith("my_data.csv")
