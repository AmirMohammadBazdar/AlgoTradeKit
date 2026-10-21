"""
Price alerts without a strategy: Telegram hears when BTCUSDT crosses a level.

    read -rs TELEGRAM_BOT_TOKEN && export TELEGRAM_BOT_TOKEN   # paste the token; no echo
    export TELEGRAM_CHAT_ID=123456789                          # your own chat with the bot
    python examples/07_price_alerts.py                         # Ctrl+C to stop

What it needs:

* ``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_CHAT_ID``: a bot made with @BotFather, and
  where to send.  ``python examples/04_telegram_messages.py`` with the token alone
  lists the chats that wrote to the bot, with their ids.
* ``TELEGRAM_PROXY`` (optional): ``http://127.0.0.1:10809`` or
  ``socks5h://127.0.0.1:10808``, where Telegram is blocked.
* The network to Binance's public market data; no key.

It sets one alert with two levels, 0.2% above and 0.2% below BTCUSDT's price at the
start, so on a usual day it fires within minutes.  It watches the last trade — the
forming 1m candle's close — and fires once, on whichever level the price crosses
first: «🔔 BTCUSDT crossed above 84,068.4 · now 84,070.1».  ``bot.wait()`` keeps the
script alive until then, or until Ctrl+C.
"""
from __future__ import annotations

import os
import sys

from AlgoTradeKit.broker import Broker, BrokerError
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

# ── The alert ─────────────────────────────────────────────────────────
# watch() returns at once: the broker's candle stream runs on a thread of its own,
# and the alert fires from there.  A wrong symbol, or no Binance, raises right here.
binance = Broker("binance-spot")                # public market data, no key
try:
    price = binance.get_ticker("BTCUSDT").last
    above, below = round(price * 1.002, 2), round(price * 0.998, 2)
    alert = bot.watch(binance, "BTCUSDT", above=above, below=below)
except BrokerError as exc:
    sys.exit(f"  Binance: {exc}")
print(f"BTCUSDT is at {price:,.2f}: watching for {below:,.2f} and {above:,.2f} "
      "(Ctrl+C to stop).")

# ── Until it fires ────────────────────────────────────────────────────
bot.wait()        # returns once no alert is armed, or on Ctrl+C, which cancels it
print(f"Done: the alert {alert.state}" + (f", at {alert.last_price:,.2f}." if alert.fired
                                         else "."))
