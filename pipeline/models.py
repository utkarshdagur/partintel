"""Core data models for the product-intelligence pipeline.

Every object the UI needs for explainability lives here:
per-source extracted values with exact citations, merged fields with
confidence + status, conflicts, validation issues, and the final record.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any, Optional


@dataclass
class FieldValue:
    """One extracted value from one source, with its exact source span."""

    field: str
    value: Any                      # exact text as written in the source
    unit: Optional[str]
    confidence: float               # 0-1, extraction-level confidence
    method: str                     # table_parse | llm_inference | ocr_heuristic
    source_id: str
    snippet: str                    # verbatim source span
    line_start: int
    line_end: int
    reasoning: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class SourceExtraction:
    """All fields extracted from a single source."""

    source_id: str
    fields: list[FieldValue] = field(default_factory=list)


@dataclass
class Conflict:
    """Two or more sources disagree on the same field."""

    field: str
    reason: str
    values: list[dict] = field(default_factory=list)  # [{source_id, value, unit}]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MergedField:
    """A field after cross-source merge, with confidence + routing status."""

    field: str
    label: str
    unit: Optional[str]
    value: Any                     # normalized merged value (None if unresolved conflict)
    confidence: float              # 0-1 effective field confidence
    status: str                    # verified | needs_review
    flags: list[str]               # conflict | single_source | multi_source | low_confidence | validation
    sources: list[FieldValue]      # per-source extractions that CONTRIBUTED to the merge (explainability)
    conflicts: list[Conflict]
    reasoning: str
    dropped: list[FieldValue] = field(default_factory=list)  # unparseable extractions, kept for audit only
    validation_issues: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["sources"] = [fv.to_dict() for fv in self.sources]
        d["dropped"] = [fv.to_dict() for fv in self.dropped]
        d["conflicts"] = [c.to_dict() for c in self.conflicts]
        return d


@dataclass
class ValidationIssue:
    """A failed plausibility rule (small rule table, not ML)."""

    field: str
    rule_id: str
    severity: str                  # error | warning
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProductRecord:
    """The final structured product record (what the demo UI renders)."""

    product_id: str
    category: str
    category_label: str
    pipeline: dict                # mode, provider, version, run_at
    sources: list[dict]           # source metadata + line counts + extraction counts
    fields: list[MergedField]
    validation: dict              # {passed, rules_run, issues: [ValidationIssue dicts]}
    needs_review_queue: list[dict]
    marketing: dict               # {description, citations, sentences_skipped}
    summary: dict                 # counters for the UI header
    ai_review: Optional[dict] = None  # semantic review of needs_review fields

    def to_dict(self) -> dict:
        return {
            "product_id": self.product_id,
            "category": self.category,
            "category_label": self.category_label,
            "pipeline": self.pipeline,
            "sources": self.sources,
            "fields": [f.to_dict() for f in self.fields],
            "validation": {
                **self.validation,
                "issues": [i.to_dict() for i in self.validation["issues"]],
            },
            "needs_review_queue": self.needs_review_queue,
            "marketing": self.marketing,
            "summary": self.summary,
            "ai_review": self.ai_review,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)
