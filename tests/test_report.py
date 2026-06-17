"""
tests/test_report.py
~~~~~~~~~~~~~~~~~~~~~
Tests for AlgoTradeKit v0.7.0 — report module, PositionBox drawing,
SimulateConfig new fields, and StrategyResult.drawings.

These tests use only the existing test helpers and do not start any
real HTTP server or open any browser window.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers — minimal stubs so we can test without a full simulation run
# ---------------------------------------------------------------------------

def _make_minimal_closed_trade(
    trade_id: int = 0,
    direction: str = "long",
    net_pnl: float = 100.0,
    close_reason: str = "tp",
    open_time: int = 1_700_000_000_000,
    close_time: int = 1_700_003_600_000,
    entry_price: float = 50_000.0,
    stop_loss: float  = 49_500.0,
    take_profit: float | None = 51_000.0,
):
    """Return a minimal ClosedTrade-like object for report building tests."""
    from AlgoTradeKit.simulate._position import ClosedTrade
    return ClosedTrade(
        trade_id=trade_id,
        symbol="btcusdt",
        direction=direction,
        open_time=open_time,
        close_time=close_time,
        entry_price=entry_price,
        exit_price=take_profit if close_reason == "tp" else stop_loss,
        initial_stop_loss=stop_loss,
        final_stop_loss=stop_loss,
        take_profit=take_profit,
        size=0.02,
        margin_amount=1_000.0,
        risk_amount=100.0,
        gross_pnl=net_pnl + 2.0,   # commission = 2.0
        commission=2.0,
        net_pnl=net_pnl,
        pnl_r=net_pnl / 100.0,
        close_reason=close_reason,
        rr_levels_hit=0,
        max_favourable_excursion=net_pnl * 1.5 if net_pnl > 0 else 0.0,
        max_adverse_excursion=net_pnl * -0.5,
        leverage=10.0,
        spread_paid=1.0,
        signal_metadata={},
        signal_candle_index=0,
    )


def _make_minimal_balance_history(n: int = 50) -> list[dict]:
    """Generate a simple rising equity curve."""
    base = 10_000.0
    return [
        {
            "timestamp": 1_700_000_000_000 + i * 3_600_000,
            "wallet":    round(base + i * 20.0, 2),
            "equity":    round(base + i * 22.0, 2),
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# 1. StrategyResult.drawings
# ---------------------------------------------------------------------------

class TestStrategyResultDrawings:
    def test_drawings_default_empty(self):
        from AlgoTradeKit.strategy._types import (
            Signal, StrategyResult, StrategyMode,
        )
        result = StrategyResult(
            signals=[],
            exit_signals=[],
            data={},
            mode=StrategyMode.BACKTEST,
        )
        assert result.drawings == []

    def test_drawings_stored_correctly(self):
        from AlgoTradeKit.strategy._types import StrategyResult, StrategyMode

        drawings = [
            {"type": "hline", "price": 42000.0, "color": "#58a6ff", "source": "server"},
            {"type": "box", "time1": 1700000000, "price1": 40000.0,
             "time2": 1700007200, "price2": 42000.0, "source": "server"},
        ]
        result = StrategyResult(
            signals=[], exit_signals=[], data={},
            mode=StrategyMode.BACKTEST,
            drawings=drawings,
        )
        assert len(result.drawings) == 2
        assert result.drawings[0]["type"] == "hline"
        assert result.drawings[1]["type"] == "box"

    def test_repr_includes_drawings_count(self):
        from AlgoTradeKit.strategy._types import StrategyResult, StrategyMode
        result = StrategyResult(
            signals=[], exit_signals=[], data={},
            mode=StrategyMode.BACKTEST,
            drawings=[{"type": "hline", "price": 1.0}],
        )
        assert "drawings=1" in repr(result)


# ---------------------------------------------------------------------------
# 2. PositionBox model
# ---------------------------------------------------------------------------

class TestPositionBox:
    def test_long_to_dict_zones(self):
        """Loss zone is below entry; profit zone is above entry for long."""
        from AlgoTradeKit.visual.models import PositionBox
        pb = PositionBox(
            open_time=1700000000,
            close_time=1700003600,
            entry_price=50_000.0,
            stop_loss=49_500.0,
            take_profit=51_000.0,
            direction="long",
            net_pnl=100.0,
            close_reason="tp",
            trade_id=1,
            rr_ratio=2.0,
        )
        d = pb.to_dict()
        assert d["type"] == "position_box"
        assert d["profit_top"]    == 51_000.0
        assert d["profit_bottom"] == 50_000.0
        assert d["loss_top"]      == 50_000.0
        assert d["loss_bottom"]   == 49_500.0
        assert d["profit_color"]  == "#3fb950"
        assert d["loss_color"]    == "#f85149"
        assert d["direction"]     == "long"
        assert d["source"]        == "server"

    def test_short_to_dict_zones(self):
        """Loss zone is above entry; profit zone is below entry for short."""
        from AlgoTradeKit.visual.models import PositionBox
        pb = PositionBox(
            open_time=1700000000,
            close_time=1700003600,
            entry_price=50_000.0,
            stop_loss=50_500.0,
            take_profit=49_000.0,
            direction="short",
            net_pnl=100.0,
            close_reason="tp",
            trade_id=2,
            rr_ratio=2.0,
        )
        d = pb.to_dict()
        assert d["profit_top"]    == 50_000.0
        assert d["profit_bottom"] == 49_000.0
        assert d["loss_top"]      == 50_500.0
        assert d["loss_bottom"]   == 50_000.0

    def test_none_take_profit_uses_2r_placeholder(self):
        """When take_profit is None, visual_tp = entry ± 2 × sl_distance."""
        from AlgoTradeKit.visual.models import PositionBox
        pb = PositionBox(
            open_time=1700000000,
            close_time=1700003600,
            entry_price=50_000.0,
            stop_loss=49_500.0,   # sl_distance = 500
            take_profit=None,
            direction="long",
            net_pnl=0.0,
            close_reason="sl",
            trade_id=3,
        )
        d = pb.to_dict()
        assert d["visual_tp"] == pytest.approx(51_000.0)  # 50_000 + 2 * 500

    def test_label_tp_win(self):
        from AlgoTradeKit.visual.models import PositionBox
        pb = PositionBox(
            open_time=1700000000, close_time=1700003600,
            entry_price=50_000.0, stop_loss=49_500.0, take_profit=51_000.0,
            direction="long", net_pnl=100.0, close_reason="tp",
            rr_ratio=2.0,
        )
        d = pb.to_dict()
        assert "TP" in d["label"]
        assert "+2.00R" in d["label"]

    def test_label_sl_loss(self):
        from AlgoTradeKit.visual.models import PositionBox
        pb = PositionBox(
            open_time=1700000000, close_time=1700003600,
            entry_price=50_000.0, stop_loss=49_500.0, take_profit=51_000.0,
            direction="long", net_pnl=-100.0, close_reason="sl",
            rr_ratio=-1.0,
        )
        d = pb.to_dict()
        assert "SL" in d["label"]
        assert "-1.00R" in d["label"]

    def test_unique_ids(self):
        from AlgoTradeKit.visual.models import PositionBox
        pb1 = PositionBox(1700000000, 1700003600, 50000.0, 49500.0,
                          51000.0, "long", 100.0, "tp")
        pb2 = PositionBox(1700000000, 1700003600, 50000.0, 49500.0,
                          51000.0, "long", 100.0, "tp")
        assert pb1.id != pb2.id

    def test_position_box_in_visual_init(self):
        """PositionBox must be importable from AlgoTradeKit.visual."""
        from AlgoTradeKit.visual import PositionBox  # noqa: F401


# ---------------------------------------------------------------------------
# 3. Chart.add_position_box
# ---------------------------------------------------------------------------

class TestChartAddPositionBox:
    def _make_minimal_chart(self):
        """Create a Chart without starting a server."""
        from AlgoTradeKit.visual.chart import Chart
        chart = Chart.__new__(Chart)
        chart._drawings = []
        chart._indicators = []
        chart._shown = False
        chart._server = None
        chart._title = "test"
        return chart

    def test_add_position_box_appends_to_drawings(self):
        from AlgoTradeKit.visual.models import PositionBox
        chart = self._make_minimal_chart()
        chart.add_position_box(
            open_time=1700000000,
            close_time=1700003600,
            entry_price=50_000.0,
            stop_loss=49_500.0,
            take_profit=51_000.0,
            direction="long",
            net_pnl=100.0,
        )
        assert len(chart._drawings) == 1
        assert isinstance(chart._drawings[0], PositionBox)

    def test_add_multiple_position_boxes(self):
        chart = self._make_minimal_chart()
        for i in range(5):
            chart.add_position_box(
                open_time=1700000000 + i * 3600,
                close_time=1700000000 + (i + 1) * 3600,
                entry_price=50_000.0,
                stop_loss=49_500.0,
                take_profit=51_000.0,
                direction="long",
                net_pnl=float(i * 50),
            )
        assert len(chart._drawings) == 5


# ---------------------------------------------------------------------------
# 4. SimulateConfig new fields
# ---------------------------------------------------------------------------

class TestSimulateConfigNewFields:
    def test_defaults(self):
        from AlgoTradeKit.simulate import SimulateConfig
        cfg = SimulateConfig()
        assert cfg.show_chart is False
        assert cfg.report_mode == "none"
        assert cfg.report_save_path == "report.html"

    def test_valid_report_modes(self):
        from AlgoTradeKit.simulate import SimulateConfig
        for mode in ("none", "webpage", "save", "both"):
            cfg = SimulateConfig(report_mode=mode)
            assert cfg.report_mode == mode

    def test_invalid_report_mode_raises(self):
        from AlgoTradeKit.simulate import SimulateConfig
        with pytest.raises(ValueError, match="report_mode"):
            SimulateConfig(report_mode="invalid_mode")

    def test_show_chart_true(self):
        from AlgoTradeKit.simulate import SimulateConfig
        cfg = SimulateConfig(show_chart=True)
        assert cfg.show_chart is True

    def test_custom_report_save_path(self):
        from AlgoTradeKit.simulate import SimulateConfig
        cfg = SimulateConfig(report_save_path="/tmp/my_report.html")
        assert cfg.report_save_path == "/tmp/my_report.html"

    def test_report_mode_constants_exported(self):
        from AlgoTradeKit.simulate import (
            REPORT_MODE_NONE, REPORT_MODE_WEBPAGE,
            REPORT_MODE_SAVE, REPORT_MODE_BOTH,
        )
        assert REPORT_MODE_NONE    == "none"
        assert REPORT_MODE_WEBPAGE == "webpage"
        assert REPORT_MODE_SAVE    == "save"
        assert REPORT_MODE_BOTH    == "both"


# ---------------------------------------------------------------------------
# 5. report._builder.build_report_payload
# ---------------------------------------------------------------------------

class TestBuildReportPayload:
    def _make_report(self, n_trades: int = 5):
        """Build a minimal SimulateReport for testing."""
        from AlgoTradeKit.simulate._config import SimulateConfig
        from AlgoTradeKit.simulate._report import build_report

        trades = [
            _make_minimal_closed_trade(
                trade_id=i,
                net_pnl=100.0 if i % 2 == 0 else -50.0,
                close_reason="tp" if i % 2 == 0 else "sl",
            )
            for i in range(n_trades)
        ]
        balance_history = _make_minimal_balance_history(n=n_trades * 10)
        cfg = SimulateConfig(symbol="btcusdt", leverage=10)
        return build_report(trades, [], balance_history, cfg)

    def test_payload_keys_present(self):
        from AlgoTradeKit.report._builder import build_report_payload
        report = self._make_report()
        payload = build_report_payload(report)

        required_keys = {
            "config", "summary", "balance_history",
            "max_drawdown", "significant_drawdowns",
            "weekday_stats", "session_stats", "monthly_stats",
            "trade_markers", "has_chart", "chart_port",
        }
        assert required_keys.issubset(payload.keys())

    def test_summary_trade_counts_correct(self):
        from AlgoTradeKit.report._builder import build_report_payload
        report = self._make_report(n_trades=4)
        payload = build_report_payload(report)
        s = payload["summary"]
        assert s["total_trades"] == 4
        # Alternating tp/sl: trades 0,2 = tp (wins); 1,3 = sl (losses)
        assert s["winning_trades"] == 2
        assert s["losing_trades"]  == 2

    def test_balance_history_shape(self):
        from AlgoTradeKit.report._builder import build_report_payload
        n = 20
        report = self._make_report()
        payload = build_report_payload(report)
        hist = payload["balance_history"]
        # All entries must have t, w, e
        assert all("t" in h and "w" in h and "e" in h for h in hist)

    def test_trade_markers_structure(self):
        from AlgoTradeKit.report._builder import build_report_payload
        report = self._make_report(n_trades=3)
        payload = build_report_payload(report)
        markers = payload["trade_markers"]
        assert len(markers) == 3
        for m in markers:
            assert "trade_id"    in m
            assert "direction"   in m
            assert "open_time"   in m
            assert "close_time"  in m
            assert "net_pnl"     in m
            assert "close_reason" in m
            assert "is_win"      in m

    def test_none_values_not_nan(self):
        """NaN floats in numeric fields should be serialised as None, not NaN."""
        from AlgoTradeKit.report._builder import build_report_payload
        import json
        report = self._make_report()
        payload = build_report_payload(report)
        # json.dumps should not raise (NaN is not valid JSON)
        json_str = json.dumps(payload)
        assert "NaN" not in json_str

    def test_config_summary_present(self):
        from AlgoTradeKit.report._builder import build_report_payload
        report = self._make_report()
        payload = build_report_payload(report)
        cfg = payload["config"]
        assert cfg["symbol"] == "btcusdt"
        assert cfg["leverage"] == 10
        assert "initial_balance" in cfg

    def test_has_chart_defaults_false(self):
        from AlgoTradeKit.report._builder import build_report_payload
        report = self._make_report()
        payload = build_report_payload(report)
        assert payload["has_chart"] is False
        assert payload["chart_port"] is None


# ---------------------------------------------------------------------------
# 6. report._display.save_report_html
# ---------------------------------------------------------------------------

class TestSaveReportHtml:
    def _make_report(self):
        from AlgoTradeKit.simulate._config import SimulateConfig
        from AlgoTradeKit.simulate._report import build_report
        trades = [_make_minimal_closed_trade(trade_id=i) for i in range(3)]
        balance_history = _make_minimal_balance_history(30)
        cfg = SimulateConfig(symbol="btcusdt")
        return build_report(trades, [], balance_history, cfg)

    def test_saves_html_file(self, tmp_path):
        from AlgoTradeKit.report._display import save_report_html
        report = self._make_report()
        out = save_report_html(report, path=tmp_path / "test_report.html")
        assert out.exists()
        assert out.suffix == ".html"

    def test_saved_file_contains_data(self, tmp_path):
        from AlgoTradeKit.report._display import save_report_html
        report = self._make_report()
        out = save_report_html(report, path=tmp_path / "test_report.html")
        content = out.read_text(encoding="utf-8")
        # The JSON data should be embedded
        assert "report_data" in content or "renderReport" in content

    def test_saved_file_is_valid_html(self, tmp_path):
        from AlgoTradeKit.report._display import save_report_html
        report = self._make_report()
        out = save_report_html(report, path=tmp_path / "test_report.html")
        content = out.read_text(encoding="utf-8")
        assert content.startswith("<!DOCTYPE html>") or content.startswith("<!doctype html>")
        assert "</html>" in content


# ---------------------------------------------------------------------------
# 7. add_simulation_positions helper
# ---------------------------------------------------------------------------

class TestAddSimulationPositions:
    def _make_chart(self):
        from AlgoTradeKit.visual.chart import Chart
        chart = Chart.__new__(Chart)
        chart._drawings = []
        chart._indicators = []
        chart._shown = False
        chart._server = None
        return chart

    def _make_report_with_trades(self, n: int = 3):
        from AlgoTradeKit.simulate._config import SimulateConfig
        from AlgoTradeKit.simulate._report import build_report
        trades = [_make_minimal_closed_trade(trade_id=i) for i in range(n)]
        balance_history = _make_minimal_balance_history(n * 10)
        cfg = SimulateConfig(symbol="btcusdt")
        return build_report(trades, [], balance_history, cfg)

    def test_adds_position_boxes(self):
        from AlgoTradeKit.visual.indicator_renderer import add_simulation_positions
        from AlgoTradeKit.visual.models import PositionBox

        chart = self._make_chart()
        report = self._make_report_with_trades(3)
        add_simulation_positions(chart, report)
        pos_boxes = [d for d in chart._drawings if isinstance(d, PositionBox)]
        assert len(pos_boxes) == 3

    def test_max_trades_limit(self):
        from AlgoTradeKit.visual.indicator_renderer import add_simulation_positions
        from AlgoTradeKit.visual.models import PositionBox

        chart = self._make_chart()
        report = self._make_report_with_trades(10)
        add_simulation_positions(chart, report, max_trades=4)
        pos_boxes = [d for d in chart._drawings if isinstance(d, PositionBox)]
        assert len(pos_boxes) == 4

    def test_returns_chart_for_chaining(self):
        from AlgoTradeKit.visual.indicator_renderer import add_simulation_positions
        chart = self._make_chart()
        report = self._make_report_with_trades(2)
        result = add_simulation_positions(chart, report)
        assert result is chart


# ---------------------------------------------------------------------------
# 8. add_strategy_drawings helper
# ---------------------------------------------------------------------------

class TestAddStrategyDrawings:
    def _make_chart(self):
        from AlgoTradeKit.visual.chart import Chart
        chart = Chart.__new__(Chart)
        chart._drawings = []
        chart._indicators = []
        chart._shown = False
        chart._server = None
        return chart

    def _make_strategy_result(self, drawings):
        from AlgoTradeKit.strategy._types import StrategyResult, StrategyMode
        return StrategyResult(
            signals=[], exit_signals=[], data={},
            mode=StrategyMode.BACKTEST,
            drawings=drawings,
        )

    def test_adds_drawings(self):
        from AlgoTradeKit.visual.indicator_renderer import add_strategy_drawings
        chart = self._make_chart()
        result = self._make_strategy_result([
            {"type": "hline", "price": 50000.0, "color": "#fff"},
            {"type": "trendline", "time1": 1700000000, "price1": 49000.0,
             "time2": 1700003600, "price2": 50000.0},
        ])
        add_strategy_drawings(chart, result)
        assert len(chart._drawings) == 2

    def test_empty_drawings_no_change(self):
        from AlgoTradeKit.visual.indicator_renderer import add_strategy_drawings
        chart = self._make_chart()
        result = self._make_strategy_result([])
        add_strategy_drawings(chart, result)
        assert len(chart._drawings) == 0

    def test_auto_assigns_missing_id(self):
        from AlgoTradeKit.visual.indicator_renderer import add_strategy_drawings
        chart = self._make_chart()
        result = self._make_strategy_result([
            {"type": "hline", "price": 1.0},
        ])
        add_strategy_drawings(chart, result)
        d = chart._drawings[0]
        assert hasattr(d, "id")
        assert d.id is not None

    def test_returns_chart_for_chaining(self):
        from AlgoTradeKit.visual.indicator_renderer import add_strategy_drawings
        chart = self._make_chart()
        result = self._make_strategy_result([])
        ret = add_strategy_drawings(chart, result)
        assert ret is chart


# ---------------------------------------------------------------------------
# 9. Simulate engine: no chart/report rendered by default
# ---------------------------------------------------------------------------

class TestSimulateNoCrashNoVisualDefault:
    """
    Ensure that adding the new fields does not change existing behaviour:
    when show_chart=False and report_mode="none", run() behaves exactly
    as before — no server is started and the report is returned normally.
    """

    def _run_minimal_simulation(self):
        import pandas as pd
        from AlgoTradeKit.simulate import Simulate, SimulateConfig
        from AlgoTradeKit.strategy._types import (
            Signal, StrategyResult, StrategyMode,
        )

        n = 40
        timestamps = [1_700_000_000_000 + i * 3_600_000 for i in range(n)]
        df = pd.DataFrame({
            "timestamp": timestamps,
            "open":   [100.0 + i for i in range(n)],
            "high":   [105.0 + i for i in range(n)],
            "low":    [ 95.0 + i for i in range(n)],
            "close":  [102.0 + i for i in range(n)],
            "volume": [1_000.0] * n,
        })

        signals = [
            Signal(
                direction="long",
                entry_price=102.0 + i,
                stop_loss=97.0 + i,
                take_profit=112.0 + i,
                timestamp=timestamps[i],
                candle_index=i,
                timeframe="1h",
            )
            for i in range(0, n, 5)
        ]

        result = StrategyResult(
            signals=signals,
            exit_signals=[],
            data={"1h": df},
            mode=StrategyMode.BACKTEST,
        )

        config = SimulateConfig(
            initial_balance=10_000.0,
            leverage=1.0,
            risk_per_trade=1.0,
            show_chart=False,
            report_mode="none",
        )

        return Simulate(config).run(result)

    def test_run_returns_report(self):
        from AlgoTradeKit.simulate import SimulateReport
        report = self._run_minimal_simulation()
        assert isinstance(report, SimulateReport)

    def test_run_report_has_trades(self):
        report = self._run_minimal_simulation()
        assert report.total_trades > 0

    def test_run_report_has_balance_history(self):
        report = self._run_minimal_simulation()
        assert len(report.balance_history) > 0

    def test_run_no_server_started(self):
        """With report_mode='none', _render_report must never be called."""
        with patch(
            "AlgoTradeKit.simulate._engine.Simulate._render_report"
        ) as mock_render:
            self._run_minimal_simulation()
            mock_render.assert_not_called()
