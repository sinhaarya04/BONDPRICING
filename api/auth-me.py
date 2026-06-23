"""GET /api/auth/me — validate a bearer token, return {email, role}.

Used by the frontend's bootstrapAuth() on page load. 401 wipes the
localStorage token and bounces back to the login screen.
"""
from http.server import BaseHTTPRequestHandler
import json
import cache_store


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        sess = cache_store.auth_from_headers(self.headers)
        if sess is None:
            return self._json(401, {"error": "unauthorized"})
        return self._json(200, {
            "email":            sess["email"],
            "role":             sess["role"],
            "cache_configured": cache_store.is_configured(),
        })

    def _json(self, code, data):
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
