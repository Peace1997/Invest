"""Ingest 逐季历史基本面 into `fundamentals_ts` via Tushare fina_indicator_vip.

按报告期批量(一期一次调用拿全市场 ~8000 行), 比逐只省 100x。带 ann_date(公告日),
回测取数须 `ann_date <= 当日` 防未来函数。代理端点偶发超时, 故所有调用走 `_call` 重试。
"""
from __future__ import annotations
import logging
import time
from datetime import date

import pandas as pd

from ..storage import upsert
from ..sources.tushare_src import get_pro

log = logging.getLogger(__name__)

_FIELDS = "ts_code,ann_date,end_date,roe,grossprofit_margin,debt_to_assets,netprofit_yoy,or_yoy"
_RENAME = {
    "grossprofit_margin": "gross_margin",
    "debt_to_assets": "debt_ratio",
    "netprofit_yoy": "profit_yoy",
    "or_yoy": "revenue_yoy",
}
_COLS = ["symbol", "end_date", "ann_date", "roe", "gross_margin",
         "debt_ratio", "profit_yoy", "revenue_yoy"]


def _call(fn, *, retries: int = 3, sleep: float = 1.5, **kw) -> pd.DataFrame:
    """Tushare 调用 + 重试(代理偶发 read timeout / max retries)。"""
    last = None
    for i in range(retries):
        try:
            return fn(**kw)
        except Exception as e:  # noqa: BLE001 - 网络层各类异常都重试
            last = e
            if i < retries - 1:
                time.sleep(sleep)
    raise last


def quarter_periods(years_back: int, today: date | None = None) -> list[str]:
    """近 N 年的所有季末报告期 'YYYYMMDD'(0331/0630/0930/1231), 倒序。"""
    t = today or date.today()
    ends = []
    for y in range(t.year, t.year - years_back - 1, -1):
        for mmdd in ("1231", "0930", "0630", "0331"):
            p = f"{y}{mmdd}"
            if p <= t.strftime("%Y%m%d"):
                ends.append(p)
    return ends


def ingest_fina_indicator(con, period: str) -> int:
    """单个报告期全市场财务指标 → `fundamentals_ts`。返回落库行数。"""
    pro = get_pro()
    df = _call(pro.fina_indicator_vip, period=period, fields=_FIELDS)
    if df is None or df.empty:
        return 0
    df = df.rename(columns=_RENAME)
    df["symbol"] = df["ts_code"].str[:6]
    df["end_date"] = pd.to_datetime(df["end_date"], format="%Y%m%d").dt.date
    df["ann_date"] = pd.to_datetime(df["ann_date"], format="%Y%m%d", errors="coerce").dt.date
    # 同一 (symbol,end_date) 偶有多版(修正), 留公告日最新的一版
    df = df.sort_values("ann_date").drop_duplicates(["symbol", "end_date"], keep="last")
    return upsert(con, "fundamentals_ts", df[_COLS], ["symbol", "end_date"])


def refresh_fundamentals_ts(con, n_periods: int = 2) -> dict:
    """每日增量: 刷新最近 n_periods 个报告期。财报在季内陆续披露, 幂等 upsert
    会把当天新公告的那批票补进 `fundamentals_ts`(接续一次性 backfill)。"""
    out = {}
    for p in quarter_periods(1)[:n_periods]:
        try:
            out[p] = ingest_fina_indicator(con, p)
        except Exception as e:  # noqa: BLE001 - 单期失败不阻断其余
            log.warning("fundamentals_ts 增量 %s 失败: %s", p, e)
            out[p] = -1
    return out


def backfill_fundamentals_ts(con, years_back: int = 5) -> dict:
    """回填近 N 年逐季历史。返回 {period: rows}。"""
    out = {}
    for p in quarter_periods(years_back):
        try:
            n = ingest_fina_indicator(con, p)
            out[p] = n
            log.info("fundamentals_ts %s: %d 行", p, n)
        except Exception as e:  # noqa: BLE001
            log.warning("fundamentals_ts %s 失败: %s", p, e)
            out[p] = -1
    return out
