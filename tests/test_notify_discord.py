"""
Tests for v1.2.0 Part 6 — Discord, through its webhooks.

``DiscordNotifier`` sits on the notifier core with Telegram's kinds, filters, templates,
queue and pictures, and talks to Discord's webhook API: ``POST`` with ``?wait=true``,
``PATCH`` for an edit, multipart for a file.  Discord is a fake served over real HTTP on
127.0.0.1, answering as its pages say: the created message, 429 with ``retry_after``, 404 for
a webhook that is gone.  The real Discord is never touched.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from test_notify import FakeClock, FakeTelegram, settle, wait_for
from test_notify_alerts import FakeVenue
from test_notify_events import (
    LIVE,
    LONG,
    MINUTE,
    SEED,
    FastClock,
    Plan,
    SteppedBroker,
    at,
    candle,
    drained,
    ms,
    paper_cfg,
    trailing_day,
)
from test_notify_pictures import PICTURE, FakeEngine, _form, backtest
from test_trader import _PaperBroker

from AlgoTradeKit.notify import DiscordError, DiscordNotifier, DiscordWebhook
from AlgoTradeKit.notify._discord import SUPPRESS_NOTIFICATIONS, markdown
from AlgoTradeKit.trader import TraderConfig, TraderPair, run_live
from AlgoTradeKit.trader import _notify as adapter_module
from AlgoTradeKit.trader import _run_live as run_live_module
from AlgoTradeKit.visual import _snapshot

SIGNALS = ("1300000000000000001", "tok-signals_A1")     # a webhook: its id and its token
DESK = ("1300000000000000002", "tok-desk_B2")


# ---------------------------------------------------------------------------
# The fake Discord
# ---------------------------------------------------------------------------

class FakeDiscord(FakeTelegram):
    """Discord's webhook API, faked over real HTTP: ``GET`` a webhook, ``POST`` a message
    (``?wait=true`` gives it back), ``PATCH`` one, with JSON or multipart bodies.  ``when()``
    programs an answer for ``"get"``, ``"post"`` or ``"patch"``."""

    def reset(self) -> None:
        super().reset()
        self.hooks = {SIGNALS[0]: {"token": SIGNALS[1], "name": "ATK signals hook",
                                   "channel_id": "1400000000000000001"},
                      DESK[0]: {"token": DESK[1], "name": "desk",
                                "channel_id": "1400000000000000002"}}
        self.messages: dict[str, dict] = {}
        self.posts: list[dict] = []       # every message created, in order
        self.edits: list[dict] = []
        self.next_message = 1500000000000000001

    def hook_url(self, hook=SIGNALS) -> str:
        return f"{self.url}/api/webhooks/{hook[0]}/{hook[1]}"

    def _build(self) -> FastAPI:
        app = FastAPI()

        @app.api_route("/api/webhooks/{hook}/{token}", methods=["GET", "POST"])
        async def webhook(hook: str, token: str, request: Request):
            return await self._serve(request, hook, token, None)

        @app.api_route("/api/webhooks/{hook}/{token}/messages/{message}", methods=["PATCH"])
        async def message(hook: str, token: str, message: str, request: Request):
            return await self._serve(request, hook, token, message)

        return app

    async def _serve(self, request: Request, hook: str, token: str, message_id: str | None):
        raw = await request.body()
        kind = request.headers.get("content-type", "")
        files: dict = {}
        if kind.startswith("multipart/form-data"):
            fields, files = _form(kind, raw)
            body = json.loads(fields.get("payload_json") or "{}")
        else:
            try:
                body = json.loads(raw) if raw else {}
            except ValueError:
                body = {}
        method = request.method.lower()
        params = dict(request.query_params)
        with self._lock:
            self.calls.append({"method": method, "hook": hook, "params": params, "body": body,
                               "files": files, "at": self._now(),
                               "thread": threading.current_thread().name})
            rule = self._rule(method, {})
        if rule is not None and rule["delay"]:
            await asyncio.sleep(rule["delay"])
        with self._lock:
            known = self.hooks.get(hook)
            if rule is not None and rule["status"] != 200:
                status = rule["status"]
                answer: dict = {"message": rule["description"] or "error", "code": 0}
                if rule["retry_after"] is not None:
                    answer = {"message": "You are being rate limited.",
                              "retry_after": rule["retry_after"], "global": False}
                return JSONResponse(answer, status_code=status)
            if known is None or known["token"] != token:
                return JSONResponse({"message": "Unknown Webhook", "code": 10015},
                                    status_code=404)
            if method == "get":
                return JSONResponse({"id": hook, "name": known["name"],
                                     "channel_id": known["channel_id"], "type": 1})
            if method == "patch":
                item = self.messages.get(message_id)
                if item is None or item["hook"] != hook:
                    return JSONResponse({"message": "Unknown Message", "code": 10008},
                                        status_code=404)
                item["content"] = body.get("content")
                self.edits.append({"id": message_id, "content": body.get("content"),
                                   "body": body, "at": self._now()})
                return JSONResponse({"id": message_id, "content": item["content"]})
            message_id = str(self.next_message)
            self.next_message += 1
            item = {"id": message_id, "hook": hook, "content": body.get("content"),
                    "body": body, "params": params, "files": files, "at": self._now()}
            self.messages[message_id] = item
            self.posts.append(item)
            if params.get("wait") != "true":
                return Response(status_code=204)
            return JSONResponse({"id": message_id, "content": item["content"],
                                 "channel_id": known["channel_id"]})

    def contents(self, hook=SIGNALS) -> list[str]:
        with self._lock:
            return [item["content"] for item in self.posts if item["hook"] == hook[0]]


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


@pytest.fixture(scope="module")
def server():
    fake = FakeDiscord()
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture
def dc(server):
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
def make(dc, clock, monkeypatch):
    """Discord notifiers on the fake, on the fast clock; every one is closed after the test."""
    made: list[DiscordNotifier] = []

    def build(webhooks=None, *, on=None, **kwargs) -> DiscordNotifier:
        used = clock if on is None else on
        kwargs.setdefault("timeout", 2.0)
        monkeypatch.setattr(DiscordNotifier, "_clock", used)
        dc.clock = used
        bot = DiscordNotifier({"signals": dc.hook_url()} if webhooks is None else webhooks,
                              **kwargs)
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


def lines(out: str) -> list[str]:
    marker = "[AlgoTradeKit] discord: "
    return [line.split(marker, 1)[1] for line in out.splitlines() if marker in line]


# ===========================================================================
# Markdown
# ===========================================================================

class TestMarkdown:
    def test_the_subset_becomes_discords_markdown(self):
        assert markdown("<b>bold</b> <i>it</i> <u>under</u> <s>out</s>") == (
            "**bold** *it* __under__ ~~out~~")
        assert markdown('<a href="https://x.io/a_b?c=1&amp;d=2">the link</a>') == (
            "[the link](https://x.io/a_b?c=1&d=2)")
        assert markdown("<code>a_b*c</code> and <pre>x * y\n_z_</pre>") == (
            "`a_b*c` and ```\nx * y\n_z_\n```")
        assert markdown("<pre><code>a_b</code></pre>") == "```\na_b\n```"

    def test_text_shows_as_written(self):
        assert markdown("a_b *c* ~d~ `e` |f| > g [h] #1 - 2") == (
            r"a\_b \*c\* \~d\~ \`e\` \|f\| > g \[h\] #1 - 2")
        assert markdown("# not a header\n> nor a quote\n- nor a list") == (
            "\\# not a header\n\\> nor a quote\n\\- nor a list")
        assert markdown("&lt;b&gt; &amp; R&amp;D") == "<b> & R&D"
        assert markdown("<blink>kept</blink>") == "kept"          # an unknown tag: its text

    def test_a_default_message(self):
        text = ("<b>🟢 LONG BTCUSDT · 1h</b>\nEntry 64,250 · SL 63,800 · TP 65,150\nRR 2.00\n"
                "🕑 2026-10-14 14:00 UTC · SIM")
        assert markdown(text) == ("**🟢 LONG BTCUSDT · 1h**\nEntry 64,250 · SL 63,800 · TP "
                                  "65,150\nRR 2.00\n🕑 2026-10-14 14:00 UTC · SIM")

    def test_the_measure_counts_what_discord_counts(self):
        for text in ("<b>a_b</b>", 'x <a href="https://x.io/q">l</a> y', "😀 *", "&amp;_",
                     "<pre>a_b</pre>"):
            measured = DiscordNotifier._measure(text)
            exact = len(markdown(text).encode("utf-16-le")) // 2
            assert measured >= exact, text
            assert measured - exact <= 2, text        # code is counted as if escaped


# ===========================================================================
# Making one
# ===========================================================================

class TestCreating:
    def test_the_check_names_each_webhook(self, make, dc, capsys):
        bot = make({"signals": dc.hook_url(), "desk": DiscordWebhook(dc.hook_url(DESK))})
        assert [call["method"] for call in dc.calls] == ["get", "get"]
        assert bot._destinations["signals"].label == "signals (ATK signals hook)"
        assert bot._destinations["desk"].label == "desk (desk)"
        assert lines(capsys.readouterr().out) == []

    def test_a_webhook_that_does_not_exist_raises(self, dc):
        with pytest.raises(DiscordError) as caught:
            DiscordNotifier({"signals": f"{dc.url}/api/webhooks/1300000000000000009/nope"})
        assert str(caught.value) == (
            "Discord webhook signals does not exist — copy its URL again from the channel's "
            "settings: Integrations, Webhooks")

    def test_discord_unreachable_is_one_line_and_it_carries_on(self, make, dc, capsys):
        url = dc.hook_url()
        dc.stop()
        bot = make({"signals": url})
        assert lines(capsys.readouterr().out) == [
            "Discord unreachable (connection refused) — carrying on; messages wait in the queue"]
        dc.start()
        assert set(bot.send_text("back")) == {"signals"}

    def test_what_a_webhook_is_given_as(self, dc):
        one = DiscordNotifier(dc.hook_url(), check=False)
        assert list(one._destinations) == [SIGNALS[0]]           # named by its id
        both = DiscordNotifier([dc.hook_url(), dc.hook_url(DESK)], check=False)
        assert list(both._destinations) == [SIGNALS[0], DESK[0]]
        with pytest.raises(ValueError, match="not a Discord webhook's URL"):
            DiscordNotifier({"x": "https://discord.com/channels/1/2"}, check=False)
        with pytest.raises(ValueError, match="webhooks is empty"):
            DiscordNotifier({}, check=False)
        with pytest.raises(ValueError, match="thread_id must be"):
            DiscordWebhook(dc.hook_url(), thread_id=0)
        with pytest.raises(ValueError, match="unknown kind 'sginal'"):
            DiscordWebhook(dc.hook_url(), events={"sginal"})

    def test_the_token_is_never_said(self, make, dc, capsys):
        bot = make()
        dc.when("post", status=400, description=f"bad {SIGNALS[1]}", times=1)
        bot.send_text("x", wait=False)
        drained(bot)
        out = capsys.readouterr().out
        assert SIGNALS[1] not in out and SIGNALS[1] not in repr(bot)
        assert SIGNALS[1] not in repr(DiscordWebhook(dc.hook_url()))
        assert "***" in out


# ===========================================================================
# Sending
# ===========================================================================

class TestSending:
    def test_a_text_goes_as_markdown_and_pings_nobody(self, make, dc):
        bot = make()
        sent = bot.send_text("<b>ATK</b> · a_b @everyone")
        (post,) = dc.posts
        assert sent == {"signals": post["id"]}
        assert post["params"] == {"wait": "true"}
        assert post["body"] == {"content": r"**ATK** · a\_b @everyone",
                                "allowed_mentions": {"parse": []}}

    def test_without_sound_and_into_a_thread(self, make, dc):
        bot = make({"signals": DiscordWebhook(dc.hook_url(), thread_id=1600, silent=True)})
        bot.send_text("quiet")
        (post,) = dc.posts
        assert post["body"]["flags"] == SUPPRESS_NOTIFICATIONS == 4096
        assert post["params"] == {"wait": "true", "thread_id": "1600"}

    def test_a_long_text_goes_as_messages_of_2000_at_most(self, make, dc):
        bot = make()
        text = "\n".join(f"line {k:03d} a_b" for k in range(400))   # 5,600 once escaped
        bot.send_text(text)
        contents = dc.contents()
        assert len(contents) == 3
        assert all(len(content.encode("utf-16-le")) // 2 <= 2000 for content in contents)
        assert contents[0].startswith(r"line 000 a\_b") and contents[-1].endswith(r"399 a\_b")

    def test_a_429_waits_its_retry_after(self, make, dc):
        clock = FakeClock(at(14))
        bot = make(on=clock)
        dc.when("post", status=429, retry_after=2.5, times=1)
        bot.send_text("later", wait=False)
        settle(bot)
        assert dc.contents() == []
        clock.advance(2.0)
        settle(bot)
        assert dc.contents() == []
        clock.advance(0.5)
        settle(bot)
        assert dc.contents() == ["later"]

    def test_a_webhook_gone_later_is_refused_once(self, make, dc, capsys):
        bot = make()
        del dc.hooks[SIGNALS[0]]                     # someone deleted the webhook
        bot.send_text("lost", wait=False)
        drained(bot)
        posts = [call for call in dc.calls if call["method"] == "post"]
        assert len(posts) == 1                       # not tried again: Discord says stop
        assert lines(capsys.readouterr().out) == [
            "could not send to signals (ATK signals hook) — 404 Unknown Webhook (code 10015) "
            "(0 messages waiting)"]

    def test_a_waiting_call_raises_discords_words(self, make, dc):
        bot = make()
        dc.when("post", status=400, description="Cannot send an empty message", times=1)
        with pytest.raises(DiscordError, match="Cannot send an empty message"):
            bot.send_text("x")


# ===========================================================================
# Pictures and files
# ===========================================================================

class TestPictures:
    def test_a_picture_is_a_file_with_its_caption(self, make, dc):
        bot = make()
        sent = bot.send_photo(PICTURE, "<b>BTCUSDT</b> · 15m")
        (post,) = dc.posts
        assert sent == {"signals": post["id"]}
        assert post["body"]["content"] == "**BTCUSDT** · 15m"
        assert post["body"]["attachments"] == [{"id": 0, "filename": "picture.png"}]
        upload = post["files"]["files[0]"]
        assert upload["data"] == PICTURE and upload["type"] == "image/png"

    def test_a_caption_over_2000_follows_the_picture(self, make, dc):
        bot = make()
        caption = "\n".join(f"caption line {k:03d}" for k in range(150))   # 2,549 characters
        bot.send_photo(PICTURE, caption)
        first, *rest = dc.posts
        assert first["files"] and "content" not in first["body"]
        assert rest and all(not item["files"] for item in rest)
        assert "".join(item["content"] for item in rest).startswith("caption line 000")

    def test_e4_a_report_is_three_messages(self, make, dc, engine):
        bot = make()
        bot.send_report(backtest())
        text, picture, page = dc.posts
        assert text["content"].startswith("**📊 BTCUSDT · Simulate report**")
        assert picture["files"]["files[0]"]["filename"] == "balance.png"
        assert page["files"]["files[0]"]["filename"] == "report.html"


# ===========================================================================
# Trading sessions: edits (E5) yes, replies (E1) no
# ===========================================================================

def run_trailing(bots, clock):
    seed, live = trailing_day()
    broker = SteppedBroker(seed, live, clock=clock, bots=bots, tf_ms=MINUTE)
    strategy = Plan({ms(at(14)): [("long", 64_250.0, 63_800.0, None)]}, timeframe="1m")
    cfg = paper_cfg(bots, tp_mode="none", sl_mode="trailing", trailing_sl_percent=0.5)
    return run_live(strategy=strategy, broker=broker, config=cfg)


class TestTrading:
    def test_q5a_a_trailing_stop_edits_the_open_message(self, make, dc, clock):
        bot = make({"signals": DiscordWebhook(dc.hook_url(), events={"start", "signal", "open",
                                                                     "sl_move", "close"})})
        run_trailing([bot], clock)
        contents = dc.contents()
        # The start comes first; the stepped feed moves the clock a minute before the sender
        # gets to it, so it may read late, as in Part 2's own test.
        assert "**▶️ Paper trading started · BTCUSDT**" in contents[0].splitlines()[:2]
        assert [content.splitlines()[0] for content in contents[1:]] == [
            "**🟢 LONG BTCUSDT · 1m**",
            "**✅ OPENED #0 · LONG BTCUSDT @ 64,250**",
            "**🏁 CLOSED #0 · LONG BTCUSDT @ 64,625.25 — stop loss**",
        ]
        opened = dc.posts[2]
        assert len(dc.edits) == 70 and {edit["id"] for edit in dc.edits} == {opened["id"]}
        assert dc.edits[-1]["content"].splitlines()[2] == (
            "SL 63,800 → 64,625.25 (trailing) · 15:11")
        # no replies on Discord: the close is a plain message
        assert all("message_reference" not in item["body"] for item in dc.posts)

    def test_a_trade_message_reads_as_on_telegram(self, make, dc, clock):
        bot = make()
        broker = SteppedBroker(SEED, LIVE, clock=clock, bots=[bot])
        run_live(strategy=Plan(LONG), broker=broker, config=paper_cfg([bot]))
        assert dc.contents()[1] == ("**🟢 LONG BTCUSDT · 1h**\nEntry 64,250 · SL 63,800 · TP "
                                    "65,150\nRR 2.00\n🕑 2026-10-14 14:00 UTC · SIM")
        assert TraderConfig(symbol="BTCUSDT", min_candles=5, notifiers=[bot]).notifiers == [bot]

    def test_an_alert_goes_to_discord(self, make, dc):
        bot = make()
        venue = FakeVenue(64_900.0)
        alert = bot.watch(venue, "BTCUSDT", above=65_000)
        wait_for(lambda: venue.streams)
        venue.streams[0].push(65_010.0)
        wait_for(lambda: dc.contents())
        assert dc.contents()[0].startswith("**🔔 BTCUSDT crossed above 65,000 · now 65,010**")
        assert alert.state == "fired"


class TestTradingNeverWaits:
    """With a Discord that takes 5 seconds per request, a candle is processed within 50 ms
    of the time it takes with no notifier (Part 2's bound)."""

    BOUND = 0.050

    @pytest.mark.filterwarnings("ignore:.*silent paper run")
    def test_run_live_candle(self, make, dc):
        bot = make(check=False, on=FakeClock(at(14)), timeout=10.0)
        dc.when("post", delay=5.0)
        cost = {}
        for label, bots in (("bare", []), ("discord", [bot])):
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

            best = float("inf")
            for _ in range(3):
                started = time.perf_counter()
                one_candle()
                best = min(best, time.perf_counter() - started)
            cost[label] = best
        assert cost["discord"] - cost["bare"] < self.BOUND, cost
        dc.clear_rules()
