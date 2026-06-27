"""板块(行业)推荐 (ROADMAP Phase 3.2 / 3.3).

Scores 东财行业板块 on the same trend rubric (均线/动量/RSI/年内位置) from each
board's index history, and returns the strongest sectors with a breakdown.

Robustness: the eastmoney 行业板块 cluster is intermittently unavailable; on
failure we return ([], reason) so the caller reports the gap honestly instead of
fabricating sector calls. No valuation dim here yet (board PE source TBD).
"""
from __future__ import annotations
import logging

from ..factors.technical import trend_features
from .scoring import score_components
from .base import SignalOutput

log = logging.getLogger(__name__)


def recommend_sectors(ak_src, top_n: int = 5, start: str = "20210101",
                      max_boards: int | None = None) -> tuple[list[SignalOutput], str]:
    # 东财行业接口常断连 → 自动 fallback 到同花顺(独立 host)。
    boards, hist_fn, src_name = None, None, None
    try:
        boards = ak_src.industry_boards()
        hist_fn = lambda nm: ak_src.industry_board_hist(nm, start=start)
        src_name = "东财"
    except Exception:
        try:
            boards = ak_src.industry_boards_ths()
            hist_fn = lambda nm: ak_src.industry_board_hist_ths(nm, start=start)
            src_name = "同花顺"
        except Exception as e:
            return [], f"行业板块源(东财/同花顺)均暂不可用({type(e).__name__})，稍后重试"

    names = boards["name"].tolist()
    if max_boards:
        names = names[:max_boards]
    today_pct = {r["name"]: r.get("pct") for _, r in boards.iterrows()}

    scored: list[SignalOutput] = []
    n_fail = 0
    for nm in names:
        try:
            h = hist_fn(nm)
        except Exception:
            n_fail += 1
            continue
        if h is None or len(h) < 60:
            continue
        f = trend_features(h["close"])
        score, comps, conf = score_components(f)
        reason = " · ".join(f"{c.label.split('·')[-1]} {c.contribution:+.2f}"
                            for c in comps if c.contribution != 0) or "中性"
        scored.append(SignalOutput(
            target=f"SECTOR:{nm}", signal_name="sector_rank", score=score,
            direction="long" if score > 0.15 else "flat", horizon="mid",
            reason=reason, confidence=conf, as_of=h["trade_date"].iloc[-1],
            components=comps,
            metadata={"name": nm, "today_pct": today_pct.get(nm)}))

    if not scored:
        return [], "行业板块历史拉取失败(行情源断连)，稍后重试"
    scored.sort(key=lambda s: s.score, reverse=True)
    desc = f"{src_name}行业板块·趋势打分(共评 {len(scored)} 个" + (f", {n_fail}个拉取失败)" if n_fail else ")")
    return scored[:top_n], desc
