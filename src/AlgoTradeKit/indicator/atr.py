"""
AlgoTradeKit.indicator.atr
~~~~~~~~~~~~~~~~~~~~~~~~~~~
Average True Range (ATR) — Wilder's smoothing method.

This is a TradingView-compatible implementation that matches the output of
Pine Script's built-in ``ta.atr(length)`` exactly.

Formula
-------
True Range (TR) is the greatest of:

    TR = max(
        high − low,
        |high − previous_close|,
        |low  − previous_close|
    )

ATR is the Wilder Moving Average (RMA) of TR:

    ATR(n) = RMA(TR, n)   where alpha = 1 / n

The first valid ATR value is computed using a simple average of the first
``period`` TR values (same as TradingView / standard Wilder initialisation).

Output keys (result dict)
--------------------------
``"atr"``  : ATR series (Wilder-smoothed, NaN for the first ``period`` bars)
``"tr"``   : Raw True Range series (NaN at index 0 — no previous close)

Examples
--------
::

    from AlgoTradeKit.indicator import ATR

    atr = ATR(df["high"], df["low"], df["close"], period=14)
    print(atr.atr)   # pd.Series
    print(atr.tr)    # pd.Series

    # Add to a DataFrame
    df["atr"] = atr.atr.values
    df["tr"]  = atr.tr.values
"""

from __future__ import annotations

import pandas as pd

from ._base import (
    _BaseIndicator,
    _check_length,
    _rma_series,
    _to_series,
    _true_range,
)


class ATR(_BaseIndicator):
    """
    Average True Range.

    Parameters
    ----------
    high   : pd.Series | list
        High price series.
    low    : pd.Series | list
        Low price series.
    close  : pd.Series | list
        Close price series.
    period : int
        Smoothing period.  Default: ``14``.

    Attributes
    ----------
    atr    : pd.Series
        ATR values (Wilder's smoothed).  NaN for the first ``period`` bars.
    tr     : pd.Series
        Raw True Range.  NaN at the first bar (no previous close).

    Output keys (``result`` dict)
    ------------------------------
    ``"atr"`` : ATR series
    ``"tr"``  : True Range series
    """

    _NAME = "ATR"

    #: Default period (matches TradingView default).
    DEFAULT_PERIOD: int = 14

    def __init__(
        self,
        high:   "pd.Series | list",
        low:    "pd.Series | list",
        close:  "pd.Series | list",
        period: int = DEFAULT_PERIOD,
    ) -> None:
        super().__init__()

        self.high   = _to_series(high)
        self.low    = _to_series(low)
        self.close  = _to_series(close)
        self.period = period

        n = len(self.close)
        if len(self.high) != n:
            raise ValueError("[AlgoTradeKit] ATR: 'high' length mismatch.")
        if len(self.low) != n:
            raise ValueError("[AlgoTradeKit] ATR: 'low' length mismatch.")

        # Need period + 1 bars: 1 for TR (requires prev close) + period for RMA
        _check_length(self.close, period + 1, "ATR")

        self._compute()

    # ------------------------------------------------------------------
    # Computation
    # ------------------------------------------------------------------

    def _compute(self) -> None:
        tr  = _true_range(self.high, self.low, self.close)
        atr = _rma_series(tr, self.period)

        self.result["tr"]  = tr
        self.result["atr"] = atr

    # ------------------------------------------------------------------
    # Convenience properties
    # ------------------------------------------------------------------

    @property
    def atr(self) -> pd.Series:
        """ATR series (Wilder's smoothing)."""
        return self.result["atr"]

    @property
    def tr(self) -> pd.Series:
        """Raw True Range series."""
        return self.result["tr"]
