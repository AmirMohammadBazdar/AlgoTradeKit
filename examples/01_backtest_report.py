"""
Backtest a strategy and look at the result — chart + report.

    python examples/03_backtest_report.py [path/to/1m.csv]

Opens two browser tabs: the candle chart with every trade drawn on it, and the
report page with the stats and the balance curve.

v1.1.0 note — on the report's balance chart, **click** a trade dot. The details
box pins open, so its "Open on Candle Chart" button can actually be pressed;
before 1.1.0 the box followed the pointer and closed before you reached it.
Click anywhere outside, or press Esc, to close it.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _sample_data import one_minute_candles

from AlgoTradeKit.data import resample_ohlcv
from AlgoTradeKit.simulate import Simulate, SimulateConfig
from AlgoTradeKit.strategy import MACDCrossoverStrategy

# ── Data ──────────────────────────────────────────────────────────────
# A plain MACD crossover on 15m candles. Nothing clever — the point is to
# exercise the library, not to make money.
minutes = one_minute_candles(days=14)
candles = resample_ohlcv(minutes, "15m")
print(f"  {len(minutes)} 1m candles → {len(candles)} 15m candles")

# ── Strategy ──────────────────────────────────────────────────────────
strategy = MACDCrossoverStrategy(timeframe="15m")
result = strategy.run({"15m": candles})
print(f"  {len(result.signals)} signals")

# ── Simulation ────────────────────────────────────────────────────────
config = SimulateConfig(
    initial_balance=10_000,
    symbol="BTCUSDT",
    primary_timeframe="15m",  # must match the strategy's
    leverage=10,
    position_sizing="risk_percent",
    risk_per_trade=1.0,
    tp_mode="fixed_rr",
    tp_rr=2.0,
    show_chart=True,          # candle chart with the trades drawn on it
    report_mode="webpage",    # report page, linked to that chart
    chart_indicators=[        # computed in Python, shown in the chart's legend
        {"kind": "ema", "period": 50},
        {"kind": "macd"},
    ],
)

report = Simulate(config).run(result)

print(f"\n  {report.total_trades} trades · "
      f"win rate {report.win_rate:.1f}% · "
      f"net {report.total_pnl:+.2f} ({report.total_pnl_percent:+.2f}%)")
print("\n  Two tabs should be open. Ctrl+C when you are done looking.")
