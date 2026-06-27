"""Ingest 全市场基本面/质量指标 into `fundamentals` (read via factors/quality.py).

东财业绩报表 `stock_yjbb_em` 一次调用即拿全市场 ROE/同比增速/毛利率/所处行业 —— 覆盖
全主板比逐只 `stock_financial_abstract` 便宜得多, 且白送行业(供稳健版行业分散用)。
用最近年报(YYYY1231)→ ROE 已年化, 可比。净利率/负债率业绩报表无, 留空(本就不计分)。
"""
from __future__ import annotations
import logging
from datetime import date

import pandas as pd

from ..storage import upsert
from ..sources import AkSource

log = logging.getLogger(__name__)

_COLS = ["symbol", "report_date", "roe", "net_margin", "gross_margin",
         "debt_ratio", "profit_yoy", "revenue_yoy", "industry"]


def latest_annual(today: date | None = None) -> str:
    """Most recent fully-published annual report period as 'YYYY1231'.
    年报在次年 1-4 月披露完, 所以 5 月起用上一年, 否则用上上年(保守)。"""
    t = today or date.today()
    y = t.year - 1 if t.month >= 5 else t.year - 2
    return f"{y}1231"


def ingest_market_performance(con, report_date: str | None = None) -> int:
    """One-call whole-market fundamentals → `fundamentals`. Returns rows upserted."""
    ak = AkSource()
    rd = report_date or latest_annual()
    df = ak.market_performance(rd)
    if df is None or df.empty:
        return 0
    df = df.copy()
    df["report_date"] = pd.to_datetime(rd).date()
    df["net_margin"] = pd.NA       # 业绩报表无, 仅 financial_abstract 有
    df["debt_ratio"] = pd.NA
    return upsert(con, "fundamentals", df[_COLS], ["symbol", "report_date"])
