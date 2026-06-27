"""读取 `fund_nav` 仓库表的单位净值序列 (DB-first).

写入见 `ingest/fund_nav.py`. UI/健康分通过这里读库(毫秒级), 仅在库里没有该基金时
才由调用方回退到现抓 eastmoney —— 把"每次加载现抓 13 只"变成"读库 + 偶发兜底"。
"""
from __future__ import annotations

import pandas as pd


def nav_history(con, code) -> pd.DataFrame | None:
    """Ascending NAV series from the warehouse. Cols: nav_date, nav, daily_pct.
    None if the fund has no stored NAV (caller may fall back to a live fetch)."""
    if con is None:
        return None
    try:
        df = con.execute(
            "SELECT nav_date, nav, daily_pct FROM fund_nav WHERE symbol=? "
            "ORDER BY nav_date", [str(code).zfill(6)]).df()
    except Exception:
        return None
    if df is None or df.empty:
        return None
    df["nav_date"] = pd.to_datetime(df["nav_date"]).dt.date
    return df


def nav_latest(con, code) -> dict | None:
    """Latest stored unit NAV: {nav_date, nav, daily_pct} or None."""
    df = nav_history(con, code)
    if df is None or df.empty:
        return None
    last = df.iloc[-1]
    return {
        "nav_date": last["nav_date"],
        "nav": float(last["nav"]),
        "daily_pct": float(last["daily_pct"]) if pd.notna(last["daily_pct"]) else None,
    }
