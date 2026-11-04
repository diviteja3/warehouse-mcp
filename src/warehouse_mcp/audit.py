"""Append-only JSONL audit trail: who asked what, and what happened.

Result values are never written, only counts, so the log itself does not
become a copy of the data.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any


class AuditLog:
    def __init__(self, path: str | None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()

    def record(self, tool: str, **fields: Any) -> None:
        if self.path is None:
            return
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "tool": tool, **fields}
        line = json.dumps(entry, default=str)
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
