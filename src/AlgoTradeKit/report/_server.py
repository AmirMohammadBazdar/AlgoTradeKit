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
  5. Re-broadcasts fresh stats over the same WebSocket via
     ``push_update()`` — the open page re-renders in place (v1.0.0)

The server runs in a daemon background thread (same pattern as
``visual.server.ChartServer``) so it never blocks the Python process.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import threading
from collections.abc import Callable
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

log = logging.getLogger("algotradekit.report.server")

STATIC_DIR = Path(__file__).parent / "static"

_reserved_ports: set[int] = set()


# ---------------------------------------------------------------------------
# Port helper
# ---------------------------------------------------------------------------

def _find_free_port(start: int = 8800, host: str = "127.0.0.1") -> int:
    """Find a free TCP port starting from *start* (scans up to start+200).

    The probe binds on *host* so the port is guaranteed free on the same
    interface(s) the server will later bind to (``"0.0.0.0"`` probes all
    IPv4 interfaces; ``"::"`` probes IPv6).
    """
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    for port in range(start, start + 200):
        if port in _reserved_ports:
            continue
        with socket.socket(family, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                _reserved_ports.add(port)
                return port
            except OSError:
                continue
    raise RuntimeError(
        f"No free TCP port found in range 8800–9000 "
        f"(probed on host {host!r} — is it a local interface? "
        f"Pass an explicit port= to skip probing)"
    )


# ---------------------------------------------------------------------------
# Connection manager
# ---------------------------------------------------------------------------

class _ConnectionManager:
    def __init__(self) -> None:
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
    host : str
        Network interface to bind to (v1.0.0).  Default ``"127.0.0.1"``
        keeps the server reachable from this machine only.

        .. warning::
           **Security** — setting ``host="0.0.0.0"`` exposes the report
           server on **every network interface**: anyone who can reach this
           machine (LAN, or the whole internet on an unfirewalled VPS) can
           open the report, see your data and send WebSocket messages.
           There is no authentication.  Only use ``"0.0.0.0"`` on trusted /
           firewalled networks; an SSH tunnel to the default
           ``127.0.0.1`` binding is the safer alternative.
    """

    def __init__(
        self,
        title: str = "AlgoTradeKit Report",
        port: int = 0,
        on_open_chart: Callable[[int], None] | None = None,
        host: str = "127.0.0.1",
    ) -> None:
        self.title         = title
        self.host          = host
        self.port          = port or _find_free_port(host=host)
        self.on_open_chart = on_open_chart
        self._manager      = _ConnectionManager()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._uvicorn: uvicorn.Server | None = None
        self._pending_data: dict | None = None   # sent to every new WS connect
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
                host=self.host,
                port=self.port,
                loop="asyncio",
                log_level="warning",
            )
            server = uvicorn.Server(config)
            self._uvicorn = server
            _orig = server.startup

            async def _patched_startup(sockets=None):
                await _orig(sockets)
                ready.set()

            server.startup = _patched_startup
            try:
                self._loop.run_until_complete(server.serve())
            finally:
                # Let pending callbacks finish before the loop goes away,
                # otherwise uvicorn's lifespan task is destroyed mid-flight.
                try:
                    self._loop.run_until_complete(self._loop.shutdown_asyncgens())
                finally:
                    self._loop.close()

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        ready.wait(timeout=5)

        if open_browser:
            import webbrowser
            webbrowser.open(f"{self.url}/")

        log.info("ReportServer started → %s (bound to %s)", self.url, self.host)

    def stop(self) -> None:
        """Shut the server down cleanly.

        Asking uvicorn to exit lets ``serve()`` return on its own, so the
        thread unwinds normally.  Stopping the loop underneath it instead --
        which is what this used to do -- raised "Event loop stopped before
        Future completed" out of every server thread.
        """
        _reserved_ports.discard(self.port)
        if self._uvicorn is not None:
            self._uvicorn.should_exit = True
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3)
        self._loop = None
        self._uvicorn = None

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

    # ── Data push ─────────────────────────────────────────────────────────

    def send(self, message: dict) -> None:
        """Thread-safe broadcast to all connected browser clients.

        The coroutine is created only once the loop is known to be usable --
        building it first means a send after ``stop()`` leaves an un-awaited
        coroutine for the garbage collector to complain about.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(self._manager.broadcast(message), loop)
        except RuntimeError:        # loop stopped between the check and the call
            pass

    def set_report_data(self, data: dict) -> None:
        """
        Store the report payload so new WebSocket connections receive it
        immediately, and broadcast to any already-connected clients.
        """
        self._pending_data = {"type": "report_data", **data}
        self.send(self._pending_data)

    def push_update(self, report) -> None:
        """
        Re-broadcast fresh stats to every open report page (v1.0.0).

        The page re-renders in place, and the replay cache is refreshed so
        a page refresh (or a new tab) mid-live-run shows the current stats
        instead of the state at ``start()`` time.

        Parameters
        ----------
        report : SimulateReport | dict
            A ``SimulateReport`` (serialised via ``build_report_payload``)
            or an already-built payload dict — e.g. a combined payload from
            ``build_combined_report_payload`` — pushed as-is.

        Notes
        -----
        The chart-link keys (``has_chart`` / ``chart_port``) of the
        previously pushed payload are carried forward when the new payload
        does not set them: the linked chart server does not change between
        stat refreshes.  To unlink a chart, push via ``set_report_data``
        with the keys set explicitly.
        """
        if isinstance(report, dict):
            payload = dict(report)
        else:
            from ._builder import build_report_payload
            payload = build_report_payload(report)

        prev = self._pending_data or {}
        if not payload.get("has_chart") and prev.get("has_chart"):
            payload["has_chart"] = prev["has_chart"]
            payload["chart_port"] = prev.get("chart_port")

        self.set_report_data(payload)
