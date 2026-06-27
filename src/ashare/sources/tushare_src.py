"""Tushare 数据源初始化。

统一入口:其他模块只 `from ashare.sources.tushare_src import get_pro` 即可,
token 从 gitignored 的 `.tushare_token`(或环境变量 TUSHARE_TOKEN)读取,不入库。

调用约定(见购买方说明):
- 必须把 _DataApi__http_url 指向代理端点,否则会报 "Token 不对"。
- pro_bar 需要显式传 api=pro。
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import tushare as ts

# 代理端点(非官方 tushare.pro,购买方提供;非机密,可入库)
_HTTP_URL = "https://tt.dailyfetch.top/"

# token 文件:仓库根目录下的 .tushare_token(已 gitignore)
_TOKEN_FILE = Path(__file__).resolve().parents[3] / ".tushare_token"


def _read_token() -> str:
    env = os.environ.get("TUSHARE_TOKEN")
    if env:
        return env.strip()
    if _TOKEN_FILE.exists():
        return _TOKEN_FILE.read_text().strip()
    raise RuntimeError(
        f"未找到 Tushare token:设置环境变量 TUSHARE_TOKEN 或写入 {_TOKEN_FILE}"
    )


@lru_cache(maxsize=1)
def get_pro():
    """返回已配置好端点的 pro_api 句柄(进程内复用)。"""
    pro = ts.pro_api(_read_token())
    pro._DataApi__http_url = _HTTP_URL
    return pro


def pro_bar(**kwargs):
    """ts.pro_bar 封装,自动带上 api=pro。"""
    return ts.pro_bar(api=get_pro(), **kwargs)
