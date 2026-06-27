"""每日财经新闻入库 (Phase 4 舆情输入). 市场级, 不绑个股.

源(均免费, 实测可用): 东财全球财经快讯 `global_news`(~200条带时间) + 新闻联播
`cctv_news`(政策面)。财联社电报已404, 不用。akshare 原生 requests 无 timeout 会挂死
(见 data-source-caveats #7), 故必须经 AkSource(其 __init__ 装了 10s 超时守卫)调用。

落 `news_raw`(幂等). news_date 用"自然日": 东财快讯按发布时间日期, 新闻联播按当日。
"""
from __future__ import annotations
import logging
from datetime import date

import pandas as pd

from ..sources import AkSource
from ..storage import upsert

log = logging.getLogger(__name__)

_COLS = ["news_date", "source", "title", "summary", "ts", "url"]


def ingest_news(con, src: AkSource, as_of: date | None = None) -> int:
    """拉东财快讯(当日) + 新闻联播(当日) → news_raw。返回落库行数。"""
    as_of = as_of or date.today()
    frames = []

    # 东财全球财经快讯: 只留发布时间属于 as_of 当天的(接口给最新200条, 跨1~2天)
    try:
        g = src.global_news()
        g = g[g["ts"].dt.date == as_of]
        if not g.empty:
            g = g.assign(news_date=as_of)
            frames.append(g)
    except Exception as e:  # noqa: BLE001 - 单源失败不阻断
        log.warning("global_news 失败: %s", e)

    # 新闻联播(政策面): 当日文字稿
    try:
        c = src.cctv_news(as_of.strftime("%Y%m%d"))
        if not c.empty:
            c = c.assign(news_date=as_of)
            frames.append(c)
    except Exception as e:  # noqa: BLE001
        log.warning("cctv_news 失败: %s", e)

    if not frames:
        return 0
    df = pd.concat(frames, ignore_index=True)
    df = df.dropna(subset=["title"]).drop_duplicates(["news_date", "source", "title"])
    return upsert(con, "news_raw", df[_COLS], ["news_date", "source", "title"])


def load_news(con, as_of: date, limit: int = 80) -> pd.DataFrame:
    """取 as_of 当日新闻(快讯按时间倒序优先), 供 LLM 分析。"""
    return con.execute(
        "SELECT source, title, summary, ts FROM news_raw WHERE news_date=? "
        "ORDER BY ts DESC NULLS LAST LIMIT ?", [as_of, limit]).df()
