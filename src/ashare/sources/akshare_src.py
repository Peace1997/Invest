"""AKShare adapter. All function names verified against akshare 1.18.60.

Normalization principle: akshare returns Chinese column names; we rename to
canonical snake_case so downstream code never touches Chinese identifiers.

Proxy note: A-share data hosts (eastmoney / sina / cffex / exchanges) are
domestic. If the user has a foreign HTTP proxy set (e.g. Clash on 127.0.0.1),
requests to these hosts get hijacked through it and fail with ProxyError.
We add these hosts to NO_PROXY so `requests` bypasses the proxy for them,
while leaving the proxy intact for anything else (future LLM calls etc.).
"""
from __future__ import annotations
import os
import time
import logging
from typing import Callable

import akshare as ak
import pandas as pd
import requests
from requests.adapters import HTTPAdapter

from .base import DataSource

log = logging.getLogger(__name__)

# Domestic data hosts that must NOT go through a foreign proxy.
_NO_PROXY_HOSTS = [
    "eastmoney.com",
    "push2.eastmoney.com",
    "push2his.eastmoney.com",
    "datacenter-web.eastmoney.com",
    "quote.eastmoney.com",
    "sina.com.cn",
    "sinajs.cn",
    "finance.sina.com.cn",
    "hq.sinajs.cn",
    "cffex.com.cn",
    "sse.com.cn",
    "szse.cn",
    "csindex.com.cn",
    "tushare.pro",
    "hexin.cn",
    "data.hexin.cn",
    "legulegu.com",
    "www.legulegu.com",
    "baidu.com",
    "gushitong.baidu.com",
]


_PROXY_LOGGED = False
_DEFAULT_TIMEOUT = 10.0   # seconds; akshare issues requests with no timeout
_TIMEOUT_INSTALLED = False


def _install_default_request_timeout() -> None:
    """akshare calls `requests.get(...)` with no timeout, so a stalled host
    (e.g. eastmoney intermittently unreachable) blocks the socket forever and
    hangs the whole UI. Patch HTTPAdapter.send once to inject a default timeout
    when the caller didn't set one — turning an infinite hang into a bounded
    failure that our retry wrapper can handle. Idempotent."""
    global _TIMEOUT_INSTALLED
    if _TIMEOUT_INSTALLED:
        return
    _orig_send = HTTPAdapter.send

    def _send(self, request, *args, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = _DEFAULT_TIMEOUT
        return _orig_send(self, request, *args, **kwargs)

    HTTPAdapter.send = _send
    _TIMEOUT_INSTALLED = True


def _ensure_domestic_no_proxy() -> None:
    """Append domestic data hosts to NO_PROXY / no_proxy env vars so that
    `requests` (used by akshare) bypasses any configured HTTP proxy for them.
    Idempotent: only adds hosts not already present."""
    _install_default_request_timeout()
    for var in ("NO_PROXY", "no_proxy"):
        current = os.environ.get(var, "")
        existing = {h.strip() for h in current.split(",") if h.strip()}
        missing = [h for h in _NO_PROXY_HOSTS if h not in existing]
        if missing:
            merged = ",".join([*existing, *missing]) if existing else ",".join(missing)
            os.environ[var] = merged
    # Visibility: log once if a proxy is configured (so failures are explainable).
    global _PROXY_LOGGED
    proxy = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
    if proxy and not _PROXY_LOGGED:
        log.info("HTTP proxy detected (%s); domestic data hosts bypass it via NO_PROXY", proxy)
        _PROXY_LOGGED = True

# Canonical bar column mapping (eastmoney 东财 OHLCV layout)
_BAR_RENAME = {
    "日期":  "trade_date",
    "开盘":  "open",
    "最高":  "high",
    "最低":  "low",
    "收盘":  "close",
    "成交量": "volume",
    "成交额": "amount",
    "振幅":  "amplitude",
    "涨跌幅": "pct_chg",
    "涨跌额": "change",
    "换手率": "turnover",
}

_NORTH_RENAME = {
    "日期":        "trade_date",
    "当日成交净买额":  "net_buy",
    "买入成交额":     "buy_amount",
    "卖出成交额":     "sell_amount",
    "历史累计净买额":  "cum_net_buy",
    "当日资金流入":    "capital_inflow",
    "当日余额":       "balance",
    "持股市值":       "holding_value",
}
_NORTH_KEEP = list(_NORTH_RENAME.values())


def _exchange_of(symbol: str) -> str:
    """Infer exchange from 6-digit code. A-share + ETF/LOF/债."""
    s = str(symbol).strip().zfill(6)
    head, head2 = s[0], s[:2]
    if head in ("6", "9") or head == "5" or head2 in ("11", "13", "18"):
        return "SH"
    if head in ("0", "2", "3") or head2 in ("15", "16", "17"):
        return "SZ"
    if head in ("4", "8"):
        return "BJ"
    return "?"


class AkSource(DataSource):
    def __init__(self, per_call_sleep: float = 0.15, max_retries: int = 3):
        self.sleep = per_call_sleep
        self.max_retries = max_retries
        self._nav_cache: dict[str, pd.DataFrame] = {}
        self.prefer_sina = False   # if True, skip eastmoney hist, go straight to sina
        self._em_fail_count = 0    # cumulative eastmoney hist failures (circuit breaker)
        _ensure_domestic_no_proxy()

    # ---- internal: retry wrapper -----------------------------------------
    def _call(self, fn: Callable, *args, _retries: int | None = None, **kwargs) -> pd.DataFrame:
        n = _retries or self.max_retries
        last_exc = None
        for attempt in range(1, n + 1):
            try:
                df = fn(*args, **kwargs)
                time.sleep(self.sleep)
                return df
            except Exception as e:
                last_exc = e
                if attempt < n:                       # don't sleep after the final try
                    wait = 2 ** attempt
                    log.warning("akshare call %s failed (%d/%d): %s; sleeping %ds",
                                fn.__name__, attempt, n, e, wait)
                    time.sleep(wait)
        raise RuntimeError(f"akshare call {fn.__name__} failed after retries") from last_exc

    # ---- public methods --------------------------------------------------
    def calendar(self) -> pd.DataFrame:
        df = self._call(ak.tool_trade_date_hist_sina)
        df = df.rename(columns={"trade_date": "trade_date"}).copy()
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        return df[["trade_date"]]

    def stock_list(self) -> pd.DataFrame:
        df = self._call(ak.stock_info_a_code_name)
        df = df.rename(columns={"code": "symbol"})
        df["type"] = "stock"
        df["exchange"] = df["symbol"].map(_exchange_of)
        return df[["symbol", "name", "type", "exchange"]]

    def etf_list(self) -> pd.DataFrame:
        df = self._call(ak.fund_etf_spot_em)
        df = df.rename(columns={"代码": "symbol", "名称": "name"})
        df["type"] = "etf"
        df["exchange"] = df["symbol"].map(_exchange_of)
        return df[["symbol", "name", "type", "exchange"]].drop_duplicates("symbol")

    def global_news(self) -> pd.DataFrame:
        """东财全球财经快讯(最新~200条, 含发布时间). 列: source/title/summary/ts/url。"""
        df = self._call(ak.stock_info_global_em)
        df = df.rename(columns={"标题": "title", "摘要": "summary",
                                "发布时间": "ts", "链接": "url"})
        df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
        df["source"] = "em_global"
        return df[["source", "title", "summary", "ts", "url"]]

    def cctv_news(self, day: str) -> pd.DataFrame:
        """新闻联播文字稿(政策面). day='YYYYMMDD'. 列同 global_news (ts/url 留空)。"""
        empty = pd.DataFrame(columns=["source", "title", "summary", "ts", "url"])
        try:
            df = self._call(ak.news_cctv, date=day)
        except Exception as e:  # noqa: BLE001 - 当日没有/接口抽风都返回空, 不阻断
            log.warning("cctv_news %s 失败: %s", day, e)
            return empty
        if df is None or df.empty:
            return empty
        df = df.rename(columns={"content": "summary"})
        df["source"] = "cctv"
        df["ts"] = pd.NaT
        df["url"] = None
        return df[["source", "title", "summary", "ts", "url"]]

    def market_spot(self) -> pd.DataFrame:
        """全市场实时快照(Sina, 一次调用~10s). 列: symbol/name/price/pct_chg/prev_close/open/high/low/volume/amount。
        盘前集合竞价(9:15-9:25)返回竞价价/竞价量, 9:25 后含开盘价(今开)。盘中含当日最高/最低(尾盘选股用)。amount 单位元。"""
        df = self._call(ak.stock_zh_a_spot)
        df = df.rename(columns={"代码": "raw", "名称": "name", "最新价": "price",
                                "涨跌幅": "pct_chg", "昨收": "prev_close", "今开": "open",
                                "最高": "high", "最低": "low",
                                "成交量": "volume", "成交额": "amount"})
        df["symbol"] = df["raw"].astype(str).str[-6:]   # 去 sh/sz/bj 前缀
        return df[["symbol", "name", "price", "pct_chg", "prev_close", "open",
                   "high", "low", "volume", "amount"]]

    def main_board_symbols(self) -> list[str]:
        """全市场主板非ST代码: 沪市60xxxx + 深市00xxxx, 排除科创688/创业300/北交/ST/退市.
        Uses sina code-name list (independent of eastmoney)."""
        L = self._call(ak.stock_info_a_code_name).copy()
        L["code"] = L["code"].astype(str).str.zfill(6)
        m = L[L["code"].str[:2].isin(["60", "00"])
              & ~L["code"].str[:3].eq("688")
              & ~L["name"].str.contains("ST", case=False, na=False)
              & ~L["name"].str.contains("退", na=False)]
        return m["code"].tolist()

    def st_symbols(self) -> set[str]:
        df = self._call(ak.stock_zh_a_st_em)
        return set(df["代码"].astype(str).str.zfill(6))

    # ---- bars ------------------------------------------------------------
    def _normalize_bar(self, df: pd.DataFrame, symbol: str) -> pd.DataFrame:
        df = df.rename(columns=_BAR_RENAME).copy()
        # stock_zh_a_hist returns 股票代码; drop it (we already know symbol)
        df = df.drop(columns=[c for c in ("股票代码",) if c in df.columns])
        df["symbol"] = str(symbol).zfill(6)
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        cols = ["symbol", "trade_date", "open", "high", "low", "close",
                "volume", "amount", "amplitude", "pct_chg", "change", "turnover"]
        # Fill any missing columns with NaN to be safe across akshare versions
        for c in cols:
            if c not in df.columns:
                df[c] = pd.NA
        return df[cols]

    # Circuit breaker: eastmoney 的日线常整片断连(坑#6), 且抽风是间歇的(偶尔成功一只).
    # 用累计失败数(不被偶发成功清零): 攒够 _EM_FAIL_LIMIT 次就本会话直连 sina。单次只试
    # 1 遍(_retries=1)→ 失败 ~0.5s 即退 sina, 不再 3 次退避空耗 14s。sina 是等价可靠源。
    _EM_FAIL_LIMIT = 8

    def stock_bar(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        if not self.prefer_sina:
            try:
                df = self._call(ak.stock_zh_a_hist, symbol=symbol, period="daily",
                                start_date=start, end_date=end, adjust="", _retries=1)
                return self._normalize_bar(df, symbol)
            except Exception as e:
                self._em_fail_count += 1
                log.warning("eastmoney hist(%s) failed, fallback to sina: %s", symbol, e)
                if self._em_fail_count >= self._EM_FAIL_LIMIT:
                    self.prefer_sina = True
                    log.warning("eastmoney 日线累计失败 %d 次 → 本会话直连 sina(跳过 eastmoney)",
                                self._em_fail_count)
        return self.stock_bar_sina(symbol, start, end)

    def stock_bar_sina(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        """Daily bars from sina (stock_zh_a_daily) — independent of eastmoney.
        sina volume is in 股 → convert to 手(÷100) to match schema; pct/amplitude
        are computed from OHLC (sina omits them)."""
        s = str(symbol).zfill(6)
        pre = "sh" if _exchange_of(s) == "SH" else "sz"
        df = self._call(ak.stock_zh_a_daily, symbol=pre + s,
                        start_date=start, end_date=end, adjust="")
        df = df.rename(columns={"date": "trade_date"}).copy()
        df["symbol"] = s
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        df = df.sort_values("trade_date").reset_index(drop=True)
        if "volume" in df.columns:
            df["volume"] = pd.to_numeric(df["volume"], errors="coerce") / 100.0  # 股→手
        prev = df["close"].shift(1)
        df["change"] = df["close"] - prev
        df["pct_chg"] = 100.0 * df["change"] / prev
        df["amplitude"] = 100.0 * (df["high"] - df["low"]) / prev
        df["turnover"] = pd.to_numeric(df.get("turnover"), errors="coerce") * 100.0 \
            if "turnover" in df.columns else pd.NA
        cols = ["symbol", "trade_date", "open", "high", "low", "close",
                "volume", "amount", "amplitude", "pct_chg", "change", "turnover"]
        for c in cols:
            if c not in df.columns:
                df[c] = pd.NA
        return df[cols]

    def etf_bar(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        df = self._call(ak.fund_etf_hist_em, symbol=symbol, period="daily",
                        start_date=start, end_date=end, adjust="")
        return self._normalize_bar(df, symbol)

    def index_bar(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        """index_zh_a_hist hits 80.push2.eastmoney which is flaky; fall back to
        stock_zh_index_daily_em which uses a different host."""
        try:
            df = self._call(ak.index_zh_a_hist, symbol=symbol, period="daily",
                            start_date=start, end_date=end)
            return self._normalize_bar(df, symbol)
        except Exception as e:
            log.warning("index_zh_a_hist(%s) failed, fallback to stock_zh_index_daily_em: %s",
                        symbol, e)
            alt = self._index_alt_symbol(symbol)
            df = self._call(ak.stock_zh_index_daily_em, symbol=alt,
                            start_date=start, end_date=end)
            df = df.rename(columns={
                "date": "trade_date", "open": "open", "close": "close",
                "high": "high", "low": "low", "volume": "volume",
                "amount": "amount",
            })
            df["symbol"] = str(symbol).zfill(6)
            df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
            df = df.sort_values("trade_date").reset_index(drop=True)
            # Fallback source omits pct_chg/change/amplitude — compute from OHLC.
            prev_close = df["close"].shift(1)
            df["change"] = df["close"] - prev_close
            df["pct_chg"] = 100.0 * df["change"] / prev_close
            df["amplitude"] = 100.0 * (df["high"] - df["low"]) / prev_close
            if "turnover" not in df.columns:
                df["turnover"] = pd.NA
            return df[["symbol", "trade_date", "open", "high", "low", "close",
                       "volume", "amount", "amplitude", "pct_chg", "change", "turnover"]]

    @staticmethod
    def _index_alt_symbol(symbol: str) -> str:
        """Map plain index code → prefixed symbol used by stock_zh_index_daily_em."""
        s = str(symbol).zfill(6)
        if s.startswith(("000",)):     # 上证 / 沪深300 (000300) etc
            return "sh" + s
        if s.startswith(("399",)):     # 深证成指/中小100/创业板指
            return "sz" + s
        return "csi" + s                # 中证指数

    # ---- open-end fund NAV -----------------------------------------------
    def fund_nav_history(self, code: str) -> pd.DataFrame | None:
        """Full unit-NAV (单位净值走势) series for an OTC fund, ascending by date.

        Columns: nav_date (date) · nav (float) · daily_pct (float|NaN). Cached
        per code so valuation and trend-scoring share one network call.
        """
        key = str(code).zfill(6)
        if key in self._nav_cache:
            return self._nav_cache[key]
        try:
            df = self._call(ak.fund_open_fund_info_em, symbol=key,
                            indicator="单位净值走势")
        except Exception as e:
            log.warning("fund_nav_history(%s) failed: %s", code, e)
            return None
        if df is None or df.empty:
            return None
        df = df.rename(columns={"净值日期": "nav_date", "单位净值": "nav", "日增长率": "daily_pct"})
        df = df[["nav_date", "nav", "daily_pct"]].copy()
        df["nav_date"] = pd.to_datetime(df["nav_date"]).dt.date
        df["nav"] = pd.to_numeric(df["nav"], errors="coerce")
        df["daily_pct"] = pd.to_numeric(df["daily_pct"], errors="coerce")
        df = df.dropna(subset=["nav"]).sort_values("nav_date").reset_index(drop=True)
        self._nav_cache[key] = df
        return df

    def fund_nav_latest(self, code: str) -> dict | None:
        """Latest published unit NAV. Returns {nav_date, nav, daily_pct} or None.

        NAV is T-1 settled: funds publish the day's NAV in the evening, so during
        the day this is the previous trading day's value — surfaced via nav_date.
        """
        df = self.fund_nav_history(code)
        if df is None or df.empty:
            return None
        last = df.iloc[-1]
        try:
            return {
                "nav_date": last["nav_date"],
                "nav": float(last["nav"]),
                "daily_pct": float(last["daily_pct"]) if pd.notna(last["daily_pct"]) else None,
            }
        except (ValueError, TypeError):
            return None

    # ---- valuation (PE/PB time series) -----------------------------------
    def stock_valuation_hist(self, code: str) -> pd.DataFrame | None:
        """Per-stock valuation history (百度股市通). Columns: trade_date, pe_ttm,
        pb, total_mv (亿元). Full history (period=全部). None on failure."""
        code = str(code).zfill(6)
        try:
            pe = self._call(ak.stock_zh_valuation_baidu, symbol=code,
                            indicator="市盈率(TTM)", period="全部")
            pb = self._call(ak.stock_zh_valuation_baidu, symbol=code,
                            indicator="市净率", period="全部")
            mv = self._call(ak.stock_zh_valuation_baidu, symbol=code,
                            indicator="总市值", period="全部")
        except Exception as e:
            log.warning("stock_valuation_hist(%s) failed: %s", code, e)
            return None
        out = pe.rename(columns={"date": "trade_date", "value": "pe_ttm"})
        out = out.merge(pb.rename(columns={"date": "trade_date", "value": "pb"}),
                        on="trade_date", how="outer")
        out = out.merge(mv.rename(columns={"date": "trade_date", "value": "total_mv"}),
                        on="trade_date", how="outer")
        out["trade_date"] = pd.to_datetime(out["trade_date"]).dt.date
        for c in ("pe_ttm", "pb", "total_mv"):
            out[c] = pd.to_numeric(out[c], errors="coerce")
        return out.dropna(subset=["pe_ttm", "pb"], how="all").sort_values("trade_date")

    def index_valuation_hist(self, index_name: str) -> pd.DataFrame | None:
        """Index valuation history (乐咕乐股). `index_name` is Chinese, e.g.
        '沪深300'/'中证500'/'中证1000'/'科创50'. Columns: trade_date, pe_ttm, pb."""
        try:
            pe = self._call(ak.stock_index_pe_lg, symbol=index_name)
        except Exception as e:
            log.warning("index_valuation_hist pe(%s) failed: %s", index_name, e)
            return None
        pe = pe.rename(columns={"日期": "trade_date", "滚动市盈率": "pe_ttm"})[["trade_date", "pe_ttm"]]
        try:
            pb = self._call(ak.stock_index_pb_lg, symbol=index_name)
            pb = pb.rename(columns={"日期": "trade_date", "市净率": "pb"})[["trade_date", "pb"]]
            out = pe.merge(pb, on="trade_date", how="left")
        except Exception as e:
            log.warning("index_valuation_hist pb(%s) failed: %s", index_name, e)
            out = pe
            out["pb"] = pd.NA
        out["trade_date"] = pd.to_datetime(out["trade_date"]).dt.date
        for c in ("pe_ttm", "pb"):
            out[c] = pd.to_numeric(out[c], errors="coerce")
        return out.dropna(subset=["pe_ttm"]).sort_values("trade_date")

    # ---- fundamentals / quality (财务摘要) -------------------------------
    def financial_abstract(self, code: str) -> dict | None:
        """Latest-annual quality metrics from 东财财务摘要 (one call, ~0.6s).

        Returns {report_date, roe, net_margin, gross_margin, debt_ratio,
        profit_yoy, revenue_yoy} — all in percent — or None. Uses the most
        recent annual report (YYYY1231) so ROE/增速 are comparable (quarterly
        ROE is cumulative-to-quarter, not annualized → would mislead)."""
        fields = {
            ("盈利能力", "净资产收益率(ROE)"): "roe",
            ("盈利能力", "销售净利率"): "net_margin",
            ("盈利能力", "毛利率"): "gross_margin",
            ("财务风险", "资产负债率"): "debt_ratio",
            ("成长能力", "归属母公司净利润增长率"): "profit_yoy",
            ("成长能力", "营业总收入增长率"): "revenue_yoy",
        }
        try:
            df = self._call(ak.stock_financial_abstract, symbol=str(code).zfill(6))
        except Exception as e:
            log.warning("financial_abstract(%s) failed: %s", code, e)
            return None
        if df is None or df.empty:
            return None
        annuals = sorted([c for c in df.columns if str(c).endswith("1231")], reverse=True)
        if not annuals:
            return None
        col = annuals[0]
        out: dict = {"report_date": pd.to_datetime(col).date()}
        for (opt, ind), key in fields.items():
            sub = df[(df["选项"] == opt) & (df["指标"] == ind)]
            v = pd.to_numeric(sub[col].iloc[0], errors="coerce") if len(sub) else None
            out[key] = float(v) if v is not None and pd.notna(v) else None
        return out

    def market_performance(self, report_date: str) -> pd.DataFrame:
        """Whole-market 业绩报表 (one call): ROE/同比增速/毛利率/所处行业 for all
        A-shares at a report period. `report_date` like '20251231' (annual, so ROE
        is annualized). Columns: symbol, roe, gross_margin, profit_yoy, revenue_yoy,
        industry — all percent. Covers the full board far cheaper than per-stock."""
        df = self._call(ak.stock_yjbb_em, date=report_date)
        df = df.rename(columns={
            "股票代码": "symbol", "净资产收益率": "roe", "销售毛利率": "gross_margin",
            "净利润-同比增长": "profit_yoy", "营业总收入-同比增长": "revenue_yoy",
            "所处行业": "industry"})
        df["symbol"] = df["symbol"].astype(str).str.zfill(6)
        cols = ["symbol", "roe", "gross_margin", "profit_yoy", "revenue_yoy", "industry"]
        for c in cols:
            if c not in df.columns:
                df[c] = pd.NA
        out = df[cols].copy()
        for c in ("roe", "gross_margin", "profit_yoy", "revenue_yoy"):
            out[c] = pd.to_numeric(out[c], errors="coerce")
        return out

    # ---- industry boards (行业板块) --------------------------------------
    def industry_boards(self) -> pd.DataFrame:
        """East money 行业板块 list. Columns incl: name, pct, total_mv."""
        df = self._call(ak.stock_board_industry_name_em)
        return df.rename(columns={"板块名称": "name", "板块代码": "code",
                                  "涨跌幅": "pct", "总市值": "total_mv"})

    def industry_board_hist(self, name: str, start: str = "20210101") -> pd.DataFrame:
        """Daily close series for one 行业板块. Columns: trade_date, close."""
        df = self._call(ak.stock_board_industry_hist_em, symbol=name,
                        start_date=start, end_date="20500101",
                        period="日k", adjust="")
        df = df.rename(columns={"日期": "trade_date", "收盘": "close"})
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        return df[["trade_date", "close"]].sort_values("trade_date")

    # ---- industry boards: 同花顺(THS) 备份源 (东财行业接口断连时 fallback) -----
    def industry_boards_ths(self) -> pd.DataFrame:
        """同花顺行业板块一览. Columns: name, pct (与东财 industry_boards 同形)."""
        df = self._call(ak.stock_board_industry_summary_ths)
        return df.rename(columns={"板块": "name", "涨跌幅": "pct"})

    def industry_board_hist_ths(self, name: str, start: str = "20210101") -> pd.DataFrame:
        """同花顺单板块指数日线. Columns: trade_date, close."""
        df = self._call(ak.stock_board_industry_index_ths, symbol=name,
                        start_date=start, end_date="20500101")
        df = df.rename(columns={"日期": "trade_date", "收盘价": "close"})
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        return df[["trade_date", "close"]].sort_values("trade_date")

    # ---- northbound ------------------------------------------------------
    def northbound_hist(self, channel: str = "北向资金") -> pd.DataFrame:
        df = self._call(ak.stock_hsgt_hist_em, symbol=channel)
        df = df.rename(columns=_NORTH_RENAME)
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        df["channel"] = channel.replace("资金", "")  # '北向资金' -> '北向'
        keep = ["trade_date", "channel"] + [c for c in _NORTH_KEEP if c != "trade_date"]
        for c in keep:
            if c not in df.columns:
                df[c] = pd.NA
        return df[keep]
