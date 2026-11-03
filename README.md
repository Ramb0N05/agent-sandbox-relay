# agent-sandbox-relay

Job-Queue-Service (Docker-Service-Name: agent-sandbox-relay) fuer die
vibe-sandbox-VM. Referenz-Doku: Canvas "vibe-sandbox-vm", Abschnitt 10.5
(Transportoption E: Pull-Agent mit Job-Queue auf dem Hetzner-Server);
Dateitransfer: Abschnitt 10.8.

Der Service fuehrt selbst keine Befehle aus. Er nimmt Jobs entgegen und
liefert sie an den authentifizierten Pull-Agent (Worker) in der VM aus;
die Ausfuehrung passiert ausschliesslich in der VM. Dateien nimmt er als
Rohbytes entgegen, speichert sie gzip-komprimiert und base64-kodiert und
liefert sie beim Auslesen als Rohbytes zurueck.

Branches: main = Server-Seite (dieser Service), worker = VM-Seite
(cloud-init, Pull-Agent, systemd-Units, virt-install, Ziel-XML; enthaelt
keine Dateien aus main). Aenderungen an der API immer in BEIDEN Branches
nachziehen (Server-Dateien hier, Platzhalter/Doku des Agenten dort).

## Rollen und Tokens

Drei Rollen mit getrennten Tokens (Erzeugung ueber die Verwaltung):

- Worker-Token (Praefix w-): Pull-Agent/VM. GET /jobs/next,
  POST /jobs/<id>/result, Datei-Endpunkte, GET /usage.
- Submit-Token (Praefix s-): Absender. POST /jobs, GET /jobs/<id>,
  GET /jobs/<id>/wait, Datei-Endpunkte, GET /usage.
- Admin (HTTP Basic, Nutzername admin): Verwaltungsoberflaeche GET /admin
  (Tokens erstellen/zuordnen/loeschen, Speicher- und Audit-Anzeige).

Jeder Submitter ist genau einem Worker zugeordnet: seine Jobs landen nur
in der Queue dieses Workers, und nur dieser Worker kann sie ziehen und
Resultate liefern (403 sonst). Worker-Loeschung wird abgelehnt (409),
solange Submitter zugeordnet sind oder offene Jobs in der Queue liegen.
Gespeichert wird nur sha256(Token); das Klartext-Token erscheint genau
einmal in der Antwort der Erstellung (Web-UI: nur diesmal sichtbar).

Migration/Bootstrap: Bei leerem Token-Speicher und gesetztem
AGENT_SANDBOX_RELAY_TOKEN registriert der Serverstart dieses Token als
Worker legacy und Submit legacy (zugeordnet). So laufen VM-Agent und
Absender nach einem Rebuild ohne Volume weiter; beide Eintraege sind
ueber die Verwaltung loeschbar, sobald echte Tokens existieren.

## API-Vertrag (Endpunkte ausser /healthz und /admin: Bearer-Token im Authorization-Header, TLS via Traefik)

- POST /jobs mit {"cmd": "..."} -> {"id": "..."} (Submit-Token)
- GET /jobs/<id>/wait?timeout=25 -> Job-Zustand; wartet bis das Result
  vorliegt oder der Timeout ablaeuft (1-300 s, Default 25). Ersetzt das
  Pollen des Absenders: ein Request genuegt je Job (curl --max-time).
  (Submit-Token, nur eigene Jobs)
- GET /jobs/<id> -> Status/Output (Submit-Token, nur eigene Jobs)
- GET /jobs/next -> {"id": "...", "cmd": "..."} oder 204 (Worker-Token,
  Long-Poll bis 300 s, nur Jobs des eigenen Workers)
- POST /jobs/<id>/result mit {"output": "...", "rc": 0} (Worker-Token,
  nur eigene Jobs)
- POST /files mit Datei-Rohbytes -> {"id": "...", "size": ...,
  "size_enc": ...} (beide Rollen)
- GET /files/<id> -> Datei-Rohbytes (beide Rollen)
- GET /usage -> Dokumentation/Verwendung der API als JSON (beide Rollen)
- GET /healthz -> {"ok": true} (Healthcheck, ohne Token)
- GET /admin -> Verwaltungsoberflaeche (Login admin, 401-Pruefung je
  API-Aufruf, Klartext-Token nur einmal sichtbar)

Die automatischen Doku-Oberflaechen sind abgeschaltet (docs_url=None,
redoc_url=None, openapi_url=None): /docs, /redoc und /openapi.json
antworten 404. Die API-Doku liefert GET /usage (token-geschuetzt).

Verwaltung-API (alles hinter Admin-Login): GET /admin/api/tokens,
POST /admin/api/tokens {"role": "worker"|"submitter", "name": "...",
"worker_id": "<worker-id, Pflicht bei submitter>"},
POST /admin/api/tokens/<id>/assign {"worker_id": "..."} (nur Submitter),
DELETE /admin/api/tokens/<id>, GET /admin/api/audit?limit=50,
GET /admin/api/state (Budget/Jobs/Dateien/Zaehler).

## Environment

- AGENT_SANDBOX_RELAY_TOKEN: Legacy-Token. Pflicht, solange der
  Token-Speicher leer ist (Migration, siehe oben); ohne Token und ohne
  Speicher startet der Prozess nicht (fail-closed, SystemExit).
- AGENT_SANDBOX_ADMIN_PASSWORD: Passwort der Verwaltung (Nutzername
  admin). Ungesetzt: Bootstrap-Modus, das Legacy-Token gilt als
  Admin-Passwort. Beide ungesetzt: Verwaltung antwortet 503.
- AGENT_SANDBOX_RELAY_DATA: Verzeichnis fuer tokens.json und
  audit.jsonl (Default /tmp/agent-sandbox-relay). OHNE persistentes
  Volume geht der Token-Speicher bei jedem Neubau des Containers
  verloren (tmpfs) - die Legacy-Migration faengt das auf, echte Tokens
  sind dann aber weg. Empfehlung fuer den Stack:

      agent-sandbox-relay:
        build: https://github.com/Ramb0N05/agent-sandbox-relay.git
        read_only: true
        tmpfs:
          - /tmp
        volumes:
          - agent-sandbox-relay-data:/data
        environment:
          AGENT_SANDBOX_RELAY_DATA: /data
          AGENT_SANDBOX_ADMIN_PASSWORD: <admin-passwort>

      volumes:
        agent-sandbox-relay-data:

- AGENT_SANDBOX_RELAY_BUDGET: Speicherbudget fuer Output- plus
  Datei-Encoding-Bytes (Default 256 MiB). OOM-Schutz: Bei Druck raeumt
  der Server zuerst erledigte Jobs (aelteste zuerst), dann aelteste
  Dateien; ein Einzelresult, das nicht mehr ins Restbudget passt, wird
  mit Kennzeichnung gekuerzt statt den Container zu killen; Uploads
  ueber Budget antworten 413.

## Build und Deployment

Dieses Repo enthaelt absichtlich keine compose.yml; der Service-Block
gehoert in den bestehenden Stack auf dem Hetzner-Server. Build-Kontext ist
die Git-URL selbst:

    build: https://github.com/Ramb0N05/agent-sandbox-relay.git

Rebuild mit aktuellem Repo-Stand:

    docker compose up -d --build agent-sandbox-relay

Alternativ der Portainer-Stack-Webhook: POST auf die Webhook-URL des
Stacks stoesst das Deployment des aktuellen Git-Stands an; der Aufruf
kehrt zurueck, bevor das Deployment fertig ist (Ergebnis separat pruefen,
z. B. GET /healthz und /docs -> 404).

## Haertung

- Code enthaelt kein eval/exec/subprocess; Jobs und Dateien werden nur
  gespeichert und an authentifizierte Aufrufer ausgeliefert. Dateien
  speichert der Server gzip-komprimiert und base64-kodiert (Python-stdlib
  gzip/base64, keine Shell-Aufrufe).
- Token-Pruefung per sha256-Lookup (Hash inklusive Praefix) mit
  secrets.compare_digest (constant-time Vergleich gegen
  Timing-Seitenkanaele); nicht-ASCII-Header werden vorher mit 401
  abgelehnt, weil compare_digest nur ASCII-Strings akzeptiert. Neue
  Tokens: secrets.token_urlsafe(32) (256 Bit Entropie).
- Admin-Login: HTTP Basic (Nutzername admin), compare_digest, 0,5 s
  Verzoegerung je Fehlversuch; das Passwort liegt nur als Env im Stack.
- Pydantic erzwingt cmd als String mit max_length=10000 (Verstoss -> 422)
  und die Token-Felder (Rolle nur worker|submitter, Name 1-64 Zeichen).
- Verwaiste Long-Polls nehmen keine Jobs mehr an: GET /jobs/next prueft
  nach dem Aufwachen request.is_disconnected (Starlette) und laesst den
  Job in der Queue, wenn der Client weg ist. Ausserdem setzt der Handler
  das Ereignis erst vor dem Ziehen zurueck - ein Job, der zwischen Ziehen
  und Zuruecksetzen eintrifft, wird nicht mehr verlorenerweise
  verschlafen. GET /jobs/<id>/wait nutzt dasselbe Muster.
- Result-Output wird serverseitig auf 8 MiB gedeckelt, der Job-Store auf
  500 Eintraege, der Datei-Store auf 64 Eintraege (aelteste zuerst raus);
  Datei-Rohbytes sind auf 8 MiB begrenzt (413 Content Too Large).
- Speicherbudget (siehe Environment) schuetzt vor OOM-Kills des
  Containers (Docker exit 137): Daten werden geraeumt oder gekuerzt,
  bevor das Limit erreicht ist.
- Audit-Log (JSON-Zeilen, DATA_DIR/audit.jsonl, Rotation ab 2 MiB auf
  genau ein Backup .1): boot, migrate, submit (nur Laenge, nie
  cmd-Inhalt), deliver, result, auth_fail, admin_auth_fail, token_create/
  assign/delete (nur Namen/IDs, nie Token-Werte), evict, trim, file_up/
  file_down. Audit-Fehler brechen den Betrieb nie.
- Container: USER 65534:65534, read_only, tmpfs /tmp, cap_drop ALL,
  no-new-privileges, mem_limit/cpus (Details im Canvas; mem_limit 512m -
  deshalb Budget deutlich darunter).

## Empfehlung Rate-Limit (Traefik, nicht Teil dieses Services)

Legitime Last: ein Absender mit Status-Long-Poll liegt bei wenigen
Requests je Job; der VM-Agent haelt maximal einen /jobs/next offen.
Token-Brute-Force ist gegen 256-Bit-Token zwecklos, ein Rate-Limit
schaermt trotzdem Doerrian-Volumen ab. Vorschlag als Traefik-Middleware
(Token-Bucket: average/period = Nachfuellrate, burst = Bucket-Groesse):

    traefik.http.middlewares.relay-ratelimit.ratelimit.average=30
    traefik.http.middlewares.relay-ratelimit.ratelimit.period=1s
    traefik.http.middlewares.relay-ratelimit.ratelimit.burst=100

    # und am Router des Relays anhaengen:
    traefik.http.routers.<relay-router>.middlewares=relay-ratelimit

Zaehlbasis ist die Client-IP (Remote-Adresse). Im bestehenden Stack
terminiert Traefik selbst das TLS und zeigt direkt ins Internet, die
Remote-Adresse ist also die echte Client-IP und die Zaehlung stimmt.
