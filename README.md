# warehouse-mcp

[![CI](https://github.com/diviteja3/warehouse-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/diviteja3/warehouse-mcp/actions/workflows/ci.yml)

An [MCP](https://modelcontextprotocol.io) server that lets AI agents query a SQL database **without being able to change it, stall it, or leak personal data from it**.

When a team puts an agent in front of its real data, the first question is "what stops it from running `DELETE`, or dumping every customer's email?". This server answers that with layered controls you can explain to a security reviewer, plus an audit log they can read.

```text
agent ──MCP──▶ list_tables / describe_table / run_query
                         │
          ┌──────────────▼──────────────┐
          │ 1. SQL guard (parsed AST)   │  one SELECT only; no writes, locks, INTO,
          │                             │  side-effect functions, system catalogs
          │ 2. Row-level filters        │  operator predicates rewritten into every
          │                             │  reference to a table (joins, CTEs, subqueries)
          │ 3. Row cap                  │  wraps the query: LIMIT max_rows + 1
          ├─────────────────────────────┤
          │ 4. Read-only transaction    │  enforced by the database itself
          │ 5. Statement timeout        │  stopped by the database itself
          ├─────────────────────────────┤
          │ 6. PII masking              │  by column, through aliases, and by value
          │ 7. Audit log (JSONL)        │  every call; never the result data
          └─────────────────────────────┘
```

## Why each layer exists

| Layer | Stops | Why it isn't enough on its own |
|---|---|---|
| **SQL guard**: parses with [sqlglot](https://github.com/tobymao/sqlglot) and rejects anything but one read-only query | `DELETE`, stacked statements (`SELECT 1; DROP …`), data-modifying CTEs, `SELECT INTO`, `FOR UPDATE`, `pg_sleep`, `nextval`, `pg_read_file`, `dblink`, catalog snooping | Parsers have edge cases |
| **Read-only transaction**: `SET TRANSACTION READ ONLY` (Postgres), `PRAGMA query_only` (SQLite) | Any write the guard missed. The tests bypass the guard entirely to prove this. | Doesn't limit cost |
| **Statement timeout**: `SET LOCAL statement_timeout` (Postgres), progress handler (SQLite) | Runaway joins and recursive CTEs | Doesn't limit result size |
| **Row-level filters**: each configured table is replaced in the parsed query by `(SELECT * FROM t WHERE <predicate>) AS t` | An EU-only agent reading US customers, including through joins, subqueries, CTE bodies and `UNION`s. The agent's own `OR 1 = 1` sits outside the subquery, so it can't widen the filter. A CTE named after a filtered table is rejected. | Only as good as the predicate you write |
| **Row cap**: fetches `max_rows + 1` and reports `truncated` | Agents pulling a whole table into context | — |
| **PII masking** | `SELECT email AS contact` (traced through the alias), `upper(full_name)`, and emails, SSNs or card numbers inside free-text columns | Pattern-based; see limitations |
| **Audit log** | "What did the agent look at?" | — |

Rejections come back as tool errors that say *why* (for example `Query rejected: DELETE is not allowed in a read-only query.`), so the agent can fix its query instead of retrying the same one.

## Quickstart

```bash
pip install -e ".[postgres]"          # drop [postgres] for SQLite only
python examples/seed_demo.py          # demo e-commerce DB with fake PII
WAREHOUSE_URL=sqlite:///examples/demo.db warehouse-mcp
```

### Connect it to an MCP client

Claude Desktop, Cursor, VS Code and other MCP clients use the same shape:

```json
{
  "mcpServers": {
    "warehouse": {
      "command": "warehouse-mcp",
      "env": {
        "WAREHOUSE_URL": "postgresql+psycopg://readonly_agent@db.internal:5432/analytics",
        "WAREHOUSE_ALLOWED_SCHEMAS": "analytics,reporting",
        "WAREHOUSE_MAX_ROWS": "200"
      }
    }
  }
}
```

### Example

> **Agent:** Which product categories sold the most units, delivered orders only?

```jsonc
// run_query
{"sql": "SELECT p.category, SUM(o.quantity) AS units FROM orders o JOIN products p ON p.id = o.product_id WHERE o.status = 'delivered' GROUP BY p.category ORDER BY units DESC"}
// → {"columns": ["category","units"], "rows": [...], "row_count": 6, "truncated": false, "masked_columns": [], "filtered_tables": [], "elapsed_ms": 4}
```

```jsonc
// run_query
{"sql": "SELECT id, email AS contact, region FROM customers LIMIT 3"}
// → {"columns": ["id","contact","region"], "rows": [[1,"***","EU"], ...], "masked_columns": ["contact"]}
```

```jsonc
// run_query
{"sql": "WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d"}
// → error: Query rejected: DELETE is not allowed in a read-only query.
```

## Configuration

| Variable | Default | |
|---|---|---|
| `WAREHOUSE_URL` | (required) | Any SQLAlchemy URL. Tested with PostgreSQL 16 and SQLite. |
| `WAREHOUSE_ALLOWED_SCHEMAS` | all | Comma-separated allow list |
| `WAREHOUSE_MAX_ROWS` | `200` | Row cap per query |
| `WAREHOUSE_TIMEOUT_MS` | `5000` | Statement timeout |
| `WAREHOUSE_PII_COLUMNS` | built-in list | Extra column-name patterns to mask, added to the defaults (`email`, `phone`, `ssn`, `full_name`, `address`, …) |
| `WAREHOUSE_ROW_FILTERS` | none | JSON object of row-level filters, for example `{"customers": "region = 'EU'", "sales.orders": "tenant_id = 42"}`. A bare table name matches that table in every schema. Checked at startup. |
| `WAREHOUSE_AUDIT_LOG` | `warehouse_audit.jsonl` | Empty string disables it |

**In production, also connect as a role with only `SELECT` grants.** This server is defense in depth, not a replacement for database permissions.

## Tests

```bash
pip install -e ".[dev]"
pytest -q
```

There are 64 tests: guard attack cases, row-filter bypass attempts, PII tracing through aliases, value-level masking, end-to-end calls through a real MCP client, and audit-log contents. Read-only and timeout behaviour are tested against the database with the guard bypassed. CI runs everything against a real PostgreSQL 16 on Python 3.10, 3.11 and 3.12.

## Limitations

- PII detection is name- and pattern-based. It won't recognise a column called `notes` that holds home addresses, so add those columns with `WAREHOUSE_PII_COLUMNS`.
- Masking follows aliases and expressions but not every path. For example, `string_agg` over a PII column is masked, while a join that reconstructs PII from non-PII columns isn't detected.
- Snowflake, BigQuery and other SQLAlchemy dialects will connect, but only Postgres and SQLite have read-only and timeout enforcement at the database level today.

## Roadmap

- [ ] Snowflake and BigQuery read-only session enforcement
- [x] Per-table row-level filters (for example `region = 'EU'` for an EU-only agent)
- [ ] Query cost estimate from `EXPLAIN` before execution, rejecting full scans over a threshold
- [ ] OpenTelemetry spans per tool call
- [ ] Streamable HTTP transport with OAuth for shared deployments
