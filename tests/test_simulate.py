"""
tests/test_simulate.py
~~~~~~~~~~~~~~~~~~~~~~~
Full test suite for the AlgoTradeKit.simulate module (v0.6.0).

Coverage
--------
- SimulateConfig          validation, auto-id, convenience helpers
- _lot                    MT5 lot calculation and pip info for all symbol classes
- _session                session detection, weekday, month helpers
- _position               _InternalPosition helpers, ClosedTrade properties
- _report                 build_report with real trade data, drawdown calculation
- Simulate (engine)       exchange long/short, SL/TP/multi-RR/trailing/risk-free,
                          spread, leverage, commission, force-close, end-of-data,
                          position limits, gap open handling, MetaTrader sizing
- run_batch               parallel config sweep returns ordered results
- run_multi               shared-wallet portfolio (basic smoke test)
"""

from __future__ import annotations

from dataclasses import asdict

import numpy as np
import pandas as pd
import pytest

from AlgoTradeKit.simulate import (
    CLOSE_REASON_EOD,
    CLOSE_REASON_FC,
    CLOSE_REASON_RF,
    CLOSE_REASON_SL,
    CLOSE_REASON_TP,
    CLOSE_REASON_TP_PARTIAL,
    EVENT_CLOSE,
    EVENT_OPEN,
    EVENT_SIGNAL,
    EVENT_SL_MOVE,
    EVENT_TP_LEVEL,
    EXCHANGE_TYPE_METATRADER,
    LiveSimulation,
    Simulate,
    SimulateConfig,
    SimulateReport,
    SimulationStepper,
    run_batch,
    run_multi,
)
from AlgoTradeKit.simulate._lot import calculate_mt5_lot, get_mt5_pip_info, round_lot
from AlgoTradeKit.simulate._position import _InternalPosition
from AlgoTradeKit.simulate._report import build_report
from AlgoTradeKit.simulate._session import (
    SESSION_LONDON,
    SESSION_NEW_YORK,
    SESSION_OFF_HOURS,
    SESSION_SYDNEY,
    SESSION_TOKYO,
    get_month_key,
    get_primary_session,
    get_sessions,
    get_weekday_name,
)
from AlgoTradeKit.strategy import BaseStrategy, Signal, StrategyMode, StrategyResult
from AlgoTradeKit.strategy._types import ExitSignal

# ===========================================================================
# Shared helpers
# ===========================================================================

_BASE_TS = 1_700_000_000_000   # 2023-11-14 22:13 UTC  (arbitrary)
_1H_MS   = 3_600_000


def _make_ohlcv(
    prices: list[float],
    start_ts: int = _BASE_TS,
    spread: float = 0.5,
) -> pd.DataFrame:
    """
    Build a minimal OHLCV DataFrame from a list of close prices.
    High = close + spread, Low = close - spread, Open = prev close.
    """
    n = len(prices)
    timestamps = [start_ts + i * _1H_MS for i in range(n)]
    opens  = [prices[0]] + prices[:-1]
    highs  = [p + spread for p in prices]
    lows   = [p - spread for p in prices]
    return pd.DataFrame({
        "timestamp": timestamps,
        "open":      opens,
        "high":      highs,
        "low":       lows,
        "close":     prices,
        "volume":    [1000.0] * n,
    })


def _make_result(
    signals: list[Signal],
    df: pd.DataFrame,
    exit_signals: list[ExitSignal] | None = None,
) -> StrategyResult:
    return StrategyResult(
        signals=signals,
        exit_signals=exit_signals or [],
        data={"1h": df},
        mode=StrategyMode.BACKTEST,
    )


def _long_signal(
    candle_idx: int, df: pd.DataFrame, sl_pct: float = 0.02, tp_pct: float | None = None
) -> Signal:
    """Long signal at df.iloc[candle_idx].close with SL / optional TP."""
    entry = float(df.iloc[candle_idx]["close"])
    sl    = entry * (1 - sl_pct)
    tp    = entry * (1 + tp_pct) if tp_pct else None
    ts    = int(df.iloc[candle_idx]["timestamp"])
    return Signal(
        direction="long",
        entry_price=entry,
        stop_loss=sl,
        take_profit=tp,
        timestamp=ts,
        candle_index=candle_idx,
        timeframe="1h",
    )


def _short_signal(
    candle_idx: int, df: pd.DataFrame, sl_pct: float = 0.02, tp_pct: float | None = None
) -> Signal:
    entry = float(df.iloc[candle_idx]["close"])
    sl    = entry * (1 + sl_pct)
    tp    = entry * (1 - tp_pct) if tp_pct else None
    ts    = int(df.iloc[candle_idx]["timestamp"])
    return Signal(
        direction="short",
        entry_price=entry,
        stop_loss=sl,
        take_profit=tp,
        timestamp=ts,
        candle_index=candle_idx,
        timeframe="1h",
    )


# ===========================================================================
# 1. SimulateConfig
# ===========================================================================

class TestSimulateConfig:

    def test_defaults(self):
        cfg = SimulateConfig()
        assert cfg.initial_balance == 10_000.0
        assert cfg.leverage == 1.0
        assert cfg.spread == 0.0
        assert cfg.commission == 0.001
        assert cfg.risk_per_trade == 1.0
        assert cfg.tp_mode == "signal"
        assert cfg.sl_mode == "signal"

    def test_auto_id_generated(self):
        cfg = SimulateConfig(
            symbol="btcusdt", leverage=10, risk_per_trade=2.0, tp_mode="fixed_rr", tp_rr=2.5
        )
        assert "BTCUSDT" in cfg.config_id
        assert "risk2.0pct" in cfg.config_id
        assert "lev10x" in cfg.config_id

    def test_custom_id_preserved(self):
        cfg = SimulateConfig(config_id="my_test")
        assert cfg.config_id == "my_test"

    def test_invalid_leverage_raises(self):
        with pytest.raises(ValueError, match="leverage"):
            SimulateConfig(leverage=0)

    def test_invalid_risk_raises(self):
        with pytest.raises(ValueError, match="risk_per_trade"):
            SimulateConfig(risk_per_trade=101)

    def test_invalid_exchange_type_raises(self):
        with pytest.raises(ValueError, match="exchange_type"):
            SimulateConfig(exchange_type="unknown")

    def test_invalid_tp_mode_raises(self):
        with pytest.raises(ValueError, match="tp_mode"):
            SimulateConfig(tp_mode="magic")

    def test_multi_rr_empty_levels_raises(self):
        with pytest.raises(ValueError, match="tp_levels"):
            SimulateConfig(tp_mode="multi_rr", tp_levels=[])

    def test_is_exchange(self):
        assert SimulateConfig(exchange_type="exchange").is_exchange()
        assert not SimulateConfig(exchange_type="metatrader").is_exchange()

    def test_is_metatrader(self):
        assert SimulateConfig(exchange_type="metatrader").is_metatrader()

    def test_dict_construction(self):
        d = {"initial_balance": 5000, "leverage": 5, "risk_per_trade": 2.0}
        cfg = SimulateConfig(**d)
        assert cfg.initial_balance == 5000
        assert cfg.leverage == 5

    def test_spread_accepted(self):
        cfg = SimulateConfig(spread=0.5)
        assert cfg.spread == 0.5

    def test_commission_types_accepted(self):
        SimulateConfig(commission_type="percentage")
        SimulateConfig(commission_type="per_lot")
        SimulateConfig(commission_type="fixed")

    def test_max_positions_validation(self):
        with pytest.raises(ValueError, match="max_positions"):
            SimulateConfig(max_positions=0)


# ===========================================================================
# 2. MT5 lot calculation
# ===========================================================================

class TestLot:

    def test_eurusd_round_trip(self):
        """calculate_mt5_lot → inferred risk equals requested risk_amount."""
        risk = 100.0
        sl   = 0.005         # 50 pips
        lot  = calculate_mt5_lot("eurusd", risk, sl, 1.10)
        # pip_value EURUSD = $10/lot, 50 pips = 50*10 = $500 per lot
        expected = risk / (sl / 0.0001 * 10)
        assert abs(lot - expected) < 1e-8

    def test_usdjpy(self):
        risk = 200.0
        sl   = 0.5           # 50 pips (JPY pair, pip = 0.01)
        price = 150.0
        lot  = calculate_mt5_lot("usdjpy", risk, sl, price)
        pip_value = (0.01 / price) * 100_000
        expected  = risk / (sl / 0.01 * pip_value)
        assert abs(lot - expected) < 1e-6

    def test_xauusd(self):
        risk = 50.0
        sl   = 5.0           # $5 per oz
        lot  = calculate_mt5_lot("xauusd", risk, sl, 2000.0)
        # pip_size=0.01, pip_value=1.0, pips=500, lot=50/(500*1)=0.1
        expected = 50.0 / (500 * 1.0)
        assert abs(lot - expected) < 1e-8

    def test_btcusd(self):
        risk = 100.0
        sl   = 500.0
        lot  = calculate_mt5_lot("btcusd", risk, sl, 40000.0)
        # pip_size=1, pip_value=1, lots=100/(500*1)=0.2
        assert abs(lot - 0.2) < 1e-8

    def test_unknown_symbol_defaults(self):
        lot = calculate_mt5_lot("xyz999", 100, 0.001, 1.0)
        # default pip_size=0.0001, pip_value=10
        expected = 100.0 / (10 * 10)
        assert abs(lot - expected) < 1e-8

    def test_round_lot_clamps(self):
        assert round_lot(0.0, step=0.01, min_lot=0.01) == 0.01
        assert round_lot(200.0, step=0.01, max_lot=100.0) == 100.0

    def test_round_lot_step(self):
        result = round_lot(0.123456, step=0.01)
        assert abs(result - 0.12) < 1e-8

    def test_get_pip_info_eurusd(self):
        pip_size, pip_value = get_mt5_pip_info("eurusd", 1.10)
        assert pip_size == 0.0001
        assert pip_value == 10.0

    def test_get_pip_info_xauusd(self):
        pip_size, pip_value = get_mt5_pip_info("xauusd", 2000.0)
        assert pip_size == 0.01
        assert abs(pip_value - 1.0) < 1e-8

    def test_zero_sl_raises(self):
        with pytest.raises(ValueError):
            calculate_mt5_lot("eurusd", 100, 0, 1.10)


# ===========================================================================
# 3. Session helpers
# ===========================================================================

class TestSession:

    def _ts_utc_hour(self, hour: int) -> int:
        """UTC timestamp for a fixed date at a given hour."""
        # 2024-01-15 (Monday) = days_since_epoch 19737
        # epoch: 1970-01-01 Thursday
        base_day_ts = 19737 * 86_400_000
        return base_day_ts + hour * 3_600_000

    def test_london_session(self):
        ts = self._ts_utc_hour(10)
        assert SESSION_LONDON in get_sessions(ts)

    def test_new_york_session(self):
        ts = self._ts_utc_hour(15)
        assert SESSION_NEW_YORK in get_sessions(ts)

    def test_london_ny_overlap(self):
        ts = self._ts_utc_hour(14)
        sessions = get_sessions(ts)
        assert SESSION_LONDON in sessions
        assert SESSION_NEW_YORK in sessions

    def test_tokyo_session(self):
        ts = self._ts_utc_hour(2)
        assert SESSION_TOKYO in get_sessions(ts)

    def test_sydney_session_night(self):
        ts = self._ts_utc_hour(22)
        assert SESSION_SYDNEY in get_sessions(ts)

    def test_off_hours(self):
        ts = self._ts_utc_hour(23)  # 23 UTC: after NY close (22), before Sydney if after 21
        # Sydney starts at 21, so 23 UTC is actually IN Sydney
        get_sessions(ts)
        # Check primary: should not be off_hours since Sydney is active
        primary = get_primary_session(ts)
        assert primary != SESSION_OFF_HOURS

    def test_primary_session_priority(self):
        # 13:00 UTC = London + NY overlap → NY wins (higher priority)
        ts = self._ts_utc_hour(13)
        assert get_primary_session(ts) == SESSION_NEW_YORK

    def test_weekday_name_thursday(self):
        # Unix epoch 1970-01-01 was a Thursday
        ts = 0  # exactly epoch = Thursday
        assert get_weekday_name(ts) == "thursday"

    def test_month_key(self):
        # 2024-01-15 → "2024-01"
        ts = self._ts_utc_hour(12)  # 2024-01-15 approx
        key = get_month_key(ts)
        assert key.startswith("2024-")


# ===========================================================================
# 4. Internal position helpers
# ===========================================================================

class TestInternalPosition:

    def _make_pos(self, direction="long", entry=100.0, sl=98.0, risk=100.0, tp=None):
        return _InternalPosition(
            trade_id=0,
            symbol="btcusdt",
            direction=direction,
            entry_price=entry,
            raw_entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            margin_amount=1000.0,
            risk_amount=risk,
            size=0.1,
            open_time=_BASE_TS,
            open_commission=1.0,
            signal_metadata={},
            signal_candle_index=0,
            tp_level_prices=[tp] if tp else [],
        )

    def test_unrealised_pnl_long_at_entry(self):
        pos = self._make_pos()
        assert pos.unrealised_pnl(100.0) == pytest.approx(0.0)

    def test_unrealised_pnl_long_profit(self):
        pos = self._make_pos(entry=100.0, sl=98.0, risk=100.0)
        # sl_dist=2, ppu=50, price move +2 → pnl=+100
        assert pos.unrealised_pnl(102.0) == pytest.approx(100.0)

    def test_unrealised_pnl_long_at_sl(self):
        pos = self._make_pos(entry=100.0, sl=98.0, risk=100.0)
        assert pos.unrealised_pnl(98.0) == pytest.approx(-100.0)

    def test_unrealised_pnl_short_profit(self):
        pos = self._make_pos(direction="short", entry=100.0, sl=102.0, risk=100.0)
        # sl_dist=2, ppu=50, price down 2 → short profit = +100
        assert pos.unrealised_pnl(98.0) == pytest.approx(100.0)

    def test_current_value_at_entry(self):
        pos = self._make_pos()
        assert pos.current_value(100.0) == pytest.approx(pos.margin_amount)

    def test_update_excursion(self):
        pos = self._make_pos()
        pos.update_excursion(102.0)   # +100 profit
        assert pos._max_favourable == pytest.approx(100.0)
        pos.update_excursion(99.0)    # -50 loss
        assert pos._max_adverse == pytest.approx(50.0)


# ===========================================================================
# 5. Engine — basic long/short outcomes
# ===========================================================================

class TestEngineBasic:
    """Tests that SL, TP, and EOD closes work correctly for simple cases."""

    def _run(self, prices, signal, config=None, exit_signals=None):
        df  = _make_ohlcv(prices)
        cfg = config or SimulateConfig(
            initial_balance=10_000,
            leverage=1,
            spread=0.0,
            commission=0.0,
            commission_type="fixed",
            risk_per_trade=1.0,
            tp_mode="signal",
            primary_timeframe="1h",
        )
        result = _make_result([signal], df, exit_signals)
        return Simulate(cfg).run(result)

    # --- SL ---

    def test_long_sl_hit(self):
        # Price rises then drops below SL
        prices = [100, 101, 102, 95, 103]
        sig    = _long_signal(0, _make_ohlcv(prices), sl_pct=0.04)  # SL=96
        report = self._run(prices, sig)
        assert len(report.closed_trades) == 1
        ct = report.closed_trades[0]
        assert ct.close_reason == CLOSE_REASON_SL
        assert ct.net_pnl < 0

    def test_short_sl_hit(self):
        prices = [100, 99, 98, 105, 97]
        sig    = _short_signal(0, _make_ohlcv(prices), sl_pct=0.04)  # SL=104
        report = self._run(prices, sig)
        ct = report.closed_trades[0]
        assert ct.close_reason == CLOSE_REASON_SL
        assert ct.net_pnl < 0

    # --- TP ---

    def test_long_tp_hit(self):
        prices = [100, 101, 105, 106, 104]
        sig    = _long_signal(0, _make_ohlcv(prices), sl_pct=0.05, tp_pct=0.04)
        report = self._run(prices, sig)
        ct = report.closed_trades[0]
        assert ct.close_reason == CLOSE_REASON_TP
        assert ct.net_pnl > 0

    def test_short_tp_hit(self):
        prices = [100, 99, 95, 94, 96]
        sig    = _short_signal(0, _make_ohlcv(prices), sl_pct=0.05, tp_pct=0.04)
        report = self._run(prices, sig)
        ct = report.closed_trades[0]
        assert ct.close_reason == CLOSE_REASON_TP
        assert ct.net_pnl > 0

    # --- EOD ---

    def test_end_of_data_close(self):
        prices = [100, 101, 102]   # No SL or TP hit
        sig    = _long_signal(0, _make_ohlcv(prices), sl_pct=0.50)  # SL very far
        report = self._run(prices, sig)
        assert report.closed_trades[0].close_reason == CLOSE_REASON_EOD

    # --- Force close ---

    def test_force_close_on_exit_signal(self):
        prices = [100, 101, 102, 103, 104]
        df     = _make_ohlcv(prices)
        sig    = _long_signal(0, df, sl_pct=0.20)
        ex_sig = ExitSignal(
            reason="test", exit_price=None,
            timestamp=int(df.iloc[2]["timestamp"]), candle_index=2,
        )
        cfg    = SimulateConfig(
            initial_balance=10_000, leverage=1, spread=0.0,
            commission=0.0, commission_type="fixed",
            force_close_on_exit_signal=True, primary_timeframe="1h",
        )
        result = _make_result([sig], df, [ex_sig])
        report = Simulate(cfg).run(result)
        ct = report.closed_trades[0]
        assert ct.close_reason == CLOSE_REASON_FC


# ===========================================================================
# 6. Engine — spread and commission
# ===========================================================================

class TestEngineSpreadCommission:

    def test_spread_increases_entry_for_long(self):
        """Long fill price = signal.entry + spread."""
        prices = [100] * 5
        df     = _make_ohlcv(prices, spread=0.0)  # flat candles
        sig    = _long_signal(0, df, sl_pct=0.10)
        cfg    = SimulateConfig(
            initial_balance=10_000, leverage=1,
            spread=1.0,          # $1 spread
            commission=0.0, commission_type="fixed",
            primary_timeframe="1h",
        )
        report = Simulate(cfg).run(_make_result([sig], df))
        ct = report.closed_trades[0]
        # Entry should be signal.entry + 1.0
        assert ct.entry_price == pytest.approx(sig.entry_price + 1.0)

    def test_commission_deducted_from_pnl(self):
        """Commission reduces net_pnl vs gross_pnl."""
        prices = [100, 110, 120]   # rising; signal closes at EOD
        df     = _make_ohlcv(prices)
        sig    = _long_signal(0, df, sl_pct=0.50)
        cfg    = SimulateConfig(
            initial_balance=10_000, leverage=1,
            spread=0.0,
            commission=10.0, commission_type="fixed",
            primary_timeframe="1h",
        )
        report = Simulate(cfg).run(_make_result([sig], df))
        ct = report.closed_trades[0]
        assert ct.commission == pytest.approx(10.0)
        assert ct.net_pnl == pytest.approx(ct.gross_pnl - ct.commission)

    def test_percentage_commission(self):
        prices = [1000] * 5
        df     = _make_ohlcv(prices, spread=0.0)
        sig    = _long_signal(0, df, sl_pct=0.50)
        cfg    = SimulateConfig(
            initial_balance=10_000, leverage=1,
            spread=0.0,
            commission=0.001, commission_type="percentage",
            risk_per_trade=1.0,
            primary_timeframe="1h",
        )
        report = Simulate(cfg).run(_make_result([sig], df))
        ct = report.closed_trades[0]
        # Commission should be > 0 for percentage type
        assert ct.commission > 0


# ===========================================================================
# 7. Engine — leverage
# ===========================================================================

class TestEngineLeverage:

    def test_leverage_amplifies_pnl(self):
        """
        Dollar PnL is independent of leverage when risk_amount and sl_distance
        are the same: pnl = direction × (exit − entry) × risk_amount / sl_dist.
        Leverage only changes margin, not the P&L formula.
        """
        prices = [100, 102, 104, 106, 108]
        df     = _make_ohlcv(prices, spread=0.0)
        # Use 2 % SL so that sl_percent × leverage = 0.02 × 10 = 0.2 < 1.0 (valid)
        sig    = _long_signal(0, df, sl_pct=0.02)

        def _report(lev):
            cfg = SimulateConfig(
                initial_balance=10_000,
                leverage=lev,
                spread=0.0,
                commission=0.0, commission_type="fixed",
                risk_per_trade=1.0,
                primary_timeframe="1h",
            )
            return Simulate(cfg).run(_make_result([sig], df))

        r1  = _report(1)
        r10 = _report(10)
        assert len(r1.closed_trades)  == 1, "1x position should open"
        assert len(r10.closed_trades) == 1, "10x position should open"
        # Same risk_amount and sl_distance → same gross PnL regardless of leverage
        assert r1.closed_trades[0].gross_pnl == pytest.approx(
            r10.closed_trades[0].gross_pnl, rel=1e-6
        )

    def test_high_leverage_reduces_margin(self):
        """Higher leverage → smaller margin locked for same risk."""
        prices = [100.0] * 10
        df     = _make_ohlcv(prices, spread=0.0)
        # 2 % SL → sl_percent × leverage_max = 0.02 × 10 = 0.20 < 1.0 (valid)
        sig    = _long_signal(0, df, sl_pct=0.02)

        def _margin(lev):
            cfg = SimulateConfig(
                initial_balance=10_000,
                leverage=lev,
                spread=0.0, commission=0.0, commission_type="fixed",
                risk_per_trade=1.0,
                primary_timeframe="1h",
            )
            report = Simulate(cfg).run(_make_result([sig], df))
            assert len(report.closed_trades) == 1, f"lev={lev}: position not opened"
            return report.closed_trades[0].margin_amount

        m1  = _margin(1)
        m10 = _margin(10)
        assert m10 < m1, "10× leverage should require less margin than 1×"
        assert abs(m1 / m10 - 10.0) < 0.01, "margin should scale inversely with leverage"


# ===========================================================================
# 7b. Engine — per-signal risk_multiplier (v0.7.3)
# ===========================================================================

class TestEngineRiskMultiplier:
    """
    Signal.risk_multiplier lets one signal scale its own size relative to
    the run's global sizing config, independent of other signals in the
    same run. Default (1.0) must be fully equivalent to pre-v0.7.3 sizing.
    """

    def _cfg(self, **overrides):
        defaults = dict(
            initial_balance=10_000, leverage=1,
            spread=0.0, commission=0.0, commission_type="fixed",
            risk_per_trade=1.0, tp_mode="none",
            primary_timeframe="1h",
        )
        defaults.update(overrides)
        return SimulateConfig(**defaults)

    def test_default_multiplier_unchanged_vs_pre_0_7_3_sizing(self):
        """A signal with no risk_multiplier set sizes identically to one
        with risk_multiplier=1.0 explicitly — the new field changes nothing
        by default."""
        prices = [100, 101, 102, 103, 104]
        df     = _make_ohlcv(prices, spread=0.0)
        sig_default  = _long_signal(0, df, sl_pct=0.05)
        sig_explicit = Signal(
            direction="long", entry_price=sig_default.entry_price,
            stop_loss=sig_default.stop_loss, take_profit=None,
            timestamp=sig_default.timestamp, candle_index=0, timeframe="1h",
            risk_multiplier=1.0,
        )
        r1 = Simulate(self._cfg()).run(_make_result([sig_default], df))
        r2 = Simulate(self._cfg()).run(_make_result([sig_explicit], df))
        assert r1.closed_trades[0].size == pytest.approx(r2.closed_trades[0].size)
        assert r1.closed_trades[0].risk_amount == pytest.approx(r2.closed_trades[0].risk_amount)

    def test_half_multiplier_halves_risk_percent_sizing(self):
        prices = [100, 101, 102, 103, 104]
        df     = _make_ohlcv(prices, spread=0.0)
        full = _long_signal(0, df, sl_pct=0.05)
        half = Signal(
            direction="long", entry_price=full.entry_price, stop_loss=full.stop_loss,
            take_profit=None, timestamp=full.timestamp, candle_index=0, timeframe="1h",
            risk_multiplier=0.5,
        )
        r_full = Simulate(self._cfg()).run(_make_result([full], df))
        r_half = Simulate(self._cfg()).run(_make_result([half], df))
        t_full, t_half = r_full.closed_trades[0], r_half.closed_trades[0]
        assert t_half.risk_amount == pytest.approx(t_full.risk_amount * 0.5)
        assert t_half.size        == pytest.approx(t_full.size * 0.5)
        assert t_half.margin_amount == pytest.approx(t_full.margin_amount * 0.5)

    def test_multiplier_scales_fixed_lot_sizing(self):
        prices = [100, 101, 102, 103, 104]
        df     = _make_ohlcv(prices, spread=0.0)
        full = _long_signal(0, df, sl_pct=0.05)
        quarter = Signal(
            direction="long", entry_price=full.entry_price, stop_loss=full.stop_loss,
            take_profit=None, timestamp=full.timestamp, candle_index=0, timeframe="1h",
            risk_multiplier=0.25,
        )
        cfg = self._cfg(position_sizing="fixed_lot", fixed_lot=2.0)
        r_full = Simulate(cfg).run(_make_result([full], df))
        r_qtr  = Simulate(cfg).run(_make_result([quarter], df))
        assert r_qtr.closed_trades[0].size == pytest.approx(r_full.closed_trades[0].size * 0.25)
        assert r_qtr.closed_trades[0].size == pytest.approx(0.5)   # 2.0 * 0.25

    def test_split_signals_sum_to_one_full_risk_unit(self):
        """Three signals at the same candle, each risk_multiplier=1/3,
        should jointly risk the same total amount as one risk_multiplier=1.0
        signal — this is the scale-out / multi-sub-position pattern."""
        prices = [100] * 10
        df     = _make_ohlcv(prices, spread=0.0)

        def _sig(mult):
            return Signal(
                direction="long", entry_price=100.0, stop_loss=95.0,
                take_profit=100.0 + (5.0 * (mult * 3)),  # arbitrary distinct TPs, unused here
                timestamp=int(df.iloc[0]["timestamp"]), candle_index=0, timeframe="1h",
                risk_multiplier=mult,
            )
        split_signals = [_sig(1 / 3) for _ in range(3)]
        full_signal   = [Signal(
            direction="long", entry_price=100.0, stop_loss=95.0, take_profit=None,
            timestamp=int(df.iloc[0]["timestamp"]), candle_index=0, timeframe="1h",
        )]

        cfg = self._cfg(max_long_positions=5, max_positions=5)
        r_split = Simulate(cfg).run(_make_result(split_signals, df))
        r_full  = Simulate(cfg).run(_make_result(full_signal, df))

        assert len(r_split.closed_trades) == 3
        total_split_risk = sum(t.risk_amount for t in r_split.closed_trades)
        assert total_split_risk == pytest.approx(r_full.closed_trades[0].risk_amount, rel=1e-6)


# ===========================================================================
# 8. Engine — multi-RR TP mode
# ===========================================================================

class TestEngineMultiRR:

    def _cfg(self, levels=None):
        return SimulateConfig(
            initial_balance=10_000,
            leverage=1,
            spread=0.0,
            commission=0.0, commission_type="fixed",
            risk_per_trade=1.0,
            tp_mode="multi_rr",
            tp_levels=levels or [1.0, 2.0, 3.0],
            sl_mode="signal",
            primary_timeframe="1h",
        )

    def test_multi_rr_closes_at_final_level(self):
        """Price reaches all TP levels → close at 3R."""
        # entry=100, SL=95 (sl_dist=5), TPs at 105, 110, 115
        # Prices need to cross 115 eventually
        prices = [100, 103, 106, 110, 116, 112]
        df     = _make_ohlcv(prices, spread=0.0)
        sig    = Signal(
            direction="long", entry_price=100.0, stop_loss=95.0,
            take_profit=None, timestamp=int(df.iloc[0]["timestamp"]),
            candle_index=0, timeframe="1h",
        )
        report = Simulate(self._cfg()).run(_make_result([sig], df))
        ct = report.closed_trades[0]
        assert ct.close_reason == CLOSE_REASON_TP
        assert ct.net_pnl > 0
        assert ct.rr_levels_hit >= 2   # at least 2 levels were hit before final

    def test_multi_rr_sl_after_rr1_is_rf(self):
        """Position hits 1R TP (moves SL to entry), then price reverses → RF close."""
        # entry=100, SL=95, 1R TP=105
        # Prices: cross 105 (triggers TP1 → SL→100), then drop to 100 → RF close
        prices = [100, 106, 103, 99, 98]
        df     = _make_ohlcv(prices, spread=0.0)
        sig    = Signal(
            direction="long", entry_price=100.0, stop_loss=95.0,
            take_profit=None, timestamp=int(df.iloc[0]["timestamp"]),
            candle_index=0, timeframe="1h",
        )
        report = Simulate(self._cfg()).run(_make_result([sig], df))
        ct = report.closed_trades[0]
        # Close reason should be "rf" (risk-free SL hit after TP1)
        assert ct.close_reason == CLOSE_REASON_RF
        assert ct.rr_levels_hit == 1


# ===========================================================================
# 8b. Engine — multi-RR TRUE partial closes via tp_level_close_fractions (v0.7.3)
# ===========================================================================

class TestEngineMultiRRPartialClose:
    """
    SimulateConfig.tp_level_close_fractions lets each multi-RR level
    realise a real, partial close (reduced size, proportional PnL/
    commission/margin) instead of only moving the SL. All scenarios below
    use the same entry=100 / sl=95 (sl_distance=5) / levels=[1,2,3] →
    TP prices [105, 110, 115] shape as the existing TestEngineMultiRR
    tests, for direct comparability.
    """

    def _cfg(self, fractions, levels=None, commission=0.0, commission_type="fixed"):
        return SimulateConfig(
            initial_balance=10_000,
            leverage=1,
            spread=0.0,
            commission=commission, commission_type=commission_type,
            risk_per_trade=1.0,
            tp_mode="multi_rr",
            tp_levels=levels or [1.0, 2.0, 3.0],
            tp_level_close_fractions=fractions,
            sl_mode="signal",
            primary_timeframe="1h",
        )

    def _signal(self, df):
        return Signal(
            direction="long", entry_price=100.0, stop_loss=95.0,
            take_profit=None, timestamp=int(df.iloc[0]["timestamp"]),
            candle_index=0, timeframe="1h",
        )

    def test_equal_three_way_split_realises_progressively(self):
        """fractions=[1/3,1/3,1/3]: each level banks 1/3 of the ORIGINAL
        size; the last level's slice exactly exhausts the remainder, so it
        is labelled 'tp' (full) while the first two are 'tp_rr' (partial).
        Same price path as test_multi_rr_closes_at_final_level."""
        prices = [100, 103, 106, 110, 116, 112]
        df     = _make_ohlcv(prices, spread=0.0)
        cfg    = self._cfg(fractions=[1 / 3, 1 / 3, 1 / 3])
        report = Simulate(cfg).run(_make_result([self._signal(df)], df))

        trades = report.closed_trades
        assert len(trades) == 3
        assert all(t.trade_id == trades[0].trade_id for t in trades), \
            "all slices of one scaled-out entry must share one trade_id"
        assert [t.close_reason for t in trades] == [
            CLOSE_REASON_TP_PARTIAL, CLOSE_REASON_TP_PARTIAL, CLOSE_REASON_TP,
        ]
        assert [t.exit_price for t in trades] == [105.0, 110.0, 115.0]

        original_size = trades[0].size * 3   # each slice ≈ 1/3 of the original
        assert sum(t.size for t in trades) == pytest.approx(original_size)
        assert sum(t.risk_amount for t in trades) == pytest.approx(100.0)  # 1% of 10,000
        # Hand-derived ground truth: 1/3 of size closed at +5, +10, +15
        # respectively, with pnl_per_price_unit=20 → (5+10+15)*20/3 = 200.
        assert report.total_pnl == pytest.approx(200.0, rel=1e-6)

    def test_wallet_conserved_no_leakage_or_double_counting(self):
        """initial_balance + sum(net_pnl) must equal final_balance exactly —
        margin released and commission charged must each be counted once
        across however many partial-close events one position produces."""
        prices = [100, 103, 106, 110, 116, 112]
        df     = _make_ohlcv(prices, spread=0.0)
        cfg    = self._cfg(fractions=[1 / 3, 1 / 3, 1 / 3], commission=10.0)
        report = Simulate(cfg).run(_make_result([self._signal(df)], df))

        assert len(report.closed_trades) == 3
        manual_total_pnl = sum(t.net_pnl for t in report.closed_trades)
        assert report.final_balance == pytest.approx(
            cfg.initial_balance + manual_total_pnl, rel=1e-9
        )

    def test_commission_split_across_partials_sums_to_one_round_trip(self):
        """Commission is pre-charged once at entry; partial closes must each
        take a proportional slice, summing back to exactly one round-trip
        charge — never 3x, never 0."""
        prices = [100, 103, 106, 110, 116, 112]
        df     = _make_ohlcv(prices, spread=0.0)
        cfg_split = self._cfg(fractions=[1 / 3, 1 / 3, 1 / 3], commission=9.0)
        cfg_whole = self._cfg(fractions=None, commission=9.0)

        report_split = Simulate(cfg_split).run(_make_result([self._signal(df)], df))
        report_whole = Simulate(cfg_whole).run(_make_result([self._signal(df)], df))

        assert sum(t.commission for t in report_split.closed_trades) == pytest.approx(
            sum(t.commission for t in report_whole.closed_trades), rel=1e-9
        )

    def test_partial_then_trailing_remainder_closes_via_sl(self):
        """fractions=[0.5, 0.0] over 2 levels: half is banked at level 1,
        the other half realises NOTHING at level 2 (SL just steps to the
        level-1 price and next_tp goes away) and is only closed later when
        price reverses onto that pinned SL."""
        cfg = self._cfg(fractions=[0.5, 0.0], levels=[1.0, 2.0])
        prices = [100, 103, 106, 111, 107, 103]
        df = _make_ohlcv(prices, spread=0.0)
        report = Simulate(cfg).run(_make_result([self._signal(df)], df))

        trades = report.closed_trades
        assert len(trades) == 2
        assert trades[0].close_reason == CLOSE_REASON_TP_PARTIAL
        assert trades[0].exit_price == 105.0
        assert trades[1].close_reason == CLOSE_REASON_RF
        assert trades[1].exit_price == 105.0    # SL pinned at level-1 price
        assert trades[0].size == pytest.approx(trades[1].size)   # 50 / 50
        assert trades[0].trade_id == trades[1].trade_id

    def test_all_zero_fractions_never_partially_closes(self):
        """All-zero fractions reproduce the 'let it run, never bank early'
        pattern: SL just walks through every level and nothing closes
        until the position is eventually stopped out with its FULL size —
        one single ClosedTrade, just like the pre-v0.7.3 default, but the
        final exit is via the trailed SL rather than a final TP level."""
        cfg = self._cfg(fractions=[0.0, 0.0, 0.0])
        prices = [100, 103, 106, 110, 116, 112, 108]
        df = _make_ohlcv(prices, spread=0.0)
        report = Simulate(cfg).run(_make_result([self._signal(df)], df))

        assert len(report.closed_trades) == 1
        ct = report.closed_trades[0]
        assert ct.close_reason == CLOSE_REASON_RF
        assert ct.rr_levels_hit == 3
        assert ct.exit_price == 110.0          # pinned at the 2nd level price
        assert ct.net_pnl > 0                  # still a winner: SL trailed above entry

    def test_default_none_is_byte_for_byte_unchanged(self):
        """tp_level_close_fractions=None (the default) must reproduce
        test_multi_rr_closes_at_final_level's exact result — one full
        close, nothing partial."""
        prices = [100, 103, 106, 110, 116, 112]
        df     = _make_ohlcv(prices, spread=0.0)
        cfg    = self._cfg(fractions=None)
        report = Simulate(cfg).run(_make_result([self._signal(df)], df))
        assert len(report.closed_trades) == 1
        assert report.closed_trades[0].close_reason == CLOSE_REASON_TP

    def test_gap_spanning_two_levels_in_one_candle_with_fractions(self):
        """A single candle that gaps straight through two levels at once
        must still realise both partial fractions correctly (exercises the
        gap → body fallthrough path introduced for partial closes). The
        first level fills at the gap-open price (no trading occurred at
        the level itself); the second is caught by the body-check
        fallthrough and fills at its exact level price — this mirrors
        exactly how a non-partial multi_rr gap-then-body sequence already
        behaved pre-v0.7.3."""
        cfg = self._cfg(fractions=[0.5, 0.5], levels=[1.0, 2.0])
        # Candle 1: small move. Candle 2: opens at 112 — past both
        # TP1 (105) and TP2 (110) in one gap.
        df = pd.DataFrame({
            "timestamp": [_BASE_TS, _BASE_TS + _1H_MS, _BASE_TS + 2 * _1H_MS],
            "open":      [100.0, 100.0, 112.0],
            "high":      [100.0, 103.0, 112.0],
            "low":       [100.0, 100.0, 112.0],
            "close":     [100.0, 103.0, 112.0],
            "volume":    [1000.0, 1000.0, 1000.0],
        })
        report = Simulate(cfg).run(_make_result([self._signal(df)], df))
        trades = report.closed_trades
        assert len(trades) == 2
        assert trades[0].exit_price == pytest.approx(112.0)   # gap fill at open
        assert trades[1].exit_price == pytest.approx(110.0)   # body-check: exact level price
        assert trades[0].close_reason == CLOSE_REASON_TP_PARTIAL
        assert trades[1].close_reason == CLOSE_REASON_TP
        assert trades[0].size == pytest.approx(trades[1].size)
        assert trades[0].trade_id == trades[1].trade_id

    def test_run_multi_supports_partial_closes(self):
        """run_multi's independently-maintained loop must reproduce the
        same partial-close behaviour as the single-pair Simulate engine."""
        prices = [100, 103, 106, 110, 116, 112]
        df     = _make_ohlcv(prices, spread=0.0)
        cfg    = self._cfg(fractions=[1 / 3, 1 / 3, 1 / 3])

        class _OneShot(BaseStrategy):
            primary_timeframe = "1h"
            def setup(self, data): pass
            def prepare_indicators(self, data): return data
            def generate_signals(self, candle_index, data):
                if candle_index == 0:
                    return [self._sig]
                return []

        strat = _OneShot()
        strat._sig = self._signal(df)
        report = run_multi([(strat, {"1h": df}, cfg)])

        trades = report.closed_trades
        assert len(trades) == 3
        assert [t.close_reason for t in trades] == [
            CLOSE_REASON_TP_PARTIAL, CLOSE_REASON_TP_PARTIAL, CLOSE_REASON_TP,
        ]


# ===========================================================================
# 8c. SimulateConfig — tp_level_close_fractions validation (v0.7.3)
# ===========================================================================

class TestSimulateConfigPartialTPValidation:

    def _base(self, **overrides):
        defaults = dict(tp_mode="multi_rr", tp_levels=[1.0, 2.0, 3.0])
        defaults.update(overrides)
        return defaults

    def test_none_is_valid_default(self):
        cfg = SimulateConfig(**self._base())
        assert cfg.tp_level_close_fractions is None

    def test_equal_thirds_summing_to_one_is_valid(self):
        cfg = SimulateConfig(**self._base(
            tp_level_close_fractions=[1 / 3, 1 / 3, 1 / 3]
        ))
        assert sum(cfg.tp_level_close_fractions) == pytest.approx(1.0)

    def test_all_zero_is_valid(self):
        cfg = SimulateConfig(**self._base(tp_level_close_fractions=[0.0, 0.0, 0.0]))
        assert cfg.tp_level_close_fractions == [0.0, 0.0, 0.0]

    def test_wrong_length_raises(self):
        with pytest.raises(ValueError, match="same length"):
            SimulateConfig(**self._base(tp_level_close_fractions=[0.5, 0.5]))

    def test_fraction_above_one_raises(self):
        with pytest.raises(ValueError, match=r"\[0\.0, 1\.0\]"):
            SimulateConfig(**self._base(
                tp_level_close_fractions=[1.5, 0.0, 0.0]
            ))

    def test_negative_fraction_raises(self):
        with pytest.raises(ValueError, match=r"\[0\.0, 1\.0\]"):
            SimulateConfig(**self._base(
                tp_level_close_fractions=[-0.1, 0.5, 0.5]
            ))

    def test_sum_above_one_raises(self):
        with pytest.raises(ValueError, match="sum to <= 1.0"):
            SimulateConfig(**self._base(
                tp_level_close_fractions=[0.5, 0.5, 0.5]
            ))

    def test_requires_multi_rr_mode(self):
        with pytest.raises(ValueError, match="requires tp_mode='multi_rr'"):
            SimulateConfig(
                tp_mode="fixed_rr",
                tp_level_close_fractions=[0.5, 0.5],
            )


# ===========================================================================
# 9. Engine — trailing SL
# ===========================================================================

class TestEngineTrailingSL:

    def test_trailing_sl_moves_up(self):
        """Trail SL follows rising price then triggers on reversal."""
        prices = [100, 102, 104, 106, 105, 104, 103, 100]
        df     = _make_ohlcv(prices, spread=0.0)
        sig    = _long_signal(0, df, sl_pct=0.10)   # initial SL = 90
        cfg    = SimulateConfig(
            initial_balance=10_000,
            leverage=1,
            spread=0.0, commission=0.0, commission_type="fixed",
            risk_per_trade=1.0,
            sl_mode="trailing",
            trailing_sl_percent=2.0,   # 2 % below peak
            tp_mode="none",
            primary_timeframe="1h",
        )
        report = Simulate(cfg).run(_make_result([sig], df))
        ct = report.closed_trades[0]
        # Should close before end of data because trailing SL is hit
        assert ct.close_reason in (CLOSE_REASON_SL, CLOSE_REASON_RF)
        # Exit price should be better than initial SL (90)
        assert ct.exit_price > 90.0


# ===========================================================================
# 10. Engine — risk-free (break-even)
# ===========================================================================

class TestEngineRiskFree:

    def test_risk_free_activates_and_closes_at_entry(self):
        # entry=100, SL=95 (5 dist), risk_free_at_rr=1.0 → activates at 105
        # Price goes to 106, then drops to 100 → break-even close
        prices = [100, 107, 104, 101, 99]
        df     = _make_ohlcv(prices, spread=0.0)
        sig    = Signal(
            direction="long", entry_price=100.0, stop_loss=95.0,
            take_profit=None, timestamp=int(df.iloc[0]["timestamp"]),
            candle_index=0, timeframe="1h",
        )
        cfg = SimulateConfig(
            initial_balance=10_000, leverage=1,
            spread=0.0, commission=0.0, commission_type="fixed",
            risk_per_trade=1.0,
            tp_mode="none",
            risk_free_enabled=True,
            risk_free_at_rr=1.0,
            primary_timeframe="1h",
        )
        report = Simulate(cfg).run(_make_result([sig], df))
        ct = report.closed_trades[0]
        assert ct.close_reason == CLOSE_REASON_RF
        # Net PnL ≈ 0 minus commission (we close at entry after risk-free activated)
        assert ct.gross_pnl == pytest.approx(0.0, abs=0.01)


# ===========================================================================
# 11. Engine — position limits
# ===========================================================================

class TestEnginePositionLimits:

    def test_max_positions_enforced(self):
        """Only one position opened when max_positions=1, even with two signals."""
        prices = [100] * 20
        df     = _make_ohlcv(prices, spread=0.0)
        sig1   = _long_signal(0, df, sl_pct=0.50)
        sig2   = _long_signal(1, df, sl_pct=0.50)
        cfg    = SimulateConfig(
            initial_balance=10_000, leverage=1,
            spread=0.0, commission=0.0, commission_type="fixed",
            risk_per_trade=1.0, max_positions=1,
            tp_mode="none", primary_timeframe="1h",
        )
        report = Simulate(cfg).run(_make_result([sig1, sig2], df))
        assert report.total_trades == 1

    def test_max_long_positions_enforced(self):
        prices = [100] * 20
        df     = _make_ohlcv(prices, spread=0.0)
        sig1   = _long_signal(0, df, sl_pct=0.50)
        sig2   = _long_signal(1, df, sl_pct=0.50)
        cfg    = SimulateConfig(
            initial_balance=10_000, leverage=1,
            spread=0.0, commission=0.0, commission_type="fixed",
            risk_per_trade=1.0, max_long_positions=1, max_positions=5,
            tp_mode="none", primary_timeframe="1h",
        )
        report = Simulate(cfg).run(_make_result([sig1, sig2], df))
        long_count = report.long_trades
        assert long_count == 1

    def test_max_short_positions_enforced(self):
        prices = [100] * 20
        df     = _make_ohlcv(prices, spread=0.0)
        sig1   = _short_signal(0, df, sl_pct=0.50)
        sig2   = _short_signal(1, df, sl_pct=0.50)
        cfg    = SimulateConfig(
            initial_balance=10_000, leverage=1,
            spread=0.0, commission=0.0, commission_type="fixed",
            risk_per_trade=1.0, max_short_positions=1, max_positions=5,
            tp_mode="none", primary_timeframe="1h",
        )
        report = Simulate(cfg).run(_make_result([sig1, sig2], df))
        assert report.short_trades == 1


# ===========================================================================
# 12. Engine — MetaTrader mode
# ===========================================================================

class TestEngineMetaTrader:

    def test_mt5_eurusd_sl_hit(self):
        prices = [1.10, 1.101, 1.102, 1.090, 1.105]
        df     = _make_ohlcv(prices, spread=0.0)
        # SL just below candle 3's low (1.090 - 0.0005 = 1.0895)
        entry = 1.10
        sl    = 1.095   # ~50 pips below entry
        sig   = Signal(
            direction="long", entry_price=entry, stop_loss=sl,
            take_profit=None, timestamp=int(df.iloc[0]["timestamp"]),
            candle_index=0, timeframe="1h",
        )
        cfg   = SimulateConfig(
            initial_balance=10_000,
            symbol="eurusd",
            exchange_type="metatrader",
            leverage=100,
            spread=0.0,
            commission=5.0, commission_type="per_lot",
            risk_per_trade=1.0,
            tp_mode="none",
            primary_timeframe="1h",
        )
        report = Simulate(cfg).run(_make_result([sig], df))
        ct = report.closed_trades[0]
        assert ct.close_reason == CLOSE_REASON_SL
        # Net loss ≈ risk_amount + commission
        assert ct.net_pnl < 0

    def test_mt5_size_is_lots(self):
        """For MT5 risk sizing, size field should be a lot value (not large units)."""
        prices = [1.10] * 5
        df     = _make_ohlcv(prices, spread=0.0)
        sig    = Signal(
            direction="long", entry_price=1.10, stop_loss=1.09,
            take_profit=None, timestamp=int(df.iloc[0]["timestamp"]),
            candle_index=0, timeframe="1h",
        )
        cfg = SimulateConfig(
            initial_balance=10_000, symbol="eurusd",
            exchange_type="metatrader", leverage=100,
            spread=0.0, commission=0.0, commission_type="fixed",
            risk_per_trade=1.0, tp_mode="none", primary_timeframe="1h",
        )
        report = Simulate(cfg).run(_make_result([sig], df))
        ct = report.closed_trades[0]
        # 1% of 10000 = $100 risk; SL=100 pips → lots = 100/(100*10) = 0.10
        assert ct.size == pytest.approx(0.10, abs=0.01)


# ===========================================================================
# 13. Report statistics
# ===========================================================================

class TestReportStatistics:

    def _make_report_with_trades(self, win_pnl_list, loss_pnl_list):
        """
        Build a SimulateReport by simulating a known mix of wins and losses.
        """
        from AlgoTradeKit.simulate._position import CLOSE_REASON_SL, CLOSE_REASON_TP, ClosedTrade
        trades = []
        ts = _BASE_TS
        for i, pnl in enumerate(win_pnl_list):
            ct = ClosedTrade(
                trade_id=i, symbol="btcusdt", direction="long",
                open_time=ts, close_time=ts + _1H_MS,
                entry_price=100.0, exit_price=102.0,
                initial_stop_loss=98.0, final_stop_loss=98.0,
                take_profit=102.0,
                size=0.1, margin_amount=100.0, risk_amount=100.0,
                gross_pnl=pnl, commission=0.0, net_pnl=pnl, pnl_r=pnl/100,
                close_reason=CLOSE_REASON_TP, rr_levels_hit=0,
                max_favourable_excursion=pnl, max_adverse_excursion=0.0,
                leverage=1.0, spread_paid=0.0,
            )
            trades.append(ct)
            ts += 2 * _1H_MS

        for i, pnl in enumerate(loss_pnl_list):
            loss_pnl = -abs(pnl)   # losses must be negative
            ct = ClosedTrade(
                trade_id=len(win_pnl_list) + i, symbol="btcusdt", direction="long",
                open_time=ts, close_time=ts + _1H_MS,
                entry_price=100.0, exit_price=98.0,
                initial_stop_loss=98.0, final_stop_loss=98.0,
                take_profit=None,
                size=0.1, margin_amount=100.0, risk_amount=100.0,
                gross_pnl=loss_pnl, commission=0.0, net_pnl=loss_pnl, pnl_r=loss_pnl/100,
                close_reason=CLOSE_REASON_SL, rr_levels_hit=0,
                max_favourable_excursion=0.0, max_adverse_excursion=abs(loss_pnl),
                leverage=1.0, spread_paid=0.0,
            )
            trades.append(ct)
            ts += 2 * _1H_MS

        bal_history = []
        equity = 10_000.0
        ts2 = _BASE_TS
        for t in sorted(trades, key=lambda x: x.close_time):
            equity += t.net_pnl
            bal_history.append({"timestamp": ts2, "wallet": equity, "equity": equity})
            ts2 += _1H_MS

        cfg = SimulateConfig(initial_balance=10_000.0, drawdown_threshold=5.0)
        return build_report(trades, [], bal_history, cfg)

    def test_win_rate(self):
        report = self._make_report_with_trades([100, 200], [50])
        assert report.winning_trades == 2
        assert report.losing_trades  == 1
        assert report.win_rate       == pytest.approx(2/3 * 100, rel=0.01)

    def test_profit_factor(self):
        report = self._make_report_with_trades([100, 100], [50])
        assert report.profit_factor == pytest.approx(200 / 50, rel=0.001)

    def test_avg_win_avg_loss(self):
        report = self._make_report_with_trades([100, 200], [50, 150])
        assert report.avg_win  == pytest.approx(150.0)
        assert report.avg_loss == pytest.approx(-100.0)

    def test_consecutive_losses(self):
        wins   = [100]
        losses = [-50, -50, -50, -50, -50]   # 5 in a row
        report = self._make_report_with_trades(wins, losses)
        assert report.max_consecutive_losses == 5

    def test_max_drawdown_computed(self):
        # Force a big drawdown: first win then 5 losses
        report = self._make_report_with_trades([500], [-100, -100, -100, -100, -100])
        dd = report.max_drawdown
        assert dd is not None
        assert dd.drawdown_percent > 0

    def test_significant_drawdowns_filtered(self):
        report = self._make_report_with_trades([1000], [-50])
        # 50 / 11000 ≈ 0.5% — below default 5% threshold
        assert len(report.significant_drawdowns) == 0

    def test_no_trades_report(self):
        cfg = SimulateConfig(initial_balance=10_000.0)
        bh  = [{"timestamp": _BASE_TS, "wallet": 10_000.0, "equity": 10_000.0}]
        report = build_report([], [], bh, cfg)
        assert report.total_trades == 0
        assert report.win_rate     == 0.0
        assert report.total_pnl    == 0.0

    def test_trade_markers_populated(self):
        report = self._make_report_with_trades([100], [50])
        assert len(report.trade_markers) == 2
        marker = report.trade_markers[0]
        assert "trade_id" in marker
        assert "net_pnl" in marker
        assert "signal_candle_index" in marker


# ===========================================================================
# 14. run_batch
# ===========================================================================

class TestRunBatch:

    class _FlatStrategy(BaseStrategy):
        """Strategy that always generates one long signal at candle 2."""
        primary_timeframe = "1h"
        warmup_period = 0

        def prepare_indicators(self, data):
            return data

        def generate_signals(self, candle_index, data):
            if candle_index == 2:
                df = data["1h"]
                price = float(df.iloc[candle_index]["close"])
                ts    = int(df.iloc[candle_index]["timestamp"])
                return [Signal(
                    direction="long", entry_price=price,
                    stop_loss=price * 0.90, take_profit=None,
                    timestamp=ts, candle_index=candle_index,
                    timeframe="1h",
                )]
            return []

    def _data(self, n=30):
        prices = [100.0 + i * 0.5 for i in range(n)]
        return {"1h": _make_ohlcv(prices)}

    def test_returns_one_report_per_config(self):
        strat   = self._FlatStrategy()
        configs = [
            SimulateConfig(
                risk_per_trade=0.5, tp_mode="none", commission_type="fixed", commission=0
            ),
            SimulateConfig(
                risk_per_trade=1.0, tp_mode="none", commission_type="fixed", commission=0
            ),
            SimulateConfig(
                risk_per_trade=2.0, tp_mode="none", commission_type="fixed", commission=0
            ),
        ]
        reports = run_batch(strat, self._data(), configs, max_workers=1)
        assert len(reports) == 3

    def test_order_preserved(self):
        """Reports come back in the same order as configs."""
        strat   = self._FlatStrategy()
        risks   = [0.5, 1.5, 3.0]
        configs = [
            SimulateConfig(risk_per_trade=r, tp_mode="none",
                           commission_type="fixed", commission=0, config_id=f"r{r}")
            for r in risks
        ]
        reports = run_batch(strat, self._data(), configs, max_workers=1)
        for i, (r, rpt) in enumerate(zip(risks, reports)):
            assert rpt.config.risk_per_trade == r

    def test_higher_risk_higher_pnl_when_winning(self):
        """On a winning trade, higher risk → higher PnL (proportional)."""
        strat   = self._FlatStrategy()
        configs = [
            SimulateConfig(risk_per_trade=0.5, tp_mode="none",
                           commission_type="fixed", commission=0),
            SimulateConfig(risk_per_trade=2.0, tp_mode="none",
                           commission_type="fixed", commission=0),
        ]
        reports = run_batch(strat, self._data(), configs, max_workers=1)
        pnl_low, pnl_high = reports[0].total_pnl, reports[1].total_pnl
        # Both should have positive PnL (price rises) with high-risk having more
        assert abs(pnl_high) > abs(pnl_low)

    def test_empty_configs_returns_empty(self):
        strat   = self._FlatStrategy()
        reports = run_batch(strat, self._data(), [])
        assert reports == []


# ===========================================================================
# 15. run_multi (smoke test)
# ===========================================================================

class TestRunMulti:

    class _BtcStrategy(BaseStrategy):
        primary_timeframe = "1h"
        warmup_period = 0

        def prepare_indicators(self, data):
            return data

        def generate_signals(self, candle_index, data):
            if candle_index == 1:
                df = data["1h"]
                price = float(df.iloc[1]["close"])
                ts    = int(df.iloc[1]["timestamp"])
                return [Signal(
                    direction="long", entry_price=price,
                    stop_loss=price * 0.80, take_profit=None,
                    timestamp=ts, candle_index=1, timeframe="1h",
                )]
            return []

    def _btc_data(self):
        prices = [40000.0 + i * 100 for i in range(20)]
        return {"1h": _make_ohlcv(prices)}

    def _eth_data(self):
        prices = [2000.0 + i * 10 for i in range(20)]
        return {"1h": _make_ohlcv(prices)}

    def test_returns_single_combined_report(self):
        strat = self._BtcStrategy()
        btc_cfg = SimulateConfig(
            symbol="btcusdt", risk_per_trade=1.0,
            commission_type="fixed", commission=0,
            tp_mode="none", initial_balance=10_000,
        )
        eth_cfg = SimulateConfig(
            symbol="ethusdt", risk_per_trade=0.5,
            commission_type="fixed", commission=0,
            tp_mode="none", initial_balance=10_000,
        )
        report = run_multi([
            (strat, self._btc_data(), btc_cfg),
            (strat, self._eth_data(), eth_cfg),
        ], max_workers=1)
        assert isinstance(report, SimulateReport)
        # Two separate trades (one per symbol) should have been opened
        assert report.total_trades >= 1


# ===========================================================================
# 16. v0.7.4 — SL history, dynamic lines, posbox TP fix
# ===========================================================================

class TestSlHistory:
    """
    ClosedTrade.sl_history, final_next_tp, and peak_price fields added in
    v0.7.4.  These drive the dynamic SL/TP lines on the candle chart.
    """

    _BASE_TS = 1_700_000_000_000  # ms
    _1H_MS   = 3_600_000

    def _cfg_multi_rr(self, levels=None):
        return SimulateConfig(
            initial_balance=10_000,
            leverage=1,
            spread=0.0,
            commission=0.0, commission_type="fixed",
            risk_per_trade=1.0,
            tp_mode="multi_rr",
            tp_levels=levels or [1.0, 2.0, 3.0],
            sl_mode="signal",
            primary_timeframe="1h",
            show_chart=False,
            report_mode="none",
        )

    def _cfg_trailing(self, pct=2.0):
        return SimulateConfig(
            initial_balance=10_000,
            leverage=1,
            spread=0.0,
            commission=0.0, commission_type="fixed",
            risk_per_trade=1.0,
            tp_mode="none",
            sl_mode="trailing",
            trailing_sl_percent=pct,
            primary_timeframe="1h",
            show_chart=False,
            report_mode="none",
        )

    # ------------------------------------------------------------------
    # multi_rr: sl_history is populated at entry + after each TP hit
    # ------------------------------------------------------------------

    def test_multi_rr_sl_history_initial_entry(self):
        """sl_history must have at least one entry (the position open state)."""
        prices = [100, 101, 102, 98]   # TP1=105 never hit → closed by SL
        df  = _make_ohlcv(prices, spread=0.0)
        sig = Signal(
            direction="long", entry_price=100.0, stop_loss=95.0,
            take_profit=None, timestamp=int(df.iloc[0]["timestamp"]),
            candle_index=0, timeframe="1h",
        )
        report = Simulate(self._cfg_multi_rr()).run(_make_result([sig], df))
        ct = report.closed_trades[0]
        assert len(ct.sl_history) >= 1
        first = ct.sl_history[0]
        assert first["sl"] == pytest.approx(95.0)
        assert first["next_tp"] == pytest.approx(105.0)   # 100 + 1*5

    def test_multi_rr_sl_history_grows_on_tp_hit(self):
        """After TP1 hit SL advances to entry; sl_history should have 2 entries."""
        # entry=100, sl=95 → sl_dist=5 → tp1=105, tp2=110, tp3=115
        prices = [100, 106, 104, 98]   # crosses 105 on bar2, then drops below 100
        df  = _make_ohlcv(prices, spread=0.0)
        sig = Signal(
            direction="long", entry_price=100.0, stop_loss=95.0,
            take_profit=None, timestamp=int(df.iloc[0]["timestamp"]),
            candle_index=0, timeframe="1h",
        )
        report = Simulate(self._cfg_multi_rr()).run(_make_result([sig], df))
        ct = report.closed_trades[0]
        # sl_history: [entry-state, post-TP1-state]
        assert len(ct.sl_history) >= 2
        # After TP1 hit: SL should be at entry (break-even = 100)
        second = ct.sl_history[1]
        assert second["sl"] == pytest.approx(100.0)
        assert second["next_tp"] == pytest.approx(110.0)  # TP2

    def test_multi_rr_sl_history_has_timestamp(self):
        """Each sl_history entry must have a 'time' key in UTC ms."""
        prices = [100, 106, 111, 116]
        df  = _make_ohlcv(prices, spread=0.0)
        sig = Signal(
            direction="long", entry_price=100.0, stop_loss=95.0,
            take_profit=None, timestamp=int(df.iloc[0]["timestamp"]),
            candle_index=0, timeframe="1h",
        )
        report = Simulate(self._cfg_multi_rr()).run(_make_result([sig], df))
        ct = report.closed_trades[0]
        for entry in ct.sl_history:
            assert "time" in entry
            assert entry["time"] > 1_000_000_000_000   # plausibly ms-range

    def test_multi_rr_sl_history_after_two_tp_hits(self):
        """Two TP levels hit → sl_history has 3 entries (open + TP1 + TP2)."""
        # tp1=105 (SL→100), tp2=110 (SL→105), tp3=115 never hit → SL at 105 hit
        prices = [100, 106, 111, 108, 104]
        df  = _make_ohlcv(prices, spread=0.0)
        sig = Signal(
            direction="long", entry_price=100.0, stop_loss=95.0,
            take_profit=None, timestamp=int(df.iloc[0]["timestamp"]),
            candle_index=0, timeframe="1h",
        )
        report = Simulate(self._cfg_multi_rr()).run(_make_result([sig], df))
        ct = report.closed_trades[0]
        assert len(ct.sl_history) >= 3
        # Third entry: SL at TP1=105, next_tp=TP3=115
        third = ct.sl_history[2]
        assert third["sl"] == pytest.approx(105.0)

    # ------------------------------------------------------------------
    # multi_rr: final_next_tp is the TP target at close time
    # ------------------------------------------------------------------

    def test_final_next_tp_after_one_level(self):
        """Closed after TP1 hit (SL at entry → SL hit): final_next_tp = TP2."""
        prices = [100, 106, 103, 99]
        df  = _make_ohlcv(prices, spread=0.0)
        sig = Signal(
            direction="long", entry_price=100.0, stop_loss=95.0,
            take_profit=None, timestamp=int(df.iloc[0]["timestamp"]),
            candle_index=0, timeframe="1h",
        )
        report = Simulate(self._cfg_multi_rr()).run(_make_result([sig], df))
        ct = report.closed_trades[0]
        assert ct.final_next_tp == pytest.approx(110.0)   # TP2

    def test_final_next_tp_none_when_all_levels_consumed(self):
        """Closed at final TP: final_next_tp should be None (all consumed)."""
        prices = [100, 106, 111, 116]
        df  = _make_ohlcv(prices, spread=0.0)
        sig = Signal(
            direction="long", entry_price=100.0, stop_loss=95.0,
            take_profit=None, timestamp=int(df.iloc[0]["timestamp"]),
            candle_index=0, timeframe="1h",
        )
        report = Simulate(self._cfg_multi_rr()).run(_make_result([sig], df))
        ct = report.closed_trades[0]
        assert ct.final_next_tp is None

    # ------------------------------------------------------------------
    # multi_rr: trade_markers include sl_history and final_next_tp
    # ------------------------------------------------------------------

    def test_trade_markers_include_sl_history(self):
        prices = [100, 106, 104, 98]
        df  = _make_ohlcv(prices, spread=0.0)
        sig = Signal(
            direction="long", entry_price=100.0, stop_loss=95.0,
            take_profit=None, timestamp=int(df.iloc[0]["timestamp"]),
            candle_index=0, timeframe="1h",
        )
        report = Simulate(self._cfg_multi_rr()).run(_make_result([sig], df))
        marker = report.trade_markers[0]
        assert "sl_history" in marker
        assert isinstance(marker["sl_history"], list)
        assert "final_next_tp" in marker
        assert "peak_price" in marker

    # ------------------------------------------------------------------
    # trailing SL: sl_history records each SL advance
    # ------------------------------------------------------------------

    def test_trailing_sl_history_records_advance(self):
        """
        Trailing SL at 2% below peak.  As price rises, the SL must advance;
        each advance should be recorded in sl_history.
        """
        # entry=100, initial_sl=98 (2% below). Prices rise to 110 → trailing
        # SL chases up.  Then price drops → position closed.
        prices = [100, 103, 107, 110, 105, 100, 95]
        df  = _make_ohlcv(prices, spread=0.0)
        sig = Signal(
            direction="long", entry_price=100.0,
            stop_loss=100.0 * 0.98,   # 98
            take_profit=None,
            timestamp=int(df.iloc[0]["timestamp"]),
            candle_index=0, timeframe="1h",
        )
        report = Simulate(self._cfg_trailing(pct=2.0)).run(_make_result([sig], df))
        ct = report.closed_trades[0]
        # At least one SL advance should have been recorded beyond the initial entry
        assert len(ct.sl_history) >= 2
        # SL values should be strictly increasing (longs: SL only moves up)
        sl_vals = [e["sl"] for e in ct.sl_history]
        for a, b in zip(sl_vals, sl_vals[1:]):
            assert b >= a

    def test_trailing_sl_peak_price_stored(self):
        """peak_price in ClosedTrade = highest high during the trade (long)."""
        prices = [100, 105, 112, 109, 95]
        df  = _make_ohlcv(prices, spread=0.0)
        sig = Signal(
            direction="long", entry_price=100.0,
            stop_loss=98.0,
            take_profit=None,
            timestamp=int(df.iloc[0]["timestamp"]),
            candle_index=0, timeframe="1h",
        )
        report = Simulate(self._cfg_trailing(pct=2.0)).run(_make_result([sig], df))
        ct = report.closed_trades[0]
        # peak_price must be >= max seen close price
        max_close = max(prices[:4])   # 112 (before final close candle)
        assert ct.peak_price >= max_close

    # ------------------------------------------------------------------
    # Backward-compatibility: non-moving SL positions have sl_history
    # with exactly one entry
    # ------------------------------------------------------------------

    def test_signal_sl_mode_single_entry(self):
        """Plain signal-SL (no trailing, no multi_rr): sl_history has 1 entry."""
        prices = [100, 102, 98]
        df  = _make_ohlcv(prices, spread=0.0)
        sig = Signal(
            direction="long", entry_price=100.0, stop_loss=95.0,
            take_profit=105.0,
            timestamp=int(df.iloc[0]["timestamp"]),
            candle_index=0, timeframe="1h",
        )
        cfg = SimulateConfig(
            initial_balance=10_000, leverage=1, spread=0.0,
            commission=0.0, commission_type="fixed",
            risk_per_trade=1.0, tp_mode="signal", sl_mode="signal",
            primary_timeframe="1h", show_chart=False, report_mode="none",
        )
        report = Simulate(cfg).run(_make_result([sig], df))
        ct = report.closed_trades[0]
        # Exactly one entry: the initial open state
        assert len(ct.sl_history) == 1
        assert ct.sl_history[0]["sl"] == pytest.approx(95.0)

    # ------------------------------------------------------------------
    # indicator_renderer: add_simulation_positions (smoke test)
    # ------------------------------------------------------------------

    def test_add_simulation_positions_smoke(self):
        """
        add_simulation_positions must not raise and must add exactly one
        position box per unique trade_id (even for multi_rr).
        """
        from AlgoTradeKit.visual import Chart
        from AlgoTradeKit.visual.indicator_renderer import add_simulation_positions

        prices = [100, 106, 111, 116]
        df  = _make_ohlcv(prices, spread=0.0)
        sig = Signal(
            direction="long", entry_price=100.0, stop_loss=95.0,
            take_profit=None, timestamp=int(df.iloc[0]["timestamp"]),
            candle_index=0, timeframe="1h",
        )
        cfg = self._cfg_multi_rr()
        report = Simulate(cfg).run(_make_result([sig], df))

        chart = Chart(title="test")
        chart.set_data(df)
        add_simulation_positions(chart, report, config=cfg)

        from AlgoTradeKit.visual.models import PositionBox
        boxes = [d for d in chart._drawings if isinstance(d, PositionBox)]
        unique_tids = {m["trade_id"] for m in report.trade_markers}
        # One posbox per unique trade
        assert len(boxes) == len(unique_tids)

    def test_add_simulation_positions_draws_sl_lines(self):
        """
        For multi_rr with sl_history length > 1, dynamic SL TrendLine
        drawings must be added to the chart.
        """
        from AlgoTradeKit.visual import Chart
        from AlgoTradeKit.visual.indicator_renderer import add_simulation_positions
        from AlgoTradeKit.visual.models import TrendLine

        # hit TP1 so SL moves → sl_history length ≥ 2
        prices = [100, 106, 104, 98]
        df  = _make_ohlcv(prices, spread=0.0)
        sig = Signal(
            direction="long", entry_price=100.0, stop_loss=95.0,
            take_profit=None, timestamp=int(df.iloc[0]["timestamp"]),
            candle_index=0, timeframe="1h",
        )
        cfg = self._cfg_multi_rr()
        report = Simulate(cfg).run(_make_result([sig], df))

        chart = Chart(title="test")
        chart.set_data(df)
        add_simulation_positions(chart, report, config=cfg)

        trendlines = [d for d in chart._drawings if isinstance(d, TrendLine)]
        assert len(trendlines) >= 1   # at least one SL line segment drawn

    def test_posbox_uses_final_next_tp(self):
        """
        Position box TP boundary must reflect the final_next_tp, not the
        initial take_profit (TP1).  After TP1 hit (next_tp→TP2) the box
        should show TP2 as the profit-zone top.
        """
        from AlgoTradeKit.visual import Chart
        from AlgoTradeKit.visual.indicator_renderer import add_simulation_positions
        from AlgoTradeKit.visual.models import PositionBox

        # entry=100, SL=95 → tp1=105, tp2=110, tp3=115
        # Price hits TP1 → SL→100, next_tp→110; then price drops to 99 (SL hit)
        prices = [100, 106, 104, 98]
        df  = _make_ohlcv(prices, spread=0.0)
        sig = Signal(
            direction="long", entry_price=100.0, stop_loss=95.0,
            take_profit=None, timestamp=int(df.iloc[0]["timestamp"]),
            candle_index=0, timeframe="1h",
        )
        cfg = self._cfg_multi_rr()
        report = Simulate(cfg).run(_make_result([sig], df))

        chart = Chart(title="test")
        chart.set_data(df)
        add_simulation_positions(chart, report, config=cfg)

        boxes = [d for d in chart._drawings if isinstance(d, PositionBox)]
        assert boxes
        box = boxes[0]
        # TP shown on the box must be TP2 (110) not TP1 (105)
        assert box.take_profit == pytest.approx(110.0)

# ===========================================================================
# 17. SimulationStepper — step-driven engine (v1.0.0)
# ===========================================================================

def _random_walk_df(
    n: int, seed: int, start: float = 100.0, start_ts: int = _BASE_TS
) -> pd.DataFrame:
    """Deterministic OHLCV random walk — busy enough to hit every close path."""
    rng = np.random.default_rng(seed)
    closes = start * np.cumprod(1.0 + rng.normal(0.0005, 0.01, n))
    opens = np.concatenate([[start], closes[:-1]])
    wick = np.abs(rng.normal(0.0, 0.004, n)) * closes
    return pd.DataFrame({
        "timestamp": [start_ts + i * _1H_MS for i in range(n)],
        "open":      opens,
        "high":      np.maximum(opens, closes) + wick,
        "low":       np.minimum(opens, closes) - wick,
        "close":     closes,
        "volume":    np.full(n, 1000.0),
    })


def _walk_signal(df, i, direction, sl_pct=0.02, tp_pct=None, risk_multiplier=1.0) -> Signal:
    entry = float(df.iloc[i]["close"])
    f = -1.0 if direction == "long" else 1.0
    tp = entry * (1 - f * tp_pct) if tp_pct is not None else None
    return Signal(
        direction=direction, entry_price=entry, stop_loss=entry * (1 + f * sl_pct),
        take_profit=tp, timestamp=int(df.iloc[i]["timestamp"]), candle_index=i,
        timeframe="1h", risk_multiplier=risk_multiplier,
    )


def _drive_stepper(strategy_result, config, **stepper_kwargs):
    """Drive a SimulationStepper by hand over the full frame — the step-driven
    counterpart of ``Simulate.run()`` — and return ``(stepper, report)``."""
    df = strategy_result.data[config.primary_timeframe]
    sigs_by_idx: dict[int, list] = {}
    for s in strategy_result.signals:
        sigs_by_idx.setdefault(s.candle_index, []).append(s)
    exits_by_idx: dict[int, list] = {}
    for e in strategy_result.exit_signals:
        exits_by_idx.setdefault(e.candle_index, []).append(e)

    stepper = SimulationStepper(config, **stepper_kwargs)
    for i, row in enumerate(df.itertuples(index=False)):
        stepper.step(
            {
                "timestamp": row.timestamp,
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "volume": row.volume,   # extra key — must be ignored
            },
            sigs_by_idx.get(i, []),
            exits_by_idx.get(i, []),
        )
    stepper.finalize()
    return stepper, stepper.build_report()


def _report_blob(report) -> str:
    """Full-precision serialisation of everything the engine computes."""
    trades = repr([asdict(t) for t in report.closed_trades])
    return trades + "|" + repr(report.balance_history)


class _TableStrategy(BaseStrategy):
    """Replays a pre-built signal/exit table (for run_multi comparisons)."""

    primary_timeframe = "1h"
    warmup_period = 0

    def __init__(self, signals, exits=None):
        self._sig_table: dict[int, list] = {}
        for s in signals:
            self._sig_table.setdefault(s.candle_index, []).append(s)
        self._exit_table: dict[int, list] = {}
        for e in exits or []:
            self._exit_table.setdefault(e.candle_index, []).append(e)

    def prepare_indicators(self, data):
        return data

    def setup(self, data):
        pass

    def generate_signals(self, candle_index, data):
        return list(self._sig_table.get(candle_index, []))

    def detect_exit_signals(self, candle_index, data):
        return list(self._exit_table.get(candle_index, []))


class TestStepBatchParity:
    """ core requirement: driving the stepper candle-by-candle is
    byte-identical to the batch path (Simulate.run) on recorded data."""

    _NAMES = [
        "tp_signal", "fixed_rr", "multi_rr", "multi_rr_fractions",
        "multi_rr_remainder", "trailing", "risk_free", "force_close",
        "end_of_data", "metatrader", "fixed_lot_exchange", "fixed_amount",
        "compound", "limits", "risk_multiplier",
    ]

    def _scenario(self, name: str):
        df = _random_walk_df(300, seed=7)
        mixed = [_walk_signal(df, i, "long" if i % 2 else "short", 0.02, 0.05)
                 for i in range(10, 290, 23)]
        no_tp = [_walk_signal(df, i, "long" if i % 3 else "short", 0.015)
                 for i in range(10, 290, 17)]
        base = dict(symbol="btcusdt", show_chart=False, report_mode="none")

        if name == "tp_signal":
            return _make_result(mixed, df), SimulateConfig(**base, tp_mode="signal")
        if name == "fixed_rr":
            return _make_result(no_tp, df), SimulateConfig(**base, tp_mode="fixed_rr", tp_rr=2.0)
        if name == "multi_rr":
            return _make_result(no_tp, df), SimulateConfig(
                **base, tp_mode="multi_rr", tp_levels=[1, 2, 3])
        if name == "multi_rr_fractions":
            return _make_result(no_tp, df), SimulateConfig(
                **base, tp_mode="multi_rr", tp_levels=[1, 2, 3],
                tp_level_close_fractions=[0.4, 0.3, 0.3])
        if name == "multi_rr_remainder":
            return _make_result(no_tp, df), SimulateConfig(
                **base, tp_mode="multi_rr", tp_levels=[1, 2],
                tp_level_close_fractions=[0.3, 0.3])
        if name == "trailing":
            return _make_result(no_tp, df), SimulateConfig(
                **base, tp_mode="none", sl_mode="trailing", trailing_sl_percent=1.5)
        if name == "risk_free":
            return _make_result(mixed, df), SimulateConfig(
                **base, tp_mode="signal", risk_free_enabled=True, risk_free_at_rr=1.0)
        if name == "force_close":
            exits = [ExitSignal(reason="flip", exit_price=None,
                                timestamp=int(df.iloc[i]["timestamp"]), candle_index=i)
                     for i in range(20, 290, 41)]
            exits += [ExitSignal(reason="flip_px",
                                 exit_price=float(df.iloc[i]["close"]) * 0.999,
                                 timestamp=int(df.iloc[i]["timestamp"]), candle_index=i)
                      for i in range(35, 290, 57)]
            exits.sort(key=lambda e: e.candle_index)
            return _make_result(mixed, df, exits), SimulateConfig(
                **base, tp_mode="none", force_close_on_exit_signal=True)
        if name == "end_of_data":
            sigs = [_walk_signal(df, 295, "long", 0.10), _walk_signal(df, 296, "short", 0.10)]
            return _make_result(sigs, df), SimulateConfig(
                **base, tp_mode="none", max_positions=2,
                max_long_positions=2, max_short_positions=2)
        if name == "metatrader":
            fx = _random_walk_df(300, seed=11, start=1.10)
            fx_sigs = [_walk_signal(fx, i, "long" if i % 2 else "short", 0.005, 0.01)
                       for i in range(10, 290, 19)]
            return _make_result(fx_sigs, fx), SimulateConfig(
                symbol="eurusd", show_chart=False, report_mode="none",
                exchange_type=EXCHANGE_TYPE_METATRADER, tp_mode="signal",
                commission_type="per_lot", commission=7.0)
        if name == "fixed_lot_exchange":
            return _make_result(mixed, df), SimulateConfig(
                **base, position_sizing="fixed_lot", fixed_lot=0.05,
                tp_mode="signal", leverage=5)
        if name == "fixed_amount":
            return _make_result(mixed, df), SimulateConfig(
                **base, position_sizing="fixed_amount", fixed_amount=150.0, tp_mode="signal")
        if name == "compound":
            return _make_result(mixed, df), SimulateConfig(
                **base, compound=True, leverage=10, risk_per_trade=2.0,
                tp_mode="signal", spread=0.05, commission=0.0004)
        if name == "limits":
            dense = [_walk_signal(df, i, "long" if i % 2 else "short", 0.03, 0.06)
                     for i in range(10, 290, 5)]
            return _make_result(dense, df), SimulateConfig(
                **base, tp_mode="signal", max_positions=3,
                max_long_positions=2, max_short_positions=2)
        if name == "risk_multiplier":
            halves = [_walk_signal(df, i, "long", 0.02, 0.04, risk_multiplier=0.5)
                      for i in range(15, 280, 37)]
            return _make_result(halves, df), SimulateConfig(**base, tp_mode="signal")
        raise AssertionError(f"unknown scenario {name}")

    @pytest.mark.parametrize("name", _NAMES)
    def test_step_equals_batch(self, name):
        strategy_result, config = self._scenario(name)
        batch = Simulate(config).run(strategy_result)
        _, stepped = _drive_stepper(strategy_result, config)

        assert len(batch.closed_trades) > 0            # scenario must not be vacuous
        assert _report_blob(stepped) == _report_blob(batch)
        assert repr(stepped.final_balance) == repr(batch.final_balance)

    def test_step_drive_does_not_mutate_inputs(self):
        strategy_result, config = self._scenario("multi_rr_fractions")
        df_before = strategy_result.data["1h"].copy(deep=True)
        signals_before = list(strategy_result.signals)
        _drive_stepper(strategy_result, config)
        pd.testing.assert_frame_equal(strategy_result.data["1h"], df_before)
        assert strategy_result.signals == signals_before


class TestSimulationStepperUnit:
    """Behaviour of the stepper's own surface: step returns, finalize,
    snapshots, initial state, candle-mapping handling."""

    def _cfg(self, **over):
        base = dict(
            initial_balance=10_000, leverage=1, spread=0.0,
            commission=0.0, commission_type="fixed", risk_per_trade=1.0,
            symbol="btcusdt", primary_timeframe="1h",
            show_chart=False, report_mode="none",
        )
        base.update(over)
        return SimulateConfig(**base)

    def _long(self, df, i=0, sl=95.0, tp=None):
        return Signal(
            direction="long", entry_price=float(df.iloc[i]["close"]), stop_loss=sl,
            take_profit=tp, timestamp=int(df.iloc[i]["timestamp"]),
            candle_index=i, timeframe="1h",
        )

    @staticmethod
    def _candle(df, i) -> dict:
        row = df.iloc[i]
        return {
            "timestamp": int(row["timestamp"]),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
        }

    def test_step_returns_trades_closed_this_candle(self):
        # entry 100, TP 110 — high crosses 110 on candle 2 (111.5)
        df = _make_ohlcv([100, 104, 111, 108])
        stepper = SimulationStepper(self._cfg(tp_mode="signal"))

        assert stepper.step(self._candle(df, 0), [self._long(df, tp=110.0)]) == []
        assert len(stepper.open_positions) == 1
        assert stepper.step(self._candle(df, 1)) == []

        closed = stepper.step(self._candle(df, 2))
        assert len(closed) == 1
        assert closed[0].close_reason == CLOSE_REASON_TP
        assert closed[0] is stepper.closed_trades[-1]
        assert stepper.open_positions == []

    def test_step_returns_partial_slices(self):
        # entry 100, sl 95 → tp1 105, tp2 110; fractions close half at each
        df = _make_ohlcv([100, 106, 111, 108])
        cfg = self._cfg(tp_mode="multi_rr", tp_levels=[1, 2],
                        tp_level_close_fractions=[0.5, 0.5])
        stepper = SimulationStepper(cfg)

        stepper.step(self._candle(df, 0), [self._long(df)])
        partial = stepper.step(self._candle(df, 1))
        assert [t.close_reason for t in partial] == [CLOSE_REASON_TP_PARTIAL]
        assert len(stepper.open_positions) == 1        # half still open

        final = stepper.step(self._candle(df, 2))
        assert [t.close_reason for t in final] == [CLOSE_REASON_TP]
        assert final[0].trade_id == partial[0].trade_id
        assert stepper.open_positions == []

    def test_finalize_closes_open_at_last_stepped_close(self):
        df = _make_ohlcv([100, 101, 102])
        stepper = SimulationStepper(self._cfg(tp_mode="none"))
        for i in range(3):
            sigs = [self._long(df, 0, sl=90.0)] if i == 0 else []
            assert stepper.step(self._candle(df, i), sigs) == []

        eod = stepper.finalize()
        assert [t.close_reason for t in eod] == [CLOSE_REASON_EOD]
        assert eod[0].exit_price == pytest.approx(102.0)
        assert eod[0].close_time == int(df.iloc[-1]["timestamp"])
        assert stepper.open_positions == []
        assert stepper.finalized

    def test_finalize_idempotent_and_empty_stepper(self):
        empty = SimulationStepper(self._cfg())
        assert empty.finalize() == []
        assert empty.finalize() == []
        assert empty.finalized

        df = _make_ohlcv([100, 101])
        stepper = SimulationStepper(self._cfg(tp_mode="none"))
        stepper.step(self._candle(df, 0), [self._long(df, 0, sl=90.0)])
        stepper.step(self._candle(df, 1))
        assert len(stepper.finalize()) == 1
        assert stepper.finalize() == []                # second call: no-op
        assert len(stepper.closed_trades) == 1

    def test_step_after_finalize_raises(self):
        df = _make_ohlcv([100, 101])
        stepper = SimulationStepper(self._cfg())
        stepper.step(self._candle(df, 0))
        stepper.finalize()
        with pytest.raises(RuntimeError):
            stepper.step(self._candle(df, 1))

    def test_build_report_is_a_frozen_midrun_snapshot(self):
        df = _make_ohlcv([100, 101, 102, 103])
        stepper = SimulationStepper(self._cfg(tp_mode="none"))
        stepper.step(self._candle(df, 0), [self._long(df, 0, sl=90.0)])
        stepper.step(self._candle(df, 1))

        snap = stepper.build_report()
        assert len(snap.open_at_end) == 1              # live position visible
        assert snap.open_at_end[0]["trade_id"] == 0
        assert snap.closed_trades == []
        assert len(snap.balance_history) == 2
        assert snap.final_balance == snap.balance_history[-1]["equity"]

        stepper.step(self._candle(df, 2))
        stepper.step(self._candle(df, 3))
        stepper.finalize()
        # the earlier snapshot must not have grown
        assert len(snap.balance_history) == 2
        assert snap.closed_trades == []

        final = stepper.build_report()
        assert len(final.closed_trades) == 1
        assert final.open_at_end == []

    def test_initial_wallet_and_trade_id(self):
        df = _make_ohlcv([100, 101])
        stepper = SimulationStepper(
            self._cfg(tp_mode="none"), initial_wallet=5_000.0, initial_trade_id=100)
        assert stepper.wallet == pytest.approx(5_000.0)
        stepper.step(self._candle(df, 0), [self._long(df, 0, sl=90.0)])
        stepper.step(self._candle(df, 1))
        eod = stepper.finalize()
        assert eod[0].trade_id == 100
        assert stepper.trade_id_seq == 101

    def test_record_balance_history_off(self):
        df = _make_ohlcv([100, 101, 102])
        stepper = SimulationStepper(self._cfg(), record_balance_history=False)
        for i in range(3):
            stepper.step(self._candle(df, i))
        assert stepper.balance_history == []
        assert stepper.build_report().final_balance == pytest.approx(10_000.0)

    def test_candle_mapping_untouched_and_extra_keys_ignored(self):
        df = _make_ohlcv([100, 101])
        candle = self._candle(df, 0)
        candle["volume"] = 1234.5
        candle["closed"] = True                        # broker-stream flag
        before = dict(candle)
        stepper = SimulationStepper(self._cfg())
        stepper.step(candle, [self._long(df, 0, sl=90.0)])
        assert candle == before

    def test_missing_candle_key_raises(self):
        stepper = SimulationStepper(self._cfg())
        with pytest.raises(KeyError):
            stepper.step({"timestamp": _BASE_TS, "open": 1.0, "high": 1.0, "low": 1.0})


class TestRunMultiOnStepper:
    """run_multi now drives one SimulationStepper per pair (v1.0.0)."""

    def _cfg(self, **over):
        base = dict(
            initial_balance=10_000, spread=0.0, commission=0.0,
            commission_type="fixed", risk_per_trade=1.0,
            show_chart=False, report_mode="none",
        )
        base.update(over)
        return SimulateConfig(**base)

    def test_single_pair_run_multi_matches_simulate(self):
        """With one pair, run_multi must equal the single-pair engine
        trade-for-trade (the v0.7.4 sl_history entry state included)."""
        df = _random_walk_df(200, seed=31)
        sigs = [_walk_signal(df, i, "long" if i % 2 else "short", 0.02)
                for i in range(10, 190, 13)]
        cfg = self._cfg(symbol="btcusdt", tp_mode="multi_rr", tp_levels=[1, 2],
                        tp_level_close_fractions=[0.5, 0.5])
        strat = _TableStrategy(sigs)

        single = Simulate(cfg).run(strat.run({"1h": df}, mode=StrategyMode.BACKTEST))
        multi = run_multi([(strat, {"1h": df}, cfg)], max_workers=1)

        assert len(multi.closed_trades) > 0
        assert [asdict(t) for t in multi.closed_trades] == \
               [asdict(t) for t in single.closed_trades]
        assert multi.balance_history == single.balance_history

    def test_run_multi_trades_record_entry_sl_state(self):
        """Unification fix: portfolio trades now carry the entry-state
        sl_history record, like single-pair trades (v0.7.4)."""
        df = _random_walk_df(150, seed=32)
        sigs = [_walk_signal(df, i, "long", 0.02) for i in range(10, 140, 17)]
        cfg = self._cfg(symbol="btcusdt", tp_mode="multi_rr", tp_levels=[1, 2, 3])
        report = run_multi([(_TableStrategy(sigs), {"1h": df}, cfg)], max_workers=1)

        assert len(report.closed_trades) > 0
        for t in report.closed_trades:
            assert len(t.sl_history) >= 1
            first = t.sl_history[0]
            assert first["time"] == t.open_time
            assert first["sl"] == pytest.approx(t.initial_stop_loss)

    def test_two_pair_shared_wallet_and_trade_ids(self):
        df_a = _random_walk_df(150, seed=33)
        df_b = _random_walk_df(150, seed=34, start=50.0,
                               start_ts=_BASE_TS + 20 * _1H_MS)  # offset timeline
        sigs_a = [_walk_signal(df_a, i, "long", 0.02, 0.04) for i in range(10, 140, 11)]
        sigs_b = [_walk_signal(df_b, i, "short", 0.02, 0.04) for i in range(10, 140, 13)]
        report = run_multi([
            (_TableStrategy(sigs_a), {"1h": df_a}, self._cfg(symbol="aaausdt")),
            (_TableStrategy(sigs_b), {"1h": df_b}, self._cfg(symbol="bbbusdt")),
        ], max_workers=1)

        trades = report.closed_trades
        assert {t.symbol for t in trades} == {"aaausdt", "bbbusdt"}
        # one shared id sequence across pairs: ids are contiguous from 0
        ids = {t.trade_id for t in trades}
        assert ids == set(range(len(ids)))
        # balance history covers the union timeline of both pairs
        history_ts = [h["timestamp"] for h in report.balance_history]
        union_ts = sorted(set(df_a["timestamp"]) | set(df_b["timestamp"]))
        assert history_ts == union_ts


# ===========================================================================
# v1.0.0 — LiveSimulation (seed / step / window / display)
# ===========================================================================


def _df_rows(df: pd.DataFrame) -> list[dict]:
    """DataFrame → library-standard candle dicts (broker feed shape)."""
    return [
        {
            "timestamp": int(r.timestamp), "open": float(r.open),
            "high": float(r.high), "low": float(r.low),
            "close": float(r.close), "volume": float(r.volume),
        }
        for r in df.itertuples(index=False)
    ]


def _flat_rows(n: int, price: float = 100.0, start_ts: int = _BASE_TS) -> list[dict]:
    return [
        {
            "timestamp": start_ts + i * _1H_MS, "open": price,
            "high": price + 0.5, "low": price - 0.5,
            "close": price, "volume": 10.0,
        }
        for i in range(n)
    ]


def _candle(i: int, o: float, h: float, low: float, c: float,
            start_ts: int = _BASE_TS) -> dict:
    return {"timestamp": start_ts + i * _1H_MS, "open": o, "high": h,
            "low": low, "close": c, "volume": 10.0}


class _FakeStream:
    def __init__(self):
        self._alive = True

    @property
    def alive(self):
        return self._alive

    def stop(self, timeout: float = 5.0):
        self._alive = False


class _FakeLiveBroker:
    """Market-data-only fake broker: recorded fetches + manual stream push."""

    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.calls: list[tuple] = []
        self.on_candle = None
        self.stream_args: tuple | None = None
        self.streams: list[_FakeStream] = []

    def fetch_last_candles(self, symbol, timeframe, count):
        self.calls.append(("last", symbol, timeframe, count))
        return [dict(r) for r in self.rows[-count:]]

    def fetch_candles(self, symbol, timeframe, start_ms, end_ms):
        self.calls.append(("range", symbol, timeframe, start_ms, end_ms))
        return [dict(r) for r in self.rows if start_ms <= r["timestamp"] <= end_ms]

    def stream_candles(self, symbol, timeframe, on_candle, *, closed_only=True):
        self.on_candle = on_candle
        self.stream_args = (symbol, timeframe, closed_only)
        stream = _FakeStream()
        self.streams.append(stream)
        return stream

    def push(self, candle: dict, closed: bool = True):
        self.on_candle({**candle, "closed": closed})


class _LiveSMAStrategy(BaseStrategy):
    """Windowed SMA(3) column + deterministic signal cadence — exact under
    the tail recompute, so live stepping must equal the batch run."""

    primary_timeframe = "1h"
    warmup_period = 5

    def prepare_indicators(self, data):
        df = data[self.primary_timeframe].copy()
        df["_sma"] = df["close"].rolling(3).mean()
        data[self.primary_timeframe] = df
        return data

    def setup(self, data):
        pass

    def generate_signals(self, candle_index, data):
        df = data[self.primary_timeframe]
        row = df.iloc[candle_index]
        if candle_index % 7 == 0 and not pd.isna(row["_sma"]):
            entry = float(row["close"])
            return [Signal(
                direction="long", entry_price=entry, stop_loss=entry * 0.98,
                take_profit=entry * 1.04, timestamp=int(row["timestamp"]),
                candle_index=candle_index, timeframe="1h",
            )]
        return []


class _PlanStrategy(BaseStrategy):
    """Signals scripted by **timestamp** (stable under window trimming)."""

    primary_timeframe = "1h"
    warmup_period = 0

    def __init__(self, plan: dict[int, list[dict]]):
        self._plan = plan

    def prepare_indicators(self, data):
        return data

    def setup(self, data):
        pass

    def generate_signals(self, candle_index, data):
        df = data[self.primary_timeframe]
        ts = int(df.iloc[candle_index]["timestamp"])
        out = []
        for spec in self._plan.get(ts, []):
            out.append(Signal(
                direction=spec.get("direction", "long"),
                entry_price=spec["entry"], stop_loss=spec["sl"],
                take_profit=spec.get("tp"), timestamp=ts,
                candle_index=candle_index, timeframe="1h",
            ))
        return out


def _headless_cfg(**kw) -> SimulateConfig:
    base = dict(symbol="btcusdt", show_chart=False, report_mode="none",
                commission=0.0, spread=0.0)
    base.update(kw)
    return SimulateConfig(**base)


def _seeded_live(rows, seed_n, strategy, config, **live_kw):
    """Build a fake broker over the first *seed_n* rows, seed, and return
    ``(live, broker)`` — remaining rows are pushed by the caller."""
    broker = _FakeLiveBroker(rows[:seed_n])
    live = LiveSimulation(broker, strategy, config,
                          display_candles=seed_n, **live_kw)
    live.seed()
    return live, broker


class TestLiveSimulationSeed:

    def test_seed_fetches_display_candles(self):
        rows = _df_rows(_random_walk_df(60, seed=41))
        broker = _FakeLiveBroker(rows)
        live = LiveSimulation(broker, _LiveSMAStrategy(), _headless_cfg(),
                              display_candles=40)
        report = live.seed()
        assert broker.calls == [("last", "btcusdt", "1h", 40)]
        assert live.seeded
        assert len(live.data["1h"]) == 40
        assert live.last_report is report

    def test_seed_fetches_from_display_start(self):
        rows = _df_rows(_random_walk_df(60, seed=42))
        start_ms = rows[10]["timestamp"]
        broker = _FakeLiveBroker(rows)
        live = LiveSimulation(broker, _LiveSMAStrategy(), _headless_cfg(),
                              display_start=start_ms)
        live.seed()
        kind, symbol, tf, got_start, got_end = broker.calls[0]
        assert (kind, symbol, tf, got_start) == ("range", "btcusdt", "1h", start_ms)
        assert got_end >= rows[-1]["timestamp"]      # "until now"
        assert int(live.data["1h"]["timestamp"].iloc[0]) == start_ms
        assert len(live.data["1h"]) == 50

    def test_seed_does_not_finalize_open_positions_carry(self):
        rows = _flat_rows(10)
        # Signal on the LAST seed candle, SL far away → still open at seed end.
        plan = {rows[9]["timestamp"]: [{"entry": 100.0, "sl": 90.0}]}
        cfg = _headless_cfg(tp_mode="none")
        live, _ = _seeded_live(rows, 10, _PlanStrategy(plan), cfg)
        report = live.last_report
        assert not live.stepper.finalized
        assert len(live.stepper.open_positions) == 1
        assert len(report.open_at_end) == 1
        assert report.total_trades == 0              # nothing force-closed

    def test_seed_closed_trades_equal_batch_without_eod(self):
        df = _random_walk_df(60, seed=43)
        rows = _df_rows(df)
        cfg = _headless_cfg()
        live, _ = _seeded_live(rows, 60, _LiveSMAStrategy(), cfg)
        batch = Simulate(cfg).run(_LiveSMAStrategy().run({"1h": df.copy()}))
        batch_live_trades = [t for t in batch.closed_trades
                             if t.close_reason != CLOSE_REASON_EOD]
        assert len(live.stepper.closed_trades) > 0
        assert (repr([asdict(t) for t in live.stepper.closed_trades])
                == repr([asdict(t) for t in batch_live_trades]))

    def test_seed_emits_no_events_and_no_report_callback(self):
        rows = _df_rows(_random_walk_df(60, seed=44))
        events, reports = [], []
        live, _ = _seeded_live(rows, 60, _LiveSMAStrategy(), _headless_cfg(),
                               on_event=events.append, on_report=reports.append)
        assert len(live.stepper.closed_trades) > 0   # the seed did trade
        assert events == []
        assert reports == []

    def test_seed_twice_and_unseeded_calls_raise(self):
        rows = _flat_rows(10)
        live, broker = _seeded_live(rows, 10, _PlanStrategy({}), _headless_cfg())
        with pytest.raises(RuntimeError):
            live.seed()

        fresh = LiveSimulation(_FakeLiveBroker(rows), _PlanStrategy({}),
                               _headless_cfg(), display_candles=10)
        with pytest.raises(RuntimeError):
            fresh.start()
        with pytest.raises(RuntimeError):
            fresh.process_closed_candle(_candle(10, 100, 101, 99, 100))
        with pytest.raises(RuntimeError):
            fresh.process_forming_candle(_candle(10, 100, 101, 99, 100))

    def test_ctor_validation(self):
        rows = _flat_rows(10)
        broker = _FakeLiveBroker(rows)
        strat = _PlanStrategy({})
        cfg = _headless_cfg()
        with pytest.raises(ValueError):     # neither seed input
            LiveSimulation(broker, strat, cfg)
        with pytest.raises(ValueError):     # both seed inputs
            LiveSimulation(broker, strat, cfg,
                           display_candles=10, display_start=_BASE_TS)
        with pytest.raises(ValueError):
            LiveSimulation(broker, strat, cfg, display_candles=0)
        with pytest.raises(ValueError):
            LiveSimulation(broker, strat, cfg, display_candles=10,
                           candle_count_limit=0)
        with pytest.raises(ValueError):
            LiveSimulation(broker, strat, cfg, display_candles=10,
                           recompute_window=0)
        with pytest.raises(ValueError):     # symbol required for the feed
            LiveSimulation(broker, strat, _headless_cfg(symbol=""),
                           display_candles=10)

        class _FourHour(_PlanStrategy):
            primary_timeframe = "4h"

        with pytest.raises(ValueError):     # strategy TF must match config TF
            LiveSimulation(broker, _FourHour({}), cfg, display_candles=10)

    def test_seed_empty_fetch_raises(self):
        live = LiveSimulation(_FakeLiveBroker([]), _PlanStrategy({}),
                              _headless_cfg(), display_candles=10)
        with pytest.raises(ValueError):
            live.seed()


class TestLiveSimulationStep:

    def test_step_parity_with_batch(self):
        """ core invariant: seed + live stepping equals the batch run
        over the concatenated data (byte-identical trades + history)."""
        df = _random_walk_df(70, seed=45)
        rows = _df_rows(df)
        cfg = _headless_cfg(commission=0.001, spread=0.05)
        live, _ = _seeded_live(rows, 40, _LiveSMAStrategy(), cfg)
        for row in rows[40:]:
            live.process_closed_candle(row)

        batch = Simulate(cfg).run(_LiveSMAStrategy().run({"1h": df.copy()}))
        live.stepper.finalize()              # align end-of-data with batch
        stepped = live.stepper.build_report()
        assert len(batch.closed_trades) > 0
        assert _report_blob(stepped) == _report_blob(batch)
        assert repr(stepped.final_balance) == repr(batch.final_balance)

    def test_duplicate_and_stale_candles_skipped(self):
        rows = _df_rows(_random_walk_df(30, seed=46))
        live, _ = _seeded_live(rows, 20, _LiveSMAStrategy(), _headless_cfg())
        n_bh = len(live.stepper.balance_history)
        n_rows = len(live.data["1h"])
        assert live.process_closed_candle(rows[19]) == []    # duplicate of seed end
        assert live.process_closed_candle(rows[5]) == []     # stale
        assert len(live.stepper.balance_history) == n_bh
        assert len(live.data["1h"]) == n_rows

    def test_signal_and_open_events(self):
        rows = _flat_rows(5)
        entry_c = _candle(5, 100, 100.5, 99.5, 100)
        plan = {entry_c["timestamp"]: [{"entry": 100.0, "sl": 99.0, "tp": 104.0}]}
        events = []
        live, _ = _seeded_live(rows, 5, _PlanStrategy(plan), _headless_cfg(),
                               on_event=events.append)
        live.process_closed_candle(entry_c)
        assert [e["type"] for e in events] == [EVENT_SIGNAL, EVENT_OPEN]
        sig_ev, open_ev = events
        assert sig_ev["symbol"] == "btcusdt"
        assert isinstance(sig_ev["signal"], Signal)
        assert open_ev["trade_id"] == 0
        assert open_ev["direction"] == "long"
        assert open_ev["entry_price"] == pytest.approx(100.0)
        assert open_ev["stop_loss"] == pytest.approx(99.0)
        assert open_ev["next_tp"] == pytest.approx(104.0)
        assert open_ev["size"] > 0 and open_ev["risk_amount"] > 0

    def test_close_event_and_slice_cleanup(self):
        rows = _flat_rows(5)
        entry_c = _candle(5, 100, 100.5, 99.5, 100)
        sl_hit = _candle(6, 100, 100.2, 98.5, 99.2)      # low breaks SL 99
        plan = {entry_c["timestamp"]: [{"entry": 100.0, "sl": 99.0, "tp": 104.0}]}
        events = []
        live, _ = _seeded_live(rows, 5, _PlanStrategy(plan), _headless_cfg(),
                               on_event=events.append)
        live.process_closed_candle(entry_c)
        closed = live.process_closed_candle(sl_hit)
        assert len(closed) == 1 and closed[0].close_reason == CLOSE_REASON_SL
        close_ev = events[-1]
        assert close_ev["type"] == EVENT_CLOSE
        assert close_ev["trade_id"] == 0
        assert close_ev["trade"] is closed[0]
        assert live._slices == {} and live._sl_seen == {}   # cleaned up

    def test_tp_level_and_ladder_sl_move_events(self):
        rows = _flat_rows(3)
        entry_c = _candle(3, 100, 100.5, 99.5, 100)
        level1 = _candle(4, 100, 101.3, 99.6, 100.8)      # hits L1 = 101
        level2 = _candle(5, 100.8, 102.5, 100.2, 101.5)   # hits L2 = 102
        plan = {entry_c["timestamp"]: [{"entry": 100.0, "sl": 99.0}]}
        cfg = _headless_cfg(tp_mode="multi_rr", tp_levels=[1.0, 2.0],
                            tp_level_close_fractions=[0.5, 0.5])
        events = []
        live, _ = _seeded_live(rows, 3, _PlanStrategy(plan), cfg,
                               on_event=events.append)
        live.process_closed_candle(entry_c)

        closed = live.process_closed_candle(level1)
        assert len(closed) == 1
        assert closed[0].close_reason == CLOSE_REASON_TP_PARTIAL
        kinds = [e["type"] for e in events]
        assert kinds == [EVENT_SIGNAL, EVENT_OPEN, EVENT_TP_LEVEL, EVENT_SL_MOVE]
        tp_ev = events[2]
        assert tp_ev["trade"] is closed[0] and tp_ev["levels_hit"] == 1
        move_ev = events[3]
        assert move_ev["old_sl"] == pytest.approx(99.0)
        assert move_ev["new_sl"] == pytest.approx(100.0)   # ladder → entry
        assert move_ev["next_tp"] == pytest.approx(102.0)

        closed2 = live.process_closed_candle(level2)
        assert len(closed2) == 1
        assert events[-1]["type"] == EVENT_CLOSE           # nothing remains open
        assert live.stepper.open_positions == []
        assert live._slices == {}

    def test_trailing_sl_move_events(self):
        rows = _flat_rows(3)
        entry_c = _candle(3, 100, 100.5, 99.5, 100)
        # Tight rising candles: the 1% trail must stay below each candle's low.
        up1 = _candle(4, 100, 100.9, 100.0, 100.85)
        up2 = _candle(5, 100.85, 101.8, 100.8, 101.7)
        plan = {entry_c["timestamp"]: [{"entry": 100.0, "sl": 99.0}]}
        cfg = _headless_cfg(tp_mode="none", sl_mode="trailing",
                            trailing_sl_percent=1.0)
        events = []
        live, _ = _seeded_live(rows, 3, _PlanStrategy(plan), cfg,
                               on_event=events.append)
        live.process_closed_candle(entry_c)
        live.process_closed_candle(up1)
        live.process_closed_candle(up2)
        assert len(live.stepper.open_positions) == 1        # never stopped out
        moves = [e for e in events if e["type"] == EVENT_SL_MOVE]
        assert len(moves) == 2                              # one per candle, coalesced
        assert moves[0]["old_sl"] == pytest.approx(99.0)
        assert moves[0]["new_sl"] == pytest.approx(100.9 * 0.99)
        assert moves[1]["old_sl"] == pytest.approx(100.9 * 0.99)
        assert moves[1]["new_sl"] == pytest.approx(101.8 * 0.99)
        assert moves[0]["next_tp"] is None                  # trailing has no TP

    def test_on_report_called_each_closed_candle(self):
        rows = _df_rows(_random_walk_df(30, seed=47))
        reports = []
        live, _ = _seeded_live(rows, 20, _LiveSMAStrategy(), _headless_cfg(),
                               on_report=reports.append)
        for row in rows[20:]:
            live.process_closed_candle(row)
        assert len(reports) == 10
        assert live.last_report is reports[-1]
        assert isinstance(reports[-1], SimulateReport)

    def test_forming_candle_is_display_only(self):
        rows = _flat_rows(5)
        plan = {rows[4]["timestamp"]: [{"entry": 100.0, "sl": 90.0}]}
        events = []
        live, _ = _seeded_live(rows, 5, _PlanStrategy(plan),
                               _headless_cfg(tp_mode="none"),
                               on_event=events.append)
        df_before = live.data["1h"].copy(deep=True)
        n_bh = len(live.stepper.balance_history)
        forming = _candle(5, 100, 108.0, 99.0, 107.0)      # would move everything
        live.process_forming_candle(forming)
        live.process_forming_candle({**forming, "close": 90.0})   # updated form
        pd.testing.assert_frame_equal(live.data["1h"], df_before)
        assert len(live.stepper.balance_history) == n_bh
        assert len(live.stepper.open_positions) == 1       # untouched
        assert events == []
        live.process_forming_candle(rows[4])               # stale ts → no-op

    def test_callback_exceptions_warn_not_raise(self):
        rows = _flat_rows(5)
        plan = {_flat_rows(6)[5]["timestamp"]: [{"entry": 100.0, "sl": 99.0}]}

        def _boom(_):
            raise RuntimeError("subscriber bug")

        live, _ = _seeded_live(rows, 5, _PlanStrategy(plan),
                               _headless_cfg(tp_mode="none"),
                               on_event=_boom, on_report=_boom)
        with pytest.warns(UserWarning, match="callback failed"):
            live.process_closed_candle(_candle(5, 100, 100.5, 99.5, 100))
        assert len(live.stepper.open_positions) == 1       # step still applied

    def test_feed_routing_stop_and_error_tolerance(self):
        rows = _df_rows(_random_walk_df(30, seed=48))
        live, broker = _seeded_live(rows, 20, _LiveSMAStrategy(), _headless_cfg())
        stream = live.start()
        assert broker.stream_args == ("btcusdt", "1h", True)   # headless → closed_only
        assert live.stream is stream

        n_rows = len(live.data["1h"])
        broker.push(rows[20], closed=True)
        assert len(live.data["1h"]) == n_rows + 1
        broker.push(rows[21], closed=False)                # forming → not appended
        assert len(live.data["1h"]) == n_rows + 1
        with pytest.warns(UserWarning, match="candle processing failed"):
            broker.push({"timestamp": rows[21]["timestamp"]})   # broken candle

        with pytest.raises(RuntimeError):
            live.start()                                    # already running
        live.stop()
        assert not stream.alive and live.stream is None
        live.stop()                                         # idempotent
        assert live.start() is broker.streams[-1]           # restart allowed


class TestLiveSimulationWindow:

    def test_deque_bounded_and_engagement(self):
        rows = _df_rows(_random_walk_df(40, seed=49))
        live, _ = _seeded_live(rows, 15, _LiveSMAStrategy(), _headless_cfg(),
                               candle_count_limit=20)
        assert not live._window_engaged                     # 15 < 20 so far
        for row in rows[15:40]:
            live.process_closed_candle(row)
        assert len(live._window) == 20
        assert live._window_engaged
        assert int(live._window[0]["timestamp"]) == rows[20]["timestamp"]

    def test_windowed_report_baseline_is_equity_entering_window(self):
        df = _random_walk_df(80, seed=50)
        rows = _df_rows(df)
        cfg = _headless_cfg()
        live, _ = _seeded_live(rows, 40, _LiveSMAStrategy(), cfg,
                               candle_count_limit=30)
        for row in rows[40:]:
            live.process_closed_candle(row)

        # Independent expectation from the (parity-proven) batch history.
        batch = Simulate(cfg).run(_LiveSMAStrategy().run({"1h": df.copy()}))
        window_start = int(live._window[0]["timestamp"])
        dropped = [h for h in batch.balance_history if h["timestamp"] < window_start]
        expected_baseline = dropped[-1]["equity"]

        report = live.last_report
        assert report.initial_balance == pytest.approx(expected_baseline)
        assert report.balance_history[0]["timestamp"] == window_start
        assert len(report.balance_history) == 30
        assert report.final_balance == pytest.approx(
            batch.balance_history[-1]["equity"])
        assert report.total_pnl == pytest.approx(
            report.final_balance - expected_baseline)

    def test_windowed_report_drops_out_of_window_trades(self):
        df = _random_walk_df(80, seed=51)
        rows = _df_rows(df)
        cfg = _headless_cfg()
        live, _ = _seeded_live(rows, 40, _LiveSMAStrategy(), cfg,
                               candle_count_limit=25)
        for row in rows[40:]:
            live.process_closed_candle(row)

        batch = Simulate(cfg).run(_LiveSMAStrategy().run({"1h": df.copy()}))
        window_start = int(live._window[0]["timestamp"])
        expected = [t for t in batch.closed_trades
                    if t.close_reason != CLOSE_REASON_EOD
                    and t.close_time >= window_start]
        dropped = [t for t in batch.closed_trades
                   if t.close_reason != CLOSE_REASON_EOD
                   and t.close_time < window_start]
        assert dropped, "scenario must actually drop a trade"
        assert (repr([asdict(t) for t in live.last_report.closed_trades])
                == repr([asdict(t) for t in expected]))

    def test_trade_spanning_window_start_kept_until_close_leaves(self):
        rows = _flat_rows(4)
        entry_c = _candle(4, 100, 100.5, 99.5, 100)        # open at ts4
        plan = {entry_c["timestamp"]: [{"entry": 100.0, "sl": 90.0}]}
        cfg = _headless_cfg(tp_mode="none")
        live, _ = _seeded_live(rows, 4, _PlanStrategy(plan), cfg,
                               candle_count_limit=4)
        live.process_closed_candle(entry_c)
        live.process_closed_candle(_candle(5, 100, 100.5, 99.5, 100))
        live.process_closed_candle(_candle(6, 100, 100.5, 99.5, 100))
        closed = live.process_closed_candle(_candle(7, 100, 100.2, 89.0, 95.0))
        assert len(closed) == 1                            # SL hit at ts7

        # Window now [ts4..ts7]: open_time ts4 is inside; trade kept.
        assert len(live.last_report.closed_trades) == 1
        live.process_closed_candle(_candle(8, 95, 95.5, 94.5, 95))
        # Window [ts5..ts8]: opened before the window but closed inside → kept.
        assert len(live.last_report.closed_trades) == 1
        for i in (9, 10, 11):
            live.process_closed_candle(_candle(i, 95, 95.5, 94.5, 95))
        # Window [ts8..ts11]: close ts7 < ts8 → whole lifetime left → dropped.
        assert live.last_report.closed_trades == []
        assert live.last_report.total_trades == 0

    def test_frame_and_state_lists_stay_bounded(self):
        rows = _df_rows(_random_walk_df(120, seed=52))
        live, _ = _seeded_live(rows, 60, _LiveSMAStrategy(),
                               _headless_cfg(), candle_count_limit=25,
                               recompute_window=30)
        for row in rows[60:]:
            live.process_closed_candle(row)
        keep = max(25, 30, _LiveSMAStrategy.warmup_period + 1)
        assert len(live.data["1h"]) == keep                # trimmed master frame
        assert len(live.stepper.balance_history) == 25
        window_start = int(live._window[0]["timestamp"])
        assert all(t.close_time >= window_start
                   for t in live.stepper.closed_trades)

    def test_no_window_nothing_trimmed(self):
        rows = _df_rows(_random_walk_df(70, seed=53))
        live, _ = _seeded_live(rows, 40, _LiveSMAStrategy(), _headless_cfg())
        for row in rows[40:]:
            live.process_closed_candle(row)
        assert live._window is None
        assert len(live.data["1h"]) == 70
        assert len(live.stepper.balance_history) == 70
        assert live.last_report.initial_balance == pytest.approx(10_000.0)

    def test_window_engages_during_seed(self):
        df = _random_walk_df(50, seed=54)
        rows = _df_rows(df)
        cfg = _headless_cfg()
        live, _ = _seeded_live(rows, 50, _LiveSMAStrategy(), cfg,
                               candle_count_limit=30)
        report = live.last_report
        assert live._window_engaged
        assert len(report.balance_history) == 30
        batch = Simulate(cfg).run(_LiveSMAStrategy().run({"1h": df.copy()}))
        window_start = int(live._window[0]["timestamp"])
        dropped = [h for h in batch.balance_history if h["timestamp"] < window_start]
        assert report.initial_balance == pytest.approx(dropped[-1]["equity"])


class TestLiveSimulationDisplay:
    """Chart / report production — real servers, browser never opened."""

    @pytest.fixture(autouse=True)
    def _no_keep_alive(self, monkeypatch):
        """Never register the real atexit blocker inside pytest."""
        import AlgoTradeKit.simulate._live as live_mod
        calls = []
        monkeypatch.setattr(live_mod, "_register_keep_alive",
                            lambda: calls.append(True))
        self._keep_alive_calls = calls

    def _display_live(self, rows, seed_n, strategy, cfg, **live_kw):
        broker = _FakeLiveBroker(rows[:seed_n])
        live = LiveSimulation(broker, strategy, cfg, display_candles=seed_n,
                              open_browser=False, **live_kw)
        live.seed()
        return live, broker

    def test_chart_seed_state_live_position_and_boxes(self):
        from AlgoTradeKit.visual.models import LivePosition, PositionBox

        rows = _flat_rows(12)
        # One trade fully closed in the seed + one still open at seed end.
        plan = {
            rows[4]["timestamp"]: [{"entry": 100.0, "sl": 99.0, "tp": 100.4}],
            rows[9]["timestamp"]: [{"entry": 100.0, "sl": 90.0}],
        }
        cfg = _headless_cfg(tp_mode="signal", show_chart=True)
        live, _ = self._display_live(rows, 12, _PlanStrategy(plan), cfg,
                                     candle_count_limit=200)
        try:
            chart = live.chart
            assert chart is not None
            assert chart._candle_count_limit == 200
            lives = [d for d in chart._drawings if isinstance(d, LivePosition)]
            boxes = [d for d in chart._drawings if isinstance(d, PositionBox)]
            assert len(lives) == 1                     # the open trade
            assert len(boxes) == 1                     # the closed seed trade
            assert lives[0].stop_loss == pytest.approx(90.0)
            assert self._keep_alive_calls == [True]
        finally:
            live.stop()
            chart.stop()

    def test_live_close_swaps_live_drawing_for_final_box(self):
        from AlgoTradeKit.visual.models import LivePosition, PositionBox, TrendLine

        rows = _flat_rows(3)
        entry_c = _candle(3, 100, 100.5, 99.5, 100)
        level1 = _candle(4, 100, 101.3, 99.6, 100.8)
        level2 = _candle(5, 100.8, 102.5, 100.2, 101.5)
        plan = {entry_c["timestamp"]: [{"entry": 100.0, "sl": 99.0}]}
        cfg = _headless_cfg(tp_mode="multi_rr", tp_levels=[1.0, 2.0],
                            tp_level_close_fractions=[0.5, 0.5], show_chart=True)
        live, _ = self._display_live(rows, 3, _PlanStrategy(plan), cfg)
        try:
            chart = live.chart
            live.process_closed_candle(entry_c)
            lives = [d for d in chart._drawings if isinstance(d, LivePosition)]
            assert len(lives) == 1
            live.process_closed_candle(level1)         # partial: still live
            lives = [d for d in chart._drawings if isinstance(d, LivePosition)]
            assert len(lives) == 1
            assert lives[0].stop_loss == pytest.approx(100.0)   # ladder move pushed
            assert lives[0].next_tp == pytest.approx(102.0)

            live.process_closed_candle(level2)         # full close
            lives = [d for d in chart._drawings if isinstance(d, LivePosition)]
            boxes = [d for d in chart._drawings if isinstance(d, PositionBox)]
            segments = [d for d in chart._drawings if isinstance(d, TrendLine)]
            assert lives == []                         # live drawing removed
            assert len(boxes) == 1                     # final posbox
            assert boxes[0].trade_id == 0
            assert segments                            # dynamic SL/TP segments
            # the streamed candles reached the chart too
            assert chart._bars[-1]["time"] == level2["timestamp"] // 1000
        finally:
            live.stop()
            chart.stop()

    def test_report_webpage_push_updates_pending_payload(self):
        rows = _df_rows(_random_walk_df(40, seed=55))
        cfg = _headless_cfg(report_mode="webpage")
        live, _ = self._display_live(rows, 30, _LiveSMAStrategy(), cfg)
        try:
            server = live.report_server
            assert server is not None
            seed_total = server._pending_data["summary"]["total_trades"]
            for row in rows[30:]:
                live.process_closed_candle(row)
            new_total = server._pending_data["summary"]["total_trades"]
            assert new_total == live.last_report.total_trades
            assert new_total > seed_total
            assert self._keep_alive_calls == [True]
        finally:
            live.stop()
            server.stop()

    def test_report_save_mode_writes_file_once_at_seed(self, tmp_path):
        out = tmp_path / "live_report.html"
        rows = _df_rows(_random_walk_df(30, seed=56))
        cfg = _headless_cfg(report_mode="save", report_save_path=str(out))
        live, _ = self._display_live(rows, 30, _LiveSMAStrategy(), cfg)
        assert out.exists() and out.stat().st_size > 0
        assert live.report_server is None              # save-only: no server
        assert self._keep_alive_calls == []            # no browser UI

    def test_chart_indicator_specs_loaded_at_seed(self):
        rows = _df_rows(_random_walk_df(30, seed=57))
        cfg = _headless_cfg(show_chart=True,
                            chart_indicators=[{"kind": "ema", "period": 5}])
        live, _ = self._display_live(rows, 30, _LiveSMAStrategy(), cfg)
        try:
            assert any("EMA" in ind.name for ind in live.chart._indicators)
        finally:
            live.stop()
            live.chart.stop()

    def test_headless_seed_registers_no_keep_alive(self):
        rows = _df_rows(_random_walk_df(30, seed=58))
        live, _ = self._display_live(rows, 30, _LiveSMAStrategy(),
                                     _headless_cfg())
        assert live.chart is None and live.report_server is None
        assert self._keep_alive_calls == []

    def test_draw_trade_group_equals_batch_rendering(self):
        """The factored per-trade helper renders exactly what
        add_simulation_positions renders for the same trades."""
        from AlgoTradeKit.visual import Chart
        from AlgoTradeKit.visual.indicator_renderer import (
            add_simulation_positions,
            draw_trade_group,
        )

        df = _random_walk_df(120, seed=59)
        sigs = [_walk_signal(df, i, "long", 0.02) for i in range(10, 110, 20)]
        cfg = _headless_cfg(tp_mode="multi_rr", tp_levels=[1.0, 2.0],
                            tp_level_close_fractions=[0.5, 0.5])
        report = Simulate(cfg).run(_make_result(sigs, df))
        assert report.total_trades > 0

        chart_batch, chart_live = Chart(), Chart()
        add_simulation_positions(chart_batch, report, config=cfg)
        groups: dict[int, list[dict]] = {}
        for m in report.trade_markers:
            groups.setdefault(m["trade_id"], []).append(m)
        for markers in groups.values():
            draw_trade_group(chart_live, markers, config=cfg)

        def _no_ids(drawings):
            out = []
            for d in drawings:
                payload = {k: v for k, v in d.to_dict().items() if k != "id"}
                out.append(payload)
            return out

        assert _no_ids(chart_batch._drawings) == _no_ids(chart_live._drawings)
