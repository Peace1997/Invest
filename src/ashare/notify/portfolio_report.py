"""Daily portfolio evaluation report.

Auto-prices every holding (stocks live via Tencent, OTC funds via published
NAV) so the user only maintains shares. Computes per-item value, weight, P&L,
today's move, asset allocation, concentration, P&L attribution, and a set of
explainable risk observations.

Honesty: OTC fund NAV is T-1 (published in the evening); the report shows each
price's date and kind so nothing stale masquerades as live.
"""
from __future__ import annotations
from datetime import datetime, date

from ..portfolio import load_positions, value_portfolio, Portfolio

UP, DN, FLAT = "▲", "▼", "—"
HR = "─" * 68


def _pct(x) -> str:
    if x is None:
        return "  n/a "
    a = UP if x > 0 else DN if x < 0 else FLAT
    return f"{a}{abs(x):>5.2f}%"


def _money(x) -> str:
    if x is None:
        return "n/a"
    return f"{x:>10,.0f}"


def _hdr(title: str) -> str:
    return f"\n┌─ {title} {HR[len(title)+3:]}"


def _ds(d) -> str:
    if d is None:
        return "?"
    if isinstance(d, datetime):
        return d.strftime("%Y-%m-%d")
    return str(d)[:10]


def render_portfolio_report(pf: Portfolio | None = None,
                            advice=None,
                            timing=None,
                            health=None,
                            history=None,
                            positions_path: str = "positions.yaml") -> str:
    """Daily portfolio evaluation.

    `pf` may be a pre-valued Portfolio (so callers price once and reuse it for
    annotation/snapshot/advice). If None, we load+value here. `advice` is a list
    of decision.Advice, `timing` an index SignalOutput, `health` a dict
    code→SignalOutput, and `history` a portfolio_snapshot DataFrame.
    """
    if pf is None:
        pf = load_positions(positions_path)
        if pf is None:
            return "未找到 positions.yaml — 请先填写持仓。"
        if not pf.holdings:
            return "positions.yaml 中没有有效持仓（份额都为0或代码未填）。"
        pf = value_portfolio(pf)
    now = datetime.now()

    out = [
        "╔══════════════════════════════════════════════════════════════════╗",
        f"║  我的投资组合日评 · {now:%Y-%m-%d %H:%M}",
        "╚══════════════════════════════════════════════════════════════════╝",
    ]

    # ── 总览 ──
    tot = pf.total_market_value
    tcost = pf.total_cost_value
    tpnl = pf.total_pnl
    ttoday = pf.total_today_pnl
    out.append(_hdr("总览"))
    out.append(f"│ 总资产(可估值)  {_money(tot)} 元  ({tot/1e4:.2f}万)"
               + (f" + 现金 {pf.cash/1e4:.2f}万" if pf.cash else ""))
    if tcost:
        pnl_pct = (tpnl / tcost * 100) if tpnl is not None else None
        out.append(f"│ 累计浮动盈亏    {_money(tpnl)} 元  ({_pct(pnl_pct).strip()})  [成本 {tcost/1e4:.2f}万]")
    if ttoday is not None:
        out.append(f"│ 今日盈亏        {_money(ttoday)} 元  ({_pct(ttoday/tot*100).strip()} of 总资产)")
    out.append(f"│ 持仓数          {len(pf.valued)} 项可估值"
               + (f" / {len(pf.unvalued)} 项待估值" if pf.unvalued else ""))

    # ── 资产配置 ──
    cats = pf.by_category()
    if cats:
        out.append(_hdr("资产配置"))
        for cat, mv in sorted(cats.items(), key=lambda kv: kv[1], reverse=True):
            bar = "█" * round(mv / tot * 20)
            out.append(f"│ {cat:<8} {mv/tot*100:>5.1f}%  {bar} {mv/1e4:.2f}万")

    # ── 明细 (按权重) ──
    out.append(_hdr("持仓明细 (按市值排序)"))
    out.append("│ 代码     名称              市值(元)    权重   今日     浮盈    定价")
    valued_sorted = sorted(pf.valued, key=lambda h: h.market_value or 0, reverse=True)
    for h in valued_sorted:
        w = pf.weight(h)
        kind = f"{h.price_kind}{_ds(h.price_date)[5:]}" if h.price_kind else ""
        out.append(
            f"│ {h.code:<8} {h.name[:14]:<14} {_money(h.market_value)} "
            f"{w:>5.1f}% {_pct(h.today_pct)} {_pct(h.pnl_pct)}  {kind}"
        )

    # ── 持仓健康度 (趋势打分) ──
    if health:
        out.append(_hdr("持仓健康度 (趋势打分, -1空 ~ +1多)"))
        out.append("│ 名称              评级   分数   信号")
        ranked = sorted(pf.valued, key=lambda h: (health.get(h.code).score
                        if health.get(h.code) else 0), reverse=True)
        for h in ranked:
            sig = health.get(h.code)
            if not sig:
                continue
            out.append(f"│ {h.name[:14]:<14} {sig.label:<4} {sig.score:+.2f}  {sig.reason[:40]}")

    # ── 今日盈亏归因 ──
    movers = [(h, h.today_pnl) for h in pf.valued if h.today_pnl is not None]
    if movers:
        movers.sort(key=lambda x: x[1])
        out.append(_hdr("今日盈亏归因"))
        worst = movers[0]
        best = movers[-1]
        if worst[1] < 0:
            out.append(f"│ 最大拖累  {worst[0].name[:14]:<14} {_money(worst[1])}元 ({_pct(worst[0].today_pct).strip()})")
        if best[1] > 0:
            out.append(f"│ 最大贡献  {best[0].name[:14]:<14} {_money(best[1])}元 ({_pct(best[0].today_pct).strip()})")

    # ── 风险评估 (可解释规则) ──
    out.append(_hdr("风险评估 (规则提示, 非买卖指令)"))
    flags = _risk_flags(pf)
    if flags:
        out.extend(f"│ {f}" for f in flags)
    else:
        out.append("│ ✓ 未触发集中度/单一持仓预警")

    # ── 操作建议 (决策引擎, 可解释) ──
    out.append(_hdr("操作建议 (规则提示, 非投资指令)"))
    if timing is not None:
        out.append(f"│ 大盘择时  [{timing.label}] (score {timing.score:+.2f})  {timing.reason}")
    if advice:
        for a in advice:
            out.append(f"│ {a.tag}[{a.action}] {a.name[:12]:<12} {a.reason}")
    else:
        out.append("│ ✓ 暂无操作提示")
    out.append("│ ── 阈值: 止盈≥+30% · 止损≤-15% · 单一>25% · 单类>45% · 强弱±0.35 ──")

    # ── 历史走势 (收益曲线 / 最大回撤) ──
    if history is not None and len(history) >= 2:
        out.append(_hdr("历史走势 (来自每日快照)"))
        out.extend(_history_lines(history))

    # ── 待估值 + 诚实声明 ──
    if pf.unvalued:
        out.append(_hdr("待估值 (诚实标注)"))
        for h in pf.unvalued:
            out.append(f"│ {h.code} {h.name[:18]:<18} → {h.note}")

    out.append(_hdr("数据说明 (重要)"))
    out.append("│ 个股: 腾讯实时价(盘中未定稿, 收盘后=定稿) — 上方标 实时MM-DD")
    out.append("│ 场外基金: 单位净值 T-1(基金晚间才公布当日净值) — 上方标 净值MM-DD")
    out.append("│ ⚠ '今日'列日期不齐: 个股是最新交易日, 基金是其净值日(通常慢1天),")
    out.append("│   故'今日盈亏'合计是跨日期混合的近似值, 非同一天的精确口径。")
    out.append("│ 浮盈基于你填的成本价; 份额你维护, 价格本工具每次运行自动拉取更新。")

    return "\n".join(out) + "\n"


def _history_lines(history) -> list[str]:
    """Return-curve summary + max drawdown from the portfolio_snapshot frame."""
    from ..snapshots import max_drawdown
    h = history.sort_values("snapshot_date").reset_index(drop=True)
    mv = h["total_market_value"].astype(float)
    mv.index = h["snapshot_date"]
    first_mv, last_mv = mv.iloc[0], mv.iloc[-1]
    lines = []
    span = f"{_ds(h['snapshot_date'].iloc[0])} → {_ds(h['snapshot_date'].iloc[-1])} ({len(h)}个快照)"
    lines.append(f"│ 区间      {span}")
    if first_mv:
        chg = (last_mv / first_mv - 1) * 100
        lines.append(f"│ 总资产    {first_mv/1e4:.2f}万 → {last_mv/1e4:.2f}万  ({_pct(chg).strip()})")
    mdd, peak_d, trough_d = max_drawdown(mv)
    if mdd < 0:
        lines.append(f"│ 最大回撤  {mdd:.2f}%  ({_ds(peak_d)}峰 → {_ds(trough_d)}谷)")
    else:
        lines.append("│ 最大回撤  快照内无回撤")
    # sparkline
    if len(mv) >= 3:
        lo, hi = mv.min(), mv.max()
        blocks = "▁▂▃▄▅▆▇█"
        if hi > lo:
            spark = "".join(blocks[min(7, int((v - lo) / (hi - lo) * 7))] for v in mv)
            lines.append(f"│ 走势      {spark}")
    return lines


def _risk_flags(pf: Portfolio) -> list[str]:
    """Explainable, threshold-based risk observations."""
    flags = []
    tot = pf.total_market_value
    # 1) single-holding concentration
    for h in pf.valued:
        w = pf.weight(h)
        if w and w > 25:
            flags.append(f"⚠ 单一持仓偏高: {h.name[:14]} {w:.1f}% (>25%)")
    # 2) category concentration
    for cat, mv in pf.by_category().items():
        share = mv / tot * 100
        if cat != "现金" and share > 45:
            flags.append(f"⚠ {cat} 占比偏高: {share:.1f}% (>45%)")
    # 3) equity vs defensive balance
    cats = pf.by_category()
    defensive = cats.get("债券", 0) + cats.get("红利", 0) + cats.get("现金", 0)
    equity = tot - defensive
    if tot > 0:
        eq_pct = equity / tot * 100
        if eq_pct > 90:
            flags.append(f"⚠ 权益类占比 {eq_pct:.0f}%, 防御仓位(债/红利/现金)偏薄, 回撤缓冲有限")
    # 4) today's drawdown
    tt = pf.total_today_pnl
    if tt is not None and tt < 0 and abs(tt) / tot > 0.02:
        flags.append(f"⚠ 今日组合回撤 {tt/tot*100:.2f}% (>2%), 关注是否系统性还是单板块")
    return flags
