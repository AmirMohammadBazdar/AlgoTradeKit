"""
AlgoTradeKit.simulate
~~~~~~~~~~~~~~~~~~~~~~
Backtesting engine, position management, and reporting for AlgoTradeKit.

Quick start
-----------
Single strategy, single config::

    from AlgoTradeKit.simulate import Simulate, SimulateConfig

    config = SimulateConfig(
        initial_balance=10_000,
        symbol="btcusdt",
        exchange_type="exchange",
        leverage=10,
        spread=0.5,
        commission_type="percentage",
        commission=0.001,           # 0.1 % per side
        risk_per_trade=1.0,         # 1 % of balance per trade
        tp_mode="multi_rr",
        tp_levels=[1.0, 2.0, 3.0],
        sl_mode="signal",
        risk_free_enabled=True,
        risk_free_at_rr=1.0,
        max_positions=3,
        max_long_positions=2,
        max_short_positions=2,
        primary_timeframe="1h",
    )

    strategy_result = my_strategy.run(data)
    report = Simulate(config).run(strategy_result)

    print(report)
    # → <SimulateReport 'BTCUSDT_risk1.0pct_lev10x_tp_multi_rr_[1.0_2.0_3.0]'
    #       trades=47 pnl=+1243.80 (+12.4%) wr=61.7%>

Batch config sweep::

    from AlgoTradeKit.simulate import run_batch

    configs = [
        SimulateConfig(risk_per_trade=0.5, tp_mode="fixed_rr", tp_rr=1.5),
        SimulateConfig(risk_per_trade=1.0, tp_mode="fixed_rr", tp_rr=2.0),
        SimulateConfig(risk_per_trade=2.0, tp_mode="fixed_rr", tp_rr=3.0),
    ]
    reports = run_batch(strategy, data, configs)
    best = max(reports, key=lambda r: r.total_pnl)

Multi-pair portfolio (shared wallet)::

    from AlgoTradeKit.simulate import run_multi

    report = run_multi([
        (btc_strategy, btc_data, SimulateConfig(symbol="btcusdt", risk_per_trade=1.0)),
        (eth_strategy, eth_data, SimulateConfig(symbol="ethusdt", risk_per_trade=0.5)),
    ])

Public API
----------
Classes
    SimulateConfig       — full simulation configuration
    Simulate             — single-pair backtesting engine
    SimulateReport       — complete statistical report (input to ``report`` module)
    ClosedTrade          — immutable record of one completed trade
    DrawdownPeriod       — one peak-to-trough drawdown event
    SessionStats         — per-Forex-session trade statistics
    WeekdayStats         — per-weekday trade statistics
    MonthStats           — per-calendar-month trade statistics

Functions
    run_batch(strategy, data, configs)  — parallel config sweep
    run_multi(pairs, initial_balance)   — shared-wallet portfolio simulation

String constants (for SimulateConfig fields)
    EXCHANGE_TYPE_*      COMMISSION_TYPE_*      SIZING_*
    TP_MODE_*            SL_MODE_*
"""

from ._config import (
    COMMISSION_TYPE_FIXED,
    COMMISSION_TYPE_PERCENTAGE,
    COMMISSION_TYPE_PER_LOT,
    EXCHANGE_TYPE_EXCHANGE,
    EXCHANGE_TYPE_METATRADER,
    SIZING_FIXED_AMOUNT,
    SIZING_FIXED_LOT,
    SIZING_RISK_PERCENT,
    SL_MODE_SIGNAL,
    SL_MODE_TRAILING,
    TP_MODE_FIXED_RR,
    TP_MODE_MULTI_RR,
    TP_MODE_NONE,
    TP_MODE_SIGNAL,
    SimulateConfig,
)
from ._engine import Simulate
from ._position import (
    CLOSE_REASON_EOD,
    CLOSE_REASON_FC,
    CLOSE_REASON_RF,
    CLOSE_REASON_SL,
    CLOSE_REASON_TP,
    ClosedTrade,
)
from ._report import (
    DrawdownPeriod,
    MonthStats,
    SessionStats,
    SimulateReport,
    WeekdayStats,
    build_report,
)
from ._runner import run_batch, run_multi

__all__ = [
    # Config
    "SimulateConfig",
    "EXCHANGE_TYPE_EXCHANGE",
    "EXCHANGE_TYPE_METATRADER",
    "COMMISSION_TYPE_PERCENTAGE",
    "COMMISSION_TYPE_PER_LOT",
    "COMMISSION_TYPE_FIXED",
    "SIZING_RISK_PERCENT",
    "SIZING_FIXED_AMOUNT",
    "SIZING_FIXED_LOT",
    "TP_MODE_SIGNAL",
    "TP_MODE_FIXED_RR",
    "TP_MODE_MULTI_RR",
    "TP_MODE_NONE",
    "SL_MODE_SIGNAL",
    "SL_MODE_TRAILING",
    # Engine
    "Simulate",
    # Position records
    "ClosedTrade",
    "CLOSE_REASON_SL",
    "CLOSE_REASON_TP",
    "CLOSE_REASON_RF",
    "CLOSE_REASON_FC",
    "CLOSE_REASON_EOD",
    # Report
    "SimulateReport",
    "DrawdownPeriod",
    "SessionStats",
    "WeekdayStats",
    "MonthStats",
    "build_report",
    # Runners
    "run_batch",
    "run_multi",
]
