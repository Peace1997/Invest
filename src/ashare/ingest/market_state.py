"""Ingest market state snapshots for sentiment confirmation."""
from __future__ import annotations

import pandas as pd

from ..sources.market_state import fetch_cls_market_breadth
from ..storage import upsert

_MARKET_BREADTH_COLS = [
    "snap_time",
    "source",
    "up_count",
    "down_count",
    "flat_count",
    "suspend_count",
    "avg_rise",
    "up_ratio",
    "up_down_ratio",
    "limit_up",
    "limit_down",
    "limit_ratio",
    "sentiment_desc",
    "raw_json",
]


def ingest_market_breadth(con, timeout: float = 10.0) -> int:
    df = fetch_cls_market_breadth(timeout=timeout)
    return upsert(con, "market_breadth_snapshot", df[_MARKET_BREADTH_COLS], ["snap_time", "source"])


def latest_market_breadth(con) -> dict | None:
    df = con.execute(
        "SELECT * FROM market_breadth_snapshot ORDER BY snap_time DESC LIMIT 1"
    ).df()
    if df is None or df.empty:
        return None
    row = df.iloc[0]
    out = row.to_dict()
    for key, value in list(out.items()):
        if pd.isna(value):
            out[key] = None
    return out
