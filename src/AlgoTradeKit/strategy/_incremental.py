"""
AlgoTradeKit.strategy._incremental
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Incremental (live) strategy computation — v1.0.0.

Goal: never fetch and reprocess a big history just to get the current-moment
signal.  The first analysis runs once over the seed history
(``strategy.run()`` / ``prepare_indicators`` + ``setup``); every new candle
then costs O(1) computation.

Two per-candle paths
--------------------
1. **Hook path** — the strategy overrides
   ``BaseStrategy.update_indicators(data, new_index)``.  The hook is called
   after the new closed candle is appended, exactly once per closed candle,
   in order.  Inside it the strategy updates the indicator columns for the
   new row (e.g. via the indicator classes' streaming ``update()``) **and**
   any custom python structures (order blocks, zones, swing points — SMC /
   price-action state).  This is the supported path for stateful strategies.

2. **Fallback path** — the strategy does not override the hook.
   ``prepare_indicators`` is recomputed over only the **last K candles**
   (K = ``recompute_window``, default ``max(warmup_period, 200)``) and the
   resulting ``_``-prefixed columns are spliced back into the master frame
   for the tail rows.

   Documented limits of the fallback:

   - **exact** for windowed indicators whose lookback ≤ K;
   - **approximate** for infinite-memory recursions (EMA/RMA) unless K is
     generous;
   - **not safe** for strategies that build python-object state inside
     ``prepare_indicators`` (state would re-append every candle) — those
     must implement the hook.

   The splice is **fill-only**: a cell is written the first time the
   recompute produces a value for it (the appended row's cells, warmup
   fills, and retroactive backfills such as chikou-style ``shift(-x)``
   columns) and is never revised afterwards.  Each cell therefore keeps the
   value computed when it had the deepest available lookback — a naive
   overwrite-all splice would degrade history (a cell's lookback inside the
   tail shrinks on every later recompute, decaying to nothing K rows behind
   the tip).  Consequence: repainting indicators that legitimately revise
   past values are not supported by the fallback — implement the hook.

Forming-candle evaluation (realtime modes)
------------------------------------------
Committed strategy state advances **only on closed candles**.  In
``candle_update`` / ``tick`` execution modes, evaluation against the forming
candle runs on a **throwaway copy** of the primary frame: the forming row's
indicator values are tail-computed on the copy via ``prepare_indicators``
(never via ``update_indicators`` — non-final values would corrupt
recursive/SMC state), then ``generate_signals`` / ``detect_exit_signals``
run against the copy.  The master data and any hook-managed ``self.*``
structures are untouched.  For this to hold, ``prepare_indicators`` itself
must not mutate ``self.*`` state — SMC state belongs in ``setup()`` and the
hook.

Multi-timeframe note
--------------------
Only ``data[primary_timeframe]`` advances candle by candle; other
timeframes keep their seed-time content.  The fallback/forming recompute
passes throwaway copies of the other timeframes into ``prepare_indicators``
(so multi-timeframe strategies keep working) but splices only the primary
tail back — for multi-timeframe strategies the fallback is therefore not
O(1); implement the hook for true O(1) updates.

Caller contract
---------------
The caller owns the ``data`` dict (the master frames).  Before stepping,
``prepare_indicators`` and ``setup`` must have run over the seed history
(``strategy.run()`` does both).  ``advance_live_candle`` re-binds
``data[primary_timeframe]`` to the extended frame — always read frames
through the dict, never through a stale reference.  Appending re-indexes
the primary frame to a clean RangeIndex.
"""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd

from ._base import BaseStrategy
from ._types import ExitSignal, Signal

__all__ = [
    "advance_live_candle",
    "default_recompute_window",
    "evaluate_forming_candle",
    "has_update_hook",
]

#: Candle keys every appended/forming candle dict must provide (UTC ms timestamp).
_CANDLE_COLS = ("timestamp", "open", "high", "low", "close", "volume")

#: Lower bound for the default tail-recompute window (spec: max(warmup, 200)).
_RECOMPUTE_WINDOW_FLOOR = 200


# ---------------------------------------------------------------------------
# Introspection helpers
# ---------------------------------------------------------------------------

def has_update_hook(strategy: BaseStrategy) -> bool:
    """
    True when *strategy* overrides ``BaseStrategy.update_indicators``.

    Hook strategies get the O(1) hook path in :func:`advance_live_candle`;
    all others fall back to the tail recompute.
    """
    return type(strategy).update_indicators is not BaseStrategy.update_indicators


def default_recompute_window(strategy: BaseStrategy) -> int:
    """
    Default tail-recompute window for *strategy*:
    ``max(strategy.warmup_period, 200)``.

    Used when ``recompute_window`` is not supplied (``TraderConfig`` passes
    its own value through once the trader module lands).
    """
    return max(int(strategy.warmup_period), _RECOMPUTE_WINDOW_FLOOR)


# ---------------------------------------------------------------------------
# Public stepping API
# ---------------------------------------------------------------------------

def advance_live_candle(
    strategy: BaseStrategy,
    data: dict[str, pd.DataFrame],
    candle: Mapping,
    *,
    recompute_window: int | None = None,
) -> tuple[list[Signal], list[ExitSignal]]:
    """
    Advance the strategy by exactly one new **closed** candle.

    Runs the live per-candle lifecycle on the master ``data`` dict:
    append row → ``update_indicators`` (hook) *or* tail recompute (fallback)
    → ``generate_signals(i)`` → ``detect_exit_signals(i)``.

    Parameters
    ----------
    strategy : BaseStrategy
        Strategy instance.  ``prepare_indicators`` and ``setup`` must
        already have run over the seed history (``strategy.run()`` does
        both).
    data : dict[str, pd.DataFrame]
        Master data dict, keyed by timeframe.  ``data[primary_timeframe]``
        is re-bound to the extended frame — mutated in place as a dict.
    candle : Mapping
        The new closed candle: ``timestamp`` (UTC ms, strictly greater than
        the last row's), ``open``, ``high``, ``low``, ``close``, ``volume``.
        Extra keys (broker stream fields such as ``closed``) are ignored.
    recompute_window : int | None
        Tail size K for the fallback recompute.  ``None`` →
        :func:`default_recompute_window`.  Ignored on the hook path.

    Returns
    -------
    tuple[list[Signal], list[ExitSignal]]
        The new candle's signals.  Empty lists while the frame is still
        shorter than ``strategy.warmup_period`` (the hook/recompute still
        runs so state stays correct).
    """
    tf, df = _require_primary_df(strategy, data)
    values = _validate_candle(candle, df, context="advance_live_candle")

    data[tf] = _append_row(df, values)
    new_index = len(data[tf]) - 1

    if has_update_hook(strategy):
        strategy.update_indicators(data, new_index)
    else:
        window = _resolve_window(strategy, recompute_window)
        _tail_recompute(strategy, data, data[tf], window)

    if new_index < strategy.warmup_period:
        return [], []

    signals = strategy.generate_signals(new_index, data)
    exits = strategy.detect_exit_signals(new_index, data)
    return list(signals or []), list(exits or [])


def evaluate_forming_candle(
    strategy: BaseStrategy,
    data: dict[str, pd.DataFrame],
    candle: Mapping,
    *,
    recompute_window: int | None = None,
) -> tuple[list[Signal], list[ExitSignal]]:
    """
    Evaluate the strategy against a **forming** (unclosed) candle.

    Builds a throwaway copy of the primary frame with *candle* appended,
    tail-computes the forming row's ``_``-prefixed indicator columns on the
    copy via ``prepare_indicators`` (``update_indicators`` is **never**
    called — non-final values would corrupt recursive/SMC state), then runs
    ``generate_signals`` / ``detect_exit_signals`` on the copy.

    The master ``data`` frames are untouched; committed strategy state only
    advances via :func:`advance_live_candle` on closed candles.  Safe to
    call repeatedly for the same forming candle as it updates (realtime
    ``candle_update`` / ``tick`` modes) — acting at most once per candle is
    the caller's job.

    Requires ``prepare_indicators`` to be free of ``self.*`` side effects
    (see module docstring); returned signals carry
    ``candle_index == len(data[primary_timeframe])`` (the forming row).

    Parameters mirror :func:`advance_live_candle`; the tail recompute is
    used for **all** strategies here, hook or not.
    """
    tf, df = _require_primary_df(strategy, data)
    values = _validate_candle(candle, df, context="evaluate_forming_candle")

    eval_df = _append_row(df, values)  # concat → new frame; master untouched
    forming_index = len(eval_df) - 1

    window = _resolve_window(strategy, recompute_window)
    _tail_recompute(strategy, data, eval_df, window)

    if forming_index < strategy.warmup_period:
        return [], []

    eval_data = dict(data)
    eval_data[tf] = eval_df
    signals = strategy.generate_signals(forming_index, eval_data)
    exits = strategy.detect_exit_signals(forming_index, eval_data)
    return list(signals or []), list(exits or [])


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _require_primary_df(
    strategy: BaseStrategy, data: dict[str, pd.DataFrame]
) -> tuple[str, pd.DataFrame]:
    tf = strategy.primary_timeframe
    if tf not in data:
        raise ValueError(
            f"[AlgoTradeKit] Strategy '{type(strategy).__name__}': "
            f"primary_timeframe '{tf}' not found in data. "
            f"Available keys: {sorted(data.keys())}"
        )
    df = data[tf]
    if df.empty:
        raise ValueError(
            f"[AlgoTradeKit] data['{tf}'] is empty — live stepping needs a "
            f"seed history (run the strategy over min_candles first)."
        )
    return tf, df


def _validate_candle(candle: Mapping, df: pd.DataFrame, *, context: str) -> dict:
    """Check keys + monotonic timestamp; return the normalised column values."""
    missing = [k for k in _CANDLE_COLS if k not in candle]
    if missing:
        raise ValueError(
            f"[AlgoTradeKit] {context}: candle is missing required keys "
            f"{missing}. Required: {list(_CANDLE_COLS)}."
        )
    ts = int(candle["timestamp"])
    last_ts = int(df["timestamp"].iloc[-1])
    if ts == last_ts:
        raise ValueError(
            f"[AlgoTradeKit] {context}: candle timestamp {ts} equals the last "
            f"row's — duplicate delivery. The feed layer must dedup candles."
        )
    if ts < last_ts:
        raise ValueError(
            f"[AlgoTradeKit] {context}: candle timestamp {ts} is older than "
            f"the last row's ({last_ts}) — out-of-order delivery."
        )
    values = {k: float(candle[k]) for k in _CANDLE_COLS}
    values["timestamp"] = ts
    return values


def _append_row(df: pd.DataFrame, values: dict) -> pd.DataFrame:
    """Return *df* plus one candle row; indicator columns start as NaN."""
    row = {col: values.get(col, np.nan) for col in df.columns}
    row_df = pd.DataFrame([row], columns=df.columns)
    # Keep the standard columns' dtypes (timestamp must stay int64).
    cast = {col: df[col].dtype for col in _CANDLE_COLS if col in df.columns}
    row_df = row_df.astype(cast)
    return pd.concat([df, row_df], ignore_index=True)


def _resolve_window(strategy: BaseStrategy, recompute_window: int | None) -> int:
    if recompute_window is None:
        return default_recompute_window(strategy)
    if recompute_window < 1:
        raise ValueError(
            f"[AlgoTradeKit] recompute_window must be >= 1, got {recompute_window!r}."
        )
    return int(recompute_window)


def _tail_recompute(
    strategy: BaseStrategy,
    data: dict[str, pd.DataFrame],
    target_df: pd.DataFrame,
    window: int,
) -> None:
    """
    Recompute ``prepare_indicators`` over *target_df*'s last ``window`` rows
    and splice the resulting ``_``-prefixed columns back into *target_df*
    (in place, tail rows only, fill-only — see module docstring).

    The primary frame handed to ``prepare_indicators`` is a fresh-input tail
    copy (``_`` columns stripped, clean RangeIndex); other timeframes are
    passed as throwaway copies and their recomputed output is discarded.
    """
    tf = strategy.primary_timeframe
    n = len(target_df)
    k = min(window, n)

    base_cols = [c for c in target_df.columns if not c.startswith("_")]
    tail_input = target_df.iloc[n - k :][base_cols].reset_index(drop=True).copy()

    call_data = {key: frame.copy() for key, frame in data.items() if key != tf}
    call_data[tf] = tail_input

    enriched = strategy.prepare_indicators(call_data)
    if not isinstance(enriched, dict) or tf not in enriched:
        raise ValueError(
            f"[AlgoTradeKit] Strategy '{type(strategy).__name__}': "
            f"prepare_indicators must return the data dict including '{tf}'."
        )
    tail_out = enriched[tf]
    if len(tail_out) != k:
        raise ValueError(
            f"[AlgoTradeKit] Strategy '{type(strategy).__name__}': "
            f"prepare_indicators changed the row count ({k} -> {len(tail_out)}) "
            f"— it must only add columns."
        )

    for col in tail_out.columns:
        if not col.startswith("_"):
            continue
        new_vals = tail_out[col].to_numpy()
        if col not in target_df.columns:
            target_df[col] = np.nan
        pos = target_df.columns.get_loc(col)
        # Fill-only splice: write only cells the master does not have yet
        # (the appended row, warmup fills, retroactive backfills).  A cell's
        # first value is its best one — it was computed with the deepest
        # available lookback — so written cells are never revised; an
        # overwrite-all splice would decay history (in-tail lookback shrinks
        # on every later recompute) and NaN-band the tail head.
        old_vals = target_df.iloc[n - k :, pos].to_numpy()
        nan_old = pd.isna(old_vals)
        if not nan_old.all():
            new_vals = np.where(nan_old, new_vals, old_vals)
        target_df.iloc[n - k :, pos] = new_vals
