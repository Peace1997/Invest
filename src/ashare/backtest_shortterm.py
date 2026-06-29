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
from datetime import time as dtime

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


# ───────── 分钟精确版尾盘回测: 真实 14:30 入场, 取代上面的 15:00 近似 ─────────
# 数据源 = minute_bar(盘后回填的历史分钟). 仅覆盖已 backfill-min 的个股 → 诚实标注覆盖范围.
_CUT_1430 = dtime(14, 30)


def _minute_endday_features(con, freq: str, years_back: int):
    """每 (symbol, 交易日) 在 14:30 的真实盘中快照(只用 ≤14:30 的分钟 bar)。
    返回 DataFrame[symbol, d, open_, price_1430, pos_1430, cum_amt_1430] 或 None。"""
    max_t = con.execute(
        "SELECT max(trade_time) FROM minute_bar WHERE freq=? AND type='stock'", [freq]).fetchone()[0]
    if max_t is None:
        return None
    cutoff = (pd.Timestamp(max_t).normalize()
              - pd.Timedelta(days=int(years_back * 365.25))).to_pydatetime()
    df = con.execute(
        "SELECT symbol, trade_time, open, high, low, close, amount "
        "FROM minute_bar WHERE freq=? AND type='stock' AND trade_time>=? "
        "ORDER BY symbol, trade_time", [freq, cutoff]).df()
    if df.empty:
        return None
    df["trade_time"] = pd.to_datetime(df["trade_time"])
    df["d"] = df["trade_time"].dt.normalize()
    tt = df["trade_time"].dt.time
    up = df[tt <= _CUT_1430]                       # 截至 14:30(含)
    if up.empty:
        return None
    agg = up.groupby(["symbol", "d"]).agg(
        open_=("open", "first"), hi=("high", "max"),
        lo=("low", "min"), cum_amt_1430=("amount", "sum"))
    bar = df[tt == _CUT_1430].groupby(["symbol", "d"])["close"].first().rename("price_1430")
    out = agg.join(bar, how="inner").reset_index()  # 无 14:30 整点 bar 的(如60min)自然丢弃
    rng = (out["hi"] - out["lo"]).replace(0, np.nan)
    out["pos_1430"] = (out["price_1430"] - out["lo"]) / rng
    # 今开不从分钟首bar(09:35)推断(语义依赖K线定义), 改由 run 函数 join daily_bar.open
    return out[["symbol", "d", "price_1430", "pos_1430", "cum_amt_1430"]]


_MIN_SYMS_FOR_LABEL = 20       # 覆盖个股数阈值: 不足则不下"有正超额"定性结论
_MIN_SIGNALS_FOR_LABEL = 30    # 信号样本阈值: 同上


def run_endday_backtest_minute(con, years_back: int = 3, freq: str = "5min") -> dict:
    """尾盘策略·分钟精确版: 真实 14:30 入场 + 截至14:30的收盘位置/同时段放量, 取代日线15:00近似。
    覆盖 = minute_bar 里有该 freq 的个股(诚实标注; 扩面需先 `ashare backfill-min` 更多标的)。"""
    if freq not in ("1min", "5min", "15min", "30min"):
        return {"ok": False, "text": f"{freq} 无 14:30 整点 bar, 尾盘回测请用 1/5/15/30min。"}
    feat = _minute_endday_features(con, freq, years_back)
    if feat is None or feat.empty:
        return {"ok": False, "text": f"无分钟数据({freq})。先 `ashare backfill-min` 落库再回测。"}

    # 同时段放量分母 = 昨日(相邻交易日)截至14:30累计额。用 calendar 求前一交易日精确 merge,
    # 避免分钟缺日时 shift 取到非相邻日; 缺前一交易日数据则置 NaN(该样本被滤掉)。
    cal = con.execute("SELECT trade_date FROM calendar ORDER BY trade_date").df()
    if not cal.empty:
        cal["trade_date"] = pd.to_datetime(cal["trade_date"])
        prev_map = dict(zip(cal["trade_date"], cal["trade_date"].shift(1)))
        feat["prev_d"] = feat["d"].map(prev_map)
    else:
        feat["prev_d"] = pd.NaT
    prev_amt = feat[["symbol", "d", "cum_amt_1430"]].rename(
        columns={"d": "prev_d", "cum_amt_1430": "prev_cum_amt_1430"})
    feat = feat.merge(prev_amt, on=["symbol", "prev_d"], how="left")

    syms = feat["symbol"].unique().tolist()
    qs = ",".join(["?"] * len(syms))
    daily = con.execute(
        f"SELECT symbol, trade_date, open, close FROM daily_bar "
        f"WHERE type='stock' AND symbol IN ({qs}) ORDER BY symbol, trade_date", syms).df()
    if daily.empty:
        return {"ok": False, "text": "覆盖个股缺日线(需 daily_bar 提供今开/昨收/次日收)。"}
    daily["trade_date"] = pd.to_datetime(daily["trade_date"])
    g = daily.groupby("symbol", sort=False)
    daily["prev_close"] = g["close"].shift(1)
    daily["next_close"] = g["close"].shift(-1)
    daily["uni_t1"] = daily["next_close"] / daily["close"] - 1.0   # 覆盖内个股 T+1 基线

    m = feat.merge(daily, left_on=["symbol", "d"], right_on=["symbol", "trade_date"], how="inner")
    if m.empty:
        return {"ok": False, "text": "分钟与日线无法对齐(交易日不匹配)。"}
    m["pct_1430"] = m["price_1430"] / m["prev_close"] - 1.0
    m["amt_ratio_1430"] = m["cum_amt_1430"] / m["prev_cum_amt_1430"].replace(0, np.nan)
    m["ret_t1"] = m["next_close"] / m["price_1430"] - 1.0          # 14:30 买 → 次日收盘卖

    end = m[(m["pct_1430"] >= 0.03) & (m["pct_1430"] <= 0.08)
            & (m["pos_1430"] >= 0.5) & (m["price_1430"] >= m["open"])   # 强于今开(日线开盘价)
            & (m["amt_ratio_1430"] >= 1.0)].copy()                      # 同时段放量
    s = _stats(end["ret_t1"])
    day_base = m.groupby("d")["uni_t1"].mean()
    end["base"] = end["d"].map(day_base)
    excess = (end["ret_t1"] - end["base"]).dropna()
    ex = float(excess.mean()) if len(excess) else float("nan")

    enough = len(syms) >= _MIN_SYMS_FOR_LABEL and s["n"] >= _MIN_SIGNALS_FOR_LABEL
    if not enough:
        tag = "⚠ 覆盖/样本不足, 不做定性判断"
    elif ex > 0.0005:
        tag = "✅ 有正超额"
    elif ex < -0.0005:
        tag = "❌ 无/为负"
    else:
        tag = "≈0 无效"

    d0, d1 = m["d"].min().date(), m["d"].max().date()
    L = ["╔" + "═" * 60,
         f"║ 尾盘策略·分钟精确版({freq}) · {len(syms)}只覆盖个股 · {d0}~{d1}",
         "║ 真实14:30入场(取代15:00近似): 涨幅3~8%@14:30 + 收高位@14:30 + 强于今开 + 同时段放量",
         "╠" + "═" * 60,
         f"║ 信号样本 {s['n']} · 胜率(次日收>14:30) {s['win']*100:5.1f}% · "
         f"均值 {s['mean']*100:+.2f}% · 中位 {s['median']*100:+.2f}%",
         f"║ ➤ 相对覆盖内个股当日T+1超额: {ex*100:+.2f}%  {tag}",
         "╚" + "═" * 60,
         f"注: 覆盖仅 minute_bar 已落库的 {len(syms)} 只(来源=持仓+自选, 非历史全主板, 有选择偏差);",
         f"    定性判断需覆盖≥{_MIN_SYMS_FOR_LABEL}只且信号≥{_MIN_SIGNALS_FOR_LABEL}; "
         "14:30→次日收, 理想成交; 幸存者偏差偏乐观; edge 是概率非保证。"]
    return {"ok": True, "n": s["n"], "win": s["win"], "excess": ex,
            "enough": enough, "text": "\n".join(L)}
