"""Vercel serverless function for GET /api/schemas."""
import json
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.ingest import load_schemas_info


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        result = {
            "ok": True,
            "categories": load_schemas_info(),
            "authority": {"pdf": 0.95, "web": 0.85, "ocr": 0.70},
        }
        body = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
