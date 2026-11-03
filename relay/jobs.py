# relay/jobs.py - Job-Endpunkte (elfte Runde: angehaltene Jobs werden nicht gezogen)
import asyncio
import time
import uuid

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from . import auth, config, store

router = APIRouter()


class Job(BaseModel):
    cmd: str = Field(max_length=10000)  # Längenlimit; Verstoss -> 422


@router.post("/jobs")
async def submit(job: Job, request: Request, authorization: str | None = Header(None)):
    sub = auth.auth_bearer(request, authorization, "submitter")
    w = next((w for w in store.workers if w["id"] == sub["worker_id"]), None)
    if w is None:
        raise HTTPException(status_code=409, detail="zugeordneter Worker existiert nicht mehr")
    jid = uuid.uuid4().hex[:12]
    store.jobs[jid] = {"id": jid, "cmd": job.cmd, "status": "queued", "output": None, "rc": None,
                       "ts": time.time(), "worker_id": w["id"], "worker": w["name"],
                       "submitter_id": sub["id"], "submitter": sub["name"]}
    if len(store.jobs) > config.JOBSTOREMAX:
        _, old = store.jobs.popitem(last=False)  # Begrenzung: älteste Jobs zuerst raus
        if old.get("output") is not None:
            store.data_bytes -= len(old["output"])
        store.job_done.pop(old["id"], None)
        store.audit("evict", ursache="job_store_voll", entfernt=[{"kind": "job", "id": old["id"]}])
    store.evs.setdefault(w["id"], asyncio.Event()).set()
    store.audit("submit", jid=jid, submitter=sub["name"], worker=w["name"], cmd_zeichen=len(job.cmd))
    return {"id": jid}


def _take_queued(worker_id):
    # nur status=="queued": angehaltene Jobs (paused) überspringt der Worker
    for j in store.jobs.values():
        if j["status"] == "queued" and j["worker_id"] == worker_id:
            j["status"] = "running"
            return j
    return None


@router.get("/jobs/next")
async def next_job(request: Request, authorization: str | None = Header(None)):
    w = auth.auth_bearer(request, authorization, "worker")
    ev = store.evs.setdefault(w["id"], asyncio.Event())
    ev.clear()  # erst zurücksetzen, dann ziehen: kein verlorenes Aufwecken
    j = _take_queued(w["id"])
    if j:
        store.audit("deliver", jid=j["id"], worker=w["name"])
        return {"id": j["id"], "cmd": j["cmd"]}
    try:
        await asyncio.wait_for(ev.wait(), timeout=config.poll_seconds())
    except asyncio.TimeoutError:
        return Response(status_code=204)
    if await request.is_disconnected():  # verwaister Long-Poll: toter Client zieht keinen Job aus der Queue
        return Response(status_code=204)
    j = _take_queued(w["id"])
    if j:
        store.audit("deliver", jid=j["id"], worker=w["name"])
        return {"id": j["id"], "cmd": j["cmd"]}
    return Response(status_code=204)


@router.post("/jobs/{jid}/result")
async def post_result(jid: str, body: dict, request: Request, authorization: str | None = Header(None)):
    w = auth.auth_bearer(request, authorization, "worker")
    if jid not in store.jobs:
        raise HTTPException(status_code=404)
    if store.jobs[jid]["worker_id"] != w["id"]:
        store.audit("auth_fail", path="result", detail="falscher Worker", jid=jid, worker=w["name"])
        raise HTTPException(status_code=403)
    out = body.get("output")
    if isinstance(out, str):
        if len(out) > config.OUTMAX:
            out = out[:config.OUTMAX]  # Output-Deckel auch serverseitig
        store._evict_for(len(out))
        budget = config.budget()
        if store.data_bytes + len(out) > budget:  # Einzelresult größer als Restbudget: kürzen statt OOM
            marker = "\n[agent-sandbox-relay: Output gekürzt, Speicherbudget erreicht]"
            allowed = max(0, budget - store.data_bytes - len(marker))
            out = out[:allowed] + marker
            store.audit("trim", jid=jid, bytes_gekuerzt=len(out))
        store.data_bytes += len(out)
    store.jobs[jid].update(status="done", output=out, rc=body.get("rc"))
    store.job_done.setdefault(jid, asyncio.Event()).set()
    store.audit("result", jid=jid, worker=w["name"], rc=body.get("rc"), out_bytes=len(out) if isinstance(out, str) else 0)
    return {"ok": True}


def _own_job(request, jid, authorization):
    sub = auth.auth_bearer(request, authorization, "submitter")
    if jid not in store.jobs:
        raise HTTPException(status_code=404)
    if store.jobs[jid]["submitter_id"] != sub["id"]:
        store.audit("auth_fail", path="job_zugriff", detail="fremder Job", jid=jid, submitter=sub["name"])
        raise HTTPException(status_code=403)
    return sub, store.jobs[jid]


@router.get("/jobs/{jid}")
async def get_job(jid: str, request: Request, authorization: str | None = Header(None)):
    _, j = _own_job(request, jid, authorization)
    return j


@router.get("/jobs/{jid}/wait")
async def wait_job(jid: str, request: Request, timeout: int = 25, authorization: str | None = Header(None)):
    # Status-Long-Poll: wartet bis das Result vorliegt oder der Timeout abläuft - ersetzt das Pollen des Absenders
    _own_job(request, jid, authorization)
    timeout = max(1, min(int(timeout), 300))
    deadline = time.monotonic() + timeout
    ev = store.job_done.setdefault(jid, asyncio.Event())
    while True:
        ev.clear()  # erst zurücksetzen, dann Zustand prüfen: kein verlorenes Aufwecken
        j = store.jobs.get(jid)
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
            j = store.jobs.get(jid)
            if j is None:
                raise HTTPException(status_code=404)
            return j


def usage_data():
    # Struktur für GET /usage und die API-Doku-Seite der Verwaltung (GET /admin/api/usage, HTTP-Basic-Session)
    return {
        "service": "agent-sandbox-relay",
        "auth": "Authorization: Bearer <token>; Rollen: Submitter (Jobs einspeisen/abfragen) und Worker (Jobs ziehen/Resultate liefern); alle Endpunkte ausser GET /healthz",
        "rollen": {
            "submit": "Submit-Token: POST /jobs, GET /jobs/<id>, GET /jobs/<id>/wait, Datei-Endpunkte",
            "worker": "Worker-Token: GET /jobs/next, POST /jobs/<id>/result, Datei-Endpunkte",
            "admin": "Verwaltung (Tokens erstellen/benennen/zuordnen/löschen, Jobs anhalten/fortsetzen/löschen, Audit): GET /admin, Login mit AGENT_SANDBOX_ADMIN_USERNAME und AGENT_SANDBOX_ADMIN_PASSWORD",
        },
        "grenzen": {"cmd_zeichen": 10000, "result_output_bytes": config.OUTMAX,
                    "datei_bytes_roh": config.FILEMAX, "job_store": config.JOBSTOREMAX,
                    "datei_store": config.FILESTOREMAX,
                    "speicherbudget_output_dateien": config.budget(),
                    "budget_syntax": "b, k|kb, m|mb, g|gb case-insensitiv, ohne Einheit Bytes (wie mem_limit im Compose-File)",
                    "long_poll_s": config.poll_seconds(), "wait_s_max": 300},
        "endpunkte": {
            "POST /jobs": {"body": {"cmd": "..."}, "antwort": {"id": "..."}, "aufrufer": "Submit-Token"},
            "GET /jobs/next": {"antwort": "{'id': '...', 'cmd': '...'} oder 204 (Long-Poll, nur Jobs des eigenen Workers; angehaltene Jobs werden nicht ausgeliefert)", "aufrufer": "Worker-Token"},
            "POST /jobs/<id>/result": {"body": {"output": "...", "rc": 0}, "aufrufer": "Worker-Token (nur eigene Jobs)"},
            "GET /jobs/<id>": {"antwort": "Status/Output", "aufrufer": "Submit-Token (nur eigene Jobs)"},
            "GET /jobs/<id>/wait": {"query": "timeout=Sekunden (Default 25, max 300)", "antwort": "Job-Zustand, wartet bis done oder Timeout (kein Pollen nötig)", "aufrufer": "Submit-Token (nur eigene Jobs)"},
            "POST /files": {"body": "Rohbytes der Datei; Server speichert gzip+base64-kodiert", "antwort": {"id": "...", "size": 0, "size_enc": 0}, "aufrufer": "beide Rollen"},
            "GET /files/<id>": {"antwort": "Rohbytes (application/octet-stream)", "aufrufer": "beide Rollen"},
            "GET /usage": {"antwort": "diese Dokumentation", "aufrufer": "beide Rollen"},
            "GET /healthz": {"antwort": {"ok": True}, "aufrufer": "Healthcheck, ohne Token"},
            "GET /admin": {"antwort": "Verwaltungsoberfläche (Login)", "aufrufer": "Verwaltung"},
        },
        "verwaltung_api": {
            "GET /admin/api/tokens": "Worker und Submitter mit Namen",
            "POST /admin/api/tokens": "Token anlegen; name optional (leer -> automatisch benannt), submitter braucht worker_id",
            "PATCH /admin/api/tokens/<id>": "Umbenennen {name: 1-64 Zeichen}",
            "POST /admin/api/tokens/<id>/assign": "Submitter an anderen Worker hängen {worker_id}",
            "DELETE /admin/api/tokens/<id>": "Token löschen (409, solange Abhängigkeiten offen)",
            "GET /admin/api/jobs": "je Submitter offene Jobs und die zuletzt erledigten (Default 64, AGENT_SANDBOX_EXECJOBSSHOWN), cmd nur gekürzt",
            "GET /admin/api/jobs/<id>": "Job-Detail mit vollem cmd (Dialog der Jobsseite), Output bis 16 KiB gekürzt",
            "POST /admin/api/jobs/<id>/pause": "wartender Job -> angehalten (Worker zieht ihn nicht)",
            "POST /admin/api/jobs/<id>/resume": "angehaltener Job -> zurück in die Queue, Worker wird geweckt",
            "DELETE /admin/api/jobs/<id>": "Job löschen (jeder Status; Budget wird bei erledigten Jobs zurückgegeben)",
            "GET /admin/api/audit": "Protokoll (neueste zuerst)",
            "GET /admin/api/state": "Budget/Jobs/Dateien/Zähler",
            "GET /admin/api/settings": "zentrale Einstellungen: Budget und Anzeige-Limit änderbar, weitere Grenzen nur per Stack-Umgebung",
            "POST /admin/api/settings": "Einstellungen ändern {budget: Env-Syntax, exec_jobs_shown: 1-500}; wirksam sofort und in settings.json dauerhaft",
            "GET /admin/api/usage": "diese Dokumentation mit der Admin-Session (HTTP Basic) statt Bearer-Token",
        },
        "dateitransfer": {
            "datei_in_die_vm": "1) POST /files mit Rohbytes -> id; 2) Job cmd 'VIBE_FETCH <id> <zielpfad>'",
            "datei_aus_der_vm": "Job cmd 'VIBE_SEND <pfad>' -> Result enthält 'VIBE_SEND id=<id>'; dann GET /files/<id>",
            "fallback": "Job cmd 'base64 -w0 <datei>' (Base64-Text im Result, ohne Datei-Endpunkte)",
        },
        "beispiele": [
            "curl -X POST https://<agent-sandbox-relay-domain>/jobs -H 'Authorization: Bearer <token>' -H 'Content-Type: application/json' -d '{\"cmd\":\"uname -a\"}'",
            "curl 'https://<agent-sandbox-relay-domain>/jobs/<id>/wait?timeout=25' -H 'Authorization: Bearer <token>'",
            "curl -X POST https://<agent-sandbox-relay-domain>/files -H 'Authorization: Bearer <token>' --data-binary @datei.bin",
        ],
    }


@router.get("/usage")
async def usage(request: Request, authorization: str | None = Header(None)):
    auth.auth_any(request, authorization)
    return usage_data()
