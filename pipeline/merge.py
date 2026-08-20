"""Cross-source merge: normalization, agreement, conflict flagging,
confidence computation and needs-review routing.
"""
from __future__ import annotations

import re

from .models import Conflict, FieldValue, MergedField

VERIFIED_THRESHOLD = 0.75          # effective confidence needed for verified
# Single-source discount: with no second source there is no independent
# cross-check, so a single-source field is held *below* its raw effective
# confidence (authority x extraction). 0.95 is a deliberately simple heuristic
# (not calibrated): it nudges marginal single-source fields (effective conf in
# [0.75, 0.75/0.95) = [0.75, 0.789)) toward the review queue while leaving
# clearly strong ones verifiable. See README "Confidence model".
SINGLE_SOURCE_FACTOR = 0.95
AGREE_BOOST_PER_SOURCE = 0.08      # +confidence per agreeing extra source
MAX_CONFIDENCE = 0.98
CONFLICT_CONFIDENCE = 0.40         # unresolved conflicts never auto-publish

# ---------------------------------------------------------------------------
# Number + unit normalization
# ---------------------------------------------------------------------------

UNIT_ALIASES = {
    "r/min": "rpm", "min-1": "rpm", "min⁻¹": "rpm", "r.p.m.": "rpm", "rpm": "rpm",
    "kn": "kN", "n": "N", "lbf": "lbf",
    "kg": "kg", "g": "g", "mg": "mg", "lb": "lb", "lbs": "lb", "oz": "oz",
    "mm": "mm", "cm": "cm", "m": "m", "in": "in", "inch": "in", "inches": "in", "ft": "ft",
    "°c": "°C", "c": "°C", "degc": "°C", "deg c": "°C",
    "°f": "°F", "f": "°F", "degf": "°F", "deg f": "°F", "k": "K",
    "bar": "bar", "mbar": "mbar", "psi": "psi", "mpa": "MPa", "kpa": "kPa", "pa": "Pa", "atm": "atm",
    "kw": "kW", "w": "W", "mw": "MW", "hp": "hp",
    "nm": "Nm", "n.m": "Nm", "n·m": "Nm", "knm": "kNm",
    "ft-lb": "ft-lb", "ft lb": "ft-lb", "lbf-ft": "ft-lb", "lbf·ft": "ft-lb",
    "in-lb": "in-lb",
    "v": "V", "kv": "kV", "mv": "mV", "a": "A", "ma": "mA", "ka": "kA", "hz": "Hz",
}

UNIT_FACTORS = {
    # Force
    ("N", "kN"): 0.001,
    ("kN", "N"): 1000.0,
    ("lbf", "N"): 4.44822,
    ("lbf", "kN"): 0.00444822,
    # Mass
    ("g", "kg"): 0.001,
    ("kg", "g"): 1000.0,
    ("mg", "kg"): 0.000001,
    ("lb", "kg"): 0.453592,
    ("oz", "kg"): 0.0283495,
    # Dimensions / Length
    ("cm", "mm"): 10.0,
    ("m", "mm"): 1000.0,
    ("in", "mm"): 25.4,
    ("ft", "mm"): 304.8,
    ("mm", "cm"): 0.1,
    ("mm", "m"): 0.001,
    ("mm", "in"): 1.0 / 25.4,
    # Pressure
    ("psi", "bar"): 0.0689476,
    ("bar", "psi"): 14.5038,
    ("MPa", "bar"): 10.0,
    ("bar", "MPa"): 0.1,
    ("kPa", "bar"): 0.01,
    ("bar", "kPa"): 100.0,
    ("Pa", "bar"): 0.00001,
    ("atm", "bar"): 1.01325,
    # Power
    ("W", "kW"): 0.001,
    ("kW", "W"): 1000.0,
    ("MW", "kW"): 1000.0,
    ("hp", "kW"): 0.7457,
    ("kW", "hp"): 1.0 / 0.7457,
    # Torque
    ("ft-lb", "Nm"): 1.355818,
    ("in-lb", "Nm"): 0.1129848,
    ("kNm", "Nm"): 1000.0,
    # Frequency to speed
    ("Hz", "rpm"): 60.0,
    # Electrical
    ("kV", "V"): 1000.0,
    ("mV", "V"): 0.001,
    ("mA", "A"): 0.001,
    ("kA", "A"): 1000.0,
}


def _alias_unit(u) -> str:
    if not isinstance(u, str) or not u.strip():
        return ""
    return UNIT_ALIASES.get(u.strip().casefold(), u.strip())


def _convert_temperature(val: float, from_u: str, to_u: str) -> float:
    f, t = _alias_unit(from_u), _alias_unit(to_u)
    if not f or not t or f == t:
        return val
    # Normalize to Celsius first
    c = val
    if f == "°F":
        c = (val - 32.0) * (5.0 / 9.0)
    elif f == "K":
        c = val - 273.15
    # Convert Celsius to target
    if t == "°F":
        return c * (9.0 / 5.0) + 32.0
    elif t == "K":
        return c + 273.15
    return c


def _unit_factor(from_u, to_u) -> float:
    f, t = _alias_unit(from_u), _alias_unit(to_u)
    if not f or f == t:
        return 1.0
    if (f, t) in UNIT_FACTORS:
        return UNIT_FACTORS[(f, t)]
    if (t, f) in UNIT_FACTORS:
        return 1.0 / UNIT_FACTORS[(t, f)]
    return 1.0


def parse_number(text) -> float | None:
    """Parse messy numbers: '12 000', '7 800', '14,0', '12,000', '0.127'."""
    s = str(text).replace("\u00a0", " ").strip()
    if not s:
        return None
    s = s.replace(" ", "").replace("'", "")
    if "," in s and "." in s:
        s = s.replace(",", "")
    elif "," in s:
        if re.fullmatch(r"-?\d{1,3},\d{3}(\.\d+)?", s):
            s = s.replace(",", "")      # 12,000 -> 12000
        else:
            s = s.replace(",", ".")     # 14,0 -> 14.0
    try:
        return float(s)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Synonym maps + OCR fuzzy matching + field normalization
# ---------------------------------------------------------------------------

SEAL_SUFFIX_RE = re.compile(r"[-/]?(?:2RSH?|2RS1?|2Z|ZZ|RS|2RU|2LS|NSE|DDU)$", re.I)

ENUM_SYNONYMS = {
    "seal_type": {
        "contact seal": "2RS", "contact seal, rs1 on both sides": "2RS",
        "rs1": "RS", "rs": "RS", "2rs1": "2RS", "2rsh": "2RS", "2rs": "2RS",
        "2r5": "2RS",  # OCR digit confusion: 5 read as S
        "2z": "2Z", "zz": "2Z", "2ru": "2RU", "open": "open", "no seal": "open",
    },
    "clearance": {
        "c0": "C0", "c2": "C2", "cn": "CN", "normal": "CN",
        "c3": "C3", "c4": "C4", "c5": "C5",
    },
    "actuation": {
        "manual lever": "manual", "lever": "manual", "hand": "manual",
        "pneumatic actuator": "pneumatic", "air": "pneumatic",
        "electric actuator": "electric", "motorised": "electric", "motorized": "electric",
    },
}

OCR_CHAR_SUBS = {
    "0": "o", "1": "l", "5": "s", "8": "b", "2": "z",
}


def levenshtein_distance(s1: str, s2: str) -> int:
    """Compute standard Levenshtein edit distance between two strings."""
    if s1 == s2:
        return 0
    if len(s1) < len(s2):
        return levenshtein_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)
    prev = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr = [i + 1]
        for j, c2 in enumerate(s2):
            ins = prev[j + 1] + 1
            dels = curr[j] + 1
            subs = prev[j] + (c1 != c2)
            curr.append(min(ins, dels, subs))
        prev = curr
    return prev[-1]


def _fuzzy_match_enum(val_str: str, allowed_tokens: set[str], synonyms: dict[str, str]) -> str | None:
    """Fuzzy matching & OCR typo correction for enum values."""
    v = val_str.strip().casefold()
    if not v:
        return None
    
    # 1. Direct synonym lookup
    if v in synonyms:
        return synonyms[v]
    
    # 2. Check direct case-insensitive match against allowed
    for a in allowed_tokens:
        if v == a.casefold():
            return a
            
    # 3. OCR character normalization (e.g. 2R5 -> 2rs, manua1 -> manual)
    normalized_ocr = v
    for char, rep in OCR_CHAR_SUBS.items():
        normalized_ocr = normalized_ocr.replace(char, rep)
    if normalized_ocr in synonyms:
        return synonyms[normalized_ocr]
    for a in allowed_tokens:
        if normalized_ocr == a.casefold():
            return a
            
    # 4. Levenshtein distance check against allowed tokens and synonyms
    candidates = {}
    for syn_key, target in synonyms.items():
        dist = levenshtein_distance(v, syn_key)
        max_dist = 1 if len(syn_key) <= 4 else 2
        if dist <= max_dist:
            candidates[target] = min(candidates.get(target, 99), dist)
            
    for a in allowed_tokens:
        dist = levenshtein_distance(v, a.casefold())
        max_dist = 1 if len(a) <= 4 else 2
        if dist <= max_dist:
            candidates[a] = min(candidates.get(a, 99), dist)
            
    # If exactly one unique canonical target is closest, accept it
    if len(candidates) == 1:
        return next(iter(candidates.keys()))
    elif len(candidates) > 1:
        sorted_c = sorted(candidates.items(), key=lambda t: t[1])
        if sorted_c[0][1] < sorted_c[1][1]:
            return sorted_c[0][0]
            
    return None


def normalize_value(field_schema: dict, raw_value, raw_unit):
    """Return a comparable normalized key, or None if unparseable.

    Numbers -> float in the schema unit (unit conversions applied).
    Enums   -> canonical enum token (synonym & fuzzy OCR resolved).
    Strings -> casefolded, whitespace-collapsed (part_number strips the
               seal suffix so 6205-2RSH / 6205-2RS agree).
    """
    ftype = field_schema["type"]
    name = field_schema["name"]
    target_unit = field_schema.get("unit")
    
    if ftype == "number":
        num = parse_number(raw_value)
        if num is None:
            return None
        # Handle temperature conversions with offsets
        if target_unit and _alias_unit(target_unit) in ("°C", "°F", "K") and raw_unit and _alias_unit(raw_unit) in ("°C", "°F", "K"):
            converted = _convert_temperature(num, raw_unit, target_unit)
            return round(converted, 6)
        factor = _unit_factor(raw_unit, target_unit)
        return round(num * factor, 6)
        
    if ftype == "enum":
        v = str(raw_value).strip()
        syn = ENUM_SYNONYMS.get(name, {})
        allowed = set(field_schema.get("enum", []))
        
        fuzzy_resolved = _fuzzy_match_enum(v, allowed, syn)
        if fuzzy_resolved is not None:
            return fuzzy_resolved
        return v.casefold()
        
    s = re.sub(r"\s+", " ", str(raw_value)).strip().casefold()
    if name == "part_number":
        s = SEAL_SUFFIX_RE.sub("", s).rstrip(" -/").casefold()
    return s


def _display_value(field_schema: dict, normalized_key, best_fv: FieldValue):
    """Human-friendly merged value.

    Numbers -> normalized value, whole numbers as ints (25.0 -> 25).
    Enums   -> the canonical schema token (synonym-resolved), so the displayed
              value always matches what validation checks; an unknown value
              keeps the raw source text so it stays readable (and is flagged
              by the enum validation rule).
    Strings -> raw text of the highest-confidence source.
    """
    ftype = field_schema["type"]
    if ftype == "number":
        n = normalized_key
        return int(n) if isinstance(n, float) and n.is_integer() else n
    if ftype == "enum":
        for opt in field_schema.get("enum", []):
            if opt.casefold() == str(normalized_key).casefold():
                return opt
        return str(best_fv.value).strip()
    return str(best_fv.value).strip()


def _values_agree(field_schema: dict, a, b) -> bool:
    """Tolerance check used for number clustering.

    Relative 1% tolerance (of the larger magnitude) with a 0.05 absolute floor
    so near-zero values where relative tolerance would be meaningless still
    cluster. Heuristic, not calibrated — like the confidence constants, it is
    deliberately simple and documented in the README.
    """
    if a is None or b is None:
        return False
    if field_schema["type"] == "number":
        return abs(a - b) <= max(0.01 * max(abs(a), abs(b)), 0.05)
    return a == b

# ---------------------------------------------------------------------------
# Cross-source merge
# ---------------------------------------------------------------------------


def merge_field(field_schema: dict, fvalues: list[FieldValue], authority: dict) -> MergedField | None:
    """Merge per-source extractions of one field.

    - Normalize every value; drop unparseable ones.
    - All agree  -> merged value, confidence boosted per agreeing source.
    - Any disagree -> unresolved conflict: needs_review, confidence 0.4.
    - No usable values -> None (field omitted, never fabricated).
    """
    if not fvalues:
        return None

    normed: list[tuple[FieldValue, object, float]] = []
    dropped: list[FieldValue] = []
    for fv in fvalues:
        key = normalize_value(field_schema, fv.value, fv.unit)
        if key is None:
            fv.reasoning = (fv.reasoning + ' ' if fv.reasoning else '') + '[unparseable value excluded from merge]'
            dropped.append(fv)
            continue
        eff = authority.get(fv.source_id, 0.8) * fv.confidence
        normed.append((fv, key, eff))
    if not normed:
        return None

    # --- tolerance grouping -------------------------------------------------
    # Numbers are clustered with _values_agree (relative 1% / 0.05 floor, see
    # its docstring), so near-equal values like 24.96 and 25.0 merge instead of
    # conflicting; strings/enums group on exact equality. A value joins a group
    # only if it agrees with BOTH current bounds (min & max), which keeps the
    # cluster's spread bounded and independent of source ordering.
    groups: list = []              # [[min_key, max_key, [(fv, eff), ...]], ...]
    for fv, key, eff in normed:
        for g in groups:
            if _values_agree(field_schema, g[0], key) and _values_agree(field_schema, g[1], key):
                g[0], g[1] = min(g[0], key), max(g[1], key)
                g[2].append((fv, eff))
                break
        else:
            groups.append([key, key, [(fv, eff)]])

    sources = sorted(normed, key=lambda t: t[2], reverse=True)
    name, label = field_schema["name"], field_schema.get("label", field_schema["name"])

    if len(groups) == 1:
        best_fv, best_eff = sources[0][0], sources[0][2]
        best_key = sources[0][1]               # strongest source's normalized key (displayed)
        n = len(normed)
        conf = min(MAX_CONFIDENCE, best_eff + AGREE_BOOST_PER_SOURCE * (n - 1))
        conf = round(conf * (SINGLE_SOURCE_FACTOR if n == 1 else 1.0), 3)
        flags = ["multi_source"] if n > 1 else ["single_source"]
        if n > 1:
            reasoning = (
                f"{n} sources agree on {label} (normalized {best_key!r}); "
                f"confidence boosted from {best_eff:.2f} to {conf:.2f}."
            )
        else:
            reasoning = (
                f"Single source ({best_fv.source_id}) provides {label}; "
                f"effective confidence {conf:.2f}."
            )
        return MergedField(
            field=name,
            label=label,
            unit=field_schema.get("unit"),
            value=_display_value(field_schema, best_key, best_fv),
            confidence=conf,
            status="pending",
            flags=flags,
            sources=[fv for fv, _k, _e in normed],
            dropped=dropped,
            conflicts=[],
            reasoning=reasoning,
        )

    # --- unresolved conflict -------------------------------------------------
    conflict_values = [
        {"source_id": fv.source_id, "value": str(fv.value), "unit": fv.unit or field_schema.get("unit")}
        for fv, _k, _e in normed
    ]
    reason = (
        f"Sources disagree on {label}: "
        + "; ".join(
            f"{fv.source_id}={fv.value} {fv.unit or ''}".strip() for fv, _k, _e in normed
        )
    )
    return MergedField(
        field=name,
        label=label,
        unit=field_schema.get("unit"),
        value=None,
        confidence=CONFLICT_CONFIDENCE,
        status="needs_review",
        flags=["conflict"],
        sources=[fv for fv, _k, _e in normed],
        dropped=dropped,
        conflicts=[Conflict(field=name, reason=reason, values=conflict_values)],
        reasoning=reason,
    )


# ---------------------------------------------------------------------------
# Confidence routing (step 4: don't auto-publish low-confidence/conflicted)
# ---------------------------------------------------------------------------


def assign_status(mf: MergedField, issues: list) -> None:
    """Final status: verified only if no conflict, no validation error and
    confidence >= VERIFIED_THRESHOLD. Anything else lands in needs_review."""
    if "conflict" in mf.flags:
        mf.status = "needs_review"
        return
    if issues:
        mf.status = "needs_review"
        mf.flags.append("validation")
        mf.validation_issues = [i.to_dict() for i in issues]
        return
    if mf.confidence < VERIFIED_THRESHOLD:
        mf.status = "needs_review"
        mf.flags.append("low_confidence")
        return
    mf.status = "verified"


def needs_review_queue(fields: list[MergedField]) -> list[dict]:
    """The human review queue: conflicts first, then lowest confidence."""
    q = []
    for f in fields:
        if f.status != "needs_review":
            continue
        if "conflict" in f.flags:
            reason = f.reasoning
        elif "validation" in f.flags:
            reason = "; ".join(i["message"] for i in f.validation_issues)
        elif "low_confidence" in f.flags:
            reason = (
                f"Only {len(f.sources)} source(s), effective confidence "
                f"{f.confidence:.2f} < {VERIFIED_THRESHOLD:.2f} threshold"
            )
        else:
            reason = f.reasoning
        q.append({
            "field": f.field,
            "label": f.label,
            "confidence": f.confidence,
            "flags": list(f.flags),
            "reason": reason,
            "value": f.value,
            "sources": [s.source_id for s in f.sources],
        })
    q.sort(key=lambda x: (0 if "conflict" in x["flags"] else 1, x["confidence"]))
    return q
