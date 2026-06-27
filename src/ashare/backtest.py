"""规则回测: 衡量推荐打分是否真有 edge(胜率/超额/分层单调/IC), 严防未来函数。

每个调仓日 t 只用 ≤t 的数据(bars/估值)算分, 度量 t→t+H 的前瞻收益, 与沪深300 比超额。
质量因子(ROE/增速)只有最新一期 → 用它回测过去=未来函数, 故 v1 只测 动量/价值。

诚实边界:
  - 幸存者偏差: 已退市股不在 daily_bar, 结果偏乐观(无法用现有数据消除)。
  - 价值版受 valuation_daily 覆盖限制(默认仅沪深300有估值历史)。
  - 前瞻收益按个股自身交易日 +H 算; 长期停牌会有轻微错位。
  - 回测出的 edge 是概率(胜率), 非保证; 已用分层单调+IC 防止只看单一指标。
"""
from __future__ import annotations
import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

from .factors.technical import trend_features
from .factors.valuation import ValuationInfo, _pct_rank
from .factors.quality import QualityInfo
from .signals.scoring import score_components, score_value_components, score_reversal_components

log = logging.getLogger(__name__)


def _universe(con, universe: str, min_obs: int) -> tuple[list[str], str]:
    have = set(con.execute(
        "SELECT symbol FROM daily_bar WHERE type='stock' "
        "GROUP BY symbol HAVING count(*)>=?", [min_obs]).df()["symbol"])
    fname = "data/main_board.json" if universe == "main" else "data/csi300_main.json"
    f = Path(fname)
    if f.exists():
        codes = [str(c).zfill(6) for c in json.loads(f.read_text())]
        return [c for c in codes if c in have], ("全主板" if universe == "main" else "沪深300主板")
    return sorted(have), "库内个股"


def _load_closes(con, codes: list[str]) -> dict:
    qs = ",".join(["?"] * len(codes))
    df = con.execute(
        f"SELECT symbol, trade_date, close FROM daily_bar_adj WHERE symbol IN ({qs}) "
        "ORDER BY symbol, trade_date", codes).df()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return {code: (g["trade_date"].values, g["close"].to_numpy(dtype=float))
            for code, g in df.groupby("symbol")}


def _load_vals(con, codes: list[str]) -> dict:
    qs = ",".join(["?"] * len(codes))
    try:
        df = con.execute(
            f"SELECT symbol, trade_date, pe_ttm, pb FROM valuation_daily_canon WHERE symbol IN ({qs}) "
            "ORDER BY symbol, trade_date", codes).df()
    except Exception:
        return {}
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return {code: g.reset_index(drop=True) for code, g in df.groupby("symbol")}


def _load_fund_ts(con, codes: list[str]) -> dict:
    """逐季历史财务(带 ann_date), 供回测做防未来函数的质量分。"""
    qs = ",".join(["?"] * len(codes))
    try:
        df = con.execute(
            f"SELECT symbol, end_date, ann_date, roe, gross_margin, debt_ratio, "
            f"profit_yoy, revenue_yoy FROM fundamentals_ts WHERE symbol IN ({qs}) "
            "ORDER BY symbol, ann_date", codes).df()
    except Exception:
        return {}
    if df.empty:
        return {}
    df["ann_date"] = pd.to_datetime(df["ann_date"])
    return {code: g.reset_index(drop=True) for code, g in df.groupby("symbol")}


def _qual_asof(fdf, as_of) -> QualityInfo | None:
    """as_of 当日**已公告**的最新一期财务(ann_date<=as_of, 防未来函数)。"""
    if fdf is None:
        return None
    sub = fdf[fdf["ann_date"] <= as_of]
    if sub.empty:
        return None
    r = sub.iloc[-1]   # 已按 ann_date 升序; 取最近公告
    g = lambda k: float(r[k]) if pd.notna(r[k]) else None
    return QualityInfo(symbol="", report_date=r["end_date"], roe=g("roe"),
                       net_margin=None, gross_margin=g("gross_margin"),
                       debt_ratio=g("debt_ratio"), profit_yoy=g("profit_yoy"),
                       revenue_yoy=g("revenue_yoy"))


def _val_asof(vdf, as_of, years: int = 5) -> ValuationInfo | None:
    """历史时点估值分位(只用 ≤as_of 的数据, 防未来函数)。"""
    if vdf is None:
        return None
    sub = vdf[vdf["trade_date"] <= as_of]
    if sub.empty:
        return None
    last = sub.iloc[-1]
    pe = float(last["pe_ttm"]) if pd.notna(last["pe_ttm"]) else None
    pb = float(last["pb"]) if pd.notna(last["pb"]) else None
    win = sub[sub["trade_date"] >= as_of - pd.Timedelta(days=int(years * 365.25))]
    return ValuationInfo(
        symbol="", as_of=as_of.date(), pe_ttm=pe,
        pe_pct=_pct_rank(win["pe_ttm"], pe), pb=pb, pb_pct=_pct_rank(win["pb"], pb),
        years=years, note=("PE为负(亏损), 不做分位" if pe is not None and pe <= 0 else ""))


def run_backtest(con, style: str = "momentum", years_back: int = 3, horizon: int = 60,
                 rebalance: int = 21, top_n: int = 12, min_obs: int = 120,
                 universe: str = "csi300", benchmark: str = "000300",
                 use_quality: bool = False) -> dict:
    cal = np.array([pd.Timestamp(r[0]) for r in con.execute(
        "SELECT trade_date FROM calendar WHERE trade_date<=current_date "
        "ORDER BY trade_date").fetchall()])
    start = cal[-1] - pd.Timedelta(days=int(years_back * 365.25))
    reb_dates = [t for t in cal if t >= start][::rebalance]

    codes, uni_desc = _universe(con, universe, min_obs)
    closes = _load_closes(con, codes)
    vals = _load_vals(con, codes) if style == "value" else {}
    funds = _load_fund_ts(con, codes) if (style == "value" and use_quality) else {}

    b = con.execute("SELECT trade_date, close FROM index_bar WHERE symbol=? "
                    "ORDER BY trade_date", [benchmark]).df()
    b["trade_date"] = pd.to_datetime(b["trade_date"])
    bdates, bclose = b["trade_date"].values, b["close"].to_numpy(dtype=float)

    def _fwd(dates, arr, t):
        i = int(np.searchsorted(dates, t, side="right")) - 1
        j = i + horizon
        if i < 0 or j >= len(arr):
            return None, None
        return i, arr[j] / arr[i] - 1.0

    records = []
    for t in reb_dates:
        _, bret = _fwd(bdates, bclose, t)
        if bret is None:
            continue
        for code in codes:
            cd = closes.get(code)
            if cd is None:
                continue
            dates, cl = cd
            i = int(np.searchsorted(dates, t, side="right")) - 1
            if i < min_obs - 1 or i + horizon >= len(cl):
                continue
            fwd = cl[i + horizon] / cl[i] - 1.0
            f = trend_features(pd.Series(cl[:i + 1]))
            if f is None:
                continue
            if style == "value":
                val = _val_asof(vals.get(code), t)
                if val is None:
                    continue
                if val.pe_ttm is not None and val.pe_ttm <= 0:   # 亏损剔除(同实盘)
                    continue
                qual = _qual_asof(funds.get(code), t) if use_quality else None
                if use_quality and qual is None:   # 要求质量却无财务 → 跳过(可比性)
                    continue
                score, _, _ = score_value_components(f, val=val, qual=qual)
            elif style == "reversal":
                score, _, _ = score_reversal_components(f)
            else:
                score, _, _ = score_components(f)
            records.append((t, code, score, fwd, fwd - bret))

    return _summary(records, top_n, style, uni_desc, horizon, rebalance, len(reb_dates))


def _summary(records, top_n, style, uni_desc, horizon, rebalance, n_reb) -> dict:
    if not records:
        return {"ok": False, "text": "回测无数据(universe/估值覆盖不足或历史太短)。"}
    df = pd.DataFrame(records, columns=["date", "code", "score", "fwd", "excess"])

    top = df.sort_values("score", ascending=False).groupby("date").head(top_n)
    hit = float((top["excess"] > 0).mean())
    avg_excess = float(top["excess"].mean())
    avg_raw = float(top["fwd"].mean())

    # 5 分层(每个调仓日内按分数分5档), 看是否单调
    df = df.copy()
    df["q"] = df.groupby("date", group_keys=False)["score"].apply(
        lambda s: pd.qcut(s, 5, labels=False, duplicates="drop"))
    qtab = (df.dropna(subset=["q"]).groupby("q")["fwd"].mean() * 100)
    qvals = [qtab.get(k) for k in range(5)]
    spread = (qvals[4] - qvals[0]) if (qvals[0] is not None and qvals[4] is not None) else None
    monotonic = all(qvals[k] is not None and qvals[k + 1] is not None and qvals[k + 1] >= qvals[k]
                    for k in range(4))

    # Spearman = Pearson of ranks (avoids a scipy dependency)
    ics = df.groupby("date").apply(
        lambda g: g["score"].rank().corr(g["fwd"].rank()), include_groups=False)
    ic_mean = float(ics.mean()); ic_pos = float((ics > 0).mean())

    style_cn = {"momentum": "动量版", "value": "价值版", "reversal": "反转·超跌版"}.get(style, style)
    L = ["╔" + "═" * 56,
         f"║ 回测 · {style_cn} · {uni_desc}",
         f"║ 调仓每{rebalance}个交易日 · 持有{horizon}个交易日 · 共{n_reb}个调仓点 · 基准沪深300",
         "╠" + "═" * 56,
         f"║ 【Top{top_n} 选股】胜率(跑赢基准) {hit*100:5.1f}%",
         f"║            平均超额收益 {avg_excess*100:+5.2f}%   平均绝对收益 {avg_raw*100:+5.2f}%",
         "║ 【分层 · 低分→高分 各档{}日平均收益%】".format(horizon),
         "║   " + "  ".join(f"Q{k+1}:{(qvals[k] if qvals[k] is not None else float('nan')):+5.1f}"
                            for k in range(5)),
         f"║   高低档价差 {spread:+.1f}%  ·  单调递增: {'✅是' if monotonic else '❌否'}"
         if spread is not None else "║   分层不足",
         f"║ 【IC】平均 {ic_mean:+.3f}  ·  为正比例 {ic_pos*100:.0f}%   (|IC|>0.03 算有效)",
         "╚" + "═" * 56,
         "判读: 胜率>50%且高低档价差为正且IC为正 → 该因子有正向 edge;",
         "      分层单调 = 越高分未来越涨, 是比'Top命中率'更稳的有效性证据。",
         "注: 幸存者偏差使结果偏乐观; 价值版仅覆盖有估值历史的票; edge 是概率非保证。"]
    return {"ok": True, "hit": hit, "avg_excess": avg_excess, "ic_mean": ic_mean,
            "spread": spread, "monotonic": monotonic, "text": "\n".join(L)}
