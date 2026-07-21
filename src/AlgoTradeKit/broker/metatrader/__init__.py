"""MetaTrader 5 connector — Wine bridge (Linux/remote) or native ``MetaTrader5`` (Windows)."""
from ._bridge_client import BridgeClient
from ._client import MetaTraderBroker
from ._native import NativeTransport

__all__ = ["MetaTraderBroker", "BridgeClient", "NativeTransport"]
