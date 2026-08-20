"""Scalable catalog engine: process N product manifests in parallel.

A *catalog* is a JSON file that lists any number of products, each with its
own manifest + schema + output path (a catalog may freely mix categories):

    {
      "catalog_id": "demo",
      "name": "PartIntel demo catalog",
      "products": [
        { "product_id": "6205-2RS", "manifest": "data/sample/sources.json",
          "schema": "schema/bearing_schema.json", "output": "output/6205_product.json" },
        { "product_id": "V2000-BS", "manifest": "data/sample_valve/sources.json",
          "schema": "schema/valve_schema.json",    "output": "output/valve_product.json" }
      ]
    }

`run_catalog` processes every product with a ThreadPoolExecutor (one product
per worker — the LLM-heavy work happens inside `run_pipeline`), isolates
failures (one bad product never aborts the rest), and returns a catalog
*index*: per-product status + aggregated totals across the whole catalog.
`write_catalog_outputs` persists the index as JSON plus a one-row-per-product
CSV snapshot. In mock mode extraction is deterministic, so parallel output is
byte-identical to serial output — the tests assert exactly that.
"""
from __future__ import annotations

import csv
import datetime
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

from .llm import pick_provider
from .run import run_pipeline

BASE_DIR = Path(__file__).resolve().parent.parent

# Real-ish SKF 62xx deep groove ball bearing series:
# (part, d, D, B, dynamic kN, static kN, limiting speed r/min, mass kg).
# The synthetic catalog generator uses this table so the "large catalog" demo
# shows varied, realistic products instead of copies.
BEARING_SERIES = [
    ("6200-2RS", 10, 30, 9, 5.07, 2.36, 36000, 0.031),
    ("6201-2RS", 12, 32, 10, 7.02, 3.10, 32000, 0.037),
    ("6202-2RS", 15, 35, 11, 7.80, 3.75, 28000, 0.045),
    ("6203-2RS", 17, 40, 12, 9.95, 4.75, 24000, 0.065),
    ("6204-2RS", 20, 47, 14, 12.7, 6.55, 19000, 0.106),
    ("6205-2RS", 25, 52, 15, 14.0, 7.80, 15000, 0.127),
    ("6206-2RS", 30, 62, 16, 20.3, 11.2, 13000, 0.199),
    ("6207-2RS", 35, 72, 17, 27.0, 15.3, 11000, 0.288),
    ("6208-2RS", 40, 80, 18, 32.5, 19.0, 9500, 0.366),
    ("6209-2RS", 45, 85, 19, 35.1, 21.6, 8500, 0.405),
    ("6210-2RS", 50, 90, 20, 37.1, 23.2, 7500, 0.466),
    ("6211-2RS", 55, 100, 21, 46.5, 29.0, 6700, 0.606),
]


# ---------------------------------------------------------------------------
# Catalog loading / path resolution
# ---------------------------------------------------------------------------


def resolve_path(p, base: Path) -> Path:
    """Resolve a catalog-relative path: project root first, then catalog dir.

    Manifests in this project use project-root-relative paths (e.g.
    "data/sample/sources.json"), so BASE_DIR is checked first; a catalog placed
    outside the repo can still reference sibling files via its own directory.
    Path resolution never depends on whether a target file already exists.
    """
    path = Path(p)
    if path.is_absolute():
        return path
    for cand in (BASE_DIR / path, base / path):
        if cand.exists():
            return cand
    return BASE_DIR / path


def load_catalog(catalog_path) -> dict:
    """Load a catalog manifest and resolve every product's paths."""
    p = Path(catalog_path)
    raw = json.loads(p.read_text(encoding="utf-8"))
    base = p.resolve().parent
    products = []
    for entry in raw.get("products", []):
        pid = entry.get("product_id") or Path(entry["manifest"]).stem
        products.append({
            "product_id": pid,
            "manifest": resolve_path(entry["manifest"], base),
            "schema": resolve_path(entry.get("schema", "schema/bearing_schema.json"), base),
            "output": resolve_path(entry.get("output", f"output/{pid}_product.json"), base),
        })
    return {
        "catalog_id": raw.get("catalog_id", p.stem),
        "name": raw.get("name", raw.get("catalog_id", p.stem)),
        "products": products,
    }


# ---------------------------------------------------------------------------
# Parallel execution
# ---------------------------------------------------------------------------


def _run_one(product: dict, mode: str) -> dict:
    """Run one product's pipeline; never raises (failures are isolated)."""
    try:
        record = run_pipeline(
            manifest_path=product["manifest"],
            schema_path=product["schema"],
            output_path=str(product["output"]),
            mode=mode,
        )
        return {
            "product_id": product["product_id"],
            "status": "ok",
            "record": record,
            "output": str(product["output"]),
            "error": None,
        }
    except Exception as e:  # noqa: BLE001 — one bad product must not kill the catalog
        return {
            "product_id": product["product_id"],
            "status": "failed",
            "record": None,
            "output": str(product["output"]),
            "error": f"{type(e).__name__}: {e}",
        }


def run_catalog(catalog: dict, mode: str = "auto", max_workers: int = 4,
                progress: bool = True) -> dict:
    """Process every product in the catalog (in parallel) and build the index.

    Returns the catalog index dict (totals + per-product rows). Each product's
    structured record is also written to its own output file by `run_pipeline`.
    """
    products = catalog["products"]
    n = len(products)
    # Reorder by original catalog index (not by product_id), so duplicate ids
    # in a catalog can never collapse or misalign the results.
    results: list[dict] = [None] * n
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_run_one, p, mode): i for i, p in enumerate(products)}
        width = len(str(n))
        for done, fut in enumerate(as_completed(futures), start=1):
            i = futures[fut]
            res = fut.result()
            results[i] = res
            if progress and n > 1:
                s = res["record"].summary if res["record"] else {}
                print(
                    f"  [{done:>{width}}/{n}] {res['product_id']:<12} "
                    f"{res['status'].upper():<7} verified={s.get('verified', 0)} "
                    f"needs_review={s.get('needs_review', 0)} conflicts={s.get('conflicts', 0)}"
                )
    return _build_index(catalog, results, mode, max_workers)


# ---------------------------------------------------------------------------
# Catalog index + CSV output
# ---------------------------------------------------------------------------


def _build_index(catalog: dict, results: list[dict], mode: str, max_workers: int) -> dict:
    totals = {
        "products": len(results),
        "ok": 0,
        "failed": 0,
        "verified": 0,
        "needs_review": 0,
        "conflicts": 0,
        "rules_run": 0,
    }
    products = []
    for res in results:
        rec = res["record"]
        if rec is None:
            totals["failed"] += 1
            products.append({
                "product_id": res["product_id"],
                "category": None,
                "category_label": None,
                "status": "failed",
                "error": res["error"],
                "output": res["output"],
            })
            continue
        s = rec.summary
        totals["ok"] += 1
        totals["verified"] += s["verified"]
        totals["needs_review"] += s["needs_review"]
        totals["conflicts"] += s["conflicts"]
        totals["rules_run"] += s["rules_run"]
        products.append({
            "product_id": rec.product_id,
            "category": rec.category,
            "category_label": rec.category_label,
            "status": "ok",
            "verified": s["verified"],
            "needs_review": s["needs_review"],
            "conflicts": s["conflicts"],
            "rules_run": s["rules_run"],
            "extracted": s["extracted"],
            "omitted_not_found": s["omitted_not_found"],
            "output": res["output"],
            "error": None,
        })
    return {
        "catalog_id": catalog.get("catalog_id", "catalog"),
        "name": catalog.get("name", catalog.get("catalog_id", "catalog")),
        "pipeline": {
            "version": "1.0.0",
            "mode": pick_provider(mode),
            "max_workers": max_workers,
            "run_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        },
        "totals": totals,
        "products": products,
    }


def write_catalog_outputs(index: dict, output_dir=None) -> dict:
    """Persist the catalog index as JSON + a one-row-per-product CSV snapshot.

    Returns {"index": path, "csv": path}.
    """
    out = Path(output_dir) if output_dir is not None else BASE_DIR / "output"
    out.mkdir(parents=True, exist_ok=True)
    index_path = out / "catalog_index.json"
    csv_path = out / "catalog_index.csv"

    index_path.write_text(
        json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["product_id", "category", "status", "verified",
                    "needs_review", "conflicts", "rules_run", "extracted", "output"])
        for p in index["products"]:
            w.writerow([
                p.get("product_id", ""),
                p.get("category") or "",
                p.get("status", ""),
                p.get("verified", ""),
                p.get("needs_review", ""),
                p.get("conflicts", ""),
                p.get("rules_run", ""),
                p.get("extracted", ""),
                p.get("output", ""),
            ])
    return {"index": str(index_path), "csv": str(csv_path)}


# ---------------------------------------------------------------------------
# Synthetic catalog generator (for the "large catalog" demo, no API keys)
# ---------------------------------------------------------------------------


def generate_synthetic_bearing_catalog(n: int, out_dir: Optional[Path] = None) -> dict:
    """Generate a deterministic synthetic catalog of n bearing products.

    Writes realistic datasheet / web / OCR sources per product under `out_dir`
    (default data/generated/), so the demo exercises the real ingestion ->
    extraction -> merge -> validate -> route -> marketing stack at scale,
    fully offline. Returns an in-memory catalog dict (paths are absolute).
    """
    out_dir = Path(out_dir) if out_dir is not None else BASE_DIR / "data" / "generated"
    products = []
    for i in range(n):
        series_pn, d, D, B, c, c0, speed, mass = BEARING_SERIES[i % len(BEARING_SERIES)]
        # Keep product ids unique even beyond the 12-entry series: second cycle
        # becomes 6200-2RS-2, 6200-2RS-3, ... (distinct output files, no races).
        cycle = i // len(BEARING_SERIES)
        pn = series_pn if cycle == 0 else f"{series_pn}-{cycle + 1}"
        pdir = out_dir / pn
        pdir.mkdir(parents=True, exist_ok=True)

        (pdir / "datasheet.txt").write_text(
            f"SKF {pn} Deep groove ball bearing\n"
            f"d [mm] {d}\nD [mm] {D}\nB [mm] {B}\n"
            f"Basic dynamic load rating C {c} kN\n"
            f"Basic static load rating C0 {c0} kN\n"
            f"Limiting speed {speed} r/min\n"
            f"Mass {mass} kg\n"
            f"Seal  Contact seal, RS1 on both sides\n"
            f"Internal clearance  CN\n",
            encoding="utf-8",
        )
        (pdir / "webpage.html").write_text(
            f"<h1>{pn} Ball Bearing</h1>\n"
            f"<p>Bore: {d} mm, OD: {D} mm, Width: {B} mm.</p>\n"
            f"<table>\n"
            f"<tr><td>Dynamic load rating</td><td>{c * 1000:.0f} N</td></tr>\n"
            f"<tr><td>Static load rating</td><td>{c0 * 1000:.0f} N</td></tr>\n"
            f"<tr><td>Max speed (grease)</td><td>{speed:,} rpm</td></tr>\n"
            f"<tr><td>Operating temperature</td><td>-20 °C to +120 °C</td></tr>\n"
            f"<tr><td>Seal</td><td>2RS (rubber contact)</td></tr>\n"
            f"<tr><td>Internal clearance</td><td>CN</td></tr>\n"
            f"<tr><td>Made in</td><td>Japan</td></tr>\n"
            f"</table>\n",
            encoding="utf-8",
        )
        (pdir / "ocr.txt").write_text(
            f"{d}x{D}x{B}\n2RS\nJAPAN\nABEC 1\n", encoding="utf-8"
        )

        manifest = {
            "product_id": pn,
            "category": "deep_groove_ball_bearing",
            "sources": [
                {"id": "src_pdf", "type": "pdf",
                 "title": f"Datasheet {pn} (rev 4, p.1)",
                 "path": str(pdir / "datasheet.txt"), "authority": 0.95},
                {"id": "src_web", "type": "web",
                 "title": f"Manufacturer product page - {pn}",
                 "path": str(pdir / "webpage.html"), "authority": 0.85},
                {"id": "src_ocr", "type": "ocr",
                 "title": "Product photo OCR (stamping on bearing face)",
                 "path": str(pdir / "ocr.txt"), "authority": 0.7},
            ],
        }
        (pdir / "sources.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        products.append({
            "product_id": pn,
            "manifest": pdir / "sources.json",
            "schema": BASE_DIR / "schema" / "bearing_schema.json",
            "output": out_dir / "output" / f"{pn}_product.json",
        })

    return {"catalog_id": f"synthetic_{n}", "name": f"Synthetic bearing catalog ({n})",
            "products": products}


# ---------------------------------------------------------------------------
# Console summary
# ---------------------------------------------------------------------------


def print_catalog_summary(index: dict) -> None:
    t = index["totals"]
    print("=" * 76)
    print(f"CATALOG {index['name']}  |  {t['products']} products "
          f"({t['ok']} ok / {t['failed']} failed)  |  mode={index['pipeline']['mode']} "
          f"workers={index['pipeline']['max_workers']}")
    print(f"TOTALS  {t['verified']} verified fields / {t['needs_review']} needs review "
          f"/ {t['conflicts']} conflict(s) / {t['rules_run']} validation rules run")
    print("-" * 76)
    print(f"{'PRODUCT':<14}{'CATEGORY':<28}{'VER':>4}{'REV':>5}{'CFL':>5}  STATUS")
    for p in index["products"]:
        if p["status"] == "failed":
            print(f"{p['product_id']:<14}{'-':<28}{'-':>4}{'-':>5}{'-':>5}  FAILED  {p['error']}")
            continue
        cat = (p["category_label"] or p["category"])[:27]
        print(f"{p['product_id']:<14}{cat:<28}{p['verified']:>4}{p['needs_review']:>5}"
              f"{p['conflicts']:>5}  ok")
    print("=" * 76)
