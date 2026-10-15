"""
Tests for v1.2.0 — ``AlgoTradeKit.notify``: text to Telegram.

Everything runs against a fake Bot API: a FastAPI app on uvicorn, bound to
127.0.0.1, that records every request and answers like Telegram — ok, 400, 401,
403, 429 with ``retry_after``, 5xx, or never.  The real Telegram is never touched.

Where time matters, a fake clock stands in for the notifier's own (``_clock``).
It moves only when a test advances it, and :func:`settle` waits until the sender
thread has looked at the current time and found nothing more to do — so a test
can say exactly when, in virtual seconds, each request went out.
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from AlgoTradeKit.broker import Ticker
from AlgoTradeKit.notify import (
    KIND_TEXT,
    NotifyError,
    TelegramChat,
    TelegramChatInfo,
    TelegramError,
    TelegramNotifier,
    _core,
    _telegram,
    _template,
)

ROOT = Path(__file__).resolve().parent.parent
NOTIFY = ROOT / "src" / "AlgoTradeKit" / "notify"

TOKEN = "123456:TEST-secret_token"
SECRET = "TEST-secret_token"
ME = 123456789                 # a person who pressed Start
CHANNEL = -1001234567890       # a private channel: no @name, its id is its address
GROUP = -1009876543210         # a supergroup with forum topics
PUBLIC = "@atk_signals"        # a public channel
START = datetime(2026, 10, 14, 14, 3, 27, tzinfo=timezone.utc)
CANNOT_START = "if it is a person, they must open t.me/atk_test_bot and press Start"
FIND_CHATS = "TelegramNotifier.find_chats(token) lists the chats the bot can see"


# ---------------------------------------------------------------------------
# The fake Telegram
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _absolute_form(app):
    """A request sent through an HTTP proxy names the whole URL; route it by its path."""
    async def wrapped(scope, receive, send):
        if scope["type"] == "http" and not scope["path"].startswith("/"):
            path = urlsplit(scope["path"]).path or "/"
            scope = {**scope, "path": path, "raw_path": path.encode()}
        await app(scope, receive, send)
    return wrapped


def _chats() -> dict:
    return {
        ME: {"id": ME, "type": "private", "first_name": "Ada", "username": "ada"},
        CHANNEL: {"id": CHANNEL, "type": "channel", "title": "ATK Signals"},
        PUBLIC: {"id": -1005555555555, "type": "channel", "title": "ATK Public",
                 "username": "atk_signals"},
        GROUP: {"id": GROUP, "type": "supergroup", "title": "ATK Group", "is_forum": True},
    }


class FakeTelegram:
    """The Bot API, faked over real HTTP.  ``when()`` programs an answer; ``calls``
    is every request, ``answered`` every message it accepted."""

    BOT = {"id": 123456, "is_bot": True, "first_name": "ATK test", "username": "atk_test_bot"}

    def __init__(self) -> None:
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self._lock = threading.Lock()
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self.app = _absolute_form(self._build())
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.calls: list[dict] = []
            self.answered: list[dict] = []
            self.rules: list[dict] = []
            self.updates: list[dict] = []
            self.chats = _chats()
            self.members: dict = {}
            self.next_id = 1
            self.clock: FakeClock | None = None

    def when(self, method, *, chat=None, status=200, description="", retry_after=None,
             times=None, delay=0.0, never=False, text=None) -> None:
        """The next requests of *method* (to *chat*) get this answer, *times* times."""
        with self._lock:
            self.rules.append(dict(method=method, chat=chat, status=status,
                                   description=description, retry_after=retry_after,
                                   times=times, delay=delay, never=never, text=text))

    def clear_rules(self) -> None:
        with self._lock:
            self.rules = []

    @property
    def texts(self) -> list[str]:
        with self._lock:
            return [item["text"] for item in self.answered]

    def tries(self, method: str = "sendMessage") -> list[dict]:
        with self._lock:
            return [call for call in self.calls if call["method"] == method]

    # ---- serving ----

    def _now(self) -> float:
        return self.clock.elapsed() if self.clock is not None else time.monotonic()

    def _build(self) -> FastAPI:
        app = FastAPI()

        @app.api_route("/bot{token}/{method}", methods=["GET", "POST"])
        async def bot_api(token: str, method: str, request: Request):
            raw = await request.body()
            try:
                params = json.loads(raw) if raw else {}
            except ValueError:
                params = {}
            params.update(request.query_params)
            with self._lock:
                self.calls.append({"method": method, "params": params, "at": self._now(),
                                   "host": request.headers.get("host", "")})
                rule = self._rule(method, params)
            if rule is not None and rule["delay"]:
                await asyncio.sleep(rule["delay"])
            if rule is not None and rule["never"]:
                return JSONResponse({"ok": False}, status_code=504)
            with self._lock:
                status, body = self._answer(token, method, params, rule)
            if isinstance(body, str):
                return PlainTextResponse(body, status_code=status)
            return JSONResponse(body, status_code=status)

        return app

    def _rule(self, method: str, params: dict) -> dict | None:
        for rule in self.rules:
            if rule["method"] != method or rule["times"] == 0:
                continue
            if rule["chat"] is not None and params.get("chat_id") != rule["chat"]:
                continue
            if rule["times"] is not None:
                rule["times"] -= 1
            return rule
        return None

    def _answer(self, token: str, method: str, params: dict, rule: dict | None):
        if token != TOKEN:
            return 401, {"ok": False, "error_code": 401, "description": "Unauthorized"}
        if rule is not None and rule["text"] is not None:
            return rule["status"], rule["text"]
        if rule is not None and rule["status"] != 200:
            body = {"ok": False, "error_code": rule["status"], "description": rule["description"]}
            if rule["retry_after"] is not None:
                body["parameters"] = {"retry_after": rule["retry_after"]}
            return rule["status"], body
        chat = self.chats.get(params.get("chat_id"))
        if method == "getMe":
            return 200, {"ok": True, "result": self.BOT}
        if method == "getUpdates":
            return 200, {"ok": True, "result": self.updates}
        if method not in ("getChat", "getChatMember", "sendMessage"):
            return 404, {"ok": False, "error_code": 404, "description": "Not Found"}
        if chat is None:
            return 400, {"ok": False, "error_code": 400,
                         "description": "Bad Request: chat not found"}
        if method == "getChat":
            return 200, {"ok": True, "result": chat}
        if method == "getChatMember":
            member = self.members.get(params.get("chat_id"))
            if member is None:
                member = ({"status": "administrator", "can_post_messages": True}
                          if chat["type"] == "channel" else {"status": "member"})
            if "error" in member:
                return 400, {"ok": False, "error_code": 400, "description": member["error"]}
            return 200, {"ok": True, "result": {"user": self.BOT, **member}}
        message_id = self.next_id
        self.next_id += 1
        self.answered.append({"chat_id": params.get("chat_id"), "text": params.get("text"),
                              "params": params, "at": self._now()})
        return 200, {"ok": True, "result": {"message_id": message_id, "chat": chat,
                                            "text": params.get("text")}}

    def start(self) -> None:
        ready = threading.Event()
        config = uvicorn.Config(self.app, host="127.0.0.1", port=self.port,
                                log_level="warning", lifespan="off",
                                timeout_graceful_shutdown=1)
        server = uvicorn.Server(config)
        startup = server.startup

        async def started(sockets=None):
            await startup(sockets)
            ready.set()

        server.startup = started

        def run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(server.serve())
            finally:
                loop.close()

        self._server = server
        self._thread = threading.Thread(target=run, name="fake-telegram", daemon=True)
        self._thread.start()
        assert ready.wait(10), "the fake Telegram did not start"

    def stop(self) -> None:
        """Telegram is down: the port refuses connections until start()."""
        if self._server is None:
            return
        self._server.should_exit = True
        assert self._thread is not None
        self._thread.join(10)
        self._server = self._thread = None

    @property
    def running(self) -> bool:
        return self._server is not None


# ---------------------------------------------------------------------------
# The fake clock
# ---------------------------------------------------------------------------

class FakeClock:
    """Virtual wall and monotonic time.  It moves only on advance()."""

    def __init__(self, start: datetime = START) -> None:
        self.wall = start.timestamp()
        self.mono = 10_000.0
        self._origin = self.mono

    def time(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def elapsed(self) -> float:
        return round(self.mono - self._origin, 6)

    def wait(self, condition: threading.Condition, timeout: float | None) -> None:
        if timeout is not None and timeout <= 0:
            return
        condition.wait(0.005)   # look again soon: the time may have moved

    def advance(self, seconds: float) -> None:
        self.wall += seconds
        self.mono += seconds


def settle(notifier: TelegramNotifier, timeout: float = 10.0) -> None:
    """Until the sender has seen the current state at the current (fake) time, and is
    waiting with nothing due."""
    clock = notifier._clock
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with notifier._lock:
            thread = notifier._thread
            idle = thread is None or not thread.is_alive() or (
                notifier._in_wait and not notifier._dirty and notifier._in_flight is None
                and not notifier._lines and notifier._seen == clock.monotonic()
            )
        if idle:
            return
        time.sleep(0.002)
    raise AssertionError("the sender thread did not settle")


def step(notifier: TelegramNotifier, clock: FakeClock, seconds: float) -> None:
    clock.advance(seconds)
    settle(notifier)


def run_for(notifier, clock: FakeClock, seconds: float, every: float) -> None:
    for _ in range(round(seconds / every)):
        step(notifier, clock, every)


def jobs_settle(notifier: TelegramNotifier, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with notifier._jobs_lock:
            if notifier._jobs_in_wait and notifier._jobs_seen == notifier._clock.time():
                return
        time.sleep(0.002)
    raise AssertionError("the daily-job thread did not settle")


def wait_for(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not met in time")


def lines(out: str, marker: str) -> list[str]:
    return [line for line in out.splitlines() if marker in line]


class _Broker:
    """What send_text needs of a broker: get_ticker(symbol).last."""

    def __init__(self, last: float = 64_312.5) -> None:
        self.last = last
        self.asked: list[tuple[str, str]] = []

    def get_ticker(self, symbol: str) -> Ticker:
        self.asked.append((symbol, threading.current_thread().name))
        return Ticker(symbol=symbol, last=self.last)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _no_proxies(monkeypatch):
    """Proxies from this machine's environment must not reach the fake."""
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                 "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


@pytest.fixture(scope="module")
def server():
    fake = FakeTelegram()
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture
def tg(server):
    if not server.running:
        server.start()
    server.reset()
    yield server
    if not server.running:
        server.start()


@pytest.fixture
def make(tg, monkeypatch):
    """Build notifiers against the fake; every one is closed after the test."""
    made: list[TelegramNotifier] = []

    def build(chats=None, *, clock: FakeClock | None = None, **kwargs) -> TelegramNotifier:
        kwargs.setdefault("base_url", tg.url)
        kwargs.setdefault("timeout", 2.0)
        if clock is not None:
            monkeypatch.setattr(TelegramNotifier, "_clock", clock)
            tg.clock = clock
        notifier = TelegramNotifier(TOKEN, {"me": ME} if chats is None else chats, **kwargs)
        if clock is not None:
            notifier._clock = clock
        made.append(notifier)
        return notifier

    yield build
    for notifier in made:
        notifier.close(timeout=0.5)


@pytest.fixture
def clock():
    return FakeClock()


# ===========================================================================
# The public names
# ===========================================================================

class TestPublicNames:
    def test_exports(self):
        import AlgoTradeKit.notify as notify

        assert set(notify.__all__) == {"TelegramNotifier", "TelegramChat", "TelegramChatInfo",
                                       "TelegramError", "NotifyError", "KIND_TEXT"}
        assert KIND_TEXT == "text"
        assert issubclass(TelegramError, NotifyError)


# ===========================================================================
# Chats, names and filters
# ===========================================================================

class TestChats:
    def test_chat_id_forms(self):
        assert TelegramChat(ME).chat_id == ME
        assert TelegramChat("@atk_signals").chat_id == "@atk_signals"
        assert TelegramChat("-1001234567890").chat_id == CHANNEL     # what an env var holds
        assert TelegramChat(" 123456789 ").chat_id == ME
        for wrong in ("atk_signals", "abc", "", "@"):
            with pytest.raises(ValueError, match="chat_id must be"):
                TelegramChat(wrong)
        with pytest.raises(TypeError):
            TelegramChat(True)

    def test_settings_are_checked(self):
        chat = TelegramChat(GROUP, thread_id="42", silent=True, symbols={"btcusdt"},
                            events=["signal"])
        assert chat.thread_id == 42
        assert chat.symbols == frozenset({"BTCUSDT"})
        assert chat.events == frozenset({"signal"})
        with pytest.raises(TypeError, match="not the string"):
            TelegramChat(ME, events="close")           # would be its letters
        with pytest.raises(TypeError, match="not the string"):
            TelegramChat(ME, symbols="BTCUSDT")
        with pytest.raises(ValueError, match="thread_id"):
            TelegramChat(GROUP, thread_id=0)
        with pytest.raises(TypeError, match="silent"):
            TelegramChat(ME, silent="yes")

    def test_a_dict_one_chat_or_a_list(self, make):
        assert list(make({"me": ME, "signals": PUBLIC})._destinations) == ["me", "signals"]
        assert list(make(ME)._destinations) == [str(ME)]
        assert list(make([ME, PUBLIC])._destinations) == [str(ME), PUBLIC]
        assert list(make("-1001234567890")._destinations) == [str(CHANNEL)]

    def test_empty_and_doubled_chats_raise(self, make):
        with pytest.raises(ValueError, match="chats is empty"):
            make({})
        with pytest.raises(ValueError, match="chats is empty"):
            make([])
        with pytest.raises(ValueError, match="given twice"):
            make([ME, str(ME)])

    def test_unknown_to_names_the_known_chats(self, make):
        bot = make({"me": ME, "signals": PUBLIC})
        with pytest.raises(ValueError, match=r"unknown chat 'sigals' — known: me, signals"):
            bot.send_text("hi", to="sigals")
        with pytest.raises(ValueError, match="unknown chat"):
            bot.send_text("hi", to=ME)        # a dict names its chats by their keys

    def test_to_takes_the_id_of_a_chat_given_on_its_own(self, make, tg):
        bot = make([ME, PUBLIC])
        assert set(bot.send_text("hi", to=ME)) == {str(ME)}
        assert set(bot.send_text("hi", to=PUBLIC)) == {PUBLIC}

    def test_text_to_a_person_a_channel_and_a_forum_topic(self, make, tg):
        bot = make({"me": ME, "signals": PUBLIC,
                    "topic": TelegramChat(GROUP, thread_id=42, silent=True)})
        for name in ("me", "signals", "topic"):
            assert set(bot.send_text(f"to {name}", to=name)) == {name}
        sent = {item["text"]: item["params"] for item in tg.answered}
        assert sent["to me"]["chat_id"] == ME
        assert sent["to signals"]["chat_id"] == PUBLIC
        topic = sent["to topic"]
        assert (topic["chat_id"], topic["message_thread_id"]) == (GROUP, 42)
        assert topic["disable_notification"] is True
        assert "disable_notification" not in sent["to me"]
        assert "message_thread_id" not in sent["to me"]
        for params in sent.values():
            assert params["parse_mode"] == "HTML"
            assert params["link_preview_options"] == {"is_disabled": True}

    def test_silent_overrides_the_chats_own_setting(self, make, tg):
        bot = make({"me": ME, "quiet": TelegramChat(CHANNEL, silent=True)})
        bot.send_text("a")                       # each chat's own setting
        bot.send_text("b", silent=False, to="quiet")
        bot.send_text("c", silent=True, to="me")
        got = {(item["chat_id"], item["text"]): item["params"].get("disable_notification")
               for item in tg.answered}
        assert got == {(ME, "a"): None, (CHANNEL, "a"): True,
                       (CHANNEL, "b"): None, (ME, "c"): True}


class TestFilters:
    def test_q14_a_channel_gets_only_its_kinds(self, make, tg):
        bot = make({"me": ME,
                    "signals": TelegramChat(PUBLIC, events={"signal", "tp_level", "close"})})
        assert set(bot.send_text("hi")) == {"me"}
        assert set(bot.send_text("hi", to="signals")) == {"signals"}
        assert [item["chat_id"] for item in tg.answered] == [ME, PUBLIC]

    def test_q16a_a_symbol_filter_keeps_out_other_symbols_only(self, make):
        bot = make({"me": ME, "btc": TelegramChat(CHANNEL, symbols={"BTCUSDT"})})
        assert set(bot.send_text("Good morning", wait=False)) == {"me", "btc"}
        assert set(bot.send_text("BTC above 64k", symbol="BTCUSDT", wait=False)) == {"me", "btc"}
        assert set(bot.send_text("ETH above 2,500", symbol="ETHUSDT", wait=False)) == {"me"}
        assert set(bot.send_text("btc, in lower case", symbol="btcusdt", wait=False)) == {
            "me", "btc"}

    def test_to_overrides_both_filters(self, make):
        bot = make({"me": ME,
                    "btc": TelegramChat(CHANNEL, events=set(), symbols={"BTCUSDT"})})
        assert set(bot.send_text("hi", wait=False)) == {"me"}            # an empty set: to= only
        assert set(bot.send_text("ETH", symbol="ETHUSDT", to=["btc"], wait=False)) == {"btc"}
        assert bot.send_text("nobody", to=[], wait=False) == {}

    def test_the_default_set_is_every_kind_but_sl_move(self, make):
        dest = make({"me": ME})._destinations["me"]
        assert dest.accepts(KIND_TEXT, None)
        assert dest.accepts("signal", "BTCUSDT")
        assert not dest.accepts("sl_move", "BTCUSDT")

    def test_no_chat_takes_it(self, make):
        bot = make({"signals": TelegramChat(PUBLIC, events={"close"})})
        assert bot.send_text("hi") == {}
        assert bot.send_text("hi", wait=False) == {}


# ===========================================================================
# Placeholders and formatting
# ===========================================================================

class TestTemplates:
    TEXT = "{time} · {symbol} {price} — watching the 64k level"

    def test_q11_the_worked_example(self, make, tg, clock):
        bot = make(clock=clock)
        bot.send_text(self.TEXT, symbol="BTCUSDT", broker=_Broker())
        assert tg.texts == ["2026-10-14 14:03 UTC · BTCUSDT 64,312.5 — watching the 64k level"]

    def test_q11_the_worked_example_in_tehran(self, make, tg, clock):
        bot = make(clock=clock, timezone="Asia/Tehran")
        bot.send_text(self.TEXT, symbol="BTCUSDT", broker=_Broker())
        assert tg.texts == [
            "2026-10-14 17:33 (UTC+03:30) · BTCUSDT 64,312.5 — watching the 64k level"]

    def test_the_offset_is_the_one_of_that_moment(self):
        london = _template.parse_zone("Europe/London")
        january = datetime(2026, 1, 14, 14, 3, tzinfo=timezone.utc).timestamp()
        october = datetime(2026, 10, 14, 14, 3, tzinfo=timezone.utc).timestamp()
        assert format(_template.Moment(january, london)) == "2026-01-14 14:03 (UTC+00:00)"
        assert format(_template.Moment(october, london)) == "2026-10-14 15:03 (UTC+01:00)"
        assert _template.parse_zone("UTC") is None and _template.parse_zone(None) is None

    def test_an_unknown_zone_raises_when_the_notifier_is_created(self, make):
        with pytest.raises(ValueError, match="unknown time zone 'Asia/Tehrn'"):
            make(timezone="Asia/Tehrn")

    def test_header_date_and_specs(self, make, tg, clock):
        bot = make(clock=clock, timezone="Asia/Tehran")
        bot.send_text("{header}\n{date} {date:%d %b} {time:%H:%M} {price:.1f} {price:,.2f}",
                      symbol="BTCUSDT", broker=_Broker())
        assert tg.texts == ["🕑 2026-10-14 17:33 (UTC+03:30) · BTCUSDT 64,312.5\n"
                            "2026-10-14 14 Oct 17:33 64312.5 64,312.50"]

    def test_numbers_print_for_a_person(self):
        human = _template.human_number
        assert [human(v) for v in (64312.5, 1.08345, 64312.0, 0.00001234, 1e-10, -1234.5)] == [
            "64,312.5", "1.08345", "64,312", "0.00001234", "0", "-1,234.5"]
        assert format(_template.Number(64312.5), ".1f") == "64312.5"
        assert str(_template.Number(0.1 + 0.2)) == "0.3"

    def test_a_text_without_placeholders_goes_as_written(self, make, tg):
        make().send_text("Watching the 64k level {{literally}}")
        assert tg.texts == ["Watching the 64k level {literally}"]

    @pytest.mark.parametrize(("text", "message"), [
        ("{prise}", "unknown field {prise} — known: time, date, symbol, price, header"),
        ("{price}", "{price} needs broker= and symbol="),
        ("{header}", "{header} needs broker= and symbol="),
        ("{symbol}", "{symbol} needs symbol="),
        ("{symbol.__class__}", "is not allowed — a template is data, not code"),
        ("{symbol[0]}", "is not allowed — a template is data, not code"),
        ("{}", "positional field {}"),
        ("{0}", "positional field {0}"),
        ("{price!r}", "conversions are not allowed"),
        ("{price:{width}}", "a field inside a format spec is not allowed"),
        ("a { b", "write {{ or }} for a literal brace"),
        ("a } b", "write {{ or }} for a literal brace"),
    ])
    def test_mistakes_raise_at_the_call(self, make, tg, text, message):
        bot = make()
        with pytest.raises(ValueError) as caught:
            bot.send_text(text)
        assert message in str(caught.value)
        assert tg.tries() == []

    def test_price_needs_a_symbol_too(self, make):
        with pytest.raises(ValueError, match=r"\{price\} needs broker= and symbol="):
            make().send_text("{price}", broker=_Broker())

    def test_the_price_is_asked_at_the_call_on_the_callers_thread(self, make, tg):
        broker = _Broker()
        make().send_text("{price}", symbol="BTCUSDT", broker=broker, wait=False)
        assert broker.asked == [("BTCUSDT", threading.current_thread().name)]

    def test_a_broker_error_propagates_as_the_broker_raised_it(self, make, tg):
        class Down(_Broker):
            def get_ticker(self, symbol):
                raise ConnectionError("venue down")

        with pytest.raises(ConnectionError, match="venue down"):
            make().send_text("{price}", symbol="BTCUSDT", broker=Down())
        assert tg.tries() == []

    def test_values_are_escaped_and_the_texts_own_tags_kept(self, make, tg):
        make().send_text("<b>{symbol}</b> {price}", symbol="A<B&C", broker=_Broker(1.5))
        assert tg.texts == ["<b>A&lt;B&amp;C</b> 1.5"]

    def test_a_venues_message_with_brackets_is_escaped(self):
        fields = _template.fields_of("⚠️ {message}", ["message"])
        text = _template.render("⚠️ {message}", {"message": "create_order: <code -2019> & co"},
                                escape=True)
        assert fields == ["message"]
        assert text == "⚠️ create_order: &lt;code -2019&gt; &amp; co"

    def test_plain_text_mode_escapes_nothing(self, make, tg):
        make(parse_mode=None).send_text("<b>{symbol}</b>", symbol="A<B&C")
        assert tg.texts == ["<b>A<B&C</b>"]
        assert "parse_mode" not in tg.answered[0]["params"]

    def test_an_empty_text_raises(self, make):
        for text in ("", "   ", "<b> </b>"):
            with pytest.raises(ValueError, match="the text is empty"):
                make().send_text(text)

    def test_persian_text(self, make, tg):
        make().send_text("سلام — قیمت {symbol}", symbol="BTCUSDT")
        assert tg.texts == ["سلام — قیمت BTCUSDT"]


# ===========================================================================
# Long text
# ===========================================================================

class TestSplitting:
    measure = staticmethod(TelegramNotifier._measure)

    def split(self, text, rich=True, limit=4096):
        return _core.split_text(text, limit, rich=rich, measure=self.measure)

    def test_5000_characters_arrive_as_two_messages_split_at_a_line_break(self, make, tg):
        lines_ = [f"line {i:03d} " + "x" * 70 for i in range(64)]
        text = "\n".join(lines_)
        assert len(text) > 5_000
        ids = make().send_text(text)
        first, second = tg.texts
        assert ids == {"me": 1}
        assert first.endswith(lines_[len(first.splitlines()) - 1])
        assert first + "\n" + second == text
        assert len(first) <= 4096 and len(second) <= 4096

    def test_a_line_longer_than_the_limit_is_cut_at_it(self):
        parts = self.split("y" * 5_000)
        assert [len(p) for p in parts] == [4096, 904]

    def test_length_is_counted_in_utf16_units(self):
        assert self.measure("😀") == 2 and self.measure("a😀ب") == 4
        parts = self.split("😀" * 3_000)
        assert [self.measure(p) for p in parts] == [4096, 1904]
        assert "".join(parts) == "😀" * 3_000

    def test_open_tags_are_closed_and_opened_again(self):
        parts = self.split("<b>" + "y" * 5_000 + "</b>")
        assert [p[:4] for p in parts] == ["<b>y", "<b>y"]
        assert [p[-5:] for p in parts] == ["y</b>", "y</b>"]
        assert all(self.measure(p) <= 4096 for p in parts)
        nested = self.split('<a href="https://x.y"><i>' + "z\n" * 3_000 + "</i></a>")
        assert nested[1].startswith('<a href="https://x.y"><i>')
        assert nested[0].endswith("</i></a>")

    def test_a_cut_never_lands_inside_a_tag_or_an_entity(self):
        text = "&amp;" * 2_000
        for part in self.split(text, limit=4_098):
            assert part == "&amp;" * (len(part) // 5)

    def test_plain_text_has_no_tags(self):
        parts = self.split("<b>" + "y" * 5_000, rich=False)
        assert parts[0].startswith("<b>") and not parts[0].endswith("</b>")
        assert "".join(parts) == "<b>" + "y" * 5_000


# ===========================================================================
# The queue: pacing, retries, failures, outages, late marks
# ===========================================================================

class TestDelivery:
    def test_wait_true_returns_the_message_ids(self, make, tg):
        bot = make({"me": ME, "signals": PUBLIC})
        assert bot.send_text("hi") == {"me": 1, "signals": 2}

    def test_wait_false_returns_at_once_and_delivers(self, make, tg, clock):
        bot = make({"me": ME, "signals": PUBLIC}, clock=clock)
        assert bot.send_text("hi", wait=False) == {"me": None, "signals": None}
        settle(bot)
        assert tg.texts == ["hi", "hi"]

    def test_one_message_a_second_per_chat_in_order(self, make, tg, clock):
        bot = make(clock=clock)
        for i in range(3):
            bot.send_text(f"m{i}", wait=False)
        settle(bot)
        assert tg.texts == ["m0"]
        step(bot, clock, 0.5)
        assert tg.texts == ["m0"]
        step(bot, clock, 0.5)
        step(bot, clock, 1.0)
        assert tg.texts == ["m0", "m1", "m2"]
        assert [item["at"] for item in tg.answered] == [0.0, 1.0, 2.0]

    def test_other_chats_do_not_wait_for_a_busy_one(self, make, tg, clock):
        bot = make({"me": ME, "chan": CHANNEL}, clock=clock)
        bot.send_text("a", to="me", wait=False)
        bot.send_text("b", to="me", wait=False)
        bot.send_text("c", to="chan", wait=False)
        settle(bot)
        assert sorted(tg.texts) == ["a", "c"]

    def test_two_names_for_one_chat_share_its_pacing(self, make, tg, clock):
        bot = make({"me": ME, "also_me": TelegramChat(ME, silent=True)}, clock=clock)
        bot.send_text("hi", wait=False)
        settle(bot)
        assert len(tg.answered) == 1
        step(bot, clock, 1.0)
        assert len(tg.answered) == 2

    def test_twenty_a_minute_to_a_channel(self, make, tg, clock):
        bot = make({"chan": CHANNEL}, clock=clock)
        for i in range(21):
            bot.send_text(f"m{i}", wait=False)
        settle(bot)
        for _ in range(19):
            step(bot, clock, 1.0)
        assert len(tg.answered) == 20
        step(bot, clock, 40.0)
        assert len(tg.answered) == 20
        step(bot, clock, 1.0)
        assert len(tg.answered) == 21
        assert tg.answered[-1]["at"] == 60.0

    def test_429_waits_retry_after(self, make, tg, clock):
        bot = make(clock=clock)
        tg.when("sendMessage", status=429, description="Too Many Requests: retry after 7",
                retry_after=7, times=1)
        bot.send_text("hi", wait=False)
        settle(bot)
        step(bot, clock, 6)
        assert len(tg.tries()) == 1
        step(bot, clock, 1)
        assert [call["at"] for call in tg.tries()] == [0.0, 7.0]
        assert tg.texts == ["hi"]

    def test_a_server_error_is_tried_again_after_1_5_and_15_seconds(
            self, make, tg, clock, capsys):
        bot = make(clock=clock)
        tg.when("sendMessage", status=502, description="Bad Gateway", times=3)
        bot.send_text("hi", wait=False)
        settle(bot)
        for seconds in (1, 5, 14):
            step(bot, clock, seconds)
        assert [call["at"] for call in tg.tries()] == [0.0, 1.0, 6.0]
        step(bot, clock, 1)
        assert [call["at"] for call in tg.tries()] == [0.0, 1.0, 6.0, 21.0]
        assert tg.texts == ["hi"]
        out = capsys.readouterr().out
        assert lines(out, "could not send") == [
            "[AlgoTradeKit] telegram: could not send to me (Ada, 123456789) — "
            "502 Bad Gateway (1 message waiting)"]

    def test_a_server_error_holds_its_own_chat_only(self, make, tg, clock):
        bot = make({"me": ME, "chan": CHANNEL}, clock=clock)
        tg.when("sendMessage", chat=ME, status=500, description="Internal Server Error")
        bot.send_text("a", to="me", wait=False)
        bot.send_text("b", to="chan", wait=False)
        settle(bot)
        step(bot, clock, 1.0)
        bot.send_text("c", to="chan", wait=False)
        step(bot, clock, 1.0)
        assert tg.texts == ["b", "c"]

    def test_403_is_not_tried_again(self, make, tg, clock, capsys):
        bot = make(clock=clock)
        tg.when("sendMessage", status=403, description="Forbidden: bot was blocked by the user")
        bot.send_text("hi", wait=False)
        settle(bot)
        run_for(bot, clock, 60, every=15)
        assert len(tg.tries()) == 1
        assert lines(capsys.readouterr().out, "could not send") == [
            "[AlgoTradeKit] telegram: could not send to me (Ada, 123456789) — "
            "403 Forbidden: bot was blocked by the user (0 messages waiting)"]

    def test_the_failure_line_comes_once_a_minute_per_chat(self, make, tg, clock, capsys):
        bot = make({"me": ME, "signals": PUBLIC}, clock=clock)
        tg.when("sendMessage", chat=PUBLIC, status=403,
                description="Forbidden: bot is not a member of the channel chat")
        for text in ("a", "b", "c", "d"):
            bot.send_text(text, to="signals", wait=False)
        settle(bot)
        for _ in range(3):
            step(bot, clock, 1.0)
        out = capsys.readouterr().out
        assert lines(out, "could not send") == [
            "[AlgoTradeKit] telegram: could not send to signals (@atk_signals) — 403 Forbidden:"
            " bot is not a member of the channel chat (3 messages waiting)"]
        step(bot, clock, 57.0)
        bot.send_text("e", to="signals", wait=False)
        settle(bot)
        assert len(lines(capsys.readouterr().out, "could not send")) == 1

    def test_wait_true_raises_with_every_chats_outcome(self, make, tg):
        bot = make({"me": ME, "signals": PUBLIC})
        tg.when("sendMessage", chat=PUBLIC, status=403,
                description="Forbidden: bot is not a member of the channel chat")
        with pytest.raises(TelegramError) as caught:
            bot.send_text("hi")
        error = caught.value
        assert str(error) == ("could not send to signals (@atk_signals) — 403 Forbidden: bot is "
                              "not a member of the channel chat; sent to me")
        assert error.sent == {"me": 1}
        assert list(error.failed) == ["signals"]

    def test_a_parse_error_says_how_to_write_a_bracket(self, make, tg):
        tg.when("sendMessage", status=400, description="Bad Request: can't parse entities: "
                "Unsupported start tag \"64k\" at byte offset 8")
        with pytest.raises(TelegramError, match="write &lt; &gt; &amp; for a literal"):
            make().send_text("price <64k>")

    def test_the_queue_holds_1000_and_says_what_it_dropped(self, make, tg, clock, capsys):
        bot = make(clock=clock)
        tg.stop()
        release = threading.Event()
        deliver = bot._deliver

        def held(dest, text, *, silent):              # the sender is busy until released
            release.wait(30)
            return deliver(dest, text, silent=silent)

        bot._deliver = held
        bot.send_text("m0", wait=False)
        wait_for(lambda: bot._in_flight is not None)
        for i in range(1, 1_005):
            bot.send_text(f"m{i}", wait=False)
        release.set()
        settle(bot)
        assert bot._waiting == 1_000
        out = capsys.readouterr().out
        assert lines(out, "queue full") == [
            "[AlgoTradeKit] telegram: queue full — dropped 5 oldest messages"]
        for i in range(3):
            bot.send_text(f"more{i}", wait=False)
        settle(bot)
        assert lines(capsys.readouterr().out, "queue full") == []       # within the minute
        step(bot, clock, 60.0)
        assert lines(capsys.readouterr().out, "queue full") == [
            "[AlgoTradeKit] telegram: queue full — dropped 3 oldest messages"]

    def test_q24_an_outage_keeps_every_message_and_marks_the_late_ones(
            self, make, tg, capsys):
        clock = FakeClock(datetime(2026, 10, 14, 14, 2, tzinfo=timezone.utc))
        bot = make(clock=clock, check=False)
        tg.stop()                                          # 14:02, the proxy goes down
        step(bot, clock, 60)
        bot.send_text("SIGNAL long BTCUSDT @ 64,250", wait=False)        # 14:03
        bot.send_text("OPEN #12 long BTCUSDT", wait=False)
        run_for(bot, clock, 8 * 60, every=5)
        bot.send_text("CLOSE #12 stop loss −$100.00", wait=False)        # 14:11
        run_for(bot, clock, 3 * 60, every=5)
        assert tg.texts == []                              # 14:14
        tg.start()
        run_for(bot, clock, 30, every=1)
        assert tg.texts == ["⏱ 11 min late\nSIGNAL long BTCUSDT @ 64,250",
                            "⏱ 11 min late\nOPEN #12 long BTCUSDT",
                            "⏱ 3 min late\nCLOSE #12 stop loss −$100.00"]
        out = capsys.readouterr().out
        assert lines(out, "reachable again") == [
            "[AlgoTradeKit] telegram: Telegram reachable again — sending 3 waiting messages"]
        down = lines(out, "unreachable")
        assert down[0].startswith(
            "[AlgoTradeKit] telegram: Telegram unreachable (connection refused) — ")
        assert 1 < len(down) <= 12                         # at most one a minute, 14:03–14:14

    def test_a_network_error_is_tried_after_1_5_15_then_every_15_seconds(
            self, make, tg, clock, capsys):
        bot = make(clock=clock, timeout=1.0)
        tg.when("sendMessage", never=True, delay=3.0)      # never answers in time
        bot.send_text("hi", wait=False)
        settle(bot)
        for tries, seconds in enumerate((1, 5, 15, 15), start=2):
            wait_for(lambda: len(tg.tries()) == tries - 1)
            step(bot, clock, seconds)
        wait_for(lambda: len(tg.tries()) == 5)
        assert [call["at"] for call in tg.tries()] == [0.0, 1.0, 6.0, 21.0, 36.0]
        assert lines(capsys.readouterr().out, "unreachable") == [
            "[AlgoTradeKit] telegram: Telegram unreachable (no answer in 1 s) — "
            "1 message waiting"]
        tg.clear_rules()
        step(bot, clock, 15)
        assert tg.texts == ["hi"]                          # 51 s: not yet a minute late

    def test_while_unreachable_only_the_oldest_message_is_tried(self, make, tg, clock):
        bot = make({"me": ME, "chan": CHANNEL}, clock=clock, timeout=1.0)
        tg.when("sendMessage", never=True, delay=3.0)
        bot.send_text("a", to="me", wait=False)
        bot.send_text("b", to="chan", wait=False)
        settle(bot)
        for tries, seconds in enumerate((1, 5, 15), start=2):
            wait_for(lambda: len(tg.tries()) == tries - 1)
            step(bot, clock, seconds)
        wait_for(lambda: len(tg.tries()) == 4)
        assert {call["params"]["chat_id"] for call in tg.tries()} == {ME}
        tg.clear_rules()
        step(bot, clock, 15)
        assert sorted(tg.texts) == ["a", "b"]

    def test_q15b_a_waiting_call_gives_up_after_21_seconds_and_its_message_is_withdrawn(
            self, make, tg, clock):
        bot = make(clock=clock, check=False)
        tg.stop()
        caught: list[BaseException] = []
        call = threading.Thread(target=lambda: _record(caught, bot.send_text, "Still watching"))
        call.start()
        wait_for(lambda: bot._waiting == 1)
        settle(bot)
        for seconds in (1, 5, 14):
            step(bot, clock, seconds)
        assert call.is_alive()                             # 20 s
        step(bot, clock, 1)                                # the try at 21 s fails
        call.join(5)
        assert not call.is_alive()
        assert isinstance(caught[0], TelegramError)
        assert str(caught[0]) == ("could not send to me (123456789) — Telegram unreachable "
                                  "(connection refused); not sent")
        assert caught[0].failed and caught[0].sent == {}
        tg.start()
        run_for(bot, clock, 30, every=5)
        assert tg.texts == []                              # never sent behind the caller's back

    def test_q15b_inside_a_running_outage_the_call_hears_at_the_next_try(
            self, make, tg, clock):
        bot = make(clock=clock, check=False)
        tg.stop()
        bot.send_text("queued earlier", wait=False)        # tried at 0, 1, 6, 21, 36…
        settle(bot)
        for seconds in (1, 5):
            step(bot, clock, seconds)
        step(bot, clock, 1)                                # 7 s: a call that waits
        caught: list[BaseException] = []
        call = threading.Thread(target=lambda: _record(caught, bot.send_text, "now"))
        call.start()
        wait_for(lambda: bot._waiting == 2)
        step(bot, clock, 14)                               # 21 s: the call is 14 s old
        step(bot, clock, 14)
        assert call.is_alive()                             # 35 s
        step(bot, clock, 1)                                # 36 s: 29 s after the call
        call.join(5)
        assert isinstance(caught[0], TelegramError)
        tg.start()
        run_for(bot, clock, 20, every=5)
        assert tg.texts == ["queued earlier"]

    def test_the_late_marker_goes_on_the_first_part_only(self, make, tg, clock):
        bot = make(clock=clock, check=False)
        tg.stop()
        text = "\n".join("x" * 79 for _ in range(64))
        bot.send_text(text, wait=False)
        settle(bot)
        run_for(bot, clock, 90, every=15)
        tg.start()
        run_for(bot, clock, 20, every=1)
        first, second = tg.texts
        assert first.startswith("⏱ 1 min late\nxxx")
        assert not second.startswith("⏱")
        assert first[len("⏱ 1 min late\n"):] + "\n" + second == text
        assert all(TelegramNotifier._measure(t) <= 4096 for t in (first, second))


def _record(into: list, function, *args, **kwargs) -> None:
    try:
        into.append(function(*args, **kwargs))
    except BaseException as exc:  # noqa: BLE001 — the test reads it
        into.append(exc)


# ===========================================================================
# Shutdown
# ===========================================================================

class TestShutdown:
    def test_close_delivers_what_is_queued(self, make, tg):
        bot = make()
        bot.send_text("a", wait=False)
        bot.send_text("b", wait=False)
        bot.close(timeout=5)
        assert tg.texts == ["a", "b"]

    def test_close_gives_up_when_time_runs_out(self, make, tg, clock, capsys):
        bot = make(clock=clock)
        for text in ("a", "b", "c"):
            bot.send_text(text, wait=False)
        settle(bot)
        bot.close(timeout=0.3)                       # the fake clock never reaches 1 s
        assert tg.texts == ["a"]
        assert lines(capsys.readouterr().out, "closed with") == [
            "[AlgoTradeKit] telegram: closed with 2 messages unsent"]

    def test_close_cuts_off_a_call_that_waits(self, make, tg, clock):
        bot = make(clock=clock)
        bot.send_text("a", wait=False)
        caught: list[BaseException] = []
        call = threading.Thread(target=lambda: _record(caught, bot.send_text, "b"))
        call.start()
        wait_for(lambda: bot._waiting == 1 and len(tg.answered) == 1)
        bot.close(timeout=0.2)
        call.join(5)
        assert str(caught[0]) == ("could not send to me (Ada, 123456789) — notifier closed "
                                  "before the message was sent")

    def test_a_closed_notifier_can_be_used_again(self, make, tg):
        bot = make()
        bot.send_text("before")
        bot.close()
        assert bot._thread is None
        assert bot.send_text("after") == {"me": 2}
        assert tg.texts == ["before", "after"]

    def test_a_notifier_is_a_context_manager(self, make, tg):
        with make() as bot:
            bot.send_text("inside", wait=False)
        assert tg.texts == ["inside"]
        assert bot._thread is None

    @pytest.mark.parametrize("keep_alive", ["before the first send", "after the first send"])
    def test_the_exit_flush_runs_before_the_charts_keep_alive_blocks(self, tg, keep_alive):
        """The keep-alive is an atexit handler that blocks until Ctrl+C.  Registered after
        the notifier's own hook, a plain atexit flush would run only after it."""
        tg.when("sendMessage", delay=1.0)              # a slow Telegram
        script = (
            "import sys\n"
            "from AlgoTradeKit.notify import TelegramNotifier\n"
            "from AlgoTradeKit.simulate._engine import _register_keep_alive\n"
            "url, order, token, me = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])\n"
            "bot = TelegramNotifier(token, {'me': me}, base_url=url, check=False)\n"
            "if order == 'before the first send':\n"
            "    _register_keep_alive()\n"
            "bot.send_text('sent at exit', wait=False)\n"
            "if order == 'after the first send':\n"
            "    _register_keep_alive()\n"
            "print('script done', flush=True)\n"
        )
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONUNBUFFERED": "1"}
        process = subprocess.Popen(
            [sys.executable, "-c", script, tg.url, keep_alive, TOKEN, str(ME)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
        )
        seen: queue.Queue = queue.Queue()
        threading.Thread(target=lambda: [seen.put(line) for line in process.stdout],
                         daemon=True).start()
        try:
            output = []
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                try:
                    line = seen.get(timeout=1)
                except queue.Empty:
                    continue
                output.append(line)
                if "Browser UI open" in line:
                    break
            else:
                pytest.fail("the keep-alive never started:\n" + "".join(output))
            assert tg.texts == ["sent at exit"], "".join(output)
        finally:
            process.kill()
            process.wait(10)


# ===========================================================================
# Daily jobs
# ===========================================================================

class TestDailyJobs:
    def test_a_job_runs_at_its_wall_clock_time_in_the_zone(self, make):
        clock = FakeClock(datetime(2026, 10, 14, 20, 29, tzinfo=timezone.utc))  # 23:59 Tehran
        bot = make(clock=clock, timezone="Asia/Tehran", check=False)
        runs: list[float] = []
        cancel = bot.add_daily_job(lambda: runs.append(clock.time()), at="00:00")
        jobs_settle(bot)
        clock.advance(59)
        jobs_settle(bot)
        assert runs == []
        clock.advance(1)                                    # 00:00 in Tehran
        wait_for(lambda: len(runs) == 1)
        assert datetime.fromtimestamp(runs[0], timezone.utc).strftime("%H:%M") == "20:30"
        clock.advance(3 * 86_400)                           # the machine slept three days
        wait_for(lambda: len(runs) == 2)
        jobs_settle(bot)
        assert len(runs) == 2                               # once, not once per missed day
        cancel()
        clock.advance(86_400)
        jobs_settle(bot)
        assert len(runs) == 2

    def test_a_failing_job_says_so_and_the_others_carry_on(self, make, clock, capsys):
        bot = make(clock=clock, check=False)
        runs: list[int] = []

        def boom_job():
            raise ValueError("boom")

        bot.add_daily_job(boom_job, at="14:04")
        bot.add_daily_job(lambda: runs.append(1), at="14:04")
        clock.advance(60)
        wait_for(lambda: runs == [1])
        wait_for(lambda: "daily job boom_job failed" in capsys.readouterr().out)

    def test_a_slow_job_never_delays_a_message(self, make, tg, clock):
        bot = make(clock=clock)
        release = threading.Event()
        started = threading.Event()

        def slow():
            started.set()
            release.wait(10)

        bot.add_daily_job(slow, at="14:04")
        clock.advance(60)
        assert started.wait(5)
        assert bot.send_text("while the job runs") == {"me": 1}
        release.set()

    def test_a_job_may_send(self, make, tg, clock):
        bot = make(clock=clock)
        bot.add_daily_job(lambda: bot.send_text("📊 the day", wait=False), at="14:04")
        clock.advance(60)
        wait_for(lambda: tg.texts == ["📊 the day"])

    def test_close_cancels_the_jobs(self, make, clock):
        bot = make(clock=clock, check=False)
        runs: list[int] = []
        bot.add_daily_job(lambda: runs.append(1), at="14:04")
        bot.close()
        clock.advance(60)
        time.sleep(0.05)
        assert runs == []

    @pytest.mark.parametrize("at", ["24:00", "7", "12:60", "noon", 1200])
    def test_a_wrong_time_raises(self, make, at):
        with pytest.raises(ValueError, match="time of day"):
            make(check=False).add_daily_job(lambda: None, at=at)


# ===========================================================================
# The setup check
# ===========================================================================

class TestCheck:
    def test_a_wrong_token(self, tg):
        with pytest.raises(TelegramError) as caught:
            TelegramNotifier("999:wrong-token", {"me": ME}, base_url=tg.url)
        assert str(caught.value) == ("Telegram rejected the token (401 Unauthorized) — copy it "
                                     "again from @BotFather")

    def test_q13_a_person_who_never_pressed_start(self, make, tg):
        del tg.chats[ME]
        with pytest.raises(TelegramError) as caught:
            make({"me": ME, "signals": PUBLIC})
        assert str(caught.value) == f"chat 123456789 not found — {CANNOT_START}; {FIND_CHATS}"

    def test_an_unknown_group_or_channel(self, make, tg):
        for address in (-1001111111111, "@nobody_here"):
            with pytest.raises(TelegramError) as caught:
                make({"x": address})
            assert str(caught.value) == (
                f"chat {address} not found — add the bot to the group, or make it an "
                f"administrator of the channel; {FIND_CHATS}")

    @pytest.mark.parametrize("member", [
        {"status": "member"},
        {"status": "administrator", "can_post_messages": False},
        {"status": "left"},
    ])
    def test_a_channel_where_the_bot_cannot_post(self, make, tg, member):
        tg.members[PUBLIC] = member
        with pytest.raises(TelegramError) as caught:
            make({"signals": PUBLIC})
        assert str(caught.value) == ('the bot cannot post in @atk_signals — make it an '
                                     'administrator with "Post messages"')

    def test_a_private_channel_is_named_by_its_title(self, make, tg):
        tg.members[CHANNEL] = {"error": "Bad Request: member list is inaccessible"}
        with pytest.raises(TelegramError) as caught:
            make({"signals": CHANNEL})
        assert str(caught.value) == (
            'the bot cannot post in ATK Signals (-1001234567890) — make it an administrator '
            'with "Post messages" (Telegram answered 400 Bad Request: member list is '
            'inaccessible)')

    @pytest.mark.parametrize("status", ["left", "kicked"])
    def test_a_group_the_bot_is_not_in(self, make, tg, status):
        tg.members[GROUP] = {"status": status}
        with pytest.raises(TelegramError) as caught:
            make({"group": GROUP})
        assert str(caught.value) == ("the bot is not a member of ATK Group (-1009876543210) — "
                                     "add it to the group again")

    def test_a_group_where_the_bot_is_restricted(self, make, tg):
        tg.members[GROUP] = {"status": "restricted", "is_member": True,
                             "can_send_messages": False}
        with pytest.raises(TelegramError, match="it is restricted there"):
            make({"group": GROUP})

    def test_q13_telegram_unreachable_one_line_and_the_text_goes_out_later(
            self, make, tg, capsys):
        tg.stop()
        bot = make({"me": ME, "signals": PUBLIC})
        out = capsys.readouterr().out
        assert out.splitlines() == [
            "[AlgoTradeKit] telegram: Telegram unreachable (connection refused) — carrying on;"
            " messages wait in the queue"]
        bot.send_text("queued while Telegram was down", to="me", wait=False)
        tg.start()
        out = ""

        def said_reachable() -> bool:
            nonlocal out
            out += capsys.readouterr().out
            return "reachable again" in out

        wait_for(said_reachable)
        assert tg.texts == ["queued while Telegram was down"]
        assert lines(out, "unreachable") == []
        assert lines(out, "reachable again") == [
            "[AlgoTradeKit] telegram: Telegram reachable again — sending 1 waiting message"]

    @pytest.mark.parametrize(("method", "status", "description"), [
        ("getMe", 502, "Bad Gateway"),
        ("getChat", 429, "Too Many Requests: retry after 5"),
        ("getChatMember", 500, "Internal Server Error"),
    ])
    def test_a_busy_telegram_counts_as_unreachable(self, make, tg, capsys, method, status,
                                                   description):
        tg.when(method, status=status, description=description, retry_after=5)
        make({"signals": PUBLIC})
        assert capsys.readouterr().out.splitlines() == [
            f"[AlgoTradeKit] telegram: Telegram unreachable ({status} {description}) — "
            "carrying on; messages wait in the queue"]

    def test_check_false_asks_nothing(self, make, tg):
        make({"me": ME, "signals": PUBLIC}, check=False)
        assert tg.calls == []

    def test_one_question_per_chat_however_many_names(self, make, tg):
        make({"me": ME, "also_me": TelegramChat(ME, silent=True)})
        assert [call["method"] for call in tg.calls] == ["getMe", "getChat"]

    def test_a_chat_by_id_is_named_by_its_title_in_lines(self, make, tg, clock, capsys):
        bot = make({"signals": CHANNEL}, clock=clock)
        assert bot._destinations["signals"].label == "signals (ATK Signals, -1001234567890)"
        assert repr(bot) == "TelegramNotifier(@atk_test_bot, chats=['signals'])"
        tg.when("sendMessage", status=403, description="Forbidden: bot is not a member of "
                "the channel chat")
        bot.send_text("hi", wait=False)
        settle(bot)
        assert "could not send to signals (ATK Signals, -1001234567890) — 403" in \
            capsys.readouterr().out


# ===========================================================================
# find_chats
# ===========================================================================

class TestFindChats:
    def test_it_lists_people_groups_supergroups_and_channels(self, tg, capsys):
        tg.updates = [
            {"update_id": 1, "message": {"chat": {"id": ME, "type": "private",
                                                  "first_name": "Ada", "username": "ada"}}},
            {"update_id": 2, "message": {"chat": {"id": -4001, "type": "group",
                                                  "title": "Old Group"}}},
            {"update_id": 3, "message": {"chat": {"id": GROUP, "type": "supergroup",
                                                  "title": "ATK Group"}}},
            {"update_id": 4, "channel_post": {"chat": {"id": CHANNEL, "type": "channel",
                                                       "title": "ATK Signals"}}},
            {"update_id": 5, "my_chat_member": {"chat": {"id": CHANNEL, "type": "channel",
                                                         "title": "ATK Signals"}}},
        ]
        chats = TelegramNotifier.find_chats(TOKEN, base_url=tg.url)
        assert chats == [
            TelegramChatInfo(ME, "private", "Ada", "ada"),
            TelegramChatInfo(-4001, "group", "Old Group"),
            TelegramChatInfo(GROUP, "supergroup", "ATK Group"),
            TelegramChatInfo(CHANNEL, "channel", "ATK Signals"),
        ]
        out = capsys.readouterr().out
        assert "Chats that wrote to @atk_test_bot in the last 24 hours:" in out
        assert "  channel" + " " * 6 + "-1001234567890  ATK Signals\n" in out
        assert "  private" + " " * 11 + "123456789  Ada  @ada\n" in out
        updates = tg.tries("getUpdates")
        assert len(updates) == 1 and "offset" not in updates[0]["params"]   # nothing eaten

    def test_nobody_wrote(self, tg, capsys):
        assert TelegramNotifier.find_chats(TOKEN, base_url=tg.url) == []
        assert capsys.readouterr().out.strip() == (
            "Nobody has written to the bot in the last 24 hours. Send it a message, add it to "
            "the group, or post in the channel, then run this again.")

    def test_a_wrong_token(self, tg):
        with pytest.raises(TelegramError, match=r"rejected the token \(401 Unauthorized\)"):
            TelegramNotifier.find_chats("999:wrong", base_url=tg.url)

    def test_telegram_unreachable(self, tg):
        tg.stop()
        with pytest.raises(TelegramError, match=r"Telegram unreachable \(connection refused\)"):
            TelegramNotifier.find_chats(TOKEN, base_url=tg.url)


# ===========================================================================
# The proxy
# ===========================================================================

class TestProxy:
    OUTSIDE = "http://telegram.invalid"      # reachable only through the proxy

    def test_a_proxy_carries_every_request(self, make, tg):
        bot = make(base_url=self.OUTSIDE, proxy=tg.url)
        bot.send_text("through the proxy")
        assert tg.texts == ["through the proxy"]
        assert {call["host"] for call in tg.calls} == {"telegram.invalid"}

    def test_without_one_the_environment_is_honoured(self, make, tg, monkeypatch):
        monkeypatch.setenv("HTTP_PROXY", tg.url)
        make(base_url=self.OUTSIDE).send_text("through the environment's proxy")
        assert tg.texts == ["through the environment's proxy"]

    def test_a_proxy_of_your_own_beats_the_environment(self, make, tg, monkeypatch):
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")     # nothing listens there
        make(base_url=self.OUTSIDE, proxy=tg.url).send_text("ours")
        assert tg.texts == ["ours"]

    def test_socks_needs_pysocks(self, tg, monkeypatch):
        monkeypatch.setattr(_telegram, "_socks_available", lambda: False)
        needs = 'a SOCKS proxy needs PySocks: pip install "AlgoTradeKit[notify]"'
        with pytest.raises(NotifyError) as caught:
            TelegramNotifier(TOKEN, {"me": ME}, proxy="socks5h://127.0.0.1:10808")
        assert str(caught.value) == needs
        with pytest.raises(NotifyError, match="PySocks"):
            TelegramNotifier.find_chats(TOKEN, proxy="socks5://127.0.0.1:10808")
        monkeypatch.setenv("ALL_PROXY", "socks5h://127.0.0.1:10808")
        with pytest.raises(NotifyError, match="PySocks"):
            TelegramNotifier(TOKEN, {"me": ME}, check=False)

    @pytest.mark.parametrize("proxy", ["127.0.0.1:10808", "ftp://127.0.0.1:21", ""])
    def test_a_proxy_that_is_not_one(self, proxy):
        with pytest.raises(ValueError, match="proxy must look like"):
            TelegramNotifier(TOKEN, {"me": ME}, proxy=proxy, check=False)


# ===========================================================================
# The token
# ===========================================================================

class TestToken:
    def test_a_token_that_is_not_one(self):
        for wrong in ("", "abc", "123456", "123456:", ":abc"):
            with pytest.raises(ValueError, match="does not look like a bot token"):
                TelegramNotifier(wrong, {"me": ME}, check=False)

    def test_the_token_never_shows(self, make, tg, capsys):
        seen: list[str] = []
        tg.stop()
        down = make({"me": ME}, timeout=0.3)               # a line, no exception
        seen.append(repr(down))
        down.send_text("queued while down", wait=False)
        tg.start()
        tg.when("sendMessage", chat=PUBLIC, status=403, description="Forbidden: no")
        bot = make({"me": ME, "signals": PUBLIC})
        seen.append(repr(bot))
        with pytest.raises(TelegramError) as caught:
            bot.send_text("partly")
        seen += [str(caught.value), repr(caught.value), *caught.value.failed.values(),
                 "".join(traceback.format_exception(caught.value))]
        assert caught.value.__cause__ is None and caught.value.__context__ is None
        tg.stop()
        with pytest.raises(TelegramError) as caught:
            TelegramNotifier.find_chats(TOKEN, base_url=tg.url)
        seen.append("".join(traceback.format_exception(caught.value)))
        assert caught.value.__context__ is None
        output = capsys.readouterr()
        seen += [output.out, output.err]
        assert "Telegram unreachable" in output.out
        for text in seen:
            assert SECRET not in text
        assert _telegram._mask_token(f"GET /bot{TOKEN}/getMe", TOKEN) == "GET /bot123456:***/getMe"


# ===========================================================================
# The import rule
# ===========================================================================

def _imports(node: ast.AST, in_function: bool = False):
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.Import, ast.ImportFrom)):
            yield child, in_function
        inside = in_function or isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
        yield from _imports(child, inside)


def _imported(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if node.level == 0:
        return [node.module or ""]
    base = ".".join(["AlgoTradeKit", "notify"][: 3 - node.level])
    if node.module:
        return [f"{base}.{node.module}"]
    return [f"{base}.{alias.name}" for alias in node.names]


def _rule_breaks(source: str) -> list[str]:
    """What breaks notify's import rule: at module level only the standard library,
    requests and AlgoTradeKit.notify; inside a function also visual and report."""
    allowed = set(sys.stdlib_module_names) | {"requests"}
    broken = []
    for node, in_function in _imports(ast.parse(source)):
        for module in _imported(node):
            parts = module.split(".")
            if parts[0] == "AlgoTradeKit":
                sub = parts[1] if len(parts) > 1 else ""
                fine = sub == "notify" or (in_function and sub in ("visual", "report"))
            else:
                fine = parts[0] in allowed
            if not fine:
                broken.append(module)
    return broken


class TestImportRule:
    def test_notify_imports_no_sibling_at_module_level(self):
        files = sorted(NOTIFY.glob("*.py"))
        assert {path.name for path in files} >= {"__init__.py", "_core.py", "_template.py",
                                                 "_telegram.py"}
        for path in files:
            assert _rule_breaks(path.read_text(encoding="utf-8")) == [], path.name

    def test_the_rule_catches_what_it_should(self):
        assert _rule_breaks("from ..trader import Trader") == ["AlgoTradeKit.trader"]
        assert _rule_breaks("from .. import data") == ["AlgoTradeKit.data"]
        assert _rule_breaks("import AlgoTradeKit.broker") == ["AlgoTradeKit.broker"]
        assert _rule_breaks("import pandas") == ["pandas"]
        assert _rule_breaks("from ..visual import Chart") == ["AlgoTradeKit.visual"]
        assert _rule_breaks("def picture():\n    from ..visual import Chart\n") == []
        assert _rule_breaks("def picture():\n    from ..report import show_report\n") == []
        assert _rule_breaks("def f():\n    from ..simulate import Simulate\n") == [
            "AlgoTradeKit.simulate"]

    def test_importing_notify_loads_nothing_else_of_the_library(self):
        code = ("import json, sys, AlgoTradeKit.notify\n"
                "print(json.dumps(sorted(m for m in sys.modules if m.startswith('AlgoTradeKit'))))")
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                              env=env, timeout=120, check=True)
        assert json.loads(done.stdout) == [
            "AlgoTradeKit", "AlgoTradeKit.notify", "AlgoTradeKit.notify._core",
            "AlgoTradeKit.notify._telegram", "AlgoTradeKit.notify._template"]
