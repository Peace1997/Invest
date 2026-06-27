"""Portfolio history snapshots: persist a daily point-in-time valuation so we
can draw a return curve and compute max drawdown over time.

Two grains are written each run (idempotent on snapshot_date):
  - portfolio_snapshot : one row per day  (totals)
  - position_snapshot  : one row per day × holding (detail)

Honesty: a snapshot records what we could *value* at run time. Fund NAV is T-1,
stocks are live/closing — so today_pnl is the same cross-date approximation the
daily report already flags. We store it as-is and never invent missing prices.
"""
from __future__ import annotations
from datetime import date, datetime

import pandas as pd

from .portfolio import Portfolio


def snapshot_portfolio(con, pf: Portfolio, when: date | None = None) -> date:
    """Upsert today's totals + per-holding detail into the warehouse."""
    d = when or date.today()

    totals = pd.DataFrame([{
        "snapshot_date": d,
        "total_market_value": pf.total_market_value,
        "total_cost": pf.total_cost_value,
        "total_pnl": pf.total_pnl,
        "today_pnl": pf.total_today_pnl,
        "cash": pf.cash,
        "n_holdings": len(pf.valued),
    }])

    rows = []
    for h in pf.holdings:
        pd_ = h.price_date
        if isinstance(pd_, datetime):
            pd_ = pd_.date()
        elif isinstance(pd_, str) and pd_:
            try:
                pd_ = pd.to_datetime(pd_).date()
            except (ValueError, TypeError):
                pd_ = None
        rows.append({
            "snapshot_date": d,
            "code": h.code,
            "name": h.name,
            "type": h.type,
            "shares": h.shares,
            "cost": h.cost or None,
            "price": h.price,
            "price_date": pd_ if isinstance(pd_, date) else None,
            "price_kind": h.price_kind or None,
            "market_value": h.market_value,
            "pnl": h.pnl,
            "today_pnl": h.today_pnl,
            "weight": pf.weight(h),
        })
    detail = pd.DataFrame(rows)

    from .storage import upsert
    upsert(con, "portfolio_snapshot", totals, keys=["snapshot_date"])
    upsert(con, "position_snapshot", detail, keys=["snapshot_date", "code"])
    return d


def load_history(con) -> pd.DataFrame:
    """All portfolio-level snapshots, oldest→newest."""
    return con.execute(
        "SELECT * FROM portfolio_snapshot ORDER BY snapshot_date"
    ).df()


def max_drawdown(series: pd.Series) -> tuple[float, date | None, date | None]:
    """Max drawdown (%) of an equity series, with peak/trough dates.

    Returns (drawdown_pct<=0, peak_date, trough_date). Empty/flat → (0, None, None).
    """
    if series is None or len(series) < 2:
        return 0.0, None, None
    running_peak = series.cummax()
    dd = (series - running_peak) / running_peak * 100.0
    trough_i = dd.idxmin()
    mdd = dd.loc[trough_i]
    if mdd >= 0:
        return 0.0, None, None
    peak_i = series.loc[:trough_i].idxmax()
    return float(mdd), peak_i, trough_i
