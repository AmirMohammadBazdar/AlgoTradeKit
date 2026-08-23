"""
Tests for v1.1.0 — ``ChartPage``: several charts on one page, one port.

Each chart is served into its own frame, so their DOM ids, legend handlers and
overlay canvases cannot collide.  What is tested here is the Python half: the
layout, the proxy that lets an ordinary ``Chart`` talk through the page server,
and the per-view routing of the WebSocket traffic in both directions.
"""
from __future__ import annotations

import json
import time
import urllib.request

import pandas as pd
import pytest

from AlgoTradeKit.visual import Chart, ChartPage

MIN_MS = 60_000
ANCHOR = 1_700_000_000_000 - (1_700_000_000_000 % 900_000)


def _frame(n: int = 300) -> pd.DataFrame:
    return pd.DataFrame({
        "timestamp": [ANCHOR + i * MIN_MS for i in range(n)],
        "open":      [100.0 + i for i in range(n)],
        "high":      [101.0 + i for i in range(n)],
        "low":       [ 99.0 + i for i in range(n)],
        "close":     [100.5 + i for i in range(n)],
        "volume":    [10.0] * n,
    })


def _chart(title: str, tf: str | None = None) -> Chart:
    c = Chart(title=title, display_timeframe=tf)
    c.set_data(_frame())
    return c


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------

class TestLayout:
    def test_add_stacks_rows_by_default(self):
        page = ChartPage()
        a, b = _chart("A"), _chart("B")
        page.add(a)
        page.add(b)
        assert page._rows == [["v1"], ["v2"]]
        assert page.charts == [a, b]

    def test_a_row_index_puts_charts_side_by_side(self):
        page = ChartPage()
        a, b, c = _chart("A"), _chart("B"), _chart("C")
        page.add(a)              # row 0
        page.add(b)              # row 1
        page.add(c, row=1)       # beside b
        assert page._rows == [["v1"], ["v2", "v3"]]
        assert [[ch.title for ch in row] for row in page.rows] == [["A"], ["B", "C"]]

    def test_row_equal_to_the_count_starts_a_new_row(self):
        page = ChartPage()
        page.add(_chart("A"))
        page.add(_chart("B"), row=1)
        assert page._rows == [["v1"], ["v2"]]

    def test_a_row_that_does_not_exist_is_rejected(self):
        page = ChartPage()
        page.add(_chart("A"))
        with pytest.raises(ValueError, match="row 5 does not exist"):
            page.add(_chart("B"), row=5)

    def test_only_charts_can_be_added(self):
        page = ChartPage()
        with pytest.raises(TypeError, match="takes a Chart"):
            page.add("not a chart")

    def test_the_same_chart_cannot_be_added_twice(self):
        page = ChartPage()
        chart = _chart("A")
        page.add(chart)
        with pytest.raises(ValueError, match="already on this page"):
            page.add(chart)

    def test_view_ids_can_be_chosen(self):
        page = ChartPage()
        chart = _chart("A")
        page.add(chart, view_id="fast")
        assert page.view_id(chart) == "fast"
        with pytest.raises(ValueError, match="already used"):
            page.add(_chart("B"), view_id="fast")

    def test_view_id_of_an_unattached_chart_is_none(self):
        assert ChartPage().view_id(_chart("A")) is None

    def test_repr_shows_the_shape(self):
        page = ChartPage(title="desk")
        page.add(_chart("A"))
        page.add(_chart("B"))
        page.add(_chart("C"), row=1)
        assert "charts=3" in repr(page) and "rows=1x2" in repr(page)

    def test_layout_payload(self):
        page = ChartPage(title="desk", theme="light", sync_crosshair=False)
        page.add(_chart("A"))
        page.add(_chart("B"), row=0)
        payload = page._layout_payload()
        assert payload["title"] == "desk"
        assert payload["theme"] == "light"
        assert payload["sync"] == {"time": True, "crosshair": False}
        assert payload["rows"] == [["v1", "v2"]]
        assert payload["views"] == [
            {"id": "v1", "title": "A"}, {"id": "v2", "title": "B"}
        ]

    def test_showing_an_empty_page_is_refused(self):
        with pytest.raises(ValueError, match="has no charts"):
            ChartPage().show()


# ---------------------------------------------------------------------------
# The proxy that stands in for a chart's own server
# ---------------------------------------------------------------------------

class TestViewProxy:
    def test_attaching_redirects_the_chart_without_changing_its_api(self):
        page  = ChartPage()
        chart = _chart("A")
        original = chart._server
        page.add(chart)

        assert chart._server is not original
        assert chart._server.port == page._server.port     # one port for all
        assert page._server.on_message["v1"] == chart._handle_browser_message

    def test_chart_messages_are_tagged_with_their_view(self):
        page = ChartPage()
        a, b = _chart("A"), _chart("B")
        page.add(a)
        page.add(b)

        seen: list[tuple[str, str]] = []
        page._server.send = lambda vid, msg: seen.append((vid, msg["type"]))
        a._shown = b._shown = True

        a.add_hline(1.0)
        b.add_hline(2.0)
        assert seen == [("v1", "add_drawing"), ("v2", "add_drawing")]

    def test_the_proxy_caches_init_for_a_refresh(self):
        page  = ChartPage()
        chart = _chart("A")
        page.add(chart)
        chart._shown = True
        page._server.send = lambda vid, msg: None

        chart._send_init()
        assert chart._server._last_init["type"] == "init"
        # which is what keeps _refresh_init_cache() working inside a page
        chart.add_hline(5.0)
        assert chart._server._last_init is not None

    def test_view_url_points_at_the_pages_own_port(self):
        page  = ChartPage()
        chart = _chart("A")
        page.add(chart)
        assert chart._server.url.endswith("/view?view=v1")
        assert str(page._server.port) in chart._server.url


# ---------------------------------------------------------------------------
# Served for real
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def page():
    """One served page for the whole module — starting a server per test would
    dominate the runtime."""
    page = ChartPage(title="BTC desk")
    page.add(_chart("BTC 3m", "3m"))                 # v1, row 0
    page.add(_chart("BTC 5m", "5m"))                 # v2, row 1
    page.add(_chart("ETH 5m", "5m"), row=1)          # v3, beside v2
    page.show(block=False, open_browser=False)
    time.sleep(0.5)
    yield page
    page.stop()


class TestServedPage:
    @staticmethod
    def _get(page, path: str) -> str:
        with urllib.request.urlopen(page.url + path, timeout=5) as resp:
            return resp.read().decode()

    def test_the_shell_is_served_at_the_root(self, page):
        html = self._get(page, "/")
        assert "CHART PAGE SHELL" in html
        assert "iframe" in html

    def test_the_chart_page_is_served_to_the_frames(self, page):
        html = self._get(page, "/view")
        assert "createPane" in html          # the ordinary chart page

    def test_layout_endpoint(self, page):
        layout = json.loads(self._get(page, "/layout"))
        assert layout["rows"] == [["v1"], ["v2", "v3"]]
        assert [v["title"] for v in layout["views"]] == ["BTC 3m", "BTC 5m", "ETH 5m"]

    def test_everything_is_on_one_port(self, page):
        """One port means one SSH tunnel, which is how this gets viewed."""
        ports = {c._server.port for c in page.charts} | {page._server.port}
        assert len(ports) == 1

    def test_each_socket_receives_only_its_own_view(self, page):
        from websockets.sync.client import connect

        url = f"ws://127.0.0.1:{page._server.port}/ws"
        with connect(f"{url}?view=v1") as w1, connect(f"{url}?view=v2") as w2:
            init1 = json.loads(w1.recv(timeout=5))
            init2 = json.loads(w2.recv(timeout=5))
            assert init1["title"] == "BTC 3m" and init1["displayTimeframe"] == "3m"
            assert init2["title"] == "BTC 5m" and init2["displayTimeframe"] == "5m"
            assert len(init1["bars"]) == 100          # 300 one-minute candles
            assert len(init2["bars"]) == 60

            page.charts[0].add_hline(123.0)
            got = json.loads(w1.recv(timeout=5))
            assert got["type"] == "add_drawing"

            with pytest.raises(TimeoutError):
                w2.recv(timeout=1.0)                  # the other view stays quiet

    def test_browser_messages_reach_the_right_chart(self, page):
        from websockets.sync.client import connect

        url = f"ws://127.0.0.1:{page._server.port}/ws?view=v2"
        with connect(url) as ws:
            ws.recv(timeout=5)                        # the replayed init
            ws.send(json.dumps({"type": "set_timeframe", "tf": "15m"}))
            answer = json.loads(ws.recv(timeout=5))

        assert answer["displayTimeframe"] == "15m"
        assert page.charts[1].display_timeframe == "15m"
        assert page.charts[0].display_timeframe == "3m"     # untouched
        page.charts[1].set_timeframe("5m")                  # leave it as found

    def test_a_chart_added_after_show_appears(self, page):
        added = _chart("LTC 5m", "5m")
        page.add(added, row=0)
        layout = json.loads(self._get(page, "/layout"))
        assert layout["rows"][0] == ["v1", "v4"]
        assert added._shown is True
