"""
Tests for v1.3.0 Part 3 — edits on the chart reach the strategy.

A strategy draws its own zones (``self.draw``) and hears what the user does to them
(``on_chart_edit``, ``on_chart_action``) between candles, under the session's step
lock.  What it accepts is saved to ``chart_edits_path`` and read back before
``setup()`` at the next start; «Re-run with edits» starts the program again.  The
page side is Part 2's: here a page's message goes through the chart's own handler,
as a page's socket hands it over.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading

import pandas as pd
import pytest
from test_trader import (
    _p_candle,
    _p_flat,
    _p_ts,
    _PaperBroker,
    _PaperStream,
    _t_cfg,
    _t_trader,
)

import AlgoTradeKit.simulate._live as live_module
from AlgoTradeKit.strategy import BaseStrategy, Signal
from AlgoTradeKit.trader import TraderConfig, run_live
from AlgoTradeKit.trader._chart_edits import (
    RERUN_LABEL,
    ChartBridge,
    EditsFile,
    load_chart_edits,
)
from AlgoTradeKit.visual import Chart
from AlgoTradeKit.visual._edits import drawing_key
from AlgoTradeKit.visual.indicator_renderer import erase_strategy_drawing, put_strategy_drawing
from AlgoTradeKit.visual.server import _find_free_port

#: A paper run without a display or a log warns that it is silent; these mean it.
pytestmark = pytest.mark.filterwarnings("ignore:.*silent paper run")

ZONE = "zone:a"
#: Ten flat candles at 100, then the live ones: 8, 9 and 10 stay below the zone,
#: 11 closes at 102, above its top — the breakout at N+3 when N is candle 8.
SEED = _p_flat(8)
LIVE = [*_p_flat(3, start=8), _p_candle(11, 100.0, 102.5, 99.8, 102.0),
        _p_candle(12, 102.0, 102.5, 101.5, 102.0)]
ZONES = {_p_ts(5): (ZONE, 101.0, 100.6)}


class Zones(BaseStrategy):
    """High zones made at given candles: a close above one's top goes long, stop at
    its bottom, and the zone is erased.  It applies the saved edits as each zone is
    made, and the user's as they come."""

    primary_timeframe = "1h"

    def __init__(self, zones=None, during=None, watch=None, actions=()):
        self.zones = dict(zones or {})          # candle opening -> (key, top, bottom)
        self.during = dict(during or {})        # candle opening -> fn(self), inside the step
        self.chart_actions = tuple(actions)
        self.heard: list = []
        self.trades: list = []
        if watch is not None:                   # before any session exists
            self.watch_chart(watch, actions=("Mark",))

    def prepare_indicators(self, data):
        return data

    def setup(self, data):
        self.at_setup = tuple(self.chart_edits)
        self.live: dict = {}
        self.gone = {r["key"] for r in self.chart_edits if r.get("type") == "delete"}
        self.moved = {r["key"]: r["drawing"] for r in self.chart_edits if r.get("type") == "edit"}
        self.in_step = False
        self.steps = 0

    def generate_signals(self, i, data):
        self.in_step = True
        try:
            row = data["1h"].iloc[i]
            ts = int(row["timestamp"])
            if ts in self.during:
                self.during[ts](self)
            if ts in self.zones:
                key, top, bottom = self.zones[ts]
                if key not in self.gone:
                    if key in self.moved:
                        prices = (self.moved[key]["price1"], self.moved[key]["price2"])
                        top, bottom = max(prices), min(prices)
                    self.live[key] = (top, bottom)
                    self.draw({"type": "box", "key": key, "time1": ts // 1000,
                               "time2": ts // 1000 + 4 * 3600, "price1": top, "price2": bottom,
                               "editable": ["price"], "title": key})
            signals = []
            for key, (top, bottom) in list(self.live.items()):
                if float(row["close"]) > top:
                    signals.append(Signal("long", float(row["close"]), bottom, None, ts, i, "1h"))
                    self.trades.append((key, ts))
                    del self.live[key]
                    self.erase(key)
            return signals
        finally:
            self.in_step = False
            self.steps += 1

    def on_chart_edit(self, edit):
        self.heard.append((edit.key, edit.after is None, self.in_step, self.steps))
        if edit.after is None:
            self.live.pop(edit.key, None)
            return None
        prices = (edit.after["price1"], edit.after["price2"])
        if max(prices) - min(prices) > 5:
            return False                                    # too tall
        self.live[edit.key] = (max(prices), min(prices))
        return None

    def on_chart_action(self, action):
        self.heard.append(("action", action.label, action.time, self.in_step))


class Stepped(_PaperBroker):
    """Live candles one at a time; ``after(index, row)`` runs between them, as the
    time between two candles of a real feed."""

    def __init__(self, rows, live_rows, after=None):
        super().__init__(rows, live_rows=live_rows)
        self.after = after

    def stream_candles(self, symbol, timeframe, on_candle, *, closed_only=True):
        self.calls.append(("stream", symbol, timeframe, closed_only))
        for index, row in enumerate(self.live_rows):
            on_candle({**row, "closed": True})
            if self.after is not None:
                self.after(index, row)
        stream = _PaperStream(alive=False)
        self.streams.append(stream)
        return stream


def config(**overrides) -> TraderConfig:
    kwargs = {"symbol": "BTCUSDT", "min_candles": 5, "leverage": 10.0, "log_events": False,
              "display": True, "display_candles": 8, "display_open_browser": False}
    kwargs.update(overrides)
    return TraderConfig(**kwargs)


@pytest.fixture
def built(monkeypatch):
    """The charts run_live builds, as it builds them; no keep-alive at the end."""
    charts: list[Chart] = []
    real = live_module.LiveSimulation._build_chart

    def capture(self, report, result):
        real(self, report, result)
        charts.append(self.chart)

    monkeypatch.setattr(live_module.LiveSimulation, "_build_chart", capture)
    monkeypatch.setattr(live_module, "_register_keep_alive", lambda: None)
    yield charts
    for chart in charts:
        chart.stop()


def zone_id(chart: Chart, key: str = ZONE) -> str:
    (found,) = [d.id for d in chart._drawings if drawing_key(d) == key]
    return found


def page_says(chart: Chart, msg: dict) -> None:
    """A page's message, as its socket hands it over; then wait for the hooks."""
    chart._handle_browser_message(msg)
    assert chart._events.wait_idle(10)


def delete_zone(built):
    def after(index, row):
        if index == 0:                       # between candle 8 (N) and candle 9
            chart = built[-1]                # this run's chart
            page_says(chart, {"type": "drawing_deleted", "id": zone_id(chart)})
    return after


# ===========================================================================
# The strategy draws (Q3.3 a)
# ===========================================================================

class TestStrategyDraws:
    def test_run_returns_what_the_strategy_drew(self):
        frame = pd.DataFrame(SEED + LIVE)
        result = Zones(ZONES).run({"1h": frame})
        assert result.drawings == []                        # drawn, then erased on its breakout
        result = Zones(ZONES).run({"1h": pd.DataFrame(SEED)})
        (zone,) = result.drawings
        assert (zone["key"], zone["price1"], zone["price2"]) == (ZONE, 101.0, 100.6)

    def test_drawing_again_with_a_key_replaces_it(self):
        strategy = Zones()
        strategy.__dict__["_atk_drawn"] = []
        strategy.draw({"type": "hline", "price": 1.0, "key": "k"})
        strategy.draw({"type": "hline", "price": 2.0})
        strategy.draw({"type": "hline", "price": 3.0, "key": "k"})
        assert [d["price"] for d in strategy.__dict__["_atk_drawn"]] == [3.0, 2.0]
        strategy.erase("k")
        assert [d["price"] for d in strategy.__dict__["_atk_drawn"]] == [2.0]
        with pytest.raises(TypeError, match="draw takes a dict"):
            strategy.draw(["hline"])

    def test_outside_a_run_and_without_a_chart_nothing_happens(self):
        strategy = Zones()
        strategy.draw({"type": "hline", "price": 1.0})      # no run, no session: dropped
        strategy.erase("k")
        assert strategy.__dict__.get("_atk_drawn") is None

    def test_the_session_chart_shows_the_seed_and_the_live_drawings(self, built):
        made = {**ZONES, _p_ts(9): ("zone:b", 105.0, 104.0)}
        run_live(strategy=Zones(made), broker=Stepped(SEED, LIVE), config=config())
        (chart,) = built
        keys = [drawing_key(d) for d in chart._drawings if drawing_key(d)]
        assert keys == ["zone:b"]              # zone:a drawn at the seed, erased at its breakout
        assert [d["key"] for d in chart._build_init_payload()["drawings"]
                if d.get("key")] == ["zone:b"]


def test_put_and_erase_by_key():
    chart = Chart(title="x")
    first = put_strategy_drawing(chart, {"type": "hline", "price": 1.0, "key": "level"})
    again = put_strategy_drawing(chart, {"type": "hline", "price": 2.0, "key": "level"})
    other = put_strategy_drawing(chart, {"type": "hline", "price": 3.0})
    assert first == again and first.startswith("drawn_") and other != first
    assert [d.to_dict()["price"] for d in chart._drawings] == [2.0, 3.0]
    erase_strategy_drawing(chart, "level")
    erase_strategy_drawing(chart, "unknown")
    assert [d.to_dict()["price"] for d in chart._drawings] == [3.0]


# ===========================================================================
# The hooks, between candles (Q3.1 c, the «a» half)
# ===========================================================================

class TestHooks:
    def test_q3_1_a_a_deleted_zone_skips_the_breakout_a_run_without_the_delete_takes(
            self, built):
        kept = Zones(ZONES)
        run_live(strategy=kept, broker=Stepped(SEED, LIVE), config=config())
        assert kept.trades == [(ZONE, _p_ts(11))]

        deleted = Zones(ZONES)
        report = run_live(strategy=deleted, broker=Stepped(SEED, LIVE, delete_zone(built)),
                          config=config())
        assert deleted.trades == []
        assert deleted.heard == [(ZONE, True, False, 9)]   # after candle 8's step
        assert report.open_at_end == [] and report.total_trades == 0
        assert all(drawing_key(d) != ZONE for d in built[1]._drawings)

    def test_an_edit_during_a_step_is_heard_after_that_step(self, built):
        def from_inside(strategy):                 # the page, while candle 9 is stepped
            chart = built[0]
            chart._handle_browser_message({"type": "drawing_deleted", "id": zone_id(chart)})

        def after(index, row):
            if index == 1:
                assert built[0]._events.wait_idle(10)

        strategy = Zones(ZONES, during={_p_ts(9): from_inside})
        run_live(strategy=strategy, broker=Stepped(SEED, LIVE, after), config=config())
        assert strategy.heard == [(ZONE, True, False, 10)]  # candle 9's step done, not inside
        assert strategy.trades == []

    def test_false_refuses_and_the_zone_goes_back(self, built, tmp_path):
        path = tmp_path / "edits.json"

        def after(index, row):
            if index == 0:
                chart = built[0]
                zone = next(d for d in chart._drawings if drawing_key(d) == ZONE)
                tall = {**zone.to_dict(), "price1": 110.0}
                page_says(chart, {"type": "drawing_updated", "drawing": tall})
                assert zone.to_dict()["price1"] == 101.0          # put back

        strategy = Zones(ZONES)
        run_live(strategy=strategy, broker=Stepped(SEED, LIVE, after),
                 config=config(chart_edits_path=str(path)))
        assert strategy.heard == [(ZONE, False, False, 9)]
        assert strategy.trades == [(ZONE, _p_ts(11))]           # the zone traded as it was
        assert not path.exists()                                 # nothing accepted, nothing saved

    def test_actions_reach_the_strategy(self, built):
        def after(index, row):
            if index == 0:
                page_says(built[0], {"type": "chart_action", "label": "Add zone here",
                                     "time": _p_ts(8) // 1000, "price": 101.5})

        strategy = Zones(ZONES, actions=("Add zone here",))
        run_live(strategy=strategy, broker=Stepped(SEED, LIVE, after), config=config())
        assert built[0].actions == ["Add zone here"]
        assert strategy.heard == [("action", "Add zone here", _p_ts(8), False)]

    def test_watch_chart_before_and_during_the_session(self):
        own, later = Chart(title="Own"), Chart(title="Later")
        later.add_box(1, 101.0, 2, 100.0, key="later:1")

        def after(index, row):
            if index == 0:
                page_says(own, {"type": "chart_action", "label": "Mark", "time": 7200,
                                "price": 1.0})
            if index == 2:
                box = later._drawings[0]
                page_says(later, {"type": "drawing_updated",
                                  "drawing": {**box.to_dict(), "price1": 102.0}})

        strategy = Zones(ZONES, watch=own, during={_p_ts(9): lambda s: s.watch_chart(later)})
        run_live(strategy=strategy, broker=Stepped(SEED, LIVE, after),
                 config=config(display=False, display_candles=None))
        assert own.actions == ["Mark"]
        # a seed of min_candles=5 steps, then candles 8, 9 and 10
        assert strategy.heard == [("action", "Mark", 7_200_000, False),
                                  ("later:1", False, False, 8)]

    def test_a_chart_outside_any_session_says_nobody_hears(self, capsys):
        chart = Chart(title="Backtest")
        chart.add_box(1, 101.0, 2, 100.0, key="zone:1")
        chart.add_box(1, 102.0, 2, 101.0, key="zone:2")
        for drawing in list(chart._drawings):
            chart._handle_browser_message({"type": "drawing_deleted", "id": drawing.id})
        assert capsys.readouterr().out.splitlines() == [
            "[AlgoTradeKit] chart: edit of 'zone:1' — no live session, nothing told"]


# ===========================================================================
# Saved edits (Q3.2 b) and the «b» half of Q3.1
# ===========================================================================

class TestSavedEdits:
    def test_records_of_what_was_accepted(self, built, tmp_path):
        path = tmp_path / "edits.json"

        def after(index, row):
            chart = built[0]
            if index == 0:
                zone = next(d for d in chart._drawings if drawing_key(d) == ZONE)
                page_says(chart, {"type": "drawing_updated",
                                  "drawing": {**zone.to_dict(), "price1": 101.5}})
            elif index == 1:
                page_says(chart, {"type": "chart_action", "label": "Add zone here",
                                  "time": _p_ts(9) // 1000, "price": 100.25})
            elif index == 2:
                page_says(chart, {"type": "drawing_deleted", "id": zone_id(chart)})

        run_live(strategy=Zones(ZONES, actions=("Add zone here",)),
                 broker=Stepped(SEED, LIVE, after),
                 config=config(chart_edits_path=str(path)))
        edit, action, delete = json.loads(path.read_text())
        for record in (edit, action, delete):
            assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", record.pop("saved"))
        assert edit["type"] == "edit" and edit["chart"].startswith("BTCUSDT")
        assert (edit["key"], edit["drawing"]["price1"]) == (ZONE, 101.5)
        assert action == {"type": "action", "chart": edit["chart"], "label": "Add zone here",
                          "time": _p_ts(9), "price": 100.25}
        assert delete == {"type": "delete", "chart": edit["chart"], "key": ZONE}
        assert [p.name for p in tmp_path.iterdir()] == ["edits.json"]   # no temporary left

    def test_q3_1_b_and_q3_2_b_the_next_start_reads_them_and_its_seed_has_no_trade(
            self, built, tmp_path):
        path = tmp_path / "edits.json"
        run_live(strategy=Zones(ZONES), broker=Stepped(SEED, LIVE[:1], delete_zone(built)),
                 config=config(chart_edits_path=str(path)))
        saved = json.loads(path.read_text())

        history = SEED + LIVE                       # the next start's seed holds the breakout
        again = Zones(ZONES)
        report = run_live(strategy=again, broker=Stepped(history, []),
                          config=config(chart_edits_path=str(path), display_candles=13))
        assert again.at_setup == tuple(saved)       # there before setup()
        assert again.trades == [] and report.total_trades == 0

        control = Zones(ZONES)
        run_live(strategy=control, broker=Stepped(history, []),
                 config=config(display_candles=13))
        assert control.trades == [(ZONE, _p_ts(11))]

    @pytest.mark.parametrize(("content", "why"), [
        ("{", "Expecting property name enclosed in double quotes: line 1 column 2 (char 1)"),
        ('{"type": "edit"}', "it holds a dict"),
        ('[{"type": "edit"}, 3]', "item 2 is not a record"),
    ])
    def test_a_bad_file_stops_the_start(self, tmp_path, content, why):
        path = tmp_path / "edits.json"
        path.write_text(content)
        broker = Stepped(SEED, LIVE)
        with pytest.raises(ValueError) as raised:
            run_live(strategy=Zones(ZONES), broker=broker,
                     config=config(chart_edits_path=str(path), display=False,
                                   display_candles=None))
        assert str(raised.value) == f"chart_edits_path: {path} is not a list of edits — {why}"
        assert broker.calls == []                   # the venue was asked nothing

    def test_a_crash_while_writing_leaves_the_old_file(self, tmp_path, monkeypatch):
        path = tmp_path / "edits.json"
        edits = EditsFile(str(path), load_chart_edits(str(path)))
        edits.append({"type": "delete", "chart": "c", "key": "a"})
        before = path.read_text()

        def crash(source, target):
            raise OSError("disk gone")

        monkeypatch.setattr(os, "replace", crash)
        with pytest.raises(OSError, match="disk gone"):
            edits.append({"type": "delete", "chart": "c", "key": "b"})
        assert path.read_text() == before
        assert [p.name for p in tmp_path.iterdir()] == ["edits.json"]


# ===========================================================================
# «Re-run with edits»
# ===========================================================================

class TestRerun:
    def test_the_button_stops_the_session_and_starts_the_program_again(
            self, built, tmp_path, monkeypatch, capsys):
        calls = []
        monkeypatch.setattr(os, "execv", lambda path, argv: calls.append(
            (path, list(argv), capsys.readouterr().out)))

        def after(index, row):
            if index == 0:
                page_says(built[0], {"type": "button", "label": RERUN_LABEL})

        run_live(strategy=Zones(ZONES), broker=Stepped(SEED, LIVE, after),
                 config=config(chart_edits_path=str(tmp_path / "edits.json"),
                               chart_port=_find_free_port(), log_events=True))
        assert list(built[0]._buttons) == [RERUN_LABEL]
        ((path, argv, out_before),) = calls
        assert (path, argv) == (sys.executable, [sys.executable, *sys.orig_argv[1:]])
        assert "STOP re-run with chart edits" in out_before     # the stop message came first

    def test_not_offered_without_a_fixed_port(self, built, tmp_path, capsys):
        run_live(strategy=Zones(ZONES), broker=Stepped(SEED, LIVE),
                 config=config(chart_edits_path=str(tmp_path / "edits.json")))
        assert built[0]._buttons == {}
        assert ("[AlgoTradeKit] run_live BTCUSDT: «Re-run with edits» is not offered — "
                "chart_port is 0, and a re-run is found only on a fixed port."
                in capsys.readouterr().out)

    def test_the_editors_address_carries_the_key(self, built, capsys):
        run_live(strategy=Zones(ZONES), broker=Stepped(SEED, LIVE),
                 config=config(chart_edit_key="s3cret"))
        assert f"BTCUSDT chart  → {built[0].url}/?key=s3cret" in capsys.readouterr().out
        assert built[0]._edit_key == "s3cret"


# ===========================================================================
# The Trader
# ===========================================================================

class TestTrader:
    def test_its_hooks_run_under_the_step_lock_between_items(self, tmp_path):
        own = Chart(title="Own")
        own.add_box(1, 101.0, 2, 100.0, key="zone:t")
        released = threading.Event()

        class Watching(Zones):
            def on_chart_edit(self, edit):
                self.heard.append((worker.step_lock.locked(), released.is_set()))

        strategy = Watching()
        strategy.watch_chart(own)
        t = _t_trader(cfg=_t_cfg(chart_edits_path=str(tmp_path / "edits.json")),
                      strategy=strategy, state_path=tmp_path / "state.json")
        worker = t._worker
        worker.seed()
        worker._wire_chart_edits()
        box = own._drawings[0]
        with worker.step_lock:                  # an item in progress
            own._handle_browser_message({"type": "drawing_updated",
                                         "drawing": {**box.to_dict(), "price1": 101.5}})
            released.set()
        assert own._events.wait_idle(10)
        assert strategy.heard == [(True, True)]   # with the lock, once the item let go
        (record,) = json.loads((tmp_path / "edits.json").read_text())
        assert (record["type"], record["key"]) == ("edit", "zone:t")

    def test_saved_edits_reach_the_strategy_and_its_display_copy(self, tmp_path):
        path = tmp_path / "edits.json"
        path.write_text(json.dumps([{"saved": "x", "type": "delete", "chart": "c", "key": "a"}]))
        strategy = Zones()
        t = _t_trader(cfg=_t_cfg(chart_edits_path=str(path), display=True,
                                 display_candles=10), strategy=strategy,
                      state_path=tmp_path / "state.json")
        copy_ = t._worker.display._sim_strategy
        assert strategy.chart_edits == copy_.chart_edits == tuple(json.loads(path.read_text()))

    def test_real_mode_draws_go_through_the_display_queue(self, tmp_path):
        t = _t_trader(cfg=_t_cfg(display=True, display_candles=10, display_trades="real"),
                      state_path=tmp_path / "state.json")
        worker, display = t._worker, t._worker.display
        display._real_chart = Chart(title="real")
        worker._wire_chart_edits()
        assert worker._draw_target == display.enqueue_draw
        worker._draw_target("draw", {"type": "hline", "price": 1.0, "key": "level"})
        assert display.real_chart._drawings == []               # queued, not drawn yet
        kind, payload = display._queue.get_nowait()
        display._process(kind, payload, backlog=False)
        assert [drawing_key(d) for d in display.real_chart._drawings] == ["level"]


def test_the_copy_follows_only_what_the_strategy_accepted():
    chart = Chart(title="c")
    chart.add_box(1, 101.0, 2, 100.0, key="k")
    box = chart._drawings[0]
    main, copy_ = Zones(), Zones()
    for strategy in (main, copy_):
        strategy.setup({})
    bridge = ChartBridge(main, threading.Lock(), followers=[(copy_, threading.Lock())])
    bridge.wire(chart)
    page_says(chart, {"type": "drawing_updated", "drawing": {**box.to_dict(), "price1": 102.0}})
    page_says(chart, {"type": "drawing_updated", "drawing": {**box.to_dict(), "price1": 130.0}})
    assert [h[0] for h in main.heard] == ["k", "k"]
    assert [h[0] for h in copy_.heard] == ["k"]                  # the refused one never came
