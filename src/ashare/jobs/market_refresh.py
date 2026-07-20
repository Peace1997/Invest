"""One public refresh operation shared by the dashboard and scheduler."""
from __future__ import annotations

from ..ingest.fund_nav import refresh_fund_nav
from ..ingest.market_state import ingest_market_breadth
from ..ingest.valuation import refresh_valuation
from ..portfolio import load_positions
from ..sources import AkSource
from .daily_update import daily_update, expected_latest_trade_date


def _latest(con, table: str, date_column: str):
    return con.execute(f"SELECT max({date_column}) FROM {table}").fetchone()[0]


def refresh_market_data(con, cfg: dict, src: AkSource | None = None) -> dict:
    """Refresh bars, held-fund NAVs and portfolio valuation in one operation."""
    src = src or AkSource(
        per_call_sleep=cfg["rate_limit"]["per_call_sleep"],
        max_retries=cfg["rate_limit"]["max_retries"],
    )
    result: dict = {"errors": {}}
    indices = cfg.get("sample_targets", {}).get("indices") or ("000300",)

    try:
        result["daily"] = daily_update(
            con, src, indices=indices, universe="db", refresh_metadata=False,
        )
    except Exception as exc:  # noqa: BLE001 - keep later refresh stages independent
        result["daily"] = {}
        result["errors"]["行情"] = str(exc)
    try:
        result["market_breadth"] = ingest_market_breadth(con)
    except Exception as exc:  # noqa: BLE001
        result["market_breadth"] = 0
        result["errors"]["市场温度"] = str(exc)

    portfolio = load_positions()
    if portfolio and portfolio.holdings:
        try:
            result["fund_nav"] = refresh_fund_nav(con, portfolio, ak=src)
        except Exception as exc:  # noqa: BLE001
            result["fund_nav"] = {"done": [], "skipped": [], "failed": []}
            result["errors"]["基金净值"] = str(exc)
        try:
            result["valuation"] = refresh_valuation(
                con, portfolio, index_symbol=indices[0], ak=src,
            )
        except Exception as exc:  # noqa: BLE001
            result["valuation"] = {"done": [], "skipped": [], "failed": []}
            result["errors"]["估值"] = str(exc)
    else:
        result["fund_nav"] = {"done": [], "skipped": [], "failed": []}
        result["valuation"] = {"done": [], "skipped": [], "failed": []}

    result["dates"] = {
        "expected_market": expected_latest_trade_date(con),
        "market": _latest(con, "daily_bar", "trade_date"),
        "fund_nav": _latest(con, "fund_nav", "nav_date"),
        "valuation": _latest(con, "valuation_daily", "trade_date"),
    }
    return result
