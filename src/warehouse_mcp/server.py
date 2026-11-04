"""MCP server exposing a SQL warehouse to agents, read-only and audited."""

from __future__ import annotations

import time
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from warehouse_mcp.audit import AuditLog
from warehouse_mcp.config import Settings
from warehouse_mcp.db import QueryTimeout, Warehouse
from warehouse_mcp.masking import PiiPolicy
from warehouse_mcp.sql_guard import QueryRejected, guard

INSTRUCTIONS = """Read-only access to an analytics database.
Workflow: call list_tables, then describe_table for the tables you need, then run_query.
Only single SELECT statements are accepted. Results are capped and PII columns are masked;
`truncated: true` means more rows exist, so aggregate or filter instead of paging through raw rows.
If a query is rejected, the error says why; fix the query rather than retrying it unchanged."""

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)


def create_server(settings: Settings, warehouse: Warehouse | None = None) -> MCPServer:
    wh = warehouse or Warehouse(settings.url, settings.statement_timeout_ms)
    pii = PiiPolicy(settings.pii_columns)
    audit = AuditLog(settings.audit_log)

    server = MCPServer(name="warehouse-mcp", version="0.1.0", instructions=INSTRUCTIONS)

    @server.tool(annotations=READ_ONLY)
    def list_tables() -> dict[str, Any]:
        """List the tables and views you can query."""
        tables = wh.list_tables(settings.allowed_schemas)
        audit.record("list_tables", status="ok", count=len(tables))
        return {"tables": tables}

    @server.tool(annotations=READ_ONLY)
    def describe_table(table: str, schema: str | None = None) -> dict[str, Any]:
        """Columns, types, primary key and foreign keys for one table. Columns treated as PII are flagged."""
        if settings.allowed_schemas is not None and schema and schema.lower() not in settings.allowed_schemas:
            audit.record("describe_table", status="rejected", table=table, schema=schema)
            raise ToolError(f"Schema {schema!r} is not in the allowed list.")
        try:
            info = wh.describe_table(table, schema)
        except LookupError as exc:
            audit.record("describe_table", status="not_found", table=table, schema=schema)
            raise ToolError(str(exc)) from None
        for col in info["columns"]:
            col["pii_masked"] = pii.is_pii_column(col["name"])
        audit.record("describe_table", status="ok", table=table, schema=schema)
        return info

    @server.tool(annotations=READ_ONLY)
    def run_query(sql: str) -> dict[str, Any]:
        """Run one read-only SELECT. Returns columns, rows (capped), whether the result was truncated,
        and which columns were masked as PII."""
        started = time.perf_counter()
        try:
            q = guard(
                sql,
                dialect=wh.sqlglot_dialect,
                max_rows=settings.max_rows,
                allowed_schemas=settings.allowed_schemas,
                is_pii=pii.is_pii_column,
            )
        except QueryRejected as exc:
            audit.record("run_query", status="rejected", sql=sql, reason=str(exc))
            raise ToolError(f"Query rejected: {exc}") from None

        try:
            result = wh.run(q.sql, settings.max_rows)
        except QueryTimeout as exc:
            audit.record("run_query", status="timeout", sql=sql, tables=q.tables)
            raise ToolError(str(exc)) from None
        except Exception as exc:  # database errors: report the class and first line only
            message = str(exc).splitlines()[0][:300]
            audit.record("run_query", status="error", sql=sql, tables=q.tables, error=type(exc).__name__)
            raise ToolError(f"Database error: {message}") from None

        rows, masked = pii.apply(result.columns, result.rows, q.pii_outputs)
        elapsed = round((time.perf_counter() - started) * 1000)
        audit.record(
            "run_query",
            status="ok",
            sql=sql,
            tables=q.tables,
            rows=len(rows),
            truncated=result.truncated,
            masked_columns=masked,
            ms=elapsed,
        )
        return {
            "columns": result.columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": result.truncated,
            "masked_columns": masked,
            "elapsed_ms": elapsed,
        }

    return server


def main() -> None:
    create_server(Settings.from_env()).run("stdio")


if __name__ == "__main__":
    main()
