"""推送通道 — 盘中预警等用. 目前支持 Server酱(微信).

密钥从 gitignored `.serverchan_key` 文件或环境变量 SERVERCHAN_KEY 读。无密钥/失败
返回 False(调用方据此如实提示, 不假装已推送)。
"""
from __future__ import annotations
import logging
import os
from pathlib import Path

import requests

log = logging.getLogger(__name__)
_SC_KEY_FILE = Path(__file__).resolve().parents[3] / ".serverchan_key"


def _serverchan_key() -> str | None:
    k = os.environ.get("SERVERCHAN_KEY")
    if k and k.strip():
        return k.strip()
    if _SC_KEY_FILE.exists():
        t = _SC_KEY_FILE.read_text(encoding="utf-8").strip()
        if t:
            return t
    return None


def send_push(title: str, body: str, cfg: dict | None = None) -> bool:
    """按 cfg.channel 推送, 返回是否成功。"""
    cfg = cfg or {}
    channel = cfg.get("channel", "serverchan")
    if channel == "serverchan":
        key = _serverchan_key()
        if not key:
            log.warning("Server酱 未配置密钥(.serverchan_key 或 SERVERCHAN_KEY)")
            return False
        try:
            r = requests.post(f"https://sctapi.ftqq.com/{key}.send",
                              data={"title": title[:100], "desp": body}, timeout=10)
            ok = r.ok and r.json().get("code", -1) == 0
            if not ok:
                log.warning("Server酱 返回异常: %s", r.text[:200])
            return ok
        except Exception as e:  # noqa: BLE001
            log.warning("Server酱 推送失败: %s", e)
            return False
    log.warning("未知推送通道: %s", channel)
    return False
