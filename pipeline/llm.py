"""Structured extraction via LLM (tool-calling / JSON mode) + offline mock.

Provider auto-detection:
  1. ANTHROPIC_API_KEY set  -> Claude tool use with input_schema
  2. OPENAI_API_KEY set     -> OpenAI Structured Outputs (json_schema)
  3. neither                -> MockExtractor: a deterministic regex stand-in
     that mirrors the LLM contract exactly (values + exact snippets + line
     spans + confidence + reasoning), so the whole demo runs offline and the
     merge/validate/routing code is provider-independent.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional

from .models import FieldValue, SourceExtraction
from .sources import Block, line_numbered

BASE_DIR = Path(__file__).resolve().parent.parent
PROMPT_PATH = BASE_DIR / "prompts" / "extraction_prompt.md"

METHODS = ("table_parse", "llm_inference", "ocr_heuristic")

# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


def load_prompt_template(path: Optional[str] = None) -> str:
    p = Path(path) if path else PROMPT_PATH
    return p.read_text(encoding="utf-8")


def _variant_block(template: str, variant: str) -> str:
    m = re.search(
        rf"<!-- VARIANT: {variant} -->\n(.*?)\n<!-- /VARIANT -->", template, re.S
    )
    if not m:
        raise ValueError(f"prompt template has no {variant!r} variant")
    return m.group(1).strip()


def render_prompt(template, variant, source_meta, source_text, schema, output_schema):
    """Render the full prompt (system + variant user message) for one source."""
    system = _variant_block(template, "system")
    user = _variant_block(template, variant)
    fills = {
        "source_title": source_meta.get("title", source_meta.get("id", "?")),
        "source_id": source_meta.get("id", "?"),
        "source_text": source_text,
        "schema_json": json.dumps(schema, indent=2, ensure_ascii=False),
        "output_schema_json": json.dumps(output_schema, indent=2),
        "category_label": schema.get("category_label", schema.get("category", "?")),
    }
    user = re.sub(
        r"\{\{\s*([a-z_]+)\s*\}\}",
        lambda m: str(fills.get(m.group(1), m.group(0))),
        user,
    )
    return f"{system}\n\n{user}"


def build_output_schema(schema: dict) -> dict:
    """JSON Schema for the extraction response (fields array)."""
    field_names = [f["name"] for f in schema["fields"]]
    return {
        "type": "object",
        "properties": {
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "field": {"type": "string", "description": f"One of: {', '.join(field_names)}"},
                        "value": {"type": "string"},
                        "unit": {"type": ["string", "null"]},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "method": {"type": "string", "enum": list(METHODS)},
                        "snippet": {"type": "string"},
                        "line_start": {"type": "integer"},
                        "line_end": {"type": "integer"},
                        "reasoning": {"type": "string"},
                    },
                    "required": ["field", "value", "confidence", "method", "snippet", "line_start", "line_end"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["fields"],
        "additionalProperties": False,
    }


# ---------------------------------------------------------------------------
# Real LLM adapters (used when API keys are present)
# ---------------------------------------------------------------------------


def pick_provider(mode: str = "auto") -> str:
    if mode in ("anthropic", "openai", "mock"):
        return mode
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return "mock"


def _call_anthropic(prompt: str, output_schema: dict) -> dict:
    import anthropic

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5"),
        max_tokens=4096,
        tools=[{
            "name": "submit_extraction",
            "description": "Submit extracted product fields for one source.",
            "input_schema": output_schema,
        }],
        tool_choice={"type": "tool", "name": "submit_extraction"},
        messages=[{"role": "user", "content": prompt}],
    )
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "submit_extraction":
            return block.input
    raise RuntimeError("Claude did not return a submit_extraction tool call")


def _call_openai(prompt: str, output_schema: dict) -> dict:
    import openai

    client = openai.OpenAI()
    resp = client.chat.completions.create(
        model=os.environ.get("OPENAI_MODEL", "gpt-4.1-mini"),
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "extraction", "schema": output_schema, "strict": True},
        },
        messages=[{"role": "user", "content": prompt}],
    )
    return json.loads(resp.choices[0].message.content)


class LLMExtractor:
    """Extract structured fields from one source via a real LLM."""

    def __init__(self, provider: str, template: str):
        self.provider = provider
        self.template = template

    def extract(self, source_meta: dict, blocks: list[Block], schema: dict) -> SourceExtraction:
        output_schema = build_output_schema(schema)
        prompt = render_prompt(
            self.template,
            source_meta["type"],
            source_meta,
            line_numbered(blocks),
            schema,
            output_schema,
        )
        if self.provider == "anthropic":
            data = _call_anthropic(prompt, output_schema)
        else:
            data = _call_openai(prompt, output_schema)
        return _parse_llm_response(data, source_meta, schema)


def _parse_llm_response(data, source_meta: dict, schema: dict) -> SourceExtraction:
    """Parse the LLM's JSON into FieldValues, tolerating malformed/partial
    output: None or non-dict payloads, missing/extra fields, wrong value
    types, out-of-range confidence, bad methods, non-numeric line spans.
    Anything unusable is skipped rather than raising."""
    known = {f["name"] for f in schema["fields"]}
    out = SourceExtraction(source_id=source_meta["id"])
    if not isinstance(data, dict):
        return out
    items = data.get("fields")
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        field = item.get("field")
        if not isinstance(field, str) or field not in known:
            continue  # drop hallucinated / unknown fields

        # value must be a scalar string or number; objects/lists/bools and
        # empty values are treated as a failed extraction and skipped.
        value = item.get("value")
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            continue
        value = str(value) if isinstance(value, (int, float)) else value
        if not value.strip():
            continue

        try:
            conf = max(0.0, min(1.0, float(item.get("confidence", 0.0))))
        except (TypeError, ValueError):
            conf = 0.0
        method = item.get("method", "llm_inference")
        if not isinstance(method, str) or method not in METHODS:
            method = "llm_inference"
        try:
            line_start = int(item.get("line_start", 0) or 0)
            line_end = int(item.get("line_end", 0) or 0)
        except (TypeError, ValueError):
            line_start = line_end = 0
        snippet = item.get("snippet", "")
        reasoning = item.get("reasoning", "")
        unit = item.get("unit")
        out.fields.append(
            FieldValue(
                field=field,
                value=value,
                unit=unit if isinstance(unit, str) and unit.strip() else None,
                confidence=conf,
                method=method,
                source_id=source_meta["id"],
                snippet=snippet if isinstance(snippet, str) else "",
                line_start=line_start,
                line_end=line_end,
                reasoning=reasoning if isinstance(reasoning, str) else "",
            )
        )
    return out

# ---------------------------------------------------------------------------
# Mock extractor (offline demo stand-in for the LLM)
# ---------------------------------------------------------------------------

Rule = tuple  # (field, pattern, value_fn, unit, confidence, method)


PDF_RULES: list[Rule] = [
    # 62\d\d covers the whole 62xx series (6200 .. 6211 in the synthetic catalog)
    ("part_number", r"(?i)^(?:SKF\s+)?(62\d{2}[\w-]*)\s+DEEP GROOVE", lambda m: m.group(1), None, 0.95, "table_parse"),
    ("brand", r"^(SKF)\s+62\d{2}[\w-]*", lambda m: m.group(1), None, 0.95, "table_parse"),
    ("bore_diameter", r"^d \[mm\]\s+(\d+(?:[.,]\d+)?)", lambda m: m.group(1), "mm", 0.95, "table_parse"),
    ("outer_diameter", r"^D \[mm\]\s+(\d+(?:[.,]\d+)?)", lambda m: m.group(1), "mm", 0.95, "table_parse"),
    ("width", r"^B \[mm\]\s+(\d+(?:[.,]\d+)?)", lambda m: m.group(1), "mm", 0.95, "table_parse"),
    ("dynamic_load_rating", r"Basic dynamic load rating C\s+([\d\s.,]+)\s*(kN|N)", lambda m: m.group(1).replace(" ", ""), "kN", 0.95, "table_parse"),
    ("static_load_rating", r"Basic static load rating C0\s+([\d\s.,]+)\s*(kN|N)", lambda m: m.group(1).replace(" ", ""), "kN", 0.95, "table_parse"),
    ("max_speed", r"Limiting speed\s+([\d\s.]+)\s*(r/min|rpm)", lambda m: m.group(1).replace(" ", ""), "rpm", 0.95, "table_parse"),
    # weight accepts kg/g/lb — lb is converted to kg in merge
    ("weight", r"^Mass\s+([\d.]+)\s*(kg|g|lb)", lambda m: m.group(1), lambda m: m.group(2), 0.95, "table_parse"),
    ("seal_type", r"Seal\s+Contact seal,\s*RS1? on both sides", lambda m: "2RS", None, 0.95, "llm_inference"),
    ("clearance", r"^Internal clearance\s+(C[0-5]|CN)", lambda m: m.group(1), None, 0.95, "table_parse"),
]

WEB_RULES: list[Rule] = [
    ("part_number", r"^(\d{4}-2RS[\w-]*)\b", lambda m: m.group(1), None, 0.85, "llm_inference"),
    ("bore_diameter", r"Bore:\s*(\d+(?:[.,]\d+)?)\s*mm", lambda m: m.group(1), "mm", 0.95, "table_parse"),
    ("outer_diameter", r"(?:OD|outer diameter)\s*[: ]\s*(\d+(?:[.,]\d+)?)\s*mm", lambda m: m.group(1), "mm", 0.95, "table_parse"),
    ("width", r"Width:\s*(\d+(?:[.,]\d+)?)\s*mm", lambda m: m.group(1), "mm", 0.95, "table_parse"),
    ("dynamic_load_rating", r"Dynamic load rating\s*(?:[|:]\s*)?([\d\s.,]+)\s*(kN|N)", lambda m: m.group(1).replace(" ", ""), lambda m: m.group(2), 0.95, "table_parse"),
    ("static_load_rating", r"Static load rating\s*(?:[|:]\s*)?([\d\s.,]+)\s*(kN|N)", lambda m: m.group(1).replace(" ", ""), lambda m: m.group(2), 0.95, "table_parse"),
    ("max_speed", r"Max speed[^\n]*?([\d\s.,]+)\s*(rpm|r/min)", lambda m: m.group(1).replace(" ", ""), "rpm", 0.85, "table_parse"),
    ("temperature_range_min", r"Operating temperature[^\n]*?([+-]?\d+)\s*°C\s+to\s+([+-]?\d+)\s*°C", lambda m: m.group(1), "°C", 0.95, "table_parse"),
    ("temperature_range_max", r"Operating temperature[^\n]*?([+-]?\d+)\s*°C\s+to\s+([+-]?\d+)\s*°C", lambda m: m.group(2), "°C", 0.95, "table_parse"),
    ("seal_type", r"Seal\s*(?:[|:]\s*)?(2RS|2Z|ZZ|RS|open)", lambda m: m.group(1), None, 0.95, "table_parse"),
    ("weight", r"Weight\s*[:| ]+([\d\s.,]+)\s*(kg|g|lb)", lambda m: m.group(1).replace(" ", ""), lambda m: m.group(2), 0.95, "table_parse"),
    ("clearance", r"Internal clearance\s*(?:[|:]\s*)?(C[0-5]|CN)", lambda m: m.group(1), None, 0.95, "table_parse"),
    ("country_of_origin", r"Made in\s*(?:[|:]\s*)?([A-Za-z]+)", lambda m: m.group(1), None, 0.95, "table_parse"),
]

OCR_RULES: list[Rule] = [
    ("bore_diameter", r"^(\d{2,3})x(\d{2,3})x(\d{2,3})$", lambda m: m.group(1), "mm", 0.7, "llm_inference"),
    ("outer_diameter", r"^(\d{2,3})x(\d{2,3})x(\d{2,3})$", lambda m: m.group(2), "mm", 0.7, "llm_inference"),
    ("width", r"^(\d{2,3})x(\d{2,3})x(\d{2,3})$", lambda m: m.group(3), "mm", 0.7, "llm_inference"),
    ("seal_type", r"^(2RS|2Z|ZZ|RS)$", lambda m: m.group(1), None, 0.6, "ocr_heuristic"),
    ("seal_type", r"^(2R5)$", lambda m: m.group(1), None, 0.55, "ocr_heuristic"),  # OCR typo → fuzzy-resolved to 2RS in merge
    ("country_of_origin", r"^([A-Z]{2,10})$", lambda m: m.group(1).title(), None, 0.6, "ocr_heuristic"),
    ("precision_grade", r"^ABEC\s*(\d)$", lambda m: f"ABEC {m.group(1)}", None, 0.55, "ocr_heuristic"),
]

# --- 3-phase induction motor (third category — proves the pipeline is category-agnostic)
MOTOR_PDF_RULES: list[Rule] = [
    ("part_number", r"^Part no\.?\s+([A-Z0-9\-]+)", lambda m: m.group(1), None, 0.95, "table_parse"),
    ("brand", r"^Brand\s+([A-Za-z]+)", lambda m: m.group(1), None, 0.95, "table_parse"),
    # power accepts kW or HP (hp->kW conversion happens in merge, so a datasheet
    # in HP and a web page in kW reconcile to the same normalized value)
    ("power_kw", r"Output power\s+([\d.]+)\s*(kW|HP|hp)\b", lambda m: m.group(1), lambda m: m.group(2), 0.95, "table_parse"),
    ("rated_voltage", r"Rated voltage\s+([\d.]+)\s*V\b", lambda m: m.group(1), "V", 0.95, "table_parse"),
    ("rated_current", r"Rated current\s+([\d.]+)\s*A\b", lambda m: m.group(1), "A", 0.95, "table_parse"),
    ("rated_speed", r"Rated speed\s+([\d\s.,]+)\s*(r/min|rpm)", lambda m: m.group(1).replace(" ", ""), "rpm", 0.95, "table_parse"),
    ("frequency", r"Frequency\s+([\d.]+)\s*Hz", lambda m: m.group(1), "Hz", 0.95, "table_parse"),
    ("efficiency", r"Efficiency\s+([\d.]+)\s*%", lambda m: m.group(1), "%", 0.95, "table_parse"),
    ("frame_size", r"Frame size\s+(\d{3}[A-Z]?)", lambda m: m.group(1), None, 0.95, "table_parse"),
    ("protection_class", r"Protection\s+(IP\d+)", lambda m: m.group(1), None, 0.95, "table_parse"),
    ("insulation_class", r"Insulation class\s+([A-Z])", lambda m: m.group(1), None, 0.95, "table_parse"),
    ("mounting", r"Mounting\s+([A-Z0-9]+)", lambda m: m.group(1), None, 0.95, "table_parse"),
    ("weight", r"^Mass\s+([\d.]+)\s*(kg|g)", lambda m: m.group(1), "kg", 0.95, "table_parse"),
]

MOTOR_WEB_RULES: list[Rule] = [
    ("part_number", r"^([A-Z0-9\-]+)\s+Induction Motor", lambda m: m.group(1), None, 0.85, "llm_inference"),
    ("power_kw", r"Output power\s*(?:[|:]\s*)?([\d.]+)\s*(kW|HP|hp)\b", lambda m: m.group(1), lambda m: m.group(2), 0.95, "table_parse"),
    ("rated_voltage", r"(?:Voltage|Rated voltage)\s*(?:[|:]\s*)?([\d.]+)\s*V\b", lambda m: m.group(1), "V", 0.95, "table_parse"),
    ("rated_current", r"Rated current\s*(?:[|:]\s*)?([\d.]+)\s*A\b", lambda m: m.group(1), "A", 0.95, "table_parse"),
    ("rated_speed", r"Rated speed\s*(?:[|:]\s*)?([\d\s.,]+)\s*(rpm|r/min)", lambda m: m.group(1).replace(" ", ""), "rpm", 0.95, "table_parse"),
    ("efficiency", r"Efficiency\s*(?:[|:]\s*)?([\d.]+)\s*%", lambda m: m.group(1), "%", 0.95, "table_parse"),
    ("frame_size", r"Frame size\s*(?:[|:]\s*)?(\d{3}[A-Z]?)", lambda m: m.group(1), None, 0.95, "table_parse"),
    ("protection_class", r"Protection\s*(?:[|:]\s*)?(IP\d+)", lambda m: m.group(1), None, 0.95, "table_parse"),
    ("insulation_class", r"Insulation\s*(?:[|:]\s*)?Class\s*([A-Z])", lambda m: m.group(1), None, 0.95, "table_parse"),
    ("mounting", r"Mounting\s*(?:[|:]\s*)?([A-Z0-9]+)", lambda m: m.group(1), None, 0.95, "table_parse"),
    ("weight", r"Weight\s*(?:[|:]\s*)?([\d.]+)\s*kg", lambda m: m.group(1), "kg", 0.95, "table_parse"),
]

MOTOR_OCR_RULES: list[Rule] = [
    ("part_number", r"^([A-Z0-9\-]+)$", lambda m: m.group(1), None, 0.7, "ocr_heuristic"),
    ("power_kw", r"^([\d.]+)\s*kW", lambda m: m.group(1), "kW", 0.7, "ocr_heuristic"),
    ("rated_voltage", r"^(\d+)\s*V$", lambda m: m.group(1), "V", 0.7, "ocr_heuristic"),
    ("rated_speed", r"^(\d+)\s*r/min", lambda m: m.group(1), "rpm", 0.7, "ocr_heuristic"),
    ("protection_class", r"^(IP\d+)$", lambda m: m.group(1), None, 0.7, "ocr_heuristic"),
    ("insulation_class", r"^([A-Z])$", lambda m: m.group(1), None, 0.6, "ocr_heuristic"),
    ("mounting", r"^(B[0-9]+)$", lambda m: m.group(1), None, 0.6, "ocr_heuristic"),
]

# --- 2-way ball valve (second category — proves the pipeline is category-agnostic)
VALVE_PDF_RULES: list[Rule] = [
    ("part_number", r"^Part no\.?\s+([A-Z0-9\-]+)", lambda m: m.group(1), None, 0.95, "table_parse"),
    ("brand", r"^Brand\s+([A-Za-z]+)", lambda m: m.group(1), None, 0.95, "table_parse"),
    ("connection_size", r"^DN \[mm\]\s+(\d+(?:[.,]\d+)?)", lambda m: m.group(1), "mm", 0.95, "table_parse"),
    # pressure accepts bar or psi (psi->bar conversion happens in merge, so a
    # datasheet in bar and a distributor page in PSI reconcile to one value)
    ("pressure_rating", r"^Pressure rating\s+([\d.]+)\s*(bar|psi|PSI)\b", lambda m: m.group(1), lambda m: m.group(2), 0.95, "table_parse"),
    ("body_material", r"^Body material\s+([a-z_ ]+)", lambda m: m.group(1).strip(), None, 0.95, "table_parse"),
    ("actuation", r"^Actuation\s+([a-z ]+)", lambda m: m.group(1).strip(), None, 0.95, "table_parse"),
    ("temperature_range_min", r"Operating temperature\s*([+-]?\d+)\s*°C\s+to\s+([+-]?\d+)\s*°C", lambda m: m.group(1), "°C", 0.95, "table_parse"),
    ("temperature_range_max", r"Operating temperature\s*([+-]?\d+)\s*°C\s+to\s+([+-]?\d+)\s*°C", lambda m: m.group(2), "°C", 0.95, "table_parse"),
    ("weight", r"^Weight\s+([\d.]+)\s*(kg|g|lb)", lambda m: m.group(1), lambda m: m.group(2), 0.95, "table_parse"),
]

VALVE_WEB_RULES: list[Rule] = [
    ("part_number", r"^(V[0-9A-Z\-]+)\b", lambda m: m.group(1), None, 0.85, "llm_inference"),
    ("connection_size", r"DN\s*(\d+(?:[.,]\d+)?)\s*mm", lambda m: m.group(1), "mm", 0.95, "table_parse"),
    ("connection_size", r"Connection\s*[:| ]+([\d.]+)\s*in\b", lambda m: m.group(1), "in", 0.9, "table_parse"),
    ("pressure_rating", r"(?:PN|Pressure rating)[^\d]*?([\d.]+)\s*(bar|psi|PSI)\b", lambda m: m.group(1), lambda m: m.group(2), 0.9, "table_parse"),
    ("body_material", r"Body material\s*[:| ]+([A-Za-z_ ]+)", lambda m: m.group(1).strip(), None, 0.9, "table_parse"),
    ("actuation", r"Actuation\s*[:| ]+([a-z ]+)", lambda m: m.group(1).strip(), None, 0.9, "table_parse"),
    ("temperature_range_min", r"Operating temperature[^\n]*?([+-]?\d+)\s*°C\s+to\s+([+-]?\d+)\s*°C", lambda m: m.group(1), "°C", 0.95, "table_parse"),
    ("temperature_range_max", r"Operating temperature[^\n]*?([+-]?\d+)\s*°C\s+to\s+([+-]?\d+)\s*°C", lambda m: m.group(2), "°C", 0.95, "table_parse"),
    ("weight", r"Weight\s*[:| ]+([\d.]+)\s*(kg|g|lb)", lambda m: m.group(1), lambda m: m.group(2), 0.95, "table_parse"),
]

# --- helical gear reducer (fourth category — proves the pipeline is category-agnostic)
GEARBOX_PDF_RULES: list[Rule] = [
    ("part_number", r"^Part no\.?\s+([A-Z0-9\-]+)", lambda m: m.group(1), None, 0.95, "table_parse"),
    ("brand", r"^Brand\s+([A-Za-z]+)", lambda m: m.group(1), None, 0.95, "table_parse"),
    ("ratio", r"^Ratio\s+([\d.]+)\s*:\s*1\b", lambda m: m.group(1), None, 0.95, "table_parse"),
    ("input_speed", r"^Input speed\s+([\d\s.,]+)\s*(r/min|rpm)", lambda m: m.group(1).replace(" ", ""), "rpm", 0.95, "table_parse"),
    ("output_speed", r"^Output speed\s+([\d\s.,]+)\s*(r/min|rpm)", lambda m: m.group(1).replace(" ", ""), "rpm", 0.95, "table_parse"),
    # torque accepts N·m or ft·lb (ft-lb->N·m conversion happens in merge, so a
    # datasheet in N·m and a distributor page in ft·lb reconcile to one value)
    ("rated_torque", r"^Rated torque\s+([\d\s.,]+)\s*(Nm|N·m|ft-lb|lbf·ft)\b", lambda m: m.group(1).replace(" ", ""), lambda m: m.group(2), 0.95, "table_parse"),
    ("service_factor", r"^Service factor\s+([\d.]+)", lambda m: m.group(1), None, 0.95, "table_parse"),
    ("efficiency", r"^Efficiency\s+([\d.]+)\s*%", lambda m: m.group(1), "%", 0.95, "table_parse"),
    ("mounting", r"^Mounting\s+([A-Z0-9]+)", lambda m: m.group(1), None, 0.95, "table_parse"),
    ("protection_class", r"^Protection\s+(IP\d+)", lambda m: m.group(1), None, 0.95, "table_parse"),
    ("lubrication", r"^Lubrication\s+(oil|grease)", lambda m: m.group(1), None, 0.95, "table_parse"),
    ("weight", r"^Mass\s+([\d.]+)\s*(kg|g)", lambda m: m.group(1), "kg", 0.95, "table_parse"),
]

GEARBOX_WEB_RULES: list[Rule] = [
    ("part_number", r"^([A-Z0-9\-]+)\s+Gear (?:Reducer|Reduction)", lambda m: m.group(1), None, 0.85, "llm_inference"),
    ("ratio", r"Ratio\s*(?:[|:]\s*)?([\d.]+)\s*:\s*1\b", lambda m: m.group(1), None, 0.9, "table_parse"),
    ("input_speed", r"Input speed\s*(?:[|:]\s*)?([\d\s.,]+)\s*(rpm|r/min)", lambda m: m.group(1).replace(" ", ""), "rpm", 0.9, "table_parse"),
    ("output_speed", r"Output speed\s*(?:[|:]\s*)?([\d\s.,]+)\s*(rpm|r/min)", lambda m: m.group(1).replace(" ", ""), "rpm", 0.9, "table_parse"),
    ("rated_torque", r"(?:Rated torque|Torque rating)\s*(?:[|:]\s*)?([\d\s.,]+)\s*(Nm|N·m|ft-lb|lbf·ft)\b", lambda m: m.group(1).replace(" ", ""), lambda m: m.group(2), 0.9, "table_parse"),
    ("service_factor", r"Service factor\s*(?:[|:]\s*)?([\d.]+)", lambda m: m.group(1), None, 0.9, "table_parse"),
    ("efficiency", r"Efficiency\s*(?:[|:]\s*)?([\d.]+)\s*%", lambda m: m.group(1), "%", 0.9, "table_parse"),
    ("mounting", r"Mounting\s*(?:[|:]\s*)?([A-Z0-9]+)", lambda m: m.group(1), None, 0.9, "table_parse"),
    ("protection_class", r"Protection\s*(?:[|:]\s*)?(IP\d+)", lambda m: m.group(1), None, 0.9, "table_parse"),
    ("lubrication", r"Lubrication\s*(?:[|:]\s*)?(oil|grease)", lambda m: m.group(1), None, 0.9, "table_parse"),
    ("weight", r"Weight\s*(?:[|:]\s*)?([\d.]+)\s*kg", lambda m: m.group(1), "kg", 0.9, "table_parse"),
]

GEARBOX_OCR_RULES: list[Rule] = [
    ("part_number", r"^([A-Z0-9]{3,}[-/][A-Z0-9\-]+)$", lambda m: m.group(1), None, 0.7, "ocr_heuristic"),
    ("ratio", r"^([\d.]+)\s*:\s*1$", lambda m: m.group(1), None, 0.7, "ocr_heuristic"),
    ("rated_torque", r"^([\d\s.,]+)\s*N[·.]?m$", lambda m: m.group(1).replace(" ", ""), "Nm", 0.7, "ocr_heuristic"),
    ("protection_class", r"^(IP\d+)$", lambda m: m.group(1), None, 0.7, "ocr_heuristic"),
    ("weight", r"^([\d.]+)\s*kg$", lambda m: m.group(1), "kg", 0.7, "ocr_heuristic"),
]

# Per-category mock rule tables. The extractor picks the table from the schema
# category, so the merge/validate/route/marketing stack is provably generic.
_RULE_TABLES: dict[str, dict] = {
    "deep_groove_ball_bearing": {"pdf": PDF_RULES, "web": WEB_RULES, "ocr": OCR_RULES},
    "2_way_ball_valve": {"pdf": VALVE_PDF_RULES, "web": VALVE_WEB_RULES},
    "electric_motor": {"pdf": MOTOR_PDF_RULES, "web": MOTOR_WEB_RULES, "ocr": MOTOR_OCR_RULES},
    "helical_gear_reducer": {"pdf": GEARBOX_PDF_RULES, "web": GEARBOX_WEB_RULES, "ocr": GEARBOX_OCR_RULES},
}


class MockExtractor:
    """Deterministic regex extraction that mirrors the LLM output contract."""

    def extract(self, source_meta: dict, blocks: list[Block], schema: dict) -> SourceExtraction:
        rules = _RULE_TABLES.get(schema.get("category", ""), {}).get(source_meta["type"], [])
        known = {f["name"] for f in schema["fields"]}
        out = SourceExtraction(source_id=source_meta["id"])
        for field, pattern, value_fn, unit, conf, method in rules:
            if field not in known:
                continue
            for b in blocks:
                m = re.search(pattern, b.text.strip())
                if m:
                    unit = unit(m) if callable(unit) else unit
                    out.fields.append(
                        FieldValue(
                            field=field,
                            value=value_fn(m),
                            unit=unit,
                            confidence=conf,
                            method=method,
                            source_id=source_meta["id"],
                            snippet=b.text,
                            line_start=b.line_start,
                            line_end=b.line_end,
                            reasoning=(
                                f"Pattern matched on line {b.line_start}: {b.text.strip()}"
                            ),
                        )
                    )
                    break
        return out


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_extractor(mode: str = "auto", template: Optional[str] = None) -> object:
    """Return an extractor with .extract(source_meta, blocks, schema)."""
    provider = pick_provider(mode)
    tpl = template if template is not None else load_prompt_template()
    if provider in ("anthropic", "openai"):
        return LLMExtractor(provider, tpl)
    return MockExtractor()
