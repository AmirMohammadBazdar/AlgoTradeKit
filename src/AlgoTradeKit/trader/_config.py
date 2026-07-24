"""
AlgoTradeKit.trader._config
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Configuration & types for the v1.0.0 live-trading module:
``TraderConfig``, ``TraderPair``, ``TraderSettings`` and the pair-list
validation used by the multi-pair ``Trader``.

``TraderConfig`` deliberately uses the **same field names as
``SimulateConfig``**, so a tuned backtest config copy-pastes into live
trading, plus trader-only groups: cost overrides, execution mode, data/feed,
event logging, display and safety.  The display ``SimulateConfig`` is derived
automatically via :meth:`TraderConfig.to_simulate_config` — same-name fields
copied, costs filled from ``broker.get_trading_costs()`` unless
overridden.

Notes
-----
* ``Trader`` itself lands in and ``run_live()`` in — this module is
  types + validation only.
* ``TraderSettings`` carries the Trader-level constructor kwargs (they apply
  to all pairs); ``on_stop`` and ``kill_switch_file`` are live and
  ``state_path`` journaling / restart reconciliation is live (
  ``trader/_state.py``); the fields and their validation live here.
* :func:`validate_pairs` enforces the rule — the same ``(broker, symbol)``
  never appears twice — at config time (relies on it).
* Mirrored-field validation is delegated to a probe ``SimulateConfig`` so
  the simulate rules stay the single source of truth (error messages are
  re-prefixed ``TraderConfig.``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from ..broker import MetaTraderBroker
from ..simulate import (
    EXCHANGE_TYPE_EXCHANGE,
    EXCHANGE_TYPE_METATRADER,
    REPORT_MODE_NONE,
    REPORT_MODE_WEBPAGE,
    SIZING_RISK_PERCENT,
    SL_MODE_SIGNAL,
    TP_MODE_SIGNAL,
    SimulateConfig,
)
from ..strategy import BaseStrategy
from ._events import ALL_EVENT_TYPES

# ---------------------------------------------------------------------------
# String-literal constants (re-exported from trader/__init__.py)
# ---------------------------------------------------------------------------

#: Execution mode — when the strategy is (re-)evaluated.
EXEC_CANDLE_CLOSE = "candle_close"    # once per closed candle, at the exact boundary
EXEC_CANDLE_UPDATE = "candle_update"  # on every forming-candle update (~1-2 s)
EXEC_TICK = "tick"                    # on every tick — finest granularity

#: Which trades the live display shows.
DISPLAY_TRADES_SIM = "sim"    # theoretical performance from the display LiveSimulation
DISPLAY_TRADES_REAL = "real"  # actual broker fills — boxes, markers and report stats
DISPLAY_TRADES_BOTH = "both"  # real fills overlaid on sim trades; report shows both

#: What happens to open positions on Trader shutdown.
ON_STOP_KEEP = "keep"            # positions stay; venue SL/TP keep protecting them
ON_STOP_CLOSE_ALL = "close_all"  # market-close everything, cancel working orders


# ---------------------------------------------------------------------------
# Safety-field parsing
# ---------------------------------------------------------------------------

def parse_max_daily_loss(value: float | str) -> tuple[str, float]:
    """
    Parse a ``TraderConfig.max_daily_loss`` value into ``(mode, number)``.

    ``mode`` is ``"amount"`` for a plain positive number (account-currency
    dollars) or ``"percent"`` for a string like ``"2%"`` (percent of the
    UTC-day-start account balance, ``0 < p <= 100``).  Used both for config
    validation here and by the daily-loss gate.
    """
    if isinstance(value, str):
        text = value.strip()
        if not text.endswith("%"):
            raise ValueError(
                "TraderConfig.max_daily_loss string form must be a percent like '2%', "
                f"got {value!r} (use a plain number for a $ threshold)."
            )
        try:
            percent = float(text[:-1])
        except ValueError:
            raise ValueError(
                f"TraderConfig.max_daily_loss percent is not a number: {value!r}."
            ) from None
        if not 0.0 < percent <= 100.0:
            raise ValueError(
                f"TraderConfig.max_daily_loss percent must be in (0, 100], got {value!r}."
            )
        return ("percent", percent)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        amount = float(value)
        if amount <= 0:
            raise ValueError(f"TraderConfig.max_daily_loss must be > 0, got {value!r}.")
        return ("amount", amount)
    raise ValueError(
        "TraderConfig.max_daily_loss must be a positive number ($), a percent string "
        f"like '2%', or None, got {value!r}."
    )


# ---------------------------------------------------------------------------
# TraderConfig
# ---------------------------------------------------------------------------

@dataclass
class TraderConfig:
    """
    Full configuration for live-trading one instrument — used by both the
    real-order ``Trader`` and ``run_live()`` paper trading.

    Field names shared with :class:`~AlgoTradeKit.simulate.SimulateConfig`
    keep identical semantics — copy your backtest values straight in.
    Live enforcement is venue-native: SL/TP prices computed by the same
    shared maths the simulator uses are attached to / modified on the real
    venue orders.

    Attributes
    ----------
    symbol : str
        Trading instrument, e.g. ``"BTCUSDT"``, ``"EURUSD"``.  Required.
    min_candles : int
        History length fetched once at startup for the first strategy
        computation (e.g. 500); after that every new candle costs O(1).
        Required.  Belongs to the Data group but sits early because it has
        no default.
    leverage : float
        Leverage multiplier (default 1.0).  Mirrors ``SimulateConfig``.
    position_sizing / risk_per_trade / fixed_amount / fixed_lot / compound
        Sizing group — mirrors ``SimulateConfig``.  Live sizing uses the
        pair's **real account balance**; ``compound=False`` sizes
        against the balance seen at Trader start.
    tp_mode / tp_rr / tp_levels / tp_level_close_fractions
        TP group — mirrors ``SimulateConfig``.  Live: TP is sent to the venue
        only when the mode defines one; multi-RR ladders are reduce-only
        orders on Binance futures / feed-triggered partial closes on MT5.
    sl_mode / trailing_sl_percent / risk_free_enabled / risk_free_at_rr
        SL group — mirrors ``SimulateConfig``.  Live: every SL move modifies
        the venue SL; no soft SL exists anywhere.
    force_close_on_exit_signal : bool
        Mirrors ``SimulateConfig``: strategy ``ExitSignal`` → market close of
        the open position(s).
    max_long_positions / max_short_positions / max_positions : int
        Position limits — mirror ``SimulateConfig``; counted against live
        open positions for the pair.
    spread : float | None
        Cost override, price units.  ``None`` (default) → auto-filled from
        ``broker.get_trading_costs()`` when deriving the display config.
    commission_type : str | None
        Cost override (``"percentage"`` / ``"per_lot"`` / ``"fixed"``).
        ``None`` → auto from the venue.
    commission : float | None
        Cost override (semantics follow ``commission_type``).  ``None`` →
        auto from the venue.  MT5 does not expose commission — set it here.
    execution : str
        ``"candle_close"`` (default) — evaluate once per closed candle at the
        exact venue-clock boundary; ``"candle_update"`` — re-evaluate on
        every forming-candle update; ``"tick"`` — on every tick.
    recompute_window : int | None
        Tail length K for the fallback recompute when the strategy has no
        ``update_indicators`` hook.  ``None`` → ``max(warmup_period, 200)``.
    candle_poll_interval / tick_poll_interval : float
        Poll cadence in seconds for polling venues (MT5).  Defaults 1.0 /
        0.2.
    log_events : bool
        Master toggle for the per-event terminal report.
        Default True.
    log_event_types : frozenset[str] | None
        ``None`` (default) — log every event type; a collection of the
        ``EVENT_*`` constants — log only those.  Stored as a ``frozenset``;
        membership is validated against the event types in
        ``trader._events``.
    display : bool
        ``True`` → live chart + live report for this pair.  Off by
        default — display never slows trading.
    display_trades : str
        ``"sim"`` (default) — theoretical sim performance; ``"real"`` —
        actual broker fills; ``"both"``.  ``run_live()`` forces
        ``"sim"``.
    display_open_browser : bool
        ``True`` (default) — open a local browser tab; ``False`` (VPS) —
        print the chart/report URLs instead.
    chart_host : str
        Bind host for the chart *and* report servers (default
        ``"127.0.0.1"``; ``"0.0.0.0"`` exposes them — see the security
        warning).
    chart_port / report_port : int
        Fixed ports, or 0 (default) to auto-pick a free port.
    display_candles : int | None
        Seed the display with the last N candles.  Exactly one of
        ``display_candles`` / ``display_start`` when ``display=True``; must
        be >= ``min_candles``.
    display_start
        Seed the display from this datetime (str / datetime / UTC ms) until
        now.  The fetched count is checked against ``min_candles`` at seed
        time.
    candle_count_limit : int | None
        Rolling window: chart and report only ever contain the last M
        candles.  ``None`` — unbounded.
    max_daily_loss : float | str | None
        Per-pair daily-loss limit: a number = account-currency $, a
        string like ``"2%"`` = percent of the UTC-day-start balance.
        Realized + unrealized loss for the UTC day at/over the threshold →
        no new entries until the next UTC day.  ``None`` disables.  Enforced
        by the real-order ``Trader``; inert in ``run_live`` paper trading.
    close_on_daily_loss : bool
        With ``max_daily_loss``: also flatten open positions when the limit
        trips (default False).
    """

    # ------------------------------------------------------------------
    # Instrument (min_candles is Data-group but required → no default)
    # ------------------------------------------------------------------
    symbol: str
    min_candles: int
    leverage: float = 1.0

    # ------------------------------------------------------------------
    # Position sizing — mirrors SimulateConfig
    # ------------------------------------------------------------------
    position_sizing: str = SIZING_RISK_PERCENT
    risk_per_trade: float = 1.0
    fixed_amount: float = 100.0
    fixed_lot: float = 0.01
    compound: bool = False

    # ------------------------------------------------------------------
    # Take-profit / stop-loss — mirrors SimulateConfig
    # ------------------------------------------------------------------
    tp_mode: str = TP_MODE_SIGNAL
    tp_rr: float = 2.0
    tp_levels: list[float] = field(default_factory=lambda: [1.0, 2.0, 3.0])
    tp_level_close_fractions: list[float] | None = None
    sl_mode: str = SL_MODE_SIGNAL
    trailing_sl_percent: float = 1.0
    risk_free_enabled: bool = False
    risk_free_at_rr: float = 1.0
    force_close_on_exit_signal: bool = False

    # ------------------------------------------------------------------
    # Position limits — mirrors SimulateConfig
    # ------------------------------------------------------------------
    max_long_positions: int = 1
    max_short_positions: int = 1
    max_positions: int = 1

    # ------------------------------------------------------------------
    # Trading costs — overrides; None → auto from broker.get_trading_costs()
    # ------------------------------------------------------------------
    spread: float | None = None
    commission_type: str | None = None
    commission: float | None = None

    # ------------------------------------------------------------------
    # Execution mode
    # ------------------------------------------------------------------
    execution: str = EXEC_CANDLE_CLOSE

    # ------------------------------------------------------------------
    # Data / feed
    # ------------------------------------------------------------------
    recompute_window: int | None = None
    candle_poll_interval: float = 1.0
    tick_poll_interval: float = 0.2

    # ------------------------------------------------------------------
    # Event logging
    # ------------------------------------------------------------------
    log_events: bool = True
    log_event_types: frozenset[str] | None = None

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------
    display: bool = False
    display_trades: str = DISPLAY_TRADES_SIM
    display_open_browser: bool = True
    chart_host: str = "127.0.0.1"
    chart_port: int = 0
    report_port: int = 0
    display_candles: int | None = None
    display_start: Any = None
    candle_count_limit: int | None = None

    # ------------------------------------------------------------------
    # Safety
    # ------------------------------------------------------------------
    max_daily_loss: float | str | None = None
    close_on_daily_loss: bool = False

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or not self.symbol:
            raise ValueError(
                "TraderConfig.symbol is required — the live feed needs an instrument."
            )
        self.min_candles = int(self.min_candles)
        if self.min_candles < 1:
            raise ValueError(f"TraderConfig.min_candles must be >= 1, got {self.min_candles}.")

        if self.execution not in (EXEC_CANDLE_CLOSE, EXEC_CANDLE_UPDATE, EXEC_TICK):
            raise ValueError(
                f"TraderConfig.execution must be one of '{EXEC_CANDLE_CLOSE}', "
                f"'{EXEC_CANDLE_UPDATE}', '{EXEC_TICK}', got {self.execution!r}."
            )

        # Cost overrides (None = auto; numeric ranges are trader-side rules).
        if self.spread is not None and self.spread < 0:
            raise ValueError(f"TraderConfig.spread must be >= 0 or None, got {self.spread!r}.")
        if self.commission is not None and self.commission < 0:
            raise ValueError(
                f"TraderConfig.commission must be >= 0 or None, got {self.commission!r}."
            )
        # commission_type membership is checked by the probe SimulateConfig below.

        # Data / feed.
        if self.recompute_window is not None:
            self.recompute_window = int(self.recompute_window)
            if self.recompute_window < 1:
                raise ValueError(
                    f"TraderConfig.recompute_window must be >= 1 or None, "
                    f"got {self.recompute_window}."
                )
        if self.candle_poll_interval <= 0 or self.tick_poll_interval <= 0:
            raise ValueError(
                "TraderConfig.candle_poll_interval / tick_poll_interval must be > 0 seconds."
            )

        # Event logging.
        if self.log_event_types is not None:
            types = tuple(self.log_event_types)
            if not all(isinstance(t, str) for t in types):
                raise ValueError(
                    "TraderConfig.log_event_types must be a collection of event-type "
                    "strings, or None for all event types."
                )
            unknown = frozenset(types) - ALL_EVENT_TYPES
            if unknown:
                raise ValueError(
                    f"TraderConfig.log_event_types contains unknown event types "
                    f"{sorted(unknown)}; valid types are {sorted(ALL_EVENT_TYPES)}."
                )
            self.log_event_types = frozenset(types)

        # Display.
        if self.display_trades not in (
            DISPLAY_TRADES_SIM, DISPLAY_TRADES_REAL, DISPLAY_TRADES_BOTH
        ):
            raise ValueError(
                f"TraderConfig.display_trades must be one of '{DISPLAY_TRADES_SIM}', "
                f"'{DISPLAY_TRADES_REAL}', '{DISPLAY_TRADES_BOTH}', "
                f"got {self.display_trades!r}."
            )
        if not isinstance(self.chart_host, str) or not self.chart_host:
            raise ValueError(
                f"TraderConfig.chart_host must be a non-empty host string, "
                f"got {self.chart_host!r}."
            )
        for name in ("chart_port", "report_port"):
            port = int(getattr(self, name))
            setattr(self, name, port)
            if not (0 <= port <= 65535):
                raise ValueError(
                    f"TraderConfig.{name} must be 0 (auto) or a valid port, got {port}."
                )

        # History seed: display_candles XOR display_start.
        if self.display_candles is not None and self.display_start is not None:
            raise ValueError(
                "TraderConfig: display_candles and display_start are mutually exclusive "
                "— give exactly one history-seed input."
            )
        if self.display and self.display_candles is None and self.display_start is None:
            raise ValueError(
                "TraderConfig: display=True needs a history seed — set display_candles "
                "or display_start."
            )
        if self.display_candles is not None:
            self.display_candles = int(self.display_candles)
            if self.display_candles < self.min_candles:
                raise ValueError(
                    f"TraderConfig.display_candles ({self.display_candles}) cannot be "
                    f"lower than min_candles ({self.min_candles})."
                )
        if self.candle_count_limit is not None:
            self.candle_count_limit = int(self.candle_count_limit)
            if self.candle_count_limit < 1:
                raise ValueError(
                    f"TraderConfig.candle_count_limit must be >= 1 or None, "
                    f"got {self.candle_count_limit}."
                )

        # Safety.
        if self.max_daily_loss is not None:
            parse_max_daily_loss(self.max_daily_loss)   # raises on a bad value
        if self.close_on_daily_loss and self.max_daily_loss is None:
            raise ValueError(
                "TraderConfig.close_on_daily_loss=True requires max_daily_loss to be set."
            )

        self._validate_mirrored()

    def _validate_mirrored(self) -> None:
        """
        Validate every ``SimulateConfig``-mirrored field by constructing a
        probe ``SimulateConfig`` — the simulate rules stay the single source
        of validation truth.  Unset cost overrides fall back to the
        ``SimulateConfig`` defaults as placeholders (they are auto-derived
        later); error messages are re-prefixed ``TraderConfig.``.
        """
        kwargs: dict[str, Any] = {
            "symbol": self.symbol,
            "leverage": self.leverage,
            "position_sizing": self.position_sizing,
            "risk_per_trade": self.risk_per_trade,
            "fixed_amount": self.fixed_amount,
            "fixed_lot": self.fixed_lot,
            "compound": self.compound,
            "max_long_positions": self.max_long_positions,
            "max_short_positions": self.max_short_positions,
            "max_positions": self.max_positions,
            "tp_mode": self.tp_mode,
            "tp_rr": self.tp_rr,
            "tp_levels": self.tp_levels,
            "tp_level_close_fractions": self.tp_level_close_fractions,
            "sl_mode": self.sl_mode,
            "trailing_sl_percent": self.trailing_sl_percent,
            "risk_free_enabled": self.risk_free_enabled,
            "risk_free_at_rr": self.risk_free_at_rr,
            "force_close_on_exit_signal": self.force_close_on_exit_signal,
        }
        if self.spread is not None:
            kwargs["spread"] = self.spread
        if self.commission_type is not None:
            kwargs["commission_type"] = self.commission_type
        if self.commission is not None:
            kwargs["commission"] = self.commission
        try:
            SimulateConfig(**kwargs)
        except ValueError as exc:
            raise ValueError(str(exc).replace("SimulateConfig.", "TraderConfig.")) from None

    # ------------------------------------------------------------------
    # Display-config derivation (the dependency)
    # ------------------------------------------------------------------

    def to_simulate_config(
        self,
        broker: Any,
        *,
        initial_balance: float,
        primary_timeframe: str,
    ) -> SimulateConfig:
        """
        Derive the display ``SimulateConfig`` for this pair.

        Same-name fields are copied; ``spread`` / ``commission_type`` /
        ``commission`` come from the config when set, else from
        ``broker.get_trading_costs(symbol)`` — user overrides always
        win, and the venue is only queried when at least one cost field is
        unset.  ``exchange_type`` follows the broker (``MetaTraderBroker`` →
        ``"metatrader"``, anything else → ``"exchange"``).

        ``initial_balance`` and ``primary_timeframe`` are the caller's:
        the wallet is the pair's real account balance (or the paper-trading
        default) and the timeframe is the strategy's — neither is a
        ``TraderConfig`` field.  ``display`` maps onto ``show_chart`` /
        ``report_mode``: on → chart + webpage report, off → neither.
        """
        spread, commission_type, commission = self.spread, self.commission_type, self.commission
        if spread is None or commission_type is None or commission is None:
            costs = broker.get_trading_costs(self.symbol)
            if spread is None:
                spread = costs.spread
            if commission_type is None:
                commission_type = costs.commission_type
            if commission is None:
                commission = costs.commission
        exchange_type = (
            EXCHANGE_TYPE_METATRADER
            if isinstance(broker, MetaTraderBroker)
            else EXCHANGE_TYPE_EXCHANGE
        )
        return SimulateConfig(
            initial_balance=initial_balance,
            symbol=self.symbol,
            exchange_type=exchange_type,
            leverage=self.leverage,
            spread=spread,
            commission_type=commission_type,
            commission=commission,
            position_sizing=self.position_sizing,
            risk_per_trade=self.risk_per_trade,
            fixed_amount=self.fixed_amount,
            fixed_lot=self.fixed_lot,
            compound=self.compound,
            max_long_positions=self.max_long_positions,
            max_short_positions=self.max_short_positions,
            max_positions=self.max_positions,
            tp_mode=self.tp_mode,
            tp_rr=self.tp_rr,
            tp_levels=list(self.tp_levels),
            tp_level_close_fractions=(
                list(self.tp_level_close_fractions)
                if self.tp_level_close_fractions is not None
                else None
            ),
            sl_mode=self.sl_mode,
            trailing_sl_percent=self.trailing_sl_percent,
            risk_free_enabled=self.risk_free_enabled,
            risk_free_at_rr=self.risk_free_at_rr,
            force_close_on_exit_signal=self.force_close_on_exit_signal,
            primary_timeframe=primary_timeframe,
            show_chart=self.display,
            report_mode=REPORT_MODE_WEBPAGE if self.display else REPORT_MODE_NONE,
        )

    def __repr__(self) -> str:
        return (
            f"<TraderConfig {self.symbol} exec={self.execution} "
            f"risk={self.risk_per_trade}% lev={self.leverage}x "
            f"tp={self.tp_mode} sl={self.sl_mode} display={self.display}>"
        )


# ---------------------------------------------------------------------------
# TraderPair + pair-list validation
# ---------------------------------------------------------------------------

@dataclass
class TraderPair:
    """
    One trading entry for the multi-pair ``Trader``: a broker, its
    per-pair config and the strategy to run.  Brokers may repeat across
    entries (pairs on the same broker share that account's wallet); the same
    ``(broker, symbol)`` may not appear twice — :func:`validate_pairs`.

    ``broker`` is duck-typed (anything with the ``BaseBroker`` surface, same
    as ``LiveSimulation``) so test doubles work; ``config`` / ``strategy``
    must be real ``TraderConfig`` / ``BaseStrategy`` instances.
    """

    broker: Any
    config: TraderConfig
    strategy: BaseStrategy

    def __post_init__(self) -> None:
        if self.broker is None:
            raise TypeError("TraderPair.broker is required (a Broker/BaseBroker instance).")
        if not isinstance(self.config, TraderConfig):
            raise TypeError(
                f"TraderPair.config must be a TraderConfig, got {type(self.config).__name__}."
            )
        if not isinstance(self.strategy, BaseStrategy):
            raise TypeError(
                f"TraderPair.strategy must be a BaseStrategy, got {type(self.strategy).__name__}."
            )


def validate_pairs(pairs: Any) -> list[TraderPair]:
    """
    Validate a Trader pair list and return it as a new ``list``.

    Rules: at least one entry; every entry a :class:`TraderPair`; the same
    ``(broker, symbol)`` never appears twice — brokers compared by object
    identity (reuse the same instance for one account), symbols
    case-insensitively.
    """
    entries = list(pairs)
    if not entries:
        raise ValueError("Trader needs at least one TraderPair.")
    seen: dict[tuple[int, str], int] = {}
    for idx, pair in enumerate(entries):
        if not isinstance(pair, TraderPair):
            raise TypeError(f"pairs[{idx}] must be a TraderPair, got {type(pair).__name__}.")
        key = (id(pair.broker), pair.config.symbol.lower())
        if key in seen:
            raise ValueError(
                f"pairs[{idx}]: duplicate (broker, symbol) — {pair.config.symbol!r} already "
                f"appears on the same broker at pairs[{seen[key]}]; the same (broker, symbol) "
                "may not be configured twice."
            )
        seen[key] = idx
    return entries


# ---------------------------------------------------------------------------
# TraderSettings — Trader-level constructor kwargs (apply to all pairs)
# ---------------------------------------------------------------------------

@dataclass
class TraderSettings:
    """
    Trader-level settings — passed as ``Trader`` constructor kwargs and
    applied to all pairs.  ``on_stop`` and ``kill_switch_file`` are live
; ``state_path`` journaling + restart reconciliation are live.

    Attributes
    ----------
    on_stop : str
        ``"keep"`` (default) — on shutdown positions stay open and the venue
        SL/TP keep protecting them (protective orders are not cancelled);
        ``"close_all"`` — market-close everything and cancel working orders
.
    state_path : str | None
        Journal file for persistence / restart reconciliation: open
        trades (signal, ``sl_history``, ladder cursor, ``peak_price``, venue
        order ids), the trade-id sequence and the signal-dedup keys are
        flushed atomically on every change; ``run()`` reconciles it against
        the venue before trading starts (a corrupt file raises).  ``None``
        (default) → ``./.atk_trader_state.json`` (resolved by the Trader).
        ``os.PathLike`` accepted, stored as ``str``.
    kill_switch_file : str | None
        Touching this file triggers a graceful shutdown — polled while
        ``run()`` blocks; the file is never auto-deleted, and one that
        already exists when ``run()`` starts raises (stale kill switch).
        ``None`` (default) disables the file kill switch.  ``os.PathLike``
        accepted, stored as ``str``.
    """

    on_stop: str = ON_STOP_KEEP
    state_path: str | None = None
    kill_switch_file: str | None = None

    def __post_init__(self) -> None:
        if self.on_stop not in (ON_STOP_KEEP, ON_STOP_CLOSE_ALL):
            raise ValueError(
                f"TraderSettings.on_stop must be '{ON_STOP_KEEP}' or "
                f"'{ON_STOP_CLOSE_ALL}', got {self.on_stop!r}."
            )
        for name in ("state_path", "kill_switch_file"):
            value = getattr(self, name)
            if value is None:
                continue
            try:
                setattr(self, name, os.fspath(value))
            except TypeError:
                raise ValueError(
                    f"TraderSettings.{name} must be a path (str / os.PathLike) or None, "
                    f"got {value!r}."
                ) from None
