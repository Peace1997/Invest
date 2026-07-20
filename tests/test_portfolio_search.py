from __future__ import annotations

import unittest

import duckdb

from ashare.portfolio import search_instruments


class PortfolioInstrumentSearchTest(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect(":memory:")
        self.con.execute(
            """
            CREATE TABLE instruments (
                symbol VARCHAR, name VARCHAR, type VARCHAR, exchange VARCHAR
            );
            INSERT INTO instruments VALUES
                ('000001', '平安银行', 'stock', 'SZ'),
                ('000300', '沪深300', 'index', 'SH'),
                ('510300', '沪深300ETF', 'etf', 'SH'),
                ('560700', '央企红利ETF', 'etf', 'SH'),
                ('600000', '浦发银行', 'stock', 'SH');
            """
        )

    def tearDown(self):
        self.con.close()

    def test_exact_code_match_is_returned_first(self):
        matches = search_instruments(self.con, "560700")

        self.assertEqual(matches[0], {
            "code": "560700",
            "name": "央企红利ETF",
            "type": "etf",
            "exchange": "SH",
        })

    def test_code_prefix_finds_matching_instruments(self):
        matches = search_instruments(self.con, "56")

        self.assertEqual([item["code"] for item in matches], ["560700"])

    def test_name_fragment_finds_matching_instruments(self):
        matches = search_instruments(self.con, "银行")

        self.assertEqual(
            {item["code"] for item in matches},
            {"000001", "600000"},
        )

    def test_result_count_respects_limit(self):
        matches = search_instruments(self.con, "银行", limit=1)

        self.assertEqual(len(matches), 1)

    def test_non_tradeable_index_is_not_returned(self):
        matches = search_instruments(self.con, "沪深300")

        self.assertEqual([item["code"] for item in matches], ["510300"])


if __name__ == "__main__":
    unittest.main()
