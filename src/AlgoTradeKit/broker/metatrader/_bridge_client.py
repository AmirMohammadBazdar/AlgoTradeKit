"""
TCP JSON-RPC client that talks to the MetaTrader bridge server.

The bridge server (:mod:`bridge_server`) runs inside the Wine Python where the
``MetaTrader5`` package is importable; this client runs in the normal Linux
Python of the library.  Protocol: newline-delimited JSON, one request →
one response, over a persistent socket.  Standard library only.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import threading
from pathlib import Path
from typing import Any

from .._errors import BrokerError, ConnectionFailed

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _diagnose_unreachable(host: str, port: int, exc: OSError) -> str:
    """
    Explain WHY the bridge is unreachable and name the exact MT5_WINE_SETUP.md
    section that fixes it (decision: diagnose + guide + stop — no silent
    fallback, no auto-start).
    """
    if host not in _LOCAL_HOSTS:
        # Remote bridge — local wine/prefix checks are meaningless here.
        return (
            f"Could not reach the MetaTrader bridge at {host}:{port}. Check that "
            "bridge_server.py is running on that machine — see MT5_WINE_SETUP.md Part G "
            "(run bridge_server.py inside tmux) — and that the port is reachable from "
            f"here (open or SSH-tunnelled). (underlying error: {exc})"
        )
    if shutil.which("wine") is None:
        return (
            "Wine is not installed — see MT5_WINE_SETUP.md Part A. (The MetaTrader "
            f"bridge runs inside Wine, so nothing can be listening on {host}:{port}. "
            f"underlying error: {exc})"
        )
    prefix = Path(os.environ.get("WINEPREFIX", "") or (Path.home() / ".mt5"))
    if not prefix.exists():
        return (
            f"MT5 Wine prefix not found ({prefix}) — see MT5_WINE_SETUP.md Part B "
            "(create the prefix), then Parts C-D (install the MT5 terminal and the "
            f"Windows Python inside it). (underlying error: {exc})"
        )
    return (
        "Bridge is not running — see MT5_WINE_SETUP.md Part G (run bridge_server.py "
        f"inside tmux). Wine and the prefix look fine, but nothing answered on "
        f"{host}:{port}. (underlying error: {exc})"
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
            raise ConnectionFailed(_diagnose_unreachable(self.host, self.port, exc)) from exc
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
