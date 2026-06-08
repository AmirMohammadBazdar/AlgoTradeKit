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
    IndicatorSeries, Signal, TextLabel, TrendLine,
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

        Example
        -------
        ::

            # Load a Collector CSV directly — no extra code needed
            c = Chart.from_csv("data/binance-futures_BTCUSDT_1d.csv")
            c.show(block=True)
        """
        import os
        df = pd.read_csv(path)
        if not title:
            title = os.path.basename(path)
        chart = cls(
            title=title, chart_type=chart_type,
            theme=theme, port=port, volume_in_main=volume_in_main,
        )
        chart.set_data(df)
        return chart

    # -----------------------------------------------------------------------
    # Data
    # -----------------------------------------------------------------------

    def set_data(
        self,
        df:         pd.DataFrame,
        chart_type: Optional[str] = None,
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

    def _send_init(self) -> None:
        self._server.send({
            "type":         "init",
            "title":        self.title,
            "chartType":    self.chart_type,
            "theme":        self.theme,
            "volumeInMain": self.volume_in_main,
            "bars":         self._bars,
            "indicators":   [i.to_dict() for i in self._indicators],
            "drawings":     [d.to_dict() for d in self._drawings],
        })

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
