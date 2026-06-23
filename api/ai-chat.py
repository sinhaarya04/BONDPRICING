"""POST /api/ai/chat — scenario chatbot, gated by bearer token.

Same prompt-master CO-STAR pattern used everywhere else. Prompt is
pre-built in index.html by generateScenarioChatPrompt(). Returns
Claude's JSON-parsed reply or {error, detail} on failure.
"""
from http.server import BaseHTTPRequestHandler
import json
import cache_store
import claude_client

MAX_TOKENS = 1024


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
            # Keep 200 so the UI can render an inline error without throwing
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
