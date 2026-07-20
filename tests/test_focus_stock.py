from __future__ import annotations

import unittest

import duckdb
import pandas as pd

from ashare.focus_stock import (
    _momentum_bucket,
    _non_overlapping_sample,
    conditional_forward_forecast,
    focus_stock_data_status,
)


def _market_with_prices(prices: list[float]):
    con = duckdb.connect(":memory:")
    dates = pd.date_range("2025-01-02", periods=len(prices), freq="B")
    bars = pd.DataFrame({
        "symbol": "002611",
        "trade_date": dates,
        "close": prices,
        "type": "stock",
    })
    factors = bars[["symbol", "trade_date"]].assign(adj_factor=1.0)
    con.register("bars_input", bars)
    con.register("factors_input", factors)
    con.execute("CREATE TABLE daily_bar AS SELECT * FROM bars_input")
    con.execute("CREATE TABLE adj_factor AS SELECT * FROM factors_input")
    return con, dates


class FocusStockForecastTest(unittest.TestCase):
    def test_focus_stock_dates_distinguish_stale_market_data_from_research_date(self):
        status = focus_stock_data_status(
            market_date="2026-07-15",
            expected_market_date="2026-07-20",
            research_date="2026-07-16",
        )

        self.assertTrue(status["market_stale"])
        self.assertEqual(status["market_date"], "2026-07-15")
        self.assertEqual(status["expected_market_date"], "2026-07-20")
        self.assertEqual(status["research_date"], "2026-07-16")

    def test_momentum_bucket_boundaries_are_stable(self):
        self.assertEqual(_momentum_bucket(-10), "20日≤-10%")
        self.assertEqual(_momentum_bucket(-0.01), "20日-10%~0%")
        self.assertEqual(_momentum_bucket(0), "20日0%~10%")
        self.assertEqual(_momentum_bucket(9.99), "20日0%~10%")
        self.assertEqual(_momentum_bucket(10), "20日≥10%")
        self.assertIsNone(_momentum_bucket(None))

    def test_forward_samples_do_not_overlap(self):
        forward = pd.Series(range(10), dtype=float)
        mask = pd.Series(
            [pd.NA, True, True, True, True, True, True, True, True, True],
            dtype="boolean",
        )

        sample = _non_overlapping_sample(forward, mask, horizon=3)

        self.assertEqual(sample.index.tolist(), [1, 4, 7])

    def test_exact_regime_returns_auditable_positive_distribution(self):
        prices = [100.0 * (1.005 ** day) for day in range(260)]
        con, _ = _market_with_prices(prices)

        result = conditional_forward_forecast(
            con, "2611", horizons=(5,), min_samples=10
        )

        self.assertTrue(result["ok"])
        horizon = result["horizons"][0]
        self.assertEqual(horizon["condition"], "MA60位置 + 20日动量档（不重叠样本）")
        self.assertGreaterEqual(horizon["samples"], 10)
        self.assertEqual(horizon["win_rate"], 100.0)
        self.assertGreater(horizon["median_return"], 0)
        con.close()

    def test_sparse_exact_regime_falls_back_to_ma60_side(self):
        prices = [100.0] * 220
        prices.extend(100.0 * (1.005 ** day) for day in range(1, 41))
        con, _ = _market_with_prices(prices)

        result = conditional_forward_forecast(
            con, "002611", horizons=(1,), min_samples=30
        )

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["horizons"][0]["condition"],
            "仅MA60位置（精确状态样本不足；不重叠样本）",
        )
        self.assertGreaterEqual(result["horizons"][0]["samples"], 30)
        con.close()

    def test_structural_break_rejects_insufficient_post_break_history(self):
        prices = [100.0 * (1.002 ** day) for day in range(260)]
        con, dates = _market_with_prices(prices)

        result = conditional_forward_forecast(
            con,
            "002611",
            regime_start=str(dates[-50].date()),
        )

        self.assertFalse(result["ok"])
        self.assertIn("当前仅 50 条", result["error"])
        con.close()

    def test_forecast_rejects_horizons_below_final_sample_floor(self):
        prices = [100.0 * (1.002 ** day) for day in range(100)]
        con, _ = _market_with_prices(prices)

        result = conditional_forward_forecast(
            con, "002611", horizons=(20,), min_samples=100
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["horizons"], [])
        self.assertIn("至少 100 个不重叠样本", result["error"])
        con.close()

    def test_non_positive_horizon_is_rejected(self):
        prices = [100.0 * (1.002 ** day) for day in range(100)]
        con, _ = _market_with_prices(prices)

        with self.assertRaisesRegex(ValueError, "horizons must be positive"):
            conditional_forward_forecast(con, "002611", horizons=(0,), min_samples=1)
        con.close()


if __name__ == "__main__":
    unittest.main()
