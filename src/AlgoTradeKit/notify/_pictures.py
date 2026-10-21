"""
AlgoTradeKit.notify._pictures
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
What ``send_chart`` and ``send_report`` gather at the call, and the functions
that make their pictures later, on the notifier's thread (v1.2.0).

This module is imported only by those two calls, and it imports ``visual`` and
``report`` only inside its functions: ``notify`` itself imports no sibling.
"""

from __future__ import annotations

import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import _template
from ._core import KIND_TEXT, PICTURE_CANDLES, Notifier, _Media

#: Candles fetched before a picture's first one, so its indicators have settled
#: there — the live recompute window's floor.
WARMUP_CANDLES = 200

_UNITS_MS = {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}
_TIMEFRAME = re.compile(r"^\s*(\d+)\s*([mhdw])\s*$")


def timeframe_ms(timeframe: str) -> int:
    """``"15m"`` → 900,000.  Months have no fixed length, so ``"1M"`` is refused."""
    match = _TIMEFRAME.match(str(timeframe))
    if match is None or int(match.group(1)) <= 0:
        raise ValueError(f"timeframe {timeframe!r} has no fixed length — use one such as "
                         f"'1m', '15m', '4h' or '1d'")
    return int(match.group(1)) * _UNITS_MS[match.group(2)]


def _to_ms(value: Any) -> int:
    from ..visual.chart import Chart

    return Chart._parse_range_time(value) * 1000


def _checked_specs(indicators: Any) -> list[dict]:
    from ..visual.chart import INDICATOR_KINDS

    if indicators is None:
        return []
    if isinstance(indicators, dict) or not isinstance(indicators, (list, tuple)):
        raise TypeError('indicators must be a list of specs such as [{"kind": "ema", '
                        f'"period": 20}}], got {type(indicators).__name__}')
    specs = []
    for spec in indicators:
        if not isinstance(spec, dict) or "kind" not in spec:
            raise ValueError(f'an indicator spec is a dict with a "kind", got {spec!r}')
        kind = str(spec["kind"]).lower()
        if kind not in INDICATOR_KINDS:
            raise ValueError(f"unknown indicator kind {spec['kind']!r} — known: "
                             f"{', '.join(INDICATOR_KINDS)}")
        specs.append(dict(spec))
    return specs


def chart_maker(
    notifier: Notifier, chart: Any, start: Any, end: Any, *, broker: Any, symbol: str | None,
    timeframe: str | None, indicators: Any,
) -> Callable[[], bytes]:
    """Check what can be checked now and gather the candles (Q3.6 a); return what
    makes the picture on the notifier's thread."""
    from ..visual import _snapshot

    _snapshot.check_engine()    # no Playwright, or no browser: raises here
    zone = notifier._zone
    if chart is not None:
        if timeframe is not None or indicators is not None:
            raise TypeError("timeframe= and indicators= are for a picture from a broker — "
                            "a chart has its own")
        for value in (start, end):
            if value is not None:
                _to_ms(value)    # a date it cannot read raises now
        return lambda: chart.screenshot(start, end, timezone=zone)
    if broker is None or not symbol or not timeframe:
        raise TypeError("send_chart needs a chart, or broker=, symbol= and timeframe=")
    specs = _checked_specs(indicators)
    step = timeframe_ms(timeframe)
    end_ms = int(notifier._clock.time() * 1000) if end is None else _to_ms(end)
    start_ms = end_ms - (PICTURE_CANDLES - 1) * step if start is None else _to_ms(start)
    if start_ms > end_ms:
        raise ValueError("start is after end")
    warmup = WARMUP_CANDLES * step if specs else 0
    frame = broker.get_candles_df(symbol, timeframe, start_ms - warmup, end_ms)
    shown = frame[frame["timestamp"] >= start_ms] if len(frame) else frame
    if not len(shown):
        span = (f"{_template.Moment(start_ms / 1000, zone)} and "
                f"{_template.Moment(end_ms / 1000, zone)}")
        raise ValueError(f"no candle of {symbol} {timeframe} between {span}")
    first, last = int(shown["timestamp"].iloc[0]), int(shown["timestamp"].iloc[-1])

    def make() -> bytes:
        from ..visual import Chart

        picture = Chart(title=symbol, source_timeframe=timeframe)
        picture.set_data(frame)
        for spec in specs:
            picture.add_indicator_spec(dict(spec))
        return picture.screenshot(first, last, timezone=zone)

    return make


# ---------------------------------------------------------------------------
# E4 — a backtest's report
# ---------------------------------------------------------------------------

def report_text(notifier: Notifier, report: Any, title: str) -> str:
    """The figures of *report*, as the first of its three messages."""
    trades = int(getattr(report, "total_trades", 0) or 0)
    if trades:
        factor = float(report.profit_factor)
        drawdown = getattr(report, "max_drawdown", None)
        parts = [
            f"Net {_template.Pnl(report.total_pnl)} ({float(report.total_pnl_percent):+.2f}%)",
            f"{trades:,} {'trade' if trades == 1 else 'trades'}",
            f"win rate {float(report.win_rate):.1f}%",
            "profit factor " + ("∞" if factor == float("inf") else f"{factor:.2f}"),
        ]
        if drawdown is not None:
            parts.append(f"max drawdown -{abs(float(drawdown.drawdown_percent)):.2f}%")
        figures = " · ".join(parts)
    else:
        figures = "no trades"
    head = title
    if notifier._rich:
        head = f"<b>{_template.render('{t}', {'t': title}, escape=True)}</b>"
        figures = _template.render("{f}", {"f": figures}, escape=True)
    moment = _template.Moment(notifier._clock.time(), notifier._zone)
    return f"{head}\n{figures}\n🕑 {moment}"


def _file_name(title: str | None) -> str:
    if not title:
        return "report.html"
    safe = re.sub(r"[^\w.-]+", "_", title, flags=re.ASCII).strip("._") or "report"
    return f"{safe}.html"


def send_report(notifier: Notifier, report: Any, *, title: str | None, to: Any) -> dict:
    """E4: the figures, the balance curve and the HTML file, in that order."""
    from ..report._display import save_report_html
    from ..visual import _snapshot

    if KIND_TEXT in notifier._templates and notifier._templates[KIND_TEXT] is None:
        return {}   # templates={"text": None}: what you send yourself is not sent
    _snapshot.check_engine()
    symbol = getattr(getattr(report, "config", None), "symbol", None) or None
    name = title or f"📊 {symbol or 'Backtest'} · Simulate report"
    text = report_text(notifier, report, name)
    with tempfile.TemporaryDirectory() as folder:
        html = Path(save_report_html(report, Path(folder) / "report.html")).read_bytes()

    sent = notifier._enqueue(KIND_TEXT, text, symbol=symbol, to=to, silent=None, wait=True)
    if getattr(report, "balance_history", None):
        def balance() -> bytes:
            with tempfile.TemporaryDirectory() as folder:
                page = Path(folder) / "report.html"
                page.write_bytes(html)
                return _snapshot.snapshot(
                    page.as_uri(), selector="#equity-chart-wrap", ready=_snapshot.REPORT_READY,
                    library=("Chart", _snapshot.NO_JSDELIVR), page_name="the report page",
                )

        notifier._enqueue(KIND_TEXT, "", symbol=symbol, to=to, silent=None, wait=True,
                          media=_Media(_Media.PHOTO, make=balance, filename="balance.png"))
    if len(html) <= notifier._max_document_bytes:
        notifier._enqueue(KIND_TEXT, "", symbol=symbol, to=to, silent=None, wait=True,
                          media=_Media(_Media.DOCUMENT, data=html, filename=_file_name(title)))
    else:
        limit = notifier._max_document_bytes / 1024 / 1024
        notifier._say(f"the report file was left out — it is {len(html) / 1e6:.1f} MB, and "
                      f"{notifier._service} takes at most {limit:g} MB")
    return sent
