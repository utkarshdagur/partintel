"""Category plausibility validation.

A small explicit rule table (NOT ML) that flags physically impossible or
implausible values, e.g. bore_diameter >= outer_diameter, inverted
temperature ranges. Rules only run on fields that were actually extracted;
missing fields are skipped (omit, never fabricate).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .models import MergedField, ValidationIssue


@dataclass
class Rule:
    id: str
    name: str
    severity: str                     # error | warning
    fields: tuple                    # schema field names the rule reads
    check: Callable[[list], bool]    # receives resolved numeric/str values
    message: Callable[[list], str]


def run_validation(fields: list[MergedField], schema: dict) -> tuple[bool, list[ValidationIssue], int]:
    """Returns (passed, issues, rules_run). Missing fields skip their rules."""
    by_name = {f.field: f for f in fields}

    def num(name: str) -> Optional[float]:
        f = by_name.get(name)
        if f is None or not isinstance(f.value, (int, float)):
            return None
        return float(f.value)

    def val(name: str):
        f = by_name.get(name)
        return f.value if f else None

    # --- category rule table -------------------------------------------------
    rules = [
        Rule("bore_lt_outer", "Bore < outer diameter", "error", ("bore_diameter", "outer_diameter"),
             lambda v: v[0] < v[1],
             lambda v: f"bore_diameter ({v[0]:g} mm) must be smaller than outer_diameter ({v[1]:g} mm)"),
        Rule("width_lt_outer", "Width < outer diameter", "error", ("width", "outer_diameter"),
             lambda v: v[0] < v[1],
             lambda v: f"width ({v[0]:g} mm) must be smaller than outer_diameter ({v[1]:g} mm)"),
        Rule("dims_positive", "Dimensions positive", "error", ("bore_diameter", "outer_diameter", "width"),
             lambda v: all(x > 0 for x in v),
             lambda v: f"dimensions must be > 0 (got {v})"),
        Rule("dynamic_ge_static", "Dynamic load >= static load", "error", ("dynamic_load_rating", "static_load_rating"),
             lambda v: v[0] >= v[1],
             lambda v: f"dynamic_load_rating ({v[0]:g} kN) cannot be below static_load_rating ({v[1]:g} kN)"),
        Rule("loads_positive", "Load ratings positive", "error", ("dynamic_load_rating", "static_load_rating"),
             lambda v: all(x > 0 for x in v),
             lambda v: f"load ratings must be > 0 (got {v})"),
        Rule("speed_positive", "Max speed positive", "error", ("max_speed",),
             lambda v: v[0] > 0,
             lambda v: f"max_speed must be > 0 (got {v[0]:g} rpm)"),
        Rule("weight_sane", "Weight in sane range", "warning", ("weight",),
             lambda v: 0.001 <= v[0] <= 500,
             lambda v: f"weight {v[0]:g} kg outside sane range (0.001-500 kg)"),
        Rule("temp_min_lt_max", "Min temp < max temp", "error", ("temperature_range_min", "temperature_range_max"),
             lambda v: v[0] < v[1],
             lambda v: f"temperature_range_min ({v[0]:g} °C) must be below temperature_range_max ({v[1]:g} °C)"),
        Rule("temp_sane_bounds", "Temp range within sane bounds", "warning", ("temperature_range_min", "temperature_range_max"),
             lambda v: -80 <= v[0] and v[1] <= 400,
             lambda v: f"temperature range [{v[0]:g}, {v[1]:g}] °C outside sane bounds (-80..400 °C)"),
        # --- electric motor (3rd category). Like the bearing rules, these only
        # fire on fields that were actually extracted (missing fields skip).
        Rule("motor_power_positive", "Motor output power positive", "error", ("power_kw",),
             lambda v: v[0] > 0,
             lambda v: f"power_kw must be > 0 (got {v[0]:g} kW)"),
        Rule("motor_power_sane", "Motor output power in sane range", "warning", ("power_kw",),
             lambda v: 0.01 <= v[0] <= 50000,
             lambda v: f"power_kw {v[0]:g} kW outside sane range (0.01-50000 kW)"),
        Rule("motor_voltage_positive", "Motor rated voltage positive", "error", ("rated_voltage",),
             lambda v: v[0] > 0,
             lambda v: f"rated_voltage must be > 0 (got {v[0]:g} V)"),
        Rule("motor_voltage_sane", "Motor rated voltage in sane range", "warning", ("rated_voltage",),
             lambda v: 12 <= v[0] <= 15000,
             lambda v: f"rated_voltage {v[0]:g} V outside sane range (12-15000 V)"),
        Rule("motor_current_positive", "Motor rated current positive", "error", ("rated_current",),
             lambda v: v[0] > 0,
             lambda v: f"rated_current must be > 0 (got {v[0]:g} A)"),
        Rule("motor_speed_positive", "Motor rated speed positive", "error", ("rated_speed",),
             lambda v: v[0] > 0,
             lambda v: f"rated_speed must be > 0 (got {v[0]:g} rpm)"),
        Rule("motor_speed_sane", "Motor rated speed in sane range", "warning", ("rated_speed",),
             lambda v: 100 <= v[0] <= 40000,
             lambda v: f"rated_speed {v[0]:g} rpm outside sane range (100-40000 rpm)"),
        Rule("motor_efficiency_range", "Motor efficiency in 0..100 %", "error", ("efficiency",),
             lambda v: 0 < v[0] <= 100,
             lambda v: f"efficiency {v[0]:g}% outside 0..100% range"),
        Rule("motor_freq_sane", "Motor frequency in sane range", "warning", ("frequency",),
             lambda v: 5 <= v[0] <= 1000,
             lambda v: f"frequency {v[0]:g} Hz outside sane range (5..1000 Hz)"),
        Rule("motor_weight_sane", "Motor weight in sane range", "warning", ("weight",),
             lambda v: 0.1 <= v[0] <= 10000,
             lambda v: f"weight {v[0]:g} kg outside sane range (0.1-10000 kg)"),
        # --- 2-way ball valve (2nd category)
        Rule("valve_conn_positive", "Valve connection size positive", "error", ("connection_size",),
             lambda v: v[0] > 0,
             lambda v: f"connection_size must be > 0 (got {v[0]:g} mm)"),
        Rule("valve_conn_sane", "Valve connection size in sane range", "warning", ("connection_size",),
             lambda v: 1 <= v[0] <= 2000,
             lambda v: f"connection_size {v[0]:g} mm outside sane range (1-2000 mm)"),
        Rule("valve_pressure_positive", "Valve pressure rating positive", "error", ("pressure_rating",),
             lambda v: v[0] > 0,
             lambda v: f"pressure_rating must be > 0 (got {v[0]:g} bar)"),
        Rule("valve_pressure_sane", "Valve pressure rating in sane range", "warning", ("pressure_rating",),
             lambda v: 0.1 <= v[0] <= 1000,
             lambda v: f"pressure_rating {v[0]:g} bar outside sane range (0.1-1000 bar)"),
        # --- helical gear reducer (4th category)
        Rule("gb_ratio_positive", "Gear ratio positive", "error", ("ratio",),
             lambda v: v[0] > 0,
             lambda v: f"ratio must be > 0 (got {v[0]:g})"),
        Rule("gb_ratio_sane", "Gear ratio in sane range", "warning", ("ratio",),
             lambda v: 1 <= v[0] <= 10000,
             lambda v: f"ratio {v[0]:g} outside sane range (1-10000)"),
        Rule("gb_input_speed_sane", "Input speed in sane range", "warning", ("input_speed",),
             lambda v: 100 <= v[0] <= 20000,
             lambda v: f"input_speed {v[0]:g} rpm outside sane range (100-20000 rpm)"),
        Rule("gb_output_positive", "Output speed positive", "error", ("output_speed",),
             lambda v: v[0] > 0,
             lambda v: f"output_speed must be > 0 (got {v[0]:g} rpm)"),
        Rule("gb_output_lt_input", "Output speed < input speed", "warning", ("input_speed", "output_speed"),
             lambda v: v[0] > v[1],
             lambda v: f"output_speed ({v[1]:g} rpm) must be below input_speed ({v[0]:g} rpm)"),
        Rule("gb_torque_positive", "Rated torque positive", "error", ("rated_torque",),
             lambda v: v[0] > 0,
             lambda v: f"rated_torque must be > 0 (got {v[0]:g} N·m)"),
        Rule("gb_torque_sane", "Rated torque in sane range", "warning", ("rated_torque",),
             lambda v: 0.05 <= v[0] <= 5000000,
             lambda v: f"rated_torque {v[0]:g} N·m outside sane range (0.05-5000000 N·m)"),
    ]

    # --- generic enum rule: ONE rule per schema enum field -------------------
    # Generated from the schema (not hardcoded per category), so an enum value
    # that is not in the schema enum can never pass silently, in any category.
    for fs in schema["fields"]:
        if fs.get("type") != "enum" or not fs.get("enum"):
            continue
        fname = fs["name"]
        allowed = {x.casefold() for x in fs["enum"]}
        rules.append(Rule(
            f"{fname}_enum", f"{fs.get('label', fname)} in schema enum", "error", (fname,),
            lambda v, a=allowed: str(v[0]).casefold() in a,
            lambda v, a=allowed, fn=fname, e=fs["enum"]: f"{fn} {v[0]!r} not in schema enum {sorted(e)}",
        ))

    issues: list[ValidationIssue] = []
    rules_run = 0
    for r in rules:
        vals = []
        for fname in r.fields:
            ftype = _field_type(schema, fname)
            vals.append(num(fname) if ftype == "number" else val(fname))
        if any(v is None for v in vals):
            continue  # field missing -> rule skipped, no fabrication
        rules_run += 1
        if not r.check(vals):
            issues.append(ValidationIssue(
                field=r.fields[0], rule_id=r.id, severity=r.severity, message=r.message(vals)
            ))

    passed = not any(i.severity == "error" for i in issues)
    return passed, issues, rules_run


def _enum(schema: dict, name: str) -> list:
    for f in schema["fields"]:
        if f["name"] == name:
            return f.get("enum", [])
    return []


def _field_type(schema: dict, name: str) -> str:
    for f in schema["fields"]:
        if f["name"] == name:
            return f.get("type", "string")
    return "string"
