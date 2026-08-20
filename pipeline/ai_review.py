"""AI semantic review of fields routed to the review queue.

Complements the explicit rule table in `validate.py`: for every field that
could not be auto-published, this stage produces a human-readable explanation
and a suggested next action. Like the extraction stack it has two
implementations:

  * `MockAIReviewer`  — deterministic, template-driven, works fully offline
    (no API key), so the demo and tests always exercise the contract.
  * `LLMAIReviewer`   — real LLM (Claude tool-calling / OpenAI JSON schema)
    used when ANTHROPIC_API_KEY / OPENAI_API_KEY is set.

Both return the same shape: `{mode, provider_note, reviews, summary,
fields_reviewed, all_clear}`. Reviews never change field values or statuses —
they only explain and suggest, so the routing guarantees (never auto-publish
a conflicted/low-confidence field) are untouched.
"""
from __future__ import annotations

import json

from .llm import _call_anthropic, _call_openai, pick_provider

VERIFIED_THRESHOLD = 0.75
TOLERANCE_NOTE = "1% (relative, with a 0.05 absolute floor)"

# JSON Schema for the LLM review response.
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "reviews": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "review": {"type": "string"},
                    "suggested_action": {"type": "string"},
                },
                "required": ["field", "review", "suggested_action"],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["reviews", "summary"],
    "additionalProperties": False,
}


def _source_label(sid: str, source_infos: list[dict]) -> str:
    for s in source_infos:
        if s["id"] == sid:
            return s["title"]
    return sid


def _mock_review(queue: list[dict], ctx: dict) -> dict:
    authority: dict = ctx.get("authority", {})
    infos: list[dict] = ctx.get("source_infos", [])
    reviews: list[dict] = []
    n_conflict = n_low = n_valid = 0

    for q in queue:
        flags = q["flags"]
        if "conflict" in flags:
            n_conflict += 1
            srcs = ", ".join(
                f"{_source_label(s, infos)} (authority {authority.get(s, 0.8):.2f})"
                for s in q["sources"]
            )
            review = (
                f"Cross-source conflict on {q['label']}: {q['reason']}. The values "
                f"disagree beyond the {TOLERANCE_NOTE} agreement tolerance, so neither "
                f"can be auto-published without human judgment."
            )
            action = (
                f"Check the highest-authority source ({srcs}) for a typo or a different "
                f"operating condition, then record the single correct value."
            )
        elif "validation" in flags:
            n_valid += 1
            review = (
                f"{q['label']} failed a validation rule: {q['reason']}. The value is "
                f"physically implausible or not in the schema enum."
            )
            action = (
                "Correct the value at the source, or confirm it is genuinely correct "
                "before overriding the rule."
            )
        else:  # low_confidence / single_source
            n_low += 1
            review = (
                f"{q['label']} is backed by only {len(q['sources'])} source(s) with "
                f"effective confidence {q['confidence']:.2f}, below the "
                f"{VERIFIED_THRESHOLD:.2f} verification threshold. With no second "
                f"independent source there is no cross-check."
            )
            action = (
                f"Confirm {q['label']} from an additional independent source to raise "
                f"confidence above {VERIFIED_THRESHOLD:.2f}."
            )
        reviews.append({
            "field": q["field"],
            "label": q["label"],
            "flags": list(flags),
            "confidence": q["confidence"],
            "value": q["value"],
            "review": review,
            "suggested_action": action,
        })

    if queue:
        summary = (
            f"Reviewed {len(queue)} field(s): {n_conflict} conflict(s), {n_low} "
            f"low-confidence, {n_valid} validation. None can auto-publish until resolved."
        )
    else:
        summary = "No fields were routed to review - every extracted field was verified."
    return {
        "mode": "mock",
        "provider_note": "deterministic template review (offline)",
        "reviews": reviews,
        "summary": summary,
        "fields_reviewed": len(queue),
        "all_clear": not queue,
    }


class MockAIReviewer:
    """Offline, deterministic reviewer (mirrors the LLM output contract)."""

    def review(self, queue: list[dict], ctx: dict) -> dict:
        return _mock_review(queue, ctx)


class LLMAIReviewer:
    """Real LLM reviewer (Claude tool-calling / OpenAI JSON schema)."""

    def __init__(self, provider: str):
        self.provider = provider

    def review(self, queue: list[dict], ctx: dict) -> dict:
        if not queue:
            return _mock_review(queue, ctx)  # nothing to review — same shape
        payload = [
            {
                "field": q["field"],
                "label": q["label"],
                "flags": q["flags"],
                "confidence": round(q["confidence"], 3),
                "value": q["value"],
                "why_flagged": q["reason"],
                "sources": q["sources"],
            }
            for q in queue
        ]
        prompt = (
            "You are a product-data reviewer. For each field below that was routed to a "
            "human review queue, write a concise natural-language explanation of why it "
            "cannot be auto-published and a concrete suggested next action. Never change "
            "any value; never claim a field is verified. Return the JSON per the schema.\n\n"
            + json.dumps({"fields_flagged": payload}, indent=2, ensure_ascii=False)
        )
        if self.provider == "anthropic":
            data = _call_anthropic(prompt, REVIEW_SCHEMA)
        else:
            data = _call_openai(prompt, REVIEW_SCHEMA)

        by_field = {q["field"]: q for q in queue}
        reviews = []
        items = data.get("reviews") if isinstance(data, dict) else None
        if isinstance(items, list):
            for it in items:
                if not isinstance(it, dict):
                    continue
                fld = it.get("field")
                if not isinstance(fld, str) or fld not in by_field:
                    continue  # drop hallucinated fields
                q = by_field[fld]
                reviews.append({
                    "field": fld,
                    "label": q["label"],
                    "flags": list(q["flags"]),
                    "confidence": q["confidence"],
                    "value": q["value"],
                    "review": str(it.get("review", "")),
                    "suggested_action": str(it.get("suggested_action", "")),
                })
        return {
            "mode": self.provider,
            "provider_note": f"{self.provider} semantic review",
            "reviews": reviews,
            "summary": data.get("summary", "") if isinstance(data, dict) else "",
            "fields_reviewed": len(reviews),
            "all_clear": not queue,
        }


def build_ai_reviewer(mode: str = "auto"):
    """Return a reviewer with .review(queue, ctx) -> dict."""
    provider = pick_provider(mode)
    if provider in ("anthropic", "openai"):
        return LLMAIReviewer(provider)
    return MockAIReviewer()
