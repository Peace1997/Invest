from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime

from .config import resolve_path
from .repositories import DecisionJournal, MarketWarehouse

_DEFAULT_HORIZONS = {
    "momentum": 60,
    "value": 60,
    "leader": 60,
    "reversal": 20,
    "premarket": 1,
    "endday": 1,
    "pf": 20,
}

_DEFENSIVE_WORDS = ("减仓", "止损", "止盈", "谨慎", "卖出", "回避", "暂停")


def signal_id(trade_date, source: str, style: str | None, symbol: str, horizon_days: int) -> str:
    raw = f"{_as_date(trade_date)}|{source}|{style}|{symbol}|{horizon_days}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def record_recommend_signals(
    cfg: dict,
    con,
    stocks: list,
    funds: list,
    style: str,
    *,
    journal: DecisionJournal | None = None,
) -> int:
    if not _enabled(cfg):
        return 0
    journal = journal or open_decision_journal(cfg, con)
    horizon = _horizon(cfg, style)
    rows = []
    for sig in stocks or []:
        rows.append(_signal_from_output(
            con, sig, "recommend", style, "stock", horizon, "关注·买入观察"
        ))
    for sig in funds or []:
        asset_type = _asset_type((getattr(sig, "metadata", {}) or {}).get("type") or "etf")
        rows.append(_signal_from_output(
            con, sig, "recommend", style, asset_type, horizon, "关注·买入观察"
        ))
    return journal.record_many([row for row in rows if row])


def record_pf_signals(
    cfg: dict,
    con,
    pf,
    advice: list,
    *,
    journal: DecisionJournal | None = None,
) -> int:
    if not _enabled(cfg):
        return 0
    journal = journal or open_decision_journal(cfg, con)
    holdings = {h.code.zfill(6): h for h in getattr(pf, "holdings", [])}
    horizon = _horizon(cfg, "pf")
    rows = []
    market = MarketWarehouse(con)
    for a in advice or []:
        code = str(getattr(a, "code", "")).zfill(6)
        h = holdings.get(code)
        if not h:
            continue
        rows.append(_make_signal(
            market=market,
            source="pf",
            style=None,
            symbol=code,
            name=getattr(a, "name", code),
            asset_type=_asset_type(getattr(h, "type", "")),
            action=getattr(a, "action", ""),
            score=_clean_float(getattr(a, "score", None)),
            horizon_days=horizon,
            factors=json.dumps({"持仓建议": getattr(a, "reason", "")}, ensure_ascii=False),
            rationale=getattr(a, "reason", None),
            as_of=date.today(),
        ))
    return journal.record_many([row for row in rows if row])


def record_shortterm_result(
    cfg: dict,
    con,
    source: str,
    result: dict,
    *,
    journal: DecisionJournal | None = None,
) -> int:
    if not _enabled(cfg):
        return 0
    items = result.get("items") if isinstance(result, dict) else None
    if not items:
        return 0
    return record_shortterm_signals(cfg, con, source, items, journal=journal)


def record_shortterm_signals(
    cfg: dict,
    con,
    source: str,
    items: list[dict],
    *,
    journal: DecisionJournal | None = None,
    as_of: date | None = None,
) -> int:
    if not _enabled(cfg):
        return 0
    journal = journal or open_decision_journal(cfg, con)
    horizon = _horizon(cfg, source)
    market = MarketWarehouse(con)
    rows = []
    for item in items or []:
        code = str(item.get("code") or "").zfill(6)
        if not (code.isdigit() and len(code) == 6):
            continue
        factors = {
            "性质": "短线投机线索",
            "reason": item.get("reason"),
            "watch": item.get("watch"),
            "stop": item.get("stop"),
            "risk": item.get("risk"),
        }
        rows.append(_make_signal(
            market=market,
            source=source,
            style=None,
            symbol=code,
            name=item.get("name") or code,
            asset_type="stock",
            action="短线关注",
            score=None,
            horizon_days=horizon,
            factors=json.dumps(factors, ensure_ascii=False),
            rationale=item.get("reason"),
            as_of=as_of or date.today(),
            ref_price=item.get("ref_price"),
            trade_date=item.get("trade_date"),
        ))
    return journal.record_many([row for row in rows if row])


def evaluate_due_signals(
    cfg: dict,
    con,
    force: bool = False,
    *,
    journal: DecisionJournal | None = None,
    as_of: date | None = None,
) -> dict:
    journal = journal or open_decision_journal(cfg, con)
    market = MarketWarehouse(con)
    as_of = as_of or market.latest_trade_date(date.today()) or date.today()
    signal_cfg = cfg.get("decision_signal", {}) or {}
    threshold = float(signal_cfg.get("hit_excess_pct", 1.0))
    review_horizons = {
        int(value)
        for value in (signal_cfg.get("review_horizons", []) or [])
        if int(value) > 0
    }
    candidates = journal.open_signals(
        include_closed=force or bool(review_horizons)
    )
    recorded_horizons = journal.recorded_horizons()

    observations = []
    settlements = []
    scanned = 0
    pending_data = 0
    unable = 0
    for s in candidates:
        elapsed = market.trading_days_elapsed(s["trade_date"], as_of)
        horizons = sorted(review_horizons | {int(s["horizon_days"])})
        due_horizons = [horizon for horizon in horizons if horizon <= elapsed]
        if not due_horizons:
            continue
        scanned += 1
        was_closed = s.get("status") == "closed"
        ref_price = _clean_float(s.get("ref_price"))
        if ref_price is None or ref_price <= 0:
            if not was_closed:
                journal.mark_status(s["signal_id"], "unable")
            unable += 1
            continue

        b_ref = market.close_on_or_before("000300", "index", s["trade_date"])
        for horizon in due_horizons:
            if not force and horizon in recorded_horizons.get(s["signal_id"], set()):
                continue
            fwd = market.forward_close(
                s["symbol"], s["asset_type"], s["trade_date"], horizon
            )
            b_end = market.forward_close(
                "000300", "index", s["trade_date"], horizon
            )
            if not (fwd and b_ref and b_end and b_ref[1] > 0):
                if not was_closed and horizon == int(s["horizon_days"]):
                    journal.mark_status(s["signal_id"], "pending_data")
                pending_data += 1
                continue

            end_date, end_price, bars_used = fwd
            fwd_pct = (end_price / ref_price - 1.0) * 100
            bench_pct = (b_end[1] / b_ref[1] - 1.0) * 100
            excess = fwd_pct - bench_pct
            outcome = _judge_outcome(
                str(s.get("action") or ""), fwd_pct, excess, threshold
            )
            result = {
                "signal_id": s["signal_id"],
                "evaluated_at": datetime.now(),
                "end_date": end_date,
                "bars_forward": bars_used,
                "ref_price": ref_price,
                "end_price": end_price,
                "fwd_return_pct": fwd_pct,
                "bench_return_pct": bench_pct,
                "excess_pct": excess,
                "outcome": outcome,
                "note": None,
            }
            if horizon == int(s["horizon_days"]):
                settlements.append(result)
            else:
                observations.append(result)
    observed = journal.record_outcomes(observations)
    closed = journal.settle_many(settlements)
    return {
        "scanned": scanned,
        "evaluated": observed + closed,
        "closed": closed,
        "pending_data": pending_data,
        "unable": unable,
        "as_of": as_of,
    }


def _signal_from_output(con, sig, source: str, style: str | None,
                        asset_type: str, horizon: int, action: str) -> dict | None:
    market = MarketWarehouse(con)
    meta = getattr(sig, "metadata", {}) or {}
    factors = {
        "reason": getattr(sig, "reason", None),
        "components": [
            {
                "label": getattr(c, "label", ""),
                "contribution": getattr(c, "contribution", None),
                "detail": getattr(c, "detail", ""),
                "cap": getattr(c, "cap", ""),
            }
            for c in (getattr(sig, "components", None) or [])
        ],
        "metadata": {k: v for k, v in meta.items() if k not in ("levels", "bullets")},
    }
    return _make_signal(
        market=market,
        source=source,
        style=style,
        symbol=str(getattr(sig, "target", "")).replace("INDEX:", "").zfill(6),
        name=meta.get("name"),
        asset_type=asset_type,
        action=action,
        score=_clean_float(getattr(sig, "score", None)),
        horizon_days=horizon,
        factors=json.dumps(factors, ensure_ascii=False, default=str),
        rationale=getattr(sig, "reason", None),
        as_of=getattr(sig, "as_of", None) or date.today(),
    )


def _make_signal(*, market: MarketWarehouse, source: str, style: str | None, symbol: str,
                 name: str | None, asset_type: str, action: str, score: float | None,
                 horizon_days: int, factors: str | None, rationale: str | None, as_of,
                 ref_price=None, trade_date=None) -> dict:
    symbol = str(symbol).zfill(6)
    explicit_price = _clean_float(ref_price)
    explicit_date = _as_date(trade_date)
    anchor = (
        (explicit_date, explicit_price)
        if explicit_date and explicit_price is not None and explicit_price > 0
        else market.close_on_or_before(symbol, asset_type, _as_date(as_of) or date.today())
    )
    signal_date = anchor[0] if anchor else (
        _as_date(as_of) or market.latest_trade_date() or date.today()
    )
    signal_price = anchor[1] if anchor else None
    status = "active" if signal_price is not None else "unable"
    return {
        "signal_id": signal_id(signal_date, source, style, symbol, int(horizon_days)),
        "created_at": datetime.now(),
        "trade_date": signal_date,
        "source": source,
        "style": style,
        "symbol": symbol,
        "name": name,
        "asset_type": asset_type,
        "action": action,
        "score": score,
        "ref_price": signal_price,
        "horizon_days": int(horizon_days),
        "factors": factors,
        "rationale": rationale,
        "status": status,
    }


def _judge_outcome(action: str, fwd_pct: float, excess_pct: float | None, threshold: float) -> str:
    if any(w in action for w in _DEFENSIVE_WORDS):
        metric = fwd_pct
        if metric < -threshold:
            return "hit"
        if metric > threshold:
            return "miss"
        return "neutral"
    metric = fwd_pct if excess_pct is None else excess_pct
    if metric > threshold:
        return "hit"
    if metric < -threshold:
        return "miss"
    return "neutral"


def _enabled(cfg: dict) -> bool:
    return (cfg.get("decision_signal", {}) or {}).get("enabled", True)


def _horizon(cfg: dict, key: str) -> int:
    horizons = (cfg.get("decision_signal", {}) or {}).get("horizons", {}) or {}
    return int(horizons.get(key, _DEFAULT_HORIZONS[key]))


def _asset_type(raw: str) -> str:
    raw = str(raw or "").strip()
    if raw in ("stock", "etf", "index", "otc_fund"):
        return raw
    if raw == "bond_fund":
        return "otc_fund"
    return "stock"


def _clean_float(x):
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else v


def _as_date(x):
    if x is None:
        return None
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    if isinstance(x, str):
        return date.fromisoformat(x)
    if hasattr(x, "date"):
        return x.date()
    return x


def open_decision_journal(cfg: dict, legacy_con=None) -> DecisionJournal:
    journal = DecisionJournal(resolve_path(cfg, "decision_journal"))
    if legacy_con is not None:
        journal.migrate_from_duckdb(legacy_con)
    return journal


def render_review(rows: list[dict], window_days: int, source: str | None = None) -> str:
    if not rows:
        return f"近{window_days}天暂无已结算决策信号 outcome。"
    title = f"决策信号复盘汇总 · 近{window_days}天"
    if source:
        title += f" · source={source}"
    headers = ("分组", "样本数", "hit率%", "neutral占比%", "平均前向收益%", "平均超额%")
    lines = [title, " | ".join(headers)]
    for row in rows:
        values = (
            row["group"],
            str(row["sample_count"]),
            _format_pct(row["hit_rate_pct"]),
            _format_pct(row["neutral_rate_pct"]),
            _format_pct(row["avg_fwd_return_pct"]),
            _format_pct(row["avg_excess_pct"]),
        )
        lines.append(" | ".join(values))
    return "\n".join(lines)


def _format_pct(value) -> str:
    return "" if value is None else f"{float(value):.2f}"
