from __future__ import annotations

from datetime import date

import pandas as pd


def enrich_selected_items(
    picks: list[dict],
    candidates: pd.DataFrame,
    *,
    as_of: date,
    limit: int,
) -> list[dict]:
    """Attach the observable decision-time anchor to selected short-term items."""
    prices = {
        str(row.symbol).zfill(6): float(row.price)
        for row in candidates[["symbol", "price"]].itertuples(index=False)
        if pd.notna(row.price)
    }
    items = []
    for pick in picks[:limit]:
        item = dict(pick)
        code = str(item.get("code") or "").zfill(6)
        if code in prices:
            item["ref_price"] = prices[code]
            item["trade_date"] = as_of.isoformat()
        items.append(item)
    return items
