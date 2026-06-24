"""
AlgoTradeKit.simulate._engine
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
``Simulate`` — the backtesting engine that replays a ``StrategyResult``
candle-by-candle and produces a ``SimulateReport``.

Design contract
---------------
* The engine is **pure**: it never mutates the ``StrategyResult`` or the
  underlying DataFrames.
* All time values are UTC milliseconds (matching ``AlgoTradeKit.data``).
* PnL is computed with a single unified formula that works for both
  exchange and MetaTrader instruments::

      pnl = direction_factor × (exit_price − entry_price) × pnl_per_price_unit

  where ``pnl_per_price_unit = risk_amount / sl_distance`` for risk-based
  sizing, or ``size × pip_value / pip_size`` for fixed-lot MT5 sizing.

Position life-cycle per candle (in order)
------------------------------------------
1. Update trailing-SL peak prices.
2. Activate risk-free (break-even) SL when profit threshold is reached.
3. Check SL / TP close triggers for all open positions.
4. Force-close positions on ``ExitSignal`` (when configured).
5. Open new positions from entry ``Signal``s at this candle.
6. Record equity snapshot to ``balance_history``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..strategy._types import StrategyResult

if TYPE_CHECKING:  # pragma: no cover
    from ..simulate._report import SimulateReport
from ._config import (
    SimulateConfig,
    SL_MODE_TRAILING,
    SIZING_FIXED_LOT,
    SIZING_FIXED_AMOUNT,
    SIZING_RISK_PERCENT,
    COMMISSION_TYPE_PERCENTAGE,
    COMMISSION_TYPE_PER_LOT,
    COMMISSION_TYPE_FIXED,
    TP_MODE_SIGNAL,
    TP_MODE_FIXED_RR,
    TP_MODE_MULTI_RR,
    TP_MODE_NONE,
    REPORT_MODE_NONE,
    REPORT_MODE_WEBPAGE,
    REPORT_MODE_SAVE,
    REPORT_MODE_BOTH,
)
from ._lot import calculate_mt5_lot, get_mt5_pip_info, round_lot
from ._position import (
    CLOSE_REASON_EOD,
    CLOSE_REASON_FC,
    CLOSE_REASON_RF,
    CLOSE_REASON_SL,
    CLOSE_REASON_TP,
    CLOSE_REASON_TP_PARTIAL,
    ClosedTrade,
    _InternalPosition,
)
from ._report import SimulateReport, build_report

# Tolerance below which a position's remaining size is treated as fully
# closed (avoids float dust keeping a ~0-size position open forever).
_SIZE_EPSILON = 1e-9

# ---------------------------------------------------------------------------
# Keep-alive: prevent the process from exiting while browser tabs are open
# ---------------------------------------------------------------------------

_keep_alive_registered = False   # register only once per process


def _register_keep_alive() -> None:
    """
    Register a one-time ``atexit`` handler that keeps the Python process
    alive (blocking on ``while True: sleep(1)``) until the user presses
    Ctrl+C.

    **Why this is necessary**

    Both ``ChartServer`` and ``ReportServer`` run in *daemon* threads.
    Python's process exits as soon as the main thread finishes, and daemon
    threads are killed at that point — before the browser has had any
    meaningful time to connect, receive data, or let the user interact.
    This caused the chart/report to show "disconnected" immediately after
    the script finished.

    **Why atexit (not blocking inside Simulate.run)**

    Blocking inside ``Simulate.run()`` would prevent the caller from doing
    anything after the simulation — printing stats, saving CSV history, or
    running a second backtest.  An ``atexit`` handler runs *after* all user
    code has finished but *before* Python kills daemon threads, so:

    1. ``Simulate.run()`` returns immediately → caller code runs normally.
    2. When the caller's script finishes, Python queues shutdown.
    3. The atexit handler fires → the daemon server threads are still alive.
    4. The handler blocks → servers stay up, browser tabs remain interactive.
    5. User presses Ctrl+C → handler unblocks → Python kills daemon threads.

    The handler is registered at most once per process invocation, so
    multiple ``Simulate.run()`` calls (e.g., a parameter sweep) register
    only a single blocking handler.
    """
    global _keep_alive_registered
    if _keep_alive_registered:
        return
    _keep_alive_registered = True

    import atexit
    import time as _t

    def _block() -> None:
        print(
            "\n  \033[90m[AlgoTradeKit] Browser UI open — "
            "press Ctrl+C to exit\033[0m",
            flush=True,
        )
        try:
            while True:
                _t.sleep(1)
        except KeyboardInterrupt:
            print("\n  \033[90m[AlgoTradeKit] Shutting down…\033[0m", flush=True)

    atexit.register(_block)


# ---------------------------------------------------------------------------
# Module-level private helpers
# ---------------------------------------------------------------------------

def _sl_reason(pos: _InternalPosition) -> str:
    """
    Map position state to the correct SL close-reason string.

    * ``"rf"`` — risk-free SL hit: either a multi-RR TP level advanced the SL
      (``last_rr_hit > 0``) or the standalone risk-free break-even was
      activated (``risk_free_triggered``).
    * ``"sl"`` — plain stop-loss hit with no prior TP advancement.
    """
    return CLOSE_REASON_RF if (pos.last_rr_hit > 0 or pos.risk_free_triggered) else CLOSE_REASON_SL


def _build_tp_level_prices(
    signal_tp: float | None,
    fill_price: float,
    sl_distance: float,
    direction_factor: float,
    config: SimulateConfig,
) -> list[float]:
    """
    Compute the ordered list of TP price targets from config.tp_mode.

    Returns
    -------
    list[float]
        Empty list means no TP (position closes only on SL / force-close).
        One element → close at that price.
        Multiple elements → multi-RR: SL moves after each hit; final level
        closes the position.
    """
    if config.tp_mode == TP_MODE_NONE:
        return []

    if config.tp_mode == TP_MODE_SIGNAL:
        return [signal_tp] if signal_tp is not None else []

    if config.tp_mode == TP_MODE_FIXED_RR:
        return [fill_price + direction_factor * config.tp_rr * sl_distance]

    if config.tp_mode == TP_MODE_MULTI_RR:
        return [
            fill_price + direction_factor * lvl * sl_distance
            for lvl in config.tp_levels
        ]

    return []


def _advance_multi_rr(pos: _InternalPosition, timestamp: int) -> None:
    """
    Mutate *pos* to advance to the next TP level after the current one is hit.

    SL is moved to:
    * entry price (break-even) after the first TP level
    * previous TP level price after subsequent levels

    ``pos.next_tp`` is updated to the next price, or ``None`` when the final
    level was just hit (engine should close the position in that case).

    Also records the new SL state in ``pos.sl_history`` (v0.7.4).
    """
    # Move SL
    if pos.last_rr_hit == 0:
        pos.stop_loss = pos.entry_price      # break-even
    else:
        pos.stop_loss = pos.tp_level_prices[pos.last_rr_hit - 1]

    pos.last_rr_hit += 1

    # Set next TP target
    if pos.last_rr_hit < len(pos.tp_level_prices):
        pos.next_tp = pos.tp_level_prices[pos.last_rr_hit]
    else:
        pos.next_tp = None  # all levels consumed

    # Record SL state change (v0.7.4)
    pos.sl_history.append({
        "time": timestamp,
        "sl": pos.stop_loss,
        "next_tp": pos.next_tp,
    })


def _update_trailing_sl(
    pos: _InternalPosition,
    config: SimulateConfig,
    timestamp: int,
) -> None:
    """
    Recompute and apply the trailing stop-loss from *pos.peak_price*.

    Called AFTER ``pos.update_trailing_peak()`` has already updated the
    running peak for the current candle, and AFTER the gap-open close check
    has passed (so the SL advance cannot falsely trigger a "gap" close on a
    candle that opened between the old and new SL levels).

    Records a SL-state change in ``pos.sl_history`` whenever the SL
    actually advances (v0.7.4).
    """
    if config.sl_mode != SL_MODE_TRAILING:
        return

    trail_pct = config.trailing_sl_percent / 100.0

    if pos.direction == "long":
        new_sl = pos.peak_price * (1.0 - trail_pct)
        if new_sl > pos.stop_loss:
            pos.stop_loss = new_sl
            pos.sl_history.append({
                "time": timestamp,
                "sl": new_sl,
                "next_tp": pos.next_tp,
            })
    else:  # short
        new_sl = pos.peak_price * (1.0 + trail_pct)
        if new_sl < pos.stop_loss:
            pos.stop_loss = new_sl
            pos.sl_history.append({
                "time": timestamp,
                "sl": new_sl,
                "next_tp": pos.next_tp,
            })


def _check_risk_free(
    pos: _InternalPosition,
    candle_high: float,
    candle_low: float,
    config: SimulateConfig,
) -> None:
    """
    Activate break-even SL when profit >= ``risk_free_at_rr × sl_distance``.

    Has no effect when the position's SL has already been moved (e.g. by
    multi-RR TP advancement), or when ``risk_free_triggered`` is already set.
    Called only when ``config.risk_free_enabled`` is True.
    """
    if pos.risk_free_triggered:
        return

    threshold_move = config.risk_free_at_rr * pos.sl_distance
    direction_factor = 1.0 if pos.direction == "long" else -1.0
    favourable_extreme = candle_high if pos.direction == "long" else candle_low

    profit_move = direction_factor * (favourable_extreme - pos.entry_price)
    if profit_move >= threshold_move:
        pos.stop_loss = pos.entry_price
        pos.risk_free_triggered = True


def _check_close(
    pos: _InternalPosition,
    candle_open: float,
    candle_high: float,
    candle_low: float,
    candle_close: float,
    config: SimulateConfig,
    timestamp: int,
) -> list[tuple[float, str, float]]:
    """
    Determine whether *pos* should close (fully or partially) on this candle.

    Returns
    -------
    list[tuple[float, str, float]]
        Zero or more ``(exit_price, close_reason, closed_size)`` events.

    Priority / ordering
    -------------------
    1. Gap opens past SL → fill at ``candle_open``.
    2. Trailing SL advance from current candle peak (AFTER gap check, so the
       newly-advanced SL cannot falsely trigger a gap-open close on a candle
       that simply opened between the old and new SL levels).
    3. TP hit (``pos.next_tp``): last level → close; intermediate → advance SL.
    4. SL hit within candle body.
    """
    direction = pos.direction
    is_long = direction == "long"
    events: list[tuple[float, str, float]] = []
    remaining = pos.size

    # ------------------------------------------------------------------
    # 1. Gap open past SL (uses SL as it stood at END of previous candle)
    # ------------------------------------------------------------------
    if is_long and candle_open <= pos.stop_loss:
        events.append((candle_open, _sl_reason(pos), remaining))
        return events
    if not is_long and candle_open >= pos.stop_loss:
        events.append((candle_open, _sl_reason(pos), remaining))
        return events

    # ------------------------------------------------------------------
    # 2. Advance trailing SL from this candle's peak
    #    (peak already updated in step A of the main loop)
    # ------------------------------------------------------------------
    _update_trailing_sl(pos, config, timestamp)

    # ------------------------------------------------------------------
    # 3. Gap open past TP (favourable gap — fill at open)
    # ------------------------------------------------------------------
    if pos.next_tp is not None:
        if is_long and candle_open >= pos.next_tp:
            fully_closed, remaining = _handle_tp_level_hit(
                pos, candle_open, config, events, remaining, timestamp
            )
            if fully_closed:
                return events
        elif not is_long and candle_open <= pos.next_tp:
            fully_closed, remaining = _handle_tp_level_hit(
                pos, candle_open, config, events, remaining, timestamp
            )
            if fully_closed:
                return events

    # ------------------------------------------------------------------
    # 4. TP hit within candle body
    # ------------------------------------------------------------------
    if pos.next_tp is not None:
        tp_hit = (is_long and candle_high >= pos.next_tp) or \
                 (not is_long and candle_low <= pos.next_tp)
        sl_hit = (is_long and candle_low <= pos.stop_loss) or \
                 (not is_long and candle_high >= pos.stop_loss)

        if tp_hit:
            if sl_hit:
                tp_first = candle_close > candle_open
                if not tp_first:
                    events.append((pos.stop_loss, _sl_reason(pos), remaining))
                    return events
                _handle_tp_level_hit(pos, pos.next_tp, config, events, remaining, timestamp)
                return events
            _handle_tp_level_hit(pos, pos.next_tp, config, events, remaining, timestamp)
            return events

    # ------------------------------------------------------------------
    # 5. SL hit within candle body
    # ------------------------------------------------------------------
    sl_hit = (is_long and candle_low <= pos.stop_loss) or \
             (not is_long and candle_high >= pos.stop_loss)

    if sl_hit:
        events.append((pos.stop_loss, _sl_reason(pos), remaining))
        return events

    return events


def _make_closed_trade(
    pos: _InternalPosition,
    exit_price: float,
    reason: str,
    close_time: int,
    config: SimulateConfig,
    closed_size: float | None = None,
) -> ClosedTrade:
    """
    Build an immutable ``ClosedTrade`` from a live position and exit info.

    Parameters
    ----------
    closed_size : float | None
        Absolute size realised by *this* close event.  ``None`` (default)
        closes ``pos``'s entire current remaining size — the original,
        pre-v0.7.3 behaviour, byte-for-byte (the fraction below evaluates
        to ``1.0`` and every field reduces to the old formula exactly).

        A value less than ``pos.size`` builds a record for a *partial*
        close: every dollar figure (risk, margin, gross PnL, commission)
        is scaled by ``closed_size / pos.size``.  The caller is responsible
        for shrinking ``pos`` itself afterwards via ``_apply_partial_close``
        — this function only reads from ``pos``, it never mutates it.
    """
    if closed_size is None or closed_size >= pos.size:
        frac = 1.0
        closed_size = pos.size
    elif pos.size > 0:
        frac = closed_size / pos.size
    else:
        frac = 1.0

    direction_factor = 1.0 if pos.direction == "long" else -1.0
    slice_pnl_per_price_unit = pos.pnl_per_price_unit * frac
    gross_pnl = direction_factor * (exit_price - pos.entry_price) * slice_pnl_per_price_unit
    slice_commission = pos.open_commission * frac
    net_pnl = gross_pnl - slice_commission
    slice_risk_amount = pos.risk_amount * frac
    pnl_r = net_pnl / slice_risk_amount if slice_risk_amount > 0 else 0.0
    spread_paid = config.spread * slice_pnl_per_price_unit

    return ClosedTrade(
        trade_id=pos.trade_id,
        symbol=pos.symbol,
        direction=pos.direction,
        open_time=pos.open_time,
        close_time=close_time,
        entry_price=pos.entry_price,
        exit_price=exit_price,
        initial_stop_loss=pos.initial_stop_loss,
        final_stop_loss=pos.stop_loss,
        take_profit=pos.take_profit,
        size=closed_size,
        margin_amount=pos.margin_amount * frac,
        risk_amount=slice_risk_amount,
        gross_pnl=gross_pnl,
        commission=slice_commission,
        net_pnl=net_pnl,
        pnl_r=pnl_r,
        close_reason=reason,
        rr_levels_hit=pos.last_rr_hit,
        max_favourable_excursion=pos._max_favourable,
        max_adverse_excursion=pos._max_adverse,
        leverage=config.leverage,
        spread_paid=spread_paid,
        signal_metadata=dict(pos.signal_metadata),
        signal_candle_index=pos.signal_candle_index,
        # v0.7.4 — snapshot SL trajectory + closing-moment info
        sl_history=tuple(pos.sl_history),
        final_next_tp=pos.next_tp,
        peak_price=pos.peak_price,
    )


def _apply_partial_close(pos: _InternalPosition, closed_size: float) -> None:
    """
    Shrink *pos* in place after realising *closed_size* units of profit/loss.

    Scales ``size``, ``risk_amount``, ``margin_amount``, ``pnl_per_price_unit``,
    and the not-yet-charged ``open_commission`` all by the same remaining
    fraction, so:

    * the position continues to behave as a smaller version of itself for
      every subsequent candle, and
    * commission/risk recorded across every ``ClosedTrade`` that shares this
      ``trade_id`` sums back to the original totals (nothing is charged
      twice, nothing is lost).
    """
    if pos.size <= 0:
        return
    frac = min(closed_size / pos.size, 1.0)
    remaining_frac = max(1.0 - frac, 0.0)
    pos.size *= remaining_frac
    pos.risk_amount *= remaining_frac
    pos.margin_amount *= remaining_frac
    pos.pnl_per_price_unit *= remaining_frac
    pos.open_commission *= remaining_frac


def _handle_tp_level_hit(
    pos: _InternalPosition,
    price: float,
    config: SimulateConfig,
    events: list[tuple[float, str, float]],
    remaining: float,
    timestamp: int,
) -> tuple[bool, float]:
    """
    Process one multi-RR TP level being reached at *price*.

    Always advances SL / ``next_tp`` / ``last_rr_hit`` bookkeeping via
    ``_advance_multi_rr`` (this part is independent of size and must stay
    live for both this candle's later checks and every future candle).

    Whether anything actually *closes* here depends on
    ``config.tp_level_close_fractions``:

    * ``None`` (default) — reproduces the pre-v0.7.3 behaviour exactly:
      intermediate levels close nothing (SL only advances), the final
      level closes 100 % of whatever remains.
    * configured — closes exactly ``tp_level_close_fractions[level]`` of
      the position's *original* size at every level, including the last
      (no more automatic "close everything" special-case for the final
      level — any unspecified remainder stays open, trailing at the last
      level's price, until it is eventually stopped out, force-closed,
      or hits end-of-data).

    Note this function does **not** mutate ``pos.size``/``risk_amount``/
    ``margin_amount`` — it only threads *remaining* (this candle's running
    size total, local to one ``_check_close`` call) so that a gap spanning
    more than one level still computes each event's ``closed_size`` against
    the correct just-reduced amount. The caller applies the real
    ``_apply_partial_close`` to ``pos`` once per returned event, in order,
    right after building that event's ``ClosedTrade``.

    Returns
    -------
    tuple[bool, float]
        ``(fully_closed, new_remaining)`` — ``fully_closed`` is ``True``
        when *new_remaining* is ~0 (caller should stop and return).
    """
    level_idx = pos.last_rr_hit
    is_last = level_idx + 1 >= len(pos.tp_level_prices)

    if config.tp_level_close_fractions is not None:
        frac = config.tp_level_close_fractions[level_idx]
    else:
        frac = 1.0 if is_last else 0.0

    _advance_multi_rr(pos, timestamp)

    if frac <= 0.0:
        return False, remaining

    closed_size = min(frac * pos.original_size, remaining)
    if closed_size <= 0.0:
        return False, remaining

    new_remaining = remaining - closed_size
    fully_closed = new_remaining <= _SIZE_EPSILON
    reason = CLOSE_REASON_TP if fully_closed else CLOSE_REASON_TP_PARTIAL
    events.append((price, reason, closed_size))
    return fully_closed, new_remaining


def _compute_position_params(
    sig,                   # Signal
    config: SimulateConfig,
    wallet: float,
) -> tuple | None:
    """
    Compute all sizing parameters for a new position.

    ``sig.risk_multiplier`` (default ``1.0``) scales whatever size/risk this
    signal would otherwise receive from *config* — see ``Signal.risk_multiplier``.

    Returns
    -------
    tuple | None
        ``(margin_amount, size, pnl_per_price_unit, risk_amount,
           commission, spread_paid, tp_level_prices, fill_price)``
        ``None`` when the position cannot be opened (insufficient funds,
        invalid SL distance, or impossible leverage ratio).
    """
    direction = sig.direction
    direction_factor = 1.0 if direction == "long" else -1.0

    # Apply spread to get actual fill price
    fill_price = sig.entry_price + (config.spread if direction == "long" else -config.spread)

    # SL distance from fill price (use signal's SL unchanged)
    sl_distance = abs(fill_price - sig.stop_loss)
    if sl_distance <= 0:
        return None

    is_mt5 = config.is_metatrader()

    # ------------------------------------------------------------------
    # Determine risk_amount ($ at risk) — not needed for fixed_lot yet
    # ------------------------------------------------------------------
    risk_amount: float
    size: float
    margin_amount: float
    pnl_per_price_unit: float
    commission: float

    if config.position_sizing == SIZING_FIXED_LOT:
        # ---- Fixed-lot: size is specified directly ----
        fixed_l = config.fixed_lot * sig.risk_multiplier

        if is_mt5:
            pip_size, pip_value = get_mt5_pip_info(config.symbol, fill_price)
            if pip_size <= 0 or pip_value <= 0:
                return None
            pnl_per_price_unit = fixed_l * pip_value / pip_size
            risk_amount = pnl_per_price_unit * sl_distance
            size = fixed_l
            margin_amount = risk_amount  # simplified MT5 margin model

            if config.commission_type == COMMISSION_TYPE_PER_LOT:
                commission = fixed_l * config.commission
            elif config.commission_type == COMMISSION_TYPE_FIXED:
                commission = config.commission
            else:  # percentage: approximate on risk amount
                commission = risk_amount * config.commission * 2.0

        else:
            # Exchange: fixed_lot = base-asset units to CONTROL
            size = fixed_l
            pnl_per_price_unit = size   # $1 price move → $size gain/loss
            risk_amount = size * sl_distance
            margin_amount = size * fill_price / config.leverage

            if config.commission_type == COMMISSION_TYPE_PERCENTAGE:
                commission = (size * fill_price) * config.commission * 2.0
            elif config.commission_type == COMMISSION_TYPE_FIXED:
                commission = config.commission
            else:  # per_lot: treat 1 unit as 1 lot
                commission = size * config.commission

    else:
        # ---- Risk-based sizing: compute risk_amount first ----
        if config.position_sizing == SIZING_RISK_PERCENT:
            balance_ref = wallet if config.compound else config.initial_balance
            risk_amount = balance_ref * config.risk_per_trade / 100.0 * sig.risk_multiplier
        else:  # SIZING_FIXED_AMOUNT
            risk_amount = config.fixed_amount * sig.risk_multiplier

        if risk_amount <= 0:
            return None

        if is_mt5:
            lots = calculate_mt5_lot(config.symbol, risk_amount, sl_distance, fill_price)
            lots = round_lot(lots)
            if lots <= 0:
                return None
            size = lots
            margin_amount = risk_amount   # simplified MT5 margin
            pnl_per_price_unit = risk_amount / sl_distance

            if config.commission_type == COMMISSION_TYPE_PER_LOT:
                commission = lots * config.commission
            elif config.commission_type == COMMISSION_TYPE_FIXED:
                commission = config.commission
            else:  # percentage: approximate on risk amount notional
                commission = risk_amount * config.commission * 2.0

        else:
            # Exchange with leverage
            sl_percent = sl_distance / fill_price
            effective_risk_ratio = sl_percent * config.leverage
            if effective_risk_ratio >= 1.0:
                # SL would wipe out more than 100 % of margin — reject
                return None

            size = risk_amount / sl_distance          # base-asset units controlled
            margin_amount = size * fill_price / config.leverage
            pnl_per_price_unit = risk_amount / sl_distance   # == size

            if config.commission_type == COMMISSION_TYPE_PERCENTAGE:
                commission = (size * fill_price) * config.commission * 2.0
            elif config.commission_type == COMMISSION_TYPE_FIXED:
                commission = config.commission
            else:  # per_lot: not standard on exchange
                commission = size * config.commission

    # Wallet must cover margin + commission
    if wallet < margin_amount + commission:
        return None

    spread_paid = config.spread * pnl_per_price_unit

    tp_prices = _build_tp_level_prices(
        sig.take_profit, fill_price, sl_distance, direction_factor, config
    )

    return (
        margin_amount, size, pnl_per_price_unit, risk_amount,
        commission, spread_paid, tp_prices, fill_price,
    )


def _can_open_position(
    sig,
    open_positions: list[_InternalPosition],
    config: SimulateConfig,
) -> bool:
    """Check position-count limits before committing to a new entry."""
    if len(open_positions) >= config.max_positions:
        return False

    longs = sum(1 for p in open_positions if p.direction == "long")
    shorts = sum(1 for p in open_positions if p.direction == "short")

    if sig.direction == "long" and longs >= config.max_long_positions:
        return False
    if sig.direction == "short" and shorts >= config.max_short_positions:
        return False

    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class Simulate:
    """
    Backtesting engine for a single strategy/config pair.

    Parameters
    ----------
    config : SimulateConfig
        Full simulation configuration (costs, sizing, TP/SL mode, etc.).

    Usage
    -----
    ::

        config = SimulateConfig(
            initial_balance=10_000,
            leverage=10,
            risk_per_trade=1.0,
            tp_mode="multi_rr",
            tp_levels=[1.0, 2.0, 3.0],
        )
        strategy = MyStrategy()
        result   = strategy.run(data)        # StrategyResult

        sim    = Simulate(config)
        report = sim.run(result)             # SimulateReport

    Or build from a config dict::

        cfg = {"initial_balance": 10_000, "tp_mode": "fixed_rr", "tp_rr": 2.0}
        report = Simulate(SimulateConfig(**cfg)).run(result)
    """

    def __init__(self, config: SimulateConfig) -> None:
        self.config = config

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(self, strategy_result: StrategyResult) -> SimulateReport:
        """
        Replay *strategy_result* candle-by-candle and produce a report.

        Parameters
        ----------
        strategy_result : StrategyResult
            Output of ``BaseStrategy.run(data, mode=StrategyMode.BACKTEST)``.
            Must contain the primary timeframe specified in
            ``self.config.primary_timeframe``.

        Returns
        -------
        SimulateReport
            Fully computed report (no further processing required).

        Raises
        ------
        ValueError
            When ``primary_timeframe`` is not found in ``strategy_result.data``.
        """
        config = self.config
        tf = config.primary_timeframe

        if tf not in strategy_result.data:
            available = sorted(strategy_result.data.keys())
            raise ValueError(
                f"Simulate: primary_timeframe '{tf}' not found in "
                f"strategy_result.data. Available: {available}"
            )

        primary_df = strategy_result.data[tf]

        if primary_df.empty:
            raise ValueError(
                f"Simulate: data['{tf}'] is empty — nothing to simulate."
            )

        # Validate required OHLCV columns
        _required = {"timestamp", "open", "high", "low", "close"}
        missing = _required - set(primary_df.columns)
        if missing:
            raise ValueError(
                f"Simulate: data['{tf}'] is missing columns: {sorted(missing)}"
            )

        # Index signals by candle position for O(1) lookup
        signals_by_idx: dict[int, list] = {}
        for sig in strategy_result.signals:
            signals_by_idx.setdefault(sig.candle_index, []).append(sig)

        exits_by_idx: dict[int, list] = {}
        for ex in strategy_result.exit_signals:
            exits_by_idx.setdefault(ex.candle_index, []).append(ex)

        report = self._simulate_loop(
            primary_df, signals_by_idx, exits_by_idx, config
        )

        # ------------------------------------------------------------------
        # Post-simulation rendering (v0.7.0)
        # Runs only when the caller has enabled show_chart / report_mode.
        # ------------------------------------------------------------------
        self._render_post_simulation(report, strategy_result)

        return report

    # ------------------------------------------------------------------
    # Post-simulation visualisation
    # ------------------------------------------------------------------

    def _render_post_simulation(
        self,
        report: "SimulateReport",
        strategy_result: "StrategyResult",
    ) -> None:
        """
        Open the candle chart and/or the report page after a simulation run.

        Called automatically by ``run()`` when the config has
        ``show_chart=True`` or ``report_mode != "none"``.

        v0.7.4 fix
        ----------
        Both servers (chart and report) run in daemon threads.  Daemon threads
        are killed the moment the Python process's main thread exits, which
        previously caused the browser tabs to show "disconnected" immediately
        after the user's script finished (e.g. after printing stats and saving
        history).

        The fix: register an ``atexit`` handler that blocks with
        ``while True: sleep(1)`` after all user code has run.  ``atexit``
        handlers execute *before* daemon threads are killed, so the servers
        stay up until the user presses Ctrl+C.
        """
        config = self.config
        tf = config.primary_timeframe

        chart = None
        chart_server = None
        report_server = None
        has_browser_ui = False

        # ---- 1. Build candle chart with positions ----
        if config.show_chart:
            chart, chart_server = self._build_simulation_chart(
                strategy_result, report, tf
            )
            has_browser_ui = True

        # ---- 2. Report page ----
        if config.report_mode != REPORT_MODE_NONE:
            report_server = self._render_report(
                report, strategy_result, chart, chart_server
            )
            # report_server is None for save-only mode (no browser tab)
            if report_server is not None:
                has_browser_ui = True

        # ---- 3. Keep process alive while browser tabs are open ----
        if has_browser_ui:
            _register_keep_alive()

    def _build_simulation_chart(
        self,
        strategy_result: "StrategyResult",
        report: "SimulateReport",
        tf: str,
    ):
        """
        Build and show an interactive candle chart with position boxes and
        strategy drawings.  Returns (Chart, ChartServer) tuple.
        """
        import time as _time
        from ..visual import Chart
        from ..visual.indicator_renderer import (
            add_simulation_positions,
            add_strategy_drawings,
        )

        primary_df = strategy_result.data[tf]

        chart = Chart(
            title=f"{self.config.symbol or ''} — Simulation Chart ({tf})",
        )
        chart.set_data(primary_df)

        # Add strategy-defined drawings (support/resistance, signal zones, etc.)
        if strategy_result.drawings:
            add_strategy_drawings(chart, strategy_result)

        # Add position boxes (and dynamic SL/TP lines for multi_rr / trailing)
        add_simulation_positions(chart, report, opacity=0.15, config=self.config)

        # Show chart (non-blocking so report can open simultaneously)
        chart.show(block=False)

        # Give server a moment to start
        _time.sleep(0.4)

        return chart, chart._server

    def _render_report(
        self,
        report: "SimulateReport",
        strategy_result: "StrategyResult",
        chart,
        chart_server,
    ):
        """Build and display/save the report page.

        Returns
        -------
        ReportServer | None
            The running server when a browser tab is opened
            (``report_mode`` is ``"webpage"`` or ``"both"``);
            ``None`` for save-only mode (no browser UI to keep alive).
        """
        import time as _time
        from ..report._builder import build_report_payload
        from ..report._server import ReportServer
        from ..report._display import save_report_html

        config = self.config

        payload = build_report_payload(report)

        # Patch payload with chart connection info
        if chart is not None and chart_server is not None:
            payload["has_chart"] = True
            payload["chart_port"] = getattr(chart_server, "port", None)

        open_browser = config.report_mode in (REPORT_MODE_WEBPAGE, REPORT_MODE_BOTH)
        save_file    = config.report_mode in (REPORT_MODE_SAVE, REPORT_MODE_BOTH)

        # Callback: when user clicks a trade in the report, navigate the chart
        def _on_open_chart(trade_id: int) -> None:
            if chart is None:
                return
            marker = next(
                (m for m in report.trade_markers if m["trade_id"] == trade_id), None
            )
            if marker:
                chart.navigate_to_candle(marker["open_time"])

        report_server = None
        if open_browser:
            report_server = ReportServer(
                title=f"AlgoTradeKit Report — {config.config_id}",
                on_open_chart=_on_open_chart if chart else None,
            )
            report_server.start(open_browser=True)
            _time.sleep(0.3)
            report_server.set_report_data(payload)

        if save_file:
            out_path = save_report_html(report, path=config.report_save_path)
            print(f"[AlgoTradeKit] Report saved → {out_path}")

        return report_server

    # ------------------------------------------------------------------
    # Core loop (extracted for reuse in _runner.py multi-pair engine)
    # ------------------------------------------------------------------

    @staticmethod
    def _simulate_loop(
        primary_df,
        signals_by_idx: dict[int, list],
        exits_by_idx: dict[int, list],
        config: SimulateConfig,
        initial_wallet: float | None = None,
        initial_trade_id: int = 0,
    ) -> SimulateReport:
        """
        Core candle-by-candle simulation loop.

        This static method is exposed so ``_runner.py`` can call it for
        both single-pair and multi-pair simulations.
        """
        wallet: float = initial_wallet if initial_wallet is not None else config.initial_balance
        open_positions: list[_InternalPosition] = []
        closed_trades: list[ClosedTrade] = []
        balance_history: list[dict] = []
        trade_id_seq: int = initial_trade_id

        symbol = config.symbol

        for candle_idx, row in enumerate(primary_df.itertuples(index=False)):
            ts       = int(row.timestamp)
            c_open   = float(row.open)
            c_high   = float(row.high)
            c_low    = float(row.low)
            c_close  = float(row.close)

            # -------------------------------------------------------
            # A. Update trailing peak + excursion tracking
            #    Trailing SL is applied later inside _check_close, AFTER
            #    the gap-open check, so a freshly-advanced SL cannot
            #    falsely trigger a "gap" close on the same candle.
            # -------------------------------------------------------
            for pos in open_positions:
                pos.update_trailing_peak(c_high if pos.direction == "long" else c_low)
                pos.update_excursion(c_close)

            # -------------------------------------------------------
            # B. Activate risk-free SL (if enabled & not multi-RR)
            # -------------------------------------------------------
            if config.risk_free_enabled and config.tp_mode != TP_MODE_MULTI_RR:
                for pos in open_positions:
                    _check_risk_free(pos, c_high, c_low, config)

            # -------------------------------------------------------
            # C. Check SL / TP close for all open positions
            #    (may yield zero, one, or several partial+final events)
            # -------------------------------------------------------
            newly_closed: list[_InternalPosition] = []
            for pos in open_positions:
                events = _check_close(pos, c_open, c_high, c_low, c_close, config, ts)
                for exit_price, reason, closed_size in events:
                    ct = _make_closed_trade(pos, exit_price, reason, ts, config, closed_size)
                    wallet += ct.margin_amount + ct.gross_pnl
                    closed_trades.append(ct)
                    _apply_partial_close(pos, closed_size)
                if pos.size <= _SIZE_EPSILON:
                    newly_closed.append(pos)

            for pos in newly_closed:
                open_positions.remove(pos)

            # -------------------------------------------------------
            # D. Force-close on ExitSignal
            # -------------------------------------------------------
            if config.force_close_on_exit_signal and open_positions:
                exit_sigs = exits_by_idx.get(candle_idx, [])
                if exit_sigs:
                    exit_price = (
                        exit_sigs[0].exit_price
                        if exit_sigs[0].exit_price is not None
                        else c_close
                    )
                    for pos in open_positions[:]:
                        ct = _make_closed_trade(pos, exit_price, CLOSE_REASON_FC, ts, config)
                        wallet += pos.margin_amount + ct.gross_pnl
                        closed_trades.append(ct)
                    open_positions.clear()

            # -------------------------------------------------------
            # E. Open new positions from entry signals at this candle
            # -------------------------------------------------------
            for sig in signals_by_idx.get(candle_idx, []):
                if not _can_open_position(sig, open_positions, config):
                    continue

                params = _compute_position_params(sig, config, wallet)
                if params is None:
                    continue

                (margin_amount, size, pnl_pu, risk_amount,
                 commission, spread_paid, tp_prices, fill_price) = params

                # Deduct margin and commission from wallet
                wallet -= margin_amount + commission

                # Store first TP as the display-level take_profit
                display_tp = tp_prices[0] if tp_prices else None

                pos = _InternalPosition(
                    trade_id=trade_id_seq,
                    symbol=symbol or sig.metadata.get("symbol", ""),
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
                # Initialise SL history with the entry state (v0.7.4)
                pos.sl_history.append({
                    "time": ts,
                    "sl": sig.stop_loss,
                    "next_tp": tp_prices[0] if tp_prices else None,
                })
                open_positions.append(pos)
                trade_id_seq += 1

            # -------------------------------------------------------
            # F. Record equity snapshot
            # -------------------------------------------------------
            unrealised = sum(p.unrealised_pnl(c_close) for p in open_positions)
            equity = (
                wallet
                + sum(p.margin_amount for p in open_positions)
                + unrealised
            )
            balance_history.append({
                "timestamp": ts,
                "wallet": wallet,
                "equity": equity,
            })

        # -----------------------------------------------------------
        # G. Close any remaining open positions at final candle close
        # -----------------------------------------------------------
        if not primary_df.empty:
            last = primary_df.iloc[-1]
            last_ts = int(last["timestamp"])
            last_close = float(last["close"])

            for pos in open_positions:
                ct = _make_closed_trade(pos, last_close, CLOSE_REASON_EOD, last_ts, config)
                wallet += pos.margin_amount + ct.gross_pnl
                closed_trades.append(ct)

        # -----------------------------------------------------------
        # H. Build report (all computation lives in _report.py)
        # -----------------------------------------------------------
        return build_report(closed_trades, [], balance_history, config)
