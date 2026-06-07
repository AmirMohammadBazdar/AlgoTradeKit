"""
AlgoTradeKit.visual.indicator_renderer
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Bridges the indicator module with the visual/Chart module.

Responsibilities
----------------
1. ``add_ma(chart, ma_obj)``        — overlays any MA line on the price pane
2. ``add_rsi(chart, rsi)``          — adds an RSI pane below the chart
3. ``add_macd(chart, macd)``        — adds a MACD pane below the chart
4. ``add_ichimoku(chart, ichi)``    — overlays all Ichimoku lines + cloud fill

Data format (must match IndicatorSeries.to_dict() exactly)
-----------------------------------------------------------
The frontend addIndicator() reads these flat keys directly:

    {
      "name":       str,               # legend label  (= ind.name)
      "data":       [{time, value}],   # time in UNIX SECONDS (not ms)
      "color":      str,               # hex colour
      "overlay":    bool,              # True → price pane, False → sub-pane
      "pane":       int,               # sub-pane index (ignored when overlay=True)
      "lineWidth":  int,
      "seriesType": "line"|"histogram"|"area"
    }

Timestamps
----------
AlgoTradeKit Collector CSVs use milliseconds.
lightweight-charts expects UNIX SECONDS.
All _series_to_tv() calls divide by 1000 when values > 1e10 (ms range).
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import pandas as pd

from .models import RawIndicator

if TYPE_CHECKING:  # pragma: no cover
    from AlgoTradeKit.visual.chart import Chart
    from AlgoTradeKit.indicator.rsi import RSI
    from AlgoTradeKit.indicator.macd import MACD
    from AlgoTradeKit.indicator.ichimoku import Ichimoku
    from AlgoTradeKit.indicator._base import _BaseIndicator


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _to_seconds(ts_series: pd.Series) -> pd.Series:
    """
    Normalise a timestamp series to UNIX seconds.
    If values are > 1e10 they are assumed to be milliseconds and divided by 1000.
    """
    ts = ts_series.reset_index(drop=True)
    if ts.max() > 1e10:
        return (ts // 1000).astype(int)
    return ts.astype(int)


def _series_to_tv(
    series: pd.Series,
    timestamps: "pd.Series | None" = None,
) -> list[dict]:
    """
    Convert a pandas Series to [{time: int_seconds, value: float}].
    NaN values are dropped (lightweight-charts gaps are handled natively).
    """
    if timestamps is None:
        ts = pd.Series(range(len(series)), dtype="int64")
    else:
        ts = _to_seconds(timestamps)

    result = []
    for t, v in zip(ts, series.reset_index(drop=True)):
        if pd.isna(v):
            continue
        result.append({"time": int(t), "value": float(v)})
    return result


def _series_to_hist(
    series: pd.Series,
    colors: pd.Series,
    timestamps: "pd.Series | None" = None,
) -> list[dict]:
    """
    Convert histogram Series to [{time, value, color}].
    lightweight-charts histogram series supports per-bar colour natively.
    """
    if timestamps is None:
        ts = pd.Series(range(len(series)), dtype="int64")
    else:
        ts = _to_seconds(timestamps)

    result = []
    for t, v, c in zip(ts, series.reset_index(drop=True), colors.reset_index(drop=True)):
        if pd.isna(v) or c is None or (isinstance(c, float) and math.isnan(c)):
            continue
        result.append({"time": int(t), "value": float(v), "color": str(c)})
    return result


def _next_pane(chart: "Chart") -> int:
    """
    Return the next free sub-pane index (1+).
    Reads .pane from both RawIndicator payloads and IndicatorSeries objects.
    """
    used = set()
    for ind in getattr(chart, "_indicators", []):
        if isinstance(ind, RawIndicator):
            used.add(ind.payload.get("pane", 0))
        else:
            used.add(getattr(ind, "pane", 0))
    p = 1
    while p in used:
        p += 1
    return p


def _push(chart: "Chart", name: str, payload: dict) -> None:
    """Append a RawIndicator to chart._indicators, initialising the list if needed."""
    if not hasattr(chart, "_indicators"):
        chart._indicators = []
    chart._indicators.append(RawIndicator(name=name, payload=payload))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def add_ma(
    chart: "Chart",
    ma_obj: "_BaseIndicator",
    timestamps: "pd.Series | None" = None,
    color: "str | None" = None,
    line_width: int = 1,
    title: "str | None" = None,
) -> None:
    """
    Overlay any MA line (SMA, EMA, WMA, SMMA, DEMA, TEMA, HullMA, VWMA, VWAP)
    on the price pane.

    Parameters
    ----------
    chart      : Chart instance
    ma_obj     : any MA indicator object
    timestamps : Unix-ms timestamp series aligned to the data
    color      : override colour (uses indicator DEFAULT_COLOR if None)
    line_width : line width in pixels (default 1)
    title      : legend label (auto-derived from indicator name if None)
    """
    result = ma_obj.result
    if not result:
        return

    default_color = getattr(ma_obj, "DEFAULT_COLOR", "#2962FF")
    line_color = color or default_color
    name = ma_obj._NAME
    length = getattr(ma_obj, "length", "")
    auto_title = f"{name}({length})" if length else name
    label = title or auto_title

    for key, series in result.items():
        if key.startswith("upper_") or key.startswith("lower_"):
            continue  # VWAP bands handled below
        _push(chart, label, {
            "name":       label,
            "data":       _series_to_tv(series, timestamps),
            "color":      line_color,
            "overlay":    True,
            "pane":       0,
            "lineWidth":  line_width,
            "seriesType": "line",
        })

    # VWAP ±σ bands as dashed lines
    for b in [1, 2, 3]:
        for side_key, side_label in [(f"upper_{b}", f"VWAP +{b}σ"),
                                      (f"lower_{b}", f"VWAP -{b}σ")]:
            if side_key in result:
                _push(chart, side_label, {
                    "name":       side_label,
                    "data":       _series_to_tv(result[side_key], timestamps),
                    "color":      line_color,
                    "overlay":    True,
                    "pane":       0,
                    "lineWidth":  1,
                    "seriesType": "line",
                })


def add_rsi(
    chart: "Chart",
    rsi: "RSI",
    timestamps: "pd.Series | None" = None,
) -> None:
    """
    Add RSI as a new oscillator sub-pane below the chart.

    Renders:
    - RSI line  (purple, TradingView default)
    - Optional MA on RSI  (red)
    """
    pane = _next_pane(chart)
    rsi_label = f"RSI({rsi.length})"

    _push(chart, rsi_label, {
        "name":       rsi_label,
        "data":       _series_to_tv(rsi.rsi, timestamps),
        "color":      rsi.COLOR_RSI,
        "overlay":    False,
        "pane":       pane,
        "lineWidth":  1,
        "seriesType": "line",
    })

    if "rsi_ma" in rsi.result:
        ma_label = f"{rsi.ma_type}({rsi.ma_length})"
        _push(chart, ma_label, {
            "name":       ma_label,
            "data":       _series_to_tv(rsi.rsi_ma, timestamps),
            "color":      rsi.COLOR_MA,
            "overlay":    False,
            "pane":       pane,
            "lineWidth":  1,
            "seriesType": "line",
        })


def add_macd(
    chart: "Chart",
    macd: "MACD",
    timestamps: "pd.Series | None" = None,
) -> None:
    """
    Add MACD as a new oscillator sub-pane below the chart.

    Renders:
    - Histogram  (4-colour TradingView scheme, per-bar colours)
    - MACD line  (blue)
    - Signal line  (orange)
    """
    pane = _next_pane(chart)
    colors = macd.histogram_colors()
    macd_label = f"MACD({macd.fast_length},{macd.slow_length},{macd.signal_length})"
    sig_label  = f"Signal({macd.signal_length})"

    # Histogram first so it renders behind the lines
    _push(chart, "Histogram", {
        "name":       "Histogram",
        "data":       _series_to_hist(macd.histogram, colors, timestamps),
        "color":      macd.COLOR_HIST_POS_GROW,
        "overlay":    False,
        "pane":       pane,
        "lineWidth":  1,
        "seriesType": "histogram",
    })

    _push(chart, macd_label, {
        "name":       macd_label,
        "data":       _series_to_tv(macd.macd, timestamps),
        "color":      macd.COLOR_MACD,
        "overlay":    False,
        "pane":       pane,
        "lineWidth":  1,
        "seriesType": "line",
    })

    _push(chart, sig_label, {
        "name":       sig_label,
        "data":       _series_to_tv(macd.signal, timestamps),
        "color":      macd.COLOR_SIGNAL,
        "overlay":    False,
        "pane":       pane,
        "lineWidth":  1,
        "seriesType": "line",
    })


def add_ichimoku(
    chart: "Chart",
    ichi: "Ichimoku",
    timestamps: "pd.Series | None" = None,
) -> None:
    """
    Overlay the full Ichimoku system on the price pane.

    Renders (matching TradingView exactly):
    - Tenkan-sen   (dark red)
    - Kijun-sen    (dark blue)
    - Chikou Span  (green, displaced 26 bars into the past)
    - Senkou Span A (teal, displaced 26 bars into the future)
    - Senkou Span B (red,  displaced 26 bars into the future)
    - Cloud fill   (two area series back-to-back, green A>B / red B>A)
      — extends PAST the last candle
    """
    n    = len(ichi.close)
    disp = ichi.displacement

    # ── Timestamp arrays ──────────────────────────────────────────────────
    if timestamps is not None:
        ts = _to_seconds(timestamps)
        interval = int(ts.iloc[1]) - int(ts.iloc[0]) if len(ts) >= 2 else 86400
        last_ts  = int(ts.iloc[-1])
        future_ts = pd.Series([last_ts + interval * (i + 1) for i in range(disp)],
                               dtype="int64")
        ts_fwd    = pd.concat([ts, future_ts]).reset_index(drop=True)
    else:
        ts        = pd.Series(range(n), dtype="int64")
        interval  = 1
        future_ts = pd.Series(range(n, n + disp), dtype="int64")
        ts_fwd    = pd.concat([ts, future_ts]).reset_index(drop=True)

    # ── Five lines ────────────────────────────────────────────────────────
    # Tenkan
    _push(chart, f"Tenkan({ichi.tenkan_period})", {
        "name":       f"Tenkan({ichi.tenkan_period})",
        "data":       _series_to_tv(ichi.tenkan, ts),
        "color":      ichi.COLOR_TENKAN,
        "overlay":    True,
        "pane":       0,
        "lineWidth":  1,
        "seriesType": "line",
    })

    # Kijun
    _push(chart, f"Kijun({ichi.kijun_period})", {
        "name":       f"Kijun({ichi.kijun_period})",
        "data":       _series_to_tv(ichi.kijun, ts),
        "color":      ichi.COLOR_KIJUN,
        "overlay":    True,
        "pane":       0,
        "lineWidth":  1,
        "seriesType": "line",
    })

    # Chikou — uses original timestamps; data series is already shifted back
    _push(chart, "Chikou", {
        "name":       "Chikou",
        "data":       _series_to_tv(ichi.chikou, ts),
        "color":      ichi.COLOR_CHIKOU,
        "overlay":    True,
        "pane":       0,
        "lineWidth":  1,
        "seriesType": "line",
    })

    # Senkou Span A — forward-shifted timestamps, includes future extension
    sa_full = pd.concat([ichi.senkou_a,
                         ichi.result["cloud_future_a"]]).reset_index(drop=True)
    _push(chart, "Span A", {
        "name":       "Span A",
        "data":       _series_to_tv(sa_full, ts_fwd),
        "color":      ichi.COLOR_SENKOU_A,
        "overlay":    True,
        "pane":       0,
        "lineWidth":  1,
        "seriesType": "line",
    })

    # Senkou Span B — forward-shifted timestamps, includes future extension
    sb_full = pd.concat([ichi.senkou_b,
                         ichi.result["cloud_future_b"]]).reset_index(drop=True)
    _push(chart, f"Span B({ichi.senkou_b_period})", {
        "name":       f"Span B({ichi.senkou_b_period})",
        "data":       _series_to_tv(sb_full, ts_fwd),
        "color":      ichi.COLOR_SENKOU_B,
        "overlay":    True,
        "pane":       0,
        "lineWidth":  1,
        "seriesType": "line",
    })

    # ── Cloud fill ────────────────────────────────────────────────────────
    # lightweight-charts has no native "between two series" fill.
    # Best approximation: render Span A as a translucent area series whose
    # baseline is Span B by using the "area" type with topColor / bottomColor.
    # We split into bullish (A>=B) and bearish (A<B) segments so the colour
    # switches correctly — each segment is its own area series.
    #
    # Approach: build two interleaved area series:
    #   cloud_up   — uses COLOR_CLOUD_UP   (teal fill)
    #   cloud_down — uses COLOR_CLOUD_DOWN (red fill)
    # Each uses the transparent rgba colours so they visually overlap with
    # the price candles without obscuring them.

    # Bullish cloud — Span A as area (teal), zero-opacity bottom
    _push(chart, "Cloud (Bull)", {
        "name":       "Cloud (Bull)",
        "data":       _series_to_tv(sa_full, ts_fwd),
        "color":      ichi.COLOR_CLOUD_UP,
        "overlay":    True,
        "pane":       0,
        "lineWidth":  0,
        "seriesType": "area",
    })

    # Bearish cloud — Span B as area (red), zero-opacity bottom
    _push(chart, "Cloud (Bear)", {
        "name":       "Cloud (Bear)",
        "data":       _series_to_tv(sb_full, ts_fwd),
        "color":      ichi.COLOR_CLOUD_DOWN,
        "overlay":    True,
        "pane":       0,
        "lineWidth":  0,
        "seriesType": "area",
    })
