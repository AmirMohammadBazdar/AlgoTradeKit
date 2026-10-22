"""
Paper-trade a MACD crossover on live BTCUSDT 1m candles, and command it from Telegram:
/status, /positions, /chart and /stop, from your phone.

    read -rs TELEGRAM_BOT_TOKEN && export TELEGRAM_BOT_TOKEN   # paste the token; no echo
    export TELEGRAM_CHAT_ID=123456789                          # your own chat with the bot
    python examples/08_telegram_commands.py                    # /stop or Ctrl+C to stop

What it needs:

* ``TELEGRAM_BOT_TOKEN``: a bot made with @BotFather.  Open the bot in Telegram and
  press **Start** first, because a bot cannot write to someone who never did.
* ``TELEGRAM_CHAT_ID``: where the session's messages go.  When it is your private chat
  with the bot, it is also your Telegram id, and so the one person who may command it.
* ``TELEGRAM_COMMANDER_ID`` (optional): your Telegram id, when ``TELEGRAM_CHAT_ID`` is a
  group or a channel.  ``python examples/04_telegram_messages.py`` with the token alone
  lists the chats that wrote to the bot, your private chat with its id among them.
* ``TELEGRAM_PROXY`` (optional): ``http://127.0.0.1:10809``, or
  ``socks5h://127.0.0.1:10808`` after ``pip install "AlgoTradeKit[notify]"``, where
  Telegram is blocked.

No order is ever placed: ``run_live`` paper-trades on Binance's public spot candles,
with no key.  The bot answers only its commander; anyone else's command is refused.
Each answer is a reply to its command.  /stop ends the session the way Ctrl+C does —
paper trading stops at once, where live trading (``Trader``) asks for «/stop CONFIRM»
first.  /chart sends a picture, which needs the ``[notify]`` extra and its browser:
``pip install "AlgoTradeKit[notify]"``, then ``playwright install chromium``.
"""
from __future__ import annotations

import os
import sys

from AlgoTradeKit.broker import Broker
from AlgoTradeKit.notify import NotifyError, TelegramChat, TelegramNotifier
from AlgoTradeKit.strategy import MACDCrossoverStrategy
from AlgoTradeKit.trader import TraderConfig, run_live

token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
commander = os.environ.get("TELEGRAM_COMMANDER_ID", "").strip()
proxy = os.environ.get("TELEGRAM_PROXY", "").strip() or None

# ── What to set ───────────────────────────────────────────────────────
if not token or not chat:
    print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to run this:\n"
          "    read -rs TELEGRAM_BOT_TOKEN && export TELEGRAM_BOT_TOKEN\n"
          "    export TELEGRAM_CHAT_ID=<your own chat's id>\n"
          "examples/04_telegram_messages.py with the token alone lists the chat ids.")
    sys.exit(0)
if not commander and chat.isdigit():   # a person's chat: its id is the person's
    commander = chat
if not commander:
    print("TELEGRAM_CHAT_ID is a group or a channel — set TELEGRAM_COMMANDER_ID to your own\n"
          "Telegram id: the id of your private chat with the bot, which\n"
          "examples/04_telegram_messages.py with the token alone lists.")
    sys.exit(0)

# ── The notifier, listening to its commander ──────────────────────────
# Creating it checks the setup, as in examples/04.  With commanders= it also
# listens, on a thread of its own: commands sent while nothing listened are
# skipped, and trading never waits on any of it.
try:
    bot = TelegramNotifier(
        token,
        {"me": TelegramChat(chat, events={"start", "signal", "close", "stop"})},
        proxy=proxy,
        commanders=[commander],
    )
except NotifyError as exc:
    sys.exit(f"  {exc}")

# ── Paper trading ─────────────────────────────────────────────────────
# As examples/05: the take profit is 2R, a MACD reversal closes the position
# early, and the paper wallet takes about one BTC only with leverage.
config = TraderConfig(
    symbol="BTCUSDT",
    min_candles=200,
    leverage=20.0,
    tp_mode="fixed_rr",
    tp_rr=2.0,
    force_close_on_exit_signal=True,
    notifiers=[bot],
)
print("Send /help to your bot — /stop or Ctrl+C ends this.")
run_live(
    strategy=MACDCrossoverStrategy(timeframe="1m"),
    broker=Broker("binance-spot"),            # public market data, no key
    config=config,
)
# The stop message is out by now: run_live closes the notifier before it returns.
