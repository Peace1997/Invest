from __future__ import annotations

import unittest

from ashare.jobs.market_refresh import market_refresh_failures


class MarketRefreshFailuresTest(unittest.TestCase):
    def test_partial_stage_and_target_failures_prevent_success(self):
        result = {
            "errors": {},
            "daily": {"failed": [("stock", "000001", "source timeout")]},
            "fund_nav": {"failed": ["006663"]},
            "valuation": {"failed": ["005658"]},
            "stale_targets": {"002611": "2026-07-15"},
        }

        failures = market_refresh_failures(result)

        self.assertEqual(len(failures), 4)
        self.assertTrue(any("000001" in item for item in failures))
        self.assertTrue(any("006663" in item for item in failures))
        self.assertTrue(any("005658" in item for item in failures))
        self.assertTrue(any("002611" in item for item in failures))

    def test_complete_refresh_has_no_failures(self):
        result = {
            "errors": {},
            "daily": {"failed": []},
            "fund_nav": {"failed": []},
            "valuation": {"failed": []},
            "stale_targets": {},
        }

        self.assertEqual(market_refresh_failures(result), [])

    def test_global_stale_result_fails_without_exchange_traded_holdings(self):
        result = {
            "errors": {},
            "daily": {"failed": [], "stale": True},
            "fund_nav": {"failed": []},
            "valuation": {"failed": []},
            "stale_targets": {},
        }

        self.assertEqual(
            market_refresh_failures(result),
            ["整体行情未追到应有交易日"],
        )


if __name__ == "__main__":
    unittest.main()
