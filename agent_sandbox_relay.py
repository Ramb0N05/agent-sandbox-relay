# agent_sandbox_relay.py - Entry (elfte Runde): App-Assembly und Start; Logik im Paket relay/
# Quelle: https://github.com/Ramb0N05/agent-sandbox-relay (Branch main)
# Ausführung: uvicorn agent_sandbox_relay:app (siehe Dockerfile); Docker-Service heisst agent-sandbox-relay
# Module: relay/config.py (Env, Budget-Syntax), relay/store.py (Zustand, Persistenz, Audit, OOM-Eviction),
# relay/auth.py (Rollen-Tokens, Admin-Login mit Lockout), relay/jobs.py (Job-Endpunkte inkl. /usage),
# relay/files.py (Datei-Endpunkte), relay/admin.py (Verwaltung) mit relay/templates/admin.html (Admin-UI).
# keine öffentliche API-Doku: /docs, /redoc, /openapi.json antworten 404 (Nutzerentscheidung 03.11.2026)
from fastapi import FastAPI

from relay import store
from relay.admin import router as admin_router
from relay.files import router as files_router
from relay.jobs import router as jobs_router

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


app.include_router(jobs_router)
app.include_router(files_router)
app.include_router(admin_router)

store.init()
