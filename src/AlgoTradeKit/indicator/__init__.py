"""
AlgoTradeKit — indicator module
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
TradingView-compatible technical indicators implemented from scratch.

Indicators
----------
- RSI      : Relative Strength Index
- MACD     : Moving Average Convergence Divergence
- ATR      : Average True Range (Wilder's smoothing)
- SMA      : Simple Moving Average
- EMA      : Exponential Moving Average
- WMA      : Weighted Moving Average
- VWMA     : Volume-Weighted Moving Average
- SMMA     : Smoothed Moving Average (RMA)
- DEMA     : Double EMA
- TEMA     : Triple EMA
- HullMA   : Hull Moving Average
- VWAP     : Volume-Weighted Average Price
- Ichimoku : Ichimoku Cloud (Kinko Hyo)
"""

from .atr import ATR
from .ichimoku import Ichimoku
from .ma import DEMA, EMA, SMA, SMMA, TEMA, VWAP, VWMA, WMA, HullMA
from .macd import MACD
from .rsi import RSI

__all__ = [
    "ATR",
    "RSI",
    "MACD",
    "SMA",
    "EMA",
    "WMA",
    "VWMA",
    "SMMA",
    "DEMA",
    "TEMA",
    "HullMA",
    "VWAP",
    "Ichimoku",
]
