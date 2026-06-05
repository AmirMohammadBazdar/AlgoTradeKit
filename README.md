# AlgoTradeKit

**Algorithmic Trading Toolkit** — collect market data, convert timeframes, visualize charts, build indicators and strategies, backtest, and trade live.

```bash
pip install AlgoTradeKit
```

---

## Modules

| Module | Status | Description |
|---|---|---|
| `data` | ✅ v0.1.0 | Collect and convert OHLCV candles from exchanges |
| `visual` | ✅ v0.3.0 | Interactive candlestick charts in your browser |
| `indicator` | 🔜 planned | RSI, EMA, MACD, Bollinger Bands, and custom indicators |
| `strategy` | 🔜 planned | Build and combine trading strategies |
| `simulate` | 🔜 planned | Backtest strategies on historical data |
| `trade` | 🔜 planned | Live trading via exchange API or MT5 |

---

## Quick Start

### 1 — Collect candle data

```python
from AlgoTradeKit.data import Collector

collector = Collector(source="binance-futures", symbol="BTCUSDT", timeframe="1d")
collector.destination = "data/"
collector.starttime   = "2020/01/01"
collector.collect()
```

Output:
```
[AlgoTradeKit] Symbol     : BTCUSDT
[AlgoTradeKit] Source     : binance-futures
[AlgoTradeKit] Timeframe  : 1 Day  (1d)
[AlgoTradeKit] Range      : 2020-01-01 → 2026-05-26
[AlgoTradeKit] Existing file : none — starting fresh
[AlgoTradeKit] Found 1 gap(s) to fill
    [██████████████████████████████]  2,338 candles fetched
[AlgoTradeKit] ✓ Saved  2,338 candles  →  data/binance-futures_BTCUSDT_1d.csv
```

Run it again — it only fetches what is missing:
```
[AlgoTradeKit] ✓ Everything is up to date — nothing to download.
```

#### Collector options

```python
collector = Collector(source="binance-spot", symbol="ETHUSDT", timeframe="4h")

collector.destination = "data/"           # folder to save CSV  (default: ./)
collector.outputname  = "eth_4h.csv"      # custom filename     (auto-generated if not set)
collector.starttime   = "2021/01/01"      # required — YYYY/MM/DD or YYYY-MM-DD
collector.endtime     = "2023/01/01"      # optional — defaults to now

collector.collect()                        # returns path to the saved CSV
```

#### Supported sources

| Source key | Market |
|---|---|
| `"binance-spot"` | Binance Spot |
| `"binance-futures"` | Binance USD-M Futures |

#### Supported timeframes

`1m` `3m` `5m` `15m` `30m` `1h` `2h` `4h` `6h` `8h` `12h` `1d` `3d` `1w` `1M`

---

### 2 — Convert timeframes

```python
from AlgoTradeKit.data import Converter

# From a CSV file — timeframe is auto-detected from timestamps
conv = Converter(source="data/binance-futures_BTCUSDT_1h.csv", target_timeframe="4h")
conv.destination = "data/"
conv.convert()

# From a pandas DataFrame
conv = Converter(source=df, target_timeframe="1d")
conv.destination = "data/"
conv.outputname  = "btc_daily.csv"
conv.convert()
```

Valid conversions — target must be a whole multiple of the source:

```
1h  → 4h, 6h, 12h, 1d, 1w, 1M   ✓
15m → 1h, 4h, 1d                  ✓
4h  → 1h                          ✗  (cannot go down)
1h  → 3h                          ✗  (3h is not a supported timeframe)
```

---

### 3 — Visualize

```python
from AlgoTradeKit.visual import Chart

# Load a Collector CSV directly — one line
c = Chart.from_csv("data/binance-futures_BTCUSDT_1d.csv")
c.show(block=True)
```

Or with the full data pipeline:

```python
from AlgoTradeKit.data   import Collector
from AlgoTradeKit.visual import Chart

# Step 1: collect
collector = Collector("binance-futures", "BTCUSDT", "1d")
collector.destination = "data/"
collector.starttime   = "2024/01/01"
collector.collect()

# Step 2: visualize
c = Chart(title="BTCUSDT 1D — Binance Futures")
c.set_data("data/binance-futures_BTCUSDT_1d.csv")
c.show(block=True)
```

#### Add indicators

```python
import pandas as pd

df    = pd.read_csv("data/binance-futures_BTCUSDT_1d.csv")
ema20 = df.assign(value=df["close"].ewm(span=20).mean())[["timestamp", "value"]]
ema50 = df.assign(value=df["close"].ewm(span=50).mean())[["timestamp", "value"]]

c = Chart.from_csv("data/binance-futures_BTCUSDT_1d.csv", title="BTCUSDT 1D")
c.add_indicator_from_atk(ema20, name="EMA 20", color="#f0c040", overlay=True)
c.add_indicator_from_atk(ema50, name="EMA 50", color="#58a6ff", overlay=True)
c.show(block=True)
```

#### Add drawings

```python
c = Chart.from_csv("data/binance-futures_BTCUSDT_1d.csv")

# Horizontal price levels
c.add_hline(95000, color="#ef5350", label="Resistance", line_style=2)
c.add_hline(60000, color="#3fb950", label="Support",    line_style=2)

# Trend line between two price/time points
c.add_trendline(time1=1704067200, price1=42000,
                time2=1706745600, price2=48000, color="#2196f3")

# Price box (range highlight)
c.add_box(time1=1704067200, price1=40000,
          time2=1709251200, price2=50000, color="#26a69a", opacity=0.15)

# Buy / sell signals
c.add_signal(time=1704067200, side="buy",  price=42000, label="Entry")
c.add_signal(time=1709251200, side="sell", price=67000, label="Exit")

# Fibonacci retracement
c.add_fib(time1=1704067200, price1=38000,
          time2=1709251200, price2=69000, color="#9c27b0")

# Text annotation
c.add_text(time=1704067200, price=72000, text="ATH Zone", color="#ffa500")

c.show(block=True)
```

#### Live streaming

```python
import time

c = Chart.from_csv("data/binance-futures_BTCUSDT_1h.csv", title="BTCUSDT Live")
c.show()

# Stream candles in real time — 'time' in seconds (standard format)
for bar in my_live_feed():
    c.stream(bar)
    c.stream_indicator("EMA 20", bar["time"], updated_ema)
    time.sleep(0.1)

# Stream from an AlgoTradeKit exchange source — 'timestamp' in ms
for candle in exchange.live_feed("BTCUSDT", "1m"):
    c.stream_from_atk(candle)
```

#### Save and restore layout

```python
# Save all indicators and drawings to JSON
c.save_layout("layouts/btcusdt_1d.json")

# Restore later — load data first, then apply the saved layout
c2 = Chart.from_csv("data/binance-futures_BTCUSDT_1d.csv")
c2.load_layout("layouts/btcusdt_1d.json")
c2.show(block=True)
```

#### Chart options

```python
c = Chart(
    title          = "BTCUSDT 1D",
    chart_type     = "heikinashi",   # "candlestick" (default) or "heikinashi"
    theme          = "dark",         # "dark" (default) or "light"
    volume_in_main = True,           # show volume bars in the main price pane
    port           = 0,              # 0 = auto-select a free port
)
```

---

## Full pipeline example

```python
from AlgoTradeKit.data   import Collector, Converter
from AlgoTradeKit.visual import Chart
import pandas as pd

# 1. Collect 1h candles
col = Collector("binance-futures", "BTCUSDT", "1h")
col.destination = "data/"
col.starttime   = "2024/01/01"
col.collect()

# 2. Convert to 4h
conv = Converter("data/binance-futures_BTCUSDT_1h.csv", target_timeframe="4h")
conv.destination = "data/"
conv.convert()

# 3. Load and add indicators
df    = pd.read_csv("data/converted_BTCUSDT_1h_to_4h.csv")
ema20 = df.assign(value=df["close"].ewm(span=20).mean())[["timestamp", "value"]]
ema50 = df.assign(value=df["close"].ewm(span=50).mean())[["timestamp", "value"]]

# 4. Visualize
chart = Chart(title="BTCUSDT 4H")
chart.set_data(df)
chart.add_indicator_from_atk(ema20, name="EMA 20", color="#f0c040")
chart.add_indicator_from_atk(ema50, name="EMA 50", color="#58a6ff")
chart.add_hline(95000, color="#ef5350", label="Resistance")
chart.add_signal(time=1704067200, side="buy", price=42000, label="Entry")
chart.show(block=True)
```

---

## Installation

Requires Python 3.10+

```bash
pip install AlgoTradeKit
```

For development:

```bash
git clone https://github.com/AmirMohammadBazdar/AlgoTradeKit.git
cd AlgoTradeKit
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

---

## Requirements

| Package | Version | Used by |
|---|---|---|
| `pandas` | >= 2.0 | `data`, `visual` |
| `requests` | >= 2.28 | `data` |
| `fastapi` | >= 0.110.0 | `visual` |
| `uvicorn[standard]` | >= 0.29.0 | `visual` |
| `websockets` | >= 12.0 | `visual` |

---

## Roadmap

### Data
- [ ] MEXC Spot & Futures
- [ ] MetaTrader 5 (MT5)
- [ ] Bybit, OKX

### Visual
- [ ] Live exchange WebSocket feed (real-time candlestick stream)
- [ ] Multi-chart layout (side-by-side panels)
- [ ] Screenshot / export to PNG

### Upcoming modules
- [ ] `indicator` — RSI, EMA, MACD, Bollinger Bands, ATR, VWAP
- [ ] `strategy` — entry/exit logic builder, signal generation
- [ ] `simulate` — backtesting engine with full performance report
- [ ] `trade` — live order execution via exchange API / MT5

---

## License

MIT — see [LICENSE](LICENSE)
