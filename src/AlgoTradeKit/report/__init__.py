"""
AlgoTradeKit.report
====================
Interactive simulation report module.

Receives a ``SimulateReport`` and renders it as a fully-interactive web
page served locally at ``http://127.0.0.1:<port>/``.  The page shows:

* **Equity curve** — wallet balance and equity over time with zoom/pan,
  maximum-drawdown shading, significant-drawdown regions, and per-trade
  dots that open a detailed tooltip on hover.
* **Performance summary** — PnL, win rate, avg win/loss, largest win/loss.
* **Risk metrics** — Sharpe, Sortino, Calmar, recovery factor, profit
  factor, expectancy.
* **Trade statistics** — split by direction, close reason, and streak.
* **Drawdown table** — all drawdowns above the configured threshold,
  sorted by severity.
* **Time analysis** — per-weekday and per-Forex-session tables.
* **Monthly analysis** — full calendar breakdown.
* **Cost summary** — commissions, spread, MAE/MFE averages.
* **PDF/print export** — a single button triggers the browser's print
  dialog with print-optimised CSS.

Quick start
-----------
::

    from AlgoTradeKit.report import show_report

    report = Simulate(config).run(strategy_result)
    show_report(report, block=True)

Integration with simulate
--------------------------
Set ``SimulateConfig.report_mode`` to automatically show or save the
report after a simulation completes::

    config = SimulateConfig(
        report_mode="webpage",   # "none" | "webpage" | "save" | "both"
        show_chart=True,         # also open the candle chart
    )
    report = Simulate(config).run(strategy_result)

Save as standalone HTML
------------------------
::

    from AlgoTradeKit.report import save_report_html

    save_report_html(report, "report_output.html")

Live push + combined rendering (v1.0.0)
----------------------------------------
``ReportServer.push_update(report_or_payload)`` re-broadcasts fresh stats
to every open report page over the existing WebSocket — the page
re-renders in place (used by the live trader / ``run_live``).  The server
also takes a ``host`` parameter (same semantics and security warning as
the chart server).

Multi-pair aggregate report (one page for a whole portfolio)::

    from AlgoTradeKit.report import show_combined_report

    show_combined_report({"binance:BTCUSDT": report_a,
                          "mt5:EURUSD":      report_b}, block=True)

The combined page shows the merged trade list, the equity curve summed
across the accounts, portfolio-wide stats, and a per-pair breakdown
table.  ``build_combined_report_payload`` exposes the raw aggregate
payload; ``save_combined_report_html`` writes the standalone file.

Public API
----------
Functions
    show_report(report, block, port, title, on_open_chart, host)
    save_report_html(report, path)            — save as standalone HTML
    build_report_payload(report)              — JSON dict for custom use
    show_combined_report(pairs, block, port, title, host)
    save_combined_report_html(pairs, path)
    build_combined_report_payload(pairs)      — aggregate JSON dict

Classes
    ReportServer                              — low-level server class

Constants
    REPORT_MODE_NONE, REPORT_MODE_WEBPAGE,
    REPORT_MODE_SAVE, REPORT_MODE_BOTH
"""

from ._builder import build_combined_report_payload, build_report_payload
from ._display import (
    save_combined_report_html,
    save_report_html,
    show_combined_report,
    show_report,
)
from ._server import ReportServer

# Mode constants (mirrors SimulateConfig.report_mode values)
REPORT_MODE_NONE    = "none"
REPORT_MODE_WEBPAGE = "webpage"
REPORT_MODE_SAVE    = "save"
REPORT_MODE_BOTH    = "both"

__all__ = [
    "ReportServer",
    "build_report_payload",
    "build_combined_report_payload",
    "show_report",
    "show_combined_report",
    "save_report_html",
    "save_combined_report_html",
    "REPORT_MODE_NONE",
    "REPORT_MODE_WEBPAGE",
    "REPORT_MODE_SAVE",
    "REPORT_MODE_BOTH",
]
