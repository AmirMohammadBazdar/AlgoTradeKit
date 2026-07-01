"""
Binance WebSocket helpers: public market streams + the private user-data stream.

Public streams need no auth (``<symbol>@kline_<interval>``, ``<symbol>@bookTicker``).
The user-data stream needs a *listen key* obtained from a signed REST call and
kept alive with a periodic PUT (Binance expires idle keys after ~60 min).
"""
from __future__ import annotations

import threading
from typing import Any, Callable

from ..._stream import Stream, start_ws_stream
from ._endpoints import Endpoints
from ._rest import BinanceREST


def _stream_url(ep: Endpoints, stream_name: str) -> str:
    return f"{ep.ws_base}/{stream_name}"


def stream_kline(
    ep: Endpoints,
    symbol: str,
    interval: str,
    on_message: Callable[[dict[str, Any]], None],
) -> Stream:
    """Open a ``<symbol>@kline_<interval>`` stream."""
    name = f"{symbol.lower()}@kline_{interval}"
    return start_ws_stream(_stream_url(ep, name), on_message, name=name)


def stream_book_ticker(
    ep: Endpoints,
    symbol: str,
    on_message: Callable[[dict[str, Any]], None],
) -> Stream:
    """Open a ``<symbol>@bookTicker`` stream (best bid / ask)."""
    name = f"{symbol.lower()}@bookTicker"
    return start_ws_stream(_stream_url(ep, name), on_message, name=name)


class UserDataStream:
    """
    Private account event stream (order updates, balance / position changes).

    Manages the listen-key lifecycle: create → open socket → keepalive PUT every
    ``keepalive_seconds`` → close + delete on :meth:`stop`.
    """

    def __init__(
        self,
        rest: BinanceREST,
        ep: Endpoints,
        on_event: Callable[[dict[str, Any]], None],
        keepalive_seconds: float = 1800.0,
    ) -> None:
        self._rest = rest
        self._ep = ep
        self._on_event = on_event
        self._keepalive_seconds = keepalive_seconds
        self._listen_key: str | None = None
        self._stream: Stream | None = None
        self._ka_stop = threading.Event()
        self._ka_thread: threading.Thread | None = None

    def start(self) -> "UserDataStream":
        resp = self._rest.signed("POST", self._ep.listen_key, {})
        self._listen_key = resp["listenKey"]
        url = f"{self._ep.ws_base}/{self._listen_key}"
        self._stream = start_ws_stream(url, self._on_event, name="userdata")

        self._ka_thread = threading.Thread(
            target=self._keepalive_loop, name="atk-userdata-keepalive", daemon=True
        )
        self._ka_thread.start()
        return self

    def _keepalive_loop(self) -> None:
        while not self._ka_stop.wait(self._keepalive_seconds):
            try:
                self._rest.signed(
                    "PUT", self._ep.listen_key, {"listenKey": self._listen_key}
                )
            except Exception:  # noqa: BLE001 — keepalive best effort
                pass

    def stop(self) -> None:
        self._ka_stop.set()
        if self._stream is not None:
            self._stream.stop()
        if self._listen_key is not None:
            try:
                self._rest.signed(
                    "DELETE", self._ep.listen_key, {"listenKey": self._listen_key}
                )
            except Exception:  # noqa: BLE001
                pass
