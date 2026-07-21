"""
``AlgoTradeKit.broker`` — one unified door to every exchange and broker.

Create a connection with :func:`Broker` and hand the result to ``data.Collector``
(for candles / streaming) or, later, to the ``trader`` module (for live orders).
A Binance spot account, a Binance USD-M futures account, and a MetaTrader forex
account all expose the **same** :class:`BaseBroker` interface — each connector
hides its venue's quirks.

Examples
--------
Market data only (no credentials needed)::

    from AlgoTradeKit.broker import Broker
    b = Broker("binance-futures")
    candles = b.fetch_last_candles("BTCUSDT", "1h", 500)

Trading (credentials required)::

    b = Broker("binance-futures", api_key="…", api_secret="…", testnet=True)
    b.create_market_order("BTCUSDT", "buy", 0.001)

MetaTrader forex via the headless Wine bridge (see
``metatrader/bridge_server.py``)::

    mt = Broker("metatrader", server="MyBroker-Demo", login=123, password="…")
    candles = mt.fetch_last_candles("EURUSD", "15m", 1000)
"""
from __future__ import annotations

from ._errors import (
    AuthenticationError,
    BrokerError,
    ConnectionFailed,
    NotSupportedError,
    OrderError,
)
from ._stream import Stream
from ._types import (
    COMMISSION_TYPE_FIXED,
    COMMISSION_TYPE_PER_LOT,
    COMMISSION_TYPE_PERCENTAGE,
    MARKET_FOREX,
    MARKET_FUTURES,
    MARKET_SPOT,
    ORDER_LIMIT,
    ORDER_MARKET,
    ORDER_STOP,
    ORDER_STOP_LIMIT,
    ORDER_TAKE_PROFIT,
    POSITION_LONG,
    POSITION_SHORT,
    SIDE_BUY,
    SIDE_SELL,
    STATUS_CANCELED,
    STATUS_EXPIRED,
    STATUS_FILLED,
    STATUS_NEW,
    STATUS_PARTIALLY_FILLED,
    STATUS_REJECTED,
    TIF_FOK,
    TIF_GTC,
    TIF_IOC,
    AccountInfo,
    Balance,
    Order,
    OrderResult,
    Position,
    Ticker,
    TradingCosts,
)
from .base import BaseBroker
from .exchange.binance import BinanceBroker
from .metatrader import MetaTraderBroker

_BINANCE_ALIASES = {
    "binance": "spot", "binance-spot": "spot", "binancespot": "spot", "binance_spot": "spot",
    "binance-futures": "futures", "binancefutures": "futures", "binance_futures": "futures",
}
_MT_ALIASES = {"metatrader", "metatrader5", "mt5", "mt", "forex"}


def Broker(
    name: str,
    api_key: str | None = None,
    api_secret: str | None = None,
    *,
    market: str | None = None,
    testnet: bool = False,
    recv_window: int = 5000,
    # MetaTrader-specific
    server: str | None = None,
    login: int | None = None,
    password: str | None = None,
    mode: str = "auto",
    host: str = "127.0.0.1",
    port: int = 18812,
    timeout: float = 30.0,
    candle_poll_interval: float = 1.0,
    tick_poll_interval: float = 0.2,
    connect: bool = True,
) -> BaseBroker:
    """
    Build a broker connection from a venue name.

    ``name`` is case-insensitive: ``"binance-spot"``, ``"binance-futures"``,
    ``"metatrader"`` (aliases: ``mt5``, ``forex``).  Credentials are optional —
    without them only public market-data calls work.

    MetaTrader picks its transport by ``mode``: ``"auto"`` (default) uses the
    Wine bridge for a non-default ``host`` or on Linux/macOS, and the native
    ``MetaTrader5`` package on Windows; ``"native"`` / ``"bridge"`` force it.
    MetaTrader streaming (``stream_candles`` / ``stream_ticker``) polls the
    transport at ``candle_poll_interval`` / ``tick_poll_interval`` seconds
    (MT5 has no push feed); Binance streams over WebSocket and ignores both.

    Returns a concrete :class:`BaseBroker`, so the whole library can treat every
    venue identically.
    """
    key = name.lower().strip()

    if key in _MT_ALIASES:
        return MetaTraderBroker(
            server=server, login=login, password=password, mode=mode,
            host=host, port=port, timeout=timeout,
            candle_poll_interval=candle_poll_interval,
            tick_poll_interval=tick_poll_interval, connect=connect,
        )

    if key in _BINANCE_ALIASES or key.startswith("binance"):
        resolved_market = market or _BINANCE_ALIASES.get(key, "spot")
        return BinanceBroker(
            market=resolved_market, api_key=api_key, api_secret=api_secret,
            testnet=testnet, recv_window=recv_window,
        )

    raise ValueError(
        f"Unknown broker '{name}'. Supported: 'binance-spot', 'binance-futures', "
        "'metatrader'."
    )


# Backwards-friendly alias — some callers may prefer a verb.
create_broker = Broker

__all__ = [
    # entry points
    "Broker", "create_broker", "BaseBroker",
    "BinanceBroker", "MetaTraderBroker", "Stream",
    # types
    "Balance", "AccountInfo", "Ticker", "Order", "OrderResult", "Position",
    "TradingCosts",
    # errors
    "BrokerError", "AuthenticationError", "NotSupportedError", "OrderError",
    "ConnectionFailed",
    # constants
    "SIDE_BUY", "SIDE_SELL",
    "ORDER_MARKET", "ORDER_LIMIT", "ORDER_STOP", "ORDER_STOP_LIMIT", "ORDER_TAKE_PROFIT",
    "TIF_GTC", "TIF_IOC", "TIF_FOK",
    "POSITION_LONG", "POSITION_SHORT",
    "MARKET_SPOT", "MARKET_FUTURES", "MARKET_FOREX",
    "COMMISSION_TYPE_PERCENTAGE", "COMMISSION_TYPE_PER_LOT", "COMMISSION_TYPE_FIXED",
    "STATUS_NEW", "STATUS_PARTIALLY_FILLED", "STATUS_FILLED",
    "STATUS_CANCELED", "STATUS_REJECTED", "STATUS_EXPIRED",
]
