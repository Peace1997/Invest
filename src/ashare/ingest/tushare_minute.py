"""分钟行情入库 · Tushare stk_mins (默认 5min, 仅个股)。

口径: volume=手(tushare vol 股 ÷100, 对齐 daily_bar); amount=元(stk_mins 原始即元, 不缩放;
注意 daily 的 amount 是千元, 二者来源单位不同)。
trade_time = bar 结束时间。单次调用上限 8000 行(超出会截断早段) → 按 freq 自动分窗。
ETF/指数 stk_mins 返回空, 故仅个股; 短线池/自选/持仓个股都适用。
"""
from __future__ import annotations
import logging
import time
from datetime import datetime, timedelta

import pandas as pd

from ..storage import upsert
from ..sources.tushare_src import get_pro

log = logging.getLogger(__name__)

DEFAULT_FREQ = "5min"
_BARS_PER_DAY = {"1min": 240, "5min": 48, "15min": 16, "30min": 8, "60min": 4}
_MIN_COLS = ["symbol", "freq", "trade_time", "type",
             "open", "high", "low", "close", "volume", "amount"]


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


def _ts_code(con, code: str) -> str:
    """6位代码 → tushare ts_code。优先用 instruments.exchange, 否则按前缀兜底。"""
    code = str(code).zfill(6)
    ex = None
    try:
        r = con.execute("SELECT exchange FROM instruments WHERE symbol=?", [code]).fetchone()
        ex = r[0] if r else None
    except Exception:  # noqa: BLE001
        ex = None
    if ex in ("SH", "SZ", "BJ"):
        return f"{code}.{ex}"
    if code.startswith(("60", "68", "90")):
        return f"{code}.SH"
    if code.startswith(("43", "83", "87", "88", "92")):
        return f"{code}.BJ"
    return f"{code}.SZ"


def _chunk_days(freq: str) -> int:
    """每窗口跨度(日历日), 使单窗行数 ≈ days×0.69×bars_per_day 留在 8000 上限内(取 6500 余量)。"""
    bpd = _BARS_PER_DAY.get(freq, 48)
    return max(5, int(6500 / (0.69 * bpd)))


def _to_date(s: str):
    return datetime.strptime(str(s).replace("-", ""), "%Y%m%d").date()


def ingest_minute_bars(con, symbols, start: str, end: str, freq: str = DEFAULT_FREQ,
                       type_: str = "stock", sleep: float = 0.2) -> dict:
    """逐只个股按窗口分块拉 stk_mins → minute_bar(幂等)。

    start/end 接受 'YYYYMMDD' 或 'YYYY-MM-DD'。返回 {freq, symbols, rows, failed}。
    """
    pro = get_pro()
    d0, d1 = _to_date(start), _to_date(end)
    step = timedelta(days=_chunk_days(freq))
    out = {"freq": freq, "symbols": 0, "rows": 0, "failed": []}
    seq = [str(c).zfill(6) for c in symbols]

    for i, code in enumerate(seq, 1):
        ts = _ts_code(con, code)
        n_sym = 0
        try:
            a = d0
            while a <= d1:
                b = min(a + step, d1)
                df = _call(pro.stk_mins, ts_code=ts, freq=freq,
                           start_date=a.strftime("%Y-%m-%d") + " 09:00:00",
                           end_date=b.strftime("%Y-%m-%d") + " 15:30:00")
                if df is not None and not df.empty:
                    df = df.copy()
                    df["symbol"] = code
                    df["freq"] = freq
                    df["type"] = type_
                    df["trade_time"] = pd.to_datetime(df["trade_time"])
                    df["volume"] = df["vol"] / 100.0   # 股 → 手 (对齐 daily_bar)
                    # 注: stk_mins 的 amount 单位已是「元」(与 daily 的「千元」不同), 不缩放
                    n_sym += upsert(con, "minute_bar", df[_MIN_COLS],
                                    ["symbol", "freq", "trade_time"])
                a = b + timedelta(days=1)
                time.sleep(sleep)
            out["rows"] += n_sym
            out["symbols"] += 1
            log.info("[min %d/%d] %s %s: +%d 行", i, len(seq), code, freq, n_sym)
        except Exception as e:  # noqa: BLE001 - 单只失败不阻断其余
            log.error("[min] %s %s 失败: %s", code, freq, e)
            out["failed"].append((code, str(e)[:100]))
    return out
