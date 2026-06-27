"""Daily briefing report.

Design principle: **data honesty above all**.

- Header separates report-generation-time from data-as-of-date.
- Every section shows its own data-as-of date; we never say "today" loosely.
- If data lags the latest trading day, a prominent WARNING block at the top
  spells out which sources are stale and by how many trading days.
- Derived values that were computed locally (not pulled from upstream) are
  suffixed with "(本地计算)" so the reader can audit provenance.

Renders to plain text (terminal). Phase 8 will add Markdown + Telegram.
"""
from __future__ import annotations
import duckdb
import pandas as pd
from datetime import date, datetime

UP, DN, FLAT = "▲", "▼", "—"
HR = "─" * 64


# ─────────────────────────────────────────────────────────────────────
# Provenance helpers
# ─────────────────────────────────────────────────────────────────────

def _last_trading_day(con: duckdb.DuckDBPyConnection) -> date | None:
    row = con.execute(
        "SELECT max(trade_date) FROM calendar WHERE trade_date <= current_date"
    ).fetchone()
    return row[0] if row and row[0] else None


def _trading_day_lag(con: duckdb.DuckDBPyConnection, dt: date | None) -> int | None:
    """How many trading days `dt` lags behind the latest known trading day."""
    if dt is None:
        return None
    n = con.execute(
        "SELECT count(*) FROM calendar WHERE trade_date > ? AND trade_date <= current_date",
        [dt],
    ).fetchone()[0]
    return int(n)


def _freshness_label(lag: int | None) -> str:
    if lag is None:
        return "无数据 ❌"
    if lag == 0:
        return "最新 ✓"
    if lag == 1:
        return "滞后 1 个交易日 🟡"
    return f"滞后 {lag} 个交易日 🔴"


# ─────────────────────────────────────────────────────────────────────
# Formatters
# ─────────────────────────────────────────────────────────────────────

def _pct(x: float | None) -> str:
    if x is None or pd.isna(x):
        return "  n/a "
    arrow = UP if x > 0 else DN if x < 0 else FLAT
    return f"{arrow}{abs(x):>5.2f}%"


def _fmt_amt(amt: float | None) -> str:
    if amt is None or pd.isna(amt):
        return "n/a"
    a = float(amt)
    if a >= 1e8:
        return f"{a/1e8:>6.2f}亿"
    if a >= 1e4:
        return f"{a/1e4:>6.2f}万"
    return f"{a:>8.0f}"


def _section_header(title: str) -> str:
    return f"\n┌─ {title} {HR[len(title)+3:]}"


def _ds(d) -> str:
    """Format date-like value as YYYY-MM-DD."""
    if d is None or pd.isna(d):
        return "n/a"
    return pd.Timestamp(d).strftime("%Y-%m-%d")


# ─────────────────────────────────────────────────────────────────────
# Top-of-report freshness summary
# ─────────────────────────────────────────────────────────────────────

def section_freshness_panel(con: duckdb.DuckDBPyConnection) -> tuple[str, bool]:
    """Returns (text, has_stale). has_stale=True if any source lags > 0."""
    last_cal = _last_trading_day(con)

    rows = []
    # index_bar (all indices)
    idx = con.execute(
        "SELECT symbol, max(trade_date) AS d FROM index_bar GROUP BY symbol ORDER BY symbol"
    ).df()
    for _, r in idx.iterrows():
        lag = _trading_day_lag(con, r["d"])
        rows.append(("index_bar:" + str(r["symbol"]), r["d"], lag))

    # daily_bar (aggregate by type)
    bars = con.execute(
        "SELECT type, max(trade_date) AS d, count(DISTINCT symbol) AS n FROM daily_bar GROUP BY type"
    ).df()
    for _, r in bars.iterrows():
        lag = _trading_day_lag(con, r["d"])
        rows.append((f"daily_bar:{r['type']} ({int(r['n'])} symbols)", r["d"], lag))

    # northbound
    nb = con.execute(
        "SELECT channel, max(trade_date) AS d FROM northbound WHERE net_buy IS NOT NULL GROUP BY channel"
    ).df()
    for _, r in nb.iterrows():
        lag = _trading_day_lag(con, r["d"])
        rows.append((f"northbound:{r['channel']}", r["d"], lag))

    has_stale = any((lag is None or lag > 0) for _, _, lag in rows)

    lines = [_section_header(f"数据新鲜度 · 最新交易日 {_ds(last_cal)}")]
    if has_stale:
        lines.append("│ ⚠⚠⚠ 部分数据未跟到最新交易日，下面的"
                     "走势/排名/统计均为该数据日的状态，不是今日 ⚠⚠⚠")
    for name, d, lag in rows:
        lines.append(f"│ {name:<36} 截至 {_ds(d)}  {_freshness_label(lag)}")
    return "\n".join(lines), has_stale


# ─────────────────────────────────────────────────────────────────────
# Index thermometer (gated by data presence)
# ─────────────────────────────────────────────────────────────────────

def section_market_thermometer(con: duckdb.DuckDBPyConnection,
                               index_symbol: str = "000300") -> str:
    df = con.execute("""
        SELECT trade_date, close, pct_chg, change, amplitude, amount
        FROM index_bar WHERE symbol = ?
        ORDER BY trade_date DESC LIMIT 250
    """, [index_symbol]).df()
    if df.empty:
        return _section_header(f"指数温度计 ({index_symbol})") + "\n│ 无数据"

    df = df.sort_values("trade_date").reset_index(drop=True)
    latest = df.iloc[-1]
    data_dt = _ds(latest["trade_date"])
    lag = _trading_day_lag(con, latest["trade_date"])

    name_map = {"000300": "沪深300", "000001": "上证指数", "399001": "深证成指",
                "399006": "创业板指", "000905": "中证500", "000852": "中证1000"}
    name = name_map.get(index_symbol, index_symbol)

    ma60 = df["close"].rolling(60).mean().iloc[-1]
    ma200 = df["close"].rolling(200).mean().iloc[-1]
    ret_5d = (latest["close"] / df["close"].iloc[-6] - 1) * 100 if len(df) >= 6 else None
    ret_20d = (latest["close"] / df["close"].iloc[-21] - 1) * 100 if len(df) >= 21 else None
    ret_60d = (latest["close"] / df["close"].iloc[-61] - 1) * 100 if len(df) >= 61 else None

    diff_60 = (latest["close"] / ma60 - 1) * 100 if not pd.isna(ma60) else None
    diff_200 = (latest["close"] / ma200 - 1) * 100 if not pd.isna(ma200) else None
    pos_60 = "上方" if (diff_60 or 0) > 0 else "下方"
    pos_200 = "上方" if (diff_200 or 0) > 0 else "下方"

    if diff_60 is not None and diff_200 is not None:
        if diff_60 > 0 and diff_200 > 0:
            trend = "多头排列 · 上行"
        elif diff_60 < 0 and diff_200 < 0:
            trend = "空头排列 · 下行"
        else:
            trend = "震荡分歧"
    else:
        trend = "数据不足"

    header = f"指数温度计 · {name}({index_symbol}) · 数据截至 {data_dt} {_freshness_label(lag)}"
    lines = [
        _section_header(header),
        f"│ 收盘 {latest['close']:>10.2f}  {_pct(latest['pct_chg'])} (本地计算)  "
        f"{latest['change']:+.2f}pt  成交 {_fmt_amt(latest['amount'])}",
        f"│ 5日 {_pct(ret_5d)}    20日 {_pct(ret_20d)}    60日 {_pct(ret_60d)}  (本地计算)",
        f"│ MA60  {ma60:>8.2f} (现价{pos_60} {_pct(diff_60).strip()})  (本地计算)",
        f"│ MA200 {ma200:>8.2f} (现价{pos_200} {_pct(diff_200).strip()})  (本地计算)",
        f"│ 趋势判定: {trend}  (规则: MA60/MA200 同向, 非估值非ERP, 仅 Phase 0 占位)",
    ]
    return "\n".join(lines)


def section_northbound(con: duckdb.DuckDBPyConnection) -> str:
    df = con.execute("""
        SELECT channel, trade_date, net_buy, cum_net_buy, holding_value
        FROM northbound
        WHERE net_buy IS NOT NULL
        ORDER BY channel, trade_date DESC
    """).df()
    if df.empty:
        return _section_header("北向资金") + "\n│ 无有效数据"

    last_dts = df.groupby("channel")["trade_date"].max()
    overall_last = last_dts.max()
    lag = _trading_day_lag(con, overall_last)

    header = (f"北向资金 · 数据截至 {_ds(overall_last)} {_freshness_label(lag)} · "
              "(上游 2024-08 起停发当日净买额)")
    lines = [_section_header(header)]
    for ch in ["北向", "沪股通", "深股通"]:
        sub = df[df["channel"] == ch].sort_values("trade_date", ascending=False)
        if sub.empty:
            continue
        latest = sub.iloc[0]
        last20 = sub.head(20)["net_buy"].sum()
        last5 = sub.head(5)["net_buy"].sum()
        lines.append(
            f"│ {ch:<4} 最后日 {_ds(latest['trade_date'])}  "
            f"当日 {latest['net_buy']:+8.2f}亿  "
            f"近5日 {last5:+8.2f}亿  近20日 {last20:+8.2f}亿"
        )
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# Market breadth (with data date in title)
# ─────────────────────────────────────────────────────────────────────

def _format_bar_line(r) -> str:
    name = (r["name"] or "")[:10]
    turn = f"换手 {r['turnover']:>5.2f}%" if not pd.isna(r["turnover"]) else "换手   n/a"
    return (f"│ {r['symbol']} {name:<10} [{r['type']:<5}]  "
            f"{r['close']:>8.3f}  {_pct(r['pct_chg'])}  "
            f"{_fmt_amt(r['amount'])}  {turn}")


def section_market_breadth(con: duckdb.DuckDBPyConnection,
                           watchlist: list[str] | None = None,
                           top_n: int = 5) -> str:
    latest_df = con.execute("""
        WITH latest AS (
            SELECT symbol, max(trade_date) AS d FROM daily_bar GROUP BY symbol
        )
        SELECT b.symbol, b.type, i.name, b.trade_date,
               b.close, b.pct_chg, b.amount, b.turnover
        FROM daily_bar b
        JOIN latest l ON b.symbol = l.symbol AND b.trade_date = l.d
        LEFT JOIN instruments i ON b.symbol = i.symbol
    """).df()
    if latest_df.empty:
        return _section_header("市场宽度") + "\n│ 无数据"

    # The bar data may be inhomogeneous (different last dates per symbol).
    # Be honest: use the mode date and report how many symbols share it.
    mode_dt = latest_df["trade_date"].mode().iloc[0]
    today_df = latest_df[latest_df["trade_date"] == mode_dt].copy()
    n_at_mode = len(today_df)
    n_other = len(latest_df) - n_at_mode
    lag = _trading_day_lag(con, mode_dt)

    up = int((today_df["pct_chg"] > 0).sum())
    dn = int((today_df["pct_chg"] < 0).sum())
    flat = int((today_df["pct_chg"] == 0).sum())

    header = (f"市场宽度 · 数据截至 {_ds(mode_dt)} {_freshness_label(lag)} · "
              f"{n_at_mode} 只在此日有 bar"
              + (f" (另 {n_other} 只更旧, 已排除)" if n_other else ""))
    lines = [
        _section_header(header),
        f"│ 上涨 {up:>4} ▲   下跌 {dn:>4} ▼   平盘 {flat:>4} —   "
        f"(注: 仅本库 {n_at_mode} 只样本, 非全市场)",
    ]

    if watchlist:
        wl_df = today_df[today_df["symbol"].isin(watchlist)]
        missing = [s for s in watchlist if s not in today_df["symbol"].tolist()]
        lines.append("│")
        lines.append("│ — 关注列表 —")
        for _, r in wl_df.iterrows():
            lines.append(_format_bar_line(r))
        for m in missing:
            lines.append(f"│ {m} (无 {_ds(mode_dt)} 数据 ❌)")

    gainers = today_df.nlargest(top_n, "pct_chg")
    lines.append("│")
    lines.append(f"│ — 涨幅 Top {top_n} (限本库样本) —")
    for _, r in gainers.iterrows():
        lines.append(_format_bar_line(r))

    losers = today_df.nsmallest(top_n, "pct_chg")
    lines.append("│")
    lines.append(f"│ — 跌幅 Top {top_n} (限本库样本) —")
    for _, r in losers.iterrows():
        lines.append(_format_bar_line(r))

    movers = today_df.nlargest(top_n, "amount")
    lines.append("│")
    lines.append(f"│ — 成交额 Top {top_n} (限本库样本) —")
    for _, r in movers.iterrows():
        lines.append(_format_bar_line(r))

    return "\n".join(lines)


def section_coverage(con: duckdb.DuckDBPyConnection) -> str:
    inst = con.execute(
        "SELECT count(*), sum(CASE WHEN type='stock' THEN 1 ELSE 0 END), "
        "sum(CASE WHEN type='etf' THEN 1 ELSE 0 END) FROM instruments"
    ).fetchone()
    bars = con.execute("""
        SELECT count(DISTINCT symbol), count(*), min(trade_date), max(trade_date)
        FROM daily_bar
    """).fetchone()
    idx = con.execute("SELECT count(DISTINCT symbol), count(*) FROM index_bar").fetchone()
    nb = con.execute("SELECT count(*) FROM northbound").fetchone()[0]
    lines = [
        _section_header("数据库覆盖"),
        f"│ instruments  {inst[0]:>5} 只  (股 {int(inst[1] or 0)} / ETF {int(inst[2] or 0)})",
        f"│ daily_bar    {bars[0]:>5} 标的 / {bars[1]} 行 / {_ds(bars[2])} → {_ds(bars[3])}",
        f"│ index_bar    {idx[0]:>5} 指数 / {idx[1]} 行",
        f"│ northbound   {nb:>5} 行 (3 通道, 上游 2024-08 后停发)",
    ]
    return "\n".join(lines)


def section_my_portfolio() -> str | None:
    """My real holdings (positions.yaml), valued live. None if no positions file."""
    from ..portfolio import load_positions, value_portfolio
    pf = load_positions()
    if pf is None or not pf.holdings:
        return None
    pf = value_portfolio(pf)

    lines = [_section_header("我的持仓 · 实时估值(腾讯, 盘中未定稿)")]

    valued = pf.valued
    if valued:
        total = pf.total_market_value
        lines.append(f"│ 可估值市值 {total/1e4:.2f}万"
                     + (f" (含现金 {pf.cash/1e4:.2f}万)" if pf.cash else ""))
        # sort by weight desc
        valued_sorted = sorted(valued, key=lambda h: pf.weight(h) or 0, reverse=True)
        for h in valued_sorted:
            w = pf.weight(h)
            tp = h.today_pct
            pnl_pct = h.pnl_pct
            tp_s = _pct(tp) if tp is not None else "  n/a "
            pnl_s = f"浮盈 {pnl_pct:+.1f}%" if pnl_pct is not None else "浮盈 n/a(无成本)"
            lines.append(
                f"│ {h.code} {h.name[:14]:<14} 权重{w:>5.1f}%  今日{tp_s}  {pnl_s}"
            )
        # today's total portfolio impact
        tps = [h.today_pnl for h in valued if h.today_pnl is not None]
        if tps:
            tot_today = sum(tps)
            lines.append(f"│ 今日持仓盈亏合计: {tot_today:+,.0f}元 "
                         f"({tot_today/total*100:+.2f}% of 可估值市值)")
        # concentration by type
        by_type: dict[str, float] = {}
        for h in valued:
            by_type[h.type] = by_type.get(h.type, 0) + (h.market_value or 0)
        conc = sorted(by_type.items(), key=lambda kv: kv[1], reverse=True)
        conc_s = "  ".join(f"{t}:{v/total*100:.0f}%" for t, v in conc)
        lines.append(f"│ 类型集中度: {conc_s}")

    unv = pf.unvalued
    if unv:
        lines.append("│")
        lines.append("│ — 暂无法估值(诚实标注) —")
        for h in unv:
            lines.append(f"│ {h.code} {h.name[:16]:<16} → {h.note or '无价'}")

    return "\n".join(lines)


def section_next_steps() -> str:
    return "\n".join([
        _section_header("路线提醒"),
        "│ Phase 0 ✓ 数据底座 (calendar/instruments/bars/northbound)",
        "│ Phase 1 ⇒ 复权因子 + 全市场回填 + APScheduler + 估值/财报/宏观",
        "│ Phase 3 ⭐ 真·指数温度计 (估值分位 + ERP + 趋势综合判定)",
        "│ 详见 docs/ROADMAP.md",
    ])


def render_daily_report(con: duckdb.DuckDBPyConnection,
                        index_symbol: str = "000300",
                        watchlist: list[str] | None = None) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    last_cal = _last_trading_day(con)

    freshness_text, has_stale = section_freshness_panel(con)

    banner_lines = [
        "╔════════════════════════════════════════════════════════════════╗",
        f"║  A股辅助报告 · 生成 {now} · 最新交易日 {_ds(last_cal)}",
        "╚════════════════════════════════════════════════════════════════╝",
    ]
    if has_stale:
        banner_lines.insert(2,
            "║  🟡 注意: 部分数据未跟到最新交易日, 详见下方[数据新鲜度]")

    from ..signals.sentiment import latest_sentiment, render_sentiment
    sent_row = latest_sentiment(con)

    sections = [freshness_text]
    if sent_row:
        sections.append(render_sentiment(sent_row))
    sections += [
        section_market_thermometer(con, index_symbol),
        section_northbound(con),
        section_market_breadth(con, watchlist=watchlist),
    ]
    portfolio = section_my_portfolio()
    if portfolio:
        sections.append(portfolio)
    sections += [
        section_coverage(con),
        section_next_steps(),
    ]
    return "\n".join(banner_lines) + "\n" + "\n".join(sections) + "\n"
