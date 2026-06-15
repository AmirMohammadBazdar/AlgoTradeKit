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
)
from ._lot import calculate_mt5_lot, get_mt5_pip_info, round_lot
from ._position import (
    CLOSE_REASON_EOD,
    CLOSE_REASON_FC,
    CLOSE_REASON_RF,
    CLOSE_REASON_SL,
    CLOSE_REASON_TP,
    ClosedTrade,
    _InternalPosition,
)
from ._report import SimulateReport, build_report


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


def _advance_multi_rr(pos: _InternalPosition) -> None:
    """
    Mutate *pos* to advance to the next TP level after the current one is hit.

    SL is moved to:
    * entry price (break-even) after the first TP level
    * previous TP level price after subsequent levels

    ``pos.next_tp`` is updated to the next price, or ``None`` when the final
    level was just hit (engine should close the position in that case).
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


def _update_trailing_sl(
    pos: _InternalPosition,
    candle_high: float,
    candle_low: float,
    config: SimulateConfig,
) -> None:
    """
    Update the trailing stop-loss for *pos* if ``sl_mode`` is ``"trailing"``.

    The trailing SL is placed ``trailing_sl_percent`` % below the running
    peak for longs and above the running trough for shorts.
    """
    if config.sl_mode != SL_MODE_TRAILING:
        return

    trail_pct = config.trailing_sl_percent / 100.0

    if pos.direction == "long":
        if candle_high > pos.peak_price:
            pos.peak_price = candle_high
        new_sl = pos.peak_price * (1.0 - trail_pct)
        if new_sl > pos.stop_loss:
            pos.stop_loss = new_sl
    else:  # short
        if candle_low < pos.peak_price:
            pos.peak_price = candle_low
        new_sl = pos.peak_price * (1.0 + trail_pct)
        if new_sl < pos.stop_loss:
            pos.stop_loss = new_sl


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
) -> tuple[float, str] | None:
    """
    Determine whether *pos* should close on this candle.

    Returns
    -------
    tuple[float, str] | None
        ``(exit_price, close_reason)`` if a close is triggered,
        ``None`` otherwise.

    Priority / ordering
    -------------------
    1. Gap opens (candle opens past SL or TP) → fill at ``candle_open``.
    2. TP hit (``pos.next_tp``): last level → close; intermediate → advance SL.
    3. SL hit.
    """
    direction = pos.direction
    is_long = direction == "long"

    # ------------------------------------------------------------------
    # 1. Gap open past SL (unfavourable gap)
    # ------------------------------------------------------------------
    if is_long and candle_open <= pos.stop_loss:
        return candle_open, _sl_reason(pos)
    if not is_long and candle_open >= pos.stop_loss:
        return candle_open, _sl_reason(pos)

    # ------------------------------------------------------------------
    # 2. Gap open past TP (favourable gap — fill at open)
    # ------------------------------------------------------------------
    if pos.next_tp is not None:
        if is_long and candle_open >= pos.next_tp:
            is_last = pos.last_rr_hit + 1 >= len(pos.tp_level_prices)
            if is_last:
                return candle_open, CLOSE_REASON_TP
            # Intermediate TP gapped: advance and continue
            _advance_multi_rr(pos)
            # After advancing, SL may now be hit at new level (checked below)

        elif not is_long and candle_open <= pos.next_tp:
            is_last = pos.last_rr_hit + 1 >= len(pos.tp_level_prices)
            if is_last:
                return candle_open, CLOSE_REASON_TP
            _advance_multi_rr(pos)

    # ------------------------------------------------------------------
    # 3. TP hit within candle body
    # ------------------------------------------------------------------
    if pos.next_tp is not None:
        tp_hit = (is_long and candle_high >= pos.next_tp) or \
                 (not is_long and candle_low <= pos.next_tp)
        sl_hit = (is_long and candle_low <= pos.stop_loss) or \
                 (not is_long and candle_high >= pos.stop_loss)

        if tp_hit:
            is_last = pos.last_rr_hit + 1 >= len(pos.tp_level_prices)

            if sl_hit:
                # Both TP and SL triggered on same candle.
                # Tiebreaker: bullish candle → TP first; bearish → SL first.
                tp_first = candle_close > candle_open

                if not tp_first:
                    return pos.stop_loss, _sl_reason(pos)
                # TP wins:
                if is_last:
                    return pos.next_tp, CLOSE_REASON_TP
                _advance_multi_rr(pos)
                # SL at new level is checked in step 4 below
                return None

            # Only TP hit (no SL conflict)
            if is_last:
                return pos.next_tp, CLOSE_REASON_TP
            # Intermediate multi-RR TP → advance, do NOT close
            _advance_multi_rr(pos)
            return None

    # ------------------------------------------------------------------
    # 4. SL hit within candle body
    # ------------------------------------------------------------------
    sl_hit = (is_long and candle_low <= pos.stop_loss) or \
             (not is_long and candle_high >= pos.stop_loss)

    if sl_hit:
        return pos.stop_loss, _sl_reason(pos)

    return None  # Position stays open


def _make_closed_trade(
    pos: _InternalPosition,
    exit_price: float,
    reason: str,
    close_time: int,
    config: SimulateConfig,
) -> ClosedTrade:
    """
    Build an immutable ``ClosedTrade`` from a live position and exit info.
    """
    direction_factor = 1.0 if pos.direction == "long" else -1.0
    gross_pnl = direction_factor * (exit_price - pos.entry_price) * pos.pnl_per_price_unit
    net_pnl = gross_pnl - pos.open_commission
    pnl_r = net_pnl / pos.risk_amount if pos.risk_amount > 0 else 0.0
    spread_paid = config.spread * pos.pnl_per_price_unit

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
        size=pos.size,
        margin_amount=pos.margin_amount,
        risk_amount=pos.risk_amount,
        gross_pnl=gross_pnl,
        commission=pos.open_commission,
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
    )


def _compute_position_params(
    sig,                   # Signal
    config: SimulateConfig,
    wallet: float,
) -> tuple | None:
    """
    Compute all sizing parameters for a new position.

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
        fixed_l = config.fixed_lot

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
            risk_amount = balance_ref * config.risk_per_trade / 100.0
        else:  # SIZING_FIXED_AMOUNT
            risk_amount = config.fixed_amount

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

        return self._simulate_loop(
            primary_df, signals_by_idx, exits_by_idx, config
        )

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
            # A. Update trailing SL peak prices + excursion tracking
            # -------------------------------------------------------
            for pos in open_positions:
                _update_trailing_sl(pos, c_high, c_low, config)
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
            # -------------------------------------------------------
            newly_closed: list[_InternalPosition] = []
            for pos in open_positions:
                result = _check_close(pos, c_open, c_high, c_low, c_close)
                if result is not None:
                    exit_price, reason = result
                    ct = _make_closed_trade(pos, exit_price, reason, ts, config)
                    wallet += pos.margin_amount + ct.gross_pnl
                    closed_trades.append(ct)
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
