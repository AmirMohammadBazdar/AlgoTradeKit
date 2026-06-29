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
from typing import Literal, Optional

import pandas as pd

from .models import (
    Bar, Box, FibRetracement, HorizontalLine,
    IndicatorSeries, PositionBox, Signal, TextLabel, TrendLine,
)
from .server import ChartServer


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
    """

    def __init__(
        self,
        title:          str  = "Chart",
        chart_type:     Literal["candlestick", "heikinashi"] = "candlestick",
        port:           int  = 0,
        theme:          Literal["dark", "light"] = "dark",
        volume_in_main: bool = True,
    ) -> None:
        self.title          = title
        self.chart_type     = chart_type
        self.theme          = theme
        self.volume_in_main = volume_in_main

        self._bars:       list[dict]          = []
        self._indicators: list[IndicatorSeries] = []
        self._drawings:   list                = []
        self.df:          pd.DataFrame        = pd.DataFrame()  # raw OHLCV kept for indicator use

        self._server = ChartServer(title=title, port=port)
        self._server.on_message = self._handle_browser_message
        self._shown = False

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
        candle_range:  "dict | None" = None,
    ) -> "Chart":
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
        )
        chart.set_data(df, candle_range=candle_range)
        return chart

    # -----------------------------------------------------------------------
    # Data
    # -----------------------------------------------------------------------

    def set_data(
        self,
        df:         pd.DataFrame,
        chart_type: Optional[str] = None,
        candle_range: "dict | None" = None,
    ) -> "Chart":
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
        self.df = df.copy()

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

        if chart_type is not None:
            self.chart_type = chart_type

        if self.chart_type == "heikinashi":
            df = self._to_heikinashi(df)

        required = {"open", "high", "low", "close"}
        missing  = required - set(df.columns)
        if missing:
            raise ValueError(f"DataFrame missing required columns: {missing}")

        # ── Candle range filter (v0.7.2) ──────────────────────────────────
        # Applied *after* time is in Unix seconds so start/end comparisons
        # work uniformly regardless of the original timestamp format.
        if candle_range is not None:
            df = self._apply_candle_range(df, candle_range)
        # ─────────────────────────────────────────────────────────────────

        self._bars = [
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

        if self._shown:
            self._send_init()

        return self

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
        from datetime import datetime, timezone as _tz

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
    ) -> "Chart":
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
    ) -> "Chart":
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

    def add_hline(
        self,
        price:      float,
        color:      str = "#ef5350",
        line_width: int = 1,
        line_style: int = 0,
        label:      str = "",
    ) -> "Chart":
        d = HorizontalLine(
            price=price, color=color,
            line_width=line_width, line_style=line_style, label=label,
        )
        self._drawings.append(d)
        if self._shown:
            self._server.send({"type": "add_drawing", "drawing": d.to_dict()})
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
    ) -> "Chart":
        d = TrendLine(
            time1=time1, price1=price1, time2=time2, price2=price2,
            color=color, line_width=line_width, extend=extend, label=label,
        )
        self._drawings.append(d)
        if self._shown:
            self._server.send({"type": "add_drawing", "drawing": d.to_dict()})
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
    ) -> "Chart":
        d = Box(
            time1=time1, price1=price1, time2=time2, price2=price2,
            color=color, opacity=opacity, border_color=border_color, label=label,
        )
        self._drawings.append(d)
        if self._shown:
            self._server.send({"type": "add_drawing", "drawing": d.to_dict()})
        return self

    def add_signal(
        self,
        time:  int,
        side:  Literal["buy", "sell"],
        price: Optional[float] = None,
        label: str = "",
        color: str = "",
    ) -> "Chart":
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
        self._drawings.append(d)
        if self._shown:
            self._server.send({"type": "add_drawing", "drawing": d.to_dict()})
        return self

    def add_text(
        self,
        time:      int,
        price:     float,
        text:      str,
        color:     str = "#ffffff",
        font_size: int = 12,
    ) -> "Chart":
        d = TextLabel(time=time, price=price, text=text, color=color, font_size=font_size)
        self._drawings.append(d)
        if self._shown:
            self._server.send({"type": "add_drawing", "drawing": d.to_dict()})
        return self

    def add_fib(
        self,
        time1:  int,
        price1: float,
        time2:  int,
        price2: float,
        color:  str  = "#9c27b0",
        levels: Optional[list] = None,
        label:  str  = "",
    ) -> "Chart":
        d = FibRetracement(
            time1=time1, price1=price1, time2=time2, price2=price2,
            color=color,
            levels=levels or [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0],
            label=label,
        )
        self._drawings.append(d)
        if self._shown:
            self._server.send({"type": "add_drawing", "drawing": d.to_dict()})
        return self

    def add_position_box(
        self,
        open_time:    int,
        close_time:   int,
        entry_price:  float,
        stop_loss:    float,
        take_profit:  Optional[float],
        direction:    str,
        net_pnl:      float,
        close_reason: str = "",
        trade_id:     int  = -1,
        rr_ratio:     float = 0.0,
        opacity:      float = 0.15,
    ) -> "Chart":
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
        self._drawings.append(d)
        if self._shown:
            self._server.send({"type": "add_drawing", "drawing": d.to_dict()})
        return self

    def navigate_to_candle(self, timestamp_ms: int) -> "Chart":
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

    def remove_drawing(self, drawing_id: str) -> "Chart":
        self._drawings = [d for d in self._drawings if d.id != drawing_id]
        if self._shown:
            self._server.send({"type": "remove_drawing", "id": drawing_id})
        return self

    def clear_drawings(self) -> "Chart":
        self._drawings.clear()
        if self._shown:
            self._server.send({"type": "clear_drawings"})
        return self

    # -----------------------------------------------------------------------
    # Live streaming
    # -----------------------------------------------------------------------

    def stream(self, bar) -> "Chart":
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

        return self

    def stream_from_atk(self, candle: dict) -> "Chart":
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

    def stream_indicator(self, name: str, time: int, value: float) -> "Chart":
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

        elif msg_type == "drawing_deleted":
            did = msg.get("id")
            if did:
                self._drawings = [dr for dr in self._drawings if dr.id != did]

        elif msg_type == "drawing_lock_changed":
            did   = msg.get("id")
            state = msg.get("locked")
            if did is not None and state is not None:
                for drawing in self._drawings:
                    if drawing.id == did:
                        drawing.locked = state
                        break

        elif msg_type == "remove_indicator":
            name = msg.get("name")
            if name:
                self._indicators = [i for i in self._indicators if i.name != name]
                if callable(self.on_indicator_removed):
                    self.on_indicator_removed(name)

        elif msg_type == "compute_indicator":
            params = msg.get("params", {})
            self.add_indicator_spec(params)

    # -----------------------------------------------------------------------
    # On-demand indicator computation (v0.7.4)
    # -----------------------------------------------------------------------

    def add_indicator_spec(self, params: dict) -> "dict | None":
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

    def _compute_indicator_series(self, params: dict) -> "dict | None":
        """
        Run the indicator maths for *params* and ``_push`` the resulting
        series onto ``self._indicators``.  Returns the resolved spec (with all
        defaults filled in) or ``None``.  Does **not** broadcast.
        """
        kind  = params.get("kind", "ema").lower()
        color = params.get("color") or None  # None → use indicator default

        from .indicator_renderer import (
            add_ma, add_rsi, add_macd, add_ichimoku, _next_pane, _push,
            _series_to_tv,
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
        from AlgoTradeKit.indicator.ma import (
            SMA, EMA, WMA, SMMA, DEMA, TEMA, HullMA, VWMA, VWAP,
        )
        import pandas as pd
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

    def save_layout(self, path: str) -> "Chart":
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

    def load_layout(self, path: str) -> "Chart":
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
    ) -> "Chart":
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
            "type":         "init",
            "title":        self.title,
            "chartType":    self.chart_type,
            "theme":        self.theme,
            "volumeInMain": self.volume_in_main,
            "bars":         self._bars,
            "indicators":   [i.to_dict() for i in self._indicators],
            "drawings":     [d.to_dict() for d in self._drawings],
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
        """The local URL of the chart server."""
        return f"http://127.0.0.1:{self._server.port}"

    def __repr__(self) -> str:
        return (
            f"<Chart title={self.title!r} "
            f"bars={len(self._bars)} "
            f"indicators={len(self._indicators)} "
            f"drawings={len(self._drawings)}>"
        )
