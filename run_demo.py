"""Demo entry point: run the pipeline on a sample product, print the summary,
and write the structured JSON.

Usage:
    python run_demo.py                  # bearing, auto mode (LLM if key set, else mock)
    python run_demo.py mock             # force the offline deterministic extractor
    python run_demo.py anthropic        # force Claude tool-calling (needs ANTHROPIC_API_KEY)
    python run_demo.py openai           # force OpenAI JSON mode (needs OPENAI_API_KEY)
    python run_demo.py mock valve       # second category: 2-way ball valve (proves category-agnostic)
"""
from __future__ import annotations

import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pipeline.run import print_summary, run_pipeline

# Catalog of demo products. The merge / validate / route / marketing / UI
# stack is category-agnostic — the valve entry proves it end-to-end.
CATALOGS = {
    "bearing": {
        "manifest": "data/sample/sources.json",
        "schema": "schema/bearing_schema.json",
        "output": "output/6205_product.json",
    },
    "valve": {
        "manifest": "data/sample_valve/sources.json",
        "schema": "schema/valve_schema.json",
        "output": "output/valve_product.json",
    },
    "motor": {
        "manifest": "data/sample_motor/sources.json",
        "schema": "schema/motor_schema.json",
        "output": "output/motor_product.json",
    },
    "gearbox": {
        "manifest": "data/sample_gearbox/sources.json",
        "schema": "schema/gearbox_schema.json",
        "output": "output/gearbox_product.json",
    },
}


def _embed_sample_into_ui(record_path: Path) -> None:
    """Keep the double-click UI in sync with fresh pipeline output: re-embed
    the generated JSON into ui/index.html (the `const SAMPLE = {...}` block).

    Stops at `const CATALOG = ` when present so a `run_catalog.py` embed of the
    catalog dashboard + records is preserved (never clobbered)."""
    import json

    ui = Path(__file__).resolve().parent / "ui" / "index.html"
    js = json.dumps(
        json.loads(record_path.read_text(encoding="utf-8")),
        separators=(",", ":"),
        ensure_ascii=False,
    ).replace("<", "\\u003c")  # keep `</script>` out of the page
    html = ui.read_text(encoding="utf-8")
    start = html.index("const SAMPLE = ")
    if "const CATALOG = " in html:
        end = html.index("const CATALOG = ", start)
    else:
        end = html.index("const $", start)
    nl = "\r\n" if "\r\n" in html else "\n"
    html = html[:start] + "const SAMPLE = " + js + ";" + nl + nl + html[end:]
    ui.write_text(html, encoding="utf-8")


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "auto"
    category = sys.argv[2] if len(sys.argv) > 2 else "bearing"
    if mode not in ("auto", "mock", "anthropic", "openai"):
        print(__doc__)
        return 1
    cat = CATALOGS.get(category)
    if cat is None:
        print(f"Unknown category {category!r}. Choose from: {', '.join(CATALOGS)}")
        return 1
    root = Path(__file__).resolve().parent
    out = root / cat["output"]
    record = run_pipeline(
        manifest_path=root / cat["manifest"],
        schema_path=root / cat["schema"],
        output_path=str(out),
        mode=mode,
    )
    print_summary(record)
    print(f"\nFull structured JSON written to: {out}")
    if category == "bearing":
        _embed_sample_into_ui(out)
        print(f"UI sample embedded from: ui/index.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
