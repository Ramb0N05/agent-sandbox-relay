# agent_sandbox_relay.py - Job-Queue + Datei-Transport + Token-Verwaltung (Transportoption E)
# Quelle: https://github.com/Ramb0N05/agent-sandbox-relay (Branch main)
# Ausfuehrung: uvicorn agent_sandbox_relay:app (siehe Dockerfile); Docker-Service heisst agent-sandbox-relay
# Speicher: Token-Store und Audit-Log als Dateien in AGENT_SANDBOX_RELAY_DATA (Volume empfohlen);
# ohne Volume ueberlebt der Token-Store einen Neubau des Containers nicht (Legacy-Migration greift dann erneut).
import asyncio, base64, gzip, hashlib, json, os, secrets, sys, time, uuid
from collections import OrderedDict
from datetime import datetime, timezone
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

TOKEN = os.environ.get("AGENT_SANDBOX_RELAY_TOKEN", "")  # Legacy-Token: Migration bei leerem Token-Speicher
ADMIN_PASSWORD = os.environ.get("AGENT_SANDBOX_ADMIN_PASSWORD", "")  # Login der Verwaltung; ungesetzt -> Bootstrap ueber Legacy-Token
DATA_DIR = os.environ.get("AGENT_SANDBOX_RELAY_DATA", "/tmp/agent-sandbox-relay")  # Token-Store + Audit-Log
OUTMAX = 8 * 1024 * 1024  # Result-Output-Deckel 8 MiB (Nutzerentscheidung 11/2026, Abschnitt 10.7)
FILEMAX = 8 * 1024 * 1024  # Roh-Groesse pro Datei im Datei-Store (Abschnitt 10.8)
FILESTOREMAX = 64  # Eintraege im Datei-Store (FIFO, aelteste zuerst raus)
BUDGET = int(os.environ.get("AGENT_SANDBOX_RELAY_BUDGET", str(256 * 1024 * 1024)))  # OOM-Schutz: Budget fuer Output+Datei-Encoding
AUDITMAX = 2 * 1024 * 1024  # Audit-Log-Rotation: ab dieser Groesse auf .1 umdrehen
# keine oeffentliche API-Doku: /docs, /redoc, /openapi.json antworten 404 (Nutzerentscheidung 03.11.2026)
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
jobs: "OrderedDict[str, dict]" = OrderedDict()
files: "OrderedDict[str, dict]" = OrderedDict()
workers: list = []
submitters: list = []
worker_by_hash: dict = {}
submit_by_hash: dict = {}
data_bytes = 0  # belegtes Budget: Output- plus Datei-Encoding-Bytes
evs: dict = {}  # worker_id -> asyncio.Event (Aufwecken bei neuem Job)
job_done: dict = {}  # job-id -> asyncio.Event (Aufwecken bei Result)
security = HTTPBasic(auto_error=False)

def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

def audit(event, **fields):
    # Audit-Log: JSON-Zeilen in DATA_DIR/audit.jsonl; niemals cmd-Inhalte oder Token-Werte loggen
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        p = os.path.join(DATA_DIR, "audit.jsonl")
        if os.path.exists(p) and os.path.getsize(p) > AUDITMAX:
            os.replace(p, p + ".1")  # Rotation: ein Backup behalten
        line = json.dumps({"ts": _now(), "event": event, **fields}, ensure_ascii=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        print("audit-fehler:", e, file=sys.stderr)  # Audit darf den Betrieb nie abbrechen

def save_state():
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = os.path.join(DATA_DIR, "tokens.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"workers": workers, "submitters": submitters}, f)
    os.replace(tmp, os.path.join(DATA_DIR, "tokens.json"))  # atomar: erst schreiben, dann umbenennen

def _reindex():
    global worker_by_hash, submit_by_hash
    worker_by_hash = {w["hash"]: w for w in workers}
    submit_by_hash = {s["hash"]: s for s in submitters}

def load_state():
    global workers, submitters
    p = os.path.join(DATA_DIR, "tokens.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            workers = d.get("workers", [])
            submitters = d.get("submitters", [])
        except (OSError, ValueError):
            raise SystemExit("Token-Speicher unlesbar: " + p)
    if not workers and not submitters and TOKEN:
        # Migration: das Legacy-Token aus der Env wird als Worker- und Submit-Token registriert;
        # die laufende VM und der Absender arbeiten weiter, beide Eintraege sind ueber die
        # Verwaltung loeschbar, sobald echte Tokens existieren
        h = hashlib.sha256(TOKEN.encode()).hexdigest()
        w = {"id": "w-" + uuid.uuid4().hex[:10], "name": "legacy", "hash": h, "legacy": True, "created": _now()}
        s = {"id": "s-" + uuid.uuid4().hex[:10], "name": "legacy", "hash": h, "worker_id": w["id"], "legacy": True, "created": _now()}
        workers = [w]
        submitters = [s]
        save_state()
        audit("migrate", detail="Legacy-Token als Worker- und Submit-Token registriert")
    if not workers and not submitters:
        raise SystemExit("weder Token-Speicher noch AGENT_SANDBOX_RELAY_TOKEN vorhanden")  # fail-closed
    _reindex()

load_state()
audit("boot", budget=BUDGET, outmax=OUTMAX, filemax=FILEMAX, workers=len(workers), submitters=len(submitters))

def _peer(request):
    peer = request.client.host if request.client else None
    xff = request.headers.get("x-forwarded-for")  # unveraessert: nur zur Information im Audit
    return peer, xff

def _auth_fail(request):
    peer, xff = _peer(request)
    audit("auth_fail", path=request.url.path, peer=peer, xff=xff)
    raise HTTPException(status_code=401)

def _token_hash(authorization):
    return hashlib.sha256(authorization[7:].encode()).hexdigest()

def auth_bearer(request, authorization, kind):
    # kind: "worker" (VM-Agent) oder "submitter" (Absender); Token wird nur als Hash geprueft
    if not authorization or not authorization.isascii() or not authorization.startswith("Bearer "):
        _auth_fail(request)
    h = _token_hash(authorization)
    rec = (worker_by_hash if kind == "worker" else submit_by_hash).get(h)
    if rec is None:
        _auth_fail(request)
    return rec

def auth_any(request, authorization):
    # beide Rollen (Datei-Endpunkte, /usage); Rueckgabe: (Record, Rollenname)
    if not authorization or not authorization.isascii() or not authorization.startswith("Bearer "):
        _auth_fail(request)
    h = _token_hash(authorization)
    rec = worker_by_hash.get(h)
    if rec is not None:
        return rec, "worker"
    rec = submit_by_hash.get(h)
    if rec is not None:
        return rec, "submitter"
    _auth_fail(request)

def _evict_for(need):
    # OOM-Schutz: Budget freiraeumen - erst erledigte Jobs (aeltestes zuerst), dann aelteste Dateien
    global data_bytes
    evicted = []
    while data_bytes + need > BUDGET:
        victim = None
        for j in jobs.values():
            if j["status"] == "done" and j.get("output") is not None:
                victim = j
                break
        if victim is not None:
            data_bytes -= len(victim["output"])
            job_done.pop(victim["id"], None)
            jobs.pop(victim["id"])
            evicted.append({"kind": "job", "id": victim["id"]})
        elif files:
            _, fr = files.popitem(last=False)
            data_bytes -= len(fr["enc"])
            evicted.append({"kind": "file", "id": fr["id"]})
        else:
            break
    if evicted:
        audit("evict", angefordert=need, belegt=data_bytes, entfernt=evicted)
    return evicted

class Job(BaseModel):
    cmd: str = Field(max_length=10000)  # Laengenlimit; Verstoss -> 422

@app.get("/healthz")
async def healthz():
    return {"ok": True}

@app.get("/usage")
async def usage(request: Request, authorization: str | None = Header(None)):
    auth_any(request, authorization)
    return {
        "service": "agent-sandbox-relay",
        "auth": "Authorization: Bearer <token>; Rollen: Submitter (Jobs einspeisen/abfragen) und Worker (Jobs ziehen/Resultate liefern); alle Endpunkte ausser GET /healthz",
        "rollen": {
            "submit": "Submit-Token: POST /jobs, GET /jobs/<id>, GET /jobs/<id>/wait, Datei-Endpunkte",
            "worker": "Worker-Token: GET /jobs/next, POST /jobs/<id>/result, Datei-Endpunkte",
            "admin": "Verwaltung (Tokens erstellen/zuordnen/loeschen, Audit): GET /admin, Login mit AGENT_SANDBOX_ADMIN_PASSWORD",
        },
        "grenzen": {"cmd_zeichen": 10000, "result_output_bytes": OUTMAX,
                    "datei_bytes_roh": FILEMAX, "job_store": 500,
                    "datei_store": FILESTOREMAX,
                    "speicherbudget_output_dateien": BUDGET,
                    "long_poll_s": 300, "wait_s_max": 300},
        "endpunkte": {
            "POST /jobs": {"body": {"cmd": "..."}, "antwort": {"id": "..."}, "aufrufer": "Submit-Token"},
            "GET /jobs/next": {"antwort": "{'id': '...', 'cmd': '...'} oder 204 (Long-Poll bis 300 s, nur Jobs des eigenen Workers)", "aufrufer": "Worker-Token"},
            "POST /jobs/<id>/result": {"body": {"output": "...", "rc": 0}, "aufrufer": "Worker-Token (nur eigene Jobs)"},
            "GET /jobs/<id>": {"antwort": "Status/Output", "aufrufer": "Submit-Token (nur eigene Jobs)"},
            "GET /jobs/<id>/wait": {"query": "timeout=Sekunden (Default 25, max 300)", "antwort": "Job-Zustand, wartet bis done oder Timeout (kein Pollen noetig)", "aufrufer": "Submit-Token (nur eigene Jobs)"},
            "POST /files": {"body": "Rohbytes der Datei; Server speichert gzip+base64-kodiert", "antwort": {"id": "...", "size": 0, "size_enc": 0}, "aufrufer": "beide Rollen"},
            "GET /files/<id>": {"antwort": "Rohbytes (application/octet-stream)", "aufrufer": "beide Rollen"},
            "GET /usage": {"antwort": "diese Dokumentation", "aufrufer": "beide Rollen"},
            "GET /healthz": {"antwort": {"ok": True}, "aufrufer": "Healthcheck, ohne Token"},
            "GET /admin": {"antwort": "Verwaltungsoberflaeche (Login)", "aufrufer": "Verwaltung"},
        },
        "dateitransfer": {
            "datei_in_die_vm": "1) POST /files mit Rohbytes -> id; 2) Job cmd 'VIBE_FETCH <id> <zielpfad>'",
            "datei_aus_der_vm": "Job cmd 'VIBE_SEND <pfad>' -> Result enthaelt 'VIBE_SEND id=<id>'; dann GET /files/<id>",
            "fallback": "Job cmd 'base64 -w0 <datei>' (Base64-Text im Result, ohne Datei-Endpunkte)",
        },
        "beispiele": [
            "curl -X POST https://<agent-sandbox-relay-domain>/jobs -H 'Authorization: Bearer <token>' -H 'Content-Type: application/json' -d '{\"cmd\":\"uname -a\"}'",
            "curl 'https://<agent-sandbox-relay-domain>/jobs/<id>/wait?timeout=25' -H 'Authorization: Bearer <token>'",
            "curl -X POST https://<agent-sandbox-relay-domain>/files -H 'Authorization: Bearer <token>' --data-binary @datei.bin",
        ],
    }

@app.post("/jobs")
async def submit(job: Job, request: Request, authorization: str | None = Header(None)):
    global data_bytes
    sub = auth_bearer(request, authorization, "submitter")
    w = next((w for w in workers if w["id"] == sub["worker_id"]), None)
    if w is None:
        raise HTTPException(status_code=409, detail="zugeordneter Worker existiert nicht mehr")
    jid = uuid.uuid4().hex[:12]
    jobs[jid] = {"id": jid, "cmd": job.cmd, "status": "queued", "output": None, "rc": None,
                 "ts": time.time(), "worker_id": w["id"], "worker": w["name"],
                 "submitter_id": sub["id"], "submitter": sub["name"]}
    if len(jobs) > 500:
        _, old = jobs.popitem(last=False)  # Begrenzung: aelteste Jobs zuerst raus
        if old.get("output") is not None:
            data_bytes -= len(old["output"])
        job_done.pop(old["id"], None)
        audit("evict", ursache="job_store_voll", entfernt=[{"kind": "job", "id": old["id"]}])
    evs.setdefault(w["id"], asyncio.Event()).set()
    audit("submit", jid=jid, submitter=sub["name"], worker=w["name"], cmd_zeichen=len(job.cmd))
    return {"id": jid}

def _take_queued(worker_id):
    for j in jobs.values():
        if j["status"] == "queued" and j["worker_id"] == worker_id:
            j["status"] = "running"
            return j
    return None

@app.get("/jobs/next")
async def next_job(request: Request, authorization: str | None = Header(None)):
    w = auth_bearer(request, authorization, "worker")
    ev = evs.setdefault(w["id"], asyncio.Event())
    ev.clear()  # erst zuruecksetzen, dann ziehen: kein verlorenes Aufwecken
    j = _take_queued(w["id"])
    if j:
        audit("deliver", jid=j["id"], worker=w["name"])
        return {"id": j["id"], "cmd": j["cmd"]}
    try:
        await asyncio.wait_for(ev.wait(), timeout=300)
    except asyncio.TimeoutError:
        return Response(status_code=204)
    if await request.is_disconnected():  # verwaister Long-Poll: toter Client zieht keinen Job aus der Queue
        return Response(status_code=204)
    j = _take_queued(w["id"])
    if j:
        audit("deliver", jid=j["id"], worker=w["name"])
        return {"id": j["id"], "cmd": j["cmd"]}
    return Response(status_code=204)

@app.post("/jobs/{jid}/result")
async def post_result(jid: str, body: dict, request: Request, authorization: str | None = Header(None)):
    global data_bytes
    w = auth_bearer(request, authorization, "worker")
    if jid not in jobs:
        raise HTTPException(status_code=404)
    if jobs[jid]["worker_id"] != w["id"]:
        audit("auth_fail", path="result", detail="falscher Worker", jid=jid, worker=w["name"])
        raise HTTPException(status_code=403)
    out = body.get("output")
    if isinstance(out, str):
        if len(out) > OUTMAX:
            out = out[:OUTMAX]  # Output-Deckel auch serverseitig
        _evict_for(len(out))
        if data_bytes + len(out) > BUDGET:  # Einzelresult groesser als Restbudget: kuerzen statt OOM
            marker = "\n[agent-sandbox-relay: Output gekuerzt, Speicherbudget erreicht]"
            allowed = max(0, BUDGET - data_bytes - len(marker))
            out = out[:allowed] + marker
            audit("trim", jid=jid, bytes_gekuerzt=len(out))
        data_bytes += len(out)
    jobs[jid].update(status="done", output=out, rc=body.get("rc"))
    job_done.setdefault(jid, asyncio.Event()).set()
    audit("result", jid=jid, worker=w["name"], rc=body.get("rc"), out_bytes=len(out) if isinstance(out, str) else 0)
    return {"ok": True}

def _own_job(request, jid, authorization):
    sub = auth_bearer(request, authorization, "submitter")
    if jid not in jobs:
        raise HTTPException(status_code=404)
    if jobs[jid]["submitter_id"] != sub["id"]:
        audit("auth_fail", path="job_zugriff", detail="fremder Job", jid=jid, submitter=sub["name"])
        raise HTTPException(status_code=403)
    return sub, jobs[jid]

@app.get("/jobs/{jid}")
async def get_job(jid: str, request: Request, authorization: str | None = Header(None)):
    _, j = _own_job(request, jid, authorization)
    return j

@app.get("/jobs/{jid}/wait")
async def wait_job(jid: str, request: Request, timeout: int = 25, authorization: str | None = Header(None)):
    # Status-Long-Poll: wartet bis das Result vorliegt oder der Timeout ablaeuft - ersetzt das Pollen des Absenders
    _own_job(request, jid, authorization)
    timeout = max(1, min(int(timeout), 300))
    deadline = time.monotonic() + timeout
    ev = job_done.setdefault(jid, asyncio.Event())
    while True:
        ev.clear()  # erst zuruecksetzen, dann Zustand pruefen: kein verlorenes Aufwecken
        j = jobs.get(jid)
        if j is None:
            raise HTTPException(status_code=404)
        if j["status"] == "done":
            return j
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return j
        try:
            await asyncio.wait_for(ev.wait(), timeout=remaining)
        except asyncio.TimeoutError:
            j = jobs.get(jid)
            if j is None:
                raise HTTPException(status_code=404)
            return j

@app.post("/files")
async def upload_file(request: Request, authorization: str | None = Header(None)):
    global data_bytes
    rec, role = auth_any(request, authorization)
    try:
        declared = int(request.headers.get("content-length", ""))
    except ValueError:
        declared = -1
    if declared > FILEMAX:  # frueh ablehnen, bevor der Body gelesen wird
        raise HTTPException(status_code=413)
    raw = await request.body()  # Roh-Body ohne Validierung (FastAPI-Request-Objekt)
    if len(raw) > FILEMAX:
        raise HTTPException(status_code=413)
    if not raw:
        raise HTTPException(status_code=422)
    enc = base64.b64encode(gzip.compress(raw)).decode("ascii")  # Speicherform: gzip+base64
    _evict_for(len(enc))
    if data_bytes + len(enc) > BUDGET:
        raise HTTPException(status_code=413, detail="Speicherbudget erreicht")
    data_bytes += len(enc)
    fid = "f" + uuid.uuid4().hex[:12]
    files[fid] = {"id": fid, "enc": enc, "size": len(raw), "size_enc": len(enc), "ts": time.time()}
    if len(files) > FILESTOREMAX:
        _, old = files.popitem(last=False)  # Begrenzung: aelteste Dateien zuerst raus
        data_bytes -= len(old["enc"])
        audit("evict", ursache="datei_store_voll", entfernt=[{"kind": "file", "id": old["id"]}])
    audit("file_up", fid=fid, size=len(raw), rolle=role, name=rec["name"])
    return {"id": fid, "size": len(raw), "size_enc": len(enc)}

@app.get("/files/{fid}")
async def get_file(fid: str, request: Request, authorization: str | None = Header(None)):
    rec, role = auth_any(request, authorization)
    if fid not in files:
        raise HTTPException(status_code=404)
    raw = gzip.decompress(base64.b64decode(files[fid]["enc"]))  # Rueckverwandlung
    audit("file_down", fid=fid, size=len(raw), rolle=role, name=rec["name"])
    return Response(content=raw, media_type="application/octet-stream")

# ---- Verwaltung (Admin): Login, Tokens, Audit ----

class TokenCreate(BaseModel):
    role: str = Field(pattern="^(worker|submitter)$")
    name: str = Field(min_length=1, max_length=64)
    worker_id: str | None = None  # Pflicht fuer Submitter: jeder Submitter haengt an genau einem Worker

class TokenAssign(BaseModel):
    worker_id: str = Field(min_length=1)

def _admin_pw():
    if ADMIN_PASSWORD:
        return ADMIN_PASSWORD, "basic"
    if TOKEN:
        return TOKEN, "bootstrap"  # Migration: Legacy-Token als Admin-Passwort, bis AGENT_SANDBOX_ADMIN_PASSWORD gesetzt ist
    return None, None

async def _require_admin(request, credentials):
    pw, mode = _admin_pw()
    if pw is None:
        raise HTTPException(status_code=503, detail="Verwaltung deaktiviert: AGENT_SANDBOX_ADMIN_PASSWORD und Legacy-Token ungesetzt")
    peer, xff = _peer(request)
    ok = credentials is not None and credentials.password.isascii() \
        and secrets.compare_digest(credentials.username.encode(), b"admin") \
        and secrets.compare_digest(credentials.password.encode(), pw.encode())
    if not ok:
        await asyncio.sleep(0.5)  # Brute-Force bremsen
        audit("admin_auth_fail", peer=peer, xff=xff, modus=mode)
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic realm=\"agent-sandbox-relay\""})
    return mode

@app.get("/admin")
async def admin_page():
    return HTMLResponse(ADMIN_HTML, headers={"Cache-Control": "no-store"})

@app.get("/admin/api/state")
async def admin_state(request: Request, credentials: HTTPBasicCredentials | None = Depends(security)):
    await _require_admin(request, credentials)
    return {"budget": BUDGET, "data_bytes": data_bytes, "jobs": len(jobs),
            "files": len(files), "workers": len(workers), "submitters": len(submitters)}

@app.get("/admin/api/tokens")
async def admin_tokens(request: Request, credentials: HTTPBasicCredentials | None = Depends(security)):
    mode = await _require_admin(request, credentials)
    wn = {w["id"]: w["name"] for w in workers}
    return {
        "admin_mode": mode,
        "workers": [{"id": w["id"], "name": w["name"], "legacy": w.get("legacy", False), "created": w["created"]} for w in workers],
        "submitters": [{"id": s["id"], "name": s["name"], "legacy": s.get("legacy", False),
                        "worker_id": s["worker_id"], "worker_name": wn.get(s["worker_id"], "?"), "created": s["created"]} for s in submitters],
    }

@app.post("/admin/api/tokens")
async def admin_create(body: TokenCreate, request: Request, credentials: HTTPBasicCredentials | None = Depends(security)):
    await _require_admin(request, credentials)
    plain = secrets.token_urlsafe(32)  # 32 Byte Zufall gelten als ausreichend (Python-secrets-Doku)
    if body.role == "worker":
        tok = "w-" + plain
        rec = {"id": "w-" + uuid.uuid4().hex[:10], "name": body.name, "hash": hashlib.sha256(tok.encode()).hexdigest(), "created": _now()}
        workers.append(rec)
        save_state()
        _reindex()
        audit("token_create", rolle="worker", name=body.name, id=rec["id"])
        return {"id": rec["id"], "role": "worker", "name": body.name, "token": tok}
    w = next((w for w in workers if w["id"] == body.worker_id), None)
    if not body.worker_id or w is None:
        raise HTTPException(status_code=400, detail="Submitter braucht eine gueltige worker_id")
    tok = "s-" + plain
    rec = {"id": "s-" + uuid.uuid4().hex[:10], "name": body.name, "hash": hashlib.sha256(tok.encode()).hexdigest(),
           "worker_id": w["id"], "created": _now()}
    submitters.append(rec)
    save_state()
    _reindex()
    audit("token_create", rolle="submitter", name=body.name, id=rec["id"], worker=w["name"])
    return {"id": rec["id"], "role": "submitter", "name": body.name, "worker": w["name"], "token": tok}

@app.post("/admin/api/tokens/{tid}/assign")
async def admin_assign(tid: str, body: TokenAssign, request: Request, credentials: HTTPBasicCredentials | None = Depends(security)):
    await _require_admin(request, credentials)
    s = next((s for s in submitters if s["id"] == tid), None)
    if s is None:
        raise HTTPException(status_code=404, detail="Submitter nicht gefunden")
    w = next((w for w in workers if w["id"] == body.worker_id), None)
    if w is None:
        raise HTTPException(status_code=400, detail="worker_id unbekannt")
    alt = next((x["name"] for x in workers if x["id"] == s["worker_id"]), "?")
    s["worker_id"] = w["id"]
    save_state()
    audit("token_assign", submitter=s["name"], von=alt, nach=w["name"])
    return {"ok": True, "submitter": s["name"], "worker": w["name"]}

@app.delete("/admin/api/tokens/{tid}")
async def admin_delete(tid: str, request: Request, credentials: HTTPBasicCredentials | None = Depends(security)):
    await _require_admin(request, credentials)
    s = next((s for s in submitters if s["id"] == tid), None)
    if s is not None:
        submitters.remove(s)
        save_state()
        _reindex()
        audit("token_delete", rolle="submitter", name=s["name"], id=tid)
        return {"ok": True}
    w = next((w for w in workers if w["id"] == tid), None)
    if w is not None:
        if any(s["worker_id"] == tid for s in submitters):
            raise HTTPException(status_code=409, detail="Worker hat noch zugeordnete Submitter")
        if any(j["worker_id"] == tid and j["status"] == "queued" for j in jobs.values()):
            raise HTTPException(status_code=409, detail="Worker hat noch offene Jobs")
        workers.remove(w)
        save_state()
        _reindex()
        audit("token_delete", rolle="worker", name=w["name"], id=tid)
        return {"ok": True}
    raise HTTPException(status_code=404, detail="Token nicht gefunden")

@app.get("/admin/api/audit")
async def admin_audit(request: Request, limit: int = 50, credentials: HTTPBasicCredentials | None = Depends(security)):
    await _require_admin(request, credentials)
    limit = max(1, min(int(limit), 500))
    p = os.path.join(DATA_DIR, "audit.jsonl")
    lines = []
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
    out = []
    for ln in reversed(lines):  # neueste zuerst
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return {"events": out}

ADMIN_HTML = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>agent-sandbox-relay Verwaltung</title>
<style>
body{font-family:system-ui,sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem}
table{border-collapse:collapse;width:100%;margin:.4rem 0 1rem}
th,td{border:1px solid #ccc;padding:.35rem .6rem;text-align:left;font-size:.9rem}
th{background:#f3f3f3}
#tokbox{background:#f7f7f7;border:1px solid #888;padding:.6rem;margin:.6rem 0;white-space:pre-wrap;word-break:break-all}
.muted{color:#666;font-size:.85rem}
input,select{padding:.25rem}
</style>
</head>
<body>
<h1>agent-sandbox-relay Verwaltung</h1>
<div id="login">
  <p>Login (Nutzername <code>admin</code>):</p>
  <input type="password" id="pw" placeholder="Admin-Passwort">
  <button onclick="doLogin()">Login</button>
  <span id="loginmsg" class="muted"></span>
</div>
<div id="app" hidden>
  <p class="muted" id="mode"></p>
  <button onclick="logout()">Logout</button>
  <h2>Workers (VM-Agenten)</h2>
  <table id="wtab"><thead><tr><th>Name</th><th>ID</th><th>angelegt</th><th></th></tr></thead><tbody></tbody></table>
  <p>Neu: <input id="wname" placeholder="Name, z.B. vm1"> <button onclick="addWorker()">Worker anlegen</button></p>
  <h2>Submitters (Absender, je einem Worker zugeordnet)</h2>
  <table id="stab"><thead><tr><th>Name</th><th>ID</th><th>Worker</th><th>angelegt</th><th>Umziehen</th><th></th></tr></thead><tbody></tbody></table>
  <p>Neu: <input id="sname" placeholder="Name, z.B. vibe"> Worker: <select id="sworker"></select> <button onclick="addSubmitter()">Submitter anlegen</button></p>
  <h2>Speicher</h2>
  <p id="state" class="muted"></p>
  <h2>Protokoll (neueste zuerst)</h2>
  <table id="atab"><thead><tr><th>Zeit (UTC)</th><th>Ereignis</th><th>Details</th></tr></thead><tbody></tbody></table>
  <div id="tokbox" hidden></div>
</div>
<script>
"use strict";
var PW = sessionStorage.getItem("relayadmin") || "";
function hdr(extra){ var h = {"Authorization": "Basic " + btoa("admin:" + PW)}; if (extra) { for (var k in extra) { h[k] = extra[k]; } } return h; }
async function api(path, opts){
  opts = opts || {};
  opts.headers = hdr(opts.headers);
  var r = await fetch(path, opts);
  if (r.status === 401){ showLogin("Login fehlgeschlagen"); throw new Error("401"); }
  if (r.status === 503){ alert("Verwaltung deaktiviert (kein Admin-Passwort gesetzt)"); throw new Error("503"); }
  if (!r.ok){ var t = await r.text(); alert("Fehler " + r.status + ": " + t); throw new Error(String(r.status)); }
  return r.json();
}
function showLogin(msg){
  document.getElementById("app").hidden = true;
  document.getElementById("login").hidden = false;
  if (msg){ document.getElementById("loginmsg").textContent = msg; }
}
async function doLogin(){
  PW = document.getElementById("pw").value;
  try { await refresh(); sessionStorage.setItem("relayadmin", PW); document.getElementById("login").hidden = true; document.getElementById("app").hidden = false; }
  catch (e){}
}
function logout(){ sessionStorage.removeItem("relayadmin"); location.reload(); }
function esc(s){ return String(s).replace(/[&<>"]/g, function(c){ return {"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;"}[c]; }); }
function showToken(tok, name){
  var b = document.getElementById("tokbox");
  b.hidden = false;
  b.textContent = "Token fuer '" + name + "' (nur diesmal sichtbar, jetzt sichern):\\n" + tok;
}
async function refresh(){
  var d = await api("/admin/api/tokens");
  document.getElementById("mode").textContent = "Admin-Modus: " + d.admin_mode + " | Workers: " + d.workers.length + " | Submitters: " + d.submitters.length;
  var wt = document.querySelector("#wtab tbody"); wt.innerHTML = "";
  var sw = document.getElementById("sworker"); sw.innerHTML = "";
  d.workers.forEach(function(w){
    wt.insertAdjacentHTML("beforeend", "<tr><td>" + esc(w.name) + (w.legacy ? " (legacy)" : "") + "</td><td>" + esc(w.id) + "</td><td>" + esc(w.created) + "</td><td><button data-id='" + esc(w.id) + "' class='delw'>loeschen</button></td></tr>");
    sw.insertAdjacentHTML("beforeend", "<option value='" + esc(w.id) + "'>" + esc(w.name) + "</option>");
  });
  var st = document.querySelector("#stab tbody"); st.innerHTML = "";
  d.submitters.forEach(function(s){
    var opts = d.workers.map(function(w){ return "<option value='" + esc(w.id) + "'" + (w.id === s.worker_id ? " selected" : "") + ">" + esc(w.name) + "</option>"; }).join("");
    st.insertAdjacentHTML("beforeend", "<tr><td>" + esc(s.name) + (s.legacy ? " (legacy)" : "") + "</td><td>" + esc(s.id) + "</td><td>" + esc(s.worker_name) + "</td><td>" + esc(s.created) + "</td><td><select id='as-" + esc(s.id) + "'>" + opts + "</select> <button data-id='" + esc(s.id) + "' class='ass'>speichern</button></td><td><button data-id='" + esc(s.id) + "' class='dels'>loeschen</button></td></tr>");
  });
  Array.prototype.forEach.call(document.querySelectorAll(".delw"), function(b){ b.onclick = function(){ del(b.dataset.id, "worker"); }; });
  Array.prototype.forEach.call(document.querySelectorAll(".dels"), function(b){ b.onclick = function(){ del(b.dataset.id, "submitter"); }; });
  Array.prototype.forEach.call(document.querySelectorAll(".ass"), function(b){ b.onclick = function(){ assign(b.dataset.id); }; });
  var stt = await api("/admin/api/state");
  document.getElementById("state").textContent = "Budget belegt: " + stt.data_bytes + " / " + stt.budget + " Bytes | Jobs: " + stt.jobs + " | Dateien: " + stt.files;
  var au = await api("/admin/api/audit?limit=50");
  var at = document.querySelector("#atab tbody"); at.innerHTML = "";
  au.events.forEach(function(e){
    var rest = JSON.parse(JSON.stringify(e)); delete rest.ts; delete rest.event;
    at.insertAdjacentHTML("beforeend", "<tr><td>" + esc(e.ts) + "</td><td>" + esc(e.event) + "</td><td>" + esc(JSON.stringify(rest)) + "</td></tr>");
  });
}
async function addWorker(){
  var n = document.getElementById("wname").value.trim();
  if (!n){ return; }
  var d = await api("/admin/api/tokens", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({role: "worker", name: n}) });
  showToken(d.token, n);
  document.getElementById("wname").value = "";
  refresh();
}
async function addSubmitter(){
  var n = document.getElementById("sname").value.trim();
  var w = document.getElementById("sworker").value;
  if (!n || !w){ return; }
  var d = await api("/admin/api/tokens", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({role: "submitter", name: n, worker_id: w}) });
  showToken(d.token, n);
  document.getElementById("sname").value = "";
  refresh();
}
async function assign(tid){
  var v = document.getElementById("as-" + tid).value;
  await api("/admin/api/tokens/" + tid + "/assign", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({worker_id: v}) });
  refresh();
}
async function del(tid, role){
  if (!confirm("Token loeschen? Der Zugang endet sofort.")){ return; }
  await api("/admin/api/tokens/" + tid, { method: "DELETE" });
  refresh();
}
if (PW){ doLogin(); }
</script>
</body>
</html>
"""
