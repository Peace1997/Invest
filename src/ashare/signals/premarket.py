"""开盘竞价短线分析 (9:25 跑, 9:30 前推送)。

⚠️ 诚实口径: 追强势/打板在 A 股回测是**负超额**(见 reference-backtest-findings, 动量 IC≈-0.13)。
本功能是**短线投机线索, 非验证过的策略** —— 只给"竞价异动 + 消息面"的关注候选 + 明确风险/止损,
不保证盈利。仅主板(60/00), 排除 ST/退/一字板(买不进)。

流程: 全市场快照 → 量价初筛(竞价高开+放量+昨日接力, 缩到~25只) → DeepSeek 结合今日舆情
点名 3-5 只 + 理由/关注价/止损/风险 → Server酱 推送。只读 DB, 不写库(短线线索不留档)。
"""
from __future__ import annotations
import json
import logging
from datetime import date, datetime

import pandas as pd

from ..sources import AkSource
from ..llm import complete, NoKeyError
from ..notify.push import send_push
from .sentiment import latest_sentiment

log = logging.getLogger(__name__)

_SYSTEM = ("你是A股超短线接力/打板交易员。只依据用户给的集合竞价异动数据和今日舆情判断, "
           "不得编造未提供的信息。清醒认识追高风险。输出必须是严格 JSON。")


def _z(x: pd.Series) -> pd.Series:
    x = pd.to_numeric(x, errors="coerce")
    sd = x.std(ddof=0)
    return (x - x.mean()) / sd if sd and sd > 0 else x * 0


def _candidates(con, src: AkSource, top: int = 25) -> pd.DataFrame:
    s = src.market_spot()
    s = s[s["symbol"].str.match(r"^(60|00)\d{4}$", na=False)]          # 仅主板
    s = s[~s["name"].astype(str).str.contains("ST|退", na=False)]      # 排除ST/退
    s = s.dropna(subset=["pct_chg", "price", "prev_close"])
    s = s[(s["price"] >= 2) & (s["price"] <= 200)]
    s = s[(s["pct_chg"] >= 1.0) & (s["pct_chg"] <= 9.7)]               # 高开但非一字板
    if s.empty:
        return s
    last = con.execute(
        "SELECT symbol, pct_chg AS y_pct, amount AS y_amt FROM daily_bar "
        "WHERE trade_date=(SELECT max(trade_date) FROM daily_bar) AND type='stock'").df()
    s = s.merge(last, on="symbol", how="left")
    s["vol_ratio"] = pd.to_numeric(s["amount"], errors="coerce") / \
        pd.to_numeric(s["y_amt"], errors="coerce").replace(0, pd.NA)   # 竞价额/昨日全天额
    s["score"] = (_z(s["pct_chg"]) + _z(s["vol_ratio"].fillna(0))
                  + (pd.to_numeric(s["y_pct"], errors="coerce") >= 5).astype(float) * 0.8)
    return s.sort_values("score", ascending=False).head(top)


def _parse(raw: str):
    """取 JSON 数组; 推理模型输出常被 max_tokens 截断(缺]), 故兜底抢救已完整的对象。"""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
    a = raw.find("[")
    if a < 0:
        return None
    b = raw.rfind("]")
    if b > a:
        try:
            return json.loads(raw[a:b + 1])
        except Exception:  # noqa: BLE001
            pass
    # 截断兜底: 扫出 a 之后所有顶层 {...} 完整对象, 丢弃最后被切断的那个
    objs, depth, start = [], 0, None
    for i in range(a + 1, len(raw)):
        ch = raw[i]
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    objs.append(json.loads(raw[start:i + 1]))
                except Exception:  # noqa: BLE001
                    pass
                start = None
    return objs or None


def run_premarket(con, cfg: dict | None = None, force: bool = False) -> dict:
    cfg = cfg or {}
    pm = cfg.get("premarket", {})
    if not pm.get("enabled", True):
        return {"skipped": "disabled"}
    today = date.today()
    if not force and not con.execute(
            "SELECT 1 FROM calendar WHERE trade_date=?", [today]).fetchone():
        return {"skipped": "non-trading-day"}

    src = AkSource()
    cands = _candidates(con, src, top=int(pm.get("pool", 25)))
    if cands is None or cands.empty:
        return {"skipped": "no-candidates"}

    sent = latest_sentiment(con)
    sent_line = (f"{sent['label']}({sent['score']:+.2f}) {sent['summary']}"
                 if sent else "（今日舆情暂无）")
    rows = "\n".join(
        f"- {r.symbol} {r.name} 竞价高开{r.pct_chg:+.1f}% 竞价额{(r.amount or 0)/1e8:.2f}亿"
        f" 昨日{(r.y_pct if pd.notna(r.y_pct) else 0):+.1f}%"
        for r in cands.itertuples())
    n_pick = int(pm.get("picks", 5))
    prompt = (f"今日({today})集合竞价主板异动股(已排除ST/一字板), 量价初筛如下:\n{rows}\n\n"
              f"今日舆情: {sent_line}\n\n"
              f"请从**超短线接力/打板**角度挑最值得今日关注的不超过{n_pick}只, 每只给字段: "
              "code, name, reason(为何竞价异动值得关注, 结合题材/接力/消息面), "
              "watch(参考关注价或'不追高于X元'), stop(止损参考), risk(主要风险)。\n"
              "严格输出 JSON 数组 [{\"code\",\"name\",\"reason\",\"watch\",\"stop\",\"risk\"}], "
              "不要多余文字。强调短线投机、不追一字板、控制仓位。")

    model = cfg.get("sentiment", {}).get("model", "deepseek-v4-pro")
    base_url = cfg.get("sentiment", {}).get("base_url")
    try:
        raw = complete(prompt, model=model, system=_SYSTEM, max_tokens=6000, base_url=base_url)
    except NoKeyError as e:
        log.warning("竞价分析: %s", e)
        return {"skipped": "no-llm-key"}
    except Exception as e:  # noqa: BLE001
        log.warning("竞价分析 LLM 失败: %s", e)
        return {"skipped": f"llm-error: {e}"}

    picks = _parse(raw)
    if not picks:
        log.warning("竞价分析: LLM 输出无法解析。原文: %s", raw[:200])
        return {"skipped": "parse-failed"}

    title = f"🌅 开盘竞价短线 {len(picks)}只 · {datetime.now():%H:%M}"
    lines = ["⚠ 短线投机参考·非验证策略·追高有风险(本项目回测:动量在A股负超额)\n"]
    for p in picks[:n_pick]:
        lines.append(
            f"**{p.get('name','')}（{p.get('code','')}）**\n"
            f"· 关注: {p.get('reason','')}\n"
            f"· 参考: {p.get('watch','')}　止损: {p.get('stop','')}\n"
            f"· 风险: {p.get('risk','')}\n")
    lines.append("———\n仅短线投机线索, 非投资建议; 不追一字板、控仓位、严守止损。")
    body = "\n".join(lines)

    ok = send_push(title, body, cfg.get("alerts", {}))
    return {"picks": len(picks), "pushed": ok,
            "names": [p.get("name") for p in picks[:n_pick]]}
