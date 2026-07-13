from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal, TypedDict, cast


SignalSource = Literal["recommend", "premarket", "endday", "pf"]
SignalStatus = Literal["active", "pending_data", "closed", "unable"]
AssetType = Literal["stock", "etf", "index", "otc_fund"]
OutcomeLabel = Literal["hit", "miss", "neutral"]


class DecisionSignal(TypedDict):
    signal_id: str
    created_at: datetime
    trade_date: date
    source: SignalSource
    style: str | None
    symbol: str
    name: str | None
    asset_type: AssetType
    action: str
    score: float | None
    ref_price: float | None
    horizon_days: int
    factors: str | None
    rationale: str | None
    status: SignalStatus


class DecisionOutcome(TypedDict):
    signal_id: str
    evaluated_at: datetime
    end_date: date
    bars_forward: int
    ref_price: float
    end_price: float
    fwd_return_pct: float
    bench_return_pct: float
    excess_pct: float
    outcome: OutcomeLabel
    note: str | None


_SIGNAL_COLUMNS = (
    "signal_id", "created_at", "trade_date", "source", "style", "symbol",
    "name", "asset_type", "action", "score", "ref_price", "horizon_days",
    "factors", "rationale", "status",
)

_OUTCOME_COLUMNS = (
    "signal_id", "evaluated_at", "end_date", "bars_forward", "ref_price",
    "end_price", "fwd_return_pct", "bench_return_pct", "excess_pct",
    "outcome", "note",
)


class DecisionJournal:
    """SQLite-backed journal for operational decision signals and outcomes."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS decision_signal (
                    signal_id TEXT PRIMARY KEY,
                    created_at TIMESTAMP,
                    trade_date DATE,
                    source TEXT NOT NULL,
                    style TEXT,
                    symbol TEXT NOT NULL,
                    name TEXT,
                    asset_type TEXT NOT NULL,
                    action TEXT NOT NULL,
                    score REAL,
                    ref_price REAL,
                    horizon_days INTEGER NOT NULL,
                    factors TEXT,
                    rationale TEXT,
                    status TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_decision_signal_status
                    ON decision_signal(status, trade_date);
                CREATE INDEX IF NOT EXISTS idx_decision_signal_source
                    ON decision_signal(source, trade_date);

                CREATE TABLE IF NOT EXISTS decision_outcome (
                    signal_id TEXT,
                    evaluated_at TIMESTAMP,
                    end_date DATE,
                    bars_forward INTEGER,
                    ref_price REAL,
                    end_price REAL,
                    fwd_return_pct REAL,
                    bench_return_pct REAL,
                    excess_pct REAL,
                    outcome TEXT,
                    note TEXT,
                    PRIMARY KEY(signal_id, bars_forward),
                    FOREIGN KEY(signal_id) REFERENCES decision_signal(signal_id)
                );
                CREATE INDEX IF NOT EXISTS idx_decision_outcome_outcome
                    ON decision_outcome(outcome);
                """
            )
            self._upgrade_outcome_primary_key(con)

    def record_many(self, signals: list[DecisionSignal]) -> int:
        if not signals:
            return 0
        placeholders = ", ".join("?" for _ in _SIGNAL_COLUMNS)
        updates = ", ".join(
            f"{column}=excluded.{column}"
            for column in _SIGNAL_COLUMNS
            if column != "signal_id"
        )
        sql = (
            f"INSERT INTO decision_signal ({', '.join(_SIGNAL_COLUMNS)}) "
            f"VALUES ({placeholders}) ON CONFLICT(signal_id) DO UPDATE SET {updates} "
            "WHERE decision_signal.status NOT IN ('closed', 'unable')"
        )
        rows = [
            tuple(_to_sql(signal.get(column)) for column in _SIGNAL_COLUMNS)
            for signal in signals
        ]
        with self._connect() as con:
            con.executemany(sql, rows)
        return len(rows)

    def open_signals(self, include_closed: bool = False) -> list[DecisionSignal]:
        statuses = ("active", "pending_data")
        if include_closed:
            statuses += ("closed",)
        placeholders = ", ".join("?" for _ in statuses)
        with self._connect() as con:
            rows = con.execute(
                f"SELECT {', '.join(_SIGNAL_COLUMNS)} FROM decision_signal "
                f"WHERE status IN ({placeholders}) ORDER BY trade_date, source, symbol",
                statuses,
            ).fetchall()
        return [_signal_from_row(row) for row in rows]

    def migrate_from_duckdb(self, legacy_con) -> dict[str, int]:
        """Copy legacy decision tables from the market warehouse, idempotently."""
        tables = {
            row[0]
            for row in legacy_con.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='main'"
            ).fetchall()
        }
        signals = (
            self._legacy_rows(legacy_con, "decision_signal", _SIGNAL_COLUMNS)
            if "decision_signal" in tables else []
        )
        outcomes = (
            self._legacy_rows(legacy_con, "decision_outcome", _OUTCOME_COLUMNS)
            if "decision_outcome" in tables else []
        )
        self._insert_missing_signals(signals)
        self._insert_missing_outcomes(outcomes)
        return {"signals": len(signals), "outcomes": len(outcomes)}

    def _insert_missing_signals(self, signals: list[DecisionSignal]) -> int:
        if not signals:
            return 0
        placeholders = ", ".join("?" for _ in _SIGNAL_COLUMNS)
        rows = [
            tuple(_to_sql(signal.get(column)) for column in _SIGNAL_COLUMNS)
            for signal in signals
        ]
        with self._connect() as con:
            con.executemany(
                f"INSERT INTO decision_signal ({', '.join(_SIGNAL_COLUMNS)}) "
                f"VALUES ({placeholders}) ON CONFLICT(signal_id) DO NOTHING",
                rows,
            )
        return len(rows)

    def _insert_missing_outcomes(self, outcomes: list[DecisionOutcome]) -> int:
        if not outcomes:
            return 0
        placeholders = ", ".join("?" for _ in _OUTCOME_COLUMNS)
        rows = [
            tuple(_to_sql(outcome.get(column)) for column in _OUTCOME_COLUMNS)
            for outcome in outcomes
        ]
        with self._connect() as con:
            con.executemany(
                f"INSERT INTO decision_outcome ({', '.join(_OUTCOME_COLUMNS)}) "
                f"VALUES ({placeholders}) "
                "ON CONFLICT(signal_id, bars_forward) DO NOTHING",
                rows,
            )
        return len(rows)

    def settle_many(self, outcomes: list[DecisionOutcome]) -> int:
        if not outcomes:
            return 0
        with self._connect() as con:
            written = self._upsert_outcomes_on(con, outcomes)
            con.executemany(
                "UPDATE decision_signal SET status='closed' WHERE signal_id=?",
                [(outcome["signal_id"],) for outcome in outcomes],
            )
        return written

    def record_outcomes(self, outcomes: list[DecisionOutcome]) -> int:
        """Record interim observations without closing their signals."""
        if not outcomes:
            return 0
        with self._connect() as con:
            return self._upsert_outcomes_on(con, outcomes)

    def recorded_horizons(self) -> dict[str, set[int]]:
        with self._connect() as con:
            rows = con.execute(
                "SELECT signal_id, bars_forward FROM decision_outcome"
            ).fetchall()
        result: dict[str, set[int]] = {}
        for row in rows:
            result.setdefault(row["signal_id"], set()).add(int(row["bars_forward"]))
        return result

    def review_details(
        self,
        window_days: int,
        source: str | None = None,
        status: str | None = None,
        as_of: date | None = None,
    ) -> list[dict]:
        cutoff = (as_of or date.today()) - timedelta(days=window_days)
        where = ["s.trade_date >= ?"]
        params: list[object] = [_to_sql(cutoff)]
        if source:
            where.append("s.source = ?")
            params.append(source)
        if status:
            where.append("s.status = ?")
            params.append(status)
        with self._connect() as con:
            rows = con.execute(
                f"""
                SELECT
                    s.signal_id, s.trade_date, s.source, s.style, s.symbol,
                    s.name, s.action, s.rationale, s.status,
                    o.end_date, o.bars_forward, o.fwd_return_pct,
                    o.excess_pct, o.outcome
                FROM decision_signal s
                LEFT JOIN decision_outcome o ON o.signal_id=s.signal_id
                WHERE {' AND '.join(where)}
                ORDER BY s.trade_date DESC, s.source, s.symbol, o.bars_forward
                """,
                params,
            ).fetchall()

        details: dict[str, dict] = {}
        for row in rows:
            signal_id = row["signal_id"]
            detail = details.setdefault(signal_id, {
                "signal_id": signal_id,
                "trade_date": date.fromisoformat(row["trade_date"]),
                "source": row["source"],
                "style": row["style"],
                "symbol": row["symbol"],
                "name": row["name"],
                "action": row["action"],
                "rationale": row["rationale"],
                "status": row["status"],
                "verdict": "待观察",
                "outcomes": {},
            })
            if row["bars_forward"] is None:
                continue
            bars_forward = int(row["bars_forward"])
            outcome = row["outcome"]
            detail["outcomes"][bars_forward] = {
                "end_date": date.fromisoformat(row["end_date"]),
                "fwd_return_pct": row["fwd_return_pct"],
                "excess_pct": row["excess_pct"],
                "outcome": outcome,
            }
            detail["verdict"] = {
                "hit": "有效",
                "miss": "失效",
                "neutral": "待观察",
            }.get(outcome, "待观察")
        return list(details.values())

    def mark_status(self, signal_id: str, status: SignalStatus) -> None:
        with self._connect() as con:
            con.execute(
                "UPDATE decision_signal SET status=? WHERE signal_id=?",
                [status, signal_id],
            )

    def review(
        self,
        window_days: int,
        source: str | None = None,
        horizon_days: int | None = None,
        status: str | None = None,
        as_of: date | None = None,
    ) -> list[dict]:
        cutoff = (as_of or date.today()) - timedelta(days=window_days)
        where = "s.trade_date >= ?"
        params: list[object] = [_to_sql(cutoff)]
        if source:
            where += " AND s.source = ?"
            params.append(source)
        if horizon_days is not None:
            where += " AND o.bars_forward = ?"
            params.append(int(horizon_days))
        if status:
            where += " AND s.status = ?"
            params.append(status)
        with self._connect() as con:
            rows = con.execute(
                f"""
                SELECT
                    CASE
                        WHEN s.source='recommend' AND s.style IS NOT NULL
                            THEN s.source || '/' || s.style
                        ELSE s.source
                    END AS signal_group,
                    COUNT(*) AS sample_count,
                    AVG(CASE WHEN o.outcome='hit' THEN 1.0 ELSE 0.0 END) * 100 AS hit_rate_pct,
                    AVG(CASE WHEN o.outcome='neutral' THEN 1.0 ELSE 0.0 END) * 100 AS neutral_rate_pct,
                    AVG(o.fwd_return_pct) AS avg_fwd_return_pct,
                    AVG(o.excess_pct) AS avg_excess_pct
                FROM decision_signal s
                JOIN decision_outcome o ON o.signal_id=s.signal_id
                WHERE {where} AND o.outcome IN ('hit', 'miss', 'neutral')
                GROUP BY signal_group
                ORDER BY signal_group
                """,
                params,
            ).fetchall()
        return [
            {
                "group": row["signal_group"],
                "sample_count": row["sample_count"],
                "hit_rate_pct": row["hit_rate_pct"],
                "neutral_rate_pct": row["neutral_rate_pct"],
                "avg_fwd_return_pct": row["avg_fwd_return_pct"],
                "avg_excess_pct": row["avg_excess_pct"],
            }
            for row in rows
        ]

    @staticmethod
    def _upsert_outcomes_on(
        con: sqlite3.Connection,
        outcomes: list[DecisionOutcome],
    ) -> int:
        placeholders = ", ".join("?" for _ in _OUTCOME_COLUMNS)
        updates = ", ".join(
            f"{column}=excluded.{column}"
            for column in _OUTCOME_COLUMNS
            if column != "signal_id"
        )
        sql = (
            f"INSERT INTO decision_outcome ({', '.join(_OUTCOME_COLUMNS)}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT(signal_id, bars_forward) DO UPDATE SET {updates}"
        )
        rows = [
            tuple(_to_sql(outcome.get(column)) for column in _OUTCOME_COLUMNS)
            for outcome in outcomes
        ]
        con.executemany(sql, rows)
        return len(rows)

    @staticmethod
    def _upgrade_outcome_primary_key(con: sqlite3.Connection) -> None:
        columns = con.execute("PRAGMA table_info(decision_outcome)").fetchall()
        primary_key = [row[1] for row in columns if row[5]]
        if primary_key == ["signal_id", "bars_forward"]:
            return
        con.executescript(
            """
            DROP INDEX IF EXISTS idx_decision_outcome_outcome;
            ALTER TABLE decision_outcome RENAME TO decision_outcome_single;
            CREATE TABLE decision_outcome (
                signal_id TEXT,
                evaluated_at TIMESTAMP,
                end_date DATE,
                bars_forward INTEGER,
                ref_price REAL,
                end_price REAL,
                fwd_return_pct REAL,
                bench_return_pct REAL,
                excess_pct REAL,
                outcome TEXT,
                note TEXT,
                PRIMARY KEY(signal_id, bars_forward),
                FOREIGN KEY(signal_id) REFERENCES decision_signal(signal_id)
            );
            INSERT INTO decision_outcome (
                signal_id, evaluated_at, end_date, bars_forward, ref_price,
                end_price, fwd_return_pct, bench_return_pct, excess_pct,
                outcome, note
            )
            SELECT
                signal_id, evaluated_at, end_date, bars_forward, ref_price,
                end_price, fwd_return_pct, bench_return_pct, excess_pct,
                outcome, note
            FROM decision_outcome_single;
            DROP TABLE decision_outcome_single;
            CREATE INDEX idx_decision_outcome_outcome
                ON decision_outcome(outcome);
            """
        )

    @staticmethod
    def _legacy_rows(con, table: str, columns: tuple[str, ...]) -> list[dict]:
        cursor = con.execute(f"SELECT {', '.join(columns)} FROM {table}")
        return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(
            self.path,
            timeout=5,
        )
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=5000")
        con.execute("PRAGMA foreign_keys=ON")
        return con


def _to_sql(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _signal_from_row(row: sqlite3.Row) -> DecisionSignal:
    signal = dict(row)
    if isinstance(signal.get("created_at"), str):
        signal["created_at"] = datetime.fromisoformat(signal["created_at"])
    if isinstance(signal.get("trade_date"), str):
        signal["trade_date"] = date.fromisoformat(signal["trade_date"])
    return cast(DecisionSignal, signal)
