"""
Tests for v1.3.0 Part 4c — the chart page's tools, Python's half.

Time zones: the zone a chart shows its times in and the zone its candles are
stamped in, set by the program (``Chart(timezone=, source_timezone=)``,
``TraderConfig(chart_timezone=, chart_source_timezone=)``) and read by the
pictures.  A position box's planned RR.  A drawing that runs to the live edge
(``time2=None``).  The menu on a drawing the viewer drew
(``Chart.add_drawing_action``, ``BaseStrategy.chart_drawing_actions``).
"""
from __future__ import annotations

import json
import re
from urllib.parse import parse_qs

import pandas as pd
import pytest
from test_chart_edits import ZONES, Stepped, Zones, built, config  # noqa: F401
from test_trader import _p_flat

from AlgoTradeKit.trader import TraderConfig, run_live
from AlgoTradeKit.visual import Chart, ChartAction, EditRefused
from AlgoTradeKit.visual.chart import _local, _span_text

#: 2026-10-30 22:45 as MetaTrader stamps it (the broker's clock, written as UTC):
#: 20:45 UTC, 00:15 on 10-31 in Tehran (UTC+3:30).
STAMP = 1_793_400_300


def _frame(n: int = 12, start: int = STAMP - 11 * 300) -> pd.DataFrame:
    stamps = [(start + i * 300) * 1000 for i in range(n)]
    return pd.DataFrame({"timestamp": stamps, "open": 100.0, "high": 101.0, "low": 99.0,
                         "close": 100.5, "volume": 1.0})


class TestTimeZones:
    def test_a_chart_takes_its_zones_and_sends_them_in_its_init(self):
        chart = Chart(title="c", timezone="Asia/Tehran", source_timezone="Europe/Athens")
        chart.set_data(_frame())
        init = chart._build_init_payload()
        assert (chart.timezone, chart.source_timezone) == ("Asia/Tehran", "Europe/Athens")
        assert (init["timezone"], init["sourceTimezone"]) == ("Asia/Tehran", "Europe/Athens")
        plain = Chart(title="p")
        assert (plain.timezone, plain.source_timezone) == (None, None)

    @pytest.mark.parametrize("field", ["timezone", "source_timezone"])
    def test_an_unknown_zone_is_refused(self, field):
        with pytest.raises(ValueError, match=f"{field}: unknown time zone 'Mars/Olympus'"):
            Chart(**{field: "Mars/Olympus"})

    def test_a_stamp_is_its_source_zones_wall_clock(self):
        from zoneinfo import ZoneInfo

        tehran = ZoneInfo("Asia/Tehran")
        assert f"{_local(STAMP, tehran, 'Europe/Athens'):%Y-%m-%d %H:%M}" == "2026-10-31 00:15"
        assert f"{_local(STAMP, None, 'Europe/Athens'):%H:%M}" == "20:45"     # in UTC
        assert f"{_local(STAMP, tehran):%Y-%m-%d %H:%M}" == "2026-10-31 02:15"  # read as UTC
        assert _span_text(STAMP, STAMP, tehran, "Europe/Athens") == (
            "2026-10-31 00:15 – 00:15 (UTC+03:30)")

    def test_a_picture_reads_the_charts_source_zone(self):
        chart = Chart(title="XAU", source_timezone="Europe/Athens")
        chart.set_data(_frame())
        query = parse_qs(chart._snapshot_query(timezone="Asia/Tehran"))
        assert query["tz"] == ["Asia/Tehran"] and query["stz"] == ["Europe/Athens"]
        assert query["title"][0].endswith("2026-10-31 00:15 (UTC+03:30)")
        plain = Chart(title="BTC")
        plain.set_data(_frame())
        assert "stz" not in parse_qs(plain._snapshot_query(timezone="Asia/Tehran"))

    def test_the_trader_config_takes_them_and_a_paper_session_hands_them_on(
            self, built):  # noqa: F811 — the fixture
        with pytest.raises(ValueError, match="TraderConfig.chart_source_timezone: unknown"):
            TraderConfig(symbol="BTCUSDT", min_candles=5, chart_source_timezone="Nowhere/Else")
        run_live(strategy=Zones(ZONES), broker=Stepped(_p_flat(8), _p_flat(2, start=8)),
                 config=config(chart_timezone="Asia/Tehran",
                               chart_source_timezone="Europe/Athens"))
        assert (built[0].timezone, built[0].source_timezone) == ("Asia/Tehran", "Europe/Athens")


class TestPlannedRR:
    def test_a_trade_box_says_the_rr_it_was_opened_with(self):
        chart = Chart(title="c")
        chart.add_position_box(open_time=1, close_time=2, entry_price=100.0, stop_loss=99.0,
                               take_profit=104.0, direction="long", net_pnl=3.0,
                               close_reason="tp", rr_ratio=3.0, planned_tp=102.0)
        box = chart._drawings[0].to_dict()
        assert box["label"] == "✓ TP  +3.00R"           # its result
        assert box["planned_label"] == "L  1:2.0R"      # what a replay shows while it runs
        chart.add_position_box(open_time=1, close_time=None, entry_price=100.0, stop_loss=101.0,
                               take_profit=97.0, direction="short", net_pnl=0.0)
        assert chart._drawings[1].to_dict()["planned_label"] == "S  1:3.0R"

    def test_simulation_positions_give_the_first_tp(self):
        """A trailing trade's box shows the peak it chased as its TP; a replay still
        says the RR the trade was opened with."""
        from types import SimpleNamespace

        from AlgoTradeKit.visual.indicator_renderer import draw_trade_group

        marker = {"trade_id": 1, "direction": "long", "open_time": 60_000,
                  "close_time": 600_000, "entry_price": 100.0, "initial_stop_loss": 99.0,
                  "take_profit": 102.0, "exit_price": 103.0, "net_pnl": 3.0, "pnl_r": 3.0,
                  "close_reason": "sl", "sl_history": [{"time": 60_000, "sl": 99.0},
                                                       {"time": 300_000, "sl": 101.5}],
                  "peak_price": 104.0, "final_next_tp": None}
        chart = Chart(title="c")
        draw_trade_group(chart, [marker], config=SimpleNamespace(sl_mode="trailing",
                                                                 tp_mode="signal"))
        boxes = [d.to_dict() for d in chart._drawings if d.to_dict()["type"] == "position_box"]
        assert boxes[0]["visual_tp"] == 104.0              # the peak it chased
        assert boxes[0]["planned_label"] == "L  1:2.0R"


class TestRunningEnds:
    def test_a_box_may_run_to_the_live_edge_and_is_never_trimmed(self):
        chart = Chart(title="c")
        chart.set_data(_frame(40, start=STAMP - 39 * 300))
        chart.add_box(STAMP - 30 * 300, 101.0, None, 99.0, key="zone:a")
        drawing = chart._drawings[0]
        assert drawing.to_dict()["time2"] is None
        assert chart._drawing_end_time(drawing) is None
        chart.set_candle_limit(5)                       # trims the old candles, not it
        assert [d.id for d in chart._drawings] == [drawing.id]
        assert Chart._drawing_end_time({"type": "position_box", "open_time": 1,
                                        "close_time": None}) is None


class TestDrawingActions:
    def test_a_viewers_drawing_offers_the_programs_items(self):
        chart = Chart(title="c", edit_key="k")
        heard = []
        chart.add_drawing_action("Make it a zone", heard.append)
        assert chart.drawing_actions == ["Make it a zone"]
        with pytest.raises(ValueError, match="already on this chart"):
            chart.add_drawing_action("Make it a zone", heard.append)
        mine = {"type": "box", "id": "box_1", "time1": 1700, "time2": 2900, "price1": 90,
                "price2": 80, "source": "user"}
        chart._handle_browser_message({"type": "drawing_action", "label": "Make it a zone",
                                       "drawing": mine, "_editor": False})   # no key: nothing
        chart._handle_browser_message({"type": "drawing_action", "label": "Make it a zone",
                                       "drawing": mine})
        assert chart._events.wait_idle(10)
        (action,) = heard
        assert action == ChartAction(chart=chart, label="Make it a zone", time=1_700_000,
                                     price=None, drawing=mine)
        chart.remove_drawing_action("Make it a zone")
        assert chart.drawing_actions == []

    def test_a_strategys_items_reach_it_with_the_drawing_and_are_saved(
            self, built, tmp_path):  # noqa: F811 — the fixture
        path = tmp_path / "edits.json"

        class Making(Zones):
            chart_drawing_actions = ("Make it a zone",)

            def on_chart_action(self, action):
                self.heard.append((action.label, (action.drawing or {}).get("id")))

        strategy = Making(ZONES)
        mine = {"type": "box", "id": "box_7", "time1": 1700000000, "time2": 1700003600,
                "price1": 101.0, "price2": 100.5, "source": "user"}

        def after(index, row):
            if index == 0:
                chart = built[0]
                assert chart.drawing_actions == ["Make it a zone"]
                chart._handle_browser_message({"type": "drawing_action",
                                               "label": "Make it a zone", "drawing": mine})
                assert chart._events.wait_idle(10)

        run_live(strategy=strategy, broker=Stepped(_p_flat(8), _p_flat(2, start=8), after),
                 config=config(chart_edits_path=str(path)))
        assert ("Make it a zone", "box_7") in strategy.heard
        (record,) = json.loads(path.read_text())
        assert record["type"] == "action" and record["drawing"]["id"] == "box_7"
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", record["saved"])

    def test_a_watched_chart_takes_drawing_actions_too(self):
        from AlgoTradeKit.strategy import BaseStrategy

        class Plain(BaseStrategy):
            def prepare_indicators(self, data):
                return data

            def generate_signals(self, i, data):
                return []

        own = Chart(title="own")
        strategy = Plain()
        strategy.watch_chart(own, actions=("Mark",), drawing_actions=("Make it",))
        strategy.watch_chart(own, drawing_actions=("Make it", "Other"))
        assert strategy.__dict__["_atk_watched"] == [(own, ("Mark",), ("Make it", "Other"))]
        heard = []
        strategy._watch_hook = lambda chart, actions: heard.append((chart, actions))
        strategy.watch_chart(own, actions=("Mark",))       # a two-argument hook still works
        assert heard == [(own, ("Mark",))]


class TestRefusalsAndPictures:
    """v1.3.0 Part 4e: what a program refuses on the page says why; a picture shows
    the chart's own zone."""

    @staticmethod
    def _sent(chart: Chart) -> list:
        sent: list = []
        chart._server = type("S", (), {"send": staticmethod(sent.append),
                                       "_last_init": None})()
        chart._shown = True
        return sent

    def test_a_refused_drawing_action_says_why_on_the_page(self):
        chart = Chart(title="c")
        sent = self._sent(chart)

        def refuse(action):
            raise EditRefused("a POI starts on a candle")

        chart.add_drawing_action("Make it a zone", refuse)
        chart._handle_browser_message({"type": "drawing_action", "label": "Make it a zone",
                                       "drawing": {"type": "box", "id": "box_3", "time1": 60}})
        assert chart._events.wait_idle(10)
        assert {"type": "edit_refused", "id": "box_3",
                "message": "a POI starts on a candle"} in sent

    def test_a_refused_candle_action_says_why_too(self):
        chart = Chart(title="c")
        sent = self._sent(chart)

        def refuse(action):
            raise EditRefused("not on this candle")

        chart.add_action("Mark here", refuse)
        chart._handle_browser_message({"type": "chart_action", "label": "Mark here",
                                       "time": 60, "price": 1.0})
        assert chart._events.wait_idle(10)
        assert [m for m in sent if m.get("type") == "edit_refused"] == [
            {"type": "edit_refused", "id": "", "message": "not on this candle"}]

    def test_a_picture_shows_the_charts_own_zone_unless_told_another(self):
        chart = Chart(title="XAU", timezone="Asia/Tehran", source_timezone="Europe/Athens")
        chart.set_data(_frame())
        query = parse_qs(chart._snapshot_query())
        assert query["tz"] == ["Asia/Tehran"] and query["stz"] == ["Europe/Athens"]
        assert query["title"][0].endswith("2026-10-31 00:15 (UTC+03:30)")
        assert parse_qs(chart._snapshot_query(timezone="UTC"))["tz"] == ["UTC"]


class TestPictureRoom:
    """v1.3.0 Part 4h: a picture leaves empty candles after its last one — 10 unless the
    program says otherwise, for one picture, one chart or every picture."""

    def _room(self, chart, **kw):
        return parse_qs(chart._snapshot_query(**kw))["room"]

    def test_ten_by_default(self):
        chart = Chart(title="XAU")
        chart.set_data(_frame())
        assert Chart.picture_room == 10
        assert self._room(chart) == ["10"]

    def test_one_picture_one_chart_or_every_picture(self, monkeypatch):
        chart = Chart(title="XAU")
        chart.set_data(_frame())
        assert self._room(chart, room=0) == ["0"] and self._room(chart, room=25) == ["25"]
        chart.picture_room = 3
        assert self._room(chart) == ["3"]
        monkeypatch.setattr(Chart, "picture_room", 6)          # every chart's pictures
        other = Chart(title="BTC")
        other.set_data(_frame())
        assert self._room(other) == ["6"] and self._room(chart) == ["3"]

    @pytest.mark.parametrize("room", [-1, 1001, 2.5, "10", True])
    def test_a_room_that_is_not_a_count_of_candles_is_refused(self, room):
        chart = Chart(title="XAU")
        chart.set_data(_frame())
        with pytest.raises(ValueError, match="right_room"):
            chart._snapshot_query(room=room)

    def test_screenshot_takes_right_room(self, monkeypatch):
        import AlgoTradeKit.visual._snapshot as snap

        asked = []
        monkeypatch.setattr(snap, "snapshot", lambda address, w, h: asked.append(address) or b"png")
        chart = Chart(title="XAU")
        chart.set_data(_frame())
        chart.screenshot(right_room=4)
        chart.screenshot()
        assert [parse_qs(a.split("?", 1)[1])["room"] for a in asked] == [["4"], ["10"]]
