"""
``MetaTraderBroker`` — unified connector for a MetaTrader 5 account.

Reaches MT5 through one of two interchangeable transports (picked by ``mode``):
the bridge server (:mod:`bridge_server` running in a Wine Python — Linux/remote)
or the in-process ``MetaTrader5`` package (:mod:`_native` — Windows).  Everything
is mapped onto the shared ``broker`` types, so a MetaTrader forex account behaves
exactly like a Binance connector to the rest of the library.
"""
from __future__ import annotations

import platform
from collections.abc import Callable
from typing import Any

from .._errors import BrokerError, ConnectionFailed, NotSupportedError, OrderError
from .._stream import Stream, start_poll_stream
from .._timeutil import normalize_timeframe, now_ms
from .._types import (
    COMMISSION_TYPE_PER_LOT,
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
    TradingCosts,
)
from ..base import BaseBroker
from ._bridge_client import BridgeClient
from ._native import NativeTransport

_TRADE_RETCODE_DONE = 10009  # MetaTrader5.TRADE_RETCODE_DONE

_MODES = ("auto", "native", "bridge")

# Candles fetched per stream poll: the forming bar + enough closed bars to
# self-heal short stalls between polls (real gap-fill is the trader's job).
_CANDLE_TAIL_COUNT = 10

# MT5 pending-order type ints → (unified side, unified order type)
_MT_ORDER_TYPE = {
    2: (SIDE_BUY, ORDER_LIMIT), 3: (SIDE_SELL, ORDER_LIMIT),
    4: (SIDE_BUY, ORDER_STOP),  5: (SIDE_SELL, ORDER_STOP),
}

_ORDER_KIND = {ORDER_MARKET: "market", ORDER_LIMIT: "limit", ORDER_STOP: "stop"}


class MetaTraderBroker(BaseBroker):
    """
    MetaTrader 5 connector (Wine bridge or native ``MetaTrader5``, by ``mode``).

    Parameters
    ----------
    server / login / password:
        Broker credentials.  If given, the connector logs the terminal in;
        otherwise it uses whatever account the terminal / bridge already has.
    mode:
        ``"auto"`` (default) — a non-default ``host`` (anything other than
        ``127.0.0.1``) means you are pointing at a remote bridge → **bridge**;
        else on Windows → **native** (in-process ``MetaTrader5``,
        ``pip install AlgoTradeKit[mt5]``); else (Linux/macOS) → **bridge** on
        ``host:port``.  ``"native"`` / ``"bridge"`` force a transport.  The
        resolved choice is exposed as ``self.mode`` (``"custom"`` when a
        ``transport`` is injected).
    host / port:
        Where the bridge server is listening (default ``127.0.0.1:18812``).
        Ignored in native mode.
    candle_poll_interval / tick_poll_interval:
        Seconds between feed polls for :meth:`stream_candles` (default 1.0) /
        :meth:`stream_ticker` (default 0.2).  MT5 has no push feed, so
        streaming polls the transport at these intervals.
    transport:
        Injectable RPC transport (used by tests); overrides ``mode``.
    connect:
        Probe the account in ``__init__`` (raises early with a guided setup
        message if MT5 is unreachable).  Set ``False`` to defer.
    """

    def __init__(
        self,
        server: str | None = None,
        login: int | None = None,
        password: str | None = None,
        *,
        mode: str = "auto",
        host: str = "127.0.0.1",
        port: int = 18812,
        timeout: float = 30.0,
        candle_poll_interval: float = 1.0,
        tick_poll_interval: float = 0.2,
        transport: Any = None,
        connect: bool = True,
    ) -> None:
        self.market = MARKET_FOREX
        self.name = "metatrader"
        self.supports_positions = True

        if candle_poll_interval <= 0 or tick_poll_interval <= 0:
            raise ValueError("candle_poll_interval / tick_poll_interval must be > 0 seconds.")
        self.candle_poll_interval = float(candle_poll_interval)
        self.tick_poll_interval = float(tick_poll_interval)

        resolved = self._resolve_mode(mode, host)
        if transport is not None:
            self.mode = "custom"
            self._transport = transport
        else:
            self.mode = resolved
            self._transport = (
                NativeTransport() if resolved == "native" else BridgeClient(host, port, timeout)
            )
        self._server = server
        self._login = login
        self._account_currency = ""
        self._authenticated = False

        if connect:
            self._connect(login, password, server)

    @staticmethod
    def _resolve_mode(mode: str, host: str) -> str:
        """``"auto"`` → bridge for a non-default host, else native on Windows only."""
        if mode not in _MODES:
            raise ValueError(
                f"Unknown MetaTrader mode '{mode}'. Use 'auto', 'native' or 'bridge'."
            )
        if mode != "auto":
            return mode
        if host != "127.0.0.1":
            return "bridge"  # user is pointing at a (remote) VPS bridge — works from any OS
        if platform.system() == "Windows":
            return "native"
        return "bridge"

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

    @staticmethod
    def _ticker_from_payload(symbol: str, t: dict) -> Ticker:
        bid, ask = float(t.get("bid", 0.0)), float(t.get("ask", 0.0))
        last = float(t.get("last") or 0.0) or ((bid + ask) / 2 if bid and ask else bid or ask)
        return Ticker(symbol=symbol, last=last, bid=bid, ask=ask,
                      timestamp=int(t.get("time_ms", now_ms())), raw=t)

    def get_ticker(self, symbol: str) -> Ticker:
        return self._ticker_from_payload(symbol, self._call("tick", symbol))

    def server_time(self) -> int:
        # MT5 exposes no clean broker-server clock; the terminal host clock is the
        # closest proxy and the feed timestamps are authoritative anyway.
        return now_ms()

    def get_trading_costs(self, symbol: str) -> TradingCosts:
        """
        Spread + contract size for *symbol* from MT5 ``symbol_info``.

        Spread is ``spread × point`` in price units (falling back to the
        payload's ``ask − bid`` when the venue reports no spread field).  MT5
        does not expose commission through its API, so it is reported as 0 —
        the ``TraderConfig`` cost override is the place to supply a real
        per-lot commission.  An unknown symbol raises (transport error).
        """
        info = self._call("symbol_info", symbol)
        point = float(info.get("point", 0.0) or 0.0)
        spread = float(info.get("spread", 0.0) or 0.0) * point
        if not spread:
            bid = float(info.get("bid", 0.0) or 0.0)
            ask = float(info.get("ask", 0.0) or 0.0)
            spread = max(ask - bid, 0.0) if (bid and ask) else 0.0
        contract_size = float(info.get("trade_contract_size", 0.0) or 0.0) or None
        return TradingCosts(
            commission_type=COMMISSION_TYPE_PER_LOT,
            commission=0.0,
            spread=spread,
            contract_size=contract_size,
            raw=info,
        )

    # ---- streaming (v1.0.0 — polling; MT5 has no push feed) ----

    def stream_candles(
        self,
        symbol: str,
        timeframe: str,
        on_candle: Callable[[dict[str, Any]], None],
        *,
        closed_only: bool = True,
    ) -> Stream:
        """
        Real-time candles by polling the transport every ``candle_poll_interval``
        seconds (identical over bridge and native).

        *on_candle* receives library-standard candle dicts plus a ``"closed"``
        flag — the same shape the Binance WebSocket stream emits.  A candle
        counts as closed once a newer bar appears in the polled tail; closed
        candles fire exactly once, in order (dedup by ``timestamp``).  With
        ``closed_only=False`` the forming candle is also emitted whenever it
        changes (and immediately on subscribe).
        """
        tf = normalize_timeframe(timeframe)
        poll = self._make_candle_poller(symbol, tf, on_candle, closed_only)
        return start_poll_stream(
            poll,
            interval=self.candle_poll_interval,
            name=f"{symbol.lower()}-candles-{tf}",
        )

    def stream_ticker(self, symbol: str, on_tick: Callable[[Ticker], None]) -> Stream:
        """
        Real-time ticker by polling the transport every ``tick_poll_interval``
        seconds.  *on_tick* receives a :class:`Ticker` whenever the venue tick
        changed since the previous poll (the first poll always fires).
        """
        poll = self._make_ticker_poller(symbol, on_tick)
        return start_poll_stream(
            poll,
            interval=self.tick_poll_interval,
            name=f"{symbol.lower()}-ticker",
        )

    def _make_candle_poller(
        self,
        symbol: str,
        tf: str,
        on_candle: Callable[[dict[str, Any]], None],
        closed_only: bool,
    ) -> Callable[[], None]:
        """One candle-poll step as a closure (drives ``stream_candles``)."""
        state: dict[str, Any] = {"last_closed_ts": None, "forming": None}

        def _poll() -> None:
            rows = self._call("candles_from_count", symbol, tf, _CANDLE_TAIL_COUNT)
            if not rows:
                return
            newest_ts = int(rows[-1]["timestamp"])
            if state["last_closed_ts"] is None:
                # Baseline: bars already closed at subscribe time never fire.
                state["last_closed_ts"] = newest_ts - 1
            for row in rows:
                ts = int(row["timestamp"])
                if state["last_closed_ts"] < ts < newest_ts:
                    state["last_closed_ts"] = ts
                    on_candle({**row, "closed": True})
            if not closed_only and rows[-1] != state["forming"]:
                state["forming"] = rows[-1]
                on_candle({**rows[-1], "closed": False})

        return _poll

    def _make_ticker_poller(
        self, symbol: str, on_tick: Callable[[Ticker], None]
    ) -> Callable[[], None]:
        """One ticker-poll step as a closure (drives ``stream_ticker``)."""
        state: dict[str, Any] = {"last": None}

        def _poll() -> None:
            t = self._call("tick", symbol)
            if t == state["last"]:
                return
            state["last"] = t
            on_tick(self._ticker_from_payload(symbol, t))

        return _poll

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
        price: float | None = None,
        stop_price: float | None = None,
        time_in_force: str = "gtc",
        reduce_only: bool = False,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        client_order_id: str | None = None,
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

    def open_orders(self, symbol: str | None = None) -> list[Order]:
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

    def open_positions(self, symbol: str | None = None) -> list[Position]:
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

    def history_deals(
        self,
        from_ms: int | None = None,
        to_ms: int | None = None,
        *,
        position: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Account deal history as raw MT5 deal dicts (``time`` in seconds,
        ``time_msc`` in ms; ``entry`` 0=in / 1=out; ``reason`` = MT5
        ``DEAL_REASON_*`` int — 4=SL, 5=TP).

        Pass a UTC-ms range (*from_ms*, *to_ms*) or ``position=<ticket>`` for
        every deal of one position.  The live trader uses the ticket form to
        read the real exit fill of a position the venue closed — SL/TP hit or
        a manual close (v1.0.0 close detection).
        """
        self._require_auth()
        if position is not None:
            return self._call("history_deals", 0, 0, int(position))
        if from_ms is None or to_ms is None:
            raise ValueError(
                "history_deals needs from_ms and to_ms, or position=<ticket>."
            )
        return self._call("history_deals", int(from_ms), int(to_ms), None)

    def close_position(
        self,
        symbol: str,
        quantity: float | None = None,
        *,
        ticket: int | None = None,
    ) -> OrderResult:
        """
        Close (or partially close) a position — the symbol's first position by
        default, or one specific position by *ticket* (hedging accounts / the
        live trader's per-trade management, v1.0.0).
        """
        self._require_auth()
        resp = self._call(
            "close_position", int(ticket) if ticket is not None else None, symbol, quantity
        )
        return self._order_result(resp, symbol, "")

    def modify_position(
        self, ticket: int, stop_loss: float | None = None, take_profit: float | None = None
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
