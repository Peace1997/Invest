"""LLM 客户端 (OpenAI 兼容) — 给舆情分析等 LLM 信号用.

支持任意 OpenAI 兼容端点(aihubmix / deepseek / openai / 各类中转), 由 config 的
`base_url` + `model` 决定。密钥从 gitignored `.anthropic_key` 文件(沿用旧文件名,
内容是当前 provider 的 key)或环境变量 LLM_API_KEY / OPENAI_API_KEY 读取。
诚实: 无密钥时抛 NoKeyError, 调用方应跳过并如实提示, 绝不编造分析。
"""
from __future__ import annotations
import os
from functools import lru_cache
from pathlib import Path

_KEY_FILE = Path(__file__).resolve().parents[2] / ".anthropic_key"


class NoKeyError(RuntimeError):
    """未配置 LLM 密钥。"""


def _read_key() -> str:
    for env in ("LLM_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        k = os.environ.get(env)
        if k and k.strip():
            return k.strip()
    if _KEY_FILE.exists():
        t = _KEY_FILE.read_text(encoding="utf-8").strip()
        if t:
            return t
    raise NoKeyError(f"未配置 LLM 密钥: 设环境变量 LLM_API_KEY 或写入 {_KEY_FILE}")


@lru_cache(maxsize=8)
def get_client(base_url: str | None = None, timeout: float = 120.0):
    # 必须给超时: 否则中转端卡住时调用会无限挂(曾导致 cli daily 的舆情步骤挂死78分钟、
    # 死握 DuckDB 写锁、看板全崩)。推理模型可能慢, 给到 120s; 配 max_retries=1 限重试。
    from openai import OpenAI
    return OpenAI(api_key=_read_key(), base_url=base_url or None,
                  timeout=timeout, max_retries=1)


def complete(prompt: str, *, model: str, max_tokens: int = 1024,
             system: str | None = None, temperature: float = 0.0,
             base_url: str | None = None, timeout: float = 120.0) -> str:
    """单轮补全(OpenAI chat 格式), 返回正文文本(不含推理模型的 reasoning_content)。
    timeout 秒后放弃(防中转端卡死拖挂调用方); 调用方应捕获异常并诚实跳过。"""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    resp = get_client(base_url, timeout).chat.completions.create(
        model=model, messages=messages, max_tokens=max_tokens, temperature=temperature)
    return (resp.choices[0].message.content or "").strip()
