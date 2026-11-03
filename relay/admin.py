# relay/admin.py - management: tokens, jobs, audit, admin UI (11th round)
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
    name: str | None = Field(default=None, max_length=64)  # optional: empty/None -> auto-named
    worker_id: str | None = None  # required for submitters: each submitter is tied to exactly one worker


class TokenRename(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class TokenAssign(BaseModel):
    worker_id: str = Field(min_length=1)


class SettingsUpdate(BaseModel):
    # settings page (17th round): budget and byte limits in env syntax (b/kb/mb/gb),
    # numeric limits with range check in the endpoint
    budget: str | None = Field(default=None, max_length=16)
    exec_jobs_shown: int | None = None
    cmd_zeichen: int | None = None
    result_output_bytes: str | None = Field(default=None, max_length=16)
    datei_bytes_roh: str | None = Field(default=None, max_length=16)
    job_store_eintraege: int | None = None
    datei_store_eintraege: int | None = None
    audit_rotation_bytes: str | None = Field(default=None, max_length=16)
    long_poll_s: int | None = None


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


@router.get("/admin/api/settings")
async def admin_settings(request: Request, credentials: HTTPBasicCredentials | None = Depends(security)):
    # settings page: all changeable values with source (datei|env|default) plus stack-fixed limits
    await auth.require_admin(request, credentials)
    budget_raw = os.environ.get("AGENT_SANDBOX_RELAY_BUDGET", config.DEFAULT_BUDGET)
    return {
        "budget": config.budget(),
        "budget_max": config.budget_max(),
        "budget_headroom": config.BUDGET_HEADROOM,
        "mem_limit": config.mem_limit(),
        "budget_roh": store.settings_raw("budget") if config.get_override("budget") is not None else budget_raw,
        "budget_quelle": "datei" if config.get_override("budget") is not None else ("env" if "AGENT_SANDBOX_RELAY_BUDGET" in os.environ else "default"),
        "data_bytes": store.data_bytes,
        "exec_jobs_shown": config.exec_jobs_shown(),
        "exec_quelle": "datei" if config.get_override("exec_jobs_shown") is not None else ("env" if "AGENT_SANDBOX_EXECJOBSSHOWN" in os.environ else "default"),
        "cmd_zeichen": config.cmd_zeichen(),
        "cmd_zeichen_quelle": "datei" if config.get_override("cmd_zeichen") is not None else "default",
        "result_output_bytes": config.result_output_bytes(),
        "result_output_bytes_roh": store.settings_raw("result_output_bytes") if config.get_override("result_output_bytes") is not None else "8m",
        "result_output_bytes_quelle": "datei" if config.get_override("result_output_bytes") is not None else "default",
        "job_store_eintraege": config.job_store_eintraege(),
        "job_store_eintraege_quelle": "datei" if config.get_override("job_store_eintraege") is not None else "default",
        "datei_store_eintraege": config.datei_store_eintraege(),
        "datei_store_eintraege_quelle": "datei" if config.get_override("datei_store_eintraege") is not None else "default",
        "audit_rotation_bytes": config.audit_rotation_bytes(),
        "audit_rotation_bytes_roh": store.settings_raw("audit_rotation_bytes") if config.get_override("audit_rotation_bytes") is not None else "2m",
        "audit_rotation_bytes_quelle": "datei" if config.get_override("audit_rotation_bytes") is not None else "default",
        "long_poll_s": config.poll_seconds(),
        "long_poll_quelle": "datei" if config.get_override("long_poll_s") is not None else ("env" if "AGENT_SANDBOX_RELAY_POLL" in os.environ else "default"),
        "datei_bytes_roh": config.datei_bytes_roh(),
        "datei_bytes_roh_roh": store.settings_raw("datei_bytes_roh") if config.get_override("datei_bytes_roh") is not None else "8m",
        "datei_bytes_roh_quelle": "datei" if config.get_override("datei_bytes_roh") is not None else "default",
        "fixe": {
            "admin_nutzername": config.ADMIN_USERNAME,
        },
        "hinweis_env": "Nur der Admin-Nutzername steht als Stack-Umgebung fest (Neustart nötig); alle anderen Werte sind hier änderbar.",
    }


@router.post("/admin/api/settings")
async def admin_settings_update(body: SettingsUpdate, request: Request, credentials: HTTPBasicCredentials | None = Depends(security)):
    await auth.require_admin(request, credentials)
    changed = {}
    cand_budget = None
    if body.budget is not None:
        raw = body.budget.strip()
        try:
            cand_budget = config.parse_size(raw)
        except ValueError:
            raise HTTPException(status_code=422, detail="Budget ungültig. Syntax wie AGENT_SANDBOX_RELAY_BUDGET: b, k|kb, m|mb, g|gb case-insensitiv, ohne Einheit Bytes (Beispiel: 384m)")
        changed["budget"] = raw
    cand_out = None
    if body.result_output_bytes is not None:
        raw = body.result_output_bytes.strip()
        try:
            cand_out = config.parse_size(raw)
        except ValueError:
            raise HTTPException(status_code=422, detail="result_output_bytes ungültig. Syntax wie das Budget: b, k|kb, m|mb, g|gb case-insensitiv, ohne Einheit Bytes (Beispiel: 8m)")
        changed["result_output_bytes"] = raw
    cand_file = None
    if body.datei_bytes_roh is not None:
        raw = body.datei_bytes_roh.strip()
        try:
            cand_file = config.parse_size(raw)
        except ValueError:
            raise HTTPException(status_code=422, detail="datei_bytes_roh ungültig. Syntax wie das Budget: b, k|kb, m|mb, g|gb case-insensitiv, ohne Einheit Bytes (Beispiel: 8m)")
        changed["datei_bytes_roh"] = raw
    # cross-check budget and output cap: budget >= cap, cap >= 64k, cap <= budget
    eff_out = cand_out if cand_out is not None else config.result_output_bytes()
    eff_budget = cand_budget if cand_budget is not None else config.budget()
    if cand_budget is not None:
        if cand_budget < eff_out:
            raise HTTPException(status_code=422, detail="Budget (%d Bytes) unter dem Output-Deckel (%d Bytes) macht die Queue unbrauchbar" % (cand_budget, eff_out))
        bmax = config.budget_max()
        if bmax is not None and cand_budget > bmax:
            raise HTTPException(status_code=422, detail="Budget (%d Bytes) über dem Maximum (%d Bytes): Container-Speichergrenze (%s) minus 64 MiB Headroom - knappere Werte verhindern den Kernel-OOM-Kill des Containers (Exit 137)" % (cand_budget, bmax, "unbegrenzt" if config.mem_limit() is None else "%d Bytes" % config.mem_limit()))
        if cand_budget < store.data_bytes:
            raise HTTPException(status_code=409, detail="Budget (%d Bytes) liegt unter dem aktuell belegten Speicher (%d Bytes) - erst Jobs oder Dateien löschen" % (cand_budget, store.data_bytes))
    if cand_out is not None:
        if cand_out < 65536:
            raise HTTPException(status_code=422, detail="result_output_bytes unter 64k (%d Bytes) - Resultate brauchen Raum für Meldungen" % cand_out)
        if cand_out > eff_budget:
            raise HTTPException(status_code=422, detail="result_output_bytes (%d Bytes) über dem Budget (%d Bytes) - jedes Result muss ins Budget passen" % (cand_out, eff_budget))
    if cand_file is not None:
        if cand_file < 65536:
            raise HTTPException(status_code=422, detail="datei_bytes_roh unter 64k (%d Bytes) - auch kleine Dateien brauchen Spielraum für Meldungen" % cand_file)
        if cand_file > eff_budget:
            raise HTTPException(status_code=422, detail="datei_bytes_roh (%d Bytes) über dem Budget (%d Bytes) - jede Datei muss ins Budget passen" % (cand_file, eff_budget))
    if body.exec_jobs_shown is not None:
        if not 1 <= body.exec_jobs_shown <= 500:
            raise HTTPException(status_code=422, detail="erledigte Jobs je Submitter: 1 bis 500")
        changed["exec_jobs_shown"] = body.exec_jobs_shown
    if body.cmd_zeichen is not None:
        if not 1 <= body.cmd_zeichen <= config.CMD_ZEICHEN_HARD:
            raise HTTPException(status_code=422, detail="cmd-Zeichenlimit: 1 bis %d" % config.CMD_ZEICHEN_HARD)
        changed["cmd_zeichen"] = body.cmd_zeichen
    if body.job_store_eintraege is not None:
        if not 1 <= body.job_store_eintraege <= 5000:
            raise HTTPException(status_code=422, detail="Job-Store-Einträge: 1 bis 5000")
        changed["job_store_eintraege"] = body.job_store_eintraege
    if body.datei_store_eintraege is not None:
        if not 1 <= body.datei_store_eintraege <= 5000:
            raise HTTPException(status_code=422, detail="Datei-Store-Einträge: 1 bis 5000")
        changed["datei_store_eintraege"] = body.datei_store_eintraege
    if body.audit_rotation_bytes is not None:
        raw = body.audit_rotation_bytes.strip()
        try:
            v = config.parse_size(raw)
        except ValueError:
            raise HTTPException(status_code=422, detail="audit_rotation_bytes ungültig. Syntax wie das Budget: b, k|kb, m|mb, g|gb case-insensitiv, ohne Einheit Bytes (Beispiel: 2m)")
        if not 65536 <= v <= 64 * 1024 * 1024:
            raise HTTPException(status_code=422, detail="Audit-Rotation: 64k bis 64m (%d Bytes ist ausserhalb)" % v)
        changed["audit_rotation_bytes"] = raw
    if body.long_poll_s is not None:
        if not 0 <= body.long_poll_s <= 3600:
            raise HTTPException(status_code=422, detail="Long-Poll: 0 bis 3600 Sekunden")
        changed["long_poll_s"] = body.long_poll_s
    if not changed:
        raise HTTPException(status_code=422, detail="kein Feld gesetzt (budget, exec_jobs_shown, cmd_zeichen, result_output_bytes, datei_bytes_roh, job_store_eintraege, datei_store_eintraege, audit_rotation_bytes, long_poll_s)")
    values = store.settings_all()
    values.update(changed)
    if "budget" in values:
        config.set_override("budget", config.parse_size(values["budget"]))
    if "exec_jobs_shown" in values:
        config.set_override("exec_jobs_shown", int(values["exec_jobs_shown"]))
    if "cmd_zeichen" in values:
        config.set_override("cmd_zeichen", int(values["cmd_zeichen"]))
    if "result_output_bytes" in values:
        config.set_override("result_output_bytes", config.parse_size(values["result_output_bytes"]))
    if "datei_bytes_roh" in values:
        config.set_override("datei_bytes_roh", config.parse_size(values["datei_bytes_roh"]))
    if "job_store_eintraege" in values:
        config.set_override("job_store_eintraege", int(values["job_store_eintraege"]))
    if "datei_store_eintraege" in values:
        config.set_override("datei_store_eintraege", int(values["datei_store_eintraege"]))
    if "audit_rotation_bytes" in values:
        config.set_override("audit_rotation_bytes", config.parse_size(values["audit_rotation_bytes"]))
    if "long_poll_s" in values:
        config.set_override("long_poll_s", int(values["long_poll_s"]))
    store.save_settings(values)
    store.audit("settings_change", **changed)
    return {"ok": True, "budget": config.budget(), "exec_jobs_shown": config.exec_jobs_shown(),
            "budget_roh": values.get("budget")}


@router.get("/admin/api/usage")
async def admin_usage(request: Request, credentials: HTTPBasicCredentials | None = Depends(security)):
    # API docs for the admin UI: same structure as GET /usage, but with the HTTP basic session instead of a bearer token
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
    plain = secrets.token_urlsafe(32)  # 32 random bytes are considered sufficient (Python secrets docs)
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
        raise HTTPException(status_code=400, detail="Submitter braucht eine gültige worker_id")
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
    # rename a worker or submitter (display in admin UI and audit)
    await auth.require_admin(request, credentials)
    rec = next((x for x in store.workers if x["id"] == tid), None)
    role = "worker"
    if rec is None:
        rec = next((x for x in store.submitters if x["id"] == tid), None)
        role = "submitter"
    if rec is None:
        raise HTTPException(status_code=404, detail="Token nicht gefunden")
    old_name = rec["name"]
    rec["name"] = body.name
    # jobs in the queue keep the old display name (historical id); new jobs carry the new name
    store.save_state()
    store.audit("token_rename", rolle=role, id=tid, von=old_name, nach=body.name)
    return {"ok": True, "rolle": role, "name": body.name}


@router.post("/admin/api/tokens/{tid}/assign")
async def admin_assign(tid: str, body: TokenAssign, request: Request, credentials: HTTPBasicCredentials | None = Depends(security)):
    await auth.require_admin(request, credentials)
    s = next((s for s in store.submitters if s["id"] == tid), None)
    if s is None:
        raise HTTPException(status_code=404, detail="Submitter nicht gefunden")
    w = next((w for w in store.workers if w["id"] == body.worker_id), None)
    if w is None:
        raise HTTPException(status_code=400, detail="worker_id unbekannt")
    old_name = next((x["name"] for x in store.workers if x["id"] == s["worker_id"]), "?")
    s["worker_id"] = w["id"]
    store.save_state()
    store.audit("token_assign", submitter=s["name"], von=old_name, nach=w["name"])
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
    if len(cmd) > 80:  # never the full cmd in the listing (execution plans do not belong in the overview)
        cmd = cmd[:80] + "..."
    return {"id": j["id"], "status": j["status"], "worker": j.get("worker"), "submitter": j.get("submitter"),
            "rc": j.get("rc"), "out_bytes": len(j["output"]) if isinstance(j.get("output"), str) else 0,
            "ts": j["ts"], "cmd": cmd}


DETAIL_OUTMAX = 16384  # output truncated in the job dialog (a full result would be up to 8 MiB in the UI)


@router.get("/admin/api/jobs/{jid}")
async def admin_job_detail(jid: str, request: Request, credentials: HTTPBasicCredentials | None = Depends(security)):
    # full cmd for the jobs page dialog (the listing truncates at 80 characters)
    await auth.require_admin(request, credentials)
    j = store.jobs.get(jid)
    if j is None:
        raise HTTPException(status_code=404, detail="Job nicht gefunden")
    out = j.get("output")
    out_len = len(out) if isinstance(out, str) else 0
    if isinstance(out, str) and out_len > DETAIL_OUTMAX:
        out = out[:DETAIL_OUTMAX] + "\n[agent-sandbox-relay: Output gekürzt, voll %d Bytes]" % out_len
    return {"id": j["id"], "status": j["status"], "worker": j.get("worker"), "submitter": j.get("submitter"),
            "rc": j.get("rc"), "out_bytes": out_len, "ts": j["ts"], "cmd": j.get("cmd") or "",
            "output": out if isinstance(out, str) else None, "output_gekuerzt": out_len > DETAIL_OUTMAX}


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
        if sid not in subs:  # do not hide jobs of a deleted submitter
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
        done = sorted(done_by[g["id"]], key=lambda v: v["ts"], reverse=True)[:shown]  # newest first
        open_jobs = sorted(open_by[g["id"]], key=lambda v: v["ts"])  # oldest first
        out.append({"id": g["id"], "name": g["name"], "offen": open_jobs, "erledigt": done})
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
    store.evs.setdefault(j["worker_id"], asyncio.Event()).set()  # wake waiting workers immediately
    store.audit("job_resume", jid=jid)
    return {"ok": True}


@router.delete("/admin/api/jobs/{jid}")
async def admin_delete_job(jid: str, request: Request, credentials: HTTPBasicCredentials | None = Depends(security)):
    await auth.require_admin(request, credentials)
    j = store.jobs.pop(jid, None)
    if j is None:
        raise HTTPException(status_code=404, detail="Job nicht gefunden")
    if isinstance(j.get("output"), str):  # return allocated budget
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
    for ln in reversed(lines):  # newest first
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return {"events": out}
