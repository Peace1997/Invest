from __future__ import annotations

import unittest
from datetime import date

import duckdb

from ashare.repositories import MarketWarehouse


class MarketWarehouseTest(unittest.TestCase):
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
