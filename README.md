# AlgoTradeKit

**Algorithmic Trading Toolkit** — collect market data, build indicators and strategies, backtest, and trade live.

```
pip install AlgoTradeKit
```

---

## Modules

| Module      | Status      | Description                                            |
| ----------- | ----------- | ------------------------------------------------------ |
| `data`      | ✅ v0.1.0   | Collect OHLCV candles from exchanges                   |
| `indicator` | ✅ v0.4.0   | RSI, MACD, MA family, Ichimoku — TradingView-compatible|
| `visual`    | ✅ v0.3.0   | Candlestick charts, indicators, live streaming         |
| `strategy`  | 🔜 planned  | Build and combine trading strategies                   |
| `simulate`  | 🔜 planned  | Backtest strategies on historical data                 |
| `trade`     | 🔜 planned  | Live trading via exchange API or MT5                   |

---

## Quick Start

### Collect candle data

```python
from AlgoTradeKit.data import Collector

collector = Collector(source="binance-futures", symbol="BTCUSDT", timeframe="1d")
collector.destination = "data/"
collector.starttime   = "2020/01/01"
collector.collect()
```

### Compute indicators

```python
import pandas as pd
from AlgoTradeKit.indicator import RSI, MACD, EMA, SMA, Ichimoku

df = pd.read_csv("data/binance-futures_BTCUSDT_1d.csv")

# RSI (default: length=14, OB=70, OS=30)
rsi = RSI(df["close"])
print(rsi.rsi.tail())

# RSI with MA overlay
rsi_ma = RSI(df["close"], length=14, show_ma=True, ma_type="EMA", ma_length=9)

# MACD (default: 12, 26, 9)
macd = MACD(df["close"])
print(macd.macd.tail(), macd.signal.tail(), macd.histogram.tail())

# EMA
ema20 = EMA(df["close"], length=20)

# Ichimoku (all default TradingView settings)
ichi = Ichimoku(df["high"], df["low"], df["close"])
print(ichi.tenkan.tail())
print(ichi.cloud_df().tail(30))  # includes 26 future bars
```

### Visualise with indicators

```python
from AlgoTradeKit.visual import Chart, add_rsi, add_macd, add_ma, add_ichimoku
from AlgoTradeKit.indicator import RSI, MACD, EMA, Ichimoku

chart = Chart.from_csv("data/binance-futures_BTCUSDT_1d.csv")

rsi  = RSI(chart.df["close"])
macd = MACD(chart.df["close"])
ema  = EMA(chart.df["close"], length=20)
ichi = Ichimoku(chart.df["high"], chart.df["low"], chart.df["close"])

add_ma(chart,       ema,  timestamps=chart.df["timestamp"])
add_ichimoku(chart, ichi, timestamps=chart.df["timestamp"])
add_rsi(chart,      rsi,  timestamps=chart.df["timestamp"])
add_macd(chart,     macd, timestamps=chart.df["timestamp"])

chart.show()   # opens browser at http://localhost:9000
```

| Feature            | Details                                                                     |
|--------------------|-----------------------------------------------------------------------------|
| Ichimoku cloud     | Canvas-drawn fill between Span A/B — bullish teal / bearish red             |
| Indicator grouping | Multi-line indicators grouped with collapsible header, hide-all, remove-all |
| Legend position    | Top-left of chart pane                                                      |
| Settings ⚙         | Font, axis label toggles (last close / indicator prices on right axis)      |
| Cursor axis label  | Always shows candle close price, never indicator values                     |

---

## Data Module

### Collect candle data

```python
from AlgoTradeKit.data import Collector

collector = Collector(source="binance-spot", symbol="ETHUSDT", timeframe="4h")
collector.destination = "data/"
collector.starttime   = "2021/01/01"
collector.endtime     = "2023/01/01"   # optional — defaults to now
collector.collect()                     # returns path to saved CSV
```

### Resample timeframes

```python
from AlgoTradeKit.data import Converter

conv = Converter(source="data/binance-futures_BTCUSDT_1h.csv", target_timeframe="4h")
conv.destination = "data/"
conv.convert()
```

### Supported sources

| Source key            | Market                |
| --------------------- | --------------------- |
| `"binance-spot"`      | Binance Spot          |
| `"binance-futures"`   | Binance USD-M Futures |

### Supported timeframes

`1m` `3m` `5m` `15m` `30m` `1h` `2h` `4h` `6h` `8h` `12h` `1d` `3d` `1w` `1M`

---

## Indicator Reference

### MA Family

All Moving Averages accept `source` (price series) and `length` (period).

| Class    | Formula                              | TV Default | TV Colour  |
| -------- | ------------------------------------ | ---------- | ---------- |
| `SMA`    | Rolling mean                         | 9          | `#2962FF`  |
| `EMA`    | α = 2/(n+1)                          | 9          | `#FF6D00`  |
| `WMA`    | Linear weights                       | 9          | `#00BCD4`  |
| `SMMA`   | Wilder RMA, α = 1/n                  | 9          | `#4CAF50`  |
| `DEMA`   | 2·EMA − EMA(EMA)                     | 9          | `#F44336`  |
| `TEMA`   | 3·EMA − 3·EMA(EMA) + EMA(EMA(EMA))  | 9          | `#FF9800`  |
| `HullMA` | WMA(2·WMA(n/2)−WMA(n), √n)          | 9          | `#9C27B0`  |
| `VWMA`   | Σ(price·vol) / Σ(vol)                | 20         | `#E040FB`  |
| `VWAP`   | Cumulative PV/V with σ bands         | —          | `#2962FF`  |

```python
from AlgoTradeKit.indicator import SMA, EMA, WMA, VWMA, SMMA, DEMA, TEMA, HullMA, VWAP

sma  = SMA(close, length=20)
ema  = EMA(close, length=20)
wma  = WMA(close, length=20)
vwma = VWMA(close, volume, length=20)
smma = SMMA(close, length=20)
dema = DEMA(close, length=20)
tema = TEMA(close, length=20)
hma  = HullMA(close, length=20)
vwap = VWAP(high, low, close, volume, anchor="none", bands=[1, 2])
```

### RSI

```python
rsi = RSI(
    source      = df["close"],
    length      = 14,          # period (TV default)
    overbought  = 70,          # upper level (TV default)
    oversold    = 30,          # lower level (TV default)
    show_ma     = False,       # add MA on RSI line
    ma_type     = "EMA",       # EMA | SMA | SMMA | WMA
    ma_length   = 14,
)

rsi.rsi                    # RSI series
rsi.is_overbought()        # bool series
rsi.is_oversold()          # bool series
rsi.crossover_ob()         # entered overbought
rsi.crossunder_os()        # exited oversold (bullish signal)
```

### MACD

```python
macd = MACD(
    source        = df["close"],
    fast_length   = 12,        # TV default
    slow_length   = 26,        # TV default
    signal_length = 9,         # TV default
    oscillator_ma = "EMA",     # EMA | SMA | SMMA | WMA
    signal_ma     = "EMA",     # EMA | SMA | SMMA | WMA
)

macd.macd                  # MACD line
macd.signal                # Signal line
macd.histogram             # MACD − Signal
macd.histogram_colors()    # per-bar TV 4-colour Series
macd.crossover()           # bullish MACD/signal cross
macd.zero_crossover()      # MACD crosses above zero
```

### Ichimoku

```python
ichi = Ichimoku(
    high            = df["high"],
    low             = df["low"],
    close           = df["close"],
    tenkan_period   = 9,        # TV default
    kijun_period    = 26,       # TV default
    senkou_b_period = 52,       # TV default
    displacement    = 26,       # TV default
)

ichi.tenkan                # Conversion Line
ichi.kijun                 # Base Line
ichi.senkou_a              # Leading Span A  (26 bars forward)
ichi.senkou_b              # Leading Span B  (26 bars forward)
ichi.chikou                # Lagging Span    (26 bars back)
ichi.cloud_df()            # DataFrame with full cloud + 26 future bars
ichi.tk_cross_bullish()    # Tenkan crosses above Kijun
ichi.price_above_cloud()   # Close above both Span A and B
```

---

## Installation

Requires Python 3.10+

```
pip install AlgoTradeKit
```

For development:

```
git clone https://github.com/AmirMohammadBazdar/AlgoTradeKit.git
cd AlgoTradeKit
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

---

## Requirements

- `pandas >= 2.0`
- `requests >= 2.28`
- `fastapi >= 0.110.0` *(visual module)*
- `uvicorn >= 0.29.0` *(visual module)*
- `websockets >= 12.0` *(visual module)*

---

## Roadmap

- [x] `data` module — Collector (Binance Spot & Futures)
- [x] `data` module — Converter (timeframe resampling)
- [x] `visual` module — interactive candlestick chart
- [x] `indicator` module — RSI, MACD, MA family, Ichimoku
- [ ] `indicator` — Bollinger Bands, ATR, Stochastic, VWAP session anchor
- [ ] `strategy` module — strategy builder with entry/exit logic
- [ ] `simulate` module — backtesting engine with full report
- [ ] `trade` module — live trading via exchange API / MT5
- [ ] MEXC, Bybit, OKX data sources

---

## License

MIT — see [LICENSE](LICENSE)
