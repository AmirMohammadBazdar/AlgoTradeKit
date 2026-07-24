# AlgoTradeKit — Project Structure Reference

Read this file before making any code changes. It documents the module boundaries, public APIs, key dataclass fields, and conventions that must stay compatible across the codebase.

## Overview

```
AlgoTradeKit/
├── src/AlgoTradeKit/
│   ├── __init__.py             # __version__, __author__, lazy run_live re-export
│   ├── broker/                 # Unified exchange & MetaTrader access (v0.9.0)
│   ├── data/                   # OHLCV collection and normalisation
│   ├── indicator/              # Technical indicators (no third-party TA)
│   ├── strategy/               # BaseStrategy, Signal types, StrategyResult
│   ├── simulate/               # Backtesting engine, config, report stats
│   ├── trader/                 # Live trading + run_live paper trading (v1.0.0)
│   ├── visual/                 # Browser chart (TradingView lightweight-charts)
│   └── report/                 # Interactive HTML simulation report
├── tests/
│   ├── test_broker.py
│   ├── test_broker_mt5.py
│   ├── test_data.py
│   ├── test_data_converter.py
│   ├── test_data_normalizer.py
│   ├── test_demos.py           # demo.py / bridge_server / ichimoku mode 2 (v1.0.0)
│   ├── test_docs.py            # doc ↔ code consistency (v1.0.0)
│   ├── test_indicator.py
│   ├── test_report.py
│   ├── test_simulate.py
│   ├── test_strategy.py
│   ├── test_trader.py
│   ├── test_visual.py
│   └── test_visual__0_4_1.py
├── pyproject.toml              # version, deps, ruff/black/pytest config
├── CHANGELOG.md
├── README.md
├── CLAUDE.md                   # Instructions for Claude Code
├── MT5_WINE_SETUP.md           # Headless MT5 on Linux (Wine bridge) + Windows path
├── Publish.txt                 # Step-by-step release workflow
├── demo.py                     # MT5 smoke test (CLI: symbol/timeframe/chart host…)
└── ichimoku_strategy.py        # Example strategy script (mode 1 backtest / mode 2 live)
```

## Data flow (one-directional)

```
broker ──► data ──► indicator ──► strategy ──► simulate ──► report
                                       │           └──────► visual
                                       └──────► trader ◄────┘
```

`simulate` cross-imports from `strategy` and optionally `visual`/`report`. As of
v0.9.0, `data` imports from `broker` (candle logic + streaming); `broker` imports
from no sibling (it has its own `_timeutil` to avoid a cycle).

**v1.0.0** adds `trader`, the only module that imports from many siblings —
`broker`, `strategy`, `simulate`, and (through `simulate._live`) `visual` /
`report`. Nothing imports **from** `trader`, so it stays a leaf: the arrow only
ever points into it. `simulate` still may not import `trader` — the live
simulation emits plain dicts and `trader` maps them onto its typed events.

---

## Module: `data`

**Public API** (`data/__init__.py`)

```python
from AlgoTradeKit.data import Collector, Converter, Normalizer
```

### `Collector`
- `__init__(source, symbol, timeframe, *, broker=None)` — `source` is a venue
  **name** (`"binance-futures"`) **or** a `Broker` instance (v0.9.0, way 2)
- Settable attributes: `destination`, `outputname`, `starttime`, `endtime`
- `collect()` → path to saved CSV (gap-aware; downloads only missing candles)
- `stream(on_candle, *, closed_only=True, timeframe=None)` → real-time candles (v0.9.0)
- CSV files named `{source}_{SYMBOL}_{tf}.csv`
- REST kline logic now lives in `broker.exchange.binance` (delegated; the
  `data.sources.BinanceSource` adapter and `get_source` registry are unchanged)

### `Converter`
- Resamples OHLCV from a smaller to a larger timeframe
- Input: CSV path or `pd.DataFrame`
- Auto-detects source timeframe from timestamp gaps

### `Normalizer`
- Converts non-standard broker/MT5 CSVs to library standard
- Auto-detects timestamp unit (seconds `< 1e10` → convert to ms)
- Attributes: `start`, `end` (date filter)
- Methods: `normalize()` → DataFrame, `save(destination)` → path, `normalize_and_save()` → `(DataFrame, path)`

### DataFrame schema (library standard)

| Column | Type | Notes |
|---|---|---|
| `timestamp` | int64 | UTC milliseconds — always |
| `open` | float | |
| `high` | float | |
| `low` | float | |
| `close` | float | |
| `volume` | float | defaults to 0 if absent |

---

## Module: `broker` (v0.9.0)

**Public API** (`broker/__init__.py`)

```python
from AlgoTradeKit.broker import (
    Broker, BaseBroker, BinanceBroker, MetaTraderBroker, Stream,
    Balance, AccountInfo, Ticker, Order, OrderResult, Position, TradingCosts,
    COMMISSION_TYPE_PERCENTAGE, COMMISSION_TYPE_PER_LOT, COMMISSION_TYPE_FIXED,
    BrokerError, AuthenticationError, NotSupportedError, OrderError, ConnectionFailed,
    SIDE_BUY, SIDE_SELL, ORDER_MARKET, ORDER_LIMIT, ORDER_STOP, ORDER_STOP_LIMIT,
    TIF_GTC, TIF_IOC, TIF_FOK, POSITION_LONG, POSITION_SHORT,
    MARKET_SPOT, MARKET_FUTURES, MARKET_FOREX,
    STATUS_NEW, STATUS_PARTIALLY_FILLED, STATUS_FILLED,
    STATUS_CANCELED, STATUS_REJECTED, STATUS_EXPIRED,
)
```

### `Broker(name, ...)` — factory

Returns a concrete `BaseBroker`. `name` (case-insensitive): `"binance-spot"`,
`"binance-futures"`, `"metatrader"` (aliases `mt5` / `forex`).

```python
Broker(name, api_key=None, api_secret=None, *, market=None, testnet=False,
       recv_window=5000,
       server=None, login=None, password=None,         # MetaTrader
       mode="auto",                                    # v1.0.0 — native|bridge|auto
       host="127.0.0.1", port=18812, timeout=30.0,
       candle_poll_interval=1.0, tick_poll_interval=0.2,   # v1.0.0 — MT5 polling
       connect=True) -> BaseBroker
```

Credentials are **optional** — without them only public market-data calls work;
private calls raise `AuthenticationError`.

### `BaseBroker` — unified interface

| Group | Methods |
|---|---|
| Market data (public) | `fetch_candles`, `fetch_last_candles`, `get_candles_df`, `get_ticker`, `server_time`, `stream_candles`, `stream_ticker` |
| Costs & clock (v1.0.0) | `get_trading_costs`, `clock_offset_ms` |
| Account (private) | `get_balance`, `get_account_info` |
| Trading (private) | `create_order` (+ `create_market_order`/`create_limit_order`), `cancel_order`, `cancel_all`, `open_orders`, `open_positions`, `close_position`, `set_leverage` |
| Lifecycle | `close`, `is_authenticated` |

`fetch_candles(symbol, timeframe, start_ms, end_ms) -> list[dict]` returns the
library-standard 11-column candle schema — identical to `data.sources`, so a
`Broker` slots straight into `Collector`.

### Trading costs + venue clock (v1.0.0)

```python
broker.get_trading_costs(symbol) -> TradingCosts
# TradingCosts(commission_type, commission, spread, contract_size|None, raw)

broker.clock_offset_ms(force_refresh=False) -> int   # venue clock − local clock
```

- **Binance**: commission = the account's **taker** rate (futures
  `commissionRate`, spot `commissionRates.taker`), falling back to the standard
  taker fee when unauthenticated or rejected; spread from a book-ticker snapshot.
- **MetaTrader**: spread + `contract_size` from `symbol_info`; commission is
  reported as `0.0` / `"per_lot"` — MT5 does not expose it, so supply it via
  `TraderConfig.commission`.
- Any venue that cannot answer → the `BaseBroker` default (zero costs, reason in
  `raw`). `clock_offset_ms` is the median of 5 midpoint-corrected samples, cached
  for 5 minutes — used by the §14 candle-close scheduler so closes are never late.
- `COMMISSION_TYPE_*` deliberately carry the same string values as
  `SimulateConfig.commission_type` (`broker` may not import `simulate`).

### MetaTrader transports (v1.0.0)

`mode="auto"` (default) resolves as: a **non-default `host`** → `bridge` (you are
pointing at a remote VPS); else Windows → `native`; else → `bridge`. `"native"` /
`"bridge"` force it. The resolved choice is on `broker.mode` (`"custom"` when a
transport is injected).

| Transport | File | How it talks to MT5 |
|---|---|---|
| `BridgeClient` | `metatrader/_bridge_client.py` | TCP/JSON to `bridge_server.py` inside Wine (Linux/macOS, or any remote host) |
| `NativeTransport` | `metatrader/_native.py` | `import MetaTrader5` in-process (Windows, `pip install AlgoTradeKit[mt5]`) |

Both drive the **same** `metatrader/_ops.py` (`MT5Ops`) implementations, so the
two paths are behaviour-identical. Connection failures diagnose and stop (D4):
the message names the failing step and the exact `MT5_WINE_SETUP.md` Part that
fixes it (wine missing → A, prefix missing → B, bridge down → G).

### MetaTrader streaming (v1.0.0)

MT5 has no push feed, so `stream_candles` / `stream_ticker` **poll** the
transport (`candle_poll_interval` 1.0 s / `tick_poll_interval` 0.2 s) and return
the same stoppable `Stream` handle with the same candle dicts as the Binance
WebSocket streams. Closed candles fire exactly once, in order, deduped by
`timestamp`; `closed_only=False` also emits forming-candle updates on change.

### Types (`_types.py`) — frozen dataclasses

`Balance`, `AccountInfo`, `Ticker`, `Order`, `OrderResult`, `Position`. Each
connector maps its venue payloads onto these; unit is native (crypto base-asset
qty for Binance, lots for MetaTrader).

### Connectors

```
broker/
├── base.py                 # BaseBroker ABC (+ get_trading_costs / clock_offset_ms)
├── _types.py _errors.py _timeutil.py _stream.py   # shared internals
│                           # _stream.py: start_ws_stream + start_poll_stream
├── exchange/binance/       # BinanceBroker — spot + USD-M futures
│   ├── _endpoints.py       # live/testnet URLs + interval map
│   ├── _rest.py            # signed HMAC-SHA256 REST (stdlib crypto)
│   ├── _ws.py              # kline / bookTicker / user-data streams
│   └── _client.py          # BinanceBroker
└── metatrader/             # MetaTraderBroker (Wine bridge OR native)
    ├── bridge_server.py    # RUN INSIDE Wine python (imports MetaTrader5)
    ├── _ops.py             # MT5Ops — the operations, shared by both transports
    ├── _bridge_client.py   # TCP/JSON client (stdlib) + D4 diagnostics
    ├── _native.py          # NativeTransport — in-process MetaTrader5 (Windows)
    └── _client.py          # MetaTraderBroker — picks the transport by `mode`
```

### MetaTrader headless model

On Linux, `MetaTrader5` runs only inside the **Wine** Python on the VPS (under
`xvfb`, no GUI). `bridge_server.py` exposes it over a newline-delimited JSON
socket; `MetaTraderBroker` (normal Linux side) is a client of that bridge.
**`MetaTrader5` is not a core AlgoTradeKit dependency** → no dependency conflict;
on Windows it is the opt-in extra `pip install AlgoTradeKit[mt5]` (declared with
a `platform_system == "Windows"` marker, so it can never install on Linux).

A standalone bridge deploy is **two files** — `bridge_server.py` **and**
`_ops.py`, in the same folder (MT5_WINE_SETUP.md Part E).

### Compatibility rules when adding features

1. **New venue**: subclass `BaseBroker`, map payloads onto the shared types,
   register the name in `broker/__init__.py::Broker`. Big exchanges → a
   sub-package under `exchange/`; small ones → a single file.
2. **`broker` must not import from `data`** (would cycle) — use `broker/_timeutil.py`.
3. **Candle dicts** returned by `fetch_candles` must be the 11-column standard schema.
4. **New `*Broker` methods** that aren't universal go on the connector, not `BaseBroker`.
5. **New MT5 operations** go in `_ops.py` (both transports get them at once);
   `bridge_server.py` keeps only the lifecycle and stays stdlib + MetaTrader5.

---

## Module: `indicator`

**Public API** (`indicator/__init__.py`)

```python
from AlgoTradeKit.indicator import RSI, MACD, ATR, EMA, SMA, WMA, SMMA, DEMA, TEMA, HullMA, VWMA, VWAP, Ichimoku
```

### Construction pattern

All indicators take `pd.Series` inputs and are instantiated directly. No mutation of inputs.

```python
rsi  = RSI(close, length=14)
macd = MACD(close, fast=12, slow=26, signal=9)
atr  = ATR(high, low, close, period=14)
ichi = Ichimoku(high, low, close)
ema  = EMA(close, length=20)
```

### Key attributes

| Class | Attributes |
|---|---|
| `RSI` | `.rsi` (pd.Series) |
| `MACD` | `.macd`, `.signal`, `.histogram` |
| `ATR` | `.atr`, `.tr` — Wilder's RMA smoothing |
| `EMA/SMA/WMA/SMMA/DEMA/TEMA/HullMA/VWMA/VWAP` | `.ema`, `.sma`, etc. |
| `Ichimoku` | `.tenkan`, `.kijun`, `.senkou_a`, `.senkou_b`, `.chikou`, `.span_a_raw`, `.span_b_raw`, `.cloud_future_a`, `.cloud_future_b` |

`Ichimoku.span_a_raw`/`.span_b_raw` — current-bar values before the 26-bar displacement shift. `senkou_a`/`senkou_b` are the shifted (chart-display) versions.

### Incremental updates (v1.0.0)

Every indicator class has `update(...)` — push one new bar, get the new value,
O(1) (or bounded by the indicator's own window):

```python
ema = EMA(close, length=20)
ema.update(new_close)          # -> float; source + result series extended in place
macd.update(new_close)         # -> {"macd", "signal", "histogram"}
ichi.update(high, low, close)  # -> {"tenkan", "kijun", "senkou_a", ...}
atr.update(high, low, close)   # -> float (tr appended too)
```

Streaming state (`_EwmState` / `_WindowState` in `_base.py`) is seeded lazily by
replaying the stored history once, then carried; values are **parity-tested
against the batch computation** for all 13 classes. Because the stored series are
extended in place, properties, crossover helpers and `cloud_df()` stay correct
after streaming.

**Rule**: All indicators built from scratch — no `pandas-ta`, no `ta-lib`.

---

## Module: `strategy`

**Public API** (`strategy/__init__.py`)

```python
from AlgoTradeKit.strategy import (
    BaseStrategy, Signal, ExitSignal, StrategyResult, StrategyMode,
    # incremental / live computation (v1.0.0)
    advance_live_candle, evaluate_forming_candle,
    default_recompute_window, has_update_hook,
)
```

### `BaseStrategy` lifecycle

```
prepare_indicators(data) → setup(data) → [generate_signals(i, data) + detect_exit_signals(i, data)] × N candles
```

- `prepare_indicators(data: dict[str, pd.DataFrame]) → dict[str, pd.DataFrame]` — add indicator columns; use `.copy()` to avoid mutating caller data
- `setup(data)` — initialise stateful attributes (`self.my_list = []`); called before every `run()` — **not** `__init__()`
- `generate_signals(candle_index, data) → list[Signal]`
- `detect_exit_signals(candle_index, data) → list[ExitSignal]` — optional
- Class attributes: `primary_timeframe = "1h"`, `warmup_period = 0`
- Helper methods: `get_candle(i, data, tf)`, `latest_candle_at(tf, timestamp, data)`, `history(i, data, tf, lookback)`

### Incremental computation (v1.0.0)

Live sessions must not recompute the whole history per candle. Optional hook:

```python
def update_indicators(self, data, new_index) -> None:
    """Called exactly once per closed candle, in order, after the row is
    appended. Update the new row's `_` columns AND any custom state
    (order blocks, zones, arbitrary self.* structures)."""
```

Machinery (`strategy/_incremental.py`, stateless module functions — the caller
owns the data dict, which is what lets `LiveSimulation` trim its window):

| Function | Role |
|---|---|
| `advance_live_candle(strategy, data, candle, *, recompute_window=None)` | append the closed candle → hook **or** tail recompute → `(signals, exits)` |
| `evaluate_forming_candle(strategy, data, candle, *, recompute_window=None)` | throwaway evaluation of a forming bar; master frames and `self.*` untouched |
| `default_recompute_window(strategy)` | fallback tail size K = `max(warmup_period, 200)` |
| `has_update_hook(strategy)` | is `update_indicators` overridden? |

**Fallback (no hook)**: `prepare_indicators` is re-run over the last K rows and
the `_`-prefixed columns are spliced back **fill-only** — a cell is written the
first time the recompute produces a value and never revised (prevents a NaN band
and decaying recursive columns as the window slides). Consequences:

- exact for windowed lookback ≤ K, approximate for long EMA/RMA recursions
  unless K is generous;
- **repainting indicators and python-object state built in `prepare_indicators`
  need the hook** — build SMC/zone state in `setup()` + the hook so
  `prepare_indicators` stays side-effect-free (forming evaluation calls it).

### `Signal` fields

```python
Signal(
    direction: str,           # "long" | "short"
    entry_price: float,
    stop_loss: float,
    take_profit: float | None,
    timestamp: int,           # UTC milliseconds
    candle_index: int,
    timeframe: str,
    metadata: dict = {},
    risk_multiplier: float = 1.0,  # v0.7.3 — scales position size
)
```

`risk_multiplier > 0` required. Use `< 1.0` to split one trade idea across multiple signals that sum to one risk unit.

### `ExitSignal` fields

```python
ExitSignal(
    reason: str,              # e.g. "trend_reversal", "force_close"
    exit_price: float | None, # None = market (candle close)
    timestamp: int,           # UTC milliseconds
    candle_index: int,
    metadata: dict = {},
)
```

### `StrategyResult` fields

```python
StrategyResult(
    signals: list[Signal],
    exit_signals: list[ExitSignal],
    data: dict[str, pd.DataFrame],  # enriched with indicator columns
    mode: StrategyMode,
    drawings: list[dict],           # visual drawings — times in Unix seconds
)
```

**Drawings dict format** (times in **Unix seconds**, not ms):
```python
{"type": "hline", "price": 42000.0, "color": "#58a6ff", "label": "Support"}
{"type": "trendline", "time1": 1700000000, "price1": 40000.0, "time2": 1700003600, "price2": 41000.0}
{"type": "box", "time1": 1700000000, "price1": 41000.0, "time2": 1700007200, "price2": 42000.0}
```

### `StrategyMode`

- `StrategyMode.BACKTEST` — all candles, all signals
- `StrategyMode.LIVE` — all candles processed (state builds up), but only last-candle signals returned

---

## Module: `simulate`

**Public API** (`simulate/__init__.py`)

```python
from AlgoTradeKit.simulate import (
    Simulate, SimulationStepper, LiveSimulation,      # v1.0.0 adds the last two
    SimulateConfig, SimulateReport, ClosedTrade,
    run_batch, run_multi,
    EVENT_SIGNAL, EVENT_EXIT_SIGNAL, EVENT_OPEN,      # v1.0.0 LiveSimulation events
    EVENT_SL_MOVE, EVENT_TP_LEVEL, EVENT_CLOSE,
    TP_MODE_SIGNAL, TP_MODE_FIXED_RR, TP_MODE_MULTI_RR, TP_MODE_NONE,
    SL_MODE_SIGNAL, SL_MODE_TRAILING,
    REPORT_MODE_NONE, REPORT_MODE_WEBPAGE, REPORT_MODE_SAVE, REPORT_MODE_BOTH,
    CLOSE_REASON_SL, CLOSE_REASON_TP, CLOSE_REASON_TP_PARTIAL,
    CLOSE_REASON_RF, CLOSE_REASON_FC, CLOSE_REASON_EOD,
    # ... other constants
)
```

### `SimulateConfig` key fields

| Field | Default | Notes |
|---|---|---|
| `initial_balance` | 10000 | |
| `symbol` | `""` | required for MT5 lot sizing |
| `exchange_type` | `"exchange"` | `"exchange"` or `"metatrader"` |
| `leverage` | 1.0 | must be > 0 |
| `spread` | 0.0 | price units |
| `commission_type` | `"percentage"` | `"percentage"` / `"per_lot"` / `"fixed"` |
| `commission` | 0.001 | 0.1% per side |
| `position_sizing` | `"risk_percent"` | `"risk_percent"` / `"fixed_amount"` / `"fixed_lot"` |
| `risk_per_trade` | 1.0 | % of balance |
| `compound` | False | size off current balance if True |
| `max_positions` | 1 | total simultaneous |
| `max_long_positions` | 1 | |
| `max_short_positions` | 1 | |
| `tp_mode` | `"signal"` | `"signal"` / `"fixed_rr"` / `"multi_rr"` / `"none"` |
| `tp_rr` | 2.0 | for `fixed_rr` |
| `tp_levels` | [1,2,3] | for `multi_rr` |
| `tp_level_close_fractions` | None | v0.7.3; must sum ≤ 1.0, same length as `tp_levels` |
| `sl_mode` | `"signal"` | `"signal"` / `"trailing"` |
| `trailing_sl_percent` | 1.0 | % from peak |
| `risk_free_enabled` | False | move SL to break-even |
| `risk_free_at_rr` | 1.0 | R multiple to activate |
| `force_close_on_exit_signal` | False | |
| `show_chart` | False | v0.7.0 |
| `report_mode` | `"none"` | v0.7.0 |
| `report_save_path` | `"report.html"` | |
| `primary_timeframe` | `"1h"` | must match `StrategyResult.data` key |
| `chart_indicators` | `[]` | v0.8.0; list of indicator-spec dicts drawn on the `show_chart` chart (backend-computed) |
| `config_id` | auto-generated | |

### `ClosedTrade` key fields (frozen dataclass)

```
trade_id, symbol, direction
open_time, close_time           # UTC ms
entry_price, exit_price
initial_stop_loss, final_stop_loss
take_profit                     # None when no TP configured
size, margin_amount, risk_amount
gross_pnl, commission, net_pnl, pnl_r
close_reason                    # "sl"|"tp"|"tp_rr"|"rf"|"force_close"|"end_of_data"
rr_levels_hit
max_favourable_excursion, max_adverse_excursion
leverage, spread_paid
signal_metadata, signal_candle_index
sl_history: tuple               # v0.7.4 — ordered SL-state dicts {"time","sl","next_tp"}
final_next_tp: float | None     # v0.7.4 — TP target active at close moment
peak_price: float               # v0.7.4 — most favourable price reached
```

Properties: `.is_win`, `.is_loss`, `.is_sl`, `.is_tp`, `.is_force_close`, `.is_risk_free`, `.is_end_of_data`, `.duration_ms`

### Shared position math (v1.0.0)

`simulate/_position_math.py` holds every position-maths helper — sizing
(`compute_position_params`, `can_open_position`), TP laddering
(`build_tp_level_prices`), SL transitions (`advance_multi_rr`,
`update_trailing_sl`, `check_risk_free`), close detection (`check_close`,
`sl_reason`) and PnL accounting (`make_closed_trade`, `apply_partial_close`,
`handle_tp_level_hit`). Stateless: all state lives on the `_InternalPosition`
passed in; it never touches DataFrames or `StrategyResult`.

Consumers — `SimulationStepper`, `run_multi`, and the live `trader` — call the
**same** functions, so backtest and live behave identically by construction.

### `SimulationStepper` (v1.0.0)

The step-driven core of the engine; `Simulate._simulate_loop` is now a thin
batch driver of it, so batch results are byte-identical.

```python
stepper = SimulationStepper(config, initial_wallet=None, initial_trade_id=0,
                            record_balance_history=True)
closed = stepper.step(candle, signals=(), exit_signals=())  # -> list[ClosedTrade]
closed = stepper.finalize()          # phase G, idempotent; step() after → RuntimeError
report = stepper.build_report()      # any-time snapshot; mid-run → report.open_at_end
```

`candle` is any mapping with the standard `timestamp/open/high/low/close` keys —
broker stream dicts work as-is (extra keys ignored).

### `LiveSimulation` (v1.0.0)

`simulate/_live.py` — seed → step → window; the engine behind `run_live` (§13)
and the Trader display. Market data only: it never calls an order method.

```python
LiveSimulation(broker, strategy, config, *,
               display_candles=None, display_start=None, min_seed_candles=None,
               candle_count_limit=None, recompute_window=None,
               chart_host="127.0.0.1", chart_port=0, report_host=None, report_port=0,
               open_browser=True, on_event=None, on_report=None)
```

- **Seed** — fetch history (`display_candles` **xor** `display_start`), run the
  strategy, run the sim, render chart + report the normal way.
- **Step** — per closed candle: append → incremental strategy update → advance
  the stepper → push chart/report updates → `on_report(SimulateReport)` snapshot
  and `on_event(dict)` per trade moment.
- **Window** (`candle_count_limit`) — bounded candle deque; trades that leave the
  window are dropped and the report is recomputed over the window (baseline
  balance = equity at window start); the chart trims client-side.
- Events are plain **dicts** (`EVENT_SIGNAL` / `EVENT_EXIT_SIGNAL` / `EVENT_OPEN`
  / `EVENT_SL_MOVE` / `EVENT_TP_LEVEL` / `EVENT_CLOSE`) — `simulate` may not
  import `trader`, which maps them onto its typed events.
- Fills/SL/TP happen on **closed candles** (the engine is candle-based); forming
  candles still render live on the chart.

### Candle-loop order (`SimulationStepper.step`, driven by `_simulate_loop`)

```
A. update_trailing_peak + update_excursion for all open positions
B. check_risk_free (if enabled and not multi_rr)
C. check_close → events → make_closed_trade → wallet update → apply_partial_close
D. force-close on ExitSignal (if force_close_on_exit_signal)
E. open new positions from signals at this candle index
F. equity snapshot → balance_history
```

The phase-C helpers live in `_position_math.py` (v1.0.0; they were private
`_engine` functions before). End of data (`finalize()`, phase G): all remaining
positions closed at the final candle close (`CLOSE_REASON_EOD`).

### Partial-close invariant

When `tp_level_close_fractions` is set, multiple `ClosedTrade` records share one `trade_id`. For each slice: `frac = closed_size / pos.size`. Every dollar field (risk, margin, PnL, commission) is scaled by `frac`. The total across all slices always equals the original position's values.

### `SimulateReport` key fields

```
initial_balance, final_balance, total_pnl, total_pnl_percent
total_trades, long_trades, short_trades
win_count, loss_count, break_even_count
sl_count, tp_count, rf_count, fc_count, eod_count
win_rate, loss_rate, avg_win, avg_loss, largest_win, largest_loss, avg_pnl_r
max_consecutive_wins, max_consecutive_losses
profit_factor, expectancy, sharpe_ratio, sortino_ratio, calmar_ratio, recovery_factor
max_drawdown: DrawdownPeriod
significant_drawdowns: list[DrawdownPeriod]
weekday_stats: dict[str, WeekdayStats]
monthly_stats: dict[str, MonthStats]
session_stats: dict[str, SessionStats]
balance_history: list[dict]     # {"timestamp", "wallet", "equity"} per candle
trade_markers: list[dict]       # per-trade dict for report/chart rendering
closed_trades: list[ClosedTrade]
total_commission, total_spread_cost, avg_mae, avg_mfe
```

---

## Module: `trader` (v1.0.0)

Live trading: `run_live()` paper trading and the real-order `Trader`. Imports
from `broker`, `strategy`, `simulate` (and, through `simulate._live`, `visual` /
`report`). **Nothing imports from `trader`.**

**Public API** (`trader/__init__.py`)

```python
from AlgoTradeKit.trader import (
    run_live, Trader, TraderConfig, TraderPair,
    EXEC_CANDLE_CLOSE, EXEC_CANDLE_UPDATE, EXEC_TICK,
    DISPLAY_TRADES_SIM, DISPLAY_TRADES_REAL, DISPLAY_TRADES_BOTH,
    ON_STOP_KEEP, ON_STOP_CLOSE_ALL, CLOSE_REASON_MANUAL,
    TraderEvent, SignalEvent, OpenEvent, SlMoveEvent, RiskFreeEvent, TpLevelEvent,
    CloseEvent, ExitSignalEvent, DailyLossEvent, ReconcileEvent, ErrorEvent,
    EventStream, TerminalEventPrinter, attach_terminal_printer,
    EVENT_SIGNAL, EVENT_EXIT_SIGNAL, EVENT_OPEN, EVENT_SL_MOVE, EVENT_RISK_FREE,
    EVENT_TP_LEVEL, EVENT_CLOSE, EVENT_DAILY_LOSS, EVENT_RECONCILE, EVENT_ERROR,
    ALL_EVENT_TYPES, SOURCE_SIM, SOURCE_LIVE,
)

from AlgoTradeKit import run_live      # also lazily re-exported at the top level
```

### Files

```
trader/
├── __init__.py     # public API
├── _config.py      # TraderConfig, TraderPair, TraderSettings, validate_pairs
├── _events.py      # typed events, EventStream, TerminalEventPrinter
├── _run_live.py    # run_live() — paper trading
├── _scheduler.py   # exact candle-close scheduling off the venue clock
├── _execution.py   # order placement + venue-native SL/TP management
├── _trader.py      # Trader — loop, execution modes, safety rails, multi-pair
├── _state.py       # journal persistence + restart reconciliation
└── _display.py     # display bridge → LiveSimulation / real fills
```

### `run_live()` — paper trading (D8)

```python
run_live(strategy=MyStrategy(), broker=Broker("metatrader"), config=TraderConfig(...))
run_live(pairs=[TraderPair(broker=b, config=c, strategy=s), ...], initial_balance=10_000)
```

Runs any strategy on live market data with **simulated** positions. Never
imports `_execution` and never calls an order method — market data plus
`get_trading_costs` only. `display_trades` must stay `"sim"` (`"real"`/`"both"`
raise). Blocks until Ctrl+C or until every feed ends; returns the final
`SimulateReport` (single form) or a list in pair order.

### `Trader` — real orders

```python
Trader(broker=…, strategy=…, config=…)                # single pair
Trader(pairs=[TraderPair(...), ...], on_stop="keep",  # multi-pair / multi-venue
       state_path=None, kill_switch_file=None)
t.run()      # blocking; Ctrl+C / SIGTERM / kill-switch file = graceful shutdown
```

- **Venue-native SL/TP always** (D1): SL/TP are attached to the real order;
  trailing / risk-free / multi-RR moves **modify the venue SL**. There is no soft
  SL anywhere.
- **Execution modes** (D2): `candle_close` (default — fires at the exact venue-clock
  boundary, never late; closed-candle detection is arithmetic, not "a newer row
  appeared"), `candle_update`, `tick`.
- **Multi-RR ladders** (D5): reduce-only level orders on Binance futures;
  feed-detected market partial closes on MetaTrader.
- **Safety** (§18): `max_daily_loss` ($ or `"2%"`) halts new entries for the UTC
  day (optional flatten); `on_stop` = `"keep"` (positions stay protected by their
  venue SL/TP) or `"close_all"`; kill-switch file.
- **Persistence** (§19): state journaled to `state_path` on every change and
  reconciled against the venue on restart — journaled positions adopted (trailing
  / ladder / risk-free state resumes, lost protection re-armed), offline closes
  recorded from venue history, foreign positions warned about and left alone.
- **Multi-pair** (D9): one worker per entry, brokers may repeat (pairs on one
  broker share that account's wallet), duplicate `(broker, symbol)` rejected at
  config time.

### `TraderConfig`

Same field names as `SimulateConfig` (D12) — copy a tuned backtest straight in —
plus trader-only groups. `to_simulate_config(broker, initial_balance=…,
primary_timeframe=…)` derives the display config, filling costs from
`broker.get_trading_costs()` unless overridden.

| Group | Fields |
|---|---|
| Instrument / Data | `symbol`, `min_candles` (both required), `leverage`, `recompute_window`, `candle_poll_interval`, `tick_poll_interval` |
| Sizing / TP / SL / Limits | identical to `SimulateConfig` (`position_sizing`, `risk_per_trade`, `tp_mode`, `tp_levels`, `sl_mode`, `risk_free_*`, `max_*_positions`, …) |
| Costs (override) | `spread`, `commission_type`, `commission` — `None` = auto from the venue |
| Execution | `execution` ∈ `EXEC_CANDLE_CLOSE` / `EXEC_CANDLE_UPDATE` / `EXEC_TICK` |
| Logging | `log_events=True`, `log_event_types=None` (all) |
| Display | `display=False`, `display_trades`, `display_open_browser`, `chart_host`, `chart_port`, `report_port`, `display_candles` XOR `display_start`, `candle_count_limit` |
| Safety | `max_daily_loss`, `close_on_daily_loss` |

`TraderPair(broker, config, strategy)` is one entry of the multi-pair form;
`validate_pairs` enforces the no-duplicate-`(broker, symbol)` rule.

### Events + terminal log (D13)

Every meaningful moment is a frozen dataclass published on an `EventStream`;
v1.0.0 ships one subscriber, `TerminalEventPrinter` (one timestamped, grep-able
line per event), wired by `attach_terminal_printer(stream, config)` according to
`log_events` / `log_event_types`. Source tags: `[SIM]` (`SOURCE_SIM` — paper /
display sim) and `[LIVE]` (`SOURCE_LIVE` — real trading).

| Event | Carries |
|---|---|
| `SignalEvent` | direction, entry, SL, TP, size, risk, RR, timeframe, metadata |
| `OpenEvent` | fill price, size, margin, venue order id (`None` for a sim fill) |
| `SlMoveEvent` | old → new SL, cause (`trailing` / `ladder`) |
| `RiskFreeEvent` | RR level touched, SL → break-even |
| `TpLevelEvent` | level, fraction closed, realized PnL, new SL |
| `CloseEvent` | exit price, reason, gross/net PnL, R multiple, duration, `ClosedTrade` |
| `ExitSignalEvent` | strategy exit reason, action taken |
| `DailyLossEvent` / `ReconcileEvent` / `ErrorEvent` | safety, restart, failure detail |

Subscribers are the D14 extension point — notification backends (Telegram first)
land in v1.1 on this same stream.

### Display (D10, off by default)

`config.display=True` serves a live chart + report **beside** the trading, on a
low-priority background queue: the trading loop only enqueues, never waits, and
display work is coalesced (skip to latest) if it backs up. `display_trades`
selects the source — `"sim"` (theoretical, from a `LiveSimulation`), `"real"`
(actual broker fills, report stats from executed trades), `"both"` (overlaid,
two report sections). `display_open_browser=False` prints the URLs instead of
opening tabs (VPS); combine with `chart_host="0.0.0.0"` to make them reachable.
Two or more displaying pairs also get one **combined** report page.

---

## Module: `visual`

**Public API** (`visual/__init__.py`)

```python
from AlgoTradeKit.visual import (
    Chart,
    HorizontalLine, TrendLine, Box, Signal, TextLabel, FibRetracement, PositionBox,
    LivePosition,                                    # v1.0.0
    add_rsi, add_macd, add_ma, add_ichimoku,
    add_simulation_positions, add_strategy_drawings,
)
```

### `Chart`

- `Chart(title, ..., host="127.0.0.1", candle_count_limit=None)` — create chart
  (v1.0.0 adds both kwargs; `host="0.0.0.0"` exposes the server — see the
  security warning in the docstring)
- `Chart.from_csv(path, candle_range=None)` — factory
- `chart.set_data(df, candle_range=None)` — df with `timestamp` (ms), OHLCV columns
- `chart.show(block=False)` — open browser tab; `block=True` keeps process alive
- `chart.stream(bar_dict)` — streaming (time in seconds)
- `chart.stream_from_atk(candle_dict)` — streaming (timestamp in ms)
- `chart.add_hline(price, label, color, lineWidth, lineStyle)`
- `chart.add_box(time1, price1, time2, price2, color, opacity)`
- `chart.add_signal(time, side)` — buy/sell marker
- `chart.add_position_box(open_time, close_time, entry_price, stop_loss, take_profit, direction, net_pnl, close_reason, trade_id, rr_ratio)`
- `chart.navigate_to_candle(timestamp_ms)` — scroll to candle
- `chart.add_indicator_spec(spec)` — compute an indicator **server-side** from a
  `{"kind": ..., <params>}` dict and add it to the chart, tagging each series
  with the resolved spec (v0.8.0). Shared by the in-browser indicator toolbar
  (add/edit) and `SimulateConfig.chart_indicators`. `kind` ∈ MA family / `rsi` /
  `macd` / `atr` / `ichimoku`.

**All times passed to chart methods: Unix seconds.** (`timestamp_ms` parameter in `navigate_to_candle` is the exception — it converts internally.)

### Live push (v1.0.0)

- `chart.url` / `ChartServer.display_host` — browsable address; a `0.0.0.0` /
  `::` bind is displayed as `127.0.0.1` (bind-to-all is not a destination).
- `chart.add_live_position(...) -> id` — an **open** trade: dashed entry line +
  direction triangle, solid current-SL line coloured by the v0.7.4 zone rule,
  optional dashed next-TP line, auto-extending to the newest candle.
- `chart.update_live_position(id, stop_loss=…, next_tp=…, label=…)` — one push
  per trailing move / risk-free jump / next-TP change (`next_tp=None` clears the
  line; omitted = unchanged). Remove with `remove_drawing(id)`; on close the
  caller draws the standard final position box.
- `chart.update_drawing(id, **fields)` — generic: re-broadcasts the **full**
  serialised drawing so derived fields (posbox zones, SL colour) stay consistent.
- `chart.set_candle_limit(N | None)` — rolling window (D6): Python trims `_bars`
  in place and drops drawings whose whole time range left the window; the browser
  mirrors it (candles, volume, indicator series, cloud points, drawings) and
  restores the visible range so the view does not jump.
- Every mutation refreshes the server's cached `init` payload, so a page refresh
  mid-live-session reproduces the **current** chart, not the state at `show()`.

### `candle_range` dict modes

```python
{"start": "2024/01/01", "end": "2024/06/01"}  # inclusive range
{"start": "2024/06/01"}                         # from date to end
{"end": "2024/01/01"}                           # from start to date
{"last_n": 500}                                 # last N candles
{"first_n": 200}                                # first N candles
```

`start`/`end` accept `"YYYY/MM/DD"`, `"YYYY-MM-DD"`, datetime, Unix-second int, Unix-ms int.

### Indicator renderer helpers

```python
add_ma(chart, ma_obj, timestamps)       # any MA type — overlays on price pane
add_rsi(chart, rsi_obj, timestamps)     # RSI — adds sub-pane
add_macd(chart, macd_obj, timestamps)   # MACD — adds sub-pane with histogram
add_ichimoku(chart, ichi_obj, timestamps)

# After simulation:
add_simulation_positions(chart, report, opacity=0.15, config=sim_config)
# config is required to enable dynamic SL/TP line drawing (v0.7.4)

add_strategy_drawings(chart, strategy_result)
```

---

## Module: `report`

**Public API** (`report/__init__.py`)

```python
from AlgoTradeKit.report import (
    show_report, save_report_html, build_report_payload, ReportServer,
    show_combined_report, save_combined_report_html,      # v1.0.0
    build_combined_report_payload,                        # v1.0.0
)
```

- `show_report(report, block=False, port=None, title=None, on_open_chart=None, host="127.0.0.1")`
- `save_report_html(report, path="report.html")` — standalone HTML, no server needed
- `build_report_payload(report)` — JSON-safe dict for custom use
- `ReportServer` — FastAPI + WebSocket server (daemon thread); `host` mirrors the
  chart's (v1.0.0), with the same `0.0.0.0` → `127.0.0.1` display substitution

Mode constants match `SimulateConfig.report_mode` values: `REPORT_MODE_NONE/WEBPAGE/SAVE/BOTH`.

### Live push + combined reports (v1.0.0)

- `ReportServer.push_update(report_or_payload)` — re-broadcast fresh stats over
  the existing WebSocket; the open page re-renders in place. Accepts a
  `SimulateReport` **or** a ready payload dict (that is how combined payloads are
  pushed). Refreshes the replay cache, and carries the chart-link keys forward.
- `build_combined_report_payload(pairs)` (dict or `(label, report)` iterable) —
  D9 portfolio view: merged trade list (markers tagged `pair` + `uid`), equity
  curve **summed across accounts** over the union timeline, portfolio-wide stats,
  per-pair breakdown table, merged config bar (`"mixed"` where pairs differ).
  `show_combined_report(pairs, ...)` / `save_combined_report_html(pairs, path)`
  mirror the single-report entry points.
- A report built from **real broker fills** needs no special support: those are
  plain `ClosedTrade` records, so every path above renders them unchanged (D10).
- `report` may not import `simulate` — portfolio formulas that cannot be derived
  from per-pair numbers (streaks, sharpe/sortino/calmar, drawdowns over the summed
  curve) are mirrored locally in `_builder.py`; a single-pair combined payload is
  test-locked to equal `build_report_payload` exactly.

---

## Compatibility rules when adding features

1. **New `ClosedTrade` fields**: add with a default value (frozen dataclass), never change field order.
2. **New `SimulateConfig` fields**: add with a sensible default; validate in `__post_init__`; re-export constant from `simulate/__init__.py`.
3. **New indicator**: takes `pd.Series` inputs, exposes named attributes, add to `indicator/__init__.py` `__all__` — and give it an `update()` so live strategies stay O(1) (v1.0.0).
4. **New strategy helper**: add to `BaseStrategy` only if universally useful; don't add lifecycle methods.
5. **Chart drawing times**: always Unix seconds in dicts/models; convert ms → s before storing.
6. **Position maths**: change it in `simulate/_position_math.py` only — engine, `run_multi` and the live trader share it, and that is what makes backtest and live identical (v1.0.0). `_simulate_loop` is a thin driver of `SimulationStepper`.
7. **`simulate` purity**: `_engine.py` must never mutate `StrategyResult` or source DataFrames — the stepper never mutates the candle mapping or signals either.
8. **New `TraderConfig` fields** (v1.0.0): mirror the `SimulateConfig` name when one exists (D12); validate in `__post_init__`; carry it into `to_simulate_config()` if the display needs it; re-export any new constant from `trader/__init__.py`.
9. **New live event** (v1.0.0): add the frozen dataclass + `EVENT_*` constant in `trader/_events.py`, teach `TerminalEventPrinter` to render it, add it to `ALL_EVENT_TYPES`, and re-export — subscribers (v1.1 notifications) then get it for free.
10. **`simulate` may not import `trader`**: `LiveSimulation` emits plain dicts; the trader maps them onto typed events. Likewise `report` may not import `simulate`, and `broker` may not import `data`.

---

## Version history summary

| Version | Key additions |
|---|---|
| 0.1.0 | `data.Collector` (Binance REST) |
| 0.2.0 | `data.Converter` (timeframe resampling) |
| 0.3.0 | `visual.Chart` (browser candlestick chart) |
| 0.4.0 | `indicator` module (RSI, MACD, MA family, Ichimoku) |
| 0.4.1 | Visual bug fixes (Ichimoku cloud, crosshair, legend) |
| 0.5.0 | `strategy` module (BaseStrategy, Signal, MACDCrossoverStrategy) |
| 0.6.0 | `simulate` module (full backtesting engine + SimulateReport) |
| 0.7.0 | `report` module; `show_chart`/`report_mode`; position boxes; strategy drawings |
| 0.7.1 | Visual bug fixes (canvas clipping, navigate timing, chart_port) |
| 0.7.2 | `data.Normalizer`; `ATR`; `Ichimoku.span_a_raw`; `candle_range` filter |
| 0.7.3 | `Signal.risk_multiplier`; `tp_level_close_fractions` (partial closes) |
| 0.7.4 | `sl_history`/`final_next_tp`/`peak_price` on `ClosedTrade`; dynamic SL/TP chart lines; indicator toolbar |
| 0.8.0 | Working indicator toolbar (add **and** edit, all maths backend-computed); `Chart.add_indicator_spec`; `SimulateConfig.chart_indicators`; Ichimoku/RSI/VWAP/VWMA toolbar fixes |
| 0.9.0 | `broker` module — unified `Broker(...)` + `BaseBroker`; Binance spot/futures (REST orders/account/positions, WebSocket streams); MetaTrader connector via headless Wine bridge; `data` now uses `broker`, `Collector` accepts a name **or** a `Broker` instance + `Collector.stream()` |
| 1.0.0 | **`trader` module** — live orders (venue-native SL/TP, three execution modes, multi-RR ladders, safety rails, restart reconciliation, multi-pair/multi-venue) and **`run_live()` paper trading**; typed event stream + terminal log. MetaTrader cross-platform (`mode="auto"` — native on Windows via `[mt5]`, Wine bridge on Linux) + MT5 polling streams, `get_trading_costs()`, `clock_offset_ms()`. Incremental `update()` on all 13 indicators + the `update_indicators` strategy hook. `SimulationStepper`, shared `_position_math`, `LiveSimulation`. Live-updating chart + report (`host`, `LivePosition`, `push_update`, rolling window, combined portfolio report) |
