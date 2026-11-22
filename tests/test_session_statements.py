import pytest

from warehouse_mcp.db import is_timeout_error, session_statements


def test_postgres_statements():
    assert session_statements("postgresql", 500) == ["SET TRANSACTION READ ONLY", "SET LOCAL statement_timeout = 500"]


@pytest.mark.parametrize("ms,secs", [(5000, 5), (1, 1), (1500, 2), (0, 1)])
def test_snowflake_timeout_rounds_up_to_whole_seconds(ms, secs):
    assert session_statements("snowflake", ms) == [f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {secs}"]


def test_other_dialects_have_no_statements():
    assert session_statements("sqlite", 5000) == []
    assert session_statements("bigquery", 5000) == []


def test_timeout_error_detection():
    assert is_timeout_error("snowflake", Exception("Statement reached its statement or warehouse timeout of 5 s"))
    assert is_timeout_error("postgresql", Exception("canceling statement due to statement timeout"))
    assert is_timeout_error("sqlite", Exception("interrupted"))
    assert not is_timeout_error("snowflake", Exception("syntax error"))
