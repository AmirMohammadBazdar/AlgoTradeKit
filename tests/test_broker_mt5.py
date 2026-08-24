"""
Tests for v1.0.0 — MetaTrader cross-platform (native transport, mode
selection, guided failure diagnostics, ``[mt5]`` packaging) and MT5
streaming via transport polling.

Everything is mocked: a fake ``MetaTrader5`` module stands in for the real
Windows package, and the bridge parity test runs a real TCP server around the
actual ``bridge_server`` protocol handlers.
"""
from __future__ import annotations

import copy
import platform
import socket
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

import AlgoTradeKit.broker.metatrader.bridge_server as bridge_server
from AlgoTradeKit.broker import (
    COMMISSION_TYPE_PER_LOT,
    Broker,
    BrokerError,
    ConnectionFailed,
    Stream,
    Ticker,
    TradingCosts,
)
from AlgoTradeKit.broker._stream import start_poll_stream
from AlgoTradeKit.broker.metatrader import BridgeClient, MetaTraderBroker, NativeTransport

# ---------------------------------------------------------------------------
# Fake MetaTrader5 module
# ---------------------------------------------------------------------------

_TF_NAMES = [
    "TIMEFRAME_M1", "TIMEFRAME_M3", "TIMEFRAME_M5", "TIMEFRAME_M15", "TIMEFRAME_M30",
    "TIMEFRAME_H1", "TIMEFRAME_H2", "TIMEFRAME_H4", "TIMEFRAME_H6", "TIMEFRAME_H8",
    "TIMEFRAME_H12", "TIMEFRAME_D1", "TIMEFRAME_W1", "TIMEFRAME_MN1",
]


class _NT:
    """Namedtuple-ish stand-in: attribute access + ``_asdict()``."""

    def __init__(self, **kw):
        self.__dict__.update(kw)

    def _asdict(self):
        return dict(self.__dict__)


def make_fake_mt5(*, init_ok=True, last_error=(1, "Success"), select_ok=True):
    m = types.ModuleType("MetaTrader5")
    for i, name in enumerate(_TF_NAMES):
        setattr(m, name, i + 1)
    m.TRADE_ACTION_DEAL = 1
    m.TRADE_ACTION_PENDING = 5
    m.TRADE_ACTION_SLTP = 6
    m.TRADE_ACTION_REMOVE = 2
    m.ORDER_TYPE_BUY = 0
    m.ORDER_TYPE_SELL = 1
    m.ORDER_TYPE_BUY_LIMIT = 2
    m.ORDER_TYPE_SELL_LIMIT = 3
    m.ORDER_TYPE_BUY_STOP = 4
    m.ORDER_TYPE_SELL_STOP = 5
    m.ORDER_TIME_GTC = 0
    m.ORDER_FILLING_IOC = 1
    m.ORDER_FILLING_FOK = 2
    m.POSITION_TYPE_BUY = 0

    rows = [
        {"time": 1_700_000_000, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
         "tick_volume": 5, "real_volume": 0},
        {"time": 1_700_000_900, "open": 1.5, "high": 2.5, "low": 1.0, "close": 2.0,
         "tick_volume": 7, "real_volume": 12},
    ]

    m.initialize = lambda **kw: init_ok
    m.last_error = lambda: last_error
    m.login = lambda login, password=None, server=None: True
    m.version = lambda: (500, 4000, "5.0.45")
    m.shutdown = lambda: None
    m.symbol_select = lambda symbol, enable=True: select_ok
    m.copy_rates_from_pos = lambda symbol, tf, start, count: rows[:count]
    m.copy_rates_range = lambda symbol, tf, frm, to: rows
    m.symbol_info_tick = lambda symbol: _NT(bid=1.2, ask=1.2002, last=0.0, time=1_700_000_000)
    m.symbol_info = lambda symbol: _NT(name=symbol, spread=13, trade_contract_size=100000.0)
    m.symbols_get = lambda group=None: (_NT(name="EURUSD"), _NT(name="XAUUSD"))
    m.account_info = lambda: _NT(login=42, currency="USD", balance=1000.0, equity=1010.0,
                                 margin=50.0, margin_free=960.0, profit=10.0, leverage=100.0)
    m.positions_get = lambda **kw: (_NT(ticket=999, symbol="EURUSD", volume=0.1, type=0,
                                        price_open=1.2, price_current=1.25, profit=5.0,
                                        sl=1.1, tp=1.4, time=1_700_000_000),)
    m.orders_get = lambda **kw: ()
    m.order_send = lambda req: _NT(retcode=10009, order=555, deal=777,
                                   volume=req.get("volume", 0.0),
                                   price=req.get("price", 0.0), comment="ok")
    return m


@pytest.fixture()
def fake_mt5(monkeypatch):
    fake = make_fake_mt5()
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    return fake


# ---------------------------------------------------------------------------
# Bridge test server (real protocol: _handle_client + _dispatch over TCP)
# ---------------------------------------------------------------------------

def _start_bridge(dispatcher):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(2)
    port = srv.getsockname()[1]
    lock = threading.Lock()

    def _accept_loop():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(
                target=bridge_server._handle_client, args=(conn, dispatcher, lock), daemon=True
            ).start()

    threading.Thread(target=_accept_loop, daemon=True).start()
    return srv, port


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------------------
# NativeTransport vs BridgeClient parity
# ---------------------------------------------------------------------------

_PARITY_CALLS = [
    ("ping",),
    ("candles_from_count", "EURUSD", "15m", 2),
    ("candles_range", "EURUSD", "1h", 1_700_000_000_000, 1_700_100_000_000),
    ("tick", "EURUSD"),
    ("symbols", "*"),
    ("symbol_info", "EURUSD"),
    ("account_info",),
    ("positions",),
    ("pending_orders",),
    ("place_order", {"symbol": "EURUSD", "side": "buy", "order_kind": "market", "volume": 0.1}),
    ("modify_position", 999, 1.15, 1.45),
    ("close_position", 999, None, None),
]


def test_native_vs_bridge_parity(fake_mt5):
    native = NativeTransport()
    dispatcher = bridge_server.MT5Dispatcher()
    srv, port = _start_bridge(dispatcher)
    client = BridgeClient("127.0.0.1", port, timeout=5.0)
    try:
        for call in _PARITY_CALLS:
            method, *args = call
            assert native.call(method, *args) == client.call(method, *args), method
    finally:
        client.close()
        srv.close()


def test_native_vs_bridge_parity_on_op_error(monkeypatch):
    fake = make_fake_mt5(select_ok=False)
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    native = NativeTransport()
    dispatcher = bridge_server.MT5Dispatcher()
    srv, port = _start_bridge(dispatcher)
    client = BridgeClient("127.0.0.1", port, timeout=5.0)
    try:
        with pytest.raises(BrokerError) as native_err:
            native.call("candles_from_count", "EURUSD", "15m", 2)
        with pytest.raises(BrokerError) as bridge_err:
            client.call("candles_from_count", "EURUSD", "15m", 2)
        assert "symbol_select" in str(native_err.value)
        assert "symbol_select" in str(bridge_err.value)
    finally:
        client.close()
        srv.close()


def test_native_vs_bridge_parity_private_method_guard(fake_mt5):
    native = NativeTransport()
    dispatcher = bridge_server.MT5Dispatcher()
    srv, port = _start_bridge(dispatcher)
    client = BridgeClient("127.0.0.1", port, timeout=5.0)
    try:
        with pytest.raises(BrokerError, match="private methods"):
            native.call("_tf", "1m")
        with pytest.raises(BrokerError, match="private methods"):
            client.call("_tf", "1m")
    finally:
        client.close()
        srv.close()


def test_metatrader_broker_end_to_end_over_native(fake_mt5):
    mt = MetaTraderBroker(mode="native")     # connect=True probe runs through native
    assert mt.mode == "native"
    assert mt.is_authenticated               # fake account login=42
    rows = mt.fetch_last_candles("EURUSD", "15m", 2)
    assert rows[0]["open"] == 1.0 and rows[0]["timestamp"] == 1_700_000_000_000
    assert "XAUUSD" in mt.list_symbols("*")


# ---------------------------------------------------------------------------
# Mode auto-detection matrix (mode × platform × host)
# ---------------------------------------------------------------------------

def test_mode_auto_on_windows_default_host_is_native(monkeypatch, fake_mt5):
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    mt = MetaTraderBroker(connect=False)
    assert mt.mode == "native" and isinstance(mt._transport, NativeTransport)


def test_mode_auto_on_windows_remote_host_is_bridge(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    mt = MetaTraderBroker(connect=False, host="203.0.113.7")
    assert mt.mode == "bridge" and isinstance(mt._transport, BridgeClient)


def test_mode_auto_on_linux_is_bridge(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    mt = MetaTraderBroker(connect=False)
    assert mt.mode == "bridge" and isinstance(mt._transport, BridgeClient)


def test_mode_forced_native_on_linux(monkeypatch, fake_mt5):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    mt = MetaTraderBroker(connect=False, mode="native")
    assert mt.mode == "native" and isinstance(mt._transport, NativeTransport)


def test_mode_forced_bridge_on_windows(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    mt = MetaTraderBroker(connect=False, mode="bridge")
    assert mt.mode == "bridge" and isinstance(mt._transport, BridgeClient)


def test_mode_invalid_raises_valueerror():
    with pytest.raises(ValueError, match="auto"):
        MetaTraderBroker(connect=False, mode="wat")


def test_mode_injected_transport_wins():
    class _Dummy:
        def call(self, *a, **k):
            return None

        def close(self):
            pass

    mt = MetaTraderBroker(transport=_Dummy(), connect=False)
    assert mt.mode == "custom" and isinstance(mt._transport, _Dummy)


def test_factory_passes_mode(monkeypatch, fake_mt5):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    assert Broker("metatrader", mode="native", connect=False).mode == "native"
    assert Broker("mt5", connect=False).mode == "bridge"


# ---------------------------------------------------------------------------
# Windows failure UX (native path)
# ---------------------------------------------------------------------------

def test_native_missing_package_raises_install_hint(monkeypatch):
    monkeypatch.setitem(sys.modules, "MetaTrader5", None)   # forces ImportError
    with pytest.raises(BrokerError) as err:
        NativeTransport()
    msg = str(err.value)
    assert "pip install AlgoTradeKit[mt5]" in msg
    assert "pip install MetaTrader5" in msg


def test_native_missing_package_via_broker(monkeypatch):
    monkeypatch.setitem(sys.modules, "MetaTrader5", None)
    with pytest.raises(BrokerError, match=r"pip install AlgoTradeKit\[mt5\]"):
        MetaTraderBroker(connect=False, mode="native")


def test_native_initialize_failure_raises_connectionfailed(monkeypatch):
    fake = make_fake_mt5(init_ok=False, last_error=(-10005, "IPC timeout"))
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    tr = NativeTransport()
    with pytest.raises(ConnectionFailed) as err:
        tr.call("ping")
    msg = str(err.value)
    assert "-10005" in msg and "terminal" in msg.lower()


def test_native_initialize_failure_surfaces_through_broker(monkeypatch):
    fake = make_fake_mt5(init_ok=False, last_error=(-10005, "IPC timeout"))
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    with pytest.raises(ConnectionFailed):
        MetaTraderBroker(mode="native")      # probe must not swallow this


# ---------------------------------------------------------------------------
# Linux failure UX (bridge path diagnostics)
# ---------------------------------------------------------------------------

def test_diagnostics_wine_missing_points_to_part_a(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda cmd: None)
    client = BridgeClient("127.0.0.1", _free_port(), timeout=0.25)
    with pytest.raises(ConnectionFailed) as err:
        client.call("ping")
    msg = str(err.value)
    assert "Wine is not installed" in msg and "Part A" in msg


def test_diagnostics_prefix_missing_points_to_part_b(monkeypatch, tmp_path):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda cmd: "/usr/bin/wine")
    monkeypatch.setenv("WINEPREFIX", str(tmp_path / "definitely-missing"))
    client = BridgeClient("127.0.0.1", _free_port(), timeout=0.25)
    with pytest.raises(ConnectionFailed) as err:
        client.call("ping")
    msg = str(err.value)
    assert "prefix not found" in msg and "Part B" in msg


def test_diagnostics_bridge_not_running_points_to_part_g(monkeypatch, tmp_path):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda cmd: "/usr/bin/wine")
    monkeypatch.setenv("WINEPREFIX", str(tmp_path))         # exists
    client = BridgeClient("127.0.0.1", _free_port(), timeout=0.25)
    with pytest.raises(ConnectionFailed) as err:
        client.call("ping")
    msg = str(err.value)
    assert "Bridge is not running" in msg and "Part G" in msg


def test_diagnostics_remote_host_skips_local_checks(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda cmd: None)   # would say Part A if local

    # Hermetic on purpose.  This used to dial 192.0.2.1 (TEST-NET-1) and trust
    # it to be unroutable, which is not true everywhere: on a network whose ISP
    # runs a catch-all middlebox the connect *succeeds* and the call turns into
    # a read timeout, so the test failed for a reason that had nothing to do
    # with the diagnostic being tested.
    def _unreachable(address, timeout=None, *args, **kwargs):
        raise OSError(101, "Network is unreachable")

    monkeypatch.setattr(socket, "create_connection", _unreachable)

    client = BridgeClient("192.0.2.1", 18812, timeout=0.25)
    with pytest.raises(ConnectionFailed) as err:
        client.call("ping")
    msg = str(err.value)
    assert "192.0.2.1:18812" in msg and "Part G" in msg
    assert "Part A" not in msg and "Wine is not installed" not in msg


def test_diagnostics_silent_peer_points_to_part_g():
    """A port that accepts TCP but never answers must diagnose, not leak a bare
    TimeoutError — the D4 contract is diagnose-and-stop for every failure."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)                      # completes the handshake, never replies
    host, port = server.getsockname()
    client = BridgeClient(host, port, timeout=0.25)
    try:
        with pytest.raises(ConnectionFailed) as err:
            client.call("ping")
        msg = str(err.value)
        assert "accepted the connection but sent no reply" in msg
        assert f"{host}:{port}" in msg
        assert "Part G" in msg and "Troubleshooting" in msg
    finally:
        client.close()
        server.close()


# ---------------------------------------------------------------------------
# Packaging + standalone bridge deploy
# ---------------------------------------------------------------------------

def test_pyproject_has_windows_only_mt5_extra():
    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    assert "mt5 = " in text
    assert 'MetaTrader5; platform_system == "Windows"' in text


def test_ops_module_is_stdlib_only():
    import AlgoTradeKit.broker.metatrader._ops as ops_mod  # imports fine without MetaTrader5

    src = Path(ops_mod.__file__).read_text()
    for forbidden in ("import AlgoTradeKit", "from AlgoTradeKit", "from .", "import pandas"):
        assert forbidden not in src, f"_ops.py must stay stdlib-only, found: {forbidden}"


def test_bridge_server_standalone_two_file_deploy(tmp_path):
    src_dir = Path(bridge_server.__file__).resolve().parent
    for name in ("bridge_server.py", "_ops.py"):
        (tmp_path / name).write_text((src_dir / name).read_text())
    out = subprocess.run(
        [sys.executable, "-c", "import bridge_server; print('standalone-ok')"],
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert "standalone-ok" in out.stdout


# ---------------------------------------------------------------------------
# — MT5 streaming (polling): stream_candles / stream_ticker
# ---------------------------------------------------------------------------

_T0 = 1_700_000_000_000        # bar-aligned UTC ms
_MIN = 60_000


def _bar(ts_ms, close=1.5, **over):
    row = {"timestamp": ts_ms, "open": 1.0, "high": 2.0, "low": 0.5, "close": close,
           "volume": 5.0, "close_time": ts_ms + _MIN - 1, "quote_volume": 0.0,
           "trades": 5, "taker_buy_base": 0.0, "taker_buy_quote": 0.0}
    row.update(over)
    return row


class _ScriptedTransport:
    """Feeds pollers a scripted response per call (last one repeats); records calls."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def call(self, method, *args, **kwargs):
        self.calls.append((method, args))
        resp = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(resp, Exception):
            raise resp
        return copy.deepcopy(resp)      # like the bridge JSON round-trip: fresh objects

    def close(self):
        pass


def _mt(transport, **kw):
    return MetaTraderBroker(transport=transport, connect=False, **kw)


def test_stream_candles_closed_only_baseline_and_dedup():
    a, b, c = _bar(_T0), _bar(_T0 + _MIN), _bar(_T0 + 2 * _MIN)
    tr = _ScriptedTransport([[a, b], [a, b], [a, b, c], [a, b, c]])
    got = []
    poll = _mt(tr)._make_candle_poller("EURUSD", "1m", got.append, True)
    poll()                                    # baseline: pre-subscribe history never fires
    poll()                                    # unchanged tail → still nothing
    assert got == []
    poll()                                    # c appeared → b closed, exactly once
    assert [(r["timestamp"], r["closed"]) for r in got] == [(_T0 + _MIN, True)]
    poll()                                    # same tail again → dedup by timestamp
    assert len(got) == 1
    assert got[0]["close"] == 1.5             # full library-standard row passed through


def test_stream_candles_emits_missed_bars_in_order():
    a, b = _bar(_T0), _bar(_T0 + _MIN)
    c, d, e = _bar(_T0 + 2 * _MIN), _bar(_T0 + 3 * _MIN), _bar(_T0 + 4 * _MIN)
    tr = _ScriptedTransport([[a, b], [b, c, d, e]])   # 3 bars advanced in one poll gap
    got = []
    poll = _mt(tr)._make_candle_poller("EURUSD", "1m", got.append, True)
    poll()
    poll()
    assert [r["timestamp"] for r in got] == [_T0 + _MIN, _T0 + 2 * _MIN, _T0 + 3 * _MIN]
    assert all(r["closed"] for r in got)


def test_stream_candles_forming_updates_emit_on_change_only():
    a, b = _bar(_T0), _bar(_T0 + _MIN, close=1.5)
    b2 = _bar(_T0 + _MIN, close=1.7, high=2.2)
    c = _bar(_T0 + 2 * _MIN)
    tr = _ScriptedTransport([[a, b], [a, b], [a, b2], [b2, c]])
    got = []
    poll = _mt(tr)._make_candle_poller("EURUSD", "1m", got.append, False)
    poll()                                    # first poll → current forming bar immediately
    assert [(r["timestamp"], r["closed"]) for r in got] == [(_T0 + _MIN, False)]
    poll()                                    # identical forming bar → silent
    assert len(got) == 1
    poll()                                    # forming bar changed → emitted
    assert got[-1]["closed"] is False and got[-1]["close"] == 1.7
    poll()                                    # b closes (final data), then c forming
    assert [(r["timestamp"], r["closed"]) for r in got[-2:]] == [
        (_T0 + _MIN, True), (_T0 + 2 * _MIN, False)]
    assert len(got) == 4


def test_stream_candles_tolerates_empty_tail():
    a, b, c = _bar(_T0), _bar(_T0 + _MIN), _bar(_T0 + 2 * _MIN)
    tr = _ScriptedTransport([[], [a, b], [a, b, c]])
    got = []
    poll = _mt(tr)._make_candle_poller("EURUSD", "1m", got.append, True)
    poll()                                    # empty venue answer → no crash, no baseline
    assert got == []
    poll()                                    # baseline set here
    poll()
    assert [r["timestamp"] for r in got] == [_T0 + _MIN]


def test_stream_ticker_emits_on_change_only():
    t1 = {"bid": 1.2, "ask": 1.2002, "last": 0.0, "time_ms": _T0}
    t2 = {"bid": 1.21, "ask": 1.2102, "last": 0.0, "time_ms": _T0 + 500}
    tr = _ScriptedTransport([t1, t1, t2])
    got = []
    poll = _mt(tr)._make_ticker_poller("EURUSD", got.append)
    poll()                                    # first poll always fires
    poll()                                    # unchanged → silent
    poll()                                    # changed → fires
    assert len(got) == 2 and all(isinstance(t, Ticker) for t in got)
    assert got[0].bid == 1.2 and got[0].timestamp == _T0
    assert got[1].last == pytest.approx((1.21 + 1.2102) / 2)   # mid fallback, as get_ticker
    assert tr.calls[0] == ("tick", ("EURUSD",))


def test_stream_candles_returns_stoppable_stream_and_polls_tail():
    tr = _ScriptedTransport([[_bar(_T0), _bar(_T0 + _MIN)]])
    mt = _mt(tr, candle_poll_interval=0.01)
    got = []
    stream = mt.stream_candles("EURUSD", "1H", got.append)     # "1H" → normalized "1h"
    assert isinstance(stream, Stream) and stream.alive
    deadline = time.time() + 2.0
    while len(tr.calls) < 3 and time.time() < deadline:
        time.sleep(0.005)
    assert len(tr.calls) >= 3                                  # polling loop is running
    assert tr.calls[0] == ("candles_from_count", ("EURUSD", "1h", 10))
    stream.stop()
    assert not stream.alive                                    # worker thread joined
    n = len(tr.calls)
    time.sleep(0.05)
    assert len(tr.calls) == n                                  # polling really stopped


def test_stream_ticker_returns_stoppable_stream():
    tr = _ScriptedTransport([{"bid": 1.2, "ask": 1.2002, "last": 0.0, "time_ms": _T0}])
    stream = _mt(tr, tick_poll_interval=0.01).stream_ticker("EURUSD", lambda t: None)
    assert isinstance(stream, Stream) and stream.alive
    stream.stop()
    assert not stream.alive


def test_stream_candles_end_to_end_over_bridge(monkeypatch):
    """Full production path: poller → BridgeClient → TCP → bridge dispatcher → fake MT5."""
    fake = make_fake_mt5()
    rows = [
        {"time": 1_700_000_000, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
         "tick_volume": 5, "real_volume": 0},
        {"time": 1_700_000_900, "open": 1.5, "high": 2.5, "low": 1.0, "close": 2.0,
         "tick_volume": 7, "real_volume": 12},
    ]
    polls = []

    def _rates(symbol, tf, start, count):
        polls.append(1)
        return rows[-count:]

    fake.copy_rates_from_pos = _rates
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    dispatcher = bridge_server.MT5Dispatcher()
    srv, port = _start_bridge(dispatcher)
    mt = MetaTraderBroker(mode="bridge", port=port, connect=False, candle_poll_interval=0.02)
    got, stream = [], None
    try:
        stream = mt.stream_candles("EURUSD", "15m", got.append)   # closed_only default
        deadline = time.time() + 5.0
        while not polls and time.time() < deadline:
            time.sleep(0.01)                                      # baseline poll sees 2 bars
        rows.append({"time": 1_700_001_800, "open": 2.0, "high": 3.0, "low": 1.5,
                     "close": 2.5, "tick_volume": 3, "real_volume": 0})
        while not got and time.time() < deadline:
            time.sleep(0.01)
        assert [(c["timestamp"], c["closed"]) for c in got] == [(1_700_000_900_000, True)]
        assert got[0]["close"] == 2.0
    finally:
        if stream is not None:
            stream.stop()
        mt.close()
        srv.close()


def test_start_poll_stream_swallows_errors_and_continues():
    calls, errors = [], []

    def poll():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("bridge hiccup")

    s = start_poll_stream(poll, interval=0.01, name="test-poll", on_error=errors.append)
    deadline = time.time() + 2.0
    while len(calls) < 3 and time.time() < deadline:
        time.sleep(0.005)
    s.stop()
    assert len(calls) >= 3                    # the failing first poll did not kill the feed
    assert len(errors) == 1 and "hiccup" in str(errors[0])


def test_stream_candles_invalid_timeframe_raises():
    mt = _mt(_ScriptedTransport([[]]))
    with pytest.raises(ValueError, match="Unsupported timeframe"):
        mt.stream_candles("EURUSD", "7m", lambda c: None)


def test_poll_interval_defaults_and_validation(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    mt = MetaTraderBroker(connect=False)
    assert mt.candle_poll_interval == 1.0 and mt.tick_poll_interval == 0.2
    with pytest.raises(ValueError, match="poll"):
        MetaTraderBroker(connect=False, candle_poll_interval=0)
    with pytest.raises(ValueError, match="poll"):
        MetaTraderBroker(connect=False, tick_poll_interval=-1)


def test_factory_passes_poll_intervals(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    mt = Broker("metatrader", connect=False, candle_poll_interval=2.5, tick_poll_interval=0.5)
    assert mt.candle_poll_interval == 2.5 and mt.tick_poll_interval == 0.5


# ---------------------------------------------------------------------------
# — get_trading_costs from symbol_info + venue clock
# ---------------------------------------------------------------------------

def _symbol_info(**over):
    info = {"spread": 12, "point": 1e-05, "trade_contract_size": 100_000.0,
            "bid": 1.10000, "ask": 1.10012, "digits": 5}
    info.update(over)
    return info


def test_trading_costs_from_symbol_info():
    tr = _ScriptedTransport([_symbol_info()])
    tc = _mt(tr).get_trading_costs("EURUSD")
    assert isinstance(tc, TradingCosts)
    assert tc.commission_type == COMMISSION_TYPE_PER_LOT
    assert tc.commission == 0.0                       # MT5 exposes none — override
    assert tc.spread == pytest.approx(12e-05)         # spread points × point
    assert tc.contract_size == 100_000.0
    assert tc.raw == _symbol_info()                   # full payload kept for inspection
    assert tr.calls == [("symbol_info", ("EURUSD",))]


def test_trading_costs_spread_falls_back_to_bid_ask():
    tr = _ScriptedTransport([_symbol_info(spread=0, point=0.0, trade_contract_size=0.0)])
    tc = _mt(tr).get_trading_costs("EURUSD")
    assert tc.spread == pytest.approx(0.00012)        # ask − bid when no spread field
    assert tc.contract_size is None                   # venue reported none


def test_trading_costs_unknown_symbol_propagates():
    tr = _ScriptedTransport([BrokerError("symbol_info failed: (1, 'not found')")])
    with pytest.raises(BrokerError, match="symbol_info failed"):
        _mt(tr).get_trading_costs("NOPE")


def test_mt5_clock_offset_is_near_zero():
    # MT5 server_time() proxies the local clock → venue offset ≈ 0.
    off = _mt(_ScriptedTransport([{}])).clock_offset_ms()
    assert abs(off) <= 50


# ---------------------------------------------------------------------------
# — history deals op (close detection reads the real exit fill)
# ---------------------------------------------------------------------------

def test_history_deals_op_position_range_and_error():
    from AlgoTradeKit.broker.metatrader._ops import MT5Ops

    deal = _NT(ticket=1, position_id=999, entry=1, price=1.0949, reason=4,
               time=1_700_000_500, time_msc=1_700_000_500_123, volume=0.1)
    calls = {}
    fake = make_fake_mt5()

    def _hdg(*args, **kwargs):
        calls["args"], calls["kwargs"] = args, kwargs
        return (deal,)

    fake.history_deals_get = _hdg
    ops = MT5Ops(fake)

    # Position-ticket form: keyword-only, no date range.
    rows = ops.history_deals(0, 0, 999)
    assert calls["args"] == () and calls["kwargs"] == {"position": 999}
    assert rows == [deal._asdict()]
    assert rows[0]["price"] == 1.0949 and rows[0]["reason"] == 4

    # Range form: UTC-ms bounds become tz-aware datetimes.
    ops.history_deals(1_700_000_000_000, 1_700_001_000_000, None)
    frm, to = calls["args"]
    assert calls["kwargs"] == {}
    assert frm.year == 2023 and frm.tzinfo is not None
    assert (to - frm).total_seconds() == 1_000

    # MT5 answering None is an error, like every other op.
    fake.history_deals_get = lambda *a, **kw: None
    with pytest.raises(RuntimeError, match="history_deals_get failed"):
        ops.history_deals(0, 0, 999)
