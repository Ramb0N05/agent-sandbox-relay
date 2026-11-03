# relay/auth.py - bearer tokens by role, admin login with lockout (11th round; 14th: 401 without basic challenge)
import asyncio
import hashlib
import secrets
import time

from fastapi import HTTPException

from . import config, store

# lockout per peer IP (behind Traefik: last X-Forwarded-For entry):
# 5 failed attempts -> 300 s lock, each further failure doubles up to 3600 s
LOCK_FAILS = 5
LOCK_BASE = 300
LOCK_MAX = 3600
_locks: dict = {}  # peer -> [fehlversuche, gesperrt_bis_unix]


async def admin_fail_delay():
    # slow down brute force; swappable for stub tests
    await asyncio.sleep(0.5)


def _peer(request):
    peer = request.client.host if request.client else None
    xff = request.headers.get("x-forwarded-for")
    if xff:
        # behind Traefik the real client IP is the last entry
        peer = xff.split(",")[-1].strip()
    return peer, xff


def _auth_fail(request, detail=None):
    peer, xff = _peer(request)
    fields = {"path": request.url.path, "peer": peer, "xff": xff}
    if detail:
        fields["detail"] = detail
    store.audit("auth_fail", **fields)
    raise HTTPException(status_code=401)


def _token_hash(authorization):
    return hashlib.sha256(authorization[7:].encode()).hexdigest()


def auth_bearer(request, authorization, kind):
    # kind: "worker" (VM agent) or "submitter" (sender); token is only checked as hash
    if not authorization or not authorization.isascii() or not authorization.startswith("Bearer "):
        _auth_fail(request)
    h = _token_hash(authorization)
    rec = (store.worker_by_hash if kind == "worker" else store.submit_by_hash).get(h)
    if rec is None:
        _auth_fail(request)
    return rec


def auth_any(request, authorization):
    # both roles (file endpoints, /usage); returns (record, role name)
    if not authorization or not authorization.isascii() or not authorization.startswith("Bearer "):
        _auth_fail(request)
    h = _token_hash(authorization)
    rec = store.worker_by_hash.get(h)
    if rec is not None:
        return rec, "worker"
    rec = store.submit_by_hash.get(h)
    if rec is not None:
        return rec, "submitter"
    _auth_fail(request)


def admin_pw():
    if config.ADMIN_PASSWORD:
        return config.ADMIN_PASSWORD, "basic"
    if config.TOKEN:
        return config.TOKEN, "bootstrap"  # Migration: Legacy-Token als Admin-Passwort, bis AGENT_SANDBOX_ADMIN_PASSWORD gesetzt ist
    return None, None


def _lock_state(peer):
    rec = _locks.get(peer)
    if not rec:
        rec = _locks[peer] = [0, 0]
    return rec


def _register_fail(peer):
    fails, until = _lock_state(peer)
    fails += 1
    if fails >= LOCK_FAILS:
        until = time.time() + min(LOCK_BASE * (2 ** (fails - LOCK_FAILS)), LOCK_MAX)
        store.audit("admin_lockout_block", peer=peer, fehlversuche=fails, sekunden=until - time.time())
    else:
        store.audit("admin_lockout", peer=peer, fehlversuche=fails)
    _locks[peer] = [fails, until]
    return fails, until


async def require_admin(request, credentials):
    pw, mode = admin_pw()
    if pw is None:
        raise HTTPException(status_code=503, detail="Verwaltung deaktiviert: AGENT_SANDBOX_ADMIN_PASSWORD und Legacy-Token ungesetzt")
    peer, xff = _peer(request)
    fails, until = _lock_state(peer)
    if until > time.time():
        raise HTTPException(status_code=429, detail="zu viele Fehlversuche, gesperrt",
                            headers={"Retry-After": str(int(until - time.time()) + 1)})
    ok = credentials is not None and credentials.password.isascii() \
        and secrets.compare_digest(credentials.username.encode(), config.ADMIN_USERNAME.encode()) \
        and secrets.compare_digest(credentials.password.encode(), pw.encode())
    if not ok:
        _register_fail(peer)
        await admin_fail_delay()
        store.audit("admin_auth_fail", peer=peer, xff=xff, modus=mode)
        # 401 deliberately without WWW-Authenticate challenge: the legitimate client (admin UI)
        # authenticates proactively via the Authorization header; a basic challenge
        # triggers the native browser login dialog, even for fetch, before the
        # UI JavaScript sees the 401 (Airbyte issue 78177; SO 86105/9859627;
        # RFC 7235 states the challenge only as a SHOULD).
        raise HTTPException(status_code=401)
    _locks.pop(peer, None)  # erfolgreicher Login setzt Zähler und Sperre zurück
    return mode
