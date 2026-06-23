"""POST /api/ai/pitch-commentary — generate analyst commentary for the
pitch deck PPTX. Bigger max_tokens than chat / peer-suggestion since
it's expected to return 4 paragraphs (exec summary, market context,
credit rationale, risk commentary).
"""
from http.server import BaseHTTPRequestHandler
import json
import cache_store
import claude_client

MAX_TOKENS = 2048


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if cache_store.auth_from_headers(self.headers) is None:
            return self._json(401, {"error": "unauthorized"})

        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (ValueError, json.JSONDecodeError):
            return self._json(400, {"error": "bad_request"})

        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            return self._json(400, {"error": "missing_prompt"})
        if len(prompt) > 60000:
            return self._json(413, {"error": "prompt_too_long",
                                    "detail": f"{len(prompt)} chars > 60000"})
        if not claude_client.is_configured():
            return self._json(503, {"error": "not_configured",
                                    "detail": "Tigress AI not configured."})

        res = claude_client.call_claude_json(prompt, max_tokens=MAX_TOKENS)
        if not res.get("ok"):
            return self._json(200, {
                "error":  res.get("error", "unknown"),
                "detail": res.get("detail", ""),
            })
        return self._json(200, res["json"])

    def _json(self, code, data):
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)
