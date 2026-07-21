"""
Tests for AlgoTradeKit.trader (v1.0.0) — configuration & types, the event
stream + terminal log, ``run_live`` paper trading, the exact candle-close
scheduler, order execution, the live loop, multi-RR ladders, safety rails,
persistence and multi-pair orchestration.

Config: validation (XOR seed fields,
display_candles >= min_candles, duplicate (broker, symbol), constants
exported) plus the display-SimulateConfig derivation (costs auto-filled
from broker.get_trading_costs(), user overrides win).

Events: every event type printed once with full detail;
log_events=False silences; log_event_types filters.  Plus: constants /
export surface, dataclass semantics (frozen, kw_only, source validation),
EventStream delivery + exception isolation, and the log_event_types
membership validation deferred here.

Scheduler: boundary arithmetic incl. the "no new candle until first
trade" quirk (mock venue that lags candle creation); venue-clock use;
lateness detection.  Plus: retry backoff + give-up semantics, gap-fill
after downtime, grid re-anchoring after a venue re-phase, baseline
initialisation, and stop semantics (scripted clock + real Event).

Execution (mock brokers): venue-native SL/TP attach per venue;
trailing/risk-free SL modification sequences equal simulate's sl_history;
spot short raises.  Plus: real-balance sizing (compound both ways,
risk_multiplier, position limits against live positions), client_order_id
format (MT5 seconds variant), slippage recording, naked-position emergency
close, SL-modify failure revert + retry, force-close per venue with
protective-order cleanup, and the events each operation emits.

Live loop: signal dedup — once per (pair, candle, direction), spanning
forming evaluations and the closed-candle commit.  Plus: Trader API guards
(pairs, display), seeding (min_candles, seed signals never
traded, scheduler anchored at seed end), the three execution modes
(scheduler-only commits; forming rows never mutate committed state;
tick-synthesized forming OHLC), venue-side close detection with real fill
prices (Binance user-data SL/TP fills incl. partial + rf mapping, futures
manual-close reconcile, MT5 positions poll + history_deals reasons, spot
degraded path), record_external_close, and run/stop/Ctrl+C lifecycle with
positions kept on shutdown.

Ladders: Binance reduce-only ladder vs MT5 partial-close ladder;
multi-RR SL modification sequences equal simulate's ``sl_history`` (full
``asdict`` slice parity against a ``SimulationStepper`` drive, both fraction
modes); each realized level emits ``TP_LEVEL``.  Plus: ladder placement
(fractions / zero-fraction / fraction-less plans, spot rejection, degrade on
placement failure with feed-detected market fallback), fill settlement
(partial slice accounting, final full close with leftover-order cleanup,
remainder below 1.0, out-of-order catch-up, price/qty fallbacks),
post-fill SL-modify retry (``pending_sl_sync``), advance-only revert on
venue failure, and the wiring (detection feed subscription, forming
items settle levels without strategy evaluation, user-data ladder routing,
SL-fill ladder cleanup, no double settlement across forming + commit).

Safety: kill switch — ``stop()`` / Ctrl+C-SIGTERM handlers (installed
on the main thread only, restored after ``run()``) / ``kill_switch_file``
(stops the session, stale file at start raises, never auto-deleted);
``on_stop`` both paths — ``keep`` leaves positions with their venue SL/TP
armed, ``close_all`` market-closes and cancels working orders (venue refusal
keeps the position protected); daily-loss gate — realized + unrealized per
UTC day, $ and percent thresholds (day-start base re-read at rollover, kept
on read failure), one ``DAILY_LOSS`` event per day, entries blocked while SL
management / exit closes continue, reset next UTC day, optional flatten
(``close_on_daily_loss``), forming rows trip mid-candle.

Persistence: round-trip (LiveTrade ⇄ journal dict, atomic
write-if-changed journal file); all three reconciliation cases — journaled +
present adopted with trailing/ladder/risk-free state resumed and lost venue
protection re-armed, journaled + missing closed from the venue trade history
(Binance ``my_trades`` / MT5 ``history_deals``, ``sl``/``rf``/``tp``/manual
reason mapping, leftover-order cleanup), present + not journaled warned and
left untouched.  Plus: dedup keys / trade-id sequence / trader_id restored
across restarts (a journaled signal is never re-sent), journal flush wiring
(per item, on shutdown, write-failure ERROR), corrupt-journal refusal before
anything starts, and a threaded stop → restart → adopt → venue-close e2e.

Multi-pair: shared broker — ``Trader(pairs=[...])`` runs
one worker per entry, brokers may repeat (pairs on one broker share that
account's wallet: sizing reads the same balance), one shared trader_id and
journal file (per-pair keys, ``#2``-suffixed on same-(market, symbol)
duplicates, thread-safe flushes), per-pair event streams / terminal filters /
daily-loss gates with an aggregate ``Trader.events`` stream, every kill
switch and ``on_stop`` stopping all pairs, flat close_time-sorted ``run()``
results, and startup-failure cleanup.  Combined report aggregation —
``run_live`` with ≥ 2 pairs and a displaying pair serves ONE combined
page (labels deduped per pair order, snapshots re-pushed on every closed
candle of any pair, URL printed in the no-browser flow); gate negatives
(single pair / no displaying pair) stay combined-free.
"""

from __future__ import annotations

import _thread
import dataclasses
import itertools
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

import AlgoTradeKit.simulate as sim_pkg
import AlgoTradeKit.trader as trader_pkg
import AlgoTradeKit.trader._scheduler as sched_module
import AlgoTradeKit.trader._trader as trader_module
from AlgoTradeKit.broker import (
    MARKET_FOREX,
    MARKET_FUTURES,
    MARKET_SPOT,
    STATUS_FILLED,
    STATUS_NEW,
    STATUS_REJECTED,
    AccountInfo,
    Balance,
    BrokerError,
    ConnectionFailed,
    MetaTraderBroker,
    Order,
    OrderError,
    OrderResult,
    Position,
    Ticker,
    TradingCosts,
)
from AlgoTradeKit.simulate import (
    CLOSE_REASON_FC,
    CLOSE_REASON_RF,
    CLOSE_REASON_SL,
    CLOSE_REASON_TP,
    CLOSE_REASON_TP_PARTIAL,
    EXCHANGE_TYPE_EXCHANGE,
    EXCHANGE_TYPE_METATRADER,
    REPORT_MODE_NONE,
    REPORT_MODE_WEBPAGE,
    TP_MODE_MULTI_RR,
    TP_MODE_NONE,
    TP_MODE_SIGNAL,
    SimulateConfig,
    SimulateReport,
    SimulationStepper,
)
from AlgoTradeKit.strategy import BaseStrategy, ExitSignal, Signal
from AlgoTradeKit.trader import (
    ALL_EVENT_TYPES,
    CLOSE_REASON_MANUAL,
    DISPLAY_TRADES_BOTH,
    DISPLAY_TRADES_REAL,
    DISPLAY_TRADES_SIM,
    EVENT_CLOSE,
    EVENT_DAILY_LOSS,
    EVENT_ERROR,
    EVENT_EXIT_SIGNAL,
    EVENT_OPEN,
    EVENT_RECONCILE,
    EVENT_RISK_FREE,
    EVENT_SIGNAL,
    EVENT_SL_MOVE,
    EVENT_TP_LEVEL,
    EXEC_CANDLE_CLOSE,
    EXEC_CANDLE_UPDATE,
    EXEC_TICK,
    ON_STOP_CLOSE_ALL,
    ON_STOP_KEEP,
    SOURCE_LIVE,
    SOURCE_SIM,
    CloseEvent,
    DailyLossEvent,
    ErrorEvent,
    EventStream,
    ExitSignalEvent,
    OpenEvent,
    ReconcileEvent,
    RiskFreeEvent,
    SignalEvent,
    SlMoveEvent,
    TerminalEventPrinter,
    TpLevelEvent,
    Trader,
    TraderConfig,
    TraderEvent,
    TraderPair,
    attach_terminal_printer,
    run_live,
)
from AlgoTradeKit.trader._config import (
    TraderSettings,
    parse_max_daily_loss,
    validate_pairs,
)
from AlgoTradeKit.trader._execution import ExecutionEngine
from AlgoTradeKit.trader._run_live import _EventBridge
from AlgoTradeKit.trader._scheduler import (
    DEFAULT_FETCH_TIMEOUT,
    DEFAULT_FINE_TICK_MS,
    DEFAULT_FINE_WAIT_MS,
    DEFAULT_LATE_WARN_MS,
    DEFAULT_RETRY_CAP_MS,
    DEFAULT_RETRY_INITIAL_MS,
    DEFAULT_TAIL_COUNT,
    CandleCloseScheduler,
    SchedulerTick,
    candle_close_ms,
    is_candle_final,
    newest_final_open_ms,
    next_boundary_ms,
    timeframe_ms,
)
from AlgoTradeKit.trader._state import (
    TraderStateJournal,
    build_pair_state,
    reconcile_startup,
    restore_trade,
    serialize_trade,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _cfg(**overrides) -> TraderConfig:
    """A valid minimal TraderConfig with per-test overrides."""
    kwargs = {"symbol": "BTCUSDT", "min_candles": 500}
    kwargs.update(overrides)
    return TraderConfig(**kwargs)


class _FakeCostsBroker:
    """Duck-typed broker exposing only get_trading_costs (like the fakes)."""

    def __init__(self, costs: TradingCosts | None = None):
        self.costs = costs or TradingCosts(
            commission_type="percentage", commission=0.0005, spread=0.5
        )
        self.calls: list[str] = []

    def get_trading_costs(self, symbol: str) -> TradingCosts:
        self.calls.append(symbol)
        return self.costs


class _NoopStrategy(BaseStrategy):
    primary_timeframe = "1h"

    def prepare_indicators(self, data):
        return data

    def setup(self, data):
        pass

    def generate_signals(self, candle_index, data):
        return []


class _RefusingTransport:
    """MetaTrader transport stub that must never be used."""

    def call(self, method, *args, **kwargs):  # pragma: no cover - guard only
        raise AssertionError("transport must not be called in config tests")


def _mt5_broker() -> MetaTraderBroker:
    return MetaTraderBroker(transport=_RefusingTransport(), connect=False)


# ---------------------------------------------------------------------------
# Defaults + SimulateConfig mirroring
# ---------------------------------------------------------------------------


class TestTraderConfigDefaults:
    def test_minimal_construction_and_defaults(self):
        c = _cfg()
        assert c.symbol == "BTCUSDT"
        assert c.min_candles == 500
        # Costs default to auto-derive.
        assert c.spread is None and c.commission_type is None and c.commission is None
        # Execution / data.
        assert c.execution == EXEC_CANDLE_CLOSE
        assert c.recompute_window is None
        assert c.candle_poll_interval == 1.0
        assert c.tick_poll_interval == 0.2
        # Logging.
        assert c.log_events is True
        assert c.log_event_types is None
        # Display.
        assert c.display is False
        assert c.display_trades == DISPLAY_TRADES_SIM
        assert c.display_open_browser is True
        assert c.chart_host == "127.0.0.1"
        assert c.chart_port == 0 and c.report_port == 0
        assert c.display_candles is None and c.display_start is None
        assert c.candle_count_limit is None
        # Safety.
        assert c.max_daily_loss is None
        assert c.close_on_daily_loss is False

    def test_mirrored_defaults_equal_simulate_config(self):
        c = _cfg()
        s = SimulateConfig()
        for name in (
            "leverage",
            "position_sizing",
            "risk_per_trade",
            "fixed_amount",
            "fixed_lot",
            "compound",
            "max_long_positions",
            "max_short_positions",
            "max_positions",
            "tp_mode",
            "tp_rr",
            "tp_levels",
            "tp_level_close_fractions",
            "sl_mode",
            "trailing_sl_percent",
            "risk_free_enabled",
            "risk_free_at_rr",
            "force_close_on_exit_signal",
        ):
            assert getattr(c, name) == getattr(s, name), name

    def test_min_candles_coerced_to_int(self):
        assert _cfg(min_candles=500.0).min_candles == 500


# ---------------------------------------------------------------------------
# Validation — trader-only fields
# ---------------------------------------------------------------------------


class TestTraderConfigValidation:
    def test_symbol_required(self):
        with pytest.raises(ValueError, match="symbol"):
            TraderConfig(symbol="", min_candles=500)
        with pytest.raises(ValueError, match="symbol"):
            TraderConfig(symbol=None, min_candles=500)  # type: ignore[arg-type]

    def test_min_candles_bounds(self):
        with pytest.raises(ValueError, match="min_candles"):
            _cfg(min_candles=0)

    @pytest.mark.parametrize("mode", [EXEC_CANDLE_CLOSE, EXEC_CANDLE_UPDATE, EXEC_TICK])
    def test_execution_valid(self, mode):
        assert _cfg(execution=mode).execution == mode

    def test_execution_invalid(self):
        with pytest.raises(ValueError, match="execution"):
            _cfg(execution="hourly")

    def test_cost_override_bounds(self):
        with pytest.raises(ValueError, match="spread"):
            _cfg(spread=-0.1)
        with pytest.raises(ValueError, match="commission"):
            _cfg(commission=-1.0)
        c = _cfg(spread=0.5, commission_type="per_lot", commission=5.0)
        assert (c.spread, c.commission_type, c.commission) == (0.5, "per_lot", 5.0)

    def test_commission_type_membership_reports_trader_config(self):
        with pytest.raises(ValueError) as err:
            _cfg(commission_type="bogus")
        assert "TraderConfig.commission_type" in str(err.value)
        assert "SimulateConfig" not in str(err.value)

    def test_mirrored_rules_enforced_with_trader_prefix(self):
        with pytest.raises(ValueError) as err:
            _cfg(leverage=0)
        assert "TraderConfig.leverage" in str(err.value)
        assert "SimulateConfig" not in str(err.value)
        with pytest.raises(ValueError, match="risk_per_trade"):
            _cfg(risk_per_trade=150.0)
        with pytest.raises(ValueError, match="max_positions"):
            _cfg(max_positions=0)
        with pytest.raises(ValueError, match="tp_mode"):
            _cfg(tp_mode="everything")
        with pytest.raises(ValueError, match="sl_mode"):
            _cfg(sl_mode="psychic")
        with pytest.raises(ValueError, match="tp_level_close_fractions"):
            _cfg(tp_mode="multi_rr", tp_levels=[1.0, 2.0], tp_level_close_fractions=[0.5])
        with pytest.raises(ValueError, match="tp_level_close_fractions"):
            _cfg(tp_level_close_fractions=[0.5])  # requires multi_rr

    def test_poll_intervals_positive(self):
        with pytest.raises(ValueError, match="poll_interval"):
            _cfg(candle_poll_interval=0)
        with pytest.raises(ValueError, match="poll_interval"):
            _cfg(tick_poll_interval=-1)

    def test_recompute_window(self):
        assert _cfg(recompute_window=None).recompute_window is None
        assert _cfg(recompute_window=300.0).recompute_window == 300
        with pytest.raises(ValueError, match="recompute_window"):
            _cfg(recompute_window=0)

    def test_candle_count_limit(self):
        assert _cfg(candle_count_limit=1000).candle_count_limit == 1000
        with pytest.raises(ValueError, match="candle_count_limit"):
            _cfg(candle_count_limit=0)

    def test_ports(self):
        with pytest.raises(ValueError, match="chart_port"):
            _cfg(chart_port=-1)
        with pytest.raises(ValueError, match="report_port"):
            _cfg(report_port=70000)
        c = _cfg(chart_port=8801, report_port=8802)
        assert (c.chart_port, c.report_port) == (8801, 8802)

    def test_chart_host_non_empty(self):
        with pytest.raises(ValueError, match="chart_host"):
            _cfg(chart_host="")

    def test_display_trades_membership(self):
        for value in (DISPLAY_TRADES_SIM, DISPLAY_TRADES_REAL, DISPLAY_TRADES_BOTH):
            assert _cfg(display_trades=value).display_trades == value
        with pytest.raises(ValueError, match="display_trades"):
            _cfg(display_trades="imaginary")

    def test_log_event_types_normalised_to_frozenset(self):
        c = _cfg(log_event_types=["open", "close", "open"])
        assert c.log_event_types == frozenset({"open", "close"})
        assert isinstance(c.log_event_types, frozenset)
        assert _cfg(log_event_types=()).log_event_types == frozenset()
        assert _cfg(log_event_types=None).log_event_types is None
        with pytest.raises(ValueError, match="log_event_types"):
            _cfg(log_event_types=["open", 3])


class TestMaxDailyLoss:
    def test_valid_amount_and_percent(self):
        assert _cfg(max_daily_loss=250).max_daily_loss == 250
        assert _cfg(max_daily_loss=99.5).max_daily_loss == 99.5
        assert _cfg(max_daily_loss="2%").max_daily_loss == "2%"
        assert _cfg(max_daily_loss=" 3.5% ").max_daily_loss == " 3.5% "

    def test_parse_helper(self):
        assert parse_max_daily_loss(100) == ("amount", 100.0)
        assert parse_max_daily_loss("2%") == ("percent", 2.0)
        assert parse_max_daily_loss(" 3.5% ") == ("percent", 3.5)

    @pytest.mark.parametrize(
        "bad", [0, -10, "2", "abc%", "0%", "150%", True, [5]], ids=repr
    )
    def test_invalid_values(self, bad):
        with pytest.raises(ValueError, match="max_daily_loss"):
            _cfg(max_daily_loss=bad)

    def test_close_on_daily_loss_requires_limit(self):
        with pytest.raises(ValueError, match="close_on_daily_loss"):
            _cfg(close_on_daily_loss=True)
        assert _cfg(max_daily_loss="2%", close_on_daily_loss=True).close_on_daily_loss


# ---------------------------------------------------------------------------
# Validation — history-seed XOR (: "XOR seed fields")
# ---------------------------------------------------------------------------


class TestSeedFieldsXor:
    def test_both_set_always_rejected(self):
        for display in (False, True):
            with pytest.raises(ValueError, match="mutually exclusive"):
                _cfg(display=display, display_candles=600, display_start="2026/01/01")

    def test_display_on_requires_a_seed(self):
        with pytest.raises(ValueError, match="history seed"):
            _cfg(display=True)

    def test_display_on_with_either_seed_ok(self):
        c = _cfg(display=True, display_candles=600)
        assert c.display_candles == 600
        c = _cfg(display=True, display_start="2026/01/01")
        assert c.display_start == "2026/01/01"

    def test_display_off_defaults_ok(self):
        c = _cfg()
        assert c.display_candles is None and c.display_start is None

    def test_display_off_single_seed_allowed(self):
        assert _cfg(display_candles=600).display_candles == 600
        assert _cfg(display_start=1_700_000_000_000).display_start == 1_700_000_000_000

    def test_display_candles_at_least_min_candles(self):
        with pytest.raises(ValueError, match="display_candles"):
            _cfg(display=True, display_candles=499)
        # Checked whenever set — display off included.
        with pytest.raises(ValueError, match="display_candles"):
            _cfg(display_candles=100)
        assert _cfg(display=True, display_candles=500).display_candles == 500

    def test_display_candles_coerced_to_int(self):
        assert _cfg(display_candles=512.0).display_candles == 512


# ---------------------------------------------------------------------------
# TraderPair + validate_pairs (: "duplicate (broker, symbol)")
# ---------------------------------------------------------------------------


class TestTraderPair:
    def test_valid_pair(self):
        broker, strat = _FakeCostsBroker(), _NoopStrategy()
        pair = TraderPair(broker=broker, config=_cfg(), strategy=strat)
        assert pair.broker is broker and pair.strategy is strat

    def test_type_checks(self):
        with pytest.raises(TypeError, match="broker"):
            TraderPair(broker=None, config=_cfg(), strategy=_NoopStrategy())
        with pytest.raises(TypeError, match="config"):
            TraderPair(broker=_FakeCostsBroker(), config={"symbol": "X"}, strategy=_NoopStrategy())
        with pytest.raises(TypeError, match="strategy"):
            TraderPair(broker=_FakeCostsBroker(), config=_cfg(), strategy=object())


class TestValidatePairs:
    def _pair(self, broker, symbol):
        return TraderPair(
            broker=broker, config=_cfg(symbol=symbol), strategy=_NoopStrategy()
        )

    def test_duplicate_broker_symbol_rejected(self):
        broker = _FakeCostsBroker()
        pairs = [self._pair(broker, "BTCUSDT"), self._pair(broker, "BTCUSDT")]
        with pytest.raises(ValueError, match="duplicate \\(broker, symbol\\)"):
            validate_pairs(pairs)

    def test_duplicate_detection_is_case_insensitive(self):
        broker = _FakeCostsBroker()
        with pytest.raises(ValueError, match="duplicate"):
            validate_pairs([self._pair(broker, "BTCUSDT"), self._pair(broker, "btcusdt")])

    def test_same_broker_different_symbols_ok(self):
        broker = _FakeCostsBroker()
        pairs = [self._pair(broker, "EURUSD"), self._pair(broker, "GBPUSD")]
        assert validate_pairs(pairs) == pairs

    def test_same_symbol_different_brokers_ok(self):
        pairs = [
            self._pair(_FakeCostsBroker(), "BTCUSDT"),
            self._pair(_FakeCostsBroker(), "BTCUSDT"),
        ]
        assert validate_pairs(pairs) == pairs

    def test_empty_rejected(self):
        with pytest.raises(ValueError, match="at least one"):
            validate_pairs([])

    def test_non_pair_entry_rejected(self):
        with pytest.raises(TypeError, match="pairs\\[1\\]"):
            validate_pairs([self._pair(_FakeCostsBroker(), "BTCUSDT"), "not a pair"])

    def test_returns_new_list_and_accepts_any_iterable(self):
        source = (self._pair(_FakeCostsBroker(), "BTCUSDT"),)
        result = validate_pairs(source)
        assert isinstance(result, list) and list(source) == result
        result.clear()                       # caller-side mutation is isolated
        assert len(source) == 1


# ---------------------------------------------------------------------------
# TraderSettings (Trader-level kwargs — fields + validation land in)
# ---------------------------------------------------------------------------


class TestTraderSettings:
    def test_defaults(self):
        s = TraderSettings()
        assert s.on_stop == ON_STOP_KEEP
        assert s.state_path is None and s.kill_switch_file is None

    def test_on_stop_membership(self):
        assert TraderSettings(on_stop=ON_STOP_CLOSE_ALL).on_stop == "close_all"
        with pytest.raises(ValueError, match="on_stop"):
            TraderSettings(on_stop="flee")

    def test_paths_normalised_to_str(self):
        s = TraderSettings(
            state_path=Path("state") / "t.json", kill_switch_file=Path("STOP")
        )
        assert s.state_path == str(Path("state") / "t.json")
        assert s.kill_switch_file == "STOP"
        assert isinstance(s.state_path, str) and isinstance(s.kill_switch_file, str)

    def test_bad_path_types_rejected(self):
        with pytest.raises(ValueError, match="state_path"):
            TraderSettings(state_path=123)
        with pytest.raises(ValueError, match="kill_switch_file"):
            TraderSettings(kill_switch_file=0.5)


# ---------------------------------------------------------------------------
# Display-SimulateConfig derivation (costs auto-derive)
# ---------------------------------------------------------------------------


class TestToSimulateConfig:
    def test_costs_auto_filled_from_broker(self):
        broker = _FakeCostsBroker(
            TradingCosts(commission_type="per_lot", commission=7.0, spread=0.25)
        )
        sim = _cfg().to_simulate_config(broker, initial_balance=25_000, primary_timeframe="4h")
        assert broker.calls == ["BTCUSDT"]
        assert sim.spread == 0.25
        assert sim.commission_type == "per_lot"
        assert sim.commission == 7.0
        assert sim.initial_balance == 25_000
        assert sim.primary_timeframe == "4h"
        assert sim.exchange_type == EXCHANGE_TYPE_EXCHANGE

    def test_full_override_skips_the_venue(self):
        broker = _FakeCostsBroker()
        cfg = _cfg(spread=0.9, commission_type="fixed", commission=1.5)
        sim = cfg.to_simulate_config(broker, initial_balance=10_000, primary_timeframe="1h")
        assert broker.calls == []            # user overrides win, venue never queried
        assert (sim.spread, sim.commission_type, sim.commission) == (0.9, "fixed", 1.5)

    def test_partial_override_mixes_user_and_venue(self):
        broker = _FakeCostsBroker(
            TradingCosts(commission_type="percentage", commission=0.0004, spread=0.5)
        )
        cfg = _cfg(commission_type="per_lot", commission=5.0)   # spread stays auto
        sim = cfg.to_simulate_config(broker, initial_balance=10_000, primary_timeframe="1h")
        assert broker.calls == ["BTCUSDT"]
        assert sim.spread == 0.5
        assert (sim.commission_type, sim.commission) == ("per_lot", 5.0)

    def test_mirrored_fields_copied(self):
        cfg = _cfg(
            symbol="EURUSD",
            leverage=30.0,
            position_sizing="fixed_lot",
            fixed_lot=0.2,
            compound=True,
            max_positions=3,
            max_long_positions=2,
            max_short_positions=2,
            tp_mode="multi_rr",
            tp_levels=[1.0, 2.0, 3.0],
            tp_level_close_fractions=[0.3, 0.3, 0.4],
            sl_mode="trailing",
            trailing_sl_percent=2.0,
            risk_free_enabled=True,
            risk_free_at_rr=1.5,
            force_close_on_exit_signal=True,
        )
        sim = cfg.to_simulate_config(
            _FakeCostsBroker(), initial_balance=5_000, primary_timeframe="1h"
        )
        for name in (
            "symbol",
            "leverage",
            "position_sizing",
            "fixed_lot",
            "compound",
            "max_positions",
            "max_long_positions",
            "max_short_positions",
            "tp_mode",
            "tp_levels",
            "tp_level_close_fractions",
            "sl_mode",
            "trailing_sl_percent",
            "risk_free_enabled",
            "risk_free_at_rr",
            "force_close_on_exit_signal",
        ):
            assert getattr(sim, name) == getattr(cfg, name), name
        assert isinstance(sim, SimulateConfig) and sim.config_id  # valid, auto-id'd

    def test_lists_are_copied_not_shared(self):
        cfg = _cfg(tp_mode="multi_rr", tp_levels=[1.0, 2.0], tp_level_close_fractions=[0.5, 0.5])
        sim = cfg.to_simulate_config(
            _FakeCostsBroker(), initial_balance=10_000, primary_timeframe="1h"
        )
        cfg.tp_levels.append(9.0)
        cfg.tp_level_close_fractions.append(0.0)
        assert sim.tp_levels == [1.0, 2.0]
        assert sim.tp_level_close_fractions == [0.5, 0.5]

    def test_metatrader_broker_sets_exchange_type(self):
        cfg = _cfg(symbol="EURUSD", spread=0.0001, commission_type="per_lot", commission=5.0)
        sim = cfg.to_simulate_config(
            _mt5_broker(), initial_balance=10_000, primary_timeframe="1h"
        )
        assert sim.exchange_type == EXCHANGE_TYPE_METATRADER

    def test_display_maps_to_show_chart_and_report_mode(self):
        on = _cfg(display=True, display_candles=600).to_simulate_config(
            _FakeCostsBroker(), initial_balance=10_000, primary_timeframe="1h"
        )
        assert on.show_chart is True and on.report_mode == REPORT_MODE_WEBPAGE
        off = _cfg().to_simulate_config(
            _FakeCostsBroker(), initial_balance=10_000, primary_timeframe="1h"
        )
        assert off.show_chart is False and off.report_mode == REPORT_MODE_NONE


# ---------------------------------------------------------------------------
# Exports (: "constants exported")
# ---------------------------------------------------------------------------


class TestExports:
    def test_constant_values(self):
        assert EXEC_CANDLE_CLOSE == "candle_close"
        assert EXEC_CANDLE_UPDATE == "candle_update"
        assert EXEC_TICK == "tick"
        assert DISPLAY_TRADES_SIM == "sim"
        assert DISPLAY_TRADES_REAL == "real"
        assert DISPLAY_TRADES_BOTH == "both"
        assert ON_STOP_KEEP == "keep"
        assert ON_STOP_CLOSE_ALL == "close_all"

    def test_package_all(self):
        assert set(trader_pkg.__all__) == {
            # — run_live paper trading
            "run_live",
            # — Trader live loop
            "Trader",
            "CLOSE_REASON_MANUAL",
            "TraderConfig",
            "TraderPair",
            "EXEC_CANDLE_CLOSE",
            "EXEC_CANDLE_UPDATE",
            "EXEC_TICK",
            "DISPLAY_TRADES_SIM",
            "DISPLAY_TRADES_REAL",
            "DISPLAY_TRADES_BOTH",
            "ON_STOP_KEEP",
            "ON_STOP_CLOSE_ALL",
            # — event stream + terminal log
            "TraderEvent",
            "SignalEvent",
            "OpenEvent",
            "SlMoveEvent",
            "RiskFreeEvent",
            "TpLevelEvent",
            "CloseEvent",
            "ExitSignalEvent",
            "DailyLossEvent",
            "ReconcileEvent",
            "ErrorEvent",
            "EventStream",
            "TerminalEventPrinter",
            "attach_terminal_printer",
            "EVENT_SIGNAL",
            "EVENT_EXIT_SIGNAL",
            "EVENT_OPEN",
            "EVENT_SL_MOVE",
            "EVENT_RISK_FREE",
            "EVENT_TP_LEVEL",
            "EVENT_CLOSE",
            "EVENT_DAILY_LOSS",
            "EVENT_RECONCILE",
            "EVENT_ERROR",
            "ALL_EVENT_TYPES",
            "SOURCE_SIM",
            "SOURCE_LIVE",
        }
        for name in trader_pkg.__all__:
            assert hasattr(trader_pkg, name), name


# ---------------------------------------------------------------------------
# — event stream + terminal log
# ---------------------------------------------------------------------------


TS = 1_752_241_800_000  # 2025-07-11 13:50:00 UTC
STAMP = "2025-07-11 13:50:00"


def _signal_event(**overrides) -> SignalEvent:
    kwargs = dict(
        time=TS, symbol="BTCUSDT", source=SOURCE_LIVE,
        direction="long", entry_price=64250.0, stop_loss=63800.0,
        take_profit=65000.0, size=0.5, risk_amount=100.0, rr=1.67,
        timeframe="1h", metadata={"note": "breakout"},
    )
    kwargs.update(overrides)
    return SignalEvent(**kwargs)


class TestEventTypesAndConstants:
    def test_trader_only_constant_values(self):
        assert EVENT_RISK_FREE == "risk_free"
        assert EVENT_DAILY_LOSS == "daily_loss"
        assert EVENT_RECONCILE == "reconcile"
        assert EVENT_ERROR == "error"
        assert SOURCE_SIM == "sim"
        assert SOURCE_LIVE == "live"

    def test_shared_constants_are_simulates(self):
        # The six sim-known types are imported from AlgoTradeKit.simulate —
        # values shared by construction.
        assert EVENT_SIGNAL is sim_pkg.EVENT_SIGNAL
        assert EVENT_EXIT_SIGNAL is sim_pkg.EVENT_EXIT_SIGNAL
        assert EVENT_OPEN is sim_pkg.EVENT_OPEN
        assert EVENT_SL_MOVE is sim_pkg.EVENT_SL_MOVE
        assert EVENT_TP_LEVEL is sim_pkg.EVENT_TP_LEVEL
        assert EVENT_CLOSE is sim_pkg.EVENT_CLOSE

    def test_all_event_types_is_the_full_domain(self):
        assert ALL_EVENT_TYPES == frozenset({
            "signal", "exit_signal", "open", "sl_move", "risk_free",
            "tp_level", "close", "daily_loss", "reconcile", "error",
        })
        assert isinstance(ALL_EVENT_TYPES, frozenset)

    def test_event_type_class_attribute_per_class(self):
        expected = {
            SignalEvent: EVENT_SIGNAL,
            OpenEvent: EVENT_OPEN,
            SlMoveEvent: EVENT_SL_MOVE,
            RiskFreeEvent: EVENT_RISK_FREE,
            TpLevelEvent: EVENT_TP_LEVEL,
            CloseEvent: EVENT_CLOSE,
            ExitSignalEvent: EVENT_EXIT_SIGNAL,
            DailyLossEvent: EVENT_DAILY_LOSS,
            ReconcileEvent: EVENT_RECONCILE,
            ErrorEvent: EVENT_ERROR,
        }
        for cls, etype in expected.items():
            assert cls.event_type == etype, cls
        assert set(expected.values()) == ALL_EVENT_TYPES


class TestEventDataclasses:
    def test_events_are_frozen(self):
        ev = _signal_event()
        with pytest.raises(dataclasses.FrozenInstanceError):
            ev.entry_price = 1.0

    def test_events_are_kw_only(self):
        with pytest.raises(TypeError):
            SlMoveEvent(TS, "BTCUSDT", SOURCE_SIM, 1, 100.0, 101.0)  # positional args

    def test_source_validated(self):
        with pytest.raises(ValueError, match="source"):
            _signal_event(source="paper")

    def test_time_coerced_to_int(self):
        ev = _signal_event(time=float(TS))
        assert ev.time == TS and isinstance(ev.time, int)

    def test_all_events_are_trader_events(self):
        ev = _signal_event()
        assert isinstance(ev, TraderEvent)

    def test_optional_fields_default_none(self):
        ev = SignalEvent(
            time=TS, symbol="X", source=SOURCE_SIM,
            direction="short", entry_price=10.0, stop_loss=11.0,
        )
        assert ev.take_profit is None and ev.size is None
        assert ev.risk_amount is None and ev.rr is None
        assert ev.timeframe == "" and ev.metadata == {} and ev.signal is None
        op = OpenEvent(
            time=TS, symbol="X", source=SOURCE_SIM, trade_id=1,
            direction="long", fill_price=10.0, size=1.0, margin_amount=10.0,
        )
        assert op.order_id is None and op.stop_loss is None and op.next_tp is None

    def test_reconcile_sequences_normalised_to_tuples(self):
        ev = ReconcileEvent(
            time=TS, symbol="", source=SOURCE_LIVE,
            adopted=["a", "b"], closed_offline=("c",), foreign=[],
        )
        assert ev.adopted == ("a", "b")
        assert ev.closed_offline == ("c",)
        assert ev.foreign == ()

    def test_rich_objects_ride_along(self):
        sig = Signal(
            direction="long", entry_price=100.0, stop_loss=99.0,
            take_profit=102.0, timestamp=TS, candle_index=7, timeframe="1h",
        )
        ev = _signal_event(signal=sig)
        assert ev.signal is sig
        ex = ExitSignal(reason="reversal", exit_price=None, timestamp=TS, candle_index=8)
        ee = ExitSignalEvent(
            time=TS, symbol="X", source=SOURCE_SIM, reason="reversal",
            action="force_close", exit_signal=ex,
        )
        assert ee.exit_signal is ex


class TestEventStream:
    def test_emit_delivers_to_all_subscribers_in_order(self):
        stream = EventStream()
        order: list[str] = []
        stream.subscribe(lambda e: order.append("first"))
        stream.subscribe(lambda e: order.append("second"))
        stream.emit(_signal_event())
        assert order == ["first", "second"]

    def test_subscriber_receives_the_event_object(self):
        stream = EventStream()
        got: list[TraderEvent] = []
        stream.subscribe(got.append)
        ev = _signal_event()
        stream.emit(ev)
        assert got == [ev] and got[0] is ev

    def test_unsubscribe_stops_delivery_and_is_idempotent(self):
        stream = EventStream()
        got: list[TraderEvent] = []
        unsubscribe = stream.subscribe(got.append)
        stream.emit(_signal_event())
        unsubscribe()
        unsubscribe()  # second call is a no-op
        stream.emit(_signal_event())
        assert len(got) == 1 and len(stream) == 0

    def test_failing_subscriber_warns_and_others_still_run(self):
        stream = EventStream()
        got: list[TraderEvent] = []

        def bad(event):
            raise RuntimeError("backend down")

        stream.subscribe(bad)
        stream.subscribe(got.append)
        with pytest.warns(UserWarning, match="subscriber failed"):
            stream.emit(_signal_event())
        assert len(got) == 1  # trading (and other subscribers) unaffected

    def test_unsubscribe_during_emit_is_safe(self):
        stream = EventStream()
        got: list[str] = []
        holder: dict = {}

        def self_removing(event):
            got.append("self")
            holder["unsub"]()

        holder["unsub"] = stream.subscribe(self_removing)
        stream.subscribe(lambda e: got.append("other"))
        stream.emit(_signal_event())    # snapshot iteration — both fire
        stream.emit(_signal_event())    # self_removing is gone now
        assert got == ["self", "other", "other"]

    def test_subscribe_rejects_non_callable(self):
        with pytest.raises(TypeError, match="callable"):
            EventStream().subscribe("not-a-callback")


class TestTerminalPrinterLines:
    """: every event type printed once with full detail."""

    def _line(self, event) -> str:
        lines: list[str] = []
        TerminalEventPrinter(out=lines.append)(event)
        assert len(lines) == 1
        return lines[0]

    def test_signal_line_full_detail(self):
        line = self._line(_signal_event())
        assert line == (
            f"{STAMP} [LIVE][BTCUSDT] SIGNAL long @ 64250 sl=63800 tp=65000 "
            f"size=0.5 risk=$100 rr=1.67 tf=1h meta={{'note': 'breakout'}}"
        )

    def test_signal_line_omits_unset_optionals(self):
        line = self._line(SignalEvent(
            time=TS, symbol="EURUSD", source=SOURCE_SIM,
            direction="short", entry_price=1.0855, stop_loss=1.0900,
        ))
        assert line == f"{STAMP} [SIM][EURUSD] SIGNAL short @ 1.0855 sl=1.09"
        assert "tp=" not in line and "risk=" not in line and "meta=" not in line

    def test_open_line_full_detail(self):
        line = self._line(OpenEvent(
            time=TS, symbol="BTCUSDT", source=SOURCE_LIVE, trade_id=7,
            direction="long", fill_price=64251.5, size=0.5,
            margin_amount=3212.58, risk_amount=100.0, stop_loss=63800.0,
            next_tp=65000.0, order_id="atk-1-BTCUSDT-1752241800000",
        ))
        assert line == (
            f"{STAMP} [LIVE][BTCUSDT] OPEN #7 long filled @ 64251.5 size=0.5 "
            f"margin=$3212.58 risk=$100 sl=63800 next_tp=65000 "
            f"order=atk-1-BTCUSDT-1752241800000"
        )

    def test_open_line_sim_fill_has_no_order_id(self):
        line = self._line(OpenEvent(
            time=TS, symbol="X", source=SOURCE_SIM, trade_id=1,
            direction="short", fill_price=100.0, size=2.0, margin_amount=200.0,
        ))
        assert line == f"{STAMP} [SIM][X] OPEN #1 short filled @ 100 size=2 margin=$200"
        assert "order=" not in line

    def test_sl_move_line(self):
        line = self._line(SlMoveEvent(
            time=TS, symbol="BTCUSDT", source=SOURCE_SIM, trade_id=3,
            old_sl=63800.0, new_sl=64100.0,
        ))
        assert line == f"{STAMP} [SIM][BTCUSDT] SL_MOVE #3 sl 63800 -> 64100 (trailing)"

    def test_sl_move_line_with_next_tp(self):
        line = self._line(SlMoveEvent(
            time=TS, symbol="BTCUSDT", source=SOURCE_LIVE, trade_id=3,
            old_sl=63800.0, new_sl=64250.0, cause="ladder", next_tp=66000.0,
        ))
        assert line.endswith("SL_MOVE #3 sl 63800 -> 64250 (ladder) next_tp=66000")

    def test_risk_free_line(self):
        line = self._line(RiskFreeEvent(
            time=TS, symbol="EURUSD", source=SOURCE_LIVE, trade_id=12,
            rr_level=1.0, old_sl=1.0800, new_sl=1.0850,
        ))
        assert line == (
            f"{STAMP} [LIVE][EURUSD] RISK_FREE #12 rr=1.00 touched, "
            f"sl 1.08 -> 1.085 (break-even)"
        )

    def test_tp_level_line(self):
        line = self._line(TpLevelEvent(
            time=TS, symbol="BTCUSDT", source=SOURCE_SIM, trade_id=5,
            level=2.0, fraction_closed=0.5, realized_pnl=95.5, new_sl=64250.0,
        ))
        assert line == (
            f"{STAMP} [SIM][BTCUSDT] TP_LEVEL #5 rr=2.00 hit closed=50% "
            f"pnl=$95.5 new_sl=64250"
        )

    def test_close_line_full_detail(self):
        line = self._line(CloseEvent(
            time=TS, symbol="BTCUSDT", source=SOURCE_LIVE, trade_id=5,
            exit_price=65000.0, reason="tp", gross_pnl=375.0, net_pnl=373.5,
            pnl_r=1.99, duration_ms=8_100_000,
        ))
        assert line == (
            f"{STAMP} [LIVE][BTCUSDT] CLOSE #5 @ 65000 reason=tp gross=$375 "
            f"net=$373.5 r=1.99 duration=2h 15m"
        )

    def test_close_line_negative_pnl_and_seconds_duration(self):
        line = self._line(CloseEvent(
            time=TS, symbol="X", source=SOURCE_SIM, trade_id=2,
            exit_price=99.0, reason="sl", gross_pnl=-50.0, net_pnl=-51.2,
            pnl_r=-1.0, duration_ms=30_000,
        ))
        assert "gross=-$50 net=-$51.2 r=-1.00 duration=30s" in line

    def test_exit_signal_line(self):
        line = self._line(ExitSignalEvent(
            time=TS, symbol="BTCUSDT", source=SOURCE_SIM,
            reason="trend_reversal", action="force_close",
        ))
        assert line == (
            f"{STAMP} [SIM][BTCUSDT] EXIT_SIGNAL reason=trend_reversal action=force_close"
        )

    def test_daily_loss_line_dollar_limit(self):
        line = self._line(DailyLossEvent(
            time=TS, symbol="BTCUSDT", source=SOURCE_LIVE, limit=500.0, loss=512.3,
        ))
        assert line == f"{STAMP} [LIVE][BTCUSDT] DAILY_LOSS loss=$512.3 limit=$500 trading halted"

    def test_daily_loss_line_percent_limit_and_close_all(self):
        line = self._line(DailyLossEvent(
            time=TS, symbol="BTCUSDT", source=SOURCE_LIVE,
            limit="2%", loss=512.3, closed_all=True,
        ))
        assert line.endswith(
            "DAILY_LOSS loss=$512.3 limit=2% trading halted (closing all positions)"
        )

    def test_reconcile_line(self):
        line = self._line(ReconcileEvent(
            time=TS, symbol="EURUSD", source=SOURCE_LIVE,
            adopted=("atk-1-a", "atk-1-b"), closed_offline=("atk-1-c",), foreign=("manual-1",),
        ))
        assert line == (
            f"{STAMP} [LIVE][EURUSD] RECONCILE adopted=[atk-1-a, atk-1-b] "
            f"closed_offline=[atk-1-c] foreign=[manual-1]"
        )

    def test_error_line_with_retry_and_details(self):
        line = self._line(ErrorEvent(
            time=TS, symbol="BTCUSDT", source=SOURCE_LIVE,
            where="create_order", message="Margin is insufficient.",
            will_retry=True, details={"code": -2019},
        ))
        assert line == (
            f"{STAMP} [LIVE][BTCUSDT] ERROR create_order: Margin is insufficient. "
            f"(will retry) details={{'code': -2019}}"
        )

    def test_error_line_minimal(self):
        line = self._line(ErrorEvent(
            time=TS, symbol="X", source=SOURCE_SIM, where="stream", message="drop",
        ))
        assert line == f"{STAMP} [SIM][X] ERROR stream: drop"

    def test_empty_symbol_omits_symbol_bracket(self):
        line = self._line(ReconcileEvent(time=TS, symbol="", source=SOURCE_LIVE))
        assert line.startswith(f"{STAMP} [LIVE] RECONCILE ")

    def test_timestamp_is_utc(self):
        # 2025-01-01 00:00:00 UTC exactly — a non-UTC strftime would shift it.
        line = self._line(ErrorEvent(
            time=1_735_689_600_000, symbol="X", source=SOURCE_SIM, where="w", message="m",
        ))
        assert line.startswith("2025-01-01 00:00:00 ")

    def test_grepable_prefix_on_every_type(self):
        events = [
            _signal_event(),
            OpenEvent(time=TS, symbol="BTCUSDT", source=SOURCE_LIVE, trade_id=1,
                      direction="long", fill_price=1.0, size=1.0, margin_amount=1.0),
            SlMoveEvent(time=TS, symbol="BTCUSDT", source=SOURCE_LIVE, trade_id=1,
                        old_sl=1.0, new_sl=2.0),
            RiskFreeEvent(time=TS, symbol="BTCUSDT", source=SOURCE_LIVE, trade_id=1,
                          rr_level=1.0, old_sl=1.0, new_sl=2.0),
            TpLevelEvent(time=TS, symbol="BTCUSDT", source=SOURCE_LIVE, trade_id=1,
                         level=1.0, fraction_closed=0.5, realized_pnl=1.0),
            CloseEvent(time=TS, symbol="BTCUSDT", source=SOURCE_LIVE, trade_id=1,
                       exit_price=1.0, reason="sl", gross_pnl=1.0, net_pnl=1.0,
                       pnl_r=1.0, duration_ms=1000),
            ExitSignalEvent(time=TS, symbol="BTCUSDT", source=SOURCE_LIVE, reason="r"),
            DailyLossEvent(time=TS, symbol="BTCUSDT", source=SOURCE_LIVE, limit=1.0, loss=2.0),
            ReconcileEvent(time=TS, symbol="BTCUSDT", source=SOURCE_LIVE),
            ErrorEvent(time=TS, symbol="BTCUSDT", source=SOURCE_LIVE, where="w", message="m"),
        ]
        assert {e.event_type for e in events} == ALL_EVENT_TYPES
        for event in events:
            line = self._line(event)
            assert line.startswith(f"{STAMP} [LIVE][BTCUSDT] "), line
            assert event.event_type.upper() in line

    def test_unknown_event_type_falls_back_to_generic_body(self):
        @dataclasses.dataclass(frozen=True, kw_only=True)
        class CustomEvent(TraderEvent):
            event_type = "custom_thing"
            detail: str = "x"
            missing: int | None = None

        line = self._line(CustomEvent(time=TS, symbol="X", source=SOURCE_SIM, detail="hello"))
        assert line == f"{STAMP} [SIM][X] CUSTOM_THING detail='hello'"

    def test_default_out_is_print(self, capsys):
        TerminalEventPrinter()(_signal_event())
        out = capsys.readouterr().out
        assert out.startswith(f"{STAMP} [LIVE][BTCUSDT] SIGNAL long @ 64250")
        assert out.endswith("\n")

    def test_format_event_returns_without_printing(self, capsys):
        text = TerminalEventPrinter().format_event(_signal_event())
        assert text.startswith(STAMP)
        assert capsys.readouterr().out == ""


class TestTerminalPrinterFilter:
    """: log_event_types filters; log_events=False silences."""

    def test_no_filter_prints_all_types(self):
        lines: list[str] = []
        printer = TerminalEventPrinter(out=lines.append)
        printer(_signal_event())
        printer(ErrorEvent(time=TS, symbol="X", source=SOURCE_SIM, where="w", message="m"))
        assert len(lines) == 2

    def test_filter_prints_only_named_types(self):
        lines: list[str] = []
        printer = TerminalEventPrinter(event_types={EVENT_CLOSE, EVENT_ERROR}, out=lines.append)
        printer(_signal_event())                                            # filtered out
        printer(CloseEvent(time=TS, symbol="X", source=SOURCE_SIM, trade_id=1,
                           exit_price=1.0, reason="sl", gross_pnl=0.0, net_pnl=0.0,
                           pnl_r=0.0, duration_ms=0))
        printer(ErrorEvent(time=TS, symbol="X", source=SOURCE_SIM, where="w", message="m"))
        assert len(lines) == 2
        assert "CLOSE" in lines[0] and "ERROR" in lines[1]

    def test_empty_filter_prints_nothing(self):
        lines: list[str] = []
        printer = TerminalEventPrinter(event_types=frozenset(), out=lines.append)
        printer(_signal_event())
        assert lines == []


class TestAttachTerminalPrinter:
    def test_log_events_true_attaches_and_prints(self):
        stream, lines = EventStream(), []
        unsubscribe = attach_terminal_printer(stream, _cfg(), out=lines.append)
        assert callable(unsubscribe)
        stream.emit(_signal_event())
        assert len(lines) == 1 and "SIGNAL" in lines[0]

    def test_log_events_false_silences(self):
        stream, lines = EventStream(), []
        result = attach_terminal_printer(stream, _cfg(log_events=False), out=lines.append)
        assert result is None
        stream.emit(_signal_event())
        assert lines == [] and len(stream) == 0

    def test_config_filter_honoured(self):
        stream, lines = EventStream(), []
        cfg = _cfg(log_event_types=[EVENT_CLOSE])
        attach_terminal_printer(stream, cfg, out=lines.append)
        stream.emit(_signal_event())    # filtered out
        stream.emit(CloseEvent(time=TS, symbol="X", source=SOURCE_SIM, trade_id=1,
                               exit_price=1.0, reason="sl", gross_pnl=0.0, net_pnl=0.0,
                               pnl_r=0.0, duration_ms=0))
        assert len(lines) == 1 and "CLOSE" in lines[0]

    def test_unsubscribe_detaches_printer(self):
        stream, lines = EventStream(), []
        unsubscribe = attach_terminal_printer(stream, _cfg(), out=lines.append)
        stream.emit(_signal_event())
        unsubscribe()
        stream.emit(_signal_event())
        assert len(lines) == 1

    def test_default_out_prints_to_stdout(self, capsys):
        stream = EventStream()
        attach_terminal_printer(stream, _cfg())
        stream.emit(_signal_event())
        assert "SIGNAL long @ 64250" in capsys.readouterr().out


class TestLogEventTypesMembership:
    """ deferred this validation to — the constants now exist."""

    def test_valid_constants_accepted(self):
        c = _cfg(log_event_types=list(ALL_EVENT_TYPES))
        assert c.log_event_types == ALL_EVENT_TYPES

    def test_unknown_type_rejected_and_named(self):
        with pytest.raises(ValueError, match=r"unknown event types.*bogus"):
            _cfg(log_event_types=["open", "bogus"])

    def test_error_message_lists_valid_types(self):
        with pytest.raises(ValueError, match="risk_free"):
            _cfg(log_event_types=["nope"])


# ---------------------------------------------------------------------------
# — run_live() paper trading
# ---------------------------------------------------------------------------


_P_TS = 1_752_000_000_000   # base candle open time (UTC ms)
_P_1H = 3_600_000


def _p_ts(i: int) -> int:
    return _P_TS + i * _P_1H


def _p_flat(n: int, price: float = 100.0, start: int = 0) -> list[dict]:
    return [
        {"timestamp": _p_ts(start + i), "open": price, "high": price + 0.5,
         "low": price - 0.5, "close": price, "volume": 10.0}
        for i in range(n)
    ]


def _p_candle(i: int, o: float, h: float, low: float, c: float) -> dict:
    return {"timestamp": _p_ts(i), "open": o, "high": h, "low": low,
            "close": c, "volume": 10.0}


class _PaperStream:
    def __init__(self, alive: bool = True):
        self._alive = alive

    @property
    def alive(self) -> bool:
        return self._alive

    def stop(self, timeout: float = 5.0) -> None:
        self._alive = False


class _PaperBroker:
    """Market-data-only broker double for run_live.

    ``live_rows`` are pushed **synchronously** inside ``stream_candles`` and
    the returned stream is already dead — run_live's wait loop then exits on
    its own (the feed-death path).  Without ``live_rows`` the stream stays
    alive (the Ctrl+C path).  Every order/position method records itself and
    raises — the zero-orders guarantee.
    """

    _ORDER_METHODS = (
        "create_order", "create_market_order", "create_limit_order",
        "cancel_order", "cancel_all", "close_position", "set_leverage",
        "modify_position", "open_orders", "open_positions",
    )

    def __init__(self, rows, live_rows=None, costs: TradingCosts | None = None):
        self.rows = list(rows)
        self.live_rows = list(live_rows or [])
        self.costs = costs or TradingCosts(
            commission_type="percentage", commission=0.0, spread=0.0
        )
        self.calls: list[tuple] = []
        self.order_calls: list[tuple] = []
        self.streams: list[_PaperStream] = []
        self.subscribed = threading.Event()

    # -- market data ---------------------------------------------------
    def fetch_last_candles(self, symbol, timeframe, count):
        self.calls.append(("last", symbol, timeframe, count))
        return [dict(r) for r in self.rows[-count:]]

    def fetch_candles(self, symbol, timeframe, start_ms, end_ms):
        self.calls.append(("range", symbol, timeframe, start_ms, end_ms))
        return [dict(r) for r in self.rows if start_ms <= r["timestamp"] <= end_ms]

    def get_trading_costs(self, symbol):
        self.calls.append(("costs", symbol))
        return self.costs

    def stream_candles(self, symbol, timeframe, on_candle, *, closed_only=True):
        self.calls.append(("stream", symbol, timeframe, closed_only))
        for row in self.live_rows:
            on_candle({**row, "closed": True})
        stream = _PaperStream(alive=not self.live_rows)
        self.streams.append(stream)
        self.subscribed.set()
        return stream

    # -- order surface: must never be reached in paper mode -------------
    def __getattr__(self, name):
        if name in _PaperBroker._ORDER_METHODS:
            def _trap(*args, **kwargs):
                self.order_calls.append((name, args, kwargs))
                raise AssertionError(f"paper mode called order method {name!r}")
            return _trap
        raise AttributeError(name)


class _PaperPlanStrategy(BaseStrategy):
    """Signals scripted by timestamp — stable under any seed length."""

    primary_timeframe = "1h"
    warmup_period = 0

    def __init__(self, plan: dict[int, list[dict]] | None = None):
        self._plan = dict(plan or {})

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


def _paper_cfg(**overrides) -> TraderConfig:
    kwargs = {"symbol": "BTCUSDT", "min_candles": 5}
    kwargs.update(overrides)
    return TraderConfig(**kwargs)


def _tp_lifecycle() -> tuple[_PaperBroker, _PaperPlanStrategy]:
    """10 seed candles + live: signal at candle 10 (entry 100, sl 95,
    tp 110) filled same candle, TP hit on candle 11 → one closed trade."""
    seed = _p_flat(10)
    live = [
        _p_candle(10, 100.0, 100.5, 99.5, 100.0),
        _p_candle(11, 100.2, 111.0, 99.8, 110.5),
    ]
    plan = {_p_ts(10): [{"entry": 100.0, "sl": 95.0, "tp": 110.0}]}
    return _PaperBroker(seed, live_rows=live), _PaperPlanStrategy(plan)


class TestRunLivePaperGuarantees:
    def test_places_zero_orders_and_returns_report(self):
        broker, strategy = _tp_lifecycle()
        report = run_live(strategy=strategy, broker=broker, config=_paper_cfg())
        assert broker.order_calls == []
        assert isinstance(report, SimulateReport)
        assert report.total_trades == 1
        assert report.tp_count == 1
        assert report.initial_balance == 10_000.0
        kinds = {c[0] for c in broker.calls}
        assert kinds <= {"last", "range", "costs", "stream"}

    def test_execution_module_never_imported(self, monkeypatch):
        # the tests import trader._execution in this very file — drop it
        # from sys.modules first so this asserts run_live itself never
        # (re-)imports the order code, independent of test order.
        monkeypatch.delitem(sys.modules, "AlgoTradeKit.trader._execution", raising=False)
        broker, strategy = _tp_lifecycle()
        run_live(strategy=strategy, broker=broker, config=_paper_cfg())
        assert "AlgoTradeKit.trader._execution" not in sys.modules

    @pytest.mark.parametrize("mode", [DISPLAY_TRADES_REAL, DISPLAY_TRADES_BOTH])
    def test_display_trades_real_or_both_raise(self, mode):
        broker, strategy = _tp_lifecycle()
        with pytest.raises(ValueError, match="no real fills"):
            run_live(strategy=strategy, broker=broker,
                     config=_paper_cfg(display_trades=mode))
        assert broker.order_calls == []

    def test_both_outputs_off_warns_and_still_runs(self):
        broker, strategy = _tp_lifecycle()
        with pytest.warns(UserWarning, match="silent paper run"):
            report = run_live(strategy=strategy, broker=broker,
                              config=_paper_cfg(log_events=False))
        assert report.total_trades == 1

    def test_log_events_on_no_silent_warning(self, recwarn):
        broker, strategy = _tp_lifecycle()
        run_live(strategy=strategy, broker=broker, config=_paper_cfg())
        assert not [w for w in recwarn if "silent paper run" in str(w.message)]


class TestRunLiveApi:
    def test_single_form_requires_all_three(self):
        broker, strategy = _tp_lifecycle()
        with pytest.raises(ValueError, match="all required"):
            run_live(strategy=strategy, broker=broker)
        with pytest.raises(ValueError, match="all required"):
            run_live(strategy=strategy, config=_paper_cfg())
        with pytest.raises(ValueError, match="all required"):
            run_live(broker=broker, config=_paper_cfg())

    def test_pairs_and_single_form_exclusive(self):
        broker, strategy = _tp_lifecycle()
        pair = TraderPair(broker=broker, config=_paper_cfg(), strategy=strategy)
        with pytest.raises(ValueError, match="not both"):
            run_live(strategy=strategy, pairs=[pair])

    def test_duplicate_pairs_rejected(self):
        broker, strategy = _tp_lifecycle()
        pair1 = TraderPair(broker=broker, config=_paper_cfg(), strategy=strategy)
        pair2 = TraderPair(broker=broker, config=_paper_cfg(),
                           strategy=_PaperPlanStrategy({}))
        with pytest.raises(ValueError, match="duplicate"):
            run_live(pairs=[pair1, pair2])

    def test_initial_balance_validation(self):
        broker, strategy = _tp_lifecycle()
        for bad in (0, -100):
            with pytest.raises(ValueError, match="initial_balance"):
                run_live(strategy=strategy, broker=broker,
                         config=_paper_cfg(), initial_balance=bad)

    def test_initial_balance_flows_into_the_paper_wallet(self):
        broker, strategy = _tp_lifecycle()
        report = run_live(strategy=strategy, broker=broker,
                          config=_paper_cfg(), initial_balance=2_500.0)
        assert report.initial_balance == 2_500.0
        assert report.total_trades == 1

    def test_multi_pair_returns_reports_in_order(self):
        seed = _p_flat(10)
        live = [
            _p_candle(10, 100.0, 100.5, 99.5, 100.0),
            _p_candle(11, 100.2, 111.0, 99.8, 110.5),
        ]
        plan = {_p_ts(10): [{"entry": 100.0, "sl": 95.0, "tp": 110.0}]}
        broker = _PaperBroker(seed, live_rows=live)
        pair_a = TraderPair(broker=broker, config=_paper_cfg(),
                            strategy=_PaperPlanStrategy(plan))
        pair_b = TraderPair(broker=broker, config=_paper_cfg(symbol="ETHUSDT"),
                            strategy=_PaperPlanStrategy({}))
        reports = run_live(pairs=[pair_a, pair_b], initial_balance=5_000.0)
        assert isinstance(reports, list) and len(reports) == 2
        assert all(isinstance(r, SimulateReport) for r in reports)
        assert reports[0].total_trades == 1
        assert reports[1].total_trades == 0
        assert all(r.initial_balance == 5_000.0 for r in reports)
        assert broker.order_calls == []

    def test_silent_pair_warning_names_the_pair(self):
        seed = _p_flat(10)
        live = [_p_candle(10, 100.0, 100.5, 99.5, 100.0)]
        broker = _PaperBroker(seed, live_rows=live)
        pair_a = TraderPair(broker=broker, config=_paper_cfg(),
                            strategy=_PaperPlanStrategy({}))
        pair_b = TraderPair(broker=broker,
                            config=_paper_cfg(symbol="ETHUSDT", log_events=False),
                            strategy=_PaperPlanStrategy({}))
        with pytest.warns(UserWarning, match="ETHUSDT"):
            run_live(pairs=[pair_a, pair_b])

    def test_top_level_reexport(self):
        import AlgoTradeKit

        assert AlgoTradeKit.run_live is run_live
        assert "run_live" in AlgoTradeKit.__all__
        with pytest.raises(AttributeError):
            AlgoTradeKit.does_not_exist


class TestRunLiveSeeding:
    def test_headless_default_seeds_min_candles(self):
        broker = _PaperBroker(_p_flat(10),
                              live_rows=[_p_candle(10, 100.0, 100.5, 99.5, 100.0)])
        run_live(strategy=_PaperPlanStrategy({}), broker=broker, config=_paper_cfg())
        assert ("last", "BTCUSDT", "1h", 5) in broker.calls

    def test_display_candles_honored_when_display_off(self):
        broker = _PaperBroker(_p_flat(10),
                              live_rows=[_p_candle(10, 100.0, 100.5, 99.5, 100.0)])
        run_live(strategy=_PaperPlanStrategy({}), broker=broker,
                 config=_paper_cfg(display_candles=8))
        assert ("last", "BTCUSDT", "1h", 8) in broker.calls

    def test_display_start_honored(self):
        broker = _PaperBroker(_p_flat(10),
                              live_rows=[_p_candle(10, 100.0, 100.5, 99.5, 100.0)])
        run_live(strategy=_PaperPlanStrategy({}), broker=broker,
                 config=_paper_cfg(display_start=_p_ts(0)))
        range_calls = [c for c in broker.calls if c[0] == "range"]
        assert len(range_calls) == 1
        assert range_calls[0][1:4] == ("BTCUSDT", "1h", _p_ts(0))

    def test_display_start_too_few_candles_raises(self):
        broker = _PaperBroker(_p_flat(10))
        with pytest.raises(ValueError, match="fewer than the required minimum"):
            run_live(strategy=_PaperPlanStrategy({}), broker=broker,
                     config=_paper_cfg(min_candles=50, display_start=_p_ts(0)))
        assert broker.streams == []   # never subscribed

    def test_short_venue_history_raises(self):
        broker = _PaperBroker(_p_flat(4))   # venue has fewer than min_candles
        with pytest.raises(ValueError, match="fewer than the required minimum"):
            run_live(strategy=_PaperPlanStrategy({}), broker=broker,
                     config=_paper_cfg(display_candles=8))


def _paper_pos(**overrides) -> SimpleNamespace:
    base = dict(trade_id=0, stop_loss=95.0, initial_stop_loss=95.0,
                entry_price=100.0, last_rr_hit=0, risk_free_triggered=False,
                original_size=2.0)
    base.update(overrides)
    return SimpleNamespace(**base)


def _paper_trade(**overrides) -> SimpleNamespace:
    base = dict(trade_id=0, exit_price=110.0, close_reason="tp",
                gross_pnl=200.0, net_pnl=198.0, pnl_r=2.0,
                duration_ms=3_600_000, size=1.0, rr_levels_hit=1)
    base.update(overrides)
    return SimpleNamespace(**base)


def _bridge_kit(config: TraderConfig | None = None, positions=None,
                ts: int = _p_ts(20)):
    """(bridge, seen_events, sim_stub) with a collector subscribed."""
    config = config or _paper_cfg()
    stream = EventStream()
    seen: list = []
    stream.subscribe(seen.append)
    bridge = _EventBridge(config, stream)
    sim = SimpleNamespace(
        stepper=SimpleNamespace(open_positions=list(positions or [])),
        data={"1h": pd.DataFrame({"timestamp": [ts]})},
        strategy=SimpleNamespace(primary_timeframe="1h"),
    )
    bridge.bind(sim)
    return bridge, seen, sim


class TestRunLiveEventBridge:
    def test_signal_event_mapping(self):
        bridge, seen, _ = _bridge_kit()
        sig = Signal(direction="long", entry_price=100.0, stop_loss=95.0,
                     take_profit=110.0, timestamp=_p_ts(10), candle_index=10,
                     timeframe="1h", metadata={"why": "test"})
        bridge.on_event({"type": EVENT_SIGNAL, "time": _p_ts(10),
                         "symbol": "BTCUSDT", "signal": sig})
        assert len(seen) == 1
        ev = seen[0]
        assert isinstance(ev, SignalEvent)
        assert ev.source == SOURCE_SIM
        assert ev.time == _p_ts(10) and ev.symbol == "BTCUSDT"
        assert ev.direction == "long" and ev.entry_price == 100.0
        assert ev.stop_loss == 95.0 and ev.take_profit == 110.0
        assert ev.rr == pytest.approx(2.0)
        assert ev.timeframe == "1h"
        assert ev.metadata == {"why": "test"} and ev.metadata is not sig.metadata
        assert ev.signal is sig
        assert ev.size is None and ev.risk_amount is None

    def test_signal_without_tp_has_no_rr(self):
        bridge, seen, _ = _bridge_kit()
        sig = Signal(direction="short", entry_price=100.0, stop_loss=105.0,
                     take_profit=None, timestamp=_p_ts(10), candle_index=10,
                     timeframe="1h")
        bridge.on_event({"type": EVENT_SIGNAL, "time": _p_ts(10),
                         "symbol": "BTCUSDT", "signal": sig})
        assert seen[0].take_profit is None and seen[0].rr is None

    def test_signal_zero_risk_has_no_rr(self):
        # A real Signal forbids sl == entry; the rr guard still protects
        # against duck-typed signal objects.
        bridge, seen, _ = _bridge_kit()
        sig = SimpleNamespace(direction="long", entry_price=100.0,
                              stop_loss=100.0, take_profit=110.0,
                              timeframe="1h", metadata={})
        bridge.on_event({"type": EVENT_SIGNAL, "time": _p_ts(10),
                         "symbol": "BTCUSDT", "signal": sig})
        assert seen[0].rr is None

    @pytest.mark.parametrize("force,action", [(True, "force_close"), (False, "none")])
    def test_exit_signal_action_follows_config(self, force, action):
        bridge, seen, _ = _bridge_kit(_paper_cfg(force_close_on_exit_signal=force))
        ex = ExitSignal(reason="trend_reversal", exit_price=None,
                        timestamp=_p_ts(11), candle_index=11)
        bridge.on_event({"type": EVENT_EXIT_SIGNAL, "time": _p_ts(11),
                         "symbol": "BTCUSDT", "exit_signal": ex})
        ev = seen[0]
        assert isinstance(ev, ExitSignalEvent)
        assert ev.reason == "trend_reversal" and ev.action == action
        assert ev.exit_signal is ex

    def _open_dict(self, **overrides):
        base = dict(type=EVENT_OPEN, time=_p_ts(10), symbol="BTCUSDT",
                    trade_id=0, direction="long", entry_price=100.0, size=20.0,
                    margin_amount=2_000.0, risk_amount=100.0, stop_loss=95.0,
                    next_tp=None)
        base.update(overrides)
        return base

    def test_open_event_mapping_and_memory(self):
        bridge, seen, _ = _bridge_kit()
        bridge.on_event(self._open_dict())
        ev = seen[0]
        assert isinstance(ev, OpenEvent)
        assert ev.trade_id == 0 and ev.direction == "long"
        assert ev.fill_price == 100.0 and ev.size == 20.0
        assert ev.margin_amount == 2_000.0 and ev.risk_amount == 100.0
        assert ev.stop_loss == 95.0 and ev.order_id is None
        assert bridge._sl_known[0] == 95.0
        assert bridge._rf_known[0] is False and bridge._rr_known[0] == 0

    def test_close_event_mapping_and_cleanup(self):
        bridge, seen, _ = _bridge_kit()
        bridge.on_event(self._open_dict())
        trade = _paper_trade()
        bridge.on_event({"type": EVENT_CLOSE, "time": _p_ts(11),
                         "symbol": "BTCUSDT", "trade_id": 0, "trade": trade})
        ev = seen[-1]
        assert isinstance(ev, CloseEvent)
        assert ev.exit_price == 110.0 and ev.reason == "tp"
        assert ev.gross_pnl == 200.0 and ev.net_pnl == 198.0
        assert ev.pnl_r == 2.0 and ev.duration_ms == 3_600_000
        assert ev.trade is trade
        assert 0 not in bridge._sl_known and 0 not in bridge._rf_known
        assert 0 not in bridge._rr_known and 0 not in bridge._tp_marked

    def test_tp_level_event_mapping(self):
        cfg = _paper_cfg(tp_mode=TP_MODE_MULTI_RR, tp_levels=[1.0, 2.0],
                         tp_level_close_fractions=[0.5, 0.5])
        pos = _paper_pos(stop_loss=100.0, last_rr_hit=1)
        bridge, seen, _ = _bridge_kit(cfg, positions=[pos])
        slice_trade = _paper_trade(size=1.0, rr_levels_hit=1, net_pnl=50.0)
        bridge.on_event({"type": EVENT_TP_LEVEL, "time": _p_ts(11),
                         "symbol": "BTCUSDT", "trade_id": 0,
                         "trade": slice_trade, "levels_hit": 1})
        ev = seen[0]
        assert isinstance(ev, TpLevelEvent)
        assert ev.level == 1.0
        assert ev.fraction_closed == pytest.approx(0.5)   # 1.0 of original 2.0
        assert ev.realized_pnl == 50.0
        assert ev.new_sl == 100.0                          # ladder already moved it
        assert ev.trade is slice_trade
        assert bridge._tp_marked[0] == _p_ts(11)
        assert bridge._rr_known[0] == 1

    def test_sl_move_suppressed_after_tp_level_same_candle(self):
        cfg = _paper_cfg(tp_mode=TP_MODE_MULTI_RR, tp_levels=[1.0, 2.0],
                         tp_level_close_fractions=[0.5, 0.5])
        pos = _paper_pos(stop_loss=100.0, last_rr_hit=1)
        bridge, seen, _ = _bridge_kit(cfg, positions=[pos])
        bridge.on_event({"type": EVENT_TP_LEVEL, "time": _p_ts(11),
                         "symbol": "BTCUSDT", "trade_id": 0,
                         "trade": _paper_trade(size=1.0), "levels_hit": 1})
        bridge.on_event({"type": EVENT_SL_MOVE, "time": _p_ts(11),
                         "symbol": "BTCUSDT", "trade_id": 0,
                         "old_sl": 95.0, "new_sl": 100.0, "next_tp": 110.0})
        assert len(seen) == 1                       # no standalone SlMoveEvent
        assert isinstance(seen[0], TpLevelEvent)
        assert 0 not in bridge._tp_marked           # mark consumed
        assert bridge._sl_known[0] == 100.0

    def test_sl_move_ladder_cause_without_partial_close(self):
        cfg = _paper_cfg(tp_mode=TP_MODE_MULTI_RR, tp_levels=[1.0, 2.0])
        pos = _paper_pos(stop_loss=100.0, last_rr_hit=1)
        bridge, seen, _ = _bridge_kit(cfg, positions=[pos])
        bridge._rr_known[0] = 0
        bridge.on_event({"type": EVENT_SL_MOVE, "time": _p_ts(11),
                         "symbol": "BTCUSDT", "trade_id": 0,
                         "old_sl": 95.0, "new_sl": 100.0, "next_tp": 110.0})
        ev = seen[0]
        assert isinstance(ev, SlMoveEvent)
        assert ev.cause == "ladder"
        assert ev.old_sl == 95.0 and ev.new_sl == 100.0 and ev.next_tp == 110.0
        assert bridge._rr_known[0] == 1

    def test_sl_move_trailing_cause(self):
        pos = _paper_pos(stop_loss=99.0, last_rr_hit=0)
        bridge, seen, _ = _bridge_kit(_paper_cfg(sl_mode="trailing"),
                                      positions=[pos])
        bridge._rr_known[0] = 0
        bridge.on_event({"type": EVENT_SL_MOVE, "time": _p_ts(11),
                         "symbol": "BTCUSDT", "trade_id": 0,
                         "old_sl": 95.0, "new_sl": 99.0, "next_tp": None})
        assert seen[0].cause == "trailing"

    def test_risk_free_scan_emits_once(self):
        cfg = _paper_cfg(tp_mode=TP_MODE_NONE, risk_free_enabled=True,
                         risk_free_at_rr=1.5)
        pos = _paper_pos()
        bridge, seen, _ = _bridge_kit(cfg, positions=[pos], ts=_p_ts(11))
        bridge.on_event(self._open_dict())
        pos.risk_free_triggered = True
        pos.stop_loss = 100.0
        bridge.on_report(None)
        rf = [e for e in seen if isinstance(e, RiskFreeEvent)]
        assert len(rf) == 1
        assert rf[0].time == _p_ts(11) and rf[0].symbol == "BTCUSDT"
        assert rf[0].source == SOURCE_SIM
        assert rf[0].rr_level == 1.5
        assert rf[0].old_sl == 95.0 and rf[0].new_sl == 100.0
        bridge.on_report(None)                      # second scan: no duplicate
        assert len([e for e in seen if isinstance(e, RiskFreeEvent)]) == 1

    def test_risk_free_scan_gated_by_config(self):
        pos = _paper_pos(risk_free_triggered=True, stop_loss=100.0)
        for cfg in (
            _paper_cfg(),                                        # rf disabled
            _paper_cfg(tp_mode=TP_MODE_MULTI_RR, tp_levels=[1.0, 2.0],
                       risk_free_enabled=True),                  # multi-RR
        ):
            bridge, seen, _ = _bridge_kit(cfg, positions=[pos])
            bridge.on_report(None)
            assert seen == []

    def test_prime_snapshots_seed_positions(self):
        cfg = _paper_cfg(tp_mode=TP_MODE_NONE, risk_free_enabled=True)
        pos = _paper_pos(risk_free_triggered=True, stop_loss=100.0,
                         last_rr_hit=0)
        bridge, seen, _ = _bridge_kit(cfg, positions=[pos])
        bridge.prime()
        assert bridge._sl_known[0] == 100.0
        assert bridge._rf_known[0] is True
        bridge.on_report(None)                      # already seen at prime time
        assert seen == []

    def test_unknown_event_type_ignored(self):
        bridge, seen, _ = _bridge_kit()
        bridge.on_event({"type": "someday", "time": _p_ts(10),
                         "symbol": "BTCUSDT"})
        assert seen == []


class TestRunLiveLifecycleAndLog:
    def test_sim_tagged_event_lines(self, capsys):
        broker, strategy = _tp_lifecycle()
        run_live(strategy=strategy, broker=broker, config=_paper_cfg())
        out = capsys.readouterr().out
        lines = [ln for ln in out.splitlines() if "[SIM][BTCUSDT]" in ln]
        sig = next(ln for ln in lines if " SIGNAL " in ln)
        opn = next(ln for ln in lines if " OPEN " in ln)
        clo = next(ln for ln in lines if " CLOSE " in ln)
        assert "SIGNAL long @ 100 sl=95 tp=110 rr=2.00 tf=1h" in sig
        assert "OPEN #0 long filled @ 100" in opn
        assert "order=" not in opn                  # sim fill: no venue order id
        assert "CLOSE #0 @ 110 reason=tp" in clo
        assert lines.index(sig) < lines.index(opn) < lines.index(clo)
        assert "[LIVE]" not in out

    def test_feed_end_notes_printed(self, capsys):
        broker, strategy = _tp_lifecycle()
        run_live(strategy=strategy, broker=broker, config=_paper_cfg())
        out = capsys.readouterr().out
        assert "paper trading BTCUSDT" in out
        assert "feed for BTCUSDT ended" in out
        assert "all feeds ended" in out

    def test_ctrl_c_stops_cleanly(self, capsys):
        broker = _PaperBroker(_p_flat(10))          # unscripted: stream stays alive
        strategy = _PaperPlanStrategy({})

        def _interrupt():
            broker.subscribed.wait(5.0)
            time.sleep(0.15)
            _thread.interrupt_main()

        threading.Thread(target=_interrupt, daemon=True).start()
        report = run_live(strategy=strategy, broker=broker, config=_paper_cfg())
        assert isinstance(report, SimulateReport)
        assert broker.streams and broker.streams[0].alive is False
        assert "Ctrl+C — stopping" in capsys.readouterr().out

    def test_risk_free_line_end_to_end(self, capsys):
        seed = _p_flat(10)
        live = [
            _p_candle(10, 100.0, 100.5, 99.5, 100.0),     # signal + open @ 100
            _p_candle(11, 100.1, 105.2, 100.05, 104.0),   # +1R touched → BE jump,
        ]                                                 # low stays above BE
        plan = {_p_ts(10): [{"entry": 100.0, "sl": 95.0}]}
        broker = _PaperBroker(seed, live_rows=live)
        cfg = _paper_cfg(tp_mode=TP_MODE_NONE, risk_free_enabled=True,
                         risk_free_at_rr=1.0)
        run_live(strategy=_PaperPlanStrategy(plan), broker=broker, config=cfg)
        out = capsys.readouterr().out
        assert "RISK_FREE #0 rr=1.00 touched, sl 95 -> 100 (break-even)" in out
        assert broker.order_calls == []

    def test_trailing_sl_move_line_end_to_end(self, capsys):
        seed = _p_flat(10)
        live = [
            _p_candle(10, 100.0, 100.5, 99.5, 100.0),    # signal + open @ 100
            _p_candle(11, 102.5, 103.0, 102.0, 102.8),   # peak 103 → SL 101.97
            _p_candle(12, 102.8, 106.0, 102.5, 105.5),   # peak 106 → SL hit
        ]
        plan = {_p_ts(10): [{"entry": 100.0, "sl": 95.0}]}
        broker = _PaperBroker(seed, live_rows=live)
        cfg = _paper_cfg(tp_mode=TP_MODE_NONE, sl_mode="trailing",
                         trailing_sl_percent=1.0)
        report = run_live(strategy=_PaperPlanStrategy(plan), broker=broker,
                          config=cfg)
        out = capsys.readouterr().out
        assert "SL_MOVE #0 sl 95 -> 101.97 (trailing)" in out
        assert "CLOSE #0 @ 104.94 reason=sl" in out
        assert [t.close_reason for t in report.closed_trades] == ["sl"]

    def test_multi_rr_ladder_lines_end_to_end(self, capsys):
        seed = _p_flat(10)
        live = [
            _p_candle(10, 100.0, 100.5, 99.5, 100.0),    # signal + open @ 100
            _p_candle(11, 100.2, 105.3, 100.1, 105.0),   # L1 (105): close 50%, SL→BE
            _p_candle(12, 105.0, 110.4, 104.0, 110.0),   # L2 (110): close remainder
        ]
        plan = {_p_ts(10): [{"entry": 100.0, "sl": 95.0}]}
        broker = _PaperBroker(seed, live_rows=live)
        cfg = _paper_cfg(tp_mode=TP_MODE_MULTI_RR, tp_levels=[1.0, 2.0],
                         tp_level_close_fractions=[0.5, 0.5])
        report = run_live(strategy=_PaperPlanStrategy(plan), broker=broker,
                          config=cfg)
        out = capsys.readouterr().out
        tp_line = next(ln for ln in out.splitlines() if "TP_LEVEL" in ln)
        assert "TP_LEVEL #0 rr=1.00 hit closed=50% pnl=$50 new_sl=100" in tp_line
        assert "SL_MOVE" not in out          # the ladder move rode the TP_LEVEL line
        assert "CLOSE #0 @ 110 reason=tp gross=" in out   # final-level remainder
        assert report.total_trades == 2      # two slices sharing one trade_id
        assert {t.trade_id for t in report.closed_trades} == {0}
        assert [t.close_reason for t in report.closed_trades] == ["tp_rr", "tp"]


class TestRunLiveDisplay:
    @pytest.fixture(autouse=True)
    def _no_keep_alive(self, monkeypatch):
        import AlgoTradeKit.simulate._live as live_mod

        monkeypatch.setattr(live_mod, "_register_keep_alive", lambda: None)

    def test_display_urls_printed_when_browser_off(self, capsys):
        seed = _p_flat(10)
        live = [_p_candle(10, 100.0, 100.5, 99.5, 100.0)]
        broker = _PaperBroker(seed, live_rows=live)
        cfg = _paper_cfg(display=True, display_candles=6,
                         display_open_browser=False)
        report = run_live(strategy=_PaperPlanStrategy({}), broker=broker,
                          config=cfg)
        out = capsys.readouterr().out
        assert "BTCUSDT chart  → http://127.0.0.1:" in out
        assert "BTCUSDT report → http://127.0.0.1:" in out
        assert isinstance(report, SimulateReport)
        assert ("stream", "BTCUSDT", "1h", False) in broker.calls  # forming feed


# ---------------------------------------------------------------------------
# — exact candle-close scheduler
# ---------------------------------------------------------------------------


_TF = 300_000                    # 5m in ms
_P0 = 1_700_000_047_000          # deliberately OFF the epoch grid — phase comes from the venue


class _SchedClock:
    """Scripted clock pair: venue time is primary, ``local = venue - offset``."""

    def __init__(self, venue_ms: int, offset_ms: int = 0):
        self.venue_ms = int(venue_ms)
        self.offset_ms = int(offset_ms)

    @property
    def local_ms(self) -> int:
        return self.venue_ms - self.offset_ms

    def advance(self, ms: int) -> None:
        self.venue_ms += int(ms)


class _SchedVenue:
    """Fake broker for the scheduler: a bar becomes visible at its ``avail_ms``
    (venue clock) — mock venue that lags candle creation."""

    def __init__(self, clock: _SchedClock):
        self.clock = clock
        self._bars: dict[int, int] = {}        # open_ms -> avail_ms
        self.offset_calls = 0
        self.tail_calls: list[tuple] = []
        self.range_calls: list[tuple] = []
        self.fail_next = 0                     # raise on the next N fetches

    def add_bar(self, open_ms: int, avail_ms: int | None = None) -> None:
        self._bars[int(open_ms)] = int(open_ms if avail_ms is None else avail_ms)

    def _visible(self) -> list[dict]:
        now = self.clock.venue_ms
        return [
            {"timestamp": ts, "open": 1.0, "high": 2.0, "low": 0.5,
             "close": 1.5, "volume": 10.0}
            for ts, avail in sorted(self._bars.items())
            if avail <= now
        ]

    def _maybe_fail(self) -> None:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise ConnectionFailed("scripted fetch failure")

    def clock_offset_ms(self, *, force_refresh: bool = False) -> int:
        self.offset_calls += 1
        return self.clock.offset_ms

    def fetch_last_candles(self, symbol: str, timeframe: str, count: int) -> list[dict]:
        self._maybe_fail()
        self.tail_calls.append((symbol, timeframe, count))
        return self._visible()[-count:]

    def fetch_candles(self, symbol: str, timeframe: str,
                      start_ms: int, end_ms: int) -> list[dict]:
        self._maybe_fail()
        self.range_calls.append((symbol, timeframe, start_ms, end_ms))
        return [c for c in self._visible() if start_ms <= c["timestamp"] <= end_ms]


def _make_sched(monkeypatch, *, venue_start: int, offset_ms: int = 0,
                last_open: int | None = _P0, tf: str = "5m", **knobs) -> SimpleNamespace:
    """Scheduler + scripted clock/venue; ``_wait`` advances the clock instead
    of sleeping, so every test is deterministic and instant."""
    clock = _SchedClock(venue_start, offset_ms)
    venue = _SchedVenue(clock)
    monkeypatch.setattr(sched_module, "now_ms", lambda: clock.local_ms)
    lines: list[str] = []
    sched = CandleCloseScheduler(
        venue, "TESTUSDT", tf, last_open_ms=last_open, log=lines.append, **knobs
    )
    waits: list[float] = []

    def fake_wait(seconds: float) -> bool:
        waits.append(seconds)
        clock.advance(round(seconds * 1000))
        return sched._stop_event.is_set()

    sched._wait = fake_wait
    return SimpleNamespace(clock=clock, venue=venue, sched=sched, lines=lines, waits=waits)


class TestSchedulerArithmetic:
    def test_timeframe_ms_values(self):
        assert timeframe_ms("1m") == 60_000
        assert timeframe_ms("5m") == 300_000
        assert timeframe_ms("1h") == 3_600_000
        assert timeframe_ms("1H") == 3_600_000          # normalised
        assert timeframe_ms("1w") == 7 * 24 * 3_600_000

    def test_timeframe_ms_rejects_variable_and_unknown(self):
        with pytest.raises(ValueError, match="no fixed length"):
            timeframe_ms("1M")
        with pytest.raises(ValueError, match="Unsupported timeframe"):
            timeframe_ms("7m")

    def test_candle_close_ms(self):
        assert candle_close_ms(1_000, 300) == 1_300

    def test_finality_is_arithmetic_at_the_exact_boundary(self):
        # A candle is final once venue_now >= open + tf.  No candle rows are
        # involved at all — "a newer row appeared" is never the criterion.
        t = _P0
        assert is_candle_final(t, _TF, t + _TF) is True         # final at close, exactly
        assert is_candle_final(t, _TF, t + _TF - 1) is False    # 1 ms early: not final

    def test_next_boundary_ms(self):
        assert next_boundary_ms(1_000_000, 300) == 1_000_600

    def test_newest_final_open_ms(self):
        p = _P0
        assert newest_final_open_ms(p, _TF, p + _TF - 1) is None
        assert newest_final_open_ms(p, _TF, p + _TF) == p
        assert newest_final_open_ms(p, _TF, p + 2 * _TF - 1) == p
        assert newest_final_open_ms(p, _TF, p + 2 * _TF) == p + _TF
        assert newest_final_open_ms(p, _TF, p + 9 * _TF + 5) == p + 8 * _TF


class TestSchedulerWaitFire:
    def test_sleeps_to_boundary_and_fires_exactly(self, monkeypatch):
        kit = _make_sched(monkeypatch, venue_start=_P0 + _TF + 60_000)
        kit.venue.add_bar(_P0 + _TF)              # closes exactly at the boundary
        kit.venue.add_bar(_P0 + 2 * _TF)          # forming bar
        tick = kit.sched.wait_next_close()
        boundary = _P0 + 2 * _TF
        assert tick.boundary_ms == boundary
        assert kit.clock.venue_ms == boundary     # fired the instant the venue clock hit it
        assert tick.fired_at_ms == boundary
        assert tick.late_ms == 0
        assert [c["timestamp"] for c in tick.candles] == [_P0 + _TF]
        assert tick.complete and tick.retries == 0 and tick.fetch_delay_ms == 0
        assert kit.sched.last_open_ms == _P0 + _TF

    def test_venue_clock_is_used_not_local(self, monkeypatch):
        offset = 120_000                          # venue runs 2 min ahead of local
        kit = _make_sched(monkeypatch, venue_start=_P0 + _TF + 60_000, offset_ms=offset)
        kit.venue.add_bar(_P0 + _TF)
        tick = kit.sched.wait_next_close()
        boundary = _P0 + 2 * _TF
        assert kit.clock.venue_ms == boundary            # fire keyed to the venue clock…
        assert kit.clock.local_ms == boundary - offset   # …while local is still 2 min early
        assert [c["timestamp"] for c in tick.candles] == [_P0 + _TF]
        # offset refreshed exactly once per wait — never inside the fine loop
        assert kit.venue.offset_calls == 1

    def test_late_fire_detected_and_logged(self, monkeypatch):
        kit = _make_sched(monkeypatch, venue_start=_P0 + 2 * _TF + 90_000)
        kit.venue.add_bar(_P0 + _TF)
        tick = kit.sched.wait_next_close()
        assert kit.waits == []                    # already past the boundary: no sleeping
        assert tick.late_ms == 90_000
        assert tick.fired_at_ms == _P0 + 2 * _TF + 90_000
        assert any("late fire: 90000 ms" in ln for ln in kit.lines)
        assert [c["timestamp"] for c in tick.candles] == [_P0 + _TF]

    def test_small_lateness_not_warned(self, monkeypatch):
        kit = _make_sched(monkeypatch, venue_start=_P0 + 2 * _TF + 300)
        kit.venue.add_bar(_P0 + _TF)
        tick = kit.sched.wait_next_close()
        assert tick.late_ms == 300                # below late_warn_ms (default 1000)
        assert not any("late fire" in ln for ln in kit.lines)

    def test_suspend_safe_coarse_recompute(self, monkeypatch):
        # A wait that wakes early (suspend, spurious wake) must loop and still
        # fire exactly at the boundary, never before.
        kit = _make_sched(monkeypatch, venue_start=_P0 + _TF + 60_000)
        kit.venue.add_bar(_P0 + _TF)
        half_waits: list[float] = []

        def half_wait(seconds: float) -> bool:
            half_waits.append(seconds)
            kit.clock.advance(max(1, round(seconds * 1000 / 2)))
            return False

        kit.sched._wait = half_wait
        tick = kit.sched.wait_next_close()
        assert kit.clock.venue_ms == _P0 + 2 * _TF
        assert tick.late_ms == 0
        assert len(half_waits) > 2                # the loop re-computed the remainder

    def test_stopped_before_wait_returns_none(self, monkeypatch):
        kit = _make_sched(monkeypatch, venue_start=_P0 + _TF + 60_000)
        kit.sched.stop()
        assert kit.sched.stopped is True
        assert kit.sched.wait_next_close() is None

    def test_stop_during_sleep_returns_none(self, monkeypatch):
        kit = _make_sched(monkeypatch, venue_start=_P0 + _TF + 60_000)
        kit.venue.add_bar(_P0 + _TF)
        inner = kit.sched._wait

        def stopping_wait(seconds: float) -> bool:
            kit.sched.stop()
            return inner(seconds)

        kit.sched._wait = stopping_wait
        assert kit.sched.wait_next_close() is None

    def test_stop_interrupts_a_real_sleep(self):
        # Real Event, real clock: the boundary is ~1h away; stop() must abort
        # the coarse sleep promptly.
        venue = _SchedVenue(_SchedClock(0))
        tf_ms = timeframe_ms("1h")
        now_real = int(time.time() * 1000)
        sched = CandleCloseScheduler(
            venue, "T", "1h", last_open_ms=now_real - tf_ms, log=lambda _s: None
        )
        threading.Timer(0.15, sched.stop).start()
        t0 = time.monotonic()
        assert sched.wait_next_close() is None
        assert time.monotonic() - t0 < 5.0


class TestSchedulerFetchQuirk:
    def test_expected_bar_evaluated_without_newer_row(self, monkeypatch):
        # THE venue quirk: at the boundary the venue shows the just-closed bar
        # and NO newer row (no trade yet in the new interval) — the bar is
        # still evaluated immediately, by arithmetic alone.
        kit = _make_sched(monkeypatch, venue_start=_P0 + _TF + 60_000)
        kit.venue.add_bar(_P0 + _TF)              # no forming bar ever appears
        tick = kit.sched.wait_next_close()
        assert [c["timestamp"] for c in tick.candles] == [_P0 + _TF]
        assert tick.complete and tick.retries == 0

    def test_forming_bar_excluded_arithmetically(self, monkeypatch):
        kit = _make_sched(monkeypatch, venue_start=_P0 + 2 * _TF + 30_000)
        kit.venue.add_bar(_P0 + _TF)
        kit.venue.add_bar(_P0 + 2 * _TF)          # forming: visible but not final
        tick = kit.sched.wait_next_close()
        assert [c["timestamp"] for c in tick.candles] == [_P0 + _TF]
        assert tick.complete

    def test_venue_lag_retried_until_bar_arrives(self, monkeypatch):
        kit = _make_sched(monkeypatch, venue_start=_P0 + _TF + 60_000)
        boundary = _P0 + 2 * _TF
        # The venue prints the closed bar 700 ms after its close.
        kit.venue.add_bar(_P0 + _TF, avail_ms=boundary + 700)
        tick = kit.sched.wait_next_close()
        assert [c["timestamp"] for c in tick.candles] == [_P0 + _TF]
        assert tick.complete
        assert tick.retries == 2                  # miss at fire, miss at +250 ms, hit at +750 ms
        assert tick.fetch_delay_ms == 750
        assert any("arrived 750 ms after the boundary (2 retries)" in ln for ln in kit.lines)

    def test_retry_backoff_doubles_up_to_cap(self, monkeypatch):
        kit = _make_sched(monkeypatch, venue_start=_P0 + 2 * _TF + 100,
                          retry_cap_ms=1_000, fetch_timeout=10.0)
        tick = kit.sched.wait_next_close()        # expected bar never prints
        assert kit.waits[:3] == [0.25, 0.5, 1.0]  # doubling…
        assert max(kit.waits) == 1.0              # …capped at retry_cap_ms
        assert tick.complete is False
        assert tick.retries == len(kit.waits)

    def test_timeout_gives_up_incomplete(self, monkeypatch):
        kit = _make_sched(monkeypatch, venue_start=_P0 + 2 * _TF + 100, fetch_timeout=2.0)
        tick = kit.sched.wait_next_close()
        assert tick is not None
        assert tick.complete is False and tick.candles == ()
        assert kit.sched.last_open_ms == _P0      # never advanced past the missing bar
        assert tick.fetch_delay_ms == 2_000
        assert any("giving up" in ln for ln in kit.lines)

    def test_give_up_defers_to_next_grid_boundary(self, monkeypatch):
        # Weekend semantics: one bounded attempt per boundary, then quiet
        # until the NEXT grid point — no immediate-refire spin.
        kit = _make_sched(monkeypatch, venue_start=_P0 + 2 * _TF + 100, fetch_timeout=2.0)
        tick1 = kit.sched.wait_next_close()
        assert tick1.complete is False
        tick2 = kit.sched.wait_next_close()
        assert tick2.boundary_ms == _P0 + 3 * _TF  # next future grid point, not a refire
        assert tick2.late_ms == 0
        assert tick2.complete is False             # still nothing printed

    def test_absence_proof_skips_trade_less_interval(self, monkeypatch):
        kit = _make_sched(monkeypatch, venue_start=_P0 + _TF + 60_000)
        kit.venue.add_bar(_P0 + 2 * _TF)          # next interval printed; expected never traded
        tick = kit.sched.wait_next_close()
        assert tick.complete and tick.candles == () and tick.retries == 0
        assert kit.sched.last_open_ms == _P0 + _TF   # grid skipped past the dead interval
        assert any("no candle (no trades)" in ln for ln in kit.lines)
        # Continuation: the printed bar arrives normally at its own boundary.
        tick2 = kit.sched.wait_next_close()
        assert tick2.boundary_ms == _P0 + 3 * _TF
        assert [c["timestamp"] for c in tick2.candles] == [_P0 + 2 * _TF]
        assert tick2.late_ms == 0

    def test_hole_at_top_returns_partial_then_resolves(self, monkeypatch):
        # Downtime over three intervals; the newest missed interval printed
        # nothing.  Tick 1 returns what exists; tick 2 resolves the dead
        # interval as complete-empty via the later-bar proof.
        kit = _make_sched(monkeypatch, venue_start=_P0 + 3 * _TF + 20_000)
        kit.venue.add_bar(_P0 + _TF)
        kit.venue.add_bar(_P0 + 3 * _TF)          # forming bar = the proof
        tick1 = kit.sched.wait_next_close()
        assert [c["timestamp"] for c in tick1.candles] == [_P0 + _TF]
        assert tick1.complete and kit.sched.last_open_ms == _P0 + _TF
        tick2 = kit.sched.wait_next_close()
        assert tick2.candles == () and tick2.complete
        assert kit.sched.last_open_ms == _P0 + 2 * _TF
        tick3 = kit.sched.wait_next_close()       # the forming bar closes on time
        assert tick3.boundary_ms == _P0 + 4 * _TF
        assert [c["timestamp"] for c in tick3.candles] == [_P0 + 3 * _TF]

    def test_gap_fill_uses_range_fetch(self, monkeypatch):
        kit = _make_sched(monkeypatch, venue_start=_P0 + 8 * _TF + 10_000)
        for k in range(1, 8):
            kit.venue.add_bar(_P0 + k * _TF)
        tick = kit.sched.wait_next_close()
        assert len(kit.venue.range_calls) == 1    # missed 7 >= tail 5 → one ranged fetch
        sym, tf, start, end = kit.venue.range_calls[0]
        assert (sym, tf) == ("TESTUSDT", "5m")
        assert start == _P0 + _TF                 # exactly the gap — never the full history
        assert end == kit.clock.venue_ms
        assert kit.venue.tail_calls == []
        assert [c["timestamp"] for c in tick.candles] == [_P0 + k * _TF for k in range(1, 8)]
        assert tick.complete
        assert kit.sched.last_open_ms == _P0 + 7 * _TF
        assert any("gap-fill: 7 candle(s)" in ln for ln in kit.lines)
        assert any("late fire" in ln for ln in kit.lines)

    def test_small_catchup_uses_tail_fetch(self, monkeypatch):
        kit = _make_sched(monkeypatch, venue_start=_P0 + 3 * _TF + 10_000)
        kit.venue.add_bar(_P0 + _TF)
        kit.venue.add_bar(_P0 + 2 * _TF)
        tick = kit.sched.wait_next_close()
        assert kit.venue.tail_calls == [("TESTUSDT", "5m", DEFAULT_TAIL_COUNT)]
        assert kit.venue.range_calls == []
        assert [c["timestamp"] for c in tick.candles] == [_P0 + _TF, _P0 + 2 * _TF]
        assert tick.complete and kit.sched.last_open_ms == _P0 + 2 * _TF

    def test_fetch_error_retried_then_succeeds(self, monkeypatch):
        kit = _make_sched(monkeypatch, venue_start=_P0 + 2 * _TF + 100)
        kit.venue.add_bar(_P0 + _TF)
        kit.venue.fail_next = 2
        tick = kit.sched.wait_next_close()
        assert tick.complete
        assert [c["timestamp"] for c in tick.candles] == [_P0 + _TF]
        assert tick.retries == 2
        assert sum("fetch failed" in ln for ln in kit.lines) == 2

    def test_late_bar_recovered_by_next_gap_fill(self, monkeypatch):
        # Give-up never advances past the missing bar, so a very late print is
        # picked up (in order) by the next boundary's fetch.
        kit = _make_sched(monkeypatch, venue_start=_P0 + _TF + 60_000, fetch_timeout=2.0)
        tick1 = kit.sched.wait_next_close()       # bar missing → timeout give-up
        assert tick1.complete is False and kit.sched.last_open_ms == _P0
        kit.venue.add_bar(_P0 + _TF, avail_ms=kit.clock.venue_ms + 1_000)  # prints very late
        kit.venue.add_bar(_P0 + 2 * _TF)
        tick2 = kit.sched.wait_next_close()
        assert [c["timestamp"] for c in tick2.candles] == [_P0 + _TF, _P0 + 2 * _TF]
        assert tick2.complete and kit.sched.last_open_ms == _P0 + 2 * _TF

    def test_grid_re_anchors_after_venue_rephase(self, monkeypatch):
        # DST-style re-phase: the venue starts printing 1h bars 30 min off the
        # old grid.  The scheduler re-anchors to the real bar opens and is
        # back to exact-time fires one boundary later.
        tf1h = 3_600_000
        s = _P0 + tf1h + 1_800_000                # first re-phased bar (+30 min)
        kit = _make_sched(monkeypatch, tf="1h", venue_start=_P0 + tf1h + 60_000)
        kit.venue.add_bar(s)
        kit.venue.add_bar(s + tf1h)
        tick1 = kit.sched.wait_next_close()       # old-grid interval dead → proof, empty
        assert tick1.candles == () and tick1.complete
        assert kit.sched.last_open_ms == _P0 + tf1h
        tick2 = kit.sched.wait_next_close()       # returns the re-phased bar
        assert [c["timestamp"] for c in tick2.candles] == [s]
        assert kit.sched.last_open_ms == s        # grid re-anchored to the venue's new phase
        tick3 = kit.sched.wait_next_close()       # new grid, exact-time again
        assert tick3.boundary_ms == s + 2 * tf1h
        assert [c["timestamp"] for c in tick3.candles] == [s + tf1h]
        assert tick3.late_ms == 0

    def test_baseline_when_no_last_open(self, monkeypatch):
        kit = _make_sched(monkeypatch, last_open=None,
                          venue_start=_P0 + 2 * _TF + 60_000)
        for k in range(0, 3):
            kit.venue.add_bar(_P0 + k * _TF)      # P0, P0+tf final; P0+2tf forming
        tick = kit.sched.wait_next_close()
        # Baselined on the newest FINAL bar — history never fires.
        assert any("baseline: newest closed candle open=" in ln for ln in kit.lines)
        assert tick.boundary_ms == _P0 + 3 * _TF
        assert [c["timestamp"] for c in tick.candles] == [_P0 + 2 * _TF]
        assert tick.late_ms == 0

    def test_baseline_without_final_candle_raises(self, monkeypatch):
        kit = _make_sched(monkeypatch, last_open=None, venue_start=_P0 + 100)
        kit.venue.add_bar(_P0)                    # forming only — not final yet
        with pytest.raises(ValueError, match="no closed candle"):
            kit.sched.wait_next_close()

    def test_candles_strictly_after_last_open(self, monkeypatch):
        kit = _make_sched(monkeypatch, venue_start=_P0 + _TF + 60_000)
        kit.venue.add_bar(_P0 - _TF)              # history in the tail…
        kit.venue.add_bar(_P0)                    # …incl. the already-handled bar
        kit.venue.add_bar(_P0 + _TF)
        tick = kit.sched.wait_next_close()
        assert [c["timestamp"] for c in tick.candles] == [_P0 + _TF]


class TestSchedulerConfigAndLogging:
    def test_ctor_validation(self):
        venue = _SchedVenue(_SchedClock(0))
        base = {"symbol": "BTCUSDT", "timeframe": "5m"}
        cases = [
            ({"broker": None}, "needs a broker"),
            ({"symbol": ""}, "non-empty"),
            ({"symbol": "   "}, "non-empty"),
            ({"symbol": 123}, "non-empty"),
            ({"timeframe": "1M"}, "no fixed length"),
            ({"timeframe": "7m"}, "Unsupported timeframe"),
            ({"tail_count": 0}, "tail_count"),
            ({"fine_wait_ms": 0}, "fine_wait_ms / fine_tick_ms"),
            ({"fine_tick_ms": 0}, "fine_wait_ms / fine_tick_ms"),
            ({"retry_initial_ms": 0}, "retry_initial_ms"),
            ({"retry_cap_ms": 100}, "retry_cap_ms"),   # < default retry_initial_ms
            ({"fetch_timeout": 0}, "fetch_timeout"),
            ({"late_warn_ms": -1}, "late_warn_ms"),
            ({"log": 42}, "callable"),
        ]
        for overrides, match in cases:
            kwargs = {"broker": venue, **base, **overrides}
            with pytest.raises(ValueError, match=match):
                CandleCloseScheduler(
                    kwargs.pop("broker"), kwargs.pop("symbol"), kwargs.pop("timeframe"),
                    **kwargs,
                )

    def test_defaults_are_module_constants(self):
        venue = _SchedVenue(_SchedClock(0))
        sched = CandleCloseScheduler(venue, "BTCUSDT", "5m")
        assert (DEFAULT_TAIL_COUNT, DEFAULT_FINE_WAIT_MS, DEFAULT_FINE_TICK_MS,
                DEFAULT_RETRY_INITIAL_MS, DEFAULT_RETRY_CAP_MS, DEFAULT_FETCH_TIMEOUT,
                DEFAULT_LATE_WARN_MS) == (5, 500, 10, 250, 5_000, 30.0, 1_000)
        assert sched._tail_count == DEFAULT_TAIL_COUNT
        assert sched._fine_wait_ms == DEFAULT_FINE_WAIT_MS
        assert sched._fine_tick_ms == DEFAULT_FINE_TICK_MS
        assert sched._retry_initial_ms == DEFAULT_RETRY_INITIAL_MS
        assert sched._retry_cap_ms == DEFAULT_RETRY_CAP_MS
        assert sched._fetch_timeout == DEFAULT_FETCH_TIMEOUT
        assert sched._late_warn_ms == DEFAULT_LATE_WARN_MS

    def test_symbol_and_timeframe_normalised(self):
        venue = _SchedVenue(_SchedClock(0))
        sched = CandleCloseScheduler(venue, "  BTCUSDT  ", "1H")
        assert sched.symbol == "BTCUSDT"
        assert sched.timeframe == "1h"
        assert sched.last_open_ms is None

    def test_default_log_prints(self, capsys):
        venue = _SchedVenue(_SchedClock(0))
        sched = CandleCloseScheduler(venue, "BTCUSDT", "5m")
        sched._log("hello")
        assert capsys.readouterr().out.strip() == \
            "[AlgoTradeKit] scheduler BTCUSDT 5m: hello"

    def test_venue_now_ms_applies_offset(self, monkeypatch):
        kit = _make_sched(monkeypatch, venue_start=_P0, offset_ms=5_000)
        assert kit.sched.venue_now_ms() == kit.clock.venue_ms
        assert kit.sched.venue_now_ms() == kit.clock.local_ms + 5_000

    def test_tick_is_frozen(self):
        tick = SchedulerTick(boundary_ms=1, fired_at_ms=1, late_ms=0, candles=(),
                             retries=0, fetch_delay_ms=0, complete=True)
        with pytest.raises(dataclasses.FrozenInstanceError):
            tick.late_ms = 5


# ===========================================================================
# — execution core (entries + venue SL/TP)
# ===========================================================================

_X0 = 1_700_000_000_000          # first candle open (UTC ms)
_XHOUR = 3_600_000


def _x_cfg(**overrides) -> TraderConfig:
    """ config: explicit zero costs (venue never queried unless a test wants it)."""
    kwargs = {
        "symbol": "BTCUSDT", "min_candles": 10, "tp_mode": TP_MODE_NONE,
        "spread": 0.0, "commission_type": "percentage", "commission": 0.0,
    }
    kwargs.update(overrides)
    return TraderConfig(**kwargs)


def _x_sig(entry=100.0, sl=95.0, tp=None, direction="long", ts=_X0, index=0, rm=1.0) -> Signal:
    return Signal(direction, entry, sl, tp, ts, index, "1h",
                  metadata={}, risk_multiplier=rm)


def _x_candles(n, start_close=100.0, step=1.0):
    """Tight rising candles: trailing advances every bar, never stops out
    (low stays above both the entering SL and the same-bar advanced SL)."""
    out = []
    for k in range(n):
        close = start_close + k * step
        out.append({"timestamp": _X0 + k * _XHOUR, "open": close - 0.05,
                    "high": close + 0.1, "low": close - 0.1, "close": close,
                    "volume": 1.0})
    return out


class _XVenueBase:
    """Shared recording plumbing for the mock brokers."""

    def __init__(self, balance=10_000.0, fill_price=0.0):
        self.balance = balance
        self.fill_price = fill_price          # 0.0 → engine falls back to the reference
        self.orders: list[dict] = []
        self.cancels: list[str] = []
        self.fail_order_types: set[str] = set()   # create_order 'type' values that raise
        self.reject_order_types: set[str] = set()  # ... that answer STATUS_REJECTED
        self.fail_cancel = False
        self.ticker_last = 0.0
        self.fail_ticker = False
        self.costs = TradingCosts(commission_type="percentage", commission=0.0, spread=0.0)
        self.costs_calls: list[str] = []
        self._oid = itertools.count(1)

    def get_trading_costs(self, symbol):
        self.costs_calls.append(symbol)
        return self.costs

    def get_ticker(self, symbol):
        if self.fail_ticker:
            raise ConnectionFailed("ticker down")
        return Ticker(symbol=symbol, last=self.ticker_last)

    def create_order(self, symbol, side, quantity, **kwargs):
        order_id = str(next(self._oid))
        record = {"order_id": order_id, "symbol": symbol, "side": side,
                  "quantity": quantity, **kwargs}
        self.orders.append(record)
        order_type = kwargs.get("type")
        if order_type in self.fail_order_types:
            raise OrderError(f"scripted {order_type} failure")
        status = STATUS_REJECTED if order_type in self.reject_order_types else (
            STATUS_FILLED if order_type == "market" else STATUS_NEW)
        return OrderResult(
            order_id=order_id, symbol=symbol, side=side, status=status,
            filled_quantity=quantity if order_type == "market" else 0.0,
            avg_fill_price=self.fill_price if order_type == "market" else 0.0,
            raw={"scripted": True},
        )

    def cancel_order(self, order_id, symbol):
        if self.fail_cancel:
            raise OrderError("scripted cancel failure")
        self.cancels.append(order_id)
        return True

    def orders_of_type(self, order_type):
        return [o for o in self.orders if o.get("type") == order_type]


class _XFuturesBroker(_XVenueBase):
    market = MARKET_FUTURES
    name = "mock-futures"

    def __init__(self, **kw):
        super().__init__(**kw)
        self.leverage_calls: list[tuple] = []

    def set_leverage(self, symbol, leverage):
        self.leverage_calls.append((symbol, leverage))

    def get_account_info(self):
        return AccountInfo(currency="USDT", wallet_balance=self.balance,
                           equity=self.balance, available=self.balance)


class _XSpotBroker(_XVenueBase):
    market = MARKET_SPOT
    name = "mock-spot"

    def __init__(self, **kw):
        super().__init__(**kw)
        self.ocos: list[dict] = []
        self.list_cancels: list[str] = []
        self.fail_oco = False
        self.fail_cancel_list = False
        self._list_id = itertools.count(900)

    def get_balance(self):
        return [Balance(asset="BTC", free=1.0), Balance(asset="USDT", free=self.balance)]

    def create_oco_order(self, symbol, side, quantity, *, take_profit_price,
                         stop_price, stop_limit_price=None, client_order_id=None):
        record = {"symbol": symbol, "side": side, "quantity": quantity,
                  "take_profit_price": take_profit_price, "stop_price": stop_price,
                  "client_order_id": client_order_id}
        self.ocos.append(record)
        if self.fail_oco:
            raise OrderError("scripted OCO failure")
        return OrderResult(order_id=str(next(self._list_id)), symbol=symbol,
                           side=side, status=STATUS_NEW)

    def cancel_order_list(self, symbol, order_list_id):
        if self.fail_cancel_list:
            raise OrderError("scripted list-cancel failure")
        self.list_cancels.append(order_list_id)
        return True


class _XMT5Broker(_XVenueBase):
    market = MARKET_FOREX
    name = "mock-mt5"

    def __init__(self, **kw):
        super().__init__(**kw)
        self.modifies: list[tuple] = []
        self.closes: list[tuple] = []
        self.fail_modify = False
        self.last_comment = None
        self.positions_ticket = "7777"

    def get_account_info(self):
        return AccountInfo(currency="USD", wallet_balance=self.balance,
                           equity=self.balance, available=self.balance)

    def create_order(self, symbol, side, quantity, **kwargs):
        self.last_comment = kwargs.get("client_order_id")
        return super().create_order(symbol, side, quantity, **kwargs)

    def open_positions(self, symbol=None):
        return [Position(symbol=symbol or "EURUSD", side="long", quantity=0.2,
                         entry_price=1.1, position_id=self.positions_ticket,
                         raw={"comment": self.last_comment})]

    def modify_position(self, ticket, stop_loss=None, take_profit=None):
        self.modifies.append((ticket, stop_loss, take_profit))
        status = STATUS_REJECTED if self.fail_modify else STATUS_FILLED
        return OrderResult(order_id=str(ticket), symbol="", side="", status=status,
                           raw={"retcode": 10013 if self.fail_modify else 10009})

    def close_position(self, symbol, quantity=None, *, ticket=None):
        self.closes.append((symbol, quantity, ticket))
        if "close" in self.fail_order_types:
            raise OrderError("scripted close failure")
        return OrderResult(order_id="556", symbol=symbol, side="", status=STATUS_FILLED,
                           avg_fill_price=self.fill_price)


def _x_engine(broker=None, config=None, **engine_kw):
    """(engine, broker, events list) — engine already started."""
    broker = broker if broker is not None else _XFuturesBroker()
    config = config if config is not None else _x_cfg()
    events: list = []
    stream = EventStream()
    stream.subscribe(events.append)
    engine = ExecutionEngine(broker, config, events=stream,
                             trader_id=engine_kw.pop("trader_id", "abc123"), **engine_kw)
    engine.start()
    return engine, broker, events


def _x_events(events, event_type):
    return [e for e in events if e.event_type == event_type]


class TestExecutionEngineSetup:
    def test_futures_start_leverage_balance_and_costs(self):
        broker = _XFuturesBroker(balance=5_000.0)
        cfg = TraderConfig(symbol="BTCUSDT", min_candles=10, leverage=3.0)  # costs unset
        engine, broker, _ = _x_engine(broker, cfg)
        assert broker.leverage_calls == [("BTCUSDT", 3.0)]
        assert engine.start_balance == 5_000.0
        assert engine.sim_config.initial_balance == 5_000.0
        assert broker.costs_calls == ["BTCUSDT"]          # costs auto-derived
        assert engine.sim_config.leverage == 3.0

    def test_full_cost_override_skips_venue(self):
        engine, broker, _ = _x_engine()                    # _x_cfg overrides all costs
        assert broker.costs_calls == []
        assert engine.sim_config.spread == 0.0 and engine.sim_config.commission == 0.0

    def test_spot_rules_and_quote_asset(self):
        with pytest.raises(ValueError, match="leverage"):
            ExecutionEngine(_XSpotBroker(), _x_cfg(leverage=2.0), events=EventStream())
        engine, _, _ = _x_engine(_XSpotBroker())
        assert engine._quote_asset == "USDT"
        engine2, _, _ = _x_engine(_XSpotBroker(), quote_asset="usdt")
        assert engine2._quote_asset == "USDT"
        with pytest.raises(ValueError, match="quote_asset"):
            ExecutionEngine(_XSpotBroker(), _x_cfg(symbol="WEIRDPAIR"),
                            events=EventStream())

    def test_unsupported_market_and_bad_config(self):
        class NoMarket:
            market = "bonds"
        with pytest.raises(ValueError, match="market"):
            ExecutionEngine(NoMarket(), _x_cfg(), events=EventStream())
        with pytest.raises(TypeError, match="TraderConfig"):
            ExecutionEngine(_XFuturesBroker(), SimulateConfig(), events=EventStream())
        with pytest.raises(ValueError, match="trader_id"):
            ExecutionEngine(_XFuturesBroker(), _x_cfg(), events=EventStream(), trader_id="")

    def test_mt5_start_no_leverage_and_metatrader_maths(self):
        engine, broker, _ = _x_engine(
            _XMT5Broker(), _x_cfg(symbol="EURUSD", commission_type="per_lot"))
        assert not hasattr(broker, "leverage_calls")       # set_leverage never touched
        # duck-typed forex broker (not a MetaTraderBroker instance) still gets
        # the MT5 maths path — market wins over isinstance
        assert engine.sim_config.is_metatrader()

    def test_lifecycle_guards(self):
        engine, _, _ = _x_engine()
        with pytest.raises(RuntimeError, match="once"):
            engine.start()
        fresh = ExecutionEngine(_XFuturesBroker(), _x_cfg(), events=EventStream())
        with pytest.raises(RuntimeError, match="start"):
            fresh.open_position(_x_sig())
        with pytest.raises(RuntimeError, match="start"):
            fresh.update_stops(_x_candles(1)[0])
        with pytest.raises(RuntimeError, match="start"):
            fresh.force_close()
        broke = ExecutionEngine(_XFuturesBroker(balance=0.0), _x_cfg(),
                                events=EventStream())
        with pytest.raises(ValueError, match="balance"):
            broke.start()

    def test_trader_id_default_is_random_hex(self):
        engine = ExecutionEngine(_XFuturesBroker(), _x_cfg(), events=EventStream())
        assert len(engine.trader_id) == 6
        int(engine.trader_id, 16)                          # hex or raise


class TestExecutionEntries:
    def test_futures_entry_attaches_sl_only(self):
        engine, broker, events = _x_engine()
        trade = engine.open_position(_x_sig())             # 100/95, risk 1% of 10k
        assert trade is not None and engine.open_trades == (trade,)
        entry, sl = broker.orders
        assert entry["type"] == "market" and entry["side"] == "buy"
        assert entry["quantity"] == pytest.approx(20.0)    # 100$ risk / 5$ distance
        assert entry["client_order_id"] == f"atk-abc123-BTCUSDT-{_X0}"
        assert sl["type"] == "stop" and sl["side"] == "sell"
        assert sl["stop_price"] == 95.0 and sl["reduce_only"] is True
        assert sl["quantity"] == pytest.approx(20.0)
        assert sl["client_order_id"] == entry["client_order_id"] + "-sl"
        assert broker.orders_of_type("take_profit") == []  # tp_mode="none" → no TP sent
        assert trade.sl_order_id == sl["order_id"] and trade.tp_order_id is None

    def test_open_event_fields(self):
        engine, _, events = _x_engine()
        trade = engine.open_position(_x_sig())
        (open_event,) = _x_events(events, EVENT_OPEN)
        assert open_event.source == SOURCE_LIVE and open_event.symbol == "BTCUSDT"
        assert open_event.trade_id == 0 and open_event.direction == "long"
        assert open_event.fill_price == 100.0 and open_event.size == pytest.approx(20.0)
        assert open_event.stop_loss == 95.0 and open_event.next_tp is None
        assert open_event.order_id == trade.entry_order_id
        assert open_event.margin_amount == pytest.approx(2_000.0)   # 20 × 100 / lev 1
        assert open_event.risk_amount == pytest.approx(100.0)

    def test_take_profit_sent_only_when_mode_defines_one(self):
        # signal mode with a TP → the signal's TP goes venue-native
        engine, broker, _ = _x_engine(config=_x_cfg(tp_mode="signal"))
        trade = engine.open_position(_x_sig(tp=110.0))
        (tp,) = broker.orders_of_type("take_profit")
        assert tp["stop_price"] == 110.0 and tp["reduce_only"] is True
        assert tp["side"] == "sell"
        assert tp["client_order_id"].endswith("-tp")
        assert trade.venue_tp == 110.0 and trade.tp_order_id == tp["order_id"]
        # signal mode without a TP → nothing sent
        engine2, broker2, _ = _x_engine(config=_x_cfg(tp_mode="signal"))
        trade2 = engine2.open_position(_x_sig(tp=None))
        assert broker2.orders_of_type("take_profit") == [] and trade2.venue_tp is None

    def test_fixed_rr_tp_computed(self):
        engine, broker, _ = _x_engine(config=_x_cfg(tp_mode="fixed_rr", tp_rr=2.0))
        engine.open_position(_x_sig())                     # dist 5 → TP at 110
        (tp,) = broker.orders_of_type("take_profit")
        assert tp["stop_price"] == pytest.approx(110.0)

    def test_multi_rr_sends_no_tp_but_tracks_ladder(self):
        engine, broker, _ = _x_engine(config=_x_cfg(tp_mode=TP_MODE_MULTI_RR))
        trade = engine.open_position(_x_sig())
        assert broker.orders_of_type("take_profit") == []  # no TAKE_PROFIT order type
        assert trade.venue_tp is None
        assert trade.pos.tp_level_prices == [105.0, 110.0, 115.0]
        assert trade.pos.next_tp == 105.0
        assert trade.pos.sl_history[0] == {"time": _X0, "sl": 95.0, "next_tp": 105.0}
        # fraction-less ladder: ONE full-size reduce-only limit at the
        # FINAL level; intermediate levels stay feed-detected (SL-advance-only).
        (ladder,) = broker.orders_of_type("limit")
        assert ladder["price"] == 115.0 and ladder["quantity"] == trade.pos.original_size
        assert ladder["reduce_only"] is True
        assert ladder["client_order_id"] == trade.client_order_id + "-tp3"
        assert trade.ladder_order_ids == {ladder["order_id"]: 2}

    def test_futures_short_entry(self):
        engine, broker, _ = _x_engine()
        engine.open_position(_x_sig(entry=100.0, sl=105.0, direction="short"))
        entry, sl = broker.orders
        assert entry["side"] == "sell" and sl["side"] == "buy"
        assert sl["stop_price"] == 105.0

    def test_spot_oco_when_tp_stop_when_not_short_raises(self):
        engine, broker, _ = _x_engine(_XSpotBroker(), _x_cfg(tp_mode="signal"))
        trade = engine.open_position(_x_sig(tp=110.0))
        (oco,) = broker.ocos
        assert oco["side"] == "sell" and oco["take_profit_price"] == 110.0
        assert oco["stop_price"] == 95.0
        assert oco["client_order_id"] == trade.client_order_id + "-x"
        assert trade.oco_order_list_id == "900" and trade.sl_order_id is None

        engine2, broker2, _ = _x_engine(_XSpotBroker())    # tp_mode="none"
        trade2 = engine2.open_position(_x_sig())
        assert broker2.ocos == []
        (sl,) = broker2.orders_of_type("stop")
        assert sl["side"] == "sell" and sl["stop_price"] == 95.0
        assert trade2.sl_order_id == sl["order_id"] and trade2.oco_order_list_id is None

        with pytest.raises(OrderError, match="spot cannot short"):
            engine2.open_position(_x_sig(entry=100.0, sl=105.0, direction="short",
                                         ts=_X0 + _XHOUR, index=1))

    def test_mt5_atomic_sl_tp_and_ticket(self):
        engine, broker, _ = _x_engine(
            _XMT5Broker(fill_price=1.1),
            _x_cfg(symbol="EURUSD", tp_mode="signal", commission_type="per_lot"))
        trade = engine.open_position(_x_sig(entry=1.1000, sl=1.0950, tp=1.1100))
        (entry,) = broker.orders
        assert entry["stop_loss"] == 1.0950 and entry["take_profit"] == 1.1100
        # MT5 comment: seconds timestamp (31-char cap), rides client_order_id
        assert entry["client_order_id"] == f"atk-abc123-EURUSD-{_X0 // 1000}"
        assert trade.mt5_ticket == "7777"                  # matched by comment
        assert trade.pos.size == pytest.approx(0.2)        # 100$ risk / 50 pips / 10$

    def test_sizing_respects_compound_and_risk_multiplier(self):
        # compound=False: risk base = start balance even after the wallet grows
        engine, broker, _ = _x_engine()
        broker.balance = 20_000.0
        trade = engine.open_position(_x_sig())
        assert trade.pos.size == pytest.approx(20.0)       # still 1% of 10k
        # compound=True: risk base = current balance
        engine2, broker2, _ = _x_engine(config=_x_cfg(compound=True))
        broker2.balance = 20_000.0
        trade2 = engine2.open_position(_x_sig())
        assert trade2.pos.size == pytest.approx(40.0)      # 1% of 20k
        # risk_multiplier scales the risk unit
        engine3, _, _ = _x_engine()
        trade3 = engine3.open_position(_x_sig(rm=0.5))
        assert trade3.pos.size == pytest.approx(10.0)

    def test_position_limits_count_live_positions(self):
        engine, broker, events = _x_engine(config=_x_cfg(max_positions=1))
        assert engine.open_position(_x_sig()) is not None
        orders_before = len(broker.orders)
        assert engine.open_position(_x_sig(ts=_X0 + _XHOUR, index=1)) is None
        assert len(broker.orders) == orders_before         # venue never touched
        assert len(_x_events(events, EVENT_OPEN)) == 1

        engine2, _, _ = _x_engine(config=_x_cfg(
            max_positions=2, max_long_positions=2))
        t1 = engine2.open_position(_x_sig())
        t2 = engine2.open_position(_x_sig(ts=_X0 + _XHOUR, index=1))
        assert (t1.pos.trade_id, t2.pos.trade_id) == (0, 1)

    def test_unaffordable_position_skipped(self):
        engine, broker, _ = _x_engine()
        # dist 0.1 → size 1000 → margin 100k > 10k wallet → sizing returns None
        assert engine.open_position(_x_sig(entry=100.0, sl=99.9)) is None
        assert broker.orders == []

    def test_slippage_recorded_and_state_reanchored(self):
        engine, broker, _ = _x_engine(_XFuturesBroker(fill_price=101.0))
        trade = engine.open_position(_x_sig())             # reference fill = 100
        assert trade.reference_price == 100.0
        assert trade.fill_price == 101.0 and trade.slippage == pytest.approx(1.0)
        assert trade.pos.entry_price == 101.0              # break-even = real entry
        # $/price-unit is physical (20); risk re-measured off the real fill
        assert trade.pos.pnl_per_price_unit == pytest.approx(20.0)
        assert trade.pos.risk_amount == pytest.approx(20.0 * 6.0)
        assert trade.pos.margin_amount == pytest.approx(20.0 * 101.0)

    def test_entry_failure_paths(self):
        # entry order itself fails → ERROR + None, nothing tracked
        broker = _XFuturesBroker()
        broker.fail_order_types.add("market")
        engine, _, events = _x_engine(broker)
        assert engine.open_position(_x_sig()) is None
        assert engine.open_trades == ()
        (err,) = _x_events(events, EVENT_ERROR)
        assert err.where == "open_position" and "market failure" in err.message

    def test_naked_entry_emergency_closed_when_sl_attach_fails(self):
        broker = _XFuturesBroker()
        broker.fail_order_types.add("stop")
        engine, _, events = _x_engine(broker)
        assert engine.open_position(_x_sig()) is None
        assert engine.open_trades == ()
        market_orders = broker.orders_of_type("market")
        assert len(market_orders) == 2                     # entry + emergency close
        assert market_orders[1]["side"] == "sell"
        assert market_orders[1]["reduce_only"] is True
        (err,) = _x_events(events, EVENT_ERROR)
        assert err.where == "attach_protection"
        assert "emergency-closed" in err.message
        assert err.details["emergency_close"] == "done"

    def test_tp_attach_failure_keeps_protected_position(self):
        broker = _XFuturesBroker()
        broker.fail_order_types.add("take_profit")
        engine, _, events = _x_engine(broker, _x_cfg(tp_mode="signal"))
        trade = engine.open_position(_x_sig(tp=110.0))
        assert trade is not None and engine.open_trades == (trade,)
        assert trade.sl_order_id is not None and trade.tp_order_id is None
        (err,) = _x_events(events, EVENT_ERROR)
        assert err.where == "attach_take_profit" and "stop-loss armed" in err.message


class TestExecutionStops:
    def test_trailing_sequence_equals_simulate_sl_history(self):
        candles = _x_candles(10)
        sig = _x_sig(entry=100.0, sl=99.0)
        cfg = _x_cfg(sl_mode="trailing", trailing_sl_percent=1.0)
        engine, broker, _ = _x_engine(config=cfg)
        trade = engine.open_position(sig)
        for candle in candles[1:]:
            engine.update_stops(candle)

        stepper = SimulationStepper(engine.sim_config)
        stepper.step(candles[0], signals=[sig])
        for candle in candles[1:]:
            stepper.step(candle)
        (sim_trade,) = stepper.finalize()

        #: live SL modification sequence equals simulate's sl_history
        assert tuple(trade.pos.sl_history) == sim_trade.sl_history
        venue_stops = [o["stop_price"] for o in broker.orders_of_type("stop")]
        assert venue_stops == [sig.stop_loss] + [h["sl"] for h in sim_trade.sl_history[1:]]
        assert trade.pos.stop_loss == sim_trade.final_stop_loss
        assert trade.pos.peak_price == sim_trade.peak_price

    def test_sl_move_event_fields_and_replace_ordering(self):
        cfg = _x_cfg(sl_mode="trailing", trailing_sl_percent=1.0)
        engine, broker, events = _x_engine(config=cfg)
        trade = engine.open_position(_x_sig(entry=100.0, sl=99.0))
        first_sl_id = trade.sl_order_id

        sequence: list[str] = []
        real_create, real_cancel = broker.create_order, broker.cancel_order
        broker.create_order = lambda *a, **k: sequence.append("place") or \
            real_create(*a, **k)
        broker.cancel_order = lambda *a, **k: sequence.append("cancel") or \
            real_cancel(*a, **k)

        candle = _x_candles(2)[1]
        engine.update_stops(candle)
        (move,) = _x_events(events, EVENT_SL_MOVE)
        assert move.time == candle["timestamp"]            # candle ts, sim parity
        assert move.old_sl == 99.0
        assert move.new_sl == pytest.approx(0.99 * candle["high"])
        assert move.cause == "trailing" and move.next_tp is None
        # futures never go unprotected: new stop PLACED first, old cancelled second
        assert sequence == ["place", "cancel"]
        assert broker.cancels == [first_sl_id]
        new_stop = broker.orders_of_type("stop")[-1]
        assert trade.sl_order_id == new_stop["order_id"] != first_sl_id

    def test_risk_free_jump_and_gates(self):
        cfg = _x_cfg(risk_free_enabled=True, risk_free_at_rr=1.0)
        engine, broker, events = _x_engine(config=cfg)
        trade = engine.open_position(_x_sig(entry=100.0, sl=95.0))   # 1R = 105
        engine.update_stops({"timestamp": _X0 + _XHOUR, "open": 101.0, "high": 106.0,
                             "low": 100.5, "close": 105.0})
        (rf,) = _x_events(events, EVENT_RISK_FREE)
        assert rf.time == _X0 + _XHOUR and rf.source == SOURCE_LIVE
        assert rf.rr_level == 1.0 and rf.old_sl == 95.0 and rf.new_sl == 100.0
        assert trade.pos.stop_loss == 100.0 and trade.pos.risk_free_triggered
        assert broker.orders_of_type("stop")[-1]["stop_price"] == 100.0
        assert len(trade.pos.sl_history) == 1              # sim parity: rf not recorded
        # no re-fire on later candles
        engine.update_stops({"timestamp": _X0 + 2 * _XHOUR, "open": 105.0, "high": 107.0,
                             "low": 101.0, "close": 106.0})
        assert len(_x_events(events, EVENT_RISK_FREE)) == 1

        # engine mirrors the sim gate: risk-free is OFF in multi-RR mode
        cfg2 = _x_cfg(risk_free_enabled=True, risk_free_at_rr=1.0,
                      tp_mode=TP_MODE_MULTI_RR)
        engine2, _, events2 = _x_engine(config=cfg2)
        engine2.open_position(_x_sig(entry=100.0, sl=95.0))
        engine2.update_stops({"timestamp": _X0 + _XHOUR, "open": 101.0, "high": 104.9,
                              "low": 100.5, "close": 104.0})
        assert _x_events(events2, EVENT_RISK_FREE) == []

    def test_risk_free_final_sl_matches_stepper(self):
        sig = _x_sig(entry=100.0, sl=95.0)
        candles = [
            {"timestamp": _X0, "open": 99.9, "high": 100.2, "low": 99.8, "close": 100.0,
             "volume": 1.0},
            {"timestamp": _X0 + _XHOUR, "open": 101.0, "high": 106.0, "low": 100.5,
             "close": 105.0, "volume": 1.0},
            {"timestamp": _X0 + 2 * _XHOUR, "open": 105.0, "high": 107.0, "low": 100.5,
             "close": 106.0, "volume": 1.0},
        ]
        cfg = _x_cfg(risk_free_enabled=True, risk_free_at_rr=1.0)
        engine, _, _ = _x_engine(config=cfg)
        trade = engine.open_position(sig)
        for candle in candles[1:]:
            engine.update_stops(candle)
        stepper = SimulationStepper(engine.sim_config)
        stepper.step(candles[0], signals=[sig])
        for candle in candles[1:]:
            stepper.step(candle)
        (sim_trade,) = stepper.finalize()
        assert trade.pos.stop_loss == sim_trade.final_stop_loss == 100.0
        assert tuple(trade.pos.sl_history) == sim_trade.sl_history

    def test_modify_failure_reverts_and_retries_next_candle(self):
        cfg = _x_cfg(sl_mode="trailing", trailing_sl_percent=1.0)
        engine, broker, events = _x_engine(config=cfg)
        trade = engine.open_position(_x_sig(entry=100.0, sl=99.0))
        candles = _x_candles(3)
        broker.fail_order_types.add("stop")                # replacement will raise
        engine.update_stops(candles[1])
        (err,) = _x_events(events, EVENT_ERROR)
        assert err.where == "sl_move" and err.will_retry is True
        assert trade.pos.stop_loss == 99.0                 # reverted
        assert len(trade.pos.sl_history) == 1              # history record popped
        assert _x_events(events, EVENT_SL_MOVE) == []
        # venue healed → next candle retries with the freshly computed SL
        broker.fail_order_types.discard("stop")
        engine.update_stops(candles[2])
        (move,) = _x_events(events, EVENT_SL_MOVE)
        assert move.old_sl == 99.0
        assert move.new_sl == pytest.approx(0.99 * candles[2]["high"])
        assert trade.pos.stop_loss == move.new_sl

    def test_mt5_move_uses_modify_position(self):
        cfg = _x_cfg(symbol="EURUSD", sl_mode="trailing", trailing_sl_percent=1.0,
                     commission_type="per_lot")
        engine, broker, events = _x_engine(_XMT5Broker(fill_price=1.1), cfg)
        trade = engine.open_position(_x_sig(entry=1.1000, sl=1.0950))
        engine.update_stops({"timestamp": _X0 + _XHOUR, "open": 1.12, "high": 1.13,
                             "low": 1.115, "close": 1.125})
        assert broker.modifies == [(7777, pytest.approx(0.99 * 1.13), None)]
        # a rejected modify reverts + reports
        broker.fail_modify = True
        engine.update_stops({"timestamp": _X0 + 2 * _XHOUR, "open": 1.14, "high": 1.15,
                             "low": 1.135, "close": 1.145})
        (err,) = _x_events(events, EVENT_ERROR)
        assert err.where == "sl_move" and trade.pos.stop_loss == pytest.approx(0.99 * 1.13)

    def test_spot_move_cancels_then_replaces(self):
        # OCO variant: cancel the list, place a new OCO at the same TP
        cfg = _x_cfg(tp_mode="signal", sl_mode="trailing", trailing_sl_percent=1.0)
        engine, broker, _ = _x_engine(_XSpotBroker(), cfg)
        trade = engine.open_position(_x_sig(entry=100.0, sl=99.0, tp=200.0))
        engine.update_stops(_x_candles(2)[1])
        assert broker.list_cancels == ["900"]
        assert len(broker.ocos) == 2
        assert broker.ocos[1]["take_profit_price"] == 200.0          # TP unchanged
        assert broker.ocos[1]["stop_price"] == pytest.approx(0.99 * 101.1)
        assert trade.oco_order_list_id == "901"

        # stop-only variant: cancel the stop order, place a new one
        cfg2 = _x_cfg(sl_mode="trailing", trailing_sl_percent=1.0)
        engine2, broker2, _ = _x_engine(_XSpotBroker(), cfg2)
        trade2 = engine2.open_position(_x_sig(entry=100.0, sl=99.0))
        old_stop_id = trade2.sl_order_id
        engine2.update_stops(_x_candles(2)[1])
        assert broker2.cancels == [old_stop_id]
        assert trade2.sl_order_id == broker2.orders_of_type("stop")[-1]["order_id"]

    def test_spot_restore_then_emergency_paths(self):
        # replacement fails, restore at the old SL succeeds → revert + ERROR
        cfg = _x_cfg(tp_mode="signal", sl_mode="trailing", trailing_sl_percent=1.0)
        engine, broker, events = _x_engine(_XSpotBroker(), cfg)
        trade = engine.open_position(_x_sig(entry=100.0, sl=99.0, tp=200.0))

        real_oco = broker.create_oco_order
        broker.fail_oco = True

        def oco_fail_then_ok(*args, **kwargs):
            if broker.fail_oco:
                broker.fail_oco = False        # replacement fails, restore succeeds
                raise OrderError("scripted OCO failure")
            return real_oco(*args, **kwargs)

        broker.create_oco_order = oco_fail_then_ok
        engine.update_stops(_x_candles(2)[1])
        (err,) = _x_events(events, EVENT_ERROR)
        assert err.where == "sl_move" and "restored" in err.message
        assert trade.pos.stop_loss == 99.0                 # reverted
        assert broker.ocos[-1]["stop_price"] == 99.0       # restored at the OLD SL
        assert engine.open_trades == (trade,)

        # replacement AND restore fail → emergency market sell + CLOSE + untracked
        engine2, broker2, events2 = _x_engine(_XSpotBroker(fill_price=100.0), cfg)
        engine2.open_position(_x_sig(entry=100.0, sl=99.0, tp=200.0))
        broker2.fail_oco = True
        engine2.update_stops(_x_candles(2)[1])
        assert engine2.open_trades == ()
        sells = [o for o in broker2.orders if o["side"] == "sell"
                 and o.get("type") == "market"]
        assert len(sells) == 1                             # the emergency sell
        (err2,) = _x_events(events2, EVENT_ERROR)
        assert "emergency-sold" in err2.message
        (close,) = _x_events(events2, EVENT_CLOSE)
        assert close.reason == CLOSE_REASON_FC and close.trade.trade_id == 0

    def test_update_stops_noop_without_positions_or_movement(self):
        engine, broker, events = _x_engine()               # sl_mode="signal"
        engine.update_stops(_x_candles(1)[0])              # no positions: no-op
        engine.open_position(_x_sig())
        orders_before = len(broker.orders)
        engine.update_stops(_x_candles(2)[1])              # signal SL never moves
        assert len(broker.orders) == orders_before
        assert _x_events(events, EVENT_SL_MOVE) == []


class TestExecutionForceClose:
    def test_futures_force_close_full_flow(self):
        engine, broker, events = _x_engine(
            _XFuturesBroker(fill_price=100.0), _x_cfg(tp_mode="signal"))
        trade = engine.open_position(_x_sig(tp=110.0))
        broker.fill_price = 104.0                          # close fills at 104
        closed = engine.force_close()
        assert engine.open_trades == ()
        (record,) = closed
        assert record.close_reason == CLOSE_REASON_FC
        assert record.exit_price == 104.0
        assert record.net_pnl == pytest.approx(20.0 * 4.0)  # ppu 20 × +4$, zero costs
        close_order = broker.orders_of_type("market")[-1]
        assert close_order["side"] == "sell" and close_order["reduce_only"] is True
        assert close_order["quantity"] == pytest.approx(20.0)
        # both protective orders cancelled after the close
        assert set(broker.cancels) == {trade.sl_order_id, trade.tp_order_id}
        (close_event,) = _x_events(events, EVENT_CLOSE)
        assert close_event.reason == CLOSE_REASON_FC and close_event.trade is record

    def test_spot_force_close_cancels_before_selling(self):
        engine, broker, _ = _x_engine(
            _XSpotBroker(fill_price=100.0), _x_cfg(tp_mode="signal"))
        engine.open_position(_x_sig(tp=110.0))
        calls: list[str] = []
        real_cancel_list = broker.cancel_order_list
        real_create = broker.create_order
        broker.cancel_order_list = lambda *a, **k: calls.append("cancel") or \
            real_cancel_list(*a, **k)
        broker.create_order = lambda *a, **k: calls.append(k.get("type", "?")) or \
            real_create(*a, **k)
        engine.force_close()
        assert calls == ["cancel", "market"]               # unlock funds, then sell

    def test_mt5_force_close_by_ticket(self):
        engine, broker, _ = _x_engine(
            _XMT5Broker(fill_price=1.105),
            _x_cfg(symbol="EURUSD", commission_type="per_lot"))
        trade = engine.open_position(_x_sig(entry=1.1000, sl=1.0950))
        engine.force_close()
        assert broker.closes == [("EURUSD", pytest.approx(trade.pos.size), 7777)]

    def test_close_failure_keeps_trade_tracked(self):
        engine, broker, events = _x_engine(config=_x_cfg(
            max_positions=2, max_long_positions=2))
        engine.open_position(_x_sig())
        engine.open_position(_x_sig(ts=_X0 + _XHOUR, index=1))

        real_create = broker.create_order
        state = {"first": True}

        def close_first_fails(symbol, side, quantity, **kwargs):
            if kwargs.get("type") == "market" and side == "sell" and state["first"]:
                state["first"] = False
                broker.orders.append({"side": side, "quantity": quantity, **kwargs})
                raise OrderError("scripted close failure")
            return real_create(symbol, side, quantity, **kwargs)

        broker.create_order = close_first_fails
        closed = engine.force_close()
        assert len(closed) == 1 and len(engine.open_trades) == 1
        assert engine.open_trades[0].pos.trade_id == 0     # the failed one stayed
        errors = _x_events(events, EVENT_ERROR)
        assert any("market close failed" in e.message for e in errors)

    def test_exit_price_fallbacks(self):
        # venue reports no fill price → live ticker
        broker = _XFuturesBroker(fill_price=100.0)
        engine, _, _ = _x_engine(broker)
        engine.open_position(_x_sig())
        broker.fill_price = 0.0
        broker.ticker_last = 123.0
        (record,) = engine.force_close()
        assert record.exit_price == 123.0
        # ticker also down → entry price (last resort)
        broker2 = _XFuturesBroker(fill_price=100.0)
        engine2, _, _ = _x_engine(broker2)
        engine2.open_position(_x_sig())
        broker2.fill_price = 0.0
        broker2.fail_ticker = True
        (record2,) = engine2.force_close()
        assert record2.exit_price == 100.0

    def test_all_execution_events_tagged_live(self):
        cfg = _x_cfg(sl_mode="trailing", trailing_sl_percent=1.0)
        engine, broker, events = _x_engine(
            _XFuturesBroker(fill_price=100.0), cfg)
        engine.open_position(_x_sig(entry=100.0, sl=99.0))
        engine.update_stops(_x_candles(2)[1])
        broker.fail_cancel = True                          # force one ERROR too
        engine.force_close()
        types = {e.event_type for e in events}
        assert {EVENT_OPEN, EVENT_SL_MOVE, EVENT_CLOSE, EVENT_ERROR} <= types
        assert all(e.source == SOURCE_LIVE for e in events)


# ---------------------------------------------------------------------------
# — Trader: live loop & execution modes
# ---------------------------------------------------------------------------

_T0 = _X0 + 10 * _XHOUR          # first LIVE candle open (seed = 10 candles before)


def _t_cfg(**overrides) -> TraderConfig:
    """ config: explicit zero costs, terminal log off (tests read events)."""
    kwargs = {
        "symbol": "BTCUSDT", "min_candles": 10, "tp_mode": TP_MODE_NONE,
        "spread": 0.0, "commission_type": "percentage", "commission": 0.0,
        "log_events": False,
    }
    kwargs.update(overrides)
    return TraderConfig(**kwargs)


def _t_candle(k: int, close: float = 110.0, wick: float = 0.5) -> dict:
    """Live candle #k (opens at _T0 + k hours), library-standard dict."""
    ts = _T0 + k * _XHOUR
    return {"timestamp": ts, "open": close, "high": close + wick,
            "low": close - wick, "close": close, "volume": 1.0}


def _t_tick(candles: list[dict]) -> SchedulerTick:
    boundary = (int(candles[-1]["timestamp"]) + _XHOUR) if candles else _T0
    return SchedulerTick(boundary_ms=boundary, fired_at_ms=boundary, late_ms=0,
                         candles=tuple(candles), retries=0, fetch_delay_ms=0,
                         complete=True)


class _TPlanStrategy(BaseStrategy):
    """Signals / exits scripted by candle timestamp — fires on committed AND
    forming rows alike (whatever row it is evaluated against)."""

    primary_timeframe = "1h"
    warmup_period = 0

    def __init__(self, signals_at=None, exits_at=None):
        self.signals_at = signals_at or {}   # ts -> [(direction, entry, sl, tp)]
        self.exits_at = exits_at or {}       # ts -> reason
        self.fail_at: set[int] = set()       # ts values whose evaluation raises

    def prepare_indicators(self, data):
        return data

    def setup(self, data):
        pass

    def generate_signals(self, i, data):
        row = data[self.primary_timeframe].iloc[i]
        ts = int(row["timestamp"])
        if ts in self.fail_at:
            raise RuntimeError("scripted strategy failure")
        return [
            Signal(direction, entry, sl, tp, ts, i, "1h")
            for direction, entry, sl, tp in self.signals_at.get(ts, [])
        ]

    def detect_exit_signals(self, i, data):
        row = data[self.primary_timeframe].iloc[i]
        ts = int(row["timestamp"])
        reason = self.exits_at.get(ts)
        return [ExitSignal(reason, None, ts, i)] if reason else []


class _TStream:
    """Stoppable stand-in for a broker Stream handle."""

    def __init__(self):
        self.alive = True
        self.stops = 0

    def stop(self, timeout: float = 5.0):
        self.alive = False
        self.stops += 1


class _TFeedMixin:
    """ additions shared by the venue mocks: seed history, streams,
    a zero venue-clock offset and engine-mirroring venue state."""

    def _init_feed(self, seed_candles):
        self.seed_candles = list(seed_candles)
        self.fetch_calls: list[tuple] = []
        self.mirror_engine = None            # set to t.engine → venue mirrors tracked state
        self.candles_cb = None
        self.ticker_cb = None
        self.user_cb = None
        self.user_stream_handle = None
        self.raise_user_data = False
        self.stream_candles_calls: list[dict] = []
        self.streams: list[_TStream] = []

    def clock_offset_ms(self, *, force_refresh=False):
        return 0

    def fetch_last_candles(self, symbol, timeframe, count):
        self.fetch_calls.append((symbol, timeframe, count))
        return self.seed_candles[-count:]

    def stream_candles(self, symbol, timeframe, on_candle, *, closed_only=True):
        self.candles_cb = on_candle
        self.stream_candles_calls.append(
            {"symbol": symbol, "timeframe": timeframe, "closed_only": closed_only})
        stream = _TStream()
        self.streams.append(stream)
        return stream

    def stream_ticker(self, symbol, on_tick):
        self.ticker_cb = on_tick
        stream = _TStream()
        self.streams.append(stream)
        return stream


class _TFuturesBroker(_TFeedMixin, _XFuturesBroker):
    def __init__(self, seed_candles, **kw):
        super().__init__(**kw)
        self._init_feed(seed_candles)
        self.positions_payload: list | None = None    # None → mirror the engine

    def stream_user_data(self, on_event):
        if self.raise_user_data:
            raise BrokerError("no listen key")
        self.user_cb = on_event
        self.user_stream_handle = _TStream()
        return self.user_stream_handle

    def open_positions(self, symbol=None):
        if self.positions_payload is not None:
            return self.positions_payload
        if self.mirror_engine is None:
            return []
        return [
            Position(symbol="BTCUSDT", side=t.pos.direction, quantity=t.pos.size,
                     entry_price=t.pos.entry_price, position_id="net")
            for t in self.mirror_engine.open_trades
        ]


class _TSpotBroker(_TFeedMixin, _XSpotBroker):
    def __init__(self, seed_candles, **kw):
        super().__init__(**kw)
        self._init_feed(seed_candles)
        self.orders_payload: list | None = None       # None → mirror the engine

    def stream_user_data(self, on_event):
        if self.raise_user_data:
            raise BrokerError("no listen key")
        self.user_cb = on_event
        self.user_stream_handle = _TStream()
        return self.user_stream_handle

    def open_orders(self, symbol=None):
        if self.orders_payload is not None:
            return self.orders_payload
        out = []
        if self.mirror_engine is not None:
            for t in self.mirror_engine.open_trades:
                if t.sl_order_id:
                    out.append(Order(order_id=t.sl_order_id, symbol="BTCUSDT",
                                     side="sell", type="stop", quantity=t.pos.size))
                if t.oco_order_list_id:
                    out.append(Order(order_id=f"leg-{t.oco_order_list_id}",
                                     symbol="BTCUSDT", side="sell", type="stop",
                                     quantity=t.pos.size,
                                     raw={"orderListId": t.oco_order_list_id}))
        return out


class _TMT5Broker(_TFeedMixin, _XMT5Broker):
    def __init__(self, seed_candles, **kw):
        super().__init__(**kw)
        self._init_feed(seed_candles)
        self.venue_positions: list | None = None      # None → mirror the engine
        self.deals: list[dict] = []
        self.deals_calls: list = []
        self.fail_deals = False
        self.candle_poll_interval = 1.0               # knobs the worker sets
        self.tick_poll_interval = 0.2
        self._ticket_seq = itertools.count(7000)
        self._comment_ticket: dict[str, str] = {}

    def open_positions(self, symbol=None):
        if self.venue_positions is not None:
            return self.venue_positions
        out = []
        tracked = list(self.mirror_engine.open_trades) if self.mirror_engine else []
        known = set()
        for t in tracked:
            known.add(t.mt5_ticket)
            out.append(Position(symbol="EURUSD", side=t.pos.direction,
                                quantity=t.pos.size, entry_price=t.pos.entry_price,
                                position_id=t.mt5_ticket,
                                raw={"comment": t.client_order_id}))
        # The just-opened, not-yet-tracked position: the engine resolves its
        # ticket here by comment match during open().
        if self.last_comment:
            if self.last_comment not in self._comment_ticket:
                self._comment_ticket[self.last_comment] = str(next(self._ticket_seq))
            ticket = self._comment_ticket[self.last_comment]
            if ticket not in known:
                out.append(Position(symbol="EURUSD", side="long", quantity=0.1,
                                    entry_price=1.1, position_id=ticket,
                                    raw={"comment": self.last_comment}))
        return out

    def history_deals(self, from_ms=None, to_ms=None, *, position=None):
        self.deals_calls.append(position)
        if self.fail_deals:
            raise BrokerError("history down")
        return list(self.deals)


def _t_patch_scheduler(monkeypatch, ticks):
    """Replace the scheduler with a scripted one: hands out *ticks*, then
    blocks on the stop event.  Returns a holder exposing the instance."""
    holder = {}

    class _FakeScheduler:
        def __init__(self, broker, symbol, timeframe, *, last_open_ms=None,
                     stop_event=None, **kw):
            self.symbol = symbol
            self.timeframe = timeframe
            self.anchor = last_open_ms
            self._stop = stop_event if stop_event is not None else threading.Event()
            self._ticks = list(ticks)
            holder["instance"] = self
            holder.setdefault("instances", []).append(self)   # multi-pair

        def wait_next_close(self):
            if self._stop.is_set():
                return None
            if self._ticks:
                return self._ticks.pop(0)
            self._stop.wait()
            return None

        def stop(self):
            self._stop.set()

    monkeypatch.setattr(trader_module, "CandleCloseScheduler", _FakeScheduler)
    return holder


def _t_trader(broker=None, cfg=None, strategy=None, **trader_kw):
    broker = broker if broker is not None else _TFuturesBroker(_x_candles(10), fill_price=110.0)
    cfg = cfg if cfg is not None else _t_cfg()
    strategy = strategy if strategy is not None else _TPlanStrategy()
    trader_kw.setdefault("trader_id", "t16")
    t = Trader(broker=broker, strategy=strategy, config=cfg, **trader_kw)
    if hasattr(broker, "mirror_engine"):
        broker.mirror_engine = t.engine
    return t


def _t_ready(t: Trader):
    """Seed + arm the engine WITHOUT starting any thread/stream — tests then
    drive the worker's handlers directly (deterministic, single-threaded)."""
    worker = t._worker
    worker.seed()
    worker.engine.start()
    return worker


def _t_collect(t: Trader) -> list:
    events: list = []
    t.events.subscribe(events.append)
    return events


def _t_wait(condition, timeout=5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


class TestTraderApi:
    def test_all_three_required(self):
        with pytest.raises(ValueError, match="all required"):
            Trader(broker=_TFuturesBroker(_x_candles(10)), config=_t_cfg())

    def test_pairs_form_accepted(self):
        #: the multi-pair form exists — the old guard is gone.
        pair = TraderPair(broker=_TFuturesBroker(_x_candles(10)),
                          config=_t_cfg(), strategy=_TPlanStrategy())
        t = Trader(pairs=[pair])
        assert len(t.engines) == 1

    def test_display_accepted(self):
        #: the display bridge exists — the old guard is gone.  Sim
        # modes get a dedicated deep-copied strategy instance.
        strategy = _TPlanStrategy()
        cfg = _t_cfg(display=True, display_candles=10)
        t = _t_trader(cfg=cfg, strategy=strategy)
        bridge = t._worker.display
        assert bridge is not None and bridge.mode == "sim"
        assert isinstance(bridge._sim_strategy, _TPlanStrategy)
        assert bridge._sim_strategy is not strategy
        # Real mode needs no sim strategy; display=False builds no bridge.
        t_real = _t_trader(cfg=_t_cfg(display=True, display_candles=10,
                                      display_trades="real"))
        assert t_real._worker.display._sim_strategy is None
        assert _t_trader()._worker.display is None

    def test_multi_rr_accepted(self):
        #: the live ladder exists — the old guard is gone.
        cfg = _t_cfg(tp_mode=TP_MODE_MULTI_RR, tp_levels=[1.0, 2.0],
                     tp_level_close_fractions=[0.5, 0.5])
        t = _t_trader(cfg=cfg)
        assert t.engine.config.tp_mode == TP_MODE_MULTI_RR

    def test_settings_validated_and_stored(self, tmp_path):
        with pytest.raises(ValueError, match="on_stop"):
            _t_trader(on_stop="explode")
        t = _t_trader(on_stop=ON_STOP_CLOSE_ALL,
                      state_path=tmp_path / "state.json",
                      kill_switch_file=tmp_path / "kill")
        assert t.settings.on_stop == ON_STOP_CLOSE_ALL
        assert t.settings.state_path == str(tmp_path / "state.json")

    def test_engine_guards_apply_at_construction(self):
        # Spot leverage rule comes straight from the engine.
        broker = _TSpotBroker(_x_candles(10))
        with pytest.raises(ValueError, match="leverage"):
            _t_trader(broker=broker, cfg=_t_cfg(leverage=2.0))

    def test_ids_and_properties(self):
        t = _t_trader(trader_id="idxyz")
        assert t.trader_id == "idxyz"
        assert t.open_trades == () and t.closed_trades == ()
        assert t.engine is t._worker.engine and t.events is t._worker.events

    def test_run_only_once(self, monkeypatch):
        _t_patch_scheduler(monkeypatch, [])
        t = _t_trader()
        threading.Timer(0.2, t.stop).start()
        t.run()
        with pytest.raises(RuntimeError, match="only run once"):
            t.run()

    def test_close_reason_manual_constant(self):
        assert CLOSE_REASON_MANUAL == "manual"
        assert trader_pkg.CLOSE_REASON_MANUAL is CLOSE_REASON_MANUAL


class TestTraderSeeding:
    def test_seed_fetches_min_candles_and_anchors(self):
        t = _t_trader(cfg=_t_cfg(min_candles=10))
        worker = _t_ready(t)
        assert worker.broker.fetch_calls == [("BTCUSDT", "1h", 10)]
        frame = worker.data["1h"]
        assert len(frame) == 10
        assert worker._last_committed_ts == _X0 + 9 * _XHOUR

    def test_short_history_raises(self):
        broker = _TFuturesBroker(_x_candles(4))
        t = _t_trader(broker=broker)
        with pytest.raises(ValueError, match="min_candles"):
            t._worker.seed()

    def test_seed_signals_never_traded(self):
        # A signal inside the seed range is history — no order, no event.
        strategy = _TPlanStrategy(
            signals_at={_X0 + 5 * _XHOUR: [("long", 105.0, 100.0, None)]})
        t = _t_trader(strategy=strategy)
        events = _t_collect(t)
        _t_ready(t)
        assert t._worker.broker.orders == []
        assert events == []

    def test_scheduler_anchored_at_seed_end(self, monkeypatch):
        holder = _t_patch_scheduler(monkeypatch, [])
        t = _t_trader()
        threading.Timer(0.3, t.stop).start()
        t.run()
        assert holder["instance"].anchor == _X0 + 9 * _XHOUR
        assert holder["instance"].symbol == "BTCUSDT"

    def test_poll_intervals_applied_to_polling_venue(self, monkeypatch):
        _t_patch_scheduler(monkeypatch, [])
        broker = _TMT5Broker(_x_candles(10), fill_price=1.1)
        cfg = _t_cfg(symbol="EURUSD", candle_poll_interval=3.5,
                     tick_poll_interval=0.7, commission_type="per_lot")
        t = _t_trader(broker=broker, cfg=cfg)
        worker = t._worker
        worker.seed()
        worker.start()                       # engine start + producers + knobs
        try:
            assert broker.candle_poll_interval == 3.5
            assert broker.tick_poll_interval == 0.7
        finally:
            worker.shutdown()


class TestTraderCandleClose:
    def test_signal_to_entry_pipeline(self):
        strategy = _TPlanStrategy(signals_at={_T0: [("long", 110.0, 105.0, None)]})
        t = _t_trader(strategy=strategy)
        events = _t_collect(t)
        worker = _t_ready(t)
        worker._process_scheduler_tick(_t_tick([_t_candle(0)]))
        markets = worker.broker.orders_of_type("market")
        assert len(markets) == 1 and markets[0]["side"] == "buy"
        assert len(t.open_trades) == 1
        kinds = [e.event_type for e in events]
        assert kinds == [EVENT_SIGNAL, EVENT_OPEN]
        assert all(e.source == SOURCE_LIVE for e in events)
        assert events[0].entry_price == 110.0 and events[0].stop_loss == 105.0
        # Master frame advanced by exactly one committed candle.
        assert len(worker.data["1h"]) == 11

    def test_trailing_sl_managed_per_closed_candle(self):
        strategy = _TPlanStrategy(signals_at={_T0: [("long", 110.0, 105.0, None)]})
        cfg = _t_cfg(sl_mode="trailing", trailing_sl_percent=1.0)
        t = _t_trader(cfg=cfg, strategy=strategy)
        worker = _t_ready(t)
        worker._process_scheduler_tick(_t_tick([_t_candle(0, 110.0)]))
        stops_before = len(worker.broker.orders_of_type("stop"))
        worker._process_scheduler_tick(_t_tick([_t_candle(1, 114.0)]))
        worker._process_scheduler_tick(_t_tick([_t_candle(2, 118.0)]))
        # Each rising candle advanced the venue stop (place-new-then-cancel-old).
        assert len(worker.broker.orders_of_type("stop")) >= stops_before + 2
        (trade,) = t.open_trades
        assert trade.pos.stop_loss > 105.0
        assert len(trade.pos.sl_history) >= 3       # entry record + 2 moves

    def test_exit_signal_force_close(self):
        strategy = _TPlanStrategy(
            signals_at={_T0: [("long", 110.0, 105.0, None)]},
            exits_at={_T0 + _XHOUR: "trend_reversal"},
        )
        cfg = _t_cfg(force_close_on_exit_signal=True)
        t = _t_trader(cfg=cfg, strategy=strategy)
        events = _t_collect(t)
        worker = _t_ready(t)
        worker.broker.ticker_last = 111.0
        worker._process_scheduler_tick(_t_tick([_t_candle(0)]))
        worker._process_scheduler_tick(_t_tick([_t_candle(1, 111.0)]))
        exit_events = [e for e in events if e.event_type == EVENT_EXIT_SIGNAL]
        assert len(exit_events) == 1
        assert exit_events[0].reason == "trend_reversal"
        assert exit_events[0].action == "force_close"
        assert t.open_trades == ()
        (closed,) = t.closed_trades
        assert closed.close_reason == CLOSE_REASON_FC

    def test_exit_signal_without_flag_keeps_position(self):
        strategy = _TPlanStrategy(
            signals_at={_T0: [("long", 110.0, 105.0, None)]},
            exits_at={_T0 + _XHOUR: "trend_reversal"},
        )
        t = _t_trader(strategy=strategy)          # force_close_on_exit_signal=False
        events = _t_collect(t)
        worker = _t_ready(t)
        worker._process_scheduler_tick(_t_tick([_t_candle(0)]))
        worker._process_scheduler_tick(_t_tick([_t_candle(1, 111.0)]))
        (exit_event,) = [e for e in events if e.event_type == EVENT_EXIT_SIGNAL]
        assert exit_event.action == "none"
        assert len(t.open_trades) == 1

    def test_gap_fill_stale_signals_skip_entry(self, capsys):
        # Downtime tick carries 3 candles; signals on an old and the newest
        # candle: the old one prints but places no order.
        strategy = _TPlanStrategy(signals_at={
            _T0: [("long", 110.0, 105.0, None)],
            _T0 + 2 * _XHOUR: [("long", 112.0, 107.0, None)],
        })
        t = _t_trader(strategy=strategy)
        events = _t_collect(t)
        worker = _t_ready(t)
        worker._process_scheduler_tick(
            _t_tick([_t_candle(0, 110.0), _t_candle(1, 111.0), _t_candle(2, 112.0)]))
        signal_events = [e for e in events if e.event_type == EVENT_SIGNAL]
        assert len(signal_events) == 2               # both printed
        markets = worker.broker.orders_of_type("market")
        assert len(markets) == 1                     # only the newest entered
        (trade,) = t.open_trades
        assert trade.signal.entry_price == 112.0
        assert "stale" in capsys.readouterr().out

    def test_spot_short_signal_survives_session(self):
        strategy = _TPlanStrategy(signals_at={_T0: [("short", 110.0, 115.0, None)]})
        broker = _TSpotBroker(_x_candles(10), fill_price=110.0)
        t = _t_trader(broker=broker, strategy=strategy, cfg=_t_cfg())
        events = _t_collect(t)
        worker = _t_ready(t)
        worker._process_scheduler_tick(_t_tick([_t_candle(0)]))     # must not raise
        errors = [e for e in events if e.event_type == EVENT_ERROR]
        assert errors and "short" in errors[0].message
        assert t.open_trades == ()

    def test_empty_tick_is_harmless(self):
        t = _t_trader()
        worker = _t_ready(t)
        worker._process_scheduler_tick(_t_tick([]))
        assert len(worker.data["1h"]) == 10


class TestTraderDedup:
    def _forming(self, k=0, close=110.0):
        return {**_t_candle(k, close), "closed": False}

    def test_once_per_candle_direction_across_forming_and_close(self):
        strategy = _TPlanStrategy(signals_at={_T0: [("long", 110.0, 105.0, None)]})
        cfg = _t_cfg(execution=EXEC_CANDLE_UPDATE)
        t = _t_trader(cfg=cfg, strategy=strategy)
        events = _t_collect(t)
        worker = _t_ready(t)
        worker._process_forming(self._forming())
        worker._process_forming(self._forming(close=110.2))
        worker._process_forming(self._forming(close=110.4))
        # Same candle commits — the strategy re-emits the same signal there too.
        worker._process_scheduler_tick(_t_tick([_t_candle(0)]))
        assert len(worker.broker.orders_of_type("market")) == 1
        assert len([e for e in events if e.event_type == EVENT_SIGNAL]) == 1
        assert len(t.open_trades) == 1

    def test_new_candle_allows_new_entry(self):
        strategy = _TPlanStrategy(signals_at={
            _T0: [("long", 110.0, 105.0, None)],
            _T0 + _XHOUR: [("long", 111.0, 106.0, None)],
        })
        cfg = _t_cfg(execution=EXEC_CANDLE_UPDATE, max_positions=2, max_long_positions=2)
        t = _t_trader(cfg=cfg, strategy=strategy)
        worker = _t_ready(t)
        worker._process_forming(self._forming(0))
        worker._process_scheduler_tick(_t_tick([_t_candle(0)]))
        worker._process_forming(self._forming(1, close=111.0))
        assert len(worker.broker.orders_of_type("market")) == 2

    def test_forming_never_mutates_master(self):
        strategy = _TPlanStrategy(signals_at={_T0: [("long", 110.0, 105.0, None)]})
        cfg = _t_cfg(execution=EXEC_CANDLE_UPDATE)
        t = _t_trader(cfg=cfg, strategy=strategy)
        worker = _t_ready(t)
        worker._process_forming(self._forming())
        assert len(worker.data["1h"]) == 10          # still seed-only
        assert worker._last_committed_ts == _X0 + 9 * _XHOUR

    def test_stale_forming_ignored_after_commit(self):
        strategy = _TPlanStrategy(signals_at={_T0: [("long", 110.0, 105.0, None)]})
        cfg = _t_cfg(execution=EXEC_CANDLE_UPDATE)
        t = _t_trader(cfg=cfg, strategy=strategy)
        worker = _t_ready(t)
        worker._process_scheduler_tick(_t_tick([_t_candle(0)]))
        before = len(worker.broker.orders)
        worker._process_forming(self._forming())      # forming row of a committed candle
        assert len(worker.broker.orders) == before

    def test_exit_action_once_per_candle(self):
        strategy = _TPlanStrategy(exits_at={_T0: "give_up"})
        cfg = _t_cfg(execution=EXEC_CANDLE_UPDATE, force_close_on_exit_signal=True)
        t = _t_trader(cfg=cfg, strategy=strategy)
        events = _t_collect(t)
        worker = _t_ready(t)
        worker._process_forming(self._forming())
        worker._process_forming(self._forming(close=110.3))
        assert len([e for e in events if e.event_type == EVENT_EXIT_SIGNAL]) == 1

    def test_closed_stream_candles_never_commit(self):
        cfg = _t_cfg(execution=EXEC_CANDLE_UPDATE)
        t = _t_trader(cfg=cfg)
        worker = _t_ready(t)
        worker._on_forming({**_t_candle(0), "closed": True})
        assert worker.queue.empty()                  # dropped at the producer

    def test_feed_subscribes_with_forming_updates(self, monkeypatch):
        _t_patch_scheduler(monkeypatch, [])
        cfg = _t_cfg(execution=EXEC_CANDLE_UPDATE)
        t = _t_trader(cfg=cfg)
        worker = t._worker
        worker.seed()
        worker.start()
        try:
            (call,) = worker.broker.stream_candles_calls
            assert call["closed_only"] is False
        finally:
            worker.shutdown()
        assert worker.broker.streams[0].alive is False


class TestTraderTickMode:
    def _t(self, strategy=None, **cfg_over):
        cfg = _t_cfg(execution=EXEC_TICK, **cfg_over)
        t = _t_trader(cfg=cfg, strategy=strategy)
        return t, _t_ready(t)

    def test_ticks_synthesize_forming_row(self):
        t, worker = self._t()
        stamp = _T0 + 60_000
        worker._process_tick(Ticker(symbol="BTCUSDT", last=111.0, timestamp=stamp))
        worker._process_tick(Ticker(symbol="BTCUSDT", last=113.0, timestamp=stamp + 1))
        worker._process_tick(Ticker(symbol="BTCUSDT", last=110.0, timestamp=stamp + 2))
        forming = worker._forming
        assert forming["timestamp"] == _T0
        assert forming["open"] == 111.0 and forming["high"] == 113.0
        assert forming["low"] == 110.0 and forming["close"] == 110.0
        assert forming["volume"] == 0.0

    def test_tick_signal_enters_mid_candle(self):
        strategy = _TPlanStrategy(signals_at={_T0: [("long", 111.0, 106.0, None)]})
        t, worker = self._t(strategy=strategy)
        worker._process_tick(Ticker(symbol="BTCUSDT", last=111.0, timestamp=_T0 + 5_000))
        assert len(worker.broker.orders_of_type("market")) == 1
        assert len(worker.data["1h"]) == 10          # committed state untouched

    def test_tick_in_committed_interval_ignored(self):
        t, worker = self._t()
        worker._process_tick(
            Ticker(symbol="BTCUSDT", last=109.0, timestamp=_X0 + 9 * _XHOUR + 1))
        assert worker._forming is None

    def test_new_interval_resets_forming_row(self):
        t, worker = self._t()
        worker._process_tick(Ticker(symbol="BTCUSDT", last=111.0, timestamp=_T0 + 1))
        worker._process_tick(
            Ticker(symbol="BTCUSDT", last=115.0, timestamp=_T0 + _XHOUR + 1))
        forming = worker._forming
        assert forming["timestamp"] == _T0 + _XHOUR
        assert forming["open"] == forming["close"] == 115.0

    def test_ticker_feed_subscribed(self, monkeypatch):
        _t_patch_scheduler(monkeypatch, [])
        t = _t_trader(cfg=_t_cfg(execution=EXEC_TICK))
        worker = t._worker
        worker.seed()
        worker.start()
        try:
            assert worker.broker.ticker_cb is not None
        finally:
            worker.shutdown()


class TestTraderCloseDetection:
    def _opened(self, cfg=None, strategy=None, broker=None, tp=None):
        strategy = strategy or _TPlanStrategy(
            signals_at={_T0: [("long", 110.0, 105.0, tp)]})
        t = _t_trader(broker=broker, cfg=cfg, strategy=strategy)
        events = _t_collect(t)
        worker = _t_ready(t)
        worker.broker.ticker_last = 110.0
        worker._process_scheduler_tick(_t_tick([_t_candle(0)]))
        assert len(t.open_trades) == 1
        return t, worker, events

    def test_futures_sl_fill_via_user_data(self):
        t, worker, events = self._opened()
        (trade,) = t.open_trades
        size = trade.pos.size
        fill_time = _T0 + 30 * 60_000
        worker._process_user_data({
            "e": "ORDER_TRADE_UPDATE", "E": fill_time,
            "o": {"i": trade.sl_order_id, "X": "FILLED",
                  "ap": "104.9", "z": str(size), "T": fill_time},
        })
        assert t.open_trades == ()
        (closed,) = t.closed_trades
        assert closed.close_reason == CLOSE_REASON_SL
        assert closed.exit_price == 104.9            # the venue's real fill
        assert closed.close_time == fill_time
        close_events = [e for e in events if e.event_type == EVENT_CLOSE]
        assert close_events and close_events[0].trade is closed

    def test_futures_sl_fill_after_risk_free_maps_to_rf(self):
        cfg = _t_cfg(risk_free_enabled=True, risk_free_at_rr=1.0)
        strategy = _TPlanStrategy(signals_at={_T0: [("long", 110.0, 105.0, None)]})
        t = _t_trader(cfg=cfg, strategy=strategy)
        worker = _t_ready(t)
        worker._process_scheduler_tick(_t_tick([_t_candle(0)]))
        (trade,) = t.open_trades
        # +1R touched → shared maths move the SL to break-even on the venue.
        worker._process_scheduler_tick(_t_tick([_t_candle(1, 115.5)]))
        assert trade.pos.risk_free_triggered
        worker._process_user_data({
            "e": "ORDER_TRADE_UPDATE", "E": _T0 + 2 * _XHOUR,
            "o": {"i": trade.sl_order_id, "X": "FILLED",
                  "ap": "110.0", "z": str(trade.pos.size)},
        })
        (closed,) = t.closed_trades
        assert closed.close_reason == CLOSE_REASON_RF

    def test_futures_tp_fill_cancels_leftover_sl(self):
        cfg = _t_cfg(tp_mode=TP_MODE_SIGNAL)
        t, worker, events = self._opened(cfg=cfg, tp=120.0)
        (trade,) = t.open_trades
        sl_id, tp_id = trade.sl_order_id, trade.tp_order_id
        assert sl_id and tp_id
        worker._process_user_data({
            "e": "ORDER_TRADE_UPDATE", "E": _T0 + _XHOUR,
            "o": {"i": tp_id, "X": "FILLED", "ap": "120.0",
                  "z": str(trade.pos.size)},
        })
        (closed,) = t.closed_trades
        assert closed.close_reason == CLOSE_REASON_TP
        assert closed.exit_price == 120.0
        assert sl_id in worker.broker.cancels        # sibling protection removed

    def test_futures_partial_fill_shrinks_position(self):
        t, worker, events = self._opened()
        (trade,) = t.open_trades
        original = trade.pos.size
        half = original / 2
        worker._process_user_data({
            "e": "ORDER_TRADE_UPDATE", "E": _T0 + _XHOUR,
            "o": {"i": trade.sl_order_id, "X": "FILLED",
                  "ap": "104.9", "z": str(half)},
        })
        assert len(t.open_trades) == 1               # still tracked
        assert t.open_trades[0].pos.size == pytest.approx(half)
        (closed,) = t.closed_trades
        assert closed.size == pytest.approx(half)
        assert worker.broker.cancels == []           # partial: protection stays

    def test_unrelated_user_data_ignored(self):
        t, worker, events = self._opened()
        before = len(t.closed_trades)
        worker._process_user_data({"e": "ORDER_TRADE_UPDATE", "E": 1,
                                   "o": {"i": "999999", "X": "FILLED", "ap": "1"}})
        worker._process_user_data({"e": "ACCOUNT_UPDATE"})
        (trade,) = t.open_trades
        worker._process_user_data({
            "e": "ORDER_TRADE_UPDATE", "E": 1,
            "o": {"i": trade.sl_order_id, "X": "NEW"},   # not a fill
        })
        assert len(t.closed_trades) == before
        assert len(t.open_trades) == 1

    def test_spot_oco_leg_fill_maps_tp_and_sl(self):
        broker = _TSpotBroker(_x_candles(10), fill_price=110.0)
        cfg = _t_cfg(tp_mode=TP_MODE_SIGNAL)
        t, worker, events = self._opened(cfg=cfg, broker=broker, tp=120.0)
        (trade,) = t.open_trades
        assert trade.oco_order_list_id
        worker._process_user_data({
            "e": "executionReport", "E": _T0 + _XHOUR, "i": 424242,
            "g": trade.oco_order_list_id, "o": "LIMIT_MAKER", "X": "FILLED",
            "L": "120.0", "z": str(trade.pos.size),
        })
        (closed,) = t.closed_trades
        assert closed.close_reason == CLOSE_REASON_TP and closed.exit_price == 120.0
        assert t.open_trades == ()

    def test_spot_stop_leg_fill_maps_sl(self):
        broker = _TSpotBroker(_x_candles(10), fill_price=110.0)
        t, worker, events = self._opened(broker=broker)      # tp_mode none → stop only
        (trade,) = t.open_trades
        assert trade.sl_order_id and not trade.oco_order_list_id
        worker._process_user_data({
            "e": "executionReport", "E": _T0 + _XHOUR, "i": trade.sl_order_id,
            "g": "-1", "o": "STOP_LOSS", "X": "FILLED",
            "L": "104.8", "z": str(trade.pos.size),
        })
        (closed,) = t.closed_trades
        assert closed.close_reason == CLOSE_REASON_SL and closed.exit_price == 104.8

    def test_futures_manual_close_reconciled(self):
        t, worker, events = self._opened()
        (trade,) = t.open_trades
        sl_id = trade.sl_order_id
        worker.broker.positions_payload = []          # user flattened on the app
        worker.broker.ticker_last = 108.5
        worker._last_reconcile = 0.0
        worker._reconcile_positions()
        assert t.open_trades == ()
        (closed,) = t.closed_trades
        assert closed.close_reason == CLOSE_REASON_MANUAL
        assert closed.exit_price == 108.5             # ticker fallback
        assert sl_id in worker.broker.cancels         # leftover stop cancelled

    def test_futures_manual_partial_reconciled(self):
        t, worker, events = self._opened()
        (trade,) = t.open_trades
        remaining = trade.pos.size * 0.25
        worker.broker.positions_payload = [Position(
            symbol="BTCUSDT", side="long", quantity=remaining,
            entry_price=110.0, position_id="net")]
        worker.broker.ticker_last = 109.0
        worker._last_reconcile = 0.0
        worker._reconcile_positions()
        assert len(t.open_trades) == 1
        assert t.open_trades[0].pos.size == pytest.approx(remaining)
        (closed,) = t.closed_trades
        assert closed.close_reason == CLOSE_REASON_MANUAL

    def _mt5_opened(self, **cfg_over):
        broker = _TMT5Broker(_x_candles(10), fill_price=1.1)
        cfg = _t_cfg(symbol="EURUSD", commission_type="per_lot", **cfg_over)
        strategy = _TPlanStrategy(signals_at={_T0: [("long", 1.1000, 1.0950, None)]})
        t = _t_trader(broker=broker, cfg=cfg, strategy=strategy)
        worker = _t_ready(t)
        worker.broker.ticker_last = 1.1
        worker._process_scheduler_tick(_t_tick([_t_candle(0, close=1.1, wick=0.001)]))
        assert len(t.open_trades) == 1
        return t, worker

    def test_mt5_sl_close_from_history_deals(self):
        t, worker = self._mt5_opened()
        (trade,) = t.open_trades
        worker.broker.venue_positions = []            # ticket vanished
        worker.broker.deals = [
            {"entry": 0, "price": 1.1, "reason": 0, "time": _T0 // 1000},
            {"entry": 1, "price": 1.0949, "reason": 4,          # DEAL_REASON_SL
             "time_msc": _T0 + 45 * 60_000},
        ]
        worker._last_reconcile = 0.0
        worker._reconcile_positions()
        assert t.open_trades == ()
        (closed,) = t.closed_trades
        assert closed.close_reason == CLOSE_REASON_SL
        assert closed.exit_price == 1.0949            # the deal's real fill
        assert closed.close_time == _T0 + 45 * 60_000
        assert worker.broker.deals_calls == [int(trade.mt5_ticket)]

    def test_mt5_tp_and_manual_deal_reasons(self):
        t, worker = self._mt5_opened()
        worker.broker.venue_positions = []
        worker.broker.deals = [{"entry": 1, "price": 1.1180, "reason": 5,
                                "time_msc": _T0 + _XHOUR}]
        worker._last_reconcile = 0.0
        worker._reconcile_positions()
        assert t.closed_trades[-1].close_reason == CLOSE_REASON_TP

        t2, worker2 = self._mt5_opened()
        worker2.broker.venue_positions = []
        worker2.broker.deals = [{"entry": 1, "price": 1.1010, "reason": 0,
                                 "time_msc": _T0 + _XHOUR}]
        worker2._last_reconcile = 0.0
        worker2._reconcile_positions()
        assert t2.closed_trades[-1].close_reason == CLOSE_REASON_MANUAL

    def test_mt5_no_deals_falls_back_to_ticker(self):
        t, worker = self._mt5_opened()
        worker.broker.venue_positions = []
        worker.broker.fail_deals = True
        worker.broker.ticker_last = 1.0725
        worker._last_reconcile = 0.0
        worker._reconcile_positions()
        (closed,) = t.closed_trades
        assert closed.close_reason == CLOSE_REASON_MANUAL
        assert closed.exit_price == 1.0725

    def test_mt5_manual_partial_close(self):
        t, worker = self._mt5_opened()
        (trade,) = t.open_trades
        remaining = trade.pos.size * 0.5
        worker.broker.venue_positions = [Position(
            symbol="EURUSD", side="long", quantity=remaining,
            entry_price=1.1, position_id=trade.mt5_ticket,
            raw={"comment": trade.client_order_id})]
        worker.broker.deals = [{"entry": 1, "price": 1.1120, "reason": 1,
                                "time_msc": _T0 + _XHOUR}]
        worker._last_reconcile = 0.0
        worker._reconcile_positions()
        assert len(t.open_trades) == 1
        assert t.open_trades[0].pos.size == pytest.approx(remaining)
        (closed,) = t.closed_trades
        assert closed.close_reason == CLOSE_REASON_MANUAL
        assert closed.exit_price == 1.1120

    def test_spot_degraded_reconcile_when_user_stream_down(self):
        broker = _TSpotBroker(_x_candles(10), fill_price=110.0)
        t, worker, events = self._opened(broker=broker)
        assert worker._user_stream is None            # never started in direct drive
        worker.broker.orders_payload = []             # protection vanished
        worker.broker.ticker_last = 109.9
        worker._last_reconcile = 0.0
        worker._reconcile_positions()
        (closed,) = t.closed_trades
        assert closed.close_reason == CLOSE_REASON_MANUAL
        assert closed.exit_price == 109.9

    def test_reconcile_venue_outage_reports_once(self):
        t, worker, events = self._opened()

        def _boom(symbol=None):
            raise ConnectionFailed("venue down")

        worker.broker.open_positions = _boom
        for _ in range(3):
            worker._last_reconcile = 0.0
            worker._reconcile_positions()
        errors = [e for e in events if e.event_type == EVENT_ERROR
                  and e.where == "reconcile"]
        assert len(errors) == 1                       # reported once, not spammed
        assert len(t.open_trades) == 1                # nothing was force-guessed


class TestRecordExternalClose:
    def test_full_close_untracks_and_emits(self):
        engine, broker, events = _x_engine(_XFuturesBroker(fill_price=100.0))
        trade = engine.open_position(_x_sig(entry=100.0, sl=95.0))
        closed = engine.record_external_close(trade, 95.1, CLOSE_REASON_SL,
                                              close_time=_X0 + _XHOUR)
        assert engine.open_trades == ()
        assert closed.exit_price == 95.1 and closed.close_time == _X0 + _XHOUR
        assert closed.close_reason == CLOSE_REASON_SL
        close_events = _x_events(events, EVENT_CLOSE)
        assert close_events and close_events[0].trade is closed

    def test_partial_close_shrinks_and_shares_trade_id(self):
        engine, broker, events = _x_engine(_XFuturesBroker(fill_price=100.0))
        trade = engine.open_position(_x_sig(entry=100.0, sl=95.0))
        original = trade.pos.size
        slice_ = engine.record_external_close(
            trade, 104.0, CLOSE_REASON_MANUAL, closed_size=original / 4)
        assert len(engine.open_trades) == 1
        assert trade.pos.size == pytest.approx(original * 0.75)
        assert slice_.size == pytest.approx(original / 4)
        assert slice_.trade_id == trade.pos.trade_id

    def test_untracked_trade_raises(self):
        engine, broker, _ = _x_engine(_XFuturesBroker(fill_price=100.0))
        trade = engine.open_position(_x_sig(entry=100.0, sl=95.0))
        engine.record_external_close(trade, 99.0, CLOSE_REASON_SL)
        with pytest.raises(ValueError, match="not tracked"):
            engine.record_external_close(trade, 99.0, CLOSE_REASON_SL)


class TestTraderLifecycle:
    def test_run_stop_thread_lifecycle(self, monkeypatch):
        strategy = _TPlanStrategy(signals_at={_T0: [("long", 110.0, 105.0, None)]})
        holder = _t_patch_scheduler(monkeypatch, [_t_tick([_t_candle(0)])])
        broker = _TFuturesBroker(_x_candles(10), fill_price=110.0)
        broker.ticker_last = 110.0
        t = _t_trader(broker=broker, strategy=strategy)
        result: dict = {}

        def _go():
            result["r"] = t.run()

        thread = threading.Thread(target=_go, daemon=True)
        thread.start()
        assert _t_wait(lambda: len(broker.orders_of_type("market")) >= 1)
        t.stop()
        thread.join(timeout=5.0)
        assert not thread.is_alive()
        assert result["r"] == []                     # nothing closed this session
        assert len(t.open_trades) == 1               # position kept on shutdown
        assert holder["instance"].anchor == _X0 + 9 * _XHOUR
        assert broker.user_stream_handle.alive is False   # feeds stopped

    def test_run_returns_session_closes(self, monkeypatch):
        strategy = _TPlanStrategy(signals_at={_T0: [("long", 110.0, 105.0, None)]})
        _t_patch_scheduler(monkeypatch, [_t_tick([_t_candle(0)])])
        broker = _TFuturesBroker(_x_candles(10), fill_price=110.0)
        broker.ticker_last = 110.0
        t = _t_trader(broker=broker, strategy=strategy)
        result: dict = {}

        def _go():
            result["r"] = t.run()

        thread = threading.Thread(target=_go, daemon=True)
        thread.start()
        assert _t_wait(lambda: broker.user_cb is not None
                       and len(t.open_trades) == 1)
        (trade,) = t.open_trades
        broker.user_cb({
            "e": "ORDER_TRADE_UPDATE", "E": _T0 + _XHOUR,
            "o": {"i": trade.sl_order_id, "X": "FILLED",
                  "ap": "104.9", "z": str(trade.pos.size)},
        })
        assert _t_wait(lambda: len(t.closed_trades) == 1)
        t.stop()
        thread.join(timeout=5.0)
        assert [c.close_reason for c in result["r"]] == [CLOSE_REASON_SL]

    def test_ctrl_c_stops_cleanly(self, monkeypatch, capsys):
        _t_patch_scheduler(monkeypatch, [])
        t = _t_trader()

        def _interrupt():
            time.sleep(0.4)
            _thread.interrupt_main()

        threading.Thread(target=_interrupt, daemon=True).start()
        out = t.run()
        assert out == []
        assert "Ctrl+C — stopping" in capsys.readouterr().out

    def test_bad_item_emits_error_and_session_survives(self, monkeypatch):
        strategy = _TPlanStrategy(signals_at={
            _T0 + _XHOUR: [("long", 111.0, 106.0, None)]})
        strategy.fail_at.add(_T0)                    # first live candle blows up
        _t_patch_scheduler(monkeypatch, [
            _t_tick([_t_candle(0)]),
            _t_tick([_t_candle(1, 111.0)]),
        ])
        broker = _TFuturesBroker(_x_candles(10), fill_price=111.0)
        broker.ticker_last = 111.0
        t = _t_trader(broker=broker, strategy=strategy)
        events = _t_collect(t)
        thread = threading.Thread(target=t.run, daemon=True)
        thread.start()
        assert _t_wait(lambda: len(broker.orders_of_type("market")) >= 1)
        t.stop()
        thread.join(timeout=5.0)
        errors = [e for e in events if e.event_type == EVENT_ERROR
                  and e.where == "candles"]
        assert errors and errors[0].will_retry
        assert len(t.open_trades) == 1               # second tick still traded

    def test_user_data_unavailable_degrades_gracefully(self, monkeypatch):
        _t_patch_scheduler(monkeypatch, [])
        broker = _TFuturesBroker(_x_candles(10), fill_price=110.0)
        broker.raise_user_data = True
        t = _t_trader(broker=broker)
        events = _t_collect(t)
        worker = t._worker
        worker.seed()
        worker.start()
        try:
            errors = [e for e in events if e.event_type == EVENT_ERROR
                      and e.where == "user_data"]
            assert errors and "falls back" in errors[0].message
            assert worker._user_stream is None
        finally:
            worker.shutdown()

    def test_terminal_log_prints_live_lines(self, monkeypatch, capsys):
        strategy = _TPlanStrategy(signals_at={_T0: [("long", 110.0, 105.0, None)]})
        _t_patch_scheduler(monkeypatch, [_t_tick([_t_candle(0)])])
        broker = _TFuturesBroker(_x_candles(10), fill_price=110.0)
        broker.ticker_last = 110.0
        t = _t_trader(broker=broker, strategy=strategy, cfg=_t_cfg(log_events=True))
        thread = threading.Thread(target=t.run, daemon=True)
        thread.start()
        assert _t_wait(lambda: len(t.open_trades) == 1)
        t.stop()
        thread.join(timeout=5.0)
        out = capsys.readouterr().out
        assert "[LIVE][BTCUSDT] SIGNAL long @ 110" in out
        assert "[LIVE][BTCUSDT] OPEN #0 long filled @ 110" in out
        assert "[SIM]" not in out


# ===========================================================================
# — multi-RR live ladders
# ===========================================================================
#
# Futures numbers used throughout: signal long 100 / SL 95 → sl_distance 5 →
# levels [1, 2, 3] land at 105 / 110 / 115; risk 1 % of 10 000 = $100 →
# size 20 units; fractions [0.5, 0.3, 0.2] → level quantities 10 / 6 / 4.
# MT5 numbers: EURUSD long 1.1000 / SL 1.0950 → levels 1.105 / 1.110;
# risk $100 over 50 pips → 0.2 lots; fractions [0.5, 0.5] → 0.1 lots each.


def _l_cfg(**overrides) -> TraderConfig:
    kwargs = {"tp_mode": TP_MODE_MULTI_RR, "tp_levels": [1.0, 2.0, 3.0],
              "tp_level_close_fractions": [0.5, 0.3, 0.2]}
    kwargs.update(overrides)
    return _x_cfg(**kwargs)


def _l_mt5_cfg(**overrides) -> TraderConfig:
    kwargs = {"symbol": "EURUSD", "commission_type": "per_lot",
              "tp_mode": TP_MODE_MULTI_RR, "tp_levels": [1.0, 2.0],
              "tp_level_close_fractions": [0.5, 0.5]}
    kwargs.update(overrides)
    return _x_cfg(**kwargs)


def _l_order_id(trade, level_idx: int) -> str:
    """The venue order id of *trade*'s ladder level *level_idx*."""
    for oid, idx in trade.ladder_order_ids.items():
        if idx == level_idx:
            return oid
    raise AssertionError(f"no ladder order tracked for level {level_idx}")


def _l_fill_msg(order_id: str, price: float, qty: float, t: int) -> dict:
    """Binance futures user-data ORDER_TRADE_UPDATE for a filled ladder order."""
    return {"e": "ORDER_TRADE_UPDATE", "E": t,
            "o": {"X": "FILLED", "i": order_id, "ap": str(price), "z": str(qty)}}


def _l_mt5_engine(**cfg_overrides):
    """(engine, broker, events, trade) — MT5 multi-RR engine with one open
    long 1.1000 / SL 1.0950 (0.2 lots).  fill_price stays 0.0 so the entry
    falls back to the reference and a partial close falls back to the level
    price."""
    broker = _XMT5Broker()
    engine, broker, events = _x_engine(broker, _l_mt5_cfg(**cfg_overrides))
    trade = engine.open_position(_x_sig(entry=1.1000, sl=1.0950))
    return engine, broker, events, trade


class TestLadderPlacement:
    """ entry-time ladder orders: Binance futures reduce-only limits."""

    def test_futures_fraction_ladder_orders(self):
        engine, broker, _ = _x_engine(config=_l_cfg())
        trade = engine.open_position(_x_sig())
        limits = broker.orders_of_type("limit")
        assert [o["price"] for o in limits] == [105.0, 110.0, 115.0]
        assert [o["quantity"] for o in limits] == [
            pytest.approx(10.0), pytest.approx(6.0), pytest.approx(4.0)]
        assert all(o["reduce_only"] is True and o["side"] == "sell" for o in limits)
        assert [o["client_order_id"] for o in limits] == [
            trade.client_order_id + f"-tp{k}" for k in (1, 2, 3)]
        assert trade.ladder_order_ids == {
            limits[0]["order_id"]: 0, limits[1]["order_id"]: 1, limits[2]["order_id"]: 2}

    def test_short_ladder_orders_buy_side(self):
        engine, broker, _ = _x_engine(config=_l_cfg())
        engine.open_position(_x_sig(entry=100.0, sl=105.0, direction="short"))
        limits = broker.orders_of_type("limit")
        assert [o["price"] for o in limits] == [95.0, 90.0, 85.0]
        assert all(o["side"] == "buy" for o in limits)

    def test_zero_fraction_level_gets_no_order(self):
        engine, broker, _ = _x_engine(config=_l_cfg(tp_level_close_fractions=[0.5, 0.0, 0.5]))
        trade = engine.open_position(_x_sig())
        limits = broker.orders_of_type("limit")
        assert [o["price"] for o in limits] == [105.0, 115.0]
        assert sorted(trade.ladder_order_ids.values()) == [0, 2]

    def test_mt5_places_no_ladder_orders(self):
        engine, broker, _, trade = _l_mt5_engine()
        assert broker.orders_of_type("limit") == []
        assert trade.ladder_order_ids == {}
        (entry,) = broker.orders
        assert entry["take_profit"] is None          # multi_rr sends no venue TP
        assert trade.pos.next_tp == pytest.approx(1.105)

    def test_spot_multi_rr_rejected(self):
        with pytest.raises(ValueError, match="not supported on Binance spot"):
            ExecutionEngine(_XSpotBroker(), _l_cfg(), events=EventStream())

    def test_placement_failure_degrades_to_feed_detection(self):
        engine, broker, events = _x_engine(config=_l_cfg())
        broker.fail_order_types = {"limit"}
        trade = engine.open_position(_x_sig())
        errors = [e for e in _x_events(events, EVENT_ERROR) if e.where == "place_ladder"]
        assert len(errors) == 3
        assert all("degrades to feed detection" in e.message for e in errors)
        assert trade.ladder_order_ids == {} and trade in engine.open_trades
        assert trade.sl_order_id                     # the SL stayed armed
        # The degraded level settles from the feed: market execution on touch.
        broker.fail_order_types = set()
        engine.check_tp_levels(105.2, 101.0, _X0 + _XHOUR)
        fallback = [o for o in broker.orders_of_type("market") if o.get("reduce_only")]
        assert fallback and fallback[-1]["quantity"] == pytest.approx(10.0)
        (tp_event,) = _x_events(events, EVENT_TP_LEVEL)
        assert tp_event.level == 1.0
        assert tp_event.trade.exit_price == 105.0    # venue gave no price → level price
        assert trade.pos.size == pytest.approx(10.0) and trade.pos.stop_loss == 100.0

    def test_rejected_status_ladder_order_also_degrades(self):
        engine, broker, events = _x_engine(config=_l_cfg())
        broker.reject_order_types = {"limit"}
        trade = engine.open_position(_x_sig())
        errors = [e for e in _x_events(events, EVENT_ERROR) if e.where == "place_ladder"]
        assert len(errors) == 3 and trade.ladder_order_ids == {}


class TestLadderFillsFutures:
    """ venue-native fills (user-data path via record_ladder_fill)."""

    def test_first_level_fill_partial_slice_and_events(self):
        engine, broker, events = _x_engine(config=_l_cfg())
        trade = engine.open_position(_x_sig())
        sl_before = trade.sl_order_id
        t1 = _X0 + _XHOUR
        closed = engine.record_ladder_fill(
            trade, _l_order_id(trade, 0), 105.0, 10.0, close_time=t1)
        # slice accounting — shares the trade id, reason tp_rr
        assert closed.close_reason == CLOSE_REASON_TP_PARTIAL
        assert closed.trade_id == trade.pos.trade_id
        assert closed.size == pytest.approx(10.0)
        assert closed.net_pnl == pytest.approx(50.0)
        assert closed.pnl_r == pytest.approx(1.0)
        assert closed.rr_levels_hit == 1 and closed.final_next_tp == 110.0
        # Ladder advanced: SL → break-even, next level armed
        assert trade.pos.size == pytest.approx(10.0)
        assert trade.pos.stop_loss == 100.0 and trade.pos.next_tp == 110.0
        assert trade.pos.sl_history[-1] == {"time": t1, "sl": 100.0, "next_tp": 110.0}
        # Venue SL move: new stop first (quantity-scoped to the REMAINING
        # size), old cancelled second
        stop = broker.orders_of_type("stop")[-1]
        assert stop["stop_price"] == 100.0 and stop["quantity"] == pytest.approx(10.0)
        assert broker.cancels == [sl_before]
        assert trade.sl_order_id == stop["order_id"]
        # Events: the ladder move rides TP_LEVEL — no standalone SL_MOVE
        (tp_event,) = _x_events(events, EVENT_TP_LEVEL)
        assert tp_event.time == t1 and tp_event.level == 1.0
        assert tp_event.fraction_closed == pytest.approx(0.5)
        assert tp_event.realized_pnl == pytest.approx(50.0)
        assert tp_event.new_sl == 100.0 and tp_event.trade is closed
        assert _x_events(events, EVENT_SL_MOVE) == []
        assert _x_events(events, EVENT_CLOSE) == []
        assert 0 not in trade.ladder_order_ids.values()   # L1's order consumed

    def test_full_ladder_run_to_final_close(self):
        engine, broker, events = _x_engine(config=_l_cfg())
        trade = engine.open_position(_x_sig())
        for level_idx, (price, qty) in enumerate([(105.0, 10.0), (110.0, 6.0), (115.0, 4.0)]):
            engine.record_ladder_fill(
                trade, _l_order_id(trade, level_idx), price, qty,
                close_time=_X0 + (level_idx + 1) * _XHOUR)
        assert engine.open_trades == ()
        tp_events = _x_events(events, EVENT_TP_LEVEL)
        (close_event,) = _x_events(events, EVENT_CLOSE)
        assert [e.level for e in tp_events] == [1.0, 2.0]
        final = close_event.trade
        assert final.close_reason == CLOSE_REASON_TP and final.size == pytest.approx(4.0)
        assert final.final_stop_loss == 110.0 and final.final_next_tp is None
        assert final.rr_levels_hit == 3
        # Realized PnL across the slices: 10×5 + 6×10 + 4×15 = 170
        realized = sum(e.realized_pnl for e in tp_events) + close_event.net_pnl
        assert realized == pytest.approx(170.0)
        # The leftover SL stop was cancelled after the final fill
        assert trade.sl_order_id in broker.cancels
        assert trade.ladder_order_ids == {}

    def test_remainder_stays_open_below_sum_one(self):
        engine, broker, events = _x_engine(
            config=_l_cfg(tp_levels=[1.0, 2.0], tp_level_close_fractions=[0.4, 0.4]))
        trade = engine.open_position(_x_sig())
        engine.record_ladder_fill(trade, _l_order_id(trade, 0), 105.0, 8.0,
                                  close_time=_X0 + _XHOUR)
        closed = engine.record_ladder_fill(trade, _l_order_id(trade, 1), 110.0, 8.0,
                                           close_time=_X0 + 2 * _XHOUR)
        # Final level consumed but 20 % of the size stays open (sim parity)
        assert closed.close_reason == CLOSE_REASON_TP_PARTIAL
        assert trade in engine.open_trades
        assert trade.pos.size == pytest.approx(4.0)
        assert trade.pos.next_tp is None and trade.pos.stop_loss == 105.0
        assert trade.ladder_order_ids == {}
        assert _x_events(events, EVENT_CLOSE) == []
        assert len(_x_events(events, EVENT_TP_LEVEL)) == 2

    def test_out_of_order_fill_catches_up(self):
        engine, broker, events = _x_engine(config=_l_cfg())
        trade = engine.open_position(_x_sig())
        t2 = _X0 + 2 * _XHOUR
        # L2 fill arrives while L1's fill is missing → L1 advances advance-only
        closed2 = engine.record_ladder_fill(
            trade, _l_order_id(trade, 1), 110.0, 6.0, close_time=t2)
        (move,) = _x_events(events, EVENT_SL_MOVE)
        assert move.cause == "ladder" and (move.old_sl, move.new_sl) == (95.0, 100.0)
        assert closed2.rr_levels_hit == 2
        assert trade.pos.last_rr_hit == 2 and trade.pos.stop_loss == 105.0
        assert trade.pos.next_tp == 115.0
        assert trade.pos.size == pytest.approx(14.0)
        # The late L1 fill records its slice WITHOUT re-advancing the ladder
        history_len = len(trade.pos.sl_history)
        closed1 = engine.record_ladder_fill(
            trade, _l_order_id(trade, 0), 105.0, 10.0, close_time=t2 + 1)
        assert closed1.close_reason == CLOSE_REASON_TP_PARTIAL
        assert closed1.size == pytest.approx(10.0)
        assert trade.pos.last_rr_hit == 2 and len(trade.pos.sl_history) == history_len
        assert trade.pos.size == pytest.approx(4.0)
        assert [e.level for e in _x_events(events, EVENT_TP_LEVEL)] == [2.0, 1.0]

    def test_sl_modify_failure_after_fill_pending_retry(self):
        engine, broker, events = _x_engine(config=_l_cfg())
        trade = engine.open_position(_x_sig())
        broker.fail_order_types = {"stop"}           # the replacement stop fails
        closed = engine.record_ladder_fill(
            trade, _l_order_id(trade, 0), 105.0, 10.0, close_time=_X0 + _XHOUR)
        # The realized close cannot revert: state stays advanced, retry armed
        assert closed.close_reason == CLOSE_REASON_TP_PARTIAL
        assert trade.pos.stop_loss == 100.0 and trade.pos.last_rr_hit == 1
        assert trade.pending_sl_sync is True
        errors = [e for e in _x_events(events, EVENT_ERROR) if e.where == "ladder_sl_move"]
        assert errors and errors[0].will_retry
        assert len(_x_events(events, EVENT_TP_LEVEL)) == 1   # the level still reported
        # Next closed candle retries the venue modify until it lands
        engine.update_stops({"timestamp": _X0 + 2 * _XHOUR,
                             "high": 102.0, "low": 100.5, "close": 101.0})
        assert trade.pending_sl_sync is True                 # still failing
        broker.fail_order_types = set()
        engine.update_stops({"timestamp": _X0 + 3 * _XHOUR,
                             "high": 102.0, "low": 100.5, "close": 101.0})
        assert trade.pending_sl_sync is False
        stop = broker.orders_of_type("stop")[-1]
        assert stop["stop_price"] == 100.0 and stop["quantity"] == pytest.approx(10.0)

    def test_fill_price_and_quantity_fallbacks(self):
        engine, broker, _ = _x_engine(config=_l_cfg())
        trade = engine.open_position(_x_sig())
        closed = engine.record_ladder_fill(
            trade, _l_order_id(trade, 0), 0.0, 0.0, close_time=_X0 + _XHOUR)
        assert closed.exit_price == 105.0            # level price fallback
        assert closed.size == pytest.approx(10.0)    # planned level quantity fallback

    def test_record_ladder_fill_guards(self):
        engine, broker, _ = _x_engine(config=_l_cfg())
        trade = engine.open_position(_x_sig())
        with pytest.raises(ValueError, match="not a ladder order"):
            engine.record_ladder_fill(trade, "no-such-order", 105.0, 10.0)
        engine.force_close()
        with pytest.raises(ValueError, match="not tracked"):
            engine.record_ladder_fill(trade, "1", 105.0, 10.0)

    def test_force_close_cancels_ladder_orders(self):
        engine, broker, _ = _x_engine(config=_l_cfg())
        trade = engine.open_position(_x_sig())
        ladder_ids = list(trade.ladder_order_ids)
        engine.force_close()
        for oid in ladder_ids:
            assert oid in broker.cancels

    def test_sl_history_and_slice_parity_with_stepper(self):
        """: multi-RR SL modification sequences equal simulate's sl_history —
        full-precision asdict equality of every slice between a hand-driven
        SimulationStepper and the live engine over the same candles."""
        engine, broker, _ = _x_engine(config=_l_cfg())
        sig = _x_sig()                        # long 100 / SL 95 @ candle 0
        candles = [
            {"timestamp": _X0, "open": 99.5, "high": 100.5, "low": 99.0, "close": 100.0},
            {"timestamp": _X0 + _XHOUR, "open": 101.0, "high": 105.5, "low": 100.8,
             "close": 104.0},
            {"timestamp": _X0 + 2 * _XHOUR, "open": 106.0, "high": 110.5, "low": 105.8,
             "close": 109.0},
            {"timestamp": _X0 + 3 * _XHOUR, "open": 111.0, "high": 115.5, "low": 110.8,
             "close": 114.0},
        ]
        # Sim reference — driven by the byte-authoritative maths, with
        # the engine's own derived config so every cost knob is identical.
        stepper = SimulationStepper(engine.sim_config)
        stepper.step(candles[0], signals=[sig])
        for candle in candles[1:]:
            stepper.step(candle)
        sim_trades = list(stepper.closed_trades)
        assert [t.close_reason for t in sim_trades] == [
            CLOSE_REASON_TP_PARTIAL, CLOSE_REASON_TP_PARTIAL, CLOSE_REASON_TP]

        # Live drive: same candles; each level's venue fill lands at the level
        # price on the candle that touches it (quantity 0 → planned size, the
        # exact sim fraction).
        trade = engine.open_position(sig)
        live_trades = []
        for candle in candles[1:]:
            engine.update_stops(candle)
            level_idx = trade.pos.last_rr_hit
            live_trades.append(engine.record_ladder_fill(
                trade, _l_order_id(trade, level_idx),
                trade.pos.tp_level_prices[level_idx], 0.0,
                close_time=int(candle["timestamp"])))
        assert engine.open_trades == ()
        assert [dataclasses.asdict(t) for t in live_trades] == \
            [dataclasses.asdict(t) for t in sim_trades]

    def test_fraction_less_parity_with_stepper(self):
        """fractions=None: intermediate levels advance-only (feed-detected),
        final level = full venue-native close — one trade, sim-identical."""
        engine, broker, events = _x_engine(config=_l_cfg(tp_level_close_fractions=None))
        sig = _x_sig()
        candles = [
            {"timestamp": _X0, "open": 99.5, "high": 100.5, "low": 99.0, "close": 100.0},
            {"timestamp": _X0 + _XHOUR, "open": 101.0, "high": 105.5, "low": 100.8,
             "close": 104.0},
            {"timestamp": _X0 + 2 * _XHOUR, "open": 106.0, "high": 110.5, "low": 105.8,
             "close": 109.0},
            {"timestamp": _X0 + 3 * _XHOUR, "open": 111.0, "high": 115.5, "low": 110.8,
             "close": 114.0},
        ]
        stepper = SimulationStepper(engine.sim_config)
        stepper.step(candles[0], signals=[sig])
        for candle in candles[1:]:
            stepper.step(candle)
        (sim_trade,) = stepper.closed_trades

        trade = engine.open_position(sig)
        (ladder_order,) = broker.orders_of_type("limit")    # final level only, full size
        assert ladder_order["price"] == 115.0
        assert ladder_order["quantity"] == pytest.approx(20.0)
        for candle in candles[1:3]:
            engine.update_stops(candle)
            engine.check_tp_levels(candle["high"], candle["low"],
                                   int(candle["timestamp"]))
        moves = _x_events(events, EVENT_SL_MOVE)
        assert [(m.old_sl, m.new_sl) for m in moves] == [(95.0, 100.0), (100.0, 105.0)]
        assert all(m.cause == "ladder" for m in moves)
        engine.update_stops(candles[3])
        # The armed venue order owns the final level — feed detection skips it
        engine.check_tp_levels(candles[3]["high"], candles[3]["low"],
                               int(candles[3]["timestamp"]))
        assert engine.open_trades == (trade,)
        live_trade = engine.record_ladder_fill(
            trade, _l_order_id(trade, 2), 115.0, 0.0,
            close_time=int(candles[3]["timestamp"]))
        assert dataclasses.asdict(live_trade) == dataclasses.asdict(sim_trade)


class TestLadderFeedMT5:
    """ MT5: feed-detected levels → partial-close market orders."""

    def test_feed_touch_partial_close_and_sl_move(self):
        engine, broker, events, trade = _l_mt5_engine()
        t1 = _X0 + _XHOUR
        engine.check_tp_levels(1.106, 1.100, t1)
        # Partial close by volume on the position ticket, at market
        assert broker.closes == [("EURUSD", pytest.approx(0.1), 7777)]
        assert broker.modifies[-1] == (7777, pytest.approx(1.1), None)
        (tp_event,) = _x_events(events, EVENT_TP_LEVEL)
        assert tp_event.level == 1.0 and tp_event.fraction_closed == pytest.approx(0.5)
        assert tp_event.new_sl == pytest.approx(1.1)
        assert tp_event.trade.exit_price == pytest.approx(1.105)   # venue gave no price
        assert tp_event.trade.close_reason == CLOSE_REASON_TP_PARTIAL
        assert tp_event.realized_pnl == pytest.approx(50.0)
        assert trade.pos.size == pytest.approx(0.1)
        assert trade.pos.next_tp == pytest.approx(1.110)
        assert trade.pos.sl_history[-1]["time"] == t1
        # No double settlement when the same candle is re-checked
        engine.check_tp_levels(1.106, 1.100, t1)
        assert len(broker.closes) == 1

    def test_gap_through_two_levels_one_candle(self):
        engine, broker, events, trade = _l_mt5_engine()
        engine.check_tp_levels(1.1150, 1.1000, _X0 + _XHOUR)
        assert [c[1] for c in broker.closes] == [pytest.approx(0.1), pytest.approx(0.1)]
        (tp_event,) = _x_events(events, EVENT_TP_LEVEL)
        (close_event,) = _x_events(events, EVENT_CLOSE)
        assert tp_event.level == 1.0
        final = close_event.trade
        assert final.close_reason == CLOSE_REASON_TP
        assert final.exit_price == pytest.approx(1.110)
        assert final.final_stop_loss == pytest.approx(1.105)
        assert final.rr_levels_hit == 2
        assert engine.open_trades == ()
        # Only the L1 ladder step moved the venue SL — L2 closed everything
        assert [m[1] for m in broker.modifies] == [pytest.approx(1.1)]

    def test_fraction_less_advance_only_then_final_full_close(self):
        engine, broker, events, trade = _l_mt5_engine(tp_level_close_fractions=None)
        t1, t2 = _X0 + _XHOUR, _X0 + 2 * _XHOUR
        engine.check_tp_levels(1.106, 1.100, t1)
        assert broker.closes == []                   # nothing realized at L1
        assert broker.modifies[-1] == (7777, pytest.approx(1.1), None)
        (move,) = _x_events(events, EVENT_SL_MOVE)
        assert move.cause == "ladder" and move.next_tp == pytest.approx(1.110)
        assert trade.pos.sl_history[-1] == {
            "time": t1, "sl": pytest.approx(1.1), "next_tp": pytest.approx(1.110)}
        engine.check_tp_levels(1.1115, 1.1050, t2)   # final level → full market close
        assert broker.closes == [("EURUSD", pytest.approx(0.2), 7777)]
        (close_event,) = _x_events(events, EVENT_CLOSE)
        final = close_event.trade
        assert final.close_reason == CLOSE_REASON_TP
        assert final.exit_price == pytest.approx(1.110)
        assert final.final_stop_loss == pytest.approx(1.105)
        assert final.rr_levels_hit == 2 and final.final_next_tp is None
        assert engine.open_trades == ()

    def test_advance_only_venue_failure_reverts_and_redetects(self):
        engine, broker, events, trade = _l_mt5_engine(tp_level_close_fractions=None)
        broker.fail_modify = True
        engine.check_tp_levels(1.106, 1.100, _X0 + _XHOUR)
        # Nothing was realized → the advance is fully reverted (trailing rule)
        assert trade.pos.stop_loss == 1.0950 and trade.pos.last_rr_hit == 0
        assert trade.pos.next_tp == pytest.approx(1.105)
        assert len(trade.pos.sl_history) == 1        # entry record only
        errors = [e for e in _x_events(events, EVENT_ERROR) if e.where == "ladder_sl_move"]
        assert errors and errors[0].will_retry
        assert _x_events(events, EVENT_SL_MOVE) == []
        # The untouched next_tp re-detects the level once the venue recovers
        broker.fail_modify = False
        engine.check_tp_levels(1.106, 1.100, _X0 + 2 * _XHOUR)
        assert trade.pos.stop_loss == pytest.approx(1.1)
        assert len(_x_events(events, EVENT_SL_MOVE)) == 1

    def test_partial_close_venue_failure_retries(self):
        engine, broker, events, trade = _l_mt5_engine()
        broker.fail_order_types = {"close"}
        engine.check_tp_levels(1.106, 1.100, _X0 + _XHOUR)
        errors = [e for e in _x_events(events, EVENT_ERROR) if e.where == "tp_level_close"]
        assert errors and errors[0].will_retry
        assert trade.pos.last_rr_hit == 0 and trade.pos.next_tp == pytest.approx(1.105)
        assert trade.pos.size == pytest.approx(0.2)
        assert _x_events(events, EVENT_TP_LEVEL) == []
        broker.fail_order_types = set()
        engine.check_tp_levels(1.106, 1.100, _X0 + 2 * _XHOUR)
        assert trade.pos.size == pytest.approx(0.1)
        assert len(_x_events(events, EVENT_TP_LEVEL)) == 1


class TestLadderWorkerWiring:
    """ wiring in the worker: detection feed, routing, cleanup."""

    def test_detection_feed_subscription_matrix(self, monkeypatch):
        _t_patch_scheduler(monkeypatch, [])
        # MT5 multi-RR pair in candle_close mode → forming stream for ladder
        # detection only
        mt5 = _TMT5Broker(_x_candles(10, start_close=1.09, step=0.001))
        cfg = _t_cfg(symbol="EURUSD", tp_mode=TP_MODE_MULTI_RR, tp_levels=[1.0, 2.0],
                     tp_level_close_fractions=[0.5, 0.5], commission_type="per_lot")
        t = _t_trader(broker=mt5, cfg=cfg)
        worker = t._worker
        worker.seed()
        worker.start()
        try:
            assert mt5.stream_candles_calls == [
                {"symbol": "EURUSD", "timeframe": "1h", "closed_only": False}]
        finally:
            worker.shutdown()
        # Futures multi-RR (venue-native ladder) gets NO detection feed
        fut = _TFuturesBroker(_x_candles(10), fill_price=110.0)
        t2 = _t_trader(broker=fut, cfg=_t_cfg(
            tp_mode=TP_MODE_MULTI_RR, tp_levels=[1.0, 2.0],
            tp_level_close_fractions=[0.5, 0.5]))
        worker2 = t2._worker
        worker2.seed()
        worker2.start()
        try:
            assert fut.stream_candles_calls == []
        finally:
            worker2.shutdown()
        # MT5 without multi_rr keeps candle_close pure — no stream either
        mt5b = _TMT5Broker(_x_candles(10, start_close=1.09, step=0.001))
        t3 = _t_trader(broker=mt5b, cfg=_t_cfg(symbol="EURUSD",
                                               commission_type="per_lot"))
        worker3 = t3._worker
        worker3.seed()
        worker3.start()
        try:
            assert mt5b.stream_candles_calls == []
        finally:
            worker3.shutdown()

    def test_forming_item_settles_level_without_strategy_eval(self):
        mt5 = _TMT5Broker(_x_candles(10, start_close=1.09, step=0.001))
        cfg = _t_cfg(symbol="EURUSD", tp_mode=TP_MODE_MULTI_RR, tp_levels=[1.0, 2.0],
                     tp_level_close_fractions=[0.5, 0.5], commission_type="per_lot")
        strategy = _TPlanStrategy(signals_at={_T0: [("long", 1.2, 1.19, None)]})
        t = _t_trader(broker=mt5, cfg=cfg, strategy=strategy)
        events = _t_collect(t)
        worker = _t_ready(t)
        t.engine.open_position(Signal("long", 1.1, 1.095, None, _X0 + 9 * _XHOUR, 9, "1h"))
        forming = {"timestamp": _T0, "open": 1.1, "high": 1.106, "low": 1.099,
                   "close": 1.1055, "volume": 0.0}
        worker._process_forming(dict(forming))
        # The level settled the moment the forming price touched it...
        assert mt5.closes and mt5.closes[0][1] == pytest.approx(0.1)
        assert [e for e in events if e.event_type == EVENT_TP_LEVEL]
        # ...with NO strategy evaluation (candle_close mode: detection only)
        assert [e for e in events if e.event_type == EVENT_SIGNAL] == []
        assert len(worker.data["1h"]) == 10          # master frame untouched
        # Committing the same candle later does not settle the level twice —
        # and only NOW does the strategy evaluate (SIGNAL fires at commit).
        worker._process_scheduler_tick(_t_tick([{**forming, "volume": 1.0}]))
        assert len(mt5.closes) == 1
        assert [e for e in events if e.event_type == EVENT_SIGNAL]

    def test_scheduler_candle_runs_ladder_check(self):
        mt5 = _TMT5Broker(_x_candles(10, start_close=1.09, step=0.001))
        cfg = _t_cfg(symbol="EURUSD", tp_mode=TP_MODE_MULTI_RR, tp_levels=[1.0, 2.0],
                     tp_level_close_fractions=[0.5, 0.5], commission_type="per_lot")
        t = _t_trader(broker=mt5, cfg=cfg)
        worker = _t_ready(t)
        t.engine.open_position(Signal("long", 1.1, 1.095, None, _X0 + 9 * _XHOUR, 9, "1h"))
        candle = {"timestamp": _T0, "open": 1.1, "high": 1.106, "low": 1.099,
                  "close": 1.104, "volume": 1.0}
        worker._process_scheduler_tick(_t_tick([candle]))
        # Closed-candle commit is the authoritative catch-up detection
        assert mt5.closes and mt5.closes[0][1] == pytest.approx(0.1)

    def test_user_data_routes_ladder_fill_and_collects_slice(self):
        fut = _TFuturesBroker(_x_candles(10), fill_price=0.0)
        cfg = _t_cfg(tp_mode=TP_MODE_MULTI_RR, tp_levels=[1.0, 2.0, 3.0],
                     tp_level_close_fractions=[0.5, 0.3, 0.2])
        t = _t_trader(broker=fut, cfg=cfg)
        events = _t_collect(t)
        worker = _t_ready(t)
        trade = t.engine.open_position(_x_sig(ts=_T0))
        worker._process_user_data(
            _l_fill_msg(_l_order_id(trade, 0), 105.0, 10.0, _T0 + _XHOUR))
        assert trade.pos.size == pytest.approx(10.0)
        assert trade.pos.stop_loss == 100.0
        (tp_event,) = [e for e in events if e.event_type == EVENT_TP_LEVEL]
        assert tp_event.time == _T0 + _XHOUR
        #: partial ladder slices land in Trader.closed_trades too
        assert [ct.close_reason for ct in t.closed_trades] == [CLOSE_REASON_TP_PARTIAL]
        assert t.closed_trades[0].size == pytest.approx(10.0)

    def test_sl_fill_after_level_cancels_remaining_ladder(self):
        fut = _TFuturesBroker(_x_candles(10), fill_price=0.0)
        cfg = _t_cfg(tp_mode=TP_MODE_MULTI_RR, tp_levels=[1.0, 2.0, 3.0],
                     tp_level_close_fractions=[0.5, 0.3, 0.2])
        t = _t_trader(broker=fut, cfg=cfg)
        worker = _t_ready(t)
        trade = t.engine.open_position(_x_sig(ts=_T0))
        worker._process_user_data(
            _l_fill_msg(_l_order_id(trade, 0), 105.0, 10.0, _T0 + _XHOUR))
        remaining = list(trade.ladder_order_ids)     # L2 + L3 still on the book
        assert len(remaining) == 2
        worker._process_user_data({
            "e": "ORDER_TRADE_UPDATE", "E": _T0 + 2 * _XHOUR,
            "o": {"X": "FILLED", "i": trade.sl_order_id, "ap": "100.0", "z": "10"}})
        assert t.engine.open_trades == ()
        closed = t.closed_trades[-1]
        # SL at break-even after a ladder advance maps to rf (sim parity)
        assert closed.close_reason == CLOSE_REASON_RF
        assert closed.exit_price == pytest.approx(100.0)
        for oid in remaining:
            assert oid in fut.cancels


# ===========================================================================
# — safety rails
# ===========================================================================
#
# Futures numbers used throughout: signal long 110 / SL 105 → sl_distance 5;
# risk 1 % of 10 000 = $100 → size 20 units; an SL fill at 105 realizes
# −$100 net (zero costs); unrealised PnL at price p = (p − 110) × 20.
# UTC-day arithmetic on the _T0 grid: candles 0–15 share one UTC day,
# candle 16 (_T0 + 16 h) opens the next one.

_DAY_MS = 86_400_000


def _s18_trader(cfg_overrides=None, signals_at=None, exits_at=None, **trader_kw):
    """(trader, broker) — futures pair on the harness, fill @ 110."""
    strategy = _TPlanStrategy(signals_at=signals_at or {}, exits_at=exits_at or {})
    broker = _TFuturesBroker(_x_candles(10), fill_price=110.0)
    broker.ticker_last = 110.0
    t = _t_trader(broker=broker, strategy=strategy, cfg=_t_cfg(**(cfg_overrides or {})),
                  **trader_kw)
    return t, broker


def _s18_sl_fill(worker, time_ms):
    """Fill the single open trade's venue SL at 105 via user-data (−$100)."""
    (trade,) = worker.engine.open_trades
    worker._process_user_data({
        "e": "ORDER_TRADE_UPDATE", "E": time_ms,
        "o": {"X": "FILLED", "i": trade.sl_order_id, "ap": "105.0",
              "z": str(trade.pos.size)},
    })


def _s18_daily_events(events):
    return [e for e in events if e.event_type == EVENT_DAILY_LOSS]


class TestDailyLossGate:
    def test_day_grid_assumption(self):
        # The comments above rely on this: candle 16 is the next UTC day.
        assert (_T0 + 15 * _XHOUR) // _DAY_MS == _T0 // _DAY_MS
        assert (_T0 + 16 * _XHOUR) // _DAY_MS == _T0 // _DAY_MS + 1

    def test_disabled_by_default(self):
        t, broker = _s18_trader(
            {"max_positions": 2, "max_long_positions": 2},
            signals_at={_T0: [("long", 110.0, 105.0, None)],
                        _T0 + 2 * _XHOUR: [("long", 110.0, 105.0, None)]},
        )
        events = _t_collect(t)
        worker = _t_ready(t)
        worker._process_scheduler_tick(_t_tick([_t_candle(0)]))
        worker._process_scheduler_tick(_t_tick([_t_candle(1, close=90.0)]))  # −$400 open
        worker._process_scheduler_tick(_t_tick([_t_candle(2)]))
        assert _s18_daily_events(events) == []
        assert len(broker.orders_of_type("market")) == 2   # second entry allowed

    def test_trips_on_realized_loss_and_blocks_entries(self, capsys):
        t, broker = _s18_trader(
            {"max_daily_loss": 50.0},
            signals_at={_T0: [("long", 110.0, 105.0, None)],
                        _T0 + 2 * _XHOUR: [("long", 110.0, 105.0, None)]},
        )
        events = _t_collect(t)
        worker = _t_ready(t)
        worker._process_scheduler_tick(_t_tick([_t_candle(0)]))
        _s18_sl_fill(worker, _T0 + 30 * 60 * 1000)          # realized −$100 today
        worker._process_scheduler_tick(_t_tick([_t_candle(1)]))
        (daily,) = _s18_daily_events(events)
        assert daily.loss == pytest.approx(100.0)
        assert daily.limit == 50.0 and daily.closed_all is False
        assert "MAX DAILY LOSS HIT" in capsys.readouterr().out
        # Same-day signal: SIGNAL event still prints, the entry is skipped.
        before = len(broker.orders_of_type("market"))
        worker._process_scheduler_tick(_t_tick([_t_candle(2)]))
        signals = [e for e in events if e.event_type == EVENT_SIGNAL]
        assert len(signals) == 2                            # entry + gated one
        assert len(broker.orders_of_type("market")) == before
        assert "max_daily_loss hit" in capsys.readouterr().out
        # Still only one DAILY_LOSS event for the day.
        worker._process_scheduler_tick(_t_tick([_t_candle(3)]))
        assert len(_s18_daily_events(events)) == 1

    def test_counts_unrealized_open_drawdown(self):
        t, _broker = _s18_trader(
            {"max_daily_loss": 50.0}, signals_at={_T0: [("long", 110.0, 105.0, None)]}
        )
        events = _t_collect(t)
        worker = _t_ready(t)
        worker._process_scheduler_tick(_t_tick([_t_candle(0)]))
        worker._process_scheduler_tick(_t_tick([_t_candle(1, close=107.0)]))  # −$60 open
        (daily,) = _s18_daily_events(events)
        assert daily.loss == pytest.approx(60.0)
        assert len(t.open_trades) == 1                      # not flattened by default

    def test_wins_offset_losses(self):
        t, _broker = _s18_trader(
            {"max_daily_loss": 50.0},
            signals_at={_T0 + _XHOUR: [("long", 110.0, 105.0, None)]},
        )
        events = _t_collect(t)
        worker = _t_ready(t)
        worker._process_scheduler_tick(_t_tick([_t_candle(0)]))
        # A realized +$100 win today, banked before the losing trade opens.
        first = worker.engine.open_position(
            Signal("long", 110.0, 105.0, None, _T0 + 10 * 60 * 1000, 0, "1h")
        )
        worker.engine.record_external_close(
            first, 115.0, CLOSE_REASON_MANUAL, close_time=_T0 + 20 * 60 * 1000
        )
        worker._process_scheduler_tick(_t_tick([_t_candle(1)]))          # opens the loser
        worker._process_scheduler_tick(_t_tick([_t_candle(2, close=104.0)]))
        assert _s18_daily_events(events) == []              # 100 − 120 → loss $20 < $50
        worker._process_scheduler_tick(_t_tick([_t_candle(3, close=102.0)]))
        (daily,) = _s18_daily_events(events)                # 100 − 160 → loss $60
        assert daily.loss == pytest.approx(60.0)

    def test_percent_threshold_uses_day_start_balance(self):
        t, _broker = _s18_trader(
            {"max_daily_loss": "1%"}, signals_at={_T0: [("long", 110.0, 105.0, None)]}
        )
        events = _t_collect(t)
        worker = _t_ready(t)
        worker._process_scheduler_tick(_t_tick([_t_candle(0)]))
        worker._process_scheduler_tick(_t_tick([_t_candle(1, close=107.0)]))
        assert _s18_daily_events(events) == []              # $60 < 1% of 10 000
        worker._process_scheduler_tick(_t_tick([_t_candle(2, close=104.0)]))
        (daily,) = _s18_daily_events(events)                # $120 ≥ $100
        assert daily.loss == pytest.approx(120.0)
        assert daily.limit == "1%"

    def test_percent_base_reread_on_day_rollover(self):
        t, broker = _s18_trader(
            {"max_daily_loss": "1%"}, signals_at={_T0: [("long", 110.0, 105.0, None)]}
        )
        events = _t_collect(t)
        worker = _t_ready(t)
        worker._process_scheduler_tick(_t_tick([_t_candle(0)]))
        worker._process_scheduler_tick(_t_tick([_t_candle(1, close=107.0)]))
        assert _s18_daily_events(events) == []              # day 1 base 10 000 → $100
        broker.balance = 5_000.0                            # venue balance at rollover
        worker._process_scheduler_tick(_t_tick([_t_candle(16, close=107.0)]))
        (daily,) = _s18_daily_events(events)                # new base → $50 ≤ $60 loss
        assert daily.loss == pytest.approx(60.0)
        assert daily.time == _T0 + 16 * _XHOUR

    def test_percent_base_kept_on_read_failure(self, monkeypatch):
        t, _broker = _s18_trader(
            {"max_daily_loss": "1%"}, signals_at={_T0: [("long", 110.0, 105.0, None)]}
        )
        events = _t_collect(t)
        worker = _t_ready(t)
        worker._process_scheduler_tick(_t_tick([_t_candle(0)]))

        def _down():
            raise BrokerError("account endpoint down")

        monkeypatch.setattr(t.engine, "read_balance", _down)
        worker._process_scheduler_tick(_t_tick([_t_candle(16, close=107.0)]))
        errors = [e for e in events if e.event_type == EVENT_ERROR
                  and e.where == "daily_loss"]
        assert errors and "balance read failed" in errors[0].message
        assert _s18_daily_events(events) == []              # old base 10 000 kept
        worker._process_scheduler_tick(_t_tick([_t_candle(17, close=104.0)]))
        (daily,) = _s18_daily_events(events)                # $120 ≥ $100 still trips
        assert daily.loss == pytest.approx(120.0)

    def test_resets_next_utc_day(self):
        t, broker = _s18_trader(
            {"max_daily_loss": 50.0},
            signals_at={_T0: [("long", 110.0, 105.0, None)],
                        _T0 + 16 * _XHOUR: [("long", 110.0, 105.0, None)]},
        )
        events = _t_collect(t)
        worker = _t_ready(t)
        worker._process_scheduler_tick(_t_tick([_t_candle(0)]))
        _s18_sl_fill(worker, _T0 + 30 * 60 * 1000)
        worker._process_scheduler_tick(_t_tick([_t_candle(1)]))          # trips day 1
        assert len(_s18_daily_events(events)) == 1
        worker._process_scheduler_tick(_t_tick([_t_candle(16)]))         # next UTC day
        assert len(broker.orders_of_type("market")) == 2    # entry allowed again
        assert len(_s18_daily_events(events)) == 1          # no re-trip (loss 0)

    def test_close_on_daily_loss_flattens(self, capsys):
        t, broker = _s18_trader(
            {"max_daily_loss": 50.0, "close_on_daily_loss": True},
            signals_at={_T0: [("long", 110.0, 105.0, None)]},
        )
        events = _t_collect(t)
        worker = _t_ready(t)
        worker._process_scheduler_tick(_t_tick([_t_candle(0)]))
        (trade,) = t.open_trades
        sl_id = trade.sl_order_id
        worker._process_scheduler_tick(_t_tick([_t_candle(1, close=107.0)]))
        (daily,) = _s18_daily_events(events)
        assert daily.closed_all is True
        assert t.open_trades == ()
        assert sl_id in broker.cancels                      # protection cancelled
        closes = [o for o in broker.orders_of_type("market") if o.get("reduce_only")]
        assert len(closes) == 1 and closes[0]["quantity"] == pytest.approx(20.0)
        assert [c.close_reason for c in t.closed_trades] == [CLOSE_REASON_FC]
        assert "Closing all positions" in capsys.readouterr().out

    def test_sl_management_continues_while_gated(self):
        t, broker = _s18_trader(
            {"max_daily_loss": 50.0, "sl_mode": "trailing", "trailing_sl_percent": 1.0},
            signals_at={_T0: [("long", 110.0, 105.0, None)]},
        )
        events = _t_collect(t)
        worker = _t_ready(t)
        worker._process_scheduler_tick(_t_tick([_t_candle(0)]))
        worker._process_scheduler_tick(_t_tick([_t_candle(1, close=107.0)]))  # trips
        assert len(_s18_daily_events(events)) == 1
        stops_before = len(broker.orders_of_type("stop"))
        worker._process_scheduler_tick(_t_tick([_t_candle(2, close=111.0)]))
        assert len(broker.orders_of_type("stop")) > stops_before   # venue SL replaced
        moves = [e for e in events if e.event_type == EVENT_SL_MOVE
                 and e.time == _T0 + 2 * _XHOUR]
        assert moves and moves[0].cause == "trailing"
        assert worker.daily_loss.blocked                    # gate stays for the day

    def test_exit_force_close_continues_while_gated(self):
        t, broker = _s18_trader(
            {"max_daily_loss": 50.0, "force_close_on_exit_signal": True},
            signals_at={_T0: [("long", 110.0, 105.0, None)],
                        _T0 + 2 * _XHOUR: [("long", 110.0, 105.0, None)]},
            exits_at={_T0 + 2 * _XHOUR: "give_up"},
        )
        events = _t_collect(t)
        worker = _t_ready(t)
        worker._process_scheduler_tick(_t_tick([_t_candle(0)]))
        worker._process_scheduler_tick(_t_tick([_t_candle(1, close=107.0)]))  # trips
        worker._process_scheduler_tick(_t_tick([_t_candle(2, close=107.0)]))
        assert t.open_trades == ()                          # exit close still ran
        assert [c.close_reason for c in t.closed_trades] == [CLOSE_REASON_FC]
        assert len(broker.orders_of_type("market")) == 2    # entry + close, no new entry
        exit_events = [e for e in events if e.event_type == EVENT_EXIT_SIGNAL]
        assert exit_events and exit_events[0].action == "force_close"

    def test_forming_row_trips_mid_candle(self):
        t, _broker = _s18_trader(
            {"max_daily_loss": 50.0, "close_on_daily_loss": True,
             "execution": EXEC_CANDLE_UPDATE},
            signals_at={_T0: [("long", 110.0, 105.0, None)]},
        )
        events = _t_collect(t)
        worker = _t_ready(t)
        worker._process_scheduler_tick(_t_tick([_t_candle(0)]))
        worker._process_forming({
            "timestamp": _T0 + _XHOUR, "open": 109.0, "high": 109.0,
            "low": 106.8, "close": 107.0, "volume": 0.0,
        })
        (daily,) = _s18_daily_events(events)
        assert daily.time == _T0 + _XHOUR and daily.closed_all is True
        assert t.open_trades == ()                          # flattened mid-candle


class TestKillSwitch:
    def test_kill_switch_file_stops_run(self, monkeypatch, tmp_path, capsys):
        _t_patch_scheduler(monkeypatch, [])
        kill = tmp_path / "kill"
        t, _broker = _s18_trader({}, kill_switch_file=kill)
        threading.Timer(0.3, kill.touch).start()
        assert t.run() == []
        out = capsys.readouterr().out
        assert "kill-switch file" in out and "detected — stopping" in out
        assert kill.exists()                                # never auto-deleted

    def test_stale_kill_switch_file_raises(self, monkeypatch, tmp_path):
        _t_patch_scheduler(monkeypatch, [])
        kill = tmp_path / "kill"
        kill.touch()
        t, broker = _s18_trader({}, kill_switch_file=kill)
        with pytest.raises(ValueError, match="already exists"):
            t.run()
        assert broker.fetch_calls == []                     # nothing started
        kill.unlink()                                       # user removes it → runnable
        threading.Timer(0.2, t.stop).start()
        assert t.run() == []

    def test_kill_switch_applies_on_stop_policy(self, monkeypatch, tmp_path):
        strategy_signals = {_T0: [("long", 110.0, 105.0, None)]}
        holder = _t_patch_scheduler(monkeypatch, [_t_tick([_t_candle(0)])])
        del holder
        kill = tmp_path / "kill"
        t, broker = _s18_trader({}, signals_at=strategy_signals,
                                kill_switch_file=kill, on_stop=ON_STOP_CLOSE_ALL)
        result: dict = {}
        thread = threading.Thread(target=lambda: result.update(r=t.run()), daemon=True)
        thread.start()
        assert _t_wait(lambda: len(t.open_trades) == 1)
        kill.touch()
        thread.join(timeout=5.0)
        assert not thread.is_alive()
        assert t.open_trades == ()
        assert [c.close_reason for c in result["r"]] == [CLOSE_REASON_FC]

    def test_no_watcher_without_kill_switch_file(self):
        t, _broker = _s18_trader({})
        assert t._start_kill_switch_watcher() is None


class TestSignalHandlers:
    def test_sigterm_stops_gracefully_and_restores_handlers(self, monkeypatch, capsys):
        _t_patch_scheduler(monkeypatch, [])
        t, _broker = _s18_trader({})
        before_int = signal.getsignal(signal.SIGINT)
        before_term = signal.getsignal(signal.SIGTERM)
        threading.Timer(0.3, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()
        assert t.run() == []
        assert "SIGTERM — stopping" in capsys.readouterr().out
        assert signal.getsignal(signal.SIGINT) is before_int
        assert signal.getsignal(signal.SIGTERM) is before_term

    def test_sigint_handler_prints_ctrl_c(self, monkeypatch, capsys):
        _t_patch_scheduler(monkeypatch, [])
        t, _broker = _s18_trader({})
        threading.Timer(0.3, lambda: _thread.interrupt_main()).start()
        assert t.run() == []
        assert "Ctrl+C — stopping" in capsys.readouterr().out

    def test_run_off_main_thread_skips_handlers(self, monkeypatch):
        holder = _t_patch_scheduler(monkeypatch, [])
        t, _broker = _s18_trader({})
        before_term = signal.getsignal(signal.SIGTERM)
        thread = threading.Thread(target=t.run, daemon=True)
        thread.start()
        assert _t_wait(lambda: "instance" in holder)        # worker started
        time.sleep(0.2)                                     # handler-install point passed
        assert signal.getsignal(signal.SIGTERM) is before_term
        t.stop()
        thread.join(timeout=5.0)
        assert not thread.is_alive()


class TestOnStopPolicy:
    def _opened(self, **trader_kw):
        t, broker = _s18_trader(
            {}, signals_at={_T0: [("long", 110.0, 105.0, None)]}, **trader_kw
        )
        worker = _t_ready(t)
        worker._process_scheduler_tick(_t_tick([_t_candle(0)]))
        assert len(t.open_trades) == 1
        return t, broker, worker

    def test_keep_is_default(self, capsys):
        t, broker, worker = self._opened()
        assert t.settings.on_stop == ON_STOP_KEEP
        worker.shutdown()
        assert len(t.open_trades) == 1                      # position stays
        assert broker.cancels == []                         # protection untouched
        assert t.closed_trades == ()
        assert "kept — the venue SL/TP stay armed" in capsys.readouterr().out

    def test_close_all_flattens_and_cancels(self, capsys):
        t, broker, worker = self._opened(on_stop=ON_STOP_CLOSE_ALL)
        (trade,) = t.open_trades
        sl_id = trade.sl_order_id
        worker.shutdown(close_all=True)
        assert t.open_trades == ()
        closes = [o for o in broker.orders_of_type("market") if o.get("reduce_only")]
        assert len(closes) == 1 and closes[0]["quantity"] == pytest.approx(20.0)
        assert sl_id in broker.cancels
        assert [c.close_reason for c in t.closed_trades] == [CLOSE_REASON_FC]
        assert "close_all" in capsys.readouterr().out

    def test_close_all_noop_without_positions(self, capsys):
        t, broker = _s18_trader({})
        worker = _t_ready(t)
        worker.shutdown(close_all=True)
        assert broker.orders_of_type("market") == []
        assert "close_all" not in capsys.readouterr().out

    def test_close_all_failure_keeps_position_protected(self):
        t, broker, worker = self._opened(on_stop=ON_STOP_CLOSE_ALL)
        events = _t_collect(t)
        broker.fail_order_types.add("market")               # venue refuses the close
        worker.shutdown(close_all=True)
        assert len(t.open_trades) == 1                      # kept, SL still armed
        assert broker.cancels == []
        errors = [e for e in events if e.event_type == EVENT_ERROR
                  and e.where == "force_close"]
        assert errors

    def test_run_applies_close_all(self, monkeypatch):
        strategy_signals = {_T0: [("long", 110.0, 105.0, None)]}
        _t_patch_scheduler(monkeypatch, [_t_tick([_t_candle(0)])])
        t, _broker = _s18_trader({}, signals_at=strategy_signals,
                                 on_stop=ON_STOP_CLOSE_ALL)
        result: dict = {}
        thread = threading.Thread(target=lambda: result.update(r=t.run()), daemon=True)
        thread.start()
        assert _t_wait(lambda: len(t.open_trades) == 1)
        t.stop()
        thread.join(timeout=5.0)
        assert not thread.is_alive()
        assert t.open_trades == ()
        assert [c.close_reason for c in result["r"]] == [CLOSE_REASON_FC]


# ---------------------------------------------------------------------------
# — trader: persistence & restart reconciliation
# ---------------------------------------------------------------------------

import AlgoTradeKit.trader._state as state_module  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_default_state_path(tmp_path, monkeypatch):
    """Every test gets its own default journal path — run()-based tests from
    any section must never share (or litter) ``./.atk_trader_state.json``."""
    monkeypatch.setattr(
        trader_module, "DEFAULT_STATE_PATH", str(tmp_path / "default_state.json")
    )


class _S19FuturesBroker(_TFuturesBroker):
    """ futures mock + the venue surfaces: scripted open orders and
    trade-history fills."""

    def __init__(self, seed_candles, **kw):
        super().__init__(seed_candles, **kw)
        self.orders_payload: list = []
        self.fills_payload: list = []
        self.my_trades_calls: list = []
        self.fail_my_trades = False

    def open_orders(self, symbol=None):
        return list(self.orders_payload)

    def my_trades(self, symbol, *, start_ms=None, end_ms=None, from_id=None, limit=None):
        self.my_trades_calls.append((symbol, start_ms))
        if self.fail_my_trades:
            raise BrokerError("history down")
        return list(self.fills_payload)


class _S19SpotBroker(_TSpotBroker):
    def __init__(self, seed_candles, **kw):
        super().__init__(seed_candles, **kw)
        self.fills_payload: list = []

    def my_trades(self, symbol, *, start_ms=None, end_ms=None, from_id=None, limit=None):
        return list(self.fills_payload)


def _s19_session1(tmp_path, *, broker=None, cfg=None, sig=None, drive=None,
                  trader_id="s19a", pair_key="futures:BTCUSDT"):
    """Scripted first session on a bare engine; journals its state.
    Returns ``(journal_path, engine, trade, broker)``."""
    broker = broker if broker is not None else _XFuturesBroker(fill_price=110.0)
    broker.ticker_last = 110.0
    cfg = cfg if cfg is not None else _x_cfg(sl_mode="trailing", trailing_sl_percent=1.0)
    engine, broker, _events = _x_engine(broker, cfg, trader_id=trader_id)
    sig = sig if sig is not None else _x_sig(entry=110.0, sl=105.0, ts=_T0)
    trade = engine.open_position(sig)
    assert trade is not None
    if drive is not None:
        drive(engine, trade)
    path = str(tmp_path / "restart.json")
    journal = TraderStateJournal(path)
    journal.trader_id = trader_id
    journal.write_pair(
        pair_key,
        build_pair_state(engine, {(int(sig.timestamp), sig.direction)}, set()),
    )
    return path, engine, trade, broker


def _s19_restart(path, *, broker, cfg=None, strategy=None, trader_id="s19b"):
    """Second-session Trader on *broker*, engine armed, journal attached —
    reconciliation NOT yet run.  Returns ``(trader, worker, events)``."""
    t = _t_trader(
        broker=broker,
        cfg=cfg if cfg is not None else _t_cfg(sl_mode="trailing", trailing_sl_percent=1.0),
        strategy=strategy, trader_id=trader_id,
    )
    worker = _t_ready(t)
    worker.attach_journal(TraderStateJournal(path).load())
    events = _t_collect(t)
    return t, worker, events


def _s19_reconcile_events(events):
    return [e for e in events if e.event_type == EVENT_RECONCILE]


class TestStateSerialization:
    def test_trailing_trade_round_trip(self):
        def drive(engine, trade):
            for k, close in enumerate([112.0, 114.0], start=1):
                engine.update_stops(_t_candle(k, close=close))

        engine, broker, _ = _x_engine(_XFuturesBroker(fill_price=110.0),
                                      _x_cfg(sl_mode="trailing"))
        trade = engine.open_position(_x_sig(entry=110.0, sl=105.0, ts=_T0))
        drive(engine, trade)
        assert len(trade.pos.sl_history) == 3          # entry record + 2 trailing moves
        payload = json.loads(json.dumps(serialize_trade(trade)))
        restored = restore_trade(payload)
        assert serialize_trade(restored) == serialize_trade(trade)
        for slot in ("trade_id", "direction", "entry_price", "raw_entry_price",
                     "stop_loss", "initial_stop_loss", "take_profit", "margin_amount",
                     "risk_amount", "size", "original_size", "open_time",
                     "open_commission", "sl_distance", "pnl_per_price_unit",
                     "peak_price", "next_tp", "last_rr_hit", "signal_candle_index",
                     "tp_level_prices", "risk_free_triggered", "_max_favourable",
                     "_max_adverse", "sl_history"):
            assert getattr(restored.pos, slot) == getattr(trade.pos, slot), slot
        for name in ("client_order_id", "entry_order_id", "reference_price",
                     "fill_price", "slippage", "venue_tp", "sl_order_id",
                     "tp_order_id", "oco_order_list_id", "mt5_ticket",
                     "ladder_order_ids", "pending_sl_sync"):
            assert getattr(restored, name) == getattr(trade, name), name
        assert dataclasses.asdict(restored.signal) == dataclasses.asdict(trade.signal)

    def test_ladder_trade_round_trip_after_partial(self):
        cfg = _x_cfg(tp_mode=TP_MODE_MULTI_RR, tp_levels=[1, 2],
                     tp_level_close_fractions=[0.5, 0.5])
        engine, broker, _ = _x_engine(_XFuturesBroker(fill_price=110.0), cfg)
        trade = engine.open_position(_x_sig(entry=110.0, sl=105.0, ts=_T0))
        (l1_id,) = [oid for oid, idx in trade.ladder_order_ids.items() if idx == 0]
        engine.record_ladder_fill(trade, l1_id, 115.0, 10.0, close_time=_T0 + _XHOUR)
        trade.pending_sl_sync = True
        restored = restore_trade(json.loads(json.dumps(serialize_trade(trade))))
        assert restored.pos.size == trade.pos.size < restored.pos.original_size
        assert restored.pos.last_rr_hit == 1
        assert restored.pos.next_tp == trade.pos.next_tp
        assert restored.ladder_order_ids == trade.ladder_order_ids
        assert restored.pending_sl_sync is True

    def test_risk_free_state_round_trips_into_rf_reason(self):
        cfg = _x_cfg(risk_free_enabled=True, risk_free_at_rr=1.0)
        engine, broker, _ = _x_engine(_XFuturesBroker(fill_price=110.0), cfg)
        trade = engine.open_position(_x_sig(entry=110.0, sl=105.0, ts=_T0))
        engine.update_stops(_t_candle(1, close=115.2))   # high 115.7 ≥ entry + 1R
        assert trade.pos.risk_free_triggered is True
        restored = restore_trade(json.loads(json.dumps(serialize_trade(trade))))
        assert restored.pos.risk_free_triggered is True
        assert restored.pos.stop_loss == trade.pos.entry_price
        from AlgoTradeKit.simulate._position_math import sl_reason
        assert sl_reason(restored.pos) == CLOSE_REASON_RF

    def test_derived_entry_state_survives(self):
        engine, broker, _ = _x_engine(_XFuturesBroker(fill_price=110.0),
                                      _x_cfg(sl_mode="trailing"))
        trade = engine.open_position(_x_sig(entry=110.0, sl=105.0, ts=_T0))
        engine.update_stops(_t_candle(1, close=114.0))
        restored = restore_trade(json.loads(json.dumps(serialize_trade(trade))))
        assert restored.pos.initial_stop_loss == 105.0
        assert restored.pos.stop_loss != 105.0           # diverged live value kept
        assert restored.pos.original_size == trade.pos.original_size

    def test_non_json_metadata_is_stringified_not_fatal(self, tmp_path):
        engine, broker, _ = _x_engine(_XFuturesBroker(fill_price=110.0), _x_cfg())
        weird = object()
        trade = engine.open_position(
            Signal("long", 110.0, 105.0, None, _T0, 0, "1h", metadata={"obj": weird})
        )
        assert trade is not None
        journal = TraderStateJournal(str(tmp_path / "j.json"))
        journal.write_pair("futures:BTCUSDT", build_pair_state(engine, set(), set()))
        stored = TraderStateJournal(str(tmp_path / "j.json")).load()
        payload = stored.pair_state("futures:BTCUSDT")["trades"][0]
        assert isinstance(payload["pos"]["signal_metadata"]["obj"], str)
        restored = restore_trade(payload)                # loads fine, value is str
        assert isinstance(restored.signal.metadata["obj"], str)


class TestStateJournal:
    def test_write_pair_atomic_and_shaped(self, tmp_path):
        path = str(tmp_path / "j.json")
        engine, _b, _ = _x_engine(config=_x_cfg())
        journal = TraderStateJournal(path)
        journal.trader_id = "abc123"
        assert journal.write_pair(
            "futures:BTCUSDT", build_pair_state(engine, {(1, "long")}, {2})
        ) is True
        assert os.path.exists(path) and not os.path.exists(path + ".tmp")
        data = json.loads(Path(path).read_text())
        assert data["version"] == state_module.JOURNAL_VERSION
        assert data["trader_id"] == "abc123"
        pair = data["pairs"]["futures:BTCUSDT"]
        assert pair == {"trade_seq": 0, "acted": [[1, "long"]],
                        "exit_acted": [2], "trades": []}

    def test_unchanged_write_is_skipped(self, tmp_path, monkeypatch):
        path = str(tmp_path / "j.json")
        engine, _b, _ = _x_engine(config=_x_cfg())
        journal = TraderStateJournal(path)
        state = build_pair_state(engine, set(), set())
        replaces = []
        real_replace = os.replace
        monkeypatch.setattr(
            state_module.os, "replace",
            lambda src, dst: replaces.append(dst) or real_replace(src, dst),
        )
        assert journal.write_pair("k", dict(state)) is True
        assert journal.write_pair("k", dict(state)) is False
        assert len(replaces) == 1

    def test_load_missing_file_is_empty(self, tmp_path):
        journal = TraderStateJournal(str(tmp_path / "missing.json")).load()
        assert journal.trader_id is None
        assert journal.pair_state("futures:BTCUSDT") is None

    def test_corrupt_file_raises_naming_path(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ValueError, match="unreadable or corrupt"):
            TraderStateJournal(str(path)).load()

    def test_wrong_shape_raises(self, tmp_path):
        path = tmp_path / "list.json"
        path.write_text("[]", encoding="utf-8")
        with pytest.raises(ValueError, match="unreadable or corrupt"):
            TraderStateJournal(str(path)).load()

    def test_pair_state_round_trips_through_disk(self, tmp_path):
        path = str(tmp_path / "j.json")
        engine, _b, _ = _x_engine(config=_x_cfg(sl_mode="trailing"))
        engine.open_position(_x_sig(entry=110.0, sl=105.0, ts=_T0))
        state = build_pair_state(engine, {(_T0, "long")}, set())
        TraderStateJournal(path).write_pair("futures:BTCUSDT", state)
        loaded = TraderStateJournal(path).load().pair_state("futures:BTCUSDT")
        assert loaded == json.loads(json.dumps(state))


class TestJournalFlushWiring:
    def _armed(self, tmp_path, monkeypatch=None, **cfg_overrides):
        strategy = _TPlanStrategy(signals_at={_T0: [("long", 110.0, 105.0, None)]})
        broker = _TFuturesBroker(_x_candles(10), fill_price=110.0)
        broker.ticker_last = 110.0
        t = _t_trader(broker=broker, strategy=strategy,
                      cfg=_t_cfg(sl_mode="trailing", trailing_sl_percent=1.0,
                                 **cfg_overrides))
        worker = _t_ready(t)
        journal = TraderStateJournal(str(tmp_path / "j.json"))
        journal.trader_id = t.trader_id
        worker.attach_journal(journal)
        return t, worker, broker, journal

    def _stored(self, worker):
        return TraderStateJournal(worker.journal.path).load().pair_state(worker.pair_key)

    def test_open_is_journaled(self, tmp_path):
        t, worker, broker, journal = self._armed(tmp_path)
        worker._process_scheduler_tick(_t_tick([_t_candle(0)]))
        worker._flush_state()
        stored = self._stored(worker)
        assert stored["trade_seq"] == 1
        assert [_T0, "long"] in stored["acted"]
        (payload,) = stored["trades"]
        (trade,) = t.engine.open_trades
        assert payload["client_order_id"] == trade.client_order_id
        assert payload["pos"]["stop_loss"] == 105.0

    def test_sl_move_updates_journal(self, tmp_path):
        t, worker, broker, journal = self._armed(tmp_path)
        worker._process_scheduler_tick(_t_tick([_t_candle(0)]))
        worker._flush_state()
        before = self._stored(worker)["trades"][0]["pos"]
        worker._process_scheduler_tick(_t_tick([_t_candle(1, close=114.0)]))
        worker._flush_state()
        after = self._stored(worker)["trades"][0]["pos"]
        assert after["stop_loss"] > before["stop_loss"]
        assert len(after["sl_history"]) == len(before["sl_history"]) + 1

    def test_close_clears_trades_keeps_seq_and_dedup(self, tmp_path):
        t, worker, broker, journal = self._armed(tmp_path)
        worker._process_scheduler_tick(_t_tick([_t_candle(0)]))
        _s18_sl_fill(worker, _T0 + _XHOUR)
        worker._flush_state()
        stored = self._stored(worker)
        assert stored["trades"] == []
        assert stored["trade_seq"] == 1
        assert [_T0, "long"] in stored["acted"]

    def test_shutdown_flushes_without_manual_flush(self, tmp_path):
        t, worker, broker, journal = self._armed(tmp_path)
        worker._process_scheduler_tick(_t_tick([_t_candle(0)]))
        worker.shutdown()                                # keep — but journal flushed
        stored = self._stored(worker)
        assert len(stored["trades"]) == 1

    def test_write_failure_errors_once_then_recovers(self, tmp_path, monkeypatch):
        t, worker, broker, journal = self._armed(tmp_path)
        events = _t_collect(t)
        real_write = journal.write_pair
        fail = {"on": True}

        def flaky(key, state):
            if fail["on"]:
                raise OSError("disk full")
            return real_write(key, state)

        monkeypatch.setattr(journal, "write_pair", flaky)
        worker._flush_state()
        worker._flush_state()                            # second failure stays quiet
        errors = [e for e in events if e.event_type == EVENT_ERROR and e.where == "journal"]
        assert len(errors) == 1 and errors[0].will_retry
        fail["on"] = False
        worker._flush_state()                            # success resets the once-flag
        fail["on"] = True
        worker._flush_state()
        errors = [e for e in events if e.event_type == EVENT_ERROR and e.where == "journal"]
        assert len(errors) == 2


class TestReconcileAdopted:
    def _venue_holding(self, trade, *, orders=True):
        """(positions_payload, orders_payload) matching a journaled trade."""
        positions = [Position(symbol="BTCUSDT", side=trade.pos.direction,
                              quantity=trade.pos.size, entry_price=trade.pos.entry_price,
                              position_id="net")]
        order_rows = []
        if orders:
            if trade.sl_order_id:
                order_rows.append(Order(order_id=trade.sl_order_id, symbol="BTCUSDT",
                                        side="sell", type="stop", quantity=trade.pos.size))
            if trade.tp_order_id:
                order_rows.append(Order(order_id=trade.tp_order_id, symbol="BTCUSDT",
                                        side="sell", type="take_profit",
                                        quantity=trade.pos.size))
            for oid in trade.ladder_order_ids:
                order_rows.append(Order(order_id=oid, symbol="BTCUSDT", side="sell",
                                        type="limit", quantity=trade.pos.size))
        return positions, order_rows

    def test_adopt_resumes_trailing_state(self, tmp_path):
        def drive(engine, trade):
            for k, close in enumerate([112.0, 114.0], start=1):
                engine.update_stops(_t_candle(k, close=close))

        path, _eng1, tr1, _b1 = _s19_session1(tmp_path, drive=drive)
        b2 = _S19FuturesBroker(_x_candles(10), fill_price=110.0)
        b2.positions_payload, b2.orders_payload = self._venue_holding(tr1)
        t2, worker, events = _s19_restart(path, broker=b2)
        reconcile_startup(worker)
        (adopted,) = t2.engine.open_trades
        assert adopted.client_order_id == tr1.client_order_id
        assert adopted.pos.stop_loss == tr1.pos.stop_loss
        assert adopted.pos.sl_history == tr1.pos.sl_history
        assert adopted.pos.peak_price == tr1.pos.peak_price
        (rec,) = _s19_reconcile_events(events)
        assert rec.adopted == (tr1.client_order_id,)
        assert rec.closed_offline == () and rec.foreign == ()
        # trailing resumes from the journaled peak on the next candle
        before = adopted.pos.stop_loss
        t2.engine.update_stops(_t_candle(3, close=116.0))
        assert adopted.pos.stop_loss > before
        stops = b2.orders_of_type("stop")
        assert stops and stops[-1]["stop_price"] == adopted.pos.stop_loss
        assert tr1.sl_order_id in b2.cancels             # old venue stop replaced

    def test_adopt_restores_dedup_and_trade_seq(self, tmp_path):
        path, _eng1, tr1, _b1 = _s19_session1(tmp_path)
        b2 = _S19FuturesBroker(_x_candles(10), fill_price=110.0)
        b2.positions_payload, b2.orders_payload = self._venue_holding(tr1)
        t2, worker, events = _s19_restart(path, broker=b2)
        reconcile_startup(worker)
        assert (_T0, "long") in worker._acted
        assert t2.engine.trade_seq == 1
        entries_before = len(b2.orders_of_type("market"))
        worker._act_on_signals([_x_sig(entry=110.0, sl=105.0, ts=_T0)], _T0,
                               allow_entry=True)
        assert len(b2.orders_of_type("market")) == entries_before   # never re-sent
        assert [e for e in events if e.event_type == EVENT_SIGNAL] == []

    def test_adopt_rearms_vanished_sl(self, tmp_path):
        path, _eng1, tr1, _b1 = _s19_session1(tmp_path)
        b2 = _S19FuturesBroker(_x_candles(10), fill_price=110.0)
        b2.positions_payload, _ = self._venue_holding(tr1)
        b2.orders_payload = []                           # SL order vanished offline
        t2, worker, events = _s19_restart(path, broker=b2)
        reconcile_startup(worker)
        (adopted,) = t2.engine.open_trades
        (stop,) = b2.orders_of_type("stop")
        assert stop["stop_price"] == tr1.pos.stop_loss
        assert stop["reduce_only"] is True
        assert adopted.sl_order_id == stop["order_id"]
        assert adopted.pending_sl_sync is False
        assert b2.cancels == []                          # stale id never cancelled

    def test_adopt_sl_rearm_failure_arms_retry(self, tmp_path):
        path, _eng1, tr1, _b1 = _s19_session1(tmp_path)
        b2 = _S19FuturesBroker(_x_candles(10), fill_price=110.0)
        b2.positions_payload, _ = self._venue_holding(tr1)
        b2.orders_payload = []
        b2.fail_order_types.add("stop")
        t2, worker, events = _s19_restart(path, broker=b2)
        reconcile_startup(worker)
        (adopted,) = t2.engine.open_trades
        assert adopted.pending_sl_sync is True
        errors = [e for e in events if e.event_type == EVENT_ERROR
                  and e.where == "reconcile" and e.will_retry]
        assert errors

    def test_adopt_rearms_vanished_tp(self, tmp_path):
        cfg1 = _x_cfg(tp_mode=TP_MODE_SIGNAL)
        path, _eng1, tr1, _b1 = _s19_session1(
            tmp_path, cfg=cfg1, sig=_x_sig(entry=110.0, sl=105.0, tp=120.0, ts=_T0))
        assert tr1.tp_order_id
        b2 = _S19FuturesBroker(_x_candles(10), fill_price=110.0)
        positions, orders = self._venue_holding(tr1)
        b2.positions_payload = positions
        b2.orders_payload = [o for o in orders if o.type == "stop"]   # TP vanished
        t2, worker, events = _s19_restart(path, broker=b2,
                                          cfg=_t_cfg(tp_mode=TP_MODE_SIGNAL))
        reconcile_startup(worker)
        (adopted,) = t2.engine.open_trades
        (tp,) = b2.orders_of_type("take_profit")
        assert tp["stop_price"] == 120.0
        assert adopted.tp_order_id == tp["order_id"]

    def test_adopt_replaces_vanished_unfilled_ladder(self, tmp_path):
        cfg1 = _x_cfg(tp_mode=TP_MODE_MULTI_RR, tp_levels=[1, 2],
                      tp_level_close_fractions=[0.5, 0.5])
        path, _eng1, tr1, _b1 = _s19_session1(tmp_path, cfg=cfg1)
        assert len(tr1.ladder_order_ids) == 2
        b2 = _S19FuturesBroker(_x_candles(10), fill_price=110.0)
        positions, orders = self._venue_holding(tr1)
        b2.positions_payload = positions
        b2.orders_payload = [o for o in orders if o.type == "stop"]   # ladder gone
        t2, worker, events = _s19_restart(
            path, broker=b2,
            cfg=_t_cfg(tp_mode=TP_MODE_MULTI_RR, tp_levels=[1, 2],
                       tp_level_close_fractions=[0.5, 0.5]))
        reconcile_startup(worker)
        (adopted,) = t2.engine.open_trades
        limits = b2.orders_of_type("limit")
        assert [o["price"] for o in limits] == [115.0, 120.0]
        assert all(o["reduce_only"] for o in limits)
        assert sorted(adopted.ladder_order_ids.values()) == [0, 1]
        assert set(adopted.ladder_order_ids) == {o["order_id"] for o in limits}

    def test_adopt_settles_offline_ladder_fill(self, tmp_path):
        cfg1 = _x_cfg(tp_mode=TP_MODE_MULTI_RR, tp_levels=[1, 2],
                      tp_level_close_fractions=[0.5, 0.5])
        path, _eng1, tr1, _b1 = _s19_session1(tmp_path, cfg=cfg1)
        (l1_id,) = [oid for oid, idx in tr1.ladder_order_ids.items() if idx == 0]
        (l2_id,) = [oid for oid, idx in tr1.ladder_order_ids.items() if idx == 1]
        half = tr1.pos.size / 2
        b2 = _S19FuturesBroker(_x_candles(10), fill_price=110.0)
        b2.positions_payload = [Position(symbol="BTCUSDT", side="long", quantity=half,
                                         entry_price=110.0, position_id="net")]
        _, orders = self._venue_holding(tr1)
        b2.orders_payload = [o for o in orders if o.order_id in (tr1.sl_order_id, l2_id)]
        b2.fills_payload = [{"orderId": l1_id, "price": "115.0", "qty": str(half),
                             "time": _T0 + 2 * _XHOUR}]
        t2, worker, events = _s19_restart(
            path, broker=b2,
            cfg=_t_cfg(tp_mode=TP_MODE_MULTI_RR, tp_levels=[1, 2],
                       tp_level_close_fractions=[0.5, 0.5]))
        reconcile_startup(worker)
        (adopted,) = t2.engine.open_trades
        assert adopted.pos.size == pytest.approx(half)
        assert adopted.pos.last_rr_hit == 1
        assert l1_id not in adopted.ladder_order_ids
        (tp_event,) = [e for e in events if e.event_type == EVENT_TP_LEVEL]
        assert tp_event.trade.close_reason == CLOSE_REASON_TP_PARTIAL
        assert tp_event.trade.exit_price == 115.0
        assert tp_event.trade.close_time == _T0 + 2 * _XHOUR
        assert adopted.pos.stop_loss == adopted.pos.entry_price   # ladder moved to BE
        assert worker.closed_trades and worker.closed_trades[0] is tp_event.trade
        (rec,) = _s19_reconcile_events(events)
        assert rec.adopted == (tr1.client_order_id,)

    def test_adopt_records_offline_manual_reduction(self, tmp_path):
        path, _eng1, tr1, _b1 = _s19_session1(tmp_path)
        kept = tr1.pos.size * 0.6
        b2 = _S19FuturesBroker(_x_candles(10), fill_price=110.0)
        b2.ticker_last = 111.0
        b2.positions_payload = [Position(symbol="BTCUSDT", side="long", quantity=kept,
                                         entry_price=110.0, position_id="net")]
        _, b2.orders_payload = self._venue_holding(tr1)
        t2, worker, events = _s19_restart(path, broker=b2)
        reconcile_startup(worker)
        (adopted,) = t2.engine.open_trades
        assert adopted.pos.size == pytest.approx(kept)
        (close,) = [e for e in events if e.event_type == EVENT_CLOSE]
        assert close.reason == CLOSE_REASON_MANUAL
        assert close.trade.size == pytest.approx(tr1.pos.size - kept)
        assert close.exit_price == 111.0                 # ticker fallback price
        (rec,) = _s19_reconcile_events(events)
        assert rec.adopted == (tr1.client_order_id,) and rec.closed_offline == ()

    def test_trader_id_reused_from_journal(self, tmp_path):
        path = str(tmp_path / "restart.json")
        Path(path).write_text(json.dumps(
            {"version": 1, "trader_id": "zz19", "pairs": {}}), encoding="utf-8")
        broker = _TFuturesBroker(_x_candles(10), fill_price=110.0)
        t = Trader(broker=broker, strategy=_TPlanStrategy(), config=_t_cfg(),
                   state_path=path)                      # no explicit trader_id
        t._prepare_journal()
        assert t.trader_id == "zz19"
        broker2 = _TFuturesBroker(_x_candles(10), fill_price=110.0)
        t2 = Trader(broker=broker2, strategy=_TPlanStrategy(), config=_t_cfg(),
                    state_path=path, trader_id="mine")   # explicit id always wins
        journal = t2._prepare_journal()
        assert t2.trader_id == "mine" and journal.trader_id == "mine"

    def test_default_state_path_used_when_unset(self, tmp_path):
        broker = _TFuturesBroker(_x_candles(10), fill_price=110.0)
        t = Trader(broker=broker, strategy=_TPlanStrategy(), config=_t_cfg())
        journal = t._prepare_journal()
        assert journal.path == trader_module.DEFAULT_STATE_PATH
        assert str(tmp_path) in journal.path             # the autouse isolation

    def test_adopt_mt5_partial_and_protection_rearm(self, tmp_path):
        mt5_1 = _XMT5Broker(fill_price=100.0)
        path, _eng1, tr1, _b1 = _s19_session1(
            tmp_path, broker=mt5_1, cfg=_x_cfg(tp_mode=TP_MODE_SIGNAL),
            sig=_x_sig(entry=100.0, sl=95.0, tp=110.0), pair_key="forex:BTCUSDT")
        assert tr1.mt5_ticket == "7777"
        kept = tr1.pos.size * 0.5
        b2 = _TMT5Broker(_x_candles(10), fill_price=100.0)
        b2.ticker_last = 101.0
        b2.venue_positions = [Position(symbol="BTCUSDT", side="long", quantity=kept,
                                       entry_price=100.0, position_id="7777",
                                       stop_loss=None, take_profit=None,
                                       raw={"comment": tr1.client_order_id})]
        b2.deals = [{"entry": 1, "price": 102.5, "time_msc": _X0 + 3 * _XHOUR,
                     "reason": 0, "volume": kept}]
        t2, worker, events = _s19_restart(path, broker=b2,
                                          cfg=_t_cfg(tp_mode=TP_MODE_SIGNAL))
        reconcile_startup(worker)
        (adopted,) = t2.engine.open_trades
        assert adopted.pos.size == pytest.approx(kept)
        (close,) = [e for e in events if e.event_type == EVENT_CLOSE]
        assert close.reason == CLOSE_REASON_MANUAL and close.exit_price == 102.5
        # SL and TP were zeroed on the venue — one modify re-arms both
        assert b2.modifies[-1] == (7777, tr1.pos.stop_loss, 110.0)
        (rec,) = _s19_reconcile_events(events)
        assert rec.adopted == (tr1.client_order_id,)


class TestReconcileClosedOffline:
    def _gone(self, tr1, *, keep_orders=()):
        b2 = _S19FuturesBroker(_x_candles(10), fill_price=110.0)
        b2.ticker_last = 110.0
        b2.positions_payload = []
        b2.orders_payload = [Order(order_id=oid, symbol="BTCUSDT", side="sell",
                                   type="stop", quantity=tr1.pos.size)
                             for oid in keep_orders]
        return b2

    def test_futures_sl_fill_weighted_from_history(self, tmp_path):
        cfg1 = _x_cfg(tp_mode=TP_MODE_SIGNAL)
        path, _eng1, tr1, _b1 = _s19_session1(
            tmp_path, cfg=cfg1, sig=_x_sig(entry=110.0, sl=105.0, tp=120.0, ts=_T0))
        b2 = self._gone(tr1)
        b2.fills_payload = [
            {"orderId": tr1.sl_order_id, "price": "105.2", "qty": "12.0",
             "time": _T0 + 2 * _XHOUR},
            {"orderId": tr1.sl_order_id, "price": "104.8", "qty": "8.0",
             "time": _T0 + 2 * _XHOUR + 5},
        ]
        t2, worker, events = _s19_restart(path, broker=b2,
                                          cfg=_t_cfg(tp_mode=TP_MODE_SIGNAL))
        reconcile_startup(worker)
        assert t2.engine.open_trades == ()
        (close,) = [e for e in events if e.event_type == EVENT_CLOSE]
        assert close.reason == CLOSE_REASON_SL
        assert close.exit_price == pytest.approx((105.2 * 12 + 104.8 * 8) / 20)
        assert close.trade.close_time == _T0 + 2 * _XHOUR + 5
        assert tr1.tp_order_id in b2.cancels             # leftover TP cleaned up
        (rec,) = _s19_reconcile_events(events)
        assert rec.closed_offline == (tr1.client_order_id,) and rec.adopted == ()
        assert t2.engine.trade_seq == 1                  # sequence still restored

    def test_futures_sl_fill_after_risk_free_maps_rf(self, tmp_path):
        cfg1 = _x_cfg(risk_free_enabled=True, risk_free_at_rr=1.0)

        def drive(engine, trade):
            engine.update_stops(_t_candle(1, close=115.2))   # trips break-even
            assert trade.pos.risk_free_triggered

        path, _eng1, tr1, _b1 = _s19_session1(tmp_path, cfg=cfg1, drive=drive)
        b2 = self._gone(tr1)
        b2.fills_payload = [{"orderId": tr1.sl_order_id, "price": "110.0",
                             "qty": str(tr1.pos.size), "time": _T0 + 3 * _XHOUR}]
        t2, worker, events = _s19_restart(
            path, broker=b2, cfg=_t_cfg(risk_free_enabled=True, risk_free_at_rr=1.0))
        reconcile_startup(worker)
        (close,) = [e for e in events if e.event_type == EVENT_CLOSE]
        assert close.reason == CLOSE_REASON_RF

    def test_futures_tp_fill_cancels_leftover_sl(self, tmp_path):
        cfg1 = _x_cfg(tp_mode=TP_MODE_SIGNAL)
        path, _eng1, tr1, _b1 = _s19_session1(
            tmp_path, cfg=cfg1, sig=_x_sig(entry=110.0, sl=105.0, tp=120.0, ts=_T0))
        b2 = self._gone(tr1)
        b2.fills_payload = [{"orderId": tr1.tp_order_id, "price": "120.0",
                             "qty": str(tr1.pos.size), "time": _T0 + 4 * _XHOUR}]
        t2, worker, events = _s19_restart(path, broker=b2,
                                          cfg=_t_cfg(tp_mode=TP_MODE_SIGNAL))
        reconcile_startup(worker)
        (close,) = [e for e in events if e.event_type == EVENT_CLOSE]
        assert close.reason == CLOSE_REASON_TP and close.exit_price == 120.0
        assert tr1.sl_order_id in b2.cancels

    def test_futures_offline_ladder_run_to_full_close(self, tmp_path):
        cfg1 = _x_cfg(tp_mode=TP_MODE_MULTI_RR, tp_levels=[1, 2],
                      tp_level_close_fractions=[0.5, 0.5])
        path, _eng1, tr1, _b1 = _s19_session1(tmp_path, cfg=cfg1)
        (l1_id,) = [oid for oid, idx in tr1.ladder_order_ids.items() if idx == 0]
        (l2_id,) = [oid for oid, idx in tr1.ladder_order_ids.items() if idx == 1]
        half = tr1.pos.size / 2
        b2 = self._gone(tr1)
        b2.fills_payload = [
            {"orderId": l1_id, "price": "115.0", "qty": str(half), "time": _T0 + _XHOUR},
            {"orderId": l2_id, "price": "120.0", "qty": str(half),
             "time": _T0 + 2 * _XHOUR},
        ]
        t2, worker, events = _s19_restart(
            path, broker=b2,
            cfg=_t_cfg(tp_mode=TP_MODE_MULTI_RR, tp_levels=[1, 2],
                       tp_level_close_fractions=[0.5, 0.5]))
        reconcile_startup(worker)
        assert t2.engine.open_trades == ()
        closes = [e.trade for e in events
                  if e.event_type in (EVENT_TP_LEVEL, EVENT_CLOSE) and e.trade]
        assert [c.close_reason for c in closes] == [CLOSE_REASON_TP_PARTIAL,
                                                    CLOSE_REASON_TP]
        assert [c.exit_price for c in closes] == [115.0, 120.0]
        assert closes[0].trade_id == closes[1].trade_id
        (rec,) = _s19_reconcile_events(events)
        assert rec.closed_offline == (tr1.client_order_id,)

    def test_futures_untraceable_close_falls_back_to_ticker(self, tmp_path):
        path, _eng1, tr1, _b1 = _s19_session1(tmp_path)
        b2 = self._gone(tr1)
        b2.ticker_last = 108.5
        t2, worker, events = _s19_restart(path, broker=b2)
        reconcile_startup(worker)
        (close,) = [e for e in events if e.event_type == EVENT_CLOSE]
        assert close.reason == CLOSE_REASON_MANUAL and close.exit_price == 108.5

    def test_history_read_failure_degrades_with_error(self, tmp_path):
        path, _eng1, tr1, _b1 = _s19_session1(tmp_path)
        b2 = self._gone(tr1)
        b2.ticker_last = 109.0
        b2.fail_my_trades = True
        t2, worker, events = _s19_restart(path, broker=b2)
        reconcile_startup(worker)
        errors = [e for e in events if e.event_type == EVENT_ERROR
                  and e.where == "reconcile"]
        assert errors
        (close,) = [e for e in events if e.event_type == EVENT_CLOSE]
        assert close.reason == CLOSE_REASON_MANUAL and close.exit_price == 109.0

    def test_mt5_offline_deals_settle_as_slices(self, tmp_path):
        mt5_1 = _XMT5Broker(fill_price=100.0)
        path, _eng1, tr1, _b1 = _s19_session1(
            tmp_path, broker=mt5_1, sig=_x_sig(entry=100.0, sl=95.0),
            cfg=_x_cfg(), pair_key="forex:BTCUSDT")
        half = tr1.pos.size / 2
        b2 = _TMT5Broker(_x_candles(10), fill_price=100.0)
        b2.venue_positions = []                          # ticket vanished offline
        b2.deals = [
            {"entry": 1, "price": 97.0, "time_msc": _X0 + 2 * _XHOUR, "reason": 0,
             "volume": half},
            {"entry": 1, "price": 95.0, "time_msc": _X0 + 3 * _XHOUR, "reason": 4,
             "volume": half},
        ]
        t2, worker, events = _s19_restart(path, broker=b2, cfg=_t_cfg())
        reconcile_startup(worker)
        assert t2.engine.open_trades == ()
        closes = [e for e in events if e.event_type == EVENT_CLOSE]
        assert [(c.reason, c.exit_price) for c in closes] == [
            (CLOSE_REASON_MANUAL, 97.0), (CLOSE_REASON_SL, 95.0)]
        assert closes[0].trade.trade_id == closes[1].trade.trade_id
        assert closes[0].trade.close_time == _X0 + 2 * _XHOUR
        assert b2.deals_calls == [int(tr1.mt5_ticket)]
        (rec,) = _s19_reconcile_events(events)
        assert rec.closed_offline == (tr1.client_order_id,)

    def test_spot_vanished_protection_reason_by_nearest_price(self, tmp_path):
        spot1 = _XSpotBroker(fill_price=110.0)
        path, _eng1, tr1, _b1 = _s19_session1(
            tmp_path, broker=spot1, cfg=_x_cfg(tp_mode=TP_MODE_SIGNAL),
            sig=_x_sig(entry=110.0, sl=105.0, tp=120.0), pair_key="spot:BTCUSDT")
        assert tr1.oco_order_list_id
        b2 = _S19SpotBroker(_x_candles(10), fill_price=110.0)
        b2.orders_payload = []                           # whole OCO gone
        b2.fills_payload = [{"orderId": "777", "orderListId": tr1.oco_order_list_id,
                             "price": "119.9", "qty": str(tr1.pos.size),
                             "time": _X0 + 2 * _XHOUR}]
        t2, worker, events = _s19_restart(path, broker=b2,
                                          cfg=_t_cfg(tp_mode=TP_MODE_SIGNAL))
        reconcile_startup(worker)
        (close,) = [e for e in events if e.event_type == EVENT_CLOSE]
        assert close.reason == CLOSE_REASON_TP           # 119.9 sits at the TP leg
        assert close.exit_price == 119.9
        (rec,) = _s19_reconcile_events(events)
        assert rec.closed_offline == (tr1.client_order_id,)

    def test_spot_fill_near_stop_maps_sl(self, tmp_path):
        spot1 = _XSpotBroker(fill_price=110.0)
        path, _eng1, tr1, _b1 = _s19_session1(
            tmp_path, broker=spot1, cfg=_x_cfg(tp_mode=TP_MODE_SIGNAL),
            sig=_x_sig(entry=110.0, sl=105.0, tp=120.0), pair_key="spot:BTCUSDT")
        b2 = _S19SpotBroker(_x_candles(10), fill_price=110.0)
        b2.orders_payload = []
        b2.fills_payload = [{"orderId": "778", "orderListId": tr1.oco_order_list_id,
                             "price": "104.95", "qty": str(tr1.pos.size),
                             "time": _X0 + 2 * _XHOUR}]
        t2, worker, events = _s19_restart(path, broker=b2,
                                          cfg=_t_cfg(tp_mode=TP_MODE_SIGNAL))
        reconcile_startup(worker)
        (close,) = [e for e in events if e.event_type == EVENT_CLOSE]
        assert close.reason == CLOSE_REASON_SL and close.exit_price == 104.95


class TestReconcileForeignAndLifecycle:
    def test_futures_foreign_net_warned_and_untouched(self, tmp_path, capsys):
        b2 = _S19FuturesBroker(_x_candles(10), fill_price=110.0)
        b2.positions_payload = [Position(symbol="BTCUSDT", side="long", quantity=0.5,
                                         entry_price=100.0, position_id="net")]
        t2, worker, events = _s19_restart(str(tmp_path / "none.json"), broker=b2)
        orders_before = len(b2.orders)
        reconcile_startup(worker)
        assert t2.engine.open_trades == ()
        assert len(b2.orders) == orders_before           # nothing placed or closed
        (rec,) = _s19_reconcile_events(events)
        assert rec.adopted == () and rec.closed_offline == ()
        assert rec.foreign == ("BTCUSDT long 0.5",)
        assert "left untouched" in capsys.readouterr().out

    def test_mt5_foreign_ticket_label(self, tmp_path):
        b2 = _TMT5Broker(_x_candles(10), fill_price=100.0)
        b2.venue_positions = [Position(symbol="BTCUSDT", side="long", quantity=0.2,
                                       entry_price=100.0, position_id="9999",
                                       raw={"comment": "somebody-else"})]
        t2, worker, events = _s19_restart(str(tmp_path / "none.json"), broker=b2,
                                          cfg=_t_cfg())
        reconcile_startup(worker)
        (rec,) = _s19_reconcile_events(events)
        assert rec.foreign == ("ticket 9999 [somebody-else]",)

    def test_clean_first_start_emits_no_reconcile_event(self, tmp_path):
        b2 = _S19FuturesBroker(_x_candles(10), fill_price=110.0)
        b2.positions_payload = []
        t2, worker, events = _s19_restart(str(tmp_path / "none.json"), broker=b2)
        reconcile_startup(worker)
        assert _s19_reconcile_events(events) == []

    def test_venue_read_failure_aborts_start(self, tmp_path):
        path, _eng1, tr1, _b1 = _s19_session1(tmp_path)
        b2 = _S19FuturesBroker(_x_candles(10), fill_price=110.0)
        t2, worker, events = _s19_restart(path, broker=b2)

        def boom(symbol=None):
            raise ConnectionFailed("venue down")

        b2.open_positions = boom
        with pytest.raises(BrokerError):
            reconcile_startup(worker)
        assert t2.engine.open_trades == ()               # nothing half-adopted acts

    def test_corrupt_journal_refused_before_anything_starts(self, tmp_path):
        path = tmp_path / "corrupt.json"
        path.write_text("{broken", encoding="utf-8")
        broker = _TFuturesBroker(_x_candles(10), fill_price=110.0)
        t = Trader(broker=broker, strategy=_TPlanStrategy(), config=_t_cfg(),
                   state_path=str(path))
        with pytest.raises(ValueError, match="unreadable or corrupt"):
            t.run()
        assert t._ran is False                           # instance stays runnable
        assert broker.fetch_calls == []                  # nothing seeded or started

    def test_restart_e2e_adopt_close_and_never_double_open(self, tmp_path, monkeypatch):
        path = str(tmp_path / "e2e.json")
        signals = {_T0: [("long", 110.0, 105.0, None)]}

        # --- session 1: open a position, stop with on_stop="keep" ---
        _t_patch_scheduler(monkeypatch, [_t_tick([_t_candle(0)])])
        b1 = _TFuturesBroker(_x_candles(10), fill_price=110.0)
        b1.ticker_last = 110.0
        t1 = _t_trader(broker=b1, strategy=_TPlanStrategy(signals_at=signals),
                       cfg=_t_cfg(), state_path=path, trader_id="e2e19")
        result1: dict = {}
        thread1 = threading.Thread(target=lambda: result1.update(r=t1.run()), daemon=True)
        thread1.start()
        assert _t_wait(lambda: len(t1.open_trades) == 1)

        def journaled_trades():
            try:
                return json.loads(Path(path).read_text())["pairs"][
                    "futures:BTCUSDT"]["trades"]
            except (OSError, KeyError, ValueError):
                return []

        assert _t_wait(lambda: len(journaled_trades()) == 1)   # run_loop flushed it
        t1.stop()
        thread1.join(timeout=5.0)
        assert not thread1.is_alive() and result1["r"] == []
        (tr1,) = t1.open_trades                          # kept, venue SL armed

        # --- session 2: same journal; venue still holds the position ---
        b2 = _S19FuturesBroker(_x_candles(10), fill_price=110.0)
        b2.ticker_last = 110.0
        b2.positions_payload = [Position(symbol="BTCUSDT", side="long",
                                         quantity=tr1.pos.size, entry_price=110.0,
                                         position_id="net")]
        b2.orders_payload = [Order(order_id=tr1.sl_order_id, symbol="BTCUSDT",
                                   side="sell", type="stop", quantity=tr1.pos.size)]
        t2 = Trader(broker=b2, strategy=_TPlanStrategy(signals_at=signals),
                    config=_t_cfg(), state_path=path)    # no trader_id → journal's
        b2.mirror_engine = t2.engine
        events2 = _t_collect(t2)
        result2: dict = {}
        thread2 = threading.Thread(target=lambda: result2.update(r=t2.run()), daemon=True)
        thread2.start()
        assert _t_wait(lambda: len(t2.open_trades) == 1)
        (adopted,) = t2.open_trades
        assert adopted.client_order_id == tr1.client_order_id
        assert t2.trader_id == "e2e19"                   # reused from the journal
        (rec,) = _s19_reconcile_events(events2)
        assert rec.adopted == (tr1.client_order_id,)
        # the scripted scheduler replays candle 0 → the same signal fires again,
        # but the journaled dedup key blocks it: no new entry, no OPEN event
        assert _t_wait(lambda: b2.user_cb is not None)
        assert b2.orders_of_type("market") == []
        assert [e for e in events2 if e.event_type == EVENT_OPEN] == []
        # the venue SL fills → close detected, journal drained
        b2.user_cb({"e": "ORDER_TRADE_UPDATE", "E": _T0 + 2 * _XHOUR,
                    "o": {"X": "FILLED", "i": adopted.sl_order_id, "ap": "105.0",
                          "z": str(adopted.pos.size)}})
        assert _t_wait(lambda: t2.open_trades == ())
        assert _t_wait(lambda: journaled_trades() == [])
        t2.stop()
        thread2.join(timeout=5.0)
        assert not thread2.is_alive()
        assert [c.close_reason for c in result2["r"]] == [CLOSE_REASON_SL]
        assert result2["r"][0].exit_price == 105.0


# ===========================================================================
# — multi-pair orchestration + combined report
# ===========================================================================
#
# Futures numbers as in: signal long 110 / SL 105 → sl_distance 5;
# risk 1 % of the shared 10 000 account = $100 → size 20 units per pair; an
# SL fill at 105 realizes −$100 net (zero costs).

import AlgoTradeKit.trader._run_live as run_live_module  # noqa: E402
from AlgoTradeKit.trader._run_live import _combined_labels  # noqa: E402


class _M20SharedFuturesBroker(_TFuturesBroker):
    """One futures account traded by several pairs: symbol-aware engine
    mirror (the mock mirrors a single engine) and one user-data callback
    per subscribing worker."""

    def __init__(self, seed_candles, **kw):
        super().__init__(seed_candles, **kw)
        self.engines_by_symbol: dict = {}
        self.user_cbs: list = []

    def open_positions(self, symbol=None):
        engine = self.engines_by_symbol.get(symbol)
        if engine is None:
            return []
        return [
            Position(symbol=symbol, side=t.pos.direction, quantity=t.pos.size,
                     entry_price=t.pos.entry_price, position_id="net")
            for t in engine.open_trades
        ]

    def stream_user_data(self, on_event):
        handle = super().stream_user_data(on_event)
        self.user_cbs.append(on_event)
        return handle


def _m20_trader(*, broker=None, symbols=("BTCUSDT", "ETHUSDT"),
                signals=None, cfgs=None, **trader_kw):
    """(trader, shared broker) — one futures account, one pair per symbol."""
    broker = broker if broker is not None else _M20SharedFuturesBroker(
        _x_candles(10), fill_price=110.0)
    broker.ticker_last = 110.0
    signals = signals or [None] * len(symbols)
    pairs = []
    for i, symbol in enumerate(symbols):
        cfg = cfgs[i] if cfgs and cfgs[i] is not None else _t_cfg(symbol=symbol)
        pairs.append(TraderPair(broker=broker, config=cfg,
                                strategy=_TPlanStrategy(signals_at=signals[i] or {})))
    trader_kw.setdefault("trader_id", "t20")
    t = Trader(pairs=pairs, **trader_kw)
    if hasattr(broker, "engines_by_symbol"):
        for worker in t._workers:
            broker.engines_by_symbol[worker.config.symbol] = worker.engine
    return t, broker


def _m20_ready(t: Trader):
    """Seed + arm every pair WITHOUT starting any thread/stream — tests then
    drive the workers' handlers directly (deterministic, single-threaded)."""
    for worker in t._workers:
        worker.seed()
        worker.engine.start()
    return t._workers


def _m20_sl_fill(worker, time_ms):
    """Fill the worker's single open trade's venue SL at 105 (−$100 net)."""
    (trade,) = worker.engine.open_trades
    worker._process_user_data({
        "e": "ORDER_TRADE_UPDATE", "E": time_ms,
        "o": {"X": "FILLED", "i": trade.sl_order_id, "ap": "105.0",
              "z": str(trade.pos.size)},
    })


_M20_SIG = [("long", 110.0, 105.0, None)]


class TestTraderMultiPairApi:
    def test_pairs_xor_single_form(self):
        broker = _TFuturesBroker(_x_candles(10))
        pair = TraderPair(broker=broker, config=_t_cfg(), strategy=_TPlanStrategy())
        with pytest.raises(ValueError, match="not both"):
            Trader(broker=broker, pairs=[pair])
        with pytest.raises(ValueError, match="all required"):
            Trader()

    def test_duplicate_broker_symbol_rejected(self):
        broker = _TFuturesBroker(_x_candles(10))
        pairs = [
            TraderPair(broker=broker, config=_t_cfg(), strategy=_TPlanStrategy()),
            TraderPair(broker=broker, config=_t_cfg(), strategy=_TPlanStrategy()),
        ]
        with pytest.raises(ValueError, match="duplicate"):
            Trader(pairs=pairs)

    def test_display_pairs_accepted(self):
        #: displaying pairs construct their bridges — the guard is gone.
        broker = _TFuturesBroker(_x_candles(10))
        pairs = [
            TraderPair(broker=broker, config=_t_cfg(), strategy=_TPlanStrategy()),
            TraderPair(broker=_TFuturesBroker(_x_candles(10)),
                       config=_t_cfg(symbol="ETHUSDT", display=True,
                                     display_candles=10),
                       strategy=_TPlanStrategy()),
        ]
        t = Trader(pairs=pairs)
        assert t._workers[0].display is None
        assert t._workers[1].display is not None
        assert t._workers[1].display.mode == "sim"

    def test_engines_tuple_and_engine_guard(self):
        t, _broker = _m20_trader()
        assert [e.config.symbol for e in t.engines] == ["BTCUSDT", "ETHUSDT"]
        with pytest.raises(RuntimeError, match="engines"):
            t.engine
        # Single-pair form: both spellings keep working.
        single = _t_trader()
        assert single.engine is single.engines[0]

    def test_shared_trader_id(self):
        t, _broker = _m20_trader(trader_id="idxyz")
        assert t.trader_id == "idxyz"
        assert {e.trader_id for e in t.engines} == {"idxyz"}
        auto, _b = _m20_trader(trader_id=None)
        ids = {e.trader_id for e in auto.engines}
        assert len(ids) == 1 and len(ids.pop()) == 6   # one shared random hex id

    def test_aggregate_event_stream(self):
        t, _broker = _m20_trader()
        assert t.events is not t._workers[0].events
        got: list = []
        t.events.subscribe(got.append)
        a = _signal_event(symbol="BTCUSDT")
        b = _signal_event(symbol="ETHUSDT")
        t._workers[0].events.emit(a)
        t._workers[1].events.emit(b)
        assert got == [a, b]

    def test_per_pair_streams_stay_isolated(self):
        t, _broker = _m20_trader()
        only_a: list = []
        t._workers[0].events.subscribe(only_a.append)
        t._workers[1].events.emit(_signal_event(symbol="ETHUSDT"))
        assert only_a == []

    def test_pair_key_suffix_for_same_market_symbol(self):
        # Two accounts (two broker objects), same market+symbol — legal per
        #; the journal keys disambiguate by pair order.
        pairs = [
            TraderPair(broker=_TFuturesBroker(_x_candles(10)), config=_t_cfg(),
                       strategy=_TPlanStrategy()),
            TraderPair(broker=_TFuturesBroker(_x_candles(10)), config=_t_cfg(),
                       strategy=_TPlanStrategy()),
            TraderPair(broker=_TFuturesBroker(_x_candles(10)),
                       config=_t_cfg(symbol="ETHUSDT"), strategy=_TPlanStrategy()),
        ]
        t = Trader(pairs=pairs)
        assert [w.pair_key for w in t._workers] == [
            "futures:BTCUSDT", "futures:BTCUSDT#2", "futures:ETHUSDT"]

    def test_run_only_once_in_pairs_form(self, monkeypatch):
        _t_patch_scheduler(monkeypatch, [])
        t, _broker = _m20_trader()
        threading.Timer(0.2, t.stop).start()
        t.run()
        with pytest.raises(RuntimeError, match="only run once"):
            t.run()


class TestTraderMultiPairLoop:
    def test_shared_broker_wallet_and_entries(self):
        #: multi-pair with shared broker — both pairs size off the SAME
        # account balance (10 000 → $100 risk → 20 units each) and trade
        # through the one broker.
        t, broker = _m20_trader(signals=[{_T0: _M20_SIG}, {_T0: _M20_SIG}])
        workers = _m20_ready(t)
        for worker in workers:
            worker._process_scheduler_tick(_t_tick([_t_candle(0)]))
        markets = broker.orders_of_type("market")
        assert [o["symbol"] for o in markets] == ["BTCUSDT", "ETHUSDT"]
        assert {e.start_balance for e in t.engines} == {10_000.0}
        assert [t_.pos.size for t_ in t.open_trades] == [20.0, 20.0]
        assert sorted(s for s, _l in broker.leverage_calls) == ["BTCUSDT", "ETHUSDT"]
        for trade in t.open_trades:
            assert trade.client_order_id.startswith("atk-t20-")
        assert t.closed_trades == ()

    def test_daily_loss_gate_is_per_pair(self):
        # Pair A trips its $50 daily-loss limit (−$100 realized) → its next
        # entry is skipped; pair B (no limit) keeps trading the same candles.
        signals = [
            {_T0: _M20_SIG, _T0 + _XHOUR: _M20_SIG},
            {_T0 + _XHOUR: _M20_SIG},
        ]
        cfgs = [_t_cfg(max_daily_loss=50.0), _t_cfg(symbol="ETHUSDT")]
        t, broker = _m20_trader(signals=signals, cfgs=cfgs)
        worker_a, worker_b = _m20_ready(t)
        events_a: list = []
        worker_a.events.subscribe(events_a.append)
        worker_a._process_scheduler_tick(_t_tick([_t_candle(0)]))
        _m20_sl_fill(worker_a, _T0 + 1000)
        for worker in (worker_a, worker_b):
            worker._process_scheduler_tick(_t_tick([_t_candle(1)]))
        assert [e.event_type for e in events_a].count(EVENT_DAILY_LOSS) == 1
        assert worker_a.engine.open_trades == ()          # gated — no re-entry
        assert len(worker_b.engine.open_trades) == 1      # pair B unaffected
        a_markets = [o for o in broker.orders_of_type("market")
                     if o["symbol"] == "BTCUSDT"]
        assert len(a_markets) == 1

    def test_one_journal_file_two_pair_keys(self, tmp_path):
        path = str(tmp_path / "multi.json")
        t, _broker = _m20_trader(signals=[{_T0: _M20_SIG}, None], state_path=path)
        t._prepare_journal()
        workers = _m20_ready(t)
        for worker in workers:
            worker._process_scheduler_tick(_t_tick([_t_candle(0)]))
            worker._flush_state()
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        assert data["trader_id"] == "t20"
        assert set(data["pairs"]) == {"futures:BTCUSDT", "futures:ETHUSDT"}
        assert len(data["pairs"]["futures:BTCUSDT"]["trades"]) == 1
        assert data["pairs"]["futures:ETHUSDT"]["trades"] == []

    def test_journal_write_pair_thread_safe(self, tmp_path):
        journal = TraderStateJournal(str(tmp_path / "hammer.json"))
        errors: list = []

        def hammer(key):
            try:
                for i in range(200):
                    journal.write_pair(key, {"trade_seq": i, "acted": [],
                                             "exit_acted": [], "trades": []})
            except Exception as exc:  # pragma: no cover — the assertion target
                errors.append(exc)

        threads = [threading.Thread(target=hammer, args=(k,))
                   for k in ("futures:BTCUSDT", "futures:ETHUSDT")]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=10.0)
        assert errors == []
        data = json.loads(Path(journal.path).read_text(encoding="utf-8"))
        assert set(data["pairs"]) == {"futures:BTCUSDT", "futures:ETHUSDT"}
        assert all(p["trade_seq"] == 199 for p in data["pairs"].values())


class TestTraderMultiPairLifecycle:
    def test_run_returns_flat_sorted_and_stops_all(self, monkeypatch):
        _t_patch_scheduler(monkeypatch, [_t_tick([_t_candle(0)])])
        t, broker = _m20_trader(signals=[{_T0: _M20_SIG}, {_T0: _M20_SIG}])
        result: dict = {}
        thread = threading.Thread(target=lambda: result.update(r=t.run()), daemon=True)
        thread.start()
        assert _t_wait(lambda: len(t.open_trades) == 2)
        assert len(broker.user_cbs) == 2                  # one user stream per pair
        # ETHUSDT's SL fills FIRST, BTCUSDT's later → the flat result is
        # sorted by close_time, not by pair order.
        _m20_sl_fill(t._workers[1], _T0 + 1_000)
        _m20_sl_fill(t._workers[0], _T0 + 2_000)
        assert _t_wait(lambda: len(t.closed_trades) == 2)
        t.stop()
        thread.join(timeout=5.0)
        assert not thread.is_alive()
        trades = result["r"]
        assert [tr.symbol for tr in trades] == ["ETHUSDT", "BTCUSDT"]
        assert [tr.close_time for tr in trades] == [_T0 + 1_000, _T0 + 2_000]
        assert all(tr.close_reason == CLOSE_REASON_SL for tr in trades)
        assert all(w.stop_event.is_set() for w in t._workers)
        for stream in broker.streams:
            assert not stream.alive                       # every feed stopped

    def test_kill_switch_stops_every_pair(self, monkeypatch, tmp_path, capsys):
        _t_patch_scheduler(monkeypatch, [])
        kill = tmp_path / "kill"
        t, _broker = _m20_trader(kill_switch_file=str(kill))
        result: dict = {}
        thread = threading.Thread(target=lambda: result.update(r=t.run()), daemon=True)
        thread.start()
        assert _t_wait(lambda: t._workers[-1]._scheduler is not None)
        kill.write_text("x", encoding="utf-8")
        thread.join(timeout=5.0)
        assert not thread.is_alive()
        assert result["r"] == []
        assert all(w.stop_event.is_set() for w in t._workers)
        assert "kill-switch file" in capsys.readouterr().out

    def test_on_stop_close_all_flattens_every_pair(self, monkeypatch):
        _t_patch_scheduler(monkeypatch, [_t_tick([_t_candle(0)])])
        t, broker = _m20_trader(signals=[{_T0: _M20_SIG}, {_T0: _M20_SIG}],
                                on_stop=ON_STOP_CLOSE_ALL)
        result: dict = {}
        thread = threading.Thread(target=lambda: result.update(r=t.run()), daemon=True)
        thread.start()
        assert _t_wait(lambda: len(t.open_trades) == 2)
        t.stop()
        thread.join(timeout=5.0)
        assert not thread.is_alive()
        assert t.open_trades == ()
        assert sorted(tr.symbol for tr in result["r"]) == ["BTCUSDT", "ETHUSDT"]
        assert all(tr.close_reason == CLOSE_REASON_FC for tr in result["r"])

    def test_startup_failure_shuts_down_started_pairs(self, monkeypatch):
        # Pair B's reconcile hits a venue read failure → the start
        # aborts, pair A (already started) is shut down again, positions
        # stay protected (none were opened), the error propagates.
        _t_patch_scheduler(monkeypatch, [])
        broker_b = _TFuturesBroker(_x_candles(10), fill_price=110.0)

        def boom(symbol=None):
            raise ConnectionFailed("venue down")

        broker_b.open_positions = boom
        pairs = [
            TraderPair(broker=_M20SharedFuturesBroker(_x_candles(10),
                                                      fill_price=110.0),
                       config=_t_cfg(), strategy=_TPlanStrategy()),
            TraderPair(broker=broker_b, config=_t_cfg(symbol="ETHUSDT"),
                       strategy=_TPlanStrategy()),
        ]
        t = Trader(pairs=pairs, trader_id="t20")
        with pytest.raises(BrokerError):
            t.run()
        assert all(w.stop_event.is_set() for w in t._workers)
        assert t.open_trades == ()


class TestRunLiveCombined:
    @pytest.fixture(autouse=True)
    def _no_keep_alive(self, monkeypatch):
        import AlgoTradeKit.simulate._live as live_mod

        monkeypatch.setattr(live_mod, "_register_keep_alive", lambda: None)

    @pytest.fixture()
    def fake_combined(self, monkeypatch):
        """Replace _CombinedLiveReport with a recording fake (no server)."""

        class _FakeCombined:
            instances: list = []

            def __init__(self, labels, reports, *, host, port=0,
                         open_browser=True, title="AlgoTradeKit Combined Report"):
                self.labels = list(labels)
                self.reports = list(reports)
                self.host = host
                self.open_browser = open_browser
                self.updates: list[tuple] = []
                _FakeCombined.instances.append(self)

            @property
            def url(self):
                return "http://combined.fake"

            def update(self, label, report):
                self.updates.append((label, report))

        monkeypatch.setattr(run_live_module, "_CombinedLiveReport", _FakeCombined)
        return _FakeCombined

    @staticmethod
    def _pairs(broker, *, display_first=True):
        cfg_a = _paper_cfg(display=display_first, display_candles=6,
                           display_open_browser=False)
        cfg_b = _paper_cfg(symbol="ETHUSDT")
        plan = {_p_ts(10): [{"entry": 100.0, "sl": 95.0, "tp": 110.0}]}
        return [
            TraderPair(broker=broker, config=cfg_a,
                       strategy=_PaperPlanStrategy(plan)),
            TraderPair(broker=broker, config=cfg_b,
                       strategy=_PaperPlanStrategy({})),
        ]

    def test_combined_labels_dedup(self):
        broker = _PaperBroker(_p_flat(10))
        entries = [
            TraderPair(broker=broker, config=_paper_cfg(),
                       strategy=_PaperPlanStrategy({})),
            TraderPair(broker=_PaperBroker(_p_flat(10)), config=_paper_cfg(),
                       strategy=_PaperPlanStrategy({})),
            TraderPair(broker=broker, config=_paper_cfg(symbol="ETHUSDT"),
                       strategy=_PaperPlanStrategy({})),
        ]
        assert _combined_labels(entries) == ["BTCUSDT", "BTCUSDT#2", "ETHUSDT"]

    def test_combined_started_and_fed_per_closed_candle(self, fake_combined, capsys):
        live = [
            _p_candle(10, 100.0, 100.5, 99.5, 100.0),
            _p_candle(11, 100.2, 111.0, 99.8, 110.5),
        ]
        broker = _PaperBroker(_p_flat(10), live_rows=live)
        reports = run_live(pairs=self._pairs(broker))
        (combined,) = fake_combined.instances
        assert combined.labels == ["BTCUSDT", "ETHUSDT"]
        assert combined.host == "127.0.0.1" and combined.open_browser is False
        assert all(isinstance(r, SimulateReport) for r in combined.reports)
        assert all(r.total_trades == 0 for r in combined.reports)   # seed snapshots
        # One update per closed candle of EITHER pair, labelled per pair;
        # the last BTCUSDT snapshot carries its finished TP trade.
        assert [lb for lb, _ in combined.updates].count("BTCUSDT") == 2
        assert [lb for lb, _ in combined.updates].count("ETHUSDT") == 2
        last_a = [r for lb, r in combined.updates if lb == "BTCUSDT"][-1]
        assert last_a.total_trades == 1 and reports[0].total_trades == 1
        assert "combined report → http://combined.fake" in capsys.readouterr().out

    def test_no_combined_without_display_or_second_pair(self, fake_combined):
        live = [_p_candle(10, 100.0, 100.5, 99.5, 100.0)]
        # ≥2 pairs, nobody displays → no combined server.
        broker = _PaperBroker(_p_flat(10), live_rows=live)
        run_live(pairs=self._pairs(broker, display_first=False))
        assert fake_combined.instances == []
        # Single-pair form → no combined either.
        broker2 = _PaperBroker(_p_flat(10), live_rows=live)
        run_live(strategy=_PaperPlanStrategy({}), broker=broker2,
                 config=_paper_cfg())
        assert fake_combined.instances == []

    def test_combined_real_server_payload(self, monkeypatch, capsys):
        # Full path: real per-pair chart/report servers + the real combined
        # ReportServer; the replay payload ends at the pushed (post-trade)
        # state and aggregates both pairs.
        created: list = []
        real = run_live_module._CombinedLiveReport

        class _Spy(real):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                created.append(self)

        monkeypatch.setattr(run_live_module, "_CombinedLiveReport", _Spy)
        live = [
            _p_candle(10, 100.0, 100.5, 99.5, 100.0),
            _p_candle(11, 100.2, 111.0, 99.8, 110.5),
        ]
        broker = _PaperBroker(_p_flat(10), live_rows=live)
        try:
            reports = run_live(pairs=self._pairs(broker))
            (combined,) = created
            payload = combined.server._pending_data
            assert payload["combined"] is True
            assert [row["label"] for row in payload["pairs"]] == [
                "BTCUSDT", "ETHUSDT"]
            assert payload["summary"]["total_trades"] == 1   # pushed, not seed
            assert payload["summary"]["initial_balance"] == 20_000.0
            assert payload["has_chart"] is False
            assert f"combined report → {combined.url}" in capsys.readouterr().out
            assert reports[0].total_trades == 1
        finally:
            for spy in created:
                spy.server.stop()


# ---------------------------------------------------------------------------
# — trader: display bridge + real-fills pipeline
# ---------------------------------------------------------------------------

import AlgoTradeKit.trader._display as display_module  # noqa: E402


class _D21ChartServer:
    def __init__(self, host):
        self.port = 4321
        self.host = host


class _D21Chart:
    """Recording stand-in for ``visual.Chart`` (display tests)."""

    def __init__(self, title="", host="127.0.0.1", port=0, candle_count_limit=None, **kw):
        self.title = title
        self.host = host
        self.candle_count_limit = candle_count_limit
        self.frames: list = []
        self.bars: list[dict] = []
        self.live: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.shown = False
        self.open_browser = None
        self.url = f"http://{host}:4321"
        self._server = _D21ChartServer(host)
        self._ids = itertools.count(1)

    def set_data(self, df, candle_range=None):
        self.frames.append(df)

    def show(self, open_browser=True, block=False):
        self.shown = True
        self.open_browser = open_browser

    def stream_from_atk(self, candle):
        self.bars.append(dict(candle))

    def add_live_position(self, **kw):
        drawing_id = f"live-{next(self._ids)}"
        self.live[drawing_id] = dict(kw)
        self.calls.append(("add_live", drawing_id, dict(kw)))
        return drawing_id

    def update_live_position(self, drawing_id, **kw):
        self.live.setdefault(drawing_id, {}).update(kw)
        self.calls.append(("update_live", drawing_id, dict(kw)))

    def remove_drawing(self, drawing_id):
        self.live.pop(drawing_id, None)
        self.calls.append(("remove", drawing_id))

    def navigate_to_candle(self, timestamp_ms):
        self.calls.append(("navigate", timestamp_ms))


class _D21ReportServer:
    """Recording stand-in for ``report._server.ReportServer``."""

    def __init__(self, title="", port=0, host="127.0.0.1", on_open_chart=None, **kw):
        self.title = title
        self.host = host
        self.on_open_chart = on_open_chart
        self.port = 5678
        self.url = f"http://{host}:5678"
        self.started = False
        self.open_browser = None
        self.data = None
        self.pushes: list = []

    def start(self, open_browser=True):
        self.started = True
        self.open_browser = open_browser

    def set_report_data(self, data):
        self.data = data

    def push_update(self, report):
        self.pushes.append(report)


@pytest.fixture
def d21(monkeypatch):
    """ display stubs: fake Chart/ReportServer, recorded draw_trade_group,
    keep-alive disabled, zero server settle."""
    import AlgoTradeKit.report._server as report_server_mod
    import AlgoTradeKit.simulate._engine as engine_mod
    import AlgoTradeKit.simulate._live as live_mod
    import AlgoTradeKit.visual as visual_pkg
    import AlgoTradeKit.visual.indicator_renderer as renderer_mod

    made = SimpleNamespace(charts=[], servers=[], groups=[])

    class _Chart(_D21Chart):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            made.charts.append(self)

    class _Server(_D21ReportServer):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            made.servers.append(self)

    def _group(chart, markers, *, opacity=0.15, config=None):
        made.groups.append((chart, markers, opacity, config))

    monkeypatch.setattr(visual_pkg, "Chart", _Chart)
    monkeypatch.setattr(report_server_mod, "ReportServer", _Server)
    monkeypatch.setattr(renderer_mod, "draw_trade_group", _group)
    monkeypatch.setattr(live_mod, "_register_keep_alive", lambda: None)
    monkeypatch.setattr(engine_mod, "_register_keep_alive", lambda: None)
    monkeypatch.setattr(display_module, "_SERVER_SETTLE_SECONDS", 0.0)
    return made


def _d_cfg(**overrides):
    kwargs = {"display": True, "display_candles": 10, "display_trades": "real"}
    kwargs.update(overrides)
    return _t_cfg(**kwargs)


def _d_start(t: Trader, worker=None):
    """Seed + arm one pair, start its display bridge, then park the display
    thread — tests drive ``_next_item``/``_process`` deterministically."""
    worker = worker if worker is not None else t._worker
    worker.seed()
    worker.engine.start()
    bridge = worker.display
    bridge.start()
    bridge._stop.set()
    bridge._thread.join(timeout=5.0)
    bridge._stop.clear()
    return worker, bridge


def _d_drain(bridge):
    """Process everything currently queued, computing backlog like _run."""
    while bridge._pending or not bridge._queue.empty():
        kind, payload = bridge._next_item()
        bridge._process(kind, payload, backlog=bridge._backlog())


def _d_open(worker, entry=110.0, sl=100.0, ts=None):
    ts = ts if ts is not None else _T0 + 10 * _XHOUR
    signal = Signal("long", entry, sl, None, ts, 9, "1h")
    return worker.engine.open_position(signal)


class TestDisplayWiring:
    def test_worker_hands_over_candles_and_forming(self, d21):
        t = _t_trader(cfg=_d_cfg())
        worker, bridge = _d_start(t)
        candle = _t_candle(10)
        worker._process_scheduler_tick(_t_tick([candle]))
        forming = dict(_t_candle(11), closed=False)
        worker._process_forming(forming)
        kinds = []
        while not bridge._queue.empty():
            kinds.append(bridge._queue.get_nowait())
        assert [k for k, _ in kinds] == ["candle", "forming"]
        assert kinds[0][1]["timestamp"] == candle["timestamp"]

    def test_display_only_feed_for_candle_close(self, d21):
        broker = _TFuturesBroker(_x_candles(10), fill_price=110.0)
        t = _t_trader(broker=broker, cfg=_d_cfg())
        _worker, bridge = _d_start(t)
        # hybrid-display note: candle_close pairs get a display-only
        # forming stream; its closed candles are dropped (scheduler commits).
        assert bridge._feed is not None
        (call,) = broker.stream_candles_calls
        assert call["closed_only"] is False
        broker.candles_cb(dict(_t_candle(10), closed=True))
        assert bridge._queue.empty()
        broker.candles_cb(dict(_t_candle(10), closed=False))
        assert bridge._queue.get_nowait()[0] == "forming"

    def test_no_extra_feed_in_realtime_modes(self, d21):
        t = _t_trader(cfg=_d_cfg(execution=EXEC_CANDLE_UPDATE))
        _worker, bridge = _d_start(t)
        assert bridge._feed is None   # the worker's own forming feed is the source

    def test_no_extra_feed_for_mt5_ladder_pair(self, d21):
        broker = _TMT5Broker(_x_candles(10), fill_price=110.0)
        cfg = _d_cfg(symbol="EURUSD", tp_mode=TP_MODE_MULTI_RR,
                     tp_levels=[1.0, 2.0], tp_level_close_fractions=[0.5, 0.5])
        t = _t_trader(broker=broker, cfg=cfg)
        _worker, bridge = _d_start(t)
        assert bridge._feed is None   # the ladder-detection feed flows via the worker


class TestDisplaySim:
    def test_sim_display_advances_independently(self, d21):
        ts = _T0 + 10 * _XHOUR
        strategy = _TPlanStrategy(signals_at={ts: [("long", 110.0, 100.0, None)]})
        t = _t_trader(cfg=_d_cfg(display_trades="sim"), strategy=strategy)
        worker, bridge = _d_start(t)
        sim = bridge.sim
        assert sim is not None and sim.config is worker.engine.sim_config
        assert sim.config.initial_balance == 10_000.0     # real start balance
        assert sim.config.show_chart is True
        assert bridge._sim_strategy is not strategy       # deep copy drives the sim
        assert sim.last_report is not None                # seeded
        rows_before = len(worker.data["1h"])
        bridge._process_candle(_t_candle(10), backlog=False)
        assert len(sim.data["1h"]) == 11                  # sim advanced
        assert len(worker.data["1h"]) == rows_before      # trader state untouched
        assert len(sim.stepper.open_positions) == 1       # the scripted signal filled
        chart = made_chart = d21.charts[0]
        assert made_chart.live                            # live drawing for the sim trade
        assert chart.bars[-1]["timestamp"] == _T0 + 10 * _XHOUR

    def test_backlog_quiets_sim_report_push(self, d21):
        t = _t_trader(cfg=_d_cfg(display_trades="sim"))
        _worker, bridge = _d_start(t)
        server = d21.servers[0]                           # the sim's report server
        pushed = len(server.pushes)
        bridge._process_candle(_t_candle(10), backlog=True)
        assert len(server.pushes) == pushed               # coalesced
        bridge._process_candle(_t_candle(11), backlog=False)
        assert len(server.pushes) == pushed + 1


class TestDisplayReal:
    def test_open_event_draws_live_position(self, d21):
        t = _t_trader(cfg=_d_cfg())
        worker, bridge = _d_start(t)
        trade = _d_open(worker)
        _d_drain(bridge)
        chart = bridge._real_chart
        (call,) = [c for c in chart.calls if c[0] == "add_live"]
        assert call[2]["entry_price"] == 110.0
        assert call[2]["stop_loss"] == 100.0
        assert call[2]["trade_id"] == trade.pos.trade_id
        assert bridge._real_drawings[trade.pos.trade_id] == call[1]

    def test_sl_move_event_updates_drawing(self, d21):
        t = _t_trader(cfg=_d_cfg())
        worker, bridge = _d_start(t)
        trade = _d_open(worker)
        _d_drain(bridge)
        trade.pos.stop_loss = 105.0
        worker.events.emit(SlMoveEvent(
            time=_T0 + 11 * _XHOUR, symbol="BTCUSDT", source=SOURCE_LIVE,
            trade_id=trade.pos.trade_id, old_sl=100.0, new_sl=105.0,
        ))
        _d_drain(bridge)
        chart = bridge._real_chart
        (call,) = [c for c in chart.calls if c[0] == "update_live"]
        assert call[2]["stop_loss"] == 105.0

    def test_close_finalizes_drawing_and_pushes_real_report(self, d21):
        t = _t_trader(cfg=_d_cfg())
        worker, bridge = _d_start(t)
        trade = _d_open(worker)
        _d_drain(bridge)
        worker.engine.record_external_close(trade, 112.0, CLOSE_REASON_SL)
        _d_drain(bridge)
        chart = bridge._real_chart
        assert [c[0] for c in chart.calls if c[0] == "remove"] == ["remove"]
        ((g_chart, markers, opacity, g_cfg),) = d21.groups
        assert g_chart is chart and opacity == 0.15
        assert markers[0]["trade_id"] == trade.pos.trade_id
        server = bridge._real_server
        report = server.pushes[-1]
        assert isinstance(report, SimulateReport)
        assert report.closed_trades[0].exit_price == 112.0   # the real fill

    def test_equity_snapshot_per_candle(self, d21):
        broker = _TFuturesBroker(_x_candles(10), fill_price=110.0)
        t = _t_trader(broker=broker, cfg=_d_cfg())
        _worker, bridge = _d_start(t)
        assert len(bridge._real_balance) == 1     # seed anchor
        broker.balance = 10_500.0
        bridge._process_candle(_t_candle(10), backlog=False)
        snap = bridge._real_balance[-1]
        assert snap == {"timestamp": _T0 + 10 * _XHOUR,
                        "wallet": 10_500.0, "equity": 10_500.0}
        report = bridge._real_server.pushes[-1]
        assert report.final_balance == 10_500.0

    def test_equity_read_failure_carries_previous(self, d21):
        broker = _TFuturesBroker(_x_candles(10), fill_price=110.0)
        t = _t_trader(broker=broker, cfg=_d_cfg())
        worker, bridge = _d_start(t)
        events = _t_collect(t)
        def _boom():
            raise BrokerError("account endpoint down")
        broker.get_account_info = _boom
        bridge._process_candle(_t_candle(10), backlog=False)
        assert bridge._real_balance[-1]["equity"] == 10_000.0   # carried forward
        errors = [e for e in events if e.event_type == EVENT_ERROR]
        assert len(errors) == 1 and errors[0].where == "display"
        bridge._process_candle(_t_candle(11), backlog=False)
        assert len([e for e in events if e.event_type == EVENT_ERROR]) == 1  # once

    def test_spot_falls_back_to_read_balance(self, d21):
        broker = _TSpotBroker(_x_candles(10), fill_price=110.0)
        t = _t_trader(broker=broker, cfg=_d_cfg())
        _worker, bridge = _d_start(t)
        bridge._process_candle(_t_candle(10), backlog=False)
        snap = bridge._real_balance[-1]
        assert snap["wallet"] == 10_000.0 and snap["equity"] == 10_000.0

    def test_initial_real_payload_links_the_chart(self, d21):
        t = _t_trader(cfg=_d_cfg())
        worker, bridge = _d_start(t)
        server = bridge._real_server
        assert "(real)" in server.title
        assert server.data["has_chart"] is True
        assert server.data["chart_port"] == 4321
        trade = _d_open(worker)
        _d_drain(bridge)
        worker.engine.record_external_close(trade, 112.0, CLOSE_REASON_SL)
        _d_drain(bridge)
        server.on_open_chart(trade.pos.trade_id)
        chart = bridge._real_chart
        assert ("navigate", chart.calls[-1][1]) == chart.calls[-1]


class TestDisplayBoth:
    def test_one_chart_two_section_page(self, d21):
        t = _t_trader(cfg=_d_cfg(display_trades="both"))
        worker, bridge = _d_start(t)
        assert len(d21.charts) == 1                       # the sim's chart, shared
        assert bridge._real_chart is bridge.sim.chart
        assert bridge.sim.config.report_mode == REPORT_MODE_NONE
        assert worker.engine.sim_config.report_mode == REPORT_MODE_WEBPAGE  # untouched
        (server,) = d21.servers                           # only the combined page
        assert server.data["combined"] is True
        assert [row["label"] for row in server.data["pairs"]] == [
            "BTCUSDT (sim)", "BTCUSDT (real)"]
        assert server.data["has_chart"] is False

    def test_real_overlay_uses_higher_opacity(self, d21):
        t = _t_trader(cfg=_d_cfg(display_trades="both"))
        worker, bridge = _d_start(t)
        trade = _d_open(worker)
        _d_drain(bridge)
        worker.engine.record_external_close(trade, 112.0, CLOSE_REASON_SL)
        _d_drain(bridge)
        ((g_chart, _markers, opacity, _cfg),) = d21.groups
        assert g_chart is bridge.sim.chart and opacity == 0.30
        payload = bridge._real_server.pushes[-1]
        assert payload["combined"] is True
        real_row = payload["pairs"][1]
        assert real_row["label"] == "BTCUSDT (real)"
        assert real_row["total_trades"] == 1


class TestDisplayUrlMode:
    def test_urls_printed_when_browser_off(self, d21, capsys):
        t = _t_trader(cfg=_d_cfg(display_open_browser=False))
        _worker, bridge = _d_start(t)
        out = capsys.readouterr().out
        assert "BTCUSDT chart  → http://127.0.0.1:4321" in out
        assert "BTCUSDT report → http://127.0.0.1:5678" in out
        assert "SECURITY" not in out
        assert bridge._real_chart.open_browser is False
        assert bridge._real_server.open_browser is False

    def test_security_note_on_public_host(self, d21, capsys):
        t = _t_trader(cfg=_d_cfg(display_open_browser=False, chart_host="0.0.0.0"))
        _d_start(t)
        out = capsys.readouterr().out
        assert "SECURITY" in out and "'0.0.0.0'" in out
        assert "SSH tunnel" in out

    def test_browser_mode_prints_no_urls(self, d21, capsys):
        t = _t_trader(cfg=_d_cfg())
        _worker, bridge = _d_start(t)
        out = capsys.readouterr().out
        assert "chart  →" not in out and "report →" not in out
        assert bridge._real_chart.open_browser is True


class TestDisplayWindowTrim:
    def test_balance_window_and_baseline(self, d21):
        broker = _TFuturesBroker(_x_candles(10), fill_price=110.0)
        t = _t_trader(broker=broker, cfg=_d_cfg(candle_count_limit=3))
        _worker, bridge = _d_start(t)
        assert bridge._real_chart.candle_count_limit == 3   # chart-side trim
        equities = {}
        for k in range(10, 15):
            broker.balance = 10_000.0 + k
            bridge._process_candle(_t_candle(k), backlog=False)
            equities[_T0 + k * _XHOUR] = 10_000.0 + k
        assert len(bridge._real_balance) <= 3
        window_start = int(bridge._real_window[0])
        assert all(int(s["timestamp"]) >= window_start for s in bridge._real_balance)
        # Baseline = the equity that entered the window (newest dropped row).
        dropped = [ts for ts in equities if ts < window_start]
        assert bridge._real_baseline == equities[max(dropped)]
        report = bridge._real_server.pushes[-1]
        assert report.initial_balance == bridge._real_baseline

    def test_old_real_trade_dropped_but_run_record_kept(self, d21):
        t = _t_trader(cfg=_d_cfg(candle_count_limit=3))
        worker = t._worker
        worker.seed()
        worker.engine.start()
        trade = _d_open(worker, ts=_X0 + 1 * _XHOUR)
        # Closed long before the display window starts (the seed spans bars
        # _X0+0h.._X0+9h, the window keeps the last 3 → start _X0+7h).
        worker.engine.record_external_close(
            trade, 105.0, CLOSE_REASON_SL, close_time=_X0 + 2 * _XHOUR
        )
        bridge = worker.display
        bridge.start()
        bridge._stop.set()
        bridge._thread.join(timeout=5.0)
        bridge._stop.clear()
        assert bridge._real_trades == []                    # trimmed at seed
        assert len(worker.closed_trades) == 1               # run() record untouched
        assert bridge._build_real_report().total_trades == 0


class TestDisplayPriority:
    def test_trading_always_drains_first(self, d21):
        t = _t_trader(cfg=_d_cfg())
        worker = t._worker
        worker.seed()
        worker.engine.start()
        bridge = worker.display
        for _ in range(3):
            worker.queue.put(("reconcile", None))           # pending trading work
        bridge.start()                                      # display thread runs
        try:
            snapshots = len(bridge._real_balance)
            bridge.enqueue_candle(_t_candle(10))
            time.sleep(0.4)
            assert len(bridge._real_balance) == snapshots   # starved: trading first
            while not worker.queue.empty():
                worker.queue.get_nowait()                   # trading drained
            assert _t_wait(lambda: len(bridge._real_balance) == snapshots + 1)
        finally:
            bridge._stop.set()
            bridge._thread.join(timeout=5.0)

    def test_backlog_coalesces_pushes_and_venue_reads(self, d21):
        broker = _TFuturesBroker(_x_candles(10), fill_price=110.0)
        t = _t_trader(broker=broker, cfg=_d_cfg())
        _worker, bridge = _d_start(t)
        reads = {"n": 0}
        original = broker.get_account_info
        def _counting():
            reads["n"] += 1
            return original()
        broker.get_account_info = _counting
        server = bridge._real_server
        pushed = len(server.pushes)
        for k in range(10, 14):
            bridge.enqueue_candle(_t_candle(k))
        _d_drain(bridge)
        assert reads["n"] == 1                              # only the newest read
        assert len(server.pushes) == pushed + 1             # one coalesced push
        assert len(bridge._real_balance) == 5               # every candle snapshotted
        assert [s["equity"] for s in bridge._real_balance[-4:-1]] == [10_000.0] * 3

    def test_forming_rows_skip_to_latest(self, d21):
        t = _t_trader(cfg=_d_cfg())
        _worker, bridge = _d_start(t)
        for k in range(3):
            bridge.enqueue_forming(dict(_t_candle(10), close=110.0 + k, closed=False))
        item = bridge._next_item()
        assert item[0] == "forming" and item[1]["close"] == 112.0
        assert bridge._queue.empty() and not bridge._pending


class TestTraderCombinedDisplay:
    class _FakeCombined:
        instances: list = []

        def __init__(self, labels, reports, *, host="127.0.0.1",
                     port=0, open_browser=True, title=""):
            self.labels = list(labels)
            self.reports = list(reports)
            self.host = host
            self.open_browser = open_browser
            self.updates: list = []
            self.url = "http://127.0.0.1:9999"
            type(self).instances.append(self)

        def update(self, label, report):
            self.updates.append((label, report))

    @pytest.fixture(autouse=True)
    def _fake_combined(self, monkeypatch):
        self._FakeCombined.instances = []
        monkeypatch.setattr(
            run_live_module, "_CombinedLiveReport", self._FakeCombined
        )

    def _two_pair_trader(self, **cfg_overrides):
        pairs = [
            TraderPair(broker=_TFuturesBroker(_x_candles(10), fill_price=110.0),
                       config=_d_cfg(**cfg_overrides), strategy=_TPlanStrategy()),
            TraderPair(broker=_TFuturesBroker(_x_candles(10), fill_price=110.0),
                       config=_d_cfg(symbol="ETHUSDT", **cfg_overrides),
                       strategy=_TPlanStrategy()),
        ]
        return Trader(pairs=pairs, trader_id="t21")

    def test_combined_armed_and_fed(self, d21, capsys):
        t = self._two_pair_trader(display_open_browser=False)
        bridges = []
        for worker in t._workers:
            bridges.append(_d_start(t, worker)[1])
        t._maybe_start_combined()
        (combined,) = self._FakeCombined.instances
        assert combined.labels == ["BTCUSDT", "ETHUSDT"]
        assert all(isinstance(r, SimulateReport) for r in combined.reports)
        assert combined.open_browser is False
        assert f"combined report → {combined.url}" in capsys.readouterr().out
        bridges[1]._process_candle(_t_candle(10), backlog=False)
        assert combined.updates[-1][0] == "ETHUSDT"

    def test_combined_source_follows_display_trades(self, d21):
        pairs = [
            TraderPair(broker=_TFuturesBroker(_x_candles(10), fill_price=110.0),
                       config=_d_cfg(display_trades="sim"),
                       strategy=_TPlanStrategy()),
            TraderPair(broker=_TFuturesBroker(_x_candles(10), fill_price=110.0),
                       config=_d_cfg(symbol="ETHUSDT"),
                       strategy=_TPlanStrategy()),
        ]
        t = Trader(pairs=pairs, trader_id="t21")
        for worker in t._workers:
            _d_start(t, worker)
        t._maybe_start_combined()
        (combined,) = self._FakeCombined.instances
        sim_bridge, real_bridge = (w.display for w in t._workers)
        assert combined.reports[0] is sim_bridge.sim.last_report   # sim pair → sim
        assert combined.reports[1].total_trades == 0               # real pair → real
        assert combined.reports[1].initial_balance == 10_000.0

    def test_combined_gate_negatives(self, d21):
        # One displaying pair of two → no combined page.
        pairs = [
            TraderPair(broker=_TFuturesBroker(_x_candles(10), fill_price=110.0),
                       config=_t_cfg(), strategy=_TPlanStrategy()),
            TraderPair(broker=_TFuturesBroker(_x_candles(10), fill_price=110.0),
                       config=_d_cfg(symbol="ETHUSDT"), strategy=_TPlanStrategy()),
        ]
        t = Trader(pairs=pairs, trader_id="t21")
        t._workers[0].seed()
        t._workers[0].engine.start()
        _d_start(t, t._workers[1])
        assert t._maybe_start_combined() is None
        # Single-pair form → never a combined page.
        single = _t_trader(cfg=_d_cfg())
        _d_start(single)
        assert single._maybe_start_combined() is None
        assert self._FakeCombined.instances == []
