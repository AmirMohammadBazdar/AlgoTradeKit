"""
Tests for v1.2.0 Part 3 — chart pictures in the real headless browser.

Skipped where Playwright and its Chromium are not installed, as the node tests are
without node, and where this machine cannot reach unpkg.com, where the chart page
loads lightweight-charts from.  ``tests/test_notify_pictures.py`` covers the rest
with the engine faked, and never skips.
"""
from __future__ import annotations

import struct
import zlib

import pandas as pd
import pytest

from AlgoTradeKit.visual import Chart, _snapshot

T0800, T1400 = 1791964800, 1791986400      # 2026-10-14 08:00 and 14:00 UTC, in seconds
LINE_PRICE = 64_030.0
MAGENTA = "#ff00ff"


def _engine_or_skip() -> None:
    try:
        _snapshot.check_engine()
    except ImportError as exc:
        pytest.skip(str(exc))
    except _snapshot.SnapshotError as exc:
        pytest.skip(str(exc))


@pytest.fixture(scope="module")
def chart():
    """BTCUSDT 15m for 2026-10-14, rising a dollar a candle, with EMA(20) and a
    magenta line at 64,030."""
    _engine_or_skip()
    first = int(pd.Timestamp("2026-10-14 00:00", tz="UTC").timestamp() * 1000)
    frame = pd.DataFrame([
        {"timestamp": first + k * 900_000, "open": 64_000 + k, "high": 64_010 + k,
         "low": 63_990 + k, "close": 64_005 + k, "volume": 1.0 + k % 5} for k in range(96)])
    built = Chart(title="BTCUSDT")
    built.set_data(frame)
    built.add_indicator_spec({"kind": "ema", "period": 20})
    built.add_hline(LINE_PRICE, color=MAGENTA, line_width=2)
    try:
        built.screenshot(width=320, height=200)
    except _snapshot.SnapshotError as exc:
        if str(exc) == _snapshot.NO_UNPKG:
            pytest.skip(str(exc))
        raise
    return built


def _png_rows(data: bytes, last_row: int) -> tuple[int, int, int, list[bytearray]]:
    """Width, height, channels and the rows up to *last_row* of an 8-bit RGB(A) PNG."""
    pos, idat = 8, b""
    width = height = depth = kind = 0
    while pos < len(data):
        length = int.from_bytes(data[pos:pos + 4], "big")
        tag, body = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if tag == b"IHDR":
            width, height, depth, kind = struct.unpack(">IIBB", body[:10])
        elif tag == b"IDAT":
            idat += body
    assert depth == 8 and kind in (2, 6)
    channels = 3 if kind == 2 else 4
    stride = width * channels
    raw = zlib.decompress(idat)
    rows: list[bytearray] = []
    previous = bytearray(stride)
    offset = 0
    for _ in range(min(height, last_row + 1)):
        method, line = raw[offset], bytearray(raw[offset + 1:offset + 1 + stride])
        offset += 1 + stride
        for x in range(stride):
            left = line[x - channels] if x >= channels else 0
            up = previous[x]
            corner = previous[x - channels] if x >= channels else 0
            if method == 1:
                line[x] = (line[x] + left) & 255
            elif method == 2:
                line[x] = (line[x] + up) & 255
            elif method == 3:
                line[x] = (line[x] + (left + up) // 2) & 255
            elif method == 4:
                guess = left + up - corner
                near = min((abs(guess - left), 0, left), (abs(guess - up), 1, up),
                           (abs(guess - corner), 2, corner))[2]
                line[x] = (line[x] + near) & 255
        rows.append(line)
        previous = line
    return width, height, channels, rows


def _page_facts(chart: Chart, start, end, timezone=None) -> dict:
    """Open the screenshot page in the engine's own browser and read back what it
    shows: the candles in view, the axis labels, and where the magenta line is."""
    query = chart._snapshot_query(start, end, timezone)
    chart.show(open_browser=False)
    chart._server._last_init = chart._build_init_payload()
    url = f"{chart.url}/?{query}"

    def read(playwright, get_browser):
        context = get_browser().new_context(viewport={"width": 1280, "height": 720},
                                            device_scale_factor=1)
        try:
            page = context.new_page()
            page.goto(url)
            page.wait_for_function(_snapshot.CHART_READY, timeout=60_000)
            return page.evaluate("""(price) => {
              const r = mainChart.timeScale().getVisibleRange();
              const inside = allBars.filter(b => b.time >= r.from && b.time <= r.to);
              const panes = document.getElementById('panes').getBoundingClientRect();
              return {first: inside[0].time, last: inside[inside.length - 1].time,
                      count: inside.length, ticks: snapTicks.map(t => [t[0], t[2]]),
                      title: document.getElementById('chart-title').textContent,
                      lineY: panes.top + mainSeries.priceToCoordinate(price)};
            }""", LINE_PRICE)
        finally:
            context.close()

    return _snapshot._engine.run(read, 60.0)


class TestTheRealPicture:
    def test_a_png_of_1280_by_720(self, chart):
        picture = chart.screenshot("2026-10-14 08:00", "2026-10-14 14:00")
        assert picture[:8] == b"\x89PNG\r\n\x1a\n"
        assert struct.unpack(">II", picture[16:24]) == (1280, 720)

    def test_q32_25_candles_from_0800_to_1400_read_back(self, chart):
        facts = _page_facts(chart, "2026-10-14 08:00", "2026-10-14 14:00")
        assert (facts["first"], facts["last"], facts["count"]) == (T0800, T1400, 25)
        assert facts["title"] == "BTCUSDT · 15m · 2026-10-14 08:00 – 14:00 UTC"

    def test_the_1400_tick_reads_1730_in_tehran(self, chart):
        facts = _page_facts(chart, "2026-10-14 08:00", "2026-10-14 14:00", "Asia/Tehran")
        assert [T1400, "17:30"] in facts["ticks"]
        assert facts["title"] == "BTCUSDT · 15m · 2026-10-14 11:30 – 17:30 (UTC+03:30)"

    def test_a_drawings_colour_is_where_it_belongs(self, chart):
        facts = _page_facts(chart, "2026-10-14 08:00", "2026-10-14 14:00")
        picture = chart.screenshot("2026-10-14 08:00", "2026-10-14 14:00")
        y = round(facts["lineY"])
        width, height, channels, rows = _png_rows(picture, y + 3)
        assert (width, height) == (1280, 720)

        def magenta(row: int, x: int) -> bool:
            red, green, blue = rows[row][x * channels:x * channels + 3]
            return red > 180 and green < 110 and blue > 180

        assert any(magenta(row, 600) for row in range(y - 2, y + 3))
        assert not any(magenta(row, 600) for row in range(max(0, y - 40), y - 10))
