import time
from ashare.storage import open_db, init_schema
from ashare.ingest.tushare_valuation import backfill_valuation_history

con = open_db("data/warehouse.duckdb")
init_schema(con)
t0 = time.time()
res = backfill_valuation_history(con, years_back=5, every=5)
print(f"weekly grid: {res['dates']} 天  落库 {res['rows']} 行  失败 {res['fail']} 天  "
      f"耗时 {time.time()-t0:.0f}s", flush=True)
n = con.execute(
    "SELECT count(*), count(distinct symbol), min(trade_date), max(trade_date) "
    "FROM valuation_daily WHERE src='tushare'").fetchone()
print("valuation_daily(tushare): 行数=%d 票数=%d 期间=%s~%s" % n, flush=True)
con.close()
