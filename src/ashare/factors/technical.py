"""Technical factors (Phase 2, lightweight imperative version).

Per ROADMAP we don't build a full Qlib Expression engine — just well-defined
pure functions over a price series (a stock's close, a fund's NAV, or an index
level). Everything is computed from one ascending-by-date `pd.Series` so the
same code scores stocks, funds and indices uniformly.

Honesty: features degrade gracefully — if the series is too short for a window
(e.g. <200 points for MA200) that field stays None rather than being faked.
"""
from __future__ import annotations
from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class TrendFeatures:
    n: int                       # number of observations available
    last: float
    ma20: float | None = None
    ma60: float | None = None
    ma120: float | None = None
    ma200: float | None = None
    ret20: float | None = None   # 20-bar return, %
    ret60: float | None = None
    ret120: float | None = None
    rsi14: float | None = None
    high252: float | None = None
    low252: float | None = None
    dd_from_high: float | None = None   # % below trailing-252 high (<=0)
    range_pos252: float | None = None   # 0..100, where last sits in 252d hi-lo band
    dist_ma60: float | None = None      # % distance of last vs MA60
    dist_ma200: float | None = None     # % distance of last vs MA200
    ann_vol: float | None = None        # annualized volatility, %
    above_ma60: bool | None = None
    above_ma200: bool | None = None

    @property
    def trend_label(self) -> str:
        if self.above_ma60 is None:
            return "数据不足"
        if self.above_ma60 and (self.above_ma200 in (True, None)):
            return "多头"
        if (not self.above_ma60) and (self.above_ma200 is False):
            return "空头"
        return "震荡"


def _rsi(close: pd.Series, period: int = 14) -> float | None:
    if len(close) <= period:
        return None
    delta = close.diff().dropna()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    last_gain, last_loss = gain.iloc[-1], loss.iloc[-1]
    if pd.isna(last_gain) or pd.isna(last_loss):
        return None
    if last_loss == 0:
        return 100.0
    rs = last_gain / last_loss
    return float(100 - 100 / (1 + rs))


def trend_features(prices: pd.Series) -> TrendFeatures | None:
    """Compute trend/momentum features from an ascending price/NAV series."""
    s = pd.Series(prices).dropna().astype(float)
    if len(s) < 2:
        return None
    last = float(s.iloc[-1])
    f = TrendFeatures(n=len(s), last=last)

    for w, attr in ((20, "ma20"), (60, "ma60"), (120, "ma120"), (200, "ma200")):
        if len(s) >= w:
            setattr(f, attr, float(s.tail(w).mean()))
    for w, attr in ((20, "ret20"), (60, "ret60"), (120, "ret120")):
        if len(s) > w:
            setattr(f, attr, float((last / s.iloc[-w - 1] - 1) * 100))

    f.rsi14 = _rsi(s)

    tail252 = s.tail(252)
    f.high252 = float(tail252.max())
    f.low252 = float(tail252.min())
    if f.high252:
        f.dd_from_high = float((last / f.high252 - 1) * 100)
    band = f.high252 - f.low252
    if band > 0:
        f.range_pos252 = float((last - f.low252) / band * 100)

    rets = s.pct_change().dropna().tail(60)
    if len(rets) >= 20:
        f.ann_vol = float(rets.std() * np.sqrt(252) * 100)

    if f.ma60 is not None:
        f.above_ma60 = last >= f.ma60
        f.dist_ma60 = float((last / f.ma60 - 1) * 100)
    if f.ma200 is not None:
        f.above_ma200 = last >= f.ma200
        f.dist_ma200 = float((last / f.ma200 - 1) * 100)
    return f
