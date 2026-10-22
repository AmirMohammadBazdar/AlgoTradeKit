"""
AlgoTradeKit.notify._commands
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Commands from Telegram (v1.2.0, E7): ``TelegramNotifier(…, commanders=[123456789])``
listens, and answers /status, /positions, /chart, /stop and /help.

Who (Q5.1 a)
    Only the people whose Telegram ids ``commanders=`` lists, whatever chat they write in:
    their private chat with the bot, or a group it is in, where Telegram's privacy mode
    delivers ``/status@thebot``.  Anyone else's first command gets one answer, «🚫 Only
    this bot's commanders can use /stop.», and every refused one a line with the sender's
    id and the command's name only (Q5.6 b).  A channel post, an edited message, a message
    sent as a chat and another bot's message never command.
What (Q5.3 b)
    /status — the trading sessions this notifier serves: since when, today's trades and
    net, the open positions and their P&L now; /positions — each open position; /chart — a
    picture of the last 100 candles; /stop; /help, which /start answers too.  Every answer
    is a reply to its command, in the chat it came from, and ends with the 🕑 line.
/stop (Q5.2 a, Q5.4 a)
    Paper trading stops at once.  Live trading asks first: «Send /stop CONFIRM within 60
    s», from the same person in the same chat.  Either way it is the path Ctrl+C takes,
    with the Trader's ``on_stop``, and the stop message says «/stop from Telegram».
Receiving (Q5.5 a)
    Long polling: one ``getUpdates`` open for up to 30 s, on a thread of its own, from the
    notifier's creation until ``close()``.  It confirms what it read, and skips every
    command sent before it listened (Q5.7 a), with one line.  One listener per bot in a
    program; another program reading the same bot is named in a line (409).
Prices (Q5.8 b)
    An answer values the open positions at a price asked from the pair's broker on the
    listener's thread; when the broker does not answer, at the runner's own newest price,
    which the answer names («14:00 close»).
Trading never waits
    Slow work — the prices, a picture's candles — runs on the listener's thread.  The answer
    is written on the notifier's thread, where the day's counts live.  /stop only sets the
    flag Ctrl+C would set.

A trading session (``trader/_notify.py``'s ``NotifySession``) gives ``mode``,
``on_stop``, ``started_at``, ``markets(notifier)`` (read on the listener's thread),
``now(notifier, prices)`` and ``chart(notifier, symbol, timeframe)`` (on the notifier's
thread), and ``stop(reason)``.
"""

from __future__ import annotations

import html
import math
import re
import threading
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any, NamedTuple

from . import _template
from ._core import Notifier, _Media, _SystemClock
from ._telegram import TelegramChatInfo, _chats_in

#: How long one ``getUpdates`` waits for a message, seconds (long polling).
POLL_SECONDS = 30
#: How long a live /stop waits for its CONFIRM, seconds.
CONFIRM_SECONDS = 60.0
#: The pauses after Telegram could not be reached, the last one repeated.
RETRY_STEPS = (1.0, 5.0, 15.0)
#: An outage this long gets a line, and then one at most this often, seconds; so does a
#: 409, from the first.
QUIET_AFTER = 60.0
LINE_EVERY = 600.0
#: After a 409 — another program asked for the same bot's updates — this pause, seconds.
CONFLICT_PAUSE = 5.0
#: A refused person gets a line at most this often, seconds.
REFUSED_EVERY = 60.0

#: The commands in /help's order, with the line /help and the bot's menu give each.
COMMANDS = (
    ("status", "what is trading, since when, today's net, open positions"),
    ("positions", "each open position: entry, SL, next TP, P&L now"),
    ("chart", "a picture of the last 100 candles"),
    ("stop", "stop trading (live trading asks you to confirm)"),
    ("help", "this list"),
)

#: The listener's thread.
THREAD_NAME = "atk-notify-telegram-commands"
#: What the stop message names as the reason.
STOP_REASON = "/stop from Telegram"

_COMMAND = re.compile(r"/([A-Za-z0-9_]{1,32})(?:@([A-Za-z0-9_]+))?(?:\s+(.*))?\Z", re.DOTALL)
_ANSWER = "answer"              # the label of an answer's message: no kind, so no filter
_CONFIRM = "confirm"
_MODES = {"paper": "Paper trading", "live": "Live trading"}
_SOURCES = {"paper": "SIM", "live": "LIVE"}
_ON_STOP_CLOSE_ALL = "close_all"

#: The listener's own waits — its pauses, and the spacing of its lines; tests replace it.
_clock = _SystemClock()

#: The listener reading each bot's updates in this program, by token, while it runs.
_listening: dict[str, Listener] = {}
_listening_lock = threading.Lock()


def listening(token: str) -> Listener | None:
    """The listener reading *token*'s updates in this program, if one is."""
    with _listening_lock:
        return _listening.get(token)


def check_free(token: str) -> None:
    """A second notifier of one bot with ``commanders=`` raises: a bot has one update
    consumer."""
    if listening(token) is not None:
        raise ValueError("this bot already takes commands in this program — give "
                         "commanders= to one notifier")


def _plural(count: int, noun: str) -> str:
    return f"{count:,} {noun}" if count == 1 else f"{count:,} {noun}s"


def _span(seconds: float) -> str:
    """«6h 30m», «45s»: the two largest units, as the terminal prints a duration."""
    left = max(0, int(seconds))
    parts = []
    for unit, size in (("d", 86_400), ("h", 3_600), ("m", 60), ("s", 1)):
        if left >= size:
            parts.append(f"{left // size}{unit}")
            left %= size
    return " ".join(parts[:2]) if parts else "0s"


def _ticker(broker: Any, symbol: str) -> float | None:
    """The broker's last price for *symbol*, or ``None`` when it does not answer."""
    try:
        price = float(broker.get_ticker(symbol).last)
    except Exception:  # noqa: BLE001 — the answer falls back to the runner's price
        return None
    return price if math.isfinite(price) and price > 0 else None


def _one_line(exc: BaseException) -> str:
    said = str(exc).strip()
    return said.splitlines()[0] if said else type(exc).__name__


class _Command(NamedTuple):
    name: str          # lower case, without the slash
    args: str          # what follows it
    sent: str          # as a line names it: «/stop CONFIRM»


class Listener:
    """One bot's commands: the thread that reads its updates, the trading sessions it
    answers for, and what it remembers between commands."""

    def __init__(self, notifier: Notifier, commanders: frozenset[int]) -> None:
        self.notifier = notifier
        self.commanders = commanders
        self.username: str | None = getattr(notifier, "_bot_username", None)
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._sessions: list[Any] = []
        self._running = False
        self._off = False                    # Telegram refused it for good: it stays off
        self._generation = 0
        self._ever = False                   # it listened before, in this program
        self._offset: int | None = None
        self._chats: dict[int, TelegramChatInfo] = {}   # every chat its updates named
        self._answered: set[Any] = set()     # refused senders already answered once
        self._refused: dict[Any, list] = {}  # sender -> [its last line, refusals since]
        self._pending: tuple[int, int, float] | None = None   # a live /stop: who, where, until

    # ------------------------------------------------------------------
    # Lifetime
    # ------------------------------------------------------------------

    def resume(self) -> None:
        """Listen, unless it does already, or Telegram turned it off.  Another notifier of
        the same bot that listens in this program keeps the commands: one line says so."""
        if self._off:
            return
        token = self.notifier._token
        with _listening_lock:
            held = _listening.get(token)
            if held is None:
                _listening[token] = self
        if held is not None and held is not self:
            self.notifier._say("another notifier of this bot takes its commands in this "
                               "program — this one does not listen")
            return
        with self._lock:
            if self._running:
                return
            self._running = True
            self._generation += 1
            generation, first = self._generation, not self._ever
            self._ever = True
        threading.Thread(target=self._run, args=(generation, first), name=THREAD_NAME,
                         daemon=True).start()

    def pause(self) -> None:
        """Stop listening: ``close()``, or a refusal nothing will fix."""
        with self._lock:
            self._running = False
            self._generation += 1
            self._pending = None
            self._cond.notify_all()
        with _listening_lock:
            if _listening.get(self.notifier._token) is self:
                del _listening[self.notifier._token]

    def _alive(self, generation: int) -> bool:
        with self._lock:
            return self._running and generation == self._generation

    def _halt(self, generation: int, line: str) -> None:
        """A refusal nothing here will fix: one line, and commands stay off."""
        if self._alive(generation):
            self._off = True
            self.notifier._say(line)
            self.pause()

    def _sleep(self, seconds: float, generation: int) -> None:
        until = _clock.monotonic() + seconds
        with self._cond:
            while self._running and generation == self._generation:
                left = until - _clock.monotonic()
                if left <= 0:
                    return
                _clock.wait(self._cond, left)

    # ------------------------------------------------------------------
    # The trading sessions
    # ------------------------------------------------------------------

    def add(self, session: Any) -> None:
        with self._lock:
            if session not in self._sessions:
                self._sessions.append(session)
        self.resume()

    def remove(self, session: Any) -> None:
        with self._lock:
            if session in self._sessions:
                self._sessions.remove(session)

    def sessions(self) -> list[Any]:
        with self._lock:
            return list(self._sessions)

    def seen(self) -> list[TelegramChatInfo]:
        """Every chat the updates named since the listener first listened."""
        with self._lock:
            return list(self._chats.values())

    # ------------------------------------------------------------------
    # The listener's thread
    # ------------------------------------------------------------------

    def _run(self, generation: int, first: bool) -> None:
        notifier = self.notifier
        started = notifier._clock.time()   # what was sent before this is skipped (Q5.7 a)
        menus = False
        failures = 0
        down_since: float | None = None
        down_line_at: float | None = None
        conflict_line_at: float | None = None
        while self._alive(generation):
            if self.username is None:
                answer = notifier._call("getMe")
            elif not menus:
                menus = True
                self._set_menus(generation)
                continue
            else:
                answer = notifier._poll(self._offset, POLL_SECONDS)
            if not self._alive(generation):
                return   # close() came while the request was out: nothing it read counts
            if answer.ok:
                if down_line_at is not None:
                    notifier._say("commands: Telegram reachable again")
                failures, down_since, down_line_at = 0, None, None
                if self.username is None:
                    self.username = str(answer.fields.get("username") or "")
                else:
                    self._read(answer.result, started, first, generation)
                continue
            if answer.status == 409:
                if "webhook" in answer.reason.lower():
                    self._halt(generation, "commands: this bot sends its updates to a "
                                           "webhook — getUpdates cannot run beside it; "
                                           "commands are off")
                    return
                now = _clock.monotonic()
                if conflict_line_at is None or now - conflict_line_at >= LINE_EVERY:
                    conflict_line_at = now
                    notifier._say("another program is reading this bot's updates (409 "
                                  "Conflict) — a command may reach it instead of this one")
                self._sleep(CONFLICT_PAUSE, generation)
                continue
            if answer.status in (401, 404):
                self._halt(generation, f"commands: Telegram rejected the token "
                                       f"({answer.reason}) — commands are off")
                return
            if not answer.unreachable:
                self._halt(generation, f"commands: Telegram answered {answer.reason} — "
                                       "commands are off")
                return
            failures += 1
            now = _clock.monotonic()
            if down_since is None:
                down_since = now
            if now - down_since >= QUIET_AFTER and (
                    down_line_at is None or now - down_line_at >= LINE_EVERY):
                down_line_at = now
                minutes = int((now - down_since) // 60)
                notifier._say(f"commands: Telegram unreachable for {minutes} min "
                              f"({answer.reason}) — still trying")
            pause = RETRY_STEPS[min(failures, len(RETRY_STEPS)) - 1]
            self._sleep(max(pause, answer.retry_after), generation)

    def _set_menus(self, generation: int) -> None:
        """Each commander's private chat gets the bot's command menu."""
        commands = [{"command": name, "description": text} for name, text in COMMANDS]
        for person in sorted(self.commanders):
            if not self._alive(generation):
                return
            answer = self.notifier._call("setMyCommands", {
                "commands": commands, "scope": {"type": "chat", "chat_id": person},
            })
            if answer.unreachable:
                return   # the outage has its own lines; the menu waits for the next start
            if not answer.ok:
                self.notifier._say(f"could not set the command menu for {person} — "
                                   f"{answer.reason}")

    def _read(self, updates: Any, started: float, first: bool, generation: int) -> None:
        if not isinstance(updates, list):
            return
        ids = [update["update_id"] for update in updates
               if isinstance(update, dict) and isinstance(update.get("update_id"), int)]
        if ids:
            self._offset = max(ids) + 1   # the next request confirms these
        with self._lock:
            for chat in _chats_in(updates):
                self._chats[chat.id] = chat
        skipped: list[tuple[_Command, dict]] = []
        for update in updates:
            message = update.get("message") if isinstance(update, dict) else None
            command = self._command_of(message)
            if command is None:
                continue
            assert isinstance(message, dict)
            date = message.get("date")
            if isinstance(date, (int, float)) and date < int(started):
                skipped.append((command, message))
                continue
            if not self._alive(generation):
                return
            try:
                self._handle(command, message)
            except Exception as exc:  # noqa: BLE001 — one command never stops the listener
                self.notifier._say(f"could not answer /{command.name} "
                                   f"({type(exc).__name__}: {exc})")
        if skipped:
            self._skipped(skipped, first)

    def _skipped(self, skipped: list[tuple[_Command, dict]], first: bool) -> None:
        command, message = skipped[-1]
        sender = message.get("sender_chat") or message.get("from") or {}
        when = _hour(float(message.get("date") or 0), self.notifier._zone)
        what = f"/{command.name} from {sender.get('id')} at {when}"
        if len(skipped) > 1:
            what = f"the last: {what}"
        before = "before this program started" if first else "while the bot was not listening"
        self.notifier._say(f"skipped {_plural(len(skipped), 'command')} sent {before} ({what})")

    def _command_of(self, message: Any) -> _Command | None:
        """The command a message holds, if it is one for this bot."""
        if not isinstance(message, dict) or not isinstance(message.get("text"), str):
            return None
        match = _COMMAND.match(message["text"].strip())
        if match is None:
            return None
        name, bot, args = match.groups()
        if bot is not None and bot.lower() != (self.username or "").lower():
            return None   # /status@OtherBot is someone else's
        args = (args or "").strip()
        sent = f"/{name.lower()} {args}" if args else f"/{name.lower()}"
        return _Command(name.lower(), args, sent)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def _handle(self, command: _Command, message: dict) -> None:
        received = self.notifier._clock.time()
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        if not isinstance(chat.get("id"), int):
            return
        sender_chat = message.get("sender_chat")
        if isinstance(sender_chat, dict):   # sent as a chat: no person to check
            anonymous = sender_chat.get("id") == chat.get("id")
            self._refuse(command, message, received, sender_chat.get("id"),
                         "sent anonymously — turn off Remain anonymous to command" if anonymous
                         else "sent on behalf of a chat — a commander writes as themselves")
            return
        sender = message.get("from")
        if not isinstance(sender, dict) or sender.get("is_bot"):
            return   # a bot never commands, and is never answered
        person = sender.get("id")
        if person not in self.commanders:
            self._refuse(command, message, received, person, "not a commander")
            return
        handler = {
            "status": self._status, "positions": self._positions, "chart": self._chart,
            "stop": self._stop, "help": self._help, "start": self._help,
        }.get(command.name)
        if handler is None:
            self._line(command, person)
            self._reply(message, received, [f"Unknown command /{command.name} — /help lists "
                                            "them."], [])
            return
        handler(command, message, person, received)

    def _refuse(self, command: _Command, message: dict, received: float, sender: Any,
                why: str) -> None:
        now = _clock.monotonic()
        line = None
        with self._lock:
            seen = self._refused.get(sender)
            if seen is None or now - seen[0] >= REFUSED_EVERY:
                more = seen[1] if seen is not None else 0
                self._refused[sender] = [now, 0]
                line = f"refused /{command.name} from {sender} — {why}"
                if more:
                    line += f" ({more} more since the last line)"
            else:
                seen[1] += 1
            answer = sender not in self._answered
            self._answered.add(sender)
        if line is not None:
            self.notifier._say(line)
        if answer:
            self._reply(message, received,
                        [f"🚫 Only this bot's commanders can use /{command.name}."], [])

    def _line(self, command: _Command, person: Any, what: str | None = None) -> None:
        self.notifier._say(f"{command.sent} from {person}" + (f" — {what}" if what else ""))

    def _help(self, command: _Command, message: dict, person: int, received: float) -> None:
        self._line(command, person)
        name = f"@{self.username}" if self.username else "the bot"
        lines = [f"🤖 {name} · commands"]
        for each, text in COMMANDS:
            shown = "/chart [SYMBOL] [TF]" if each == "chart" else f"/{each}"
            lines.append(f"{shown} — {text}")
        if self.username:
            lines.append(f"In a group, add the bot's name: /status@{self.username}")
        self._reply(message, received, lines, self.sessions())

    def _status(self, command: _Command, message: dict, person: int, received: float) -> None:
        self._line(command, person)
        sessions = self.sessions()
        prices = [self._prices(session) for session in sessions]   # here, not on the notifier's

        def answer() -> None:
            lines: list[str] = []
            for session, asked in zip(sessions, prices):
                lines.extend(self._status_of(session, asked, received))
            if not sessions:
                lines.append("📟 No trading session is running")
            armed = len(self.notifier.alerts())
            if armed:
                lines.append(f"Alerts: {armed:,} armed")
            self._reply(message, received, lines, sessions)

        self.notifier._post(answer)

    def _status_of(self, session: Any, prices: list, received: float) -> list[str]:
        pairs = session.now(self.notifier, prices)
        word = _MODES.get(session.mode, str(session.mode).title())
        symbols = ", ".join(dict.fromkeys(pair.symbol for pair in pairs))
        if session.started_at is None:
            return [f"▶️ {word} is starting · {symbols}"]
        lines = [f"📟 {word} · {symbols} · up {_span(received - session.started_at)}"]
        if len(pairs) == 1:
            pair = pairs[0]
            if pair.realized:
                lines.append(f"Today: {_plural(pair.trades, 'trade')} · won {pair.won} · lost "
                             f"{pair.lost} · net {_template.Pnl(pair.net)}")
            else:
                lines.append("Today: no trades")
            if pair.positions:
                lines.append(_join(f"Open: {len(pair.positions)}", _worth(pair)))
            else:
                lines.append("No open positions")
            return lines
        for pair in pairs:
            trades = (f"{_plural(pair.trades, 'trade')} · net {_template.Pnl(pair.net)}"
                      if pair.realized else "no trades")
            held = "no open positions"
            if pair.positions:
                worth = _worth(pair)
                held = f"{len(pair.positions)} open" + (f", {worth}" if worth else "")
            lines.append(f"{pair.symbol}: {trades} · {held}")
        return lines

    def _positions(self, command: _Command, message: dict, person: int,
                   received: float) -> None:
        self._line(command, person)
        sessions = self.sessions()
        prices = [self._prices(session) for session in sessions]

        def answer() -> None:
            if not sessions:
                self._reply(message, received, ["📟 No trading session is running"], sessions)
                return
            if all(session.started_at is None for session in sessions):
                lines = [line for session, asked in zip(sessions, prices)
                         for line in self._status_of(session, asked, received)]
                self._reply(message, received, lines, sessions)
                return
            pairs = [pair for session, asked in zip(sessions, prices)
                     for pair in session.now(self.notifier, asked)]
            held = [(pair, position) for pair in pairs for position in pair.positions]
            if not held:
                symbols = ", ".join(dict.fromkeys(pair.symbol for pair in pairs))
                self._reply(message, received, [f"📂 No open positions · {symbols}"], sessions)
                return
            lines = [f"📂 Open positions · {len(held)}"]
            for pair, position in held:
                lines.append(f"#{position.trade_id} {str(position.direction).upper()} "
                             f"{pair.symbol} {_template.Size(position.size)} @ "
                             f"{_template.Number(position.entry_price)}")
                lines.append(_join(
                    None if position.stop_loss is None
                    else f"SL {_template.Number(position.stop_loss)}",
                    None if position.next_tp is None
                    else f"Next TP {_template.Number(position.next_tp)}",
                    _now(pair),
                    _pnl(position),
                ) or "—")
            self._reply(message, received, lines, sessions)

        self.notifier._post(answer)

    def _chart(self, command: _Command, message: dict, person: int, received: float) -> None:
        from ..visual import _snapshot
        from . import _pictures

        self._line(command, person)
        sessions = self.sessions()
        markets = [(session, index, market) for session in sessions
                   for index, market in enumerate(session.markets(self.notifier))]
        if not markets:
            self._reply(message, received, ["/chart needs a trading session — its broker "
                                            "gives the candles"], sessions)
            return
        words = command.args.split()
        if len(words) > 2:
            self._reply(message, received, ["/chart takes a symbol and a timeframe: /chart "
                                            "ETHUSDT 15m"], sessions)
            return
        first = markets[0][2]
        symbol = words[0].upper() if words else first.symbol
        timeframe = words[1] if len(words) > 1 else first.timeframe
        try:
            _pictures.timeframe_ms(timeframe)
            _snapshot.check_engine()   # no Playwright, or no browser: say what to install
        except (ImportError, RuntimeError, ValueError) as exc:
            self._reply(message, received, [_one_line(exc)], sessions)
            return
        own = next(((session, index, market) for session, index, market in markets
                    if market.symbol.upper() == symbol and market.timeframe == timeframe), None)
        broker = own[2].broker if own is not None else first.broker
        price = _ticker(broker, symbol)
        if own is not None:   # the pair's own candles, with its open positions
            session, index, _market = own
            asked = [price if at == index else None
                     for at in range(len(session.markets(self.notifier)))]

            def answer() -> None:
                media = session.chart(self.notifier, symbol, timeframe)
                pair = session.now(self.notifier, asked)[index]
                head = _join(symbol, timeframe, _now(pair))
                if media is None:
                    self._reply(message, received, [head, "no candles to draw yet"], [session])
                    return
                self._reply(message, received, [head], [session], media=media)

            self.notifier._post(answer)
            return
        try:   # another symbol or timeframe: the broker's candles, fetched here (Q3.6 a)
            make = _pictures.chart_maker(self.notifier, None, None, None, broker=broker,
                                         symbol=symbol, timeframe=timeframe, indicators=None)
        except Exception as exc:  # noqa: BLE001 — a wrong symbol: the broker's own words
            self._reply(message, received, [f"/chart {symbol}: {_one_line(exc)}"], sessions)
            return
        head = _join(symbol, timeframe,
                     None if price is None else f"now {_template.Number(price)}")
        media = _Media(_Media.PHOTO, make=make, fallback=True, label="/chart",
                       filename="chart.png")
        self._reply(message, received, [head], [markets[0][0]], media=media)

    def _stop(self, command: _Command, message: dict, person: int, received: float) -> None:
        sessions = self.sessions()
        chat = message["chat"]["id"]
        if command.args.lower() == _CONFIRM:
            with self._lock:
                pending = self._pending
                confirmed = (pending is not None and pending[0] == person and pending[1] == chat
                             and received <= pending[2])
                if confirmed or (pending is not None and received > pending[2]):
                    self._pending = None
            if not confirmed or not sessions:
                self._line(command, person, "no stop is waiting")
                self._reply(message, received, ["No stop is waiting — send /stop first"],
                            sessions)
                return
            word = _word(sessions)
            self._line(command, person, f"stopping {word.lower()}")
            self._reply(message, received, [f"⏹ Stopping {word.lower()} of "
                                            f"{_symbols(self.notifier, sessions)}…"], sessions)
            for session in sessions:
                session.stop(STOP_REASON)
            return
        if not sessions:
            self._line(command, person, "nothing is trading")
            self._reply(message, received, ["📟 No trading session is running — there is "
                                            "nothing to stop"], sessions)
            return
        if all(session.mode != "live" for session in sessions):   # paper trading: at once
            self._line(command, person, "stopping paper trading")
            self._reply(message, received, [f"⏹ Stopping paper trading of "
                                            f"{_symbols(self.notifier, sessions)}…"], sessions)
            for session in sessions:
                session.stop(STOP_REASON)
            return
        with self._lock:
            self._pending = (person, chat, received + CONFIRM_SECONDS)
        self._line(command, person, "asked to confirm")

        def answer() -> None:
            lines = [f"⚠️ Stop live trading of {_symbols(self.notifier, sessions)}?"]
            for session in sessions:
                if session.mode == "live":
                    held = sum(len(pair.positions) for pair in session.now(
                        self.notifier, [None] * len(session.markets(self.notifier))))
                    lines.append(_what_stays(session.on_stop, held))
            lines.append(f"Send /stop CONFIRM within {CONFIRM_SECONDS:g} s.")
            self._reply(message, received, lines, sessions)

        self.notifier._post(answer)

    # ------------------------------------------------------------------
    # Answers
    # ------------------------------------------------------------------

    def _prices(self, session: Any) -> list[float | None]:
        """A price for each pair of *session*, from its broker, on this thread (Q5.8 b)."""
        return [_ticker(market.broker, market.symbol)
                for market in session.markets(self.notifier)]

    def _reply(self, message: dict, received: float, lines: list[str | None],
               sessions: Iterable[Any], *, media: _Media | None = None) -> None:
        """Answer *message* in its chat, as a reply to it: *lines*, the first one bold,
        and the 🕑 line."""
        notifier = self.notifier
        rich = notifier._rich
        kept = [line for line in lines if line]
        sources = list(dict.fromkeys(_SOURCES.get(session.mode, str(session.mode).upper())
                                     for session in sessions))
        kept.append(_join(f"🕑 {_template.Moment(received, notifier._zone)}", *sources))
        if rich:
            kept = [html.escape(line, quote=False) for line in kept]
            kept[0] = f"<b>{kept[0]}</b>"
        chat = message["chat"]
        thread = message.get("message_thread_id") if message.get("is_topic_message") else None
        dest = notifier._answer_destination(chat, thread)
        message_id = message.get("message_id")
        notifier._queue_event(
            dest, _ANSWER, None, "\n".join(kept), queued_at=received, silent=False,
            reply=None if message_id is None else (lambda: message_id), media=media,
        )


def _hour(epoch: float, zone: Any) -> str:
    """«06:12 UTC», or in a notifier's zone «09:42 (UTC+03:30)»."""
    moment = datetime.fromtimestamp(epoch, tz=zone or timezone.utc)
    label = "UTC" if zone is None else f"({_template.offset_label(moment)})"
    return f"{moment:%H:%M} {label}"


def _join(*parts: str | None) -> str:
    return " · ".join(part for part in parts if part)


def _word(sessions: list[Any]) -> str:
    return "Live trading" if any(session.mode == "live" for session in sessions) else (
        "Paper trading")


def _symbols(notifier: Notifier, sessions: list[Any]) -> str:
    return ", ".join(dict.fromkeys(
        market.symbol for session in sessions for market in session.markets(notifier)))


def _now(pair: Any) -> str | None:
    """«now 64,700», or the runner's own price and what it is: «now 64,000 (14:00 close)»."""
    if pair.price is None:
        return None
    note = f" ({pair.price_note})" if pair.price_note else ""
    return f"now {_template.Number(pair.price)}{note}"


def _worth(pair: Any) -> str | None:
    """The open positions' P&L together: «+$100.00 now», «-$55.56 (14:00 close)»."""
    values = [position.pnl for position in pair.positions]
    if not values or any(value is None for value in values):
        return None
    note = f"({pair.price_note})" if pair.price_note else "now"
    return f"{_template.Pnl(sum(values))} {note}"


def _pnl(position: Any) -> str | None:
    if position.pnl is None:
        return None
    if position.risk:
        return (f"{_template.Pnl(position.pnl)} "
                f"({_template.SignedRatio(position.pnl / position.risk)} R)")
    return str(_template.Pnl(position.pnl))


def _what_stays(on_stop: Any, held: int) -> str:
    if on_stop == _ON_STOP_CLOSE_ALL:
        return (f"Every open position is closed at the market and its orders cancelled "
                f'(on_stop="close_all"): {held:,} now.')
    if held == 0:
        return f'No position is open (on_stop="{on_stop}").'
    if held == 1:
        return f'1 position stays open with its venue SL/TP (on_stop="{on_stop}").'
    return f'{held:,} positions stay open with their venue SL/TP (on_stop="{on_stop}").'
