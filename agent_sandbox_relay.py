# agent_sandbox_relay.py - minimale Job-Queue + Datei-Transport (Transportoption E), In-Memory
# Quelle: https://github.com/Ramb0N05/agent-sandbox-relay (Branch main)
# Ausfuehrung: uvicorn agent_sandbox_relay:app (siehe Dockerfile); Docker-Service heisst agent-sandbox-relay
import asyncio, base64, gzip, os, time, uuid
from collections import OrderedDict
from fastapi import FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

TOKEN = os.environ.get("AGENT_SANDBOX_RELAY_TOKEN", "")
OUTMAX = 8 * 1024 * 1024  # Result-Output-Deckel 8 MiB (Nutzerentscheidung 11/2026, Abschnitt 10.7)
FILEMAX = 8 * 1024 * 1024  # Roh-Groesse pro Datei im Datei-Store (Abschnitt 10.8)
FILESTOREMAX = 64  # Eintraege im Datei-Store (FIFO, aelteste zuerst raus)
if not TOKEN:
    raise SystemExit("AGENT_SANDBOX_RELAY_TOKEN ist nicht gesetzt")  # fail-closed
app = FastAPI()
jobs: "OrderedDict[str, dict]" = OrderedDict()
files: "OrderedDict[str, dict]" = OrderedDict()
ev = asyncio.Event()

class Job(BaseModel):
    cmd: str = Field(max_length=10000)  # Laengenlimit; Verstoss -> 422

def check(authorization):
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401)

@app.get("/healthz")
async def healthz():
    return {"ok": True}

@app.get("/usage")
async def usage(authorization: str | None = Header(None)):
    check(authorization)
    return {
        "service": "agent-sandbox-relay",
        "auth": "Authorization: Bearer <token>; alle Endpunkte ausser GET /healthz",
        "grenzen": {"cmd_zeichen": 10000, "result_output_bytes": OUTMAX,
                    "datei_bytes_roh": FILEMAX, "job_store": 500,
                    "datei_store": FILESTOREMAX},
        "endpunkte": {
            "POST /jobs": {"body": {"cmd": "..."}, "antwort": {"id": "..."}, "aufrufer": "Absender"},
            "GET /jobs/next": {"antwort": "{'id': '...', 'cmd': '...'} oder 204 (Long-Poll bis 300 s)", "aufrufer": "VM-Agent"},
            "POST /jobs/<id>/result": {"body": {"output": "...", "rc": 0}, "aufrufer": "VM-Agent"},
            "GET /jobs/<id>": {"antwort": "Status/Output", "aufrufer": "Absender"},
            "POST /files": {"body": "Rohbytes der Datei; Server speichert gzip+base64-kodiert", "antwort": {"id": "...", "size": 0, "size_enc": 0}, "aufrufer": "Absender oder VM-Agent (VIBE_SEND)"},
            "GET /files/<id>": {"antwort": "Rohbytes (application/octet-stream); Server wandelt gzip+base64 zurueck", "aufrufer": "Absender oder VM-Agent (VIBE_FETCH)"},
            "GET /usage": {"antwort": "diese Dokumentation", "aufrufer": "Absender"},
            "GET /healthz": {"antwort": {"ok": True}, "aufrufer": "Healthcheck, ohne Token"},
        },
        "dateitransfer": {
            "datei_in_die_vm": "1) POST /files mit Rohbytes -> id; 2) Job cmd 'VIBE_FETCH <id> <zielpfad>'",
            "datei_aus_der_vm": "Job cmd 'VIBE_SEND <pfad>' -> Result enthaelt 'VIBE_SEND id=<id>'; dann GET /files/<id>",
            "fallback": "Job cmd 'base64 -w0 <datei>' (Base64-Text im Result, ohne Datei-Endpunkte)",
        },
        "beispiele": [
            "curl -X POST https://<agent-sandbox-relay-domain>/jobs -H 'Authorization: Bearer <token>' -H 'Content-Type: application/json' -d '{\"cmd\":\"uname -a\"}'",
            "curl -X POST https://<agent-sandbox-relay-domain>/files -H 'Authorization: Bearer <token>' --data-binary @datei.bin",
            "curl https://<agent-sandbox-relay-domain>/files/<id> -H 'Authorization: Bearer <token>' -o download.bin",
        ],
    }

@app.post("/jobs")
async def submit(job: Job, authorization: str | None = Header(None)):
    check(authorization)
    jid = uuid.uuid4().hex[:12]
    jobs[jid] = {"id": jid, "cmd": job.cmd, "status": "queued",
                 "output": None, "rc": None, "ts": time.time()}
    if len(jobs) > 500:
        jobs.popitem(last=False)  # Begrenzung: aelteste Jobs zuerst raus
    ev.set()
    return {"id": jid}

def _take_queued():
    for j in jobs.values():
        if j["status"] == "queued":
            j["status"] = "running"
            return j
    return None

@app.get("/jobs/next")
async def next_job(authorization: str | None = Header(None)):
    check(authorization)
    j = _take_queued()
    if j:
        return {"id": j["id"], "cmd": j["cmd"]}
    ev.clear()
    try:
        await asyncio.wait_for(ev.wait(), timeout=300)
    except asyncio.TimeoutError:
        return Response(status_code=204)
    j = _take_queued()
    if j:
        return {"id": j["id"], "cmd": j["cmd"]}
    return Response(status_code=204)

@app.post("/jobs/{jid}/result")
async def post_result(jid: str, body: dict, authorization: str | None = Header(None)):
    check(authorization)
    if jid not in jobs:
        raise HTTPException(status_code=404)
    out = body.get("output")
    if isinstance(out, str):
        out = out[:OUTMAX]  # Output-Deckel auch serverseitig
    jobs[jid].update(status="done", output=out, rc=body.get("rc"))
    return {"ok": True}

@app.get("/jobs/{jid}")
async def get_job(jid: str, authorization: str | None = Header(None)):
    check(authorization)
    if jid not in jobs:
        raise HTTPException(status_code=404)
    return jobs[jid]

@app.post("/files")
async def upload_file(request: Request, authorization: str | None = Header(None)):
    check(authorization)
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
    fid = "f" + uuid.uuid4().hex[:12]
    files[fid] = {"id": fid, "enc": enc, "size": len(raw), "size_enc": len(enc), "ts": time.time()}
    if len(files) > FILESTOREMAX:
        files.popitem(last=False)  # Begrenzung: aelteste Dateien zuerst raus
    return {"id": fid, "size": len(raw), "size_enc": len(enc)}

@app.get("/files/{fid}")
async def get_file(fid: str, authorization: str | None = Header(None)):
    check(authorization)
    if fid not in files:
        raise HTTPException(status_code=404)
    raw = gzip.decompress(base64.b64decode(files[fid]["enc"]))  # Rueckverwandlung
    return Response(content=raw, media_type="application/octet-stream")
