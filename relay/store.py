# relay/store.py - Zustand: Jobs, Dateien, Tokens, Budget, Audit, Persistenz (elfte Runde)
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
    # Audit-Log: JSON-Zeilen in DATA_DIR/audit.jsonl; niemals cmd-Inhalte oder Token-Werte loggen
    try:
        d = config.data_dir()
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "audit.jsonl")
        if os.path.exists(p) and os.stat(p).st_size > config.AUDITMAX:
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
    # aktuelle Roh-Werte aus settings.json (ohne Overrides-Logik, fuer Anzeige und Merge)
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
    # Einstellungsseite (sechzehnte Runde): Overrides dauerhaft in DATA_DIR/settings.json,
    # Roh-Werte (Budget in Env-Syntax) fuer Nachvollziehbarkeit; atomar wie tokens.json
    d = config.data_dir()
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, "settings.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(values, f, ensure_ascii=True)
    os.replace(tmp, os.path.join(d, "settings.json"))


def load_settings():
    # beim Start: settings.json uebertraegt Werte in die config-Overrides (fail-closed wie tokens.json)
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
            raise SystemExit("Einstellungs-Speicher: budget ungueltig: %r (%s). Syntax wie AGENT_SANDBOX_RELAY_BUDGET: b, k|kb, m|mb, g|gb case-insensitiv, ohne Einheit Bytes" % (d["budget"], e))
        config.set_override("budget", v)
    if "exec_jobs_shown" in d:
        try:
            n = int(d["exec_jobs_shown"])
        except (TypeError, ValueError):
            raise SystemExit("Einstellungs-Speicher: exec_jobs_shown keine Zahl: %r" % (d["exec_jobs_shown"],))
        if not 1 <= n <= 500:
            raise SystemExit("Einstellungs-Speicher: exec_jobs_shown ausserhalb 1..500: %r" % (d["exec_jobs_shown"],))
        config.set_override("exec_jobs_shown", n)


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
        # Migration: das Legacy-Token aus der Env wird als Worker- und Submit-Token registriert;
        # die laufende VM und der Absender arbeiten weiter, beide Eintraege sind ueber die
        # Verwaltung loeschbar, sobald echte Tokens existieren
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
    # Schreib-Probe vor allem anderen: ein root-gehoeriges Volume wuerde sonst erst beim
    # ersten save_state mit einem Traceback crashen (PermissionError tokens.json.tmp)
    d = config.data_dir()
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".write-probe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
    except OSError as e:
        raise SystemExit(
            "DATA_DIR nicht schreibbar: %s (%s). Fix: Volume in Portainer loeschen (Inhalt wird "
            "aus der Env neu migriert) oder einmalig chownen: docker run --rm -v <volume>:/data "
            "alpine chown -R 65534:65534 /data; danach Container neu starten." % (d, e))
    load_settings()
    load_state()
    audit("boot", budget=config.budget(), outmax=config.OUTMAX, filemax=config.FILEMAX,
          workers=len(workers), submitters=len(submitters))


def reset_for_tests():
    # nur fuer Stub-Tests: alles zuruecksetzen
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
    # OOM-Schutz: Budget freiraeumen - erst erledigte Jobs (aeltestes zuerst), dann aelteste Dateien
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
    # Rueckverwandlung der Speicherform gzip+base64 in Rohbytes
    return gzip.decompress(base64.b64decode(rec["enc"]))
