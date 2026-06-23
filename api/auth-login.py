"""POST /api/auth/login — issue a bearer token for valid credentials.

Vercel Python serverless function. Same auth contract as server.py's
local /api/auth/login: takes {email, password}, returns {token, email,
role, cache_configured} or 401 invalid_credentials.
"""
from http.server import BaseHTTPRequestHandler
import json
import cache_store


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"error": "bad_request"})

        email = (body.get("email") or "").strip().lower()
        password = body.get("password") or ""
        result = cache_store.verify_login(email, password)
        if result is None:
            return self._json(401, {"error": "invalid_credentials"})

        email, role = result
        token = cache_store.create_session(email, role)
        return self._json(200, {
            "token":            token,
            "email":            email,
            "role":             role,
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
