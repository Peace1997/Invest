"""Market state sources used to confirm or reject news sentiment."""
from __future__ import annotations

import json
from datetime import datetime

import pandas as pd
import requests

CLS_MARKET_HOME_URL = "https://x-quote.cls.cn/quote/index/home"


def _sentiment_desc(up_down_ratio: float) -> str:
    if up_down_ratio >= 2:
        return "普涨(极强)"
    if up_down_ratio >= 1.5:
        return "偏强"
    if up_down_ratio > 1:
        return "稍强"
    if up_down_ratio == 1:
        return "中性"
    if up_down_ratio > 0.5:
        return "稍弱"
    if up_down_ratio > 0:
        return "偏弱"
    return "普跌(冰点)"


def _to_float(v, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _to_int(v, default: int = 0) -> int:
    try:
        if v is None or v == "":
            return default
        return int(float(v))
    except (TypeError, ValueError):
        return default


def fetch_cls_market_breadth(timeout: float = 10.0) -> pd.DataFrame:
    """Fetch A-share market breadth from CLS.

    Returns one normalized row for `market_breadth_snapshot`.
    """
    resp = requests.get(
        CLS_MARKET_HOME_URL,
        params={"app": "CailianpressWeb", "os": "web", "sv": "8.4.6"},
        headers={
            "Referer": "https://www.cls.cn/",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36"
            ),
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    payload = resp.json()
    if _to_int(payload.get("code")) != 200:
        raise RuntimeError(f"CLS market breadth returned code={payload.get('code')} msg={payload.get('msg')}")

    data = payload.get("data") or {}
    dist = data.get("up_down_dis") or {}
    rise = _to_int(dist.get("rise_num"))
    fall = _to_int(dist.get("fall_num"))
    flat = _to_int(dist.get("flat_num"))
    limit_up = _to_int(dist.get("up_num"))
    limit_down = _to_int(dist.get("down_num"))

    total_directional = rise + fall
    up_ratio = float(rise) / total_directional * 100 if total_directional else 0.0
    if fall > 0:
        up_down_ratio = float(rise) / fall
    elif rise > 0:
        up_down_ratio = float(rise)
    else:
        up_down_ratio = 0.0

    if limit_down > 0:
        limit_ratio = float(limit_up) / limit_down
    elif limit_up > 0:
        limit_ratio = float(limit_up)
    else:
        limit_ratio = 0.0

    snap_time = datetime.now().replace(second=0, microsecond=0)
    avg_rise = _to_float(dist.get("average_rise")) * 100
    row = {
        "snap_time": snap_time,
        "source": "cls",
        "up_count": rise,
        "down_count": fall,
        "flat_count": flat,
        "suspend_count": _to_int(dist.get("suspend_num")),
        "avg_rise": avg_rise,
        "up_ratio": up_ratio,
        "up_down_ratio": up_down_ratio,
        "limit_up": limit_up,
        "limit_down": limit_down,
        "limit_ratio": limit_ratio,
        "sentiment_desc": _sentiment_desc(up_down_ratio),
        "raw_json": json.dumps(data, ensure_ascii=False),
    }
    return pd.DataFrame([row])
