"""Streamlit app — Financial Analysis.

Reuses the existing layers (no logic forked here):
  portfolio  → 估值     signals → 健康分/温度计     decision → 操作建议
  snapshots  → 收益曲线  factors → 技术指标

Run:  uv run streamlit run src/ashare/ui/app.py
 or:  uv run python -m ashare.cli ui
"""
from __future__ import annotations
import os
import re
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

# allow `streamlit run src/ashare/ui/app.py` without installing the package
import sys, pathlib
_SRC = pathlib.Path(__file__).resolve().parents[2]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ashare.config import load, resolve_path
from ashare.storage import open_db, init_schema
from ashare.sources import AkSource
from ashare.portfolio import load_positions, value_portfolio, Holding
from ashare.signals import score_portfolio, index_timing, recommend_stocks, recommend_index_funds
from ashare.decision import generate_advice, compute_price_levels
from ashare.snapshots import load_history, snapshot_portfolio, max_drawdown
from ashare.factors.nav import nav_history
from ashare.risk import portfolio_risk, render_risk
from ashare.rebalance import rebalance_plan, render_rebalance
from ashare.ingest.news import ingest_news
from ashare.ingest.market_state import ingest_market_breadth, latest_market_breadth
from ashare.signals.sentiment import generate_sentiment, latest_sentiment, sentiment_history
from ashare.decision_signals import evaluate_due_signals
from ashare.repositories import DecisionJournal, MarketWarehouse

st.set_page_config(
    page_title="Invest · 市场情报",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

UP, DN = "#e23b3b", "#1aae5c"   # A股惯例: 红涨绿跌

MONITOR_INTERVAL = timedelta(seconds=60)
PAGE_REFRESH_INTERVAL = timedelta(seconds=60)
AI_AUTO_REFRESH_INTERVAL = timedelta(minutes=15)
AI_NEWS_DELTA_THRESHOLD = 30
ALERT_RULES = [
    ("重大风险", 3, ("战争", "袭击", "爆炸", "制裁", "关税", "违约", "破产", "暴雷", "退市", "停牌", "熔断")),
    ("政策监管", 2, ("国务院", "证监会", "央行", "财政部", "发改委", "监管", "降准", "降息", "IPO", "印花税")),
    ("宏观冲击", 2, ("美联储", "CPI", "非农", "PMI", "汇率", "人民币", "美元指数", "原油", "黄金", "国债")),
    ("市场异动", 2, ("跳水", "大涨", "大跌", "创阶段新高", "创阶段新低", "北向", "成交额", "恐慌")),
    ("产业主题", 1, ("半导体", "芯片", "人工智能", "算力", "新能源", "光伏", "医药", "房地产", "低空经济")),
]
LOCAL_BULLISH_WORDS = (
    "利好", "上涨", "大涨", "反弹", "突破", "创新高", "增持", "回购", "超预期", "降准", "降息",
    "扩产", "订单", "中标", "复苏", "景气", "走强", "涨停", "净流入",
)
LOCAL_BEARISH_WORDS = (
    "利空", "下跌", "大跌", "跳水", "跌停", "减持", "亏损", "不及预期", "违约", "退市",
    "停牌", "制裁", "监管", "调查", "处罚", "爆雷", "破产", "裁员", "走弱", "净流出",
)
THEME_KEYWORDS = {
    "AI/算力": ("人工智能", "AI", "算力", "芯片", "半导体", "GPU", "数据中心"),
    "新能源": ("新能源", "光伏", "锂电", "储能", "电动车", "风电", "绿色电力"),
    "医药": ("医药", "创新药", "医疗", "医保", "疫苗", "CXO"),
    "地产金融": ("房地产", "地产", "银行", "券商", "保险", "信托"),
    "红利/央企": ("红利", "央企", "国企", "中特估", "分红"),
    "黄金/商品": ("黄金", "原油", "铜", "商品", "大宗"),
}


def _apply_dashboard_style():
    """Keep the Streamlit surface compact and scan-friendly on desktop/mobile."""
    st.markdown(
        """
        <style>
        .stMainBlockContainer {
            max-width: 1480px;
            padding-top: 1.35rem;
            padding-bottom: 3rem;
        }
        [data-testid="stHeader"] { background: transparent; }
        [data-testid="stSidebar"] {
            border-right: 1px solid color-mix(in srgb, var(--text-color) 12%, transparent);
        }
        h1, h2, h3, h4, h5, h6, p, button, input, label {
            letter-spacing: 0 !important;
        }
        h1 { font-size: 1.85rem !important; line-height: 1.2 !important; }
        h2 { font-size: 1.35rem !important; }
        h3 { font-size: 1.05rem !important; }
        [data-testid="stMetric"] {
            min-height: 6.9rem;
            padding: 0.8rem 0.9rem;
            border: 1px solid color-mix(in srgb, var(--text-color) 12%, transparent);
            border-radius: 6px;
            background: color-mix(in srgb, var(--secondary-background-color) 72%, transparent);
        }
        [data-testid="stMetricLabel"] p {
            font-size: 0.78rem !important;
            color: color-mix(in srgb, var(--text-color) 68%, transparent);
        }
        [data-testid="stMetricValue"] {
            font-size: 1.35rem !important;
            line-height: 1.25 !important;
        }
        [data-testid="stMetricDelta"] { font-size: 0.76rem !important; }
        [data-testid="stVerticalBlockBorderWrapper"] {
            border-radius: 6px !important;
        }
        .stButton > button, [data-testid="stBaseButton-secondary"],
        [data-testid="stBaseButton-primary"] {
            min-height: 2.35rem;
            border-radius: 6px !important;
        }
        [data-baseweb="tab-list"] { gap: 0.35rem; }
        [data-baseweb="tab"] { border-radius: 4px !important; }
        [data-testid="stDataFrame"] { font-size: 0.84rem; }
        hr { margin: 1.25rem 0 !important; }
        @media (max-width: 760px) {
            .stMainBlockContainer { padding: 0.8rem 0.7rem 2rem; }
            h1 { font-size: 1.5rem !important; }
            [data-testid="stMetric"] { min-height: 5.9rem; padding: 0.65rem 0.7rem; }
            [data-testid="stMetricValue"] { font-size: 1.12rem !important; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


# ── shared resources (one instance, reused across reruns) ──
@st.cache_resource
def get_cfg():
    return load("config.yaml")


@st.cache_resource
def get_con():
    # The sentiment page refreshes live news and writes the latest AI summary.
    # Keep one app-owned read/write connection so DuckDB does not reject a
    # second connection with a different read_only configuration.
    # 刷新窗口(15:15/20:00 cron 跑 `cli daily` 时会独占写锁)里, DuckDB 会拒绝本连接 →
    # 别让整个看板抛栈, 退避重试几次, 仍锁住就友好提示并停渲染(而非红色 Traceback)。
    import time as _time
    cfg = get_cfg()
    path = resolve_path(cfg, "warehouse")
    for attempt in range(4):
        try:
            return open_db(path, read_only=False)
        except Exception as e:  # noqa: BLE001
            if "lock" not in str(e).lower():
                raise
            if attempt < 3:
                _time.sleep(1.0)
                continue
            st.warning("📊 数据正在更新中（每天 15:15 收盘后、20:00 各刷新一次，约几分钟）——"
                       "请稍候再点左上「🔄 刷新数据」或刷新页面。")
            st.stop()


def _reset_db_connection():
    """Drop cached DB/data handles so the next rerun sees the latest DuckDB file."""
    try:
        get_con().close()
    except Exception:  # noqa: BLE001
        pass
    get_con.clear()
    st.cache_data.clear()


@st.cache_resource
def get_ak():
    return AkSource()


# ── data (re-fetched at most every 5 min unless 🔄 pressed) ──
@st.cache_data(ttl=300, show_spinner="拉取行情/净值/信号中…")
def load_bundle():
    con, ak = get_con(), get_ak()
    pf = load_positions()
    if pf is not None and pf.holdings:
        pf = value_portfolio(pf, ak_src=ak, con=con)
        health = score_portfolio(pf, con=con, ak_src=ak)
        advice = generate_advice(pf, health=health, timing=index_timing(con))
    else:
        health, advice = {}, []
    timing = index_timing(con)
    history = load_history(con)
    return pf, health, advice, timing, history


@st.cache_data(ttl=1800, show_spinner="全市场主板打分中(首次较慢)…")
def load_recommendations(top_n: int = 12, style: str = "momentum"):
    # read-only: ak_src=None so no valuation writes (dashboard holds a RO lock)
    con = get_con()
    stocks, uni = recommend_stocks(con, ak_src=None, top_n=top_n, style=style)
    return stocks, uni


@st.cache_data(ttl=1800, show_spinner="指数基金打分中…")
def load_index_fund_recommendations(top_n: int = 9,
                                    held_codes: tuple[str, ...] = (),
                                    extra_items: tuple[tuple[str, str, str], ...] = ()):
    con = get_con()
    extra = {code: (name, kind) for code, name, kind in extra_items}
    return recommend_index_funds(con, top_n=top_n, held=set(held_codes), extra_funds=extra)


@st.cache_data(ttl=60, show_spinner=False)
def load_live_quotes(codes: tuple[str, ...]) -> dict:
    """Live current price for a handful of codes (Tencent, ~1 call). Scores/levels
    are computed from daily bars (as-of last close); this gives the true 现价 so the
    table isn't showing a stale close. Empty on failure → caller falls back to close."""
    if not codes:
        return {}
    from ashare.sources import TencentSource
    df = TencentSource().spot(list(codes))
    if df.empty:
        return {}
    return {r["symbol"]: {"price": r["price"], "pct": r["pct_chg"]}
            for _, r in df.iterrows()}


@st.cache_data(ttl=300, show_spinner=False)
def bars_as_of() -> object:
    """Latest trade_date in daily_bar — used to warn when the warehouse is stale."""
    return get_con().execute("SELECT max(trade_date) FROM daily_bar").fetchone()[0]


def _news_for_day(con, day: date | object | None = None) -> pd.DataFrame:
    """Latest ingested news for display. Returns an empty DataFrame if none."""
    if day is None:
        day = con.execute("SELECT max(news_date) FROM news_raw").fetchone()[0]
    if day is None:
        return pd.DataFrame(columns=["source", "title", "summary", "ts", "url"])
    return con.execute(
        "SELECT source, title, summary, ts, url FROM news_raw WHERE news_date=? "
        "ORDER BY ts DESC NULLS LAST", [day]).df()


def _source_counts(news: pd.DataFrame) -> dict[str, int]:
    if news is None or news.empty:
        return {}
    return {str(k): int(v) for k, v in news.groupby("source").size().items()}


def _text_of_news(r) -> str:
    return f"{getattr(r, 'title', '')} {getattr(r, 'summary', '')}"


def _keyword_hits(text: str, words) -> list[str]:
    return [w for w in words if w and str(w) in text]


def _holding_watch_terms() -> list[dict]:
    """Terms derived from positions.yaml. Used only for local highlighting."""
    pf = load_positions()
    if pf is None:
        return []
    terms = []
    for h in pf.holdings:
        candidates = [h.code, h.name]
        for part in re.split(r"[（）()·\s/_-]+", h.name or ""):
            part = part.strip()
            if len(part) >= 2:
                candidates.append(part)
        clean = []
        for item in candidates:
            item = str(item or "").strip()
            if item and item not in clean and item not in {"ETF", "联接", "指数", "股票", "债券"}:
                clean.append(item)
        terms.append({"code": h.code, "name": h.name, "terms": clean})
    return terms


def _classify_news_row(r, holding_terms: list[dict] | None = None) -> dict:
    """Cheap local signal: topic/risk/sentiment/holding hits before LLM."""
    text = _text_of_news(r)
    risk_reasons = []
    risk_level = 0
    for label, weight, words in ALERT_RULES:
        hits = _keyword_hits(text, words)
        if hits:
            risk_reasons.append(f"{label}: " + "、".join(hits[:3]))
            risk_level = max(risk_level, weight)
    if len(risk_reasons) >= 2:
        risk_level = min(3, risk_level + 1)

    bullish = _keyword_hits(text, LOCAL_BULLISH_WORDS)
    bearish = _keyword_hits(text, LOCAL_BEARISH_WORDS)
    local_score = max(-5, min(5, len(bullish) - len(bearish)))
    if risk_level >= 3:
        local_score = min(local_score, -2)
    polarity = "偏多" if local_score > 0 else "偏空" if local_score < 0 else "中性"

    themes = []
    for label, words in THEME_KEYWORDS.items():
        if _keyword_hits(text, words):
            themes.append(label)

    holding_hits = []
    for h in holding_terms or []:
        matched = _keyword_hits(text, h["terms"])
        if matched:
            holding_hits.append({
                "code": h["code"],
                "name": h["name"],
                "matched": matched[:4],
            })

    return {
        "risk_level": risk_level,
        "risk_reasons": risk_reasons,
        "bullish_words": bullish[:5],
        "bearish_words": bearish[:5],
        "local_score": local_score,
        "polarity": polarity,
        "themes": themes[:4],
        "holding_hits": holding_hits,
    }


def _enrich_news(news: pd.DataFrame) -> list[dict]:
    if news is None or news.empty:
        return []
    holdings = _holding_watch_terms()
    enriched = []
    for r in news.itertuples():
        info = _classify_news_row(r, holdings)
        info["row"] = r
        enriched.append(info)
    enriched.sort(
        key=lambda x: (
            bool(x["holding_hits"]),
            x["risk_level"],
            abs(x["local_score"]),
            pd.Timestamp(getattr(x["row"], "ts", pd.NaT)).timestamp()
            if pd.notna(getattr(x["row"], "ts", pd.NaT)) else 0,
        ),
        reverse=True,
    )
    return enriched


def _llm_runtime_config() -> dict:
    cfg = get_cfg().get("sentiment", {}) or {}
    base_url = str(cfg.get("base_url") or "").strip()
    key_source = "未配置"
    if os.environ.get("LLM_API_KEY"):
        key_source = "LLM_API_KEY"
    elif os.environ.get("OPENAI_API_KEY"):
        key_source = "OPENAI_API_KEY"
    elif os.environ.get("ANTHROPIC_API_KEY"):
        key_source = "ANTHROPIC_API_KEY"
    elif (pathlib.Path(__file__).resolve().parents[3] / ".anthropic_key").exists():
        key_source = ".anthropic_key"
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "model": str(cfg.get("model") or "claude-haiku-4-5-20251001"),
        "base_url": base_url or "OpenAI SDK 默认",
        "base_host": urlparse(base_url).netloc if base_url else "default",
        "max_headlines": int(cfg.get("max_headlines", 80)),
        "key_source": key_source,
    }


def _render_llm_config_card():
    c = _llm_runtime_config()
    last = st.session_state.get("last_llm_refresh") or {}
    cols = st.columns(4)
    cols[0].metric("LLM", "启用" if c["enabled"] else "关闭")
    cols[1].metric("模型", c["model"])
    cols[2].metric("端点", c["base_host"])
    cols[3].metric("输入上限", f"{c['max_headlines']} 条")
    st.caption(f"Base URL: `{c['base_url']}` · 密钥来源: {c['key_source']}")
    if last:
        status = last.get("status", "unknown")
        elapsed = last.get("elapsed_sec")
        suffix = f" · 耗时 {elapsed:.1f}s" if isinstance(elapsed, (int, float)) else ""
        if status == "ok":
            st.success(f"最近一次 AI 总结成功: {last.get('finished_at', '')}{suffix}")
        elif status == "skipped":
            st.info(f"最近一次 AI 总结跳过: {last.get('reason', '')}")
        else:
            st.warning(f"最近一次 AI 总结失败: {last.get('error', 'unknown')}{suffix}")


def _news_data_quality(news: pd.DataFrame, sent: dict | None) -> list[str]:
    warnings = []
    if news is None or news.empty:
        warnings.append("今日新闻源为空")
        return warnings
    latest_ts = None
    if "ts" in news:
        ts = pd.to_datetime(news["ts"], errors="coerce").dropna()
        if not ts.empty:
            latest_ts = ts.max().to_pydatetime()
    if latest_ts is None:
        warnings.append("新闻缺少发布时间，只能按入库日期判断")
    elif datetime.now() - latest_ts > timedelta(hours=3):
        warnings.append(f"最新快讯时间 {latest_ts:%H:%M}，可能不是实时流")
    if len(news) < 10:
        warnings.append(f"今日新闻仅 {len(news)} 条，AI 总结样本偏少")
    if sent:
        as_of = str(sent.get("as_of_date"))[:10]
        today = date.today().isoformat()
        if as_of < today:
            warnings.append(f"AI 总结停留在 {as_of}")
        elif as_of == today:
            try:
                if int(sent.get("n_news") or 0) > len(news):
                    warnings.append("AI 总结样本数大于当前新闻流，可能来自更早缓存")
            except Exception:
                pass
    else:
        warnings.append("暂无 AI 总结")
    return warnings


def _format_news_title(r) -> str:
    ts = ""
    if pd.notna(getattr(r, "ts", None)):
        try:
            ts = pd.to_datetime(getattr(r, "ts")).strftime("%H:%M")
        except Exception:  # noqa: BLE001
            ts = ""
    title = str(getattr(r, "title", ""))
    if getattr(r, "url", None):
        title = f"[{title}]({getattr(r, 'url')})"
    return f"{ts + ' · ' if ts else ''}{title}"


def _news_key(r) -> str:
    return f"{getattr(r, 'source', '')}|{getattr(r, 'title', '')}"


def _alert_for_news(r) -> dict | None:
    text = f"{getattr(r, 'title', '')} {getattr(r, 'summary', '')}"
    hits = []
    severity = 0
    for label, weight, words in ALERT_RULES:
        matched = [w for w in words if w in text]
        if matched:
            hits.append(f"{label}: " + "、".join(matched[:3]))
            severity = max(severity, weight)
    if not hits:
        return None
    if len(hits) >= 2:
        severity = min(3, severity + 1)
    return {
        "severity": severity,
        "title": str(getattr(r, "title", "")),
        "source": str(getattr(r, "source", "")),
        "ts": getattr(r, "ts", None),
        "url": getattr(r, "url", None),
        "reasons": hits,
    }


def _monitor_news_once(force: bool = False, reset: bool = False) -> dict:
    """Poll news sources once and flag newly observed breaking-risk headlines."""
    con, ak = get_con(), get_ak()
    init_schema(con)
    now = datetime.now()
    if reset:
        st.session_state["news_monitor_seen"] = set()
        st.session_state["news_monitor_ready"] = False

    try:
        inserted = ingest_news(con, ak, date.today())
        error = None
    except Exception as e:  # noqa: BLE001
        inserted, error = 0, str(e)

    news = _news_for_day(con, date.today())
    keys = {_news_key(r) for r in news.itertuples()} if not news.empty else set()
    seen = st.session_state.get("news_monitor_seen")
    ready = bool(st.session_state.get("news_monitor_ready", False))

    if seen is None or not ready:
        new_rows = []
        st.session_state["news_monitor_ready"] = True
    else:
        new_rows = [r for r in news.itertuples() if _news_key(r) not in seen]

    st.session_state["news_monitor_seen"] = keys
    st.session_state["news_monitor_last_check"] = now

    alerts = []
    for r in new_rows:
        a = _alert_for_news(r)
        if a:
            alerts.append(a)
    alerts.sort(key=lambda x: x["severity"], reverse=True)
    enriched = _enrich_news(news)
    high_risk = [x for x in enriched if x["risk_level"] >= 2]
    holding_hits = [x for x in enriched if x["holding_hits"]]

    return {
        "at": now,
        "inserted": inserted,
        "error": error,
        "news": news,
        "new_rows": new_rows,
        "alerts": alerts,
        "high_risk": high_risk,
        "holding_hits": holding_hits,
        "enriched": enriched,
        "source_counts": _source_counts(news),
        "force": force,
    }


def _refresh_sentiment_now(force: bool = False) -> dict:
    """Refresh live news + today's AI sentiment summary for the sentiment page."""
    now = datetime.now()
    if not force:
        last = st.session_state.get("sentiment_refresh_at")
        if last and now - last < timedelta(minutes=5):
            return {"skipped": "recent", "at": last}

    con, ak, cfg = get_con(), get_ak(), get_cfg()
    init_schema(con)
    llm_cfg = _llm_runtime_config()
    started = datetime.now()
    try:
        row = generate_sentiment(con, ak, cfg.get("sentiment", {}), as_of=date.today())
    except Exception as e:  # noqa: BLE001
        finished = datetime.now()
        st.session_state["sentiment_refresh_at"] = finished
        st.session_state["last_llm_refresh"] = {
            "status": "error",
            "error": str(e),
            "model": llm_cfg["model"],
            "base_url": llm_cfg["base_url"],
            "started_at": started.strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": finished.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_sec": (finished - started).total_seconds(),
        }
        return {"error": str(e), "at": finished}

    finished = datetime.now()
    st.session_state["sentiment_refresh_at"] = finished
    latest = latest_sentiment(con)
    news = _news_for_day(con, date.today())
    if row is None:
        st.session_state["last_llm_refresh"] = {
            "status": "skipped",
            "reason": "无新闻、未配置密钥、模型调用失败或输出无法解析",
            "model": llm_cfg["model"],
            "base_url": llm_cfg["base_url"],
            "news_count": int(len(news)),
            "started_at": started.strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": finished.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_sec": (finished - started).total_seconds(),
        }
    else:
        st.session_state["last_llm_refresh"] = {
            "status": "ok",
            "model": llm_cfg["model"],
            "base_url": llm_cfg["base_url"],
            "news_count": int(len(news)),
            "started_at": started.strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": finished.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_sec": (finished - started).total_seconds(),
        }
    return {
        "row": row,
        "latest": latest,
        "news_count": int(len(news)),
        "source_counts": _source_counts(news),
        "at": finished,
    }


def _ai_summary_needs_refresh(news: pd.DataFrame, sent: dict | None, cfg: dict) -> tuple[bool, str]:
    if news is None or news.empty:
        return False, "无今日新闻"
    if not sent:
        return True, "今日暂无 AI 总结"
    today = date.today().isoformat()
    as_of = str(sent.get("as_of_date"))[:10]
    if as_of != today:
        return True, f"AI 总结停留在 {as_of}"
    max_h = int((cfg.get("sentiment", {}) or {}).get("max_headlines", 80))
    target_n = min(len(news), max_h)
    try:
        current_n = int(sent.get("n_news") or 0)
    except Exception:
        current_n = 0
    if target_n - current_n >= AI_NEWS_DELTA_THRESHOLD:
        return True, f"新闻样本已从 {current_n} 增至可用 {target_n} 条"
    return False, "AI 总结仍在有效样本范围内"


def _refresh_page_inputs(force: bool = False, force_ai: bool = False) -> dict:
    """Refresh data that the sentiment page depends on before rendering."""
    con, ak, cfg = get_con(), get_ak(), get_cfg()
    init_schema(con)
    now = datetime.now()
    info: dict = {"news_rows": None, "market_rows": None, "ai": None}
    last = st.session_state.get("page_inputs_refresh_at")
    should_refresh_inputs = force or not last or now - last >= PAGE_REFRESH_INTERVAL

    if should_refresh_inputs:
        try:
            info["news_rows"] = ingest_news(con, ak, date.today())
        except Exception as e:  # noqa: BLE001
            info["news_error"] = str(e)
        try:
            info["market_rows"] = ingest_market_breadth(con)
        except Exception as e:  # noqa: BLE001
            info["market_error"] = str(e)
        st.session_state["page_inputs_refresh_at"] = now

    news = _news_for_day(con, date.today())
    sent = latest_sentiment(con)
    needs_ai, reason = _ai_summary_needs_refresh(news, sent, cfg)
    last_ai = st.session_state.get("sentiment_auto_refresh_at")
    ai_cooldown_ok = not last_ai or now - last_ai >= AI_AUTO_REFRESH_INTERVAL
    if force_ai or (needs_ai and ai_cooldown_ok):
        refresh = _refresh_sentiment_now(force=True)
        st.session_state["sentiment_auto_refresh_at"] = datetime.now()
        info["ai"] = refresh
        info["ai_reason"] = reason
    else:
        info["ai_reason"] = reason
    return info


@st.cache_data(ttl=300)
def get_fund_sparkline(code: str) -> pd.DataFrame:
    df = get_con().execute(
        "SELECT trade_date, close FROM daily_bar WHERE symbol=? AND type='etf' "
        "ORDER BY trade_date DESC LIMIT 90", [code.zfill(6)]).df()
    if df is None or df.empty:
        df = get_con().execute(
            "SELECT nav_date AS trade_date, nav AS close FROM fund_nav WHERE symbol=? "
            "ORDER BY nav_date DESC LIMIT 90", [code.zfill(6)]).df()
    if df is None or df.empty:
        return pd.DataFrame()
    return df.sort_values("trade_date")


@st.cache_data(ttl=300)
def get_series(code: str, ctype: str) -> pd.DataFrame:
    """OHLC for stocks/index (from DB), NAV line for funds (from akshare)."""
    con, ak = get_con(), get_ak()
    if ctype in ("stock", "etf"):
        df = con.execute(
            "SELECT trade_date, open, high, low, close, volume FROM daily_bar "
            "WHERE symbol=? ORDER BY trade_date", [code.zfill(6)]).df()
        df["kind"] = "ohlc"
        return df
    if ctype in ("otc_fund", "bond_fund"):
        nav = nav_history(con, code)            # warehouse first
        if nav is None or nav.empty:
            nav = ak.fund_nav_history(code)     # live fallback if not stored
        if nav is None or nav.empty:
            return pd.DataFrame()
        df = nav.rename(columns={"nav_date": "trade_date", "nav": "close"})[["trade_date", "close"]].copy()
        df["kind"] = "line"
        return df
    # index fallback
    df = con.execute(
        "SELECT trade_date, open, high, low, close, volume FROM index_bar "
        "WHERE symbol=? ORDER BY trade_date", [code.zfill(6)]).df()
    df["kind"] = "ohlc"
    return df


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    gain = d.clip(lower=0).rolling(n).mean()
    loss = (-d.clip(upper=0)).rolling(n).mean()
    rs = gain / loss.replace(0, pd.NA)
    return 100 - 100 / (1 + rs)


def price_chart(name: str, df: pd.DataFrame, lookback: int) -> go.Figure:
    df = df.tail(lookback).copy()
    has_vol = "volume" in df and df["volume"].notna().any()
    rows = 3 if df["kind"].iloc[0] == "ohlc" else 2
    heights = [0.6, 0.2, 0.2] if rows == 3 else [0.72, 0.28]
    fig = make_subplots(rows=rows, cols=1, shared_xaxes=True,
                        vertical_spacing=0.03, row_heights=heights)
    x = df["trade_date"]

    if df["kind"].iloc[0] == "ohlc":
        fig.add_trace(go.Candlestick(
            x=x, open=df["open"], high=df["high"], low=df["low"], close=df["close"],
            name="K线", increasing_line_color=UP, decreasing_line_color=DN,
            increasing_fillcolor=UP, decreasing_fillcolor=DN), row=1, col=1)
    else:
        fig.add_trace(go.Scatter(x=x, y=df["close"], name="单位净值",
                                 line=dict(color="#2b6cb0", width=1.6)), row=1, col=1)

    for w, c in ((20, "#ff9f43"), (60, "#5f27cd"), (200, "#8395a7")):
        if len(df) >= w:
            fig.add_trace(go.Scatter(x=x, y=df["close"].rolling(w).mean(),
                          name=f"MA{w}", line=dict(width=1)), row=1, col=1)

    # volume
    vrow = 2
    if df["kind"].iloc[0] == "ohlc" and has_vol:
        colors = [UP if c >= o else DN for o, c in zip(df["open"], df["close"])]
        fig.add_trace(go.Bar(x=x, y=df["volume"], marker_color=colors,
                      name="成交量", showlegend=False), row=vrow, col=1)
        vrow = 3

    # RSI
    rsi = _rsi(df["close"])
    fig.add_trace(go.Scatter(x=x, y=rsi, name="RSI14",
                  line=dict(color="#9b59b6", width=1)), row=vrow, col=1)
    fig.add_hline(y=70, line=dict(color="#aaa", dash="dot"), row=vrow, col=1)
    fig.add_hline(y=30, line=dict(color="#aaa", dash="dot"), row=vrow, col=1)

    fig.update_layout(
        title=name, height=620, margin=dict(l=10, r=10, t=40, b=10),
        xaxis_rangeslider_visible=False, legend=dict(orientation="h", y=1.02),
        dragmode="pan", hovermode="x unified")
    fig.update_yaxes(title_text="RSI", range=[0, 100], row=vrow, col=1)
    return fig


def add_price_levels(fig: go.Figure, lv) -> go.Figure:
    """Draw buy zone (green band) / stop (red) / target (blue) on the price pane."""
    if lv is None:
        return fig
    fig.add_hrect(y0=lv.buy_lo, y1=lv.buy_hi, line_width=0,
                  fillcolor="rgba(26,174,92,0.16)", row=1, col=1)
    fig.add_hline(y=lv.buy_hi, line=dict(color="#1aae5c", width=1, dash="dot"),
                  annotation_text=f"买入区上沿 {lv.buy_hi}", annotation_position="right",
                  row=1, col=1)
    fig.add_hline(y=lv.buy_lo, line=dict(color="#1aae5c", width=1, dash="dot"),
                  annotation_text=f"买入区下沿 {lv.buy_lo}", annotation_position="right",
                  row=1, col=1)
    fig.add_hline(y=lv.stop, line=dict(color=UP, width=1.3, dash="dash"),
                  annotation_text=f"止损 {lv.stop}", annotation_position="right", row=1, col=1)
    fig.add_hline(y=lv.target, line=dict(color="#2b6cb0", width=1.3, dash="dash"),
                  annotation_text=f"止盈 {lv.target}", annotation_position="right", row=1, col=1)
    return fig


def score_waterfall(sig) -> go.Figure:
    """Waterfall: how each factor's contribution sums to the final score."""
    comps = sig.components
    x = [c.label for c in comps] + ["= 综合健康分"]
    y = [round(c.contribution, 3) for c in comps] + [round(sig.score, 3)]
    measure = ["relative"] * len(comps) + ["total"]
    text = [f"{c.contribution:+.2f}" for c in comps] + [f"{sig.score:+.2f}"]
    fig = go.Figure(go.Waterfall(
        orientation="v", measure=measure, x=x, y=y, text=text,
        textposition="outside", connector=dict(line=dict(color="#ccc")),
        increasing=dict(marker=dict(color=UP)),
        decreasing=dict(marker=dict(color=DN)),
        totals=dict(marker=dict(color="#2b6cb0"))))
    fig.update_layout(height=320, margin=dict(t=36, b=10, l=10, r=10),
                      title="健康分拆解 · 各因子贡献", yaxis_title="对 score 的贡献",
                      showlegend=False)
    return fig


def render_breakdown(sig):
    """Waterfall + per-factor evidence table — the 'how it was computed' panel."""
    if not sig or not sig.components:
        st.caption("无打分明细。")
        return
    left, right = st.columns([1, 1])
    left.plotly_chart(score_waterfall(sig), width="stretch")
    rows = [{"因子": c.label, "贡献": round(c.contribution, 3),
             "影响上限": c.cap, "依据(实际数值→解读)": c.detail}
            for c in sig.components]
    df = pd.DataFrame(rows)
    right.markdown(f"**综合健康分 = {sig.score:+.2f}** ({sig.label}) · "
                   f"置信度 {sig.confidence:.0%}（样本 {sig.metadata.get('n_obs','?')} 日）")
    right.dataframe(df.style.map(_color, subset=["贡献"]),
                    width="stretch", hide_index=True)
    right.caption("正贡献=偏多动力, 负贡献=偏空动力；各因子相加(裁剪到±1)即综合分。")


def _color(v):
    if v is None or pd.isna(v):
        return "color:#888"
    return f"color:{UP}" if v > 0 else f"color:{DN}" if v < 0 else "color:#888"


def _safe_float(v, default: float = 0.0) -> float:
    try:
        return default if v is None or pd.isna(v) else float(v)
    except (TypeError, ValueError):
        return default


def _pct_text(v) -> str:
    if v is None or pd.isna(v):
        return "—"
    return f"{float(v):+.2f}%"


def _sparkline_fig(df: pd.DataFrame, color: str) -> go.Figure:
    fig = go.Figure()
    if df is not None and not df.empty:
        fig.add_trace(go.Scatter(
            x=df["trade_date"], y=df["close"], mode="lines",
            line=dict(color=color, width=2), hoverinfo="skip"))
    fig.update_layout(height=82, margin=dict(l=0, r=0, t=4, b=0),
                      xaxis=dict(visible=False), yaxis=dict(visible=False),
                      showlegend=False, plot_bgcolor="white", paper_bgcolor="white")
    return fig


def render_index_fund_cards(funds, desc: str):
    st.markdown("#### 指数基金买卖观察")
    st.caption(f"{desc} · 策略分=趋势/动量/RSI/估值分位综合, 规则提示非投资指令")
    if not funds:
        st.info("暂无可评分的指数基金 — 先跑 `daily --universe all` 或补齐 ETF / 场外基金净值。")
        return

    live = load_live_quotes(tuple(s.target for s in funds if s.metadata.get("type") == "etf"))
    for start in range(0, len(funds), 3):
        cols = st.columns(3)
        for col, s in zip(cols, funds[start:start + 3]):
            m = s.metadata
            q = live.get(s.target)
            price = q["price"] if q else m.get("close")
            pct = q["pct"] if q else m.get("today_pct")
            price_txt = "n/a" if price is None or pd.isna(price) else f"{float(price):.3f}"
            tone = UP if s.score >= 0 else DN
            with col.container(border=True):
                top = st.columns([0.68, 0.32])
                top[0].markdown(f"**{m.get('name', s.target)}**")
                top[0].caption(f"{s.target} · {m.get('theme', '指数基金')}")
                top[1].markdown(f"**{m.get('posture')}**")
                top[1].caption(f"策略分 {m.get('strategy_score')}")

                st.metric("现价/净值", price_txt, _pct_text(pct))
                spark = get_fund_sparkline(s.target)
                st.plotly_chart(_sparkline_fig(spark, tone), width="stretch",
                                config={"displayModeBar": False})

                k = st.columns(3)
                k[0].metric("日涨跌", _pct_text(pct), delta_color="off")
                k[1].metric("周涨跌", _pct_text(m.get("week_pct")), delta_color="off")
                k[2].metric("月涨跌", _pct_text(m.get("month_pct")), delta_color="off")

                st.markdown(f"**{m.get('action')}**")
                for b in m.get("bullets", [])[:4]:
                    st.caption("• " + b)


# ════════════════════════════ layout ════════════════════════════
_apply_dashboard_style()

pf, health, advice, timing, history = None, {}, [], None, pd.DataFrame()
adv_by_code, label_to_holding = {}, {}


def _sentiment_card():
    """今日舆情卡片(读预生成的 sentiment_daily, 不在UI调LLM/不需密钥)。"""
    sent = latest_sentiment(get_con())
    if not sent:
        st.caption("📰 今日舆情: 暂无 — 配置 ANTHROPIC 密钥(.anthropic_key)后跑 "
                   "`ashare sentiment` 或 `ashare daily` 生成。")
        return
    score = float(sent["score"])
    as_of = str(sent["as_of_date"])[:10]
    arrow = "🔺" if score > 0.1 else "🔻" if score < -0.1 else "➖"
    with st.container(border=True):
        cols = st.columns([0.2, 0.8])
        cols[0].metric(f"今日舆情 {arrow} {sent['label']}", f"{score:+.2f}",
                       help=f"AI生成 · 基于 {int(sent['n_news'])} 条新闻 · {as_of}")
        with cols[1]:
            st.markdown(f"**{sent['summary']}**")
            st.caption(f"🔺 利好: {sent.get('bullish') or '—'}")
            st.caption(f"🔻 利空: {sent.get('bearish') or '—'}")
            st.caption(f"{sent.get('model','')} 解读 · 仅供参考非投资建议 · 数据截至 {as_of}")


def page_overview():
    st.subheader("📊 持仓总览")
    _sentiment_card()
    c1, c2, c3, c4 = st.columns(4)
    tot = pf.total_market_value
    c1.metric("总资产", f"{tot/1e4:.2f} 万", help=f"{tot:,.0f} 元")
    if pf.total_cost_value:
        pnl = pf.total_pnl or 0
        c2.metric("累计浮盈", f"{pnl:,.0f} 元", f"{pnl/pf.total_cost_value*100:+.2f}%")
    if pf.total_today_pnl is not None:
        tp = pf.total_today_pnl
        c3.metric("今日盈亏", f"{tp:,.0f} 元", f"{tp/tot*100:+.2f}%")
    c4.metric("持仓数", f"{len(pf.valued)} 项")

    cc = st.columns([1, 1])
    # allocation pie
    cats = pf.by_category()
    pie = go.Figure(go.Pie(labels=list(cats), values=list(cats.values()), hole=0.45,
                           textinfo="label+percent"))
    pie.update_layout(title="资产配置", height=340, margin=dict(t=40, b=10, l=10, r=10),
                      showlegend=False)
    cc[0].plotly_chart(pie, width="stretch")

    # equity curve
    if history is not None and len(history) >= 2:
        h = history.sort_values("snapshot_date")
        eq = go.Figure(go.Scatter(x=h["snapshot_date"], y=h["total_market_value"],
                       fill="tozeroy", line=dict(color="#2b6cb0")))
        mv = h.set_index("snapshot_date")["total_market_value"].astype(float)
        mdd, pk, tr = max_drawdown(mv)
        eq.update_layout(title=f"总资产曲线 (最大回撤 {mdd:.2f}%)", height=340,
                         margin=dict(t=40, b=10, l=10, r=10))
        cc[1].plotly_chart(eq, width="stretch")
    else:
        cc[1].info("收益曲线需 ≥2 天快照。每天跑一次 `pf` 或开着看板即可积累。")

    # holdings table
    rows = []
    for h in sorted(pf.valued, key=lambda x: x.market_value or 0, reverse=True):
        sig = health.get(h.code)
        rows.append({
            "代码": h.code, "名称": h.name,
            "市值(元)": round(h.market_value or 0),
            "权重%": round(pf.weight(h) or 0, 1),
            "今日%": round(h.today_pct, 2) if h.today_pct is not None else None,
            "浮盈%": round(h.pnl_pct, 1) if h.pnl_pct is not None else None,
            "健康分": round(sig.score, 2) if sig else None,
            "评级": sig.label if sig else "",
            "定价": f"{h.price_kind}{str(h.price_date)[5:]}" if h.price_kind else "",
        })
    df = pd.DataFrame(rows)
    st.dataframe(
        df.style.map(_color, subset=["今日%", "浮盈%", "健康分"]),
        width="stretch", hide_index=True)


def page_advice():
    st.subheader("💡 操作建议")
    st.caption("规则提示, 非投资指令 · 风控>选股>择时 · 阈值: 止盈≥+30% 止损≤-15% 单一>25% 强弱±0.35")
    if timing is not None:
        st.info(f"**大盘择时 [{timing.label}]** (score {timing.score:+.2f}) — {timing.reason}")
        with st.expander("🔍 大盘择时是怎么算出来的"):
            render_breakdown(timing)
    icon = {3: "🟥", 2: "🟧", 1: "⬜"}
    for a in advice:
        with st.container(border=True):
            cols = st.columns([0.1, 0.2, 0.7])
            cols[0].markdown(f"### {icon.get(a.urgency, '⬜')}")
            cols[1].markdown(f"**{a.action}**\n\n{a.name}")
            score = f" · 健康分 {a.score:+.2f}" if a.score is not None else ""
            cols[2].markdown(f"{a.reason}{score}")


def page_risk():
    st.subheader("🛡 组合风控 · 整体视角")
    st.caption("风控>选股>择时 · 真实分散度(相关性)/集中度/市场暴露/压力测试 + 再平衡(风险贡献/ERC) · 历史统计, 非未来保证")
    # 用独立 cursor: 风控要对每只持仓逐一查历史(一串密集查询), 若复用共享连接会与
    # Streamlit 其它线程/重跑在同一 DuckDB 连接上串台(结果错位→KeyError)。cursor() 是
    # 官方推荐的"同库多连接"线程安全做法。
    con = get_con().cursor()
    rep = portfolio_risk(pf, con)
    plan = rebalance_plan(pf, con)

    # 关键洞察可视化: 名义权重 ≠ 真实风险贡献
    if plan.ok and plan.rows:
        names = [x["name"][:8] for x in plan.rows]
        fig = go.Figure()
        fig.add_trace(go.Bar(x=names, y=[x["weight"] for x in plan.rows],
                             name="权重%", marker_color="#8395a7"))
        fig.add_trace(go.Bar(x=names, y=[x["rc"] for x in plan.rows],
                             name="风险贡献%", marker_color=UP))
        fig.add_trace(go.Bar(x=names, y=[x["erc"] for x in plan.rows],
                             name="等风险(ERC)目标%", marker_color="#2b6cb0"))
        fig.update_layout(barmode="group", height=360,
                          title="权重 vs 风险贡献 vs 等风险目标 (谁在吃掉你的波动)",
                          margin=dict(t=40, b=10, l=10, r=10),
                          legend=dict(orientation="h", y=1.02))
        st.plotly_chart(fig, width="stretch")

    c1, c2 = st.columns(2)
    c1.code(render_risk(rep), language=None)
    c2.code(render_rebalance(plan), language=None)


def _render_news_quality_panel(news: pd.DataFrame, sent: dict | None):
    warnings = _news_data_quality(news, sent)
    counts = _source_counts(news)
    cols = st.columns(4)
    cols[0].metric("今日新闻", 0 if news is None else len(news))
    cols[1].metric("新闻源", len(counts))
    latest = "—"
    if news is not None and not news.empty and "ts" in news:
        ts = pd.to_datetime(news["ts"], errors="coerce").dropna()
        if not ts.empty:
            latest = ts.max().strftime("%H:%M")
    cols[2].metric("最新快讯", latest)
    cols[3].metric("数据质量", "注意" if warnings else "正常")
    detail = " · ".join(f"{k}:{v}" for k, v in counts.items()) or "暂无新闻源计数"
    st.caption(detail)
    if warnings:
        st.warning("；".join(warnings))


def _local_news_stats(news: pd.DataFrame) -> dict:
    enriched = _enrich_news(news)
    high = [x for x in enriched if x["risk_level"] >= 2]
    holding = [x for x in enriched if x["holding_hits"]]
    bullish = [x for x in enriched if x["local_score"] > 0]
    bearish = [x for x in enriched if x["local_score"] < 0]
    themes = {}
    for item in enriched:
        for t in item["themes"]:
            themes[t] = themes.get(t, 0) + 1
    top_themes = sorted(themes.items(), key=lambda x: x[1], reverse=True)
    return {
        "enriched": enriched,
        "high": high,
        "holding": holding,
        "bullish": bullish,
        "bearish": bearish,
        "themes": themes,
        "top_themes": top_themes,
    }


def _market_news_alignment(sent: dict | None, market: dict | None) -> str | None:
    if not sent or not market:
        return None
    try:
        score = float(sent["score"])
        ratio = float(market.get("up_down_ratio") or 0)
    except (TypeError, ValueError):
        return None
    if score > 0.1 and ratio < 1:
        return "AI 偏多，但市场宽度偏弱"
    if score < -0.1 and ratio > 1:
        return "AI 偏空，但市场宽度偏强"
    if score > 0.1 and ratio >= 1:
        return "AI 偏多，市场宽度配合"
    if score < -0.1 and ratio <= 1:
        return "AI 偏空，市场宽度配合"
    return "AI 中性，观察市场宽度变化"


INDEX_NAMES = {
    "000001": "上证指数",
    "399001": "深证成指",
    "399006": "创业板指",
    "000300": "沪深300",
    "000905": "中证500",
    "000852": "中证1000",
    "000688": "科创50",
    "899050": "北证50",
}


@st.cache_data(ttl=60, show_spinner=False)
def _live_major_indices(codes: tuple[str, ...]) -> pd.DataFrame:
    """Live index quotes are provisional and never written into settled bars."""
    if not codes:
        return pd.DataFrame()
    try:
        from ashare.sources import TencentSource

        return TencentSource().spot(list(codes), index_symbols=set(codes))
    except Exception:  # noqa: BLE001 - local close data remains available
        return pd.DataFrame()


def _latest_index_snapshot(con) -> pd.DataFrame:
    """Prefer live major-index quotes, filling gaps with local settled closes."""
    try:
        local = con.execute(
            """SELECT symbol, trade_date, close, pct_chg
               FROM (
                   SELECT symbol, trade_date, close, pct_chg,
                          row_number() OVER (PARTITION BY symbol ORDER BY trade_date DESC) AS rn
                   FROM index_bar
               ) WHERE rn=1"""
        ).df()
    except Exception:  # noqa: BLE001 - optional dashboard module
        local = pd.DataFrame()

    live = _live_major_indices(tuple(INDEX_NAMES))
    frames = []
    live_symbols: set[str] = set()
    if live is not None and not live.empty:
        live = live.copy()
        live["symbol"] = live["symbol"].astype(str).str.zfill(6)
        live = live[live["symbol"].isin(INDEX_NAMES)]
        live = live[live["price"].notna()]
        if not live.empty:
            live_symbols = set(live["symbol"])
            live["close"] = live["price"]
            live["data_time"] = live["quote_time"]
            live["data_kind"] = "实时"
            frames.append(live[["symbol", "name", "close", "pct_chg", "data_time", "data_kind"]])

    if local is not None and not local.empty:
        local = local.copy()
        local["symbol"] = local["symbol"].astype(str).str.zfill(6)
        local = local[~local["symbol"].isin(live_symbols)]
        local["name"] = local["symbol"].map(INDEX_NAMES).fillna(local["symbol"])
        local["data_time"] = local["trade_date"]
        local["data_kind"] = "收盘"
        frames.append(local[["symbol", "name", "close", "pct_chg", "data_time", "data_kind"]])
    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    df["name"] = df["name"].fillna(df["symbol"].map(INDEX_NAMES)).fillna(df["symbol"])
    order = {code: i for i, code in enumerate(INDEX_NAMES)}
    df["_order"] = df["symbol"].map(order).fillna(len(order))
    return df.sort_values(["_order", "symbol"]).drop(columns="_order")


def _market_breadth_history(con, limit: int = 240) -> pd.DataFrame:
    try:
        df = con.execute(
            """SELECT snap_time, up_count, down_count, avg_rise, up_ratio,
                      limit_up, limit_down, sentiment_desc
               FROM market_breadth_snapshot
               ORDER BY snap_time DESC LIMIT ?""",
            [int(limit)],
        ).df()
    except Exception:  # noqa: BLE001 - optional dashboard module
        return pd.DataFrame()
    if df.empty:
        return df
    df["snap_time"] = pd.to_datetime(df["snap_time"], errors="coerce")
    return df.dropna(subset=["snap_time"]).drop_duplicates("snap_time").sort_values("snap_time")


def _render_breadth_trend(con):
    hist = _market_breadth_history(con)
    st.markdown("#### 市场宽度")
    if len(hist) < 2:
        st.info("市场宽度历史不足，刷新后将逐步形成盘中趋势。")
        return

    fig = make_subplots(
        rows=1,
        cols=2,
        column_widths=[0.58, 0.42],
        horizontal_spacing=0.12,
        subplot_titles=("上涨家数占比", "涨跌停结构"),
    )
    fig.add_trace(
        go.Scatter(
            x=hist["snap_time"],
            y=pd.to_numeric(hist["up_ratio"], errors="coerce"),
            mode="lines",
            line=dict(color="#d84a4a", width=2),
            fill="tozeroy",
            fillcolor="rgba(216,74,74,0.10)",
            hovertemplate="%{x|%m-%d %H:%M}<br>上涨占比 %{y:.1f}%<extra></extra>",
            name="上涨占比",
        ),
        row=1,
        col=1,
    )
    fig.add_hline(y=50, line_dash="dot", line_color="#8a94a3", row=1, col=1)
    fig.add_trace(
        go.Bar(
            x=hist["snap_time"],
            y=pd.to_numeric(hist["limit_up"], errors="coerce").fillna(0),
            marker_color=UP,
            hovertemplate="%{x|%m-%d %H:%M}<br>涨停 %{y:.0f}<extra></extra>",
            name="涨停",
        ),
        row=1,
        col=2,
    )
    fig.add_trace(
        go.Bar(
            x=hist["snap_time"],
            y=-pd.to_numeric(hist["limit_down"], errors="coerce").fillna(0),
            marker_color=DN,
            hovertemplate="%{x|%m-%d %H:%M}<br>跌停 %{customdata:.0f}<extra></extra>",
            customdata=pd.to_numeric(hist["limit_down"], errors="coerce").fillna(0),
            name="跌停",
        ),
        row=1,
        col=2,
    )
    fig.update_yaxes(range=[0, 100], ticksuffix="%", row=1, col=1)
    fig.update_layout(
        height=300,
        margin=dict(t=42, b=10, l=10, r=10),
        barmode="relative",
        showlegend=False,
        hovermode="x unified",
    )
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})


def _render_major_indices(con):
    st.markdown("#### 主要指数")
    df = _latest_index_snapshot(con)
    if df.empty:
        st.info("本地指数行情暂无数据。")
        return
    show = df.rename(columns={
        "name": "指数", "symbol": "代码", "close": "收盘", "pct_chg": "涨跌%",
        "data_time": "时间", "data_kind": "口径",
    })[["指数", "代码", "收盘", "涨跌%", "口径", "时间"]]
    times = pd.to_datetime(show["时间"], errors="coerce")
    show["时间"] = [
        "—" if pd.isna(ts) else ts.strftime("%m-%d" if ts.hour == ts.minute == 0 else "%m-%d %H:%M")
        for ts in times
    ]
    st.dataframe(
        show.style.map(_color, subset=["涨跌%"]).format({"收盘": "{:.2f}", "涨跌%": "{:+.2f}"}),
        width="stretch",
        hide_index=True,
        height=min(315, 36 + 35 * len(show)),
    )
    st.caption("指数优先使用腾讯 60 秒实时快照，缺失项回退本地收盘；盘中宽度来自市场快照。")


def _topic_rows(news: pd.DataFrame) -> pd.DataFrame:
    stats = _local_news_stats(news)
    rows = []
    for theme, count in stats["top_themes"]:
        score = sum(
            int(item["local_score"])
            for item in stats["enriched"]
            if theme in item["themes"]
        )
        rows.append({"主题": theme, "新闻数": count, "倾向分": score})
    return pd.DataFrame(rows)


def _render_hot_topics(news: pd.DataFrame):
    st.markdown("#### 热点主题")
    topics = _topic_rows(news)
    if topics.empty:
        st.info("当前新闻尚未命中主题词。")
        return
    topics = topics.sort_values(["新闻数", "倾向分"], ascending=[True, True]).tail(8)
    colors = [UP if value > 0 else DN if value < 0 else "#8a94a3" for value in topics["倾向分"]]
    fig = go.Figure(go.Bar(
        x=topics["新闻数"],
        y=topics["主题"],
        orientation="h",
        marker_color=colors,
        text=topics["新闻数"],
        textposition="outside",
        customdata=topics["倾向分"],
        hovertemplate="%{y}<br>新闻 %{x} 条<br>本地倾向分 %{customdata:+.0f}<extra></extra>",
    ))
    fig.update_layout(
        height=max(230, 38 * len(topics)),
        margin=dict(t=10, b=10, l=10, r=28),
        xaxis=dict(showgrid=False, showticklabels=False, zeroline=False),
        yaxis=dict(title=None),
    )
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
    st.caption("红/绿分别表示本地关键词偏多/偏空；灰色为中性，仅用于新闻聚类。")


def _render_sentiment_trend(con):
    st.markdown("#### 情绪走势")
    hist = sentiment_history(con, days=30).sort_values("as_of_date")
    if len(hist) < 2:
        st.info("舆情历史不足，至少需要两个有数据的交易日。")
        return
    colors = [UP if s > 0.1 else DN if s < -0.1 else "#8a94a3" for s in hist["score"]]
    fig = go.Figure(go.Scatter(
        x=hist["as_of_date"].astype(str),
        y=hist["score"],
        mode="lines+markers",
        line=dict(color="#3973ac", width=2),
        marker=dict(color=colors, size=8),
        hovertemplate="%{x}<br>情绪分 %{y:+.2f}<extra></extra>",
    ))
    fig.add_hline(y=0, line_dash="dot", line_color="#8a94a3")
    fig.update_layout(
        height=280,
        margin=dict(t=10, b=10, l=10, r=10),
        yaxis=dict(range=[-1, 1], title=None),
        xaxis=dict(title=None),
    )
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
    st.caption("AI 生成，-1 为偏空、0 为中性、+1 为偏多。")


def _render_sentiment_brief(sent: dict | None, market: dict | None):
    st.markdown("#### 舆情判断")
    if not sent:
        st.info("当前没有可用的 AI 舆情总结；本地规则与市场宽度仍可独立使用。")
        return
    alignment = _market_news_alignment(sent, market)
    st.markdown(f"**{sent['summary']}**")
    if alignment:
        st.caption(f"市场校验: {alignment}")
    opportunity, risk = st.columns(2)
    with opportunity:
        st.markdown("**正面主题**")
        values = [x for x in str(sent.get("bullish") or "").split("；") if x]
        st.caption(" · ".join(values) if values else "—")
    with risk:
        st.markdown("**风险主题**")
        values = [x for x in str(sent.get("bearish") or "").split("；") if x]
        st.caption(" · ".join(values) if values else "—")
    created = sent.get("created_at")
    created_txt = (
        pd.Timestamp(created).strftime("%Y-%m-%d %H:%M")
        if created is not None and pd.notna(created) else "n/a"
    )
    st.caption(
        f"{sent.get('model', '')} · {int(sent.get('n_news') or 0)} 条新闻 · "
        f"生成于 {created_txt} · AI 生成，非投资建议"
    )


def _render_market_overview(news: pd.DataFrame, sent: dict | None, market: dict | None):
    from ashare.ingest.fundflow import market_fund_flow_hist

    con = get_con()
    mff = market_fund_flow_hist(con, 20)
    flow = mff.iloc[0] if not mff.empty else None
    metrics = st.columns(5, gap="small")
    if market:
        metrics[0].metric(
            "市场温度",
            market.get("sentiment_desc") or "—",
            f"均涨 {_safe_float(market.get('avg_rise')):+.2f}%",
            delta_color="off",
        )
        metrics[1].metric(
            "上涨 / 下跌",
            f"{int(_safe_float(market.get('up_count')))} / {int(_safe_float(market.get('down_count')))}",
            f"上涨占比 {_safe_float(market.get('up_ratio')):.1f}%",
            delta_color="off",
        )
        metrics[2].metric(
            "涨停 / 跌停",
            f"{int(_safe_float(market.get('limit_up')))} / {int(_safe_float(market.get('limit_down')))}",
            "盘中宽度",
            delta_color="off",
        )
    else:
        for col, label in zip(metrics[:3], ["市场温度", "上涨 / 下跌", "涨停 / 跌停"]):
            col.metric(label, "未采集")
    if sent:
        metrics[3].metric(
            "AI 舆情",
            str(sent.get("label") or "—"),
            f"情绪分 {_safe_float(sent.get('score')):+.2f}",
            delta_color="off",
        )
    else:
        metrics[3].metric("AI 舆情", "暂无")
    if flow is not None:
        metrics[4].metric(
            "主力净流入",
            f"{_safe_float(flow.get('main_net')) / 1e8:+.1f} 亿",
            f"{_safe_float(flow.get('main_ratio')):+.2f}%",
            delta_color="off",
        )
    else:
        metrics[4].metric("主力净流入", "暂无")

    freshness = []
    if market and market.get("snap_time") is not None:
        freshness.append(f"市场快照 {pd.Timestamp(market['snap_time']):%m-%d %H:%M}")
    if flow is not None:
        freshness.append(f"资金数据 {str(flow['trade_date'])[:10]}")
    if freshness:
        st.caption(" · ".join(freshness))

    left, right = st.columns([0.62, 0.38], gap="large")
    with left:
        _render_breadth_trend(con)
    with right:
        _render_major_indices(con)

    st.divider()
    left, right = st.columns([0.5, 0.5], gap="large")
    with left:
        _render_sentiment_brief(sent, market)
        _render_sentiment_trend(con)
    with right:
        _render_hot_topics(news)


def _render_attention_queues(news: pd.DataFrame):
    stats = _local_news_stats(news)
    high, holding = st.columns(2, gap="large")
    with high:
        st.markdown("#### 风险队列")
        if not stats["high"]:
            st.caption("当前没有中高风险新闻。")
        for item in stats["high"][:8]:
            severity = "高" if item["risk_level"] >= 3 else "中"
            st.markdown(f"- **{severity}** · {_format_news_title(item['row'])}")
            st.caption("触发: " + "；".join(item["risk_reasons"]))
    with holding:
        st.markdown("#### 持仓命中")
        if not stats["holding"]:
            st.caption("当前新闻没有命中持仓关键词。")
        for item in stats["holding"][:8]:
            labels = [f"{h['name']}({h['code']})" for h in item["holding_hits"]]
            st.markdown(f"- {_format_news_title(item['row'])}")
            st.caption("关联: " + "、".join(labels))


def _render_data_inventory(con):
    specs = [
        ("市场宽度", "market_breadth_snapshot", "snap_time"),
        ("A股指数", "index_bar", "trade_date"),
        ("市场资金", "market_fund_flow", "trade_date"),
        ("龙虎榜", "lhb_detail", "trade_date"),
        ("原始新闻", "news_raw", "news_date"),
        ("AI舆情", "sentiment_daily", "as_of_date"),
        ("全球指数", "global_index_snapshot", "snap_time"),
        ("板块资金", "sector_fund_flow_snapshot", "snap_time"),
        ("个股异动", "stock_change_event", "event_time"),
    ]
    rows = []
    for label, table, time_col in specs:
        try:
            count, latest = con.execute(
                f"SELECT count(*), max({time_col}) FROM {table}"
            ).fetchone()
        except Exception:  # noqa: BLE001 - schema may be older than the UI
            count, latest = 0, None
        rows.append({
            "数据集": label,
            "记录数": int(count or 0),
            "最近数据": "—" if latest is None else str(latest)[:19],
            "状态": "可用" if count else "待采集",
        })
    st.dataframe(
        pd.DataFrame(rows),
        width="stretch",
        hide_index=True,
        column_config={"记录数": st.column_config.NumberColumn(format="localized")},
    )


def _render_sentiment_overview(news: pd.DataFrame, sent: dict | None, market: dict | None = None):
    stats = _local_news_stats(news)
    warnings = _news_data_quality(news, sent)
    llm = _llm_runtime_config()
    last = st.session_state.get("last_llm_refresh") or {}

    st.markdown("##### 今日舆情总览")
    left, mid, right = st.columns([0.44, 0.32, 0.24])

    with left:
        if sent:
            score = float(sent["score"])
            arrow = "Risk-on" if score > 0.1 else "Risk-off" if score < -0.1 else "Neutral"
            as_of = str(sent["as_of_date"])[:10]
            created = sent.get("created_at")
            created_txt = pd.Timestamp(created).strftime("%m-%d %H:%M") if created is not None and pd.notna(created) else "n/a"
            st.metric(f"{arrow} · {sent['label']}", f"{score:+.2f}",
                      help="-1 极空/恐慌 ~ 0 中性 ~ +1 极多/亢奋")
            st.markdown(f"**{sent['summary']}**")
            st.caption(f"AI 基于 {int(sent['n_news'])} 条新闻 · {as_of} · 生成 {created_txt}")
        else:
            st.metric("AI 舆情结论", "暂无")
            st.caption("已展示本地规则预筛；可点击上方按钮生成 AI 总结。")

    with mid:
        st.metric("本地风险", len(stats["high"]))
        c1, c2 = st.columns(2)
        c1.metric("持仓命中", len(stats["holding"]))
        c2.metric("偏多/偏空", f"{len(stats['bullish'])}/{len(stats['bearish'])}")
        if stats["top_themes"]:
            st.caption("主题: " + " · ".join(f"{k}:{v}" for k, v in stats["top_themes"][:4]))
        if stats["holding"]:
            first = stats["holding"][0]
            labels = [f"{h['name']}({h['code']})" for h in first["holding_hits"]]
            st.caption("最近持仓相关新闻: " + "、".join(labels[:3]))

    with right:
        if market:
            snap = pd.Timestamp(market["snap_time"]).strftime("%H:%M")
            st.metric("市场温度", market.get("sentiment_desc") or "—")
            st.metric("涨跌家数", f"{market.get('up_count', 0)}/{market.get('down_count', 0)}")
            st.caption(
                f"{snap} · 涨停/跌停 {market.get('limit_up', 0)}/{market.get('limit_down', 0)} · "
                f"均涨 {float(market.get('avg_rise') or 0):+.2f}%"
            )
            align = _market_news_alignment(sent, market)
            if align:
                st.caption(align)
        else:
            st.metric("市场温度", "未采集")
            st.caption("点击顶部刷新市场温度。")
        counts = _source_counts(news)
        st.caption(f"今日新闻 {0 if news is None else len(news)} 条 · 新闻源 {len(counts)} 个")
        st.caption(f"LLM: {llm['model']} · {llm['base_host']}")
        if last:
            st.caption(f"最近调用: {last.get('status', 'unknown')} · {last.get('finished_at', '')}")
        st.caption("数据质量: " + ("注意" if warnings else "正常"))

    if warnings:
        st.warning("；".join(warnings))


def _render_local_news_panel(news: pd.DataFrame):
    stats = _local_news_stats(news)
    enriched = stats["enriched"]
    if not enriched:
        return
    high = stats["high"]
    holding = stats["holding"]
    bullish = stats["bullish"]
    bearish = stats["bearish"]
    themes = stats["themes"]

    st.markdown("##### 本地规则明细")
    cols = st.columns(4)
    cols[0].metric("高/中风险", len(high))
    cols[1].metric("命中持仓", len(holding))
    cols[2].metric("偏多/偏空", f"{len(bullish)}/{len(bearish)}")
    cols[3].metric("主题数", len(themes))
    if themes:
        st.caption("主题热词: " + " · ".join(f"{k}:{v}" for k, v in sorted(themes.items(), key=lambda x: x[1], reverse=True)[:6]))

    if holding:
        with st.expander("命中持仓/关注标的", expanded=True):
            for item in holding[:12]:
                r = item["row"]
                hits = []
                for h in item["holding_hits"]:
                    hits.append(f"{h['name']}({h['code']}): " + "、".join(h["matched"]))
                st.markdown(f"- {_format_news_title(r)}")
                st.caption("　命中: " + "；".join(hits))

    if high:
        with st.expander("本地高风险新闻", expanded=True):
            for item in high[:12]:
                r = item["row"]
                sev = "高" if item["risk_level"] >= 3 else "中"
                st.markdown(f"- 风险{sev} · {_format_news_title(r)}")
                st.caption("　触发: " + "；".join(item["risk_reasons"]))


def _render_ai_input_sample(news: pd.DataFrame):
    if news is None or news.empty:
        return
    cfg = _llm_runtime_config()
    sample = news.head(cfg["max_headlines"])
    with st.expander("AI 总结输入样本与来源", expanded=False):
        st.caption(f"本次页面可见新闻 {len(news)} 条；AI 总结最多取 {cfg['max_headlines']} 条，按时间倒序。")
        st.dataframe(
            sample[["source", "title", "ts"]].assign(
                ts=lambda d: pd.to_datetime(d["ts"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M")
            ),
            width="stretch",
            hide_index=True,
        )


def _capital_watch_symbols(con=None) -> list[str]:
    """龙虎榜/资金流交叉验证的个股集 = config sample_targets.stocks + minute.watchlist
    + 持仓个股 (与 CLI._stock_watch_symbols 口径一致)。"""
    cfg = get_cfg()
    syms = {str(c).zfill(6) for c in (cfg.get("sample_targets", {}).get("stocks") or [])}
    syms |= {str(c).zfill(6) for c in ((cfg.get("minute", {}) or {}).get("watchlist") or [])}
    try:
        pf_local = load_positions()
        if pf_local:
            syms |= {h.code.zfill(6) for h in pf_local.holdings if h.type == "stock"}
    except Exception:  # noqa: BLE001
        pass
    if con is not None and syms:
        try:
            placeholders = ",".join(["?"] * len(syms))
            nonstock = {
                str(row[0]).zfill(6)
                for row in con.execute(
                    f"SELECT symbol FROM instruments WHERE symbol IN ({placeholders}) AND type<>'stock'",
                    sorted(syms),
                ).fetchall()
            }
            syms -= nonstock
        except Exception:  # noqa: BLE001 - unknown symbols remain eligible
            pass
    return sorted(syms)


def _capital_flow_panel():
    """Dedicated money-flow and LHB workspace. All inputs are T+1 close data."""
    from ashare.ingest.fundflow import (
        ingest_market_fund_flow,
        ingest_stock_fund_flow,
        load_stock_flow_for,
        market_fund_flow_hist,
    )
    from ashare.ingest.lhb import (
        ingest_lhb,
        latest_lhb_date,
        load_active_seats,
        load_inst_recent,
        load_lhb_for_symbols,
    )

    con = get_con()
    init_schema(con)
    watch_symbols = _capital_watch_symbols(con)
    header, action = st.columns([0.82, 0.18], vertical_alignment="center")
    header.markdown("### 资金流向")
    with action:
        refresh = st.button(
            "刷新资金数据",
            key="refresh_lhb",
            icon=":material/sync:",
            width="stretch",
        )
    if refresh:
        with st.spinner("拉取龙虎榜与资金流…"):
            try:
                ak = get_ak()
                ingest_lhb(con, ak)
                ingest_market_fund_flow(con, ak)
                ingest_stock_fund_flow(con, ak, watch_symbols)
                st.success("资金数据已更新。")
                _reset_db_connection()
                st.rerun()
            except Exception as e:  # noqa: BLE001
                st.warning(f"刷新失败: {e}")

    mff = market_fund_flow_hist(con, 20)
    if mff.empty and latest_lhb_date(con) is None:
        st.info("暂无龙虎榜与资金流数据。")
        return

    if not mff.empty:
        latest = mff.iloc[0]
        metrics = st.columns(4, gap="small")
        metrics[0].metric(
            "当日主力净流入",
            f"{_safe_float(latest.get('main_net')) / 1e8:+.1f} 亿",
            f"占比 {_safe_float(latest.get('main_ratio')):+.2f}%",
            delta_color="off",
        )
        metrics[1].metric(
            "近5日累计",
            f"{float(mff.head(5)['main_net'].sum()) / 1e8:+.1f} 亿",
            delta_color="off",
        )
        metrics[2].metric(
            "近20日累计",
            f"{float(mff['main_net'].sum()) / 1e8:+.1f} 亿",
            delta_color="off",
        )
        metrics[3].metric(
            "沪 / 深",
            f"{_safe_float(latest.get('sh_pct')):+.2f}% / {_safe_float(latest.get('sz_pct')):+.2f}%",
            delta_color="off",
        )
        st.caption(
            f"数据截至 {str(latest['trade_date'])[:10]} · 主力口径为超大单与大单净额"
        )

        left, right = st.columns([0.58, 0.42], gap="large")
        with left:
            st.markdown("#### 大盘主力趋势")
            chart = mff.sort_values("trade_date").copy()
            net = pd.to_numeric(chart["main_net"], errors="coerce").fillna(0) / 1e8
            fig = go.Figure(go.Bar(
                x=chart["trade_date"].astype(str),
                y=net,
                marker_color=[UP if v >= 0 else DN for v in net],
                hovertemplate="%{x}<br>主力净流 %{y:+.1f} 亿<extra></extra>",
            ))
            fig.add_hline(y=0, line_color="#8a94a3", line_width=1)
            fig.update_layout(
                height=300,
                margin=dict(t=10, b=10, l=10, r=10),
                xaxis=dict(title=None),
                yaxis=dict(title="亿元", zeroline=False),
            )
            st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})

        with right:
            st.markdown("#### 自选 / 持仓主力流")
            stock_flow = load_stock_flow_for(con, watch_symbols, days=5)
            if stock_flow.empty:
                st.caption("暂无自选或持仓个股资金流。")
            else:
                latest_flow = (
                    stock_flow.sort_values(["symbol", "trade_date"], ascending=[True, False])
                    .drop_duplicates("symbol")
                )
                totals = stock_flow.groupby("symbol", as_index=False)["main_net"].sum()
                totals = totals.rename(columns={"main_net": "five_day_net"})
                latest_flow = latest_flow.merge(totals, on="symbol", how="left")
                names = {}
                try:
                    pf_local = load_positions()
                    names = {h.code.zfill(6): h.name for h in (pf_local.holdings if pf_local else [])}
                except Exception:  # noqa: BLE001
                    pass
                latest_flow["名称"] = latest_flow["symbol"].map(names).fillna(latest_flow["symbol"])
                latest_flow["当日净流(亿)"] = (latest_flow["main_net"] / 1e8).round(2)
                latest_flow["5日净流(亿)"] = (latest_flow["five_day_net"] / 1e8).round(2)
                show = latest_flow.rename(columns={
                    "symbol": "代码", "pct_chg": "涨跌%", "main_ratio": "主力占比%",
                })[["名称", "代码", "涨跌%", "主力占比%", "当日净流(亿)", "5日净流(亿)"]]
                st.dataframe(
                    show.style.map(
                        _color,
                        subset=["涨跌%", "主力占比%", "当日净流(亿)", "5日净流(亿)"],
                    ),
                    width="stretch",
                    hide_index=True,
                    height=min(300, 36 + 35 * len(show)),
                )

    st.divider()
    st.markdown("### 龙虎榜")
    hit = load_lhb_for_symbols(con, watch_symbols, days=10)
    if hit.empty:
        st.caption("近10日自选与持仓没有龙虎榜记录。")
    else:
        st.markdown("#### 自选 / 持仓上榜")
        show = hit.copy()
        show["龙虎净买(亿)"] = (show["net_buy"] / 1e8).round(2)
        show["机构净买(亿)"] = (show["org_net_amt"] / 1e8).round(2)
        show = show[["trade_date", "symbol", "name", "pct_chg", "龙虎净买(亿)",
                     "机构净买(亿)", "reason"]].rename(columns={
                         "trade_date": "日期", "symbol": "代码", "name": "名称",
                         "pct_chg": "涨跌%", "reason": "上榜原因",
                     })
        st.dataframe(
            show.style.map(_color, subset=["涨跌%", "龙虎净买(亿)", "机构净买(亿)"]),
            width="stretch",
            hide_index=True,
        )

    institutions, seats_col = st.columns(2, gap="large")
    inst = load_inst_recent(con, 8)
    with institutions:
        st.markdown("#### 机构净买 Top")
        if inst.empty:
            st.caption("暂无机构席位数据。")
        else:
            table = inst.copy()
            table["机构净买(亿)"] = (table["org_net_amt"] / 1e8).round(2)
            table = table[["symbol", "name", "pct_chg", "机构净买(亿)", "buy_org_n"]].rename(
                columns={"symbol": "代码", "name": "名称", "pct_chg": "涨跌%",
                         "buy_org_n": "买方机构数"}
            )
            st.dataframe(
                table.style.map(_color, subset=["涨跌%", "机构净买(亿)"]),
                width="stretch",
                hide_index=True,
            )
    seats = load_active_seats(con, 8)
    with seats_col:
        st.markdown("#### 活跃营业部 Top")
        if seats.empty:
            st.caption("暂无营业部数据。")
        else:
            table = seats.copy()
            table["净买(亿)"] = (table["net_amt"] / 1e8).round(2)
            table["营业部"] = table["yyb_name"].str.replace("证券股份有限公司", "", regex=False)
            table = table[["营业部", "净买(亿)", "buy_stock_n"]].rename(
                columns={"buy_stock_n": "买入只数"}
            )
            st.dataframe(
                table.style.map(_color, subset=["净买(亿)"]),
                width="stretch",
                hide_index=True,
            )
    st.caption("龙虎榜、机构与营业部数据均为交易所 T+1 盘后口径，非实时成交信号。")


def page_sentiment():
    head = st.columns([0.52, 0.16, 0.16, 0.16], vertical_alignment="bottom")
    head[0].title("市场情报工作台")
    head[0].caption("A股行情、资金与资讯交叉验证")
    with head[1]:
        refresh_all = st.button(
            "刷新全部",
            icon=":material/sync:",
            type="primary",
            width="stretch",
        )
    with head[2]:
        refresh_market = st.button(
            "刷新行情",
            icon=":material/monitoring:",
            width="stretch",
        )
    with head[3]:
        summarize = st.button(
            "更新AI总结",
            icon=":material/auto_awesome:",
            width="stretch",
        )

    view = st.segmented_control(
        "工作区",
        ["市场概览", "资讯监控", "资金与龙虎榜", "决策复盘", "数据状态"],
        default="市场概览",
        required=True,
        key="market_workspace",
        label_visibility="collapsed",
        width="stretch",
    )
    con = get_con()

    if refresh_all:
        with st.spinner("刷新新闻、市场温度和 AI 总结…"):
            info = _refresh_page_inputs(force=True, force_ai=True)
        if info.get("news_error"):
            st.warning(f"新闻刷新失败: {info['news_error']}")
        if info.get("market_error"):
            st.warning(f"市场温度刷新失败: {info['market_error']}")
        ai = info.get("ai") or {}
        if ai.get("error"):
            st.warning(f"AI 总结失败: {ai['error']}")
        elif ai.get("row") is None:
            st.warning("新闻/市场温度已刷新，但 AI 总结未生成。")
        else:
            st.success("新闻、市场温度和 AI 总结已更新。")
        _reset_db_connection()
        st.rerun()

    if refresh_market:
        try:
            n = ingest_market_breadth(con)
            st.success(f"市场温度已更新: {n} 行")
            _reset_db_connection()
            st.rerun()
        except Exception as e:  # noqa: BLE001
            st.warning(f"市场温度刷新失败: {e}")

    if summarize:
        with st.spinner("基于当前新闻生成 AI 舆情总结…"):
            refresh = _refresh_sentiment_now(force=True)
        if refresh.get("error"):
            st.warning(f"AI 总结失败: {refresh['error']}。下方展示最近一次已保存的舆情分析。")
        elif refresh.get("row") is None:
            st.warning("新闻已更新，但 AI 舆情总结未生成，可能是无新闻、未配置密钥或模型调用失败。")
        else:
            st.success("AI 舆情总结已更新。")
            _reset_db_connection()
            st.rerun()

    if view in ("市场概览", "资讯监控"):
        info = _refresh_page_inputs()
        if info.get("news_error"):
            st.warning(f"新闻自动刷新失败: {info['news_error']}")
        if info.get("market_error"):
            st.warning(f"市场温度自动刷新失败: {info['market_error']}")
        if info.get("ai") and info["ai"].get("row") is not None:
            st.success(f"AI 总结已自动更新: {info.get('ai_reason', '')}")
            _reset_db_connection()
            st.rerun()

    today_news = _news_for_day(con, date.today())
    news_date = date.today()
    news = today_news
    if news.empty:
        news_date = con.execute("SELECT max(news_date) FROM news_raw").fetchone()[0]
        news = _news_for_day(con, news_date) if news_date is not None else today_news
    current_sent = latest_sentiment(con)
    current_market = latest_market_breadth(con)

    status = []
    if current_market and current_market.get("snap_time") is not None:
        status.append(f"行情 {pd.Timestamp(current_market['snap_time']):%m-%d %H:%M}")
    if current_sent:
        status.append(f"舆情 {str(current_sent.get('as_of_date'))[:10]}")
    if news_date is not None:
        status.append(f"资讯 {str(news_date)[:10]} · {len(news)} 条")
    if status:
        st.caption("数据状态 · " + " · ".join(status))

    if view == "市场概览":
        _render_market_overview(news, current_sent, current_market)
    elif view == "资讯监控":
        st.markdown("### 实时监控")
        _live_news_monitor()
        st.divider()
        _render_attention_queues(news)
        st.divider()
        _render_news_feed(news, news_date)
    elif view == "资金与龙虎榜":
        _capital_flow_panel()
    elif view == "决策复盘":
        _render_decision_review(con)
    else:
        st.markdown("### 数据状态")
        _render_market_quality_panel(con)
        st.divider()
        _render_news_quality_panel(today_news, current_sent)
        st.markdown("#### 数据资产")
        _render_data_inventory(con)
        st.divider()
        st.markdown("### AI 运行配置")
        _render_llm_config_card()
        _render_ai_input_sample(news)


def _decision_journal() -> DecisionJournal:
    return DecisionJournal(resolve_path(get_cfg(), "decision_journal"))


def _render_decision_review(con):
    st.markdown("### 决策复盘")
    st.caption("跟踪每条决策在 T+1 / T+5 / T+20 的绝对收益和相对沪深300超额；最长已完成周期决定当前判定。")

    controls = st.columns([0.18, 0.22, 0.22, 0.2, 0.18], vertical_alignment="bottom")
    with controls[0]:
        window = st.selectbox(
            "复盘窗口", [30, 90, 180, 365], index=1,
            format_func=lambda value: f"近 {value} 天",
        )
    with controls[1]:
        source_label = st.selectbox(
            "信号来源", ["全部", "推荐", "开盘", "尾盘", "持仓"],
        )
    with controls[2]:
        status_label = st.selectbox(
            "信号状态", ["全部", "观察中", "待数据", "已完成", "无法评估"],
        )
    with controls[4]:
        recalculate = st.button(
            "重算历史结果", icon=":material/replay:", width="stretch",
        )

    source = {
        "推荐": "recommend", "开盘": "premarket",
        "尾盘": "endday", "持仓": "pf",
    }.get(source_label)
    status = {
        "观察中": "active", "待数据": "pending_data",
        "已完成": "closed", "无法评估": "unable",
    }.get(status_label)
    journal = _decision_journal()

    if recalculate:
        with st.spinner("按当前行情重算历史观察结果…"):
            result = evaluate_due_signals(
                get_cfg(), con, force=True, journal=journal
            )
        st.success(
            f"已扫描 {result['scanned']} 条决策，更新 {result['evaluated']} 个周期结果，"
            f"完成 {result['closed']} 条；"
            f"等待数据 {result['pending_data']} 个。"
        )

    details = journal.review_details(
        window, source=source, status=status,
    )
    evaluated = [row for row in details if row["outcomes"]]
    effective = sum(row["verdict"] == "有效" for row in evaluated)
    invalid = sum(row["verdict"] == "失效" for row in evaluated)
    metrics = st.columns(4)
    metrics[0].metric("决策样本", len(details))
    metrics[1].metric("已有结果", len(evaluated))
    metrics[2].metric("当前有效", effective)
    decided = effective + invalid
    metrics[3].metric(
        "有效率",
        f"{effective / decided * 100:.1f}%" if decided else "—",
        help="只统计当前判定为有效或失效的样本；中性结果不进入分母。",
    )

    summary_rows = []
    for horizon in (1, 5, 20):
        for row in journal.review(
            window, source=source, horizon_days=horizon, status=status,
        ):
            summary_rows.append({
                "周期": f"T+{horizon}",
                "信号组": row["group"],
                "样本": row["sample_count"],
                "命中率%": round(row["hit_rate_pct"], 1),
                "平均收益%": round(row["avg_fwd_return_pct"], 2),
                "平均超额%": round(row["avg_excess_pct"], 2),
            })
    if summary_rows:
        st.markdown("#### 分组表现")
        st.dataframe(
            pd.DataFrame(summary_rows).style.map(
                _color, subset=["平均收益%", "平均超额%"]
            ),
            width="stretch", hide_index=True,
        )

    st.markdown("#### 决策明细")
    if not details:
        st.info("当前筛选条件下暂无决策记录。")
        return
    detail_rows = []
    for row in details:
        values = {}
        for horizon in (1, 5, 20):
            outcome = row["outcomes"].get(horizon)
            values[f"T+{horizon}收益%"] = (
                round(outcome["fwd_return_pct"], 2) if outcome else None
            )
            values[f"T+{horizon}超额%"] = (
                round(outcome["excess_pct"], 2) if outcome else None
            )
        detail_rows.append({
            "日期": row["trade_date"],
            "来源": row["source"],
            "策略": row["style"] or "—",
            "代码": row["symbol"],
            "名称": row["name"] or "—",
            "动作": row["action"],
            "当前判定": row["verdict"],
            **values,
            "理由": row["rationale"] or "—",
            "状态": row["status"],
        })
    numeric_columns = [
        f"T+{horizon}{kind}%"
        for horizon in (1, 5, 20)
        for kind in ("收益", "超额")
    ]
    st.dataframe(
        pd.DataFrame(detail_rows).style.map(_color, subset=numeric_columns),
        width="stretch", hide_index=True,
    )


def _render_market_quality_panel(con):
    st.markdown("#### 核心数据质量")
    report = MarketWarehouse(con).quality_report(date.today())
    counts = {
        status: sum(row["status"] == status for row in report)
        for status in ("正常", "警告", "异常")
    }
    cards = st.columns(3)
    cards[0].metric("正常", counts["正常"])
    cards[1].metric("警告", counts["警告"])
    cards[2].metric("异常", counts["异常"])
    rows = [{
        "状态": row["status"],
        "数据集": row["dataset"],
        "最新日期": row["latest_date"] or "—",
        "应到日期": row["expected_date"] or "—",
        "交易日延迟": row["lag_trading_days"] if row["lag_trading_days"] is not None else "—",
        "最新行数": row["latest_rows"],
        "覆盖率%": row["coverage_pct"] if row["coverage_pct"] is not None else "—",
        "空值": row["null_count"],
        "重复键": row["duplicate_count"],
        "多源冲突": row["source_conflict_count"],
    } for row in report]
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    st.caption("场外基金净值允许 T+1；估值多源数值不一致显示为警告；缺表、关键值缺失或重复键显示为异常。")


@st.fragment(run_every=MONITOR_INTERVAL)
def _live_news_monitor():
    st.markdown("##### 实时新闻监控")
    ctrl = st.columns([0.2, 0.2, 0.6])
    force = ctrl[0].button("立即轮询", width="stretch")
    reset = ctrl[1].button("重置基线", width="stretch")
    res = _monitor_news_once(force=force, reset=reset)

    status = st.columns(4)
    status[0].metric("监控状态", "运行中", "60秒轮询")
    status[1].metric("本轮新增", len(res["new_rows"]))
    status[2].metric("本地风险", len(res["high_risk"]))
    status[3].metric("持仓命中", len(res["holding_hits"]))

    detail = " · ".join(f"{k}:{v}" for k, v in res["source_counts"].items()) or "暂无新闻"
    st.caption(f"上次检查 {res['at']:%H:%M:%S} · 本轮处理 {res['inserted']} 条 · 当前新闻池 {len(res['news'])} 条 · {detail}")
    if res["error"]:
        st.warning(f"新闻源轮询失败: {res['error']}。保留页面已有新闻。")
    if reset:
        st.info("监控基线已重置；下一轮开始只提示新出现的新闻。")

    if res["alerts"]:
        high = [a for a in res["alerts"] if a["severity"] >= 3]
        box = st.error if high else st.warning
        box(f"发现 {len(res['alerts'])} 条突发新闻预警。")
        for a in res["alerts"][:8]:
            sev = "高" if a["severity"] >= 3 else "中" if a["severity"] == 2 else "低"
            ts = ""
            if a["ts"] is not None and pd.notna(a["ts"]):
                ts = pd.Timestamp(a["ts"]).strftime("%H:%M")
            title = a["title"]
            if a.get("url"):
                title = f"[{title}]({a['url']})"
            st.markdown(f"- {ts + ' · ' if ts else ''}风险等级 {sev} · {title}")
            st.caption("　触发: " + "；".join(a["reasons"]))
    elif res["new_rows"]:
        st.info(f"有 {len(res['new_rows'])} 条新增新闻，未触发突发规则。")
    else:
        st.caption("暂无新增新闻。")

    if res["holding_hits"]:
        with st.expander("当前新闻流命中持仓", expanded=False):
            for item in res["holding_hits"][:8]:
                r = item["row"]
                labels = []
                for h in item["holding_hits"]:
                    labels.append(f"{h['name']}({h['code']})")
                st.markdown(f"- {_format_news_title(r)}")
                st.caption("　关联: " + "、".join(labels))

    if res["new_rows"]:
        with st.expander("本轮新增新闻", expanded=True):
            for r in res["new_rows"][:12]:
                info = _classify_news_row(r, _holding_watch_terms())
                badges = []
                if info["risk_level"]:
                    badges.append("风险" + ("高" if info["risk_level"] >= 3 else "中" if info["risk_level"] == 2 else "低"))
                if info["holding_hits"]:
                    badges.append("命中持仓")
                if info["themes"]:
                    badges.append("主题:" + "、".join(info["themes"][:2]))
                prefix = f"{' / '.join(badges)} · " if badges else ""
                st.markdown(f"- {prefix}{_format_news_title(r)}")
                if isinstance(r.summary, str) and r.summary.strip():
                    st.caption("　" + r.summary[:120])


def _render_news_feed(news: pd.DataFrame, nd):
    st.markdown("### 新闻流")
    if news is None or news.empty:
        st.caption("暂无入库新闻。")
        return
    label_map = {"em_global": "财经快讯", "cctv": "新闻联播"}
    sources = sorted(str(x) for x in news["source"].dropna().unique())
    controls = st.columns([0.42, 0.2, 0.24, 0.14], vertical_alignment="bottom")
    with controls[0]:
        query = st.text_input(
            "搜索新闻",
            placeholder="搜索标题或正文",
            label_visibility="collapsed",
            key="news_search",
            icon=":material/search:",
        ).strip().lower()
    with controls[1]:
        source = st.selectbox(
            "来源",
            ["全部"] + sources,
            format_func=lambda x: label_map.get(x, x),
            label_visibility="collapsed",
            key="news_source",
        )
    with controls[2]:
        focus = st.selectbox(
            "关注类型",
            ["全部", "中高风险", "持仓相关", "偏多", "偏空"],
            label_visibility="collapsed",
            key="news_focus",
        )
    with controls[3]:
        limit = st.selectbox(
            "显示数量",
            [25, 50, 100],
            index=1,
            format_func=lambda value: f"{value} 条",
            label_visibility="collapsed",
            key="news_limit",
        )

    items = _enrich_news(news)
    filtered = []
    for item in items:
        row = item["row"]
        if source != "全部" and str(getattr(row, "source", "")) != source:
            continue
        if query and query not in _text_of_news(row).lower():
            continue
        if focus == "中高风险" and item["risk_level"] < 2:
            continue
        if focus == "持仓相关" and not item["holding_hits"]:
            continue
        if focus == "偏多" and item["local_score"] <= 0:
            continue
        if focus == "偏空" and item["local_score"] >= 0:
            continue
        filtered.append(item)

    st.caption(f"新闻日期 {nd} · 匹配 {len(filtered)} / {len(news)} 条 · 当前显示前 {min(limit, len(filtered))} 条")
    if not filtered:
        st.info("没有符合当前筛选条件的新闻。")
        return

    for item in filtered[:limit]:
        row = item["row"]
        source_name = label_map.get(str(getattr(row, "source", "")), str(getattr(row, "source", "")))
        badges = [source_name]
        if item["risk_level"] >= 2:
            badges.append("风险" + ("高" if item["risk_level"] >= 3 else "中"))
        if item["holding_hits"]:
            badges.append("持仓")
        if item["polarity"] != "中性":
            badges.append(item["polarity"])
        if item["themes"]:
            badges.append("、".join(item["themes"][:2]))
        st.markdown(f"- **{' · '.join(badges)}**　{_format_news_title(row)}")
        details = []
        if item["holding_hits"]:
            details.append("关联 " + "、".join(
                f"{h['name']}({h['code']})" for h in item["holding_hits"]
            ))
        if item["risk_reasons"]:
            details.append("触发 " + "；".join(item["risk_reasons"]))
        if details:
            st.caption("　" + " · ".join(details))
        if (
            isinstance(row.summary, str)
            and row.summary.strip()
            and row.summary.strip() != str(row.title).strip()
        ):
            st.caption("　" + row.summary[:160])


def page_stockeval():
    st.subheader("🔎 个股速评")
    st.caption("输入6位代码 → 即时汇总 实时行情/估值/质量/技术面/三种打分/买卖位。"
               "AI综合解读按需生成(费 token)。打分均为回测偏弱的概率信号, 非买卖指令。")
    code = st.text_input("股票代码(6位)", max_chars=6, placeholder="如 600519").strip()
    if not code:
        return
    from ashare.stockeval import evaluate_stock
    res = evaluate_stock(get_con(), code, with_llm=False)
    if not res.get("ok"):
        st.error(res.get("error"))
        return
    p = res["profile"]
    q, val, qual, f = p["quote"], p["val"], p["qual"], p["f"]
    c = st.columns(4)
    if q and q.get("price") is not None:
        c[0].metric(p["name"], f"{q['price']:g}", f"{q.get('pct_chg', 0):+.2f}%")
    else:
        c[0].metric(p["name"], f"{p['close']:g}", "实时未取到")
    if val is not None and val.pe_ttm is not None:
        c[1].metric("PE-TTM", f"{val.pe_ttm:.1f}",
                    f"分位{val.pe_pct:.0f}%" if val.pe_pct is not None else "亏损/无分位",
                    delta_color="off")
    if qual is not None and qual.roe is not None:
        c[2].metric("ROE", f"{qual.roe:.1f}%",
                    f"净利{qual.profit_yoy:+.0f}%" if qual.profit_yoy is not None else None,
                    delta_color="off")
    if f is not None:
        c[3].metric("趋势", f.trend_label,
                    f"20日{f.ret20:+.1f}%" if f.ret20 is not None else None, delta_color="off")
    st.code(res["text"], language=None)
    if st.button("🤖 生成 AI 综合解读(费 token)", type="primary"):
        with st.spinner("DeepSeek 解读中(约10-30秒)…"):
            r2 = evaluate_stock(get_con(), code, with_llm=True, cfg=get_cfg())
        llm = r2.get("llm")
        if llm:
            st.markdown("##### 🤖 AI 综合解读")
            st.info(llm)
            st.caption("AI生成 · 仅基于上面量化数据(未接个股新闻) · 非投资建议")
        else:
            st.warning("LLM 未配置密钥或调用失败 — 看上面量化数据即可。")


def page_positions():
    st.subheader("✏️ 持仓管理")
    st.caption("直接编辑下表(右下角 ＋ 增行、选中行 🗑 删行)。代码=6位数字;类型 etf/stock 场内实时估值、"
               "otc_fund/bond_fund 场外按净值T-1。只填 代码/名称/类型/份额/成本——"
               "现价·市值·浮盈等批注由系统每日自动刷新, 不用你管。保存即写回 positions.yaml。")
    from ashare.portfolio import save_positions
    rows = [{"code": h.code, "name": h.name, "type": h.type,
             "shares": float(h.shares), "cost": float(h.cost)} for h in pf.holdings]
    df = pd.DataFrame(rows, columns=["code", "name", "type", "shares", "cost"])
    edited = st.data_editor(
        df, num_rows="dynamic", width="stretch", key="pos_editor",
        column_config={
            "code": st.column_config.TextColumn("代码", help="6位数字代码", max_chars=6, required=True),
            "name": st.column_config.TextColumn("名称", help="自己看的, 随便写"),
            "type": st.column_config.SelectboxColumn(
                "类型", options=["etf", "stock", "otc_fund", "bond_fund"], required=True),
            "shares": st.column_config.NumberColumn("份额/股数", min_value=0.0, format="%.2f"),
            "cost": st.column_config.NumberColumn("成本价", min_value=0.0, format="%.4f",
                                                  help="每股/每份成本; 不知道填0则只算权重不算盈亏"),
        })
    cash = st.number_input("现金仓位(元, 计入总资产算权重)", min_value=0.0,
                           value=float(pf.cash), step=100.0)
    c1, c2 = st.columns([0.18, 0.82])
    if c1.button("💾 保存", type="primary", width="stretch"):
        try:
            save_positions(edited.to_dict("records"), cash)
        except ValueError as e:
            st.error(f"保存失败: {e}")
        else:
            st.cache_data.clear()      # 让其余页面用新持仓重算
            st.success("已保存到 positions.yaml。现价/估值将在下次「🔄 刷新」或夜间更新后重算。")
            st.rerun()
    c2.caption("⚠ 这是你的真实持仓隐私文件(已 gitignore, 不会提交/外传), 保存只改本机文件。")


def page_recommend():
    st.subheader("🎯 推荐")
    fund_held = tuple(sorted(h.code for h in pf.holdings if h.type in ("etf", "otc_fund")))
    extra_items = tuple(sorted(
        (h.code, h.name, h.type) for h in pf.holdings if h.type in ("otc_fund",)
    ))
    funds, fdesc = load_index_fund_recommendations(9, fund_held, extra_items)
    render_index_fund_cards(funds, fdesc)

    st.divider()
    st.markdown("#### 主板个股推荐")
    # 稳健·价值版默认(最稳的"筛选+排雷"); 反转版有3-5年正超额但近1年衰减为负, 仅作可选项
    style_label = st.radio("打分风格",
                           ["稳健·价值版", "反转·超跌版(近年衰减⚠)", "自上而下·龙头版", "动量版(回测负超额⚠)"],
                           horizontal=True, label_visibility="collapsed")
    style = ("reversal" if "反转" in style_label
             else "leader" if "龙头" in style_label
             else "momentum" if "动量" in style_label else "value")
    stocks, uni = load_recommendations(12, style)
    if style == "reversal":
        st.caption(f"universe: {uni} · 买近20日超跌(动量的镜像), 月度持有/调仓 · 缓存30分钟")
        st.warning("反转·超跌版: 近3-5年回测有正超额(IC≈+0.09, Top20≈+1.7%/月), **但近1年衰减为负**"
                   "(超跌档多空 −2%)——edge 随市场风格切换、不稳定。已排雷剔 ST/退/亏损/极端暴跌。"
                   "**只宜小仓位试、严格止损、配合大盘择时, 不是稳赚**; 回测有幸存者偏差(偏乐观)。", icon="⚠️")
    elif style == "value":
        st.caption(f"universe: {uni} · 价值(PE/PB分位)+ 质量(ROE/增速) + 亏损剔除; 不奖励趋势/动量 · 缓存30分钟")
        st.warning("稳健·价值版: 估值便宜+盈利质量好+剔除亏损; 已**去掉追强势**(回测证明在A股是负超额)。"
                   "诚实提示: 回测信号偏弱(IC≈+0.06)且2026走弱, **本榜主要用于估值筛选+排雷, 非保证跑赢指数**。"
                   "最便宜的那几只常是价值陷阱, 务必结合基本面。", icon="⚠️")
    elif style == "leader":
        st.caption(f"universe: {uni} · 热门行业(成分股动量)→ 行业内价值+质量龙头 · 缓存30分钟")
        st.warning("自上而下·龙头版: 按板块动量选热门行业, 再选行业内价值+质量最高者。"
                   "注意: '热门=动量'这层**尚未回测验证**(个股层面动量在A股是负的, 板块层面待测); "
                   "'龙头'=板块内综合分最高(非按市值)。规则提示, 非投资指令。", icon="⚠️")
    else:
        st.caption(f"universe: {uni} · 趋势/动量主导 · 缓存30分钟")
        st.warning("⚠ 动量版: **回测显示负超额**——追强势在A股主板系统性偏亏(60日动量 IC≈−0.13/年, "
                   "高动量十分位反而垫底)。**保留仅供参考/反向思考, 不建议据此追高**。", icon="🚫")
    if not stocks:
        msg = ("暂无结果 — 稳健/龙头版需先关看板跑 `value-backfill --full` 回填全主板估值/质量。"
               if style in ("value", "leader") else "暂无结果 — 先关看板跑 `recommend` 或回填主板 bars。")
        st.info(msg)
        return

    # honesty: scores/买卖价位 are computed from daily bars (as-of last close); warn if
    # the warehouse is behind today, and show a *live* 现价 so the table isn't stale.
    asof = bars_as_of()
    if asof is not None and asof < date.today():
        gap = (date.today() - asof).days
        st.warning(f"⏰ 行情数据截至 **{asof}**(落后 {gap} 天)。评分/买卖价位基于该日收盘; "
                   f"现价/今日% 为实时。要刷新评分请关看板跑 `daily`。", icon="⏰")
    live = load_live_quotes(tuple(s.target for s in stocks))

    rows = []
    for s in stocks:
        m = s.metadata; lv = m.get("levels")
        q = live.get(s.target)
        price = q["price"] if q else m.get("close")
        pct = q["pct"] if q else m.get("today_pct")
        row = {
            "代码": s.target, "名称": m.get("name", ""), "评级": s.label,
            "评分": round(s.score, 2),
            "现价": round(price, 2) if price is not None else None,
            "今日%": round(pct, 2) if pct is not None else None,
            "PE分位": round(m["pe_pct"]) if m.get("pe_pct") is not None else None}
        if style in ("value", "leader"):   # 展示质量 + 行业
            row["ROE%"] = round(m["roe"], 1) if m.get("roe") is not None else None
            row["利润增速%"] = round(m["profit_yoy"], 1) if m.get("profit_yoy") is not None else None
            row["行业"] = m.get("industry") or ""
            if style == "leader":
                row["板块热度%"] = m.get("sector_heat")
        row.update({"买入区": f"{lv.buy_lo}~{lv.buy_hi}" if lv else "",
                    "止盈": lv.target if lv else None, "止损": lv.stop if lv else None})
        rows.append(row)
    st.dataframe(pd.DataFrame(rows).style.map(_color, subset=["今日%", "评分"]),
                 width="stretch", hide_index=True)
    st.caption("现价/今日% 为腾讯实时(60秒缓存); 评分·PE分位·ROE·买卖价位基于最新收盘日。")

    labels = {f"{s.metadata.get('name','')}({s.target})": s for s in stocks}
    s = labels[st.selectbox("查看个股详情(K线+买卖价位+拆解)", list(labels))]
    lv = s.metadata.get("levels")
    if lv:
        q = live.get(s.target)
        cur = q["price"] if q else lv.last        # live 现价 优先; 价位距离按现价算
        kind = "实时" if q else f"收盘{asof}"
        c = st.columns(4)
        c[0].metric(f"现价({kind})", round(cur, 2))
        c[1].metric("买入区", f"{lv.buy_lo}~{lv.buy_hi}")
        c[2].metric("止盈目标", lv.target, f"+{(lv.target/cur-1)*100:.1f}%")
        c[3].metric("止损", lv.stop, f"{(lv.stop/cur-1)*100:.1f}%")
        st.success(f"**{lv.status}**" + (f"　·　风险报酬比 ≈ {lv.rr}" if lv.rr else ""))
        for n in lv.notes:
            st.caption("• " + n)
    dfb = get_series(s.target, "stock")
    if not dfb.empty:
        fig = price_chart(s.metadata.get("name", ""), dfb, 250)
        add_price_levels(fig, lv)
        st.plotly_chart(fig, width="stretch")
    with st.expander("🔍 健康分拆解(每个因子的贡献)"):
        render_breakdown(s)


def page_holding(h: Holding):
    sig = health.get(h.code)
    st.subheader(f"{h.name}  ·  {h.code}")
    c = st.columns(5)
    c[0].metric("现价/净值", f"{h.price:g}" if h.price else "n/a")
    if h.today_pct is not None:
        c[1].metric("今日", f"{h.today_pct:+.2f}%")
    if h.pnl_pct is not None:
        c[2].metric("浮盈", f"{h.pnl_pct:+.1f}%", f"{h.pnl:,.0f} 元")
    c[3].metric("市值", f"{(h.market_value or 0)/1e4:.2f} 万", f"权重 {pf.weight(h):.1f}%")
    if sig:
        c[4].metric(f"健康分 · {sig.label}", f"{sig.score:+.2f}")

    a = adv_by_code.get(h.code)
    if a:
        st.info(f"**建议: {a.action}** — {a.reason}")

    # the 'why' panel — full score breakdown
    with st.expander("🔍 健康分是怎么算出来的（点开看每个因子的贡献）", expanded=True):
        render_breakdown(sig)

    st.divider()
    look = st.radio("区间", [60, 120, 250, 9999],
                    format_func=lambda n: "全部" if n == 9999 else f"近{n}日",
                    index=2, horizontal=True)
    df = get_series(h.code, h.type)
    if df.empty:
        st.warning("无价格/净值序列。")
        return
    lv = compute_price_levels(df) if h.type == "stock" else None
    if lv:
        cc = st.columns(4)
        cc[0].metric("买入区", f"{lv.buy_lo}~{lv.buy_hi}")
        cc[1].metric("止盈目标", lv.target, f"+{(lv.target/lv.last-1)*100:.1f}%")
        cc[2].metric("止损", lv.stop, f"{(lv.stop/lv.last-1)*100:.1f}%")
        cc[3].metric("风报比", lv.rr if lv.rr else "—")
        st.caption("🎯 " + lv.status)
    if h.type in ("otc_fund", "bond_fund"):
        st.caption("场外基金按**单位净值**绘制(无 OHLC), 净值为 T-1; 定投类不设买卖价位。")
    fig = price_chart(h.name, df, look)
    add_price_levels(fig, lv)
    st.plotly_chart(fig, width="stretch")


page_sentiment()
