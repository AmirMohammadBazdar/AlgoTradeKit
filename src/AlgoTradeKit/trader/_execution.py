"""
AlgoTradeKit.trader._execution
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Execution core for the v1.0.0 live trader: real order placement and
**venue-native** position protection, per venue.

Design
-----------
* Entries are **market orders** — signals are market-momented; the signal's
  ``entry_price`` (the sim's reference) drives sizing and SL/TP price
  computation, the real fill may differ by slippage and is **recorded**
  (``LiveTrade.fill_price`` / ``.slippage``); the position state is then
  seeded with the real fill so break-even and RR thresholds protect the
  actual entry.
* The SL is **always attached venue-native at creation**; a TP is attached
  only when the mode defines one (``tp_mode="signal"`` → the signal's TP,
  ``"fixed_rr"`` → the computed target).  Trailing / risk-free / multi-RR
  flows send **no** TP price (the ladder places its own reduce-only
  orders per level).
* SL moves (trailing per closed candle, risk-free jump to break-even) are
  computed with the **shared position maths** of ``simulate._position_math``
  on a real ``_InternalPosition`` — live SL values equal simulate SL
  values by construction — then the **venue SL is modified**.  No soft SL
  exists anywhere: if the process dies, the venue SL still protects the
  position.
* **Multi-RR ladders ** — ``tp_mode="multi_rr"`` live, per venue:
  Binance futures places one reduce-only **limit** order per level with the
  fraction's quantity (fully venue-native; fills arrive via the user-data
  stream → :meth:`ExecutionEngine.record_ladder_fill`); MetaTrader levels are
  **feed-detected** (:meth:`ExecutionEngine.check_tp_levels`) and executed as
  partial-close **market** orders the moment a level price is reached, the
  venue SL moving per the simulate ladder logic (shared maths) either way.
  ``tp_level_close_fractions=None`` mirrors the sim exactly: intermediate
  levels are SL-advance-only (feed-detected, no venue order), the final level
  closes 100 % (futures: one full-size reduce-only limit; MT5: full market
  close on touch).  Binance **spot** cannot host a venue-native ladder (the
  holding is locked by its protective stop) — ``multi_rr`` is rejected there.
* Every operation emits its event (``OPEN`` / ``SL_MOVE`` / ``RISK_FREE``
  / ``TP_LEVEL`` / ``CLOSE`` / ``ERROR``), tagged ``SOURCE_LIVE``.

Per-venue mechanics (``broker.market`` picks the path)
------------------------------------------------------
* **Binance futures** — leverage set via ``set_leverage`` on :meth:`start`;
  SL = reduce-only STOP_MARKET order, TP = reduce-only TAKE_PROFIT_MARKET
  order, both quantity-scoped to the trade.  A SL move places the **new**
  stop first and cancels the old one second (reduce-only stops lock no
  funds), so the position is never unprotected.
* **Binance spot** — longs only (short signals raise), no leverage
  (``TraderConfig.leverage`` must be 1).  Protection = an OCO order list
  (SL + TP) or a single STOP_LOSS order (SL only).  The held asset is locked
  by the protective orders, so a SL move must cancel-then-replace; on
  replacement failure the old stop is restored, and if that fails too the
  position is emergency-closed at market (never left unprotected).
* **MetaTrader** — SL/TP travel inside the order request (atomic, no naked
  window); moves via ``modify_position`` on the position ticket; per-ticket
  closes.  The ``client_order_id`` rides the MT5 *comment* field.

``client_order_id`` = ``atk-<trader_id>-<PAIR>-<signal_ts>`` ties venue
orders to signals for reconciliation and the real-fills display.
``signal_ts`` is UTC ms; on MetaTrader it is UTC **seconds** — the comment
field caps at ~31 characters.  Protective orders suffix ``-sl`` / ``-tp`` /
``-x`` (spot OCO list id).

Division of labour: drives this engine (feed, strategy evaluation,
signal dedup, venue-side close detection — including routing ladder fills
and feed touches into the methods here); (``trader._state``)
journals every tracked trade and, on restart, re-adopts journaled positions
via :meth:`ExecutionEngine.adopt_trade` / :meth:`resync_stop` /
:meth:`rearm_take_profit` / :meth:`place_ladder_level`.  A failed
entry emits ``ERROR`` and returns ``None``; a failed SL modify emits
``ERROR`` and **reverts** the internal SL (and its ``sl_history`` record) so
the next closed candle retries — except right after a ladder fill, where the
realized close cannot be reverted: the internal state stays advanced and the
venue modify is retried every closed candle (``pending_sl_sync``); an entry
whose SL cannot be attached is **emergency-closed** — an unprotected
position must not stand.  A ladder order that cannot be placed degrades
that level to feed detection (market execution on touch) with an ``ERROR``.
"""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from ..broker import (
    MARKET_FOREX,
    MARKET_FUTURES,
    MARKET_SPOT,
    ORDER_LIMIT,
    ORDER_MARKET,
    ORDER_STOP,
    ORDER_TAKE_PROFIT,
    SIDE_BUY,
    SIDE_SELL,
    STATUS_REJECTED,
    BrokerError,
    OrderError,
    OrderResult,
)
from ..broker._timeutil import now_ms
from ..simulate import (
    CLOSE_REASON_FC,
    CLOSE_REASON_TP,
    CLOSE_REASON_TP_PARTIAL,
    EXCHANGE_TYPE_METATRADER,
    TP_MODE_FIXED_RR,
    TP_MODE_MULTI_RR,
    TP_MODE_SIGNAL,
    ClosedTrade,
)
from ..simulate._position import _InternalPosition
from ..simulate._position_math import (
    SIZE_EPSILON,
    advance_multi_rr,
    apply_partial_close,
    can_open_position,
    check_risk_free,
    compute_position_params,
    make_closed_trade,
    update_trailing_sl,
)
from ..strategy import Signal
from ._config import TraderConfig
from ._events import (
    SOURCE_LIVE,
    CloseEvent,
    ErrorEvent,
    EventStream,
    OpenEvent,
    RiskFreeEvent,
    SlMoveEvent,
    TpLevelEvent,
)

#: Quote assets recognised when detecting a spot symbol's quote currency for
#: balance-based sizing (longest suffix wins); override with
#: ``ExecutionEngine(quote_asset=...)`` for anything unusual.
_KNOWN_QUOTE_ASSETS = (
    "USDT", "USDC", "FDUSD", "TUSD", "BUSD", "USDP", "DAI",
    "BTC", "ETH", "BNB", "SOL", "DOGE",
    "EUR", "GBP", "TRY", "BRL", "JPY", "ARS", "MXN", "PLN", "RON", "UAH", "ZAR",
)


def _detect_quote_asset(symbol: str) -> str:
    """Longest-suffix match of *symbol* against the known quote assets."""
    s = symbol.upper()
    for quote in sorted(_KNOWN_QUOTE_ASSETS, key=len, reverse=True):
        if s.endswith(quote) and len(s) > len(quote):
            return quote
    raise ValueError(
        f"Cannot detect the quote asset of spot symbol {symbol!r} — pass "
        "ExecutionEngine(quote_asset=...) explicitly."
    )


def _ensure_accepted(result: OrderResult, what: str) -> OrderResult:
    """Raise :class:`OrderError` when the venue rejected *what*."""
    if result.status == STATUS_REJECTED:
        raise OrderError(f"{what} rejected by the venue: {result.raw!r}")
    return result


# ---------------------------------------------------------------------------
# Live trade record
# ---------------------------------------------------------------------------

@dataclass
class LiveTrade:
    """
    One live position under this engine's management.

    ``pos`` is a real simulate ``_InternalPosition`` — the single source of
    truth for the SL state machine (current SL, trailing peak, ``sl_history``,
    multi-RR ladder position), advanced by the exact maths the backtest
    uses.  The other fields are the venue handles and the slippage record.
    """

    pos: _InternalPosition
    signal: Signal
    client_order_id: str
    entry_order_id: str
    reference_price: float          # sim-reference fill (signal entry ± config spread)
    fill_price: float               # real venue fill (reference when the venue reports none)
    slippage: float                 # fill_price - reference_price, signed
    venue_tp: float | None = None   # TP price sent to the venue (None = no TP sent)
    sl_order_id: str | None = None          # Binance stop order (futures; spot without TP)
    tp_order_id: str | None = None          # Binance futures take-profit order
    oco_order_list_id: str | None = None    # Binance spot OCO list (SL + TP)
    mt5_ticket: str | None = None           # MetaTrader position ticket
    # multi-RR ladder state
    ladder_order_ids: dict[str, int] = field(default_factory=dict)  # order_id -> level idx
    pending_sl_sync: bool = False   # venue SL modify failed after a ladder fill — retrying


@dataclass
class _OpenOutcome:
    """What a venue's ``open()`` produced: the entry fill + protection handles."""

    entry: OrderResult
    sl_order_id: str | None = None
    tp_order_id: str | None = None
    oco_order_list_id: str | None = None
    mt5_ticket: str | None = None
    tp_error: str | None = None     # TP attach failed (position kept — SL is armed)


class _ProtectionFailure(Exception):
    """Entry filled but the venue SL could not be attached; the naked entry
    was emergency-closed (or the close itself failed — see the message)."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = details or {}


class _EmergencyClosed(Exception):
    """A spot SL replacement could not restore protection — the position was
    emergency-closed at market.  ``result`` is the close order's outcome."""

    def __init__(self, message: str, result: OrderResult) -> None:
        super().__init__(message)
        self.result = result


# ---------------------------------------------------------------------------
# Per-venue order mechanics
# ---------------------------------------------------------------------------

class _FuturesVenue:
    """Binance USD-M futures: quantity-scoped reduce-only trigger orders."""

    def __init__(self, broker: Any, symbol: str) -> None:
        self.broker = broker
        self.symbol = symbol

    @staticmethod
    def _exit_side(direction: str) -> str:
        return SIDE_SELL if direction == "long" else SIDE_BUY

    def open(
        self,
        direction: str,
        size: float,
        stop_loss: float,
        take_profit: float | None,
        client_order_id: str,
    ) -> _OpenOutcome:
        side = SIDE_BUY if direction == "long" else SIDE_SELL
        entry = _ensure_accepted(
            self.broker.create_order(
                self.symbol, side, size, type=ORDER_MARKET, client_order_id=client_order_id
            ),
            "entry order",
        )
        exit_side = self._exit_side(direction)
        try:
            sl_res = _ensure_accepted(
                self.broker.create_order(
                    self.symbol, exit_side, size, type=ORDER_STOP, stop_price=stop_loss,
                    reduce_only=True, client_order_id=client_order_id + "-sl",
                ),
                "stop-loss order",
            )
        except BrokerError as exc:
            details: dict[str, Any] = {"entry_order_id": entry.order_id}
            try:
                self.broker.create_order(
                    self.symbol, exit_side, size, type=ORDER_MARKET, reduce_only=True
                )
                message = (
                    f"stop-loss could not be attached ({exc}); the naked entry was "
                    "emergency-closed at market."
                )
                details["emergency_close"] = "done"
            except BrokerError as close_exc:
                message = (
                    f"stop-loss could not be attached ({exc}) AND the emergency close "
                    f"failed ({close_exc}) — the position may be open and UNPROTECTED; "
                    "intervene manually."
                )
                details["emergency_close"] = f"failed: {close_exc}"
            raise _ProtectionFailure(message, details) from exc

        tp_id: str | None = None
        tp_error: str | None = None
        if take_profit is not None:
            try:
                tp_res = _ensure_accepted(
                    self.broker.create_order(
                        self.symbol, exit_side, size, type=ORDER_TAKE_PROFIT,
                        stop_price=take_profit, reduce_only=True,
                        client_order_id=client_order_id + "-tp",
                    ),
                    "take-profit order",
                )
                tp_id = tp_res.order_id
            except BrokerError as exc:
                tp_error = (
                    f"take-profit could not be attached ({exc}); the position stays "
                    "open with its stop-loss armed."
                )
        return _OpenOutcome(
            entry=entry, sl_order_id=sl_res.order_id, tp_order_id=tp_id, tp_error=tp_error
        )

    def move_stop(self, trade: LiveTrade, new_sl: float, old_sl: float | None = None) -> list[str]:
        # New stop first, old cancel second — reduce-only stops lock no funds,
        # so the position is never unprotected in between.
        exit_side = self._exit_side(trade.pos.direction)
        new_res = _ensure_accepted(
            self.broker.create_order(
                self.symbol, exit_side, trade.pos.size, type=ORDER_STOP,
                stop_price=new_sl, reduce_only=True,
            ),
            "replacement stop-loss order",
        )
        old_id, trade.sl_order_id = trade.sl_order_id, new_res.order_id
        warnings: list[str] = []
        if old_id:
            try:
                self.broker.cancel_order(old_id, self.symbol)
            except BrokerError as exc:
                warnings.append(
                    f"old stop order {old_id} could not be cancelled ({exc}); the new "
                    f"stop {new_res.order_id} is armed."
                )
        return warnings

    def place_ladder_order(
        self, direction: str, quantity: float, price: float, client_order_id: str
    ) -> OrderResult:
        # multi-RR level: reduce-only limit at the level price.
        return _ensure_accepted(
            self.broker.create_order(
                self.symbol, self._exit_side(direction), quantity, type=ORDER_LIMIT,
                price=price, reduce_only=True, client_order_id=client_order_id,
            ),
            "ladder take-profit order",
        )

    def partial_close(self, trade: LiveTrade, quantity: float) -> OrderResult:
        # feed-detected level (a degraded ladder order): reduce-only market.
        return _ensure_accepted(
            self.broker.create_order(
                self.symbol, self._exit_side(trade.pos.direction), quantity,
                type=ORDER_MARKET, reduce_only=True,
            ),
            "ladder partial-close order",
        )

    def place_take_profit(
        self, trade: LiveTrade, price: float, client_order_id: str | None = None
    ) -> OrderResult:
        # adoption re-arm: re-place a vanished plain TP order.
        return _ensure_accepted(
            self.broker.create_order(
                self.symbol, self._exit_side(trade.pos.direction), trade.pos.size,
                type=ORDER_TAKE_PROFIT, stop_price=price, reduce_only=True,
                client_order_id=client_order_id,
            ),
            "take-profit order",
        )

    def close(self, trade: LiveTrade) -> tuple[OrderResult, list[str]]:
        exit_side = self._exit_side(trade.pos.direction)
        result = _ensure_accepted(
            self.broker.create_order(
                self.symbol, exit_side, trade.pos.size, type=ORDER_MARKET, reduce_only=True
            ),
            "close order",
        )
        warnings: list[str] = []
        protective = [("stop-loss", trade.sl_order_id), ("take-profit", trade.tp_order_id)]
        protective += [
            (f"ladder level {idx + 1}", oid) for oid, idx in trade.ladder_order_ids.items()
        ]
        for label, order_id in protective:
            if not order_id:
                continue
            try:
                self.broker.cancel_order(order_id, self.symbol)
            except BrokerError as exc:
                warnings.append(
                    f"{label} order {order_id} could not be cancelled after close ({exc})."
                )
        return result, warnings


class _SpotVenue:
    """Binance spot: OCO (SL+TP) or a single stop order; funds are locked by
    the working protection, so replacement is cancel-then-place."""

    def __init__(self, broker: Any, symbol: str) -> None:
        self.broker = broker
        self.symbol = symbol

    def open(
        self,
        direction: str,
        size: float,
        stop_loss: float,
        take_profit: float | None,
        client_order_id: str,
    ) -> _OpenOutcome:
        # Short signals were rejected by the engine before reaching the venue.
        entry = _ensure_accepted(
            self.broker.create_order(
                self.symbol, SIDE_BUY, size, type=ORDER_MARKET,
                client_order_id=client_order_id,
            ),
            "entry order",
        )
        quantity = float(entry.filled_quantity or 0.0) or size
        try:
            sl_id, oco_id = self._place_protection(
                quantity, stop_loss, take_profit, client_order_id
            )
        except BrokerError as exc:
            details: dict[str, Any] = {"entry_order_id": entry.order_id}
            try:
                self.broker.create_order(self.symbol, SIDE_SELL, quantity, type=ORDER_MARKET)
                message = (
                    f"protection could not be attached ({exc}); the naked entry was "
                    "emergency-sold at market."
                )
                details["emergency_close"] = "done"
            except BrokerError as close_exc:
                message = (
                    f"protection could not be attached ({exc}) AND the emergency sell "
                    f"failed ({close_exc}) — the holding may be UNPROTECTED; intervene "
                    "manually."
                )
                details["emergency_close"] = f"failed: {close_exc}"
            raise _ProtectionFailure(message, details) from exc
        return _OpenOutcome(entry=entry, sl_order_id=sl_id, oco_order_list_id=oco_id)

    def _place_protection(
        self,
        quantity: float,
        stop_loss: float,
        take_profit: float | None,
        client_order_id: str | None = None,
    ) -> tuple[str | None, str | None]:
        """Place the exit protection; returns ``(sl_order_id, oco_list_id)``."""
        if take_profit is not None:
            res = _ensure_accepted(
                self.broker.create_oco_order(
                    self.symbol, SIDE_SELL, quantity,
                    take_profit_price=take_profit, stop_price=stop_loss,
                    client_order_id=(client_order_id + "-x") if client_order_id else None,
                ),
                "OCO order",
            )
            return None, res.order_id
        res = _ensure_accepted(
            self.broker.create_order(
                self.symbol, SIDE_SELL, quantity, type=ORDER_STOP, stop_price=stop_loss,
                client_order_id=(client_order_id + "-sl") if client_order_id else None,
            ),
            "stop-loss order",
        )
        return res.order_id, None

    def _cancel_protection(self, trade: LiveTrade) -> None:
        if trade.oco_order_list_id:
            self.broker.cancel_order_list(self.symbol, trade.oco_order_list_id)
            trade.oco_order_list_id = None
        elif trade.sl_order_id:
            self.broker.cancel_order(trade.sl_order_id, self.symbol)
            trade.sl_order_id = None

    def move_stop(self, trade: LiveTrade, new_sl: float, old_sl: float | None = None) -> list[str]:
        # The working protection locks the asset — cancel first.  Failure here
        # leaves the old stop armed; the engine reverts the internal SL.
        self._cancel_protection(trade)
        quantity = trade.pos.size
        try:
            sl_id, oco_id = self._place_protection(quantity, new_sl, trade.venue_tp)
        except BrokerError as exc:
            if old_sl is not None:
                try:
                    sl_id, oco_id = self._place_protection(quantity, old_sl, trade.venue_tp)
                    trade.sl_order_id, trade.oco_order_list_id = sl_id, oco_id
                except BrokerError:
                    pass
                else:
                    raise OrderError(
                        f"replacement protection failed ({exc}); the previous stop at "
                        f"{old_sl} was restored."
                    ) from exc
            try:
                close_res = _ensure_accepted(
                    self.broker.create_order(
                        self.symbol, SIDE_SELL, quantity, type=ORDER_MARKET
                    ),
                    "emergency close order",
                )
            except BrokerError as close_exc:
                raise OrderError(
                    f"replacement protection failed ({exc}), the old stop could not be "
                    f"restored, and the emergency sell failed too ({close_exc}) — the "
                    "holding is UNPROTECTED; protection will be retried next candle."
                ) from close_exc
            raise _EmergencyClosed(
                f"replacement protection failed ({exc}) and the old stop could not be "
                "restored — the holding was emergency-sold at market (a position is "
                "never left unprotected).",
                close_res,
            ) from exc
        trade.sl_order_id, trade.oco_order_list_id = sl_id, oco_id
        return []

    def close(self, trade: LiveTrade) -> tuple[OrderResult, list[str]]:
        warnings: list[str] = []
        try:
            self._cancel_protection(trade)   # unlock the asset before selling
        except BrokerError as exc:
            warnings.append(
                f"protective orders could not be cancelled before close ({exc}); the "
                "market sell may be rejected while funds stay locked."
            )
        result = _ensure_accepted(
            self.broker.create_order(self.symbol, SIDE_SELL, trade.pos.size, type=ORDER_MARKET),
            "close order",
        )
        return result, warnings


class _MetaTraderVenue:
    """MetaTrader: SL/TP inside the order request; ``modify_position`` moves;
    per-ticket closes.  ``client_order_id`` rides the comment field."""

    def __init__(self, broker: Any, symbol: str) -> None:
        self.broker = broker
        self.symbol = symbol

    def open(
        self,
        direction: str,
        size: float,
        stop_loss: float,
        take_profit: float | None,
        client_order_id: str,
    ) -> _OpenOutcome:
        side = SIDE_BUY if direction == "long" else SIDE_SELL
        entry = _ensure_accepted(
            self.broker.create_order(
                self.symbol, side, size, type=ORDER_MARKET,
                stop_loss=stop_loss, take_profit=take_profit,
                client_order_id=client_order_id,
            ),
            "entry order",
        )
        ticket = self._find_ticket(client_order_id) or entry.order_id
        return _OpenOutcome(entry=entry, mt5_ticket=str(ticket) if ticket else None)

    def _find_ticket(self, client_order_id: str) -> str | None:
        """Position ticket whose comment carries our client order id."""
        try:
            positions = self.broker.open_positions(self.symbol)
        except BrokerError:
            return None
        for position in positions:
            if position.raw.get("comment") == client_order_id:
                return position.position_id
        return None

    @staticmethod
    def _ticket(trade: LiveTrade) -> int:
        if not trade.mt5_ticket:
            raise OrderError(
                "no MetaTrader position ticket recorded — cannot manage the venue SL."
            )
        try:
            return int(trade.mt5_ticket)
        except ValueError:
            raise OrderError(
                f"MetaTrader position ticket {trade.mt5_ticket!r} is not numeric."
            ) from None

    def move_stop(self, trade: LiveTrade, new_sl: float, old_sl: float | None = None) -> list[str]:
        # modify_position keeps the venue TP when take_profit is not passed.
        _ensure_accepted(
            self.broker.modify_position(self._ticket(trade), stop_loss=new_sl),
            "SL modify",
        )
        return []

    def partial_close(self, trade: LiveTrade, quantity: float) -> OrderResult:
        # multi-RR level: partial close by volume on the position ticket —
        # "partial-close market order the moment a level price is reached".
        return _ensure_accepted(
            self.broker.close_position(
                self.symbol, quantity=quantity, ticket=self._ticket(trade)
            ),
            "ladder partial-close order",
        )

    def close(self, trade: LiveTrade) -> tuple[OrderResult, list[str]]:
        result = _ensure_accepted(
            self.broker.close_position(
                self.symbol, quantity=trade.pos.size, ticket=self._ticket(trade)
            ),
            "close order",
        )
        return result, []   # MT5 SL/TP are position attributes — gone with the position


_VENUES = {
    MARKET_FUTURES: _FuturesVenue,
    MARKET_SPOT: _SpotVenue,
    MARKET_FOREX: _MetaTraderVenue,
}


# ---------------------------------------------------------------------------
# ExecutionEngine
# ---------------------------------------------------------------------------

class ExecutionEngine:
    """
    Per-pair execution core: real entries, venue-native SL/TP, shared-
    maths SL management, force-close.  Driven by the trader loop —
    single-threaded per pair; this class performs no feed subscription, no
    strategy evaluation and no venue-side close detection.

    Parameters
    ----------
    broker
        A connected ``BaseBroker`` (duck-typed; ``broker.market`` picks the
        venue mechanics — ``"futures"`` / ``"spot"`` / ``"forex"``).
    config : TraderConfig
        The pair's config.  Spot venues require ``leverage == 1``.
    events : EventStream
        stream — receives ``OPEN`` / ``SL_MOVE`` / ``RISK_FREE`` /
        ``CLOSE`` / ``ERROR`` events, all tagged ``SOURCE_LIVE``.
    trader_id : str | None
        Short id embedded in every ``client_order_id`` (ties venue orders to
        this trader run).  ``None`` → a random 6-hex-char id.
    primary_timeframe : str
        The strategy's primary timeframe — forwarded into the derived
        ``SimulateConfig``.
    quote_asset : str | None
        Spot only: the quote currency whose free balance funds this pair.
        ``None`` → detected from the symbol suffix (``"BTCUSDT"`` → USDT).
    """

    def __init__(
        self,
        broker: Any,
        config: TraderConfig,
        *,
        events: EventStream,
        trader_id: str | None = None,
        primary_timeframe: str = "1h",
        quote_asset: str | None = None,
    ) -> None:
        if not isinstance(config, TraderConfig):
            raise TypeError(f"config must be a TraderConfig, got {type(config).__name__}.")
        market = getattr(broker, "market", None)
        if market not in _VENUES:
            raise ValueError(
                "ExecutionEngine needs a broker with market 'futures', 'spot' or "
                f"'forex', got market={market!r}."
            )
        if trader_id is not None and (not isinstance(trader_id, str) or not trader_id):
            raise ValueError(f"trader_id must be a non-empty string, got {trader_id!r}.")
        self._quote_asset: str | None = None
        if market == MARKET_SPOT:
            if config.leverage != 1.0:
                raise ValueError(
                    "Binance spot has no leverage — TraderConfig.leverage must be 1.0, "
                    f"got {config.leverage}."
                )
            if config.tp_mode == TP_MODE_MULTI_RR:
                raise ValueError(
                    "tp_mode='multi_rr' is not supported on Binance spot — the held "
                    "asset is locked by its protective stop, so a per-level ladder "
                    "cannot be venue-native (covers Binance futures and "
                    "MetaTrader).  Use binance-futures for multi-RR ladders."
                )
            self._quote_asset = (quote_asset or _detect_quote_asset(config.symbol)).upper()

        self.broker = broker
        self.config = config
        self.events = events
        self.trader_id = trader_id or secrets.token_hex(3)
        self._market = market
        self._primary_timeframe = primary_timeframe
        self._venue = _VENUES[market](broker, config.symbol)
        self._trades: list[LiveTrade] = []
        self._trade_seq = 0
        self._started = False
        self._sim_config = None
        self._start_balance = 0.0

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def open_trades(self) -> tuple[LiveTrade, ...]:
        """The live positions currently under this engine's management."""
        return tuple(self._trades)

    @property
    def trade_seq(self) -> int:
        """The next trade id this engine will assign (journaled by)."""
        return self._trade_seq

    @property
    def sim_config(self):
        """The derived ``SimulateConfig`` driving the shared maths (after start)."""
        return self._sim_config

    @property
    def start_balance(self) -> float:
        """Account balance captured at :meth:`start` — the non-compound risk base."""
        return self._start_balance

    def read_balance(self) -> float:
        """Current account balance in the pair's sizing currency (a venue
        read — the daily-loss percent base re-reads it at each UTC-day
        rollover)."""
        return self._read_balance()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """
        Prepare for trading: set the leverage on futures venues (—
        "leverage set via set_leverage on start"), capture the account
        balance (the ``compound=False`` risk base) and derive the
        ``SimulateConfig`` that feeds the shared position maths — costs from
        ``broker.get_trading_costs()`` unless overridden.
        """
        if self._started:
            raise RuntimeError("ExecutionEngine.start() may only run once.")
        if self._market == MARKET_FUTURES:
            self.broker.set_leverage(self.config.symbol, self.config.leverage)
        balance = self._read_balance()
        if balance <= 0:
            raise ValueError(
                f"Live account balance for {self.config.symbol} must be > 0 to trade, "
                f"got {balance}."
            )
        self._start_balance = balance
        sim_config = self.config.to_simulate_config(
            self.broker, initial_balance=balance, primary_timeframe=self._primary_timeframe
        )
        if self._market == MARKET_FOREX and not sim_config.is_metatrader():
            # to_simulate_config keys exchange_type off isinstance(MetaTraderBroker);
            # the venue maths must follow the market either way (lot sizing).
            sim_config = replace(sim_config, exchange_type=EXCHANGE_TYPE_METATRADER)
        self._sim_config = sim_config
        self._started = True

    def _require_started(self) -> None:
        if not self._started:
            raise RuntimeError("ExecutionEngine.start() must run before trading operations.")

    # ------------------------------------------------------------------
    # restart adoption
    # ------------------------------------------------------------------

    def adopt_trade(self, trade: LiveTrade) -> None:
        """Adopt a journal-restored :class:`LiveTrade` into live management
        (restart reconciliation): the trade is tracked as if this engine
        had opened it — ``update_stops`` / ladder checks / close detection
        all resume from its restored state.  The trade-id sequence advances
        past the adopted id so new trades never collide."""
        self._require_started()
        if trade in self._trades:
            raise ValueError(
                f"trade #{trade.pos.trade_id} is already tracked by this engine."
            )
        self._trades.append(trade)
        self.ensure_trade_seq(trade.pos.trade_id + 1)

    def ensure_trade_seq(self, value: int) -> None:
        """Raise the trade-id sequence to at least *value* (: journaled
        across restarts so trade ids stay unique per journal)."""
        self._trade_seq = max(self._trade_seq, int(value))

    def resync_stop(self, trade: LiveTrade, *, where: str = "reconcile") -> bool:
        """Push the position's current **internal** SL to the venue as a fresh
        protective order (adoption re-arm: the venue-side SL vanished
        while offline — says re-arm immediately).

        Returns ``True`` when the venue accepted it.  Failure emits
        ``ERROR(will_retry)`` and arms ``pending_sl_sync`` so
        :meth:`update_stops` retries every closed candle; a spot venue that
        can neither re-place protection nor keep the old one emergency-sells
        (the trade is then untracked — check ``open_trades``)."""
        self._require_started()
        try:
            warnings = self._venue.move_stop(trade, trade.pos.stop_loss, None)
        except _EmergencyClosed as exc:
            self._handle_emergency_close(trade, exc, where)
            return False
        except BrokerError as exc:
            trade.pending_sl_sync = True
            self._error(where, f"venue SL modify failed: {exc}",
                        will_retry=True, details={"trade_id": trade.pos.trade_id})
            return False
        trade.pending_sl_sync = False
        for message in warnings:
            self._error(where, message, details={"trade_id": trade.pos.trade_id})
        return True

    def rearm_take_profit(self, trade: LiveTrade) -> bool:
        """Re-place a vanished plain TP order at ``trade.venue_tp`` (
        adoption; Binance futures only — spot TP rides the OCO protection and
        MT5 TP is a position attribute).  Failure emits ``ERROR`` and clears
        the stale ``tp_order_id`` — the position stays SL-protected."""
        self._require_started()
        if self._market != MARKET_FUTURES or trade.venue_tp is None:
            return False
        try:
            result = self._venue.place_take_profit(
                trade, trade.venue_tp, trade.client_order_id + "-tp"
            )
        except BrokerError as exc:
            trade.tp_order_id = None
            self._error(
                "attach_take_profit",
                f"take-profit could not be re-attached ({exc}); the position stays "
                "open with its stop-loss armed.",
                details={"trade_id": trade.pos.trade_id},
            )
            return False
        trade.tp_order_id = result.order_id
        return True

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _read_balance(self) -> float:
        """Real account balance in the pair's sizing currency."""
        if self._market == MARKET_SPOT:
            for balance in self.broker.get_balance():
                if balance.asset.upper() == self._quote_asset:
                    return float(balance.free)
            return 0.0
        return float(self.broker.get_account_info().wallet_balance)

    def _client_order_id(self, signal: Signal) -> str:
        timestamp = int(signal.timestamp)
        if self._market == MARKET_FOREX:
            timestamp //= 1000   # MT5 comment caps at ~31 chars — seconds fit
        return f"atk-{self.trader_id}-{self.config.symbol.upper()}-{timestamp}"

    def _emit(self, event) -> None:
        self.events.emit(event)

    def _error(
        self,
        where: str,
        message: str,
        *,
        will_retry: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        self._emit(ErrorEvent(
            time=now_ms(), symbol=self.config.symbol, source=SOURCE_LIVE,
            where=where, message=message, will_retry=will_retry, details=details or {},
        ))

    def _market_price_fallback(self, pos: _InternalPosition) -> float:
        """Exit-price fallback when a close fill reports no price."""
        try:
            last = float(self.broker.get_ticker(self.config.symbol).last)
            if last > 0:
                return last
        except BrokerError:
            pass
        return pos.entry_price

    def _close_and_emit(
        self,
        trade: LiveTrade,
        exit_price: float,
        reason: str,
        *,
        close_time: int | None = None,
        closed_size: float | None = None,
    ) -> ClosedTrade:
        """Build the ``ClosedTrade`` for *trade* and emit its CLOSE event.

        ``closed_size`` builds a partial slice (the caller shrinks the
        position afterwards); tracking-list changes are the caller's job.
        """
        close_time = now_ms() if close_time is None else int(close_time)
        closed = make_closed_trade(
            trade.pos, exit_price, reason, close_time, self._sim_config,
            closed_size=closed_size,
        )
        self._emit(CloseEvent(
            time=close_time, symbol=self.config.symbol, source=SOURCE_LIVE,
            trade_id=closed.trade_id, exit_price=exit_price, reason=reason,
            gross_pnl=closed.gross_pnl, net_pnl=closed.net_pnl, pnl_r=closed.pnl_r,
            duration_ms=closed.duration_ms, trade=closed,
        ))
        return closed

    def _handle_emergency_close(self, trade: LiveTrade, exc: _EmergencyClosed, where: str) -> None:
        """A venue op emergency-closed *trade* — record, emit and untrack it."""
        self._trades.remove(trade)
        self._error(where, str(exc), details={"trade_id": trade.pos.trade_id})
        exit_price = float(exc.result.avg_fill_price or 0.0) or \
            self._market_price_fallback(trade.pos)
        self._close_and_emit(trade, exit_price, CLOSE_REASON_FC)

    # ------------------------------------------------------------------
    # Entries
    # ------------------------------------------------------------------

    def open_position(self, signal: Signal) -> LiveTrade | None:
        """
        Execute *signal* as a market entry with venue-native protection.

        Sizing runs through the shared helpers against the **real**
        account balance (``compound``, ``risk_multiplier`` and the
        ``max_*_positions`` limits — counted against this engine's live
        positions — all respected).  Returns the tracked :class:`LiveTrade`,
        or ``None`` when the entry was skipped (position limits, sizing
        returned nothing) or failed on the venue (an ``ERROR`` event carries
        the venue's words; a filled entry whose SL could not be attached is
        emergency-closed —).

        Binance spot cannot short — a short signal raises ``OrderError``.
        """
        self._require_started()
        if self._market == MARKET_SPOT and signal.direction == "short":
            raise OrderError(
                "Binance spot cannot short — a spot wallet only sells what it holds. "
                "Use binance-futures for short signals."
            )
        cfg = self._sim_config
        if not can_open_position(signal, [t.pos for t in self._trades], cfg):
            return None
        try:
            wallet = self._read_balance()
        except BrokerError as exc:
            self._error("open_position", f"balance read failed: {exc}")
            return None
        params = compute_position_params(signal, cfg, wallet)
        if params is None:
            return None
        (_margin, size, pnl_per_price_unit, _risk, commission,
         _spread_paid, tp_prices, reference_price) = params
        venue_tp = (
            tp_prices[0]
            if cfg.tp_mode in (TP_MODE_SIGNAL, TP_MODE_FIXED_RR) and tp_prices
            else None
        )
        client_order_id = self._client_order_id(signal)

        try:
            outcome = self._venue.open(
                signal.direction, size, signal.stop_loss, venue_tp, client_order_id
            )
        except _ProtectionFailure as exc:
            self._error("attach_protection", str(exc), details=exc.details)
            return None
        except BrokerError as exc:
            self._error("open_position", f"entry order failed: {exc}")
            return None

        fill_price = float(outcome.entry.avg_fill_price or 0.0) or reference_price
        slippage = fill_price - reference_price
        # Re-anchor the $-risk and margin on the real fill: the $-per-price-unit
        # is a physical property of the filled size and stays as sized; the
        # distance to the signal SL is re-measured from the fill.
        sl_distance = abs(fill_price - signal.stop_loss) or abs(
            reference_price - signal.stop_loss
        )
        risk_amount = pnl_per_price_unit * sl_distance
        if cfg.is_metatrader():
            margin_amount = risk_amount           # simplified MT5 margin model (sim parity)
        else:
            margin_amount = size * fill_price / cfg.leverage

        pos = _InternalPosition(
            trade_id=self._trade_seq,
            symbol=cfg.symbol or dict(signal.metadata).get("symbol", ""),
            direction=signal.direction,
            entry_price=fill_price,
            raw_entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit=tp_prices[0] if tp_prices else None,
            margin_amount=margin_amount,
            risk_amount=risk_amount,
            size=size,
            open_time=int(signal.timestamp),
            open_commission=commission,
            signal_metadata=dict(signal.metadata),
            signal_candle_index=signal.candle_index,
            tp_level_prices=tp_prices,
        )
        # Entry-state SL record — exactly like the sim engine (v0.7.4)
        pos.sl_history.append({
            "time": int(signal.timestamp),
            "sl": signal.stop_loss,
            "next_tp": tp_prices[0] if tp_prices else None,
        })
        trade = LiveTrade(
            pos=pos,
            signal=signal,
            client_order_id=client_order_id,
            entry_order_id=outcome.entry.order_id,
            reference_price=reference_price,
            fill_price=fill_price,
            slippage=slippage,
            venue_tp=venue_tp,
            sl_order_id=outcome.sl_order_id,
            tp_order_id=outcome.tp_order_id,
            oco_order_list_id=outcome.oco_order_list_id,
            mt5_ticket=outcome.mt5_ticket,
        )
        self._trades.append(trade)
        self._trade_seq += 1
        self._emit(OpenEvent(
            time=now_ms(), symbol=cfg.symbol, source=SOURCE_LIVE,
            trade_id=pos.trade_id, direction=signal.direction,
            fill_price=fill_price, size=size, margin_amount=margin_amount,
            risk_amount=risk_amount, stop_loss=signal.stop_loss,
            next_tp=pos.next_tp, order_id=outcome.entry.order_id or None,
        ))
        if outcome.tp_error:
            self._error("attach_take_profit", outcome.tp_error,
                        details={"trade_id": pos.trade_id})
        if (
            self._market == MARKET_FUTURES
            and cfg.tp_mode == TP_MODE_MULTI_RR
            and pos.tp_level_prices
        ):
            self._place_ladder(trade)
        return trade

    # ------------------------------------------------------------------
    # SL management (per closed candle)
    # ------------------------------------------------------------------

    def update_stops(self, candle: Mapping) -> None:
        """
        Advance every tracked position's SL state for one **closed** candle,
        mirroring the sim engine's per-candle order (steps A → B → the
        trailing part of C) with the exact shared maths, and push every
        SL change to the venue.

        No close detection happens here — the armed venue SL/TP orders do
        the closing and detects their fills.  A venue modify failure
        emits ``ERROR`` and reverts the internal SL (and its ``sl_history``
        record) so the next candle retries.
        """
        self._require_started()
        if not self._trades:
            return
        timestamp = int(candle["timestamp"])
        high = float(candle["high"])
        low = float(candle["low"])
        close = float(candle["close"])
        cfg = self._sim_config

        #: a venue SL modify that failed right after a ladder fill is
        # retried here every closed candle until it lands — the realized
        # close cannot be reverted, so the internal state stays advanced.
        for trade in self._trades:
            if not trade.pending_sl_sync:
                continue
            try:
                warnings = self._venue.move_stop(trade, trade.pos.stop_loss, None)
            except BrokerError as exc:
                self._error("ladder_sl_move", f"venue SL modify retry failed: {exc}",
                            will_retry=True, details={"trade_id": trade.pos.trade_id})
                continue
            trade.pending_sl_sync = False
            for message in warnings:
                self._error("ladder_sl_move", message,
                            details={"trade_id": trade.pos.trade_id})

        # A. trailing peak + excursion tracking (engine step A)
        for trade in self._trades:
            pos = trade.pos
            pos.update_trailing_peak(high if pos.direction == "long" else low)
            pos.update_excursion(close)

        # B. risk-free break-even (engine step B — not in multi-RR mode)
        if cfg.risk_free_enabled and cfg.tp_mode != TP_MODE_MULTI_RR:
            for trade in list(self._trades):
                pos = trade.pos
                old_sl = pos.stop_loss
                check_risk_free(pos, high, low, cfg)
                if pos.stop_loss == old_sl:
                    continue
                try:
                    warnings = self._venue.move_stop(trade, pos.stop_loss, old_sl)
                except _EmergencyClosed as exc:
                    self._handle_emergency_close(trade, exc, "risk_free")
                    continue
                except BrokerError as exc:
                    pos.stop_loss = old_sl
                    pos.risk_free_triggered = False
                    self._error("risk_free", f"venue SL modify failed: {exc}",
                                will_retry=True, details={"trade_id": pos.trade_id})
                    continue
                self._emit(RiskFreeEvent(
                    time=timestamp, symbol=cfg.symbol, source=SOURCE_LIVE,
                    trade_id=pos.trade_id, rr_level=cfg.risk_free_at_rr,
                    old_sl=old_sl, new_sl=pos.stop_loss,
                ))
                for message in warnings:
                    self._error("risk_free", message, details={"trade_id": pos.trade_id})

        # C. trailing SL advance (engine folds this into check_close step 2;
        #    live needs no gap check — the venue SL was already armed)
        for trade in list(self._trades):
            pos = trade.pos
            old_sl = pos.stop_loss
            history_len = len(pos.sl_history)
            update_trailing_sl(pos, cfg, timestamp)
            if pos.stop_loss == old_sl:
                continue
            try:
                warnings = self._venue.move_stop(trade, pos.stop_loss, old_sl)
            except _EmergencyClosed as exc:
                self._handle_emergency_close(trade, exc, "sl_move")
                continue
            except BrokerError as exc:
                pos.stop_loss = old_sl
                del pos.sl_history[history_len:]
                self._error("sl_move", f"venue SL modify failed: {exc}",
                            will_retry=True, details={"trade_id": pos.trade_id})
                continue
            self._emit(SlMoveEvent(
                time=timestamp, symbol=cfg.symbol, source=SOURCE_LIVE,
                trade_id=pos.trade_id, old_sl=old_sl, new_sl=pos.stop_loss,
                cause="trailing", next_tp=pos.next_tp,
            ))
            for message in warnings:
                self._error("sl_move", message, details={"trade_id": pos.trade_id})

    # ------------------------------------------------------------------
    # Multi-RR live ladder
    # ------------------------------------------------------------------

    def _ladder_plan(self, pos: _InternalPosition) -> list[tuple[int, float, float]]:
        """``(level_idx, price, quantity)`` per venue-order level of the ladder.

        ``tp_level_close_fractions`` set → one entry per level with a positive
        fraction (zero-fraction levels are SL-advance-only → feed-detected).
        ``None`` (fraction-less sim mode) → only the final level, full size —
        intermediate levels close nothing and stay feed-detected.
        """
        fractions = self._sim_config.tp_level_close_fractions
        prices = pos.tp_level_prices
        if fractions is None:
            return [(len(prices) - 1, prices[-1], pos.original_size)]
        return [
            (idx, prices[idx], fractions[idx] * pos.original_size)
            for idx in range(len(prices))
            if fractions[idx] > 0.0
        ]

    def _place_ladder(self, trade: LiveTrade) -> None:
        """Place the venue-native reduce-only ladder orders (Binance futures).

        A level whose order cannot be placed emits ``ERROR`` and degrades to
        feed detection — :meth:`check_tp_levels` will close it at market when
        the price reaches it; the position keeps its armed SL either way.
        """
        for level_idx, price, quantity in self._ladder_plan(trade.pos):
            self.place_ladder_level(trade, level_idx, price, quantity)

    def place_ladder_level(
        self, trade: LiveTrade, level_idx: int, price: float, quantity: float
    ) -> bool:
        """Place one venue-native ladder level order (entry placement and the
        re-place of a vanished unfilled level).  Returns ``True`` when the
        order is on the book; ``False`` degrades the level to feed detection
        (``ERROR`` emitted)."""
        client_order_id = f"{trade.client_order_id}-tp{level_idx + 1}"
        try:
            result = self._venue.place_ladder_order(
                trade.pos.direction, quantity, price, client_order_id
            )
        except BrokerError as exc:
            self._error(
                "place_ladder",
                f"ladder level {level_idx + 1} order could not be placed ({exc}); "
                "the level degrades to feed detection — it will be closed at "
                "market when the price reaches it.",
                details={"trade_id": trade.pos.trade_id, "level": level_idx + 1},
            )
            return False
        trade.ladder_order_ids[result.order_id] = level_idx
        return True

    def _ladder_close_size(self, pos: _InternalPosition, level_idx: int) -> float:
        """Absolute size a feed-detected hit of *level_idx* should realize —
        the exact ``handle_tp_level_hit`` fraction rule."""
        fractions = self._sim_config.tp_level_close_fractions
        if fractions is not None:
            frac = fractions[level_idx]
        else:
            frac = 1.0 if level_idx + 1 >= len(pos.tp_level_prices) else 0.0
        if frac <= 0.0:
            return 0.0
        return min(frac * pos.original_size, pos.size)

    def check_tp_levels(self, candle_high: float, candle_low: float, timestamp: int) -> None:
        """
        Feed-detected multi-RR level checks — called by on every
        closed candle and on every forming update / synthesized tick row, so
        MT5 partial closes fire "the moment a level price is reached".

        No-op unless ``tp_mode="multi_rr"``.  A level whose ladder order is
        live on the venue is skipped — its fill (user-data stream) settles it
        via :meth:`record_ladder_fill`.  For the rest: a touched level with a
        positive close size is executed as a partial-close **market** order
        (MT5 always; futures only for degraded levels), a zero-size level is
        an SL-advance-only move.  One candle gapping through several levels
        processes them in order, exactly like the sim's ``check_close``.
        """
        self._require_started()
        cfg = self._sim_config
        if cfg.tp_mode != TP_MODE_MULTI_RR or not self._trades:
            return
        high, low, ts = float(candle_high), float(candle_low), int(timestamp)
        for trade in list(self._trades):
            pos = trade.pos
            while pos.next_tp is not None:
                if pos.direction == "long":
                    touched = high >= pos.next_tp
                else:
                    touched = low <= pos.next_tp
                if not touched:
                    break
                level_idx = pos.last_rr_hit
                if level_idx in trade.ladder_order_ids.values():
                    break   # a live venue order owns this level (fills settle it)
                closed_size = self._ladder_close_size(pos, level_idx)
                if closed_size <= SIZE_EPSILON:
                    if not self._ladder_advance_only(trade, ts):
                        break   # venue SL modify failed — reverted, re-detected
                    continue
                if not self._ladder_market_close(trade, level_idx, closed_size, ts):
                    break       # venue close failed — next_tp unchanged, retried
                if trade not in self._trades:
                    break       # that level fully closed the position

    def _ladder_advance_only(self, trade: LiveTrade, timestamp: int) -> bool:
        """A touched level that closes nothing: advance the ladder (shared
        maths) and push the new SL to the venue.  Returns False on venue
        failure — nothing was realized, so the advance is fully reverted and
        the untouched ``next_tp`` re-detects the level on the next update."""
        pos = trade.pos
        old_sl = pos.stop_loss
        advance_multi_rr(pos, timestamp)
        try:
            warnings = self._venue.move_stop(trade, pos.stop_loss, old_sl)
        except BrokerError as exc:
            pos.stop_loss = old_sl
            pos.last_rr_hit -= 1
            pos.next_tp = pos.tp_level_prices[pos.last_rr_hit]
            pos.sl_history.pop()
            self._error("ladder_sl_move", f"venue SL modify failed: {exc}",
                        will_retry=True, details={"trade_id": pos.trade_id})
            return False
        self._emit(SlMoveEvent(
            time=timestamp, symbol=self._sim_config.symbol, source=SOURCE_LIVE,
            trade_id=pos.trade_id, old_sl=old_sl, new_sl=pos.stop_loss,
            cause="ladder", next_tp=pos.next_tp,
        ))
        for message in warnings:
            self._error("ladder_sl_move", message, details={"trade_id": pos.trade_id})
        return True

    def _ladder_market_close(
        self, trade: LiveTrade, level_idx: int, closed_size: float, timestamp: int
    ) -> bool:
        """Execute a feed-detected level at market and settle it.
        Returns False when the venue refused the close — ``next_tp`` is
        untouched, so the next feed update retries."""
        pos = trade.pos
        try:
            result = self._venue.partial_close(trade, closed_size)
        except BrokerError as exc:
            self._error(
                "tp_level_close",
                f"partial close at ladder level {level_idx + 1} failed: {exc}",
                will_retry=True, details={"trade_id": pos.trade_id},
            )
            return False
        exit_price = float(result.avg_fill_price or 0.0) or pos.tp_level_prices[level_idx]
        self._settle_ladder_fill(trade, level_idx, exit_price, closed_size, timestamp)
        return True

    def record_ladder_fill(
        self,
        trade: LiveTrade,
        order_id: str,
        exit_price: float,
        quantity: float,
        *,
        close_time: int | None = None,
    ) -> ClosedTrade:
        """
        A venue-native ladder order filled (Binance futures user-data stream,
        detection) — settle that level: advance the ladder with the
        shared maths, record the partial ``ClosedTrade`` slice (or the
        final full close), move the venue SL, emit ``TP_LEVEL`` / ``CLOSE``.

        The real fill is authoritative: *exit_price* falls back to the level
        price and *quantity* to the level's planned size when the venue
        reports none.
        """
        self._require_started()
        if trade not in self._trades:
            raise ValueError(
                f"trade #{trade.pos.trade_id} is not tracked by this engine."
            )
        if order_id not in trade.ladder_order_ids:
            raise ValueError(
                f"order {order_id!r} is not a ladder order of trade "
                f"#{trade.pos.trade_id}."
            )
        level_idx = trade.ladder_order_ids.pop(order_id)
        close_time = now_ms() if close_time is None else int(close_time)
        price = float(exit_price or 0.0) or trade.pos.tp_level_prices[level_idx]
        qty = float(quantity or 0.0) or self._ladder_close_size(trade.pos, level_idx)
        return self._settle_ladder_fill(trade, level_idx, price, qty, close_time)

    def _settle_ladder_fill(
        self,
        trade: LiveTrade,
        level_idx: int,
        exit_price: float,
        quantity: float,
        close_time: int,
    ) -> ClosedTrade:
        """Common settlement of one realized ladder level (venue fill or
        feed-detected market close): catch-up skipped levels, advance the
        ladder, build the slice, move the venue SL, emit the events."""
        pos = trade.pos
        cfg = self._sim_config
        venue_old_sl = pos.stop_loss

        # Catch-up: a fill can arrive for a level ahead of the ladder cursor
        # (an earlier level's order failed to place or its fill event was
        # missed) — advance the skipped levels first, advance-only; their own
        # late fills, if any, will record their slices without re-advancing.
        while pos.last_rr_hit < level_idx:
            old_sl = pos.stop_loss
            advance_multi_rr(pos, close_time)
            self._emit(SlMoveEvent(
                time=close_time, symbol=cfg.symbol, source=SOURCE_LIVE,
                trade_id=pos.trade_id, old_sl=old_sl, new_sl=pos.stop_loss,
                cause="ladder", next_tp=pos.next_tp,
            ))
        if pos.last_rr_hit == level_idx:
            advance_multi_rr(pos, close_time)   # this level's own ladder step

        full = quantity >= pos.size - SIZE_EPSILON
        if full:
            closed = self._close_and_emit(
                trade, exit_price, CLOSE_REASON_TP, close_time=close_time
            )
            self._trades.remove(trade)
            self._cancel_ladder_leftovers(trade)
            return closed

        closed = make_closed_trade(
            pos, exit_price, CLOSE_REASON_TP_PARTIAL, close_time, cfg,
            closed_size=quantity,
        )
        apply_partial_close(pos, quantity)
        if pos.stop_loss != venue_old_sl:
            # The replacement stop is quantity-scoped — shrink first, move after.
            self._ladder_move_venue_sl(trade, venue_old_sl)
        level_rr = (
            float(cfg.tp_levels[level_idx])
            if 0 <= level_idx < len(cfg.tp_levels)
            else float(level_idx + 1)
        )
        self._emit(TpLevelEvent(
            time=close_time, symbol=cfg.symbol, source=SOURCE_LIVE,
            trade_id=pos.trade_id, level=level_rr,
            fraction_closed=closed.size / pos.original_size if pos.original_size else 0.0,
            realized_pnl=closed.net_pnl, new_sl=pos.stop_loss, trade=closed,
        ))
        return closed

    def _ladder_move_venue_sl(self, trade: LiveTrade, old_sl: float) -> None:
        """Push a post-fill ladder SL to the venue.  The realized close cannot
        be reverted, so a failure keeps the internal state advanced, emits
        ``ERROR(will_retry)`` and arms the per-candle retry
        (``pending_sl_sync``, drained by :meth:`update_stops`)."""
        try:
            warnings = self._venue.move_stop(trade, trade.pos.stop_loss, old_sl)
        except BrokerError as exc:
            trade.pending_sl_sync = True
            self._error(
                "ladder_sl_move",
                f"venue SL modify failed after a TP level fill: {exc}",
                will_retry=True, details={"trade_id": trade.pos.trade_id},
            )
            return
        for message in warnings:
            self._error("ladder_sl_move", message,
                        details={"trade_id": trade.pos.trade_id})

    def _cancel_ladder_leftovers(self, trade: LiveTrade) -> None:
        """After the ladder fully closed the position: cancel whatever
        protective/ladder orders are still armed (Binance futures — MT5 SL/TP
        are position attributes and died with the position)."""
        if self._market != MARKET_FUTURES:
            return
        leftovers = [("stop-loss", trade.sl_order_id), ("take-profit", trade.tp_order_id)]
        leftovers += [
            (f"ladder level {idx + 1}", oid) for oid, idx in trade.ladder_order_ids.items()
        ]
        for label, order_id in leftovers:
            if not order_id:
                continue
            try:
                self.broker.cancel_order(order_id, self.config.symbol)
            except BrokerError as exc:
                self._error(
                    "cleanup",
                    f"{label} order {order_id} could not be cancelled after the "
                    f"ladder closed trade #{trade.pos.trade_id} ({exc}).",
                    details={"trade_id": trade.pos.trade_id},
                )
        trade.ladder_order_ids.clear()

    # ------------------------------------------------------------------
    # Force close
    # ------------------------------------------------------------------

    def force_close(self, reason: str = CLOSE_REASON_FC) -> list[ClosedTrade]:
        """
        Market-close every tracked position (ExitSignal with
        ``force_close_on_exit_signal``, the ``close_all``, …), cancel its
        protective orders and emit a ``CLOSE`` event per trade.

        Exits always fill at market — an ``ExitSignal.exit_price`` cannot be
        demanded from a market close, so the venue's fill (falling back to
        the current ticker) is the exit price.  A trade whose close order
        fails stays tracked (its venue SL keeps protecting it) and emits
        ``ERROR``.  Returns the ``ClosedTrade`` records (maths — the
        real-fills display consumes them via the ``CLOSE`` events).
        """
        self._require_started()
        closed: list[ClosedTrade] = []
        remaining: list[LiveTrade] = []
        for trade in self._trades:
            try:
                result, warnings = self._venue.close(trade)
            except BrokerError as exc:
                self._error("force_close", f"market close failed: {exc}",
                            details={"trade_id": trade.pos.trade_id})
                remaining.append(trade)
                continue
            for message in warnings:
                self._error("force_close", message, details={"trade_id": trade.pos.trade_id})
            exit_price = float(result.avg_fill_price or 0.0) or \
                self._market_price_fallback(trade.pos)
            closed.append(self._close_and_emit(trade, exit_price, reason))
        self._trades = remaining
        return closed

    # ------------------------------------------------------------------
    # Venue-detected closes (close detection feeds this)
    # ------------------------------------------------------------------

    def record_external_close(
        self,
        trade: LiveTrade,
        exit_price: float,
        reason: str,
        *,
        close_time: int | None = None,
        closed_size: float | None = None,
    ) -> ClosedTrade:
        """
        Record a close (or partial close) the **venue** performed — an armed
        SL/TP order filled, or the position was closed / reduced manually on
        the venue.  the close detection calls this with the real fill
        price; the engine itself never watches for closes (contract).

        Full close (default, or *closed_size* >= the remaining size): the
        trade is untracked.  Partial (*closed_size* < remaining): a
        partial slice sharing the ``trade_id`` is recorded and the position
        shrinks in place (``apply_partial_close``).  Emits the ``CLOSE``
        event either way and returns the ``ClosedTrade``.  Cancelling a
        leftover protective order is the caller's job — it is venue- and
        cause-specific (e.g. a filled spot OCO leg auto-cancels its sibling,
        a filled futures stop does not cancel the take-profit order).
        """
        self._require_started()
        if trade not in self._trades:
            raise ValueError(
                f"trade #{trade.pos.trade_id} is not tracked by this engine."
            )
        partial = closed_size is not None and closed_size < trade.pos.size - SIZE_EPSILON
        closed = self._close_and_emit(
            trade, exit_price, reason,
            close_time=close_time,
            closed_size=closed_size if partial else None,
        )
        if partial:
            apply_partial_close(trade.pos, closed_size)
        else:
            self._trades.remove(trade)
        return closed
