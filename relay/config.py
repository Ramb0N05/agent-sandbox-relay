# relay/config.py - Umgebung (elfte Runde: menschenlesbare Budget-Syntax, Admin-Nutzername, Job-Anzeige-Limit)
import os

TOKEN = os.environ.get("AGENT_SANDBOX_RELAY_TOKEN", "")  # Legacy-Token: Migration bei leerem Token-Speicher
ADMIN_PASSWORD = os.environ.get("AGENT_SANDBOX_ADMIN_PASSWORD", "")  # Login der Verwaltung; ungesetzt -> Bootstrap ueber Legacy-Token
ADMIN_USERNAME = os.environ.get("AGENT_SANDBOX_ADMIN_USERNAME", "admin")
OUTMAX = 8 * 1024 * 1024  # Result-Output-Deckel 8 MiB (Nutzerentscheidung 11/2026, Abschnitt 10.7)
FILEMAX = 8 * 1024 * 1024  # Roh-Groesse pro Datei im Datei-Store (Abschnitt 10.8)
FILESTOREMAX = 64  # Eintraege im Datei-Store (FIFO, aelteste zuerst raus)
AUDITMAX = 2 * 1024 * 1024  # Audit-Log-Rotation: ab dieser Groesse auf .1 umdrehen
JOBSTOREMAX = 500  # Eintraege im Job-Store (aelteste zuerst raus)
DEFAULT_BUDGET = "256m"

_UNITS = {"b": 1, "k": 1024, "kb": 1024, "m": 1024 * 1024, "mb": 1024 * 1024,
          "g": 1024 * 1024 * 1024, "gb": 1024 * 1024 * 1024}


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
    # Speicherbudget fuer Output- plus Datei-Encoding-Bytes; ungueltige Angabe: Start-Abbruch
    raw = os.environ.get("AGENT_SANDBOX_RELAY_BUDGET", DEFAULT_BUDGET)
    try:
        return parse_size(raw)
    except ValueError as e:
        raise SystemExit("AGENT_SANDBOX_RELAY_BUDGET ungueltig: %r (%s). Syntax wie mem_limit im Compose-File: b, k|kb, m|mb, g|gb case-insensitiv, ohne Einheit Bytes (Beispiel: 384m)" % (raw, e))


def poll_seconds():
    # Long-Poll-Dauer von /jobs/next (Env nur fuer Tests verkuerzbar)
    try:
        return max(0, int(os.environ.get("AGENT_SANDBOX_RELAY_POLL", "300")))
    except ValueError:
        return 300


def exec_jobs_shown():
    # je Submitter angezeigte erledigte Jobs in der Verwaltung
    try:
        return max(1, int(os.environ.get("AGENT_SANDBOX_EXECJOBSSHOWN", "64")))
    except ValueError:
        return 64
