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
from .rsi import RSI
from .macd import MACD
from .ma import SMA, EMA, WMA, VWMA, SMMA, DEMA, TEMA, HullMA, VWAP
from .ichimoku import Ichimoku

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
