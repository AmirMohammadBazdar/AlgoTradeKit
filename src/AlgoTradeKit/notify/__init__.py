"""
AlgoTradeKit.notify
~~~~~~~~~~~~~~~~~~~
Notifications (v1.2.0) — send text to Telegram: to people, groups, channels and
forum topics::

    from AlgoTradeKit.notify import TelegramChat, TelegramNotifier

    bot = TelegramNotifier(token, {"me": 123456789, "signals": "@atk_signals"})
    bot.send_text("{time} · {symbol} {price} — watching the 64k level",
                  symbol="BTCUSDT", broker=binance)
    # → «2026-10-14 14:03 UTC · BTCUSDT 64,312.5 — watching the 64k level»

Classes
    TelegramNotifier    One bot, its named chats, and a queue on its own thread:
                        paced to Telegram's limits, retried after errors, waiting
                        through an outage and marking what arrives late.  The
                        setup is checked when it is created; ``find_chats(token)``
                        lists the chat ids the bot can see.
    TelegramChat        A chat with its own settings: a forum topic
                        (``thread_id``), no sound (``silent``), and filters — the
                        kinds it gets (``events``) and its ``symbols``.
    TelegramChatInfo    One chat as ``find_chats`` reports it.

Errors
    NotifyError         The base error of this module.
    TelegramError       Telegram refused, or could not be reached for a call that
                        waited.

Constants
    KIND_TEXT           The kind of a ``send_text`` message.

Placeholders in a text: ``{time}``, ``{date}``, ``{symbol}``, ``{price}`` (the last
price, from ``broker.get_ticker(symbol)``) and ``{header}``, a ready-made first
line, «🕑 {time} · {symbol} {price}».  Times are UTC unless the notifier is given
a ``timezone``.

The module imports no other part of AlgoTradeKit: a broker arrives as an object.
Text needs nothing beyond the core install; a SOCKS proxy needs
``pip install "AlgoTradeKit[notify]"``.
"""

from ._core import KIND_TEXT, NotifyError
from ._telegram import TelegramChat, TelegramChatInfo, TelegramError, TelegramNotifier

__all__ = [
    "TelegramNotifier",
    "TelegramChat",
    "TelegramChatInfo",
    "TelegramError",
    "NotifyError",
    "KIND_TEXT",
]
