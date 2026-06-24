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
    from AlgoTradeKit.simulate._report import SimulateReport
    from AlgoTradeKit.strategy._types import StrategyResult


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
            "group":      label,
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
                    "group":      label,
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
    rsi_group = f"RSI({rsi.length})"

    _push(chart, rsi_label, {
        "name":       rsi_label,
        "data":       _series_to_tv(rsi.rsi, timestamps),
        "color":      rsi.COLOR_RSI,
        "overlay":    False,
        "pane":       pane,
        "lineWidth":  1,
        "seriesType": "line",
        "group":      rsi_group,
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
            "group":      rsi_group,
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
    macd_group = f"MACD({macd.fast_length},{macd.slow_length},{macd.signal_length})"

    # Histogram first so it renders behind the lines
    _push(chart, "Histogram", {
        "name":       "Histogram",
        "data":       _series_to_hist(macd.histogram, colors, timestamps),
        "color":      macd.COLOR_HIST_POS_GROW,
        "overlay":    False,
        "pane":       pane,
        "lineWidth":  1,
        "seriesType": "histogram",
        "group":      macd_group,
    })

    _push(chart, macd_label, {
        "name":       macd_label,
        "data":       _series_to_tv(macd.macd, timestamps),
        "color":      macd.COLOR_MACD,
        "overlay":    False,
        "pane":       pane,
        "lineWidth":  1,
        "seriesType": "line",
        "group":      macd_group,
    })

    _push(chart, sig_label, {
        "name":       sig_label,
        "data":       _series_to_tv(macd.signal, timestamps),
        "color":      macd.COLOR_SIGNAL,
        "overlay":    False,
        "pane":       pane,
        "lineWidth":  1,
        "seriesType": "line",
        "group":      macd_group,
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
    n          = len(ichi.close)
    disp       = ichi.displacement
    ichi_group = f"Ichimoku({ichi.tenkan_period},{ichi.kijun_period},{ichi.senkou_b_period})"

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
        "group":      ichi_group,
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
        "group":      ichi_group,
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
        "group":      ichi_group,
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
        "group":      ichi_group,
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
        "group":      ichi_group,
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
        "group":      ichi_group,
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
        "group":      ichi_group,
    })


# ---------------------------------------------------------------------------
# Dynamic SL/TP line helpers (v0.7.4)
# ---------------------------------------------------------------------------

# Segment colours based on SL position relative to entry
_COLOR_SL_LOSS      = "#f85149"  # red   — SL is in the loss zone
_COLOR_SL_BREAKEVEN = "#e3b341"  # amber — SL is at break-even (entry)
_COLOR_SL_PROFIT    = "#22d3ee"  # cyan  — SL is in profit territory
_COLOR_NEXT_TP      = "#3fb950"  # green — next TP target line


def _sl_segment_color(sl: float, entry: float, direction: str) -> str:
    """
    Return the colour for a SL horizontal line segment based on whether
    the stop-loss is in the loss zone, at break-even, or in profit.

    Loss zone (red)  : for a long, SL < entry; for a short, SL > entry
    Break-even (yellow): SL is within a tiny epsilon of entry
    Profit zone (cyan): for a long, SL > entry; for a short, SL < entry
    """
    factor = 1.0 if direction == "long" else -1.0
    profit_margin = factor * (sl - entry)
    eps = abs(entry) * 1e-7 or 1e-9    # relative epsilon
    if profit_margin > eps:
        return _COLOR_SL_PROFIT
    elif profit_margin > -eps:
        return _COLOR_SL_BREAKEVEN
    else:
        return _COLOR_SL_LOSS


def _draw_sl_line(chart: "Chart", t1: int, t2: int, price: float, color: str) -> None:
    """Draw a horizontal SL line segment as a TrendLine."""
    from .models import TrendLine
    line = TrendLine(
        time1=t1, price1=price,
        time2=t2, price2=price,
        color=color, line_width=2, label="",
    )
    chart._drawings.append(line)
    if chart._shown:
        chart._server.send({"type": "add_drawing", "drawing": line.to_dict()})


def _draw_tp_line(chart: "Chart", t1: int, t2: int, price: float) -> None:
    """Draw a horizontal next-TP line segment (green) as a TrendLine."""
    from .models import TrendLine
    line = TrendLine(
        time1=t1, price1=price,
        time2=t2, price2=price,
        color=_COLOR_NEXT_TP, line_width=1, label="",
    )
    chart._drawings.append(line)
    if chart._shown:
        chart._server.send({"type": "add_drawing", "drawing": line.to_dict()})


def _draw_dynamic_sl_tp_lines(
    chart: "Chart",
    marker: dict,
    close_time_ms: int,
    draw_tp_lines: bool,
) -> None:
    """
    Draw coloured horizontal SL line segments (and optionally TP lines) for
    a position whose SL moved during its lifetime.

    ``marker["sl_history"]`` is an ordered list of dicts::

        [{"time": int_ms, "sl": float, "next_tp": float | None}, ...]

    Each entry represents a moment when the SL changed.  Between consecutive
    entries the SL was constant.  The last segment runs to ``close_time_ms``.

    SL line colour is determined by position relative to entry price:
    - RED    → SL is below entry (long) / above entry (short): loss zone
    - YELLOW → SL is at entry price: break-even
    - CYAN   → SL is above entry (long) / below entry (short): profit zone

    TP lines (green, drawn only when ``draw_tp_lines=True``) show the next
    TP target for each segment, matching the SL segment's time range.
    """
    sl_history = marker.get("sl_history", [])
    if not sl_history:
        return

    entry_price = marker["entry_price"]
    direction   = marker["direction"]
    n = len(sl_history)

    for i, event in enumerate(sl_history):
        t_start_ms = event["time"]
        t_end_ms   = sl_history[i + 1]["time"] if i + 1 < n else close_time_ms

        t1 = t_start_ms // 1000
        t2 = t_end_ms   // 1000

        if t2 <= t1:
            continue

        sl_price = event["sl"]
        next_tp  = event.get("next_tp")

        # SL line
        color = _sl_segment_color(sl_price, entry_price, direction)
        _draw_sl_line(chart, t1, t2, sl_price, color)

        # TP line (multi_rr only)
        if draw_tp_lines and next_tp is not None:
            _draw_tp_line(chart, t1, t2, next_tp)


# ---------------------------------------------------------------------------
# Simulation positions (v0.7.0, enhanced v0.7.4)
# ---------------------------------------------------------------------------

def add_simulation_positions(
    chart: "Chart",
    report: "SimulateReport",
    opacity: float = 0.15,
    max_trades: int | None = None,
    config=None,
) -> "Chart":
    """
    Overlay all simulated trades on a candle chart as position boxes.

    For each completed trade in *report* a :class:`PositionBox` is added
    showing the loss zone (SL→entry) and profit zone (entry→TP), plus
    entry and exit signal markers.

    v0.7.4 enhancements
    --------------------
    * **multi_rr positions** — each time the SL advances (after a TP level
      is hit) a new coloured horizontal line segment is drawn:

      - RED    → SL in loss zone (below entry for long)
      - YELLOW → SL at break-even (entry price)
      - CYAN   → SL in profit territory (above entry for long)

      A GREEN TP-target line is drawn alongside each SL segment showing
      the next TP the position was aiming at.

    * **trailing SL positions** — same coloured SL line treatment (no TP
      line), plus the position box top is set to ``peak_price`` (the
      highest price the position ever saw, which is also the price that
      drove the trailing SL to its final level).

    * **posbox TP fix** — the profit-zone boundary now uses
      ``final_next_tp`` (the TP target active *at the moment of closing*)
      instead of the initial TP at entry.  For example, if a position hit
      TP3 and the next target was TP4 before the SL was hit, the box shows
      TP4.

    Parameters
    ----------
    chart : Chart
        The candle chart to draw on.  Must already have OHLCV data loaded.
    report : SimulateReport
        The completed simulation report (from ``Simulate(config).run()``).
    opacity : float
        Fill opacity for position boxes (default 0.15).
    max_trades : int | None
        Limit the number of trades drawn.  ``None`` draws all.
    config : SimulateConfig | None
        When provided, used to detect ``tp_mode`` and ``sl_mode`` so the
        correct line style is applied automatically.  If ``None``, both
        dynamic SL and TP lines are drawn whenever ``sl_history`` is non-empty.

    Returns
    -------
    Chart
        The same *chart* instance (for chaining).

    Example
    -------
    ::

        from AlgoTradeKit.visual import Chart
        from AlgoTradeKit.visual.indicator_renderer import add_simulation_positions

        chart = Chart.from_csv("data/binance-futures_BTCUSDT_1h.csv")
        add_simulation_positions(chart, report, config=sim_config)
        chart.show(block=True)
    """
    from AlgoTradeKit.simulate._report import SimulateReport  # noqa: F401

    # Determine active modes from config if provided
    tp_mode = getattr(config, "tp_mode", None) if config is not None else None
    sl_mode = getattr(config, "sl_mode", None) if config is not None else None

    is_multi_rr = (tp_mode == "multi_rr") if tp_mode is not None else None
    is_trailing  = (sl_mode == "trailing") if sl_mode is not None else None

    # Group markers by trade_id so that partial-close positions (multiple
    # ClosedTrade rows per trade) are rendered as a single posbox + SL lines.
    groups: dict[int, list[dict]] = {}
    for m in report.trade_markers:
        groups.setdefault(m["trade_id"], []).append(m)

    drawn = 0
    for tid, g_markers in groups.items():
        if max_trades is not None and drawn >= max_trades:
            break

        # First marker carries entry data; last marker carries exit data and
        # the most-complete sl_history snapshot.
        first = g_markers[0]
        last  = g_markers[-1]

        open_ms   = first["open_time"]
        close_ms  = last["close_time"]
        open_sec  = open_ms  // 1000
        close_sec = close_ms // 1000

        entry_price  = first["entry_price"]
        initial_sl   = first["initial_stop_loss"]
        direction    = first["direction"]
        net_pnl      = sum(m["net_pnl"] for m in g_markers)
        close_reason = last["close_reason"]
        rr           = last.get("pnl_r", 0.0)
        sl_history   = last.get("sl_history", [])
        final_next_tp = last.get("final_next_tp")
        peak_price   = last.get("peak_price", entry_price)

        # ---- Determine posbox TP (v0.7.4 fix) ----
        # For trailing SL, the top of the profit zone is the peak price
        # (the furthest price the trailing SL ever chased).
        # For multi_rr, use the final_next_tp so the box shows the TP target
        # that was active at the moment the position closed.
        visual_tp = first.get("take_profit")   # initial TP (fallback)

        if is_trailing or (is_trailing is None and sl_mode == "trailing"
                           and len(sl_history) > 1):
            # Peak-based profit zone for trailing positions
            visual_tp = peak_price
        elif final_next_tp is not None:
            # Use the closing-moment next-TP for multi_rr and other modes
            visual_tp = final_next_tp
        elif last.get("close_reason") == "tp" and last.get("exit_price"):
            # Position closed exactly at the final TP — use exit price
            visual_tp = last["exit_price"]

        # ---- Position box ----
        chart.add_position_box(
            open_time    = open_sec,
            close_time   = close_sec,
            entry_price  = entry_price,
            stop_loss    = initial_sl,
            take_profit  = visual_tp,
            direction    = direction,
            net_pnl      = net_pnl,
            close_reason = close_reason,
            trade_id     = tid,
            rr_ratio     = rr,
            opacity      = opacity,
        )

        # ---- Dynamic SL/TP lines (v0.7.4) ----
        has_sl_history = bool(sl_history) and len(sl_history) > 1

        draw_dynamic = (
            has_sl_history
            and (
                is_multi_rr is True
                or is_trailing is True
                or is_multi_rr is None  # config not provided: draw if data present
            )
        )

        if draw_dynamic:
            # For multi_rr: draw SL lines + green TP lines
            # For trailing: draw SL lines only (no TP line)
            draw_tp_lines = not is_trailing   # True for multi_rr / unknown
            _draw_dynamic_sl_tp_lines(
                chart, last, close_ms, draw_tp_lines=draw_tp_lines
            )

        drawn += 1

    return chart


def add_strategy_drawings(
    chart: "Chart",
    strategy_result: "StrategyResult",
) -> "Chart":
    """
    Add all strategy-generated drawings (signal zones, support/resistance
    lines, indicator levels, etc.) from *strategy_result* to *chart*.

    Strategies populate ``StrategyResult.drawings`` in their
    ``prepare_indicators()`` or ``generate_signals()`` methods.  Each
    drawing must be a dict with at minimum ``{"type": ..., ...}`` in the
    same format as the chart module's drawing objects.  Times must be in
    **Unix seconds**.

    Parameters
    ----------
    chart : Chart
        Target candle chart.
    strategy_result : StrategyResult
        The result of ``strategy.run(data)``.

    Returns
    -------
    Chart
        The same *chart* instance (for chaining).
    """
    import itertools as _itertools

    _id_ctr = _itertools.count(1)

    for raw in strategy_result.drawings:
        d = dict(raw)  # defensive copy
        if "id" not in d:
            d["id"] = f"strat_{next(_id_ctr)}"
        if "locked" not in d:
            d["locked"] = True
        if "source" not in d:
            d["source"] = "server"

        # Store as a raw dict wrapper so chart._drawings can serialise it
        from .models import RawIndicator

        class _RawDrawing:
            """Thin wrapper that gives a dict the .id and .to_dict() interface."""
            def __init__(self, payload: dict) -> None:
                self.id = payload["id"]
                self._payload = payload

            def to_dict(self) -> dict:
                return self._payload

        wrapped = _RawDrawing(d)
        chart._drawings.append(wrapped)  # type: ignore[arg-type]
        if chart._shown:
            chart._server.send({"type": "add_drawing", "drawing": d})

    return chart
