"""
AlgoTradeKit.simulate._lot
~~~~~~~~~~~~~~~~~~~~~~~~~~~
MetaTrader 5 lot-size calculation for the simulate engine.

Each instrument category has its own pip definition and pip-value formula.
The lot size is calculated so that hitting the stop-loss costs exactly
``risk_amount`` in the account (USD) currency.

Public API
----------
``calculate_mt5_lot(symbol, risk_amount, sl_distance, current_price)``
    Returns the lot size (float) for the given parameters.

``round_lot(lot, step, min_lot, max_lot)``
    Rounds a raw lot to the broker's step and enforces min/max limits.

All pip values are computed for a *standard lot* (100 000 units for forex,
100 oz for gold, etc.).  Exotic/Scandinavian pairs approximate the
quote-currency-to-USD exchange rate; these can be overridden by passing a
``usd_rates`` dict.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Default approximate USD cross-rates
# Used when the quote currency is not USD and no live rate is available.
# ---------------------------------------------------------------------------

_DEFAULT_USD_RATES: dict[str, float] = {
    # Key = quote currency ISO code; value = quote units per 1 USD
    "chf": 0.88,
    "cad": 1.35,
    "aud": 1.50,   # i.e. 1 USD ≈ 1.50 AUD
    "nzd": 1.70,
    "gbp": 0.75,
    "eur": 0.92,
    "hkd": 7.85,
    "sgd": 1.35,
    "jpy": 150.0,
    "try": 32.0,
    "zar": 18.5,
    "mxn": 17.0,
    "pln": 4.0,
    "huf": 360.0,
    "czk": 22.5,
    "sek": 10.5,
    "nok": 10.5,
    "dkk": 6.9,
}

# Standard lot sizes
_FOREX_LOT = 100_000          # 100 000 base currency units
_GOLD_LOT = 100               # 100 troy oz  (XAUUSD)
_SILVER_LOT = 5_000           # 5 000 oz     (XAGUSD)
_PLATINUM_LOT = 100           # 100 oz       (XPTUSD, XPDUSD)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def round_lot(
    lot: float,
    step: float = 0.01,
    min_lot: float = 0.01,
    max_lot: float = 100.0,
) -> float:
    """
    Round *lot* to the nearest *step* and clamp to [*min_lot*, *max_lot*].

    Parameters
    ----------
    lot : float
        Raw calculated lot size.
    step : float
        Broker's lot step (default: 0.01).
    min_lot : float
        Broker's minimum lot size (default: 0.01).
    max_lot : float
        Broker's maximum lot size (default: 100.0).

    Returns
    -------
    float
        Validated lot size.
    """
    if step <= 0:
        raise ValueError("round_lot: step must be > 0.")
    rounded = round(round(lot / step) * step, 10)
    return max(min_lot, min(max_lot, rounded))


def calculate_mt5_lot(
    symbol: str,
    risk_amount: float,
    sl_distance: float,
    current_price: float,
    usd_rates: dict[str, float] | None = None,
) -> float:
    """
    Calculate the MT5 lot size so that hitting the stop-loss costs
    exactly *risk_amount* USD.

    Parameters
    ----------
    symbol : str
        Instrument symbol, case-insensitive (e.g. ``"eurusd"``, ``"xauusd"``).
    risk_amount : float
        Dollar amount to risk (e.g. 100.0 = $100).
    sl_distance : float
        ``|entry_price - stop_loss|`` in price units.
    current_price : float
        Current market price (used for JPY / exotic pairs to convert pips).
    usd_rates : dict[str, float] | None
        Optional override for quote-currency-to-USD rates.
        Keys are lowercase ISO currency codes (e.g. ``{"cad": 1.36}``).

    Returns
    -------
    float
        Calculated lot size (raw, not rounded).
    """
    if sl_distance <= 0:
        raise ValueError("calculate_mt5_lot: sl_distance must be > 0.")
    if risk_amount <= 0:
        raise ValueError("calculate_mt5_lot: risk_amount must be > 0.")

    rates = dict(_DEFAULT_USD_RATES)
    if usd_rates:
        rates.update({k.lower(): v for k, v in usd_rates.items()})

    sym = symbol.lower()

    # ------------------------------------------------------------------ #
    # 1. Determine pip size and pip value per standard lot (in USD)        #
    # ------------------------------------------------------------------ #

    # --- JPY pairs (XXX/JPY) ---
    if sym in {
        "usdjpy", "eurjpy", "gbpjpy", "audjpy", "nzdjpy",
        "cadjpy", "chfjpy", "sgdjpy", "hkdjpy",
    }:
        pip_size = 0.01
        pip_value = (pip_size / current_price) * _FOREX_LOT   # USD per pip per lot

    # --- Standard USD-quote pairs (XXX/USD) ---
    elif sym in {"eurusd", "gbpusd", "audusd", "nzdusd"}:
        pip_size = 0.0001
        pip_value = 10.0   # fixed: $10 per pip per standard lot

    # --- USD-base pairs (USD/XXX, non-JPY) ---
    elif sym in {"usdchf", "usdcad"}:
        pip_size = 0.0001
        quote = sym[3:]    # "chf" or "cad"
        q_per_usd = rates.get(quote, 1.0)
        pip_value = (pip_size / current_price) * _FOREX_LOT * (1.0 / q_per_usd) if q_per_usd else 10.0

    # --- CHF-quote pairs (XXX/CHF) ---
    elif sym in {"eurchf", "gbpchf", "audchf", "nzdchf", "cadchf"}:
        pip_size = 0.0001
        q_per_usd = rates.get("chf", 0.88)
        pip_value = pip_size * _FOREX_LOT / q_per_usd

    # --- CAD-quote pairs (XXX/CAD) ---
    elif sym in {"eurcad", "gbpcad", "audcad", "nzdcad"}:
        pip_size = 0.0001
        q_per_usd = rates.get("cad", 1.35)
        pip_value = pip_size * _FOREX_LOT / q_per_usd

    # --- AUD-quote pairs (XXX/AUD) ---
    elif sym in {"euraud", "gbpaud", "nzdaud"}:
        pip_size = 0.0001
        q_per_usd = rates.get("aud", 1.50)
        pip_value = pip_size * _FOREX_LOT / q_per_usd

    # --- NZD-quote pairs (XXX/NZD) ---
    elif sym in {"eurnzd", "gbpnzd", "audnzd"}:
        pip_size = 0.0001
        q_per_usd = rates.get("nzd", 1.70)
        pip_value = pip_size * _FOREX_LOT / q_per_usd

    # --- GBP-quote pairs (XXX/GBP) ---
    elif sym in {"eurgbp"}:
        pip_size = 0.0001
        q_per_usd = rates.get("gbp", 0.75)
        pip_value = pip_size * _FOREX_LOT / q_per_usd

    # --- HKD-quote pairs ---
    elif sym in {"usdhkd", "eurhkd", "gbphkd"}:
        pip_size = 0.0001
        q_per_usd = rates.get("hkd", 7.85)
        pip_value = pip_size * _FOREX_LOT / q_per_usd

    # --- Exotic pairs (larger pip size) ---
    elif sym in {"usdzar", "usdtry", "usdmxn"}:
        pip_size = 0.001
        pip_value = (pip_size / current_price) * _FOREX_LOT

    # --- Eastern European currencies ---
    elif sym in {
        "eurpln", "usdpln", "eurhuf", "usdhuf",
        "eurczk", "usdczk", "eurrub", "usdrub",
    }:
        pip_size = 0.001
        pip_value = (pip_size / current_price) * _FOREX_LOT

    # --- Scandinavian currencies ---
    elif sym in {
        "eursek", "usdsek", "eurnok", "usdnok", "eurdkk", "usddkk",
    }:
        pip_size = 0.001
        pip_value = (pip_size / current_price) * _FOREX_LOT

    # ------------------------------------------------------------------ #
    # 2. Metals                                                            #
    # ------------------------------------------------------------------ #

    elif sym == "xauusd":
        pip_size = 0.01         # $0.01 per ounce
        pip_value = pip_size * _GOLD_LOT   # $0.01 × 100 = $1 per pip

    elif sym == "xagusd":
        pip_size = 0.001
        pip_value = pip_size * _SILVER_LOT  # $0.001 × 5000 = $5 per pip

    elif sym in {"xptusd", "xpdusd"}:
        pip_size = 0.01
        pip_value = pip_size * _PLATINUM_LOT  # $1 per pip

    # ------------------------------------------------------------------ #
    # 3. Cryptocurrencies                                                  #
    # ------------------------------------------------------------------ #

    elif sym in {
        "btcusd", "xbtusd",
        "ethusd", "xetusd",
        "xrpusd", "ltcusd", "bchusd",
        "adausd", "dotusd", "solusd",
        "bnbusd", "maticusd", "lnkusd",
    }:
        # 1 lot = 1 unit of base crypto; pip = $1 move → pip_value = 1
        pip_size = 1.0
        pip_value = 1.0

    # ------------------------------------------------------------------ #
    # 4. Indices                                                           #
    # ------------------------------------------------------------------ #

    elif sym in {
        "us30", "us100", "us500", "spx500",
        "ger40", "dax40", "uk100", "fra40", "esp35",
        "jpx500", "jp225", "hk50", "aus200",
        "jpxjpy", "hkxhkd",
    }:
        # $1 per index point per lot (broker-dependent; common default)
        pip_size = 1.0
        pip_value = 1.0

    # ------------------------------------------------------------------ #
    # 5. Oil / Commodities                                                 #
    # ------------------------------------------------------------------ #

    elif sym in {"wtiusd", "usoil", "ukoil", "xtiusd", "xbrusd"}:
        pip_size = 0.01
        pip_value = 10.0   # $10 per pip per lot (1000 barrels × $0.01)

    elif sym in {"natgas", "xngusd"}:
        pip_size = 0.001
        pip_value = 10.0

    # ------------------------------------------------------------------ #
    # 6. Default fallback                                                   #
    # ------------------------------------------------------------------ #

    else:
        pip_size = 0.0001
        pip_value = 10.0

    # ------------------------------------------------------------------ #
    # 7. Lot calculation                                                    #
    # ------------------------------------------------------------------ #

    pips = sl_distance / pip_size
    if pips <= 0 or pip_value <= 0:
        return 0.0

    return risk_amount / (pips * pip_value)


def get_mt5_pip_info(
    symbol: str,
    current_price: float,
    usd_rates: dict[str, float] | None = None,
) -> tuple[float, float]:
    """
    Return ``(pip_size, pip_value_per_standard_lot)`` for *symbol*.

    Used when position sizing is ``"fixed_lot"`` and the engine needs to
    derive ``pnl_per_price_unit`` from a known lot size rather than from
    a risk amount.

    Parameters
    ----------
    symbol : str
        Instrument symbol, case-insensitive.
    current_price : float
        Current market price (required for JPY / exotic cross-rate conversion).
    usd_rates : dict[str, float] | None
        Optional override for quote-currency-to-USD rates.

    Returns
    -------
    tuple[float, float]
        ``(pip_size, pip_value_per_standard_lot_in_USD)``
    """
    rates = dict(_DEFAULT_USD_RATES)
    if usd_rates:
        rates.update({k.lower(): v for k, v in usd_rates.items()})

    sym = symbol.lower()

    # JPY pairs
    if sym in {
        "usdjpy", "eurjpy", "gbpjpy", "audjpy", "nzdjpy",
        "cadjpy", "chfjpy", "sgdjpy", "hkdjpy",
    }:
        pip_size = 0.01
        pip_value = (pip_size / current_price) * _FOREX_LOT
        return pip_size, pip_value

    # Standard USD-quote pairs
    if sym in {"eurusd", "gbpusd", "audusd", "nzdusd"}:
        return 0.0001, 10.0

    # USD-base pairs
    if sym in {"usdchf", "usdcad"}:
        quote = sym[3:]
        q_per_usd = rates.get(quote, 1.0)
        pip_value = (0.0001 / current_price) * _FOREX_LOT * (1.0 / q_per_usd) if q_per_usd else 10.0
        return 0.0001, pip_value

    # CHF-quote
    if sym in {"eurchf", "gbpchf", "audchf", "nzdchf", "cadchf"}:
        return 0.0001, 0.0001 * _FOREX_LOT / rates.get("chf", 0.88)

    # CAD-quote
    if sym in {"eurcad", "gbpcad", "audcad", "nzdcad"}:
        return 0.0001, 0.0001 * _FOREX_LOT / rates.get("cad", 1.35)

    # AUD-quote
    if sym in {"euraud", "gbpaud", "nzdaud"}:
        return 0.0001, 0.0001 * _FOREX_LOT / rates.get("aud", 1.50)

    # NZD-quote
    if sym in {"eurnzd", "gbpnzd", "audnzd"}:
        return 0.0001, 0.0001 * _FOREX_LOT / rates.get("nzd", 1.70)

    # GBP-quote
    if sym in {"eurgbp"}:
        return 0.0001, 0.0001 * _FOREX_LOT / rates.get("gbp", 0.75)

    # Exotic
    if sym in {"usdzar", "usdtry", "usdmxn"}:
        return 0.001, (0.001 / current_price) * _FOREX_LOT

    # Eastern European
    if sym in {"eurpln", "usdpln", "eurhuf", "usdhuf", "eurczk", "usdczk"}:
        return 0.001, (0.001 / current_price) * _FOREX_LOT

    # Scandinavian
    if sym in {"eursek", "usdsek", "eurnok", "usdnok", "eurdkk", "usddkk"}:
        return 0.001, (0.001 / current_price) * _FOREX_LOT

    # Gold
    if sym == "xauusd":
        return 0.01, 0.01 * _GOLD_LOT

    # Silver
    if sym == "xagusd":
        return 0.001, 0.001 * _SILVER_LOT

    # Platinum/Palladium
    if sym in {"xptusd", "xpdusd"}:
        return 0.01, 0.01 * _PLATINUM_LOT

    # Crypto (1 unit = $1 move)
    if sym in {
        "btcusd", "xbtusd", "ethusd", "xetusd",
        "xrpusd", "ltcusd", "bchusd", "adausd", "dotusd", "solusd",
    }:
        return 1.0, 1.0

    # Indices
    if sym in {
        "us30", "us100", "us500", "spx500",
        "ger40", "dax40", "uk100", "fra40", "esp35",
        "jpx500", "jp225", "hk50", "aus200",
    }:
        return 1.0, 1.0

    # Oil
    if sym in {"wtiusd", "usoil", "ukoil", "xtiusd", "xbrusd"}:
        return 0.01, 10.0

    # Natural Gas
    if sym in {"natgas", "xngusd"}:
        return 0.001, 10.0

    # Default
    return 0.0001, 10.0
