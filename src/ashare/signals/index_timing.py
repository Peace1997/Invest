"""指数温度计 (ROADMAP Phase 3.1).

Currently a trend-based thermometer on index_bar. Valuation percentile / ERP /
northbound overlays are Phase 1.3+ and will be added as those tables land — we
deliberately score only on what we have rather than faking a valuation read.
"""
from __future__ import annotations

import pandas as pd

from ..factors.technical import trend_features
from ..factors.valuation import valuation_info
from .scoring import score_components
from .base import SignalOutput


def index_timing(con, index_symbol: str = "000300") -> SignalOutput | None:
    if con is None:
        return None
    try:
        df = con.execute(
            "SELECT trade_date, close FROM index_bar WHERE symbol=? ORDER BY trade_date",
            [str(index_symbol).zfill(6)],
        ).df()
    except Exception:
        return None
    if df is None or len(df) < 20:
        return None

    f = trend_features(df["close"])
    val = valuation_info(con, str(index_symbol).zfill(6))
    score, comps, conf = score_components(f, val=val)
    as_of = df["trade_date"].iloc[-1]
    reason = " · ".join(
        f"{c.label.split('·')[-1]} {c.contribution:+.2f}"
        for c in comps if c.contribution != 0
    ) or "各项中性"
    direction = "long" if score > 0.15 else "short" if score < -0.15 else "flat"
    return SignalOutput(
        target=f"INDEX:{index_symbol}",
        signal_name="index_timing",
        score=score,
        direction=direction,
        horizon="long",
        reason=f"{reason} (截至{str(as_of)[:10]})",
        confidence=conf,
        as_of=as_of,
        components=comps,
        metadata={"last": f.last if f else None,
                  "ma60": f.ma60 if f else None,
                  "ma200": f.ma200 if f else None},
    )
