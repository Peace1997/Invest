"""One public refresh operation shared by the dashboard and scheduler."""
from __future__ import annotations
from datetime import date

from ..ingest.calendar import ingest_calendar
from ..ingest.fund_nav import refresh_fund_nav
from ..ingest.market_state import ingest_market_breadth
from ..ingest.valuation import refresh_valuation
from ..portfolio import load_positions
from ..sources import AkSource
from .daily_update import daily_update, expected_latest_trade_date


def _latest(con, table: str, date_column: str):
    return con.execute(f"SELECT max({date_column}) FROM {table}").fetchone()[0]


def market_refresh_failures(result: dict) -> list[str]:
    """Return one shared failure verdict for the UI, CLI and scheduler."""
    failures = [f"{stage}: {error}" for stage, error in result.get("errors", {}).items()]
    daily = result.get("daily", {})
    failures.extend(
        f"行情失败 {kind} {code}: {message}"
        for kind, code, message in daily.get("failed", [])
    )
    if daily.get("all_failed"):
        failures.append("行情源未写入任何数据")
    elif daily.get("stale"):
        failures.append("整体行情未追到应有交易日")
    for code in result.get("fund_nav", {}).get("failed", []):
        failures.append(f"基金净值失败: {code}")
    for code in result.get("valuation", {}).get("failed", []):
        failures.append(f"估值失败: {code}")
    for code, actual in result.get("stale_targets", {}).items():
        failures.append(f"持仓行情落后: {code}（{actual or '无数据'}）")
    return failures


def refresh_market_data(con, cfg: dict, src: AkSource | None = None) -> dict:
    """Refresh bars, held-fund NAVs and portfolio valuation in one operation."""
    src = src or AkSource(
        per_call_sleep=cfg["rate_limit"]["per_call_sleep"],
        max_retries=cfg["rate_limit"]["max_retries"],
    )
    result: dict = {"errors": {}, "warnings": {}}
    indices = cfg.get("sample_targets", {}).get("indices") or ("000300",)
    portfolio = load_positions()
    extra_instruments = tuple(
        (holding.code, holding.type)
        for holding in (portfolio.holdings if portfolio else [])
        if holding.type in ("stock", "etf") and holding.code.isdigit()
    )

    try:
        result["calendar"] = ingest_calendar(con, src)
    except Exception as exc:  # noqa: BLE001
        calendar_horizon = _latest(con, "calendar", "trade_date")
        if calendar_horizon is None or calendar_horizon < date.today():
            result["errors"]["交易日历"] = str(exc)
        else:
            result["warnings"]["交易日历"] = str(exc)

    try:
        result["daily"] = daily_update(
            con, src, indices=indices, universe="db", refresh_metadata=False,
            extra_instruments=extra_instruments,
        )
    except Exception as exc:  # noqa: BLE001 - keep later refresh stages independent
        result["daily"] = {}
        result["errors"]["行情"] = str(exc)
    try:
        result["market_breadth"] = ingest_market_breadth(con)
    except Exception as exc:  # noqa: BLE001
        result["market_breadth"] = 0
        result["errors"]["市场温度"] = str(exc)

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

    expected_market = expected_latest_trade_date(con)
    target_dates = {
        code: con.execute(
            "SELECT max(trade_date) FROM daily_bar WHERE symbol=? AND type=?",
            [code, kind],
        ).fetchone()[0]
        for code, kind in extra_instruments
    }
    result["dates"] = {
        "expected_market": expected_latest_trade_date(con),
        "market": _latest(con, "daily_bar", "trade_date"),
        "fund_nav": _latest(con, "fund_nav", "nav_date"),
        "valuation": _latest(con, "valuation_daily", "trade_date"),
        "calendar_horizon": _latest(con, "calendar", "trade_date"),
        "targets": target_dates,
    }
    result["stale_targets"] = {
        code: actual.isoformat() if actual else None
        for code, actual in target_dates.items()
        if expected_market is not None and (actual is None or actual < expected_market)
    }
    return result
