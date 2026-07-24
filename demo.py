"""
demo.py — pull the last N candles of a symbol from MetaTrader 5 and chart them.

This is the smallest possible MT5 smoke test for AlgoTradeKit v1.0.0.

How it fits together
--------------------
    ┌─────────────── your VPS (headless) ────────────────┐        ┌── your laptop ──┐
    │  Wine:  MT5 terminal + Windows Python +            │  TCP   │  demo.py        │
    │         MetaTrader5 pkg + bridge_server.py  ───────┼────────┼─►  + browser    │
    └────────────────────────────────────────────────────┘  18812 └─────────────────┘

`demo.py` only speaks TCP to the bridge — it does NOT need Wine or the
MetaTrader5 package itself. So run it wherever you have a browser (your laptop)
and tunnel the bridge port from the VPS first:

    ssh -N -L 18812:127.0.0.1:18812  user@your-vps      # keep this terminal open

Then, in another terminal:  python demo.py --symbol BTCUSD

(Full Wine/bridge setup: see MT5_WINE_SETUP.md.)

Everything is a CLI flag — no need to edit this file on the VPS
---------------------------------------------------------------
    python demo.py                                  # lists the broker's symbols
    python demo.py --symbol EURUSD --timeframe 1h --count 500
    python demo.py --symbol EURUSD --host 10.0.0.5 --port 18812      # remote bridge
    python demo.py --symbol EURUSD --chart-host 0.0.0.0 --chart-port 8080 \
                   --no-open-browser                 # running ON the VPS

Transport (v1.0.0)
------------------
`--mode auto` (the default) picks the transport for you: **Windows** talks to
the MT5 terminal in-process (`pip install AlgoTradeKit[mt5]`), **Linux/macOS**
go through the Wine bridge on `--host:--port`. A non-default `--host` always
means the bridge (you are pointing at a remote VPS). `--mode native` /
`--mode bridge` force it. The resolved choice is printed at startup.
"""
from __future__ import annotations

import argparse
import sys

import pandas as pd

from AlgoTradeKit.broker import Broker
from AlgoTradeKit.visual import Chart

# ─── config — CLI flags override every value here ───────────────────────────
SYMBOL    = None         # None → list the broker's symbols and exit (spellings vary!)
TIMEFRAME = "15m"        # 1m 5m 15m 30m 1h 4h 1d ...
COUNT     = 100

BRIDGE_HOST = "127.0.0.1"   # where the bridge (or your SSH tunnel) is reachable
BRIDGE_PORT = 18812
MODE        = "auto"        # "auto" | "native" (Windows, no bridge) | "bridge"

# Leave as None if you started the bridge WITH --login/--password/--server.
# Otherwise fill them to log in from here:
LOGIN:    int | None = None
PASSWORD: str | None = None
SERVER:   str | None = None

OPEN_BROWSER = True         # True on your laptop; False if you run this ON the VPS
CHART_HOST   = "127.0.0.1"  # "0.0.0.0" to expose publicly (anyone who can reach
                            #   the port sees the chart — prefer an SSH tunnel)
CHART_PORT   = 0            # 0 = auto; set e.g. 8080 for a predictable SSH tunnel

#: How many symbol names to print when suggesting spellings.
SUGGEST_LIMIT = 40
# ────────────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """CLI parser — every default comes from the config block above."""
    ap = argparse.ArgumentParser(
        description="Fetch the last N MT5 candles of a symbol and chart them."
    )
    ap.add_argument("--symbol", default=SYMBOL,
                    help="EXACT symbol name from MT5 Market Watch (varies per broker). "
                         "Omit it to list the broker's symbols and exit.")
    ap.add_argument("--timeframe", default=TIMEFRAME,
                    help=f"Candle timeframe (default: {TIMEFRAME})")
    ap.add_argument("--count", type=int, default=COUNT,
                    help=f"How many candles (default: {COUNT})")

    ap.add_argument("--host", default=BRIDGE_HOST,
                    help=f"Bridge host — or your SSH tunnel (default: {BRIDGE_HOST})")
    ap.add_argument("--port", type=int, default=BRIDGE_PORT,
                    help=f"Bridge port (default: {BRIDGE_PORT})")
    ap.add_argument("--mode", choices=("auto", "native", "bridge"), default=MODE,
                    help="MT5 transport: 'auto' (default) = native MetaTrader5 on Windows "
                         "with the default host, bridge otherwise; 'native' / 'bridge' force it.")

    ap.add_argument("--chart-host", default=CHART_HOST,
                    help=f"Chart server bind address (default: {CHART_HOST}; "
                         "'0.0.0.0' exposes it publicly)")
    ap.add_argument("--chart-port", type=int, default=CHART_PORT,
                    help=f"Chart server port, 0 = auto (default: {CHART_PORT})")
    ap.add_argument("--open-browser", action=argparse.BooleanOptionalAction, default=OPEN_BROWSER,
                    help="Open a browser tab (default: %(default)s). Use --no-open-browser "
                         "when running on the VPS and browse the printed URL instead.")
    return ap


def list_symbols(broker, pattern: str = "*") -> list[str]:
    """Symbol names matching *pattern*, empty list if the venue cannot answer."""
    try:
        return broker.list_symbols(pattern) or []
    except Exception as exc:  # noqa: BLE001 — a suggestion is best-effort
        print(f"  (could not list symbols: {exc})")
        return []


def suggest_symbols(broker, symbol: str | None = None) -> list[str]:
    """
    Symbol spellings to show the user: names close to *symbol* first (e.g.
    ``BTCUSD`` → ``*BTC*``), falling back to the head of the full list.
    """
    matches: list[str] = []
    if symbol:
        stem = symbol[:3].upper()
        matches = list_symbols(broker, f"*{stem}*")
    if not matches:
        matches = list_symbols(broker, "*")
    return matches[:SUGGEST_LIMIT]


def print_suggestions(broker, symbol: str | None) -> None:
    """Print candidate symbol spellings (the ``symbol_select failed`` path)."""
    matches = suggest_symbols(broker, symbol)
    if matches:
        print("  Try one of these:", ", ".join(matches))
        print(f"  e.g.  python demo.py --symbol {matches[0]}")
    else:
        print("  (no symbols reported — is the terminal logged in?)")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Connect to MT5 (no API keys needed for candles). On Windows with the
    # default host, mode="auto" skips the bridge entirely (v1.0.0 §1).
    mt = Broker(
        "metatrader",
        mode=args.mode,
        host=args.host, port=args.port,
        server=SERVER, login=LOGIN, password=PASSWORD,
    )
    where = "in-process (native MetaTrader5)" if mt.mode == "native" else f"{args.host}:{args.port}"
    print(f"Connected to MT5 — transport: {mt.mode} → {where}.")

    # No symbol given: list what this broker actually calls things and stop.
    if not args.symbol:
        print("\n  No --symbol given. Symbols available on this account:")
        print_suggestions(mt, None)
        return 1

    print(f"Fetching last {args.count} × {args.timeframe} candles of {args.symbol} …")

    try:
        rows = mt.fetch_last_candles(args.symbol, args.timeframe, args.count)
    except Exception as exc:  # noqa: BLE001 — most often a wrong symbol spelling
        print(f"\n  Fetch failed: {exc}")
        if "symbol_select" in str(exc):
            print(f"  '{args.symbol}' is not a symbol on this account.")
            print_suggestions(mt, args.symbol)
        return 1

    if not rows:
        print(f"\n  No candles for '{args.symbol}'. Symbol spellings differ per broker.")
        print_suggestions(mt, args.symbol)
        return 1

    df = pd.DataFrame(rows)
    print(df[["timestamp", "open", "high", "low", "close", "volume"]].tail(), "\n")
    print(f"Got {len(df)} candles — opening chart (Ctrl+C to quit) …")

    chart = Chart(
        title=f"{args.symbol} — {args.timeframe} (MT5, last {args.count})",
        theme="dark",
        host=args.chart_host,
        port=args.chart_port,
    )
    chart.set_data(df)                                        # timestamp is UTC ms — handled
    chart.show(open_browser=args.open_browser, block=False)   # serves on chart_host:<port>
    if not args.open_browser:
        # Headless (VPS): print the address to paste into your own browser.
        print(f"  Chart → {chart.url}")
    chart.show(block=True)                                    # Ctrl+C to quit
    return 0


if __name__ == "__main__":
    sys.exit(main())
