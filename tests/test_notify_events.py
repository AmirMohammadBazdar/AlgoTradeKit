"""
Tests for v1.2.0 Part 2 — trade events to Telegram.

``run_live`` and ``Trader`` send their events through ``TraderConfig.notifiers``:
the default texts and your own, each chat's filter, one thread per trade (E1), a
session's start and stop (E2), the day's summary (E3), stop moves that edit the
trade's message (E5), a strategy's own message (Q2.3), and trading that never
waits on any of it.

Telegram is the fake Bot API of ``tests/test_notify.py``, taught
``editMessageText``; the markets are the paper and venue fakes of
``tests/test_trader.py``.  A fake clock jumps wherever the sender would wait for
its pacing, so a minute of Telegram's limits costs no real time.  The real
Telegram is never touched.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from test_notify import CHANNEL, ME, TOKEN, FakeClock, FakeTelegram, settle, step
from test_trader import (
    _T0,
    _XHOUR,
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

from AlgoTradeKit.broker import ConnectionFailed
from AlgoTradeKit.notify import (
    KIND_ALERT,
    KIND_CLOSE,
    KIND_DAILY_LOSS,
    KIND_DAILY_SUMMARY,
    KIND_ERROR,
    KIND_EXIT_SIGNAL,
    KIND_MESSAGE,
    KIND_OPEN,
    KIND_RECONCILE,
    KIND_RISK_FREE,
    KIND_SIGNAL,
    KIND_SL_MOVE,
    KIND_START,
    KIND_STOP,
    KIND_TEXT,
    KIND_TP_LEVEL,
    TelegramChat,
    TelegramNotifier,
    _core,
    _template,
)
from AlgoTradeKit.simulate import Simulate, SimulateConfig
from AlgoTradeKit.strategy import BaseStrategy, Signal, StrategyMode
from AlgoTradeKit.strategy._base import _NOTIFY_FIELDS, _check_notify_text
from AlgoTradeKit.trader import (
    ALL_EVENT_TYPES,
    EVENT_MESSAGE,
    EVENT_START,
    EVENT_STOP,
    SOURCE_LIVE,
    SOURCE_SIM,
    CloseEvent,
    DailyLossEvent,
    ErrorEvent,
    EventStream,
    ExitSignalEvent,
    MessageEvent,
    OpenEvent,
    ReconcileEvent,
    SignalEvent,
    StartEvent,
    StopEvent,
    TerminalEventPrinter,
    Trader,
    TraderConfig,
    TraderPair,
    run_live,
)
from AlgoTradeKit.trader import _notify as adapter_module
from AlgoTradeKit.trader import _run_live as run_live_module

UTC = timezone.utc
DAY = datetime(2026, 10, 14, tzinfo=UTC)
MINUTE = 60_000
HOUR = 3_600_000


def at(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return DAY + timedelta(hours=hour, minutes=minute, seconds=second)


def ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def candle(opens: datetime, o: float, h: float, low: float, c: float) -> dict:
    return {"timestamp": ms(opens), "open": o, "high": h, "low": low, "close": c,
            "volume": 1.0}


# ---------------------------------------------------------------------------
# The fake Telegram, taught to edit
# ---------------------------------------------------------------------------

NOT_FOUND = "Bad Request: message to edit not found"
NOT_MODIFIED = ("Bad Request: message is not modified: specified new message content and "
                "reply markup are exactly the same as a current content and reply markup "
                "of the message")


class EditingTelegram(FakeTelegram):
    """The fake Bot API, keeping each message's id and text, and answering
    ``editMessageText`` as Telegram does."""

    def reset(self) -> None:
        super().reset()
        self.messages: dict[tuple, str] = {}    # (chat_id, message_id) -> its text now
        self.edits: list[dict] = []

    def _answer(self, token, method, params, rule):
        free = rule is None or (rule["status"] == 200 and rule["text"] is None)
        if method == "editMessageText" and token == TOKEN and free:
            key = (params.get("chat_id"), params.get("message_id"))
            if key not in self.messages:
                return 400, {"ok": False, "error_code": 400, "description": NOT_FOUND}
            if self.messages[key] == params.get("text"):
                return 400, {"ok": False, "error_code": 400, "description": NOT_MODIFIED}
            self.messages[key] = params.get("text")
            self.edits.append({"chat_id": key[0], "message_id": key[1],
                               "text": params.get("text"), "params": params,
                               "at": self._now()})
            return 200, {"ok": True, "result": {"message_id": key[1],
                                                "text": params.get("text")}}
        status, body = super()._answer(token, method, params, rule)
        if method == "sendMessage" and status == 200 and isinstance(body, dict) and body["ok"]:
            message_id = body["result"]["message_id"]
            self.answered[-1]["message_id"] = message_id
            self.messages[(params.get("chat_id"), message_id)] = params.get("text")
        return status, body

    def delete(self, chat_id, message_id) -> None:
        with self._lock:
            self.messages.pop((chat_id, message_id), None)

    def got(self, chat_id) -> list[dict]:
        """What a chat received, in order."""
        with self._lock:
            return [item for item in self.answered if item["chat_id"] == chat_id]

    def texts(self, chat_id) -> list[str]:
        return [item["text"] for item in self.got(chat_id)]

    def edits_of(self, chat_id) -> list[dict]:
        with self._lock:
            return [item for item in self.edits if item["chat_id"] == chat_id]


class FastClock(FakeClock):
    """A fake clock that never makes a sender wait: when a notifier's sender
    waits for a message to come due, the clock jumps there.  Other waits (the
    daily-job thread) only poll, so midnight comes when a test moves the clock."""

    def __init__(self, start: datetime) -> None:
        super().__init__(start)
        self.jump_for: set = set()

    def wait(self, condition, timeout) -> None:
        if timeout is not None and timeout > 0 and condition in self.jump_for:
            self.advance(timeout)
            return
        super().wait(condition, timeout)

    def set(self, moment: datetime) -> None:
        """Move the clock forward to *moment*."""
        gap = moment.timestamp() - self.wall
        assert gap >= 0, "the clock only moves forward"
        self.advance(gap)


def drained(*bots, timeout: float = 15.0) -> None:
    """Until each notifier has sent all it holds — its inbox too — and waits."""
    for bot in bots:
        deadline = time.monotonic() + timeout
        while True:
            settle(bot, timeout)
            with bot._lock:
                busy = bot._waiting or bot._inbox or bot._working or bot._callbacks
            if not busy:
                break
            assert time.monotonic() < deadline, "the notifier did not drain"
            time.sleep(0.002)


@pytest.fixture(autouse=True)
def _no_proxies(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                 "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


@pytest.fixture(scope="module")
def server():
    fake = EditingTelegram()
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

    def build(chats=None, *, on: FakeClock | None = None, **kwargs) -> TelegramNotifier:
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


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    import AlgoTradeKit.trader._trader as trader_module

    monkeypatch.setattr(trader_module, "DEFAULT_STATE_PATH", str(tmp_path / "state.json"))


# ---------------------------------------------------------------------------
# Paper trading, one candle at a time
# ---------------------------------------------------------------------------

class SteppedBroker(_PaperBroker):
    """The paper broker of ``tests/test_trader.py``, delivering its live candles
    one at a time: before each, the clock moves to just after the candle's close;
    after each, every notifier drains — as a real feed waits for the next candle.
    ``after(index, row)`` runs once the candle is processed."""

    def __init__(self, rows, live_rows, *, clock, bots=(), tf_ms=HOUR, after=None,
                 fail_seed=None):
        super().__init__(rows, live_rows=live_rows)
        self.clock, self.bots, self.tf_ms, self.after = clock, tuple(bots), tf_ms, after
        self.fail_seed = fail_seed

    def fetch_last_candles(self, symbol, timeframe, count):
        if self.fail_seed is not None:
            raise self.fail_seed
        return super().fetch_last_candles(symbol, timeframe, count)

    def stream_candles(self, symbol, timeframe, on_candle, *, closed_only=True):
        self.calls.append(("stream", symbol, timeframe, closed_only))
        for index, row in enumerate(self.live_rows):
            self.clock.set(datetime.fromtimestamp(
                (row["timestamp"] + self.tf_ms) / 1000 + 1, tz=UTC))
            on_candle({**row, "closed": True})
            drained(*self.bots)
            if self.after is not None:
                self.after(index, row)
        stream = _PaperStream(alive=False)
        self.streams.append(stream)
        self.subscribed.set()
        return stream


class Plan(BaseStrategy):
    """Signals and messages scripted by candle opening (UTC ms)."""

    primary_timeframe = "1h"
    warmup_period = 0

    def __init__(self, signals=None, notes=None, timeframe="1h", metadata=None):
        self.primary_timeframe = timeframe
        self.signals = dict(signals or {})    # ts -> [(direction, entry, sl, tp)]
        self.notes = dict(notes or {})        # ts -> [text]
        self.metadata = dict(metadata or {})
        self.said: list[str] = []

    def prepare_indicators(self, data):
        return data

    def generate_signals(self, candle_index, data):
        ts = int(data[self.primary_timeframe]["timestamp"].iloc[candle_index])
        for text in self.notes.get(ts, ()):
            self.said.append(text)
            self.notify(text)
        return [
            Signal(direction=direction, entry_price=entry, stop_loss=sl, take_profit=tp,
                   timestamp=ts, candle_index=candle_index, timeframe=self.primary_timeframe,
                   metadata=dict(self.metadata))
            for direction, entry, sl, tp in self.signals.get(ts, ())
        ]


def paper_cfg(bots, symbol="BTCUSDT", **overrides) -> TraderConfig:
    kwargs = {"symbol": symbol, "min_candles": 5, "leverage": 10.0, "log_events": False,
              "notifiers": list(bots)}
    kwargs.update(overrides)
    return TraderConfig(**kwargs)


#: The example trade: long BTCUSDT 1h at 64,250, SL 63,800, TP 65,150,
#: risking 1% of 10,000 — $100 over 450, so 0.2222 BTC, RR 2.00.  It opens on the
#: 13:00 candle (raised at 14:00:01) and takes its profit on the 14:00 one.
SEED = [candle(at(hour), 64_250, 64_260, 64_240, 64_250) for hour in range(3, 13)]
LIVE = [candle(at(13), 64_250, 64_260, 64_240, 64_250),
        candle(at(14), 64_250, 65_200, 64_240, 65_100)]
LONG = {ms(at(13)): [("long", 64_250.0, 63_800.0, 65_150.0)]}

SIGNAL = ("<b>🟢 LONG BTCUSDT · 1h</b>\nEntry 64,250 · SL 63,800 · TP 65,150\nRR 2.00\n"
          "🕑 2026-10-14 14:00 UTC · SIM")
OPENED = ("<b>✅ OPENED #0 · LONG BTCUSDT @ 64,250</b>\n"
          "Size 0.2222 · Risk $100 · SL 63,800 · Next TP 65,150\n🕑 2026-10-14 14:00 UTC · SIM")
CLOSED = ("<b>🏁 CLOSED #0 · LONG BTCUSDT @ 65,150 — take profit</b>\n"
          "Net +$200.00 (+2.00 R) · held 1h\n🕑 2026-10-14 15:00 UTC · SIM")
STARTED = "<b>▶️ Paper trading started · BTCUSDT</b>\n🕑 2026-10-14 14:00 UTC · SIM"
STOPPED = ("<b>⏹ Paper trading stopped · BTCUSDT · feeds ended · no paper positions open</b>\n"
           "🕑 2026-10-14 15:00 UTC · SIM")


def paper(bots, clock, *, signals=LONG, notes=None, live=LIVE, seed=SEED, after=None,
          **overrides):
    broker = SteppedBroker(seed, live, clock=clock, bots=bots, after=after)
    strategy = Plan(signals, notes)
    report = run_live(strategy=strategy, broker=broker, config=paper_cfg(bots, **overrides))
    return report, broker, strategy


# ===========================================================================
# The kinds, and what a notifier refuses
# ===========================================================================

class TestKinds:
    def test_the_kind_constants_are_the_event_types(self):
        kinds = {KIND_SIGNAL, KIND_EXIT_SIGNAL, KIND_OPEN, KIND_SL_MOVE, KIND_RISK_FREE,
                 KIND_TP_LEVEL, KIND_CLOSE, KIND_DAILY_LOSS, KIND_RECONCILE, KIND_ERROR,
                 KIND_MESSAGE, KIND_START, KIND_STOP}
        assert kinds == ALL_EVENT_TYPES
        assert KIND_DAILY_SUMMARY == "daily_summary" and KIND_TEXT == "text"
        assert set(_core.KINDS) == kinds | {KIND_TEXT, KIND_DAILY_SUMMARY, KIND_ALERT}

    def test_an_unknown_kind_is_refused(self, make):
        with pytest.raises(ValueError, match=r"unknown kind 'sginal' — known: text, signal, "):
            TelegramChat(ME, events={"sginal", "close"})
        with pytest.raises(ValueError, match="unknown kind 'opne'"):
            make(templates={"opne": "…"})
        with pytest.raises(TypeError, match=r"templates\['open'\] must be a string"):
            make(templates={"open": 12})

    def test_sound(self):
        assert _core.SILENT_KINDS == {KIND_SL_MOVE, KIND_RISK_FREE, KIND_DAILY_SUMMARY}

    def test_the_value_formats(self):
        assert [format(_template.Size(v)) for v in (100 / 450, 0.01, 0.0001234567, 1250.5)] \
            == ["0.2222", "0.01", "0.0001235", "1,250.5"]
        assert [format(_template.Money(v)) for v in (100, 512.4, -100)] == [
            "$100", "$512.40", "-$100"]
        assert [format(_template.Pnl(v)) for v in (200, -100, 0)] == [
            "+$200.00", "-$100.00", "+$0.00"]
        assert format(_template.Ratio(2)) == "2.00"
        assert format(_template.SignedRatio(1.5)) == "+1.50"
        assert [format(_template.Percent(v)) for v in (0.5, 1 / 3)] == ["50%", "33.3%"]
        assert format(_template.Pnl(200), ".1f") == "200.0"        # a spec: the raw number


class TestTraderConfigNotifiers:
    def test_default_and_forms(self, make):
        bot = make()
        assert TraderConfig(symbol="BTCUSDT", min_candles=5).notifiers == []
        assert TraderConfig(symbol="BTCUSDT", min_candles=5, notifiers=(bot,)).notifiers == [bot]

    def test_mistakes_are_named(self, make):
        bot = make()
        with pytest.raises(ValueError, match=r"^TraderConfig.notifiers must be a list of "
                                             r"notifiers \(TelegramNotifier, …\), got 'str'$"):
            TraderConfig(symbol="BTCUSDT", min_candles=5, notifiers="bot")
        with pytest.raises(ValueError, match=r"^TraderConfig.notifiers\[1\] must be a notifier "
                                             r"\(TelegramNotifier, …\), got 'str'$"):
            TraderConfig(symbol="BTCUSDT", min_candles=5, notifiers=[bot, "bot"])
        with pytest.raises(ValueError, match="names the same notifier twice"):
            TraderConfig(symbol="BTCUSDT", min_candles=5, notifiers=[bot, bot])

    def test_log_event_types_take_the_new_types(self):
        cfg = TraderConfig(symbol="BTCUSDT", min_candles=5,
                           log_event_types={"start", "stop", "message"})
        assert cfg.log_event_types == {EVENT_START, EVENT_STOP, EVENT_MESSAGE}


# ===========================================================================
# The default texts, run_live end to end
# ===========================================================================

class TestDefaultTexts:
    def test_the_example_trade(self, make, tg, clock):
        bot = make()
        report, broker, _ = paper([bot], clock)
        assert report.total_trades == 1 and broker.order_calls == []
        assert tg.texts(ME) == [STARTED, SIGNAL, OPENED, CLOSED, STOPPED]

    def test_the_numbers_come_out_exactly(self, make, tg, clock):
        """1% of 10,000 risked over 450 → 0.2222; RR 2.00; +$200.00 (+2.00 R)."""
        bot = make(templates={
            "open": "{size} {risk_amount} {size:.6f}",
            "signal": "{rr}",
            "close": "{net_pnl} {pnl_r} {gross_pnl} {duration} {reason_text}",
        })
        paper([bot], clock)
        texts = tg.texts(ME)
        assert "2.00" in texts
        assert "0.2222 $100 0.222222" in texts
        assert "+$200.00 +2.00 +$200.00 1h take profit" in texts

    def test_the_trade_reads_as_one_thread(self, make, tg, clock):
        bot = make()
        paper([bot], clock)
        got = {item["text"]: item for item in tg.got(ME)}
        opened = got[OPENED]["message_id"]
        assert "reply_parameters" not in got[SIGNAL]["params"]
        assert "reply_parameters" not in got[OPENED]["params"]
        assert got[CLOSED]["params"]["reply_parameters"] == {
            "message_id": opened, "allow_sending_without_reply": True}

    def test_q22_the_time_is_when_it_was_raised(self, make, tg, clock):
        bot = make(templates={"signal": "{time} | {candle_time} | {date}"})
        paper([bot], clock)
        assert "2026-10-14 14:00 UTC | 2026-10-14 13:00 UTC | 2026-10-14" in tg.texts(ME)

    def test_in_tehran(self, make, tg, clock):
        bot = make(timezone="Asia/Tehran")
        paper([bot], clock)
        assert tg.texts(ME)[1].endswith("🕑 2026-10-14 17:30 (UTC+03:30) · SIM")

    def test_plain_text_mode_has_no_bold(self, make, tg, clock):
        bot = make(parse_mode=None)
        paper([bot], clock)
        assert tg.texts(ME)[1] == SIGNAL.replace("<b>", "").replace("</b>", "")
        assert "parse_mode" not in tg.got(ME)[1]["params"]

    def test_a_short_and_what_is_missing_is_left_out(self, make, tg, clock):
        bot = make()
        short = {ms(at(13)): [("short", 64_250.0, 64_700.0, None)]}
        paper([bot], clock, signals=short, tp_mode="none")
        assert tg.texts(ME)[1] == ("<b>🔴 SHORT BTCUSDT · 1h</b>\nEntry 64,250 · SL 64,700\n"
                                   "🕑 2026-10-14 14:00 UTC · SIM")
        assert tg.texts(ME)[2] == ("<b>✅ OPENED #0 · SHORT BTCUSDT @ 64,250</b>\n"
                                   "Size 0.2222 · Risk $100 · SL 64,700\n"
                                   "🕑 2026-10-14 14:00 UTC · SIM")

    def test_values_are_escaped(self, make, tg, clock):
        bot = make(templates={"signal": "<i>{metadata}</i>"})
        broker = SteppedBroker(SEED, LIVE, clock=clock, bots=[bot])
        run_live(strategy=Plan(LONG, metadata={"why": "<a&b>"}), broker=broker,
                 config=paper_cfg([bot]))
        assert "<i>why=&lt;a&amp;b&gt;</i>" in tg.texts(ME)


class TestOwnTemplates:
    def test_a_template_replaces_the_default(self, make, tg, clock):
        bot = make(templates={"open": "<b>IN</b> {symbol} {fill_price} · {signal_time}"})
        paper([bot], clock)
        assert "<b>IN</b> BTCUSDT 64,250 · 2026-10-14 13:00 UTC" in tg.texts(ME)

    def test_a_field_with_no_value_prints_a_dash(self, make, tg, clock):
        bot = make(templates={"open": "order {order_id} · {order_id:>8}"})
        paper([bot], clock)
        assert "order — · —" in tg.texts(ME)

    def test_a_mistake_raises_when_wired_before_any_trading(self, make, tg, clock):
        bot = make(templates={"open": "{fill} at {fill_price}"})
        broker = SteppedBroker(SEED, LIVE, clock=clock, bots=[bot])
        with pytest.raises(ValueError, match=r"^template for 'open' uses \{fill\} — known: "
                                             r"time, date, candle_time, symbol, source, "):
            run_live(strategy=Plan(LONG), broker=broker, config=paper_cfg([bot]))
        assert broker.calls == []          # the venue was never asked
        drained(bot)
        assert tg.answered == []           # nothing was sent

    def test_none_is_never_sent(self, make, tg, clock):
        bot = make(templates={"signal": None, "start": None, "stop": None})
        paper([bot], clock)
        assert tg.texts(ME) == [OPENED, CLOSED]

    def test_the_fields_of_each_kind(self):
        common = ("time", "date", "candle_time", "symbol", "source", "timeframe", "price",
                  "header")
        assert _NOTIFY_FIELDS == common
        assert adapter_module.fields_of_kind(KIND_CLOSE)[len(common):] == (
            "trade_id", "exit_price", "reason", "gross_pnl", "net_pnl", "pnl_r",
            "duration_ms", "direction", "reason_text", "duration")
        assert adapter_module.fields_of_kind(KIND_OPEN)[-1] == "signal_time"
        assert adapter_module.fields_of_kind(KIND_MESSAGE)[len(common):] == ("text", "silent")
        assert adapter_module.fields_of_kind(KIND_START)[len(common):] == (
            "mode", "symbols", "session_id")
        assert adapter_module.fields_of_kind(KIND_STOP)[len(common):] == (
            "mode", "reason", "open_positions", "error", "session_id", "symbols")
        assert adapter_module.fields_of_kind(KIND_DAILY_SUMMARY)[len(common):] == (
            "day", "trades", "won", "lost", "net", "open_positions", "open_list")
        for kind in (KIND_SIGNAL, KIND_SL_MOVE, KIND_RISK_FREE, KIND_TP_LEVEL,
                     KIND_EXIT_SIGNAL, KIND_DAILY_LOSS, KIND_RECONCILE, KIND_ERROR):
            names = adapter_module.fields_of_kind(kind)
            assert names[:len(common)] == common
            assert not {"signal", "trade", "exit_signal"} & set(names)   # a template is data


class TestTextWrapper:
    """Q2.10 (a): ``templates={"text": …}`` wraps every custom text."""

    def test_the_wrapper(self, make, tg):
        bot = make(templates={"text": "<b>ATK</b> · {text}"})
        bot.send_text("Watching the <i>64k</i> level")
        assert tg.texts(ME) == ["<b>ATK</b> · Watching the <i>64k</i> level"]

    def test_its_fields_are_a_custom_texts(self, make, tg, clock):
        bot = make(templates={"text": "{text} — {time}"})
        bot.send_text("hi")
        assert tg.texts(ME) == ["hi — 2026-10-14 14:00 UTC"]
        with pytest.raises(ValueError, match=r"\{symbol\} needs symbol="):
            make(templates={"text": "{symbol}: {text}"}).send_text("x")

    def test_a_mistake_raises_when_the_notifier_is_created(self, make):
        with pytest.raises(ValueError, match=r"^unknown field \{txt\} — known: text, time, "
                                             r"date, symbol, price, header$"):
            make(templates={"text": "{txt}"})

    def test_none_sends_no_text(self, make, tg):
        bot = make(templates={"text": None})
        assert bot.send_text("hi") == {}
        with pytest.raises(ValueError, match="unknown field"):
            bot.send_text("{prise}")       # still checked
        drained(bot)
        assert tg.answered == []


# ===========================================================================
# Filters and sound
# ===========================================================================

class TestFiltersAndSound:
    def test_each_chat_gets_what_its_filter_lets_through(self, make, tg, clock):
        tg.chats[-1003333333333] = {"id": -1003333333333, "type": "channel", "title": "ETH"}
        bot = make({"me": ME,
                    "signals": TelegramChat(CHANNEL, events={"signal", "tp_level", "close"}),
                    "eth": TelegramChat(-1003333333333, symbols={"ETHUSDT"})})
        paper([bot], clock)
        assert tg.texts(ME) == [STARTED, SIGNAL, OPENED, CLOSED, STOPPED]
        channel = tg.texts(CHANNEL)
        assert [text.splitlines()[0] for text in channel] == [
            "<b>🟢 LONG BTCUSDT · 1h</b>",
            "<b>🏁 CLOSED #0 · LONG BTCUSDT @ 65,150 — take profit</b>"]
        # a symbol filter keeps out other symbols only: start and stop name no symbol
        assert [text.splitlines()[0] for text in tg.texts(-1003333333333)] == [
            "<b>▶️ Paper trading started · BTCUSDT</b>",
            "<b>⏹ Paper trading stopped · BTCUSDT · feeds ended · no paper positions open</b>"]

    def test_sl_moves_risk_free_and_the_day_go_without_sound(self, make, tg, clock):
        bot = make({"me": ME, "quiet": TelegramChat(CHANNEL, silent=True)})
        live = [candle(at(13), 64_250, 64_260, 64_240, 64_250),
                candle(at(14), 64_300, 64_710, 64_260, 64_700)]   # +1R → break-even
        paper([bot], clock, live=live, tp_mode="none", risk_free_enabled=True)
        loud = {item["text"].splitlines()[0]: item["params"].get("disable_notification")
                for item in tg.got(ME)}
        assert loud["<b>🛡 RISK-FREE #0 · BTCUSDT</b>"] is True
        (risk_free,) = [text for text in tg.texts(ME) if text.startswith("<b>🛡")]
        assert risk_free == ("<b>🛡 RISK-FREE #0 · BTCUSDT</b>\n1.00 R reached · SL 63,800 → "
                             "64,250 (break-even)\n🕑 2026-10-14 15:00 UTC · SIM")
        assert loud["<b>🟢 LONG BTCUSDT · 1h</b>"] is None
        assert all(item["params"].get("disable_notification") for item in tg.got(CHANNEL))


# ===========================================================================
# E1 — one trade, one thread
# ===========================================================================

LADDER = {"tp_mode": "multi_rr", "tp_levels": [1.0, 2.0], "tp_level_close_fractions": [0.5, 0.5]}
LADDER_LIVE = [candle(at(13), 64_250, 64_260, 64_240, 64_250),
               candle(at(14), 64_250, 64_710, 64_240, 64_700),    # 1R: half, SL to 64,250
               candle(at(15), 64_700, 65_200, 64_600, 65_150)]    # 2R: the rest


class TestThreads:
    def test_e1_tp_level_and_close_reply_to_the_open_message(self, make, tg, clock):
        bot = make()
        paper([bot], clock, live=LADDER_LIVE, **LADDER)
        got = tg.got(ME)
        heads = [item["text"].splitlines()[0] for item in got]
        opened = got[heads.index("<b>✅ OPENED #0 · LONG BTCUSDT @ 64,250</b>")]
        level = got[heads.index("<b>🎯 TP 1.00 R #0 · BTCUSDT</b>")]
        closed = got[heads.index("<b>🏁 CLOSED #0 · LONG BTCUSDT @ 65,150 — take profit</b>")]
        for reply in (level, closed):
            assert reply["params"]["reply_parameters"]["message_id"] == opened["message_id"]
        assert level["text"].splitlines()[1] == "Closed 50% · +$50.00 · new SL 64,250"

    def test_q27_the_close_counts_the_whole_trade(self, make, tg, clock):
        bot = make()
        paper([bot], clock, live=LADDER_LIVE, **LADDER)
        closed = next(text for text in tg.texts(ME) if text.startswith("<b>🏁"))
        assert closed.splitlines()[1] == "Net +$150.00 (+1.50 R) · held 2h"

    def test_e1_without_open_they_reply_to_the_signal_message(self, make, tg, clock):
        bot = make({"signals": TelegramChat(CHANNEL, events={"signal", "tp_level", "close"})})
        paper([bot], clock, live=LADDER_LIVE, **LADDER)
        signal, level, closed = tg.got(CHANNEL)
        assert signal["text"].startswith("<b>🟢 LONG")
        for reply in (level, closed):
            assert reply["params"]["reply_parameters"]["message_id"] == signal["message_id"]


class TestRootsKept:
    def test_a_refused_open_has_no_root(self, make, tg, clock):
        bot = make()
        # Refuse exactly the third message to "me": START, SIGNAL, then the OPEN.
        count = {"n": 0}
        original = tg._answer

        def answer(token, method, params, rule):
            if method == "sendMessage" and params.get("chat_id") == ME:
                count["n"] += 1
                if count["n"] == 3:
                    return 400, {"ok": False, "error_code": 400,
                                 "description": "Bad Request: scripted"}
            return original(token, method, params, rule)

        tg._answer = answer
        try:
            paper([bot], clock)
        finally:
            del tg._answer
        closed = next(item for item in tg.got(ME) if item["text"].startswith("<b>🏁"))
        assert "reply_parameters" not in closed["params"]

    def test_a_chat_keeps_at_most_500_roots(self, make):
        bot = make()
        adapter = adapter_module._Adapter(bot, None, None)
        dest = bot._destinations["me"]
        for trade_id in range(510):
            adapter._keep_root(dest, ("open", adapter, trade_id))(trade_id + 1, "text")
        roots = adapter.hub.roots["me"]
        assert len(roots) == 500
        assert ("open", adapter, 9) not in roots and ("open", adapter, 10) in roots


# ===========================================================================
# E5 — SL moves edit the open message
# ===========================================================================

def trailing_day() -> tuple[list, list]:
    """BTCUSDT 1m: a long opens at 64,250 (SL 63,800) on the 14:00 candle, its
    trailing stop (0.5%) moves on the next 70 candles, holds for 109, and is hit
    on the 180th."""
    seed = [candle(at(13, 50 + k), 64_250, 64_260, 64_240, 64_250) for k in range(10)]
    live = [candle(at(14), 64_250, 64_260, 64_240, 64_250)]
    for k in range(1, 71):              # a new high every candle: the stop follows
        high = 64_250 + 10 * k
        live.append(candle(at(14, k), high - 10, high, high - 20, high - 5))
    for k in range(71, 180):            # below the peak, above the stop
        live.append(candle(at(14, k), 64_800, 64_900, 64_700, 64_800))
    live.append(candle(at(14, 180), 64_800, 64_810, 64_500, 64_550))   # the stop is hit
    return seed, live


class TestEdits:
    def run_trailing(self, bots, clock, **overrides):
        seed, live = trailing_day()
        broker = SteppedBroker(seed, live, clock=clock, bots=bots, tf_ms=MINUTE)
        strategy = Plan({ms(at(14)): [("long", 64_250.0, 63_800.0, None)]}, timeframe="1m")
        cfg = paper_cfg(bots, tp_mode="none", sl_mode="trailing", trailing_sl_percent=0.5,
                        **overrides)
        return run_live(strategy=strategy, broker=broker, config=cfg)

    def test_q21_seventy_moves_three_messages_and_seventy_edits(self, make, tg, clock):
        everything = set(_core.KINDS) - {KIND_TEXT}
        bot = make({"me": ME, "all": TelegramChat(CHANNEL, events=everything)})
        report = self.run_trailing([bot], clock)
        assert [t.close_reason for t in report.closed_trades] == ["sl"]
        heads = [text.splitlines()[0] for text in tg.texts(ME)]
        assert heads[1:-1] == ["<b>🟢 LONG BTCUSDT · 1m</b>",
                               "<b>✅ OPENED #0 · LONG BTCUSDT @ 64,250</b>",
                               "<b>🏁 CLOSED #0 · LONG BTCUSDT @ 64,625.25 — stop loss</b>"]
        assert [text.splitlines()[0] for text in tg.texts(CHANNEL)] == [
            text.splitlines()[0] for text in tg.texts(ME)]              # no SL message there
        opened = tg.got(CHANNEL)[2]
        edits = tg.edits_of(CHANNEL)
        assert len(edits) == 70
        assert {edit["message_id"] for edit in edits} == {opened["message_id"]}
        assert tg.edits_of(ME) == []
        first, last = edits[0]["text"].splitlines(), edits[-1]["text"].splitlines()
        assert first[2] == "SL 63,800 → 63,938.7 (trailing) · 14:02"
        assert last[2] == "SL 63,800 → 64,625.25 (trailing) · 15:11"
        assert last[:2] == opened["text"].splitlines()[:2]
        assert last[3] == opened["text"].splitlines()[2]               # the 🕑 line, last

    def test_a_newer_edit_replaces_one_still_waiting(self, make, tg, clock):
        bot = make()
        dest = bot._destinations["me"]
        release = threading.Event()
        deliver = bot._deliver

        def held(dest, text, *, silent, **reply):
            release.wait(10)
            return deliver(dest, text, silent=silent, **reply)

        bot._deliver = held
        bot.send_text("root", wait=False)
        settle_until = time.monotonic() + 5
        while bot._in_flight is None and time.monotonic() < settle_until:
            time.sleep(0.002)
        for n in (1, 2, 3):
            bot._queue_event(dest, KIND_SL_MOVE, "BTCUSDT", f"move {n}", queued_at=clock.time(),
                             silent=True, coalesce=("sl_move", 0))
        release.set()
        drained(bot)
        assert tg.texts(ME) == ["root", "move 3"]

    def test_a_deleted_root_gets_a_new_silent_message(self, make, tg, clock):
        bot = make({"all": TelegramChat(ME, events=set(_core.KINDS) - {KIND_TEXT})})
        deleted = {}

        def delete_the_root(index, row):
            if index == 0:
                opened = next(item for item in tg.got(ME) if item["text"].startswith("<b>✅"))
                tg.delete(ME, opened["message_id"])
                deleted["id"] = opened["message_id"]

        seed, live = trailing_day()
        live = live[:3]    # the open, then two moves
        broker = SteppedBroker(seed, live, clock=clock, bots=[bot], tf_ms=MINUTE,
                               after=delete_the_root)
        strategy = Plan({ms(at(14)): [("long", 64_250.0, 63_800.0, None)]}, timeframe="1m")
        run_live(strategy=strategy, broker=broker,
                 config=paper_cfg([bot], tp_mode="none", sl_mode="trailing",
                                  trailing_sl_percent=0.5))
        moves = [item for item in tg.got(ME) if item["text"].startswith("<b>↗️")]
        assert [item["text"].splitlines()[1] for item in moves] == [
            "63,800 → 63,938.7 (trailing)", "63,938.7 → 63,948.65 (trailing)"]
        assert all(item["params"]["disable_notification"] is True for item in moves)
        assert tg.edits_of(ME) == []

    def test_not_modified_counts_as_done(self, make, tg, clock):
        bot = make()
        dest = bot._destinations["me"]
        bot.send_text("same")
        (sent,) = tg.got(ME)
        bot._queue_event(dest, KIND_SL_MOVE, None, "fallback", queued_at=clock.time(),
                         silent=True, edit=lambda: (sent["message_id"], "same"))
        drained(bot)
        assert tg.texts(ME) == ["same"]                    # no new message
        assert len(tg.tries("editMessageText")) == 1       # and no second try

    def test_without_a_root_a_move_is_a_new_message(self, make, tg, clock):
        bot = make({"moves": TelegramChat(ME, events={"sl_move"})})
        seed, live = trailing_day()
        broker = SteppedBroker(seed, live[:3], clock=clock, bots=[bot], tf_ms=MINUTE)
        strategy = Plan({ms(at(14)): [("long", 64_250.0, 63_800.0, None)]}, timeframe="1m")
        run_live(strategy=strategy, broker=broker,
                 config=paper_cfg([bot], tp_mode="none", sl_mode="trailing",
                                  trailing_sl_percent=0.5))
        assert tg.texts(ME) == [
            "<b>↗️ SL MOVED #0 · BTCUSDT</b>\n63,800 → 63,938.7 (trailing)\n"
            "🕑 2026-10-14 14:02 UTC · SIM",
            "<b>↗️ SL MOVED #0 · BTCUSDT</b>\n63,938.7 → 63,948.65 (trailing)\n"
            "🕑 2026-10-14 14:03 UTC · SIM",
        ]


# ===========================================================================
# E2 — start and stop
# ===========================================================================

ETH_SEED = [candle(at(hour), 2_500, 2_501, 2_499, 2_500) for hour in range(3, 13)]
ETH_LIVE = [candle(at(13), 2_500, 2_501, 2_499, 2_500), candle(at(14), 2_500, 2_501, 2_499, 2_500)]


class TestStartAndStop:
    def test_one_start_and_one_stop_per_notifier_naming_both_pairs(self, make, tg, clock):
        bot = make()
        other = make({"other": CHANNEL})
        btc = SteppedBroker(SEED, LIVE, clock=clock, bots=[bot, other])
        eth = _PaperBroker(ETH_SEED, live_rows=ETH_LIVE)
        report = run_live(pairs=[
            TraderPair(broker=btc, config=paper_cfg([bot, other]), strategy=Plan(LONG)),
            TraderPair(broker=eth, config=paper_cfg([bot, other], symbol="ETHUSDT"),
                       strategy=Plan()),
        ])
        assert len(report) == 2
        for chat in (ME, CHANNEL):
            heads = [text.splitlines()[0] for text in tg.texts(chat)]
            assert heads.count("<b>▶️ Paper trading started · BTCUSDT, ETHUSDT</b>") == 1
            assert heads[-1] == ("<b>⏹ Paper trading stopped · BTCUSDT, ETHUSDT · "
                                 "feeds ended · no paper positions open</b>")
            assert sum(head.startswith("<b>⏹") for head in heads) == 1

    def test_e2_ctrl_c_on_two_pairs(self, make, tg, clock, monkeypatch):
        bot = make()
        btc = _PaperBroker(SEED)                 # no live rows: the feeds stay alive
        eth = _PaperBroker(ETH_SEED)

        def ctrl_c(sessions):
            # Ctrl+C reaches run_live as a KeyboardInterrupt inside its wait.  It is
            # raised here rather than by _thread.interrupt_main(), which does nothing
            # while SIGINT is ignored or held by a handler left from elsewhere.
            assert all(session.sim.stream.alive for session in sessions)
            raise KeyboardInterrupt

        monkeypatch.setattr(run_live_module, "_wait", ctrl_c)
        run_live(pairs=[
            TraderPair(broker=btc, config=paper_cfg([bot]), strategy=Plan()),
            TraderPair(broker=eth, config=paper_cfg([bot], symbol="ETHUSDT"), strategy=Plan()),
        ])
        texts = tg.texts(ME)           # out before run_live returned
        assert texts[-1].splitlines()[0] == (
            "<b>⏹ Paper trading stopped · BTCUSDT, ETHUSDT · Ctrl+C · no paper positions "
            "open</b>")
        assert texts[0].splitlines()[0] == "<b>▶️ Paper trading started · BTCUSDT, ETHUSDT</b>"

    def test_the_stop_counts_what_is_left_open(self, make, tg, clock):
        bot = make()
        paper([bot], clock, live=LIVE[:1])          # opens, and the feed ends
        assert tg.texts(ME)[-1].splitlines()[0] == (
            "<b>⏹ Paper trading stopped · BTCUSDT · feeds ended · 1 paper position open</b>")

    def test_a_crash_sends_the_stop_with_the_error(self, make, tg, clock, monkeypatch):
        bot = make()

        def crash(sessions):
            raise RuntimeError("boom")

        monkeypatch.setattr(run_live_module, "_wait", crash)
        with pytest.raises(RuntimeError, match="boom"):
            paper([bot], clock, log_events=True)
        assert tg.texts(ME)[-1].splitlines()[0] == (
            "<b>⏹ Paper trading stopped by an error · BTCUSDT · RuntimeError: boom · "
            "no paper positions open</b>")

    def test_q12b_a_seed_that_fails_sends_the_stop_with_the_error(self, make, tg, clock):
        bot = make()
        broker = SteppedBroker(SEED, LIVE, clock=clock, bots=[bot],
                               fail_seed=ConnectionFailed("Binance unreachable"))
        with pytest.raises(ConnectionFailed):
            run_live(strategy=Plan(LONG), broker=broker, config=paper_cfg([bot]))
        assert tg.texts(ME) == [
            "<b>⏹ Paper trading stopped by an error · BTCUSDT · ConnectionFailed: Binance "
            "unreachable</b>\n🕑 2026-10-14 14:00 UTC · SIM"]

    def test_the_printer_prints_them(self, make, tg, clock, capsys):
        bot = make()
        paper([bot], clock, log_events=True, notes={ms(at(14)): ["RSI above 70 {price}"]})
        out = capsys.readouterr().out
        assert "[SIM] START paper BTCUSDT" in out
        assert "[SIM] STOP feeds ended open=0" in out
        assert "[SIM][BTCUSDT] MESSAGE RSI above 70 {price}" in out

    def test_the_printer_lines(self):
        printer = TerminalEventPrinter()
        start = StartEvent(time=ms(at(14)), symbol="", source=SOURCE_LIVE, mode="live",
                           symbols=("BTCUSDT", "ETHUSDT"), session_id="ab12")
        stop = StopEvent(time=ms(at(15)), symbol="", source=SOURCE_LIVE, mode="live",
                         reason="crash", open_positions=0, error="ValueError: boom")
        message = MessageEvent(time=ms(at(13)), symbol="BTCUSDT", source=SOURCE_SIM,
                               text="RSI above 70")
        assert printer.format_event(start) == (
            "2026-10-14 14:00:00 [LIVE] START live BTCUSDT, ETHUSDT")
        assert printer.format_event(stop) == (
            "2026-10-14 15:00:00 [LIVE] STOP crash open=0 error=ValueError: boom")
        assert printer.format_event(message) == (
            "2026-10-14 13:00:00 [SIM][BTCUSDT] MESSAGE RSI above 70")
        assert printer.format_event(StopEvent(
            time=ms(at(15)), symbol="", source=SOURCE_SIM, mode="paper", reason="Ctrl+C",
            open_positions=None)).endswith("STOP Ctrl+C")


# ===========================================================================
# E3 — the day
# ===========================================================================

class TestTheDay:
    def test_e3_at_midnight_in_tehran_one_summary_per_pair(self, make, tg, clock):
        """23:30 to 00:01 Asia/Tehran (20:00 to 20:31 UTC): one trade closes; then
        midnight passes while the session runs."""
        clock.set(at(18, 30))
        bot = make(timezone="Asia/Tehran")
        seed = [candle(at(hour), 64_250, 64_260, 64_240, 64_250) for hour in range(8, 18)]
        live = [candle(at(18), 64_250, 64_260, 64_240, 64_250),     # opens at 22:30 Tehran
                candle(at(19), 64_250, 65_200, 64_240, 65_100)]     # +$200 at 23:30
        signals = {ms(at(18)): [("long", 64_250.0, 63_800.0, 65_150.0)]}

        def past_midnight(index, row):
            if index == 1:
                clock.set(at(20, 31))           # 00:01 in Tehran, on the 15th
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    drained(bot)
                    if sum(text.startswith("<b>📊") for text in tg.texts(ME)) == 2:
                        break
                    time.sleep(0.01)

        btc = SteppedBroker(seed, live, clock=clock, bots=[bot], after=past_midnight)
        eth = _PaperBroker([candle(at(hour), 2_500, 2_501, 2_499, 2_500) for hour in range(8, 18)],
                           live_rows=[candle(at(18), 2_500, 2_501, 2_499, 2_500)])
        run_live(pairs=[
            TraderPair(broker=btc, config=paper_cfg([bot]), strategy=Plan(signals)),
            TraderPair(broker=eth, config=paper_cfg([bot], symbol="ETHUSDT"), strategy=Plan()),
        ])
        summaries = [item for item in tg.got(ME) if item["text"].startswith("<b>📊")]
        assert sorted(item["text"] for item in summaries) == sorted([
            "<b>📊 DAY 2026-10-14 · BTCUSDT</b>\nTrades 1 · won 1 · lost 0 · net +$200.00\n"
            "No open positions\n🕑 2026-10-15 00:01 (UTC+03:30) · SIM",
            "<b>📊 DAY 2026-10-14 · ETHUSDT — no trades · no open positions</b>\n"
            "🕑 2026-10-15 00:01 (UTC+03:30) · SIM",
        ])
        assert all(item["params"]["disable_notification"] is True for item in summaries)

    def test_the_summary_lists_the_open_positions(self, make, tg, clock):
        bot = make()

        def midnight(index, row):
            clock.set(at(24, 0, 5))
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                drained(bot)
                if any(text.startswith("<b>📊") for text in tg.texts(ME)):
                    break
                time.sleep(0.01)

        paper([bot], clock, live=LIVE[:1], after=midnight)
        (summary,) = [text for text in tg.texts(ME) if text.startswith("<b>📊")]
        assert summary == ("<b>📊 DAY 2026-10-14 · BTCUSDT</b>\nNo trades\n"
                           "Open now: 1 (#0 LONG @ 64,250)\n🕑 2026-10-15 00:00 UTC · SIM")

    def test_a_tp_level_counts_in_the_days_net(self, make, tg, clock):
        bot = make()

        def midnight(index, row):
            if index == 1:
                clock.set(at(24, 0, 5))
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    drained(bot)
                    if any(text.startswith("<b>📊") for text in tg.texts(ME)):
                        break
                    time.sleep(0.01)

        paper([bot], clock, live=LADDER_LIVE[:2], after=midnight, **LADDER)
        (summary,) = [text for text in tg.texts(ME) if text.startswith("<b>📊")]
        assert summary.splitlines()[1] == "Trades 0 · won 0 · lost 0 · net +$50.00"


# ===========================================================================
# A strategy's own message (Q2.3 b, Q2.11 b)
# ===========================================================================

def rsi_hot(hours) -> dict:
    return {ms(at(hour)): ["RSI above 70 · {symbol} {price}"] for hour in hours}


class TestStrategyMessages:
    def test_q23_the_seed_says_nothing_two_new_candles_two_messages(self, make, tg, clock):
        """A 500-candle seed with 31 candles of RSI above 70: no message.  Two new such
        candles: two messages."""
        bot = make()
        seed = [candle(at(hour), 64_250, 64_260, 64_240, 64_250) for hour in range(-487, 13)]
        notes = rsi_hot(list(range(-487, 13, 16))[:31])   # 31 of the seed's 500 candles
        notes.update(rsi_hot([13, 14]))        # and both live ones
        assert len(seed) == 500 and len(notes) == 33
        _, _, strategy = paper([bot], clock, signals={}, notes=notes, seed=seed,
                               min_candles=500)
        assert len(strategy.said) == 33        # called on every such candle…
        messages = [text for text in tg.texts(ME) if text.startswith("<b>💬")]
        assert messages == [                   # …sent for the two live ones
            "<b>💬 BTCUSDT · 1h</b>\nRSI above 70 · BTCUSDT 64,250\n🕑 2026-10-14 14:00 UTC · SIM",
            "<b>💬 BTCUSDT · 1h</b>\nRSI above 70 · BTCUSDT 65,100\n🕑 2026-10-14 15:00 UTC · SIM",
        ]

    def test_q23_silent_in_a_backtest(self, make, tg):
        make()
        strategy = Plan(notes=rsi_hot(range(3, 13)))
        frame = __import__("pandas").DataFrame(SEED)
        Simulate(SimulateConfig(symbol="BTCUSDT")).run(strategy.run({"1h": frame}))
        assert len(strategy.said) == 10
        assert tg.answered == []

    def test_q211b_a_mistake_raises_in_a_backtest(self):
        strategy = Plan(notes={ms(at(5)): ["{symbl} RSI above 70"]})
        frame = __import__("pandas").DataFrame(SEED)
        with pytest.raises(ValueError, match=r"^unknown field \{symbl\} — known: time, date, "
                                             r"candle_time, symbol, source, timeframe, price, "
                                             r"header$"):
            strategy.run({"1h": frame}, mode=StrategyMode.BACKTEST)

    def test_q211b_live_a_mistake_is_an_error_and_the_candle_goes_on(self, make, tg, clock):
        bot = make()
        notes = {ms(at(13)): ["{symbl} RSI above 70"]}
        report, _, _ = paper([bot], clock, notes=notes)
        assert report.total_trades == 1        # the candle opened its trade all the same
        errors = [text for text in tg.texts(ME) if text.startswith("<b>⚠️")]
        assert errors == [
            "<b>⚠️ ERROR · BTCUSDT</b>\nnotify: unknown field {symbl} — known: time, date, "
            "candle_time, symbol, source, timeframe, price, header\n"
            "🕑 2026-10-14 14:00 UTC · SIM"]

    def test_silent_true(self, make, tg, clock):
        bot = make()

        class Quiet(Plan):
            def generate_signals(self, candle_index, data):
                if int(data["1h"]["timestamp"].iloc[candle_index]) == ms(at(13)):
                    self.notify("quiet {symbol}", silent=True)
                return []

        broker = SteppedBroker(SEED, LIVE[:1], clock=clock, bots=[bot])
        run_live(strategy=Quiet(), broker=broker, config=paper_cfg([bot]))
        (item,) = [item for item in tg.got(ME) if item["text"].startswith("<b>💬")]
        assert item["params"]["disable_notification"] is True

    def test_the_two_checks_agree(self):
        texts = ["RSI {price}", "{prise}", "{}", "{0}", "{a.b}", "{a[0]}", "{price!r}",
                 "{price:{w}}", "{", "}", "{{literal}} {time:%H:%M} {price:.1f}", "plain"]
        for text in texts:
            notify_said = strategy_said = None
            try:
                _template.fields_of(text, _NOTIFY_FIELDS)
            except ValueError as exc:
                notify_said = str(exc)
            try:
                _check_notify_text(text)
            except ValueError as exc:
                strategy_said = str(exc)
            assert notify_said == strategy_said, text

    def test_without_a_hook_a_good_text_does_nothing(self):
        Plan().notify("fine {symbol}")       # no runner: nothing to send it to


# ===========================================================================
# The Trader
# ===========================================================================

def live_trader(bots, tmp_path, *, signals=None, ticks=(), monkeypatch, **cfg_kw):
    _t_patch_scheduler(monkeypatch, list(ticks))
    broker = _TFuturesBroker(_x_candles(10), fill_price=110.0)
    broker.ticker_last = 110.0
    cfg = _t_cfg(notifiers=list(bots), **cfg_kw)
    t = Trader(broker=broker, strategy=_TPlanStrategy(signals_at=signals or {}), config=cfg,
               trader_id="t2", state_path=tmp_path / "state.json")
    broker.mirror_engine = t.engine
    return t, broker


def run_until(t, condition, timeout=5.0):
    thread = threading.Thread(target=t.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not condition():
        time.sleep(0.01)
    t.stop()
    thread.join(timeout=15.0)
    assert not thread.is_alive()


class TestTrader:
    def test_signal_open_start_and_stop(self, make, tg, clock, tmp_path, monkeypatch):
        bot = make()
        t, _ = live_trader([bot], tmp_path, monkeypatch=monkeypatch,
                           signals={_T0: [("long", 110.0, 105.0, None)]},
                           ticks=[_t_tick([_t_candle(0)])])
        run_until(t, lambda: len(t.open_trades) == 1)
        heads = [text.splitlines()[0] for text in tg.texts(ME)]
        assert heads == [
            "<b>▶️ Live trading started · BTCUSDT</b>",
            "<b>🟢 LONG BTCUSDT · 1h</b>",
            "<b>✅ OPENED #0 · LONG BTCUSDT @ 110</b>",
            "<b>⏹ Live trading stopped · BTCUSDT · stop() · 1 position left open with its "
            "venue SL/TP</b>",
        ]
        opened = tg.texts(ME)[2]
        assert opened.endswith(" · LIVE · order 1")
        assert opened.splitlines()[1] == "Size 20 · Risk $100 · SL 105"

    def test_the_open_carries_its_signals_time(self, make, tg, clock, tmp_path, monkeypatch):
        bot = make(templates={"open": "{signal_time} | {candle_time}"})
        t, _ = live_trader([bot], tmp_path, monkeypatch=monkeypatch,
                           signals={_T0: [("long", 110.0, 105.0, None)]},
                           ticks=[_t_tick([_t_candle(0)])])
        events: list = []
        t.events.subscribe(events.append)
        run_until(t, lambda: len(t.open_trades) == 1)
        (opened,) = [e for e in events if isinstance(e, OpenEvent)]
        assert opened.signal_time == _T0
        stamp = format(_template.Moment(_T0 / 1000, None))
        assert f"{stamp} | {stamp}" in tg.texts(ME)    # Q2.8 b: the signal's candle

    def test_close_all_says_all_positions_closed(self, make, tg, clock, tmp_path,
                                                 monkeypatch):
        bot = make()
        _t_patch_scheduler(monkeypatch, [_t_tick([_t_candle(0)])])
        broker = _TFuturesBroker(_x_candles(10), fill_price=110.0)
        broker.ticker_last = 110.0
        t = Trader(broker=broker, config=_t_cfg(notifiers=[bot]),
                   strategy=_TPlanStrategy(signals_at={_T0: [("long", 110.0, 105.0, None)]}),
                   trader_id="t2", state_path=tmp_path / "state.json", on_stop="close_all")
        broker.mirror_engine = t.engine
        run_until(t, lambda: len(t.open_trades) == 1)
        assert tg.texts(ME)[-1].splitlines()[0] == (
            "<b>⏹ Live trading stopped · BTCUSDT · stop() · all positions closed</b>")

    def test_a_failed_start_sends_the_stop(self, make, tg, clock, tmp_path, monkeypatch):
        bot = make()
        t, broker = live_trader([bot], tmp_path, monkeypatch=monkeypatch)
        broker.seed_candles = _x_candles(3)           # shorter than min_candles=10
        with pytest.raises(ValueError, match="min_candles"):
            t.run()
        (text,) = tg.texts(ME)
        assert text.startswith("<b>⏹ Live trading stopped by an error · BTCUSDT · ValueError: "
                               "Trader: BTCUSDT 1h returned 3 candles")
        assert "position" not in text                  # no positions were read

    def test_a_stale_kill_switch_refuses_with_nothing_sent(self, make, tg, clock, tmp_path,
                                                           monkeypatch):
        bot = make()
        kill = tmp_path / "kill"
        kill.write_text("")
        _t_patch_scheduler(monkeypatch, [])
        broker = _TFuturesBroker(_x_candles(10), fill_price=110.0)
        t = Trader(broker=broker, strategy=_TPlanStrategy(), config=_t_cfg(notifiers=[bot]),
                   trader_id="t2", state_path=tmp_path / "state.json", kill_switch_file=kill)
        with pytest.raises(ValueError, match="kill_switch_file"):
            t.run()
        drained(bot)
        assert tg.answered == []

    def test_the_kill_switch_names_itself(self, make, tg, clock, tmp_path, monkeypatch):
        bot = make()
        kill = tmp_path / "kill"
        _t_patch_scheduler(monkeypatch, [])
        broker = _TFuturesBroker(_x_candles(10), fill_price=110.0)
        t = Trader(broker=broker, strategy=_TPlanStrategy(), config=_t_cfg(notifiers=[bot]),
                   trader_id="t2", state_path=tmp_path / "state.json", kill_switch_file=kill)
        threading.Timer(0.3, kill.write_text, args=("",)).start()
        t.run()
        assert tg.texts(ME)[-1].splitlines()[0] == (
            "<b>⏹ Live trading stopped · BTCUSDT · kill switch · no positions open</b>")

    def test_a_reconcile_comes_after_the_start(self, make, tg, clock, tmp_path, monkeypatch):
        bot = make()
        t, _ = live_trader([bot], tmp_path, monkeypatch=monkeypatch)
        events: list = []
        t.events.subscribe(events.append)
        original = t._worker.start

        def start_with_a_reconcile():
            original()
            t._worker.events.emit(ReconcileEvent(
                time=_T0, symbol="BTCUSDT", source=SOURCE_LIVE, adopted=("a", "b"),
                closed_offline=("c",)))

        t._worker.start = start_with_a_reconcile
        run_until(t, lambda: any(isinstance(e, ReconcileEvent) for e in events))
        kinds = [e.event_type for e in events]
        assert kinds.index("start") < kinds.index("reconcile")
        assert tg.texts(ME)[1].splitlines()[1] == "Adopted 2 · closed while offline 1 · foreign 0"

    def test_strategy_messages_only_on_closed_candles(self, make, tg, clock, tmp_path,
                                                      monkeypatch):
        bot = make()

        class Talking(_TPlanStrategy):
            def generate_signals(self, i, data):
                self.notify("closed or forming? {price}")
                return []

        broker = _TFuturesBroker(_x_candles(10), fill_price=110.0)
        t = Trader(broker=broker, strategy=Talking(),
                   config=_t_cfg(notifiers=[bot], execution="candle_update"),
                   trader_id="t2", state_path=tmp_path / "state.json")
        worker = t._worker
        worker.seed()                     # the seed: silent
        worker.engine.start()
        forming = dict(_t_candle(1, close=112.0), closed=False)
        worker._process_forming(forming)  # a forming evaluation: silent
        worker._process_scheduler_tick(_t_tick([_t_candle(0, close=111.0)]))
        drained(bot)
        assert tg.texts(ME) == [
            "<b>💬 BTCUSDT · 1h</b>\nclosed or forming? 111\n🕑 2026-10-14 14:00 UTC · LIVE"]
        assert worker.strategy._notify_hook is None

    def test_the_display_copy_never_speaks(self, make, tmp_path):
        bot = make()
        broker = _TFuturesBroker(_x_candles(10), fill_price=110.0)
        t = Trader(broker=broker, strategy=_TPlanStrategy(), trader_id="t2",
                   config=_t_cfg(notifiers=[bot], display=True, display_candles=10),
                   state_path=tmp_path / "state.json")
        assert t._worker.display._sim_strategy._notify_hook is adapter_module.mute_hook

    def test_price_is_the_forming_rows_in_realtime_modes(self, make, tmp_path):
        bot = make()
        broker = _TFuturesBroker(_x_candles(10), fill_price=110.0)
        t = Trader(broker=broker, strategy=_TPlanStrategy(), trader_id="t2",
                   config=_t_cfg(notifiers=[bot], execution="tick"),
                   state_path=tmp_path / "state.json")
        worker = t._worker
        worker.seed()
        worker.engine.start()
        (adapter,) = t._notify_session.adapters
        assert adapter.view.last_price() == 109.0          # the seed's last close
        worker._process_forming(dict(_t_candle(1, close=113.5), closed=False))
        assert adapter.view.last_price() == 113.5

    def test_subscribing_a_notifier_to_the_stream(self, make, tg, clock, tmp_path,
                                                  monkeypatch):
        bot = make(templates={"signal": "{symbol} {price} {entry_price}"})
        t, _ = live_trader([], tmp_path, monkeypatch=monkeypatch,
                           signals={_T0: [("long", 110.0, 105.0, None)]},
                           ticks=[_t_tick([_t_candle(0)])])
        t.events.subscribe(bot)
        run_until(t, lambda: len(t.open_trades) == 1)
        drained(bot)                                       # not closed by the Trader
        texts = tg.texts(ME)
        assert "BTCUSDT — 110" in texts                    # a stream carries no price
        assert texts[0].startswith("<b>▶️ Live trading started · BTCUSDT</b>")
        assert texts[-1].startswith("<b>⏹ Live trading stopped · BTCUSDT · stop() · "
                                    "1 position left open")

    def test_a_bad_template_on_the_stream_raises_there(self, make):
        bot = make(templates={"close": "{netpnl}"})
        with pytest.raises(ValueError, match=r"template for 'close' uses \{netpnl\}"):
            EventStream().subscribe(bot)


# ===========================================================================
# Every kind's default text, from the events themselves
# ===========================================================================

class TestEveryDefault:
    def send(self, make, tg, *events, **chats):
        bot = make({"me": TelegramChat(ME, events=set(_core.KINDS) - {KIND_TEXT})})
        stream = EventStream()
        stream.subscribe(bot)
        for event in events:
            stream.emit(event)
        drained(bot)
        return tg.texts(ME)

    def test_exit_signal_daily_loss_reconcile_and_error(self, make, tg):
        common = {"time": ms(at(13)), "symbol": "BTCUSDT", "source": SOURCE_LIVE}
        texts = self.send(
            make, tg,
            ExitSignalEvent(**common, reason="macd_cross_down", action="force_close"),
            ExitSignalEvent(**common, reason="macd_cross_down", action="none"),
            DailyLossEvent(**common, limit=500.0, loss=512.4, closed_all=True),
            DailyLossEvent(**common, limit="2%", loss=512.4),
            ReconcileEvent(**common, adopted=("x", "y"), closed_offline=("z",)),
            ErrorEvent(**common, where="create_order", message="Margin is insufficient.",
                       will_retry=True),
        )
        clock_line = "🕑 2026-10-14 14:00 UTC · LIVE"
        assert texts == [
            f"<b>🚪 EXIT SIGNAL · BTCUSDT</b>\nmacd_cross_down → position closed\n{clock_line}",
            "<b>🚪 EXIT SIGNAL · BTCUSDT</b>\nmacd_cross_down → no action "
            f"(force_close_on_exit_signal is off)\n{clock_line}",
            "<b>⛔ DAILY LOSS LIMIT · BTCUSDT</b>\nLoss $512.40 of $500 · no new entries until "
            f"the next UTC day · closing all positions\n{clock_line}",
            "<b>⛔ DAILY LOSS LIMIT · BTCUSDT</b>\nLoss $512.40 of 2% · no new entries until "
            f"the next UTC day\n{clock_line}",
            "<b>🔄 RESTART RECONCILED · BTCUSDT</b>\nAdopted 2 · closed while offline 1 · "
            f"foreign 0\n{clock_line}",
            "<b>⚠️ ERROR · BTCUSDT</b>\ncreate_order: Margin is insufficient. (will retry)\n"
            f"{clock_line}",
        ]

    def test_a_close_from_before_the_session_and_its_reasons(self, make, tg):
        common = {"time": ms(at(13)), "symbol": "BTCUSDT", "source": SOURCE_LIVE}
        texts = self.send(
            make, tg,
            CloseEvent(**common, trade_id=7, exit_price=63_800.0, reason="manual",
                       gross_pnl=-100.0, net_pnl=-100.0, pnl_r=-1.0, duration_ms=15_300_000),
            CloseEvent(**common, trade_id=8, exit_price=1.0, reason="weird", gross_pnl=0.0,
                       net_pnl=0.0, pnl_r=0.0, duration_ms=30_000),
        )
        assert texts[0] == ("<b>🏁 CLOSED #7 · BTCUSDT @ 63,800 — closed on the venue</b>\n"
                            "Net -$100.00 (-1.00 R) · held 4h 15m\n🕑 2026-10-14 14:00 UTC · LIVE")
        assert texts[1].splitlines()[0] == "<b>🏁 CLOSED #8 · BTCUSDT @ 1 — weird</b>"

    def test_a_signal_on_the_bare_stream(self, make, tg):
        texts = self.send(make, tg, SignalEvent(
            time=ms(at(13)), symbol="BTCUSDT", source=SOURCE_SIM, direction="long",
            entry_price=64_250.0, stop_loss=63_800.0, take_profit=65_150.0, rr=2.0,
            timeframe="1h"))
        assert texts == [SIGNAL]


# ===========================================================================
# Q2.4 — after an outage, late messages say so
# ===========================================================================

class TestLate:
    def test_q24_down_from_1402_to_1414(self, make, tg):
        """The SIGNAL and OPEN raised at 14:03, the CLOSE at 14:11; Telegram is
        unreachable from 14:02 to 14:14.  At 14:14 they go in order, marked."""
        manual = FakeClock(at(14, 1))
        bot = make(on=manual)
        seed = [candle(at(13, 52 + k), 64_250, 64_260, 64_240, 64_250) for k in range(5)]
        seed += [candle(at(13, 57 + k), 64_250, 64_260, 64_240, 64_250) for k in range(5)]
        live = [candle(at(14, 2), 64_250, 64_260, 64_240, 64_250)]          # 14:03: signal, open
        live += [candle(at(14, 2 + k), 64_250, 64_260, 64_240, 64_250) for k in range(1, 8)]
        live.append(candle(at(14, 10), 64_250, 65_200, 64_240, 65_100))     # 14:11: the close

        class Outage(SteppedBroker):
            def stream_candles(self, symbol, timeframe, on_candle, *, closed_only=True):
                settle(bot)
                tg.stop()                                                   # 14:02
                for row in self.live_rows:
                    manual.wall = (row["timestamp"] + MINUTE) / 1000
                    manual.mono += 60
                    on_candle({**row, "closed": True})
                    settle(bot)
                manual.wall = ms(at(14, 14)) / 1000
                manual.mono += 180
                tg.start()                                                  # 14:14
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    step(bot, manual, 1.0)
                    with bot._lock:
                        if not bot._waiting and not bot._inbox:
                            break
                step(bot, manual, 5.0)
                stream = _PaperStream(alive=False)
                self.streams.append(stream)
                self.subscribed.set()
                return stream

        broker = Outage(seed, live, clock=manual, tf_ms=MINUTE)
        strategy = Plan({ms(at(14, 2)): [("long", 64_250.0, 63_800.0, 65_150.0)]},
                        timeframe="1m")
        run_live(strategy=strategy, broker=broker, config=paper_cfg([bot]))
        firsts = [text.splitlines()[:2] for text in tg.texts(ME)]
        assert firsts[1] == ["⏱ 11 min late", "<b>🟢 LONG BTCUSDT · 1m</b>"]
        assert firsts[2] == ["⏱ 11 min late", "<b>✅ OPENED #0 · LONG BTCUSDT @ 64,250</b>"]
        assert firsts[3][0] == "⏱ 3 min late"
        assert firsts[3][1].startswith("<b>🏁 CLOSED #0")


# ===========================================================================
# Trading never waits
# ===========================================================================

class TestTradingNeverWaits:
    """With a Telegram that takes 5 seconds per request, a candle is processed
    within 50 ms of the time it takes with no notifier."""

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
        tg.when("sendMessage", delay=5.0)
        cost = {}
        for label, bots in (("bare", []), ("notified", [bot])):
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
        assert cost["notified"] - cost["bare"] < self.BOUND, cost

    def test_trader_candle(self, make, tg, tmp_path):
        bot = make(check=False, on=FakeClock(at(14)), timeout=10.0)
        tg.when("sendMessage", delay=5.0)
        cost = {}
        for label, bots in (("bare", []), ("notified", [bot])):
            broker = _TFuturesBroker(_x_candles(10), fill_price=110.0)
            broker.ticker_last = 110.0
            signals = {_T0 + k * _XHOUR: [("long", 110.0, 105.0, None)] for k in range(5)}
            t = Trader(broker=broker, strategy=_TPlanStrategy(signals_at=signals),
                       config=_t_cfg(notifiers=bots, max_positions=99, max_long_positions=99),
                       trader_id="t2", state_path=tmp_path / f"{label}.json")
            broker.mirror_engine = t.engine
            worker = t._worker
            worker.seed()
            worker.engine.start()
            k = iter(range(5))
            cost[label] = self.timed(
                lambda worker=worker, k=k: worker._process_scheduler_tick(
                    _t_tick([_t_candle(next(k))])))
        assert cost["notified"] - cost["bare"] < self.BOUND, cost
