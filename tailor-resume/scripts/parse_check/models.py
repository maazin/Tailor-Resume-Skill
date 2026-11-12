"""Parse-check report schema.

Modified from Resume-Matcher (https://github.com/srbhr/Resume-Matcher),
apps/backend/app/services/ats_parse/models.py, Apache-2.0.
Changes: Pydantic models replaced with dataclasses (one less dependency);
the report records which extractor produced it.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

SCHEMA_VERSION = "1.0"

Category = Literal["extraction", "layout", "content"]
Severity = Literal["high", "medium", "low"]
Status = Literal["pass", "warn", "fail"]
FieldStatus = Literal["found", "garbled", "missing", "hidden"]


@dataclass
class Check:
    id: str
    category: str
    severity: str
    status: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class RoundTripField:
    field: str
    status: str
    score: float


@dataclass
class RoundTrip:
    """Self-consistency of our own render, not real-ATS accuracy."""

    content_recall: float
    order_fidelity: float
    truncated: bool = False
    fields: list[RoundTripField] = field(default_factory=list)


@dataclass
class ParseCheckReport:
    extractor: str
    extractability: str
    overall_score: int
    content_score: int
    page_count: int | None
    checks: list[Check]
    roundtrip: RoundTrip | None = None
    extracted_text_preview: str = ""
    schema_version: str = SCHEMA_VERSION

    def check(self, check_id: str) -> Check | None:
        return next((c for c in self.checks if c.id == check_id), None)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
