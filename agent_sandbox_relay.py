# agent_sandbox_relay.py - entry (11th round): app assembly and startup; logic in package relay/
# Quelle: https://github.com/Ramb0N05/agent-sandbox-relay (Branch main)
# run: uvicorn agent_sandbox_relay:app (see Dockerfile); the docker service is called agent-sandbox-relay
# modules: relay/config.py (env, budget syntax), relay/store.py (state, persistence, audit, OOM eviction),
# relay/auth.py (role tokens, admin login with lockout), relay/jobs.py (job endpoints incl. /usage),
# relay/files.py (file endpoints), relay/admin.py (management) with relay/templates/admin.html (admin UI).
# no public API docs: /docs, /redoc, /openapi.json return 404 (user decision 2026-11-03)
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
