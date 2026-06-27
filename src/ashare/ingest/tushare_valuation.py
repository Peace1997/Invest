"""Ingest 历史 PE/PB/市值 into `valuation_daily` (src='tushare') via daily_basic.

回测的"估值分位"需要每只票多年的 PE/PB 历史。daily_basic 一次调用拿全市场一天(~5300行),
按**周频网格**(每 5 个交易日取一天)覆盖 5 年 ≈ 250 次调用就够算分位——比逐日省 5x,
分位数对采样频率不敏感。total_mv 原始单位万元 → 转亿元(与表注释一致)。
"""
from __future__ import annotations
import logging
import time

import pandas as pd

from ..storage import upsert
from ..sources.tushare_src import get_pro

log = logging.getLogger(__name__)

_COLS = ["symbol", "trade_date", "pe_ttm", "pb", "total_mv", "src"]


def _call(fn, *, retries: int = 3, sleep: float = 1.5, **kw):
    last = None
    for i in range(retries):
        try:
            return fn(**kw)
        except Exception as e:  # noqa: BLE001
            last = e
            if i < retries - 1:
                time.sleep(sleep)
    raise last


def _grid_dates(con, years_back: int, every: int) -> list[str]:
    rows = con.execute(
        "SELECT trade_date FROM calendar WHERE trade_date<=current_date "
        "AND trade_date>=current_date - INTERVAL (?) YEAR ORDER BY trade_date",
        [years_back]).fetchall()
    dates = [r[0] for r in rows][::every]
    return [d.strftime("%Y%m%d") for d in dates]


def ingest_valuation_day(con, trade_date: str) -> int:
    pro = get_pro()
    df = _call(pro.daily_basic, trade_date=trade_date,
               fields="ts_code,pe_ttm,pb,total_mv")
    if df is None or df.empty:
        return 0
    df = df.copy()
    df["symbol"] = df["ts_code"].str[:6]
    df["trade_date"] = pd.to_datetime(trade_date, format="%Y%m%d").date()
    df["total_mv"] = df["total_mv"] / 1e4   # 万元 → 亿元
    df["src"] = "tushare"
    return upsert(con, "valuation_daily", df[_COLS], ["symbol", "trade_date", "src"])


def refresh_valuation_ts(con, trade_dates: list[str]) -> int:
    """每日增量: 拉窗口内每个交易日的全市场 PE/PB/市值 → valuation_daily(幂等)。
    让价值版/回测长期保持新鲜(接续一次性 backfill_valuation_history)。"""
    total = 0
    for d in trade_dates:
        try:
            total += ingest_valuation_day(con, d)
        except Exception as e:  # noqa: BLE001 - 单日失败不阻断其余
            log.warning("valuation_daily 增量 %s 失败: %s", d, e)
    return total


def backfill_valuation_history(con, years_back: int = 5, every: int = 5) -> dict:
    dates = _grid_dates(con, years_back, every)
    out = {"dates": len(dates), "rows": 0, "fail": 0}
    for i, d in enumerate(dates):
        try:
            out["rows"] += ingest_valuation_day(con, d)
        except Exception as e:  # noqa: BLE001
            out["fail"] += 1
            log.warning("daily_basic %s 失败: %s", d, e)
        if (i + 1) % 50 == 0:
            log.info("valuation_history %d/%d", i + 1, len(dates))
    return out
