"""POST /api/auth/logout — delete the bearer token from sessions.

Idempotent: returns {ok: true} even if the token was already gone.
"""
from http.server import BaseHTTPRequestHandler
import json
import cache_store


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        h = self.headers.get("Authorization") or self.headers.get("authorization") or ""
        if h.lower().startswith("bearer "):
            cache_store.delete_session(h[7:].strip())
        return self._json(200, {"ok": True})

    def _json(self, code, data):
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
