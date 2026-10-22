"""
AlgoTradeKit.notify._webhook
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The webhook backend (v1.2.0, E8): :class:`WebhookNotifier` and :class:`WebhookTarget`.

Each message is an HTTP POST of JSON to your URL (Q6.4 a)::

    {"id": "8f3c…", "kind": "close", "symbol": "BTCUSDT", "time": 1792001700123,
     "source": "SIM", "text": "🏁 CLOSED #12 · …", "html": "<b>🏁 CLOSED #12 · …</b>…",
     "silent": false, "fields": {"trade_id": 12, "net_pnl": 200.0, …}, "late_seconds": 0}

* ``id`` is the message's own, the same on every try: a receiver drops a repeat, as a
  retry after a timeout can deliver twice.  It is also what ``send_text`` returns.
* ``time`` is when the event was received, in UTC ms; ``source`` is SIM or LIVE for a
  trading session, the broker's name for an alert, and ``null`` for a text.
* ``fields`` holds the event's own values raw — numbers as numbers (a close's are the
  whole trade's, as its text says), times in UTC ms, and the candle it belongs to.
* ``text`` is plain; ``html`` is the rich text a Telegram chat gets.
* With a picture the body is ``multipart/form-data``: this JSON as ``payload_json``, and the
  PNG as ``file``.
* With ``secret=``, two headers: ``X-AlgoTradeKit-Timestamp`` (Unix seconds) and
  ``X-AlgoTradeKit-Signature: sha256=<hex>``, the HMAC-SHA256 of «timestamp.json».

A webhook has no replies and no edits (Q6.5 a): each message is a new POST, and a close's
``fields.trade_id`` ties it to its open.  Each target is retried on its own, so one receiver
that is down never holds the others.  A line names a target by its host only: a URL can hold
a secret.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import mimetypes
from collections.abc import Mapping
from dataclasses import KW_ONLY, dataclass
from typing import Any
from urllib.parse import urlsplit

import requests

from . import _template
from ._core import (
    Notifier,
    NotifyError,
    _Destination,
    _Media,
    _Message,
    _Outcome,
    check_kinds,
    plain_text,
)
from ._telegram import _name_set, _network_reason

#: A target gets at most ten messages a second; its own answers do the rest.
_PACE = ((10, 1.0),)
SIGNATURE_HEADER = "X-AlgoTradeKit-Signature"
TIMESTAMP_HEADER = "X-AlgoTradeKit-Timestamp"


class WebhookError(NotifyError):
    """A webhook refused a message, or could not be reached for a call that waited."""


def _check_url(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError(f"a webhook is its URL, a string, got {type(value).__name__}")
    url = value.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("a webhook's URL must start with https:// or http://")
    return url


@dataclass(frozen=True)
class WebhookTarget:
    """One URL the webhook posts to, with its own filters.

    url
        ``https://example.com/hook``.
    events
        The kinds this target gets, as a set, or ``None`` for every kind except
        ``sl_move``.  An empty set: only what names it in ``to=``.
    symbols
        The symbols it gets, as a set compared in capitals, or ``None`` for all.
    headers
        More headers for every request to it: ``{"Authorization": "Bearer …"}``.
    """

    url: str
    _: KW_ONLY
    events: frozenset[str] | None = None
    symbols: frozenset[str] | None = None
    headers: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "url", _check_url(self.url))
        events = _name_set(self.events, "events", '{"signal", "close"}')
        if events is not None:
            check_kinds(events)
        object.__setattr__(self, "events", events)
        symbols = _name_set(self.symbols, "symbols", '{"BTCUSDT"}')
        object.__setattr__(
            self, "symbols", None if symbols is None else frozenset(s.upper() for s in symbols)
        )
        if self.headers is not None:
            if not isinstance(self.headers, Mapping) or not all(
                    isinstance(key, str) and isinstance(item, str)
                    for key, item in self.headers.items()):
                raise TypeError("headers must be a dict of strings, such as "
                                '{"Authorization": "Bearer …"}')
            object.__setattr__(self, "headers", dict(self.headers))

    def __repr__(self) -> str:   # the URL can hold a secret
        return f"WebhookTarget({self.host})"

    @property
    def host(self) -> str:
        return urlsplit(self.url).netloc.rpartition("@")[2]


def _target_specs(targets: Any) -> dict[str, WebhookTarget]:
    """``targets`` as a dict of names: a dict keeps its keys; a URL given on its own is
    named by its host."""
    def made(value: Any) -> WebhookTarget:
        return value if isinstance(value, WebhookTarget) else WebhookTarget(value)

    if isinstance(targets, Mapping):
        items = []
        for name, target in targets.items():
            if not isinstance(name, str) or not name.strip():
                raise TypeError(f"target names must be non-empty strings, got {name!r}")
            items.append((name, made(target)))
    else:
        if isinstance(targets, (WebhookTarget, str)):
            targets = [targets]
        elif not isinstance(targets, (list, tuple)):
            raise TypeError('targets must be a dict such as {"bot": "https://example.com/hook"}, '
                            f"one URL, or a list, got {type(targets).__name__}")
        items = [(target.host, target) for target in map(made, targets)]
    if not items:
        raise ValueError('targets is empty — give at least one, such as '
                         '{"bot": "https://example.com/hook"}')
    specs: dict[str, WebhookTarget] = {}
    for name, target in items:
        if name in specs:
            raise ValueError(f"target {name} is given twice")
        specs[name] = target
    return specs


def jsonable(value: Any) -> Any:
    """*value* as JSON takes it: a number that is not finite becomes ``null``, a numpy
    number a plain one, and a tuple a list."""
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(item) for item in value]
    item = getattr(value, "item", None)   # a numpy scalar
    if callable(item):
        try:
            return jsonable(item())
        except (TypeError, ValueError):
            pass
    return str(value)


class WebhookNotifier(Notifier):
    """POST each message as JSON to your own URLs.

    ::

        from AlgoTradeKit.notify import WebhookNotifier

        hook = WebhookNotifier({"bot": "https://example.com/hook"}, secret=shared_secret)

    targets
        A dict of names → a URL or a :class:`WebhookTarget`, one URL, or a list.
    secret
        Sign every request: ``X-AlgoTradeKit-Signature: sha256=<hex>``, the HMAC-SHA256 of
        «timestamp.json» under this secret, with the timestamp in
        ``X-AlgoTradeKit-Timestamp``.  A receiver checks both, and refuses an old timestamp.
    timezone, timeout, templates, images
        As for ``TelegramNotifier``.  Nothing is asked of a target when the notifier is
        created: a receiver may answer anything to a probe.

    A 2xx answer is sent; a 429 waits its ``Retry-After``; a 5xx or no answer is tried
    again, for that target alone; any other answer refuses the message.
    """

    _service = "the webhook"
    _tag = "webhook"
    _error = WebhookError
    _max_length = 1_000_000
    _max_caption = 1_000_000
    _can_reply = False
    _can_edit = False
    _can_send_media = True
    _wants_message = True
    _max_photo_bytes = 20 * 1024 * 1024
    _max_document_bytes = 20 * 1024 * 1024
    _max_photo_sides = 1_000_000
    _max_photo_ratio = 1e9
    #: A picture's upload gets this much longer than a text's request.
    _UPLOAD_EXTRA = 30.0

    def __init__(
        self,
        targets: Any,
        *,
        secret: str | bytes | None = None,
        timezone: str | None = None,
        timeout: float = 10.0,
        templates: Mapping[str, str | None] | None = None,
        images: Any = None,
    ) -> None:
        if secret is not None:
            if not isinstance(secret, (str, bytes)) or not secret:
                raise ValueError("secret must be a non-empty string")
            secret = secret.encode() if isinstance(secret, str) else secret
        self._secret = secret
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError(f"timeout must be a number of seconds above 0, got {timeout!r}")
        self._timeout = float(timeout)
        zone = _template.parse_zone(timezone)
        self._session = requests.Session()
        destinations = {
            name: _Destination(
                name, target, events=target.events, symbols=target.symbols, silent=False,
                label=f"{name} ({target.host})" if name != target.host else target.host,
                pace_key=("webhook", name), pace_rules=_PACE,
            )
            for name, target in _target_specs(targets).items()
        }
        super().__init__(destinations, zone=zone, rich=True, templates=templates, images=images)
        if self._images:   # what cannot fix itself is found now (Q3.7 a)
            from ..visual import _snapshot

            _snapshot.check_engine()

    def __repr__(self) -> str:
        return f"WebhookNotifier(targets={list(self._destinations)!r})"

    def _mask(self, text: str) -> str:
        if self._secret:
            text = text.replace(self._secret.decode("utf-8", "replace"), "***")
        return text

    # ------------------------------------------------------------------
    # Requests
    # ------------------------------------------------------------------

    def _payload(self, message: _Message | None, text: str, silent: bool) -> dict[str, Any]:
        fields = dict(message.fields or {}) if message is not None else {}
        source = fields.pop("source", None)
        now = self._clock.time()
        queued = message.queued_at if message is not None else now
        return {
            "id": message.uid if message is not None else None,
            "kind": message.kind if message is not None else None,
            "symbol": message.symbol if message is not None else None,
            "time": int(queued * 1000),
            "source": source,
            "text": plain_text(text),
            "html": text,
            "silent": silent,
            "fields": jsonable(fields),
            "late_seconds": max(0, int(now - queued)),
        }

    def _headers(self, target: WebhookTarget, body: bytes) -> dict[str, str]:
        headers = {"User-Agent": "AlgoTradeKit"}
        if target.headers:
            headers.update(target.headers)
        if self._secret:
            stamp = str(int(self._clock.time()))
            signature = hmac.new(self._secret, stamp.encode() + b"." + body,
                                 hashlib.sha256).hexdigest()
            headers[TIMESTAMP_HEADER] = stamp
            headers[SIGNATURE_HEADER] = f"sha256={signature}"
        return headers

    def _request(self, target: WebhookTarget, payload: dict[str, Any], *,
              file: tuple[str, bytes, str] | None = None) -> _Outcome:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        headers = self._headers(target, body)
        wait = self._timeout if file is None else self._timeout + self._UPLOAD_EXTRA
        try:
            if file is None:
                headers["Content-Type"] = "application/json"
                response = self._session.post(target.url, data=body, headers=headers,
                                              timeout=wait)
            else:
                response = self._session.post(
                    target.url, headers=headers, timeout=wait,
                    files={"payload_json": (None, body, "application/json"), "file": file},
                )
        except requests.exceptions.RequestException as exc:
            # This target alone: one receiver that is down never holds the others.
            return _Outcome("server", reason=_network_reason(exc, wait))
        status = response.status_code
        if 200 <= status < 300:
            return _Outcome("sent", message_id=payload["id"])
        reason = f"{status} {response.reason or ''}".strip()
        if status == 429:
            try:
                retry_after = float(response.headers.get("Retry-After") or 1)
            except ValueError:
                retry_after = 1.0
            return _Outcome("wait", reason=reason, retry_after=retry_after)
        if status >= 500:
            return _Outcome("server", reason=reason)
        return _Outcome("refused", reason=reason)

    # ------------------------------------------------------------------
    # The core's hooks
    # ------------------------------------------------------------------

    def _deliver(
        self, dest: _Destination, text: str, *, silent: bool, reply_to: Any = None,
        message: _Message | None = None,
    ) -> _Outcome:
        return self._request(dest.target, self._payload(message, text, silent))

    def _deliver_media(
        self, dest: _Destination, media: _Media, data: bytes, caption: str | None, *,
        silent: bool, reply_to: Any = None, message: _Message | None = None,
    ) -> _Outcome:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            mime = "image/png"
        elif data[:3] == b"\xff\xd8\xff":
            mime = "image/jpeg"
        else:
            mime = mimetypes.guess_type(media.filename)[0] or "application/octet-stream"
        return self._request(dest.target, self._payload(message, caption or "", silent),
                          file=(media.filename, data, mime))
