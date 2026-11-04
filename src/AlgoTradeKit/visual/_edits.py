"""
algotradekit.visual._edits
~~~~~~~~~~~~~~~~~~~~~~~~~~
Edits on the page that reach Python (v1.3.0).

A drawing with a ``key`` reports what the user does to it on the page: a move or
a resize to ``chart.on_drawing_edit(fn)``, a deletion to
``chart.on_drawing_delete(fn)``.  ``chart.add_action(label, fn)`` puts ``label`` in
a menu that a right-click on a candle opens.  Each callback gets a
:class:`DrawingEdit` or a :class:`ChartAction`, on a thread of the chart's own,
never on the page server's loop.  A callback that raises :class:`EditRefused`
puts the drawing back, in every open page.

Nothing here imports a sibling module: a chart calls the functions it is given.
"""
from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

#: The words ``editable`` takes: which handles of a drawing move once it is unlocked.
HANDLES = ("price", "time", "move")


@dataclass(frozen=True)
class DrawingEdit:
    """One change the user made to a keyed drawing on the page.

    ``before`` and ``after`` are the drawing's dicts, as ``to_dict()`` gives them;
    ``after`` is ``None`` for a deletion.
    """

    chart: Any
    drawing_id: str
    key: str
    before: dict
    after: dict | None


@dataclass(frozen=True)
class ChartAction:
    """A label picked from the candle menu.

    ``time`` is the opening of the candle under the pointer, in UTC milliseconds
    (to the right of the last candle, the opening of a candle not there yet), and
    ``price`` the pointer's price.
    """

    chart: Any
    label: str
    time: int
    price: float | None


class EditRefused(Exception):
    """Raise it in an edit or delete callback to undo the change.

    The drawing goes back in every open page, and the page's status bar shows the
    message: ``raise EditRefused("too tall")``.
    """


def check_editable(value) -> bool | tuple:
    """``True``, ``False``, or the tuple of ``"price"``, ``"time"`` and ``"move"``
    *value* names (a list is taken too, as JSON gives it back)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (tuple, list)):
        if not value:
            raise ValueError(f"editable: {value!r} names no handle — use False")
        for word in value:
            if word not in HANDLES:
                raise ValueError(f"editable: {word!r} is not 'price', 'time' or 'move'")
        return tuple(value)
    raise ValueError(
        f"editable: {value!r} is not True, False or a tuple of 'price', 'time' and 'move'"
    )


def check_edit_fields(payload: dict) -> None:
    """Refuse a drawing whose ``key``, ``editable``, ``deletable`` or ``title`` is
    not what the page can use."""
    key = payload.get("key")
    if key is not None and (not isinstance(key, str) or not key):
        raise ValueError(f"key: {key!r} is not a non-empty string")
    check_editable(payload.get("editable", True))
    deletable = payload.get("deletable", True)
    if not isinstance(deletable, bool):
        raise ValueError(f"deletable: {deletable!r} is not True or False")
    title = payload.get("title")
    if title is not None and not isinstance(title, str):
        raise ValueError(f"title: {title!r} is not a string")


def drawing_key(drawing) -> str | None:
    """A drawing's ``key``: a strategy's dict keeps it in its payload."""
    payload = getattr(drawing, "_payload", None)
    if isinstance(payload, dict):
        return payload.get("key")
    return getattr(drawing, "key", None)


class ChartEvents:
    """The thread one chart runs its callbacks and actions on, ``atk-chart-events``.

    Jobs run one at a time, in the order they were put, so a callback sees the
    user's changes in the order they were made.  The thread starts with the first
    job and is a daemon: it never keeps a program alive.
    """

    def __init__(self) -> None:
        self._jobs: queue.SimpleQueue = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def put(self, job: Callable[[], None]) -> None:
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="atk-chart-events", daemon=True
                )
                self._thread.start()
        self._jobs.put(job)

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Wait until every job put so far has run; ``False`` if *timeout* passed."""
        done = threading.Event()
        self.put(done.set)
        return done.wait(timeout)

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            try:
                job()
            except Exception as exc:          # each job reports its own; the last guard
                print(f"[AlgoTradeKit] chart: {exc}", flush=True)
