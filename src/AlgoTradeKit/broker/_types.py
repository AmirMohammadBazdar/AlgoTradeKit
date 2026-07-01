"""
Unified data types shared by every ``broker`` connector.

The whole point of the ``broker`` module is that a Binance spot account, a
Binance USD-M futures account, and a MetaTrader forex account all speak the
**same** vocabulary to the rest of AlgoTradeKit.  These dataclasses are that
vocabulary; each concrete connector maps its venue-specific payloads onto them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# ---------------------------------------------------------------------------
# String constants (re-exported from broker/__init__.py, mirroring the
# `simulate` module's constant convention)
# ---------------------------------------------------------------------------

# Order side
SIDE_BUY = "buy"
SIDE_SELL = "sell"

# Order type
ORDER_MARKET = "market"
ORDER_LIMIT = "limit"
ORDER_STOP = "stop"          # stop-market
ORDER_STOP_LIMIT = "stop_limit"

# Time in force
TIF_GTC = "gtc"
TIF_IOC = "ioc"
TIF_FOK = "fok"

# Position direction
POSITION_LONG = "long"
POSITION_SHORT = "short"

# Market kind (what the connector trades)
MARKET_SPOT = "spot"
MARKET_FUTURES = "futures"
MARKET_FOREX = "forex"

# Order status (normalised)
STATUS_NEW = "new"
STATUS_PARTIALLY_FILLED = "partially_filled"
STATUS_FILLED = "filled"
STATUS_CANCELED = "canceled"
STATUS_REJECTED = "rejected"
STATUS_EXPIRED = "expired"


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Balance:
    """A single asset / currency balance."""
    asset: str
    free: float           # available to trade / withdraw
    locked: float = 0.0   # held in open orders
    total: float = 0.0    # free + locked (or wallet balance for margin accounts)

    def __post_init__(self) -> None:
        if not self.total:
            object.__setattr__(self, "total", self.free + self.locked)


@dataclass(frozen=True)
class AccountInfo:
    """
    Account-level snapshot.  ``balances`` is per-asset; the scalar fields are
    populated for margin/futures/MetaTrader accounts (a single account currency)
    and left at ``0.0`` for plain spot.
    """
    balances: tuple[Balance, ...] = ()
    currency: str = ""              # account currency (e.g. "USDT", "USD")
    equity: float = 0.0             # balance + unrealised PnL
    wallet_balance: float = 0.0
    available: float = 0.0          # free margin / available for new orders
    margin_used: float = 0.0
    unrealized_pnl: float = 0.0
    leverage: float = 1.0
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class Ticker:
    """Latest price snapshot for a symbol."""
    symbol: str
    last: float
    bid: float = 0.0
    ask: float = 0.0
    timestamp: int = 0              # UTC ms
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class Order:
    """A working / historical order (unified across venues)."""
    order_id: str
    symbol: str
    side: str                       # SIDE_BUY / SIDE_SELL
    type: str                       # ORDER_MARKET / ORDER_LIMIT / ...
    quantity: float
    price: Optional[float] = None   # limit / stop price; None for pure market
    stop_price: Optional[float] = None
    status: str = STATUS_NEW
    filled_quantity: float = 0.0
    timestamp: int = 0              # UTC ms
    client_order_id: str = ""
    reduce_only: bool = False
    time_in_force: str = TIF_GTC
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class OrderResult:
    """Outcome of a create-order request."""
    order_id: str
    symbol: str
    side: str
    status: str
    filled_quantity: float = 0.0
    avg_fill_price: float = 0.0
    client_order_id: str = ""
    dry_run: bool = False           # True → was NOT sent to the venue
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_filled(self) -> bool:
        return self.status == STATUS_FILLED


@dataclass(frozen=True)
class Position:
    """
    An open leveraged position (Binance futures) or a MetaTrader position.

    ``quantity`` is in the venue's native unit — base-asset contracts for
    crypto futures, lots for MetaTrader.
    """
    symbol: str
    side: str                       # POSITION_LONG / POSITION_SHORT
    quantity: float
    entry_price: float
    mark_price: float = 0.0
    unrealized_pnl: float = 0.0
    leverage: float = 1.0
    liquidation_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    position_id: str = ""           # futures: symbol; MT: ticket
    timestamp: int = 0              # UTC ms (open time when known)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)
