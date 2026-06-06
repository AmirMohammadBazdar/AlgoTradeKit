"""
algotradekit.visual.models
~~~~~~~~~~~~~~~~~~~~~~~~~~
Data-classes for all chart objects: candles, indicators, and drawings.

Every class has a ``to_dict()`` method that serialises it into the JSON
format the frontend (lightweight-charts) understands.
"""

from dataclasses import dataclass, field, asdict
from typing import Optional, Literal
import itertools

_id_counter = itertools.count(1)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{next(_id_counter)}"


# ---------------------------------------------------------------------------
# Candle / OHLCV
# ---------------------------------------------------------------------------

@dataclass
class Bar:
    """A single OHLCV candle."""
    time:   int
    open:   float
    high:   float
    low:    float
    close:  float
    volume: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Indicator overlay / sub-pane
# ---------------------------------------------------------------------------

@dataclass
class IndicatorSeries:
    """A line, histogram, or area series drawn over or below the chart."""
    name:        str
    data:        list
    color:       str   = "#f0c040"
    overlay:     bool  = True
    pane:        int   = 1
    line_width:  int   = 1
    series_type: Literal["line", "histogram", "area"] = "line"

    def to_dict(self) -> dict:
        return {
            "name":       self.name,
            "data":       self.data,
            "color":      self.color,
            "overlay":    self.overlay,
            "pane":       self.pane,
            "lineWidth":  self.line_width,
            "seriesType": self.series_type,
        }


# ---------------------------------------------------------------------------
# Drawings
# ---------------------------------------------------------------------------

@dataclass
class HorizontalLine:
    """A horizontal price level line."""
    price:       float
    color:       str = "#ef5350"
    line_width:  int = 1
    line_style:  int = 0          # 0=solid 1=dotted 2=dashed
    label:       str = ""
    locked:      bool = True      # server-created lines are locked by default
    id:          str = field(default_factory=lambda: _new_id("hline"))

    def to_dict(self) -> dict:
        return {
            "type":      "hline",
            "price":     self.price,
            "color":     self.color,
            "lineWidth": self.line_width,
            "lineStyle": self.line_style,
            "label":     self.label,
            "id":        self.id,
            "locked":    self.locked,
            "source":    "server",
        }


@dataclass
class TrendLine:
    """A line segment between two price/time points."""
    time1:      int
    price1:     float
    time2:      int
    price2:     float
    color:      str  = "#2196f3"
    line_width: int  = 1
    extend:     bool = False
    label:      str  = ""
    locked:     bool = True
    id:         str  = field(default_factory=lambda: _new_id("tline"))

    def to_dict(self) -> dict:
        return {
            "type":      "trendline",
            "time1":     self.time1,
            "price1":    self.price1,
            "time2":     self.time2,
            "price2":    self.price2,
            "color":     self.color,
            "lineWidth": self.line_width,
            "extend":    self.extend,
            "label":     self.label,
            "id":        self.id,
            "locked":    self.locked,
            "source":    "server",
        }


@dataclass
class Box:
    """A filled rectangle between two price/time corners."""
    time1:        int
    price1:       float
    time2:        int
    price2:       float
    color:        str   = "#26a69a"
    opacity:      float = 0.2
    border_color: str   = "#26a69a"
    label:        str   = ""
    locked:       bool  = True
    id:           str   = field(default_factory=lambda: _new_id("box"))

    def to_dict(self) -> dict:
        return {
            "type":        "box",
            "time1":       self.time1,
            "price1":      self.price1,
            "time2":       self.time2,
            "price2":      self.price2,
            "color":       self.color,
            "opacity":     self.opacity,
            "borderColor": self.border_color,
            "label":       self.label,
            "id":          self.id,
            "locked":      self.locked,
            "source":      "server",
        }


@dataclass
class Signal:
    """
    A buy or sell marker on the chart.

    Used by the future ``strategy`` module to visualise entry/exit points.
    """
    time:   int
    side:   Literal["buy", "sell"]
    price:  Optional[float] = None
    label:  str  = ""
    color:  str  = ""            # auto-coloured if empty
    locked: bool = True
    id:     str  = field(default_factory=lambda: _new_id("sig"))

    def to_dict(self) -> dict:
        auto_color = "#3fb950" if self.side == "buy" else "#f85149"
        return {
            "type":   "signal",
            "time":   self.time,
            "side":   self.side,
            "price":  self.price,
            "label":  self.label,
            "color":  self.color or auto_color,
            "id":     self.id,
            "locked": self.locked,
            "source": "server",
        }


@dataclass
class TextLabel:
    """A floating text annotation at a price/time position."""
    time:      int
    price:     float
    text:      str
    color:     str = "#ffffff"
    font_size: int = 12
    locked:    bool = True
    id:        str  = field(default_factory=lambda: _new_id("txt"))

    def to_dict(self) -> dict:
        return {
            "type":     "text",
            "time":     self.time,
            "price":    self.price,
            "text":     self.text,
            "color":    self.color,
            "fontSize": self.font_size,
            "id":       self.id,
            "locked":   self.locked,
            "source":   "server",
        }


@dataclass
class RawIndicator:
    """
    Thin wrapper that lets the indicator_renderer push arbitrary dict payloads
    into ``chart._indicators`` while still satisfying the ``.name`` and
    ``.to_dict()`` interface that ``chart.py`` expects everywhere.

    Used internally by ``add_rsi``, ``add_macd``, ``add_ma``, and
    ``add_ichimoku``; not intended to be constructed by end-users.
    """
    name:    str
    payload: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return self.payload


@dataclass
class FibRetracement:
    """Fibonacci retracement levels between two swing points."""
    time1:  int
    price1: float
    time2:  int
    price2: float
    color:  str  = "#9c27b0"
    levels: list = field(
        default_factory=lambda: [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]
    )
    label:  str  = ""
    locked: bool = True
    id:     str  = field(default_factory=lambda: _new_id("fib"))

    def to_dict(self) -> dict:
        return {
            "type":   "fib",
            "time1":  self.time1,
            "price1": self.price1,
            "time2":  self.time2,
            "price2": self.price2,
            "color":  self.color,
            "levels": self.levels,
            "label":  self.label,
            "id":     self.id,
            "locked": self.locked,
            "source": "server",
        }
