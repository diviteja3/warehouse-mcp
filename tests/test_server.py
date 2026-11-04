"""End-to-end through a real MCP client connected in-process."""

from mcp import Client
from sqlalchemy import create_engine, text

from warehouse_mcp.db import Warehouse


async def call(server, tool, args=None):
    async with Client(server) as client:
        return await client.call_tool(tool, args or {})


async def test_tools_are_advertised_read_only(make_server):
    async with Client(make_server()) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {"list_tables", "describe_table", "run_query"}
    assert all(t.annotations.read_only_hint for t in tools.values())


async def test_list_and_describe(make_server):
    server = make_server()
    tables = (await call(server, "list_tables")).structured_content["tables"]
    assert {t["table"] for t in tables} == {"customers", "orders", "products", "support_tickets"}

    info = (await call(server, "describe_table", {"table": "orders"})).structured_content
    assert info["primary_key"] == ["id"]
    assert any(fk["references"] == "customers(id)" for fk in info["foreign_keys"])

    customers = (await call(server, "describe_table", {"table": "customers"})).structured_content
    flags = {c["name"]: c["pii_masked"] for c in customers["columns"]}
    assert flags["email"] and flags["full_name"] and not flags["region"]


async def test_describe_unknown_table(make_server):
    result = await call(make_server(), "describe_table", {"table": "nope"})
    assert result.is_error and "not found" in result.content[0].text


async def test_aggregate_query(make_server):
    result = await call(
        make_server(),
        "run_query",
        {
            "sql": "SELECT p.category, SUM(o.quantity) AS units FROM orders o JOIN products p ON p.id = o.product_id "
            "WHERE o.status = 'delivered' GROUP BY p.category ORDER BY units DESC"
        },
    )
    body = result.structured_content
    assert not result.is_error
    assert body["columns"] == ["category", "units"]
    assert body["row_count"] == 6 and body["truncated"] is False


async def test_rows_are_capped(make_server):
    body = (await call(make_server(max_rows=10), "run_query", {"sql": "SELECT id FROM orders"})).structured_content
    assert body["row_count"] == 10 and body["truncated"] is True


async def test_pii_is_masked_even_when_aliased(make_server):
    body = (
        await call(
            make_server(),
            "run_query",
            {"sql": "SELECT id, email AS contact, region FROM customers ORDER BY id LIMIT 3"},
        )
    ).structured_content
    assert body["masked_columns"] == ["contact"]
    assert all(row[1] == "***" for row in body["rows"])
    assert all(row[2] in {"NA", "EU", "APAC", "LATAM"} for row in body["rows"])


async def test_pii_in_free_text_is_masked(make_server):
    body = (
        await call(make_server(), "run_query", {"sql": "SELECT body FROM support_tickets ORDER BY id"})
    ).structured_content
    joined = " ".join(r[0] for r in body["rows"])
    assert "4111 1111 1111 1111" not in joined and "123-45-6789" not in joined
    assert "backup.mail@" not in joined
    assert "Do the trail shoes run small?" in joined


async def test_rejection_is_reported_and_audited(make_server, read_audit):
    result = await call(make_server(), "run_query", {"sql": "DELETE FROM orders"})
    assert result.is_error and "Only SELECT" in result.content[0].text

    entry = read_audit()[-1]
    assert entry["tool"] == "run_query" and entry["status"] == "rejected"


async def test_audit_never_contains_result_values(make_server, audit_path):
    await call(make_server(), "run_query", {"sql": "SELECT email FROM customers"})
    log = audit_path.read_text()
    assert "@example.com" not in log
    assert '"rows": 50' in log


async def test_database_error_is_clean(make_server):
    result = await call(make_server(), "run_query", {"sql": "SELECT no_such_column FROM orders"})
    assert result.is_error and result.content[0].text.startswith("Error executing tool run_query: Database error:")


def test_sqlite_connection_is_read_only_even_without_the_guard(demo_url):
    """Defense in depth: bypass the SQL guard entirely and the database still refuses writes."""
    wh = Warehouse(demo_url)
    try:
        wh.run("DELETE FROM orders", max_rows=1)
        raise AssertionError("write should have failed")
    except Exception as exc:
        assert "readonly" in str(exc).lower() or "read-only" in str(exc).lower()
    with create_engine(demo_url).connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM orders")).scalar() == 500


def test_sqlite_timeout(demo_url):
    wh = Warehouse(demo_url, statement_timeout_ms=50)
    slow = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 100000000) SELECT count(*) FROM n"
    try:
        wh.run(slow, max_rows=1)
        raise AssertionError("should have timed out")
    except Exception as exc:
        assert type(exc).__name__ == "QueryTimeout"
