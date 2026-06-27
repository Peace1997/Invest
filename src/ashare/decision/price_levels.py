"""可解释的买卖价位 (Phase 4).

从日线推导 **参考价位**(非保证): 买入区 / 止盈目标 / 止损价, 全部基于可观察的
结构(均线支撑、近端高低点)+ ATR 波动, 并给出风险报酬比。设计同项目原则: 每个
价位都能说清"为什么是这个价"。

口径(趋势股的常规打法 — 不追高, 回调到支撑分批; 破位止损; 前高/量度止盈):
  买入区  = [MA60, MA20]   回调到均线支撑再买
  止损    = min(MA60, 近60日低点) 再下移一档(ATR/3%)  跌破=趋势破坏
  止盈    = 近一年高点(前高阻力); 若已创新高 → 现价 + 2×ATR(量度延伸)
"""
from __future__ import annotations
from dataclasses import dataclass, field

import pandas as pd


@dataclass
class PriceLevels:
    last: float
    buy_lo: float
    buy_hi: float
    stop: float
    target: float
    status: str                 # 现价相对买入区的位置 + 建议
    rr: float | None = None     # 风险报酬比 (target-last)/(last-stop)
    notes: list[str] = field(default_factory=list)


def _atr(df: pd.DataFrame, n: int = 14) -> float | None:
    close = df["close"]
    if {"high", "low"}.issubset(df.columns) and df["high"].notna().any():
        prev = close.shift(1)
        tr = pd.concat([(df["high"] - df["low"]).abs(),
                        (df["high"] - prev).abs(),
                        (df["low"] - prev).abs()], axis=1).max(axis=1)
    else:  # NAV-only series: approximate range by |Δclose|
        tr = close.diff().abs()
    tr = tr.dropna().tail(n)
    return float(tr.mean()) if len(tr) else None


def compute_price_levels(df: pd.DataFrame) -> PriceLevels | None:
    """df needs at least trade_date + close (high/low optional for ATR)."""
    if df is None or len(df) < 60:
        return None
    close = df["close"].astype(float)
    last = float(close.iloc[-1])
    ma20 = float(close.tail(20).mean())
    ma60 = float(close.tail(60).mean())
    high252 = float(close.tail(252).max())
    low60 = float(close.tail(60).min())
    atr = _atr(df) or (last * 0.03)

    buy_lo, buy_hi = sorted((ma20, ma60))            # 深/浅回调支撑
    stop = round(min(ma60, low60) - atr, 2)          # 结构下方再留一档
    if stop >= last:                                  # 价已破位 → 用 ATR 止损
        stop = round(last - 2 * atr, 2)

    if last < high252 * 0.985:
        target = round(high252, 2)                    # 前高=第一阻力
        tnote = f"第一目标=近一年高点 {target}"
    else:
        target = round(last + 2 * atr, 2)             # 已破前高 → 量度延伸
        tnote = f"已创新高, 量度目标=现价+2×ATR={target}, 之后跟踪MA20移动止盈"

    rr = round((target - last) / (last - stop), 2) if last > stop else None

    if last > buy_hi * 1.04:
        status = f"现价 {last} 高于买入区(+{(last/buy_hi-1)*100:.0f}%), 偏贵 → 等回调, 勿追高"
    elif last >= buy_lo:
        status = f"现价 {last} 在买入区内/附近 → 可分批建仓"
    else:
        status = f"现价 {last} 已跌破买入区下沿(MA60), 趋势存疑 → 谨慎/观望"

    notes = [
        f"买入区 {buy_lo:.2f}~{buy_hi:.2f} (MA60~MA20, 回调到均线支撑分批)",
        f"止损 {stop:.2f} (跌破MA60/近60日低点 {low60:.2f}, 趋势破坏离场)",
        tnote,
    ]
    if rr is not None:
        notes.append(f"风险报酬比 ≈ {rr} (赚{target-last:.2f} : 亏{last-stop:.2f})")
    return PriceLevels(last=last, buy_lo=round(buy_lo, 2), buy_hi=round(buy_hi, 2),
                       stop=stop, target=target, status=status, rr=rr, notes=notes)
