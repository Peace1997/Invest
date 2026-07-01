"""每日舆情分析: news_raw → Claude → 情绪分 + 结论 + 利好/利空主题 → sentiment_daily.

诚实约束(见 feedback-data-honesty): 结论只依据喂入的真实新闻, prompt 强制严格 JSON、
禁止编造; 无密钥/无新闻/解析失败都返回 None 且不写库(宁可没有, 不要假的)。卡片标注
"AI生成"与日期, 不冒充官方判断。市场级(不绑个股), 一天一行, 幂等。
"""
from __future__ import annotations
import json
import logging
import time
from datetime import date

import pandas as pd

from ..ingest.news import ingest_news, load_news
from ..llm import complete, NoKeyError
from ..storage import upsert

log = logging.getLogger(__name__)

_DEFAULT_MODEL = "claude-haiku-4-5-20251001"

_SYSTEM = ("你是A股市场的资深舆情分析师。只依据用户提供的当日财经新闻做判断, "
           "不得引入未提供的信息或编造。输出必须是严格 JSON。")

_PROMPT_TMPL = """下面是{n}条今日({day})A股相关财经新闻(快讯/新闻联播)。请综合判断**今日市场整体舆情**, 只依据这些新闻:

{headlines}

输出严格 JSON(不要 markdown 代码块, 不要多余文字), 字段:
{{
  "score": 数值(-1到1, -1极度利空/恐慌, 0中性, 1极度利好/亢奋),
  "label": "偏多" 或 "中性" 或 "偏空",
  "summary": "1-2句中文结论, 点明今日舆情主基调",
  "bullish": ["利好主题", ...],
  "bearish": ["利空主题", ...]
}}
bullish/bearish 各最多3条, 没有就空数组。"""


def _line(r) -> str:
    s = f"- [{r.source}] {r.title}"
    if isinstance(r.summary, str) and r.summary.strip():
        s += " — " + r.summary[:60]
    return s


def generate_sentiment(con, src, cfg: dict | None = None,
                       as_of: date | None = None) -> dict | None:
    """拉当日新闻 → Claude 解读 → 写 sentiment_daily。返回写入行 or None(诚实跳过)。"""
    cfg = cfg or {}
    if not cfg.get("enabled", True):
        return None
    as_of = as_of or date.today()
    model = cfg.get("model", _DEFAULT_MODEL)
    base_url = cfg.get("base_url")
    max_h = int(cfg.get("max_headlines", 80))

    ingest_news(con, src, as_of)                 # 幂等
    news = load_news(con, as_of, limit=max_h)
    if news.empty:
        log.info("舆情: %s 无新闻, 跳过", as_of)
        return None

    headlines = "\n".join(_line(r) for r in news.itertuples())
    prompt = _PROMPT_TMPL.format(n=len(news), day=as_of.isoformat(), headlines=headlines)

    raw = None
    for attempt in range(3):                     # 代理偶发 503/超时 → 轻重试(每天仅一次机会)
        try:
            raw = complete(prompt, model=model, system=_SYSTEM, max_tokens=3000,
                           base_url=base_url)
            break
        except NoKeyError as e:
            log.warning("舆情: %s", e)
            return None
        except Exception as e:  # noqa: BLE001 - LLM/网络异常重试, 仍失败则诚实跳过不编造
            log.warning("舆情 LLM 调用失败(%d/3): %s", attempt + 1, e)
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    if raw is None:
        return None

    data = _parse(raw)
    if data is None:
        log.warning("舆情: LLM 输出无法解析为 JSON, 不写库。原文前200字: %s", raw[:200])
        return None

    row = {
        "as_of_date": as_of,
        "score": float(max(-1.0, min(1.0, float(data.get("score", 0) or 0)))),
        "label": str(data.get("label", "中性"))[:8],
        "summary": str(data.get("summary", ""))[:500],
        "bullish": "；".join(data.get("bullish", []) or [])[:500],
        "bearish": "；".join(data.get("bearish", []) or [])[:500],
        "n_news": int(len(news)),
        "model": model,
    }
    upsert(con, "sentiment_daily", pd.DataFrame([row]), ["as_of_date"])
    return row


def _parse(raw: str) -> dict | None:
    raw = raw.strip()
    if raw.startswith("```"):                    # 去掉可能的 ```json 围栏
        raw = raw.strip("`")
    s, e = raw.find("{"), raw.rfind("}")
    if s < 0 or e <= s:
        return None
    try:
        return json.loads(raw[s:e + 1])
    except Exception:  # noqa: BLE001
        return None


def latest_sentiment(con) -> dict | None:
    df = con.execute(
        "SELECT as_of_date, score, label, summary, bullish, bearish, n_news, model, created_at "
        "FROM sentiment_daily ORDER BY as_of_date DESC LIMIT 1").df()
    if df.empty:
        return None
    return df.iloc[0].to_dict()


def sentiment_history(con, days: int = 30) -> pd.DataFrame:
    """近 days 天的舆情分(升序待调用方排), 供趋势图。空表则返回空 DataFrame。"""
    return con.execute(
        "SELECT as_of_date, score, label, n_news FROM sentiment_daily "
        "ORDER BY as_of_date DESC LIMIT ?", [days]).df()


def render_sentiment(row: dict | None) -> str:
    if not row:
        return ("今日舆情: 暂无 — 配置 ANTHROPIC 密钥(.anthropic_key)后跑 "
                "`ashare sentiment` 或 `ashare daily` 生成。")
    L = ["╔" + "═" * 54,
         f"║ 今日舆情 · {str(row['as_of_date'])[:10]}  (AI生成, 基于 {int(row['n_news'])} 条新闻)",
         "╠" + "═" * 54,
         f"║ 情绪: {row['label']}  (分 {float(row['score']):+.2f}, -1空 ~ +1多)",
         f"║ 结论: {row['summary']}",
         f"║ 利好: {row.get('bullish') or '—'}",
         f"║ 利空: {row.get('bearish') or '—'}",
         "╚" + "═" * 54,
         f"注: {row.get('model', '')} 解读, 仅供参考非投资建议; 结论受当日新闻样本与模型影响。"]
    return "\n".join(L)
