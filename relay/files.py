# relay/files.py - file endpoints (raw bytes in, gzip+base64 stored, raw bytes out)
import base64
import gzip
import time
import uuid

from fastapi import APIRouter, Header, HTTPException, Request, Response

from . import auth, config, store

router = APIRouter()


@router.post("/files")
async def upload_file(request: Request, authorization: str | None = Header(None)):
    rec, role = auth.auth_any(request, authorization)
    try:
        declared = int(request.headers.get("content-length", ""))
    except ValueError:
        declared = -1
    if declared > config.datei_bytes_roh():  # reject early, before the body is read
        raise HTTPException(status_code=413)
    raw = await request.body()  # Roh-Body ohne Validierung (FastAPI-Request-Objekt)
    if len(raw) > config.datei_bytes_roh():
        raise HTTPException(status_code=413)
    if not raw:
        raise HTTPException(status_code=422)
    enc = base64.b64encode(gzip.compress(raw)).decode("ascii")  # Speicherform: gzip+base64
    store._evict_for(len(enc))
    if store.data_bytes + len(enc) > config.budget():
        raise HTTPException(status_code=413, detail="Speicherbudget erreicht")
    store.data_bytes += len(enc)
    fid = "f" + uuid.uuid4().hex[:12]
    store.files[fid] = {"id": fid, "enc": enc, "size": len(raw), "size_enc": len(enc), "ts": time.time(),
                       "submitter_id": rec["id"] if role == "submitter" else None,
                       "submitter": rec["name"] if role == "submitter" else None,
                       "delivered": 0, "path": None}
    if len(store.files) > config.datei_store_eintraege():
        _, old = store.files.popitem(last=False)  # limit: oldest files dropped first
        store.data_bytes -= len(old["enc"])
        store.audit("evict", ursache="datei_store_voll", entfernt=[{"kind": "file", "id": old["id"]}])
    store.audit("file_up", fid=fid, size=len(raw), rolle=role, name=rec["name"])
    return {"id": fid, "size": len(raw), "size_enc": len(enc)}


@router.get("/files/{fid}")
async def get_file(fid: str, request: Request, authorization: str | None = Header(None)):
    rec, role = auth.auth_any(request, authorization)
    if fid not in store.files:
        raise HTTPException(status_code=404)
    raw = store.decode_file(store.files[fid])  # convert back
    store.audit("file_down", fid=fid, size=len(raw), rolle=role, name=rec["name"])
    return Response(content=raw, media_type="application/octet-stream")
