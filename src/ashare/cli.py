"""CLI entry. Usage:

    uv run python -m ashare.cli init                       # init DB schema
    uv run python -m ashare.cli sample                     # closed-loop demo
    uv run python -m ashare.cli backfill-meta              # calendar + instruments
    uv run python -m ashare.cli backfill-all               # full universe (slow)
    uv run python -m ashare.cli daily                      # incremental (in-DB only)
    uv run python -m ashare.cli daily --universe all       # full-market refresh
    uv run python -m ashare.cli today                      # daily briefing report
    uv run python -m ashare.cli live                       # real-time intraday snapshot
    uv run python -m ashare.cli pf                          # 我的持仓日评 (auto-priced)
    uv run python -m ashare.cli ui                          # 启动网页看板 (Streamlit)
    uv run python -m ashare.cli query                      # peek at warehouse
"""
from __future__ import annotations
import argparse
import logging

from .config import load, resolve_path
from .sources import AkSource
from .storage import open_db, init_schema
from .jobs.backfill import backfill_meta, backfill_bars, backfill_northbound
from .jobs.daily_update import daily_update
from .notify.report import render_daily_report
from .notify.live import render_live_report
from .notify.portfolio_report import render_portfolio_report


def _setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_init(cfg, con, src):
    init_schema(con)
    print("schema initialized at", resolve_path(cfg, "warehouse"))


def cmd_sample(cfg, con, src):
    init_schema(con)
    backfill_meta(con, src)
    t = cfg["sample_targets"]
    start = cfg.get("backfill_start", "20240101")
    res = backfill_bars(
        con, src,
        stocks=t["stocks"], etfs=t["etfs"], indices=t["indices"],
        start=start,
        batch_size=cfg["rate_limit"]["batch_size"],
        batch_sleep=cfg["rate_limit"]["batch_sleep"],
    )
    print("sample backfill counts:", {k: v for k, v in res.items() if k != "failed"})
    if res["failed"]:
        print("failures:", res["failed"])
    backfill_northbound(con, src)
    cmd_query(cfg, con, src)


def cmd_backfill_meta(cfg, con, src):
    init_schema(con)
    backfill_meta(con, src)
    print(con.execute(
        "SELECT type, count(*) FROM instruments GROUP BY type ORDER BY 1"
    ).df().to_string(index=False))


def cmd_backfill_all(cfg, con, src):
    init_schema(con)
    backfill_meta(con, src)
    stocks = con.execute("SELECT symbol FROM instruments WHERE type='stock'").df()["symbol"].tolist()
    etfs   = con.execute("SELECT symbol FROM instruments WHERE type='etf'").df()["symbol"].tolist()
    indices = cfg["sample_targets"]["indices"]
    start = cfg.get("backfill_start", "20150101")
    res = backfill_bars(
        con, src, stocks=stocks, etfs=etfs, indices=indices, start=start,
        batch_size=cfg["rate_limit"]["batch_size"],
        batch_sleep=cfg["rate_limit"]["batch_sleep"],
    )
    print({k: v for k, v in res.items() if k != "failed"})
    print("failures:", len(res["failed"]))
    backfill_northbound(con, src)


def cmd_daily(cfg, con, src, args=None):
    init_schema(con)
    universe = getattr(args, "universe", None) or "db"
    result = daily_update(con, src, indices=cfg["sample_targets"]["indices"],
                          universe=universe)

    # 每日舆情 (Claude, best-effort; 无密钥/无新闻/未启用则诚实跳过, 不影响行情入库)
    from .signals.sentiment import generate_sentiment, render_sentiment
    try:
        row = generate_sentiment(con, src, cfg.get("sentiment", {}))
        if row:
            print(render_sentiment(row))
        else:
            print("ℹ 舆情: 跳过(无密钥/无新闻/未启用 — 见 config.yaml sentiment)")
    except Exception as e:
        print(f"⚠ 舆情生成失败(不影响行情入库): {e}")

    if result.get("all_failed") or result.get("stale"):
        import sys
        sys.exit(2)   # 全失败 或 数据未追到最新交易日 → 非0退出, 供 cron/监控捕获


def cmd_alerts(cfg, con, src, args=None):
    """盘中预警: 查持仓+大盘实时价, 触发阈值推送(Server酱). 交易时段用; --force 忽略时段测试。
    只读 DB(不抢看板锁), 状态写 data/alert_state.json。"""
    from .notify.alerts import run_alerts
    force = args is not None and getattr(args, "force", False)
    res = run_alerts(con, cfg.get("alerts", {}), force=force)
    print("盘中预警:", res)


def cmd_premarket(cfg, con, src, args=None):
    """开盘竞价短线分析: 量价异动初筛 + DeepSeek 结合舆情点名 → Server酱推送. 9:25 用; --force 测试。
    只读 DB(不抢看板锁)。⚠ 短线投机, 非验证策略。"""
    from .signals.premarket import run_premarket
    force = args is not None and getattr(args, "force", False)
    res = run_premarket(con, cfg, force=force)
    print("开盘竞价短线:", res)


def cmd_endday(cfg, con, src, args=None):
    """尾盘半小时短线: 尾盘强势量价初筛 + DeepSeek 结合舆情点名 → Server酱推送. 14:30 用; --force 测试。
    只读 DB(不抢看板锁)。⚠ 短线投机, 非验证策略。"""
    from .signals.endday import run_endday
    force = args is not None and getattr(args, "force", False)
    res = run_endday(con, cfg, force=force)
    print("尾盘短线:", res)


def cmd_stock(cfg, con, src, args=None):
    """个股速评: 给代码即时汇总 行情/估值/质量/技术/打分/买卖位 + (可选)LLM解读。
    用法: `ashare stock 600519`; 加 --llm 生成AI综合解读。只读。"""
    from .stockeval import evaluate_stock
    code = getattr(args, "code", None)
    if not code:
        print("用法: ashare stock <6位代码> [--llm]"); return
    with_llm = bool(getattr(args, "llm", False))
    res = evaluate_stock(con, code, with_llm=with_llm, cfg=cfg)
    if not res.get("ok"):
        print("❌", res.get("error")); return
    print(res["text"])
    if with_llm:
        print("\n🤖 AI综合解读:\n" + (res["llm"] or "（LLM 未配置密钥或调用失败, 仅看上面量化数据）"))


def cmd_sentiment(cfg, con, src):
    """生成/刷新今日舆情(东财快讯+新闻联播 → Claude)并打印。需 ANTHROPIC 密钥。"""
    from .signals.sentiment import generate_sentiment, render_sentiment, latest_sentiment
    init_schema(con)
    row = generate_sentiment(con, src, cfg.get("sentiment", {}))
    if row is None:                       # 生成失败 → 退回展示最近一条(诚实标注其日期)
        row = latest_sentiment(con)
    print(render_sentiment(row))


def cmd_query(cfg, con, src):
    print("\n--- calendar (last 5 trading days) ---")
    print(con.execute(
        "SELECT * FROM calendar WHERE trade_date <= current_date ORDER BY trade_date DESC LIMIT 5"
    ).df().to_string(index=False))
    print("\n--- instruments by type ---")
    print(con.execute(
        "SELECT type, count(*) AS n FROM instruments GROUP BY type ORDER BY 1"
    ).df().to_string(index=False))
    print("\n--- daily_bar coverage ---")
    print(con.execute("""
        SELECT type, count(DISTINCT symbol) AS symbols,
               min(trade_date) AS first_dt, max(trade_date) AS last_dt,
               count(*) AS rows
        FROM daily_bar GROUP BY type ORDER BY 1
    """).df().to_string(index=False))
    print("\n--- index_bar coverage ---")
    print(con.execute("""
        SELECT symbol, count(*) AS rows, min(trade_date) AS first_dt, max(trade_date) AS last_dt
        FROM index_bar GROUP BY symbol
    """).df().to_string(index=False))
    print("\n--- last 5 bars of sample stock ---")
    print(con.execute("""
        SELECT * FROM daily_bar
        ORDER BY trade_date DESC LIMIT 5
    """).df().to_string(index=False))


def cmd_pf(cfg, con, src, args=None):
    """我的持仓日评: 自动估值 → 操作建议 → 写回 positions.yaml 注释 → 存历史快照。"""
    from datetime import date
    from pathlib import Path
    from .portfolio import load_positions, value_portfolio, annotate_positions_file
    from .decision import generate_advice
    from .signals import score_portfolio, index_timing
    from .snapshots import snapshot_portfolio, load_history

    init_schema(con)
    pf = load_positions()
    if pf is None or not pf.holdings:
        print(render_portfolio_report())
        return

    no_write = args is not None and getattr(args, "no_write", False)

    # NAV (T-1 settled) → warehouse first, so pricing & health read the DB
    # instead of fetching every fund from eastmoney on each run.
    if not no_write:
        from .ingest.fund_nav import refresh_fund_nav
        try:
            nr = refresh_fund_nav(con, pf)
            if nr["done"]:
                print(f"📥 净值已更新: {len(nr['done'])} 只 (跳过 {len(nr['skipped'])} 已最新)")
        except Exception as e:
            print(f"⚠ 净值更新失败(不影响其余): {e}")

    pf = value_portfolio(pf, con=con, ak_src=src)

    idx = (cfg.get("sample_targets", {}).get("indices") or ["000300"])[0]

    # top up valuation (PE/PB) for stocks + index funds + thermometer (idempotent)
    if not (args is not None and getattr(args, "no_write", False)):
        from .ingest.valuation import refresh_valuation
        try:
            vr = refresh_valuation(con, pf, index_symbol=idx)
            if vr["done"]:
                print(f"📈 估值已更新: {len(vr['done'])} 项 (跳过 {len(vr['skipped'])} 已最新)")
        except Exception as e:
            print(f"⚠ 估值更新失败(不影响其余): {e}")

    health = score_portfolio(pf, con=con, ak_src=src)
    timing = index_timing(con, index_symbol=idx)
    advice = generate_advice(pf, health=health, timing=timing)

    # Persist today's snapshot first, then load full history (incl. today).
    if not no_write:
        snapshot_portfolio(con, pf)
    history = load_history(con)

    text = render_portfolio_report(pf=pf, advice=advice, timing=timing,
                                   health=health, history=history)

    # 组合层风控 + 再平衡(接进日评; 风控>选股>择时)
    from .risk import portfolio_risk, render_risk
    from .rebalance import rebalance_plan, render_rebalance
    text += "\n" + render_risk(portfolio_risk(pf, con)) \
          + "\n" + render_rebalance(rebalance_plan(pf, con)) + "\n"
    print(text)

    if not no_write:
        if annotate_positions_file(pf):
            print("✍  已把现价/市值/浮盈写回 positions.yaml (你只需维护 shares/cost)")
        print(f"💾 已存今日快照到 portfolio_snapshot ({date.today().isoformat()})")

    if args is not None and getattr(args, "save", False):
        out_dir = Path(resolve_path(cfg, "data_dir")) / "reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"portfolio-{date.today().isoformat()}.txt"
        out_path.write_text(text, encoding="utf-8")
        print(f"📄 持仓日评已保存: {out_path}")


def cmd_risk(cfg, con, src):
    """组合层风控: 真实分散度(相关性)/集中度/市场暴露/波动/压力测试。只读。"""
    from .portfolio import load_positions, value_portfolio
    from .risk import portfolio_risk, render_risk
    pf = load_positions()
    if pf is None or not pf.holdings:
        print("无持仓 (positions.yaml 为空)。")
        return
    pf = value_portfolio(pf, con=con, ak_src=src)
    print(render_risk(portfolio_risk(pf, con)))


def cmd_rebalance(cfg, con, src):
    """组合再平衡建议: 风险贡献分解 + 等风险(ERC)参考 → 减超配/补低配。只读。"""
    from .portfolio import load_positions, value_portfolio
    from .rebalance import rebalance_plan, render_rebalance
    pf = load_positions()
    if pf is None or not pf.holdings:
        print("无持仓 (positions.yaml 为空)。")
        return
    pf = value_portfolio(pf, con=con, ak_src=src)
    print(render_rebalance(rebalance_plan(pf, con)))


def cmd_valuation(cfg, con, src):
    """Backfill/refresh PE/PB valuation for holdings + index (force full pull)."""
    from .portfolio import load_positions
    from .ingest.valuation import refresh_valuation
    init_schema(con)
    pf = load_positions()
    if pf is None or not pf.holdings:
        print("无持仓, 仅刷新指数估值。")
        from .ingest.valuation import ingest_index_valuation
        ingest_index_valuation(con, src, "沪深300", store_as="000300")
        return
    idx = (cfg.get("sample_targets", {}).get("indices") or ["000300"])[0]
    res = refresh_valuation(con, pf, index_symbol=idx, force=True)
    print("估值回填:", res)
    print(con.execute(
        "SELECT symbol, count(*) n, min(trade_date) f, max(trade_date) l "
        "FROM valuation_daily GROUP BY symbol ORDER BY symbol").df().to_string(index=False))


def cmd_recommend(cfg, con, src, args=None):
    """推荐指数基金买卖观察 + ≥5 主板个股 + 板块 (规则打分, 可解释)."""
    from datetime import date
    from pathlib import Path
    from .signals import recommend_stocks, recommend_index_funds, recommend_sectors
    from .notify.recommend_report import render_recommendations
    from .portfolio import load_positions
    init_schema(con)
    top = getattr(args, "top", None) or 5
    held = set()
    fund_held = set()
    extra_funds = {}
    pf = load_positions()
    if pf:
        held = {h.code for h in pf.holdings if h.type in ("stock",)}
        fund_held = {h.code for h in pf.holdings if h.type in ("etf", "otc_fund")}
        extra_funds = {h.code: (h.name, h.type) for h in pf.holdings if h.type in ("otc_fund",)}
    style = getattr(args, "style", None) or "momentum"
    if style == "value":
        print("⏳ 个股打分(稳健·价值版: 读库估值 + 亏损剔除)...")
    else:
        print("⏳ 个股打分(趋势预筛 + 估值精排)...")
    print("⏳ 指数基金打分(ETF日线/NAV + 趋势/估值)...")
    funds, fdesc = recommend_index_funds(con, top_n=max(top, 6), held=fund_held, extra_funds=extra_funds)
    stocks, uni = recommend_stocks(con, ak_src=src, top_n=top, exclude=set(), style=style)
    print("⏳ 板块打分(行情源在线时)...")
    sectors, sdesc = recommend_sectors(src, top_n=top)
    text = render_recommendations(stocks, uni, sectors, sdesc, funds=funds, fund_desc=fdesc,
                                  held=held | fund_held)
    print(text)
    if args is not None and getattr(args, "save", False):
        out_dir = Path(resolve_path(cfg, "data_dir")) / "reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        p = out_dir / f"recommend-{date.today().isoformat()}.txt"
        p.write_text(text, encoding="utf-8")
        print(f"📄 已保存: {p}")


def cmd_backtest(cfg, con, src, args=None):
    """回测推荐打分是否真有 edge(胜率/超额/分层单调/IC). 只读, 可与看板共存."""
    from .backtest import run_backtest
    style = getattr(args, "style", None) or "momentum"
    horizon = getattr(args, "horizon", None) or 60
    years = getattr(args, "years", None) or 3
    top = getattr(args, "top", None) or 12
    print(f"⏳ 回测 {style} · 持有{horizon}个交易日 · 近{years}年 · 沪深300 universe(首次需1-2分钟)...")
    res = run_backtest(con, style=style, horizon=horizon, years_back=years, top_n=top)
    print(res["text"])


def cmd_backtest_short(cfg, con, src, args=None):
    """短线策略回测: 日线粗验证(竞价/尾盘) + 尾盘分钟精确版(真实14:30, 仅已落库个股). 只读."""
    from .backtest_shortterm import run_shortterm_backtest, run_endday_backtest_minute
    years = getattr(args, "years", None) or 3
    freq = getattr(args, "freq", None) or (cfg.get("minute", {}) or {}).get("freq", "5min")
    print(f"⏳ 短线日线粗验证(近{years}年, 主板60/00)...")
    print(run_shortterm_backtest(con, years_back=years)["text"])
    print(f"\n⏳ 尾盘分钟精确版({freq}, 真实14:30入场, 覆盖=已 backfill-min 的个股)...")
    print(run_endday_backtest_minute(con, years_back=years, freq=freq)["text"])


def cmd_value_backfill(cfg, con, src, args=None):
    """回填稳健·价值版所需数据: 质量(全主板, 一次调用) + 估值(PE/PB, baidu 逐只).
    默认估值仅沪深300(~10-15min); --full 全主板估值(~数小时). 质量始终全主板. 幂等."""
    from .ingest.valuation import backfill_value_universe
    from .ingest.fundamentals import ingest_market_performance, latest_annual
    init_schema(con)
    force = getattr(args, "force", False)
    full = getattr(args, "full", False)
    rd = latest_annual()
    print(f"⏳ 回填全主板基本面/质量(业绩报表 {rd}, 一次调用)...")
    try:
        n = ingest_market_performance(con, rd)
        print(f"✅ 质量已落库: {n} 只 (ROE/增速/毛利率/行业)")
    except Exception as e:
        print(f"⚠ 质量回填失败(不影响估值): {e}")
    backfill_value_universe(con, force=force, full=full)


def _minute_symbols(cfg, con) -> list[str]:
    """分钟落库标的 = config minute.watchlist + 持仓个股(positions.yaml). 仅个股。"""
    mcfg = cfg.get("minute", {}) or {}
    syms = {str(c).zfill(6) for c in (mcfg.get("watchlist") or [])}
    try:
        from .portfolio import load_positions
        pf = load_positions()
        if pf:
            syms |= {h.code.zfill(6) for h in pf.holdings if h.type == "stock"}
    except Exception:  # noqa: BLE001 - 无持仓文件不致命
        pass
    return sorted(syms)


def cmd_backfill_min(cfg, con, src, args=None):
    """回填分钟行情(Tushare stk_mins, 仅个股) → minute_bar. 默认 5min · 持仓+自选 · 近1年.
    用法: ashare backfill-min [--freq 5min] [--symbols 600519,000001] [--start 20240101] [--end 20250601]"""
    from datetime import date, timedelta
    from .ingest.tushare_minute import ingest_minute_bars, DEFAULT_FREQ
    init_schema(con)
    mcfg = cfg.get("minute", {}) or {}
    freq = getattr(args, "freq", None) or mcfg.get("freq", DEFAULT_FREQ)
    raw = getattr(args, "symbols", None)
    syms = [s.strip().zfill(6) for s in raw.split(",") if s.strip()] if raw else _minute_symbols(cfg, con)
    if not syms:
        print("无分钟标的: config.yaml minute.watchlist 为空且无持仓。用 --symbols 指定。")
        return
    end = getattr(args, "end", None) or date.today().strftime("%Y%m%d")
    start = getattr(args, "start", None) or (
        date.today() - timedelta(days=int(mcfg.get("history_days", 365)))).strftime("%Y%m%d")
    print(f"⏳ 分钟回填 {freq} · {len(syms)}只个股 · {start}→{end} (按窗口分块, 仅个股)...")
    res = ingest_minute_bars(con, syms, start, end, freq=freq)
    print(f"✅ 完成: 成功{res['symbols']}只 · {res['rows']}行 · 失败{len(res['failed'])}只")
    for c, m in res["failed"][:10]:
        print(f"  ❌ {c}  {m}")


def cmd_min_update(cfg, con, src, args=None):
    """分钟增量更新(近 update_days 天, 默认5) → minute_bar(幂等). 收盘后/盘后跑。"""
    from datetime import date, timedelta
    from .ingest.tushare_minute import ingest_minute_bars, DEFAULT_FREQ
    init_schema(con)
    mcfg = cfg.get("minute", {}) or {}
    freq = getattr(args, "freq", None) or mcfg.get("freq", DEFAULT_FREQ)
    syms = _minute_symbols(cfg, con)
    if not syms:
        print("无分钟标的(config.yaml minute.watchlist 为空且无持仓)。")
        return
    days = int(mcfg.get("update_days", 5))
    end = date.today().strftime("%Y%m%d")
    start = (date.today() - timedelta(days=days)).strftime("%Y%m%d")
    print(f"⏳ 分钟增量 {freq} · {len(syms)}只 · 近{days}天 ({start}→{end})...")
    res = ingest_minute_bars(con, syms, start, end, freq=freq)
    print(f"✅ 完成: 成功{res['symbols']}只 · {res['rows']}行 · 失败{len(res['failed'])}只")


def cmd_ui(cfg, con, src):
    """Launch the Streamlit dashboard (browser-based 持仓看板)."""
    import subprocess, sys
    from pathlib import Path
    app = Path(__file__).with_name("ui") / "app.py"
    print(f"🚀 启动看板: http://localhost:8501  (Ctrl-C 停止)")
    con.close()  # let streamlit's own process own the DB
    subprocess.run([sys.executable, "-m", "streamlit", "run", str(app),
                    "--server.headless", "true", "--browser.gatherUsageStats", "false"])


def cmd_live(cfg, con, src):
    t = cfg.get("sample_targets", {})
    indices = t.get("indices") or ["000300"]
    watchlist = (t.get("stocks") or []) + (t.get("etfs") or [])
    print(render_live_report(index_symbols=indices, watchlist=watchlist))


def cmd_today(cfg, con, src, args=None):
    from datetime import date
    from pathlib import Path
    idx = (cfg.get("sample_targets", {}).get("indices") or ["000300"])[0]
    watchlist = (cfg.get("sample_targets", {}).get("stocks") or []) + \
                (cfg.get("sample_targets", {}).get("etfs") or [])
    text = render_daily_report(con, index_symbol=idx, watchlist=watchlist)
    print(text)
    if args is not None and getattr(args, "save", False):
        data_dir = Path(resolve_path(cfg, "data_dir"))
        out_dir = data_dir / "reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{date.today().isoformat()}.txt"
        out_path.write_text(text, encoding="utf-8")
        print(f"\n📄 报告已保存: {out_path}")


CMDS = {
    "init": cmd_init,
    "sample": cmd_sample,
    "backfill-meta": cmd_backfill_meta,
    "backfill-all": cmd_backfill_all,
    "backfill-min": cmd_backfill_min,
    "min-update": cmd_min_update,
    "daily": cmd_daily,
    "today": cmd_today,
    "live": cmd_live,
    "ui": cmd_ui,
    "valuation": cmd_valuation,
    "value-backfill": cmd_value_backfill,
    "backtest": cmd_backtest,
    "backtest-short": cmd_backtest_short,
    "recommend": cmd_recommend,
    "pf": cmd_pf,
    "risk": cmd_risk,
    "rebalance": cmd_rebalance,
    "sentiment": cmd_sentiment,
    "alerts": cmd_alerts,
    "premarket": cmd_premarket,
    "endday": cmd_endday,
    "stock": cmd_stock,
    "query": cmd_query,
}


def main():
    _setup_logging()
    p = argparse.ArgumentParser(prog="ashare")
    p.add_argument("cmd", choices=list(CMDS.keys()))
    p.add_argument("code", nargs="?", default=None, help="for `stock`: 6位股票代码")
    p.add_argument("--llm", action="store_true", help="for `stock`: 额外生成 AI 综合解读")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--universe", choices=["db", "all"], default=None,
                   help="for `daily`: 'db' (default) updates only in-DB symbols; "
                        "'all' refreshes the full market (slow).")
    p.add_argument("--save", action="store_true",
                   help="for `today`/`pf`: also write report to data/reports/")
    p.add_argument("--no-write", action="store_true",
                   help="for `pf`: skip writing positions.yaml annotations and the daily snapshot (read-only)")
    p.add_argument("--top", type=int, default=None,
                   help="for `recommend`/`backtest`: number of top stocks (default 5/12)")
    p.add_argument("--horizon", type=int, default=None,
                   help="for `backtest`: holding period in trading days (default 60)")
    p.add_argument("--years", type=int, default=None,
                   help="for `backtest`: how many years back to test (default 3)")
    p.add_argument("--style", choices=["momentum", "value", "leader", "reversal"], default=None,
                   help="for `recommend`: 'momentum'(强势股) / 'value'(稳健·价值版) / 'leader'(自上而下·龙头版) / 'reversal'(反转·超跌版); for `backtest` 同样支持 reversal")
    p.add_argument("--force", action="store_true",
                   help="for `value-backfill`: re-pull valuation even if already fresh")
    p.add_argument("--full", action="store_true",
                   help="for `value-backfill`: backfill valuation for the FULL main board "
                        "(~hours) instead of just 沪深300 (quality is always full-board)")
    p.add_argument("--freq", choices=["1min", "5min", "15min", "30min", "60min"],
                   default=None, help="for `backfill-min`/`min-update`: 分钟频率 (默认读 config minute.freq)")
    p.add_argument("--symbols", default=None,
                   help="for `backfill-min`: 逗号分隔的6位个股代码; 不传则用持仓+config watchlist")
    p.add_argument("--start", default=None, help="for `backfill-min`: 起始日 YYYYMMDD")
    p.add_argument("--end", default=None, help="for `backfill-min`: 结束日 YYYYMMDD (默认今天)")
    args = p.parse_args()

    cfg = load(args.config)
    db_path = resolve_path(cfg, "warehouse")
    # read-only commands can run alongside an open dashboard; write commands need
    # exclusive access — give a friendly hint instead of a lock stack trace.
    read_only_cmds = {"ui", "query", "today", "live", "backtest", "backtest-short", "risk", "rebalance", "alerts", "premarket", "endday", "stock"}
    ro = args.cmd in read_only_cmds
    try:
        con = open_db(db_path, read_only=ro)
    except Exception as e:
        if "lock" in str(e).lower():
            print("❌ 数据库被占用（多半是看板 `ui` 还开着）。\n"
                  "   写入类命令(pf/daily/valuation/backfill)需要独占, 请先关掉看板再跑;\n"
                  "   只读看数据可用: query / today / live / recommend。")
            import sys; sys.exit(3)
        raise
    src = AkSource(
        per_call_sleep=cfg["rate_limit"]["per_call_sleep"],
        max_retries=cfg["rate_limit"]["max_retries"],
    )
    try:
        fn = CMDS[args.cmd]
        # Pass args only to commands that opt in (via 4-arg signature).
        import inspect
        if len(inspect.signature(fn).parameters) == 4:
            fn(cfg, con, src, args)
        else:
            fn(cfg, con, src)
    finally:
        con.close()


if __name__ == "__main__":
    main()
