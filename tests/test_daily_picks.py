"""Daily-picks bridge. Fixtures are labeled fixture; nothing is invented."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd

from ashare.daily_picks import from_alphasift, render, screen_warehouse
from ashare.storage import init_schema, upsert

ROOT = Path(__file__).resolve().parents[1]


def _screen():
    return {
        "strategy": "dual_low",
        "mode": "fixture",
        "data_date": "2024-06-03",
        "snapshot_source": "fixture",
        "picks": [
            {"rank": 1, "code": "600001", "name": "夹具甲", "final_score": 90, "price": 10,
             "pe_ratio": 8, "pb_ratio": 0.7},
            {"rank": 2, "code": "600002", "name": "夹具乙", "final_score": 80, "price": 12,
             "pe_ratio": 9, "pb_ratio": 0.8},
            {"rank": 3, "code": "600003", "name": "夹具丙", "final_score": 70, "price": 11,
             "pe_ratio": 10, "pb_ratio": 0.9},
            {"rank": 4, "code": "600004", "name": "夹具丁", "final_score": 60, "price": 13,
             "pe_ratio": 11, "pb_ratio": 1.0},
        ],
    }


class AlphaSiftTests(unittest.TestCase):
    def test_fixture_keeps_three_and_cannot_be_relabelled_live(self):
        result = from_alphasift(_screen(), requested_mode="live", limit=3)
        self.assertEqual(result.mode, "fixture")
        self.assertEqual(result.source, "alphasift_dual_low")
        self.assertEqual(result.data_date, "2024-06-03")
        self.assertEqual([p.symbol for p in result.picks], ["600001", "600002", "600003"])
        first = result.picks[0]
        self.assertEqual(first.reference_price, 10.0)
        self.assertEqual(first.entry_low, 9.90)
        self.assertEqual(first.entry_cap, 10.20)
        self.assertEqual(first.invalidation_price, 9.70)
        self.assertIn("T+1", first.invalidation)
        self.assertIn("no order is sent", first.entry_discipline)
        text = render(result)
        self.assertIn("mode=fixture", text)
        self.assertIn("FIXTURE", text)
        self.assertNotIn("600004", text)

    def test_created_at_is_not_a_data_date(self):
        payload = _screen()
        del payload["data_date"]
        payload["created_at"] = "2024-06-04T01:02:03"
        result = from_alphasift(payload, requested_mode="replay")
        self.assertEqual(result.picks, [])
        self.assertIsNone(result.data_date)
        self.assertIn("data_date", result.note)

    def test_other_strategy_and_empty_screen_emit_nothing(self):
        other = _screen()
        other["strategy"] = "volume_breakout"
        self.assertEqual(from_alphasift(other).picks, [])
        empty = _screen()
        empty["picks"] = []
        result = from_alphasift(empty, requested_mode="live")
        self.assertEqual(result.mode, "fixture")
        self.assertEqual(result.picks, [])

    def test_replay_file_is_not_labeled_live(self):
        payload = _screen()
        del payload["mode"]
        result = from_alphasift(payload, requested_mode="replay")
        self.assertEqual(result.mode, "replay")
        live = from_alphasift(payload, requested_mode="live")
        self.assertEqual(live.mode, "live")
        self.assertEqual(live.banner.startswith("LIVE"), True)


class WarehouseTests(unittest.TestCase):
    def _con(self):
        con = duckdb.connect()
        init_schema(con)
        return con

    def _load(self, con, rows):
        bars = pd.DataFrame([{
            "symbol": r["symbol"], "trade_date": date(2024, 6, 3), "type": "stock",
            "open": r["close"], "high": r["close"], "low": r["close"], "close": r["close"],
            "volume": 1000, "amount": r["amount"], "amplitude": 1.0,
            "pct_chg": r["pct"], "change": 0.1, "turnover": 1.0,
        } for r in rows])
        vals = pd.DataFrame([{
            "symbol": r["symbol"], "trade_date": date(2024, 6, 3),
            "pe_ttm": r["pe"], "pb": r["pb"], "total_mv": r["mv"], "src": "tushare",
        } for r in rows])
        inst = pd.DataFrame([{
            "symbol": r["symbol"], "name": r["name"], "type": "stock", "exchange": "SH",
            "list_date": pd.NaT, "delist_date": pd.NaT, "is_st": r["st"], "sw_l1": None,
        } for r in rows])
        upsert(con, "daily_bar", bars, ["symbol", "trade_date"])
        upsert(con, "valuation_daily", vals, ["symbol", "trade_date", "src"])
        upsert(con, "instruments", inst, ["symbol"])

    def test_rank_is_deterministic_and_capped(self):
        con = self._con()
        self._load(con, [
            {"symbol": "600003", "name": "丙", "close": 11, "amount": 8e7, "pct": 1, "pe": 14, "pb": 1.8, "mv": 80, "st": False},
            {"symbol": "600001", "name": "甲", "close": 10, "amount": 9e7, "pct": 0, "pe": 5, "pb": 0.5, "mv": 100, "st": False},
            {"symbol": "600002", "name": "乙", "close": 12, "amount": 9e7, "pct": -1, "pe": 10, "pb": 1.0, "mv": 200, "st": False},
            {"symbol": "600009", "name": "贵", "close": 20, "amount": 9e7, "pct": 0, "pe": 40, "pb": 0.4, "mv": 100, "st": False},
            {"symbol": "600010", "name": "*ST样", "close": 4, "amount": 9e7, "pct": 0, "pe": 4, "pb": 0.4, "mv": 80, "st": True},
        ])
        result = screen_warehouse(con, as_of=date(2024, 6, 3), mode="fixture", limit=5)
        self.assertEqual(result.mode, "fixture")
        self.assertEqual(result.source, "warehouse_dual_low")
        self.assertEqual([p.symbol for p in result.picks], ["600001", "600002", "600003"])
        self.assertAlmostEqual(result.picks[0].score, 66.6667, places=4)
        self.assertEqual(result.picks[0].invalidation_price, 9.70)
        self.assertIn("T+1", result.picks[0].invalidation)
        again = screen_warehouse(con, as_of=date(2024, 6, 3), mode="fixture")
        self.assertEqual([p.symbol for p in again.picks], ["600001", "600002", "600003"])
        con.close()

    def test_one_name_is_not_a_pick(self):
        con = self._con()
        self._load(con, [
            {"symbol": "000001", "name": "平安银行", "close": 10, "amount": 9e7, "pct": 0,
             "pe": 5, "pb": 0.5, "mv": 100, "st": False},
        ])
        result = screen_warehouse(con, mode="live")
        self.assertEqual(result.picks, [])
        self.assertIn("need at least 2", result.note)
        self.assertIn("LIVE", render(result))
        con.close()

    def test_empty_warehouse(self):
        con = self._con()
        result = screen_warehouse(con, mode="live")
        self.assertEqual(result.picks, [])
        self.assertIsNone(result.data_date)
        con.close()


class CliTests(unittest.TestCase):
    def test_cli_prints_fixture_banner(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src")
        path = ROOT / "tests" / "_tmp_dual_low.json"
        payload = _screen()
        path.write_text(json.dumps(payload), encoding="utf-8")
        try:
            ran = subprocess.run(
                [sys.executable, "-m", "ashare.cli", "daily-picks", "--screen", str(path), "--live", "--json"],
                cwd=ROOT, env=env, capture_output=True, text=True,
            )
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        body = json.loads(ran.stdout)
        self.assertEqual(body["mode"], "fixture")
        self.assertEqual(body["count"], 3)
        self.assertTrue(body["banner"].startswith("FIXTURE"))
        self.assertIn("T+1", body["picks"][0]["invalidation"])


if __name__ == "__main__":
    unittest.main()
