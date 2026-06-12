"""
AlgoTradeKit — strategy.builtin
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Ready-to-use built-in strategies.

Each file in this package contains a single strategy class.
This mirrors the ``indicator`` package structure.

Strategies
----------
- MACDCrossoverStrategy : entry on MACD/signal line crossover
"""

from .macd import MACDCrossoverStrategy

__all__ = ["MACDCrossoverStrategy"]
