"""Mongo-backed cache + session store, shared by local server.py and the
Vercel client viewer.

Two collections in DB `bondpricing`:

  cache:    _id=ticker, cached_at, cached_by, peers_response, load_response,
            peers_used. Per-ticker payload snapshot — only refreshed when an
            employee re-searches.

  sessions: token, email, role, created_at. Bearer tokens issued by the
            login endpoint; both local server.py and Vercel functions
            consult the same collection so a token from one works on the
            other.

User accounts are loaded from the TIGRESS_USERS_JSON env var so creds
never live in source. Plaintext-compare prototype scope, flagged in
the plan.

Graceful degradation: if MONGO_URI is missing or the cluster is
unreachable, get_db() returns None and every helper becomes a no-op
read/write. Lets the server boot for local dev without Mongo, and the
frontend just sees cache-miss / not-configured paths.
"""
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

try:
    from pymongo import MongoClient
    from pymongo.errors import PyMongoError
except ImportError:
    MongoClient = None
    PyMongoError = Exception


# ── Users (loaded from env var, never hardcoded) ───────────────────
# TIGRESS_USERS_JSON env var holds a JSON map:
#   {"email": {"pw": "...", "role": "employee|client"}, ...}
# Lives in .env locally and Vercel project env vars in production.
# Repo can stay public — credentials only ever live in env, not source.

_USERS_CACHE = None


def _users():
    """Lazy load + memoize the user map from TIGRESS_USERS_JSON.
    Returns {} if the env var is missing or malformed (so every login
    fails closed rather than open).
    """
    global _USERS_CACHE
    if _USERS_CACHE is not None:
        return _USERS_CACHE
    _load_dotenv()
    raw = os.environ.get("TIGRESS_USERS_JSON") or ""
    out = {}
    if raw:
        try:
            data = json.loads(raw)
            for email, entry in data.items():
                pw = entry.get("pw")
                role = entry.get("role")
                if pw and role:
                    out[email.strip().lower()] = (pw, role)
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass
    _USERS_CACHE = out
    return _USERS_CACHE


def verify_login(email, password):
    """Return (email, role) on match else None. Case-insensitive on email."""
    if not email or not password:
        return None
    entry = _users().get(email.strip().lower())
    if entry is None:
        return None
    expected_pw, role = entry
    if password != expected_pw:
        return None
    return (email.strip().lower(), role)


# ── .env loader (shared with claude_client.py pattern) ─────────────

_dotenv_loaded = False


def _load_dotenv():
    """Read .env from this file's directory once; do not override existing
    env vars. Mirrors the loader in claude_client.py.
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
    except Exception:
        pass


# ── Mongo singleton ────────────────────────────────────────────────

_client = None
_db = None
_init_attempted = False


def get_db():
    """Lazy-connect to Mongo. Returns the bondpricing db handle or None.

    Returns None silently when MONGO_URI is absent, pymongo isn't
    installed, or the cluster is unreachable — every caller of the
    cache_store API is then a no-op.
    """
    global _client, _db, _init_attempted
    if _db is not None:
        return _db
    if _init_attempted:
        return None
    _init_attempted = True
    _load_dotenv()
    uri = os.environ.get("MONGO_URI")
    if not uri or MongoClient is None:
        return None
    try:
        _client = MongoClient(uri, serverSelectionTimeoutMS=4000)
        # Force connection check now so failures surface here, not later
        _client.admin.command("ping")
        _db = _client["bondpricing"]
        return _db
    except PyMongoError:
        _client = None
        _db = None
        return None


def is_configured():
    """True iff a Mongo connection is live. Used by /api/auth/me to
    decide whether to advertise cache availability.
    """
    return get_db() is not None


# ── Cache (per ticker) ─────────────────────────────────────────────

def _norm_ticker(t):
    return (t or "").strip().upper()


def read_cache(ticker):
    """Return the cache doc for a ticker or None. Doc shape:
      {_id, cached_at, cached_by, peers_response, load_response, peers_used}
    """
    db = get_db()
    if db is None:
        return None
    try:
        return db.cache.find_one({"_id": _norm_ticker(ticker)})
    except PyMongoError:
        return None


def write_cache(ticker, key, value, by, extra=None):
    """Upsert a single subfield (`peers_response` or `load_response`) into
    the ticker's cache doc. Updates cached_at + cached_by on every write.
    `extra` lets the load route also set peers_used in the same call.
    Returns True on success, False on any failure (caller never crashes).
    """
    db = get_db()
    if db is None:
        return False
    if key not in ("peers_response", "load_response"):
        return False
    update = {
        key:         value,
        "cached_at": datetime.now(timezone.utc),
        "cached_by": by,
    }
    if extra:
        update.update(extra)
    try:
        db.cache.update_one(
            {"_id": _norm_ticker(ticker)},
            {"$set": update},
            upsert=True,
        )
        return True
    except PyMongoError:
        return False


# ── Sessions (bearer tokens) ───────────────────────────────────────

def create_session(email, role):
    """Insert a new session and return the token string. Falls back to a
    token even if Mongo is down — the local server then accepts that
    token only within its own in-process memory (see _MEM_SESSIONS below).
    """
    token = secrets.token_urlsafe(32)
    db = get_db()
    if db is not None:
        try:
            db.sessions.insert_one({
                "token":      token,
                "email":      email,
                "role":       role,
                "created_at": datetime.now(timezone.utc),
            })
        except PyMongoError:
            pass
    # Mirror to in-memory map so single-process dev still works without Mongo
    _MEM_SESSIONS[token] = {"email": email, "role": role}
    return token


def get_session(token):
    """Return {email, role} or None. Checks Mongo first, then in-memory
    fallback (for dev without Mongo).
    """
    if not token:
        return None
    db = get_db()
    if db is not None:
        try:
            doc = db.sessions.find_one({"token": token})
            if doc:
                return {"email": doc["email"], "role": doc["role"]}
        except PyMongoError:
            pass
    mem = _MEM_SESSIONS.get(token)
    if mem:
        return mem
    return None


def delete_session(token):
    """Remove a session. Best-effort across Mongo + in-memory."""
    if not token:
        return
    db = get_db()
    if db is not None:
        try:
            db.sessions.delete_one({"token": token})
        except PyMongoError:
            pass
    _MEM_SESSIONS.pop(token, None)


def auth_from_headers(headers):
    """Pull `Authorization: Bearer <token>` out of a headers-like mapping
    and return the session {email, role} or None. Shared by server.py's
    _get_auth and every Vercel api/*.py handler.
    """
    h = headers.get("Authorization") or headers.get("authorization") or ""
    if not h.lower().startswith("bearer "):
        return None
    return get_session(h[7:].strip())


def client_peers_view(doc):
    """Build the peer-select payload a client should see for this ticker.

    Clients should NEVER see the raw Bloomberg auto-suggestion — they
    should see the curated set the employee actually priced against
    (which includes AI-added names like MSFT/GOOGL on top of, or replacing,
    the Bloomberg defaults).

    Strategy:
      1. If load_response is cached AND has peerTickers + peerIssuers,
         synthesize a peers list from those. This is the curated set.
      2. Otherwise, fall back to the raw peers_response (Bloomberg's
         auto-suggestion) — better than nothing for cold cache.
      3. Return None if neither is cached.

    Returned shape mirrors the existing peers_response contract:
      {issuer, source, peers:[{ticker,name,rating,sector}], dropped:[]}
    """
    if doc is None:
        return None
    peers_resp = doc.get("peers_response")
    load_resp  = doc.get("load_response")

    if load_resp:
        peer_tickers = load_resp.get("peerTickers") or []
        peer_issuers = load_resp.get("peerIssuers") or {}
        if peer_tickers and peer_issuers:
            curated = []
            for tk in peer_tickers:
                info = peer_issuers.get(tk) or {}
                curated.append({
                    "ticker": tk,
                    "name":   info.get("name") or tk,
                    "rating": info.get("rating") or "NR",
                    "sector": info.get("sector") or "",
                })
            issuer = (load_resp.get("issuer")
                      or (peers_resp.get("issuer") if peers_resp else {}))
            return {
                "issuer":  issuer,
                "source":  "curated",
                "peers":   curated,
                "dropped": [],
            }

    return peers_resp


# In-process fallback so the local server still works for the boss who
# hasn't filled in MONGO_URI yet. Restart of server.py clears these.
_MEM_SESSIONS = {}
