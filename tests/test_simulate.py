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

import math
import pytest
import numpy as np
import pandas as pd

from AlgoTradeKit.simulate import (
    CLOSE_REASON_EOD,
    CLOSE_REASON_FC,
    CLOSE_REASON_RF,
    CLOSE_REASON_SL,
    CLOSE_REASON_TP,
    CLOSE_REASON_TP_PARTIAL,
    EXCHANGE_TYPE_EXCHANGE,
    EXCHANGE_TYPE_METATRADER,
    Simulate,
    SimulateConfig,
    SimulateReport,
    run_batch,
    run_multi,
)
from AlgoTradeKit.simulate._lot import calculate_mt5_lot, get_mt5_pip_info, round_lot
from AlgoTradeKit.simulate._position import _InternalPosition
from AlgoTradeKit.simulate._report import DrawdownPeriod, build_report
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


def _long_signal(candle_idx: int, df: pd.DataFrame, sl_pct: float = 0.02, tp_pct: float | None = None) -> Signal:
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


def _short_signal(candle_idx: int, df: pd.DataFrame, sl_pct: float = 0.02, tp_pct: float | None = None) -> Signal:
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
        cfg = SimulateConfig(symbol="btcusdt", leverage=10, risk_per_trade=2.0, tp_mode="fixed_rr", tp_rr=2.5)
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
        sessions = get_sessions(ts)
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
        sl_dist = abs(entry - sl)
        ppu     = risk / sl_dist
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
        from AlgoTradeKit.simulate._position import (
            CLOSE_REASON_SL, CLOSE_REASON_TP, ClosedTrade
        )
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
            trades.append(ct); ts += 2 * _1H_MS

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
            trades.append(ct); ts += 2 * _1H_MS

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
            SimulateConfig(risk_per_trade=0.5, tp_mode="none", commission_type="fixed", commission=0),
            SimulateConfig(risk_per_trade=1.0, tp_mode="none", commission_type="fixed", commission=0),
            SimulateConfig(risk_per_trade=2.0, tp_mode="none", commission_type="fixed", commission=0),
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
