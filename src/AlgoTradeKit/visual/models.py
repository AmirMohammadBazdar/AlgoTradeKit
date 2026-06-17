"""
algotradekit.visual.models
~~~~~~~~~~~~~~~~~~~~~~~~~~
Data-classes for all chart objects: candles, indicators, and drawings.

Every class has a ``to_dict()`` method that serialises it into the JSON
format the frontend (lightweight-charts) understands.

v0.7.0 additions
-----------------
PositionBox — TradingView-style position box rendered on a candle chart.
              Shows loss zone (SL→entry) and profit zone (entry→TP) as two
              stacked coloured rectangles, with entry/exit markers and an
              R:R ratio label.
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
    group:       str   = ""

    def to_dict(self) -> dict:
        return {
            "name":       self.name,
            "data":       self.data,
            "color":      self.color,
            "overlay":    self.overlay,
            "pane":       self.pane,
            "lineWidth":  self.line_width,
            "seriesType": self.series_type,
            "group":      self.group,
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


@dataclass
class PositionBox:
    """
    TradingView-style position box rendered on a candle chart.

    Renders two stacked coloured rectangles spanning the trade's time
    range:

    * **Loss zone** — from ``stop_loss`` to ``entry_price``:
      red for long, green for short (the zone where a loss occurs).
    * **Profit zone** — from ``entry_price`` to ``take_profit``:
      green for long, red for short (the zone where profit is made).

    Additionally draws:
    * A dashed horizontal entry line across the full time span.
    * Entry/exit markers (triangles) at the open/close timestamps.
    * A centred risk/reward label in the profit zone.

    Parameters
    ----------
    open_time : int
        Entry candle timestamp in **Unix seconds** (not milliseconds).
    close_time : int
        Exit candle timestamp in **Unix seconds**.
    entry_price : float
        Actual fill price.
    stop_loss : float
        Initial stop-loss price.
    take_profit : float | None
        Take-profit price.  When ``None``, the profit zone is drawn at
        2× the SL distance as a visual placeholder.
    direction : str
        ``"long"`` or ``"short"``.
    net_pnl : float
        Net profit/loss of the trade (used to colour the exit marker).
    close_reason : str
        Reason the trade was closed (``"sl"``, ``"tp"``, ``"rf"``, …).
    trade_id : int
        Sequential trade ID from the simulation.
    rr_ratio : float
        Actual R:R of the trade (net_pnl / risk_amount).
    win_color : str
        Colour for the profit zone and winning markers (default green).
    loss_color : str
        Colour for the loss zone and losing markers (default red).
    opacity : float
        Fill opacity for both zones (default 0.15).
    locked : bool
        When ``True`` the drawing cannot be moved in the browser.
    """

    open_time:    int           # Unix seconds
    close_time:   int           # Unix seconds
    entry_price:  float
    stop_loss:    float
    take_profit:  Optional[float]
    direction:    str           # "long" | "short"
    net_pnl:      float
    close_reason: str
    trade_id:     int  = -1
    rr_ratio:     float = 0.0
    win_color:    str  = "#3fb950"
    loss_color:   str  = "#f85149"
    opacity:      float = 0.15
    locked:       bool  = True
    id:           str   = field(default_factory=lambda: _new_id("posbox"))

    def to_dict(self) -> dict:
        sl_dist = abs(self.entry_price - self.stop_loss)

        # Compute a visual TP if none provided (2R placeholder)
        if self.take_profit is not None:
            visual_tp = self.take_profit
        else:
            if self.direction == "long":
                visual_tp = self.entry_price + sl_dist * 2.0
            else:
                visual_tp = self.entry_price - sl_dist * 2.0

        # Profit zone: entry → TP direction
        # Loss zone:   entry → SL direction
        if self.direction == "long":
            profit_top    = visual_tp
            profit_bottom = self.entry_price
            loss_top      = self.entry_price
            loss_bottom   = self.stop_loss
            profit_color  = self.win_color
            loss_color    = self.loss_color
        else:  # short
            profit_top    = self.entry_price
            profit_bottom = visual_tp
            loss_top      = self.stop_loss
            loss_bottom   = self.entry_price
            profit_color  = self.win_color
            loss_color    = self.loss_color

        # R:R label
        tp_dist = abs(visual_tp - self.entry_price)
        rr = (tp_dist / sl_dist) if sl_dist > 0 else 0.0
        label = f"{self.direction[0].upper()}  1:{rr:.1f}R"
        if self.close_reason in ("sl", "rf"):
            label = f"✕ SL  {self.rr_ratio:+.2f}R"
        elif self.close_reason == "tp":
            label = f"✓ TP  {self.rr_ratio:+.2f}R"
        elif self.close_reason == "force_close":
            label = f"↯ FC  {self.rr_ratio:+.2f}R"

        return {
            "type":          "position_box",
            "open_time":     self.open_time,
            "close_time":    self.close_time,
            "entry_price":   self.entry_price,
            "stop_loss":     self.stop_loss,
            "visual_tp":     visual_tp,
            "profit_top":    profit_top,
            "profit_bottom": profit_bottom,
            "loss_top":      loss_top,
            "loss_bottom":   loss_bottom,
            "profit_color":  profit_color,
            "loss_color":    loss_color,
            "direction":     self.direction,
            "net_pnl":       self.net_pnl,
            "close_reason":  self.close_reason,
            "trade_id":      self.trade_id,
            "rr_ratio":      self.rr_ratio,
            "label":         label,
            "opacity":       self.opacity,
            "id":            self.id,
            "locked":        self.locked,
            "source":        "server",
        }
