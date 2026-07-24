"""
``NativeTransport`` — in-process MetaTrader 5 transport for Windows.

Exposes the exact same ``call(method, *args, **kwargs)`` surface as
:class:`BridgeClient`, but executes each operation directly via
``import MetaTrader5`` (no Wine, no socket).  Method names and payload shapes
are identical to the bridge protocol — both transports drive the shared
:class:`MT5Ops` implementations — so :class:`MetaTraderBroker` behaves
identically over either one.

The ``MetaTrader5`` package is an optional extra (Windows-only)::

    pip install AlgoTradeKit[mt5]
"""
from __future__ import annotations

import threading
from typing import Any

from .._errors import BrokerError, ConnectionFailed
from ._ops import MT5Ops

_INSTALL_HINT = (
    "The 'MetaTrader5' package is not installed in this Python. Install it with:\n"
    "    pip install AlgoTradeKit[mt5]\n"
    "(or: pip install MetaTrader5)\n"
    "Note: the package exists for Windows only — on Linux/macOS use the Wine "
    "bridge instead (mode=\"bridge\", see MT5_WINE_SETUP.md)."
)

_INITIALIZE_HINT = (
    "mt5.initialize() failed — could not attach to a MetaTrader 5 terminal. The MT5 "
    "terminal must be installed on this machine (and ideally have been started and "
    "logged in once) before the native connector can use it."
)


class NativeTransport:
    """
    In-process MT5 transport (``mode="native"``, Windows).

    ``import MetaTrader5`` happens eagerly in ``__init__`` so a missing package
    fails fast with the install command; ``mt5.initialize()`` runs lazily on the
    first ``call()`` so constructing a broker with ``connect=False`` touches no
    terminal.
    """

    def __init__(self) -> None:
        try:
            import MetaTrader5 as mt5
        except ImportError as exc:
            raise BrokerError(_INSTALL_HINT) from exc
        self._mt5 = mt5
        self._ops = MT5Ops(mt5)
        self._initialized = False
        self._lock = threading.Lock()  # MetaTrader5 is not thread-safe

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        if not self._mt5.initialize():
            raise ConnectionFailed(f"{_INITIALIZE_HINT} MT5 error: {self._mt5.last_error()}")
        self._initialized = True

    def call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Invoke *method* on the shared MT5 ops (bridge-protocol compatible)."""
        with self._lock:
            self._ensure_initialized()
            try:
                if method.startswith("_"):
                    raise ValueError("private methods are not callable")
                func = getattr(self._ops, method)
                return func(*args, **kwargs)
            except BrokerError:
                raise
            except Exception as exc:
                raise BrokerError(
                    f"MetaTrader error in {method}: {type(exc).__name__}: {exc}"
                ) from exc

    def close(self) -> None:
        with self._lock:
            if self._initialized:
                try:
                    self._mt5.shutdown()
                except Exception:  # noqa: BLE001
                    pass
                self._initialized = False
