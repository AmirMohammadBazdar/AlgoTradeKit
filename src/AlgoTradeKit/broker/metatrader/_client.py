"""
``MetaTraderBroker`` — unified connector for a MetaTrader 5 account.

Runs on the normal Linux side of AlgoTradeKit and reaches MT5 only through the
bridge server (:mod:`bridge_server`) running in the Wine Python.  Everything is
mapped onto the shared ``broker`` types, so a MetaTrader forex account behaves
exactly like a Binance connector to the rest of the library.
"""
from __future__ import annotations

from typing import Any, Optional

from ..base import BaseBroker
from .._errors import BrokerError, ConnectionFailed, NotSupportedError, OrderError
from .._timeutil import normalize_timeframe, now_ms
from .._types import (
    MARKET_FOREX,
    ORDER_LIMIT,
    ORDER_MARKET,
    ORDER_STOP,
    POSITION_LONG,
    POSITION_SHORT,
    SIDE_BUY,
    SIDE_SELL,
    STATUS_FILLED,
    STATUS_NEW,
    STATUS_REJECTED,
    AccountInfo,
    Balance,
    Order,
    OrderResult,
    Position,
    Ticker,
)
from ._bridge_client import BridgeClient

_TRADE_RETCODE_DONE = 10009  # MetaTrader5.TRADE_RETCODE_DONE

# MT5 pending-order type ints → (unified side, unified order type)
_MT_ORDER_TYPE = {
    2: (SIDE_BUY, ORDER_LIMIT), 3: (SIDE_SELL, ORDER_LIMIT),
    4: (SIDE_BUY, ORDER_STOP),  5: (SIDE_SELL, ORDER_STOP),
}

_ORDER_KIND = {ORDER_MARKET: "market", ORDER_LIMIT: "limit", ORDER_STOP: "stop"}


class MetaTraderBroker(BaseBroker):
    """
    MetaTrader 5 connector (via the headless Wine bridge).

    Parameters
    ----------
    server / login / password:
        Broker credentials.  If given, the connector logs the terminal in;
        otherwise it uses whatever account the bridge was started with.
    host / port:
        Where the bridge server is listening (default ``127.0.0.1:18812``).
    transport:
        Injectable RPC transport (used by tests); defaults to a
        :class:`BridgeClient`.
    connect:
        Probe the bridge in ``__init__`` (raises early with a setup hint if the
        bridge is unreachable).  Set ``False`` to defer.
    """

    def __init__(
        self,
        server: Optional[str] = None,
        login: Optional[int] = None,
        password: Optional[str] = None,
        *,
        host: str = "127.0.0.1",
        port: int = 18812,
        timeout: float = 30.0,
        transport: Any = None,
        connect: bool = True,
    ) -> None:
        self.market = MARKET_FOREX
        self.name = "metatrader"
        self.supports_positions = True

        self._transport = transport if transport is not None else BridgeClient(host, port, timeout)
        self._server = server
        self._login = login
        self._account_currency = ""
        self._authenticated = False

        if connect:
            self._connect(login, password, server)

    # ------------------------------------------------------------------
    # Connection / auth
    # ------------------------------------------------------------------

    def _connect(self, login, password, server) -> None:
        if login and password and server:
            try:
                self._transport.call("login", login, password, server)
            except BrokerError as exc:
                raise BrokerError(f"MetaTrader login failed: {exc}") from exc
        # Probe account so is_authenticated reflects reality. A truly unreachable
        # bridge surfaces early (with its setup hint); "bridge up but not logged
        # in" is tolerated (data-only usage stays possible).
        try:
            info = self._transport.call("account_info")
            self._authenticated = int(info.get("login", 0)) > 0
            self._account_currency = info.get("currency", "")
        except ConnectionFailed:
            raise
        except BrokerError:
            self._authenticated = False

    def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        return self._transport.call(method, *args, **kwargs)

    # ==================================================================
    # Market data
    # ==================================================================

    def fetch_candles(
        self, symbol: str, timeframe: str, start_ms: int, end_ms: int
    ) -> list[dict[str, Any]]:
        tf = normalize_timeframe(timeframe)
        return self._call("candles_range", symbol, tf, start_ms, end_ms)

    def fetch_last_candles(self, symbol: str, timeframe: str, count: int) -> list[dict[str, Any]]:
        tf = normalize_timeframe(timeframe)
        return self._call("candles_from_count", symbol, tf, int(count))

    def list_symbols(self, group: str = "*") -> list[str]:
        """
        Available symbol names on the account (MetaTrader-specific helper).

        Pass a filter to narrow it down, e.g. ``"*BTC*"`` or ``"*EUR*"`` — handy
        when you don't know the broker's exact symbol spelling.
        """
        return self._call("symbols", group)

    def get_ticker(self, symbol: str) -> Ticker:
        t = self._call("tick", symbol)
        bid, ask = float(t.get("bid", 0.0)), float(t.get("ask", 0.0))
        last = float(t.get("last") or 0.0) or ((bid + ask) / 2 if bid and ask else bid or ask)
        return Ticker(symbol=symbol, last=last, bid=bid, ask=ask,
                      timestamp=int(t.get("time_ms", now_ms())), raw=t)

    def server_time(self) -> int:
        # MT5 exposes no clean broker-server clock; the terminal host clock is the
        # closest proxy and the feed timestamps are authoritative anyway.
        return now_ms()

    # ==================================================================
    # Account
    # ==================================================================

    def get_balance(self) -> list[Balance]:
        self._require_auth()
        info = self._call("account_info")
        return [Balance(asset=info.get("currency", ""),
                        free=float(info.get("margin_free", 0.0)),
                        locked=float(info.get("margin", 0.0)),
                        total=float(info.get("balance", 0.0)))]

    def get_account_info(self) -> AccountInfo:
        self._require_auth()
        info = self._call("account_info")
        cur = info.get("currency", "")
        return AccountInfo(
            balances=(Balance(asset=cur, free=float(info.get("margin_free", 0.0)),
                              locked=float(info.get("margin", 0.0)),
                              total=float(info.get("balance", 0.0))),),
            currency=cur,
            equity=float(info.get("equity", 0.0)),
            wallet_balance=float(info.get("balance", 0.0)),
            available=float(info.get("margin_free", 0.0)),
            margin_used=float(info.get("margin", 0.0)),
            unrealized_pnl=float(info.get("profit", 0.0)),
            leverage=float(info.get("leverage", 1.0)),
            raw=info,
        )

    # ==================================================================
    # Trading
    # ==================================================================

    def create_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        *,
        type: str = ORDER_MARKET,
        price: Optional[float] = None,
        stop_price: Optional[float] = None,
        time_in_force: str = "gtc",
        reduce_only: bool = False,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        client_order_id: Optional[str] = None,
    ) -> OrderResult:
        self._require_auth()
        kind = _ORDER_KIND.get(type)
        if kind is None:
            raise OrderError(f"MetaTrader supports market/limit/stop, not '{type}'.")

        spec: dict[str, Any] = {
            "symbol": symbol, "side": side, "order_kind": kind, "volume": float(quantity),
        }
        if kind != "market":
            trigger = price if price is not None else stop_price
            if trigger is None:
                raise OrderError(f"{kind} order needs a price / stop_price.")
            spec["price"] = float(trigger)
        if stop_loss is not None:
            spec["sl"] = float(stop_loss)
        if take_profit is not None:
            spec["tp"] = float(take_profit)
        if client_order_id:
            spec["comment"] = str(client_order_id)

        resp = self._call("place_order", spec)
        return self._order_result(resp, symbol, side)

    @staticmethod
    def _order_result(resp: dict, symbol: str, side: str) -> OrderResult:
        retcode = int(resp.get("retcode", 0))
        ok = retcode == _TRADE_RETCODE_DONE
        oid = resp.get("order") or resp.get("deal") or 0
        return OrderResult(
            order_id=str(oid),
            symbol=symbol,
            side=side,
            status=STATUS_FILLED if ok else STATUS_REJECTED,
            filled_quantity=float(resp.get("volume", 0.0) or 0.0),
            avg_fill_price=float(resp.get("price", 0.0) or 0.0),
            raw=resp,
        )

    def cancel_order(self, order_id: str, symbol: str) -> bool:
        self._require_auth()
        resp = self._call("cancel_order", int(order_id))
        return int(resp.get("retcode", 0)) == _TRADE_RETCODE_DONE

    def open_orders(self, symbol: Optional[str] = None) -> list[Order]:
        self._require_auth()
        rows = self._call("pending_orders", symbol)
        out = []
        for o in rows:
            side, otype = _MT_ORDER_TYPE.get(int(o.get("type", -1)), (SIDE_BUY, ORDER_LIMIT))
            out.append(Order(
                order_id=str(o.get("ticket", "")),
                symbol=o.get("symbol", ""),
                side=side, type=otype,
                quantity=float(o.get("volume_current", o.get("volume_initial", 0.0)) or 0.0),
                price=float(o.get("price_open", 0.0) or 0.0) or None,
                stop_price=float(o.get("price_open", 0.0) or 0.0) if otype == ORDER_STOP else None,
                status=STATUS_NEW,
                timestamp=int(o.get("time_setup", 0) or 0) * 1000,
                raw=o,
            ))
        return out

    def open_positions(self, symbol: Optional[str] = None) -> list[Position]:
        self._require_auth()
        rows = self._call("positions", symbol)
        out = []
        for p in rows:
            is_long = int(p.get("type", 0)) == 0
            out.append(Position(
                symbol=p.get("symbol", ""),
                side=POSITION_LONG if is_long else POSITION_SHORT,
                quantity=float(p.get("volume", 0.0) or 0.0),
                entry_price=float(p.get("price_open", 0.0) or 0.0),
                mark_price=float(p.get("price_current", 0.0) or 0.0),
                unrealized_pnl=float(p.get("profit", 0.0) or 0.0),
                stop_loss=float(p.get("sl", 0.0) or 0.0) or None,
                take_profit=float(p.get("tp", 0.0) or 0.0) or None,
                position_id=str(p.get("ticket", "")),
                timestamp=int(p.get("time", 0) or 0) * 1000,
                raw=p,
            ))
        return out

    def close_position(self, symbol: str, quantity: Optional[float] = None) -> OrderResult:
        self._require_auth()
        resp = self._call("close_position", None, symbol, quantity)
        return self._order_result(resp, symbol, "")

    def modify_position(
        self, ticket: int, stop_loss: Optional[float] = None, take_profit: Optional[float] = None
    ) -> OrderResult:
        """Move SL / TP of an open position (MetaTrader-specific helper)."""
        self._require_auth()
        resp = self._call("modify_position", int(ticket), stop_loss, take_profit)
        return self._order_result(resp, "", "")

    def set_leverage(self, symbol: str, leverage: float) -> None:
        raise NotSupportedError(
            "MetaTrader leverage is set per-account by the broker, not per-order."
        )

    def close(self) -> None:
        try:
            self._transport.close()
        except Exception:  # noqa: BLE001
            pass
