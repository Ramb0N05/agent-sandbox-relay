# relay/config.py - environment (11th round: human-readable budget syntax, admin username, job display limit)
import os

TOKEN = os.environ.get("AGENT_SANDBOX_RELAY_TOKEN", "")  # Legacy-Token: Migration bei leerem Token-Speicher
ADMIN_PASSWORD = os.environ.get("AGENT_SANDBOX_ADMIN_PASSWORD", "")  # admin login; unset -> bootstrap via legacy token
ADMIN_USERNAME = os.environ.get("AGENT_SANDBOX_ADMIN_USERNAME", "admin")
OUTMAX = 8 * 1024 * 1024  # Result-Output-Deckel 8 MiB (Nutzerentscheidung 11/2026, Abschnitt 10.7)
FILEMAX = 8 * 1024 * 1024  # raw size per file in the file store (section 10.8)
FILESTOREMAX = 64  # file store entries (FIFO, oldest dropped first)
AUDITMAX = 2 * 1024 * 1024  # audit log rotation: rotate to .1 at this size
JOBSTOREMAX = 500  # job store entries (oldest dropped first)
DEFAULT_BUDGET = "256m"

_UNITS = {"b": 1, "k": 1024, "kb": 1024, "m": 1024 * 1024, "mb": 1024 * 1024,
          "g": 1024 * 1024 * 1024, "gb": 1024 * 1024 * 1024}

# runtime overrides from DATA_DIR/settings.json (16th round): values the
# admin UI changes at runtime. Env stays fallback, settings.json wins.
# Betroffen: budget(), exec_jobs_shown() - alles andere bleibt Start-Umwelt.
_overrides: dict = {}


def set_override(key, value):
    _overrides[key] = value


def get_override(key):
    return _overrides.get(key)


def parse_size(raw):
    # byte value like mem_limit in the compose file: units b, k|kb, m|mb, g|gb (case-insensitive),
    # without unit bytes (docker docs: "b (bytes), k or kb (kilo bytes), m or mb (mega bytes) and g or gb (giga bytes)")
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
    # memory budget for output plus file encoding bytes; invalid value: abort startup
    if "budget" in _overrides:  # settings page: override from settings.json (already parsed)
        return _overrides["budget"]
    raw = os.environ.get("AGENT_SANDBOX_RELAY_BUDGET", DEFAULT_BUDGET)
    try:
        return parse_size(raw)
    except ValueError as e:
        raise SystemExit("AGENT_SANDBOX_RELAY_BUDGET ungültig: %r (%s). Syntax wie mem_limit im Compose-File: b, k|kb, m|mb, g|gb case-insensitiv, ohne Einheit Bytes (Beispiel: 384m)" % (raw, e))


def poll_seconds():
    # long-poll duration of /jobs/next (override before env; env can only be shortened for tests)
    if "long_poll_s" in _overrides:
        return max(0, int(_overrides["long_poll_s"]))
    try:
        return max(0, int(os.environ.get("AGENT_SANDBOX_RELAY_POLL", "300")))
    except ValueError:
        return 300


def exec_jobs_shown():
    # finished jobs shown per submitter in the admin UI
    if "exec_jobs_shown" in _overrides:  # settings page: override from settings.json
        return _overrides["exec_jobs_shown"]
    try:
        return max(1, int(os.environ.get("AGENT_SANDBOX_EXECJOBSSHOWN", "64")))
    except ValueError:
        return 64


# 17th round: more configurable limits - override from settings.json before the
# default (cmd length, store sizes, result cap, audit rotation, long poll).
# 18th round: datei_bytes_roh (raw file cap, 413 on upload) joined the
# configurable set. Fixed: only the admin username - it is tied to the
# container start environment.
CMD_ZEICHEN_HARD = 100000  # absolute pydantic cap for a job cmd; submit checks the configurable limit


def cmd_zeichen():
    # maximum length of a job cmd (the listing always shows it truncated)
    if "cmd_zeichen" in _overrides:
        return _overrides["cmd_zeichen"]
    return 10000


def result_output_bytes():
    # cap for job output in the store; the budget must stay above it
    if "result_output_bytes" in _overrides:
        return _overrides["result_output_bytes"]
    return OUTMAX


def datei_bytes_roh():
    # raw size cap per file in the file store (413 on upload); one file must fit the budget
    if "datei_bytes_roh" in _overrides:
        return _overrides["datei_bytes_roh"]
    return FILEMAX


def job_store_eintraege():
    # job store entries (FIFO, oldest dropped first)
    if "job_store_eintraege" in _overrides:
        return _overrides["job_store_eintraege"]
    return JOBSTOREMAX


def datei_store_eintraege():
    # file store entries (FIFO, oldest dropped first)
    if "datei_store_eintraege" in _overrides:
        return _overrides["datei_store_eintraege"]
    return FILESTOREMAX


def audit_rotation_bytes():
    # audit rotation threshold: rotate to exactly one backup .1 at this size
    if "audit_rotation_bytes" in _overrides:
        return _overrides["audit_rotation_bytes"]
    return AUDITMAX


# container memory limit (docker mem_limit), 17th round:
# the container reads its own cgroup - the cgroup namespace shows it
# "his own slice of the tree" (Redline Soft: "cat /sys/fs/cgroup/memory.max
# inside a container shows your limit, not the host's"). cgroup v2:
# memory.max is the hard limit ("The default is max, meaning there is no
# limit", Facebook cgroup2 docs); cgroup v1: memory.limit_in_bytes
# ("set/show limit of memory usage", kernel memory.txt); without a limit it
# holds PAGE_COUNTER_MAX * PAGE_SIZE = 9223372036854771712 (huatuo PR 717).
_CGROUP_LIMIT_FILES = (
    "/sys/fs/cgroup/memory.max",                       # cgroup v2
    "/sys/fs/cgroup/memory/memory.limit_in_bytes",     # cgroup v1
)
_CGROUP_UNLIMITED = 1 << 50  # threshold: the v1 default sits near 8 EiB, a real limit never does

BUDGET_HEADROOM = 64 * 1024 * 1024  # buffer between budget and container limit (user decision: mem_limit - 64 MiB)


def mem_limit():
    # container memory limit in bytes, or None (unlimited/unknown)
    for path in _CGROUP_LIMIT_FILES:
        try:
            with open(path, encoding="ascii") as f:
                raw = f.read().strip()
        except OSError:
            continue
        if raw == "max":  # cgroup v2 unlimited
            return None
        try:
            v = int(raw)
        except ValueError:
            continue
        if v >= _CGROUP_UNLIMITED:  # cgroup v1 unlimited (9223372036854771712)
            return None
        return v
    return None


def budget_max():
    # ceiling for the memory budget: container memory limit minus headroom;
    # None if the container runs unlimited (then no kernel OOM threatens via the budget)
    m = mem_limit()
    return None if m is None else max(0, m - BUDGET_HEADROOM)
