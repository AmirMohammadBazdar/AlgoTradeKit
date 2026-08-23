"""
algotradekit.visual.chart
~~~~~~~~~~~~~~~~~~~~~~~~~
Main user-facing ``Chart`` class.

Quick start
-----------
    # From an AlgoTradeKit Collector CSV (timestamp in ms):
    from AlgoTradeKit.visual import Chart

    c = Chart.from_csv("data/binance-futures_BTCUSDT_1d.csv")
    c.show(block=True)

    # Full pipeline:
    from AlgoTradeKit.data   import Collector
    from AlgoTradeKit.visual import Chart

    collector = Collector("binance-futures", "BTCUSDT", "1d")
    collector.destination = "data/"
    collector.starttime   = "2024/01/01"
    collector.collect()

    c = Chart.from_csv("data/binance-futures_BTCUSDT_1d.csv")
    c.show(block=True)

    # With indicators (AlgoTradeKit DataFrames use 'timestamp', not 'time'):
    ema_df = df.assign(value=df["close"].ewm(span=20).mean())[["timestamp","value"]]
    c.add_indicator_from_atk(ema_df, name="EMA 20", color="#f0c040", overlay=True)

    # Live streaming (for future trade module):
    for candle in exchange.live_feed("BTCUSDT", "1m"):
        c.stream_from_atk(candle)      # handles 'timestamp' ms automatically
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Literal

import pandas as pd

from .models import (
    Box,
    FibRetracement,
    HorizontalLine,
    IndicatorSeries,
    LivePosition,
    PositionBox,
    Signal,
    TextLabel,
    TrendLine,
)
from .server import ChartServer

# Sentinel for "argument not passed" where None is a meaningful value
# (e.g. update_live_position(next_tp=None) clears the TP line).
_UNSET = object()


class Chart:
    """
    Interactive candlestick chart served in a browser via WebSocket.

    One ``Chart`` instance = one browser tab.

    Parameters
    ----------
    title : str
        Window / tab title shown in the browser header bar.
    chart_type : "candlestick" | "heikinashi"
        Initial chart rendering mode.
    port : int
        TCP port for the local server.  0 = auto-select.
    theme : "dark" | "light"
        Colour theme.
    volume_in_main : bool
        When ``True`` volume bars appear in the main price pane.
    host : str
        Network interface the chart server binds to (v1.0.0).  Default
        ``"127.0.0.1"`` keeps it reachable from this machine only.

        .. warning::
           **Security** — ``host="0.0.0.0"`` exposes the chart on **every
           network interface**: anyone who can reach this machine (LAN, or
           the whole internet on an unfirewalled VPS) can open the chart,
           see your data and send WebSocket messages.  There is no
           authentication.  Only use it on trusted / firewalled networks;
           an SSH tunnel to the default binding is the safer alternative.
    candle_count_limit : int | None
        Rolling window size (v1.0.0).  When set, the chart only ever keeps
        the last N candles: older candles, and drawings whose whole time
        range left the window, are dropped on both the Python side and the
        browser side as new candles stream in.  ``None`` = unbounded.
        Can also be changed later via :meth:`set_candle_limit`.
    source_timeframe : str | None
        Timeframe of the data you pass to :meth:`set_data` (v1.1.0).
        ``None`` auto-detects it from the timestamps.  Give it explicitly for
        very short or gappy data, where detection cannot see a regular step.
    display_timeframe : str | None
        Timeframe to *show* (v1.1.0).  ``None`` shows the source unchanged.
        Anything higher is resampled **in Python** — feed 1m candles, display
        5m, and let the viewer switch from the toolbar.  Only whole multiples
        of the source are possible (5m from 1m yes, 6h from 4h no).
    timeframes : list[str] | None
        Which timeframes the browser's selector offers.  ``None`` offers every
        valid multiple of the source.  Pass a list to narrow it; an entry that
        cannot be produced from the source is rejected.
    """

    def __init__(
        self,
        title:          str  = "Chart",
        chart_type:     Literal["candlestick", "heikinashi"] = "candlestick",
        port:           int  = 0,
        theme:          Literal["dark", "light"] = "dark",
        volume_in_main: bool = True,
        host:           str  = "127.0.0.1",
        candle_count_limit: int | None = None,
        source_timeframe:   str | None = None,
        display_timeframe:  str | None = None,
        timeframes:         list[str] | None = None,
    ) -> None:
        self.title          = title
        self.chart_type     = chart_type
        self.theme          = theme
        self.volume_in_main = volume_in_main

        self._bars:       list[dict]          = []
        self._indicators: list[IndicatorSeries] = []
        self._drawings:   list                = []
        self.df:          pd.DataFrame        = pd.DataFrame()  # raw OHLCV kept for indicator use

        # ── Timeframe state (v1.1.0) ──────────────────────────────────────
        # _raw_df   : exactly what set_data() was given (what self.df is at
        #             the source timeframe — kept so switching back is exact)
        # _source_df: the same data canonicalised to a 'timestamp' ms column,
        #             which is what the resampler consumes
        self._raw_df:    pd.DataFrame = pd.DataFrame()
        self._source_df: pd.DataFrame = pd.DataFrame()
        self._source_tf:  str | None = None
        self._display_tf: str | None = None
        self._tf_hint          = source_timeframe
        self._pending_display  = display_timeframe
        self._tf_requested     = timeframes
        self._timeframes: list[str] = []
        self._candle_range: dict | None = None

        self._server = ChartServer(title=title, port=port, host=host)
        self._server.on_message = self._handle_browser_message
        self._shown = False

        self._candle_count_limit: int | None = None
        if candle_count_limit is not None:
            self.set_candle_limit(candle_count_limit)

        # Optional callback: called when the user removes an indicator from
        # the browser legend.  Signature: on_indicator_removed(name: str) -> None
        self.on_indicator_removed = None

    # -----------------------------------------------------------------------
    # Factory constructors
    # -----------------------------------------------------------------------

    @classmethod
    def from_csv(
        cls,
        path:          str,
        title:         str  = "",
        chart_type:    Literal["candlestick", "heikinashi"] = "candlestick",
        theme:         Literal["dark", "light"] = "dark",
        port:          int  = 0,
        volume_in_main: bool = True,
        candle_range:  dict | None = None,
        source_timeframe:  str | None = None,
        display_timeframe: str | None = None,
        timeframes:        list[str] | None = None,
    ) -> Chart:
        """
        Create a Chart pre-loaded with data from an AlgoTradeKit CSV file.

        The CSV must have been produced by ``Collector`` (has a ``timestamp``
        column in UTC milliseconds) or any CSV with compatible OHLCV columns.

        Parameters
        ----------
        path : str
            Path to the CSV file.
        title : str
            Chart window title.  Defaults to the filename.
        candle_range : dict | None
            Restrict which candles are shown.  Accepted dict shapes:

            * ``{"start": ..., "end": ...}``  — datetime range (both optional)
            * ``{"start": ...}``              — from date to last candle
            * ``{"end": ...}``                — from first candle to date
            * ``{"last_n": 500}``             — last 500 candles
            * ``{"first_n": 500}``            — first 500 candles

            ``start`` / ``end`` values accept the same formats as
            ``Collector.starttime``: ``"YYYY/MM/DD"``, ``"YYYY-MM-DD"``,
            ``datetime``, Unix-seconds ``int``, or Unix-milliseconds ``int``.
        source_timeframe, display_timeframe, timeframes
            Timeframe switching (v1.1.0) — see :class:`Chart`.  Load a 1m CSV
            with ``display_timeframe="5m"`` and the viewer can switch from the
            toolbar.

        Example
        -------
        ::

            # Load a Collector CSV directly — no extra code needed
            c = Chart.from_csv("data/binance-futures_BTCUSDT_1d.csv")
            c.show(block=True)

            # Show only the last 200 candles
            c = Chart.from_csv("data/btc_1h.csv", candle_range={"last_n": 200})

            # Show a specific date range
            c = Chart.from_csv(
                "data/btc_1h.csv",
                candle_range={"start": "2024/01/01", "end": "2024/06/01"},
            )
        """
        import os
        df = pd.read_csv(path)
        if not title:
            title = os.path.basename(path)
        chart = cls(
            title=title, chart_type=chart_type,
            theme=theme, port=port, volume_in_main=volume_in_main,
            source_timeframe=source_timeframe,
            display_timeframe=display_timeframe,
            timeframes=timeframes,
        )
        chart.set_data(df, candle_range=candle_range)
        return chart

    # -----------------------------------------------------------------------
    # Data
    # -----------------------------------------------------------------------

    def set_data(
        self,
        df:         pd.DataFrame,
        chart_type: str | None = None,
        candle_range: dict | None = None,
    ) -> Chart:
        """
        Load OHLCV data from a pandas DataFrame.

        Accepted column names (case-insensitive)
        -----------------------------------------
        * ``time`` or ``timestamp``  — Unix seconds *or* milliseconds (auto-detected)
        * ``open``, ``high``, ``low``, ``close``  — required
        * ``volume``                              — optional (defaults to 0)

        AlgoTradeKit ``Collector`` CSVs use a ``timestamp`` column in
        milliseconds — this method handles that automatically.

        Parameters
        ----------
        df : pd.DataFrame
            OHLCV data.  May also have a ``DatetimeIndex``.
        chart_type : str | None
            Override the chart type for this data load.
        candle_range : dict | None
            Restrict which candles are shown.  Accepted dict shapes:

            * ``{"start": ..., "end": ...}``  — datetime range (both optional)
            * ``{"start": ...}``              — from date to last candle
            * ``{"end": ...}``                — from first candle to date
            * ``{"last_n": 500}``             — last 500 candles
            * ``{"first_n": 500}``            — first 500 candles

            ``start`` / ``end`` accept: ``"YYYY/MM/DD"``, ``"YYYY-MM-DD"``,
            ``datetime``, Unix-seconds ``int``, or Unix-milliseconds ``int``.
        """
        df = df.copy()
        df.columns = [c.lower() for c in df.columns]

        # Keep the original (pre-mutation) DataFrame available for the
        # indicator module.  Store before any renaming/conversion so that
        # column names match what the user expects (e.g. 'timestamp' intact).
        self.df      = df.copy()
        self._raw_df = df.copy()

        if chart_type is not None:
            self.chart_type = chart_type
        self._candle_range = candle_range

        # ── Timeframe support (v1.1.0) ────────────────────────────────────
        # Canonicalise to a 'timestamp' (ms) frame for the resampler, work out
        # the source timeframe, and decide which timeframes may be offered.
        self._source_df = self._canonical_ms_frame(df)
        self._detect_source_timeframe()
        self._resolve_timeframes()

        target = self._pending_display if self._display_tf is None else self._display_tf
        if target is not None and self._source_tf is not None:
            target = self._normalize_tf(target)
        if target is not None and target != self._source_tf:
            # Renders through the same pipeline, then sends init if shown.
            self._pending_display = None
            self._rebuild_for_timeframe(target)
            return self
        self._pending_display = None
        self._display_tf = None

        self._bars = self._frame_to_bars(df)

        # Rolling window (v1.0.0): a freshly loaded frame obeys the limit too
        self._enforce_candle_limit()

        if self._shown:
            self._send_init()

        return self

    def _frame_to_bars(self, df: pd.DataFrame) -> list[dict]:
        """
        Turn a (lower-cased) OHLCV frame into the browser's bar list.

        Shared by :meth:`set_data` and the timeframe rebuild so both produce
        byte-identical bars.  Accepts ``time`` (seconds or ms) or ``timestamp``
        (ms), applies Heikin-Ashi and the candle-range filter.
        """
        # ── AlgoTradeKit compatibility ─────────────────────────────────────
        # Collector CSVs use 'timestamp' (ms). Rename it so the rest of
        # the method (which works with 'time') handles it uniformly.
        if "timestamp" in df.columns and "time" not in df.columns:
            df.rename(columns={"timestamp": "time"}, inplace=True)
        # ──────────────────────────────────────────────────────────────────

        # Normalise the time column
        if "time" not in df.columns:
            if isinstance(df.index, pd.DatetimeIndex):
                df["time"] = (df.index.astype("int64") // 1_000_000_000).astype(int)
            else:
                raise ValueError(
                    "DataFrame must have a 'time' or 'timestamp' column, "
                    "or a DatetimeIndex."
                )
        else:
            col = df["time"]
            if pd.api.types.is_datetime64_any_dtype(col):
                df["time"] = (col.astype("int64") // 1_000_000_000).astype(int)
            elif df["time"].dtype == object:
                df["time"] = (
                    pd.to_datetime(col).astype("int64") // 1_000_000_000
                ).astype(int)
            else:
                # Auto-detect milliseconds: values > 1e10 are ms, not seconds
                ts_vals = pd.to_numeric(df["time"], errors="coerce")
                if ts_vals.max() > 1e10:
                    df["time"] = (ts_vals // 1000).astype(int)
                else:
                    df["time"] = ts_vals.astype(int)

        if self.chart_type == "heikinashi":
            df = self._to_heikinashi(df)

        required = {"open", "high", "low", "close"}
        missing  = required - set(df.columns)
        if missing:
            raise ValueError(f"DataFrame missing required columns: {missing}")

        # ── Candle range filter (v0.7.2) ──────────────────────────────────
        # Applied *after* time is in Unix seconds so start/end comparisons
        # work uniformly regardless of the original timestamp format.
        if self._candle_range is not None:
            df = self._apply_candle_range(df, self._candle_range)
        # ─────────────────────────────────────────────────────────────────

        return [
            {
                "time":   int(row["time"]),
                "open":   float(row["open"]),
                "high":   float(row["high"]),
                "low":    float(row["low"]),
                "close":  float(row["close"]),
                "volume": float(row.get("volume", 0)),
            }
            for _, row in df.iterrows()
        ]

    # -----------------------------------------------------------------------
    # Candle range filter (v0.7.2)
    # -----------------------------------------------------------------------

    @staticmethod
    def _apply_candle_range(df: pd.DataFrame, candle_range: dict) -> pd.DataFrame:
        """
        Filter *df* (which has a ``time`` column in Unix **seconds**) according
        to *candle_range*.

        Accepted shapes
        ---------------
        ``{"start": ..., "end": ...}``   — keep rows in [start, end] (both optional)
        ``{"start": ...}``               — from date to last candle
        ``{"end": ...}``                 — from first candle to date
        ``{"last_n": int}``              — last N candles
        ``{"first_n": int}``             — first N candles
        """
        if not isinstance(candle_range, dict) or not candle_range:
            return df

        df = df.copy()

        # ── last_n / first_n ──────────────────────────────────────────────
        if "last_n" in candle_range:
            n = int(candle_range["last_n"])
            return df.iloc[-n:].reset_index(drop=True)

        if "first_n" in candle_range:
            n = int(candle_range["first_n"])
            return df.iloc[:n].reset_index(drop=True)

        # ── datetime range (start / end) ──────────────────────────────────
        start_val = candle_range.get("start")
        end_val   = candle_range.get("end")

        if start_val is not None:
            start_s = Chart._parse_range_time(start_val)
            df = df[df["time"] >= start_s]

        if end_val is not None:
            end_s = Chart._parse_range_time(end_val)
            df = df[df["time"] <= end_s]

        return df.reset_index(drop=True)

    @staticmethod
    def _parse_range_time(value) -> int:
        """
        Convert a ``candle_range`` start/end value to Unix **seconds**.

        Accepts
        -------
        * ``str``      — ``"YYYY/MM/DD"``, ``"YYYY-MM-DD"``,
                         ``"YYYY/MM/DD HH:MM"``, ``"YYYY-MM-DD HH:MM:SS"``
        * ``datetime`` — timezone-aware or naive (assumed UTC)
        * ``int``      — already in seconds if < 10^10; in ms if >= 10^10
        * ``float``    — treated as seconds
        """
        from datetime import datetime
        from datetime import timezone as _tz

        _PARSE_FMTS = [
            "%Y/%m/%d", "%Y-%m-%d",
            "%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M",
            "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S",
        ]
        _MS_THRESHOLD = 10_000_000_000

        if isinstance(value, (int, float)):
            v = int(value)
            # If the value looks like milliseconds, convert to seconds
            return v // 1000 if v >= _MS_THRESHOLD else v

        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=_tz.utc)
            return int(value.timestamp())

        if isinstance(value, str):
            for fmt in _PARSE_FMTS:
                try:
                    dt = datetime.strptime(value.strip(), fmt)
                    return int(dt.replace(tzinfo=_tz.utc).timestamp())
                except ValueError:
                    continue
            raise ValueError(
                f"Cannot parse candle_range datetime string '{value}'. "
                "Use YYYY/MM/DD or YYYY-MM-DD (optionally with HH:MM or HH:MM:SS)."
            )

        raise TypeError(
            f"candle_range start/end must be str, datetime, int, or float; "
            f"got {type(value).__name__}."
        )

    @staticmethod
    def _to_heikinashi(df: pd.DataFrame) -> pd.DataFrame:
        ha = df.copy()
        ha["close"] = (df["open"] + df["high"] + df["low"] + df["close"]) / 4
        ha_open = [(df["open"].iloc[0] + df["close"].iloc[0]) / 2]
        for i in range(1, len(df)):
            ha_open.append((ha_open[-1] + ha["close"].iloc[i - 1]) / 2)
        ha["open"] = ha_open
        ha["high"] = pd.concat(
            [ha["open"], ha["close"], df["high"]], axis=1
        ).max(axis=1)
        ha["low"] = pd.concat(
            [ha["open"], ha["close"], df["low"]], axis=1
        ).min(axis=1)
        return ha

    # -----------------------------------------------------------------------
    # Timeframe switching (v1.1.0)
    # -----------------------------------------------------------------------
    # The chart holds the data at its **source** timeframe and resamples to
    # whatever is being displayed.  All of it happens here in Python: the
    # browser only ever asks for a timeframe and receives finished candles.

    @staticmethod
    def _normalize_tf(tf: str) -> str:
        from ..data.converter import normalize_timeframe

        return normalize_timeframe(tf)

    @staticmethod
    def _canonical_ms_frame(df: pd.DataFrame) -> pd.DataFrame:
        """
        Return *df* with a ``timestamp`` column in UTC **milliseconds**.

        The resampler needs one canonical shape; users hand us seconds, ms,
        datetimes or a DatetimeIndex.  Everything else in the frame is kept,
        so extra Collector columns still aggregate.
        """
        out = df.copy()

        col = None
        if "timestamp" in out.columns:
            col = out["timestamp"]
        elif "time" in out.columns:
            col = out["time"]
        elif isinstance(out.index, pd.DatetimeIndex):
            out["timestamp"] = out.index.astype("int64") // 1_000_000
            return out.reset_index(drop=True)
        else:
            return pd.DataFrame()

        if pd.api.types.is_datetime64_any_dtype(col):
            ms = col.astype("int64") // 1_000_000
        elif col.dtype == object:
            ms = pd.to_datetime(col).astype("int64") // 1_000_000
        else:
            vals = pd.to_numeric(col, errors="coerce")
            # Seconds vs milliseconds, same rule set_data uses
            ms = vals.astype("int64") if vals.max() > 1e10 else (vals * 1000).astype("int64")

        out["timestamp"] = ms.astype("int64")
        if "time" in out.columns and "timestamp" != "time":
            out = out.drop(columns=["time"])
        return out.sort_values("timestamp").reset_index(drop=True)

    def _detect_source_timeframe(self) -> None:
        """Work out the source timeframe, or leave it ``None`` (switching off)."""
        self._source_tf = None
        if self._source_df.empty or "timestamp" not in self._source_df.columns:
            return

        from ..data.converter import detect_timeframe

        if self._tf_hint:
            hint = self._normalize_tf(self._tf_hint)
            try:
                found = detect_timeframe(self._source_df)
            except ValueError:
                # Too short / too gappy to detect — trust the caller.
                self._source_tf = hint
                return
            if found != hint:
                raise ValueError(
                    f"source_timeframe='{self._tf_hint}' does not match the data, "
                    f"whose candles are {found} apart. Drop the argument to let it "
                    f"be detected, or correct it."
                )
            self._source_tf = hint
            return

        try:
            self._source_tf = detect_timeframe(self._source_df)
        except ValueError:
            # Irregular data (gappy sessions, a handful of rows): the chart
            # still works, it just cannot offer other timeframes.
            self._source_tf = None

    def _resolve_timeframes(self) -> None:
        """Build the list of timeframes the selector may offer."""
        self._timeframes = []
        if self._source_tf is None:
            if self._tf_requested:
                raise ValueError(
                    "timeframes= was given but the source timeframe could not be "
                    "detected. Pass source_timeframe= as well."
                )
            return

        from ..data._utils import TIMEFRAME_MS
        from ..data.converter import can_convert

        if self._tf_requested is None:
            # "1M" is deliberately absent: months are not a fixed number of
            # milliseconds, and every bucket calculation here is arithmetic.
            offered = [tf for tf in TIMEFRAME_MS if can_convert(self._source_tf, tf)]
        else:
            offered = []
            for raw in self._tf_requested:
                tf = self._normalize_tf(raw)
                if tf == "1M":
                    raise ValueError(
                        "'1M' cannot be a chart timeframe: months have no fixed "
                        "length, and the chart's bucket maths is arithmetic."
                    )
                if tf == self._source_tf or can_convert(self._source_tf, tf):
                    offered.append(tf)
                else:
                    raise ValueError(
                        f"timeframe '{raw}' cannot be produced from {self._source_tf} "
                        f"data — it must be a whole multiple of it."
                    )
        # The source itself is always selectable, and always sorts first.
        self._timeframes = [self._source_tf] + [tf for tf in offered if tf != self._source_tf]

    @property
    def source_timeframe(self) -> str | None:
        """Timeframe of the data that was loaded (``None`` = undetectable)."""
        return self._source_tf

    @property
    def display_timeframe(self) -> str | None:
        """Timeframe currently shown.  ``None`` means the source timeframe."""
        return self._display_tf

    @property
    def timeframes(self) -> list[str]:
        """Timeframes the browser's selector offers (source first)."""
        return list(self._timeframes)

    def set_timeframe(self, timeframe: str | None) -> Chart:
        """
        Switch the displayed timeframe (v1.1.0).

        ``None`` (or the source timeframe) shows the data as loaded; anything
        higher is resampled from the source.  Candles, spec-built indicators
        and the browser are all updated; drawings are absolute-time and are
        left alone.

        The same path serves the browser's timeframe selector, so a
        programmatic switch and a click behave identically.
        """
        if timeframe is not None:
            timeframe = self._normalize_tf(timeframe)
        if self._source_df.empty:
            # No data yet — remember it and apply on the next set_data().
            self._pending_display = timeframe
            return self
        if self._source_tf is None:
            raise ValueError(
                "This chart cannot change timeframe: the source timeframe could "
                "not be detected from the data. Pass source_timeframe= to Chart()."
            )
        if timeframe is not None and timeframe not in self._timeframes:
            raise ValueError(
                f"'{timeframe}' is not one of this chart's timeframes: {self._timeframes}"
            )
        return self._rebuild_for_timeframe(timeframe)

    def _rebuild_for_timeframe(self, timeframe: str | None) -> Chart:
        """Re-render candles and indicators at *timeframe* and push to the browser."""
        back_to_source = timeframe is None or timeframe == self._source_tf

        if back_to_source:
            frame     = self._raw_df.copy()
            self.df   = self._raw_df.copy()
            self._display_tf = None
        else:
            from ..data.converter import resample_ohlcv

            # drop_incomplete=False: a chart shows the candle that is still
            # forming, exactly as TradingView does.  Converter (a data
            # pipeline) keeps dropping it.
            frame = resample_ohlcv(
                self._source_df, timeframe,
                source_timeframe=self._source_tf, drop_incomplete=False,
            )
            frame = self._drop_partial_first_candle(frame)
            self.df = frame.copy()
            self._display_tf = timeframe

        self._bars = self._frame_to_bars(frame)
        self._enforce_candle_limit()
        self._recompute_indicators()

        if self._shown:
            self._send_init()
        return self

    # ------------------------------------------------------------------
    # Indicators across a timeframe change
    # ------------------------------------------------------------------

    def _recompute_indicators(self) -> None:
        """
        Rebuild every indicator for the current display timeframe.

        Two kinds exist and they cannot be treated alike:

        * **spec-built** — added through :meth:`add_indicator_spec` (the
          browser toolbar, ``SimulateConfig.chart_indicators``).  The recipe
          is known, so the maths is simply re-run on the new candles and the
          values are exact.
        * **raw** — a caller handed us finished points
          (:meth:`add_indicator_from_atk`, ``add_rsi`` / ``add_macd`` /
          ``add_ichimoku``, a strategy's own columns).  There is no recipe to
          re-run, so the points are downsampled last-value-per-bucket and
          tagged ``approx`` so the legend can say so.
        """
        old = list(self._indicators)
        if not old:
            return

        self._indicators = []
        # id(spec) → did the recompute succeed?  One spec can have produced
        # several series (MACD makes three, Ichimoku more) that all share the
        # very same dict, so it must run once — but when it *fails* every one
        # of those series still has to be carried over individually.
        handled: dict[int, bool] = {}

        for ind in old:
            payload = getattr(ind, "payload", None)
            spec    = payload.get("spec") if isinstance(payload, dict) else None

            if spec is not None:
                sid = id(spec)
                if sid not in handled:
                    start = len(self._indicators)
                    try:
                        ok = self._compute_indicator_series(spec) is not None
                    except (ValueError, KeyError, IndexError):
                        # A higher timeframe leaves fewer candles, and an
                        # indicator can need more than remain (MACD wants 34).
                        # Losing the series would be worse than thinning it.
                        ok = False
                    handled[sid] = ok
                    if ok:
                        for fresh in self._indicators[start:]:
                            if hasattr(fresh, "payload"):
                                fresh.payload["spec"] = spec
                        continue
                    del self._indicators[start:]
                elif handled[sid]:
                    continue        # the recompute already produced this one

            self._indicators.append(self._downsample_indicator(ind))

    def _downsample_indicator(self, ind):
        """
        Retime a precomputed indicator to the display timeframe, in place.

        The points as first supplied are kept on the object (``_full_data``)
        and every thinning starts from them — otherwise switching 1m → 15m →
        1m would leave the series permanently at 15m resolution.  At the
        source timeframe the pristine points are simply restored.
        """
        full = getattr(ind, "_full_data", None)
        if full is None:
            full = self._indicator_points(ind)
            if full is None:
                return ind
            ind._full_data = full

        tf_ms  = self._display_tf_ms()
        approx = tf_ms is not None
        data   = self._downsample_points(full, tf_ms) if approx else list(full)

        # A thinned series can gain one bucket in front of the first candle
        # (see _drop_partial_first_candle).  Trim the front only — Ichimoku's
        # spans are *meant* to run past the last candle.
        if approx and self._bars:
            first = self._bars[0]["time"]
            data  = [p for p in data if p.get("time", first) >= first]

        payload = getattr(ind, "payload", None)
        if isinstance(payload, dict):
            payload["data"]   = data
            payload["approx"] = approx
        else:
            ind.data   = data
            ind.approx = approx
        return ind

    @staticmethod
    def _indicator_points(ind) -> list | None:
        """The point list of either indicator flavour, or ``None``."""
        payload = getattr(ind, "payload", None)
        if isinstance(payload, dict):
            data = payload.get("data")
        else:
            data = getattr(ind, "data", None)
        return list(data) if isinstance(data, list) else None

    def _drop_partial_first_candle(self, frame: pd.DataFrame) -> pd.DataFrame:
        """
        Drop a leading bucket the data only partly covers.

        Buckets are aligned to the epoch, so 1m data starting at 09:55 puts its
        first ten minutes into the 09:45 fifteen-minute bucket — a candle that
        would claim a low and an open it never had.  The **trailing** partial
        candle is different and is kept: that one is genuinely still forming,
        which is what a chart should show.
        """
        if frame.empty or len(frame) < 2 or self._source_df.empty:
            return frame
        first_src = int(self._source_df["timestamp"].iloc[0])
        if int(frame["timestamp"].iloc[0]) < first_src:
            return frame.iloc[1:].reset_index(drop=True)
        return frame

    def _display_tf_ms(self) -> int | None:
        """Length of one displayed candle in ms, or ``None`` at the source."""
        if self._display_tf is None:
            return None
        from ..data._utils import TIMEFRAME_MS

        return TIMEFRAME_MS.get(self._display_tf)

    @staticmethod
    def _downsample_points(points: list[dict], tf_ms: int) -> list[dict]:
        """
        Keep the last point of every display bucket, re-timed to the bucket's
        open, so the series lines up with the candles it is drawn over.

        Times here are Unix **seconds** (chart convention), *tf_ms* is in
        milliseconds.
        """
        step = max(1, tf_ms // 1000)
        out: dict[int, dict] = {}
        for p in points:
            t = p.get("time")
            if t is None:
                continue
            bucket = (int(t) // step) * step
            point  = dict(p)
            point["time"] = bucket
            out[bucket] = point          # later points win → last in bucket
        return [out[k] for k in sorted(out)]

    # -----------------------------------------------------------------------
    # Indicators
    # -----------------------------------------------------------------------

    def add_indicator(
        self,
        df:          pd.DataFrame,
        name:        str,
        color:       str  = "#f0c040",
        overlay:     bool = True,
        pane:        int  = 1,
        line_width:  int  = 1,
        series_type: Literal["line", "histogram", "area"] = "line",
        group:       str  = "",
    ) -> Chart:
        """
        Add an indicator series.

        *df* must have a ``time`` column (Unix seconds, same format as
        ``set_data``) and a ``value`` column.

        For multi-line indicators (e.g. Bollinger Bands) call this once
        per line.
        """
        df = df.copy()
        df.columns = [c.lower() for c in df.columns]

        if "time" not in df.columns:
            if isinstance(df.index, pd.DatetimeIndex):
                df["time"] = (df.index.astype("int64") // 1_000_000_000).astype(int)
            else:
                raise ValueError("Indicator DataFrame needs a 'time' column or DatetimeIndex")
        else:
            col = df["time"]
            if pd.api.types.is_datetime64_any_dtype(col):
                df["time"] = (col.astype("int64") // 1_000_000_000).astype(int)
            else:
                ts_vals = pd.to_numeric(df["time"], errors="coerce")
                if ts_vals.max() > 1e10:
                    df["time"] = (ts_vals // 1000).astype(int)
                else:
                    df["time"] = ts_vals.astype(int)

        value_col = "value" if "value" in df.columns else df.columns[-1]
        data = [
            {"time": int(r["time"]), "value": float(r[value_col])}
            for _, r in df.iterrows()
            if not pd.isna(r[value_col])
        ]

        ind = IndicatorSeries(
            name=name, data=data, color=color,
            overlay=overlay, pane=pane,
            line_width=line_width, series_type=series_type,
            group=group or name,
        )
        # A series handed to us at the source resolution has to be thinned if
        # the chart is currently showing a higher timeframe (v1.1.0).
        if self._display_tf is not None:
            self._downsample_indicator(ind)

        self._indicators.append(ind)

        if self._shown:
            self._server.send({"type": "add_indicator", "indicator": ind.to_dict()})

        return self

    def add_indicator_from_atk(
        self,
        df:          pd.DataFrame,
        name:        str,
        color:       str  = "#f0c040",
        overlay:     bool = True,
        pane:        int  = 1,
        line_width:  int  = 1,
        series_type: Literal["line", "histogram", "area"] = "line",
        group:       str  = "",
    ) -> Chart:
        """
        Add an indicator from an AlgoTradeKit-format DataFrame.

        Identical to ``add_indicator()`` but accepts DataFrames that use
        ``timestamp`` (ms) instead of ``time`` (seconds).

        This is the bridge used by the future ``indicator`` and ``strategy``
        modules to display their output on the chart.

        Example
        -------
        ::

            # Manual EMA using AlgoTradeKit CSV columns
            ema_df = df.assign(
                value=df["close"].ewm(span=20).mean()
            )[["timestamp", "value"]]

            c.add_indicator_from_atk(ema_df, name="EMA 20", color="#f0c040")
        """
        df = df.copy()
        df.columns = [c.lower() for c in df.columns]
        if "timestamp" in df.columns and "time" not in df.columns:
            df.rename(columns={"timestamp": "time"}, inplace=True)
        return self.add_indicator(
            df, name=name, color=color,
            overlay=overlay, pane=pane,
            line_width=line_width, series_type=series_type,
            group=group,
        )

    # -----------------------------------------------------------------------
    # Drawings
    # -----------------------------------------------------------------------

    def _add_drawing(self, drawing) -> None:
        """Store *drawing*, push it to live browsers, keep the replay fresh.

        Central path for every drawing add (v1.0.0): the server's cached
        ``init`` is refreshed so a page refresh mid-session reproduces the
        current chart state, including drawings added after ``show()``.
        """
        self._drawings.append(drawing)
        if self._shown:
            self._server.send({"type": "add_drawing", "drawing": drawing.to_dict()})
            self._refresh_init_cache()

    def add_hline(
        self,
        price:      float,
        color:      str = "#ef5350",
        line_width: int = 1,
        line_style: int = 0,
        label:      str = "",
    ) -> Chart:
        d = HorizontalLine(
            price=price, color=color,
            line_width=line_width, line_style=line_style, label=label,
        )
        self._add_drawing(d)
        return self

    def add_trendline(
        self,
        time1:      int,
        price1:     float,
        time2:      int,
        price2:     float,
        color:      str  = "#2196f3",
        line_width: int  = 1,
        extend:     bool = False,
        label:      str  = "",
    ) -> Chart:
        d = TrendLine(
            time1=time1, price1=price1, time2=time2, price2=price2,
            color=color, line_width=line_width, extend=extend, label=label,
        )
        self._add_drawing(d)
        return self

    def add_box(
        self,
        time1:        int,
        price1:       float,
        time2:        int,
        price2:       float,
        color:        str   = "#26a69a",
        opacity:      float = 0.2,
        border_color: str   = "#26a69a",
        label:        str   = "",
    ) -> Chart:
        d = Box(
            time1=time1, price1=price1, time2=time2, price2=price2,
            color=color, opacity=opacity, border_color=border_color, label=label,
        )
        self._add_drawing(d)
        return self

    def add_signal(
        self,
        time:  int,
        side:  Literal["buy", "sell"],
        price: float | None = None,
        label: str = "",
        color: str = "",
    ) -> Chart:
        """
        Add a buy or sell marker.

        This method will be called directly by the future ``strategy`` module
        to visualise entry and exit points.

        Parameters
        ----------
        time  : Unix timestamp in seconds.
        side  : ``"buy"`` or ``"sell"``.
        price : Price at which to anchor the marker.  ``None`` = auto (high/low).
        """
        d = Signal(time=time, side=side, price=price, label=label, color=color)
        self._add_drawing(d)
        return self

    def add_text(
        self,
        time:      int,
        price:     float,
        text:      str,
        color:     str = "#ffffff",
        font_size: int = 12,
    ) -> Chart:
        d = TextLabel(time=time, price=price, text=text, color=color, font_size=font_size)
        self._add_drawing(d)
        return self

    def add_fib(
        self,
        time1:  int,
        price1: float,
        time2:  int,
        price2: float,
        color:  str  = "#9c27b0",
        levels: list | None = None,
        label:  str  = "",
    ) -> Chart:
        d = FibRetracement(
            time1=time1, price1=price1, time2=time2, price2=price2,
            color=color,
            levels=levels or [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0],
            label=label,
        )
        self._add_drawing(d)
        return self

    def add_position_box(
        self,
        open_time:    int,
        close_time:   int,
        entry_price:  float,
        stop_loss:    float,
        take_profit:  float | None,
        direction:    str,
        net_pnl:      float,
        close_reason: str = "",
        trade_id:     int  = -1,
        rr_ratio:     float = 0.0,
        opacity:      float = 0.15,
    ) -> Chart:
        """
        Add a TradingView-style position box for one simulated trade.

        Times must be in **Unix seconds** (divide AlgoTradeKit ms by 1000).
        Use ``add_simulation_positions(chart, report)`` from the
        ``visual.indicator_renderer`` module to add all trades at once.

        Parameters
        ----------
        open_time  : Entry candle time in Unix seconds.
        close_time : Exit candle time in Unix seconds.
        entry_price: Actual fill price.
        stop_loss  : Initial stop-loss price.
        take_profit: Take-profit price or ``None`` (draws 2R placeholder).
        direction  : ``"long"`` or ``"short"``.
        net_pnl    : Net P&L of the trade.
        close_reason: ``"sl"`` / ``"tp"`` / ``"rf"`` / ``"force_close"``.
        trade_id   : Sequential trade ID from the simulation.
        rr_ratio   : Actual R multiple of the trade.
        opacity    : Box fill opacity (default 0.15).
        """
        d = PositionBox(
            open_time=open_time,
            close_time=close_time,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            direction=direction,
            net_pnl=net_pnl,
            close_reason=close_reason,
            trade_id=trade_id,
            rr_ratio=rr_ratio,
            opacity=opacity,
        )
        self._add_drawing(d)
        return self

    # -----------------------------------------------------------------------
    # Live positions (v1.0.0)
    # -----------------------------------------------------------------------

    def add_live_position(
        self,
        open_time:   int,
        entry_price: float,
        stop_loss:   float,
        direction:   str,
        next_tp:     float | None = None,
        trade_id:    int = -1,
        label:       str = "",
    ) -> str:
        """
        Draw an OPEN (still running) trade whose lines auto-extend to the
        newest candle: dashed entry line, coloured current-SL line (red =
        loss zone, amber = break-even, cyan = profit — v0.7.4 scheme) and,
        when *next_tp* is given, a dashed green next-TP target line.

        Returns the drawing **id** (unlike the other ``add_*`` methods,
        which return the chart) — keep it to stream SL/TP changes with
        :meth:`update_live_position` and, when the trade closes, remove the
        live drawing with :meth:`remove_drawing` before adding the final
        position box.

        Parameters
        ----------
        open_time  : Entry candle time in **Unix seconds** (ms // 1000).
        entry_price: Fill price.
        stop_loss  : Current stop-loss price.
        direction  : ``"long"`` or ``"short"``.
        next_tp    : Next TP target price, or ``None`` when the mode has no
                     TP (trailing / risk-free flows send no TP line).
        trade_id   : Trade ID from the live simulation / trader.
        label      : Optional short label drawn at the entry line.
        """
        if direction not in ("long", "short"):
            raise ValueError(f"direction must be 'long' or 'short', got {direction!r}")
        d = LivePosition(
            open_time=int(open_time),
            entry_price=float(entry_price),
            stop_loss=float(stop_loss),
            direction=direction,
            next_tp=None if next_tp is None else float(next_tp),
            trade_id=trade_id,
            label=label,
        )
        self._add_drawing(d)
        return d.id

    def update_live_position(
        self,
        drawing_id: str,
        stop_loss:  float | None = None,
        next_tp=_UNSET,
        label:      str | None = None,
    ) -> Chart:
        """
        Stream a state change of an open live position to the browser:
        trailing SL move, risk-free jump to break-even, or a new next-TP
        target after a multi-RR level fill.

        The full drawing dict is re-broadcast, so the SL line colour is
        recomputed backend-side from the new SL.  Unknown *drawing_id* is a
        silent no-op (the position may already have been closed/removed).

        Parameters
        ----------
        drawing_id : Id returned by :meth:`add_live_position`.
        stop_loss  : New SL price (``None`` = unchanged).
        next_tp    : New next-TP price; pass ``None`` to **clear** the TP
                     line; omit the argument to leave it unchanged.
        label      : New label (``None`` = unchanged).
        """
        for d in self._drawings:
            if isinstance(d, LivePosition) and d.id == drawing_id:
                if stop_loss is not None:
                    d.stop_loss = float(stop_loss)
                if next_tp is not _UNSET:
                    d.next_tp = None if next_tp is None else float(next_tp)
                if label is not None:
                    d.label = label
                if self._shown:
                    self._server.send({"type": "update_drawing", "drawing": d.to_dict()})
                    self._refresh_init_cache()
                break
        return self

    def navigate_to_candle(self, timestamp_ms: int) -> Chart:
        """
        Scroll and zoom the chart to show the candle at *timestamp_ms*.

        Called by the report module when the user clicks a trade marker
        in the report page.  The browser smoothly scrolls to centre the
        requested candle.

        Parameters
        ----------
        timestamp_ms : int
            UTC millisecond timestamp of the target candle.
        """
        if self._shown:
            self._server.send({
                "type":      "navigate_to_candle",
                "timestamp": timestamp_ms // 1000,  # frontend uses seconds
            })
        return self

    def update_drawing(self, drawing_id: str, **fields) -> Chart:
        """
        Update fields of an existing drawing and push the change to live
        browsers (v1.0.0) — e.g. move a trendline SL segment, retint a box.

        *fields* use the drawing's Python attribute names (``price``,
        ``time2``, ``stop_loss``, ``color``, …); attributes the drawing
        does not have are ignored.  The **full** re-serialised drawing dict
        is broadcast as an ``update_drawing`` message, so derived fields
        (position-box zones, live-position SL colour) stay consistent.
        Unknown *drawing_id* is a silent no-op, mirroring
        :meth:`remove_drawing`.
        """
        for d in self._drawings:
            if getattr(d, "id", None) != drawing_id:
                continue
            if hasattr(d, "_payload"):          # raw strategy-drawing wrapper
                d._payload.update(fields)
            else:
                for key, val in fields.items():
                    if hasattr(d, key):
                        setattr(d, key, val)
            if self._shown:
                self._server.send({"type": "update_drawing", "drawing": d.to_dict()})
                self._refresh_init_cache()
            break
        return self

    def remove_drawing(self, drawing_id: str) -> Chart:
        self._drawings = [d for d in self._drawings if d.id != drawing_id]
        if self._shown:
            self._server.send({"type": "remove_drawing", "id": drawing_id})
            self._refresh_init_cache()
        return self

    def clear_drawings(self) -> Chart:
        self._drawings.clear()
        if self._shown:
            self._server.send({"type": "clear_drawings"})
            self._refresh_init_cache()
        return self

    # -----------------------------------------------------------------------
    # Live streaming
    # -----------------------------------------------------------------------

    def stream(self, bar) -> Chart:
        """
        Push a new (or updated) bar in real-time.

        *bar* can be a ``dict``, a ``Bar`` dataclass, or a pandas ``Series``.
        The ``time`` field must be in Unix seconds.

        For AlgoTradeKit exchange data (milliseconds), use ``stream_from_atk()``
        instead.
        """
        if isinstance(bar, pd.Series):
            bar = bar.to_dict()
        elif hasattr(bar, "to_dict"):
            bar = bar.to_dict()

        bar = {k.lower(): v for k, v in bar.items()}
        bar.setdefault("volume", 0.0)

        # Timeframe conversion (v1.1.0): a streamed bar is a *source* bar.
        # Fold it into the source frame and hand the browser the display
        # candle it belongs to — which is usually still forming.
        if self._display_tf is not None:
            bar = self._fold_source_bar(bar)

        if self.chart_type == "heikinashi" and self._bars:
            prev     = self._bars[-1]
            ha_close = (bar["open"] + bar["high"] + bar["low"] + bar["close"]) / 4
            ha_open  = (prev["open"] + prev["close"]) / 2
            bar = {
                "time":   bar["time"],
                "open":   ha_open,
                "high":   max(bar["high"], ha_open, ha_close),
                "low":    min(bar["low"],  ha_open, ha_close),
                "close":  ha_close,
                "volume": bar["volume"],
            }

        # Update in-memory list (update current bar or append new one)
        if self._bars and self._bars[-1]["time"] == bar["time"]:
            self._bars[-1] = bar
        else:
            self._bars.append(bar)

        if self._shown:
            self._server.send({"type": "stream", "bar": bar})

        # Rolling window (v1.0.0): the frontend enforces its own limit on
        # every streamed bar; mirror it here so Python memory stays bounded
        # and the init replay matches what an open page shows.
        if self._enforce_candle_limit() and self._shown:
            self._refresh_init_cache()

        return self

    def stream_from_atk(self, candle: dict) -> Chart:
        """
        Stream a live candle from an AlgoTradeKit exchange source.

        Accepts a dict with either:
        * ``timestamp`` (ms) — AlgoTradeKit / exchange WebSocket format
        * ``time`` (seconds) — standard lightweight-charts format

        This is the method the future ``trade`` module will call when
        forwarding live ticks from a Binance / MEXC WebSocket feed.

        Example
        -------
        ::

            # Future trade module usage:
            for candle in exchange.live_feed("BTCUSDT", "1m"):
                chart.stream_from_atk(candle)
        """
        candle = {k.lower(): v for k, v in candle.items()}
        if "timestamp" in candle and "time" not in candle:
            candle["time"] = int(candle.pop("timestamp")) // 1000
        return self.stream(candle)

    def _fold_source_bar(self, bar: dict) -> dict:
        """
        Add a source-timeframe *bar* to the source frame and return the display
        candle that now covers it (v1.1.0).

        The returned bar carries the bucket's open time, so the browser's
        ``series.update()`` grows the forming candle and only starts a new one
        on a real boundary.
        """
        tf_ms = self._display_tf_ms()
        if tf_ms is None:
            return bar

        ts_ms = int(bar["time"]) * 1000
        row = {
            "timestamp": ts_ms,
            "open":   float(bar["open"]),
            "high":   float(bar["high"]),
            "low":    float(bar["low"]),
            "close":  float(bar["close"]),
            "volume": float(bar.get("volume", 0.0) or 0.0),
        }

        src = self._source_df
        if not src.empty and int(src.iloc[-1]["timestamp"]) == ts_ms:
            for key, val in row.items():                 # same candle, revised
                src.iloc[-1, src.columns.get_loc(key)] = val
        else:
            self._source_df = pd.concat(
                [src, pd.DataFrame([row])], ignore_index=True
            ) if not src.empty else pd.DataFrame([row])
            src = self._source_df

        # Aggregate every source row inside this display bucket
        bucket_ms = (ts_ms // tf_ms) * tf_ms
        window    = src[src["timestamp"] >= bucket_ms]
        return {
            "time":   bucket_ms // 1000,
            "open":   float(window.iloc[0]["open"]),
            "high":   float(window["high"].max()),
            "low":    float(window["low"].min()),
            "close":  float(window.iloc[-1]["close"]),
            "volume": float(window["volume"].sum()) if "volume" in window else 0.0,
        }

    def stream_indicator(self, name: str, time: int, value: float) -> Chart:
        """
        Push a single new indicator point without recalculating from scratch.

        Call this from your live-feed callback alongside ``stream()``:

        ::

            c.stream(bar)
            c.stream_indicator("EMA 20", bar["time"], new_ema_value)
            c.stream_indicator("RSI 14", bar["time"], new_rsi_value)

        The named indicator must have been previously added via ``add_indicator()``.
        """
        if self._shown:
            self._server.send({
                "type":  "stream_indicator",
                "name":  name,
                "point": {"time": int(time), "value": float(value)},
            })
        return self

    # -----------------------------------------------------------------------
    # Rolling candle window (v1.0.0)
    # -----------------------------------------------------------------------

    def set_candle_limit(self, limit: int | None) -> Chart:
        """
        Cap the chart at the last *limit* candles (rolling window).

        Effective immediately and on every subsequently streamed bar, on
        both sides:

        * **Python** — ``_bars`` is trimmed in place and drawings whose
          whole time range fell out of the window are dropped, so the init
          replayed to a refreshed page matches the live view.
        * **Browser** — a ``set_candle_limit`` message arms the frontend,
          which trims candles, indicator points, ichimoku cloud points and
          expired drawings now and after each streamed bar.

        Timeless drawings (horizontal lines) and open live positions are
        never dropped.  ``None`` disables the window.
        """
        if limit is not None:
            limit = int(limit)
            if limit <= 0:
                raise ValueError(f"candle_count_limit must be a positive integer, got {limit}")
        self._candle_count_limit = limit
        self._enforce_candle_limit()
        if self._shown:
            self._server.send({"type": "set_candle_limit", "limit": limit})
            self._refresh_init_cache()
        return self

    def _enforce_candle_limit(self) -> bool:
        """Trim Python-side state to the rolling window.  True if trimmed.

        ``_bars`` is shrunk **in place** — the server's cached init holds a
        reference to the same list, so it stays current without a rebuild.
        """
        limit = self._candle_count_limit
        if not limit or len(self._bars) <= limit:
            return False
        del self._bars[: len(self._bars) - limit]
        cutoff = self._bars[0]["time"]
        kept = [
            d for d in self._drawings
            if not self._drawing_expired(d, cutoff)
        ]
        if len(kept) != len(self._drawings):
            self._drawings[:] = kept
        return True

    @classmethod
    def _drawing_expired(cls, drawing, cutoff: int) -> bool:
        """A drawing expires when its whole time range is before *cutoff*."""
        end = cls._drawing_end_time(drawing)
        return end is not None and end < cutoff

    @staticmethod
    def _drawing_end_time(drawing) -> int | None:
        """
        Latest chart time (Unix seconds) a drawing occupies, or ``None``
        for drawings that must never be window-trimmed: timeless ones
        (``hline``) and open live positions (they extend to "now").

        Works for both dataclass drawings and raw strategy-drawing dicts —
        all types share the ``time`` / ``time1``/``time2`` / ``close_time``
        field vocabulary.
        """
        payload = drawing.to_dict() if hasattr(drawing, "to_dict") else dict(drawing)
        if payload.get("type") == "live_position":
            return None
        if payload.get("close_time") is not None:
            return payload["close_time"]
        t1, t2 = payload.get("time1"), payload.get("time2")
        times = [t for t in (t1, t2) if t is not None]
        if times:
            return max(times)
        if payload.get("time") is not None:
            return payload["time"]
        return None

    # -----------------------------------------------------------------------
    # Browser → Python message handling
    # -----------------------------------------------------------------------

    def _handle_browser_message(self, msg: dict) -> None:
        """Handle messages sent from the browser (drawing edits, indicator removal)."""
        msg_type = msg.get("type")

        if msg_type == "drawing_updated":
            d   = msg.get("drawing", {})
            did = d.get("id")
            if did:
                for drawing in self._drawings:
                    if drawing.id == did:
                        for key, val in d.items():
                            if hasattr(drawing, key):
                                setattr(drawing, key, val)
                        break
                self._refresh_init_cache()

        elif msg_type == "drawing_deleted":
            did = msg.get("id")
            if did:
                self._drawings = [dr for dr in self._drawings if dr.id != did]
                self._refresh_init_cache()

        elif msg_type == "drawing_lock_changed":
            did   = msg.get("id")
            state = msg.get("locked")
            if did is not None and state is not None:
                for drawing in self._drawings:
                    if drawing.id == did:
                        drawing.locked = state
                        break
                self._refresh_init_cache()

        elif msg_type == "remove_indicator":
            name = msg.get("name")
            if name:
                self._indicators = [i for i in self._indicators if i.name != name]
                if callable(self.on_indicator_removed):
                    self.on_indicator_removed(name)
                self._refresh_init_cache()

        elif msg_type == "compute_indicator":
            params = msg.get("params", {})
            self.add_indicator_spec(params)

        elif msg_type == "replay_arm":
            # v1.1.0 — bar replay wants the *source* candles so it can build a
            # forming candle inside the displayed one.  Sent once, on arming:
            # every step after that is client-side, which matters when the
            # browser is on the other end of an SSH tunnel.
            self._send_replay_data()

        elif msg_type == "set_timeframe":
            # v1.1.0 — the toolbar selector. Resampling happens here, never in
            # the browser; a bad value is reported instead of crashing the loop.
            try:
                self.set_timeframe(msg.get("tf"))
            except ValueError as exc:
                self._server.send({"type": "timeframe_error", "message": str(exc)})

    # -----------------------------------------------------------------------
    # Bar replay support (v1.1.0)
    # -----------------------------------------------------------------------

    def source_bars(self) -> list[dict]:
        """
        The data at its **source** timeframe, in the browser's bar shape
        (``time`` in Unix seconds).

        Bar replay uses these to build the candle that is still forming inside
        the displayed timeframe: with 1m data shown at 5m, a cursor three
        minutes into a bucket shows a 5m candle made of those three minutes.
        Returns ``[]`` when the display is already the source timeframe --
        there is nothing finer to show.
        """
        if self._display_tf is None or self._source_df.empty:
            return []
        return [
            {
                "time":   int(row.timestamp) // 1000,
                "open":   float(row.open),
                "high":   float(row.high),
                "low":    float(row.low),
                "close":  float(row.close),
                "volume": float(getattr(row, "volume", 0.0) or 0.0),
            }
            for row in self._source_df.itertuples(index=False)
        ]

    def _send_replay_data(self) -> None:
        """Answer a browser's ``replay_arm`` with the source candles."""
        from ..data._utils import TIMEFRAME_MS

        step_ms = TIMEFRAME_MS.get(self._source_tf or "") or 0
        self._server.send({
            "type":            "replay_data",
            "sourceTimeframe": self._source_tf,
            "stepSeconds":     step_ms // 1000,
            "sourceBars":      self.source_bars(),
        })

    # -----------------------------------------------------------------------
    # On-demand indicator computation (v0.7.4)
    # -----------------------------------------------------------------------

    def add_indicator_spec(self, params: dict) -> dict | None:
        """
        Compute an indicator from the stored OHLCV data **server-side** and add
        it to the chart, tagging every resulting series with the resolved spec
        that produced it.

        This single entry point is used by both:

        * the browser toolbar — a ``compute_indicator`` WebSocket message
          (live add, or live edit, which removes the old group then re-adds);
        * ``SimulateConfig.chart_indicators`` — indicators pre-loaded before
          the chart is shown so they appear automatically.

        All maths run here (never in the browser).  The tagged ``spec`` lets the
        front-end's per-indicator gear icon re-open the panel pre-filled and
        recompute through this same path.

        Parameters accepted in *params*
        --------------------------------
        kind : str
            One of ``"sma"``, ``"ema"``, ``"wma"``, ``"smma"``, ``"dema"``,
            ``"tema"``, ``"hma"``, ``"vwma"``, ``"vwap"``,
            ``"rsi"``, ``"macd"``, ``"atr"``, ``"ichimoku"``.
        period : int      (MA, RSI, ATR)
        source : str      ``"close"`` | ``"open"`` | ``"high"`` | ``"low"``  (MA, RSI)
        fast / slow / signal_period : int   (MACD; defaults 12 / 26 / 9)
        ma_type : str     MA on RSI (``"none"`` | ``"sma"`` | ``"ema"``)
        ma_period : int   MA period on RSI (default 14)
        tenkan / kijun / senkou_b / displacement : int  (Ichimoku; 9/26/52/26)
        color   : str     Hex colour override for the first series

        Returns
        -------
        dict | None
            The resolved spec that was tagged onto the new series, or ``None``
            if nothing could be computed (empty data / unknown kind).
        """
        if self.df.empty:
            return None

        start = len(self._indicators)
        spec  = self._compute_indicator_series(params)
        if spec is None:
            return None

        # Tag every freshly-added series with the resolved spec so the browser
        # can map group → spec for the edit (gear) flow.
        for ind in self._indicators[start:]:
            if hasattr(ind, "payload"):
                ind.payload["spec"] = spec

        # Push to a live browser, and keep the replay cache in sync so a page
        # refresh still shows (and can still edit) the indicator.
        self._broadcast_last_indicators(0)
        self._refresh_init_cache()
        return spec

    # Backwards-compatible alias (pre-0.8.0 name).
    def _compute_and_send_indicator(self, params: dict) -> None:
        self.add_indicator_spec(params)

    def _compute_indicator_series(self, params: dict) -> dict | None:
        """
        Run the indicator maths for *params* and ``_push`` the resulting
        series onto ``self._indicators``.  Returns the resolved spec (with all
        defaults filled in) or ``None``.  Does **not** broadcast.
        """
        kind  = params.get("kind", "ema").lower()
        color = params.get("color") or None  # None → use indicator default

        from .indicator_renderer import (
            _next_pane,
            _push,
            _series_to_tv,
            add_ichimoku,
            add_ma,
            add_macd,
            add_rsi,
        )

        # Stored OHLCV uses 'timestamp' (ms); fall back to 'time' if present.
        if "timestamp" in self.df.columns:
            ts = self.df["timestamp"]
        elif "time" in self.df.columns:
            ts = self.df["time"]
        else:
            return None

        # ── Moving Averages ─────────────────────────────────────────────
        _MA_KINDS = {"sma", "ema", "wma", "smma", "dema", "tema", "hma", "vwma", "vwap"}
        if kind in _MA_KINDS:
            period  = int(params.get("period", 20))
            src_col = params.get("source", "close")
            src     = self.df[src_col] if src_col in self.df.columns else self.df["close"]
            ma = self._build_ma(kind, src, period)
            if ma is None:
                return None
            add_ma(self, ma, timestamps=ts, color=color)
            return {"kind": kind, "period": period, "source": src_col, "color": color}

        # ── RSI ─────────────────────────────────────────────────────────
        if kind == "rsi":
            from AlgoTradeKit.indicator.rsi import RSI as _RSI
            period    = int(params.get("period", 14))
            src_col   = params.get("source", "close")
            ma_type   = params.get("ma_type", "none")
            ma_period = int(params.get("ma_period", 14))
            src = self.df[src_col] if src_col in self.df.columns else self.df["close"]
            show_ma = ma_type != "none"
            rsi = _RSI(
                src, period,
                show_ma=show_ma,
                ma_type=(ma_type if show_ma else "EMA"),  # RSI upper-cases this
                ma_length=ma_period,
            )
            add_rsi(self, rsi, timestamps=ts)
            return {"kind": "rsi", "period": period, "source": src_col,
                    "ma_type": ma_type, "ma_period": ma_period, "color": color}

        # ── MACD ────────────────────────────────────────────────────────
        if kind == "macd":
            from AlgoTradeKit.indicator.macd import MACD as _MACD
            fast   = int(params.get("fast",   12))
            slow   = int(params.get("slow",   26))
            signal = int(params.get("signal_period", 9))
            macd = _MACD(self.df["close"], fast, slow, signal)
            add_macd(self, macd, timestamps=ts)
            return {"kind": "macd", "fast": fast, "slow": slow,
                    "signal_period": signal, "color": color}

        # ── ATR (custom, no third-party TA lib) ─────────────────────────
        if kind == "atr":
            period  = int(params.get("period", 14))
            atr_ser = self._compute_atr(period)
            if atr_ser is None:
                return None
            pane  = _next_pane(self)
            label = f"ATR({period})"
            _push(self, label, {
                "name":       label,
                "data":       _series_to_tv(atr_ser, ts),
                "color":      color or "#ff9800",
                "overlay":    False,
                "pane":       pane,
                "lineWidth":  1,
                "seriesType": "line",
                "group":      label,
            })
            return {"kind": "atr", "period": period, "color": color}

        # ── Ichimoku ────────────────────────────────────────────────────
        if kind == "ichimoku":
            from AlgoTradeKit.indicator.ichimoku import Ichimoku as _Ichi
            tenkan       = int(params.get("tenkan",   9))
            kijun        = int(params.get("kijun",   26))
            senkou_b     = int(params.get("senkou_b", 52))
            displacement = int(params.get("displacement", 26))
            # Ichimoku signature is (high, low, close) — order matters.
            ichi = _Ichi(
                self.df["high"], self.df["low"], self.df["close"],
                tenkan_period=tenkan, kijun_period=kijun,
                senkou_b_period=senkou_b, displacement=displacement,
            )
            add_ichimoku(self, ichi, timestamps=ts)
            return {"kind": "ichimoku", "tenkan": tenkan, "kijun": kijun,
                    "senkou_b": senkou_b, "displacement": displacement, "color": color}

        return None

    # ------------------------------------------------------------------
    # Indicator computation helpers
    # ------------------------------------------------------------------

    def _build_ma(self, kind: str, src, period: int):
        """Instantiate the right MA class from the indicator module."""
        import pandas as pd

        from AlgoTradeKit.indicator.ma import (
            DEMA,
            EMA,
            SMA,
            SMMA,
            TEMA,
            VWAP,
            VWMA,
            WMA,
            HullMA,
        )
        _MAP = {
            "sma":  lambda: SMA(src, period),
            "ema":  lambda: EMA(src, period),
            "wma":  lambda: WMA(src, period),
            "smma": lambda: SMMA(src, period),
            "dema": lambda: DEMA(src, period),
            "tema": lambda: TEMA(src, period),
            "hma":  lambda: HullMA(src, period),
            "vwma": lambda: VWMA(
                source=src, length=period,
                volume=self.df.get("volume", pd.Series([1.0] * len(src))),
            ),
            "vwap": lambda: VWAP(
                high=self.df.get("high", src),
                low=self.df.get("low", src),
                close=src,
                volume=self.df.get("volume", pd.Series([1.0] * len(src))),
            ),
        }
        factory = _MAP.get(kind)
        return factory() if factory else None

    def _compute_atr(self, period: int):
        """
        Compute Average True Range (pure pandas, no third-party TA lib).

        True Range = max(high-low, |high-prev_close|, |low-prev_close|)
        ATR        = Wilder smoothed average of TR over *period* bars.
        """
        import pandas as pd
        df = self.df
        if not {"high", "low", "close"}.issubset(df.columns):
            return None

        high  = df["high"].astype(float)
        low   = df["low"].astype(float)
        close = df["close"].astype(float)
        prev  = close.shift(1)

        tr = pd.concat([
            high - low,
            (high - prev).abs(),
            (low  - prev).abs(),
        ], axis=1).max(axis=1)

        # Wilder smoothing (equivalent to EMA with alpha = 1/period)
        atr = pd.Series(index=tr.index, dtype=float)
        alpha = 1.0 / period
        atr_val = float("nan")
        for i, v in enumerate(tr):
            if i < period - 1:
                atr.iloc[i] = float("nan")
            elif i == period - 1:
                atr_val = float(tr.iloc[:period].mean())
                atr.iloc[i] = atr_val
            else:
                atr_val = atr_val * (1 - alpha) + float(v) * alpha
                atr.iloc[i] = atr_val
        return atr

    def _broadcast_last_indicators(self, up_to_pane: int) -> None:
        """
        Send the most recently added indicator(s) to the browser.

        We track the count before/after via a sentinel — the helpers above
        always append to ``self._indicators``.  We simply send everything
        that was added since the last call to this method.
        """
        if not self._shown:
            return
        # Find newly added: anything without a "_sent" mark
        for ind in self._indicators:
            if not getattr(ind, "_sent_to_browser", False):
                payload = ind.to_dict() if hasattr(ind, "to_dict") else ind.payload
                self._server.send({"type": "add_indicator", "indicator": payload})
                ind._sent_to_browser = True  # type: ignore[attr-defined]

    # -----------------------------------------------------------------------
    # Layout persistence
    # -----------------------------------------------------------------------

    def save_layout(self, path: str) -> Chart:
        """Save the current chart configuration (indicators + drawings) to JSON."""
        layout = {
            "title":      self.title,
            "chart_type": self.chart_type,
            "theme":      self.theme,
            "indicators": [i.to_dict() for i in self._indicators],
            "drawings":   [d.to_dict() for d in self._drawings],
        }
        Path(path).write_text(json.dumps(layout, indent=2), encoding="utf-8")
        return self

    def load_layout(self, path: str) -> Chart:
        """Restore chart configuration from a JSON file saved by ``save_layout()``."""
        layout = json.loads(Path(path).read_text(encoding="utf-8"))

        self.title      = layout.get("title",      self.title)
        self.chart_type = layout.get("chart_type", self.chart_type)
        self.theme      = layout.get("theme",       self.theme)

        TYPE_MAP = {
            "hline":     HorizontalLine,
            "trendline": TrendLine,
            "box":       Box,
            "signal":    Signal,
            "text":      TextLabel,
            "fib":       FibRetracement,
        }

        self._drawings.clear()
        for d in layout.get("drawings", []):
            cls = TYPE_MAP.get(d["type"])
            if cls:
                obj = cls.__new__(cls)
                obj.__dict__.update(
                    {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
                )
                self._drawings.append(obj)

        self._indicators.clear()
        for ind in layout.get("indicators", []):
            self._indicators.append(IndicatorSeries(
                name=ind["name"],  data=ind["data"],
                color=ind["color"], overlay=ind["overlay"],
                pane=ind["pane"],  line_width=ind["lineWidth"],
                series_type=ind["seriesType"],
            ))

        if self._shown:
            self._send_init()

        return self

    # -----------------------------------------------------------------------
    # Show / stop
    # -----------------------------------------------------------------------

    def show(
        self,
        open_browser: bool = True,
        block:        bool = False,
    ) -> Chart:
        """
        Start the local server and optionally open the browser.

        Parameters
        ----------
        open_browser : bool
            Open a browser tab automatically.  Set to ``False`` in tests.
        block : bool
            Block the calling thread (Ctrl+C to stop).  Useful for scripts.
        """
        if not self._shown:
            self._server.start(open_browser=open_browser)
            self._shown = True
            time.sleep(0.8)     # let the browser connect before pushing data
            self._send_init()

        if block:
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass

        return self

    def stop(self) -> None:
        """Stop the server and close the browser connection."""
        self._server.stop()

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _build_init_payload(self) -> dict:
        # Mark every indicator as already delivered so a later *live* add only
        # broadcasts genuinely new series (prevents duplicates when indicators
        # were pre-loaded via SimulateConfig.chart_indicators before show()).
        for ind in self._indicators:
            ind._sent_to_browser = True  # type: ignore[attr-defined]
        return {
            "type":             "init",
            "title":            self.title,
            "chartType":        self.chart_type,
            "theme":            self.theme,
            "volumeInMain":     self.volume_in_main,
            "candleCountLimit": self._candle_count_limit,
            "sourceTimeframe":  self._source_tf,
            "displayTimeframe": self._display_tf,
            "timeframes":       list(self._timeframes),
            "bars":             self._bars,
            "indicators":       [i.to_dict() for i in self._indicators],
            "drawings":         [d.to_dict() for d in self._drawings],
        }

    def _send_init(self) -> None:
        self._server.send(self._build_init_payload())

    def _refresh_init_cache(self) -> None:
        """Keep the server's replay cache current so a browser refresh still
        shows (and can still edit) indicators added after the initial init."""
        if self._shown and self._server._last_init is not None:
            self._server._last_init = self._build_init_payload()

    @property
    def url(self) -> str:
        """Browsable URL of the chart server.

        Uses the configured host; a ``0.0.0.0`` / ``::`` bind address is
        substituted with ``127.0.0.1`` (bind-to-all is not a destination).
        """
        return self._server.url

    def __repr__(self) -> str:
        return (
            f"<Chart title={self.title!r} "
            f"bars={len(self._bars)} "
            f"indicators={len(self._indicators)} "
            f"drawings={len(self._drawings)}>"
        )
