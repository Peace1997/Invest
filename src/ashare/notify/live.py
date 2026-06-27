"""Live (intraday) quote report — real-time snapshot for online decisioning.

Distinct from the daily briefing (notify/report.py): this pulls *current* prices
from Tencent and is explicitly labeled provisional/realtime. It does NOT touch
the warehouse; it's a read-through to the live market.
"""
from __future__ import annotations
from datetime import datetime

import pandas as pd

from ..sources import TencentSource

UP, DN, FLAT = "▲", "▼", "—"
HR = "─" * 64


def _pct(x) -> str:
    if x is None or pd.isna(x):
        return "  n/a "
    arrow = UP if x > 0 else DN if x < 0 else FLAT
    return f"{arrow}{abs(x):>5.2f}%"


def _fmt_amt(a) -> str:
    if a is None or pd.isna(a):
        return "n/a"
    a = float(a)
    if a >= 1e8:
        return f"{a/1e8:>6.2f}亿"
    if a >= 1e4:
        return f"{a/1e4:>6.2f}万"
    return f"{a:>8.0f}"


def _session_state(now: datetime) -> str:
    t = now.time()
    hm = t.hour * 60 + t.minute
    if now.weekday() >= 5:
        return "休市(周末)"
    if hm < 9 * 60 + 15:
        return "盘前"
    if hm < 9 * 60 + 30:
        return "集合竞价"
    if 9 * 60 + 30 <= hm <= 11 * 60 + 30:
        return "盘中(上午)"
    if 11 * 60 + 30 < hm < 13 * 60:
        return "午间休市"
    if 13 * 60 <= hm <= 15 * 60:
        return "盘中(下午)"
    return "已收盘"


def _line(r) -> str:
    name = (r["name"] or "")[:10]
    amt = _fmt_amt(r["amount"])
    turn = f"换手 {r['turnover']:>5.2f}%" if not pd.isna(r.get("turnover")) else "换手   n/a"
    return (f"│ {r['symbol']} {name:<11} {r['price']:>9.3f}  {_pct(r['pct_chg'])}  "
            f"昨收 {r['prev_close']:>9.3f}  {amt}  {turn}")


def render_live_report(index_symbols: list[str],
                       watchlist: list[str],
                       src: TencentSource | None = None) -> str:
    src = src or TencentSource()
    now = datetime.now()
    idx_set = set(index_symbols)
    all_syms = list(dict.fromkeys([*index_symbols, *watchlist]))  # dedupe, keep order

    df = src.spot(all_syms, index_symbols=idx_set)

    banner = [
        "╔════════════════════════════════════════════════════════════════╗",
        f"║  A股实时盘中快照 · {now:%Y-%m-%d %H:%M:%S} · {_session_state(now)}",
        "║  🔴 实时未定稿数据(腾讯), 仅供盘中参考, 收盘后以 daily 定稿为准",
        "╚════════════════════════════════════════════════════════════════╝",
    ]
    if df.empty:
        return "\n".join(banner) + "\n│ ❌ 实时数据拉取失败 (检查网络/代理)\n"

    # quote_time spread: surface the data timestamp honestly
    qt = df["quote_time"].dropna()
    qt_note = ""
    if not qt.empty:
        qt_note = f"  数据时间 {qt.max():%H:%M:%S}"

    lines = list(banner)

    idx_df = df[df["symbol"].isin(idx_set)]
    if not idx_df.empty:
        lines.append(f"\n┌─ 指数{qt_note} {HR[len(qt_note)+5:]}")
        for _, r in idx_df.iterrows():
            lines.append(_line(r))

    wl_df = df[~df["symbol"].isin(idx_set)]
    if not wl_df.empty:
        lines.append(f"\n┌─ 关注列表 {HR[5:]}")
        for _, r in wl_df.iterrows():
            lines.append(_line(r))

    missing = [s for s in all_syms if s not in df["symbol"].values]
    if missing:
        lines.append(f"\n│ ⚠ 未取到: {', '.join(missing)}")

    return "\n".join(lines) + "\n"
