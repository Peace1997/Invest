"""持仓健康度 (ROADMAP Phase 3.2).

Scores each holding on the same trend rubric as the index thermometer, reading
its own price history:
  - stock : daily_bar close
  - otc_fund / bond_fund : fund NAV series (单位净值走势)
  - etf   : daily_bar close

Returns a SignalOutput per holding (score ∈ [-1,+1] + reason). The DecisionEngine
turns score + cost-basis P&L + position weight into an explainable action.
"""
from __future__ import annotations

import pandas as pd

from ..portfolio import Portfolio, Holding
from ..factors.technical import trend_features
from ..factors.valuation import valuation_info
from ..factors.nav import nav_history
from .scoring import score_components
from .base import SignalOutput, ScoreComponent


def _series_for(h: Holding, con, ak_src) -> tuple[pd.Series | None, object]:
    """Return (ascending price/NAV series, as_of date) for a holding."""
    if h.type in ("stock", "etf"):
        if con is None:
            return None, None
        try:
            df = con.execute(
                "SELECT trade_date, close FROM daily_bar_adj WHERE symbol=? ORDER BY trade_date",
                [h.code.zfill(6)],
            ).df()
        except Exception:
            return None, None
        if df is None or df.empty:
            return None, None
        return df["close"], df["trade_date"].iloc[-1]
    if h.type in ("otc_fund", "bond_fund"):
        df = nav_history(con, h.code)   # warehouse first
        if (df is None or df.empty) and ak_src is not None:
            df = ak_src.fund_nav_history(h.code)   # live fallback if not stored
        if df is None or df.empty:
            return None, None
        return df["nav"], df["nav_date"].iloc[-1]
    return None, None


def holding_health(h: Holding, con=None, ak_src=None) -> SignalOutput:
    series, as_of = _series_for(h, con, ak_src)
    f = trend_features(series) if series is not None else None
    val = valuation_info(con, h.code) if con is not None else None
    score, comps, conf = score_components(f, val=val)

    # bond funds: muted score, they're a defensive sleeve not a trend bet
    if h.type == "bond_fund":
        for c in comps:
            c.contribution *= 0.4
        score = max(-1.0, min(1.0, sum(c.contribution for c in comps)))
        comps.insert(0, ScoreComponent(
            "债基调整", 0.0, "防御性债基, 趋势分按 0.4 缩放(不做趋势博弈)", "—"))

    # concise reason = signed contributions of the factors that actually moved it
    reason = " · ".join(
        f"{c.label.split('·')[-1]} {c.contribution:+.2f}"
        for c in comps if c.contribution != 0
    ) or "各项中性, 综合 0"

    direction = "long" if score > 0.15 else "short" if score < -0.15 else "flat"
    return SignalOutput(
        target=h.code,
        signal_name="holding_health",
        score=score,
        direction=direction,
        horizon="mid",
        reason=reason,
        confidence=conf,
        as_of=as_of,
        components=comps,
        metadata={"trend": f.trend_label if f else "数据不足",
                  "type": h.type, "n_obs": f.n if f else 0},
    )


def score_portfolio(pf: Portfolio, con=None, ak_src=None) -> dict[str, SignalOutput]:
    """Health signal for every holding, keyed by code. Lazily inits AkSource if
    the portfolio holds OTC funds and none was supplied."""
    needs_ak = any(h.type in ("otc_fund", "bond_fund") for h in pf.holdings)
    if needs_ak and ak_src is None:
        from ..sources import AkSource
        ak_src = AkSource()
    return {h.code: holding_health(h, con=con, ak_src=ak_src) for h in pf.holdings}
