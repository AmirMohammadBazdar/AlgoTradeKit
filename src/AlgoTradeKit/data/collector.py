from __future__ import annotations
from datetime import datetime, timezone
from typing import Any
import os

from ._utils import (
    TIMEFRAME_LABELS,
    normalize_timeframe,
    now_ms,
    ms_to_dt,
    parse_to_ms,
    TIMEFRAME_MS,
)
from .sources import get_source
from .sources.base import BaseSource
from .storage.csv_handler import CSVHandler
from ..broker import Broker
from ..broker.base import BaseBroker
 
 
# ---------------------------------------------------------------------------
# Formatting helpers (no external deps)
# ---------------------------------------------------------------------------
 
def _fmt_dt(ms: int) -> str:
    return ms_to_dt(ms).strftime("%Y-%m-%d %H:%M UTC")
 
def _fmt_n(n: int) -> str:
    return f"{n:,}"
 
def _bar(pct: float, width: int = 30) -> str:
    filled = int(width * pct)
    return "█" * filled + "░" * (width - filled)
 
def _print(msg: str) -> None:
    print(f"  [AlgoTradeKit] {msg}")
 
 
# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------
 
class Collector:
    """
    Collect OHLCV candle data from a supported exchange and save it as CSV.
 
    Parameters (constructor — required)
    ------------------------------------
    source : str
        Exchange + market identifier.
        Currently supported: ``"binance-spot"``, ``"binance-futures"``.
    symbol : str
        Trading pair, e.g. ``"BTCUSDT"``, ``"ETHUSDT"``.
    timeframe : str
        Candle interval. Examples: ``"1m"``, ``"15m"``, ``"1h"``, ``"4h"``,
        ``"1d"``, ``"1w"``, ``"1M"``.  Case-insensitive except ``"1M"``
        (monthly).
 
    Settable attributes (optional — set before calling ``collect()``)
    ------------------------------------------------------------------
    destination : str
        Directory where the CSV will be saved.  Default: current directory.
    outputname : str | None
        CSV filename.  If ``None``, auto-generated as
        ``<source>_<symbol>_<timeframe>.csv``.
    starttime : str | datetime | int | None
        Earliest candle to collect.  Accepts ``"YYYY/MM/DD"``,
        ``"YYYY-MM-DD"``, a ``datetime`` object, or a millisecond timestamp.
        **Required** before calling ``collect()``.
    endtime : str | datetime | int | None
        Latest candle to collect.  Defaults to *now*.
 
    Examples
    --------
    Basic usage::
 
        collector = Collector("binance-futures", "BTCUSDT", "1d")
        collector.destination = "data/"
        collector.starttime   = "2020/01/01"
        collector.collect()
 
    Custom output filename and end date::
 
        collector = Collector("binance-spot", "ETHUSDT", "4h")
        collector.destination = "/home/user/market_data/"
        collector.outputname  = "eth_4h.csv"
        collector.starttime   = "2021/06/01"
        collector.endtime     = "2023/01/01"
        collector.collect()
    """
 
    def __init__(
        self,
        source: str | BaseBroker,
        symbol: str,
        timeframe: str,
        *,
        broker: BaseBroker | None = None,
    ) -> None:
        # Two ways to point the Collector at a venue (v0.9.0):
        #   way 1 — pass a name string ("binance-futures"); the Collector builds
        #           the broker/source itself.
        #   way 2 — pass a ready ``Broker`` instance (as ``source`` or via the
        #           ``broker=`` keyword), e.g. an authenticated or MetaTrader
        #           (forex) connection.
        fetcher = broker if broker is not None else (
            source if isinstance(source, BaseBroker) else None
        )
        self._fetcher: BaseBroker | None = fetcher
        if fetcher is not None:
            self._source_name: str = getattr(fetcher, "name", "broker")
        else:
            self._source_name = str(source)

        self._symbol: str      = symbol.upper().strip()
        self._timeframe: str   = normalize_timeframe(timeframe)

        # Settable attributes
        self.destination: str       = "./"
        self.outputname:  str | None = None
        self.starttime:   Any        = None   # parsed lazily in collect()
        self.endtime:     Any        = None   # parsed lazily in collect()

        # Resolved lazily
        self._source: BaseSource | None = None
 
    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------
 
    def collect(self) -> str:
        """
        Run the collection pipeline and return the path to the saved CSV.
 
        Steps
        -----
        1. Validate inputs.
        2. Load any existing CSV data.
        3. Compute missing ranges (head / tail / middle gaps).
        4. Fetch each missing range from the exchange.
        5. Merge all data and save.
        6. Print a summary.
 
        Returns
        -------
        str
            Absolute path of the saved CSV file.
 
        Raises
        ------
        ValueError
            If ``starttime`` was not set or if inputs are invalid.
        """
        # ---- Validate ------------------------------------------------
        if self.starttime is None:
            raise ValueError(
                "collector.starttime must be set before calling collect().\n"
                '  Example: collector.starttime = "2020/01/01"'
            )
 
        start_ms = parse_to_ms(self.starttime)
        end_ms   = parse_to_ms(self.endtime) if self.endtime else now_ms()
 
        if start_ms >= end_ms:
            raise ValueError(
                f"starttime ({_fmt_dt(start_ms)}) must be before "
                f"endtime ({_fmt_dt(end_ms)})."
            )
 
        source   = self._get_source()
        filepath = self._resolve_filepath()
        handler  = CSVHandler(filepath)
 
        # ---- Header --------------------------------------------------
        tf_label = TIMEFRAME_LABELS.get(self._timeframe, self._timeframe)
        print()
        _print(f"{'─' * 56}")
        _print(f"Symbol      : {self._symbol}")
        _print(f"Source      : {self._source_name}")
        _print(f"Timeframe   : {tf_label}  ({self._timeframe})")
        _print(f"Range       : {_fmt_dt(start_ms)}  →  {_fmt_dt(end_ms)}")
        _print(f"Output      : {filepath}")
        _print(f"{'─' * 56}")
 
        # ---- Load existing -------------------------------------------
        existing = handler.load()
        if existing is not None:
            _print(
                f"Existing file : {_fmt_n(len(existing))} candles  "
                f"({_fmt_dt(int(existing['timestamp'].iloc[0]))} → "
                f"{_fmt_dt(int(existing['timestamp'].iloc[-1]))})"
            )
        else:
            _print("Existing file : none — starting fresh")
 
        # ---- Gap detection -------------------------------------------
        _print("Scanning for missing candles…")
 
        if existing is None or existing.empty:
            # No existing data — fetch the whole range as one gap
            gaps = [(start_ms, end_ms)]
        else:
            gaps = handler.find_gaps(existing, self._timeframe, start_ms, end_ms)
 
        if not gaps:
            _print("✓ Everything is up to date — nothing to download.")
            print()
            return filepath
 
        total_gaps = len(gaps)
        _print(f"Found {total_gaps} gap(s) to fill.")
        print()
 
        # ---- Fetch each gap ------------------------------------------
        new_rows: list[dict[str, Any]] = []
 
        for idx, (gap_start, gap_end) in enumerate(gaps, start=1):
            gap_label = (
                f"{_fmt_dt(gap_start)}  →  {_fmt_dt(gap_end)}"
            )
            _print(f"Gap {idx}/{total_gaps}  {gap_label}")
 
            fetched = self._fetch_with_progress(
                source, gap_start, gap_end, idx, total_gaps
            )
            new_rows.extend(fetched)
 
            if fetched:
                _print(
                    f"  └─ {_fmt_n(len(fetched))} candle(s) received"
                )
            else:
                _print("  └─ No data returned for this range (exchange may not have it yet)")
            print()
 
        # ---- Merge & save --------------------------------------------
        if new_rows:
            combined = handler.merge_and_save(existing, new_rows)
            total = len(combined)
        else:
            total = len(existing) if existing is not None else 0
 
        _print(f"{'─' * 56}")
        _print(
            f"✓ Saved  {_fmt_n(total)} candles  →  {filepath}"
        )
        _print(f"{'─' * 56}")
        print()
 
        return filepath
 
    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------
 
    def _fetch_with_progress(
        self,
        source: BaseSource,
        start_ms: int,
        end_ms: int,
        gap_idx: int,
        total_gaps: int,
    ) -> list[dict[str, Any]]:
        """
        Fetch candles for one gap and print a simple inline progress bar.
 
        We estimate total candles in the range to show progress; the
        actual count may differ (exchange may have fewer candles).
        """
        tf       = self._timeframe
        tf_ms    = TIMEFRAME_MS.get(tf)
        estimated = (
            max(1, (end_ms - start_ms) // tf_ms + 1)
            if tf_ms else 50
        )
 
        # ---- Fetch (the source handles pagination internally) ---------
        # We monkey-patch a lightweight progress callback by fetching
        # in smaller windows and printing progress ourselves.
 
        all_candles: list[dict[str, Any]] = []
        cursor = start_ms
        BATCH  = 1000  # Binance max per request
 
        while cursor <= end_ms:
            batch_end = end_ms
 
            # --- Estimate batch end time ---------------------
            if tf_ms:
                batch_end_candidate = cursor + BATCH * tf_ms - 1
                batch_end = min(batch_end_candidate, end_ms)
 
            chunk = source.fetch_candles(self._symbol, tf, cursor, batch_end)
            all_candles.extend(chunk)
 
            # ---- Progress bar --------------------------------
            fetched_so_far = len(all_candles)
            pct = min(fetched_so_far / estimated, 1.0)
            bar = _bar(pct)
            print(
                f"\r    [{bar}]  {_fmt_n(fetched_so_far)} / ~{_fmt_n(estimated)}",
                end="",
                flush=True,
            )
 
            if not chunk or len(chunk) < BATCH:
                break
 
            # Advance cursor
            if tf_ms:
                cursor = chunk[-1]["timestamp"] + tf_ms
            else:
                # Monthly: use last candle ts + 1 month
                last_ts = chunk[-1]["timestamp"]
                dt = ms_to_dt(last_ts)
                m, y = dt.month, dt.year
                if m == 12:
                    y += 1; m = 1
                else:
                    m += 1
                cursor = int(
                    datetime(y, m, 1, tzinfo=timezone.utc).timestamp() * 1000
                )
 
        # Finish the progress line
        final = len(all_candles)
        bar   = _bar(1.0)
        print(
            f"\r    [{bar}]  {_fmt_n(final)} candles fetched          ",
        )
 
        return all_candles
 
    def _get_source(self) -> BaseSource | BaseBroker:
        # A passed-in Broker instance and a registry BaseSource both expose the
        # same ``fetch_candles(symbol, timeframe, start_ms, end_ms)`` signature,
        # so the collect() loop treats them identically.
        if self._fetcher is not None:
            return self._fetcher
        if self._source is None:
            self._source = get_source(self._source_name)
        return self._source

    # ------------------------------------------------------------------
    # Real-time streaming (v0.9.0)
    # ------------------------------------------------------------------

    def stream(
        self,
        on_candle,
        *,
        closed_only: bool = True,
        timeframe: str | None = None,
    ):
        """
        Subscribe to real-time candles for this Collector's symbol.

        *on_candle* receives library-standard candle dicts (same schema as
        ``collect()`` rows, plus a ``"closed"`` flag).  Works for both exchange
        (Binance spot/futures WebSocket) and — via a passed-in MetaTrader
        ``Broker`` — forex feeds.

        Returns a ``Stream`` handle; call ``.stop()`` to end the subscription.
        """
        broker = self._fetcher or Broker(self._source_name)
        tf = timeframe or self._timeframe
        return broker.stream_candles(self._symbol, tf, on_candle, closed_only=closed_only)
 
    def _resolve_filepath(self) -> str:
        if self.outputname:
            name = self.outputname
            if not name.lower().endswith(".csv"):
                name += ".csv"
        else:
            safe_source = self._source_name.replace("/", "-")
            name = f"{safe_source}_{self._symbol}_{self._timeframe}.csv"
 
        dest = self.destination or "./"
        return os.path.abspath(os.path.join(dest, name))
 
    # ------------------------------------------------------------------
    # Repr
    # ------------------------------------------------------------------
 
    def __repr__(self) -> str:
        tf = TIMEFRAME_LABELS.get(self._timeframe, self._timeframe)
        return (
            f"<Collector source={self._source_name!r} "
            f"symbol={self._symbol!r} timeframe={tf!r}>"
        )
