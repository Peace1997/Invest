"""指数基金买卖观察。

复用持仓健康度同一套趋势/估值打分口径, 只把输出翻译成更适合指数基金的
动作: 买入观察 / 定投持有 / 减仓回避。数据只读仓库, 不在推荐阶段联网。
"""
from __future__ import annotations

import re
from math import ceil

import pandas as pd

from ..decision.price_levels import compute_price_levels
from ..factors.technical import trend_features
from ..factors.valuation import valuation_info
from .base import SignalOutput
from .scoring import score_components

_EXCLUDE_NAME = re.compile("货币|现金|债|国债|政金债|信用债|可转债|短融|同业存单")


def _name_map(con, codes: list[str]) -> dict[str, str]:
    if not codes:
        return {}
    qs = ",".join(["?"] * len(codes))
    try:
        rows = con.execute(
            f"SELECT symbol, name FROM instruments WHERE symbol IN ({qs})",
            codes,
        ).fetchall()
        return {str(r[0]).zfill(6): r[1] for r in rows}
    except Exception:
        return {}


def _theme(name: str, code: str) -> str:
    text = f"{name}{code}"
    for key, tag in [
        ("沪深300", "A股核心指数"),
        ("中证500", "A股中盘指数"),
        ("中证1000", "A股小盘指数"),
        ("科创50", "科技成长指数"),
        ("创业板", "成长风格指数"),
        ("上证50", "大盘蓝筹指数"),
        ("红利", "红利策略指数"),
        ("纳指", "美股科技指数"),
        ("纳斯达克", "美股科技指数"),
        ("标普", "美股宽基指数"),
        ("恒生", "港股指数"),
        ("黄金", "商品指数"),
    ]:
        if key in text:
            return tag
    return "指数基金"


def _action(score: float) -> tuple[str, str, str]:
    if score >= 0.50:
        return "买入观察", "进攻观察", "long"
    if score >= 0.15:
        return "分批/定投", "进攻观察", "long"
    if score > -0.15:
        return "按计划持有", "中性观察", "flat"
    if score > -0.50:
        return "暂停加仓", "防守观察", "short"
    return "卖出/回避", "减仓观察", "short"


def _pct(close: pd.Series, bars: int) -> float | None:
    s = pd.Series(close).dropna().astype(float)
    if len(s) <= bars:
        return None
    return float((s.iloc[-1] / s.iloc[-bars - 1] - 1) * 100)


def _bullets(f, val) -> list[str]:
    if f is None:
        return ["历史样本不足, 暂不做明确买卖判断。"]
    out: list[str] = []
    if f.ma20 is not None and f.ma60 is not None:
        if f.last < f.ma20:
            out.append("价格低于20日均线, 短线仍需防守。")
        elif f.ma20 >= f.ma60:
            out.append("20日均线高于60日均线, 中期趋势较健康。")
        else:
            out.append("价格站上20日均线, 但中期均线仍待修复。")
    elif f.ma60 is not None:
        out.append("样本不足以看长期均线, 先以60日均线作参考。")

    if f.ret60 is not None:
        if f.ret60 >= 8:
            out.append(f"近60日上涨{f.ret60:.1f}%, 趋势偏强但避免追高。")
        elif f.ret60 <= -8:
            out.append(f"近60日下跌{abs(f.ret60):.1f}%, 等企稳或小仓试探。")
        else:
            out.append(f"近60日涨跌{f.ret60:+.1f}%, 处于震荡区间。")

    if f.rsi14 is not None:
        if f.rsi14 >= 75:
            out.append(f"RSI {f.rsi14:.0f} 偏热, 新买等待回落。")
        elif f.rsi14 <= 30:
            out.append(f"RSI {f.rsi14:.0f} 偏冷, 可观察反弹确认。")
        else:
            out.append(f"RSI {f.rsi14:.0f} 在中性区, 等待更明确方向。")

    if val is not None and val.pe_pct is not None:
        if val.pe_pct <= 25:
            out.append(f"估值分位{val.pe_pct:.0f}%, 处历史偏低区域。")
        elif val.pe_pct >= 75:
            out.append(f"估值分位{val.pe_pct:.0f}%, 追高性价比下降。")
    return out[:4]


def _etf_prices(con, code: str) -> pd.DataFrame | None:
    code = code.zfill(6)
    try:
        df = con.execute(
            "SELECT b.trade_date, b.open, b.high, b.low, b.close, b.pct_chg, "
            "       a.close AS adj_close "
            "FROM daily_bar b LEFT JOIN daily_bar_adj a "
            "  ON b.symbol=a.symbol AND b.trade_date=a.trade_date "
            "WHERE b.symbol=? AND b.type='etf' ORDER BY b.trade_date",
            [code],
        ).df()
    except Exception:
        df = con.execute(
            "SELECT trade_date, open, high, low, close, pct_chg, close AS adj_close "
            "FROM daily_bar WHERE symbol=? AND type='etf' ORDER BY trade_date",
            [code],
        ).df()
    return df if df is not None and len(df) else None


def _nav_prices(con, code: str) -> pd.DataFrame | None:
    try:
        df = con.execute(
            "SELECT nav_date AS trade_date, nav AS close, daily_pct AS pct_chg, nav AS adj_close "
            "FROM fund_nav WHERE symbol=? ORDER BY nav_date",
            [code],
        ).df()
    except Exception:
        return None
    if df is None or df.empty:
        return None
    df["open"] = df["close"]
    df["high"] = df["close"]
    df["low"] = df["close"]
    return df


def _score_fund(con, code: str, name: str, kind: str, min_obs: int) -> SignalOutput | None:
    df = _etf_prices(con, code) if kind == "etf" else _nav_prices(con, code)
    if df is None or len(df) < min_obs:
        return None
    code = code.zfill(6)
    f = trend_features(df["adj_close"])
    val = valuation_info(con, code)
    score, comps, conf = score_components(f, val=val)
    action, posture, direction = _action(score)
    close = float(df["close"].iloc[-1])
    as_of = df["trade_date"].iloc[-1]
    today_pct = float(df["pct_chg"].iloc[-1]) if pd.notna(df["pct_chg"].iloc[-1]) else _pct(df["close"], 1)
    week_pct = _pct(df["close"], 5)
    month_pct = _pct(df["close"], 21)
    levels = compute_price_levels(df[["trade_date", "open", "high", "low", "close"]])
    reason = " · ".join(
        f"{c.label.split('·')[-1]} {c.contribution:+.2f}"
        for c in comps if c.contribution != 0
    ) or "中性"
    return SignalOutput(
        target=code,
        signal_name="index_fund_scorer",
        score=score,
        direction=direction,
        horizon="mid",
        reason=reason,
        confidence=conf,
        as_of=as_of,
        components=comps,
        metadata={
            "name": name or code,
            "type": kind,
            "theme": _theme(name or "", code),
            "close": close,
            "today_pct": today_pct,
            "week_pct": week_pct,
            "month_pct": month_pct,
            "strategy_score": int(round((score + 1.0) * 50)),
            "action": action,
            "posture": posture,
            "pe_pct": val.pe_pct if val else None,
            "pe_ttm": val.pe_ttm if val else None,
            "levels": levels,
            "bullets": _bullets(f, val),
            "n_obs": f.n if f else 0,
        },
    )


def recommend_index_funds(
    con,
    top_n: int = 8,
    min_obs: int = 60,
    held: set[str] | None = None,
    extra_funds: dict[str, tuple[str, str]] | None = None,
) -> tuple[list[SignalOutput], str]:
    """Return index-fund buy/sell observations.

    ETF universe comes from `daily_bar(type='etf')`. `extra_funds` may add held
    OTC index funds already stored in `fund_nav`: {code: (name, type)}.
    """
    held = {str(x).zfill(6) for x in (held or set())}
    extra_funds = extra_funds or {}
    try:
        rows = con.execute(
            "SELECT symbol, count(*) AS n FROM daily_bar WHERE type='etf' "
            "GROUP BY symbol HAVING count(*)>=?",
            [min_obs],
        ).fetchall()
    except Exception:
        rows = []
    codes = [str(r[0]).zfill(6) for r in rows]
    names = _name_map(con, codes)

    scored: list[SignalOutput] = []
    skipped_name = 0
    for code in codes:
        name = names.get(code, code)
        if _EXCLUDE_NAME.search(name or ""):
            skipped_name += 1
            continue
        sig = _score_fund(con, code, name, "etf", min_obs)
        if sig:
            scored.append(sig)

    for raw_code, (name, kind) in extra_funds.items():
        code = str(raw_code).zfill(6)
        if any(s.target == code for s in scored):
            continue
        if _EXCLUDE_NAME.search(name or ""):
            continue
        sig = _score_fund(con, code, name, kind, min_obs)
        if sig:
            scored.append(sig)

    scored.sort(key=lambda s: s.score, reverse=True)
    if not scored:
        return [], "库内暂无可评分的指数基金(日线/NAV样本不足)"

    n_buy = min(len(scored), max(ceil(top_n * 0.6), 1))
    selected: list[SignalOutput] = scored[:n_buy]
    selected_codes = {s.target for s in selected}

    for s in sorted(scored, key=lambda x: x.score):
        if len(selected) >= top_n:
            break
        if s.target not in selected_codes and s.score <= -0.15:
            selected.append(s)
            selected_codes.add(s.target)

    for s in scored:
        if len(selected) >= top_n:
            break
        if s.target in held and s.target not in selected_codes:
            selected.append(s)
            selected_codes.add(s.target)

    selected.sort(key=lambda s: (s.metadata.get("posture") in ("减仓观察", "防守观察"), -s.score))
    desc = (f"库内ETF {len(codes)} 只, 可评分 {len(scored)} 只"
            + (f", 排除货币/债券类 {skipped_name} 只" if skipped_name else ""))
    return selected[:top_n], desc
