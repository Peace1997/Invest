from __future__ import annotations

import unittest

from ashare.portfolio import unit_cost_from_price, unit_cost_from_total


class PortfolioCostInputTest(unittest.TestCase):
    def test_total_cost_and_shares_are_converted_to_unit_cost(self):
        cost = unit_cost_from_total(4416.90, 300)

        self.assertAlmostEqual(cost, 14.723)

    def test_total_cost_requires_positive_shares(self):
        with self.assertRaisesRegex(ValueError, "份额必须大于0"):
            unit_cost_from_total(4416.90, 0)

    def test_stock_cost_price_is_kept_as_unit_cost(self):
        self.assertEqual(unit_cost_from_price(14.723, 300), 14.723)

    def test_stock_cost_price_requires_positive_shares(self):
        with self.assertRaisesRegex(ValueError, "股数必须大于0"):
            unit_cost_from_price(14.723, 0)


if __name__ == "__main__":
    unittest.main()
