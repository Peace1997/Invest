# A股量化决策辅助工具 — 架构设计与分阶段实现路线

> 目标：构建一个**长期可演进、可解释、决策导向**的个人 A 股投资辅助系统。  
> 现状：Phase 0 数据底座已完成（DuckDB + AKShare + 幂等 upsert）。  
> 本文档：从主流开源项目提炼可借鉴模式 → 设计可扩展架构 → 分阶段交付路线。

---

## 0. 设计原则（先立规矩）

| 原则 | 含义 | 反面教材 |
|---|---|---|
| **决策可解释** | 每个买卖建议必须能回答"为什么、什么时候反转" | 黑盒 ML 给个分数完事 |
| **风控优先** | 仓位/止损/集中度规则跑在收益模型之前 | 先追求收益、风控当事后补丁 |
| **配置驱动** | 因子/信号/策略组合用 YAML 描述，不改代码 | 每加一个策略就 fork 一份 |
| **增量演进** | 每个 Phase 独立可用，前一个未完不阻塞下一个 | 大爆炸式重写 |
| **单人可维护** | 抽象层不超过决策传导链所需，复杂工具按需引入 | 一开始就上 K8s + 微服务 |
| **数据不可变** | 所有 bar 表存未复权原始价，复权用因子表 + 视图 | 直接改数据 |
| **可审计** | 每次决策落盘（输入/规则/输出），供复盘和 LLM 记忆 | 决策只在脑子里 |

---

## 1. 开源生态借鉴（已读源码 + 文档）

### 1.1 Microsoft Qlib — 借鉴架构分层 + 因子表达式

**核心可借鉴：**
- **分层架构**：Data → Feature/Expression → Model → Strategy → Backtest → Workflow，每层抽象类清晰，插件化
- **Expression Engine**：声明式因子定义（`Ref($close, -1)/$close - 1`），自动缓存
- **Alpha158/360**：158/360 个高质量基础因子，是中国市场常用 alpha 的工业级实现
- **Nested Decision Framework**：策略嵌套（日频选股 + 日内执行）
- **RD-Agent**：LLM 自动化因子挖掘/模型调优（2024 集成）
- **RL 模块**：用于订单执行优化（PPO/OPDS）

**不照搬：**
- 数据层用 HDF5/二进制 — 我们用 DuckDB 更适合单人开发
- 完整 Expression Engine 工程量太大 — 我们用 Python 函数 + SQL 模板的轻量版

### 1.2 zvt — 借鉴 schema 哲学 + 三层因子计算

**核心可借鉴：**
- **三问哲学**：What data / How to record / How to query — 每个数据域都用这三问标准化
- **三层因子流水线**：`data_df → factor_df → result_df`（原始 → 计算 → 信号）
- **TradableEntity 抽象**：Stock/ETF/Index/Fund 同接口，可横跨市场
- **TargetSelector 模式**：组合多因子信号生成候选名单
- **多 provider 冗余**：em / joinquant / sina 互为备份（你已实践）

### 1.3 FinRL — 借鉴 MDP 抽象 + Gym 环境（备 RL 接入点）

**核心可借鉴：**
- **MDP 形式化**：State = OHLCV + 指标 + VIX + turbulence；Action = 仓位调整；Reward = 风险调整收益
- **三层架构**：Applications / Agents / Meta — 让 RL 算法可替换（SB3/RLlib/ElegantRL）
- **gym 环境接口**：把回测引擎做成 `env.step()` 接口，RL/规则/LLM 都能调

**不照搬：**
- 现阶段不上 RL，但保留 `BaseAgent` 接口以备后续 Phase 7

### 1.4 TradingAgents — 借鉴多智能体辩论 + 决策记忆

**核心可借鉴：**
- **角色分解**：Fundamentals / Sentiment / News / Technical 四类分析师，独立产出
- **对抗式评审**：Bull vs Bear 研究员辩论，避免单边偏见
- **决策日志**：每个决策带 rationale，事后复盘 → 形成 agent memory
- **LangGraph 编排**：多 agent 状态机
- **多 LLM provider**：OpenAI/Claude/Ollama 抽象

**不照搬：**
- 个人工具用 LangGraph 偏重，先用纯 Python orchestration，agent 多了再换

### 1.5 其他参考

| 项目 | 用法 |
|---|---|
| **vectorbt** | 高性能向量化回测，直接做依赖 |
| **AlphaLens / empyrical** | 因子有效性分析 / 业绩指标，工具库直接调 |
| **akshare** | 你已用，主数据源 |
| **Tushare Pro** | 付费但数据稳定，Phase 2 后可平行接入 |
| **APScheduler** | 进程内调度，A 股频率够用 |

---

## 2. 总体架构

```
┌──────────────────────────────────────────────────────────────────┐
│  L7  Presentation       Dashboard / Daily Report / Alerts / CLI  │
├──────────────────────────────────────────────────────────────────┤
│  L6  Decision           PortfolioManager · RiskEngine · RuleEng  │
│                         (combine signals → actionable decisions) │
├──────────────────────────────────────────────────────────────────┤
│  L5  Signal (pluggable) IndexTiming · StockScorer · LLMAnalyst · │
│                         RLAgent · CustomSignal                   │
│                         统一输出: SignalOutput                    │
├──────────────────────────────────────────────────────────────────┤
│  L4  Feature/Factor     FactorRegistry · Expression · Cache      │
│                         (declarative + imperative)               │
├──────────────────────────────────────────────────────────────────┤
│  L3  Data Warehouse     DuckDB · views · adj_factor join        │
│                         (Phase 0 已建)                            │
├──────────────────────────────────────────────────────────────────┤
│  L2  Ingest             Sources (AKShare/Tushare/Sina) · Jobs    │
│                         · Calendar · Idempotent Upsert          │
├──────────────────────────────────────────────────────────────────┤
│  L1  Cross-cutting      Scheduler · Logging · EventBus · Memory  │
│                         · Backtest · Eval · Config               │
└──────────────────────────────────────────────────────────────────┘
```

### 关键数据流

```
                       (实时/T+0)             (T+1批)
News/Policy ─────────► LLM Sentiment ───┐
Macro Indicators ─────► FactorStore ◄───┤
Market Bars ──────────► FactorStore ◄───┤    (composable)
Northbound/Margin ────► FactorStore ◄───┤
Fundamentals ─────────► FactorStore ◄───┘
                              │
                              ▼
         ┌────────────────────┴───────────────────┐
         ▼                    ▼                   ▼
    IndexTiming         StockScorer          LLMAnalyst
    (大盘冷热)           (个股Top-N)          (新闻/政策影响)
         │                    │                   │
         └────────────────────┼───────────────────┘
                              ▼
                      DecisionEngine
                  (Rule + Risk + Portfolio)
                              │
                              ▼
                   Decision Log (DuckDB)
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
        Daily Report     Alerts (TG/Email)  Dashboard
```

---

## 3. 目标目录结构

在现有 Phase 0 基础上扩展（标 ✦ 为新增，⚙ 为升级）：

```
src/ashare/
├── config.py                       # 配置加载 (已有)
├── cli.py                          # CLI 入口 ⚙ (扩展更多命令)
│
├── sources/                        # L2 数据源（已有）
│   ├── base.py                     # ✦ BaseSource ABC
│   ├── akshare_src.py              # 已有
│   ├── tushare_src.py              # ✦ Phase 2 加入
│   └── registry.py                 # ✦ source 注册中心
│
├── ingest/                         # L2 入库作业（已有）
│   ├── calendar.py / instruments.py / bars.py / northbound.py
│   ├── adj_factor.py               # ✦ Phase 1
│   ├── valuation.py                # ✦ Phase 1 (PE/PB)
│   ├── fundamentals.py             # ✦ Phase 1 (财报)
│   ├── macro.py                    # ✦ Phase 1 (CPI/PMI/M2/LPR)
│   ├── fund_nav.py                 # ✦ Phase 1 (场外联接)
│   ├── margin.py                   # ✦ Phase 1 (两融)
│   ├── unlock.py                   # ✦ Phase 1 (解禁)
│   ├── institutional.py            # ✦ Phase 2 (机构持仓)
│   └── news.py                     # ✦ Phase 4 (新闻)
│
├── storage/                        # L3 数仓 (已有)
│   ├── db.py / schema.sql
│   ├── views.sql                   # ✦ 前复权视图 / 估值分位视图
│   └── migrations/                 # ✦ schema 版本管理
│
├── factors/                        # L4 因子层 ✦ Phase 2
│   ├── base.py                     # BaseFactor ABC + FactorOutput
│   ├── registry.py                 # @register_factor 装饰器
│   ├── cache.py                    # 因子缓存 (DuckDB factor_daily 表)
│   ├── technical/                  # 动量/反转/波动率/量价
│   ├── value/                      # PE分位/PB分位/股息率
│   ├── quality/                    # ROE/毛利率/现金流
│   ├── sentiment/                  # 北向/两融/资金流
│   └── macro/                      # 利率/通胀/汇率作为全市场因子
│
├── signals/                        # L5 信号层 ✦ Phase 3
│   ├── base.py                     # BaseSignal ABC + SignalOutput
│   ├── registry.py
│   ├── index_timing.py             # 指数温度计 (估值+趋势+ERP)
│   ├── stock_scorer.py             # 多因子打分
│   ├── event_signal.py             # 业绩预告/解禁等事件信号
│   ├── llm_analyst.py              # ✦ Phase 6
│   └── rl_agent.py                 # ✦ Phase 7 (预留接口)
│
├── decision/                       # L6 决策层 ✦ Phase 4
│   ├── portfolio.py                # PortfolioManager (当前持仓 / 目标权重)
│   ├── risk.py                     # RiskEngine (止损/集中度/相关性)
│   ├── rules.py                    # RuleEngine (买卖触发规则 DSL)
│   └── log.py                      # DecisionLog (落库)
│
├── backtest/                       # ✦ Phase 5
│   ├── engine.py                   # 包装 vectorbt
│   ├── eval.py                     # 指标 (Sharpe/MaxDD/Calmar)
│   └── reports.py                  # 报告生成
│
├── agents/                         # ✦ Phase 6 LLM 多智能体
│   ├── base.py                     # BaseAgent ABC
│   ├── fundamentals.py             # 基本面分析师
│   ├── news.py                     # 新闻/政策分析师
│   ├── technical.py                # 技术面分析师
│   ├── bull_bear.py                # 多空辩论
│   ├── memory.py                   # 决策记忆 (SQLite + embedding)
│   └── orchestrator.py             # 多 agent 编排 (后续可换 LangGraph)
│
├── jobs/                           # 调度作业 (已有)
│   ├── daily_update.py / backfill.py
│   ├── factor_compute.py           # ✦ Phase 2
│   ├── signal_compute.py           # ✦ Phase 3
│   ├── decision_run.py             # ✦ Phase 4
│   └── scheduler.py                # ✦ Phase 1 (APScheduler)
│
├── notify/                         # ✦ Phase 8
│   ├── email.py / telegram.py / wechat.py
│   └── report.py                   # Markdown 日报模板
│
├── ui/                             # ✦ Phase 8
│   └── dash_app.py                 # Plotly Dash 看板
│
└── core/                           # ✦ 横切关注点
    ├── events.py                   # EventBus (decoupling)
    ├── types.py                    # 通用 dataclass
    └── time_utils.py               # 交易日工具
```

---

## 4. 核心接口（关键抽象）

> 这些是**扩展性的根**。所有未来扩展（新数据源、新因子、新策略、LLM、RL）都通过实现这几个接口接入。

### 4.1 BaseSource — 数据源抽象

```python
# src/ashare/sources/base.py
from abc import ABC, abstractmethod
import pandas as pd

class BaseSource(ABC):
    name: str  # 注册名，如 "akshare" / "tushare"

    @abstractmethod
    def get_daily_bars(self, symbol: str, start: str, end: str) -> pd.DataFrame: ...
    @abstractmethod
    def get_instruments(self) -> pd.DataFrame: ...
    # ... 其他数据接口
    def supports(self, capability: str) -> bool: return False

# 注册中心
class SourceRegistry:
    _sources: dict[str, BaseSource] = {}
    @classmethod
    def register(cls, src: BaseSource): cls._sources[src.name] = src
    @classmethod
    def get(cls, name: str) -> BaseSource: return cls._sources[name]
```

### 4.2 BaseFactor — 因子抽象（声明式 + 命令式混合）

```python
# src/ashare/factors/base.py
from dataclasses import dataclass

@dataclass
class FactorOutput:
    symbol: str
    date: str
    name: str
    value: float
    metadata: dict | None = None  # e.g. {"window": 20, "version": "1.0"}

class BaseFactor(ABC):
    name: str
    version: str = "1.0"
    dependencies: list[str] = []   # 依赖的其他因子或表
    @abstractmethod
    def compute(self, ctx: "FactorContext") -> pd.DataFrame: ...

# 装饰器注册
def register_factor(name: str, **kwargs):
    def deco(cls):
        FactorRegistry.add(name, cls(**kwargs))
        return cls
    return deco
```

**简单因子可以直接用 SQL 模板**（参考 Qlib 的 Expression 简化版）：

```python
@register_factor("momentum_20d")
class Momentum20d(SQLFactor):
    sql = """
    SELECT symbol, trade_date,
           close / LAG(close, 20) OVER (PARTITION BY symbol ORDER BY trade_date) - 1 AS value
    FROM v_daily_bar_adj
    """
```

### 4.3 BaseSignal — 信号抽象（**关键扩展点**）

```python
# src/ashare/signals/base.py
@dataclass
class SignalOutput:
    target: str          # symbol 或 'INDEX:000300' 或 'MARKET'
    date: str
    signal_name: str
    score: float         # -1 ~ +1 或 0 ~ 1
    confidence: float    # 0 ~ 1
    direction: str       # 'long' | 'short' | 'flat'
    horizon: str         # 'short' (T+5) | 'mid' (T+20) | 'long' (T+60)
    reason: str          # 必填！决策可解释性
    metadata: dict       # 输入因子值、参数等

class BaseSignal(ABC):
    name: str
    @abstractmethod
    def evaluate(self, date: str, ctx: "SignalContext") -> list[SignalOutput]: ...
```

**这个统一接口是 LLM/RL/规则信号能融合的关键**：
- `IndexTiming.evaluate()` → 大盘信号
- `StockScorer.evaluate()` → 多只股票信号
- `LLMAnalyst.evaluate()` → LLM 给出的方向 + 理由
- `RLAgent.evaluate()` → RL policy 推断结果

### 4.4 BaseStrategy / DecisionEngine — 决策层

```python
@dataclass
class Decision:
    date: str
    action: str          # 'buy' | 'sell' | 'hold' | 'rebalance'
    symbol: str
    target_weight: float
    rationale: str       # 触发哪些信号、为什么
    risk_check: dict     # 通过了哪些风控检查
    signals_used: list[SignalOutput]

class DecisionEngine:
    def __init__(self, signals: list[BaseSignal], risk: RiskEngine, rules: RuleEngine): ...
    def run(self, date: str, portfolio: Portfolio) -> list[Decision]: ...
```

### 4.5 BaseAgent — 给 LLM/RL 的统一接口

```python
class BaseAgent(ABC):
    """LLM 分析师 / RL agent 都实现这个接口，最终接入 Signal 层"""
    name: str
    @abstractmethod
    def analyze(self, ctx: "AgentContext") -> SignalOutput: ...
    def memory_recall(self, query: str) -> list[dict]: return []
```

---

## 5. 配置系统（YAML 驱动）

**目标：换策略不改代码。**

```yaml
# config/strategies/conservative_etf.yaml
strategy:
  name: conservative_etf
  universe:
    - INDEX:000300
    - ETF:510300
    - ETF:510500

  signals:
    - name: index_timing
      params: {valuation_window: 5y, ma_periods: [60, 200]}
      weight: 0.4
    - name: stock_scorer
      params: {factors: [momentum_60d, pe_percentile, roe_ttm], weights: [0.3, 0.4, 0.3]}
      weight: 0.4
    - name: llm_news_analyst   # Phase 6
      enabled: false
      weight: 0.2

  risk:
    max_position: 0.25
    max_sector: 0.40
    stop_loss: -0.12
    max_drawdown: -0.20

  rules:
    - when: "index_timing.score < -0.5 AND trend == 'down'"
      action: reduce_to: 0.3
    - when: "stock_scorer.score > 0.7 AND position == 0"
      action: open_position: 0.1
```

---

## 6. 数据 Schema 扩展（增量演进）

Phase 0 已有：`calendar / instruments / daily_bar / index_bar / northbound`

后续阶段新增：

| Phase | 表 | 用途 |
|---|---|---|
| 1 | `adj_factor` | 复权因子 |
| 1 | `valuation_daily` | PE/PB/股息率 |
| 1 | `fundamentals_q` | 季频财报 |
| 1 | `macro_indicator` | CPI/PPI/PMI/M2/LPR/USDCNY |
| 1 | `fund_nav` | 场外基金净值 |
| 1 | `margin_daily` | 两融余额 |
| 1 | `share_unlock` | 限售解禁 |
| 2 | `institutional_holding` | 公募季报持仓 |
| 2 | `factor_daily` | 因子值统一表 (long format) |
| 3 | `signal_log` | 信号历史 |
| 4 | `decision_log` | 决策历史（关键审计表） |
| 4 | `portfolio_state` | 持仓快照 |
| 6 | `news_event` | 新闻/公告 |
| 6 | `agent_memory` | LLM 决策记忆 |

视图：
- `v_daily_bar_adj` — 前复权拼接 `daily_bar + adj_factor`
- `v_valuation_percentile` — 估值分位预计算

---

## 7. 分阶段实现路线

> 每个 Phase 独立可用、独立可暂停、独立可衡量价值。

---

### Phase 1 · 数据底座补完 + 调度（2-3 周）

**目标**：把 Phase 0 的"演示数仓"变成"真数仓"，定时自动更新。

| 任务 | 交付物 | 优先级 |
|---|---|---|
| 1.1 复权因子表 + 前复权视图 | `adj_factor` 表 / `v_daily_bar_adj` 视图 | ⭐⭐⭐ |
| 1.2 场外基金 NAV 通道 | `fund_nav` 表 + ingest job | ⭐⭐⭐ |
| 1.3 估值数据 | `valuation_daily` (个股 PE/PB) + 指数估值 | ⭐⭐⭐ |
| 1.4 季频财报 | `fundamentals_q` (营收/净利/ROE) | ⭐⭐ |
| 1.5 宏观指标 | `macro_indicator` (CPI/PPI/PMI/M2/LPR/USDCNY/10Y国债) | ⭐⭐ |
| 1.6 两融 + 解禁 | `margin_daily` + `share_unlock` | ⭐⭐ |
| 1.7 全市场每日回填 | 后台跑 `backfill-all`，建立全市场历史 | ⭐⭐⭐ |
| 1.8 APScheduler | `jobs/scheduler.py` 交易日 15:30 自动 `daily` | ⭐⭐⭐ |
| 1.9 BaseSource 抽象 + Tushare 备份 | `sources/base.py` + `sources/registry.py` | ⭐ |

**完成后能用：** `query` 命令能看到全市场任意股票/ETF 的复权 K 线、估值、财报、宏观环境。

---

### Phase 2 · 因子层（2 周）

**目标**：建立可复用的因子体系，所有信号都从这里取数。

| 任务 | 交付物 |
|---|---|
| 2.1 `factors/base.py` + `registry.py` + `cache.py` | 抽象与注册中心 |
| 2.2 `factor_daily` 表 + 增量计算 | 统一存储，避免重复计算 |
| 2.3 技术因子 ×10 | momentum/reversal/volatility/turnover/量比/RSI/MACD 等 |
| 2.4 估值因子 ×5 | PE 分位/PB 分位/股息率/PEG/PB-ROE |
| 2.5 质量因子 ×5 | ROE-TTM/毛利率/经营现金流/资产负债率/营收增速 |
| 2.6 资金面因子 ×5 | 北向持股变动/两融余额变动/换手率/大单净流入 |
| 2.7 AlphaLens 集成 | `notebooks/factor_eval.ipynb` 评估每个因子 IC/IR |

**完成后能用：** 任意一行代码取出任意股票任意日期的任意因子值。每个因子有 IC 报告，知道哪些有效。

---

### Phase 3 · 信号层 MVP ⭐ **最先有用** （2 周）

**目标**：每天告诉你"大盘冷热"和"个股推荐 Top N"。

#### 3.1 指数温度计 (`signals/index_timing.py`)

**输出维度：**
- 估值百分位（PE/PB 在过去 5/10 年的位置）
- 趋势状态（价格 vs MA60/MA200）
- 风险溢价（1/PE - 10Y 国债收益率）
- 资金面（北向 30 日累计 + 两融变动）

**输出 SignalOutput：**
```python
SignalOutput(
    target="INDEX:000300",
    score=-0.35,                        # 偏空
    direction="long" if score>0 else "flat",
    horizon="long",
    reason="PE分位62%(中性偏高), 200日均线下方, 北向近30日净流出"
)
```

#### 3.2 个股打分器 (`signals/stock_scorer.py`)

多因子加权打分，每日输出全市场 Top 50 + 你持仓股的健康度分数。

#### 3.3 事件信号 (`signals/event_signal.py`)

业绩预告、解禁、ST 摘帽等离散事件触发的信号。

**完成后能用：** `uv run python -m ashare.cli today` 输出当日大盘温度 + Top N + 你持仓预警。**这一步已经是可用的决策辅助工具了。**

---

### Phase 4 · 决策与风控层（2 周）

**目标**：从"信号"到"建议动作"，含风控、仓位、规则引擎、决策落盘。

| 模块 | 职责 |
|---|---|
| `decision/portfolio.py` | 持仓状态、当前权重、目标权重、再平衡需求 |
| `decision/risk.py` | 止损 / 单一持仓上限 / 行业集中度 / 相关性矩阵 / 最大回撤监控 |
| `decision/rules.py` | YAML 规则引擎，把 SignalOutput 转 Decision |
| `decision/log.py` | 每次决策落 `decision_log` 表（含输入信号、规则触发、风控结果） |

**关键设计：** 决策必须可解释 —— 每条 Decision 携带：
- 触发的信号列表
- 通过的风控检查
- 用人话写的 `rationale`

**完成后能用：** "如果你信任规则，按这个动作做"。即便你不全信，**决策日志可以作为你做投资决策时的参照清单**。

---

### Phase 5 · 回测引擎（1-2 周）

**目标**：把同一套 Signal+Decision 在历史数据上跑，得到 Sharpe/MaxDD/Calmar。

- 包装 **vectorbt** 做向量化回测（不自己造轮子）
- `backtest/engine.py`：吃同一个 strategy YAML 配置，输出业绩
- `backtest/eval.py`：调 `empyrical` 计算指标
- `backtest/reports.py`：HTML + 图表

**完成后能用：** 改任何信号/权重/规则，都能立刻看历史表现，避免"凭感觉"。

---

### Phase 6 · LLM 多智能体分析师（2-3 周）⭐ 新能力

**目标**：让模型读新闻/公告/政策，给出**带理由**的方向判断，融入 Signal 层。

#### 6.1 数据准备
- `ingest/news.py`：东财/新浪新闻 / 公司公告 / 央行/证监会政策
- `news_event` 表：每条新闻含 (date, source, title, content, tags, embedding)

#### 6.2 Agent 实现（参考 TradingAgents）

| Agent | 输入 | 输出 |
|---|---|---|
| `agents/fundamentals.py` | 财报 + 业绩预告 | 基本面方向 |
| `agents/news.py` | 近 7 日新闻 | 事件情绪 |
| `agents/technical.py` | 量价 + 技术因子 | 技术方向 |
| `agents/macro.py` | 宏观指标变化 | 大势判断 |

#### 6.3 多空辩论 (`agents/bull_bear.py`)
Bull researcher 找看多理由，Bear researcher 找看空理由，第三个 Judge agent 综合。

#### 6.4 决策记忆 (`agents/memory.py`)
- SQLite + sentence-transformers embedding
- 每次决策存：context、reasoning、当时输入信号、事后回报（T+5/T+20 验证）
- Agent 决策前先 recall 类似历史情况

#### 6.5 编排
- 起步用纯 Python orchestrator（不上 LangGraph）
- 接口：`LLMAnalyst(BaseSignal)` 把 agent 输出包成 SignalOutput
- LLM provider 抽象：Claude/GPT/本地 Ollama 可换

**完成后能用：** 每日报告里多出"AI 分析师视角"段落，把零散新闻消化成 1-2 句结论 + 信号分数，**作为传统量化信号的正交补充**。

---

### Phase 7 · RL 执行/配置优化（可选，3-4 周）

**目标**：用 RL 做两件事，**不强求**：
1. **订单执行优化**：把日级目标仓位拆成日内冲击最小的下单序列（参考 Qlib RL）
2. **动态权重分配**：信号组合的权重让 RL 学（参考 FinRL）

**实现：**
- `agents/rl_agent.py` 实现 `BaseAgent`，包装 SB3 PPO
- Gym 环境用回测引擎 wrap，state = 因子向量，action = 仓位调整，reward = 风险调整收益
- 训练在 Phase 5 回测数据上

**先决条件：** Phase 5 回测引擎稳定 + 因子足够丰富。**不达到这两点就不要开 Phase 7**（RL 没数据/没环境就是空中楼阁）。

---

### Phase 8 · UI / 告警 / 日报（1-2 周）

| 模块 | 实现 |
|---|---|
| **日报** | `notify/report.py` 生成 Markdown，含温度计、Top N、持仓健康度、关键事件、AI 视角 |
| **告警** | 持仓异动（跌停/重大新闻）、规则触发 → Telegram bot 或邮件 |
| **看板** | Plotly Dash 单页应用：温度计、因子热力图、决策日志查询 |
| **盘中** | 5 分钟轮询 `stock_zh_a_spot_em`，异动触发告警 |

---

## 8. 技术选型一览

| 层 | 选型 | 理由 |
|---|---|---|
| 数据存储 | DuckDB + Parquet | 单文件、嵌入式、SQL 强、零运维 |
| 数据源 | AKShare 主 + Tushare 备 | 已实践，备份增稳定 |
| 数据处理 | Polars + DuckDB | 比 pandas 快 10x，列存友好 |
| 因子库基础 | 自建 + 借鉴 Qlib alpha158 | 完全工程化太重，挑高价值的实现 |
| 回测 | **vectorbt** | 向量化最快，不自己造 |
| 因子评估 | AlphaLens / empyrical | 标准工具 |
| 调度 | APScheduler | 进程内、零依赖 |
| LLM SDK | Anthropic SDK 直连 | 简单，按需引入 LangGraph |
| Embedding | sentence-transformers (本地) | 隐私 + 免费 |
| Memory | SQLite + 向量列 (sqlite-vss) | 单文件、零运维 |
| RL | Stable Baselines 3 (Phase 7) | 社区主流 |
| UI | Plotly Dash | Python 单语言、图表强 |
| 告警 | python-telegram-bot | 个人最方便 |
| 配置 | YAML + Pydantic | 类型校验 |

---

## 9. 决策输出样例（最终形态）

```
═══════════ A股每日辅助报告 · 2026-05-29 ═══════════

【大盘温度计】
  沪深300:  PE 12.8x (近5年 58%分位) | 200日均线上方 | ERP 4.2% (中性偏高)
  → 信号: 中性 (score=+0.05, horizon=long)
  → 建议: 维持仓位, 不加不减

【资金面】
  北向近30日: -156亿 (流出) ⚠
  两融余额: 1.62万亿 (+0.8% MoM)

【宏观信号】
  CPI 0.3% YoY ↑ | LPR 维持 3.45% | USDCNY 7.18 ↓
  → 流动性边际宽松, 利好成长

【持仓健康度】
  ✓ 沪深300ETF        weight 25%   score +0.12  → hold
  ⚠ 中证1000ETF       weight 15%   score -0.41  → 触发减仓规则 (estimated -5%)
       原因: 估值分位 88%(高估) + 北向减持 + 60日动量转负

【AI 分析师视角】
  Fundamentals: ↑ 整体盈利预期上修, 上游周期偏弱, 中游制造改善
  News:         央行重提"灵活适度", 利好高股息
  Macro:        美联储 6 月降息概率 78%, 人民币压力缓解

【今日决策】
  1. SELL  中证1000ETF  当前15% → 目标10%   (规则: stock_scorer<-0.4 AND pe_pct>0.8)
  2. HOLD  其余持仓
  3. WATCH 红利低波ETF  关注信号 (score +0.62, 突破200日均线)

【风控状态】
  最大持仓 25% ≤ 25% 上限 ✓
  行业集中度 金融 32% ≤ 40% 上限 ✓
  当前回撤 -8.2% ≤ -20% 止损线 ✓
```

---

## 10. 当前进度对齐 + 下一步动作

**已完成（Phase 0）：**
- ✅ DuckDB 数仓 + 幂等 upsert
- ✅ AKShare 适配器 + 限流重试
- ✅ calendar / instruments / daily_bar / index_bar / northbound 五表
- ✅ CLI 工具链 (init/sample/backfill/daily/query)

**已完成（增量, 2026-05-29）：**
- ✅ 持仓估值闭环：`positions.yaml`(隐私) + 腾讯实时(场内) + 基金NAV(场外T-1) → `pf` 日评
- ✅ **持仓历史快照**：`portfolio_snapshot`/`position_snapshot` 两表 + `snapshots.py`(收益曲线/最大回撤/sparkline)；`pf` 每跑一次幂等存一天
- ✅ **写回 positions.yaml**：每行 `# ➤ 现价|市值|浮盈|今|权重` 幂等注释, 用户只维护 shares/cost
- ✅ **Phase 2 因子层(轻量)**：`factors/technical.py` 纯函数(MA/动量/RSI/距高/波动)在 close 或 NAV 序列上统一计算
- ✅ **Phase 3.1 指数温度计**：`signals/index_timing.py`(趋势版, 估值分位待 Phase1.3)
- ✅ **Phase 3.2 持仓健康度**：`signals/health.py` 每只持仓 SignalOutput(score∈[-1,1]+reason)，股票走 daily_bar、基金走 NAV 历史
- ✅ **Phase 4 决策引擎(规则版)**：`decision/rules.py` 融合 健康分+浮盈+权重 → 每只持仓一条可解释动作(加仓/持有/谨慎/止损评估/减仓/止盈)，风控优先
- ✅ **Phase 1.3 估值分位**：`valuation_daily` 表 + `ingest/valuation.py`(个股百度PE/PB、指数乐咕滚动PE、宽基基金按跟踪指数) + `factors/valuation.py`(PE/PB 近5年分位,负PE诚实不打分) → 健康分/温度计新增"估值·PE分位"因子(±0.12)；`cli valuation` 回填、`cli pf` 自动 top-up
- ✅ **Phase 3.2 个股/板块推荐**：`signals/stock_scorer.recommend_stocks`(沪深300主板成分为universe, 两段式:趋势预筛→估值精排, 同一可审计rubric) + `signals/sector_rank.recommend_sectors`(东财行业板块趋势打分, 源断连时诚实留空)。`cli recommend [--top N]` 出≥5主板个股(带逐因子拆解)+板块。沪深300主板成分 bars 已回填(data/csi300_main.json)
- ✅ **并发修复**：看板 DuckDB 改 read_only(`open_db(read_only=)`); CLI 写命令遇锁给友好提示(`ui`占锁时); 只读命令(ui/query/today/live)可与看板并存
- ⏳ 待补：Phase1.1 复权因子、Phase1.8 调度、Phase2 因子IC评估、Phase5 回测、Phase6 LLM 分析师

**下一步推荐起手姿势：**
1. **Phase 1.1** 复权因子 — 没它后面全错
2. **Phase 1.8** APScheduler — 让数据自己更新
3. **Phase 1.7** backfill-all 后台跑 — 你睡觉时把数据攒齐
4. **直接跳 Phase 3.1** 指数温度计 MVP（用 Phase 0 数据 + Phase 1.1 复权先做大盘版）— **这是最快产出"真正可用工具"的路径**

**不推荐：**
- ❌ 一开始就上 LLM/RL — 没数据没因子没风控，agent 是空中楼阁
- ❌ 一开始就追求全市场 Top N — 先把"沪深300 当前冷热"这一个问题答好
- ❌ 一开始就建 UI — Markdown 日报 + CLI 已经够你日常用

---

## 11. 长期演进方向（写给一年后的你）

- **多市场扩展**：架构已抽象到 TradableEntity，未来加港股/美股只需新 Source + 复权规则适配
- **因子市场化**：所有因子带 version + IC 评估，淘汰失效因子是常态
- **Agent 自主化**：参考 Qlib RD-Agent，让 LLM 自动提出新因子假设并回测验证（高阶能力）
- **联邦数据**：和朋友 / 圈子共享因子库（zvt 模式）
- **持续学习**：每次决策的事后回报喂回 agent memory，形成长期 alpha

---

*本文档随实现进度更新。每完成一个 Phase，对应章节标注 ✅ 并补充实际遇到的坑。*
