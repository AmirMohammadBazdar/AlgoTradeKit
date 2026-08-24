# AlgoTradeKit

**AlgoTradeKit** is a modular Python library for building, backtesting, and
**live-trading** algorithmic strategies.  Every indicator is implemented
from scratch — no `pandas-ta`, no `ta-lib` — so you have full control over
every calculation.

```bash
pip install AlgoTradeKit
```

> Requires Python 3.10+

**Runnable examples** live in [`examples/`](examples). Each one downloads a
little public Binance data (or falls back to synthetic candles offline) and
needs no API key:

```bash
python examples/01_backtest_report.py          # backtest → chart + report
python examples/02_chart_timeframes_replay.py  # timeframe switching + bar replay
python examples/03_multi_chart_page.py         # three synced charts on one page
```

> ### 🚀 New in v1.1.0 — the chart grew up
>
> Give the chart your finest candles once and look at them any way you like:
>
> ```python
> from AlgoTradeKit.visual import Chart, ChartPage
>
> fast = Chart(title="BTC 3m", display_timeframe="3m"); fast.set_data(df_1m)
> slow = Chart(title="BTC 5m", display_timeframe="5m"); slow.set_data(df_1m)
>
> page = ChartPage(title="BTC desk")
> page.add(fast)          # row 0
> page.add(slow)          # row 1 — stacked; row=0 would put it alongside
> page.show(block=True)
> ```
>
> * **Timeframe switching** — feed 1m candles, display any multiple, and change
>   it from the toolbar. Resampling and indicator maths happen in Python.
> * **Several charts on one page**, scrolling and crosshairing together by
>   *time*, so different timeframes stay aligned. One port, one SSH tunnel.
> * **Bar replay** — step history a candle at a time, watching the higher
>   timeframe candle build out of the lower ones. Across a whole page, every
>   chart replays from the same instant.
>
> See [`visual`](#visual--interactive-chart).
>
> **v1.0.0** brought live trading: `Trader` for real orders, `run_live()` for
> paper trading on live data, both sharing the backtest's own position maths.
> See [`trader`](#trader--live-trading-v100).

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [broker — Exchanges & MetaTrader](#broker--exchanges--metatrader-v090)
3. [data — OHLCV Collection](#data--ohlcv-collection)
4. [indicator — Technical Indicators](#indicator--technical-indicators)
5. [strategy — Signal Generation](#strategy--signal-generation)
6. [simulate — Backtesting Engine](#simulate--backtesting-engine)
7. [trader — Live Trading](#trader--live-trading-v100)
8. [visual — Interactive Chart](#visual--interactive-chart)
9. [report — Simulation Report](#report--simulation-report-v070)
10. [Built-in MACD Strategy Demo](#built-in-macd-strategy-demo)
11. [Configuration Reference](#configuration-reference)

---

## Architecture Overview

```
AlgoTradeKit/
├── broker/         Unified exchange & MetaTrader access — candles, orders, account, sockets  ← v0.9.0
├── data/           Download and cache OHLCV candles (via the broker module)
├── indicator/      RSI, MACD, EMA, SMA, ATR, Ichimoku — all with O(1) streaming updates
├── strategy/       BaseStrategy, Signal, StrategyResult, built-in strategies
├── simulate/       Backtesting engine, position management, SimulateReport
├── trader/         Live trading — real orders and run_live paper trading  ← new in v1.0.0
├── visual/         Interactive candlestick chart served in your browser
└── report/         Interactive simulation report web page  ← v0.7.0
```

Data flows in one direction:

```
broker ──► data ──► indicator ──► strategy ──► simulate ──► report
                                       │           └──────► visual
                                       └──────► trader ◄────┘
```

`trader` is a leaf: it consumes `broker`, `strategy` and `simulate`, and nothing
imports from it. That is what keeps the backtest engine free of live-trading
concerns while both share the same position maths.

---

## broker — Exchanges & MetaTrader *(v0.9.0)*

One unified door to every venue. Create a connection with `Broker(...)` and hand
the result to `data.Collector` (candles / streaming) or use it directly for
account info and orders. A Binance spot account, a Binance USD-M futures
account, and a MetaTrader forex account all expose the **same** `BaseBroker`
interface — the connector hides each venue's quirks.

**Market data — no credentials needed:**

```python
from AlgoTradeKit.broker import Broker

b = Broker("binance-futures")                       # public: no API keys
candles = b.fetch_last_candles("BTCUSDT", "1h", 500)  # list of standard candle dicts
tick    = b.get_ticker("BTCUSDT")                     # last / bid / ask
```

**Trading — credentials required** (`testnet=True` for the sandbox):

```python
b = Broker("binance-futures", api_key="…", api_secret="…", testnet=True)

b.set_leverage("BTCUSDT", 10)
res = b.create_market_order("BTCUSDT", "buy", 0.001,
                            stop_loss=58000, take_profit=72000)  # SL/TP = reduce-only orders
print(b.open_positions(), b.get_account_info().equity)
b.close_position("BTCUSDT")
```

Order placement is safe-by-default in the sense that private calls need explicit
credentials; live endpoints are the default and `testnet=True` opts into the
sandbox.

**Real-time streams** — every venue, one API:

```python
stream = b.stream_candles("BTCUSDT", "1m", lambda c: print(c["close"]), closed_only=True)
# … later …
stream.stop()
```

Binance streams over WebSocket. MetaTrader has no push feed, so *(v1.0.0)* it
polls the terminal behind the same `Stream` handle and emits the same candle
dicts — closed candles fire exactly once, in order.

**Trading costs & venue clock** *(v1.0.0)*

```python
costs = b.get_trading_costs("BTCUSDT")
costs.commission, costs.commission_type, costs.spread, costs.contract_size

b.clock_offset_ms()      # venue clock − local clock (median-sampled, cached)
```

Binance reports your account's real taker fee and a live book spread;
MetaTrader reports spread and contract size from `symbol_info`. The live trader
uses both to size positions correctly and to fire exactly on candle close.

**MetaTrader (forex) — Windows and Linux** *(v1.0.0)*

MetaTrader has no public API: the terminal speaks a proprietary protocol to your
broker's server. AlgoTradeKit picks the right transport **automatically**.

```python
mt = Broker("metatrader", server="MyBroker-Demo", login=12345678, password="***")
print(mt.mode)   # "native" on Windows, "bridge" on Linux/macOS

candles = mt.fetch_last_candles("EURUSD", "15m", 1000)
mt.create_market_order("EURUSD", "buy", 0.10, stop_loss=1.0800, take_profit=1.1000)
```

| Where you run | Transport | Setup |
|---|---|---|
| **Windows** | in-process `MetaTrader5` | `pip install AlgoTradeKit[mt5]` + the terminal installed and logged in once |
| **Linux / macOS** | Wine bridge over TCP | [`MT5_WINE_SETUP.md`](MT5_WINE_SETUP.md) |
| any OS, remote `host=` | that host's bridge | bridge running on the VPS |

Force it with `mode="native"` / `mode="bridge"` if you ever need to. Both
transports run the *same* operation implementations, so behaviour is identical.

`MetaTrader5` is **never a core dependency** — on Windows it is the opt-in
`[mt5]` extra (marker-gated so it can never install on Linux), and on Linux it
lives only inside the Wine Python. No dependency conflict either way.

When a bridge connection fails, the error names the failing step *and* the exact
setup section that fixes it — Wine missing, prefix missing, or bridge not
running — then stops. No silent fallback.

> 📘 **Headless Linux setup: [`MT5_WINE_SETUP.md`](MT5_WINE_SETUP.md).**
> Install Wine + Xvfb, run the bridge, SSH-tunnel the port, plus a Windows
> section, an error-message reference table and troubleshooting.

---

## data — OHLCV Collection

The `data` module now sits on top of `broker`. `Collector` takes either a venue
**name** *or* a ready `Broker` instance:

```python
from AlgoTradeKit.data import Collector
from AlgoTradeKit.broker import Broker

# way 1 — by name (Collector builds the connector)
c = Collector("binance-futures", "BTCUSDT", "1h")

# way 2 — bring your own Broker (e.g. authenticated, or a MetaTrader forex link)
c = Collector(Broker("metatrader", server="MyBroker-Demo", login=1, password="…"),
              "EURUSD", "15m")

# real-time candles for either venue
stream = c.stream(lambda candle: print(candle["close"]))
```

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

**Resampling in memory** *(v1.1.0)*

`Converter` writes CSVs; when you just want the candles, the same maths is a
plain function:

```python
from AlgoTradeKit.data import resample_ohlcv, detect_timeframe, can_convert

detect_timeframe(df)                # "1m"
resample_ohlcv(df, "5m")            # -> DataFrame, timestamp in UTC ms
resample_ohlcv(df, "5m", drop_incomplete=False)   # keep the candle still forming
can_convert("4h", "6h")             # False — not a whole multiple
```

No printing, no files, and the input frame is never modified. This is what the
chart's timeframe switching runs on, so a resample you do yourself and one the
chart does are the same numbers.

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

### Streaming updates *(v1.0.0)*

Every indicator can consume one new bar at a time — O(1), no recompute:

```python
ema = EMA(close, length=20)
ema.update(new_close)                     # -> float
macd.update(new_close)                    # -> {"macd", "signal", "histogram"}
atr.update(new_high, new_low, new_close)  # -> float
ichi.update(new_high, new_low, new_close) # -> {"tenkan", "kijun", ...}
```

The stored source and result series are extended in place, so properties and
crossover helpers stay correct afterwards. Values are **parity-tested against
the batch computation** for all 13 indicator classes — streaming and batch agree.

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

### Live / Incremental Computation *(v1.0.0)*

A live session must not recompute the whole history every candle. Add an
optional hook and each new candle costs O(1):

```python
class MyStrategy(BaseStrategy):

    def setup(self, data):
        self.zones = []          # custom state lives here, not in prepare_indicators

    def update_indicators(self, data, new_index):
        """Called exactly once per closed candle, after the row is appended."""
        df = data[self.primary_timeframe]
        df.loc[new_index, "_ema20"] = self._ema.update(df.loc[new_index, "close"])
        self.zones = self._rebuild_zones(df, new_index)   # any custom state too
```

**The hook is optional.** Without it the library falls back to recomputing
`prepare_indicators` over the last K candles and splicing the `_`-prefixed
columns back — accurate for windowed indicators, and never revising a value it
already wrote. Implement the hook if your strategy repaints or builds Python
objects (order blocks, zones, SMC structures) during `prepare_indicators`.

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
| `chart_indicators` | `[]` | **v0.8.0** Indicator specs drawn on the `show_chart` chart (backend-computed) — see below |

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

### Candle-by-Candle Stepping *(v1.0.0)*

The engine core is also usable one candle at a time — the batch loop is a thin
driver over it, so results stay byte-identical:

```python
from AlgoTradeKit.simulate import SimulationStepper

stepper = SimulationStepper(config)
for candle in feed:                                  # broker stream dicts work as-is
    closed = stepper.step(candle, signals=sigs)      # -> trades closed this candle
    snapshot = stepper.build_report()                # any-time report snapshot
stepper.finalize()                                   # close leftovers at the last close
```

`LiveSimulation` builds on it: seed history → simulate → keep stepping a live
feed, pushing chart and report updates each closed candle. It is what powers
`run_live()` and the trader's display, and it never places an order.

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

## trader — Live Trading *(v1.0.0)*

Two entry points, one config class. `run_live()` paper-trades on live market
data; `Trader` places real orders. Both take the **same** `TraderConfig`, whose
field names mirror `SimulateConfig` — a tuned backtest config copy-pastes across.

### Paper Trading — `run_live()`

Watch a strategy trade the current market without opening a position. Order code
is never even imported; the simulation engine fills the trades, with the venue's
real costs applied.

```python
from AlgoTradeKit.broker import Broker
from AlgoTradeKit.trader import TraderConfig, run_live

config = TraderConfig(
    symbol="BTCUSDT",
    min_candles=500,          # history the strategy needs before it may trade
    risk_per_trade=1.0,
    tp_mode="multi_rr",
    tp_levels=[1.0, 2.0, 3.0],
    display=True,             # live chart + live report
    display_candles=1000,     # seed the display with the last 1000 candles
    log_events=True,          # per-event terminal log
)

report = run_live(strategy=MyStrategy(),
                  broker=Broker("binance-futures"),
                  config=config)
```

Blocks until Ctrl+C, then returns the final `SimulateReport`.

### Real Orders — `Trader`

```python
from AlgoTradeKit.trader import Trader

trader = Trader(
    broker=Broker("binance-futures", api_key="…", api_secret="…"),
    strategy=MyStrategy(),
    config=config,
    state_path="trader_state.json",     # journal for restart recovery
    kill_switch_file="STOP",            # touch this file to shut down
    on_stop="keep",                     # or "close_all"
)
trader.run()          # blocks; Ctrl+C / SIGTERM shut down gracefully
```

**Stop-losses are always venue-native.** SL and TP are attached to the real
order, and trailing / risk-free / multi-RR moves *modify the venue SL*. There is
no soft stop watching price in Python anywhere — if your process dies, your stop
is still on the exchange.

### What it does for you

| | |
|---|---|
| **Execution modes** | `candle_close` (fires at the exact venue-clock boundary — never late, even when the venue prints no candle until its first trade), `candle_update`, `tick` |
| **Position management** | Trailing SL, risk-free moves and multi-RR ladders, all from the **same maths the backtest uses** |
| **Multi-RR ladders** | Venue-native: reduce-only level orders on Binance futures, feed-detected partial closes on MetaTrader |
| **Safety** | `max_daily_loss` (`500` or `"2%"`) halts new entries for the UTC day, optional flatten; kill-switch file; graceful shutdown |
| **Restart recovery** | State is journaled on every change and reconciled against the venue on start — open positions are adopted with their trailing/ladder state, offline closes are recovered from venue history, foreign positions are left untouched |
| **Multi-pair** | One worker per pair, brokers may repeat, one combined report |

### Event Log

Every meaningful moment is a typed event on a stream, printed as one grep-able
line (`log_events=True`, filter with `log_event_types`):

```
[LIVE][BTCUSDT] SIGNAL long @ 64250.0 sl=63800.0 tp=65150.0 rr=2.00 risk=$100.00
[LIVE][BTCUSDT] OPEN    long 0.015 @ 64251.5 margin=$96.38 order=8412…
[LIVE][BTCUSDT] SL_MOVE 63800.0 -> 64251.5 (risk-free, rr=1.00)
[LIVE][BTCUSDT] CLOSE   @ 65150.0 reason=tp net=$+198.40 R=+1.98 held=4h12m
```

`[SIM]` tags paper trades, `[LIVE]` real ones. Subscribers are the extension
point for notification backends.

### Multi-Pair / Multi-Venue

```python
from AlgoTradeKit.trader import Trader, TraderPair

trader = Trader(pairs=[
    TraderPair(broker=binance, config=cfg_btc, strategy=StratA()),
    TraderPair(broker=mt5,     config=cfg_eur, strategy=StratB()),
    TraderPair(broker=mt5,     config=cfg_gbp, strategy=StratB()),
])
trader.run()
```

Pairs on the same broker share that account's wallet naturally. Every pair gets
its own chart, report and event log, plus **one combined portfolio report** —
merged trades, equity summed across accounts, per-pair breakdown.

### Live Display

Optional and **off by default** — display work runs on a low-priority background
queue, so it can never slow the trading loop.

```python
TraderConfig(
    display=True,
    display_trades="sim",           # "sim" | "real" | "both"
    display_open_browser=False,     # VPS: print the URLs instead of opening tabs
    chart_host="0.0.0.0",           # reachable from another machine
    chart_port=8080, report_port=8081,
    candle_count_limit=2000,        # rolling window keeps memory bounded
)
```

`"sim"` shows theoretical strategy performance, `"real"` shows your actual
fills, `"both"` overlays them.

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
add_simulation_positions(chart, report, opacity=0.15, config=sim_config)
chart.show(block=True)
```

**Dynamic SL/TP lines *(v0.7.4)*** — when `config` is passed, coloured
horizontal lines are drawn on the chart for every trade where the SL moved:

| Colour | Meaning |
|--------|---------|
| 🔴 Red | SL is in the loss zone (below entry for long) |
| 🟡 Amber | SL is at break-even (entry price) |
| 🔵 Cyan | SL is in profit territory |
| 🟢 Green | Next TP target (`multi_rr` only) |

The position box's profit-zone boundary is also corrected to show the TP
target that was active at the *moment of closing* rather than the initial TP
at entry.  For trailing-SL trades the box top is set to `peak_price` (the
highest price the trailing SL ever chased).

Manual single box:
```python
chart.add_position_box(
    open_time=1700000000, close_time=1700003600,
    entry_price=50_000, stop_loss=49_500, take_profit=51_000,
    direction="long", net_pnl=100.0, close_reason="tp",
    trade_id=1, rr_ratio=2.0,
)
```

### Indicator Toolbar *(v0.8.0)*

The chart toolbar has an **INDICATORS** button on the **left**.  Clicking it
opens a panel where indicators can be added — and edited — interactively at
runtime, no code required:

- **Moving averages**: EMA, SMA, WMA, SMMA, DEMA, TEMA, HMA, VWMA, VWAP
- **Oscillators**: RSI (with optional MA), MACD, ATR
- **Trend**: Ichimoku Cloud (Tenkan / Kijun / Senkou B / Displacement)

Each type shows its own parameter form.  After clicking *Add to Chart*, the
**backend** computes the indicator from the OHLCV data and the chart updates
immediately (no maths in the browser).  Every indicator on the chart gets a
⚙ gear icon in the legend — click it to re-open the panel pre-filled with the
current settings; saving recomputes it server-side.

### Indicators on the Simulation Chart *(v0.8.0)*

When `show_chart=True`, pass `chart_indicators` to draw indicators on the
simulation chart automatically.  Use the same parameters your strategy trades
on to mirror it exactly, add extra indicators, or both — all computed in the
backend before the chart opens (and still editable live via the toolbar):

```python
config = SimulateConfig(
    show_chart=True,
    chart_indicators=[
        {"kind": "ichimoku", "tenkan": 8, "kijun": 22,
         "senkou_b": 44, "displacement": 22},   # match the strategy
        {"kind": "rsi", "period": 14, "source": "close"},
        {"kind": "ema", "period": 200, "source": "close"},  # extra
    ],
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

### Live Updates *(v1.0.0)*

```python
chart = Chart(host="0.0.0.0", candle_count_limit=2000)   # host default: 127.0.0.1
print(chart.url)                                          # print instead of opening a tab

pid = chart.add_live_position(open_time=…, entry_price=…, stop_loss=…,
                              direction="long", next_tp=…)
chart.update_live_position(pid, stop_loss=new_sl)         # trailing / risk-free move
chart.set_candle_limit(2000)                              # rolling window, browser mirrors it
```

An open trade draws as a dashed entry line plus a live SL line, colour-coded by
zone (red = loss, amber = break-even, cyan = profit) and a dashed next-TP line.
Every change is pushed to the open page, and refreshing the browser mid-session
replays the **current** chart, not the state it started in.

> ⚠️ `host="0.0.0.0"` exposes the chart to anyone who can reach the port. An SSH
> tunnel is the safer way to view a VPS chart.

---

## report — Simulation Report (v0.7.0)

An interactive single-page web report for a `SimulateReport`.

### Timeframe Switching *(v1.1.0)*

Give the chart its finest candles and display any whole multiple of them.
The resampling — and the indicator maths on top of it — happens in Python; the
browser only asks for a timeframe.

```python
chart = Chart.from_csv("data/btc_1m.csv", display_timeframe="5m")
chart.add_indicator_spec({"kind": "ema", "period": 50})
chart.show(block=True)          # a 1m / 3m / 5m / 15m … selector sits in the toolbar

chart.set_timeframe("15m")      # or from Python; same code path as a click
chart.set_timeframe(None)       # back to the source candles
chart.source_timeframe          # detected from the data, or set it explicitly
chart.timeframes                # what the selector offers
```

Only whole multiples are offered: `4h` data can become `12h` or `1d`, never
`6h`. Restrict the list with `timeframes=["5m", "15m", "1h"]`.

Indicators are handled by where they came from:

| Added by | On a timeframe change |
|---|---|
| `add_indicator_spec` / `chart_indicators` / the toolbar | **recomputed** on the new candles — exact |
| `add_indicator_from_atk`, `add_rsi`, `add_macd`, `add_ichimoku` | **thinned** last-value-per-bucket and flagged approximate |

Drawings and position boxes are absolute-time, so they stay put.

A partly-covered *first* candle is dropped — 1m data starting at 09:55 would
otherwise show a 09:45 fifteen-minute candle claiming an open it never had. The
partly-covered *last* candle is kept, because that one is still forming.

### Several Charts on One Page *(v1.1.0)*

```python
from AlgoTradeKit.visual import Chart, ChartPage

page = ChartPage(title="BTC desk")
page.add(fast)             # row 0
page.add(slow)             # row 1 — a new row, stacked underneath
page.add(eth, row=1)       # beside slow — side by side
page.show(block=True)
```

Rows stack top to bottom and the charts inside a row sit left to right, with
dividers you can drag on both axes.

Charts scroll and crosshair together **by time**, so a 3m and a 1h chart stay
on the same instant instead of the same candle number. Both syncs toggle from
the page header, or at construction with `sync_time=` / `sync_crosshair=`.

Everything — the page, every chart and every WebSocket — is served from **one
port**, so viewing a VPS page still needs only one SSH tunnel.

Attaching a chart changes nothing about how you drive it: indicators, drawings,
live positions, streaming and `set_timeframe` all work exactly as they do on a
chart of its own. Charts can also be added after `show()`.

### Bar Replay *(v1.1.0)*

Press **⏵⏵ REPLAY**, click a candle, and the chart forgets everything after it.

* `▶|` / `◀` step, `▶` plays at 0.25×–10×, `⏮` returns to the start, `✕` leaves.
* Keyboard: `Space`, `Shift+→`, `Shift+←`, `Esc`.
* A step is one **source** candle, so a chart showing 5m built from 1m data
  advances a minute at a time and you watch the 5m candle being built.
* Indicators stop at the last **closed** candle. Series that legitimately run
  ahead of the data — Ichimoku's projected spans — keep that overhang.
* Backtest positions do not give the future away: a trade that has not opened
  is not drawn, and one still open at the cursor stops there with no result
  label.

On a `ChartPage` the replay bar lives in the page header and drives every chart
from one cursor, stepping by the finest source timeframe on the page.

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

Clicking a trade dot on the balance chart pins its details open *(v1.1.0 —
before that the box followed the pointer and closed before you could reach it)*.
It stays until you click elsewhere or press `Esc`, so **"Open on Candle Chart"**
is clickable: it opens the linked chart at that trade's entry candle.

### Live Updates & Portfolio Reports *(v1.0.0)*

```python
from AlgoTradeKit.report import ReportServer, show_combined_report

server = ReportServer(host="127.0.0.1")
server.push_update(fresh_report)     # re-renders the open page in place
```

A **combined report** aggregates several pairs into one page — merged trade
list, equity curve summed across accounts, and a per-pair breakdown table:

```python
show_combined_report({"BTCUSDT": btc_report, "EURUSD": eur_report})
```

A report built from **real broker fills** is an ordinary `SimulateReport`, so
every path above renders live trading exactly like a backtest.

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

### Trader Constants *(v1.0.0)*

`from AlgoTradeKit.trader import …`

| Constant | Value | Behaviour |
|---|---|---|
| `EXEC_CANDLE_CLOSE` | `"candle_close"` | Evaluate once per closed candle, at the exact venue-clock boundary (default) |
| `EXEC_CANDLE_UPDATE` | `"candle_update"` | Re-evaluate on every forming-candle update |
| `EXEC_TICK` | `"tick"` | Re-evaluate on every tick |
| `DISPLAY_TRADES_SIM` | `"sim"` | Display shows theoretical simulated trades (default) |
| `DISPLAY_TRADES_REAL` | `"real"` | Display shows actual broker fills |
| `DISPLAY_TRADES_BOTH` | `"both"` | Real fills overlaid on simulated trades |
| `ON_STOP_KEEP` | `"keep"` | On shutdown, leave positions open under their venue SL/TP (default) |
| `ON_STOP_CLOSE_ALL` | `"close_all"` | On shutdown, market-close everything and cancel working orders |

Event types — the domain of `log_event_types`, and `ALL_EVENT_TYPES` is the full
set: `EVENT_SIGNAL`, `EVENT_EXIT_SIGNAL`, `EVENT_OPEN`, `EVENT_SL_MOVE`,
`EVENT_RISK_FREE`, `EVENT_TP_LEVEL`, `EVENT_CLOSE`, `EVENT_DAILY_LOSS`,
`EVENT_RECONCILE`, `EVENT_ERROR`.

---

## License

MIT — see [LICENSE](LICENSE).
