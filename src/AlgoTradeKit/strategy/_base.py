"""
AlgoTradeKit.strategy._base
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
BaseStrategy — the abstract foundation every strategy must extend.

Lifecycle (called in order by ``run()``)
-----------------------------------------
1. ``prepare_indicators(data)``  — compute and attach all indicator columns.
2. ``setup(data)``               — initialise any stateful variables.
3. ``generate_signals(i, data)`` — per-candle entry detection (main loop).
4. ``detect_exit_signals(i, data)`` — per-candle exit detection (optional).

Run modes
---------
BACKTEST  Every candle is processed; all signals are returned.
LIVE      Every candle is still processed (stateful strategies need the full
          history to build correct state), but only signals whose timestamp
          matches the LAST candle of the primary timeframe are returned.
          This mirrors the simulate/trade contract exactly.

State management note
---------------------
Initialise **all** stateful variables (lists, counters, flags) inside
``setup()``, **not** ``__init__()``.  ``setup()`` is called at the start of
every ``run()`` call, ensuring a clean slate even if the same strategy
instance is reused across multiple runs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import pandas as pd

from ._types import ExitSignal, Signal, StrategyMode, StrategyResult

# Required columns that every OHLCV DataFrame must have
_REQUIRED_COLS = frozenset({"timestamp", "open", "high", "low", "close", "volume"})


class BaseStrategy(ABC):
    """
    Abstract base class for all AlgoTradeKit strategies.

    Subclassing
    -----------
    1. Declare ``primary_timeframe`` as a class attribute (default ``"1h"``).
    2. Implement ``prepare_indicators(data)`` — add indicator columns.
    3. Implement ``generate_signals(candle_index, data)`` — return entry signals.
    4. Optionally override ``setup(data)`` for stateful initialisation.
    5. Optionally override ``detect_exit_signals(candle_index, data)`` for exits.

    Calling the strategy
    --------------------
    ::

        strategy = MyStrategy()
        result = strategy.run(data)           # BACKTEST — returns all signals
        result = strategy.run(data, mode=StrategyMode.LIVE)  # LIVE — last candle only

    Multi-timeframe
    ---------------
    Pass a dict of DataFrames keyed by timeframe string::

        data = {
            "1m":  df_1m,
            "15m": df_15m,
        }
        result = strategy.run(data)

    If a plain DataFrame is passed it is wrapped as
    ``{primary_timeframe: df}`` automatically.
    """

    # ------------------------------------------------------------------
    # Class attributes — override in subclass
    # ------------------------------------------------------------------

    #: Timeframe the main loop iterates over.
    primary_timeframe: str = "1h"

    #: Skip this many candles at the start of the primary timeframe before
    #: calling ``generate_signals``.  The strategy is still responsible for
    #: checking NaN values in indicator columns; this is a convenience guard.
    warmup_period: int = 0

    # ------------------------------------------------------------------
    # Abstract interface — MUST implement
    # ------------------------------------------------------------------

    @abstractmethod
    def prepare_indicators(
        self, data: dict[str, pd.DataFrame]
    ) -> dict[str, pd.DataFrame]:
        """
        Compute and attach all technical indicators.

        Called **once** before the main loop.  Receives the complete data dict
        and must return it with indicator columns added to each DataFrame.

        Parameters
        ----------
        data : dict[str, pd.DataFrame]
            Keyed by timeframe string.  Each DataFrame contains at least:
            ``timestamp``, ``open``, ``high``, ``low``, ``close``, ``volume``.

        Returns
        -------
        dict[str, pd.DataFrame]
            Same dict with indicator columns added.  Using ``.copy()`` on
            each DataFrame is recommended to avoid mutating the caller's data.

        Example
        -------
        ::

            def prepare_indicators(self, data):
                df = data["1h"].copy()
                macd = MACD(df["close"])
                df["_macd"]   = macd.macd.values
                df["_signal"] = macd.signal.values
                data["1h"] = df
                return data
        """

    @abstractmethod
    def generate_signals(
        self, candle_index: int, data: dict[str, pd.DataFrame]
    ) -> list[Signal]:
        """
        Core strategy logic — called once per candle of the primary timeframe.

        Only data up to and including ``candle_index`` should be considered to
        avoid look-ahead bias::

            history = data[self.primary_timeframe].iloc[: candle_index + 1]

        The ``self.history()`` and ``self.latest_candle_at()`` helpers make
        multi-timeframe lookups convenient.

        State variables (set up in ``setup()``) are available as instance
        attributes and accumulate naturally as the loop progresses.

        Parameters
        ----------
        candle_index : int
            Index of the current candle in ``data[primary_timeframe]``.
        data : dict[str, pd.DataFrame]
            Full data dict with indicator columns already added.

        Returns
        -------
        list[Signal]
            Signals generated at this candle.  Usually 0 or 1 signals.
        """

    # ------------------------------------------------------------------
    # Optional interface — CAN override
    # ------------------------------------------------------------------

    def setup(self, data: dict[str, pd.DataFrame]) -> None:
        """
        Initialise strategy state.  Called **once** before the main loop.

        Override to set up any stateful variables your strategy needs.
        Using instance attributes here (not in ``__init__``) guarantees a
        clean state on every ``run()`` call::

            def setup(self, data):
                self.buy_poi_list  = []
                self.sell_poi_list = []
                self.trend         = "up"
        """

    def detect_exit_signals(
        self, candle_index: int, data: dict[str, pd.DataFrame]
    ) -> list[ExitSignal]:
        """
        Optional exit-condition detection.  Called once per candle after
        ``generate_signals``.

        Override to detect conditions that mean open positions should be
        closed (trend reversal, trailing stop hit, force-close, etc.).
        The simulate/trade module interprets these signals and acts on them.

        Returns
        -------
        list[ExitSignal]
            Exit signals at this candle.  Return empty list (default) when
            exit detection is not needed.
        """
        return []

    # ------------------------------------------------------------------
    # Framework entry point — do NOT override
    # ------------------------------------------------------------------

    def run(
        self,
        data: "dict[str, pd.DataFrame] | pd.DataFrame",
        mode: StrategyMode = StrategyMode.BACKTEST,
    ) -> StrategyResult:
        """
        Execute the strategy and return results.

        Parameters
        ----------
        data : dict[str, pd.DataFrame] | pd.DataFrame
            OHLCV data.  A plain DataFrame is wrapped as
            ``{primary_timeframe: data}`` automatically.
        mode : StrategyMode
            ``BACKTEST`` — collect all signals across all candles.
            ``LIVE`` — run all candles (for correct state), but return only
            signals whose timestamp matches the last candle of the primary
            timeframe.

        Returns
        -------
        StrategyResult
        """
        # ---- Normalise input ----------------------------------------
        if isinstance(data, pd.DataFrame):
            data = {self.primary_timeframe: data}

        self._validate_input(data)

        # ---- 1. Compute all indicators (once) -----------------------
        data = self.prepare_indicators(data)

        # ---- 2. Initialise state (once) -----------------------------
        self.setup(data)

        # ---- 3. Main loop — candle by candle on primary TF ----------
        primary_df = data[self.primary_timeframe]
        n = len(primary_df)

        all_signals: list[Signal] = []
        all_exits: list[ExitSignal] = []

        for i in range(n):
            if i < self.warmup_period:
                continue

            signals = self.generate_signals(i, data)
            exits = self.detect_exit_signals(i, data)

            if signals:
                all_signals.extend(signals)
            if exits:
                all_exits.extend(exits)

        # ---- 4. Filter for LIVE mode --------------------------------
        if mode is StrategyMode.LIVE and n > 0:
            last_ts = int(primary_df.iloc[-1]["timestamp"])
            all_signals = [s for s in all_signals if s.timestamp == last_ts]
            all_exits = [e for e in all_exits if e.timestamp == last_ts]

        return StrategyResult(
            signals=all_signals,
            exit_signals=all_exits,
            data=data,
            mode=mode,
        )

    # ------------------------------------------------------------------
    # Helper utilities available inside generate_signals / detect_exit_signals
    # ------------------------------------------------------------------

    def get_candle(
        self,
        candle_index: int,
        data: dict[str, pd.DataFrame],
        timeframe: str | None = None,
    ) -> pd.Series:
        """
        Return the current candle as a Series.

        Parameters
        ----------
        candle_index : int
            Index relative to the primary timeframe (or the supplied timeframe).
        timeframe : str | None
            Timeframe key.  Defaults to ``primary_timeframe``.
        """
        tf = timeframe or self.primary_timeframe
        return data[tf].iloc[candle_index]

    def latest_candle_at(
        self,
        timeframe: str,
        timestamp: int,
        data: dict[str, pd.DataFrame],
    ) -> pd.Series | None:
        """
        Return the most recent closed candle in ``timeframe`` whose open-time
        is **at or before** ``timestamp``.

        Useful for cross-timeframe lookups while iterating the primary TF::

            def generate_signals(self, i, data):
                curr = data[self.primary_timeframe].iloc[i]
                htf  = self.latest_candle_at("4h", curr["timestamp"], data)
                if htf is not None and htf["close"] > htf["open"]:
                    ...

        Returns ``None`` if no candle satisfies the constraint.
        """
        df = data[timeframe]
        mask = df["timestamp"] <= timestamp
        if not mask.any():
            return None
        return df[mask].iloc[-1]

    def history(
        self,
        candle_index: int,
        data: dict[str, pd.DataFrame],
        timeframe: str | None = None,
        lookback: int | None = None,
    ) -> pd.DataFrame:
        """
        Return historical candles up to and including ``candle_index``.

        Parameters
        ----------
        candle_index : int
            Current candle index in the primary timeframe.
        timeframe : str | None
            Timeframe to query.  Defaults to ``primary_timeframe``.
        lookback : int | None
            How many candles to return.  ``None`` returns the full history.
        """
        tf = timeframe or self.primary_timeframe
        df = data[tf].iloc[: candle_index + 1]
        if lookback is not None:
            df = df.iloc[-lookback:]
        return df

    # ------------------------------------------------------------------
    # Internal validation
    # ------------------------------------------------------------------

    def _validate_input(self, data: dict[str, pd.DataFrame]) -> None:
        if self.primary_timeframe not in data:
            raise ValueError(
                f"[AlgoTradeKit] Strategy '{self.__class__.__name__}': "
                f"primary_timeframe '{self.primary_timeframe}' not found in data. "
                f"Available keys: {sorted(data.keys())}"
            )

        for tf, df in data.items():
            if not isinstance(df, pd.DataFrame):
                raise TypeError(
                    f"[AlgoTradeKit] data['{tf}'] must be a pandas DataFrame, "
                    f"got {type(df).__name__}."
                )
            missing = _REQUIRED_COLS - set(df.columns)
            if missing:
                raise ValueError(
                    f"[AlgoTradeKit] data['{tf}'] is missing required columns: "
                    f"{sorted(missing)}. Found: {sorted(df.columns)}"
                )
            if df.empty:
                raise ValueError(
                    f"[AlgoTradeKit] data['{tf}'] is empty."
                )

        primary = data[self.primary_timeframe]
        if len(primary) < self.warmup_period:
            raise ValueError(
                f"[AlgoTradeKit] Strategy '{self.__class__.__name__}': "
                f"primary timeframe has {len(primary)} candles but "
                f"warmup_period is {self.warmup_period}."
            )

    # ------------------------------------------------------------------
    # Repr
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} "
            f"primary_tf={self.primary_timeframe!r} "
            f"warmup={self.warmup_period}>"
        )
