"""Tushare HTTP client and sample ingest. No network and no token."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from datetime import date, datetime
from pathlib import Path

import duckdb
import pandas as pd

from ashare.config import load
from ashare.ingest.tushare_sample import (
    ingest_sample,
    sample_universe,
    ts_code_for,
)
from ashare.sources.tushare_http import (
    TOKEN_ENV,
    URL_ENV,
    TushareConfigError,
    TushareHttpError,
    query,
)
from ashare.storage import init_schema

ROOT = Path(__file__).resolve().parents[1]
_HTTP = ROOT / "src" / "ashare" / "sources" / "tushare_http.py"


def _frame(columns, rows):
    return pd.DataFrame(rows, columns=columns)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self._env = {TOKEN_ENV: os.environ.get(TOKEN_ENV), URL_ENV: os.environ.get(URL_ENV)}
        os.environ.pop(TOKEN_ENV, None)
        os.environ.pop(URL_ENV, None)

    def tearDown(self):
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_missing_env_names_the_variables(self):
        with self.assertRaises(TushareConfigError) as caught:
            query("trade_cal")
        text = str(caught.exception)
        self.assertIn(TOKEN_ENV, text)
        self.assertIn(URL_ENV, text)

    def test_szse_host_is_rejected(self):
        os.environ[TOKEN_ENV] = "test-token"
        os.environ[URL_ENV] = "https://www.szse.cn/api"
        with self.assertRaises(TushareConfigError) as caught:
            query("daily", {"ts_code": "000001.SZ"})
        self.assertIn("szse.cn", str(caught.exception))

    def test_query_posts_env_and_parses_items(self):
        seen = {}

        def transport(url, body):
            seen["url"] = url
            seen["body"] = body
            return json.dumps({
                "code": 0,
                "data": {
                    "fields": ["ts_code", "trade_date", "close"],
                    "items": [["000001.SZ", "20240603", 10.5]],
                },
            })

        os.environ[TOKEN_ENV] = "test-token"
        os.environ[URL_ENV] = "https://example.test/tushare"
        df = query("daily", {"ts_code": "000001.SZ", "start_date": "20240601"}, "ts_code,trade_date,close",
                   transport=transport)
        self.assertEqual(seen["url"], "https://example.test/tushare")
        self.assertEqual(seen["body"]["token"], "test-token")
        self.assertEqual(seen["body"]["api_name"], "daily")
        self.assertEqual(seen["body"]["params"]["ts_code"], "000001.SZ")
        self.assertEqual(df.loc[0, "close"], 10.5)

        os.environ[TOKEN_ENV] = "rotated-token"
        os.environ[URL_ENV] = "https://example.test/other"
        query("daily", transport=transport)
        self.assertEqual(seen["url"], "https://example.test/other")
        self.assertEqual(seen["body"]["token"], "rotated-token")

    def test_error_text_drops_the_token(self):
        os.environ[TOKEN_ENV] = "super-secret-token"
        os.environ[URL_ENV] = "https://example.test/tushare"

        def transport(url, body):
            raise RuntimeError(f"denied {body['token']}")

        with self.assertRaises(TushareHttpError) as caught:
            query("daily", transport=transport)
        self.assertNotIn("super-secret-token", str(caught.exception))
        self.assertIn("***", str(caught.exception))

    def test_source_has_no_builtin_endpoint(self):
        text = _HTTP.read_text(encoding="utf-8")
        self.assertNotIn("api.tushare.pro", text)
        self.assertNotIn("dailyfetch", text)
        self.assertNotIn("https://www.szse.cn", text)
        self.assertNotIn("http://www.szse.cn", text)


class IngestTests(unittest.TestCase):
    def test_sample_codes_match_config(self):
        self.assertEqual(
            sample_universe(load()),
            [("000300", "index"), ("510300", "etf"), ("000001", "stock")],
        )
        self.assertEqual(ts_code_for("000300", "index"), "000300.SH")
        self.assertEqual(ts_code_for("510300", "etf"), "510300.SH")
        self.assertEqual(ts_code_for("000001", "stock"), "000001.SZ")
        self.assertEqual(ts_code_for("399006", "index"), "399006.SZ")

    def test_ingest_writes_calendar_metadata_and_bars(self):
        calls = []

        def query(api_name, params, fields):
            calls.append((api_name, dict(params)))
            code = params.get("ts_code")
            if api_name == "trade_cal":
                return _frame(
                    ["exchange", "cal_date", "is_open"],
                    [["SSE", "20240602", "0"], ["SSE", "20240603", "1"], ["SSE", "20240604", "1"]],
                )
            if api_name == "index_basic":
                return _frame(["ts_code", "name", "market", "list_date"],
                              [["000300.SH", "沪深300", "CSI", "20050408"]])
            if api_name == "fund_basic":
                return _frame(["ts_code", "name", "market", "list_date", "delist_date"],
                              [["510300.SH", "沪深300ETF", "E", "20120528", None]])
            if api_name == "stock_basic":
                if code == "000002.SZ":
                    return _frame(["ts_code", "name", "exchange", "list_date"],
                                  [["000002.SZ", "ST测试", "SZSE", "19910129"]])
                return _frame(["ts_code", "name", "exchange", "list_date"],
                              [["000001.SZ", "平安银行", "SZSE", "19910403"]])
            if api_name == "daily_basic":
                if code == "000002.SZ":
                    return pd.DataFrame()
                return _frame(
                    ["ts_code", "trade_date", "turnover_rate", "pe_ttm", "pb", "total_mv"],
                    [["000001.SZ", "20240603", 1.25, 5.5, 0.6, 1_000_000]],
                )
            if code == "000002.SZ":
                return pd.DataFrame()
            day = "20240604" if api_name == "index_daily" else "20240603"
            return _frame(
                ["ts_code", "trade_date", "open", "high", "low", "close", "pre_close",
                 "change", "pct_chg", "vol", "amount"],
                [[code, day, 10, 11, 9, 10.5, 10, 0.5, 5, 100, 12.5]],
            )

        con = duckdb.connect()
        init_schema(con)
        res = ingest_sample(
            con, query,
            [("000300", "index"), ("510300", "etf"), ("000001", "stock"), ("000002", "stock")],
            "20240601", "20240604",
            now=datetime(2024, 6, 4, 10, 0),
        )
        apis = {name for name, _ in calls}
        self.assertTrue(apis <= {
            "trade_cal", "stock_basic", "fund_basic", "index_basic",
            "daily", "daily_basic", "fund_daily", "index_daily",
        })
        self.assertFalse(any("szse.cn" in json.dumps(params) for _, params in calls))
        self.assertEqual(con.execute("SELECT trade_date FROM calendar ORDER BY 1").fetchall(),
                         [(date(2024, 6, 3),), (date(2024, 6, 4),)])
        names = {r[0]: r[1:] for r in con.execute(
            "SELECT symbol, name, type, exchange, is_st FROM instruments").fetchall()}
        self.assertEqual(names["000300"], ("沪深300", "index", "SH", False))
        self.assertEqual(names["510300"], ("沪深300ETF", "etf", "SH", False))
        self.assertEqual(names["000001"], ("平安银行", "stock", "SZ", False))
        self.assertEqual(names["000002"], ("ST测试", "stock", "SZ", True))
        stock = con.execute(
            "SELECT close, volume, amount, amplitude, turnover, type FROM daily_bar WHERE symbol='000001'"
        ).fetchone()
        self.assertEqual(stock[5], "stock")
        self.assertAlmostEqual(stock[0], 10.5)
        self.assertEqual(stock[1], 100)
        self.assertAlmostEqual(stock[2], 12500.0)
        self.assertAlmostEqual(stock[3], 20.0)
        self.assertAlmostEqual(stock[4], 1.25)
        self.assertEqual(con.execute("SELECT count(*) FROM daily_bar WHERE symbol='510300'").fetchone()[0], 1)
        # 2024-06-04 10:00 is before the close cutoff, so the index's same-day bar is not stored.
        self.assertEqual(con.execute("SELECT count(*) FROM index_bar").fetchone()[0], 0)
        self.assertTrue(any("000300 bars" in item for item in res["failed"]))
        self.assertTrue(any("000002 bars" in item for item in res["failed"]))
        mv = con.execute("SELECT total_mv, pe_ttm, src FROM valuation_daily").fetchone()
        self.assertAlmostEqual(mv[0], 100.0)
        self.assertAlmostEqual(mv[1], 5.5)
        self.assertEqual(mv[2], "tushare")
        con.close()


class CliImportTests(unittest.TestCase):
    def test_light_cli_does_not_import_akshare(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src")
        env.pop("TUSHARE_TOKEN", None)
        env.pop("TUSHARE_HTTP_URL", None)
        script = (
            "import ashare.cli, ashare.daily_picks, ashare.ingest.tushare_sample, sys\n"
            "assert 'akshare' not in sys.modules\n"
        )
        ran = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env,
                             capture_output=True, text=True)
        self.assertEqual(ran.returncode, 0, ran.stderr)

    def test_tushare_sample_without_env_exits_before_creating_a_warehouse(self):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src")
        env.pop("TUSHARE_TOKEN", None)
        env.pop("TUSHARE_HTTP_URL", None)
        warehouse = ROOT / "data" / "warehouse.duckdb"
        existed = warehouse.exists()
        ran = subprocess.run(
            [sys.executable, "-m", "ashare.cli", "tushare-sample", "--start", "20240601", "--end", "20240604"],
            cwd=ROOT, env=env, capture_output=True, text=True,
        )
        self.assertEqual(ran.returncode, 2)
        self.assertIn("TUSHARE_TOKEN", ran.stdout)
        self.assertIn("TUSHARE_HTTP_URL", ran.stdout)
        self.assertEqual(warehouse.exists(), existed)


if __name__ == "__main__":
    unittest.main()
