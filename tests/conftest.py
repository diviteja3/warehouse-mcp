import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "examples"))
from seed_demo import seed  # noqa: E402

from warehouse_mcp.config import Settings  # noqa: E402
from warehouse_mcp.server import create_server  # noqa: E402


@pytest.fixture
def demo_url(tmp_path):
    url = f"sqlite:///{tmp_path / 'demo.db'}"
    seed(url)
    return url


@pytest.fixture
def audit_path(tmp_path):
    return tmp_path / "audit.jsonl"


@pytest.fixture
def make_server(demo_url, audit_path):
    def _make(**overrides):
        settings = Settings(url=demo_url, audit_log=str(audit_path), **overrides)
        return create_server(settings)

    return _make


@pytest.fixture
def read_audit(audit_path):
    def _read():
        return [json.loads(line) for line in audit_path.read_text().splitlines()]

    return _read
