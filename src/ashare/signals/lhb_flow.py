"""龙虎榜 + 资金流向的报告渲染 (Phase 4 主力/游资).

数据来自 ingest.lhb / ingest.fundflow 落的表, 这里只做只读查询 + 文本渲染,
供 CLI(ashare lhb/fundflow)、每日报告(notify.report)、看板(ui) 共用。

诚实标注: 龙虎榜/机构/游资均 T+1 盘后; "主力"净流入是东财按单笔金额分档推断。
"""
from __future__ import annotations

import pandas as pd

from ..ingest.lhb import (load_active_seats, load_inst_recent,
                          load_lhb_for_symbols, latest_lhb_date)
from ..ingest.fundflow import market_fund_flow_hist, load_stock_flow_for


# 复用报告的格式化 helper (report 对 lhb_flow 只在函数内惰性 import, 无循环)。
def _helpers():
    from ..notify.report import _section_header, _ds, _pct
    return _section_header, _ds, _pct


def _amt(v) -> str:
    """金额 → 亿/万, 负号保留 (report._fmt_amt 对负数不缩写, 资金流常为负故本地实现)。"""
    if v is None or pd.isna(v):
        return "n/a"
    a = float(v)
    sign = "-" if a < 0 else ""
    a = abs(a)
    if a >= 1e8:
        return f"{sign}{a/1e8:.2f}亿"
    if a >= 1e4:
        return f"{sign}{a/1e4:.2f}万"
    return f"{sign}{a:.0f}"


def _int(v) -> str:
    """整数展示, 空值(上游列漂移时 adapter 会填 NA) → '?', 避免 int(NA) 抛错。"""
    return "?" if v is None or pd.isna(v) else str(int(v))


def section_market_fund_flow(con, days: int = 5) -> str:
    """大盘资金流: 最新日主力净额 + 近 days 日累计。"""
    _section_header, _ds, _pct = _helpers()
    df = market_fund_flow_hist(con, days)
    if df.empty:
        return _section_header("大盘资金流") + "\n│ 无数据 — 跑 `ashare fundflow` 生成"
    latest = df.iloc[0]
    cum = df["main_net"].sum()
    lines = [_section_header(f"大盘资金流 · 截至 {_ds(latest['trade_date'])} (主力=超大+大单, 东财推断)")]
    lines.append(
        f"│ 当日 主力净流入 {_amt(latest['main_net'])} ({_pct(latest['main_ratio'])})  "
        f"沪 {_pct(latest['sh_pct'])} 深 {_pct(latest['sz_pct'])}"
    )
    lines.append(f"│ 近{len(df)}日 主力累计净流入 {_amt(cum)}")
    return "\n".join(lines)


def section_lhb(con, symbols: list[str] | None = None, top: int = 6) -> str:
    """龙虎榜: 主力机构净买 top + 一线游资营业部 top + 持仓/自选命中。"""
    _section_header, _ds, _pct = _helpers()
    last = latest_lhb_date(con)
    if last is None:
        return _section_header("龙虎榜") + "\n│ 无数据 — 跑 `ashare lhb` 生成"
    lines = [_section_header(f"龙虎榜 · {_ds(last)} (T+1 盘后)")]

    # 持仓/自选命中 (最重要, 放最前)
    hit = load_lhb_for_symbols(con, symbols or [], days=10)
    if not hit.empty:
        lines.append("│ ⭐ 自选/持仓上榜:")
        for r in hit.head(6).itertuples():
            org = f" 机构净{_amt(r.org_net_amt)}" if pd.notna(r.org_net_amt) else ""
            lines.append(
                f"│   {r.symbol} {str(r.name):<6} {_ds(r.trade_date)} "
                f"{_pct(r.pct_chg)} 龙虎净{_amt(r.net_buy)}{org} · {str(r.reason)[:16]}"
            )

    # 主力机构净买 top
    inst = load_inst_recent(con, top)
    if not inst.empty:
        lines.append("│ 主力机构净买 Top:")
        for r in inst.itertuples():
            lines.append(
                f"│   {r.symbol} {str(r.name):<6} {_pct(r.pct_chg)} "
                f"机构净买 {_amt(r.org_net_amt)} (买{_int(r.buy_org_n)}/卖{_int(r.sell_org_n)}家)"
            )

    # 一线游资营业部 top
    seats = load_active_seats(con, top)
    if not seats.empty:
        lines.append("│ 活跃游资营业部 Top:")
        for r in seats.itertuples():
            name = str(r.yyb_name).replace("证券股份有限公司", "").replace("证券", "")
            lines.append(
                f"│   {name[:18]:<18} 净买 {_amt(r.net_amt)} (买{_int(r.buy_stock_n)}只)"
            )
    return "\n".join(lines)


def render_stock_flow_table(con, symbols: list[str], days: int = 5) -> str:
    """给定个股近 days 天主力净流入明细(CLI 用)。"""
    _, _, _pct = _helpers()
    df = load_stock_flow_for(con, symbols, days)
    if df.empty:
        return "个股资金流: 无数据 (先跑 `ashare fundflow`)"
    lines = ["个股主力资金流 (近%d日):" % days]
    for sym, g in df.groupby("symbol"):
        cum = g["main_net"].sum()
        latest = g.sort_values("trade_date", ascending=False).iloc[0]
        lines.append(
            f"  {sym} 当日 {_amt(latest['main_net'])} ({_pct(latest['main_ratio'])}) · "
            f"近{len(g)}日累计 {_amt(cum)}"
        )
    return "\n".join(lines)
