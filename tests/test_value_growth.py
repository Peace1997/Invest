from __future__ import annotations

import unittest
from datetime import date

import duckdb
import pandas as pd

from ashare.signals.value_growth import rank_value_growth_candidates, screen_value_growth


def _row(symbol: str, *, pe: float, pb: float, industry: str = "银行Ⅱ",
         profit: float = 10, revenue: float = 10, roe: float = 10) -> dict:
    return {
        "symbol": symbol,
        "name": symbol,
        "industry": industry,
        "pe_ttm": pe,
        "pb": pb,
        "total_mv": 100,
        "roe": roe,
        "debt_ratio": 50,
        "profit_yoy": profit,
        "revenue_yoy": revenue,
        "valuation_lag_days": 1,
    }


class ValueGrowthScreenTest(unittest.TestCase):
    def test_positive_profit_and_revenue_growth_are_hard_gates(self):
        rows = pd.DataFrame([
            _row("000001", pe=5, pb=0.5, profit=-1),
            _row("000002", pe=6, pb=0.6, revenue=-1),
            _row("000004", pe=7, pb=0.7, revenue=4.9),
            _row("000005", pe=8, pb=0.8, roe=4.9),
            _row("000006", pe=5, pb=0.5, profit=200.1),
            _row("000007", pe=5, pb=0.5, revenue=100.1),
            _row("000003", pe=10, pb=1.0),
        ])

        result = rank_value_growth_candidates(rows, limit=5)

        self.assertEqual(result["symbol"].tolist(), ["000003"])

    def test_low_valuation_leads_with_a_reserved_tech_candidate(self):
        rows = pd.DataFrame([
            _row("000001", pe=5, pb=0.5),
            _row("000002", pe=6, pb=0.6),
            _row("000003", pe=7, pb=0.7),
            _row("000004", pe=8, pb=0.8),
            _row("000005", pe=30, pb=3.0, industry="软件开发"),
            _row("000006", pe=40, pb=4.0, industry="半导体"),
        ])

        result = rank_value_growth_candidates(rows, limit=4, tech_share=0.25)

        self.assertEqual(result.iloc[0]["symbol"], "000001")
        self.assertEqual(result["is_tech"].sum(), 1)
        self.assertIn("000005", result["symbol"].tolist())

    def test_screen_uses_only_announced_reports_and_includes_tech_boards(self):
        con = duckdb.connect(":memory:")
        con.execute("""
            CREATE TABLE instruments (
                symbol VARCHAR, name VARCHAR, type VARCHAR,
                is_st BOOLEAN, delist_date DATE
            );
            CREATE TABLE valuation_daily_canon (
                symbol VARCHAR, trade_date DATE, pe_ttm DOUBLE,
                pb DOUBLE, total_mv DOUBLE
            );
            CREATE TABLE fundamentals_ts (
                symbol VARCHAR, end_date DATE, ann_date DATE, roe DOUBLE,
                gross_margin DOUBLE, debt_ratio DOUBLE, profit_yoy DOUBLE,
                revenue_yoy DOUBLE
            );
            CREATE TABLE fundamentals (
                symbol VARCHAR, report_date DATE, industry VARCHAR
            );
        """)
        for symbol in ("688001", "300001"):
            con.execute(
                "INSERT INTO instruments VALUES (?, ?, 'stock', false, NULL)",
                [symbol, symbol],
            )
            con.execute(
                "INSERT INTO valuation_daily_canon VALUES (?, '2026-06-29', 20, 2, 100)",
                [symbol],
            )
            con.execute(
                "INSERT INTO fundamentals VALUES (?, '2026-06-30', '半导体')",
                [symbol],
            )
            con.execute(
                "INSERT INTO fundamentals_ts VALUES (?, '2025-12-31', '2026-03-30', 10, 30, 40, 12, 8)",
                [symbol],
            )
        con.execute(
            "INSERT INTO fundamentals_ts VALUES ('688001', '2026-06-30', '2026-07-31', 2, 20, 40, -5, -3)"
        )
        con.execute(
            "INSERT INTO fundamentals VALUES ('688001', '2026-09-30', '银行Ⅱ')"
        )

        result = screen_value_growth(con, limit=5, as_of=date(2026, 6, 30))

        self.assertEqual(set(result["symbol"]), {"688001", "300001"})
        self.assertEqual(set(result["report_date"].dt.date), {date(2025, 12, 31)})
        self.assertTrue(result["is_tech"].all())


if __name__ == "__main__":
    unittest.main()
