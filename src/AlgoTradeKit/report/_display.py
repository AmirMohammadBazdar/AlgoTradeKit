"""
AlgoTradeKit.report._display
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
High-level display helpers: show the report in a browser or save it
as a standalone HTML file.

Public functions
----------------
show_report(report, block, port, title, on_open_chart)
    Start a ReportServer, push the report data, and open a browser tab.
    When *block* is True the call does not return until the user presses
    Ctrl-C (useful as the last line of a script).

save_report_html(report, path)
    Write a standalone, self-contained HTML file that can be opened in
    any browser without a running server.  The report data is embedded
    as a JSON literal inside a ``<script>`` block.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from ._builder import build_report_payload
from ._server import ReportServer

if TYPE_CHECKING:
    from AlgoTradeKit.simulate._report import SimulateReport


def show_report(
    report: "SimulateReport",
    block: bool = False,
    port: int = 0,
    title: str = "AlgoTradeKit Report",
    on_open_chart: Optional[Callable[[int], None]] = None,
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

    Returns
    -------
    ReportServer
        The running server instance.  Call ``server.stop()`` to shut down.
    """
    payload = build_report_payload(report)

    server = ReportServer(
        title=title,
        port=port,
        on_open_chart=on_open_chart,
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


def save_report_html(
    report: "SimulateReport",
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
    payload = build_report_payload(report)

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
