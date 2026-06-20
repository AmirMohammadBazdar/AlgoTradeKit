"""
AlgoTradeKit.data.normalizer
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Normalizer — convert non-standard OHLCV CSV data to AlgoTradeKit format.

Handles these common non-standard situations
--------------------------------------------
* **Timestamps in seconds** (Unix epoch, e.g. ``1766421600``) — auto-detected
  and converted to milliseconds so all library tools receive a consistent unit.
* **Timestamps in milliseconds** — used as-is.
* **Missing optional columns** (``close_time``, ``quote_volume``, ``trades``,
  ``taker_buy_base``, ``taker_buy_quote``) — filled with ``None``.
* **Missing ``volume`` column** — filled with ``0``.
* **Date-range filtering** — apply ``start`` / ``end`` before returning data
  to reduce memory use in downstream tools.

Accepted CSV format (the user's broker / MT5 export format)
-----------------------------------------------------------
::

    timestamp,open,high,low,close,volume
    1766421600,0.91485,0.91489,0.91479,0.91483,0
    1766421660,0.91485,0.91487,0.91479,0.91482,0

Column names are matched case-insensitively and stripped of whitespace.

Output
------
The normalized DataFrame / CSV uses the same schema as ``Collector`` output:

    timestamp, open, high, low, close, volume,
    close_time, quote_volume, trades, taker_buy_base, taker_buy_quote

All timestamps are **integer UTC milliseconds**.

Examples
--------
::

    # Normalize and get a DataFrame (no file written)
    norm = Normalizer("USDJPY_1m_ohlcv.csv")
    norm.start = "2020/01/01"
    df = norm.normalize()

    # Normalize and save
    norm = Normalizer("USDJPY_1m_ohlcv.csv")
    norm.start = "2020/01/01"
    path = norm.save(destination="data/")

    # Normalize and save in one call
    df, path = Normalizer("raw.csv").normalize_and_save(destination="data/")

    # From an in-memory DataFrame
    norm = Normalizer(some_df)
    norm.start = datetime(2022, 1, 1, tzinfo=timezone.utc)
    df = norm.normalize()
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Union

import pandas as pd

from ._utils import ms_to_dt, parse_to_ms

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

#: Full canonical column order (matches CSVHandler and Collector output).
_STANDARD_COLUMNS: list[str] = [
    "timestamp", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "trades",
    "taker_buy_base", "taker_buy_quote",
]

#: Columns that MUST be present in the source (after case-normalisation).
_REQUIRED_COLUMNS: frozenset[str] = frozenset({"timestamp", "open", "high", "low", "close"})

# Timestamps above this value are in milliseconds; below it — in seconds.
# Threshold ≈ 2001-09-08 UTC as seconds ≈ 1 000 000 000 s
# Any broker timestamp after year 2001 expressed in seconds will be < 10^10.
_MS_THRESHOLD: int = 10_000_000_000


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _print(msg: str) -> None:
    print(f"  [AlgoTradeKit] {msg}")


def _fmt_dt(ms: int) -> str:
    return ms_to_dt(ms).strftime("%Y-%m-%d %H:%M UTC")


# ---------------------------------------------------------------------------
# Normalizer
# ---------------------------------------------------------------------------

class Normalizer:
    """
    Normalize non-standard OHLCV data to the AlgoTradeKit standard format.

    Parameters
    ----------
    source : str | pd.DataFrame
        Path to a CSV file **or** a pandas DataFrame.

        Required source columns: ``timestamp``, ``open``, ``high``, ``low``,
        ``close``.  Column names are matched case-insensitively.

        Optional source columns: ``volume`` (defaults to ``0`` if absent).

    Settable attributes (optional — set before calling normalize / save)
    --------------------------------------------------------------------
    start : str | datetime | int | None
        Earliest candle to keep (inclusive).  Accepts the same formats as
        ``Collector.starttime`` — ``"YYYY/MM/DD"``, ``"YYYY-MM-DD"``,
        ``datetime``, or an integer millisecond timestamp.
        ``None`` means no lower bound.
    end : str | datetime | int | None
        Latest candle to keep (inclusive).  ``None`` means no upper bound.
    destination : str
        Folder where the output CSV will be saved.  Default ``"./"``.
    outputname : str | None
        Custom CSV filename (without or with ``.csv`` extension).
        Auto-generated from the source filename when ``None``.

    Examples
    --------
    ::

        # Return a normalized DataFrame — no file written
        norm = Normalizer("USDJPY_1m_ohlcv.csv")
        norm.start = "2020/01/01"
        df = norm.normalize()

        # Save the normalized CSV to disk
        norm = Normalizer("USDJPY_1m_ohlcv.csv")
        norm.start = "2020/01/01"
        path = norm.save(destination="data/")

        # One-liner: normalize + save
        df, path = Normalizer("raw.csv").normalize_and_save(destination="data/")

        # From an in-memory DataFrame
        norm = Normalizer(df)
        norm.start = datetime(2022, 1, 1, tzinfo=timezone.utc)
        norm.end   = datetime(2023, 1, 1, tzinfo=timezone.utc)
        df_norm = norm.normalize()
    """

    def __init__(self, source: "str | pd.DataFrame") -> None:
        self._source = source

        # Optional date-range filters
        self.start: "Any | None" = None
        self.end: "Any | None" = None

        # Output settings
        self.destination: str = "./"
        self.outputname: "str | None" = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def normalize(self) -> pd.DataFrame:
        """
        Normalize the source data and return a DataFrame in library format.

        The returned DataFrame:

        * ``timestamp`` column in **UTC milliseconds** (``int64``)
        * All 11 standard columns (optional ones filled with ``None``)
        * Filtered to ``[start, end]`` if those attributes are set
        * Sorted ascending by ``timestamp``, duplicate timestamps removed

        Returns
        -------
        pd.DataFrame

        Raises
        ------
        FileNotFoundError
            Source path does not exist.
        ValueError
            Source is empty, missing required columns, or the date filter
            leaves no rows.
        TypeError
            Source is neither a string nor a DataFrame.
        """
        df, source_name = self._load()
        df = self._normalize_column_names(df, source_name)
        df = self._validate_required(df, source_name)
        df = self._convert_timestamps(df)
        df = self._apply_date_filter(df)
        df = self._fill_optional_columns(df)
        df = self._finalize(df)

        if df.empty:
            raise ValueError(
                "After applying the date filter, no candles remain. "
                "Check your start / end range."
            )

        self._print_summary(df, source_name)
        return df

    def save(
        self,
        destination: "str | None" = None,
        outputname: "str | None" = None,
    ) -> str:
        """
        Normalize the data and save it as a CSV file.

        Parameters
        ----------
        destination : str | None
            Override ``self.destination`` for this call only.
        outputname : str | None
            Override ``self.outputname`` for this call only.

        Returns
        -------
        str
            Absolute path of the saved CSV file.
        """
        if destination is not None:
            self.destination = destination
        if outputname is not None:
            self.outputname = outputname

        df = self.normalize()
        path = self._resolve_filepath()
        _make_dirs(path)
        df.to_csv(path, index=False)
        _print(f"✓ Saved  {len(df):,} candles  →  {path}")
        return path

    def normalize_and_save(
        self,
        destination: "str | None" = None,
        outputname: "str | None" = None,
    ) -> "tuple[pd.DataFrame, str]":
        """
        Normalize **and** save in one call.

        Returns
        -------
        tuple[pd.DataFrame, str]
            ``(normalized_dataframe, absolute_path_of_saved_csv)``
        """
        if destination is not None:
            self.destination = destination
        if outputname is not None:
            self.outputname = outputname

        df = self.normalize()
        path = self._resolve_filepath()
        _make_dirs(path)
        df.to_csv(path, index=False)
        _print(f"✓ Saved  {len(df):,} candles  →  {path}")
        return df, path

    # ------------------------------------------------------------------
    # Internal — loading
    # ------------------------------------------------------------------

    def _load(self) -> "tuple[pd.DataFrame, str]":
        source = self._source

        if isinstance(source, str):
            path = os.path.abspath(source)
            if not os.path.isfile(path):
                raise FileNotFoundError(f"Source file not found: '{path}'")
            try:
                df = pd.read_csv(path)
            except Exception as exc:
                raise ValueError(
                    f"Could not read CSV file '{path}': {exc}"
                ) from exc
            if df.empty:
                raise ValueError(f"Source file is empty: '{path}'")
            return df, os.path.basename(path)

        elif isinstance(source, pd.DataFrame):
            if source.empty:
                raise ValueError("Source DataFrame is empty.")
            return source.copy(), "<DataFrame>"

        else:
            raise TypeError(
                f"source must be a file path (str) or a pandas DataFrame, "
                f"got {type(source).__name__}."
            )

    # ------------------------------------------------------------------
    # Internal — transformation steps
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_column_names(df: pd.DataFrame, source_name: str) -> pd.DataFrame:
        """Strip whitespace and lowercase all column names."""
        df = df.copy()
        df.columns = [c.strip().lower() for c in df.columns]
        return df

    @staticmethod
    def _validate_required(df: pd.DataFrame, source_name: str) -> pd.DataFrame:
        """Raise if any required column is absent."""
        missing = _REQUIRED_COLUMNS - set(df.columns)
        if missing:
            raise ValueError(
                f"Source '{source_name}' is missing required columns: "
                f"{sorted(missing)}. "
                f"Found columns: {sorted(df.columns)}"
            )
        return df

    @staticmethod
    def _convert_timestamps(df: pd.DataFrame) -> pd.DataFrame:
        """
        Detect whether timestamps are in seconds or milliseconds and convert
        all values to integer milliseconds.

        Detection rule
        --------------
        If the **maximum** timestamp value is below ``_MS_THRESHOLD`` (10^10),
        the column is in seconds and every value is multiplied by 1 000.
        Otherwise the column is treated as already being in milliseconds.

        This covers all real-world broker data after year 2001:
        - Unix seconds in 2024 ≈ 1.7 × 10^9  (< 10^10) → seconds
        - Unix ms in 2024      ≈ 1.7 × 10^12 (> 10^10) → milliseconds
        """
        df = df.copy()

        # Drop rows where timestamp is missing or non-numeric
        df = df.dropna(subset=["timestamp"]).copy()
        df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce")
        df = df.dropna(subset=["timestamp"])

        max_ts = float(df["timestamp"].max())

        if max_ts < _MS_THRESHOLD:
            # Seconds → milliseconds
            df["timestamp"] = (df["timestamp"].astype("float64") * 1_000).astype("int64")
        else:
            # Already milliseconds
            df["timestamp"] = df["timestamp"].astype("int64")

        return df

    def _apply_date_filter(self, df: pd.DataFrame) -> pd.DataFrame:
        """Filter rows to the [start, end] date range (inclusive).

        ``start`` / ``end`` accept:
        - String  → parsed as UTC date/datetime via ``parse_to_ms``
        - datetime → converted to UTC ms via ``parse_to_ms``
        - int < 10^10 → treated as **seconds** and converted to ms (auto-detect)
        - int ≥ 10^10 → treated as **milliseconds** (passed to ``parse_to_ms``)
        """
        if self.start is None and self.end is None:
            return df

        df = df.copy()

        if self.start is not None:
            start_ms = _normalizer_to_ms(self.start)
            df = df[df["timestamp"] >= start_ms]

        if self.end is not None:
            end_ms = _normalizer_to_ms(self.end)
            df = df[df["timestamp"] <= end_ms]

        return df

    @staticmethod
    def _fill_optional_columns(df: pd.DataFrame) -> pd.DataFrame:
        """Add optional standard columns with ``None`` if absent."""
        df = df.copy()

        # volume defaults to 0 if missing (some forex data has no volume)
        if "volume" not in df.columns:
            df["volume"] = 0.0

        # All other optional columns → None
        for col in _STANDARD_COLUMNS:
            if col not in df.columns:
                df[col] = None

        return df

    @staticmethod
    def _finalize(df: pd.DataFrame) -> pd.DataFrame:
        """Reorder, sort by timestamp, drop duplicates, reset index."""
        df = (
            df[_STANDARD_COLUMNS]
            .sort_values("timestamp")
            .drop_duplicates(subset=["timestamp"])
            .reset_index(drop=True)
        )
        df["timestamp"] = df["timestamp"].astype("int64")
        return df

    # ------------------------------------------------------------------
    # Internal — output path
    # ------------------------------------------------------------------

    def _resolve_filepath(self) -> str:
        if self.outputname:
            name = self.outputname
            if not name.lower().endswith(".csv"):
                name += ".csv"
        else:
            if isinstance(self._source, str):
                stem = os.path.splitext(os.path.basename(self._source))[0]
                name = f"normalized_{stem}.csv"
            else:
                name = "normalized_data.csv"

        dest = self.destination or "./"
        return os.path.abspath(os.path.join(dest, name))

    # ------------------------------------------------------------------
    # Internal — diagnostics
    # ------------------------------------------------------------------

    def _print_summary(self, df: pd.DataFrame, source_name: str) -> None:
        first_ts = int(df["timestamp"].iloc[0])
        last_ts = int(df["timestamp"].iloc[-1])
        print()
        _print("─" * 56)
        _print(f"Normalizer")
        _print(f"Source     : {source_name}")
        _print(f"Candles    : {len(df):,}")
        _print(f"Range      : {_fmt_dt(first_ts)}  →  {_fmt_dt(last_ts)}")
        _print("─" * 56)

    # ------------------------------------------------------------------
    # Repr
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        src = (
            os.path.basename(self._source)
            if isinstance(self._source, str)
            else "<DataFrame>"
        )
        return f"<Normalizer source={src!r}>"


# ---------------------------------------------------------------------------
# Private utility
# ---------------------------------------------------------------------------

def _normalizer_to_ms(value) -> int:
    """
    Convert a ``start`` / ``end`` filter value to UTC **milliseconds**.

    Integers are auto-detected:
    - ``< 10^10``  → treated as Unix **seconds** (typical broker timestamps)
    - ``≥ 10^10``  → treated as Unix **milliseconds** (library standard)

    All other types (str, datetime) are forwarded to ``parse_to_ms``.
    """
    if isinstance(value, int):
        if value < _MS_THRESHOLD:
            return value * 1_000   # seconds → ms
        return value               # already ms
    return parse_to_ms(value)


def _make_dirs(path: str) -> None:
    """Create parent directories of *path* if they do not exist."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
