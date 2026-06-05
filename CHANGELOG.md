# Changelog

All notable changes to **AlgoTradeKit** are documented here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)
Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html)

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
