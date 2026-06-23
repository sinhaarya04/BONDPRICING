"""GET /api/load/<TICKER> — cache-only read of the full pricing payload.

Mirrors api/peers.py but for the load_response key. Any ?peers= query
from the frontend is intentionally ignored (the ticker comes in via
?t= from the vercel.json rewrite).
"""
from http.server import BaseHTTPRequestHandler
import json
import urllib.parse
import cache_store


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        sess = cache_store.auth_from_headers(self.headers)
        if sess is None:
            return self._json(401, {"error": "unauthorized"})

        ticker = self._extract_ticker()
        if not ticker or len(ticker) > 6:
            return self._json(400, {"error": "Invalid ticker."})

        doc = cache_store.read_cache(ticker)
        if doc and doc.get("load_response"):
            return self._json(200, doc["load_response"])
        return self._json(404, {
            "error":   "not_cached",
            "message": "Not yet available \u2014 ask your Tigress contact "
                       "to pull this name.",
        })

    def _extract_ticker(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        t = (qs.get("t") or [""])[0]
        if t:
            return t.strip().upper()
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "load":
            return parts[2].strip().upper()
        return ""

    def _json(self, code, data):
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
