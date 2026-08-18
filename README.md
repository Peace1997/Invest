# Trading / A 股决策辅助工具

这是一个本地运行的个人 A 股决策辅助系统。当前实现已经覆盖行情与基本面入库、持仓估值、可解释打分、风险与再平衡、策略回测、新闻舆情、短线观察、决策复盘和 Streamlit 看板。

> 代码现状、核心调用链和模块职责见 [docs/CODEBASE_GUIDE.md](docs/CODEBASE_GUIDE.md)。后续接手或修改功能，建议先读这份文档，而不是从整个 `src/` 重新开始。

## 快速开始

要求 Python 3.12+ 和 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync
uv run ashare init
uv run ashare ui
```

看板默认打开 `http://localhost:8501`。也可以直接运行：

```bash
uv run streamlit run src/ashare/ui/app.py
```

常用操作：

```bash
uv run ashare market-refresh                 # 刷新看板依赖的市场数据
uv run ashare pf                             # 持仓日评、快照和决策留痕
uv run ashare pf --no-write                  # 只读持仓分析
uv run ashare stock 600519                   # 个股速评
uv run ashare stock 600519 --llm             # 加 AI 综合解读
uv run ashare recommend --style value        # 价值风格推荐
uv run ashare backtest --style value         # 对应风格回测
uv run ashare signals-outcome                # 结算到期决策结果
uv run ashare signals-review --window 90     # 查看复盘汇总
uv run ashare --help                         # 完整命令列表
```

## 本地文件与密钥

- `config.yaml`：路径、刷新范围、策略周期、预警阈值和 LLM 配置。
- `positions.yaml`：真实持仓与现金，仅保存在本机，已被 Git 忽略。
- `data/warehouse.duckdb`：市场数据仓库，已被 Git 忽略。
- `data/decision_journal.sqlite3`：决策信号与结果，已被 Git 忽略。
- `.tushare_token` 或 `TUSHARE_TOKEN`：Tushare 凭证。
- `.anthropic_key` 或 `LLM_API_KEY` / `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`：OpenAI 兼容 LLM 端点的凭证；文件名是历史遗留。
- `.serverchan_key` 或 `SERVERCHAN_KEY`：Server 酱推送凭证。

这些文件都不应提交。首次完整建库可用 `backfill-meta`、`backfill-all` 等命令；全量历史任务可能耗时很长，具体用途见代码地图。

## 文档索引

- [当前实现与代码地图](docs/CODEBASE_GUIDE.md)：以当前代码为准的入口文档。
- [低估值成长观察规则](docs/STRATEGY_VALUE_GROWTH.md)：早盘/尾盘筛选逻辑、价格口径和历史验证。
- [决策信号存储](docs/DECISION_STORAGE.md)：DuckDB / SQLite 分工、信号状态和复盘口径。
- [架构路线图](docs/ROADMAP.md)：早期设计原则与目标架构，不代表所有目录仍按原计划实现。

## 验证

```bash
uv run pytest -q
```

截至 2026-08-18，测试收集存在一个已知问题：三个决策相关测试通过 `tests.decision_fixtures` 导入夹具，但 `tests` 目录不是可导入包，导致 `ModuleNotFoundError: tests`。这是当前仓库状态，不是安装失败；详见代码地图的“已知边界”。
