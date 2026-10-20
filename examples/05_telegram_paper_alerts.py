"""
Paper-trade a MACD crossover on live BTCUSDT 1m candles, and get its signals and its
closes in Telegram, with a message when it starts and one when it stops.

    read -rs TELEGRAM_BOT_TOKEN && export TELEGRAM_BOT_TOKEN   # paste the token; no echo
    export TELEGRAM_CHAT_ID=123456789                          # your own chat with the bot
    python examples/05_telegram_paper_alerts.py                # Ctrl+C to stop

What it needs:

* ``TELEGRAM_BOT_TOKEN``: a bot made with @BotFather.  Open the bot in Telegram and
  press **Start** first, because a bot cannot write to someone who never did.
* ``TELEGRAM_CHAT_ID``: where to send.  ``python examples/04_telegram_messages.py``
  with the token alone lists the chats that wrote to the bot, with their ids.
* ``TELEGRAM_PROXY`` (optional): ``http://127.0.0.1:10809``, or
  ``socks5h://127.0.0.1:10808`` after ``pip install "AlgoTradeKit[notify]"``, where
  Telegram is blocked.

No order is ever placed.  ``run_live`` paper-trades on Binance's public spot
candles, with no key: the positions are the simulator's, and shorts are as good as
longs.  (``Broker("binance-futures")`` works the same where its WebSocket is
reachable.)

The chat takes four kinds of message — the start, each signal, each close and the
stop — so a quiet minute still shows the script runs, and each close replies to the
signal it ends.  Every other kind (the open, TP levels, errors…) stays out of this
chat; a chat without ``events=`` gets every kind but the trailing stop's moves.
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
proxy = os.environ.get("TELEGRAM_PROXY", "").strip() or None

# ── What to set ───────────────────────────────────────────────────────
if not token or not chat:
    print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to run this:\n"
          "    read -rs TELEGRAM_BOT_TOKEN && export TELEGRAM_BOT_TOKEN\n"
          "    export TELEGRAM_CHAT_ID=<your own chat's id>\n"
          "examples/04_telegram_messages.py with the token alone lists the chat ids.")
    sys.exit(0)

# ── The notifier ──────────────────────────────────────────────────────
# Creating it checks the setup: a wrong token, or a person who never pressed
# Start, raises here and says what to do.  Telegram unreachable prints one line,
# and the messages wait in the queue: trading never waits on Telegram.
try:
    bot = TelegramNotifier(
        token,
        {"me": TelegramChat(chat, events={"start", "signal", "close", "stop"})},
        proxy=proxy,
    )
except NotifyError as exc:
    sys.exit(f"  {exc}")

# ── Paper trading ─────────────────────────────────────────────────────
# TraderConfig(notifiers=[bot]) is all it takes: run_live sends the pair's events
# through the notifier, each to the chats whose filter lets it through.  The take
# profit is 2R, and a MACD reversal closes the position early.  On 1m candles the
# stop sits about 0.1% away, so risking 1% of the $10,000 paper wallet takes about
# one BTC: the wallet holds that only with leverage, and without it no trade opens.
config = TraderConfig(
    symbol="BTCUSDT",
    min_candles=200,
    leverage=20.0,
    tp_mode="fixed_rr",
    tp_rr=2.0,
    force_close_on_exit_signal=True,
    notifiers=[bot],
)
run_live(
    strategy=MACDCrossoverStrategy(timeframe="1m"),
    broker=Broker("binance-spot"),            # public market data, no key
    config=config,
)
# The stop message is out by now: run_live closes the notifier before it returns.
