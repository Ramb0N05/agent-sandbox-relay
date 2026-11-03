# agent-sandbox-relay

Job-Queue-Service (Docker-Service-Name: agent-sandbox-relay) fuer die
vibe-sandbox-VM. Referenz-Doku: Canvas "vibe-sandbox-vm", Abschnitt 10.5
(Transportoption E: Pull-Agent mit Job-Queue auf dem Hetzner-Server);
Dateitransfer: Abschnitt 10.8.

Der Service fuehrt selbst keine Befehle aus. Er nimmt Jobs entgegen und
liefert sie an den authentifizierten Pull-Agent in der VM aus; die
Ausfuehrung passiert ausschliesslich in der VM. Dateien nimmt er als
Rohbytes entgegen, speichert sie gzip-komprimiert und base64-kodiert und
liefert sie beim Auslesen als Rohbytes zurueck.

Branches: main = Server-Seite (dieser Service), worker = VM-Seite
(cloud-init, Pull-Agent, systemd-Units, virt-install, Ziel-XML; enthaelt
keine Dateien aus main).

## API-Vertrag (alle Endpunkte: Bearer-Token im Authorization-Header, TLS via Traefik)

- POST /jobs mit {"cmd": "..."} -> {"id": "..."} (Absender)
- GET /jobs/next -> {"id": "...", "cmd": "..."} oder 204 (VM-Agent, Long-Poll bis 300 s)
- POST /jobs/<id>/result mit {"output": "...", "rc": 0} (VM-Agent)
- GET /jobs/<id> -> Status/Output (Absender)
- POST /files mit Datei-Rohbytes -> {"id": "...", "size": ..., "size_enc": ...} (Absender oder VM-Agent)
- GET /files/<id> -> Datei-Rohbytes (Absender oder VM-Agent)
- GET /usage -> Dokumentation/Verwendung der API als JSON (Absender)
- GET /healthz -> {"ok": true} (Healthcheck, ohne Token)

Die automatischen Doku-Oberflaechen sind abgeschaltet (docs_url=None,
redoc_url=None, openapi_url=None): /docs, /redoc und /openapi.json
antworten 404. Die API-Doku liefert GET /usage (token-geschuetzt).

## Environment

- AGENT_SANDBOX_RELAY_TOKEN: Bearer-Token, Pflicht. Ohne Token startet der
  Prozess nicht (fail-closed, SystemExit).

## Build

Dieses Repo enthaelt absichtlich keine compose.yml; der Service-Block
gehoert in den bestehenden Stack auf dem Hetzner-Server. Build-Kontext ist
die Git-URL selbst:

    build: https://github.com/Ramb0N05/agent-sandbox-relay.git

Rebuild mit aktuellem Repo-Stand:

    docker compose up -d --build agent-sandbox-relay

Komplett ohne Layer-Cache:

    docker compose build --no-cache agent-sandbox-relay

## Haertung

- Code enthaelt kein eval/exec/subprocess; Jobs und Dateien werden nur
  gespeichert und an authentifizierte Aufrufer ausgeliefert. Dateien
  speichert der Server gzip-komprimiert und base64-kodiert (Python-stdlib
  gzip/base64, keine Shell-Aufrufe).
- Token-Pruefung mit secrets.compare_digest (constant-time Vergleich gegen
  Timing-Seitenkanaele); nicht-ASCII-Header werden vorher mit 401
  abgelehnt, weil compare_digest nur ASCII-Strings akzeptiert.
- Pydantic erzwingt cmd als String mit max_length=10000 (Verstoss -> 422).
- Verwaiste Long-Polls nehmen keine Jobs mehr an: GET /jobs/next prueft
  nach dem Aufwachen request.is_disconnected (Starlette) und laesst den
  Job in der Queue, wenn der Client weg ist. Ausserdem setzt der Handler
  das Ereignis erst vor dem Ziehen zurueck - ein Job, der zwischen Ziehen
  und Zuruecksetzen eintrifft, wird nicht mehr verlorenerweise
  verschlafen.
- Result-Output wird serverseitig auf 8 MiB gedeckelt, der Job-Store auf
  500 Eintraege, der Datei-Store auf 64 Eintraege (In-Memory, aelteste
  zuerst raus); Datei-Rohbytes sind auf 8 MiB begrenzt (413 Content Too
  Large).
- Container: USER 65534:65534, read_only, tmpfs /tmp, cap_drop ALL,
  no-new-privileges, mem_limit/cpus (Details im Canvas; mem_limit 512m).
