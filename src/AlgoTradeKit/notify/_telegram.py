"""
AlgoTradeKit.notify._telegram
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The Telegram backend (v1.2.0): :class:`TelegramNotifier`, :class:`TelegramChat`,
:meth:`TelegramNotifier.find_chats`.

It talks to the Bot API over HTTPS with ``requests``, a handful of calls
(``getMe``, ``getChat``, ``getChatMember``, ``sendMessage``, ``getUpdates``),
and no Telegram library.

Telegram's limits shape it:

* a message holds 4,096 characters, which Telegram counts in UTF-16 units, so an
  emoji counts two; a longer text goes as several messages;
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
import re
from collections.abc import Mapping
from dataclasses import KW_ONLY, dataclass
from typing import Any

import requests

from . import _template
from ._core import Notifier, NotifyError, _Destination, _Outcome

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
        ``sl_move``.  An empty set: only what names the chat in ``to=``.
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
        object.__setattr__(
            self, "events", _name_set(self.events, "events", '{"signal", "close"}')
        )
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

    Messages are queued, paced and retried on the notifier's own thread (see
    :meth:`send_text`); ``close()`` delivers what is left, and so does the end of the
    program, for up to 10 seconds.
    """

    _service = "Telegram"
    _tag = "telegram"
    _error = TelegramError
    _max_length = 4096

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
    ) -> None:
        self._token = _check_token(token)
        self._bot_id = int(self._token.partition(":")[0])
        self._bot_username: str | None = None
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
            zone=zone, rich=parse_mode == "HTML",
        )
        if check:
            self._check()

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
        @name: the id printed here is its address."""
        token = _check_token(token)
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

    def _deliver(self, dest: _Destination, text: str, *, silent: bool) -> _Outcome:
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
        answer = self._call("sendMessage", params)
        if answer.status == 0:
            return _Outcome("unreachable", reason=answer.reason)
        if answer.ok:
            return _Outcome("sent", message_id=answer.fields.get("message_id"))
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
