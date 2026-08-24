"""
Shared helper for the examples: get some 1m candles to play with.

Tries, in order: a CSV you point it at, a download from Binance's public API
(no key needed), and finally a synthetic random walk so the examples still run
with no network at all.
"""
from __future__ import annotations

import math
import random
import sys

import pandas as pd


def one_minute_candles(
    symbol: str = "BTCUSDT",
    days: float = 2.0,
    csv: str | None = None,
) -> pd.DataFrame:
    """Return a 1m OHLCV frame — ``timestamp`` in UTC milliseconds."""
    csv = csv or (sys.argv[1] if len(sys.argv) > 1 else None)
    if csv:
        print(f"  reading {csv}")
        return pd.read_csv(csv)

    try:
        return _download(symbol, days)
    except Exception as exc:                      # offline, blocked, rate-limited
        print(f"  download failed ({exc}); using synthetic candles instead")
        return _synthetic(days)


def _download(symbol: str, days: float) -> pd.DataFrame:
    """Public market data — no API key involved."""
    from AlgoTradeKit.broker import Broker

    print(f"  downloading {days:g} day(s) of {symbol} 1m from Binance …")
    rows = Broker("binance-futures").fetch_last_candles(
        symbol, "1m", int(days * 24 * 60)
    )
    if not rows:
        raise RuntimeError("no candles returned")
    return pd.DataFrame(rows)


def _synthetic(days: float) -> pd.DataFrame:
    """A random walk with enough shape that indicators and trades do something."""
    n = int(days * 24 * 60)
    start = 1_700_000_000_000 - (1_700_000_000_000 % 60_000) - n * 60_000
    rng = random.Random(7)
    price, rows = 30_000.0, []
    for i in range(n):
        # a slow cycle plus noise, so crossovers actually happen
        price *= 1 + 0.0004 * math.sin(i / 90) + rng.gauss(0, 0.0006)
        high = price * (1 + abs(rng.gauss(0, 0.0004)))
        low = price * (1 - abs(rng.gauss(0, 0.0004)))
        open_ = rows[-1]["close"] if rows else price
        rows.append({
            "timestamp": start + i * 60_000,
            "open": round(open_, 2), "high": round(max(high, open_, price), 2),
            "low": round(min(low, open_, price), 2), "close": round(price, 2),
            "volume": round(abs(rng.gauss(12, 4)), 3),
        })
    return pd.DataFrame(rows)
