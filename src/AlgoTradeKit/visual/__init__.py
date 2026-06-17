"""
AlgoTradeKit.visual
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
    PositionBox  (v0.7.0) — TradingView-style simulation position box

Streaming (live data)
---------------------
    # For standard dicts with 'time' in seconds:
    c.stream(bar_dict)

    # For AlgoTradeKit exchange dicts with 'timestamp' in ms:
    c.stream_from_atk(candle_dict)

Indicators (v0.4.0)
-------------------
    from AlgoTradeKit.visual import add_rsi, add_macd, add_ma, add_ichimoku
    from AlgoTradeKit.indicator import RSI, MACD, EMA, Ichimoku

    rsi  = RSI(chart.df["close"], length=14)
    add_rsi(chart, rsi, timestamps=chart.df["timestamp"])

    macd = MACD(chart.df["close"])
    add_macd(chart, macd, timestamps=chart.df["timestamp"])

    ema20 = EMA(chart.df["close"], length=20)
    add_ma(chart, ema20, timestamps=chart.df["timestamp"])

    ichi = Ichimoku(chart.df["high"], chart.df["low"], chart.df["close"])
    add_ichimoku(chart, ichi, timestamps=chart.df["timestamp"])

    chart.show()

Simulation positions (v0.7.0)
------------------------------
    from AlgoTradeKit.visual.indicator_renderer import add_simulation_positions

    # After running a simulation:
    chart = Chart.from_csv("data/binance-futures_BTCUSDT_1h.csv")
    add_simulation_positions(chart, report)
    chart.show(block=True)

    # Or let SimulateConfig.show_chart do it automatically.

Strategy drawings (v0.7.0)
---------------------------
    from AlgoTradeKit.visual.indicator_renderer import add_strategy_drawings

    result = strategy.run(data)
    chart = Chart.from_csv("data/binance-futures_BTCUSDT_1h.csv")
    add_strategy_drawings(chart, result)
    chart.show()
"""

from .chart import Chart
from .models import (
    Bar,
    Box,
    FibRetracement,
    HorizontalLine,
    IndicatorSeries,
    PositionBox,
    Signal,
    TextLabel,
    TrendLine,
)
from .indicator_renderer import (
    add_ichimoku,
    add_ma,
    add_macd,
    add_rsi,
    add_simulation_positions,
    add_strategy_drawings,
)

__all__ = [
    # Chart
    "Chart",
    # Models
    "Bar",
    "Box",
    "FibRetracement",
    "HorizontalLine",
    "IndicatorSeries",
    "PositionBox",
    "Signal",
    "TextLabel",
    "TrendLine",
    # Indicator renderer helpers
    "add_rsi",
    "add_macd",
    "add_ma",
    "add_ichimoku",
    # Simulation helpers (v0.7.0)
    "add_simulation_positions",
    "add_strategy_drawings",
]
