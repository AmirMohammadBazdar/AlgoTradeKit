from __future__ import annotations
from datetime import datetime, timezone
import pandas as pd
import os
import re

from ._utils import (
    TIMEFRAME_MS,
    TIMEFRAME_LABELS,
    VALID_TIMEFRAMES,
    ms_to_dt,
    normalize_timeframe,
    now_ms,
)

# ---------------------------------------------------------------------------
# Pandas resample frequency map
# Converts our normalised timeframe keys to pandas offset aliases
# ---------------------------------------------------------------------------
_PANDAS_FREQ: dict[str, str] = {
    "1m":  "1min",
    "3m":  "3min",
    "5m":  "5min",
    "15m": "15min",
    "30m": "30min",
    "1h":  "1h",
    "2h":  "2h",
    "4h":  "4h",
    "6h":  "6h",
    "8h":  "8h",
    "12h": "12h",
    "1d":  "1D",
    "3d":  "3D",
    "1w":  "1W",
    "1M":  "MS",   # MS = Month Start — calendar-aware
}

# Columns that are summed when aggregating
_SUM_COLS = ["volume", "quote_volume", "trades", "taker_buy_base", "taker_buy_quote"]

# Output column order — must match CSVHandler
_COLUMNS = [
    "timestamp", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "trades",
    "taker_buy_base", "taker_buy_quote",
]

# Pattern to extract symbol from Collector-generated filenames
# e.g. "binance-futures_BTCUSDT_1h.csv" → "BTCUSDT"
_FILENAME_SYMBOL_RE = re.compile(r"^.+?_([A-Z0-9]+)_[^_]+\.csv$", re.IGNORECASE)


def _print(msg: str) -> None:
    print(f"  [AlgoTradeKit] {msg}")


def _fmt_dt(ms: int) -> str:
    return ms_to_dt(ms).strftime("%Y-%m-%d %H:%M UTC")


def _fmt_n(n: int) -> str:
    return f"{n:,}"


# ===========================================================================
# Converter
# ===========================================================================

class Converter:
    """
    Resample OHLCV candle data from a lower timeframe to a higher timeframe.

    Aggregation rules
    -----------------
    * open         → first value in the group
    * high         → maximum value in the group
    * low          → minimum value in the group
    * close        → last value in the group
    * volume       → sum of the group
    * quote_volume → sum
    * trades       → sum
    * taker_buy_*  → sum

    Incomplete groups (groups that do not have the full expected number of
    source candles) are automatically dropped from the output.  This happens
    at the tail of the data when the latest candle has not yet closed.

    Parameters (constructor)
    ------------------------
    source : str | pd.DataFrame
        Either a path to a CSV file written by ``Collector``, or a pandas
        DataFrame that already has a ``timestamp`` column (UTC milliseconds).
    target_timeframe : str
        The timeframe you want the output in.  e.g. ``"4h"``, ``"1d"``, ``"1w"``.
    source_timeframe : str | None
        Only required when ``source`` is a DataFrame **and** the timeframe
        cannot be reliably detected (e.g. the DataFrame has fewer than 2 rows).
        When ``source`` is a CSV file this parameter is ignored — the
        timeframe is always detected automatically from the timestamps.

    Settable attributes
    -------------------
    destination : str
        Folder where the output CSV will be saved.  Default: ``"./"``
    outputname : str | None
        Custom CSV filename.  If ``None``, auto-generated as
        ``converted_<SYMBOL>_<source_tf>_to_<target_tf>.csv``
        (symbol is extracted from the source filename when available).

    Examples
    --------
    ::

        # Basic usage — CSV in, CSV out
        conv = Converter("data/BTCUSDT_1h.csv", target_timeframe="4h")
        conv.destination = "data/"
        conv.convert()

        # DataFrame input
        conv = Converter(df, target_timeframe="1d")
        conv.destination = "data/"
        conv.outputname  = "btc_daily.csv"
        conv.convert()
    """

    def __init__(
        self,
        source: "str | pd.DataFrame",
        target_timeframe: str,
        source_timeframe: "str | None" = None,
    ) -> None:
        self._source_input     = source
        self._target_tf        = normalize_timeframe(target_timeframe)
        self._source_tf_hint   = normalize_timeframe(source_timeframe) if source_timeframe else None

        # Settable by the user before calling convert()
        self.destination: str        = "./"
        self.outputname: "str | None" = None

    # -----------------------------------------------------------------------
    # Public
    # -----------------------------------------------------------------------

    def convert(self) -> str:
        ...

    def _detect_timeframe(self, df: pd.DataFrame) -> str:
        """
        Infer the timeframe of *df* by measuring the gap between its timestamps.

        We look at the **most common** gap across the first 20 rows rather than
        just the first two.  This makes detection robust even when the data
        starts with a small gap (e.g. a weekend with no trades on some markets).

        Raises
        ------
        ValueError
            If the DataFrame has fewer than 2 rows, or if the most common gap
            does not match any known timeframe.
        """
        timestamps = sorted(df["timestamp"].astype("int64").tolist())

        if len(timestamps) < 2:
            raise ValueError(
                "Need at least 2 candles to auto-detect the timeframe. "
                "Pass source_timeframe='Xh' explicitly if your data is very short."
            )

        # Compute gaps between consecutive timestamps (up to first 20 candles)
        sample = timestamps[:21]
        gaps = [sample[i + 1] - sample[i] for i in range(len(sample) - 1)]

        # Pick the most frequently occurring gap
        most_common_gap = max(set(gaps), key=gaps.count)

        # Look it up in TIMEFRAME_MS
        for tf, tf_ms in TIMEFRAME_MS.items():
            if most_common_gap == tf_ms:
                return tf

        # Not found — give the user something actionable
        seconds  = most_common_gap // 1000
        minutes  = seconds // 60
        hours    = minutes // 60
        readable = (
            f"{hours}h" if hours and minutes % 60 == 0
            else f"{minutes}m" if minutes and seconds % 60 == 0
            else f"{seconds}s"
        )
        raise ValueError(
            f"Cannot detect timeframe: the most common gap between candles is "
            f"{most_common_gap} ms ({readable}), which does not match any "
            f"supported timeframe. "
            f"Supported: {sorted(TIMEFRAME_MS)}. "
            f"Pass source_timeframe='...' explicitly to override."
        )

    def _validate_conversion(self, source_tf: str, target_tf: str) -> None:
        """
        Raise ``ValueError`` if converting *source_tf* → *target_tf* is not valid.

        Rules
        -----
        * Same timeframe is rejected.
        * Going down (target smaller than source) is rejected.
        * ``"1M"`` (monthly) as a source is always rejected.
        * For fixed-length timeframes, ``target_ms`` must be a whole multiple
          of ``source_ms``.
        * ``"1M"`` as a target is always accepted (pandas uses calendar months).
        """
        if source_tf == target_tf:
            raise ValueError(
                f"source and target timeframe are both '{source_tf}'. "
                f"They must be different."
            )

        # Monthly source → always impossible to go higher
        if source_tf == "1M":
            raise ValueError(
                "Cannot convert from monthly ('1M'): "
                "there is no supported timeframe above 1M."
            )

        # Monthly target → always valid (any fixed TF can roll up to a month)
        if target_tf == "1M":
            return

        source_ms = TIMEFRAME_MS[source_tf]
        target_ms = TIMEFRAME_MS[target_tf]

        # Going down — target is smaller than or equal to source
        if target_ms <= source_ms:
            raise ValueError(
                f"Cannot convert {source_tf} → {target_tf}: "
                f"the target ({target_tf} = {target_ms} ms) must be "
                f"larger than the source ({source_tf} = {source_ms} ms). "
                f"Downsampling is not supported."
            )

        # Not a whole multiple — e.g. 4h → 6h (6h / 4h = 1.5 — not whole)
        if target_ms % source_ms != 0:
            raise ValueError(
                f"Cannot convert {source_tf} → {target_tf}: "
                f"{target_tf} ({target_ms} ms) is not a whole multiple of "
                f"{source_tf} ({source_ms} ms). "
                f"The ratio is {target_ms / source_ms:.2f}. "
                f"It must be an integer (e.g. 1h → 4h gives ratio 4.0 ✓)."
            )

    def _load_source(self) -> "tuple[pd.DataFrame, str]":
        """
        Load the source data and return ``(dataframe, display_name)``.

        Handles two cases:
        * ``str`` — path to a CSV file.  File must exist and have a
          ``timestamp`` column.
        * ``pd.DataFrame`` — used as-is.  Must have a ``timestamp`` column.

        The ``display_name`` is the filename (for CSV) or ``"<DataFrame>"``
        (for in-memory data).  It is used later to auto-generate the output
        filename.

        Raises
        ------
        FileNotFoundError
            If the path does not point to an existing file.
        ValueError
            If the file/DataFrame is empty or lacks a ``timestamp`` column.
        TypeError
            If source is neither a string nor a DataFrame.
        """
        source = self._source_input

        # ---- Case 1: CSV file path ---------------------------------------
        if isinstance(source, str):
            path = os.path.abspath(source)

            if not os.path.isfile(path):
                raise FileNotFoundError(
                    f"Source file not found: '{path}'"
                )

            try:
                df = pd.read_csv(path)
            except Exception as exc:
                raise ValueError(f"Could not read CSV file '{path}': {exc}") from exc

            if df.empty:
                raise ValueError(f"Source file is empty: '{path}'")

            if "timestamp" not in df.columns:
                raise ValueError(
                    f"Source file '{path}' has no 'timestamp' column. "
                    f"Columns found: {list(df.columns)}"
                )

            df["timestamp"] = df["timestamp"].astype("int64")
            df = df.sort_values("timestamp").reset_index(drop=True)

            display_name = os.path.basename(path)
            return df, display_name

        # ---- Case 2: pandas DataFrame ------------------------------------
        elif isinstance(source, pd.DataFrame):
            if source.empty:
                raise ValueError("Source DataFrame is empty.")

            if "timestamp" not in source.columns:
                raise ValueError(
                    "Source DataFrame has no 'timestamp' column. "
                    f"Columns found: {list(source.columns)}"
                )

            df = source.copy()
            df["timestamp"] = df["timestamp"].astype("int64")
            df = df.sort_values("timestamp").reset_index(drop=True)

            return df, "<DataFrame>"

        # ---- Unsupported type -------------------------------------------
        else:
            raise TypeError(
                f"source must be a file path (str) or a pandas DataFrame, "
                f"got {type(source).__name__}."
            )

    def _resample(
        self,
        df: pd.DataFrame,
        source_tf: str,
        target_tf: str,
    ) -> pd.DataFrame:
        """
        Resample *df* from *source_tf* to *target_tf*.

        Returns a DataFrame with the same column schema as the input,
        with incomplete groups at the edges dropped.

        How aggregation works
        ---------------------
        * open         → first
        * high         → max
        * low          → min
        * close        → last
        * volume       → sum
        * quote_volume → sum
        * trades       → sum
        * taker_buy_*  → sum
        * close_time   → last   (close time of the last sub-candle in the group)
        * timestamp    → first  (open time of the first sub-candle = new open time)
        """
        freq = _PANDAS_FREQ[target_tf]

        # Build a datetime index from the millisecond timestamps
        df = df.copy()
        df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df = df.set_index("datetime").sort_index()

        # Define aggregation rules for every column we care about
        # (extra columns not listed here are silently dropped)
        agg_rules: dict[str, str] = {
            "open":            "first",
            "high":            "max",
            "low":             "min",
            "close":           "last",
            "volume":          "sum",
            "close_time":      "last",
            "quote_volume":    "sum",
            "trades":          "sum",
            "taker_buy_base":  "sum",
            "taker_buy_quote": "sum",
        }

        # Only aggregate columns that actually exist in the source data
        existing_agg = {col: rule for col, rule in agg_rules.items() if col in df.columns}

        resampled = df.resample(freq).agg(existing_agg)

        # Drop groups where there was no data at all (open is NaN)
        resampled = resampled.dropna(subset=["open"])

        # ---- Drop incomplete groups ------------------------------------
        resampled = self._drop_incomplete(df, resampled, source_tf, target_tf, freq)

        # Convert datetime index back to millisecond timestamp
        resampled["timestamp"] = (
            resampled.index.astype("int64") // 1_000_000
        )
        resampled = resampled.reset_index(drop=True)

        # Ensure correct column order, filling any missing optional columns with None
        final_cols = [c for c in _COLUMNS if c in resampled.columns]
        return resampled[final_cols]

    def _drop_incomplete(
        self,
        source_df:   pd.DataFrame,
        resampled:   pd.DataFrame,
        source_tf:   str,
        target_tf:   str,
        freq:        str,
    ) -> pd.DataFrame:
        """
        Remove groups that do not have the expected number of source candles.

        For fixed-length timeframes, every complete group must contain
        exactly ``target_ms // source_ms`` source candles.

        For monthly targets (``"1M"``), we count source candles per calendar
        month and compare against the most common count.  We keep only groups
        that match the mode — this drops partial months at both edges.
        """
        if target_tf == "1M":
            return self._drop_incomplete_monthly(source_df, resampled, freq)

        source_ms   = TIMEFRAME_MS[source_tf]
        target_ms   = TIMEFRAME_MS[target_tf]
        expected    = target_ms // source_ms

        # Count how many source rows fall into each resampled bucket
        counts = source_df.resample(freq).size()

        # Keep only buckets that have exactly the right number of candles
        complete_idx = counts[counts == expected].index
        return resampled[resampled.index.isin(complete_idx)]

    def _drop_incomplete_monthly(
        self,
        source_df: pd.DataFrame,
        resampled: pd.DataFrame,
        freq:      str,
    ) -> pd.DataFrame:
        """
        For monthly targets, drop months that have fewer candles than the mode.

        Why the mode and not a fixed number?
        A daily → monthly conversion has 28–31 source candles per group
        depending on the month.  Using the mode (most common count) means
        February is still kept — only partially-collected months (the current
        live month, or a partial month at the start of the data) are dropped.
        """
        counts = source_df.resample(freq).size()

        if counts.empty:
            return resampled

        mode_count  = counts.mode().iloc[0]
        # Accept months within ±1 candle of the mode to handle February / DST
        tolerance   = 1
        valid_idx   = counts[counts >= mode_count - tolerance].index

        return resampled[resampled.index.isin(valid_idx)]

    def _resolve_filepath(self, source_tf, target_tf) -> str:
        # Build output path, same logic as Collector
        ...
