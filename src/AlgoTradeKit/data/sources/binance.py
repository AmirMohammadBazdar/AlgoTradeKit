from __future__ import annotations

from typing import Any
import requests
import time

from .base import BaseSource
from .._utils import TIMEFRAME_MS, ms_to_dt


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SPOT_BASE    = "https://api.binance.com"
_FUTURES_BASE = "https://fapi.binance.com"

#: Binance API interval strings (keyed by our normalised timeframe).
_INTERVAL_MAP: dict[str, str] = {
    "1m": "1m",  "3m": "3m",  "5m": "5m",  "15m": "15m", "30m": "30m",
    "1h": "1h",  "2h": "2h",  "4h": "4h",  "6h": "6h",
    "8h": "8h",  "12h": "12h",
    "1d": "1d",  "3d": "3d",
    "1w": "1w",
    "1M": "1M",
}

_MAX_PER_REQUEST = 1000   # Binance hard limit
_REQUEST_WEIGHT  = 2      # weight consumed per klines call (limit ≤ 1000)
_RATE_LIMIT_PAUSE = 0.12  # seconds between requests ≈ safe under 1200/min


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _next_open_ms(last_open_ms: int, timeframe: str) -> int:
    """
    Return the expected open-time of the candle AFTER ``last_open_ms``.

    For fixed-length timeframes this is ``last_open_ms + tf_ms``.
    For monthly (``"1M"``) we advance one calendar month.
    """
    if timeframe == "1M":
        dt = ms_to_dt(last_open_ms)
        year, month = dt.year, dt.month
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
        from datetime import datetime, timezone
        return int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp() * 1000)
    return last_open_ms + TIMEFRAME_MS[timeframe]


# ---------------------------------------------------------------------------
# Source class
# ---------------------------------------------------------------------------

class BinanceSource(BaseSource):
    """
    Fetch OHLCV klines from Binance via public REST endpoints.

    Parameters
    ----------
    market:
        ``"spot"`` → ``api.binance.com``
        ``"futures"`` → ``fapi.binance.com`` (USD-M Futures)
    """

    def __init__(self, market: str = "spot") -> None:
        market = market.lower().strip()
        if market not in ("spot", "futures"):
            raise ValueError(f"market must be 'spot' or 'futures', got '{market}'")

        self.market = market
        self._base = _FUTURES_BASE if market == "futures" else _SPOT_BASE

        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    @property
    def _klines_url(self) -> str:
        if self.market == "futures":
            return f"{self._base}/fapi/v1/klines"
        return f"{self._base}/api/v3/klines"

    @property
    def _time_url(self) -> str:
        if self.market == "futures":
            return f"{self._base}/fapi/v1/time"
        return f"{self._base}/api/v3/time"

    # ------------------------------------------------------------------
    # BaseSource interface
    # ------------------------------------------------------------------

    def get_server_time(self) -> int:
        resp = self._get(_TIME_URL := self._time_url, {})
        return resp["serverTime"]

    def validate_timeframe(self, timeframe: str) -> bool:
        return timeframe in _INTERVAL_MAP

    def fetch_candles(
        self,
        symbol: str,
        timeframe: str,
        start_ms: int,
        end_ms: int,
    ) -> list[dict[str, Any]]:
        """
        Paginate through Binance klines API and return all candles in
        [start_ms, end_ms] as a list of dicts.
        """
        if timeframe not in _INTERVAL_MAP:
            raise ValueError(
                f"Binance does not support timeframe '{timeframe}'. "
                f"Valid: {sorted(_INTERVAL_MAP)}"
            )

        interval   = _INTERVAL_MAP[timeframe]
        symbol     = symbol.upper()
        all_candles: list[dict[str, Any]] = []
        cursor     = start_ms

        while cursor <= end_ms:
            params = {
                "symbol":    symbol,
                "interval":  interval,
                "startTime": cursor,
                "endTime":   end_ms,
                "limit":     _MAX_PER_REQUEST,
            }

            raw = self._get(self._klines_url, params)

            if not raw:
                break

            for k in raw:
                all_candles.append(self._parse_kline(k))

            # If fewer than the max were returned we've reached the end
            if len(raw) < _MAX_PER_REQUEST:
                break

            # Advance cursor past the last returned candle
            cursor = _next_open_ms(raw[-1][0], timeframe)
            time.sleep(_RATE_LIMIT_PAUSE)

        return all_candles

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(self, url: str, params: dict) -> Any:
        """
        GET *url* with *params*, auto-retrying on transient errors.

        Respects 429 / 418 rate-limit responses by honouring the
        ``Retry-After`` header.
        """
        max_attempts = 5
        for attempt in range(max_attempts):
            try:
                resp = self._session.get(url, params=params, timeout=30)
            except requests.ConnectionError as exc:
                if attempt == max_attempts - 1:
                    raise ConnectionError(
                        f"Cannot reach Binance ({self.market}). "
                        "Check your internet connection."
                    ) from exc
                time.sleep(2 ** attempt)
                continue
            except requests.Timeout as exc:
                if attempt == max_attempts - 1:
                    raise TimeoutError("Binance request timed out after 30 s.") from exc
                time.sleep(2 ** attempt)
                continue

            # Rate-limited
            if resp.status_code in (429, 418):
                retry_after = int(resp.headers.get("Retry-After", 60))
                print(f"\n  ⚠  Rate limited by Binance. Waiting {retry_after} s…")
                time.sleep(retry_after)
                continue

            # Bad symbol / interval → no point retrying
            if resp.status_code == 400:
                try:
                    detail = resp.json().get("msg", resp.text)
                except Exception:
                    detail = resp.text
                raise ValueError(f"Binance rejected the request: {detail}")

            resp.raise_for_status()
            return resp.json()

        raise RuntimeError(f"Binance request failed after {max_attempts} attempts.")

    @staticmethod
    def _parse_kline(k: list) -> dict[str, Any]:
        """
        Convert a raw Binance kline list to our standard candle dict.

        Binance kline format (index → field):
        0  open_time, 1 open, 2 high, 3 low, 4 close, 5 volume,
        6  close_time, 7 quote_asset_volume, 8 number_of_trades,
        9  taker_buy_base_asset_volume, 10 taker_buy_quote_asset_volume
        """
        return {
            "timestamp":       int(k[0]),
            "open":            float(k[1]),
            "high":            float(k[2]),
            "low":             float(k[3]),
            "close":           float(k[4]),
            "volume":          float(k[5]),
            "close_time":      int(k[6]),
            "quote_volume":    float(k[7]),
            "trades":          int(k[8]),
            "taker_buy_base":  float(k[9]),
            "taker_buy_quote": float(k[10]),
        }

    def __repr__(self) -> str:
        return f"<BinanceSource market={self.market!r}>"
