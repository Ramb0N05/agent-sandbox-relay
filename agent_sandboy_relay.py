# agent_sandboy_relay.py - minimale Job-Queue fuer die vibe-sandbox-VM (Transportoption E), In-Memory
# Repo: https://github.com/Ramb0N05/agent-sandbox-relay
# Ausfuehrung: uvicorn agent_sandboy_relay:app (siehe Dockerfile); Docker-Service heisst agent-sandboy-relay
import asyncio, os, time, uuid
from collections import OrderedDict
from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field

TOKEN = os.environ.get("AGENT_SANDBOY_RELAY_TOKEN", "")
OUTMAX = 4 * 1024 * 1024  # Result-Output-Deckel: 4 MiB (Nutzervorgabe 2-10 MB, Empfehlung 4 MB)
if not TOKEN:
    raise SystemExit("AGENT_SANDBOY_RELAY_TOKEN ist nicht gesetzt")  # fail-closed
app = FastAPI()
jobs: "OrderedDict[str, dict]" = OrderedDict()
ev = asyncio.Event()

class Job(BaseModel):
    cmd: str = Field(max_length=10000)  # Laengenlimit; Verstoss -> 422

def check(authorization):
    if authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401)

@app.get("/healthz")
async def healthz():
    return {"ok": True}

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
