from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Any


class BaseSource(ABC):
    """
    Contract for all exchange / broker data sources.

    A concrete source only needs to implement two methods:
    ``fetch_candles`` and ``get_server_time``.  Everything else
    (pagination, retries, rate-limiting) lives inside the concrete class.

    Candle dict schema (all sources must return this shape)
    --------------------------------------------------------
    {
        "timestamp":       int    # open-time in UTC milliseconds
        "open":            float
        "high":            float
        "low":             float
        "close":           float
        "volume":          float  # base-asset volume
        "close_time":      int    # close-time in UTC milliseconds
        "quote_volume":    float  # quote-asset volume
        "trades":          int    # number of trades in the candle
        "taker_buy_base":  float
        "taker_buy_quote": float
    }
    """

    # ------------------------------------------------------------------
    # Interface
    # ------------------------------------------------------------------

    @abstractmethod
    def fetch_candles(
        self,
        symbol: str,
        timeframe: str,
        start_ms: int,
        end_ms: int,
    ) -> list[dict[str, Any]]:
        """
        Fetch OHLCV candles for *symbol* over [start_ms, end_ms].

        Parameters
        ----------
        symbol:
            Trading pair in the format the exchange expects, e.g. ``"BTCUSDT"``.
        timeframe:
            Normalised timeframe string, e.g. ``"1d"``, ``"4h"``, ``"1M"``.
        start_ms:
            Range start – UTC millisecond timestamp (inclusive).
        end_ms:
            Range end   – UTC millisecond timestamp (inclusive).

        Returns
        -------
        list[dict]
            Candle dicts sorted ascending by ``timestamp``.
            Empty list if no data exists for the range.
        """

    @abstractmethod
    def get_server_time(self) -> int:
        """Return the exchange server's current time as UTC milliseconds."""

    # ------------------------------------------------------------------
    # Optional hooks (override when needed)
    # ------------------------------------------------------------------

    def validate_symbol(self, symbol: str) -> bool:  # noqa: ARG002
        """Return ``True`` if *symbol* exists on this source. Default: always True."""
        return True

    def validate_timeframe(self, timeframe: str) -> bool:  # noqa: ARG002
        """Return ``True`` if *timeframe* is supported. Default: always True."""
        return True

    # ------------------------------------------------------------------
    # Repr
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}>"
