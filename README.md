# AlgoTradeKit

**AlgoTradeKit** is a modular Python library for building, backtesting, and
visualising algorithmic trading strategies.  Every indicator is implemented
from scratch — no `pandas-ta`, no `ta-lib` — so you have full control over
every calculation.

```bash
pip install AlgoTradeKit
```

> Requires Python 3.10+

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [data — OHLCV Collection](#data--ohlcv-collection)
3. [indicator — Technical Indicators](#indicator--technical-indicators)
4. [strategy — Signal Generation](#strategy--signal-generation)
5. [simulate — Backtesting Engine](#simulate--backtesting-engine)
6. [visual — Interactive Chart](#visual--interactive-chart)
7. [report — Simulation Report](#report--simulation-report-v070)
8. [Built-in MACD Strategy Demo](#built-in-macd-strategy-demo)
9. [Configuration Reference](#configuration-reference)

---

## Architecture Overview

```
AlgoTradeKit/
├── data/           Download and cache OHLCV candles from exchange REST APIs
├── indicator/      RSI, MACD, EMA, SMA, Bollinger Bands, ATR, Ichimoku
├── strategy/       BaseStrategy, Signal, StrategyResult, built-in strategies
├── simulate/       Backtesting engine, position management, SimulateReport
├── visual/         Interactive candlestick chart served in your browser
└── report/         Interactive simulation report web page  ← new in v0.7.0
```

Data flows in one direction:

```
data ──► indicator ──► strategy ──► simulate ──► report
                                         └──────► visual
```

---

## data — OHLCV Collection

```python
from AlgoTradeKit.data import Collector

collector = Collector(
    exchange="binance_futures",
    symbol="BTCUSDT",
    timeframes=["1h", "4h"],
    save_dir="data/",
)
data = collector.fetch(start="2024-01-01", end="2024-12-31")
# data["1h"] → pd.DataFrame  columns: timestamp(UTC ms), open, high, low, close, volume
```

Load from an existing CSV:
```python
import pandas as pd
data = {"1h": pd.read_csv("data/binance-futures_BTCUSDT_1h.csv")}
```

**Normalizing non-standard CSVs** *(v0.7.2)*

Broker / MT5 exports often use Unix-second timestamps and omit optional columns.
`Normalizer` converts any OHLCV CSV to the library standard automatically:

```python
from AlgoTradeKit.data import Normalizer

# Accepts: timestamp in seconds OR milliseconds (auto-detected)
# Required columns: timestamp, open, high, low, close
# Optional: volume (defaults to 0 if absent)
norm = Normalizer("USDJPY_1m.csv")
norm.start = "2022/01/01"   # optional date filter
norm.end   = "2024/01/01"

# Return as DataFrame (no file written)
df = norm.normalize()

# Save as library-standard CSV
path = norm.save(destination="data/")

# One-liner: normalize + save
df, path = Normalizer("USDJPY_1m.csv").normalize_and_save(destination="data/")
```

---

## indicator — Technical Indicators

All indicators are **built from scratch** — no third-party TA wrappers.

```python
from AlgoTradeKit.indicator import ATR, RSI, MACD, EMA, SMA, Ichimoku

close = df["close"]   # pd.Series

rsi  = RSI(close, length=14)           # rsi.rsi
macd = MACD(close, fast=12, slow=26, signal=9)
                                        # macd.macd, .signal, .histogram
ema  = EMA(close, length=20)           # ema.ema
sma  = SMA(close, length=50)           # sma.sma
atr  = ATR(df["high"], df["low"], close, period=14)  # atr.atr, atr.tr
ichi = Ichimoku(df["high"], df["low"], close)
                                        # ichi.tenkan / kijun / senkou_a / senkou_b
                                        # ichi.chikou / cloud_future_a / cloud_future_b
                                        # ichi.span_a_raw / span_b_raw  ← v0.7.2
```

**`ATR` — Average True Range** *(v0.7.2)*

Wilder's smoothing (RMA, `alpha = 1/period`). Matches TradingView `ta.atr()`.

```python
atr = ATR(df["high"], df["low"], df["close"], period=14)
df["atr"] = atr.atr.values   # Wilder-smoothed ATR
df["tr"]  = atr.tr.values    # raw True Range
```

**`Ichimoku.span_a_raw` / `.span_b_raw`** *(v0.7.2)*

Unshifted Senkou Span A and B (at the current bar, before the displacement shift
is applied). Useful in strategy logic where you need the live cloud value:

```python
ichi = Ichimoku(high, low, close, displacement=26)
# ichi.senkou_a   = span_a_raw.shift(26)  — displayed cloud (forward-shifted)
# ichi.span_a_raw = (tenkan + kijun) / 2  — current bar value (unshifted)
```

---

## strategy — Signal Generation

Subclass `BaseStrategy`, implement two methods, and return `Signal` objects.

```python
from AlgoTradeKit.strategy import BaseStrategy, Signal, StrategyMode

class EMAStrategy(BaseStrategy):

    def prepare_indicators(self, data):
        df = data["1h"].copy()
        df["ema20"] = EMA(df["close"], 20).value
        return {**data, "1h": df}

    def generate_signals(self, data):
        df, signals = data["1h"], []
        for i in range(1, len(df)):
            r, p = df.iloc[i], df.iloc[i - 1]
            if p["close"] < p["ema20"] and r["close"] >= r["ema20"]:
                signals.append(Signal(
                    direction="long",
                    entry_price=r["close"],
                    stop_loss=r["close"] * 0.98,
                    take_profit=r["close"] * 1.04,
                    timestamp=int(r["timestamp"]),
                    candle_index=i,
                    timeframe="1h",
                ))
        return signals

result = EMAStrategy().run(data, mode=StrategyMode.BACKTEST)
```

### Strategy Drawings (v0.7.0)

Strategies can attach visual drawings so they appear on the candle chart:

```python
# In generate_signals or prepare_indicators:
self._drawings.append({
    "type": "hline", "price": 42000.0,
    "color": "#58a6ff", "label": "Support",
})

# Return them in StrategyResult:
return StrategyResult(..., drawings=self._drawings)
```

### Built-in Strategies

| Strategy | Import |
|---|---|
| MACD Crossover | `from AlgoTradeKit.strategy.builtin.macd import MACDCrossoverStrategy` |

```python
from AlgoTradeKit.strategy.builtin.macd import MACDCrossoverStrategy

strategy = MACDCrossoverStrategy(fast=12, slow=26, signal=9,
                                  sl_atr_multiplier=1.5, timeframe="1h")
result = strategy.run(data)
```

---

## simulate — Backtesting Engine

Replay `StrategyResult` signals candle-by-candle and produce a `SimulateReport`.

### Basic Usage

```python
from AlgoTradeKit.simulate import Simulate, SimulateConfig

config = SimulateConfig(
    initial_balance=10_000,
    symbol="btcusdt",
    leverage=10,
    commission=0.001,         # 0.1% per side
    risk_per_trade=1.0,       # 1% of balance at risk
    tp_mode="fixed_rr",
    tp_rr=2.0,
    primary_timeframe="1h",
)
report = Simulate(config).run(strategy_result)
print(report)
```

### Auto Visualisation (v0.7.0)

```python
config = SimulateConfig(
    ...
    show_chart=True,          # open candle chart with position boxes
    report_mode="both",       # "none" | "webpage" | "save" | "both"
    report_save_path="report.html",
)
report = Simulate(config).run(strategy_result)
# → two browser tabs open: candle chart and report page
# → report.html saved to disk
```

### SimulateConfig Key Fields

| Field | Default | Description |
|---|---|---|
| `initial_balance` | 10 000 | Starting wallet balance |
| `leverage` | 1.0 | Leverage multiplier |
| `commission` | 0.001 | 0.1% per side (percentage mode) |
| `risk_per_trade` | 1.0 | % of balance at risk per trade |
| `tp_mode` | `"signal"` | `"signal"/"fixed_rr"/"multi_rr"/"none"` |
| `tp_rr` | 2.0 | R:R ratio for `fixed_rr` mode |
| `tp_levels` | [1,2,3] | R levels for `multi_rr` mode |
| `tp_level_close_fractions` | `None` | **v0.7.3** Fraction of original size to realise at each `tp_levels` entry (`multi_rr` only) — see below |
| `sl_mode` | `"signal"` | `"signal"` or `"trailing"` |
| `risk_free_enabled` | False | Move SL to break-even at `risk_free_at_rr` |
| `show_chart` | **False** | **v0.7.0** Open candle chart after run |
| `report_mode` | **`"none"`** | **v0.7.0** Post-run report rendering |
| `report_save_path` | `"report.html"` | **v0.7.0** HTML save path |

### Per-Signal Sizing & Partial Take-Profit (v0.7.3)

`Signal.risk_multiplier` scales one signal's size relative to the run's
sizing config — useful for varying risk by context, or for splitting one
trade idea into several sub-positions whose sizes sum to one risk unit:

```python
# Three signals at the same candle, each risking 1/3 of a normal trade,
# targeting 1R / 2R / 3R — equivalent to scaling out of one position.
for i, tp_r in enumerate([1, 2, 3], start=1):
    signals.append(Signal(
        direction="long", entry_price=entry, stop_loss=sl,
        take_profit=entry + tp_r * (entry - sl),
        timestamp=ts, candle_index=idx, timeframe="1h",
        risk_multiplier=1 / 3,
    ))
```

`SimulateConfig.tp_level_close_fractions` turns `multi_rr` into a true
scale-out — each level realises a real, partial close instead of only
moving the SL:

```python
config = SimulateConfig(
    ...,
    tp_mode="multi_rr",
    tp_levels=[1.0, 2.0, 3.0],
    tp_level_close_fractions=[1 / 3, 1 / 3, 1 / 3],  # bank 1/3 at each level
)
```

Set every fraction to `0.0` instead to get the opposite pattern — SL walks
through every level but nothing closes until the position is eventually
stopped out, letting winners run with a trailing stop and no fixed target.

### Batch Sweep

```python
from AlgoTradeKit.simulate import run_batch

reports = run_batch(strategy, data, [
    SimulateConfig(tp_rr=1.5),
    SimulateConfig(tp_rr=2.0),
    SimulateConfig(tp_rr=3.0),
])
best = max(reports, key=lambda r: r.sharpe_ratio)
```

### Multi-Pair Portfolio

```python
from AlgoTradeKit.simulate import run_multi

report = run_multi([
    (btc_strategy, btc_data, SimulateConfig(symbol="btcusdt")),
    (eth_strategy, eth_data, SimulateConfig(symbol="ethusdt")),
], initial_balance=10_000)
```

### SimulateReport

```python
report.total_pnl          # float
report.win_rate           # float (%)
report.profit_factor      # gross profit / gross loss
report.sharpe_ratio       # annualised Sharpe
report.sortino_ratio
report.calmar_ratio
report.max_drawdown       # DrawdownPeriod
report.significant_drawdowns  # list[DrawdownPeriod]
report.weekday_stats      # dict[str, WeekdayStats]
report.session_stats      # dict[str, SessionStats]  (London/NY/Tokyo/Sydney)
report.monthly_stats      # dict[str, MonthStats]
report.balance_history    # list[dict]
report.trade_markers      # list[dict]
```

---

## visual — Interactive Chart

Interactive candlestick chart served locally, based on TradingView's
[lightweight-charts](https://tradingview.github.io/lightweight-charts/).

```python
from AlgoTradeKit.visual import Chart

chart = Chart.from_csv("data/binance-futures_BTCUSDT_1h.csv")
chart.show(block=True)
```

**Candle range filter** *(v0.7.2)*

Restrict which candles are visible without modifying the source data:

```python
# Show a specific datetime range
chart = Chart.from_csv("data/btc_1h.csv",
    candle_range={"start": "2024/01/01", "end": "2024/06/01"})

# From a date to the last candle
chart = Chart.from_csv("data/btc_1h.csv",
    candle_range={"start": "2024/06/01"})

# From the first candle to a date
chart = Chart.from_csv("data/btc_1h.csv",
    candle_range={"end": "2024/01/01"})

# Last / first N candles
chart = Chart.from_csv("data/btc_1h.csv", candle_range={"last_n": 500})
chart = Chart.from_csv("data/btc_1h.csv", candle_range={"first_n": 200})

# Also available on set_data()
chart.set_data(df, candle_range={"start": "2023/01/01", "end": "2024/01/01"})
```

### Add Indicators

```python
from AlgoTradeKit.visual import add_rsi, add_macd, add_ma, add_ichimoku
from AlgoTradeKit.indicator import RSI, MACD, EMA, Ichimoku

add_rsi(chart,  RSI(df["close"]),             timestamps=df["timestamp"])
add_macd(chart, MACD(df["close"]),            timestamps=df["timestamp"])
add_ma(chart,   EMA(df["close"], 20),         timestamps=df["timestamp"], name="EMA 20")
add_ichimoku(chart, Ichimoku(df["high"], df["low"], df["close"]),
                              timestamps=df["timestamp"])
```

### Drawings

```python
chart.add_hline(price=42_000, label="Support")
chart.add_box(time1=1700000000, price1=41_000,
              time2=1700007200, price2=43_000, opacity=0.1)
chart.add_signal(time=1700000000, side="buy")
```

### Position Boxes (v0.7.0)

TradingView-style position boxes showing SL/TP zones with R:R labels:

```python
from AlgoTradeKit.visual.indicator_renderer import add_simulation_positions

chart = Chart.from_csv("data/binance-futures_BTCUSDT_1h.csv")
add_simulation_positions(chart, report, opacity=0.15)
chart.show(block=True)
```

Manual single box:
```python
chart.add_position_box(
    open_time=1700000000, close_time=1700003600,
    entry_price=50_000, stop_loss=49_500, take_profit=51_000,
    direction="long", net_pnl=100.0, close_reason="tp",
    trade_id=1, rr_ratio=2.0,
)
```

### Strategy Drawings (v0.7.0)

```python
from AlgoTradeKit.visual.indicator_renderer import add_strategy_drawings
add_strategy_drawings(chart, strategy_result)
```

### Navigate to Candle (v0.7.0)

```python
chart.navigate_to_candle(timestamp_ms=1700000000000)
```

---

## report — Simulation Report (v0.7.0)

An interactive single-page web report for a `SimulateReport`.

### Show in Browser

```python
from AlgoTradeKit.report import show_report
show_report(report, block=True)
```

### Save as Standalone HTML

```python
from AlgoTradeKit.report import save_report_html
save_report_html(report, "report.html")
```

### Report Page Sections

| Section | Contents |
|---|---|
| **Header** | Symbol, config ID, PnL badge, PDF export |
| **Config** | All SimulateConfig parameters |
| **Equity curve** | Wallet/equity line, max DD shading, DD regions, trade dots (zoom/pan) |
| **Trade tooltip** | Entry/exit, SL/TP, PnL, R-multiple, close reason + "Open Chart" button |
| **Performance KPIs** | PnL%, win rate, avg win/loss, largest win/loss, avg R |
| **Risk metrics** | Profit factor, expectancy, Sharpe, Sortino, Calmar, recovery factor |
| **Trade stats** | Total/long/short, win/loss/BE, SL/TP/RF/FC/EOD counts |
| **Drawdown table** | All DDs above threshold, sorted by severity |
| **Weekday analysis** | Trades, win%, PnL per weekday |
| **Session analysis** | London, New York, Tokyo, Sydney, Off-Hours |
| **Monthly analysis** | Per-month: trades, win%, total PnL, avg PnL |
| **Cost summary** | Commission, spread, avg MAE, avg MFE |

Clicking a trade dot and pressing **"Open on Candle Chart"** navigates the
linked chart to that trade's entry candle.

---

## Built-in MACD Strategy Demo

```python
"""demo_macd.py — run MACD strategy, open chart + report."""

import pandas as pd
from AlgoTradeKit.strategy.builtin.macd import MACDCrossoverStrategy
from AlgoTradeKit.simulate import Simulate, SimulateConfig

# Load data (replace path with your CSV)
data = {"1h": pd.read_csv("data/binance-futures_BTCUSDT_1h.csv")}

# Run strategy
strategy = MACDCrossoverStrategy(fast=12, slow=26, signal=9,
                                  sl_atr_multiplier=1.5, timeframe="1h")
result = strategy.run(data)
print(f"Signals: {result.signal_count}")

# Simulate with auto chart + report
config = SimulateConfig(
    symbol="btcusdt",
    leverage=10,
    commission=0.001,
    risk_per_trade=1.0,
    tp_mode="fixed_rr",
    tp_rr=2.0,
    show_chart=True,
    report_mode="webpage",
)
report = Simulate(config).run(result)
print(report)

# Keep servers alive
import time
try:
    while True: time.sleep(1)
except KeyboardInterrupt:
    pass
```

Run:
```bash
python demo_macd.py
```

Two browser tabs open:
- **Candle chart** — with MACD indicator and position boxes for every trade
- **Report page** — equity curve, full metrics, drawdown table, time analysis

---

## Configuration Reference

### Report Mode Constants

| Constant | Value | Behaviour |
|---|---|---|
| `REPORT_MODE_NONE` | `"none"` | No report (default) |
| `REPORT_MODE_WEBPAGE` | `"webpage"` | Open in browser |
| `REPORT_MODE_SAVE` | `"save"` | Save standalone HTML |
| `REPORT_MODE_BOTH` | `"both"` | Open + save |

### TP Mode Constants

| Constant | Value | Behaviour |
|---|---|---|
| `TP_MODE_SIGNAL` | `"signal"` | Use Signal's take_profit |
| `TP_MODE_FIXED_RR` | `"fixed_rr"` | entry ± `tp_rr` × SL distance |
| `TP_MODE_MULTI_RR` | `"multi_rr"` | Multiple levels, SL trails (and optionally partially closes — see `tp_level_close_fractions` *(v0.7.3)*) |
| `TP_MODE_NONE` | `"none"` | No TP |

### Close Reason Constants

| Constant | Value | Meaning |
|---|---|---|
| `CLOSE_REASON_SL` | `"sl"` | Stop loss hit |
| `CLOSE_REASON_TP` | `"tp"` | Take profit hit — nothing left open afterwards |
| `CLOSE_REASON_TP_PARTIAL` | `"tp_rr"` | **v0.7.3** Intermediate `multi_rr` level partially realised — position still open with reduced size |
| `CLOSE_REASON_RF` | `"rf"` | Risk-free / trailed SL hit |
| `CLOSE_REASON_FC` | `"force_close"` | Closed by an `ExitSignal` |
| `CLOSE_REASON_EOD` | `"end_of_data"` | Still open when data ran out |

### SL Mode Constants

| Constant | Value | Behaviour |
|---|---|---|
| `SL_MODE_SIGNAL` | `"signal"` | Use Signal's stop_loss |
| `SL_MODE_TRAILING` | `"trailing"` | Trail `trailing_sl_percent`% from peak |

---

## License

MIT — see [LICENSE](LICENSE).
