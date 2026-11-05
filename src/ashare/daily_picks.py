"""Daily 1–3 A-share candidates from a real screen.

Two inputs, and no third:

* An AlphaSift `dual_low` `ScreenResult` JSON (`picks` or `candidates`).
* The same hard filters applied to rows already stored in the warehouse
  (`daily_bar` ⋈ `valuation_daily_canon`). That path is an equivalent,
  reproducible ranking. It is not the AlphaSift factor model.

A missing date, a non-dual_low strategy, or an empty filter result yields
zero candidates. Nothing in this module substitutes today's date or a
built-in ticker list.

`mode` is how the caller must read the output:

* `fixture` — the payload is marked as a test fixture. `--live` cannot promote it.
* `replay` — an operator-supplied file that was not marked live.
* `live` — the operator asked to treat a file as a live screen, or the
  warehouse was screened. Live still means "this process read that source".
  It does not mean an order was sent.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import pandas as pd

# Next-session entry band around the data-date close, and the level that voids it.
ENTRY_DISCOUNT = Decimal("0.01")
CHASE_CAP = Decimal("0.02")
INVALIDATION = Decimal("0.03")

# AlphaSift dual_low.yaml hard filters that this warehouse mirror can evaluate.
# market_cap in that file is yuan; valuation_daily.total_mv is 亿元.
PE_MIN, PE_MAX = 0.0, 15.0
PB_MIN, PB_MAX = 0.0, 2.0
PRICE_MIN, PRICE_MAX = 3.0, 80.0
AMOUNT_MIN = 50_000_000.0          # 元
MV_MIN, MV_MAX = 50.0, 3000.0      # 亿元
CHG_MIN, CHG_MAX = -4.5, 4.5
MIN_NAMES = 2                      # a one-name table is not a cross-section

_DATE_KEYS = ("data_date", "trade_date", "as_of", "snapshot_trade_date")


@dataclass
class DailyPick:
    symbol: str
    name: str
    rank: int
    score: float
    data_date: str
    reference_price: float
    entry_low: float
    entry_cap: float
    entry_discipline: str
    invalidation_price: float
    invalidation: str
    pe: float | None = None
    pb: float | None = None


@dataclass
class DailyPicksResult:
    mode: str
    source: str
    strategy: str
    data_date: str | None
    picks: list[DailyPick] = field(default_factory=list)
    note: str = ""

    @property
    def banner(self) -> str:
        return {
            "fixture": "FIXTURE — test fixture output, not a market screen.",
            "replay": "REPLAY — operator-supplied file, not a live AlphaSift run.",
            "live": "LIVE — read from the source below. This process sends no orders.",
        }[self.mode]

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["banner"] = self.banner
        payload["count"] = len(self.picks)
        return payload


def _money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _price(value) -> Decimal | None:
    try:
        if value is None or pd.isna(value):
            return None
    except TypeError:
        pass
    try:
        dec = Decimal(str(value))
    except Exception:
        return None
    if not dec.is_finite() or dec <= 0:
        return None
    return dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _first_num(row: dict, *keys: str) -> float | None:
    for key in keys:
        if key not in row:
            continue
        found = _num(row.get(key))
        if found is not None:
            return found
    return None


def _num(value) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
    except TypeError:
        pass
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:  # NaN
        return None
    return out


def _parse_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text or text.lower() in {"none", "nat", "nan"}:
        return None
    if len(text) == 8 and text.isdigit():
        return datetime.strptime(text, "%Y%m%d").date()
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def _clamp_limit(limit: int | None) -> int:
    if limit is None:
        return 3
    return max(0, min(3, int(limit)))


def resolve_mode(payload: dict, requested: str) -> str:
    if requested not in {"fixture", "replay", "live"}:
        raise ValueError(f"unknown mode {requested!r}")
    marked = str(payload.get("mode") or "").strip().lower()
    if marked == "fixture" or payload.get("fixture") is True:
        return "fixture"
    return requested


def _levels(ref: Decimal) -> tuple[float, float, float, str, str]:
    low = _money(ref * (Decimal("1") - ENTRY_DISCOUNT))
    cap = _money(ref * (Decimal("1") + CHASE_CAP))
    stop = _money(ref * (Decimal("1") - INVALIDATION))
    entry = (
        f"Next session after the data date only. Reference close { _money(ref):.2f}. "
        f"Price band {low:.2f}–{cap:.2f}. A next-session open above {cap:.2f} "
        f"voids the candidate before entry. Research note only; no order is sent."
    )
    invalid = (
        f"Invalidation price {stop:.2f} (3% under the data-date close). "
        f"A-share T+1: a fill on the entry session cannot be sold that same session, "
        f"so a print through {stop:.2f} is actionable on the following session. "
        f"An entry-session open already through {stop:.2f} means do not enter."
    )
    return low, cap, stop, entry, invalid


def _pick_from_row(row: dict, data_date: date, rank: int) -> DailyPick | None:
    symbol = str(row.get("code") or row.get("symbol") or row.get("stock_code") or "")
    symbol = symbol.split(".")[0].strip()
    if len(symbol) > 6:
        symbol = symbol[-6:]
    if not (symbol.isdigit() and len(symbol) == 6):
        return None
    ref = _price(row.get("price", row.get("close")))
    if ref is None:
        return None
    own_date = _parse_date(row.get("trade_date") or row.get("data_date"))
    if own_date is not None and own_date != data_date:
        return None
    score = _num(row.get("final_score"))
    if score is None:
        score = _num(row.get("score"))
    if score is None:
        score = _num(row.get("screen_score"))
    if score is None:
        score = 0.0
    given_rank = row.get("rank")
    try:
        use_rank = int(given_rank) if given_rank is not None and not pd.isna(given_rank) else rank
    except (TypeError, ValueError):
        use_rank = rank
    low, cap, stop, entry, invalid = _levels(ref)
    name = str(row.get("name") or row.get("stock_name") or "")
    return DailyPick(
        symbol=symbol,
        name=name,
        rank=use_rank,
        score=score,
        data_date=data_date.isoformat(),
        reference_price=_money(ref),
        entry_low=low,
        entry_cap=cap,
        entry_discipline=entry,
        invalidation_price=stop,
        invalidation=invalid,
        pe=_first_num(row, "pe_ratio", "pe", "pe_ttm"),
        pb=_first_num(row, "pb_ratio", "pb"),
    )


def _empty(mode: str, source: str, strategy: str, data_date: date | None, note: str) -> DailyPicksResult:
    return DailyPicksResult(
        mode=mode,
        source=source,
        strategy=strategy,
        data_date=data_date.isoformat() if data_date else None,
        picks=[],
        note=note,
    )


def from_alphasift(payload: dict, *, requested_mode: str = "replay", limit: int | None = 3) -> DailyPicksResult:
    """Turn one dual_low ScreenResult into at most `limit` candidates."""
    if not isinstance(payload, dict):
        raise TypeError("screen payload must be an object")
    mode = resolve_mode(payload, requested_mode)
    strategy = str(payload.get("strategy") or "")
    source = "alphasift_dual_low"
    if strategy != "dual_low":
        return _empty(mode, source, strategy, None,
                      "strategy is not dual_low; no candidates emitted.")
    data_date = None
    for key in _DATE_KEYS:
        data_date = _parse_date(payload.get(key))
        if data_date is not None:
            break
    # created_at is a run timestamp, not the market-data date.
    if data_date is None:
        return _empty(mode, source, strategy, None,
                      "screen has no data_date/trade_date/as_of; no candidates emitted.")
    raw = payload.get("picks")
    if raw is None:
        raw = payload.get("candidates")
    if not raw:
        return _empty(mode, source, strategy, data_date, "screen picks are empty.")
    built: list[DailyPick] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        pick = _pick_from_row(item, data_date, rank=len(built) + 1)
        if pick is not None:
            built.append(pick)
    built.sort(key=lambda p: (p.rank, -p.score, p.symbol))
    # Re-number after the stable sort so the printed rank is 1..n within the cut.
    cut = built[:_clamp_limit(limit)]
    for i, pick in enumerate(cut, 1):
        pick.rank = i
    if not cut:
        why = "limit is 0." if built else (
            "no pick had a 6-digit code, a positive price, and the screen data date."
        )
        return _empty(mode, source, strategy, data_date, why)
    return DailyPicksResult(mode=mode, source=source, strategy=strategy,
                            data_date=data_date.isoformat(), picks=cut, note="")


def load_screen(path: str | Path) -> dict:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("screen file must be a JSON object")
    return payload


def _warehouse_day(con, as_of: date | None) -> date | None:
    if as_of is not None:
        return as_of
    row = con.execute(
        """
        SELECT MAX(b.trade_date)
        FROM daily_bar b
        JOIN valuation_daily_canon v
          ON v.symbol = b.symbol AND v.trade_date = b.trade_date
        WHERE b.type = 'stock'
        """
    ).fetchone()
    if not row or row[0] is None:
        return None
    return _parse_date(row[0])


def screen_warehouse(con, *, as_of: date | None = None, mode: str = "live",
                     limit: int | None = 3) -> DailyPicksResult:
    """Rank stocks already stored for one date with the dual_low hard filters.

    Score = 100 * (1 - average percentile of PE and PB), lower valuation higher.
    Ties break by symbol. Fewer than two names after the filter → no pick.
    """
    if mode not in {"fixture", "replay", "live"}:
        raise ValueError(f"unknown mode {mode!r}")
    source = "warehouse_dual_low"
    day = _warehouse_day(con, as_of)
    if day is None:
        return _empty(mode, source, "dual_low", None,
                      "warehouse has no stock bar joined to valuation; no candidates emitted.")
    frame = con.execute(
        """
        SELECT b.symbol, COALESCE(i.name, '') AS name, b.trade_date, b.close,
               b.pct_chg, b.amount, v.pe_ttm, v.pb, v.total_mv,
               COALESCE(i.is_st, FALSE) AS is_st
        FROM daily_bar b
        JOIN valuation_daily_canon v
          ON v.symbol = b.symbol AND v.trade_date = b.trade_date
        LEFT JOIN instruments i ON i.symbol = b.symbol
        WHERE b.type = 'stock' AND b.trade_date = ?
        """,
        [day],
    ).df()
    if frame is None or frame.empty:
        return _empty(mode, source, "dual_low", day,
                      f"no joined rows on {day.isoformat()}; no candidates emitted.")
    for col in ("close", "pct_chg", "amount", "pe_ttm", "pb", "total_mv"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    name = frame["name"].fillna("").astype(str)
    ok = (
        frame["pe_ttm"].between(PE_MIN, PE_MAX, inclusive="right")
        & frame["pb"].between(PB_MIN, PB_MAX, inclusive="right")
        & frame["close"].between(PRICE_MIN, PRICE_MAX)
        & (frame["amount"] >= AMOUNT_MIN)
        & frame["total_mv"].between(MV_MIN, MV_MAX)
        & frame["pct_chg"].between(CHG_MIN, CHG_MAX)
        & ~frame["is_st"].astype(bool)
        & ~name.str.contains("ST", case=False, na=False)
    )
    kept = frame.loc[ok].copy()
    if len(kept) < MIN_NAMES:
        return _empty(
            mode, source, "dual_low", day,
            f"{len(kept)} name(s) passed dual_low filters on {day.isoformat()}; "
            f"need at least {MIN_NAMES} to rank. No candidates emitted.",
        )
    n = len(kept)
    pe_pct = kept["pe_ttm"].rank(method="average", ascending=True) / n
    pb_pct = kept["pb"].rank(method="average", ascending=True) / n
    kept["score"] = (100.0 * (1.0 - (pe_pct + pb_pct) / 2.0)).round(4)
    kept = kept.sort_values(["score", "symbol"], ascending=[False, True])
    picks: list[DailyPick] = []
    for _, rec in kept.head(_clamp_limit(limit)).iterrows():
        row = {
            "symbol": rec["symbol"],
            "name": rec["name"],
            "price": rec["close"],
            "score": float(rec["score"]),
            "pe_ttm": rec["pe_ttm"],
            "pb": rec["pb"],
        }
        pick = _pick_from_row(row, day, rank=len(picks) + 1)
        if pick is not None:
            picks.append(pick)
    for i, pick in enumerate(picks, 1):
        pick.rank = i
    note = ""
    if len(kept) < 5:
        note = (f"cross-section is {len(kept)} names on {day.isoformat()}, "
                "not a full-market AlphaSift snapshot.")
    return DailyPicksResult(mode=mode, source=source, strategy="dual_low",
                            data_date=day.isoformat(), picks=picks, note=note)


def render(result: DailyPicksResult) -> str:
    lines = [
        f"daily picks · mode={result.mode} · source={result.source} · "
        f"strategy={result.strategy or '-'} · data_date={result.data_date or '-'} · "
        f"n={len(result.picks)}",
        result.banner,
    ]
    if result.note:
        lines.append(result.note)
    if not result.picks:
        lines.append("No candidates.")
        return "\n".join(lines)
    for pick in result.picks:
        pe = "-" if pick.pe is None else f"{pick.pe:.2f}"
        pb = "-" if pick.pb is None else f"{pick.pb:.2f}"
        lines.append(
            f"{pick.rank}. {pick.symbol} {pick.name}  score={pick.score:.4g}  "
            f"ref={pick.reference_price:.2f}  band={pick.entry_low:.2f}–{pick.entry_cap:.2f}  "
            f"invalidation={pick.invalidation_price:.2f}  pe={pe}  pb={pb}"
        )
        lines.append(f"   entry: {pick.entry_discipline}")
        lines.append(f"   invalidation: {pick.invalidation}")
    return "\n".join(lines)
