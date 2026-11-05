"""
AlgoTradeKit.trader._chart_edits
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Edits on the chart reach the strategy (v1.3.0).

A chart runs its edit and action callbacks on a thread of its own (``visual``).
The bridge a live session puts on its charts takes the session's step lock there,
so the strategy's hook runs between two candles — at once, or right after the
candle in progress — and never sees half a candle.  What the strategy accepts is
saved to ``TraderConfig.chart_edits_path``, and read back at the next start.

``strategy`` and ``visual`` know nothing of each other: this is where they meet,
and only ``trader`` imports it.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from typing import Any

#: The reason a session stops for «Re-run with edits»: run_live starts again.
RERUN_REASON = "re-run with chart edits"
RERUN_LABEL = "Re-run with edits"
RERUN_CONFIRM = "Re-run with the saved edits? The session stops and starts again."


def load_chart_edits(path: str | None) -> tuple[dict, ...]:
    """The records saved at *path*: ``()`` when there is no file.  A file that is
    not a list of records raises ``ValueError`` — a run that quietly dropped your
    edits would not be the run you asked for."""
    if path is None or not os.path.exists(path):
        return ()
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    try:
        records = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"chart_edits_path: {path} is not a list of edits — {exc}") from None
    if not isinstance(records, list):
        raise ValueError(f"chart_edits_path: {path} is not a list of edits — it holds a "
                         f"{type(records).__name__}")
    for number, record in enumerate(records, 1):
        if not isinstance(record, dict):
            raise ValueError(f"chart_edits_path: {path} is not a list of edits — item "
                             f"{number} is not a record")
    return tuple(records)


class EditsFile:
    """The saved edits at one path, shared by every pair of a session that names
    it.  Each record is appended under a lock, written to a temporary file and
    renamed over the old one, so a crash never leaves half a file."""

    def __init__(self, path: str, records: tuple[dict, ...] = ()) -> None:
        self.path = path
        self._records = list(records)
        self._lock = threading.Lock()

    @property
    def records(self) -> tuple[dict, ...]:
        with self._lock:
            return tuple(self._records)

    def append(self, record: dict) -> None:
        saved = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock:
            self._records.append({"saved": saved, **record})
            folder = os.path.dirname(os.path.abspath(self.path))
            handle, temporary = tempfile.mkstemp(prefix=".chart-edits-", suffix=".json",
                                                 dir=folder)
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as fh:
                    json.dump(self._records, fh, indent=1)
                    fh.write("\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(temporary, self.path)
            except BaseException:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
                raise


def open_edits_files(configs) -> dict[str, EditsFile]:
    """One :class:`EditsFile` per path the configs name (two pairs may share one),
    read now: a bad file stops the start before the venue is asked anything."""
    files: dict[str, EditsFile] = {}
    for config in configs:
        path = config.chart_edits_path
        if path is not None and os.path.abspath(path) not in files:
            files[os.path.abspath(path)] = EditsFile(path, load_chart_edits(path))
    return files


def edits_file_for(config, files: dict[str, EditsFile]) -> EditsFile | None:
    path = config.chart_edits_path
    return None if path is None else files[os.path.abspath(path)]


def chart_drawer(chart) -> Any:
    """``BaseStrategy._draw_hook`` for *chart*: ``draw`` puts the drawing on it (a
    key it already holds changes that drawing), ``erase`` takes one off."""
    from ..visual.indicator_renderer import erase_strategy_drawing, put_strategy_drawing

    def hook(kind: str, payload) -> None:
        if kind == "draw":
            put_strategy_drawing(chart, payload)
        elif kind == "erase":
            erase_strategy_drawing(chart, payload)

    return hook


def drop_drawing(kind: str, payload) -> None:
    """``BaseStrategy._draw_hook`` for a session with no chart: nothing to draw on."""


class ChartBridge:
    """
    One strategy's routing from the charts a session shows to its hooks.

    *lock* is the lock the session's candle step holds: every hook runs with it.
    *followers* are ``(strategy, lock)`` pairs told what the strategy accepted —
    the Trader's display steps a copy of its own, which must not go on drawing or
    trading a zone the user deleted.  *draw_hook*, when given, is armed on the
    strategy while a hook runs (a Trader arms it only around its own steps).
    *clock* gives the opening of the last candle the session closed: a saved edit
    carries it as ``"at"`` (v1.3.0).
    """

    def __init__(self, strategy, lock, *, edits: EditsFile | None = None, followers=(),
                 draw_hook=None, clock=None) -> None:
        self.strategy = strategy
        self.lock = lock
        self.edits = edits
        self.followers = list(followers)
        self.draw_hook = draw_hook
        #: The opening of the last candle the session closed (UTC ms), or None.
        self.clock = clock
        self._wired: dict[int, Any] = {}

    def wire(self, chart, actions=()) -> None:
        """Route *chart*'s edits, deletes and *actions* to the strategy.  A chart is
        wired once; a label it already has is left as it is."""
        actions = tuple(actions)
        for label in actions:
            if not isinstance(label, str) or not label:
                raise ValueError(f"chart action label {label!r} is not a non-empty string")
        if id(chart) not in self._wired:
            self._wired[id(chart)] = chart
            chart.on_drawing_edit(self._on_edit)
            chart.on_drawing_delete(self._on_delete)
        for label in actions:
            if label not in chart.actions:
                chart.add_action(label, self._on_action)

    # ── the hooks, on the chart's thread ─────────────────────────────────────

    def _on_edit(self, edit) -> None:
        with self.lock, self._armed():
            if self.strategy.on_chart_edit(edit) is False:
                from ..visual._edits import EditRefused

                raise EditRefused(f"{type(self.strategy).__name__} refused it")
            self._tell_followers("on_chart_edit", edit)
            record = {"type": "edit", "chart": edit.chart.title, "key": edit.key,
                      "drawing": edit.after}
            at = self.clock() if self.clock is not None else None
            if at is not None:
                # When it was made: a strategy that replays an edit from the start
                # can tell what came before it from what came after.
                record["at"] = int(at)
            self._save(record)

    def _on_delete(self, edit) -> None:
        with self.lock, self._armed():
            if self.strategy.on_chart_edit(edit) is False:
                from ..visual._edits import EditRefused

                raise EditRefused(f"{type(self.strategy).__name__} refused it")
            self._tell_followers("on_chart_edit", edit)
            self._save({"type": "delete", "chart": edit.chart.title, "key": edit.key})

    def _on_action(self, action) -> None:
        with self.lock, self._armed():
            self.strategy.on_chart_action(action)
            self._tell_followers("on_chart_action", action)
            self._save({"type": "action", "chart": action.chart.title, "label": action.label,
                        "time": action.time, "price": action.price})

    def _tell_followers(self, hook: str, event) -> None:
        for follower, lock in self.followers:
            try:
                with lock:
                    getattr(follower, hook)(event)
            except Exception as exc:     # the copy only follows; the strategy decided
                print(f"[AlgoTradeKit] chart: the display's copy failed {hook} — "
                      f"{type(exc).__name__}: {exc}", flush=True)

    def _save(self, record: dict) -> None:
        if self.edits is not None:
            self.edits.append(record)

    def _armed(self):
        return _Armed(self.strategy, self.draw_hook)


class _Armed:
    """Arms the strategy's draw hook for the length of a chart hook, when the
    session arms it only around its own steps."""

    def __init__(self, strategy, hook) -> None:
        self.strategy, self.hook, self.before = strategy, hook, None

    def __enter__(self) -> None:
        if self.hook is not None:
            self.before = self.strategy.__dict__.get("_draw_hook")
            self.strategy._draw_hook = self.hook

    def __exit__(self, *exc) -> None:
        if self.hook is not None:
            self.strategy._draw_hook = self.before
