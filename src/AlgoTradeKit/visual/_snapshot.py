"""
AlgoTradeKit.visual._snapshot
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Chart pictures (v1.2.0): a headless Chromium photographs the library's own pages.

One browser per process, launched on first use and closed at exit.  Playwright's
sync API belongs to the thread that started it, so one daemon thread owns Playwright
and the browser and takes requests from a queue; a caller waits for its answer.
Each picture gets a page of its own, closed after it.

Playwright is an extra — ``pip install "AlgoTradeKit[notify]"``, then ``playwright
install chromium`` — and is imported only on that thread.  The pages load their
charting library from a CDN (lightweight-charts from unpkg, Chart.js from jsdelivr),
so a machine that makes pictures must reach it.
"""

from __future__ import annotations

import atexit
import glob
import importlib.util
import os
import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as _FutureTimeout
from typing import Any

#: Raised when Playwright is not installed.
NO_ENGINE = ('chart pictures need the [notify] extra: pip install "AlgoTradeKit[notify]", '
             "then: playwright install chromium")
#: Raised when Playwright has no browser.
NO_BROWSER = ("Chromium is not installed for Playwright — run: playwright install chromium "
              "(on Linux: playwright install --with-deps chromium)")
NO_UNPKG = ("the chart page could not load lightweight-charts from unpkg.com — "
            "this machine must reach it to make pictures")
NO_JSDELIVR = ("the report page could not load Chart.js from cdn.jsdelivr.net — "
               "this machine must reach it to make pictures")

#: What the chart page sets once candles, indicators and drawings are painted.
CHART_READY = "window.__atkSnapshotReady === true"
#: The report page is ready once its balance chart exists.
REPORT_READY = "typeof equityChart !== 'undefined' && equityChart !== null"

#: Starting the driver and the browser comes on top of a picture's own timeout.
_START_ALLOWANCE = 60.0


class SnapshotError(RuntimeError):
    """A picture could not be made: no browser, a page that did not load, a timeout."""


def _waits_two_frames(page: Any) -> None:
    page.evaluate("() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))")


class _Engine:
    """The snapshot thread: Playwright, its browser, and the queue that feeds them."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: queue.Queue[tuple[Callable[[Any, Any], Any], Future] | None] = (
            queue.Queue()
        )
        self._thread: threading.Thread | None = None
        self._closer_registered = False
        self._found_browser = False

    def run(self, work: Callable[[Any, Any], Any], timeout: float) -> Any:
        """``work(playwright, browser_getter)`` on the snapshot thread; its result, or
        its exception, here."""
        future: Future = Future()
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._loop, name="atk-snapshot", daemon=True
                )
                self._thread.start()
                if not self._closer_registered:
                    self._closer_registered = True
                    atexit.register(self.close)
            self._requests.put((work, future))
        try:
            return future.result(timeout=timeout + _START_ALLOWANCE)
        except _FutureTimeout:
            raise SnapshotError(
                f"the browser did not answer in {timeout + _START_ALLOWANCE:g} s"
            ) from None

    def close(self, timeout: float = 5.0) -> None:
        """Close the browser and stop the thread (at exit; a later picture starts both
        again)."""
        with self._lock:
            thread = self._thread
            if thread is None or not thread.is_alive():
                return
            self._requests.put(None)
        thread.join(timeout)

    def _loop(self) -> None:
        playwright = None
        browser: list[Any] = [None]

        def get_browser() -> Any:
            if browser[0] is None or not browser[0].is_connected():
                browser[0] = _launch(playwright)
            return browser[0]

        try:
            while True:
                item = self._requests.get()
                if item is None:
                    return
                work, future = item
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    if playwright is None:
                        from playwright.sync_api import sync_playwright

                        playwright = sync_playwright().start()
                    result = work(playwright, get_browser)
                except BaseException as exc:  # noqa: BLE001 — the caller gets it
                    future.set_exception(exc)
                else:
                    future.set_result(result)
        finally:
            for closing in (lambda: browser[0] and browser[0].close(),
                            lambda: playwright and playwright.stop()):
                try:
                    closing()
                except Exception:  # noqa: BLE001 — at exit, nothing to tell
                    pass


_engine = _Engine()


def _require_playwright() -> None:
    if importlib.util.find_spec("playwright") is None:
        raise ImportError(NO_ENGINE)


def _has_browser(playwright: Any) -> bool:
    """Is a Chromium build installed for this Playwright, without launching it?

    Headless pictures use the headless shell where there is one (Playwright 1.49
    and later), which is installed beside the full build; either one will do."""
    try:
        path = playwright.chromium.executable_path
    except Exception:  # noqa: BLE001 — a driver that cannot say has none
        return False
    if path and os.path.exists(path):
        return True
    builds = os.path.dirname(os.path.dirname(os.path.dirname(path or "")))
    shells = glob.glob(os.path.join(builds, "chromium_headless_shell-*", "*",
                                    "chrome-headless-shell*"))
    return any(os.path.isfile(shell) for shell in shells)


def _launch(playwright: Any) -> Any:
    """The headless browser: the headless shell, else the full Chromium."""
    from playwright.sync_api import Error as PlaywrightError

    try:
        return playwright.chromium.launch()
    except PlaywrightError as exc:
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        if "Executable doesn't exist" not in str(exc):
            raise SnapshotError(f"Chromium did not start ({first})") from None
    try:   # only the full build is installed: its own headless mode
        return playwright.chromium.launch(channel="chromium")
    except PlaywrightError:
        raise SnapshotError(NO_BROWSER) from None


def check_engine() -> None:
    """Raise if pictures cannot be made here: ``ImportError`` without Playwright,
    :class:`SnapshotError` without its Chromium.  It starts Playwright's driver,
    and looks for the browser without launching it; once found, it is not asked
    again."""
    _require_playwright()
    if _engine._found_browser:
        return

    def look(playwright: Any, get_browser: Callable[[], Any]) -> bool:
        del get_browser
        return _has_browser(playwright)

    if not _engine.run(look, 20.0):
        raise SnapshotError(NO_BROWSER)
    _engine._found_browser = True


def snapshot(
    url: str,
    width: int = 1280,
    height: int = 720,
    timeout: float = 20.0,
    *,
    selector: str | None = None,
    ready: str = CHART_READY,
    library: tuple[str, str] | None = ("LightweightCharts", NO_UNPKG),
    page_name: str = "the chart page",
) -> bytes:
    """Open *url* in the headless browser at *width* × *height*, wait for *ready*
    (an expression the page makes true), and return a PNG of the page, or of the
    element *selector* names.

    ``library`` is ``(global, sentence)``: a page whose library did not load raises
    that sentence.  Raises ``ImportError`` without Playwright and
    :class:`SnapshotError` for anything else that keeps the picture from being
    made."""
    _require_playwright()
    width, height = int(width), int(height)
    if width <= 0 or height <= 0:
        raise ValueError(f"a picture needs a positive size, got {width}×{height}")

    def photograph(playwright: Any, get_browser: Callable[[], Any]) -> bytes:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        del playwright
        browser = get_browser()
        context = browser.new_context(
            viewport={"width": width, "height": height}, device_scale_factor=1
        )
        try:
            page = context.new_page()
            wait_ms = max(1, int(timeout * 1000))
            try:
                page.goto(url, wait_until="load", timeout=wait_ms)
            except PlaywrightTimeout:
                pass   # a script that never arrived: told apart just below
            except PlaywrightError as exc:
                said = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
                raise SnapshotError(f"{page_name} did not open ({said})") from None
            if library is not None and not page.evaluate(
                f"typeof {library[0]} !== 'undefined'"
            ):
                raise SnapshotError(library[1])
            try:
                page.wait_for_function(ready, timeout=wait_ms)
            except PlaywrightTimeout:
                raise SnapshotError(f"{page_name} sent no data in {timeout:g} s") from None
            if selector is None:
                return page.screenshot(type="png", timeout=wait_ms)
            _waits_two_frames(page)
            return page.locator(selector).screenshot(type="png", timeout=wait_ms)
        finally:
            context.close()

    return _engine.run(photograph, timeout)
