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

    @staticmethod
    def _table(asset_type: str):
        if asset_type == "index":
            return "index_bar", "trade_date", "close"
        if asset_type in ("stock", "etf"):
            return "daily_bar_adj", "trade_date", "close"
        if asset_type == "otc_fund":
            return "fund_nav", "nav_date", "nav"
        return None, None, None
