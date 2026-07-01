"""
``BinanceSource`` — a thin ``BaseSource`` adapter over the ``broker`` module.

As of v0.9.0 all Binance REST/WebSocket logic lives in
``AlgoTradeKit.broker.exchange.binance``.  This adapter keeps the historical
``data.sources`` contract (``fetch_candles`` / ``get_server_time``) so the
``Collector`` pipeline and the source registry are unchanged — it just forwards
to the broker connector.
"""
from __future__ import annotations

from typing import Any

from ...broker.exchange.binance import fetch_candles as _broker_fetch_candles
from ...broker.exchange.binance import get_server_time as _broker_server_time
from ...broker.exchange.binance._endpoints import INTERVAL_MAP
from .base import BaseSource


class BinanceSource(BaseSource):
    """
    Fetch OHLCV klines from Binance (spot or USD-M futures).

    Parameters
    ----------
    market:
        ``"spot"`` or ``"futures"``.
    testnet:
        Use Binance testnet endpoints.
    """

    def __init__(self, market: str = "spot", testnet: bool = False) -> None:
        market = market.lower().strip()
        if market not in ("spot", "futures"):
            raise ValueError(f"market must be 'spot' or 'futures', got '{market}'")
        self.market = market
        self.testnet = testnet

    def fetch_candles(
        self,
        symbol: str,
        timeframe: str,
        start_ms: int,
        end_ms: int,
    ) -> list[dict[str, Any]]:
        return _broker_fetch_candles(
            self.market, symbol, timeframe, start_ms, end_ms, self.testnet
        )

    def get_server_time(self) -> int:
        return _broker_server_time(self.market, self.testnet)

    def validate_timeframe(self, timeframe: str) -> bool:
        return timeframe in INTERVAL_MAP

    def __repr__(self) -> str:
        return f"<BinanceSource market={self.market!r}>"
