from __future__ import annotations
import logging
import time
from typing import Iterable

from ..sources import AkSource
from ..ingest.calendar import ingest_calendar
from ..ingest.instruments import ingest_instruments
from ..ingest.bars import ingest_stock_bar, ingest_etf_bar, ingest_index_bar
from ..ingest.northbound import ingest_northbound

log = logging.getLogger(__name__)


def backfill_meta(con, src: AkSource) -> None:
    ingest_calendar(con, src)
    ingest_instruments(con, src)


def backfill_bars(
    con,
    src: AkSource,
    stocks: Iterable[str] = (),
    etfs: Iterable[str] = (),
    indices: Iterable[str] = (),
    start: str = "20150101",
    end: str = "20500101",
    batch_size: int = 50,
    batch_sleep: float = 2.0,
) -> dict:
    counts = {"stock": 0, "etf": 0, "index": 0, "failed": []}

    def _run(kind: str, symbols, fn):
        seq = list(symbols)
        for i, s in enumerate(seq, 1):
            try:
                n = fn(con, src, s, start, end)
                counts[kind] += n
                log.info("[%s %d/%d] %s: +%d rows", kind, i, len(seq), s, n)
            except Exception as e:
                log.error("[%s] %s failed: %s", kind, s, e)
                counts["failed"].append((kind, s, str(e)))
            if i % batch_size == 0:
                time.sleep(batch_sleep)

    _run("stock", stocks, ingest_stock_bar)
    _run("etf",   etfs,   ingest_etf_bar)
    _run("index", indices, ingest_index_bar)
    return counts


def backfill_northbound(con, src: AkSource) -> int:
    return ingest_northbound(con, src)
