from __future__ import annotations
import logging
from ..sources import AkSource
from ..storage import upsert

log = logging.getLogger(__name__)


def ingest_calendar(con, src: AkSource) -> int:
    df = src.calendar()
    n = upsert(con, "calendar", df, keys=["trade_date"])
    log.info("calendar: upserted %d rows", n)
    return n
