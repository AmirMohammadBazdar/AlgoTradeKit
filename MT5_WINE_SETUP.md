# Running MetaTrader 5 headless on a Linux VPS (Wine) — complete guide

> **Which path do I need?** (v1.0.0)
> The library **auto-detects the OS** — you do not choose a transport by hand:
>
> | You run on | What happens | What to read |
> |---|---|---|
> | **Windows** | `Broker("metatrader", ...)` talks to the local MT5 terminal **in-process** — no Wine, no bridge | [Windows — no bridge needed](#windows--no-bridge-needed) (one short section) |
> | **Linux / macOS** | the connector talks to the **Wine bridge** over TCP | this guide, Parts A–I |
> | any OS, `host=` a remote VPS | bridge on that host (a non-default `host` always means bridge) | Parts G–I |
>
> Override with `mode="native"` / `mode="bridge"` if you ever need to force it.
> Every `ConnectionFailed` the library raises names the Part below that fixes
> it — see [What the library's error messages mean](#what-the-librarys-error-messages-mean).

MetaTrader 5 is a **Windows** program and its `MetaTrader5` Python package only
talks to a running MT5 **terminal**. There is no public MT5 web API. So to use
it on a headless Linux VPS (SSH only, no desktop) we:

1. run the MT5 terminal + a Windows Python **inside Wine** (Wine runs Windows
   programs on Linux),
2. run everything under **Xvfb** (a fake, invisible screen — so no GUI is
   needed),
3. run AlgoTradeKit's **bridge server** in that Wine Python; it exposes MT5 over
   a tiny TCP/JSON socket,
4. run your actual code (`demo.py`) on the normal Linux side — or on your laptop
   — talking to the bridge.

```
┌─────────────────────── VPS (no GUI) ────────────────────────┐        your laptop
│  Xvfb ┌───────────── Wine ─────────────┐                    │  TCP   ┌──────────┐
│       │ MT5 terminal (logged in)       │                    │ 18812  │ demo.py  │
│       │ Windows Python + MetaTrader5   │◄── bridge_server ──┼────────┤ + browser│
│       └────────────────────────────────┘   (JSON socket)    │        └──────────┘
└─────────────────────────────────────────────────────────────┘
```

> **`MetaTrader5` is installed only inside the Wine Python** — it is *not* a
> dependency of AlgoTradeKit, so it can never conflict with the library's deps.
> Never `pip install MetaTrader5` in the VPS's normal (Linux) Python: no Linux
> build exists. The only place it is a real install is **Windows**, and there it
> is the opt-in extra `pip install AlgoTradeKit[mt5]` (declared with a
> `platform_system == "Windows"` marker, so Linux installs can never pull it in).

Tested on Ubuntu 22.04 / 24.04 (Debian is similar). Commands assume a normal
sudo user. Replace `user@your-vps`, the login/password/server, and the symbol
with your own.

---

## Windows — no bridge needed

On Windows the whole Wine/bridge apparatus below is unnecessary: the library
imports `MetaTrader5` and talks to the terminal in the same process.

```powershell
pip install AlgoTradeKit[mt5]        # the MetaTrader5 package, Windows only
```

Then:

1. Install the **MT5 terminal** (your broker's build) on the same machine.
2. Start it and **log in once** — the terminal must be able to attach.
3. Use the library exactly as everywhere else:

```python
from AlgoTradeKit.broker import Broker

mt = Broker("metatrader")            # mode="auto" → native on Windows
print(mt.mode)                       # "native"
print(mt.fetch_last_candles("EURUSD", "15m", 500)[-1])
```

Credentials are optional — pass `server=`/`login=`/`password=` to log the
terminal into a different account. `mode="bridge"` plus `host=`/`port=` still
works from Windows if you want to reach a **remote** VPS bridge instead.

Failure messages on this path:

| Message | Fix |
|---|---|
| `The 'MetaTrader5' package is not installed in this Python` | `pip install AlgoTradeKit[mt5]` (or `pip install MetaTrader5`). The package is Windows-only — on Linux/macOS use the bridge. |
| `mt5.initialize() failed — could not attach to a MetaTrader 5 terminal` (with the MT5 error code) | Install the terminal, start it and log in once, then retry. |

> On Linux/macOS **never** `pip install MetaTrader5` into the library's Python —
> there is no Linux build; it belongs only in the Wine Python (Part D).

---

## What the library's error messages mean

Per decision D4 the connector **diagnoses and stops** — it never silently falls
back or auto-starts anything. Each message names the Part that fixes it:

| `ConnectionFailed` message | Meaning | Fix |
|---|---|---|
| `Wine is not installed — see MT5_WINE_SETUP.md Part A.` | `wine` is not on `PATH`, so nothing can host the bridge | **Part A** |
| `MT5 Wine prefix not found (…) — see MT5_WINE_SETUP.md Part B` | Wine is there, but `$WINEPREFIX` / `~/.mt5` does not exist | **Part B**, then **Parts C–D** |
| `Bridge is not running — see MT5_WINE_SETUP.md Part G` | Wine and the prefix are fine, nothing answered on `host:port` | **Part G** (start it in tmux) |
| `Could not reach the MetaTrader bridge at HOST:PORT` (remote host) | A non-local `host` was given; local checks are skipped | **Part G** on that machine + open the port / SSH tunnel |
| `MetaTrader bridge closed the connection.` | The bridge died mid-call (terminal crash, `wineserver -k`) | Re-attach tmux, restart the bridge (**Part G**) |
| `… accepted the connection but sent no reply within Ns` | Something *is* listening on that port, but it is not answering: a stale SSH tunnel, a port forwarded to another service, a captive middlebox that accepts every TCP connection, or a bridge whose terminal is still starting | Confirm `bridge_server.py` owns that port (**Part G**), re-make the tunnel, then Troubleshooting |
| `mt5.initialize failed: (-10005, 'IPC timeout')` (printed **by the bridge**) | Stale Wine processes, or `--path` was passed | `wineserver -k`, retry **without** `--path` (**Part G**, Troubleshooting) |

---

## What you need first

- A VPS you reach over SSH (2 GB+ RAM recommended; MT5 + Wine is chunky).
- An **MT5 demo account** from a broker: three values —
  - **server** name, e.g. `MetaQuotes-Demo` or `YourBroker-Demo`
  - **login** (a number)
  - **password**
- Ideally your **broker's own MT5 installer** (its download page). Its server is
  pre-registered in the terminal, which makes headless login "just work". The
  generic MetaQuotes installer works too but may need the server added.

---

## Part A — install Wine + Xvfb on the VPS

```bash
sudo dpkg --add-architecture i386          # Wine needs 32-bit libs too
sudo apt update
sudo apt install -y wine winbind xvfb wget tmux cabextract
wine --version                              # expect wine-9.x or newer
```

---

## Part B — create an isolated Wine "prefix"

A prefix is a self-contained fake `C:` drive. We keep MT5 in its own.

```bash
export WINEPREFIX="$HOME/.mt5"
export WINEARCH=win64
export WINEDEBUG=-all
# Skip the Mono/Gecko pop-ups (we don't need them and can't click them headless):
export WINEDLLOVERRIDES="mscoree,mshtml="

Xvfb :99 -screen 0 1280x800x24 &
sleep 2
DISPLAY=:99 wine wineboot --init
sleep 20
```

Add those `export` lines to `~/.bashrc` so every new SSH session has them:

```bash
cat >> ~/.bashrc << 'EOF'

# Wine + MT5 setup
export WINEPREFIX="$HOME/.mt5"
export WINEARCH=win64
export WINEDEBUG=-all
export WINEDLLOVERRIDES="mscoree,mshtml="
export DISPLAY=:99
EOF

source ~/.bashrc
```

---

## Part C — install the MT5 terminal (under Xvfb, silent)

**Preferred — your broker's installer** (replace the URL with your broker's):

```bash
cd ~
wget -O mt5setup.exe "https://YOUR-BROKER-download-link/mt5setup.exe"
DISPLAY=:99 wine mt5setup.exe /auto
```

**Or the generic MetaQuotes installer:**

```bash
cd ~
wget -O mt5setup.exe "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe"
DISPLAY=:99 wine mt5setup.exe /auto
```

`/auto` runs a silent install; give it 2–5 minutes. When done the terminal is at:

```
~/.mt5/drive_c/Program Files/MetaTrader 5/terminal64.exe
```

(Broker-branded builds may use a different folder name — check with
`ls "$HOME/.mt5/drive_c/Program Files/"`.)

**Verify**:
```bash
ls "$HOME/.mt5/drive_c/Program Files/MetaTrader 5/terminal64.exe" && echo "MT5 OK"
```

---

## Part D — install Windows Python + the MetaTrader5 package

> ⚠️ **Python 3.8 is required.** Python 3.10/3.11 hit an
> `ucrtbase.dll.crealf` bug in Wine 9.0 that is not fixable via DLL overrides
> (marking ucrtbase as "native" breaks Wine's own system DLLs like sechost,
> advapi32, and user32). Python 3.8 avoids this entirely and works out of the
> box with Wine's built-in DLLs.

```bash
cd ~
wget https://www.python.org/ftp/python/3.8.10/python-3.8.10-amd64.exe
DISPLAY=:99 wine python-3.8.10-amd64.exe /quiet InstallAllUsers=1 PrependPath=1 Include_test=0
sleep 15

# Verify Python 3.8 installed
ls "$HOME/.mt5/drive_c/Program Files/Python38/python.exe" && echo "PYTHON OK"
```

Install the MetaTrader5 package:

```bash
DISPLAY=:99 wine "C:/Program Files/Python38/python.exe" -m pip install --upgrade pip MetaTrader5
```

Quick check (Wine Python stdout is unreliable — use a file):

```bash
DISPLAY=:99 wine "C:/Program Files/Python38/python.exe" -c "
import MetaTrader5 as m
with open('C:/mt5_check.txt', 'w') as f:
    f.write('MetaTrader5 ' + str(m.__version__) + '\n')
"
cat "$HOME/.mt5/drive_c/mt5_check.txt"
```

---

## Part E — get the bridge files onto the VPS

The bridge is **two files** (v1.0.0): `bridge_server.py` (lifecycle + TCP
server) and `_ops.py` (the MT5 operations, shared with the Windows native
transport). Copy **both into the same folder** — `bridge_server.py` imports
`_ops` from next to itself when it is not installed as a package.

They stay standalone: standard library + `MetaTrader5`, so AlgoTradeKit itself
does **not** need to be installed in the Wine Python.

Copy from your laptop:

```bash
scp /path/to/AlgoTradeKit/src/AlgoTradeKit/broker/metatrader/{bridge_server.py,_ops.py} \
    root@your-vps:~/
```

Or, if AlgoTradeKit is installed in the VPS's normal Python:

```bash
python3 - <<'PY'
import shutil, pathlib
import AlgoTradeKit.broker.metatrader.bridge_server as b
src = pathlib.Path(b.__file__).parent
for name in ("bridge_server.py", "_ops.py"):
    shutil.copy(src / name, pathlib.Path.home() / name)
    print("copied", name)
PY
```

> Copying only `bridge_server.py` fails at startup with
> `ModuleNotFoundError: No module named '_ops'`.

---

## Part F — one-time interactive login (skip if headless login works)

Some brokers (Alpari, FTMO, etc.) require the account to be logged in **once
interactively** before headless login works. Do this via VNC.

**On the VPS:**
```bash
# Kill any stale processes
wineserver -k -9
sleep 2

# Make sure Xvfb is running
ps aux | grep Xvfb | grep -v grep

# Start MT5 terminal
DISPLAY=:99 wine "C:/Program Files/MetaTrader 5/terminal64.exe" &
sleep 10

# Start VNC server
sudo apt install -y x11vnc
x11vnc -display :99 -localhost -rfbport 5900 -bg
```

**On your laptop** (first terminal — SSH tunnel):
```bash
ssh -L 5900:127.0.0.1:5900 root@your-vps
```

**On your laptop** (second terminal — VNC viewer):
```bash
# Arch/Omarchy
sudo pacman -S tigervnc
vncviewer 127.0.0.1:5900
```

Or use Remmina (VNC protocol, server `127.0.0.1:5900`).

In the VNC window, log in to MT5 with your demo account credentials. Wait until
it connects (balance shows in the bottom bar), then close the window. **This
step only needs to be done once** — after that, headless login works.

**Clean up:**
```bash
pkill -9 terminal64
pkill x11vnc
wineserver -k -9
sleep 3
```

---

## Part G — run the bridge (inside tmux)

```bash
tmux new -s mt5           # a detachable session so it survives your SSH logout
```

Inside tmux:

```bash
# Kill stale processes first
wineserver -k -9
sleep 3

# Run the bridge (DO NOT use --path — let mt5.initialize() auto-detect)
DISPLAY=:99 wine "C:/Program Files/Python38/python.exe" ~/bridge_server.py \
    --host 127.0.0.1 --port 18812 \
    --login 12345678 --password "YOUR_PASSWORD" --server "YourBroker-Demo"
```

> ⚠️ Never pass `--path` unless absolutely necessary. An explicit path often
> triggers `IPC timeout` (-10005) under Wine. Auto-detection works reliably.
> `--path` is still accepted as a last-resort escape hatch, and its `--help`
> text says the same thing — no example in this guide or in the library uses it.

You should see:

```
[mt5-bridge] listening on 127.0.0.1:18812
```

Detach (leave it running): press **Ctrl-b** then **d**.
Re-attach later: `tmux attach -t mt5`.

> Keep `--host 127.0.0.1` (the default — localhost only) and reach it via an SSH
> tunnel. `--host 0.0.0.0` exposes an **unauthenticated, order-capable** socket
> to the network; the bridge prints a warning when you do it, and the systemd
> unit below stays on `127.0.0.1`.

If you hit `IPC timeout`, always `wineserver -k` first before retrying — zombie
processes block the IPC channel. The bridge prints exactly that guidance itself
on a -10005 failure (kill stale Wine processes → don't pass `--path` → check
Xvfb/DISPLAY), so the terminal tells you the fix even without this guide.

> **Seeing no output at all?** Wine's Windows Python does not reliably deliver
> `print()` through `xvfb-run`. Redirect the bridge's output to a file on the
> Wine `C:` drive and read it from Linux:
>
> ```bash
> DISPLAY=:99 wine "C:/Program Files/Python38/python.exe" ~/bridge_server.py \
>     --host 127.0.0.1 --port 18812 > "C:/bridge.log" 2>&1
> cat ~/.mt5/drive_c/bridge.log      # from the Linux side
> ```

---

## Part H — find your symbol name

Symbol names vary wildly per broker. Check from your laptop through the tunnel:

```bash
# First, open the SSH tunnel:
ssh -N -L 18812:127.0.0.1:18812 root@your-vps

# Then in another terminal, query symbols:
python -c "
from AlgoTradeKit.broker import Broker
mt = Broker('metatrader', host='127.0.0.1', port=18812)
print(mt.list_symbols('*BTC*'))
print(mt.list_symbols('*'))
"
```

Common patterns:
- `EURUSD`, `BTCUSD`, etc.
- Suffixed: `BTCUSD.`, `BTCUSD.r`, `BITCOIN_i`, `EURUSD_i` (Alpari uses `_i`)
- Crypto: `BTCUSD.crypto`, `BITCOIN`, `Bitcoin`
- Index: `US30`, `SP500`, `US100Cash`

---

## Part I — run `demo.py` on the VPS, view chart on your laptop

**On the VPS** — install AlgoTradeKit in a virtual environment:

```bash
# Ubuntu 24 blocks system-wide pip (PEP 668) — use a venv
sudo apt install -y python3-venv
python3 -m venv ~/venv
source ~/venv/bin/activate
pip install AlgoTradeKit websockets pandas
```

Copy `demo.py` from your laptop to the VPS:

```bash
scp /path/to/AlgoTradeKit/demo.py root@your-vps:~/demo.py
```

**No file editing needed** (v1.0.0) — every setting is a CLI flag:

```bash
source ~/venv/bin/activate

# 1. Which symbols does this broker have?  (no --symbol = list them and exit)
python ~/demo.py

# 2. Chart one, headless, on a predictable port:
python ~/demo.py --symbol BITCOIN_i --timeframe 15m --count 500 \
                 --chart-port 8080 --no-open-browser
```

It prints the chart address to open (`Chart → http://127.0.0.1:8080`).

Useful flags:

| Flag | Purpose |
|---|---|
| `--symbol` `--timeframe` `--count` | what to fetch (omit `--symbol` to list symbols) |
| `--host` `--port` | where the **bridge** is (default `127.0.0.1:18812`) |
| `--chart-host` `--chart-port` | where the **chart server** binds (default `127.0.0.1`, auto port) |
| `--no-open-browser` | headless: print the URL instead of opening a tab |
| `--mode auto\|native\|bridge` | force the transport (default `auto` — see the top of this guide) |

**On your laptop** — two options to view the chart:

### Option 1: SSH tunnel (recommended, secure)

```bash
ssh -N -L 8080:127.0.0.1:8080 root@your-vps
```

Open **http://127.0.0.1:8080** in your browser.

### Option 2: Public port via socat (quick access, no tunnel)

On the VPS:

```bash
sudo apt install -y socat
socat TCP-LISTEN:8081,fork,reuseaddr TCP:127.0.0.1:8080 &
```

Open **http://YOUR_VPS_IP:8081** in your browser.

Kill socat when done:

```bash
pkill socat
```

### Option 3: Run demo.py from your laptop entirely

If your laptop has the library installed:

```bash
pip install AlgoTradeKit websockets pandas
ssh -N -L 18812:127.0.0.1:18812 root@your-vps   # tunnel bridge port
python demo.py --symbol BITCOIN_i                 # runs locally, opens browser
```

---

## Part J — live sessions on the VPS (`run_live` / `Trader`)

The v1.0.0 live entry points (`run_live()` paper trading, `Trader` real
orders) serve the same chart plus a live report, and they handle the "I am on
a VPS" case themselves — no `sed`, no socat needed:

```python
from AlgoTradeKit.trader import TraderConfig, run_live

config = TraderConfig(
    symbol="BITCOIN_i", min_candles=200,
    display=True, display_candles=1000,   # or display_start="2026/01/01"
    display_open_browser=False,           # VPS → print the URLs
    chart_host="0.0.0.0",                 # reachable from outside (see warning)
    chart_port=8080, report_port=8081,
    log_events=True,                      # per-event terminal log
)
run_live(strategy=MyStrategy(), broker=mt, config=config)
```

With `display_open_browser=False` the session prints the addresses to open:

```
[AlgoTradeKit] run_live BITCOIN_i chart  → http://127.0.0.1:8080
[AlgoTradeKit] run_live BITCOIN_i report → http://127.0.0.1:8081
```

> ⚠️ `chart_host="0.0.0.0"` exposes the chart and report to anyone who can
> reach those ports. The **SSH tunnel of Option 1 remains the recommended
> way** — keep `chart_host="127.0.0.1"` and forward the ports instead. The
> socat trick (Option 2) still works as a fallback.

`ichimoku_strategy.py` ships this as **mode 2** (`RUN_MODE = 2`) — set the
`LIVE_*` block, run it, and watch the chart + report + event log update on
every closed candle. No orders are placed in `run_live`.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Could not reach the MetaTrader bridge` | The bridge isn't running, wrong port, or the SSH tunnel dropped. Re-attach tmux (`tmux attach -t mt5`); confirm it says `listening`. Re-open the tunnel. |
| `mt5.login failed` | Wrong **server/login/password**, or the server isn't registered in the terminal. Use your **broker's** MT5 installer (not the generic one). Double-check the server string exactly. |
| `mt5.initialize failed: IPC timeout` (-10005) | The bridge now prints the fix list itself. Always run `wineserver -k` before retrying. Don't pass `--path`. If still failing, try starting the terminal manually first (`DISPLAY=:99 wine "C:/Program Files/MetaTrader 5/terminal64.exe" &` then kill it with `pkill terminal64`), then retry. |
| `ModuleNotFoundError: No module named '_ops'` | You copied only `bridge_server.py`. The bridge is two files — see Part E. |
| `symbol_select(BTCUSD) failed` / **no candles** | The symbol name is wrong. Run `python demo.py` with **no** `--symbol` to list the account's symbols (or see Part H). Names vary a lot — Alpari uses the `_i` suffix (e.g. `BITCOIN_i`). |
| Wine shows a **Mono / Gecko** pop-up and hangs | You skipped `WINEDLLOVERRIDES="mscoree,mshtml="`. Export it and retry that step. |
| Terminal exits immediately / can't log in headless | Do the one-time interactive login via VNC (Part F), then the headless login works. |
| `wine: command not found` after reboot | Re-export `WINEPREFIX`/`WINEARCH` (put them in `~/.bashrc`). |
| `pip install` fails with `externally-managed-environment` | You're on Ubuntu 24+. Use a venv: `python3 -m venv ~/venv && source ~/venv/bin/activate && pip install ...` |
| `unimplemented function ucrtbase.dll.crealf` | You used Python 3.10/3.11. **Use Python 3.8 instead** (see Part D). Setting ucrtbase to "native" breaks Wine completely. |
| Wine Python command produces **no output** | Wine Python stdout is unreliable under Xvfb. Redirect to a file on the Wine `C:` drive and read it from Linux — `> "C:/bridge.log" 2>&1`, then `cat ~/.mt5/drive_c/bridge.log` (recipe in Part G; also documented in `bridge_server.py`'s docstring). |
| `The 'MetaTrader5' package is not installed in this Python` | You are on **Windows**: `pip install AlgoTradeKit[mt5]`. On Linux this means `mode="native"` was forced — use the bridge instead. |

---

## Optional — auto-start the bridge on boot (systemd)

Create `/etc/systemd/system/mt5-bridge.service`:

```ini
[Unit]
Description=AlgoTradeKit MT5 bridge
After=network-online.target

[Service]
User=YOUR_USER
Environment=WINEPREFIX=/home/YOUR_USER/.mt5
Environment=WINEDEBUG=-all
Environment=WINEDLLOVERRIDES=mscoree,mshtml=
Environment=DISPLAY=:99
ExecStartPre=/usr/bin/pkill -9 wineserver 2>/dev/null; /usr/bin/sleep 3
ExecStart=/usr/bin/xvfb-run -a /usr/bin/wine "C:/Program Files/Python38/python.exe" /home/YOUR_USER/bridge_server.py \
    --host 127.0.0.1 --port 18812 \
    --login 12345678 --password "YOUR_PASSWORD" --server "YourBroker-Demo"
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mt5-bridge
journalctl -u mt5-bridge -f          # watch its logs
```

> The password sits in this file in plain text — `sudo chmod 600` it and keep
> the VPS locked down. This is a **demo** account; never put a funded live
> account on an unhardened box.
