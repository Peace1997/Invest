"""组合再平衡建议 (机械·不靠预测) —— 接在 risk.py 之后的"该减谁/该补谁"。

两个层次, 都可解释:
  1. 风险贡献分解: 每只持仓贡献了组合多少波动(RC% = w·(Σw)/w'Σw)。关键洞察:
     高相关的A股指数簇, 权重看着分散, 风险贡献却远超其权重 —— 这才是真实超配。
  2. 等风险(ERC)参考配置: 让每只贡献相等风险, 与现状比 → 减超配、补低配。
     诚实口径: ERC 会自然偏向低波资产(债/黄金), 这是"方向"不是"满仓债"的命令;
     建议已限幅(单次±LIMIT), 且只动有足够历史的持仓; 定投(shares=0)不参与。
"""
from __future__ import annotations
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .portfolio import Portfolio
from .risk import _returns_matrix

_TRADE_LIMIT = 10.0   # 单次建议调整幅度上限(百分点), 防过度换手


@dataclass
class RebalancePlan:
    rows: list[dict] = field(default_factory=list)   # 每只: name/weight/rc/erc/delta
    trims: list[str] = field(default_factory=list)
    adds: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    ok: bool = True


def _erc_weights(cov: np.ndarray, iters: int = 20000, tol: float = 1e-9) -> np.ndarray:
    """等风险贡献权重(无杠杆, 全多头)。乘性不动点: 高RC下调、低RC上调。"""
    n = len(cov)
    w = np.ones(n) / n
    for _ in range(iters):
        mrc = cov @ w
        rc = w * mrc
        if (rc <= 0).any():
            rc = np.clip(rc, 1e-12, None)
        w_new = w * np.sqrt(rc.mean() / rc)
        w_new = np.clip(w_new, 1e-9, None)
        w_new /= w_new.sum()
        if np.abs(w_new - w).max() < tol:
            return w_new
        w = w_new
    return w


def rebalance_plan(pf: Portfolio, con) -> RebalancePlan:
    rets, w, skipped = _returns_matrix(pf, con)
    if rets is None or w is None:
        return RebalancePlan(ok=False,
                             notes=["可估值且历史充足的持仓不足(<2), 无法做再平衡。"])
    r = rets[w.index].dropna(how="any")
    if len(r) < 60:
        return RebalancePlan(ok=False, notes=["重叠历史样本不足(<60交易日)。"])

    cov = r.cov().values
    wv = w.values
    port_var = float(wv @ cov @ wv)
    if port_var <= 0:
        return RebalancePlan(ok=False, notes=["组合方差异常, 跳过。"])
    rc = wv * (cov @ wv) / port_var          # 风险贡献占比
    erc = _erc_weights(cov)

    code2name = {h.code: h.name for h in pf.valued}
    tot_mv = pf.total_market_value
    rows = []
    for i, code in enumerate(w.index):
        delta = float(erc[i] - wv[i]) * 100      # 目标-现状(百分点)
        delta_cap = max(-_TRADE_LIMIT, min(_TRADE_LIMIT, delta))
        rows.append({
            "code": code, "name": code2name.get(code, code),
            "weight": float(wv[i] * 100), "rc": float(rc[i] * 100),
            "erc": float(erc[i] * 100), "delta": delta, "delta_cap": delta_cap,
            "amount": delta_cap / 100 * tot_mv,
        })
    rows.sort(key=lambda x: x["rc"], reverse=True)

    plan = RebalancePlan(rows=rows)
    for x in rows:
        # 超配(风险贡献明显高于其在ERC下应有的) → 减; 反之补
        if x["delta_cap"] <= -1.0:
            plan.trims.append(
                f"减 {x['name'][:12]} 约 {x['delta_cap']:+.0f}pct (≈{x['amount']:+,.0f}元) "
                f"〔权重{x['weight']:.0f}% 但风险贡献{x['rc']:.0f}%, 超配风险〕")
        elif x["delta_cap"] >= 1.0:
            plan.adds.append(
                f"补 {x['name'][:12]} 约 {x['delta_cap']:+.0f}pct (≈{x['amount']:+,.0f}元) "
                f"〔风险贡献仅{x['rc']:.0f}%, 与A股低相关, 加它才真分散〕")

    # 诚实提示
    bond_erc = sum(x["erc"] for x in rows
                   if any(k in x["name"] for k in ("债", "货币")))
    if bond_erc > 25:
        plan.notes.append(
            f"ERC 把债/低波配到 ~{bond_erc:.0f}% 属正常(它波动低), 这是'降总波动的方向', "
            "不是让你真的满仓债 —— 按你的风险偏好取其方向、分步调。")
    if skipped:
        plan.notes.append("历史不足、未纳入: " + "、".join(s[:8] for s in skipped[:5]))
    plan.notes.append(f"建议单次调整已限幅 ±{_TRADE_LIMIT:.0f}pct, 分批做即可; 全程不靠预测, "
                      "只让'每只贡献相等风险'。相关性大跌时趋近1, 故分散要靠低相关资产而非多买同类。")
    return plan


def render_rebalance(plan: RebalancePlan) -> str:
    if not plan.ok:
        return "组合再平衡: " + (plan.notes[0] if plan.notes else "数据不足。")
    L = ["╔" + "═" * 56, "║ 组合再平衡建议 · 机械·不靠预测", "╠" + "═" * 56,
         "║ 权重 vs 风险贡献 (谁在吃掉你的波动)", "║"]
    for x in plan.rows:
        flag = " ⚠超配风险" if x["rc"] > x["weight"] + 4 else (
            " ·防御" if x["rc"] < x["weight"] - 4 else "")
        L.append(f"║  {x['name'][:14]:<14} 权重{x['weight']:5.1f}%  风险贡献{x['rc']:5.1f}%"
                 f"  →ERC{x['erc']:5.1f}%{flag}")
    L.append("╟" + "─" * 56)
    if plan.trims:
        L.append("║ 【减】超配风险, 优先降:")
        L += [f"║   • {t}" for t in plan.trims[:5]]
    if plan.adds:
        L.append("║ 【补】低相关, 加它才真分散:")
        L += [f"║   • {a}" for a in plan.adds[:5]]
    if not plan.trims and not plan.adds:
        L.append("║ 当前已较均衡, 无需大调。")
    L.append("╚" + "═" * 56)
    for n in plan.notes:
        L.append("注: " + n)
    return "\n".join(L)
