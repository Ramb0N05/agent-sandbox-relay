# relay/store.py - state: jobs, files, tokens, budget, audit, persistence (11th round)
import asyncio
import gzip
import base64
import hashlib
import json
import os
import sys
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone

from . import config

jobs: "OrderedDict[str, dict]" = OrderedDict()
files: "OrderedDict[str, dict]" = OrderedDict()
workers: list = []
submitters: list = []
worker_by_hash: dict = {}
submit_by_hash: dict = {}
data_bytes = 0  # belegtes Budget: Output- plus Datei-Encoding-Bytes
evs: dict = {}  # worker_id -> asyncio.Event (Aufwecken bei neuem Job)
job_done: dict = {}  # job-id -> asyncio.Event (Aufwecken bei Result)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def audit(event, **fields):
    # audit log: JSON lines in DATA_DIR/audit.jsonl; never log cmd contents or token values
    try:
        d = config.data_dir()
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "audit.jsonl")
        if os.path.exists(p) and os.stat(p).st_size > config.audit_rotation_bytes():
            os.replace(p, p + ".1")  # Rotation: ein Backup behalten
        line = json.dumps({"ts": _now(), "event": event, **fields}, ensure_ascii=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        print("audit-fehler:", e, file=sys.stderr)  # Audit darf den Betrieb nie abbrechen


def save_state():
    d = config.data_dir()
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, "tokens.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"workers": workers, "submitters": submitters}, f)
    os.replace(tmp, os.path.join(d, "tokens.json"))  # atomar: erst schreiben, dann umbenennen


def _reindex():
    global worker_by_hash, submit_by_hash
    worker_by_hash = {w["hash"]: w for w in workers}
    submit_by_hash = {s["hash"]: s for s in submitters}


def settings_all():
    # raw values from settings.json (no override logic, for display and merge)
    p = os.path.join(config.data_dir(), "settings.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}
    return {}


def settings_raw(key):
    return settings_all().get(key)


def save_settings(values):
    # settings page (16th round): overrides persisted in DATA_DIR/settings.json,
    # raw values (budget in env syntax) for traceability; atomic like tokens.json
    d = config.data_dir()
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, "settings.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(values, f, ensure_ascii=True)
    os.replace(tmp, os.path.join(d, "settings.json"))


def load_settings():
    # at startup: settings.json transfers values into config overrides (fail-closed like tokens.json)
    p = os.path.join(config.data_dir(), "settings.json")
    if not os.path.exists(p):
        return
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError) as e:
        raise SystemExit("Einstellungs-Speicher unlesbar: %s (%s)" % (p, e))
    if not isinstance(d, dict):
        raise SystemExit("Einstellungs-Speicher unlesbar: %s (kein JSON-Objekt)" % p)
    if "budget" in d:
        try:
            v = config.parse_size(d["budget"])
        except ValueError as e:
            raise SystemExit("Einstellungs-Speicher: budget ungültig: %r (%s). Syntax wie AGENT_SANDBOX_RELAY_BUDGET: b, k|kb, m|mb, g|gb case-insensitiv, ohne Einheit Bytes" % (d["budget"], e))
        config.set_override("budget", v)
    if "exec_jobs_shown" in d:
        try:
            n = int(d["exec_jobs_shown"])
        except (TypeError, ValueError):
            raise SystemExit("Einstellungs-Speicher: exec_jobs_shown keine Zahl: %r" % (d["exec_jobs_shown"],))
        if not 1 <= n <= 500:
            raise SystemExit("Einstellungs-Speicher: exec_jobs_shown ausserhalb 1..500: %r" % (d["exec_jobs_shown"],))
        config.set_override("exec_jobs_shown", n)
    if "cmd_zeichen" in d:
        try:
            n = int(d["cmd_zeichen"])
        except (TypeError, ValueError):
            raise SystemExit("Einstellungs-Speicher: cmd_zeichen keine Zahl: %r" % (d["cmd_zeichen"],))
        if not 1 <= n <= config.CMD_ZEICHEN_HARD:
            raise SystemExit("Einstellungs-Speicher: cmd_zeichen ausserhalb 1..%d: %r" % (config.CMD_ZEICHEN_HARD, d["cmd_zeichen"]))
        config.set_override("cmd_zeichen", n)
    if "result_output_bytes" in d:
        try:
            v = config.parse_size(d["result_output_bytes"])
        except ValueError as e:
            raise SystemExit("Einstellungs-Speicher: result_output_bytes ungültig: %r (%s). Syntax wie AGENT_SANDBOX_RELAY_BUDGET: b, k|kb, m|mb, g|gb case-insensitiv, ohne Einheit Bytes" % (d["result_output_bytes"], e))
        if v < 65536:
            raise SystemExit("Einstellungs-Speicher: result_output_bytes unter 64k: %r" % (d["result_output_bytes"],))
        if v > config.budget():
            raise SystemExit("Einstellungs-Speicher: result_output_bytes über dem Budget (%d Bytes): %d" % (config.budget(), v))
        config.set_override("result_output_bytes", v)
    if "job_store_eintraege" in d:
        try:
            n = int(d["job_store_eintraege"])
        except (TypeError, ValueError):
            raise SystemExit("Einstellungs-Speicher: job_store_eintraege keine Zahl: %r" % (d["job_store_eintraege"],))
        if not 1 <= n <= 5000:
            raise SystemExit("Einstellungs-Speicher: job_store_eintraege ausserhalb 1..5000: %r" % (d["job_store_eintraege"],))
        config.set_override("job_store_eintraege", n)
    if "datei_store_eintraege" in d:
        try:
            n = int(d["datei_store_eintraege"])
        except (TypeError, ValueError):
            raise SystemExit("Einstellungs-Speicher: datei_store_eintraege keine Zahl: %r" % (d["datei_store_eintraege"],))
        if not 1 <= n <= 5000:
            raise SystemExit("Einstellungs-Speicher: datei_store_eintraege ausserhalb 1..5000: %r" % (d["datei_store_eintraege"],))
        config.set_override("datei_store_eintraege", n)
    if "datei_bytes_roh" in d:
        try:
            v = config.parse_size(d["datei_bytes_roh"])
        except ValueError as e:
            raise SystemExit("Einstellungs-Speicher: datei_bytes_roh ungültig: %r (%s). Syntax wie AGENT_SANDBOX_RELAY_BUDGET: b, k|kb, m|mb, g|gb case-insensitiv, ohne Einheit Bytes" % (d["datei_bytes_roh"], e))
        if v < 65536:
            raise SystemExit("Einstellungs-Speicher: datei_bytes_roh unter 64k: %r" % (d["datei_bytes_roh"],))
        if v > config.budget():
            raise SystemExit("Einstellungs-Speicher: datei_bytes_roh über dem Budget: %r (%d Bytes > %d Bytes)" % (d["datei_bytes_roh"], v, config.budget()))
        config.set_override("datei_bytes_roh", v)
    if "audit_rotation_bytes" in d:
        try:
            v = config.parse_size(d["audit_rotation_bytes"])
        except ValueError as e:
            raise SystemExit("Einstellungs-Speicher: audit_rotation_bytes ungültig: %r (%s). Syntax wie AGENT_SANDBOX_RELAY_BUDGET: b, k|kb, m|mb, g|gb case-insensitiv, ohne Einheit Bytes" % (d["audit_rotation_bytes"], e))
        if not 65536 <= v <= 64 * 1024 * 1024:
            raise SystemExit("Einstellungs-Speicher: audit_rotation_bytes ausserhalb 64k..64m: %r" % (d["audit_rotation_bytes"],))
        config.set_override("audit_rotation_bytes", v)
    if "long_poll_s" in d:
        try:
            n = int(d["long_poll_s"])
        except (TypeError, ValueError):
            raise SystemExit("Einstellungs-Speicher: long_poll_s keine Zahl: %r" % (d["long_poll_s"],))
        if not 0 <= n <= 3600:
            raise SystemExit("Einstellungs-Speicher: long_poll_s ausserhalb 0..3600: %r" % (d["long_poll_s"],))
        config.set_override("long_poll_s", n)


def load_state():
    global workers, submitters
    p = os.path.join(config.data_dir(), "tokens.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            workers = d.get("workers", [])
            submitters = d.get("submitters", [])
        except (OSError, ValueError):
            raise SystemExit("Token-Speicher unlesbar: " + p)
    if not workers and not submitters and config.TOKEN:
        # migration: the legacy env token is registered as worker and submit token;
        # the running VM and the sender keep working, both entries can be deleted via the
        # admin UI once real tokens exist
        h = hashlib.sha256(config.TOKEN.encode()).hexdigest()
        w = {"id": "w-" + uuid.uuid4().hex[:10], "name": "legacy", "hash": h, "legacy": True, "created": _now()}
        s = {"id": "s-" + uuid.uuid4().hex[:10], "name": "legacy", "hash": h, "worker_id": w["id"], "legacy": True, "created": _now()}
        workers = [w]
        submitters = [s]
        save_state()
        audit("migrate", detail="Legacy-Token als Worker- und Submit-Token registriert")
    if not workers and not submitters:
        raise SystemExit("weder Token-Speicher noch AGENT_SANDBOX_RELAY_TOKEN vorhanden")  # fail-closed
    _reindex()


def init():
    # write probe before anything else: a root-owned volume would otherwise only crash at
    # the first save_state with a traceback (PermissionError tokens.json.tmp)
    d = config.data_dir()
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".write-probe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
    except OSError as e:
        raise SystemExit(
            "DATA_DIR nicht schreibbar: %s (%s). Fix: Volume in Portainer löschen (Inhalt wird "
            "aus der Env neu migriert) oder einmalig chownen: docker run --rm -v <volume>:/data "
            "alpine chown -R 65534:65534 /data; danach Container neu starten." % (d, e))
    load_settings()
    load_state()
    audit("boot", budget=config.budget(), outmax=config.result_output_bytes(), filemax=config.datei_bytes_roh(),
          workers=len(workers), submitters=len(submitters))


def reset_for_tests():
    # stub tests only: reset everything
    global jobs, files, workers, submitters, worker_by_hash, submit_by_hash, data_bytes, evs, job_done
    jobs = OrderedDict()
    files = OrderedDict()
    workers = []
    submitters = []
    worker_by_hash = {}
    submit_by_hash = {}
    data_bytes = 0
    evs = {}
    job_done = {}


def _evict_for(need):
    # OOM protection: free budget - finished jobs first (oldest first), then oldest files
    global data_bytes
    budget = config.budget()
    evicted = []
    while data_bytes + need > budget:
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


def decode_file(rec):
    # convert the stored gzip+base64 form back to raw bytes
    return gzip.decompress(base64.b64decode(rec["enc"]))
