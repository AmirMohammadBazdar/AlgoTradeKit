"""
``BinanceBroker`` — unified connector for Binance spot and USD-M futures.

Maps every Binance REST / WebSocket payload onto the shared ``broker`` types so
the rest of AlgoTradeKit treats Binance exactly like any other venue.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from ...base import BaseBroker
from ..._errors import NotSupportedError, OrderError
from ..._stream import Stream
from ..._timeutil import next_open_ms, normalize_timeframe, now_ms
from ..._types import (
    MARKET_FUTURES,
    MARKET_SPOT,
    ORDER_LIMIT,
    ORDER_MARKET,
    ORDER_STOP,
    ORDER_STOP_LIMIT,
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
)
from ._endpoints import INTERVAL_MAP, MAX_KLINES_PER_REQUEST, resolve_endpoints
from ._rest import BinanceREST
from ._ws import UserDataStream, stream_book_ticker, stream_kline

# ---------------------------------------------------------------------------
# Small mapping tables
# ---------------------------------------------------------------------------

_TIF_TO_BINANCE = {TIF_GTC: "GTC", TIF_IOC: "IOC", TIF_FOK: "FOK"}
_TIF_FROM_BINANCE = {v: k for k, v in _TIF_TO_BINANCE.items()}

_STATUS_MAP = {
    "NEW": STATUS_NEW,
    "PARTIALLY_FILLED": STATUS_PARTIALLY_FILLED,
    "FILLED": STATUS_FILLED,
    "CANCELED": STATUS_CANCELED,
    "PENDING_CANCEL": STATUS_CANCELED,
    "REJECTED": STATUS_REJECTED,
    "EXPIRED": STATUS_EXPIRED,
}

_TYPE_FROM_BINANCE = {
    "MARKET": ORDER_MARKET,
    "LIMIT": ORDER_LIMIT,
    "STOP_MARKET": ORDER_STOP,
    "STOP_LOSS": ORDER_STOP,
    "STOP": ORDER_STOP_LIMIT,
    "STOP_LOSS_LIMIT": ORDER_STOP_LIMIT,
    "TAKE_PROFIT_MARKET": ORDER_STOP,
    "TAKE_PROFIT": ORDER_STOP,
}


def _num(x: float) -> str:
    """Format a number for Binance (no scientific notation, no trailing zeros)."""
    return f"{x:.8f}".rstrip("0").rstrip(".")


class BinanceBroker(BaseBroker):
    """
    Binance connector.

    Parameters
    ----------
    market:
        ``"spot"`` or ``"futures"`` (USD-M).
    api_key / api_secret:
        Optional.  Without them only public market-data calls work; private
        (account / trading) calls raise :class:`AuthenticationError`.
    testnet:
        Point at Binance testnet endpoints instead of live.
    """

    def __init__(
        self,
        market: str = MARKET_SPOT,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        *,
        testnet: bool = False,
        recv_window: int = 5000,
    ) -> None:
        market = market.lower().strip()
        self.market = MARKET_FUTURES if market == "futures" else MARKET_SPOT
        self.testnet = testnet
        self.name = f"binance-{self.market}" + ("-testnet" if testnet else "")
        self.supports_positions = self.market == MARKET_FUTURES

        self._ep = resolve_endpoints(self.market, testnet)
        self._rest = BinanceREST(self._ep, api_key, api_secret, recv_window)
        self._authenticated = self._rest.has_credentials
        self._user_stream: UserDataStream | None = None

    # ==================================================================
    # Market data (public)
    # ==================================================================

    def _interval(self, timeframe: str) -> str:
        tf = normalize_timeframe(timeframe)
        if tf not in INTERVAL_MAP:
            raise ValueError(f"Binance does not support timeframe '{tf}'.")
        return INTERVAL_MAP[tf]

    def fetch_candles(
        self, symbol: str, timeframe: str, start_ms: int, end_ms: int
    ) -> list[dict[str, Any]]:
        tf = normalize_timeframe(timeframe)
        interval = self._interval(tf)
        symbol = symbol.upper()

        out: list[dict[str, Any]] = []
        cursor = start_ms
        while cursor <= end_ms:
            raw = self._rest.fetch_klines(
                symbol, interval, cursor, end_ms, MAX_KLINES_PER_REQUEST
            )
            if not raw:
                break
            out.extend(self._parse_kline(k) for k in raw)
            if len(raw) < MAX_KLINES_PER_REQUEST:
                break
            cursor = next_open_ms(int(raw[-1][0]), tf)
        return out

    @staticmethod
    def _parse_kline(k: list) -> dict[str, Any]:
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

    def get_ticker(self, symbol: str) -> Ticker:
        symbol = symbol.upper()
        price = self._rest.public("GET", self._ep.ticker_price, {"symbol": symbol})
        book = self._rest.public("GET", self._ep.book_ticker, {"symbol": symbol})
        return Ticker(
            symbol=symbol,
            last=float(price["price"]),
            bid=float(book.get("bidPrice", 0.0)),
            ask=float(book.get("askPrice", 0.0)),
            timestamp=now_ms(),
            raw={"price": price, "book": book},
        )

    def server_time(self) -> int:
        return self._rest.get_server_time()

    # ---- streaming ----

    def stream_candles(
        self,
        symbol: str,
        timeframe: str,
        on_candle: Callable[[dict[str, Any]], None],
        *,
        closed_only: bool = True,
    ) -> Stream:
        interval = self._interval(timeframe)

        def _handle(msg: dict[str, Any]) -> None:
            k = msg.get("k")
            if not k:
                return
            if closed_only and not k.get("x", False):
                return
            on_candle({
                "timestamp":       int(k["t"]),
                "open":            float(k["o"]),
                "high":            float(k["h"]),
                "low":             float(k["l"]),
                "close":           float(k["c"]),
                "volume":          float(k["v"]),
                "close_time":      int(k["T"]),
                "quote_volume":    float(k.get("q", 0.0)),
                "trades":          int(k.get("n", 0)),
                "taker_buy_base":  float(k.get("V", 0.0)),
                "taker_buy_quote": float(k.get("Q", 0.0)),
                "closed":          bool(k.get("x", False)),
            })

        return stream_kline(self._ep, symbol, interval, _handle)

    def stream_ticker(self, symbol: str, on_tick: Callable[[Ticker], None]) -> Stream:
        sym = symbol.upper()

        def _handle(msg: dict[str, Any]) -> None:
            bid = float(msg.get("b", 0.0))
            ask = float(msg.get("a", 0.0))
            last = (bid + ask) / 2 if (bid and ask) else (bid or ask)
            on_tick(Ticker(symbol=sym, last=last, bid=bid, ask=ask, timestamp=now_ms(), raw=msg))

        return stream_book_ticker(self._ep, symbol, _handle)

    def stream_user_data(self, on_event: Callable[[dict[str, Any]], None]) -> UserDataStream:
        """Open the private account event stream (order / balance / position updates)."""
        self._require_auth()
        self._user_stream = UserDataStream(self._rest, self._ep, on_event).start()
        return self._user_stream

    # ==================================================================
    # Account (private)
    # ==================================================================

    def get_balance(self) -> list[Balance]:
        self._require_auth()
        if self.market == MARKET_FUTURES:
            rows = self._rest.signed("GET", self._ep.balance, {})
            out = []
            for r in rows:
                total = float(r["balance"])
                avail = float(r.get("availableBalance", total))
                if total or avail:
                    out.append(Balance(asset=r["asset"], free=avail,
                                       locked=max(total - avail, 0.0), total=total))
            return out
        # spot
        acct = self._rest.signed("GET", self._ep.account, {})
        out = []
        for b in acct.get("balances", []):
            free, locked = float(b["free"]), float(b["locked"])
            if free or locked:
                out.append(Balance(asset=b["asset"], free=free, locked=locked))
        return out

    def get_account_info(self) -> AccountInfo:
        self._require_auth()
        if self.market != MARKET_FUTURES:
            return AccountInfo(balances=tuple(self.get_balance()))
        acct = self._rest.signed("GET", self._ep.account, {})
        balances = tuple(
            Balance(asset=a["asset"], free=float(a.get("availableBalance", 0.0)),
                    locked=0.0, total=float(a.get("walletBalance", 0.0)))
            for a in acct.get("assets", []) if float(a.get("walletBalance", 0.0))
        )
        return AccountInfo(
            balances=balances,
            currency="USDT",
            equity=float(acct.get("totalMarginBalance", 0.0)),
            wallet_balance=float(acct.get("totalWalletBalance", 0.0)),
            available=float(acct.get("availableBalance", 0.0)),
            margin_used=float(acct.get("totalInitialMargin", 0.0)),
            unrealized_pnl=float(acct.get("totalUnrealizedProfit", 0.0)),
            raw=acct,
        )

    # ==================================================================
    # Trading (private)
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
        time_in_force: str = TIF_GTC,
        reduce_only: bool = False,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        client_order_id: Optional[str] = None,
    ) -> OrderResult:
        self._require_auth()
        symbol = symbol.upper()
        # Fail before touching the venue: attached SL/TP is futures-only.
        if (stop_loss is not None or take_profit is not None) and self.market != MARKET_FUTURES:
            raise NotSupportedError(
                "Attached stop_loss/take_profit is futures-only. On spot, place a "
                "separate OCO/stop order after the entry fills."
            )
        params = self._build_order_params(
            symbol, side, quantity, type, price, stop_price,
            time_in_force, reduce_only, client_order_id,
        )
        resp = self._rest.signed("POST", self._ep.order, params)
        result = self._order_result(resp, symbol, side)

        # Best-effort protective orders (futures only — spot needs an OCO order).
        if stop_loss is not None or take_profit is not None:
            protective = self._attach_sl_tp(symbol, side, quantity, stop_loss, take_profit)
            if protective:
                object.__setattr__(result, "raw", {**result.raw, "protective": protective})
        return result

    def _build_order_params(
        self, symbol, side, quantity, order_type, price, stop_price,
        tif, reduce_only, client_order_id,
    ) -> dict:
        p: dict[str, Any] = {
            "symbol": symbol,
            "side": "BUY" if side == SIDE_BUY else "SELL",
            "quantity": _num(quantity),
        }
        if order_type == ORDER_MARKET:
            p["type"] = "MARKET"
        elif order_type == ORDER_LIMIT:
            if price is None:
                raise OrderError("Limit order requires a price.")
            p.update(type="LIMIT", price=_num(price),
                     timeInForce=_TIF_TO_BINANCE.get(tif, "GTC"))
        elif order_type == ORDER_STOP:
            if stop_price is None:
                raise OrderError("Stop order requires a stop_price.")
            p["type"] = "STOP_MARKET" if self.market == MARKET_FUTURES else "STOP_LOSS"
            p["stopPrice"] = _num(stop_price)
        elif order_type == ORDER_STOP_LIMIT:
            if price is None or stop_price is None:
                raise OrderError("Stop-limit order requires price and stop_price.")
            p["type"] = "STOP" if self.market == MARKET_FUTURES else "STOP_LOSS_LIMIT"
            p.update(price=_num(price), stopPrice=_num(stop_price),
                     timeInForce=_TIF_TO_BINANCE.get(tif, "GTC"))
        else:
            raise OrderError(f"Unsupported order type '{order_type}'.")

        if reduce_only and self.market == MARKET_FUTURES:
            p["reduceOnly"] = "true"
        if client_order_id:
            p["newClientOrderId"] = client_order_id
        return p

    def _attach_sl_tp(self, symbol, entry_side, quantity, stop_loss, take_profit) -> dict:
        if self.market != MARKET_FUTURES:
            raise NotSupportedError(
                "Attached stop_loss/take_profit is futures-only. On spot, place "
                "a separate OCO/stop order after the entry fills."
            )
        exit_side = "SELL" if entry_side == SIDE_BUY else "BUY"
        placed: dict[str, Any] = {}
        for label, trigger, btype in (
            ("stop_loss", stop_loss, "STOP_MARKET"),
            ("take_profit", take_profit, "TAKE_PROFIT_MARKET"),
        ):
            if trigger is None:
                continue
            try:
                resp = self._rest.signed("POST", self._ep.order, {
                    "symbol": symbol, "side": exit_side, "type": btype,
                    "stopPrice": _num(trigger), "closePosition": "true",
                    "quantity": _num(quantity), "reduceOnly": "true",
                })
                placed[label] = str(resp.get("orderId"))
            except OrderError as exc:
                placed[f"{label}_error"] = str(exc)
        return placed

    def _order_result(self, resp: dict, symbol: str, side: str) -> OrderResult:
        executed = float(resp.get("executedQty", 0.0) or 0.0)
        avg = float(resp.get("avgPrice", 0.0) or 0.0)
        if not avg:
            fills = resp.get("fills") or []
            if fills:
                qty = sum(float(f["qty"]) for f in fills)
                cost = sum(float(f["qty"]) * float(f["price"]) for f in fills)
                avg = cost / qty if qty else 0.0
            elif executed:
                avg = float(resp.get("cummulativeQuoteQty", 0.0) or 0.0) / executed
        return OrderResult(
            order_id=str(resp.get("orderId", "")),
            symbol=symbol,
            side=side,
            status=_STATUS_MAP.get(resp.get("status", "NEW"), STATUS_NEW),
            filled_quantity=executed,
            avg_fill_price=avg,
            client_order_id=resp.get("clientOrderId", ""),
            raw=resp,
        )

    def cancel_order(self, order_id: str, symbol: str) -> bool:
        self._require_auth()
        self._rest.signed("DELETE", self._ep.order,
                          {"symbol": symbol.upper(), "orderId": order_id})
        return True

    def cancel_all(self, symbol: Optional[str] = None) -> int:
        self._require_auth()
        if self.market == MARKET_FUTURES and symbol:
            self._rest.signed("DELETE", self._ep.all_open_orders, {"symbol": symbol.upper()})
            return -1  # Binance returns a status, not a count
        return super().cancel_all(symbol)

    def open_orders(self, symbol: Optional[str] = None) -> list[Order]:
        self._require_auth()
        params = {"symbol": symbol.upper()} if symbol else {}
        rows = self._rest.signed("GET", self._ep.open_orders, params)
        return [self._parse_order(o) for o in rows]

    def _parse_order(self, o: dict) -> Order:
        price = float(o.get("price", 0.0) or 0.0)
        stop = float(o.get("stopPrice", 0.0) or 0.0)
        return Order(
            order_id=str(o.get("orderId", "")),
            symbol=o.get("symbol", ""),
            side=SIDE_BUY if o.get("side") == "BUY" else SIDE_SELL,
            type=_TYPE_FROM_BINANCE.get(o.get("type", ""), o.get("type", "").lower()),
            quantity=float(o.get("origQty", 0.0) or 0.0),
            price=price or None,
            stop_price=stop or None,
            status=_STATUS_MAP.get(o.get("status", "NEW"), STATUS_NEW),
            filled_quantity=float(o.get("executedQty", 0.0) or 0.0),
            timestamp=int(o.get("time") or o.get("updateTime") or 0),
            client_order_id=o.get("clientOrderId", ""),
            reduce_only=bool(o.get("reduceOnly", False)),
            time_in_force=_TIF_FROM_BINANCE.get(o.get("timeInForce", "GTC"), TIF_GTC),
            raw=o,
        )

    def open_positions(self, symbol: Optional[str] = None) -> list[Position]:
        if self.market != MARKET_FUTURES:
            return []
        self._require_auth()
        params = {"symbol": symbol.upper()} if symbol else {}
        rows = self._rest.signed("GET", self._ep.position_risk, params)
        out = []
        for r in rows:
            amt = float(r.get("positionAmt", 0.0) or 0.0)
            if amt == 0.0:
                continue
            liq = float(r.get("liquidationPrice", 0.0) or 0.0)
            out.append(Position(
                symbol=r.get("symbol", ""),
                side=POSITION_LONG if amt > 0 else POSITION_SHORT,
                quantity=abs(amt),
                entry_price=float(r.get("entryPrice", 0.0) or 0.0),
                mark_price=float(r.get("markPrice", 0.0) or 0.0),
                unrealized_pnl=float(r.get("unRealizedProfit", 0.0) or 0.0),
                leverage=float(r.get("leverage", 1.0) or 1.0),
                liquidation_price=liq or None,
                position_id=r.get("symbol", ""),
                raw=r,
            ))
        return out

    def close_position(self, symbol: str, quantity: Optional[float] = None) -> OrderResult:
        if self.market != MARKET_FUTURES:
            raise NotSupportedError("close_position is futures-only.")
        self._require_auth()
        positions = self.open_positions(symbol)
        if not positions:
            raise OrderError(f"No open position for {symbol}.")
        pos = positions[0]
        qty = quantity if quantity is not None else pos.quantity
        exit_side = SIDE_SELL if pos.side == POSITION_LONG else SIDE_BUY
        return self.create_order(symbol, exit_side, qty,
                                 type=ORDER_MARKET, reduce_only=True)

    def set_leverage(self, symbol: str, leverage: float) -> None:
        if self.market != MARKET_FUTURES:
            raise NotSupportedError("set_leverage is futures-only.")
        self._require_auth()
        self._rest.signed("POST", self._ep.leverage,
                          {"symbol": symbol.upper(), "leverage": int(leverage)})

    # ==================================================================
    # Lifecycle
    # ==================================================================

    def close(self) -> None:
        if self._user_stream is not None:
            self._user_stream.stop()
            self._user_stream = None
        self._rest.close()


# ---------------------------------------------------------------------------
# Module-level helpers — used by data.sources.binance to delegate REST klines
# ---------------------------------------------------------------------------

def public_broker(market: str, testnet: bool = False) -> BinanceBroker:
    """A credential-free connector (market-data only)."""
    return BinanceBroker(market=market, testnet=testnet)


def fetch_candles(
    market: str, symbol: str, timeframe: str, start_ms: int, end_ms: int,
    testnet: bool = False,
) -> list[dict[str, Any]]:
    """Standalone candle fetch (the logic ``data`` now delegates to)."""
    return public_broker(market, testnet).fetch_candles(symbol, timeframe, start_ms, end_ms)


def get_server_time(market: str, testnet: bool = False) -> int:
    return public_broker(market, testnet).server_time()
