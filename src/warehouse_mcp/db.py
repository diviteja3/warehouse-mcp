"""Database access with read-only enforced by the database, not just by us.

- PostgreSQL: each query runs in `SET TRANSACTION READ ONLY` with a
  `SET LOCAL statement_timeout`, so writes fail and runaway queries stop at
  the server even if the SQL guard missed something.
- SQLite: connections set `PRAGMA query_only = ON` and a progress handler
  stops queries that exceed the timeout.

Use a database role with SELECT-only grants in production as well. Each layer
assumes the others might fail.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine


class QueryTimeout(RuntimeError):
    pass


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool
    elapsed_ms: int


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    return str(value)  # dates, decimals, UUIDs


class Warehouse:
    def __init__(self, url: str, statement_timeout_ms: int = 5000):
        self.timeout_ms = statement_timeout_ms
        self.engine: Engine = create_engine(url, pool_pre_ping=True)
        self.dialect = self.engine.dialect.name  # "postgresql", "sqlite", ...

        if self.dialect == "sqlite":

            @event.listens_for(self.engine, "connect")
            def _read_only(dbapi_conn, _record):
                dbapi_conn.execute("PRAGMA query_only = ON")

    @property
    def sqlglot_dialect(self) -> str:
        return {"postgresql": "postgres"}.get(self.dialect, self.dialect)

    # ---- metadata -----------------------------------------------------------

    def list_tables(self, allowed_schemas: frozenset[str] | None = None) -> list[dict]:
        insp = inspect(self.engine)
        if self.dialect == "sqlite":
            schemas = [None]
        else:
            schemas = [s for s in insp.get_schema_names() if s not in ("information_schema", "pg_catalog", "pg_toast")]
            if allowed_schemas is not None:
                schemas = [s for s in schemas if s.lower() in allowed_schemas]

        out = []
        for schema in schemas:
            for name in insp.get_table_names(schema=schema):
                out.append({"schema": schema, "table": name, "kind": "table"})
            for name in insp.get_view_names(schema=schema):
                out.append({"schema": schema, "table": name, "kind": "view"})
        return out

    def describe_table(self, table: str, schema: str | None = None) -> dict:
        insp = inspect(self.engine)
        if not insp.has_table(table, schema=schema) and table not in insp.get_view_names(schema=schema):
            raise LookupError(f"Table {schema + '.' if schema else ''}{table} not found.")
        columns = [
            {"name": c["name"], "type": str(c["type"]), "nullable": c.get("nullable", True)}
            for c in insp.get_columns(table, schema=schema)
        ]
        pk = insp.get_pk_constraint(table, schema=schema).get("constrained_columns", [])
        fks = [
            {
                "columns": fk["constrained_columns"],
                "references": f"{fk['referred_table']}({', '.join(fk['referred_columns'])})",
            }
            for fk in insp.get_foreign_keys(table, schema=schema)
        ]
        return {"schema": schema, "table": table, "columns": columns, "primary_key": pk, "foreign_keys": fks}

    # ---- queries ------------------------------------------------------------

    def run(self, sql: str, max_rows: int) -> QueryResult:
        started = time.perf_counter()
        with self.engine.connect() as conn:
            if self.dialect == "postgresql":
                conn.exec_driver_sql("SET TRANSACTION READ ONLY")
                conn.exec_driver_sql(f"SET LOCAL statement_timeout = {int(self.timeout_ms)}")
            elif self.dialect == "sqlite":
                deadline = started + self.timeout_ms / 1000
                raw = conn.connection.driver_connection
                raw.set_progress_handler(lambda: 1 if time.perf_counter() > deadline else 0, 10_000)

            try:
                result = conn.execute(text(sql))
                columns = list(result.keys())
                fetched = result.fetchmany(max_rows + 1)
            except Exception as exc:
                if self.dialect == "sqlite" and "interrupted" in str(exc).lower():
                    raise QueryTimeout(f"Query exceeded {self.timeout_ms} ms and was stopped.") from None
                if "statement timeout" in str(exc).lower():
                    raise QueryTimeout(f"Query exceeded {self.timeout_ms} ms and was stopped.") from None
                raise
            finally:
                if self.dialect == "sqlite":
                    conn.connection.driver_connection.set_progress_handler(None, 0)
                conn.rollback()

        truncated = len(fetched) > max_rows
        rows = [[_jsonable(v) for v in row] for row in fetched[:max_rows]]
        return QueryResult(columns, rows, truncated, round((time.perf_counter() - started) * 1000))
