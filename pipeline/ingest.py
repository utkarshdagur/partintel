"""Ad-hoc product ingestion: turn pasted raw snippets (PDF datasheet text,
distributor web specs, photo OCR) into a structured, merged, reviewed product
record through the exact same stack as run_demo / run_catalog.

The demo UI's "➕ Ingest New Product" modal POSTs to the local server
(run_ingest.py), which calls :func:`ingest_product` here. Keeping the core
logic HTTP-free makes it unit-testable without sockets and identical to what
the CLI demo runs:

    ingest_product() -> pipeline: sources -> extraction -> merge (unit
    normalization, conflicts) -> validate -> route -> AI review -> marketing
                      -> write output/ingested/<pid>_product.json
                      -> upsert into output/catalog_index.json + CSV
                      -> merge into the embedded catalog dashboard of
                         ui/index.html (const CATALOG / const RECORDS)
"""
from __future__ import annotations

import datetime
import json
import re
from pathlib import Path
from typing import Optional

from .catalog import write_catalog_outputs
from .run import run_pipeline

BASE_DIR = Path(__file__).resolve().parent.parent

# category key -> schema path (relative to project root). Adding a category
# here automatically makes it selectable in the ingest modal.
CATEGORY_SCHEMAS = {
    "deep_groove_ball_bearing": "schema/bearing_schema.json",
    "2_way_ball_valve": "schema/valve_schema.json",
    "electric_motor": "schema/motor_schema.json",
    "helical_gear_reducer": "schema/gearbox_schema.json",
}

AUTHORITY_BY_TYPE = {"pdf": 0.95, "web": 0.85, "ocr": 0.70}

SOURCE_TYPE_LABELS = {
    "pdf": "PDF datasheet",
    "web": "Manufacturer / distributor web page",
    "ocr": "Product photo OCR",
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", text.strip()).strip(".-")
    return s or "product"


def _js_json(obj) -> str:
    """Compact JSON safe to inline into a <script> block."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).replace(
        "<", "\\u003c"
    )  # keep `</script>` and friends out of the page


def _nl(html: str) -> str:
    return "\r\n" if "\r\n" in html else "\n"


def _embedded_block_span(html: str, name: str):
    r"""Return (start, end) of the `const NAME = {...};` block, or None.

    Scans with brace depth + string-literal awareness instead of a naive
    `.*?\};` regex, so arbitrary text *inside* the JSON (e.g. a pasted source
    snippet that happens to contain `};`) can never truncate the block and
    corrupt ui/index.html.
    """
    m = re.search(r"const " + name + r"\s*=\s*\{", html)
    if not m:
        return None
    start = m.start()
    depth = 0
    in_str = False
    esc = False
    i = m.end() - 1  # position of the opening '{'
    n = len(html)
    while i < n:
        c = html[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    j = i + 1
                    while j < n and html[j] in " \t\r\n":
                        j += 1
                    if j < n and html[j] == ";":
                        return start, j + 1
                    return start, i + 1
        i += 1
    return None


def _extract_embedded(html: str, name: str):
    """Parse `const NAME = {...};` from ui/index.html (minified JSON object)."""
    span = _embedded_block_span(html, name)
    if not span:
        return None
    body = html[span[0]:span[1]]
    body = body[body.index("{"):].rstrip(";").strip()
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        return None


def _replace_embedded(html: str, name: str, obj, nl: str) -> str:
    """Replace (or insert before `const $`) the embedded block for `name`."""
    block = "const " + name + " = " + _js_json(obj) + ";"
    span = _embedded_block_span(html, name)
    if span:
        return html[:span[0]] + block + html[span[1]:]
    marker = "const $"
    if marker in html:
        return html.replace(marker, block + nl + marker, 1)
    return html


# ---------------------------------------------------------------------------
# Schema info (for the ingest modal: pick a category, see its target fields)
# ---------------------------------------------------------------------------


def load_schemas_info() -> dict:
    """{category: {category_label, fields: [{name,label,type,unit}, ...]}}."""
    out = {}
    for cat, rel in CATEGORY_SCHEMAS.items():
        schema = json.loads((BASE_DIR / rel).read_text(encoding="utf-8"))
        out[cat] = {
            "category_label": schema.get("category_label", cat),
            "fields": [
                {"name": f["name"], "label": f.get("label", f["name"]),
                 "type": f.get("type"), "unit": f.get("unit")}
                for f in schema["fields"]
            ],
        }
    return out


# ---------------------------------------------------------------------------
# Manifest building
# ---------------------------------------------------------------------------


def build_manifest(product_id: str, category: str, sources: list[dict],
                   out_root: Optional[Path] = None) -> Path:
    """Write the pasted sources to disk and return the manifest path.

    Each source dict: {id, type (pdf|web|ocr), title, text, authority?}.
    Plain web text is wrapped in <p> lines so parse_web_html's bs4 path (which
    only emits p/h/li/tr elements) keeps every line readable.
    """
    out_root = Path(out_root) if out_root is not None else BASE_DIR / "data" / "ingested"
    pdir = out_root / _slug(product_id)
    pdir.mkdir(parents=True, exist_ok=True)

    manifest_sources = []
    used_ids: set[str] = set()
    for i, src in enumerate(sources, start=1):
        text = (src.get("text") or "").strip()
        if not text:
            continue
        stype = src.get("type", "pdf")
        if stype == "web" and "<" not in text:
            text = "\n".join(
                "<p>" + ln.strip() + "</p>" for ln in text.splitlines() if ln.strip()
            )
        ext = ".html" if stype == "web" else ".txt"
        sid = src.get("id") or f"src_{stype}"
        if sid in used_ids:          # duplicate ids never collide on disk
            sid = f"{sid}-{i}"
        used_ids.add(sid)
        path = pdir / f"{_slug(sid)}{ext}"
        path.write_text(text, encoding="utf-8")
        manifest_sources.append({
            "id": sid,
            "type": stype,
            "title": src.get("title") or f"{SOURCE_TYPE_LABELS.get(stype, stype)} snippet",
            "authority": float(src.get("authority") or AUTHORITY_BY_TYPE.get(stype, 0.8)),
            "path": str(path),
        })

    manifest = {"product_id": product_id, "category": category,
                "sources": manifest_sources}
    manifest_path = pdir / "sources.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return manifest_path


# ---------------------------------------------------------------------------
# Catalog index upkeep (single source of truth: output/catalog_index.json,
# seeded from the embedded CATALOG block on first use so the 3 demo products
# are never lost).
# ---------------------------------------------------------------------------


def _product_row(record: dict, output: Path) -> dict:
    s = record["summary"]
    return {
        "product_id": record["product_id"],
        "category": record["category"],
        "category_label": record["category_label"],
        "status": "ok",
        "verified": s["verified"],
        "needs_review": s["needs_review"],
        "conflicts": s["conflicts"],
        "rules_run": s["rules_run"],
        "extracted": s["extracted"],
        "omitted_not_found": s["omitted_not_found"],
        "output": str(output),
        "error": None,
    }


def _totals(products: list[dict], mode: str) -> dict:
    ok = [p for p in products if p["status"] == "ok"]
    return {
        "products": len(products),
        "ok": len(ok),
        "failed": len(products) - len(ok),
        "verified": sum(p.get("verified", 0) for p in ok),
        "needs_review": sum(p.get("needs_review", 0) for p in ok),
        "conflicts": sum(p.get("conflicts", 0) for p in ok),
        "rules_run": sum(p.get("rules_run", 0) for p in ok),
    }


def _load_catalog_index(output_dir: Path) -> Optional[dict]:
    p = output_dir / "catalog_index.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def _upsert_catalog(index: Optional[dict], record: dict, output: Path) -> dict:
    """Add/replace the product in the catalog index and recompute totals."""
    products = [p for p in (index or {}).get("products", [])
                if p.get("product_id") != record["product_id"]]
    products.append(_product_row(record, output))
    prev = index or {}
    return {
        "catalog_id": prev.get("catalog_id", "demo"),
        "name": prev.get("name", "PartIntel demo catalog"),
        "pipeline": {
            "version": "1.0.0",
            "mode": record["pipeline"]["mode"],
            "max_workers": 1,
            "run_at": datetime.datetime.now(datetime.timezone.utc).isoformat(
                timespec="seconds"),
        },
        "totals": _totals(products, record["pipeline"]["mode"]),
        "products": products,
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def ingest_product(product_id: str, category: str, sources: list[dict],
                   mode: str = "auto",
                   out_root: Optional[Path] = None,
                   output_dir: Optional[Path] = None,
                   ui_path: Optional[Path] = None,
                   embed: bool = True) -> dict:
    """Run the full pipeline on pasted snippets and fold the result into the
    catalog + demo UI.

    Returns {"ok", "record", "output", "catalog", "records"} (JSON-serializable
    for the ingest HTTP endpoint).
    """
    product_id = (product_id or "").strip()
    category = (category or "").strip()
    if not product_id:
        raise ValueError("product_id (SKU / product name) is required")
    if category not in CATEGORY_SCHEMAS:
        raise ValueError(
            f"unknown category {category!r}; choose from {sorted(CATEGORY_SCHEMAS)}"
        )
    used = [s for s in sources if (s.get("text") or "").strip()]
    if not used:
        raise ValueError("at least one source snippet with text is required")

    output_dir = Path(output_dir) if output_dir is not None else BASE_DIR / "output"
    out = output_dir / "ingested" / f"{_slug(product_id)}_product.json"
    manifest_path = build_manifest(product_id, category, used, out_root)
    record = run_pipeline(
        manifest_path=manifest_path,
        schema_path=BASE_DIR / CATEGORY_SCHEMAS[category],
        output_path=str(out),
        mode=mode,
    )
    rec = record.to_dict()

    index = _upsert_catalog(_load_catalog_index(output_dir), rec, out)
    write_catalog_outputs(index, output_dir)

    # The response always carries the ingested record; the full records map
    # is seeded from the embedded dashboard when it can be read.
    records: dict = {rec["product_id"]: rec}
    if embed:
        ui = Path(ui_path) if ui_path is not None else BASE_DIR / "ui" / "index.html"
        try:
            html = ui.read_text(encoding="utf-8")
        except OSError:
            html = ""
        if html:
            nl = _nl(html)
            embedded_cat = _extract_embedded(html, "CATALOG")
            embedded_recs = _extract_embedded(html, "RECORDS") or {}
            # Merge with the currently embedded dashboard (preserves any
            # products not present in the persisted index), then persist.
            cat = _upsert_catalog(embedded_cat or index, rec, out)
            cat["products"] = [p for p in cat["products"]]
            records = dict(embedded_recs)
            records[rec["product_id"]] = rec
            html = _replace_embedded(html, "CATALOG", cat, nl)
            html = _replace_embedded(html, "RECORDS", records, nl)
            ui.write_text(html, encoding="utf-8")
            index = cat

    return {
        "ok": True,
        "record": rec,
        "output": str(out),
        "catalog": index,
        "records": records,
    }
