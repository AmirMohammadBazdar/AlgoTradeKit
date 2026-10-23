"""
AlgoTradeKit.notify._telegram
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The Telegram backend (v1.2.0): :class:`TelegramNotifier`, :class:`TelegramChat`,
:meth:`TelegramNotifier.find_chats`.

It talks to the Bot API over HTTPS with ``requests``, a handful of calls
(``getMe``, ``getChat``, ``getChatMember``, ``sendMessage``, ``editMessageText``,
``getUpdates``; for pictures and files ``sendPhoto``, ``sendDocument`` and
``editMessageCaption``, uploaded as multipart; for commands ``getWebhookInfo`` and
``setMyCommands``), and no Telegram library.  A trade's messages reply to its first
one (``reply_parameters``, which still sends when that message was deleted), and a
trailing stop edits it.  With ``commanders=`` the bot also listens, on a thread of
its own, and answers commands (``notify/_commands.py``).

Telegram's limits shape it:

* a message holds 4,096 characters, which Telegram counts in UTF-16 units, so an
  emoji counts two; a longer text goes as several messages;
* a picture's caption holds 1,024; a longer one goes as a reply to the picture.  A
  picture is at most 10 MB, with a width + height of at most 10,000, and a file at
  most 50 MB;
* at most one message a second to a chat, and 20 a minute to a group or a channel
  (a chat id below 0, or an ``@name``); a 429 says how long to wait, and is
  waited;
* a person must open the bot and press Start before it can write to them, and a
  channel needs the bot as an administrator allowed to post.

The token is a password: it appears in no line, error or ``repr``.  Every request
carries it in its URL, so a URL in an error is masked as ``bot<id>:***``.
"""

from __future__ import annotations

import importlib.util
import json
import mimetypes
import re
import sys
from collections.abc import Iterable, Mapping
from dataclasses import KW_ONLY, dataclass
from typing import Any
from urllib.parse import urlsplit

import requests

from . import _template
from ._core import Notifier, NotifyError, _Destination, _Media, _Outcome, check_kinds

_API = "https://api.telegram.org"

#: Pacing: at most one message a second to a person; to a group or a channel, also
#: at most 20 a minute.
_CHAT_PACE = ((1, 1.0),)
_GROUP_PACE = ((1, 1.0), (20, 60.0))

#: Seconds ``find_chats`` waits for Telegram.
_FIND_TIMEOUT = 10.0

_PROXY_SCHEMES = ("http", "https", "socks4", "socks4a", "socks5", "socks5h")
_SOCKS_NEEDS = 'a SOCKS proxy needs PySocks: pip install "AlgoTradeKit[notify]"'
_FIND_CHATS = "TelegramNotifier.find_chats(token) lists the chats the bot can see"
_NOBODY = (
    "Nobody has written to the bot in the last 24 hours. Send it a message, add it to "
    "the group, or post in the channel, then run this again."
)

_TOKEN = re.compile(r"\d+:[A-Za-z0-9_-]+")
_NUMERIC = re.compile(r"-?\d+")
_USERNAME = re.compile(r"@[A-Za-z0-9_]+")


class TelegramError(NotifyError):
    """Telegram refused a setting or a message, or could not be reached for a call
    that waited.  ``sent`` and ``failed`` say how each chat of a waiting call fared."""


# ---------------------------------------------------------------------------
# Chats
# ---------------------------------------------------------------------------

def _chat_address(value: Any) -> int | str:
    if isinstance(value, bool):
        raise TypeError(f"chat_id must be an int or a string, got {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if _NUMERIC.fullmatch(text):
            return int(text)
        if _USERNAME.fullmatch(text):
            return text
    raise ValueError(
        'chat_id must be an int, an "@name" or a numeric string such as "-1001234567890", '
        f"got {value!r}"
    )


def _name_set(value: Any, what: str, example: str) -> frozenset[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        raise TypeError(f"{what} must be a set such as {example}, not the string {value!r}")
    try:
        names = frozenset(value)
    except TypeError:
        raise TypeError(f"{what} must be a set such as {example}, got {value!r}") from None
    for name in names:
        if not isinstance(name, str):
            raise TypeError(f"{what} must hold strings, got {name!r}")
    return names


@dataclass(frozen=True)
class TelegramChat:
    """One Telegram destination, with its own settings and filters.

    chat_id
        An ``int`` (a person, a group or a channel, by id), an ``"@name"`` (a public
        channel or group), or a numeric string such as ``"-1001234567890"``, which is
        made an int.  A private channel has no ``@name``: its id is its only address,
        and ``TelegramNotifier.find_chats(token)`` finds it.
    thread_id
        Post into this forum topic of a group.
    silent
        Send without sound.
    events
        The kinds this chat gets, as a set, or ``None`` for every kind except
        ``sl_move``.  An empty set: only what names the chat in ``to=``.  A chat
        that gets ``sl_move`` sees each trade's first message edited as its stop
        moves.
    symbols
        The symbols this chat gets, as a set compared in capitals, or ``None`` for
        all.  A message about no symbol is not kept out.
    """

    chat_id: int | str
    _: KW_ONLY
    thread_id: int | None = None
    silent: bool = False
    events: frozenset[str] | None = None
    symbols: frozenset[str] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "chat_id", _chat_address(self.chat_id))
        thread = self.thread_id
        if isinstance(thread, str) and thread.strip().isdigit():
            thread = int(thread.strip())
        if thread is not None and (
            isinstance(thread, bool) or not isinstance(thread, int) or thread <= 0
        ):
            raise ValueError(
                f"thread_id must be a forum topic's id (an int above 0), got {thread!r}"
            )
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


@dataclass(frozen=True)
class TelegramChatInfo:
    """A chat the bot can see, as :meth:`TelegramNotifier.find_chats` reports it.

    ``kind`` is ``"private"``, ``"group"``, ``"supergroup"`` or ``"channel"``;
    ``title`` is a group's or channel's title, or a person's name."""

    id: int
    kind: str
    title: str
    username: str | None = None


def _as_chat(chat: Any) -> TelegramChat:
    if isinstance(chat, TelegramChat):
        return chat
    if isinstance(chat, (int, str)) and not isinstance(chat, bool):
        return TelegramChat(chat)
    raise TypeError(f'a chat is an int, an "@name" or a TelegramChat, got {chat!r}')


def _chat_specs(chats: Any) -> dict[str, TelegramChat]:
    """``chats`` as a dict of names: a dict keeps its keys; a chat given on its own is
    named by its address as text."""
    if isinstance(chats, Mapping):
        items = []
        for name, chat in chats.items():
            if not isinstance(name, str) or not name.strip():
                raise TypeError(f"chat names must be non-empty strings, got {name!r}")
            items.append((name, _as_chat(chat)))
    else:
        if isinstance(chats, (TelegramChat, int, str)):
            chats = [chats]
        elif not isinstance(chats, (list, tuple)):
            raise TypeError(
                'chats must be a dict such as {"me": 123456789}, one chat, or a list of '
                f"chats, got {type(chats).__name__}"
            )
        items = [(str(spec.chat_id), spec) for spec in map(_as_chat, chats)]
    if not items:
        raise ValueError('chats is empty — give at least one, such as {"me": 123456789}')
    specs: dict[str, TelegramChat] = {}
    for name, spec in items:
        if name in specs:
            raise ValueError(f"chat {name} is given twice")
        specs[name] = spec
    return specs


def _label(name: str, address: int | str, title: str | None = None) -> str:
    """How a line names a chat: ``signals (@atk_signals)``; a chat without an @name by
    its title, then its id: ``signals (ATK Signals, -1001234567890)``."""
    if name == str(address):
        return f"{title} ({address})" if title else str(address)
    return f"{name} ({title}, {address})" if title else f"{name} ({address})"


def _title_of(chat: Mapping[str, Any]) -> str:
    title = chat.get("title")
    if title:
        return str(title)
    return " ".join(str(part) for part in (chat.get("first_name"), chat.get("last_name")) if part)


def _destination(name: str, chat: TelegramChat) -> _Destination:
    address = chat.chat_id
    person = isinstance(address, int) and address > 0
    return _Destination(
        name, chat,
        events=chat.events, symbols=chat.symbols, silent=chat.silent,
        label=_label(name, address), pace_key=address,
        pace_rules=_CHAT_PACE if person else _GROUP_PACE,
    )


def _commander_ids(value: Any) -> frozenset[int]:
    """``commanders=`` (v1.2.0): people's Telegram ids — an int, digits in a string, or a
    list of them."""
    if value is None:
        return frozenset()
    items = [value] if isinstance(value, (int, str)) else value
    if isinstance(items, (bool, Mapping)) or not isinstance(items, Iterable):
        raise TypeError(f"commanders must be a list of Telegram user ids such as [123456789], "
                        f"got {type(value).__name__}")
    ids: set[int] = set()
    for item in items:
        if isinstance(item, bool) or not isinstance(item, (int, str)):
            raise TypeError(f"commanders must hold Telegram user ids such as 123456789, "
                            f"got {item!r}")
        text = item.strip() if isinstance(item, str) else str(item)
        if text.startswith("@"):
            raise ValueError(f"commanders: {item!r} — give the person's numeric id; "
                             "find_chats(token) lists it as the id of their private chat with "
                             "the bot")
        if not _NUMERIC.fullmatch(text) or int(text) == 0:
            raise ValueError(f"commanders: {item!r} is not a Telegram user id such as 123456789")
        if int(text) < 0:
            raise ValueError(f"commanders: {int(text)} is a group or a channel — a commander is "
                             "a person")
        ids.add(int(text))
    return frozenset(ids)


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------

def _check_token(token: Any) -> str:
    if not isinstance(token, str):
        raise TypeError("token must be a string: the one @BotFather gave you")
    token = token.strip()
    if not _TOKEN.fullmatch(token):
        raise ValueError(
            "this does not look like a bot token (digits, a colon, then letters) — "
            "copy it again from @BotFather"
        )
    return token


def _check_base_url(base_url: Any) -> str:
    if not isinstance(base_url, str) or not base_url.startswith(("http://", "https://")):
        raise ValueError(f"base_url must start with https:// or http://, got {base_url!r}")
    return base_url.rstrip("/")


def _mask_token(text: str, token: str) -> str:
    """``…/bot123456:AAH…/sendMessage`` → ``…/bot123456:***/sendMessage``."""
    secret = token.partition(":")[2]
    return text.replace(secret, "***") if secret else text


def _socks_available() -> bool:
    return importlib.util.find_spec("socks") is not None


def _is_socks(proxy: str | None) -> bool:
    return bool(proxy) and str(proxy).lower().startswith("socks")


def _proxies(proxy: str | None, base_url: str) -> dict[str, str] | None:
    """The ``proxies=`` every request passes.  ``None`` leaves requests' default, which
    honours ``HTTPS_PROXY`` / ``ALL_PROXY``; a proxy of our own is passed with every
    request, because the environment's would override a session's."""
    if proxy is None:
        environ = requests.utils.get_environ_proxies(base_url)
        if _is_socks(requests.utils.select_proxy(base_url, environ)) and not _socks_available():
            raise NotifyError(_SOCKS_NEEDS)
        return None
    scheme = proxy.partition("://")[0].lower() if isinstance(proxy, str) else ""
    if "://" not in str(proxy) or scheme not in _PROXY_SCHEMES:
        raise ValueError(
            'proxy must look like "http://127.0.0.1:10809" or "socks5h://127.0.0.1:10808"'
        )
    if _is_socks(proxy) and not _socks_available():
        raise NotifyError(_SOCKS_NEEDS)
    return {"http": proxy, "https": proxy}


def _network_reason(exc: Exception, timeout: float) -> str:
    """A short reason for a request that never got an answer — never ``str(exc)``,
    which holds the URL, and the URL holds the token."""
    if isinstance(exc, requests.exceptions.ProxyError):
        return "ProxyError"
    if isinstance(exc, requests.exceptions.SSLError):
        return "SSLError"
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return f"no connection in {timeout:g} s"
    if isinstance(exc, requests.exceptions.Timeout):
        return f"no answer in {timeout:g} s"
    text = str(exc)
    if any(sign in text for sign in (
        "NameResolutionError", "Name or service not known", "getaddrinfo failed",
        "nodename nor servname", "Temporary failure in name resolution",
    )):
        return "DNS lookup failed"
    if "Connection refused" in text or "10061" in text:
        return "connection refused"
    if any(sign in text for sign in ("RemoteDisconnected", "Connection aborted", "reset by peer")):
        return "connection dropped"
    return type(exc).__name__


class _Answer:
    """What Telegram said: ``status`` is the HTTP status, 0 when nothing came back."""

    __slots__ = ("status", "result", "reason", "retry_after")

    def __init__(
        self, status: int, *, result: Any = None, reason: str = "", retry_after: float = 0.0
    ) -> None:
        self.status = status
        self.result = result
        self.reason = reason
        self.retry_after = retry_after

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def unreachable(self) -> bool:
        """No use asking again now: the network, a 5xx, or a 429."""
        return self.status == 0 or self.status == 429 or self.status >= 500

    @property
    def fields(self) -> dict[str, Any]:
        return self.result if self.ok and isinstance(self.result, dict) else {}


def _parse_answer(response: requests.Response) -> _Answer:
    status = response.status_code
    try:
        data = response.json()
    except ValueError:
        data = None
    if isinstance(data, dict) and isinstance(data.get("ok"), bool):
        if data["ok"]:
            return _Answer(status if 200 <= status < 300 else 200, result=data.get("result"))
        code = data.get("error_code") if isinstance(data.get("error_code"), int) else status
        description = str(data.get("description") or response.reason or "").strip()
        parameters = data.get("parameters") if isinstance(data.get("parameters"), dict) else {}
        reason = f"{code} {description}".strip()
        if parameters.get("migrate_to_chat_id"):
            reason += f" — its new id is {parameters['migrate_to_chat_id']}"
        try:
            retry_after = float(parameters.get("retry_after") or 0)
        except (TypeError, ValueError):
            retry_after = 0.0
        return _Answer(code, reason=reason, retry_after=retry_after)
    if 200 <= status < 300:
        return _Answer(502, reason=f"{status}, but the answer is not Telegram's")
    retry_after = 0.0
    if status == 429:
        try:
            retry_after = float(response.headers.get("Retry-After") or 0)
        except ValueError:
            pass
    return _Answer(status, reason=f"{status} {response.reason or ''}".strip(),
                   retry_after=retry_after)


def _api_call(
    session: requests.Session, base_url: str, token: str, method: str,
    params: Mapping[str, Any] | None, *, timeout: float, proxies: dict[str, str] | None,
) -> _Answer:
    """One Bot API call.  Never raises: a request that got no answer comes back
    with status 0, and its exception is dropped here, so nothing chains it."""
    reason = None
    try:
        response = session.post(
            f"{base_url}/bot{token}/{method}", json=dict(params or {}),
            timeout=timeout, proxies=proxies,
        )
    except requests.exceptions.RequestException as exc:
        reason = _network_reason(exc, timeout)
    if reason is not None:
        return _Answer(0, reason=reason)
    return _parse_answer(response)


def _api_upload(
    session: requests.Session, base_url: str, token: str, method: str,
    fields: Mapping[str, Any], files: Mapping[str, tuple[str, bytes, str]], *, timeout: float,
    proxies: dict[str, str] | None,
) -> _Answer:
    """One Bot API call with a file, as a multipart upload (v1.2.0).  Never raises,
    as :func:`_api_call`."""
    form = {
        key: json.dumps(value) if isinstance(value, (dict, list))
        else ("true" if value is True else "false" if value is False else str(value))
        for key, value in fields.items()
    }
    reason = None
    try:
        response = session.post(
            f"{base_url}/bot{token}/{method}", data=form, files=dict(files),
            timeout=timeout, proxies=proxies,
        )
    except requests.exceptions.RequestException as exc:
        reason = _network_reason(exc, timeout)
    if reason is not None:
        return _Answer(0, reason=reason)
    return _parse_answer(response)


def _chats_in(updates: Any) -> list[TelegramChatInfo]:
    """Every chat named in *updates*, in the order first seen, each with its latest
    details."""
    seen: dict[int, Mapping[str, Any]] = {}
    for update in updates if isinstance(updates, list) else ():
        if not isinstance(update, dict):
            continue
        for item in update.values():
            if not isinstance(item, dict):
                continue
            chat = item.get("chat")
            if not isinstance(chat, dict) and isinstance(item.get("message"), dict):
                chat = item["message"].get("chat")   # a button press carries its message
            if isinstance(chat, dict) and isinstance(chat.get("id"), int):
                seen[chat["id"]] = chat
    return [
        TelegramChatInfo(
            id=chat_id, kind=str(chat.get("type", "")), title=_title_of(chat),
            username=chat.get("username") or None,
        )
        for chat_id, chat in seen.items()
    ]


def _print_chats(chats: list[TelegramChatInfo], bot_username: str | None) -> None:
    if not chats:
        print(_NOBODY)
        return
    bot = f"@{bot_username}" if bot_username else "the bot"
    print(f"Chats that wrote to {bot} in the last 24 hours:")
    print(f"  {'kind':<11}{'id':>16}  name")
    for chat in chats:
        name = f"{chat.title}  @{chat.username}" if chat.username else chat.title
        print(f"  {chat.kind:<11}{chat.id:>16}  {name}")


def _with_hint(reason: str) -> str:
    if "can't parse entities" in reason:
        return (f"{reason} — write &lt; &gt; &amp; for a literal < > &, "
                "or create the notifier with parse_mode=None")
    return reason


# ---------------------------------------------------------------------------
# The notifier
# ---------------------------------------------------------------------------

class TelegramNotifier(Notifier):
    """Send text to Telegram: to people, groups, channels and forum topics.

    ::

        from AlgoTradeKit.notify import TelegramChat, TelegramNotifier

        bot = TelegramNotifier(token, {
            "me": 123456789,
            "signals": TelegramChat(-1001234567890, events={"signal", "tp_level", "close"}),
        })
        bot.send_text("{header}\\nwatching the 64k level", symbol="BTCUSDT", broker=binance)

    token
        From @BotFather.  The library reads no environment variable for it; the
        examples read ``TELEGRAM_BOT_TOKEN``.
    chats
        A dict of names → chats (``123456789``, ``"@atk_signals"``, a numeric string,
        or a :class:`TelegramChat`), one chat, or a list of chats.
    timezone
        ``None`` (UTC) or an IANA name: ``"Asia/Tehran"`` prints «2026-10-14 17:33
        (UTC+03:30)».  It changes only the text a person reads.
    proxy
        ``"http://127.0.0.1:10809"`` or ``"socks5h://127.0.0.1:10808"`` (SOCKS needs
        PySocks: ``pip install "AlgoTradeKit[notify]"``).  ``None`` keeps requests'
        default, which honours ``HTTPS_PROXY`` and ``ALL_PROXY``.
    parse_mode
        ``"HTML"``: your own ``<b>``, ``<i>``, ``<code>``, ``<a href>``… work, and every
        placeholder value is escaped.  ``None``: plain text.
    check
        ``True`` checks the setup now.  A wrong token, an unknown chat, a channel where
        the bot cannot post, or a group it is not in raises :class:`TelegramError`,
        saying what to do.  Telegram unreachable prints one line and carries on: the
        messages wait in the queue.
    timeout
        Seconds each request may take.
    base_url
        Another Bot API server: a self-hosted one, or a fake in tests.
    templates
        Your own text for a kind of message, ``{kind: text}``, or ``None`` to never
        send that kind: ``{"open": "…", "sl_move": None}``.  ``"text"`` wraps every
        custom text, which its ``{text}`` holds: ``{"text": "<b>ATK</b> · {text}"}``.
    images
        The kinds of trade message that carry a picture of the pair's chart (v1.2.0),
        ``{"open", "close"}``: its text becomes the caption.  An OPEN and a SIGNAL show
        the last 100 candles with the position; a CLOSE, the trade from 20 candles
        before its entry.  ``text``, ``start`` and ``stop`` name no pair and are
        refused.  Needs the ``[notify]`` extra and its browser, checked now with
        ``check=True``: ``pip install "AlgoTradeKit[notify]"``, then ``playwright
        install chromium``.
    commanders
        The Telegram ids of the people who may command the bot (v1.2.0):
        ``[123456789]``.  The bot then listens, on a thread of its own, and answers
        /status, /positions, /chart, /stop and /help from them, in any chat they write
        in; anyone else's command is refused.  /stop ends a trading session, after a
        confirmation for live trading.  A person's id is the id of their private chat
        with the bot, which :meth:`find_chats` lists.

    Pictures of your own: :meth:`send_chart` (a chart, or a broker's candles between
    two datetimes), :meth:`send_photo` (bytes you made) and :meth:`send_report` (a
    backtest's figures, balance curve and HTML report).
        A trade event's fields are its placeholders (``{entry_price}``,
        ``{net_pnl}``, …), checked when the notifier is wired into trading.

    Messages are queued, paced and retried on the notifier's own thread (see
    :meth:`send_text`); ``close()`` delivers what is left, and so does the end of the
    program, for up to 10 seconds.  ``TraderConfig(notifiers=[bot])`` sends a
    trading session's events through it.
    """

    _service = "Telegram"
    _tag = "telegram"
    _error = TelegramError
    _max_length = 4096
    _can_reply = True
    _can_edit = True
    _can_send_media = True
    #: A picture's upload gets this much longer than a text's request.
    _UPLOAD_EXTRA = 30.0

    def __init__(
        self,
        token: str,
        chats: Any,
        *,
        timezone: str | None = None,
        proxy: str | None = None,
        parse_mode: str | None = "HTML",
        check: bool = True,
        timeout: float = 10.0,
        base_url: str = _API,
        templates: Mapping[str, str | None] | None = None,
        images: Any = None,
        commanders: Any = None,
    ) -> None:
        self._token = _check_token(token)
        self._bot_id = int(self._token.partition(":")[0])
        self._bot_username: str | None = None
        commander_ids = _commander_ids(commanders)
        if commander_ids:
            from . import _commands

            _commands.check_free(self._token)   # one listener per bot in a program
        if parse_mode not in ("HTML", None):
            raise ValueError(f'parse_mode must be "HTML" or None, got {parse_mode!r}')
        self._parse_mode = parse_mode
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError(f"timeout must be a number of seconds above 0, got {timeout!r}")
        self._timeout = float(timeout)
        self._base_url = _check_base_url(base_url)
        zone = _template.parse_zone(timezone)
        specs = _chat_specs(chats)
        self._proxies = _proxies(proxy, self._base_url)
        self._session = requests.Session()
        super().__init__(
            {name: _destination(name, chat) for name, chat in specs.items()},
            zone=zone, rich=parse_mode == "HTML", templates=templates, images=images,
        )
        if check:
            self._check()
            if self._images:   # what cannot fix itself is found now (Q3.7 a)
                from ..visual import _snapshot

                _snapshot.check_engine()
            if commander_ids and self._down_since is None:
                self._check_webhook()
        if commander_ids:
            from . import _commands

            self._listen_session = requests.Session()   # a long poll holds a connection
            self._commands = _commands.Listener(self, commander_ids)
            self._commands.resume()

    def __repr__(self) -> str:
        bot = f"@{self._bot_username}" if self._bot_username else f"bot {self._bot_id}"
        return f"TelegramNotifier({bot}, chats={list(self._destinations)!r})"

    # ------------------------------------------------------------------
    # Finding chat ids
    # ------------------------------------------------------------------

    @staticmethod
    def find_chats(
        token: str, *, proxy: str | None = None, base_url: str = _API
    ) -> list[TelegramChatInfo]:
        """Print and return every chat that wrote to the bot in the last 24 hours.

        Telegram keeps a bot's updates for 24 hours; this reads them without
        acknowledging any, so running it never eats one.  A private channel has no
        @name: the id printed here is its address.

        A bot that takes commands (``commanders=``) reads and confirms its updates
        itself.  While it listens in this program, this lists the chats its listener
        has seen, without asking Telegram; a listener in another program leaves here
        only what it has not read yet."""
        token = _check_token(token)
        commands = sys.modules.get(f"{__package__}._commands")
        listener = commands.listening(token) if commands is not None else None
        if listener is not None:
            seen = listener.seen()
            _print_chats(seen, listener.username)
            return seen
        base_url = _check_base_url(base_url)
        proxies = _proxies(proxy, base_url)
        with requests.Session() as session:
            me = _api_call(session, base_url, token, "getMe", None,
                           timeout=_FIND_TIMEOUT, proxies=proxies)
            updates = me
            if me.ok:
                updates = _api_call(session, base_url, token, "getUpdates", None,
                                    timeout=_FIND_TIMEOUT, proxies=proxies)
        problem = None
        if me.status in (401, 404):
            problem = f"Telegram rejected the token ({me.reason}) — copy it again from @BotFather"
        elif updates.status == 0:
            problem = f"Telegram unreachable ({updates.reason})"
        elif not updates.ok:
            method = "getMe" if updates is me else "getUpdates"
            problem = f"{method}: Telegram answered {updates.reason}"
        if problem is not None:
            raise TelegramError(_mask_token(problem, token))
        chats = _chats_in(updates.result)
        _print_chats(chats, me.fields.get("username"))
        return chats

    # ------------------------------------------------------------------
    # The core's hooks
    # ------------------------------------------------------------------

    @staticmethod
    def _measure(text: str) -> int:
        return len(text.encode("utf-16-le")) // 2

    def _mask(self, text: str) -> str:
        return _mask_token(text, self._token)

    def _call(self, method: str, params: Mapping[str, Any] | None = None) -> _Answer:
        return _api_call(self._session, self._base_url, self._token, method, params,
                         timeout=self._timeout, proxies=self._proxies)

    # ---- commands (v1.2.0) ----------------------------------------------

    def _poll(self, offset: int | None, wait: int) -> _Answer:
        """One long poll for the bot's updates, on a session of its own.  Telegram's
        default set of updates is asked for, so a later ``find_chats`` sees them all."""
        params: dict[str, Any] = {"timeout": wait, "allowed_updates": []}
        if offset is not None:
            params["offset"] = offset   # confirms every update before it
        return _api_call(self._listen_session, self._base_url, self._token, "getUpdates",
                         params, timeout=wait + self._timeout, proxies=self._proxies)

    def _answer_destination(self, chat: Mapping[str, Any], thread_id: Any) -> _Destination:
        """Where an answer to a command in *chat* (a forum topic of it) goes: beside the
        named chats, paced with them."""
        chat_id = int(chat["id"])
        key = (chat_id, thread_id)
        with self._lock:
            dest = self._extra_destinations.get(key)
            if dest is None:
                dest = _Destination(
                    f"chat {chat_id}", TelegramChat(chat_id, thread_id=thread_id),
                    events=frozenset(), symbols=None, silent=False,
                    label=_label(str(chat_id), chat_id, _title_of(chat) or None),
                    pace_key=chat_id, pace_rules=_CHAT_PACE if chat_id > 0 else _GROUP_PACE,
                )
                self._extra_destinations[key] = dest
            return dest

    def _check_webhook(self) -> None:
        """``commanders=``: getUpdates cannot run beside a webhook.  Only the host is
        said, because the address can hold a secret."""
        hook = self._call("getWebhookInfo")
        if self._carry_on(hook) or not hook.ok:
            return
        url = str(hook.fields.get("url") or "")
        if url:
            host = urlsplit(url).hostname or "an address"
            raise TelegramError(f"this bot sends its updates to a webhook ({host}) — "
                                "getUpdates cannot run beside it; remove the webhook, or "
                                "leave commanders= out")

    def _deliver(
        self, dest: _Destination, text: str, *, silent: bool, reply_to: Any = None
    ) -> _Outcome:
        chat: TelegramChat = dest.target
        params: dict[str, Any] = {
            "chat_id": chat.chat_id,
            "text": text,
            "link_preview_options": {"is_disabled": True},
        }
        if self._parse_mode is not None:
            params["parse_mode"] = self._parse_mode
        if chat.thread_id is not None:
            params["message_thread_id"] = chat.thread_id
        if silent:
            params["disable_notification"] = True
        if reply_to is not None:
            # A deleted root never fails the reply: it then goes as a plain message.
            params["reply_parameters"] = {
                "message_id": reply_to, "allow_sending_without_reply": True,
            }
        answer = self._call("sendMessage", params)
        return self._outcome(answer, message_id=answer.fields.get("message_id"))

    def _edit(self, dest: _Destination, message_id: Any, text: str) -> _Outcome:
        chat: TelegramChat = dest.target
        params: dict[str, Any] = {
            "chat_id": chat.chat_id,
            "message_id": message_id,
            "text": text,
            "link_preview_options": {"is_disabled": True},
        }
        if self._parse_mode is not None:
            params["parse_mode"] = self._parse_mode
        answer = self._call("editMessageText", params)
        if answer.status == 400 and "message is not modified" in answer.reason.lower():
            return _Outcome("sent", message_id=message_id)   # already says this: done
        return self._outcome(answer, message_id=message_id)

    def _edit_caption(self, dest: _Destination, message_id: Any, text: str) -> _Outcome:
        chat: TelegramChat = dest.target
        params: dict[str, Any] = {"chat_id": chat.chat_id, "message_id": message_id,
                                  "caption": text}
        if self._parse_mode is not None:
            params["parse_mode"] = self._parse_mode
        answer = self._call("editMessageCaption", params)
        if answer.status == 400 and "message is not modified" in answer.reason.lower():
            return _Outcome("sent", message_id=message_id)   # already says this: done
        return self._outcome(answer, message_id=message_id)

    def _deliver_media(
        self, dest: _Destination, media: _Media, data: bytes, caption: str | None, *,
        silent: bool, reply_to: Any = None,
    ) -> _Outcome:
        chat: TelegramChat = dest.target
        photo = media.kind == _Media.PHOTO
        fields: dict[str, Any] = {"chat_id": chat.chat_id}
        if caption:
            fields["caption"] = caption
            if self._parse_mode is not None:
                fields["parse_mode"] = self._parse_mode
        if chat.thread_id is not None:
            fields["message_thread_id"] = chat.thread_id
        if silent:
            fields["disable_notification"] = True
        if reply_to is not None:
            fields["reply_parameters"] = {
                "message_id": reply_to, "allow_sending_without_reply": True,
            }
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            mime = "image/png"
        elif data[:3] == b"\xff\xd8\xff":
            mime = "image/jpeg"
        else:
            mime = mimetypes.guess_type(media.filename)[0] or "application/octet-stream"
        field = "photo" if photo else "document"
        answer = _api_upload(
            self._session, self._base_url, self._token,
            "sendPhoto" if photo else "sendDocument", fields,
            {field: (media.filename, data, mime)},
            timeout=self._timeout + self._UPLOAD_EXTRA, proxies=self._proxies,
        )
        return self._outcome(answer, message_id=answer.fields.get("message_id"))

    @staticmethod
    def _outcome(answer: _Answer, *, message_id: Any) -> _Outcome:
        if answer.status == 0:
            return _Outcome("unreachable", reason=answer.reason)
        if answer.ok:
            return _Outcome("sent", message_id=message_id)
        if answer.status == 429:
            return _Outcome("wait", reason=answer.reason, retry_after=answer.retry_after)
        if answer.status >= 500:
            return _Outcome("server", reason=answer.reason)
        return _Outcome("refused", reason=_with_hint(answer.reason))

    # ------------------------------------------------------------------
    # The setup check
    # ------------------------------------------------------------------

    def _check(self) -> None:
        """Raise on what cannot fix itself; carry on when Telegram cannot be reached."""
        me = self._call("getMe")
        if self._carry_on(me):
            return
        if me.status in (401, 404):
            raise TelegramError(
                f"Telegram rejected the token ({me.reason}) — copy it again from @BotFather"
            )
        if not me.ok:
            raise TelegramError(f"getMe: Telegram answered {me.reason}")
        self._bot_username = me.fields.get("username") or None
        seen: dict[int | str, dict[str, Any]] = {}
        for dest in self._destinations.values():
            address = dest.target.chat_id
            if address not in seen:
                info = self._check_chat(address)
                if info is None:
                    return
                seen[address] = info
            info = seen[address]
            if isinstance(address, int):
                dest.label = _label(dest.name, address, _title_of(info) or None)
            if isinstance(info.get("id"), int):
                dest.pace_key = info["id"]

    def _carry_on(self, answer: _Answer) -> bool:
        """Telegram cannot be reached (or is too busy to ask): say so once, and carry on."""
        if not answer.unreachable:
            return False
        self._mark_unreachable(
            f"Telegram unreachable ({answer.reason}) — carrying on; messages wait in the queue"
        )
        return True

    def _check_chat(self, address: int | str) -> dict[str, Any] | None:
        """``getChat``, and the bot's own rights there.  ``None``: Telegram unreachable."""
        chat = self._call("getChat", {"chat_id": address})
        if self._carry_on(chat):
            return None
        if not chat.ok:
            raise TelegramError(self._unknown_chat(address, chat))
        info = chat.fields
        kind = info.get("type")
        if kind not in ("channel", "group", "supergroup"):
            return info
        member = self._call("getChatMember", {"chat_id": address, "user_id": self._bot_id})
        if self._carry_on(member):
            return None
        where = self._where(address, info)
        said = "" if member.ok else f" (Telegram answered {member.reason})"
        status = member.fields.get("status")
        if kind == "channel":
            if status != "creator" and not (
                status == "administrator" and member.fields.get("can_post_messages")
            ):
                raise TelegramError(
                    f'the bot cannot post in {where} — make it an administrator with '
                    f'"Post messages"{said}'
                )
            return info
        if not member.ok or status in ("left", "kicked") or (
            status == "restricted" and member.fields.get("is_member") is False
        ):
            raise TelegramError(
                f"the bot is not a member of {where} — add it to the group again{said}"
            )
        if status == "restricted" and member.fields.get("can_send_messages") is False:
            raise TelegramError(
                f"the bot cannot send messages in {where} — it is restricted there; "
                "give it back the right to send messages"
            )
        permissions = info.get("permissions") if isinstance(info.get("permissions"), dict) else {}
        if status == "member" and permissions.get("can_send_messages") is False:
            raise TelegramError(
                f"the bot cannot send messages in {where} — members may not send there; "
                "make the bot an administrator"
            )
        return info

    def _unknown_chat(self, address: int | str, answer: _Answer) -> str:
        if isinstance(address, int) and address > 0:
            bot = f"t.me/{self._bot_username}" if self._bot_username else "the bot"
            hint = f"if it is a person, they must open {bot} and press Start"
        else:
            hint = "add the bot to the group, or make it an administrator of the channel"
        if "chat not found" in answer.reason.lower():
            head = f"chat {address} not found"
        else:
            head = f"chat {address}: Telegram answered {answer.reason}"
        return f"{head} — {hint}; {_FIND_CHATS}"

    @staticmethod
    def _where(address: int | str, info: Mapping[str, Any]) -> str:
        if isinstance(address, str):
            return address
        title = _title_of(info)
        return f"{title} ({address})" if title else str(address)
