"""读取 `fundamentals` 表的质量指标 (DB-first, 最近年报).

写入见 `ingest/fundamentals.py`. 稳健·价值版的质量因子(ROE/盈利增速)从这里读;
未覆盖时返回 None, 打分器优雅降级(只用估值, 不臆造质量分)。
"""
from __future__ import annotations
from dataclasses import dataclass

import pandas as pd


@dataclass
class QualityInfo:
    symbol: str
    report_date: object
    roe: float | None          # 净资产收益率, %
    net_margin: float | None    # 销售净利率, %
    gross_margin: float | None  # 毛利率, %
    debt_ratio: float | None    # 资产负债率, %
    profit_yoy: float | None    # 归母净利润增长率, %
    revenue_yoy: float | None   # 营业总收入增长率, %
    industry: str | None = None # 所处行业(东财)


def quality_info(con, code) -> QualityInfo | None:
    if con is None:
        return None
    try:
        df = con.execute(
            "SELECT report_date, roe, net_margin, gross_margin, debt_ratio, "
            "profit_yoy, revenue_yoy, industry FROM fundamentals WHERE symbol=? "
            "ORDER BY report_date DESC LIMIT 1", [str(code).zfill(6)]).df()
    except Exception:
        return None
    if df is None or df.empty:
        return None
    r = df.iloc[0]
    g = lambda k: float(r[k]) if pd.notna(r[k]) else None
    return QualityInfo(
        symbol=str(code).zfill(6),
        report_date=pd.to_datetime(r["report_date"]).date(),
        roe=g("roe"), net_margin=g("net_margin"), gross_margin=g("gross_margin"),
        debt_ratio=g("debt_ratio"), profit_yoy=g("profit_yoy"),
        revenue_yoy=g("revenue_yoy"),
        industry=(str(r["industry"]) if pd.notna(r["industry"]) else None))
