"""
Timeframe switching and bar replay on one chart  *(v1.1.0)*

    python examples/04_chart_timeframes_replay.py [path/to/1m.csv]

The chart is given **1m** candles and told to display **5m**. Everything else
happens in the browser toolbar.

What to try
-----------
1. The `5m` button in the toolbar → pick 15m, 1h, then `1m (source)`.
   The candles are rebuilt in Python each time.
2. Watch the two indicators as you switch:
   * **EMA 50** was added by spec, so it is *recomputed* on the new candles —
     the line changes shape.
   * **1m close** was handed over as finished points, so it can only be
     *thinned*; its legend entry is marked approximate. Switch back to `1m`
     and every original point returns.
3. `⏵⏵ REPLAY` → click a candle. History stops there.
   * `▶|` steps **one minute** — the 5m candle grows in front of you and only
     closes on its own boundary.
   * `Space` plays, the selector changes speed, `Shift+←` steps back.
   * The EMA stops at the last *closed* candle: no value is drawn for a candle
     that has not finished.
   * `Esc` leaves replay and every candle comes back.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _sample_data import one_minute_candles

from AlgoTradeKit.visual import Chart

candles = one_minute_candles(days=5)
print(f"  {len(candles)} 1m candles")

chart = Chart(
    title="BTCUSDT — 1m data, any timeframe",
    display_timeframe="5m",          # what to show; None would show the 1m data
    # timeframes=["1m", "5m", "15m", "1h"],   # narrow the selector if you like
)
chart.set_data(candles)

# Recomputed on every timeframe change — the maths runs in Python
chart.add_indicator_spec({"kind": "ema", "period": 50})

# Handed over as finished points: nothing to recompute, so it gets thinned
chart.add_indicator_from_atk(
    candles.assign(value=candles["close"])[["timestamp", "value"]],
    name="1m close", color="#8b949e",
)

print(f"  source {chart.source_timeframe} · showing {chart.display_timeframe} "
      f"({len(chart._bars)} candles)")
print(f"  selector offers: {', '.join(chart.timeframes)}")
print("\n  Opening the chart. Ctrl+C when you are done.")
chart.show(block=True)
