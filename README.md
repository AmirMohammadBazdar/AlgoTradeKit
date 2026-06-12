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
| `strategy`  | ✅ v0.5.0   | Build and combine trading strategies                   |
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
from AlgoTradeKit.indicator import RSI, MACD, EMA, Ichimoku

df = pd.read_csv("data/binance-futures_BTCUSDT_1d.csv")

rsi  = RSI(df["close"])
macd = MACD(df["close"])
ema  = EMA(df["close"], length=20)
ichi = Ichimoku(df["high"], df["low"], df["close"])
```

### Run a built-in strategy

```python
import pandas as pd
from AlgoTradeKit.strategy import MACDCrossoverStrategy, StrategyMode

df = pd.read_csv("data/binance-futures_BTCUSDT_4h.csv")

strategy = MACDCrossoverStrategy(timeframe="4h")

# Backtest — get all signals across all candles
result = strategy.run(df)
print(f"Found {result.signal_count} signals ({len(result.long_signals)} long, {len(result.short_signals)} short)")
for sig in result.signals:
    print(sig)

# Live — only the last candle's signal (for the trade module)
result_live = strategy.run(df, mode=StrategyMode.LIVE)
if result_live.has_signal:
    print("LIVE signal:", result_live.signals[0])
```

### Write your own strategy

```python
from AlgoTradeKit.strategy import BaseStrategy, Signal
from AlgoTradeKit.indicator import MACD, RSI

class MyMACDRSIStrategy(BaseStrategy):
    primary_timeframe = "4h"

    def prepare_indicators(self, data):
        df = data["4h"].copy()
        macd = MACD(df["close"])
        rsi  = RSI(df["close"])
        df["_macd"]   = macd.macd.values
        df["_sig"]    = macd.signal.values
        df["_rsi"]    = rsi.rsi.values
        data["4h"] = df
        return data

    def generate_signals(self, i, data):
        if i < 1:
            return []
        df   = data["4h"]
        curr = df.iloc[i]
        prev = df.iloc[i - 1]

        import pandas as pd
        if any(pd.isna(v) for v in [curr["_macd"], curr["_sig"], curr["_rsi"]]):
            return []

        # Long: bullish MACD crossover + RSI in healthy zone
        if prev["_macd"] < prev["_sig"] and curr["_macd"] >= curr["_sig"] \
                and 50 < curr["_rsi"] < 70:
            sl = float(df.iloc[max(0, i-10): i+1]["low"].min())
            return [Signal(
                direction="long",
                entry_price=float(curr["close"]),
                stop_loss=sl,
                take_profit=None,
                timestamp=int(curr["timestamp"]),
                candle_index=i,
                timeframe=self.primary_timeframe,
            )]
        return []

strategy = MyMACDRSIStrategy()
result = strategy.run(df)
```

### Multi-timeframe strategy

```python
class HTFFilterStrategy(BaseStrategy):
    primary_timeframe = "1h"    # loop iterates over 1h candles

    def prepare_indicators(self, data):
        for tf in data:
            df = data[tf].copy()
            df["_ema200"] = EMA(df["close"], length=200).ema.values
            data[tf] = df
        return data

    def generate_signals(self, i, data):
        curr_1h = data["1h"].iloc[i]
        ts      = int(curr_1h["timestamp"])

        # Get the most recent 4h candle at or before current time
        htf = self.latest_candle_at("4h", ts, data)
        if htf is None:
            return []

        # Only take longs when price is above the 4h EMA200
        if curr_1h["close"] > htf["_ema200"]:
            # ... your entry logic ...
            pass
        return []

data = {"1h": df_1h, "4h": df_4h}
result = HTFFilterStrategy().run(data)
```

---

## Strategy Module Reference

### BaseStrategy

Abstract base class. Subclass it and implement the required methods.

```
primary_timeframe : str   = "1h"   # timeframe the main loop iterates over
warmup_period     : int   = 0      # skip this many candles at the start
```

| Method | Required | Called | Purpose |
|---|---|---|---|
| `prepare_indicators(data)` | ✅ | Once before loop | Add indicator columns |
| `generate_signals(i, data)` | ✅ | Every candle | Detect entries, return `list[Signal]` |
| `setup(data)` | ❌ | Once before loop | Initialise stateful variables |
| `detect_exit_signals(i, data)` | ❌ | Every candle | Detect exits, return `list[ExitSignal]` |

**Entry point:** `strategy.run(data, mode=StrategyMode.BACKTEST)`

**Input formats:**
- Single timeframe: `strategy.run(df)` — wrapped as `{primary_timeframe: df}`
- Multi-timeframe: `strategy.run({"1h": df_1h, "4h": df_4h})`

**Helper methods** (available inside `generate_signals`):

```python
# Get current candle
candle = self.get_candle(i, data)                          # primary TF
candle = self.get_candle(i, data, timeframe="4h")         # other TF by index

# Cross-timeframe lookup by timestamp (no look-ahead)
htf = self.latest_candle_at("4h", curr["timestamp"], data)

# Historical slice
hist = self.history(i, data)                              # all up to i
hist = self.history(i, data, lookback=20)                 # last 20 candles
hist = self.history(i, data, timeframe="4h", lookback=5)  # other TF
```

### StrategyMode

```python
StrategyMode.BACKTEST  # all candles processed, all signals returned
StrategyMode.LIVE      # all candles processed, only last-candle signals returned
```

In LIVE mode every candle is still run — stateful strategies (POI lists, breaker blocks,
trend tracking) build up their internal state correctly by processing the full history.
Only the output is filtered to the last candle.

### Signal

```python
Signal(
    direction    = "long" | "short",
    entry_price  = float,
    stop_loss    = float,
    take_profit  = float | None,    # None = managed by simulate/trade
    timestamp    = int,             # UTC milliseconds
    candle_index = int,
    timeframe    = str,
    metadata     = dict,            # any extra data
)

signal.is_long          # bool
signal.is_short         # bool
signal.sl_distance      # abs(entry - sl)
signal.risk_reward      # tp_distance / sl_distance (or None)
```

### ExitSignal

```python
ExitSignal(
    reason      = str,              # "trend_reversal", "force_close", etc.
    exit_price  = float | None,     # None = exit at market
    timestamp   = int,
    candle_index = int,
    metadata    = dict,
)
```

### StrategyResult

```python
result.signals          # list[Signal]  — all entry signals
result.exit_signals     # list[ExitSignal]
result.data             # dict[str, pd.DataFrame] — enriched with indicators
result.mode             # StrategyMode

result.has_signal       # bool
result.signal_count     # int
result.long_signals     # list[Signal]
result.short_signals    # list[Signal]
```

### Built-in: MACDCrossoverStrategy

```python
from AlgoTradeKit.strategy import MACDCrossoverStrategy

strategy = MACDCrossoverStrategy(
    fast_length   = 12,    # TV default
    slow_length   = 26,    # TV default
    signal_length = 9,     # TV default
    sl_lookback   = 10,    # candles for swing-low/high SL
    timeframe     = "1h",
)
```

Signals: MACD/signal line crossovers.
SL: swing low (long) or swing high (short) over `sl_lookback` candles.
Exits: `ExitSignal(reason="macd_reversal")` when histogram changes sign.

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
- [x] `strategy` module — BaseStrategy framework + MACDCrossoverStrategy
- [ ] `indicator` — Bollinger Bands, ATR, Stochastic
- [ ] `simulate` module — backtesting engine with full report
- [ ] `trade` module — live trading via exchange API / MT5
- [ ] MEXC, Bybit, OKX data sources

---

## License

MIT — see [LICENSE](LICENSE)
