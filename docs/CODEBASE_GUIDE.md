# 当前实现与代码地图

> 更新时间：2026-08-18。本文描述“代码现在如何工作”，不是目标架构。规划和历史设计仍见 [ROADMAP.md](ROADMAP.md)。

## 1. 一句话定位

本项目是一个 Python 3.12 的本地单体应用：从多个数据源采集 A 股行情、估值、财务、资金与新闻，写入 DuckDB；再通过因子、可解释打分、持仓规则和回测产生分析结果；最后由 CLI、Streamlit 看板和 Server 酱呈现。决策信号及其 T+N 结果单独写入 SQLite，形成可复盘闭环。

当前主入口：

- CLI：`src/ashare/cli.py`，安装后命令名为 `ashare`。
- Web：`src/ashare/ui/app.py`，单文件 Streamlit 应用。
- 配置：`config.yaml`。
- 主数仓 schema：`src/ashare/storage/schema.sql`。
- 真实持仓：`positions.yaml`，本地隐私文件，不进 Git。

## 2. 运行时架构

```text
AkShare / Tushare / 腾讯 / 同花顺 / 财联社
                    │
                    ▼
sources/* → ingest/* → jobs/* → DuckDB warehouse.duckdb
                                      │
                 ┌────────────────────┼────────────────────┐
                 ▼                    ▼                    ▼
            factors/*            signals/*        repositories/
                 │                    │                    │
                 └──────────────┬─────┘                    │
                                ▼                          │
                  decision/* + portfolio/risk/rebalance    │
                                │                          │
                 ┌──────────────┼──────────────┐           │
                 ▼              ▼              ▼           │
              CLI          Streamlit UI    notify/*        │
                 │                                         │
                 └── decision_signals.py ──────────────────┘
                                │
                                ▼
                    SQLite decision_journal.sqlite3
```

边界要点：

- `sources/` 负责请求和字段标准化；`ingest/` 负责数据集级转换与幂等写入。
- `jobs/` 编排多个 ingest，不承载打分规则。
- `factors/` 只计算指标；`signals/` 把指标转成带原因的统一 `SignalOutput`。
- `decision/` 把信号、盈亏和仓位约束变成动作建议。
- UI 尽量复用上述模块，但 `ui/app.py` 也包含不少资讯分类和展示辅助逻辑，是目前最大、最耦合的文件。
- `repositories/` 目前只为决策评估提供稳定的市场查询接口和 SQLite 日志接口，并未覆盖整个 DuckDB。

## 3. 核心数据流

### 3.1 市场刷新

入口为 `ashare market-refresh`，调用链如下：

```text
cli.cmd_market_refresh
  → jobs.market_refresh.refresh_market_data
    → ingest_calendar
    → jobs.daily_update.daily_update
      → Tushare 批量日线/复权因子（首选）
      → AkShare 逐标的日线（未覆盖时回退）
      → Tushare 估值和历史财务增量
    → 财联社市场宽度快照
    → 持仓场外基金净值
    → 持仓与指数估值
    → 检查整体行情与每个场内持仓是否追到应有交易日
```

`daily_update` 默认只更新库内已有股票/ETF；`ashare daily --universe all` 才刷新完整 instruments universe。交易日收盘前不会把当日临时日 K 写入 settled 日线。任务即使写入了一些旧行，也必须通过新鲜度检查才算成功；失败时 CLI 以退出码 2 结束，供 cron/launchd 发现。

`scripts/macos-market-refresh.sh` 会先停止本地 Streamlit 释放 DuckDB 写锁，刷新后再启动。`launchd/` 中的 plist 在工作日 16:00 和 20:15 调度刷新。

### 3.2 持仓分析

入口为 `ashare pf` 或看板“我的仓位”：

```text
positions.yaml
  → portfolio.load_positions
  → portfolio.value_portfolio
      场内资产：腾讯实时行情
      场外基金：DuckDB fund_nav，缺失时回退 AkShare
  → signals.health.score_portfolio
      日线/NAV → technical + valuation → 可解释健康分
  → signals.index_timing
  → decision.rules.generate_advice
      风控优先级：集中度 → 止损 → 止盈 → 趋势
  → risk.portfolio_risk + rebalance.rebalance_plan
  → 文本报告 / 当日快照 / positions.yaml 注释 / 决策留痕
```

`pf` 默认会更新净值、估值、组合快照并写回持仓注释；`--no-write` 会跳过这些写入。持仓类别是按资产类型和名称关键词粗分，属于展示与集中度辅助，不是正式证券分类。

### 3.3 推荐、因子与回测

统一信号对象在 `signals/base.py`：

- `SignalOutput`：目标、名称、`[-1, +1]` 分数、方向、周期、理由、置信度和元数据。
- `ScoreComponent`：单项贡献、真实数值解释和贡献上限。

主要因子：

- `factors/technical.py`：MA20/60/120/200、20/60/120 日收益、RSI14、年内位置、回撤、60 日年化波动。
- `factors/valuation.py`：从 canonical 估值视图计算 PE/PB 历史分位；PE 非正时不伪造分位。
- `factors/quality.py`：读取最近一期 ROE、利润率、负债率、营收/利润增速。
- `factors/nav.py`：场外基金净值序列。

个股推荐在 `signals/stock_scorer.py`，支持：

- `momentum`：趋势/动量为主，估值精排；代码和 UI 明确标注历史回测为负超额。
- `value`：PE/PB 分位为主，质量指标主要用于展示和排雷，不奖励动量。
- `leader`：先选热门行业，再选行业内候选。
- `reversal`：买近期超跌；较长窗口曾有正 edge，但近一年衰减，明确标为不稳定。

`backtest.py` 对相应风格做横截面回测，关注胜率、相对沪深 300 超额、分层单调和 IC。`backtest_shortterm.py` 验证竞价/尾盘规则，并可用 `minute_bar` 做真实 14:30 入场的尾盘验证。`backtest_reversal.py` 是反转因子研究函数，目前没有独立 CLI 命令。

### 3.4 新闻、舆情与短线观察

```text
AkShare 财经快讯/新闻联播 → news_raw
                          → 本地关键词分类（主要在 ui/app.py）
                          → OpenAI 兼容 LLM → sentiment_daily
                          → 看板 / 日报 / 竞价与尾盘提示
```

LLM 客户端在 `llm.py`，实际使用的是 OpenAI 兼容 Chat Completions 接口。`config.yaml` 决定 `base_url` 和模型；`.anthropic_key` 只是沿用的旧文件名。无密钥、无新闻、解析失败或调用失败时，舆情流程跳过，不生成伪结果。

`premarket.py` 与 `endday.py` 共用 `value_growth.py`：最新已公告财务必须保持利润和营收正增长，再按低 PE/PB 排序；Top 5 优先为科技行业保留 1 个候选。最终候选才通过腾讯接口抓取 5 分钟内带交易所时间戳的实时价，然后由 Server 酱推送。LLM 不参与选股，也不生成价格。规则与回测见 [低估值成长观察规则](STRATEGY_VALUE_GROWTH.md)。

### 3.5 决策留痕与 outcome

`decision_signals.py` 把以下来源统一转成决策信号：

- `recommend`：推荐股票和基金。
- `pf`：持仓建议。
- `premarket` / `endday`：早盘与尾盘的低估值成长观察结果。

信号以 `trade_date + source + style + symbol + horizon` 生成稳定 ID。参考价优先使用筛选当时传入的价格，否则由 `MarketWarehouse` 取不晚于信号日的收盘/NAV。

`ashare signals-outcome` 按配置的 T+1/T+5/T+20 观察周期和信号最终周期结算：进攻型信号看相对沪深 300 超额，减仓/止损等防守型信号看标的绝对跌幅。中间观察只写 outcome，达到最终周期才关闭信号。详细状态和迁移约定见 [DECISION_STORAGE.md](DECISION_STORAGE.md)。

## 4. 存储设计

### DuckDB：市场与分析数据

路径默认是 `data/warehouse.duckdb`。所有 ingest 通过 `storage.db.upsert` 使用原生 `ON CONFLICT` 幂等更新；`init_schema` 每次执行完整 `schema.sql`，目前没有独立 migration 目录。

主要表按用途分组：

| 用途 | 表 / 视图 |
|---|---|
| 元数据与行情 | `calendar`, `instruments`, `daily_bar`, `index_bar`, `minute_bar` |
| 复权读取 | `adj_factor`, `daily_bar_adj`, `minute_bar_adj` |
| 估值与财务 | `valuation_daily`, `valuation_daily_canon`, `fundamentals`, `fundamentals_ts`, `fund_nav` |
| 组合历史 | `portfolio_snapshot`, `position_snapshot` |
| 新闻与 AI | `news_raw`, `news_signal`, `news_tag`, `news_entity`, `sentiment_daily`, `llm_run`, `analysis_context_snapshot` |
| 市场状态 | `northbound`, `market_breadth_snapshot`, `global_index_snapshot`, `sector_fund_flow_snapshot`, `stock_change_event` |
| 龙虎榜与资金流 | `lhb_detail`, `lhb_inst`, `lhb_yyb`, `stock_fund_flow`, `market_fund_flow` |

价格口径：

- `daily_bar` / `minute_bar` 保存未复权名义价，用于展示、成交价位和当日涨跌。
- `daily_bar_adj` / `minute_bar_adj` 是后复权视图，用于跨除权的趋势、动量、回撤和回测。
- 缺失复权因子时视图以 1 回退为原始价，因此应结合数据质量判断覆盖。
- `valuation_daily` 允许多来源共存；`valuation_daily_canon` 为每个标的选择覆盖行数最多的单一来源，平局优先级为 Tushare、百度、乐咕，避免分位窗口混源。

### SQLite：操作型决策日志

路径默认是 `data/decision_journal.sqlite3`，启用 WAL。只有 `decision_signal` 和 `decision_outcome` 两张核心表，适合 UI 与后台任务并发读写。`MarketWarehouse` 只读 DuckDB，为 outcome 计算屏蔽股票、ETF、指数和场外基金的表差异。

## 5. 目录职责

| 路径 | 当前职责 |
|---|---|
| `src/ashare/cli.py` | 所有 CLI 参数、命令分发、DuckDB 读写模式选择 |
| `src/ashare/config.py` | 从仓库根目录解析 YAML 与数据路径 |
| `src/ashare/sources/` | AkShare 主适配器、Tushare 客户端、腾讯实时行情、同花顺北向数据、财联社市场宽度 |
| `src/ashare/ingest/` | 每种数据集的拉取、标准化与 upsert |
| `src/ashare/jobs/` | 回填、每日增量、看板一键市场刷新 |
| `src/ashare/storage/` | DuckDB 连接、schema、通用 upsert |
| `src/ashare/repositories/` | 市场查询门面与 SQLite 决策日志 |
| `src/ashare/factors/` | 技术、估值、质量和 NAV 因子读取/计算 |
| `src/ashare/signals/` | 持仓、指数、基金、股票、板块、舆情、短线和资金流信号 |
| `src/ashare/decision/` | 持仓动作规则与买卖价位 |
| `src/ashare/portfolio.py` | 持仓模型、估值、搜索、YAML 读写 |
| `src/ashare/risk.py` | 相关性、集中度、波动、Beta 和压力测试 |
| `src/ashare/rebalance.py` | 风险贡献与等风险参考的调仓建议 |
| `src/ashare/backtest*.py` | 推荐、短线和反转研究回测 |
| `src/ashare/notify/` | 日报、持仓报告、实时报告、预警和 Server 酱推送 |
| `src/ashare/ui/app.py` | Streamlit 工作区、缓存、图表和交互 |
| `scripts/` | cron/launchd 入口、部署和一次性回填脚本 |
| `tests/` | 主要覆盖刷新、新鲜度、持仓输入、决策日志/outcome、单股预测和数据源初始化 |

## 6. CLI 速查

按用途分组比 `--help` 的平铺列表更容易定位：

| 分组 | 命令 | 说明 |
|---|---|---|
| 初始化/回填 | `init`, `sample`, `backfill-meta`, `backfill-all` | 建 schema、样例闭环、元数据和全量历史 |
| 行情刷新 | `daily`, `market-refresh`, `backfill-min`, `min-update`, `valuation`, `value-backfill` | 日线/分钟/估值/财务更新 |
| 查看与报告 | `query`, `today`, `live`, `ui`, `stock` | 仓库概览、日报、实时快照、看板、个股速评 |
| 持仓 | `pf`, `risk`, `rebalance` | 持仓日评、组合风险、再平衡 |
| 推荐/验证 | `recommend`, `backtest`, `backtest-short` | 推荐与策略验证 |
| 决策复盘 | `signals-outcome`, `signals-review` | 结算和汇总历史决策 |
| 新闻/市场 | `sentiment`, `market-breadth`, `lhb`, `fundflow` | 舆情、市场温度、龙虎榜和资金流 |
| 盘中任务 | `alerts`, `premarket`, `endday` | 阈值预警、竞价、尾盘观察与推送 |

需要注意，CLI 中所谓 `read_only` 只表示 DuckDB 连接模式：例如 `signals-outcome` 仍会写 SQLite，`alerts` 会写状态文件并可能推送，`premarket/endday` 也可能写决策日志并向外发送消息。

## 7. Streamlit 工作区

当前 UI 顶层工作区为：

- 市场概览：市场宽度、主要指数、热点和 AI 舆情。
- 我的仓位：持仓总览、编辑、建议、风险和标的详情。
- 东方精工：针对 `002611` 的专用研究工作区，结合量化状态和 `docs/research/` 最新研究底稿。
- 资讯监控：新闻流、本地规则分类、持仓命中和 AI 摘要。
- 资金与龙虎榜：T+1 资金流与龙虎榜交叉验证。
- 决策复盘：信号状态、T+1/T+5/T+20 表现和历史重算。
- 数据状态：数据质量、资产清单和 LLM 配置。

UI 使用一个缓存的读写 DuckDB 连接，因为资讯自动刷新会写库。外部写任务可能与看板争锁；本地自动刷新脚本通过停看板再刷新来规避。

## 8. 修改功能时从哪里开始

| 需求 | 建议入口 | 通常还需检查 |
|---|---|---|
| 新增数据集 | `sources/` + `ingest/` | `schema.sql`, `jobs/`, 数据状态页 |
| 修改日更逻辑 | `jobs/daily_update.py` | `jobs/market_refresh.py`, `tests/test_daily_update.py`, launchd/cron |
| 修改打分权重 | `signals/scoring.py` | 推荐器、持仓健康、个股速评、对应回测 |
| 新增推荐风格 | `signals/stock_scorer.py` | CLI 参数、UI 单选、`backtest.py`, 决策周期配置 |
| 修改持仓建议 | `decision/rules.py` | `portfolio.py`, `signals/health.py`, 报告和 UI |
| 修改 outcome 口径 | `decision_signals.py` | 两个 repository、`DECISION_STORAGE.md`、决策相关测试 |
| 修改看板 | `ui/app.py` | 缓存失效、DuckDB 锁、是否能复用已有领域函数 |
| 修改 LLM 行为 | `llm.py` + 对应调用模块 | 超时、无密钥降级、审计/提示是否诚实 |

修改原则：用于展示的名义价和用于趋势/回测的后复权价不能混用；新增推荐规则必须有 `ScoreComponent` 解释，最好同时增加相应回测或可验证样本；网络源失败不得静默伪造成成功。

## 9. 已知边界与维护风险

这些是阅读当前代码时最容易踩到的点：

1. `ROADMAP.md` 的目标目录已经落后于实现，例如 backtest 仍是顶层模块、UI 是 `ui/app.py`，不能把 roadmap 当现状索引。
2. `ui/app.py` 约 2900 行，同时包含展示、资讯分类、缓存和部分刷新编排，是当前最明显的维护热点；本文只记录，不在本次文档任务中重构。
3. `schema.sql` 采用“每次重放 CREATE/VIEW”的演进方式，没有显式 schema 版本；破坏性变更需要额外迁移方案。
4. 数据源是多源回退体系，但 `DataSource` 抽象只完整覆盖 AkShare 那类历史数据接口；Tushare、腾讯、同花顺并不实现同一个接口。
5. 回测明确存在幸存者偏差、理想成交和数据覆盖限制；代码内结论是概率证据，不应转写成确定性投资建议。
6. 当前 `uv run pytest --collect-only -q` 能列出 30 个测试，但在收集 `test_decision_cli.py`、`test_decision_journal.py`、`test_decision_signals.py` 时因 `tests.decision_fixtures` 无法导入而中止。原因是 `tests/` 没有成为可导入包；本次只记录现状，未改测试结构。
7. 看板持有 DuckDB 读写连接；直接运行其他写命令可能报锁冲突。CLI 已提供提示，本地定时脚本会主动停看板。
8. `positions.yaml`、数据库和密钥都被 Git 忽略；排查“换机器后缺数据/缺持仓”时先检查这些本地资产，而不是只看代码。

## 10. 文档维护约定

出现以下变更时同步更新本文：

- 新增或删除顶层 CLI 命令。
- 改变主数据流、价格口径、数据库拆分或关键表。
- 新增推荐风格、信号来源、决策状态或复盘口径。
- 调整 Streamlit 顶层工作区。
- 修复“已知边界”中已经记录的问题。

小型实现细节不必逐条写入；本文的目标是让下一次定位代码时，能先找到正确入口和边界，再只阅读必要模块。
