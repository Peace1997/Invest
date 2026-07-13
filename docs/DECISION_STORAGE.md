# 决策信号存储

决策闭环采用两种本地存储，各自只承担适合的工作负载：

- DuckDB (`data/warehouse.duckdb`)：行情、估值、因子、交易日和回测数据。
- SQLite (`data/decision_journal.sqlite3`)：决策信号、状态转换和 outcome。

## 模块接口

`MarketWarehouse` 隐藏 DuckDB 的表名、复权价格口径和交易日计算。决策代码只通过它读取锚定价、前向价格和交易日进度。

开盘竞价和尾盘信号优先保存筛选时的实时价格与日期作为锚点，避免用上一交易日收盘价计算已经发生的选择日涨跌。

`DecisionJournal` 隐藏 SQLite schema、WAL、幂等写入、状态转换、结算和复盘汇总。调用方不直接拼接决策日志 SQL。

## 状态约定

- `active`：尚未到达评估周期。
- `pending_data`：已经到期，但行情暂未补齐；后续任务继续重试。
- `closed`：已写入 outcome；重复产生同一信号不得重新打开。
- `unable`：缺少有效参考价格等永久不可评估情况。

## 兼容迁移

首次打开 `DecisionJournal` 时会从旧 DuckDB `decision_signal` / `decision_outcome` 表导入已有数据。迁移只补缺失记录，不覆盖 SQLite 中更新的状态或结果，因此可以安全重复执行。

## 命令

```bash
ashare signals-outcome
ashare signals-review --window 90
ashare signals-review --window 90 --source recommend
```

两个命令都只读 DuckDB；写入发生在独立 SQLite 文件中，因此可以与只读看板同时运行。
