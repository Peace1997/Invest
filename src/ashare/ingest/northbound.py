from __future__ import annotations
import logging
from datetime import datetime

import pandas as pd

from ..sources import AkSource, HexinSource
from ..storage import upsert

log = logging.getLogger(__name__)

CHANNELS = ["北向资金", "沪股通", "深股通"]

# Columns that should have meaningful values (used to detect "empty" rows
# that upstream returns post-2024-08 freeze).
_VALUE_COLS = ["net_buy", "buy_amount", "sell_amount",
               "cum_net_buy", "capital_inflow", "balance", "holding_value"]


def ingest_northbound(con, src: AkSource) -> int:
    """Pull historical northbound flow.

    Upstream caveat: HK/SH/SZ exchanges stopped publishing real-time northbound
    flow detail on 2024-08-18. AKShare keeps returning rows for new dates but
    every numeric column is NaN. Without filtering, those NaN rows would
    overwrite the valuable 2014-2024 history via DELETE+INSERT upsert.

    Strategy: drop rows where every value column is NaN before upserting.
    Net effect: historical data preserved; nothing new lands after 2024-08.
    """
    total = 0
    for ch in CHANNELS:
        try:
            df = src.northbound_hist(ch)
            before = len(df)
            # Upstream uses holding_value=0 as a placeholder when flow data
            # is frozen; treat zero as missing for the all-NaN detection.
            check = df[_VALUE_COLS].copy()
            check["holding_value"] = check["holding_value"].where(
                check["holding_value"] != 0
            )
            mask_any_value = check.notna().any(axis=1)
            df = df[mask_any_value].copy()
            dropped = before - len(df)
            if dropped:
                log.info("northbound %s: dropped %d all-NaN rows (upstream freeze)", ch, dropped)
            n = upsert(con, "northbound", df, keys=["trade_date", "channel"])
            log.info("northbound %s: %d rows upserted", ch, n)
            total += n
        except Exception as e:
            log.warning("northbound %s failed: %s", ch, e)
    return total


def ingest_northbound_ths(con, hexin: HexinSource | None = None,
                          now: datetime | None = None) -> int:
    """Capture *today's* closing northbound net buy from THS (同花顺) and
    accumulate it forward. Fills the gap left by eastmoney's 2024-08 freeze —
    but only from the day you start running this; it cannot backfill the past.

    Idempotent: re-running on the same day overwrites that day's snapshot.
    Returns rows upserted (0 or 3: 北向/沪股通/深股通).
    """
    hexin = hexin or HexinSource()
    snap = hexin.northbound_close(now=now)
    if snap is None:
        log.info("northbound THS: no settled value available today (provisional or fetch failed)")
        return 0

    rows = [
        {"trade_date": snap["trade_date"], "channel": "北向",   "net_buy": snap["north"]},
        {"trade_date": snap["trade_date"], "channel": "沪股通", "net_buy": snap["hgt"]},
        {"trade_date": snap["trade_date"], "channel": "深股通", "net_buy": snap["sgt"]},
    ]
    df = pd.DataFrame(rows)
    # Fill the rest of the schema columns as NULL — THS only gives net flow,
    # not gross buy/sell or holdings. Honest about what we have.
    for c in ["buy_amount", "sell_amount", "cum_net_buy",
              "capital_inflow", "balance", "holding_value"]:
        df[c] = pd.NA
    n = upsert(con, "northbound", df, keys=["trade_date", "channel"])
    log.info("northbound THS: upserted %d rows for %s (北向 %.2f亿)",
             n, snap["trade_date"], snap["north"])
    return n
