import pytest

from warehouse_mcp.masking import PiiPolicy
from warehouse_mcp.sql_guard import QueryRejected, guard

PII = PiiPolicy().is_pii_column


def g(sql, **kw):
    return guard(sql, dialect="postgres", max_rows=100, is_pii=PII, **kw)


@pytest.mark.parametrize(
    "sql, reason",
    [
        ("DELETE FROM orders", "Only SELECT"),
        ("UPDATE orders SET status = 'x'", "Only SELECT"),
        ("DROP TABLE orders", "Only SELECT"),
        ("SELECT 1; DROP TABLE orders", "exactly one statement"),
        ("WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d", "DELETE"),
        ("SELECT * INTO copy_of_orders FROM orders", "INTO"),
        ("SELECT * FROM orders FOR UPDATE", "LOCK"),
        ("SELECT pg_sleep(30)", "pg_sleep"),
        ("SELECT nextval('orders_id_seq')", "nextval"),
        ("SELECT pg_read_file('/etc/passwd')", "pg_read_file"),
        ("SELECT * FROM dblink('host=evil', 'select 1') AS t(a int)", "dblink"),
        ("COPY orders TO '/tmp/out.csv'", "Only SELECT"),
        ("SET statement_timeout = 0", "Only SELECT"),
        ("SELECT * FROM pg_catalog.pg_shadow", "System catalog"),
        ("SELECT * FROM information_schema.tables", "System catalog"),
        ("   ", "Empty"),
        ("SELECT * FROM (", "parse"),
        ("SELEC * FRM x", "Only SELECT"),  # typo parses as an alias: still rejected
    ],
)
def test_rejects(sql, reason):
    with pytest.raises(QueryRejected, match=reason):
        g(sql)


def test_comment_and_case_tricks_do_not_hide_writes():
    with pytest.raises(QueryRejected):
        g("/* harmless */ sElEcT 1; -- \n dElEtE FROM orders")


def test_allowed_schema_list():
    g("SELECT * FROM analytics.orders", allowed_schemas=frozenset({"analytics"}))
    with pytest.raises(QueryRejected, match="not in the allowed list"):
        g("SELECT * FROM hr.salaries", allowed_schemas=frozenset({"analytics"}))


def test_row_cap_wraps_query_and_keeps_inner_limit():
    q = g("SELECT id FROM orders ORDER BY id DESC LIMIT 5")
    assert q.sql.endswith("LIMIT 101")
    assert "LIMIT 5" in q.sql


def test_union_and_cte_are_allowed():
    q = g("WITH recent AS (SELECT * FROM orders) SELECT id FROM recent UNION SELECT id FROM products")
    assert q.tables == ["orders", "products"]  # CTE name is not reported as a table


@pytest.mark.parametrize(
    "sql, flagged",
    [
        ("SELECT email FROM customers", {0}),
        ("SELECT id, email AS contact FROM customers", {1}),
        ("SELECT upper(full_name) AS n, region FROM customers", {0}),
        ("SELECT region, count(email) FROM customers GROUP BY region", set()),
        ("SELECT region FROM customers", set()),
    ],
)
def test_pii_tracking_through_aliases(sql, flagged):
    assert g(sql).pii_outputs == flagged
