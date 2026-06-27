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
