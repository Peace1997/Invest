from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

import duckdb
import yaml

from ashare.repositories import DecisionJournal
from tests.decision_fixtures import decision_signal


class DecisionCliTest(unittest.TestCase):
    def test_signals_outcome_runs_alongside_an_existing_read_only_market_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            market_path = tmp_path / "market.duckdb"
            market = duckdb.connect(market_path)
            market.execute(
                """
                CREATE TABLE calendar (trade_date DATE);
                INSERT INTO calendar VALUES
                    (DATE '2026-07-10'), (DATE '2026-07-13');
                CREATE TABLE daily_bar_adj (symbol VARCHAR, trade_date DATE, close DOUBLE);
                INSERT INTO daily_bar_adj VALUES
                    ('000001', DATE '2026-07-10', 10.0),
                    ('000001', DATE '2026-07-13', 11.0);
                CREATE TABLE index_bar (symbol VARCHAR, trade_date DATE, close DOUBLE);
                INSERT INTO index_bar VALUES
                    ('000300', DATE '2026-07-10', 4000.0),
                    ('000300', DATE '2026-07-13', 4040.0);
                CREATE TABLE fund_nav (symbol VARCHAR, nav_date DATE, nav DOUBLE);
                """
            )
            market.close()
            journal_path = tmp_path / "decisions.sqlite3"
            journal = DecisionJournal(journal_path)
            journal.record_many([decision_signal(
                created_at=datetime(2026, 7, 10, 15, 0),
                trade_date=date(2026, 7, 10),
                source="recommend",
                style="momentum",
                action="关注·买入观察",
                score=0.8,
                rationale="趋势向上",
            )])
            config_path = self._write_config(tmp_path, market_path, journal_path)
            guard = duckdb.connect(market_path, read_only=True)

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "ashare.cli",
                    "signals-outcome",
                    "--config",
                    str(config_path),
                ],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                check=False,
            )
            guard.close()

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("结算1条", result.stdout)
            self.assertEqual(journal.open_signals(), [])

    def test_signals_review_reads_sqlite_while_market_database_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            market_path = tmp_path / "market.duckdb"
            market = duckdb.connect(market_path)
            market.execute("CREATE TABLE calendar (trade_date DATE)")
            market.close()

            journal_path = tmp_path / "decisions.sqlite3"
            journal = DecisionJournal(journal_path)
            journal.record_many([decision_signal(
                source="recommend",
                style="momentum",
                action="关注·买入观察",
                score=0.8,
                rationale="趋势向上",
            )])
            journal.settle_many([{
                "signal_id": "signal-1",
                "evaluated_at": datetime(2026, 7, 13, 15, 0),
                "end_date": date(2026, 7, 13),
                "bars_forward": 1,
                "ref_price": 10.0,
                "end_price": 10.5,
                "fwd_return_pct": 5.0,
                "bench_return_pct": 1.0,
                "excess_pct": 4.0,
                "outcome": "hit",
                "note": None,
            }])
            config_path = self._write_config(tmp_path, market_path, journal_path)

            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "ashare.cli",
                    "signals-review",
                    "--config",
                    str(config_path),
                    "--window",
                    "90",
                ],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("recommend/momentum", result.stdout)
            self.assertIn("100.00", result.stdout)

    @staticmethod
    def _write_config(tmp_path: Path, market_path: Path, journal_path: Path) -> Path:
        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            yaml.safe_dump({
                "paths": {
                    "data_dir": str(tmp_path),
                    "warehouse": str(market_path),
                    "decision_journal": str(journal_path),
                    "raw_dir": str(tmp_path / "raw"),
                },
                "rate_limit": {"per_call_sleep": 0, "max_retries": 1},
                "decision_signal": {"enabled": True},
            }),
            encoding="utf-8",
        )
        return config_path


if __name__ == "__main__":
    unittest.main()
