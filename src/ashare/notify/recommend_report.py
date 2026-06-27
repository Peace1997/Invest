"""选股 / 选板块 推荐报告 (Phase 3.2)."""
from __future__ import annotations

HR = "─" * 68


def _hdr(t: str) -> str:
    return f"\n┌─ {t} {HR[len(t)+3:]}"


def render_recommendations(stocks, stock_uni, sectors, sector_desc,
                           held: set[str] | None = None) -> str:
    held = held or set()
    out = ["╔══════════════════════════════════════════════════════════════════╗",
           "║  选股 / 选板块 推荐 (规则打分, 非投资指令)",
           "╚══════════════════════════════════════════════════════════════════╝"]

    out.append(_hdr(f"主板个股 Top{len(stocks)}  · universe: {stock_uni}"))
    if stocks:
        out.append("│ 代码     名称            评级  健康分  现价   今日%   PE分位  信号")
        for s in stocks:
            m = s.metadata
            tag = " (持仓)" if s.target in held else ""
            pe = f"{m['pe_pct']:.0f}%" if m.get("pe_pct") is not None else "  —"
            pct = f"{m['today_pct']:+.1f}" if m.get("today_pct") is not None else "  —"
            out.append(
                f"│ {s.target:<8} {(m.get('name','')[:12]):<12}{tag:<6} {s.label:<4} "
                f"{s.score:+.2f}  {m.get('close',0):>6.2f} {pct:>6}  {pe:>5}  {s.reason[:30]}")
        out.append("│")
        out.append("│ 详细拆解 + 买卖价位(每只可解释):")
        for s in stocks:
            out.append(f"│  ▸ {s.metadata.get('name','')}({s.target}) {s.label} {s.score:+.2f}")
            for c in s.components:
                if c.contribution != 0 or "估值" in c.label:
                    out.append(f"│      {c.contribution:+.2f}  {c.label}: {c.detail}")
            lv = s.metadata.get("levels")
            if lv:
                out.append(f"│      🎯 {lv.status}")
                out.append(f"│         买入区 {lv.buy_lo}~{lv.buy_hi} · 止盈 {lv.target} · 止损 {lv.stop}"
                           + (f" · 风报比 {lv.rr}" if lv.rr else ""))
    else:
        out.append("│ (无足够数据的候选 — 先跑 universe 回填)")

    out.append(_hdr(f"推荐板块 Top{len(sectors)}  · {sector_desc}"))
    if sectors:
        out.append("│ 板块            评级  趋势分  今日%   信号")
        for s in sectors:
            m = s.metadata
            pct = f"{m['today_pct']:+.1f}" if m.get("today_pct") is not None else "  —"
            out.append(f"│ {m.get('name','')[:14]:<14} {s.label:<4} {s.score:+.2f}  {pct:>6}  {s.reason[:34]}")
    else:
        out.append(f"│ ⚠ {sector_desc}")

    out.append(_hdr("说明"))
    out.append("│ 评分口径同持仓健康度: 趋势·均线+动量+RSI+乖离过热+年内位置+估值PE分位, 满分±1。")
    out.append("│ universe = 全市场主板(沪60/深00)非ST, 估值分位按个股近5年。")
    out.append("│ ⚠ 当前为趋势/动量主导的打分, 偏好上涨势头强的票; 高分常是强势股,")
    out.append("│   也可能已涨幅巨大/过热(看'乖离·过热'项与PB分位), 追高需谨慎, 务必结合基本面。")
    out.append("│ 这是规则提示, 不构成投资建议; 买入前请自行结合基本面/风险承受度。")
    return "\n".join(out) + "\n"
