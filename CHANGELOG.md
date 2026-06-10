# Changelog

All notable changes to **AlgoTradeKit** are documented here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)
Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html)

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
