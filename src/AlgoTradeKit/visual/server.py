"""
algotradekit.visual.server
~~~~~~~~~~~~~~~~~~~~~~~~~~
Lightweight FastAPI server that:
  - Serves the static index.html on GET /
  - Handles WebSocket connections on /ws
  - Lets Chart instances push messages to all connected clients via broadcast()
"""

import asyncio
import json
import logging
import socket
import threading
from pathlib import Path
from typing import Set

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

log = logging.getLogger("algotradekit.visual.server")

# Path is relative to THIS file — always resolves correctly regardless of cwd
STATIC_DIR = Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# Port helper
# ---------------------------------------------------------------------------

def _find_free_port(start: int = 8700) -> int:
    """Find a free TCP port starting from *start* (scans up to start+200)."""
    for port in range(start, start + 200):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("No free TCP port found in range 8700–8900")


# ---------------------------------------------------------------------------
# Connection manager
# ---------------------------------------------------------------------------

class ConnectionManager:
    """Tracks active WebSocket connections and broadcasts messages to all of them."""

    def __init__(self):
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
# Chart server
# ---------------------------------------------------------------------------

class ChartServer:
    """
    One ``ChartServer`` instance = one browser tab.

    The server runs in a daemon background thread so it never blocks the
    Python process.  Messages from Python → browser use ``send()``.
    Messages from browser → Python arrive via the ``on_message`` callback.

    Parameters
    ----------
    title : str
        Used in the browser URL query-string (cosmetic only).
    port : int
        TCP port to listen on.  0 = auto-select a free port.
    """

    def __init__(self, title: str = "Chart", port: int = 0) -> None:
        self.title   = title
        self.port    = port or _find_free_port()
        self._manager = ConnectionManager()
        self._loop:   asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._app    = self._build_app()

        # Set by Chart to receive browser → Python messages.
        # Signature: on_message(msg: dict) -> None
        self.on_message = None

    # ── FastAPI application ────────────────────────────────────────────────

    def _build_app(self) -> FastAPI:
        app = FastAPI(docs_url=None, redoc_url=None)

        @app.get("/", response_class=HTMLResponse)
        async def root():
            html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
            return HTMLResponse(content=html)

        @app.websocket("/ws")
        async def ws_endpoint(websocket: WebSocket):
            await self._manager.connect(websocket)
            try:
                while True:
                    raw = await websocket.receive_text()
                    if self.on_message:
                        try:
                            self.on_message(json.loads(raw))
                        except Exception:
                            pass
            except WebSocketDisconnect:
                self._manager.disconnect(websocket)

        return app

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def start(self, open_browser: bool = True) -> None:
        """Start the server in a daemon thread and optionally open a browser tab."""
        ready = threading.Event()

        def _run():
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

            # Patch startup so we can signal readiness
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
            webbrowser.open(f"http://127.0.0.1:{self.port}/?title={self.title}")

        log.info("ChartServer started → http://127.0.0.1:%d", self.port)

    def stop(self) -> None:
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

    # ── Messaging ──────────────────────────────────────────────────────────

    def send(self, message: dict) -> None:
        """Thread-safe: broadcast *message* to all connected browser clients."""
        if self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(
            self._manager.broadcast(message), self._loop
        )
