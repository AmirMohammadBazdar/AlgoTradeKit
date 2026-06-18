"""
algotradekit.visual.server
~~~~~~~~~~~~~~~~~~~~~~~~~~
Lightweight FastAPI server that:
  - Serves the static index.html on GET /
  - Handles WebSocket connections on /ws
  - Lets Chart instances push messages to all connected clients via broadcast()
  - Replays the last ``init`` (and any pending ``navigate_to_candle``) to
    newly-connecting browsers so a page-refresh or a second tab always gets
    the current chart state.
"""

import asyncio
import json
import logging
import socket
import threading
from pathlib import Path
from typing import Optional, Set

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

log = logging.getLogger("algotradekit.visual.server")

# Path is relative to THIS file — always resolves correctly regardless of cwd
STATIC_DIR = Path(__file__).parent / "static"

# Ports assigned to a ChartServer that hasn't started its thread yet.
# Prevents two Chart() calls from getting the same port number.
_reserved_ports: set[int] = set()


# ---------------------------------------------------------------------------
# Port helper
# ---------------------------------------------------------------------------

def _find_free_port(start: int = 8700) -> int:
    """Find a free TCP port starting from *start* (scans up to start+200)."""
    for port in range(start, start + 200):
        if port in _reserved_ports:       # already claimed by another Chart()
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                _reserved_ports.add(port) # claim it immediately
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

    v0.7.1 changes
    --------------
    * ``_last_init`` — caches the most recent ``init`` message so new
      browser connections (page refresh, second tab) immediately receive the
      current chart state without waiting for the Python side to re-push.
    * ``_last_navigate`` — caches the most recent ``navigate_to_candle``
      message so a newly-opened browser tab (opened by the report's
      "Open on Candle Chart" button) scrolls to the correct position
      even though the navigate message was sent before the tab connected.

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
        self._loop:   Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._app    = self._build_app()

        # Set by Chart to receive browser → Python messages.
        # Signature: on_message(msg: dict) -> None
        self.on_message = None

        # v0.7.1: replay cache — new browsers get the current state immediately
        self._last_init:     Optional[dict] = None
        self._last_navigate: Optional[dict] = None

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
            # Replay cached init so a refreshed/new tab sees the current chart
            if self._last_init is not None:
                try:
                    await websocket.send_text(json.dumps(self._last_init))
                except Exception:
                    pass
            # Replay pending navigate so a new tab scrolls to the right candle
            if self._last_navigate is not None:
                try:
                    await websocket.send_text(json.dumps(self._last_navigate))
                except Exception:
                    pass
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
        _reserved_ports.discard(self.port)
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

    # ── Messaging ──────────────────────────────────────────────────────────

    def send(self, message: dict) -> None:
        """Thread-safe: broadcast *message* to all connected browser clients.

        As a side-effect, ``init`` and ``navigate_to_candle`` messages are
        cached so newly-connecting browsers receive the current state.
        """
        # Cache for replay on new connections
        msg_type = message.get("type")
        if msg_type == "init":
            self._last_init = message
            # Clear stale navigate when a fresh init is pushed
            self._last_navigate = None
        elif msg_type == "navigate_to_candle":
            self._last_navigate = message

        if self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(
            self._manager.broadcast(message), self._loop
        )
