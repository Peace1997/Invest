from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import duckdb

from ashare.decision_signals import (
    evaluate_due_signals,
    record_pf_signals,
    record_recommend_signals,
    record_shortterm_signals,
)
from ashare.repositories import DecisionJournal
from tests.decision_fixtures import decision_signal


class DecisionSignalEvaluationTest(unittest.TestCase):
    def test_recommendation_and_portfolio_paths_share_the_sqlite_journal(self):
        market = duckdb.connect(":memory:")
        market.execute(
            """
            CREATE TABLE calendar (trade_date DATE);
            INSERT INTO calendar VALUES (DATE '2026-07-13');
            CREATE TABLE daily_bar_adj (symbol VARCHAR, trade_date DATE, close DOUBLE);
            INSERT INTO daily_bar_adj VALUES
                ('000001', DATE '2026-07-13', 10.0),
                ('510300', DATE '2026-07-13', 4.0);
            CREATE TABLE index_bar (symbol VARCHAR, trade_date DATE, close DOUBLE);
            CREATE TABLE fund_nav (symbol VARCHAR, nav_date DATE, nav DOUBLE);
            """
        )
        with tempfile.TemporaryDirectory() as tmp:
            journal = DecisionJournal(Path(tmp) / "decisions.sqlite3")
            cfg = {
                "decision_signal": {
                    "enabled": True,
                    "horizons": {"momentum": 60, "pf": 20},
                }
            }
            stock = SimpleNamespace(
                target="000001",
                metadata={"name": "平安银行"},
                reason="趋势向上",
                components=[],
                score=0.8,
                as_of=date(2026, 7, 13),
            )
            fund = SimpleNamespace(
                target="510300",
                metadata={"name": "沪深300ETF", "type": "etf"},
                reason="宽基偏强",
                components=[],
                score=0.7,
                as_of=date(2026, 7, 13),
            )
            holding = SimpleNamespace(code="000001", type="stock")
            portfolio = SimpleNamespace(holdings=[holding])
            advice = SimpleNamespace(
                code="000001",
                name="平安银行",
                action="持有",
                score=0.2,
                reason="趋势正常",
            )

            self.assertEqual(
                record_recommend_signals(
                    cfg, market, [stock], [fund], "momentum", journal=journal
                ),
                2,
            )
            self.assertEqual(
                record_pf_signals(cfg, market, portfolio, [advice], journal=journal),
                1,
            )

            signals = journal.open_signals()
            self.assertEqual({signal["source"] for signal in signals}, {"recommend", "pf"})
            self.assertEqual(
                {signal["asset_type"] for signal in signals if signal["source"] == "recommend"},
                {"stock", "etf"},
            )
        market.close()

    def test_shortterm_signal_is_recorded_while_market_database_is_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            market_path = Path(tmp) / "market.duckdb"
            market = duckdb.connect(market_path)
            market.execute(
                """
                CREATE TABLE calendar (trade_date DATE);
                INSERT INTO calendar VALUES (DATE '2026-07-13');
                CREATE TABLE daily_bar_adj (symbol VARCHAR, trade_date DATE, close DOUBLE);
                INSERT INTO daily_bar_adj VALUES ('000001', DATE '2026-07-13', 10.0);
                CREATE TABLE index_bar (symbol VARCHAR, trade_date DATE, close DOUBLE);
                CREATE TABLE fund_nav (symbol VARCHAR, nav_date DATE, nav DOUBLE);
                """
            )
            market.close()
            market = duckdb.connect(market_path, read_only=True)
            journal = DecisionJournal(Path(tmp) / "decisions.sqlite3")
            cfg = {
                "decision_signal": {
                    "enabled": True,
                    "horizons": {"premarket": 1, "endday": 1},
                }
            }

            for source, ref_price in (("premarket", 10.1), ("endday", 10.8)):
                with self.subTest(source=source):
                    written = record_shortterm_signals(
                        cfg,
                        market,
                        source,
                        [{
                            "code": "000001",
                            "name": "平安银行",
                            "reason": "量价异动",
                            "ref_price": ref_price,
                            "trade_date": "2026-07-13",
                        }],
                        journal=journal,
                        as_of=date(2026, 7, 13),
                    )
                    self.assertEqual(written, 1)

            signals = journal.open_signals()
            self.assertEqual({signal["source"] for signal in signals}, {"premarket", "endday"})
            self.assertEqual(
                {signal["source"]: signal["ref_price"] for signal in signals},
                {"premarket": 10.1, "endday": 10.8},
            )
            market.close()

    def test_missing_market_data_is_retried_and_settled_when_data_arrives(self):
        market = duckdb.connect(":memory:")
        market.execute(
            """
            CREATE TABLE calendar (trade_date DATE);
            INSERT INTO calendar VALUES
                (DATE '2026-07-10'), (DATE '2026-07-13'), (DATE '2026-07-14');
            CREATE TABLE daily_bar_adj (symbol VARCHAR, trade_date DATE, close DOUBLE);
            INSERT INTO daily_bar_adj VALUES
                ('000001', DATE '2026-07-10', 10.0),
                ('000001', DATE '2026-07-13', 10.5);
            CREATE TABLE index_bar (symbol VARCHAR, trade_date DATE, close DOUBLE);
            INSERT INTO index_bar VALUES
                ('000300', DATE '2026-07-10', 4000.0),
                ('000300', DATE '2026-07-13', 4020.0),
                ('000300', DATE '2026-07-14', 4040.0);
            CREATE TABLE fund_nav (symbol VARCHAR, nav_date DATE, nav DOUBLE);
            """
        )
        with tempfile.TemporaryDirectory() as tmp:
            journal = DecisionJournal(Path(tmp) / "decisions.sqlite3")
            journal.record_many([decision_signal(
                created_at=datetime(2026, 7, 10, 15, 0),
                trade_date=date(2026, 7, 10),
                source="recommend",
                style="momentum",
                action="关注·买入观察",
                score=0.8,
                horizon_days=2,
                rationale="趋势向上",
            )])
            cfg = {"decision_signal": {"hit_excess_pct": 1.0}}

            first = evaluate_due_signals(
                cfg,
                market,
                journal=journal,
                as_of=date(2026, 7, 14),
            )

            self.assertEqual(first, {
                "scanned": 1,
                "closed": 0,
                "pending_data": 1,
                "unable": 0,
                "as_of": date(2026, 7, 14),
            })
            self.assertEqual(journal.open_signals()[0]["status"], "pending_data")

            market.execute(
                "INSERT INTO daily_bar_adj VALUES ('000001', DATE '2026-07-14', 11.0)"
            )
            second = evaluate_due_signals(
                cfg,
                market,
                journal=journal,
                as_of=date(2026, 7, 14),
            )

            self.assertEqual(second["closed"], 1)
            self.assertEqual(second["pending_data"], 0)
            self.assertEqual(journal.open_signals(), [])
            self.assertEqual(
                journal.review(90, as_of=date(2026, 7, 14))[0]["group"],
                "recommend/momentum",
            )

            market.execute("DELETE FROM daily_bar_adj WHERE trade_date=DATE '2026-07-14'")
            forced = evaluate_due_signals(
                cfg,
                market,
                force=True,
                journal=journal,
                as_of=date(2026, 7, 14),
            )
            self.assertEqual(forced["closed"], 0)
            self.assertEqual(
                journal.open_signals(include_closed=True)[0]["status"],
                "closed",
            )
        market.close()

    def test_missing_benchmark_data_is_retried_before_settlement(self):
        market = duckdb.connect(":memory:")
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
            INSERT INTO index_bar VALUES ('000300', DATE '2026-07-10', 4000.0);
            CREATE TABLE fund_nav (symbol VARCHAR, nav_date DATE, nav DOUBLE);
            """
        )
        with tempfile.TemporaryDirectory() as tmp:
            journal = DecisionJournal(Path(tmp) / "decisions.sqlite3")
            journal.record_many([decision_signal(
                created_at=datetime(2026, 7, 10, 15, 0),
                trade_date=date(2026, 7, 10),
                source="recommend",
                style="momentum",
                action="关注·买入观察",
                score=0.8,
                rationale="趋势向上",
            )])
            cfg = {"decision_signal": {"hit_excess_pct": 1.0}}

            first = evaluate_due_signals(
                cfg, market, journal=journal, as_of=date(2026, 7, 13)
            )
            self.assertEqual(first["pending_data"], 1)
            self.assertEqual(journal.open_signals()[0]["status"], "pending_data")

            market.execute(
                "INSERT INTO index_bar VALUES ('000300', DATE '2026-07-13', 4040.0)"
            )
            second = evaluate_due_signals(
                cfg, market, journal=journal, as_of=date(2026, 7, 13)
            )
            self.assertEqual(second["closed"], 1)
        market.close()


if __name__ == "__main__":
    unittest.main()
