"""
TCP JSON-RPC client that talks to the MetaTrader bridge server.

The bridge server (:mod:`bridge_server`) runs inside the Wine Python where the
``MetaTrader5`` package is importable; this client runs in the normal Linux
Python of the library.  Protocol: newline-delimited JSON, one request →
one response, over a persistent socket.  Standard library only.
"""
from __future__ import annotations

import json
import socket
import threading
from typing import Any

from .._errors import BrokerError, ConnectionFailed

_SETUP_HINT = (
    "Could not reach the MetaTrader bridge. On the VPS, start it inside the Wine "
    "Python (headless, no GUI) with:\n"
    "    xvfb-run wine python bridge_server.py --host 127.0.0.1 --port 18812 \\\n"
    "        --login <LOGIN> --password <PASSWORD> --server <BROKER-SERVER>\n"
    "See AlgoTradeKit/broker/metatrader/bridge_server.py for the full setup."
)


class BridgeClient:
    """Minimal, thread-safe JSON-RPC client for the MetaTrader bridge."""

    def __init__(self, host: str = "127.0.0.1", port: int = 18812, timeout: float = 30.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._buf = b""
        self._id = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def _connect(self) -> socket.socket:
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as exc:
            raise ConnectionFailed(f"{_SETUP_HINT}\n\n(underlying error: {exc})") from exc
        sock.settimeout(self.timeout)
        self._sock = sock
        self._buf = b""
        return sock

    def _ensure(self) -> socket.socket:
        return self._sock or self._connect()

    # ------------------------------------------------------------------
    # RPC
    # ------------------------------------------------------------------

    def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Invoke *method* on the bridge and return its result."""
        with self._lock:
            self._id += 1
            payload = json.dumps(
                {"id": self._id, "method": method, "args": list(args), "kwargs": kwargs}
            ).encode("utf-8") + b"\n"

            try:
                sock = self._ensure()
                sock.sendall(payload)
                line = self._read_line(sock)
            except (OSError, ConnectionFailed):
                # One transparent reconnect + retry
                self.close()
                sock = self._connect()
                sock.sendall(payload)
                line = self._read_line(sock)

        resp = json.loads(line)
        if not resp.get("ok", False):
            raise BrokerError(f"MetaTrader bridge error in {method}: {resp.get('error')}")
        return resp.get("result")

    def _read_line(self, sock: socket.socket) -> bytes:
        while b"\n" not in self._buf:
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionFailed("MetaTrader bridge closed the connection.")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return line

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        self._buf = b""
