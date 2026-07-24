# v1.0.0 implementation report

One entry per implemented section of `v100.md`, appended in order. Do not
overwrite earlier entries.

## Section 1 — broker: MetaTrader cross-platform + `[mt5]` packaging — 2026-07-06

- **What was implemented**
  - `_ops.py`: all MT5 operations (candles, tick, symbols, account, orders,
    positions, modify/close) moved verbatim out of `bridge_server.MT5Dispatcher`
    into a shared, stdlib-only `MT5Ops` class (the `MetaTrader5` module is
    handed in, never imported at module level).
  - `bridge_server.py`: `MT5Dispatcher` now subclasses `MT5Ops` and only owns
    the lifecycle (import / `initialize()` / optional login). Protocol and
    server code unchanged. Imports `_ops` package-relative with a plain
    same-directory fallback, so the standalone VPS deploy works by copying
    **both** `bridge_server.py` and `_ops.py` into one folder (per Q&A;
    MT5_WINE_SETUP.md Part E update lands in §23).
  - `_native.py`: new `NativeTransport` with the same `call(method, *args,
    **kwargs)` surface as `BridgeClient`, driving the same `MT5Ops` — Windows
    in-process path. `import MetaTrader5` is eager in `__init__` (missing
    package → `BrokerError` with `pip install AlgoTradeKit[mt5]` /
    `pip install MetaTrader5`); `mt5.initialize()` is lazy on first call
    (failure → `ConnectionFailed` naming the terminal requirement + the MT5
    error code, so the connect probe never swallows it). Serialised with a
    lock; same private-method guard as the bridge dispatcher.
  - `_client.py`: `MetaTraderBroker(..., mode="auto"|"native"|"bridge")`.
    Auto rules per spec: non-default `host` → bridge; else Windows → native;
    else bridge. Resolved choice exposed as `broker.mode` (`"custom"` when a
    transport is injected). `Broker("metatrader", mode=...)` passes through.
  - `_bridge_client.py`: on TCP connect failure runs D4 diagnostics and raises
    `ConnectionFailed` naming the failing step + the exact guide section:
    wine missing → Part A; wine present but `WINEPREFIX`/`~/.mt5` prefix
    missing → Part B; both fine but nothing listening → Part G; remote host →
    generic bridge-unreachable message pointing at Part G (local checks
    skipped). No fallback, no auto-start.
  - `pyproject.toml`: new extra `mt5 = ['MetaTrader5; platform_system ==
    "Windows"']` — never installs on Linux. `MetaTrader5` stays out of core
    deps (next_version #9). next_version #10 (`websockets` availability)
    verified: `websockets>=12.0` is a **core** dependency, so it is always
    present — `[dev]` left as-is (per Q&A).
- **Deviations from spec** (all confirmed in Q&A before coding)
  - Bridge VPS deploy becomes two files (`bridge_server.py` + `_ops.py`);
    Part E doc update deferred to §23.
  - `[dev]` extras not replaced with the spec's 4-item list — websockets/pandas
    already core deps; black/ruff/build/twine kept for the release workflow.
  - Native error types: missing package → `BrokerError` (spec) at transport
    construction; `initialize()` failure → `ConnectionFailed` (subclass of
    `BrokerError`) so it surfaces through the connect probe.
  - Diagnostics additions beyond the spec's two examples: prefix-missing →
    Part B message; remote-host case skips local wine/prefix checks.
- **New files**
  - `src/AlgoTradeKit/broker/metatrader/_ops.py`
  - `src/AlgoTradeKit/broker/metatrader/_native.py`
  - `tests/test_broker_mt5.py`
- **Changed files**
  - `src/AlgoTradeKit/broker/metatrader/bridge_server.py` — ops extracted to
    `MT5Ops`; dispatcher subclasses it; docstring covers the two-file copy.
  - `src/AlgoTradeKit/broker/metatrader/_bridge_client.py` — static setup hint
    replaced by `_diagnose_unreachable()` (D4 guided diagnostics).
  - `src/AlgoTradeKit/broker/metatrader/_client.py` — `mode` parameter,
    `_resolve_mode()`, transport pick (native/bridge/custom), docstrings.
  - `src/AlgoTradeKit/broker/metatrader/__init__.py` — export `NativeTransport`.
  - `src/AlgoTradeKit/broker/__init__.py` — `Broker(..., mode="auto")`
    pass-through + docstring.
  - `pyproject.toml` — `[project.optional-dependencies] mt5` extra.
- **Tests added** (`tests/test_broker_mt5.py`, 23 tests)
  - Native vs bridge parity over a real TCP bridge socket + mocked
    `MetaTrader5` module: 12 RPC methods byte-equal, error parity, private
    -method guard parity; end-to-end `MetaTraderBroker` over native.
  - Mode auto-detection matrix (mode × platform × host), forced modes,
    invalid mode, injected transport, factory pass-through.
  - Windows failure UX: missing package (install hint, also via broker),
    `initialize()` failure (`ConnectionFailed` with MT5 code, not swallowed
    by the connect probe).
  - Linux diagnostics: Part A / Part B / Part G messages, remote-host skip.
  - Packaging (`mt5` extra marker), `_ops.py` stdlib-only guard, standalone
    two-file deploy in a subprocess.
  - Results: `654 passed, 1 warning in 7.39s` (631 pre-existing + 23 new; the
    warning is a pre-existing `PytestUnhandledThreadExceptionWarning` from
    `test_visual`, present before this section). Ruff on all §1-touched files:
    `All checks passed!` (also fixed the 21 pre-existing ruff findings in
    those files; repo-wide count 299 → 278, remainder is older files untouched
    by §1 flagged by the newer ruff 0.15.20). Note: `black --check` fails on
    55 files repo-wide **including files §1 never touched** — the tree is not
    black-clean at baseline, so black was not applied (it would bury §1's diff
    in unrelated reformatting); new code follows the surrounding hand style
    and the 100-char limit.
- **Suggested commit messages**
  1. `feat(broker): MT5 cross-platform — NativeTransport, mode auto-detect, guided bridge diagnostics, [mt5] extra`
  2. `test(broker): native/bridge parity, mode matrix, failure-path guide references — 23 tests`

## Section 2 — broker: MT5 streaming (polling) — 2026-07-06

- **What was implemented**
  - `broker/_stream.py`: new `start_poll_stream(poll_once, *, interval, name,
    on_error)` — polling counterpart of `start_ws_stream`: daemon thread, first
    poll immediate, then every `interval` seconds; exceptions inside a poll are
    swallowed (optional `on_error`) so one bad poll never kills the feed.
    `Stream.loop` became optional (`None` for polling streams; `stop()` guards
    the asyncio path) — same stoppable handle for WebSocket and polling.
  - `MetaTraderBroker.stream_candles(symbol, tf, on_candle, closed_only=True)`:
    polls the `candles_from_count` tail (constant `_CANDLE_TAIL_COUNT = 10`)
    every `candle_poll_interval` (default 1.0 s), identical over bridge and
    native transports (both are lock-serialised, so the polling thread safely
    shares the transport with user calls). Emits library-standard candle dicts
    plus a `"closed"` flag — same shape as the Binance WebSocket stream. A
    candle counts as closed once a newer bar appears in the polled tail
    (bar-advance detection); closed candles fire exactly once, in order, dedup
    by `timestamp`; bars already closed at subscribe time never fire. With
    `closed_only=False` the forming candle is also emitted on every change and
    immediately on the first poll. Multi-bar catch-up: all new closed bars in
    the tail are emitted in order after a stall.
  - `MetaTraderBroker.stream_ticker(symbol, on_tick)`: polls the `tick` op
    every `tick_poll_interval` (default 0.2 s); emits a `Ticker` only when the
    venue tick changed since the previous poll (first poll always fires). The
    payload→`Ticker` mapping was factored into `_ticker_from_payload()`, shared
    with `get_ticker()` (incl. the mid-price fallback when `last` is 0).
  - Poll intervals are constructor kwargs on `MetaTraderBroker`
    (`candle_poll_interval=1.0`, `tick_poll_interval=0.2`, validated > 0) and
    pass through the `Broker("metatrader", ...)` factory.
  - Pollers are built as one-step closures (`_make_candle_poller` /
    `_make_ticker_poller`) so tests drive them deterministically without
    threads or sleeps.
- **Deviations from spec** (all confirmed in Q&A before coding)
  - Closed-candle detection = bar-advance (newer bar in the tail), not clock
    arithmetic — §2 has no venue clock (`clock_offset_ms` lands in §3); §14
    later handles exact-time closes for the trader.
  - Tail size fixed at 10 (module constant, not a kwarg); real gap-fill after
    downtime stays §14's job.
  - Poll intervals also exposed on the `Broker()` factory (spec only says
    "constructor kwargs"; mirrors §1's `mode` pass-through).
  - `closed_only=False` emits the current forming candle immediately on the
    first poll (closest to Binance subscribe behaviour).
  - Not in spec but required: `Stream.loop` made optional (polling streams have
    no asyncio loop); `get_ticker` mapping factored out for reuse. Ruff-fixed
    the style findings (`Optional` → `| None`, `typing.Callable` →
    `collections.abc`) in the touched files, per §1's precedent.
- **New files** — none (no new source or test files).
- **Changed files**
  - `src/AlgoTradeKit/broker/_stream.py` — `Stream.loop` optional; new
    `start_poll_stream()`; module docstring covers polling.
  - `src/AlgoTradeKit/broker/metatrader/_client.py` — poll-interval kwargs +
    validation; `stream_candles` / `stream_ticker`; `_make_candle_poller` /
    `_make_ticker_poller`; `_ticker_from_payload` shared with `get_ticker`;
    `_CANDLE_TAIL_COUNT`.
  - `src/AlgoTradeKit/broker/__init__.py` — factory forwards
    `candle_poll_interval` / `tick_poll_interval`; docstring note.
  - `tests/test_broker_mt5.py` — §2 test section appended (scripted-transport
    harness + threaded/e2e tests).
- **Tests added** (`tests/test_broker_mt5.py`, 12 tests)
  - Closed-only exactness: subscribe baseline (history never fires), exactly-
    once close on bar advance, dedup across identical tails, multi-bar
    catch-up in order, empty-tail tolerance.
  - Forming updates (`closed_only=False`): immediate first emission,
    emit-on-change only, final close followed by next forming bar.
  - Ticker: emit-on-change only, `Ticker` mapping + mid-price fallback parity
    with `get_ticker`.
  - Stop semantics: `Stream` handle alive → `stop()` joins the thread and
    polling provably ceases (both streams); `start_poll_stream` swallows a
    poll error and keeps polling (`on_error` reported).
  - Config: interval defaults, > 0 validation, factory pass-through, timeframe
    normalisation (`"1H"` → `"1h"`) + invalid timeframe raises, tail polled
    via `candles_from_count` with count 10.
  - End-to-end over a real bridge TCP socket (fake MT5 behind the §1 test
    server): a bar appended mid-stream arrives as exactly one closed candle.
  - Results: full suite `666 passed, 1 warning in 7.35s` (654 baseline + 12
    new; the warning is the pre-existing `test_visual` thread warning). Ruff on
    all §2-touched files: `All checks passed!`
- **Suggested commit messages**
  1. `feat(broker): MT5 candle/ticker streaming via transport polling — same Stream handle and candle shape as Binance, poll-interval kwargs`
  2. `test(broker): MT5 streaming — polling dedup, closed-only exactness, stop semantics, bridge e2e — 12 tests`

## Section 3 — broker: trading costs + venue clock — 2026-07-07

- **What was implemented**
  - `TradingCosts` frozen dataclass in `broker/_types.py`, field order per spec
    (`commission_type, commission, spread, contract_size|None, raw`), plus
    `COMMISSION_TYPE_PERCENTAGE / PER_LOT / FIXED` constants — deliberately
    re-declared with the exact `SimulateConfig.commission_type` string values
    (`broker` may not import `simulate`; a test locks the parity), re-exported
    from `broker/__init__.py`.
  - `BaseBroker.get_trading_costs(symbol)` — concrete default for venues that
    cannot answer: zero costs + `raw={"source": "default", "note": ...}`.
  - `BinanceBroker.get_trading_costs()` — commission = account **taker** rate
    (entries are market orders, §15): futures `GET /fapi/v1/commissionRate`
    (new `Endpoints.commission_rate`), spot `commissionRates.taker` from
    `/api/v3/account` (legacy basis-points `takerCommission/10000` fallback).
    Unauthenticated **or** commission call rejected → standard taker fee
    (spot 0.001, futures 0.0005 — module table `_STANDARD_TAKER_FEE`), reason
    recorded in `raw`; `ConnectionFailed` still propagates. Spread = one
    book-ticker snapshot `ask − bid` (price units). `contract_size=None`.
  - `MetaTraderBroker.get_trading_costs()` — one `symbol_info` transport call:
    spread = `spread × point` (falls back to the payload's `ask − bid` when the
    venue reports no spread/point), `contract_size = trade_contract_size`
    (0 → `None`), commission reported 0 with `commission_type="per_lot"` (MT5
    exposes none; the §11 `TraderConfig` override supplies a real value),
    `raw` = the full `symbol_info` payload. Unknown symbol propagates.
  - `BaseBroker.clock_offset_ms(*, force_refresh=False)` — venue minus local
    clock: each sample brackets one `server_time()` round trip and compares
    against the local **midpoint** (cancels request/response asymmetry);
    result = median of `_CLOCK_OFFSET_SAMPLES = 5` samples, cached in
    `_clock_offset_cache` and re-measured automatically after
    `_CLOCK_OFFSET_TTL_MS = 5 min` (or on `force_refresh=True`). MT5 inherits
    it; its `server_time()` proxies the local clock → offset ≈ 0 by design.
- **Deviations from spec** (all confirmed in Q&A before coding)
  - Clock samples are midpoint-corrected rather than the literal
    `server_time() - now_ms()` — same measurement, ~½·RTT less bias (D2:
    "never be late").
  - "A few samples / refreshed periodically" made concrete: 5 samples, 5-minute
    TTL, `force_refresh` kwarg.
  - MT5 commission override **not** added as broker kwargs — it is §11's
    `TraderConfig` job; §3 reports 0.0 / `per_lot`.
  - Binance fallback also covers an authenticated-but-rejected commission call
    (spec names only the unauthenticated case); connection failures and MT5
    unknown-symbol errors still raise — "cannot answer" ≠ "unreachable".
  - MT5 spread falls back to the payload's `ask − bid` when `spread`/`point`
    are missing/zero (robustness; spec names `symbol_info` as the source).
  - Housekeeping per §1/§2 precedent: pre-existing ruff findings in the touched
    files auto-fixed (`Optional` → `| None`, `Callable` import, import sort);
    `_rest.py`/`_ws.py` were not touched, so their findings remain.
- **New files** — none.
- **Changed files**
  - `src/AlgoTradeKit/broker/_types.py` — `COMMISSION_TYPE_*` constants +
    `TradingCosts` dataclass.
  - `src/AlgoTradeKit/broker/base.py` — `clock_offset_ms()` +
    `get_trading_costs()` default, `_clock_offset_cache`, sample/TTL constants,
    docstring method-group line.
  - `src/AlgoTradeKit/broker/exchange/binance/_endpoints.py` —
    `commission_rate` property (futures).
  - `src/AlgoTradeKit/broker/exchange/binance/_client.py` —
    `get_trading_costs()`, `_taker_commission()`, `_STANDARD_TAKER_FEE`.
  - `src/AlgoTradeKit/broker/metatrader/_client.py` — `get_trading_costs()`
    from `symbol_info`.
  - `src/AlgoTradeKit/broker/__init__.py` — export `TradingCosts` +
    `COMMISSION_TYPE_*`.
  - `tests/test_broker.py` — §3 Binance/base/clock tests + imports.
  - `tests/test_broker_mt5.py` — §3 MT5 costs/clock tests + imports.
- **Tests added** (15: 11 in `tests/test_broker.py`, 4 in `tests/test_broker_mt5.py`)
  - Binance: spot/futures unauthenticated standard-fee fallback (+ book-ticker
    URL/params + spread maths asserted); futures authed commissionRate endpoint
    (URL, params, taker rate); spot authed `commissionRates`; spot legacy
    basis-points; authed-rejected → fallback with error in `raw`;
    `ConnectionFailed` propagates on both the book and commission legs.
  - Base: default `get_trading_costs` (zeros + note); commission-constant
    parity with `simulate`.
  - Clock: midpoint + median arithmetic under a scripted clock (outlier sample
    ignored); cache hit / TTL expiry / `force_refresh` via `server_time()`
    call-counting.
  - MT5: costs from `symbol_info` (spread×point, contract size, raw payload,
    exactly one transport call); ask−bid spread fallback + zero contract size →
    `None`; unknown symbol propagates; venue clock offset ≈ 0.
  - Results: full suite `681 passed, 3 warnings in 7.87s` (666 baseline + 15
    new; warnings are the pre-existing environmental set — system `websockets`
    deprecations + the `test_visual` thread warning, count varies 2–3 per run;
    the "Event loop is closed" stderr noise reproduces on `test_visual` alone,
    pre-dates this section). Ruff on all §3-touched files: `All checks passed!`
- **Suggested commit messages**
  1. `feat(broker): trading costs + venue clock — TradingCosts, get_trading_costs() per venue, clock_offset_ms() with median sampling + TTL cache`
  2. `test(broker): costs fallbacks per venue + clock-offset sampling — 15 tests`

## Section 4 — indicator: incremental updates — 2026-07-07

- **What was implemented**
  - Every indicator class (all 13: `SMA EMA WMA VWMA SMMA DEMA TEMA HullMA
    VWAP RSI MACD ATR Ichimoku`) gained a public `update(...)` method: push
    exactly one new bar, get the new value back, with the stored source and
    every `result` series extended in place — so properties, crossover
    helpers and `cloud_df()` stay correct after streaming.
  - Streaming state machines in `_base.py`: `_EwmState` (bit-exact replica of
    pandas `ewm(adjust=False, ignore_na=False).mean()` incl. NaN seeding,
    weight decay across gaps, the constant-input guard and min_periods
    masking — verified bitwise against pandas), `_WindowState` (bounded deque
    for rolling mean/sum/max/min/WMA with min_periods == window semantics),
    `_ma_state()` (streaming counterpart of the RSI/MACD MA dispatch) and
    `_ieee_div()` (0/0 → NaN, x/0 → ±inf like pandas; Python `/` would
    raise). `_BaseIndicator` gained the lazy `_stream` slot and the
    `_append_value` / `_append_results` helpers (RangeIndex-clean appends).
  - State is seeded lazily on the first `update()` call by replaying the
    stored history through the state machine once (exact by construction for
    any NaN pattern); each further call is O(1) / bounded by the indicator's
    own window — no full recompute anywhere.
  - Composite chains feed masked intermediate outputs into the next state
    (DEMA/TEMA EMA-of-EMA, HullMA raw→WMA, MACD signal-of-macd, RSI
    rsi_ma-of-rsi), reproducing the batch NaN warmup positions exactly.
  - Return types: single-line indicators return `float` (MA family → their
    line, VWAP → vwap with bands appended too, RSI → rsi with rsi_ma
    appended, ATR → atr with tr appended); `MACD` returns
    `{"macd","signal","histogram"}`, `Ichimoku` returns the new bar's
    `{tenkan, kijun, senkou_a, senkou_b, span_a_raw, span_b_raw, chikou}`.
  - Ichimoku streaming semantics: donchian rolling max/min windows per spec;
    the new bar's `senkou_a/b` read the raw span from `displacement` bars
    back; the new close retro-fills `chikou[n − displacement]` (exactly like
    the batch `shift(-displacement)`); `cloud_future_a/b` are rebuilt each
    update, re-anchored past the new last bar (index matches batch).
  - VWAP streams both anchors: `"none"` replicates pandas cumsum NaN
    semantics (NaN bar stays NaN, running total continues); `"session"`
    mirrors the batch loop body exactly (including its never-reset
    approximation — bug-compatible by design).
  - ATR streaming replicates the real batch behaviour: `TR[0] = high − low`
    (batch `DataFrame.max(axis=1)` skips NaN), first valid ATR at index
    `period − 1`.
- **Deviations from spec** (all confirmed in Q&A before coding)
  - Return-type convention (float vs dict split) chosen — spec only showed
    `ema.update(new_close) -> float`.
  - "Appends internally" interpreted as: source + all result series extended;
    parity defined as full-series equality vs a batch computation over the
    extended data.
  - Parity strictness: NaN masks exact + values at `rtol=1e-9` — EWM-family
    lines are in practice bitwise; rolling mean/sum lines can differ from
    pandas' Kahan-compensated sliding sums by ~1 ulp (replicating pandas'
    private internals was rejected as fragile).
  - Coverage widened to all 13 exported classes per §26 "every indicator"
    (spec text names only RSI/ATR/SMMA, MA family, MACD, Ichimoku).
  - Not in spec but required/precedent: pre-existing ruff findings fixed in
    all touched files (quoted annotations, import sort, unused
    imports/variables, ambiguous `l` names, long lines — indicator module +
    `tests/test_indicator.py` now ruff-clean); two stale ATR docstring lines
    corrected (`tr[0]`/ATR NaN counts) because the new streaming comment in
    the same file would have contradicted them — observed batch behaviour is
    unchanged.
- **New files** — none.
- **Changed files**
  - `src/AlgoTradeKit/indicator/_base.py` — `_EwmState`, `_WindowState`,
    `_ma_state`, `_ieee_div`; `_BaseIndicator._stream` + append helpers;
    import cleanup.
  - `src/AlgoTradeKit/indicator/ma.py` — `update()` on
    SMA/EMA/WMA/VWMA/SMMA/DEMA/TEMA/HullMA/VWAP (+ `_init_stream`,
    `VWAP._push_vwap`); dead VWAP locals removed; ruff fixes.
  - `src/AlgoTradeKit/indicator/rsi.py` — `update()` + `_push_rsi`
    (incl. streamed `rsi_ma` for all 4 ma types); ruff fixes.
  - `src/AlgoTradeKit/indicator/macd.py` — `update()` + `_push_macd`
    (dict return, all 4 ma types via `_ma_state`); ruff fixes.
  - `src/AlgoTradeKit/indicator/atr.py` — `update()` + `_push_atr`;
    docstring NaN-count corrections; ruff fixes.
  - `src/AlgoTradeKit/indicator/ichimoku.py` — `update()` + `_push_ichimoku`
    (chikou retro-write, future-cloud re-anchor); dead `cloud_df` locals
    removed; ruff fixes.
  - `src/AlgoTradeKit/indicator/__init__.py` — ruff import sort only.
  - `tests/test_indicator.py` — `TestIncrementalUpdates` appended;
    pre-existing ruff findings fixed.
- **Tests added** (`tests/test_indicator.py::TestIncrementalUpdates`, 29 tests)
  - MA-family parity (9 parametrized: SMA×2 lengths, EMA×2, WMA, SMMA, DEMA,
    TEMA, HullMA); VWMA parity; VWAP parity for both anchors with bands
    [1,2,3] × multiplier 1.5.
  - RSI parity + `show_ma` parity for all 4 ma types; MACD parity for 3
    ma-type combos + dict-return keys + return-equals-appended check.
  - ATR parity; Ichimoku parity over all 9 result keys + future-cloud index
    equality + per-update return-dict shape (newest chikou always NaN);
    explicit chikou retro-write test.
  - Cross-cutting: return value equals appended last value; update straight
    after minimal-length construction (EMA/SMA/SMMA/ATR); constant-series
    bitwise equality (pandas constant-guard); RangeIndex/length growth;
    full-series helpers (`crossover`/`crossunder`) intact after streaming.
  - Results: full suite `710 passed, 3 warnings in 9.24s` (681 baseline + 29
    new; warnings are the pre-existing environmental set from §3). Ruff on
    all §4-touched files: `All checks passed!`
- **Suggested commit messages**
  1. `feat(indicator): incremental update() on all 13 indicators — O(1) streaming state machines with batch-parity maths`
  2. `test(indicator): streaming-vs-batch parity for every indicator — 29 tests`

## Section 5 — strategy: incremental computation API — 2026-07-08

- **What was implemented**
  - `BaseStrategy.update_indicators(data, new_index)` — the optional §5.1 hook
    (no-op default, spec docstring + live lifecycle + fallback/forming notes).
    Never called by `run()`; detection of an override via
    `has_update_hook(strategy)`.
  - New `strategy/_incremental.py` — the "library machinery" as stateless
    module functions (API shape confirmed in Q&A; caller owns the data dict,
    which fits §10's window trimming):
    - `advance_live_candle(strategy, data, candle, *, recompute_window=None)`
      → `(signals, exits)` — validates the candle (all 6 standard keys
      required, timestamp strictly greater than the last row, extra broker
      keys ignored), appends the row to `data[primary_timeframe]` (re-bound,
      clean RangeIndex, `timestamp` stays int64, indicator cells start NaN),
      then hook path (`update_indicators(new_index)`) or fallback path (tail
      recompute), then `generate_signals(i)` + `detect_exit_signals(i)`.
      Below `warmup_period` the signal calls are skipped (mirrors `run()`)
      but the hook/recompute still executes so state advances.
    - Fallback tail recompute (§5.2): `prepare_indicators` re-run over a
      fresh-input copy of the last K rows (`_`-columns stripped, RangeIndex
      reset), K = `recompute_window` or `default_recompute_window(strategy)`
      = `max(warmup_period, 200)`; the resulting `_`-prefixed columns are
      spliced back into the master tail rows (fill-only — see deviations).
    - `evaluate_forming_candle(...)` (§5.3) — throwaway copy of the primary
      frame with the forming row appended; forming `_`-values tail-computed
      via `prepare_indicators` on the copy for **all** strategies (the hook
      is never called on forming candles); master frames and committed
      `self.*` state untouched; repeatable per forming update; returned
      signals carry `candle_index == len(master)`.
    - `default_recompute_window()` / `has_update_hook()` helpers; all four
      functions re-exported from `strategy/__init__.py` (`__all__`).
  - Documented limits per spec (module + hook docstrings): fallback exact for
    windowed lookback ≤ K, approximate for EMA/RMA recursions unless K is
    generous, not safe for python-object state built in `prepare_indicators`
    (implement the hook; SMC state belongs in `setup()` + hook so
    `prepare_indicators` stays side-effect-free for forming safety).
- **Deviations from spec** (1–4 confirmed in Q&A before coding)
  - API surface: spec names only the hook; the machinery landed as the four
    module functions above in `strategy/_incremental.py`.
  - Multi-timeframe live: only the primary timeframe advances; other TFs keep
    seed content. The fallback passes throwaway copies of the other TFs into
    `prepare_indicators` (no KeyError for multi-TF strategies) and splices
    only the primary tail — documented as not O(1); hook = true O(1).
  - Forming eval tail-computes via `prepare_indicators` even for hook
    strategies (confirmed reading of §5.3), with the documented
    "prepare must not mutate self" caveat.
  - Append validation is strict: duplicate or out-of-order timestamp raises
    (feeds dedup per §2; §14 owns gap-fill); extra candle keys ignored.
  - **Fill-only splice** (implementation decision, not pre-confirmed): the
    literal "splice the tail rows" corrupts the master — (a) the recompute's
    warmup NaNs at the tail head would permanently overwrite previously
    correct values as the window slides (a growing NaN band K rows behind the
    tip), and (b) recursive columns (EMA — non-NaN from row 0) would be
    rewritten with ever-shorter in-tail lookback, decaying to ~raw values K
    rows behind the tip. Fix: a cell is written the first time the recompute
    produces a value for it (appended row, warmup fills, retroactive
    chikou-style `shift(-x)` backfills) and never revised. Consequence
    (documented): repainting indicators need the hook. Tests lock all three
    behaviours (no NaN band, immutability, retro backfill).
  - Housekeeping per §1–§4 precedent: ruff autofixes in touched files only
    (`_base.py` unused `typing.Any` + quoted annotation; test-file import
    sort); `_types.py`/`builtin/macd.py` untouched so their findings remain.
    black unavailable in this environment and the tree is not black-clean at
    baseline (§1) — new code follows the 100-char limit and surrounding style.
- **New files**
  - `src/AlgoTradeKit/strategy/_incremental.py`
- **Changed files**
  - `src/AlgoTradeKit/strategy/_base.py` — `update_indicators` hook + module
    docstring note; ruff fixes.
  - `src/AlgoTradeKit/strategy/__init__.py` — re-export the four incremental
    functions; docstring section.
  - `tests/test_strategy.py` — §5 fixtures (EMA hook/tail, SMA, lead-column,
    SMC swing-zone, multi-TF strategies) + 6 test classes; import sort fix.
- **Tests added** (`tests/test_strategy.py`, 40 tests)
  - Hook contract: no-op default, `has_update_hook` both ways, default-window
    maths (floor 200 / warmup dominates), public exports.
  - Hook path: called exactly once per closed candle in order; sees the
    appended NaN row; `prepare_indicators` never re-runs (seed only); column
    + signal parity vs batch `run()` (streamed EMA via §4 `update()`);
    signal fields carry the new index/timestamp; timestamp dtype stays
    int64; SMC swing-zone state through the hook equals a pure-python
    replica (plus hook-maintained column parity).
  - Fallback: windowed SMA bit-parity with batch (column + signals);
    recomputes exactly min(K, len) rows once per candle; EMA approximation
    bounds (K=120 close, K=25 measurably approximate but bounded); no NaN
    band after 100 steps with K=30; fill-only immutability + NaN refill;
    splice locality (outside-tail cells untouched); retroactive `shift(-2)`
    column backfilled.
  - Validation/warmup: missing key, duplicate ts, out-of-order ts, extra keys
    ignored, empty frame, missing primary TF, `recompute_window=0`; below
    warmup → no signal calls while the hook still runs.
  - Forming: master frames + zones + hook-call log untouched (recompute used
    instead); forming signal values/index correct (SMA of history + forming
    close); forming vs closed same-candle signal equality; repeatable incl.
    updated same-timestamp candle; signal against committed SMC zone; below
    warmup empty; duplicate timestamp raises.
  - Multi-TF: advance appends primary only (4h object identity + content
    unchanged), forming leaves both frames untouched.
  - Results: full suite `750 passed, 3 warnings in 14.90s` (710 baseline +
    40 new; warnings are the pre-existing environmental set from §3). Ruff on
    all §5-touched files: `All checks passed!` Plus an out-of-pytest smoke:
    unmodified `MACDCrossoverStrategy` live-stepped 100 candles via the
    fallback — stepped signals identical to the batch tail, forming eval
    non-mutating.
- **Suggested commit messages**
  1. `feat(strategy): incremental computation API — update_indicators hook, advance_live_candle with fill-only tail-recompute fallback, forming-candle evaluation`
  2. `test(strategy): hook/fallback parity, tail-recompute bounds, forming-candle isolation, SMC state through the hook — 40 tests`

## Section 6 — simulate: shared position math — 2026-07-08

- **What was implemented**
  - New `simulate/_position_math.py`: every position-maths helper factored
    **verbatim** out of `_engine.py` into one shared module — sizing
    (`compute_position_params`, `can_open_position`), TP price laddering
    (`build_tp_level_prices`), SL transition logic (`advance_multi_rr`,
    `update_trailing_sl`, `check_risk_free`), per-candle close detection
    (`check_close`, `sl_reason`), PnL accounting incl. partial closes
    (`make_closed_trade`, `apply_partial_close`, `handle_tp_level_hit`) and
    the `SIZE_EPSILON` dust tolerance. Module docstring states the design
    contract (stateless; state lives on the `_InternalPosition` passed in;
    never touches DataFrames/StrategyResult) and names the consumers.
  - `_engine.py` reduced to orchestration only: `Simulate`
    (`run` / rendering / `_simulate_loop`) + the keep-alive handler; all
    maths now imported from `_position_math`. Docstring gained a pointer to
    the shared module.
  - `_runner.py::run_multi` imports the same functions from
    `_position_math` directly (previously reached them through `_engine`),
    so engine, multi-pair runner and (later, §15) the live trader consume
    the identical single source of truth.
- **Deviations from spec** (1–3 confirmed in Q&A before coding)
  - Filename `_position_math.py` confirmed (spec said "name TBD").
  - Full 11-helper set moved (spec literally names sizing/SL-TP/trailing/
    risk-free/multi-RR; close detection + PnL builders went along so the
    engine keeps zero maths).
  - Helpers renamed **public** (leading underscore dropped) — the module
    itself stays private (`_position_math`), same pattern as `_lot.py`
    (public helpers inside a private module; nothing re-exported from
    `simulate/__init__.py`).
  - Dropped `_runner.py`'s unused `_update_trailing_sl` re-export
    (`# noqa: F401`) — the canonical import point is now `_position_math`;
    also removed two dead `all_open_*` list-comprehension assignments in
    `run_multi` (one was ruff F841; both never read — behaviour unchanged,
    locked by the hash check below).
  - Housekeeping per §1–§5 precedent: pre-existing ruff findings in the two
    touched files fixed (import sorts, quoted-annotation UP037s, dead
    TYPE_CHECKING `SimulateReport` import, F841). CLAUDE.md /
    PROJECT_STRUCTURE.md still name the old `_make_closed_trade` /
    `_apply_partial_close` internals — doc refresh is §23's job.
- **New files**
  - `src/AlgoTradeKit/simulate/_position_math.py`
- **Changed files**
  - `src/AlgoTradeKit/simulate/_engine.py` — 11 helpers + `_SIZE_EPSILON`
    deleted; imports + call sites switched to `_position_math`; docstring
    pointer; ruff fixes.
  - `src/AlgoTradeKit/simulate/_runner.py` — imports switched to
    `_position_math`; call sites renamed; dead assignments removed; ruff
    fixes.
- **Tests added** — none (per Q&A: §26's §6 share is "shared math refactor
  leaves all existing simulate tests green"; the step-driven byte-identity
  test belongs to §7). Verification:
  - Full suite: `750 passed, 3 warnings in 15.32s` (identical count to the
    §5 baseline; warnings are the pre-existing environmental set).
  - Ruff on all §6-touched files: `All checks passed!`
  - Out-of-pytest regression harness (scratchpad, not committed): 14
    scenarios — signal / fixed_rr / multi_rr (with and without
    `tp_level_close_fractions`) / none TP, trailing SL, risk-free,
    force-close, end-of-data, MT5 risk-percent + fixed-lot, exchange
    fixed-lot / fixed-amount / no-compound, position limits, `run_batch`
    ×3, `run_multi` shared wallet — sha256 over
    `repr(closed_trades) + repr(balance_history)` **byte-identical before
    vs after** the refactor; scenarios exercised every close reason
    (`sl`/`tp`/`tp_rr`/`rf`/`force_close`/`end_of_data`) and dense SL
    histories (308 trailing moves in one scenario).
- **Suggested commit message** (single commit — one logical unit, no new
  test files)
  1. `refactor(simulate): factor position sizing/SL-TP/trailing/risk-free/multi-RR maths into shared _position_math.py — single source of truth for engine, runner and the v1.0.0 live trader; batch results byte-identical`

## Section 7 — simulate: step-driven engine — 2026-07-08

- **What was implemented**
  - `SimulationStepper` in `simulate/_engine.py` — the step-driven core
    (API shape confirmed in Q&A). The old `_simulate_loop` body moved into
    it verbatim:
    - `step(candle, signals=(), exit_signals=()) -> list[ClosedTrade]` —
      per-candle phases A–F unchanged; `candle` is any mapping with the
      library-standard `timestamp/open/high/low/close` keys (extra keys such
      as `volume`/`closed` ignored — broker-stream dicts work as-is); returns
      the trades closed during this step, partial multi-RR slices included.
    - `finalize() -> list[ClosedTrade]` — phase G: closes leftovers at the
      last stepped candle's close (`end_of_data`); idempotent; a stepper that
      never stepped finalizes to `[]`; `step()` after finalize raises
      `RuntimeError`.
    - `build_report() -> SimulateReport` — phase H as an any-time snapshot:
      mid-run the live positions surface in `report.open_at_end`; state lists
      are shallow-copied so a taken snapshot never grows; after `finalize()`
      it is the batch-identical final report.
    - Ctor `(config, initial_wallet=None, initial_trade_id=0, *,
      record_balance_history=True)`; public running state (`wallet`,
      `trade_id_seq`, `open_positions`, `closed_trades`, `balance_history`)
      plus a `finalized` property. Purity kept: the stepper never mutates the
      candle mapping, signals, or any DataFrame.
  - `Simulate._simulate_loop` (name and signature unchanged) is now a thin
    batch driver of the stepper — batch results byte-identical by
    construction and regression-proved (below).
  - `run_multi` rewritten onto one stepper per pair: the shared wallet and
    the single trade-id sequence are rebound around every step (pairs step
    strictly sequentially, so this is exact); steppers run with
    `record_balance_history=False` while the runner keeps its own
    combined-portfolio equity snapshot per union timestamp; per-pair
    end-of-data via `finalize()`; the combined trade list is built from the
    `step()`/`finalize()` return values, preserving the original
    chronological event order.
  - `SimulationStepper` exported from `simulate/__init__.py` (`__all__` +
    docstring class list) — public now, per Q&A choice.
- **Deviations from spec** (all confirmed in Q&A before coding)
  - Spec allows "step function / generator" — class-based stepper chosen.
  - Public export now rather than package-internal until §10.
  - Scope beyond the spec sentence (user-directed): `run_multi` refactored
    onto the stepper too, with two approved semantic unifications of its
    output: (1) portfolio trades now record the v0.7.4 **entry-state
    `sl_history` record** like single-pair trades (dynamic SL/TP chart lines
    render correctly for portfolio trades); (2) force-close exit price uses
    the engine's `is not None` check instead of the runner's truthiness test
    (observable only for a literal 0.0 exit price).
  - `build_report()` passes the live open positions as `open_at_end`
    (batch always passed `[]`; after `finalize()` the list is empty, so the
    batch report is unchanged — mid-run snapshots gain the information).
  - Housekeeping per §1–§6 precedent: dead `pair_ts_to_idx` block removed
    from `run_multi` (write-only since the `pair_candle_idx` map superseded
    it); pre-existing ruff findings fixed in the touched files
    (`simulate/__init__.py` import sort; test-file import sort, unused
    imports, long lines, semicolon statements, unused locals).
- **New files** — none.
- **Changed files**
  - `src/AlgoTradeKit/simulate/_engine.py` — `SimulationStepper` added (loop
    body moved verbatim into `step`/`finalize`/`build_report`);
    `_simulate_loop` reduced to a thin batch driver; module docstring names
    the stepper.
  - `src/AlgoTradeKit/simulate/_runner.py` — `run_multi` drives per-pair
    steppers (shared wallet/trade-id rebinding; combined equity snapshot and
    report stay runner-side); dead `pair_ts_to_idx` removed; imports trimmed
    to `SimulationStepper`/`ClosedTrade`/`build_report`.
  - `src/AlgoTradeKit/simulate/_position_math.py` — consumer-list docstring
    now names `SimulationStepper` instead of `_simulate_loop`.
  - `src/AlgoTradeKit/simulate/__init__.py` — export `SimulationStepper`;
    docstring class list; import-sort fix.
  - `tests/test_simulate.py` — §7 test section appended; pre-existing ruff
    findings fixed.
- **Tests added** (`tests/test_simulate.py`, 29 tests)
  - `TestStepBatchParity` (16): 15 parametrized scenarios — signal /
    fixed_rr / multi_rr / multi_rr fractions / remainder fractions /
    trailing / risk-free / force-close (market + explicit exit prices) /
    end-of-data / MetaTrader per-lot / exchange fixed-lot / fixed-amount /
    compound+leverage+spread+commission / position limits / risk_multiplier —
    each asserts full-precision `repr(asdict(trade))` equality of
    closed_trades plus balance_history plus final_balance between a
    hand-driven stepper and `Simulate.run`; plus an input-purity test
    (DataFrame and signals untouched by a step drive).
  - `TestSimulationStepperUnit` (10): step returns closed trades / partial
    slices; finalize EOD price/time semantics; finalize idempotence + empty
    stepper; step-after-finalize raises; frozen mid-run snapshot with
    `open_at_end`; `initial_wallet`/`initial_trade_id`;
    `record_balance_history=False`; candle mapping untouched + extra keys
    ignored; missing candle key raises `KeyError`.
  - `TestRunMultiOnStepper` (3): single-pair `run_multi` now equals
    `Simulate.run` trade-for-trade and history-for-history (invariant made
    possible by the unification); portfolio trades carry the entry-state
    `sl_history` record; two-pair offset-timeline run — shared contiguous
    trade-id sequence and union-timeline balance history.
  - Results: full suite `779 passed, 3 warnings in 16.62s` (750 baseline +
    29 new; warnings are the pre-existing environmental set from §3). Ruff on
    all §7-touched files: `All checks passed!`
  - Out-of-pytest regression harness (scratchpad, not committed): 21
    scenarios (14 single-pair configs covering every close reason and sizing
    mode, `run_batch` ×3, `run_multi` ×2) hashed as sha256 over
    full-precision trade dicts + balance history — all 19 single-pair/batch
    scenarios **byte-identical before vs after**; the 2 `run_multi` scenarios
    hash-match once `sl_history` is stripped, proving the approved
    entry-state record is the entire delta (trade counts, balances and
    histories identical).
- **Suggested commit messages**
  1. `feat(simulate): step-driven engine — SimulationStepper with step/finalize/build_report; _simulate_loop and run_multi now drive it; batch results byte-identical`
  2. `test(simulate): step-vs-batch byte-identity across 15 scenarios, stepper unit behaviour, run_multi-on-stepper invariants — 29 tests`

## Section 8 — visual: chart host + live push — 2026-07-09

- **What was implemented**
  - **Configurable `host`** (next_version #1): `ChartServer(title, port, host="127.0.0.1")`
    and `Chart(..., host=...)` — uvicorn binds the given host; the port
    auto-pick probe binds on the same host (IPv6-aware: `"::"` probes an
    `AF_INET6` socket) and its failure message now names the probed host.
    Loud security warnings in both docstrings. `ChartServer.display_host` /
    `.url` substitute `127.0.0.1` when bound to `0.0.0.0`/`::` (bind-to-all
    is not a destination); `Chart.url`, the auto-opened browser tab and the
    startup log all use it (log also prints the bind host).
  - **Live trade push** (v091 item 3): new `LivePosition` drawing
    (`models.py`, exported from `AlgoTradeKit.visual`) — an OPEN trade drawn
    as horizontal lines from `open_time` auto-extending to the newest candle:
    dashed entry line + direction triangle, solid current-SL line coloured by
    the v0.7.4 zone rule (red = loss / amber = break-even / cyan = profit,
    computed backend-side per repo convention), optional dashed green next-TP
    line. API: `chart.add_live_position(...) -> id`,
    `chart.update_live_position(id, stop_loss=, next_tp=, label=)` (one
    `update_drawing` WS message per trailing move / risk-free jump / next-TP
    change; `next_tp=None` clears the line, omitted = unchanged), removal via
    the existing `remove_drawing`. Plus a generic
    `chart.update_drawing(id, **fields)` that re-broadcasts the **full**
    serialised drawing (derived fields — posbox zones, SL colour — stay
    consistent); the frontend's previously-unused `update_drawing` handler now
    has a server-side sender. New position boxes and markers already streamed
    via `add_drawing`.
  - **Always-fresh init replay**: new central `Chart._add_drawing()` (used by
    every `add_*` method, the v0.7.4 dynamic SL/TP line helpers and
    `add_strategy_drawings`) broadcasts and refreshes the server's cached
    `init`; `update_drawing`/`update_live_position`/`remove_drawing`/
    `clear_drawings`/browser-edit messages refresh it too — a page refresh
    mid-live-session now reproduces the exact current chart instead of the
    state at `show()` time.
  - **Rolling candle window** (D6 client side): `Chart(candle_count_limit=N)`
    / `chart.set_candle_limit(N | None)`. Python mirror: `_bars` trimmed **in
    place** (the cached init shares the list) on set, on `set_data` and on
    every `stream()`; drawings whose whole time range left the window are
    dropped (`_drawing_end_time` rules: `close_time` → `max(time1, time2)` →
    `time`; timeless hlines and open live positions never trimmed). Frontend:
    `candleCountLimit` arrives via the init payload and a new
    `set_candle_limit` message; `enforceCandleLimit()` runs immediately and
    after every streamed bar — slices `allBars`, re-`setData`s candles +
    volume, drops indicator-series points and ichimoku-cloud points older
    than the window start, deletes expired drawings (same rules as Python via
    `drawingEndTime()`), restores the visible time range so the view doesn't
    jump.
  - Times unchanged: chart = Unix seconds, Python = UTC ms.
- **Deviations from the v100.md spec** (1–4 confirmed in Q&A before coding)
  - Open-position visuals shaped as the dedicated `live_position` drawing
    type (chosen over generic-primitives-only and chart-owns-close options);
    on close the caller (§10) removes it and draws the standard final posbox
    + segment lines.
  - Window trim extended beyond the spec's "candles/drawings" to indicator
    series and cloud points (otherwise the time scale keeps the old span and
    indicator memory grows unbounded).
  - `0.0.0.0`/`::` → `127.0.0.1` substitution in `url`/browser-open (§21
    prints externally reachable URLs itself).
  - Replay-cache freshness fix (not in the spec text; required for a usable
    live view across page refreshes).
  - `add_live_position` returns the drawing **id** (str), not the chart —
    callers need the handle for updates/removal; documented in the docstring.
  - `_find_free_port` gained the family-aware probe + host-naming error (the
    literal spec only says "host configurable"; probing `"::"`/non-local
    hosts with an IPv4 socket cannot work).
  - Housekeeping per §1–§7 precedent: all 75 pre-existing ruff findings in
    the touched files fixed (quoted annotations, `Optional[...]` → `| None`,
    import sorts, deprecated `typing.Set`); black remains unavailable in this
    environment and the tree is not black-clean at baseline (§1) — new code
    follows the 100-char limit and surrounding style.
- **New files** — none.
- **Changed files**
  - `src/AlgoTradeKit/visual/models.py` — `COLOR_SL_*`/`COLOR_NEXT_TP`
    constants + `sl_zone_color()` (moved in from indicator_renderer, single
    source); new `LivePosition` dataclass; docstring; ruff fixes.
  - `src/AlgoTradeKit/visual/server.py` — `host` on `ChartServer` +
    `_find_free_port` (IPv6-aware probe, host-naming error); uvicorn binds
    it; `display_host`/`url` properties; browser-open + log via `url`;
    security warning; ruff fixes.
  - `src/AlgoTradeKit/visual/chart.py` — `Chart(host=, candle_count_limit=)`;
    central `_add_drawing()`; `add_live_position`/`update_live_position`;
    generic `update_drawing`; `set_candle_limit` + `_enforce_candle_limit` +
    `_drawing_expired`/`_drawing_end_time`; window enforcement in `stream()`
    and `set_data()`; cache refresh on remove/clear/browser edits;
    `candleCountLimit` in the init payload; `url` delegates to the server;
    ruff fixes.
  - `src/AlgoTradeKit/visual/indicator_renderer.py` — SL zone rule + colours
    imported from `models` (old private names aliased); `_draw_sl_line`/
    `_draw_tp_line`/`add_strategy_drawings` route through
    `chart._add_drawing` (live broadcast + fresh replay for free); ruff
    fixes.
  - `src/AlgoTradeKit/visual/static/index.html` — `live_position` renderer
    (entry/SL/next-TP lines to the newest bar, backend `sl_color`);
    `set_candle_limit` message + `candleCountLimit` from init;
    `drawingEndTime()`/`enforceCandleLimit()`; `streamBar` enforces the
    window each bar.
  - `src/AlgoTradeKit/visual/__init__.py` — export `LivePosition`; v1.0.0
    docstring section.
  - `tests/test_visual.py` — Group 13 (§8) appended; pre-existing ruff
    findings fixed.
- **Tests added** (`tests/test_visual.py`, 54 tests)
  - `TestHostConfig` (9): defaults, custom host in URL (explicit port),
    `0.0.0.0`/`::` substitution + `display_host`, Chart→server pass-through,
    host-aware port probe, probe-error names the host, and a live server
    bound to `0.0.0.0` served over `127.0.0.1`.
  - `TestUpdateDrawingGeneric` (6): full-dict broadcast, silent unknown-id,
    unknown-field ignore, posbox derived-zone recompute, raw strategy-drawing
    payload update, not-shown state-only update.
  - `TestLivePosition` (15): payload shape + JSON-serialisable, 6-case SL
    zone-colour matrix (long/short × loss/BE/profit), add/update/remove
    broadcasts + replay-cache effects, next-TP clear-vs-keep semantics,
    label update, invalid direction raises, unknown-id no-op.
  - `TestCandleLimit` (11): validation (0/negative, ctor + method), in-place
    bar trim, ctor limit applied on `set_data`, under-limit no-op, `None`
    disables, per-type drawing expiry matrix (hline/live kept; old signal/
    trendline/posbox dropped; spanning box kept), stream enforcement,
    `set_candle_limit` message, `candleCountLimit` in init, stream eviction
    refreshes the cached drawings + shared bars list.
  - `TestReplayCacheFresh` (7): add/update/remove/clear + browser-edit/
    browser-delete all land in the cached init; not-shown adds don't touch it.
  - `TestPushProtocolE2E` (1): real `ChartServer` + real `websockets` client —
    init replay, live-position add → risk-free recolour → window arm →
    candle stream → close (remove + final posbox) received in order; a
    second client (page refresh) replays the trimmed, armed, current state.
  - `TestFrontendWiring` (5): shipped `index.html` contains the
    `set_candle_limit`/`update_drawing` handlers, `live_position` renderer,
    trim machinery incl. indicator/cloud filtering, and `streamBar` calls
    `enforceCandleLimit()`.
  - Results: full suite `833 passed, 9 warnings in 16.29s` (779 baseline +
    54 new). Warnings are the pre-existing environmental set (system
    `websockets` deprecations + the known `ChartServer.stop()` thread
    warning; the count rose because two new §8 tests also start/stop real
    chart servers, adding instances of that same pre-existing warning).
    Ruff on all §8-touched files: `All checks passed!`
  - Out-of-pytest frontend verification (scratchpad, not committed): the
    whole `index.html` script block passes `node --check`, and the pure
    `drawingEndTime()` was executed in node against a 9-case expiry matrix —
    all correct (the browser-runtime parts are covered by the static wiring
    asserts + the Python-mirror tests).
- **Suggested commit messages**
  1. `feat(visual): chart host + live push + rolling window — Chart/ChartServer host with 0.0.0.0 URL substitution, LivePosition live SL/TP updates, generic update_drawing, set_candle_limit trim on both sides, always-fresh init replay`
  2. `test(visual): host matrix, live-position push, update_drawing, window-trim rules, replay-cache freshness, WebSocket e2e — 54 tests`

## Section 9 — report: live push + combined rendering — 2026-07-09

- **What was implemented**
  - **`ReportServer.push_update(report_or_payload)`** (v091 item 3): accepts
    a `SimulateReport` (serialised via `build_report_payload`) **or** a
    ready payload dict (so §20 can push combined payloads through the same
    method); broadcasts over the existing WebSocket — the open page
    re-renders in place — and refreshes the server's replay cache, so a
    page refresh / second tab mid-live-run shows the current stats (§8
    precedent). Chart-link keys (`has_chart`/`chart_port`) of the previous
    payload are carried forward when the new payload doesn't set them (the
    linked chart server doesn't change between stat refreshes); documented,
    explicit values win.
  - **Configurable `host`** — exact §8 mirror: `ReportServer(host=...)` and
    `show_report(host=...)` (default `127.0.0.1`, loud security warning),
    host-aware IPv6-capable port probe (`_find_free_port(start, host)`,
    failure names the probed host), `display_host`/`url` properties
    (`0.0.0.0`/`::` → `127.0.0.1` — bind-to-all is not a destination),
    uvicorn binds the host, browser-open + startup log use `url`.
  - **Combined-report rendering** (D9): new
    `build_combined_report_payload(pairs)` (dict or `(label, report)`
    iterable; labels unique/non-empty) — merged trade list (markers tagged
    `pair` + `uid = "label#trade_id"`, per-pair id sequences collide),
    equity curve **summed across accounts** over the union timeline
    (step-hold per account; before an account's first snapshot its
    `initial_balance`), portfolio-wide top-level stats, weekday/session/
    monthly tables summed key-wise, per-pair breakdown rows, merged config
    bar (identical → value, else `"mixed"`; `initial_balance` = sum,
    `drawdown_threshold` = first pair's). Entry points
    `show_combined_report(pairs, ...)` / `save_combined_report_html(pairs,
    path)` mirror the single-report pair; all exported from
    `AlgoTradeKit.report`. Frontend renders a new "Per-Pair Breakdown"
    table section (hidden for single reports), combined page title, pair
    row in trade tooltips, `uid`-keyed marker lookup, `"mixed"`-safe
    config badges. Combined page has `has_chart=False` (portfolio-level
    chart linking is §21+).
  - Portfolio formulas not derivable from per-pair numbers (streaks over
    the merged chronology; sharpe/sortino/calmar/recovery/drawdowns over
    the summed curve) are **mirrored locally** from `simulate/_report.py`
    (`report` may not import `simulate` — module boundary); everything
    else is aggregated from exact per-pair data. Parity is locked by test:
    a combined payload over ONE pair equals `build_report_payload` of that
    pair **exactly** (every summary/curve/stats/marker value; config
    differs only in `config_id`).
  - **Real-trades report** (D10): no code needed by design — a report
    built from broker-fill `ClosedTrade` records is a plain
    `SimulateReport`; locked by tests through payload/save/push/combined
    paths.
  - **Frontend live-re-render fixes** (pre-existing bugs exposed by
    push_update, fix confirmed in Q&A): equity-chart pan/zoom listeners
    now bound **once** (`_panZoomBound` guard — previously every re-render
    stacked another wheel/mousedown handler, so one wheel notch zoomed N×
    after N updates), and the user's active zoom/pan window is preserved
    across re-renders (x-range saved before `chart.destroy()`, re-applied
    after rebuild; double-click reset still returns to full range).
- **Deviations from the v100.md spec** (1–4 confirmed in Q&A before coding)
  - Combined **builder lives in the report module** (spec's "the trader
    builds the payload in §20" reads as the trader assembling per-pair
    reports and calling this support): `build_combined_report_payload` +
    show/save entry points land here; §20 just calls them.
  - Combined top-level stats = **full stats via locally mirrored formulas**
    (chosen over omitting non-summable stats), parity-locked as above.
  - `push_update` beyond the spec line: dict-payload input, replay-cache
    refresh, chart-link carry-forward.
  - Frontend duplicate-listener + zoom-reset fixes (not in the spec text;
    required for a usable live-updating page).
  - Stated defaults (announced before coding, not objected): `uid`/`pair`
    marker tags, `"mixed"` config-bar policy, `has_chart=False` for
    combined, breakdown table placed between equity curve and performance
    summary, `portfolio(N pairs)` config id.
  - Housekeeping per §1–§8 precedent: all 28 pre-existing ruff findings in
    the touched files fixed (typing.Optional/Set/Callable modernisation,
    import sorts, unused imports, one unused test variable); black remains
    unavailable in this environment and the tree is not black-clean at
    baseline (§1) — new code follows the 100-char limit and surrounding
    style.
- **New files** — none.
- **Changed files**
  - `src/AlgoTradeKit/report/_server.py` — `host` param + security warning,
    host-aware `_find_free_port`, `display_host`/`url`, uvicorn binds host,
    browser/log via `url`, `push_update()`; ruff fixes.
  - `src/AlgoTradeKit/report/_builder.py` — payload fragments factored
    (`_config_summary`/`_marker_payload`/`_balance_history_payload`/
    `_dd_payload`/`_grouped_stats_payload` — single output unchanged);
    `build_combined_report_payload` + mirrored portfolio maths
    (`_mirror_streaks/_mirror_sharpe/_mirror_sortino/_mirror_drawdowns`,
    `_sum_balance_histories`, `_sum_grouped_stats`,
    `_merge_config_summaries`); module-boundary docstring; ruff fixes.
  - `src/AlgoTradeKit/report/_display.py` — `show_report(host=...)`;
    `show_combined_report` / `save_combined_report_html`; shared
    `_serve_payload` / `_write_standalone_html` internals; ruff fixes.
  - `src/AlgoTradeKit/report/__init__.py` — export the three combined
    functions; v1.0.0 docstring section.
  - `src/AlgoTradeKit/report/static/report.html` — per-pair breakdown
    section + `renderPairsTable`, combined title, tooltip pair row,
    `uid` marker lookup, `fmtCfg` mixed-value guard, `_panZoomBound`
    bind-once, zoom/pan window preserved across re-renders.
  - `tests/test_report.py` — §9 groups 10–16 appended; pre-existing ruff
    findings fixed.
- **Tests added** (`tests/test_report.py`, 39 tests)
  - `TestReportServerHost` (6): defaults, custom host in URL, `0.0.0.0`/`::`
    display substitution, host-aware probe, probe error names the host,
    live server bound to `0.0.0.0` served over `127.0.0.1`.
  - `TestPushUpdate` (5): payload build + replay-cache write, dict
    passthrough without input mutation, chart-link carry-forward, explicit
    link wins, no-previous-payload → no link.
  - `TestReportPushProtocolE2E` (1): real server + real `websockets`
    client — initial replay, `push_update` re-broadcast with fresh stats +
    carried chart link, second client (page refresh) replays the UPDATED
    payload.
  - `TestCombinedReportPayload` (14): validation (empty/duplicate/invalid
    labels, dict input); **single-pair parity exact** (summary, curve,
    drawdowns, all stats tables, markers modulo pair/uid, config modulo
    config_id); two-pair sums; union-timeline step-hold summed curve
    (hand-computed, incl. pre-first-snapshot initial hold); merged markers
    sorted with pair/uid; streaks over merged chronology (3-streak across
    pairs no single pair reaches); drawdown on the summed curve (10% pair
    dip → 5% portfolio); grouped stats key-wise sums; per-pair breakdown
    rows vs single payloads; mixed config fields; JSON-safe.
  - `TestRealTradesReportRendering` (4): hand-built fill records →
    payload, standalone save, `push_update`, and as a pair inside a
    combined report.
  - `TestCombinedDisplayEntryPoints` (3): standalone combined HTML
    (embedded `"combined":true`), `show_combined_report` serves the
    payload, `show_report` host passthrough with URL substitution.
  - `TestReportFrontendWiring` (6): shipped `report.html` contains the
    pairs section/renderer, tooltip pair row, uid fallback, bind-once
    guard, zoom preservation, mixed-config guard.
  - Results: full suite `872 passed, 17 warnings in 17.17s` (833 baseline
    + 39 new; warnings are the pre-existing environmental set — system
    `websockets` deprecations + the known server-stop thread warning,
    count scales with how many real servers the run starts/stops). Ruff on
    all §9-touched files: `All checks passed!`
  - Out-of-pytest verification (scratchpad, not committed): the whole
    `report.html` script block passes `node --check`; end-to-end drive
    with two REAL `Simulate` runs — combined payload totals match the two
    reports, standalone combined HTML written, and a live WebSocket client
    received a single-report push followed by a combined-payload push
    through the same `push_update`.
- **Suggested commit messages**
  1. `feat(report): live push + combined rendering — ReportServer.push_update with replay-cache refresh and chart-link carry, configurable host with display-URL substitution, combined payload builder + per-pair breakdown page, bind-once pan/zoom + preserved zoom across re-renders`
  2. `test(report): host matrix, push_update semantics + WebSocket e2e, combined single-pair parity and portfolio aggregation, real-fills rendering, frontend wiring — 39 tests`

## Section 10 — simulate: `LiveSimulation` core — 2026-07-10

- **What was implemented**
  - New `simulate/_live.py` — `LiveSimulation(broker, strategy, config, ...)`,
    the internal engine behind `run_live` (§13) and the Trader display (§21):
    - **Seed**: fetches the primary-TF history from the broker (`display_candles`
      via `fetch_last_candles` **xor** `display_start`→now via `fetch_candles`;
      `display_start` parsed with `broker._timeutil.parse_to_ms`), runs
      `strategy.run(..., BACKTEST)`, replays the seed through a
      `SimulationStepper` **without finalizing** (seed-end open positions carry
      into the live phase, visible in `report.open_at_end`), then produces the
      initial chart + report exactly the existing `Simulate` way
      (`config.show_chart` / `report_mode` / `report_save_path` /
      `chart_indicators`; report page gets the `has_chart`/`chart_port` link and
      an "Open on Candle Chart" callback that reads the **current** snapshot's
      markers; keep-alive registered like `Simulate` when a browser UI opened).
    - **Step**: per closed candle — `advance_live_candle` (§5, honours
      `recompute_window`) → `stepper.step(candle, signals, exits)` → chart
      updates (final bar via `stream_from_atk`, live SL/TP line pushes from
      `sl_history` diffs via `update_live_position`, on full close the live
      drawing is removed and the final posbox + dynamic SL/TP segments are
      drawn from all accumulated partial slices) → `ReportServer.push_update`
      (webpage modes) → fresh `SimulateReport` snapshot to `on_report` and
      per-trade events to `on_event`.
    - **Feed**: `start()` subscribes `broker.stream_candles` itself
      (`closed_only=False` when a chart is shown so forming candles render,
      `True` otherwise) and routes by the candle's `closed` flag; `stop()` ends
      the stream (idempotent, restart allowed); public
      `process_closed_candle()` / `process_forming_candle()` let §21's queue
      drive it without the built-in feed. Duplicate/stale candles
      (ts ≤ last appended) are skipped silently; the feed callback warns
      instead of raising so one bad candle never kills the stream. Forming
      candles are display-only — strategy state and the sim never advance on
      them (fills/SL/TP on closed candles only).
    - **Window** (`candle_count_limit`, D6): candles kept in a bounded
      `deque(maxlen=N)`; once candles fall out, the windowed report is built
      via `build_report` over the trimmed inputs with
      `replace(config, initial_balance=baseline)` — baseline = **equity
      entering the window** (newest dropped balance snapshot; before anything
      drops, `config.initial_balance` and the report is the plain full one);
      `stepper.balance_history` / `stepper.closed_trades` are trimmed in place
      (a trade drops once `close_time < window start` — whole lifetime left;
      per `ClosedTrade` slice); the strategy master frame is trimmed to
      `max(N, recompute_window, warmup+1)` rows so signal maths never degrade;
      the chart trims itself to exactly N via §8's `Chart(candle_count_limit=N)`
      machinery (both sides). The same bookkeeping runs during the seed, so a
      seed longer than the window yields an already-windowed initial report.
  - **Events**: `on_event(dict)` with types `EVENT_SIGNAL / EVENT_EXIT_SIGNAL /
    EVENT_OPEN / EVENT_SL_MOVE (coalesced one per candle per position) /
    EVENT_TP_LEVEL (partial slice, position still open) / EVENT_CLOSE`; every
    event carries `type`, `time` (UTC ms), `symbol` plus rich objects
    (`Signal` / `ClosedTrade`); callback exceptions are warned, never raised.
    Constants re-exported from `simulate/__init__.py` (library convention).
    Seeding emits **no** events (historical replay).
  - **Visual reuse**: the per-trade body of
    `visual/indicator_renderer.add_simulation_positions` was factored into a
    module-level `draw_trade_group(chart, markers, *, opacity, config)`;
    the batch function now groups markers and delegates (output unchanged,
    test-locked), and `LiveSimulation` renders a live close through the same
    code path — live rendering equals batch rendering by construction.
- **Deviations from the v100.md spec** (all confirmed in Q&A before coding)
  - API shape (spec gives none): constructor kwargs
    (`display_candles`/`display_start` XOR-validated, `candle_count_limit`,
    `recompute_window`, `chart_host/chart_port/report_host/report_port`,
    `open_browser`, `on_event`/`on_report`); display on/off comes from the
    existing `SimulateConfig.show_chart`/`report_mode` (TraderConfig lands in
    §11 and will map onto these); owns-feed `start()/stop()` **plus** public
    step methods for §21's external driver.
  - "Per-trade events" shaped as generic dicts + `on_report` callback — typed
    events, `[SIM]` tagging and the terminal printer are §12's job
    (`simulate` may not import `trader`); no cause classification on SL moves.
  - Window scope: report + chart trim to exactly N; the strategy frame keeps
    `max(N, recompute_window, warmup+1)` rows (documented caveat: row indices
    shift — hook strategies must not store absolute row indices).
  - Baseline = equity **entering** the window (newest dropped snapshot);
    trade-drop rule `close_time < window start` (trades opened before the
    window but closed inside stay).
  - Per the "do everything completely" instruction, remaining behaviours follow
    the spec/"existing way" and are documented in the module docstring:
    keep-alive registered exactly like batch `Simulate` when a browser UI
    exists; `report_mode="save"`/`"both"` writes the standalone HTML once at
    seed (live pushes go to the webpage); **no gap-fill** here — stale/dup skip
    only, gap-fill is §14's job per the spec; `config.chart_indicators`
    computed once at seed and not recomputed per live candle (spec has no live
    indicator recompute; the browser toolbar still works).
  - Seed chart draws posbox groups only for **fully-closed** trades; trades
    still open at seed end render as §8 `LivePosition` drawings instead (the
    batch path never sees open trades — necessary divergence from "exactly the
    existing way", per the §8 report's contract that §10 owns the live⇄final
    drawing swap).
  - `simulate` now imports `broker._timeutil` (`parse_to_ms`, `now_ms`) —
    downstream→upstream per the flow diagram; the module-coupling notes in
    CLAUDE.md / PROJECT_STRUCTURE.md get refreshed in §23.
- **New files**
  - `src/AlgoTradeKit/simulate/_live.py`
- **Changed files**
  - `src/AlgoTradeKit/simulate/__init__.py` — export `LiveSimulation` +
    `EVENT_*` constants; docstring class list + constants line.
  - `src/AlgoTradeKit/visual/indicator_renderer.py` — new `draw_trade_group()`
    (verbatim per-trade body); `add_simulation_positions` groups + delegates.
  - `tests/test_simulate.py` — §10 test section appended (fake broker/stream,
    SMA + plan strategies, 4 test classes); import block extended.
- **Tests added** (`tests/test_simulate.py`, 32 tests)
  - `TestLiveSimulationSeed` (8): display_candles fetch args + frame; display
    _start fetch (parsed start, end="now"); seed does **not** finalize (open
    position carries, `open_at_end`); seed closed trades equal the batch run's
    non-EOD trades (full-precision repr); no events / no report callback during
    seed; double-seed + unseeded start/process raise; ctor validation matrix
    (XOR seed inputs, bounds, empty symbol, TF mismatch); empty fetch raises.
  - `TestLiveSimulationStep` (10): **seed+stream equals batch over the
    concatenated data** (byte-identical trades + balance history + final
    balance after aligning end-of-data); dup/stale skip; signal+open event
    payloads; close event + slice/seen-state cleanup; multi-RR ladder
    (tp_level slice + coalesced sl_move old→new→entry, then final close, chart
    state); trailing sl_move sequence (two moves, old/new chain, no TP);
    on_report once per closed candle; forming candle is display-only (master
    frame, stepper, events untouched; stale forming no-op); callback exception
    warns while the step still applies; feed routing (closed_only=True
    headless, forming vs closed, broken candle warns, stop/restart semantics,
    double-start raises).
  - `TestLiveSimulationWindow` (7): deque bounded + engagement + window start;
    **baseline = equity entering the window** cross-checked against the
    parity-proven batch balance history (plus windowed history length, final
    balance, total_pnl consistency); out-of-window trades dropped (windowed
    trade list equals the batch list filtered by close_time, with a real drop
    asserted); a trade opened pre-window but closed inside stays until its
    close leaves; frame/balance/trade lists stay bounded
    (`max(N, recompute, warmup+1)` frame rows); no-window run trims nothing;
    window engages during the seed (initial report already windowed).
  - `TestLiveSimulationDisplay` (7, real servers, `open_browser=False`, autouse
    fixture stubs the keep-alive so no atexit blocker runs in pytest):
    seed chart state (LivePosition for the open trade, posbox for the closed
    one, candle limit passed, keep-alive requested); live close swaps the live
    drawing for the final posbox + TrendLine segments after a multi-RR ladder
    (with intermediate SL/next-TP line pushes asserted); webpage report
    `push_update` refreshes the server replay payload per candle; save mode
    writes the standalone HTML at seed with no server and no keep-alive;
    `chart_indicators` specs load at seed; headless seed registers no
    keep-alive; `draw_trade_group` renders drawing-for-drawing identically to
    `add_simulation_positions` (ids stripped).
  - Results: full suite `904 passed, 23 warnings in 24.67s` (872 baseline + 32
    new; warnings are the pre-existing environmental set — system `websockets`
    deprecations + the known server-stop thread warning, whose count scales
    with how many real servers a run starts/stops). Ruff on all §10-touched
    files: `All checks passed!` black remains unavailable in this environment
    and the tree is not black-clean at baseline (§1) — new code follows the
    100-char limit and surrounding style.
  - Out-of-pytest E2E (scratch script, not committed): real chart + report
    servers driven through a fake broker feed — subscribe with
    `closed_only=False` (chart shown), forming candle rendered, open → live
    drawing, L1 partial → SL 100 / next-TP 102 pushed to the live line, full
    close → live drawing swapped for the final posbox, report replay payload
    refreshed with the chart link carried, terminal event flow
    `signal → open → tp_level → sl_move → close`.
- **Suggested commit messages**
  1. `feat(simulate): LiveSimulation core — seed/step/window live engine: broker-fed seeding without finalize, per-candle strategy+sim advance with chart/report push, per-trade event dicts, windowed report with equity-at-window-start baseline; draw_trade_group factored out of add_simulation_positions`
  2. `test(simulate): LiveSimulation — seed/step batch parity, event stream, windowed baseline + trade-drop correctness, display lifecycle incl. live→final drawing swap — 32 tests`

## Section 11 — trader: config & types — 2026-07-11

- **What was implemented**
  - New top-level `trader/` package (config & types layer only — `Trader` is
    §16, `run_live` is §13, `_events.py` is §12).
  - `trader/_config.py`:
    - Constants `EXEC_CANDLE_CLOSE / EXEC_CANDLE_UPDATE / EXEC_TICK` (D2),
      `DISPLAY_TRADES_SIM / REAL / BOTH` (D10), `ON_STOP_KEEP / CLOSE_ALL`
      (§18) — all re-exported from `trader/__init__.py` (library convention).
    - `TraderConfig` — same field names as `SimulateConfig` (D12: sizing,
      TP/SL incl. `force_close_on_exit_signal`, limits, `leverage`) + trader
      groups per the spec table: cost overrides (`spread` /
      `commission_type` / `commission`, `None` → auto), `execution`
      (default `candle_close`), data/feed (`min_candles` required,
      `recompute_window`, poll intervals), logging (`log_events`,
      `log_event_types` → `frozenset`), display (`display`,
      `display_trades`, `display_open_browser`, `chart_host`,
      `chart_port`/`report_port` 0 = auto, `display_candles` XOR
      `display_start`, `candle_count_limit`), safety (`max_daily_loss`
      $-or-percent, `close_on_daily_loss`).
    - Validation in `__post_init__`: trader-only rules directly; every
      mirrored field via a **probe `SimulateConfig`** (single source of
      validation truth; unset cost overrides use placeholders; messages
      re-prefixed `SimulateConfig.` → `TraderConfig.`). Seed rules: both
      seed fields → always error; `display=True` requires exactly one;
      `display_candles >= min_candles` whenever set.
    - `TraderConfig.to_simulate_config(broker, *, initial_balance,
      primary_timeframe)` — the §3-dependent display-config auto-derive:
      same-name fields copied (lists defensively copied), costs per-field
      from `broker.get_trading_costs(symbol)` only where the override is
      `None` (venue not queried when fully overridden), `exchange_type`
      from the broker (`MetaTraderBroker` → `"metatrader"`, else
      `"exchange"`), `display` → `show_chart` + `report_mode`
      (`webpage`/`none`, per §10's mapping note).
    - `TraderPair(broker, config, strategy)` — broker duck-typed
      (LiveSimulation precedent; `None` rejected), `config`/`strategy`
      isinstance-checked. `validate_pairs(pairs)` — D9 rule at config time:
      ≥ 1 entry, all `TraderPair`, duplicate `(broker, symbol)` rejected
      (broker by object identity, symbol case-insensitive); returns a new
      list (§16/§20 consume it).
    - `TraderSettings` — the Trader-level constructor kwargs (`on_stop`,
      `state_path`, `kill_switch_file`): fields + validation land here per
      spec, features in §18/§19; paths normalised to `str` via
      `os.fspath`, `state_path=None` kept (→ `./.atk_trader_state.json`
      resolved by §19).
    - `parse_max_daily_loss(value)` → `("amount", $)` or `("percent", p)`
      — validates here, reused by the §18 daily-loss gate (number = $,
      `"2%"` = percent of the UTC-day-start balance, 0 < p <= 100).
- **Deviations from the v100.md spec** (1–8 confirmed in Q&A — "continue
  with your defaults")
  - Trader-level kwargs shaped as the `TraderSettings` dataclass (Trader
    class doesn't exist until §16; spec: "the fields and validation land
    here").
  - Derive = `to_simulate_config` method with `initial_balance` /
    `primary_timeframe` as **parameters** (neither is a TraderConfig field;
    wallet is the real account's or the paper default, timeframe is the
    strategy's), per-field cost fallback, isinstance-based
    `exchange_type`, `display` → `show_chart`/`report_mode` mapping.
  - `force_close_on_exit_signal` added as a mirrored field (spec table
    omits it; §15 uses it — "ExitSignal + force_close_on_exit_signal →
    close_position at market").
  - Seed XOR semantics: both set always rejected; one-alone allowed with
    `display=False` (ignored later); `>= min_candles` enforced whenever
    `display_candles` is set (`display_start` count check is seed-time,
    §13/§21).
  - `max_daily_loss` encoding: number = dollars, `"N%"` string = percent of
    UTC-day-start balance; `bool` explicitly rejected.
  - `log_event_types` membership validation deferred to §12 (event-type
    constants don't exist yet); any string collection accepted, stored as
    `frozenset`.
  - Mirrored validation via probe `SimulateConfig` (zero duplicated rules)
    instead of re-implemented checks.
  - Duplicate detection: broker object identity + case-insensitive symbol.
  - Announced defaults (not objected): `execution` defaults to
    `candle_close` (the periodic D2 mode); `close_on_daily_loss=True`
    without `max_daily_loss` is a config error; `validate_pairs` rejects an
    empty list and raises `TypeError` for non-`TraderPair` entries;
    `min_candles` sits second in field order (required → no default, spec
    table groups it under Data); `TraderSettings` / `validate_pairs` /
    `parse_max_daily_loss` are public names inside the private `_config`
    module but **not** package exports (spec's `__init__` list is
    `Trader, TraderConfig, TraderPair, run_live, constants`); ports
    validated 0–65535 with 0 = auto-pick.
- **New files**
  - `src/AlgoTradeKit/trader/__init__.py`
  - `src/AlgoTradeKit/trader/_config.py`
  - `tests/test_trader.py`
- **Changed files** — none.
- **Tests added** (`tests/test_trader.py`, 59 tests)
  - Defaults: spec-table defaults per group; mirrored defaults equal
    `SimulateConfig()` field-for-field; `min_candles` int coercion.
  - Validation: symbol/min_candles required; execution + display_trades
    membership; cost-override bounds and `TraderConfig.`-prefixed probe
    errors (leverage, risk_per_trade, max_positions, tp/sl modes, fraction
    rules); poll intervals; recompute_window; candle_count_limit; ports;
    chart_host; `log_event_types` frozenset normalisation + non-str
    rejection.
  - `max_daily_loss`: valid $/percent forms kept as given; parse helper
    tuples; 8 invalid forms (incl. `True` and `"150%"`);
    `close_on_daily_loss` gate.
  - Seed XOR: both-set rejected under both display states; display-on
    requires a seed; either seed accepted; one-alone with display off
    allowed; `>= min_candles` on and off; int coercion.
  - `TraderPair` type checks; `validate_pairs`: duplicate (same broker,
    same/case-different symbol) rejected, same-broker-different-symbol and
    same-symbol-different-broker allowed, empty list, non-pair entry,
    list-copy isolation from any iterable.
  - `TraderSettings`: defaults, `on_stop` membership, `Path` → `str`
    normalisation, bad path types.
  - `to_simulate_config`: venue costs auto-filled (called once, with the
    symbol); full override skips the venue entirely; partial override
    mixes user + venue; 16 mirrored fields copied; lists copied not
    shared; `MetaTraderBroker` (offline, injected transport) →
    `"metatrader"`; display on/off → `show_chart`/`report_mode`.
  - Exports: constant values; `__all__` exact set and importable.
  - Results: full suite `963 passed, 23 warnings in 22.99s` (904 baseline
    + 59 new; warnings are the pre-existing environmental set from §3/§10 —
    system `websockets` deprecations + the known server-stop thread
    warning). Ruff on all §11 files: `All checks passed!` black remains
    unavailable in this environment and the tree is not black-clean at
    baseline (§1) — new code follows the 100-char limit and surrounding
    style.
- **Suggested commit messages**
  1. `feat(trader): config & types — TraderConfig mirroring SimulateConfig + trader groups, TraderPair with duplicate-(broker,symbol) validation, TraderSettings, display-SimulateConfig auto-derive from broker costs`
  2. `test(trader): config validation — seed XOR, display_candles >= min_candles, duplicate pairs, probe-error prefixes, cost derivation, constants export — 59 tests`

## Section 12 — trader: event stream + terminal log (D13) — 2026-07-11

- **What was implemented**
  - New `trader/_events.py` — the D13 event system used by both `run_live`
    (§13) and `Trader` (§16):
    - **Typed events** — one frozen `kw_only` dataclass per type on a common
      base `TraderEvent` (`time` UTC ms, int-coerced; `symbol`; `source`
      validated ∈ {`sim`, `live`}); `event_type` is a ClassVar carrying the
      type constant so subscribers dispatch without isinstance chains. Ten
      types: `SignalEvent` (direction, entry, SL, TP?, size?, risk?, RR?,
      timeframe, metadata, originating `Signal`), `OpenEvent` (trade_id,
      direction, fill price, size, margin, risk?, SL?, next-TP?, venue
      `order_id` — `None` for sim fills), `SlMoveEvent` (old→new SL, cause
      default `"trailing"`, next_tp?), `RiskFreeEvent` (RR level touched,
      old→new SL), `TpLevelEvent` (level, fraction closed, realized PnL, new
      SL?, partial `ClosedTrade` slice), `CloseEvent` (exit price, reason,
      gross/net PnL, R multiple, duration, `ClosedTrade`), `ExitSignalEvent`
      (reason, action, `ExitSignal`), `DailyLossEvent` (limit $-or-`"N%"`,
      loss, closed_all), `ReconcileEvent` (adopted / closed_offline / foreign
      label tuples, sequences normalised), `ErrorEvent` (where, venue
      message, will_retry, details).
    - **Constants**: the six sim-known types are **imported from**
      `AlgoTradeKit.simulate` (values shared by construction — same precedent
      as `_config` importing `TP_MODE_SIGNAL`); the four trader-only types
      (`EVENT_RISK_FREE / EVENT_DAILY_LOSS / EVENT_RECONCILE / EVENT_ERROR`)
      plus `SOURCE_SIM` / `SOURCE_LIVE` and `ALL_EVENT_TYPES` (the
      `log_event_types` domain) are defined here.
    - **`EventStream`** — `subscribe(callback)` → idempotent unsubscribe
      callable; `emit(event)` iterates a snapshot (subscribers may
      unsubscribe mid-emit) with per-subscriber exception isolation (warn +
      skip, never raise — LiveSimulation's rule: one bad notification backend
      must not break trading); `__len__`.
    - **`TerminalEventPrinter`** — callable subscriber printing one
      timestamped grep-able line per event: `YYYY-MM-DD HH:MM:SS` (UTC) +
      `[SIM]`/`[LIVE]` + `[SYMBOL]` prefix, then the §12 table's printed
      detail. `event_types=None` = all / collection = filter; injectable
      `out` sink (default `print`); public `format_event()`; unknown/future
      event types fall back to a generic `TYPE k=v` body instead of crashing.
    - **`attach_terminal_printer(stream, config, *, out=None)`** — the one
      call §13/§16 make: `log_events=False` → nothing attached, returns
      `None`; else subscribes a printer filtered by `log_event_types` and
      returns its unsubscribe.
  - `_config.py`: the §11-deferred `log_event_types` **membership
    validation** now enforced against `ALL_EVENT_TYPES` — unknown types raise
    a ValueError naming them and listing the valid set.
  - `trader/__init__.py`: §12 surface re-exported (10 event classes +
    `TraderEvent`, `EventStream`, `TerminalEventPrinter`,
    `attach_terminal_printer`, 10 `EVENT_*`, `ALL_EVENT_TYPES`, `SOURCE_*`)
    with a docstring section.
  - No LiveSimulation wiring — bridging its event dicts onto these types is
    §13's job (per the §10 report's contract).
- **Deviations from the v100.md spec** (1–4 confirmed in Q&A before coding)
  1. API surface (spec names none): `EventStream` + printer + the
     config-aware `attach_terminal_printer` helper, so §26's
     "`log_events=False` silences" is testable at §12 level.
  2. The six shared event-type constants are imported from `simulate`, not
     re-declared (trader may import simulate — unlike §3's broker case).
  3. Enriched event fields: the spec table's printed detail **plus**
     `trade_id`/`direction` identifiers and optional rich objects
     (`signal` / `exit_signal` / `trade`) mirroring §10's payloads — v1.1
     notification backends get full data; optional numerics (e.g. SIGNAL
     size/risk, not always computable at signal time) print only when set.
  4. Full public export from `AlgoTradeKit.trader` (spec's §11 layout comment
     says "constants"; §7/§10 precedent chose public — subscribers are the
     D14 extension point).
  - Announced defaults (stated before coding, not objected): plain grep-able
    numbers (`64250` — not the spec example's `64 250` thousands spacing,
    which breaks grep/awk), UTC `YYYY-MM-DD HH:MM:SS` stamp, `$` on money,
    `.2f` ratios, humanized durations (`2h 15m`), `None` fields omitted from
    lines, `source` membership validation on the base event, generic-body
    fallback for unknown types.
- **New files**
  - `src/AlgoTradeKit/trader/_events.py`
- **Changed files**
  - `src/AlgoTradeKit/trader/_config.py` — `log_event_types` membership
    validation against `ALL_EVENT_TYPES` (§11 deferral resolved); docstring
    updated.
  - `src/AlgoTradeKit/trader/__init__.py` — §12 exports + docstring section.
  - `tests/test_trader.py` — §12 test section appended; `test_package_all`
    extended to the new export set; header imports/docstring updated.
- **Tests added** (`tests/test_trader.py`, 51 tests)
  - Constants: trader-only values; the six shared ones **are** simulate's
    objects; `ALL_EVENT_TYPES` = exactly the 10; per-class `event_type`.
  - Dataclasses: frozen, kw_only, source validated, time int-coerced,
    base-class isinstance, optional-field defaults, `ReconcileEvent` tuple
    normalisation, rich objects ride along.
  - EventStream: in-order delivery to all subscribers, event object
    identity, unsubscribe stops delivery + idempotent, failing subscriber
    warns while others still run, unsubscribe-during-emit safe, non-callable
    rejected.
  - Printer lines (§26 "every event type printed once with full detail"):
    exact full-detail line asserted for all 10 types, optional-omission
    (signal without TP/size/meta, sim open without order id), ladder cause +
    next_tp, negative PnL `-$` money, `30s` and `2h 15m` durations, `$` vs
    `"2%"` daily-loss limits, empty-symbol prefix, UTC-stamp proof,
    grep-prefix sweep over all 10 types, generic fallback for an unknown
    type, default out = `print` (capsys), `format_event` prints nothing.
  - Filtering (§26): no filter prints all; type filter selects only those;
    empty frozenset silences; `attach_terminal_printer` attaches + prints,
    `log_events=False` → `None` + silence, config filter honoured,
    unsubscribe detaches, default sink is stdout.
  - Config: all valid constants accepted; unknown type rejected naming it
    and listing the valid set.
  - Results: full suite `1014 passed, 23 warnings in 22.65s` (963 baseline +
    51 new; warnings are the pre-existing environmental set from §3/§10).
    Ruff on all §12-touched files: `All checks passed!` black remains
    unavailable in this environment and the tree is not black-clean at
    baseline (§1) — new code follows the 100-char limit and surrounding
    style.
  - Out-of-pytest E2E (scratch, not committed): all 10 event types driven
    through `EventStream` → `attach_terminal_printer(stream,
    TraderConfig(...))` → stdout — a full trade lifecycle (signal → open →
    trailing move → risk-free → TP level → close) plus
    exit-signal / daily-loss / reconcile / error lines, all correctly
    stamped and tagged.
- **Suggested commit messages**
  1. `feat(trader): event stream + terminal log — typed events for every live-trading moment, EventStream pub/sub with subscriber isolation, TerminalEventPrinter + attach_terminal_printer, log_event_types membership validation`
  2. `test(trader): event stream — full-detail line per event type, log_events=False silences, log_event_types filters, stream/printer semantics — 51 tests`

## Section 13 — trader: `run_live()` paper trading (D8) — 2026-07-12

- **What was implemented**
  - New `trader/_run_live.py` — `run_live()`, the paper-trading entry point:
    - **Single-pair form** per spec (`run_live(strategy=…, broker=…,
      config=…)` — all three required together) and the **multi-pair form**
      (`run_live(pairs=[TraderPair, …])`, mutually exclusive with the single
      form); both funnel through `validate_pairs` (duplicate `(broker,
      symbol)` rejected).  Per-pair chart + report + `[SIM]` event log —
      combined report arrives with §20, per the spec.
    - **Zero order code**: never imports `trader/_execution.py` (doesn't
      exist yet; test-locked via `sys.modules`), never calls an order method
      — only `fetch_candles`/`fetch_last_candles`/`stream_candles` and (when
      costs aren't overridden) `get_trading_costs`.
    - Per pair: `display_trades != "sim"` raises (no real fills in paper
      mode); `display=False` **and** `log_events=False` warns ("a silent
      paper run is useless") and still runs.
    - Display config auto-derived via §11's
      `TraderConfig.to_simulate_config(broker, initial_balance,
      primary_timeframe=strategy.primary_timeframe)`; `chart_host` /
      `chart_port` / `report_port` / `candle_count_limit` /
      `recompute_window` / `display_open_browser` pass to `LiveSimulation`
      (§10).  Seed source: `display_candles` **or** `display_start` whenever
      set (display on or off), else the last `min_candles`; the fetched
      count must reach `min_candles` in every path.  With `display=True` and
      `display_open_browser=False`, the chart/report URLs are printed (§21
      print-URL flow, paper edition).
    - **Blocking run**: seeds all pairs → starts all feeds → blocks until
      Ctrl+C **or** every feed stream has ended (each dead feed prints a
      note naming the pair); feeds are always stopped cleanly in a
      `finally`.  Returns the final `SimulateReport` snapshot(s): single
      form → one report, `pairs` form → list in pair order.  With a browser
      UI, §10's keep-alive still holds the servers up after the script ends
      until one more Ctrl+C (documented).
    - `_EventBridge` — the §12-contract bridge from `LiveSimulation`'s dict
      events onto the typed events, tagged `SOURCE_SIM`, one `EventStream` +
      `attach_terminal_printer(stream, config)` per pair:
      - `signal` → `SignalEvent` (rr computed from entry/SL/TP when the
        signal has a TP; size/risk omitted — the sim sizes at open);
        `exit_signal` → `ExitSignalEvent` (`action="force_close"` iff
        `force_close_on_exit_signal`); `open` → `OpenEvent`
        (`order_id=None` — sim fill); `close` → `CloseEvent` (all PnL
        fields + the `ClosedTrade`).
      - `tp_level` → `TpLevelEvent`: `level` = the RR value from
        `config.tp_levels[slice.rr_levels_hit − 1]`, `fraction_closed` =
        `slice.size / position.original_size` (exact by construction),
        `new_sl` read from the position (the ladder already moved it).
      - SL-move classification (the dicts carry no cause): an `sl_move`
        accompanied by a `tp_level` for the same trade on the same candle is
        **suppressed** — the ladder move rides `TpLevelEvent.new_sl` (§12's
        "ladder moves ride TpLevelEvent"); a remaining `sl_move` whose
        position advanced `last_rr_hit` (fraction-less multi-RR) →
        `SlMoveEvent(cause="ladder")`; anything else →
        `cause="trailing"`.
      - **Risk-free detection** (the Q&A-resolved conflict, see deviations):
        once per closed candle (the `on_report` hook) the bridge scans the
        stepper's open positions for `risk_free_triggered` flips and emits
        `RiskFreeEvent(rr_level=config.risk_free_at_rr, old_sl` from bridge
        memory`, new_sl=`break-even`)`; `prime()` snapshots seed-carried
        positions right after `seed()` so their historical state never
        re-fires as live events.
  - `simulate/_live.py`: new optional `LiveSimulation(min_seed_candles=…)` —
    enforced right after the seed fetch (before the strategy or any server
    runs).  This is the §11-deferred "`display_start` count check is
    seed-time" landing in §13; it also catches a venue whose history is
    shorter than `display_candles`.
  - Exports: `run_live` from `trader/__init__.py` (docstring section +
    `__all__`) and **lazily** from the package top level
    (`AlgoTradeKit/__init__.py` module `__getattr__` + `__all__`) — spec's
    "also re-exported at package top level" without making
    `import AlgoTradeKit` pull the pandas stack.
- **Deviations from the v100.md spec** (all confirmed in Q&A before coding)
  - **Conflict found and resolved — bridge-side risk-free detection**: the
    engine's `check_risk_free()` moves the SL to break-even **without**
    recording it in `sl_history`, so `LiveSimulation` (which detects SL
    moves via `sl_history` growth) emits no event at all for a risk-free
    jump — the bridge cannot map what never arrives, yet D13/§12/§13 list
    the risk-free touch in run_live's log.  Chosen fix (option a): the
    bridge detects flips itself via the per-candle position scan above; no
    simulate output changes (option b would have broken the §6/§7
    byte-identity guarantees).  Documented edge: a jump whose break-even SL
    is hit on the very same candle prints only `CLOSE` (reason `rf`) — the
    position is already gone when the scan runs.
  - Headless seed source: `display_candles`/`display_start` are honored
    whenever set even with `display=False` (rather than §11's tentative
    "ignored later" reading); with neither set, the seed is the last
    `min_candles`.
  - `initial_balance=10_000.0` keyword added to `run_live` (spec's "paper
    default" made tunable; applied per pair — each pair gets its own
    independent paper wallet).
  - Exit semantics: blocks until Ctrl+C **or** all feeds died; returns the
    final report(s) (spec is silent on both).
  - `execution` and the poll-interval config fields are inert in paper mode
    (documented): sim fills are candle-based (§10) and polling venues stream
    at the broker's own §2 intervals — run_live never mutates the user's
    broker.  They drive the real trader (§16).
  - Announced defaults (stated before coding, not objected): lazy top-level
    re-export; one `EventStream`/printer per pair (a pair's `log_event_types`
    filter must not leak across pairs); the both-outputs-off warning fires
    per silent pair in multi-pair form; `min_seed_candles` lives on
    `LiveSimulation` (a §10 file) as a backward-compatible optional param.
- **New files**
  - `src/AlgoTradeKit/trader/_run_live.py`
- **Changed files**
  - `src/AlgoTradeKit/simulate/_live.py` — `min_seed_candles` ctor param +
    validation + fetch-time check; docstring entry.
  - `src/AlgoTradeKit/trader/__init__.py` — export `run_live`; docstring
    Functions section; "§13 lands later" note removed.
  - `src/AlgoTradeKit/__init__.py` — lazy `__getattr__` re-export of
    `run_live` + `__all__`.
  - `tests/test_trader.py` — §13 test section appended (paper broker/stream
    doubles, plan strategy, bridge kit); imports extended;
    `test_package_all` updated for the new export.
- **Tests added** (`tests/test_trader.py`, 41 tests)
  - `TestRunLivePaperGuarantees` (5, §26's list): zero order calls on a
    recording broker double whose order methods also raise + only
    market-data/cost call kinds seen + a real closed trade in the returned
    report; `trader._execution` never in `sys.modules`;
    `display_trades="real"`/`"both"` raise; both-outputs-off warns and still
    runs; log-on run emits no silent-run warning.
  - `TestRunLiveApi` (8): single form requires all three; pairs XOR single
    form; duplicate `(broker, symbol)` rejected; `initial_balance`
    validation and flow into `report.initial_balance`; multi-pair returns
    reports in pair order (trade counts per pair asserted, shared broker
    instance); silent-pair warning names the pair; top-level lazy re-export
    (`AlgoTradeKit.run_live is trader.run_live`, `__all__`, unknown
    attribute still raises).
  - `TestRunLiveSeeding` (5): headless default fetches exactly
    `min_candles`; `display_candles` honored with display off;
    `display_start` honored (range fetch args); `display_start` yielding
    fewer than `min_candles` raises before any subscription; venue history
    shorter than `display_candles` raises.
  - `TestRunLiveEventBridge` (14, bridge unit): signal mapping (fields, rr,
    metadata copy, `[SIM]` source) + no-TP and zero-risk rr guards;
    exit-signal action follows `force_close_on_exit_signal`; open mapping +
    memory priming; close mapping + memory cleanup; tp-level mapping (level
    value, fraction from original size, `new_sl` from the advanced
    position, mark set); same-candle `sl_move` suppression after a
    tp_level; ladder cause without partial close; trailing cause;
    risk-free scan emits exactly once with old/new SL and configured RR;
    scan gated by `risk_free_enabled`/multi-RR; `prime()` prevents
    seed-state re-fires; unknown event types ignored.
  - `TestRunLiveLifecycleAndLog` (6, e2e through `run_live`): `[SIM]`-tagged
    SIGNAL→OPEN→CLOSE lines in order with exact bodies and no `[LIVE]` tag;
    feed-end notes; Ctrl+C (real `interrupt_main` while blocked) returns the
    report and stops the stream; risk-free line end-to-end (`RISK_FREE #0
    rr=1.00 touched, sl 95 -> 100 (break-even)`); trailing `SL_MOVE …
    (trailing)` line end-to-end; multi-RR ladder end-to-end (`TP_LEVEL …
    closed=50% … new_sl=100`, **no** standalone SL_MOVE line, final
    remainder CLOSE, slice reasons `["tp_rr", "tp"]`).
  - `TestRunLiveDisplay` (1, real chart + report servers, keep-alive
    stubbed): `display_open_browser=False` prints both URLs; the feed
    subscribes with `closed_only=False` when a chart is shown.
  - Results: full suite `1055 passed, 22 warnings in 25.07s` (1014 baseline
    + 41 new; warnings are the pre-existing environmental set — system
    `websockets` deprecations + the known server-stop thread warning, count
    scaling with how many real servers a run starts).  Ruff on all
    §13-touched files: `All checks passed!`  black remains unavailable in
    this environment and the tree is not black-clean at baseline (§1) — new
    code follows the 100-char limit and surrounding style.
  - Out-of-pytest smokes (scratch, not committed): full lifecycles driven
    through the real terminal printer — TP trade, risk-free jump (incl. the
    same-candle-close edge showing only `CLOSE reason=rf`), fraction-less
    ladder, trailing SL — all lines correctly stamped, tagged and grep-able.
- **Suggested commit messages**
  1. `feat(trader): run_live() paper trading — LiveSimulation-backed sessions with zero order code, TraderConfig/TraderPair API, per-pair [SIM] event bridge with ladder/trailing/risk-free classification, seed-count check via LiveSimulation.min_seed_candles, top-level re-export`
  2. `test(trader): run_live — zero-orders guarantee, _execution never imported, [SIM] tagging, display_trades=real raises, silent-run warning, seeding matrix, bridge classification, Ctrl+C/feed-death lifecycle, URL printing — 41 tests`

## Section 14 — trader: exact candle-close scheduler — 2026-07-12

- **What was implemented**
  - New `trader/_scheduler.py` — the §14/D2 machinery, standalone (§16
    consumes it later; not exported from `trader/__init__`, per the §11
    export list):
    - **Pure boundary arithmetic** (module functions, reused by §16 as
      closed-candle guards): `timeframe_ms()` (normalises, rejects the
      variable-length `"1M"`), `candle_close_ms()`, `is_candle_final()` —
      the mandatory venue quirk: a candle with open `T` is final once
      `venue_now >= T + tf`, **never** "a newer row appeared" —
      `next_boundary_ms()`, `newest_final_open_ms()`.
    - **`CandleCloseScheduler(broker, symbol, timeframe, *,
      last_open_ms=None, …knobs)`** — blocking, owns the fetch.
      `wait_next_close()` computes the next boundary on the **venue clock**
      (`now_ms() + clock_offset_ms()`, §3; the offset is refreshed exactly
      once per wait, *before* sleeping, so a TTL re-measurement can never
      land inside the fine wait), coarse-sleeps to `fine_wait_ms` before the
      boundary (suspend-safe recompute loop), fine-waits in `fine_tick_ms`
      steps and fires the instant the venue clock reaches the boundary —
      never early, never late. Lateness (`fired_at − boundary`) beyond
      `late_warn_ms` is logged and always reported on the tick.
    - **Fetching**: tail only — `fetch_last_candles(tail_count=5)`;
      a gap ≥ `tail_count` (downtime) switches to one ranged
      `fetch_candles(processed+tf, venue_now)` over exactly the gap, never
      the full history; all newly closed candles return ascending on one
      tick. If the venue's tail response lags the just-closed bar, the fetch
      retries on a growing backoff (250 ms doubling, capped 5 s) and the
      arrival delay is logged. Fetch exceptions are logged and count as
      retries (one bad call never kills the loop).
    - **Give-up + recovery**: the retry ends early when a *later* bar
      appears (venues print bars in order → the missing interval had no
      trades; logged, grid skipped past it) or after `fetch_timeout`
      (default 30 s, venue-clock measured). An incomplete tick never
      advances past the missing bar — a late print is recovered by the next
      boundary's gap-fill — and defers the next fire to the next future
      grid point: a dead market (forex weekend) costs one bounded attempt
      per boundary, not a 250 ms spin.
    - **Boundary grid = phase from the venue's own candles**
      (`processed_open + k·tf`, re-anchored to the real open of every
      returned candle) — correct for MT5 server-timezone-aligned 4h/1d
      (EET), Binance's Monday-open `1w`, and self-correcting one bar after
      a DST re-phase; epoch-identical for Binance intraday.
    - **`SchedulerTick`** (frozen): `boundary_ms`, `fired_at_ms`, `late_ms`,
      `candles` (tuple, may be empty), `retries`, `fetch_delay_ms`,
      `complete` (False = give-up, recoverable). `None` from
      `wait_next_close()` = stopped.
    - Lifecycle/misc: `stop()` + shareable `stop_event` interrupt any
      sleep/retry promptly; `last_open_ms=None` → first wait baselines on
      the venue's newest *final* candle (history never fires; no final
      candle → `ValueError`); `venue_now_ms()` helper; injectable `log`
      callable defaulting to an `[AlgoTradeKit] scheduler SYMBOL tf:`-
      prefixed `print`; knob validation in the ctor.
- **Deviations from the v100.md spec** (1–4 confirmed in Q&A before coding)
  1. API shape (spec gives none): blocking class that owns fetch/gap-fill/
     retry (§14 items 1–4 all in one module), pure helpers exposed for §16.
  2. Boundary grid phase from venue candles rather than literal epoch-UTC
     multiples (which are wrong for `1w` and tz-offset MT5 4h/1d).
  3. The literal "retry … until the expected closed candle arrives" is
     bounded: later-bar absence proof + `fetch_timeout`, with the
     no-advance/gap-fill guarantee making early give-up lossless. Chosen
     over spec-literal infinite retry (would poll a closed market for days).
  4. Knobs are ctor kwargs + module default constants; `TraderConfig`
     untouched (§11's spec table defines no scheduler fields).
  - Announced defaults (stated before coding, not objected): offset
    refreshed once per wait pre-sleep; module not exported from
    `trader/__init__`; `SchedulerTick` shape; `"1M"` rejected; default
    print logger; `None`-seed baseline semantics; empty-candles ticks
    allowed; tests appended to `tests/test_trader.py`.
  - Implementation decisions documented in code and test-locked: on a
    complete tick the grid anchor advances to the newest *real* bar open
    (`expected` only when the tick is empty-by-proof) — this is what makes
    the DST re-anchor work; consequence: a trade-less interval at the top
    of a gap resolves via one extra immediate empty tick. `fetch_timeout`
    is measured on the venue clock (test-friendly, wall-jump safe).
- **New files**
  - `src/AlgoTradeKit/trader/_scheduler.py`
- **Changed files**
  - `tests/test_trader.py` — §14 test section appended (scripted
    clock/venue kit with a deterministic `_wait` driver); header imports +
    module docstring extended.
- **Tests added** (`tests/test_trader.py`, 36 tests)
  - `TestSchedulerArithmetic` (6): timeframe table + normalisation, `"1M"`/
    unknown rejected, close arithmetic, **finality at the exact boundary
    with no candle rows involved**, next-boundary, newest-final matrix.
  - `TestSchedulerWaitFire` (8): sleeps then fires exactly at the boundary
    (venue clock lands on it to the ms); venue-clock-not-local (local 2 min
    behind; offset queried exactly once per wait — never in the fine loop);
    late fire detected + logged (90 s, no sleeping); sub-threshold lateness
    not warned; suspend-safe coarse recompute (half-waits still fire
    exactly, never early); stopped-before returns `None`; stop during
    sleep; a **real** `threading.Event` stop interrupting a real ~1 h sleep
    in <5 s.
  - `TestSchedulerFetchQuirk` (16): the quirk — expected bar evaluated with
    **no newer row** present; forming bar excluded arithmetically; venue
    lag retried until arrival (miss/miss/hit at +750 ms: retries, delay,
    log line); backoff doubles 250→500→1000 capped; timeout give-up
    (incomplete, empty, anchor unmoved, delay = budget, logged); give-up
    defers to the next grid boundary (weekend semantics — no refire spin);
    absence-proof skips a trade-less interval (+ normal continuation);
    hole-at-top partial then resolve; downtime gap-fill via one ranged
    fetch (exact gap bounds, no tail call, 7 candles ascending, both logs);
    small catch-up stays on the tail fetch; fetch errors retried then
    succeed; late bar recovered by the next boundary's gap-fill; DST
    re-phase re-anchors the grid (back to exact-time one boundary later);
    baseline-from-venue (history never fires); baseline with no final
    candle raises; candles strictly after `last_open` (history dedup).
  - `TestSchedulerConfigAndLogging` (6): 14-case ctor validation matrix,
    defaults == module constants (5 / 500 / 10 / 250 / 5000 / 30.0 / 1000),
    symbol strip + timeframe normalisation, default log prints the exact
    prefixed line, `venue_now_ms()` applies the offset, tick frozen.
  - Results: full suite `1091 passed, 21 warnings in 25.26s` (1055 baseline
    + 36 new; warnings are the pre-existing environmental set — system
    `websockets` deprecations + the known server-stop thread warning, count
    varies run-to-run with the real-server tests). Ruff on both §14-touched
    files: `All checks passed!` black remains unavailable in this
    environment and the tree is not black-clean at baseline (§1) — new code
    follows the 100-char limit and surrounding style.
  - Out-of-pytest smoke (scratchpad, not committed): a seven-tick story
    through the real default printer — on-time quirk fire, 900 ms venue lag
    (3 retries logged), 7-candle downtime gap-fill with late-fire log,
    trade-less interval skipped by proof, dead-market give-up, and the
    given-up bar recovered by the next boundary's fetch — all lines correct.
- **Suggested commit messages**
  1. `feat(trader): exact candle-close scheduler — venue-clock boundaries with phase from venue candles, coarse+fine wait that never fires late, arithmetic closed-candle detection, lag retry with capped backoff, downtime gap-fill, trade-less-interval proof and bounded give-up with gap-fill recovery`
  2. `test(trader): scheduler — boundary arithmetic incl. the no-candle-until-first-trade quirk, venue-clock use, lateness detection, backoff/give-up/recovery, DST grid re-anchoring, stop semantics — 36 tests`

## Section 15 — trader: execution core (entries + venue SL/TP) — 2026-07-12

- **What was implemented**
  - New `trader/_execution.py` — `ExecutionEngine`, the per-pair §15 core
    (§16 drives it later; private module, nothing exported from
    `trader/__init__`, per the §11 export list):
    - **Entries**: signal → **market order**; sizing via the shared §6
      helpers (`compute_position_params` / `can_open_position`) against the
      **real account balance** — `compound=False` risks off the balance
      captured at `start()` (it becomes the derived config's
      `initial_balance`), `compound=True` off the balance re-read per entry;
      `risk_multiplier` and the `max_*_positions` limits (counted against
      the engine's live positions) all flow through the sim maths untouched.
      Skips (limits / sizing `None`) return `None` silently, sim-style.
    - **Venue-native SL/TP (D1)**: SL always attached at creation; TP only
      when the mode defines one (`signal` → signal's TP, `fixed_rr` →
      computed) — trailing/risk-free/multi-RR send **no** TP.  `multi_rr`
      entries carry the full ladder state internally
      (`tp_level_prices`/`next_tp` on the position) for §17; venue-side they
      get the SL only.
    - **Per-venue mechanics** (picked by `broker.market`): Binance
      **futures** — `set_leverage` on `start()`; SL/TP as quantity-scoped
      reduce-only STOP_MARKET / TAKE_PROFIT_MARKET orders; SL move =
      **place new stop first, cancel old second** (never unprotected).
      Binance **spot** — short signals raise `OrderError` (spec);
      `leverage != 1` rejected at construction; protection = OCO list
      (SL+TP) or single stop order; funds are locked by the protection, so
      a move is cancel→replace with the same TP; on replacement failure the
      old SL is restored, and if that fails too the holding is
      **emergency-sold** (CLOSE event + untracked) — never left naked.
      **MetaTrader** — SL/TP inside the order request (atomic);
      `client_order_id` rides the comment; position ticket resolved by
      comment match from `open_positions` (entry-order-id fallback); moves
      via `modify_position(ticket, sl)` (venue TP preserved); per-ticket
      close.
    - **SL management** (`update_stops(candle)`, per closed candle): mirrors
      the sim engine's steps A→B→C-trailing exactly — trailing peak +
      excursion, `check_risk_free` (gated `risk_free_enabled and tp_mode !=
      multi_rr`, engine-identical), `update_trailing_sl` — on the live
      `_InternalPosition` via the shared §6 functions, then the venue SL is
      modified.  Live `sl_history` equals simulate's by construction
      (§26 test locks it, entry-state record included).  A venue modify
      failure emits `ERROR(will_retry=True)` and **reverts** the internal SL
      (+ pops the `sl_history` record) so the next candle retries.
    - **Force close** (`force_close(reason="force_close")`): market-close
      every tracked position with per-trade quantity (spot cancels the
      protection **before** selling — funds locked; futures closes then
      cancels; MT5 by ticket), builds the §6 `make_closed_trade` record
      (exit price: venue fill → live ticker → entry-price fallback), emits
      `CLOSE` per trade, returns the `ClosedTrade` list; a failed close
      keeps the trade tracked (venue SL still protecting) + `ERROR`.
    - **Naked-position policy**: entry filled but SL attach failed → the
      entry is **emergency-closed at market** + `ERROR` (an unprotected
      position must not stand, D1); TP attach failure keeps the position
      (SL armed) + `ERROR`.
    - **`client_order_id` = `atk-<trader_id>-<PAIR>-<signal_ts>`** (§19/§21
      matching): ms timestamp on Binance, **seconds on MT5** (comment caps
      at ~31 chars); protective orders suffix `-sl`/`-tp`/`-x`;
      `trader_id` ctor param (default 6-hex random).
    - **Events (§12)**: `OPEN` / `SL_MOVE(cause="trailing")` / `RISK_FREE` /
      `CLOSE` / `ERROR`, all `SOURCE_LIVE`; SL-move/risk-free events + the
      `sl_history` records carry the **candle timestamp** (sim parity),
      OPEN/CLOSE/ERROR carry `now_ms()`.  `ExitSignalEvent` stays §16's job.
    - Slippage recorded: `LiveTrade.reference_price` / `.fill_price` /
      `.slippage`; the position is seeded with the **real fill** (Q3 choice
      b) — `$`-per-price-unit stays as sized (physical property of the
      size), risk/margin re-measured off the real fill, so break-even and
      RR thresholds protect the actual entry; TP/SL prices stay
      reference-computed (identical numbers venue-side and internal).
  - Broker plumbing the spec's D1 mechanics require (Q2-confirmed):
    - `broker/_types.py` + `broker/__init__.py`: new **`ORDER_TAKE_PROFIT`**
      order type (`TAKE_PROFIT_MARKET` on futures, `TAKE_PROFIT` on spot).
    - `BinanceBroker.create_oco_order()` / `cancel_order_list()` + the
      `orderList/oco` / `orderList` endpoints — current Binance API
      (above/below legs: LIMIT_MAKER TP + STOP_LOSS[_LIMIT] SL, side-aware
      assignment); spot-only, futures raise `NotSupportedError`.
    - `MetaTraderBroker.close_position(..., ticket=None)` — per-ticket close
      (the ops layer already supported it; the client now exposes it).
- **Deviations from the v100.md spec** (Q&A: 6 questions asked, answered
  "continue with your defaults" — my recommendations became the decisions)
  1. API shape (spec gives none): `ExecutionEngine` + `LiveTrade` as above;
     venue dispatch by `broker.market` (mock-friendly), not isinstance.
  2. Broker-module edits (spec-vs-code conflict, flagged): spot OCO and a
     take-profit order type did not exist; added as connector-level surface
     (PROJECT_STRUCTURE rule 4).  Side effect: `open_orders()` now parses
     venue `TAKE_PROFIT*` order types as `"take_profit"` (previously
     `"stop"`/absent).  The engine places futures protection itself as
     quantity-scoped reduce-only orders — the pre-existing
     `_attach_sl_tp` (`closePosition=true`) path is not used by the trader
     and was left untouched.
  3. Real-fill seeding (option b): sizing and SL/TP prices from the
     reference (MT5 needs SL/TP in the order request), position state from
     the real fill; equals full reference parity whenever fill == reference
     (all §26 mock tests).
  4. Balance sources: futures/MT5 = `get_account_info().wallet_balance`;
     spot = quote-asset **free** balance (longest-suffix detection over a
     known-quotes table + `quote_asset` ctor override; undetectable symbol
     raises naming the override).  Derived config via §11's
     `to_simulate_config`, then `exchange_type` forced to `"metatrader"`
     when `broker.market == "forex"` (the §11 isinstance check can't see
     duck-typed/forex test brokers; real `MetaTraderBroker`s agree anyway).
  5. MT5 client-order-id timestamp in **seconds** (31-char comment cap; ms
     everywhere else).
  6. Emergency-close policy as described (my Q6 recommendation).
  - `tests/test_trader.py::test_execution_module_never_imported` (§13)
    now `monkeypatch.delitem`s `trader._execution` from `sys.modules` first
    — this file's §15 tests import the module at top level, which would
    have tripped the old global assert; the guarantee (run_live itself
    never imports order code) is unchanged and now order-independent.
  - `_InternalPosition` is imported from `simulate._position` (private
    module) — the §6 design contract explicitly places live state on that
    class; same-package-family precedent as §13 reading stepper internals.
- **New files**
  - `src/AlgoTradeKit/trader/_execution.py`
- **Changed files**
  - `src/AlgoTradeKit/broker/_types.py` — `ORDER_TAKE_PROFIT` constant.
  - `src/AlgoTradeKit/broker/__init__.py` — export `ORDER_TAKE_PROFIT`.
  - `src/AlgoTradeKit/broker/exchange/binance/_endpoints.py` —
    `order_list_oco` / `order_list` endpoint properties (spot).
  - `src/AlgoTradeKit/broker/exchange/binance/_client.py` —
    `ORDER_TAKE_PROFIT` param build + reverse type map;
    `create_oco_order()`; `cancel_order_list()`.
  - `src/AlgoTradeKit/broker/metatrader/_client.py` — `close_position`
    gains the `ticket` keyword.
  - `tests/test_trader.py` — §15 section appended (mock futures/spot/MT5
    brokers + 37 tests); header imports + docstring; §13 sys.modules test
    made order-independent.
  - `tests/test_broker.py` — 9 §15 tests (take-profit params spot+futures,
    OCO create/cancel param + side-assignment + futures-raise matrix, MT5
    per-ticket close pass-through); `_FakeMTTransport` gained a
    `close_position` branch.
- **Tests added** (46: 37 in `tests/test_trader.py`, 9 in `tests/test_broker.py`)
  - Setup: futures start (set_leverage, start balance, §3 cost auto-derive
    exactly once, full-override skips the venue); spot leverage/quote rules
    (detection, override, undetectable raises); MT5 forex-maths override;
    lifecycle guards (double start, ops before start, zero balance,
    unsupported market, bad config/trader_id, default trader-id shape).
  - Entries: futures SL-only attach (reduce-only stop, quantity, ids,
    suffixes) + OpenEvent field-exact; TP sent only when the mode defines
    one (signal-with/without-TP, fixed_rr price, multi_rr none + internal
    ladder + entry-state sl_history record); short entry sides; spot OCO vs
    stop-only vs short-raises; MT5 atomic SL/TP + seconds comment + ticket
    match; compound both ways + risk_multiplier; position limits leave the
    venue untouched; unaffordable skip; slippage recording + real-fill
    re-anchoring; entry-failure ERROR; naked-entry emergency close;
    TP-attach failure keeps the protected position.
  - Stops (§26 core): **trailing venue sequence + full `sl_history` ==
    `SimulationStepper`'s** (10 candles, entry record included, final
    SL/peak equal); SlMoveEvent fields + place-before-cancel ordering;
    risk-free jump (event once, venue modify to break-even, no history
    record — sim parity, no re-fire, multi-RR gate) + final SL equals the
    stepper's; modify-failure revert + next-candle retry; MT5
    modify_position path incl. rejected-modify revert; spot cancel→replace
    (OCO with same TP, stop-only) + restore-at-old-SL and
    emergency-sell paths; no-op guards.
  - Force close: futures full flow (reduce-only close, per-trade quantity,
    both protective orders cancelled, hand-checked PnL on the ClosedTrade,
    CLOSE event carries it); spot cancels-before-sell (call order); MT5 by
    ticket; partial failure keeps the failed trade tracked; exit-price
    fallback chain (venue fill → ticker → entry); every §15 event tagged
    `SOURCE_LIVE`.
  - Broker: TAKE_PROFIT_MARKET/TAKE_PROFIT param build + stop_price
    required; OCO SELL/BUY leg assignment, stop-limit leg, listClientOrderId,
    orderListId result; futures OCO/cancel raise before sending;
    cancel_order_list URL/params; MT5 close_position ticket pass-through.
  - Results: full suite `1137 passed, 23 warnings in 25.52s` (1091 baseline
    + 46 new; warnings are the pre-existing environmental set — system
    `websockets` deprecations + the known server-stop thread warning, count
    varies run-to-run).  Ruff on all §15-touched files: `All checks
    passed!`  black remains unavailable in this environment and the tree is
    not black-clean at baseline (§1) — new code follows the 100-char limit
    and surrounding style.
  - Out-of-pytest smoke (scratchpad, not committed): all three venues driven
    end-to-end through the real `EventStream` + `TerminalEventPrinter` —
    futures trailing run with 9 venue stop replacements byte-equal to the
    stepper's `sl_history`, risk-free jump line, MT5 comment/ticket/modify/
    per-ticket close, spot OCO + cancel-before-sell force close, spot short
    raise — every printed line correctly stamped and `[LIVE]`-tagged.
- **Suggested commit messages**
  1. `feat(broker): take-profit order type + spot OCO order lists + per-ticket MT5 close — venue plumbing for the live execution core`
  2. `feat(trader): execution core — ExecutionEngine entries with venue-native SL/TP per venue, shared-maths sizing off the real balance, trailing/risk-free venue SL moves with simulate sl_history parity, force-close, naked-position emergency policies, client-order-id signal tagging`
  3. `test(trader,broker): §15 — venue SL/TP attach per venue, trailing/risk-free sequences equal simulate's sl_history, spot short raises, sizing/limits/slippage, failure + emergency paths — 46 tests`

## Section 16 — trader: live loop & execution modes — 2026-07-13

- **What was implemented**
  - New `trader/_trader.py` — `Trader` (public) + `_PairWorker` (internal):
    the per-pair worker tying feed → strategy → execution.  One consumer
    loop on the calling thread drains a per-pair queue; producers are the
    §14 scheduler thread, a reconcile timer, the mode's optional
    forming/tick stream and the Binance user-data stream — strategy
    evaluation and every order stay **single-threaded per pair** (the §15
    engine contract).  A failing queue item emits `ERROR` and the loop
    continues; backed-up forming/tick/reconcile items are coalesced to the
    newest (candles and user-data fills never dropped).
  - **Execution modes (D2)**: `candle_close` — scheduler tick → append →
    §5 `advance_live_candle` → `engine.update_stops` (sim step order
    A/B/C-trailing) → exit actions → entries.  `candle_update` — adds
    `stream_candles(closed_only=False)`; every forming update runs §5.3
    `evaluate_forming_candle` (throwaway copy; committed state never
    advances) and entries fire mid-candle.  `tick` — adds `stream_ticker`;
    ticks are synthesized into the forming row (first tick of an interval
    opens it, later ticks extend high/low/close, volume 0; interval from
    boundary arithmetic) and evaluated the same way.  **Closed-candle
    commits come ONLY from the scheduler in every mode** — never from a
    stream's `closed` flag (the MT5 stream detects closes by bar-advance =
    exactly the §14 forbidden quirk; a quiet market cannot delay commits).
  - **Signal dedup**: acts once per (pair, candle, direction), keyed by the
    evaluated candle's open time, spanning forming evaluations and the
    closed-candle commit of the same candle; duplicates are silent (no
    event spam).  Exit actions deduped once per candle.  Memory pruned 50
    candles behind the tip.
  - **Venue-side close detection** → §6 `ClosedTrade` with **real fill
    prices** + `CLOSE` events:
    - Binance futures/spot: `stream_user_data` — `ORDER_TRADE_UPDATE` /
      `executionReport` fills matched to tracked protective order ids
      (spot OCO legs identified via `orderListId` + `LIMIT_MAKER` = TP
      leg); exit price = the event's avg/last fill; SL fill →
      `sl_reason(pos)` (`rf` after a risk-free jump — sim parity), TP →
      `tp`; partial-aware (`apply_partial_close`, slice shares the
      trade_id); the surviving futures protective order is cancelled.
    - Futures manual closes: periodic `open_positions` reconcile — venue
      net below tracked sum → FIFO close (partial-aware) with reason
      `manual`, ticker-price fallback, leftover protection cancelled.
    - MetaTrader: periodic positions poll — vanished ticket (or shrunken
      volume = manual partial) resolved via `broker.history_deals(position=
      ticket)`: exit deal's real price + `DEAL_REASON_SL/TP` (4/5) mapped
      to `sl`/`rf`/`tp`, anything else `manual`; ticker fallback when the
      history is unavailable.
    - Spot degraded path (user-data down): vanished protection → close at
      ticker with reason `manual`; venue-outage errors reported once, not
      per poll.
  - New constant `CLOSE_REASON_MANUAL = "manual"` (trader-only — the sim
    never produces it), re-exported with `Trader` from `trader/__init__`.
  - `ExecutionEngine.record_external_close(trade, exit_price, reason, *,
    close_time, closed_size)` on the §15 engine — builds the §6 record
    (partial slices shrink the position in place), emits `CLOSE`, untracks
    on full close; `_close_and_emit` gained the optional
    `close_time`/`closed_size` plumbing (defaults byte-compatible).
  - Broker plumbing (§15 precedent): `MT5Ops.history_deals(from_ms, to_ms,
    position=None)` (stdlib-only; the bridge dispatcher exposes it
    automatically) + `MetaTraderBroker.history_deals()` (ticket or UTC-ms
    range form).
  - Lifecycle: `run()` blocks — seeds the last `min_candles` →
    `strategy.run(BACKTEST)` builds indicator/SMC state (seed signals are
    history: discarded, never traded, no events) → `engine.start()` →
    scheduler anchored at the seed's last open → producers up → consumer
    loop; returns the session's real `ClosedTrade` list (collected via a
    CLOSE-event subscriber, so engine-emitted emergency/force closes are
    never missed).  `stop()` (any thread) or Ctrl+C (KeyboardInterrupt)
    end it; shutdown stops feeds/threads and **keeps positions** — venue
    SL/TP stay armed (D1).  `TraderConfig.candle_poll_interval` /
    `tick_poll_interval` are applied onto same-named broker attrs (MT5)
    before subscribing.  Properties: `events` (subscribe custom
    backends), `engine`, `trader_id`, `open_trades`, `closed_trades`.
  - Scope guards for real money (temporary, removed by their sections):
    `pairs=` → §20, `display=True` → §21 (run_live already offers the live
    display in paper mode), `tp_mode="multi_rr"` → §17 (a position would
    get its venue SL but no ladder).  `on_stop` / `state_path` /
    `kill_switch_file` are accepted + validated via `TraderSettings`,
    stored, inert until §18/§19 (documented).
- **Deviations from the v100.md spec** (Q&A: 12-point plan with marked
  recommendations; user answered "continue by your self" — the ★ defaults
  became the decisions)
  1. API shape (spec gives none): single-pair `Trader` now; `pairs=`
     raises naming §20; `Trader` exported from `trader/__init__` only (the
     spec's top-level re-export names only `run_live`).
  2. §16 stop semantics: `stop()` + KeyboardInterrupt try/except (run_live
     precedent) — SIGINT/SIGTERM handlers, kill-switch file and
     `on_stop="close_all"` stay §18's.
  3. "Guarded by the same boundary arithmetic" implemented as: the §14
     scheduler runs in ALL modes and is the only committer; realtime
     streams only feed forming evaluations.
  4. Dedup key = evaluated candle open time (not the strategy-set signal
     timestamp), so forming and closed evaluations of one candle share it.
  5. Gap-fill catch-up: strategy state and SL management advance through
     every candle, but **entries act only on the tick's newest candle** —
     stale signals print and are skipped (market moved past their
     entry/SL references).  Exception to my own announced default: **exit
     actions run on stale candles too** — the strategy already abandoned
     the position, so flattening late beats holding it (flatten is
     protective, entries are not).
  6. Close reasons: venue SL fill maps through §6 `sl_reason` (sim
     parity: `sl`/`rf`); untraceable closes get the new `manual` reason; the
     futures manual-close reconcile is an explicitly documented FIFO
     heuristic (the venue nets one-way positions); spot manual flows
     (cancel-OCO-then-sell) are §19's job — §16 covers them only when the
     user-data stream is down, via the vanished-protection path.
  7. `record_external_close` (venue-detected closes) added to the §15
     engine file — ClosedTrade construction stays in one place; §17's
     ladder fills will ride the same partial-close machinery.
  8. One spot-short signal raises `OrderError` engine-side (§15 rule); the
     §16 loop catches it, emits `ERROR` and keeps the session alive.
  9. Tick-mode forming rows are tick-synthesized with `volume=0.0`
     (documented; the scheduler's committed candle is authoritative).
- **New files**
  - `src/AlgoTradeKit/trader/_trader.py`
- **Changed files**
  - `src/AlgoTradeKit/broker/metatrader/_ops.py` — `history_deals` op
    (stdlib-only; ticket or datetime-range form).
  - `src/AlgoTradeKit/broker/metatrader/_client.py` —
    `MetaTraderBroker.history_deals()`.
  - `src/AlgoTradeKit/trader/_execution.py` — `_close_and_emit` gained
    `close_time`/`closed_size`; new `record_external_close`; imports.
  - `src/AlgoTradeKit/trader/__init__.py` — export `Trader` +
    `CLOSE_REASON_MANUAL`; docstring Trader entry.
  - `tests/test_trader.py` — §16 test section (8 classes, 57 tests) +
    header imports, exports-set update, module docstring §16 paragraph.
  - `tests/test_broker.py` — `_FakeMTTransport` `history_deals` branch;
    passthrough test; auth-guard assertion.
  - `tests/test_broker_mt5.py` — `history_deals` op test (position kw /
    datetime range / None → RuntimeError).
- **Tests added** (59: 57 in `tests/test_trader.py`, 2 in the broker files)
  - API guards: all-three-required, `pairs=`→§20, `display`→§21,
    `multi_rr`→§17, TraderSettings validation + storage, spot-leverage
    engine guard at construction, properties/ids, run-once,
    `CLOSE_REASON_MANUAL` export.
  - Seeding: exact `min_candles` fetch + anchor, short history raises,
    **seed signals never traded**, scheduler anchored at seed end
    (scripted scheduler), poll-interval knobs applied to the MT5 mock.
  - candle_close loop: signal→SIGNAL/OPEN events + market entry + master
    frame advance; trailing venue-stop replacement per closed candle;
    exit-signal force-close (event `action`, position gone, reason
    `force_close`) and no-flag variant; **gap-fill stale-signal skip**
    (old candle prints, only newest enters); spot short survives the
    session as `ERROR`; empty tick harmless.
  - Dedup: once per (candle, direction) across three forming updates AND
    the same candle's commit (1 order, 1 SIGNAL event); next candle opens
    again; forming never mutates the master frame; stale forming after
    commit ignored; exit action once per candle; producer drops
    stream-closed candles (never commit); `closed_only=False` subscribe.
  - tick mode: OHLC synthesis (open/high/low/close/volume 0), mid-candle
    entry with committed state untouched, committed-interval ticks
    ignored, interval rollover resets the row, ticker subscription.
  - Close detection: futures SL fill via user-data (real `ap` price, event
    close time, CLOSE event carries the record); SL-after-risk-free maps
    to `rf`; TP fill cancels the leftover SL; partial fill shrinks the
    tracked position (protection kept); unrelated/non-FILLED payloads
    ignored; spot OCO TP leg + stop-only SL leg mapping; futures manual
    close reconcile (ticker fallback + leftover cancel) and manual partial;
    MT5 SL/TP/manual deal-reason mapping with the deal's real price +
    `time_msc`, per-ticket `history_deals` call, ticker fallback when
    history fails, manual partial via volume shrink; spot degraded
    reconcile; venue-outage reconcile errors reported once.
  - `record_external_close` unit: full close untracks + emits, partial
    slice shares trade_id + shrinks, untracked trade raises.
  - Lifecycle (threaded e2e over a scripted scheduler): run/stop with
    position kept and feeds stopped; user-data SL fill during `run()` →
    returned `ClosedTrade` list; Ctrl+C via `interrupt_main`; a scripted
    strategy failure emits `ERROR(will_retry)` and the next tick still
    trades; user-data-unavailable degrade message; terminal printer
    end-to-end (`[LIVE][BTCUSDT] SIGNAL/OPEN` lines, no `[SIM]`).
  - Results: full suite `1196 passed, 22 warnings in 27.69s` (1137
    baseline + 59 new; warnings are the pre-existing environmental set —
    system `websockets` deprecations + the known server-stop thread
    warning, count varies run-to-run).  Ruff on all §16-touched files:
    `All checks passed!`  black remains unavailable in this environment
    and the tree is not black-clean at baseline (§1) — new code follows
    the 100-char limit and surrounding style.
  - Out-of-pytest smoke (scratchpad, not committed): a real
    `TerminalEventPrinter` lifecycle over a mock futures venue —
    `set_leverage`, SIGNAL line, market entry with slippage re-anchoring
    (fill 110.05 vs reference 110 → risk re-measured), venue stop
    placement, trailing move as place-new-then-cancel-old + `SL_MOVE`
    line, user-data SL fill at 112.86 → `CLOSE ... reason=sl` with the
    venue's exact fill price, position untracked, `run()` returning the
    record.
- **Suggested commit messages**
  1. `feat(broker): MT5 history_deals op + MetaTraderBroker.history_deals — deal-history plumbing for live close detection`
  2. `feat(trader): Trader live loop & execution modes — candle_close/candle_update/tick with scheduler-only commits, once-per-(candle,direction) signal dedup, venue-side close detection with real fill prices (Binance user-data, futures reconcile, MT5 history deals), ExecutionEngine.record_external_close, §17/§20/§21 scope guards`
  3. `test(trader,broker): §16 — mode loops, dedup across forming and close, per-venue close-detection matrix incl. partial and manual paths, run/stop/Ctrl+C lifecycle, record_external_close — 59 tests`

## Section 17 — trader: multi-RR live ladders (D5) — 2026-07-14

- **What was implemented**
  - `trader/_execution.py` — the §17 ladder inside `ExecutionEngine`
    (methods, no new module; §16 routes into them):
    - **Placement (Binance futures)**: a `multi_rr` entry now places one
      **reduce-only limit order per ladder level** at the level price
      (reference-computed, §15 rule) with the fraction's quantity
      (`fraction × original_size`), client-order-id suffix `-tp1..-tpN`;
      ids tracked in `LiveTrade.ladder_order_ids` (order_id → level index).
      Zero-fraction levels get no order.  `tp_level_close_fractions=None`
      (fraction-less sim mode) places **one full-size limit at the final
      level only** — intermediate levels close nothing in the sim and stay
      feed-detected.  A level whose order fails to place emits `ERROR` and
      **degrades to feed detection** (market execution on touch); the SL
      stays armed, the position stands.
    - **Fill settlement** — `record_ladder_fill(trade, order_id, price, qty,
      close_time)` (driven by §16's user-data routing): advances the ladder
      with the shared §6 `advance_multi_rr`, builds the §6 partial
      `ClosedTrade` slice (`tp_rr`; final full close → `tp`) via
      `make_closed_trade`/`apply_partial_close`, moves the venue SL
      (place-new-cancel-old, quantity-scoped to the remaining size), emits
      `TP_LEVEL` (ladder moves ride it — no standalone `SL_MOVE`, §12 rule)
      or `CLOSE` + cancels leftover SL/ladder orders on full close.
      **Catch-up**: a fill arriving ahead of the ladder cursor first
      advances the skipped levels advance-only (`SL_MOVE cause="ladder"`);
      a late fill of an already-advanced level records its slice without
      re-advancing.  Real fill is authoritative; price falls back to the
      level price, quantity to the planned level size (the exact sim
      fraction).
    - **Feed detection** — `check_tp_levels(high, low, timestamp)`: for
      every touched level *without* a live venue order (MT5 always;
      futures degraded/zero-fraction/fraction-less-intermediate levels),
      executes per the fraction rule: positive size → **partial-close
      market order** (`_MetaTraderVenue.partial_close` = close-by-volume on
      the position ticket; `_FuturesVenue.partial_close` = reduce-only
      market) then settle as above; zero size → advance-only ladder SL move
      (`SL_MOVE cause="ladder"`).  Levels with a live venue order are
      skipped (their fills settle them — no double settlement); a gap
      candle spanning several levels processes them in order, exactly like
      the sim's `check_close`.
    - **Failure policy** (Q&A-approved): post-fill venue SL modify failure →
      internal state stays advanced (the realized close cannot revert),
      `ERROR(will_retry)` + `LiveTrade.pending_sl_sync`; `update_stops`
      retries the modify every closed candle until it lands.  Advance-only
      venue failure → **full revert** (SL, `last_rr_hit`, `next_tp`,
      history record — trailing-move rule): the untouched `next_tp`
      re-detects the level.  Partial-close order failure → `ERROR
      (will_retry)`, nothing advanced, re-detected next update.
    - **Spot**: `tp_mode="multi_rr"` rejected at engine construction (the
      holding is locked by its protective stop; D5 names futures + MT5).
    - `_FuturesVenue.close()` (force close) also cancels remaining ladder
      orders.
  - `trader/_trader.py` — §16 wiring:
    - The §16 `tp_mode="multi_rr"` guard **removed** — the ladder is live.
    - `check_tp_levels` runs on every scheduler candle (after
      `update_stops`, matching the sim's per-candle order) and on every
      forming update / tick-synthesized row.
    - **MT5 detection feed** (Q&A-approved D5 reading): an MT5 multi-RR pair
      in `candle_close` mode subscribes `stream_candles(closed_only=False)`
      used **only** for ladder checks — partial closes fire "the moment a
      level price is reached" (~the §2 poll interval); strategy evaluation
      still happens exclusively on scheduler candles (`_process_forming`
      returns before evaluation in `candle_close` mode).  Futures pairs get
      no extra feed (the venue orders already fire at the moment); closed
      candles remain the authoritative catch-up everywhere.
    - User-data routing: a filled order matching `ladder_order_ids` →
      `record_ladder_fill` (real fill price + venue event time); SL/TP
      fills keep the §16 path, and `_cancel_leftover_protection` now also
      cancels remaining ladder orders (e.g. SL hit at break-even with
      L2/L3 still on the book).
    - `_collect_close` also collects `TP_LEVEL` slices — partial ladder
      closes are real `ClosedTrade` records sharing the trade id and now
      appear in `Trader.run()`'s result / `closed_trades`.
    - Feed-death message adapted for detection-only feeds.
  - Timestamps rule (precedent-consistent): feed-detected ladder actions
    carry the triggering candle's open timestamp (sim parity, §15 trailing
    rule); venue fills carry the venue event time (§16 close-detection
    rule).
- **Deviations from the v100.md spec** (1–4 asked and answered — all
  recommendations confirmed)
  1. **Both fraction modes** supported (spec sentence names
     `tp_level_close_fractions`; D5 says "full support" and D12 wants
     copy-paste config parity): fraction-less = final-level venue order +
     feed-detected intermediate SL advances, exactly the sim semantics.
  2. **Binance spot rejected** with a clear error (out of D5's named scope;
     a spot ladder would need a cancel/replace dance of the whole locked
     protection).
  3. **MT5 `candle_close` pairs get a ladder-detection forming feed** so the
     partial close fires the moment a level is touched (D5's words) instead
     of only at the boundary; the feed never evaluates the strategy.
  4. **Degrade + retry failure policy** (placement failure → feed-detected
     market fallback; post-fill SL-modify failure → keep state + per-candle
     retry) instead of closing healthy protected positions on transient
     venue errors.
  - Announced defaults (stated before coding, not objected): `-tpN` id
    suffixes; only `X == "FILLED"` user-data events processed (§16
    precedent); out-of-order/missed-fill catch-up semantics; the
    timestamps rule above; partial slice → `TpLevelEvent` / advance-only →
    `SlMoveEvent(cause="ladder")` / full close → `CloseEvent` (§12/§13
    mapping); leftover SL + ladder orders cancelled on any full close;
    `closed_trades` collects `TP_LEVEL` slices.
  - Two §15/§16 tests updated as §17 consequences:
    `test_multi_rr_sends_no_tp_but_tracks_ladder` (now also asserts the
    fraction-less final-level order) and `test_multi_rr_lands_in_s17` →
    `test_multi_rr_accepted`.
  - Inherited caveat (documented, not changed): §16's futures manual-close
    reconcile is a FIFO net-exposure heuristic — a ladder fill whose
    user-data event is still in flight could momentarily look like a manual
    reduction (ms-scale window vs the ≥1 s reconcile cadence); §19's
    journal-based reconciliation refines this.
- **New files** — none.
- **Changed files**
  - `src/AlgoTradeKit/trader/_execution.py` — ladder placement / settlement
    / feed detection / failure handling on `ExecutionEngine`;
    `LiveTrade.ladder_order_ids` + `pending_sl_sync`; futures
    `place_ladder_order`/`partial_close` + ladder-aware `close()`; MT5
    `partial_close`; spot multi_rr rejection; `update_stops` pending-SL
    retry; module docstring.
  - `src/AlgoTradeKit/trader/_trader.py` — §16 guard removed;
    `check_tp_levels` wired into scheduler/forming/tick paths; MT5
    detection-feed subscription; user-data ladder routing;
    ladder-aware `_cancel_leftover_protection`; `TP_LEVEL` slices in
    `closed_trades`; docstrings.
  - `src/AlgoTradeKit/trader/__init__.py` — docstring (§17 shipped).
  - `tests/test_trader.py` — §17 section appended (4 classes, 27 tests) +
    the two §15/§16 test updates + module docstring §17 paragraph +
    `CLOSE_REASON_TP_PARTIAL` import.
- **Tests added** (`tests/test_trader.py`, 27 tests)
  - `TestLadderPlacement` (7): per-level reduce-only limits (prices,
    quantities, `-tpN` ids, id→level map); short ladder sides;
    zero-fraction level skipped; MT5 places no orders and no venue TP;
    spot multi_rr raises; placement failure degrades with ERROR and the
    degraded level later closes at market from the feed; rejected-status
    orders degrade too.
  - `TestLadderFillsFutures` (10): first-level fill — §6 slice accounting
    (`tp_rr`, shared trade id, $50 / 1R), ladder advance to break-even,
    quantity-scoped place-new-cancel-old venue SL move, `TP_LEVEL` with no
    standalone `SL_MOVE`; full run to the final `CLOSE` (`tp`, $170 total,
    leftover SL cancelled); fractions summing < 1 leave the remainder open
    (`next_tp=None`, no `CLOSE`); out-of-order L2-before-L1 catch-up
    (advance-only `SL_MOVE`, late slice without re-advance);
    post-fill SL-modify failure → `pending_sl_sync` retried per candle
    until it lands; price/quantity fallbacks; guard errors; force-close
    cancels ladder orders; **sl_history + slice parity with
    `SimulationStepper` — full-precision `asdict` equality of every slice
    over the same candles, both fraction modes** (§26's "multi-RR SL
    modification sequences equal simulate's sl_history").
  - `TestLadderFeedMT5` (5): feed touch → close-by-volume on the ticket +
    `modify_position` to break-even + `TP_LEVEL` (level-price fallback,
    50 % fraction) with no re-settlement on the same candle; a gap candle
    through both levels settles them in order (final full close `tp`);
    fraction-less advance-only then final full market close;
    advance-only venue failure fully reverts and re-detects; partial-close
    venue failure retries.
  - `TestLadderWorkerWiring` (5): detection-feed subscription matrix (MT5
    multi-RR `candle_close` → `closed_only=False` stream; futures multi-RR
    and non-multi-RR MT5 → none); a forming item settles the level
    mid-candle **without strategy evaluation** and the later commit neither
    double-settles nor skips the strategy; scheduler candles run the ladder
    check; user-data ladder fills route to `record_ladder_fill` and the
    slice lands in `Trader.closed_trades`; an SL fill after a level maps to
    `rf` (sim parity) and cancels the remaining ladder orders.
  - Results: full suite `1223 passed, 22 warnings in 30.11s` (1196 baseline
    + 27 new; warnings are the pre-existing environmental set — system
    `websockets` deprecations + the known server-stop thread warning).
    Ruff on all §17-touched files: `All checks passed!` (the whole
    `trader/` package is ruff-clean; the ~60 repo-wide findings live in
    older files untouched by §17, per the §1 baseline note).  black remains
    unavailable in this environment and the tree is not black-clean at
    baseline (§1) — new code follows the 100-char limit and surrounding
    style.
  - Out-of-pytest smoke (scratchpad, not committed): all three flows driven
    through the real `EventStream` + `TerminalEventPrinter` — futures
    ladder (`OPEN` → 3 venue limits → `TP_LEVEL rr=1.00 closed=50% pnl=$50
    new_sl=100` → `TP_LEVEL rr=2.00 closed=30% pnl=$60 new_sl=105` →
    `CLOSE @ 115 reason=tp r=3.00`, old stops + leftover SL cancelled), MT5
    fraction ladder (two close-by-volume orders on ticket 7777, one SL
    modify), MT5 fraction-less (`SL_MOVE (ladder)` then full close) — every
    line correctly stamped and `[LIVE]`-tagged.
- **Suggested commit messages**
  1. `feat(trader): multi-RR live ladders (D5) — venue-native reduce-only level orders on Binance futures with user-data fill settlement, feed-detected market partial closes on MetaTrader incl. a candle_close detection feed, both fraction modes, ladder SL moves via shared §6 maths with post-fill retry and advance-only revert, spot rejection, §16 multi_rr guard removed`
  2. `test(trader): §17 — Binance reduce-only vs MT5 partial-close ladders, sl_history/slice asdict parity with SimulationStepper in both fraction modes, degrade/retry failure paths, out-of-order catch-up, worker wiring — 27 tests`

## Section 18 — trader: safety rails — 2026-07-17

- **What was implemented**
  - **Kill switches** (`trader/_trader.py`, all Trader-level):
    - SIGINT/SIGTERM handlers installed while `run()` blocks on the **main
      thread** (Python restriction; elsewhere the §16 `KeyboardInterrupt`
      fallback still covers Ctrl+C).  Handlers are **one-shot**: the first
      signal prints (`Ctrl+C — stopping. (again = force quit)` — same
      grep-prefix as §16) and requests the graceful stop, restoring the
      previous handlers immediately so a second signal can interrupt a stuck
      shutdown the default way; `run()`'s `finally` restores them again
      (idempotent).
    - `kill_switch_file`: a daemon watcher thread
      (`atk-trader-killswitch`) polls existence every
      `_KILL_SWITCH_POLL_SECONDS = 0.5`; on touch → loud log + `stop()`.
      The file is **never auto-deleted**; a file that already exists when
      `run()` starts raises `ValueError` *before* `_ran` is set and before
      anything starts — remove the stale file and the same instance is
      runnable again.
    - `t.stop()` / Ctrl+C were §16's; all three switches now funnel into the
      same graceful path: stop feeds/scheduler → (state flush is §19's — no
      journal exists yet) → apply `on_stop`.
  - **`on_stop` policy** — `_PairWorker.shutdown(*, close_all=False)`:
    default `"keep"` unchanged (positions stay, venue SL/TP armed, nothing
    cancelled — D1); `"close_all"` → `engine.force_close()` after the feeds
    stop: market-close every tracked position (reason `force_close`) and
    cancel its working orders (SL/TP/ladder — the §15/§17 venue `close()`
    paths).  A close the venue refuses emits `ERROR`, stays tracked and keeps
    its armed SL (the "kept" log then names the remainder).  Closes land in
    `closed_trades` / `run()`'s return via the CLOSE-event collector.
  - **Max daily loss** — new `_DailyLossGuard` in `_trader.py`, wired per
    worker (§20-ready):
    - loss = −(Σ net PnL of the pair's trades closed this UTC day, partial
      slices included + Σ current `unrealised_pnl(mark)` of its open
      positions) — the same `_InternalPosition.unrealised_pnl` the sim equity
      snapshot uses.
    - Marked to market on every scheduler candle and every forming/tick row
      (`evaluate(candle_ts, row_close)`), *after* `update_stops` /
      `check_tp_levels` and *before* exits/entries — so every entry decision
      sees a fresh gate, and `close_on_daily_loss` flattens mid-candle in
      realtime modes.  Day arithmetic uses candle timestamps (venue time).
    - At/over the threshold: one `DailyLossEvent` per UTC day (positive
      `loss`, `limit` in its configured form, `closed_all` flag) + an
      **unconditional** loud log line; `close_on_daily_loss=True` also
      `engine.force_close()`.  While gated: SIGNAL events still print, the
      entry is skipped with a log line; SL management, ladder checks,
      exit-signal closes and `close_all` all keep working.  Next UTC day:
      gate resets.
    - Percent form (`"2%"`): threshold = pct × UTC-day-start balance; first
      day base = `engine.start_balance`, re-read via the new public
      `ExecutionEngine.read_balance()` at each rollover; a failed read keeps
      the previous base and emits `ERROR(where="daily_loss")`.
  - **Paper rehearsal** bullet: documentation only (no `dry_run` exists to
    remove — D8 already shipped that way in §13); the Trader docstring now
    carries the rehearse-with-`run_live`/testnet/demo pointer that
    `_run_live.py` already had.
- **Deviations from the v100.md spec** (4 asked and answered — all
  recommendations confirmed)
  1. Daily-loss measure: overnight positions count their **full** current
     unrealized PnL (no day-start baseline snapshot) — simpler and
     conservative; the rail trips earlier when a loser is carried across
     midnight.
  2. Percent base = venue balance read at UTC-day start (first day: the
     engine's start balance; kept with an `ERROR` on read failure).
  3. Gate is **Trader-only**: `run_live` paper trading leaves
     `max_daily_loss` / `close_on_daily_loss` inert (documented on
     `TraderConfig`); kill switch / `on_stop` were already Trader-level
     (`TraderSettings`).
  4. A stale `kill_switch_file` at `run()` start **raises** (explicit over a
     surprise no-op session); the file is never auto-deleted.
  - Announced defaults (stated before coding, not objected): one-shot signal
    handlers with the §16-compatible `Ctrl+C — stopping` wording; 0.5 s
    kill-file poll on a Trader-level watcher thread; `close_all` runs inside
    `shutdown()` after the feeds stop and therefore also on crash paths, with
    close reason `force_close` (`CLOSE_REASON_FC` — the engine docstring
    already named §18's close_all as a `force_close` caller); `DAILY_LOSS`
    emitted once per UTC day at candle time; gated entries still emit their
    SIGNAL event; realized closes landing between marks are picked up by the
    next mark; unrealized side is gross (sim-equity convention), realized
    side is net.
- **New files** — none.
- **Changed files**
  - `src/AlgoTradeKit/trader/_trader.py` — `_DailyLossGuard`; worker wiring
    (guard construction, scheduler/forming marks, entry gate, `shutdown`
    `close_all` policy); `Trader.run()` stale-file check + handler install/
    restore + watcher lifecycle + `on_stop` pass-through;
    `_install_signal_handlers` / `_restore_signal_handlers` /
    `_start_kill_switch_watcher`; `_KILL_SWITCH_POLL_SECONDS` / `_DAY_MS`;
    module + class docstrings (§18 section).
  - `src/AlgoTradeKit/trader/_execution.py` — public
    `ExecutionEngine.read_balance()` (daily-loss percent base re-read).
  - `src/AlgoTradeKit/trader/_config.py` — docstrings only: `TraderSettings`
    / module notes say §18 is live, `max_daily_loss` documents the
    Trader-only scope and the stale-file rule.
  - `src/AlgoTradeKit/trader/__init__.py` — docstring Trader entry (§18
    shipped).
  - `tests/test_trader.py` — §18 test section appended (4 classes, 25
    tests) + `os`/`signal` imports + module docstring §18 paragraph.
- **Tests added** (`tests/test_trader.py`, 25 tests)
  - `TestDailyLossGate` (13): day-grid assumption; disabled by default;
    realized trip blocks entries while SIGNAL still prints, one event per
    day; unrealized drawdown trips without flattening; wins offset losses;
    percent threshold off the day-start balance; rollover re-reads the venue
    balance (threshold shifts); read failure keeps the old base +
    `ERROR(daily_loss)` and still trips at the old threshold; reset next UTC
    day (entry allowed again, no re-trip); `close_on_daily_loss` flattens
    (reduce-only market close, protection cancelled, `force_close` record,
    loud log); trailing SL keeps replacing the venue stop while gated;
    exit-signal force-close still runs while gated; a forming row trips and
    flattens mid-candle (`candle_update`).
  - `TestKillSwitch` (4): touching the file ends `run()` with the loud log
    and the file left in place; a stale file raises before anything starts
    and the instance runs after removal; the kill switch applies the
    `on_stop="close_all"` policy end-to-end (threaded); no watcher without a
    configured file.
  - `TestSignalHandlers` (3): SIGTERM (real `os.kill`) stops gracefully and
    both handlers are restored; SIGINT path prints the Ctrl+C line; `run()`
    off the main thread installs nothing and still stops cleanly.
  - `TestOnStopPolicy` (5): keep is the default (position + protection
    untouched, "kept" log); close_all flattens and cancels (quantity-scoped
    reduce-only close, SL cancel, `force_close` record, log); close_all
    no-ops without positions; a refused close keeps the position protected
    with `ERROR(force_close)`; `run()` applies close_all on `stop()`
    (threaded e2e).
  - Results: full suite `1248 passed, 23 warnings in 31.73s` (1223 baseline
    + 25 new; warnings are the pre-existing environmental set — system
    `websockets` deprecations + the known server-stop thread warning).  Ruff
    on all §18-touched files: `All checks passed!`  black remains
    unavailable in this environment and the tree is not black-clean at
    baseline (§1) — new code follows the 100-char limit and surrounding
    style.
  - Out-of-pytest smoke (scratchpad, not committed): a real
    `TerminalEventPrinter` session over the mock futures venue — SIGNAL →
    OPEN → `DAILY_LOSS loss=$60 limit=$50 trading halted` + the unconditional
    `MAX DAILY LOSS HIT` line → gated SIGNAL printed with the skip log →
    kill-switch file touched → `close_all` flatten (`CLOSE ...
    reason=force_close`) → `run()` returned the record, kill file left in
    place.
- **Suggested commit messages**
  1. `feat(trader): §18 safety rails — SIGINT/SIGTERM one-shot handlers, kill_switch_file watcher with stale-file guard, on_stop keep/close_all shutdown policy, per-pair max-daily-loss gate (realized+unrealized per UTC day, $ and percent thresholds, DAILY_LOSS event, optional flatten), ExecutionEngine.read_balance()`
  2. `test(trader): §18 — kill switch and on_stop both paths, signal-handler install/restore, daily-loss gate incl. percent base rollover, reset, flatten and gated-session behaviour — 25 tests`

## Section 19 — trader: persistence & restart reconciliation — 2026-07-17

- **What was implemented**
  - New `trader/_state.py` — the §19 module (`_trader.py` imports it; it never
    imports `_trader`, the worker is passed duck-typed):
    - **Serialization**: `serialize_trade` / `restore_trade` — the full
      resumable `LiveTrade` as a JSON-safe dict: every `_InternalPosition`
      field (current SL, `sl_history`, trailing `peak_price`, ladder cursor
      `next_tp`/`last_rr_hit`, excursions, `risk_free_triggered`, sizes,
      entry-derived values), the originating `Signal`, the venue handles
      (entry/SL/TP/OCO/ladder order ids, MT5 ticket), slippage record and
      `pending_sl_sync`.  `build_pair_state` adds the pair's trade-id
      sequence and the §16 dedup keys (`_acted` / `_exit_acted`).
    - **`TraderStateJournal`** — `{"version","trader_id","pairs"}` JSON file:
      atomic writes (same-dir tmp + `os.replace`), flush skipped when the
      serialized content is unchanged, non-JSON metadata values stringified
      (`default=str`) instead of failing the flush; missing file → empty
      journal; unreadable/corrupt/wrong-shape → `ValueError` naming the file.
    - **`reconcile_startup(worker)`** — reads `broker.open_positions()` +
      `broker.open_orders()`, reconciles by `client_order_id`/comment/ticket
      and emits one `RECONCILE` event (§12) + loud logs:
      - *journaled + present* → `engine.adopt_trade()`; trailing/ladder/
        risk-free state resumes from the journal; venue protection lost while
        offline is **re-armed immediately** (D1): vanished SL re-placed at the
        journaled stop via `engine.resync_stop()` (failure arms the §17
        `pending_sl_sync` per-candle retry), vanished plain TP re-attached
        (`rearm_take_profit`), vanished **unfilled** ladder levels re-placed
        (`place_ladder_level`; failure degrades to feed detection, §17 rule);
        MT5 re-arms zeroed SL/TP with one `modify_position` carrying only the
        missing parts.  Ladder levels that **filled** offline are settled from
        history through `record_ladder_fill` (real §17 slices + ladder SL
        moves); an offline manual reduction becomes a partial `manual` slice.
      - *journaled + missing* → closed offline: the real exit fill is fetched
        from history — Binance `my_trades` (fills aggregated per order id:
        qty-weighted price, last fill time; spot OCO legs matched via
        `orderListId`), MT5 `history_deals(position=ticket)` (multiple exit
        deals settle as partial slices in time order) — reasons map exactly
        like live detection (`sl_reason` → `sl`/`rf`, TP → `tp`, spot OCO leg
        by nearest-of(SL, TP) price, untraceable → ticker fallback +
        `manual`); leftover protective/ladder orders are cancelled.
      - *present + not journaled* → foreign: warn + leave untouched (MT5 =
        unknown tickets `ticket N [comment]`; futures = residual net exposure
        after journal-order FIFO coverage — same heuristic family as §16's
        manual-close reconcile; spot holdings not detectable by design).
      - Dedup keys + `trade_seq` restored **before** anything else — a signal
        for a (pair, candle) already journaled is never re-sent (never
        double-open), and restarted sessions never reuse trade ids.
      - A venue read failure **aborts the start** (`BrokerError` propagates —
        no producer thread exists yet); the event is emitted only when there
        was a journal to reconcile or something foreign to report (a clean
        first start prints nothing).
  - `_trader.py` wiring: worker `attach_journal()` + `_flush_state()`
    (write-if-changed; `OSError` → one `ERROR(journal, will_retry)` and
    trading continues — the venue SL protects regardless, D1) flushed after
    **every processed queue item**, after startup reconciliation and at the
    end of `shutdown()`; `_PairWorker.start()` runs `reconcile_startup`
    between `engine.start()` and the first producer thread;
    `Trader._prepare_journal()` (called by `run()` before `_ran` is set, after
    the stale-kill-file check) loads `state_path` (`None` →
    `./.atk_trader_state.json` via `DEFAULT_STATE_PATH`), **reuses the
    journaled `trader_id`** when none was passed explicitly, and refuses a
    corrupt journal exactly like a stale kill file (instance stays runnable
    after the fix).  The shared constants (`CLOSE_REASON_MANUAL`,
    `QTY_EPSILON`, MT5 deal-reason codes) moved to `_state.py` — `_trader.py`
    re-imports them, the `trader/__init__.py` re-export is unchanged.
  - `_execution.py` — §19 engine surface: `adopt_trade()`,
    `ensure_trade_seq()`, public `trade_seq` property, `resync_stop()`
    (push the internal SL as fresh venue protection; handles the spot
    emergency-close path), `rearm_take_profit()` +
    `_FuturesVenue.place_take_profit()`, and `_place_ladder` refactored onto a
    public per-level `place_ladder_level()` (behaviour unchanged — same loop,
    same degrade-on-failure ERROR).
  - Broker plumbing (Q&A choice 1): `BinanceBroker.my_trades(symbol, *,
    start_ms, end_ms, from_id, limit)` — signed GET of the account fill
    history (futures `/fapi/v1/userTrades`, spot `/api/v3/myTrades`, new
    `Endpoints.my_trades`), raw venue dicts (§21's real-fills backfill will
    reuse it).
- **Deviations from the v100.md spec** (1–4 asked and answered — all four
  recommendations confirmed)
  1. "Fetch fill from history" on Binance required new venue plumbing —
     `my_trades()` added now (spec-true) instead of a ticker-only fallback
     until §21.
  2. Adopted position whose venue SL vanished offline → **re-armed
     immediately** during reconcile (D1), not warn-only; failure falls into
     the §17 `pending_sl_sync` per-candle retry.
  3. Vanished **unfilled** ladder orders → **re-placed** (ladder continues
     where it left off); a re-place failure degrades that level to feed
     detection (§17 policy).
  4. `trader_id` is journaled and **reused on restart** when the user did not
     pass one — client-order-id session identity stays uniform; an explicit
     id always wins.
  - Announced defaults (stated before coding, not objected): journal scope =
    full `LiveTrade` superset of the spec's named fields **plus** the dedup
    keys and `trade_seq` (closes the realtime-mode double-open gap the spec's
    "never double-open" implies); flush cadence = snapshot after every
    processed queue item + reconcile + shutdown (crash window = one item,
    documented); per-venue presence rules (MT5 ticket; futures FIFO net
    coverage; spot = protection still working — a protection the user
    cancelled while keeping the holding is recorded `manual`, §16's rule, so
    the Q&A "spot re-arm" case is unreachable by construction); foreign
    labels per venue; journal file kept after all positions close (retains
    `trade_seq`/dedup memory); `run_live` journals nothing (paper mode);
    reconcile venue-read failure aborts the start; `RECONCILE` emitted only
    when a journal existed or something was found (clean first start silent).
  - Implementation notes: in the offline-ladder-then-closed case the L1
    settlement transiently re-places a venue SL for a position that is
    already gone — it is cancelled again by the same reconcile pass
    (harmless: reduce-only; correct reasons/slices are worth it).  Restored
    signal metadata containing non-JSON values comes back stringified
    (documented).  `_state` reads `engine._ladder_plan` (private, same
    package — §13/§15 precedent).
  - Test-infrastructure addition: an autouse fixture in `tests/test_trader.py`
    patches `DEFAULT_STATE_PATH` into each test's `tmp_path` — §16/§18
    `run()`-based tests must never share/litter `./.atk_trader_state.json`
    (they now journal by default, by design).
- **New files**
  - `src/AlgoTradeKit/trader/_state.py`
- **Changed files**
  - `src/AlgoTradeKit/trader/_trader.py` — journal attrs + `attach_journal` /
    `_flush_state`; flush points (run_loop item, shutdown); reconcile inside
    `start()`; `Trader._prepare_journal()` + corrupt-journal refusal +
    trader_id reuse; shared constants re-imported from `_state`; module/class
    docstrings (§19 section).
  - `src/AlgoTradeKit/trader/_execution.py` — `adopt_trade`,
    `ensure_trade_seq`, `trade_seq` property, `resync_stop`,
    `rearm_take_profit`, `place_ladder_level` (+ `_FuturesVenue.
    place_take_profit`); docstring division-of-labour update.
  - `src/AlgoTradeKit/trader/_config.py` — docstrings only (§19 live;
    `state_path` documented in full).
  - `src/AlgoTradeKit/trader/__init__.py` — docstring Trader entry (§19
    shipped).
  - `src/AlgoTradeKit/broker/exchange/binance/_endpoints.py` — `my_trades`
    endpoint property.
  - `src/AlgoTradeKit/broker/exchange/binance/_client.py` — `my_trades()`.
  - `tests/test_trader.py` — §19 section (5 classes, 42 tests) + autouse
    default-path isolation fixture + `json` import + module docstring §19
    paragraph; ruff import-sort fix.
  - `tests/test_broker.py` — 3 `my_trades` tests appended.
- **Tests added** (45: 42 in `tests/test_trader.py`, 3 in `tests/test_broker.py`)
  - `TestStateSerialization` (5): trailing-trade full round-trip (every pos
    slot + LiveTrade field + signal `asdict`), post-partial ladder round-trip
    (shrunk size, cursor, ladder ids, `pending_sl_sync`), risk-free flag →
    restored `sl_reason` = `rf`, diverged entry-state fields survive,
    non-JSON metadata stringified through a real disk round-trip.
  - `TestStateJournal` (6): atomic write + exact file shape, unchanged write
    skipped (`os.replace` counted), missing file empty, corrupt + wrong-shape
    raise naming the path, trader_id persists, pair state disk round-trip.
  - `TestJournalFlushWiring` (5): open journaled (trade payload, acted key,
    seq), SL move updates journal (`sl_history` grows), close clears trades
    but keeps seq + dedup, `shutdown()` flushes by itself, write failure →
    one `ERROR(journal)` then recovery resets the once-flag.
  - `TestReconcileAdopted` (11): adopt resumes trailing (state equality +
    next-candle venue stop replacement), dedup + trade_seq restored (replayed
    signal → no order, no event), SL re-arm (stop at journaled price, stale
    id never cancelled) + failure arms `pending_sl_sync`, TP re-arm, vanished
    ladder re-placed (both levels, reduce-only, correct prices), offline
    ladder fill settled (slice, BE SL, `TP_LEVEL` event, collector), offline
    manual reduction partial slice, trader_id reuse vs explicit override,
    default-path resolution, MT5 adopt (manual partial from deals + one
    modify re-arming both SL and TP).
  - `TestReconcileClosedOffline` (9): futures SL fill qty-weighted from two
    history rows (+ leftover TP cancel, seq restored), post-risk-free SL fill
    → `rf`, TP fill → leftover SL cancel, offline ladder run to full close
    (`tp_rr` + `tp` slices sharing the id), untraceable → ticker `manual`,
    history-read failure degrades with `ERROR`, MT5 multi-deal slices
    (manual partial + SL final, real prices/`time_msc`, per-ticket call),
    spot OCO-leg fill mapped by nearest price to `tp` and to `sl`.
  - `TestReconcileForeignAndLifecycle` (6): futures foreign net warned +
    untouched, MT5 foreign ticket label, clean first start emits no event,
    venue read failure aborts, corrupt journal refused before anything starts
    (`_ran` stays False, nothing fetched), threaded restart e2e — session 1
    opens + journal flushed mid-run → stop keeps the position → session 2
    adopts (journal trader_id reused), the replayed scheduler candle's signal
    is **not** re-sent, the venue SL fill closes the adopted trade with the
    real price and drains the journal.
  - Broker (3): futures `userTrades` URL + full param map, spot `myTrades`
    URL + minimal params, unauthenticated raises.
  - Results: full suite `1293 passed, 23 warnings in 35.22s` (1248 baseline +
    45 new; warnings are the pre-existing environmental set — system
    `websockets` deprecations + the known server-stop thread warning, count
    varies 20–23 run-to-run).  Ruff on all §19-touched files: `All checks
    passed!`  black remains unavailable in this environment and the tree is
    not black-clean at baseline (§1) — new code follows the 100-char limit
    and surrounding style.
  - Out-of-pytest smokes (scratchpad, not committed): (a) engine-level
    round-trip + journal atomicity/write-skip + adopt with trailing resuming
    on the very next candle; (b) all three reconciliation flows through the
    real `TerminalEventPrinter` — `RECONCILE adopted=[…]`, an offline `CLOSE
    #0 @ 105 reason=sl r=-1.00 duration=2h` stamped with the **historical**
    fill time, and the foreign-position warn line.
- **Suggested commit messages**
  1. `feat(broker): my_trades account fill history — futures userTrades / spot myTrades signed endpoints, raw venue rows for offline-close reconciliation`
  2. `feat(trader): §19 persistence & restart reconciliation — atomic write-if-changed state journal (full LiveTrade state, trade-id sequence, signal-dedup keys, trader_id) flushed on every change; startup reconcile adopts journaled positions with immediate SL/TP/ladder re-arm, settles offline ladder fills and closes from venue history with sl/rf/tp/manual mapping, warns foreign positions, never double-opens; RECONCILE event + corrupt-journal refusal`
  3. `test(trader,broker): §19 — serialization round-trip, journal atomicity/flush wiring, all three reconciliation cases per venue incl. offline ladder/rf fills and FIFO net coverage, trader_id reuse, restart e2e with never-double-open — 45 tests`

## Section 20 — trader: multi-pair orchestration + combined report — 2026-07-18

- **What was implemented**
  - **Multi-pair `Trader` (D9)** — `Trader(pairs=[TraderPair, ...])` in
    `trader/_trader.py`: one `_PairWorker` per `(broker, config, strategy)`
    entry; brokers may repeat — pairs on one broker share that account's
    wallet naturally (each engine sizes off the same venue balance, §15);
    duplicate `(broker, symbol)` rejected via §11's `validate_pairs`.
    Mutually exclusive with the single form (single-pair `Trader` behaviour
    byte-preserved: same loop on the calling thread, same messages, same
    return).  Per pair: own `EventStream` + terminal printer (per-pair
    `log_event_types` filters never leak), own §18 daily-loss gate, own
    scheduler/feeds/user-data/reconcile producers.  Multi-pair `run()`
    spawns one `atk-trader-pair-<symbol>` consumer thread per pair (§15
    single-threaded-per-pair contract holds per worker) while the calling
    thread supervises and handles signals; every kill switch (`stop()`,
    Ctrl+C/SIGTERM, `kill_switch_file`) and the `on_stop` policy apply to
    **all** pairs; startup is seed-all-first (fail fast) then start-each —
    a mid-start failure (short history, §19 venue read error) shuts already-
    started pairs down again with positions kept protected (D1) and
    propagates.  `run()` returns the flat `ClosedTrade` list of all pairs
    sorted by `close_time` (single form: unchanged detection order).
  - **Shared identity & journal**: one session `trader_id` across all pairs
    (explicit > journaled > random) — client_order_ids stay uniform and the
    §19 journal's single `trader_id` slot stays truthful; one journal file
    serves every pair under its own key.  Same-`(market, symbol)` pairs on
    different brokers (legal per D9) get occurrence-suffixed keys
    (`futures:BTCUSDT`, `futures:BTCUSDT#2`, by pair order);
    `TraderStateJournal` gained an internal `threading.Lock`
    (`write_pair` is now thread-safe — several consumer threads flush one
    file).
  - **Introspection**: new `Trader.engines` tuple (pair order); `engine`
    raises in the multi form naming `engines`; `open_trades` /
    `closed_trades` aggregate across pairs (`closed_trades` close_time-
    sorted in multi form); `events` returns an **aggregate** stream in the
    multi form — every pair's stream forwards into it, one subscribe point
    for custom/D14 backends; internal `_worker` kept as a property for the
    single-pair form.
  - **`run_live` combined report (D9/§13)** — `trader/_run_live.py`:
    `_CombinedLiveReport`, a deliberately **source-agnostic** manager
    (`label → SimulateReport` snapshots in; §9
    `build_combined_report_payload` + `ReportServer` + `push_update` out)
    so §21's Trader display bridge can drive the same class with its
    sim/real reports.  Gate: pairs form, ≥ 2 entries, ≥ 1 pair with
    `display=True` → one combined page (auto port; host and
    open-browser-vs-print-URL follow the **first** displaying pair; URL
    printed as `combined report → …` in the no-browser flow).  Labels =
    symbols in pair order, `#2`-suffixed when a symbol repeats across
    brokers (`_combined_labels`).  Initial payload built from the seed
    snapshots after all seeds, armed **before** the feeds start; every
    closed candle of any pair routes its fresh snapshot through the
    session's `on_report` (bridge first, combined second) and re-pushes the
    rebuilt payload (thread-safe — snapshots arrive on per-pair feed
    threads); a `display=False` pair still contributes to the breakdown.
    Return value unchanged (per-pair reports).
- **Deviations from the v100.md spec** (4 questions asked and answered —
  all recommendations confirmed)
  1. **Trader-side combined report deferred to §21** (spec-order conflict
    flagged: §20's "the Trader builds one combined report" needs per-pair
    Trader reports and the `get_account_info()` equity snapshots that are
    §21 spec text; `display=True` still raises until §21).  §20 ships the
    Trader multi-pair orchestration + the combined report live in
    `run_live`, with the manager built source-agnostic so §21 plugs the
    Trader display into it — no later-section code was written.
  2. Multi-pair API shape: flat close_time-sorted `run()` result, aggregate
    `open_trades`/`closed_trades`, aggregate `events` stream, `engines`
    tuple + `engine` guard (chosen over per-pair lists everywhere).
  3. Combined gate: ≥ 2 pairs + any displaying pair; host/browser behaviour
    from the first displaying pair; labels = `#N`-deduped symbols.
  4. Journal keys: occurrence suffix + journal lock (chosen over rejecting
    same-`(market, symbol)` across brokers, which would restrict D9).
    Documented caveat: keep the relative order of such duplicate pairs
    stable across restarts, or their journals swap.
  - Announced defaults (stated before coding, not objected): single-pair
    `run()` unchanged / multi-pair one consumer thread per pair;
    seed-all-then-start-each with keep-positions cleanup on a failed start;
    one shared `trader_id`; per-worker user-data streams and reconcile
    polls on a shared broker (order-id matching filters cross-pair events;
    Binance reuses the listenKey), MT5 poll-interval knobs last-pair-wins
    (documented); combined manager lives in `_run_live.py`; `run_live`
    return unchanged; Trader `display=True` still rejected (any pair)
    until §21.
- **New files** — none.
- **Changed files**
  - `src/AlgoTradeKit/trader/_trader.py` — multi-pair ctor (pairs XOR
    single, per-pair streams/engines/workers, shared session id, journal-key
    suffixing, aggregate stream), `_worker` property, `engines`/`engine`/
    `events`/`open_trades`/`closed_trades` property semantics, multi-aware
    `run()`/`stop()`/`_prepare_journal()`/kill-switch watcher (trader-level
    stop event), `_describe_pairs()`, startup-failure cleanup; module +
    class docstrings (§20 section).
  - `src/AlgoTradeKit/trader/_run_live.py` — `_CombinedLiveReport`,
    `_combined_labels`, `_maybe_start_combined`; `_PaperSession` gained the
    `on_report` dispatcher (bridge + combined) and the combined fields;
    docstrings (module, `run_live`).
  - `src/AlgoTradeKit/trader/_state.py` — `TraderStateJournal` internal
    lock; thread-safety notes.
  - `src/AlgoTradeKit/trader/__init__.py` — docstring: run_live combined
    report + Trader multi-pair (§20 shipped).
  - `tests/test_trader.py` — §20 test section appended (4 classes, 21
    tests); §16 `test_pairs_form_lands_in_s20` → `test_pairs_form_accepted`
    (guard gone, §17 precedent); the §16 scripted-scheduler holder also
    records all instances (additive); module docstring §20 paragraph.
- **Tests added** (`tests/test_trader.py`, 21 tests)
  - `TestTraderMultiPairApi` (9): pairs XOR single form; duplicate
    `(broker, symbol)` rejected; `display=True` on any pair raises naming
    §21 + the symbol; `engines` tuple in pair order + `engine` guard
    (single form keeps both spellings); shared explicit/random `trader_id`
    across engines; aggregate `events` stream receives every pair's events
    while per-pair streams stay isolated; journal-key `#2` suffixing for
    same-`(market, symbol)` on two brokers; run-once rule in pairs form.
  - `TestTraderMultiPairLoop` (4, deterministic single-threaded drives):
    **shared broker** — both pairs enter through the one account (same
    `start_balance`, $100 risk → 20 units each, per-symbol market orders +
    leverage calls, shared client-id prefix); daily-loss gate is per pair
    (A trips $50 and is blocked, B keeps trading the same candles); one
    journal file with both pair keys and one `trader_id`;
    `write_pair` thread-safety hammer (2 × 200 interleaved flushes → valid
    file, both keys, last states).
  - `TestTraderMultiPairLifecycle` (4, threaded e2e over the scripted
    scheduler): `run()` returns the flat close_time-sorted list across
    pairs (ETHUSDT's earlier SL fill sorts first) with one user-data
    stream per pair, all stop events set and every feed stopped; the
    kill-switch file ends every pair; `on_stop="close_all"` flattens both
    pairs' positions (`force_close` records per symbol); a §19 venue read
    failure on pair B aborts the start and shuts already-started pair A
    down again.
  - `TestRunLiveCombined` (4): `_combined_labels` dedup
    (`BTCUSDT`/`BTCUSDT#2`/`ETHUSDT`); recording-fake manager — combined
    started once with pair-ordered labels and seed snapshots
    (`total_trades == 0`), host/browser from the first displaying pair,
    one update per closed candle per pair with the final BTCUSDT snapshot
    carrying its TP trade, URL printed; gate negatives — no displaying
    pair and single-pair form construct nothing; **real-server** e2e —
    the combined `ReportServer` replay payload after the run is the
    **pushed** state (`combined: True`, pair rows `BTCUSDT`/`ETHUSDT`,
    `summary.total_trades == 1`, `initial_balance == 20 000`,
    `has_chart is False`) and its real URL was printed.
  - Results: full suite `1314 passed, 24 warnings in 36.99s` (1293
    baseline + 21 new; warnings are the pre-existing environmental set —
    system `websockets` deprecations + the known server-stop thread
    warning, whose count scales with how many real servers a run
    starts/stops).  Ruff on all §20-touched files: `All checks passed!`
    black remains unavailable in this environment and the tree is not
    black-clean at baseline (§1) — new code follows the 100-char limit and
    surrounding style.
  - Out-of-pytest smoke (scratchpad, not committed): (a) a two-pair Trader
    on one shared futures account through the real `TerminalEventPrinter` —
    per-pair `[LIVE][BTCUSDT]`/`[LIVE][ETHUSDT]` SIGNAL/OPEN/CLOSE lines,
    both entries sized $100 off the shared 10 000 wallet, shared
    `atk-smk20-…` client ids, flat close_time-sorted `closed_trades`, the
    aggregate stream carrying both symbols, one journal file with both
    keys; (b) `run_live` with two pairs on real servers — per-pair chart +
    report URLs plus `combined report → http://127.0.0.1:…` printed,
    `[SIM]` lifecycle to the TP close, combined replay payload aggregated
    (`initial=20 000`, 1 trade, both pair rows).
- **Suggested commit messages**
  1. `feat(trader): §20 multi-pair orchestration + combined report — Trader(pairs=[...]) with one worker thread per pair, shared-broker wallets, one shared trader_id and journal (per-pair keys, #2-suffixed duplicates, thread-safe flushes), aggregate events stream, all-pair kill switches and on_stop, flat close_time-sorted results, startup-failure cleanup; run_live serves the §9 combined report across all pairs, re-pushed every closed candle`
  2. `test(trader): §20 — multi-pair with shared broker (wallet sizing, per-pair daily-loss/journal/event isolation, threaded lifecycle, kill switch, close_all, abort cleanup), combined report gating/labels/feeding + real-server payload, journal thread-safety — 21 tests`

## Section 21 — trader: display bridge + real-fills pipeline — 2026-07-18

- **What was implemented**
  - New `trader/_display.py` — `_DisplayBridge`, the per-pair §21 display
    (internal; nothing exported from `trader/__init__`, per the §11 export
    list).  Built by `Trader.__init__` for every `display=True` pair
    (`worker.display`); the §16/§20 `display=True` guards are **removed**.
  - **`display_trades` modes (D10)**:
    - `"sim"` — one §10 `LiveSimulation` per displaying pair on the
      auto-derived `engine.sim_config` (real venue costs, real start
      balance) and a **deep copy** of the pair's strategy taken pristine at
      Trader construction (worker and sim each advance their own instance —
      sharing one would double-advance state).  Driven externally through
      `process_closed_candle` / `process_forming_candle` (§10 built these
      for §21); `sim.start()` never called.  The sim owns its chart +
      report servers, exactly the run_live display.
    - `"real"` — no LiveSimulation: a chart built directly (seed candles
      per `display_candles`/`display_start`, ≥ `min_candles` enforced),
      real trades drawn through the §8/§10 pipeline (open → `LivePosition`
      drawing; SL_MOVE/RISK_FREE/TP_LEVEL → live-line updates from the
      engine's position state; close → live drawing swapped for the final
      posbox + dynamic SL/TP segments via `draw_trade_group`), and a report
      page of `simulate.build_report` over the real `ClosedTrade` records +
      the venue equity curve, pushed via `ReportServer.push_update` (chart
      link carried, "Open on Candle Chart" navigates the real chart).
    - `"both"` — real fills overlaid on the sim's chart (overlay posboxes
      at opacity 0.30 vs sim 0.15) and ONE report page carrying both
      sections: a §9 two-entry combined payload
      `{"SYMBOL (sim)": …, "SYMBOL (real)": …}`; the sim's own report
      server is suppressed (`report_mode="none"` on the sim's config copy;
      `engine.sim_config` untouched); the combined page has
      `has_chart=False` per §9's combined contract.
  - **Real-fills pipeline**: no new venue plumbing — the spec paragraph
    (user-data stream + `userTrades` backfill, MT5 positions + history
    deals, `client_order_id`/comment matching) is the §16/§19 machinery
    that already produces `worker.closed_trades` / `engine.open_trades`
    with real fill prices; §21 consumes exactly those.  NEW: the equity
    curve — one `{"timestamp","wallet","equity"}` snapshot per closed
    candle from `broker.get_account_info()` (plain spot, whose
    `AccountInfo` scalars are 0.0 by design, falls back to
    `engine.read_balance()`; a failed venue read carries the previous
    values forward and emits one `ERROR(where="display")` until recovery).
    The display keeps its **own** trade-list copy — `worker.closed_trades`
    (the `run()` return value) is never trimmed.
  - **Zero impact on trading speed**: one low-priority daemon display
    thread + queue per pair.  Producers only enqueue: the worker hands over
    each committed candle after processing it and forwards forming rows
    from its own forming/tick handlers; real-trade drawing updates ride an
    event-stream subscriber (enqueue-only); `candle_close` pairs with no
    forming feed of their own get a display-only
    `stream_candles(closed_only=False)` subscription feeding ONLY the
    display queue (§16 hybrid-display note; closed candles from it are
    dropped — commits stay scheduler-only; MT5 multi-RR pairs reuse §17's
    ladder feed instead).  The display thread processes an item only when
    the pair's trading queue is empty (poll before every item — trading
    always preempts).  Backlog coalescing: closed candles are never
    dropped (the sim steps every candle in order) but their report/combined
    pushes are suppressed until the newest of the burst; consecutive
    forming rows skip to the latest; venue equity reads happen only when
    the queue is idle (carry-forward under backlog).
  - **Browser vs URL**: `display_open_browser=False` prints the chart +
    report URLs (`[AlgoTradeKit] trader SYMBOL chart/report → …`); a
    non-local `chart_host` adds a SECURITY note naming the bind and
    recommending an SSH tunnel.  Real-mode charts register the same §10
    keep-alive as the sim display.
  - **Rolling window (D6)**: charts trim via §8's
    `Chart(candle_count_limit=M)`; the sim report via §10; the real report
    mirrors §10's exact rules here — balance snapshots older than the
    window drop (baseline := equity entering the window) and a real trade
    drops once `close_time` left the window (window prefilled from the
    seed candles, so §19 offline closes older than the window never show).
  - **Trader-side combined report** (the §20-deferred piece):
    `Trader._maybe_start_combined()` — multi-pair form with **two or more
    displaying pairs** serves one §20 `_CombinedLiveReport` page (labels =
    `_combined_labels` over the displaying pairs; host/browser behaviour
    from the first displaying pair; URL printed in the no-browser flow).
    Each pair contributes its display's primary report — `"sim"` pairs the
    sim report, `"real"`/`"both"` pairs the real one — refreshed through
    the bridges on every closed candle.
  - `LiveSimulation.process_closed_candle(candle, *, quiet=False)` — new
    backward-compatible kwarg (§13 precedent for a §10-file addition):
    `quiet=True` suppresses only the per-candle report-server push (the
    §21 backlog coalescing); state, chart streaming, events and
    `on_report` unaffected.
  - `Trader.shutdown` path: `display.close()` after the `on_stop` policy —
    stops the display feed/thread, drains the leftovers inline (so
    `close_all` closes reach the page) and pushes the final report state;
    servers stay up via keep-alive.
- **Deviations from the v100.md spec** (Q&A: 4 questions, all four
  recommendations confirmed)
  1. Per-mode architecture as above — notably `"real"` has no
     LiveSimulation, and `"both"`'s "report carries both sections" is the
     §9 combined renderer with `(sim)`/`(real)` entries on one server.
  2. Trader combined page = **displaying pairs only** (option a);
     consequence: the gate is ≥ 2 *displaying* pairs (a one-contributor
     combined page would equal that pair's own report page — run_live's
     ≥ 2-pairs + ≥ 1-displaying gate stays as shipped in §20, where every
     paper pair has a sim to contribute).  Armed after all workers started
     — a candle landing in that gap skips one combined push; the next
     candle carries the current state.
  3. Real-fills pipeline reuses §16/§19 records — the §21 spec paragraph
     describes machinery those sections already landed; re-deriving fills
     venue-side in the display would duplicate detection.
  4. Priority semantics: closed candles never dropped (stepper correctness)
     with pushes coalesced to the newest; forming skip-to-latest; equity
     venue reads idle-only.
  - Announced defaults (stated before coding, not objected): display seeds
    inside `worker.start()` after engine + §19 reconcile and before any
    producer (real view seeds from the settled state — no reconcile-event
    catching); the trader's sim display emits **no** terminal events
    (`on_event=None` — the `[LIVE]` stream is the trader's voice, a
    parallel `[SIM]` log would be noise); run_live's §13 URL printer
    untouched (the Trader's printer lives in `_display.py` and adds the
    security note); `_DisplayBridge` stays internal.
- **New files**
  - `src/AlgoTradeKit/trader/_display.py`
- **Changed files**
  - `src/AlgoTradeKit/trader/_trader.py` — §16/§20 display guards removed;
    bridge construction with pristine strategy deepcopy; `worker.display`
    attr + start/enqueue/shutdown hooks (scheduler candles, forming rows,
    `display.close()`); `Trader._maybe_start_combined()` + `_combined`
    attr; module/class docstrings (§21 section).
  - `src/AlgoTradeKit/simulate/_live.py` —
    `process_closed_candle(quiet=...)` kwarg (report push suppression
    only); docstring.
  - `src/AlgoTradeKit/trader/__init__.py` — docstring (§21 shipped).
  - `src/AlgoTradeKit/trader/_execution.py` — one docstring line
    (`force_close` → §21 consumes the records via CLOSE events).
  - `tests/test_trader.py` — §21 section appended (8 classes, 26 tests;
    fake Chart/ReportServer + recorded `draw_trade_group`, `d21` fixture,
    deterministic thread-parked bridge driver); the two §16/§20 guard
    tests rewritten as acceptance tests (`test_display_accepted`,
    `test_display_pairs_accepted`).
- **Tests added** (`tests/test_trader.py`, 26 tests)
  - `TestDisplayWiring` (4): worker hands candles + forming rows to the
    display queue; display-only forming feed on `candle_close`
    (`closed_only=False`, closed candles dropped); no extra feed in
    realtime modes; no extra feed for MT5 multi-RR pairs (§17 feed reused).
  - `TestDisplaySim` (2): sim display on `engine.sim_config` (identity,
    real start balance, `show_chart`) advancing the deep-copied strategy
    independently of the trader frame, live drawing + streamed bar;
    `quiet` backlog suppresses the sim report push, idle candle pushes.
  - `TestDisplayReal` (7): OPEN event → `LivePosition` with entry/SL/id;
    SL_MOVE updates the live line from engine state; close → drawing swap
    + `draw_trade_group` + real-report push carrying the venue exit price;
    equity snapshot per candle (values + report `final_balance`); venue
    read failure carries forward with exactly one `ERROR(display)`; spot
    fallback to `read_balance`; initial payload links the chart
    (`has_chart`/`chart_port`) and `on_open_chart` navigates.
  - `TestDisplayBoth` (2): one shared chart, sim report server suppressed
    (`report_mode` forced `none` on the sim copy only), combined two-entry
    payload with `(sim)`/`(real)` labels and `has_chart=False`; real
    overlay at opacity 0.30 and the real row's `total_trades` on the
    pushed combined payload.
  - `TestDisplayUrlMode` (3): URLs printed with browser off; SECURITY +
    SSH-tunnel note on `0.0.0.0` (absent on localhost); browser mode
    prints nothing and opens tabs.
  - `TestDisplayWindowTrim` (2): balance history bounded with baseline =
    the equity entering the window (report `initial_balance` follows);
    an old real trade trimmed at display seed while `worker.closed_trades`
    (the `run()` record) keeps it.
  - `TestDisplayPriority` (3, §26's starvation test): with pending trading
    queue items the running display thread processes nothing until the
    trading queue drains (then catches up); a 4-candle backlog performs
    exactly one venue equity read and one report push while snapshotting
    every candle (carry-forward proven); consecutive forming rows collapse
    to the newest.
  - `TestTraderCombinedDisplay` (3): combined armed with pair-ordered
    labels + seed reports, browser flag from the first displaying pair,
    URL printed, per-candle `update` routed with the pair's label; sim
    pair contributes `sim.last_report` (identity) while the real pair
    contributes the real report; gate negatives — one displaying pair of
    two, and the single-pair form, build nothing.
  - Results: full suite `1340 passed, 23 warnings in 46.86s` (1314
    baseline + 26 new; warnings are the pre-existing environmental set —
    system `websockets` deprecations + the known server-stop thread
    warning, count varies run-to-run).  Ruff on all §21-touched files
    (whole `trader/` package + `simulate/_live.py` + `tests/test_trader.py`):
    `All checks passed!`  black remains unavailable in this environment
    and the tree is not black-clean at baseline (§1) — new code follows
    the 100-char limit and surrounding style.
  - Out-of-pytest smoke (scratchpad, not committed) over REAL chart +
    report servers: real mode — printed URLs live (HTTP 200), a venue
    close lands in the report server's WebSocket replay payload with the
    real fill (`exit_price 112.0`, reason `sl`), the chart link carried,
    and the `LivePosition` drawing swapped for the final posbox; both
    mode — the sim's chart shared and the combined replay payload carrying
    `BTCUSDT (sim)` / `BTCUSDT (real)` rows.
- **Suggested commit messages**
  1. `feat(trader): §21 display bridge + real-fills pipeline — per-pair low-priority display queue/thread (trading always preempts, backlog coalescing), display_trades sim/real/both (externally driven LiveSimulation on a deep-copied strategy / real §16-§19 ClosedTrades with a get_account_info equity curve / shared-chart overlay + two-section combined page), URL printing with security note, D6 window for the real report, Trader combined report across displaying pairs, LiveSimulation quiet-push kwarg`
  2. `test(trader): §21 — sim/real/both payloads, URL-print mode incl. security note, window trim vs untouched run() records, priority starvation + push/venue-read coalescing, display-only forming feed matrix, combined gating and per-mode sources — 26 tests`

## Section 22 — demos & bridge polish — 2026-07-20

- **What was implemented**
  - **`demo.py`** (next_version #2, #3, #7 + the §1 auto-detection flag):
    - Full CLI — `--symbol`, `--timeframe`, `--count`, `--host`, `--port`
      (bridge), `--chart-host`, `--chart-port`,
      `--open-browser/--no-open-browser`, `--mode` — every default read from
      the config block, so nothing has to be `sed`-edited on the VPS.
      `build_parser()` is a separate function (testable, and `--help`
      documents each default).
    - **Symbol discovery is the default** (#2): `SYMBOL = None` → the run
      lists the account's symbols and exits with status 1 instead of failing
      on a guessed spelling.  Both failure paths now auto-suggest:
      the `symbol_select(...) failed` exception (matched on the message, so
      unrelated errors are *not* mis-reported as a symbol problem) and the
      empty-candles path.  `suggest_symbols()` tries the symbol's 3-letter
      stem first (`BTCUSD` → `*BTC*`) and falls back to the head of `*`,
      printing a ready-to-paste `python demo.py --symbol …` line.
    - **`CHART_HOST`** in the config block (#7), `"127.0.0.1"` with the
      `"0.0.0.0" to expose publicly` comment, wired to `Chart(host=…)` (§8)
      alongside `--chart-port`.
    - **`--mode auto|native|bridge`** exercises §1's OS auto-detection —
      passed straight to `Broker("metatrader", mode=…)`; the resolved
      `broker.mode` is printed at startup (`native` prints "in-process
      (native MetaTrader5)", bridge prints `host:port`).
    - Headless runs print the chart URL: `show(open_browser=…, block=False)`
      → print `chart.url` → `show(block=True)`, so `--no-open-browser` on a
      VPS gives a pasteable address (the §21 print-URL flow, demo edition).
    - `main(argv=None)` returns an exit status (0 chart shown, 1 nothing to
      chart) and is wired to `sys.exit`.
  - **`bridge_server.py`** (next_version #4, #5, #6, #8):
    - **IPC-timeout guidance** (#4): new `initialize_failure_message(error,
      path=None)` — on `mt5.last_error()` code `-10005` it prints the three
      fixes in order (`wineserver -k` first; do **not** pass `--path`, and if
      one *was* passed it is quoted back with "retry WITHOUT --path"; check
      Xvfb/DISPLAY) plus the MT5_WINE_SETUP.md pointer.  Other error codes
      keep the plain raw-error message.  `MT5Dispatcher.__init__` both
      **prints** (flush) and raises it — under Wine + Xvfb a traceback is
      easy to lose.
    - **`--path` demoted** (#5): kept as a working last-resort escape hatch,
      but its `--help` now reads "NOT recommended … often causes the IPC
      timeout (-10005)", and no example anywhere in the file hands it out.
      New docstring section "Do not pass ``--path``".
    - **Wine + Xvfb stdout workaround** (#6): docstring section explaining
      that `print()` may never reach the Linux terminal, with the
      redirect-to-`C:/bridge.log` recipe and where to read it from Linux
      (`~/.mt5/drive_c/`).
    - **Bind address documented** (#8): docstring section on
      `--host`/`--port` (default `127.0.0.1:18812`, SSH-tunnel recipe, and
      what `0.0.0.0` really exposes); `--host`/`--port` gained help text; new
      `bind_warning(host)` prints a security note from `main()` when the bind
      address is not loopback (the protocol is unauthenticated and can place
      orders).  The systemd default stays `127.0.0.1`.  CLI construction
      split into `build_parser()`.
  - **`ichimoku_strategy.py` — mode 2** (v091 item 5): `RUN_MODE = 1|2` plus
    a `── Live paper trading config ──` block (`LIVE_SYMBOL`, `LIVE_CANDLES`
    XOR `LIVE_START`, `LIVE_MIN_CANDLES`, MT5 mode/host/port/credentials,
    display + chart/report host & ports, `LIVE_LOG_EVENTS`,
    `LIVE_CANDLE_COUNT_LIMIT`).
    - `build_live_config()` builds the `TraderConfig` with the same values
      the backtest uses (leverage, risk, `tp_mode="multi_rr"` 1R…20R,
      force-close) and enforces the seed XOR with a clear error; costs follow
      §3 — `spread=None` so `run_live` fills it from the venue's
      `symbol_info`, `commission_type="per_lot"` + `commission=FEE_PER_LOT`
      because MT5 never reports commission.
    - `connect_live_broker()` → `Broker("metatrader", mode=…)` (native on
      Windows, bridge otherwise) printing the resolved transport;
      `run_live_mode()` seeds → simulates → chart + report + `[SIM]` terminal
      log via **`run_live(...)` (§13)**, blocking until Ctrl+C, then prints
      the final report through the existing `_print_report`.  No order code
      is involved anywhere (paper trading, D8).
    - `prepare_indicators()` now iterates the timeframes actually present in
      `data` instead of a hard-coded `("1m","5m","15m")` — the live feed is
      single-timeframe (`{"15m": df}`) and would otherwise `KeyError`.  Mode 1
      behaviour is unchanged (all three keys are present).  `main()` gained
      the `RUN_MODE` dispatch (+ validation) and the docstring gained the
      MODE 2 how-to.
- **Deviations from the v100.md spec** (all confirmed in Q&A before coding)
  - **§26 lists no tests for §22.**  Per your answer, a new
    `tests/test_demos.py` covers all three files rather than nothing.
  - `--path` is **kept** (demoted in help + purged from examples) rather than
    deleted — "removed from … all docs" read as a docs/recommendation change,
    with the flag surviving as an escape hatch.
  - `MT5_WINE_SETUP.md` was **not** touched: §23 owns it, its systemd block
    already carries no `--path`, and Part G already warns against it.
  - `IchimokuStrategy.prepare_indicators` had to change for mode 2 (see
    above) — mode 1 output is byte-identical, verified by a test that all
    three timeframes still get every `_`-prefixed column.
  - Mode 2 forces `check_lower_tf=False` with a printed note (the 5m/1m
    confirmation cannot be evaluated on a single-timeframe live feed).
  - The live chart carries no `chart_indicators`: `TraderConfig` has no such
    field (§11) and adding one would be out-of-scope; the in-browser
    INDICATORS toolbar (§8/v0.8.0) covers it. Noted in the file.
  - Demo-script header versions updated (`demo.py` v0.9.1 → v1.0.0,
    `ichimoku_strategy.py` v0.8.0 → v1.0.0, including the `main()` banner):
    these are the example scripts' own "targets AlgoTradeKit vX" labels, not
    the package version — `pyproject.toml` / `__init__.py` untouched.
  - Six pre-existing ruff findings in `ichimoku_strategy.py` (I001 import
    order, 4× UP045 `Optional[...]`, E741 ambiguous `l`) were fixed while
    editing the file, following §1's precedent of leaving touched files
    ruff-clean.  `E741` fix renames `h, l, c` → `high, low, close`.
- **New files**
  - `/amb/AlgoTradeKit/tests/test_demos.py`
- **Changed files**
  - `/amb/AlgoTradeKit/demo.py` — argparse CLI (9 flags), symbol discovery as
    the default + both auto-suggest paths, `CHART_HOST`, `--mode`
    pass-through with the resolved transport printed, headless URL print,
    `main(argv)` exit status.
  - `/amb/AlgoTradeKit/src/AlgoTradeKit/broker/metatrader/bridge_server.py` —
    `initialize_failure_message()` (IPC-timeout guidance, printed *and*
    raised), `bind_warning()` + non-loopback warning in `main()`,
    `build_parser()` with documented `--host`/`--port` and a demoted
    `--path`, docstring sections for bind address / never-`--path` /
    Wine+Xvfb stdout workaround.
  - `/amb/AlgoTradeKit/ichimoku_strategy.py` — `RUN_MODE` + live config
    block, `build_live_strategy_cfg()` / `build_live_config()` /
    `connect_live_broker()` / `run_live_mode()` (new SECTION 6),
    `prepare_indicators` follows the timeframes present, `main()` dispatch,
    MODE 2 docs, imports + pre-existing ruff fixes.
- **Tests added** (`tests/test_demos.py`, 41 tests)
  - `TestDemoCli` (4): parser defaults equal the config-block constants
    (incl. `SYMBOL is None` and `CHART_HOST`); every setting overridable from
    the CLI; `--open-browser` forcible on; the `CHART_HOST` block documents
    the public option.
  - `TestDemoSymbolDiscovery` (7): no `--symbol` lists symbols, exits 1 and
    never fetches or charts; `symbol_select` failure auto-suggests with a
    ready-to-paste command; an unrelated fetch error is **not** reported as a
    symbol problem; empty candles suggest; stem-match preferred over the full
    list; full-list fallback; `list_symbols` swallows broker errors.
  - `TestDemoTransportAndChart` (4): `--mode` reaches the factory and the
    resolved transport is printed (native + bridge wording); `auto` is the
    default; the chart gets host/port/title/candles and the exact
    `show()` sequence; headless run prints the chart URL.
  - `TestBridgeIpcTimeoutGuidance` (5): -10005 message names `wineserver -k`,
    `--path` and the guide; a passed `--path` is quoted back; other codes
    stay hint-free; the dispatcher prints *and* raises it; a successful
    `initialize()` still builds a dispatcher.
  - `TestBridgeCliAndDocs` (6): loopback defaults; `bind_warning` fires only
    for public hosts; `main()` prints it before serving; `--path` help says
    NOT recommended and cites -10005; the docstring documents `--host`/
    `--port`/`0.0.0.0` and never `--path` in a runnable example; the
    Wine+Xvfb `C:/bridge.log` workaround is documented.
  - `TestIchimokuIndicatorPrep` (3): single-timeframe `{"15m": …}` prep gains
    every `_` column (the mode-2 requirement); all three timeframes still do;
    the caller's frame is not mutated.
  - `TestIchimokuLiveConfig` (6): live config mirrors the backtest settings
    and the §3 cost policy; date seed; the seed XOR raises both ways
    (parametrized); display/log switches carried; `CHECK_LOWER_TF` disabled
    with a note, and silent when already off.
  - `TestIchimokuLiveMode` (5): `run_live_mode` wires broker/strategy/config/
    `initial_balance` and prints the "no orders are placed" banner + seed
    description; date-seed announcement; `main()` dispatches to mode 2
    without touching the CSV loader; mode 1 still backtests; an unknown
    `RUN_MODE` raises.
  - Results: full suite `1381 passed, 23 warnings in 50.87s` (1340 baseline
    + 41 new; warnings are the pre-existing environmental set — system
    `websockets` deprecations + the known server-stop thread warning).  Ruff
    on all §22-touched files (`demo.py`, `ichimoku_strategy.py`,
    `bridge_server.py`, `tests/test_demos.py`): `All checks passed!`
    Repo-wide `ruff check src/ tests/` stays at its 60-error baseline (older
    files §22 never touched).  black remains unavailable in this environment
    and the tree is not black-clean at baseline (§1) — new code follows the
    100-char limit and the surrounding hand style.
- **Suggested commit messages**
  1. `feat(broker): bridge_server polish — IPC-timeout (-10005) guidance printed and raised, --path demoted to a documented last resort, bind-address warning for non-loopback --host, Wine+Xvfb stdout workaround documented`
  2. `feat(demo): demo.py CLI (symbol/timeframe/count/bridge host+port/chart host+port/open-browser/mode) with symbol discovery as the default and auto-suggest on symbol_select failure`
  3. `feat(demo): ichimoku_strategy.py mode 2 — live paper trading on MT5 via run_live (seed last N candles or from a date, chart + report + [SIM] event log), single-timeframe indicator prep`
  4. `test(demo): §22 — demo CLI/discovery/chart wiring, bridge IPC-timeout and bind guidance, ichimoku mode-2 config and dispatch — 41 tests`

## Section 23 — documentation updates — 2026-07-20

- **What was implemented**
  - **`MT5_WINE_SETUP.md`** — reworked for the v1.0.0 cross-platform reality:
    - **New routing table at the top**: the library auto-detects the OS —
      Windows → native, Linux/macOS → bridge, non-default `host` → bridge from
      any OS — with links to the two new sections; the guide is explicitly the
      **Linux/Wine path** and the thing the library's error messages point at.
    - **New "Windows — no bridge needed" section**: `pip install
      AlgoTradeKit[mt5]`, terminal installed + logged in once, `Broker(
      "metatrader")` → `mode="native"`, plus the two native failure messages
      and their fixes.
    - **New "What the library's error messages mean" section** (§1/D4): a
      table mapping each real `ConnectionFailed` string to its meaning and the
      Part that fixes it — wine missing → A, prefix missing → B, bridge down →
      G, remote host → G-on-that-machine, bridge closed mid-call, plus the
      bridge-side IPC-timeout line.
    - **Part E rewritten for the two-file deploy** (the §1 change that was
      explicitly deferred here): `bridge_server.py` **and** `_ops.py` into one
      folder, `scp` brace-expansion + a copy snippet, and the
      `ModuleNotFoundError: No module named '_ops'` symptom if only one is
      copied.
    - **Part G strengthened**: `--path` still discouraged (now noting it
      survives only as a documented escape hatch), `--host 0.0.0.0` warning
      (unauthenticated, order-capable), the bridge's own -10005 guidance, and
      the Wine+Xvfb stdout workaround with the `C:/bridge.log` recipe (#4, #6,
      #8).
    - **Part I rewritten**: the three `sed -i` edits are gone — `demo.py` is
      driven by flags (symbol discovery first, then a headless charting run)
      with a flag table; the SSH tunnel stays Option 1, socat stays Option 2.
    - **New Part J** — live sessions on the VPS: a `run_live` snippet with
      `display_open_browser=False` + `chart_host`, the printed-URL sample
      output, the security note preferring the SSH tunnel, and a pointer to
      `ichimoku_strategy.py` mode 2.
    - Troubleshooting table updated: IPC-timeout row notes the bridge prints
      the fix itself, new `_ops` row, symbol row points at `python demo.py`
      with no `--symbol`, stdout row carries the `drive_c` read-back path, new
      Windows missing-package row. The MetaTrader5-is-not-a-dependency note
      (#9) now also states there is no Linux build and that Windows uses the
      marker-gated `[mt5]` extra.
  - **`PROJECT_STRUCTURE.md`**:
    - Tree + test list updated (`trader/`, `test_broker*.py`, `test_demos.py`,
      `test_docs.py`, `test_trader.py`, `demo.py`, `MT5_WINE_SETUP.md`);
      data-flow diagram gains `trader` with the "nothing imports from trader"
      leaf rule and the `simulate`↛`trader` ban.
    - `broker`: `mode`/`NativeTransport`/`_ops.py` transport table + auto rules,
      polling streams, `get_trading_costs`/`clock_offset_ms`/`TradingCosts`,
      two-file bridge deploy, new compatibility rule for MT5 ops.
    - `indicator`: incremental `update()` section. `strategy`: the
      `update_indicators` hook + the four `_incremental` functions and the
      fill-only fallback limits. `simulate`: `_position_math`,
      `SimulationStepper`, `LiveSimulation`, and the candle-loop block renamed
      off the old private helper names (the stale `_make_closed_trade` /
      `_apply_partial_close` references §6 flagged for this section).
    - **New `Module: trader` section**: public API, file table, `run_live`,
      `Trader`, `TraderConfig` group table, `TraderPair`, the event table +
      terminal printer, and the display/`display_trades` semantics.
    - `visual`/`report`: live-push APIs (`host`, `LivePosition`,
      `update_drawing`, `set_candle_limit`, `push_update`, combined reports).
    - Compatibility rules extended to 10 (position maths single source, new
      `TraderConfig` field, new event type, the module-import bans);
      version-history row for **1.0.0**.
  - **`CLAUDE.md`**: architecture diagram gains `trader` + the import bans;
    `broker` section rewritten for cross-platform MT5 + streaming + costs/clock;
    indicator `update()`; strategy incremental subsection; simulate section
    updated to `SimulationStepper`/`_position_math`/`LiveSimulation` (stale
    private names fixed); **new `trader/` section** (entry-point table + the
    five rules live trading depends on: venue-native SL/TP, arithmetic
    closed-candle detection, shared position maths, display never slows
    trading, venue-native ladders); visual/report live paragraphs; four new Key
    conventions; v1.0.0 commands (`[mt5]` extra, `demo.py`, bridge run,
    ichimoku mode 2); `Current version: 1.0.0` + a reference-documents pointer.
  - **`v091.md` / `next_version.md`**: superseded banners naming `v100.md` as
    the source of truth and its §24/§25 mapping tables (and, for v091, the D8
    `dry_run` → `run_live` change of plan). Files kept for history.
- **Deviations from the v100.md spec** (all confirmed in Q&A before writing)
  - **`README.md` and `CHANGELOG.md` were not touched** — §23 tags them
    "(release step, §27)" and your rule 4 defers them to the release pass.
  - Superseded files got a **banner** rather than deletion, so the §24/§25
    mapping tables still resolve.
  - Version statements: PROJECT_STRUCTURE's 1.0.0 history row **and**
    CLAUDE.md's `Current version: 1.0.0` are written now (prose describing the
    release being built); `pyproject.toml` / `__init__.py` stay at 0.9.1 until
    the release step.
  - Added beyond the spec's bullet list: MT5_WINE_SETUP.md **Part J**
    (`run_live`/`Trader` on a VPS) — §23 asks for the print-URL workflow to
    replace the socat hack "for trader/run_live", which needed a place to live;
    and the Part E two-file fix, which §1 explicitly deferred to this section.
  - §26 lists no tests for §23 either — per your answer, `tests/test_docs.py`
    was written instead of nothing.
- **New files**
  - `/amb/AlgoTradeKit/tests/test_docs.py`
- **Changed files**
  - `/amb/AlgoTradeKit/MT5_WINE_SETUP.md` — OS-routing intro, Windows section,
    error-message→Part table, Part E two-file deploy, Part G hardening
    (`--path`, bind address, stdout workaround), Part I on CLI flags, new Part
    J, refreshed troubleshooting + dependency note.
  - `/amb/AlgoTradeKit/PROJECT_STRUCTURE.md` — tree/test list, data flow with
    `trader`, broker/indicator/strategy/simulate/visual/report v1.0.0 sections,
    new `trader` module section, 10 compatibility rules, 1.0.0 history row.
  - `/amb/AlgoTradeKit/CLAUDE.md` — diagram + import bans, broker rewrite,
    indicator/strategy/simulate updates, new `trader/` section, visual/report
    live paragraphs, new conventions, v1.0.0 commands, version line.
  - `/amb/AlgoTradeKit/v091.md` — superseded banner.
  - `/amb/AlgoTradeKit/next_version.md` — superseded banner.
- **Tests added** (`tests/test_docs.py`, 57 tests)
  - `TestGuideStructure` (11): all nine Parts A–I exist as headings; the
    Windows / error-message / Troubleshooting sections exist; the intro's two
    in-page anchors resolve to real headings.
  - `TestGuideMatchesDiagnostics` (4): every `MT5_WINE_SETUP.md Part X` string
    inside `_bridge_client.py` resolves to a real heading; all four diagnostic
    messages are documented; the native transport's install/initialize hints
    are documented and the guide quotes the real `_INSTALL_HINT` text; the
    guide's `wineserver -k` / `-10005` guidance matches what
    `bridge_server.initialize_failure_message()` actually prints.
  - `TestGuideInstructionsMatchReality` (8): Part E names both files **and**
    both really sit in the package directory; the `[mt5]` extra text matches
    `pyproject.toml` and `MetaTrader5` is absent from the core deps (text-
    parsed on purpose — `tomllib` is 3.11+, the package floor is 3.10); Part I
    contains no `sed -i` and names the new flags; **every `--flag` the guide
    mentions exists** in `demo.py`'s or `bridge_server.py`'s parser (the
    drift-catcher); documented bridge defaults equal the parser defaults; no
    runnable line (`wine`/`xvfb-run`/`DISPLAY=`/`ExecStart`) passes `--path`;
    the stdout workaround and the not-a-Linux-dependency note are present.
  - `TestModuleMap` (20): both docs describe all 8 shipped modules, every
    documented module directory exists, both data-flow diagrams include
    `trader`, and both state the import bans.
  - `TestAdvertisedApiExists` (10): for `trader`/`simulate`/`strategy`/
    `visual`/`report`/`broker`, every name the docs advertise is in that
    module's `__all__` **and** every checked export appears in the docs; the
    documented `trader/` file table matches the files on disk; the top-level
    `run_live` re-export works and is documented; the `_position_math` helper
    names in CLAUDE.md all exist.
  - `TestVersionStatements` (2) + `TestSupersededRoadmaps` (3): the 1.0.0
    history row mentions `trader`, CLAUDE.md states the version, both roadmap
    files carry a SUPERSEDED banner pointing at `v100.md`, both still exist,
    and `v100.md` still carries the §24/§25 mapping tables.
  - Results: full suite `1438 passed, 18 warnings in 55.40s` (1381 baseline +
    57 new; warnings are the pre-existing environmental set — system
    `websockets` deprecations + the known server-stop thread warning).  Ruff on
    the §23-touched code (`tests/test_docs.py`, `tests/test_demos.py`):
    `All checks passed!`  Repo-wide `ruff check src/ tests/` stays at its
    60-error baseline (older files this section never touched).  black remains
    unavailable in this environment and the tree is not black-clean at baseline
    (§1).
- **Suggested commit message(s)**
  1. `docs(mt5): rewrite MT5_WINE_SETUP.md for v1.0.0 — OS auto-detection routing, Windows native section, ConnectionFailed→Part diagnostics table, two-file bridge deploy (Part E), demo.py CLI instead of sed edits (Part I), run_live/Trader print-URL workflow (Part J), bind-address and stdout guidance`
  2. `docs: PROJECT_STRUCTURE + CLAUDE for v1.0.0 — trader module section, broker mode/streaming/costs/clock, indicator update(), strategy hook, SimulationStepper/_position_math/LiveSimulation, chart & report live APIs, new conventions and compatibility rules, 1.0.0 history row`
  3. `docs: mark v091.md and next_version.md superseded by v100.md`
  4. `test(docs): doc↔code consistency — guide Part references, documented flags vs parsers, advertised exports vs __all__, superseded banners — 57 tests`
