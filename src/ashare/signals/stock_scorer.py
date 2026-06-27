"""个股打分器 / 主板推荐 (ROADMAP Phase 3.2).

Scores a universe (default: 沪深300 的主板成分) on the SAME auditable rubric used
for holdings — 趋势·均线 + 动量 + RSI + 年内位置 + 估值·PE分位 — so a recommendation
comes with the exact factor breakdown, never a black-box pick.

Two-pass for speed + honesty:
  1. 趋势预筛: score every universe member from warehouse bars (no network).
  2. 估值精排: only the finalists get their PE/PB pulled (baidu) and re-scored,
     so "贵不贵" is reflected without 250×网络调用.

Universe is read from data/csi300_main.json (written by the backfill job); if
absent we fall back to all in-DB stocks with enough history — and say so.
"""
from __future__ import annotations
import json
import logging
from pathlib import Path
from statistics import median

import pandas as pd

from ..factors.technical import trend_features
from ..factors.valuation import valuation_info
from ..factors.quality import quality_info
from .scoring import score_components, score_value_components, score_reversal_components
from .base import SignalOutput

log = logging.getLogger(__name__)
# full main-board non-ST universe (written by the backfill job); csi300 is a seed fallback
_UNIVERSE_FILES = [Path("data/main_board.json"), Path("data/csi300_main.json")]


def _name_map(con, codes) -> dict[str, str]:
    if not codes:
        return {}
    qs = ",".join(["?"] * len(codes))
    try:
        rows = con.execute(
            f"SELECT symbol, name FROM instruments WHERE symbol IN ({qs})",
            list(codes)).fetchall()
        return {r[0]: r[1] for r in rows}
    except Exception:
        return {}


def _universe(con, min_obs: int) -> tuple[list[str], str]:
    # only score names we actually have enough history for (honest coverage)
    have = {r[0] for r in con.execute(
        "SELECT symbol FROM daily_bar WHERE type='stock' "
        "GROUP BY symbol HAVING count(*)>=?", [min_obs]).fetchall()}
    for f in _UNIVERSE_FILES:
        if f.exists():
            codes = [str(c).zfill(6) for c in json.loads(f.read_text())]
            covered = [c for c in codes if c in have]
            desc = ("全市场主板非ST" if "main_board" in f.name else "沪深300主板成分")
            return covered, f"{desc}(已覆盖 {len(covered)}/{len(codes)} 只有足够历史)"
    return list(have), "库内有足够历史的个股(非全市场)"


def _bar_close(con, code: str) -> pd.DataFrame | None:
    # close = 名义价(展示用); adj_close = 后复权价(喂 trend_features, 跨除权连续)
    df = con.execute(
        "SELECT b.trade_date, b.close, b.pct_chg, "
        "       b.close * COALESCE(f.adj_factor, 1) AS adj_close "
        "FROM daily_bar b LEFT JOIN adj_factor f "
        "  ON b.symbol = f.symbol AND b.trade_date = f.trade_date "
        "WHERE b.symbol=? ORDER BY b.trade_date", [code.zfill(6)]).df()
    return df if df is not None and len(df) else None


def recommend_stocks(con, ak_src=None, top_n: int = 5, finalists: int = 20,
                     min_obs: int = 120, years: int = 5,
                     exclude: set[str] | None = None,
                     style: str = "momentum") -> tuple[list[SignalOutput], str]:
    """Return (top_n SignalOutputs with breakdown, universe-description).

    style='momentum' (默认): 全主板, 趋势/动量主导(两遍: 趋势预筛 + 估值精排).
    style='value'   : 全主板, 价值+质量主导 + 亏损硬剔除 + 行业分散(稳健版, 读库).
    style='leader'  : 自上而下——先按成分股动量选热门行业, 再在行业内选综合分最高的龙头."""
    if style == "value":
        return _recommend_value(con, ak_src, top_n, min_obs, years, exclude or set())
    if style == "leader":
        return _recommend_leader(con, ak_src, top_n, min_obs, years, exclude or set())
    if style == "reversal":
        return _recommend_reversal(con, top_n, finalists, min_obs, years, exclude or set())
    exclude = exclude or set()
    codes, uni_desc = _universe(con, min_obs)
    codes = [c for c in codes if c not in exclude]

    # pass 1: trend pre-screen (no network)
    prelim: list[tuple[str, float, object, object]] = []
    for code in codes:
        df = _bar_close(con, code)
        if df is None or len(df) < min_obs:
            continue
        f = trend_features(df["adj_close"])   # 后复权喂趋势, df["close"] 仍为名义价(展示)
        score, _, _ = score_components(f)
        last_pct = float(df["pct_chg"].iloc[-1]) if pd.notna(df["pct_chg"].iloc[-1]) else None
        prelim.append((code, score, f, (float(df["close"].iloc[-1]),
                                        df["trade_date"].iloc[-1], last_pct)))
    prelim.sort(key=lambda x: x[1], reverse=True)
    short = prelim[:max(finalists, top_n)]

    # pass 2: valuation refine + buy/sell levels on finalists only
    from ..decision.price_levels import compute_price_levels
    names = _name_map(con, [c for c, *_ in short])
    out: list[SignalOutput] = []
    for code, _, f, (close, as_of, pct) in short:
        val = valuation_info(con, code, years)
        if val is None and ak_src is not None:
            try:
                from ..ingest.valuation import ingest_stock_valuation
                ingest_stock_valuation(con, ak_src, code)
                val = valuation_info(con, code, years)
            except Exception as e:
                log.warning("valuation for %s failed: %s", code, e)
        score, comps, conf = score_components(f, val=val)
        reason = " · ".join(f"{c.label.split('·')[-1]} {c.contribution:+.2f}"
                            for c in comps if c.contribution != 0) or "中性"
        ohlc = con.execute(
            "SELECT trade_date, open, high, low, close FROM daily_bar "
            "WHERE symbol=? ORDER BY trade_date", [code.zfill(6)]).df()
        levels = compute_price_levels(ohlc)
        out.append(SignalOutput(
            target=code, signal_name="stock_scorer", score=score,
            direction="long" if score > 0.15 else "flat",
            horizon="mid", reason=reason, confidence=conf, as_of=as_of,
            components=comps,
            metadata={"name": names.get(code, code), "close": close,
                      "today_pct": pct, "pe_pct": (val.pe_pct if val else None),
                      "pe_ttm": (val.pe_ttm if val else None), "levels": levels}))
    out.sort(key=lambda s: s.score, reverse=True)
    return out[:top_n], uni_desc


def _universe_value(con, min_obs: int) -> tuple[list[str], str]:
    """稳健·价值版 universe = 全主板非ST (有 main_board.json 用它, 否则退 csi300).
    实际可排的子集由估值覆盖决定(见 _recommend_value 的 gate)。"""
    have = {r[0] for r in con.execute(
        "SELECT symbol FROM daily_bar WHERE type='stock' "
        "GROUP BY symbol HAVING count(*)>=?", [min_obs]).fetchall()}
    for f, desc in ((Path("data/main_board.json"), "全市场主板非ST"),
                    (Path("data/csi300_main.json"), "沪深300主板成分")):
        if f.exists():
            codes = [str(c).zfill(6) for c in json.loads(f.read_text())]
            return [c for c in codes if c in have], desc
    return list(have), "库内有足够历史的个股"


def _recommend_value(con, ak_src, top_n, min_obs, years, exclude, max_per_industry=2
                     ) -> tuple[list[SignalOutput], str]:
    """价值+质量主导排序. 只排"估值已覆盖"的票(批量 gate, 快), 亏损(PE≤0)硬剔除,
    趋势仅作确认。质量(ROE/增速/行业)读 fundamentals。估值/基本面均由 value-backfill 落库。
    行业分散: 每个行业最多 max_per_industry 只, 避免选出来清一色银行。"""
    from ..decision.price_levels import compute_price_levels
    codes, uni_desc = _universe_value(con, min_obs)
    # gate: 只保留估值已覆盖的(批量取, 避免逐只查空)
    val_syms = {r[0] for r in con.execute(
        "SELECT DISTINCT symbol FROM valuation_daily").fetchall()}
    codes = [c for c in codes if c not in exclude and c in val_syms]
    names = _name_map(con, codes)
    out: list[SignalOutput] = []
    loss = 0
    st = 0
    for code in codes:
        nm = names.get(code, "")
        if "ST" in nm or "退" in nm:   # 排雷硬闸门: ST/退市风险(退市的-100%回测看不见, 但真金白银致命)
            st += 1
            continue
        df = _bar_close(con, code)
        if df is None or len(df) < min_obs:
            continue
        val = valuation_info(con, code, years)
        if val is None:
            continue
        if val.pe_ttm is not None and val.pe_ttm <= 0:   # 亏损硬闸门
            loss += 1
            continue
        f = trend_features(df["adj_close"])   # 后复权喂趋势, df["close"] 仍为名义价(展示)
        qual = quality_info(con, code)
        score, comps, conf = score_value_components(f, val=val, qual=qual)
        close = float(df["close"].iloc[-1]); as_of = df["trade_date"].iloc[-1]
        pct = float(df["pct_chg"].iloc[-1]) if pd.notna(df["pct_chg"].iloc[-1]) else None
        ohlc = con.execute(
            "SELECT trade_date, open, high, low, close FROM daily_bar "
            "WHERE symbol=? ORDER BY trade_date", [code.zfill(6)]).df()
        levels = compute_price_levels(ohlc)
        reason = " · ".join(f"{c.label.split('·')[-1]} {c.contribution:+.2f}"
                            for c in comps if c.contribution != 0) or "中性"
        out.append(SignalOutput(
            target=code, signal_name="stock_value", score=score,
            direction="long" if score > 0.15 else "flat",
            horizon="mid", reason=reason, confidence=conf, as_of=as_of,
            components=comps,
            metadata={"name": names.get(code, code), "close": close,
                      "today_pct": pct, "pe_pct": val.pe_pct,
                      "pe_ttm": val.pe_ttm, "levels": levels,
                      "roe": (qual.roe if qual else None),
                      "profit_yoy": (qual.profit_yoy if qual else None),
                      "industry": (qual.industry if qual else None)}))
    out.sort(key=lambda s: s.score, reverse=True)
    # 行业分散: 按分高到低取, 每行业不超过 max_per_industry 只
    picked: list[SignalOutput] = []
    per_ind: dict[str, int] = {}
    for s in out:
        ind = s.metadata.get("industry") or "未分类"
        if per_ind.get(ind, 0) >= max_per_industry:
            continue
        picked.append(s)
        per_ind[ind] = per_ind.get(ind, 0) + 1
        if len(picked) >= top_n:
            break
    desc = (f"{uni_desc} · 估值已覆盖 {len(codes)} 只, 排雷剔除 ST/退 {st} 只 + 亏损 {loss} 只 · "
            f"行业分散(每行业≤{max_per_industry})")
    return picked, desc


def _recommend_reversal(con, top_n, finalists, min_obs, years, exclude
                        ) -> tuple[list[SignalOutput], str]:
    """反转·超跌版: 全主板"买近期超跌"(20日动量的镜像), 月度持有/调仓。
    回测(主板2023-2026, 标准harness H=20): IC≈+0.092(71%为正), Top20超额≈+1.68%/月,
    而追涨动量版 IC≈-0.078/超额-0.49% —— 反转是数据支持的正 edge。
    排雷(风控>选股): ST/退、极端暴跌(20日≤-35%, 多为暴雷/退市)、亏损(PE≤0) 一律剔除, 不接刀子。"""
    from ..decision.price_levels import compute_price_levels
    codes, uni_desc = _universe(con, min_obs)
    codes = [c for c in codes if c not in exclude]
    names = _name_map(con, codes)
    st = crash = loss = 0
    prelim: list[tuple[str, float, object, object]] = []
    for code in codes:
        nm = names.get(code, "")
        if "ST" in nm or "退" in nm:          # 排雷: ST/退市风险
            st += 1
            continue
        df = _bar_close(con, code)
        if df is None or len(df) < min_obs:
            continue
        f = trend_features(df["adj_close"])   # 后复权喂趋势, df["close"] 仍为名义价(展示)
        if f is None or f.ret20 is None:
            continue
        if f.ret20 <= -35:                    # 排雷: 极端暴跌不接刀(暴雷/退市风险)
            crash += 1
            continue
        score, _, _ = score_reversal_components(f)
        last_pct = float(df["pct_chg"].iloc[-1]) if pd.notna(df["pct_chg"].iloc[-1]) else None
        prelim.append((code, score, f, (float(df["close"].iloc[-1]),
                                        df["trade_date"].iloc[-1], last_pct)))
    prelim.sort(key=lambda x: x[1], reverse=True)
    short = prelim[:max(finalists, top_n) * 2]   # 多取些, 给亏损剔除留余量

    names2 = _name_map(con, [c for c, *_ in short])
    out: list[SignalOutput] = []
    for code, _, f, (close, as_of, pct) in short:
        val = valuation_info(con, code, years)
        if val is not None and val.pe_ttm is not None and val.pe_ttm <= 0:   # 排雷: 亏损
            loss += 1
            continue
        score, comps, conf = score_reversal_components(f, val=val)
        reason = " · ".join(f"{c.label.split('·')[-1]} {c.contribution:+.2f}"
                            for c in comps if c.contribution != 0) or "中性"
        ohlc = con.execute(
            "SELECT trade_date, open, high, low, close FROM daily_bar "
            "WHERE symbol=? ORDER BY trade_date", [code.zfill(6)]).df()
        levels = compute_price_levels(ohlc)
        out.append(SignalOutput(
            target=code, signal_name="stock_reversal", score=score,
            direction="long" if score > 0.15 else "flat",
            horizon="short", reason=reason, confidence=conf, as_of=as_of, components=comps,
            metadata={"name": names2.get(code, code), "close": close, "today_pct": pct,
                      "pe_pct": (val.pe_pct if val else None),
                      "pe_ttm": (val.pe_ttm if val else None), "levels": levels}))
        if len(out) >= top_n:
            break
    out.sort(key=lambda s: s.score, reverse=True)
    desc = (f"{uni_desc} · 反转·超跌(月度持有) · 排雷剔除 ST/退 {st} + 极端暴跌 {crash} + 亏损 {loss} · "
            "⚠买超跌有'接刀子'风险, 严格止损")
    return out[:top_n], desc


def _recommend_leader(con, ak_src, top_n, min_obs, years, exclude,
                      n_sectors=6, per_sector=2, min_members=3
                      ) -> tuple[list[SignalOutput], str]:
    """自上而下·龙头版: ① 候选个股按 所处行业 分组; ② 板块热度 = 成分股 60日动量中位数,
    取最热 n_sectors 个行业; ③ 在每个热门行业内, 按价值+质量+趋势综合分取前 per_sector 只龙头。
    完全用库内数据(行业来自 yjbb, 估值/质量已落库), 不依赖外部板块接口。"""
    from ..decision.price_levels import compute_price_levels
    codes, uni_desc = _universe_value(con, min_obs)
    # gate: 有基本面(yjbb 全主板)就算候选 —— 板块热度用动量(不需估值), 全主板更准。
    qual_syms = {r[0] for r in con.execute(
        "SELECT DISTINCT symbol FROM fundamentals").fetchall()}
    codes = [c for c in codes if c not in exclude and c in qual_syms]

    # ① 给每只候选打分 + 收集行业/动量(亏损剔除: ROE≤0 或 PE≤0)
    cand: dict[str, list] = {}   # industry -> [ (code, score, comps, conf, ret60, val, qual, df) ]
    loss = 0
    for code in codes:
        df = _bar_close(con, code)
        if df is None or len(df) < min_obs:
            continue
        qual = quality_info(con, code)
        val = valuation_info(con, code, years)   # 有则用(csi300), 无则只按质量+趋势
        if (qual and qual.roe is not None and qual.roe <= 0) or \
           (val and val.pe_ttm is not None and val.pe_ttm <= 0):
            loss += 1
            continue
        f = trend_features(df["adj_close"])   # 后复权喂趋势, df["close"] 仍为名义价(展示)
        score, comps, conf = score_value_components(f, val=val, qual=qual)
        ind = (qual.industry if qual and qual.industry else "未分类")
        cand.setdefault(ind, []).append(
            (code, score, comps, conf, (f.ret60 if f else None), val, qual, df))

    # ② 板块热度 = 成分股 60日动量中位数 (成员太少不算板块)
    heat = {ind: median([m[4] for m in members if m[4] is not None])
            for ind, members in cand.items()
            if len(members) >= min_members and any(m[4] is not None for m in members)}
    hot = sorted(heat, key=lambda i: heat[i], reverse=True)[:n_sectors]

    # ③ 每个热门行业内按综合分取龙头
    names = _name_map(con, [m[0] for members in cand.values() for m in members])
    out: list[SignalOutput] = []
    for ind in hot:
        members = sorted(cand[ind], key=lambda m: m[1], reverse=True)
        for code, score, comps, conf, ret60, val, qual, df in members[:per_sector]:
            close = float(df["close"].iloc[-1]); as_of = df["trade_date"].iloc[-1]
            pct = float(df["pct_chg"].iloc[-1]) if pd.notna(df["pct_chg"].iloc[-1]) else None
            ohlc = con.execute(
                "SELECT trade_date, open, high, low, close FROM daily_bar "
                "WHERE symbol=? ORDER BY trade_date", [code.zfill(6)]).df()
            levels = compute_price_levels(ohlc)
            reason = " · ".join(f"{c.label.split('·')[-1]} {c.contribution:+.2f}"
                                for c in comps if c.contribution != 0) or "中性"
            out.append(SignalOutput(
                target=code, signal_name="stock_leader", score=score,
                direction="long" if score > 0.15 else "flat",
                horizon="mid", reason=reason, confidence=conf, as_of=as_of,
                components=comps,
                metadata={"name": names.get(code, code), "close": close,
                          "today_pct": pct,
                          "pe_pct": (val.pe_pct if val else None),
                          "pe_ttm": (val.pe_ttm if val else None),
                          "levels": levels, "roe": (qual.roe if qual else None),
                          "profit_yoy": (qual.profit_yoy if qual else None),
                          "industry": ind, "sector_heat": round(heat[ind], 1)}))
    # 先按板块热度、再按个股分排序(热板块的龙头靠前)
    out.sort(key=lambda s: (s.metadata["sector_heat"], s.score), reverse=True)
    hot_txt = "、".join(f"{i}({heat[i]:+.0f}%)" for i in hot[:4])
    desc = (f"{uni_desc} · 自上而下: 热门行业 {hot_txt}… (按成分股60日动量) · "
            f"每行业取龙头≤{per_sector}")
    return out[:top_n], desc
