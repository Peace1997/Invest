"""Streamlit app — Financial Analysis.

Reuses the existing layers (no logic forked here):
  portfolio  → 估值     signals → 健康分/温度计     decision → 操作建议
  snapshots  → 收益曲线  factors → 技术指标

Run:  uv run streamlit run src/ashare/ui/app.py
 or:  uv run python -m ashare.cli ui
"""
from __future__ import annotations
import os
from datetime import date, datetime, timedelta

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
from ashare.signals.sentiment import generate_sentiment, latest_sentiment, sentiment_history

st.set_page_config(page_title="Financial Analysis", page_icon="📈", layout="wide")

UP, DN = "#e23b3b", "#1aae5c"   # A股惯例: 红涨绿跌

MONITOR_INTERVAL = timedelta(seconds=60)
ALERT_RULES = [
    ("重大风险", 3, ("战争", "袭击", "爆炸", "制裁", "关税", "违约", "破产", "暴雷", "退市", "停牌", "熔断")),
    ("政策监管", 2, ("国务院", "证监会", "央行", "财政部", "发改委", "监管", "降准", "降息", "IPO", "印花税")),
    ("宏观冲击", 2, ("美联储", "CPI", "非农", "PMI", "汇率", "人民币", "美元指数", "原油", "黄金", "国债")),
    ("市场异动", 2, ("跳水", "大涨", "大跌", "创阶段新高", "创阶段新低", "北向", "成交额", "恐慌")),
    ("产业主题", 1, ("半导体", "芯片", "人工智能", "算力", "新能源", "光伏", "医药", "房地产", "低空经济")),
]


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

    return {
        "at": now,
        "inserted": inserted,
        "error": error,
        "news": news,
        "new_rows": new_rows,
        "alerts": alerts,
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
    try:
        row = generate_sentiment(con, ak, cfg.get("sentiment", {}), as_of=date.today())
    except Exception as e:  # noqa: BLE001
        st.session_state["sentiment_refresh_at"] = now
        return {"error": str(e), "at": now}

    st.session_state["sentiment_refresh_at"] = now
    latest = latest_sentiment(con)
    news = _news_for_day(con, date.today())
    return {
        "row": row,
        "latest": latest,
        "news_count": int(len(news)),
        "source_counts": _source_counts(news),
        "at": now,
    }


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
    left.plotly_chart(score_waterfall(sig), use_container_width=True)
    rows = [{"因子": c.label, "贡献": round(c.contribution, 3),
             "影响上限": c.cap, "依据(实际数值→解读)": c.detail}
            for c in sig.components]
    df = pd.DataFrame(rows)
    right.markdown(f"**综合健康分 = {sig.score:+.2f}** ({sig.label}) · "
                   f"置信度 {sig.confidence:.0%}（样本 {sig.metadata.get('n_obs','?')} 日）")
    right.dataframe(df.style.map(_color, subset=["贡献"]),
                    use_container_width=True, hide_index=True)
    right.caption("正贡献=偏多动力, 负贡献=偏空动力；各因子相加(裁剪到±1)即综合分。")


def _color(v):
    if v is None:
        return "color:#888"
    return f"color:{UP}" if v > 0 else f"color:{DN}" if v < 0 else "color:#888"


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
                st.plotly_chart(_sparkline_fig(spark, tone), use_container_width=True,
                                config={"displayModeBar": False})

                k = st.columns(3)
                k[0].metric("日涨跌", _pct_text(pct), delta_color="off")
                k[1].metric("周涨跌", _pct_text(m.get("week_pct")), delta_color="off")
                k[2].metric("月涨跌", _pct_text(m.get("month_pct")), delta_color="off")

                st.markdown(f"**{m.get('action')}**")
                for b in m.get("bullets", [])[:4]:
                    st.caption("• " + b)


# ════════════════════════════ layout ════════════════════════════
st.sidebar.title("Financial Analysis")
st.sidebar.caption("当前仅开放: 舆情分析")

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
    cc[0].plotly_chart(pie, use_container_width=True)

    # equity curve
    if history is not None and len(history) >= 2:
        h = history.sort_values("snapshot_date")
        eq = go.Figure(go.Scatter(x=h["snapshot_date"], y=h["total_market_value"],
                       fill="tozeroy", line=dict(color="#2b6cb0")))
        mv = h.set_index("snapshot_date")["total_market_value"].astype(float)
        mdd, pk, tr = max_drawdown(mv)
        eq.update_layout(title=f"总资产曲线 (最大回撤 {mdd:.2f}%)", height=340,
                         margin=dict(t=40, b=10, l=10, r=10))
        cc[1].plotly_chart(eq, use_container_width=True)
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
        use_container_width=True, hide_index=True)


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
        st.plotly_chart(fig, use_container_width=True)

    c1, c2 = st.columns(2)
    c1.code(render_risk(rep), language=None)
    c2.code(render_rebalance(plan), language=None)


def page_sentiment():
    st.subheader("舆情分析 · News & Sentiment Monitor")
    st.caption("流式新闻监控每 60 秒轮询新闻源；突发预警先用本地事件规则识别，AI 总结按需生成。")
    con = get_con()

    _live_news_monitor()

    st.divider()
    csum = st.columns([0.72, 0.28])
    csum[0].markdown("##### AI 市场舆情总结")
    with csum[1]:
        summarize = st.button("生成/更新AI总结", use_container_width=True)
    if summarize:
        with st.spinner("基于当前新闻生成 AI 舆情总结…"):
            refresh = _refresh_sentiment_now(force=True)
        if refresh.get("error"):
            st.warning(f"AI 总结失败: {refresh['error']}。下方展示最近一次已保存的舆情分析。")
        elif refresh.get("row") is None:
            st.warning("新闻已更新，但 AI 舆情总结未生成，可能是无新闻、未配置密钥或模型调用失败。")
        else:
            st.success("AI 舆情总结已更新。")

    sent = latest_sentiment(con)
    if not sent:
        news = _news_for_day(con, date.today())
        if not news.empty:
            st.info("已拉到最新新闻，但暂未生成 AI 舆情总结。请检查 LLM 密钥或模型配置。")
            _render_news_feed(news, date.today())
        else:
            st.info("暂无舆情数据，也没有拉到今日新闻。请检查新闻源网络或稍后再刷新。")
        return

    as_of = str(sent["as_of_date"])[:10]
    score = float(sent["score"])
    created = sent.get("created_at")
    created_txt = pd.Timestamp(created).strftime("%Y-%m-%d %H:%M:%S") if created is not None and pd.notna(created) else "n/a"
    if as_of < date.today().isoformat():
        st.warning(f"⚠ 最新舆情停留在 {as_of}(非今日)——今日可能尚未生成, 或新闻源/LLM 异常。"
                   "下方为最近一次分析, 已诚实标注日期。")

    arrow = "Risk-on" if score > 0.1 else "Risk-off" if score < -0.1 else "Neutral"
    c1, c2 = st.columns([0.26, 0.74])
    with c1:
        st.metric(f"{arrow} · {sent['label']}", f"{score:+.2f}",
                  help="-1 极空/恐慌 ~ 0 中性 ~ +1 极多/亢奋")
        st.caption(f"基于 {int(sent['n_news'])} 条新闻 · {as_of}")
        st.caption(f"生成时间 {created_txt}")
        st.caption(f"AI生成({sent.get('model','')}) · 仅供参考非投资建议")
    with c2:
        st.markdown(f"#### {sent['summary']}")
        cols = st.columns(2)
        cols[0].markdown("**机会/正面主题**")
        for item in [x for x in str(sent.get("bullish") or "").split("；") if x]:
            cols[0].caption("• " + item)
        if not sent.get("bullish"):
            cols[0].caption("—")
        cols[1].markdown("**风险/负面主题**")
        for item in [x for x in str(sent.get("bearish") or "").split("；") if x]:
            cols[1].caption("• " + item)
        if not sent.get("bearish"):
            cols[1].caption("—")

    st.divider()
    hist = sentiment_history(con, days=30).sort_values("as_of_date")
    if len(hist) >= 2:
        st.markdown("##### 情绪走势(近30个有数据的交易日)")
        cols = [UP if s > 0.1 else DN if s < -0.1 else "#8395a7" for s in hist["score"]]
        fig = go.Figure(go.Scatter(
            x=hist["as_of_date"].astype(str), y=hist["score"], mode="lines+markers",
            line=dict(color="#2b6cb0"), marker=dict(color=cols, size=8),
            hovertemplate="%{x}<br>情绪分 %{y:+.2f}<extra></extra>"))
        fig.add_hline(y=0, line_dash="dot", line_color="#cbd5e0")
        fig.update_layout(height=240, margin=dict(t=10, b=10, l=10, r=10),
                          yaxis=dict(range=[-1, 1], title="情绪分"))
        st.plotly_chart(fig, use_container_width=True)

    nd = con.execute("SELECT max(news_date) FROM news_raw").fetchone()[0]
    news = _news_for_day(con, nd)
    _render_news_feed(news, nd)


@st.fragment(run_every=MONITOR_INTERVAL)
def _live_news_monitor():
    st.markdown("##### 实时新闻监控")
    ctrl = st.columns([0.2, 0.2, 0.6])
    force = ctrl[0].button("立即轮询", use_container_width=True)
    reset = ctrl[1].button("重置基线", use_container_width=True)
    res = _monitor_news_once(force=force, reset=reset)

    status = st.columns(4)
    status[0].metric("监控状态", "运行中", "60秒轮询")
    status[1].metric("本轮新增", len(res["new_rows"]))
    status[2].metric("突发预警", len(res["alerts"]))
    status[3].metric("源新闻", len(res["news"]))

    detail = " · ".join(f"{k}:{v}" for k, v in res["source_counts"].items()) or "暂无新闻"
    st.caption(f"上次检查 {res['at']:%H:%M:%S} · 本轮入库 {res['inserted']} 条 · {detail}")
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

    if res["new_rows"]:
        with st.expander("本轮新增新闻", expanded=True):
            for r in res["new_rows"][:12]:
                ts = ""
                if pd.notna(r.ts):
                    ts = pd.Timestamp(r.ts).strftime("%H:%M")
                title = str(r.title)
                if getattr(r, "url", None):
                    title = f"[{title}]({r.url})"
                st.markdown(f"- {ts + ' · ' if ts else ''}{title}")
                if isinstance(r.summary, str) and r.summary.strip():
                    st.caption("　" + r.summary[:120])


def _render_news_feed(news: pd.DataFrame, nd):
    st.markdown("##### 源新闻流")
    st.caption("当前源: 东财全球财经快讯 + 新闻联播。舆情总结只基于这里展示的入库新闻；更深的外媒、社媒、个股实体识别和突发事件预警需要继续接源。")
    if news is None or news.empty:
        st.caption("暂无入库新闻。")
        return
    label_map = {"em_global": "财经快讯(含海外宏观/外盘)", "cctv": "新闻联播(国内政策面)"}
    st.caption(f"新闻日期 {nd} · 共 {len(news)} 条")
    for code, grp in news.groupby("source"):
        with st.expander(f"{label_map.get(code, code)} · {len(grp)} 条",
                         expanded=(code == "em_global")):
            for r in grp.itertuples():
                t = ""
                if pd.notna(r.ts):
                    try:
                        t = pd.to_datetime(r.ts).strftime("%H:%M")
                    except Exception:  # noqa: BLE001
                        t = ""
                title = str(r.title)
                if getattr(r, "url", None):
                    title = f"[{title}]({r.url})"
                prefix = f"{t} · " if t else ""
                st.markdown(f"- {prefix}{title}")
                if isinstance(r.summary, str) and r.summary.strip() and r.summary.strip() != str(r.title).strip():
                    st.caption("　" + r.summary[:120])


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
        df, num_rows="dynamic", use_container_width=True, key="pos_editor",
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
    if c1.button("💾 保存", type="primary", use_container_width=True):
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
                 use_container_width=True, hide_index=True)
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
        st.plotly_chart(fig, use_container_width=True)
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
    st.plotly_chart(fig, use_container_width=True)


page_sentiment()

st.sidebar.divider()
st.sidebar.caption(f"日期 {date.today().isoformat()}")
