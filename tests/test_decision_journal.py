from __future__ import annotations

import tempfile
import unittest
import sqlite3
from datetime import date, datetime
from pathlib import Path

import duckdb

from ashare.repositories import DecisionJournal
from tests.decision_fixtures import decision_signal


class DecisionJournalTest(unittest.TestCase):
    def test_review_details_filters_by_source_and_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = DecisionJournal(Path(tmp) / "decisions.sqlite3")
            journal.record_many([
                decision_signal(signal_id="active-1", source="premarket"),
                decision_signal(
                    signal_id="unable-1",
                    source="pf",
                    status="unable",
                ),
            ])

            self.assertEqual(
                [
                    row["signal_id"]
                    for row in journal.review_details(
                        90,
                        source="pf",
                        status="unable",
                        as_of=date(2026, 7, 18),
                    )
                ],
                ["unable-1"],
            )

    def test_existing_single_horizon_database_is_upgraded_without_data_loss(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.sqlite3"
            con = sqlite3.connect(path)
            con.executescript(
                """
                CREATE TABLE decision_signal (
                    signal_id TEXT PRIMARY KEY, created_at TIMESTAMP,
                    trade_date DATE, source TEXT, style TEXT, symbol TEXT,
                    name TEXT, asset_type TEXT, action TEXT, score REAL,
                    ref_price REAL, horizon_days INTEGER, factors TEXT,
                    rationale TEXT, status TEXT
                );
                CREATE TABLE decision_outcome (
                    signal_id TEXT PRIMARY KEY, evaluated_at TIMESTAMP,
                    end_date DATE, bars_forward INTEGER, ref_price REAL,
                    end_price REAL, fwd_return_pct REAL, bench_return_pct REAL,
                    excess_pct REAL, outcome TEXT, note TEXT
                );
                INSERT INTO decision_signal VALUES (
                    'signal-1', '2026-07-13T09:30:00', '2026-07-13',
                    'premarket', NULL, '000001', '平安银行', 'stock',
                    '短线关注', NULL, 10.0, 20, '{}', '量价异动', 'active'
                );
                INSERT INTO decision_outcome VALUES (
                    'signal-1', '2026-07-14T15:00:00', '2026-07-14', 1,
                    10.0, 10.5, 5.0, 1.0, 4.0, 'hit', NULL
                );
                """
            )
            con.close()

            journal = DecisionJournal(path)
            journal.record_outcomes([{
                "signal_id": "signal-1",
                "evaluated_at": datetime(2026, 7, 18, 15, 0),
                "end_date": date(2026, 7, 18),
                "bars_forward": 5,
                "ref_price": 10.0,
                "end_price": 11.0,
                "fwd_return_pct": 10.0,
                "bench_return_pct": 2.0,
                "excess_pct": 8.0,
                "outcome": "hit",
                "note": None,
            }])

            detail = journal.review_details(90, as_of=date(2026, 7, 18))[0]
            self.assertEqual(set(detail["outcomes"]), {1, 5})

    def test_review_details_keeps_multiple_horizons_for_one_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = DecisionJournal(Path(tmp) / "decisions.sqlite3")
            journal.record_many([decision_signal(horizon_days=20)])
            base = {
                "signal_id": "signal-1",
                "evaluated_at": datetime(2026, 7, 14, 15, 0),
                "ref_price": 10.0,
                "bench_return_pct": 1.0,
                "note": None,
            }

            journal.record_outcomes([
                {
                    **base,
                    "end_date": date(2026, 7, 14),
                    "bars_forward": 1,
                    "end_price": 10.5,
                    "fwd_return_pct": 5.0,
                    "excess_pct": 4.0,
                    "outcome": "hit",
                },
                {
                    **base,
                    "end_date": date(2026, 7, 18),
                    "bars_forward": 5,
                    "end_price": 9.5,
                    "fwd_return_pct": -5.0,
                    "excess_pct": -6.0,
                    "outcome": "miss",
                },
            ])

            self.assertEqual(
                journal.review_details(90, as_of=date(2026, 7, 18)),
                [{
                    "signal_id": "signal-1",
                    "trade_date": date(2026, 7, 13),
                    "source": "premarket",
                    "style": None,
                    "symbol": "000001",
                    "name": "平安银行",
                    "action": "短线关注",
                    "rationale": "量价异动",
                    "status": "active",
                    "verdict": "失效",
                    "outcomes": {
                        1: {
                            "end_date": date(2026, 7, 14),
                            "fwd_return_pct": 5.0,
                            "excess_pct": 4.0,
                            "outcome": "hit",
                        },
                        5: {
                            "end_date": date(2026, 7, 18),
                            "fwd_return_pct": -5.0,
                            "excess_pct": -6.0,
                            "outcome": "miss",
                        },
                    },
                }],
            )
            self.assertEqual(
                journal.review(
                    90,
                    horizon_days=5,
                    status="active",
                    as_of=date(2026, 7, 18),
                ),
                [{
                    "group": "premarket",
                    "sample_count": 1,
                    "hit_rate_pct": 0.0,
                    "neutral_rate_pct": 0.0,
                    "avg_fwd_return_pct": -5.0,
                    "avg_excess_pct": -6.0,
                }],
            )
            self.assertEqual(
                journal.review(
                    90,
                    horizon_days=5,
                    status="closed",
                    as_of=date(2026, 7, 18),
                ),
                [],
            )

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
