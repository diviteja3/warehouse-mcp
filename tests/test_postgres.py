"""Runs against a real PostgreSQL when WAREHOUSE_TEST_PG_URL is set (CI starts one)."""

import os
import sys
from pathlib import Path

import pytest

PG_URL = os.getenv("WAREHOUSE_TEST_PG_URL")
pytestmark = [pytest.mark.postgres, pytest.mark.skipif(not PG_URL, reason="WAREHOUSE_TEST_PG_URL not set")]


@pytest.fixture(scope="module")
def pg():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "examples"))
    from seed_demo import seed

    from warehouse_mcp.db import Warehouse

    seed(PG_URL)
    return Warehouse(PG_URL, statement_timeout_ms=500)


def test_read_only_transaction_blocks_writes_without_the_guard(pg):
    with pytest.raises(Exception, match="read-only transaction"):
        pg.run("DELETE FROM orders", max_rows=1)
    assert pg.run("SELECT count(*) FROM orders", max_rows=1).rows == [[500]]


def test_statement_timeout(pg):
    from warehouse_mcp.db import QueryTimeout

    with pytest.raises(QueryTimeout):
        pg.run("SELECT pg_sleep(5)", max_rows=1)  # the guard would block this; here we test the DB layer


def test_list_tables_skips_system_schemas(pg):
    schemas = {t["schema"] for t in pg.list_tables()}
    assert "pg_catalog" not in schemas and "public" in schemas
