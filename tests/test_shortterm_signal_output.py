from __future__ import annotations

import unittest
from datetime import date

import pandas as pd

from ashare.signals.shortterm_output import enrich_selected_items


class ShorttermSignalOutputTest(unittest.TestCase):
    def test_selected_items_keep_the_decision_time_price_and_date(self):
        candidates = pd.DataFrame([
            {"symbol": "000001", "price": 10.8},
            {"symbol": "600519", "price": 1600.0},
        ])

        items = enrich_selected_items(
            [{"code": "000001", "name": "平安银行", "reason": "尾盘强势"}],
            candidates,
            as_of=date(2026, 7, 13),
            limit=5,
        )

        self.assertEqual(items[0]["ref_price"], 10.8)
        self.assertEqual(items[0]["trade_date"], "2026-07-13")


if __name__ == "__main__":
    unittest.main()
