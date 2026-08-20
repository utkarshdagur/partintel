"""Vercel serverless function for POST /api/ingest."""
import json
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.ingest import ingest_product


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("payload must be a JSON object")
        except Exception as e:
            self._send_json(400, {"ok": False, "error": f"bad request: {e}"})
            return
        try:
            result = ingest_product(
                product_id=payload.get("product_id") or "",
                category=payload.get("category") or "",
                sources=payload.get("sources") or [],
                mode="mock",
            )
            self._send_json(200, result)
        except Exception as e:
            self._send_json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"})

    def _send_json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
