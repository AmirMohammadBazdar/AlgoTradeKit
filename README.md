# AlgoTradeKit

**Algorithmic Trading Toolkit** — collect market data, build indicators and strategies, backtest with a full trading simulation, and trade live.

```
pip install AlgoTradeKit
```

---

## Modules

| Module      | Status     | Description                                              |
|-------------|------------|----------------------------------------------------------|
| `data`      | ✅ v0.1.0  | Collect OHLCV candles from exchanges                     |
| `indicator` | ✅ v0.4.0  | RSI, EMA/SMA/WMA/VWAP, MACD, Ichimoku (no TA-lib)        |
| `strategy`  | ✅ v0.5.0  | Build and combine trading strategies                     |
| `simulate`  | ✅ v0.6.0  | Backtest strategies with realistic cost and TP/SL models |
| `visual`    | ✅ v0.3.0  | Interactive candlestick charts via FastAPI + WebSocket   |
| `trade`     | 🔜 planned | Live trading via exchange API or MT5                     |

---

## Quick Start

### 1 · Collect candle data

```python
from AlgoTradeKit.data import Collector

collector = Collector(source="binance-futures", symbol="BTCUSDT", timeframe="1h")
collector.destination = "data/"
collector.starttime   = "2022/01/01"
collector.collect()
```

### 2 · Compute indicators

```python
import pandas as pd
from AlgoTradeKit.indicator import EMA, RSI, MACD, Ichimoku

df  = pd.read_csv("data/binance-futures_BTCUSDT_1h.csv")
ema = EMA(length=50).calculate(df)
rsi = RSI(length=14).calculate(df)
```

### 3 · Build a strategy

```python
from AlgoTradeKit.strategy import BaseStrategy, Signal

class MyStrategy(BaseStrategy):
    primary_timeframe = "1h"
    warmup_period     = 50

    def prepare_indicators(self, data):
        data["1h"]["ema50"] = EMA(50).calculate(data["1h"])["ema50"]
        return data

    def generate_signals(self, candle_index, data):
        df  = data["1h"]
        row = df.iloc[candle_index]
        if row["close"] > row["ema50"]:
            return [Signal(
                direction="long",
                entry_price=row["close"],
                stop_loss=row["close"] * 0.98,
                take_profit=None,
                timestamp=int(row["timestamp"]),
                candle_index=candle_index,
                timeframe="1h",
            )]
        return []
```

### 4 · Simulate (backtest)

```python
from AlgoTradeKit.simulate import Simulate, SimulateConfig

config = SimulateConfig(
    initial_balance=10_000,
    symbol="btcusdt",
    exchange_type="exchange",   # or "metatrader"
    leverage=10,
    spread=0.5,                 # $0.50 bid-ask spread
    commission_type="percentage",
    commission=0.001,           # 0.1 % per side
    position_sizing="risk_percent",
    risk_per_trade=1.0,         # risk 1 % of balance per trade
    tp_mode="multi_rr",         # trail SL after each TP level
    tp_levels=[1.0, 2.0, 3.0], # close at 1R, 2R, 3R
    sl_mode="signal",
    risk_free_enabled=True,     # break-even after 1R profit
    max_positions=3,
    primary_timeframe="1h",
)

strategy = MyStrategy()
data     = {"1h": df}

result = strategy.run(data)
report = Simulate(config).run(result)

print(report)
# → <SimulateReport 'BTCUSDT_risk1.0pct_lev10x_tp_multi_rr_[1.0_2.0_3.0]'
#       trades=47 pnl=+1243.80 (+12.4%) wr=61.7%>

print(f"Profit factor : {report.profit_factor:.2f}")
print(f"Max drawdown  : {report.max_drawdown.drawdown_percent:.1f}%")
print(f"Sharpe ratio  : {report.sharpe_ratio:.2f}")
```

### 5 · Sweep configs in parallel

```python
from AlgoTradeKit.simulate import run_batch

configs = [
    SimulateConfig(risk_per_trade=0.5, tp_mode="fixed_rr", tp_rr=1.5),
    SimulateConfig(risk_per_trade=1.0, tp_mode="fixed_rr", tp_rr=2.0),
    SimulateConfig(risk_per_trade=2.0, tp_mode="fixed_rr", tp_rr=3.0),
]
reports = run_batch(strategy, data, configs)
best    = max(reports, key=lambda r: r.total_pnl)
print(f"Best: {best.config.config_id}  PnL={best.total_pnl:.2f}")
```

### 6 · Multi-symbol portfolio (shared wallet)

```python
from AlgoTradeKit.simulate import run_multi

report = run_multi([
    (btc_strategy, btc_data, SimulateConfig(symbol="btcusdt", risk_per_trade=1.0)),
    (eth_strategy, eth_data, SimulateConfig(symbol="ethusdt", risk_per_trade=0.5)),
])
print(f"Portfolio PnL: {report.total_pnl:.2f}")
```

### 7 · Visualise

```python
from AlgoTradeKit.visual import Chart

chart = Chart.from_csv("data/binance-futures_BTCUSDT_1h.csv")
chart.serve()   # opens browser at localhost:8000
```

---

## SimulateConfig reference

| Parameter | Default | Description |
|---|---|---|
| `initial_balance` | `10_000` | Starting wallet balance |
| `symbol` | `""` | Instrument (required for MT5 lot sizing) |
| `exchange_type` | `"exchange"` | `"exchange"` or `"metatrader"` |
| `leverage` | `1.0` | Leverage multiplier |
| `spread` | `0.0` | Bid-ask spread in price units |
| `commission_type` | `"percentage"` | `"percentage"` / `"per_lot"` / `"fixed"` |
| `commission` | `0.001` | Commission rate or amount |
| `position_sizing` | `"risk_percent"` | `"risk_percent"` / `"fixed_amount"` / `"fixed_lot"` |
| `risk_per_trade` | `1.0` | % of balance to risk |
| `compound` | `False` | Size off current balance (True) or initial (False) |
| `max_positions` | `1` | Max simultaneous positions |
| `max_long_positions` | `1` | Max simultaneous longs |
| `max_short_positions` | `1` | Max simultaneous shorts |
| `tp_mode` | `"signal"` | `"signal"` / `"fixed_rr"` / `"multi_rr"` / `"none"` |
| `tp_rr` | `2.0` | Risk-reward ratio for `"fixed_rr"` |
| `tp_levels` | `[1,2,3]` | R-multiples for `"multi_rr"` |
| `sl_mode` | `"signal"` | `"signal"` / `"trailing"` |
| `trailing_sl_percent` | `1.0` | Trailing SL % from peak price |
| `risk_free_enabled` | `False` | Move SL to entry at N×R profit |
| `risk_free_at_rr` | `1.0` | R-multiple that activates break-even |
| `force_close_on_exit_signal` | `False` | Close on strategy ExitSignal |
| `drawdown_threshold` | `5.0` | Report drawdowns ≥ this % |
| `primary_timeframe` | `"1h"` | TF key in StrategyResult.data |

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
pytest
```

---

## Requirements

- `pandas >= 2.0`
- `requests >= 2.28`

---

## Roadmap

- [ ] `trade` module — live trading via Binance API / MT5
- [ ] `report` module — HTML/PDF report with interactive balance chart, drawdown overlay, session heatmaps
- [ ] MEXC, Bybit, OKX data sources
- [ ] WebSocket live data bridge

---

## License

MIT — see [LICENSE](LICENSE)
