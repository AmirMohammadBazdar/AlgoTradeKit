# Changelog

All notable changes to **AlgoTradeKit** are documented here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/)
Versioning: [Semantic Versioning](https://semver.org/spec/v2.0.0.html)

---

## [Unreleased]

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
