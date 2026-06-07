"""
AlgoTradeKit.indicator.rsi
~~~~~~~~~~~~~~~~~~~~~~~~~~
Relative Strength Index — TradingView Pine Script v5 compatible.

Formula (matches TradingView exactly)
--------------------------------------
change = close - close[1]
gain   = change > 0 ? change : 0
loss   = change < 0 ? -change : 0
avg_gain = rma(gain, length)   # Wilder's smoothing (alpha = 1/length)
avg_loss = rma(loss, length)
rs   = avg_gain / avg_loss
rsi  = 100 - 100 / (1 + rs)

Divergence between RSI and price is a common signal; the class exposes
all the data needed for further analysis.

TradingView defaults
---------------------
length    = 14
source    = close
overbought = 70   (upper horizontal line)
oversold   = 30   (lower horizontal line)
midline    = 50   (optional)
ma_type    = EMA  (for the optional smoothing line)
ma_length  = 14
"""

from __future__ import annotations

from typing import Literal

import pandas as pd

from ._base import (
    _BaseIndicator,
    _check_length,
    _ema_series,
    _rma_series,
    _sma_series,
    _to_series,
    _wma_series,
)


class RSI(_BaseIndicator):
    """
    Relative Strength Index

    Parameters
    ----------
    source          : price series (default: close prices)
    length          : RSI period          (default 14)
    overbought      : upper signal level  (default 70)
    oversold        : lower signal level  (default 30)
    show_ma         : add a smoothing MA on the RSI line  (default False)
    ma_type         : "EMA" | "SMA" | "SMMA" | "WMA"     (default "EMA")
    ma_length       : smoothing MA period                  (default 14)

    TradingView colours
    -------------------
    rsi_color       : "#7E57C2"   (purple)
    ma_color        : "#F7525F"   (red)
    ob_color        : "#787B86"   (grey)
    os_color        : "#787B86"   (grey)

    Output keys (result dict)
    -------------------------
    "rsi"     : RSI values
    "rsi_ma"  : smoothing MA on RSI (only if show_ma=True)
    """

    _NAME = "RSI"

    # TradingView defaults
    DEFAULT_LENGTH: int = 14
    DEFAULT_OVERBOUGHT: float = 70.0
    DEFAULT_OVERSOLD: float = 30.0
    DEFAULT_MA_TYPE: str = "EMA"
    DEFAULT_MA_LENGTH: int = 14

    # TradingView colours
    COLOR_RSI: str = "#7E57C2"
    COLOR_MA: str = "#F7525F"
    COLOR_OB: str = "#787B86"
    COLOR_OS: str = "#787B86"
    COLOR_OB_FILL: str = "rgba(120, 123, 134, 0.1)"
    COLOR_OS_FILL: str = "rgba(120, 123, 134, 0.1)"

    def __init__(
        self,
        source: "pd.Series | list",
        length: int = DEFAULT_LENGTH,
        overbought: float = DEFAULT_OVERBOUGHT,
        oversold: float = DEFAULT_OVERSOLD,
        show_ma: bool = False,
        ma_type: Literal["EMA", "SMA", "SMMA", "WMA"] = DEFAULT_MA_TYPE,
        ma_length: int = DEFAULT_MA_LENGTH,
    ) -> None:
        super().__init__()
        self.source = _to_series(source)
        self.length = length
        self.overbought = overbought
        self.oversold = oversold
        self.show_ma = show_ma
        self.ma_type = ma_type.upper()
        self.ma_length = ma_length

        _check_length(self.source, length + 1, "RSI")
        self._compute()

    # ------------------------------------------------------------------

    def _compute(self) -> None:
        change = self.source.diff(1)

        gain = change.clip(lower=0.0)
        loss = (-change).clip(lower=0.0)

        # Wilder smoothing (RMA) — exactly as TradingView
        avg_gain = _rma_series(gain, self.length)
        avg_loss = _rma_series(loss, self.length)

        rs = avg_gain / avg_loss
        rsi = 100.0 - (100.0 / (1.0 + rs))

        self.result["rsi"] = rsi

        if self.show_ma:
            ma_fn = self._ma_fn(self.ma_type)
            self.result["rsi_ma"] = ma_fn(rsi, self.ma_length)

    def _ma_fn(self, ma_type: str):
        dispatch = {
            "EMA": _ema_series,
            "SMA": _sma_series,
            "SMMA": _rma_series,
            "WMA": _wma_series,
        }
        if ma_type not in dispatch:
            raise ValueError(
                f"[AlgoTradeKit] RSI: unknown ma_type '{ma_type}'. "
                f"Choose from: {list(dispatch.keys())}"
            )
        return dispatch[ma_type]

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    @property
    def rsi(self) -> pd.Series:
        return self.result["rsi"]

    @property
    def rsi_ma(self) -> pd.Series:
        if "rsi_ma" not in self.result:
            raise AttributeError("RSI was initialised with show_ma=False.")
        return self.result["rsi_ma"]

    def is_overbought(self) -> pd.Series:
        """Boolean series: True where RSI > overbought level."""
        return self.rsi > self.overbought

    def is_oversold(self) -> pd.Series:
        """Boolean series: True where RSI < oversold level."""
        return self.rsi < self.oversold

    def crossover_ob(self) -> pd.Series:
        """Boolean series: True on bars where RSI crosses above overbought."""
        prev = self.rsi.shift(1)
        return (prev < self.overbought) & (self.rsi >= self.overbought)

    def crossunder_ob(self) -> pd.Series:
        """Boolean series: True on bars where RSI crosses below overbought."""
        prev = self.rsi.shift(1)
        return (prev >= self.overbought) & (self.rsi < self.overbought)

    def crossover_os(self) -> pd.Series:
        """Boolean series: True on bars where RSI crosses above oversold."""
        prev = self.rsi.shift(1)
        return (prev < self.oversold) & (self.rsi >= self.oversold)

    def crossunder_os(self) -> pd.Series:
        """Boolean series: True on bars where RSI crosses below oversold."""
        prev = self.rsi.shift(1)
        return (prev >= self.oversold) & (self.rsi < self.oversold)

    def visual_config(self) -> dict:
        """
        Return a dict that the visual module can consume to configure pane
        appearance — matches TradingView default RSI style.
        """
        cfg: dict = {
            "pane": "oscillator",
            "scale": {"min": 0, "max": 100},
            "lines": [
                {
                    "key": "rsi",
                    "color": self.COLOR_RSI,
                    "width": 1,
                    "style": "solid",
                    "label": f"RSI({self.length})",
                },
            ],
            "levels": [
                {"value": self.overbought, "color": self.COLOR_OB, "style": "dashed"},
                {"value": self.oversold, "color": self.COLOR_OS, "style": "dashed"},
                {"value": 50, "color": "#787B86", "style": "dotted"},
            ],
            "bands": [
                {
                    "upper": self.overbought,
                    "lower": 100,
                    "color": self.COLOR_OB_FILL,
                },
                {
                    "upper": 0,
                    "lower": self.oversold,
                    "color": self.COLOR_OS_FILL,
                },
            ],
        }
        if self.show_ma:
            cfg["lines"].append(
                {
                    "key": "rsi_ma",
                    "color": self.COLOR_MA,
                    "width": 1,
                    "style": "solid",
                    "label": f"{self.ma_type}({self.ma_length})",
                }
            )
        return cfg
