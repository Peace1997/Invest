"""短期反转因子验证 —— A股动量为负, 故"买近期超跌"可能有正 edge。

测法(横截面, 防未来函数): 每隔 H 个交易日为一个调仓点, 当日按"过去 L 日涨幅"对全主板
个股排序, 度量其 t→t+H 前瞻收益。报告:
  - IC(过去涨幅 vs 前瞻收益的 Spearman): **为负 = 反转**(过去跌的未来涨), 越负反转越强。
  - 十分位: 按过去涨幅从低(超跌)到高(超涨)分10档前瞻收益 —— 反转有效则 D1(超跌)>D10(超涨)。
  - 反转多空: 买超跌档(D1)、卖超涨档(D10)的平均前瞻收益差。

诚实边界: 幸存者偏差(退市股缺失)偏乐观; 无滑点/成本; ST 未历史剔除; 主板60/00。
edge 是概率非保证。前瞻窗非重叠(每H天取一个点)以避免重复计数。
"""
from __future__ import annotations
import logging

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)


def _load_close(con, years_back: int) -> pd.DataFrame:
    max_d = con.execute("SELECT max(trade_date) FROM daily_bar").fetchone()[0]
    cutoff = max_d - pd.Timedelta(days=int(years_back * 365.25))
    df = con.execute(
        "SELECT symbol, trade_date, close FROM daily_bar "
        "WHERE type='stock' AND (symbol LIKE '60%' OR symbol LIKE '00%') "
        "AND trade_date >= ? ORDER BY symbol, trade_date", [cutoff]).df()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    return df


def _one(df: pd.DataFrame, lookback: int, horizon: int) -> dict:
    g = df.groupby("symbol", sort=False)
    sig = g["close"].shift(0) / g["close"].shift(lookback) - 1.0   # 过去L日涨幅
    fwd = g["close"].shift(-horizon) / g["close"].shift(0) - 1.0   # 前瞻H日收益
    w = pd.DataFrame({"trade_date": df["trade_date"], "sig": sig.values, "fwd": fwd.values}).dropna()
    # 非重叠调仓点: 每 horizon 个交易日取一个
    days = np.sort(w["trade_date"].unique())[::horizon]
    w = w[w["trade_date"].isin(days)]
    if w.empty:
        return {"n_days": 0}
    ics = w.groupby("trade_date").apply(
        lambda x: x["sig"].rank().corr(x["fwd"].rank()) if len(x) >= 20 else np.nan,
        include_groups=False).dropna()
    w = w.copy()
    w["d"] = w.groupby("trade_date", group_keys=False)["sig"].apply(
        lambda s: pd.qcut(s, 10, labels=False, duplicates="drop"))
    dec = w.dropna(subset=["d"]).groupby("d")["fwd"].mean() * 100
    d1 = dec.get(0); d10 = dec.get(9)
    return {"n_days": int(len(ics)), "ic": float(ics.mean()),
            "ic_neg_ratio": float((ics < 0).mean()),
            "d1": (float(d1) if d1 is not None else None),
            "d10": (float(d10) if d10 is not None else None),
            "ls": (float(d1 - d10) if (d1 is not None and d10 is not None) else None)}


def run_reversal_backtest(con, years_back: int = 3,
                          combos: tuple = ((5, 5), (10, 10), (20, 20), (60, 20))) -> dict:
    df = _load_close(con, years_back)
    if df.empty:
        return {"ok": False, "text": "无数据。"}
    d0, d1 = df["trade_date"].min().date(), df["trade_date"].max().date()
    rows = [(lb, h, _one(df, lb, h)) for lb, h in combos]

    L = ["╔" + "═" * 62,
         f"║ 短期反转因子验证 · 主板60/00 · {d0}~{d1} · 近{years_back}年",
         "║ IC=过去涨幅vs前瞻收益(Spearman): 为负=反转(超跌者未来涨)",
         "╠" + "═" * 62,
         "║ 回看L/持有H   IC均值  IC<0比例   D1超跌%   D10超涨%   多空(D1-D10)",
         "╟" + "─" * 62]
    for lb, h, r in rows:
        if r["n_days"] == 0:
            L.append(f"║ L{lb:>2}/H{h:<2}        样本不足")
            continue
        d1v = f"{r['d1']:+5.2f}" if r["d1"] is not None else "  n/a"
        d10v = f"{r['d10']:+5.2f}" if r["d10"] is not None else "  n/a"
        lsv = f"{r['ls']:+5.2f}" if r["ls"] is not None else " n/a"
        flag = "✅反转" if r["ic"] < -0.02 else "⚠动量" if r["ic"] > 0.02 else "·弱"
        L.append(f"║ L{lb:>2}/H{h:<2}  {r['ic']:+.3f}   {r['ic_neg_ratio']*100:4.0f}%    "
                 f"{d1v}    {d10v}     {lsv}  {flag}")
    L += ["╚" + "═" * 62,
          "判读: IC明显<0 且 D1>D10 且 多空>0 → 反转有正edge, 可做'买超跌'因子;",
          "      多空价差是每H天滚动的平均收益差(%), 非年化; 未计滑点/成本/幸存者偏差。"]
    return {"ok": True, "rows": rows, "text": "\n".join(L)}
