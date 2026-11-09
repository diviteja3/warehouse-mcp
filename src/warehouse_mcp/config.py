from __future__ import annotations

import json
import os
from dataclasses import dataclass

from warehouse_mcp.masking import DEFAULT_PII_COLUMNS


def _csv(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


@dataclass(frozen=True)
class Settings:
    url: str
    max_rows: int = 200
    statement_timeout_ms: int = 5000
    allowed_schemas: frozenset[str] | None = None
    pii_columns: tuple[str, ...] = DEFAULT_PII_COLUMNS
    audit_log: str | None = "warehouse_audit.jsonl"
    row_filters: dict[str, str] | None = None  # {"table" or "schema.table": "SQL predicate"}

    @classmethod
    def from_env(cls) -> Settings:
        url = os.getenv("WAREHOUSE_URL")
        if not url:
            raise SystemExit(
                "Set WAREHOUSE_URL, e.g. postgresql+psycopg://readonly@localhost/analytics "
                "or sqlite:///examples/demo.db"
            )
        schemas = _csv(os.getenv("WAREHOUSE_ALLOWED_SCHEMAS"))
        extra_pii = _csv(os.getenv("WAREHOUSE_PII_COLUMNS"))
        row_filters = None
        if os.getenv("WAREHOUSE_ROW_FILTERS"):
            try:
                row_filters = json.loads(os.environ["WAREHOUSE_ROW_FILTERS"])
            except json.JSONDecodeError as exc:
                raise SystemExit(f"WAREHOUSE_ROW_FILTERS is not valid JSON: {exc}") from None
            if not isinstance(row_filters, dict) or not all(isinstance(v, str) for v in row_filters.values()):
                raise SystemExit('WAREHOUSE_ROW_FILTERS must be a JSON object like {"customers": "region = \'EU\'"}')
        return cls(
            url=url,
            max_rows=int(os.getenv("WAREHOUSE_MAX_ROWS", "200")),
            statement_timeout_ms=int(os.getenv("WAREHOUSE_TIMEOUT_MS", "5000")),
            allowed_schemas=frozenset(s.lower() for s in schemas) or None,
            pii_columns=DEFAULT_PII_COLUMNS + tuple(extra_pii),
            audit_log=os.getenv("WAREHOUSE_AUDIT_LOG", "warehouse_audit.jsonl") or None,
            row_filters=row_filters,
        )
