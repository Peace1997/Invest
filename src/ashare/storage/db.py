from __future__ import annotations
from pathlib import Path
from typing import Sequence
import duckdb
import pandas as pd

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def open_db(path: str | Path, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(path), read_only=read_only)


def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(SCHEMA_PATH.read_text(encoding="utf-8"))


def upsert(
    con: duckdb.DuckDBPyConnection,
    table: str,
    df: pd.DataFrame,
    keys: Sequence[str],
) -> int:
    """Idempotent upsert via DELETE ... USING + INSERT, wrapped in a single
    transaction so a mid-way failure can't leave rows deleted-but-not-inserted.
    Returns row count."""
    if df is None or df.empty:
        return 0
    cols = list(df.columns)
    key_pred = " AND ".join(f"t.{k}=s.{k}" for k in keys)
    col_list = ", ".join(cols)
    con.register("_stg", df)
    try:
        con.execute("BEGIN TRANSACTION")
        try:
            con.execute(f"DELETE FROM {table} t USING _stg s WHERE {key_pred}")
            con.execute(f"INSERT INTO {table} ({col_list}) SELECT {col_list} FROM _stg")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")   # 删了未插 → 整体回滚, 旧数据不丢
            raise
    finally:
        con.unregister("_stg")
    return len(df)
