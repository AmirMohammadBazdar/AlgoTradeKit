"""
algotradekit.visual
===================

Interactive candlestick charting module.

Renders a live chart in your browser served by a local FastAPI + WebSocket
server.  Based on TradingView's lightweight-charts library.

Quick start
-----------
    from AlgoTradeKit.visual import Chart

    # Load directly from a Collector CSV
    c = Chart.from_csv("data/binance-futures_BTCUSDT_1d.csv")
    c.show(block=True)

Supported drawing types
-----------------------
    HorizontalLine, TrendLine, Box, Signal, TextLabel, FibRetracement

Streaming (live data)
---------------------
    # For standard dicts with 'time' in seconds:
    c.stream(bar_dict)

    # For AlgoTradeKit exchange dicts with 'timestamp' in ms:
    c.stream_from_atk(candle_dict)

Future integration
------------------
    # indicator module (v0.4.0):
    c.add_indicator_from_atk(indicator_df, name="RSI 14", overlay=False, pane=1)

    # strategy module (v0.5.0):
    for signal in strategy.signals:
        c.add_signal(signal.time, signal.side, price=signal.price)
"""

from .chart import Chart
from .models import (
    Bar,
    Box,
    FibRetracement,
    HorizontalLine,
    IndicatorSeries,
    Signal,
    TextLabel,
    TrendLine,
)

__all__ = [
    "Chart",
    # Models
    "Bar",
    "Box",
    "FibRetracement",
    "HorizontalLine",
    "IndicatorSeries",
    "Signal",
    "TextLabel",
    "TrendLine",
]
