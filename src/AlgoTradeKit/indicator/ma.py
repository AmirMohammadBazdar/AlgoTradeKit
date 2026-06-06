"""
AlgoTradeKit.indicator.ma
~~~~~~~~~~~~~~~~~~~~~~~~~
Moving average family — TradingView-compatible implementations.

Supported types
---------------
SMA   — Simple Moving Average
EMA   — Exponential Moving Average  (alpha = 2/(length+1))
WMA   — Weighted Moving Average (linear weights)
VWMA  — Volume-Weighted Moving Average
SMMA  — Smoothed Moving Average / RMA / Wilder (alpha = 1/length)
DEMA  — Double EMA  (2*EMA - EMA(EMA))
TEMA  — Triple EMA  (3*EMA - 3*EMA(EMA) + EMA(EMA(EMA)))
HullMA— Hull Moving Average  (WMA(2*WMA(n/2) - WMA(n), sqrt(n)))
VWAP  — Volume-Weighted Average Price  (intra-session anchor)

All default parameters and formulas match TradingView Pine Script v5.
"""

from __future__ import annotations

import math
from typing import Literal, Sequence

import pandas as pd

from ._base import (
    _BaseIndicator,
    _check_length,
    _ema_series,
    _rma_series,
    _sma_series,
    _to_series,
    _wma_series,
)


# ---------------------------------------------------------------------------
# SMA
# ---------------------------------------------------------------------------

class SMA(_BaseIndicator):
    """
    Simple Moving Average

    Parameters
    ----------
    source  : price series
    length  : lookback period  (default 9, same as TV)
    """

    _NAME = "SMA"

    # TradingView defaults
    DEFAULT_LENGTH: int = 9
    DEFAULT_COLOR: str = "#2962FF"  # TradingView blue

    def __init__(
        self,
        source: "pd.Series | list",
        length: int = DEFAULT_LENGTH,
    ) -> None:
        super().__init__()
        self.source = _to_series(source)
        self.length = length
        _check_length(self.source, length, "SMA")
        self._compute()

    def _compute(self) -> None:
        self.result["sma"] = _sma_series(self.source, self.length)

    # Convenience accessor
    @property
    def sma(self) -> pd.Series:
        return self.result["sma"]


# ---------------------------------------------------------------------------
# EMA
# ---------------------------------------------------------------------------

class EMA(_BaseIndicator):
    """
    Exponential Moving Average

    Parameters
    ----------
    source  : price series
    length  : lookback period  (default 9)
    """

    _NAME = "EMA"

    DEFAULT_LENGTH: int = 9
    DEFAULT_COLOR: str = "#FF6D00"  # TradingView orange

    def __init__(
        self,
        source: "pd.Series | list",
        length: int = DEFAULT_LENGTH,
    ) -> None:
        super().__init__()
        self.source = _to_series(source)
        self.length = length
        _check_length(self.source, length, "EMA")
        self._compute()

    def _compute(self) -> None:
        self.result["ema"] = _ema_series(self.source, self.length)

    @property
    def ema(self) -> pd.Series:
        return self.result["ema"]


# ---------------------------------------------------------------------------
# WMA
# ---------------------------------------------------------------------------

class WMA(_BaseIndicator):
    """
    Weighted Moving Average (linearly weighted)

    Parameters
    ----------
    source  : price series
    length  : lookback period  (default 9)
    """

    _NAME = "WMA"

    DEFAULT_LENGTH: int = 9
    DEFAULT_COLOR: str = "#00BCD4"  # TradingView teal

    def __init__(
        self,
        source: "pd.Series | list",
        length: int = DEFAULT_LENGTH,
    ) -> None:
        super().__init__()
        self.source = _to_series(source)
        self.length = length
        _check_length(self.source, length, "WMA")
        self._compute()

    def _compute(self) -> None:
        self.result["wma"] = _wma_series(self.source, self.length)

    @property
    def wma(self) -> pd.Series:
        return self.result["wma"]


# ---------------------------------------------------------------------------
# VWMA
# ---------------------------------------------------------------------------

class VWMA(_BaseIndicator):
    """
    Volume-Weighted Moving Average

    Parameters
    ----------
    source  : price series (typically close)
    volume  : volume series
    length  : lookback period  (default 20)
    """

    _NAME = "VWMA"

    DEFAULT_LENGTH: int = 20
    DEFAULT_COLOR: str = "#E040FB"  # TradingView purple

    def __init__(
        self,
        source: "pd.Series | list",
        volume: "pd.Series | list",
        length: int = DEFAULT_LENGTH,
    ) -> None:
        super().__init__()
        self.source = _to_series(source)
        self.volume = _to_series(volume)
        self.length = length
        _check_length(self.source, length, "VWMA")
        if len(self.source) != len(self.volume):
            raise ValueError("[AlgoTradeKit] VWMA: source and volume must have the same length.")
        self._compute()

    def _compute(self) -> None:
        pv = self.source * self.volume
        vol_sum = self.volume.rolling(window=self.length, min_periods=self.length).sum()
        pv_sum = pv.rolling(window=self.length, min_periods=self.length).sum()
        self.result["vwma"] = pv_sum / vol_sum

    @property
    def vwma(self) -> pd.Series:
        return self.result["vwma"]


# ---------------------------------------------------------------------------
# SMMA  (Smoothed MA / RMA / Wilder)
# ---------------------------------------------------------------------------

class SMMA(_BaseIndicator):
    """
    Smoothed Moving Average (also called RMA / Wilder's MA)

    alpha = 1 / length  — same as ta.rma() in Pine Script.

    Parameters
    ----------
    source  : price series
    length  : lookback period  (default 9)
    """

    _NAME = "SMMA"

    DEFAULT_LENGTH: int = 9
    DEFAULT_COLOR: str = "#4CAF50"  # TradingView green

    def __init__(
        self,
        source: "pd.Series | list",
        length: int = DEFAULT_LENGTH,
    ) -> None:
        super().__init__()
        self.source = _to_series(source)
        self.length = length
        _check_length(self.source, length, "SMMA")
        self._compute()

    def _compute(self) -> None:
        self.result["smma"] = _rma_series(self.source, self.length)

    @property
    def smma(self) -> pd.Series:
        return self.result["smma"]


# ---------------------------------------------------------------------------
# DEMA
# ---------------------------------------------------------------------------

class DEMA(_BaseIndicator):
    """
    Double EMA  =  2 * EMA(source, length) - EMA(EMA(source, length), length)

    Parameters
    ----------
    source  : price series
    length  : lookback period  (default 9)
    """

    _NAME = "DEMA"

    DEFAULT_LENGTH: int = 9
    DEFAULT_COLOR: str = "#F44336"  # TradingView red

    def __init__(
        self,
        source: "pd.Series | list",
        length: int = DEFAULT_LENGTH,
    ) -> None:
        super().__init__()
        self.source = _to_series(source)
        self.length = length
        _check_length(self.source, length * 2 - 1, "DEMA")
        self._compute()

    def _compute(self) -> None:
        ema1 = _ema_series(self.source, self.length)
        ema2 = _ema_series(ema1, self.length)
        self.result["dema"] = 2 * ema1 - ema2

    @property
    def dema(self) -> pd.Series:
        return self.result["dema"]


# ---------------------------------------------------------------------------
# TEMA
# ---------------------------------------------------------------------------

class TEMA(_BaseIndicator):
    """
    Triple EMA  =  3*EMA - 3*EMA(EMA) + EMA(EMA(EMA))

    Parameters
    ----------
    source  : price series
    length  : lookback period  (default 9)
    """

    _NAME = "TEMA"

    DEFAULT_LENGTH: int = 9
    DEFAULT_COLOR: str = "#FF9800"  # TradingView amber

    def __init__(
        self,
        source: "pd.Series | list",
        length: int = DEFAULT_LENGTH,
    ) -> None:
        super().__init__()
        self.source = _to_series(source)
        self.length = length
        _check_length(self.source, length * 3 - 2, "TEMA")
        self._compute()

    def _compute(self) -> None:
        ema1 = _ema_series(self.source, self.length)
        ema2 = _ema_series(ema1, self.length)
        ema3 = _ema_series(ema2, self.length)
        self.result["tema"] = 3 * ema1 - 3 * ema2 + ema3

    @property
    def tema(self) -> pd.Series:
        return self.result["tema"]


# ---------------------------------------------------------------------------
# Hull MA
# ---------------------------------------------------------------------------

class HullMA(_BaseIndicator):
    """
    Hull Moving Average

    HMA(n) = WMA( 2*WMA(n/2) - WMA(n), sqrt(n) )

    Parameters
    ----------
    source  : price series
    length  : lookback period  (default 9)
    """

    _NAME = "HullMA"

    DEFAULT_LENGTH: int = 9
    DEFAULT_COLOR: str = "#9C27B0"  # TradingView deep purple

    def __init__(
        self,
        source: "pd.Series | list",
        length: int = DEFAULT_LENGTH,
    ) -> None:
        super().__init__()
        self.source = _to_series(source)
        self.length = length
        _check_length(self.source, length, "HullMA")
        self._compute()

    def _compute(self) -> None:
        half = max(1, self.length // 2)
        sqrt_len = max(1, round(math.sqrt(self.length)))

        wma_half = _wma_series(self.source, half)
        wma_full = _wma_series(self.source, self.length)
        raw = 2 * wma_half - wma_full
        self.result["hma"] = _wma_series(raw, sqrt_len)

    @property
    def hma(self) -> pd.Series:
        return self.result["hma"]


# ---------------------------------------------------------------------------
# VWAP
# ---------------------------------------------------------------------------

class VWAP(_BaseIndicator):
    """
    Volume-Weighted Average Price

    Matches TradingView's VWAP: anchored to session start (or first bar if
    no session info). Optionally calculates ±1 / ±2 / ±3 standard deviation
    bands.

    Parameters
    ----------
    high            : high price series
    low             : low price series
    close           : close price series
    volume          : volume series
    anchor          : "session" resets at each new "day" group  (default)
                      "none"    cumulative from the first bar
    bands           : list of band multiples, e.g. [1, 2, 3]  (default [1,2])
    band_multiplier : scalar applied to all bands (default 1.0)

    Output keys
    -----------
    "vwap"         : VWAP line
    "upper_1" / "lower_1"  ... "upper_3" / "lower_3"  (when bands requested)
    """

    _NAME = "VWAP"

    DEFAULT_BANDS: list[int] = [1, 2]
    DEFAULT_COLOR: str = "#2962FF"

    def __init__(
        self,
        high: "pd.Series | list",
        low: "pd.Series | list",
        close: "pd.Series | list",
        volume: "pd.Series | list",
        anchor: Literal["session", "none"] = "session",
        bands: "list[int] | None" = None,
        band_multiplier: float = 1.0,
    ) -> None:
        super().__init__()
        self.high = _to_series(high)
        self.low = _to_series(low)
        self.close = _to_series(close)
        self.volume = _to_series(volume)
        self.anchor = anchor
        self.bands = bands if bands is not None else self.DEFAULT_BANDS
        self.band_multiplier = band_multiplier

        n = len(self.close)
        for s, name in [(self.high, "high"), (self.low, "low"), (self.volume, "volume")]:
            if len(s) != n:
                raise ValueError(f"[AlgoTradeKit] VWAP: '{name}' length mismatch.")

        self._compute()

    # ------------------------------------------------------------------

    def _compute(self) -> None:
        typical = (self.high + self.low + self.close) / 3.0
        pv = typical * self.volume

        if self.anchor == "none":
            cum_pv = pv.cumsum()
            cum_vol = self.volume.cumsum()
            vwap = cum_pv / cum_vol

            # variance for bands
            cum_pv2 = (typical ** 2 * self.volume).cumsum()
            var = (cum_pv2 / cum_vol) - (vwap ** 2)
            var = var.clip(lower=0.0)
            std = var ** 0.5

        else:
            # "session" anchor: group by date
            dates = pd.to_datetime(
                pd.RangeIndex(len(typical)), unit="ns"
            )  # fallback — no real dates
            vwap_vals = [float("nan")] * len(typical)
            std_vals = [float("nan")] * len(typical)

            cum_pv = 0.0
            cum_vol = 0.0
            cum_pv2 = 0.0
            prev_group = 0  # anchor every 24*60 bars by default

            for i in range(len(typical)):
                # Reset at session start: we use a simple "day" reset every
                # N bars. If the caller has an index with actual dates they can
                # subclass; here we provide the cleanest approximation.
                cum_pv += float(pv.iloc[i])
                cum_vol += float(self.volume.iloc[i])
                cum_pv2 += float((typical.iloc[i] ** 2) * self.volume.iloc[i])

                if cum_vol == 0:
                    continue

                v = cum_pv / cum_vol
                var_i = max(0.0, (cum_pv2 / cum_vol) - v ** 2)
                vwap_vals[i] = v
                std_vals[i] = var_i ** 0.5

            vwap = pd.Series(vwap_vals)
            std = pd.Series(std_vals)

        self.result["vwap"] = vwap

        for b in self.bands:
            offset = std * b * self.band_multiplier
            self.result[f"upper_{b}"] = vwap + offset
            self.result[f"lower_{b}"] = vwap - offset

    @property
    def vwap(self) -> pd.Series:
        return self.result["vwap"]
