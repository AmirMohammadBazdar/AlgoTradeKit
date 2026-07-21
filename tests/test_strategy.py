"""
tests/test_strategy.py
~~~~~~~~~~~~~~~~~~~~~~~
Full test suite for AlgoTradeKit.strategy module.

Tests cover
-----------
- _types       : Signal, ExitSignal, StrategyResult, StrategyMode
- _base        : BaseStrategy lifecycle, helpers, validation, BACKTEST vs LIVE
- builtin      : MACDCrossoverStrategy indicators, crossovers, SL, exit signals
- _incremental : update_indicators hook, advance_live_candle (hook + tail
                 recompute fallback), evaluate_forming_candle (v1.0.0)
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from AlgoTradeKit.indicator import EMA, MACD
from AlgoTradeKit.strategy import (
    BaseStrategy,
    ExitSignal,
    MACDCrossoverStrategy,
    Signal,
    StrategyMode,
    StrategyResult,
    advance_live_candle,
    default_recompute_window,
    evaluate_forming_candle,
    has_update_hook,
)

# ===========================================================================
# Test fixtures / helpers
# ===========================================================================

def _make_df(n: int = 200, seed: int = 0) -> pd.DataFrame:
    """Deterministic synthetic OHLCV DataFrame with 1h timestamps."""
    rng = np.random.default_rng(seed)
    close = 100.0 + np.cumsum(rng.normal(0, 0.5, n))
    close = np.abs(close) + 10          # keep prices positive
    spread = 0.3
    return pd.DataFrame({
        "timestamp": [1_609_459_200_000 + i * 3_600_000 for i in range(n)],
        "open":   close - spread * 0.5,
        "high":   close + spread,
        "low":    close - spread,
        "close":  close,
        "volume": rng.integers(1_000, 5_000, n).astype(float),
    })


def _make_multi_tf(n_primary: int = 200, seed: int = 0) -> dict[str, pd.DataFrame]:
    """Two-timeframe dict: primary '1h' + higher '4h'."""
    df_1h = _make_df(n_primary, seed)
    # 4h: every 4th row, resampled simply
    df_4h = _make_df(n_primary // 4, seed + 1)
    df_4h["timestamp"] = [1_609_459_200_000 + i * 4 * 3_600_000 for i in range(len(df_4h))]
    return {"1h": df_1h, "4h": df_4h}


# Minimal concrete BaseStrategy implementation for framework tests
class _PassthroughStrategy(BaseStrategy):
    """Strategy that collects all candles it sees; never emits signals."""
    primary_timeframe = "1h"

    def __init__(self):
        self.setup_called = False
        self.candles_seen: list[int] = []
        self.exits_seen: list[int] = []

    def prepare_indicators(self, data):
        df = data["1h"].copy()
        df["_dummy"] = 1.0
        data["1h"] = df
        return data

    def setup(self, data):
        self.setup_called = True
        self.candles_seen = []
        self.exits_seen = []

    def generate_signals(self, i, data):
        self.candles_seen.append(i)
        return []

    def detect_exit_signals(self, i, data):
        self.exits_seen.append(i)
        return []


class _AlwaysSignalStrategy(BaseStrategy):
    """Strategy that emits one Signal per candle (for testing LIVE mode)."""
    primary_timeframe = "1h"

    def prepare_indicators(self, data):
        return data

    def generate_signals(self, i, data):
        candle = data["1h"].iloc[i]
        return [Signal(
            direction="long",
            entry_price=float(candle["close"]),
            stop_loss=float(candle["close"]) - 1.0,
            take_profit=None,
            timestamp=int(candle["timestamp"]),
            candle_index=i,
            timeframe="1h",
        )]


class _ExitStrategy(BaseStrategy):
    """Strategy that emits one ExitSignal per candle."""
    primary_timeframe = "1h"

    def prepare_indicators(self, data):
        return data

    def generate_signals(self, i, data):
        return []

    def detect_exit_signals(self, i, data):
        candle = data["1h"].iloc[i]
        return [ExitSignal(
            reason="test_exit",
            exit_price=float(candle["close"]),
            timestamp=int(candle["timestamp"]),
            candle_index=i,
        )]


# ===========================================================================
# 1. _types — Signal
# ===========================================================================

class TestSignal:
    def test_long_signal_creation(self):
        s = Signal("long", 100.0, 95.0, 110.0, 1_000_000, 5, "1h")
        assert s.direction == "long"
        assert s.entry_price == 100.0
        assert s.stop_loss == 95.0
        assert s.take_profit == 110.0
        assert s.timestamp == 1_000_000
        assert s.candle_index == 5
        assert s.timeframe == "1h"
        assert s.metadata == {}

    def test_short_signal_creation(self):
        s = Signal("short", 100.0, 105.0, 90.0, 1_000_000, 5, "4h")
        assert s.direction == "short"
        assert s.is_short

    def test_long_signal_is_long(self):
        s = Signal("long", 100.0, 95.0, None, 0, 0, "1h")
        assert s.is_long
        assert not s.is_short

    def test_invalid_direction_raises(self):
        with pytest.raises(ValueError, match="direction must be"):
            Signal("buy", 100.0, 95.0, None, 0, 0, "1h")

    def test_sl_equals_entry_raises(self):
        with pytest.raises(ValueError, match="stop_loss must differ"):
            Signal("long", 100.0, 100.0, None, 0, 0, "1h")

    def test_sl_distance_long(self):
        s = Signal("long", 100.0, 95.0, None, 0, 0, "1h")
        assert s.sl_distance == pytest.approx(5.0)

    def test_sl_distance_short(self):
        s = Signal("short", 100.0, 105.0, None, 0, 0, "1h")
        assert s.sl_distance == pytest.approx(5.0)

    def test_risk_reward_with_tp(self):
        s = Signal("long", 100.0, 90.0, 120.0, 0, 0, "1h")
        # TP distance = 20, SL distance = 10 → RR = 2.0
        assert s.risk_reward == pytest.approx(2.0)

    def test_risk_reward_without_tp(self):
        s = Signal("long", 100.0, 90.0, None, 0, 0, "1h")
        assert s.risk_reward is None

    def test_metadata_default_is_empty_dict(self):
        s = Signal("long", 100.0, 95.0, None, 0, 0, "1h")
        assert isinstance(s.metadata, dict)
        assert len(s.metadata) == 0

    def test_metadata_can_be_set(self):
        s = Signal("long", 100.0, 95.0, None, 0, 0, "1h", metadata={"rr": 2.5})
        assert s.metadata["rr"] == 2.5

    def test_repr_contains_direction(self):
        s = Signal("long", 100.0, 95.0, None, 0, 0, "1h")
        assert "LONG" in repr(s)

    def test_repr_short(self):
        s = Signal("short", 100.0, 105.0, None, 0, 0, "1h")
        assert "SHORT" in repr(s)

    def test_risk_multiplier_default_is_one(self):
        s = Signal("long", 100.0, 95.0, None, 0, 0, "1h")
        assert s.risk_multiplier == 1.0

    def test_risk_multiplier_can_be_set(self):
        s = Signal("long", 100.0, 95.0, None, 0, 0, "1h", risk_multiplier=0.25)
        assert s.risk_multiplier == 0.25

    def test_risk_multiplier_zero_raises(self):
        with pytest.raises(ValueError, match="risk_multiplier must be > 0"):
            Signal("long", 100.0, 95.0, None, 0, 0, "1h", risk_multiplier=0.0)

    def test_risk_multiplier_negative_raises(self):
        with pytest.raises(ValueError, match="risk_multiplier must be > 0"):
            Signal("long", 100.0, 95.0, None, 0, 0, "1h", risk_multiplier=-0.5)

    def test_risk_multiplier_does_not_break_positional_construction(self):
        # 7 positional args (the pre-v0.7.3 shape) must still work unchanged.
        s = Signal("long", 100.0, 95.0, 110.0, 1_000_000, 5, "1h")
        assert s.risk_multiplier == 1.0


# ===========================================================================
# 2. _types — ExitSignal
# ===========================================================================

class TestExitSignal:
    def test_creation(self):
        e = ExitSignal("trend_reversal", 99.5, 1_000_000, 10)
        assert e.reason == "trend_reversal"
        assert e.exit_price == pytest.approx(99.5)
        assert e.timestamp == 1_000_000
        assert e.candle_index == 10

    def test_none_exit_price(self):
        e = ExitSignal("force_close", None, 0, 0)
        assert e.exit_price is None

    def test_metadata(self):
        e = ExitSignal("trailing", 99.0, 0, 0, {"bar": 1})
        assert e.metadata["bar"] == 1

    def test_repr(self):
        e = ExitSignal("reversal", 50.0, 0, 0)
        assert "reversal" in repr(e)
        assert "50." in repr(e)

    def test_repr_market_price(self):
        e = ExitSignal("force_close", None, 0, 0)
        assert "market" in repr(e)


# ===========================================================================
# 3. _types — StrategyResult
# ===========================================================================

class TestStrategyResult:
    def _make_result(self, n_long=2, n_short=1, n_exits=1):
        signals = (
            [Signal("long", 100.0, 95.0, None, i, i, "1h") for i in range(n_long)]
            + [Signal("short", 100.0, 105.0, None, 10 + i, 10 + i, "1h") for i in range(n_short)]
        )
        exits = [ExitSignal("test", 100.0, i, i) for i in range(n_exits)]
        return StrategyResult(signals, exits, {}, StrategyMode.BACKTEST)

    def test_signal_count(self):
        r = self._make_result(n_long=3, n_short=2)
        assert r.signal_count == 5

    def test_long_signals(self):
        r = self._make_result(n_long=3, n_short=2)
        assert len(r.long_signals) == 3

    def test_short_signals(self):
        r = self._make_result(n_long=3, n_short=2)
        assert len(r.short_signals) == 2

    def test_has_signal_true(self):
        r = self._make_result(n_long=1)
        assert r.has_signal

    def test_has_signal_false(self):
        r = StrategyResult([], [], {}, StrategyMode.BACKTEST)
        assert not r.has_signal

    def test_repr(self):
        r = self._make_result()
        assert "backtest" in repr(r)


# ===========================================================================
# 4. _types — StrategyMode
# ===========================================================================

class TestStrategyMode:
    def test_backtest_value(self):
        assert StrategyMode.BACKTEST.value == "backtest"

    def test_live_value(self):
        assert StrategyMode.LIVE.value == "live"

    def test_enum_members(self):
        assert set(StrategyMode) == {StrategyMode.BACKTEST, StrategyMode.LIVE}


# ===========================================================================
# 5. BaseStrategy — abstract enforcement
# ===========================================================================

class TestBaseStrategyAbstract:
    def test_cannot_instantiate_base(self):
        with pytest.raises(TypeError):
            BaseStrategy()

    def test_missing_prepare_indicators_raises(self):
        class _Bad(BaseStrategy):
            primary_timeframe = "1h"
            def generate_signals(self, i, data): return []

        with pytest.raises(TypeError):
            _Bad()

    def test_missing_generate_signals_raises(self):
        class _Bad(BaseStrategy):
            primary_timeframe = "1h"
            def prepare_indicators(self, data): return data

        with pytest.raises(TypeError):
            _Bad()

    def test_valid_subclass_can_be_instantiated(self):
        s = _PassthroughStrategy()
        assert s is not None


# ===========================================================================
# 6. BaseStrategy — lifecycle
# ===========================================================================

class TestBaseStrategyLifecycle:
    def test_prepare_indicators_called(self):
        strat = _PassthroughStrategy()
        df = _make_df(50)
        result = strat.run(df)
        # _dummy column must have been added
        assert "_dummy" in result.data["1h"].columns

    def test_setup_called_once(self):
        strat = _PassthroughStrategy()
        df = _make_df(50)
        strat.run(df)
        assert strat.setup_called

    def test_generate_signals_called_for_every_candle(self):
        strat = _PassthroughStrategy()
        df = _make_df(50)
        strat.run(df)
        # Should be called for all 50 candles (warmup_period=0)
        assert len(strat.candles_seen) == 50
        assert strat.candles_seen == list(range(50))

    def test_detect_exit_signals_called_for_every_candle(self):
        strat = _PassthroughStrategy()
        df = _make_df(50)
        strat.run(df)
        assert len(strat.exits_seen) == 50

    def test_setup_resets_state_on_second_run(self):
        strat = _PassthroughStrategy()
        df = _make_df(50)
        strat.run(df)
        first_run_count = len(strat.candles_seen)
        strat.run(df)
        second_run_count = len(strat.candles_seen)
        # setup() should reset candles_seen; second run should start fresh
        assert second_run_count == first_run_count


# ===========================================================================
# 7. BaseStrategy — run modes
# ===========================================================================

class TestBaseStrategyModes:
    def test_backtest_returns_all_signals(self):
        strat = _AlwaysSignalStrategy()
        df = _make_df(80)
        result = strat.run(df, mode=StrategyMode.BACKTEST)
        # One signal per candle
        assert result.signal_count == 80

    def test_live_returns_only_last_candle_signal(self):
        strat = _AlwaysSignalStrategy()
        df = _make_df(80)
        result = strat.run(df, mode=StrategyMode.LIVE)
        assert result.signal_count == 1
        last_ts = int(df.iloc[-1]["timestamp"])
        assert result.signals[0].timestamp == last_ts

    def test_backtest_mode_stored_in_result(self):
        strat = _PassthroughStrategy()
        result = strat.run(_make_df(30), mode=StrategyMode.BACKTEST)
        assert result.mode is StrategyMode.BACKTEST

    def test_live_mode_stored_in_result(self):
        strat = _PassthroughStrategy()
        result = strat.run(_make_df(30), mode=StrategyMode.LIVE)
        assert result.mode is StrategyMode.LIVE

    def test_live_exit_signals_filtered_to_last_candle(self):
        strat = _ExitStrategy()
        df = _make_df(50)
        result = strat.run(df, mode=StrategyMode.LIVE)
        assert len(result.exit_signals) == 1
        last_ts = int(df.iloc[-1]["timestamp"])
        assert result.exit_signals[0].timestamp == last_ts

    def test_backtest_collects_exit_signals_from_all_candles(self):
        strat = _ExitStrategy()
        df = _make_df(50)
        result = strat.run(df, mode=StrategyMode.BACKTEST)
        assert len(result.exit_signals) == 50


# ===========================================================================
# 8. BaseStrategy — input handling
# ===========================================================================

class TestBaseStrategyInput:
    def test_plain_dataframe_wrapped_automatically(self):
        strat = _PassthroughStrategy()
        df = _make_df(50)
        result = strat.run(df)   # no dict wrapper
        assert "1h" in result.data

    def test_dict_input_accepted(self):
        strat = _PassthroughStrategy()
        data = {"1h": _make_df(50)}
        result = strat.run(data)
        assert "1h" in result.data

    def test_wrong_primary_timeframe_raises(self):
        strat = _PassthroughStrategy()
        with pytest.raises(ValueError, match="primary_timeframe"):
            strat.run({"4h": _make_df(50)})   # strat expects "1h"

    def test_missing_required_column_raises(self):
        strat = _PassthroughStrategy()
        df = _make_df(50).drop(columns=["volume"])
        with pytest.raises(ValueError, match="missing required columns"):
            strat.run(df)

    def test_non_dataframe_value_raises(self):
        strat = _PassthroughStrategy()
        with pytest.raises(TypeError):
            strat.run({"1h": "not_a_dataframe"})

    def test_empty_dataframe_raises(self):
        strat = _PassthroughStrategy()
        empty = _make_df(50).iloc[0:0]  # empty
        with pytest.raises(ValueError, match="empty"):
            strat.run({"1h": empty})

    def test_multi_timeframe_dict_accepted(self):
        strat = _PassthroughStrategy()
        data = _make_multi_tf()
        result = strat.run(data)
        assert "1h" in result.data
        assert "4h" in result.data


# ===========================================================================
# 9. BaseStrategy — warmup_period
# ===========================================================================

class TestBaseStrategyWarmup:
    def test_warmup_skips_early_candles(self):
        class _WarmupStrat(_PassthroughStrategy):
            warmup_period = 10

        strat = _WarmupStrat()
        strat.setup_called = False
        df = _make_df(50)
        strat.run(df)
        # generate_signals should NOT have been called for indices 0-9
        assert 0 not in strat.candles_seen
        assert 9 not in strat.candles_seen
        assert 10 in strat.candles_seen
        assert strat.candles_seen == list(range(10, 50))

    def test_warmup_larger_than_data_raises(self):
        class _BigWarmup(_PassthroughStrategy):
            warmup_period = 100

        strat = _BigWarmup()
        with pytest.raises(ValueError, match="warmup_period"):
            strat.run(_make_df(50))


# ===========================================================================
# 10. BaseStrategy — helper methods
# ===========================================================================

class TestBaseStrategyHelpers:
    def _running_strat(self, df):
        """Helper: run passthrough and return (strat, data)."""
        strat = _PassthroughStrategy()
        result = strat.run(df)
        return strat, result.data

    def test_get_candle_primary(self):
        strat = _PassthroughStrategy()
        df = _make_df(50)
        data = {"1h": df}
        data = strat.prepare_indicators(data)
        candle = strat.get_candle(5, data)
        assert candle["timestamp"] == df.iloc[5]["timestamp"]

    def test_get_candle_other_timeframe(self):
        strat = _PassthroughStrategy()
        data = _make_multi_tf()
        data = strat.prepare_indicators(data)
        candle = strat.get_candle(2, data, timeframe="4h")
        assert candle["timestamp"] == data["4h"].iloc[2]["timestamp"]

    def test_latest_candle_at_exact_match(self):
        strat = _PassthroughStrategy()
        df = _make_df(50)
        data = {"1h": df}
        ts = int(df.iloc[10]["timestamp"])
        candle = strat.latest_candle_at("1h", ts, data)
        assert candle is not None
        assert int(candle["timestamp"]) == ts

    def test_latest_candle_at_before_all_returns_none(self):
        strat = _PassthroughStrategy()
        df = _make_df(50)
        data = {"1h": df}
        candle = strat.latest_candle_at("1h", 0, data)   # before first ts
        assert candle is None

    def test_latest_candle_at_between_timestamps(self):
        strat = _PassthroughStrategy()
        df = _make_df(50)
        data = {"1h": df}
        # Use a timestamp halfway between two candles
        ts_5 = int(df.iloc[5]["timestamp"])
        ts_6 = int(df.iloc[6]["timestamp"])
        midpoint = ts_5 + (ts_6 - ts_5) // 2
        candle = strat.latest_candle_at("1h", midpoint, data)
        assert int(candle["timestamp"]) == ts_5   # should return candle at index 5

    def test_history_returns_all_up_to_index(self):
        strat = _PassthroughStrategy()
        df = _make_df(50)
        data = {"1h": df}
        hist = strat.history(9, data)
        assert len(hist) == 10          # indices 0..9

    def test_history_lookback_limits_result(self):
        strat = _PassthroughStrategy()
        df = _make_df(50)
        data = {"1h": df}
        hist = strat.history(20, data, lookback=5)
        assert len(hist) == 5
        # Last row should be at index 20
        assert int(hist.iloc[-1]["timestamp"]) == int(df.iloc[20]["timestamp"])

    def test_repr_contains_class_name(self):
        strat = _PassthroughStrategy()
        assert "_PassthroughStrategy" in repr(strat)
        assert "1h" in repr(strat)


# ===========================================================================
# 11. MACDCrossoverStrategy — construction
# ===========================================================================

class TestMACDStrategyConstruction:
    def test_default_params(self):
        strat = MACDCrossoverStrategy()
        assert strat.fast_length == 12
        assert strat.slow_length == 26
        assert strat.signal_length == 9
        assert strat.sl_lookback == 10
        assert strat.primary_timeframe == "1h"

    def test_custom_params(self):
        strat = MACDCrossoverStrategy(
            fast_length=8, slow_length=21, signal_length=5,
            sl_lookback=20, timeframe="4h"
        )
        assert strat.fast_length == 8
        assert strat.slow_length == 21
        assert strat.signal_length == 5
        assert strat.sl_lookback == 20
        assert strat.primary_timeframe == "4h"

    def test_fast_ge_slow_raises(self):
        with pytest.raises(ValueError, match="fast_length"):
            MACDCrossoverStrategy(fast_length=26, slow_length=12)

    def test_fast_equal_slow_raises(self):
        with pytest.raises(ValueError, match="fast_length"):
            MACDCrossoverStrategy(fast_length=12, slow_length=12)

    def test_zero_length_raises(self):
        with pytest.raises(ValueError):
            MACDCrossoverStrategy(fast_length=0)

    def test_negative_sl_lookback_raises(self):
        with pytest.raises(ValueError, match="sl_lookback"):
            MACDCrossoverStrategy(sl_lookback=0)

    def test_warmup_period_set(self):
        strat = MACDCrossoverStrategy(fast_length=12, slow_length=26, signal_length=9)
        # warmup = slow_length + signal_length - 1 = 34
        assert strat.warmup_period == 34

    def test_warmup_custom(self):
        strat = MACDCrossoverStrategy(slow_length=20, signal_length=5)
        assert strat.warmup_period == 24   # 20 + 5 - 1


# ===========================================================================
# 12. MACDCrossoverStrategy — prepare_indicators
# ===========================================================================

class TestMACDStrategyPrepareIndicators:
    def test_indicator_columns_added(self):
        strat = MACDCrossoverStrategy()
        df = _make_df(100)
        result = strat.run(df)
        cols = result.data["1h"].columns
        assert "_atk_macd" in cols
        assert "_atk_macd_signal" in cols
        assert "_atk_macd_hist" in cols

    def test_original_data_not_mutated(self):
        strat = MACDCrossoverStrategy()
        df = _make_df(100)
        original_cols = set(df.columns)
        strat.run(df.copy())
        assert set(df.columns) == original_cols

    def test_indicator_values_match_standalone_macd(self):
        """MACD values in the result must match the standalone MACD indicator."""
        strat = MACDCrossoverStrategy()
        df = _make_df(100)
        result = strat.run(df)
        enriched = result.data["1h"]

        ref_macd = MACD(df["close"])

        # Compare non-NaN positions
        valid = ~enriched["_atk_macd"].isna()
        np.testing.assert_allclose(
            enriched["_atk_macd"][valid].values,
            ref_macd.macd[valid].values,
            rtol=1e-9,
        )

    def test_warmup_region_is_nan(self):
        """
        MACD line becomes valid after slow_length candles.
        Signal line becomes valid after slow_length + signal_length - 1 candles.
        Verify each boundary independently.
        """
        strat = MACDCrossoverStrategy()
        df = _make_df(100)
        result = strat.run(df)
        enriched = result.data["1h"]

        # MACD line: valid at index slow_length - 1 (0-based), so NaN before that
        first_valid_macd = strat.slow_length - 1
        assert enriched["_atk_macd"].iloc[:first_valid_macd].isna().all(), \
            "MACD line should be NaN before slow_length candles"
        assert not pd.isna(enriched["_atk_macd"].iloc[first_valid_macd]), \
            "MACD line should be valid at slow_length - 1"

        # Signal line: valid at index warmup_period - 1 (= slow + signal - 2, 0-based)
        first_valid_signal = strat.warmup_period - 1
        assert enriched["_atk_macd_signal"].iloc[:first_valid_signal].isna().all(), \
            "Signal line should be NaN before warmup_period"
        assert not pd.isna(enriched["_atk_macd_signal"].iloc[first_valid_signal]), \
            "Signal line should be valid at warmup_period - 1"


# ===========================================================================
# 13. MACDCrossoverStrategy — signal generation
# ===========================================================================

class TestMACDStrategySignals:
    """Verifies signals are emitted exactly where crossovers occur."""

    def _run_and_find_expected(self, df, strat):
        """Return (result, expected_crossover_indices)."""
        result = strat.run(df)
        enriched = result.data["1h"]

        macd_col = "_atk_macd"
        sig_col  = "_atk_macd_signal"

        crossovers_bull = []
        crossovers_bear = []
        n = len(enriched)
        for i in range(1, n):
            p_m = enriched[macd_col].iloc[i - 1]
            p_s = enriched[sig_col].iloc[i - 1]
            c_m = enriched[macd_col].iloc[i]
            c_s = enriched[sig_col].iloc[i]
            if any(map(pd.isna, [p_m, p_s, c_m, c_s])):
                continue
            if p_m < p_s and c_m >= c_s:
                crossovers_bull.append(i)
            elif p_m > p_s and c_m <= c_s:
                crossovers_bear.append(i)

        return result, crossovers_bull, crossovers_bear

    def test_signal_count_matches_crossover_count(self):
        strat = MACDCrossoverStrategy()
        df = _make_df(200)
        result, bull, bear = self._run_and_find_expected(df, strat)
        assert result.signal_count == len(bull) + len(bear)

    def test_long_signals_at_bullish_crossovers(self):
        strat = MACDCrossoverStrategy()
        df = _make_df(200)
        result, bull, bear = self._run_and_find_expected(df, strat)

        long_indices = [s.candle_index for s in result.long_signals]
        assert sorted(long_indices) == sorted(bull)

    def test_short_signals_at_bearish_crossovers(self):
        strat = MACDCrossoverStrategy()
        df = _make_df(200)
        result, bull, bear = self._run_and_find_expected(df, strat)

        short_indices = [s.candle_index for s in result.short_signals]
        assert sorted(short_indices) == sorted(bear)

    def test_signal_entry_price_is_close(self):
        strat = MACDCrossoverStrategy()
        df = _make_df(200)
        result = strat.run(df)
        for sig in result.signals:
            expected_close = float(df.iloc[sig.candle_index]["close"])
            assert sig.entry_price == pytest.approx(expected_close)

    def test_signal_timestamp_matches_candle(self):
        strat = MACDCrossoverStrategy()
        df = _make_df(200)
        result = strat.run(df)
        for sig in result.signals:
            expected_ts = int(df.iloc[sig.candle_index]["timestamp"])
            assert sig.timestamp == expected_ts

    def test_signal_timeframe_is_primary(self):
        strat = MACDCrossoverStrategy(timeframe="4h")
        df = _make_df(200)
        result = strat.run({strat.primary_timeframe: df})
        for sig in result.signals:
            assert sig.timeframe == "4h"

    def test_no_signal_before_warmup(self):
        strat = MACDCrossoverStrategy()
        df = _make_df(200)
        result = strat.run(df)
        for sig in result.signals:
            assert sig.candle_index >= strat.warmup_period


# ===========================================================================
# 14. MACDCrossoverStrategy — stop-loss
# ===========================================================================

class TestMACDStrategySL:
    def test_long_sl_is_window_min_low(self):
        strat = MACDCrossoverStrategy(sl_lookback=10)
        df = _make_df(200)
        result = strat.run(df)

        for sig in result.long_signals:
            i = sig.candle_index
            start = max(0, i - strat.sl_lookback + 1)
            window_low = float(df.iloc[start: i + 1]["low"].min())
            # If window_low < entry_price, SL should equal window_low
            if window_low < sig.entry_price:
                assert sig.stop_loss == pytest.approx(window_low)

    def test_short_sl_is_window_max_high(self):
        strat = MACDCrossoverStrategy(sl_lookback=10)
        df = _make_df(200)
        result = strat.run(df)

        for sig in result.short_signals:
            i = sig.candle_index
            start = max(0, i - strat.sl_lookback + 1)
            window_high = float(df.iloc[start: i + 1]["high"].max())
            if window_high > sig.entry_price:
                assert sig.stop_loss == pytest.approx(window_high)

    def test_long_sl_below_entry(self):
        strat = MACDCrossoverStrategy()
        df = _make_df(200)
        result = strat.run(df)
        for sig in result.long_signals:
            assert sig.stop_loss < sig.entry_price, \
                f"Long SL {sig.stop_loss} must be < entry {sig.entry_price}"

    def test_short_sl_above_entry(self):
        strat = MACDCrossoverStrategy()
        df = _make_df(200)
        result = strat.run(df)
        for sig in result.short_signals:
            assert sig.stop_loss > sig.entry_price, \
                f"Short SL {sig.stop_loss} must be > entry {sig.entry_price}"


# ===========================================================================
# 15. MACDCrossoverStrategy — exit signals
# ===========================================================================

class TestMACDStrategyExits:
    def test_exit_signals_emitted(self):
        strat = MACDCrossoverStrategy()
        df = _make_df(200)
        result = strat.run(df)
        # The MACD reversal exits should mirror the crossover count
        assert len(result.exit_signals) > 0

    def test_exit_reason_is_macd_reversal(self):
        strat = MACDCrossoverStrategy()
        df = _make_df(200)
        result = strat.run(df)
        for ex in result.exit_signals:
            assert ex.reason == "macd_reversal"

    def test_exit_price_is_close(self):
        strat = MACDCrossoverStrategy()
        df = _make_df(200)
        result = strat.run(df)
        for ex in result.exit_signals:
            expected = float(df.iloc[ex.candle_index]["close"])
            assert ex.exit_price == pytest.approx(expected)

    def test_exit_metadata_has_histogram_values(self):
        strat = MACDCrossoverStrategy()
        df = _make_df(200)
        result = strat.run(df)
        for ex in result.exit_signals:
            assert "histogram_prev" in ex.metadata
            assert "histogram_curr" in ex.metadata

    def test_exit_histogram_sign_change(self):
        """Every exit should be at a sign change of the histogram."""
        strat = MACDCrossoverStrategy()
        df = _make_df(200)
        result = strat.run(df)
        for ex in result.exit_signals:
            prev_h = ex.metadata["histogram_prev"]
            curr_h = ex.metadata["histogram_curr"]
            # One must be positive, other <= 0 (or vice versa)
            assert (prev_h > 0) != (curr_h > 0) or (prev_h < 0) != (curr_h < 0) \
                or prev_h == 0 or curr_h == 0


# ===========================================================================
# 16. MACDCrossoverStrategy — LIVE mode
# ===========================================================================

class TestMACDStrategyLiveMode:
    def test_live_returns_at_most_one_signal(self):
        strat = MACDCrossoverStrategy()
        df = _make_df(200)
        result = strat.run(df, mode=StrategyMode.LIVE)
        assert result.signal_count <= 1

    def test_live_signal_timestamp_is_last_candle(self):
        strat = MACDCrossoverStrategy()
        df = _make_df(200)
        result = strat.run(df, mode=StrategyMode.LIVE)
        last_ts = int(df.iloc[-1]["timestamp"])
        for sig in result.signals:
            assert sig.timestamp == last_ts

    def test_backtest_has_more_or_equal_signals_than_live(self):
        strat = MACDCrossoverStrategy()
        df = _make_df(200)
        result_bt = strat.run(df, mode=StrategyMode.BACKTEST)
        result_live = strat.run(df, mode=StrategyMode.LIVE)
        assert result_bt.signal_count >= result_live.signal_count

    def test_live_data_still_enriched(self):
        strat = MACDCrossoverStrategy()
        df = _make_df(200)
        result = strat.run(df, mode=StrategyMode.LIVE)
        assert "_atk_macd" in result.data[strat.primary_timeframe].columns

    def test_live_mode_stored_in_result(self):
        strat = MACDCrossoverStrategy()
        df = _make_df(200)
        result = strat.run(df, mode=StrategyMode.LIVE)
        assert result.mode is StrategyMode.LIVE


# ===========================================================================
# 17. MACDCrossoverStrategy — signal metadata
# ===========================================================================

class TestMACDStrategyMetadata:
    def test_metadata_keys_present(self):
        strat = MACDCrossoverStrategy()
        df = _make_df(200)
        result = strat.run(df)
        if result.has_signal:
            meta = result.signals[0].metadata
            for key in ("macd", "macd_signal", "histogram",
                        "fast_length", "slow_length", "signal_length"):
                assert key in meta, f"Missing metadata key: {key}"

    def test_metadata_lengths_match_params(self):
        strat = MACDCrossoverStrategy(fast_length=8, slow_length=21, signal_length=5)
        df = _make_df(200)
        result = strat.run(df)
        if result.has_signal:
            meta = result.signals[0].metadata
            assert meta["fast_length"] == 8
            assert meta["slow_length"] == 21
            assert meta["signal_length"] == 5

    def test_metadata_values_are_finite(self):
        strat = MACDCrossoverStrategy()
        df = _make_df(200)
        result = strat.run(df)
        for sig in result.signals:
            assert math.isfinite(sig.metadata["macd"])
            assert math.isfinite(sig.metadata["macd_signal"])
            assert math.isfinite(sig.metadata["histogram"])


# ===========================================================================
# 18. MACDCrossoverStrategy — edge cases
# ===========================================================================

class TestMACDStrategyEdgeCases:
    def test_custom_timeframe_key(self):
        strat = MACDCrossoverStrategy(timeframe="15m")
        df = _make_df(200)
        result = strat.run({"15m": df})
        for sig in result.signals:
            assert sig.timeframe == "15m"

    def test_take_profit_is_none(self):
        strat = MACDCrossoverStrategy()
        df = _make_df(200)
        result = strat.run(df)
        for sig in result.signals:
            assert sig.take_profit is None

    def test_multi_timeframe_data_not_broken_by_strategy(self):
        """Strategy only touches primary TF; other TFs are passed through."""
        strat = MACDCrossoverStrategy(timeframe="1h")
        data = _make_multi_tf()
        result = strat.run(data)
        # "4h" key preserved
        assert "4h" in result.data
        # Original 4h data unchanged (no indicator columns added there)
        assert "_atk_macd" not in result.data["4h"].columns

    def test_repr(self):
        strat = MACDCrossoverStrategy(timeframe="4h")
        r = repr(strat)
        assert "MACDCrossoverStrategy" in r
        assert "4h" in r


# ===========================================================================
# 19. Incremental computation (v1.0.0) — fixtures
# ===========================================================================

def _split_data(n_total: int = 200, n_seed: int = 150, seed: int = 0):
    """Full frame + (seed frame, remaining rows as live candle dicts)."""
    full = _make_df(n_total, seed)
    seed_df = full.iloc[:n_seed].reset_index(drop=True).copy()
    candles = [full.iloc[j].to_dict() for j in range(n_seed, n_total)]
    return full, seed_df, candles


def _candle(ts, o, h, low, c, v=1000.0, **extra):
    d = {"timestamp": ts, "open": o, "high": h, "low": low, "close": c, "volume": v}
    d.update(extra)
    return d


def _sig_key(sig: Signal):
    return (sig.timestamp, sig.direction,
            round(sig.entry_price, 9), round(sig.stop_loss, 9))


class _EmaCrossBase(BaseStrategy):
    """Close-crosses-above-EMA signals; `_ema` column; metadata carries the EMA."""
    primary_timeframe = "1h"

    def __init__(self, length: int = 20):
        self.length = length
        self.warmup_period = length
        self.prepare_calls = 0              # observation only
        self.prepare_lens: list[int] = []   # observation only

    def prepare_indicators(self, data):
        df = data["1h"].copy()
        df["_ema"] = EMA(df["close"], length=self.length).ema.values
        data["1h"] = df
        self.prepare_calls += 1
        self.prepare_lens.append(len(df))
        return data

    def generate_signals(self, i, data):
        if i < 1:
            return []
        df = data["1h"]
        curr, prev = df.iloc[i], df.iloc[i - 1]
        if pd.isna(curr["_ema"]) or pd.isna(prev["_ema"]):
            return []
        if float(prev["close"]) <= float(prev["_ema"]) < float(curr["close"]):
            return [Signal(
                direction="long",
                entry_price=float(curr["close"]),
                stop_loss=float(curr["close"]) * 0.99,
                take_profit=None,
                timestamp=int(curr["timestamp"]),
                candle_index=i,
                timeframe="1h",
                metadata={"ema": float(curr["_ema"])},
            )]
        return []


class _EmaTailStrategy(_EmaCrossBase):
    """No hook — exercises the tail-recompute fallback."""


class _EmaHookStrategy(_EmaCrossBase):
    """Hook path — streams the EMA via indicator.update()."""

    def setup(self, data):
        # Streaming twin built from the seed history.  prepare_indicators
        # stays free of self.* state (forming-candle safety).
        self._ema_stream = EMA(data["1h"]["close"], length=self.length)
        self.hook_calls: list[int] = []
        self.hook_saw_nan: list[bool] = []
        self.hook_row_count: list[int] = []

    def update_indicators(self, data, new_index):
        df = data["1h"]
        self.hook_row_count.append(len(df))
        self.hook_saw_nan.append(bool(pd.isna(df["_ema"].iloc[new_index])))
        value = self._ema_stream.update(float(df["close"].iloc[new_index]))
        df.iloc[new_index, df.columns.get_loc("_ema")] = value
        self.hook_calls.append(new_index)


class _SmaCrossStrategy(BaseStrategy):
    """No hook; windowed `_sma` — the fallback is exact when lookback <= K."""
    primary_timeframe = "1h"

    def __init__(self, length: int = 10):
        self.length = length
        self.warmup_period = length
        self.prepare_calls = 0
        self.prepare_lens: list[int] = []

    def prepare_indicators(self, data):
        df = data["1h"].copy()
        df["_sma"] = df["close"].rolling(self.length).mean()
        data["1h"] = df
        self.prepare_calls += 1
        self.prepare_lens.append(len(df))
        return data

    def generate_signals(self, i, data):
        curr = data["1h"].iloc[i]
        if pd.isna(curr["_sma"]):
            return []
        if float(curr["close"]) > float(curr["_sma"]) * 1.001:
            return [Signal(
                direction="long",
                entry_price=float(curr["close"]),
                stop_loss=float(curr["close"]) * 0.99,
                take_profit=None,
                timestamp=int(curr["timestamp"]),
                candle_index=i,
                timeframe="1h",
                metadata={"sma": float(curr["_sma"])},
            )]
        return []


class _LeadColStrategy(BaseStrategy):
    """No hook; `_lead` = close.shift(-2) — a retroactive backfill column."""
    primary_timeframe = "1h"

    def prepare_indicators(self, data):
        df = data["1h"].copy()
        df["_lead"] = df["close"].shift(-2)
        data["1h"] = df
        return data

    def generate_signals(self, i, data):
        return []


class _SwingZoneStrategy(BaseStrategy):
    """SMC-style strategy: python-object zone state maintained in the hook."""
    primary_timeframe = "1h"
    warmup_period = 3

    def __init__(self):
        self.prepare_calls = 0

    def prepare_indicators(self, data):
        df = data["1h"].copy()
        df["_hh3"] = df["high"].rolling(3).max()
        data["1h"] = df
        self.prepare_calls += 1
        return data

    def setup(self, data):
        self.zones: list[dict] = []
        self.hook_calls: list[int] = []

    def update_indicators(self, data, new_index):
        df = data["1h"]
        i = new_index
        if i >= 2:
            h0 = float(df["high"].iloc[i - 2])
            h1 = float(df["high"].iloc[i - 1])
            h2 = float(df["high"].iloc[i])
            if h1 > h0 and h1 > h2:
                self.zones.append({"price": h1, "index": i - 1})
        hh3 = float(df["high"].iloc[max(0, i - 2): i + 1].max())
        df.iloc[i, df.columns.get_loc("_hh3")] = hh3
        self.hook_calls.append(i)

    def generate_signals(self, i, data):
        if not self.zones:
            return []
        zone = self.zones[-1]["price"]
        curr = data["1h"].iloc[i]
        if float(curr["close"]) > zone:
            return [Signal(
                direction="long",
                entry_price=float(curr["close"]),
                stop_loss=float(curr["close"]) * 0.99,
                take_profit=None,
                timestamp=int(curr["timestamp"]),
                candle_index=i,
                timeframe="1h",
                metadata={"zone": zone},
            )]
        return []


def _expected_zones(highs, start: int, end: int) -> list[dict]:
    """Pure-python replica of _SwingZoneStrategy's hook zone detection."""
    zones = []
    for i in range(max(2, start), end):
        if highs[i - 1] > highs[i - 2] and highs[i - 1] > highs[i]:
            zones.append({"price": float(highs[i - 1]), "index": i - 1})
    return zones


class _MultiTfStrategy(BaseStrategy):
    """No hook; prepare_indicators touches primary AND a higher timeframe."""
    primary_timeframe = "1h"
    warmup_period = 5

    def prepare_indicators(self, data):
        df = data["1h"].copy()
        df["_sma"] = df["close"].rolling(5).mean()
        data["1h"] = df
        df4 = data["4h"].copy()
        df4["_htf_mean"] = df4["close"].rolling(3).mean()
        data["4h"] = df4
        return data

    def generate_signals(self, i, data):
        curr = data["1h"].iloc[i]
        htf = self.latest_candle_at("4h", int(curr["timestamp"]), data)
        if htf is None or pd.isna(htf["_htf_mean"]):
            return []
        if float(curr["close"]) > float(htf["_htf_mean"]):
            return [Signal(
                direction="long",
                entry_price=float(curr["close"]),
                stop_loss=float(curr["close"]) * 0.99,
                take_profit=None,
                timestamp=int(curr["timestamp"]),
                candle_index=i,
                timeframe="1h",
            )]
        return []


class _WarmupProbeStrategy(_PassthroughStrategy):
    warmup_period = 10


def _run_hook_stepping(n_total=180, n_seed=150):
    """Seed an _EmaHookStrategy then advance the remaining candles."""
    full, seed_df, candles = _split_data(n_total, n_seed)
    strat = _EmaHookStrategy()
    data = strat.run({"1h": seed_df}).data
    stepped: list[tuple[int, list[Signal], list[ExitSignal]]] = []
    for j, c in enumerate(candles):
        sigs, exits = advance_live_candle(strat, data, c)
        stepped.append((n_seed + j, sigs, exits))
    return full, strat, data, stepped


# ===========================================================================
# 20. Incremental — hook contract & helpers
# ===========================================================================

class TestIncrementalHookContract:
    def test_base_update_indicators_is_noop(self):
        strat = _PassthroughStrategy()
        df = _make_df(10)
        data = {"1h": df.copy()}
        assert strat.update_indicators(data, 0) is None
        assert data["1h"].equals(df)

    def test_has_update_hook_false_without_override(self):
        assert has_update_hook(_PassthroughStrategy()) is False
        assert has_update_hook(MACDCrossoverStrategy()) is False
        assert has_update_hook(_EmaTailStrategy()) is False

    def test_has_update_hook_true_with_override(self):
        assert has_update_hook(_EmaHookStrategy()) is True
        assert has_update_hook(_SwingZoneStrategy()) is True

    def test_default_recompute_window_floor(self):
        assert default_recompute_window(_SmaCrossStrategy(length=10)) == 200

    def test_default_recompute_window_warmup_dominates(self):
        strat = _SmaCrossStrategy(length=10)
        strat.warmup_period = 300
        assert default_recompute_window(strat) == 300

    def test_public_exports(self):
        import AlgoTradeKit.strategy as strategy_module
        for name in ("advance_live_candle", "evaluate_forming_candle",
                     "default_recompute_window", "has_update_hook"):
            assert name in strategy_module.__all__
            assert callable(getattr(strategy_module, name))


# ===========================================================================
# 21. Incremental — advance_live_candle, hook path
# ===========================================================================

class TestAdvanceHookPath:
    def test_hook_called_once_per_candle_in_order(self):
        _, strat, _, _ = _run_hook_stepping()
        assert strat.hook_calls == list(range(150, 180))

    def test_hook_sees_appended_nan_row(self):
        _, strat, _, _ = _run_hook_stepping()
        # Row already appended when the hook runs; its `_ema` cell starts NaN.
        assert strat.hook_row_count == [i + 1 for i in strat.hook_calls]
        assert all(strat.hook_saw_nan)

    def test_hook_path_never_recomputes(self):
        _, strat, _, _ = _run_hook_stepping()
        assert strat.prepare_calls == 1          # the seed run only
        assert strat.prepare_lens == [150]

    def test_hook_column_parity_with_batch(self):
        full, _, data, _ = _run_hook_stepping()
        batch = _EmaHookStrategy().run({"1h": full.copy()})
        a = batch.data["1h"]["_ema"].to_numpy()
        b = data["1h"]["_ema"].to_numpy()
        assert np.array_equal(pd.isna(a), pd.isna(b))
        mask = ~pd.isna(a)
        assert np.allclose(a[mask], b[mask], rtol=1e-9)

    def test_hook_signal_parity_with_batch(self):
        full, _, _, stepped = _run_hook_stepping()
        batch = _EmaHookStrategy().run({"1h": full.copy()})
        expected = [_sig_key(s) for s in batch.signals if s.candle_index >= 150]
        got = [_sig_key(s) for _, sigs, _ in stepped for s in sigs]
        assert got == expected
        assert all(exits == [] for _, _, exits in stepped)

    def test_advance_returns_new_candle_signal_fields(self):
        full, _, _, stepped = _run_hook_stepping()
        for idx, sigs, _ in stepped:
            for sig in sigs:
                assert sig.candle_index == idx
                assert sig.timestamp == int(full["timestamp"].iloc[idx])

    def test_timestamp_dtype_and_values(self):
        full, _, data, _ = _run_hook_stepping()
        ts = data["1h"]["timestamp"]
        assert ts.dtype == np.int64
        assert ts.tolist() == full["timestamp"].tolist()

    def test_smc_zones_via_hook(self):
        full, seed_df, candles = _split_data(200, 160)
        strat = _SwingZoneStrategy()
        data = strat.run({"1h": seed_df}).data
        for c in candles:
            advance_live_candle(strat, data, c)
        highs = [float(h) for h in full["high"]]
        assert strat.zones == _expected_zones(highs, 160, 200)
        assert strat.hook_calls == list(range(160, 200))
        # The hook also maintains its indicator column exactly.
        expected_hh3 = full["high"].rolling(3).max()
        got = data["1h"]["_hh3"]
        assert np.allclose(got.iloc[160:], expected_hh3.iloc[160:], rtol=1e-12)


# ===========================================================================
# 22. Incremental — advance_live_candle, tail-recompute fallback
# ===========================================================================

class TestAdvanceFallback:
    def test_windowed_indicator_exact_parity(self):
        full, seed_df, candles = _split_data(200, 150)
        batch = _SmaCrossStrategy().run({"1h": full.copy()})
        strat = _SmaCrossStrategy()
        data = strat.run({"1h": seed_df}).data
        stepped_signals: list[Signal] = []
        for c in candles:
            sigs, _ = advance_live_candle(strat, data, c)
            stepped_signals.extend(sigs)
        a = batch.data["1h"]["_sma"].to_numpy()
        b = data["1h"]["_sma"].to_numpy()
        assert np.array_equal(pd.isna(a), pd.isna(b))
        mask = ~pd.isna(a)
        assert np.allclose(a[mask], b[mask], rtol=1e-12)
        expected = [_sig_key(s) for s in batch.signals if s.candle_index >= 150]
        assert [_sig_key(s) for s in stepped_signals] == expected

    def test_fallback_recomputes_tail_only(self):
        _, seed_df, candles = _split_data(180, 150)
        strat = _SmaCrossStrategy()
        data = strat.run({"1h": seed_df}).data
        for c in candles:
            advance_live_candle(strat, data, c, recompute_window=40)
        assert strat.prepare_calls == 1 + len(candles)
        assert strat.prepare_lens == [150] + [40] * len(candles)

    def test_ema_fallback_generous_window_is_close(self):
        full, seed_df, candles = _split_data(200, 150)
        batch = _EmaTailStrategy().run({"1h": full.copy()})
        strat = _EmaTailStrategy()
        data = strat.run({"1h": seed_df}).data
        for c in candles:
            advance_live_candle(strat, data, c, recompute_window=120)
        a = batch.data["1h"]["_ema"].iloc[150:].to_numpy()
        b = data["1h"]["_ema"].iloc[150:].to_numpy()
        assert not np.isnan(b).any()
        assert np.allclose(a, b, atol=1e-2)

    def test_ema_fallback_small_window_is_approximate(self):
        full, seed_df, candles = _split_data(200, 150)
        batch = _EmaTailStrategy().run({"1h": full.copy()})
        strat = _EmaTailStrategy()
        data = strat.run({"1h": seed_df}).data
        for c in candles:
            advance_live_candle(strat, data, c, recompute_window=25)
        a = batch.data["1h"]["_ema"].iloc[150:].to_numpy()
        b = data["1h"]["_ema"].iloc[150:].to_numpy()
        diff = np.max(np.abs(a - b))
        assert diff > 1e-9          # genuinely approximate with a small K…
        assert diff < 5.0           # …but bounded (documented limit)

    def test_no_nan_band_after_many_steps(self):
        full, seed_df, candles = _split_data(160, 60)
        strat = _SmaCrossStrategy(length=10)
        data = strat.run({"1h": seed_df}).data
        for c in candles:
            advance_live_candle(strat, data, c, recompute_window=30)
        got = data["1h"]["_sma"]
        assert got.iloc[: strat.length - 1].isna().all()
        assert got.iloc[strat.length - 1:].notna().all()
        batch = _SmaCrossStrategy(length=10).run({"1h": full.copy()})
        assert np.allclose(got.iloc[strat.length - 1:],
                           batch.data["1h"]["_sma"].iloc[strat.length - 1:], rtol=1e-12)

    def test_fill_only_splice_never_revises_written_cells(self):
        full, seed_df, candles = _split_data(120, 100)
        strat = _SmaCrossStrategy(length=5)
        data = strat.run({"1h": seed_df}).data
        pos = data["1h"].columns.get_loc("_sma")
        data["1h"].iloc[95, pos] = 12345.0    # written cell → immutable
        data["1h"].iloc[90, pos] = np.nan     # NaN cell → refilled
        advance_live_candle(strat, data, candles[0])   # default K covers all rows
        df = data["1h"]
        assert df["_sma"].iloc[95] == 12345.0
        batch = _SmaCrossStrategy(length=5).run({"1h": full.copy()})
        assert math.isclose(df["_sma"].iloc[90],
                            float(batch.data["1h"]["_sma"].iloc[90]), rel_tol=1e-12)

    def test_splice_touches_tail_rows_only(self):
        _, seed_df, candles = _split_data(120, 100)
        strat = _SmaCrossStrategy(length=5)
        data = strat.run({"1h": seed_df}).data
        pos = data["1h"].columns.get_loc("_sma")
        data["1h"].iloc[10, pos] = np.nan     # outside the K=30 tail
        advance_live_candle(strat, data, candles[0], recompute_window=30)
        assert pd.isna(data["1h"]["_sma"].iloc[10])

    def test_retro_column_backfilled(self):
        full, seed_df, candles = _split_data(120, 100)
        strat = _LeadColStrategy()
        data = strat.run({"1h": seed_df}).data
        for c in candles:
            advance_live_candle(strat, data, c)
        got = data["1h"]["_lead"]
        assert np.allclose(got.iloc[:-2], full["close"].iloc[2:], rtol=1e-12)
        assert got.iloc[-2:].isna().all()


# ===========================================================================
# 23. Incremental — advance_live_candle validation & warmup
# ===========================================================================

class TestAdvanceValidation:
    def _seeded(self):
        _, seed_df, candles = _split_data(120, 100)
        strat = _SmaCrossStrategy(length=5)
        data = strat.run({"1h": seed_df}).data
        return strat, data, candles

    def test_missing_key_raises(self):
        strat, data, candles = self._seeded()
        bad = {k: v for k, v in candles[0].items() if k != "volume"}
        with pytest.raises(ValueError, match="missing required keys"):
            advance_live_candle(strat, data, bad)

    def test_duplicate_timestamp_raises(self):
        strat, data, candles = self._seeded()
        dup = dict(candles[0])
        dup["timestamp"] = int(data["1h"]["timestamp"].iloc[-1])
        with pytest.raises(ValueError, match="duplicate"):
            advance_live_candle(strat, data, dup)

    def test_out_of_order_timestamp_raises(self):
        strat, data, candles = self._seeded()
        old = dict(candles[0])
        old["timestamp"] = int(data["1h"]["timestamp"].iloc[-1]) - 3_600_000
        with pytest.raises(ValueError, match="out-of-order"):
            advance_live_candle(strat, data, old)

    def test_extra_keys_ignored(self):
        strat, data, candles = self._seeded()
        candle = dict(candles[0])
        candle.update({"closed": True, "quote_volume": 1.0, "trades": 42})
        advance_live_candle(strat, data, candle)
        row = data["1h"].iloc[-1]
        assert "closed" not in data["1h"].columns
        assert int(row["timestamp"]) == int(candle["timestamp"])
        assert float(row["close"]) == float(candle["close"])

    def test_empty_primary_raises(self):
        strat = _SmaCrossStrategy(length=5)
        data = {"1h": _make_df(10).iloc[0:0]}
        with pytest.raises(ValueError, match="empty"):
            advance_live_candle(strat, data, _candle(1, 1.0, 1.0, 1.0, 1.0))

    def test_missing_primary_tf_raises(self):
        strat = _SmaCrossStrategy(length=5)
        data = {"4h": _make_df(10)}
        with pytest.raises(ValueError, match="primary_timeframe"):
            advance_live_candle(strat, data, _candle(1, 1.0, 1.0, 1.0, 1.0))

    def test_bad_recompute_window_raises(self):
        strat, data, candles = self._seeded()
        with pytest.raises(ValueError, match="recompute_window"):
            advance_live_candle(strat, data, candles[0], recompute_window=0)

    def test_below_warmup_no_signals_hook_still_runs(self):
        full = _make_df(30)
        seed = full.iloc[:15].reset_index(drop=True).copy()
        strat = _EmaHookStrategy(length=10)
        strat.warmup_period = 30                     # frame stays below warmup
        data = strat.prepare_indicators({"1h": seed})
        strat.setup(data)
        sigs, exits = advance_live_candle(strat, data, full.iloc[15].to_dict())
        assert sigs == [] and exits == []
        assert strat.hook_calls == [15]              # state still advanced

    def test_below_warmup_generate_not_called(self):
        full = _make_df(8)
        seed = full.iloc[:5].reset_index(drop=True).copy()
        strat = _WarmupProbeStrategy()               # warmup_period == 10
        data = strat.prepare_indicators({"1h": seed})
        strat.setup(data)
        for j in range(5, 8):
            sigs, exits = advance_live_candle(strat, data, full.iloc[j].to_dict())
            assert sigs == [] and exits == []
        assert strat.candles_seen == []
        assert strat.exits_seen == []


# ===========================================================================
# 24. Incremental — evaluate_forming_candle
# ===========================================================================

class TestEvaluateFormingCandle:
    @staticmethod
    def _forming_after(df, close_mult=2.0, **extra):
        last = df.iloc[-1]
        close = float(last["close"]) * close_mult
        return _candle(
            int(last["timestamp"]) + 3_600_000,
            float(last["close"]), max(close, float(last["close"])),
            min(close, float(last["close"])), close, **extra,
        )

    def test_master_and_state_untouched(self):
        _, seed_df, candles = _split_data(200, 190)
        strat = _SwingZoneStrategy()
        data = strat.run({"1h": seed_df}).data
        for c in candles[:5]:
            advance_live_candle(strat, data, c)
        snap = {tf: df.copy() for tf, df in data.items()}
        zones_before = [dict(z) for z in strat.zones]
        hooks_before = list(strat.hook_calls)
        prep_before = strat.prepare_calls
        evaluate_forming_candle(strat, data, candles[5])
        assert set(data) == set(snap)
        assert all(data[tf].equals(snap[tf]) for tf in data)
        assert strat.zones == zones_before
        assert strat.hook_calls == hooks_before        # hook never called
        assert strat.prepare_calls == prep_before + 1  # tail recompute instead

    def test_forming_signal_values_and_index(self):
        _, seed_df, _ = _split_data(120, 100)
        strat = _SmaCrossStrategy(length=5)
        data = strat.run({"1h": seed_df}).data
        forming = self._forming_after(data["1h"], closed=False)
        sigs, _ = evaluate_forming_candle(strat, data, forming)
        assert len(sigs) == 1
        sig = sigs[0]
        assert sig.candle_index == 100 == len(data["1h"])
        assert sig.timestamp == forming["timestamp"]
        closes = [float(v) for v in data["1h"]["close"].iloc[-4:]]
        expected_sma = float(np.mean(closes + [forming["close"]]))
        assert math.isclose(sig.metadata["sma"], expected_sma, rel_tol=1e-9)
        assert len(data["1h"]) == 100                  # master unchanged

    def test_forming_then_closed_give_same_signal(self):
        _, seed_df, _ = _split_data(120, 100)
        strat = _SmaCrossStrategy(length=5)
        data = strat.run({"1h": seed_df}).data
        candle = self._forming_after(data["1h"])
        forming_sigs, _ = evaluate_forming_candle(strat, data, candle)
        closed_sigs, _ = advance_live_candle(strat, data, candle)
        assert [_sig_key(s) for s in forming_sigs] == [_sig_key(s) for s in closed_sigs]
        assert math.isclose(forming_sigs[0].metadata["sma"],
                            closed_sigs[0].metadata["sma"], rel_tol=1e-12)

    def test_forming_repeatable(self):
        _, seed_df, _ = _split_data(120, 100)
        strat = _SmaCrossStrategy(length=5)
        data = strat.run({"1h": seed_df}).data
        candle = self._forming_after(data["1h"])
        first = evaluate_forming_candle(strat, data, candle)
        second = evaluate_forming_candle(strat, data, candle)
        assert [_sig_key(s) for s in first[0]] == [_sig_key(s) for s in second[0]]
        # Same forming candle updated mid-bar (same timestamp) is fine too.
        updated = dict(candle)
        updated["close"] = candle["close"] * 1.01
        updated["high"] = max(updated["high"], updated["close"])
        sigs, _ = evaluate_forming_candle(strat, data, updated)
        assert len(sigs) == 1
        assert len(data["1h"]) == 100

    def test_forming_signal_against_committed_zone(self):
        _, seed_df, _ = _split_data(120, 100)
        strat = _SwingZoneStrategy()
        data = strat.run({"1h": seed_df}).data
        last = data["1h"].iloc[-1]
        ts, base = int(last["timestamp"]), float(last["close"])
        # Force a swing high at +5: highs base+2, base+5, base+1.
        for k, high in enumerate((base + 2, base + 5, base + 1)):
            advance_live_candle(strat, data, _candle(
                ts + (k + 1) * 3_600_000, base, high, base - 1, base))
        assert strat.zones and strat.zones[-1]["price"] == base + 5
        forming = _candle(ts + 4 * 3_600_000, base, base + 11, base, base + 10)
        sigs, _ = evaluate_forming_candle(strat, data, forming)
        assert len(sigs) == 1
        assert sigs[0].metadata["zone"] == base + 5
        assert strat.hook_calls[-1] == len(data["1h"]) - 1   # forming did not hook

    def test_forming_below_warmup_returns_empty(self):
        full = _make_df(30)
        seed = full.iloc[:15].reset_index(drop=True).copy()
        strat = _EmaHookStrategy(length=10)
        strat.warmup_period = 30                     # frame stays below warmup
        data = strat.prepare_indicators({"1h": seed})
        strat.setup(data)
        sigs, exits = evaluate_forming_candle(strat, data, full.iloc[15].to_dict())
        assert sigs == [] and exits == []
        assert strat.hook_calls == []

    def test_forming_duplicate_timestamp_raises(self):
        _, seed_df, _ = _split_data(120, 100)
        strat = _SmaCrossStrategy(length=5)
        data = strat.run({"1h": seed_df}).data
        stale = self._forming_after(data["1h"])
        stale["timestamp"] = int(data["1h"]["timestamp"].iloc[-1])
        with pytest.raises(ValueError, match="duplicate"):
            evaluate_forming_candle(strat, data, stale)


# ===========================================================================
# 25. Incremental — multi-timeframe
# ===========================================================================

class TestIncrementalMultiTimeframe:
    def _seeded(self):
        mt = _make_multi_tf(200)
        full_1h = mt["1h"]
        seed = {
            "1h": full_1h.iloc[:150].reset_index(drop=True).copy(),
            "4h": mt["4h"],
        }
        strat = _MultiTfStrategy()
        data = strat.run(seed).data
        candles = [full_1h.iloc[j].to_dict() for j in range(150, 160)]
        return strat, data, candles

    def test_advance_appends_primary_only(self):
        strat, data, candles = self._seeded()
        htf_obj = data["4h"]
        htf_snap = data["4h"].copy()
        for c in candles:
            advance_live_candle(strat, data, c)
        assert len(data["1h"]) == 160
        assert data["4h"] is htf_obj
        assert data["4h"].equals(htf_snap)
        assert data["1h"]["_sma"].iloc[-len(candles):].notna().all()

    def test_forming_with_multi_tf(self):
        strat, data, candles = self._seeded()
        snap_1h = data["1h"].copy()
        snap_4h = data["4h"].copy()
        evaluate_forming_candle(strat, data, candles[0])
        assert data["1h"].equals(snap_1h)
        assert data["4h"].equals(snap_4h)
