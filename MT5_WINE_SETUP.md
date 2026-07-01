# Running MetaTrader 5 headless on a Linux VPS (Wine) — complete guide

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

Tested on Ubuntu 22.04 / 24.04 (Debian is similar). Commands assume a normal
sudo user. Replace `user@your-vps`, the login/password/server, and the symbol
with your own.

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
wine --version                              # expect wine-6.x or newer
```

If `apt`'s Wine is old (< 6.0) and MT5 misbehaves, install the newer WineHQ
build — but try the distro one first; it's usually fine.

---

## Part B — create an isolated Wine "prefix"

A prefix is a self-contained fake `C:` drive. We keep MT5 in its own.

```bash
export WINEPREFIX="$HOME/.mt5"
export WINEARCH=win64
export WINEDEBUG=-all
# Skip the Mono/Gecko pop-ups (we don't need them and can't click them headless):
export WINEDLLOVERRIDES="mscoree,mshtml="

xvfb-run -a wineboot --init
sleep 15
```

Add those `export` lines to `~/.bashrc` so every new SSH session has them, or
re-run them each time.

---

## Part C — install the MT5 terminal (under Xvfb, silent)

**Preferred — your broker's installer** (replace the URL with your broker's):

```bash
cd ~
wget -O mt5setup.exe "https://YOUR-BROKER-download-link/mt5setup.exe"
xvfb-run -a wine mt5setup.exe /auto
```

**Or the generic MetaQuotes installer:**

```bash
cd ~
wget -O mt5setup.exe "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe"
xvfb-run -a wine mt5setup.exe /auto
```

`/auto` runs a silent install; give it 2–5 minutes. When done the terminal is at:

```
~/.mt5/drive_c/Program Files/MetaTrader 5/terminal64.exe
```

(Broker-branded builds may use a different folder name — check with
`ls "$HOME/.mt5/drive_c/Program Files/"`.)

---

## Part D — install Windows Python + the MetaTrader5 package

Use Python **3.11** (has `MetaTrader5` wheels).

```bash
cd ~
wget https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe
xvfb-run -a wine python-3.11.9-amd64.exe /quiet InstallAllUsers=1 PrependPath=1 Include_test=0
sleep 10

# Install the package into the Wine Python:
xvfb-run -a wine python -m pip install --upgrade pip MetaTrader5
```

If `wine python` isn't found, use the full path (adjust the version folder):

```bash
xvfb-run -a wine "C:/Program Files/Python311/python.exe" -m pip install --upgrade pip MetaTrader5
```

Quick check that the package imports inside Wine:

```bash
xvfb-run -a wine python -c "import MetaTrader5 as m; print('MetaTrader5', m.__version__)"
```

---

## Part E — get `bridge_server.py` onto the VPS

It ships inside AlgoTradeKit and is standalone (standard library + MetaTrader5 —
it does **not** need AlgoTradeKit installed in the Wine Python).

If AlgoTradeKit is installed in the VPS's normal Python:

```bash
cp "$(python3 -c 'import AlgoTradeKit.broker.metatrader.bridge_server as b; print(b.__file__)')" ~/bridge_server.py
```

Otherwise just copy the file from the repo (`src/AlgoTradeKit/broker/metatrader/bridge_server.py`)
to `~/bridge_server.py` with `scp`.

---

## Part F — run the bridge (keep it running with tmux)

```bash
tmux new -s mt5           # a detachable session so it survives your SSH logout

export WINEPREFIX="$HOME/.mt5"
export WINEDEBUG=-all

xvfb-run -a wine python ~/bridge_server.py \
    --host 127.0.0.1 --port 18812 \
    --login 12345678 --password "YOUR_PASSWORD" --server "YourBroker-Demo" \
    --path "C:/Program Files/MetaTrader 5/terminal64.exe"
```

You should see:

```
[mt5-bridge] listening on 127.0.0.1:18812
```

Detach (leave it running): press **Ctrl-b** then **d**.
Re-attach later: `tmux attach -t mt5`.

> Keep `--host 127.0.0.1` (localhost only) and reach it via an SSH tunnel — do
> **not** expose the bridge to the internet; it has no authentication.

---

## Part G — run `demo.py` from your laptop (where a browser exists)

On your **laptop** install the library (with `websockets` for the chart):

```bash
pip install AlgoTradeKit websockets pandas
# or, from the repo:  pip install -e ".[dev]"
```

Open an SSH tunnel so `127.0.0.1:18812` on your laptop reaches the VPS bridge —
keep this terminal open:

```bash
ssh -N -L 18812:127.0.0.1:18812  user@your-vps
```

In another terminal, edit the config block at the top of `demo.py` (set your
`SYMBOL`), then:

```bash
python demo.py
```

The last 100 candles print to the console and a candle chart opens in your
browser. 🎉

### Alternative: run everything on the VPS

If you'd rather run `demo.py` on the VPS, the chart server binds
`127.0.0.1:<port>` there, so forward that port instead:

- In `demo.py` set `OPEN_BROWSER = False` and `CHART_PORT = 8080`.
- `python3 demo.py` on the VPS.
- From your laptop: `ssh -N -L 8080:127.0.0.1:8080 user@your-vps`
- Open **http://127.0.0.1:8080** in your laptop browser.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Could not reach the MetaTrader bridge` | The bridge isn't running, wrong port, or the SSH tunnel dropped. Re-attach tmux (`tmux attach -t mt5`); confirm it says `listening`. Re-open the tunnel. |
| `mt5.login failed` | Wrong **server/login/password**, or the server isn't registered in the terminal. Use your **broker's** MT5 installer (not the generic one). Double-check the server string exactly. |
| `mt5.initialize failed` | Terminal not found — pass the right `--path` to `terminal64.exe` (check `ls "$HOME/.mt5/drive_c/Program Files/"`). |
| `symbol_select(BTCUSD) failed` / **no candles** | The symbol name is wrong. Names vary a lot (`BTCUSD`, `BTCUSD.`, `BTCUSD.r`, `Bitcoin`, `BTC/USD`). `demo.py` prints suggestions; or call `mt.list_symbols("*BTC*")`. |
| Wine shows a **Mono / Gecko** pop-up and hangs | You skipped `WINEDLLOVERRIDES="mscoree,mshtml="`. Export it and retry that step. |
| Terminal exits immediately / can't log in headless | A few brokers require the terminal to be logged in **once interactively** to accept the account. Do it via VNC once (see below), then the headless login works. |
| `wine: command not found` after reboot | Re-export `WINEPREFIX`/`WINEARCH` (put them in `~/.bashrc`). |

### One-time interactive login (only if your broker needs it)

If headless login is refused, log in once with a temporary GUI over SSH:

```bash
sudo apt install -y x11vnc
Xvfb :0 -screen 0 1280x800x24 &
DISPLAY=:0 WINEPREFIX=$HOME/.mt5 wine "C:/Program Files/MetaTrader 5/terminal64.exe" &
x11vnc -display :0 -localhost -rfbport 5900 &
# From your laptop:  ssh -L 5900:127.0.0.1:5900 user@your-vps
# then connect a VNC viewer to 127.0.0.1:5900, log into the account, close it.
```

After that, run the bridge (Part F) normally.

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
ExecStart=/usr/bin/xvfb-run -a /usr/bin/wine python /home/YOUR_USER/bridge_server.py \
    --host 127.0.0.1 --port 18812 \
    --login 12345678 --password "YOUR_PASSWORD" --server "YourBroker-Demo" \
    --path "C:/Program Files/MetaTrader 5/terminal64.exe"
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
