"""Row-level filters: the agent can't write a query that reaches filtered-out rows."""

import pytest
from mcp import Client
from sqlalchemy import create_engine, text

from warehouse_mcp.row_filters import RowFilterConfigError, parse_row_filters
from warehouse_mcp.sql_guard import QueryRejected, guard

EU_ONLY = {"customers": "region = 'EU'"}


def g(sql, filters=EU_ONLY, dialect="postgres"):
    return guard(sql, dialect=dialect, max_rows=100, row_filters=parse_row_filters(filters, dialect))


# ---- rewrite ---------------------------------------------------------------


def test_table_is_replaced_by_filtered_subquery_keeping_alias():
    q = g("SELECT c.id FROM customers c WHERE c.id < 10")
    assert "(SELECT * FROM customers WHERE region = 'EU') AS c" in q.sql
    assert q.filtered_tables == ["customers"]


def test_unaliased_table_keeps_its_name_so_qualified_columns_still_work():
    q = g("SELECT customers.id FROM customers")
    assert "AS customers" in q.sql


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM orders o JOIN customers c ON c.id = o.customer_id",
        "SELECT * FROM orders WHERE customer_id IN (SELECT id FROM customers)",
        "WITH x AS (SELECT * FROM customers) SELECT * FROM x",
        "SELECT id FROM orders UNION SELECT id FROM customers",
        "SELECT (SELECT count(*) FROM customers) AS n",
        "SELECT * FROM public.customers",
        "SELECT * FROM CUSTOMERS",
    ],
)
def test_every_reference_is_filtered(sql):
    q = g(sql)
    assert "WHERE region = 'EU'" in q.sql
    assert q.sql.count("region = 'EU'") == sql.lower().count("customers")


def test_unfiltered_tables_are_untouched():
    q = g("SELECT * FROM orders")
    assert "region" not in q.sql and q.filtered_tables == []


def test_schema_qualified_key_only_matches_that_schema():
    filters = {"sales.customers": "region = 'EU'"}
    assert g("SELECT * FROM sales.customers", filters).filtered_tables == ["sales.customers"]
    assert g("SELECT * FROM crm.customers", filters).filtered_tables == []


def test_cte_shadowing_a_filtered_table_is_rejected():
    # In Postgres the inner `customers` is the real table; skipping it as a CTE
    # reference would leak every row. Fail closed instead.
    with pytest.raises(QueryRejected, match="CTE may not be named 'customers'"):
        g("WITH customers AS (SELECT * FROM customers) SELECT * FROM customers")


def test_agent_or_conditions_cannot_widen_the_filter():
    q = g("SELECT * FROM customers WHERE region = 'US' OR 1 = 1")
    # The operator's predicate sits inside the subquery, the agent's outside it.
    assert "(SELECT * FROM customers WHERE region = 'EU') AS customers WHERE region = 'US' OR 1 = 1" in q.sql


# ---- config validation -----------------------------------------------------


@pytest.mark.parametrize(
    "filters, match",
    [
        ({"customers": "region = "}, "not a valid predicate"),
        ({"customers": "id IN (SELECT id FROM vip)"}, "without subqueries"),
        ({"customers": "pg_sleep(1) IS NULL"}, "pg_sleep"),
        ({"a.b.c": "x = 1"}, "must be 'table' or 'schema.table'"),
    ],
)
def test_bad_config_fails_at_startup(filters, match):
    with pytest.raises(RowFilterConfigError, match=match):
        parse_row_filters(filters, "postgres")


# ---- end to end through MCP, against real rows ------------------------------


async def run(server, sql):
    async with Client(server) as client:
        return await client.call_tool("run_query", {"sql": sql})


def regions(demo_url):
    with create_engine(demo_url).connect() as conn:
        return dict(conn.execute(text("SELECT region, count(*) FROM customers GROUP BY region")).all())


async def test_end_to_end_agent_sees_only_eu_rows(make_server, demo_url, read_audit):
    counts = regions(demo_url)
    assert len(counts) > 1 and counts["EU"] < sum(counts.values())

    server = make_server(row_filters=EU_ONLY)
    body = (await run(server, "SELECT region, count(*) AS n FROM customers GROUP BY region")).structured_content
    assert body["rows"] == [["EU", counts["EU"]]]
    assert body["filtered_tables"] == ["customers"]

    # A join from an unfiltered table still only reaches EU customers.
    joined = (
        await run(server, "SELECT DISTINCT c.region FROM orders o JOIN customers c ON c.id = o.customer_id")
    ).structured_content
    assert joined["rows"] == [["EU"]]

    assert read_audit()[-1]["filtered_tables"] == ["customers"]


async def test_end_to_end_without_filters_sees_everything(make_server, demo_url):
    body = (await run(make_server(), "SELECT count(*) FROM customers")).structured_content
    assert body["rows"][0][0] == sum(regions(demo_url).values())
    assert body["filtered_tables"] == []
