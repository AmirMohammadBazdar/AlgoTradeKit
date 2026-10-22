"""
Tests for v1.2.0 Part 6 — a webhook: JSON posted to your own URL.

``WebhookNotifier`` sits on the notifier core and posts each message as JSON (Q6.4 a): its
``id``, kind, symbol, time and source, the text plain and rich, and the event's own values
raw; a picture as a multipart part; and with ``secret=`` an HMAC signature.  The receiver is a
fake served over real HTTP on 127.0.0.1.  No real webhook is ever touched.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import socket
import time

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from test_notify import FakeClock, FakeTelegram, settle, wait_for
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
from test_notify_pictures import PICTURE, _form
from test_trader import _PaperBroker

from AlgoTradeKit.notify import WebhookNotifier, WebhookTarget
from AlgoTradeKit.trader import TraderPair, run_live
from AlgoTradeKit.trader import _notify as adapter_module
from AlgoTradeKit.trader import _run_live as run_live_module

SECRET = "s3cret-shared"


# ---------------------------------------------------------------------------
# The fake receiver
# ---------------------------------------------------------------------------

class FakeReceiver(FakeTelegram):
    """A webhook receiver over real HTTP: ``POST /hook/<name>`` records the headers and the
    body, JSON or multipart.  ``when(name, …)`` programs an answer for one hook."""

    def reset(self) -> None:
        super().reset()
        self.got: list[dict] = []

    def hook(self, name: str = "bot") -> str:
        return f"{self.url}/hook/{name}"

    def _build(self) -> FastAPI:
        app = FastAPI()

        @app.post("/hook/{name}")
        async def hook(name: str, request: Request):
            raw = await request.body()
            kind = request.headers.get("content-type", "")
            files: dict = {}
            if kind.startswith("multipart/form-data"):
                fields, files = _form(kind, raw)
                signed = fields["payload_json"].encode()
                payload = json.loads(signed)
            else:
                signed = raw
                payload = json.loads(raw)
            with self._lock:
                self.calls.append({"method": name, "params": {}})
                rule = self._rule(name, {})
            if rule is not None and rule["delay"]:
                await asyncio.sleep(rule["delay"])
            with self._lock:
                if rule is not None and rule["status"] != 200:
                    headers = {}
                    if rule["retry_after"] is not None:
                        headers["Retry-After"] = str(rule["retry_after"])
                    return JSONResponse({"error": rule["description"]},
                                        status_code=rule["status"], headers=headers)
                self.got.append({"name": name, "payload": payload, "signed": signed,
                                 "headers": dict(request.headers), "files": files,
                                 "at": self._now()})
            return JSONResponse({"ok": True})

        return app

    def payloads(self, name: str = "bot") -> list[dict]:
        with self._lock:
            return [item["payload"] for item in self.got if item["name"] == name]


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
    fake = FakeReceiver()
    fake.start()
    yield fake
    fake.stop()


@pytest.fixture
def rx(server):
    if not server.running:
        server.start()
    server.reset()
    yield server


@pytest.fixture
def clock():
    return FastClock(at(14, 0, 1))


@pytest.fixture
def make(rx, clock, monkeypatch):
    """Webhook notifiers to the fake receiver, on the fast clock; closed after the test."""
    made: list[WebhookNotifier] = []

    def build(targets=None, *, on=None, **kwargs) -> WebhookNotifier:
        used = clock if on is None else on
        kwargs.setdefault("timeout", 2.0)
        monkeypatch.setattr(WebhookNotifier, "_clock", used)
        rx.clock = used
        bot = WebhookNotifier({"bot": rx.hook()} if targets is None else targets, **kwargs)
        bot._clock = used
        if isinstance(used, FastClock):
            used.jump_for.add(bot._cond)
        made.append(bot)
        return bot

    yield build
    for bot in made:
        bot.close(timeout=0.5)


def lines(out: str) -> list[str]:
    marker = "[AlgoTradeKit] webhook: "
    return [line.split(marker, 1)[1] for line in out.splitlines() if marker in line]


def _closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# ===========================================================================
# What it posts (Q6.4 a)
# ===========================================================================

class TestPayload:
    def test_q4a_a_text(self, make, rx, clock):
        bot = make()
        sent = bot.send_text("<b>Good</b> morning &amp; welcome")
        (payload,) = rx.payloads()
        assert sent == {"bot": payload["id"]} and len(payload["id"]) == 32
        assert payload == {
            "id": payload["id"], "kind": "text", "symbol": None,
            "time": int(at(14, 0, 1).timestamp() * 1000), "source": None,
            "text": "Good morning & welcome", "html": "<b>Good</b> morning &amp; welcome",
            "silent": False, "fields": {}, "late_seconds": 0,
        }

    def test_q4a_a_trades_messages_carry_its_raw_values(self, make, rx, clock):
        bot = make()
        broker = SteppedBroker(SEED, LIVE, clock=clock, bots=[bot])
        run_live(strategy=Plan(LONG), broker=broker, config=paper_cfg([bot]))
        by_kind = {payload["kind"]: payload for payload in rx.payloads()}
        assert list(by_kind) == ["start", "signal", "open", "close", "stop"]
        closed = by_kind["close"]
        assert (closed["symbol"], closed["source"]) == ("BTCUSDT", "SIM")
        fields = closed["fields"]
        assert {key: fields[key] for key in ("trade_id", "exit_price", "reason", "net_pnl",
                                             "pnl_r", "direction", "duration_ms")} == {
            "trade_id": 0, "exit_price": 65150.0, "reason": "tp", "net_pnl": 200.0,
            "pnl_r": 2.0, "direction": "long", "duration_ms": 3_600_000}
        assert fields["candle_time"] == ms(at(14))
        assert "trade" not in fields                      # objects are left out
        opened = by_kind["open"]["fields"]
        assert opened["signal_time"] == ms(at(13)) and opened["risk_amount"] == 100.0
        assert round(opened["size"], 4) == 0.2222
        assert by_kind["start"]["fields"]["symbols"] == ["BTCUSDT"]
        stop = by_kind["stop"]["fields"]
        assert (stop["reason"], stop["open_positions"]) == ("feeds ended", 0)
        assert by_kind["close"]["text"].startswith(
            "🏁 CLOSED #0 · LONG BTCUSDT @ 65,150 — take profit\nNet +$200.00 (+2.00 R)")

    def test_an_alert_posts_what_fired(self, make, rx):
        bot = make()
        venue = FakeVenue(64_900.0)
        bot.watch(venue, "BTCUSDT", above=65_000)
        wait_for(lambda: venue.streams)
        venue.streams[0].push(65_010.0)
        wait_for(lambda: rx.payloads())
        (payload,) = rx.payloads()
        assert (payload["kind"], payload["source"]) == ("alert", "fake-venue")
        assert {key: payload["fields"][key] for key in ("level", "side", "price")} == {
            "level": 65000.0, "side": "above", "price": 65010.0}


# ===========================================================================
# Signatures, pictures, headers
# ===========================================================================

class TestSigned:
    def test_the_signature_is_the_hmac_of_timestamp_and_body(self, make, rx, clock):
        bot = make(secret=SECRET)
        bot.send_text("signed")
        (got,) = rx.got
        stamp = got["headers"]["x-algotradekit-timestamp"]
        assert stamp == str(int(clock.time()))
        expected = hmac.new(SECRET.encode(), stamp.encode() + b"." + got["signed"],
                            hashlib.sha256).hexdigest()
        assert got["headers"]["x-algotradekit-signature"] == f"sha256={expected}"

    def test_unsigned_without_a_secret(self, make, rx):
        make().send_text("plain")
        assert "x-algotradekit-signature" not in rx.got[0]["headers"]

    def test_a_picture_is_a_part_beside_the_json(self, make, rx):
        bot = make(secret=SECRET)
        sent = bot.send_photo(PICTURE, "<b>BTCUSDT</b> · 15m")
        (got,) = rx.got
        assert got["payload"]["text"] == "BTCUSDT · 15m" and sent == {"bot": got["payload"]["id"]}
        assert got["files"]["file"]["data"] == PICTURE
        assert got["files"]["file"]["type"] == "image/png"
        stamp = got["headers"]["x-algotradekit-timestamp"]
        expected = hmac.new(SECRET.encode(), stamp.encode() + b"." + got["signed"],
                            hashlib.sha256).hexdigest()
        assert got["headers"]["x-algotradekit-signature"] == f"sha256={expected}"

    def test_a_targets_own_headers(self, make, rx):
        bot = make({"bot": WebhookTarget(rx.hook(), headers={"Authorization": "Bearer t0k"})})
        bot.send_text("with a key")
        assert rx.got[0]["headers"]["authorization"] == "Bearer t0k"


# ===========================================================================
# Answers, retries and lines
# ===========================================================================

class TestAnswers:
    def test_a_retry_carries_the_same_id(self, make, rx):
        bot = make()
        rx.when("bot", status=500, description="busy", times=1)
        sent = bot.send_text("twice tried")
        assert len(rx.calls) == 2
        assert rx.payloads()[0]["id"] == sent["bot"]

    def test_a_429_waits_its_retry_after(self, make, rx):
        clock = FakeClock(at(14))
        bot = make(on=clock)
        rx.when("bot", status=429, retry_after=3, times=1)
        bot.send_text("later", wait=False)
        settle(bot)
        assert rx.payloads() == []
        clock.advance(2.0)
        settle(bot)
        assert rx.payloads() == []
        clock.advance(1.0)
        settle(bot)
        assert [payload["text"] for payload in rx.payloads()] == ["later"]

    def test_a_refusal_is_one_line_with_the_host_only(self, make, rx, capsys):
        bot = make({"bot": f"{rx.hook()}?key=s3cret-in-url"})
        rx.when("bot", status=400, description="nope", times=1)
        bot.send_text("refused", wait=False)
        drained(bot)
        host = rx.url.split("//", 1)[1]
        assert lines(capsys.readouterr().out) == [
            f"could not send to bot ({host}) — 400 Bad Request (0 messages waiting)"]

    def test_one_target_down_never_holds_another(self, make, rx, capsys):
        dead = f"http://127.0.0.1:{_closed_port()}/hook/dead"
        bot = make({"dead": dead, "bot": rx.hook()})
        bot.send_text("to both", wait=False)
        wait_for(lambda: rx.payloads())
        assert [payload["text"] for payload in rx.payloads()] == ["to both"]
        wait_for(lambda: any("could not send to dead" in line
                             for line in lines(capsys.readouterr().out)))

    def test_what_a_target_is_given_as(self, rx):
        one = WebhookNotifier(rx.hook())
        host = rx.url.split("//", 1)[1]
        assert list(one._destinations) == [host]
        assert repr(WebhookTarget(f"{rx.hook()}?key=x")) == f"WebhookTarget({host})"
        with pytest.raises(ValueError, match="must start with https:// or http://"):
            WebhookNotifier("ftp://example.com/x")
        with pytest.raises(ValueError, match="targets is empty"):
            WebhookNotifier({})
        with pytest.raises(ValueError, match="secret must be a non-empty string"):
            WebhookNotifier(rx.hook(), secret="")
        with pytest.raises(TypeError, match="headers must be a dict of strings"):
            WebhookTarget(rx.hook(), headers={"X": 1})


# ===========================================================================
# Trading never waits
# ===========================================================================

class TestTradingNeverWaits:
    """With a receiver that takes 5 seconds per request, a candle is processed within 50 ms
    of the time it takes with no notifier (Part 2's bound)."""

    BOUND = 0.050

    @pytest.mark.filterwarnings("ignore:.*silent paper run")
    def test_run_live_candle(self, make, rx):
        bot = make(on=FakeClock(at(14)), timeout=10.0)
        rx.when("bot", delay=5.0)
        cost = {}
        for label, bots in (("bare", []), ("webhook", [bot])):
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
        assert cost["webhook"] - cost["bare"] < self.BOUND, cost
        rx.clear_rules()
