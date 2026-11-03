# relay/config.py - Umgebung (elfte Runde: menschenlesbare Budget-Syntax, Admin-Nutzername, Job-Anzeige-Limit)
import os

TOKEN = os.environ.get("AGENT_SANDBOX_RELAY_TOKEN", "")  # Legacy-Token: Migration bei leerem Token-Speicher
ADMIN_PASSWORD = os.environ.get("AGENT_SANDBOX_ADMIN_PASSWORD", "")  # Login der Verwaltung; ungesetzt -> Bootstrap über Legacy-Token
ADMIN_USERNAME = os.environ.get("AGENT_SANDBOX_ADMIN_USERNAME", "admin")
OUTMAX = 8 * 1024 * 1024  # Result-Output-Deckel 8 MiB (Nutzerentscheidung 11/2026, Abschnitt 10.7)
FILEMAX = 8 * 1024 * 1024  # Roh-Größe pro Datei im Datei-Store (Abschnitt 10.8)
FILESTOREMAX = 64  # Einträge im Datei-Store (FIFO, älteste zuerst raus)
AUDITMAX = 2 * 1024 * 1024  # Audit-Log-Rotation: ab dieser Größe auf .1 umdrehen
JOBSTOREMAX = 500  # Einträge im Job-Store (älteste zuerst raus)
DEFAULT_BUDGET = "256m"

_UNITS = {"b": 1, "k": 1024, "kb": 1024, "m": 1024 * 1024, "mb": 1024 * 1024,
          "g": 1024 * 1024 * 1024, "gb": 1024 * 1024 * 1024}

# Laufzeit-Overrides aus DATA_DIR/settings.json (sechzehnte Runde): Werte, die die
# Verwaltungsoberfläche zur Laufzeit ändert. Env bleibt Fallback, settings.json gewinnt.
# Betroffen: budget(), exec_jobs_shown() - alles andere bleibt Start-Umwelt.
_overrides: dict = {}


def set_override(key, value):
    _overrides[key] = value


def get_override(key):
    return _overrides.get(key)


def parse_size(raw):
    # Byte-Wert wie mem_limit im Compose-File: Einheiten b, k|kb, m|mb, g|gb (case-insensitiv),
    # ohne Einheit Bytes (docker docs: "b (bytes), k or kb (kilo bytes), m or mb (mega bytes) and g or gb (giga bytes)")
    if not isinstance(raw, str) or not raw:
        raise ValueError("leere Angabe")
    s = raw.strip().lower()
    num = ""
    while s and (s[0].isdigit() or s[0] in "."):
        num += s[0]
        s = s[1:]
    try:
        value = float(num)
    except ValueError:
        raise ValueError("keine Zahl: %r" % raw)
    if s == "":
        factor = 1
    elif s in _UNITS:
        factor = _UNITS[s]
    else:
        raise ValueError("unbekannte Einheit: %r" % raw)
    if value < 0:
        raise ValueError("negativ: %r" % raw)
    return int(value * factor)


def data_dir():
    return os.environ.get("AGENT_SANDBOX_RELAY_DATA", "/tmp/agent-sandbox-relay")


def budget():
    # Speicherbudget für Output- plus Datei-Encoding-Bytes; ungültige Angabe: Start-Abbruch
    if "budget" in _overrides:  # Einstellungsseite: Override aus settings.json (bereits geparst)
        return _overrides["budget"]
    raw = os.environ.get("AGENT_SANDBOX_RELAY_BUDGET", DEFAULT_BUDGET)
    try:
        return parse_size(raw)
    except ValueError as e:
        raise SystemExit("AGENT_SANDBOX_RELAY_BUDGET ungültig: %r (%s). Syntax wie mem_limit im Compose-File: b, k|kb, m|mb, g|gb case-insensitiv, ohne Einheit Bytes (Beispiel: 384m)" % (raw, e))


def poll_seconds():
    # Long-Poll-Dauer von /jobs/next (Override vor Env; Env nur für Tests verkürzbar)
    if "long_poll_s" in _overrides:
        return max(0, int(_overrides["long_poll_s"]))
    try:
        return max(0, int(os.environ.get("AGENT_SANDBOX_RELAY_POLL", "300")))
    except ValueError:
        return 300


def exec_jobs_shown():
    # je Submitter angezeigte erledigte Jobs in der Verwaltung
    if "exec_jobs_shown" in _overrides:  # Einstellungsseite: Override aus settings.json
        return _overrides["exec_jobs_shown"]
    try:
        return max(1, int(os.environ.get("AGENT_SANDBOX_EXECJOBSSHOWN", "64")))
    except ValueError:
        return 64


# siebzehnte Runde: weitere einstellbare Grenzen - Override aus settings.json vor dem
# Default (Cmd-Länge, Store-Größen, Result-Deckel, Audit-Rotation, Long-Poll).
# Fix bleiben: datei_bytes_roh (FILEMAX, 413-Validierung beim Upload) und der
# Admin-Nutzername - beide hängen an der Start-Umgebung des Containers.
CMD_ZEICHEN_HARD = 100000  # absolute Pydantic-Kappe für Job-cmd; das einstellbare Limit prüft submit


def cmd_zeichen():
    # Maximale Länge eines Job-cmd (das Listing zeigt ihn unabhängig davon gekürzt)
    if "cmd_zeichen" in _overrides:
        return _overrides["cmd_zeichen"]
    return 10000


def result_output_bytes():
    # Deckel für Job-Output im Store; das Budget muss darüber bleiben
    if "result_output_bytes" in _overrides:
        return _overrides["result_output_bytes"]
    return OUTMAX


def job_store_eintraege():
    # Einträge im Job-Store (FIFO, älteste zuerst raus)
    if "job_store_eintraege" in _overrides:
        return _overrides["job_store_eintraege"]
    return JOBSTOREMAX


def datei_store_eintraege():
    # Einträge im Datei-Store (FIFO, älteste zuerst raus)
    if "datei_store_eintraege" in _overrides:
        return _overrides["datei_store_eintraege"]
    return FILESTOREMAX


def audit_rotation_bytes():
    # Audit-Rotationsschwelle: ab dieser Größe auf genau ein Backup .1
    if "audit_rotation_bytes" in _overrides:
        return _overrides["audit_rotation_bytes"]
    return AUDITMAX


# Speichergrenze des Containers (docker mem_limit), siebzwehnte Runde:
# Der Container liest seine eigene cgroup - der cgroup-Namespace zeigt ihm
# "his own slice of the tree" (Redline Soft: "cat /sys/fs/cgroup/memory.max
# inside a container shows your limit, not the host's"). cgroup v2:
# memory.max ist der Hard-Limit ("The default is max, meaning there is no
# limit", Facebook cgroup2-Doku); cgroup v1: memory.limit_in_bytes
# ("set/show limit of memory usage", kernel memory.txt), ohne Limit steht
# dort PAGE_COUNTER_MAX * PAGE_SIZE = 9223372036854771712 (huatuo PR 717).
_CGROUP_LIMIT_FILES = (
    "/sys/fs/cgroup/memory.max",                       # cgroup v2
    "/sys/fs/cgroup/memory/memory.limit_in_bytes",     # cgroup v1
)
_CGROUP_UNLIMITED = 1 << 50  # Schwellwert: v1-Default liegt bei ~8 EiB, echtes Limit nie

BUDGET_HEADROOM = 64 * 1024 * 1024  # Puffer zwischen Budget und Container-Grenze (Nutzerentscheidung: mem_limit - 64 MiB)


def mem_limit():
    # Speichergrenze des Containers in Bytes oder None (unbegrenzt/unbekannt)
    for path in _CGROUP_LIMIT_FILES:
        try:
            with open(path, encoding="ascii") as f:
                raw = f.read().strip()
        except OSError:
            continue
        if raw == "max":  # cgroup v2 ohne Limit
            return None
        try:
            v = int(raw)
        except ValueError:
            continue
        if v >= _CGROUP_UNLIMITED:  # cgroup v1 ohne Limit (9223372036854771712)
            return None
        return v
    return None


def budget_max():
    # Höchstwert für das Speicherbudget: Container-Speichergrenze minus Headroom;
    # None, wenn der Container unbegrenzt läuft (dann droht kein Kernel-OOM über das Budget)
    m = mem_limit()
    return None if m is None else max(0, m - BUDGET_HEADROOM)
