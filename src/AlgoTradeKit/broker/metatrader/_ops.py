"""
Shared MetaTrader 5 operation implementations.

One implementation of every MT5 operation (candles, ticks, account, orders,
positions), driven by BOTH transports:

- ``bridge_server.py`` — runs inside the Wine Python on a Linux VPS and exposes
  these ops over the JSON socket (``mode="bridge"``);
- ``_native.py`` ``NativeTransport`` — imports ``MetaTrader5`` in-process on
  Windows (``mode="native"``).

Method names and payload shapes ARE the bridge protocol, so the two transports
are behaviour-identical and ``MetaTraderBroker`` cannot tell them apart.

IMPORTANT — this module must stay **standard-library only** (the ``MetaTrader5``
module is handed in, never imported here), so the bridge needs no AlgoTradeKit
install in the Wine Python.  Deploying the bridge on a VPS means copying
``bridge_server.py`` **and** this file into the same folder.
"""
from __future__ import annotations

# Timeframe string → MetaTrader5 constant NAME (resolved via getattr at runtime).
_TF_NAME = {
    "1m": "TIMEFRAME_M1", "3m": "TIMEFRAME_M3", "5m": "TIMEFRAME_M5",
    "15m": "TIMEFRAME_M15", "30m": "TIMEFRAME_M30",
    "1h": "TIMEFRAME_H1", "2h": "TIMEFRAME_H2", "4h": "TIMEFRAME_H4",
    "6h": "TIMEFRAME_H6", "8h": "TIMEFRAME_H8", "12h": "TIMEFRAME_H12",
    "1d": "TIMEFRAME_D1", "1w": "TIMEFRAME_W1", "1M": "TIMEFRAME_MN1",
}

# Milliseconds per timeframe (for close_time; "1M" handled separately).
_TF_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000,
    "8h": 28_800_000, "12h": 43_200_000, "1d": 86_400_000, "1w": 604_800_000,
    "1M": 2_592_000_000,
}


class MT5Ops:
    """
    JSON-friendly MT5 operations over an already-imported ``MetaTrader5`` module.

    The caller owns the module lifecycle (import, ``initialize()``, login);
    this class only executes operations.  Anything not starting with ``_`` is
    a callable RPC method.
    """

    def __init__(self, mt5_module) -> None:
        self.mt5 = mt5_module

    # ---- helpers ----

    def _tf(self, tf_str):
        name = _TF_NAME.get(tf_str)
        if name is None:
            raise ValueError(f"Unsupported timeframe '{tf_str}'.")
        return getattr(self.mt5, name)

    def _ensure_symbol(self, symbol):
        # Crypto / many CFD symbols are hidden from Market Watch by default;
        # copy_rates returns None until the symbol is selected.
        if not self.mt5.symbol_select(symbol, True):
            raise RuntimeError(
                f"symbol_select({symbol}) failed — is '{symbol}' the EXACT symbol "
                f"name in MT5 Market Watch? {self.mt5.last_error()}"
            )

    def _candle(self, r, tf_str) -> dict:
        ts = int(r["time"]) * 1000
        vol = float(r["real_volume"]) if r["real_volume"] else float(r["tick_volume"])
        return {
            "timestamp": ts,
            "open": float(r["open"]), "high": float(r["high"]),
            "low": float(r["low"]), "close": float(r["close"]),
            "volume": vol,
            "close_time": ts + _TF_MS.get(tf_str, 0) - 1,
            "quote_volume": 0.0, "trades": int(r["tick_volume"]),
            "taker_buy_base": 0.0, "taker_buy_quote": 0.0,
        }

    # ---- RPC methods (anything not starting with "_" is callable) ----

    def ping(self) -> dict:
        v = self.mt5.version()
        return {"version": list(v) if v else None}

    def login(self, login, password, server) -> bool:
        return bool(self.mt5.login(int(login), password=password, server=server))

    def candles_range(self, symbol, timeframe, start_ms, end_ms) -> list:
        from datetime import datetime, timezone
        self._ensure_symbol(symbol)
        frm = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
        to = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc)
        rates = self.mt5.copy_rates_range(symbol, self._tf(timeframe), frm, to)
        if rates is None:
            raise RuntimeError(f"copy_rates_range failed: {self.mt5.last_error()}")
        return [self._candle(r, timeframe) for r in rates]

    def candles_from_count(self, symbol, timeframe, count) -> list:
        self._ensure_symbol(symbol)
        rates = self.mt5.copy_rates_from_pos(symbol, self._tf(timeframe), 0, int(count))
        if rates is None:
            raise RuntimeError(f"copy_rates_from_pos failed: {self.mt5.last_error()}")
        return [self._candle(r, timeframe) for r in rates]

    def tick(self, symbol) -> dict:
        self._ensure_symbol(symbol)
        t = self.mt5.symbol_info_tick(symbol)
        if t is None:
            raise RuntimeError(f"symbol_info_tick failed: {self.mt5.last_error()}")
        return {"bid": t.bid, "ask": t.ask, "last": t.last, "time_ms": int(t.time) * 1000}

    def symbol_info(self, symbol) -> dict:
        info = self.mt5.symbol_info(symbol)
        if info is None:
            raise RuntimeError(f"symbol_info failed: {self.mt5.last_error()}")
        return info._asdict()

    def symbols(self, group="*") -> list:
        syms = self.mt5.symbols_get(group) if group else self.mt5.symbols_get()
        return [s.name for s in (syms or ())]

    def account_info(self) -> dict:
        info = self.mt5.account_info()
        if info is None:
            raise RuntimeError(f"account_info failed: {self.mt5.last_error()}")
        return info._asdict()

    def positions(self, symbol=None) -> list:
        rows = self.mt5.positions_get(symbol=symbol) if symbol else self.mt5.positions_get()
        return [p._asdict() for p in (rows or ())]

    def history_deals(self, from_ms, to_ms, position=None) -> list:
        """Deal history: one position's deals (by ticket) or a UTC-ms time range."""
        from datetime import datetime, timezone
        if position is not None:
            rows = self.mt5.history_deals_get(position=int(position))
        else:
            frm = datetime.fromtimestamp(from_ms / 1000, tz=timezone.utc)
            to = datetime.fromtimestamp(to_ms / 1000, tz=timezone.utc)
            rows = self.mt5.history_deals_get(frm, to)
        if rows is None:
            raise RuntimeError(f"history_deals_get failed: {self.mt5.last_error()}")
        return [d._asdict() for d in rows]

    def pending_orders(self, symbol=None) -> list:
        rows = self.mt5.orders_get(symbol=symbol) if symbol else self.mt5.orders_get()
        return [o._asdict() for o in (rows or ())]

    def place_order(self, spec: dict) -> dict:
        mt5 = self.mt5
        symbol = spec["symbol"]
        side = spec["side"]                      # "buy" / "sell"
        kind = spec.get("order_kind", "market")  # market / limit / stop
        volume = float(spec["volume"])
        is_buy = side == "buy"

        if not mt5.symbol_select(symbol, True):
            raise RuntimeError(f"symbol_select({symbol}) failed: {mt5.last_error()}")
        tick = mt5.symbol_info_tick(symbol)

        if kind == "market":
            action = mt5.TRADE_ACTION_DEAL
            otype = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
            price = tick.ask if is_buy else tick.bid
        else:
            action = mt5.TRADE_ACTION_PENDING
            price = float(spec["price"])
            if kind == "limit":
                otype = mt5.ORDER_TYPE_BUY_LIMIT if is_buy else mt5.ORDER_TYPE_SELL_LIMIT
            else:  # stop
                otype = mt5.ORDER_TYPE_BUY_STOP if is_buy else mt5.ORDER_TYPE_SELL_STOP

        req = {
            "action": action, "symbol": symbol, "volume": volume,
            "type": otype, "price": float(price),
            "deviation": int(spec.get("deviation", 20)),
            "magic": int(spec.get("magic", 0)),
            "comment": spec.get("comment", "AlgoTradeKit"),
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": getattr(mt5, "ORDER_FILLING_" + spec.get("filling", "IOC").upper()),
        }
        if spec.get("sl") is not None:
            req["sl"] = float(spec["sl"])
        if spec.get("tp") is not None:
            req["tp"] = float(spec["tp"])
        result = mt5.order_send(req)
        return result._asdict()

    def cancel_order(self, ticket) -> dict:
        result = self.mt5.order_send({
            "action": self.mt5.TRADE_ACTION_REMOVE, "order": int(ticket),
        })
        return result._asdict()

    def close_position(self, ticket=None, symbol=None, volume=None) -> dict:
        mt5 = self.mt5
        if ticket:
            positions = mt5.positions_get(ticket=int(ticket))
        else:
            positions = mt5.positions_get(symbol=symbol)
        if not positions:
            raise RuntimeError("No matching position to close.")
        pos = positions[0]
        is_long = pos.type == mt5.POSITION_TYPE_BUY
        tick = mt5.symbol_info_tick(pos.symbol)
        req = {
            "action": mt5.TRADE_ACTION_DEAL, "position": pos.ticket, "symbol": pos.symbol,
            "volume": float(volume) if volume else pos.volume,
            "type": mt5.ORDER_TYPE_SELL if is_long else mt5.ORDER_TYPE_BUY,
            "price": tick.bid if is_long else tick.ask,
            "deviation": 20, "magic": 0, "comment": "AlgoTradeKit close",
            "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
        }
        return mt5.order_send(req)._asdict()

    def modify_position(self, ticket, sl=None, tp=None) -> dict:
        mt5 = self.mt5
        positions = mt5.positions_get(ticket=int(ticket))
        if not positions:
            raise RuntimeError("No matching position to modify.")
        pos = positions[0]
        req = {
            "action": mt5.TRADE_ACTION_SLTP, "position": pos.ticket, "symbol": pos.symbol,
            "sl": float(sl) if sl is not None else pos.sl,
            "tp": float(tp) if tp is not None else pos.tp,
        }
        return mt5.order_send(req)._asdict()

    def shutdown(self) -> bool:
        self.mt5.shutdown()
        return True
