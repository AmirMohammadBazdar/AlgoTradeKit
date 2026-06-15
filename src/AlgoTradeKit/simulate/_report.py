"""
AlgoTradeKit.simulate._report
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
All statistical analysis lives here.  The engine produces raw lists of
``ClosedTrade`` and balance-history dicts; ``build_report()`` turns them
into a fully-populated ``SimulateReport``.

The ``report`` module (future) will receive a ``SimulateReport`` and only
*display* or *export* it — no further computation needed there.

Public types
------------
DrawdownPeriod   — a single peak-to-trough drawdown event
SessionStats     — per-Forex-session aggregated statistics
WeekdayStats     — per-weekday aggregated statistics
MonthStats       — per-calendar-month aggregated statistics
SimulateReport   — the master report object

Public function
---------------
build_report(closed_trades, open_at_end, balance_history, config)
    → SimulateReport
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ._position import ClosedTrade, _InternalPosition
from ._session import (
    SESSION_LONDON, SESSION_NEW_YORK, SESSION_OFF_HOURS,
    SESSION_SYDNEY, SESSION_TOKYO,
    get_month_key, get_primary_session, get_weekday_name,
)

if TYPE_CHECKING:
    from ._config import SimulateConfig


# ---------------------------------------------------------------------------
# Sub-report types
# ---------------------------------------------------------------------------

@dataclass
class DrawdownPeriod:
    """
    A single peak-to-trough drawdown event in the equity curve.

    Attributes
    ----------
    start_time : int
        UTC ms timestamp of the equity peak that starts the drawdown.
    end_time : int
        UTC ms timestamp of the lowest equity point (trough).
    peak_balance : float
        Equity at the start of the drawdown (the peak).
    trough_balance : float
        Lowest equity reached during this drawdown.
    drawdown_amount : float
        ``peak_balance - trough_balance`` in account currency.
    drawdown_percent : float
        ``drawdown_amount / peak_balance * 100``.
    """
    start_time: int
    end_time: int
    peak_balance: float
    trough_balance: float
    drawdown_amount: float
    drawdown_percent: float


@dataclass
class SessionStats:
    """Aggregated trade statistics for one Forex trading session."""
    session: str           # "london" | "new_york" | "tokyo" | "sydney" | "off_hours"
    total_trades: int
    winning_trades: int
    losing_trades: int
    break_even_trades: int
    total_pnl: float
    avg_pnl: float
    win_rate: float        # 0–100


@dataclass
class WeekdayStats:
    """Aggregated trade statistics for one weekday (UTC open-time)."""
    weekday: str           # "monday" | "tuesday" | … | "sunday"
    total_trades: int
    winning_trades: int
    losing_trades: int
    break_even_trades: int
    total_pnl: float
    avg_pnl: float
    win_rate: float        # 0–100


@dataclass
class MonthStats:
    """Aggregated trade statistics for one calendar month."""
    month: str             # "YYYY-MM"
    total_trades: int
    winning_trades: int
    losing_trades: int
    break_even_trades: int
    total_pnl: float
    avg_pnl: float
    win_rate: float        # 0–100


# ---------------------------------------------------------------------------
# Master report
# ---------------------------------------------------------------------------

@dataclass
class SimulateReport:
    """
    Complete backtesting report for one simulation run.

    Passed to the ``report`` module (future) for display or export.
    All statistics are pre-computed — the report module performs no maths.

    Attributes
    ----------
    config : SimulateConfig
        The configuration used for this run.
    initial_balance : float
        Starting wallet balance.
    final_balance : float
        Wallet balance after all trades are closed.
    total_pnl : float
        ``final_balance - initial_balance`` (net of all commissions).
    total_pnl_percent : float
        ``total_pnl / initial_balance * 100``.
    total_trades : int
        Total number of completed trades (excludes ``open_at_end``).
    long_trades / short_trades : int
        Split by direction.
    winning_trades / losing_trades / break_even_trades : int
        Split by outcome (net_pnl > 0 / < 0 / == 0).
    sl_count / tp_count / force_close_count / risk_free_count / end_of_data_count : int
        Trade count by close reason.
    win_rate / loss_rate : float
        Percentage (0–100) of winning / losing trades.
    avg_win / avg_loss : float
        Average net PnL of winning / losing trades in account currency.
    largest_win / largest_loss : float
        Best and worst single trade net PnL.
    avg_pnl_r : float
        Average trade PnL expressed in R-multiples.
    max_consecutive_wins / max_consecutive_losses : int
        Longest run of wins or losses in chronological order.
    profit_factor : float
        Gross profit / abs(gross loss).  ``inf`` if no losing trades.
    expectancy : float
        ``avg_win * win_rate_decimal - avg_loss * loss_rate_decimal``
        — expected net PnL per trade in account currency.
    sharpe_ratio : float | None
        Candle-by-candle Sharpe ratio (not annualised).
        ``None`` when fewer than 2 data points.
    sortino_ratio : float | None
        Sharpe variant using downside deviation only.
    calmar_ratio : float | None
        ``total_pnl_percent / max_drawdown.drawdown_percent``.
        ``None`` when no drawdown occurred.
    recovery_factor : float | None
        ``total_pnl / max_drawdown.drawdown_amount``.
    max_drawdown : DrawdownPeriod | None
        The single largest peak-to-trough drawdown.
    significant_drawdowns : list[DrawdownPeriod]
        All drawdowns >= ``config.drawdown_threshold`` percent, oldest first.
    weekday_stats : dict[str, WeekdayStats]
        Keyed by lowercase weekday name, e.g. ``"monday"``.
    monthly_stats : dict[str, MonthStats]
        Keyed by ``"YYYY-MM"``, e.g. ``"2024-01"``.
    session_stats : dict[str, SessionStats]
        Keyed by session name.  Includes all five session keys even if empty.
    balance_history : list[dict]
        One entry per primary-TF candle::

            {"timestamp": int, "wallet": float, "equity": float}

    closed_trades : list[ClosedTrade]
        All completed trades in chronological order.
    open_at_end : list[dict]
        Simplified dicts for positions still open at the last candle.
    trade_markers : list[dict]
        Per-trade metadata for the visual report (chart dots / links).
    avg_mae : float
        Average max-adverse-excursion across all trades ($ terms).
    avg_mfe : float
        Average max-favourable-excursion across all trades ($ terms).
    total_commission : float
        Sum of all commissions paid.
    total_spread_cost : float
        Sum of all spread costs paid.
    """

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------
    config: "SimulateConfig"

    # ------------------------------------------------------------------
    # Balance
    # ------------------------------------------------------------------
    initial_balance: float
    final_balance: float
    total_pnl: float
    total_pnl_percent: float

    # ------------------------------------------------------------------
    # Trade counts
    # ------------------------------------------------------------------
    total_trades: int
    long_trades: int
    short_trades: int
    winning_trades: int
    losing_trades: int
    break_even_trades: int

    sl_count: int
    tp_count: int
    force_close_count: int
    risk_free_count: int
    end_of_data_count: int

    # ------------------------------------------------------------------
    # Win/loss metrics
    # ------------------------------------------------------------------
    win_rate: float          # 0–100
    loss_rate: float         # 0–100
    avg_win: float
    avg_loss: float
    largest_win: float
    largest_loss: float
    avg_pnl_r: float         # average R-multiple

    # ------------------------------------------------------------------
    # Streaks
    # ------------------------------------------------------------------
    max_consecutive_wins: int
    max_consecutive_losses: int

    # ------------------------------------------------------------------
    # Risk metrics
    # ------------------------------------------------------------------
    profit_factor: float
    expectancy: float
    sharpe_ratio: float | None
    sortino_ratio: float | None
    calmar_ratio: float | None
    recovery_factor: float | None

    # ------------------------------------------------------------------
    # Drawdown
    # ------------------------------------------------------------------
    max_drawdown: DrawdownPeriod | None
    significant_drawdowns: list[DrawdownPeriod]

    # ------------------------------------------------------------------
    # Time analysis
    # ------------------------------------------------------------------
    weekday_stats: dict[str, WeekdayStats]
    monthly_stats: dict[str, MonthStats]
    session_stats: dict[str, SessionStats]

    # ------------------------------------------------------------------
    # Raw data (for visual report)
    # ------------------------------------------------------------------
    balance_history: list[dict]
    closed_trades: list[ClosedTrade]
    open_at_end: list[dict]
    trade_markers: list[dict]

    # ------------------------------------------------------------------
    # Excursion / cost summary
    # ------------------------------------------------------------------
    avg_mae: float
    avg_mfe: float
    total_commission: float
    total_spread_cost: float

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        sign = "+" if self.total_pnl >= 0 else ""
        return (
            f"<SimulateReport '{self.config.config_id}' "
            f"trades={self.total_trades} "
            f"pnl={sign}{self.total_pnl:.2f} ({sign}{self.total_pnl_percent:.1f}%) "
            f"wr={self.win_rate:.1f}%>"
        )


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

def build_report(
    closed_trades: list[ClosedTrade],
    open_at_end: list[_InternalPosition],
    balance_history: list[dict],
    config: "SimulateConfig",
) -> SimulateReport:
    """
    Compute the full ``SimulateReport`` from raw simulation outputs.

    Parameters
    ----------
    closed_trades : list[ClosedTrade]
        All trades completed during the simulation.
    open_at_end : list[_InternalPosition]
        Positions still open at the final candle (informational only;
        the engine closes them at market before calling this).
    balance_history : list[dict]
        One ``{"timestamp", "wallet", "equity"}`` dict per primary-TF candle.
    config : SimulateConfig
        The simulation configuration.
    """

    initial = config.initial_balance
    final   = balance_history[-1]["equity"] if balance_history else initial

    # ------------------------------------------------------------------
    # 1. Basic counts & PnL split
    # ------------------------------------------------------------------
    total     = len(closed_trades)
    wins      = [t for t in closed_trades if t.net_pnl > 0]
    losses    = [t for t in closed_trades if t.net_pnl < 0]
    be_trades = [t for t in closed_trades if t.net_pnl == 0]

    longs  = [t for t in closed_trades if t.direction == "long"]
    shorts = [t for t in closed_trades if t.direction == "short"]

    sl_count    = sum(1 for t in closed_trades if t.is_sl)
    tp_count    = sum(1 for t in closed_trades if t.is_tp)
    fc_count    = sum(1 for t in closed_trades if t.is_force_close)
    rf_count    = sum(1 for t in closed_trades if t.is_risk_free)
    eod_count   = sum(1 for t in closed_trades if t.is_end_of_data)

    win_rate  = len(wins) / total * 100 if total else 0.0
    loss_rate = len(losses) / total * 100 if total else 0.0

    avg_win     = (sum(t.net_pnl for t in wins) / len(wins)) if wins else 0.0
    avg_loss    = (sum(t.net_pnl for t in losses) / len(losses)) if losses else 0.0
    largest_win  = max((t.net_pnl for t in wins), default=0.0)
    largest_loss = min((t.net_pnl for t in losses), default=0.0)
    avg_r       = (sum(t.pnl_r for t in closed_trades) / total) if total else 0.0

    # ------------------------------------------------------------------
    # 2. Streaks
    # ------------------------------------------------------------------
    max_cw, max_cl = _compute_streaks(closed_trades)

    # ------------------------------------------------------------------
    # 3. Risk metrics
    # ------------------------------------------------------------------
    gross_profit = sum(t.net_pnl for t in wins)
    gross_loss   = abs(sum(t.net_pnl for t in losses))
    pf = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

    # Expectancy: E[win] * P(win) - E[loss] * P(loss)
    win_r  = win_rate / 100
    loss_r = loss_rate / 100
    expectancy = avg_win * win_r - abs(avg_loss) * loss_r

    sharpe  = _compute_sharpe(balance_history)
    sortino = _compute_sortino(balance_history)

    # ------------------------------------------------------------------
    # 4. Drawdown
    # ------------------------------------------------------------------
    max_dd, sig_dds = _compute_drawdowns(balance_history, config.drawdown_threshold)

    total_pnl = final - initial
    pnl_pct   = total_pnl / initial * 100 if initial else 0.0

    calmar = (
        pnl_pct / max_dd.drawdown_percent
        if max_dd and max_dd.drawdown_percent > 0
        else None
    )
    recovery = (
        total_pnl / max_dd.drawdown_amount
        if max_dd and max_dd.drawdown_amount > 0
        else None
    )

    # ------------------------------------------------------------------
    # 5. Session / weekday / monthly stats
    # ------------------------------------------------------------------
    session_stats  = _compute_grouped_stats(closed_trades, get_primary_session, "session")
    weekday_stats  = _compute_grouped_stats(closed_trades, get_weekday_name, "weekday")
    monthly_stats  = _compute_grouped_stats(closed_trades, get_month_key, "month")

    # Ensure all five sessions always present
    _fill_session_defaults(session_stats)

    # ------------------------------------------------------------------
    # 6. MAE / MFE / costs
    # ------------------------------------------------------------------
    avg_mae = (
        sum(t.max_adverse_excursion for t in closed_trades) / total if total else 0.0
    )
    avg_mfe = (
        sum(t.max_favourable_excursion for t in closed_trades) / total if total else 0.0
    )
    total_commission  = sum(t.commission for t in closed_trades)
    total_spread_cost = sum(t.spread_paid for t in closed_trades)

    # ------------------------------------------------------------------
    # 7. Trade markers (for visual chart linking)
    # ------------------------------------------------------------------
    trade_markers = _build_trade_markers(closed_trades)

    # ------------------------------------------------------------------
    # 8. Open-at-end summary (informational)
    # ------------------------------------------------------------------
    open_dicts = [
        {
            "trade_id": p.trade_id,
            "symbol": p.symbol,
            "direction": p.direction,
            "entry_price": p.entry_price,
            "open_time": p.open_time,
            "stop_loss": p.stop_loss,
            "take_profit": p.take_profit,
            "margin_amount": p.margin_amount,
            "risk_amount": p.risk_amount,
        }
        for p in open_at_end
    ]

    return SimulateReport(
        config=config,
        initial_balance=initial,
        final_balance=final,
        total_pnl=total_pnl,
        total_pnl_percent=pnl_pct,
        total_trades=total,
        long_trades=len(longs),
        short_trades=len(shorts),
        winning_trades=len(wins),
        losing_trades=len(losses),
        break_even_trades=len(be_trades),
        sl_count=sl_count,
        tp_count=tp_count,
        force_close_count=fc_count,
        risk_free_count=rf_count,
        end_of_data_count=eod_count,
        win_rate=win_rate,
        loss_rate=loss_rate,
        avg_win=avg_win,
        avg_loss=avg_loss,
        largest_win=largest_win,
        largest_loss=largest_loss,
        avg_pnl_r=avg_r,
        max_consecutive_wins=max_cw,
        max_consecutive_losses=max_cl,
        profit_factor=pf,
        expectancy=expectancy,
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        calmar_ratio=calmar,
        recovery_factor=recovery,
        max_drawdown=max_dd,
        significant_drawdowns=sig_dds,
        session_stats=session_stats,
        weekday_stats=weekday_stats,
        monthly_stats=monthly_stats,
        balance_history=balance_history,
        closed_trades=closed_trades,
        open_at_end=open_dicts,
        trade_markers=trade_markers,
        avg_mae=avg_mae,
        avg_mfe=avg_mfe,
        total_commission=total_commission,
        total_spread_cost=total_spread_cost,
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _compute_streaks(trades: list[ClosedTrade]) -> tuple[int, int]:
    """Return (max_consecutive_wins, max_consecutive_losses)."""
    max_w = max_l = curr_w = curr_l = 0
    for t in trades:
        if t.net_pnl > 0:
            curr_w += 1; curr_l = 0
            max_w = max(max_w, curr_w)
        elif t.net_pnl < 0:
            curr_l += 1; curr_w = 0
            max_l = max(max_l, curr_l)
        else:
            curr_w = curr_l = 0
    return max_w, max_l


def _compute_sharpe(balance_history: list[dict]) -> float | None:
    """Candle-period Sharpe ratio (mean return / std return)."""
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
    n = len(returns)
    mean_r = sum(returns) / n
    var = sum((r - mean_r) ** 2 for r in returns) / (n - 1)
    std_r = math.sqrt(var)
    return (mean_r / std_r) if std_r > 0 else None


def _compute_sortino(balance_history: list[dict]) -> float | None:
    """Sortino ratio using downside-only standard deviation."""
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
    mean_r = sum(returns) / len(returns)
    neg = [r for r in returns if r < 0]
    if not neg:
        return None
    downside_var = sum(r ** 2 for r in neg) / len(neg)
    downside_std = math.sqrt(downside_var)
    return (mean_r / downside_std) if downside_std > 0 else None


def _compute_drawdowns(
    balance_history: list[dict],
    threshold_pct: float,
) -> tuple[DrawdownPeriod | None, list[DrawdownPeriod]]:
    """
    Compute the maximum drawdown and all significant drawdowns.

    Returns
    -------
    tuple[DrawdownPeriod | None, list[DrawdownPeriod]]
        ``(max_drawdown, significant_drawdowns_above_threshold)``
    """
    if not balance_history:
        return None, []

    # ---  max drawdown  ---
    peak_eq   = balance_history[0]["equity"]
    peak_ts   = balance_history[0]["timestamp"]
    trough_eq = peak_eq
    trough_ts = peak_ts
    max_dd_pct = 0.0
    max_dd_obj: DrawdownPeriod | None = None

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
                max_dd_obj = DrawdownPeriod(
                    start_time=peak_ts,
                    end_time=trough_ts,
                    peak_balance=peak_eq,
                    trough_balance=trough_eq,
                    drawdown_amount=peak_eq - trough_eq,
                    drawdown_percent=dd_pct,
                )

    # ---  significant drawdowns  ---
    sig: list[DrawdownPeriod] = []
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
                    sig.append(DrawdownPeriod(
                        start_time=dd_start_ts,
                        end_time=dd_trough_ts,
                        peak_balance=dd_peak,
                        trough_balance=dd_trough,
                        drawdown_amount=dd_peak - dd_trough,
                        drawdown_percent=dd_pct,
                    ))
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

    # Close any open drawdown at the end of data
    if in_dd:
        dd_pct = (dd_peak - dd_trough) / dd_peak * 100 if dd_peak > 0 else 0.0
        if dd_pct >= threshold_pct:
            sig.append(DrawdownPeriod(
                start_time=dd_start_ts,
                end_time=dd_trough_ts,
                peak_balance=dd_peak,
                trough_balance=dd_trough,
                drawdown_amount=dd_peak - dd_trough,
                drawdown_percent=dd_pct,
            ))

    return max_dd_obj, sig


def _compute_grouped_stats(
    trades: list[ClosedTrade],
    key_fn,          # callable(timestamp_ms) → str
    stat_type: str,  # "session" | "weekday" | "month"
) -> dict:
    """Group trades by a time-based key and compute aggregated stats."""
    groups: dict[str, list[ClosedTrade]] = {}
    for t in trades:
        key = key_fn(t.open_time)
        groups.setdefault(key, []).append(t)

    result = {}
    for key, group in sorted(groups.items()):
        wins = [t for t in group if t.net_pnl > 0]
        losses = [t for t in group if t.net_pnl < 0]
        be = [t for t in group if t.net_pnl == 0]
        total_pnl = sum(t.net_pnl for t in group)
        wr = len(wins) / len(group) * 100 if group else 0.0
        avg = total_pnl / len(group) if group else 0.0

        if stat_type == "session":
            result[key] = SessionStats(
                session=key,
                total_trades=len(group),
                winning_trades=len(wins),
                losing_trades=len(losses),
                break_even_trades=len(be),
                total_pnl=total_pnl,
                avg_pnl=avg,
                win_rate=wr,
            )
        elif stat_type == "weekday":
            result[key] = WeekdayStats(
                weekday=key,
                total_trades=len(group),
                winning_trades=len(wins),
                losing_trades=len(losses),
                break_even_trades=len(be),
                total_pnl=total_pnl,
                avg_pnl=avg,
                win_rate=wr,
            )
        else:
            result[key] = MonthStats(
                month=key,
                total_trades=len(group),
                winning_trades=len(wins),
                losing_trades=len(losses),
                break_even_trades=len(be),
                total_pnl=total_pnl,
                avg_pnl=avg,
                win_rate=wr,
            )
    return result


def _fill_session_defaults(session_stats: dict) -> None:
    """Ensure all five session keys are present (empty stats if no trades)."""
    _all = [SESSION_LONDON, SESSION_NEW_YORK, SESSION_TOKYO, SESSION_SYDNEY, SESSION_OFF_HOURS]
    for s in _all:
        if s not in session_stats:
            session_stats[s] = SessionStats(
                session=s, total_trades=0, winning_trades=0, losing_trades=0,
                break_even_trades=0, total_pnl=0.0, avg_pnl=0.0, win_rate=0.0,
            )


def _build_trade_markers(trades: list[ClosedTrade]) -> list[dict[str, Any]]:
    """
    Build the list of per-trade marker dicts consumed by the visual report.

    Each dict contains everything the chart widget needs to render a dot on
    the balance curve and open the visual chart on click.
    """
    markers = []
    for t in trades:
        markers.append({
            "trade_id":           t.trade_id,
            "symbol":             t.symbol,
            "direction":          t.direction,
            "open_time":          t.open_time,
            "close_time":         t.close_time,
            "entry_price":        t.entry_price,
            "exit_price":         t.exit_price,
            "initial_stop_loss":  t.initial_stop_loss,
            "final_stop_loss":    t.final_stop_loss,
            "take_profit":        t.take_profit,
            "net_pnl":            t.net_pnl,
            "pnl_r":              t.pnl_r,
            "close_reason":       t.close_reason,
            "is_win":             t.is_win,
            "signal_candle_index": t.signal_candle_index,
            "leverage":           t.leverage,
            "spread_paid":        t.spread_paid,
            "commission":         t.commission,
            "size":               t.size,
            "margin_amount":      t.margin_amount,
            "risk_amount":        t.risk_amount,
            "max_adverse_excursion":    t.max_adverse_excursion,
            "max_favourable_excursion": t.max_favourable_excursion,
        })
    return markers
