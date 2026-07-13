from __future__ import annotations

import unittest
from datetime import date

import duckdb

from ashare.repositories import MarketWarehouse


class MarketWarehouseTest(unittest.TestCase):
    def test_quality_report_tolerates_t1_fund_nav_and_flags_valuation_conflicts(self):
        con = duckdb.connect(":memory:")
        con.execute(
            """
            CREATE TABLE calendar (trade_date DATE);
            INSERT INTO calendar VALUES
                (DATE '2026-07-13'), (DATE '2026-07-14');
            CREATE TABLE instruments (
                symbol VARCHAR, type VARCHAR, delist_date DATE
            );
            INSERT INTO instruments VALUES ('000001', 'stock', NULL);
            CREATE TABLE daily_bar (
                symbol VARCHAR, trade_date DATE, close DOUBLE
            );
            CREATE TABLE fund_nav (
                symbol VARCHAR, nav_date DATE, nav DOUBLE
            );
            INSERT INTO fund_nav VALUES
                ('001234', DATE '2026-07-13', 1.25);
            CREATE TABLE valuation_daily (
                symbol VARCHAR, trade_date DATE, pe_ttm DOUBLE,
                pb DOUBLE, src VARCHAR
            );
            INSERT INTO valuation_daily VALUES
                ('000001', DATE '2026-07-14', 8.0, 1.0, 'tushare'),
                ('000001', DATE '2026-07-14', 9.0, 1.1, 'baidu');
            """
        )

        report = {
            row["dataset"]: row
            for row in MarketWarehouse(con).quality_report(date(2026, 7, 14))
        }

        self.assertEqual(report["场外基金净值"]["lag_trading_days"], 1)
        self.assertEqual(report["场外基金净值"]["status"], "正常")
        self.assertIsNone(report["股票/ETF日线"]["lag_trading_days"])
        self.assertEqual(report["股票/ETF日线"]["status"], "异常")
        self.assertEqual(report["估值数据"]["source_conflict_count"], 1)
        self.assertEqual(report["估值数据"]["duplicate_count"], 0)
        self.assertEqual(report["估值数据"]["status"], "警告")
        con.close()

    def test_quality_report_exposes_freshness_nulls_duplicates_and_coverage(self):
        con = duckdb.connect(":memory:")
        con.execute(
            """
            CREATE TABLE calendar (trade_date DATE);
            INSERT INTO calendar VALUES
                (DATE '2026-07-13'), (DATE '2026-07-14');
            CREATE TABLE instruments (
                symbol VARCHAR, type VARCHAR, delist_date DATE
            );
            INSERT INTO instruments VALUES
                ('000001', 'stock', NULL),
                ('510300', 'etf', NULL),
                ('000300', 'index', NULL);
            CREATE TABLE daily_bar (
                symbol VARCHAR, trade_date DATE, close DOUBLE
            );
            INSERT INTO daily_bar VALUES
                ('000001', DATE '2026-07-14', 10.0),
                ('000001', DATE '2026-07-14', NULL);
            CREATE TABLE index_bar (
                symbol VARCHAR, trade_date DATE, close DOUBLE
            );
            INSERT INTO index_bar VALUES
                ('000300', DATE '2026-07-13', 4000.0);
            """
        )

        report = {
            row["dataset"]: row
            for row in MarketWarehouse(con).quality_report(date(2026, 7, 14))
        }

        self.assertEqual(
            report["股票/ETF日线"],
            {
                "dataset": "股票/ETF日线",
                "table": "daily_bar",
                "latest_date": date(2026, 7, 14),
                "expected_date": date(2026, 7, 14),
                "lag_trading_days": 0,
                "latest_rows": 2,
                "coverage_pct": 50.0,
                "null_count": 1,
                "duplicate_count": 1,
                "source_conflict_count": 0,
                "status": "异常",
            },
        )
        self.assertEqual(report["指数日线"]["lag_trading_days"], 1)
        self.assertEqual(report["指数日线"]["status"], "警告")
        con.close()

    def test_price_and_trading_day_queries_share_one_market_interface(self):
        con = duckdb.connect(":memory:")
        con.execute(
            """
            CREATE TABLE calendar (trade_date DATE);
            INSERT INTO calendar VALUES
                (DATE '2026-07-10'), (DATE '2026-07-13'), (DATE '2026-07-14');
            CREATE TABLE daily_bar_adj (symbol VARCHAR, trade_date DATE, close DOUBLE);
            INSERT INTO daily_bar_adj VALUES
                ('000001', DATE '2026-07-10', 10.0),
                ('000001', DATE '2026-07-13', 11.0),
                ('000001', DATE '2026-07-14', 12.0);
            CREATE TABLE index_bar (symbol VARCHAR, trade_date DATE, close DOUBLE);
            INSERT INTO index_bar VALUES
                ('000300', DATE '2026-07-10', 4000.0),
                ('000300', DATE '2026-07-13', 4040.0),
                ('000300', DATE '2026-07-14', 4080.0);
            CREATE TABLE fund_nav (symbol VARCHAR, nav_date DATE, nav DOUBLE);
            """
        )
        warehouse = MarketWarehouse(con)

        self.assertEqual(
            warehouse.close_on_or_before("000001", "stock", date(2026, 7, 10)),
            (date(2026, 7, 10), 10.0),
        )
        self.assertEqual(
            warehouse.forward_close("000001", "stock", date(2026, 7, 10), 2),
            (date(2026, 7, 14), 12.0, 2),
        )
        self.assertEqual(
            warehouse.trading_days_elapsed(date(2026, 7, 10), date(2026, 7, 14)),
            2,
        )
        self.assertEqual(
            warehouse.latest_trade_date(date(2026, 7, 13)),
            date(2026, 7, 13),
        )
        con.close()


if __name__ == "__main__":
    unittest.main()
