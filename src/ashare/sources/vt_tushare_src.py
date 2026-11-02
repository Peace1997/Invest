"""Tushare via Vibe-Trading 的 MCP 代理 —— **兜底数据源**（2026-10-23 新增）。

═══ 为什么要有这个模块 ═══

2026-10-23 排查 `com.mapeixin.trading.market-refresh` exit 2 时发现两层问题：

1. **原本的 Tushare 兜底是"名存实亡"的。**
   配置的端点 `ts.gyzcloud.top` 对 `index_daily` **静默返回 0 行**（不报错），
   日志里只有 `tushare 指数兜底: 0 行, 覆盖 set()`。于是"兜底"从未真正生效，
   流程直接掉到 akshare。

2. **akshare 在 16:00 / 20:15 这两个定时时点会被限流。**
   `index_zh_a_hist`（80.push2.eastmoney）连失败 3 次，
   备用 `stock_zh_index_daily_em` 同样 RemoteDisconnected 3 次 → index 000300 失败 → exit 2。
   而 11:30 手工测试时两者**都能work** —— 说明是**限流/间歇**，不是接口死亡。

**Vibe-Trading 的代理 `t.xiaodefa.top/mcp`（JSON-RPC + gzip）实测稳定可用**，
本项目直接复用，作为 Tushare 兜底之上的**第三层**。

═══ 口径说明（重要，避免与别处混用）═══
- 本模块返回的 `amount` 单位是**千元**（Tushare 原生口径），与本项目 `_map()` 的
  "千元→元" 换算一致，因此**必须经由 `_map()` 处理**，不要直接使用。
- 只覆盖 `index_daily`；个股/ETF/估值仍走原路径（它们的失败率远低于指数）。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

# Vibe-Trading 的本地代理模块（用户另一项目，路径稳定）
_VT_DIR = Path("/Users/mapeixin/Trading/Vibe-Trading/local/vt")

_loaded = False


def _load():
    """惰性导入 VT 的 tushare_mcp。失败返回 None（不抛，保持调用方可降级）。"""
    global _loaded
    if _loaded:
        return sys.modules.get("tushare_mcp")
    _loaded = True
    if not (_VT_DIR / "tushare_mcp.py").exists():
        log.warning("VT tushare 代理不存在于 %s，跳过该兜底", _VT_DIR)
        return None
    if str(_VT_DIR) not in sys.path:
        sys.path.insert(0, str(_VT_DIR))
    try:
        import tushare_mcp  # noqa: PLC0415

        return tushare_mcp
    except Exception as exc:  # noqa: BLE001
        log.warning("导入 VT tushare 代理失败: %s", exc)
        return None


def index_daily(ts_code: str, start: str, end: str) -> pd.DataFrame | None:
    """拉单个指数的日线（Tushare 原生列名）。

    Args:
        ts_code: 带后缀，如 ``000300.SH``。MCP 口径参数名是 ``ts_code``。
        start / end: ``YYYYMMDD``。

    Returns:
        DataFrame（列含 trade_date/open/high/low/close/vol/amount/pct_chg/change），
        或 None（不可用/失败/空）。
    """
    mod = _load()
    if mod is None:
        return None
    try:
        n, fields, items = mod.call("index_daily", ts_code=ts_code,
                                    start_date=start, end_date=end)
    except Exception as exc:  # noqa: BLE001
        log.warning("VT 代理 index_daily %s 异常: %s", ts_code, exc)
        return None
    if not n:
        log.warning("VT 代理 index_daily %s 无数据: %s", ts_code, items)
        return None
    recs = mod.to_records(fields, items)
    if not recs:
        return None
    return pd.DataFrame(recs)


def available() -> bool:
    """冒烟检查：代理模块是否可导入。"""
    return _load() is not None
