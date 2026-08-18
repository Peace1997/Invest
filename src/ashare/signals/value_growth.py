"""Point-in-time value/growth screen shared by the morning and afternoon pushes."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd

from ..sources import TencentSource


TECH_INDUSTRY_TERMS = (
    "半导体", "软件", "计算机", "通信", "电子", "自动化", "机器人",
    "光学光电子", "消费电子", "元件", "电池", "光伏", "航空装备",
    "航天装备", "游戏", "数字媒体", "互联网", "IT服务",
)


def _is_tech(industry: object) -> bool:
    name = str(industry or "")
    return any(term in name for term in TECH_INDUSTRY_TERMS)


def rank_value_growth_candidates(
    rows: pd.DataFrame,
    *,
    limit: int,
    tech_share: float = 0.25,
) -> pd.DataFrame:
    """Apply the audited gate, rank by valuation, and reserve a tech share.

    Growth is deliberately a gate rather than a ranking reward. The 2021-2026
    point-in-time backtest found that ranking ever-higher reported growth made
    results worse, while requiring positive profit and revenue growth improved
    the low-valuation portfolio.
    """
    if rows is None or rows.empty or limit <= 0:
        return pd.DataFrame()
    out = rows.copy()
    numeric = (
        "pe_ttm", "pb", "total_mv", "roe", "debt_ratio",
        "profit_yoy", "revenue_yoy", "valuation_lag_days",
    )
    for column in numeric:
        out[column] = pd.to_numeric(out[column], errors="coerce")
    out = out[
        out["pe_ttm"].between(0.01, 60)
        & out["pb"].between(0.01, 8)
        & (out["total_mv"] >= 20)
        & (out["roe"] >= 5)
        & (out["debt_ratio"] < 85)
        & out["profit_yoy"].between(0.01, 200)
        & out["revenue_yoy"].between(5, 100)
        & (out["valuation_lag_days"] <= 30)
    ].copy()
    if out.empty:
        return out

    out["is_tech"] = out["industry"].map(_is_tech)
    out["pe_rank"] = out["pe_ttm"].rank(pct=True)
    out["pb_rank"] = out["pb"].rank(pct=True)
    out["score"] = 0.65 * (1 - out["pe_rank"]) + 0.35 * (1 - out["pb_rank"])
    out = out.sort_values(["score", "symbol"], ascending=[False, True])

    tech_target = min(len(out[out["is_tech"]]), max(1, round(limit * tech_share)))
    tech = out[out["is_tech"]].head(tech_target)
    remainder = out[~out["symbol"].isin(tech["symbol"])].head(limit - len(tech))
    return pd.concat([tech, remainder], ignore_index=True).sort_values(
        ["score", "symbol"], ascending=[False, True]
    ).reset_index(drop=True)


def screen_value_growth(con, *, limit: int = 25, as_of: date | None = None) -> pd.DataFrame:
    """Read the latest point-in-time valuation/fundamentals and return picks."""
    as_of = as_of or con.execute(
        "SELECT max(trade_date) FROM daily_bar WHERE trade_date <= current_date"
    ).fetchone()[0]
    if as_of is None:
        return pd.DataFrame()
    rows = con.execute(
        """
        WITH latest_valuation AS (
            SELECT symbol, trade_date AS valuation_date, pe_ttm, pb, total_mv
            FROM valuation_daily_canon
            WHERE trade_date <= ?
            QUALIFY row_number() OVER (
                PARTITION BY symbol ORDER BY trade_date DESC
            ) = 1
        ),
        latest_fundamental AS (
            SELECT symbol, end_date AS report_date, ann_date,
                   roe, gross_margin, debt_ratio, profit_yoy, revenue_yoy
            FROM fundamentals_ts
            WHERE ann_date IS NOT NULL AND ann_date <= ?
            QUALIFY row_number() OVER (
                PARTITION BY symbol ORDER BY ann_date DESC, end_date DESC
            ) = 1
        ),
        latest_industry AS (
            SELECT symbol, arg_max(industry, report_date) AS industry
            FROM fundamentals
            WHERE report_date <= ?
            GROUP BY symbol
        )
        SELECT i.symbol, i.name, x.industry,
               v.valuation_date, date_diff('day', v.valuation_date, ?) AS valuation_lag_days,
               v.pe_ttm, v.pb, v.total_mv,
               f.report_date, f.ann_date, f.roe, f.gross_margin,
               f.debt_ratio, f.profit_yoy, f.revenue_yoy
        FROM instruments i
        JOIN latest_valuation v ON v.symbol=i.symbol
        JOIN latest_fundamental f ON f.symbol=i.symbol
        LEFT JOIN latest_industry x ON x.symbol=i.symbol
        WHERE i.type='stock'
          AND (
              i.symbol LIKE '60%' OR i.symbol LIKE '68%'
              OR i.symbol LIKE '00%' OR i.symbol LIKE '30%'
          )
          AND coalesce(i.is_st, false)=false
          AND (i.delist_date IS NULL OR i.delist_date > ?)
        """,
        [as_of, as_of, as_of, as_of, as_of],
    ).df()
    return rank_value_growth_candidates(rows, limit=limit)


def attach_live_quotes(
    candidates: pd.DataFrame,
    *,
    source: TencentSource | None = None,
    now: datetime | None = None,
    max_age: timedelta = timedelta(minutes=5),
) -> pd.DataFrame:
    """Attach quotes observed within the decision-time freshness window."""
    if candidates is None or candidates.empty:
        return pd.DataFrame()
    source = source or TencentSource()
    quotes = source.spot(candidates["symbol"].astype(str).tolist())
    if quotes is None or quotes.empty:
        return pd.DataFrame()
    observed_at = pd.Timestamp(now or datetime.now())
    quotes = quotes.dropna(subset=["price", "quote_time"]).copy()
    quotes["quote_time"] = pd.to_datetime(quotes["quote_time"], errors="coerce")
    age = observed_at - quotes["quote_time"]
    quotes = quotes[
        quotes["quote_time"].notna()
        & (age >= pd.Timedelta(minutes=-1))
        & (age <= pd.Timedelta(max_age))
    ]
    quotes = quotes[pd.to_numeric(quotes["price"], errors="coerce") > 0]
    return candidates.merge(
        quotes[["symbol", "price", "prev_close", "pct_chg", "quote_time"]],
        on="symbol",
        how="inner",
    )


def candidate_reason(row) -> str:
    industry = str(row.industry) if pd.notna(row.industry) else "行业未标注"
    tech = f"科技优先·{industry}" if bool(row.is_tech) else industry
    return (
        f"{tech}；PE {row.pe_ttm:.1f}、PB {row.pb:.2f}；"
        f"利润同比 {row.profit_yoy:+.1f}%、营收同比 {row.revenue_yoy:+.1f}%、"
        f"ROE {row.roe:.1f}%"
    )


def push_body(candidates: pd.DataFrame, *, snapshot_label: str) -> str:
    lines = [
        "低估值成长观察：利润增速0-200%、营收增速5-100%、ROE≥5%为门槛，按 PE/PB 排序，Top 5 优先保留1只科技候选。",
        "价格只取交易所时间戳行情，不使用 AI 生成价格。\n",
    ]
    for row in candidates.itertuples():
        quote_time = pd.Timestamp(row.quote_time).strftime("%H:%M:%S")
        pct = f"{row.pct_chg:+.2f}%" if pd.notna(row.pct_chg) else "暂无"
        lines.append(
            f"**{row.name}（{row.symbol}）**{' · 科技' if row.is_tech else ''}\n"
            f"· {snapshot_label}: {row.price:.2f} 元（行情时间 {quote_time}，当日 {pct}）\n"
            f"· 依据: {candidate_reason(row)}\n"
            f"· 数据: 估值 {row.valuation_date}；财报 {row.report_date}，公告 {row.ann_date}\n"
        )
    lines.append("———\n这是中期观察池，不追高，不因单日涨跌直接买入；数据与回测均不保证未来收益。")
    return "\n".join(lines)


def decision_items(candidates: pd.DataFrame, *, as_of: date) -> list[dict]:
    return [
        {
            "code": str(row.symbol).zfill(6),
            "name": row.name,
            "reason": candidate_reason(row),
            "risk": "估值、增长和行业偏好只是概率筛选，需复核最新公告与价格位置",
            "ref_price": float(row.price),
            "trade_date": as_of.isoformat(),
        }
        for row in candidates.itertuples()
    ]
