from ashare.storage import open_db, init_schema
from ashare.ingest.tushare_fundamentals import backfill_fundamentals_ts, quarter_periods

con = open_db("data/warehouse.duckdb")
init_schema(con)
print("报告期:", quarter_periods(5), flush=True)
res = backfill_fundamentals_ts(con, years_back=5)
for p, n in res.items():
    print(f"  {p}: {n}", flush=True)
ok = {k: v for k, v in res.items() if v > 0}
print(f"落库报告期数: {len(ok)}/{len(res)}  总行数: {sum(ok.values())}", flush=True)
n = con.execute(
    "SELECT count(*), count(distinct symbol), min(end_date), max(end_date) FROM fundamentals_ts"
).fetchone()
print("fundamentals_ts: 行数=%d 股票数=%d 期间=%s~%s" % n, flush=True)
con.close()
