"""龙虎榜入库 (Phase 4 主力/游资输入). T+1 盘后. 主源 Tushare, 东财兜底.

三张表: lhb_detail(上榜个股+原因) · lhb_inst(机构专用席位) · lhb_yyb(活跃营业部/一线游资).
- Tushare: top_list → detail; top_inst(按营业部) 聚合出 机构席位 与 游资营业部。
- 东财兜底: src.lhb_detail / lhb_inst / lhb_active_seats。
全部幂等 upsert; 价值在交叉验证: 持仓/自选是否上榜、被机构还是游资买。
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

_DETAIL_COLS = ["trade_date", "symbol", "name", "close", "pct_chg", "net_buy",
                "buy_amt", "sell_amt", "lhb_amt", "net_ratio", "turnover_rate",
                "float_mv", "reason", "interpret"]
_INST_COLS = ["trade_date", "symbol", "name", "close", "pct_chg", "buy_org_n",
              "sell_org_n", "org_buy_amt", "org_sell_amt", "org_net_amt",
              "net_ratio", "turnover_rate", "float_mv", "reason"]
_YYB_COLS = ["trade_date", "yyb_code", "yyb_name", "buy_stock_n", "sell_stock_n",
             "buy_amt", "sell_amt", "net_amt", "buy_stocks"]


def ingest_lhb(con, src: AkSource, start: date | None = None,
               end: date | None = None) -> dict:
    """拉 [start, end] 龙虎榜明细/机构/游资 → 三表。默认回看近 7 个自然日。
    tushare 主, 失败回退东财。返回各表落库行数。"""
    end = end or date.today()
    start = start or (end - timedelta(days=7))
    try:
        return _ts_ingest_lhb(con, start, end)
    except Exception as e:  # noqa: BLE001
        log.warning("龙虎榜 tushare 失败, 回退东财: %s", e)
        return _ak_ingest_lhb(con, src, start, end)


# ---- Tushare 主源 ------------------------------------------------------
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


def _ts_trade_dates(con, start: date, end: date) -> list[str]:
    df = con.execute(
        "SELECT trade_date FROM calendar WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date",
        [start, end]).df()
    return [pd.Timestamp(d).strftime("%Y%m%d") for d in df["trade_date"]]


def _map_top_list(tl: pd.DataFrame) -> pd.DataFrame:
    """tushare top_list → lhb_detail 列 (金额单位=元)。"""
    df = tl.rename(columns={
        "pct_change": "pct_chg", "net_amount": "net_buy", "l_buy": "buy_amt",
        "l_sell": "sell_amt", "l_amount": "lhb_amt", "net_rate": "net_ratio",
        "float_values": "float_mv"}).copy()
    df["symbol"] = df["ts_code"].astype(str).str.split(".").str[0]
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    df["interpret"] = None                       # tushare 无"解读"字段
    for c in _DETAIL_COLS:
        if c not in df.columns:
            df[c] = pd.NA
    return df[_DETAIL_COLS]


def _agg_top_inst(ti: pd.DataFrame, name_map: dict, close_map: dict,
                  pct_map: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """tushare top_inst(按营业部逐行) → (lhb_inst 机构专用聚合, lhb_yyb 营业部聚合)。"""
    ti = ti.copy()
    ti["symbol"] = ti["ts_code"].astype(str).str.split(".").str[0]
    for c in ("buy", "sell", "net_buy"):
        ti[c] = pd.to_numeric(ti[c], errors="coerce").fillna(0.0)
    ti["td"] = pd.to_datetime(ti["trade_date"]).dt.date

    # --- 机构席位 (营业部名含"机构专用") ---
    inst = ti[ti["exalter"].str.contains("机构专用", na=False)]
    if inst.empty:
        inst_df = pd.DataFrame(columns=_INST_COLS)
    else:
        g = inst.groupby(["td", "symbol"])
        inst_df = pd.DataFrame({
            "buy_org_n": g["buy"].apply(lambda s: int((s > 0).sum())),
            "sell_org_n": g["sell"].apply(lambda s: int((s > 0).sum())),
            "org_buy_amt": g["buy"].sum(),
            "org_sell_amt": g["sell"].sum(),
            "org_net_amt": g["net_buy"].sum(),
            "reason": g["reason"].first(),
        }).reset_index().rename(columns={"td": "trade_date"})
        inst_df["name"] = inst_df["symbol"].map(name_map)
        inst_df["close"] = inst_df["symbol"].map(close_map)
        inst_df["pct_chg"] = inst_df["symbol"].map(pct_map)
        for c in ("net_ratio", "turnover_rate", "float_mv"):
            inst_df[c] = pd.NA
        inst_df = inst_df[_INST_COLS]

    # --- 游资营业部 (全部营业部按日聚合) ---
    grp = ti.groupby(["td", "exalter"])
    yyb_df = pd.DataFrame({
        "buy_amt": grp["buy"].sum(),
        "sell_amt": grp["sell"].sum(),
        "net_amt": grp["net_buy"].sum(),
    }).reset_index()
    bn = ti[ti["buy"] > 0].groupby(["td", "exalter"])["symbol"].nunique()
    sn = ti[ti["sell"] > 0].groupby(["td", "exalter"])["symbol"].nunique()
    buy2 = ti[ti["buy"] > 0].copy()
    buy2["nm"] = buy2["symbol"].map(name_map).fillna(buy2["symbol"])
    bs = buy2.groupby(["td", "exalter"])["nm"].apply(lambda s: " ".join(dict.fromkeys(s)))
    yyb_df = yyb_df.set_index(["td", "exalter"])
    yyb_df["buy_stock_n"] = bn
    yyb_df["sell_stock_n"] = sn
    yyb_df["buy_stocks"] = bs
    yyb_df = yyb_df.reset_index().rename(columns={"td": "trade_date", "exalter": "yyb_name"})
    yyb_df["yyb_code"] = yyb_df["yyb_name"]        # tushare 无营业部代码, 用名称当键
    yyb_df["buy_stock_n"] = yyb_df["buy_stock_n"].fillna(0).astype(int)
    yyb_df["sell_stock_n"] = yyb_df["sell_stock_n"].fillna(0).astype(int)
    return inst_df, yyb_df[_YYB_COLS]


def _ts_ingest_lhb(con, start: date, end: date) -> dict:
    pro = get_pro()
    dates = _ts_trade_dates(con, start, end)
    if not dates:                                  # calendar 为空时退当日
        dates = [end.strftime("%Y%m%d")]
    det, inst, yyb = [], [], []
    for d in dates:
        # Let a fully unavailable/unauthorized Tushare endpoint bubble up so
        # ingest_lhb can execute the advertised Eastmoney fallback.
        tl = _ts_call(pro.top_list, trade_date=d)
        ti = _ts_call(pro.top_inst, trade_date=d)
        name_map = close_map = pct_map = {}
        if tl is not None and not tl.empty:
            det.append(_map_top_list(tl))
            tlx = tl.copy()
            tlx["symbol"] = tlx["ts_code"].astype(str).str.split(".").str[0]
            name_map = dict(zip(tlx["symbol"], tlx["name"]))
            close_map = dict(zip(tlx["symbol"], tlx["close"]))
            pct_map = dict(zip(tlx["symbol"], tlx["pct_change"]))
        if ti is not None and not ti.empty:
            idf, ydf = _agg_top_inst(ti, name_map, close_map, pct_map)
            if not idf.empty:
                inst.append(idf)
            if not ydf.empty:
                yyb.append(ydf)

    out = {"detail": 0, "inst": 0, "yyb": 0}
    if det:
        df = pd.concat(det, ignore_index=True)
        df["reason"] = df["reason"].fillna("").replace("", "未标注")
        out["detail"] = upsert(con, "lhb_detail", df, ["trade_date", "symbol", "reason"])
    if inst:
        df = pd.concat(inst, ignore_index=True)
        df["reason"] = df["reason"].fillna("").replace("", "未标注")
        out["inst"] = upsert(con, "lhb_inst", df, ["trade_date", "symbol", "reason"])
    if yyb:
        df = pd.concat(yyb, ignore_index=True).drop_duplicates(["trade_date", "yyb_code"])
        out["yyb"] = upsert(con, "lhb_yyb", df, ["trade_date", "yyb_code"])
    log.info("lhb(tushare): 明细%d 机构%d 游资%d (%d 交易日)",
             out["detail"], out["inst"], out["yyb"], len(dates))
    return out


# ---- 东财兜底 ----------------------------------------------------------
def _ak_ingest_lhb(con, src: AkSource, start: date, end: date) -> dict:
    s, e = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
    out = {"detail": 0, "inst": 0, "yyb": 0}
    for key, fn, table, keys in (
        ("detail", src.lhb_detail, "lhb_detail", ["trade_date", "symbol", "reason"]),
        ("inst", src.lhb_inst, "lhb_inst", ["trade_date", "symbol", "reason"]),
        ("yyb", src.lhb_active_seats, "lhb_yyb", ["trade_date", "yyb_code"]),
    ):
        try:
            df = fn(s, e)
            if key != "yyb":
                df["reason"] = df["reason"].fillna("").replace("", "未标注")
            n = upsert(con, table, df, keys=keys)
            out[key] = n
            log.info("lhb %s(东财兜底): %d 行 (%s~%s)", key, n, s, e)
        except Exception as ex:  # noqa: BLE001 - 单源失败不阻断
            log.warning("lhb %s 东财兜底失败: %s", key, ex)
    return out


def latest_lhb_date(con) -> date | None:
    r = con.execute("SELECT max(trade_date) FROM lhb_detail").fetchone()
    return r[0] if r and r[0] else None


def load_lhb_for_symbols(con, symbols: list[str], days: int = 10) -> pd.DataFrame:
    """近 days 天内, 给定个股的龙虎榜上榜(一行一票一天, 含机构净买额若有)。供持仓交叉验证。
    一票一天可有多条上榜原因/机构统计, 聚合成一行避免 JOIN 扇出重复。"""
    if not symbols:
        return pd.DataFrame()
    syms = [str(s).zfill(6) for s in symbols]
    ph = ",".join(["?"] * len(syms))
    return con.execute(
        f"""SELECT d.trade_date, d.symbol,
                   any_value(d.name) AS name, any_value(d.pct_chg) AS pct_chg,
                   max(d.net_buy) AS net_buy, arg_max(d.reason, d.net_buy) AS reason,
                   i.org_net_amt, i.buy_org_n
            FROM lhb_detail d
            LEFT JOIN (SELECT trade_date, symbol, max(org_net_amt) AS org_net_amt,
                              max(buy_org_n) AS buy_org_n
                       FROM lhb_inst GROUP BY trade_date, symbol) i
              ON d.trade_date=i.trade_date AND d.symbol=i.symbol
            WHERE d.symbol IN ({ph})
              AND d.trade_date >= (SELECT max(trade_date) FROM lhb_detail) - {int(days)}
            GROUP BY d.trade_date, d.symbol, i.org_net_amt, i.buy_org_n
            ORDER BY d.trade_date DESC, net_buy DESC""",
        syms).df()


def load_active_seats(con, top: int = 8) -> pd.DataFrame:
    """最新交易日活跃营业部，排除机构与沪深股通专用席位。"""
    return con.execute(
        """SELECT trade_date, yyb_name, buy_amt, sell_amt, net_amt,
                  buy_stock_n, buy_stocks
           FROM lhb_yyb
           WHERE trade_date = (SELECT max(trade_date) FROM lhb_yyb)
             AND coalesce(yyb_name, '') NOT LIKE '%机构专用%'
             AND coalesce(yyb_name, '') NOT LIKE '%股通专用%'
           ORDER BY net_amt DESC LIMIT ?""", [int(top)]).df()


def load_inst_recent(con, top: int = 8) -> pd.DataFrame:
    """最新交易日机构净买入最高的个股(主力机构动向)。按个股聚合(同票多原因合一)。"""
    return con.execute(
        """SELECT trade_date, symbol, any_value(name) AS name,
                  any_value(pct_chg) AS pct_chg, max(org_net_amt) AS org_net_amt,
                  max(buy_org_n) AS buy_org_n, max(sell_org_n) AS sell_org_n
           FROM lhb_inst
           WHERE trade_date = (SELECT max(trade_date) FROM lhb_inst)
           GROUP BY trade_date, symbol
           ORDER BY org_net_amt DESC LIMIT ?""", [int(top)]).df()
