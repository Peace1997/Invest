from __future__ import annotations
import logging
from datetime import date, datetime, time as dtime

from ..sources import AkSource
from ..storage import upsert

log = logging.getLogger(__name__)

# A-share session ends 15:00; vendor daily bars settle shortly after. Use a
# buffer to be safe. Before this cutoff, "today" bars are intraday snapshots
# (provisional close/high/low/volume) and must NOT be frozen into the DB as
# if they were the official close.
_CLOSE_CUTOFF = dtime(15, 30)


def _drop_provisional_today(df, now: datetime | None = None):
    """Remove the current trading day's bar if the market hasn't closed yet.

    Returns (filtered_df, dropped_count). Keeps all historical bars untouched;
    only guards against storing an unsettled intraday bar for *today*.
    """
    if df is None or df.empty:
        return df, 0
    now = now or datetime.now()
    today = now.date()
    if now.time() >= _CLOSE_CUTOFF:
        return df, 0  # after close: today's bar is final, keep it
    mask_today = df["trade_date"].apply(
        lambda d: (d == today) or (hasattr(d, "date") and d.date() == today)
    )
    dropped = int(mask_today.sum())
    if dropped:
        return df[~mask_today].copy(), dropped
    return df, 0


def ingest_stock_bar(con, src: AkSource, symbol: str, start: str, end: str) -> int:
    df = src.stock_bar(symbol, start, end)
    if df.empty:
        return 0
    df, dropped = _drop_provisional_today(df)
    if dropped:
        log.debug("stock %s: skipped %d provisional intraday bar(s)", symbol, dropped)
    if df.empty:
        return 0
    df["type"] = "stock"
    df = df[["symbol", "trade_date", "type", "open", "high", "low", "close",
             "volume", "amount", "amplitude", "pct_chg", "change", "turnover"]]
    return upsert(con, "daily_bar", df, keys=["symbol", "trade_date"])


def ingest_etf_bar(con, src: AkSource, symbol: str, start: str, end: str) -> int:
    df = src.etf_bar(symbol, start, end)
    if df.empty:
        return 0
    df, dropped = _drop_provisional_today(df)
    if dropped:
        log.debug("etf %s: skipped %d provisional intraday bar(s)", symbol, dropped)
    if df.empty:
        return 0
    df["type"] = "etf"
    df = df[["symbol", "trade_date", "type", "open", "high", "low", "close",
             "volume", "amount", "amplitude", "pct_chg", "change", "turnover"]]
    return upsert(con, "daily_bar", df, keys=["symbol", "trade_date"])


def ingest_index_bar(con, src: AkSource, symbol: str, start: str, end: str) -> int:
    df = src.index_bar(symbol, start, end)
    if df.empty:
        return 0
    df, dropped = _drop_provisional_today(df)
    if dropped:
        log.debug("index %s: skipped %d provisional intraday bar(s)", symbol, dropped)
    if df.empty:
        return 0
    df = df[["symbol", "trade_date", "open", "high", "low", "close",
             "volume", "amount", "amplitude", "pct_chg", "change", "turnover"]]
    return upsert(con, "index_bar", df, keys=["symbol", "trade_date"])
