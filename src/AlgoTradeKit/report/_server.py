"""
AlgoTradeKit.report._server
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Lightweight FastAPI server that serves the interactive simulation report
page and communicates with it over WebSocket.

Architecture
------------
One ``ReportServer`` instance = one browser report tab.

The server:
  1. Serves ``static/report.html`` on ``GET /``
  2. Exposes a WebSocket on ``/ws`` for bidirectional communication
  3. Sends the full serialised ``SimulateReport`` as JSON on first connect
  4. Receives ``{type: "open_chart", trade_id: N}`` messages from the
     browser and calls the registered ``on_open_chart`` callback

The server runs in a daemon background thread (same pattern as
``visual.server.ChartServer``) so it never blocks the Python process.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import threading
from pathlib import Path
from typing import Any, Callable, Optional, Set

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

log = logging.getLogger("algotradekit.report.server")

STATIC_DIR = Path(__file__).parent / "static"

_reserved_ports: set[int] = set()


# ---------------------------------------------------------------------------
# Port helper
# ---------------------------------------------------------------------------

def _find_free_port(start: int = 8800) -> int:
    """Find a free TCP port starting from *start* (scans up to start+200)."""
    for port in range(start, start + 200):
        if port in _reserved_ports:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                _reserved_ports.add(port)
                return port
            except OSError:
                continue
    raise RuntimeError("No free TCP port found in range 8800–9000")


# ---------------------------------------------------------------------------
# Connection manager
# ---------------------------------------------------------------------------

class _ConnectionManager:
    def __init__(self) -> None:
        self.active: Set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self.active.discard(ws)

    async def broadcast(self, message: dict) -> None:
        dead: Set[WebSocket] = set()
        for ws in list(self.active):
            try:
                await ws.send_text(json.dumps(message))
            except Exception:
                dead.add(ws)
        self.active -= dead


# ---------------------------------------------------------------------------
# ReportServer
# ---------------------------------------------------------------------------

class ReportServer:
    """
    One ``ReportServer`` instance = one browser report tab.

    Parameters
    ----------
    title : str
        Browser tab title.
    port : int
        TCP port.  0 = auto-select.
    on_open_chart : callable | None
        Called when the browser requests to open the candle chart for a
        specific trade.  Signature: ``on_open_chart(trade_id: int) -> None``.
    """

    def __init__(
        self,
        title: str = "AlgoTradeKit Report",
        port: int = 0,
        on_open_chart: Optional[Callable[[int], None]] = None,
    ) -> None:
        self.title         = title
        self.port          = port or _find_free_port()
        self.on_open_chart = on_open_chart
        self._manager      = _ConnectionManager()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._pending_data: Optional[dict] = None   # sent to first WS connect
        self._app          = self._build_app()

    # ── FastAPI app ────────────────────────────────────────────────────────

    def _build_app(self) -> FastAPI:
        app = FastAPI(docs_url=None, redoc_url=None)

        @app.get("/", response_class=HTMLResponse)
        async def root():
            html = (STATIC_DIR / "report.html").read_text(encoding="utf-8")
            return HTMLResponse(content=html)

        @app.websocket("/ws")
        async def ws_endpoint(websocket: WebSocket):
            await self._manager.connect(websocket)
            # Send pending data to this new client
            if self._pending_data is not None:
                try:
                    await websocket.send_text(json.dumps(self._pending_data))
                except Exception:
                    pass
            try:
                while True:
                    raw = await websocket.receive_text()
                    try:
                        msg = json.loads(raw)
                        self._handle_browser_msg(msg)
                    except Exception:
                        pass
            except WebSocketDisconnect:
                self._manager.disconnect(websocket)

        return app

    # ── Message handling ───────────────────────────────────────────────────

    def _handle_browser_msg(self, msg: dict) -> None:
        if msg.get("type") == "open_chart" and self.on_open_chart:
            try:
                self.on_open_chart(int(msg.get("trade_id", -1)))
            except Exception as e:
                log.warning("on_open_chart error: %s", e)

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def start(self, open_browser: bool = True) -> None:
        """Start the server in a daemon thread and optionally open a browser tab."""
        ready = threading.Event()

        def _run() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            config = uvicorn.Config(
                self._app,
                host="127.0.0.1",
                port=self.port,
                loop="asyncio",
                log_level="warning",
            )
            server = uvicorn.Server(config)
            _orig = server.startup

            async def _patched_startup(sockets=None):
                await _orig(sockets)
                ready.set()

            server.startup = _patched_startup
            self._loop.run_until_complete(server.serve())

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        ready.wait(timeout=5)

        if open_browser:
            import webbrowser
            webbrowser.open(f"http://127.0.0.1:{self.port}/")

        log.info("ReportServer started → http://127.0.0.1:%d", self.port)

    def stop(self) -> None:
        _reserved_ports.discard(self.port)
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

    # ── Data push ─────────────────────────────────────────────────────────

    def send(self, message: dict) -> None:
        """Thread-safe broadcast to all connected browser clients."""
        if self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(
            self._manager.broadcast(message), self._loop
        )

    def set_report_data(self, data: dict) -> None:
        """
        Store the report payload so new WebSocket connections receive it
        immediately, and broadcast to any already-connected clients.
        """
        self._pending_data = {"type": "report_data", **data}
        self.send(self._pending_data)
