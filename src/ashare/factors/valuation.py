"""估值分位因子 (ROADMAP Phase 1.3 / 2.4).

Reads the `valuation_daily` warehouse table and computes where today's PE-TTM /
PB sit within their own trailing-N-year history (percentile). This answers
"贵不贵" — the dimension the pure-trend score was missing.

Honesty:
  - PE percentile is only computed over **positive** PE observations; if current
    PE ≤ 0 (亏损) we return pe_pct=None and say so rather than ranking a loss.
  - `years` reflects the *actual* span available, not a fixed claim of 5y.
"""
from __future__ import annotations
from dataclasses import dataclass

import pandas as pd


@dataclass
class ValuationInfo:
    symbol: str
    as_of: object
    pe_ttm: float | None
    pe_pct: float | None     # 0..100 percentile within window (None if PE≤0)
    pb: float | None
    pb_pct: float | None
    years: float             # actual history span used, years
    note: str = ""


def _pct_rank(series: pd.Series, value: float) -> float | None:
    s = series.dropna()
    s = s[s > 0]
    if len(s) < 30 or value is None or value <= 0:
        return None
    return float((s <= value).mean() * 100.0)


def valuation_info(con, symbol: str, years: int = 5) -> ValuationInfo | None:
    key = str(symbol).zfill(6) if str(symbol).isdigit() else str(symbol)
    try:
        df = con.execute(
            "SELECT trade_date, pe_ttm, pb FROM valuation_daily WHERE symbol=? "
            "ORDER BY trade_date", [key]).df()
    except Exception:
        return None
    if df is None or df.empty:
        return None
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    last = df.iloc[-1]
    as_of = last["trade_date"].date()
    cutoff = df["trade_date"].max() - pd.Timedelta(days=int(years * 365.25))
    win = df[df["trade_date"] >= cutoff]
    span_years = (df["trade_date"].max() - df["trade_date"].min()).days / 365.25

    pe = float(last["pe_ttm"]) if pd.notna(last["pe_ttm"]) else None
    pb = float(last["pb"]) if pd.notna(last["pb"]) else None
    pe_pct = _pct_rank(win["pe_ttm"], pe)
    pb_pct = _pct_rank(win["pb"], pb)

    note = ""
    if pe is not None and pe <= 0:
        note = "PE为负(亏损), 不做分位"
    return ValuationInfo(
        symbol=key, as_of=as_of, pe_ttm=pe, pe_pct=pe_pct,
        pb=pb, pb_pct=pb_pct, years=min(years, round(span_years, 1)), note=note,
    )
