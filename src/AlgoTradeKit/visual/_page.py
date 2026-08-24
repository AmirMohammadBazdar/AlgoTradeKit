"""
algotradekit.visual._page
~~~~~~~~~~~~~~~~~~~~~~~~~
``ChartPage`` (v1.1.0) — several charts on one page, on one port, moving
together.

    from AlgoTradeKit.visual import Chart, ChartPage

    page = ChartPage(title="BTC")

    fast = Chart(title="BTC 3m"); fast.set_data(df_1m); fast.set_timeframe("3m")
    slow = Chart(title="BTC 5m"); slow.set_data(df_1m); slow.set_timeframe("5m")

    page.add(fast)              # row 0
    page.add(slow)              # row 1 — stacked vertically
    page.add(eth, row=1)        # row 1 as well — side by side with `slow`
    page.show(block=True)

Every chart keeps its whole API: indicators, drawings, live positions,
streaming, ``set_timeframe``.  Attaching one only swaps the object it sends
through, so nothing about how you drive a chart changes.

Layout is rows of cells: rows stack top to bottom, the charts inside one row
sit left to right, and the dividers drag on both axes.  That covers
TradingView's 1x2, 2x1, 2x2 and L layouts without a general grid engine.
"""
from __future__ import annotations

from .chart import Chart
from .server import PageServer


class _ViewProxy:
    """
    Stands in for a :class:`~AlgoTradeKit.visual.server.ChartServer` while a
    chart is attached to a page.

    A ``Chart`` talks to its server through exactly three things — ``send``,
    ``on_message`` and ``_last_init`` — so honouring those is all it takes for
    every existing chart method to work inside a page.
    """

    def __init__(self, page_server: PageServer, view_id: str) -> None:
        self._server  = page_server
        self._view_id = view_id
        self.on_message = None
        self._last_init: dict | None = None

    def send(self, message: dict) -> None:
        if message.get("type") == "init":
            self._last_init = message
        self._server.send(self._view_id, message)

    def start(self, open_browser: bool = True) -> None:      # pragma: no cover
        """No-op: the page owns the server."""

    def stop(self) -> None:                                  # pragma: no cover
        """No-op: the page owns the server."""

    @property
    def display_host(self) -> str:
        return self._server.display_host

    @property
    def port(self) -> int:
        return self._server.port

    @property
    def url(self) -> str:
        return f"{self._server.url}/view?view={self._view_id}"


class ChartPage:
    """
    A browser page holding several synchronised charts.

    Parameters
    ----------
    title : str
        Page title, shown in the tab.
    theme : "dark" | "light"
        Applied to the shell; each chart keeps its own theme setting.
    port : int
        TCP port.  0 = auto-select.  Everything — shell, chart pages and every
        WebSocket — is served from this one port, so one SSH tunnel is enough.
    host : str
        Interface to bind.

        .. warning::
           **Security** — ``host="0.0.0.0"`` exposes the page on every network
           interface with no authentication.  See :class:`Chart` for the full
           warning.
    sync_time : bool
        Scrolling or zooming one chart moves the others to the same time range.
        Charts on different timeframes stay aligned because the sync is by
        **time**, never by candle index.
    sync_crosshair : bool
        The crosshair is mirrored onto the other charts at the same instant.
    """

    def __init__(
        self,
        title: str = "Chart Page",
        theme: str = "dark",
        port:  int = 0,
        host:  str = "127.0.0.1",
        sync_time: bool = True,
        sync_crosshair: bool = True,
    ) -> None:
        self.title = title
        self.theme = theme
        self.sync_time = sync_time
        self.sync_crosshair = sync_crosshair

        self._server = PageServer(title=title, port=port, host=host)
        self._rows: list[list[str]] = []
        self._views: dict[str, Chart] = {}
        self._shown = False

    # ------------------------------------------------------------------
    # Composition
    # ------------------------------------------------------------------

    def add(self, chart: Chart, row: int | None = None, view_id: str | None = None) -> Chart:
        """
        Attach *chart* to the page and return it.

        ``row=None`` (default) starts a new row underneath, so charts stack
        vertically.  Passing an existing row index puts the chart **beside**
        the charts already in that row.

        May be called after :meth:`show`; the open page re-reads the layout.
        """
        if not isinstance(chart, Chart):
            raise TypeError(f"ChartPage.add() takes a Chart, got {type(chart).__name__}")
        if chart in self._views.values():
            raise ValueError(f"{chart.title!r} is already on this page")

        vid = view_id or f"v{len(self._views) + 1}"
        if vid in self._views:
            raise ValueError(f"view id {vid!r} is already used on this page")

        if row is None:
            self._rows.append([vid])
        else:
            if not 0 <= row <= len(self._rows):
                raise ValueError(
                    f"row {row} does not exist — the page has {len(self._rows)} "
                    f"row(s); pass row=None to start a new one"
                )
            if row == len(self._rows):
                self._rows.append([vid])
            else:
                self._rows[row].append(vid)

        self._views[vid] = chart
        self._attach(chart, vid)

        if self._shown:
            self._refresh_layout()
        return chart

    def _attach(self, chart: Chart, vid: str) -> None:
        """Point *chart* at this page's server instead of its own."""
        proxy = _ViewProxy(self._server, vid)
        proxy.on_message = chart._handle_browser_message
        chart._server = proxy
        chart._shown  = self._shown
        self._server.on_message[vid] = chart._handle_browser_message

    # ------------------------------------------------------------------
    # Layout payload
    # ------------------------------------------------------------------

    def _layout_payload(self) -> dict:
        return {
            "title": self.title,
            "theme": self.theme,
            "sync":  {"time": self.sync_time, "crosshair": self.sync_crosshair},
            "rows":  [list(row) for row in self._rows],
            "views": [
                {"id": vid, "title": chart.title}
                for vid, chart in self._views.items()
            ],
        }

    def _refresh_layout(self) -> None:
        self._server.layout_payload = self._layout_payload()
        # Tell every open frame the layout moved; the shell re-reads /layout.
        for vid in self._views:
            self._server.send(vid, {"type": "layout_changed"})

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def show(self, block: bool = False, open_browser: bool = True) -> ChartPage:
        """
        Serve the page and open it in a browser.

        ``block=True`` keeps the process alive until Ctrl+C, which is what a
        script that only draws charts wants.
        """
        if not self._views:
            raise ValueError("ChartPage has no charts — call page.add(chart) first")

        self._server.layout_payload = self._layout_payload()
        if not self._shown:
            self._server.start(open_browser=open_browser)
            self._shown = True

        # Every chart pushes its current state to its own frame
        for chart in self._views.values():
            chart._shown = True
            chart._send_init()

        if block:
            try:
                import time as _time

                while True:
                    _time.sleep(1)
            except KeyboardInterrupt:
                self.stop()
        return self

    def stop(self) -> None:
        """Shut the page server down."""
        self._server.stop()
        self._shown = False

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def url(self) -> str:
        """Browsable URL of the page."""
        return self._server.url

    @property
    def charts(self) -> list[Chart]:
        """The attached charts, in the order they were added."""
        return list(self._views.values())

    @property
    def rows(self) -> list[list[Chart]]:
        """The layout as rows of charts."""
        return [[self._views[vid] for vid in row] for row in self._rows]

    def view_id(self, chart: Chart) -> str | None:
        """The id the page gave *chart*, or ``None`` if it is not attached."""
        for vid, attached in self._views.items():
            if attached is chart:
                return vid
        return None

    def __repr__(self) -> str:
        shape = "x".join(str(len(r)) for r in self._rows) or "empty"
        return f"<ChartPage title={self.title!r} charts={len(self._views)} rows={shape}>"
