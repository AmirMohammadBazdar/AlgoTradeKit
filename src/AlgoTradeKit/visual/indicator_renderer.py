"""
AlgoTradeKit.visual.indicator_renderer
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Bridges the indicator module with the visual/Chart module.

Responsibilities
----------------
1. ``add_rsi(chart, rsi)``          — adds an RSI pane below the chart
2. ``add_macd(chart, macd)``        — adds a MACD pane below the chart
3. ``add_ma(chart, ma_obj)``        — overlays any MA line on the price pane
4. ``add_ichimoku(chart, ichi)``    — overlays all Ichimoku lines + cloud
                                      (including future cloud extension)

Each function serialises the indicator data into the JSON payload that
lightweight-charts (the frontend) understands, and pushes it to the
Chart's in-memory state so it is available when the page is served.

The visual module already serialises ``chart._indicators`` into the
WebSocket / HTTP payload; we just need to populate that list correctly.

Data format accepted by the frontend
-------------------------------------
Each entry in ``chart._indicators`` is a dict:

    {
      "id": str,          # unique id used by JS to find/update the series
      "type": "line" | "histogram" | "cloud" | "band",
      "pane": int,        # 0 = price pane, 1+ = sub-panes
      "data": [{"time": int_ms, "value": float}, ...],
      "options": {        # lightweight-charts series options
          "color": str,
          "lineWidth": int,
          "title": str,
          ...
      },
      # cloud only:
      "data_upper": [...],
      "data_lower": [...],
      "color_up": str,
      "color_down": str,
    }
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

def _series_to_tv(series: pd.Series, timestamps: "pd.Series | None" = None) -> list[dict]:
    """
    Convert a pandas Series to the list-of-{time, value} format that
    lightweight-charts requires.

    ``timestamps`` should be Unix milliseconds (int64).  When not supplied
    we synthesise sequential second-timestamps starting at 0 — useful for
    unit tests / standalone use.
    """
    if timestamps is None:
        # fallback: 1-second bars starting at Unix epoch
        ts = pd.Series(range(len(series)), dtype="int64") * 1000
    else:
        ts = timestamps.reset_index(drop=True)

    result = []
    for i, (t, v) in enumerate(zip(ts, series.reset_index(drop=True))):
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
    Convert histogram Series to lightweight-charts colored histogram format.
    Each bar: {"time": ms, "value": float, "color": hex_str}
    """
    if timestamps is None:
        ts = pd.Series(range(len(series)), dtype="int64") * 1000
    else:
        ts = timestamps.reset_index(drop=True)

    result = []
    for t, v, c in zip(ts, series.reset_index(drop=True), colors.reset_index(drop=True)):
        if pd.isna(v) or c is None or (isinstance(c, float) and math.isnan(c)):
            continue
        result.append({"time": int(t), "value": float(v), "color": str(c)})
    return result


def _next_pane(chart: "Chart") -> int:
    """Return the next free pane index (0 = price pane).

    Works with both IndicatorSeries objects (have a .pane attribute) and
    RawIndicator objects (have a .payload dict with a 'pane' key).
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
    Overlay any MA line (SMA, EMA, WMA, etc.) on the price pane.

    Parameters
    ----------
    chart      : Chart instance
    ma_obj     : any MA indicator (SMA, EMA, WMA, VWMA, SMMA, DEMA, TEMA, HullMA, VWAP)
    timestamps : pd.Series of Unix-ms timestamps aligned to the MA data
    color      : override the default TradingView colour
    line_width : line pixel width (default 1)
    title      : legend label (auto-derived from indicator if None)
    """
    if not hasattr(chart, "_indicators"):
        chart._indicators = []

    # Find the primary output key (first key in result)
    result = ma_obj.result
    if not result:
        return

    # Pull default colour from the class if available
    default_color = getattr(ma_obj, "DEFAULT_COLOR", "#2962FF")
    line_color = color or default_color

    name = ma_obj._NAME
    length = getattr(ma_obj, "length", "")
    auto_title = f"{name}({length})" if length else name

    for key, series in result.items():
        # Skip band series (upper_N / lower_N) for VWAP — rendered separately
        if key.startswith("upper_") or key.startswith("lower_"):
            continue

        chart._indicators.append(RawIndicator(
            name=title or auto_title,
            payload={
                "id": f"ma_{name}_{key}_{id(ma_obj)}",
                "type": "line",
                "pane": 0,  # price pane
                "data": _series_to_tv(series, timestamps),
                "options": {
                    "color": line_color,
                    "lineWidth": line_width,
                    "title": title or auto_title,
                    "priceScaleId": "right",
                },
            },
        ))

    # VWAP bands
    for b in [1, 2, 3]:
        uk = f"upper_{b}"
        lk = f"lower_{b}"
        if uk in result and lk in result:
            for side_key, side_label in [(uk, f"VWAP +{b}σ"), (lk, f"VWAP -{b}σ")]:
                chart._indicators.append(RawIndicator(
                    name=side_label,
                    payload={
                        "id": f"vwap_band_{b}_{side_key}_{id(ma_obj)}",
                        "type": "line",
                        "pane": 0,
                        "data": _series_to_tv(result[side_key], timestamps),
                        "options": {
                            "color": line_color,
                            "lineWidth": 1,
                            "lineStyle": 2,  # dashed
                            "title": side_label,
                            "priceScaleId": "right",
                        },
                    },
                ))


def add_rsi(
    chart: "Chart",
    rsi: "RSI",
    timestamps: "pd.Series | None" = None,
) -> None:
    """
    Add RSI as a new oscillator pane below the chart.

    Renders:
    - RSI line (purple)
    - Optional MA on RSI (red)
    - Overbought / oversold horizontal levels (grey dashed)
    - Shaded zones above OB and below OS
    """
    if not hasattr(chart, "_indicators"):
        chart._indicators = []

    pane = _next_pane(chart)

    # --- RSI line ---
    chart._indicators.append(RawIndicator(
        name=f"RSI({rsi.length})",
        payload={
            "id": f"rsi_line_{id(rsi)}",
            "type": "line",
            "pane": pane,
            "data": _series_to_tv(rsi.rsi, timestamps),
            "options": {
                "color": rsi.COLOR_RSI,
                "lineWidth": 1,
                "title": f"RSI({rsi.length})",
                "priceScaleId": f"pane{pane}",
            },
            "scale": {"min": 0, "max": 100},
            "levels": [
                {"value": rsi.overbought, "color": rsi.COLOR_OB, "style": "dashed", "label": str(rsi.overbought)},
                {"value": rsi.oversold,   "color": rsi.COLOR_OS, "style": "dashed", "label": str(rsi.oversold)},
                {"value": 50,             "color": "#787B86",    "style": "dotted"},
            ],
            "zones": [
                {"upper": 100,           "lower": rsi.overbought, "color": rsi.COLOR_OB_FILL},
                {"upper": rsi.oversold,  "lower": 0,              "color": rsi.COLOR_OS_FILL},
            ],
        },
    ))

    # --- Optional MA on RSI ---
    if "rsi_ma" in rsi.result:
        chart._indicators.append(RawIndicator(
            name=f"{rsi.ma_type}({rsi.ma_length})",
            payload={
                "id": f"rsi_ma_{id(rsi)}",
                "type": "line",
                "pane": pane,
                "data": _series_to_tv(rsi.rsi_ma, timestamps),
                "options": {
                    "color": rsi.COLOR_MA,
                    "lineWidth": 1,
                    "title": f"{rsi.ma_type}({rsi.ma_length})",
                    "priceScaleId": f"pane{pane}",
                },
            },
        ))


def add_macd(
    chart: "Chart",
    macd: "MACD",
    timestamps: "pd.Series | None" = None,
) -> None:
    """
    Add MACD as a new oscillator pane below the chart.

    Renders:
    - Histogram bars with TradingView 4-colour scheme
    - MACD line (blue)
    - Signal line (orange)
    - Zero level (grey)
    """
    if not hasattr(chart, "_indicators"):
        chart._indicators = []

    pane = _next_pane(chart)
    colors = macd.histogram_colors()

    macd_title = f"MACD({macd.fast_length},{macd.slow_length},{macd.signal_length})"

    # --- Histogram ---
    chart._indicators.append(RawIndicator(
        name="Histogram",
        payload={
            "id": f"macd_hist_{id(macd)}",
            "type": "histogram",
            "pane": pane,
            "data": _series_to_hist(macd.histogram, colors, timestamps),
            "options": {
                "color": macd.COLOR_HIST_POS_GROW,  # default; overridden per-bar
                "title": "Histogram",
                "priceScaleId": f"pane{pane}",
            },
            "levels": [
                {"value": 0, "color": "#787B86", "style": "solid"},
            ],
        },
    ))

    # --- MACD line ---
    chart._indicators.append(RawIndicator(
        name=macd_title,
        payload={
            "id": f"macd_line_{id(macd)}",
            "type": "line",
            "pane": pane,
            "data": _series_to_tv(macd.macd, timestamps),
            "options": {
                "color": macd.COLOR_MACD,
                "lineWidth": 1,
                "title": macd_title,
                "priceScaleId": f"pane{pane}",
            },
        },
    ))

    # --- Signal line ---
    chart._indicators.append(RawIndicator(
        name=f"Signal({macd.signal_length})",
        payload={
            "id": f"macd_signal_{id(macd)}",
            "type": "line",
            "pane": pane,
            "data": _series_to_tv(macd.signal, timestamps),
            "options": {
                "color": macd.COLOR_SIGNAL,
                "lineWidth": 1,
                "title": f"Signal({macd.signal_length})",
                "priceScaleId": f"pane{pane}",
            },
        },
    ))


def add_ichimoku(
    chart: "Chart",
    ichi: "Ichimoku",
    timestamps: "pd.Series | None" = None,
) -> None:
    """
    Overlay the full Ichimoku system on the price pane, matching TradingView:

    - Tenkan-sen (dark red, solid)
    - Kijun-sen  (dark blue, solid)
    - Chikou Span (green, displaced BACK by `displacement` bars)
    - Senkou Span A (teal, displaced FORWARD by `displacement` bars)
    - Senkou Span B (red,  displaced FORWARD by `displacement` bars)
    - Cloud fill: green where A>B, red where B>A — extends PAST last candle

    The displacement is baked into the data arrays: Span A/B/Chikou timestamps
    are shifted so the frontend just plots them at their correct positions.
    """
    if not hasattr(chart, "_indicators"):
        chart._indicators = []

    n = len(ichi.close)
    disp = ichi.displacement

    # Build timestamp arrays with displacement
    if timestamps is not None:
        ts = timestamps.reset_index(drop=True)
        # Interval between bars (assume uniform)
        if len(ts) >= 2:
            interval_ms = int(ts.iloc[1]) - int(ts.iloc[0])
        else:
            interval_ms = 1000  # fallback

        # Future timestamps for the cloud extension
        last_ts = int(ts.iloc[-1])
        future_ts = pd.Series([last_ts + interval_ms * (i + 1) for i in range(disp)])

        ts_shifted_fwd = pd.concat([ts, future_ts]).reset_index(drop=True)
        ts_chikou = ts  # chikou uses original timestamps; data is shifted already
    else:
        ts = pd.Series(range(n), dtype="int64") * 1000
        interval_ms = 1000
        future_ts = pd.Series([n * 1000 + i * 1000 for i in range(disp)])
        ts_shifted_fwd = pd.concat([ts, future_ts]).reset_index(drop=True)
        ts_chikou = ts

    # ---- Tenkan ----
    chart._indicators.append(RawIndicator(
        name=f"Tenkan({ichi.tenkan_period})",
        payload={
            "id": f"ichi_tenkan_{id(ichi)}",
            "type": "line",
            "pane": 0,
            "data": _series_to_tv(ichi.tenkan, ts),
            "options": {
                "color": ichi.COLOR_TENKAN,
                "lineWidth": 1,
                "title": f"Tenkan({ichi.tenkan_period})",
                "priceScaleId": "right",
            },
        },
    ))

    # ---- Kijun ----
    chart._indicators.append(RawIndicator(
        name=f"Kijun({ichi.kijun_period})",
        payload={
            "id": f"ichi_kijun_{id(ichi)}",
            "type": "line",
            "pane": 0,
            "data": _series_to_tv(ichi.kijun, ts),
            "options": {
                "color": ichi.COLOR_KIJUN,
                "lineWidth": 1,
                "title": f"Kijun({ichi.kijun_period})",
                "priceScaleId": "right",
            },
        },
    ))

    # ---- Chikou Span (plotted disp bars in the PAST) ----
    chart._indicators.append(RawIndicator(
        name="Chikou",
        payload={
            "id": f"ichi_chikou_{id(ichi)}",
            "type": "line",
            "pane": 0,
            "data": _series_to_tv(ichi.chikou, ts_chikou),
            "options": {
                "color": ichi.COLOR_CHIKOU,
                "lineWidth": 1,
                "title": "Chikou",
                "priceScaleId": "right",
            },
        },
    ))

    # ---- Senkou Span A (forward-shifted) ----
    sa_full = pd.concat([ichi.senkou_a, ichi.result["cloud_future_a"]]).reset_index(drop=True)
    chart._indicators.append(RawIndicator(
        name="Span A",
        payload={
            "id": f"ichi_span_a_{id(ichi)}",
            "type": "line",
            "pane": 0,
            "data": _series_to_tv(sa_full, ts_shifted_fwd),
            "options": {
                "color": ichi.COLOR_SENKOU_A,
                "lineWidth": 1,
                "title": "Span A",
                "priceScaleId": "right",
            },
        },
    ))

    # ---- Senkou Span B (forward-shifted) ----
    sb_full = pd.concat([ichi.senkou_b, ichi.result["cloud_future_b"]]).reset_index(drop=True)
    chart._indicators.append(RawIndicator(
        name=f"Span B({ichi.senkou_b_period})",
        payload={
            "id": f"ichi_span_b_{id(ichi)}",
            "type": "line",
            "pane": 0,
            "data": _series_to_tv(sb_full, ts_shifted_fwd),
            "options": {
                "color": ichi.COLOR_SENKOU_B,
                "lineWidth": 1,
                "title": f"Span B({ichi.senkou_b_period})",
                "priceScaleId": "right",
            },
        },
    ))

    # ---- Cloud fill ----
    cloud_data_a = _series_to_tv(sa_full, ts_shifted_fwd)
    cloud_data_b = _series_to_tv(sb_full, ts_shifted_fwd)

    chart._indicators.append(RawIndicator(
        name="Ichimoku Cloud",
        payload={
            "id": f"ichi_cloud_{id(ichi)}",
            "type": "cloud",
            "pane": 0,
            "data_upper": cloud_data_a,
            "data_lower": cloud_data_b,
            "color_up": ichi.COLOR_CLOUD_UP,
            "color_down": ichi.COLOR_CLOUD_DOWN,
            "options": {
                "title": "Ichimoku Cloud",
                "priceScaleId": "right",
            },
        },
    ))