"""
Send a text and a chart picture to Discord, by e-mail and to a webhook of your own: to
each backend whose variables are set.

    export DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...   # a channel's webhook
    export SMTP_HOST=smtp.gmail.com SMTP_USER=you@gmail.com EMAIL_TO=you@gmail.com
    read -rs SMTP_PASSWORD && export SMTP_PASSWORD                    # paste it; no echo
    export WEBHOOK_URL=https://example.com/hook                       # and WEBHOOK_SECRET
    python examples/09_other_backends.py

What it needs, for each backend you want:

* Discord: ``DISCORD_WEBHOOK_URL``, from a channel's settings: Integrations, Webhooks, New
  Webhook, Copy Webhook URL.  The URL holds the webhook's token: keep it as a password.
* E-mail: ``SMTP_HOST``, ``SMTP_USER``, ``SMTP_PASSWORD`` and ``EMAIL_TO``, and
  ``SMTP_PORT`` when it is not 587 (465 takes SSL).  Gmail takes an app password, made in
  your Google account once 2-Step Verification is on, not the account's own.
* A webhook: ``WEBHOOK_URL``, and ``WEBHOOK_SECRET`` to sign each request with an HMAC.

The text is the «{header}» line of the Telegram demos, with BTCUSDT's last price from
Binance's public ticker (no key).  Each backend shows it its own way: Markdown on Discord,
an HTML e-mail, and JSON at the webhook.  The picture needs the ``[notify]`` extra and its
browser: ``pip install "AlgoTradeKit[notify]"``, then ``playwright install chromium``;
without them the text goes alone.
"""
from __future__ import annotations

import os
import sys
import time

from AlgoTradeKit.broker import Broker
from AlgoTradeKit.notify import DiscordNotifier, EmailNotifier, NotifyError, WebhookNotifier


def env(name: str) -> str:
    return os.environ.get(name, "").strip()


# ── The backends whose variables are set ──────────────────────────────
makers = {}
if env("DISCORD_WEBHOOK_URL"):
    makers["Discord"] = lambda: DiscordNotifier({"channel": env("DISCORD_WEBHOOK_URL")})
if env("SMTP_HOST") and env("SMTP_USER") and env("SMTP_PASSWORD") and env("EMAIL_TO"):
    makers["e-mail"] = lambda: EmailNotifier(
        env("SMTP_HOST"), {"me": env("EMAIL_TO")}, port=int(env("SMTP_PORT") or 587),
        username=env("SMTP_USER"), password=env("SMTP_PASSWORD"),
    )
if env("WEBHOOK_URL"):
    makers["webhook"] = lambda: WebhookNotifier({"hook": env("WEBHOOK_URL")},
                                                secret=env("WEBHOOK_SECRET") or None)
if not makers:
    print("Set the variables of at least one backend to run this:\n"
          "    export DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...\n"
          "    export SMTP_HOST=smtp.gmail.com SMTP_USER=you@gmail.com EMAIL_TO=you@gmail.com\n"
          "    read -rs SMTP_PASSWORD && export SMTP_PASSWORD\n"
          "    export WEBHOOK_URL=https://example.com/hook")
    sys.exit(0)

binance = Broker("binance-spot")                  # public market data, no key
try:
    binance.get_ticker("BTCUSDT")
    text, symbol, broker = "{header}\nAlgoTradeKit, through {backend}", "BTCUSDT", binance
except Exception:  # noqa: BLE001 — without Binance the text says the time alone
    text, symbol, broker = "🕑 {time}\nAlgoTradeKit, through {backend}", None, None

now_ms = int(time.time() * 1000)
for name, make in makers.items():
    # ── Making one checks it, as the Telegram notifier does: a Discord webhook that
    # does not exist, or a login the SMTP server refuses, raises here.
    try:
        notifier = make()
    except NotifyError as exc:
        print(f"{name}: {exc}")
        continue
    with notifier:            # closing it delivers what is still queued
        try:
            sent = notifier.send_text(text.replace("{backend}", name), symbol=symbol,
                                      broker=broker)
            print(f"{name}: text sent, {next(iter(sent.values()))}.")
            sent = notifier.send_chart(
                broker=binance, symbol="BTCUSDT", timeframe="15m",
                start=now_ms - 6 * 3_600_000, end=now_ms,
                indicators=[{"kind": "ema", "period": 20}],
                caption="<b>BTCUSDT · 15m</b> · the last six hours with EMA(20)\n🕑 {time}",
            )
            print(f"{name}: picture sent, {next(iter(sent.values()))}.")
        except (ImportError, RuntimeError) as exc:    # no picture engine, or no browser
            print(f"{name}: the text went alone — {exc}")
        except NotifyError as exc:                    # the service refused it
            print(f"{name}: {exc}")
