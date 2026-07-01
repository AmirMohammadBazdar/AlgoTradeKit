"""
Signed Binance REST client (spot + USD-M futures).

Public endpoints need no auth; private endpoints are signed with
``HMAC-SHA256(secret, query_string)`` and an ``X-MBX-APIKEY`` header — all with
the Python standard library (``hmac`` / ``hashlib`` / ``urllib.parse``), so the
only third-party dependency is ``requests`` (already required by the library).
"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any, Optional
from urllib.parse import urlencode

import requests

from ..._errors import AuthenticationError, ConnectionFailed, OrderError
from ..._timeutil import now_ms
from ._endpoints import Endpoints

_MAX_ATTEMPTS = 5
_RATE_LIMIT_PAUSE = 0.12  # seconds — safe under Binance weight limits


class BinanceREST:
    """Thin signed HTTP client for one Binance market (spot or futures)."""

    def __init__(
        self,
        endpoints: Endpoints,
        api_key: Optional[str] = None,
        api_secret: Optional[str] = None,
        recv_window: int = 5000,
    ) -> None:
        self.ep = endpoints
        self._api_key = api_key
        self._api_secret = api_secret
        self._recv_window = recv_window

        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})
        if api_key:
            self._session.headers.update({"X-MBX-APIKEY": api_key})

    @property
    def has_credentials(self) -> bool:
        return bool(self._api_key and self._api_secret)

    # ------------------------------------------------------------------
    # Public / signed requests
    # ------------------------------------------------------------------

    def public(self, method: str, url: str, params: Optional[dict] = None) -> Any:
        return self._request(method, url, params or {}, signed=False)

    def signed(self, method: str, url: str, params: Optional[dict] = None) -> Any:
        if not self.has_credentials:
            raise AuthenticationError(
                "This Binance call requires api_key and api_secret."
            )
        params = dict(params or {})
        params["timestamp"] = now_ms()
        params["recvWindow"] = self._recv_window
        return self._request(method, url, params, signed=True)

    # ------------------------------------------------------------------
    # Klines (public) — single page
    # ------------------------------------------------------------------

    def fetch_klines(
        self,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
        limit: int,
    ) -> list[list]:
        params = {
            "symbol": symbol.upper(),
            "interval": interval,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": limit,
        }
        return self.public("GET", self.ep.klines, params)

    def get_server_time(self) -> int:
        return int(self.public("GET", self.ep.server_time, {})["serverTime"])

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _sign(self, query: str) -> str:
        return hmac.new(
            self._api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _request(self, method: str, url: str, params: dict, signed: bool) -> Any:
        # Build the exact query string we will sign AND send, so the signature
        # always matches what the server receives.
        query = urlencode(params, doseq=True)
        if signed:
            query = f"{query}&signature={self._sign(query)}" if query else \
                f"signature={self._sign('')}"
        full_url = f"{url}?{query}" if query else url

        for attempt in range(_MAX_ATTEMPTS):
            try:
                resp = self._session.request(method.upper(), full_url, timeout=30)
            except requests.ConnectionError as exc:
                if attempt == _MAX_ATTEMPTS - 1:
                    raise ConnectionFailed(
                        f"Cannot reach Binance ({self.ep.market}). Check connectivity."
                    ) from exc
                time.sleep(2 ** attempt)
                continue
            except requests.Timeout as exc:
                if attempt == _MAX_ATTEMPTS - 1:
                    raise ConnectionFailed("Binance request timed out after 30 s.") from exc
                time.sleep(2 ** attempt)
                continue

            # Rate limited — honour Retry-After
            if resp.status_code in (429, 418):
                retry_after = int(resp.headers.get("Retry-After", 60))
                print(f"  ⚠  Rate limited by Binance. Waiting {retry_after} s…")
                time.sleep(retry_after)
                continue

            if resp.status_code >= 400:
                detail = self._error_detail(resp)
                # -2xxx / 4xx business errors won't succeed on retry
                if resp.status_code == 401:
                    raise AuthenticationError(f"Binance auth failed: {detail}")
                raise OrderError(f"Binance rejected the request ({resp.status_code}): {detail}")

            if signed:
                time.sleep(_RATE_LIMIT_PAUSE)
            return resp.json()

        raise ConnectionFailed(f"Binance request failed after {_MAX_ATTEMPTS} attempts.")

    @staticmethod
    def _error_detail(resp: requests.Response) -> str:
        try:
            body = resp.json()
            return f"code={body.get('code')} msg={body.get('msg', resp.text)}"
        except Exception:  # noqa: BLE001
            return resp.text

    def close(self) -> None:
        self._session.close()
