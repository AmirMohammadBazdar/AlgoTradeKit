"""
Threaded async WebSocket client used by connectors for real-time feeds.

The library already depends on ``websockets`` (asyncio) for the ``visual`` /
``report`` servers.  Here we reuse it on the *client* side: each stream runs its
own asyncio event loop inside a daemon thread, so callers get a simple
synchronous ``Stream`` handle with a ``.stop()`` method and never touch asyncio.
"""
from __future__ import annotations

import asyncio
import json
import threading
from typing import Any, Callable, Optional


class Stream:
    """
    Handle to a running background WebSocket subscription.

    Returned by ``broker.stream_candles(...)`` / ``stream_ticker(...)``.  Call
    :meth:`stop` to end the subscription and join the worker thread.
    """

    def __init__(
        self,
        name: str,
        thread: threading.Thread,
        stop_event: threading.Event,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.name = name
        self._thread = thread
        self._stop_event = stop_event
        self._loop = loop

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the worker to close the socket and wait for it to exit."""
        self._stop_event.set()
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except RuntimeError:
            pass  # loop already stopped
        self._thread.join(timeout=timeout)

    def __repr__(self) -> str:
        return f"<Stream name={self.name!r} alive={self.alive}>"


def start_ws_stream(
    url: str,
    on_message: Callable[[dict[str, Any]], None],
    *,
    on_open_frames: Optional[list[dict]] = None,
    name: str = "ws",
    ping_interval: float = 20.0,
    reconnect: bool = True,
    on_error: Optional[Callable[[Exception], None]] = None,
) -> Stream:
    """
    Open *url* in a background daemon thread and deliver each JSON message to
    *on_message* (already parsed to a dict).

    Parameters
    ----------
    url:
        Full WebSocket URL (Binance single-stream or user-data listen-key URL).
    on_message:
        Synchronous callback invoked for every message.  Exceptions inside it
        are swallowed (optionally reported via *on_error*) so one bad callback
        cannot kill the feed.
    on_open_frames:
        Optional JSON frames sent immediately after connecting (e.g. a Binance
        ``{"method": "SUBSCRIBE", "params": [...], "id": 1}``).
    reconnect:
        Auto-reconnect with capped exponential backoff on transport errors.
    """
    try:
        import websockets
    except ImportError as exc:  # pragma: no cover - declared dependency
        raise ImportError(
            "Real-time streaming needs the 'websockets' package "
            "(pip install websockets)."
        ) from exc

    stop_event = threading.Event()
    loop = asyncio.new_event_loop()

    async def _runner() -> None:
        backoff = 1.0
        while not stop_event.is_set():
            try:
                async with websockets.connect(url, ping_interval=ping_interval) as ws:
                    backoff = 1.0  # reset after a successful connect
                    for frame in on_open_frames or []:
                        await ws.send(json.dumps(frame))
                    while not stop_event.is_set():
                        raw = await ws.recv()
                        try:
                            msg = json.loads(raw)
                        except (ValueError, TypeError):
                            continue
                        try:
                            on_message(msg)
                        except Exception as exc:  # noqa: BLE001 — never kill the feed
                            if on_error is not None:
                                on_error(exc)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001 — transport dropped
                if on_error is not None:
                    on_error(exc)
                if not reconnect or stop_event.is_set():
                    break
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def _thread_main() -> None:
        asyncio.set_event_loop(loop)
        task = loop.create_task(_runner())
        try:
            loop.run_until_complete(task)
        except RuntimeError:
            pass  # loop.stop() was called from stop()
        finally:
            task.cancel()
            try:
                loop.run_until_complete(asyncio.sleep(0))
            except Exception:  # noqa: BLE001
                pass
            loop.close()

    thread = threading.Thread(target=_thread_main, name=f"atk-{name}", daemon=True)
    thread.start()
    return Stream(name=name, thread=thread, stop_event=stop_event, loop=loop)
