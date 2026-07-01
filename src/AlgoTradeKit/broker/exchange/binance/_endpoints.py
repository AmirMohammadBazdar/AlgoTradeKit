"""
Binance REST + WebSocket base URLs and the timeframe → interval map.

Two markets (``spot`` / ``futures``) × two networks (live / testnet).
"""
from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Base URLs
# ---------------------------------------------------------------------------

_REST = {
    ("spot", False):    "https://api.binance.com",
    ("spot", True):     "https://testnet.binance.vision",
    ("futures", False): "https://fapi.binance.com",
    ("futures", True):  "https://testnet.binancefuture.com",
}

_WS = {
    ("spot", False):    "wss://stream.binance.com:9443/ws",
    ("spot", True):     "wss://stream.testnet.binance.vision/ws",
    ("futures", False): "wss://fstream.binance.com/ws",
    ("futures", True):  "wss://stream.binancefuture.com/ws",
}

#: Binance interval strings, keyed by our normalised timeframe.
INTERVAL_MAP: dict[str, str] = {
    "1m": "1m",  "3m": "3m",  "5m": "5m",  "15m": "15m", "30m": "30m",
    "1h": "1h",  "2h": "2h",  "4h": "4h",  "6h": "6h",
    "8h": "8h",  "12h": "12h",
    "1d": "1d",  "3d": "3d",
    "1w": "1w",
    "1M": "1M",
}

MAX_KLINES_PER_REQUEST = 1000


@dataclass(frozen=True)
class Endpoints:
    """Resolved URLs + REST path prefixes for one (market, network) combo."""
    market: str
    testnet: bool
    rest_base: str
    ws_base: str

    # ---- REST paths (differ between spot and futures) ----
    @property
    def _api(self) -> str:
        return "/fapi/v1" if self.market == "futures" else "/api/v3"

    @property
    def klines(self) -> str:
        return f"{self.rest_base}{self._api}/klines"

    @property
    def ticker_price(self) -> str:
        return f"{self.rest_base}{self._api}/ticker/price"

    @property
    def book_ticker(self) -> str:
        return f"{self.rest_base}{self._api}/ticker/bookTicker"

    @property
    def server_time(self) -> str:
        return f"{self.rest_base}{self._api}/time"

    @property
    def order(self) -> str:
        return f"{self.rest_base}{self._api}/order"

    @property
    def open_orders(self) -> str:
        return f"{self.rest_base}{self._api}/openOrders"

    @property
    def all_open_orders(self) -> str:
        # Futures-only bulk cancel
        return f"{self.rest_base}/fapi/v1/allOpenOrders"

    @property
    def account(self) -> str:
        # Futures account lives at v2
        if self.market == "futures":
            return f"{self.rest_base}/fapi/v2/account"
        return f"{self.rest_base}/api/v3/account"

    @property
    def balance(self) -> str:
        return f"{self.rest_base}/fapi/v2/balance"      # futures only

    @property
    def position_risk(self) -> str:
        return f"{self.rest_base}/fapi/v2/positionRisk"  # futures only

    @property
    def leverage(self) -> str:
        return f"{self.rest_base}/fapi/v1/leverage"      # futures only

    @property
    def listen_key(self) -> str:
        if self.market == "futures":
            return f"{self.rest_base}/fapi/v1/listenKey"
        return f"{self.rest_base}/api/v3/userDataStream"


def resolve_endpoints(market: str, testnet: bool) -> Endpoints:
    """Build an :class:`Endpoints` for a (market, network) pair."""
    market = market.lower().strip()
    if market not in ("spot", "futures"):
        raise ValueError(f"market must be 'spot' or 'futures', got '{market}'")
    return Endpoints(
        market=market,
        testnet=testnet,
        rest_base=_REST[(market, testnet)],
        ws_base=_WS[(market, testnet)],
    )
