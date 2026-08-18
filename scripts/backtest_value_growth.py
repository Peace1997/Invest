#!/usr/bin/env python3
"""Reproduce the production value/growth screen on historical announcement dates."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date

from ashare.signals.value_growth import screen_value_growth
from ashare.storage import open_db


def run(con, *, start: date, horizon: int, rebalance: int, top_n: int) -> dict:
    calendar = [
        row[0] for row in con.execute(
            """
            SELECT trade_date FROM index_bar
            WHERE symbol='000300' AND trade_date >= ? AND close > 0
            ORDER BY trade_date
            """,
            [start],
        ).fetchall()
    ]
    observations = []
    for index in range(1, len(calendar) - horizon, rebalance):
        trade_date = calendar[index]
        signal_date = calendar[index - 1]
        exit_date = calendar[index + horizon]
        picks = screen_value_growth(con, limit=top_n, as_of=signal_date)
        if picks.empty:
            continue
        symbols = picks["symbol"].astype(str).tolist()
        placeholders = ",".join("?" for _ in symbols)
        prices = con.execute(
            f"""
            SELECT symbol,
                   max(close) FILTER (WHERE trade_date=?) AS entry_close,
                   arg_max(close, trade_date) AS exit_close
            FROM daily_bar_adj
            WHERE symbol IN ({placeholders})
              AND trade_date BETWEEN ? AND ?
            GROUP BY symbol
            """,
            [trade_date, *symbols, trade_date, exit_date],
        ).df()
        prices = prices.dropna(subset=["entry_close", "exit_close"])
        prices = prices[prices["entry_close"] > 0]
        if prices.empty:
            continue
        stock_return = (prices["exit_close"] / prices["entry_close"] - 1).mean()
        benchmark = con.execute(
            """
            SELECT max(close) FILTER (WHERE trade_date=?),
                   max(close) FILTER (WHERE trade_date=?)
            FROM index_bar WHERE symbol='000300' AND trade_date IN (?, ?)
            """,
            [trade_date, exit_date, trade_date, exit_date],
        ).fetchone()
        benchmark_return = benchmark[1] / benchmark[0] - 1
        observations.append({
            "date": trade_date,
            "return": float(stock_return),
            "excess": float(stock_return - benchmark_return),
            "tech_share": float(picks["is_tech"].mean()),
            "picks": len(prices),
        })

    yearly = defaultdict(list)
    for row in observations:
        yearly[row["date"].year].append(row["excess"])
    count = len(observations)
    if count == 0:
        return {"horizon": horizon, "observations": 0}
    return {
        "horizon": horizon,
        "observations": count,
        "average_return": sum(row["return"] for row in observations) / count,
        "average_excess": sum(row["excess"] for row in observations) / count,
        "win_rate": sum(row["excess"] > 0 for row in observations) / count,
        "tech_share": sum(row["tech_share"] for row in observations) / count,
        "average_picks": sum(row["picks"] for row in observations) / count,
        "yearly_excess": {
            year: sum(values) / len(values) for year, values in sorted(yearly.items())
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/warehouse.duckdb")
    parser.add_argument("--start", type=date.fromisoformat, default=date(2021, 8, 1))
    parser.add_argument("--rebalance", type=int, default=20)
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--horizons", type=int, nargs="+", default=[20, 60])
    args = parser.parse_args()
    con = open_db(args.db, read_only=True)
    try:
        for horizon in args.horizons:
            result = run(
                con,
                start=args.start,
                horizon=horizon,
                rebalance=args.rebalance,
                top_n=args.top,
            )
            if result["observations"] == 0:
                print(
                    f"T+{horizon}: no observations; this warehouse lacks the "
                    "required historical prices, valuations, or announced fundamentals"
                )
                continue
            print(
                f"T+{horizon}: n={result['observations']} "
                f"return={result['average_return']:+.2%} "
                f"excess={result['average_excess']:+.2%} "
                f"win={result['win_rate']:.1%} "
                f"tech={result['tech_share']:.1%} "
                f"picks={result['average_picks']:.2f}"
            )
            print("yearly excess:", " ".join(
                f"{year}={value:+.2%}"
                for year, value in result["yearly_excess"].items()
            ))
    finally:
        con.close()


if __name__ == "__main__":
    main()
