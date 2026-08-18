from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch

import pandas as pd

from ashare.signals import endday, premarket
from ashare.signals.value_growth import attach_live_quotes


class ShorttermSignalOutputTest(unittest.TestCase):
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

    def test_live_quotes_must_be_positive_and_fresh_at_decision_time(self):
        now = datetime(2026, 8, 18, 14, 30)
        candidates = pd.DataFrame([
            {"symbol": symbol, "name": symbol}
            for symbol in ("000001", "000002", "000003", "000004")
        ])

        class FakeSource:
            def spot(self, symbols):
                return pd.DataFrame([
                    {"symbol": "000001", "price": 10.8, "prev_close": 10.5,
                     "pct_chg": 2.86, "quote_time": now - timedelta(minutes=2)},
                    {"symbol": "000002", "price": 20.0, "prev_close": 19.0,
                     "pct_chg": 5.26, "quote_time": now - timedelta(hours=5)},
                    {"symbol": "000003", "price": 0, "prev_close": 8.0,
                     "pct_chg": None, "quote_time": now},
                    {"symbol": "000004", "price": 12.0, "prev_close": 12.0,
                     "pct_chg": 0, "quote_time": None},
                ])

        result = attach_live_quotes(candidates, source=FakeSource(), now=now)

        self.assertEqual(result["symbol"].tolist(), ["000001"])


if __name__ == "__main__":
    unittest.main()
