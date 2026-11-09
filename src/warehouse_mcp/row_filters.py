"""Row-level filters: an agent only ever sees the rows it is allowed to see.

An operator configures a predicate per table, for example
``{"customers": "region = 'EU'"}``. Before a query runs, every reference to
``customers`` (in the main query, joins, subqueries and CTE bodies) is replaced
with ``(SELECT * FROM customers WHERE region = 'EU') AS customers``. The
agent's SQL is otherwise untouched, so its own filters, joins and aliases keep
working, but there is no way to write a query that reaches the other rows.

Filters are matched on the parsed AST, not on strings, and the rewrite fails
closed: a CTE that shadows a filtered table's name is rejected instead of
guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError


class RowFilterConfigError(ValueError):
    """A configured filter is not a valid, read-only predicate."""


@dataclass(frozen=True)
class RowFilter:
    schema: str | None  # None matches the table name in any schema
    table: str
    predicate: str  # as configured, for docs and audit

    def matches(self, table: exp.Table) -> bool:
        if table.name.lower() != self.table:
            return False
        return self.schema is None or (table.db or "").lower() == self.schema


def parse_row_filters(raw: dict[str, str], dialect: str) -> tuple[RowFilter, ...]:
    """Validate operator config once at startup, so a typo fails fast instead of per query."""
    # Imported here to avoid a cycle: sql_guard imports this module.
    from warehouse_mcp.sql_guard import FORBIDDEN_FUNCTIONS, FORBIDDEN_NODES, _function_name

    filters = []
    for key, predicate in raw.items():
        parts = [p.strip().lower() for p in key.split(".")]
        if len(parts) > 2 or not all(parts):
            raise RowFilterConfigError(f"Row filter key {key!r} must be 'table' or 'schema.table'.")
        try:
            condition = sqlglot.parse_one(predicate, read=dialect, into=exp.Condition)
        except ParseError as exc:
            raise RowFilterConfigError(f"Row filter for {key!r} is not a valid predicate: {exc}") from None
        for node in condition.walk():
            if isinstance(node, FORBIDDEN_NODES) or isinstance(node, exp.Query):
                raise RowFilterConfigError(f"Row filter for {key!r} must be a plain condition, without subqueries.")
            if isinstance(node, exp.Func) and _function_name(node) in FORBIDDEN_FUNCTIONS:
                raise RowFilterConfigError(f"Row filter for {key!r} uses forbidden function {_function_name(node)}().")
        schema, table = (parts[0], parts[1]) if len(parts) == 2 else (None, parts[0])
        filters.append(RowFilter(schema, table, predicate))
    return tuple(filters)


def apply_row_filters(tree: exp.Query, filters: tuple[RowFilter, ...], cte_names: set[str], dialect: str) -> list[str]:
    """Rewrite `tree` in place. Returns the filtered tables, for the response and the audit log."""
    if not filters:
        return []

    shadowed = {f.table for f in filters} & cte_names
    if shadowed:
        name = sorted(shadowed)[0]
        raise_rejected(f"A CTE may not be named {name!r}, because that table has a row filter. Rename the CTE.")

    # Collect first, then replace, so the subqueries we insert aren't visited again.
    targets = []
    for table in tree.find_all(exp.Table):
        if not table.db and table.name.lower() in cte_names:
            continue
        rule = next((f for f in filters if f.matches(table)), None)
        if rule:
            targets.append((table, rule))

    applied = set()
    for table, rule in targets:
        alias = table.alias or table.name
        base = exp.Table(this=table.this.copy(), db=table.args["db"].copy() if table.args.get("db") else None)
        condition = sqlglot.parse_one(rule.predicate, read=dialect, into=exp.Condition)
        table.replace(exp.select("*").from_(base).where(condition).subquery(alias))
        applied.add(f"{table.db.lower()}.{table.name.lower()}" if table.db else table.name.lower())
    return sorted(applied)


def raise_rejected(message: str) -> None:
    from warehouse_mcp.sql_guard import QueryRejected

    raise QueryRejected(message)
