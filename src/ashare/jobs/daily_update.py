from __future__ import annotations
import logging
from datetime import date, timedelta

from ..sources import AkSource
from ..ingest.calendar import ingest_calendar
from ..ingest.instruments import ingest_instruments
from ..ingest.bars import ingest_stock_bar, ingest_etf_bar, ingest_index_bar
from ..ingest.northbound import ingest_northbound, ingest_northbound_ths

log = logging.getLogger(__name__)


def _ymd(d: date) -> str:
    return d.strftime("%Y%m%d")


def daily_update(con, src: AkSource, indices=(), lookback_days: int = 5,
                 universe: str = "db") -> dict:
    """Run after market close. Pull last `lookback_days` to be safe against
    holidays / late corrections. Idempotent via upsert.

    universe:
      'db'  — only refresh symbols already in daily_bar (fast: minutes for a
              watchlist-sized DB, ~hours for a fully-backfilled universe)
      'all' — refresh every active stock + ETF in `instruments` (slow: ~17min
              per 7000 symbols at 0.15s rate-limit). Use for daily full-market
              sync after Phase 1.7 backfill-all.
    """
    end = date.today()
    start = end - timedelta(days=lookback_days)
    start_s, end_s = _ymd(start), _ymd(end)
    log.info("daily_update window: %s -> %s (universe=%s)", start_s, end_s, universe)

    # Metadata refreshes are best-effort: a flaky meta endpoint must NOT abort
    # the price ingestion that follows (bars are the priority).
    for label, fn in [
        ("calendar", lambda: ingest_calendar(con, src)),
        ("instruments", lambda: ingest_instruments(con, src)),
        ("northbound", lambda: ingest_northbound(con, src)),   # eastmoney history (frozen 2024-08)
        ("northbound_ths", lambda: ingest_northbound_ths(con)),  # THS accumulate forward
    ]:
        try:
            fn()
        except Exception as e:
            log.warning("daily meta step '%s' failed (non-fatal): %s", label, e)

    counts = {"stock": 0, "etf": 0, "index": 0, "failed": []}

    if universe == "db":
        # Only symbols already known to daily_bar
        rows = con.execute(
            "SELECT DISTINCT symbol, type FROM daily_bar"
        ).df()
        stocks = rows[rows["type"] == "stock"]["symbol"].tolist()
        etfs = rows[rows["type"] == "etf"]["symbol"].tolist()
        log.info("daily_update universe=db: %d stocks, %d etfs", len(stocks), len(etfs))
    elif universe == "all":
        stocks = con.execute(
            "SELECT symbol FROM instruments WHERE type='stock' AND delist_date IS NULL"
        ).df()["symbol"].tolist()
        etfs = con.execute(
            "SELECT symbol FROM instruments WHERE type='etf' AND delist_date IS NULL"
        ).df()["symbol"].tolist()
        log.info("daily_update universe=all: %d stocks, %d etfs", len(stocks), len(etfs))
    else:
        raise ValueError(f"unknown universe: {universe!r} (expected 'db' or 'all')")

    attempted = {"stock": len(stocks), "etf": len(etfs), "index": len(indices)}
    fail_by_type = {"stock": 0, "etf": 0, "index": 0}

    # ───── Tushare 兜底(首选): 全市场逐日, 1 调用/天, 稳定快; akshare 仅作回退 ─────
    # 治本东财/新浪收盘后空转失败。trade_dates 取自 calendar(只含真实交易日)。
    trade_dates = [d[0].strftime("%Y%m%d") for d in con.execute(
        "SELECT trade_date FROM calendar WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date",
        [start, end]).fetchall()]
    ts_stock_ok, ts_idx_covered, ts_etf_covered = False, set(), set()
    if trade_dates:
        from ..ingest.tushare_bars import (ingest_stock_bars_bulk,
                                           ingest_index_bars_ts, ingest_etf_bars_ts)
        try:
            n, latest = ingest_stock_bars_bulk(con, set(stocks), trade_dates)
            counts["stock"] += n
            ts_stock_ok = latest is not None and latest == trade_dates[-1]
            log.info("tushare 个股兜底: %d 行, 覆盖至 %s, ok=%s", n, latest, ts_stock_ok)
        except Exception as e:
            log.warning("tushare 个股兜底失败, 回退 akshare: %s", e)
        try:
            n_idx, ts_idx_covered = ingest_index_bars_ts(con, indices, start_s, end_s)
            counts["index"] += n_idx
            log.info("tushare 指数兜底: %d 行, 覆盖 %s", n_idx, ts_idx_covered)
        except Exception as e:
            log.warning("tushare 指数兜底失败, 回退 akshare: %s", e)
        if etfs:
            try:
                n_etf, ts_etf_covered = ingest_etf_bars_ts(con, etfs, start_s, end_s)
                counts["etf"] += n_etf
                log.info("tushare ETF兜底: %d 行, 覆盖 %d 只", n_etf, len(ts_etf_covered))
            except Exception as e:
                log.warning("tushare ETF兜底失败, 回退 akshare: %s", e)

    # ───── 估值/财务增量(让价值版/回测长期新鲜; 接续一次性 backfill) ─────
    if trade_dates:
        try:
            from ..ingest.tushare_valuation import refresh_valuation_ts
            counts["valuation"] = refresh_valuation_ts(con, trade_dates)
            log.info("tushare 估值增量: %d 行 (PE/PB/市值)", counts["valuation"])
        except Exception as e:
            log.warning("tushare 估值增量失败(非致命): %s", e)
        try:
            from ..ingest.tushare_fundamentals import refresh_fundamentals_ts
            fr = refresh_fundamentals_ts(con)
            counts["fundamentals_ts"] = sum(v for v in fr.values() if v and v > 0)
            log.info("tushare 财务增量: %s", fr)
        except Exception as e:
            log.warning("tushare 财务增量失败(非致命): %s", e)

    # 个股: Tushare 已覆盖最新交易日 → 跳过 akshare 逐只 grind; 否则回退
    if not ts_stock_ok:
        for s in stocks:
            try:
                counts["stock"] += ingest_stock_bar(con, src, s, start_s, end_s)
            except Exception as e:
                counts["failed"].append(("stock", s, str(e)[:100]))
                fail_by_type["stock"] += 1
    # ETF: 仅回退 Tushare 未覆盖的
    for s in etfs:
        if str(s).zfill(6) in ts_etf_covered:
            continue
        try:
            counts["etf"] += ingest_etf_bar(con, src, s, start_s, end_s)
        except Exception as e:
            counts["failed"].append(("etf", s, str(e)[:100]))
            fail_by_type["etf"] += 1
    # 指数: 仅回退 Tushare 未覆盖的
    for s in indices:
        if str(s).zfill(6) in ts_idx_covered:
            continue
        try:
            counts["index"] += ingest_index_bar(con, src, s, start_s, end_s)
        except Exception as e:
            counts["failed"].append(("index", s, str(e)[:100]))
            fail_by_type["index"] += 1

    # ───── End-of-run honest summary ─────
    summary_ok = {k: v for k, v in counts.items() if k != "failed"}
    log.info("daily_update done: %s", summary_ok)

    print("\n══════ daily_update 汇总 ══════")
    print(f"  窗口: {start_s} → {end_s}   universe={universe}")
    print(f"  尝试: stock={attempted['stock']}  etf={attempted['etf']}  index={attempted['index']}")
    print(f"  成功: stock={counts['stock']}  etf={counts['etf']}  index={counts['index']}  (单位: 行)")
    print(f"  失败: stock={fail_by_type['stock']}  etf={fail_by_type['etf']}  index={fail_by_type['index']}  (单位: 标的)")
    if "valuation" in counts or "fundamentals_ts" in counts:
        print(f"  增量: 估值={counts.get('valuation', 0)}行  财务={counts.get('fundamentals_ts', 0)}行")

    if counts["failed"]:
        print(f"\n  🔴 失败 {len(counts['failed'])} 个标的. 前 10 个示例:")
        for t, s, msg in counts["failed"][:10]:
            print(f"     [{t}] {s}  →  {msg}")
        if len(counts["failed"]) > 10:
            print(f"     ... 还有 {len(counts['failed']) - 10} 个 (用 query 命令查看完整失败列表)")

    # Loud signal: if every single bar attempt failed, this is a network/source
    # outage, not a no-op. Mark the run as failed.
    total_attempts = sum(attempted.values())
    total_success_rows = counts["stock"] + counts["etf"] + counts["index"]
    if total_attempts > 0 and total_success_rows == 0:
        print("\n  ❌❌❌ 0 行新数据落库. 视为本次 daily 运行失败. ❌❌❌")
        print("     可能原因: 网络/代理无法连接东财, 或上游接口变更.")
        counts["all_failed"] = True

    return counts
