"""
AlgoTradeKit.trader
~~~~~~~~~~~~~~~~~~~~
Live trading (v1.0.0) — order orchestration on real venues plus ``run_live``
paper trading. ships the configuration & types layer, the event
stream + terminal log, the ``run_live()`` paper-trading entry point::

    from AlgoTradeKit.trader import TraderConfig, TraderPair, run_live

Functions
    run_live        Paper trading: run any strategy on live market
                    data with **simulated** positions — order code is never
                    touched.  Same ``TraderConfig`` (and ``TraderPair`` list)
                    as the real-order ``Trader``; live chart + report
                    (``display=True``) and/or the ``[SIM]``-tagged per-event
                    terminal log (``log_events=True``).  With two or more
                    pairs and a displaying pair, one **combined report**
                    across all pairs is served too — merged trade
                    list, summed equity curve, per-pair breakdown, refreshed
                    every closed candle.  Also re-exported at the package
                    top level (``from AlgoTradeKit import run_live``).

Classes
    Trader          Real-order live trading: the per-pair worker loop —
                    feed → strategy → execution — in the configured execution
                    mode (candle_close / candle_update / tick), with
                    signal dedup and venue-side close detection.  ``run()``
                    blocks; safety rails end it gracefully — ``stop()``,
                    Ctrl+C / SIGTERM handlers, or the ``kill_switch_file`` —
                    and shutdown applies ``on_stop``: keep positions
                    protected by their venue-native SL/TP (default) or
                    market-close everything (``"close_all"``).
                    ``max_daily_loss`` halts new entries for the UTC day once
                    the pair's realized + unrealized loss reaches it
                    (``DAILY_LOSS`` event; optional flatten).  Multi-RR TP
                    ladders trade venue-native: reduce-only level
                    orders on Binance futures, feed-detected market partial
                    closes on MetaTrader.  State is journaled to
                    ``state_path`` on every change and reconciled against
                    the venue on restart: journaled positions are
                    adopted (trailing / ladder / risk-free state resumes,
                    lost venue protection re-armed), offline closes are
                    recorded from the venue trade history, foreign positions
                    are warned about and left untouched (``RECONCILE``
                    event).  Multi-pair / multi-venue:
                    ``Trader(pairs=[TraderPair, ...])`` runs one worker per
                    entry — brokers may repeat (pairs on one broker share
                    that account's wallet naturally), one shared
                    ``trader_id`` and journal, per-pair event streams and
                    daily-loss gates, and every kill switch / ``on_stop``
                    applies to all pairs.  ``config.display=True`` serves a
                    live chart + report beside the trading —
                    sim / real / both trade sources, printed URLs with
                    ``display_open_browser=False``, one combined report page
                    when two or more pairs display — all on a low-priority
                    background thread that never slows the trading loop.
    TraderConfig    Live-trading config — same field names as SimulateConfig
                    + trader-only groups (costs overrides, execution
                    mode, data/feed, event logging, display, safety);
                    ``to_simulate_config()`` derives the display config with
                    the venue's real costs.
    TraderPair      One (broker, config, strategy) entry for multi-pair mode
; the same (broker, symbol) never appears twice.
    TraderEvent + SignalEvent / OpenEvent / SlMoveEvent / RiskFreeEvent /
    TpLevelEvent / CloseEvent / ExitSignalEvent / DailyLossEvent /
    ReconcileEvent / ErrorEvent
                    Typed events — one frozen dataclass per
                    meaningful live-trading moment.
    EventStream     Publish/subscribe backbone; subscribers are the
                    extension point (notification backends in v1.1).
    TerminalEventPrinter
                    The v1.0.0 subscriber — one timestamped, grep-able line
                    per event; ``attach_terminal_printer(stream, config)``
                    wires it per ``log_events`` / ``log_event_types``.

Constants
    EXEC_CANDLE_CLOSE / EXEC_CANDLE_UPDATE / EXEC_TICK
        Execution modes — when the strategy is evaluated.
    DISPLAY_TRADES_SIM / DISPLAY_TRADES_REAL / DISPLAY_TRADES_BOTH
        What the live display shows.
    ON_STOP_KEEP / ON_STOP_CLOSE_ALL
        Shutdown policy for open positions.
    EVENT_SIGNAL / EVENT_EXIT_SIGNAL / EVENT_OPEN / EVENT_SL_MOVE /
    EVENT_RISK_FREE / EVENT_TP_LEVEL / EVENT_CLOSE / EVENT_DAILY_LOSS /
    EVENT_RECONCILE / EVENT_ERROR — event types (``ALL_EVENT_TYPES`` is
        the full set, the ``log_event_types`` domain).
    SOURCE_SIM / SOURCE_LIVE — event source tags (``[SIM]`` / ``[LIVE]``).
    CLOSE_REASON_MANUAL
        Close reason of a venue-side close the trader did not order (
        close detection): a manual close / reduction on the venue, or a fill
        whose user-data event was missed.
"""

from ._config import (
    DISPLAY_TRADES_BOTH,
    DISPLAY_TRADES_REAL,
    DISPLAY_TRADES_SIM,
    EXEC_CANDLE_CLOSE,
    EXEC_CANDLE_UPDATE,
    EXEC_TICK,
    ON_STOP_CLOSE_ALL,
    ON_STOP_KEEP,
    TraderConfig,
    TraderPair,
)
from ._events import (
    ALL_EVENT_TYPES,
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
    TraderEvent,
    attach_terminal_printer,
)
from ._run_live import run_live
from ._trader import CLOSE_REASON_MANUAL, Trader

__all__ = [
    "run_live",
    "Trader",
    "TraderConfig",
    "TraderPair",
    "CLOSE_REASON_MANUAL",
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
]
