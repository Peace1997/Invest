"""L5 Signal abstraction. The unified output that lets rule/LLM/RL signals fuse.

Matches ROADMAP §4.3. Every signal MUST carry a human-readable `reason` —
that's the explainability contract the whole project is built on.
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class ScoreComponent:
    """One auditable line of a score: how much it moved the total and why."""
    label: str           # e.g. '趋势(均线排列)'
    contribution: float  # signed push on the [-1,+1] score
    detail: str          # the actual numbers + plain-language reading
    cap: str = ""        # e.g. '满分±0.40' — what the max influence is


@dataclass
class SignalOutput:
    target: str          # symbol, 'INDEX:000300', or 'MARKET'
    signal_name: str
    score: float         # -1 (bearish) .. +1 (bullish)
    direction: str       # 'long' | 'short' | 'flat'
    horizon: str         # 'short' (T+5) | 'mid' (T+20) | 'long' (T+60)
    reason: str          # required: why this score
    confidence: float = 0.6   # 0..1, lower when data is thin
    as_of: object | None = None
    components: list[ScoreComponent] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        if self.score >= 0.5:
            return "强多"
        if self.score >= 0.15:
            return "偏多"
        if self.score <= -0.5:
            return "强空"
        if self.score <= -0.15:
            return "偏空"
        return "中性"
