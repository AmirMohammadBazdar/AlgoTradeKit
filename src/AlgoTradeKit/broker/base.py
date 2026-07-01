"""
``BaseBroker`` — the unified interface every venue connector implements.

A strategy / the (future) ``trader`` module only ever sees this interface, so a
Binance spot account, a Binance futures account, and a MetaTrader forex account
are interchangeable.  Each connector maps its venue payloads onto the shared
types in :mod:`AlgoTradeKit.broker._types` and hides all the differences.

Method groups
-------------
* **Market data** (public, no credentials): ``fetch_candles``, ``get_ticker``,
  ``server_time``, ``stream_candles``, ``stream_ticker``.
* **Account** (private): ``get_balance``, ``get_account_info``.
* **Trading** (private): ``create_order`` (+ ``create_market_order`` /
  ``create_limit_order``), ``cancel_order``, ``cancel_all``, ``open_orders``,
  ``open_positions``, ``close_position``, ``set_leverage``.

Optional operations raise :class:`NotSupportedError` by default; private ones
raise :class:`AuthenticationError` when no credentials were supplied.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Callable, Optional

import pandas as pd

from ._errors import AuthenticationError, NotSupportedError
from ._stream import Stream
from ._timeutil import TIMEFRAME_MS, normalize_timeframe, now_ms
from ._types import (
    ORDER_LIMIT,
    ORDER_MARKET,
    TIF_GTC,
    AccountInfo,
    Balance,
    Order,
    OrderResult,
    Position,
    Ticker,
)

_CANDLE_COLUMNS = [
    "timestamp", "open", "high", "low", "close", "volume",
    "close_time", "quote_volume", "trades", "taker_buy_base", "taker_buy_quote",
]


class BaseBroker(ABC):
    """Abstract base for all exchange / broker connectors."""

    #: Human-readable connector name, e.g. ``"binance-futures"``, ``"metatrader"``.
    name: str = "base"
    #: One of ``MARKET_SPOT`` / ``MARKET_FUTURES`` / ``MARKET_FOREX``.
    market: str = ""
    #: Whether this venue has the concept of leveraged positions.
    supports_positions: bool = False

    # Connectors set this True once credentials are validated / present.
    _authenticated: bool = False

    # ------------------------------------------------------------------
    # Auth helpers
    # ------------------------------------------------------------------

    @property
    def is_authenticated(self) -> bool:
        """True when credentials were supplied (private calls are allowed)."""
        return self._authenticated

    def _require_auth(self) -> None:
        if not self._authenticated:
            raise AuthenticationError(
                f"{self.name}: this operation needs API credentials. "
                "Create the broker with api_key/api_secret (or MetaTrader "
                "login/password) to place orders and read account data."
            )

    # ==================================================================
    # Market data (public)
    # ==================================================================

    @abstractmethod
    def fetch_candles(
        self,
        symbol: str,
        timeframe: str,
        start_ms: int,
        end_ms: int,
    ) -> list[dict[str, Any]]:
        """
        Fetch OHLCV candles over ``[start_ms, end_ms]`` (both inclusive, UTC ms).

        Returns candle dicts in the library-standard 11-column schema, sorted
        ascending by ``timestamp`` — the exact shape ``data.CSVHandler`` and the
        rest of the pipeline expect.
        """

    def get_candles_df(
        self,
        symbol: str,
        timeframe: str,
        start_ms: int,
        end_ms: int,
    ) -> pd.DataFrame:
        """Convenience wrapper: :meth:`fetch_candles` as a DataFrame."""
        rows = self.fetch_candles(symbol, timeframe, start_ms, end_ms)
        df = pd.DataFrame(rows, columns=_CANDLE_COLUMNS)
        if not df.empty:
            df["timestamp"] = df["timestamp"].astype("int64")
        return df

    def fetch_last_candles(
        self, symbol: str, timeframe: str, count: int
    ) -> list[dict[str, Any]]:
        """
        The most recent *count* closed candles.  Generic default: derive a start
        time from ``count`` and slice; connectors with a native "last N" call
        (e.g. MetaTrader ``copy_rates_from_pos``) override this.
        """
        tf = normalize_timeframe(timeframe)
        if tf not in TIMEFRAME_MS:
            raise NotSupportedError(f"fetch_last_candles is not available for '{tf}'.")
        end = now_ms()
        start = end - (count + 2) * TIMEFRAME_MS[tf]
        return self.fetch_candles(symbol, tf, start, end)[-count:]

    @abstractmethod
    def get_ticker(self, symbol: str) -> Ticker:
        """Latest price / bid / ask for *symbol*."""

    @abstractmethod
    def server_time(self) -> int:
        """Venue server time as a UTC millisecond timestamp."""

    def stream_candles(
        self,
        symbol: str,
        timeframe: str,
        on_candle: Callable[[dict[str, Any]], None],
        *,
        closed_only: bool = True,
    ) -> Stream:
        """
        Subscribe to real-time candles.  *on_candle* receives library-standard
        candle dicts.  With ``closed_only=True`` only finished candles fire.
        """
        raise NotSupportedError(f"{self.name} does not support candle streaming.")

    def stream_ticker(
        self,
        symbol: str,
        on_tick: Callable[[Ticker], None],
    ) -> Stream:
        """Subscribe to real-time ticker updates."""
        raise NotSupportedError(f"{self.name} does not support ticker streaming.")

    # ==================================================================
    # Account (private)
    # ==================================================================

    @abstractmethod
    def get_balance(self) -> list[Balance]:
        """Per-asset balances (requires credentials)."""

    def get_account_info(self) -> AccountInfo:
        """Account-level snapshot.  Default: wraps :meth:`get_balance`."""
        return AccountInfo(balances=tuple(self.get_balance()))

    # ==================================================================
    # Trading (private)
    # ==================================================================

    @abstractmethod
    def create_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        *,
        type: str = ORDER_MARKET,
        price: Optional[float] = None,
        stop_price: Optional[float] = None,
        time_in_force: str = TIF_GTC,
        reduce_only: bool = False,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        client_order_id: Optional[str] = None,
    ) -> OrderResult:
        """Place an order.  Returns an :class:`OrderResult`."""

    def create_market_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        **kwargs: Any,
    ) -> OrderResult:
        """Convenience: market order."""
        return self.create_order(symbol, side, quantity, type=ORDER_MARKET, **kwargs)

    def create_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: float,
        **kwargs: Any,
    ) -> OrderResult:
        """Convenience: limit order."""
        return self.create_order(
            symbol, side, quantity, type=ORDER_LIMIT, price=price, **kwargs
        )

    @abstractmethod
    def cancel_order(self, order_id: str, symbol: str) -> bool:
        """Cancel one working order.  Returns True on success."""

    def cancel_all(self, symbol: Optional[str] = None) -> int:
        """Cancel all working orders (optionally for one symbol).  Returns count."""
        cancelled = 0
        for order in self.open_orders(symbol):
            if self.cancel_order(order.order_id, order.symbol):
                cancelled += 1
        return cancelled

    @abstractmethod
    def open_orders(self, symbol: Optional[str] = None) -> list[Order]:
        """Working (pending / not-yet-filled) orders."""

    def open_positions(self, symbol: Optional[str] = None) -> list[Position]:
        """
        Open leveraged positions.  Spot venues have none, so the default returns
        an empty list; futures / MetaTrader connectors override this.
        """
        return []

    def close_position(
        self,
        symbol: str,
        quantity: Optional[float] = None,
    ) -> OrderResult:
        """Close (or partially close) an open position."""
        raise NotSupportedError(f"{self.name} does not support close_position.")

    def set_leverage(self, symbol: str, leverage: float) -> None:
        """Set leverage for a symbol (futures / margin only)."""
        raise NotSupportedError(f"{self.name} does not support set_leverage.")

    # ==================================================================
    # Lifecycle
    # ==================================================================

    def close(self) -> None:
        """Release sockets / sessions.  Override in connectors that hold them."""

    def __repr__(self) -> str:
        auth = "auth" if self._authenticated else "public"
        return f"<{self.__class__.__name__} name={self.name!r} {auth}>"
