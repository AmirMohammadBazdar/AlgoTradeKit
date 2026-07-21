"""
AlgoTradeKit.report._builder
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Serialise a ``SimulateReport`` into a JSON-safe dict that the report
HTML page can consume directly.

All numeric values are rounded to a sensible precision.  ``None`` values
are preserved as ``null`` in JSON.  Timestamps remain as UTC milliseconds
(the chart's Chart.js dataset uses them directly).

Public functions
----------------
build_report_payload(report: SimulateReport) → dict
build_combined_report_payload(pairs) → dict          (v1.0.0)
    Aggregate payload over several pairs: merged trade list, summed
    equity curve across accounts, per-pair breakdown section.

Module boundary
---------------
``report`` never imports ``simulate`` at runtime (the dependency arrow
points the other way).  ``SimulateReport`` / ``ClosedTrade`` objects are
read duck-typed; the handful of portfolio-level formulas that cannot be
derived from per-pair numbers (streaks, sharpe/sortino, drawdowns) are
mirrored locally from ``simulate/_report.py`` and parity-locked by test:
a combined payload over ONE pair must equal ``build_report_payload`` of
that pair exactly.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from AlgoTradeKit.simulate._report import SimulateReport

WEEKDAY_ORDER = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
SESSION_ORDER = ["london", "new_york", "tokyo", "sydney", "off_hours"]
SESSION_LABELS = {
    "london":    "London",
    "new_york":  "New York",
    "tokyo":     "Tokyo",
    "sydney":    "Sydney",
    "off_hours": "Off-Hours",
}


def _r(v: float | None, n: int = 2) -> float | None:
    """Round float to *n* decimal places or return None."""
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    return round(float(v), n)


# ---------------------------------------------------------------------------
# Shared payload fragments (single and combined paths use the same mapping)
# ---------------------------------------------------------------------------

def _config_summary(cfg) -> dict:
    """Serialise a ``SimulateConfig`` into the payload's config section."""
    return {
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


def _marker_payload(m: dict) -> dict:
    """Map one simulate trade-marker dict onto the payload marker shape."""
    return {
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
    }


def _balance_history_payload(balance_history: list[dict]) -> list[dict]:
    """Compact the equity curve for the payload ({t, w, e} per snapshot)."""
    return [
        {
            "t":  int(h["timestamp"]),          # UTC ms
            "w":  round(float(h["wallet"]), 2),
            "e":  round(float(h["equity"]), 2),
        }
        for h in balance_history
    ]


def _dd_payload(dd) -> dict:
    """Serialise one drawdown period (attribute- or dict-shaped)."""
    get = dd.get if isinstance(dd, dict) else lambda k: getattr(dd, k)
    return {
        "start_time":       get("start_time"),
        "end_time":         get("end_time"),
        "peak_balance":     _r(get("peak_balance")),
        "trough_balance":   _r(get("trough_balance")),
        "drawdown_amount":  _r(get("drawdown_amount")),
        "drawdown_percent": _r(get("drawdown_percent")),
    }


def _grouped_stats_payload(rows: list[dict], label_key: str) -> list[dict]:
    """Serialise weekday/session/month rows (already ordered, exact numbers)."""
    return [
        {
            label_key:        row["label"],
            "total_trades":   row["total_trades"],
            "winning_trades": row["winning_trades"],
            "losing_trades":  row["losing_trades"],
            "total_pnl":      _r(row["total_pnl"]),
            "avg_pnl":        _r(row["avg_pnl"]),
            "win_rate":       _r(row["win_rate"]),
        }
        for row in rows
    ]


def build_report_payload(report: SimulateReport) -> dict:
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
    # Max drawdown / significant drawdowns
    # ------------------------------------------------------------------
    max_dd = _dd_payload(report.max_drawdown) if report.max_drawdown is not None else None

    sig_dds = [
        _dd_payload(dd)
        for dd in sorted(
            report.significant_drawdowns,
            key=lambda d: d.drawdown_percent,
            reverse=True,
        )
    ]

    # ------------------------------------------------------------------
    # Weekday / session / monthly stats
    # ------------------------------------------------------------------
    weekday_rows = [
        _stats_obj_row(s, s.weekday.capitalize())
        for day in WEEKDAY_ORDER
        if (s := report.weekday_stats.get(day))
    ]
    session_rows = [
        _stats_obj_row(s, SESSION_LABELS.get(key, key))
        for key in SESSION_ORDER
        if (s := report.session_stats.get(key))
    ]
    monthly_rows = [
        _stats_obj_row(report.monthly_stats[key], report.monthly_stats[key].month)
        for key in sorted(report.monthly_stats.keys())
    ]

    return {
        "config":           _config_summary(report.config),
        "summary":          summary,
        "balance_history":  _balance_history_payload(report.balance_history),
        "max_drawdown":     max_dd,
        "significant_drawdowns": sig_dds,
        "weekday_stats":    _grouped_stats_payload(weekday_rows, "weekday"),
        "session_stats":    _grouped_stats_payload(session_rows, "session"),
        "monthly_stats":    _grouped_stats_payload(monthly_rows, "month"),
        "trade_markers":    [_marker_payload(m) for m in report.trade_markers],
        "has_chart":        False,  # set to True by engine when chart is available
        "chart_port":       None,   # set by engine when chart server is running
    }


def _stats_obj_row(s, label: str) -> dict:
    """Exact-number row from a WeekdayStats/SessionStats/MonthStats object."""
    return {
        "label":          label,
        "total_trades":   s.total_trades,
        "winning_trades": s.winning_trades,
        "losing_trades":  s.losing_trades,
        "total_pnl":      s.total_pnl,
        "avg_pnl":        s.avg_pnl,
        "win_rate":       s.win_rate,
    }


# ---------------------------------------------------------------------------
# Combined (multi-pair) payload — v1.0.0
# ---------------------------------------------------------------------------
#
# The formulas below mirror simulate/_report.py::build_report and its private
# helpers exactly (report may not import simulate — see module docstring).
# Parity is locked by test: build_combined_report_payload over one pair must
# equal build_report_payload of that pair.

def _mirror_streaks(trades: list) -> tuple[int, int]:
    """Mirror of simulate/_report._compute_streaks over ClosedTrade-shaped objects."""
    max_w = max_l = curr_w = curr_l = 0
    for t in trades:
        if t.net_pnl > 0:
            curr_w += 1
            curr_l = 0
            max_w = max(max_w, curr_w)
        elif t.net_pnl < 0:
            curr_l += 1
            curr_w = 0
            max_l = max(max_l, curr_l)
        else:
            curr_w = curr_l = 0
    return max_w, max_l


def _mirror_returns(balance_history: list[dict]) -> list[float] | None:
    if len(balance_history) < 3:
        return None
    equities = [h["equity"] for h in balance_history]
    returns = []
    for i in range(1, len(equities)):
        prev = equities[i - 1]
        if prev > 0:
            returns.append(equities[i] / prev - 1.0)
    if len(returns) < 2:
        return None
    return returns


def _mirror_sharpe(balance_history: list[dict]) -> float | None:
    """Mirror of simulate/_report._compute_sharpe."""
    returns = _mirror_returns(balance_history)
    if returns is None:
        return None
    n = len(returns)
    mean_r = sum(returns) / n
    var = sum((r - mean_r) ** 2 for r in returns) / (n - 1)
    std_r = math.sqrt(var)
    return (mean_r / std_r) if std_r > 0 else None


def _mirror_sortino(balance_history: list[dict]) -> float | None:
    """Mirror of simulate/_report._compute_sortino."""
    returns = _mirror_returns(balance_history)
    if returns is None:
        return None
    mean_r = sum(returns) / len(returns)
    neg = [r for r in returns if r < 0]
    if not neg:
        return None
    downside_std = math.sqrt(sum(r ** 2 for r in neg) / len(neg))
    return (mean_r / downside_std) if downside_std > 0 else None


def _mirror_drawdowns(
    balance_history: list[dict],
    threshold_pct: float,
) -> tuple[dict | None, list[dict]]:
    """Mirror of simulate/_report._compute_drawdowns (dict-shaped periods)."""
    if not balance_history:
        return None, []

    def _period(start_ts, end_ts, peak, trough):
        return {
            "start_time":       start_ts,
            "end_time":         end_ts,
            "peak_balance":     peak,
            "trough_balance":   trough,
            "drawdown_amount":  peak - trough,
            "drawdown_percent": (peak - trough) / peak * 100 if peak > 0 else 0.0,
        }

    # ---  max drawdown  ---
    peak_eq   = balance_history[0]["equity"]
    peak_ts   = balance_history[0]["timestamp"]
    trough_eq = peak_eq
    trough_ts = peak_ts
    max_dd_pct = 0.0
    max_dd_obj: dict | None = None

    for h in balance_history:
        eq = h["equity"]
        ts = h["timestamp"]
        if eq >= peak_eq:
            peak_eq = eq
            peak_ts = ts
            trough_eq = eq
            trough_ts = ts
        else:
            if eq < trough_eq:
                trough_eq = eq
                trough_ts = ts
            dd_pct = (peak_eq - trough_eq) / peak_eq * 100 if peak_eq > 0 else 0.0
            if dd_pct > max_dd_pct:
                max_dd_pct = dd_pct
                max_dd_obj = _period(peak_ts, trough_ts, peak_eq, trough_eq)

    # ---  significant drawdowns  ---
    sig: list[dict] = []
    peak_eq = balance_history[0]["equity"]
    peak_ts = balance_history[0]["timestamp"]
    in_dd   = False
    dd_peak = peak_eq
    dd_start_ts = peak_ts
    dd_trough = peak_eq
    dd_trough_ts = peak_ts

    for h in balance_history:
        eq = h["equity"]
        ts = h["timestamp"]
        if eq >= peak_eq:
            if in_dd:
                dd_pct = (dd_peak - dd_trough) / dd_peak * 100 if dd_peak > 0 else 0.0
                if dd_pct >= threshold_pct:
                    sig.append(_period(dd_start_ts, dd_trough_ts, dd_peak, dd_trough))
                in_dd = False
            peak_eq = eq
            peak_ts = ts
            dd_trough = eq
            dd_trough_ts = ts
        else:
            dd_pct_now = (peak_eq - eq) / peak_eq * 100 if peak_eq > 0 else 0.0
            if not in_dd and dd_pct_now >= threshold_pct:
                in_dd = True
                dd_peak = peak_eq
                dd_start_ts = peak_ts
                dd_trough = eq
                dd_trough_ts = ts
            elif in_dd and eq < dd_trough:
                dd_trough = eq
                dd_trough_ts = ts

    if in_dd:
        dd_pct = (dd_peak - dd_trough) / dd_peak * 100 if dd_peak > 0 else 0.0
        if dd_pct >= threshold_pct:
            sig.append(_period(dd_start_ts, dd_trough_ts, dd_peak, dd_trough))

    return max_dd_obj, sig


def _sum_balance_histories(entries: list[tuple[float, list[dict]]]) -> list[dict]:
    """
    Sum several accounts' equity curves over the union of their timestamps.

    Per account the value is held between its own snapshots (step-hold);
    before an account's first snapshot its ``initial_balance`` is used (the
    account exists from the start), and after its last snapshot the last
    value is held.
    """
    all_ts = sorted({int(h["timestamp"]) for _, hist in entries for h in hist})
    combined: list[dict] = []
    idxs = [0] * len(entries)
    lasts: list[tuple[float, float] | None] = [None] * len(entries)

    for ts in all_ts:
        w_sum = 0.0
        e_sum = 0.0
        for k, (initial, hist) in enumerate(entries):
            i = idxs[k]
            while i < len(hist) and int(hist[i]["timestamp"]) <= ts:
                lasts[k] = (float(hist[i]["wallet"]), float(hist[i]["equity"]))
                i += 1
            idxs[k] = i
            if lasts[k] is None:
                w_sum += float(initial)
                e_sum += float(initial)
            else:
                w_sum += lasts[k][0]
                e_sum += lasts[k][1]
        combined.append({"timestamp": ts, "wallet": w_sum, "equity": e_sum})
    return combined


def _sum_grouped_stats(stats_dicts: list[dict]) -> dict[str, dict]:
    """Key-wise sum of several weekday/session/month stats dicts (exact numbers)."""
    out: dict[str, dict] = {}
    for stats in stats_dicts:
        for key, s in stats.items():
            agg = out.setdefault(key, {
                "total_trades": 0,
                "winning_trades": 0,
                "losing_trades": 0,
                "total_pnl": 0.0,
            })
            agg["total_trades"]   += s.total_trades
            agg["winning_trades"] += s.winning_trades
            agg["losing_trades"]  += s.losing_trades
            agg["total_pnl"]      += s.total_pnl
    for agg in out.values():
        total = agg["total_trades"]
        agg["avg_pnl"]  = agg["total_pnl"] / total if total else 0.0
        agg["win_rate"] = agg["winning_trades"] / total * 100 if total else 0.0
    return out


def _merge_config_summaries(configs: list[dict], initial_balance: float) -> dict:
    """Per-field merge: identical across pairs → the value, else ``"mixed"``."""
    merged = {}
    for key in configs[0]:
        values = [c.get(key) for c in configs]
        merged[key] = values[0] if all(v == values[0] for v in values[1:]) else "mixed"
    n = len(configs)
    merged["config_id"] = f"portfolio({n} pairs)"
    if merged["symbol"] == "mixed":
        merged["symbol"] = f"{n} pairs"
    # These two stay numeric whatever the pairs look like: the portfolio
    # balance is the sum of the accounts, and drawdown_threshold drives the
    # combined-curve maths + the page header (first pair's, run_multi
    # precedent).
    merged["initial_balance"] = _r(initial_balance)
    merged["drawdown_threshold"] = configs[0]["drawdown_threshold"]
    return merged


def build_combined_report_payload(pairs) -> dict:
    """
    Build the aggregate payload over several pairs (v1.0.0).

    The page renders the portfolio at the top level — merged trade list,
    equity curve summed across the accounts, portfolio-wide stats — plus a
    per-pair breakdown section.  ``show_combined_report`` /
    ``save_combined_report_html`` / ``ReportServer.push_update`` all accept
    the result; the trader builds its combined report through here.

    Parameters
    ----------
    pairs : dict[str, SimulateReport] | iterable[tuple[str, SimulateReport]]
        Pair label → completed report.  Labels must be unique, non-empty
        strings (e.g. ``"binance-futures:BTCUSDT"``).

    Returns
    -------
    dict
        Same shape as ``build_report_payload`` plus ``"combined": True``
        and a ``"pairs"`` breakdown list; every trade marker additionally
        carries ``"pair"`` (its label) and ``"uid"`` (``"<label>#<id>"`` —
        per-pair trade-id sequences may collide).
    """
    items = list(pairs.items()) if isinstance(pairs, dict) else [tuple(p) for p in pairs]
    if not items:
        raise ValueError("build_combined_report_payload: pairs must not be empty")
    labels = [label for label, _ in items]
    if any(not isinstance(lb, str) or not lb for lb in labels):
        raise ValueError("build_combined_report_payload: every pair label must be a "
                         "non-empty string")
    if len(set(labels)) != len(labels):
        raise ValueError(f"build_combined_report_payload: duplicate pair labels in {labels}")
    reports = [r for _, r in items]

    # ------------------------------------------------------------------
    # Merged trades + summed equity curve (exact, unrounded inputs)
    # ------------------------------------------------------------------
    merged_trades = sorted(
        (t for r in reports for t in r.closed_trades),
        key=lambda t: t.close_time,
    )
    combined_history = _sum_balance_histories(
        [(r.initial_balance, r.balance_history) for r in reports]
    )

    # ------------------------------------------------------------------
    # Portfolio summary — formulas mirror simulate/_report.build_report
    # ------------------------------------------------------------------
    initial = sum(r.initial_balance for r in reports)
    final   = combined_history[-1]["equity"] if combined_history else initial

    total     = len(merged_trades)
    wins      = [t for t in merged_trades if t.net_pnl > 0]
    losses    = [t for t in merged_trades if t.net_pnl < 0]
    be_trades = [t for t in merged_trades if t.net_pnl == 0]
    longs     = [t for t in merged_trades if t.direction == "long"]
    shorts    = [t for t in merged_trades if t.direction == "short"]

    win_rate  = len(wins) / total * 100 if total else 0.0
    loss_rate = len(losses) / total * 100 if total else 0.0

    avg_win      = (sum(t.net_pnl for t in wins) / len(wins)) if wins else 0.0
    avg_loss     = (sum(t.net_pnl for t in losses) / len(losses)) if losses else 0.0
    largest_win  = max((t.net_pnl for t in wins), default=0.0)
    largest_loss = min((t.net_pnl for t in losses), default=0.0)
    avg_r        = (sum(t.pnl_r for t in merged_trades) / total) if total else 0.0

    max_cw, max_cl = _mirror_streaks(merged_trades)

    gross_profit = sum(t.net_pnl for t in wins)
    gross_loss   = abs(sum(t.net_pnl for t in losses))
    pf = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")
    expectancy = avg_win * (win_rate / 100) - abs(avg_loss) * (loss_rate / 100)

    total_pnl = final - initial
    pnl_pct   = total_pnl / initial * 100 if initial else 0.0

    dd_threshold = reports[0].config.drawdown_threshold
    max_dd, sig_dds = _mirror_drawdowns(combined_history, dd_threshold)
    calmar = (
        pnl_pct / max_dd["drawdown_percent"]
        if max_dd and max_dd["drawdown_percent"] > 0
        else None
    )
    recovery = (
        total_pnl / max_dd["drawdown_amount"]
        if max_dd and max_dd["drawdown_amount"] > 0
        else None
    )

    avg_mae = sum(t.max_adverse_excursion for t in merged_trades) / total if total else 0.0
    avg_mfe = sum(t.max_favourable_excursion for t in merged_trades) / total if total else 0.0

    summary = {
        "initial_balance":  _r(initial),
        "final_balance":    _r(final),
        "total_pnl":        _r(total_pnl),
        "total_pnl_percent": _r(pnl_pct),
        "total_trades":     total,
        "long_trades":      len(longs),
        "short_trades":     len(shorts),
        "winning_trades":   len(wins),
        "losing_trades":    len(losses),
        "break_even_trades": len(be_trades),
        "sl_count":         sum(1 for t in merged_trades if t.is_sl),
        "tp_count":         sum(1 for t in merged_trades if t.is_tp),
        "force_close_count": sum(1 for t in merged_trades if t.is_force_close),
        "risk_free_count":  sum(1 for t in merged_trades if t.is_risk_free),
        "end_of_data_count": sum(1 for t in merged_trades if t.is_end_of_data),
        "win_rate":         _r(win_rate),
        "loss_rate":        _r(loss_rate),
        "avg_win":          _r(avg_win),
        "avg_loss":         _r(avg_loss),
        "largest_win":      _r(largest_win),
        "largest_loss":     _r(largest_loss),
        "avg_pnl_r":        _r(avg_r, 3),
        "max_consecutive_wins":   max_cw,
        "max_consecutive_losses": max_cl,
        "profit_factor":    _r(pf),
        "expectancy":       _r(expectancy),
        "sharpe_ratio":     _r(_mirror_sharpe(combined_history)),
        "sortino_ratio":    _r(_mirror_sortino(combined_history)),
        "calmar_ratio":     _r(calmar),
        "recovery_factor":  _r(recovery),
        "avg_mae":          _r(avg_mae),
        "avg_mfe":          _r(avg_mfe),
        "total_commission": _r(sum(t.commission for t in merged_trades)),
        "total_spread_cost": _r(sum(t.spread_paid for t in merged_trades)),
    }

    # ------------------------------------------------------------------
    # Weekday / session / monthly — key-wise sums of the per-pair stats
    # ------------------------------------------------------------------
    weekday_agg = _sum_grouped_stats([r.weekday_stats for r in reports])
    session_agg = _sum_grouped_stats([r.session_stats for r in reports])
    monthly_agg = _sum_grouped_stats([r.monthly_stats for r in reports])

    weekday_rows = [
        {**weekday_agg[day], "label": day.capitalize()}
        for day in WEEKDAY_ORDER
        if day in weekday_agg
    ]
    session_rows = [
        {**session_agg[key], "label": SESSION_LABELS.get(key, key)}
        for key in SESSION_ORDER
        if key in session_agg
    ]
    monthly_rows = [
        {**monthly_agg[key], "label": key}
        for key in sorted(monthly_agg.keys())
    ]

    # ------------------------------------------------------------------
    # Merged trade markers — tagged with their pair
    # ------------------------------------------------------------------
    merged_markers = sorted(
        (
            {
                **_marker_payload(m),
                "pair": label,
                "uid": f"{label}#{m['trade_id']}",
            }
            for label, r in items
            for m in r.trade_markers
        ),
        key=lambda m: m["close_time"],
    )

    # ------------------------------------------------------------------
    # Per-pair breakdown section
    # ------------------------------------------------------------------
    pair_rows = [
        {
            "label":            label,
            "symbol":           r.config.symbol or "—",
            "config_id":        r.config.config_id,
            "initial_balance":  _r(r.initial_balance),
            "final_balance":    _r(r.final_balance),
            "total_pnl":        _r(r.total_pnl),
            "total_pnl_percent": _r(r.total_pnl_percent),
            "total_trades":     r.total_trades,
            "win_rate":         _r(r.win_rate),
            "profit_factor":    _r(r.profit_factor),
            "max_drawdown_percent": (
                _r(r.max_drawdown.drawdown_percent) if r.max_drawdown else None
            ),
            "sharpe_ratio":     _r(r.sharpe_ratio),
        }
        for label, r in items
    ]

    sig_dds_sorted = sorted(sig_dds, key=lambda d: d["drawdown_percent"], reverse=True)

    return {
        "config":           _merge_config_summaries(
            [_config_summary(r.config) for r in reports], initial
        ),
        "summary":          summary,
        "balance_history":  _balance_history_payload(combined_history),
        "max_drawdown":     _dd_payload(max_dd) if max_dd is not None else None,
        "significant_drawdowns": [_dd_payload(dd) for dd in sig_dds_sorted],
        "weekday_stats":    _grouped_stats_payload(weekday_rows, "weekday"),
        "session_stats":    _grouped_stats_payload(session_rows, "session"),
        "monthly_stats":    _grouped_stats_payload(monthly_rows, "month"),
        "trade_markers":    merged_markers,
        "combined":         True,
        "pairs":            pair_rows,
        "has_chart":        False,   # portfolio-level chart linking is +
        "chart_port":       None,
    }
