"""14:30 low-valuation growth watchlist with exchange-timestamped prices."""
from __future__ import annotations
from datetime import date, datetime

import pandas as pd

from ..notify.push import send_push
from .value_growth import attach_live_quotes, decision_items, push_body, screen_value_growth


def _candidates(con, top: int = 5) -> pd.DataFrame:
    return attach_live_quotes(screen_value_growth(con, limit=top))


def run_endday(con, cfg: dict | None = None, force: bool = False) -> dict:
    cfg = cfg or {}
    ed = cfg.get("endday", {})
    if not ed.get("enabled", True):
        return {"skipped": "disabled"}
    today = date.today()
    if not force and not con.execute(
            "SELECT 1 FROM calendar WHERE trade_date=?", [today]).fetchone():
        return {"skipped": "non-trading-day"}

    n_pick = int(ed.get("picks", 5))
    cands = _candidates(con, top=n_pick)
    if cands is None or cands.empty:
        return {"skipped": "no-current-quotes"}

    title = f"🔚 低估值成长尾盘观察 {len(cands)}只 · {datetime.now():%H:%M}"
    body = push_body(cands, snapshot_label="14:30 参考价")
    ok = send_push(title, body, cfg.get("alerts", {}))
    items = decision_items(cands, as_of=today)
    return {"picks": len(items), "pushed": ok,
            "names": [p.get("name") for p in items],
            "items": items}
