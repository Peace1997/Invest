from __future__ import annotations

from datetime import date


class MarketWarehouse:
    """Read-only market-data interface used by decision evaluation."""

    def __init__(self, con):
        self.con = con

    def close_on_or_before(self, symbol: str, asset_type: str, on_or_before):
        table, date_col, close_expr = self._table(asset_type)
        if table is None:
            return None
        row = self.con.execute(
            f"""
            SELECT {date_col} AS trade_date, {close_expr} AS close
            FROM {table}
            WHERE symbol = ?
              AND {date_col} <= ?
              AND {close_expr} IS NOT NULL
            ORDER BY {date_col} DESC
            LIMIT 1
            """,
            [str(symbol).zfill(6), on_or_before],
        ).fetchone()
        return (row[0], float(row[1])) if row else None

    def forward_close(self, symbol: str, asset_type: str, trade_date, n_bars: int):
        table, date_col, close_expr = self._table(asset_type)
        if table is None or n_bars <= 0:
            return None
        rows = self.con.execute(
            f"""
            SELECT {date_col} AS trade_date, {close_expr} AS close
            FROM {table}
            WHERE symbol = ?
              AND {date_col} > ?
              AND {close_expr} IS NOT NULL
            ORDER BY {date_col}
            LIMIT ?
            """,
            [str(symbol).zfill(6), trade_date, int(n_bars)],
        ).fetchall()
        if len(rows) < n_bars:
            return None
        end_date, end_close = rows[-1]
        return end_date, float(end_close), len(rows)

    def trading_days_elapsed(self, trade_date, as_of_date) -> int:
        row = self.con.execute(
            "SELECT count(*) FROM calendar WHERE trade_date > ? AND trade_date <= ?",
            [trade_date, as_of_date],
        ).fetchone()
        return int(row[0]) if row else 0

    def latest_trade_date(self, on_or_before: date | None = None):
        if on_or_before is not None:
            row = self.con.execute(
                "SELECT max(trade_date) FROM calendar WHERE trade_date <= ?",
                [on_or_before],
            ).fetchone()
        else:
            row = self.con.execute("SELECT max(trade_date) FROM calendar").fetchone()
        return row[0] if row and row[0] else None

    def quality_report(self, as_of: date | None = None) -> list[dict]:
        expected_date = self.latest_trade_date(as_of)
        specs = (
            (
                "股票/ETF日线", "daily_bar", "trade_date", "close",
                ("stock", "etf"), ("symbol", "trade_date"), 0, False,
            ),
            (
                "指数日线", "index_bar", "trade_date", "close",
                ("index",), ("symbol", "trade_date"), 0, False,
            ),
            (
                "场外基金净值", "fund_nav", "nav_date", "nav",
                None, ("symbol", "nav_date"), 1, False,
            ),
            (
                "估值数据", "valuation_daily", "trade_date", "COALESCE(pe_ttm, pb)",
                ("stock",), ("symbol", "trade_date", "src"), 0, True,
            ),
        )
        return [
            self._quality_row(
                dataset, table, date_col, value_col, asset_types, key_columns,
                allowed_lag, check_source_conflicts, expected_date,
            )
            for (
                dataset, table, date_col, value_col, asset_types, key_columns,
                allowed_lag, check_source_conflicts,
            ) in specs
        ]

    def _quality_row(
        self,
        dataset: str,
        table: str,
        date_col: str,
        value_col: str,
        asset_types: tuple[str, ...] | None,
        key_columns: tuple[str, ...],
        allowed_lag: int,
        check_source_conflicts: bool,
        expected_date,
    ) -> dict:
        if not self._table_exists(table):
            return {
                "dataset": dataset,
                "table": table,
                "latest_date": None,
                "expected_date": expected_date,
                "lag_trading_days": None,
                "latest_rows": 0,
                "coverage_pct": None,
                "null_count": 0,
                "duplicate_count": 0,
                "source_conflict_count": 0,
                "status": "异常",
            }
        latest_date = self.con.execute(
            f"SELECT max({date_col}) FROM {table}"
        ).fetchone()[0]
        lag = None
        if latest_date and expected_date:
            lag = int(self.con.execute(
                "SELECT count(*) FROM calendar WHERE trade_date > ? AND trade_date <= ?",
                [latest_date, expected_date],
            ).fetchone()[0])

        latest_rows = 0
        null_count = 0
        duplicate_count = 0
        source_conflict_count = 0
        coverage_pct = None
        if latest_date:
            latest_rows, null_count = self.con.execute(
                f"SELECT count(*), count(*) FILTER (WHERE {value_col} IS NULL) "
                f"FROM {table} WHERE {date_col}=?",
                [latest_date],
            ).fetchone()
            duplicate_count = int(self.con.execute(
                f"""
                SELECT COALESCE(sum(n - 1), 0)
                FROM (
                    SELECT count(*) AS n
                    FROM {table}
                    WHERE {date_col}=?
                    GROUP BY {', '.join(key_columns)}
                    HAVING count(*) > 1
                ) duplicates
                """,
                [latest_date],
            ).fetchone()[0])
            if asset_types and self._table_exists("instruments"):
                placeholders = ", ".join("?" for _ in asset_types)
                target_count = int(self.con.execute(
                    f"SELECT count(*) FROM instruments WHERE type IN ({placeholders}) "
                    "AND (delist_date IS NULL OR delist_date > ?)",
                    [*asset_types, expected_date],
                ).fetchone()[0])
                covered = int(self.con.execute(
                    f"SELECT count(DISTINCT symbol) FROM {table} WHERE {date_col}=?",
                    [latest_date],
                ).fetchone()[0])
                if target_count:
                    coverage_pct = round(covered / target_count * 100, 2)
            if check_source_conflicts:
                source_conflict_count = int(self.con.execute(
                    f"""
                    SELECT count(*)
                    FROM (
                        SELECT symbol, {date_col}
                        FROM {table}
                        WHERE {date_col}=?
                        GROUP BY symbol, {date_col}
                        HAVING count(DISTINCT src) > 1
                           AND (
                               count(DISTINCT pe_ttm) > 1
                               OR count(DISTINCT pb) > 1
                           )
                    ) conflicts
                    """,
                    [latest_date],
                ).fetchone()[0])

        if latest_date is None or null_count or duplicate_count:
            status = "异常"
        elif (
            (lag is not None and lag > allowed_lag)
            or source_conflict_count
            or (coverage_pct is not None and coverage_pct < 90)
        ):
            status = "警告"
        else:
            status = "正常"
        return {
            "dataset": dataset,
            "table": table,
            "latest_date": latest_date,
            "expected_date": expected_date,
            "lag_trading_days": lag,
            "latest_rows": int(latest_rows),
            "coverage_pct": coverage_pct,
            "null_count": int(null_count),
            "duplicate_count": duplicate_count,
            "source_conflict_count": source_conflict_count,
            "status": status,
        }

    def _table_exists(self, table: str) -> bool:
        row = self.con.execute(
            """
            SELECT count(*)
            FROM information_schema.tables
            WHERE table_schema='main' AND table_name=?
            """,
            [table],
        ).fetchone()
        return bool(row and row[0])

    @staticmethod
    def _table(asset_type: str):
        if asset_type == "index":
            return "index_bar", "trade_date", "close"
        if asset_type in ("stock", "etf"):
            return "daily_bar_adj", "trade_date", "close"
        if asset_type == "otc_fund":
            return "fund_nav", "nav_date", "nav"
        return None, None, None
