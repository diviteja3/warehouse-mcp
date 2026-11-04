"""Static checks that run before any SQL reaches the database.

The guard parses the query instead of pattern-matching strings, so comments,
casing and whitespace tricks don't help an attacker. It accepts exactly one
read-only query and reports *why* it rejects anything else, so an agent can
correct itself.

This is the first of several layers. The database connection is also opened
read-only with a statement timeout (see db.py), so a parser gap alone is not
enough to change data.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

# Nodes that write, change session state or lock rows: rejected anywhere in
# the tree, including inside CTEs and subqueries.
FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = tuple(
    getattr(exp, name)
    for name in (
        "Insert",
        "Update",
        "Delete",
        "Merge",
        "Create",
        "Drop",
        "Alter",
        "TruncateTable",
        "Copy",
        "Command",
        "Into",
        "Lock",
        "Set",
        "Pragma",
        "Transaction",
        "Commit",
        "Rollback",
        "Grant",
        "Revoke",
        "Use",
        "Kill",
        "Analyze",
    )
    if hasattr(exp, name)
)

# Functions with side effects or access outside the query's tables.
FORBIDDEN_FUNCTIONS = frozenset(
    {
        "pg_sleep",
        "pg_sleep_for",
        "pg_sleep_until",
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_stat_file",
        "lo_import",
        "lo_export",
        "lo_unlink",
        "dblink",
        "dblink_exec",
        "dblink_connect",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_reload_conf",
        "pg_advisory_lock",
        "pg_advisory_xact_lock",
        "pg_try_advisory_lock",
        "set_config",
        "nextval",
        "setval",
        "load_extension",
        "readfile",
        "writefile",  # sqlite
    }
)

BLOCKED_SCHEMAS = frozenset({"pg_catalog", "information_schema"})
BLOCKED_TABLES = frozenset({"sqlite_master", "sqlite_schema", "sqlite_temp_master"})


class QueryRejected(ValueError):
    """Raised with a message that is safe and useful to show the agent."""


@dataclass
class GuardedQuery:
    original: str
    sql: str  # what will actually run (row-capped)
    tables: list[str]  # tables referenced, as schema.table or table
    pii_outputs: set[int] = field(default_factory=set)  # output positions derived from PII columns


def _function_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.this).lower()
    return node.sql_name().lower()


def _cte_names(tree: exp.Expression) -> set[str]:
    return {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}


def _pii_outputs(tree: exp.Query, is_pii) -> set[int]:
    """Output positions whose value is derived from a PII column.

    Follows expressions, so `email AS contact` and `lower(email)` are caught.
    A bare COUNT over a PII column reveals no values and is left alone.
    """
    first = tree
    while isinstance(first, exp.SetOperation):
        first = first.left
    if not isinstance(first, exp.Select):
        return set()

    flagged = set()
    for i, projection in enumerate(first.expressions):
        target = projection.unalias()
        if isinstance(target, exp.Count):
            continue
        if any(is_pii(col.name) for col in target.find_all(exp.Column)):
            flagged.add(i)
    return flagged


def guard(
    sql: str,
    *,
    dialect: str,
    max_rows: int,
    allowed_schemas: frozenset[str] | None = None,
    is_pii=lambda name: False,
) -> GuardedQuery:
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        raise QueryRejected("Empty query.")

    try:
        trees = [t for t in sqlglot.parse(sql, read=dialect) if t is not None]
    except ParseError as exc:
        raise QueryRejected(f"Could not parse SQL: {exc.errors[0]['description'] if exc.errors else exc}") from None

    if len(trees) != 1:
        raise QueryRejected("Send exactly one statement per call.")
    tree = trees[0]

    if not isinstance(tree, exp.Query):
        raise QueryRejected(f"Only SELECT queries are allowed (got {type(tree).__name__.upper()}).")

    for node in tree.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise QueryRejected(f"{type(node).__name__.upper()} is not allowed in a read-only query.")
        if isinstance(node, exp.Func) and _function_name(node) in FORBIDDEN_FUNCTIONS:
            raise QueryRejected(f"Function {_function_name(node)}() is not allowed.")

    ctes = _cte_names(tree)
    tables = []
    for table in tree.find_all(exp.Table):
        name, schema = table.name.lower(), (table.db or "").lower()
        if not schema and name in ctes:
            continue
        if schema in BLOCKED_SCHEMAS or name in BLOCKED_TABLES:
            raise QueryRejected(f"System catalog {schema or name} is not queryable; use list_tables/describe_table.")
        if allowed_schemas is not None and schema and schema not in allowed_schemas:
            raise QueryRejected(f"Schema {schema!r} is not in the allowed list: {sorted(allowed_schemas)}.")
        tables.append(f"{schema}.{name}" if schema else name)

    # Cap rows by wrapping, so ORDER BY / LIMIT / UNION inside the query keep
    # their meaning. Fetching one extra row tells us whether we truncated.
    capped = exp.select("*").from_(tree.copy().subquery("_q")).limit(max_rows + 1)

    return GuardedQuery(
        original=sql,
        sql=capped.sql(dialect=dialect),
        tables=sorted(set(tables)),
        pii_outputs=_pii_outputs(tree, is_pii),
    )
