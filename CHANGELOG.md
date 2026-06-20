# Changelog

All notable changes to **AlgoTradeKit** are documented here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)
Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html)

---

## [0.7.2] — 2026-06-20

### Added

#### `data` module
- **`Normalizer`** — new class to ingest non-standard OHLCV CSV files and
  convert them to the AlgoTradeKit standard format.  Features:
  - Auto-detects whether timestamps are in **Unix seconds** (`< 10^10`) or
    **milliseconds** (`≥ 10^10`) and converts to ms automatically.
  - Fills missing optional columns (`close_time`, `quote_volume`, `trades`,
    `taker_buy_base`, `taker_buy_quote`) with `None`; `volume` defaults to `0`.
  - Date-range filtering via `start` / `end` attributes before returning data
    (integers are also auto-detected as seconds or ms).
  - Three usage modes: `normalize()` → DataFrame, `save()` → CSV path,
    `normalize_and_save()` → `(DataFrame, path)`.
  - `Normalizer` is now exported from `AlgoTradeKit.data`.

#### `indicator` module
- **`ATR`** — Average True Range using Wilder's RMA smoothing (`alpha = 1 / period`,
  `min_periods = period`).  Matches TradingView `ta.atr()` output.
  Exposes `atr` and `tr` properties and result-dict keys.
  Default period: **14**.  Exported from `AlgoTradeKit.indicator`.
- **`Ichimoku.span_a_raw`** and **`Ichimoku.span_b_raw`** — two new properties
  (and `result` dict keys) exposing the **unshifted** Senkou Span A and B values
  at each bar (i.e. `(tenkan + kijun) / 2` and the Span B donchian mid *before*
  the displacement shift is applied).  These are required for strategy entry/exit
  logic where the current cloud position matters, not the displayed future cloud.
  Fully backward-compatible — no existing keys or properties were changed.

#### `visual` module
- **`Chart.set_data(candle_range=...)`** — new optional `candle_range` dict
  parameter filters which candles are rendered.  Five modes:
  - `{"start": ..., "end": ...}` — inclusive datetime range (either key optional)
  - `{"start": ...}` — from date to the last available candle
  - `{"end": ...}` — from the first candle to date
  - `{"last_n": N}` — last N candles
  - `{"first_n": N}` — first N candles

  `start` / `end` accept `"YYYY/MM/DD"`, `"YYYY-MM-DD"` strings, `datetime`
  objects, Unix-second `int`, or Unix-millisecond `int`.
- **`Chart.from_csv(candle_range=...)`** — `candle_range` forwarded from the
  factory class method.

### Tests
- `tests/test_data_normalizer.py` — 25+ unit tests for `Normalizer` covering
  timestamp detection, date filtering, schema completeness, save/load round-trip,
  and error paths.
- `tests/test_indicator.py` — extended with `TestATR` (15 tests) and
  `TestIchimokuRawSpans` (10 tests) for the new indicator features.

---

## [0.7.1] — 2026-06-18

### Fixed

#### `visual` module
- **Canvas clipping** — drawings (h-lines, trend lines, boxes, signals,
  Ichimoku cloud, position boxes) no longer bleed over the right price axis,
  the bottom time axis, or adjacent sub-chart pane areas when the chart is
  panned or zoomed.  A `getDrawingClipRect()` helper measures the actual axis
  widths from the LWC DOM at render time and applies a canvas `clip()` in
  both `redrawAll()` and `redrawGhost()`.
- **`navigate_to_candle` timing** — the chart now replies the last `init` and
  last `navigate_to_candle` message to every newly-connecting browser tab.
  Clicking "Open on Candle Chart" in the report now reliably scrolls the
  chart to the position entry candle even when the chart tab was opened
  after the navigate command was sent.
- **`chart_port` not set** — `Simulate._build_simulation_chart()` returned
  `(chart, chart)` instead of `(chart, chart._server)` because `Chart.show()`
  returns `self`.  The returned server object is now `chart._server`, so
  `report_data.chart_port` is set correctly and the "Open on Candle Chart"
  button in the report opens/focuses the right URL.

#### `report` module
- **Equity balance chart panning** — replaced `chartjs-plugin-zoom` (which
  failed to register mouse-drag pan events reliably with Chart.js 4.x) with a
  direct `mousedown` / `mousemove` / `mouseup` listener.  Holding left-click
  and dragging now pans the balance chart left/right.  Wheel scroll still
  zooms.  Double-click still resets to full range.

#### `simulate` module
- (no functional changes; only the `_build_simulation_chart` fix above)

### Changed

#### `visual` module
- **Position box: resizable** — `position_box` drawings that are not locked
  now render five resize handles:
  - **Left / Right edge** (blue) — drag to change `open_time` / `close_time`
  - **Entry line** (white) — drag to shift `entry_price` (TP and SL move with it)
  - **TP line** (green/red) — drag to change `take_profit` / `visual_tp`
  - **SL line** (red/green) — drag to change `stop_loss`
  Moving the box interior moves the entire position.  The R:R label updates
  live as you resize.
- **Position box: toolbar shape** — a new `posbox` toolbar button (⬜ icon)
  lets users draw position boxes interactively:
  - Click once to set `entry_price` and `open_time`
  - Click a second time to set the TP price and `close_time`
  - Direction (long/short) is inferred from whether the TP is above or below entry
  - SL is placed at 1R below/above entry automatically and can be dragged afterwards
  - User-created boxes are **unlocked** by default; server-created (simulation)
    boxes remain locked as before
- **Position box context menu** — right-click works on position boxes just
  like on other drawings (lock/unlock, rename label, change color, delete).

### Fixed (documentation)

- **README drawings examples** — corrected three code snippets that called
  the removed `chart.add_drawing()` method:
  - `chart.add_drawing(HorizontalLine(...))` → `chart.add_hline(...)`
  - `chart.add_drawing(Box(...))` → `chart.add_box(...)`
  - `chart.add_drawing(Signal(...))` → `chart.add_signal(...)`

---

## [0.7.0] — 2026-06-17

### Added

#### `report` module (new)
- **`ReportServer`** — FastAPI + WebSocket server that serves the report page
  in a background daemon thread, identical in architecture to `ChartServer`.
- **`show_report(report, block, port, title, on_open_chart)`** — open the
  interactive simulation report in the default browser.
- **`save_report_html(report, path)`** — save a fully self-contained HTML
  report file that can be shared and opened without a running server.
- **`build_report_payload(report)`** — serialise a `SimulateReport` into a
  JSON-safe dict for custom downstream use.
- **`report/static/report.html`** — comprehensive single-page report UI:
  - **Equity curve** (Chart.js 4, `chartjs-plugin-zoom`) — wallet balance and
    equity over time; scroll-to-zoom, drag-to-pan, double-click-to-reset.
  - **Max drawdown overlay** — shaded rectangle with percentage label.
  - **Significant drawdown regions** — all drawdowns above the configured
    threshold rendered as lighter shading.
  - **Per-trade dots** — green/red scatter markers for every closed trade on
    the equity curve; hover shows full trade detail tooltip.
  - **"Open on Candle Chart" button** in the trade tooltip — sends a
    WebSocket message to the Python process, which navigates the linked chart
    to the position entry candle.
  - **Series visibility toggles** — show/hide Balance, Wallet, Trades,
    Max DD, Drawdown regions independently.
  - **Performance KPI grid** — initial/final balance, total PnL, win rate,
    avg win/loss, largest win/loss, avg R-multiple.
  - **Risk metrics grid** — profit factor, expectancy, Sharpe, Sortino,
    Calmar, recovery factor, max consecutive wins/losses, max drawdown.
  - **Trade statistics grid** — total, long, short, wins, losses, break-even,
    SL count, TP count, risk-free count, force-close count, end-of-data count.
  - **Drawdown table** — all significant drawdowns sorted by severity; the
    deepest is highlighted.
  - **Weekday analysis table** — trades, win%, total PnL, avg PnL per day.
  - **Session analysis table** — London, New York, Tokyo, Sydney, Off-Hours.
  - **Monthly analysis table** — full calendar breakdown (all months present
    in the simulation data).
  - **Cost & excursion summary** — total commission, total spread, avg MAE,
    avg MFE.
  - **PDF/print export** — browser print dialog triggered by a header button;
    dedicated `@media print` CSS for clean print layout.
- **Report mode constants** — `REPORT_MODE_NONE`, `REPORT_MODE_WEBPAGE`,
  `REPORT_MODE_SAVE`, `REPORT_MODE_BOTH` exported from both
  `AlgoTradeKit.report` and `AlgoTradeKit.simulate`.

#### `visual` module
- **`PositionBox`** (`visual.models`) — TradingView-style position box
  drawing with a loss zone (SL → entry) and profit zone (entry → TP) rendered
  as two stacked semi-transparent rectangles, a dashed entry line,
  entry/exit markers, and a centred R:R label.
  When `take_profit` is `None`, a 2× SL-distance placeholder zone is drawn.
- **`Chart.add_position_box(...)`** — add a `PositionBox` to the chart,
  broadcasting it to any open browser tabs.
- **`Chart.navigate_to_candle(timestamp_ms)`** — scroll and zoom the chart
  to centre the candle at the given UTC millisecond timestamp.
- **`add_simulation_positions(chart, report, opacity, max_trades)`**
  (in `visual.indicator_renderer`) — overlay all trades from a
  `SimulateReport` as `PositionBox` drawings in one call.
- **`add_strategy_drawings(chart, strategy_result)`**
  (in `visual.indicator_renderer`) — apply all drawings from
  `StrategyResult.drawings` to the chart (support/resistance lines,
  signal zones, etc.).
- Both new helpers are exported from `AlgoTradeKit.visual`.
- `index.html` frontend updated to render `position_box` drawing type and
  handle the new `navigate_to_candle` WebSocket message.

#### `simulate` module
- **`SimulateConfig.show_chart`** (`bool`, default `False`) — when `True`,
  a candle chart is opened after the simulation with position boxes and any
  strategy drawings automatically applied.
- **`SimulateConfig.report_mode`** (`str`, default `"none"`) — controls
  post-simulation reporting:
  `"none"` | `"webpage"` | `"save"` | `"both"`.
- **`SimulateConfig.report_save_path`** (`str`, default `"report.html"`) —
  destination path for the saved HTML file when `report_mode` is `"save"` or
  `"both"`.
- `Simulate._render_post_simulation()` — internal method orchestrating
  chart and report rendering after `run()` completes.

#### `strategy` module
- **`StrategyResult.drawings`** (`list[dict]`, default `[]`) — list of
  visual drawing dicts the strategy wants rendered on the candle chart.
  Each dict follows the same format as the chart module's drawing objects
  (type, coordinates, color); times in **Unix seconds**.  Strategies
  populate this in `prepare_indicators()` or `generate_signals()`.

### Changed
- `visual.__init__` — exports `PositionBox`, `add_simulation_positions`,
  `add_strategy_drawings`.
- `simulate.__init__` — exports `REPORT_MODE_*` constants.
- `strategy._types.StrategyResult.__repr__` — now includes `drawings=N`.

### Fixed
- `test_report.py` — `test_run_no_server_started` patched at the correct
  (local-import) call site.

---

## [0.6.0] — 2026-06-15

### Added

- **`simulate` module** — full backtesting engine for Exchange and MetaTrader instruments
  - `SimulateConfig` dataclass — single unified configuration object for every simulation parameter:
    - **Wallet**: `initial_balance`, `symbol`, `exchange_type` (`"exchange"` / `"metatrader"`), `leverage`
    - **Trading costs**: `spread` (bid-ask spread in price units), `commission_type` (`"percentage"` / `"per_lot"` / `"fixed"`), `commission`
    - **Position sizing**: `position_sizing` (`"risk_percent"` / `"fixed_amount"` / `"fixed_lot"`), `risk_per_trade`, `fixed_amount`, `fixed_lot`, `compound` (size off current balance)
    - **Position limits**: `max_long_positions`, `max_short_positions`, `max_positions`
    - **Take-profit**: `tp_mode` (`"signal"` / `"fixed_rr"` / `"multi_rr"` / `"none"`), `tp_rr`, `tp_levels`
    - **Stop-loss**: `sl_mode` (`"signal"` / `"trailing"`), `trailing_sl_percent`
    - **Risk-free**: `risk_free_enabled`, `risk_free_at_rr` — moves SL to break-even when profit ≥ N×R
    - **Force close**: `force_close_on_exit_signal` — close on `ExitSignal` from strategy
    - **Report options**: `drawdown_threshold`, `primary_timeframe`, `config_id`
    - Supports dict-unpacking construction (`SimulateConfig(**cfg_dict)`) for batch sweeps
  - `Simulate` engine class — candle-by-candle simulation loop with full position lifecycle:
    - **Spread**: applied at fill (long = entry + spread; short = entry − spread)
    - **Leverage**: exchange margin model (`margin = size × entry / leverage`); MT5 simplified
    - **SL/TP**: gap-open detection, intra-candle priority via candle direction tiebreaker
    - **Multi-RR TP** (`tp_mode="multi_rr"`): SL trails to previous level after each TP hit; final level closes position
    - **Trailing SL** (`sl_mode="trailing"`): SL follows peak price at configurable % distance
    - **Risk-free / break-even**: SL moves to entry when position reaches `risk_free_at_rr × R` profit
    - **Force close**: positions closed at `ExitSignal.exit_price` (or candle close if None)
    - **EOD close**: all open positions closed at final candle close (`close_reason="end_of_data"`)
    - MT5 lot sizing via `calculate_mt5_lot()` for `"risk_percent"` and `"fixed_amount"` modes
    - Position limits enforced per direction and globally per candle
  - `ClosedTrade` frozen dataclass — immutable record with: `entry_price`, `exit_price`, `stop_loss`, `take_profit`, `size`, `margin_amount`, `risk_amount`, `gross_pnl`, `commission`, `net_pnl`, `pnl_r`, `close_reason`, `rr_levels_hit`, `max_favourable_excursion`, `max_adverse_excursion`, `leverage`, `spread_paid`, `signal_candle_index`
  - `SimulateReport` dataclass — fully pre-computed report (report module performs no maths):
    - **P&L summary**: `initial_balance`, `final_balance`, `total_pnl`, `total_pnl_percent`
    - **Trade counts**: total, long/short split, win/loss/break-even split, by close reason (sl/tp/rf/fc/eod)
    - **Win/loss metrics**: `win_rate`, `loss_rate`, `avg_win`, `avg_loss`, `largest_win`, `largest_loss`, `avg_pnl_r`
    - **Streaks**: `max_consecutive_wins`, `max_consecutive_losses`
    - **Risk metrics**: `profit_factor`, `expectancy`, `sharpe_ratio`, `sortino_ratio`, `calmar_ratio`, `recovery_factor`
    - **Drawdown**: `max_drawdown` (`DrawdownPeriod`), `significant_drawdowns` above configurable threshold
    - **Time analysis**: `weekday_stats` (Mon–Sun), `monthly_stats` (YYYY-MM), `session_stats` (London, New York, Tokyo, Sydney, off-hours)
    - **Balance history**: one `{timestamp, wallet, equity}` dict per primary-TF candle for the balance chart
    - **Trade markers**: per-trade dicts with all data needed for the visual report (chart dots, hover tooltips, click-to-chart links via `signal_candle_index`)
    - **Cost summary**: `total_commission`, `total_spread_cost`, `avg_mae`, `avg_mfe`
  - `run_batch(strategy, data, configs)` — run one strategy against N configs in parallel (`ThreadPoolExecutor`); strategy signals computed once, results returned in input order
  - `run_multi(pairs, initial_balance)` — shared-wallet portfolio simulation; multiple (strategy, data, config) pairs compete for the same capital, combined `SimulateReport` returned
  - `_lot` sub-module — MT5 lot-size calculation for 50+ instruments:
    - Forex: USD-quote, JPY, CHF, CAD, AUD, NZD, GBP, HKD, exotic, Eastern European, Scandinavian pairs
    - Metals: XAUUSD, XAGUSD, XPTUSD, XPDUSD
    - Crypto: BTC, ETH, XRP, LTC, BCH, ADA, DOT, SOL, BNB, MATIC, LINK
    - Indices: US30, US100, US500, GER40, UK100, FRA40, ESP35, JP225, HK50, AUS200
    - Oil & gas: WTI, Brent, Natural Gas
    - `round_lot(lot, step, min_lot, max_lot)` — broker lot-step rounding
    - `get_mt5_pip_info(symbol, price)` — returns `(pip_size, pip_value_per_lot)` for fixed-lot sizing
  - `_session` sub-module — Forex trading session detection (Sydney, Tokyo, London, New York) from UTC timestamps; weekday and YYYY-MM month key helpers
- 72 unit tests covering: `SimulateConfig` validation, MT5 lot maths, session detection, position helpers, engine SL/TP/multi-RR/trailing/risk-free/force-close/EOD/leverage/spread/commission/position-limits/MT5 sizing, report statistics (win rate, profit factor, streaks, drawdown, session/weekday/monthly breakdown, markers), `run_batch` ordering and empty input, `run_multi` combined portfolio

---

## [0.5.0] — 2026-06-12

### Added

- **`strategy` module** — framework for building, running, and combining trading strategies
  - `BaseStrategy` abstract class with a clear four-method lifecycle:
    - `prepare_indicators(data)` — compute all indicator columns once before the loop
    - `generate_signals(candle_index, data)` — per-candle entry detection (main loop)
    - `setup(data)` — optional one-time state initialisation (called before the loop)
    - `detect_exit_signals(candle_index, data)` — optional per-candle exit detection
  - `run(data, mode)` framework entry point — not overridable; handles the full lifecycle
  - `StrategyMode.BACKTEST` — processes all candles, returns all signals
  - `StrategyMode.LIVE` — processes all candles for correct state building, returns only last-candle signals (compatible with the upcoming `trade` module and WebSocket feeds)
  - `Signal` dataclass — entry signal with `direction`, `entry_price`, `stop_loss`, `take_profit`, `timestamp`, `candle_index`, `timeframe`, `metadata`; includes `sl_distance` and `risk_reward` properties
  - `ExitSignal` dataclass — exit condition with `reason`, `exit_price`, `timestamp`, `candle_index`, `metadata`
  - `StrategyResult` dataclass — full output with `signals`, `exit_signals`, `data` (enriched), `mode`; includes `long_signals`, `short_signals`, `has_signal`, `signal_count` helpers
  - Multi-timeframe support via `dict[str, pd.DataFrame]` input; plain DataFrames are auto-wrapped
  - Three built-in helper methods on `BaseStrategy`: `get_candle()`, `latest_candle_at()`, `history()`
  - `warmup_period` class attribute to skip early candles where indicators are NaN
  - Input validation: checks required OHLCV columns, primary timeframe presence, empty DataFrames
- **`strategy.builtin.MACDCrossoverStrategy`** — first built-in strategy
  - Entry: MACD line crosses above/below signal line (bullish/bearish crossover)
  - Stop-loss: swing low/high over a configurable `sl_lookback` window (default 10 candles)
  - Take-profit: `None` (managed dynamically by simulate/trade)
  - Exit: `ExitSignal(reason="macd_reversal")` when histogram changes sign
  - Configurable: `fast_length`, `slow_length`, `signal_length`, `sl_lookback`, `timeframe`
  - MACD computed via the existing `indicator.MACD` class (no duplicate code)
- 99 unit tests covering all types, BaseStrategy lifecycle, modes, helpers, validation, MACDCrossoverStrategy indicators, signals, SL, exits, live mode, and edge cases

---

## [0.4.1] — 2026-06-10

### Fixed
- **visual**: Ichimoku cloud fill now renders correctly between Span A and
  Span B — canvas polygon with bullish/bearish colour segments and
  crossover interpolation
- **visual**: Right-axis cursor crosshair label always shows candle close —
  LWC's raw horzLine label disabled and replaced with a PriceLine at
  exact candle close on hover
- **visual**: Single-line indicators (EMA, SMA, RSI) render as flat legend
  rows with no group wrapper or collapse arrow
- **visual**: Group wrappers built lazily — created on second line arrival,
  downgraded back to flat row when only one line remains
- **visual**: `toggleGroupVisibility` and `removeIndicatorGroup` no longer
  error on single-line groups with no wrapper DOM element
- **visual**: `subPaneGroups` resets correctly on chart re-init

### Added
- **visual**: Cloud (Bull) and Cloud (Bear) have independent hide and
  remove buttons inside the Ichimoku group; removing the group clears
  the canvas fill immediately
- **visual**: Multi-line sub-chart indicators (MACD) show a collapsible
  group chip with unified hide-all and remove-all controls
- **visual**: Main-chart indicator legend moved from top-right to top-left
- **visual**: Two new Settings toggles under "Price Axis Labels" —
  "Highlight last candle close on right axis" (default ON) and
  "Show indicator last prices on right axis" (default OFF)

### Performance
- **visual**: Hidden indicators skip all rendering — `visible:false` for
  LWC series, segment skips in the cloud canvas loop
- **visual**: 50 ms redraw interval skips `redrawAll()` when no drawings
  and no visible cloud spans exist

---

## [0.4.0] — 2026-06-07

### Added

- `indicator` module with TradingView-compatible implementations (no TA-lib / pandas-ta)
- **MA family** — `SMA`, `EMA`, `WMA`, `VWMA`, `SMMA` (RMA/Wilder), `DEMA`, `TEMA`, `HullMA`, `VWAP`
  - All formulas match TradingView Pine Script v5 exactly
  - All default periods, colours, and settings mirror TradingView defaults
  - `VWAP` supports `anchor="session"` and `anchor="none"` plus ±1/2/3σ bands
- **`RSI`** — Wilder's RMA smoothing (alpha=1/length), exact TV match
  - Configurable `overbought` / `oversold` levels (default 70/30)
  - Optional MA overlay (`show_ma=True`) with `EMA`, `SMA`, `SMMA`, `WMA` types
  - `is_overbought()`, `is_oversold()`, `crossover_ob()`, `crossunder_ob()`, `crossover_os()`, `crossunder_os()` signal helpers
  - `visual_config()` returns TradingView-style render spec
- **`MACD`** — configurable fast/slow/signal lengths and MA types for both oscillator and signal lines
  - TradingView 4-colour histogram (pos/grow, pos/fade, neg/fall, neg/fade)
  - `crossover()`, `crossunder()`, `zero_crossover()`, `zero_crossunder()` signal helpers
  - `histogram_colors()` per-bar colour series
  - `visual_config()` returns render spec
- **`Ichimoku`** — full Kinko Hyo with all five lines
  - Tenkan-sen (9), Kijun-sen (26), Senkou Span A, Senkou Span B (52), Chikou Span
  - Senkou Span A/B projected **26 bars into the future** past the last candle
  - `cloud_df()` returns full cloud DataFrame with bullish/bearish colour labels
  - Signal helpers: `tk_cross_bullish/bearish()`, `price_above/below/in_cloud()`, `bullish/bearish_cloud()`
  - `visual_config()` with `extend_future=True` for cloud rendering
- `visual.indicator_renderer` — bridges indicators to the `Chart` visual module
  - `add_ma(chart, ma)` — overlays any MA on the price pane
  - `add_rsi(chart, rsi)` — adds RSI oscillator pane with OB/OS zones
  - `add_macd(chart, macd)` — adds MACD pane with coloured histogram
  - `add_ichimoku(chart, ichi)` — overlays all five Ichimoku lines + future cloud fill
- 93 unit tests across `_base` helpers, all MA types, RSI, MACD, Ichimoku, and integration

### Fixed

- `Ichimoku`: independent `high` / `low` length validation (each checked separately)
- `MACD.histogram_colors()`: NaN bars reliably return `None` regardless of pandas dtype inference

---

## [0.3.0] — 2026-06-05

### Added

- `visual` module: `Chart` class for interactive candlestick visualization
- Served via FastAPI + WebSocket with TradingView lightweight-charts frontend
- `Chart.from_csv()` — load directly from AlgoTradeKit Collector CSV files
- `Chart.set_data()` — accepts AlgoTradeKit `timestamp` (ms) column natively
- `Chart.stream_from_atk()` — bridge for future live exchange feeds
- `Chart.add_indicator_from_atk()` — bridge for future indicator/strategy output
- Drawing tools: HorizontalLine, TrendLine, Box, Signal, TextLabel, FibRetracement
- Candlestick + Heikin-Ashi chart types, dark/light themes
- Live streaming: `stream()` and `stream_indicator()` for real-time updates
- Layout save/load to JSON

---

## [0.2.0] — 2026-05-31

### Added

- `Converter` class in the `data` module for OHLCV timeframe resampling
- Supports CSV file or pandas DataFrame as input
- Automatic source timeframe detection from timestamp gaps
- Validation: raises clear error if conversion is not mathematically possible
- Correct OHLCV aggregation (open=first, high=max, low=min, close=last, volume=sum)
- Incomplete edge candles are dropped from output
- Same output options as `Collector` (destination, outputname)

---

## [0.1.0] — 2026-05-26

### Added

- `data` module: `Collector` class for OHLCV candle collection
- Binance Spot and USD-M Futures support via public REST API
- Smart CSV caching: detects and fills head, tail, and middle gaps
- Handles all Binance timeframes from `1m` to `1M`
- Auto-retry with exponential backoff on network errors
- Respects Binance rate limits via `Retry-After` header
- 28 unit tests covering utils, gap detection, sources, and Collector
