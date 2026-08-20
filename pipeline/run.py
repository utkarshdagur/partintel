"""Orchestrator: sources -> per-source extraction -> merge -> validate ->
route -> marketing, producing the final ProductRecord JSON.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Optional

from . import ai_review as ai_review_mod
from . import marketing as marketing_mod
from . import merge as merge_mod
from . import validate as validate_mod
from .llm import build_extractor, load_prompt_template, pick_provider
from .models import ProductRecord
from .sources import read_source

BASE_DIR = Path(__file__).resolve().parent.parent


def _default_path(name: str) -> Path:
    return BASE_DIR / name


def run_pipeline(
    manifest_path: Optional[str | Path] = None,
    output_path: Optional[str | Path] = None,
    schema_path: Optional[str | Path] = None,
    prompt_path: Optional[str | Path] = None,
    mode: str = "auto",
) -> ProductRecord:
    """Run the full pipeline. `mode`: auto | mock | anthropic | openai."""
    manifest_path = Path(manifest_path) if manifest_path else _default_path("data/sample/sources.json")
    schema_path = Path(schema_path) if schema_path else _default_path("schema/bearing_schema.json")
    prompt_path = Path(prompt_path) if prompt_path else _default_path("prompts/extraction_prompt.md")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    template = load_prompt_template(str(prompt_path))

    provider = pick_provider(mode)
    extractor = build_extractor(mode, template)

    # --- 1. per-source extraction ------------------------------------------
    authority: dict[str, float] = {}
    source_infos: list[dict] = []
    by_field: dict[str, list] = {}

    for src in manifest["sources"]:
        blocks, _ = read_source(src)
        authority[src["id"]] = float(src.get("authority", 0.8))
        extraction = extractor.extract(src, blocks, schema)
        for fv in extraction.fields:
            by_field.setdefault(fv.field, []).append(fv)
        source_infos.append({
            "id": src["id"],
            "type": src["type"],
            "title": src.get("title", src["id"]),
            "authority": authority[src["id"]],
            "lines": len(blocks),
            "fields_extracted": len(extraction.fields),
            "path": src.get("path"),
        })

    # --- 2. merge -----------------------------------------------------------
    fields = []
    omitted: list[str] = []
    for fs in schema["fields"]:
        fvs = by_field.get(fs["name"], [])
        if not fvs:
            omitted.append(fs["name"])
            continue
        mf = merge_mod.merge_field(fs, fvs, authority)
        if mf is not None:
            fields.append(mf)

    # --- 3. validation (small rule table, not ML) ---------------------------
    passed, issues, rules_run = validate_mod.run_validation(fields, schema)

    # --- 4. confidence routing ----------------------------------------------
    for mf in fields:
        field_issues = [i for i in issues if i.field == mf.field]
        merge_mod.assign_status(mf, field_issues)
    queue = merge_mod.needs_review_queue(fields)

    # --- 5. AI semantic review (explains needs_review, never changes values) -
    ai_review = ai_review_mod.build_ai_reviewer(mode).review(
        queue, {"authority": authority, "source_infos": source_infos}
    )

    # --- 6. marketing with citations ----------------------------------------
    marketing = marketing_mod.generate_marketing(fields, schema)

    verified = sum(1 for f in fields if f.status == "verified")
    needs_review = sum(1 for f in fields if f.status == "needs_review")
    conflicts = sum(1 for f in fields if "conflict" in f.flags)

    record = ProductRecord(
        product_id=manifest["product_id"],
        category=manifest["category"],
        category_label=schema.get("category_label", manifest["category"]),
        pipeline={
            "version": "1.0.0",
            "mode": provider,
            "provider_note": (
                "deterministic mock (offline)" if provider == "mock"
                else f"{provider} structured output"
            ),
            "run_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        },
        sources=source_infos,
        fields=fields,
        validation={
            "passed": passed,
            "rules_run": rules_run,
            "issues": issues,
            "note": "Explicit category rule table (not ML); rules only run on extracted fields.",
        },
        needs_review_queue=queue,
        marketing=marketing,
        ai_review=ai_review,
        summary={
            "total_fields_in_schema": len(schema["fields"]),
            "extracted": len(fields),
            "omitted_not_found": omitted,
            "verified": verified,
            "needs_review": needs_review,
            "conflicts": conflicts,
            "rules_run": rules_run,
        },
    )

    if output_path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(record.to_json(), encoding="utf-8")
    return record


def print_summary(record: ProductRecord) -> None:
    """Console summary for the demo."""
    s = record.summary
    print("=" * 78)
    print(f"PRODUCT  {record.product_id}  |  {record.category_label}")
    print(f"PIPELINE {record.pipeline['mode']} ({record.pipeline['provider_note']})")
    print(
        f"FIELDS   {s['verified']} verified / {s['needs_review']} needs review / "
        f"{s['conflicts']} conflict(s)  |  omitted (not found): {', '.join(s['omitted_not_found']) or '-'}"
    )
    print(f"VALIDATION passed={record.validation['passed']} rules_run={s['rules_run']}")
    print("-" * 78)
    print(f"{'FIELD':<26} {'VALUE':<18} {'CONF':>5}  STATUS")
    for f in record.fields:
        if f.conflicts:
            vals = [v for c in f.conflicts for v in c.values]
            val = " / ".join(f"{v['value']} {v['unit'] or ''}".strip() for v in vals)
        else:
            val = f"{f.value} {f.unit or ''}".strip()
        print(f"{f.label:<26} {val:<18} {f.confidence:>5.2f}  {f.status.upper()}")
    print("=" * 78)
    if record.needs_review_queue:
        print("NEEDS REVIEW QUEUE")
        for q in record.needs_review_queue:
            print(f"  - [{', '.join(q['flags'])}] {q['label']}: {q['reason']}")
    print(f"MARKETING: {record.marketing['description']}")
    if record.marketing["sentences_skipped"]:
        print("SKIPPED:", *record.marketing["sentences_skipped"], sep="\n  - ")
    if record.ai_review:
        print(f"AI REVIEW: {record.ai_review['summary']}")
