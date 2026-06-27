"""Portfolio holdings: load positions.yaml and value them honestly.

On-exchange ETFs/stocks are priced live via Tencent. Off-exchange (OTC) funds
need NAV (Phase 1, not built yet) — those are surfaced as "NAV待接入" rather
than guessed, so the report never fabricates a value we don't have.
"""
from __future__ import annotations
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import yaml
import pandas as pd

from .sources import TencentSource, AkSource
from .factors.nav import nav_latest

_ON_EXCHANGE = {"etf", "stock"}
_OTC = {"otc_fund", "bond_fund"}


@dataclass
class Holding:
    code: str
    name: str
    type: str
    shares: float
    cost: float
    # filled by valuation:
    price: float | None = None
    prev_close: float | None = None
    priceable: bool = False
    note: str = ""
    price_date: object | None = None   # NAV date for funds / quote date for stocks
    price_kind: str = ""               # '实时' | '净值T-1' etc

    @property
    def market_value(self) -> float | None:
        if self.price is None or not self.shares:
            return None
        return self.price * self.shares

    @property
    def cost_value(self) -> float | None:
        if not self.cost or not self.shares:
            return None
        return self.cost * self.shares

    @property
    def pnl(self) -> float | None:
        mv, cv = self.market_value, self.cost_value
        return None if (mv is None or cv is None) else mv - cv

    @property
    def pnl_pct(self) -> float | None:
        if not self.cost or self.price is None:
            return None
        return (self.price / self.cost - 1) * 100

    @property
    def today_pnl(self) -> float | None:
        if self.price is None or self.prev_close is None or not self.shares:
            return None
        return (self.price - self.prev_close) * self.shares

    @property
    def today_pct(self) -> float | None:
        if self.price is None or self.prev_close is None or not self.prev_close:
            return None
        return (self.price / self.prev_close - 1) * 100

    @property
    def today_pnl_amount(self) -> float | None:
        return self.today_pnl

    @property
    def category(self) -> str:
        """Coarse asset bucket for allocation analysis (keyword-based)."""
        n = self.name
        if self.type in ("bond_fund",) or "债" in n:
            return "债券"
        if self.type == "stock":
            return "个股"
        if "QDII" in n or any(k in n for k in (
                "纳斯达克", "纳指", "标普", "全球", "海外", "恒生", "德国",
                "日经", "法国", "美国", "亚太")):
            return "海外QDII"
        if any(k in n for k in ("沪深300", "中证500", "中证1000", "中证800", "上证50", "A500")):
            return "宽基指数"
        if any(k in n for k in ("红利", "低波")):
            return "红利"
        # otherwise a sector/thematic fund
        return "主题行业"


@dataclass
class Portfolio:
    holdings: list[Holding] = field(default_factory=list)
    cash: float = 0.0

    @property
    def valued(self) -> list[Holding]:
        return [h for h in self.holdings if h.market_value is not None]

    @property
    def unvalued(self) -> list[Holding]:
        return [h for h in self.holdings if h.market_value is None]

    @property
    def total_market_value(self) -> float:
        return sum(h.market_value for h in self.valued) + self.cash

    @property
    def total_cost_value(self) -> float | None:
        cvs = [h.cost_value for h in self.valued if h.cost_value is not None]
        return sum(cvs) if cvs else None

    @property
    def total_pnl(self) -> float | None:
        pnls = [h.pnl for h in self.valued if h.pnl is not None]
        return sum(pnls) if pnls else None

    @property
    def total_today_pnl(self) -> float | None:
        tps = [h.today_pnl for h in self.valued if h.today_pnl is not None]
        return sum(tps) if tps else None

    def weight(self, h: Holding) -> float | None:
        tot = self.total_market_value
        mv = h.market_value
        if mv is None or tot <= 0:
            return None
        return mv / tot * 100

    def by_category(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for h in self.valued:
            out[h.category] = out.get(h.category, 0) + (h.market_value or 0)
        if self.cash:
            out["现金"] = self.cash
        return out


def load_positions(path: str | Path = "positions.yaml") -> Portfolio | None:
    p = Path(path)
    if not p.exists():
        return None
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    holdings = []
    for row in data.get("positions", []):
        code = str(row.get("code", "")).strip()
        # Skip unfilled placeholder rows entirely (don't show noise).
        if code in ("", "FILL", "?") and not row.get("shares"):
            continue
        holdings.append(Holding(
            code=code,
            name=row.get("name", code),
            type=row.get("type", "etf"),
            shares=float(row.get("shares") or 0),
            cost=float(row.get("cost") or 0),
        ))
    return Portfolio(holdings=holdings, cash=float(data.get("cash") or 0))


def value_portfolio(pf: Portfolio,
                    src: TencentSource | None = None,
                    ak_src: AkSource | None = None,
                    con=None) -> Portfolio:
    """Auto-update prices: on-exchange via Tencent (real-time), OTC funds via
    published NAV (T-1). Caller only maintains shares; pricing is automatic.

    NAV reads the `fund_nav` warehouse when `con` is given (instant); only funds
    missing from the DB fall back to a live eastmoney fetch."""
    src = src or TencentSource()

    # On-exchange: live quotes
    on_ex = [h for h in pf.holdings if h.type in _ON_EXCHANGE and h.code.isdigit()]
    quotes = {}
    if on_ex:
        df = src.spot([h.code for h in on_ex])
        if not df.empty:
            quotes = {r["symbol"]: r for _, r in df.iterrows()}

    # OTC funds: NAV is T-1 settled → read the warehouse first. Only funds the DB
    # doesn't have need a live fetch (parallel, eastmoney is often ~4s/fund); the
    # common case touches no fund network at all.
    otc = [h for h in pf.holdings if h.type in _OTC and h.code.isdigit()]
    db_nav = {h.code: nav_latest(con, h.code) for h in otc} if con is not None else {}
    missing = [h.code for h in otc if not db_nav.get(h.code)]
    if missing:
        ak_src = ak_src or AkSource()
        with ThreadPoolExecutor(max_workers=min(4, len(missing))) as ex:
            list(ex.map(ak_src.fund_nav_history, missing))

    for h in pf.holdings:
        if h.type in _ON_EXCHANGE and h.code in quotes:
            q = quotes[h.code]
            h.price = q["price"]
            h.prev_close = q["prev_close"]
            h.priceable = True
            h.price_date = q.get("quote_time")
            h.price_kind = "实时"
        elif h.type in _ON_EXCHANGE:
            h.note = "实时报价未取到"
        elif h.type in _OTC:
            nav = db_nav.get(h.code) or (ak_src.fund_nav_latest(h.code) if ak_src else None)
            if nav:
                h.price = nav["nav"]
                h.price_date = nav["nav_date"]
                h.price_kind = "净值"
                h.priceable = True
                # derive prev close from daily growth pct for today's change
                if nav["daily_pct"] is not None and (1 + nav["daily_pct"] / 100) != 0:
                    h.prev_close = nav["nav"] / (1 + nav["daily_pct"] / 100)
            else:
                h.note = "净值未取到"
        else:
            h.note = f"未知类型 {h.type}"
    return pf


# ── write computed values back into positions.yaml as inline annotations ──
_ANNOT = "# ➤"
_CODE_RE = re.compile(r'code:\s*"?(\d{4,6})"?')


def _annot_text(h: Holding, pf: Portfolio) -> str:
    """One-line live annotation for a holding (regenerated each run)."""
    if h.market_value is None:
        if h.price is not None and not h.shares:
            return f"现价{h.price:g} | 待填累计份额(定投中)"
        return f"估值失败({h.note})" if h.note else "估值失败"
    parts = [f"现价{h.price:g}", f"市值{h.market_value:,.0f}元"]
    if h.pnl is not None and h.pnl_pct is not None:
        parts.append(f"浮盈{h.pnl:+,.0f}({h.pnl_pct:+.1f}%)")
    if h.today_pct is not None:
        parts.append(f"今{h.today_pct:+.1f}%")
    w = pf.weight(h)
    if w is not None:
        parts.append(f"权重{w:.1f}%")
    return " | ".join(parts)


_VALID_TYPES = ("etf", "stock", "otc_fund", "bond_fund")


def _fmt_num(x) -> str:
    """No trailing-zero noise: integers stay integers, else plain decimal (no sci)."""
    f = float(x)
    return str(int(f)) if f == int(f) else f"{f:.6f}".rstrip("0").rstrip(".")


def _holding_line(r: dict) -> str:
    name = str(r["name"]).replace('"', "'")
    return (f'  - {{code: "{r["code"]}", name: "{name}", type: {r["type"]}, '
            f'shares: {_fmt_num(r["shares"])}, cost: {_fmt_num(r["cost"])}}}')


def save_positions(rows: list[dict], cash: float,
                   path: str | Path = "positions.yaml") -> None:
    """Surgically rewrite holding lines in positions.yaml from edited `rows`.

    Mirrors `annotate_positions_file`'s philosophy: edit only the holding lines,
    preserve every comment — the header help block, section dividers, and the
    commented-out QDII placeholders the user is staging. Existing holdings keep
    their position (annotation dropped → regenerated nightly); deleted codes are
    removed; new codes are inserted after the last active holding. Raises
    ValueError on bad input so the caller can surface it (never writes garbage).
    """
    cleaned: list[dict] = []
    seen: set[str] = set()
    for r in rows:
        raw = r.get("code")
        code = "" if raw is None or pd.isna(raw) else str(raw).strip()
        if not code:                         # blank/placeholder editor row → skip
            continue
        if not (code.isdigit() and len(code) == 6):
            raise ValueError(f"代码必须是6位数字: {code!r}")
        if code in seen:
            raise ValueError(f"重复代码: {code}")
        seen.add(code)
        typ = str(r.get("type") or "").strip()
        if typ not in _VALID_TYPES:
            raise ValueError(f"{code} 的类型须为 {'/'.join(_VALID_TYPES)}: {typ!r}")
        try:
            shares = 0.0 if pd.isna(r.get("shares")) else float(r.get("shares") or 0)
            cost = 0.0 if pd.isna(r.get("cost")) else float(r.get("cost") or 0)
        except (TypeError, ValueError):
            raise ValueError(f"{code} 的份额/成本必须是数字")
        if shares < 0 or cost < 0:
            raise ValueError(f"{code} 的份额/成本不能为负")
        cleaned.append({"code": code, "name": str(r.get("name") or code).strip(),
                        "type": typ, "shares": shares, "cost": cost})

    by_code = {r["code"]: r for r in cleaned}
    p = Path(path)
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else ["positions:"]
    out: list[str] = []
    written: set[str] = set()
    last_active = None       # out-index of last kept holding (where new rows go)
    cash_done = False
    for line in lines:
        stripped = line.lstrip()
        m = _CODE_RE.search(line)
        if m and stripped.startswith("- {") and not stripped.startswith("#"):
            code = m.group(1).zfill(6)
            if code in by_code:
                out.append(_holding_line(by_code[code]))
                written.add(code)
                last_active = len(out) - 1
            # code absent from edit → user deleted it → drop the line
            continue
        if re.match(r"^\s*cash:\s", line):
            out.append(f"cash: {_fmt_num(cash)}")
            cash_done = True
            continue
        out.append(line)

    new = [_holding_line(by_code[c]) for c in by_code if c not in written]
    if new:
        if last_active is not None:
            at = last_active + 1
        else:
            pi = next((i for i, l in enumerate(out) if l.startswith("positions:")), None)
            at = (pi + 1) if pi is not None else len(out)
        out[at:at] = new
    if not cash_done:
        out.append(f"cash: {_fmt_num(cash)}")
    p.write_text("\n".join(out) + "\n", encoding="utf-8")


def annotate_positions_file(pf: Portfolio, path: str | Path = "positions.yaml") -> bool:
    """Rewrite positions.yaml in place, appending/refreshing a `# ➤ …` live
    annotation on each holding line (现价/市值/浮盈/今日/权重).

    Idempotent: strips any prior `# ➤ …` before re-appending, so the user's own
    field edits and other comments are preserved. The structured YAML fields
    (shares/cost) are never touched — the user keeps maintaining those.
    """
    p = Path(path)
    if not p.exists():
        return False
    by_code = {h.code: h for h in pf.holdings}
    lines = p.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    for line in lines:
        m = _CODE_RE.search(line)
        if m and line.lstrip().startswith("-"):
            base = line.split(_ANNOT)[0].rstrip()
            h = by_code.get(m.group(1).zfill(6)) or by_code.get(m.group(1))
            ann = _annot_text(h, pf) if h else ""
            out.append(f"{base}   {_ANNOT} {ann}" if ann else base)
        else:
            out.append(line)
    p.write_text("\n".join(out) + "\n", encoding="utf-8")
    return True
