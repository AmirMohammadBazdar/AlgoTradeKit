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
    Alert               A price alert, as ``watch()`` returns it: its condition, its
                        state, how often it fired; ``cancel()`` ends it.
    DiscordNotifier     Discord channels, through their webhooks (v1.2.0): the same
                        messages, in Discord's Markdown; a trailing stop edits.
    DiscordWebhook      One webhook, with a thread, no sound, and filters.
    EmailNotifier       E-mail through your SMTP server (v1.2.0): HTML with pictures
                        inline, trade messages in a digest every 15 minutes.
    EmailRecipient      One address, with filters.
    WebhookNotifier     JSON posted to your URLs (v1.2.0): the text and the event's raw
                        fields, signed with ``secret=``.
    WebhookTarget       One URL, with filters and headers.

Errors
    NotifyError         The base error of this module.
    TelegramError       Telegram refused, or could not be reached for a call that
                        waited.
    DiscordError / EmailError / WebhookError
                        The same, for Discord, the SMTP server and a webhook.

Constants
    KIND_TEXT           The kind of a ``send_text`` message.
    KIND_SIGNAL / KIND_EXIT_SIGNAL / KIND_OPEN / KIND_SL_MOVE / KIND_RISK_FREE /
    KIND_TP_LEVEL / KIND_CLOSE / KIND_DAILY_LOSS / KIND_RECONCILE / KIND_ERROR
                        A trading session's events, named as its event types.
    KIND_MESSAGE        A strategy's own message (``self.notify(...)``).
    KIND_START / KIND_STOP
                        A trading session's start and stop.
    KIND_DAILY_SUMMARY  The day's summary, at 00:00 in the notifier's zone.
    KIND_ALERT          A price alert's message.

A chat's ``events`` picks its kinds; by default it gets all of them but
``sl_move``, which edits the trade's first message instead where a chat asks for
it.

Placeholders in a text: ``{time}``, ``{date}``, ``{symbol}``, ``{price}`` (the last
price, from ``broker.get_ticker(symbol)``) and ``{header}``, a ready-made first
line, «🕑 {time} · {symbol} {price}».  Times are UTC unless the notifier is given
a ``timezone``.  ``templates={kind: text}`` replaces a kind's default text.

Trading events arrive through ``TraderConfig(notifiers=[bot])``, for ``run_live``
and ``Trader`` alike.

Price alerts (v1.2.0)
    ``watch(broker, "BTCUSDT", above=65_000)`` sends «🔔 BTCUSDT crossed above 65,000 ·
    now 65,012.5» when the last trade crosses the level, with no strategy and no trading;
    ``below=``, ``move_pct=-3, window="1h"``, ``on_close="1h"``, ``repeat=True`` and
    ``image=True`` shape it.  ``alerts()`` lists them, ``cancel()`` ends one, and
    ``wait()`` keeps a script alive until none is armed.

Pictures (v1.2.0)
    ``send_chart(chart, start, end)`` sends a picture of a ``Chart`` you built, or
    with ``broker=``, ``symbol=`` and ``timeframe=`` of a venue's candles between
    two datetimes; ``send_photo(png)`` sends one you made; ``send_report(report)``
    sends a backtest's figures, its balance curve and its HTML report.
    ``TelegramNotifier(…, images={"open", "close"})`` puts a picture of the pair's
    chart on those kinds of trade message, its text as the caption.  A headless
    Chromium photographs the library's own chart page, on the notifier's thread.

Commands (v1.2.0)
    ``TelegramNotifier(…, commanders=[123456789])`` listens to the people it names, in
    any chat they write in, and answers /status (what is trading, since when, today's
    net, the open positions), /positions, /chart, /stop and /help, as replies.  /stop
    ends the ``run_live`` session or the ``Trader`` it serves, as Ctrl+C does; live
    trading asks for «/stop CONFIRM» first.  Commands sent while nothing listened are
    skipped, and anyone else's are refused.

Other backends (v1.2.0)
    ``DiscordNotifier``, ``EmailNotifier`` and ``WebhookNotifier`` take the same kinds,
    filters, templates, pictures and queue, and work wherever a ``TelegramNotifier`` does:
    ``TraderConfig(notifiers=[…])``, ``send_text``, ``send_chart``, ``send_report`` and
    ``watch``.  What a service cannot do degrades: Discord cannot reply, e-mail cannot
    edit, a webhook does neither.  Commands stay Telegram's.

The module imports no other part of AlgoTradeKit when it is imported: a broker
arrives as an object, and the chart and the report are loaded only to make a
picture.  Text needs nothing beyond the core install; a SOCKS proxy and pictures
need ``pip install "AlgoTradeKit[notify]"``, and pictures then, once,
``playwright install chromium`` (on Linux ``playwright install --with-deps
chromium``).
"""

from ._core import (
    KIND_ALERT,
    KIND_CLOSE,
    KIND_DAILY_LOSS,
    KIND_DAILY_SUMMARY,
    KIND_ERROR,
    KIND_EXIT_SIGNAL,
    KIND_MESSAGE,
    KIND_OPEN,
    KIND_RECONCILE,
    KIND_RISK_FREE,
    KIND_SIGNAL,
    KIND_SL_MOVE,
    KIND_START,
    KIND_STOP,
    KIND_TEXT,
    KIND_TP_LEVEL,
    NotifyError,
)
from ._telegram import TelegramChat, TelegramChatInfo, TelegramError, TelegramNotifier

__all__ = [
    "TelegramNotifier",
    "TelegramChat",
    "TelegramChatInfo",
    "TelegramError",
    "NotifyError",
    "KIND_TEXT",
    "KIND_SIGNAL",
    "KIND_EXIT_SIGNAL",
    "KIND_OPEN",
    "KIND_SL_MOVE",
    "KIND_RISK_FREE",
    "KIND_TP_LEVEL",
    "KIND_CLOSE",
    "KIND_DAILY_LOSS",
    "KIND_RECONCILE",
    "KIND_ERROR",
    "KIND_MESSAGE",
    "KIND_START",
    "KIND_STOP",
    "KIND_DAILY_SUMMARY",
    "KIND_ALERT",
    "Alert",
    "DiscordNotifier",
    "DiscordWebhook",
    "DiscordError",
    "EmailNotifier",
    "EmailRecipient",
    "EmailError",
    "WebhookNotifier",
    "WebhookTarget",
    "WebhookError",
]

#: The names that live in a module of their own, loaded with their first use.
_LAZY = {
    "Alert": "_alerts",
    "DiscordNotifier": "_discord", "DiscordWebhook": "_discord", "DiscordError": "_discord",
    "EmailNotifier": "_email", "EmailRecipient": "_email", "EmailError": "_email",
    "WebhookNotifier": "_webhook", "WebhookTarget": "_webhook", "WebhookError": "_webhook",
}


def __getattr__(name: str):
    """``Alert`` and the other backends live in modules of their own, loaded with their
    first use: importing the module loads only what a text to Telegram needs."""
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(f".{module}", __name__), name)
