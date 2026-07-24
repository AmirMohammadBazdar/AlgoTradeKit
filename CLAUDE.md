# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **Warning — broken git metadata:** the git history and git status in this
> folder are broken/stale and do NOT reflect the current state of the code.
> The working-tree files are the only source of truth. Ignore git history,
> git status, and branch information entirely.

## Commands

```bash
# Install in editable mode with dev dependencies
pip install -e ".[dev]"

# Run all tests
pytest

# Run a single test file
pytest tests/test_simulate.py -v

# Run a single test by name
pytest tests/test_simulate.py::TestSlHistory::test_sl_history_initial_entry -v

# Lint
ruff check src/ tests/

# Format
black src/ tests/

# Build distribution
python -m build
twine upload dist/*

# --- v1.0.0 extras ---

# Native MetaTrader mode (Windows only — never install MetaTrader5 on Linux)
pip install "AlgoTradeKit[mt5]"

# MT5 smoke test — no args lists the broker's symbols, then chart one
python demo.py
python demo.py --symbol EURUSD --timeframe 15m --count 500 --no-open-browser

# Run the MT5 bridge inside the Wine Python (VPS; needs bridge_server.py + _ops.py)
xvfb-run wine python bridge_server.py --host 127.0.0.1 --port 18812

# Live paper trading demo (ichimoku_strategy.py with RUN_MODE = 2)
python ichimoku_strategy.py
```

Line length: 100. Ruff selects E, F, I, UP rules. Target Python: 3.10+.

## Architecture

Data flows in one direction:

```
broker ──► data ──► indicator ──► strategy ──► simulate ──► report
                                       │           └──────► visual
                                       └──────► trader ◄────┘
```

Each module is independent. `simulate` imports from `strategy` and `visual`/`report`; `data` imports from `broker` (v0.9.0). `broker` imports from no sibling. `trader` (v1.0.0) imports from `broker`, `strategy` and `simulate` — and nothing imports **from** `trader`, so it stays a leaf. All other cross-module coupling goes through well-defined dataclasses.

Import bans that matter: `simulate` must not import `trader` (the live sim emits plain dicts; the trader maps them to typed events), `report` must not import `simulate`, `broker` must not import `data`.

### `broker/` (v0.9.0, extended in v1.0.0)

Unified access to every venue. `Broker(name, ...)` → concrete `BaseBroker`.
`name` ∈ `"binance-spot"` / `"binance-futures"` / `"metatrader"` (aliases `mt5`,
`forex`). Credentials optional (public market-data works without them; private
calls raise `AuthenticationError`).

| Class | Role |
|---|---|
| `Broker` | Factory → concrete `BaseBroker` for the named venue |
| `BaseBroker` | Unified interface: `fetch_candles`/`fetch_last_candles`, `get_ticker`, `stream_candles`/`stream_ticker`, `get_trading_costs`, `clock_offset_ms`, `get_balance`/`get_account_info`, `create_order`, `cancel_order`, `open_orders`, `open_positions`, `close_position`, `set_leverage` |
| `BinanceBroker` | `exchange/binance/` — spot + USD-M futures; REST (signed HMAC, stdlib), WebSocket streams, user-data stream |
| `MetaTraderBroker` | `metatrader/` — forex via the **Wine bridge** or the **native** Windows transport |

**MetaTrader is cross-platform (v1.0.0).** `mode="auto"` (default): a non-default
`host` → bridge; else Windows → native (`import MetaTrader5`, installed via
`pip install AlgoTradeKit[mt5]`); else bridge. `"native"`/`"bridge"` force it;
the resolved value is `broker.mode`. Both transports drive the same
`metatrader/_ops.py`, so behaviour is identical. On Linux `MetaTrader5` runs only
in the **Wine** Python via `metatrader/bridge_server.py` (under `xvfb`, JSON
socket); a standalone bridge deploy is **two files** — `bridge_server.py` +
`_ops.py`. `MetaTrader5` is **not** a core dependency → no conflict. Bridge
connection failures diagnose and stop, naming the `MT5_WINE_SETUP.md` Part that
fixes them (D4).

MT5 streaming is **polling** over the transport (`candle_poll_interval` 1.0 s /
`tick_poll_interval` 0.2 s) behind the same `Stream` handle Binance uses.
`get_trading_costs(symbol) -> TradingCosts` feeds the trader's auto-derived
display config; `clock_offset_ms()` (median of 5 samples, 5-min cache) is what
makes candle-close firing exact. Unified types live in `broker/_types.py`;
`broker` has its own `_timeutil.py` so it never imports `data` (avoids a cycle).

### `data/`

| Class | Role |
|---|---|
| `Collector` | Fetches OHLCV candles via the `broker` module. `Collector(source, symbol, timeframe)` takes a venue name **or** a `Broker` instance; `.collect()` saves CSV (`source_SYMBOL_tf.csv`, gap-aware), `.stream()` gives real-time candles. |
| `Converter` | Resamples OHLCV data from one timeframe to a larger one. |
| `Normalizer` | Ingests non-standard CSVs (MT5/broker exports) and converts to library standard. Auto-detects seconds vs milliseconds timestamps. |
| `BinanceSource` | Concrete `BaseSource` for Binance REST API. |
| `CSVHandler` | Reads/writes library-standard CSV files. |

All timestamps in Python are **UTC milliseconds** throughout the library.

### `indicator/`

All indicators are built from scratch — no pandas-ta, no ta-lib. Each class takes `pd.Series` inputs and exposes named attributes.

| Class | Key attributes |
|---|---|
| `RSI` | `.rsi` — Wilder's RMA smoothing (matches TradingView) |
| `MACD` | `.macd`, `.signal`, `.histogram` |
| `ATR` | `.atr`, `.tr` — Wilder's RMA smoothing (matches TradingView `ta.atr()`) |
| `EMA/SMA/WMA/SMMA/DEMA/TEMA/HullMA/VWMA/VWAP` | Various MA types |
| `Ichimoku` | `.tenkan`, `.kijun`, `.senkou_a`, `.senkou_b`, `.chikou`, `.span_a_raw`, `.span_b_raw` |

`span_a_raw`/`span_b_raw` are unshifted (current bar, before the displacement). The regular `.senkou_a`/`.senkou_b` are shifted 26 bars forward for display.

Every indicator also has `update(...)` (v1.0.0) — push one new bar, get the new value, O(1); the stored source and result series are extended in place, and the values are parity-tested against the batch computation.

### `strategy/`

`BaseStrategy` (abstract) lifecycle — always in this order:

1. `prepare_indicators(data)` — compute all indicator columns, called once
2. `setup(data)` — initialise stateful variables, called once
3. `generate_signals(candle_index, data)` — per-candle, returns `list[Signal]`
4. `detect_exit_signals(candle_index, data)` — per-candle, returns `list[ExitSignal]` (optional)

`run()` is not overridable and orchestrates the lifecycle. Returns `StrategyResult`.

Key types:

- `Signal` — direction, entry_price, stop_loss, take_profit, timestamp (UTC ms), candle_index, timeframe, metadata, `risk_multiplier` (scales position size, v0.7.3)
- `ExitSignal` — reason, exit_price, timestamp, candle_index
- `StrategyResult` — signals, exit_signals, data (enriched), mode, drawings (Unix seconds for chart)
- `StrategyMode.BACKTEST` — all signals; `StrategyMode.LIVE` — last candle signals only

Subclasses set `primary_timeframe` (default `"1h"`) and `warmup_period`. All stateful variables must be initialised in `setup()`, not `__init__()`.

Indicator columns added in `prepare_indicators()` are prefixed `_` (e.g. `_macd`, `_signal`).

Built-in strategies live in `strategy/builtin/`. Currently: `MACDCrossoverStrategy`.

**Live/incremental (v1.0.0)** — optional 5th lifecycle method plus the machinery in `strategy/_incremental.py`:

- `update_indicators(data, new_index)` — opt-in hook, called exactly once per closed candle, in order, after the row is appended. Update the new row's `_` columns **and** any custom state (order blocks, zones, arbitrary `self.*`).
- `advance_live_candle(strategy, data, candle, *, recompute_window=None)` → `(signals, exits)`; `evaluate_forming_candle(...)` for throwaway forming-bar evaluation (never mutates master frames or committed state); `default_recompute_window(strategy)` = `max(warmup_period, 200)`; `has_update_hook(strategy)`.
- Without the hook the fallback re-runs `prepare_indicators` over the last K rows and splices `_` columns back **fill-only** (a cell is written once, never revised). Repainting indicators and python-object state built in `prepare_indicators` need the hook — build such state in `setup()` + hook.

### `simulate/`

`Simulate(config).run(strategy_result)` → `SimulateReport`

**`SimulateConfig`** (`_config.py`) — key parameter groups:

| Group | Fields |
|---|---|
| Instrument | `initial_balance`, `symbol`, `exchange_type` (`"exchange"`/`"metatrader"`), `leverage` |
| Costs | `spread`, `commission_type` (`"percentage"`/`"per_lot"`/`"fixed"`), `commission` |
| Sizing | `position_sizing` (`"risk_percent"`/`"fixed_amount"`/`"fixed_lot"`), `risk_per_trade`, `fixed_amount`, `fixed_lot`, `compound` |
| TP | `tp_mode` (`"signal"`/`"fixed_rr"`/`"multi_rr"`/`"none"`), `tp_rr`, `tp_levels`, `tp_level_close_fractions` |
| SL | `sl_mode` (`"signal"`/`"trailing"`), `trailing_sl_percent` |
| Risk-free | `risk_free_enabled`, `risk_free_at_rr` |
| Limits | `max_positions`, `max_long_positions`, `max_short_positions` |
| Display | `show_chart`, `report_mode` (`"none"`/`"webpage"`/`"save"`/`"both"`), `report_save_path`, `chart_indicators` (v0.8.0) |

All string constants are re-exported from `simulate/__init__.py` (e.g. `TP_MODE_MULTI_RR`, `CLOSE_REASON_SL`).

**Core loop** — `SimulationStepper.step()` (v1.0.0); `Simulate._simulate_loop()` is a thin batch driver of it, so batch results stay byte-identical:

Per-candle order: A) trailing-peak update + excursion tracking → B) risk-free SL check → C) SL/TP close check → D) ExitSignal force-close → E) open new positions → F) equity snapshot. End-of-data (`finalize()`): all open positions closed at final candle close.

**Key internals:**

- `_InternalPosition` (mutable) — live state: `stop_loss`, `peak_price`, `sl_history`, `next_tp`, `last_rr_hit`, `size`, `original_size`, `tp_level_prices`
- `ClosedTrade` (frozen dataclass) — permanent record: includes `sl_history` (tuple of dicts), `final_next_tp`, `peak_price` (v0.7.4)
- **`_position_math.py` (v1.0.0)** — all position maths in one stateless module: `compute_position_params`, `can_open_position`, `build_tp_level_prices`, `advance_multi_rr`, `update_trailing_sl`, `check_risk_free`, `check_close`, `sl_reason`, `make_closed_trade`, `apply_partial_close`, `handle_tp_level_hit`. The engine, `run_multi` **and the live trader** all call these — change maths here or nowhere.
- `make_closed_trade()` — builds `ClosedTrade` from position; `frac` math scales all dollar fields for partial closes
- `apply_partial_close()` — shrinks `_InternalPosition` in place; commission/risk/size stay consistent across slices sharing one `trade_id`
- `tp_level_close_fractions`: when set, each multi-RR level triggers a real partial close; when `None`, only the final level closes (pre-v0.7.3 behaviour)

**Step-driven API (v1.0.0)** — `SimulationStepper(config, initial_wallet=None, initial_trade_id=0, record_balance_history=True)`: `step(candle, signals=(), exit_signals=())` → trades closed this candle, `finalize()` (idempotent; `step()` after it raises), `build_report()` any-time snapshot (mid-run open positions surface in `report.open_at_end`). `candle` is any mapping with the standard OHLCV keys — broker stream dicts work as-is.

**`LiveSimulation`** (`_live.py`, v1.0.0) — seed → step → window; the engine behind `run_live` and the Trader display. Market data only (never an order call). Emits plain **dicts** (`EVENT_*` from `simulate/__init__.py`) because `simulate` may not import `trader`. `candle_count_limit` bounds candles, drops trades that left the window and recomputes the report over it.

**Runners** (`_runner.py`):

- `run_batch(strategy, data, configs)` — parallel `ThreadPoolExecutor`; signals computed once
- `run_multi(pairs, initial_balance)` — shared-wallet portfolio; one `SimulationStepper` per pair, wallet + trade-id sequence rebound around every step

**Browser UI**: chart and report servers run in daemon threads. An `atexit` handler (`_register_keep_alive()`) blocks the process until Ctrl+C after all user code finishes. Registered at most once per process.

### `trader/` (v1.0.0)

Live trading. Two entry points, one config class, one event stream.

| Name | Role |
|---|---|
| `run_live(strategy=, broker=, config=)` / `run_live(pairs=[...])` | **Paper trading** (D8): live market data, positions filled by the sim engine. Never imports `_execution`, never calls an order method. `display_trades` must stay `"sim"`. Blocks; returns the final `SimulateReport`(s). Also `from AlgoTradeKit import run_live`. |
| `Trader(broker=, strategy=, config=)` / `Trader(pairs=[...], on_stop=, state_path=, kill_switch_file=)` | **Real orders.** `run()` blocks; Ctrl+C / SIGTERM / kill-switch file shut down gracefully. |
| `TraderConfig` | Same field names as `SimulateConfig` (D12) + trader groups (costs override, `execution`, feed, logging, display, safety). `to_simulate_config(broker, …)` derives the display config, filling costs from `get_trading_costs()`. |
| `TraderPair(broker, config, strategy)` | One multi-pair entry; the same `(broker, symbol)` may never appear twice (D9). |
| `EventStream` + `SignalEvent`/`OpenEvent`/`SlMoveEvent`/`RiskFreeEvent`/`TpLevelEvent`/`CloseEvent`/`ExitSignalEvent`/`DailyLossEvent`/`ReconcileEvent`/`ErrorEvent` | Typed events (D13). `TerminalEventPrinter` (wired by `attach_terminal_printer`) is the only v1.0.0 subscriber — one grep-able line per event, tagged `[SIM]` or `[LIVE]`. Notification backends land in v1.1 on the same stream. |

Files: `_config.py` (§11), `_events.py` (§12), `_run_live.py` (§13), `_scheduler.py` (§14), `_execution.py` (§15/§17), `_trader.py` (§16/§18/§20), `_state.py` (§19), `_display.py` (§21).

Rules that live trading depends on:

- **Venue-native SL/TP always** (D1) — SL/TP go on the real order; trailing / risk-free / multi-RR moves **modify the venue SL**. Never implement a soft SL that watches price and market-closes.
- **Closed-candle detection is arithmetic** (D2) — a candle opening at `T` on timeframe `tf` is final once `venue_now >= T + tf`, measured against the venue clock (`clock_offset_ms`), never "a newer row appeared". Some venues don't print a candle until its first trade.
- **Live position management reuses `simulate/_position_math.py`** — that is what makes live match the backtest.
- **Display never slows trading** — display work runs on a low-priority background queue; the trading loop only enqueues and coalesces backlog.
- Multi-RR ladders are venue-native (D5): reduce-only level orders on Binance futures, feed-detected partial closes on MT5.

### `visual/`

`Chart` wraps a TradingView lightweight-charts frontend served via FastAPI/uvicorn on a daemon thread. All drawing coordinates use **Unix seconds** (not ms).

Key functions:

- `chart.show(block=False/True)` — opens browser tab
- `chart.set_data(df, candle_range=...)` — accepts AlgoTradeKit DataFrames (timestamp in ms)
- `chart.add_position_box(...)` — TradingView-style position box (loss zone + profit zone)
- `chart.navigate_to_candle(timestamp_ms)` — scroll chart to candle
- `add_simulation_positions(chart, report, opacity, config)` — overlays all trades from a `SimulateReport`; pass `config` to enable dynamic SL/TP lines (v0.7.4)
- `chart.add_indicator_spec(spec)` — compute an indicator server-side from a `{"kind": ..., <params>}` dict; shared by the in-browser indicator toolbar (add/edit, all maths backend) and `SimulateConfig.chart_indicators` (v0.8.0)
- `add_strategy_drawings(chart, strategy_result)` — applies `StrategyResult.drawings` to chart
- `add_rsi/add_macd/add_ma/add_ichimoku` — bridge indicator objects to chart panes

Dynamic SL/TP line colours (v0.7.4): red = loss zone, amber = break-even, cyan = profit, green = next TP target.

Live additions (v1.0.0): `Chart(host=..., candle_count_limit=...)`; `chart.url` (a `0.0.0.0` bind displays as `127.0.0.1`); `add_live_position(...)` / `update_live_position(id, stop_loss=, next_tp=, label=)` for **open** trades; generic `update_drawing(id, **fields)` (re-broadcasts the full drawing so derived fields stay consistent); `set_candle_limit(N)` rolling window, mirrored in the browser. Every mutation refreshes the cached `init` payload, so a page refresh mid-session shows the current chart.

### `report/`

`show_report(report)` and `save_report_html(report, path)` render a self-contained single-page HTML report from `SimulateReport`. The "Open on Candle Chart" button in trade tooltips sends a WebSocket message to the linked chart server to navigate.

Live additions (v1.0.0): `ReportServer(host=...)` and `ReportServer.push_update(report_or_payload)` — re-broadcast fresh stats to the open page (replay cache refreshed, chart link carried forward). `build_combined_report_payload(pairs)` / `show_combined_report` / `save_combined_report_html` render the D9 portfolio view — merged trades, equity summed across accounts, per-pair breakdown. A report built from real broker fills is a plain `SimulateReport` and needs no special path.

## Key conventions

- **Times**: Python layer = UTC milliseconds. Chart frontend = Unix seconds. Never mix.
- `simulate/_engine.py` is pure — never mutates `StrategyResult` or source DataFrames; `SimulationStepper` also never mutates the candle mapping or the signals it is given.
- Stateful strategy variables go in `setup()`, not `__init__()`.
- `StrategyResult.drawings` times must be **Unix seconds** to match the chart frontend.
- Indicator columns in `prepare_indicators()` are prefixed `_` to distinguish from OHLCV.
- All indicators are implemented from scratch — no pandas-ta, no ta-lib.
- **Position maths lives in `simulate/_position_math.py`** — backtest, `run_multi` and the live trader share it. Never fork the maths into the trader.
- **`simulate` must not import `trader`** (dicts out, typed events mapped on the trader side); `report` must not import `simulate`; `broker` must not import `data`.
- New `trader` constants are re-exported from `trader/__init__.py`; a new event type also needs a printer branch and an `ALL_EVENT_TYPES` entry.
- Live SL/TP is **venue-native only** (D1). No soft stops, anywhere.

## Workflow for new features

Follow the order in `Publish.txt`:

1. `git checkout main && git pull origin main && git checkout -b feature/<name>`
2. Write code, commit each logical unit with a standard commit message
3. Write tests, commit; fix failures and commit fixes
4. `git push origin feature/<name>`
5. Open a PR on GitHub, merge to main
6. `git checkout main && git pull origin main`
7. Bump version in `pyproject.toml` AND `src/AlgoTradeKit/__init__.py`
8. Commit: `chore(release): bump version to X.Y.Z`
9. Update `CHANGELOG.md` and `README.md`, commit: `docs: update CHANGELOG and README for vX.Y.Z`
10. `git tag vX.Y.Z && git push origin main && git push origin vX.Y.Z`
11. Create GitHub Release — triggers PyPI publish

Current version: **1.0.0** (in `pyproject.toml` and `src/AlgoTradeKit/__init__.py`).

Reference documents: `MT5_WINE_SETUP.md` (headless MT5 on Linux + the Windows
native path), `PROJECT_STRUCTURE.md` (module boundaries and public APIs),
`Publish.txt` (release workflow).
