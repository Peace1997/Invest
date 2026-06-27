"""Ingest valuation (PE/PB) into `valuation_daily` (ROADMAP Phase 1.3).

  - 个股        : 百度股市通 (AkSource.stock_valuation_hist) → 存于个股代码
  - 指数温度计   : 乐咕乐股 (index_valuation_hist) → 存于指数代码, e.g. '000300'
  - 宽基指数基金 : 按其跟踪指数的估值, 存于**基金代码**, 这样健康分按持仓代码直接读

主题/债券基金无可比 PE → 不入库 (诚实留空, 健康分不出估值因子)。
`refresh_valuation` 幂等: 已是最新交易日的就跳过, 不重复打网络。
"""
from __future__ import annotations
import json
import logging
from pathlib import Path

import pandas as pd

from ..storage import upsert
from ..sources import AkSource

log = logging.getLogger(__name__)

# 场外宽基指数基金 → 乐咕乐股指数中文名 (能干净映射的才纳入)
FUND_INDEX_NAME = {
    "005658": "沪深300",
    "005919": "中证500",
    "016631": "中证1000",
    "011609": "科创50",
}
# 指数代码 → 乐咕中文名 (温度计 + 校验)
INDEX_NAME = {
    "000300": "沪深300",
    "000905": "中证500",
    "000852": "中证1000",
    "000688": "科创50",
}
_VAL_COLS = ["symbol", "trade_date", "pe_ttm", "pb", "total_mv", "src"]


def _store(con, symbol: str, df: pd.DataFrame, src: str) -> int:
    if df is None or df.empty:
        return 0
    df = df.copy()
    df["symbol"] = symbol
    df["src"] = src
    if "total_mv" not in df.columns:
        df["total_mv"] = pd.NA
    return upsert(con, "valuation_daily", df[_VAL_COLS], ["symbol", "trade_date"])


def ingest_stock_valuation(con, ak: AkSource, code: str) -> int:
    return _store(con, str(code).zfill(6), ak.stock_valuation_hist(code), "baidu")


def ingest_index_valuation(con, ak: AkSource, index_name: str, store_as: str) -> int:
    return _store(con, store_as, ak.index_valuation_hist(index_name), "legulegu")


def _latest(con, symbol: str):
    r = con.execute("SELECT max(trade_date) FROM valuation_daily WHERE symbol=?",
                    [symbol]).fetchone()
    return r[0] if r else None


def _last_trading_day(con):
    r = con.execute(
        "SELECT max(trade_date) FROM calendar WHERE trade_date<=current_date"
    ).fetchone()
    return r[0] if r else None


def refresh_valuation(con, pf, index_symbol: str = "000300", force: bool = False) -> dict:
    """Top up valuation for the portfolio's stocks + mapped index funds + the
    thermometer index. Skips anything already at the latest trading day."""
    ak = AkSource()
    target = _last_trading_day(con)
    done, skipped, failed = [], [], []

    def fresh(sym):
        if force or target is None:
            return False
        lv = _latest(con, sym)
        return lv is not None and lv >= target

    # 1) stocks
    for h in pf.holdings:
        if h.type == "stock" and h.code.isdigit():
            if fresh(h.code):
                skipped.append(h.code); continue
            try:
                n = ingest_stock_valuation(con, ak, h.code)
                (done if n else failed).append(h.code)
            except Exception as e:
                log.warning("valuation stock %s failed: %s", h.code, e); failed.append(h.code)

    # 2) index-tracking funds → store under fund code
    for h in pf.holdings:
        name = FUND_INDEX_NAME.get(h.code)
        if name:
            if fresh(h.code):
                skipped.append(h.code); continue
            try:
                n = ingest_index_valuation(con, ak, name, store_as=h.code)
                (done if n else failed).append(h.code)
            except Exception as e:
                log.warning("valuation fund %s(%s) failed: %s", h.code, name, e); failed.append(h.code)

    # 3) thermometer index
    iname = INDEX_NAME.get(str(index_symbol).zfill(6))
    if iname:
        if fresh(str(index_symbol).zfill(6)):
            skipped.append(index_symbol)
        else:
            try:
                ingest_index_valuation(con, ak, iname, store_as=str(index_symbol).zfill(6))
                done.append(index_symbol)
            except Exception as e:
                log.warning("valuation index %s failed: %s", index_symbol, e); failed.append(index_symbol)

    return {"done": done, "skipped": skipped, "failed": failed}


def backfill_value_universe(con, force: bool = False, full: bool = False) -> dict:
    """Backfill PE/PB (baidu) so the 稳健·价值版 can rank from the warehouse.
    Idempotent: skips names already at latest date. eastmoney 全市场快照断供时这是
    可靠的逐只兜底。默认沪深300主板(~10-15min); full=True 全主板(~数小时, 慎用)。"""
    ak = AkSource()
    target = _last_trading_day(con)
    fname = "data/main_board.json" if full else "data/csi300_main.json"
    f = Path(fname)
    codes = [str(c).zfill(6) for c in json.loads(f.read_text())] if f.exists() else []
    done = skipped = failed = 0
    scope = "全主板" if full else "沪深300主板"
    print(f"⏳ 回填{scope}估值({len(codes)}只, baidu 逐只)...")
    for i, code in enumerate(codes, 1):
        if not force and target is not None:
            lv = _latest(con, code)
            if lv is not None and lv >= target:
                skipped += 1; continue
        try:
            n = ingest_stock_valuation(con, ak, code)
            done += 1 if n else 0
            failed += 0 if n else 1
        except Exception as e:
            log.warning("value-backfill %s failed: %s", code, e); failed += 1
        if i % 20 == 0:
            print(f"  ...{i}/{len(codes)}  成功{done} 跳过{skipped} 失败{failed}")
    print(f"✅ 估值回填完成: 成功{done} 跳过{skipped} 失败{failed} / 共{len(codes)}")
    return {"done": done, "skipped": skipped, "failed": failed}
