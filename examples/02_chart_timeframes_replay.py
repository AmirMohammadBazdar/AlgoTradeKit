"""
Timeframe switching, bar replay and edits on one chart  *(v1.1.0, v1.3.0)*

    python examples/02_chart_timeframes_replay.py [path/to/1m.csv]

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
4. The yellow **High zone** around the last hour's high is the program's own
   (v1.3.0). Right-click it → `Unlock`, then drag its top or bottom edge: this
   terminal prints the new prices. Its left and right edges stay where they are
   (`editable=("price",)`). Make it more than twice as tall and the program
   refuses: the zone jumps back and the status bar says «Refused: too tall».
5. Right-click it → `Delete`: the page asks first, and this terminal prints it.
6. Right-click a candle, away from any drawing → `Mark here`: a vertical line
   appears on that candle.
7. `GO TO` in the toolbar (or `Alt+G`) → a date and a time, as the chart's time
   axis shows it → `Go`: the candle that holds that time comes to the middle, at
   the zoom you had, under the crosshair (v1.3.0). A time outside the data goes
   to the first or the last candle and the status bar says so.
8. `⬡ AlgoTradeKit` at the top left opens the project's page in a new tab.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _sample_data import one_minute_candles

from AlgoTradeKit.visual import Chart, EditRefused

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

# ── Edits on the page reach the program (v1.3.0) ─────────────────────────────
# A keyed drawing reports what the user does to it. Chart(edit_key="…") would let
# only the address with ?key=… change anything; this one lets every page.
last_hour = candles.tail(60)
high = float(last_hour["high"].max())
top, bottom = high * 1.0002, high * 0.9998
chart.add_box(int(last_hour["timestamp"].iloc[0]) // 1000, top,
              int(last_hour["timestamp"].iloc[-1]) // 1000, bottom,
              color="#e3b341", border_color="#e3b341", label="High zone",
              key="zone:high", editable=("price",), title="Last hour's high")


@chart.on_drawing_edit
def zone_edited(edit):
    height = abs(edit.after["price1"] - edit.after["price2"])
    if height > 2 * (top - bottom):
        print(f"  {edit.key}: refused, {height:,.2f} tall")
        raise EditRefused("too tall")            # it goes back, in every open page
    print(f"  {edit.key}: {edit.after['price2']:,.2f} – {edit.after['price1']:,.2f}")


@chart.on_drawing_delete
def zone_deleted(edit):
    print(f"  {edit.key}: deleted")


def mark_here(action):                           # action.time: the candle's opening, ms
    chart.add_vline(action.time // 1000, label="mark")
    price = "—" if action.price is None else f"{action.price:,.2f}"
    print(f"  marked the candle of {action.time} (UTC ms) at {price}")


chart.add_action("Mark here", mark_here)

print(f"  source {chart.source_timeframe} · showing {chart.display_timeframe} "
      f"({len(chart._bars)} candles)")
print(f"  selector offers: {', '.join(chart.timeframes)}")
print("\n  Opening the chart. Ctrl+C when you are done.")
chart.show(block=True)
