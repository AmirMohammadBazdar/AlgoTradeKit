from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import pandas as pd
import os

from .._utils import (
    TIMEFRAME_MS,
    ceil_to_tf,
    floor_to_tf,
    ms_to_dt,
    now_ms,
)

# Canonical column order written to / read from disk
_COLUMNS = [
    "timestamp", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "trades",
    "taker_buy_base", "taker_buy_quote",
]

# A "gap" tuple represents a missing range: (start_ms, end_ms) both inclusive
Gap = tuple[int, int]


class CSVHandler:
    """
    Manages a single CSV file of OHLCV candles.

    All timestamps in the file are stored as integer UTC milliseconds in
    the ``timestamp`` column.  The file is always sorted ascending on
    ``timestamp`` with no duplicates.
    """

    def __init__(self, filepath: str) -> None:
        self.filepath = os.path.abspath(filepath)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def exists(self) -> bool:
        """Return ``True`` if the backing file exists and is non-empty."""
        return os.path.isfile(self.filepath) and os.path.getsize(self.filepath) > 0

    def load(self) -> pd.DataFrame | None:
        """
        Load the CSV.  Returns ``None`` if the file does not exist or is
        empty / malformed.  Always returns data sorted by ``timestamp``.
        """
        if not self.exists():
            return None
        try:
            df = pd.read_csv(self.filepath, dtype={"timestamp": "int64"})
        except Exception:
            return None

        if "timestamp" not in df.columns or df.empty:
            return None

        df = (
            df.sort_values("timestamp")
              .drop_duplicates(subset=["timestamp"])
              .reset_index(drop=True)
        )
        # Ensure all expected columns are present (older files may lack some)
        for col in _COLUMNS:
            if col not in df.columns:
                df[col] = None
        return df[_COLUMNS]

    def save(self, df: pd.DataFrame) -> None:
        """
        Persist *df* to disk.  Sorts by timestamp and removes duplicates
        before writing.  Creates parent directories as needed.
        """
        parent = os.path.dirname(self.filepath)
        if parent:
            os.makedirs(parent, exist_ok=True)

        df = (
            df[_COLUMNS]
              .sort_values("timestamp")
              .drop_duplicates(subset=["timestamp"])
              .reset_index(drop=True)
        )
        df.to_csv(self.filepath, index=False)

    def merge_and_save(
        self,
        existing: pd.DataFrame | None,
        new_candles: list[dict[str, Any]],
    ) -> pd.DataFrame:
        """
        Merge *new_candles* into *existing*, dedup, sort, and write to disk.

        Returns the combined ``DataFrame``.
        """
        new_df = pd.DataFrame(new_candles, columns=_COLUMNS)
        if existing is not None and not existing.empty:
            combined = pd.concat([existing, new_df], ignore_index=True)
        else:
            combined = new_df

        combined["timestamp"] = combined["timestamp"].astype("int64")
        self.save(combined)
        return combined

    # ------------------------------------------------------------------
    # Gap detection
    # ------------------------------------------------------------------

    def find_gaps(
        self,
        df: pd.DataFrame,
        timeframe: str,
        start_ms: int,
        end_ms: int,
    ) -> list[Gap]:
        """
        Return a list of ``(gap_start_ms, gap_end_ms)`` ranges that are
        missing from *df* between *start_ms* and *end_ms*.

        Handles three gap types automatically:
        * **Head** – data exists but starts after ``start_ms``.
        * **Tail** – data exists but ends before ``end_ms``.
        * **Middle** – holes inside the existing data range.

        For monthly candles (``"1M"``) calendar-aware logic is used.
        For all other timeframes the millisecond step is fixed.
        """
        if timeframe == "1M":
            return self._find_monthly_gaps(df, start_ms, end_ms)
        return self._find_fixed_gaps(df, timeframe, start_ms, end_ms)

    # ------------------------------------------------------------------
    # Fixed-length gap detection  (all TFs except 1M)
    # ------------------------------------------------------------------

    def _find_fixed_gaps(
        self,
        df: pd.DataFrame,
        timeframe: str,
        start_ms: int,
        end_ms: int,
    ) -> list[Gap]:
        tf_ms = TIMEFRAME_MS[timeframe]

        # Align start upward and end downward to the grid
        aligned_start = ceil_to_tf(start_ms, tf_ms)
        aligned_end   = floor_to_tf(end_ms,   tf_ms)

        if aligned_start > aligned_end:
            return []

        # Build set of expected timestamps
        expected: set[int] = set(
            range(aligned_start, aligned_end + 1, tf_ms)
        )

        existing: set[int] = set(df["timestamp"].astype("int64").values)
        missing = sorted(expected - existing)

        if not missing:
            return []

        return self._group_into_ranges(missing, tf_ms)

    @staticmethod
    def _group_into_ranges(missing: list[int], step: int) -> list[Gap]:
        """Compress a sorted list of missing timestamps into (start, end) ranges."""
        gaps: list[Gap] = []
        range_start = missing[0]
        range_end   = missing[0]

        for ts in missing[1:]:
            if ts - range_end == step:
                range_end = ts
            else:
                gaps.append((range_start, range_end))
                range_start = ts
                range_end   = ts

        gaps.append((range_start, range_end))
        return gaps

    # ------------------------------------------------------------------
    # Monthly gap detection (calendar-aware)
    # ------------------------------------------------------------------

    def _find_monthly_gaps(
        self,
        df: pd.DataFrame,
        start_ms: int,
        end_ms: int,
    ) -> list[Gap]:
        """
        Compare which calendar months (UTC) are present in *df* against
        the months that should exist in [start_ms, end_ms].
        """
        existing_ts: set[int] = set(df["timestamp"].astype("int64").values)

        # Generate month-start timestamps for each month in the range
        expected_months: list[int] = self._month_starts(start_ms, end_ms)
        missing = sorted(set(expected_months) - existing_ts)

        if not missing:
            return []

        # Group consecutive months into ranges
        gaps: list[Gap] = []
        range_start = missing[0]
        prev         = missing[0]

        for ts in missing[1:]:
            # Check if ts is the very next month after prev
            next_month_ts = self._advance_month(prev)
            if ts == next_month_ts:
                prev = ts
            else:
                gaps.append((range_start, self._advance_month(prev) - 1))
                range_start = ts
                prev = ts

        gaps.append((range_start, self._advance_month(prev) - 1))
        return gaps

    @staticmethod
    def _month_starts(start_ms: int, end_ms: int) -> list[int]:
        """Return UTC millisecond timestamps for the 1st of each month in the range."""
        dt = ms_to_dt(start_ms).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end_dt = ms_to_dt(end_ms)
        result: list[int] = []
        while dt <= end_dt:
            result.append(int(dt.timestamp() * 1000))
            year, month = dt.year, dt.month
            if month == 12:
                year += 1
                month = 1
            else:
                month += 1
            dt = dt.replace(year=year, month=month)
        return result

    @staticmethod
    def _advance_month(ts_ms: int) -> int:
        """Return the timestamp for the start of the next calendar month."""
        dt = ms_to_dt(ts_ms)
        year, month = dt.year, dt.month
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
        return int(
            datetime(year, month, 1, tzinfo=timezone.utc).timestamp() * 1000
        )

    # ------------------------------------------------------------------
    # Repr
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return f"<CSVHandler path={self.filepath!r}>"
