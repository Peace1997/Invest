from __future__ import annotations
import logging
import pandas as pd
from ..sources import AkSource
from ..storage import upsert

log = logging.getLogger(__name__)


def ingest_instruments(con, src: AkSource) -> int:
    """Pull current stock + ETF universe, mark ST. Indices are tracked separately
    (only the ones the user configures) so we don't bloat instruments with thousands
    of indices we'll never query."""
    stocks = src.stock_list()
    etfs = src.etf_list()
    st = src.st_symbols()
    df = pd.concat([stocks, etfs], ignore_index=True)
    df["is_st"] = df["symbol"].isin(st)
    # list_date / delist_date / sw_l1 left NULL for Phase 0 (separate fundamentals job)
    df["list_date"] = pd.NaT
    df["delist_date"] = pd.NaT
    df["sw_l1"] = None
    df = df[["symbol", "name", "type", "exchange", "list_date",
             "delist_date", "is_st", "sw_l1"]]
    n = upsert(con, "instruments", df, keys=["symbol"])
    log.info("instruments: upserted %d rows (stocks=%d, etfs=%d, st=%d)",
             n, len(stocks), len(etfs), len(st))
    return n


