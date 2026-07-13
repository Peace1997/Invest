from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path

import duckdb

from ashare.repositories import DecisionJournal
from tests.decision_fixtures import decision_signal


class DecisionJournalTest(unittest.TestCase):
    def test_recording_the_same_signal_is_idempotent_and_retrievable(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = DecisionJournal(Path(tmp) / "decisions.sqlite3")
            signal = decision_signal()

            self.assertEqual(journal.record_many([signal]), 1)
            self.assertEqual(journal.record_many([signal]), 1)

            self.assertEqual(journal.open_signals(), [signal])

            journal.settle_many([{
                "signal_id": "signal-1",
                "evaluated_at": datetime(2026, 7, 14, 15, 0),
                "end_date": date(2026, 7, 14),
                "bars_forward": 1,
                "ref_price": 10.0,
                "end_price": 10.5,
                "fwd_return_pct": 5.0,
                "bench_return_pct": 1.0,
                "excess_pct": 4.0,
                "outcome": "hit",
                "note": None,
            }])
            changed_signal = {**signal, "ref_price": 99.0, "action": "改写动作"}
            journal.record_many([changed_signal])

            closed = journal.open_signals(include_closed=True)[0]
            self.assertEqual(closed["status"], "closed")
            self.assertEqual(closed["ref_price"], 10.0)
            self.assertEqual(closed["action"], "短线关注")

    def test_legacy_duckdb_signals_are_migrated_idempotently(self):
        legacy = duckdb.connect(":memory:")
        legacy.execute(
            """
            CREATE TABLE decision_signal (
                signal_id VARCHAR PRIMARY KEY, created_at TIMESTAMP,
                trade_date DATE, source VARCHAR, style VARCHAR, symbol VARCHAR,
                name VARCHAR, asset_type VARCHAR, action VARCHAR, score DOUBLE,
                ref_price DOUBLE, horizon_days INTEGER, factors VARCHAR,
                rationale VARCHAR, status VARCHAR
            );
            CREATE TABLE decision_outcome (
                signal_id VARCHAR PRIMARY KEY, evaluated_at TIMESTAMP,
                end_date DATE, bars_forward INTEGER, ref_price DOUBLE,
                end_price DOUBLE, fwd_return_pct DOUBLE, bench_return_pct DOUBLE,
                excess_pct DOUBLE, outcome VARCHAR, note VARCHAR
            );
            INSERT INTO decision_signal VALUES (
                'legacy-1', TIMESTAMP '2026-07-09 15:00:00', DATE '2026-07-09',
                'pf', NULL, '000001', '平安银行', 'stock', '持有', 0.2,
                10.0, 20, '{}', '趋势正常', 'active'
            );
            """
        )
        with tempfile.TemporaryDirectory() as tmp:
            journal = DecisionJournal(Path(tmp) / "decisions.sqlite3")

            self.assertEqual(
                journal.migrate_from_duckdb(legacy),
                {"signals": 1, "outcomes": 0},
            )
            journal.migrate_from_duckdb(legacy)

            signals = journal.open_signals()
            self.assertEqual(len(signals), 1)
            self.assertEqual(signals[0]["signal_id"], "legacy-1")

            journal.settle_many([{
                "signal_id": "legacy-1",
                "evaluated_at": datetime(2026, 7, 14, 15, 0),
                "end_date": date(2026, 7, 14),
                "bars_forward": 2,
                "ref_price": 10.0,
                "end_price": 11.0,
                "fwd_return_pct": 10.0,
                "bench_return_pct": 2.0,
                "excess_pct": 8.0,
                "outcome": "hit",
                "note": None,
            }])
            journal.migrate_from_duckdb(legacy)

            self.assertEqual(
                journal.open_signals(include_closed=True)[0]["status"],
                "closed",
            )

        legacy.close()

    def test_settlement_closes_a_signal_and_appears_in_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = DecisionJournal(Path(tmp) / "decisions.sqlite3")
            journal.record_many([
                decision_signal(
                    source="recommend",
                    style="momentum",
                    action="关注·买入观察",
                )
            ])
            outcome = {
                "signal_id": "signal-1",
                "evaluated_at": datetime(2026, 7, 14, 15, 0),
                "end_date": date(2026, 7, 14),
                "bars_forward": 1,
                "ref_price": 10.0,
                "end_price": 10.5,
                "fwd_return_pct": 5.0,
                "bench_return_pct": 1.0,
                "excess_pct": 4.0,
                "outcome": "hit",
                "note": None,
            }

            self.assertEqual(journal.settle_many([outcome]), 1)
            self.assertEqual(journal.open_signals(), [])
            self.assertEqual(journal.open_signals(include_closed=True)[0]["status"], "closed")
            self.assertEqual(
                journal.review(window_days=90, as_of=date(2026, 7, 14)),
                [{
                    "group": "recommend/momentum",
                    "sample_count": 1,
                    "hit_rate_pct": 100.0,
                    "neutral_rate_pct": 0.0,
                    "avg_fwd_return_pct": 5.0,
                    "avg_excess_pct": 4.0,
                }],
            )


if __name__ == "__main__":
    unittest.main()
