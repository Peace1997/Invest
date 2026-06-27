from __future__ import annotations
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[2]   # repo root

def load(path: str | Path = "config.yaml") -> dict:
    p = ROOT / path if not Path(path).is_absolute() else Path(path)
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def resolve_path(cfg: dict, key: str) -> Path:
    raw = cfg["paths"][key]
    p = Path(raw)
    return p if p.is_absolute() else (ROOT / p)
