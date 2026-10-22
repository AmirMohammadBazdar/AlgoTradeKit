"""
AlgoTradeKit.notify._email
~~~~~~~~~~~~~~~~~~~~~~~~~~
The e-mail backend (v1.2.0, E8): :class:`EmailNotifier` and :class:`EmailRecipient`.

It sends through your SMTP server with the standard library (``smtplib``, ``email``): one
connection per e-mail, TLS by the port — SSL on 465, STARTTLS on any other — and your login.
Each e-mail is HTML, with a plain-text alternative; a picture is inline, a file attached.  The
subject is the message's first line.

A digest (Q6.3 b).  A recipient's trade messages — signals, opens, stop moves, risk-free, TP
levels, closes, exit signals and a strategy's own messages — wait until the oldest has waited
``digest_minutes`` (15), then go as one e-mail; everything else goes at once.  A busy strategy
then sends at most 96 digests a day per recipient, under a personal Gmail account's cap.
``digest_minutes=0`` sends each message on its own, and a trade's e-mails then thread by
``In-Reply-To`` (E1).  E-mail has no edits: a stop that moves is a new e-mail.

The password appears in no line, error or ``repr``.
"""

from __future__ import annotations

import email.utils
import html
import mimetypes
import re
import smtplib
import ssl
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import KW_ONLY, dataclass
from email.message import EmailMessage
from typing import Any

from . import _template
from ._core import (
    KIND_CLOSE,
    KIND_EXIT_SIGNAL,
    KIND_MESSAGE,
    KIND_OPEN,
    KIND_RISK_FREE,
    KIND_SIGNAL,
    KIND_SL_MOVE,
    KIND_TP_LEVEL,
    Notifier,
    NotifyError,
    _Destination,
    _Media,
    _Message,
    _Outcome,
    check_kinds,
    plain_text,
)
from ._telegram import _name_set

#: The kinds a digest gathers: a trade's messages, and a strategy's own.
DIGEST_KINDS = frozenset({KIND_SIGNAL, KIND_OPEN, KIND_SL_MOVE, KIND_RISK_FREE, KIND_TP_LEVEL,
                          KIND_CLOSE, KIND_EXIT_SIGNAL, KIND_MESSAGE})
#: The fields of a ``subject=`` template.
SUBJECT_FIELDS = ("title", "kind", "symbol", "count")
_TLS = ("ssl", "starttls", "none")
#: At most one e-mail a second to a recipient.
_PACE = ((1, 1.0),)
#: How many subjects are kept, so a reply can say «Re: » and its root's.
_SUBJECTS_KEPT = 500
_ADDRESS = re.compile(r"[^@\s<>]+@[^@\s<>]+")
_LATE = re.compile(r"⏱ \d+ min late")
_PRE = re.compile(r"(<pre>.*?</pre>)", re.IGNORECASE | re.DOTALL)
_STYLE = "font-family:system-ui,-apple-system,'Segoe UI',sans-serif;font-size:15px;line-height:1.45"


class EmailError(NotifyError):
    """The SMTP server refused a login, an address or an e-mail, or could not be reached
    for a call that waited."""


def _check_address(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _ADDRESS.fullmatch(value.strip()):
        raise ValueError(f"{what} must be an e-mail address such as ada@example.com, "
                         f"got {value!r}")
    return value.strip()


@dataclass(frozen=True)
class EmailRecipient:
    """One recipient, with its own filters.

    address
        ``ada@example.com``.
    events
        The kinds this recipient gets, as a set, or ``None`` for every kind except
        ``sl_move``.  An empty set: only what names it in ``to=``.
    symbols
        The symbols it gets, as a set compared in capitals, or ``None`` for all.
    """

    address: str
    _: KW_ONLY
    events: frozenset[str] | None = None
    symbols: frozenset[str] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "address", _check_address(self.address, "address"))
        events = _name_set(self.events, "events", '{"signal", "close"}')
        if events is not None:
            check_kinds(events)
        object.__setattr__(self, "events", events)
        symbols = _name_set(self.symbols, "symbols", '{"BTCUSDT"}')
        object.__setattr__(
            self, "symbols", None if symbols is None else frozenset(s.upper() for s in symbols)
        )


def _recipient_specs(recipients: Any) -> dict[str, EmailRecipient]:
    """``recipients`` as a dict of names: a dict keeps its keys; an address given on its own
    is its own name."""
    def made(value: Any) -> EmailRecipient:
        return value if isinstance(value, EmailRecipient) else EmailRecipient(value)

    if isinstance(recipients, Mapping):
        items = []
        for name, who in recipients.items():
            if not isinstance(name, str) or not name.strip():
                raise TypeError(f"recipient names must be non-empty strings, got {name!r}")
            items.append((name, made(who)))
    else:
        if isinstance(recipients, (EmailRecipient, str)):
            recipients = [recipients]
        elif not isinstance(recipients, (list, tuple)):
            raise TypeError('recipients must be a dict such as {"me": "ada@example.com"}, one '
                            f"address, or a list, got {type(recipients).__name__}")
        items = [(who.address, who) for who in map(made, recipients)]
    if not items:
        raise ValueError('recipients is empty — give at least one, such as '
                         '{"me": "ada@example.com"}')
    specs: dict[str, EmailRecipient] = {}
    for name, who in items:
        if name in specs:
            raise ValueError(f"recipient {name} is given twice")
        specs[name] = who
    return specs


def _said(exc: smtplib.SMTPResponseException) -> str:
    text = exc.smtp_error
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    return " ".join(str(text).split())


def _reason(exc: BaseException) -> str:
    """A short reason for a connection that failed."""
    if isinstance(exc, TimeoutError):
        return "no answer in time"
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused"
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return "the server hung up"
    text = str(exc).strip()
    if "Name or service not known" in text or "getaddrinfo failed" in text or (
            "nodename nor servname" in text or "Temporary failure in name resolution" in text):
        return "DNS lookup failed"
    return type(exc).__name__


def _html_of(text: str) -> str:
    """The rich text as an e-mail's HTML: its tags kept, its lines broken, outside
    ``<pre>``."""
    pieces = _PRE.split(text)
    return "".join(piece if index % 2 else piece.replace("\n", "<br>\n")
                   for index, piece in enumerate(pieces))


class EmailNotifier(Notifier):
    """Send e-mail through your SMTP server.

    ::

        from AlgoTradeKit.notify import EmailNotifier

        mail = EmailNotifier("smtp.gmail.com", {"me": "ada@example.com"},
                             username="ada@example.com", password=app_password)
        mail.send_text("{header}\\nwatching the 64k level", symbol="BTCUSDT", broker=binance)

    host, port, tls
        The SMTP server.  ``tls=None`` takes SSL on port 465 and STARTTLS on any other;
        ``"ssl"``, ``"starttls"`` and ``"none"`` force one.  Port 25 is often closed on a
        VPS; 587 and 465 are the usual ones.
    recipients
        A dict of names → an address or an :class:`EmailRecipient`, one address, or a list.
    username, password, sender
        The login, and the address the e-mails come from (``username`` by default).  Gmail
        takes an app password, not the account's.
    subject
        A template for the subject, of ``{title}`` (the message's first line), ``{kind}``,
        ``{symbol}`` and ``{count}`` (the messages a digest holds).  ``None``: the title, and
        a digest's «· and 2 more».
    digest_minutes
        A recipient's trade messages wait this long, from the oldest, and go as one e-mail;
        the rest goes at once.  ``0``: every message on its own.
    timezone, check, timeout, templates, images
        As for ``TelegramNotifier``.  ``check=True`` logs in now: a refused login raises
        :class:`EmailError`; a server that cannot be reached prints one line, and the
        e-mails wait in the queue.
    """

    _tag = "email"
    _error = EmailError
    _max_length = 1_000_000
    _max_caption = 1_000_000
    _can_reply = True
    _can_edit = False
    _can_send_media = True
    _wants_message = True
    _max_photo_bytes = 20 * 1024 * 1024
    _max_document_bytes = 20 * 1024 * 1024
    _max_photo_sides = 1_000_000
    _max_photo_ratio = 1e9
    #: The TLS context; tests replace it to trust their own certificate.
    _ssl_context: ssl.SSLContext | None = None

    def __init__(
        self,
        host: str,
        recipients: Any,
        *,
        port: int = 587,
        tls: str | None = None,
        username: str | None = None,
        password: str | None = None,
        sender: str | None = None,
        subject: str | None = None,
        digest_minutes: float = 15,
        timezone: str | None = None,
        check: bool = True,
        timeout: float = 30.0,
        templates: Mapping[str, str | None] | None = None,
        images: Any = None,
    ) -> None:
        if not isinstance(host, str) or not host.strip():
            raise ValueError(f"host must be the SMTP server's name, such as smtp.gmail.com, "
                             f"got {host!r}")
        if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65_536:
            raise ValueError(f"port must be a port number such as 587 or 465, got {port!r}")
        if tls is not None and tls not in _TLS:
            raise ValueError(f'tls must be None, "ssl", "starttls" or "none", got {tls!r}')
        for name, value in (("username", username), ("password", password)):
            if value is not None and not isinstance(value, str):
                raise TypeError(f"{name} must be a string, got {type(value).__name__}")
        if password is not None and not username:
            raise ValueError("password= needs username= too")
        if sender is None:
            if not username or not _ADDRESS.fullmatch(username.strip()):
                raise ValueError("sender= is needed: the address the e-mails come from")
            sender = username
        self._sender = _check_address(sender, "sender")
        if subject is not None:
            if not isinstance(subject, str):
                raise TypeError(f"subject must be a template string, got {type(subject).__name__}")
            _template.fields_of(subject, SUBJECT_FIELDS)
        if isinstance(digest_minutes, bool) or not isinstance(digest_minutes, (int, float)) or (
                digest_minutes < 0):
            raise ValueError(f"digest_minutes must be 0 or more, got {digest_minutes!r}")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError(f"timeout must be a number of seconds above 0, got {timeout!r}")
        self._host = host.strip()
        self._port = port
        self._tls = tls or ("ssl" if port == 465 else "starttls")
        self._username = username.strip() if username else None
        self._password = password
        self._subject = subject
        self._timeout = float(timeout)
        self._service = self._host          # lines read «smtp.gmail.com unreachable (…)»
        self._domain = self._sender.rpartition("@")[2]
        self._subjects: OrderedDict[str, str] = OrderedDict()   # Message-ID -> its subject
        zone = _template.parse_zone(timezone)
        destinations: dict[str, _Destination] = {}
        for name, who in _recipient_specs(recipients).items():
            dest = _Destination(
                name, who, events=who.events, symbols=who.symbols, silent=False,
                label=f"{name} ({who.address})" if name != who.address else who.address,
                pace_key=who.address.lower(), pace_rules=_PACE,
            )
            dest.digest_seconds = float(digest_minutes) * 60
            dest.digest_kinds = DIGEST_KINDS
            destinations[name] = dest
        super().__init__(destinations, zone=zone, rich=True, templates=templates, images=images)
        if check:
            self._check()
            if self._images:   # what cannot fix itself is found now (Q3.7 a)
                from ..visual import _snapshot

                _snapshot.check_engine()

    def __repr__(self) -> str:
        return f"EmailNotifier({self._host}:{self._port}, recipients={list(self._destinations)!r})"

    def _mask(self, text: str) -> str:
        return text.replace(self._password, "***") if self._password else text

    # ------------------------------------------------------------------
    # The server
    # ------------------------------------------------------------------

    def _context(self) -> ssl.SSLContext:
        return self._ssl_context or ssl.create_default_context()

    def _connect(self) -> smtplib.SMTP:
        """A logged-in connection.  Raises what smtplib raises."""
        if self._tls == "ssl":
            server: smtplib.SMTP = smtplib.SMTP_SSL(self._host, self._port,
                                                    timeout=self._timeout,
                                                    context=self._context())
        else:
            server = smtplib.SMTP(self._host, self._port, timeout=self._timeout)
        try:
            if self._tls == "starttls":
                server.starttls(context=self._context())
            if self._username:
                server.login(self._username, self._password or "")
        except BaseException:
            server.close()
            raise
        return server

    def _check(self) -> None:
        """Log in: raise on what cannot fix itself, carry on when the server cannot be
        reached."""
        try:
            server = self._connect()
        except smtplib.SMTPAuthenticationError as exc:
            raise EmailError(self._mask(
                f"the SMTP server refused the login ({exc.smtp_code} {_said(exc)}) — check "
                "username and password; Gmail takes an app password, not the account's"
            )) from None
        except smtplib.SMTPNotSupportedError:
            raise EmailError(f'{self._host} does not offer STARTTLS on port {self._port} — use '
                             'tls="ssl" on port 465, or tls="none"') from None
        except ssl.SSLError as exc:
            raise EmailError(f"TLS with {self._host}:{self._port} failed ({exc.reason or exc}) "
                             '— port 465 takes tls="ssl", and 587 tls="starttls"') from None
        except (OSError, smtplib.SMTPException) as exc:
            self._mark_unreachable(f"{self._host} unreachable ({_reason(exc)}) — carrying on; "
                                   "e-mails wait in the queue")
            return
        try:
            server.quit()
        except (OSError, smtplib.SMTPException):
            server.close()

    @staticmethod
    def _by_code(code: int, said: str) -> _Outcome:
        reason = f"{code} {said}".strip()
        if 400 <= code < 500:
            return _Outcome("server", reason=reason)     # try later
        return _Outcome("refused", reason=reason)

    def _send(self, message: EmailMessage, address: str) -> _Outcome:
        try:
            server = self._connect()
        except smtplib.SMTPAuthenticationError as exc:
            return _Outcome("refused", reason=self._mask(
                f"the SMTP server refused the login ({exc.smtp_code} {_said(exc)})"))
        except smtplib.SMTPResponseException as exc:
            return self._by_code(exc.smtp_code, _said(exc))
        except ssl.SSLError as exc:
            return _Outcome("refused", reason=f"TLS failed ({exc.reason or exc})")
        except (OSError, smtplib.SMTPException) as exc:
            return _Outcome("unreachable", reason=_reason(exc))
        try:
            server.send_message(message, from_addr=self._sender, to_addrs=[address])
        except smtplib.SMTPRecipientsRefused as exc:
            code, said = next(iter(exc.recipients.values()), (550, b""))
            text = said.decode("utf-8", "replace") if isinstance(said, bytes) else str(said)
            return self._by_code(code, " ".join(text.split()))
        except smtplib.SMTPResponseException as exc:
            return self._by_code(exc.smtp_code, _said(exc))
        except (OSError, smtplib.SMTPException) as exc:
            return _Outcome("unreachable", reason=_reason(exc))
        finally:
            try:
                server.quit()
            except (OSError, smtplib.SMTPException):
                server.close()
        return _Outcome("sent", message_id=message["Message-ID"])

    # ------------------------------------------------------------------
    # E-mails
    # ------------------------------------------------------------------

    def _title(self, text: str, fallback: str) -> str:
        lines = [line.strip() for line in plain_text(text).splitlines() if line.strip()]
        while lines and _LATE.fullmatch(lines[0]):
            lines = lines[1:]
        return lines[0] if lines else fallback

    def _subject_of(self, text: str, message: _Message | None, count: int,
                    fallback: str = "AlgoTradeKit") -> str:
        title = self._title(text, fallback)
        if self._subject is None:
            subject = title if count == 1 else f"{title} · and {count - 1} more"
        else:
            subject = _template.render(self._subject, {
                "title": title, "kind": message.kind if message is not None else "",
                "symbol": (message.symbol or "") if message is not None else "",
                "count": count,
            }, escape=False)
        return " ".join(subject.split())

    def _compose(self, dest: _Destination, parts: list[tuple[str, _Media | None,
                                                               bytes | None]],
                 message: _Message | None, *, reply_to: Any = None) -> EmailMessage:
        """One e-mail of *parts* — ``(rich text, picture or file, its bytes)`` — oldest first."""
        mail = EmailMessage()
        mail["From"] = self._sender
        mail["To"] = dest.target.address
        mail["Date"] = email.utils.formatdate(self._clock.time(), usegmt=True)
        mail["Message-ID"] = email.utils.make_msgid(domain=self._domain)
        first = parts[0][1] if parts else None   # a picture or a file with no caption: its name
        subject = self._subject_of(parts[0][0] if parts else "", message, len(parts),
                                   first.filename if first is not None else "AlgoTradeKit")
        if reply_to is not None:
            mail["In-Reply-To"] = reply_to
            mail["References"] = reply_to
            root = self._subjects.get(reply_to, subject)
            subject = root if root.startswith("Re: ") else f"Re: {root}"
        mail["Subject"] = subject
        plain: list[str] = []
        blocks: list[str] = []
        pictures: list[tuple[str, bytes, str]] = []
        files: list[tuple[str, bytes, str]] = []
        for index, (text, media, data) in enumerate(parts):
            plain.append(plain_text(text))
            block = _html_of(text)
            if media is not None and data is not None:
                if media.kind == _Media.PHOTO:
                    cid = email.utils.make_msgid(domain=self._domain)
                    mime = "image/jpeg" if data[:3] == b"\xff\xd8\xff" else "image/png"
                    pictures.append((cid, data, mime))
                    alt = html.escape(media.label or "picture")
                    block += f'<br>\n<img src="cid:{cid[1:-1]}" alt="{alt}" style="max-width:100%">'
                else:
                    mime = mimetypes.guess_type(media.filename)[0] or "application/octet-stream"
                    files.append((media.filename, data, mime))
            blocks.append(f"<div>{block}</div>")
        mail.set_content("\n\n— — —\n\n".join(plain) + "\n")
        mail.add_alternative(
            f'<!doctype html>\n<html><body style="{_STYLE}">\n'
            + "\n<hr>\n".join(blocks) + "\n</body></html>\n", subtype="html")
        if pictures:
            body = mail.get_payload()[1]
            for cid, data, mime in pictures:
                maintype, subtype = mime.split("/")
                body.add_related(data, maintype=maintype, subtype=subtype, cid=cid)
        for filename, data, mime in files:
            maintype, subtype = mime.split("/", 1)
            mail.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
        self._subjects[str(mail["Message-ID"])] = subject
        while len(self._subjects) > _SUBJECTS_KEPT:
            self._subjects.popitem(last=False)
        return mail

    # ------------------------------------------------------------------
    # The core's hooks
    # ------------------------------------------------------------------

    def _deliver(
        self, dest: _Destination, text: str, *, silent: bool, reply_to: Any = None,
        message: _Message | None = None,
    ) -> _Outcome:
        mail = self._compose(dest, [(text, None, None)], message, reply_to=reply_to)
        return self._send(mail, dest.target.address)

    def _deliver_media(
        self, dest: _Destination, media: _Media, data: bytes, caption: str | None, *,
        silent: bool, reply_to: Any = None, message: _Message | None = None,
    ) -> _Outcome:
        mail = self._compose(dest, [(caption or "", media, data)], message, reply_to=reply_to)
        return self._send(mail, dest.target.address)

    def _deliver_digest(
        self, dest: _Destination, parts: list[tuple[_Message, str, _Media | None]], *,
        silent: bool,
    ) -> _Outcome:
        items = [(text, media, None if media is None else media.data)
                 for _message, text, media in parts]
        mail = self._compose(dest, items, parts[0][0] if parts else None)
        return self._send(mail, dest.target.address)
