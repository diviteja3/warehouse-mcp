"""PII masking applied to every result before it leaves the server.

Two independent checks:
1. Column-level: output columns named like PII, or derived from a PII column
   (the SQL guard traces this through aliases and expressions), are masked.
2. Value-level: any string that looks like an email, SSN or card number is
   masked wherever it appears, catching PII stored in free-text columns.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

DEFAULT_PII_COLUMNS = (
    "email",
    "phone",
    "ssn",
    "social_security",
    "tax_id",
    "passport",
    "first_name",
    "last_name",
    "full_name",
    "address",
    "street",
    "dob",
    "date_of_birth",
    "card_number",
    "iban",
    "password",
)

EMAIL = re.compile(r"\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
SSN = re.compile(r"\b\d{3}-\d{2}-(\d{4})\b")
CARD = re.compile(r"\b(?:\d[ -]?){12}(\d{4})\b")

MASK = "***"


class PiiPolicy:
    def __init__(self, column_patterns: Iterable[str] = DEFAULT_PII_COLUMNS):
        self.patterns = tuple(p.lower() for p in column_patterns if p)

    def is_pii_column(self, name: str) -> bool:
        lowered = name.lower()
        return any(p in lowered for p in self.patterns)

    @staticmethod
    def mask_text(value: str) -> str:
        value = EMAIL.sub(lambda m: f"{m.group(1)}***@{m.group(2)}", value)
        value = SSN.sub(lambda m: f"***-**-{m.group(1)}", value)
        return CARD.sub(lambda m: f"****-****-****-{m.group(1)}", value)

    def apply(
        self, columns: list[str], rows: list[list[Any]], derived_pii: set[int]
    ) -> tuple[list[list[Any]], list[str]]:
        """Return (masked rows, names of fully masked columns)."""
        masked_idx = {i for i, c in enumerate(columns) if self.is_pii_column(c)} | {
            i for i in derived_pii if i < len(columns)
        }
        out = []
        for row in rows:
            new = []
            for i, value in enumerate(row):
                if i in masked_idx and value is not None:
                    new.append(MASK)
                elif isinstance(value, str):
                    new.append(self.mask_text(value))
                else:
                    new.append(value)
            out.append(new)
        return out, [columns[i] for i in sorted(masked_idx)]
