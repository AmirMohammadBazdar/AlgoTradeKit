"""
Send text to Telegram: a plain text, one with a header line, and a silent one.

    read -rs TELEGRAM_BOT_TOKEN && export TELEGRAM_BOT_TOKEN   # paste the token; no echo
    export TELEGRAM_CHAT_ID=123456789                          # your own chat with the bot
    python examples/04_telegram_messages.py

What it needs:

* ``TELEGRAM_BOT_TOKEN``: a bot made with @BotFather.  Open the bot in Telegram and
  press **Start** first, because a bot cannot write to someone who never did.
* ``TELEGRAM_CHAT_ID``: where to send.  Without it the script lists the chats that
  wrote to the bot in the last 24 hours, with their ids, and stops.
* ``TELEGRAM_PROXY`` (optional): ``http://127.0.0.1:10809``, or
  ``socks5h://127.0.0.1:10808`` after ``pip install "AlgoTradeKit[notify]"``, where
  Telegram is blocked.

The header's price is BTCUSDT's last price from Binance's public ticker, with no key.
Without the network to Binance the script sends the time alone, and says so.
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
if not token:
    print("Set TELEGRAM_BOT_TOKEN to run this: the token @BotFather gave your bot.\n"
          "    read -rs TELEGRAM_BOT_TOKEN && export TELEGRAM_BOT_TOKEN\n"
          "Then set TELEGRAM_CHAT_ID to your own chat's id; without it, this script\n"
          "lists the chats your bot can see.")
    sys.exit(0)
if not chat:
    print("TELEGRAM_CHAT_ID is not set. The chats your bot can see:\n")
    try:
        TelegramNotifier.find_chats(token, proxy=proxy)
    except NotifyError as exc:              # a wrong token, no network, no PySocks
        sys.exit(f"  {exc}")
    print("\nexport TELEGRAM_CHAT_ID=<your own chat's id>, then run this again.")
    sys.exit(0)

# ── The notifier ──────────────────────────────────────────────────────
# Creating it checks the setup. A wrong token, or a person who never pressed
# Start, raises here and says what to do. If Telegram cannot be reached, it
# prints one line and the messages wait in the queue.
try:
    bot = TelegramNotifier(token, {"me": chat}, proxy=proxy)
except NotifyError as exc:
    sys.exit(f"  {exc}")

# ── The messages ──────────────────────────────────────────────────────
# Each call waits until Telegram has the message and returns {chat name: id}.
# A refusal raises at once; Telegram unreachable for 21 s raises too, and that
# message is withdrawn.
binance = Broker("binance-futures")         # public market data, no key
try:
    # 1. A plain text, with HTML tags of your own.
    ids = bot.send_text("Hello from AlgoTradeKit 👋 This is a plain text.")
    print(f"  plain text   → message {ids['me']}")

    # 2. A header line. {header} is «🕑 {time} · {symbol} {price}»; the price is
    #    asked of the broker now, so it needs broker= and symbol=.
    try:
        ids = bot.send_text("{header}\n<b>Watching</b> the 64k level.",
                            symbol="BTCUSDT", broker=binance)
        print(f"  header text  → message {ids['me']}")
    except BrokerError as exc:
        print(f"  no price from Binance ({type(exc).__name__}): sending the time alone")
        ids = bot.send_text("🕑 {time}\n<b>Watching</b> the 64k level. (No price: "
                            "Binance could not be reached.)")
        print(f"  time only    → message {ids['me']}")

    # 3. A silent one: it arrives without a sound.
    ids = bot.send_text("This one arrives without a sound.", silent=True)
    print(f"  silent text  → message {ids['me']}")
except NotifyError as exc:
    sys.exit(f"  {exc}")
finally:
    bot.close()
