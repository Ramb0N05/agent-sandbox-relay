# relay/admin.py - Verwaltung: Tokens, Jobs, Audit, Admin-UI (elfte Runde)
import asyncio
import hashlib
import json
import os
import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

from . import auth, config, jobs, store

router = APIRouter()
security = HTTPBasic(auto_error=False)

_TEMPLATE = os.path.join(os.path.dirname(__file__), "templates", "admin.html")


class TokenCreate(BaseModel):
    role: str = Field(pattern="^(worker|submitter)$")
    name: str | None = Field(default=None, max_length=64)  # optional: leer/None -> automatisch benannt
    worker_id: str | None = None  # Pflicht fuer Submitter: jeder Submitter haengt an genau einem Worker


class TokenRename(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class TokenAssign(BaseModel):
    worker_id: str = Field(min_length=1)


def _auto_name(prefix):
    return prefix + "-" + uuid.uuid4().hex[:6]


@router.get("/admin")
async def admin_page():
    with open(_TEMPLATE, encoding="utf-8") as f:
        html = f.read()
    nonce = secrets.token_urlsafe(16)
    html = html.replace("__NONCE__", nonce)
    return HTMLResponse(html, headers={
        "Content-Security-Policy": "default-src 'none'; script-src 'nonce-%s'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'" % nonce,
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    })


@router.get("/admin/api/state")
async def admin_state(request: Request, credentials: HTTPBasicCredentials | None = Depends(security)):
    await auth.require_admin(request, credentials)
    return {"budget": config.budget(), "data_bytes": store.data_bytes, "jobs": len(store.jobs),
            "files": len(store.files), "workers": len(store.workers), "submitters": len(store.submitters),
            "exec_jobs_je_submitter": config.exec_jobs_shown()}


@router.get("/admin/api/usage")
async def admin_usage(request: Request, credentials: HTTPBasicCredentials | None = Depends(security)):
    # API-Doku fuer die Verwaltungsoberflaeche: dieselbe Struktur wie GET /usage, aber mit der HTTP-Basic-Session statt eines Bearer-Tokens
    await auth.require_admin(request, credentials)
    return jobs.usage_data()


@router.get("/admin/api/tokens")
async def admin_tokens(request: Request, credentials: HTTPBasicCredentials | None = Depends(security)):
    mode = await auth.require_admin(request, credentials)
    wn = {w["id"]: w["name"] for w in store.workers}
    return {
        "admin_mode": mode,
        "workers": [{"id": w["id"], "name": w["name"], "legacy": w.get("legacy", False), "created": w["created"]} for w in store.workers],
        "submitters": [{"id": s["id"], "name": s["name"], "legacy": s.get("legacy", False),
                        "worker_id": s["worker_id"], "worker_name": wn.get(s["worker_id"], "?"), "created": s["created"]} for s in store.submitters],
    }


@router.post("/admin/api/tokens")
async def admin_create(body: TokenCreate, request: Request, credentials: HTTPBasicCredentials | None = Depends(security)):
    await auth.require_admin(request, credentials)
    plain = secrets.token_urlsafe(32)  # 32 Byte Zufall gelten als ausreichend (Python-secrets-Doku)
    name = (body.name or "").strip() or None
    if body.role == "worker":
        name = name or _auto_name("worker")
        tok = "w-" + plain
        rec = {"id": "w-" + uuid.uuid4().hex[:10], "name": name, "hash": hashlib.sha256(tok.encode()).hexdigest(), "created": store._now()}
        store.workers.append(rec)
        store.save_state()
        store._reindex()
        store.audit("token_create", rolle="worker", name=name, id=rec["id"])
        return {"id": rec["id"], "role": "worker", "name": name, "token": tok}
    w = next((w for w in store.workers if w["id"] == body.worker_id), None)
    if not body.worker_id or w is None:
        raise HTTPException(status_code=400, detail="Submitter braucht eine gueltige worker_id")
    name = name or _auto_name("submitter")
    tok = "s-" + plain
    rec = {"id": "s-" + uuid.uuid4().hex[:10], "name": name, "hash": hashlib.sha256(tok.encode()).hexdigest(),
           "worker_id": w["id"], "created": store._now()}
    store.submitters.append(rec)
    store.save_state()
    store._reindex()
    store.audit("token_create", rolle="submitter", name=name, id=rec["id"], worker=w["name"])
    return {"id": rec["id"], "role": "submitter", "name": name, "worker": w["name"], "token": tok}


@router.patch("/admin/api/tokens/{tid}")
async def admin_rename(tid: str, body: TokenRename, request: Request, credentials: HTTPBasicCredentials | None = Depends(security)):
    # Umbenennen von Worker oder Submitter (Anzeige in Admin-UI und Audit)
    await auth.require_admin(request, credentials)
    rec = next((x for x in store.workers if x["id"] == tid), None)
    rolle = "worker"
    if rec is None:
        rec = next((x for x in store.submitters if x["id"] == tid), None)
        rolle = "submitter"
    if rec is None:
        raise HTTPException(status_code=404, detail="Token nicht gefunden")
    alt = rec["name"]
    rec["name"] = body.name
    # Jobs in der Queue behalten den alten Anzeigenamen (historische Kennung); neue Jobs tragen den neuen Namen
    store.save_state()
    store.audit("token_rename", rolle=rolle, id=tid, von=alt, nach=body.name)
    return {"ok": True, "rolle": rolle, "name": body.name}


@router.post("/admin/api/tokens/{tid}/assign")
async def admin_assign(tid: str, body: TokenAssign, request: Request, credentials: HTTPBasicCredentials | None = Depends(security)):
    await auth.require_admin(request, credentials)
    s = next((s for s in store.submitters if s["id"] == tid), None)
    if s is None:
        raise HTTPException(status_code=404, detail="Submitter nicht gefunden")
    w = next((w for w in store.workers if w["id"] == body.worker_id), None)
    if w is None:
        raise HTTPException(status_code=400, detail="worker_id unbekannt")
    alt = next((x["name"] for x in store.workers if x["id"] == s["worker_id"]), "?")
    s["worker_id"] = w["id"]
    store.save_state()
    store.audit("token_assign", submitter=s["name"], von=alt, nach=w["name"])
    return {"ok": True, "submitter": s["name"], "worker": w["name"]}


@router.delete("/admin/api/tokens/{tid}")
async def admin_delete(tid: str, request: Request, credentials: HTTPBasicCredentials | None = Depends(security)):
    await auth.require_admin(request, credentials)
    s = next((s for s in store.submitters if s["id"] == tid), None)
    if s is not None:
        store.submitters.remove(s)
        store.save_state()
        store._reindex()
        store.audit("token_delete", rolle="submitter", name=s["name"], id=tid)
        return {"ok": True}
    w = next((w for w in store.workers if w["id"] == tid), None)
    if w is not None:
        if any(s["worker_id"] == tid for s in store.submitters):
            raise HTTPException(status_code=409, detail="Worker hat noch zugeordnete Submitter")
        if any(j["worker_id"] == tid and j["status"] in ("queued", "paused") for j in store.jobs.values()):
            raise HTTPException(status_code=409, detail="Worker hat noch offene Jobs")
        store.workers.remove(w)
        store.save_state()
        store._reindex()
        store.audit("token_delete", rolle="worker", name=w["name"], id=tid)
        return {"ok": True}
    raise HTTPException(status_code=404, detail="Token nicht gefunden")


def _job_view(j):
    cmd = j.get("cmd") or ""
    if len(cmd) > 80:  # im Listing nie den vollen cmd (Ausfuehrungspläne gehoeren nicht in die Uebersicht)
        cmd = cmd[:80] + "..."
    return {"id": j["id"], "status": j["status"], "worker": j.get("worker"), "submitter": j.get("submitter"),
            "rc": j.get("rc"), "out_bytes": len(j["output"]) if isinstance(j.get("output"), str) else 0,
            "ts": j["ts"], "cmd": cmd}


@router.get("/admin/api/jobs")
async def admin_jobs(request: Request, credentials: HTTPBasicCredentials | None = Depends(security)):
    await auth.require_admin(request, credentials)
    shown = config.exec_jobs_shown()
    subs = {s["id"]: s["name"] for s in store.submitters}
    groups = [{"id": s["id"], "name": s["name"]} for s in store.submitters]
    open_by: dict = {s["id"]: [] for s in store.submitters}
    done_by: dict = {s["id"]: [] for s in store.submitters}
    for j in store.jobs.values():
        sid = j["submitter_id"]
        if sid not in subs:  # Jobs eines geloeschten Submitters nicht verstecken
            if sid not in open_by:
                open_by[sid] = []
                done_by[sid] = []
                groups.append({"id": sid, "name": "?"})
        if j["status"] == "done":
            done_by[sid].append(_job_view(j))
        else:  # queued, paused, running
            open_by[sid].append(_job_view(j))
    out = []
    for g in groups:
        done = sorted(done_by[g["id"]], key=lambda v: v["ts"], reverse=True)[:shown]  # neueste zuerst
        offen = sorted(open_by[g["id"]], key=lambda v: v["ts"])  # aelteste zuerst
        out.append({"id": g["id"], "name": g["name"], "offen": offen, "erledigt": done})
    return {"submitters": out}


@router.post("/admin/api/jobs/{jid}/pause")
async def admin_pause(jid: str, request: Request, credentials: HTTPBasicCredentials | None = Depends(security)):
    await auth.require_admin(request, credentials)
    j = store.jobs.get(jid)
    if j is None:
        raise HTTPException(status_code=404, detail="Job nicht gefunden")
    if j["status"] != "queued":
        raise HTTPException(status_code=409, detail="nur wartende Jobs lassen sich anhalten")
    j["status"] = "paused"
    store.audit("job_pause", jid=jid)
    return {"ok": True}


@router.post("/admin/api/jobs/{jid}/resume")
async def admin_resume(jid: str, request: Request, credentials: HTTPBasicCredentials | None = Depends(security)):
    await auth.require_admin(request, credentials)
    j = store.jobs.get(jid)
    if j is None:
        raise HTTPException(status_code=404, detail="Job nicht gefunden")
    if j["status"] != "paused":
        raise HTTPException(status_code=409, detail="nur angehaltene Jobs lassen sich fortsetzen")
    j["status"] = "queued"
    store.evs.setdefault(j["worker_id"], asyncio.Event()).set()  # wartende Worker sofort wecken
    store.audit("job_resume", jid=jid)
    return {"ok": True}


@router.delete("/admin/api/jobs/{jid}")
async def admin_delete_job(jid: str, request: Request, credentials: HTTPBasicCredentials | None = Depends(security)):
    await auth.require_admin(request, credentials)
    j = store.jobs.pop(jid, None)
    if j is None:
        raise HTTPException(status_code=404, detail="Job nicht gefunden")
    if isinstance(j.get("output"), str):  # belegtes Budget zurueckgeben
        store.data_bytes -= len(j["output"])
    store.job_done.pop(jid, None)
    store.audit("job_delete", jid=jid, status=j["status"], submitter=j.get("submitter"))
    return {"ok": True}


@router.get("/admin/api/audit")
async def admin_audit(request: Request, limit: int = 50, credentials: HTTPBasicCredentials | None = Depends(security)):
    await auth.require_admin(request, credentials)
    limit = max(1, min(int(limit), 500))
    p = os.path.join(config.data_dir(), "audit.jsonl")
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
