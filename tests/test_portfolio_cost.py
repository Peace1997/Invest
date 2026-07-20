from __future__ import annotations

import unittest

from ashare.portfolio import unit_cost_from_total


class PortfolioCostInputTest(unittest.TestCase):
    def test_total_cost_and_shares_are_converted_to_unit_cost(self):
        cost = unit_cost_from_total(4416.90, 300)

        self.assertAlmostEqual(cost, 14.723)

    def test_total_cost_requires_positive_shares(self):
        with self.assertRaisesRegex(ValueError, "份额必须大于0"):
            unit_cost_from_total(4416.90, 0)


if __name__ == "__main__":
    unittest.main()
