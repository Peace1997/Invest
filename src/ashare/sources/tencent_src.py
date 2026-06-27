"""Tencent (腾讯财经) real-time quote source.

For LIVE / intraday decisioning: returns the current snapshot price with its
own exchange timestamp. This is explicitly *provisional* (the value moves while
the market is open) and is NEVER written into the settled daily_bar table — it
feeds the `live` report only.

A-share free quotes from Tencent (qt.gtimg.cn) are real-time (not delayed), and
this host is more reliable than eastmoney's push2. Verified field layout against
the live endpoint (88 fields, ~ separated).
"""
from __future__ import annotations
import logging
from datetime import datetime

import requests
import pandas as pd

from .akshare_src import _ensure_domestic_no_proxy, _exchange_of

log = logging.getLogger(__name__)

_QT_URL = "https://qt.gtimg.cn/q="

# Verified field indices in the ~-separated Tencent quote string.
_F_NAME, _F_CODE, _F_PRICE, _F_PREV, _F_OPEN = 1, 2, 3, 4, 5
_F_TIME, _F_CHANGE, _F_PCT, _F_HIGH, _F_LOW = 30, 31, 32, 33, 34
_F_VOLUME, _F_AMOUNT_WAN, _F_TURNOVER, _F_PE, _F_AMPLITUDE = 6, 37, 38, 39, 43


def _qt_prefix(symbol: str, is_index: bool = False) -> str:
    """6-digit code → tencent-prefixed symbol (sh/sz/bj).

    Indices follow different rules than stocks: 000xxx CSI/SSE indices live on
    Shanghai (sh000300), 399xxx on Shenzhen (sz399006) — whereas a 0-prefixed
    *stock* code is Shenzhen. Caller must flag indices.
    """
    s = str(symbol).zfill(6)
    if is_index:
        prefix = "sz" if s.startswith("399") else "sh"
        return prefix + s
    ex = _exchange_of(s)
    return {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(ex, "sh") + s


def _f(parts: list[str], idx: int) -> str | None:
    return parts[idx] if idx < len(parts) and parts[idx] != "" else None


def _num(parts: list[str], idx: int) -> float | None:
    v = _f(parts, idx)
    try:
        return float(v) if v is not None else None
    except (ValueError, TypeError):
        return None


class TencentSource:
    name = "tencent"

    def __init__(self, timeout: float = 8.0):
        self.timeout = timeout
        _ensure_domestic_no_proxy()

    def spot(self, symbols: list[str], index_symbols: set[str] | None = None) -> pd.DataFrame:
        """Real-time snapshot for the given 6-digit codes.

        index_symbols: codes that are indices (need sh/sz index prefix rules,
        which differ from stock prefix rules — e.g. 000300 is sh, not sz).

        Returns DataFrame[symbol, name, price, prev_close, open, high, low,
        change, pct_chg, volume, amount, turnover, amplitude, pe, quote_time].
        `quote_time` is the exchange timestamp (parsed datetime). Empty on failure.
        """
        if not symbols:
            return pd.DataFrame()
        idx = index_symbols or set()
        query = ",".join(_qt_prefix(s, is_index=(s in idx)) for s in symbols)
        try:
            r = requests.get(_QT_URL + query, timeout=self.timeout)
            r.raise_for_status()
            r.encoding = "gbk"  # tencent returns GBK
            text = r.text
        except Exception as e:
            log.warning("tencent spot fetch failed: %s", e)
            return pd.DataFrame()

        rows = []
        for line in text.strip().split("\n"):
            if '="' not in line:
                continue
            payload = line.split('"')[1]
            parts = payload.split("~")
            if len(parts) < _F_AMPLITUDE + 1:
                continue
            code = _f(parts, _F_CODE)
            if not code:
                continue
            ts_raw = _f(parts, _F_TIME)
            try:
                quote_time = datetime.strptime(ts_raw, "%Y%m%d%H%M%S") if ts_raw else None
            except ValueError:
                quote_time = None
            amount_wan = _num(parts, _F_AMOUNT_WAN)
            rows.append({
                "symbol": code,
                "name": _f(parts, _F_NAME),
                "price": _num(parts, _F_PRICE),
                "prev_close": _num(parts, _F_PREV),
                "open": _num(parts, _F_OPEN),
                "high": _num(parts, _F_HIGH),
                "low": _num(parts, _F_LOW),
                "change": _num(parts, _F_CHANGE),
                "pct_chg": _num(parts, _F_PCT),
                "volume": _num(parts, _F_VOLUME),         # 手
                "amount": amount_wan * 1e4 if amount_wan is not None else None,  # 元
                "turnover": _num(parts, _F_TURNOVER),     # %
                "amplitude": _num(parts, _F_AMPLITUDE),   # %
                "pe": _num(parts, _F_PE),
                "quote_time": quote_time,
            })
        return pd.DataFrame(rows)
