"""
AlgoTradeKit.report._display
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
High-level display helpers: show the report in a browser or save it
as a standalone HTML file.

Public functions
----------------
show_report(report, block, port, title, on_open_chart, host)
    Start a ReportServer, push the report data, and open a browser tab.
    When *block* is True the call does not return until the user presses
    Ctrl-C (useful as the last line of a script).

save_report_html(report, path)
    Write a standalone, self-contained HTML file that can be opened in
    any browser without a running server.  The report data is embedded
    as a JSON literal inside a ``<script>`` block.

show_combined_report(pairs, block, port, title, host)          (v1.0.0)
save_combined_report_html(pairs, path)                          (v1.0.0)
    Same pair of helpers for the aggregate multi-pair report
    (``build_combined_report_payload``): merged trade list, summed
    equity curve across accounts, per-pair breakdown section.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from ._builder import build_combined_report_payload, build_report_payload
from ._server import ReportServer

if TYPE_CHECKING:
    from AlgoTradeKit.simulate._report import SimulateReport


def _serve_payload(
    payload: dict,
    block: bool,
    port: int,
    title: str,
    host: str,
    on_open_chart: Callable[[int], None] | None,
) -> ReportServer:
    """Start a ReportServer for *payload* (shared by both show helpers)."""
    server = ReportServer(
        title=title,
        port=port,
        on_open_chart=on_open_chart,
        host=host,
    )
    server.start(open_browser=True)
    # Give the browser a moment to connect then push the data
    time.sleep(0.3)
    server.set_report_data(payload)

    if block:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            server.stop()

    return server


def _write_standalone_html(payload: dict, path: str | Path) -> Path:
    """Embed *payload* into the report template and write it to *path*."""
    template_path = Path(__file__).parent / "static" / "report.html"
    template = template_path.read_text(encoding="utf-8")

    # Inject data as a script block right before </body>
    data_script = (
        "\n<script>\n"
        "// Embedded report data (standalone mode — no WebSocket needed)\n"
        f"(function(){{\n"
        f"  var d = {json.dumps(payload, separators=(',', ':'))};\n"
        f"  window.addEventListener('DOMContentLoaded', function(){{\n"
        f"    d.type = 'report_data';\n"
        f"    renderReport(d);\n"
        f"    document.getElementById('wsdot').style.display = 'none';\n"
        f"    document.getElementById('loading').style.display = 'none';\n"
        f"    document.getElementById('report-content').style.display = 'block';\n"
        f"  }});\n"
        f"}})();\n"
        "</script>\n"
    )

    standalone = template.replace("</body>", data_script + "</body>")

    out = Path(path).resolve()
    out.write_text(standalone, encoding="utf-8")
    return out


def show_report(
    report: SimulateReport,
    block: bool = False,
    port: int = 0,
    title: str = "AlgoTradeKit Report",
    on_open_chart: Callable[[int], None] | None = None,
    host: str = "127.0.0.1",
) -> ReportServer:
    """
    Serve the simulation report as an interactive web page and open a
    browser tab.

    Parameters
    ----------
    report : SimulateReport
        The completed simulation report.
    block : bool
        When ``True``, block the caller until Ctrl-C is pressed.
        Useful as the last line of a standalone script.
    port : int
        TCP port (0 = auto-select in range 8800–9000).
    title : str
        Browser tab / window title.
    on_open_chart : callable | None
        Callback invoked when the browser requests the candle chart for a
        specific trade.  Receives ``trade_id: int``.
    host : str
        Network interface the server binds to (v1.0.0).  Default
        ``"127.0.0.1"`` — local only.  See the security warning on
        :class:`ReportServer` before using ``"0.0.0.0"``.

    Returns
    -------
    ReportServer
        The running server instance.  Call ``server.stop()`` to shut down.
    """
    payload = build_report_payload(report)
    return _serve_payload(payload, block, port, title, host, on_open_chart)


def show_combined_report(
    pairs,
    block: bool = False,
    port: int = 0,
    title: str = "AlgoTradeKit Combined Report",
    host: str = "127.0.0.1",
) -> ReportServer:
    """
    Serve the aggregate multi-pair report (v1.0.0) and open a browser
    tab: merged trade list, equity curve summed across the accounts,
    per-pair breakdown section.

    Parameters
    ----------
    pairs : dict[str, SimulateReport] | iterable[tuple[str, SimulateReport]]
        Pair label → completed report (labels unique, non-empty).
    block, port, title, host
        As in :func:`show_report`.  There is no ``on_open_chart`` —
        portfolio-level chart linking is not available on the combined
        page.

    Returns
    -------
    ReportServer
        The running server instance.  Push fresh stats later with
        ``server.push_update(build_combined_report_payload(pairs))``.
    """
    payload = build_combined_report_payload(pairs)
    return _serve_payload(payload, block, port, title, host, on_open_chart=None)


def save_report_html(
    report: SimulateReport,
    path: str | Path = "report.html",
) -> Path:
    """
    Save the simulation report as a **standalone** HTML file.

    The file embeds all report data as a JSON literal and all required
    CDN scripts are loaded from the internet at open time.  The result
    can be shared and opened in any modern browser.

    Parameters
    ----------
    report : SimulateReport
        The completed simulation report.
    path : str | Path
        Output file path (default: ``"report.html"`` in the current
        working directory).

    Returns
    -------
    Path
        Absolute path of the saved file.
    """
    return _write_standalone_html(build_report_payload(report), path)


def save_combined_report_html(
    pairs,
    path: str | Path = "combined_report.html",
) -> Path:
    """
    Save the aggregate multi-pair report (v1.0.0) as a **standalone**
    HTML file — same document as :func:`show_combined_report`, no server
    needed.

    Parameters
    ----------
    pairs : dict[str, SimulateReport] | iterable[tuple[str, SimulateReport]]
        Pair label → completed report (labels unique, non-empty).
    path : str | Path
        Output file path (default: ``"combined_report.html"``).

    Returns
    -------
    Path
        Absolute path of the saved file.
    """
    return _write_standalone_html(build_combined_report_payload(pairs), path)
