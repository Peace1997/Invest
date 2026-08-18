from __future__ import annotations

import unittest

import pandas as pd

from ashare.signals.value_growth import rank_value_growth_candidates


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


if __name__ == "__main__":
    unittest.main()
