"""资金流向入库 (Phase 4). 主源 Tushare(稳定, 全市场一次调用), 东财兜底.

- 大盘资金流: tushare moneyflow_mkt_dc(元) → market_fund_flow; 失败回退 ak.stock_market_fund_flow。
- 个股资金流: tushare moneyflow_dc(万元, ×1e4→元, 全市场按日拉后过滤持仓/自选) →
  stock_fund_flow; 失败回退东财逐只 ak.stock_individual_fund_flow。
"主力"=超大单+大单, 东财按单笔金额分档推断(两源同底, 实测数值一致)。
"""
from __future__ import annotations
import logging
import time
from datetime import date, timedelta

import pandas as pd

from ..sources import AkSource
from ..sources.tushare_src import get_pro
from ..storage import upsert

log = logging.getLogger(__name__)


def _ts_call(fn, *, retries: int = 3, sleep: float = 1.0, **kw):
    last = None
    for i in range(1, retries + 1):
        try:
            return fn(**kw)
        except Exception as e:  # noqa: BLE001
            last = e
            if i < retries:
                time.sleep(sleep * i)
    raise RuntimeError(f"tushare {getattr(fn, '__name__', fn)} 失败") from last


def _recent_trade_dates(con, n: int = 10) -> list[str]:
    """DB calendar 中截至今天最近 n 个交易日, 'YYYYMMDD' 降序。"""
    df = con.execute(
        "SELECT trade_date FROM calendar WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT ?",
        [date.today(), int(n)]).df()
    return [pd.Timestamp(d).strftime("%Y%m%d") for d in df["trade_date"]]


# ---- 大盘资金流 --------------------------------------------------------
_MKT_RENAME = {
    "close_sh": "sh_close", "pct_change_sh": "sh_pct",
    "close_sz": "sz_close", "pct_change_sz": "sz_pct",
    "net_amount": "main_net", "net_amount_rate": "main_ratio",
    "buy_elg_amount": "xl_net", "buy_lg_amount": "l_net",
    "buy_md_amount": "m_net", "buy_sm_amount": "s_net",
}
_MKT_COLS = ["trade_date", "sh_close", "sh_pct", "sz_close", "sz_pct",
             "main_net", "main_ratio", "xl_net", "l_net", "m_net", "s_net"]


def _ts_market_flow(con) -> int:
    pro = get_pro()
    end = date.today()
    start = end - timedelta(days=200)          # 一次调用取近半年历史
    df = _ts_call(pro.moneyflow_mkt_dc,
                  start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d"))
    if df is None or df.empty:
        raise RuntimeError("tushare moneyflow_mkt_dc 返回空数据")
    df = df.rename(columns=_MKT_RENAME).copy()   # 金额单位=元, 不缩放
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    for c in _MKT_COLS:
        if c not in df.columns:
            df[c] = pd.NA
    return upsert(con, "market_fund_flow", df[_MKT_COLS], keys=["trade_date"])


def ingest_market_fund_flow(con, src: AkSource) -> int:
    """大盘资金流 → market_fund_flow。tushare 主, 东财兜底。"""
    try:
        n = _ts_market_flow(con)
        log.info("market_fund_flow(tushare): %d 行", n)
        return n
    except Exception as e:  # noqa: BLE001
        log.warning("大盘资金流 tushare 失败, 回退东财: %s", e)
        df = src.market_fund_flow()
        n = upsert(con, "market_fund_flow", df, keys=["trade_date"])
        log.info("market_fund_flow(东财兜底): %d 行", n)
        return n


# ---- 个股资金流 --------------------------------------------------------
_DC_RENAME = {
    "pct_change": "pct_chg", "net_amount": "main_net", "net_amount_rate": "main_ratio",
    "buy_elg_amount": "xl_net", "buy_lg_amount": "l_net",
    "buy_md_amount": "m_net", "buy_sm_amount": "s_net",
}
_DC_COLS = ["symbol", "trade_date", "close", "pct_chg", "main_net",
            "main_ratio", "xl_net", "l_net", "m_net", "s_net"]
_DC_AMT = ["main_net", "xl_net", "l_net", "m_net", "s_net"]   # 万元 → 元


def _ts_stock_flow(con, symbols: list[str], days: int = 10) -> int:
    pro = get_pro()
    syms = {str(s).zfill(6) for s in symbols}
    total = 0
    dates = _recent_trade_dates(con, days)
    if not dates:
        raise RuntimeError("本地交易日历为空，无法按日拉取 tushare 个股资金流")
    for d in dates:
        df = _ts_call(pro.moneyflow_dc, trade_date=d)     # 全市场一次调用
        if df is None or df.empty:
            continue
        df = df.copy()
        df["symbol"] = df["ts_code"].astype(str).str.split(".").str[0]
        df = df[df["symbol"].isin(syms)]
        if df.empty:
            continue
        df = df.rename(columns=_DC_RENAME)
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        for c in _DC_AMT:
            df[c] = pd.to_numeric(df[c], errors="coerce") * 1e4   # 万元→元
        for c in _DC_COLS:
            if c not in df.columns:
                df[c] = pd.NA
        total += upsert(con, "stock_fund_flow", df[_DC_COLS], keys=["symbol", "trade_date"])
    if total == 0:
        raise RuntimeError("tushare moneyflow_dc 未返回关注标的，回退逐只拉取")
    return total


def ingest_stock_fund_flow(con, src: AkSource, symbols: list[str]) -> int:
    """持仓/自选个股资金流 → stock_fund_flow。tushare 主(全市场按日拉后过滤),
    东财兜底(逐只)。单只/单日失败不阻断其余。"""
    if not symbols:
        return 0
    try:
        n = _ts_stock_flow(con, symbols)
        log.info("stock_fund_flow(tushare): %d 只标的, %d 行", len(symbols), n)
        return n
    except Exception as e:  # noqa: BLE001
        log.warning("个股资金流 tushare 失败, 回退东财逐只: %s", e)
        total = 0
        for sym in symbols:
            try:
                total += upsert(con, "stock_fund_flow", src.stock_fund_flow(sym),
                                keys=["symbol", "trade_date"])
            except Exception as e2:  # noqa: BLE001
                log.warning("stock_fund_flow %s 失败: %s", sym, e2)
        log.info("stock_fund_flow(东财兜底): %d 行", total)
        return total


# ---- loaders (不分源) --------------------------------------------------
def market_fund_flow_hist(con, days: int = 20) -> pd.DataFrame:
    """近 days 天大盘资金流(降序), 供报告/趋势。"""
    return con.execute(
        "SELECT trade_date, sh_pct, sz_pct, main_net, main_ratio, xl_net, "
        "s_net FROM market_fund_flow ORDER BY trade_date DESC LIMIT ?",
        [int(days)]).df()


def load_stock_flow_for(con, symbols: list[str], days: int = 5) -> pd.DataFrame:
    """给定个股各自最近 days 条主力净流入(供持仓交叉验证)。

    按 symbol 各取最近 N 条(而非全表 max 日回看), 这样某只股票抓取滞后时
    仍返回其已有的最新数据, 不会因全局最新日更晚而被误判为"无数据"。
    """
    if not symbols:
        return pd.DataFrame()
    syms = [str(s).zfill(6) for s in symbols]
    ph = ",".join(["?"] * len(syms))
    return con.execute(
        f"""SELECT symbol, trade_date, pct_chg, main_net, main_ratio FROM (
                SELECT symbol, trade_date, pct_chg, main_net, main_ratio,
                       row_number() OVER (PARTITION BY symbol ORDER BY trade_date DESC) AS rn
                FROM stock_fund_flow WHERE symbol IN ({ph})
            ) WHERE rn <= {int(days)}
            ORDER BY symbol, trade_date DESC""", syms).df()
