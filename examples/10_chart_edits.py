"""
Edits on the chart reach the strategy  *(v1.3.0)*

    python examples/10_chart_edits.py

Paper trading on Binance's public BTCUSDT 1m candles (no API key). Every hour's
high and low become **zones** ±0.02 % around them. A close above a high zone's top
goes long, a close below a low zone's bottom goes short, the stop at the zone's far
side; a traded zone is erased. The zones are the strategy's own drawings, keyed by
the hour they were made, and the chart lets you change them.

What to try (from the address printed with `?key=…`; over an SSH tunnel to
127.0.0.1:8710 when this runs on a server)
-----------
1. Right-click a zone → `Unlock`, then drag its top or bottom edge. The terminal
   says what the strategy now trades. A zone more than 0.2 % tall is refused: it
   jumps back and the status bar says «Refused: Breakout refused it».
2. Delete a zone: the page asks first, the strategy forgets it, and no trade comes
   from it.
3. Right-click a candle → `Add zone here`: a zone at that price — a high zone above
   the last close, a low one below it.
4. Everything you changed is saved in `chart_edits.json`, here. `Re-run with edits`
   in the toolbar stops the session and starts it again: the new start walks the
   history with your edits, so a deleted zone has no trade in the seed either.
   Delete the file to forget them all.
5. Under the last price, on the price axis: the time left until the candle
   closes, counted on the feed's own clock — it appears with the first new candle
   after the start. Switch to `5m` or `1h` and it counts that candle. Settings →
   «Time to the candle's close» turns it off (kept in this browser).
6. `GO TO` (or `Alt+G`) takes the chart to a date and a time, as its axis shows it.
7. A zone that can still trade has no right edge (`"time2": None`): it runs five
   candles past the last candle. A traded zone, or one older than four hours, is
   erased.
8. Draw a box (the toolbar's ▭) where you see a zone, right-click it →
   `Make it a zone`: the strategy takes your box as a zone from its left edge — a
   high zone above the last close, a low one below — and your box becomes it.
   Saved with the rest, so a re-run makes it again on time
   (`chart_drawing_actions`; offered on the drawings you draw).
9. Settings → «Time zone» → «Show times in»: your own zone. The candles are
   stamped in UTC on Binance; on MetaTrader set
   `TraderConfig(chart_source_timezone=...)` to the broker's zone.
10. Click a zone and press Ctrl+C (⌘C), or right-click it → `📋 Copy`. Ctrl+V
    puts a copy on top of it: yours, so drag it anywhere — the strategy never
    hears of it, and `Make it a zone` works on it as on a box you drew.
    Right-click the chart away from drawings → `📋 Paste here` puts one at the
    click. Ctrl+V on another chart page, in any window, pastes it there too.
"""
from __future__ import annotations

import os

from AlgoTradeKit.broker import Broker
from AlgoTradeKit.strategy import BaseStrategy, Signal
from AlgoTradeKit.trader import TraderConfig, run_live

HOUR_MS = 3_600_000
BAND = 0.0002          # a zone is ±0.02 % around its price
TALLEST = 0.002        # a zone the user makes taller than 0.2 % is refused
KEEP_HOURS = 4         # older zones are erased


class Breakout(BaseStrategy):
    """Each hour's high and low as keyed zones; a close through one trades."""

    primary_timeframe = "1m"
    warmup_period = 60
    chart_actions = ("Add zone here",)
    chart_drawing_actions = ("Make it a zone",)

    def prepare_indicators(self, data):
        return data

    def setup(self, data):
        self.zones: dict[str, dict] = {}         # key -> {"side", "top", "bottom", "made"}
        self.last_close = 0.0
        # Edits saved by earlier runs, applied as each zone is made again
        self.gone = {e["key"] for e in self.chart_edits if e["type"] == "delete"}
        self.moved = {e["key"]: e["drawing"] for e in self.chart_edits if e["type"] == "edit"}
        self.added = [e for e in self.chart_edits if e["type"] == "action"]

    # ── zones ────────────────────────────────────────────────────────────────

    def _make(self, key, side, price, made, top=None, bottom=None):
        if key in self.gone:
            return
        if top is None:
            top, bottom = price * (1 + BAND), price * (1 - BAND)
        if key in self.moved:
            prices = (self.moved[key]["price1"], self.moved[key]["price2"])
            top, bottom = max(prices), min(prices)
        self.zones[key] = {"side": side, "top": top, "bottom": bottom, "made": made}
        colour = "#3fb950" if side == "high" else "#f85149"
        # No right edge while it can trade: it runs to the live edge (time2 None)
        self.draw({"type": "box", "key": key, "title": f"{side} zone {price:,.2f}",
                   "time1": made // 1000, "time2": None,
                   "price1": top, "price2": bottom, "color": colour, "borderColor": colour,
                   "opacity": 0.15, "label": f"{side} zone", "editable": ("price",)})

    def _add_here(self, price, made):
        """«Add zone here»: a high zone above the last close, a low one below it."""
        side = "high" if price > self.last_close else "low"
        self._make(f"zone:added:{made}", side, price, made)

    def _from_box(self, box, made):
        """«Make it a zone»: the viewer's box, from its left edge."""
        top, bottom = max(box["price1"], box["price2"]), min(box["price1"], box["price2"])
        side = "high" if bottom > self.last_close else "low"
        self._make(f"zone:drawn:{made}", side, (top + bottom) / 2, made, top, bottom)

    def _forget(self, key):
        self.zones.pop(key, None)
        self.erase(key)

    def generate_signals(self, i, data):
        frame = data[self.primary_timeframe]
        row = frame.iloc[i]
        ts, close = int(row["timestamp"]), float(row["close"])
        self.last_close = close
        if ts % HOUR_MS == 0:                      # a new hour: the last hour's high and low
            hour = frame.iloc[i - 60:i]
            made = ts - HOUR_MS
            self._make(f"zone:high:{made}", "high", float(hour["high"].max()), made)
            self._make(f"zone:low:{made}", "low", float(hour["low"].min()), made)
        for added in [a for a in self.added if a["time"] <= ts]:   # saved ones, on time
            self.added.remove(added)
            if added["label"] == "Make it a zone" and added.get("drawing"):
                self._from_box(added["drawing"], added["time"])
            elif added["label"] == "Add zone here" and added["price"] is not None:
                self._add_here(added["price"], added["time"])
        signals = []
        for key, zone in list(self.zones.items()):
            if ts - zone["made"] > KEEP_HOURS * HOUR_MS:
                self._forget(key)                    # too old to trade
            elif zone["side"] == "high" and close > zone["top"]:
                signals.append(Signal("long", close, zone["bottom"], None, ts, i, "1m"))
                self._forget(key)
            elif zone["side"] == "low" and close < zone["bottom"]:
                signals.append(Signal("short", close, zone["top"], None, ts, i, "1m"))
                self._forget(key)
        return signals

    # ── what the user does on the chart ──────────────────────────────────────

    def on_chart_edit(self, edit):
        if edit.after is None:
            self.zones.pop(edit.key, None)
            print(f"  [strategy] {edit.key} deleted: no trade will come from it")
            return None
        prices = (edit.after["price1"], edit.after["price2"])
        top, bottom = max(prices), min(prices)
        if (top - bottom) / bottom > TALLEST:
            print(f"  [strategy] {edit.key} refused: {top - bottom:,.2f} is too tall")
            return False
        if edit.key in self.zones:
            self.zones[edit.key].update(top=top, bottom=bottom)
        print(f"  [strategy] {edit.key} now trades {bottom:,.2f} – {top:,.2f}")
        return None

    def on_chart_action(self, action):
        if action.label == "Add zone here" and action.price is not None:
            self._add_here(action.price, action.time)
            print(f"  [strategy] zone added at {action.price:,.2f}")
        elif action.label == "Make it a zone" and action.drawing is not None:
            if action.drawing.get("type") != "box":
                print("  [strategy] Make it a zone: draw a box")
                return
            self._from_box(action.drawing, action.time)
            action.chart.remove_drawing(action.drawing["id"])   # your box became the zone
            print(f"  [strategy] your box is now zone:drawn:{action.time}")


if __name__ == "__main__":
    # The edit key outlives a re-run: the program starts again with this environment.
    key = os.environ.setdefault("ATK_EDIT_KEY", os.urandom(6).hex())
    edits = os.path.abspath("chart_edits.json")
    port = 8710
    print(f"  edit key   {key}")
    print(f"  edits file {edits}")
    print(f"  chart      http://127.0.0.1:{port}/?key={key}")
    run_live(
        strategy=Breakout(),
        broker=Broker("binance-spot"),            # public market data, no API key
        config=TraderConfig(
            symbol="BTCUSDT", min_candles=120,
            # A stop ~0.04 % away risks 1 % of the paper wallet with a big position:
            # leverage 50 lets it open (at leverage 1 nothing would).
            leverage=50.0, tp_mode="fixed_rr", tp_rr=2.0,
            display=True, display_candles=600, display_open_browser=False,
            chart_port=port, chart_edit_key=key, chart_edits_path=edits,
        ),
    )
