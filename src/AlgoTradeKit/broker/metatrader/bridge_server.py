#!/usr/bin/env python
"""
MetaTrader 5 bridge server — run this INSIDE the Wine Python on the VPS.

Why this exists
---------------
The ``MetaTrader5`` package only talks to a running MT5 *terminal*, and that
package/terminal only run on Windows.  On a headless Linux VPS you install MT5 +
Windows Python under **Wine** and run this script under a virtual framebuffer
(``xvfb-run``) — no GUI, SSH-only.  It exposes MT5 to the normal Linux side of
AlgoTradeKit over a tiny newline-delimited JSON socket (see ``_bridge_client``).

This file is standard-library-only **except** for ``MetaTrader5`` (imported
lazily), so the AlgoTradeKit package itself never depends on it — no dependency
conflict in the library's own environment.

Setup (once, on the VPS)
------------------------
    # 1. Install Wine + a virtual display
    sudo apt install wine xvfb
    # 2. Install the MT5 terminal under Wine (headless), then Windows Python in
    #    the same WINEPREFIX, then the package:
    wine python -m pip install MetaTrader5
    # 3. Copy this file over and run it (keeps running; use tmux/systemd):
    xvfb-run wine python bridge_server.py \
        --host 127.0.0.1 --port 18812 \
        --login 12345678 --password "***" --server "MyBroker-Demo"

Then, on the Linux side:
    from AlgoTradeKit.broker import Broker
    mt = Broker("metatrader", host="127.0.0.1", port=18812)
    candles = mt.fetch_last_candles("EURUSD", "15m", 1000)
"""
from __future__ import annotations

import argparse
import json
import socket
import threading

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


class MT5Dispatcher:
    """Wraps the ``MetaTrader5`` package and exposes JSON-friendly methods."""

    def __init__(self, login=None, password=None, server=None, path=None) -> None:
        import MetaTrader5 as mt5  # lazy: only importable inside the Wine Python
        self.mt5 = mt5

        init_kwargs = {}
        if path:
            init_kwargs["path"] = path
        if not mt5.initialize(**init_kwargs):
            raise RuntimeError(f"mt5.initialize failed: {mt5.last_error()}")

        if login and password and server:
            if not mt5.login(int(login), password=password, server=server):
                raise RuntimeError(f"mt5.login failed: {mt5.last_error()}")

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
        positions = mt5.positions_get(ticket=int(ticket)) if ticket else mt5.positions_get(symbol=symbol)
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


# ---------------------------------------------------------------------------
# TCP server
# ---------------------------------------------------------------------------

def _handle_client(conn: socket.socket, dispatcher: MT5Dispatcher, lock: threading.Lock) -> None:
    buf = b""
    with conn:
        while True:
            try:
                chunk = conn.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                resp = _dispatch(line, dispatcher, lock)
                try:
                    conn.sendall(json.dumps(resp).encode("utf-8") + b"\n")
                except OSError:
                    return


def _dispatch(line: bytes, dispatcher: MT5Dispatcher, lock: threading.Lock) -> dict:
    try:
        req = json.loads(line)
        method = req["method"]
        if method.startswith("_"):
            raise ValueError("private methods are not callable")
        func = getattr(dispatcher, method)
        with lock:  # MetaTrader5 is not thread-safe
            result = func(*req.get("args", []), **req.get("kwargs", {}))
        return {"id": req.get("id"), "ok": True, "result": result}
    except Exception as exc:  # noqa: BLE001 — report every error back to the client
        rid = None
        try:
            rid = json.loads(line).get("id")
        except Exception:  # noqa: BLE001
            pass
        return {"id": rid, "ok": False, "error": f"{type(exc).__name__}: {exc}"}


def serve(host: str, port: int, dispatcher: MT5Dispatcher) -> None:
    lock = threading.Lock()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(8)
    print(f"[mt5-bridge] listening on {host}:{port}", flush=True)
    try:
        while True:
            conn, addr = srv.accept()
            print(f"[mt5-bridge] client connected: {addr}", flush=True)
            threading.Thread(
                target=_handle_client, args=(conn, dispatcher, lock), daemon=True
            ).start()
    except KeyboardInterrupt:
        print("[mt5-bridge] shutting down", flush=True)
    finally:
        srv.close()
        dispatcher.shutdown()


def main() -> None:
    ap = argparse.ArgumentParser(description="AlgoTradeKit MetaTrader 5 bridge server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=18812)
    ap.add_argument("--login", default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("--server", default=None)
    ap.add_argument("--path", default=None, help="Path to terminal64.exe (optional)")
    args = ap.parse_args()

    dispatcher = MT5Dispatcher(
        login=args.login, password=args.password, server=args.server, path=args.path
    )
    serve(args.host, args.port, dispatcher)


if __name__ == "__main__":
    main()
