"""
Send Telegram a picture of BTCUSDT's last six hours on 15m, with EMA(20).

    pip install "AlgoTradeKit[notify]"                         # once: the picture engine
    playwright install chromium                                # once: its browser
    read -rs TELEGRAM_BOT_TOKEN && export TELEGRAM_BOT_TOKEN   # paste the token; no echo
    export TELEGRAM_CHAT_ID=123456789                          # your own chat with the bot
    python examples/06_telegram_chart_picture.py

What it needs:

* The ``[notify]`` extra and its browser: a headless Chromium photographs the
  library's own chart page.  On Linux, ``playwright install --with-deps chromium``
  also installs the system libraries it needs (with apt, as root).
* ``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_CHAT_ID``: a bot made with @BotFather, and
  where to send.  ``python examples/04_telegram_messages.py`` with the token alone
  lists the chats that wrote to the bot, with their ids.
* ``TELEGRAM_PROXY`` (optional): ``http://127.0.0.1:10809`` or
  ``socks5h://127.0.0.1:10808``, where Telegram is blocked.
* The network to Binance's public candles (no key) and to unpkg.com, where the
  chart page loads its charting library from.

The candles are fetched when ``send_chart`` is called; the picture is made on the
notifier's own thread and sent with its caption.  The first picture takes a few
seconds more, while the browser starts.
"""
from __future__ import annotations

import os
import sys
import time

from AlgoTradeKit.broker import Broker
from AlgoTradeKit.notify import NotifyError, TelegramNotifier

token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
proxy = os.environ.get("TELEGRAM_PROXY", "").strip() or None

# ── What to set ───────────────────────────────────────────────────────
if not token or not chat:
    print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to run this:\n"
          "    read -rs TELEGRAM_BOT_TOKEN && export TELEGRAM_BOT_TOKEN\n"
          "    export TELEGRAM_CHAT_ID=<your own chat's id>\n"
          "examples/04_telegram_messages.py with the token alone lists the chat ids.")
    sys.exit(0)

# ── The notifier ──────────────────────────────────────────────────────
try:
    bot = TelegramNotifier(token, {"me": chat}, proxy=proxy)
except NotifyError as exc:
    sys.exit(f"  {exc}")

# ── The picture ───────────────────────────────────────────────────────
# Six hours of 15m candles end now: send_chart fetches them on this thread, and a
# wrong symbol or a venue that cannot be reached raises right here.  Without the
# extra or its browser it raises too, saying what to install.
now_ms = int(time.time() * 1000)
try:
    sent = bot.send_chart(
        broker=Broker("binance-spot"),          # public market data, no key
        symbol="BTCUSDT",
        timeframe="15m",
        start=now_ms - 6 * 3_600_000,
        end=now_ms,
        indicators=[{"kind": "ema", "period": 20}],
        caption="<b>BTCUSDT · 15m</b> · the last six hours with EMA(20)\n🕑 {time}",
    )
except (ImportError, RuntimeError) as exc:   # no picture engine, or no browser
    sys.exit(f"  {exc}")
except NotifyError as exc:                   # Telegram refused it
    sys.exit(f"  {exc}")
print(f"Sent: message {sent['me']}.")
