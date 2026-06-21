"""
AlgoTradeKit.simulate._position
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Position types used internally by the simulation engine.

_InternalPosition
    Mutable object that tracks an open position through the simulation loop.
    Holds all live state: current SL, trailing peak, multi-RR progress, etc.
    Not exposed in the public API.

ClosedTrade
    Immutable, JSON-serialisable record of a completed trade.  This is what
    ends up in ``SimulateReport.closed_trades`` and is consumed by the report
    module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._config import SimulateConfig

# ---------------------------------------------------------------------------
# Close reason constants
# ---------------------------------------------------------------------------

CLOSE_REASON_SL = "sl"              # Stop-loss hit
CLOSE_REASON_TP = "tp"              # Take-profit hit (final level)
CLOSE_REASON_TP_PARTIAL = "tp_rr"  # Intermediate TP level hit (multi-RR)
CLOSE_REASON_RF = "rf"              # Risk-free / break-even SL hit
CLOSE_REASON_FC = "force_close"     # Force-closed by ExitSignal
CLOSE_REASON_EOD = "end_of_data"    # Position open at last candle — closed at market


# ---------------------------------------------------------------------------
# _InternalPosition  (mutable, private)
# ---------------------------------------------------------------------------

class _InternalPosition:
    """
    Live state of an open position inside the simulation engine.

    This is an internal class; users see only ``ClosedTrade`` in the report.

    Parameters
    ----------
    trade_id : int
        Sequential trade identifier assigned by the engine.
    symbol : str
        Instrument, e.g. ``"btcusdt"`` or ``"eurusd"``.
    direction : str
        ``"long"`` or ``"short"``.
    entry_price : float
        Actual fill price (close of signal candle ± spread).
    raw_entry_price : float
        Signal's ``entry_price`` before spread is applied.
    stop_loss : float
        Current stop-loss price (may trail or move on TP hit).
    initial_stop_loss : float
        Original SL price at entry — never changes.
    take_profit : float | None
        Current TP target.  ``None`` in ``"multi_rr"`` mode (uses ``next_tp``).
    margin_amount : float
        Capital locked as margin (deducted from wallet on open).
    risk_amount : float
        Dollar amount that would be lost if initial SL is hit.
    size : float
        Position size: base-asset units for exchange, lots for MT5.
        Shrinks over time if partial closes occur (multi-RR with
        ``tp_level_close_fractions``); see ``original_size``.
    original_size : float
        ``size`` at the moment the position was opened — never changes.
        Used to translate ``tp_level_close_fractions`` (fractions of the
        *original* size) into absolute amounts at each partial close.
    open_time : int
        Candle open-time in UTC ms.
    open_commission : float
        Commission charged at entry.
    sl_distance : float
        ``|entry_price - initial_stop_loss|`` — used in PnL and sizing calcs.
    pnl_per_price_unit : float
        ``risk_amount / sl_distance`` — scales price move to dollar PnL.
    peak_price : float
        Most favourable price seen since entry (for trailing SL).
    next_tp : float | None
        In ``"multi_rr"`` mode: the next TP level price to watch for.
    last_rr_hit : int
        How many TP levels have been hit so far (starts at 0).
    signal_metadata : dict
        Original signal metadata forwarded to the closed-trade record.
    signal_candle_index : int
        ``Signal.candle_index`` — used for report visual linking.
    """

    __slots__ = (
        "trade_id", "symbol", "direction",
        "entry_price", "raw_entry_price",
        "stop_loss", "initial_stop_loss",
        "take_profit",
        "margin_amount", "risk_amount", "size", "original_size",
        "open_time", "open_commission",
        "sl_distance", "pnl_per_price_unit",
        "peak_price",
        "next_tp", "last_rr_hit",
        "signal_metadata", "signal_candle_index",
        "tp_level_prices",          # list[float] — cached TP price levels (multi_rr)
        "risk_free_triggered",      # bool — break-even already activated?
        "_max_favourable",          # float — best unrealised PnL seen
        "_max_adverse",             # float — worst unrealised PnL seen (abs)
    )

    def __init__(
        self,
        trade_id: int,
        symbol: str,
        direction: str,
        entry_price: float,
        raw_entry_price: float,
        stop_loss: float,
        take_profit: float | None,
        margin_amount: float,
        risk_amount: float,
        size: float,
        open_time: int,
        open_commission: float,
        signal_metadata: dict[str, Any],
        signal_candle_index: int,
        tp_level_prices: list[float],
    ) -> None:
        self.trade_id = trade_id
        self.symbol = symbol
        self.direction = direction
        self.entry_price = entry_price
        self.raw_entry_price = raw_entry_price
        self.stop_loss = stop_loss
        self.initial_stop_loss = stop_loss
        self.take_profit = take_profit
        self.margin_amount = margin_amount
        self.risk_amount = risk_amount
        self.size = size
        self.original_size = size
        self.open_time = open_time
        self.open_commission = open_commission
        self.sl_distance = abs(entry_price - stop_loss)
        self.pnl_per_price_unit = risk_amount / self.sl_distance if self.sl_distance > 0 else 0.0
        self.peak_price = entry_price
        self.next_tp: float | None = tp_level_prices[0] if tp_level_prices else None
        self.last_rr_hit: int = 0
        self.signal_metadata = signal_metadata
        self.signal_candle_index = signal_candle_index
        self.tp_level_prices: list[float] = tp_level_prices
        self.risk_free_triggered: bool = False
        self._max_favourable: float = 0.0
        self._max_adverse: float = 0.0

    # ------------------------------------------------------------------
    # PnL helpers
    # ------------------------------------------------------------------

    def unrealised_pnl(self, price: float) -> float:
        """Unrealised PnL in account currency at *price*."""
        direction_factor = 1.0 if self.direction == "long" else -1.0
        return direction_factor * (price - self.entry_price) * self.pnl_per_price_unit

    def current_value(self, price: float) -> float:
        """Current value of the position: margin + unrealised PnL."""
        return self.margin_amount + self.unrealised_pnl(price)

    def update_excursion(self, price: float) -> None:
        """Track maximum favourable / adverse excursion in $ terms."""
        pnl = self.unrealised_pnl(price)
        if pnl > self._max_favourable:
            self._max_favourable = pnl
        if pnl < 0 and abs(pnl) > self._max_adverse:
            self._max_adverse = abs(pnl)

    def update_trailing_peak(self, price: float) -> None:
        """Update trailing peak for trailing SL mode."""
        if self.direction == "long" and price > self.peak_price:
            self.peak_price = price
        elif self.direction == "short" and price < self.peak_price:
            self.peak_price = price

    # ------------------------------------------------------------------
    # Repr
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"<_InternalPosition #{self.trade_id} {self.direction.upper()} "
            f"{self.symbol.upper()} entry={self.entry_price} sl={self.stop_loss}>"
        )


# ---------------------------------------------------------------------------
# ClosedTrade  (immutable, public)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ClosedTrade:
    """
    Immutable record of a completed trade.  Every closed trade in
    ``SimulateReport.closed_trades`` is one of these.

    Attributes
    ----------
    trade_id : int
        Sequential trade identifier.
    symbol : str
        Instrument.
    direction : str
        ``"long"`` or ``"short"``.
    open_time : int
        Entry candle open-time in UTC ms.
    close_time : int
        Exit candle open-time in UTC ms.
    entry_price : float
        Fill price at entry (after spread).
    exit_price : float
        Fill price at exit.
    initial_stop_loss : float
        SL price at time of entry.
    final_stop_loss : float
        SL price when the trade was closed (may have moved for trailing/RF).
    take_profit : float | None
        TP target at time of entry (may be ``None``).
    size : float
        Size realised by **this** close event: base-asset units (exchange)
        or lots (MT5).  Equal to the position's full size unless this is a
        partial close (see ``close_reason`` /
        ``SimulateConfig.tp_level_close_fractions``), in which case it is
        the fraction of the original size closed at this specific level —
        several ``ClosedTrade`` rows can share one ``trade_id`` when a
        position is scaled out of in pieces.
    margin_amount : float
        Capital that was locked as margin for **this** slice.
    risk_amount : float
        Dollar amount that was at risk for **this** slice (based on initial SL).
    gross_pnl : float
        PnL before commission.
    commission : float
        Commission charged for **this** slice (entry + exit, pre-allocated
        at entry — see ``SimulateConfig.commission``).
    net_pnl : float
        ``gross_pnl - commission``.
    pnl_r : float
        PnL expressed in R-multiples (net_pnl / risk_amount).
    close_reason : str
        One of: ``"sl"``, ``"tp"``, ``"tp_rr"``, ``"rf"``,
        ``"force_close"``, ``"end_of_data"``.  ``"tp_rr"`` marks a partial
        realisation at an intermediate multi-RR level (the position is
        still open afterwards with reduced size); ``"tp"`` always means
        nothing remains open after this event.
    rr_levels_hit : int
        Number of multi-RR TP levels hit before close (0 for non-multi-RR).
    max_favourable_excursion : float
        Best unrealised PnL (in $) seen during the trade.
    max_adverse_excursion : float
        Worst unrealised loss (in $, positive value) seen during the trade.
    leverage : float
        Leverage that was active for this trade.
    spread_paid : float
        Effective spread cost in account currency.
    signal_metadata : dict
        Original Signal metadata.
    signal_candle_index : int
        Signal candle position in the primary TF DataFrame.
    """

    trade_id: int
    symbol: str
    direction: str

    open_time: int          # UTC ms
    close_time: int         # UTC ms

    entry_price: float
    exit_price: float
    initial_stop_loss: float
    final_stop_loss: float
    take_profit: float | None

    size: float             # base-asset units or lots
    margin_amount: float
    risk_amount: float

    gross_pnl: float
    commission: float
    net_pnl: float
    pnl_r: float            # net_pnl / risk_amount

    close_reason: str
    rr_levels_hit: int      # multi-RR TP levels reached

    max_favourable_excursion: float
    max_adverse_excursion: float

    leverage: float
    spread_paid: float

    signal_metadata: dict[str, Any] = field(default_factory=dict)
    signal_candle_index: int = 0

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    @property
    def is_win(self) -> bool:
        """True if the trade was profitable (net_pnl > 0)."""
        return self.net_pnl > 0

    @property
    def is_loss(self) -> bool:
        """True if the trade resulted in a net loss."""
        return self.net_pnl < 0

    @property
    def duration_ms(self) -> int:
        """Trade duration in milliseconds."""
        return self.close_time - self.open_time

    @property
    def is_sl(self) -> bool:
        return self.close_reason == CLOSE_REASON_SL

    @property
    def is_tp(self) -> bool:
        return self.close_reason in (CLOSE_REASON_TP, CLOSE_REASON_TP_PARTIAL)

    @property
    def is_force_close(self) -> bool:
        return self.close_reason == CLOSE_REASON_FC

    @property
    def is_risk_free(self) -> bool:
        return self.close_reason == CLOSE_REASON_RF

    @property
    def is_end_of_data(self) -> bool:
        return self.close_reason == CLOSE_REASON_EOD

    def __repr__(self) -> str:
        sign = "+" if self.net_pnl >= 0 else ""
        return (
            f"<ClosedTrade #{self.trade_id} {self.direction.upper()} "
            f"{self.symbol.upper()} "
            f"net={sign}{self.net_pnl:.2f} reason={self.close_reason}>"
        )
