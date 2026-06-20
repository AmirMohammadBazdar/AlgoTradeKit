"""
tests/test_data_normalizer.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Unit tests for AlgoTradeKit.data.Normalizer (v0.7.2).

Tests cover:
- Timestamp detection (seconds vs milliseconds)
- Date-range filtering (start / end)
- Missing optional columns are filled with None
- Missing volume defaults to 0
- normalize() returns correct schema
- save() / normalize_and_save() write CSV files
- Error handling (missing file, missing columns, empty after filter)
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone

import pandas as pd
import pytest

from AlgoTradeKit.data.normalizer import Normalizer

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STANDARD_COLUMNS = [
    "timestamp", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "trades",
    "taker_buy_base", "taker_buy_quote",
]

# Typical broker CSV: timestamps in SECONDS
_SECONDS_CSV_CONTENT = """\
timestamp,open,high,low,close,volume
1700000000,100.0,101.0,99.0,100.5,50
1700000060,100.5,102.0,100.0,101.0,80
1700000120,101.0,103.0,100.5,102.0,60
1700000180,102.0,104.0,101.5,103.0,40
1700000240,103.0,105.0,102.5,104.0,90
"""

# AlgoTradeKit CSV: timestamps in MILLISECONDS
_MS_CSV_CONTENT = """\
timestamp,open,high,low,close,volume
1700000000000,100.0,101.0,99.0,100.5,50
1700000060000,100.5,102.0,100.0,101.0,80
1700000120000,101.0,103.0,100.5,102.0,60
"""

# CSV without volume column
_NO_VOLUME_CSV_CONTENT = """\
timestamp,open,high,low,close
1700000000,100.0,101.0,99.0,100.5
1700000060,100.5,102.0,100.0,101.0
"""

# CSV with ALL optional Collector columns
_FULL_CSV_CONTENT = """\
timestamp,open,high,low,close,volume,close_time,quote_volume,trades,taker_buy_base,taker_buy_quote
1700000000000,100.0,101.0,99.0,100.5,50,1700000059999,5000.0,25,25.0,2500.0
1700000060000,100.5,102.0,100.0,101.0,80,1700000119999,8080.0,40,40.0,4040.0
"""


def _write_tmp_csv(content: str) -> str:
    """Write *content* to a temp file and return the path."""
    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    return path


def _make_df(content: str) -> pd.DataFrame:
    """Parse a CSV content string into a DataFrame."""
    import io
    return pd.read_csv(io.StringIO(content))


# ---------------------------------------------------------------------------
# Tests — basic normalisation
# ---------------------------------------------------------------------------

class TestNormalizerSecondTimestamps:
    """CSV with Unix-second timestamps → should be converted to ms."""

    def test_timestamp_converted_to_ms(self):
        path = _write_tmp_csv(_SECONDS_CSV_CONTENT)
        try:
            df = Normalizer(path).normalize()
            # All timestamps should now be in milliseconds
            assert df["timestamp"].min() == 1700000000 * 1000
        finally:
            os.unlink(path)

    def test_output_columns_complete(self):
        path = _write_tmp_csv(_SECONDS_CSV_CONTENT)
        try:
            df = Normalizer(path).normalize()
            assert list(df.columns) == _STANDARD_COLUMNS
        finally:
            os.unlink(path)

    def test_row_count_unchanged(self):
        path = _write_tmp_csv(_SECONDS_CSV_CONTENT)
        try:
            df = Normalizer(path).normalize()
            assert len(df) == 5
        finally:
            os.unlink(path)

    def test_ohlcv_values_preserved(self):
        path = _write_tmp_csv(_SECONDS_CSV_CONTENT)
        try:
            df = Normalizer(path).normalize()
            assert df["open"].iloc[0] == pytest.approx(100.0)
            assert df["close"].iloc[-1] == pytest.approx(104.0)
        finally:
            os.unlink(path)


class TestNormalizerMillisecondTimestamps:
    """CSV with Unix-millisecond timestamps → should be used as-is."""

    def test_ms_timestamps_unchanged(self):
        path = _write_tmp_csv(_MS_CSV_CONTENT)
        try:
            df = Normalizer(path).normalize()
            assert df["timestamp"].iloc[0] == 1700000000000
        finally:
            os.unlink(path)

    def test_output_schema(self):
        path = _write_tmp_csv(_MS_CSV_CONTENT)
        try:
            df = Normalizer(path).normalize()
            assert list(df.columns) == _STANDARD_COLUMNS
        finally:
            os.unlink(path)


class TestNormalizerMissingColumns:
    """Source has optional columns missing — they should be filled with None/0."""

    def test_optional_columns_filled_with_none(self):
        path = _write_tmp_csv(_SECONDS_CSV_CONTENT)
        try:
            df = Normalizer(path).normalize()
            for col in ["close_time", "quote_volume", "trades",
                        "taker_buy_base", "taker_buy_quote"]:
                # All optional extras must exist but be None/NaN
                assert col in df.columns
                # Values should all be null for minimal CSV
                assert df[col].isna().all(), f"Expected {col} to be null"
        finally:
            os.unlink(path)

    def test_volume_defaults_to_zero_when_absent(self):
        path = _write_tmp_csv(_NO_VOLUME_CSV_CONTENT)
        try:
            df = Normalizer(path).normalize()
            assert "volume" in df.columns
            assert (df["volume"] == 0.0).all()
        finally:
            os.unlink(path)

    def test_full_csv_optional_columns_preserved(self):
        path = _write_tmp_csv(_FULL_CSV_CONTENT)
        try:
            df = Normalizer(path).normalize()
            assert df["trades"].iloc[0] == 25
            assert df["quote_volume"].iloc[1] == pytest.approx(8080.0)
        finally:
            os.unlink(path)


class TestNormalizerFromDataFrame:
    """Normalizer accepts a pandas DataFrame directly."""

    def test_dataframe_input(self):
        raw = _make_df(_SECONDS_CSV_CONTENT)
        df = Normalizer(raw).normalize()
        assert list(df.columns) == _STANDARD_COLUMNS
        assert df["timestamp"].iloc[0] == 1700000000 * 1000

    def test_dataframe_ms_input(self):
        raw = _make_df(_MS_CSV_CONTENT)
        df = Normalizer(raw).normalize()
        assert df["timestamp"].iloc[0] == 1700000000000

    def test_column_names_case_insensitive(self):
        raw = _make_df(_SECONDS_CSV_CONTENT)
        raw.columns = [c.upper() for c in raw.columns]
        df = Normalizer(raw).normalize()
        assert "timestamp" in df.columns
        assert "close" in df.columns


# ---------------------------------------------------------------------------
# Tests — date-range filtering
# ---------------------------------------------------------------------------

class TestNormalizerDateFilter:
    """start / end attributes filter rows before returning."""

    def test_start_filter_string(self):
        # 1700000120 = 2023-11-14 22:15:20 UTC (row index 2 in seconds CSV)
        path = _write_tmp_csv(_SECONDS_CSV_CONTENT)
        try:
            norm = Normalizer(path)
            # Cut off the first two rows by setting start to the 3rd timestamp
            norm.start = 1700000120  # seconds — same unit as data
            df = norm.normalize()
            # start is treated as ms if >= 1e10, so 1700000120 < 1e10 → seconds
            # but normalize() converts timestamps first, then filters by ms
            # 1700000120 seconds < 1e10 → treated as seconds → parsed_ms = 1700000120000
            assert df["timestamp"].iloc[0] == 1700000120 * 1000
            assert len(df) == 3
        finally:
            os.unlink(path)

    def test_end_filter_ms(self):
        path = _write_tmp_csv(_SECONDS_CSV_CONTENT)
        try:
            norm = Normalizer(path)
            # end = 2nd row timestamp in ms
            norm.end = 1700000060 * 1000  # already ms
            df = norm.normalize()
            assert df["timestamp"].iloc[-1] == 1700000060 * 1000
            assert len(df) == 2
        finally:
            os.unlink(path)

    def test_start_end_datetime(self):
        path = _write_tmp_csv(_SECONDS_CSV_CONTENT)
        try:
            norm = Normalizer(path)
            norm.start = datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)  # row 0
            norm.end   = datetime(2023, 11, 14, 22, 15, 20, tzinfo=timezone.utc)  # row 2
            df = norm.normalize()
            assert len(df) == 3
        finally:
            os.unlink(path)

    def test_filter_leaves_no_rows_raises(self):
        path = _write_tmp_csv(_SECONDS_CSV_CONTENT)
        try:
            norm = Normalizer(path)
            norm.start = 9_999_999_999_999  # far future in ms
            with pytest.raises(ValueError, match="no candles remain"):
                norm.normalize()
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# Tests — save / normalize_and_save
# ---------------------------------------------------------------------------

class TestNormalizerSave:
    """Tests for save() and normalize_and_save()."""

    def test_save_creates_file(self, tmp_path):
        path = _write_tmp_csv(_SECONDS_CSV_CONTENT)
        try:
            norm = Normalizer(path)
            saved = norm.save(destination=str(tmp_path))
            assert os.path.isfile(saved)
        finally:
            os.unlink(path)

    def test_saved_file_is_valid_csv(self, tmp_path):
        path = _write_tmp_csv(_SECONDS_CSV_CONTENT)
        try:
            norm = Normalizer(path)
            saved = norm.save(destination=str(tmp_path))
            loaded = pd.read_csv(saved)
            assert list(loaded.columns) == _STANDARD_COLUMNS
            assert len(loaded) == 5
        finally:
            os.unlink(path)

    def test_normalize_and_save_returns_df_and_path(self, tmp_path):
        path = _write_tmp_csv(_SECONDS_CSV_CONTENT)
        try:
            norm = Normalizer(path)
            df, saved = norm.normalize_and_save(destination=str(tmp_path))
            assert isinstance(df, pd.DataFrame)
            assert os.path.isfile(saved)
            assert len(df) == 5
        finally:
            os.unlink(path)

    def test_custom_outputname(self, tmp_path):
        path = _write_tmp_csv(_SECONDS_CSV_CONTENT)
        try:
            norm = Normalizer(path)
            saved = norm.save(destination=str(tmp_path), outputname="my_data.csv")
            assert saved.endswith("my_data.csv")
        finally:
            os.unlink(path)

    def test_auto_outputname_from_source(self, tmp_path):
        path = _write_tmp_csv(_SECONDS_CSV_CONTENT)
        try:
            basename = os.path.splitext(os.path.basename(path))[0]
            norm = Normalizer(path)
            saved = norm.save(destination=str(tmp_path))
            assert f"normalized_{basename}" in os.path.basename(saved)
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# Tests — error handling
# ---------------------------------------------------------------------------

class TestNormalizerErrors:
    """Bad inputs raise the right errors."""

    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            Normalizer("/nonexistent/path/data.csv").normalize()

    def test_missing_required_column(self):
        raw = pd.DataFrame({"timestamp": [1700000000], "open": [1.0]})
        with pytest.raises(ValueError, match="missing required columns"):
            Normalizer(raw).normalize()

    def test_empty_dataframe(self):
        with pytest.raises(ValueError, match="empty"):
            Normalizer(pd.DataFrame()).normalize()

    def test_bad_source_type(self):
        with pytest.raises(TypeError):
            Normalizer(42).normalize()  # type: ignore

    def test_sorted_and_deduped(self):
        """Output should be sorted and free of duplicate timestamps."""
        raw = pd.DataFrame({
            "timestamp": [1700000120, 1700000060, 1700000060, 1700000000],
            "open":  [3.0, 2.0, 2.0, 1.0],
            "high":  [3.5, 2.5, 2.5, 1.5],
            "low":   [2.5, 1.5, 1.5, 0.5],
            "close": [3.0, 2.0, 2.0, 1.0],
        })
        df = Normalizer(raw).normalize()
        # Should be sorted ascending
        assert list(df["timestamp"]) == sorted(df["timestamp"].tolist())
        # No duplicates
        assert df["timestamp"].nunique() == len(df)
