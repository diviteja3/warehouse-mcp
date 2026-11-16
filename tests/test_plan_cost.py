"""EXPLAIN-based cost limit, tested with a stubbed estimate (no PostgreSQL needed)."""

import json

import pytest
from mcp import Client

from warehouse_mcp.config import Settings
from warehouse_mcp.db import PlanEstimate, Warehouse, parse_pg_plan
from warehouse_mcp.server import create_server


class FakeCostWarehouse(Warehouse):
    def __init__(self, url, cost):
        super().__init__(url)
        self.cost = cost
        self.ran = False

    def estimate(self, sql):
        return PlanEstimate(self.cost, 1234)

    def run(self, sql, max_rows):
        self.ran = True
        return super().run(sql, max_rows)


def _server(demo_url, audit_path, cost, limit):
    wh = FakeCostWarehouse(demo_url, cost)
    settings = Settings(url=demo_url, audit_log=str(audit_path), max_plan_cost=limit)
    return create_server(settings, wh), wh


async def _query(server):
    async with Client(server) as client:
        return await client.call_tool("run_query", {"sql": "SELECT count(*) FROM orders"})


def test_parse_pg_plan_reads_root_node():
    raw = [{"Plan": {"Node Type": "Seq Scan", "Total Cost": 155.5, "Plan Rows": 10000}}]
    assert parse_pg_plan(raw) == PlanEstimate(155.5, 10000.0)
    assert parse_pg_plan(json.dumps(raw)) == PlanEstimate(155.5, 10000.0)


async def test_expensive_query_is_rejected_before_running(demo_url, audit_path, read_audit):
    server, wh = _server(demo_url, audit_path, cost=50_000, limit=1000)
    result = await _query(server)
    assert result.is_error
    assert "plan cost 50000 exceeds the limit of 1000" in result.content[0].text
    assert not wh.ran
    entry = read_audit()[-1]
    assert entry["status"] == "rejected" and entry["plan_cost"] == 50_000


async def test_cheap_query_runs(demo_url, audit_path):
    server, wh = _server(demo_url, audit_path, cost=10, limit=1000)
    result = await _query(server)
    assert not result.is_error and wh.ran


async def test_limit_is_ignored_on_sqlite(make_server):
    result = await _query(make_server(max_plan_cost=1))
    assert not result.is_error


def test_env_validation(monkeypatch):
    monkeypatch.setenv("WAREHOUSE_URL", "sqlite://")
    monkeypatch.setenv("WAREHOUSE_MAX_PLAN_COST", "2500")
    assert Settings.from_env().max_plan_cost == 2500
    monkeypatch.setenv("WAREHOUSE_MAX_PLAN_COST", "abc")
    with pytest.raises(SystemExit):
        Settings.from_env()
