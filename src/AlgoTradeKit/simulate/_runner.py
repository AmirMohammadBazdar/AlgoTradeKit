"""
AlgoTradeKit.simulate._runner
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
High-level helpers for running multiple simulations efficiently.

``run_batch``
    Run one strategy against *N* different configs in parallel.
    Returns one ``SimulateReport`` per config — the fastest way to sweep
    hyperparameters such as risk percentage, TP mode, or leverage.

``run_multi``
    Run several (strategy, data, config) pairs on a **shared wallet**.
    Useful for evaluating how a portfolio of independent strategies / symbols
    performs when they compete for the same capital.

Both functions return results in the same order as their input lists.

Parallelism
-----------
Python's GIL means threads do not provide real concurrency for CPU-bound
simulation loops.  ``ThreadPoolExecutor`` is used here because:

* Process spawning overhead outweighs gains for datasets < ~50 000 candles.
* Pickle constraints (lambdas, strategy instances) are avoided.
* Users with very large datasets can set ``max_workers=1`` to disable.

For heavy workloads (many configs × many candles), the recommendation is to
run in an environment where ``fork``-based multiprocessing works naturally
(Linux / macOS) using Python's ``multiprocessing`` module directly.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING

import pandas as pd

from ..strategy._types import StrategyMode, StrategyResult
from ._config import SimulateConfig
from ._engine import (
    Simulate,
    _apply_partial_close,
    _can_open_position,
    _check_close,
    _check_risk_free,
    _compute_position_params,
    _make_closed_trade,
    _update_trailing_sl,
    _SIZE_EPSILON,
)
from ._position import (
    CLOSE_REASON_EOD,
    CLOSE_REASON_FC,
    ClosedTrade,
    _InternalPosition,
)
from ._report import SimulateReport, build_report

if TYPE_CHECKING:
    from ..strategy._base import BaseStrategy


# ---------------------------------------------------------------------------
# Public: run_batch
# ---------------------------------------------------------------------------

def run_batch(
    strategy: "BaseStrategy",
    data: "dict[str, pd.DataFrame] | pd.DataFrame",
    configs: list[SimulateConfig],
    max_workers: int | None = None,
) -> list[SimulateReport]:
    """
    Run one strategy against multiple configs and return one report per config.

    The strategy's ``run()`` is called **once** (expensive indicator
    computation happens only once), and the resulting ``StrategyResult`` is
    shared across all config simulations.

    Parameters
    ----------
    strategy : BaseStrategy
        Strategy instance to run.
    data : dict[str, pd.DataFrame] | pd.DataFrame
        OHLCV data (same format as ``BaseStrategy.run()``).
    configs : list[SimulateConfig]
        List of configs to test.  Each is run independently with its own
        wallet, so the results are fully comparable.
    max_workers : int | None
        Thread pool size.  ``None`` uses Python's default (min(32, cpu+4)).
        Pass ``1`` to disable threading (useful for debugging).

    Returns
    -------
    list[SimulateReport]
        One report per config, **in the same order** as *configs*.

    Example
    -------
    ::

        from AlgoTradeKit.simulate import SimulateConfig, run_batch

        configs = [
            SimulateConfig(risk_per_trade=0.5, tp_rr=1.5, tp_mode="fixed_rr"),
            SimulateConfig(risk_per_trade=1.0, tp_rr=2.0, tp_mode="fixed_rr"),
            SimulateConfig(risk_per_trade=2.0, tp_rr=3.0, tp_mode="fixed_rr"),
        ]
        reports = run_batch(strategy, data, configs)
        best = max(reports, key=lambda r: r.total_pnl)
        print(f"Best config: {best.config.config_id}  PnL: {best.total_pnl:.2f}")
    """
    if not configs:
        return []

    # Run strategy once (builds indicators + collects signals)
    strategy_result: StrategyResult = strategy.run(data, mode=StrategyMode.BACKTEST)

    if max_workers == 1:
        # Sequential — useful for debugging / profiling
        return [Simulate(cfg).run(strategy_result) for cfg in configs]

    results: list[SimulateReport | None] = [None] * len(configs)

    def _run_one(idx: int, cfg: SimulateConfig) -> tuple[int, SimulateReport]:
        return idx, Simulate(cfg).run(strategy_result)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_run_one, i, cfg): i
            for i, cfg in enumerate(configs)
        }
        for future in as_completed(futures):
            idx, report = future.result()
            results[idx] = report

    return results  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Public: run_multi
# ---------------------------------------------------------------------------

def run_multi(
    pairs: list[tuple["BaseStrategy", "dict[str, pd.DataFrame] | pd.DataFrame", SimulateConfig]],
    initial_balance: float | None = None,
    max_workers: int | None = None,
) -> SimulateReport:
    """
    Simulate multiple strategy/symbol pairs on a **shared wallet**.

    All pairs contribute to and draw from the same pool of capital.
    Position-count limits in each pair's config apply per-pair; the shared
    wallet is the only cross-pair constraint (you cannot open a position
    if the wallet has insufficient funds).

    The initial balance is taken from ``pairs[0][2].initial_balance`` unless
    *initial_balance* is supplied explicitly.

    The returned ``SimulateReport`` represents the combined portfolio:
    * ``balance_history`` reflects the total equity across all open positions.
    * ``closed_trades`` contains trades from all pairs (symbol tag identifies
      the source).
    * All statistics are computed over the full combined set of trades.
    * The ``config`` on the report is from the first pair (informational).

    Parameters
    ----------
    pairs : list[tuple[BaseStrategy, data, SimulateConfig]]
        Each element is ``(strategy, data, config)``.  Strategies are run
        independently to collect signals; then all signals are merged onto a
        shared timeline for simulation.
    initial_balance : float | None
        Override the starting wallet.  Defaults to ``pairs[0][2].initial_balance``.
    max_workers : int | None
        Thread pool size for running strategies in parallel.  ``None`` = default.

    Returns
    -------
    SimulateReport
        Combined portfolio report.

    Example
    -------
    ::

        from AlgoTradeKit.simulate import SimulateConfig, run_multi

        btc_cfg  = SimulateConfig(symbol="btcusdt", risk_per_trade=1.0)
        eth_cfg  = SimulateConfig(symbol="ethusdt", risk_per_trade=0.5)

        report = run_multi([
            (ichimoku_strategy, btc_data, btc_cfg),
            (macd_strategy,     eth_data, eth_cfg),
        ])
        print(f"Portfolio PnL: {report.total_pnl:.2f}")
    """
    if not pairs:
        raise ValueError("run_multi: pairs list is empty.")

    starting_wallet = initial_balance if initial_balance is not None \
        else pairs[0][2].initial_balance

    # ------------------------------------------------------------------
    # 1. Run all strategies in parallel to collect signals
    # ------------------------------------------------------------------
    strategy_results: list[StrategyResult | None] = [None] * len(pairs)

    def _run_strategy(idx: int, strat, data) -> tuple[int, StrategyResult]:
        return idx, strat.run(data, mode=StrategyMode.BACKTEST)

    if max_workers == 1:
        for i, (strat, data, _) in enumerate(pairs):
            strategy_results[i] = strat.run(data, mode=StrategyMode.BACKTEST)
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futs = {
                executor.submit(_run_strategy, i, strat, data): i
                for i, (strat, data, cfg) in enumerate(pairs)
            }
            for future in as_completed(futs):
                idx, sr = future.result()
                strategy_results[idx] = sr

    # ------------------------------------------------------------------
    # 2. Build a merged, time-sorted signal timeline
    # ------------------------------------------------------------------
    # Each entry: (timestamp_ms, candle_idx_in_pair, signal, pair_idx)
    all_signals: list[tuple[int, int, object, int]] = []
    all_exit_signals: list[tuple[int, int, object, int]] = []

    for pair_idx, (sr, (_, _, cfg)) in enumerate(
        zip(strategy_results, pairs)
    ):
        for sig in sr.signals:       # type: ignore[union-attr]
            all_signals.append((sig.timestamp, sig.candle_index, sig, pair_idx))
        for ex in sr.exit_signals:   # type: ignore[union-attr]
            all_exit_signals.append((ex.timestamp, ex.candle_index, ex, pair_idx))

    all_signals.sort(key=lambda x: x[0])
    all_exit_signals.sort(key=lambda x: x[0])

    # ------------------------------------------------------------------
    # 3. Build per-pair primary DataFrames and find the union timestamp set
    # ------------------------------------------------------------------
    pair_dfs: list[pd.DataFrame] = []
    pair_ts_to_idx: list[dict[int, int]] = []

    for _, data, cfg in pairs:
        if isinstance(data, pd.DataFrame):
            df = data
        else:
            df = data[cfg.primary_timeframe]
        pair_dfs.append(df)
        ts_map = {int(row["timestamp"]): i for i, row in df.iterrows()}
        pair_ts_to_idx.append(ts_map)

    # Union of all timestamps, sorted
    all_timestamps: list[int] = sorted(
        set(ts for df in pair_dfs for ts in df["timestamp"].astype(int))
    )

    # ------------------------------------------------------------------
    # 4. Shared-wallet simulation loop over merged timeline
    # ------------------------------------------------------------------
    wallet = starting_wallet
    open_per_pair: list[list[_InternalPosition]] = [[] for _ in pairs]
    closed_trades: list[ClosedTrade] = []
    balance_history: list[dict] = []
    trade_id_seq = 0

    # Index entry signals by (pair_idx, candle_idx_in_pair)
    sigs_by_pair_candle: dict[tuple[int, int], list] = {}
    for ts, candle_idx, sig, pair_idx in all_signals:
        key = (pair_idx, candle_idx)
        sigs_by_pair_candle.setdefault(key, []).append(sig)

    exits_by_pair_candle: dict[tuple[int, int], list] = {}
    for ts, candle_idx, ex, pair_idx in all_exit_signals:
        key = (pair_idx, candle_idx)
        exits_by_pair_candle.setdefault(key, []).append(ex)

    # Pre-index DataFrames by timestamp for O(1) candle lookup
    pair_ts_rows: list[dict[int, dict]] = []
    for df in pair_dfs:
        ts_row: dict[int, dict] = {}
        for row in df.itertuples(index=False):
            ts_row[int(row.timestamp)] = {
                "open":  float(row.open),
                "high":  float(row.high),
                "low":   float(row.low),
                "close": float(row.close),
            }
        pair_ts_rows.append(ts_row)

    # Build per-pair candle → candle_index lookup
    pair_candle_idx: list[dict[int, int]] = []
    for df in pair_dfs:
        ts_to_ci: dict[int, int] = {}
        for ci, row in enumerate(df.itertuples(index=False)):
            ts_to_ci[int(row.timestamp)] = ci
        pair_candle_idx.append(ts_to_ci)

    for ts in all_timestamps:
        all_open_positions = [p for pp in open_per_pair for p in pp]

        # --- Update + check close for each pair ---
        for pair_idx, (cfg, open_positions) in enumerate(
            zip([p[2] for p in pairs], open_per_pair)
        ):
            candle = pair_ts_rows[pair_idx].get(ts)
            if candle is None:
                continue  # This pair has no candle at this timestamp

            c_open  = candle["open"]
            c_high  = candle["high"]
            c_low   = candle["low"]
            c_close = candle["close"]

            # Trailing SL + excursion
            for pos in open_positions:
                _update_trailing_sl(pos, c_high, c_low, cfg)
                pos.update_trailing_peak(c_high if pos.direction == "long" else c_low)
                pos.update_excursion(c_close)

            # Risk-free
            if cfg.risk_free_enabled and cfg.tp_mode != "multi_rr":
                for pos in open_positions:
                    _check_risk_free(pos, c_high, c_low, cfg)

            # SL/TP close (may yield zero, one, or several partial+final events)
            newly_closed = []
            for pos in open_positions:
                events = _check_close(pos, c_open, c_high, c_low, c_close, cfg)
                for exit_price, reason, closed_size in events:
                    ct = _make_closed_trade(pos, exit_price, reason, ts, cfg, closed_size)
                    wallet += ct.margin_amount + ct.gross_pnl
                    closed_trades.append(ct)
                    _apply_partial_close(pos, closed_size)
                if pos.size <= _SIZE_EPSILON:
                    newly_closed.append(pos)
            for pos in newly_closed:
                open_positions.remove(pos)

            # Force-close on ExitSignal
            if cfg.force_close_on_exit_signal and open_positions:
                ci = pair_candle_idx[pair_idx].get(ts)
                if ci is not None:
                    exit_sigs = exits_by_pair_candle.get((pair_idx, ci), [])
                    if exit_sigs:
                        exit_p = exit_sigs[0].exit_price if exit_sigs[0].exit_price else c_close
                        for pos in open_positions[:]:
                            ct = _make_closed_trade(pos, exit_p, CLOSE_REASON_FC, ts, cfg)
                            wallet += pos.margin_amount + ct.gross_pnl
                            closed_trades.append(ct)
                        open_positions.clear()

            # Entry signals for this pair at this timestamp
            ci = pair_candle_idx[pair_idx].get(ts)
            if ci is not None:
                for sig in sigs_by_pair_candle.get((pair_idx, ci), []):
                    all_open_now = [p for pp in open_per_pair for p in pp]
                    if not _can_open_position(sig, open_positions, cfg):
                        continue
                    params = _compute_position_params(sig, cfg, wallet)
                    if params is None:
                        continue
                    (margin_amount, size, pnl_pu, risk_amount,
                     commission, spread_paid, tp_prices, fill_price) = params

                    wallet -= margin_amount + commission
                    display_tp = tp_prices[0] if tp_prices else None
                    pos = _InternalPosition(
                        trade_id=trade_id_seq,
                        symbol=cfg.symbol or sig.metadata.get("symbol", ""),
                        direction=sig.direction,
                        entry_price=fill_price,
                        raw_entry_price=sig.entry_price,
                        stop_loss=sig.stop_loss,
                        take_profit=display_tp,
                        margin_amount=margin_amount,
                        risk_amount=risk_amount,
                        size=size,
                        open_time=ts,
                        open_commission=commission,
                        signal_metadata=dict(sig.metadata),
                        signal_candle_index=sig.candle_index,
                        tp_level_prices=tp_prices,
                    )
                    open_positions.append(pos)
                    trade_id_seq += 1

        # Equity snapshot (all pairs combined)
        all_open_now = [p for pp in open_per_pair for p in pp]
        all_closed_ts = sum(p.margin_amount for p in all_open_now)
        unrealised = sum(
            p.unrealised_pnl(
                pair_ts_rows[  # Use the candle for this position's pair
                    next(
                        (pi for pi, pp in enumerate(open_per_pair) if p in pp),
                        0,
                    )
                ].get(ts, {}).get("close", p.entry_price)
            )
            for p in all_open_now
        )
        equity = wallet + all_closed_ts + unrealised
        balance_history.append({"timestamp": ts, "wallet": wallet, "equity": equity})

    # Close remaining open positions at last timestamp for each pair
    for pair_idx, (cfg, open_positions) in enumerate(
        zip([p[2] for p in pairs], open_per_pair)
    ):
        if not open_positions:
            continue
        last_df = pair_dfs[pair_idx]
        last_row = last_df.iloc[-1]
        last_ts = int(last_row["timestamp"])
        last_close = float(last_row["close"])
        for pos in open_positions:
            ct = _make_closed_trade(pos, last_close, CLOSE_REASON_EOD, last_ts, cfg)
            wallet += pos.margin_amount + ct.gross_pnl
            closed_trades.append(ct)

    # Use first pair's config as the report config (combined portfolio)
    portfolio_config = SimulateConfig(
        initial_balance=starting_wallet,
        symbol="portfolio",
        config_id="multi_pair_portfolio",
        drawdown_threshold=pairs[0][2].drawdown_threshold,
        primary_timeframe=pairs[0][2].primary_timeframe,
    )

    return build_report(closed_trades, [], balance_history, portfolio_config)
