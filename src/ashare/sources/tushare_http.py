"""Tushare-compatible HTTP client.

Speaks the Pro JSON protocol (`api_name`, `token`, `params`, `fields`) against
whatever endpoint the operator sets. `TUSHARE_TOKEN` and `TUSHARE_HTTP_URL`
are read on each call. Nothing in this module is a default host or a token.

This path does not call www.szse.cn. A URL whose host is szse.cn is rejected
so a mis-set endpoint cannot fall through to that TLS failure.
"""
from __future__ import annotations

import json
import os
from typing import Callable
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import pandas as pd

TOKEN_ENV = "TUSHARE_TOKEN"
URL_ENV = "TUSHARE_HTTP_URL"

Transport = Callable[[str, dict], str]


class TushareConfigError(RuntimeError):
    """Required environment variables are absent or the URL is unusable."""


class TushareHttpError(RuntimeError):
    """The endpoint responded with an error or the body was not usable."""


def _redact(text: str, token: str) -> str:
    if token and token in text:
        return text.replace(token, "***")
    return text


def require_settings() -> tuple[str, str]:
    """Return `(token, url)` or raise `TushareConfigError`. Do not print the token."""
    return _settings()


def _settings() -> tuple[str, str]:
    token = os.environ.get(TOKEN_ENV, "").strip()
    url = os.environ.get(URL_ENV, "").strip()
    missing = [name for name, value in ((TOKEN_ENV, token), (URL_ENV, url)) if not value]
    if missing:
        raise TushareConfigError(
            "missing " + " and ".join(missing)
            + "; set them in the environment. This client has no built-in endpoint or token."
        )
    host = (urlparse(url).hostname or "").lower()
    if host == "szse.cn" or host.endswith(".szse.cn"):
        raise TushareConfigError(
            f"{URL_ENV} points at {host}. That host is not a Tushare endpoint "
            "and its TLS handshake is not used by this client."
        )
    if urlparse(url).scheme not in {"http", "https"}:
        raise TushareConfigError(f"{URL_ENV} must be an http(s) URL")
    return token, url


def _post(url: str, body: dict) -> str:
    data = json.dumps(body).encode("utf-8")
    req = Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8")
    except URLError as e:
        raise TushareHttpError(f"tushare request failed: {e.reason}") from None


def query(
    api_name: str,
    params: dict | None = None,
    fields: str = "",
    *,
    transport: Transport | None = None,
) -> pd.DataFrame:
    """POST one Pro API and return a DataFrame of `data.items`.

    `transport(url, body) -> response text` replaces the network call in tests.
    The token is read here and is not written to the exception text.
    """
    token, url = _settings()
    body = {
        "api_name": api_name,
        "token": token,
        "params": params or {},
        "fields": fields,
    }
    send = transport or _post
    try:
        raw = send(url, body)
    except TushareHttpError as e:
        raise TushareHttpError(_redact(str(e), token)) from None
    except Exception as e:  # noqa: BLE001 - transport failures stay token-free
        raise TushareHttpError(_redact(f"tushare request failed: {e}", token)) from None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise TushareHttpError(f"tushare response was not JSON: {e}") from None
    if not isinstance(payload, dict):
        raise TushareHttpError("tushare response was not an object")
    code = payload.get("code", 0)
    if code not in (0, "0", None):
        msg = _redact(str(payload.get("msg") or ""), token)
        raise TushareHttpError(msg or f"tushare code {code}")
    data = payload.get("data") or {}
    columns = list(data.get("fields") or [])
    items = data.get("items") or []
    if not columns:
        return pd.DataFrame()
    return pd.DataFrame(items, columns=columns)
