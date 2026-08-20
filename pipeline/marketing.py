"""Generated marketing description with field citations.

Sentences are drawn from a per-category template. A sentence is only emitted
if EVERY field it references is verified — needs_review / conflicted / missing
fields are never cited in marketing copy (and are reported as skipped).
"""
from __future__ import annotations

from .models import MergedField

def _fmt(value) -> str:
    """Render a merged value for copy: whole floats become ints (25.0 -> 25)."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


# Per-category marketing sentence templates. {{field}} tokens are replaced
# with the merged value; `uses` lists the fields the sentence draws from.
CATEGORY_MARKETING = {
    "deep_groove_ball_bearing": [
        {
            "sentence": "The {{part_number}} is a {{seal_type}} deep groove ball bearing with a {{bore_diameter}} mm bore, {{outer_diameter}} mm outer diameter and {{width}} mm width.",
            "uses": ["part_number", "seal_type", "bore_diameter", "outer_diameter", "width"],
            "phrases": {"seal_type": {"2RS": "sealed", "2Z": "shielded", "2RU": "sealed", "open": "open"}},
        },
        {
            "sentence": "It is rated for a dynamic load of {{dynamic_load_rating}} kN and a static load of {{static_load_rating}} kN.",
            "uses": ["dynamic_load_rating", "static_load_rating"],
        },
        {
            "sentence": "Operating temperature range is {{temperature_range_min}} °C to {{temperature_range_max}} °C.",
            "uses": ["temperature_range_min", "temperature_range_max"],
        },
        {
            "sentence": "Maximum limiting speed is {{max_speed}} rpm.",
            "uses": ["max_speed"],
        },
        {
            "sentence": "The unit weighs {{weight}} kg.",
            "uses": ["weight"],
        },
    ],
    "electric_motor": [
        {
            "sentence": "The {{part_number}} is an {{insulation_class}}-class, {{protection_class}} three-phase induction motor rated {{power_kw}} kW at {{rated_speed}} rpm.",
            "uses": ["part_number", "insulation_class", "protection_class", "power_kw", "rated_speed"],
        },
        {
            "sentence": "It runs on {{rated_voltage}} V at {{frequency}} Hz, drawing {{rated_current}} A at {{efficiency}}% efficiency.",
            "uses": ["rated_voltage", "frequency", "rated_current", "efficiency"],
        },
        {
            "sentence": "The unit weighs {{weight}} kg.",
            "uses": ["weight"],
        },
    ],
    "2_way_ball_valve": [
        {
            "sentence": "The {{part_number}} is a {{body_material}} {{actuation}} two-way ball valve with a {{connection_size}} mm connection, rated to {{pressure_rating}} bar.",
            "uses": ["part_number", "body_material", "actuation", "connection_size", "pressure_rating"],
        },
        {
            "sentence": "Operating temperature range is {{temperature_range_min}} °C to {{temperature_range_max}} °C.",
            "uses": ["temperature_range_min", "temperature_range_max"],
        },
        {
            "sentence": "The unit weighs {{weight}} kg.",
            "uses": ["weight"],
        },
    ],
}


def generate_marketing(fields: list[MergedField], schema: dict) -> dict:
    verified = {f.field: f for f in fields if f.status == "verified"}
    templates = CATEGORY_MARKETING.get(schema.get("category"), [])

    rendered: list[str] = []
    citations: list[dict] = []      # [{field, label, value, sources:[{source_id, snippet}]}]
    skipped: list[str] = []         # sentence keys skipped + why

    for tpl in templates:
        missing = [u for u in tpl["uses"] if u not in verified]
        if missing:
            skipped.append(
                f"sentence using {', '.join(missing)} skipped (needs_review/missing/conflict)"
            )
            continue
        sentence = tpl["sentence"]
        for fname in tpl["uses"]:
            f = verified[fname]
            value = _fmt(f.value)
            phrases = tpl.get("phrases", {}).get(fname, {})
            value = phrases.get(_fmt(f.value), value)
            sentence = sentence.replace("{{%s}}" % fname, value)
        rendered.append(sentence)
        for fname in tpl["uses"]:
            f = verified[fname]
            citations.append({
                "field": fname,
                "label": f.label,
                "value": _fmt(f.value),
                "sources": [
                    {"source_id": s.source_id, "snippet": s.snippet}
                    for s in f.sources
                ],
            })

    return {
        "description": " ".join(rendered),
        "citations": citations,
        "sentences_skipped": skipped,
        "note": "Marketing copy only cites verified fields; needs_review fields are never auto-published.",
    }
