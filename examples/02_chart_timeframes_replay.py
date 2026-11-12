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
   (v1.3.0). It has no right edge (`time2=None`): it runs five candles past the
   last candle, on any timeframe. Right-click it → `Unlock`, then drag its top or
   bottom edge: this terminal prints the new prices. Its left edge stays where it
   is (`editable=("price",)`). Make it more than twice as tall and the program
   refuses: the zone jumps back and the status bar says «Refused: too tall».
5. Right-click it → `Delete`: the page asks first, and this terminal prints it.
6. Right-click a candle, away from any drawing → `Mark here`: a vertical line
   appears on that candle.
7. `GO TO` in the toolbar (or `Alt+G`) → a date and a time, as the chart's time
   axis shows it → `Go`: the candle that holds that time comes to the middle, at
   the zoom you had, under the crosshair (v1.3.0). A time outside the data goes
   to the first or the last candle and the status bar says so.
8. `⬡ AlgoTradeKit` at the top left opens the project's page in a new tab.
9. Settings → «Time zone» (v1.3.0): «Show times in» your own zone — the time
   axis, the crosshair, `GO TO` and the replay's clock follow. «Times are stamped
   in» says which zone the candles' times are written in: UTC for Binance (these),
   the broker's own time for MetaTrader. A program says so with
   `Chart(source_timezone="Europe/Athens")`; the page cannot tell by itself.
10. The green **trade** two hours back: `⏵⏵ REPLAY` → click a candle inside it.
    While it is open at the cursor it says «L  1:2.0R», the RR it was opened with —
    its result («✓ TP  +2.00R») would tell the end. Its stop and target show as
    usual; the box runs five candles past the cursor.
11. There is room after the last candle: draw a box or a line into it.
12. Draw a box (the toolbar's ▭), right-click it → `Measure`: this terminal prints
    its height and its span (`Chart.add_drawing_action`; it is offered on the
    drawings you draw, not on the program's).
13. The Fib tool: drag any of its four corners to resize it. Right-click it →
    `Levels…` → «0, 50, 100, 200» gives it those levels; Settings → «Fib levels»
    sets the levels a new Fib starts with (kept in this browser).
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
    # timezone="Asia/Tehran",        # show the times in this zone (Settings can too)
    # source_timezone="UTC",         # the zone the candles are stamped in (Binance: UTC)
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
              None, bottom,               # no right edge: it runs to the live edge
              color="#e3b341", border_color="#e3b341", label="High zone",
              key="zone:high", editable=("price",), title="Last hour's high")

# A finished trade, for the replay: open two hours back, closed at its target
opened = candles.iloc[-120]
entry = float(opened["close"])
risk = entry * 0.001
chart.add_position_box(
    open_time=int(opened["timestamp"]) // 1000,
    close_time=int(candles["timestamp"].iloc[-60]) // 1000,
    entry_price=entry, stop_loss=entry - risk, take_profit=entry + 2 * risk,
    direction="long", net_pnl=20.0, close_reason="tp", rr_ratio=2.0,
    planned_tp=entry + 2 * risk,   # what a replay says while it is open: «L  1:2.0R»
)


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


def measure(action):                             # action.drawing: the box you drew
    box = action.drawing
    if box.get("type") != "box":
        print("  Measure: draw a box first")
        return
    high, low = max(box["price1"], box["price2"]), min(box["price1"], box["price2"])
    span = abs((box["time2"] or box["time1"]) - box["time1"]) // 60
    print(f"  your box: {low:,.2f} – {high:,.2f} ({(high - low) / low:.3%} tall), "
          f"{span} minutes wide")


chart.add_drawing_action("Measure", measure)

print(f"  source {chart.source_timeframe} · showing {chart.display_timeframe} "
      f"({len(chart._bars)} candles)")
print(f"  selector offers: {', '.join(chart.timeframes)}")
print("\n  Opening the chart. Ctrl+C when you are done.")
chart.show(block=True)
