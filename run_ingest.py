"""Zero-dependency local server that powers the live "➕ Ingest New Product"
modal in the demo UI — it runs the *real* pipeline (extraction -> merge ->
unit normalization -> conflict flagging -> validation -> AI review ->
marketing) on raw text pasted into the browser.

Usage:
    python run_ingest.py                    # serve on http://127.0.0.1:8765
    python run_ingest.py --port 9000        # different port
    python run_ingest.py --mode mock        # force the offline deterministic extractor

Serves ui/index.html at the root and exposes:
    GET  /api/schemas   -> product categories + target fields for the modal
    POST /api/ingest    -> body: {"product_id", "category", "sources": [
                             {"type": "pdf|web|ocr", "title", "text"} ]}

The ingestion core lives in pipeline/ingest.py (HTTP-free, unit-tested); this
file is just the stdlib http.server wrapper.
"""
from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pipeline.ingest import CATEGORY_SCHEMAS, ingest_product, load_schemas_info

BASE_DIR = Path(__file__).resolve().parent
UI_DIR = BASE_DIR / "ui"

DEFAULT_PORT = 8765


class IngestHandler(BaseHTTPRequestHandler):
    mode = "mock"  # overridden by main()

    # --- helpers ----------------------------------------------------------

    def log_message(self, fmt, *args):  # quieter, timestamped log lines
        print(f"[ingest] {fmt % args}")

    def _send(self, code: int, body, ctype: str = "application/json") -> None:
        data = body if isinstance(body, bytes) else str(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False))

    # --- routing ----------------------------------------------------------

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/schemas":
            self._json(200, {"ok": True, "categories": load_schemas_info(),
                             "authority": {"pdf": 0.95, "web": 0.85, "ocr": 0.70}})
            return
        if path in ("/", "/index.html", "/ui/index.html"):
            html = (UI_DIR / "index.html").read_bytes()
            self._send(200, html, "text/html; charset=utf-8")
            return
        if path.startswith("/ui/"):
            rel = (UI_DIR / path[len("/ui/"):]).resolve()
            if rel.is_file() and str(rel).startswith(str(UI_DIR.resolve())):
                ctype = "text/html; charset=utf-8" if rel.suffix == ".html" \
                    else "text/css; charset=utf-8" if rel.suffix == ".css" \
                    else "application/javascript; charset=utf-8" if rel.suffix == ".js" \
                    else "application/octet-stream"
                self._send(200, rel.read_bytes(), ctype)
                return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/api/ingest":
            self._json(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("payload must be a JSON object")
        except Exception as e:  # noqa: BLE001 — surface malformed requests cleanly
            self._json(400, {"ok": False, "error": f"bad request: {e}"})
            return
        try:
            result = ingest_product(
                product_id=payload.get("product_id") or "",
                category=payload.get("category") or "",
                sources=payload.get("sources") or [],
                mode=self.mode,
            )
            self._json(200, result)
        except Exception as e:  # noqa: BLE001 — pipeline failures are per-request
            self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"port to serve on (default: {DEFAULT_PORT})")
    ap.add_argument("--host", default="127.0.0.1",
                    help="host to bind to (use 0.0.0.0 for Docker/PaaS)")
    ap.add_argument("--mode", default="auto",
                    choices=["auto", "mock", "anthropic", "openai"],
                    help="extractor mode (auto = LLM if key set, else offline mock)")
    ap.add_argument("--categories", action="store_true",
                    help="print the supported categories and exit")
    args = ap.parse_args()

    if args.categories:
        print(f"Supported categories ({len(CATEGORY_SCHEMAS)}):")
        for cat, rel in sorted(CATEGORY_SCHEMAS.items()):
            print(f"  {cat:<28} {rel}")
        return 0

    IngestHandler.mode = args.mode
    try:
        server = ThreadingHTTPServer((args.host, args.port), IngestHandler)
    except OSError as e:
        print(f"error: cannot bind {args.host}:{args.port} — {e}")
        print("hint: the port may already be in use; try: python run_ingest.py --port 9000")
        return 1
    url = f"http://{args.host}:{args.port}/"
    print("=" * 72)
    print("PartIntel live ingestion server")
    print(f"  mode     : {args.mode}")
    print(f"  UI       : {url}")
    print(f"  schemas  : {url}api/schemas")
    print(f"  ingest   : POST {url}api/ingest")
    print("  press Ctrl+C to stop")
    print("=" * 72)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping ingestion server.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
