"""Pipeline unit tests (stdlib-only; no pytest required).

Run:  python -m tests.test_pipeline
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import merge as merge_mod
from pipeline import validate as validate_mod
from pipeline.llm import _parse_llm_response, build_output_schema, load_prompt_template, render_prompt
from pipeline.models import FieldValue
from pipeline.run import run_pipeline

SCHEMA = json.loads((ROOT / "schema" / "bearing_schema.json").read_text(encoding="utf-8"))
FS = {f["name"]: f for f in SCHEMA["fields"]}
VALVE_SCHEMA = json.loads((ROOT / "schema" / "valve_schema.json").read_text(encoding="utf-8"))
VFS = {f["name"]: f for f in VALVE_SCHEMA["fields"]}
GEARBOX_SCHEMA = json.loads((ROOT / "schema" / "gearbox_schema.json").read_text(encoding="utf-8"))
GFS = {f["name"]: f for f in GEARBOX_SCHEMA["fields"]}
AUTH = {"src_a": 0.95, "src_b": 0.85, "src_c": 0.7}
AUTH_VALVE = {"src_datasheet": 0.95, "src_web": 0.85}


def fv(field, value, unit=None, conf=0.95, sid="src_a", method="table_parse", snippet="snippet", line=1):
    return FieldValue(field, value, unit, conf, method, sid, snippet, line, line, "")


def test_number_normalization():
    assert merge_mod.parse_number("12 000") == 12000.0
    assert merge_mod.parse_number("7 800") == 7800.0
    assert merge_mod.parse_number("14,0") == 14.0
    assert merge_mod.parse_number("12,000") == 12000.0
    assert merge_mod.parse_number("0.127") == 0.127


def test_unit_conversion():
    fs = FS["dynamic_load_rating"]
    assert merge_mod.normalize_value(fs, "14000", "N") == 14.0
    assert merge_mod.normalize_value(fs, "14.0", "kN") == 14.0
    fs2 = FS["max_speed"]
    assert merge_mod.normalize_value(fs2, "13000", "r/min") == 13000.0


def test_agreement_boost():
    a = fv("bore_diameter", "25", "mm", sid="src_a")
    b = fv("bore_diameter", "25.0", "mm", sid="src_b")
    mf = merge_mod.merge_field(FS["bore_diameter"], [a, b], AUTH)
    assert mf.value == 25.0
    assert mf.confidence > 0.9
    assert "multi_source" in mf.flags


def test_conflict_flagging():
    a = fv("max_speed", "13000", "rpm", sid="src_a")
    b = fv("max_speed", "12000", "rpm", sid="src_b")
    mf = merge_mod.merge_field(FS["max_speed"], [a, b], AUTH)
    assert mf.status == "needs_review"
    assert "conflict" in mf.flags
    assert mf.value is None
    assert len(mf.conflicts) == 1
    assert len(mf.conflicts[0].values) == 2


def test_omit_when_missing():
    assert merge_mod.merge_field(FS["material"], [], AUTH) is None


def test_part_number_most_specific_wins():
    a = fv("part_number", "6205-2RSH", None, sid="src_a")
    b = fv("part_number", "6205-2RS", None, sid="src_b")
    mf = merge_mod.merge_field(FS["part_number"], [a, b], AUTH)
    assert mf.status != "needs_review"
    assert mf.value == "6205-2RSH"


def test_validation_bore_lt_outer():
    a = fv("bore_diameter", "52", "mm")
    b = fv("outer_diameter", "25", "mm")
    mfs = [merge_mod.merge_field(FS["bore_diameter"], [a], AUTH),
           merge_mod.merge_field(FS["outer_diameter"], [b], AUTH)]
    passed, issues, _ = validate_mod.run_validation(mfs, SCHEMA)
    assert not passed
    assert any(i.rule_id == "bore_lt_outer" for i in issues)


def test_validation_temp_order():
    a = fv("temperature_range_min", "120", "°C")
    b = fv("temperature_range_max", "-20", "°C")
    mfs = [merge_mod.merge_field(FS["temperature_range_min"], [a], AUTH),
           merge_mod.merge_field(FS["temperature_range_max"], [b], AUTH)]
    passed, issues, _ = validate_mod.run_validation(mfs, SCHEMA)
    assert any(i.rule_id == "temp_min_lt_max" for i in issues)


def test_validation_skips_missing_fields():
    mf = merge_mod.merge_field(FS["weight"], [fv("weight", "0.127", "kg")], AUTH)
    passed, issues, _ = validate_mod.run_validation([mf], SCHEMA)
    assert passed and not issues


def test_low_confidence_routing():
    mf = merge_mod.merge_field(FS["precision_grade"], [fv("precision_grade", "ABEC 1", None, conf=0.55, sid="src_c")], AUTH)
    merge_mod.assign_status(mf, [])
    assert mf.status == "needs_review"
    assert "low_confidence" in mf.flags


def test_output_schema_and_prompt():
    oschema = build_output_schema(SCHEMA)
    assert oschema["properties"]["fields"]["type"] == "array"
    tpl = load_prompt_template()
    for variant in ("pdf", "web", "ocr"):
        prompt = render_prompt(tpl, variant, {"id": "src_x", "title": "T"},
                               "line1\nline2", SCHEMA, oschema)
        assert "line1" in prompt and "TARGET CATEGORY" in prompt


def test_full_demo_pipeline():
    record = run_pipeline(mode="mock")
    s = record.summary
    by = {f.field: f for f in record.fields}
    assert s["verified"] == 13 and s["needs_review"] == 2 and s["conflicts"] == 1
    assert s["omitted_not_found"] == ["material"]
    assert by["max_speed"].status == "needs_review" and "conflict" in by["max_speed"].flags
    assert by["precision_grade"].status == "needs_review"
    assert by["dynamic_load_rating"].value == 14.0
    assert by["bore_diameter"].value == 25.0
    for f in record.fields:
        for src in f.sources:
            assert src.snippet.strip()
    cited = {c["field"] for c in record.marketing["citations"]}
    verified = {f.field for f in record.fields if f.status == "verified"}
    assert cited <= verified
    assert record.marketing["description"].strip()


def test_line_numbered_source_format():
    from pipeline.sources import Block, line_numbered

    blocks = [Block(1, 1, "d [mm] 25"), Block(4, 4, "D [mm] 52")]
    out = line_numbered(blocks)
    assert "    1  d [mm] 25" in out
    assert "    4  D [mm] 52" in out

# ---------------------------------------------------------------------------
# Third category: electric motor (category-agnostic proof)
# ---------------------------------------------------------------------------


def test_third_category_pipeline():
    """The electric motor category runs end-to-end through the same pipeline."""
    record = run_pipeline(
        manifest_path=ROOT / "data" / "sample_motor" / "sources.json",
        schema_path=ROOT / "schema" / "motor_schema.json",
        mode="mock",
    )
    by = {f.field: f for f in record.fields}
    assert record.category == "electric_motor"
    assert by["part_number"].value == "ME-132S-4"
    assert by["power_kw"].value == 7.5
    assert by["rated_voltage"].value == 400
    assert by["rated_speed"].value == 1450
    assert by["efficiency"].value == 90.5
    assert by["protection_class"].value == "IP55"          # canonical enum token
    assert by["insulation_class"].value == "F"
    assert by["mounting"].value == "B3"
    assert by["frame_size"].value == "132S"
    assert by["part_number"].status == "verified"
    assert by["rated_voltage"].status == "verified"        # 3 sources agree
    assert record.validation["passed"]
    assert record.summary["verified"] == 13                # all fields verified
    assert record.summary["omitted_not_found"] == []
    assert "ME-132S-4" in record.marketing["description"]
    assert "7.5 kW" in record.marketing["description"]


# ---------------------------------------------------------------------------
# AI semantic review (pipeline/ai_review.py)
# ---------------------------------------------------------------------------


def test_ai_review_explains_conflict_and_low_confidence():
    """Mock reviewer explains every needs_review field with a suggested action."""
    record = run_pipeline(mode="mock")  # bearing: max_speed conflict + precision_grade low conf
    ai = record.ai_review
    assert ai and ai["mode"] == "mock"
    assert ai["fields_reviewed"] == 2
    assert not ai["all_clear"]
    by = {r["field"]: r for r in ai["reviews"]}
    assert "max_speed" in by and "precision_grade" in by
    # conflict review explains the disagreement and suggests checking the datasheet
    assert "13000" in by["max_speed"]["review"] or "disagree" in by["max_speed"]["review"]
    assert by["max_speed"]["suggested_action"].strip()
    assert by["precision_grade"]["review"] and by["precision_grade"]["suggested_action"]
    # reviews never change the routing status
    assert by["max_speed"]["flags"] == ["conflict"]
    # summary mentions counts
    assert "1 conflict" in ai["summary"] or "1 conflict(s)" in ai["summary"]


def test_ai_review_all_clear_when_nothing_flagged():
    """A fully-verified product gets an all-clear review, not silence."""
    record = run_pipeline(
        manifest_path=ROOT / "data" / "sample_valve" / "sources.json",
        schema_path=ROOT / "schema" / "valve_schema.json",
        mode="mock",
    )
    ai = record.ai_review
    assert ai["all_clear"] and ai["fields_reviewed"] == 0
    assert ai["reviews"] == []
    assert "verified" in ai["summary"]


def test_ai_review_mock_reviewer_direct():
    """The mock reviewer handles validation-flagged fields too."""
    from pipeline.ai_review import MockAIReviewer

    queue = [{
        "field": "seal_type", "label": "Seal type", "confidence": 0.6,
        "flags": ["validation"], "value": "FOO", "sources": ["src_pdf"],
        "reason": "seal_type 'FOO' not in schema enum ['open', 'RS', '2RS']",
    }]
    out = MockAIReviewer().review(queue, {"authority": {"src_pdf": 0.95}, "source_infos": []})
    assert out["fields_reviewed"] == 1
    assert "validation" in out["reviews"][0]["flags"]
    assert "enum" in out["reviews"][0]["review"] or "rule" in out["reviews"][0]["review"]


# ---------------------------------------------------------------------------
# Scalable catalog engine (pipeline/catalog.py)
# ---------------------------------------------------------------------------


def test_catalog_two_products():
    """The demo catalog runs all four categories and aggregates totals."""
    from pipeline.catalog import load_catalog, run_catalog

    cat = load_catalog(ROOT / "data" / "catalog.json")
    index = run_catalog(cat, mode="mock", max_workers=4, progress=False)
    t = index["totals"]
    assert t["products"] == 4 and t["ok"] == 4 and t["failed"] == 0
    assert t["verified"] == 13 + 9 + 13 + 11
    assert t["needs_review"] == 2 + 0 + 0 + 1
    assert t["conflicts"] == 1 + 0 + 0 + 1
    by = {p["product_id"]: p for p in index["products"]}
    assert by["6205-2RS"]["verified"] == 13
    assert by["V2000-BS"]["verified"] == 9
    assert by["ME-132S-4"]["verified"] == 13
    assert by["GRX-225-M"]["verified"] == 11


def test_catalog_parallel_matches_serial():
    """Parallel execution is deterministic: totals identical to serial."""
    from pipeline.catalog import load_catalog, run_catalog

    cat = load_catalog(ROOT / "data" / "catalog.json")
    serial = run_catalog(cat, mode="mock", max_workers=1, progress=False)
    parallel = run_catalog(cat, mode="mock", max_workers=4, progress=False)
    assert serial["totals"] == parallel["totals"]
    s_by = {p["product_id"]: p for p in serial["products"]}
    p_by = {p["product_id"]: p for p in parallel["products"]}
    for pid in s_by:
        assert s_by[pid]["verified"] == p_by[pid]["verified"]
        assert s_by[pid]["needs_review"] == p_by[pid]["needs_review"]


def test_catalog_error_isolation():
    """One failing product never aborts the rest of the catalog."""
    import tempfile

    from pipeline.catalog import run_catalog

    with tempfile.TemporaryDirectory() as tmp:
        cat = {"catalog_id": "t", "products": [
            {"product_id": "good",
             "manifest": ROOT / "data/sample/sources.json",
             "schema": ROOT / "schema/bearing_schema.json",
             "output": str(Path(tmp) / "good.json")},
            {"product_id": "bad",
             "manifest": ROOT / "data/sample/__missing__.json",
             "schema": ROOT / "schema/bearing_schema.json",
             "output": str(Path(tmp) / "bad.json")},
        ]}
        index = run_catalog(cat, mode="mock", max_workers=2, progress=False)
        assert index["totals"]["ok"] == 1 and index["totals"]["failed"] == 1
        by = {p["product_id"]: p for p in index["products"]}
        # successful rows are keyed by the manifest's authoritative product_id
        assert by["6205-2RS"]["status"] == "ok"
        # failed rows keep the catalog entry's product_id
        assert by["bad"]["status"] == "failed" and by["bad"]["error"]


def test_synthetic_catalog_runs_at_scale():
    """The synthetic generator produces varied products that all process."""
    import tempfile

    from pipeline.catalog import generate_synthetic_bearing_catalog, run_catalog

    with tempfile.TemporaryDirectory() as tmp:
        cat = generate_synthetic_bearing_catalog(3, out_dir=Path(tmp))
        index = run_catalog(cat, mode="mock", max_workers=3, progress=False)
        assert index["totals"]["ok"] == 3 and index["totals"]["failed"] == 0
        assert index["totals"]["verified"] >= 3 * 10  # each product verifies most fields
        by = {p["product_id"]: p for p in index["products"]}
        assert "6200-2RS" in by and "6202-2RS" in by      # distinct products, not copies
        assert by["6200-2RS"]["verified"] == by["6202-2RS"]["verified"]


def test_synthetic_catalog_beyond_series_length():
    """N > series length still yields unique ids, outputs and clean results."""
    import tempfile

    from pipeline.catalog import generate_synthetic_bearing_catalog, run_catalog

    with tempfile.TemporaryDirectory() as tmp:
        cat = generate_synthetic_bearing_catalog(14, out_dir=Path(tmp))  # 12-series + 2 more
        ids = [p["product_id"] for p in cat["products"]]
        outputs = [str(p["output"]) for p in cat["products"]]
        assert len(set(ids)) == 14          # no duplicate product ids
        assert len(set(outputs)) == 14      # no racing output files
        assert "6200-2RS-2" in ids          # second cycle gets a unique suffix
        index = run_catalog(cat, mode="mock", max_workers=8, progress=False)
        assert index["totals"]["ok"] == 14 and index["totals"]["failed"] == 0
        assert len(index["products"]) == 14


def test_catalog_index_and_csv_written():
    """Index JSON + CSV snapshot are written and match the in-memory totals."""
    import tempfile

    from pipeline.catalog import load_catalog, run_catalog, write_catalog_outputs

    cat = load_catalog(ROOT / "data" / "catalog.json")
    index = run_catalog(cat, mode="mock", max_workers=2, progress=False)
    with tempfile.TemporaryDirectory() as tmp:
        paths = write_catalog_outputs(index, output_dir=Path(tmp))
        idx = json.loads(Path(paths["index"]).read_text(encoding="utf-8"))
        assert idx["totals"] == index["totals"]
        csv_txt = Path(paths["csv"]).read_text(encoding="utf-8")
        rows = csv_txt.splitlines()
        assert rows[0].startswith("product_id")
        assert any("6205-2RS" in r and "13" in r for r in rows)
        assert any("V2000-BS" in r for r in rows)


def test_ingest_hp_kw_reconciled():
    """Live ingestion: a motor pasted with 15 HP (web) vs 11.2 kW (datasheet)
    merges to one power value after unit normalization (15 HP ~= 11.19 kW,
    inside the 1% agreement tolerance) - while a speed mismatch (1460 vs 1500
    rpm) is flagged as a conflict for the review queue."""
    import tempfile

    from pipeline.ingest import ingest_product

    sources = [
        {"id": "src_pdf", "type": "pdf", "title": "Datasheet",
         "text": "Part no.  ME-TEST-1\nOutput power  11.2 kW\nRated voltage  400 V\n"
                  "Rated current  21.5 A\nRated speed  1460 r/min\nFrame size  160M\n"
                  "Protection  IP55\nInsulation class  F\nMounting  B3"},
        {"id": "src_web", "type": "web", "title": "Distributor",
         "text": "ME-TEST-1 Induction Motor\nOutput power  |  15 HP\n"
                  "Rated speed  |  1500 rpm\nProtection  |  IP55"},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        res = ingest_product("ME-TEST-1", "electric_motor", sources, mode="mock",
                             out_root=Path(tmp), output_dir=Path(tmp),
                             ui_path=Path(tmp) / "ui.html", embed=True)
    by = {f["field"]: f for f in res["record"]["fields"]}
    p = by["power_kw"]
    assert p["value"] == 11.2 and p["unit"] == "kW"      # 15 HP reconciled to kW
    assert p["status"] == "verified" and "multi_source" in p["flags"]
    assert by["rated_speed"]["value"] is None              # unresolved conflict
    assert "conflict" in by["rated_speed"]["flags"]
    assert by["rated_speed"]["status"] == "needs_review"
    assert res["ok"] and res["record"]["product_id"] == "ME-TEST-1"


def test_ingest_psi_bar_reconciled():
    """Live ingestion: valve datasheet in bar vs distributor page in PSI
    reconcile to a single pressure_rating value (580 PSI ~= 39.99 bar)."""
    import tempfile

    from pipeline.ingest import ingest_product

    sources = [
        {"id": "src_pdf", "type": "pdf", "title": "Datasheet",
         "text": "Part no.  V-TEST-1\nBrand  AcmeFlow\nDN [mm]  50\nPressure rating  40 bar\n"
                  "Body material  stainless_steel\nActuation  manual\nWeight  3.4 kg"},
        {"id": "src_web", "type": "web", "title": "Distributor",
         "text": "V-TEST-1 Two-Way Ball Valve\nConnection  |  DN 50 mm\n"
                  "Pressure rating  |  580 PSI\nBody material  |  stainless steel\n"
                  "Actuation  |  manual lever"},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        res = ingest_product("V-TEST-1", "2_way_ball_valve", sources, mode="mock",
                             out_root=Path(tmp), output_dir=Path(tmp),
                             ui_path=Path(tmp) / "ui.html", embed=True)
    by = {f["field"]: f for f in res["record"]["fields"]}
    pr = by["pressure_rating"]
    assert pr["value"] == 40 and pr["unit"] == "bar"
    assert pr["status"] == "verified" and "multi_source" in pr["flags"]
    assert by["actuation"]["value"] == "manual"           # 'manual lever' synonym
    assert by["body_material"]["value"] == "stainless_steel"


def test_ingest_embeds_into_ui_catalog_and_records():
    """Ingestion writes the product record, updates the catalog index and
    merges the product into the embedded CATALOG/RECORDS blocks of the UI."""
    import tempfile

    from pipeline.ingest import ingest_product

    with tempfile.TemporaryDirectory() as tmp:
        ui = Path(tmp) / "ui.html"
        ui.write_text(
            "const SAMPLE = {};\n\n"
            "const CATALOG = {\"catalog_id\":\"demo\",\"name\":\"Demo\","
            "\"pipeline\":{},\"totals\":{},\"products\":[]};\n"
            "const RECORDS = {};\n\nconst $ = (id) => null;\n",
            encoding="utf-8",
        )
        sources = [
            {"id": "src_pdf", "type": "pdf", "title": "Datasheet",
             "text": "Part no.  V-TEST-2\nDN [mm]  25\nPressure rating  40 bar\n"
                      "Body material  brass\nActuation  manual\n"
                      "Weight  0.9 kg }; extra note\n"},   # snippet deliberately
                                                          # contains '};'
        ]
        res = ingest_product("V-TEST-2", "2_way_ball_valve", sources, mode="mock",
                             out_root=Path(tmp), output_dir=Path(tmp),
                             ui_path=ui, embed=True)
        html = ui.read_text(encoding="utf-8")
        assert "V-TEST-2" in res["records"]
        assert "const CATALOG = {" in html and '"V-TEST-2"' in html
        assert html.index("const CATALOG =") < html.index("const RECORDS =")
        assert "const $" in html                      # marker untouched
        assert res["catalog"]["totals"]["products"] == 1
        # the '};' inside a JSON string must not truncate the embedded blocks:
        # the record's weight snippet carries it through and the re-embedded
        # RECORDS still parses as JSON with the full product intact.
        wt = next(f for f in res["records"]["V-TEST-2"]["fields"] if f["field"] == "weight")
        assert "};" in wt["sources"][0]["snippet"]
        from pipeline.ingest import _extract_embedded
        recs2 = _extract_embedded(html, "RECORDS")
        assert recs2 and recs2["V-TEST-2"]["product_id"] == "V-TEST-2"
        # catalog index JSON + CSV persisted alongside
        idx = json.loads((Path(tmp) / "catalog_index.json").read_text(encoding="utf-8"))
        assert idx["products"][0]["product_id"] == "V-TEST-2"
        assert (Path(tmp) / "catalog_index.csv").exists()


def test_ingest_validates_input():
    """Ingestion refuses missing SKU / unknown category / empty snippets."""
    import tempfile

    from pipeline.ingest import ingest_product

    with tempfile.TemporaryDirectory() as tmp:
        for kwargs in [
            {"product_id": "", "category": "electric_motor",
             "sources": [{"text": "x"}]},
            {"product_id": "X-1", "category": "not_a_category",
             "sources": [{"text": "x"}]},
            {"product_id": "X-1", "category": "electric_motor",
             "sources": [{"text": ""}]},
        ]:
            try:
                ingest_product(mode="mock", out_root=Path(tmp), output_dir=Path(tmp),
                               ui_path=Path(tmp) / "ui.html", **kwargs)
                raise AssertionError(f"expected ValueError for {kwargs}")
            except ValueError:
                pass


def test_merge_ftlb_nm_reconciled():
    """A gearbox torque in ft-lb (332) and N·m (450) reconcile to one value
    (332 ft-lb == 450.13 N·m, inside the 1% agreement tolerance)."""
    a = fv("rated_torque", "450", "Nm", sid="src_a")
    b = fv("rated_torque", "332", "ft-lb", sid="src_b")
    mf = merge_mod.merge_field(GFS["rated_torque"], [a, b], AUTH)
    assert mf.status != "needs_review" and "multi_source" in mf.flags
    # merged value is the best source's normalized value (450 N·m datasheet,
    # authority 0.95 > 0.85), not the converted 450.13 of the ft-lb source
    assert mf.unit == "Nm" and (mf.value == 450 or abs(mf.value - 450.13) < 0.01)
    # both raw source units survive into the record, so the UI can render the
    # conversion diff (332 ft-lb -> 450 N·m) without re-running the pipeline
    assert {s.unit for s in mf.sources} == {"Nm", "ft-lb"}


def test_ingest_gearbox_torque_reconciled():
    """Live ingestion of the 4th category: a gear reducer whose datasheet
    quotes 450 N·m but whose distributor page quotes 332 ft-lb merges to one
    verified torque; the mismatched output speed (58 vs 60 rpm) is flagged as
    a conflict for review."""
    import tempfile

    from pipeline.ingest import ingest_product

    sources = [
        {"id": "src_pdf", "type": "pdf", "title": "Datasheet",
         "text": "Part no.  GRX-TEST\nBrand  HelmDrive\nRatio  25 : 1\n"
                  "Input speed  1450 r/min\nOutput speed  58 r/min\n"
                  "Rated torque  450 N·m\nService factor  1.4\nEfficiency  94 %\n"
                  "Mounting  B3\nProtection  IP55\nLubrication  oil\nMass  62 kg"},
        {"id": "src_web", "type": "web", "title": "Distributor",
         "text": "GRX-TEST Gear Reducer\nRatio  |  25 : 1\nInput speed  |  1450 rpm\n"
                  "Output speed  |  60 rpm\nRated torque  |  332 ft-lb\n"
                  "Service factor  |  1.4\nEfficiency  |  94%\nMounting  |  B3\n"
                  "Protection  |  IP55\nLubrication  |  oil\nWeight  |  62 kg"},
        {"id": "src_ocr", "type": "ocr", "title": "Nameplate",
         "text": "GRX-TEST\n25 : 1\n450 N·m\nIP55\n62 kg"},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        res = ingest_product("GRX-TEST", "helical_gear_reducer", sources, mode="mock",
                             out_root=Path(tmp), output_dir=Path(tmp),
                             ui_path=Path(tmp) / "ui.html", embed=True)
    by = {f["field"]: f for f in res["record"]["fields"]}
    tq = by["rated_torque"]
    assert tq["value"] == 450 and tq["unit"] == "Nm"
    assert tq["status"] == "verified" and "multi_source" in tq["flags"]
    # the ft-lb source unit survives in the record, so the UI can render the
    # conversion diff (332 ft-lb -> 450 N·m) without re-running the pipeline
    assert "ft-lb" in {s["unit"] for s in tq["sources"]}
    assert by["ratio"]["value"] == 25            # 3 sources agree (pdf/web/ocr)
    assert by["output_speed"]["status"] == "needs_review"
    assert "conflict" in by["output_speed"]["flags"]
    assert by["output_speed"]["value"] is None
    assert by["mounting"]["value"] == "B3"       # enum canonical token
    assert res["record"]["summary"]["verified"] == 11
    assert res["record"]["summary"]["conflicts"] == 1


def test_ingest_stress_bearing_messy():
    """The 'stress' bearing case: thousands separators, a comma decimal
    (14,0), an OCR typo (2R5 -> 2RS) and an imperial weight (0.28 lb) all get
    parsed and normalized before agreement is checked."""
    import tempfile

    from pipeline.ingest import ingest_product

    sources = [
        {"id": "src_pdf", "type": "pdf", "title": "Datasheet",
         "text": "SKF 6208-2RSH Deep groove ball bearing\nd [mm] 40\nD [mm] 80\nB [mm] 18\n"
                  "Basic dynamic load rating C 14,0 kN\nBasic static load rating C0 7,8 kN\n"
                  "Limiting speed 12 000 r/min\nMass 0.28 lb\nSeal  Contact seal, RS1 on both sides\n"
                  "Internal clearance  CN"},
        {"id": "src_web", "type": "web", "title": "Distributor",
         "text": "6208-2RS Ball Bearing\nBore: 40 mm, OD: 80 mm, Width: 18 mm.\n"
                  "Dynamic load rating  |  14 000 N\nStatic load rating  |  7 800 N\n"
                  "Max speed (grease)  |  12 000 rpm\nOperating temperature  |  -20 °C to +120 °C\n"
                  "Seal  |  2RS (rubber contact)\nInternal clearance  |  CN\nMade in  |  Japan"},
        {"id": "src_ocr", "type": "ocr", "title": "Noisy photo OCR",
         "text": "40x80x18\n2R5\nJAPAN\nABEC 1"},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        res = ingest_product("6208-2RS", "deep_groove_ball_bearing", sources, mode="mock",
                             out_root=Path(tmp), output_dir=Path(tmp),
                             ui_path=Path(tmp) / "ui.html", embed=True)
    by = {f["field"]: f for f in res["record"]["fields"]}
    assert by["dynamic_load_rating"]["value"] == 14        # 14,0 kN + 14 000 N
    assert by["static_load_rating"]["value"] == 7.8
    assert by["max_speed"]["value"] == 12000               # 12 000 r/min + 12 000 rpm
    w = by["weight"]["value"]
    assert abs(w - 0.127006) < 0.001 and by["weight"]["unit"] == "kg"   # 0.28 lb
    assert by["seal_type"]["value"] == "2RS"              # OCR typo 2R5 fuzzy-resolved
    assert by["seal_type"]["status"] == "verified"
    assert by["precision_grade"]["status"] == "needs_review"   # noisy OCR only
    assert res["record"]["summary"]["conflicts"] == 0


def test_ingest_imperial_valve():
    """An imperial distributor page (1.97 in · 580 PSI · 7.5 lb) reconciles
    with a metric datasheet (DN 50 · 40 bar · 3.4 kg) to one set of values."""
    import tempfile

    from pipeline.ingest import ingest_product

    sources = [
        {"id": "src_pdf", "type": "pdf", "title": "Metric datasheet",
         "text": "Part no.  V-IM-1\nBrand  AcmeFlow\nDN [mm]  50\nPressure rating  40 bar\n"
                  "Body material  stainless_steel\nActuation  manual\n"
                  "Operating temperature  -20 °C to 150 °C\nWeight  3.4 kg"},
        {"id": "src_web", "type": "web", "title": "Imperial distributor",
         "text": "V-IM-1 Two-Way Ball Valve\nConnection  |  1.97 in\nPressure rating  |  580 PSI\n"
                  "Body material  |  stainless steel\nActuation  |  manual lever\n"
                  "Operating temperature  |  -20 °C to 150 °C\nWeight  |  7.5 lb"},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        res = ingest_product("V-IM-1", "2_way_ball_valve", sources, mode="mock",
                             out_root=Path(tmp), output_dir=Path(tmp),
                             ui_path=Path(tmp) / "ui.html", embed=True)
    by = {f["field"]: f for f in res["record"]["fields"]}
    assert by["connection_size"]["value"] == 50            # 1.97 in -> 50.04 mm agrees with DN 50
    assert by["pressure_rating"]["value"] == 40            # 580 PSI -> 39.99 bar
    w = by["weight"]["value"]
    assert abs(w - 3.4) < 0.01 and by["weight"]["unit"] == "kg"   # 7.5 lb -> 3.40 kg
    assert by["connection_size"]["status"] == "verified"
    # the raw imperial units survive in the record so the UI can render the
    # conversion diff (1.97 in -> 50 mm, 580 PSI -> 40 bar, 7.5 lb -> 3.4 kg)
    assert "in" in {s["unit"] for s in by["connection_size"]["sources"]}
    assert "lb" in {s["unit"] for s in by["weight"]["sources"]}
    assert res["record"]["summary"]["needs_review"] == 0


def _run_all():
    tests = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {name}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} tests passed")
    return 1 if failed else 0


def test_display_value_whole_number():
    """Whole numbers render as ints (25.0 -> 25); fractional stay floats."""
    mf = merge_mod.merge_field(FS["bore_diameter"], [fv("bore_diameter", "25", "mm")], AUTH)
    assert mf.value == 25 and type(mf.value) is int
    mf2 = merge_mod.merge_field(FS["weight"], [fv("weight", "0.127", "kg")], AUTH)
    assert mf2.value == 0.127 and type(mf2.value) is float


def test_tolerance_grouping():
    """Near-equal numbers cluster via _values_agree instead of conflicting."""
    a = fv("bore_diameter", "24.96", "mm", sid="src_a")
    b = fv("bore_diameter", "25.0", "mm", sid="src_b")
    mf = merge_mod.merge_field(FS["bore_diameter"], [a, b], AUTH)
    assert mf.status != "needs_review"
    assert "multi_source" in mf.flags
    assert mf.confidence > 0.9


def test_enum_unknown_value_routed_to_review():
    """An enum value not in the schema enum never auto-publishes; the generic
    per-schema enum rule flags it and the raw value is preserved (not dropped)."""
    mf = merge_mod.merge_field(FS["seal_type"], [fv("seal_type", "FOO", None)], AUTH)
    assert mf.value == "FOO"
    passed, issues, _ = validate_mod.run_validation([mf], SCHEMA)
    assert not passed
    assert any(i.rule_id == "seal_type_enum" for i in issues)
    merge_mod.assign_status(mf, issues)
    assert mf.status == "needs_review"
    assert "validation" in mf.flags


def test_enum_synonym_resolved_to_canonical_token():
    """'manual lever' synonym-resolves to the canonical 'manual' token, which
    is also what validation checks (raw spelling stays in the source cards)."""
    a = fv("actuation", "manual", None, conf=0.95, sid="src_datasheet")
    b = fv("actuation", "manual lever", None, conf=0.9, sid="src_web")
    mf = merge_mod.merge_field(VFS["actuation"], [a, b], AUTH_VALVE)
    assert mf.value == "manual"
    merge_mod.assign_status(mf, [])
    assert mf.status == "verified"


def test_dropped_unparseable_excluded_from_sources():
    """Unparseable extractions go to `dropped` (audit trail) and never count
    as contributing sources."""
    good = fv("bore_diameter", "25", "mm", sid="src_a")
    bad = fv("bore_diameter", "N/A", "mm", sid="src_b")
    mf = merge_mod.merge_field(FS["bore_diameter"], [good, bad], AUTH)
    assert len(mf.sources) == 1 and mf.sources[0].source_id == "src_a"
    assert len(mf.dropped) == 1 and mf.dropped[0].source_id == "src_b"
    assert "[unparseable" in mf.dropped[0].reasoning
    assert mf.value == 25


def test_parse_llm_response_malformed():
    """LLM adapter tolerates malformed/partial output without raising."""
    sid = {"id": "src_llm", "title": "T"}
    assert len(_parse_llm_response(None, sid, SCHEMA).fields) == 0
    assert len(_parse_llm_response({"fields": "oops"}, sid, SCHEMA).fields) == 0
    assert len(_parse_llm_response({"fields": [None, "nope"]}, sid, SCHEMA).fields) == 0
    # hallucinated field dropped; valid one kept with clamped/defaulted values
    data = {"fields": [
        {"field": "not_a_field", "value": "x", "confidence": 0.9, "method": "table_parse",
         "snippet": "s", "line_start": 1, "line_end": 1},
        {"field": "brand", "value": "ACME", "confidence": 2.5, "method": "hack",
         "snippet": "ACME", "line_start": "bad", "line_end": 1},
    ]}
    out = _parse_llm_response(data, sid, SCHEMA)
    assert len(out.fields) == 1 and out.fields[0].field == "brand"
    assert out.fields[0].confidence == 1.0          # clamped to [0, 1]
    assert out.fields[0].method == "llm_inference"  # unknown method defaulted
    assert out.fields[0].line_start == 0            # non-numeric span -> 0
    # wrong value types / empty values are skipped
    data2 = {"fields": [
        {"field": "brand", "value": {"nested": 1}, "confidence": 0.9, "method": "table_parse",
         "snippet": "s", "line_start": 1, "line_end": 1},
        {"field": "brand", "value": "", "confidence": 0.9, "method": "table_parse",
         "snippet": "s", "line_start": 1, "line_end": 1},
    ]}
    assert len(_parse_llm_response(data2, sid, SCHEMA).fields) == 0
    # numeric value coerced to string per the output schema
    data3 = {"fields": [{"field": "brand", "value": 42, "confidence": 0.9, "method": "table_parse",
                          "snippet": "s", "line_start": 1, "line_end": 1}]}
    assert _parse_llm_response(data3, sid, SCHEMA).fields[0].value == "42"


def test_second_category_pipeline():
    """The valve category runs end-to-end through the same pipeline,
    proving merge/validate/route/marketing are category-agnostic."""
    record = run_pipeline(
        manifest_path=ROOT / "data" / "sample_valve" / "sources.json",
        schema_path=ROOT / "schema" / "valve_schema.json",
        mode="mock",
    )
    by = {f.field: f for f in record.fields}
    assert record.category == "2_way_ball_valve"
    assert by["connection_size"].value == 25 and type(by["connection_size"].value) is int
    assert by["pressure_rating"].value == 40
    assert by["actuation"].value == "manual"        # synonym resolved
    assert by["body_material"].status == "verified"
    assert record.summary["verified"] == 9
    assert record.validation["passed"]
    assert record.marketing["description"].strip()
    assert "25 mm connection" in record.marketing["description"]


if __name__ == "__main__":
    raise SystemExit(_run_all())
