"""Tests for the ``broker`` module — fully mocked, no network access."""
from __future__ import annotations

import pytest

from AlgoTradeKit.broker import (
    MARKET_FUTURES,
    ORDER_MARKET,
    POSITION_LONG,
    SIDE_BUY,
    AuthenticationError,
    Broker,
    NotSupportedError,
    OrderResult,
)
from AlgoTradeKit.broker.exchange.binance import BinanceBroker
from AlgoTradeKit.broker.exchange.binance._client import _num
from AlgoTradeKit.broker.exchange.binance._endpoints import resolve_endpoints
from AlgoTradeKit.broker.exchange.binance._rest import BinanceREST
from AlgoTradeKit.broker.metatrader import MetaTraderBroker


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

def _kline(ts: int) -> list:
    return [ts, "1.0", "2.0", "0.5", "1.5", "10.0", ts + 59_999,
            "100.0", 5, "5.0", "50.0"]


class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.headers = {}
        self.text = ""

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


class _FakeSession:
    """Captures the final request URL so we can assert on signing."""

    def __init__(self, payload=None):
        self.headers = {}
        self.captured = []
        self._payload = payload if payload is not None else {}

    def request(self, method, url, timeout=None):
        self.captured.append((method, url))
        return _FakeResp(self._payload)

    def close(self):
        pass


# ---------------------------------------------------------------------------
# Numbers / signing
# ---------------------------------------------------------------------------

def test_num_formatting():
    assert _num(0.001) == "0.001"
    assert _num(1.0) == "1"
    assert _num(0.00000001) == "0.00000001"
    assert _num(12345.6789) == "12345.6789"


def test_signed_request_appends_signature_and_timestamp():
    ep = resolve_endpoints("spot", False)
    rest = BinanceREST(ep, api_key="key", api_secret="secret")
    fake = _FakeSession({"ok": True})
    rest._session = fake

    rest.signed("GET", ep.account, {"foo": "bar"})
    _, url = fake.captured[-1]
    assert "signature=" in url
    assert "timestamp=" in url
    assert "recvWindow=" in url
    assert "foo=bar" in url


def test_public_request_has_no_signature():
    ep = resolve_endpoints("spot", False)
    rest = BinanceREST(ep)
    fake = _FakeSession([])
    rest._session = fake
    rest.public("GET", ep.klines, {"symbol": "BTCUSDT"})
    _, url = fake.captured[-1]
    assert "signature=" not in url


def test_rest_requires_credentials_for_signed():
    ep = resolve_endpoints("spot", False)
    rest = BinanceREST(ep)  # no creds
    with pytest.raises(AuthenticationError):
        rest.signed("GET", ep.account, {})


# ---------------------------------------------------------------------------
# BinanceBroker — market data
# ---------------------------------------------------------------------------

def test_fetch_candles_paginates_and_parses():
    b = BinanceBroker("spot")
    pages = [[_kline(i * 60_000) for i in range(1000)], [_kline(1000 * 60_000)]]

    def fake_fetch_klines(symbol, interval, start, end, limit):
        return pages.pop(0) if pages else []

    b._rest.fetch_klines = fake_fetch_klines
    rows = b.fetch_candles("BTCUSDT", "1m", 0, 10**12)
    assert len(rows) == 1001
    assert rows[0]["open"] == 1.0
    assert rows[0]["trades"] == 5
    assert set(rows[0]) >= {"timestamp", "open", "high", "low", "close", "volume"}


def test_get_ticker():
    b = BinanceBroker("spot")

    def fake_public(method, url, params=None):
        if url.endswith("ticker/price"):
            return {"price": "100.0"}
        return {"bidPrice": "99.0", "askPrice": "101.0"}

    b._rest.public = fake_public
    t = b.get_ticker("btcusdt")
    assert t.last == 100.0 and t.bid == 99.0 and t.ask == 101.0
    assert t.symbol == "BTCUSDT"


def test_stream_candles_handler_parses_closed(monkeypatch):
    import AlgoTradeKit.broker.exchange.binance._client as mod

    captured = {}

    def fake_stream_kline(ep, symbol, interval, handler):
        captured["handler"] = handler
        return "stream-handle"

    monkeypatch.setattr(mod, "stream_kline", fake_stream_kline)
    b = BinanceBroker("futures")
    received = []
    b.stream_candles("BTCUSDT", "1m", received.append, closed_only=True)

    # open (not closed) candle is filtered out
    captured["handler"]({"k": {"t": 0, "o": "1", "h": "2", "l": "0", "c": "1.5",
                               "v": "9", "T": 59999, "x": False}})
    assert received == []
    # closed candle passes through, parsed
    captured["handler"]({"k": {"t": 60000, "o": "1", "h": "2", "l": "0", "c": "1.5",
                               "v": "9", "T": 119999, "x": True}})
    assert len(received) == 1 and received[0]["closed"] is True
    assert received[0]["close"] == 1.5


# ---------------------------------------------------------------------------
# BinanceBroker — trading
# ---------------------------------------------------------------------------

def test_spot_market_order_params():
    b = BinanceBroker("spot", api_key="k", api_secret="s")
    calls = []

    def fake_signed(method, url, params=None):
        calls.append((method, url, params))
        return {"orderId": 1, "status": "FILLED", "executedQty": "0.5",
                "fills": [{"qty": "0.5", "price": "100.0"}]}

    b._rest.signed = fake_signed
    res = b.create_market_order("btcusdt", "buy", 0.5)
    assert isinstance(res, OrderResult) and res.is_filled
    assert res.avg_fill_price == 100.0
    p = calls[0][2]
    assert p["type"] == "MARKET" and p["side"] == "BUY" and p["quantity"] == "0.5"


def test_futures_order_attaches_sl_tp():
    b = BinanceBroker("futures", api_key="k", api_secret="s")
    calls = []

    def fake_signed(method, url, params=None):
        calls.append(params)
        return {"orderId": len(calls), "status": "NEW", "executedQty": "0"}

    b._rest.signed = fake_signed
    res = b.create_order("BTCUSDT", SIDE_BUY, 1.0, type=ORDER_MARKET,
                         stop_loss=90.0, take_profit=120.0)
    assert len(calls) == 3            # entry + SL + TP
    assert calls[0]["type"] == "MARKET"
    assert calls[1]["type"] == "STOP_MARKET" and calls[1]["side"] == "SELL"
    assert calls[1]["closePosition"] == "true"
    assert calls[2]["type"] == "TAKE_PROFIT_MARKET"
    assert "protective" in res.raw


def test_spot_sl_tp_rejected_before_send():
    b = BinanceBroker("spot", api_key="k", api_secret="s")
    calls = []
    b._rest.signed = lambda *a, **k: calls.append(a) or {"orderId": 1, "status": "NEW"}
    with pytest.raises(NotSupportedError):
        b.create_order("BTCUSDT", "buy", 1.0, stop_loss=90.0)
    assert calls == []                # nothing was sent


def test_futures_open_positions_parse():
    b = BinanceBroker("futures", api_key="k", api_secret="s")

    def fake_signed(method, url, params=None):
        return [
            {"symbol": "BTCUSDT", "positionAmt": "0.5", "entryPrice": "100",
             "markPrice": "110", "unRealizedProfit": "5", "leverage": "10",
             "liquidationPrice": "50"},
            {"symbol": "ETHUSDT", "positionAmt": "0", "entryPrice": "0"},  # flat → skipped
        ]

    b._rest.signed = fake_signed
    positions = b.open_positions()
    assert len(positions) == 1
    assert positions[0].side == POSITION_LONG and positions[0].quantity == 0.5


def test_spot_has_no_positions():
    b = BinanceBroker("spot", api_key="k", api_secret="s")
    assert b.open_positions() == []


def test_auth_guards_on_public_broker():
    b = BinanceBroker("spot")  # no creds
    assert not b.is_authenticated
    with pytest.raises(AuthenticationError):
        b.get_balance()
    with pytest.raises(AuthenticationError):
        b.create_market_order("BTCUSDT", "buy", 1.0)


# ---------------------------------------------------------------------------
# MetaTraderBroker (fake transport)
# ---------------------------------------------------------------------------

class _FakeMTTransport:
    def __init__(self, login=123):
        self._login = login
        self.calls = []

    def call(self, method, *args, **kwargs):
        self.calls.append((method, args, kwargs))
        if method == "login":
            return True
        if method == "account_info":
            return {"login": self._login, "currency": "USD", "balance": 1000.0,
                    "equity": 1010.0, "margin": 50.0, "margin_free": 960.0,
                    "profit": 10.0, "leverage": 100.0}
        if method == "candles_range":
            return [{"timestamp": 1000, "open": 1.0, "high": 2.0, "low": 0.5,
                     "close": 1.5, "volume": 10.0, "close_time": 1999,
                     "quote_volume": 0.0, "trades": 5,
                     "taker_buy_base": 0.0, "taker_buy_quote": 0.0}]
        if method == "candles_from_count":
            return [{"timestamp": 1000, "open": 1.0, "high": 2.0, "low": 0.5,
                     "close": 1.5, "volume": 10.0, "close_time": 1999,
                     "quote_volume": 0.0, "trades": 5,
                     "taker_buy_base": 0.0, "taker_buy_quote": 0.0}]
        if method == "place_order":
            self.last_spec = args[0]
            return {"retcode": 10009, "order": 555, "volume": 0.1, "price": 1.2345}
        if method == "positions":
            return [{"type": 0, "volume": 0.1, "price_open": 1.2, "price_current": 1.25,
                     "profit": 5.0, "sl": 1.1, "tp": 1.4, "ticket": 999,
                     "symbol": "EURUSD", "time": 1700000000}]
        if method == "tick":
            return {"bid": 1.2, "ask": 1.2002, "last": 0.0, "time_ms": 1700000000000}
        return None

    def close(self):
        pass


def test_metatrader_basic_flow():
    tr = _FakeMTTransport(login=123)
    mt = MetaTraderBroker(transport=tr)
    assert mt.is_authenticated
    assert mt.market == "forex" and mt.supports_positions

    rows = mt.fetch_candles("EURUSD", "15m", 0, 10**12)
    assert rows[0]["close"] == 1.5
    assert mt.fetch_last_candles("EURUSD", "15m", 1)[0]["open"] == 1.0

    res = mt.create_market_order("EURUSD", "buy", 0.1)
    assert res.order_id == "555" and res.filled_quantity == 0.1
    assert tr.last_spec["side"] == "buy" and tr.last_spec["order_kind"] == "market"

    positions = mt.open_positions()
    assert positions[0].side == POSITION_LONG and positions[0].quantity == 0.1

    info = mt.get_account_info()
    assert info.equity == 1010.0 and info.currency == "USD"


def test_metatrader_auth_guard_when_not_logged_in():
    tr = _FakeMTTransport(login=0)   # not logged in
    mt = MetaTraderBroker(transport=tr)
    assert not mt.is_authenticated
    with pytest.raises(AuthenticationError):
        mt.get_balance()


# ---------------------------------------------------------------------------
# Broker factory
# ---------------------------------------------------------------------------

def test_broker_factory():
    assert Broker("binance-spot").market == "spot"
    fut = Broker("binance-futures")
    assert fut.market == MARKET_FUTURES and fut.supports_positions
    assert Broker("mt5", connect=False).name == "metatrader"
    with pytest.raises(ValueError):
        Broker("nope-exchange")


def test_collector_accepts_broker_instance():
    from AlgoTradeKit.data import Collector
    b = Broker("binance-futures")
    c = Collector(b, "ETHUSDT", "1h")
    assert c._get_source() is b
    assert c._source_name == "binance-futures"
