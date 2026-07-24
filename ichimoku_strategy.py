"""
╔══════════════════════════════════════════════════════════════════════════════╗
║              ICHIMOKU STRATEGY  —  AlgoTradeKit v1.0.0                       ║
║              Personal strategy file (NOT a built-in library strategy)        ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  Features:                                                                   ║
║  • Ichimoku Cloud with configurable non-standard periods (default 8-22-44-22)║
║  • ATR-based stop loss  OR  behind-cloud stop loss (with ATR fallback)       ║
║  • Multi-TP: 1R..20R intervals, SL trails to previous TP (≈ infinite TP)     ║
║  • Force-close on Tenkan/Kijun crossover against the open position           ║
║  • Multi-timeframe confirmation (15m primary, optional 5m + 1m confirm)      ║
║  • RSI filter on entry                                                       ║
║  • MT5-style lot sizing (built into the library, exchange_type="metatrader") ║
║  • Multi-symbol support with a shared wallet (run_multi)                     ║
║  • Batch runs over any combination of config parameters                      ║
║  • Candle chart with flexible range filter (date range / last N / first N)   ║
║  • Wallet balance history CSV export                                         ║
║  • MODE 2 (v1.0.0): live PAPER trading on MetaTrader — seed history, then    ║
║    keep simulating on the live feed (chart + report + terminal event log)    ║
╚══════════════════════════════════════════════════════════════════════════════╝

DATA FORMAT ACCEPTED
────────────────────
1-minute OHLCV CSVs with Unix-second OR millisecond timestamps. Example
(broker / MT5 export):

    timestamp,open,high,low,close,volume
    1766421600,0.91485,0.91489,0.91479,0.91483,0
    1766421660,0.91485,0.91487,0.91479,0.91482,0

``Normalizer`` (AlgoTradeKit v0.7.2) converts this automatically — no manual
timestamp handling needed.

HOW TO RUN
──────────
    MODE 1 — backtest on CSV files (RUN_MODE = 1, the default)
    1. Set DATA_FILES and SYMBOLS in the ── Multi-symbol config ── section.
    2. Adjust ── Strategy config ── and ── Backtest config ── to your needs.
    3. Single run:   python ichimoku_strategy.py
    4. Batch run:    set BATCH_MODE = True and fill the BATCH_* lists.
    5. Multi-symbol: set MULTI_SYMBOL = True (shared wallet across symbols).

    MODE 2 — live paper trading on MetaTrader 5 (RUN_MODE = 2, v1.0.0)
    1. Have the MT5 bridge running (see MT5_WINE_SETUP.md) — or run on
       Windows, where the library connects natively.
    2. Fill the ── Live paper trading config ── block: LIVE_SYMBOL (the EXACT
       MT5 name — `python demo.py` lists them) and the seed history, either
       LIVE_CANDLES = 1000 (last N candles) or LIVE_START = "2026/01/01"
       (from a date until now) — exactly one of the two.
    3. Set RUN_MODE = 2 and run:  python ichimoku_strategy.py
    It seeds that history, simulates it, opens the live chart + report, then
    keeps stepping the simulation on every closed 15m candle and prints every
    event to the terminal. **No order is ever placed** — this is paper trading
    through the library's `run_live()` (AlgoTradeKit v1.0.0), so any strategy
    gets the same treatment for free. Ctrl+C stops it.

    Mode 2 runs on the primary timeframe only (15m): the live feed is a single
    timeframe, so CHECK_LOWER_TF is ignored there.

HOW TO SWITCH APPROACHES
────────────────────────
    SL mode        → flip SL_BEHIND_CLOUD     (True = cloud SL, False = ATR SL)
    Force close    → flip FORCE_CLOSE
    LTF confirm    → flip CHECK_LOWER_TF
    Batch sweep    → set  BATCH_MODE = True and fill the BATCH_* lists

A NOTE ON LIBRARY ARCHITECTURE (read once)
───────────────────────────────────────────
AlgoTradeKit's strategy layer is intentionally stateless with respect to the
simulation engine: ``generate_signals`` / ``detect_exit_signals`` are called
once per candle over the *entire* dataset, independent of what the engine
later decides to open or close (this lets ``run_batch`` re-simulate the same
signals against many configs without re-running indicator code). Because of
this, a strategy cannot ask "is a position currently open, and in which
direction?" — that information only exists inside the simulation engine.

The original script tracked open positions directly and force-closed a LONG
only when `tenkan < kijun` (and a SHORT only when `tenkan > kijun`). To
reproduce this exactly without engine-side position visibility, this file
detects Tenkan/Kijun **crossovers** (the transition, not just the level) and
emits an ExitSignal on every crossover, in either direction. Combined with
the engine's default ``max_positions = 1`` (only one position open at a
time — same as the original script), the next crossover after an entry is
*always* the correct force-close condition for whichever position is open:
a LONG was entered while tenkan > kijun, so the next crossover is tenkan
dropping below kijun (correct LONG force-close); a SHORT was entered while
tenkan < kijun, so the next crossover is tenkan rising above kijun (correct
SHORT force-close). When no position is open, a crossover's ExitSignal is a
harmless no-op (the engine only acts on it if a position exists at that
candle). This was verified against the real engine in v0.7.2 testing and
produces identical behaviour to the original per-position check.
"""

from __future__ import annotations

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Standard library
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
import itertools
import os
from typing import Any

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Third-party
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
import pandas as pd

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# AlgoTradeKit
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
from AlgoTradeKit.broker import Broker
from AlgoTradeKit.data import Converter, Normalizer
from AlgoTradeKit.indicator import ATR, RSI, Ichimoku
from AlgoTradeKit.simulate import Simulate, SimulateConfig, run_multi
from AlgoTradeKit.strategy import BaseStrategy, ExitSignal, Signal, StrategyMode
from AlgoTradeKit.trader import TraderConfig, run_live
from AlgoTradeKit.visual import Chart

# ══════════════════════════════════════════════════════════════════════════════
# ██  SECTION 1 — CONFIGURATION  (edit here to switch behaviour)  ██
# ══════════════════════════════════════════════════════════════════════════════

# ── Run mode ──────────────────────────────────────────────────────────────────
# 1 = backtest CSV files (all the config below applies).
# 2 = live PAPER trading on MetaTrader 5 (v1.0.0): seed history, then keep
#     simulating on the live feed — chart + report + terminal event log.
#     No order is ever placed. See the ── Live paper trading config ── block.
RUN_MODE: int = 1

# ── Multi-symbol config ────────────────────────────────────────────────────────

# Path(s) to 1-minute OHLCV CSV files.
# Single symbol:    DATA_FILES = ["path/to/EURUSD_1m.csv"]
# Multiple symbols: DATA_FILES = ["EURUSD_1m.csv", "USDJPY_1m.csv"]
DATA_FILES: list[str] = [
    "data/usdjpy_1m.csv",     # example — change to your actual path
]

# Trading symbol for each file (same order as DATA_FILES). Used for MT5 lot
# sizing. Case-insensitive — see AlgoTradeKit.simulate._lot for supported
# instruments (forex majors/crosses, metals, crypto, indices, commodities).
SYMBOLS: list[str] = [
    "usdjpy",
]

# True  → all symbols share a single wallet (AlgoTradeKit.simulate.run_multi).
# False → each symbol is simulated independently with its own wallet.
MULTI_SYMBOL: bool = False

# ── Data normalisation / date filtering ───────────────────────────────────────

# None = from the very beginning / to the very end of the data file.
# Accepts: "YYYY/MM/DD", "YYYY-MM-DD", datetime object, Unix int (s or ms), or None.
DATA_START: Any = "2025/09/01"
DATA_END:   Any = None

# ── Ichimoku parameters ───────────────────────────────────────────────────────
# Default: non-standard periods (8-22-44-22). TradingView standard is 9-26-52-26.
TENKAN_PERIOD: int = 8
KIJUN_PERIOD:  int = 22
SPAN_B_PERIOD: int = 44
DISPLACEMENT:  int = 22

# ── RSI filter ────────────────────────────────────────────────────────────────
USE_RSI_FILTER: bool = True
RSI_PERIOD:      int = 14
# Long requires:  RSI_LONG_MIN  < rsi < RSI_LONG_MAX   (strict, matches original)
# Short requires: RSI_SHORT_MIN < rsi < RSI_SHORT_MAX  (strict)
RSI_LONG_MIN:   float = 50.0
RSI_LONG_MAX:   float = 70.0
RSI_SHORT_MIN:  float = 30.0
RSI_SHORT_MAX:  float = 50.0

# ── Multi-timeframe lower confirmation ────────────────────────────────────────
# When True, a 15m signal is only valid if the same conditions also hold on
# the 5m candle at T+10m and the 1m candle at T+14m within the same 15m bar.
CHECK_LOWER_TF: bool = False

# ── Stop loss ─────────────────────────────────────────────────────────────────
ATR_MULTIPLIER: float = 3.0   # ATR-based SL: entry ± ATR_MULTIPLIER × ATR(14)

# Cloud SL: walk backwards from entry to find the most recent opposite-regime
# cloud zone (bearish for longs / bullish for shorts) and place SL at the
# extreme span_a_raw of that zone. Falls back to ATR SL if no such zone is
# found, or if the resulting SL distance exceeds CLOUD_SL_ATR_LIMIT × ATR.
SL_BEHIND_CLOUD:    bool  = False
CLOUD_SL_ATR_LIMIT: float = 5.0

# ── Force close ───────────────────────────────────────────────────────────────
# Close the open position on the next Tenkan/Kijun crossover (see the
# architecture note above the imports for why this reproduces the original
# per-direction force-close behaviour exactly).
FORCE_CLOSE: bool = False

# ── Backtest costs / sizing ────────────────────────────────────────────────────
INITIAL_BALANCE: float = 10_000.0
LEVERAGE:          int = 10
RISK_PERCENT:    float = 0.5      # % of initial balance risked per trade
SPREAD:          float = 0.003    # price units — tune per symbol's pip size!
                                   #   e.g. 0.003 ≈ 0.3 pip on JPY pairs (pip=0.01)
                                   #        0.0001 ≈ 1 pip on EURUSD-style pairs
FEE_PER_LOT:     float = 5.0      # USD per lot (MT5-style commission)

# ── Batch mode ────────────────────────────────────────────────────────────────
# Set BATCH_MODE = True to iterate over every combination of the BATCH_* lists.
# Use single-element lists to keep a parameter fixed. NOTE: because atr_mul,
# sl_behind_cloud, cloud_sl_limit, and check_lower_tf all influence signal/SL
# generation (not just sizing), each combination re-runs the full strategy —
# this is unavoidable with this library's architecture but is fast for
# reasonable batch sizes.
BATCH_MODE: bool = False

BATCH_RISK_LIST:        list[float] = [0.5]
BATCH_ATR_MUL_LIST:     list[float] = [3.0]
BATCH_SL_CLOUD_LIST:    list[bool]  = [False]
BATCH_SL_LIMIT_LIST:    list[float] = [5.0]
BATCH_FORCE_CLOSE_LIST: list[bool]  = [False]
BATCH_CHECK_LTF_LIST:   list[bool]  = [False]

# ── Output ────────────────────────────────────────────────────────────────────
OUTPUT_DIR:   str  = "outputs/"   # directory for wallet-history CSVs
SAVE_HISTORY: bool = True         # write balance_history to CSV after each run

# Built-in interactive report (richer than the original matplotlib drawdown
# chart — includes weekday/monthly stats, drawdown periods, trade markers).
SHOW_CHART:   bool = True        # open the candle chart with position boxes
REPORT_MODE:  str  = "webpage"       # "none" | "webpage" | "save" | "both"

# Plain data chart (no positions) for inspecting raw price action, separate
# from the simulation report. Uses the v0.7.2 candle_range filter.
SHOW_DATA_CHART: bool = False
# Choose ONE mode — leave all None/commented for all candles:
CHART_RANGE: dict | None = None
# CHART_RANGE = {"start": "2023/01/01", "end": "2024/01/01"}  # date range
# CHART_RANGE = {"start": "2023/06/01"}                       # from date to end
# CHART_RANGE = {"end":   "2024/01/01"}                       # start to date
# CHART_RANGE = {"last_n":  500}                               # last N candles
# CHART_RANGE = {"first_n": 500}                               # first N candles

# ── Live paper trading config (RUN_MODE = 2) ──────────────────────────────────
# The instrument, spelled EXACTLY as your MT5 broker spells it — run
# `python demo.py` to list the account's symbols. It is also what the engine
# uses for MT5 lot sizing, so a broker-suffixed name (e.g. "USDJPY_i") falls
# back to the generic pip table in AlgoTradeKit.simulate._lot (sizing then
# approximates a 0.0001-pip / $10-per-pip instrument).
LIVE_SYMBOL: str = "USDJPY"

# Seed history — set EXACTLY ONE of these (the other stays None):
LIVE_CANDLES: int | None = 1000     # the last N 15m candles …
LIVE_START:   Any = None            # … or everything from this date until now
# LIVE_START = "2026/01/01"

# Candles the strategy needs before it may trade (warmup + margin).
LIVE_MIN_CANDLES: int = 200

# MT5 connection — the same knobs as demo.py. Leave the credentials None when
# the bridge was started with --login/--password/--server.
LIVE_MT5_MODE:    str = "auto"        # "auto" | "native" (Windows) | "bridge"
LIVE_BRIDGE_HOST: str = "127.0.0.1"   # bridge address (or your SSH tunnel)
LIVE_BRIDGE_PORT: int = 18812
LIVE_LOGIN:    int | None = None
LIVE_PASSWORD: str | None = None
LIVE_SERVER:   str | None = None

# Output — chart + report, terminal event log, or both (at least one, or the
# run produces nothing visible and run_live warns).
LIVE_DISPLAY:      bool = True        # live chart + live report
LIVE_OPEN_BROWSER: bool = True        # False on a VPS → the URLs are printed
LIVE_CHART_HOST:   str  = "127.0.0.1" # "0.0.0.0" to expose publicly
LIVE_CHART_PORT:   int  = 0           # 0 = auto
LIVE_REPORT_PORT:  int  = 0           # 0 = auto
LIVE_LOG_EVENTS:   bool = True        # [SIM] signal / open / SL move / close lines
LIVE_CANDLE_COUNT_LIMIT: int | None = None   # rolling window; None = unbounded

# Costs: the spread comes from the venue's own symbol_info; MT5 never reports
# commission, so FEE_PER_LOT (above) is sent as the per-lot override.


# ══════════════════════════════════════════════════════════════════════════════
# ██  SECTION 2 — STRATEGY CLASS  ██
# ══════════════════════════════════════════════════════════════════════════════

def _any_nan(row: pd.Series, cols: list[str]) -> bool:
    """Return True if any of *cols* in *row* is NaN / missing."""
    for col in cols:
        if pd.isna(row.get(col, float("nan"))):
            return True
    return False


class IchimokuStrategy(BaseStrategy):
    """
    Ichimoku strategy operating on three timeframes: 1m, 5m, and 15m.

    Primary timeframe is 15m — entry/exit conditions are evaluated on every
    15m candle. Optional lower-timeframe confirmation checks the 5m candle
    at T+10m and the 1m candle at T+14m within the same 15m period.

    Parameters
    ----------
    cfg : dict
        Strategy configuration snapshot for this run (see ``_make_cfg``).
    """

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.primary_timeframe = "15m"
        # Skip candles until every indicator has warmed up:
        #   span_b_period + displacement bars for the Ichimoku cloud,
        #   plus a small safety margin for the tenkan_15 lookback and RSI.
        self.warmup_period = cfg["span_b_period"] + cfg["displacement"] + 20

    # ── Indicator preparation ────────────────────────────────────────────────

    def prepare_indicators(self, data: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
        """
        Compute ATR, Ichimoku, and RSI on every timeframe present in *data*.

        Backtests (mode 1) pass all three (1m/5m/15m); live paper trading
        (mode 2) passes only the primary 15m timeframe — the live feed is
        single-timeframe — so the loop follows the keys it is given rather
        than a fixed list.
        """
        cfg = self.cfg
        for tf in list(data):
            df = data[tf].copy()
            high, low, close = df["high"], df["low"], df["close"]

            df["_atr"] = ATR(high, low, close, period=14).atr.values

            ichi = Ichimoku(
                high, low, close,
                tenkan_period=cfg["tenkan_period"],
                kijun_period=cfg["kijun_period"],
                senkou_b_period=cfg["span_b_period"],
                displacement=cfg["displacement"],
            )
            df["_tenkan"]     = ichi.tenkan.values
            df["_kijun"]      = ichi.kijun.values
            df["_senkou_a"]   = ichi.senkou_a.values    # displayed (shifted) cloud
            df["_senkou_b"]   = ichi.senkou_b.values    # displayed (shifted) cloud
            df["_span_a_raw"] = ichi.span_a_raw.values  # unshifted — v0.7.2
            df["_span_b_raw"] = ichi.span_b_raw.values  # unshifted — v0.7.2
            df["_tenkan_15"]  = ichi.tenkan.shift(15).values

            df["_rsi"] = RSI(close, length=cfg["rsi_period"]).rsi.values

            data[tf] = df
        return data

    # ── Entry signal generation (15m primary) ────────────────────────────────

    def generate_signals(
        self,
        candle_index: int,
        data: dict[str, pd.DataFrame],
    ) -> list[Signal]:
        """Called once per 15m candle. Returns 0, 1, or 2 entry signals."""
        df15 = data["15m"]
        candle = df15.iloc[candle_index]

        if _any_nan(candle, ["_atr", "_tenkan", "_kijun", "_span_a_raw",
                              "_span_b_raw", "_senkou_a", "_senkou_b", "_tenkan_15"]):
            return []

        cfg = self.cfg
        signals: list[Signal] = []

        for direction in ("long", "short"):
            if not self._check_conditions(candle, direction, cfg):
                continue

            if cfg["check_lower_tf"]:
                ts_15m_ms = int(candle["timestamp"])
                if not self._confirm_lower_tf(ts_15m_ms, data, direction, cfg):
                    continue

            entry = float(candle["close"])
            sl = self._calculate_sl(candle_index, df15, direction, entry, cfg)
            sl_dist = abs(entry - sl)
            tp = entry + sl_dist if direction == "long" else entry - sl_dist

            signals.append(Signal(
                direction=direction,
                entry_price=entry,
                stop_loss=sl,
                take_profit=tp,        # first TP target = 1R (multi_rr handles the rest)
                timestamp=int(candle["timestamp"]),
                candle_index=candle_index,
                timeframe="15m",
                metadata={},
            ))

        return signals

    # ── Force-close exit signal (Tenkan/Kijun crossover) ─────────────────────

    def detect_exit_signals(
        self,
        candle_index: int,
        data: dict[str, pd.DataFrame],
    ) -> list[ExitSignal]:
        """
        Emit an ExitSignal on every Tenkan/Kijun crossover (either direction)
        when FORCE_CLOSE is enabled. See the architecture note at the top of
        this file for why a direction-agnostic crossover correctly reproduces
        the original per-direction force-close logic given max_positions=1.
        """
        if not self.cfg["force_close"] or candle_index < 1:
            return []

        df15 = data["15m"]
        curr = df15.iloc[candle_index]
        prev = df15.iloc[candle_index - 1]

        if _any_nan(curr, ["_tenkan", "_kijun"]) or _any_nan(prev, ["_tenkan", "_kijun"]):
            return []

        c_tenkan, c_kijun = float(curr["_tenkan"]), float(curr["_kijun"])
        p_tenkan, p_kijun = float(prev["_tenkan"]), float(prev["_kijun"])

        crossed_down = p_tenkan >= p_kijun and c_tenkan < c_kijun   # long force-close
        crossed_up   = p_tenkan <= p_kijun and c_tenkan > c_kijun   # short force-close

        if crossed_down or crossed_up:
            return [ExitSignal(
                reason="tenkan_kijun_cross",
                exit_price=float(curr["close"]),
                timestamp=int(curr["timestamp"]),
                candle_index=candle_index,
                metadata={"direction": "down" if crossed_down else "up"},
            )]

        return []

    # ── Private helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _check_conditions(candle: pd.Series, direction: str, cfg: dict) -> bool:
        """
        Evaluate all 15m Ichimoku + RSI entry conditions for `direction`.

        Long  (all must hold): tenkan>kijun, close>tenkan, bullish cloud now
        (span_a_raw>span_b_raw), close above current cloud top, close above
        the displayed (shifted) cloud, close>=tenkan from 15 bars ago, and
        (optionally) RSI strictly between RSI_LONG_MIN and RSI_LONG_MAX.

        Short: mirror of long, all inequalities flipped.
        """
        t      = float(candle["_tenkan"])
        k      = float(candle["_kijun"])
        c      = float(candle["close"])
        sa     = float(candle["_span_a_raw"])    # unshifted Span A (current bar)
        sb     = float(candle["_span_b_raw"])    # unshifted Span B (current bar)
        sa_loc = float(candle["_senkou_a"])      # displayed (shifted) cloud
        sb_loc = float(candle["_senkou_b"])      # displayed (shifted) cloud
        t15    = float(candle["_tenkan_15"])
        rsi    = float(candle["_rsi"])

        if direction == "long":
            return (
                t > k
                and c > t
                and sa > sb
                and c > sa
                and c > sa_loc
                and c > sb_loc
                and c >= t15
                and (not cfg["use_rsi_filter"]
                     or (cfg["rsi_long_max"] > rsi > cfg["rsi_long_min"]))
            )
        else:  # short
            return (
                t < k
                and c < t
                and sa < sb
                and c < sa
                and c < sa_loc
                and c < sb_loc
                and c <= t15
                and (not cfg["use_rsi_filter"]
                     or (cfg["rsi_short_max"] > rsi > cfg["rsi_short_min"]))
            )

    def _confirm_lower_tf(
        self,
        ts_15m_ms: int,
        data: dict[str, pd.DataFrame],
        direction: str,
        cfg: dict,
    ) -> bool:
        """
        Multi-timeframe confirmation: the 5m candle at 15m_open+10min and the
        1m candle at 15m_open+14min must both satisfy the same conditions.
        """
        for tf, offset_min in (("5m", 10), ("1m", 14)):
            ts_ms = ts_15m_ms + offset_min * 60 * 1000
            df = data[tf]
            row = df.loc[df["timestamp"] == ts_ms]
            if row.empty:
                return False
            candle = row.iloc[0]
            if _any_nan(candle, ["_tenkan", "_kijun", "_span_a_raw", "_span_b_raw",
                                  "_senkou_a", "_senkou_b", "_tenkan_15", "_rsi"]):
                return False
            if not self._check_conditions(candle, direction, cfg):
                return False
        return True

    def _calculate_sl(
        self,
        candle_index: int,
        df15: pd.DataFrame,
        direction: str,
        entry: float,
        cfg: dict,
    ) -> float:
        """
        Calculate stop loss price.

        ATR mode: entry ∓ ATR_MULTIPLIER × ATR.

        Cloud mode: walk backwards from the entry candle to find the most
        recent opposite-regime cloud zone (bearish for longs, bullish for
        shorts) and place SL at the most extreme span_a_raw value seen in
        that zone (lowest for longs, highest for shorts). Falls back to ATR
        SL if no such zone exists, or if the resulting distance exceeds
        CLOUD_SL_ATR_LIMIT × ATR.
        """
        candle = df15.iloc[candle_index]
        atr = float(candle["_atr"])

        def _atr_sl() -> float:
            sl_dist = cfg["atr_multiplier"] * atr
            return (entry - sl_dist) if direction == "long" else (entry + sl_dist)

        if not cfg["sl_behind_cloud"]:
            return _atr_sl()

        sl: float | None = None
        entered_opposite = False

        for i in range(candle_index, -1, -1):
            bar = df15.iloc[i]
            sa, sb = bar.get("_span_a_raw"), bar.get("_span_b_raw")
            if pd.isna(sa) or pd.isna(sb):
                break
            sa, sb = float(sa), float(sb)

            cloud_bullish = (sa > sb) if direction == "long" else (sa < sb)
            in_opposite_zone = (not cloud_bullish) if direction == "long" else cloud_bullish

            if not in_opposite_zone:
                if entered_opposite:
                    break   # left the opposite-cloud zone — stop walking back
                continue    # still in the same-direction zone — keep walking back

            entered_opposite = True
            if direction == "long":
                if sl is None or sa < sl:
                    sl = sa
            else:
                if sl is None or sa > sl:
                    sl = sa

        if sl is None:
            return _atr_sl()

        if abs(entry - sl) > cfg["cloud_sl_atr_limit"] * atr:
            return _atr_sl()

        return sl


# ══════════════════════════════════════════════════════════════════════════════
# ██  SECTION 3 — DATA LOADING  ██
# ══════════════════════════════════════════════════════════════════════════════

def load_symbol_data(
    csv_path: str,
    data_start: Any,
    data_end: Any,
    work_dir: str,
) -> dict[str, pd.DataFrame]:
    """
    Normalise a raw 1m OHLCV CSV and resample it to 5m and 15m.

    Steps
    -----
    1. Normalizer  — auto-detect timestamp unit, fill missing optional
                     columns, apply the [data_start, data_end] date filter.
    2. Converter   — resample 1m → 5m and 1m → 15m. ``Converter.convert()``
                     always writes a CSV and returns its path, so each result
                     is reloaded with ``pd.read_csv``.

    Returns
    -------
    dict with keys "1m", "5m", "15m".
    """
    print(f"\n  Loading {csv_path} …")

    norm = Normalizer(csv_path)
    norm.start = data_start
    norm.end = data_end
    df_1m = norm.normalize()

    conv_dir = os.path.join(work_dir, "converted")
    os.makedirs(conv_dir, exist_ok=True)

    conv_5m = Converter(df_1m, target_timeframe="5m")
    conv_5m.destination = conv_dir
    df_5m = pd.read_csv(conv_5m.convert())

    conv_15m = Converter(df_1m, target_timeframe="15m")
    conv_15m.destination = conv_dir
    df_15m = pd.read_csv(conv_15m.convert())

    print(f"  1m  candles : {len(df_1m):,}")
    print(f"  5m  candles : {len(df_5m):,}")
    print(f"  15m candles : {len(df_15m):,}")

    return {"1m": df_1m, "5m": df_5m, "15m": df_15m}


# ══════════════════════════════════════════════════════════════════════════════
# ██  SECTION 4 — CONFIG BUILDERS  ██
# ══════════════════════════════════════════════════════════════════════════════

def _make_strategy_cfg(
    *,
    tenkan_period: int = TENKAN_PERIOD,
    kijun_period: int = KIJUN_PERIOD,
    span_b_period: int = SPAN_B_PERIOD,
    displacement: int = DISPLACEMENT,
    use_rsi_filter: bool = USE_RSI_FILTER,
    rsi_period: int = RSI_PERIOD,
    rsi_long_min: float = RSI_LONG_MIN,
    rsi_long_max: float = RSI_LONG_MAX,
    rsi_short_min: float = RSI_SHORT_MIN,
    rsi_short_max: float = RSI_SHORT_MAX,
    check_lower_tf: bool = CHECK_LOWER_TF,
    atr_multiplier: float = ATR_MULTIPLIER,
    sl_behind_cloud: bool = SL_BEHIND_CLOUD,
    cloud_sl_atr_limit: float = CLOUD_SL_ATR_LIMIT,
    force_close: bool = FORCE_CLOSE,
) -> dict:
    """Assemble a strategy configuration dict (consumed by IchimokuStrategy)."""
    return dict(
        tenkan_period=tenkan_period, kijun_period=kijun_period,
        span_b_period=span_b_period, displacement=displacement,
        use_rsi_filter=use_rsi_filter, rsi_period=rsi_period,
        rsi_long_min=rsi_long_min, rsi_long_max=rsi_long_max,
        rsi_short_min=rsi_short_min, rsi_short_max=rsi_short_max,
        check_lower_tf=check_lower_tf,
        atr_multiplier=atr_multiplier, sl_behind_cloud=sl_behind_cloud,
        cloud_sl_atr_limit=cloud_sl_atr_limit, force_close=force_close,
    )


def build_sim_config(symbol: str, strat_cfg: dict, risk_percent: float) -> SimulateConfig:
    """
    Build a ``SimulateConfig``. Uses ``tp_mode="multi_rr"`` with R-levels
    1..20 to approximate the original strategy's "infinite" multi-TP / SL
    trailing-to-previous-level behaviour (very few trades reach 20R, so the
    cap is immaterial in practice). Lot sizing and per-lot commission are
    handled natively by ``exchange_type="metatrader"``.

    When ``SHOW_CHART=True``, the Ichimoku cloud and RSI used by the strategy
    are automatically rendered on the simulation chart via ``chart_indicators``
    (v0.8.0). Their parameters match the current ``strat_cfg`` so the chart
    always reflects the exact indicator settings used in the backtest.
    """
    _chart_indicators = []
    if SHOW_CHART:
        # Mirror the exact indicators the strategy trades on (same params), so
        # the simulation chart shows the Ichimoku cloud + RSI it was computed
        # against. All maths run in the backend (AlgoTradeKit v0.8.0). Add any
        # extra indicators here too — or live, via the chart's INDICATORS button.
        _chart_indicators = [
            {
                "kind":         "ichimoku",
                "tenkan":       strat_cfg.get("tenkan_period", TENKAN_PERIOD),
                "kijun":        strat_cfg.get("kijun_period",  KIJUN_PERIOD),
                "senkou_b":     strat_cfg.get("span_b_period", SPAN_B_PERIOD),
                "displacement": strat_cfg.get("displacement",  DISPLACEMENT),
            },
            {
                "kind":   "rsi",
                "period": strat_cfg.get("rsi_period", RSI_PERIOD),
                "source": "close",
            },
        ]

    return SimulateConfig(
        symbol=symbol,
        exchange_type="metatrader",
        initial_balance=INITIAL_BALANCE,
        leverage=LEVERAGE,
        risk_per_trade=risk_percent,
        spread=SPREAD,
        commission_type="per_lot",
        commission=FEE_PER_LOT,
        tp_mode="multi_rr",
        tp_levels=[float(i) for i in range(1, 21)],   # 1R … 20R
        force_close_on_exit_signal=strat_cfg["force_close"],
        primary_timeframe="15m",
        show_chart=SHOW_CHART,
        report_mode=REPORT_MODE,
        chart_indicators=_chart_indicators,
    )


# ══════════════════════════════════════════════════════════════════════════════
# ██  SECTION 5 — RUN MODES: SINGLE / MULTI-SYMBOL / BATCH  ██
# ══════════════════════════════════════════════════════════════════════════════

def _save_balance_history(balance_history: list[dict], label: str) -> None:
    if not SAVE_HISTORY or not balance_history:
        return
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    safe_label = label.replace("/", "-").replace(" ", "_")
    path = os.path.join(OUTPUT_DIR, f"wallet_{safe_label}.csv")
    pd.DataFrame(balance_history).to_csv(path, index=False)
    print(f"  Wallet history → {path}")


def _print_report(report, label: str, strat_cfg: dict, risk_percent: float) -> None:
    """Pretty-print a single SimulateReport."""
    print(f"\n  ╔══ {label} {'═' * max(0, 50 - len(label))}╗")
    print(f"  ║  Config: risk={risk_percent}%  atr_mul={strat_cfg['atr_multiplier']}  "
          f"cloud_sl={strat_cfg['sl_behind_cloud']}  fc={strat_cfg['force_close']}  "
          f"ltf={strat_cfg['check_lower_tf']}")
    print(f"  ║  Trades : {report.total_trades}  (TP:{report.tp_count}  "
          f"SL:{report.sl_count}  RF:{report.risk_free_count}  "
          f"FC:{report.force_close_count}  EOD:{report.end_of_data_count})")
    print(f"  ║  Win rate : {report.win_rate:.1f}%")
    print(f"  ║  Balance  : {report.final_balance:,.2f}  "
          f"({'▲' if report.total_pnl >= 0 else '▼'} {abs(report.total_pnl):,.2f}, "
          f"{report.total_pnl_percent:+.2f}%)")
    if report.max_drawdown is not None:
        print(f"  ║  Max drawdown : {report.max_drawdown.drawdown_percent:.2f}%")
    print(f"  ╚══{'═' * 56}╝")


def run_single(
    datasets: dict[str, dict[str, pd.DataFrame]],
    symbols: list[str],
    strat_cfg: dict,
    risk_percent: float,
    label: str = "",
) -> None:
    """Run one backtest configuration, single-symbol or shared-wallet multi-symbol."""
    if MULTI_SYMBOL and len(symbols) > 1:
        pairs = []
        for sym in symbols:
            strategy = IchimokuStrategy(strat_cfg)
            sim_cfg = build_sim_config(sym, strat_cfg, risk_percent)
            pairs.append((strategy, datasets[sym], sim_cfg))

        report = run_multi(pairs)
        _print_report(report, label or "multi-symbol", strat_cfg, risk_percent)
        _save_balance_history(report.balance_history, label or "multi_symbol")

    else:
        for sym in symbols:
            strategy = IchimokuStrategy(strat_cfg)
            strategy_result = strategy.run(datasets[sym], mode=StrategyMode.BACKTEST)
            sim_cfg = build_sim_config(sym, strat_cfg, risk_percent)
            report = Simulate(sim_cfg).run(strategy_result)

            _print_report(report, label or sym, strat_cfg, risk_percent)
            _save_balance_history(report.balance_history, label or sym)


def run_batch(datasets: dict[str, dict[str, pd.DataFrame]], symbols: list[str]) -> None:
    """Grid-search over every combination of the BATCH_* parameter lists."""
    param_grid = list(itertools.product(
        BATCH_RISK_LIST, BATCH_ATR_MUL_LIST, BATCH_SL_CLOUD_LIST,
        BATCH_SL_LIMIT_LIST, BATCH_FORCE_CLOSE_LIST, BATCH_CHECK_LTF_LIST,
    ))

    print(f"\n  Batch mode: {len(param_grid)} combination(s)\n")
    print(f"  {'#':>4}  {'risk':>5}  {'atr_mul':>7}  {'cloud_sl':>8}  "
          f"{'sl_lim':>6}  {'fc':>5}  {'ltf':>5}  {'final_bal':>11}  {'trades':>7}")
    print("  " + "─" * 76)

    for run_num, (risk, atr_mul, sl_cloud, sl_lim, fc, ltf) in enumerate(param_grid, 1):
        strat_cfg = _make_strategy_cfg(
            atr_multiplier=atr_mul, sl_behind_cloud=sl_cloud,
            cloud_sl_atr_limit=sl_lim, force_close=fc, check_lower_tf=ltf,
        )
        label = f"r{risk}_a{atr_mul}_c{sl_cloud}_l{sl_lim}_fc{fc}_ltf{ltf}"

        for sym in symbols:
            strategy = IchimokuStrategy(strat_cfg)
            strategy_result = strategy.run(datasets[sym], mode=StrategyMode.BACKTEST)
            sim_cfg = build_sim_config(sym, strat_cfg, risk)
            report = Simulate(sim_cfg).run(strategy_result)

            print(f"  {run_num:>4}  {risk:>5.2f}  {atr_mul:>7.1f}  {str(sl_cloud):>8}  "
                  f"{sl_lim:>6.1f}  {str(fc):>5}  {str(ltf):>5}  "
                  f"{report.final_balance:>11,.2f}  {report.total_trades:>7}")

            _save_balance_history(report.balance_history, f"{sym}_{label}")


# ══════════════════════════════════════════════════════════════════════════════
# ██  SECTION 6 — MODE 2: LIVE PAPER TRADING (AlgoTradeKit v1.0.0)  ██
# ══════════════════════════════════════════════════════════════════════════════

def build_live_strategy_cfg() -> dict:
    """
    Strategy config for the live feed.

    The live feed carries the primary timeframe only (15m), so the optional
    5m/1m confirmation cannot be evaluated — it is switched off with a note
    instead of failing mid-run.
    """
    strat_cfg = _make_strategy_cfg()
    if strat_cfg["check_lower_tf"]:
        print("  NOTE: live mode is single-timeframe (15m) — CHECK_LOWER_TF ignored.")
        strat_cfg["check_lower_tf"] = False
    return strat_cfg


def build_live_config(strat_cfg: dict, risk_percent: float) -> TraderConfig:
    """
    Build the ``TraderConfig`` for mode 2 — the same field names as the
    backtest ``SimulateConfig`` (that is the point of the class), plus the
    live-only feed/display knobs.

    Costs: the spread is left as ``None`` so ``run_live`` fills it from the
    venue's own ``symbol_info``; MT5 never reports commission, so
    ``FEE_PER_LOT`` is passed as the per-lot override.
    """
    if (LIVE_CANDLES is None) == (LIVE_START is None):
        raise ValueError(
            "Live mode needs exactly one history seed: set LIVE_CANDLES (last N "
            "candles) or LIVE_START (from a date until now), and leave the other None."
        )

    return TraderConfig(
        symbol=LIVE_SYMBOL,
        min_candles=LIVE_MIN_CANDLES,
        leverage=LEVERAGE,
        risk_per_trade=risk_percent,
        tp_mode="multi_rr",
        tp_levels=[float(i) for i in range(1, 21)],       # 1R … 20R, same as the backtest
        force_close_on_exit_signal=strat_cfg["force_close"],
        spread=None,                                      # ← auto from the venue
        commission_type="per_lot",
        commission=FEE_PER_LOT,                           # MT5 does not expose it
        display=LIVE_DISPLAY,
        display_candles=LIVE_CANDLES,
        display_start=LIVE_START,
        display_open_browser=LIVE_OPEN_BROWSER,
        chart_host=LIVE_CHART_HOST,
        chart_port=LIVE_CHART_PORT,
        report_port=LIVE_REPORT_PORT,
        candle_count_limit=LIVE_CANDLE_COUNT_LIMIT,
        log_events=LIVE_LOG_EVENTS,
    )


def connect_live_broker():
    """Connect to MetaTrader — natively on Windows, else through the bridge."""
    broker = Broker(
        "metatrader",
        mode=LIVE_MT5_MODE,
        host=LIVE_BRIDGE_HOST, port=LIVE_BRIDGE_PORT,
        server=LIVE_SERVER, login=LIVE_LOGIN, password=LIVE_PASSWORD,
    )
    where = (
        "in-process (native MetaTrader5)"
        if broker.mode == "native"
        else f"{LIVE_BRIDGE_HOST}:{LIVE_BRIDGE_PORT}"
    )
    print(f"  MT5 transport: {broker.mode} → {where}")
    return broker


def run_live_mode():
    """
    Mode 2 — paper-trade the strategy on the live MT5 feed.

    Seeds the configured history, simulates it, opens the chart + report and
    then advances the simulation on every closed 15m candle, printing each
    event.  ``run_live`` places **no orders** (paper trading, D8): the
    positions are filled by the same simulation engine the backtest uses, so
    mode 1 and mode 2 behave identically on identical candles.  Blocks until
    Ctrl+C, then returns the final report.
    """
    seed = f"last {LIVE_CANDLES} candles" if LIVE_CANDLES else f"from {LIVE_START}"
    print(f"\n  LIVE PAPER TRADING — {LIVE_SYMBOL} 15m, seed: {seed}")
    print("  No orders are placed. Ctrl+C to stop.\n")

    strat_cfg = build_live_strategy_cfg()
    config = build_live_config(strat_cfg, RISK_PERCENT)
    broker = connect_live_broker()

    report = run_live(
        strategy=IchimokuStrategy(strat_cfg),
        broker=broker,
        config=config,
        initial_balance=INITIAL_BALANCE,
    )

    if report is not None:
        _print_report(report, f"{LIVE_SYMBOL} (paper)", strat_cfg, RISK_PERCENT)
    return report


# ══════════════════════════════════════════════════════════════════════════════
# ██  SECTION 7 — CHART  ██
# ══════════════════════════════════════════════════════════════════════════════

def show_data_chart(csv_path: str, symbol: str, work_dir: str) -> None:
    """Open an interactive candlestick chart of the normalised 1m data."""
    norm = Normalizer(csv_path)
    norm.start = DATA_START
    norm.end = DATA_END
    df_normalized = norm.normalize()

    chart = Chart(title=f"{symbol.upper()} — 1m", theme="dark")
    chart.set_data(df_normalized, candle_range=CHART_RANGE)
    chart.show(block=True)


# ══════════════════════════════════════════════════════════════════════════════
# ██  SECTION 8 — MAIN  ██
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    print("\n" + "═" * 60)
    print("  ICHIMOKU STRATEGY — AlgoTradeKit v1.0.0")
    print("═" * 60)

    if RUN_MODE not in (1, 2):
        raise ValueError(f"RUN_MODE must be 1 (backtest) or 2 (live paper), got {RUN_MODE!r}.")

    if RUN_MODE == 2:
        run_live_mode()
        print("\n  Done.\n")
        return

    if len(DATA_FILES) != len(SYMBOLS):
        raise ValueError(
            f"DATA_FILES and SYMBOLS must have the same length. "
            f"Got {len(DATA_FILES)} files and {len(SYMBOLS)} symbols."
        )

    work_dir = os.path.abspath("./_atk_work")
    os.makedirs(work_dir, exist_ok=True)

    datasets: dict[str, dict[str, pd.DataFrame]] = {}
    for csv_path, sym in zip(DATA_FILES, SYMBOLS):
        datasets[sym] = load_symbol_data(csv_path, DATA_START, DATA_END, work_dir)

    if SHOW_DATA_CHART:
        show_data_chart(DATA_FILES[0], SYMBOLS[0], work_dir)

    if BATCH_MODE:
        run_batch(datasets, SYMBOLS)
    else:
        strat_cfg = _make_strategy_cfg()
        run_single(datasets, SYMBOLS, strat_cfg, RISK_PERCENT)

    print("\n  Done.\n")


if __name__ == "__main__":
    main()