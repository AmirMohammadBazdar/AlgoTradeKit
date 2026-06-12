"""
AlgoTradeKit.strategy.builtin.macd
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
MACDCrossoverStrategy — Built-in MACD crossover entry strategy.

Entry rules
-----------
Long  : MACD line crosses **above** the signal line (bullish crossover).
Short : MACD line crosses **below** the signal line (bearish crossover).

Stop-loss
---------
Long  : Lowest ``low``  over the last ``sl_lookback`` candles (swing low).
Short : Highest ``high`` over the last ``sl_lookback`` candles (swing high).

Take-profit
-----------
``None`` by default.  The simulate / trade module is expected to manage TP
via risk-reward levels, trailing stops, or dynamic targets.

Exit detection
--------------
``detect_exit_signals`` emits an ``ExitSignal(reason="macd_reversal")``
when the histogram changes sign (MACD crosses the signal line in the
opposite direction of the current trend).  The simulate/trade module decides
whether to act on it.

Parameters
----------
fast_length   : MACD fast EMA period          (default 12 — TV default)
slow_length   : MACD slow EMA period          (default 26 — TV default)
signal_length : MACD signal EMA period        (default 9  — TV default)
sl_lookback   : Lookback window for SL        (default 10 candles)
timeframe     : Primary timeframe to trade on (default ``"1h"``)
"""

from __future__ import annotations

import math

import pandas as pd

from ...indicator import MACD
from .._base import BaseStrategy
from .._types import ExitSignal, Signal, StrategyMode


class MACDCrossoverStrategy(BaseStrategy):
    """
    Built-in MACD Crossover Strategy.

    Example usage
    -------------
    ::

        import pandas as pd
        from AlgoTradeKit.strategy import MACDCrossoverStrategy, StrategyMode

        df = pd.read_csv("data/binance-futures_BTCUSDT_4h.csv")

        strategy = MACDCrossoverStrategy(timeframe="4h")
        result = strategy.run(df)

        for sig in result.signals:
            print(sig)

    Multi-timeframe usage
    ---------------------
    ::

        data = {
            "4h":  df_4h,
            "1d":  df_1d,   # higher-TF context
        }
        strategy = MACDCrossoverStrategy(timeframe="4h")
        result = strategy.run(data)

    Live mode (last candle only)
    ----------------------------
    ::

        result = strategy.run(data, mode=StrategyMode.LIVE)
        if result.has_signal:
            print("LIVE signal:", result.signals[0])
    """

    # Private column name prefix (avoids collisions with user columns)
    _COL_MACD   = "_atk_macd"
    _COL_SIG    = "_atk_macd_signal"
    _COL_HIST   = "_atk_macd_hist"

    def __init__(
        self,
        fast_length: int = 12,
        slow_length: int = 26,
        signal_length: int = 9,
        sl_lookback: int = 10,
        timeframe: str = "1h",
    ) -> None:
        """
        Parameters
        ----------
        fast_length   : Fast EMA period for MACD line  (default 12).
        slow_length   : Slow EMA period for MACD line  (default 26).
        signal_length : Signal EMA period              (default 9).
        sl_lookback   : Window used to find swing high/low for SL (default 10).
        timeframe     : Primary timeframe (default ``"1h"``).
        """
        if fast_length >= slow_length:
            raise ValueError(
                f"fast_length ({fast_length}) must be < slow_length ({slow_length})."
            )
        if fast_length < 1 or slow_length < 1 or signal_length < 1:
            raise ValueError("All length parameters must be >= 1.")
        if sl_lookback < 1:
            raise ValueError("sl_lookback must be >= 1.")

        self.fast_length = fast_length
        self.slow_length = slow_length
        self.signal_length = signal_length
        self.sl_lookback = sl_lookback
        self.primary_timeframe = timeframe

        # Skip the first few candles where MACD is NaN
        # (need slow_length bars for slow EMA + signal_length bars for signal)
        self.warmup_period = slow_length + signal_length - 1

    # ------------------------------------------------------------------
    # prepare_indicators
    # ------------------------------------------------------------------

    def prepare_indicators(
        self, data: dict[str, pd.DataFrame]
    ) -> dict[str, pd.DataFrame]:
        """Compute MACD on the primary timeframe and attach columns."""
        df = data[self.primary_timeframe].copy()

        macd_obj = MACD(
            df["close"],
            fast_length=self.fast_length,
            slow_length=self.slow_length,
            signal_length=self.signal_length,
        )

        # Use .values to avoid pandas index-alignment surprises
        df[self._COL_MACD] = macd_obj.macd.values
        df[self._COL_SIG]  = macd_obj.signal.values
        df[self._COL_HIST] = macd_obj.histogram.values

        data[self.primary_timeframe] = df
        return data

    # ------------------------------------------------------------------
    # generate_signals
    # ------------------------------------------------------------------

    def generate_signals(
        self, candle_index: int, data: dict[str, pd.DataFrame]
    ) -> list[Signal]:
        """
        Detect MACD crossovers and emit entry signals.

        A crossover is only valid when both the current and previous candles
        have non-NaN MACD and signal values.
        """
        if candle_index < 1:
            return []

        df   = data[self.primary_timeframe]
        curr = df.iloc[candle_index]
        prev = df.iloc[candle_index - 1]

        # Skip if any MACD column is NaN on either candle
        for val in (
            curr[self._COL_MACD], curr[self._COL_SIG],
            prev[self._COL_MACD], prev[self._COL_SIG],
        ):
            if _is_nan(val):
                return []

        c_macd, c_sig = float(curr[self._COL_MACD]), float(curr[self._COL_SIG])
        p_macd, p_sig = float(prev[self._COL_MACD]), float(prev[self._COL_SIG])

        bullish_cross = (p_macd < p_sig) and (c_macd >= c_sig)
        bearish_cross = (p_macd > p_sig) and (c_macd <= c_sig)

        if not (bullish_cross or bearish_cross):
            return []

        # --- Stop-loss: swing low / swing high over sl_lookback window ---
        start = max(0, candle_index - self.sl_lookback + 1)
        window = df.iloc[start: candle_index + 1]

        entry_price = float(curr["close"])
        timestamp   = int(curr["timestamp"])
        meta = {
            "macd":          c_macd,
            "macd_signal":   c_sig,
            "histogram":     float(curr[self._COL_HIST]),
            "fast_length":   self.fast_length,
            "slow_length":   self.slow_length,
            "signal_length": self.signal_length,
        }

        if bullish_cross:
            sl = float(window["low"].min())
            if sl >= entry_price:
                # Degenerate case: SL above entry (e.g. very short window)
                sl = entry_price * 0.995
            return [Signal(
                direction="long",
                entry_price=entry_price,
                stop_loss=sl,
                take_profit=None,
                timestamp=timestamp,
                candle_index=candle_index,
                timeframe=self.primary_timeframe,
                metadata=meta,
            )]

        # bearish_cross
        sl = float(window["high"].max())
        if sl <= entry_price:
            sl = entry_price * 1.005
        return [Signal(
            direction="short",
            entry_price=entry_price,
            stop_loss=sl,
            take_profit=None,
            timestamp=timestamp,
            candle_index=candle_index,
            timeframe=self.primary_timeframe,
            metadata=meta,
        )]

    # ------------------------------------------------------------------
    # detect_exit_signals
    # ------------------------------------------------------------------

    def detect_exit_signals(
        self, candle_index: int, data: dict[str, pd.DataFrame]
    ) -> list[ExitSignal]:
        """
        Emit an exit signal when the histogram changes sign.

        This happens when MACD and signal cross in the opposite direction —
        a simple reversal filter.  The simulate/trade module decides whether
        to act on it (e.g. close existing long on bearish reversal).
        """
        if candle_index < 1:
            return []

        df   = data[self.primary_timeframe]
        curr = df.iloc[candle_index]
        prev = df.iloc[candle_index - 1]

        for val in (curr[self._COL_HIST], prev[self._COL_HIST]):
            if _is_nan(val):
                return []

        c_hist = float(curr[self._COL_HIST])
        p_hist = float(prev[self._COL_HIST])

        # Histogram changed sign → MACD/signal cross in opposite direction
        if (p_hist > 0 and c_hist <= 0) or (p_hist < 0 and c_hist >= 0):
            return [ExitSignal(
                reason="macd_reversal",
                exit_price=float(curr["close"]),
                timestamp=int(curr["timestamp"]),
                candle_index=candle_index,
                metadata={
                    "histogram_prev": p_hist,
                    "histogram_curr": c_hist,
                },
            )]

        return []


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _is_nan(value: object) -> bool:
    """Return True if *value* is any variant of NaN (float nan, pandas NA, etc.)."""
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False
