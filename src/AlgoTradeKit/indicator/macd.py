"""
AlgoTradeKit.indicator.macd
~~~~~~~~~~~~~~~~~~~~~~~~~~~
MACD — Moving Average Convergence Divergence
TradingView Pine Script v5 compatible.

Formula
-------
fast_ma   = EMA(source, fast_length)    # default 12
slow_ma   = EMA(source, slow_length)    # default 26
macd_line = fast_ma - slow_ma
signal    = EMA(macd_line, signal_length)  # default 9
histogram = macd_line - signal

TradingView defaults
--------------------
fast_length   = 12
slow_length   = 26
signal_length = 9
source        = close
oscillator_ma = EMA   (ma used for macd line)
signal_ma     = EMA   (ma used for signal)

Colours (TradingView default)
------------------------------
macd     : #2962FF  (blue)
signal   : #FF6D00  (orange)
hist+    : #26A69A  (teal  — positive and growing)
hist+dim : #B2DFDB  (light teal — positive but shrinking)
hist-    : #FF5252  (red   — negative and falling)
hist-dim : #FFCDD2  (light red  — negative but recovering)
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


_MA_DISPATCH = {
    "EMA": _ema_series,
    "SMA": _sma_series,
    "SMMA": _rma_series,
    "WMA": _wma_series,
}

MaType = Literal["EMA", "SMA", "SMMA", "WMA"]


def _get_ma(ma_type: str):
    key = ma_type.upper()
    if key not in _MA_DISPATCH:
        raise ValueError(
            f"[AlgoTradeKit] MACD: unknown ma_type '{ma_type}'. "
            f"Choose from: {list(_MA_DISPATCH.keys())}"
        )
    return _MA_DISPATCH[key]


class MACD(_BaseIndicator):
    """
    MACD Indicator

    Parameters
    ----------
    source          : price series (typically close)
    fast_length     : fast MA period         (default 12)
    slow_length     : slow MA period         (default 26)
    signal_length   : signal MA period       (default 9)
    oscillator_ma   : MA type for macd line  ("EMA" | "SMA" | "SMMA" | "WMA")
    signal_ma       : MA type for signal     ("EMA" | "SMA" | "SMMA" | "WMA")

    Output keys (result dict)
    -------------------------
    "macd"      : MACD line
    "signal"    : Signal line
    "histogram" : MACD - Signal
    """

    _NAME = "MACD"

    # TradingView defaults
    DEFAULT_FAST: int = 12
    DEFAULT_SLOW: int = 26
    DEFAULT_SIGNAL: int = 9
    DEFAULT_OSC_MA: str = "EMA"
    DEFAULT_SIG_MA: str = "EMA"

    # TradingView colours
    COLOR_MACD: str = "#2962FF"
    COLOR_SIGNAL: str = "#FF6D00"
    COLOR_HIST_POS_GROW: str = "#26A69A"
    COLOR_HIST_POS_FADE: str = "#B2DFDB"
    COLOR_HIST_NEG_FALL: str = "#FF5252"
    COLOR_HIST_NEG_FADE: str = "#FFCDD2"

    def __init__(
        self,
        source: "pd.Series | list",
        fast_length: int = DEFAULT_FAST,
        slow_length: int = DEFAULT_SLOW,
        signal_length: int = DEFAULT_SIGNAL,
        oscillator_ma: MaType = DEFAULT_OSC_MA,
        signal_ma: MaType = DEFAULT_SIG_MA,
    ) -> None:
        super().__init__()
        self.source = _to_series(source)
        self.fast_length = fast_length
        self.slow_length = slow_length
        self.signal_length = signal_length
        self.oscillator_ma = oscillator_ma.upper()
        self.signal_ma = signal_ma.upper()

        _check_length(self.source, slow_length + signal_length - 1, "MACD")
        self._compute()

    # ------------------------------------------------------------------

    def _compute(self) -> None:
        osc_fn = _get_ma(self.oscillator_ma)
        sig_fn = _get_ma(self.signal_ma)

        fast = osc_fn(self.source, self.fast_length)
        slow = osc_fn(self.source, self.slow_length)

        macd_line = fast - slow
        signal_line = sig_fn(macd_line, self.signal_length)
        histogram = macd_line - signal_line

        self.result["macd"] = macd_line
        self.result["signal"] = signal_line
        self.result["histogram"] = histogram

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    @property
    def macd(self) -> pd.Series:
        return self.result["macd"]

    @property
    def signal(self) -> pd.Series:
        return self.result["signal"]

    @property
    def histogram(self) -> pd.Series:
        return self.result["histogram"]

    def histogram_colors(self) -> pd.Series:
        """
        Return a Series of hex colour strings for each histogram bar,
        matching TradingView's 4-colour logic:
        - positive + growing  → COLOR_HIST_POS_GROW
        - positive + shrinking→ COLOR_HIST_POS_FADE
        - negative + falling  → COLOR_HIST_NEG_FALL
        - negative + growing  → COLOR_HIST_NEG_FADE
        """
        h = self.histogram
        prev_h = h.shift(1)

        colors = []
        for i in range(len(h)):
            cur = h.iloc[i]
            prv = prev_h.iloc[i]
            # treat any NaN (float nan, pandas NA, numpy nan) as missing
            try:
                cur_nan = cur != cur  # NaN != NaN is True
            except TypeError:
                cur_nan = True
            if pd.isna(cur) or cur_nan:
                colors.append(None)
            elif cur >= 0:
                colors.append(
                    self.COLOR_HIST_POS_GROW if (pd.isna(prv) or cur > prv)
                    else self.COLOR_HIST_POS_FADE
                )
            else:
                colors.append(
                    self.COLOR_HIST_NEG_FALL if (pd.isna(prv) or cur < prv)
                    else self.COLOR_HIST_NEG_FADE
                )
        return pd.Series(colors)

    def crossover(self) -> pd.Series:
        """True where MACD line crosses above signal (bullish cross)."""
        prev_macd = self.macd.shift(1)
        prev_sig = self.signal.shift(1)
        return (prev_macd < prev_sig) & (self.macd >= self.signal)

    def crossunder(self) -> pd.Series:
        """True where MACD line crosses below signal (bearish cross)."""
        prev_macd = self.macd.shift(1)
        prev_sig = self.signal.shift(1)
        return (prev_macd > prev_sig) & (self.macd <= self.signal)

    def zero_crossover(self) -> pd.Series:
        """True where MACD line crosses above zero."""
        return (self.macd.shift(1) < 0) & (self.macd >= 0)

    def zero_crossunder(self) -> pd.Series:
        """True where MACD line crosses below zero."""
        return (self.macd.shift(1) > 0) & (self.macd <= 0)

    def visual_config(self) -> dict:
        """
        Return dict for the visual module — matches TradingView MACD style.
        """
        return {
            "pane": "oscillator",
            "lines": [
                {
                    "key": "macd",
                    "color": self.COLOR_MACD,
                    "width": 1,
                    "style": "solid",
                    "label": f"MACD({self.fast_length},{self.slow_length},{self.signal_length})",
                },
                {
                    "key": "signal",
                    "color": self.COLOR_SIGNAL,
                    "width": 1,
                    "style": "solid",
                    "label": f"Signal({self.signal_length})",
                },
            ],
            "histogram": {
                "key": "histogram",
                "colors": {
                    "pos_grow": self.COLOR_HIST_POS_GROW,
                    "pos_fade": self.COLOR_HIST_POS_FADE,
                    "neg_fall": self.COLOR_HIST_NEG_FALL,
                    "neg_fade": self.COLOR_HIST_NEG_FADE,
                },
                "label": "Histogram",
            },
            "levels": [
                {"value": 0, "color": "#787B86", "style": "solid"},
            ],
        }
