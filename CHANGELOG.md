# Changelog

All notable changes to **AlgoTradeKit** are documented here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)
Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html)

---

## [0.9.0] — 2026-07-01

### Added

#### `broker` module — unified exchange & MetaTrader access (new)

- **`Broker(name, ...)`** — one entry point for every venue. `name` is
  case-insensitive: `"binance-spot"`, `"binance-futures"`, `"metatrader"`
  (aliases `mt5` / `forex`). Credentials are **optional** — without them only
  public market-data calls work; private (account / trading) calls raise a clear
  `AuthenticationError`. Returns a concrete `BaseBroker`, so the whole library
  treats every venue identically.
- **`BaseBroker`** unified interface: `fetch_candles` / `fetch_last_candles` /
  `get_ticker` / `server_time`, `stream_candles` / `stream_ticker`,
  `get_balance` / `get_account_info`, `create_order` (+ `create_market_order` /
  `create_limit_order`), `cancel_order` / `cancel_all`, `open_orders`,
  `open_positions`, `close_position`, `set_leverage`.
- **Unified types** (`Balance`, `AccountInfo`, `Ticker`, `Order`, `OrderResult`,
  `Position`) and string constants (`SIDE_BUY`, `ORDER_MARKET`, `MARKET_SPOT`,
  …) re-exported from `broker/__init__.py`.
- **Binance connector** (`broker/exchange/binance/`): spot **and** USD-M futures.
  - REST: klines, ticker, server time; signed account, order create/cancel,
    open orders, futures positions (`positionRisk`), balance, `leverage`.
    Signing is `HMAC-SHA256` with the Python standard library — no new deps.
  - WebSocket: real-time `kline` and `bookTicker` streams, plus the private
    user-data stream (auto listen-key keepalive).
  - Futures `create_order` can attach `stop_loss` / `take_profit` as reduce-only
    protective orders. `testnet=True` points at Binance testnet.
- **MetaTrader 5 connector** (`broker/metatrader/`) for **headless** Linux VPS:
  - `bridge_server.py` runs inside the Wine Python (where `MetaTrader5` is
    importable) under `xvfb` — no GUI. It exposes MT5 over a tiny
    newline-delimited JSON socket (standard library only).
  - `MetaTraderBroker` runs on the normal Linux side and reaches MT5 only
    through that bridge, mapping candles / positions / orders / account onto the
    same unified types.
  - **`MetaTrader5` is never a dependency of AlgoTradeKit** (it lives only in the
    Wine Python), so there is no dependency conflict.

#### `data` module — now backed by `broker`

- All Binance REST kline logic **moved** into `broker`; `data.sources.binance`
  is now a thin adapter over it (public API unchanged).
- **`Collector` accepts a name *or* a `Broker` instance** — pass
  `Collector("binance-futures", …)` (way 1, Collector builds the connector) or
  `Collector(my_broker, …)` / `Collector(..., broker=my_broker)` (way 2, bring
  your own — including an authenticated or MetaTrader **forex** connection).
- **`Collector.stream(on_candle)`** — real-time candle subscription for both
  exchange (Binance WebSocket) and MetaTrader feeds.

### Notes

- Live-order safety: `testnet` is supported; live endpoints are the default.
- Deferred to **v0.9.1** (see `v091.md`): the `trader` live-order module,
  real-time simulation, live-updating chart/report, and the ichimoku live demo.

---

## [0.8.0] — 2026-06-29

### Added

#### `visual` module — working indicator toolbar (add / edit, backend-computed)

- **Indicator button now works.** The chart toolbar's **INDICATORS** button was
  inert (it called an undefined `sendMsg`); the panel now ships the chosen spec
  to the backend over the WebSocket and the computed series returns as one or
  more `add_indicator` messages. The button (and its panel) were moved to the
  **left** of the toolbar.
- **Edit existing indicators.** Every indicator in the legend / oscillator pane
  gains a ⚙ gear icon. Clicking it re-opens the panel pre-filled with that
  indicator's current settings; saving recomputes it in the backend (the old
  series is removed and replaced). Works for price-pane lines, the Ichimoku
  group, and oscillator sub-panes (RSI / MACD / ATR).
- **`Chart.add_indicator_spec(params)`** — new public method that computes an
  indicator from the chart's stored OHLCV **server-side** and tags each
  resulting series with the resolved `spec` that produced it (so the front-end
  gear can round-trip it). Used by both the toolbar and `chart_indicators`.
- **Ichimoku Displacement** is now an editable parameter in the panel and in
  indicator specs (defaults to 26).
- Indicators added/edited live now survive a browser refresh (the server's
  init-replay cache is kept in sync).

#### `simulate` module — indicators on the simulation chart

- **`SimulateConfig.chart_indicators`** — new field: a list of indicator-spec
  dicts (same schema as the toolbar) drawn on the `show_chart=True` candle
  chart. All maths run in the backend before the chart opens. Pass the
  strategy's own indicator parameters to mirror what it traded on, add extra
  indicators, or both. Validated in `__post_init__` (must be a list of dicts,
  each with a `"kind"`).

### Fixed

- **Ichimoku from the toolbar** computed with the wrong argument order
  (`close, high, low` instead of `high, low, close`), producing incorrect
  lines. Now correct and verified against a direct `Ichimoku` call.
- **RSI from the toolbar** crashed on its default (`ma_type="none"` →
  `None.upper()`), and never showed its MA line because `show_ma` was not set.
  Both fixed.
- **VWAP / VWMA from the toolbar** raised `TypeError: got multiple values for
  argument …` due to mixed positional/keyword construction. Now built with
  explicit keyword arguments.

### Changed

- `ichimoku_strategy.py` now passes `chart_indicators` so the simulation chart
  automatically renders the Ichimoku cloud + RSI (matching the strategy's
  parameters, including the non-standard displacement).

---

## [0.7.4] — 2026-06-24

### Added

#### `simulate` module — dynamic SL/TP visualisation

- **`ClosedTrade.sl_history`** — new `tuple` field recording every SL-state
  change throughout a trade's lifetime as an ordered sequence of dicts
  `{"time": int_ms, "sl": float, "next_tp": float | None}`.  An entry is
  appended at position open (initial state) and again every time the SL
  advances: after each `multi_rr` TP level hit (SL walks to break-even /
  previous TP) or whenever the trailing SL moves to a new high-water mark.
  An empty history means the SL never moved; a single entry means the
  position opened but the SL never advanced before closing.

- **`ClosedTrade.final_next_tp`** — new `float | None` field that records
  the TP target that was active at the exact moment the trade closed.  For
  a `multi_rr` position that hit TP3 (moving next target to TP4) and was
  then stopped out at the TP3-advanced SL, this is TP4.  `None` when all
  levels were consumed (position closed at the final TP) or no TP was
  configured.

- **`ClosedTrade.peak_price`** — new `float` field storing the most
  favourable price reached during the trade (highest high for longs, lowest
  low for shorts).  Used by the visual module as the top of the position box
  for trailing-SL trades (the peak marks the last price that drove the
  trailing SL to its tightest level).

- **`SimulateReport.trade_markers`** now includes `sl_history`, `final_next_tp`,
  `peak_price`, and `rr_levels_hit` for every marker, enabling the
  chart-side rendering of dynamic SL/TP lines.

#### `visual` module — dynamic SL/TP lines on chart

- **`add_simulation_positions`** (in `indicator_renderer.py`) enhanced with:
  - **Dynamic SL line segments** — for `multi_rr` and `trailing` positions,
    coloured horizontal `TrendLine` drawings are added alongside each position
    box showing where the stop-loss was at each stage of the trade:
    - **Red** — SL is in the loss zone (below entry for long)
    - **Amber/yellow** — SL is at break-even (entry price)
    - **Cyan** — SL is in profit territory (above entry for long)
  - **Green TP lines** (`multi_rr` only) — for each SL segment, a matching
    green horizontal line shows the next TP target that was being aimed at.
  - **Posbox TP fix** — the profit-zone boundary of the position box now
    shows `final_next_tp` (the TP at closing time) rather than the initial
    TP at entry.  After hitting TP3 with next target TP4, the box correctly
    displays TP4 instead of staying stuck at TP1.
  - **Trailing posbox top** — for `sl_mode="trailing"`, the top of the
    position box is set to `peak_price` (the highest price the trailing SL
    ever chased), giving a natural view of the profit zone that was available.
  - **Partial-close grouping** — multiple `ClosedTrade` rows sharing a
    `trade_id` (from `tp_level_close_fractions`) are now collapsed into a
    single position box (one box per unique trade, not one per slice).
  - New `config` parameter on `add_simulation_positions` — pass the
    `SimulateConfig` used for the simulation to enable automatic mode
    detection (`tp_mode`/`sl_mode`) for the correct line style.

- **Indicator toolbar** — a new 📈 button in the chart toolbar opens an
  "Add Indicator" panel where the user can add technical indicators to the
  live candle chart without writing any code:
  - **Moving averages**: EMA, SMA, WMA, SMMA, DEMA, TEMA, HMA, VWMA, VWAP
  - **Oscillators**: RSI (with optional MA), MACD, ATR (pure Python — no
    third-party TA library)
  - **Trend**: Ichimoku Cloud
  - Each indicator type shows its own parameter form (period, source, MA
    type, fast/slow/signal periods, Tenkan/Kijun/Senkou B, etc.).
  - A colour picker lets the user override the default series colour.
  - The "Add to Chart" button sends a `compute_indicator` WebSocket message
    to the `Chart` server, which computes the indicator from the stored OHLCV
    `DataFrame` and broadcasts the result as one or more `add_indicator`
    messages; the chart renders them immediately.

### Fixed

- **Trailing SL false gap-open trigger** — the trailing SL was previously
  advanced in step A (before close checks), which could cause a "gap open
  past SL" close on the very candle that caused the SL to jump, when the
  candle's open price was between the old and new SL levels.  The trailing
  SL is now advanced inside `_check_close` *after* the gap-open test, so
  the gap check always uses the SL from the end of the previous candle.
  This fix applies to both the single-pair `Simulate` engine and the
  multi-pair `run_multi` engine (`_runner.py`).

- **Chart and report tabs disconnecting immediately** — the
  `ChartServer` and `ReportServer` both run in daemon threads, which Python
  kills the instant the script's main thread exits.  Previously,
  `Simulate.run()` started both servers non-blocking and returned, so all
  user code after the call (printing stats, saving CSV history, etc.) ran
  normally — but as soon as `main()` returned the process died, closing both
  browser tabs before the user could interact with them.  A one-time `atexit`
  handler is now registered whenever a browser UI is opened; it blocks with
  a `while True: sleep(1)` loop (identical to `chart.show(block=True)`) after
  all user code has finished, keeping the process alive until the user presses
  **Ctrl+C**.  The handler registers only once per process invocation, so
  multiple `Simulate.run()` calls (batch/multi-symbol) produce a single
  blocking handler at exit.

### Tests

- `tests/test_simulate.py` — new `TestSlHistory` class (13 tests) covering:
  - `sl_history` length and content after 0, 1, and 2 `multi_rr` TP hits
  - timestamp presence and ordering in `sl_history`
  - `final_next_tp` value after partial and final TP consumption
  - `peak_price` captured correctly for trailing-SL trades
  - backward-compatibility: plain `signal` SL mode produces exactly one
    `sl_history` entry (no SL movement)
  - `add_simulation_positions` smoke test: one posbox per unique `trade_id`
  - dynamic SL `TrendLine` drawings added when `sl_history` length > 1
  - posbox `take_profit` uses `final_next_tp` not initial TP

---

## [0.7.3] — 2026-06-21

### Added

#### `strategy` module
- **`Signal.risk_multiplier`** — new optional field (default `1.0`) that scales
  the size/risk a single signal receives relative to the run's global
  `SimulateConfig` sizing (`risk_percent` / `fixed_amount` / `fixed_lot`).
  Lets a strategy vary risk per signal instead of using one fixed sizing
  rule for every trade in a backtest — e.g. half size on a tentative
  "limit" entry vs. full size on a confirmed one, or splitting one trade
  idea into several sub-positions whose sizes sum to one risk unit
  (three signals at `1/3` each, targeting 1R/2R/3R, to emulate scaling out
  of a position). Default `1.0` is fully backward compatible.

#### `simulate` module
- **`SimulateConfig.tp_level_close_fractions`** — optional list, parallel to
  `tp_levels`, that turns on **true partial closes** for `tp_mode="multi_rr"`.
  Each entry is the fraction of the position's *original* size to realise
  when the corresponding level is reached, instead of only moving the SL.
  `None` (default) preserves the exact pre-0.7.3 behaviour (SL-only
  advancement, single full close at the final level). When set:
  - fractions summing to `1.0` give a full progressive scale-out
    (e.g. `[1/3, 1/3, 1/3]`) — profit is banked level by level instead of
    all-or-nothing at the end.
  - all-zero fractions give a "let it run" position: the SL just walks
    through every level and the full size stays open, trailing at the
    last level's price, until eventually stopped out.
  - any mix is valid — partially bank some levels, let others purely
    advance the SL.

  Each partial close produces its own `ClosedTrade` sharing the position's
  `trade_id`, so `SimulateReport` statistics (win rate, profit factor,
  equity curve) count every realised slice on its own line, the same way
  a real scaled-out position would appear on a broker statement.
  Commission (pre-charged once, in full, at entry) is split proportionally
  across every slice so the total charged across one position's full
  lifetime never changes.
- **`CLOSE_REASON_TP_PARTIAL`** is now exported from `AlgoTradeKit.simulate`.
  It existed on `ClosedTrade` since an earlier release (with `is_tp`
  already wired to recognise it) but no code path had ever produced it
  until `tp_level_close_fractions` above.
- `run_multi()` gained the identical partial-close handling as the
  single-pair `Simulate` engine, so shared-wallet multi-symbol portfolios
  behave the same way; `run_batch()` needed no changes (it only wraps
  `Simulate(cfg).run()`).

### Tests
- `tests/test_strategy.py` — `TestSignal` extended with 5 tests for
  `risk_multiplier` (default, custom value, `<= 0` validation, positional-
  construction backward compatibility).
- `tests/test_simulate.py` — new `TestEngineRiskMultiplier` (4 tests) and
  `TestEngineMultiRRPartialClose` (8 tests, including a hand-derived PnL
  ground truth and an end-to-end wallet-conservation check) and
  `TestSimulateConfigPartialTPValidation` (8 tests). All 171 pre-existing
  tests in these two files continue to pass unmodified.

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
