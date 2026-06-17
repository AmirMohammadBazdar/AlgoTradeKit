"""
AlgoTradeKit.strategy._types
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Shared data types for the strategy module.

Signal        — entry signal emitted by a strategy (direction, price, SL, TP)
ExitSignal    — exit condition detected by a strategy (reason + target price)
StrategyResult— full output of BaseStrategy.run()
StrategyMode  — BACKTEST (all candles) or LIVE (last candle only)

v0.7.0: StrategyResult.drawings — list of visual drawing dicts the strategy
        wants rendered on the candle chart (passed to the visual module).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pandas as pd


# ---------------------------------------------------------------------------
# Mode
# ---------------------------------------------------------------------------

class StrategyMode(Enum):
    """
    Controls what BaseStrategy.run() returns.

    BACKTEST
        Process every candle in the dataset.  All signals from all candles
        are collected and returned.  Use this with the ``simulate`` module.

    LIVE
        Process every candle in the dataset (state is still built up
        correctly for stateful strategies), but only signals whose
        ``timestamp`` matches the last candle of the primary timeframe are
        returned.  Use this with the ``trade`` module.
    """
    BACKTEST = "backtest"
    LIVE = "live"


# ---------------------------------------------------------------------------
# Signal
# ---------------------------------------------------------------------------

@dataclass
class Signal:
    """
    Entry signal emitted by a strategy for a single candle.

    Attributes
    ----------
    direction : str
        ``"long"`` or ``"short"``.
    entry_price : float
        Suggested entry price (typically the candle's close).
    stop_loss : float
        Suggested stop-loss price.
    take_profit : float | None
        Suggested take-profit price.  ``None`` means the simulate/trade
        module should manage TP dynamically (trailing, risk-free levels, etc.).
    timestamp : int
        Open-time of the candle that generated the signal, in UTC
        milliseconds (same unit as ``AlgoTradeKit.data`` timestamps).
    candle_index : int
        Integer position of the signal candle inside
        ``data[primary_timeframe]``.
    timeframe : str
        Primary timeframe that generated the signal (e.g. ``"15m"``).
    metadata : dict
        Any extra data the strategy wants to attach (indicator values,
        sub-mode names, risk-reward ratio, etc.).
    """

    direction: str          # "long" | "short"
    entry_price: float
    stop_loss: float
    take_profit: float | None
    timestamp: int          # UTC ms
    candle_index: int
    timeframe: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.direction not in ("long", "short"):
            raise ValueError(
                f"Signal.direction must be 'long' or 'short', got {self.direction!r}."
            )
        if self.stop_loss == self.entry_price:
            raise ValueError("Signal.stop_loss must differ from entry_price.")

    @property
    def is_long(self) -> bool:
        """True for a long (buy) signal."""
        return self.direction == "long"

    @property
    def is_short(self) -> bool:
        """True for a short (sell) signal."""
        return self.direction == "short"

    @property
    def sl_distance(self) -> float:
        """Absolute price distance from entry to stop-loss."""
        return abs(self.entry_price - self.stop_loss)

    @property
    def risk_reward(self) -> float | None:
        """
        Risk/reward ratio (TP distance / SL distance).
        Returns ``None`` when ``take_profit`` is not set.
        """
        if self.take_profit is None or self.sl_distance == 0:
            return None
        return abs(self.take_profit - self.entry_price) / self.sl_distance

    def __repr__(self) -> str:
        tp = f"{self.take_profit:.4f}" if self.take_profit is not None else "None"
        return (
            f"<Signal {self.direction.upper()} @{self.entry_price:.4f} "
            f"SL={self.stop_loss:.4f} TP={tp} ts={self.timestamp}>"
        )


# ---------------------------------------------------------------------------
# ExitSignal
# ---------------------------------------------------------------------------

@dataclass
class ExitSignal:
    """
    Exit condition detected by a strategy.

    Strategies that implement ``detect_exit_signals()`` emit these when
    they detect that open positions should be closed.  The simulate/trade
    module decides how to act on them.

    Attributes
    ----------
    reason : str
        Human-readable exit reason.  Common values: ``"trend_reversal"``,
        ``"force_close"``, ``"trailing_stop"``, ``"time_limit"``.
        Strategies can define any custom reason string.
    exit_price : float | None
        Target exit price.  ``None`` means exit at market (current close).
    timestamp : int
        Candle timestamp in UTC milliseconds.
    candle_index : int
        Position of the exit candle in ``data[primary_timeframe]``.
    metadata : dict
        Any extra context (which indicator triggered the exit, etc.).
    """

    reason: str
    exit_price: float | None    # None = market price
    timestamp: int              # UTC ms
    candle_index: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        price = f"{self.exit_price:.4f}" if self.exit_price is not None else "market"
        return (
            f"<ExitSignal reason={self.reason!r} price={price} ts={self.timestamp}>"
        )


# ---------------------------------------------------------------------------
# StrategyResult
# ---------------------------------------------------------------------------

@dataclass
class StrategyResult:
    """
    Full output of ``BaseStrategy.run()``.

    Attributes
    ----------
    signals : list[Signal]
        Entry signals detected by the strategy.
        In BACKTEST mode: all signals across all candles.
        In LIVE mode: only signals whose timestamp matches the last candle.
    exit_signals : list[ExitSignal]
        Exit conditions detected by the strategy (may be empty).
    data : dict[str, pd.DataFrame]
        The enriched data dict returned by ``prepare_indicators()`` —
        each DataFrame has original OHLCV columns plus indicator columns.
        Useful for the visual module or post-analysis.
    mode : StrategyMode
        The mode that was used when ``run()`` was called.
    drawings : list[dict]
        Visual drawing objects the strategy wants rendered on the candle chart.
        Each dict follows the same format as the visual module's drawing dicts
        (type, coordinates, color, etc.).  Strategies populate this list in
        ``prepare_indicators()`` or ``generate_signals()`` to show support/
        resistance lines, signal zones, indicator levels, etc.

        Drawing dict format examples::

            # Horizontal line at a detected support level
            {"type": "hline", "price": 42000.0, "color": "#58a6ff",
             "lineWidth": 1, "lineStyle": 2, "label": "Support", "source": "server"}

            # Trend line
            {"type": "trendline", "time1": 1700000000, "price1": 40000.0,
             "time2": 1700003600, "price2": 41000.0, "color": "#e3b341",
             "lineWidth": 1, "source": "server"}

            # Signal zone box
            {"type": "box", "time1": 1700000000, "price1": 41000.0,
             "time2": 1700007200, "price2": 42000.0, "color": "#3fb950",
             "opacity": 0.1, "borderColor": "#3fb950", "source": "server"}
        
        All times must be in **Unix seconds** (not milliseconds) to match
        the chart frontend's coordinate system.
    """

    signals: list[Signal]
    exit_signals: list[ExitSignal]
    data: "dict[str, pd.DataFrame]"
    mode: StrategyMode
    drawings: list[dict] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    @property
    def long_signals(self) -> list[Signal]:
        """All long-direction signals."""
        return [s for s in self.signals if s.is_long]

    @property
    def short_signals(self) -> list[Signal]:
        """All short-direction signals."""
        return [s for s in self.signals if s.is_short]

    @property
    def has_signal(self) -> bool:
        """True if at least one entry signal was generated."""
        return bool(self.signals)

    @property
    def signal_count(self) -> int:
        """Total number of entry signals."""
        return len(self.signals)

    def __repr__(self) -> str:
        return (
            f"<StrategyResult mode={self.mode.value} "
            f"signals={self.signal_count} exits={len(self.exit_signals)} "
            f"drawings={len(self.drawings)}>"
        )
