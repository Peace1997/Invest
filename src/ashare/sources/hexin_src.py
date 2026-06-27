"""Hexin (同花顺 / THS) data source — northbound capital flow.

Why this exists: eastmoney stopped publishing northbound (沪股通/深股通) net-buy
detail on 2024-08-18 (industry-wide upstream freeze). The THS hsgtApi endpoint
still serves today's intraday cumulative net-buy, so we capture the closing
value each trading day and accumulate fresh history forward from now.

Endpoint verified against a-stock-data (github.com/simonlin1212/a-stock-data),
Apache-2.0. There is NO free source that backfills the 2024-08 → present gap;
that gap stays honestly empty.

Semantics: the intraday chart's value is *cumulative within the day*, so the
last point (≈15:00) equals the full-day net buy in 亿元 (100M CNY).
"""
from __future__ import annotations
import logging
from datetime import date, datetime, time as dtime

import requests
import pandas as pd

from .akshare_src import _ensure_domestic_no_proxy

log = logging.getLogger(__name__)

_HSGT_URL = "https://data.hexin.cn/market/hsgtApi/method/dayChart/"
_HSGT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "Chrome/117.0.0.0 Safari/537.36"
    ),
    "Host": "data.hexin.cn",
    "Referer": "https://data.hexin.cn/",
}

# Mirror the bar ingest guard: before close the cumulative figure is still
# moving, so it is provisional.
_CLOSE_CUTOFF = dtime(15, 30)


class HexinSource:
    name = "hexin"

    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout
        _ensure_domestic_no_proxy()

    def northbound_intraday(self) -> pd.DataFrame:
        """Today's minute-level cumulative net buy.

        Returns DataFrame[time, hgt_yi, sgt_yi] (亿元). Empty on failure.
        """
        try:
            r = requests.get(_HSGT_URL, headers=_HSGT_HEADERS, timeout=self.timeout)
            r.raise_for_status()
            d = r.json()
        except Exception as e:
            log.warning("hexin hsgt fetch failed: %s", e)
            return pd.DataFrame()
        times = d.get("time", [])
        hgt = d.get("hgt", [])
        sgt = d.get("sgt", [])
        n = len(times)
        if n == 0:
            return pd.DataFrame()
        return pd.DataFrame({
            "time": times,
            "hgt_yi": (hgt + [None] * n)[:n],
            "sgt_yi": (sgt + [None] * n)[:n],
        })

    def northbound_close(self, now: datetime | None = None) -> dict | None:
        """Today's closing northbound net buy, or None if unavailable / provisional.

        Returns {trade_date, hgt, sgt, north} in 亿元, where:
          hgt   = 沪股通 当日净买额
          sgt   = 深股通 当日净买额
          north = 北向   当日净买额 (= hgt + sgt)

        Guard: returns None before 15:30 (intraday value not yet final).
        """
        now = now or datetime.now()
        if now.time() < _CLOSE_CUTOFF:
            log.info("hexin northbound: before close cutoff %s, value still provisional — skipping",
                     _CLOSE_CUTOFF)
            return None
        df = self.northbound_intraday()
        if df.empty:
            return None
        # Last non-null cumulative point = full-day net buy.
        hgt_series = df["hgt_yi"].dropna()
        sgt_series = df["sgt_yi"].dropna()
        if hgt_series.empty or sgt_series.empty:
            log.warning("hexin northbound: no non-null points")
            return None
        hgt = float(hgt_series.iloc[-1])
        sgt = float(sgt_series.iloc[-1])
        return {
            "trade_date": now.date(),
            "hgt": hgt,
            "sgt": sgt,
            "north": hgt + sgt,
        }
