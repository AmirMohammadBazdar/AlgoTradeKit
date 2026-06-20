"""
AlgoTradeKit.indicator.ichimoku
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Ichimoku Kinko Hyo — TradingView Pine Script v5 compatible.

Lines
-----
Tenkan-sen   (Conversion Line)  = (highest_high(9)  + lowest_low(9))  / 2
Kijun-sen    (Base Line)        = (highest_high(26) + lowest_low(26)) / 2
Senkou Span A (Leading Span A)  = (tenkan + kijun) / 2   — plotted 26 bars ahead
Senkou Span B (Leading Span B)  = (highest_high(52) + lowest_low(52)) / 2 — plotted 26 bars ahead
Chikou Span  (Lagging Span)     = close — plotted 26 bars BEHIND

TradingView defaults
--------------------
tenkan_period  = 9
kijun_period   = 26
senkou_b_period= 52
displacement   = 26

Colours (TradingView default)
------------------------------
tenkan       : #B71C1C  (dark red)
kijun        : #1565C0  (dark blue)
senkou_a     : #26A69A  (teal / green)
senkou_b     : #EF5350  (red)
chikou       : #43A047  (green)
cloud_up     : rgba(38, 166, 154, 0.15)   (bullish cloud — Span A > Span B)
cloud_down   : rgba(239, 83, 80, 0.15)    (bearish cloud — Span A < Span B)

Key visual behaviour (matches TradingView)
------------------------------------------
- Senkou A and B are shifted 26 bars INTO THE FUTURE — the cloud extends
  past the last candle on the chart.
- Chikou is the close shifted 26 bars INTO THE PAST.
- The ``result`` dict contains index-aligned series where future/past bars
  are represented by NaN in the "current" index and the displaced values
  are in the corresponding shifted keys.
- A ``cloud_df()`` helper returns a DataFrame with the full cloud
  (current + future portion) ready for the visual module.
"""

from __future__ import annotations

import pandas as pd

from ._base import _BaseIndicator, _check_length, _to_series


class Ichimoku(_BaseIndicator):
    """
    Ichimoku Kinko Hyo

    Parameters
    ----------
    high            : high price series
    low             : low price series
    close           : close price series
    tenkan_period   : Conversion Line period   (default 9)
    kijun_period    : Base Line period          (default 26)
    senkou_b_period : Senkou Span B period      (default 52)
    displacement    : cloud / chikou shift      (default 26)

    Output keys (result dict)
    -------------------------
    "tenkan"        : Conversion Line (aligned to candle index)
    "kijun"         : Base Line
    "senkou_a"      : Span A  — shifted forward by `displacement` bars
    "senkou_b"      : Span B  — shifted forward by `displacement` bars
    "chikou"        : Lagging Span — close shifted back by `displacement`
    "cloud_future_a": Span A values for the `displacement` future bars
                      (NaN for past bars, values for future bars)
    "cloud_future_b": Span B values for the `displacement` future bars
    """

    _NAME = "Ichimoku"

    DEFAULT_TENKAN: int = 9
    DEFAULT_KIJUN: int = 26
    DEFAULT_SENKOU_B: int = 52
    DEFAULT_DISPLACEMENT: int = 26

    # TradingView colours
    COLOR_TENKAN: str = "#B71C1C"
    COLOR_KIJUN: str = "#1565C0"
    COLOR_SENKOU_A: str = "#26A69A"
    COLOR_SENKOU_B: str = "#EF5350"
    COLOR_CHIKOU: str = "#43A047"
    COLOR_CLOUD_UP: str = "rgba(38, 166, 154, 0.15)"
    COLOR_CLOUD_DOWN: str = "rgba(239, 83, 80, 0.15)"

    def __init__(
        self,
        high: "pd.Series | list",
        low: "pd.Series | list",
        close: "pd.Series | list",
        tenkan_period: int = DEFAULT_TENKAN,
        kijun_period: int = DEFAULT_KIJUN,
        senkou_b_period: int = DEFAULT_SENKOU_B,
        displacement: int = DEFAULT_DISPLACEMENT,
    ) -> None:
        super().__init__()
        self.high = _to_series(high)
        self.low = _to_series(low)
        self.close = _to_series(close)
        self.tenkan_period = tenkan_period
        self.kijun_period = kijun_period
        self.senkou_b_period = senkou_b_period
        self.displacement = displacement

        n = len(self.close)
        if len(self.high) != n:
            raise ValueError("[AlgoTradeKit] Ichimoku: 'high' length mismatch.")
        if len(self.low) != n:
            raise ValueError("[AlgoTradeKit] Ichimoku: 'low' length mismatch.")
        _check_length(self.close, max(tenkan_period, kijun_period, senkou_b_period), "Ichimoku")

        self._compute()

    # ------------------------------------------------------------------

    @staticmethod
    def _donchian_mid(high: pd.Series, low: pd.Series, period: int) -> pd.Series:
        """(highest_high + lowest_low) / 2 over `period` bars."""
        hh = high.rolling(window=period, min_periods=period).max()
        ll = low.rolling(window=period, min_periods=period).min()
        return (hh + ll) / 2.0

    def _compute(self) -> None:
        n = len(self.close)
        disp = self.displacement

        # ------ current-bar lines ------
        tenkan = self._donchian_mid(self.high, self.low, self.tenkan_period)
        kijun = self._donchian_mid(self.high, self.low, self.kijun_period)

        # ------ cloud (unshifted) ------
        span_a_raw = (tenkan + kijun) / 2.0
        span_b_raw = self._donchian_mid(self.high, self.low, self.senkou_b_period)

        # ------ Chikou = close shifted disp bars to the past ------
        # In the result we store at the ORIGINAL index so that value at
        # position i is the close from bar i+disp (looks back disp bars
        # relative to the current position on a chart).
        chikou = self.close.shift(-disp)

        # ------ Senkou A & B (forward-shifted, same index length as input) ------
        # Values at index i correspond to price action disp bars BEFORE i.
        # i.e. senkou_a[i] was computed from bar i-disp → visual projection forward.
        # In the DataFrame this means we .shift(+disp) the raw computation.
        senkou_a = span_a_raw.shift(disp)
        senkou_b = span_b_raw.shift(disp)

        # ------ Future cloud: extend beyond the last candle ------
        # We compute span_a_raw / span_b_raw for the last `disp` bars of data
        # and attach them as rows disp, 2*disp future bars.
        future_idx = pd.RangeIndex(n, n + disp)
        future_a = pd.Series([float("nan")] * disp, index=future_idx)
        future_b = pd.Series([float("nan")] * disp, index=future_idx)

        # The last `disp` values of raw are valid — place them in the future
        last_a = span_a_raw.iloc[-disp:].values
        last_b = span_b_raw.iloc[-disp:].values
        future_a[:] = last_a
        future_b[:] = last_b

        # ------ store ------
        self.result["tenkan"] = tenkan
        self.result["kijun"] = kijun
        self.result["senkou_a"] = senkou_a
        self.result["senkou_b"] = senkou_b
        self.result["chikou"] = chikou
        self.result["cloud_future_a"] = future_a
        self.result["cloud_future_b"] = future_b
        # Raw (unshifted) Senkou values — useful for strategy logic.
        # These are the "current-bar" cloud values before the displacement
        # shift is applied.  senkou_a[i] = (tenkan[i] + kijun[i]) / 2 and
        # senkou_b[i] = donchian_mid(high, low, senkou_b_period)[i].
        self.result["span_a_raw"] = span_a_raw
        self.result["span_b_raw"] = span_b_raw

    # ------------------------------------------------------------------
    # Convenience accessors
    # ------------------------------------------------------------------

    @property
    def tenkan(self) -> pd.Series:
        return self.result["tenkan"]

    @property
    def kijun(self) -> pd.Series:
        return self.result["kijun"]

    @property
    def senkou_a(self) -> pd.Series:
        return self.result["senkou_a"]

    @property
    def senkou_b(self) -> pd.Series:
        return self.result["senkou_b"]

    @property
    def chikou(self) -> pd.Series:
        return self.result["chikou"]

    @property
    def span_a_raw(self) -> pd.Series:
        """
        Unshifted Senkou Span A at the **current** bar index.

        Computed as ``(tenkan + kijun) / 2`` without the forward displacement.
        This is the "live" cloud value at each candle — useful in strategy
        entry/exit conditions where you need to know where the cloud *is now*
        rather than where it will appear on the chart 26 bars later.

        Equivalent to ``senkou_span_a_shifted`` in some legacy implementations.
        """
        return self.result["span_a_raw"]

    @property
    def span_b_raw(self) -> pd.Series:
        """
        Unshifted Senkou Span B at the **current** bar index.

        Computed as ``(highest_high + lowest_low) / 2`` over ``senkou_b_period``
        bars without the forward displacement.

        Equivalent to ``senkou_span_b_shifted`` in some legacy implementations.
        """
        return self.result["span_b_raw"]

    def cloud_df(self) -> pd.DataFrame:
        """
        Returns a DataFrame with the full cloud (past + future) suitable
        for direct consumption by the visual module.

        Columns: index, senkou_a, senkou_b, cloud_color
        cloud_color is "up" (bullish, A>B) or "down" (bearish, B>A) per bar.
        """
        n = len(self.senkou_a)
        disp = self.displacement

        sa = self.senkou_a.copy()
        sb = self.senkou_b.copy()

        # Append future extension
        future_idx = pd.RangeIndex(n, n + disp)
        sa_future = self.result["cloud_future_a"].copy()
        sb_future = self.result["cloud_future_b"].copy()

        sa = pd.concat([sa, sa_future])
        sb = pd.concat([sb, sb_future])

        df = pd.DataFrame({"senkou_a": sa, "senkou_b": sb})
        df["cloud_color"] = "neutral"
        mask_up = df["senkou_a"] > df["senkou_b"]
        mask_down = df["senkou_a"] < df["senkou_b"]
        df.loc[mask_up, "cloud_color"] = "up"
        df.loc[mask_down, "cloud_color"] = "down"
        return df

    # ------------------------------------------------------------------
    # Signal helpers (common TradingView alerts)
    # ------------------------------------------------------------------

    def tk_cross_bullish(self) -> pd.Series:
        """Tenkan crosses above Kijun."""
        t, k = self.tenkan, self.kijun
        return (t.shift(1) < k.shift(1)) & (t >= k)

    def tk_cross_bearish(self) -> pd.Series:
        """Tenkan crosses below Kijun."""
        t, k = self.tenkan, self.kijun
        return (t.shift(1) > k.shift(1)) & (t <= k)

    def price_above_cloud(self) -> pd.Series:
        """Close is above both Senkou A and B."""
        close = self.close
        return (close > self.senkou_a) & (close > self.senkou_b)

    def price_below_cloud(self) -> pd.Series:
        """Close is below both Senkou A and B."""
        close = self.close
        return (close < self.senkou_a) & (close < self.senkou_b)

    def price_in_cloud(self) -> pd.Series:
        """Close is inside the cloud."""
        return ~self.price_above_cloud() & ~self.price_below_cloud()

    def bullish_cloud(self) -> pd.Series:
        """Span A > Span B (green cloud)."""
        return self.senkou_a > self.senkou_b

    def bearish_cloud(self) -> pd.Series:
        """Span B > Span A (red cloud)."""
        return self.senkou_b > self.senkou_a

    def visual_config(self) -> dict:
        """
        Return dict describing the TradingView-style Ichimoku overlay config
        for the visual module.  The visual module uses this to render lines
        and the cloud fill ahead of current price.
        """
        disp = self.displacement
        return {
            "pane": "price",
            "type": "overlay",
            "lines": [
                {
                    "key": "tenkan",
                    "color": self.COLOR_TENKAN,
                    "width": 1,
                    "style": "solid",
                    "label": f"Tenkan({self.tenkan_period})",
                },
                {
                    "key": "kijun",
                    "color": self.COLOR_KIJUN,
                    "width": 1,
                    "style": "solid",
                    "label": f"Kijun({self.kijun_period})",
                },
                {
                    "key": "senkou_a",
                    "color": self.COLOR_SENKOU_A,
                    "width": 1,
                    "style": "solid",
                    "label": f"Span A",
                    "displacement": disp,
                },
                {
                    "key": "senkou_b",
                    "color": self.COLOR_SENKOU_B,
                    "width": 1,
                    "style": "solid",
                    "label": f"Span B ({self.senkou_b_period})",
                    "displacement": disp,
                },
                {
                    "key": "chikou",
                    "color": self.COLOR_CHIKOU,
                    "width": 1,
                    "style": "solid",
                    "label": "Chikou",
                    "displacement": -disp,
                },
            ],
            "cloud": {
                "upper_key": "senkou_a",
                "lower_key": "senkou_b",
                "color_up": self.COLOR_CLOUD_UP,
                "color_down": self.COLOR_CLOUD_DOWN,
                "displacement": disp,
                "extend_future": True,
            },
        }
