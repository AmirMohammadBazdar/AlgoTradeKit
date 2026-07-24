> **SUPERSEDED — kept for history.** All ten items below shipped in **v1.0.0**;
> `v100.md` is the source of truth, and its §24 maps each item to the section
> that delivered it (chart `host` → §8, `demo.py` symbol discovery + CLI args and
> the `bridge_server.py` guidance → §22, packaging notes → §1, docs → §23).
> Do not implement from this file.

# Next Version Improvements

Notes from Wine+MT5 setup on Ubuntu 24 (2026-07-02).

## Library changes to make the setup easier

### 1. Chart server — make `host` configurable

Currently `Chart()` and `ChartServer` hardcode `127.0.0.1`. They should accept an optional `host` parameter:

```python
chart = Chart(host="0.0.0.0")   # listen on all interfaces
```

**Files:** `src/AlgoTradeKit/visual/server.py:46`, `src/AlgoTradeKit/visual/server.py:177`,
`src/AlgoTradeKit/visual/chart.py:1155`

**Why:** When running `demo.py` on the VPS, users currently need socat or an SSH tunnel
to view the chart from their laptop. A configurable host lets them bind to `0.0.0.0`
directly (with a big warning about security).

### 2. `demo.py` — make symbol discovery the default

When `fetch_last_candles` returns empty, `demo.py` already prints suggested symbols.
But the error path (`symbol_select failed`) could auto-suggest. Consider making the
initial symbol `""` or `None` so demo.py always lists symbols on first run.

### 3. `demo.py` — configurable via CLI args or env vars

Add `--symbol`, `--timeframe`, `--count` CLI args to `demo.py` so users don't
have to edit the file on the VPS. Also add `--host` and `--port` for the bridge.

### 4. `bridge_server.py` — warn about PID/cleanup on IPC timeout

When `mt5.initialize()` fails with IPC timeout (-10005), print a helpful message
suggesting `wineserver -k` before retrying, and mention that `--path` often causes
this problem.

### 5. `bridge_server.py` — don't recommend `--path` in docs

Remove `--path` from the systemd example and default docs. Auto-detection works
better under Wine; an explicit path often triggers IPC timeouts.

### 6. Windows Python stdout — document the workaround

Add a docstring note in `bridge_server.py` that under Wine+Xvfb, Python's print()
may not reach the terminal. Write diagnostic output to a file on `C:/` as a
workaround.

### 7. Consider `chart_host` in demo.py config block

```python
CHART_HOST  = "127.0.0.1"   # "0.0.0.0" to expose publicly
```

### 8. Consider `--bridge-host` / `--bridge-port` in bridge_server.py

Allow specifying the bind address (currently hardcoded to what's passed via CLI,
which is fine, but the default in systemd/service should be `127.0.0.1`).

## Packaging / metadata

### 9. PyPI dependency note

The MetaTrader5 package is NOT a dependency of AlgoTradeKit — it only lives in
the Wine Python. Make sure this is clearly documented (already is, but worth
repeating) so users don't try to `pip install MetaTrader5` in the Linux Python.

### 10. Add `websockets` to `[dev]` extras

`demo.py` needs `websockets` for the chart. It's already in `[dev]` but verify:
```toml
[project.optional-dependencies]
dev = ["pytest", "pytest-cov", "websockets", "pandas"]
```
