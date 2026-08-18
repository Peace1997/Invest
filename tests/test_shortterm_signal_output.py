from __future__ import annotations

import unittest
from datetime import date, datetime
from unittest.mock import patch

import pandas as pd

from ashare.signals.shortterm_output import enrich_selected_items
from ashare.signals import endday, premarket


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

    def test_push_uses_observed_price_instead_of_llm_generated_prices(self):
        candidates = pd.DataFrame([
            {
                "symbol": "000001", "name": "平安银行", "price": 10.8,
                "pct_chg": 3.2, "quote_time": datetime(2026, 8, 18, 9, 27),
                "industry": "软件开发", "is_tech": True,
                "pe_ttm": 15.0, "pb": 1.5, "profit_yoy": 20.0,
                "revenue_yoy": 12.0, "roe": 10.0,
                "valuation_date": date(2026, 8, 17),
                "report_date": date(2026, 6, 30),
                "ann_date": date(2026, 7, 30),
                "watch": "不追高于999元", "stop": "888元",
            },
        ])
        cfg = {
            "premarket": {"enabled": True, "picks": 5},
            "endday": {"enabled": True, "picks": 5},
            "sentiment": {},
            "alerts": {},
        }

        for module, runner in (
            (premarket, premarket.run_premarket),
            (endday, endday.run_endday),
        ):
            with self.subTest(module=module.__name__), \
                    patch.object(module, "_candidates", return_value=candidates), \
                    patch.object(module, "send_push", return_value=True) as push:
                runner(object(), cfg, force=True)

                body = push.call_args.args[1]
                self.assertIn("10.80", body)
                self.assertNotIn("999", body)
                self.assertNotIn("888", body)


if __name__ == "__main__":
    unittest.main()
