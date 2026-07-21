"""
AlgoTradeKit — strategy module
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Strategy framework for building, running, and combining trading strategies.

Lifecycle
---------
Every strategy subclasses :class:`BaseStrategy` and implements:

1. ``prepare_indicators(data)``  — add indicator columns to DataFrames (once).
2. ``generate_signals(i, data)`` — return entry :class:`Signal` list per candle.

Optionally overrides:

3. ``setup(data)``                  — initialise stateful variables (once).
4. ``detect_exit_signals(i, data)`` — return :class:`ExitSignal` list per candle.

Usage
-----
::

    from AlgoTradeKit.strategy import BaseStrategy, Signal, StrategyMode
    from AlgoTradeKit.indicator import MACD

    class MyCrossover(BaseStrategy):
        primary_timeframe = "4h"

        def prepare_indicators(self, data):
            df = data["4h"].copy()
            m = MACD(df["close"])
            df["_macd"] = m.macd.values
            df["_sig"]  = m.signal.values
            data["4h"] = df
            return data

        def generate_signals(self, i, data):
            if i < 1:
                return []
            df   = data["4h"]
            curr, prev = df.iloc[i], df.iloc[i-1]
            if prev["_macd"] < prev["_sig"] and curr["_macd"] >= curr["_sig"]:
                return [Signal(
                    direction="long",
                    entry_price=float(curr["close"]),
                    stop_loss=float(df.iloc[max(0,i-10):i+1]["low"].min()),
                    take_profit=None,
                    timestamp=int(curr["timestamp"]),
                    candle_index=i,
                    timeframe=self.primary_timeframe,
                )]
            return []

Incremental (live) computation — v1.0.0
---------------------------------------
For live stepping without full recomputes, strategies may override the
optional ``update_indicators(data, new_index)`` hook (O(1) per closed
candle, supports SMC/price-action ``self.*`` state); strategies without the
hook fall back to a tail recompute of ``prepare_indicators`` over the last
``recompute_window`` candles.  Driven by:

- :func:`advance_live_candle` — append one closed candle, update
  indicators (hook or tail recompute), return that candle's signals.
- :func:`evaluate_forming_candle` — evaluate a forming candle on a
  throwaway copy; committed state and master data stay untouched.
- :func:`default_recompute_window` / :func:`has_update_hook` — helpers.

Built-in strategies
-------------------
- :class:`MACDCrossoverStrategy` — MACD line / signal line crossover entries.
"""

from ._base import BaseStrategy
from ._incremental import (
    advance_live_candle,
    default_recompute_window,
    evaluate_forming_candle,
    has_update_hook,
)
from ._types import ExitSignal, Signal, StrategyMode, StrategyResult
from .builtin.macd import MACDCrossoverStrategy

__all__ = [
    # Core
    "BaseStrategy",
    # Types
    "Signal",
    "ExitSignal",
    "StrategyResult",
    "StrategyMode",
    # Incremental (live) computation
    "advance_live_candle",
    "evaluate_forming_candle",
    "default_recompute_window",
    "has_update_hook",
    # Built-in strategies
    "MACDCrossoverStrategy",
]
