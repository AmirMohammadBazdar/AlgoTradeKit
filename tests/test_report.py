"""
tests/test_report.py
~~~~~~~~~~~~~~~~~~~~~
Tests for AlgoTradeKit v0.7.0 — report module, PositionBox drawing,
SimulateConfig new fields, and StrategyResult.drawings.

These tests use only the existing test helpers and do not start any
real HTTP server or open any browser window.
"""

from __future__ import annotations

from unittest.mock import patch

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
            StrategyMode,
            StrategyResult,
        )
        result = StrategyResult(
            signals=[],
            exit_signals=[],
            data={},
            mode=StrategyMode.BACKTEST,
        )
        assert result.drawings == []

    def test_drawings_stored_correctly(self):
        from AlgoTradeKit.strategy._types import StrategyMode, StrategyResult

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
        from AlgoTradeKit.strategy._types import StrategyMode, StrategyResult
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
            REPORT_MODE_BOTH,
            REPORT_MODE_NONE,
            REPORT_MODE_SAVE,
            REPORT_MODE_WEBPAGE,
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
        import json

        from AlgoTradeKit.report._builder import build_report_payload
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
        from AlgoTradeKit.strategy._types import StrategyMode, StrategyResult
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
            Signal,
            StrategyMode,
            StrategyResult,
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


# ═══════════════════════════════════════════════════════════════════════════
# v1.0.0 — report: live push + combined rendering
# ═══════════════════════════════════════════════════════════════════════════

_TS0 = 1_700_000_000_000  # 2023-11-14T22:13:20Z (a Tuesday)
_HOUR = 3_600_000


def _make_trade_at(
    trade_id: int,
    net_pnl: float,
    open_time: int,
    close_time: int,
    direction: str = "long",
    close_reason: str | None = None,
):
    """ClosedTrade with explicit PnL and times (for aggregation tests)."""
    return _make_minimal_closed_trade(
        trade_id=trade_id,
        direction=direction,
        net_pnl=net_pnl,
        close_reason=close_reason or ("tp" if net_pnl > 0 else "sl"),
        open_time=open_time,
        close_time=close_time,
    )


def _make_report_for_pair(
    symbol: str,
    trades: list,
    balance_history: list[dict],
    initial_balance: float = 10_000.0,
    drawdown_threshold: float = 5.0,
    leverage: float = 10.0,
):
    from AlgoTradeKit.simulate._config import SimulateConfig
    from AlgoTradeKit.simulate._report import build_report

    cfg = SimulateConfig(
        symbol=symbol,
        initial_balance=initial_balance,
        drawdown_threshold=drawdown_threshold,
        leverage=leverage,
    )
    return build_report(trades, [], balance_history, cfg)


def _simple_history(initial: float, equities: list[float], ts0: int = _TS0) -> list[dict]:
    return [
        {"timestamp": ts0 + i * _HOUR, "wallet": e, "equity": e}
        for i, e in enumerate(equities)
    ]


# ---------------------------------------------------------------------------
# 10. — ReportServer host configuration
# ---------------------------------------------------------------------------

class TestReportServerHost:
    def test_default_host(self):
        from AlgoTradeKit.report._server import ReportServer
        srv = ReportServer()
        try:
            assert srv.host == "127.0.0.1"
            assert srv.display_host == "127.0.0.1"
            assert srv.url == f"http://127.0.0.1:{srv.port}"
        finally:
            srv.stop()

    def test_custom_host_in_url(self):
        from AlgoTradeKit.report._server import ReportServer
        srv = ReportServer(host="192.0.2.7", port=9123)
        assert srv.host == "192.0.2.7"
        assert srv.display_host == "192.0.2.7"
        assert srv.url == "http://192.0.2.7:9123"

    def test_bind_all_substituted_in_display(self):
        from AlgoTradeKit.report._server import ReportServer
        srv4 = ReportServer(host="0.0.0.0", port=9124)
        srv6 = ReportServer(host="::", port=9125)
        assert srv4.display_host == "127.0.0.1"
        assert srv4.url == "http://127.0.0.1:9124"
        assert srv6.display_host == "127.0.0.1"
        assert srv6.url == "http://127.0.0.1:9125"

    def test_port_probe_uses_host(self):
        from AlgoTradeKit.report import _server as srv_mod
        port = srv_mod._find_free_port(host="127.0.0.1")
        try:
            assert isinstance(port, int)
            assert port in srv_mod._reserved_ports
        finally:
            srv_mod._reserved_ports.discard(port)

    def test_probe_error_names_host(self):
        from AlgoTradeKit.report import _server as srv_mod
        # 203.0.113.9 (TEST-NET-3) is not a local interface — every bind fails
        with pytest.raises(RuntimeError, match="203.0.113.9"):
            srv_mod._find_free_port(host="203.0.113.9")

    def test_bind_all_served_via_loopback(self):
        import urllib.request

        from AlgoTradeKit.report._server import ReportServer

        srv = ReportServer(host="0.0.0.0")
        srv.start(open_browser=False)
        try:
            with urllib.request.urlopen(f"{srv.url}/", timeout=5) as resp:
                html = resp.read().decode("utf-8")
            assert "report-content" in html
        finally:
            srv.stop()


# ---------------------------------------------------------------------------
# 11. — ReportServer.push_update
# ---------------------------------------------------------------------------

class TestPushUpdate:
    def _report(self, final_step: float = 20.0):
        trades = [_make_minimal_closed_trade(trade_id=i) for i in range(3)]
        hist = _simple_history(10_000.0, [10_000.0 + i * final_step for i in range(10)])
        return _make_report_for_pair("btcusdt", trades, hist)

    def test_push_report_builds_payload_and_caches(self):
        from AlgoTradeKit.report._builder import build_report_payload
        from AlgoTradeKit.report._server import ReportServer

        report = self._report()
        srv = ReportServer(port=9130)
        srv.push_update(report)
        pending = srv._pending_data
        assert pending is not None
        assert pending["type"] == "report_data"
        expected = build_report_payload(report)
        assert pending["summary"] == expected["summary"]

    def test_push_dict_payload_passthrough_without_mutation(self):
        from AlgoTradeKit.report._server import ReportServer

        srv = ReportServer(port=9131)
        payload = {"summary": {"final_balance": 123.0}, "has_chart": False,
                   "chart_port": None}
        snapshot = {k: v for k, v in payload.items()}
        srv.push_update(payload)
        assert srv._pending_data["summary"] == {"final_balance": 123.0}
        assert srv._pending_data["type"] == "report_data"
        assert payload == snapshot          # input dict untouched
        assert "type" not in payload

    def test_chart_link_carried_forward(self):
        from AlgoTradeKit.report._builder import build_report_payload
        from AlgoTradeKit.report._server import ReportServer

        report = self._report()
        srv = ReportServer(port=9132)
        first = build_report_payload(report)
        first["has_chart"] = True
        first["chart_port"] = 4321
        srv.set_report_data(first)

        srv.push_update(self._report(final_step=25.0))   # fresh stats
        assert srv._pending_data["has_chart"] is True
        assert srv._pending_data["chart_port"] == 4321

    def test_explicit_chart_link_wins(self):
        from AlgoTradeKit.report._server import ReportServer

        srv = ReportServer(port=9133)
        srv.set_report_data({"has_chart": True, "chart_port": 4321})
        srv.push_update({"summary": {}, "has_chart": True, "chart_port": 9999})
        assert srv._pending_data["chart_port"] == 9999

    def test_no_previous_payload_no_link(self):
        from AlgoTradeKit.report._server import ReportServer

        srv = ReportServer(port=9134)
        srv.push_update(self._report())
        assert srv._pending_data["has_chart"] is False
        assert srv._pending_data["chart_port"] is None


# ---------------------------------------------------------------------------
# 12. — push protocol end-to-end over a real WebSocket
# ---------------------------------------------------------------------------

class TestReportPushProtocolE2E:
    def _report(self, final_equity: float):
        trades = [_make_minimal_closed_trade(trade_id=i) for i in range(2)]
        hist = _simple_history(10_000.0, [10_000.0, 10_500.0, final_equity])
        return _make_report_for_pair("btcusdt", trades, hist)

    def test_push_update_rerenders_and_replays(self):
        import json as _json

        from websockets.sync.client import connect

        from AlgoTradeKit.report._builder import build_report_payload
        from AlgoTradeKit.report._server import ReportServer

        srv = ReportServer()
        srv.start(open_browser=False)
        try:
            first = build_report_payload(self._report(11_000.0))
            first["has_chart"] = True
            first["chart_port"] = 4321
            srv.set_report_data(first)

            ws_url = f"ws://127.0.0.1:{srv.port}/ws"
            with connect(ws_url) as ws:
                msg = _json.loads(ws.recv(timeout=5))
                assert msg["type"] == "report_data"
                assert msg["summary"]["final_balance"] == 11_000.0

                # Live push: fresh stats re-broadcast to the open page
                srv.push_update(self._report(12_345.0))
                msg = _json.loads(ws.recv(timeout=5))
                assert msg["type"] == "report_data"
                assert msg["summary"]["final_balance"] == 12_345.0
                # chart link carried forward across the push
                assert msg["has_chart"] is True
                assert msg["chart_port"] == 4321

            # A SECOND client (page refresh) replays the UPDATED stats,
            # not the state at start() time.
            with connect(ws_url) as ws2:
                msg2 = _json.loads(ws2.recv(timeout=5))
                assert msg2["summary"]["final_balance"] == 12_345.0
                assert msg2["has_chart"] is True
        finally:
            srv.stop()


# ---------------------------------------------------------------------------
# 13. — combined (multi-pair) payload
# ---------------------------------------------------------------------------

class TestCombinedReportPayload:
    def _two_reports(self):
        trades_a = [
            _make_trade_at(0, 100.0, _TS0, _TS0 + 1 * _HOUR),
            _make_trade_at(1, -50.0, _TS0 + 1 * _HOUR, _TS0 + 3 * _HOUR, "short"),
        ]
        hist_a = _simple_history(10_000.0, [10_000.0, 10_100.0, 10_050.0, 10_050.0])
        r_a = _make_report_for_pair("btcusdt", trades_a, hist_a, initial_balance=10_000.0)

        trades_b = [
            _make_trade_at(0, 200.0, _TS0, _TS0 + 2 * _HOUR),
        ]
        hist_b = _simple_history(5_000.0, [5_000.0, 5_100.0, 5_200.0, 5_200.0])
        r_b = _make_report_for_pair("eurusd", trades_b, hist_b, initial_balance=5_000.0,
                                    leverage=30.0)
        return r_a, r_b

    # -- validation ---------------------------------------------------------

    def test_empty_pairs_raises(self):
        from AlgoTradeKit.report import build_combined_report_payload
        with pytest.raises(ValueError, match="must not be empty"):
            build_combined_report_payload([])

    def test_duplicate_labels_raise(self):
        from AlgoTradeKit.report import build_combined_report_payload
        r_a, _ = self._two_reports()
        with pytest.raises(ValueError, match="duplicate"):
            build_combined_report_payload([("p", r_a), ("p", r_a)])

    def test_invalid_label_raises(self):
        from AlgoTradeKit.report import build_combined_report_payload
        r_a, r_b = self._two_reports()
        with pytest.raises(ValueError, match="non-empty string"):
            build_combined_report_payload([("", r_a)])
        with pytest.raises(ValueError, match="non-empty string"):
            build_combined_report_payload([(3, r_a)])

    def test_accepts_dict_input(self):
        from AlgoTradeKit.report import build_combined_report_payload
        r_a, r_b = self._two_reports()
        payload = build_combined_report_payload({"a": r_a, "b": r_b})
        assert [p["label"] for p in payload["pairs"]] == ["a", "b"]

    # -- single-pair parity: locks the mirrored formulas to simulate's ------

    def test_single_pair_parity_exact(self):
        from AlgoTradeKit.report import (
            build_combined_report_payload,
            build_report_payload,
        )
        r_a, _ = self._two_reports()
        single = build_report_payload(r_a)
        combined = build_combined_report_payload([("only", r_a)])

        assert combined["summary"] == single["summary"]
        assert combined["balance_history"] == single["balance_history"]
        assert combined["max_drawdown"] == single["max_drawdown"]
        assert combined["significant_drawdowns"] == single["significant_drawdowns"]
        assert combined["weekday_stats"] == single["weekday_stats"]
        assert combined["session_stats"] == single["session_stats"]
        assert combined["monthly_stats"] == single["monthly_stats"]

        stripped = [
            {k: v for k, v in m.items() if k not in ("pair", "uid")}
            for m in combined["trade_markers"]
        ]
        assert stripped == single["trade_markers"]

        cfg_diffs = {
            k for k in single["config"]
            if single["config"][k] != combined["config"][k]
        }
        assert cfg_diffs == {"config_id"}     # only the portfolio id differs
        assert combined["combined"] is True
        assert len(combined["pairs"]) == 1

    # -- portfolio aggregation ----------------------------------------------

    def test_summary_sums_two_pairs(self):
        from AlgoTradeKit.report import build_combined_report_payload
        r_a, r_b = self._two_reports()
        payload = build_combined_report_payload([("a", r_a), ("b", r_b)])
        s = payload["summary"]
        assert s["initial_balance"] == 15_000.0
        assert s["final_balance"] == 15_250.0          # 10 050 + 5 200
        assert s["total_pnl"] == 250.0
        assert s["total_trades"] == 3
        assert s["winning_trades"] == 2
        assert s["losing_trades"] == 1
        assert s["long_trades"] == 2
        assert s["short_trades"] == 1
        assert s["win_rate"] == pytest.approx(66.67)
        assert s["total_commission"] == 6.0            # 2.0 per trade
        assert s["total_spread_cost"] == 3.0           # 1.0 per trade

    def test_summed_equity_union_step_hold(self):
        from AlgoTradeKit.report import build_combined_report_payload
        # Pair A snapshots at t0 and t0+2h; pair B at t0+1h and t0+3h.
        hist_a = [
            {"timestamp": _TS0, "wallet": 10_000.0, "equity": 10_000.0},
            {"timestamp": _TS0 + 2 * _HOUR, "wallet": 10_100.0, "equity": 10_100.0},
        ]
        hist_b = [
            {"timestamp": _TS0 + 1 * _HOUR, "wallet": 5_050.0, "equity": 5_050.0},
            {"timestamp": _TS0 + 3 * _HOUR, "wallet": 5_100.0, "equity": 5_100.0},
        ]
        r_a = _make_report_for_pair("btcusdt", [], hist_a, initial_balance=10_000.0)
        r_b = _make_report_for_pair("eurusd", [], hist_b, initial_balance=5_000.0)

        payload = build_combined_report_payload([("a", r_a), ("b", r_b)])
        hist = payload["balance_history"]
        assert [h["t"] for h in hist] == [
            _TS0, _TS0 + 1 * _HOUR, _TS0 + 2 * _HOUR, _TS0 + 3 * _HOUR,
        ]
        # t0:   A 10 000 + B initial 5 000 (no snapshot yet)
        # t0+1: A holds 10 000 + B 5 050
        # t0+2: A 10 100 + B holds 5 050
        # t0+3: A holds 10 100 + B 5 100
        assert [h["e"] for h in hist] == [15_000.0, 15_050.0, 15_150.0, 15_200.0]
        assert [h["w"] for h in hist] == [15_000.0, 15_050.0, 15_150.0, 15_200.0]

    def test_merged_markers_sorted_with_pair_and_uid(self):
        from AlgoTradeKit.report import build_combined_report_payload
        r_a, r_b = self._two_reports()
        payload = build_combined_report_payload([("a", r_a), ("b", r_b)])
        markers = payload["trade_markers"]
        assert len(markers) == 3
        # sorted by close_time: a#0 (t+1h), b#0 (t+2h), a#1 (t+3h)
        assert [m["uid"] for m in markers] == ["a#0", "b#0", "a#1"]
        assert [m["pair"] for m in markers] == ["a", "b", "a"]
        close_times = [m["close_time"] for m in markers]
        assert close_times == sorted(close_times)

    def test_streaks_use_merged_chronology(self):
        from AlgoTradeKit.report import build_combined_report_payload
        # A wins closing at t+1h and t+5h; B win closes at t+3h between them.
        # Merged chronology = W W W → streak 3; no single pair exceeds 2.
        trades_a = [
            _make_trade_at(0, 100.0, _TS0, _TS0 + 1 * _HOUR),
            _make_trade_at(1, 100.0, _TS0 + 4 * _HOUR, _TS0 + 5 * _HOUR),
        ]
        trades_b = [_make_trade_at(0, 100.0, _TS0 + 2 * _HOUR, _TS0 + 3 * _HOUR)]
        r_a = _make_report_for_pair(
            "btcusdt", trades_a, _simple_history(10_000.0, [10_000.0] * 8))
        r_b = _make_report_for_pair(
            "eurusd", trades_b, _simple_history(10_000.0, [10_000.0] * 8))
        assert r_a.max_consecutive_wins == 2
        assert r_b.max_consecutive_wins == 1

        payload = build_combined_report_payload([("a", r_a), ("b", r_b)])
        assert payload["summary"]["max_consecutive_wins"] == 3

    def test_drawdown_computed_on_summed_curve(self):
        from AlgoTradeKit.report import build_combined_report_payload
        # A dips 10% (10 000 → 9 000 → 10 000); B stays flat at 10 000.
        # Portfolio curve 20 000 → 19 000 → 20 000 → 5% drawdown.
        hist_a = _simple_history(10_000.0, [10_000.0, 9_000.0, 10_000.0])
        hist_b = _simple_history(10_000.0, [10_000.0, 10_000.0, 10_000.0])
        r_a = _make_report_for_pair("btcusdt", [], hist_a, drawdown_threshold=1.0)
        r_b = _make_report_for_pair("eurusd", [], hist_b, drawdown_threshold=1.0)
        assert r_a.max_drawdown.drawdown_percent == pytest.approx(10.0)

        payload = build_combined_report_payload([("a", r_a), ("b", r_b)])
        dd = payload["max_drawdown"]
        assert dd["drawdown_percent"] == pytest.approx(5.0)
        assert dd["drawdown_amount"] == pytest.approx(1_000.0)
        assert len(payload["significant_drawdowns"]) == 1
        assert payload["significant_drawdowns"][0]["drawdown_percent"] == pytest.approx(5.0)

    def test_grouped_stats_sum_key_wise(self):
        from AlgoTradeKit.report import build_combined_report_payload
        r_a, r_b = self._two_reports()
        payload = build_combined_report_payload([("a", r_a), ("b", r_b)])

        # All three trades open on the same weekday (_TS0 is a Tuesday;
        # +1h stays within it) → one merged row carrying all of them.
        weekdays = {row["weekday"]: row for row in payload["weekday_stats"]}
        assert weekdays["Tuesday"]["total_trades"] == 3
        assert weekdays["Tuesday"]["winning_trades"] == 2
        assert weekdays["Tuesday"]["total_pnl"] == 250.0

        months = {row["month"]: row for row in payload["monthly_stats"]}
        assert months["2023-11"]["total_trades"] == 3

        # Sessions: totals across all rows must equal the merged trade count
        assert sum(row["total_trades"] for row in payload["session_stats"]) == 3

    def test_pair_breakdown_rows(self):
        from AlgoTradeKit.report import (
            build_combined_report_payload,
            build_report_payload,
        )
        r_a, r_b = self._two_reports()
        payload = build_combined_report_payload([("a", r_a), ("b", r_b)])
        rows = {p["label"]: p for p in payload["pairs"]}

        single_a = build_report_payload(r_a)["summary"]
        assert rows["a"]["symbol"] == "btcusdt"
        assert rows["a"]["final_balance"] == single_a["final_balance"]
        assert rows["a"]["total_pnl"] == single_a["total_pnl"]
        assert rows["a"]["win_rate"] == single_a["win_rate"]
        assert rows["a"]["total_trades"] == single_a["total_trades"]
        assert rows["b"]["initial_balance"] == 5_000.0
        assert rows["b"]["total_trades"] == 1

    def test_config_mixed_fields(self):
        from AlgoTradeKit.report import build_combined_report_payload
        r_a, r_b = self._two_reports()     # leverage 10 vs 30, symbols differ
        payload = build_combined_report_payload([("a", r_a), ("b", r_b)])
        cfg = payload["config"]
        assert cfg["config_id"] == "portfolio(2 pairs)"
        assert cfg["symbol"] == "2 pairs"
        assert cfg["leverage"] == "mixed"
        assert cfg["initial_balance"] == 15_000.0            # summed, never "mixed"
        assert cfg["tp_mode"] == "signal"                    # identical → value
        assert cfg["drawdown_threshold"] == 5.0              # first pair's, numeric

    def test_combined_payload_json_safe(self):
        import json as _json

        from AlgoTradeKit.report import build_combined_report_payload
        r_a, r_b = self._two_reports()
        payload = build_combined_report_payload([("a", r_a), ("b", r_b)])
        text = _json.dumps(payload)
        assert "NaN" not in text
        assert payload["has_chart"] is False
        assert payload["chart_port"] is None


# ---------------------------------------------------------------------------
# 14. — real-trades report renders unchanged
# ---------------------------------------------------------------------------

class TestRealTradesReportRendering:
    """A report built from broker-fill ClosedTrade records (as the
    real-fills pipeline produces) is a plain SimulateReport — every render
    path must consume it unchanged."""

    def _real_fills_report(self):
        # Hand-built records with real fill prices — no Simulate run involved.
        trades = [
            _make_trade_at(0, 87.31, _TS0, _TS0 + 1 * _HOUR),
            _make_trade_at(1, -43.02, _TS0 + 2 * _HOUR, _TS0 + 4 * _HOUR, "short"),
            _make_trade_at(2, 155.55, _TS0 + 5 * _HOUR, _TS0 + 6 * _HOUR,
                           close_reason="force_close"),
        ]
        hist = _simple_history(
            10_000.0, [10_000.0, 10_087.31, 10_087.31, 10_044.29, 10_044.29,
                       10_044.29, 10_199.84])
        return _make_report_for_pair("btcusdt", trades, hist)

    def test_payload_renders_from_real_fills(self):
        from AlgoTradeKit.report import build_report_payload
        payload = build_report_payload(self._real_fills_report())
        assert payload["summary"]["total_trades"] == 3
        assert len(payload["trade_markers"]) == 3
        assert payload["summary"]["final_balance"] == 10_199.84

    def test_save_html_from_real_fills(self, tmp_path):
        from AlgoTradeKit.report import save_report_html
        out = save_report_html(self._real_fills_report(), tmp_path / "real.html")
        assert out.exists()
        assert "report_data" in out.read_text(encoding="utf-8")

    def test_push_update_accepts_real_fills_report(self):
        from AlgoTradeKit.report._server import ReportServer
        srv = ReportServer(port=9140)
        srv.push_update(self._real_fills_report())
        assert srv._pending_data["summary"]["total_trades"] == 3

    def test_real_fills_pair_in_combined(self):
        from AlgoTradeKit.report import build_combined_report_payload
        sim_trades = [_make_trade_at(0, 60.0, _TS0, _TS0 + 1 * _HOUR)]
        sim_report = _make_report_for_pair(
            "eurusd", sim_trades, _simple_history(5_000.0, [5_000.0, 5_060.0]),
            initial_balance=5_000.0)
        payload = build_combined_report_payload([
            ("live:btcusdt", self._real_fills_report()),
            ("sim:eurusd", sim_report),
        ])
        assert payload["summary"]["total_trades"] == 4
        assert len(payload["pairs"]) == 2


# ---------------------------------------------------------------------------
# 15. — combined display entry points
# ---------------------------------------------------------------------------

class TestCombinedDisplayEntryPoints:
    def _pairs(self):
        trades = [_make_trade_at(0, 100.0, _TS0, _TS0 + 1 * _HOUR)]
        r_a = _make_report_for_pair(
            "btcusdt", trades, _simple_history(10_000.0, [10_000.0, 10_100.0]))
        r_b = _make_report_for_pair(
            "eurusd", [], _simple_history(5_000.0, [5_000.0, 5_000.0]),
            initial_balance=5_000.0)
        return [("a", r_a), ("b", r_b)]

    def test_save_combined_report_html(self, tmp_path):
        from AlgoTradeKit.report import save_combined_report_html
        out = save_combined_report_html(self._pairs(), tmp_path / "combined.html")
        assert out.exists()
        content = out.read_text(encoding="utf-8")
        assert '"combined":true' in content
        assert "renderReport" in content
        assert content.startswith("<!DOCTYPE html>") or content.startswith("<!doctype html>")

    def test_show_combined_report_serves_payload(self):
        import time as _time

        from AlgoTradeKit.report import show_combined_report
        with patch("webbrowser.open"):
            srv = show_combined_report(self._pairs())
        try:
            assert srv._pending_data["combined"] is True
            assert len(srv._pending_data["pairs"]) == 2
            assert srv.host == "127.0.0.1"
        finally:
            _time.sleep(0.2)   # let the loop drain the scheduled broadcast
            srv.stop()

    def test_show_report_host_passthrough(self):
        import time as _time

        from AlgoTradeKit.report import show_report
        trades = [_make_minimal_closed_trade(trade_id=0)]
        report = _make_report_for_pair(
            "btcusdt", trades, _simple_history(10_000.0, [10_000.0, 10_100.0]))
        with patch("webbrowser.open"):
            srv = show_report(report, host="0.0.0.0")
        try:
            assert srv.host == "0.0.0.0"
            assert srv.url == f"http://127.0.0.1:{srv.port}"
        finally:
            _time.sleep(0.2)   # let the loop drain the scheduled broadcast
            srv.stop()


# ---------------------------------------------------------------------------
# 16. — the shipped frontend handles combined payloads + live re-render
# ---------------------------------------------------------------------------

class TestReportFrontendWiring:
    @pytest.fixture()
    def html(self) -> str:
        import AlgoTradeKit.report._server as _srv
        return (_srv.STATIC_DIR / "report.html").read_text(encoding="utf-8")

    def test_renders_pair_breakdown_section(self, html):
        assert 'id="sec-pairs"' in html
        assert 'id="pairs-tbody"' in html
        assert "function renderPairsTable" in html
        assert "d.combined" in html

    def test_tooltip_shows_pair(self, html):
        assert 'id="tt-pair-row"' in html
        assert "trade.pair" in html

    def test_markers_use_uid_fallback(self, html):
        assert "trade.uid ?? trade.trade_id" in html
        assert "(t.uid ?? t.trade_id) === hoveredTradeId" in html

    def test_pan_zoom_bound_once(self, html):
        assert "_panZoomBound" in html
        assert "if (_panZoomBound) return;" in html

    def test_zoom_preserved_across_rerender(self, html):
        assert "prevXMin" in html
        assert "prevXMax" in html

    def test_config_mixed_guard(self, html):
        assert "function fmtCfg" in html
