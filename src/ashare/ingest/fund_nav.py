"""Ingest 单位净值 into `fund_nav` (T-1 定稿; read back via factors/nav.py).

NAV 是 T-1 定稿值, 本不该每次 UI 加载现抓。这里把持仓里的场外/债券基金净值落库,
UI/健康分改读 `fund_nav`。`refresh_fund_nav` 幂等: 已到最新交易日的基金跳过, 不空打网络。
"""
from __future__ import annotations
import logging

from ..storage import upsert
from ..sources import AkSource

log = logging.getLogger(__name__)

_NAV_COLS = ["symbol", "nav_date", "nav", "daily_pct"]
_FUND_TYPES = ("otc_fund", "bond_fund")


def ingest_fund_nav(con, ak: AkSource, code: str) -> int:
    df = ak.fund_nav_history(code)
    if df is None or df.empty:
        return 0
    df = df.copy()
    df["symbol"] = str(code).zfill(6)
    return upsert(con, "fund_nav", df[_NAV_COLS], ["symbol", "nav_date"])


def _latest(con, symbol: str):
    r = con.execute("SELECT max(nav_date) FROM fund_nav WHERE symbol=?",
                    [symbol]).fetchone()
    return r[0] if r else None


def _last_trading_day(con):
    r = con.execute(
        "SELECT max(trade_date) FROM calendar WHERE trade_date<=current_date"
    ).fetchone()
    return r[0] if r else None


def refresh_fund_nav(con, pf, force: bool = False) -> dict:
    """Top up NAV for the portfolio's OTC/bond funds. Skips funds already stored
    up to the latest trading day (idempotent, avoids re-pulling eastmoney)."""
    ak = AkSource()
    target = _last_trading_day(con)
    done, skipped, failed = [], [], []
    codes = {h.code for h in pf.holdings
             if h.type in _FUND_TYPES and h.code.isdigit()}
    for code in codes:
        if not force and target is not None:
            lv = _latest(con, code)
            if lv is not None and lv >= target:
                skipped.append(code); continue
        try:
            n = ingest_fund_nav(con, ak, code)
            (done if n else failed).append(code)
        except Exception as e:
            log.warning("fund_nav %s failed: %s", code, e); failed.append(code)
    return {"done": done, "skipped": skipped, "failed": failed}
