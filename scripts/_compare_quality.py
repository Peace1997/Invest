"""核心实验: 价值版 加/不加 ROE质量, 其余完全相同 → 测质量能否过滤价值陷阱。"""
import sys
from ashare.storage import open_db
from ashare.backtest import run_backtest

YEARS = int(sys.argv[1]) if len(sys.argv) > 1 else 3
con = open_db("data/warehouse.duckdb", read_only=True)
common = dict(style="value", years_back=YEARS, horizon=60, rebalance=21,
              top_n=12, universe="main")

print(f"\n######## 窗口 {YEARS} 年 · 全主板 · 持有60日 · 月度调仓 ########")
for tag, uq in [("A) 纯价值(无质量)", False), ("B) 价值 + ROE质量过滤", True)]:
    r = run_backtest(con, use_quality=uq, **common)
    print(f"\n==== {tag} ====")
    print(r["text"])
    if r.get("ok"):
        print(f">> 摘要: 胜率={r['hit']*100:.1f}%  超额均值={r['avg_excess']*100:+.2f}%  "
              f"IC={r['ic_mean']:+.3f}  高低价差={r['spread']}  单调={r['monotonic']}")
con.close()
