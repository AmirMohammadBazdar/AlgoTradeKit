"""
AlgoTradeKit.report._builder
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Serialise a ``SimulateReport`` into a JSON-safe dict that the report
HTML page can consume directly.

All numeric values are rounded to a sensible precision.  ``None`` values
are preserved as ``null`` in JSON.  Timestamps remain as UTC milliseconds
(the chart's Chart.js dataset uses them directly).

Public function
---------------
build_report_payload(report: SimulateReport) → dict
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from AlgoTradeKit.simulate._report import SimulateReport


def _r(v: float | None, n: int = 2) -> float | None:
    """Round float to *n* decimal places or return None."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    return round(float(v), n)


def build_report_payload(report: "SimulateReport") -> dict:
    """
    Convert a ``SimulateReport`` into a JSON-serialisable dict for the
    report HTML page.

    Parameters
    ----------
    report : SimulateReport
        Completed simulation report from ``Simulate(config).run()``.

    Returns
    -------
    dict
        JSON-safe payload consumed by ``report.html`` via WebSocket.
    """
    cfg = report.config

    # ------------------------------------------------------------------
    # Config summary
    # ------------------------------------------------------------------
    config_summary = {
        "config_id":        cfg.config_id,
        "symbol":           cfg.symbol or "—",
        "exchange_type":    cfg.exchange_type,
        "leverage":         cfg.leverage,
        "initial_balance":  _r(cfg.initial_balance),
        "risk_per_trade":   _r(cfg.risk_per_trade),
        "tp_mode":          cfg.tp_mode,
        "sl_mode":          cfg.sl_mode,
        "commission":       _r(cfg.commission, 4),
        "commission_type":  cfg.commission_type,
        "spread":           _r(cfg.spread, 6),
        "primary_timeframe": cfg.primary_timeframe,
        "drawdown_threshold": cfg.drawdown_threshold,
    }

    # ------------------------------------------------------------------
    # Performance summary
    # ------------------------------------------------------------------
    summary = {
        "initial_balance":  _r(report.initial_balance),
        "final_balance":    _r(report.final_balance),
        "total_pnl":        _r(report.total_pnl),
        "total_pnl_percent": _r(report.total_pnl_percent),
        # Trade counts
        "total_trades":     report.total_trades,
        "long_trades":      report.long_trades,
        "short_trades":     report.short_trades,
        "winning_trades":   report.winning_trades,
        "losing_trades":    report.losing_trades,
        "break_even_trades": report.break_even_trades,
        "sl_count":         report.sl_count,
        "tp_count":         report.tp_count,
        "force_close_count": report.force_close_count,
        "risk_free_count":  report.risk_free_count,
        "end_of_data_count": report.end_of_data_count,
        # Win/loss metrics
        "win_rate":         _r(report.win_rate),
        "loss_rate":        _r(report.loss_rate),
        "avg_win":          _r(report.avg_win),
        "avg_loss":         _r(report.avg_loss),
        "largest_win":      _r(report.largest_win),
        "largest_loss":     _r(report.largest_loss),
        "avg_pnl_r":        _r(report.avg_pnl_r, 3),
        # Streaks
        "max_consecutive_wins":   report.max_consecutive_wins,
        "max_consecutive_losses": report.max_consecutive_losses,
        # Risk metrics
        "profit_factor":    _r(report.profit_factor),
        "expectancy":       _r(report.expectancy),
        "sharpe_ratio":     _r(report.sharpe_ratio),
        "sortino_ratio":    _r(report.sortino_ratio),
        "calmar_ratio":     _r(report.calmar_ratio),
        "recovery_factor":  _r(report.recovery_factor),
        # Cost
        "avg_mae":          _r(report.avg_mae),
        "avg_mfe":          _r(report.avg_mfe),
        "total_commission": _r(report.total_commission),
        "total_spread_cost": _r(report.total_spread_cost),
    }

    # ------------------------------------------------------------------
    # Balance history (for equity curve chart)
    # ------------------------------------------------------------------
    balance_history = [
        {
            "t":  int(h["timestamp"]),          # UTC ms
            "w":  round(float(h["wallet"]), 2),
            "e":  round(float(h["equity"]), 2),
        }
        for h in report.balance_history
    ]

    # ------------------------------------------------------------------
    # Max drawdown
    # ------------------------------------------------------------------
    max_dd = None
    if report.max_drawdown is not None:
        dd = report.max_drawdown
        max_dd = {
            "start_time":       dd.start_time,
            "end_time":         dd.end_time,
            "peak_balance":     _r(dd.peak_balance),
            "trough_balance":   _r(dd.trough_balance),
            "drawdown_amount":  _r(dd.drawdown_amount),
            "drawdown_percent": _r(dd.drawdown_percent),
        }

    # ------------------------------------------------------------------
    # Significant drawdowns
    # ------------------------------------------------------------------
    sig_dds = []
    for dd in sorted(
        report.significant_drawdowns,
        key=lambda d: d.drawdown_percent,
        reverse=True,
    ):
        sig_dds.append({
            "start_time":       dd.start_time,
            "end_time":         dd.end_time,
            "peak_balance":     _r(dd.peak_balance),
            "trough_balance":   _r(dd.trough_balance),
            "drawdown_amount":  _r(dd.drawdown_amount),
            "drawdown_percent": _r(dd.drawdown_percent),
        })

    # ------------------------------------------------------------------
    # Weekday stats
    # ------------------------------------------------------------------
    WEEKDAY_ORDER = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    weekday_stats = []
    for day in WEEKDAY_ORDER:
        s = report.weekday_stats.get(day)
        if s:
            weekday_stats.append({
                "weekday":        s.weekday.capitalize(),
                "total_trades":   s.total_trades,
                "winning_trades": s.winning_trades,
                "losing_trades":  s.losing_trades,
                "total_pnl":      _r(s.total_pnl),
                "avg_pnl":        _r(s.avg_pnl),
                "win_rate":       _r(s.win_rate),
            })

    # ------------------------------------------------------------------
    # Session stats
    # ------------------------------------------------------------------
    SESSION_ORDER = ["london", "new_york", "tokyo", "sydney", "off_hours"]
    SESSION_LABELS = {
        "london":    "London",
        "new_york":  "New York",
        "tokyo":     "Tokyo",
        "sydney":    "Sydney",
        "off_hours": "Off-Hours",
    }
    session_stats = []
    for key in SESSION_ORDER:
        s = report.session_stats.get(key)
        if s:
            session_stats.append({
                "session":        SESSION_LABELS.get(key, key),
                "total_trades":   s.total_trades,
                "winning_trades": s.winning_trades,
                "losing_trades":  s.losing_trades,
                "total_pnl":      _r(s.total_pnl),
                "avg_pnl":        _r(s.avg_pnl),
                "win_rate":       _r(s.win_rate),
            })

    # ------------------------------------------------------------------
    # Monthly stats
    # ------------------------------------------------------------------
    monthly_stats = []
    for key in sorted(report.monthly_stats.keys()):
        s = report.monthly_stats[key]
        monthly_stats.append({
            "month":          s.month,
            "total_trades":   s.total_trades,
            "winning_trades": s.winning_trades,
            "losing_trades":  s.losing_trades,
            "total_pnl":      _r(s.total_pnl),
            "avg_pnl":        _r(s.avg_pnl),
            "win_rate":       _r(s.win_rate),
        })

    # ------------------------------------------------------------------
    # Trade markers (for equity curve dots)
    # ------------------------------------------------------------------
    trade_markers = []
    for m in report.trade_markers:
        trade_markers.append({
            "trade_id":       m["trade_id"],
            "symbol":         m["symbol"],
            "direction":      m["direction"],
            "open_time":      m["open_time"],
            "close_time":     m["close_time"],
            "entry_price":    _r(m["entry_price"], 6),
            "exit_price":     _r(m["exit_price"], 6),
            "stop_loss":      _r(m["initial_stop_loss"], 6),
            "take_profit":    _r(m["take_profit"], 6) if m.get("take_profit") else None,
            "net_pnl":        _r(m["net_pnl"]),
            "pnl_r":          _r(m["pnl_r"], 3),
            "close_reason":   m["close_reason"],
            "is_win":         m["is_win"],
            "size":           _r(m["size"], 6),
            "leverage":       m.get("leverage", 1),
            "commission":     _r(m.get("commission", 0.0)),
            "spread_paid":    _r(m.get("spread_paid", 0.0)),
            "risk_amount":    _r(m.get("risk_amount", 0.0)),
            "mae":            _r(m.get("max_adverse_excursion", 0.0)),
            "mfe":            _r(m.get("max_favourable_excursion", 0.0)),
        })

    return {
        "config":           config_summary,
        "summary":          summary,
        "balance_history":  balance_history,
        "max_drawdown":     max_dd,
        "significant_drawdowns": sig_dds,
        "weekday_stats":    weekday_stats,
        "session_stats":    session_stats,
        "monthly_stats":    monthly_stats,
        "trade_markers":    trade_markers,
        "has_chart":        False,  # set to True by engine when chart is available
        "chart_port":       None,   # set by engine when chart server is running
    }
