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
(On Windows itself no bridge is needed — the library talks to MT5 in-process,
see ``_native.py``.)

This file and ``_ops.py`` (the shared operation implementations) are
standard-library-only **except** for ``MetaTrader5`` (imported lazily), so the
AlgoTradeKit package itself never depends on it — no dependency conflict in the
library's own environment.

Setup (once, on the VPS)
------------------------
    # 1. Install Wine + a virtual display
    sudo apt install wine xvfb
    # 2. Install the MT5 terminal under Wine (headless), then Windows Python in
    #    the same WINEPREFIX, then the package:
    wine python -m pip install MetaTrader5
    # 3. Copy this file AND _ops.py into the same folder, then run it
    #    (keeps running; use tmux/systemd):
    xvfb-run wine python bridge_server.py \
        --host 127.0.0.1 --port 18812 \
        --login 12345678 --password "***" --server "MyBroker-Demo"

Then, on the Linux side:
    from AlgoTradeKit.broker import Broker
    mt = Broker("metatrader", host="127.0.0.1", port=18812)
    candles = mt.fetch_last_candles("EURUSD", "15m", 1000)

Bind address (``--host`` / ``--port``)
--------------------------------------
The default is ``127.0.0.1:18812`` — loopback only, reachable from the VPS
itself and through an SSH tunnel (``ssh -N -L 18812:127.0.0.1:18812 user@vps``).
Keep that default in tmux/systemd units.  ``--host 0.0.0.0`` exposes the bridge
to the whole network: it speaks an **unauthenticated** protocol that can place
orders on the account, so only do it behind a firewall you trust — an SSH
tunnel is the safe way to reach it from another machine.

Do not pass ``--path``
----------------------
``mt5.initialize()`` auto-detects the terminal.  An explicit ``--path`` is a
common cause of the ``IPC timeout`` (-10005) failure under Wine, so it is kept
only as a last-resort escape hatch and is deliberately absent from every setup
example (see MT5_WINE_SETUP.md Part G).  If initialization does fail, run
``wineserver -k`` before retrying — this script prints the same guidance.

Diagnostics under Wine + Xvfb
-----------------------------
The Windows Python running under Wine does not always deliver ``print()`` to
the Linux terminal (and ``xvfb-run`` can swallow it entirely).  When you see no
output at all, write diagnostics to a file on the Wine ``C:`` drive and read it
from Linux (``~/.mt5/drive_c/bridge.log``)::

    xvfb-run wine python bridge_server.py --host 127.0.0.1 --port 18812 \
        > "C:/bridge.log" 2>&1
    # or, from inside a patched copy of this file:
    #   open("C:/bridge.log", "a").write(f"{message}\\n")
"""
from __future__ import annotations

import argparse
import json
import socket
import threading

try:  # normal package import (bridge_server shipped inside AlgoTradeKit)
    from ._ops import MT5Ops
except ImportError:  # standalone script on the VPS — _ops.py sits next to this file
    from _ops import MT5Ops

#: ``mt5.last_error()`` code for "IPC timeout" — the terminal never answered.
IPC_TIMEOUT_CODE = -10005

#: Hosts that keep the bridge unreachable from outside the machine.
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def _error_code(error) -> int | None:
    """First element of an ``mt5.last_error()`` tuple, when it is an int."""
    if isinstance(error, (tuple, list)) and error:
        code = error[0]
        if isinstance(code, int):
            return code
    return None


def initialize_failure_message(error, path=None) -> str:
    """
    Human-fixable message for a failed ``mt5.initialize()``.

    An ``IPC timeout`` (-10005) gets the two fixes that actually work under
    Wine: kill stale Wine processes with ``wineserver -k``
    before retrying, and drop ``--path`` — an explicit terminal path is a
    common cause of this exact timeout.
    """
    lines = [f"mt5.initialize failed: {error}"]
    if _error_code(error) == IPC_TIMEOUT_CODE:
        lines += [
            "",
            "  IPC timeout — the MT5 terminal did not answer. Fixes, in order:",
            "    1. Kill stale Wine processes, then retry:  wineserver -k",
        ]
        if path:
            lines.append(
                f"    2. Retry WITHOUT --path (you passed --path {path!r}) — an explicit "
                "terminal path is a common cause of this timeout; let "
                "mt5.initialize() auto-detect."
            )
        else:
            lines.append(
                "    2. Do NOT add --path — an explicit terminal path is a common cause "
                "of this timeout; auto-detection is more reliable under Wine."
            )
        lines += [
            "    3. Check the terminal can start headless: Xvfb running and DISPLAY set "
            "(xvfb-run ...).",
            "  See MT5_WINE_SETUP.md — Part G and Troubleshooting.",
        ]
    return "\n".join(lines)


def bind_warning(host: str) -> str | None:
    """Security note for a non-loopback ``--host``; ``None`` for loopback."""
    if host in _LOOPBACK_HOSTS:
        return None
    return (
        f"[mt5-bridge] WARNING: bound to {host} — the bridge protocol is "
        "unauthenticated and can place orders on this account. Expose it only "
        "behind a trusted firewall; an SSH tunnel to 127.0.0.1 is the safe way "
        "to reach it from another machine."
    )


class MT5Dispatcher(MT5Ops):
    """
    Owns the ``MetaTrader5`` module lifecycle for the bridge (import,
    ``initialize()``, optional login) and inherits every RPC operation from
    the shared :class:`MT5Ops`.
    """

    def __init__(self, login=None, password=None, server=None, path=None) -> None:
        import MetaTrader5 as mt5  # lazy: only importable inside the Wine Python

        init_kwargs = {}
        if path:
            init_kwargs["path"] = path
        if not mt5.initialize(**init_kwargs):
            message = initialize_failure_message(mt5.last_error(), path)
            # Printed as well as raised: under Wine + Xvfb a traceback is easy
            # to lose, and this is the message the operator has to act on.
            print(message, flush=True)
            raise RuntimeError(message)

        if login and password and server:
            if not mt5.login(int(login), password=password, server=server):
                raise RuntimeError(f"mt5.login failed: {mt5.last_error()}")

        super().__init__(mt5)


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


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="AlgoTradeKit MetaTrader 5 bridge server")
    ap.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind address (default: 127.0.0.1 — loopback only; reach it from "
             "another machine over an SSH tunnel). '0.0.0.0' exposes an "
             "unauthenticated, order-capable socket to the network.",
    )
    ap.add_argument("--port", type=int, default=18812, help="Bind port (default: 18812)")
    ap.add_argument("--login", default=None)
    ap.add_argument("--password", default=None)
    ap.add_argument("--server", default=None)
    ap.add_argument(
        "--path",
        default=None,
        help="Path to terminal64.exe — NOT recommended: auto-detection is more "
             "reliable under Wine and an explicit path often causes the IPC "
             "timeout (-10005). Last-resort escape hatch only.",
    )
    return ap


def main() -> None:
    args = build_parser().parse_args()

    warning = bind_warning(args.host)
    if warning:
        print(warning, flush=True)

    dispatcher = MT5Dispatcher(
        login=args.login, password=args.password, server=args.server, path=args.path
    )
    serve(args.host, args.port, dispatcher)


if __name__ == "__main__":
    main()
