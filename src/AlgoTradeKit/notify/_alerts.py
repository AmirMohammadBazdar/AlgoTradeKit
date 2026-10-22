"""
AlgoTradeKit.notify._alerts
~~~~~~~~~~~~~~~~~~~~~~~~~~~
Price alerts without a strategy (v1.2.0, E6): ``notifier.watch(broker, symbol, …)``.

What fires (Q4.1 a)
    A level fires when the price crosses it: from below to ``above=`` or past it, from above
    to ``below=`` or past it.  Once — or, with ``repeat=True``, again after the price has gone
    back past the level by ``rearm`` percent of it.  An alert set on the wrong side waits for
    the price to come back and cross, and one line says so.
Which price (Q4.2 b)
    The last trade: the forming 1m candle's close, from ``broker.stream_candles`` — Binance
    sends it every 2 seconds, and MetaTrader is polled every second, its candles built from
    the bid.  ``on_close="1h"`` watches closed 1h candles instead.
A move (Q4.3 a)
    ``move_pct=-3, window="1h"`` fires when the price is 3% below the price one window ago.
    The first window's history is fetched when ``watch()`` is called, so it can fire at once.
    With ``repeat=True`` it fires again once a window has passed and the change is back short
    of the move by ``rearm`` percent.
Streams
    A price arrives on the broker's stream thread, which compares it with each alert's
    condition and hands a firing alert's message to the notifier's inbox: a stream never
    waits on Telegram, on a picture or on a REST call.  Alerts on one broker, symbol and
    timeframe share a stream, which stops when its last alert ends.  A stream that goes quiet
    gets one line, at most every 10 minutes; the broker's stream keeps its own retries.
Lifetime (Q4.4 a)
    In memory: a restart forgets the alerts.  ``notifier.wait()`` blocks until none is left
    armed, or until Ctrl+C, which cancels them; ``close()`` cancels them too.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any

from . import _template
from ._core import KIND_ALERT, PICTURE_CANDLES, Notifier, _Media, _SystemClock

#: An alert's states.
ARMED, FIRED, CANCELLED = "armed", "fired", "cancelled"

#: The stream a price comes from when nothing says otherwise: the forming 1m candle.
PRICE_TIMEFRAME = "1m"
#: The timeframe of an alert's picture when ``image=True`` and there is no ``on_close``.
PICTURE_TIMEFRAME = "15m"
#: A price stream that has said nothing for this long gets a line (a candle stream: two
#: of its candles); then at most one line this often, per stream.
QUIET_SECONDS = 60.0
QUIET_LINE_EVERY = 600.0
#: How often the watchdog looks at the streams, seconds.
LOOK_EVERY = 5.0
#: What ``wait()`` gives the alerts' last messages to go out, seconds.
DRAIN_SECONDS = 10.0

#: The fields a ``templates={"alert": …}`` template may use.
ALERT_FIELDS = (*_template.TEXT_FIELDS, "level", "side", "move_pct", "window", "condition",
                "source")

_LEVEL_COLOR = "#e3b341"
_DASH = "—"

#: The watchdog's clock; tests replace it.
_clock = _SystemClock()


def _say(line: str) -> None:
    print(f"[AlgoTradeKit] alerts: {line}", flush=True)


def _plural(count: int, noun: str) -> str:
    return f"{count:,} {noun}" if count == 1 else f"{count:,} {noun}s"


def _span(seconds: float) -> str:
    """«60 s», «30 min», «2h»."""
    if seconds < 120:
        return f"{seconds:g} s"
    if seconds < 7_200:
        return f"{round(seconds / 60):g} min"
    return f"{seconds / 3_600:g}h"


def _wait_for(condition: threading.Condition, timeout: float) -> None:
    """``wait()``'s pause; tests replace it to press Ctrl+C."""
    condition.wait(timeout)


class _Missing:
    """A field with no value, in a template of your own: «—»."""

    def __format__(self, spec: str) -> str:
        return _DASH

    def __str__(self) -> str:
        return _DASH


_MISSING = _Missing()


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------

class _Edge:
    """One level of a level alert, and which side of it the price was on."""

    __slots__ = ("level", "side", "beyond", "armed")

    def __init__(self, level: float, side: str, start: float) -> None:
        self.level = level
        self.side = side
        self.beyond = self.past(start)
        self.armed = True

    def past(self, price: float) -> bool:
        return price >= self.level if self.side == "above" else price <= self.level

    def back(self, price: float, rearm: float) -> bool:
        """Has the price gone back past the level by the band, to re-arm?"""
        band = self.level * rearm / 100
        if self.side == "above":
            return price <= self.level - band if band else price < self.level
        return price >= self.level + band if band else price > self.level


class _Move:
    """A move over a window: the prices of one window, and whether it may fire."""

    __slots__ = ("pct", "window_ms", "history", "armed", "fired_at")

    def __init__(self, pct: float, window_ms: int, history: list[tuple[int, float]]) -> None:
        self.pct = pct
        self.window_ms = window_ms
        self.history: deque[tuple[int, float]] = deque(sorted(history))
        self.armed = True
        self.fired_at: int | None = None

    def change(self, stamp: int, price: float) -> tuple[float, float] | None:
        """``(change in percent, the price one window ago)``, once there is one."""
        self.history.append((stamp, price))
        bound = stamp - self.window_ms
        while len(self.history) > 1 and self.history[1][0] <= bound:
            self.history.popleft()
        then, reference = self.history[0]
        if then > bound or reference <= 0:
            return None
        return (price - reference) / reference * 100, reference

    def met(self, change: float) -> bool:
        return change <= self.pct if self.pct < 0 else change >= self.pct

    def back(self, change: float, rearm: float) -> bool:
        return change > self.pct + rearm if self.pct < 0 else change < self.pct - rearm


# ---------------------------------------------------------------------------
# The alert
# ---------------------------------------------------------------------------

class Alert:
    """One price alert: what ``notifier.watch()`` returns (v1.2.0).

    ``symbol``; ``condition``, in words; ``state`` — ``"armed"``, ``"fired"`` (a once-alert
    that went off) or ``"cancelled"``; ``fired``, how often it went off; ``last_price``, the
    newest price it saw.  ``cancel()`` ends it."""

    def __init__(
        self, notifier: Notifier, broker: Any, symbol: str, *, edges: list[_Edge],
        move: _Move | None, window: str | None, on_close: str | None, repeat: bool,
        rearm: float, image_tf: str | None, to: Any, silent: bool | None,
    ) -> None:
        self.notifier = notifier
        self.broker = broker
        self.symbol = symbol
        self.source = str(getattr(broker, "name", "") or type(broker).__name__)
        self._edges = edges
        self._move = move
        self.window = window
        self.on_close = on_close
        self.repeat = repeat
        self.rearm = rearm
        self.image_tf = image_tf
        self.to = to
        self.silent = silent
        self.fired = 0
        self.last_price: float | None = None
        self._state = ARMED
        self._lock = threading.Lock()
        self._stream: _PriceStream | None = None
        self.condition = self._words()

    @property
    def state(self) -> str:
        return self._state

    def _words(self) -> str:
        if self._move is not None:
            closes = f" on {self.on_close} closes" if self.on_close else ""
            return f"{self._move.pct:+g}% in {self.window}{closes}"
        close = f"{self.on_close} close " if self.on_close else ""
        return " or ".join(f"{close}{edge.side} {_template.Number(edge.level)}"
                           for edge in self._edges)

    def __repr__(self) -> str:
        return f"<Alert {self.symbol} {self.condition} · {self._state}>"

    def cancel(self) -> None:
        """End the alert: it fires no more, and its stream stops when no alert needs it."""
        with self._lock:
            if self._state != ARMED:
                return
            self._state = CANCELLED
        self._end()

    def _end(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.unsubscribe(self)
        self.notifier._alert_ended(self)

    # ---- on the broker's stream thread: compare, and post --------------

    def _on_candle(self, candle: dict[str, Any], tf_ms: int, closed: bool) -> None:
        try:
            price = float(candle["close"])
        except (KeyError, TypeError, ValueError):
            return
        if closed:
            stamp = int(candle["timestamp"]) + tf_ms   # a closed candle's price is its close's
        else:
            stamp = int(self.notifier._clock.time() * 1000)
        with self._lock:
            if self._state != ARMED:
                return
            self.last_price = price
            what = self._check(stamp, price)
            if what is None:
                return
            self.fired += 1
            done = not self.repeat
            if done:
                self._state = FIRED
        received = self.notifier._clock.time()
        self.notifier._post(lambda: _send(self, what, received))
        if done:
            self._end()

    def _check(self, stamp: int, price: float) -> dict[str, Any] | None:
        """The firing, or ``None``: what the message says and what its picture draws."""
        move = self._move
        if move is not None:
            found = move.change(stamp, price)
            if found is None:
                return None
            change, reference = found
            if not move.armed:
                if (move.fired_at is not None and stamp - move.fired_at >= move.window_ms
                        and move.back(change, self.rearm)):
                    move.armed = True
                return None
            if not move.met(change):
                return None
            move.armed = False
            move.fired_at = stamp
            side = "below" if change < 0 else "above"
            verb = "fell" if change < 0 else "rose"
            closes = f" on {self.on_close} closes" if self.on_close else ""
            return {"price": price, "level": reference, "side": side, "move_pct": change,
                    "condition": f"{verb} {abs(change):.1f}% in {self.window}{closes}",
                    "lines": [reference]}
        crossed = None
        for edge in self._edges:
            now_past = edge.past(price)
            if not edge.armed and edge.back(price, self.rearm):
                edge.armed = True
            if edge.armed and now_past and not edge.beyond and crossed is None:
                crossed = edge
            edge.beyond = now_past
        if crossed is None:
            return None
        crossed.armed = False
        verb = f"{self.on_close} closed" if self.on_close else "crossed"
        return {"price": price, "level": crossed.level, "side": crossed.side, "move_pct": None,
                "condition": f"{verb} {crossed.side} {_template.Number(crossed.level)}",
                "lines": [edge.level for edge in self._edges]}


# ---------------------------------------------------------------------------
# The message, on the notifier's thread
# ---------------------------------------------------------------------------

def _values(alert: Alert, what: dict[str, Any], received: float) -> dict[str, Any]:
    zone = alert.notifier._zone
    time = _template.Moment(received, zone)
    price = _template.Number(what["price"])
    change = what["move_pct"]
    return {
        "time": time,
        "date": _template.Day(received, zone),
        "symbol": alert.symbol,
        "price": price,
        "header": f"🕑 {time} · {alert.symbol} {price}",
        "level": _template.Number(what["level"]),
        "side": what["side"],
        "move_pct": None if change is None else f"{change:+.1f}%",
        "window": alert.window,
        "condition": what["condition"],
        "source": alert.source,
    }


def _body(notifier: Notifier, values: dict[str, Any]) -> str | None:
    custom, template = notifier._template_for(KIND_ALERT)
    rich = notifier._rich
    if custom:
        if template is None:
            return None
        filled = {name: _MISSING if values.get(name) is None else values[name]
                  for name in ALERT_FIELDS}
        return _template.render(template, filled, escape=rich)
    first = _template.render("🔔 {symbol} {condition} · now {price}", values, escape=rich)
    if rich:
        first = f"<b>{first}</b>"
    return first + "\n" + _template.render("🕑 {time} · {source}", values, escape=rich)


def _send(alert: Alert, what: dict[str, Any], received: float) -> None:
    notifier = alert.notifier
    body = _body(notifier, _values(alert, what, received))
    if body is None:
        return   # templates={"alert": None}: it fired, and says nothing
    dests = notifier._select(KIND_ALERT, alert.symbol, alert.to)
    if not dests:
        return
    media = None
    if alert.image_tf is not None:
        media = _Media(_Media.PHOTO, make=lambda: _picture(alert, what), fallback=True,
                       label=f"alert {alert.symbol}", filename="alert.png")
    raw = None
    if notifier._wants_message:   # the webhook posts what fired, raw (v1.2.0)
        raw = {"source": alert.source, "condition": what["condition"], "level": what["level"],
               "side": what["side"], "move_pct": what["move_pct"], "window": alert.window,
               "price": what["price"]}
    for dest in dests:
        silent = dest.silent if alert.silent is None else bool(alert.silent)
        notifier._queue_event(dest, KIND_ALERT, alert.symbol, body, queued_at=received,
                              silent=silent, media=media, fields=raw)


def _picture(alert: Alert, what: dict[str, Any]) -> bytes:
    """The last 100 candles of the picture's timeframe, with the level as a line."""
    from ..visual import Chart
    from ._pictures import timeframe_ms

    timeframe = alert.image_tf
    assert timeframe is not None
    step = timeframe_ms(timeframe)
    end = int(alert.notifier._clock.time() * 1000)
    frame = alert.broker.get_candles_df(alert.symbol, timeframe,
                                        end - (PICTURE_CANDLES - 1) * step, end)
    if not len(frame):
        raise ValueError(f"no {alert.symbol} {timeframe} candles to draw")
    chart = Chart(title=alert.symbol, source_timeframe=timeframe)
    chart.set_data(frame)
    for level in what["lines"]:
        chart.add_hline(float(level), color=_LEVEL_COLOR, line_width=2,
                        label=str(_template.Number(level)))
    return chart.screenshot(timezone=alert.notifier._zone)


# ---------------------------------------------------------------------------
# The streams, shared by the alerts on one broker, symbol and timeframe
# ---------------------------------------------------------------------------

class _PriceStream:
    __slots__ = ("key", "broker", "symbol", "timeframe", "closed", "tf_ms", "alerts", "handle",
                 "last_seen", "lined_at", "stopped")

    def __init__(self, key: tuple, broker: Any, symbol: str, timeframe: str, closed: bool,
                 tf_ms: int) -> None:
        self.key = key
        self.broker = broker
        self.symbol = symbol
        self.timeframe = timeframe
        self.closed = closed
        self.tf_ms = tf_ms
        self.alerts: list[Alert] = []
        self.handle: Any = None
        self.last_seen = _clock.monotonic()
        self.lined_at: float | None = None
        self.stopped = False

    def _on_candle(self, candle: dict[str, Any]) -> None:
        """On the broker's stream thread."""
        self.last_seen = _clock.monotonic()
        with _streams_lock:
            alerts = list(self.alerts)
        for alert in alerts:
            try:
                alert._on_candle(candle, self.tf_ms, self.closed)
            except Exception as exc:  # noqa: BLE001 — an alert never stops a stream
                _say(f"an alert on {self.symbol} failed ({type(exc).__name__}: {exc})")

    def quiet_after(self) -> float:
        return 2 * self.tf_ms / 1000 if self.closed else QUIET_SECONDS

    def unsubscribe(self, alert: Alert) -> None:
        with _streams_lock:
            if alert in self.alerts:
                self.alerts.remove(alert)
            if self.alerts or self.stopped:
                return
            self.stopped = True
            if _streams.get(self.key) is self:
                del _streams[self.key]
            handle = self.handle
        if handle is not None:   # never from its own thread: stop() joins it
            threading.Thread(target=_stop_quietly, args=(handle,), name="atk-alerts-stop",
                             daemon=True).start()


_streams: dict[tuple, _PriceStream] = {}
_streams_lock = threading.Lock()
_watchdog: threading.Thread | None = None


def _stop_quietly(handle: Any) -> None:
    try:
        handle.stop()
    except Exception:  # noqa: BLE001 — a stream that will not stop is a daemon thread anyway
        pass


def _subscribe(alert: Alert, timeframe: str, closed: bool, tf_ms: int) -> _PriceStream:
    """The alert on its shared stream; the stream is opened here when it is the first."""
    key = (id(alert.broker), alert.symbol, timeframe, closed)
    with _streams_lock:
        stream = _streams.get(key)
        fresh = stream is None
        if fresh:
            stream = _streams[key] = _PriceStream(key, alert.broker, alert.symbol, timeframe,
                                                  closed, tf_ms)
        stream.alerts.append(alert)
    if fresh:
        try:
            handle = alert.broker.stream_candles(alert.symbol, timeframe, stream._on_candle,
                                                 closed_only=closed)
        except BaseException:
            with _streams_lock:
                stream.alerts.remove(alert)
                stream.stopped = True
                if _streams.get(key) is stream:
                    del _streams[key]
            raise
        with _streams_lock:
            stream.handle = handle
            stop_now = stream.stopped
        if stop_now:   # its alerts ended while it opened
            _stop_quietly(handle)
        _ensure_watchdog()
    return stream


def _ensure_watchdog() -> None:
    global _watchdog
    with _streams_lock:
        if _watchdog is not None and _watchdog.is_alive():
            return
        _watchdog = threading.Thread(target=_watch_streams, name="atk-alerts", daemon=True)
        _watchdog.start()


def _watch_streams() -> None:
    """Say so when a stream has gone quiet; end when there is none."""
    pause = threading.Event()
    while True:
        pause.wait(LOOK_EVERY)
        with _streams_lock:
            streams = list(_streams.values())
        if not streams:
            return
        now = _clock.monotonic()
        for stream in streams:
            quiet = now - stream.last_seen
            if quiet < stream.quiet_after():
                continue
            if stream.lined_at is not None and now - stream.lined_at < QUIET_LINE_EVERY:
                continue
            stream.lined_at = now
            what = (f"{stream.timeframe} candle" if stream.closed else "price")
            _say(f"no {stream.symbol} {what} from "
                 f"{getattr(stream.broker, 'name', 'the broker')} for "
                 f"{_span(stream.quiet_after())} — the feed keeps reconnecting")


# ---------------------------------------------------------------------------
# watch() and wait()
# ---------------------------------------------------------------------------

def _number(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number, got {type(value).__name__}")
    return float(value)


def watch(
    notifier: Notifier, broker: Any, symbol: str, *, above: Any, below: Any, move_pct: Any,
    window: Any, on_close: Any, repeat: Any, rearm: Any, image: Any, to: Any, silent: Any,
) -> Alert:
    """``Notifier.watch``: everything is checked here, before anything listens."""
    from ._pictures import timeframe_ms

    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("watch needs a symbol, such as 'BTCUSDT'")
    symbol = symbol.strip()
    levels = {side: _number(side, value) for side, value in (("above", above), ("below", below))
              if value is not None}
    if move_pct is not None and levels:
        raise ValueError("an alert watches a level (above=, below=) or a move "
                         "(move_pct= with window=), not both")
    if move_pct is None and not levels:
        raise ValueError("watch needs a level (above=, below=) or a move "
                         "(move_pct= with window=)")
    if any(level <= 0 for level in levels.values()):
        raise ValueError("a level must be above 0")
    if "above" in levels and "below" in levels and levels["above"] <= levels["below"]:
        raise ValueError(f"above= ({levels['above']:g}) must be over below= "
                         f"({levels['below']:g})")
    window_ms = None
    if move_pct is not None:
        move_pct = _number("move_pct", move_pct)
        if move_pct == 0 or move_pct <= -100:
            raise ValueError("move_pct must be a rise above 0 or a fall above -100, in percent")
        if window is None:
            raise ValueError("a move needs window=, such as '1h'")
        window_ms = timeframe_ms(window)
    elif window is not None:
        raise ValueError("window= is for a move (move_pct=)")
    closes_ms = timeframe_ms(on_close) if on_close is not None else None
    if not isinstance(repeat, bool):
        raise TypeError(f"repeat must be True or False, got {type(repeat).__name__}")
    rearm = _number("rearm", rearm)
    if rearm < 0:
        raise ValueError("rearm must be 0 or more, in percent")
    if image is True:
        image_tf = on_close or PICTURE_TIMEFRAME
    elif image is False or image is None:
        image_tf = None
    elif isinstance(image, str):
        timeframe_ms(image)
        image_tf = image
    else:
        raise TypeError(f"image must be True, False or a timeframe such as '1h', "
                        f"got {type(image).__name__}")
    if silent is not None and not isinstance(silent, bool):
        raise TypeError(f"silent must be True, False or None, got {type(silent).__name__}")
    notifier._select(KIND_ALERT, symbol, to)      # an unknown chat raises now
    custom, template = notifier._template_for(KIND_ALERT)
    if custom and template is not None:
        try:
            _template.fields_of(template, ALERT_FIELDS)
        except ValueError as exc:
            said = str(exc)
            if said.startswith("unknown field "):
                field = said[len("unknown field "):].split(" — ", 1)[0]
                raise ValueError(f"template for 'alert' uses {field} — known: "
                                 f"{', '.join(ALERT_FIELDS)}") from None
            raise ValueError(f"template for 'alert': {said}") from None
    if image_tf is not None:
        from ..visual import _snapshot

        _snapshot.check_engine()                  # no picture engine: refused now (Q3.7 a)

    timeframe = on_close or PRICE_TIMEFRAME
    tf_ms = closes_ms or timeframe_ms(PRICE_TIMEFRAME)
    start = float(broker.get_ticker(symbol).last)   # a wrong symbol raises the broker's error
    edges = [_Edge(level, side, start) for side, level in levels.items()]
    move = None
    if move_pct is not None:
        assert window_ms is not None
        now = int(notifier._clock.time() * 1000)
        frame = broker.get_candles_df(symbol, timeframe, now - window_ms - 2 * tf_ms, now)
        history = [(int(stamp) + tf_ms, float(close))
                   for stamp, close in zip(frame["timestamp"].tolist(), frame["close"].tolist())
                   if int(stamp) + tf_ms <= now]
        move = _Move(move_pct, window_ms, history)
    alert = Alert(notifier, broker, symbol, edges=edges, move=move, window=window,
                  on_close=on_close, repeat=repeat, rearm=rearm, image_tf=image_tf, to=to,
                  silent=silent)
    notifier._alert_added(alert)
    try:
        stream = _subscribe(alert, timeframe, on_close is not None, tf_ms)
    except BaseException:
        alert._state = CANCELLED
        notifier._alert_ended(alert)
        raise
    alert._stream = stream
    if alert.state != ARMED:   # a first candle inside stream_candles already ended it
        stream.unsubscribe(alert)
        return alert
    for edge in edges:
        if edge.beyond:
            _say(f"{symbol} is already {edge.side} {_template.Number(edge.level)} "
                 f"({_template.Number(start)}) — the alert fires when it crosses again")
    if move is not None and on_close is None and move.history:
        alert._on_candle({"close": start}, tf_ms, closed=False)   # it may fire at once
    return alert


def wait(notifier: Notifier) -> None:
    """``Notifier.wait``: until no alert is left armed, or Ctrl+C; then the queue's last
    messages, for up to :data:`DRAIN_SECONDS`."""
    try:
        with notifier._alerts_changed:
            while any(alert.state == ARMED for alert in notifier._alerts):
                _wait_for(notifier._alerts_changed, 0.5)
    except KeyboardInterrupt:
        armed = [alert for alert in list(notifier._alerts) if alert.state == ARMED]
        for alert in armed:
            alert.cancel()
        _say(f"Ctrl+C — {_plural(len(armed), 'alert')} cancelled")
    notifier._drain(DRAIN_SECONDS)
