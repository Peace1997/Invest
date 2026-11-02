-- Phase 0 schema. All bar tables store UNADJUSTED prices (canonical, stable).
-- 复权因子 will be added in Phase 1 as a separate adj_factor table.

CREATE TABLE IF NOT EXISTS calendar (
    trade_date DATE PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS instruments (
    symbol      VARCHAR PRIMARY KEY,    -- 6-digit code, e.g. '000001', '510300'
    name        VARCHAR,
    type        VARCHAR NOT NULL,       -- 'stock' | 'etf' | 'index'
    exchange    VARCHAR,                -- 'SH' | 'SZ' | 'BJ'
    list_date   DATE,
    delist_date DATE,
    is_st       BOOLEAN DEFAULT FALSE,
    sw_l1       VARCHAR,                -- 申万一级行业
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Unified daily bars for stocks + ETFs. Indices in index_bar.
CREATE TABLE IF NOT EXISTS daily_bar (
    symbol     VARCHAR NOT NULL,
    trade_date DATE    NOT NULL,
    type       VARCHAR NOT NULL,    -- 'stock' | 'etf'
    open       DOUBLE,
    high       DOUBLE,
    low        DOUBLE,
    close      DOUBLE,
    volume     BIGINT,              -- 成交量, 单位: 手 (akshare 东财源)
    amount     DOUBLE,              -- 成交额, 元
    amplitude  DOUBLE,              -- 振幅, %
    pct_chg    DOUBLE,              -- 涨跌幅, %
    change     DOUBLE,              -- 涨跌额
    turnover   DOUBLE,              -- 换手率, %
    PRIMARY KEY (symbol, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_daily_bar_date ON daily_bar(trade_date);

CREATE TABLE IF NOT EXISTS index_bar (
    symbol     VARCHAR NOT NULL,    -- e.g. '000300' 沪深300
    trade_date DATE    NOT NULL,
    open       DOUBLE,
    high       DOUBLE,
    low        DOUBLE,
    close      DOUBLE,
    volume     BIGINT,
    amount     DOUBLE,
    amplitude  DOUBLE,
    pct_chg    DOUBLE,
    change     DOUBLE,
    turnover   DOUBLE,
    PRIMARY KEY (symbol, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_index_bar_date ON index_bar(trade_date);

-- 复权因子 (Tushare adj_factor). 后复权价 = 原始价 × adj_factor.
-- 个股有; ETF/指数留空(视图按 1 处理). 与 daily_bar 同主键, daily_update 的 tushare 路径同步落库.
CREATE TABLE IF NOT EXISTS adj_factor (
    symbol     VARCHAR NOT NULL,
    trade_date DATE    NOT NULL,
    adj_factor DOUBLE,
    PRIMARY KEY (symbol, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_adj_factor_date ON adj_factor(trade_date);

-- 后复权日线视图: 价格列 = 原始 × 复权因子(缺因子的标的/日期 COALESCE→1, 退化为原始价).
-- 动量/均线/RSI/回测/回撤等"跨除权连续性"敏感的计算读此视图;
-- 展示价、买卖位、当日涨跌幅、涨停判定、短线 gap 仍读 daily_bar(名义价).
CREATE OR REPLACE VIEW daily_bar_adj AS
SELECT b.symbol, b.trade_date, b.type,
       b.open  * COALESCE(f.adj_factor, 1) AS open,
       b.high  * COALESCE(f.adj_factor, 1) AS high,
       b.low   * COALESCE(f.adj_factor, 1) AS low,
       b.close * COALESCE(f.adj_factor, 1) AS close,
       b.volume, b.amount, b.amplitude, b.pct_chg, b.change, b.turnover,
       COALESCE(f.adj_factor, 1) AS adj_factor
FROM daily_bar b
LEFT JOIN adj_factor f ON b.symbol = f.symbol AND b.trade_date = f.trade_date;

-- 估值时间序列 (Phase 1.3): 个股走百度估值, 指数走乐咕(滚动PE)/中证.
-- symbol = 个股代码 或 指数代码(如 '000300'); 场外指数基金按其跟踪指数代码存.
-- 分钟行情 (Tushare stk_mins, 仅个股; ETF/指数不支持). 默认 5min.
-- 口径: volume=手(tushare vol 股 ÷100, 对齐 daily_bar); amount=元(stk_mins 原始即元, 不缩放).
-- trade_time = bar 结束时间. freq 入主键, 同一标的可并存多频率.
CREATE TABLE IF NOT EXISTS minute_bar (
    symbol     VARCHAR   NOT NULL,
    freq       VARCHAR   NOT NULL,   -- '1min'|'5min'|'15min'|'30min'|'60min'
    trade_time TIMESTAMP NOT NULL,   -- bar 结束时间
    type       VARCHAR   NOT NULL,   -- 'stock'(目前仅个股)
    open       DOUBLE,
    high       DOUBLE,
    low        DOUBLE,
    close      DOUBLE,
    volume     DOUBLE,               -- 成交量, 手
    amount     DOUBLE,               -- 成交额, 元
    PRIMARY KEY (symbol, freq, trade_time)
);
CREATE INDEX IF NOT EXISTS idx_minute_bar_time ON minute_bar(trade_time);
CREATE INDEX IF NOT EXISTS idx_minute_bar_sym  ON minute_bar(symbol, freq);

-- 后复权分钟视图: 复权因子按 bar 所在交易日(trade_time::DATE) join; 缺因子 COALESCE→1.
-- 跨除权的日内动量/形态计算读此视图; 当日盘口/涨停判定读 minute_bar(名义价).
CREATE OR REPLACE VIEW minute_bar_adj AS
SELECT m.symbol, m.freq, m.trade_time, m.type,
       m.open  * COALESCE(f.adj_factor, 1) AS open,
       m.high  * COALESCE(f.adj_factor, 1) AS high,
       m.low   * COALESCE(f.adj_factor, 1) AS low,
       m.close * COALESCE(f.adj_factor, 1) AS close,
       m.volume, m.amount,
       COALESCE(f.adj_factor, 1) AS adj_factor
FROM minute_bar m
LEFT JOIN adj_factor f
  ON m.symbol = f.symbol AND CAST(m.trade_time AS DATE) = f.trade_date;

-- src 入主键: 同一(symbol,trade_date)允许多源共存, 杜绝 baidu/tushare 互相静默覆盖(丢数据).
-- 读取走下方 valuation_daily_canon(每只票选单一口径), 避免分位窗口混源.
CREATE TABLE IF NOT EXISTS valuation_daily (
    symbol     VARCHAR NOT NULL,
    trade_date DATE    NOT NULL,
    pe_ttm     DOUBLE,   -- 滚动市盈率
    pb         DOUBLE,   -- 市净率
    total_mv   DOUBLE,   -- 总市值(亿元), 个股有/指数留空
    src        VARCHAR NOT NULL DEFAULT 'unknown',  -- 'tushare' | 'baidu' | 'legulegu'
    PRIMARY KEY (symbol, trade_date, src)
);
CREATE INDEX IF NOT EXISTS idx_valuation_date ON valuation_daily(trade_date);

-- 估值 canonical 视图: 每只 symbol 选**单一来源**(行数最多, 同数按 tushare>baidu>legulegu),
-- 保证 PE/PB 分位窗口口径一致, 且消除多源同日的随机覆盖. 估值分位/回测价值版读此视图.
CREATE OR REPLACE VIEW valuation_daily_canon AS
WITH src_count AS (
    SELECT symbol, src, COUNT(*) AS n,
           CASE src WHEN 'tushare' THEN 1 WHEN 'baidu' THEN 2
                    WHEN 'legulegu' THEN 3 ELSE 9 END AS pri
    FROM valuation_daily GROUP BY symbol, src
),
chosen AS (
    SELECT symbol, src,
           ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY n DESC, pri ASC) AS rn
    FROM src_count
)
SELECT v.symbol, v.trade_date, v.pe_ttm, v.pb, v.total_mv, v.src
FROM valuation_daily v
JOIN chosen c ON v.symbol = c.symbol AND v.src = c.src AND c.rn = 1;

-- 个股基本面/质量指标 (取最近年报). 稳健·价值版的质量因子从此表读, DB-first.
-- 单位均为 %. ROE/增速越高越好; 负债率仅展示不计分(金融地产天然高).
CREATE TABLE IF NOT EXISTS fundamentals (
    symbol       VARCHAR NOT NULL,
    report_date  DATE    NOT NULL,   -- 报告期(最近年报 YYYY-12-31)
    roe          DOUBLE,             -- 净资产收益率, %
    net_margin   DOUBLE,             -- 销售净利率, %
    gross_margin DOUBLE,             -- 毛利率, %
    debt_ratio   DOUBLE,             -- 资产负债率, %
    profit_yoy   DOUBLE,             -- 归母净利润增长率, %
    revenue_yoy  DOUBLE,             -- 营业总收入增长率, %
    industry     VARCHAR,            -- 所处行业(东财), 用于稳健版行业分散约束
    PRIMARY KEY (symbol, report_date)
);

-- 逐季历史基本面 (Tushare fina_indicator_vip). 与上面的 `fundamentals`(东财最新一期, 供实时UI)
-- 分开: 此表带 ann_date(公告日)用于**防未来函数**的回测, 覆盖多报告期历史(含已退市股).
-- 单位均为 %. symbol = 6位代码.
CREATE TABLE IF NOT EXISTS fundamentals_ts (
    symbol       VARCHAR NOT NULL,
    end_date     DATE    NOT NULL,   -- 报告期(季末)
    ann_date     DATE,               -- 公告日: 回测取数须 ann_date<=当日, 否则未来函数
    roe          DOUBLE,             -- 净资产收益率
    gross_margin DOUBLE,             -- 销售毛利率
    debt_ratio   DOUBLE,             -- 资产负债率
    profit_yoy   DOUBLE,             -- 归母净利润同比
    revenue_yoy  DOUBLE,             -- 营业收入同比
    PRIMARY KEY (symbol, end_date)
);
CREATE INDEX IF NOT EXISTS idx_fund_ts_ann ON fundamentals_ts(ann_date);

-- 场外/债券基金单位净值时间序列 (T-1 定稿). UI/健康分从此表读, 不每次现抓 eastmoney.
-- symbol = 基金代码. daily_pct = 当日日增长率 %.
CREATE TABLE IF NOT EXISTS fund_nav (
    symbol    VARCHAR NOT NULL,
    nav_date  DATE    NOT NULL,
    nav       DOUBLE,             -- 单位净值
    daily_pct DOUBLE,             -- 日增长率, %
    PRIMARY KEY (symbol, nav_date)
);
CREATE INDEX IF NOT EXISTS idx_fund_nav_date ON fund_nav(nav_date);

-- 组合历史快照(组合层): 每个交易日一行, 用于收益曲线 / 最大回撤计算.
-- 价格自动估值得到, 份额/成本来自 positions.yaml (隐私, 不入库代码).
CREATE TABLE IF NOT EXISTS portfolio_snapshot (
    snapshot_date      DATE PRIMARY KEY,
    total_market_value DOUBLE,   -- 可估值总市值 + 现金, 元
    total_cost         DOUBLE,   -- 成本合计, 元
    total_pnl          DOUBLE,   -- 累计浮动盈亏, 元
    today_pnl          DOUBLE,   -- 当日盈亏(跨日期近似), 元
    cash               DOUBLE,
    n_holdings         INTEGER,  -- 可估值持仓数
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 组合历史快照(明细层): 每个交易日 × 每个持仓一行.
CREATE TABLE IF NOT EXISTS position_snapshot (
    snapshot_date DATE    NOT NULL,
    code          VARCHAR NOT NULL,
    name          VARCHAR,
    type          VARCHAR,
    shares        DOUBLE,
    cost          DOUBLE,
    price         DOUBLE,
    price_date    DATE,
    price_kind    VARCHAR,
    market_value  DOUBLE,
    pnl           DOUBLE,
    today_pnl     DOUBLE,
    weight        DOUBLE,   -- 占组合权重 %
    PRIMARY KEY (snapshot_date, code)
);

-- 财经新闻原文 (Phase 4 舆情). 市场级, 不绑个股. 每日 LLM 舆情分析的输入.
CREATE TABLE IF NOT EXISTS news_raw (
    news_date DATE    NOT NULL,    -- 新闻所属交易日/自然日
    source    VARCHAR NOT NULL,    -- 'em_global'(东财快讯) | 'cctv'(新闻联播)
    title     VARCHAR NOT NULL,
    summary   VARCHAR,             -- 摘要/正文(cctv 存正文)
    ts        TIMESTAMP,           -- 发布时间(cctv 无, 留空)
    url       VARCHAR,
    PRIMARY KEY (news_date, source, title)
);
CREATE INDEX IF NOT EXISTS idx_news_date ON news_raw(news_date);

-- 可配置资讯源. 先用于新闻/舆情采集扩展, 避免把来源硬编码在 ingest_news.py。
-- scope_type: market | stock | sector | concept | macro
CREATE TABLE IF NOT EXISTS intelligence_source (
    name            VARCHAR PRIMARY KEY,
    source_type     VARCHAR NOT NULL,  -- akshare | rss | api | html | custom
    url             VARCHAR,
    enabled         BOOLEAN DEFAULT TRUE,
    scope_type      VARCHAR NOT NULL DEFAULT 'market',
    scope_value     VARCHAR,
    market          VARCHAR NOT NULL DEFAULT 'cn',
    description     VARCHAR,
    last_status     VARCHAR,
    last_error      VARCHAR,
    last_fetched_at TIMESTAMP,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_intel_source_scope
    ON intelligence_source(scope_type, scope_value, market);

INSERT INTO intelligence_source
    (name, source_type, enabled, scope_type, scope_value, market, description)
VALUES
    ('em_global', 'akshare', TRUE, 'market', NULL, 'cn', '东方财富全球财经快讯'),
    ('cctv', 'akshare', TRUE, 'market', NULL, 'cn', '央视新闻联播文字稿')
ON CONFLICT (name) DO NOTHING;

-- 新闻结构化派生层: UI/策略不再只能临时从 title/summary 里扫关键词。
-- news_key = sha1(news_date|source|title) 或其它稳定 id, 由写入端生成。
CREATE TABLE IF NOT EXISTS news_signal (
    news_key     VARCHAR PRIMARY KEY,
    news_date    DATE    NOT NULL,
    source       VARCHAR NOT NULL,
    title        VARCHAR NOT NULL,
    local_score  DOUBLE,           -- 本地词典/规则评分, 例如 -5~+5
    risk_level   INTEGER,          -- 0~3
    polarity     VARCHAR,          -- 偏多 | 中性 | 偏空
    reasons      VARCHAR,          -- JSON/分号文本: 触发规则
    themes       VARCHAR,          -- JSON/分号文本: 主题标签
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_news_signal_date ON news_signal(news_date);
CREATE INDEX IF NOT EXISTS idx_news_signal_risk ON news_signal(risk_level, news_date);

CREATE TABLE IF NOT EXISTS news_tag (
    news_key   VARCHAR NOT NULL,
    tag        VARCHAR NOT NULL,
    tag_type   VARCHAR NOT NULL,   -- theme | source_tag | risk | policy | macro | custom
    confidence DOUBLE,
    source     VARCHAR,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (news_key, tag_type, tag)
);
CREATE INDEX IF NOT EXISTS idx_news_tag_tag ON news_tag(tag_type, tag);

CREATE TABLE IF NOT EXISTS news_entity (
    news_key     VARCHAR NOT NULL,
    entity_type  VARCHAR NOT NULL, -- stock | index | sector | concept | macro | fund
    symbol       VARCHAR NOT NULL DEFAULT '',
    name         VARCHAR NOT NULL DEFAULT '',
    match_text   VARCHAR NOT NULL DEFAULT '',
    confidence   DOUBLE,
    source       VARCHAR,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (news_key, entity_type, symbol, name, match_text)
);
CREATE INDEX IF NOT EXISTS idx_news_entity_symbol ON news_entity(entity_type, symbol);
CREATE INDEX IF NOT EXISTS idx_news_entity_name ON news_entity(entity_type, name);

-- 每日舆情分析 (Claude 基于 news_raw 生成). 可解释 + 留档复盘. 一天一行.
CREATE TABLE IF NOT EXISTS sentiment_daily (
    as_of_date DATE PRIMARY KEY,
    score      DOUBLE,            -- -1(极空) ~ +1(极多)
    label      VARCHAR,           -- 偏多 | 中性 | 偏空
    summary    VARCHAR,           -- 1-2 句结论
    bullish    VARCHAR,           -- 利好主题(分号分隔)
    bearish    VARCHAR,           -- 利空主题(分号分隔)
    n_news     INTEGER,           -- 输入新闻条数
    model      VARCHAR,           -- 生成模型 id (诚实: 标注AI生成)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- LLM 调用审计. 不存明文 API key; prompt_hash/input_hash 用于复盘版本。
CREATE TABLE IF NOT EXISTS llm_run (
    run_id            VARCHAR PRIMARY KEY,
    task_type         VARCHAR NOT NULL,  -- sentiment | premarket | endday | stockeval | other
    subject_type      VARCHAR,
    subject_id        VARCHAR,
    model             VARCHAR,
    base_url          VARCHAR,
    status            VARCHAR NOT NULL,  -- ok | skipped | error
    input_count       INTEGER,
    prompt_hash       VARCHAR,
    input_hash        VARCHAR,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    total_tokens      INTEGER,
    error             VARCHAR,
    started_at        TIMESTAMP,
    finished_at       TIMESTAMP,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_llm_run_task_time ON llm_run(task_type, created_at);
CREATE INDEX IF NOT EXISTS idx_llm_run_subject ON llm_run(subject_type, subject_id);

-- 分析输入上下文快照. 存低敏 JSON, 用于解释“这次 AI/规则为什么这么判断”。
CREATE TABLE IF NOT EXISTS analysis_context_snapshot (
    run_id            VARCHAR PRIMARY KEY,
    task_type         VARCHAR NOT NULL,
    subject_type      VARCHAR,
    subject_id        VARCHAR,
    context_json      VARCHAR NOT NULL,
    data_quality_json VARCHAR,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_context_snapshot_task_time
    ON analysis_context_snapshot(task_type, created_at);

-- 决策信号留痕: recommend/premarket/endday/pf 在产出末端 best-effort 写入。
-- signal_id = sha1(trade_date|source|style|symbol|horizon_days) 前16位, 保证同日同源同标的幂等。
CREATE TABLE IF NOT EXISTS decision_signal (
    signal_id    VARCHAR PRIMARY KEY,
    created_at   TIMESTAMP,
    trade_date   DATE,
    source       VARCHAR,
    style        VARCHAR,
    symbol       VARCHAR,
    name         VARCHAR,
    asset_type   VARCHAR,
    action       VARCHAR,
    score        DOUBLE,
    ref_price    DOUBLE,
    horizon_days INTEGER,
    factors      VARCHAR,
    rationale    VARCHAR,
    status       VARCHAR
);
CREATE INDEX IF NOT EXISTS idx_decision_signal_status ON decision_signal(status, trade_date);
CREATE INDEX IF NOT EXISTS idx_decision_signal_source ON decision_signal(source, trade_date);

-- 决策信号 outcome 回溯: 一条可评估信号对应一条收益结果。
CREATE TABLE IF NOT EXISTS decision_outcome (
    signal_id        VARCHAR PRIMARY KEY,
    evaluated_at     TIMESTAMP,
    end_date         DATE,
    bars_forward     INTEGER,
    ref_price        DOUBLE,
    end_price        DOUBLE,
    fwd_return_pct   DOUBLE,
    bench_return_pct DOUBLE,
    excess_pct       DOUBLE,
    outcome          VARCHAR,
    note             VARCHAR
);
CREATE INDEX IF NOT EXISTS idx_decision_outcome_outcome ON decision_outcome(outcome);

-- 北向资金: aggregated by channel. channel in {'北向','沪股通','深股通'}
CREATE TABLE IF NOT EXISTS northbound (
    trade_date     DATE    NOT NULL,
    channel        VARCHAR NOT NULL,
    net_buy        DOUBLE,   -- 当日成交净买额 (亿元)
    buy_amount     DOUBLE,   -- 买入成交额
    sell_amount    DOUBLE,   -- 卖出成交额
    cum_net_buy    DOUBLE,   -- 历史累计净买额 (万亿)
    capital_inflow DOUBLE,   -- 当日资金流入
    balance        DOUBLE,   -- 当日余额
    holding_value  DOUBLE,   -- 持股市值
    PRIMARY KEY (trade_date, channel)
);

-- 盘中市场温度快照: 来自财联社/东方财富等宽度统计。
-- 用于验证新闻情绪是否被市场确认。
CREATE TABLE IF NOT EXISTS market_breadth_snapshot (
    snap_time      TIMESTAMP NOT NULL,
    source         VARCHAR   NOT NULL,
    up_count       INTEGER,
    down_count     INTEGER,
    flat_count     INTEGER,
    suspend_count  INTEGER,
    avg_rise       DOUBLE,   -- 平均涨幅, %
    up_ratio       DOUBLE,   -- 上涨家数占比, %
    up_down_ratio  DOUBLE,   -- 上涨/下跌
    limit_up       INTEGER,
    limit_down     INTEGER,
    limit_ratio    DOUBLE,
    sentiment_desc VARCHAR,  -- 普涨/偏强/偏弱/冰点等
    raw_json       VARCHAR,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (snap_time, source)
);
CREATE INDEX IF NOT EXISTS idx_market_breadth_time ON market_breadth_snapshot(snap_time);

-- 全球/跨市场指数快照: 盘前、夜盘和外盘风险输入。
CREATE TABLE IF NOT EXISTS global_index_snapshot (
    snap_time   TIMESTAMP NOT NULL,
    source      VARCHAR   NOT NULL,
    region      VARCHAR,
    code        VARCHAR   NOT NULL,
    name        VARCHAR,
    location    VARCHAR,
    last_price  DOUBLE,
    pct_chg     DOUBLE,
    state       VARCHAR,
    raw_json    VARCHAR,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (snap_time, source, code)
);
CREATE INDEX IF NOT EXISTS idx_global_index_region_time
    ON global_index_snapshot(region, snap_time);

-- 行业/概念资金流快照: 用于验证新闻主题是否被资金确认。
CREATE TABLE IF NOT EXISTS sector_fund_flow_snapshot (
    snap_time   TIMESTAMP NOT NULL,
    source      VARCHAR   NOT NULL,
    scope_type  VARCHAR   NOT NULL, -- industry | concept
    code        VARCHAR   NOT NULL,
    name        VARCHAR,
    net_inflow  DOUBLE,             -- 主力净流入, 元
    raw_json    VARCHAR,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (snap_time, source, scope_type, code)
);
CREATE INDEX IF NOT EXISTS idx_sector_flow_scope_time
    ON sector_fund_flow_snapshot(scope_type, snap_time);
CREATE INDEX IF NOT EXISTS idx_sector_flow_name_time
    ON sector_fund_flow_snapshot(name, snap_time);

-- 个股异动事件: 火箭发射/高台跳水/封板/大笔买卖等, 与新闻/持仓做交叉验证。
CREATE TABLE IF NOT EXISTS stock_change_event (
    event_time   TIMESTAMP NOT NULL,
    source       VARCHAR   NOT NULL,
    symbol       VARCHAR   NOT NULL,
    name         VARCHAR,
    change_type  VARCHAR,
    change_name  VARCHAR,
    price        DOUBLE,
    pct_chg      DOUBLE,
    amount       DOUBLE,
    volume       DOUBLE,
    industry     VARCHAR,
    concept      VARCHAR,
    raw_json     VARCHAR,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (event_time, source, symbol, change_type)
);
CREATE INDEX IF NOT EXISTS idx_stock_change_symbol_time
    ON stock_change_event(symbol, event_time);
CREATE INDEX IF NOT EXISTS idx_stock_change_type_time
    ON stock_change_event(change_type, event_time);

-- ============================================================================
-- 龙虎榜 + 资金流向 (东财, 均 T+1 盘后公布). 用于交叉验证: 持仓/自选是否上榜、
-- 被机构还是游资买、主力资金是否配合. 金额除注明外均为「元」.
-- ============================================================================

-- 龙虎榜每日明细 (stock_lhb_detail_em): 上榜个股 + 上榜原因 + 龙虎榜买卖.
CREATE TABLE IF NOT EXISTS lhb_detail (
    trade_date     DATE    NOT NULL,   -- 上榜日
    symbol         VARCHAR NOT NULL,
    name           VARCHAR,
    close          DOUBLE,             -- 收盘价
    pct_chg        DOUBLE,             -- 涨跌幅, %
    net_buy        DOUBLE,             -- 龙虎榜净买额, 元
    buy_amt        DOUBLE,             -- 龙虎榜买入额, 元
    sell_amt       DOUBLE,             -- 龙虎榜卖出额, 元
    lhb_amt        DOUBLE,             -- 龙虎榜成交额, 元
    net_ratio      DOUBLE,             -- 净买额占市场总成交比, %
    turnover_rate  DOUBLE,             -- 换手率, %
    float_mv       DOUBLE,             -- 流通市值, 元
    reason         VARCHAR,            -- 上榜原因
    interpret      VARCHAR,            -- 解读 (如 "1家机构卖出，成功率31.51%")
    PRIMARY KEY (trade_date, symbol, reason)
);
CREATE INDEX IF NOT EXISTS idx_lhb_detail_symbol ON lhb_detail(symbol, trade_date);

-- 机构专用席位每日统计 (stock_lhb_jgmmtj_em): 主力机构参与的个股汇总.
-- 注意: float_mv 本表单位为「亿元」(上游口径, 与 lhb_detail 不同).
CREATE TABLE IF NOT EXISTS lhb_inst (
    trade_date     DATE    NOT NULL,   -- 上榜日期
    symbol         VARCHAR NOT NULL,
    name           VARCHAR,
    close          DOUBLE,
    pct_chg        DOUBLE,
    buy_org_n      INTEGER,            -- 买方机构数
    sell_org_n     INTEGER,            -- 卖方机构数
    org_buy_amt    DOUBLE,             -- 机构买入总额, 元
    org_sell_amt   DOUBLE,             -- 机构卖出总额, 元
    org_net_amt    DOUBLE,             -- 机构买入净额, 元
    net_ratio      DOUBLE,             -- 机构净买额占总成交比, %
    turnover_rate  DOUBLE,             -- 换手率, %
    float_mv       DOUBLE,             -- 流通市值, 亿元
    reason         VARCHAR,            -- 上榜原因
    PRIMARY KEY (trade_date, symbol, reason)
);
CREATE INDEX IF NOT EXISTS idx_lhb_inst_symbol ON lhb_inst(symbol, trade_date);

-- 每日活跃营业部 / 一线游资 (stock_lhb_hyyyb_em): 按营业部汇总当日买卖.
CREATE TABLE IF NOT EXISTS lhb_yyb (
    trade_date     DATE    NOT NULL,   -- 上榜日
    yyb_code       VARCHAR NOT NULL,   -- 营业部代码
    yyb_name       VARCHAR,            -- 营业部名称 (游资席位名)
    buy_stock_n    INTEGER,            -- 买入个股数
    sell_stock_n   INTEGER,            -- 卖出个股数
    buy_amt        DOUBLE,             -- 买入总金额, 元
    sell_amt       DOUBLE,             -- 卖出总金额, 元
    net_amt        DOUBLE,             -- 总买卖净额, 元
    buy_stocks     VARCHAR,            -- 当日买入股票(空格分隔)
    PRIMARY KEY (trade_date, yyb_code)
);
CREATE INDEX IF NOT EXISTS idx_lhb_yyb_name ON lhb_yyb(yyb_name, trade_date);

-- 个股主力资金流 (stock_individual_fund_flow, 按股逐日). 主力=超大单+大单(东财口径推断).
CREATE TABLE IF NOT EXISTS stock_fund_flow (
    symbol      VARCHAR NOT NULL,
    trade_date  DATE    NOT NULL,
    close       DOUBLE,
    pct_chg     DOUBLE,             -- 涨跌幅, %
    main_net    DOUBLE,             -- 主力净流入-净额, 元
    main_ratio  DOUBLE,             -- 主力净流入-净占比, %
    xl_net      DOUBLE,             -- 超大单净流入, 元
    l_net       DOUBLE,             -- 大单净流入, 元
    m_net       DOUBLE,             -- 中单净流入, 元
    s_net       DOUBLE,             -- 小单净流入, 元
    PRIMARY KEY (symbol, trade_date)
);
CREATE INDEX IF NOT EXISTS idx_stock_fund_flow_date ON stock_fund_flow(trade_date);

-- 大盘资金流 (stock_market_fund_flow, 沪深整体, 逐日).
CREATE TABLE IF NOT EXISTS market_fund_flow (
    trade_date  DATE    NOT NULL,
    sh_close    DOUBLE,             -- 上证收盘
    sh_pct      DOUBLE,             -- 上证涨跌幅, %
    sz_close    DOUBLE,             -- 深证收盘
    sz_pct      DOUBLE,             -- 深证涨跌幅, %
    main_net    DOUBLE,             -- 主力净流入-净额, 元
    main_ratio  DOUBLE,             -- 主力净流入-净占比, %
    xl_net      DOUBLE,             -- 超大单净流入, 元
    l_net       DOUBLE,             -- 大单净流入, 元
    m_net       DOUBLE,             -- 中单净流入, 元
    s_net       DOUBLE,             -- 小单净流入, 元
    PRIMARY KEY (trade_date)
);

-- 头部沪深300ETF规模占比.
-- 分子: 天天基金规模变动「期末净资产」(元), 季度.
-- 分母实点: 东方财富沪深300行情流通市值(元). 指数收盘用于把实点回推到历史季度.
CREATE TABLE IF NOT EXISTS hs300_fund_scale (
    symbol      VARCHAR NOT NULL,   -- 6位基金代码
    name        VARCHAR,
    report_date DATE    NOT NULL,   -- 规模报告期(通常季末)
    net_asset   DOUBLE,             -- 期末净资产, 元
    shares      DOUBLE,             -- 期末总份额, 份
    src         VARCHAR NOT NULL DEFAULT 'eastmoney_gmbd',
    PRIMARY KEY (symbol, report_date)
);
CREATE INDEX IF NOT EXISTS idx_hs300_fund_scale_date ON hs300_fund_scale(report_date);

CREATE TABLE IF NOT EXISTS hs300_index_close (
    trade_date DATE PRIMARY KEY,
    close      DOUBLE NOT NULL,     -- 沪深300收盘, 点
    src        VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS hs300_float_mv (
    trade_date DATE PRIMARY KEY,    -- 行情时间所在交易日
    close      DOUBLE,              -- 快照时点位(最新价)
    total_mv   DOUBLE,              -- 总市值, 元
    float_mv   DOUBLE NOT NULL,     -- 流通市值, 元
    src        VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS hs300_ratio_meta (
    kind       VARCHAR PRIMARY KEY, -- 'fund_scale' | 'index_close' | 'float_mv'
    fetched_at TIMESTAMP NOT NULL
);
