"""Data-source adapters.

AkShare, Hexin, and Tencent are imported on first use so a Tushare-only
command can run when those packages are not installed.
"""
from __future__ import annotations

__all__ = ["AkSource", "HexinSource", "TencentSource"]


def __getattr__(name: str):
    if name == "AkSource":
        from .akshare_src import AkSource
        value = AkSource
    elif name == "HexinSource":
        from .hexin_src import HexinSource
        value = HexinSource
    elif name == "TencentSource":
        from .tencent_src import TencentSource
        value = TencentSource
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value
