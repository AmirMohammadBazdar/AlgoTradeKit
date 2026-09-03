"""AlgoTradeKit — Algorithmic Trading Toolkit"""

__version__ = "1.1.1"
__author__ = "Amir Mohammad Bazdar"

__all__ = ["run_live"]


def __getattr__(name: str):
    # Lazy top-level re-export (v1.0.0): keeps ``import AlgoTradeKit``
    # lightweight — the trader/simulate stack loads only when actually used.
    if name == "run_live":
        from .trader import run_live

        return run_live
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
