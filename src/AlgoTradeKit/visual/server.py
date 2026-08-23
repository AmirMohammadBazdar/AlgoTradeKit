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

def _find_free_port(start: int = 8700, host: str = "127.0.0.1") -> int:
    """Find a free TCP port starting from *start* (scans up to start+200).

    The probe binds on *host* so the port is guaranteed free on the same
    interface(s) the server will later bind to (``"0.0.0.0"`` probes all
    IPv4 interfaces; ``"::"`` probes IPv6).
    """
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    for port in range(start, start + 200):
        if port in _reserved_ports:       # already claimed by another Chart()
            continue
        with socket.socket(family, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                _reserved_ports.add(port) # claim it immediately
                return port
            except OSError:
                continue
    raise RuntimeError(
        f"No free TCP port found in range 8700–8900 "
        f"(probed on host {host!r} — is it a local interface? "
        f"Pass an explicit port= to skip probing)"
    )


# ---------------------------------------------------------------------------
# Connection manager
# ---------------------------------------------------------------------------

class ConnectionManager:
    """Tracks active WebSocket connections and broadcasts messages to all of them."""

    def __init__(self):
        self.active: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self.active.discard(ws)

    async def broadcast(self, message: dict) -> None:
        dead: set[WebSocket] = set()
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
    host : str
        Network interface to bind to (v1.0.0).  Default ``"127.0.0.1"``
        keeps the server reachable from this machine only.

        .. warning::
           **Security** — setting ``host="0.0.0.0"`` exposes the chart
           server on **every network interface**: anyone who can reach this
           machine (LAN, or the whole internet on an unfirewalled VPS) can
           open the chart, see your data and send WebSocket messages.
           There is no authentication.  Only use ``"0.0.0.0"`` on trusted /
           firewalled networks; an SSH tunnel to the default
           ``127.0.0.1`` binding is the safer alternative.
    """

    def __init__(self, title: str = "Chart", port: int = 0, host: str = "127.0.0.1") -> None:
        self.title   = title
        self.host    = host
        self.port    = port or _find_free_port(host=host)
        self._manager = ConnectionManager()
        self._loop:   asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._app    = self._build_app()

        # Set by Chart to receive browser → Python messages.
        # Signature: on_message(msg: dict) -> None
        self.on_message = None

        # v0.7.1: replay cache — new browsers get the current state immediately
        self._last_init:     dict | None = None
        self._last_navigate: dict | None = None

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
                host=self.host,
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
            webbrowser.open(f"{self.url}/?title={self.title}")

        log.info("ChartServer started → %s (bound to %s)", self.url, self.host)

    def stop(self) -> None:
        _reserved_ports.discard(self.port)
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

    def __del__(self) -> None:
        # A server that is created and never started still holds its port
        # reservation, and only stop() releases one.  Charts built but never
        # shown would therefore drain the 200-port range.  A running server is
        # referenced by its own thread, so it is never collected here.
        try:
            _reserved_ports.discard(self.port)
        except Exception:       # pragma: no cover - interpreter shutdown
            pass

    # ── URLs ───────────────────────────────────────────────────────────────

    @property
    def display_host(self) -> str:
        """Host usable in a browser URL.

        ``0.0.0.0`` / ``::`` are bind-to-all addresses, not destinations —
        substitute the loopback address for local display.  Remote viewers
        replace it with the machine's real IP (prints those URLs).
        """
        return "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host

    @property
    def url(self) -> str:
        """Browsable URL of this server (uses :attr:`display_host`)."""
        return f"http://{self.display_host}:{self.port}"

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


# ---------------------------------------------------------------------------
# Page server (v1.1.0) — one port, several charts
# ---------------------------------------------------------------------------

class PageServer:
    """
    Serves a multi-chart page: a shell that lays out one ``<iframe>`` per view,
    every view loading the ordinary chart page.

    Why frames rather than one document with several charts: a chart's DOM ids
    (``ilgrp-EMA(20)``), its inline legend handlers and its overlay canvases
    are all page-global. Two charts in one document would collide on every one
    of them. A document per view is what "independent chart" already means to a
    browser, and it leaves the single-chart code — which the whole simulate and
    trader display rests on — completely untouched.

    Everything still arrives on **one port**, which matters when the only way in
    is an SSH tunnel.

    Routes
    ------
    ``GET /``        the shell
    ``GET /view``    the ordinary chart page (loaded by each frame)
    ``GET /layout``  JSON: title, theme, sync flags, rows of view ids
    ``WS  /ws``      ``?view=<id>`` picks which view's messages this socket gets

    Parameters
    ----------
    title, port, host
        As :class:`ChartServer`.  The same ``host="0.0.0.0"`` security warning
        applies — there is no authentication.
    """

    def __init__(self, title: str = "Chart Page", port: int = 0,
                 host: str = "127.0.0.1") -> None:
        self.title = title
        self.host  = host
        self.port  = port or _find_free_port(host=host)

        self._loop:   asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock    = threading.Lock()

        # ws → the view id it subscribed to
        self._sockets: dict[WebSocket, str] = {}
        # view id → its cached init / navigate, replayed on (re)connect
        self._last_init:     dict[str, dict] = {}
        self._last_navigate: dict[str, dict] = {}

        # Filled in by ChartPage before start()
        self.layout_payload: dict = {}
        # view id → callback(msg: dict) for browser → Python messages
        self.on_message: dict = {}

        self._app = self._build_app()

    # ── FastAPI application ────────────────────────────────────────────────

    def _build_app(self) -> FastAPI:
        app = FastAPI(docs_url=None, redoc_url=None)

        @app.get("/", response_class=HTMLResponse)
        async def shell():
            return HTMLResponse((STATIC_DIR / "page.html").read_text(encoding="utf-8"))

        @app.get("/view", response_class=HTMLResponse)
        async def view():
            return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))

        @app.get("/layout")
        async def layout():
            return self.layout_payload

        @app.websocket("/ws")
        async def ws_endpoint(websocket: WebSocket):
            view_id = websocket.query_params.get("view", "")
            await websocket.accept()
            with self._lock:
                self._sockets[websocket] = view_id

            for cache in (self._last_init, self._last_navigate):
                cached = cache.get(view_id)
                if cached is not None:
                    try:
                        await websocket.send_text(json.dumps(cached))
                    except Exception:
                        pass

            try:
                while True:
                    raw = await websocket.receive_text()
                    handler = self.on_message.get(view_id)
                    if handler:
                        try:
                            handler(json.loads(raw))
                        except Exception:
                            pass
            except WebSocketDisconnect:
                with self._lock:
                    self._sockets.pop(websocket, None)

        return app

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def start(self, open_browser: bool = True) -> None:
        """Start the server in a daemon thread and optionally open a browser tab."""
        ready = threading.Event()

        def _run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            config = uvicorn.Config(
                self._app, host=self.host, port=self.port,
                loop="asyncio", log_level="warning",
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
            webbrowser.open(self.url)

        log.info("PageServer started → %s (bound to %s)", self.url, self.host)

    def stop(self) -> None:
        _reserved_ports.discard(self.port)
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

    def __del__(self) -> None:
        try:
            _reserved_ports.discard(self.port)
        except Exception:       # pragma: no cover - interpreter shutdown
            pass

    # ── URLs ───────────────────────────────────────────────────────────────

    @property
    def display_host(self) -> str:
        """Host usable in a browser URL — see :attr:`ChartServer.display_host`."""
        return "127.0.0.1" if self.host in ("0.0.0.0", "::") else self.host

    @property
    def url(self) -> str:
        return f"http://{self.display_host}:{self.port}"

    # ── Messaging ──────────────────────────────────────────────────────────

    def send(self, view_id: str, message: dict) -> None:
        """Send *message* to the sockets showing *view_id* (thread-safe)."""
        msg_type = message.get("type")
        if msg_type == "init":
            self._last_init[view_id] = message
            self._last_navigate.pop(view_id, None)
        elif msg_type == "navigate_to_candle":
            self._last_navigate[view_id] = message

        if self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._broadcast(view_id, message), self._loop)

    async def _broadcast(self, view_id: str, message: dict) -> None:
        text = json.dumps(message)
        dead = []
        with self._lock:
            targets = [ws for ws, vid in self._sockets.items() if vid == view_id]
        for ws in targets:
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        if dead:
            with self._lock:
                for ws in dead:
                    self._sockets.pop(ws, None)
