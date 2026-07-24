"""Tests for the ``broker`` module — fully mocked, no network access."""
from __future__ import annotations

import itertools

import pytest

import AlgoTradeKit.broker.base as broker_base
from AlgoTradeKit.broker import (
    COMMISSION_TYPE_FIXED,
    COMMISSION_TYPE_PER_LOT,
    COMMISSION_TYPE_PERCENTAGE,
    MARKET_FUTURES,
    ORDER_MARKET,
    ORDER_TAKE_PROFIT,
    POSITION_LONG,
    SIDE_BUY,
    AuthenticationError,
    BaseBroker,
    Broker,
    ConnectionFailed,
    NotSupportedError,
    OrderError,
    OrderResult,
    TradingCosts,
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


# ---------------------------------------------------------------------------
# — take-profit order type + spot OCO order lists
# ---------------------------------------------------------------------------

def _recording_broker(market: str, payload=None):
    b = BinanceBroker(market, api_key="k", api_secret="s")
    calls = []

    def fake_signed(method, url, params=None):
        calls.append((method, url, params))
        return payload if payload is not None else {"orderId": 42, "status": "NEW"}

    b._rest.signed = fake_signed
    return b, calls


def test_futures_take_profit_order_params():
    b, calls = _recording_broker("futures")
    res = b.create_order("BTCUSDT", SIDE_BUY, 1.5, type=ORDER_TAKE_PROFIT,
                         stop_price=120.0, reduce_only=True)
    assert isinstance(res, OrderResult)
    p = calls[0][2]
    assert p["type"] == "TAKE_PROFIT_MARKET"
    assert p["stopPrice"] == "120" and p["reduceOnly"] == "true"


def test_spot_take_profit_order_params():
    b, calls = _recording_broker("spot")
    b.create_order("BTCUSDT", "sell", 1.0, type=ORDER_TAKE_PROFIT, stop_price=120.0)
    p = calls[0][2]
    assert p["type"] == "TAKE_PROFIT" and p["stopPrice"] == "120"


def test_take_profit_requires_stop_price():
    b, calls = _recording_broker("futures")
    with pytest.raises(OrderError):
        b.create_order("BTCUSDT", "sell", 1.0, type=ORDER_TAKE_PROFIT)
    assert calls == []


def test_spot_oco_sell_params():
    payload = {"orderListId": 777, "listClientOrderId": "atk-x", "listOrderStatus": "EXECUTING"}
    b, calls = _recording_broker("spot", payload)
    res = b.create_oco_order("btcusdt", "sell", 0.5, take_profit_price=120.0,
                             stop_price=90.0, client_order_id="atk-x")
    method, url, p = calls[0]
    assert method == "POST" and url.endswith("/api/v3/orderList/oco")
    # SELL exit: TP (LIMIT_MAKER) above, SL (STOP_LOSS) below
    assert p["side"] == "SELL" and p["symbol"] == "BTCUSDT" and p["quantity"] == "0.5"
    assert p["aboveType"] == "LIMIT_MAKER" and p["abovePrice"] == "120"
    assert p["belowType"] == "STOP_LOSS" and p["belowStopPrice"] == "90"
    assert p["listClientOrderId"] == "atk-x"
    assert res.order_id == "777" and res.client_order_id == "atk-x"


def test_spot_oco_stop_limit_leg():
    b, calls = _recording_broker("spot", {"orderListId": 1})
    b.create_oco_order("BTCUSDT", "sell", 1.0, take_profit_price=120.0,
                       stop_price=90.0, stop_limit_price=89.5)
    p = calls[0][2]
    assert p["belowType"] == "STOP_LOSS_LIMIT"
    assert p["belowStopPrice"] == "90" and p["belowPrice"] == "89.5"
    assert p["belowTimeInForce"] == "GTC"


def test_spot_oco_buy_side_assignment():
    b, calls = _recording_broker("spot", {"orderListId": 2})
    b.create_oco_order("BTCUSDT", "buy", 1.0, take_profit_price=80.0, stop_price=110.0)
    p = calls[0][2]
    # BUY exit: SL (STOP_LOSS) above, TP (LIMIT_MAKER) below
    assert p["side"] == "BUY"
    assert p["aboveType"] == "STOP_LOSS" and p["aboveStopPrice"] == "110"
    assert p["belowType"] == "LIMIT_MAKER" and p["belowPrice"] == "80"


def test_oco_and_order_list_futures_raise():
    b, calls = _recording_broker("futures")
    with pytest.raises(NotSupportedError):
        b.create_oco_order("BTCUSDT", "sell", 1.0, take_profit_price=120.0, stop_price=90.0)
    with pytest.raises(NotSupportedError):
        b.cancel_order_list("BTCUSDT", "777")
    assert calls == []


def test_cancel_order_list_spot():
    b, calls = _recording_broker("spot", {"orderListId": 777, "listOrderStatus": "ALL_DONE"})
    assert b.cancel_order_list("btcusdt", "777") is True
    method, url, p = calls[0]
    assert method == "DELETE" and url.endswith("/api/v3/orderList")
    assert p == {"symbol": "BTCUSDT", "orderListId": "777"}


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
        if method == "close_position":
            return {"retcode": 10009, "deal": 777, "volume": args[2] or 0.1, "price": 1.25}
        if method == "history_deals":
            return [{"ticket": 1, "position_id": 999, "entry": 1, "price": 1.25,
                     "reason": 4, "time": 1700000500, "time_msc": 1700000500123,
                     "volume": 0.1}]
        if method == "positions":
            return [{"type": 0, "volume": 0.1, "price_open": 1.2, "price_current": 1.25,
                     "profit": 5.0, "sl": 1.1, "tp": 1.4, "ticket": 999,
                     "symbol": "EURUSD", "time": 1700000000}]
        if method == "tick":
            return {"bid": 1.2, "ask": 1.2002, "last": 0.0, "time_ms": 1700000000000}
        if method == "symbols":
            return ["EURUSD", "BTCUSD", "XAUUSD"]
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

    assert "BTCUSD" in mt.list_symbols("*")   # symbol discovery (used by demo.py)


def test_metatrader_close_position_ticket_passthrough():
    #: the live trader closes one specific position by ticket.
    tr = _FakeMTTransport(login=123)
    mt = MetaTraderBroker(transport=tr)
    res = mt.close_position("EURUSD", 0.1, ticket=999)
    assert res.status == "filled"
    assert ("close_position", (999, "EURUSD", 0.1), {}) in tr.calls
    mt.close_position("EURUSD")           # default: whole symbol position, no ticket
    assert ("close_position", (None, "EURUSD", None), {}) in tr.calls


def test_metatrader_history_deals_passthrough():
    #: the live trader reads a closed position's real exit fill by ticket.
    tr = _FakeMTTransport(login=123)
    mt = MetaTraderBroker(transport=tr)
    deals = mt.history_deals(position=999)
    assert ("history_deals", (0, 0, 999), {}) in tr.calls
    assert deals[0]["price"] == 1.25 and deals[0]["reason"] == 4
    mt.history_deals(1_000, 2_000)                    # range form
    assert ("history_deals", (1000, 2000, None), {}) in tr.calls
    with pytest.raises(ValueError):
        mt.history_deals(1_000)                       # range needs both ends


def test_metatrader_auth_guard_when_not_logged_in():
    tr = _FakeMTTransport(login=0)   # not logged in
    mt = MetaTraderBroker(transport=tr)
    assert not mt.is_authenticated
    with pytest.raises(AuthenticationError):
        mt.get_balance()
    with pytest.raises(AuthenticationError):
        mt.history_deals(position=999)


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


# ---------------------------------------------------------------------------
# — get_trading_costs: Binance venues + BaseBroker default
# ---------------------------------------------------------------------------

_BOOK = {"bidPrice": "99.0", "askPrice": "101.0"}


def _with_book(broker, seen=None):
    """Stub the public book-ticker call (the only public call costs make)."""
    def fake_public(method, url, params=None):
        if seen is not None:
            seen["url"], seen["params"] = url, params
        return dict(_BOOK)
    broker._rest.public = fake_public
    return broker


def test_trading_costs_spot_unauthenticated_standard_fee():
    seen = {}
    b = _with_book(BinanceBroker("spot"), seen)
    tc = b.get_trading_costs("btcusdt")
    assert seen["url"].endswith("/ticker/bookTicker")
    assert seen["params"] == {"symbol": "BTCUSDT"}
    assert isinstance(tc, TradingCosts)
    assert tc.commission_type == COMMISSION_TYPE_PERCENTAGE
    assert tc.commission == 0.001                     # standard spot taker fee
    assert tc.spread == pytest.approx(2.0)            # ask − bid, price units
    assert tc.contract_size is None                   # crypto sizes in base asset
    assert tc.raw["commission"] == {"source": "standard_taker_fee", "authenticated": False}
    assert tc.raw["book"] == _BOOK


def test_trading_costs_futures_unauthenticated_standard_fee():
    tc = _with_book(BinanceBroker("futures")).get_trading_costs("BTCUSDT")
    assert tc.commission == 0.0005                    # standard futures taker fee


def test_trading_costs_futures_authenticated_commission_endpoint():
    b = _with_book(BinanceBroker("futures", api_key="k", api_secret="s"))
    signed_calls = []

    def fake_signed(method, url, params=None):
        signed_calls.append((method, url, params))
        return {"symbol": "BTCUSDT", "makerCommissionRate": "0.000200",
                "takerCommissionRate": "0.000400"}

    b._rest.signed = fake_signed
    tc = b.get_trading_costs("btcusdt")
    assert signed_calls == [("GET", b._ep.commission_rate, {"symbol": "BTCUSDT"})]
    assert tc.commission == pytest.approx(0.0004)     # taker — entries are market orders
    assert tc.raw["commission"]["takerCommissionRate"] == "0.000400"


def test_trading_costs_spot_authenticated_commission_rates():
    b = _with_book(BinanceBroker("spot", api_key="k", api_secret="s"))
    signed_calls = []

    def fake_signed(method, url, params=None):
        signed_calls.append(url)
        return {"commissionRates": {"maker": "0.00100000", "taker": "0.00075000"},
                "balances": []}

    b._rest.signed = fake_signed
    tc = b.get_trading_costs("BTCUSDT")
    assert signed_calls == [b._ep.account]
    assert tc.commission == pytest.approx(0.00075)


def test_trading_costs_spot_legacy_basis_points():
    b = _with_book(BinanceBroker("spot", api_key="k", api_secret="s"))
    b._rest.signed = lambda method, url, params=None: {"takerCommission": 10, "balances": []}
    tc = b.get_trading_costs("BTCUSDT")
    assert tc.commission == pytest.approx(0.001)      # 10 basis points


def test_trading_costs_authed_commission_failure_falls_back():
    b = _with_book(BinanceBroker("futures", api_key="k", api_secret="s"))

    def fake_signed(method, url, params=None):
        raise OrderError("Binance rejected the request (400): no permission")

    b._rest.signed = fake_signed
    tc = b.get_trading_costs("BTCUSDT")
    assert tc.commission == 0.0005                    # standard fee, not an exception
    assert tc.raw["commission"]["source"] == "standard_taker_fee"
    assert "no permission" in tc.raw["commission"]["error"]


def test_trading_costs_connection_failure_propagates():
    # Venue unreachable is a real problem — never silently defaulted.
    b = BinanceBroker("futures", api_key="k", api_secret="s")

    def raise_conn(method, url, params=None):
        raise ConnectionFailed("Cannot reach Binance (futures).")

    b._rest.public = raise_conn                       # book-ticker leg
    with pytest.raises(ConnectionFailed):
        b.get_trading_costs("BTCUSDT")

    b = _with_book(BinanceBroker("futures", api_key="k", api_secret="s"))
    b._rest.signed = raise_conn                       # commission leg
    with pytest.raises(ConnectionFailed):
        b.get_trading_costs("BTCUSDT")


class _StubBroker(BaseBroker):
    """Minimal concrete BaseBroker: default get_trading_costs / clock_offset_ms."""

    name = "stub-venue"

    def __init__(self, server_time_fn=None):
        self._server_time_fn = server_time_fn

    def fetch_candles(self, symbol, timeframe, start_ms, end_ms):
        return []

    def get_ticker(self, symbol):
        raise NotImplementedError

    def server_time(self):
        return self._server_time_fn()

    def get_balance(self):
        return []

    def create_order(self, symbol, side, quantity, **kwargs):
        raise NotImplementedError

    def cancel_order(self, order_id, symbol):
        return True

    def open_orders(self, symbol=None):
        return []


def test_base_broker_default_trading_costs():
    tc = _StubBroker().get_trading_costs("ANYTHING")
    assert tc == TradingCosts(
        commission_type=COMMISSION_TYPE_PERCENTAGE, commission=0.0, spread=0.0,
        contract_size=None, raw=tc.raw,
    )
    assert tc.raw["source"] == "default"
    assert "stub-venue" in tc.raw["note"]


def test_commission_type_constants_match_simulate():
    # broker deliberately re-declares these (it may not import simulate);
    # the values must stay identical so TradingCosts drops into SimulateConfig.
    from AlgoTradeKit.simulate import (
        COMMISSION_TYPE_FIXED as SIM_FIXED,
    )
    from AlgoTradeKit.simulate import (
        COMMISSION_TYPE_PER_LOT as SIM_PER_LOT,
    )
    from AlgoTradeKit.simulate import (
        COMMISSION_TYPE_PERCENTAGE as SIM_PERCENTAGE,
    )
    assert COMMISSION_TYPE_PERCENTAGE == SIM_PERCENTAGE
    assert COMMISSION_TYPE_PER_LOT == SIM_PER_LOT
    assert COMMISSION_TYPE_FIXED == SIM_FIXED


# ---------------------------------------------------------------------------
# — clock_offset_ms: sampling, midpoint, cache, TTL
# ---------------------------------------------------------------------------

def test_clock_offset_midpoint_and_median(monkeypatch):
    # Local clock reads 0 before and 200 after every server_time() round trip,
    # so each sample compares against the midpoint (100).  One wild outlier
    # must not move the result: the median wins.
    ticks = itertools.cycle([0, 200])
    monkeypatch.setattr(broker_base, "now_ms", lambda: next(ticks))
    server_values = iter([1100, 1100, 1100, 1100, 900_000])
    b = _StubBroker(server_time_fn=lambda: next(server_values))
    assert b.clock_offset_ms() == 1000                # 1100 − 100, outlier ignored


def test_clock_offset_cached_ttl_and_force_refresh(monkeypatch):
    clock = {"t": 1_000_000}
    monkeypatch.setattr(broker_base, "now_ms", lambda: clock["t"])
    calls = {"n": 0}

    def server_time():
        calls["n"] += 1
        return clock["t"] + 500

    b = _StubBroker(server_time_fn=server_time)
    samples = broker_base._CLOCK_OFFSET_SAMPLES

    assert b.clock_offset_ms() == 500                 # measured: N samples
    assert calls["n"] == samples
    clock["t"] += 1_000                               # well inside the TTL
    assert b.clock_offset_ms() == 500                 # cache hit — no new calls
    assert calls["n"] == samples
    assert b.clock_offset_ms(force_refresh=True) == 500
    assert calls["n"] == 2 * samples                  # forced re-measure
    clock["t"] += broker_base._CLOCK_OFFSET_TTL_MS + 1
    assert b.clock_offset_ms() == 500                 # stale → automatic re-measure
    assert calls["n"] == 3 * samples


# ---------------------------------------------------------------------------
# — my_trades: account trade-fill history (offline-close reconciliation)
# ---------------------------------------------------------------------------

def test_my_trades_futures_url_and_params():
    b = BinanceBroker("futures", api_key="k", api_secret="s")
    calls = []

    def fake_signed(method, url, params=None):
        calls.append((method, url, params))
        return [{"orderId": 42, "price": "105.2", "qty": "12.0", "time": 1_700_000_000_000}]

    b._rest.signed = fake_signed
    rows = b.my_trades("btcusdt", start_ms=1_700_000_000_000,
                       end_ms=1_700_000_400_000, from_id=7, limit=500)
    assert calls == [("GET", b._ep.my_trades,
                      {"symbol": "BTCUSDT", "startTime": 1_700_000_000_000,
                       "endTime": 1_700_000_400_000, "fromId": 7, "limit": 500})]
    assert b._ep.my_trades.endswith("/fapi/v1/userTrades")
    assert rows[0]["orderId"] == 42                    # raw venue dicts pass through


def test_my_trades_spot_url_and_minimal_params():
    b = BinanceBroker("spot", api_key="k", api_secret="s")
    calls = []
    b._rest.signed = lambda method, url, params=None: calls.append((method, url, params)) or []
    b.my_trades("ETHUSDT")
    assert calls == [("GET", b._ep.my_trades, {"symbol": "ETHUSDT"})]
    assert b._ep.my_trades.endswith("/api/v3/myTrades")


def test_my_trades_requires_auth():
    with pytest.raises(AuthenticationError):
        BinanceBroker("futures").my_trades("BTCUSDT")
