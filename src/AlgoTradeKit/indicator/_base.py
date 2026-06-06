"""
AlgoTradeKit.indicator._base
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Shared helpers used by all indicator classes.
"""

from __future__ import annotations

import math
from typing import Sequence, overload

import pandas as pd


# ---------------------------------------------------------------------------
# Low-level math helpers (no third-party dependency)
# ---------------------------------------------------------------------------

def _to_series(data: "pd.Series | list | Sequence") -> pd.Series:
    """Coerce input to a pandas Series with a clean RangeIndex."""
    if isinstance(data, pd.Series):
        return data.reset_index(drop=True)
    return pd.Series(data)


def _check_length(series: pd.Series, min_len: int, name: str) -> None:
    if len(series) < min_len:
        raise ValueError(
            f"[AlgoTradeKit] {name} requires at least {min_len} data points, "
            f"got {len(series)}."
        )


def _sma_series(series: pd.Series, length: int) -> pd.Series:
    """Rolling simple moving average — pure pandas, no TA-lib."""
    return series.rolling(window=length, min_periods=length).mean()


def _ema_series(series: pd.Series, length: int, wilder: bool = False) -> pd.Series:
    """
    Exponential moving average.

    wilder=False : standard EMA  (alpha = 2 / (length + 1))
    wilder=True  : Wilder/RMA    (alpha = 1 / length)
    """
    alpha = 1.0 / length if wilder else 2.0 / (length + 1)
    return series.ewm(alpha=alpha, min_periods=length, adjust=False).mean()


def _rma_series(series: pd.Series, length: int) -> pd.Series:
    """Wilder's smoothing = RMA (used internally by RSI)."""
    return _ema_series(series, length, wilder=True)


def _wma_series(series: pd.Series, length: int) -> pd.Series:
    """Linearly weighted moving average."""
    weights = list(range(1, length + 1))

    def _wmav(window):
        if window.isna().any():
            return float("nan")
        return sum(w * v for w, v in zip(weights, window)) / sum(weights)

    return series.rolling(window=length, min_periods=length).apply(_wmav, raw=False)


def _true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    return pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class _BaseIndicator:
    """
    Minimal base shared by all indicator classes.

    Subclasses must implement ``compute()`` which populates ``self.result``.
    ``self.result`` is a dict of ``{label: pd.Series}`` representing each
    output line / histogram / etc.
    """

    _NAME: str = "Indicator"

    def __init__(self) -> None:
        self.result: dict[str, pd.Series] = {}

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def to_dataframe(self) -> pd.DataFrame:
        """Return all output series as a single DataFrame."""
        return pd.DataFrame(self.result)

    def __repr__(self) -> str:  # pragma: no cover
        keys = list(self.result.keys())
        return f"<{self._NAME} outputs={keys}>"
