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
    # DuckDB leaves the script result open; the next statement on this connection
    # then raises "unfinished result" unless the result is consumed.
    con.execute(SCHEMA_PATH.read_text(encoding="utf-8")).fetchall()


def upsert(
    con: duckdb.DuckDBPyConnection,
    table: str,
    df: pd.DataFrame,
    keys: Sequence[str],
) -> int:
    """Idempotent upsert using DuckDB's native ON CONFLICT.

    Older DELETE ... USING + INSERT logic can invalidate DuckDB connections when
    an indexed table hits an internal index-delete failure. Native conflict
    handling avoids that failure mode and keeps updates atomic.
    """
    if df is None or df.empty:
        return 0
    # fetchone() leaves the connection's result open; register() then fails on
    # DuckDB 2.x. complete() finishes that result and is a no-op when idle.
    complete = getattr(con, "complete", None)
    if complete is not None:
        complete()
    cols = list(df.columns)
    col_list = ", ".join(cols)
    key_list = ", ".join(keys)
    non_keys = [c for c in cols if c not in set(keys)]
    if non_keys:
        conflict = "DO UPDATE SET " + ", ".join(f"{c}=excluded.{c}" for c in non_keys)
    else:
        conflict = "DO NOTHING"
    con.register("_stg", df)
    try:
        con.execute(
            f"INSERT INTO {table} ({col_list}) SELECT {col_list} FROM _stg "
            f"ON CONFLICT ({key_list}) {conflict}"
        )
    finally:
        con.unregister("_stg")
    return len(df)
