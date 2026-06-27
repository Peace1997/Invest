"""个股速评: 给一个代码, 即时汇总 行情/估值/质量/技术面/打分/买卖位, 可选 LLM 综合解读。

诚实约束(见 feedback-data-honesty):
  - 行情走 TencentSource 实时(场内股), 估值/质量读库(Tushare/baidu, 注意 Tushare 现已过期,
    估值/财务可能 stale, 报告里标 as_of 日期)。
  - 三种打分(动量/价值/反转)都是**回测偏弱/不稳的概率信号**, 非买卖指令: 动量负超额、
    价值弱、反转近1年衰减(见 reference-backtest-findings)。报告显著标注。
  - 个股实时新闻: akshare `stock_news_em` 当前版本有 bug(\\u3000 正则), 暂不接入 → 不编造新闻。
  - LLM 解读只依据下列已取到的真实数字, 不得引入未提供的消息面。
"""
from __future__ import annotations
import logging

import pandas as pd

from .factors.technical import trend_features
from .factors.valuation import valuation_info
from .factors.quality import quality_info
from .signals.scoring import score_components, score_value_components, score_reversal_components
from .decision.price_levels import compute_price_levels
from .llm import complete, NoKeyError

log = logging.getLogger(__name__)

_STYLE_NOTE = {
    "动量": "追强势 — 回测A股主板负超额(IC≈-0.13), 高分=偏亏, 仅反向参考",
    "价值": "便宜+排雷 — 回测信号弱(IC≈+0.06)且2026走弱, 主用于筛选非保证跑赢",
    "反转": "买超跌 — 近3-5年正超额(IC≈+0.09)但近1年衰减为负, 不稳, 小仓位",
}


def _name(con, code: str) -> str:
    r = con.execute("SELECT name FROM instruments WHERE symbol=?", [code]).fetchone()
    return r[0] if r else code


def _reason(comps) -> str:
    return " · ".join(f"{c.label.split('·')[-1]} {c.contribution:+.2f}"
                      for c in comps if c.contribution != 0) or "中性"


def evaluate_stock(con, code: str, with_llm: bool = False, cfg: dict | None = None) -> dict:
    code = str(code).strip().zfill(6)
    if not (code.isdigit() and len(code) == 6):
        return {"ok": False, "error": f"代码须为6位数字: {code!r}"}
    # 名义价(展示/买卖位/当日涨跌)读 daily_bar; 复权价(adj_close, 喂趋势/动量)由 adj_factor 现算
    bars = con.execute(
        "SELECT b.trade_date, b.open, b.high, b.low, b.close, b.pct_chg, "
        "       b.close * COALESCE(f.adj_factor, 1) AS adj_close "
        "FROM daily_bar b LEFT JOIN adj_factor f "
        "  ON b.symbol = f.symbol AND b.trade_date = f.trade_date "
        "WHERE b.symbol=? AND b.type='stock' ORDER BY b.trade_date", [code]).df()
    if bars.empty:
        return {"ok": False, "error": f"库内无 {code} 的日线(可能非主板/未收录/ETF)"}
    name = _name(con, code)

    # 实时行情(场内股)
    quote = None
    try:
        from .sources import TencentSource
        q = TencentSource().spot([code])
        if not q.empty:
            quote = q.iloc[0].to_dict()
    except Exception as e:  # noqa: BLE001
        log.warning("个股速评 实时行情失败 %s: %s", code, e)

    f = trend_features(bars["adj_close"])   # 趋势/动量用后复权, 跨除权连续
    val = valuation_info(con, code)
    qual = quality_info(con, code)
    levels = compute_price_levels(bars[["trade_date", "open", "high", "low", "close"]])

    scores = {
        "动量": score_components(f, val=val),
        "价值": score_value_components(f, val=val, qual=qual),
        "反转": score_reversal_components(f, val=val),
    }
    profile = {
        "code": code, "name": name, "quote": quote, "f": f, "val": val, "qual": qual,
        "levels": levels, "scores": scores,
        "bar_date": str(bars["trade_date"].iloc[-1])[:10],
        "close": float(bars["close"].iloc[-1]),
    }
    text = _render(profile)
    profile["text"] = text
    llm = _llm_eval(profile, cfg) if with_llm else None
    return {"ok": True, "profile": profile, "text": text, "llm": llm}


def _render(p: dict) -> str:
    f, val, qual, lv, q = p["f"], p["val"], p["qual"], p["levels"], p["quote"]
    L = [f"📊 {p['name']}({p['code']}) 速评"]
    if q:
        px = q.get("price"); pc = q.get("pct_chg")
        L.append(f"现价 {px:g}  今 {pc:+.2f}%  (实时)  | 开{q.get('open'):g} 高{q.get('high'):g} 低{q.get('low'):g}"
                 if px is not None else "实时行情未取到")
    L.append(f"收盘基准 {p['close']:g} ({p['bar_date']})")
    # 技术面
    if f is not None:
        parts = [f"趋势 {f.trend_label}"]
        if f.ret20 is not None:  parts.append(f"20日 {f.ret20:+.1f}%")
        if f.ret60 is not None:  parts.append(f"60日 {f.ret60:+.1f}%")
        if f.rsi14 is not None:  parts.append(f"RSI {f.rsi14:.0f}")
        if f.dd_from_high is not None: parts.append(f"距年高 {f.dd_from_high:+.0f}%")
        if f.ann_vol is not None: parts.append(f"年化波动 {f.ann_vol:.0f}%")
        L.append("技术: " + " · ".join(parts))
    # 估值
    if val is not None:
        vt = []
        if val.pe_ttm is not None: vt.append(f"PE {val.pe_ttm:.1f}" + (f"(分位{val.pe_pct:.0f}%)" if val.pe_pct is not None else "(亏损/无分位)"))
        if val.pb is not None: vt.append(f"PB {val.pb:.2f}" + (f"(分位{val.pb_pct:.0f}%)" if val.pb_pct is not None else ""))
        if vt: L.append(f"估值(近{val.years:g}年): " + " · ".join(vt) + f"  [as_of {val.as_of}]")
    else:
        L.append("估值: 无覆盖(Tushare/baidu 未回填或已过期)")
    # 质量
    if qual is not None:
        qt = []
        if qual.roe is not None: qt.append(f"ROE {qual.roe:.1f}%")
        if qual.profit_yoy is not None: qt.append(f"净利增速 {qual.profit_yoy:+.1f}%")
        if qual.revenue_yoy is not None: qt.append(f"营收增速 {qual.revenue_yoy:+.1f}%")
        if qual.gross_margin is not None: qt.append(f"毛利 {qual.gross_margin:.1f}%")
        if qual.debt_ratio is not None: qt.append(f"负债率 {qual.debt_ratio:.1f}%")
        ind = f" · {qual.industry}" if qual.industry else ""
        if qt: L.append(f"质量(报告{qual.report_date}){ind}: " + " · ".join(qt))
    # 买卖位
    if lv is not None:
        rr = f" · 风报比 {lv.rr:.1f}" if lv.rr is not None else ""
        L.append(f"买卖位: 买入区 {lv.buy_lo:g}~{lv.buy_hi:g} · 止损 {lv.stop:g} · 目标 {lv.target:g}{rr}")
        L.append(f"  现价状态: {lv.status}")
    # 三种打分
    L.append("打分(满分±1, 仅概率信号非指令):")
    for k, (total, comps, conf) in p["scores"].items():
        L.append(f"  [{k}] {total:+.2f} (信心{conf:.0%}) — {_reason(comps)}")
        L.append(f"        ↳ {_STYLE_NOTE[k]}")
    L.append("⚠ 以上打分均为回测偏弱/不稳的概率信号; 估值/财务受 Tushare 过期影响可能 stale。非投资建议。")
    return "\n".join(L)


_SYS = ("你是严谨的A股个股分析师。只依据用户提供的量化数据(行情/估值/财务/技术/打分)做解读, "
        "**不得编造任何未提供的消息面/新闻/传闻**。清醒看待: 这些打分是回测偏弱的概率信号。输出中文纯文本。")


def _llm_eval(p: dict, cfg: dict | None) -> str | None:
    cfg = cfg or {}
    prompt = ("基于以下某A股个股的真实量化数据, 给一段综合解读(150-250字), 包含: "
              "①一句话定性 ②估值/基本面是贵是便宜、质量如何 ③技术面位置(强弱/超买超卖/距年高) "
              "④适合的打法(长线/波段/观望/回避)及主要风险。不要编造新闻, 只基于数据。\n\n"
              + p["text"])
    model = cfg.get("sentiment", {}).get("model", "deepseek-v4-pro")
    base_url = cfg.get("sentiment", {}).get("base_url")
    try:
        return complete(prompt, model=model, system=_SYS, max_tokens=4000, base_url=base_url)
    except NoKeyError:
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("个股速评 LLM 失败: %s", e)
        return None
