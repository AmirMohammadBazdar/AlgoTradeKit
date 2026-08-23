"""
Several charts on one page, moving together  *(v1.1.0)*

    python examples/05_multi_chart_page.py [path/to/1m.csv]

Three views of the *same* 1m candles — 3m, 5m and 1h — laid out as one chart
across the top and two side by side underneath. Everything is served from one
port, so viewing this on a VPS needs a single SSH tunnel.

What to try
-----------
1. Scroll or zoom any chart. The others follow to the same **clock time**, not
   the same candle count, so the 1h view stays aligned with the 3m one.
2. Move the mouse over one chart — the crosshair appears on the others at the
   same instant.
3. `TIME` and `CROSSHAIR` in the page header turn each sync off and on.
4. Drag the divider between the rows, and the one between the two lower charts.
5. Each chart keeps its own toolbar: drawing tools, indicators, and its own
   timeframe selector. Change one chart's timeframe — only that one moves.
6. `⏵⏵ REPLAY` in the **page header** arms all three. Click a candle on any of
   them and they all jump there; one step advances every chart by one minute,
   so the 5m candle closes on its boundary while the 3m one is still forming.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _sample_data import one_minute_candles

from AlgoTradeKit.visual import Chart, ChartPage

candles = one_minute_candles(days=5)
print(f"  {len(candles)} 1m candles")


def view(title: str, timeframe: str) -> Chart:
    chart = Chart(title=title, display_timeframe=timeframe)
    chart.set_data(candles)
    chart.add_indicator_spec({"kind": "ema", "period": 20})
    return chart


fast = view("BTC 3m", "3m")
slow = view("BTC 5m", "5m")
wide = view("BTC 1h", "1h")

page = ChartPage(title="BTC desk")
page.add(fast)              # row 0 — across the top
page.add(slow)              # row 1 — a new row underneath
page.add(wide, row=1)       # row 1 as well — beside `slow`

for chart in page.charts:
    print(f"  {chart.title:10} {chart.display_timeframe:>4} "
          f"→ {len(chart._bars):5} candles")
print(f"\n  One page, one port: {page.url}")
print("  Ctrl+C when you are done.")
page.show(block=True)
