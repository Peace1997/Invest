"""头部沪深300ETF规模 ÷ 沪深300流通市值.

口径(也写在看板上):
  分子  简称含「沪深300」、且不是联接/增强/风格变体的场内 ETF,
        按最新一期期末净资产取前 N(默认 10). 规模=天天基金「规模变动」
        期末净资产, 季度披露, 单位元.
  分母  东方财富沪深300行情「流通市值」(成分流通市值合计), 单位元.
        历史季度没有逐日流通市值时, 用报告期(或之前最近交易日)收盘点位
        相对最新实点等比回推, 隐含流通股本不变. 股本变化会使较早比例偏离.

入库幂等. 网络失败不写半截元数据, 已成功的行保留, 下次再补.
"""
from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from io import StringIO

import pandas as pd
import requests

from ..sources.akshare_src import _ensure_domestic_no_proxy
from ..storage import init_schema, upsert

log = logging.getLogger(__name__)

TOP_N = 10
# 联接与 ETF 持股重复; 增强/风格指数不是沪深300本身.
_EXCLUDE = ("联接", "增强", "指增", "成长", "价值", "ESG", "红利", "现金流", "质量", "低波", "股息")

_GMBD_URL = "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
_QUOTE_URL = "https://push2.eastmoney.com/api/qt/stock/get"
_CLOSE_URL = "https://www.csindex.com.cn/csindex-home/perf/index-perf"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

_FUND_TTL = timedelta(days=7)
_MV_TTL = timedelta(hours=18)
_CLOSE_TTL = timedelta(days=3)

_SCALE_COLS = ["symbol", "name", "report_date", "net_asset", "shares", "src"]
_CLOSE_COLS = ["trade_date", "close", "src"]
_MV_COLS = ["trade_date", "close", "total_mv", "float_mv", "src"]


@dataclass
class Hs300RatioView:
    series: pd.DataFrame
    funds: pd.DataFrame
    top_n: int
    anchor_date: date | None
    anchor_close: float | None
    anchor_float_mv: float | None
    anchor_total_mv: float | None


def empty_view() -> Hs300RatioView:
    return Hs300RatioView(
        series=pd.DataFrame(columns=["report_date", "net_asset", "float_mv", "ratio_pct", "basis", "index_close"]),
        funds=pd.DataFrame(columns=["symbol", "name", "net_asset", "report_date"]),
        top_n=TOP_N,
        anchor_date=None,
        anchor_close=None,
        anchor_float_mv=None,
        anchor_total_mv=None,
    )


def is_plain_hs300_etf(name: object) -> bool:
    text = str(name or "")
    if "沪深300" not in text:
        return False
    return not any(word in text for word in _EXCLUDE)


def parse_gmbd_payload(text: str) -> pd.DataFrame:
    """天天基金规模变动接口原文 → report_date/net_asset(元)/shares(份).

    接口是一段 JS：HTML 表嵌在 content 字符串里，同页还有 data 数组
    （QMJZC/NETNAV 已是元，QMZFE 是份）。优先解析 data，没有再读表。
    """
    parsed = _parse_gmbd_json(text)
    if not parsed.empty:
        return parsed
    return _parse_gmbd_html(text)


def _parse_gmbd_json(text: str) -> pd.DataFrame:
    empty = pd.DataFrame(columns=["report_date", "net_asset", "shares"])
    key = text.find('"data":')
    if key < 0:
        return empty
    start = text.find("[", key)
    if start < 0:
        return empty
    depth = 0
    end = None
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end is None:
        return empty
    try:
        rows = json.loads(text[start:end])
    except json.JSONDecodeError:
        return empty
    out_rows = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        net = _num(item.get("QMJZC"))
        if net is None:
            net = _num(item.get("NETNAV"))
        if net is None:
            continue
        out_rows.append({
            "report_date": _as_date(item.get("FSRQ")),
            "net_asset": net,
            "shares": _num(item.get("QMZFE")),
        })
    out = pd.DataFrame(out_rows, columns=["report_date", "net_asset", "shares"])
    out = out.dropna(subset=["report_date", "net_asset"])
    return out.drop_duplicates("report_date").sort_values("report_date").reset_index(drop=True)


def _parse_gmbd_html(text: str) -> pd.DataFrame:
    empty = pd.DataFrame(columns=["report_date", "net_asset", "shares"])
    start = text.find('content:"')
    if start < 0:
        return empty
    start += len('content:"')
    end = -1
    for marker in ('",summary', '",records'):
        pos = text.find(marker, start)
        if pos >= 0 and (end < 0 or pos < end):
            end = pos
    if end < 0:
        return empty
    html = text[start:end]
    if "<table" not in html:
        return empty
    table = pd.read_html(StringIO(html))[0]
    if table.empty or "日期" not in table.columns or "期末净资产（亿元）" not in table.columns:
        return empty
    out = pd.DataFrame({
        "report_date": pd.to_datetime(table["日期"], errors="coerce").dt.date,
        "net_asset": _yi_to_yuan(table["期末净资产（亿元）"]),
        "shares": _yi_to_yuan(table["期末总份额（亿份）"]) if "期末总份额（亿份）" in table.columns else pd.NA,
    })
    out = out.dropna(subset=["report_date", "net_asset"])
    return out.drop_duplicates("report_date").sort_values("report_date").reset_index(drop=True)


def build_ratio_view(
    scales: pd.DataFrame,
    closes: pd.DataFrame,
    mvs: pd.DataFrame,
    top_n: int = TOP_N,
) -> Hs300RatioView:
    """纯计算. 分母优先用与报告期同一交易日的流通市值实点, 否则按点位回推."""
    view = empty_view()
    view.top_n = top_n
    if scales is None or scales.empty or mvs is None or mvs.empty:
        return view

    funds = _latest_funds(scales)
    if funds.empty:
        return view
    picked = funds.head(min(top_n, len(funds))).copy()
    view.funds = picked.reset_index(drop=True)

    anchor = mvs.sort_values("trade_date").iloc[-1]
    anchor_mv = _f(anchor.get("float_mv"))
    anchor_px = _f(anchor.get("close"))
    anchor_date = _as_date(anchor.get("trade_date"))
    view.anchor_date = anchor_date
    view.anchor_close = anchor_px
    view.anchor_float_mv = anchor_mv
    view.anchor_total_mv = _f(anchor.get("total_mv"))
    if anchor_mv is None or anchor_mv <= 0 or anchor_date is None:
        return view

    symbols = set(picked["symbol"].astype(str))
    hist = scales[scales["symbol"].astype(str).isin(symbols)].copy()
    hist["report_date"] = hist["report_date"].map(_as_date)
    hist["net_asset"] = pd.to_numeric(hist["net_asset"], errors="coerce")
    hist = hist.dropna(subset=["report_date", "net_asset"])
    if hist.empty:
        return view

    grouped = hist.groupby("report_date", as_index=False).agg(
        net_asset=("net_asset", "sum"),
        n=("symbol", "nunique"),
    )
    grouped = grouped[grouped["n"] >= len(symbols)].sort_values("report_date")
    if grouped.empty:
        return view

    close_df = _close_frame(closes)
    if close_df.empty or anchor_px is None or anchor_px <= 0:
        # 没有指数点位就只给最新一期截面: 分子是报告期净资产, 分母是最新流通市值.
        rec = grouped.iloc[-1]
        net_asset = float(rec.net_asset)
        view.series = pd.DataFrame([{
            "report_date": _as_date(rec.report_date),
            "net_asset": net_asset,
            "float_mv": float(anchor_mv),
            "ratio_pct": net_asset / float(anchor_mv) * 100.0,
            "basis": "截面",
            "index_close": anchor_px,
        }])
        return view

    rows = []
    for rec in grouped.itertuples(index=False):
        qdate = _as_date(rec.report_date)
        q_close, close_date = _close_on_or_before(close_df, qdate)
        if q_close is None or q_close <= 0:
            continue
        if close_date == anchor_date:
            denom, basis = anchor_mv, "实值"
        else:
            denom, basis = anchor_mv * q_close / anchor_px, "回推"
        rows.append({
            "report_date": qdate,
            "net_asset": float(rec.net_asset),
            "float_mv": float(denom),
            "ratio_pct": float(rec.net_asset) / float(denom) * 100.0,
            "basis": basis,
            "index_close": q_close,
        })
    if rows:
        view.series = pd.DataFrame(rows).sort_values("report_date").reset_index(drop=True)
    return view


def needs_refresh(con) -> bool:
    ensure_schema(con)
    if _count(con, "hs300_fund_scale") == 0 or _count(con, "hs300_float_mv") == 0:
        return True
    if _meta_stale(con, "fund_scale", _FUND_TTL):
        return True
    if _meta_stale(con, "float_mv", _MV_TTL):
        return True
    if _count(con, "hs300_index_close") == 0 or _meta_stale(con, "index_close", _CLOSE_TTL):
        return True
    return False


def refresh(con) -> str | None:
    """按各自 TTL 补数. 返回给界面的错误摘要; 无错误则 None."""
    ensure_schema(con)
    _ensure_domestic_no_proxy()
    errors: list[str] = []
    if _count(con, "hs300_index_close") == 0 or _meta_stale(con, "index_close", _CLOSE_TTL):
        try:
            _refresh_closes(con)
        except Exception as e:  # noqa: BLE001 - 看板要降级而不是崩
            log.warning("沪深300收盘拉取失败: %s", e)
            errors.append(f"指数点位: {e}")
    if _count(con, "hs300_float_mv") == 0 or _meta_stale(con, "float_mv", _MV_TTL):
        try:
            _refresh_float_mv(con)
        except Exception as e:  # noqa: BLE001
            log.warning("沪深300流通市值拉取失败: %s", e)
            errors.append(f"流通市值: {e}")
    if _count(con, "hs300_fund_scale") == 0 or _meta_stale(con, "fund_scale", _FUND_TTL):
        try:
            _refresh_funds(con)
        except Exception as e:  # noqa: BLE001
            log.warning("沪深300基金规模拉取失败: %s", e)
            errors.append(f"基金规模: {e}")
    return "；".join(errors) if errors else None


def load_view(con, top_n: int = TOP_N) -> Hs300RatioView:
    ensure_schema(con)
    scales = _read(con, "SELECT symbol, name, report_date, net_asset, shares FROM hs300_fund_scale")
    closes = _read(con, "SELECT trade_date, close FROM hs300_index_close")
    mvs = _read(con, "SELECT trade_date, close, total_mv, float_mv FROM hs300_float_mv")
    return build_ratio_view(scales, closes, mvs, top_n=top_n)


def ensure_schema(con) -> None:
    init_schema(con)


def _refresh_closes(con) -> int:
    today = datetime.now(timezone.utc).astimezone(_shanghai()).date()
    start = date(2010, 1, 1).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    r = requests.get(
        _CLOSE_URL,
        params={"indexCode": "000300", "startDate": start, "endDate": end},
        headers={"User-Agent": _UA, "Referer": "https://www.csindex.com.cn/"},
        timeout=20,
    )
    r.raise_for_status()
    payload = r.json()
    rows = payload.get("data") or []
    if not rows:
        raise RuntimeError("中证指数未返回沪深300收盘")
    df = pd.DataFrame(rows)
    df["trade_date"] = pd.to_datetime(df["tradeDate"], format="%Y%m%d", errors="coerce").dt.date
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["src"] = "csindex"
    df = df.dropna(subset=["trade_date", "close"])
    n = upsert(con, "hs300_index_close", df[_CLOSE_COLS], ["trade_date"])
    _mark(con, "index_close")
    log.info("hs300_index_close: %d 行", n)
    return n


def _refresh_float_mv(con) -> int:
    data = _get_json_retry(
        _QUOTE_URL,
        params={
            "invt": "2",
            "fltt": "2",
            "secid": "1.000300",
            "fields": "f43,f57,f58,f86,f116,f117",
        },
        headers={"User-Agent": _UA, "Referer": "https://quote.eastmoney.com/"},
    ).get("data") or {}
    float_mv = _f(data.get("f117"))
    price = _f(data.get("f43"))
    if float_mv is None or float_mv <= 0 or price is None or price <= 0:
        raise RuntimeError("东方财富未返回沪深300流通市值")
    ts = data.get("f86")
    if ts:
        trade_date = datetime.fromtimestamp(float(ts), _shanghai()).date()
    else:
        trade_date = datetime.now(_shanghai()).date()
    df = pd.DataFrame([{
        "trade_date": trade_date,
        "close": price,
        "total_mv": _f(data.get("f116")),
        "float_mv": float_mv,
        "src": "eastmoney_quote",
    }])
    df["total_mv"] = pd.to_numeric(df["total_mv"], errors="coerce")
    n = upsert(con, "hs300_float_mv", df[_MV_COLS], ["trade_date"])
    _mark(con, "float_mv")
    log.info("hs300_float_mv %s: %.3f万亿", trade_date, float_mv / 1e12)
    return n


def _refresh_funds(con) -> int:
    universe = _discover_plain_etfs()
    if universe.empty:
        raise RuntimeError("没有筛到沪深300ETF")
    frames = []
    failed: list[str] = []
    todo = list(universe.itertuples(index=False))

    def _take(row, part):
        if part is None or part.empty:
            return
        part = part.copy()
        part["symbol"] = row.symbol
        part["name"] = row.name
        part["src"] = "eastmoney_gmbd"
        frames.append(part[_SCALE_COLS])

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_fetch_gmbd, row.symbol): row for row in todo}
        for fut in as_completed(futures):
            row = futures[fut]
            try:
                _take(row, fut.result())
            except Exception as e:  # noqa: BLE001 - 单只失败不阻断其余
                log.warning("规模变动 %s 失败: %s", row.symbol, e)
                failed.append(row.symbol)
    if failed:
        still = []
        for row in todo:
            if row.symbol not in set(failed):
                continue
            try:
                _take(row, _fetch_gmbd(row.symbol))
            except Exception as e:  # noqa: BLE001
                log.warning("规模变动重试 %s 失败: %s", row.symbol, e)
                still.append(row.symbol)
        failed = still
    if not frames:
        raise RuntimeError("基金规模接口没有返回可用的期末净资产")
    df = pd.concat(frames, ignore_index=True).drop_duplicates(["symbol", "report_date"])
    n = upsert(con, "hs300_fund_scale", df, ["symbol", "report_date"])
    # 少数单只失败下周再试; 大面积失败不记成功, 下次进入页面会重拉.
    if len(failed) <= max(2, len(todo) // 5):
        _mark(con, "fund_scale")
    log.info("hs300_fund_scale: %d 行, 基金 %d, 失败 %s", n, df["symbol"].nunique(), failed)
    return n


def _discover_plain_etfs() -> pd.DataFrame:
    import akshare as ak

    raw = ak.fund_etf_category_sina(symbol="ETF基金")
    if raw is None or raw.empty:
        return pd.DataFrame(columns=["symbol", "name"])
    df = raw.rename(columns={"代码": "raw_code", "名称": "name"}).copy()
    df = df[df["name"].map(is_plain_hs300_etf)]
    df["symbol"] = df["raw_code"].map(_normalize_code)
    df = df[df["symbol"].str.fullmatch(r"\d{6}")]
    return df[["symbol", "name"]].drop_duplicates("symbol").reset_index(drop=True)


def _fetch_gmbd(symbol: str) -> pd.DataFrame:
    r = requests.get(
        _GMBD_URL,
        params={"type": "gmbd", "code": symbol, "rt": "0.1"},
        headers={"User-Agent": _UA, "Referer": "https://fundf10.eastmoney.com/"},
        timeout=20,
    )
    r.raise_for_status()
    return parse_gmbd_payload(r.text)


def _get_json_retry(url: str, *, params: dict, headers: dict, tries: int = 6) -> dict:
    last = None
    for attempt in range(tries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=8)
            r.raise_for_status()
            body = r.json()
            if not isinstance(body, dict):
                raise RuntimeError("行情返回不是 JSON")
            return body
        except Exception as e:  # noqa: BLE001 - 行情网关偶发 502
            last = e
            if attempt + 1 < tries:
                time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(str(last)) from last


def _latest_funds(scales: pd.DataFrame) -> pd.DataFrame:
    df = scales.copy()
    df["report_date"] = df["report_date"].map(_as_date)
    df["net_asset"] = pd.to_numeric(df["net_asset"], errors="coerce")
    df = df.dropna(subset=["symbol", "report_date", "net_asset"])
    df["symbol"] = df["symbol"].astype(str).str.zfill(6)
    df = df.sort_values(["symbol", "report_date"])
    latest = df.groupby("symbol", as_index=False).tail(1)
    return latest.sort_values("net_asset", ascending=False)[
        ["symbol", "name", "net_asset", "report_date"]
    ]


def _close_frame(closes: pd.DataFrame) -> pd.DataFrame:
    if closes is None or closes.empty:
        return pd.DataFrame(columns=["trade_date", "close"])
    df = closes.copy()
    df["trade_date"] = df["trade_date"].map(_as_date)
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["trade_date", "close"]).drop_duplicates("trade_date")
    return df.sort_values("trade_date").reset_index(drop=True)


def _close_on_or_before(closes: pd.DataFrame, day: date | None) -> tuple[float | None, date | None]:
    if day is None or closes.empty:
        return None, None
    prior = closes[closes["trade_date"].map(lambda d: d <= day)]
    if prior.empty:
        return None, None
    row = prior.iloc[-1]
    return _f(row["close"]), _as_date(row["trade_date"])


def _yi_to_yuan(series: pd.Series) -> pd.Series:
    text = series.astype(str).str.replace(",", "", regex=False).str.replace("％", "", regex=False)
    text = text.replace({"-": pd.NA, "---": pd.NA, "nan": pd.NA, "None": pd.NA})
    return pd.to_numeric(text, errors="coerce") * 1e8


def _normalize_code(raw: object) -> str:
    text = str(raw or "").strip().lower()
    text = re.sub(r"^(sh|sz|bj)", "", text)
    digits = re.sub(r"\D", "", text)
    return digits.zfill(6) if digits else ""


def _as_date(value) -> date | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        return None
    return ts.date()


def _num(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _f(value) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:  # NaN
        return None
    return out


def _shanghai():
    from zoneinfo import ZoneInfo
    return ZoneInfo("Asia/Shanghai")


def _count(con, table: str) -> int:
    try:
        rows = con.execute(f"SELECT count(*) FROM {table}").fetchall()
    except Exception:  # noqa: BLE001
        return 0
    if not rows:
        return 0
    return int(rows[0][0] or 0)


def _read(con, sql: str) -> pd.DataFrame:
    try:
        df = con.execute(sql).df()
    except Exception:  # noqa: BLE001
        return pd.DataFrame()
    return df if df is not None else pd.DataFrame()


def _meta_stale(con, kind: str, ttl: timedelta) -> bool:
    try:
        rows = con.execute(
            "SELECT fetched_at FROM hs300_ratio_meta WHERE kind=?", [kind]
        ).fetchall()
    except Exception:  # noqa: BLE001
        return True
    if not rows or rows[0][0] is None:
        return True
    fetched = pd.Timestamp(rows[0][0])
    if fetched.tzinfo is None:
        fetched = fetched.tz_localize("UTC")
    now = pd.Timestamp.now(tz="UTC")
    return now - fetched > ttl


def _mark(con, kind: str) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    df = pd.DataFrame([{"kind": kind, "fetched_at": now}])
    upsert(con, "hs300_ratio_meta", df, ["kind"])
