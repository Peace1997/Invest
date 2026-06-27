"""组合层风控 (风控 > 选股 > 择时 的"风控"那层).

decision/rules.py 已做**逐持仓**的止损/止盈/集中度建议; 这里补**组合整体**视角:
真实分散度(相关性, 而非看名字分类)、集中度(HHI)、市场暴露(对沪深300的beta)、
组合波动率与压力测试、当前回撤。全部基于已有的价格/净值历史, 不需额外数据。

诚实口径: 相关性/波动是历史统计(默认近1年), 是"过去如何一起波动", 非未来保证;
样本不足(<60个重叠交易日)的持仓会被剔出统计并如实标注。
"""
from __future__ import annotations
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .portfolio import Portfolio, Holding
from .factors.nav import nav_history

_WIN = 250          # 统计窗口(交易日)
_MIN_OVERLAP = 60   # 少于此重叠样本的持仓不纳入相关性/波动统计


@dataclass
class RiskReport:
    n_used: int
    n_skipped: int
    skipped: list[str]
    hhi: float
    eff_names: float
    top1: tuple[str, float] | None
    top3: float
    avg_corr: float | None
    div_ratio: float | None
    vol_annual: float | None
    beta_300: float | None
    equity_beta_weight: float | None     # 与沪深300高相关(>0.6)的持仓合计权重 %
    stress_drop: float | None            # 市场-10% 时组合预估 %
    cur_drawdown: float | None
    clusters: list[tuple[float, list[str]]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _series_for(h: Holding, con) -> pd.Series | None:
    """持仓的收盘/净值序列, 按日期升序, index=日期。"""
    if h.type in ("stock", "etf"):
        try:
            df = con.execute(
                "SELECT trade_date, close FROM daily_bar_adj WHERE symbol=? ORDER BY trade_date",
                [h.code.zfill(6)]).df()
        except Exception:
            return None
        if df is None or df.empty:
            return None
        return pd.Series(df["close"].to_numpy(float),
                         index=pd.to_datetime(df["trade_date"]))
    if h.type in ("otc_fund", "bond_fund"):
        df = nav_history(con, h.code)
        if df is None or df.empty:
            return None
        return pd.Series(df["nav"].to_numpy(float),
                         index=pd.to_datetime(df["nav_date"]))
    return None


def _returns_matrix(pf: Portfolio, con):
    """对齐后的日收益矩阵 + 各列权重(归一化)。返回 (rets_df, weights, skipped)。"""
    cols, ser, skipped = [], {}, []
    for h in pf.valued:
        s = _series_for(h, con)
        if s is None or s.dropna().shape[0] < _MIN_OVERLAP:
            skipped.append(h.name)
            continue
        cols.append(h)
        ser[h.code] = s.tail(_WIN)
    if len(cols) < 2:
        return None, None, skipped
    px = pd.DataFrame(ser).sort_index()
    px = px.dropna(how="all").ffill().tail(_WIN)
    rets = px.pct_change().dropna(how="any")
    if rets.shape[0] < _MIN_OVERLAP:
        # 交集太小: 放宽为成对相关(用 pairwise), 但波动仍需共同样本
        rets = px.pct_change()
    w = np.array([pf.weight(h) or 0 for h in cols], dtype=float)
    w = w / w.sum() if w.sum() > 0 else w
    return rets, pd.Series(w, index=[h.code for h in cols]), skipped


def portfolio_risk(pf: Portfolio, con) -> RiskReport | None:
    rets, w, skipped = _returns_matrix(pf, con)
    code2name = {h.code: h.name for h in pf.valued}

    # 集中度(对全部可估值持仓, 不依赖历史)
    wv = np.array([pf.weight(h) or 0 for h in pf.valued], dtype=float)
    wv = wv / wv.sum() if wv.sum() > 0 else wv
    hhi = float(np.sum(wv ** 2))
    eff_names = (1.0 / hhi) if hhi > 0 else 0.0
    order = np.argsort(wv)[::-1]
    top1 = None
    if len(order):
        h0 = pf.valued[order[0]]
        top1 = (h0.name, float(wv[order[0]] * 100))
    top3 = float(np.sum(np.sort(wv)[::-1][:3]) * 100)

    rep = RiskReport(n_used=0, n_skipped=len(skipped), skipped=skipped,
                     hhi=hhi, eff_names=eff_names, top1=top1, top3=top3,
                     avg_corr=None, div_ratio=None, vol_annual=None,
                     beta_300=None, equity_beta_weight=None, stress_drop=None,
                     cur_drawdown=None)

    if rets is not None and w is not None and rets.dropna(how="any").shape[0] >= _MIN_OVERLAP:
        r = rets[w.index].dropna(how="any")
        rep.n_used = len(w)
        vols = r.std()
        corr = r.corr()
        # 加权平均成对相关(剔对角)
        ww = np.outer(w.values, w.values)
        mask = ~np.eye(len(w), dtype=bool)
        denom = ww[mask].sum()
        rep.avg_corr = float((corr.values * ww)[mask].sum() / denom) if denom > 0 else None
        # 组合波动 & 分散比
        port_ret = r.mul(w.values, axis=1).sum(axis=1)
        vol_p = float(port_ret.std())
        rep.vol_annual = vol_p * np.sqrt(252) * 100
        wavg_vol = float((w.values * vols.values).sum())
        rep.div_ratio = (wavg_vol / vol_p) if vol_p > 0 else None

        # 对沪深300 的 beta + 高相关暴露
        b = con.execute("SELECT trade_date, close FROM index_bar WHERE symbol='000300' "
                        "ORDER BY trade_date").df()
        if not b.empty:
            bs = pd.Series(b["close"].to_numpy(float), index=pd.to_datetime(b["trade_date"]))
            bret = bs.pct_change()
            aligned = pd.concat([port_ret, bret], axis=1, join="inner").dropna()
            aligned.columns = ["p", "m"]
            if len(aligned) >= _MIN_OVERLAP and aligned["m"].var() > 0:
                rep.beta_300 = float(aligned["p"].cov(aligned["m"]) / aligned["m"].var())
                rep.stress_drop = rep.beta_300 * -10.0
            # 各持仓对沪深300相关>0.6 的合计权重(= A股beta暴露)
            ind_corr = {}
            for code in w.index:
                a = pd.concat([r[code], bret], axis=1, join="inner").dropna()
                if len(a) >= _MIN_OVERLAP and a.iloc[:, 1].var() > 0:
                    ind_corr[code] = float(a.iloc[:, 0].corr(a.iloc[:, 1]))
            rep.equity_beta_weight = float(
                sum(w[c] for c, cc in ind_corr.items() if cc is not None and cc > 0.6) * 100)

        # 高相关簇(成对 corr>0.8) → 伪分散
        seen, clusters = set(), []
        cc = corr.copy()
        for i in cc.index:
            if i in seen:
                continue
            grp = [j for j in cc.columns if j != i and cc.loc[i, j] > 0.8]
            if grp:
                members = [i] + grp
                seen.update(members)
                wsum = float(sum(w.get(m, 0) for m in members) * 100)
                clusters.append((wsum, [code2name.get(m, m) for m in members]))
        rep.clusters = sorted(clusters, reverse=True)[:3]

    # 当前回撤(用组合快照历史峰值)
    try:
        hist = con.execute("SELECT total_market_value FROM portfolio_snapshot "
                           "ORDER BY snapshot_date").df()
        if len(hist) >= 2:
            v = hist["total_market_value"]
            peak = v.cummax().iloc[-1]
            rep.cur_drawdown = float((v.iloc[-1] / peak - 1) * 100) if peak > 0 else None
    except Exception:
        pass
    return rep


def render_risk(rep: RiskReport | None) -> str:
    if rep is None:
        return "风控层: 可估值持仓不足(<2), 无法做组合层分析。"
    L = ["╔" + "═" * 54, "║ 组合风控 · 整体视角 (风控>选股>择时)", "╠" + "═" * 54]
    # 集中度
    L.append(f"║ 集中度: 有效持仓数 {rep.eff_names:.1f} 只 (HHI {rep.hhi:.2f})")
    if rep.top1:
        L.append(f"║   最大单一 {rep.top1[0][:10]} {rep.top1[1]:.1f}%  ·  前三合计 {rep.top3:.1f}%")
    flag = "⚠ 过度集中" if rep.eff_names < 4 else "尚可" if rep.eff_names < 8 else "分散"
    L.append(f"║   判读: {flag} (有效持仓数=1/HHI, 越大越分散)")
    # 真实分散度
    if rep.avg_corr is not None:
        dr = f"{rep.div_ratio:.2f}" if rep.div_ratio else "—"
        L.append("╟" + "─" * 54)
        L.append(f"║ 真实分散度: 持仓两两平均相关 {rep.avg_corr:+.2f}  ·  分散比 {dr}")
        if rep.avg_corr > 0.6:
            L.append("║   ⚠ 持仓高度同涨同跌 → 名义分散、实则一个风险")
        elif rep.avg_corr > 0.4:
            L.append("║   中等相关 → 分散有限")
        else:
            L.append("║   相关较低 → 分散有效")
    if rep.equity_beta_weight is not None:
        L.append(f"║   与沪深300高相关(>0.6)的持仓合计权重 {rep.equity_beta_weight:.0f}% "
                 "(= A股beta暴露)")
    for wsum, names in rep.clusters:
        L.append(f"║   高相关簇({wsum:.0f}%): " + " / ".join(n[:8] for n in names[:4]))
    # 波动 / beta / 压力
    if rep.vol_annual is not None:
        L.append("╟" + "─" * 54)
        L.append(f"║ 波动率(年化) ≈ {rep.vol_annual:.0f}%"
                 + (f"  ·  对沪深300 beta ≈ {rep.beta_300:.2f}" if rep.beta_300 else ""))
    if rep.stress_drop is not None:
        L.append(f"║ 压力测试: 若沪深300跌10%, 组合预估 {rep.stress_drop:+.1f}% (按beta线性近似)")
    if rep.cur_drawdown is not None:
        L.append(f"║ 当前回撤: 距快照峰值 {rep.cur_drawdown:+.1f}%")
    if rep.n_skipped:
        L.append("╟" + "─" * 54)
        L.append(f"║ 注: {rep.n_skipped} 只历史不足未纳入统计: "
                 + "、".join(s[:8] for s in rep.skipped[:5]))
    L.append("╚" + "═" * 54)
    L.append("提示: 这些是历史统计与近似, 非未来保证。相关性会在大跌时趋近1(分散失效),")
    L.append("      故'有效持仓数'和'A股beta暴露'比单看名字分类更能反映真实风险。")
    return "\n".join(L)
