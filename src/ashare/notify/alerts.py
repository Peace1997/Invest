"""盘中预警 — 交易时段每隔几分钟查持仓+大盘实时价, 触发阈值就推送(Server酱)。

不打扰原则: 每个(持仓,规则)每天最多推一次(状态存 data/alert_state.json), 不刷屏。
只在交易日的交易时段生效(shell 脚本粗门控 + 这里查 calendar 表 + 精确时段)。
实时价走腾讯(免费, 0 token)。只读 DB, 不抢看板的写锁。规则提示, 非投资指令。
"""
from __future__ import annotations
import json
import logging
from datetime import date, datetime, time as dtime
from pathlib import Path

from ..portfolio import load_positions, value_portfolio
from ..sources import TencentSource
from .push import send_push

log = logging.getLogger(__name__)
_STATE = Path(__file__).resolve().parents[3] / "data" / "alert_state.json"


def _load_fired(today: str) -> set[str]:
    if _STATE.exists():
        try:
            d = json.loads(_STATE.read_text(encoding="utf-8"))
            if d.get("date") == today:
                return set(d.get("fired", []))
        except Exception:  # noqa: BLE001
            pass
    return set()


def _save_fired(today: str, fired: set[str]) -> None:
    _STATE.parent.mkdir(parents=True, exist_ok=True)
    _STATE.write_text(json.dumps({"date": today, "fired": sorted(fired)},
                                 ensure_ascii=False), encoding="utf-8")


def _is_trading_now() -> bool:
    t = datetime.now().time()
    return (dtime(9, 30) <= t <= dtime(11, 30)) or (dtime(13, 0) <= t <= dtime(15, 0))


def run_alerts(con, cfg: dict | None = None, force: bool = False) -> dict:
    cfg = cfg or {}
    if not cfg.get("enabled", True):
        return {"skipped": "disabled"}
    today = date.today()
    if not force:
        if not con.execute("SELECT 1 FROM calendar WHERE trade_date=?", [today]).fetchone():
            return {"skipped": "non-trading-day"}
        if not _is_trading_now():
            return {"skipped": "off-hours"}

    drop = float(cfg.get("drop_pct", 5.0))
    surge = float(cfg.get("surge_pct", 7.0))
    stoploss = float(cfg.get("stoploss_pct", 15.0))
    idx_drop = float(cfg.get("index_drop_pct", 2.0))

    pf = load_positions()
    if pf is None or not pf.holdings:
        return {"skipped": "no-positions"}
    src = TencentSource()
    pf = value_portfolio(pf, con=con)        # 场内个股/ETF 走腾讯实时

    fired = _load_fired(today.isoformat())
    new_fired = set(fired)
    alerts = []

    def _add(key, line):
        if key not in new_fired:
            new_fired.add(key)
            alerts.append(line)

    for h in pf.valued:
        if h.type not in ("stock", "etf"):
            continue
        tp, pl = h.today_pct, h.pnl_pct
        nm = h.name[:12]
        if tp is not None and tp <= -drop:
            _add(f"{h.code}:drop", f"🔻 **{nm}** 急跌 **{tp:+.1f}%**(现价 {h.price:g})")
        if tp is not None and tp >= surge:
            _add(f"{h.code}:surge", f"🔺 **{nm}** 急涨 **{tp:+.1f}%**(现价 {h.price:g})")
        if pl is not None and pl <= -stoploss:
            _add(f"{h.code}:stop", f"🛑 **{nm}** 触及止损线, 累计浮亏 **{pl:+.1f}%**")

    try:
        idf = src.spot(["000300"], index_symbols={"000300"})
        if not idf.empty and idf.iloc[0]["pct_chg"] is not None:
            ip = float(idf.iloc[0]["pct_chg"])
            if ip <= -idx_drop:
                _add("index:drop", f"📉 **沪深300 {ip:+.1f}%**, 系统性回调, 注意整体风险")
    except Exception as e:  # noqa: BLE001
        log.warning("大盘实时取价失败: %s", e)

    if not alerts:
        _save_fired(today.isoformat(), new_fired)
        return {"alerts": 0}

    title = f"⚠ 盘中预警 {len(alerts)} 条 · {datetime.now():%H:%M}"
    body = ("\n\n".join(alerts) +
            f"\n\n———\n阈值 急跌≤-{drop:.0f}% 急涨≥+{surge:.0f}% "
            f"止损≤-{stoploss:.0f}% 大盘≤-{idx_drop:.0f}% · 规则提示, 非投资指令")
    ok = send_push(title, body, cfg)
    if ok:                                   # 推送失败则不记 fired, 下次重试
        _save_fired(today.isoformat(), new_fired)
    return {"alerts": len(alerts), "pushed": ok, "lines": alerts}
