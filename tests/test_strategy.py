"""
tests/test_strategy.py
~~~~~~~~~~~~~~~~~~~~~~~
Full test suite for AlgoTradeKit.strategy module.

Tests cover
-----------
- _types  : Signal, ExitSignal, StrategyResult, StrategyMode
- _base   : BaseStrategy lifecycle, helpers, validation, BACKTEST vs LIVE
- builtin : MACDCrossoverStrategy indicators, crossovers, SL, exit signals
"""

from __future__ import annotations

import math
import pytest
import numpy as np
import pandas as pd

from AlgoTradeKit.strategy import (
    BaseStrategy,
    ExitSignal,
    MACDCrossoverStrategy,
    Signal,
    StrategyMode,
    StrategyResult,
)
from AlgoTradeKit.indicator import MACD


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
