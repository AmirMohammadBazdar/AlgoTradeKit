"""
AlgoTradeKit.simulate._config
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
SimulateConfig — the single dataclass that controls every aspect of a
simulation run: cost model, position sizing, leverage, spread, TP/SL
behaviour, position limits, and report thresholds.

Two construction styles are supported::

    # Keyword arguments
    config = SimulateConfig(
        initial_balance=10_000,
        leverage=10,
        risk_per_trade=1.0,
        tp_mode="multi_rr",
        tp_levels=[1.0, 2.0, 3.0],
    )

    # Dict unpacking — useful for batch runs
    cfg = {"initial_balance": 10_000, "leverage": 10, "tp_mode": "fixed_rr", "tp_rr": 2.0}
    config = SimulateConfig(**cfg)
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# String-literal constants (used as default values and for validation)
# ---------------------------------------------------------------------------

#: Exchange type — affects PnL formula, margin model, and lot sizing.
EXCHANGE_TYPE_EXCHANGE = "exchange"    # Crypto/spot-with-leverage (Binance Futures, etc.)
EXCHANGE_TYPE_METATRADER = "metatrader"  # Forex/CFDs via MT5

#: How trading costs are charged per trade (round-trip).
COMMISSION_TYPE_PERCENTAGE = "percentage"   # % of notional value  (0.001 = 0.1 %)
COMMISSION_TYPE_PER_LOT = "per_lot"         # Fixed $ per lot       (MT5 style, e.g. $5)
COMMISSION_TYPE_FIXED = "fixed"             # Fixed $ per trade     (e.g. $1)

#: How position size is determined.
SIZING_RISK_PERCENT = "risk_percent"   # Size so that SL = risk_per_trade % of balance
SIZING_FIXED_AMOUNT = "fixed_amount"   # Always risk exactly fixed_amount $
SIZING_FIXED_LOT = "fixed_lot"         # Always use fixed_lot lots (MT5) or units

#: Take-profit mode.
TP_MODE_SIGNAL = "signal"    # Use take_profit from Signal; None means no TP
TP_MODE_FIXED_RR = "fixed_rr"  # TP = entry ± tp_rr × sl_distance
TP_MODE_MULTI_RR = "multi_rr"  # Multiple TP levels; SL trails to previous level
TP_MODE_NONE = "none"        # No TP — only SL / force-close

#: Stop-loss mode.
SL_MODE_SIGNAL = "signal"    # Use stop_loss from Signal as-is
SL_MODE_TRAILING = "trailing"  # Trail SL behind price as it moves favourably

#: Report rendering mode (v0.7.0).
REPORT_MODE_NONE    = "none"      # No report generated
REPORT_MODE_WEBPAGE = "webpage"   # Open interactive report in browser
REPORT_MODE_SAVE    = "save"      # Save report as standalone HTML file
REPORT_MODE_BOTH    = "both"      # Open in browser AND save HTML file


# ---------------------------------------------------------------------------
# SimulateConfig
# ---------------------------------------------------------------------------

@dataclass
class SimulateConfig:
    """
    Full configuration for one simulation run.

    Attributes
    ----------
    initial_balance : float
        Starting wallet balance in account currency (default: 10 000).
    symbol : str
        Trading instrument, e.g. ``"btcusdt"``, ``"eurusd"``, ``"xauusd"``.
        Required for MT5 lot-size calculation; ignored for exchange sizing.
    exchange_type : str
        ``"exchange"`` (crypto / futures) or ``"metatrader"`` (Forex / CFDs).
    leverage : float
        Leverage multiplier (default: 1.0 = no leverage).  Must be > 0.
    spread : float
        Bid-ask spread added to entry on buy and subtracted on sell, in price
        units (default: 0.0).  E.g. 0.003 = $0.003 per unit.
    commission_type : str
        How trading costs are charged: ``"percentage"``, ``"per_lot"``, or
        ``"fixed"`` (default: ``"percentage"``).
    commission : float
        Commission rate or amount (default: 0.001).
        - percentage: 0.001 = 0.1 % of notional value per side (0.2 % round-trip)
        - per_lot:    dollar amount per lot (e.g. 5 for $5/lot MT5 style)
        - fixed:      flat dollar per trade (e.g. 1.0)
    position_sizing : str
        How to calculate position size: ``"risk_percent"``, ``"fixed_amount"``,
        or ``"fixed_lot"`` (default: ``"risk_percent"``).
    risk_per_trade : float
        For ``"risk_percent"`` sizing: percentage of balance to risk on each
        trade (default: 1.0).
    fixed_amount : float
        For ``"fixed_amount"`` sizing: dollar amount to risk per trade.
    fixed_lot : float
        For ``"fixed_lot"`` sizing: lot/unit size to use every trade.
    compound : bool
        ``True`` — use current wallet balance for sizing (compounding).
        ``False`` — always size against ``initial_balance`` (default).
    max_long_positions : int
        Maximum simultaneous long positions (default: 1).
    max_short_positions : int
        Maximum simultaneous short positions (default: 1).
    max_positions : int
        Maximum total simultaneous positions across all directions (default: 1).
    tp_mode : str
        Take-profit mode: ``"signal"``, ``"fixed_rr"``, ``"multi_rr"``, or
        ``"none"`` (default: ``"signal"``).
    tp_rr : float
        For ``"fixed_rr"`` mode: risk-reward ratio (default: 2.0).
        TP = entry ± tp_rr × sl_distance.
    tp_levels : list[float]
        For ``"multi_rr"`` mode: list of R-multiples at which TP levels sit
        (default: ``[1.0, 2.0, 3.0]``).  After each level is hit, SL moves
        to the previous level (0.0 = entry, 1.0 = first TP, etc.).
    tp_level_close_fractions : list[float] | None
        For ``"multi_rr"`` mode only.  Optional list, same length as
        ``tp_levels``, giving the fraction of the position's **original**
        size to actually realise (partially close) when each corresponding
        level is reached.  Each value must be in ``[0.0, 1.0]`` and the
        values must sum to ``<= 1.0``.

        ``None`` (default) preserves the exact pre-v0.7.3 behaviour: no
        level closes anything — the position's SL simply walks forward
        through the levels, and only the *final* level fully closes the
        position.  This default is unchanged for full backward compatibility.

        When provided, every level (including the last) closes **only**
        the fraction you specify — there is no automatic "close everything"
        special-case for the final level anymore.  Two common patterns:

        * **Scale out completely** — fractions summing to ``1.0``
          (e.g. ``[1/3, 1/3, 1/3]`` for ``tp_levels=[1, 2, 3]``) realises
          profit progressively and leaves nothing open after the last level.
        * **Trail forever, never auto-realise** — all-zero fractions
          (e.g. ``[0.0] * 20`` for ``tp_levels=[1, 2, ..., 20]``) never
          partially closes anything; the SL just keeps stepping up through
          every level and the full size stays open, trailing at the last
          level's price, until it is eventually stopped out, force-closed,
          or hits end-of-data.

        Each partial close produces its own ``ClosedTrade`` (sharing the
        same ``trade_id`` as the rest of that position's closes), so
        ``SimulateReport`` statistics count each realised slice separately
        — matching how a real scaled-out position would show up on a
        broker statement.
    sl_mode : str
        Stop-loss mode: ``"signal"`` (use SL from Signal) or ``"trailing"``
        (default: ``"signal"``).
    trailing_sl_percent : float
        For ``"trailing"`` SL mode: percentage distance from the peak price
        at which the trailing SL is placed (default: 1.0).
    risk_free_enabled : bool
        When ``True``, the SL is moved to the entry price once the position
        reaches ``risk_free_at_rr × sl_distance`` in profit.  Superseded by
        ``"multi_rr"`` TP mode which handles break-even internally.
    risk_free_at_rr : float
        The R-multiple at which break-even is activated (default: 1.0).
    force_close_on_exit_signal : bool
        When ``True``, positions are closed when the strategy emits an
        ``ExitSignal`` at the same candle (default: ``False``).
    drawdown_threshold : float
        Drawdowns equal to or above this percentage of the peak balance are
        included in ``SimulateReport.significant_drawdowns`` (default: 5.0).
    primary_timeframe : str
        The timeframe key inside ``StrategyResult.data`` to iterate over for
        OHLCV candle access (default: ``"1h"``).
    config_id : str
        Human-readable identifier for this config.  Auto-generated from key
        parameters if left empty.
    show_chart : bool
        When ``True``, open an interactive candle chart after the simulation
        completes.  The chart shows the OHLCV data with position boxes for
        every trade and any strategy drawings (default: ``False``).
    report_mode : str
        Controls whether and how the simulation report is rendered after run:

        * ``"none"``    — no report (default).
        * ``"webpage"`` — open an interactive report in the default browser.
        * ``"save"``    — save a standalone ``report.html`` file on disk.
        * ``"both"``    — open in browser **and** save to disk.
    report_save_path : str
        File path for the saved HTML report (used when *report_mode* is
        ``"save"`` or ``"both"``).  Default: ``"report.html"`` in the
        current working directory.
    """

    # ------------------------------------------------------------------
    # Wallet / Instrument
    # ------------------------------------------------------------------
    initial_balance: float = 10_000.0
    symbol: str = ""
    exchange_type: str = EXCHANGE_TYPE_EXCHANGE
    leverage: float = 1.0

    # ------------------------------------------------------------------
    # Trading costs
    # ------------------------------------------------------------------
    spread: float = 0.0                     # price units (e.g. 0.003 = $0.003)
    commission_type: str = COMMISSION_TYPE_PERCENTAGE
    commission: float = 0.001               # 0.1 % per side for exchange default

    # ------------------------------------------------------------------
    # Position sizing
    # ------------------------------------------------------------------
    position_sizing: str = SIZING_RISK_PERCENT
    risk_per_trade: float = 1.0             # % of balance when sizing = risk_percent
    fixed_amount: float = 100.0             # $ at risk when sizing = fixed_amount
    fixed_lot: float = 0.01                 # lot/unit size when sizing = fixed_lot
    compound: bool = False                  # True → size off current balance

    # ------------------------------------------------------------------
    # Position limits
    # ------------------------------------------------------------------
    max_long_positions: int = 1
    max_short_positions: int = 1
    max_positions: int = 1

    # ------------------------------------------------------------------
    # Take-profit
    # ------------------------------------------------------------------
    tp_mode: str = TP_MODE_SIGNAL
    tp_rr: float = 2.0
    tp_levels: list[float] = field(default_factory=lambda: [1.0, 2.0, 3.0])
    tp_level_close_fractions: list[float] | None = None

    # ------------------------------------------------------------------
    # Stop-loss
    # ------------------------------------------------------------------
    sl_mode: str = SL_MODE_SIGNAL
    trailing_sl_percent: float = 1.0        # % from peak price for trailing mode

    # ------------------------------------------------------------------
    # Risk-free / break-even
    # ------------------------------------------------------------------
    risk_free_enabled: bool = False
    risk_free_at_rr: float = 1.0

    # ------------------------------------------------------------------
    # Exit signals
    # ------------------------------------------------------------------
    force_close_on_exit_signal: bool = False

    # ------------------------------------------------------------------
    # Report / display options
    # ------------------------------------------------------------------
    drawdown_threshold: float = 5.0         # report drawdowns >= this % of peak
    primary_timeframe: str = "1h"

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------
    config_id: str = ""

    # ------------------------------------------------------------------
    # Visualisation / reporting (v0.7.0)
    # ------------------------------------------------------------------
    show_chart: bool = False
    """Open an interactive candle chart after the simulation (default: False)."""

    report_mode: str = REPORT_MODE_NONE
    """Report rendering mode: 'none' | 'webpage' | 'save' | 'both'."""

    report_save_path: str = "report.html"
    """Destination file path when report_mode is 'save' or 'both'."""

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def __post_init__(self) -> None:
        if self.leverage <= 0:
            raise ValueError("SimulateConfig.leverage must be > 0.")
        if not (0.0 <= self.risk_per_trade <= 100.0):
            raise ValueError("SimulateConfig.risk_per_trade must be in [0, 100].")
        if self.max_positions < 1:
            raise ValueError("SimulateConfig.max_positions must be >= 1.")
        if self.max_long_positions < 0 or self.max_short_positions < 0:
            raise ValueError(
                "SimulateConfig.max_long_positions and max_short_positions must be >= 0."
            )
        if self.exchange_type not in (EXCHANGE_TYPE_EXCHANGE, EXCHANGE_TYPE_METATRADER):
            raise ValueError(
                f"SimulateConfig.exchange_type must be "
                f"'{EXCHANGE_TYPE_EXCHANGE}' or '{EXCHANGE_TYPE_METATRADER}', "
                f"got {self.exchange_type!r}."
            )
        if self.commission_type not in (
            COMMISSION_TYPE_PERCENTAGE, COMMISSION_TYPE_PER_LOT, COMMISSION_TYPE_FIXED
        ):
            raise ValueError(
                f"SimulateConfig.commission_type must be one of "
                f"'percentage', 'per_lot', 'fixed', got {self.commission_type!r}."
            )
        if self.position_sizing not in (
            SIZING_RISK_PERCENT, SIZING_FIXED_AMOUNT, SIZING_FIXED_LOT
        ):
            raise ValueError(
                f"SimulateConfig.position_sizing must be one of "
                f"'risk_percent', 'fixed_amount', 'fixed_lot', got {self.position_sizing!r}."
            )
        if self.tp_mode not in (
            TP_MODE_SIGNAL, TP_MODE_FIXED_RR, TP_MODE_MULTI_RR, TP_MODE_NONE
        ):
            raise ValueError(
                f"SimulateConfig.tp_mode must be one of "
                f"'signal', 'fixed_rr', 'multi_rr', 'none', got {self.tp_mode!r}."
            )
        if self.sl_mode not in (SL_MODE_SIGNAL, SL_MODE_TRAILING):
            raise ValueError(
                f"SimulateConfig.sl_mode must be 'signal' or 'trailing', "
                f"got {self.sl_mode!r}."
            )
        if not self.tp_levels and self.tp_mode == TP_MODE_MULTI_RR:
            raise ValueError(
                "SimulateConfig.tp_levels must be a non-empty list when tp_mode='multi_rr'."
            )
        if self.tp_level_close_fractions is not None:
            if self.tp_mode != TP_MODE_MULTI_RR:
                raise ValueError(
                    "SimulateConfig.tp_level_close_fractions requires tp_mode='multi_rr'."
                )
            if len(self.tp_level_close_fractions) != len(self.tp_levels):
                raise ValueError(
                    "SimulateConfig.tp_level_close_fractions must be the same length as "
                    f"tp_levels ({len(self.tp_levels)}), got "
                    f"{len(self.tp_level_close_fractions)}."
                )
            if any(not (0.0 <= f <= 1.0) for f in self.tp_level_close_fractions):
                raise ValueError(
                    "SimulateConfig.tp_level_close_fractions values must all be in [0.0, 1.0]."
                )
            if sum(self.tp_level_close_fractions) > 1.0 + 1e-9:
                raise ValueError(
                    "SimulateConfig.tp_level_close_fractions must sum to <= 1.0, got "
                    f"{sum(self.tp_level_close_fractions)!r}."
                )
        if not self.config_id:
            self.config_id = self._auto_id()
        if self.report_mode not in (
            REPORT_MODE_NONE, REPORT_MODE_WEBPAGE, REPORT_MODE_SAVE, REPORT_MODE_BOTH
        ):
            raise ValueError(
                f"SimulateConfig.report_mode must be one of "
                f"'none', 'webpage', 'save', 'both', got {self.report_mode!r}."
            )

    def _auto_id(self) -> str:
        """Generate a readable identifier from key parameters."""
        parts: list[str] = []
        if self.symbol:
            parts.append(self.symbol.upper())
        parts.append(f"risk{self.risk_per_trade}pct")
        parts.append(f"lev{int(self.leverage)}x")
        parts.append(f"tp_{self.tp_mode}")
        if self.tp_mode == TP_MODE_FIXED_RR:
            parts.append(f"rr{self.tp_rr}")
        elif self.tp_mode == TP_MODE_MULTI_RR:
            levels = "_".join(str(lvl) for lvl in self.tp_levels)
            parts.append(f"[{levels}]")
        if self.compound:
            parts.append("compound")
        return "_".join(parts)

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def is_exchange(self) -> bool:
        """True when exchange_type is 'exchange'."""
        return self.exchange_type == EXCHANGE_TYPE_EXCHANGE

    def is_metatrader(self) -> bool:
        """True when exchange_type is 'metatrader'."""
        return self.exchange_type == EXCHANGE_TYPE_METATRADER

    def __repr__(self) -> str:
        return (
            f"<SimulateConfig id={self.config_id!r} "
            f"balance={self.initial_balance} lev={self.leverage}x "
            f"risk={self.risk_per_trade}% tp={self.tp_mode} sl={self.sl_mode}>"
        )
