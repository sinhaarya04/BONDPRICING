"""GET /api/peers/<TICKER> — cache-only read.

Vercel has no Bloomberg Terminal, so this can never trigger a live
fetch. If the ticker isn't in Mongo cache, return 404 not_cached with
a friendly message the frontend surfaces on the landing screen.

The dynamic-route rewriting in vercel.json turns
  /api/peers/HUM  ->  /api/peers?t=HUM
so the handler reads the ticker from the `?t=` query param. We also
fall back to parsing it from the path's last segment so the handler
still works if Vercel ever delivers the original path unchanged.
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

        # Return the CURATED peer set (what the employee actually ran with,
        # including AI-added names) — not Bloomberg's raw auto-suggestion.
        # Falls back to peers_response when load_response not yet cached.
        doc = cache_store.read_cache(ticker)
        view = cache_store.client_peers_view(doc)
        if view:
            return self._json(200, view)
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
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "peers":
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
