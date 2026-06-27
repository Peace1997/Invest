"""短线策略(竞价/尾盘)的日线粗验证 —— 把"感觉好用"变成可量化的胜率/盈亏。

诚实边界(务必随结果一起读):
  - **日线近似**: 竞价的"高开"= 当日开盘价(集合竞价结果), 精确; 但"竞价放量"需分钟/tick,
    本验证用不了 → 只测了 高开+昨日涨停 的价格信号。尾盘策略在 14:30 动手, 这里用 15:00
    收盘近似(收盘位置/涨幅/放量都用全天值), 故尾盘是**近似**, 精确需分钟数据。
  - **幸存者偏差**: 已退市股不在 daily_bar → 结果偏乐观。
  - **ST 未历史剔除**: daily_bar 无历史名称, 无法逐日剔 ST(实盘是剔的) → 略有噪声。
  - **无滑点/冲击成本/涨停打不进**: 实盘短线这些极重要, 这里是理想成交 → 结果偏乐观。
  - edge 是概率(胜率/均值), 非保证。

判读: 重点看"相对全市场基线的超额" —— 若选出来的票 T+1 收益并不比"当天随便买一只主板股"
更高(超额≈0 或为负), 说明这套短线选择**没有真实 alpha**, 体感好用是错觉。
"""
from __future__ import annotations
import logging

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)


def _load(con, years_back: int) -> pd.DataFrame:
    max_d = con.execute("SELECT max(trade_date) FROM daily_bar").fetchone()[0]
    cutoff = max_d - pd.Timedelta(days=int(years_back * 365.25))
    df = con.execute(
        "SELECT symbol, trade_date, open, high, low, close, amount, pct_chg "
        "FROM daily_bar WHERE type='stock' AND (symbol LIKE '60%' OR symbol LIKE '00%') "
        "AND trade_date >= ? ORDER BY symbol, trade_date", [cutoff]).df()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    for c in ("open", "high", "low", "close", "amount", "pct_chg"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    g = df.groupby("symbol", sort=False)
    df["prev_close"] = g["close"].shift(1)
    df["prev_pct"] = g["pct_chg"].shift(1)
    df["prev_amount"] = g["amount"].shift(1)
    df["next_open"] = g["open"].shift(-1)
    df["next_close"] = g["close"].shift(-1)
    return df


def _stats(r: pd.Series) -> dict:
    r = r.dropna()
    return {"n": int(len(r)), "win": float((r > 0).mean()) if len(r) else float("nan"),
            "mean": float(r.mean()) if len(r) else float("nan"),
            "median": float(r.median()) if len(r) else float("nan")}


def _baseline_excess(picks: pd.DataFrame, df: pd.DataFrame, ret_col: str, base_col: str) -> float:
    """选出的票相对'当天全市场该收益的均值'的超额(隔离掉大盘当天涨跌)。"""
    day_base = df.groupby("trade_date")[base_col].mean()
    merged = picks[["trade_date", ret_col]].dropna().copy()
    merged["base"] = merged["trade_date"].map(day_base)
    ex = (merged[ret_col] - merged["base"]).dropna()
    return float(ex.mean()) if len(ex) else float("nan")


def run_shortterm_backtest(con, years_back: int = 3) -> dict:
    df = _load(con, years_back)
    if df.empty:
        return {"ok": False, "text": "无数据。"}
    d0, d1 = df["trade_date"].min().date(), df["trade_date"].max().date()

    # 全市场 T+1 基线收益(用于算超额)
    df["uni_t1_close"] = df["next_close"] / df["close"] - 1.0

    # ── 竞价(premarket)代理: 高开1~9.7% + 昨日涨停; 入场=开盘价 ──
    gap = df["open"] / df["prev_close"] - 1.0
    prem = df[(gap >= 0.01) & (gap <= 0.097) & (df["prev_pct"] >= 9.8)
              & (df["open"].between(2, 200))].copy()
    prem["r_intraday"] = prem["close"] / prem["open"] - 1.0      # 开盘买, 当日收盘卖
    prem["r_t1"] = prem["next_close"] / prem["open"] - 1.0       # 开盘买, 次日收盘卖
    p_intra, p_t1 = _stats(prem["r_intraday"]), _stats(prem["r_t1"])
    # 超额: 竞价票次日收益 vs 当天全市场次日收益(都以收盘计, 可比)
    prem["r_t1_close2close"] = prem["next_close"] / prem["close"] - 1.0
    p_excess = _baseline_excess(prem, df, "r_t1_close2close", "uni_t1_close")

    # ── 尾盘(endday)代理: 涨幅3~8% + 收盘位≥0.5 + 强于今开 + 放量; 入场=收盘 ──
    rng = (df["high"] - df["low"]).replace(0, np.nan)
    pos = (df["close"] - df["low"]) / rng
    vr = df["amount"] / df["prev_amount"].replace(0, np.nan)
    end = df[(df["pct_chg"] >= 3) & (df["pct_chg"] <= 8) & (pos >= 0.5)
             & (df["close"] >= df["open"]) & (df["close"].between(2, 200))].copy()
    end["vr"] = vr.loc[end.index]
    end = end[end["vr"] >= 1.0]                                  # 明显放量(当日额≥昨日额)
    end["r_t1_close"] = end["next_close"] / end["close"] - 1.0   # 收盘买, 次日收盘卖
    end["r_t1_open"] = end["next_open"] / end["close"] - 1.0     # 收盘买, 次日开盘卖(博高开)
    e_close, e_open = _stats(end["r_t1_close"]), _stats(end["r_t1_open"])
    e_excess = _baseline_excess(end, df, "r_t1_close", "uni_t1_close")

    uni_t1 = _stats(df["uni_t1_close"])

    def fmt(s):
        return (f"样本{s['n']:>6} · 胜率{s['win']*100:5.1f}% · 均值{s['mean']*100:+6.2f}% "
                f"· 中位{s['median']*100:+6.2f}%")

    L = ["╔" + "═" * 60,
         f"║ 短线策略·日线粗验证 · 主板60/00 · {d0}~{d1} · 近{years_back}年",
         "╠" + "═" * 60,
         "║ 〔基线〕全市场个股 T+1(收→次日收)",
         f"║   {fmt(uni_t1)}",
         "╟" + "─" * 60,
         "║ 〔竞价〕高开1~9.7% + 昨日涨停, 开盘价入场:",
         f"║   日内(开→收)   {fmt(p_intra)}",
         f"║   次日(开→次收) {fmt(p_t1)}",
         f"║   ➤ 相对全市场超额(次日收→收): {p_excess*100:+.2f}%  "
         f"{'✅ 有正超额' if p_excess>0.0005 else '❌ 无超额/为负' if p_excess<-0.0005 else '≈0 无效'}",
         "╟" + "─" * 60,
         "║ 〔尾盘〕涨幅3~8%+收高位+强于今开+放量, 收盘价入场:",
         f"║   次日(收→次收) {fmt(e_close)}",
         f"║   次日(收→次开) {fmt(e_open)}",
         f"║   ➤ 相对全市场超额(次日收→收): {e_excess*100:+.2f}%  "
         f"{'✅ 有正超额' if e_excess>0.0005 else '❌ 无超额/为负' if e_excess<-0.0005 else '≈0 无效'}",
         "╚" + "═" * 60,
         "判读: 胜率看似>50% 多半只是'大盘当天涨'带的, 关键看 ➤超额; 超额≈0/为负 = 选择无 alpha。",
         "注: 理想成交(无滑点/涨停打不进/冲击成本), 实盘短线这些吃掉很多 → 真实更差; 幸存者偏差偏乐观。",
         "    竞价'放量'信号、尾盘14:30精确位置需分钟数据, 本验证未覆盖(故偏粗)。"]
    return {"ok": True, "prem_excess": p_excess, "end_excess": e_excess,
            "text": "\n".join(L)}
