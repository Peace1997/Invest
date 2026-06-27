"""Turn TrendFeatures into an *auditable* score.

Returns a list of ScoreComponent — each carries the real numbers, its signed
contribution to the [-1,+1] total, and a plain-language reading — so the user
can see exactly how a 偏空 -0.38 was reached, not just the label.

Rubric (max influence in parentheses):
    趋势·均线排列   (±0.40)   主导：收盘相对 MA60/MA200
    中期动量·60日   (±0.30)   tanh(ret60/15)
    超买超卖·RSI14  (±0.15)   >70 减分 / <30 加分
    年内位置/回撤   (±0.12)   深跌(距高≤-30%)略减分

Volatility & multi-horizon momentum are reported as **context** (contribution 0)
so the user sees them without them silently moving the score.
"""
from __future__ import annotations
import math

from ..factors.technical import TrendFeatures
from .base import ScoreComponent


def _fmt(x, suffix="", nd=2):
    return f"{x:.{nd}f}{suffix}" if x is not None else "n/a"


def _valuation_component(val) -> ScoreComponent | None:
    """估值·PE分位: 便宜(低分位)加分, 偏贵(高分位)减分, 满分±0.12."""
    if val is None:
        return None
    if val.pe_pct is None:
        why = val.note or "估值数据不足/PE无效"
        if val.pe_ttm is not None:
            why = f"PE-TTM {val.pe_ttm:.1f}; {why}"
        return ScoreComponent("估值·PE分位", 0.0, why, "满分±0.12")
    pct = val.pe_pct
    contrib = max(-1.0, min(1.0, (50.0 - pct) / 50.0)) * 0.12
    if pct <= 20:
        tag = "历史低位(便宜), 加分"
    elif pct >= 80:
        tag = "历史高位(偏贵), 减分"
    else:
        tag = "估值中性"
    pbtxt = f", PB {val.pb:.2f}(分位{val.pb_pct:.0f}%)" if val.pb_pct is not None else ""
    return ScoreComponent(
        "估值·PE分位", contrib,
        f"PE-TTM {val.pe_ttm:.1f}, 近{val.years:g}年 {pct:.0f}% 分位{pbtxt} → {tag}",
        "满分±0.12")


def score_components(f: TrendFeatures | None, val=None
                     ) -> tuple[float, list[ScoreComponent], float]:
    if f is None:
        base = [ScoreComponent("数据不足", 0.0, "历史样本不足，无法打分", "")]
        vc = _valuation_component(val)
        if vc:
            base.append(vc)
        total = sum(c.contribution for c in base)
        return max(-1.0, min(1.0, total)), base, 0.15 if vc else 0.1

    comps: list[ScoreComponent] = []

    # 1) trend vs moving averages (dominant)
    c = 0.0
    if f.above_ma60 is not None and f.above_ma200 is not None:
        d60 = _fmt(f.dist_ma60, "%", 1)
        d200 = _fmt(f.dist_ma200, "%", 1)
        if f.above_ma60 and f.above_ma200:
            c = 0.40
            read = f"收盘站上 MA60({d60})与 MA200({d200}) → 多头排列"
        elif not f.above_ma60 and not f.above_ma200:
            c = -0.40
            read = f"收盘跌破 MA60({d60})与 MA200({d200}) → 空头排列"
        else:
            c = 0.15 if f.above_ma60 else -0.15
            side = "站上MA60但未破MA200" if f.above_ma60 else "破MA60未破MA200"
            read = f"均线交织({side}, 距MA60 {d60}/距MA200 {d200}) → 震荡"
    elif f.above_ma60 is not None:
        c = 0.25 if f.above_ma60 else -0.25
        read = f"{'站上' if f.above_ma60 else '跌破'} MA60(距 {_fmt(f.dist_ma60, '%', 1)}) — 样本不足200日, 仅用MA60"
    else:
        read = "均线数据不足"
    comps.append(ScoreComponent("趋势·均线排列", c, read, "满分±0.40"))

    # 2) 60-day momentum
    if f.ret60 is not None:
        c = math.tanh(f.ret60 / 15.0) * 0.30
        extra = []
        if f.ret20 is not None:
            extra.append(f"20日 {f.ret20:+.1f}%")
        if f.ret120 is not None:
            extra.append(f"120日 {f.ret120:+.1f}%")
        ctx = f"（{', '.join(extra)}）" if extra else ""
        comps.append(ScoreComponent(
            "中期动量·60日", c,
            f"近60日涨跌 {f.ret60:+.1f}%{ctx} → {'上行' if f.ret60>0 else '下行'}动能",
            "满分±0.30"))

    # 3) RSI mean-reversion
    if f.rsi14 is not None:
        if f.rsi14 >= 75:
            c, read = -0.15, f"RSI {f.rsi14:.0f} ≥75 超买 → 短线过热, 减分"
        elif f.rsi14 <= 25:
            c, read = 0.12, f"RSI {f.rsi14:.0f} ≤25 超卖 → 短线或反弹, 加分"
        else:
            c, read = 0.0, f"RSI {f.rsi14:.0f}（30~70 中性区, 不影响）"
        comps.append(ScoreComponent("超买超卖·RSI14", c, read, "满分±0.15"))

    # 3b) 乖离/过热: 价格远高于 MA200 → 均值回归风险 (风控优先, 压制抛物线妖股)
    if f.dist_ma200 is not None and f.dist_ma200 > 30:
        c = -min(0.20, (f.dist_ma200 - 30) / 250.0)
        comps.append(ScoreComponent(
            "乖离·过热", c,
            f"高于 MA200 {f.dist_ma200:+.0f}% (乖离过大, 短期回撤/均值回归风险)",
            "满分-0.20"))

    # 4) year position / drawdown
    if f.dd_from_high is not None:
        c = -0.12 if f.dd_from_high <= -30 else 0.0
        pos = _fmt(f.range_pos252, "%", 0)
        read = (f"距一年高 {f.dd_from_high:+.0f}%, 处年内区间 {pos} 分位"
                + ("（深跌>30%, 弱势, 减分）" if c < 0 else "（未深跌, 不影响）"))
        comps.append(ScoreComponent("年内位置/回撤", c, read, "满分-0.12"))

    # 5) valuation percentile (贵不贵)
    vc = _valuation_component(val)
    if vc is not None:
        comps.append(vc)

    # context-only (contribution 0)
    if f.ann_vol is not None:
        comps.append(ScoreComponent(
            "波动率(参考)", 0.0,
            f"近60日年化波动 {f.ann_vol:.0f}%（仅作风险参考, 不计分）", "—"))

    total = max(-1.0, min(1.0, sum(x.contribution for x in comps)))
    conf = 0.75 if f.n >= 200 else 0.55 if f.n >= 60 else 0.35 if f.n >= 20 else 0.2
    return total, comps, conf


def score_reversal_components(f: TrendFeatures | None, val=None
                              ) -> tuple[float, list[ScoreComponent], float]:
    """反转·超跌版 rubric —— 回测(2023-2026主板)A股短期反转 3-5年有正 edge:
        20日涨幅 vs 前瞻20日收益 IC≈-0.116, 超跌档跑赢超涨档 +2.08%/月。
    ⚠ 但**近1年衰减为负**(超跌档多空≈-2%): edge 随风格切换不稳定, 非稳赚;
    且有幸存者偏差(偏乐观)。只宜小仓位、严格止损、配合择时。"买近期超跌"加分(动量的镜像)。逐项可审计:
        反转·20日超跌 (±0.50)  近20日跌得多→加分(主信号)
        反转·60日     (±0.20)  中期超跌辅助
        超卖·RSI14    (+0.15)  RSI≤30 反弹加分
        估值·PE分位   (±0.12)  便宜的超跌反弹更实(风控)
    亏损(PE≤0)与 ST/退 由上游硬剔除(避免买到退市/仙股的"接刀子")。持有约1个月、月度调仓。"""
    if f is None or f.ret20 is None:
        base = [ScoreComponent("数据不足", 0.0, "20日动量样本不足, 无法打反转分", "")]
        return 0.0, base, 0.2
    comps: list[ScoreComponent] = []

    c = math.tanh(-f.ret20 / 15.0) * 0.50
    tag = "超跌, 反弹概率偏高(加分)" if f.ret20 < 0 else "近期已涨, 反转空间小(减分)"
    comps.append(ScoreComponent("反转·20日超跌", c, f"近20日 {f.ret20:+.1f}% → {tag}", "满分±0.50"))

    if f.ret60 is not None:
        c2 = math.tanh(-f.ret60 / 30.0) * 0.20
        comps.append(ScoreComponent("反转·60日", c2,
                     f"近60日 {f.ret60:+.1f}% → {'中期超跌' if f.ret60 < 0 else '中期偏强'}", "满分±0.20"))

    if f.rsi14 is not None and f.rsi14 <= 30:
        comps.append(ScoreComponent("超卖·RSI14", 0.15,
                     f"RSI {f.rsi14:.0f} ≤30 超卖 → 反弹加分", "满分+0.15"))
    elif f.rsi14 is not None and f.rsi14 >= 75:
        comps.append(ScoreComponent("超买·RSI14", -0.10,
                     f"RSI {f.rsi14:.0f} ≥75 超买 → 追高减分", "满分-0.10"))

    vc = _valuation_component(val)
    if vc is not None:
        comps.append(vc)

    total = max(-1.0, min(1.0, sum(x.contribution for x in comps)))
    conf = 0.6 if f.n >= 120 else 0.4 if f.n >= 60 else 0.25
    return total, comps, conf


def _value_pct_component(label: str, pct, value, cap: float, unit: str) -> ScoreComponent:
    """便宜(低分位)加分, 偏贵(高分位)减分. 满分±cap. pct/value 可能 None."""
    if pct is None:
        return ScoreComponent(label, 0.0,
                              f"{unit} {_fmt(value, '', 1)}; 分位数据不足", f"满分±{cap:.2f}")
    contrib = max(-1.0, min(1.0, (50.0 - pct) / 50.0)) * cap
    tag = "历史低位(便宜), 加分" if pct <= 25 else "历史高位(偏贵), 减分" if pct >= 75 else "中性"
    return ScoreComponent(label, contrib,
                          f"{unit} {_fmt(value, '', 1)}, 近年 {pct:.0f}% 分位 → {tag}",
                          f"满分±{cap:.2f}")


def _quality_components(qual) -> list[ScoreComponent]:
    """质量因子: ROE/盈利增速 **仅展示不计分**(回测降级)。
    2026-06 Tushare历史财务回测证伪"高ROE过滤价值陷阱": 便宜池内 ROE↔未来超额 IC≈-0.075,
    高ROE组反而跑输低ROE组; 把ROE当排序奖励会让价值版变差。故 ROE/增速不再加减分, 只展示;
    质量的真实价值在"排雷"(退市风险随ROE降而升) → 由 _recommend_value 的 ST/亏损硬闸门承担。"""
    if qual is None:
        return [ScoreComponent("质量·ROE(参考)", 0.0, "无基本面数据(未覆盖)", "—")]
    comps: list[ScoreComponent] = []
    if qual.roe is not None:
        tag = "高" if qual.roe >= 15 else "中" if qual.roe >= 8 else "偏低"
        comps.append(ScoreComponent("质量·ROE(参考)", 0.0,
                     f"ROE {qual.roe:.1f}% (年报{qual.report_date}) → {tag} · 仅展示不计分(回测: A股便宜票里高ROE反跑输)",
                     "—"))
    if qual.profit_yoy is not None:
        comps.append(ScoreComponent("质量·盈利增速(参考)", 0.0,
                     f"归母净利增速 {qual.profit_yoy:+.1f}% → {'增长' if qual.profit_yoy > 0 else '下滑'} · 仅展示不计分",
                     "—"))
    ctx = []
    if qual.net_margin is not None:   ctx.append(f"净利率{qual.net_margin:.1f}%")
    if qual.gross_margin is not None: ctx.append(f"毛利率{qual.gross_margin:.1f}%")
    if qual.debt_ratio is not None:   ctx.append(f"负债率{qual.debt_ratio:.1f}%")
    if qual.revenue_yoy is not None:  ctx.append(f"营收增速{qual.revenue_yoy:+.1f}%")
    if ctx:
        comps.append(ScoreComponent("基本面(参考)", 0.0,
                     " · ".join(ctx) + "（仅展示不计分; 负债率高多为金融/地产, 属正常）", "—"))
    return comps


def score_value_components(f: TrendFeatures | None, val=None, qual=None
                           ) -> tuple[float, list[ScoreComponent], float]:
    """稳健·价值版 rubric (逐项可审计):
        估值·PE分位 (±0.30)  便宜(低分位)加分
        估值·PB分位 (±0.15)
        质量·ROE/增速 (参考)  仅展示不计分 —— 回测证伪"高ROE过滤陷阱"(便宜池ROE-IC≈-0.075)
        乖离·过热   (-0.20)  风控(低乖离 IC≈+0.07, 数据支持)
        年内位置    (-0.12)  风控
        趋势        (参考)   仅展示不计分 —— 回测显示A股追强势负超额, 故不奖励动量/趋势
    亏损(PE≤0)与 ST/退 由上游 _recommend_value 硬剔除(质量的价值在排雷, 不在排序)。"""
    comps: list[ScoreComponent] = []

    # 1) 估值 (便宜)
    if val is not None:
        comps.append(_value_pct_component("估值·PE分位", val.pe_pct, val.pe_ttm, 0.30, "PE-TTM"))
        comps.append(_value_pct_component("估值·PB分位", val.pb_pct, val.pb, 0.15, "PB"))
    else:
        comps.append(ScoreComponent("估值·PE分位", 0.0, "无估值数据(未覆盖)", "满分±0.30"))

    # 2) 质量 (ROE / 盈利增速)
    comps.extend(_quality_components(qual))

    # 3) 趋势 — 仅作上下文展示, 不计分。
    #    回测(2023-2026全主板)显示 A股"追强势"系统性负超额: 60日动量 IC≈-0.13/年, 高动量
    #    十分位反而垫底。故价值版不奖励趋势/动量。仅"乖离过热"的惩罚(下面)是数据支持的方向。
    if f is not None and f.above_ma60 is not None and f.above_ma200 is not None:
        state = ("多头(站上MA60/MA200)" if f.above_ma60 and f.above_ma200 else
                 "空头(跌破MA60/MA200)" if not (f.above_ma60 or f.above_ma200) else "均线交织")
        comps.append(ScoreComponent("趋势(参考)", 0.0,
                     f"{state} — 仅展示不计分(回测: 追强势在A股偏亏)", "—"))

    # 4) 乖离·过热 (风控; 低乖离 IC≈+0.07, 惩罚过度上涨是数据支持的方向)
    if f is not None and f.dist_ma200 is not None and f.dist_ma200 > 30:
        c = -min(0.20, (f.dist_ma200 - 30) / 250.0)
        comps.append(ScoreComponent("乖离·过热", c,
                     f"高于 MA200 {f.dist_ma200:+.0f}% (乖离过大, 均值回归风险)", "满分-0.20"))

    # 5) 年内位置/回撤 (风控)
    if f is not None and f.dd_from_high is not None and f.dd_from_high <= -30:
        comps.append(ScoreComponent("年内位置/回撤", -0.12,
                     f"距一年高 {f.dd_from_high:+.0f}% (深跌>30%, 弱势, 减分)", "满分-0.12"))

    total = max(-1.0, min(1.0, sum(c.contribution for c in comps)))
    n = f.n if f is not None else 0
    conf = 0.7 if (val is not None and qual is not None and n >= 200) else \
           0.5 if (val is not None and n >= 120) else 0.3
    return total, comps, conf


def score_trend(f: TrendFeatures | None) -> tuple[float, str, float]:
    """Back-compat: collapse components into a one-line reason."""
    total, comps, conf = score_components(f)
    reason = " · ".join(
        f"{c.label.split('·')[-1]}{c.contribution:+.2f}"
        for c in comps if c.contribution != 0
    ) or (comps[0].detail if comps else "无数据")
    return total, reason, conf
