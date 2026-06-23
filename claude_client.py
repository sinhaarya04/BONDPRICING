"""Anthropic Claude API client for BONDPRICING.

Loads ANTHROPIC_API_KEY (and optional ANTHROPIC_MODEL) from a local
.env file or process environment. Exposes a single call_claude(prompt)
function used by the three AI integration points in server.py:
  - peer-suggestion review
  - pitch-deck commentary generation
  - sidebar scenario chatbot

Key handling:
  * Never logged. Never returned in error messages. Only sent in the
    x-api-key request header.
  * .env loader is intentionally minimal — no python-dotenv dep, just
    a 10-line parser that handles KEY=VALUE lines.
  * is_configured() lets callers return {"error": "not_configured"}
    cleanly when the key is missing, instead of crashing.
"""
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 1024
DEFAULT_TIMEOUT_SEC = 30

_dotenv_loaded = False


def _load_dotenv():
    """Read .env from the script directory and merge into os.environ
    (without overriding existing env vars). Idempotent.
    """
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    _dotenv_loaded = True
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
    except Exception as e:
        sys.stderr.write(f"  Warning: .env load failed: {e}\n")


def is_configured():
    """True when ANTHROPIC_API_KEY is set (so server can return a clean
    not_configured response instead of crashing on missing-key)."""
    _load_dotenv()
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def get_model():
    _load_dotenv()
    return os.environ.get("ANTHROPIC_MODEL") or DEFAULT_MODEL


def call_claude(prompt, model=None, max_tokens=None, system=None):
    """Send a single-turn user message to Claude. Returns
        {"ok": True,  "text": <reply>, "model": <model>}
    or
        {"ok": False, "error": <code>, "detail": <message>}

    The API key is read from ANTHROPIC_API_KEY at call time. The key
    itself is never included in the returned dict, even on error.
    """
    _load_dotenv()
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return {"ok": False, "error": "not_configured",
                "detail": "ANTHROPIC_API_KEY not set in environment or .env."}

    body = {
        "model": model or get_model(),
        "max_tokens": max_tokens or DEFAULT_MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        body["system"] = system

    req = urllib.request.Request(
        ANTHROPIC_API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "x-api-key":          api_key,
            "anthropic-version":  ANTHROPIC_VERSION,
            "content-type":       "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT_SEC) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8")
        except Exception:
            err_body = ""
        # Strip any echoed key just in case (Anthropic doesn't, but be safe)
        if api_key and api_key in err_body:
            err_body = err_body.replace(api_key, "***REDACTED***")
        return {"ok": False, "error": f"http_{e.code}",
                "detail": err_body[:500] or e.reason}
    except urllib.error.URLError as e:
        return {"ok": False, "error": "network", "detail": str(e.reason)}
    except Exception as e:
        return {"ok": False, "error": "exception", "detail": str(e)[:300]}

    # Successful response: content is a list of {type:"text", text:"..."} blocks
    text_parts = []
    for block in data.get("content", []):
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
    return {
        "ok":    True,
        "text":  "".join(text_parts),
        "model": data.get("model", body["model"]),
        "usage": data.get("usage", {}),
    }


def call_claude_json(prompt, model=None, max_tokens=None, system=None):
    """call_claude but tries to parse the reply as JSON. If the model
    wraps JSON in markdown code fences ```json ... ```, strips them.
    Returns {"ok": True, "json": <parsed>, "raw": <text>} on success.
    """
    res = call_claude(prompt, model=model, max_tokens=max_tokens, system=system)
    if not res.get("ok"):
        return res
    text = res["text"].strip()
    # Strip ```json ... ``` or ``` ... ``` fences if present
    if text.startswith("```"):
        # Remove first fence line
        lines = text.split("\n", 1)
        if len(lines) > 1:
            text = lines[1]
        # Remove trailing fence
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3].rstrip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        return {"ok": False, "error": "bad_json",
                "detail": f"{e}: {text[:200]}", "raw": res["text"]}
    return {"ok": True, "json": parsed, "raw": res["text"],
            "model": res["model"], "usage": res["usage"]}
