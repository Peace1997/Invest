"""把 2021 后退市的主板股行情补进 daily_bar, 消除回测幸存者偏差。
raw daily(不复权, 与 daily_bar 约定一致)。amount 千元→元; 只有 close 进回测, 单位差不影响。"""
import time
import pandas as pd
from ashare.storage import open_db, init_schema, upsert
from ashare.sources.tushare_src import get_pro

pro = get_pro()
dl = pro.stock_basic(list_status="D", fields="ts_code,delist_date")
main = dl[dl["ts_code"].str[:2].isin(["60", "00"])]
recent = main[main["delist_date"].fillna("0") >= "20210101"]
codes = list(recent["ts_code"])
print(f"待补退市主板股: {len(codes)} 只", flush=True)

con = open_db("data/warehouse.duckdb")
init_schema(con)
COLS = ["symbol", "trade_date", "type", "open", "high", "low", "close",
        "volume", "amount", "pct_chg", "change"]
tot = 0
for i, ts in enumerate(codes):
    for attempt in range(3):
        try:
            df = pro.daily(ts_code=ts, start_date="20200101", end_date="20260101")
            break
        except Exception:
            time.sleep(1.5); df = None
    if df is None or df.empty:
        continue
    df = df.copy()
    df["symbol"] = df["ts_code"].str[:6]
    df["trade_date"] = pd.to_datetime(df["trade_date"], format="%Y%m%d").dt.date
    df["type"] = "stock"
    df["volume"] = df["vol"]
    df["amount"] = df["amount"] * 1000.0   # 千元 → 元
    df["change"] = df["change"]
    tot += upsert(con, "daily_bar", df[COLS], ["symbol", "trade_date"])
    if (i + 1) % 40 == 0:
        print(f"  {i+1}/{len(codes)}  累计 {tot} 行", flush=True)
print(f"完成: 补入 {tot} 行, 来自 {len(codes)} 只退市股", flush=True)
con.close()
