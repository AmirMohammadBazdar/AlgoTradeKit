from .base import BaseSource
from .binance import BinanceSource

__all__ = ["BaseSource", "BinanceSource", "get_source"]

# ---------------------------------------------------------------------------
# Source registry — add new exchanges here in future versions
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, tuple[type, dict]] = {
    # key                   → (SourceClass, kwargs)
    "binance-spot":         (BinanceSource, {"market": "spot"}),
    "binance-futures":      (BinanceSource, {"market": "futures"}),
    # future entries:
    # "mexc-spot":          (MexcSource,    {"market": "spot"}),
    # "mt5":                (MT5Source,     {}),
}


def get_source(source_name: str) -> BaseSource:
    """
    Resolve a human-readable source name to a ready-to-use source instance.

    Parameters
    ----------
    source_name:
        Case-insensitive source identifier, e.g. ``"binance-spot"``,
        ``"binance-futures"``.

    Returns
    -------
    BaseSource
        An initialised source instance.

    Raises
    ------
    ValueError
        If *source_name* is not in the registry.
    """
    key = source_name.lower().strip()
    if key not in _REGISTRY:
        valid = sorted(_REGISTRY)
        raise ValueError(
            f"Unknown source '{source_name}'. "
            f"Available: {valid}"
        )
    cls, kwargs = _REGISTRY[key]
    return cls(**kwargs)
