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
        leverage=10,
        risk_per_trade=1.0,
        tp_mode="multi_rr",
        tp_levels=[1.0, 2.0, 3.0],
        primary_timeframe="1h",
        # v0.7.0 visualisation options
        show_chart=True,         # open candle chart with position boxes
        report_mode="webpage",   # open interactive report in browser
    )

    strategy_result = my_strategy.run(data)
    report = Simulate(config).run(strategy_result)

    print(report)

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
        (btc_strategy, btc_data, SimulateConfig(symbol="btcusdt")),
        (eth_strategy, eth_data, SimulateConfig(symbol="ethusdt")),
    ])

Public API
----------
Classes
    SimulateConfig       — full simulation configuration (v0.7.0: show_chart, report_mode)
    Simulate             — single-pair backtesting engine
    SimulateReport       — complete statistical report
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
    REPORT_MODE_*        (v0.7.0)
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
    # v0.7.0 report mode constants
    REPORT_MODE_NONE,
    REPORT_MODE_WEBPAGE,
    REPORT_MODE_SAVE,
    REPORT_MODE_BOTH,
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
    # v0.7.0 report mode constants
    "REPORT_MODE_NONE",
    "REPORT_MODE_WEBPAGE",
    "REPORT_MODE_SAVE",
    "REPORT_MODE_BOTH",
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
