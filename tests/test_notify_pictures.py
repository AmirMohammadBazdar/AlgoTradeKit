"""
Tests for v1.2.0 Part 3 — chart pictures through the notifier, with the engine faked.

No browser starts here: ``visual._snapshot``'s engine is replaced by a fake that
returns a real (small) PNG and records what it was asked to photograph.  The Bot
API is Part 1's fake, taught ``sendPhoto``, ``sendDocument`` and
``editMessageCaption``, with the multipart uploads read by the standard library.
These tests never skip; ``tests/test_snapshot.py`` runs the real browser.
"""
from __future__ import annotations

import importlib.util
import json
import struct
import threading
import time
import zlib
from email.parser import BytesParser
from email.policy import HTTP
from urllib.parse import parse_qs, urlsplit

import pandas as pd
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from test_notify import ME, TOKEN, FakeClock
from test_notify_events import (
    HOUR,
    LIVE,
    LONG,
    MINUTE,
    SEED,
    EditingTelegram,
    FastClock,
    Plan,
    SteppedBroker,
    at,
    candle,
    drained,
    ms,
    paper,
    paper_cfg,
    trailing_day,
)
from test_trader import _PaperBroker

from AlgoTradeKit.notify import TelegramChat, TelegramError, TelegramNotifier, _core, _template
from AlgoTradeKit.simulate import Simulate, SimulateConfig
from AlgoTradeKit.strategy import BaseStrategy, Signal
from AlgoTradeKit.trader import EventStream, OpenEvent, TraderPair, run_live
from AlgoTradeKit.trader import _notify as adapter_module
from AlgoTradeKit.trader import _run_live as run_live_module
from AlgoTradeKit.visual import Chart, _snapshot
from AlgoTradeKit.visual import indicator_renderer as renderer_module

NOT_MODIFIED = ("Bad Request: message is not modified: specified new message content and "
                "reply markup are exactly the same as a current content and reply markup "
                "of the message")
T0800, T1400 = 1791964800, 1791986400      # 2026-10-14 08:00 and 14:00 UTC, in seconds


# ---------------------------------------------------------------------------
# A real PNG, and the fake engine
# ---------------------------------------------------------------------------

def png(width: int = 1280, height: int = 720) -> bytes:
    """A real PNG of one colour, small once compressed."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + b"\x10\x20\x30" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


PICTURE = png()


class FakeEngine:
    """Stands in for ``visual._snapshot``: what it was asked, and a PNG back."""

    def __init__(self) -> None:
        self.asked: list[dict] = []
        self.fail: Exception | None = None
        self.delay = 0.0

    def snapshot(self, url, width=1280, height=720, timeout=20.0, **options):
        query = {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}
        self.asked.append({"url": url, "query": query, "width": width, "height": height,
                           "thread": threading.current_thread().name, **options})
        if self.delay:
            time.sleep(self.delay)
        if self.fail is not None:
            raise self.fail
        return PICTURE


@pytest.fixture
def engine(monkeypatch):
    fake = FakeEngine()
    monkeypatch.setattr(_snapshot, "snapshot", fake.snapshot)
    monkeypatch.setattr(_snapshot, "check_engine", lambda: None)
    return fake


# ---------------------------------------------------------------------------
# The fake Telegram, taught to take pictures and files
# ---------------------------------------------------------------------------

def _form(content_type: str, raw: bytes) -> tuple[dict, dict]:
    """A multipart body's fields and files, read as Telegram reads them."""
    message = BytesParser(policy=HTTP).parsebytes(
        b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + raw)
    params: dict = {}
    files: dict = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        payload = part.get_payload(decode=True)
        if part.get_filename():
            files[name] = {"filename": part.get_filename(), "data": payload,
                           "type": part.get_content_type()}
            continue
        value = payload.decode()
        if name == "reply_parameters":
            value = json.loads(value)
        elif name in ("chat_id", "message_thread_id") and value.lstrip("-").isdigit():
            value = int(value)
        elif value in ("true", "false"):
            value = value == "true"
        params[name] = value
    return params, files


class PictureTelegram(EditingTelegram):
    """The fake Bot API, with ``sendPhoto``, ``sendDocument`` and
    ``editMessageCaption``.  A picture or a file is in ``answered`` like a text,
    its caption as its ``text``, with ``media`` saying which."""

    def reset(self) -> None:
        super().reset()
        self.captions: dict[tuple, str] = {}     # (chat_id, message_id) -> caption now

    def _build(self) -> FastAPI:
        app = FastAPI()

        @app.api_route("/bot{token}/{method}", methods=["GET", "POST"])
        async def bot_api(token: str, method: str, request: Request):
            raw = await request.body()
            kind = request.headers.get("content-type", "")
            files: dict = {}
            if kind.startswith("multipart/form-data"):
                params, files = _form(kind, raw)
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
                status, body = self._answer(token, method, dict(params, _files=files), rule)
            if isinstance(body, str):
                return PlainTextResponse(body, status_code=status)
            return JSONResponse(body, status_code=status)

        return app

    def _answer(self, token, method, params, rule):
        files = params.pop("_files", {})
        free = rule is None or (rule["status"] == 200 and rule["text"] is None)
        if method in ("sendPhoto", "sendDocument") and token == TOKEN and free:
            chat = self.chats.get(params.get("chat_id"))
            if chat is None:
                return 400, {"ok": False, "error_code": 400,
                             "description": "Bad Request: chat not found"}
            upload = files.get("photo" if method == "sendPhoto" else "document")
            if upload is None:
                return 400, {"ok": False, "error_code": 400,
                             "description": "Bad Request: there is no file in the request"}
            message_id = self.next_id
            self.next_id += 1
            caption = params.get("caption")
            self.answered.append({"chat_id": params["chat_id"], "text": caption,
                                  "params": params, "at": self._now(), "message_id": message_id,
                                  "media": method, "file": upload})
            self.captions[(params["chat_id"], message_id)] = caption
            return 200, {"ok": True, "result": {"message_id": message_id, "chat": chat}}
        if method == "editMessageCaption" and token == TOKEN and free:
            key = (params.get("chat_id"), params.get("message_id"))
            if key not in self.captions:
                return 400, {"ok": False, "error_code": 400,
                             "description": "Bad Request: message to edit not found"}
            if self.captions[key] == params.get("caption"):
                return 400, {"ok": False, "error_code": 400, "description": NOT_MODIFIED}
            self.captions[key] = params.get("caption")
            self.edits.append({"chat_id": key[0], "message_id": key[1],
                               "text": params.get("caption"), "params": params,
                               "at": self._now(), "caption": True})
            return 200, {"ok": True, "result": {"message_id": key[1]}}
        return super()._answer(token, method, params, rule)

    def pictures(self, chat_id) -> list[dict]:
        return [item for item in self.got(chat_id) if item.get("media") == "sendPhoto"]


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
    fake = PictureTelegram()
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
    """Notifiers on the fake, on the fast clock; every one is closed after the test."""
    made: list[TelegramNotifier] = []

    def build(chats=None, *, on=None, **kwargs) -> TelegramNotifier:
        used = clock if on is None else on
        kwargs.setdefault("base_url", tg.url)
        kwargs.setdefault("timeout", 2.0)
        monkeypatch.setattr(TelegramNotifier, "_clock", used)
        tg.clock = used
        bot = TelegramNotifier(TOKEN, {"me": ME} if chats is None else chats, **kwargs)
        bot._clock = used
        if isinstance(used, FastClock):
            used.jump_for.add(bot._cond)
        made.append(bot)
        return bot

    yield build
    for bot in made:
        bot.close(timeout=0.5)


def quarter_hours(start: str = "2026-10-14 00:00", count: int = 96) -> pd.DataFrame:
    """BTCUSDT 15m candles, rising one dollar a candle."""
    first = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    rows = [{"timestamp": first + k * 900_000, "open": 64_000 + k, "high": 64_010 + k,
             "low": 63_990 + k, "close": 64_005 + k, "volume": 1.0} for k in range(count)]
    return pd.DataFrame(rows)


class CandleBroker:
    """What ``send_chart(broker=…)`` needs: ``get_candles_df``, and who asked."""

    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = frame
        self.asked: list[tuple] = []

    def get_candles_df(self, symbol, timeframe, start_ms, end_ms):
        self.asked.append((symbol, timeframe, start_ms, end_ms,
                           threading.current_thread().name))
        if symbol != "BTCUSDT":
            raise ValueError("Binance rejected the request (400): code=-1121 msg=Invalid symbol.")
        stamps = self.frame["timestamp"]
        return self.frame[(stamps >= start_ms) & (stamps <= end_ms)].reset_index(drop=True)


# ===========================================================================
# send_photo — a picture you made
# ===========================================================================

class TestSendPhoto:
    def test_a_picture_and_its_caption(self, make, tg):
        bot = make()
        sent = bot.send_photo(PICTURE, "<b>BTC</b> at {time}")
        (picture,) = tg.pictures(ME)
        assert sent == {"me": picture["message_id"]}
        assert picture["text"] == "<b>BTC</b> at 2026-10-14 14:00 UTC"
        assert picture["params"]["parse_mode"] == "HTML"
        assert picture["file"]["data"] == PICTURE
        assert (picture["file"]["filename"], picture["file"]["type"]) == ("picture.png",
                                                                         "image/png")

    def test_a_1500_character_caption_goes_as_a_reply_to_the_picture(self, make, tg):
        bot = make()
        caption = "\n".join(f"line {n:03d} " + "·" * 30 for n in range(1, 40))
        assert 1_400 < len(caption) < 1_600
        sent = bot.send_photo(PICTURE, caption)
        picture, reply = tg.got(ME)
        assert picture["media"] == "sendPhoto" and picture["text"] is None
        assert "caption" not in picture["params"]
        assert reply["text"] == caption
        assert reply["params"]["reply_parameters"]["message_id"] == picture["message_id"]
        assert sent == {"me": picture["message_id"]}

    def test_the_text_wrapper_wraps_a_caption_and_a_bare_picture_stays_bare(self, make, tg):
        bot = make(templates={"text": "<i>ATK</i> · {text}"})
        bot.send_photo(PICTURE, "first")
        bot.send_photo(PICTURE)
        first, bare = tg.pictures(ME)
        assert first["text"] == "<i>ATK</i> · first"
        assert bare["text"] is None

    def test_text_none_sends_no_picture_either(self, make, tg):
        bot = make(templates={"text": None})
        assert bot.send_photo(PICTURE, "x") == {}
        assert tg.got(ME) == []

    def test_placeholders_as_in_a_text(self, make, tg):
        bot = make()
        with pytest.raises(ValueError, match=r"\{symbol\} needs symbol="):
            bot.send_photo(PICTURE, "{symbol}")
        bot.send_photo(PICTURE, "{symbol} · {date}", symbol="BTCUSDT")
        assert tg.pictures(ME)[0]["text"] == "BTCUSDT · 2026-10-14"

    def test_what_a_picture_must_be(self, make):
        bot = make()
        with pytest.raises(TypeError, match="not a path"):
            bot.send_photo("chart.png")
        with pytest.raises(TypeError, match="its bytes"):
            bot.send_photo(12)
        with pytest.raises(ValueError, match="the picture is empty"):
            bot.send_photo(b"")
        with pytest.raises(ValueError, match="at most 10 MB"):
            bot.send_photo(b"\x00" * (10 * 1024 * 1024 + 1))
        with pytest.raises(ValueError, match="width \\+ height of at most 10,000"):
            bot.send_photo(png(9_000, 2_000))
        with pytest.raises(ValueError, match="20 times the other"):
            bot.send_photo(png(4_000, 100))

    def test_silent_to_and_wait(self, make, tg):
        bot = make({"me": ME, "loud": TelegramChat(ME, events={"signal"})})
        assert bot.send_photo(PICTURE, "q", silent=True, wait=False) == {"me": None}
        drained(bot)
        (picture,) = tg.pictures(ME)
        assert picture["params"]["disable_notification"] is True
        bot.send_photo(PICTURE, to="loud")
        assert len(tg.pictures(ME)) == 2


# ===========================================================================
# send_chart — a chart, or a broker's candles between two datetimes
# ===========================================================================

class TestSendChart:
    def test_q32_a_chart_between_two_datetimes(self, make, tg, engine):
        chart = Chart(title="BTCUSDT")
        chart.set_data(quarter_hours())
        bot = make()
        sent = bot.send_chart(chart, "2026-10-14 08:00", "2026-10-14 14:00", caption="{date}")
        (asked,) = engine.asked
        assert asked["query"] == {
            "snapshot": "1", "from": str(T0800), "to": str(T1400), "tz": "UTC",
            "title": "BTCUSDT · 15m · 2026-10-14 08:00 – 14:00 UTC",
        }
        assert asked["thread"].startswith("atk-notify")     # never the caller's
        (picture,) = tg.pictures(ME)
        assert sent == {"me": picture["message_id"]} and picture["text"] == "2026-10-14"
        assert chart._shown is False                          # served for it, then stopped

    def test_the_picture_reads_in_the_notifiers_zone(self, make, engine):
        chart = Chart(title="BTCUSDT")
        chart.set_data(quarter_hours())
        bot = make(timezone="Asia/Tehran")
        bot.send_chart(chart, "2026-10-14 08:00", "2026-10-14 14:00")
        query = engine.asked[0]["query"]
        assert query["tz"] == "Asia/Tehran"
        assert query["title"] == "BTCUSDT · 15m · 2026-10-14 11:30 – 17:30 (UTC+03:30)"

    def test_q36_the_broker_form_fetches_on_the_callers_thread(self, make, tg, engine):
        broker = CandleBroker(quarter_hours())
        bot = make()
        bot.send_chart(broker=broker, symbol="BTCUSDT", timeframe="15m",
                       start="2026-10-14 08:00", end="2026-10-14 14:00")
        (asked,) = broker.asked
        assert asked[:4] == ("BTCUSDT", "15m", T0800 * 1000, T1400 * 1000)
        assert asked[4] == threading.current_thread().name
        query = engine.asked[0]["query"]
        assert (query["from"], query["to"]) == (str(T0800), str(T1400))
        assert query["title"] == "BTCUSDT · 15m · 2026-10-14 08:00 – 14:00 UTC"
        assert len(tg.pictures(ME)) == 1

    def test_q36_a_wrong_symbol_raises_at_the_call_even_without_waiting(self, make, tg, engine):
        bot = make()
        with pytest.raises(ValueError, match="Invalid symbol"):
            bot.send_chart(broker=CandleBroker(quarter_hours()), symbol="BTCUSTD",
                           timeframe="15m", start="2026-10-14 08:00", wait=False)
        drained(bot)
        assert tg.got(ME) == [] and engine.asked == []

    def test_the_broker_form_defaults_to_the_last_100_candles(self, make, clock, engine):
        clock.set(at(23, 50))
        broker = CandleBroker(quarter_hours())
        make().send_chart(broker=broker, symbol="BTCUSDT", timeframe="15m")
        start, end = broker.asked[0][2:4]
        assert end == ms(at(23, 50)) and start == end - 99 * 900_000
        query = engine.asked[0]["query"]
        assert int(query["to"]) == ms(at(23, 45)) // 1000                 # the newest candle

    def test_indicators_get_their_warm_up_before_the_window(self, make, engine):
        broker = CandleBroker(quarter_hours("2026-10-12 00:00", 288))
        make().send_chart(broker=broker, symbol="BTCUSDT", timeframe="15m",
                          start="2026-10-14 08:00", end="2026-10-14 14:00",
                          indicators=[{"kind": "ema", "period": 20}])
        assert broker.asked[0][2] == T0800 * 1000 - 200 * 900_000
        assert engine.asked[0]["query"]["from"] == str(T0800)

    def test_mistakes_raise_at_the_call(self, make, engine):
        bot = make()
        broker = CandleBroker(quarter_hours())
        with pytest.raises(ValueError, match=r"unknown indicator kind 'emma' — known: sma, ema"):
            bot.send_chart(broker=broker, symbol="BTCUSDT", timeframe="15m",
                           indicators=[{"kind": "emma"}])
        with pytest.raises(ValueError, match="'1M' has no fixed length"):
            bot.send_chart(broker=broker, symbol="BTCUSDT", timeframe="1M")
        with pytest.raises(TypeError, match="needs a chart, or broker="):
            bot.send_chart(symbol="BTCUSDT")
        with pytest.raises(TypeError, match="for a picture from a broker"):
            bot.send_chart(Chart(), indicators=[{"kind": "ema"}])
        with pytest.raises(ValueError, match="no candle of BTCUSDT 15m between"):
            bot.send_chart(broker=broker, symbol="BTCUSDT", timeframe="15m",
                           start="2027-01-01 00:00", end="2027-01-01 04:00")
        with pytest.raises(ValueError, match="Cannot parse candle_range"):
            bot.send_chart(Chart(), "yesterday")
        assert engine.asked == []

    def test_a_picture_that_cannot_be_made_is_not_sent(self, make, tg, engine, capsys):
        chart = Chart(title="BTCUSDT")
        chart.set_data(quarter_hours())
        engine.fail = _snapshot.SnapshotError("the chart page sent no data in 20 s")
        bot = make()
        with pytest.raises(TelegramError,
                           match=r"could not make the picture \(the chart page sent no data"):
            bot.send_chart(chart)
        bot.send_chart(chart, wait=False)
        drained(bot)
        assert tg.got(ME) == []
        assert "could not make the picture (the chart page sent no data in 20 s)" in (
            capsys.readouterr().out)


# ===========================================================================
# Without the engine (Q3.7 a)
# ===========================================================================

def _without_playwright(monkeypatch):
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a: None if name == "playwright" else real(name, *a))


class TestWithoutTheEngine:
    def test_no_playwright_raises_the_decided_sentence(self, make, monkeypatch):
        _without_playwright(monkeypatch)
        sentence = ('chart pictures need the [notify] extra: pip install "AlgoTradeKit[notify]", '
                    "then: playwright install chromium")
        with pytest.raises(ImportError) as raised:
            make(images={"open"})
        assert str(raised.value) == sentence
        bot = make(images={"open"}, check=False)    # check=False skips it, as the token's
        chart = Chart()
        chart.set_data(quarter_hours())
        with pytest.raises(ImportError, match=r"need the \[notify\] extra"):
            bot.send_chart(chart)
        with pytest.raises(ImportError, match=r"need the \[notify\] extra"):
            chart.screenshot()
        assert make().send_text("text needs nothing") == {"me": 1}

    def test_no_browser_raises_the_decided_sentence(self, make, monkeypatch):
        class Chromium:
            executable_path = "/nowhere/chromium-1243/chrome-linux64/chrome"

        class Driver:
            chromium = Chromium()

        monkeypatch.setattr(_snapshot._engine, "_found_browser", False)
        monkeypatch.setattr(_snapshot._engine, "run",
                            lambda work, timeout: work(Driver(), lambda: None))
        monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: object())
        with pytest.raises(_snapshot.SnapshotError) as raised:
            make(images={"close"})
        assert str(raised.value) == (
            "Chromium is not installed for Playwright — run: playwright install chromium "
            "(on Linux: playwright install --with-deps chromium)")


# ===========================================================================
# images= (Q3.3 b, Q3.5 a)
# ===========================================================================

class TestTheImagesOption:
    def test_kinds_are_checked(self, make, engine):
        with pytest.raises(ValueError, match="unknown kind 'opne'"):
            make(images={"opne"})
        with pytest.raises(TypeError, match="a set of kinds"):
            make(images="open")

    @pytest.mark.parametrize("kind", ["start", "stop", "text"])
    def test_q35_a_kind_that_names_no_pair_is_refused(self, make, engine, kind):
        with pytest.raises(ValueError) as raised:
            make(images={"open", kind})
        assert str(raised.value) == (
            f"images: {kind!r} names no pair — a picture shows one pair's candles; pictures "
            "go on signal, exit_signal, open, sl_move, risk_free, tp_level, close, daily_loss, "
            "reconcile, error, message, daily_summary")


# ===========================================================================
# Pictures on trade messages
# ===========================================================================

#: Twelve trades in the seed — #0 to #11 — so the live one is #12: a long on every
#: other hour, taking its profit on the next.  Then 110 quiet hours, so a picture
#: has its 100 candles and their warm-up.
TWELVE = {ms(at(0)) + 2 * k * HOUR: [("long", 64_250.0, 63_800.0, 64_255.0)] for k in range(12)}


def _seed_rows(hours: int) -> list[dict]:
    rows = []
    for k in range(hours):
        stamp = ms(at(0)) + k * HOUR
        high = 64_260 if k % 2 and k < 24 else 64_252
        rows.append({"timestamp": stamp, "open": 64_250, "high": high, "low": 64_240,
                     "close": 64_250, "volume": 1.0})
    return rows


def run_twelve(bots, clock, *, live_hours=2, signals=None, **overrides):
    """The seed's twelve trades, then the live long: #12 opens on the first live
    candle and takes its profit on the second."""
    seed = _seed_rows(134)
    first_live = ms(at(0)) + 134 * HOUR
    live = [{"timestamp": first_live, "open": 64_250, "high": 64_260, "low": 64_240,
             "close": 64_250, "volume": 1.0},
            {"timestamp": first_live + HOUR, "open": 64_250, "high": 65_200, "low": 64_240,
             "close": 65_100, "volume": 1.0}][:live_hours]
    plan = dict(TWELVE)
    plan[first_live] = [("long", 64_250.0, 63_800.0, 65_150.0)]
    plan.update(signals or {})
    clock.set(pd.Timestamp(first_live, unit="ms", tz="UTC").to_pydatetime())
    broker = SteppedBroker(seed, live, clock=clock, bots=bots)
    strategy = Plan(plan)
    config = paper_cfg(bots, min_candles=134, **overrides)
    return run_live(strategy=strategy, broker=broker, config=config)


class TestTradePictures:
    def test_q33_trade_12s_open_is_a_picture_whose_caption_is_its_open_text(
            self, make, tg, engine):
        first = FastClock(at(14, 0, 1))
        plain = make({"me": TelegramChat(ME, events={"open"})}, on=first)
        run_twelve([plain], first, live_hours=1)
        (opened_text,) = tg.texts(ME)
        assert opened_text.startswith("<b>✅ OPENED #12 · LONG BTCUSDT @ 64,250")
        tg.reset()

        again = FastClock(at(14, 0, 1))
        bot = make({"me": TelegramChat(ME, events={"open"})}, on=again, images={"open"})
        run_twelve([bot], again, live_hours=1)
        (picture,) = tg.got(ME)
        assert picture["media"] == "sendPhoto" and picture["text"] == opened_text
        query = engine.asked[0]["query"]
        candles = (int(query["to"]) - int(query["from"])) // 3600 + 1
        assert candles == 100
        assert query["title"].startswith("BTCUSDT · 1h · ")

    def test_the_open_and_the_signal_draw_their_position(self, make, tg, engine, clock,
                                                         monkeypatch):
        drawn: list[dict] = []
        real = renderer_module.populate_chart

        def recording(chart, data, **options):
            drawn.append({"rows": len(data), **options})
            return real(chart, data, **options)

        monkeypatch.setattr(renderer_module, "populate_chart", recording)
        bot = make(images={"signal", "open"})
        paper([bot], clock, live=LIVE[:1])
        signal, opened = drawn
        (proposed,) = signal["open_positions"]
        assert (proposed.entry_price, proposed.stop_loss, proposed.next_tp,
                proposed.direction) == (64_250.0, 63_800.0, 65_150.0, "long")
        (position,) = opened["open_positions"]
        assert (position.trade_id, position.entry_price, position.stop_loss,
                position.next_tp) == (0, 64_250.0, 63_800.0, 65_150.0)
        assert [item["media"] for item in tg.got(ME)[1:3]] == ["sendPhoto", "sendPhoto"]

    def test_the_close_shows_the_trade_and_replies_to_the_picture(self, make, tg, engine,
                                                                   clock, monkeypatch):
        drawn: list[dict] = []
        real = renderer_module.populate_chart

        def recording(chart, data, **options):
            drawn.append(options)
            return real(chart, data, **options)

        monkeypatch.setattr(renderer_module, "populate_chart", recording)
        bot = make({"me": TelegramChat(ME, events={"open", "close"})},
                   images={"open", "close"})
        paper([bot], clock)
        opened, closed = tg.got(ME)
        assert closed["media"] == "sendPhoto"
        assert closed["text"].startswith("<b>🏁 CLOSED #0 · LONG BTCUSDT @ 65,150")
        assert closed["params"]["reply_parameters"]["message_id"] == opened["message_id"]
        markers = drawn[1]["trade_markers"]
        assert {m["trade_id"] for m in markers} == {0} and drawn[1]["open_positions"] == []
        query = engine.asked[1]["query"]
        assert int(query["to"]) == ms(at(14)) // 1000              # the exit candle
        # 20 candles before the 13:00 entry is before the first candle the session
        # keeps (min_candles=5: 08:00), so the picture starts there.
        assert int(query["from"]) == ms(at(8)) // 1000

    def test_e5_an_sl_move_edits_the_pictures_caption(self, make, tg, engine, clock):
        bot = make({"all": TelegramChat(ME, events=set(_core.KINDS) - {"text"})},
                   images={"open"})
        seed, live = trailing_day()
        broker = SteppedBroker(seed, live[:3], clock=clock, bots=[bot], tf_ms=MINUTE)
        strategy = Plan({ms(at(14)): [("long", 64_250.0, 63_800.0, None)]}, timeframe="1m")
        run_live(strategy=strategy, broker=broker,
                 config=paper_cfg([bot], tp_mode="none", sl_mode="trailing",
                                  trailing_sl_percent=0.5))
        (picture,) = tg.pictures(ME)
        edits = tg.edits_of(ME)
        assert edits and all(edit.get("caption") for edit in edits)
        assert {edit["message_id"] for edit in edits} == {picture["message_id"]}
        assert "SL 63,800 → 63,948.65 (trailing) · " in edits[-1]["text"]
        assert edits[-1]["text"].startswith("<b>✅ OPENED #0 · LONG BTCUSDT @ 64,250")

    def test_a_picture_that_cannot_be_made_sends_the_text(self, make, tg, engine, clock,
                                                          capsys):
        engine.fail = _snapshot.SnapshotError(_snapshot.NO_UNPKG)
        bot = make({"me": TelegramChat(ME, events={"open"})}, images={"open"})
        paper([bot], clock, live=LIVE[:1])
        (opened,) = tg.got(ME)
        assert "media" not in opened and opened["text"].startswith("<b>✅ OPENED #0")
        out = capsys.readouterr().out
        assert ("[AlgoTradeKit] telegram: no picture for open #0 — the chart page could not "
                "load lightweight-charts from unpkg.com — this machine must reach it to make "
                "pictures; sent as text") in out

    def test_the_candles_are_a_copy_taken_on_the_trading_thread(self, make, engine, clock,
                                                                monkeypatch):
        seen: list[tuple] = []
        real = adapter_module._Adapter._capture

        def watched(adapter, event):
            scene = real(adapter, event)
            seen.append((threading.current_thread().name, scene))
            return scene

        monkeypatch.setattr(adapter_module._Adapter, "_capture", watched)
        bot = make({"me": TelegramChat(ME, events={"open"})}, images={"open"})
        paper([bot], clock, live=LIVE[:1])
        ((thread, scene),) = seen
        assert not thread.startswith("atk-notify")
        assert list(scene.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
        assert scene.columns["timestamp"][-1] == ms(at(13))      # up to the event's candle
        assert scene.frame()["close"].iloc[-1] == 64_250

    def test_a_bare_stream_has_no_pictures(self, make, tg, engine):
        bot = make({"me": TelegramChat(ME, events={"open"})}, images={"open"})
        stream = EventStream()
        stream.subscribe(bot)
        stream.emit(OpenEvent(time=ms(at(13)), symbol="BTCUSDT", source="sim", trade_id=3,
                              direction="long", fill_price=64_250.0, size=0.2222,
                              margin_amount=1_428.0, risk_amount=100.0, stop_loss=63_800.0,
                              next_tp=65_150.0))
        drained(bot)
        (opened,) = tg.got(ME)
        assert "media" not in opened and engine.asked == []

    def test_the_daily_summary_carries_a_picture(self, make, tg, engine, clock):
        bot = make({"me": TelegramChat(ME, events={"daily_summary"})},
                   images={"daily_summary"})

        def midnight(index, row):
            clock.set(at(24, 0, 5))
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                drained(bot)
                if tg.got(ME):
                    break
                time.sleep(0.01)

        paper([bot], clock, live=LIVE[:1], after=midnight)
        (summary,) = tg.got(ME)
        assert summary["media"] == "sendPhoto"
        assert summary["text"].startswith("<b>📊 DAY 2026-10-14 · BTCUSDT</b>")


# ===========================================================================
# Trading never waits on a picture
# ===========================================================================

class TestTradingNeverWaits:
    """With pictures that take 5 seconds to make, a candle is processed within
    50 ms of the time it takes with no notifier."""

    BOUND = 0.050

    @pytest.mark.filterwarnings("ignore:.*silent paper run")
    def test_run_live_candle(self, make, engine):
        engine.delay = 5.0
        bot = make(check=False, on=FakeClock(at(14)), timeout=10.0, images={"signal", "open"})
        cost = {}
        for label, bots in (("bare", []), ("notified", [bot])):
            session = run_live_module._build_session(
                TraderPair(broker=_PaperBroker(SEED), config=paper_cfg(bots),
                           strategy=Plan(LONG)),
                adapter_module.NotifySession("paper"))
            run_live_module._attach_sim(session, 10_000.0)
            session.sim.seed()
            session.bridge.prime()
            hours = iter(range(13, 40))
            best = float("inf")
            for _ in range(3):
                h = next(hours)
                session.pair.strategy.signals[ms(at(h))] = [("long", 64_250.0, 63_800.0,
                                                             65_150.0)]
                started = time.perf_counter()
                session.sim.process_closed_candle(candle(at(h), 64_250, 65_200, 64_240, 65_100))
                best = min(best, time.perf_counter() - started)
            cost[label] = best
        assert cost["notified"] - cost["bare"] < self.BOUND, cost


# ===========================================================================
# E4 — a backtest's report
# ===========================================================================

class _Swings(BaseStrategy):
    """A long every 10 candles on a saw-tooth: a backtest that wins and loses."""

    primary_timeframe = "1h"
    warmup_period = 0

    def prepare_indicators(self, data):
        return data

    def generate_signals(self, candle_index, data):
        if candle_index % 10 or candle_index < 1:
            return []
        row = data["1h"].iloc[candle_index]
        return [Signal(direction="long", entry_price=float(row.close),
                       stop_loss=float(row.close) - 300, take_profit=float(row.close) + 400,
                       timestamp=int(row.timestamp), candle_index=candle_index,
                       timeframe="1h")]


def backtest(rows: int = 200):
    """Up 60 a candle for 20 candles, then down 60 for 20, and again."""
    closes, price = [], 64_000.0
    for k in range(rows):
        price += 60.0 if (k // 20) % 2 == 0 else -60.0
        closes.append(price)
    frame = pd.DataFrame([
        {"timestamp": ms(at(0)) + k * HOUR, "open": close - 10, "high": close + 40,
         "low": close - 40, "close": close, "volume": 1.0} for k, close in enumerate(closes)])
    result = _Swings().run({"1h": frame})
    return Simulate(SimulateConfig(symbol="BTCUSDT", leverage=10.0)).run(result)


class TestSendReport:
    def test_e4_three_messages_the_text_the_balance_picture_and_the_file(self, make, tg,
                                                                           engine):
        report = backtest()
        assert report.total_trades > 0
        bot = make()
        sent = bot.send_report(report)
        text, picture, document = tg.got(ME)
        assert sent == {"me": text["message_id"]}
        head, figures, clock_line = text["text"].splitlines()
        assert head == "<b>📊 BTCUSDT · Simulate report</b>"
        expected = [
            f"Net {_template.Pnl(report.total_pnl)} ({report.total_pnl_percent:+.2f}%)",
            f"{report.total_trades} trades", f"win rate {report.win_rate:.1f}%",
            f"profit factor {report.profit_factor:.2f}",
        ]
        if report.max_drawdown is not None:
            expected.append(f"max drawdown -{report.max_drawdown.drawdown_percent:.2f}%")
        assert figures == " · ".join(expected)
        assert clock_line == "🕑 2026-10-14 14:00 UTC"
        assert picture["media"] == "sendPhoto" and picture["file"]["data"] == PICTURE
        (asked,) = engine.asked
        assert asked["url"].startswith("file://") and asked["url"].endswith("/report.html")
        assert (asked["selector"], asked["ready"]) == ("#equity-chart-wrap",
                                                        _snapshot.REPORT_READY)
        assert asked["library"] == ("Chart", _snapshot.NO_JSDELIVR)
        assert document["media"] == "sendDocument"
        assert document["file"]["filename"] == "report.html"
        assert b"<html" in document["file"]["data"][:500].lower()

    def test_a_title_names_the_text_and_the_file(self, make, tg, engine):
        make().send_report(backtest(), title="MACD 1h · October")
        text, _picture, document = tg.got(ME)
        assert text["text"].startswith("<b>MACD 1h · October</b>\n")
        assert document["file"]["filename"] == "MACD_1h_October.html"

    def test_a_file_over_the_limit_is_left_out(self, make, tg, engine, capsys, monkeypatch):
        bot = make()
        monkeypatch.setattr(bot, "_max_document_bytes", 1_000)
        bot.send_report(backtest())
        assert [item["media"] for item in tg.got(ME)[1:]] == ["sendPhoto"]
        assert "the report file was left out — it is" in capsys.readouterr().out

    def test_without_a_balance_history_there_is_no_picture(self, make, tg, engine):
        report = backtest(rows=5)
        assert report.total_trades == 0
        report.balance_history = []
        make().send_report(report)
        text, document = tg.got(ME)
        assert text["text"].splitlines()[1] == "no trades"
        assert document["media"] == "sendDocument" and engine.asked == []
