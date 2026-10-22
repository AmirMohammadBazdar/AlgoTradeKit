"""
Tests for v1.2.0 Part 5 — commands from Telegram.

``TelegramNotifier(…, commanders=[…])`` listens to the bot's updates and answers
/status, /positions, /chart, /stop and /help, each as a reply to its command.  The Bot
API is the fake of ``tests/test_notify.py`` with Part 3's pictures, taught
``getUpdates`` as Telegram serves it: the updates held until a later request's
``offset`` confirms them, a long poll that answers as soon as a test pushes a message,
and 409 for a poll that a newer one cuts off.  The markets are the paper and venue
fakes of ``tests/test_trader.py``, run by the real ``run_live`` and ``Trader``.  The
real Telegram is never touched.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from test_notify import CHANNEL, GROUP, ME, ROOT, TOKEN, FakeClock, wait_for
from test_notify_alerts import FakeVenue
from test_notify_events import (
    LIVE,
    LONG,
    SEED,
    FastClock,
    Plan,
    SteppedBroker,
    at,
    candle,
    drained,
    ms,
    paper_cfg,
)
from test_notify_pictures import PICTURE, FakeEngine, PictureTelegram, _form, quarter_hours
from test_trader import (
    _T0,
    _PaperBroker,
    _PaperStream,
    _t_candle,
    _t_cfg,
    _t_patch_scheduler,
    _t_tick,
    _TFuturesBroker,
    _TPlanStrategy,
    _x_candles,
)

from AlgoTradeKit.broker import ConnectionFailed, Ticker
from AlgoTradeKit.notify import TelegramChat, TelegramError, TelegramNotifier, _commands
from AlgoTradeKit.trader import Trader, TraderPair, run_live
from AlgoTradeKit.trader import _notify as adapter_module
from AlgoTradeKit.trader import _run_live as run_live_module
from AlgoTradeKit.visual import _snapshot
from AlgoTradeKit.visual import indicator_renderer as renderer_module

BOB = 555000111                  # a member of the desk, not a commander
DESK = -1001112223334            # a group the bot is in, which the notifier does not name
NAME = "atk_test_bot"            # the fake bot's @name
CONFLICT = ("Conflict: terminated by other getUpdates request; make sure that only one bot "
            "instance is running")
WEBHOOK = ("Conflict: can't use getUpdates method while webhook is active; use deleteWebhook "
           "to delete the webhook first")
PEOPLE = {
    ME: {"id": ME, "is_bot": False, "first_name": "Ada", "username": "ada"},
    BOB: {"id": BOB, "is_bot": False, "first_name": "Bob"},
}
#: What Telegram puts in ``from`` for a group admin who posts anonymously, as the group.
ANONYMOUS = {"id": 1087968824, "is_bot": True, "first_name": "Group",
             "username": "GroupAnonymousBot"}
HELP = ("<b>🤖 @atk_test_bot · commands</b>\n"
        "/status — what is trading, since when, today's net, open positions\n"
        "/positions — each open position: entry, SL, next TP, P&amp;L now\n"
        "/chart [SYMBOL] [TF] — a picture of the last 100 candles\n"
        "/stop — stop trading (live trading asks you to confirm)\n"
        "/help — this list\n"
        "In a group, add the bot's name: /status@atk_test_bot")


# ---------------------------------------------------------------------------
# The fake Telegram, taught to hold updates
# ---------------------------------------------------------------------------

class CommandTelegram(PictureTelegram):
    """The fake Bot API, also serving ``getUpdates`` as Telegram does: the updates held
    until a later request's ``offset`` confirms them, a long poll that answers as soon
    as a test pushes a message — here after at most 0.2 s of real time — and 409 for a
    poll a newer one cuts off; and ``setMyCommands`` and ``getWebhookInfo``."""

    POLL_CAP = 0.2

    def reset(self) -> None:
        super().reset()
        self.held: list[dict] = []
        self.menus: list[dict] = []
        self.webhook = ""
        self.polls = 0
        self.chats[DESK] = {"id": DESK, "type": "supergroup", "title": "ATK Desk"}
        # Ids rise across tests: a listener that is still finishing an earlier test's
        # last request can never confirm a later test's updates.
        self.next_update = getattr(self, "next_update", 1)
        self.next_text = getattr(self, "next_text", 10_000)

    def _build(self) -> FastAPI:
        app = FastAPI()

        @app.api_route("/bot{token}/{method}", methods=["GET", "POST"])
        async def bot_api(token: str, method: str, request: Request):
            raw = await request.body()
            content = request.headers.get("content-type", "")
            files: dict = {}
            if content.startswith("multipart/form-data"):
                params, files = _form(content, raw)
            else:
                try:
                    params = json.loads(raw) if raw else {}
                except ValueError:
                    params = {}
            params.update(request.query_params)
            with self._lock:
                self.calls.append({"method": method, "params": params, "at": self._now(),
                                   "host": request.headers.get("host", ""), "files": files})
                rule = self._rule(method, params)
            if rule is not None and rule["delay"]:
                await asyncio.sleep(rule["delay"])
            if rule is not None and rule["never"]:
                return JSONResponse({"ok": False}, status_code=504)
            if method == "getUpdates" and token == TOKEN and rule is None:
                status, body = await self._long_poll(params)
            else:
                with self._lock:
                    status, body = self._answer(token, method, dict(params, _files=files), rule)
            if isinstance(body, str):
                return PlainTextResponse(body, status_code=status)
            return JSONResponse(body, status_code=status)

        return app

    async def _long_poll(self, params: dict) -> tuple[int, dict]:
        with self._lock:
            offset = params.get("offset")
            if isinstance(offset, int):
                self.held = [update for update in self.held if update["update_id"] >= offset]
            self.polls += 1
            mine = self.polls
        deadline = time.monotonic() + min(float(params.get("timeout") or 0), self.POLL_CAP)
        while True:
            with self._lock:
                if self.polls != mine:
                    return 409, {"ok": False, "error_code": 409, "description": CONFLICT}
                if self.held:
                    return 200, {"ok": True, "result": [dict(update) for update in self.held]}
            if time.monotonic() >= deadline:
                return 200, {"ok": True, "result": []}
            await asyncio.sleep(0.01)

    def _answer(self, token, method, params, rule):
        free = rule is None or (rule["status"] == 200 and rule["text"] is None)
        if method in ("setMyCommands", "getWebhookInfo") and token == TOKEN and free:
            params.pop("_files", None)
            if method == "setMyCommands":
                self.menus.append(params)
                return 200, {"ok": True, "result": True}
            return 200, {"ok": True, "result": {"url": self.webhook,
                                                "pending_update_count": len(self.held)}}
        return super()._answer(token, method, params, rule)

    def push(self, text: str, *, sender: int | None = ME, chat: int = ME,
             date: float | None = None, kind: str = "message", topic: int | None = None,
             as_chat: int | None = None, bot: bool = False) -> int:
        """A message to the bot, held for its next ``getUpdates``; returns its message id."""
        with self._lock:
            update_id, self.next_update = self.next_update, self.next_update + 1
            message_id, self.next_text = self.next_text, self.next_text + 1
            if date is None:
                date = self.clock.time() if self.clock is not None else time.time()
            message = {"message_id": message_id, "date": int(date),
                       "chat": dict(self.chats[chat]), "text": text}
            if as_chat is not None:
                message["from"] = dict(ANONYMOUS)
                message["sender_chat"] = dict(self.chats[as_chat])
            elif bot:
                message["from"] = {"id": 777000111, "is_bot": True, "first_name": "Other bot"}
            elif sender is not None:
                message["from"] = dict(PEOPLE[sender])
            if topic is not None:
                message["message_thread_id"] = topic
                message["is_topic_message"] = True
            self.held.append({"update_id": update_id, kind: message})
            return message_id

    def polls_made(self) -> list[dict]:
        with self._lock:
            return [call["params"] for call in self.calls if call["method"] == "getUpdates"]

    def answer_to(self, message_id: int) -> dict | None:
        """The message that replied to *message_id*, if one did."""
        with self._lock:
            for item in self.answered:
                reply = item["params"].get("reply_parameters") or {}
                if reply.get("message_id") == message_id:
                    return item
        return None


def answered(tg: CommandTelegram, message_id: int, timeout: float = 10.0) -> dict:
    """Until something replies to *message_id*: that reply."""
    wait_for(lambda: tg.answer_to(message_id) is not None, timeout)
    found = tg.answer_to(message_id)
    assert found is not None
    return found


def listening(tg: CommandTelegram) -> None:
    """Until a listener has started — set its menus — and asked for updates since."""
    def ready() -> bool:
        with tg._lock:
            methods = [call["method"] for call in tg.calls]
        return "setMyCommands" in methods and (
            "getUpdates" in methods[methods.index("setMyCommands"):])
    wait_for(ready)


def read(tg: CommandTelegram, *bots: TelegramNotifier) -> None:
    """Until the listener has read every message pushed so far — a later request
    confirmed it — and each notifier has sent what that made."""
    last = tg.next_update - 1
    wait_for(lambda: any(isinstance(poll.get("offset"), int) and poll["offset"] > last
                         for poll in tg.polls_made()))
    drained(*bots)


def lines_of(out: str, marker: str = "[AlgoTradeKit] telegram: ") -> list[str]:
    return [line.split(marker, 1)[1] for line in out.splitlines() if marker in line]


@pytest.fixture(autouse=True)
def _no_proxies(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                 "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    import AlgoTradeKit.trader._trader as trader_module

    monkeypatch.setattr(trader_module, "DEFAULT_STATE_PATH", str(tmp_path / "state.json"))


@pytest.fixture(autouse=True)
def _quick_pauses(monkeypatch):
    """The listener's pauses after a failure, made short: the tests that count them
    move the listener's own clock instead."""
    monkeypatch.setattr(_commands, "RETRY_STEPS", (0.01, 0.01, 0.01))
    monkeypatch.setattr(_commands, "CONFLICT_PAUSE", 0.01)


@pytest.fixture(scope="module")
def server():
    fake = CommandTelegram()
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
def clock():
    return FastClock(at(14, 0, 1))


@pytest.fixture
def make(tg, clock, monkeypatch):
    """Notifiers on the fake, on the fast clock, taking commands from Ada unless told
    otherwise; every one is closed after the test."""
    made: list[TelegramNotifier] = []

    def build(chats=None, *, on=None, commanders=(ME,), **kwargs) -> TelegramNotifier:
        used = clock if on is None else on
        kwargs.setdefault("base_url", tg.url)
        kwargs.setdefault("timeout", 2.0)
        monkeypatch.setattr(TelegramNotifier, "_clock", used)
        tg.clock = used
        bot = TelegramNotifier(TOKEN, {"me": ME} if chats is None else chats,
                               commanders=list(commanders) if commanders else None, **kwargs)
        bot._clock = used
        if isinstance(used, FastClock):
            used.jump_for.add(bot._cond)
        made.append(bot)
        return bot

    yield build
    for bot in made:
        bot.close(timeout=0.5)


@pytest.fixture
def engine(monkeypatch):
    fake = FakeEngine()
    monkeypatch.setattr(_snapshot, "snapshot", fake.snapshot)
    monkeypatch.setattr(_snapshot, "check_engine", lambda: None)
    return fake


# ---------------------------------------------------------------------------
# Trading sessions on their own threads, as a script runs them
# ---------------------------------------------------------------------------

class LiveBroker(SteppedBroker):
    """Part 2's stepped paper broker, whose feed then stays up — as a real one does until
    it is stopped — with a ticker a command asks for a price, and candles for /chart."""

    def __init__(self, rows, live_rows, *, ticker=64_700.0, frames=None, **kwargs):
        super().__init__(rows, live_rows, **kwargs)
        self.ticker = ticker            # None: the broker does not answer
        self.frames = dict(frames or {})
        self.asked: list[tuple] = []    # what was asked of it, and on which thread

    def stream_candles(self, symbol, timeframe, on_candle, *, closed_only=True):
        super().stream_candles(symbol, timeframe, on_candle, closed_only=closed_only)
        stream = _PaperStream(alive=True)
        self.streams.append(stream)
        return stream

    def get_ticker(self, symbol):
        self.asked.append(("ticker", symbol, threading.current_thread().name))
        if self.ticker is None:
            raise ConnectionFailed("the venue did not answer")
        return Ticker(symbol=symbol, last=self.ticker)

    def get_candles_df(self, symbol, timeframe, start_ms, end_ms):
        self.asked.append(("candles", symbol, timeframe, threading.current_thread().name))
        frame = self.frames.get((symbol, timeframe))
        if frame is None:
            raise ValueError("Binance rejected the request (400): code=-1121 msg=Invalid symbol.")
        stamps = frame["timestamp"]
        return frame[(stamps >= start_ms) & (stamps <= end_ms)].reset_index(drop=True)

    def end(self) -> None:
        for stream in self.streams:
            stream._alive = False


@pytest.fixture
def paper_run():
    """``run_live`` on a thread of its own; its feeds end after the test."""
    runs: list[tuple] = []

    def start(bots, broker, strategy=None, *, pairs=None, **overrides) -> dict:
        result: dict = {}

        def target() -> None:
            try:
                if pairs is not None:
                    result["report"] = run_live(pairs=pairs)
                else:
                    result["report"] = run_live(strategy=strategy or Plan(LONG), broker=broker,
                                                config=paper_cfg(bots, **overrides))
            except BaseException as exc:  # noqa: BLE001 — the test reads it
                result["error"] = exc

        thread = threading.Thread(target=target, name="test-run-live", daemon=True)
        result["thread"] = thread
        runs.append((thread, broker, result))
        thread.start()
        return result

    yield start
    for thread, broker, _result in runs:
        for each in broker if isinstance(broker, list) else [broker]:
            each.end()
        thread.join(15)
        assert not thread.is_alive()


def trading(result: dict, broker: LiveBroker, *bots: TelegramNotifier) -> None:
    """Until the session runs: its candles are in, and every notifier has sent them."""
    wait_for(lambda: broker.subscribed.is_set() or "error" in result)
    assert "error" not in result, result.get("error")
    drained(*bots)


#: A long opened on the 13:00 candle, which closes at 14:00 at 64,000: entry 64,250, SL
#: 63,800, TP 65,150, $100 of risk on 0.2222 BTC (the line's example trade).
OPEN = [candle(at(13), 64_250, 64_260, 63_990, 64_000)]


def live_trader(bots, tmp_path, monkeypatch, *, on_stop="keep", ticker=110.0):
    """The Trader of Part 2's tests: a long of 20 at 110, SL 105, $100 of risk."""
    _t_patch_scheduler(monkeypatch, [_t_tick([_t_candle(0)])])
    broker = _TFuturesBroker(_x_candles(10), fill_price=110.0)
    broker.ticker_last = ticker
    trader = Trader(broker=broker, config=_t_cfg(notifiers=list(bots)),
                    strategy=_TPlanStrategy(signals_at={_T0: [("long", 110.0, 105.0, None)]}),
                    trader_id="t5", state_path=tmp_path / "state.json", on_stop=on_stop)
    broker.mirror_engine = trader.engine
    return trader, broker


@pytest.fixture
def trader_run():
    """``Trader.run`` on a thread of its own; stopped after the test."""
    runs: list[tuple] = []

    def start(trader: Trader) -> dict:
        result: dict = {}

        def target() -> None:
            try:
                result["trades"] = trader.run()
            except BaseException as exc:  # noqa: BLE001 — the test reads it
                result["error"] = exc

        thread = threading.Thread(target=target, name="test-trader", daemon=True)
        result["thread"] = thread
        runs.append((trader, thread))
        thread.start()
        return result

    yield start
    for trader, thread in runs:
        trader.stop()
        thread.join(15)
        assert not thread.is_alive()


# ===========================================================================
# Creating a notifier that takes commands
# ===========================================================================

class TestCreating:
    def test_commanders_take_ids_as_numbers_or_digits(self, make, tg):
        bot = make(commanders=[ME, str(BOB), " 42 "])
        assert bot._commands.commanders == frozenset({ME, BOB, 42})
        listening(tg)
        assert tg.polls_made()[0]["timeout"] == 30

    def test_one_id_alone_is_enough(self, make, tg):
        bot = TelegramNotifier(TOKEN, ME, base_url=tg.url, commanders=ME, check=False)
        try:
            assert bot._commands.commanders == frozenset({ME})
        finally:
            bot.close(timeout=0.5)

    def test_without_commanders_nothing_listens(self, make, tg):
        bot = make(commanders=None)
        bot.send_text("hi")
        assert bot._commands is None
        assert tg.polls_made() == [] and tg.menus == []

    def test_an_at_name_or_a_group_is_refused(self, tg):
        with pytest.raises(ValueError) as caught:
            TelegramNotifier(TOKEN, ME, base_url=tg.url, commanders=["@ada"])
        assert str(caught.value) == (
            "commanders: '@ada' — give the person's numeric id; find_chats(token) lists it "
            "as the id of their private chat with the bot")
        with pytest.raises(ValueError) as caught:
            TelegramNotifier(TOKEN, ME, base_url=tg.url, commanders=[-1001234567890])
        assert str(caught.value) == (
            "commanders: -1001234567890 is a group or a channel — a commander is a person")
        with pytest.raises(ValueError, match="is not a Telegram user id"):
            TelegramNotifier(TOKEN, ME, base_url=tg.url, commanders=["ada"])
        with pytest.raises(TypeError, match="Telegram user ids"):
            TelegramNotifier(TOKEN, ME, base_url=tg.url, commanders=[True])
        assert tg.calls == []    # refused before anything was asked of Telegram

    def test_a_webhook_is_refused_and_its_address_kept_secret(self, tg):
        tg.webhook = "https://example.com/hook/s3cret-path"
        with pytest.raises(TelegramError) as caught:
            TelegramNotifier(TOKEN, ME, base_url=tg.url, commanders=[ME])
        assert str(caught.value) == (
            "this bot sends its updates to a webhook (example.com) — getUpdates cannot run "
            "beside it; remove the webhook, or leave commanders= out")
        assert "s3cret" not in str(caught.value)
        assert _commands.listening(TOKEN) is None

    def test_one_bot_takes_commands_once_in_a_program(self, make, tg):
        first = make()
        listening(tg)
        with pytest.raises(ValueError) as caught:
            make()
        assert str(caught.value) == ("this bot already takes commands in this program — give "
                                     "commanders= to one notifier")
        first.close(timeout=0.5)
        second = make()                 # the first no longer listens: the second may
        assert _commands.listening(TOKEN) is second._commands

    def test_the_module_loads_with_the_first_notifier_that_takes_commands(self):
        code = ("import json, sys\n"
                "from AlgoTradeKit.notify import TelegramNotifier\n"
                "TelegramNotifier('1:a', 1, check=False, base_url='http://127.0.0.1:9')\n"
                "before = 'AlgoTradeKit.notify._commands' in sys.modules\n"
                "TelegramNotifier('2:b', 1, check=False, base_url='http://127.0.0.1:9', "
                "commanders=[1])\n"
                "print(json.dumps([before, 'AlgoTradeKit.notify._commands' in sys.modules]))")
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
        done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                              env=env, timeout=120, check=True)
        assert json.loads(done.stdout.strip().splitlines()[-1]) == [False, True]


# ===========================================================================
# Listening (Q5.5 a)
# ===========================================================================

class TestListening:
    def test_q5a_the_long_poll_asks_for_every_update_and_confirms_what_it_read(self, make, tg):
        bot = make()
        listening(tg)
        sent = tg.push("/help")
        answered(tg, sent)
        read(tg, bot)
        polls = tg.polls_made()
        assert all(poll["timeout"] == 30 and poll["allowed_updates"] == [] for poll in polls)
        assert polls[-1]["offset"] == tg.next_update      # everything read is confirmed
        assert tg.held == []

    def test_each_commander_gets_the_command_menu(self, make, tg):
        make(commanders=[ME, BOB])
        listening(tg)
        assert [menu["scope"] for menu in tg.menus] == [
            {"type": "chat", "chat_id": ME}, {"type": "chat", "chat_id": BOB}]
        assert [entry["command"] for entry in tg.menus[0]["commands"]] == [
            "status", "positions", "chart", "stop", "help"]
        assert tg.menus[0]["commands"][1]["description"] == (
            "each open position: entry, SL, next TP, P&L now")

    def test_a_menu_telegram_refuses_is_one_line(self, make, tg, capsys):
        tg.when("setMyCommands", status=400, description="Bad Request: chat not found", times=1)
        bot = make()
        listening(tg)
        sent = tg.push("/help")
        answered(tg, sent)                          # the commands work all the same
        drained(bot)
        assert ("could not set the command menu for 123456789 — 400 Bad Request: chat not "
                "found") in lines_of(capsys.readouterr().out)

    def test_q7a_a_command_sent_before_the_program_started_is_skipped(self, make, tg, capsys):
        stale = tg.push("/stop", date=at(6, 12).timestamp())
        bot = make()
        read(tg, bot)
        assert tg.answer_to(stale) is None
        assert lines_of(capsys.readouterr().out) == [
            "skipped 1 command sent before this program started (/stop from 123456789 at "
            "06:12 UTC)"]
        assert tg.held == []                        # confirmed: never read again

    def test_q7a_several_waiting_commands_name_the_last(self, make, tg, capsys):
        tg.push("/stop", date=at(6, 12).timestamp())
        tg.push("hello", date=at(6, 13).timestamp())             # no command: not counted
        tg.push("/status", sender=BOB, chat=DESK, date=at(6, 14).timestamp())
        bot = make()
        read(tg, bot)
        assert lines_of(capsys.readouterr().out) == [
            "skipped 2 commands sent before this program started (the last: /status from "
            "555000111 at 06:14 UTC)"]

    def test_q7a_a_command_sent_once_it_listens_is_answered(self, make, tg):
        make()
        listening(tg)
        sent = tg.push("/help")
        assert answered(tg, sent)["text"].startswith("<b>🤖 @atk_test_bot · commands</b>")

    def test_close_stops_listening_and_the_next_send_starts_it_again(self, make, tg, clock,
                                                                    capsys):
        bot = make()
        listening(tg)
        bot.close(timeout=0.5)
        time.sleep(3 * CommandTelegram.POLL_CAP)     # the last poll comes back, and is dropped
        polls = len(tg.polls_made())
        late = tg.push("/help")
        time.sleep(3 * CommandTelegram.POLL_CAP)
        assert len(tg.polls_made()) == polls        # nothing reads while it is closed
        clock.advance(5)
        bot.send_text("back")                        # the sender starts again, and listening
        read(tg, bot)
        assert tg.answer_to(late) is None
        assert ("skipped 1 command sent while the bot was not listening (/help from 123456789 "
                "at 14:00 UTC)") in lines_of(capsys.readouterr().out)
        again = tg.push("/help")
        answered(tg, again)

    def test_find_chats_beside_a_listener_asks_telegram_nothing(self, make, tg, capsys):
        tg.push("/stop", date=at(6, 12).timestamp())
        tg.push("good morning", sender=BOB, chat=DESK, date=at(6, 20).timestamp())
        bot = make()
        read(tg, bot)
        asked = len(tg.calls)
        chats = TelegramNotifier.find_chats(TOKEN, base_url=tg.url)
        assert len(tg.calls) == asked
        assert [(chat.id, chat.kind, chat.title) for chat in chats] == [
            (ME, "private", "Ada"), (DESK, "supergroup", "ATK Desk")]
        assert "Chats that wrote to @atk_test_bot in the last 24 hours:" in capsys.readouterr().out

    def test_a_409_from_another_program_is_one_line_and_it_keeps_listening(self, make, tg,
                                                                           capsys):
        tg.when("getUpdates", status=409, description=CONFLICT, times=2)
        bot = make()
        listening(tg)
        sent = tg.push("/help")
        answered(tg, sent)
        drained(bot)
        assert lines_of(capsys.readouterr().out) == [
            "another program is reading this bot's updates (409 Conflict) — a command may "
            "reach it instead of this one", "/help from 123456789"]

    def test_a_webhook_found_while_listening_turns_commands_off(self, make, tg, capsys):
        tg.when("getUpdates", status=409, description=WEBHOOK, times=1)
        bot = make(check=False)
        wait_for(lambda: "commands are off" in "".join(lines_of(capsys.readouterr().out)))
        assert _commands.listening(TOKEN) is None
        polls = len(tg.polls_made())
        assert bot.send_text("texts still go") == {"me": 1}   # and listening stays off
        time.sleep(3 * CommandTelegram.POLL_CAP)
        assert len(tg.polls_made()) == polls

    def test_a_rejected_token_turns_commands_off(self, make, tg, capsys):
        tg.when("getUpdates", status=401, description="Unauthorized", times=1)
        make()
        said: list[str] = []
        wait_for(lambda: said.extend(lines_of(capsys.readouterr().out)) or any(
            "commands are off" in line for line in said))
        assert "commands: Telegram rejected the token (401 Unauthorized) — commands are off" in said

    def test_an_outage_is_one_line_after_a_minute(self, make, tg, monkeypatch, capsys):
        slow = FakeClock()
        monkeypatch.setattr(_commands, "_clock", slow)
        monkeypatch.setattr(_commands, "RETRY_STEPS", (1.0, 5.0, 15.0))
        bot = make()
        listening(tg)
        tg.stop()                                   # Telegram is down: connections refused
        said: list[str] = []

        def looked() -> bool:
            said.extend(lines_of(capsys.readouterr().out))
            return any("unreachable" in line for line in said)

        deadline = time.monotonic() + 20
        while not looked():
            assert time.monotonic() < deadline, said
            slow.advance(5.0)
            time.sleep(0.05)
        (line,) = [line for line in said if "unreachable" in line]
        assert line.startswith("commands: Telegram unreachable for 1 min (")
        assert line.endswith(") — still trying")
        tg.start()
        while not any("reachable again" in line for line in said):
            assert time.monotonic() < deadline + 20
            slow.advance(15.0)
            time.sleep(0.05)
            said.extend(lines_of(capsys.readouterr().out))
        sent = tg.push("/help")
        answered(tg, sent)
        drained(bot)


# ===========================================================================
# Who may command (Q5.1 a), and everyone else (Q5.6 b)
# ===========================================================================

class TestWho:
    def test_q1a_a_commander_is_answered_in_a_private_chat_and_in_a_group(self, make, tg):
        make()
        listening(tg)
        private = tg.push("/help")
        group = tg.push(f"/help@{NAME}", chat=DESK)
        plain = tg.push("/help", chat=DESK)
        assert answered(tg, private)["chat_id"] == ME
        assert answered(tg, group)["chat_id"] == DESK
        assert answered(tg, plain)["chat_id"] == DESK

    def test_a_command_for_another_bot_is_not_ours(self, make, tg, capsys):
        bot = make()
        listening(tg)
        theirs = tg.push("/status@OtherBot", chat=DESK)
        read(tg, bot)
        assert tg.answer_to(theirs) is None
        assert lines_of(capsys.readouterr().out) == []

    def test_q6b_a_stranger_is_answered_once_and_logged_once_a_minute(self, make, tg,
                                                                     monkeypatch, capsys):
        slow = FakeClock()
        monkeypatch.setattr(_commands, "_clock", slow)
        bot = make()
        listening(tg)
        first = tg.push(f"/stop@{NAME}", sender=BOB, chat=DESK)
        second = tg.push(f"/stop@{NAME}", sender=BOB, chat=DESK)
        assert answered(tg, first)["text"] == (
            "<b>🚫 Only this bot's commanders can use /stop.</b>\n🕑 2026-10-14 14:00 UTC")
        read(tg, bot)
        assert tg.answer_to(second) is None
        slow.advance(61)
        third = tg.push("/status", sender=BOB)
        read(tg, bot)
        assert tg.answer_to(third) is None
        assert lines_of(capsys.readouterr().out) == [
            "refused /stop from 555000111 — not a commander",
            "refused /status from 555000111 — not a commander (1 more since the last line)"]

    def test_a_strangers_start_is_refused_once(self, make, tg):
        make()
        listening(tg)
        sent = tg.push("/start", sender=BOB, chat=ME)
        assert answered(tg, sent)["text"].startswith(
            "<b>🚫 Only this bot's commanders can use /start.</b>")

    def test_an_anonymous_admin_cannot_command(self, make, tg, capsys):
        bot = make()
        listening(tg)
        sent = tg.push(f"/stop@{NAME}", chat=DESK, as_chat=DESK)
        answered(tg, sent)
        drained(bot)
        assert lines_of(capsys.readouterr().out) == [
            f"refused /stop from {DESK} — sent anonymously — turn off Remain anonymous to "
            "command"]

    def test_what_never_commands(self, make, tg, capsys):
        bot = make()
        listening(tg)
        ignored = [
            tg.push("/stop", kind="edited_message"),
            tg.push("/stop", chat=CHANNEL, sender=None, kind="channel_post"),
            tg.push("please stop"),
            tg.push("/stop", bot=True, chat=DESK),
        ]
        read(tg, bot)
        assert all(tg.answer_to(message) is None for message in ignored)
        assert lines_of(capsys.readouterr().out) == []

    def test_an_unknown_command(self, make, tg):
        make()
        listening(tg)
        sent = tg.push("/stauts")
        assert answered(tg, sent)["text"] == (
            "<b>Unknown command /stauts — /help lists them.</b>\n🕑 2026-10-14 14:00 UTC")

    def test_help_and_start(self, make, tg, capsys):
        bot = make()
        listening(tg)
        helped = tg.push("/help")
        started = tg.push("/start")
        assert answered(tg, helped)["text"] == f"{HELP}\n🕑 2026-10-14 14:00 UTC"
        assert answered(tg, started)["text"] == f"{HELP}\n🕑 2026-10-14 14:00 UTC"
        drained(bot)
        assert lines_of(capsys.readouterr().out) == [
            "/help from 123456789", "/start from 123456789"]

    def test_the_token_is_in_no_line(self, make, tg, capsys):
        tg.when("setMyCommands", status=400, description="Bad Request: chat not found", times=1)
        bot = make()
        listening(tg)
        sent = tg.push("/stauts")
        answered(tg, sent)
        drained(bot)
        assert "TEST-secret_token" not in capsys.readouterr().out


# ===========================================================================
# Answers: where they go
# ===========================================================================

class TestAnswers:
    def test_an_answer_goes_to_its_chat_whatever_the_filters(self, make, tg):
        make({"me": TelegramChat(ME, events={"close"}, silent=True)})
        listening(tg)
        mine = tg.push("/help")
        theirs = tg.push("/help", chat=DESK)        # a chat the notifier does not name
        reply = answered(tg, mine)
        assert reply["chat_id"] == ME
        assert reply["params"]["reply_parameters"] == {
            "message_id": mine, "allow_sending_without_reply": True}
        assert "disable_notification" not in reply["params"]    # an answer rings
        assert answered(tg, theirs)["chat_id"] == DESK

    def test_an_answer_in_a_forum_topic_stays_in_its_topic(self, make, tg):
        make()
        listening(tg)
        sent = tg.push("/help", chat=GROUP, topic=7)
        assert answered(tg, sent)["params"]["message_thread_id"] == 7

    def test_with_no_session(self, make, tg):
        make()
        listening(tg)
        replies = {command: tg.push(command) for command in
                   ("/status", "/positions", "/stop", "/stop CONFIRM", "/chart")}
        assert answered(tg, replies["/status"])["text"] == (
            "<b>📟 No trading session is running</b>\n🕑 2026-10-14 14:00 UTC")
        assert answered(tg, replies["/positions"])["text"].startswith(
            "<b>📟 No trading session is running</b>")
        assert answered(tg, replies["/stop"])["text"].startswith(
            "<b>📟 No trading session is running — there is nothing to stop</b>")
        assert answered(tg, replies["/stop CONFIRM"])["text"].startswith(
            "<b>No stop is waiting — send /stop first</b>")
        assert answered(tg, replies["/chart"])["text"].startswith(
            "<b>/chart needs a trading session — its broker gives the candles</b>")

    def test_status_names_the_armed_alerts(self, make, tg):
        bot = make()
        listening(tg)
        bot.watch(FakeVenue(65_200.0), "BTCUSDT", above=70_000)
        sent = tg.push("/status")
        assert answered(tg, sent)["text"] == (
            "<b>📟 No trading session is running</b>\nAlerts: 1 armed\n🕑 2026-10-14 14:00 UTC")


# ===========================================================================
# Paper trading (Q5.4 a)
# ===========================================================================

class TestPaperTrading:
    def test_q4a_status_and_positions(self, make, tg, clock, paper_run):
        bot = make()
        broker = LiveBroker(SEED, OPEN, clock=clock, bots=[bot])
        result = paper_run([bot], broker)
        trading(result, broker, bot)
        clock.advance(6 * 3600 + 29 * 60)           # 20:29:01: 6h 29m since the start
        status = tg.push("/status")
        positions = tg.push("/positions")
        assert answered(tg, status)["text"] == (
            "<b>📟 Paper trading · BTCUSDT · up 6h 29m</b>\n"
            "Today: no trades\n"
            "Open: 1 · +$100.00 now\n"
            "🕑 2026-10-14 20:29 UTC · SIM")
        assert answered(tg, positions)["text"] == (
            "<b>📂 Open positions · 1</b>\n"
            "#0 LONG BTCUSDT 0.2222 @ 64,250\n"
            "SL 63,800 · Next TP 65,150 · now 64,700 · +$100.00 (+1.00 R)\n"
            "🕑 2026-10-14 20:29 UTC · SIM")
        # Q5.8 b: the price was asked of the broker on the listener's thread
        assert {thread for what, _symbol, thread in broker.asked if what == "ticker"} == {
            _commands.THREAD_NAME}

    def test_q8b_the_runners_price_when_the_broker_does_not_answer(self, make, tg, clock,
                                                                  paper_run):
        bot = make()
        broker = LiveBroker(SEED, OPEN, clock=clock, bots=[bot], ticker=None)
        result = paper_run([bot], broker)
        trading(result, broker, bot)
        positions = tg.push("/positions")
        status = tg.push("/status")
        assert answered(tg, positions)["text"].splitlines()[2] == (
            "SL 63,800 · Next TP 65,150 · now 64,000 (14:00 close) · -$55.56 (-0.56 R)")
        assert answered(tg, status)["text"].splitlines()[2] == "Open: 1 · -$55.56 (14:00 close)"

    def test_status_after_a_trade_closed_today(self, make, tg, clock, paper_run):
        bot = make()
        broker = LiveBroker(SEED, LIVE, clock=clock, bots=[bot])   # opens, then its TP
        result = paper_run([bot], broker)
        trading(result, broker, bot)
        sent = tg.push("/status")
        assert answered(tg, sent)["text"].splitlines()[1:3] == [
            "Today: 1 trade · won 1 · lost 0 · net +$200.00", "No open positions"]

    def test_two_pairs_in_one_status(self, make, tg, clock, paper_run):
        bot = make()
        eth_seed = [candle(at(hour), 2_500, 2_501, 2_499, 2_500) for hour in range(3, 14)]
        btc = LiveBroker(SEED, OPEN, clock=clock, bots=[bot])
        eth = LiveBroker(eth_seed, [], clock=clock, bots=[bot], ticker=2_510.0)
        result = paper_run([bot], [btc, eth], pairs=[
            TraderPair(broker=btc, config=paper_cfg([bot]), strategy=Plan(LONG)),
            TraderPair(broker=eth, config=paper_cfg([bot], symbol="ETHUSDT"), strategy=Plan()),
        ])
        wait_for(lambda: btc.subscribed.is_set() and eth.subscribed.is_set())
        assert "error" not in result
        drained(bot)
        clock.advance(2 * 3600 + 15 * 60)
        sent = tg.push("/status")
        assert answered(tg, sent)["text"] == (
            "<b>📟 Paper trading · BTCUSDT, ETHUSDT · up 2h 15m</b>\n"
            "BTCUSDT: no trades · 1 open, +$100.00 now\n"
            "ETHUSDT: no trades · no open positions\n"
            "🕑 2026-10-14 16:15 UTC · SIM")
        positions = tg.push("/positions")
        assert answered(tg, positions)["text"].splitlines()[0] == "<b>📂 Open positions · 1</b>"

    def test_q4a_stop_ends_paper_trading_at_once(self, make, tg, clock, paper_run, capsys):
        bot = make()
        broker = LiveBroker(SEED, OPEN, clock=clock, bots=[bot])
        result = paper_run([bot], broker)
        trading(result, broker, bot)
        sent = tg.push("/stop")
        result["thread"].join(15)
        assert not result["thread"].is_alive() and "error" not in result
        assert answered(tg, sent)["text"].splitlines()[0] == (
            "<b>⏹ Stopping paper trading of BTCUSDT…</b>")
        assert tg.texts(ME)[-1].splitlines()[0] == (
            "<b>⏹ Paper trading stopped · BTCUSDT · /stop from Telegram · 1 paper position "
            "open</b>")
        out = capsys.readouterr().out
        assert "[AlgoTradeKit] run_live: /stop from Telegram — stopping." in out
        assert "/stop from 123456789 — stopping paper trading" in lines_of(out)
        assert _commands.listening(TOKEN) is None       # closed with the session

    def test_status_while_the_session_starts(self, make, tg, clock, paper_run):
        bot = make()
        seeding = threading.Event()
        go = threading.Event()

        class SlowSeed(LiveBroker):
            def fetch_last_candles(self, symbol, timeframe, count):
                seeding.set()
                assert go.wait(10)
                return super().fetch_last_candles(symbol, timeframe, count)

        broker = SlowSeed(SEED, OPEN, clock=clock, bots=[bot])
        result = paper_run([bot], broker)
        assert seeding.wait(10)
        sent = tg.push("/status")
        assert answered(tg, sent)["text"] == (
            "<b>▶️ Paper trading is starting · BTCUSDT</b>\n🕑 2026-10-14 14:00 UTC · SIM")
        go.set()
        trading(result, broker, bot)

    def test_a_stop_while_it_starts_acts_once_trading_runs(self, make, tg, clock, paper_run):
        bot = make()
        seeding = threading.Event()
        go = threading.Event()

        class SlowSeed(LiveBroker):
            def fetch_last_candles(self, symbol, timeframe, count):
                seeding.set()
                assert go.wait(10)
                return super().fetch_last_candles(symbol, timeframe, count)

        broker = SlowSeed(SEED, OPEN, clock=clock, bots=[bot])
        result = paper_run([bot], broker)
        assert seeding.wait(10)
        sent = tg.push("/stop")
        answered(tg, sent)
        go.set()
        result["thread"].join(15)
        assert not result["thread"].is_alive() and "error" not in result
        heads = [text.splitlines()[0] for text in tg.texts(ME)]
        assert heads[0] == "<b>⏹ Stopping paper trading of BTCUSDT…</b>"
        assert heads[1] == "<b>▶️ Paper trading started · BTCUSDT</b>"
        assert heads[-1].startswith("<b>⏹ Paper trading stopped · BTCUSDT · /stop from Telegram")


# ===========================================================================
# Live trading (Q5.2 a)
# ===========================================================================

class TestLiveTrading:
    def test_q2a_stop_asks_for_a_confirmation_then_stops(self, make, tg, tmp_path, monkeypatch,
                                                        trader_run, capsys):
        bot = make()
        trader, broker = live_trader([bot], tmp_path, monkeypatch)
        result = trader_run(trader)
        wait_for(lambda: len(trader.open_trades) == 1)
        drained(bot)
        asked = tg.push("/stop")
        assert answered(tg, asked)["text"] == (
            "<b>⚠️ Stop live trading of BTCUSDT?</b>\n"
            '1 position stays open with its venue SL/TP (on_stop="keep").\n'
            "Send /stop CONFIRM within 60 s.\n"
            "🕑 2026-10-14 14:00 UTC · LIVE")
        assert result["thread"].is_alive()          # nothing stopped yet
        confirmed = tg.push("/stop CONFIRM")
        result["thread"].join(15)
        assert not result["thread"].is_alive() and "error" not in result
        assert answered(tg, confirmed)["text"].splitlines()[0] == (
            "<b>⏹ Stopping live trading of BTCUSDT…</b>")
        assert tg.texts(ME)[-1].splitlines()[0] == (
            "<b>⏹ Live trading stopped · BTCUSDT · /stop from Telegram · 1 position left open "
            "with its venue SL/TP</b>")
        out = capsys.readouterr().out
        assert "[AlgoTradeKit] trader: /stop from Telegram — stopping." in out
        assert lines_of(out)[-2:] == [
            "/stop from 123456789 — asked to confirm",
            "/stop CONFIRM from 123456789 — stopping live trading"]
        assert len(trader.open_trades) == 1        # on_stop="keep": the venue SL/TP stay

    def test_q2a_close_all_says_what_it_closes(self, make, tg, tmp_path, monkeypatch,
                                              trader_run):
        bot = make()
        trader, _broker = live_trader([bot], tmp_path, monkeypatch, on_stop="close_all")
        trader_run(trader)
        wait_for(lambda: len(trader.open_trades) == 1)
        sent = tg.push("/stop")
        assert answered(tg, sent)["text"].splitlines()[1] == (
            "Every open position is closed at the market and its orders cancelled "
            '(on_stop="close_all"): 1 now.')

    def test_a_confirm_from_someone_else_or_too_late_stops_nothing(self, make, tg, clock,
                                                                  tmp_path, monkeypatch,
                                                                  trader_run):
        bot = make(commanders=(ME, BOB))
        trader, _broker = live_trader([bot], tmp_path, monkeypatch)
        result = trader_run(trader)
        wait_for(lambda: len(trader.open_trades) == 1)
        answered(tg, tg.push("/stop"))
        elsewhere = tg.push("/stop CONFIRM", chat=DESK)        # Ada, in another chat
        other = tg.push("/stop CONFIRM", sender=BOB, chat=ME)   # Bob, in Ada's chat
        for message in (elsewhere, other):
            assert answered(tg, message)["text"].splitlines()[0] == (
                "<b>No stop is waiting — send /stop first</b>")
        clock.advance(61)
        late = tg.push("/stop CONFIRM")
        assert answered(tg, late)["text"].splitlines()[0] == (
            "<b>No stop is waiting — send /stop first</b>")
        drained(bot)
        assert result["thread"].is_alive()

    def test_positions_of_a_trader(self, make, tg, tmp_path, monkeypatch, trader_run):
        bot = make()
        trader, broker = live_trader([bot], tmp_path, monkeypatch)
        trader_run(trader)
        wait_for(lambda: len(trader.open_trades) == 1)
        broker.ticker_last = 112.0
        sent = tg.push("/positions")
        assert answered(tg, sent)["text"] == (
            "<b>📂 Open positions · 1</b>\n"
            "#0 LONG BTCUSDT 20 @ 110\n"
            "SL 105 · now 112 · +$40.00 (+0.40 R)\n"
            "🕑 2026-10-14 14:00 UTC · LIVE")


# ===========================================================================
# /chart (Q5.3 b)
# ===========================================================================

class TestChart:
    def test_chart_of_the_pair_with_its_open_position(self, make, tg, clock, paper_run, engine,
                                                     monkeypatch):
        drawn: list[dict] = []
        real = renderer_module.populate_chart

        def recording(chart, frame, **kwargs):
            drawn.append(kwargs)
            return real(chart, frame, **kwargs)

        monkeypatch.setattr(renderer_module, "populate_chart", recording)
        bot = make()
        broker = LiveBroker(SEED, OPEN, clock=clock, bots=[bot])
        result = paper_run([bot], broker)
        trading(result, broker, bot)
        sent = tg.push("/chart")
        picture = answered(tg, sent)
        assert picture["media"] == "sendPhoto" and picture["file"]["data"] == PICTURE
        assert picture["text"] == ("<b>BTCUSDT · 1h · now 64,700</b>\n"
                                   "🕑 2026-10-14 14:00 UTC · SIM")
        assert [position.trade_id for position in drawn[-1]["open_positions"]] == [0]
        assert engine.asked[-1]["thread"] == "atk-notify-telegram"

    def test_chart_of_another_symbol_from_the_pairs_broker(self, make, tg, clock, paper_run,
                                                          engine):
        bot = make()
        broker = LiveBroker(SEED, OPEN, clock=clock, bots=[bot],
                            frames={("ETHUSDT", "15m"): quarter_hours()})
        result = paper_run([bot], broker)
        trading(result, broker, bot)
        sent = tg.push("/chart ethusdt 15m")
        picture = answered(tg, sent)
        assert picture["media"] == "sendPhoto"
        assert picture["text"].splitlines()[0] == "<b>ETHUSDT · 15m · now 64,700</b>"
        assert ("candles", "ETHUSDT", "15m", _commands.THREAD_NAME) in broker.asked

    def test_chart_of_a_wrong_symbol(self, make, tg, clock, paper_run, engine):
        bot = make()
        broker = LiveBroker(SEED, OPEN, clock=clock, bots=[bot])
        result = paper_run([bot], broker)
        trading(result, broker, bot)
        sent = tg.push("/chart BTCUSTD")
        assert answered(tg, sent)["text"].splitlines()[0] == (
            "<b>/chart BTCUSTD: Binance rejected the request (400): code=-1121 msg=Invalid "
            "symbol.</b>")

    def test_chart_without_the_engine(self, make, tg, clock, paper_run, monkeypatch):
        def missing() -> None:
            raise ImportError(_snapshot.NO_ENGINE)

        monkeypatch.setattr(_snapshot, "check_engine", missing)
        bot = make()
        broker = LiveBroker(SEED, OPEN, clock=clock, bots=[bot])
        result = paper_run([bot], broker)
        trading(result, broker, bot)
        sent = tg.push("/chart")
        assert answered(tg, sent)["text"].splitlines()[0] == (
            '<b>chart pictures need the [notify] extra: pip install "AlgoTradeKit[notify]", '
            "then: playwright install chromium</b>")

    def test_chart_of_a_month_is_refused(self, make, tg, clock, paper_run, engine):
        bot = make()
        broker = LiveBroker(SEED, OPEN, clock=clock, bots=[bot])
        result = paper_run([bot], broker)
        trading(result, broker, bot)
        sent = tg.push("/chart BTCUSDT 1M")
        assert answered(tg, sent)["text"].splitlines()[0] == (
            "<b>timeframe '1M' has no fixed length — use one such as '1m', '15m', '4h' or "
            "'1d'</b>")


# ===========================================================================
# Trading never waits
# ===========================================================================

class TestTradingNeverWaits:
    """With a Telegram that takes 5 seconds per request — the long poll too — and
    commands waiting, a candle is processed within 50 ms of the time it takes with no
    notifier (Part 2's bound)."""

    BOUND = 0.050

    def timed(self, function, repeat=3) -> float:
        best = float("inf")
        for _ in range(repeat):
            started = time.perf_counter()
            function()
            best = min(best, time.perf_counter() - started)
        return best

    @pytest.mark.filterwarnings("ignore:.*silent paper run")
    def test_run_live_candle(self, make, tg):
        bot = make(check=False, on=FakeClock(at(14)), timeout=10.0)
        listening(tg)
        for command in ("/status", "/positions", "/help"):
            tg.push(command)
        tg.when("sendMessage", delay=5.0)
        tg.when("getUpdates", delay=5.0)
        cost = {}
        for label, bots in (("bare", []), ("listening", [bot])):
            broker = _PaperBroker(SEED)
            session = run_live_module._build_session(
                TraderPair(broker=broker, config=paper_cfg(bots, symbol="BTCUSDT"),
                           strategy=Plan(LONG)),
                adapter_module.NotifySession("paper"))
            run_live_module._attach_sim(session, 10_000.0)
            session.sim.seed()
            session.bridge.prime()
            hour = iter(range(13, 40))

            def one_candle(session=session, hour=hour):
                h = next(hour)
                plan = {ms(at(h)): [("long", 64_250.0, 63_800.0, 65_150.0)]}
                session.pair.strategy.signals.update(plan)
                session.sim.process_closed_candle(candle(at(h), 64_250, 65_200, 64_240, 65_100))

            cost[label] = self.timed(one_candle)
        assert cost["listening"] - cost["bare"] < self.BOUND, cost
        tg.clear_rules()
