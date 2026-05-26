# AlgoTradeKit

**Algorithmic Trading Toolkit** — collect market data, build indicators and strategies, backtest, and trade live.

```bash
pip install AlgoTradeKit
```

---

## Modules

| Module | Status | Description |
|---|---|---|
| `data` | ✅ v0.1.0 | Collect OHLCV candles from exchanges |
| `indicator` | 🔜 planned | RSI, EMA, MACD, Bollinger Bands, and custom indicators |
| `strategy` | 🔜 planned | Build and combine trading strategies |
| `simulate` | 🔜 planned | Backtest strategies on historical data |
| `trade` | 🔜 planned | Live trading via exchange API or MT5 |
| `visual` | 🔜 planned | Candlestick charts, indicators, live streaming |

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

Output:
```
[AlgoTradeKit] Symbol     : BTCUSDT
[AlgoTradeKit] Source     : binance-futures
[AlgoTradeKit] Timeframe  : 1 Day  (1d)
[AlgoTradeKit] Range      : 2020-01-01 00:00 UTC  →  2026-05-26 00:00 UTC
[AlgoTradeKit] Output     : data/binance-futures_BTCUSDT_1d.csv
[AlgoTradeKit] Existing file : none — starting fresh
[AlgoTradeKit] Found 1 gap(s) to fill

    [██████████████████████████████]  2,338 candles fetched

[AlgoTradeKit] ✓ Saved  2,338 candles  →  data/binance-futures_BTCUSDT_1d.csv
```

Run it again — it only fetches what's missing:
```
[AlgoTradeKit] ✓ Everything is up to date — nothing to download.
```

### Supported sources

| Source key | Market |
|---|---|
| `"binance-spot"` | Binance Spot |
| `"binance-futures"` | Binance USD-M Futures |

### Supported timeframes

`1m` `3m` `5m` `15m` `30m` `1h` `2h` `4h` `6h` `8h` `12h` `1d` `3d` `1w` `1M`

### Collector options

```python
collector = Collector(source="binance-spot", symbol="ETHUSDT", timeframe="4h")

collector.destination = "data/"           # folder to save CSV (default: ./)
collector.outputname  = "eth_4h.csv"      # custom filename (auto-generated if not set)
collector.starttime   = "2021/01/01"      # required — YYYY/MM/DD or YYYY-MM-DD
collector.endtime     = "2023/01/01"      # optional — defaults to now

collector.collect()                        # returns path to the saved CSV
```

---

## Installation

Requires Python 3.10+

```bash
pip install AlgoTradeKit
```

For development:
```bash
git clone https://github.com/YOUR_USERNAME/AlgoTradeKit.git
cd AlgoTradeKit
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

---

## Requirements

- `pandas >= 2.0`
- `requests >= 2.28`

---

## Roadmap

- [ ] MEXC Spot & Futures
- [ ] MetaTrader 5 (MT5)
- [ ] Bybit, OKX
- [ ] `indicator` module — RSI, EMA, MACD, Bollinger Bands
- [ ] `strategy` module — strategy builder with entry/exit logic
- [ ] `simulate` module — backtesting engine with full report
- [ ] `trade` module — live trading via exchange API / MT5
- [ ] `visual` module — charts, live candlestick stream via WebSocket

---

## License

MIT — see [LICENSE](LICENSE)
