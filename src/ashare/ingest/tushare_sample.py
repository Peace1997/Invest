"""Sample-universe ingest over the Tushare-compatible HTTP client.

Covers the Phase 0 closed loop in config.yaml `sample_targets`:
  000300 index, 510300 ETF, 000001 stock.
Calendar, instrument metadata, and daily bars land in the existing tables.
AkShare is not imported. www.szse.cn is not called.

Bar units match `ingest/tushare_bars.py`: volume in 手, amount converted from
千元 to 元, amplitude = (high-low)/pre_close in percent.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time as dtime, timedelta

import pandas as pd

from ..storage import upsert

log = logging.getLogger(__name__)

_CLOSE_CUTOFF = dtime(15, 30)
_BAR_COLS = ["symbol", "trade_date", "type", "open", "high", "low", "close",
             "volume", "amount", "amplitude", "pct_chg", "change", "turnover"]
_IDX_COLS = ["symbol", "trade_date", "open", "high", "low", "close",
             "volume", "amount", "amplitude", "pct_chg", "change", "turnover"]
_INST_COLS = ["symbol", "name", "type", "exchange", "list_date",
              "delist_date", "is_st", "sw_l1"]

_DAILY_FIELDS = "ts_code,trade_date,open,high,low,close,pre_close,change,pct_chg,vol,amount"
_BASIC_FIELDS = "ts_code,trade_date,turnover_rate,pe_ttm,pb,total_mv"


def sample_universe(cfg: dict | None) -> list[tuple[str, str]]:
    """(symbol, kind) from sample_targets. Defaults are the Phase 0 trio."""
    t = (cfg or {}).get("sample_targets") or {}
    rows: list[tuple[str, str]] = []
    for code in t.get("indices") or ["000300"]:
        rows.append((str(code).zfill(6), "index"))
    for code in t.get("etfs") or ["510300"]:
        rows.append((str(code).zfill(6), "etf"))
    for code in t.get("stocks") or ["000001"]:
        rows.append((str(code).zfill(6), "stock"))
    return rows


def ts_code_for(symbol: str, kind: str) -> str:
    """6-digit code → Tushare ts_code, using Invest's sample semantics.

    Indices are not stocks: 000300 is Shanghai (.SH), while 399xxx is Shenzhen.
    A 000-prefix stock such as 000001 is Shenzhen.
    """
    code = str(symbol).zfill(6)
    if kind == "index":
        suffix = "SZ" if code.startswith("399") else "SH"
    elif kind == "etf":
        suffix = "SH" if code.startswith(("5", "6")) else "SZ"
    elif code.startswith(("6", "9")) or code[:2] in {"11", "13", "18"}:
        suffix = "SH"
    elif code.startswith(("4", "8")):
        suffix = "BJ"
    else:
        suffix = "SZ"
    return f"{code}.{suffix}"


def _windows(start: str, end: str, step_days: int = 2000):
    cur = datetime.strptime(start, "%Y%m%d").date()
    last = datetime.strptime(end, "%Y%m%d").date()
    while cur <= last:
        nxt = min(last, cur + timedelta(days=step_days))
        yield cur.strftime("%Y%m%d"), nxt.strftime("%Y%m%d")
        cur = nxt + timedelta(days=1)


def _as_date(value) -> date | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    text = str(value).strip()
    if not text or text.lower() in {"none", "nat", "nan", "null"}:
        return None
    if len(text) >= 8 and text[:8].isdigit() and (len(text) == 8 or text[8] in "-T "):
        if len(text) == 8:
            return datetime.strptime(text, "%Y%m%d").date()
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def _code_series(df: pd.DataFrame) -> pd.Series:
    if "ts_code" in df.columns:
        return df["ts_code"].astype(str)
    if "symbol" in df.columns:
        return df["symbol"].astype(str)
    raise ValueError("response has neither ts_code nor symbol")


def _symbol_of(raw: str) -> str:
    return str(raw).split(".")[0].zfill(6)


def _exchange_of_row(raw_code: str, kind: str, reported: str | None) -> str:
    label = (reported or "").strip().upper()
    if label in {"SH", "SSE"}:
        return "SH"
    if label in {"SZ", "SZSE"}:
        return "SZ"
    if label in {"BJ", "BSE"}:
        return "BJ"
    suffix = str(raw_code).split(".")[-1].upper() if "." in str(raw_code) else ""
    if suffix in {"SH", "SZ", "BJ"}:
        return suffix
    return ts_code_for(_symbol_of(raw_code), kind).split(".")[-1]


def _match(df: pd.DataFrame, ts_code: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    codes = _code_series(df)
    want = ts_code.upper()
    hit = df[codes.str.upper() == want]
    if not hit.empty:
        return hit
    short = _symbol_of(ts_code)
    return df[codes.map(_symbol_of) == short]


def _drop_provisional(df: pd.DataFrame, now: datetime) -> pd.DataFrame:
    if df is None or df.empty or now.time() >= _CLOSE_CUTOFF:
        return df
    today = now.date()
    keep = df["trade_date"].map(lambda d: d != today)
    return df.loc[keep].copy()


def bars_frame(df: pd.DataFrame, symbol: str, kind: str, turnover: pd.DataFrame | None = None) -> pd.DataFrame:
    """Normalize a daily/fund_daily/index_daily frame into warehouse columns."""
    empty_cols = _IDX_COLS if kind == "index" else _BAR_COLS
    if df is None or df.empty:
        return pd.DataFrame(columns=empty_cols)
    out = pd.DataFrame()
    out["symbol"] = _code_series(df).map(_symbol_of)
    out["trade_date"] = df["trade_date"].map(_as_date)
    for col in ("open", "high", "low", "close", "pre_close", "change", "pct_chg"):
        out[col] = pd.to_numeric(df[col], errors="coerce") if col in df.columns else pd.NA
    vol = pd.to_numeric(df["vol"], errors="coerce") if "vol" in df.columns else pd.Series(pd.NA, index=df.index)
    out["volume"] = vol.round()
    amount = pd.to_numeric(df["amount"], errors="coerce") if "amount" in df.columns else pd.Series(pd.NA, index=df.index)
    out["amount"] = amount * 1000.0
    pre = pd.to_numeric(out["pre_close"], errors="coerce")
    out["amplitude"] = (out["high"] - out["low"]) / pre * 100.0
    out.loc[pre.isna() | (pre == 0), "amplitude"] = pd.NA
    if "change" not in df.columns:
        out["change"] = out["close"] - pre
    out["turnover"] = pd.NA
    if turnover is not None and not turnover.empty and "turnover_rate" in turnover.columns:
        t = turnover.copy()
        t["symbol"] = _code_series(t).map(_symbol_of)
        t["trade_date"] = t["trade_date"].map(_as_date)
        t["turnover_rate"] = pd.to_numeric(t["turnover_rate"], errors="coerce")
        out = out.merge(t[["symbol", "trade_date", "turnover_rate"]], on=["symbol", "trade_date"], how="left")
        out["turnover"] = out["turnover_rate"]
        out = out.drop(columns=["turnover_rate"])
    out = out[out["symbol"] == str(symbol).zfill(6)]
    out = out.dropna(subset=["trade_date", "close"])
    out = out.drop_duplicates(["symbol", "trade_date"], keep="last")
    if kind != "index":
        out["type"] = kind
        return out[_BAR_COLS]
    return out[_IDX_COLS]


def valuation_frame(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """daily_basic → valuation_daily. total_mv 万元 → 亿元, src='tushare'."""
    cols = ["symbol", "trade_date", "pe_ttm", "pb", "total_mv", "src"]
    if df is None or df.empty:
        return pd.DataFrame(columns=cols)
    out = pd.DataFrame()
    out["symbol"] = _code_series(df).map(_symbol_of)
    out["trade_date"] = df["trade_date"].map(_as_date)
    for col in ("pe_ttm", "pb", "total_mv"):
        out[col] = pd.to_numeric(df[col], errors="coerce") if col in df.columns else pd.NA
    out["total_mv"] = out["total_mv"] / 1e4
    out["src"] = "tushare"
    out = out[out["symbol"] == str(symbol).zfill(6)].dropna(subset=["trade_date"])
    return out[cols]


def instrument_frame(df: pd.DataFrame, symbol: str, kind: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=_INST_COLS)
    row = _match(df, ts_code_for(symbol, kind))
    if row.empty:
        row = df.iloc[[0]]
    rec = row.iloc[0]
    raw = str(rec["ts_code"] if "ts_code" in row.columns else rec.get("symbol", symbol))
    name = rec["name"] if "name" in row.columns and pd.notna(rec["name"]) else None
    reported = None
    if "exchange" in row.columns and pd.notna(rec["exchange"]):
        reported = str(rec["exchange"])
    elif "market" in row.columns and pd.notna(rec["market"]):
        reported = str(rec["market"])
    text = "" if name is None else str(name)
    out = pd.DataFrame([{
        "symbol": str(symbol).zfill(6),
        "name": text or None,
        "type": kind,
        "exchange": _exchange_of_row(raw, kind, reported),
        "list_date": _as_date(rec["list_date"]) if "list_date" in row.columns else None,
        "delist_date": _as_date(rec["delist_date"]) if "delist_date" in row.columns else None,
        "is_st": "ST" in text.upper(),
        "sw_l1": None,
    }])
    out["list_date"] = pd.to_datetime(out["list_date"], errors="coerce")
    out["delist_date"] = pd.to_datetime(out["delist_date"], errors="coerce")
    return out[_INST_COLS]


def _call(query, api_name: str, params: dict, fields: str) -> tuple[pd.DataFrame | None, str | None]:
    try:
        df = query(api_name, params, fields)
    except Exception as e:  # noqa: BLE001 - one failed API does not invent rows
        return None, f"{api_name}: {e}"
    if df is None:
        return pd.DataFrame(), None
    return df, None


def ingest_sample(
    con,
    query,
    universe: list[tuple[str, str]],
    start: str,
    end: str,
    now: datetime | None = None,
) -> dict:
    """Pull calendar, metadata, and bars for `universe`.

    `query(api_name, params, fields) -> DataFrame` is `tushare_http.query`
    or a test double. Returns counts plus `failed` messages. Empty API
    results are failures; no bars are synthesized.
    """
    now = now or datetime.now()
    failed: list[str] = []
    counts = {"calendar": 0, "instruments": 0, "stock": 0, "etf": 0, "index": 0, "valuation": 0}

    cal, err = _call(query, "trade_cal", {
        "exchange": "SSE",
        "start_date": start,
        "end_date": end,
        "is_open": "1",
    }, "exchange,cal_date,is_open,pretrade_date")
    if err:
        failed.append(err)
    elif cal is None or cal.empty or "cal_date" not in cal.columns:
        failed.append("trade_cal: empty")
    else:
        dates = [_as_date(v) for v in cal["cal_date"]]
        if "is_open" in cal.columns:
            dates = [d for d, flag in zip(dates, cal["is_open"]) if str(flag) == "1"]
        frame = pd.DataFrame({"trade_date": [d for d in dates if d is not None]}).drop_duplicates()
        if frame.empty:
            failed.append("trade_cal: no open dates")
        else:
            counts["calendar"] = upsert(con, "calendar", frame, keys=["trade_date"])

    for symbol, kind in universe:
        code = ts_code_for(symbol, kind)
        if kind == "stock":
            meta_api, meta_params, meta_fields = "stock_basic", {"ts_code": code}, \
                "ts_code,symbol,name,exchange,market,list_date,delist_date"
            bar_api = "daily"
        elif kind == "etf":
            meta_api, meta_params, meta_fields = "fund_basic", {"ts_code": code}, \
                "ts_code,name,market,list_date,delist_date,status"
            bar_api = "fund_daily"
        else:
            meta_api, meta_params, meta_fields = "index_basic", {"ts_code": code}, \
                "ts_code,name,market,list_date"
            bar_api = "index_daily"

        meta, err = _call(query, meta_api, meta_params, meta_fields)
        if err:
            failed.append(f"{symbol} metadata {err}")
        elif meta is None or meta.empty:
            failed.append(f"{symbol} metadata {meta_api}: empty")
        else:
            inst = instrument_frame(meta, symbol, kind)
            if inst.empty:
                failed.append(f"{symbol} metadata: unmatched")
            else:
                counts["instruments"] += upsert(con, "instruments", inst, keys=["symbol"])

        parts: list[pd.DataFrame] = []
        basics: list[pd.DataFrame] = []
        bar_error = None
        for w0, w1 in _windows(start, end):
            params = {"ts_code": code, "start_date": w0, "end_date": w1}
            part, err = _call(query, bar_api, params, _DAILY_FIELDS)
            if err:
                bar_error = err
                break
            if part is not None and not part.empty:
                parts.append(part)
            if kind == "stock":
                basic, berr = _call(query, "daily_basic", params, _BASIC_FIELDS)
                if berr:
                    log.warning("daily_basic %s %s-%s: %s", symbol, w0, w1, berr)
                elif basic is not None and not basic.empty:
                    basics.append(basic)
        if bar_error:
            failed.append(f"{symbol} bars {bar_error}")
            continue
        raw = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
        basic = pd.concat(basics, ignore_index=True) if basics else pd.DataFrame()
        bars = bars_frame(raw, symbol, kind, turnover=basic if kind == "stock" else None)
        before = len(bars)
        bars = _drop_provisional(bars, now)
        if bars.empty:
            if before:
                failed.append(f"{symbol} bars: only an unclosed session, not stored")
            else:
                failed.append(f"{symbol} bars: empty")
            continue
        if kind == "index":
            counts["index"] += upsert(con, "index_bar", bars, keys=["symbol", "trade_date"])
        else:
            counts[kind] += upsert(con, "daily_bar", bars, keys=["symbol", "trade_date"])
        if kind == "stock" and not basic.empty:
            val = valuation_frame(basic, symbol)
            if not val.empty:
                counts["valuation"] += upsert(con, "valuation_daily", val,
                                              keys=["symbol", "trade_date", "src"])
    counts["failed"] = failed
    log.info("tushare sample: %s", {k: v for k, v in counts.items() if k != "failed"})
    return counts
