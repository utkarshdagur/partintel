"""Scalable catalog engine entry point.

Usage:
    python run_catalog.py                       # demo catalog (3 products, 4 workers)
    python run_catalog.py --workers 8           # control parallelism
    python run_catalog.py --mode mock           # force the offline deterministic extractor
    python run_catalog.py --demo 12             # generate + run a synthetic 12-product catalog
    python run_catalog.py --demo 50 --compare   # benchmark serial (1 worker) vs parallel

Writes, per product, its structured JSON record; plus a catalog index
(output/catalog_index.json), a CSV snapshot (output/catalog_index.csv) and a
re-embedded catalog dashboard (CATALOG + RECORDS) inside ui/index.html so the
double-click UI shows the whole catalog with click-to-inspect rows.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pipeline.catalog import (
    generate_synthetic_bearing_catalog,
    load_catalog,
    print_catalog_summary,
    run_catalog,
    write_catalog_outputs,
)

BASE_DIR = Path(__file__).resolve().parent


def _js_json(obj) -> str:
    """Compact JSON safe to inline into a <script> block."""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).replace(
        "<", "\\u003c"
    )  # keep `</script>` and friends out of the page


def _embed_catalog_into_ui(index: dict) -> None:
    """Re-embed the catalog index + product records into ui/index.html.

    Replaces the `const CATALOG = ...` / `const RECORDS = {...}` blocks (or
    inserts them before `const $`) so the UI never drifts from fresh runs.
    """
    ui = BASE_DIR / "ui" / "index.html"
    html = ui.read_text(encoding="utf-8")
    marker = "const $"
    if "const CATALOG = " in html:
        start = html.index("const CATALOG = ")
        html = html[:start] + html[html.index(marker, start):]

    records: dict = {}
    for p in index["products"]:
        out = Path(p.get("output") or "")
        if p["status"] == "ok" and out.exists():
            records[p["product_id"]] = json.loads(out.read_text(encoding="utf-8"))
    nl = "\r\n" if "\r\n" in html else "\n"
    block = (
        "const CATALOG = " + _js_json(index) + ";" + nl
        + "const RECORDS = " + _js_json(records) + ";" + nl + nl
    )
    html = html.replace(marker, block + marker, 1)
    ui.write_text(html, encoding="utf-8")


def _benchmark(catalog: dict, mode: str, workers: int) -> dict:
    """Run the same catalog serially and in parallel; print the comparison.

    Returns the parallel-run index (reused as the final catalog index).
    """
    print(f"\nBenchmarking on {len(catalog['products'])} products "
          f"(mode={mode}): serial (1 worker) vs parallel ({workers} workers)...")
    t0 = time.perf_counter()
    idx1 = run_catalog(catalog, mode=mode, max_workers=1, progress=False)
    serial_t = time.perf_counter() - t0
    t0 = time.perf_counter()
    idxN = run_catalog(catalog, mode=mode, max_workers=workers, progress=False)
    par_t = time.perf_counter() - t0

    same = idx1["totals"] == idxN["totals"]
    print(f"  workers=1    : {serial_t:.2f}s")
    print(f"  workers={workers:<3}  : {par_t:.2f}s")
    print(f"  speedup      : {serial_t / par_t:.2f}x" if par_t > 0 else "  speedup      : n/a")
    print(f"  totals identical: {'YES' if same else 'NO'}")
    print("  note: offline mock extraction is CPU-bound (GIL), so parallel gains")
    print("        appear with real LLM I/O - which releases the GIL per request.")
    if not same:
        print(f"    serial   : {idx1['totals']}")
        print(f"    parallel : {idxN['totals']}")
    return idxN


def main() -> int:
    ap = argparse.ArgumentParser(description="PartIntel scalable catalog engine")
    ap.add_argument("--catalog", default="data/catalog.json",
                    help="catalog manifest path (default: data/catalog.json)")
    ap.add_argument("--mode", default="auto", choices=["auto", "mock", "anthropic", "openai"],
                    help="extractor mode (auto = LLM if key set, else offline mock)")
    ap.add_argument("--workers", type=int, default=4,
                    help="parallel workers (one product per worker)")
    ap.add_argument("--demo", type=int, metavar="N",
                    help="generate + run a synthetic N-product bearing catalog instead")
    ap.add_argument("--compare", action="store_true",
                    help="benchmark serial vs parallel on the same catalog")
    args = ap.parse_args()

    if args.demo:
        catalog = generate_synthetic_bearing_catalog(args.demo)
        print(f"Generated synthetic catalog: {len(catalog['products'])} products")
    else:
        catalog = load_catalog(BASE_DIR / args.catalog)
        print(f"Catalog '{catalog['name']}': {len(catalog['products'])} products, "
              f"{args.workers} worker(s)")

    if args.compare:
        index = _benchmark(catalog, args.mode, args.workers)
    else:
        t0 = time.perf_counter()
        index = run_catalog(catalog, mode=args.mode, max_workers=args.workers)
        elapsed = time.perf_counter() - t0
        print_catalog_summary(index)
        print(f"\nElapsed: {elapsed:.2f}s across {index['totals']['products']} products "
              f"({args.workers} worker(s))")

    paths = write_catalog_outputs(index)
    print(f"Catalog index: {paths['index']}")
    print(f"CSV snapshot:  {paths['csv']}")
    _embed_catalog_into_ui(index)
    print("UI catalog dashboard embedded: ui/index.html")
    return 0 if index["totals"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
