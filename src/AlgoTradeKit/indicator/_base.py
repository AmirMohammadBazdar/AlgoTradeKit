"""
AlgoTradeKit.indicator._base
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Shared helpers used by all indicator classes.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Sequence

import pandas as pd

# ---------------------------------------------------------------------------
# Low-level math helpers (no third-party dependency)
# ---------------------------------------------------------------------------

def _to_series(data: pd.Series | list | Sequence) -> pd.Series:
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
# Streaming (incremental) state — v1.0.0
#
# Small state machines that reproduce the batch helpers above one value at a
# time, so every indicator can offer an O(1)-per-candle ``update()``. Each
# push consumes exactly one new bar and returns the value the corresponding
# batch column would hold for that bar (including the NaN warmup masking).
# ---------------------------------------------------------------------------

def _ieee_div(a: float, b: float) -> float:
    """Divide like pandas/numpy: 0/0 → NaN, x/0 → ±inf (Python `/` would raise)."""
    if b != 0.0:
        return a / b
    if a != a or a == 0.0:
        return float("nan")
    return math.inf if (a > 0.0) == (math.copysign(1.0, b) > 0.0) else -math.inf


class _EwmState:
    """
    Streaming replica of ``series.ewm(alpha=alpha, min_periods=min_periods,
    adjust=False).mean()`` — same arithmetic as pandas (seeding on the first
    non-NaN value, weight decay across NaN gaps, the constant-input guard and
    the min_periods mask), so pushed outputs match the batch column bit for
    bit.
    """

    __slots__ = ("alpha", "min_periods", "_avg", "_old_wt", "_nobs")

    def __init__(self, alpha: float, min_periods: int) -> None:
        self.alpha = alpha
        self.min_periods = min_periods
        self._avg = float("nan")
        self._old_wt = 1.0
        self._nobs = 0

    def push(self, value: float) -> float:
        cur = float(value)
        is_obs = cur == cur  # not NaN
        if is_obs:
            self._nobs += 1
        if self._avg == self._avg:  # already seeded
            # pandas ignore_na=False: the old weight decays on every bar,
            # observation or not.
            self._old_wt *= 1.0 - self.alpha
            if is_obs:
                if self._avg != cur:  # pandas guard: constant input keeps the exact value
                    self._avg = (self._old_wt * self._avg + self.alpha * cur) / (
                        self._old_wt + self.alpha
                    )
                self._old_wt = 1.0
        elif is_obs:
            self._avg = cur
        return self._avg if self._nobs >= self.min_periods else float("nan")


class _WindowState:
    """
    Streaming replica of ``series.rolling(window, min_periods=window)`` with
    aggregate ``kind`` ∈ {"mean", "sum", "max", "min", "wma"}. Any NaN inside
    the window yields NaN — identical to the batch behaviour when
    min_periods == window.
    """

    _KINDS = ("mean", "sum", "max", "min", "wma")

    __slots__ = ("window", "kind", "_values", "_weights", "_weight_sum")

    def __init__(self, window: int, kind: str) -> None:
        if kind not in self._KINDS:
            raise ValueError(f"[AlgoTradeKit] _WindowState: unknown kind '{kind}'.")
        self.window = window
        self.kind = kind
        self._values: deque[float] = deque(maxlen=window)
        self._weights = list(range(1, window + 1))
        self._weight_sum = sum(self._weights)

    def push(self, value: float) -> float:
        self._values.append(float(value))
        if len(self._values) < self.window or any(v != v for v in self._values):
            return float("nan")
        if self.kind == "mean":
            return sum(self._values) / self.window
        if self.kind == "sum":
            return sum(self._values)
        if self.kind == "max":
            return max(self._values)
        if self.kind == "min":
            return min(self._values)
        # "wma" — same arithmetic as _wma_series's per-window function
        return sum(w * v for w, v in zip(self._weights, self._values)) / self._weight_sum


def _ma_state(ma_type: str, length: int) -> _EwmState | _WindowState:
    """Streaming state matching the batch MA dispatch used by RSI and MACD."""
    key = ma_type.upper()
    if key == "EMA":
        return _EwmState(2.0 / (length + 1), length)
    if key == "SMMA":
        return _EwmState(1.0 / length, length)
    if key == "SMA":
        return _WindowState(length, "mean")
    if key == "WMA":
        return _WindowState(length, "wma")
    raise ValueError(f"[AlgoTradeKit] unknown ma_type '{ma_type}'.")


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class _BaseIndicator:
    """
    Minimal base shared by all indicator classes.

    Subclasses must implement ``compute()`` which populates ``self.result``.
    ``self.result`` is a dict of ``{label: pd.Series}`` representing each
    output line / histogram / etc.

    Streaming: every indicator also exposes ``update(...)`` — push exactly one
    new bar, get the new value(s) back, with the source and result series
    extended in place. Streaming state is built lazily on the first
    ``update()`` call by replaying the stored history once; each further call
    is O(1) (bounded by the indicator's own window). Not thread-safe: call
    ``update()`` from one thread.
    """

    _NAME: str = "Indicator"

    def __init__(self) -> None:
        self.result: dict[str, pd.Series] = {}
        # Streaming state — built lazily by the subclass on first update().
        self._stream: dict | None = None

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def to_dataframe(self) -> pd.DataFrame:
        """Return all output series as a single DataFrame."""
        return pd.DataFrame(self.result)

    # ------------------------------------------------------------------
    # Streaming helpers (used by subclass update() implementations)
    # ------------------------------------------------------------------

    @staticmethod
    def _append_value(series: pd.Series, value: float) -> pd.Series:
        """Return `series` with one float appended, keeping a clean RangeIndex."""
        return pd.concat(
            [series, pd.Series([float(value)], dtype="float64")], ignore_index=True
        )

    def _append_results(self, values: dict[str, float]) -> None:
        """Append one new bar's value to each named result series."""
        for key, value in values.items():
            self.result[key] = self._append_value(self.result[key], value)

    def __repr__(self) -> str:  # pragma: no cover
        keys = list(self.result.keys())
        return f"<{self._NAME} outputs={keys}>"
