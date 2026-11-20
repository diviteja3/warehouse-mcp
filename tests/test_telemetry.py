import pytest

pytest.importorskip("opentelemetry.sdk")

from opentelemetry import trace  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

from .test_server import call  # noqa: E402

_exporter = InMemorySpanExporter()
_provider = TracerProvider()
_provider.add_span_processor(SimpleSpanProcessor(_exporter))
trace.set_tracer_provider(_provider)


def ours():
    return [s for s in _exporter.get_finished_spans() if s.name.startswith("warehouse_mcp.")]


@pytest.fixture(autouse=True)
def clear():
    _exporter.clear()


async def test_run_query_emits_span_without_sql(make_server):
    await call(make_server(), "run_query", {"sql": "SELECT id FROM customers LIMIT 2"})
    (span,) = ours()
    assert span.name == "warehouse_mcp.run_query"
    assert span.attributes["mcp.tool"] == "run_query"
    assert span.attributes["warehouse.status"] == "ok"
    assert span.attributes["warehouse.row_count"] == 2
    assert span.attributes["warehouse.truncated"] is False
    assert "customers" not in str(dict(span.attributes))


async def test_rejected_query_marks_span_error(make_server):
    result = await call(make_server(), "run_query", {"sql": "DELETE FROM orders"})
    assert result.is_error
    (span,) = ours()
    assert span.attributes["warehouse.status"] == "error"
    assert span.status.status_code == trace.StatusCode.ERROR
    assert "DELETE" not in (span.status.description or "")


async def test_other_tools_traced(make_server):
    await call(make_server(), "list_tables")
    assert [s.name for s in ours()] == ["warehouse_mcp.list_tables"]
