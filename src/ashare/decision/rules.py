"""Explainable buy/sell decision engine (ROADMAP Phase 4).

Fuses three inputs into one actionable line *per holding* (so every position
gets feedback, not just threshold breaches):
  - holding health  (signals.holding_health) — trend/momentum score ∈ [-1,+1]
  - cost-basis P&L  (浮盈%)                    — 止盈/止损
  - position weight (集中度)                   — 风控

Priority, per project principle 风控 > 选股 > 择时:
  1. 集中度超限 → 减仓
  2. 深度亏损   → 止损评估(趋势弱) / 观察(趋势企稳)
  3. 高额浮盈   → 止盈(尤其趋势转弱/超买)
  4. 其余由健康分定调 → 加仓 / 持有 / 持有观察 / 谨慎 / 减仓

Plus portfolio-level: 类别集中度 + 大盘择时(index_timing)。
Every Advice carries a `reason`. These are 提示, not orders.
"""
from __future__ import annotations
from dataclasses import dataclass, field

from ..portfolio import Portfolio, Holding
from ..signals.base import SignalOutput

# ── thresholds (conservative, tunable) ──
TAKE_PROFIT_PCT = 30.0
STOP_LOSS_PCT = -15.0
SINGLE_WEIGHT_PCT = 25.0
CAT_WEIGHT_PCT = 45.0
UNDERWEIGHT_PCT = 5.0       # 强势且权重 < 此值 → 可加仓
STRONG, WEAK = 0.35, -0.35  # health score 强/弱分界

ACTION_ORDER = {"减仓": 0, "止损评估": 1, "再平衡": 2, "止盈": 3,
                "谨慎": 4, "观察": 5, "加仓": 6, "持有观察": 7, "持有": 8}


@dataclass
class Advice:
    code: str
    name: str
    action: str
    reason: str
    urgency: int = 1            # 1 低 · 2 中 · 3 高
    score: float | None = None  # health score, for display

    @property
    def tag(self) -> str:
        return {1: "·", 2: "!", 3: "‼"}.get(self.urgency, "·")


def _holding_advice(h: Holding, pf: Portfolio,
                    health: SignalOutput | None) -> Advice:
    """One primary action for a single holding (risk → P&L → trend)."""
    w = pf.weight(h)
    score = health.score if health else None
    hreason = health.reason if health else "无健康数据"
    pnl = h.pnl_pct

    def mk(action, why, urgency):
        tail = f" | 浮盈{pnl:+.1f}%" if pnl is not None else ""
        return Advice(h.code, h.name, action, f"{why} 〔{hreason}{tail}〕",
                      urgency=urgency, score=score)

    # 1) concentration (risk first)
    if w is not None and w > SINGLE_WEIGHT_PCT:
        return mk("减仓", f"权重{w:.1f}%超{SINGLE_WEIGHT_PCT:.0f}%上限, 再平衡降单一风险",
                  3 if w > SINGLE_WEIGHT_PCT + 10 else 2)

    # 2) deep loss
    if pnl is not None and pnl <= STOP_LOSS_PCT:
        if score is not None and score >= 0.15:
            return mk("观察", f"深亏{pnl:.0f}%但趋势企稳, 暂不止损、跌破前低再决断", 2)
        return mk("止损评估", f"亏损{pnl:.0f}%且趋势未转强, 复核逻辑: 破位应止损",
                  3 if pnl <= STOP_LOSS_PCT - 10 else 2)

    # 3) large gain
    if pnl is not None and pnl >= TAKE_PROFIT_PCT:
        if score is not None and score <= 0:
            return mk("止盈", f"浮盈{pnl:.0f}%且趋势转弱, 分批止盈锁定", 2)
        return mk("止盈", f"浮盈{pnl:.0f}%, 可分批止盈、留底仓跟趋势", 1)

    # 4) trend-driven
    if score is None:
        return mk("持有观察", "缺少趋势数据, 仅按权重持有", 1)
    if score >= STRONG:
        if w is not None and w < UNDERWEIGHT_PCT:
            return mk("加仓", "趋势强且仓位偏低, 可逢回调加仓", 1)
        return mk("持有", "趋势强, 维持仓位", 1)
    if score >= 0.15:
        return mk("持有", "趋势偏多, 维持", 1)
    if score <= WEAK:
        return mk("谨慎", "趋势走弱, 控制仓位/设好止损", 2)
    if score <= -0.15:
        return mk("持有观察", "趋势偏空, 不加仓、观察是否企稳", 1)
    return mk("持有观察", "趋势中性震荡, 维持观察", 1)


def generate_advice(pf: Portfolio,
                    health: dict[str, SignalOutput] | None = None,
                    timing: SignalOutput | None = None) -> list[Advice]:
    """Per-holding primary action + portfolio-level concentration flags."""
    health = health or {}
    out: list[Advice] = [
        _holding_advice(h, pf, health.get(h.code)) for h in pf.valued
    ]

    # portfolio-level: category concentration
    tot = pf.total_market_value
    cat_codes: dict[str, list[Holding]] = {}
    for h in pf.valued:
        cat_codes.setdefault(h.category, []).append(h)
    for cat, mv in pf.by_category().items():
        share = mv / tot * 100 if tot else 0
        if cat != "现金" and share > CAT_WEIGHT_PCT:
            names = "/".join(x.name[:6] for x in cat_codes.get(cat, [])[:3])
            out.append(Advice(f"[{cat}]", cat, "再平衡",
                              f"{cat}合计{share:.1f}%超{CAT_WEIGHT_PCT:.0f}% ({names}…), 分散到低相关资产",
                              urgency=2))

    out.sort(key=lambda a: (-a.urgency, ACTION_ORDER.get(a.action, 9)))
    return out
