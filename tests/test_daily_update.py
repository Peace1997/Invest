from __future__ import annotations

import unittest
from datetime import date, datetime

import duckdb

from ashare.jobs.daily_update import expected_latest_trade_date


class ExpectedLatestTradeDateTest(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect(":memory:")
        self.con.execute("CREATE TABLE calendar (trade_date DATE)")
        self.con.executemany(
            "INSERT INTO calendar VALUES (?)",
            [(date(2026, 7, 17),), (date(2026, 7, 20),)],
        )

    def tearDown(self):
        self.con.close()

    def test_before_settlement_uses_previous_trade_date(self):
        latest = expected_latest_trade_date(
            self.con, datetime(2026, 7, 20, 14, 0)
        )

        self.assertEqual(latest, date(2026, 7, 17))

    def test_after_settlement_uses_current_trade_date(self):
        latest = expected_latest_trade_date(
            self.con, datetime(2026, 7, 20, 16, 0)
        )

        self.assertEqual(latest, date(2026, 7, 20))


if __name__ == "__main__":
    unittest.main()
