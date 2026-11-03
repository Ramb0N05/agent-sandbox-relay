# relay/auth.py - Bearer-Tokens nach Rolle, Admin-Login mit Lockout (elfte Runde; vierzehnte: 401 ohne Basic-Challenge)
import asyncio
import hashlib
import secrets
import time

from fastapi import HTTPException

from . import config, store

# Lockout je Peer-IP (hinter Traefik: letzter X-Forwarded-For-Eintrag):
# ab 5 Fehlversuchen 300 s Sperre, je weiterem Fehlversuch verdoppelt bis 3600 s
LOCK_FAILS = 5
LOCK_BASE = 300
LOCK_MAX = 3600
_locks: dict = {}  # peer -> [fehlversuche, gesperrt_bis_unix]


async def admin_fail_delay():
    # Brute-Force bremsen; für Stub-Tests austauschbar
    await asyncio.sleep(0.5)


def _peer(request):
    peer = request.client.host if request.client else None
    xff = request.headers.get("x-forwarded-for")
    if xff:
        # hinter Traefik ist die echte Client-IP der letzte Eintrag
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
    # kind: "worker" (VM-Agent) oder "submitter" (Absender); Token wird nur als Hash geprüft
    if not authorization or not authorization.isascii() or not authorization.startswith("Bearer "):
        _auth_fail(request)
    h = _token_hash(authorization)
    rec = (store.worker_by_hash if kind == "worker" else store.submit_by_hash).get(h)
    if rec is None:
        _auth_fail(request)
    return rec


def auth_any(request, authorization):
    # beide Rollen (Datei-Endpunkte, /usage); Rückgabe: (Record, Rollenname)
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
        # 401 bewusst ohne WWW-Authenticate-Challenge: Der legitime Client (Admin-UI)
        # meldet sich proaktiv per Authorization-Header an; eine Basic-Challenge löst
        # in Browsern den nativen Login-Dialog aus, auch bei fetch, bevor das
        # JavaScript der UI die 401 sieht (Airbyte Issue 78177; SO 86105/9859627;
        # RFC 7235 formuliert die Challenge nur als SHOULD).
        raise HTTPException(status_code=401)
    _locks.pop(peer, None)  # erfolgreicher Login setzt Zähler und Sperre zurück
    return mode
