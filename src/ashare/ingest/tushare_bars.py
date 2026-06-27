"""每日行情入库 · Tushare 兜底(首选)。

东财/新浪经常在收盘后返回空/超时(逐只 grind 几千次), Tushare 一次全市场调用就拿完:
  - 个股: daily(全市场/天) + daily_basic(换手) → daily_bar
  - 指数: index_daily(逐指数/窗口) → index_bar  (顺带治好 000300 长期补不上的坑)
ETF 仍走 akshare(标的少、风险低)。代理偶发超时 → 所有调用带重试。

口径对齐 daily_bar(不复权): volume=手(同akshare); amount 千元→元; amplitude=(high-low)/pre_close。
Tushare daily 是 EOD 定稿值, 不会返回盘中临时 bar, 故无需 provisional 守卫。
"""
from __future__ import annotations
import logging
import time

import pandas as pd

from ..storage import upsert
from ..sources.tushare_src import get_pro

log = logging.getLogger(__name__)

_BAR_COLS = ["symbol", "trade_date", "type", "open", "high", "low", "close",
             "volume", "amount", "amplitude", "pct_chg", "change", "turnover"]
_IDX_COLS = ["symbol", "trade_date", "open", "high", "low", "close",
             "volume", "amount", "amplitude", "pct_chg", "change", "turnover"]


def _call(fn, *, retries: int = 4, sleep: float = 1.5, **kw):
    last = None
    for i in range(retries):
        try:
            return fn(**kw)
        except Exception as e:  # noqa: BLE001 - 代理网络异常都重试
            last = e
            if i < retries - 1:
                time.sleep(sleep)
    raise last


def _map(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["symbol"] = df["ts_code"].str[:6]
    df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d").dt.date
    df["volume"] = df["vol"]
    df["amount"] = df["amount"] * 1000.0                      # 千元 → 元
    df["amplitude"] = (df["high"] - df["low"]) / df["pre_close"] * 100.0
    return df


def ingest_stock_bars_bulk(con, symbols: set[str], trade_dates: list[str]) -> tuple[int, str | None]:
    """全市场逐日拉取, 过滤到 `symbols`, 落 daily_bar。返回 (行数, 已覆盖的最新交易日)。"""
    pro = get_pro()
    total, latest = 0, None
    for d in trade_dates:
        df = _call(pro.daily, trade_date=d)
        if df is None or df.empty:
            continue
        tn = _call(pro.daily_basic, trade_date=d, fields="ts_code,turnover_rate")
        df = df.merge(tn, on="ts_code", how="left") if tn is not None else df.assign(turnover_rate=pd.NA)
        df = _map(df)
        df = df[df["symbol"].isin(symbols)]
        if df.empty:
            continue
        df["type"] = "stock"
        df["turnover"] = df["turnover_rate"]
        total += upsert(con, "daily_bar", df[_BAR_COLS], ["symbol", "trade_date"])
        latest = d if latest is None or d > latest else latest
    return total, latest


def ingest_adj_factor_bulk(con, symbols: set[str], trade_dates: list[str]) -> tuple[int, str | None]:
    """全市场逐日拉复权因子(adj_factor), 过滤到 `symbols`, 落 adj_factor。
    返回 (行数, 已覆盖的最新交易日)。后复权价 = daily_bar.close × adj_factor。"""
    pro = get_pro()
    total, latest = 0, None
    for d in trade_dates:
        df = _call(pro.adj_factor, trade_date=d)
        if df is None or df.empty:
            continue
        df = df.copy()
        df["symbol"] = df["ts_code"].str[:6]
        df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d").dt.date
        df = df[df["symbol"].isin(symbols)]
        if df.empty:
            continue
        total += upsert(con, "adj_factor",
                        df[["symbol", "trade_date", "adj_factor"]],
                        ["symbol", "trade_date"])
        latest = d if latest is None or d > latest else latest
    return total, latest


def ingest_etf_bars_ts(con, etfs, start: str, end: str) -> tuple[int, set[str]]:
    """逐 ETF 拉窗口(fund_daily), 落 daily_bar(type='etf')。返回 (行数, 已覆盖代码集)。
    场内ETF代码→交易所后缀: 5x→.SH, 1x→.SZ。turnover fund_daily 无 → 留空。"""
    pro = get_pro()
    total, covered = 0, set()
    for code in etfs:
        code = str(code).zfill(6)
        ts = code + (".SH" if code.startswith("5") else ".SZ")
        try:
            df = _call(pro.fund_daily, ts_code=ts, start_date=start, end_date=end)
        except Exception as e:  # noqa: BLE001
            log.warning("tushare fund_daily %s 失败: %s", ts, e)
            continue
        if df is None or df.empty:
            continue
        df = _map(df)
        df["type"] = "etf"
        df["turnover"] = pd.NA
        total += upsert(con, "daily_bar", df[_BAR_COLS], ["symbol", "trade_date"])
        covered.add(code)
    return total, covered


def ingest_index_bars_ts(con, indices, start: str, end: str) -> tuple[int, set[str]]:
    """逐指数拉窗口, 落 index_bar。返回 (行数, 已覆盖的指数代码集)。"""
    pro = get_pro()
    total, covered = 0, set()
    for code in indices:
        code = str(code).zfill(6)
        ts = code + (".SZ" if code.startswith("39") else ".SH")
        try:
            df = _call(pro.index_daily, ts_code=ts, start_date=start, end_date=end)
        except Exception as e:  # noqa: BLE001
            log.warning("tushare index_daily %s 失败: %s", ts, e)
            continue
        if df is None or df.empty:
            continue
        df = _map(df)
        df["turnover"] = pd.NA
        total += upsert(con, "index_bar", df[_IDX_COLS], ["symbol", "trade_date"])
        covered.add(code)
    return total, covered
