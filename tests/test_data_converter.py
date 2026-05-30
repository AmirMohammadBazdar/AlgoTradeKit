"""
Tests for algotradekit.data.Converter

Groups
------
1. _detect_timeframe   — infer TF from timestamp gaps
2. _validate_conversion — accept / reject conversion pairs
3. _resample           — correct OHLCV aggregation values
4. _drop_incomplete    — incomplete groups are removed
5. _resolve_filepath   — output path / filename logic
6. _load_source        — CSV and DataFrame inputs
7. convert()           — end-to-end integration
"""

import os
import pytest
import pandas as pd

from AlgoTradeKit.data import Converter
from AlgoTradeKit.data._utils import TIMEFRAME_MS, parse_to_ms


# ===========================================================================
# Shared helpers
# ===========================================================================

ONE_HOUR_MS  = TIMEFRAME_MS["1h"]
ONE_DAY_MS   = TIMEFRAME_MS["1d"]
FOUR_HOUR_MS = TIMEFRAME_MS["4h"]

# A fixed anchor time: 2020-01-06 00:00 UTC (Monday — clean week boundary)
ANCHOR = parse_to_ms("2020/01/06")


def _make_candles(
    start_ms: int,
    step_ms: int,
    n: int,
    open_: float  = 100.0,
    high_: float  = 110.0,
    low_:  float  = 90.0,
    close_: float = 105.0,
    volume_: float = 1000.0,
) -> pd.DataFrame:
    """
    Build a minimal OHLCV DataFrame with *n* candles spaced *step_ms* apart.
    All candles get the same OHLCV values (override per-test when needed).
    """
    timestamps = [start_ms + i * step_ms for i in range(n)]
    return pd.DataFrame({
        "timestamp":       timestamps,
        "open":            [open_]   * n,
        "high":            [high_]   * n,
        "low":             [low_]    * n,
        "close":           [close_]  * n,
        "volume":          [volume_] * n,
        "close_time":      [t + step_ms - 1 for t in timestamps],
        "quote_volume":    [volume_ * close_] * n,
        "trades":          [50] * n,
        "taker_buy_base":  [volume_ / 2] * n,
        "taker_buy_quote": [volume_ * close_ / 2] * n,
    })


def _make_converter(source, target_tf, source_tf=None) -> Converter:
    """Convenience: build a Converter without setting destination."""
    return Converter(source=source, target_timeframe=target_tf, source_timeframe=source_tf)


# ===========================================================================
# Group 1 — _detect_timeframe
# ===========================================================================

class TestDetectTimeframe:

    def test_detects_1h(self):
        df = _make_candles(ANCHOR, ONE_HOUR_MS, 10)
        conv = _make_converter(df, "4h")
        assert conv._detect_timeframe(df) == "1h"

    def test_detects_1d(self):
        df = _make_candles(ANCHOR, ONE_DAY_MS, 10)
        conv = _make_converter(df, "1w")
        assert conv._detect_timeframe(df) == "1d"

    def test_detects_4h(self):
        df = _make_candles(ANCHOR, FOUR_HOUR_MS, 10)
        conv = _make_converter(df, "1d")
        assert conv._detect_timeframe(df) == "4h"

    def test_detects_15m(self):
        df = _make_candles(ANCHOR, TIMEFRAME_MS["15m"], 10)
        conv = _make_converter(df, "1h")
        assert conv._detect_timeframe(df) == "15m"

    def test_detects_from_small_sample(self):
        # Only 2 candles — minimum required
        df = _make_candles(ANCHOR, ONE_HOUR_MS, 2)
        conv = _make_converter(df, "4h")
        assert conv._detect_timeframe(df) == "1h"

    def test_raises_on_single_candle(self):
        df = _make_candles(ANCHOR, ONE_HOUR_MS, 1)
        conv = _make_converter(df, "4h")
        with pytest.raises(ValueError, match="at least 2 candles"):
            conv._detect_timeframe(df)

    def test_raises_on_unknown_gap(self):
        # 3-hour gap — not a supported timeframe
        THREE_HOUR_MS = 3 * 3600 * 1000
        df = _make_candles(ANCHOR, THREE_HOUR_MS, 5)
        conv = _make_converter(df, "1d")
        with pytest.raises(ValueError, match="does not match any supported timeframe"):
            conv._detect_timeframe(df)

    def test_uses_most_common_gap_when_first_gap_is_odd(self):
        """
        First gap might be odd (e.g. missing candle at the very start).
        Detection should use the most common gap, not only the first.
        """
        df = _make_candles(ANCHOR, ONE_HOUR_MS, 10)
        # Corrupt only the very first gap by moving candle[1] forward by 2h
        df.at[1, "timestamp"] = ANCHOR + 2 * ONE_HOUR_MS
        conv = _make_converter(df, "4h")
        # Most common gap is still 1h → should detect "1h"
        assert conv._detect_timeframe(df) == "1h"


# ===========================================================================
# Group 2 — _validate_conversion
# ===========================================================================

class TestValidateConversion:

    # ---- Valid conversions ------------------------------------------------

    def test_valid_1h_to_4h(self):
        conv = _make_converter(_make_candles(ANCHOR, ONE_HOUR_MS, 4), "4h")
        conv._validate_conversion("1h", "4h")   # must not raise

    def test_valid_1h_to_1d(self):
        conv = _make_converter(_make_candles(ANCHOR, ONE_HOUR_MS, 24), "1d")
        conv._validate_conversion("1h", "1d")

    def test_valid_15m_to_1h(self):
        conv = _make_converter(_make_candles(ANCHOR, TIMEFRAME_MS["15m"], 4), "1h")
        conv._validate_conversion("15m", "1h")

    def test_valid_1m_to_5m(self):
        conv = _make_converter(_make_candles(ANCHOR, TIMEFRAME_MS["1m"], 5), "5m")
        conv._validate_conversion("1m", "5m")

    def test_valid_1d_to_1w(self):
        conv = _make_converter(_make_candles(ANCHOR, ONE_DAY_MS, 7), "1w")
        conv._validate_conversion("1d", "1w")

    def test_valid_any_to_monthly(self):
        # Any fixed TF → 1M is always valid
        for tf in ["1h", "4h", "1d", "1w"]:
            conv = _make_converter(_make_candles(ANCHOR, TIMEFRAME_MS.get(tf, ONE_HOUR_MS), 5), "1M")
            conv._validate_conversion(tf, "1M")   # must not raise

    def test_valid_5m_to_1h(self):
        # 60 / 5 = 12 — whole multiple
        conv = _make_converter(_make_candles(ANCHOR, TIMEFRAME_MS["5m"], 12), "1h")
        conv._validate_conversion("5m", "1h")

    # ---- Invalid conversions ----------------------------------------------

    def test_invalid_same_timeframe(self):
        conv = _make_converter(_make_candles(ANCHOR, ONE_HOUR_MS, 4), "1h")
        with pytest.raises(ValueError, match="both '1h'"):
            conv._validate_conversion("1h", "1h")

    def test_invalid_going_down_4h_to_1h(self):
        conv = _make_converter(_make_candles(ANCHOR, FOUR_HOUR_MS, 4), "1h")
        with pytest.raises(ValueError, match="must be larger than the source"):
            conv._validate_conversion("4h", "1h")

    def test_invalid_going_down_1d_to_1h(self):
        conv = _make_converter(_make_candles(ANCHOR, ONE_DAY_MS, 4), "1h")
        with pytest.raises(ValueError, match="must be larger than the source"):
            conv._validate_conversion("1d", "1h")

    def test_invalid_not_a_whole_multiple_4h_to_6h(self):
        # 6h / 4h = 1.5 — not a whole number
        conv = _make_converter(_make_candles(ANCHOR, FOUR_HOUR_MS, 4), "6h")
        with pytest.raises(ValueError, match="not a whole multiple"):
            conv._validate_conversion("4h", "6h")

    def test_invalid_not_a_whole_multiple_3h_not_supported(self):
        # "3h" is not a supported timeframe at all
        df = _make_candles(ANCHOR, ONE_HOUR_MS, 10)
        with pytest.raises(ValueError):
            Converter(source=df, target_timeframe="3h")   # normalize raises

    def test_invalid_from_monthly(self):
        conv = _make_converter(_make_candles(ANCHOR, ONE_DAY_MS, 5), "1d")
        with pytest.raises(ValueError, match="no supported timeframe above 1M"):
            conv._validate_conversion("1M", "1d")

    def test_error_message_includes_ratio(self):
        conv = _make_converter(_make_candles(ANCHOR, FOUR_HOUR_MS, 4), "6h")
        with pytest.raises(ValueError, match="1.50"):
            conv._validate_conversion("4h", "6h")


# ===========================================================================
# Group 3 — _resample (aggregation correctness)
# ===========================================================================

class TestResampleAggregation:
    """
    Use a hand-crafted 4-candle DataFrame (1h each) that produces
    exactly one 4h candle. Every assertion checks one aggregation rule.
    """

    @pytest.fixture
    def four_hourly_candles(self) -> pd.DataFrame:
        """Four 1h candles with distinct, easy-to-verify values."""
        return pd.DataFrame({
            "timestamp":       [ANCHOR + i * ONE_HOUR_MS for i in range(4)],
            "open":            [100, 105, 108, 115],
            "high":            [110, 115, 120, 125],
            "low":             [90,   95, 100, 105],
            "close":           [105, 108, 115, 112],
            "volume":          [1000, 1200, 800, 900],
            "close_time":      [ANCHOR + i * ONE_HOUR_MS + ONE_HOUR_MS - 1 for i in range(4)],
            "quote_volume":    [100_000, 120_000, 80_000, 90_000],
            "trades":          [50, 60, 40, 45],
            "taker_buy_base":  [500, 600, 400, 450],
            "taker_buy_quote": [50_000, 60_000, 40_000, 45_000],
        })

    @pytest.fixture
    def result(self, four_hourly_candles) -> pd.Series:
        """Run the resample and return the single output row."""
        conv = _make_converter(four_hourly_candles, "4h")
        out  = conv._resample(four_hourly_candles, "1h", "4h")
        assert len(out) == 1, f"Expected 1 candle, got {len(out)}"
        return out.iloc[0]

    def test_open_is_first(self, result):
        assert result["open"] == 100

    def test_high_is_max(self, result):
        assert result["high"] == 125

    def test_low_is_min(self, result):
        assert result["low"] == 90

    def test_close_is_last(self, result):
        assert result["close"] == 112

    def test_volume_is_sum(self, result):
        assert result["volume"] == 1000 + 1200 + 800 + 900  # 3900

    def test_quote_volume_is_sum(self, result):
        assert result["quote_volume"] == 100_000 + 120_000 + 80_000 + 90_000

    def test_trades_is_sum(self, result):
        assert result["trades"] == 50 + 60 + 40 + 45  # 195

    def test_taker_buy_base_is_sum(self, result):
        assert result["taker_buy_base"] == 500 + 600 + 400 + 450  # 1950

    def test_taker_buy_quote_is_sum(self, result):
        assert result["taker_buy_quote"] == 50_000 + 60_000 + 40_000 + 45_000

    def test_timestamp_is_group_open_time(self, result):
        # The output timestamp must equal the first source candle's open time
        assert result["timestamp"] == ANCHOR

    def test_multiple_groups_correct_count(self):
        # 24 one-hour candles → 6 four-hour candles
        df   = _make_candles(ANCHOR, ONE_HOUR_MS, 24)
        conv = _make_converter(df, "4h")
        out  = conv._resample(df, "1h", "4h")
        assert len(out) == 6

    def test_daily_from_hourly(self):
        # 24 one-hour candles → 1 daily candle
        df   = _make_candles(ANCHOR, ONE_HOUR_MS, 24)
        conv = _make_converter(df, "1d")
        out  = conv._resample(df, "1h", "1d")
        assert len(out) == 1
        assert out.iloc[0]["volume"] == 1000 * 24


# ===========================================================================
# Group 4 — _drop_incomplete
# ===========================================================================

class TestDropIncomplete:

    def test_complete_tail_is_kept(self):
        # 8 one-hour candles → exactly 2 four-hour candles (both complete)
        df   = _make_candles(ANCHOR, ONE_HOUR_MS, 8)
        conv = _make_converter(df, "4h")
        out  = conv._resample(df, "1h", "4h")
        assert len(out) == 2

    def test_incomplete_tail_is_dropped(self):
        # 5 one-hour candles → 1 complete 4h + 1 partial (1 candle)
        # Partial group must be dropped → only 1 output candle
        df   = _make_candles(ANCHOR, ONE_HOUR_MS, 5)
        conv = _make_converter(df, "4h")
        out  = conv._resample(df, "1h", "4h")
        assert len(out) == 1

    def test_incomplete_head_is_dropped(self):
        # Start 2 hours into a 4h window → first group has only 2 candles
        # 2h offset so ANCHOR+2h is the start (inside the 00:00–04:00 window)
        offset_start = ANCHOR + 2 * ONE_HOUR_MS
        df = _make_candles(offset_start, ONE_HOUR_MS, 6)
        # Groups: [02:00–04:00] = 2 candles (incomplete), [04:00–08:00] = 4 candles
        conv = _make_converter(df, "4h")
        out  = conv._resample(df, "1h", "4h")
        assert len(out) == 1
        # The kept candle must start at 04:00, not 02:00
        assert out.iloc[0]["timestamp"] == ANCHOR + 4 * ONE_HOUR_MS

    def test_all_complete_no_drops(self):
        # 7 daily candles → exactly 1 weekly candle (complete)
        df   = _make_candles(ANCHOR, ONE_DAY_MS, 7)
        conv = _make_converter(df, "1w")
        out  = conv._resample(df, "1d", "1w")
        assert len(out) == 1

    def test_raises_when_no_complete_group(self):
        # Only 2 one-hour candles trying to make a 4h candle — impossible
        df   = _make_candles(ANCHOR, ONE_HOUR_MS, 2)
        conv = _make_converter(df, "4h")
        with pytest.raises(ValueError, match="0 complete candles"):
            conv.convert()


# ===========================================================================
# Group 5 — _resolve_filepath
# ===========================================================================

class TestResolveFilepath:

    def test_auto_name_with_symbol(self):
        df   = _make_candles(ANCHOR, ONE_HOUR_MS, 8)
        conv = _make_converter(df, "4h")
        # Simulate that the source_name came from a Collector file
        conv._source_input = "data/binance-futures_BTCUSDT_1h.csv"
        fp = conv._resolve_filepath("1h", "4h", "binance-futures_BTCUSDT_1h.csv")
        assert "BTCUSDT" in fp
        assert "1h_to_4h" in fp
        assert fp.endswith(".csv")

    def test_auto_name_without_symbol(self):
        df   = _make_candles(ANCHOR, ONE_HOUR_MS, 8)
        conv = _make_converter(df, "4h")
        fp   = conv._resolve_filepath("1h", "4h", "<DataFrame>")
        assert "1h_to_4h" in fp
        assert fp.endswith(".csv")

    def test_custom_outputname(self, tmp_path):
        df   = _make_candles(ANCHOR, ONE_HOUR_MS, 8)
        conv = _make_converter(df, "4h")
        conv.destination = str(tmp_path)
        conv.outputname  = "my_output.csv"
        fp = conv._resolve_filepath("1h", "4h", "<DataFrame>")
        assert fp.endswith("my_output.csv")

    def test_outputname_csv_extension_added(self, tmp_path):
        df   = _make_candles(ANCHOR, ONE_HOUR_MS, 8)
        conv = _make_converter(df, "4h")
        conv.destination = str(tmp_path)
        conv.outputname  = "no_extension"
        fp = conv._resolve_filepath("1h", "4h", "<DataFrame>")
        assert fp.endswith(".csv")

    def test_destination_is_used(self, tmp_path):
        df   = _make_candles(ANCHOR, ONE_HOUR_MS, 8)
        conv = _make_converter(df, "4h")
        conv.destination = str(tmp_path / "output")
        fp = conv._resolve_filepath("1h", "4h", "<DataFrame>")
        assert str(tmp_path / "output") in fp

    def test_returns_absolute_path(self):
        df   = _make_candles(ANCHOR, ONE_HOUR_MS, 8)
        conv = _make_converter(df, "4h")
        fp   = conv._resolve_filepath("1h", "4h", "<DataFrame>")
        assert os.path.isabs(fp)


# ===========================================================================
# Group 6 — _load_source
# ===========================================================================

class TestLoadSource:

    def test_load_from_dataframe(self):
        df   = _make_candles(ANCHOR, ONE_HOUR_MS, 10)
        conv = _make_converter(df, "4h")
        loaded_df, name = conv._load_source()
        assert len(loaded_df) == 10
        assert name == "<DataFrame>"

    def test_load_from_csv(self, tmp_path):
        df   = _make_candles(ANCHOR, ONE_HOUR_MS, 10)
        path = str(tmp_path / "test_input.csv")
        df.to_csv(path, index=False)
        conv = _make_converter(path, "4h")
        loaded_df, name = conv._load_source()
        assert len(loaded_df) == 10
        assert name == "test_input.csv"

    def test_csv_is_sorted_on_load(self, tmp_path):
        # Write candles in reverse order
        df   = _make_candles(ANCHOR, ONE_HOUR_MS, 5).iloc[::-1].reset_index(drop=True)
        path = str(tmp_path / "reversed.csv")
        df.to_csv(path, index=False)
        conv = _make_converter(path, "4h")
        loaded_df, _ = conv._load_source()
        assert list(loaded_df["timestamp"]) == sorted(loaded_df["timestamp"])

    def test_raises_on_missing_file(self):
        conv = _make_converter("/nonexistent/path/data.csv", "4h")
        with pytest.raises(FileNotFoundError, match="not found"):
            conv._load_source()

    def test_raises_on_empty_dataframe(self):
        empty = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])
        conv  = _make_converter(empty, "4h")
        with pytest.raises(ValueError, match="empty"):
            conv._load_source()

    def test_raises_on_missing_timestamp_column(self):
        bad_df = pd.DataFrame({"price": [100, 200]})
        conv   = _make_converter(bad_df, "4h")
        with pytest.raises(ValueError, match="'timestamp' column"):
            conv._load_source()

    def test_raises_on_wrong_type(self):
        conv = Converter.__new__(Converter)
        conv._source_input   = 12345   # invalid type
        conv._target_tf      = "4h"
        conv._source_tf_hint = None
        with pytest.raises(TypeError, match="file path"):
            conv._load_source()

    def test_raises_when_source_tf_hint_mismatches(self):
        df   = _make_candles(ANCHOR, ONE_HOUR_MS, 8)  # 1h data
        # User wrongly claims it is 4h
        conv = Converter(source=df, target_timeframe="1d", source_timeframe="4h")
        conv.destination = "."
        with pytest.raises(ValueError, match="does not match the detected"):
            conv.convert()


# ===========================================================================
# Group 7 — convert() end-to-end integration
# ===========================================================================

class TestConvertIntegration:

    def test_convert_dataframe_to_csv(self, tmp_path):
        # 24 hourly candles → 6 four-hour candles
        df   = _make_candles(ANCHOR, ONE_HOUR_MS, 24)
        conv = Converter(source=df, target_timeframe="4h")
        conv.destination = str(tmp_path)
        path = conv.convert()

        assert os.path.isfile(path)
        result = pd.read_csv(path)
        assert len(result) == 6

    def test_convert_csv_to_csv(self, tmp_path):
        df       = _make_candles(ANCHOR, ONE_HOUR_MS, 24)
        src_path = str(tmp_path / "source.csv")
        df.to_csv(src_path, index=False)

        conv = Converter(source=src_path, target_timeframe="4h")
        conv.destination = str(tmp_path)
        path = conv.convert()

        assert os.path.isfile(path)
        result = pd.read_csv(path)
        assert len(result) == 6

    def test_output_file_has_correct_columns(self, tmp_path):
        df   = _make_candles(ANCHOR, ONE_HOUR_MS, 24)
        conv = Converter(source=df, target_timeframe="4h")
        conv.destination = str(tmp_path)
        path = conv.convert()

        result  = pd.read_csv(path)
        required = {"timestamp", "open", "high", "low", "close", "volume"}
        assert required.issubset(set(result.columns))

    def test_output_timestamps_are_ascending(self, tmp_path):
        df   = _make_candles(ANCHOR, ONE_HOUR_MS, 24)
        conv = Converter(source=df, target_timeframe="4h")
        conv.destination = str(tmp_path)
        path = conv.convert()

        result = pd.read_csv(path)
        ts     = list(result["timestamp"])
        assert ts == sorted(ts)

    def test_returns_output_filepath(self, tmp_path):
        df   = _make_candles(ANCHOR, ONE_HOUR_MS, 24)
        conv = Converter(source=df, target_timeframe="4h")
        conv.destination = str(tmp_path)
        path = conv.convert()
        assert isinstance(path, str)
        assert path.endswith(".csv")

    def test_destination_directory_is_created(self, tmp_path):
        new_dir = str(tmp_path / "nested" / "output")
        df      = _make_candles(ANCHOR, ONE_HOUR_MS, 24)
        conv    = Converter(source=df, target_timeframe="4h")
        conv.destination = new_dir
        path = conv.convert()
        assert os.path.isdir(new_dir)
        assert os.path.isfile(path)

    def test_custom_outputname_is_used(self, tmp_path):
        df   = _make_candles(ANCHOR, ONE_HOUR_MS, 24)
        conv = Converter(source=df, target_timeframe="4h")
        conv.destination = str(tmp_path)
        conv.outputname  = "my_4h_data.csv"
        path = conv.convert()
        assert path.endswith("my_4h_data.csv")

    def test_volume_totals_are_preserved(self, tmp_path):
        # Total volume across all output candles must equal total input volume
        df   = _make_candles(ANCHOR, ONE_HOUR_MS, 24, volume_=1000.0)
        conv = Converter(source=df, target_timeframe="4h")
        conv.destination = str(tmp_path)
        path = conv.convert()

        result       = pd.read_csv(path)
        total_in     = 1000.0 * 24
        total_out    = result["volume"].sum()
        assert abs(total_out - total_in) < 1e-6

    def test_open_of_first_output_equals_open_of_first_input(self, tmp_path):
        df   = _make_candles(ANCHOR, ONE_HOUR_MS, 24, open_=200.0)
        conv = Converter(source=df, target_timeframe="4h")
        conv.destination = str(tmp_path)
        path = conv.convert()

        result = pd.read_csv(path)
        assert result.iloc[0]["open"] == 200.0

    def test_high_of_output_never_less_than_any_input_high(self, tmp_path):
        df   = _make_candles(ANCHOR, ONE_HOUR_MS, 4, high_=110.0)
        # Make one candle have a higher high
        df.at[2, "high"] = 999.0
        conv = Converter(source=df, target_timeframe="4h")
        conv.destination = str(tmp_path)
        path = conv.convert()

        result = pd.read_csv(path)
        assert result.iloc[0]["high"] == 999.0

    def test_repr(self):
        df   = _make_candles(ANCHOR, ONE_HOUR_MS, 8)
        conv = Converter(source=df, target_timeframe="4h")
        r    = repr(conv)
        assert "Converter" in r
        assert "4h"        in r
