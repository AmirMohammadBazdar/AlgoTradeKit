"""
AlgoTradeKit.notify._discord
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The Discord backend (v1.2.0, E8): :class:`DiscordNotifier` and :class:`DiscordWebhook`.

A Discord webhook — a channel's settings, Integrations, Webhooks, New Webhook — is the whole
setup: its URL, and no application or bot (Q6.2 a).  It posts with ``?wait=true``, so the
message comes back, and it edits its own messages with ``PATCH``: a trailing stop edits the
trade's first message, as on Telegram (E5).  A webhook cannot reply, so a trade's later
messages go as plain ones (Q6.5 a).

Discord's limits shape it (``docs.discord.com/developers``, read on 2026-10-22):

* a message holds 2,000 characters; a longer text goes as several messages, and a picture's
  caption is its message's content, so it holds 2,000 too;
* a file is at most 20 MiB, Discord's default;
* its rate-limit page gives no number for a webhook: each gets one message a second, and a
  429 waits its ``retry_after``;
* a webhook that answers 404 is gone: it is not tried again.

Templates are the library's rich-text subset.  Each message is turned into Discord's
Markdown — ``<b>`` to ``**``, ``<i>`` to ``*``, ``<u>`` to ``__``, ``<s>`` to ``~~``,
``<code>`` to a backtick, ``<pre>`` to a code block, ``<a href>`` to ``[text](url)`` — and
every other character Markdown reads gets a backslash, so text shows as written.  No value
ever pings anyone: ``allowed_mentions`` allows none.

The URL holds the webhook's token, a password: lines show ``…/webhooks/<id>/***``.
"""

from __future__ import annotations

import html
import json
import mimetypes
import re
from collections.abc import Mapping
from dataclasses import KW_ONLY, dataclass
from typing import Any
from urllib.parse import urlsplit

import requests

from . import _template
from ._core import Notifier, NotifyError, _Destination, _Media, _Outcome, check_kinds
from ._telegram import _name_set, _network_reason

#: At most one message a second to a webhook.
_PACE = ((1, 1.0),)
#: The path of a webhook's URL: ``/api/webhooks/<id>/<token>``.
_WEBHOOK_PATH = re.compile(r"/api(?:/v\d+)?/webhooks/(\d+)/([A-Za-z0-9_.-]+)/?\Z")
#: ``flags`` for a message without sound.
SUPPRESS_NOTIFICATIONS = 1 << 12
_NO_MENTIONS = {"parse": []}
#: The characters Discord's Markdown reads anywhere, which a text's own get a backslash for;
#: and those it reads only at a line's start (a header, a quote, a list).
_SPECIAL = frozenset("\\*_~`|[]")
_LINE_START = frozenset("#>-")
_TOKEN = re.compile(r"<[^<>]*>|&#?[0-9A-Za-z]+;")
_TAG = re.compile(r"<(/?)\s*([A-Za-z][\w-]*)([^<>]*)>")
_HREF = re.compile(r"""href\s*=\s*(["'])(.*?)\1""", re.IGNORECASE | re.DOTALL)
_MARKS = {"b": "**", "strong": "**", "i": "*", "em": "*", "u": "__", "ins": "__",
          "s": "~~", "strike": "~~", "del": "~~", "tg-spoiler": "||"}
_HOW_TO_COPY = "copy its URL again from the channel's settings: Integrations, Webhooks"


class DiscordError(NotifyError):
    """Discord refused a webhook or a message, or could not be reached for a call that
    waited."""


def _utf16(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _escaped(text: str, at_start: bool) -> str:
    """*text* with a backslash before each character Markdown would read; *at_start* says
    whether it begins a line."""
    out = []
    for ch in text:
        if ch in _SPECIAL or (at_start and ch in _LINE_START):
            out.append(f"\\{ch}")
        else:
            out.append(ch)
        if ch == "\n":
            at_start = True
        elif not ch.isspace():
            at_start = False
    return "".join(out)


def markdown(text: str) -> str:
    """The rich-text subset of *text* as Discord's Markdown: tags turned into marks, entities
    read, and a backslash before each character Markdown would read, outside code."""
    out: list[str] = []
    links: list[str] = []
    code = pre = 0
    pos = 0

    def put(plain: str) -> None:
        if code or pre:
            out.append(plain)
            return
        written = "".join(out)
        start = not written or written.endswith("\n")
        out.append(_escaped(plain, start))

    for match in _TOKEN.finditer(text):
        put(text[pos:match.start()])
        pos = match.end()
        token = match.group()
        if token.startswith("&"):
            put(html.unescape(token))
            continue
        tag = _TAG.fullmatch(token)
        if tag is None:
            put(token)
            continue
        closing, name = tag.group(1) == "/", tag.group(2).lower()
        if name == "pre":
            pre += -1 if closing else 1
            out.append("\n```" if closing else "```\n")
        elif name == "code":
            if not pre:
                out.append("`")
            code += -1 if closing else 1
        elif name == "a":
            if closing:
                out.append(f"]({links.pop() if links else ''})")
            else:
                href = _HREF.search(tag.group(3))
                links.append(html.unescape(href.group(2)) if href else "")
                out.append("[")
        elif name in _MARKS:
            out.append(_MARKS[name])
    put(text[pos:])
    return "".join(out)


def _plain_cost(text: str) -> int:
    """Plain text's cost, as if each character Markdown may read were escaped."""
    return sum(2 if ch in _SPECIAL or ch in _LINE_START else _utf16(ch) for ch in text)


def _cost(token: str) -> int:
    """What one token of the rich text counts once it is Markdown, in UTF-16 units — a
    link's address at its opening tag, so the parts of a cut text add up."""
    if token.startswith("&"):
        return _plain_cost(html.unescape(token))
    tag = _TAG.fullmatch(token)
    if tag is None:
        return _plain_cost(token)
    closing, name = tag.group(1) == "/", tag.group(2).lower()
    if name == "pre":
        return 4
    if name == "code":
        return 1
    if name == "a":
        if closing:
            return 0
        href = _HREF.search(tag.group(3))
        return 4 + (_utf16(html.unescape(href.group(2))) if href else 0)
    return len(_MARKS.get(name, ""))


def _check_url(value: Any) -> tuple[str, str, str]:
    """A webhook's URL, its id and its token."""
    if not isinstance(value, str):
        raise TypeError(f"a Discord webhook is its URL, a string, got {type(value).__name__}")
    url = value.strip()
    parts = urlsplit(url)
    match = _WEBHOOK_PATH.search(parts.path) if parts.scheme in ("http", "https") else None
    if match is None or not parts.netloc:
        raise ValueError("this is not a Discord webhook's URL (https://discord.com/api/webhooks/"
                         f"<id>/<token>) — {_HOW_TO_COPY}")
    return url.rstrip("/"), match.group(1), match.group(2)


@dataclass(frozen=True)
class DiscordWebhook:
    """One Discord channel, by its webhook, with its own settings and filters.

    url
        The webhook's URL, from the channel's settings: Integrations, Webhooks, Copy
        Webhook URL.  It holds a token: keep it as a password.
    thread_id
        Post into this thread of the channel.
    silent
        Send without a notification.
    events
        The kinds this channel gets, as a set, or ``None`` for every kind except
        ``sl_move``.  An empty set: only what names it in ``to=``.
    symbols
        The symbols it gets, as a set compared in capitals, or ``None`` for all.
    """

    url: str
    _: KW_ONLY
    thread_id: int | None = None
    silent: bool = False
    events: frozenset[str] | None = None
    symbols: frozenset[str] | None = None

    def __post_init__(self) -> None:
        url, _webhook_id, _token = _check_url(self.url)
        object.__setattr__(self, "url", url)
        thread = self.thread_id
        if isinstance(thread, str) and thread.strip().isdigit():
            thread = int(thread.strip())
        if thread is not None and (
            isinstance(thread, bool) or not isinstance(thread, int) or thread <= 0
        ):
            raise ValueError(f"thread_id must be a thread's id (an int above 0), got {thread!r}")
        object.__setattr__(self, "thread_id", thread)
        if not isinstance(self.silent, bool):
            raise TypeError(f"silent must be True or False, got {self.silent!r}")
        events = _name_set(self.events, "events", '{"signal", "close"}')
        if events is not None:
            check_kinds(events)
        object.__setattr__(self, "events", events)
        symbols = _name_set(self.symbols, "symbols", '{"BTCUSDT"}')
        object.__setattr__(
            self, "symbols", None if symbols is None else frozenset(s.upper() for s in symbols)
        )

    def __repr__(self) -> str:   # the URL holds the token
        return f"DiscordWebhook(webhook {self.webhook_id})"

    @property
    def webhook_id(self) -> str:
        return _check_url(self.url)[1]

    @property
    def token(self) -> str:
        return _check_url(self.url)[2]


def _webhook_specs(webhooks: Any) -> dict[str, DiscordWebhook]:
    """``webhooks`` as a dict of names: a dict keeps its keys; a webhook given on its own is
    named by its id."""
    def made(value: Any) -> DiscordWebhook:
        return value if isinstance(value, DiscordWebhook) else DiscordWebhook(value)

    if isinstance(webhooks, Mapping):
        items = []
        for name, hook in webhooks.items():
            if not isinstance(name, str) or not name.strip():
                raise TypeError(f"webhook names must be non-empty strings, got {name!r}")
            items.append((name, made(hook)))
    else:
        if isinstance(webhooks, (DiscordWebhook, str)):
            webhooks = [webhooks]
        elif not isinstance(webhooks, (list, tuple)):
            raise TypeError('webhooks must be a dict such as {"signals": "https://discord.com/'
                            f'api/webhooks/…"}}, one webhook, or a list, got '
                            f"{type(webhooks).__name__}")
        items = [(hook.webhook_id, hook) for hook in map(made, webhooks)]
    if not items:
        raise ValueError('webhooks is empty — give at least one, such as {"signals": "https://'
                         'discord.com/api/webhooks/…"}')
    specs: dict[str, DiscordWebhook] = {}
    for name, hook in items:
        if name in specs:
            raise ValueError(f"webhook {name} is given twice")
        specs[name] = hook
    return specs


class DiscordNotifier(Notifier):
    """Send to Discord channels, through their webhooks.

    ::

        from AlgoTradeKit.notify import DiscordNotifier, DiscordWebhook

        discord = DiscordNotifier({
            "signals": "https://discord.com/api/webhooks/1300000000000000000/AbC…",
            "desk": DiscordWebhook("https://discord.com/api/webhooks/…", events={"close"}),
        })
        discord.send_text("{header}\\nwatching the 64k level", symbol="BTCUSDT", broker=binance)

    webhooks
        A dict of names → a webhook's URL or a :class:`DiscordWebhook`, one webhook, or a
        list of them.
    timezone
        ``None`` (UTC) or an IANA name, as for ``TelegramNotifier``.
    check
        ``True`` asks Discord for each webhook now: one that does not exist raises
        :class:`DiscordError`, saying what to do; Discord unreachable prints one line, and
        the messages wait in the queue.
    timeout
        Seconds each request may take.
    templates, images
        As for ``TelegramNotifier``: your own text per kind, written in the same rich-text
        subset, and the kinds of trade message that carry a picture.

    The rest is the same as ``TelegramNotifier``'s, but for replies: a webhook cannot
    reply, so a trade's later messages go as plain ones; a trailing stop still edits the
    trade's first message.
    """

    _service = "Discord"
    _tag = "discord"
    _error = DiscordError
    _max_length = 2000
    _max_caption = 2000
    _can_reply = False
    _can_edit = True
    _can_send_media = True
    _max_photo_bytes = 20 * 1024 * 1024
    _max_document_bytes = 20 * 1024 * 1024
    _max_photo_sides = 1_000_000          # Discord states no such limit
    _max_photo_ratio = 1e9
    #: A picture's upload gets this much longer than a text's request.
    _UPLOAD_EXTRA = 30.0

    def __init__(
        self,
        webhooks: Any,
        *,
        timezone: str | None = None,
        check: bool = True,
        timeout: float = 10.0,
        templates: Mapping[str, str | None] | None = None,
        images: Any = None,
    ) -> None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError(f"timeout must be a number of seconds above 0, got {timeout!r}")
        self._timeout = float(timeout)
        zone = _template.parse_zone(timezone)
        specs = _webhook_specs(webhooks)
        self._tokens = [hook.token for hook in specs.values()]
        self._session = requests.Session()
        destinations = {
            name: _Destination(
                name, hook, events=hook.events, symbols=hook.symbols, silent=hook.silent,
                label=f"{name} (webhook {hook.webhook_id})", pace_key=hook.webhook_id,
                pace_rules=_PACE,
            )
            for name, hook in specs.items()
        }
        super().__init__(destinations, zone=zone, rich=True, templates=templates, images=images)
        if check:
            self._check()
            if self._images:   # what cannot fix itself is found now (Q3.7 a)
                from ..visual import _snapshot

                _snapshot.check_engine()

    def __repr__(self) -> str:
        return f"DiscordNotifier(webhooks={list(self._destinations)!r})"

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------

    def _mask(self, text: str) -> str:
        for token in self._tokens:
            text = text.replace(token, "***")
        return text

    @staticmethod
    def _measure(text: str) -> int:
        """What *text* counts against 2,000 once it is Markdown, in UTF-16 units."""
        total = pos = 0
        for match in _TOKEN.finditer(text):
            total += _plain_cost(text[pos:match.start()]) + _cost(match.group())
            pos = match.end()
        return total + _plain_cost(text[pos:])

    def _request(self, method: str, url: str, *, timeout: float | None = None,
                 **kwargs: Any) -> tuple[int, Any, Mapping[str, str], str]:
        """One request: its status (0 when nothing came back), its JSON, its headers and a
        reason.  Never raises, and never says the URL, which holds the token."""
        wait = self._timeout if timeout is None else timeout
        try:
            response = self._session.request(method, url, timeout=wait, **kwargs)
        except requests.exceptions.RequestException as exc:
            return 0, None, {}, _network_reason(exc, wait)
        try:
            body = response.json()
        except ValueError:
            body = None
        said = body.get("message") if isinstance(body, dict) else None
        code = body.get("code") if isinstance(body, dict) else None
        reason = f"{response.status_code} {said or response.reason or ''}".strip()
        if code:
            reason += f" (code {code})"
        return response.status_code, body, response.headers, reason

    def _outcome(self, status: int, body: Any, headers: Mapping[str, str], reason: str, *,
                 message_id: Any = None) -> _Outcome:
        if status == 0:
            return _Outcome("unreachable", reason=reason)
        if 200 <= status < 300:
            if message_id is None and isinstance(body, dict):
                message_id = body.get("id")
            return _Outcome("sent", message_id=message_id)
        if status == 429:
            retry = body.get("retry_after") if isinstance(body, dict) else None
            try:
                retry_after = float(retry if retry is not None else headers.get("Retry-After") or 1)
            except (TypeError, ValueError):
                retry_after = 1.0
            return _Outcome("wait", reason=reason, retry_after=retry_after)
        if status >= 500:
            return _Outcome("server", reason=reason)
        return _Outcome("refused", reason=reason)   # 404: the webhook is gone; never again

    @staticmethod
    def _params(hook: DiscordWebhook, **more: Any) -> dict[str, Any]:
        params = dict(more)
        if hook.thread_id is not None:
            params["thread_id"] = hook.thread_id
        return params

    @staticmethod
    def _content(text: str | None, silent: bool) -> dict[str, Any]:
        body: dict[str, Any] = {"allowed_mentions": _NO_MENTIONS}
        if text:
            body["content"] = markdown(text)
        if silent:
            body["flags"] = SUPPRESS_NOTIFICATIONS
        return body

    # ------------------------------------------------------------------
    # The core's hooks
    # ------------------------------------------------------------------

    def _deliver(
        self, dest: _Destination, text: str, *, silent: bool, reply_to: Any = None
    ) -> _Outcome:
        hook: DiscordWebhook = dest.target
        status, body, headers, reason = self._request(
            "POST", hook.url, params=self._params(hook, wait="true"),
            json=self._content(text, silent),
        )
        return self._outcome(status, body, headers, reason)

    def _edit(self, dest: _Destination, message_id: Any, text: str) -> _Outcome:
        hook: DiscordWebhook = dest.target
        body = {"content": markdown(text), "allowed_mentions": _NO_MENTIONS}
        status, answer, headers, reason = self._request(
            "PATCH", f"{hook.url}/messages/{message_id}", params=self._params(hook), json=body,
        )
        return self._outcome(status, answer, headers, reason, message_id=message_id)

    def _edit_caption(self, dest: _Destination, message_id: Any, text: str) -> _Outcome:
        return self._edit(dest, message_id, text)   # a picture's caption is its content

    def _deliver_media(
        self, dest: _Destination, media: _Media, data: bytes, caption: str | None, *,
        silent: bool, reply_to: Any = None,
    ) -> _Outcome:
        hook: DiscordWebhook = dest.target
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            mime = "image/png"
        elif data[:3] == b"\xff\xd8\xff":
            mime = "image/jpeg"
        else:
            mime = mimetypes.guess_type(media.filename)[0] or "application/octet-stream"
        payload = self._content(caption, silent)
        payload["attachments"] = [{"id": 0, "filename": media.filename}]
        status, body, headers, reason = self._request(
            "POST", hook.url, params=self._params(hook, wait="true"),
            data={"payload_json": json.dumps(payload)},
            files={"files[0]": (media.filename, data, mime)},
            timeout=self._timeout + self._UPLOAD_EXTRA,
        )
        return self._outcome(status, body, headers, reason)

    # ------------------------------------------------------------------
    # The setup check
    # ------------------------------------------------------------------

    def _check(self) -> None:
        """Ask Discord for each webhook: raise for one that does not exist, carry on when
        Discord cannot be reached."""
        for dest in self._destinations.values():
            hook: DiscordWebhook = dest.target
            status, body, _headers, reason = self._request("GET", hook.url)
            if status == 0 or status == 429 or status >= 500:
                self._mark_unreachable(
                    f"Discord unreachable ({reason}) — carrying on; messages wait in the queue")
                return
            if status in (401, 404):
                raise DiscordError(f"Discord webhook {dest.name} does not exist — {_HOW_TO_COPY}")
            if not 200 <= status < 300:
                raise DiscordError(self._mask(f"Discord webhook {dest.name}: Discord answered "
                                              f"{reason}"))
            title = body.get("name") if isinstance(body, dict) else None
            if title:
                dest.label = f"{dest.name} ({title})"
