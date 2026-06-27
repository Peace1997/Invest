"""Streamlit dashboard — 持仓看板 (ROADMAP Phase 8, TradingView-style).

Reuses the existing layers (no logic forked here):
  portfolio  → 估值     signals → 健康分/温度计     decision → 操作建议
  snapshots  → 收益曲线  factors → 技术指标

Run:  uv run streamlit run src/ashare/ui/app.py
 or:  uv run python -m ashare.cli ui
"""
from __future__ import annotations
import os
from datetime import date

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
from ashare.signals import score_portfolio, index_timing, recommend_stocks
from ashare.decision import generate_advice, compute_price_levels
from ashare.snapshots import load_history, snapshot_portfolio, max_drawdown
from ashare.factors.nav import nav_history
from ashare.risk import portfolio_risk, render_risk
from ashare.rebalance import rebalance_plan, render_rebalance
from ashare.signals.sentiment import latest_sentiment, sentiment_history

st.set_page_config(page_title="A股持仓看板", page_icon="📈", layout="wide")

UP, DN = "#e23b3b", "#1aae5c"   # A股惯例: 红涨绿跌


# ── shared resources (one instance, reused across reruns) ──
@st.cache_resource
def get_cfg():
    return load("config.yaml")


@st.cache_resource
def get_con():
    # read-only: the dashboard never writes, and this keeps its lock from
    # silently blocking CLI write-jobs more than necessary.
    # 刷新窗口(15:15/20:00 cron 跑 `cli daily` 时会独占写锁)里, DuckDB 会拒绝本连接 →
    # 别让整个看板抛栈, 退避重试几次, 仍锁住就友好提示并停渲染(而非红色 Traceback)。
    import time as _time
    cfg = get_cfg()
    path = resolve_path(cfg, "warehouse")
    for attempt in range(4):
        try:
            return open_db(path, read_only=True)
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


# ════════════════════════════ layout ════════════════════════════
bundle = load_bundle()
pf, health, advice, timing, history = bundle

st.sidebar.title("📈 A股持仓看板")
if st.sidebar.button("🔄 刷新数据", use_container_width=True):
    load_bundle.clear()
    get_series.clear()
    st.rerun()
st.sidebar.caption("数据缓存 5 分钟；点刷新立即重取")

if pf is None or not pf.holdings:
    st.warning("未找到 positions.yaml 或没有有效持仓。")
    st.stop()

# market thermometer in sidebar
if timing is not None:
    st.sidebar.divider()
    st.sidebar.metric(f"大盘择时 · {timing.label}", f"score {timing.score:+.2f}")
    st.sidebar.caption(timing.reason)

st.sidebar.divider()
adv_by_code = {a.code: a for a in advice}
nav_options = ["📊 持仓总览", "📰 舆情分析", "💡 操作建议", "🛡 组合风控", "🎯 选股推荐", "🔎 个股速评", "✏️ 持仓管理"]
ranked = sorted(pf.valued, key=lambda h: (health.get(h.code).score if health.get(h.code) else 0),
                reverse=True)
label_to_holding = {}
for h in ranked:
    sig = health.get(h.code)
    dot = "🟢" if (sig and sig.score >= 0.15) else "🔴" if (sig and sig.score <= -0.15) else "⚪"
    lbl = f"{dot} {h.name[:12]}"
    label_to_holding[lbl] = h
    nav_options.append(lbl)

page = st.sidebar.radio("导航", nav_options, label_visibility="collapsed")


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
    st.subheader("📰 舆情分析 · 国内外财经政经新闻")
    con = get_con()
    sent = latest_sentiment(con)
    if not sent:
        st.info("暂无舆情数据 — 服务器每日 20:00 自动生成(`ashare sentiment`/`daily`), 需 LLM 密钥。")
        return
    as_of = str(sent["as_of_date"])[:10]
    score = float(sent["score"])
    if as_of < date.today().isoformat():
        st.warning(f"⚠ 最新舆情停留在 {as_of}(非今日)——今日可能尚未生成, 或新闻源/LLM 异常。"
                   "下方为最近一次分析, 已诚实标注日期。")

    arrow = "🔺" if score > 0.1 else "🔻" if score < -0.1 else "➖"
    c1, c2 = st.columns([0.28, 0.72])
    with c1:
        st.metric(f"市场情绪 {arrow} {sent['label']}", f"{score:+.2f}",
                  help="-1 极空/恐慌 ~ 0 中性 ~ +1 极多/亢奋")
        st.caption(f"基于 {int(sent['n_news'])} 条新闻 · {as_of}")
        st.caption(f"🤖 AI生成({sent.get('model','')}) · 仅供参考非投资建议")
    with c2:
        st.markdown(f"#### {sent['summary']}")
        st.markdown(f"🔺 **利好主题**:{sent.get('bullish') or '—'}")
        st.markdown(f"🔻 **利空主题**:{sent.get('bearish') or '—'}")

    st.divider()
    hist = sentiment_history(con, days=30).sort_values("as_of_date")
    if len(hist) >= 2:
        st.markdown("##### 📈 情绪走势(近30个有数据的交易日)")
        cols = [UP if s > 0.1 else DN if s < -0.1 else "#8395a7" for s in hist["score"]]
        fig = go.Figure(go.Scatter(
            x=hist["as_of_date"].astype(str), y=hist["score"], mode="lines+markers",
            line=dict(color="#2b6cb0"), marker=dict(color=cols, size=8),
            hovertemplate="%{x}<br>情绪分 %{y:+.2f}<extra></extra>"))
        fig.add_hline(y=0, line_dash="dot", line_color="#cbd5e0")
        fig.update_layout(height=240, margin=dict(t=10, b=10, l=10, r=10),
                          yaxis=dict(range=[-1, 1], title="情绪分"))
        st.plotly_chart(fig, use_container_width=True)

    st.markdown("##### 🗞 当日源新闻")
    st.caption("舆情结论即基于下列真实新闻 · 财经快讯含海外宏观/外盘, 新闻联播为国内政策面 · "
               "更深的国际政治/外媒覆盖需再接专门源")
    nd = con.execute("SELECT max(news_date) FROM news_raw").fetchone()[0]
    if nd is None:
        st.caption("暂无入库新闻。")
        return
    news = con.execute(
        "SELECT source, title, summary, ts FROM news_raw WHERE news_date=? "
        "ORDER BY ts DESC NULLS LAST", [nd]).df()
    label_map = {"em_global": "📈 财经快讯(含海外宏观/外盘)", "cctv": "🏛 新闻联播(国内政策面)"}
    st.caption(f"新闻日期 {nd} · 共 {len(news)} 条")
    for code, grp in news.groupby("source"):
        with st.expander(f"{label_map.get(code, code)} · {len(grp)} 条",
                         expanded=(code == "em_global")):
            for r in grp.itertuples():
                t = ""
                if pd.notna(r.ts):
                    try:
                        t = pd.to_datetime(r.ts).strftime("%H:%M ")
                    except Exception:  # noqa: BLE001
                        t = ""
                st.markdown(f"- **{t}**{r.title}")
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
    st.subheader("🎯 选股推荐")
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


if page == "📊 持仓总览":
    page_overview()
elif page == "📰 舆情分析":
    page_sentiment()
elif page == "💡 操作建议":
    page_advice()
elif page == "🛡 组合风控":
    page_risk()
elif page == "🎯 选股推荐":
    page_recommend()
elif page == "🔎 个股速评":
    page_stockeval()
elif page == "✏️ 持仓管理":
    page_positions()
else:
    page_holding(label_to_holding[page])

st.sidebar.divider()
st.sidebar.caption(f"快照日 {date.today().isoformat()} · 红涨绿跌 · 个股实时·基金T-1")
