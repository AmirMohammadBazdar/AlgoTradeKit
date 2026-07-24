"""
AlgoTradeKit.trader._state
~~~~~~~~~~~~~~~~~~~~~~~~~~~
Persistence & restart reconciliation for the live trader (v1.0.0).

Persistence
-----------
Every trade the :class:`~AlgoTradeKit.trader._execution.ExecutionEngine`
manages is journaled to ``state_path`` (default ``./.atk_trader_state.json``)
on every change: the originating :class:`~AlgoTradeKit.strategy.Signal`, the
full ``_InternalPosition`` state (current SL, ``sl_history``, trailing
``peak_price``, multi-RR ladder cursor ``next_tp``/``last_rr_hit``,
excursions, risk-free flag), the venue handles (entry / SL / TP / OCO /
ladder order ids, MT5 ticket) and the slippage record — plus the pair's
trade-id sequence and the signal-dedup keys.  Writes are **atomic**
(tmp file + ``os.replace``) and skipped when nothing changed.  The worker
flushes after every processed queue item, so the crash window is one item;
the venue's own orders remain the source of truth for protection either way
(— the venue SL is armed regardless of what the journal says).

Restart reconciliation
----------------------
On ``Trader.run()`` — after the engine is armed, **before** any producer can
act — the journal is reconciled against ``broker.open_positions()`` /
``broker.open_orders()`` by ``client_order_id``/comment/ticket, emitting one
``RECONCILE`` event:

* **journaled + present** → adopt and resume management: trailing state, RR
  ladder and risk-free status continue where they left off.  Protection the
  venue lost while offline is re-armed immediately: a vanished SL order
  is re-placed at the journaled stop (failure arms the per-candle
  ``pending_sl_sync`` retry), a vanished plain TP is re-attached, vanished
  **unfilled** ladder levels are re-placed (failure degrades that level to
  feed detection).  Ladder levels that **filled** while offline are
  settled from the venue's trade history (partial ``ClosedTrade`` slices,
  ladder SL moves — semantics).
* **journaled + missing** → it closed while offline: the real exit fill is
  fetched from history (Binance ``my_trades``; MetaTrader ``history_deals``)
  and recorded as a ``ClosedTrade`` (SL fills map through ``sl_reason`` —
  ``sl``/``rf`` — TP fills to ``tp``; an untraceable close falls back to the
  ticker price with reason ``manual``), and leftover protective/ladder
  orders are cancelled.
* **present + not journaled** (foreign/manual position) → **warned and left
  untouched** — never managed, never closed.  Never double-open: the
  journaled dedup keys are restored, so a signal for a (pair, candle)
  already journaled is not re-sent after a restart.

Venue notes: Binance futures nets one-way positions, so presence is
journal-order FIFO coverage of the venue's net exposure (same heuristic as
the manual-close reconcile) and *foreign* is the residual net.  Binance
spot has no positions — presence means the protective orders are still
working, so a protection the user cancelled while keeping the holding is
recorded as closed (reason ``manual``, the degraded-path rule).  A venue
read failure during reconciliation **aborts the start** — trading blind past
an unreconciled journal is never safe.

This module never imports ``_trader`` (the worker is passed in duck-typed);
``_trader`` imports the journal, the reconciler and the shared constants
from here.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any

from ..broker import (
    MARKET_FOREX,
    MARKET_FUTURES,
    MARKET_SPOT,
    POSITION_LONG,
    STATUS_REJECTED,
    BrokerError,
)
from ..broker._timeutil import now_ms
from ..simulate import CLOSE_REASON_TP
from ..simulate._position import _InternalPosition
from ..simulate._position_math import sl_reason
from ..strategy import Signal
from ._events import SOURCE_LIVE, ReconcileEvent
from ._execution import LiveTrade

#: Default journal location when ``TraderSettings.state_path`` is ``None``.
DEFAULT_STATE_PATH = ".atk_trader_state.json"
#: Journal schema version (bumped on incompatible layout changes).
JOURNAL_VERSION = 1

#: Close reason for a venue-side close the trader did not order and cannot
#: attribute to its own SL/TP — a manual close/reduction on the venue, or a
#: fill whose user-data event was missed.  Trader-only: the sim never
#: produces it (re-exported from ``trader/__init__.py`` via ``_trader``).
CLOSE_REASON_MANUAL = "manual"

#: Size tolerance when comparing venue quantities to tracked sizes (venue
#: payloads are decimal strings; sums of tracked floats need slack).
QTY_EPSILON = 1e-8

#: MT5 ``DEAL_REASON_*`` values that identify a venue-side SL / TP fill.
MT5_DEAL_REASON_SL = 4
MT5_DEAL_REASON_TP = 5
#: MT5 deal ``entry`` — 0 opens a position; anything else realises an exit.
MT5_DEAL_ENTRY_IN = 0


# ---------------------------------------------------------------------------
# Serialization — LiveTrade ⇄ JSON-safe dicts
# ---------------------------------------------------------------------------

def _serialize_position(pos: _InternalPosition) -> dict[str, Any]:
    return {
        "trade_id": pos.trade_id,
        "symbol": pos.symbol,
        "direction": pos.direction,
        "entry_price": pos.entry_price,
        "raw_entry_price": pos.raw_entry_price,
        "stop_loss": pos.stop_loss,
        "initial_stop_loss": pos.initial_stop_loss,
        "take_profit": pos.take_profit,
        "margin_amount": pos.margin_amount,
        "risk_amount": pos.risk_amount,
        "size": pos.size,
        "original_size": pos.original_size,
        "open_time": pos.open_time,
        "open_commission": pos.open_commission,
        "sl_distance": pos.sl_distance,
        "pnl_per_price_unit": pos.pnl_per_price_unit,
        "peak_price": pos.peak_price,
        "next_tp": pos.next_tp,
        "last_rr_hit": pos.last_rr_hit,
        "signal_metadata": dict(pos.signal_metadata),
        "signal_candle_index": pos.signal_candle_index,
        "tp_level_prices": list(pos.tp_level_prices),
        "risk_free_triggered": pos.risk_free_triggered,
        "max_favourable": pos._max_favourable,
        "max_adverse": pos._max_adverse,
        "sl_history": [dict(record) for record in pos.sl_history],
    }


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _restore_position(payload: dict[str, Any]) -> _InternalPosition:
    pos = _InternalPosition(
        trade_id=int(payload["trade_id"]),
        symbol=str(payload["symbol"]),
        direction=str(payload["direction"]),
        entry_price=float(payload["entry_price"]),
        raw_entry_price=float(payload["raw_entry_price"]),
        stop_loss=float(payload["initial_stop_loss"]),   # ctor: initial == entering SL
        take_profit=_opt_float(payload["take_profit"]),
        margin_amount=float(payload["margin_amount"]),
        risk_amount=float(payload["risk_amount"]),
        size=float(payload["original_size"]),            # ctor: original == entering size
        open_time=int(payload["open_time"]),
        open_commission=float(payload["open_commission"]),
        signal_metadata=dict(payload["signal_metadata"]),
        signal_candle_index=int(payload["signal_candle_index"]),
        tp_level_prices=[float(x) for x in payload["tp_level_prices"]],
    )
    # The ctor derives current-state fields from the entry state — overwrite
    # them with the journaled live values (they diverge as the trade runs).
    pos.stop_loss = float(payload["stop_loss"])
    pos.size = float(payload["size"])
    pos.sl_distance = float(payload["sl_distance"])
    pos.pnl_per_price_unit = float(payload["pnl_per_price_unit"])
    pos.peak_price = float(payload["peak_price"])
    pos.next_tp = _opt_float(payload["next_tp"])
    pos.last_rr_hit = int(payload["last_rr_hit"])
    pos.risk_free_triggered = bool(payload["risk_free_triggered"])
    pos._max_favourable = float(payload["max_favourable"])
    pos._max_adverse = float(payload["max_adverse"])
    pos.sl_history = [
        {
            "time": int(record["time"]),
            "sl": float(record["sl"]),
            "next_tp": _opt_float(record.get("next_tp")),
        }
        for record in payload["sl_history"]
    ]
    return pos


def _serialize_signal(signal: Signal) -> dict[str, Any]:
    return {
        "direction": signal.direction,
        "entry_price": signal.entry_price,
        "stop_loss": signal.stop_loss,
        "take_profit": signal.take_profit,
        "timestamp": int(signal.timestamp),
        "candle_index": int(signal.candle_index),
        "timeframe": signal.timeframe,
        "metadata": dict(signal.metadata),
        "risk_multiplier": signal.risk_multiplier,
    }


def _restore_signal(payload: dict[str, Any]) -> Signal:
    return Signal(
        str(payload["direction"]),
        float(payload["entry_price"]),
        float(payload["stop_loss"]),
        _opt_float(payload["take_profit"]),
        int(payload["timestamp"]),
        int(payload["candle_index"]),
        str(payload["timeframe"]),
        metadata=dict(payload["metadata"]),
        risk_multiplier=float(payload["risk_multiplier"]),
    )


def serialize_trade(trade: LiveTrade) -> dict[str, Any]:
    """One :class:`LiveTrade` as a JSON-safe dict — the journal's per-trade
    record (: signal, ``sl_history``, ladder cursor, ``peak_price``,
    venue order ids… the full resumable state)."""
    return {
        "pos": _serialize_position(trade.pos),
        "signal": _serialize_signal(trade.signal),
        "client_order_id": trade.client_order_id,
        "entry_order_id": trade.entry_order_id,
        "reference_price": trade.reference_price,
        "fill_price": trade.fill_price,
        "slippage": trade.slippage,
        "venue_tp": trade.venue_tp,
        "sl_order_id": trade.sl_order_id,
        "tp_order_id": trade.tp_order_id,
        "oco_order_list_id": trade.oco_order_list_id,
        "mt5_ticket": trade.mt5_ticket,
        "ladder_order_ids": {str(k): int(v) for k, v in trade.ladder_order_ids.items()},
        "pending_sl_sync": trade.pending_sl_sync,
    }


def restore_trade(payload: dict[str, Any]) -> LiveTrade:
    """Rebuild a :class:`LiveTrade` from its journaled dict (inverse of
    :func:`serialize_trade`)."""
    return LiveTrade(
        pos=_restore_position(payload["pos"]),
        signal=_restore_signal(payload["signal"]),
        client_order_id=str(payload["client_order_id"]),
        entry_order_id=str(payload["entry_order_id"]),
        reference_price=float(payload["reference_price"]),
        fill_price=float(payload["fill_price"]),
        slippage=float(payload["slippage"]),
        venue_tp=_opt_float(payload["venue_tp"]),
        sl_order_id=payload["sl_order_id"],
        tp_order_id=payload["tp_order_id"],
        oco_order_list_id=payload["oco_order_list_id"],
        mt5_ticket=payload["mt5_ticket"],
        ladder_order_ids={str(k): int(v) for k, v in payload["ladder_order_ids"].items()},
        pending_sl_sync=bool(payload["pending_sl_sync"]),
    )


def build_pair_state(engine: Any, acted: set, exit_acted: set) -> dict[str, Any]:
    """The journal's per-pair payload: open trades + trade-id sequence + the
    dedup keys (restored on restart so a journaled signal is never
    re-sent)."""
    return {
        "trade_seq": int(engine.trade_seq),
        "acted": sorted([int(ts), str(direction)] for ts, direction in acted),
        "exit_acted": sorted(int(ts) for ts in exit_acted),
        "trades": [serialize_trade(trade) for trade in engine.open_trades],
    }


# ---------------------------------------------------------------------------
# TraderStateJournal — the atomic on-disk journal
# ---------------------------------------------------------------------------

class TraderStateJournal:
    """
    The journal file: ``{"version", "trader_id", "pairs": {key: state}}``.

    ``load()`` reads an existing file (missing → empty journal; unreadable or
    structurally wrong → ``ValueError`` naming the file — fix or remove it,
    the venue positions themselves are unaffected).  ``write_pair()`` updates
    one pair's state and flushes **atomically** (same-directory tmp file +
    ``os.replace``); a flush whose serialized content is unchanged is
    skipped.  Values that are not JSON-serializable (exotic signal metadata)
    are stringified rather than failing the flush.

    ``write_pair()`` is thread-safe: a multi-pair ``Trader`` runs one
    consumer thread per pair, all flushing the same journal file.
    """

    def __init__(self, path: str | os.PathLike) -> None:
        self.path = os.fspath(path)
        self._data: dict[str, Any] = {
            "version": JOURNAL_VERSION, "trader_id": None, "pairs": {},
        }
        self._written: str | None = None
        self._lock = threading.Lock()

    def load(self) -> TraderStateJournal:
        """Read the journal from disk (missing file → empty journal)."""
        if not os.path.exists(self.path):
            return self
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict) or not isinstance(data.get("pairs"), dict):
                raise ValueError("not a trader state journal object")
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"Trader: state journal {self.path!r} is unreadable or corrupt "
                f"({exc}) — fix or remove the file before starting (positions on "
                "the venue are unaffected; reconciliation needs a valid journal)."
            ) from exc
        self._data = {
            "version": int(data.get("version", JOURNAL_VERSION)),
            "trader_id": data.get("trader_id"),
            "pairs": data["pairs"],
        }
        self._written = self._dump()
        return self

    @property
    def trader_id(self) -> str | None:
        """The session id journaled with this file (reused across restarts)."""
        value = self._data.get("trader_id")
        return str(value) if value else None

    @trader_id.setter
    def trader_id(self, value: str | None) -> None:
        self._data["trader_id"] = value

    def pair_state(self, key: str) -> dict[str, Any] | None:
        """The journaled state of one pair, or ``None`` when never written."""
        return self._data["pairs"].get(key)

    def write_pair(self, key: str, state: dict[str, Any]) -> bool:
        """Store *state* under *key* and flush; returns ``True`` when the
        file was actually (re)written.  Thread-safe."""
        with self._lock:
            self._data["pairs"][key] = state
            return self._flush()

    def _dump(self) -> str:
        return json.dumps(self._data, sort_keys=True, default=str)

    def _flush(self) -> bool:
        text = self._dump()
        if text == self._written:
            return False
        tmp_path = f"{self.path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_path, self.path)
        self._written = text
        return True


# ---------------------------------------------------------------------------
# Startup reconciliation
# ---------------------------------------------------------------------------

def reconcile_startup(worker: Any) -> None:
    """
    Reconcile the worker's journal against the venue — called by
    ``_PairWorker.start()`` after ``engine.start()`` and **before** any
    producer thread exists, so nothing can trade past an unreconciled state.

    Restores the dedup keys and the trade-id sequence, adopts / offline-
    closes every journaled trade per the module-docstring rules, warns about
    foreign positions, and emits one ``RECONCILE`` event whenever there was
    a journal to reconcile or anything foreign to report.  A ``BrokerError``
    from the venue reads propagates — the start is aborted.
    """
    journal = getattr(worker, "journal", None)
    stored = journal.pair_state(worker.pair_key) if journal is not None else None
    engine = worker.engine
    journaled: list[LiveTrade] = []
    if stored:
        worker._acted.update(
            (int(ts), str(direction)) for ts, direction in stored.get("acted", [])
        )
        worker._exit_acted.update(int(ts) for ts in stored.get("exit_acted", []))
        engine.ensure_trade_seq(int(stored.get("trade_seq", 0)))
        journaled = [restore_trade(payload) for payload in stored.get("trades", [])]

    adopted: list[str] = []
    closed_offline: list[str] = []
    foreign: list[str] = []
    if worker.market == MARKET_FOREX:
        _reconcile_mt5(worker, journaled, adopted, closed_offline, foreign)
    elif worker.market == MARKET_FUTURES:
        _reconcile_futures(worker, journaled, adopted, closed_offline, foreign)
    elif worker.market == MARKET_SPOT:
        _reconcile_spot(worker, journaled, adopted, closed_offline)

    for label in foreign:
        worker._log(
            f"foreign venue position ({label}) is not in the journal — left "
            "untouched, not managed (close it on the venue yourself if unwanted)."
        )
    if stored is not None or adopted or closed_offline or foreign:
        worker.events.emit(ReconcileEvent(
            time=now_ms(), symbol=worker.config.symbol, source=SOURCE_LIVE,
            adopted=tuple(adopted), closed_offline=tuple(closed_offline),
            foreign=tuple(foreign),
        ))
    if adopted or closed_offline:
        worker._log(
            f"restart reconciliation: {len(adopted)} position(s) adopted, "
            f"{len(closed_offline)} closed while offline, {len(foreign)} foreign."
        )


# ------------------------------ shared helpers ------------------------------

def _fetch_fills(worker: Any, journaled: list[LiveTrade]) -> dict[str, tuple[float, float, int]]:
    """Venue trade history since the oldest journaled entry, aggregated per
    order: ``order_id → (qty-weighted price, total qty, last fill time)``.
    Spot OCO legs are additionally keyed ``"list:<orderListId>"`` (the
    journal knows the list id, not the leg ids).  An unreadable history
    degrades to the ticker fallback (``ERROR`` emitted), never aborts."""
    if not journaled or not hasattr(worker.broker, "my_trades"):
        return {}
    start_ms = min(trade.pos.open_time for trade in journaled)
    try:
        rows = worker.broker.my_trades(worker.config.symbol, start_ms=start_ms)
    except BrokerError as exc:
        worker._error(
            "reconcile",
            f"trade-history read failed ({exc}) — offline exit fills fall back "
            "to the ticker price.",
        )
        return {}
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        order_id = str(row.get("orderId", "") or "")
        if not order_id:
            continue
        grouped.setdefault(order_id, []).append(row)
        list_id = str(row.get("orderListId", "") or "")
        if list_id and list_id != "-1":
            grouped.setdefault(f"list:{list_id}", []).append(row)
    fills: dict[str, tuple[float, float, int]] = {}
    for key, group in grouped.items():
        qty = sum(float(row.get("qty", 0.0) or 0.0) for row in group)
        if qty <= 0.0:
            continue
        price = sum(
            float(row.get("price", 0.0) or 0.0) * float(row.get("qty", 0.0) or 0.0)
            for row in group
        ) / qty
        fill_time = max(int(row.get("time", 0) or 0) for row in group) or now_ms()
        fills[key] = (price, qty, fill_time)
    return fills


# ------------------------------ MetaTrader ---------------------------------

def _reconcile_mt5(worker, journaled, adopted, closed_offline, foreign) -> None:
    engine = worker.engine
    positions = worker.broker.open_positions(worker.config.symbol)
    by_ticket = {str(p.position_id): p for p in positions}
    for trade in journaled:
        engine.adopt_trade(trade)
        venue = by_ticket.pop(str(trade.mt5_ticket), None) if trade.mt5_ticket else None
        if venue is None:
            _close_offline_mt5(worker, trade)
            closed_offline.append(trade.client_order_id)
            continue
        if float(venue.quantity) < trade.pos.size - QTY_EPSILON:
            # The position shrank while offline — a manual partial close.
            closed_size = trade.pos.size - float(venue.quantity)
            price, reason, close_time = worker._mt5_exit_fill(trade)
            engine.record_external_close(
                trade, price, reason, close_time=close_time, closed_size=closed_size
            )
        _repair_mt5_protection(worker, trade, venue)
        adopted.append(trade.client_order_id)
    for ticket, venue in by_ticket.items():
        comment = str(venue.raw.get("comment", "") or "")
        foreign.append(f"ticket {ticket}" + (f" [{comment}]" if comment else ""))


def _close_offline_mt5(worker, trade: LiveTrade) -> None:
    """A journaled MT5 ticket is gone — settle its exit deals as records
    (several deals = offline partial closes sharing the trade id)."""
    engine = worker.engine
    deals: list[dict] = []
    try:
        if trade.mt5_ticket:
            deals = worker.broker.history_deals(position=int(trade.mt5_ticket))
    except (BrokerError, TypeError, ValueError):
        deals = []
    exits = [d for d in deals if int(d.get("entry", 0)) != MT5_DEAL_ENTRY_IN]
    exits.sort(key=lambda d: int(d.get("time_msc") or 0) or int(d.get("time", 0) or 0) * 1000)
    if not exits:
        engine.record_external_close(
            trade, worker._price_fallback(trade.pos), CLOSE_REASON_MANUAL
        )
        return
    last = len(exits) - 1
    for index, deal in enumerate(exits):
        if trade not in engine.open_trades:
            return
        price = float(deal.get("price", 0.0) or 0.0) or worker._price_fallback(trade.pos)
        close_time = (
            int(deal.get("time_msc") or 0)
            or int(deal.get("time", 0) or 0) * 1000
            or now_ms()
        )
        deal_reason = int(deal.get("reason", -1))
        if deal_reason == MT5_DEAL_REASON_SL:
            reason = sl_reason(trade.pos)
        elif deal_reason == MT5_DEAL_REASON_TP:
            reason = CLOSE_REASON_TP
        else:
            reason = CLOSE_REASON_MANUAL
        volume = float(deal.get("volume", 0.0) or 0.0)
        partial = index < last and 0.0 < volume < trade.pos.size - QTY_EPSILON
        engine.record_external_close(
            trade, price, reason, close_time=close_time,
            closed_size=volume if partial else None,
        )


def _repair_mt5_protection(worker, trade: LiveTrade, venue) -> None:
    """Re-arm SL/TP the MT5 position lost while offline — one
    ``modify_position`` carrying only the missing parts."""
    pos = trade.pos
    sl_missing = not float(venue.stop_loss or 0.0) and bool(pos.stop_loss)
    tp_missing = trade.venue_tp is not None and not float(venue.take_profit or 0.0)
    if not sl_missing and not tp_missing:
        return
    failed = False
    try:
        result = worker.broker.modify_position(
            int(trade.mt5_ticket),
            stop_loss=pos.stop_loss if sl_missing else None,
            take_profit=trade.venue_tp if tp_missing else None,
        )
        failed = getattr(result, "status", "") == STATUS_REJECTED
    except (BrokerError, TypeError, ValueError):
        failed = True
    if failed:
        if sl_missing:
            trade.pending_sl_sync = True
        worker._error(
            "reconcile",
            f"adopted position #{pos.trade_id}: venue protection could not be "
            "re-armed; the SL sync will be retried every closed candle.",
            will_retry=sl_missing, details={"trade_id": pos.trade_id},
        )
    else:
        worker._log(
            f"re-armed venue protection of adopted trade #{pos.trade_id} "
            f"(sl={pos.stop_loss if sl_missing else 'kept'}, "
            f"tp={trade.venue_tp if tp_missing else 'kept'})."
        )


# ------------------------------ Binance futures -----------------------------

def _reconcile_futures(worker, journaled, adopted, closed_offline, foreign) -> None:
    engine = worker.engine
    symbol = worker.config.symbol
    positions = worker.broker.open_positions(symbol)
    orders = worker.broker.open_orders(symbol) if journaled else []
    open_ids = {str(order.order_id) for order in orders}
    fills = _fetch_fills(worker, journaled)
    net = {"long": 0.0, "short": 0.0}
    for position in positions:
        side = "long" if position.side == POSITION_LONG else "short"
        net[side] += float(position.quantity)

    for trade in journaled:
        engine.adopt_trade(trade)
        _settle_offline_ladder(worker, trade, open_ids, fills)
        if trade not in engine.open_trades:
            closed_offline.append(trade.client_order_id)
            continue
        if _settle_offline_protection_fill(worker, trade, open_ids, fills):
            closed_offline.append(trade.client_order_id)
            continue
        pos = trade.pos
        available = net[pos.direction]
        take = min(pos.size, available)
        if take <= QTY_EPSILON:
            # Venue net exposure has no room for this trade — closed offline
            # outside our orders (manual); no fill to pin it to.
            engine.record_external_close(
                trade, worker._price_fallback(pos), CLOSE_REASON_MANUAL
            )
            worker._cancel_leftover_protection(trade)
            closed_offline.append(trade.client_order_id)
            continue
        if pos.size - take > QTY_EPSILON:
            # Partially covered — the uncovered part was reduced manually.
            engine.record_external_close(
                trade, worker._price_fallback(pos), CLOSE_REASON_MANUAL,
                closed_size=pos.size - take,
            )
        net[pos.direction] = available - take
        _repair_futures_protection(worker, trade, open_ids)
        if trade not in engine.open_trades:
            closed_offline.append(trade.client_order_id)   # emergency-closed re-arm
            continue
        _replace_missing_ladder(worker, trade, open_ids, fills)
        adopted.append(trade.client_order_id)

    for direction in ("long", "short"):
        if net[direction] > QTY_EPSILON:
            foreign.append(f"{symbol} {direction} {net[direction]:g}")


def _settle_offline_ladder(worker, trade: LiveTrade, open_ids, fills) -> None:
    """Ladder orders that filled while offline settle exactly like live fills
    (: slice + ladder SL move), in level order."""
    for order_id, _level in sorted(trade.ladder_order_ids.items(), key=lambda kv: kv[1]):
        if trade not in worker.engine.open_trades:
            return
        if order_id in open_ids:
            continue
        aggregate = fills.get(order_id)
        if aggregate is None:
            continue   # vanished unfilled — _replace_missing_ladder re-places it
        price, qty, fill_time = aggregate
        worker.engine.record_ladder_fill(trade, order_id, price, qty, close_time=fill_time)


def _settle_offline_protection_fill(worker, trade: LiveTrade, open_ids, fills) -> bool:
    """A journaled SL/TP order that filled while offline closes the trade at
    the real historical fill.  Returns True when the trade fully closed."""
    for which, order_id in (("sl", trade.sl_order_id), ("tp", trade.tp_order_id)):
        if not order_id or order_id in open_ids:
            continue
        aggregate = fills.get(str(order_id))
        if aggregate is None:
            continue
        price, qty, fill_time = aggregate
        reason = sl_reason(trade.pos) if which == "sl" else CLOSE_REASON_TP
        partial = 0.0 < qty < trade.pos.size - QTY_EPSILON
        worker.engine.record_external_close(
            trade, price, reason, close_time=fill_time,
            closed_size=qty if partial else None,
        )
        if not partial:
            worker._cancel_leftover_protection(trade, filled=which)
            return True
    return False


def _repair_futures_protection(worker, trade: LiveTrade, open_ids) -> None:
    """Vanished (unfilled) protective orders are re-armed immediately."""
    engine = worker.engine
    pos = trade.pos
    if trade.sl_order_id and trade.sl_order_id not in open_ids:
        trade.sl_order_id = None   # stale id — nothing to cancel venue-side
        if engine.resync_stop(trade):
            worker._log(
                f"re-armed the venue SL of adopted trade #{pos.trade_id} "
                f"at {pos.stop_loss:g} (the journaled stop order was gone)."
            )
        if trade not in engine.open_trades:
            return
    if trade.venue_tp is not None and trade.tp_order_id and trade.tp_order_id not in open_ids:
        trade.tp_order_id = None
        if engine.rearm_take_profit(trade):
            worker._log(
                f"re-attached the venue TP of adopted trade #{pos.trade_id} "
                f"at {trade.venue_tp:g}."
            )


def _replace_missing_ladder(worker, trade: LiveTrade, open_ids, fills) -> None:
    """Vanished **unfilled** ladder levels are re-placed so the RR ladder
    continues where it left off; a re-place failure degrades that level to
    feed detection (policy)."""
    engine = worker.engine
    missing = [
        (order_id, level_idx)
        for order_id, level_idx in trade.ladder_order_ids.items()
        if order_id not in open_ids and order_id not in fills
    ]
    if not missing:
        return
    plan = {
        level_idx: (price, quantity)
        for level_idx, price, quantity in engine._ladder_plan(trade.pos)
    }
    for order_id, level_idx in sorted(missing, key=lambda kv: kv[1]):
        trade.ladder_order_ids.pop(order_id, None)
        entry = plan.get(level_idx)
        if entry is None or level_idx < trade.pos.last_rr_hit:
            continue   # level already realized/advanced past — nothing to re-place
        price, quantity = entry
        if engine.place_ladder_level(trade, level_idx, price, min(quantity, trade.pos.size)):
            worker._log(
                f"re-placed the vanished ladder level {level_idx + 1} order of "
                f"adopted trade #{trade.pos.trade_id} at {price:g}."
            )


# ------------------------------ Binance spot --------------------------------

def _reconcile_spot(worker, journaled, adopted, closed_offline) -> None:
    """Spot has no venue positions: presence == the protective orders are
    still working (rule).  A vanished protection is recorded as closed —
    at the historical protective fill when the venue history shows one, else
    at the ticker with reason ``manual``.  Foreign holdings are not
    detectable (a wallet balance is not a position)."""
    engine = worker.engine
    if not journaled:
        return
    orders = worker.broker.open_orders(worker.config.symbol)
    open_ids = {str(order.order_id) for order in orders}
    open_lists = {str(order.raw.get("orderListId", "")) for order in orders}
    fills = _fetch_fills(worker, journaled)
    for trade in journaled:
        engine.adopt_trade(trade)
        protected = (trade.sl_order_id and trade.sl_order_id in open_ids) or (
            trade.oco_order_list_id and str(trade.oco_order_list_id) in open_lists
        )
        if protected:
            adopted.append(trade.client_order_id)
            continue
        aggregate = fills.get(str(trade.sl_order_id)) if trade.sl_order_id else None
        if aggregate is None and trade.oco_order_list_id:
            aggregate = fills.get(f"list:{trade.oco_order_list_id}")
        if aggregate is None:
            price, reason, close_time = (
                worker._price_fallback(trade.pos), CLOSE_REASON_MANUAL, now_ms()
            )
        else:
            price, _qty, close_time = aggregate
            pos = trade.pos
            if trade.venue_tp is not None and abs(price - trade.venue_tp) <= abs(
                price - pos.stop_loss
            ):
                reason = CLOSE_REASON_TP   # the fill sits at the TP leg's price
            else:
                reason = sl_reason(pos)
        engine.record_external_close(trade, price, reason, close_time=close_time)
        closed_offline.append(trade.client_order_id)
