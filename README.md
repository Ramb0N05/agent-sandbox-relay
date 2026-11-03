# agent-sandbox-relay

Job-Queue-Service (Docker-Service-Name: agent-sandbox-relay) für die
vibe-sandbox-VM. Referenz-Doku: Canvas "vibe-sandbox-vm", Abschnitt 10.5
(Transportoption E: Pull-Agent mit Job-Queue auf dem Hetzner-Server);
Dateitransfer: Abschnitt 10.8.

Der Service führt selbst keine Befehle aus. Er nimmt Jobs entgegen und
liefert sie an den authentifizierten Pull-Agent (Worker) in der VM aus;
die Ausführung passiert ausschließlich in der VM. Dateien nimmt er als
Rohbytes entgegen, speichert sie gzip-komprimiert und base64-kodiert und
liefert sie beim Auslesen als Rohbytes zurück.

Modularisierung (elfte Runde 11/2026): agent_sandbox_relay.py ist nur noch
der Entry (FastAPI-App, Router-Assembly, /healthz). Die Logik liegt im
Paket relay/: config.py (Env, Budget-Syntax), store.py (Zustand,
Persistenz, Audit, OOM-Eviction), auth.py (Rollen-Tokens, Admin-Login mit
Lockout), jobs.py (Job-Endpunkte inkl. /usage), files.py (Datei-Endpunkte),
admin.py (Verwaltung) mit relay/templates/admin.html (Admin-UI als
HTML-Template, CSP mit frischem Nonce je Aufruf; alle Buttons binden
per addEventListener im nonce-gesicherten Script - Inline-Event-Handler
wie onclick= blockiert die CSP, Nonce/Hashes gelten nicht dafür).

Branches: main = Server-Seite (dieser Service), worker = VM-Seite
(cloud-init, Pull-Agent, systemd-Units, virt-install, Ziel-XML; enthält
keine Dateien aus main). Änderungen an der API immer in BEIDEN Branches
nachziehen (Server-Dateien hier, Platzhalter/Doku des Agenten dort).

## Rollen und Tokens

Drei Rollen mit getrennten Tokens (Erzeugung über die Verwaltung):

- Worker-Token (Präfix w-): Pull-Agent/VM. GET /jobs/next,
  POST /jobs/<id>/result, Datei-Endpunkte, GET /usage.
- Submit-Token (Präfix s-): Absender. POST /jobs, GET /jobs/<id>,
  GET /jobs/<id>/wait, Datei-Endpunkte, GET /usage.
- Admin (HTTP Basic, Nutzername admin bzw. AGENT_SANDBOX_ADMIN_USERNAME):
  Verwaltungsoberfläche GET /admin (Tokens erstellen/benennen/zuordnen/
  löschen, Job-Verwaltung je Submitter, Speicher- und Audit-Anzeige).
  Worker und Submitter sind optional benennbar: Name bei der Erstellung
  optional (leer -> automatisch benannt), nachträgliches Umbenennen per
  PATCH /admin/api/tokens/<id>.

Jeder Submitter ist genau einem Worker zugeordnet: seine Jobs landen nur
in der Queue dieses Workers, und nur dieser Worker kann sie ziehen und
Resultate liefern (403 sonst). Worker-Löschung wird abgelehnt (409),
solange Submitter zugeordnet sind oder offene Jobs in der Queue liegen.
Gespeichert wird nur sha256(Token); das Klartext-Token erscheint genau
einmal in der Antwort der Erstellung (Web-UI: nur diesmal sichtbar).

Migration/Bootstrap: Bei leerem Token-Speicher und gesetztem
AGENT_SANDBOX_RELAY_TOKEN registriert der Serverstart dieses Token als
Worker legacy und Submit legacy (zugeordnet). So laufen VM-Agent und
Absender nach einem Rebuild ohne Volume weiter; beide Einträge sind
über die Verwaltung löschbar, sobald echte Tokens existieren.

## API-Vertrag (Endpunkte ausser /healthz und /admin: Bearer-Token im Authorization-Header, TLS via Traefik)

- POST /jobs mit {"cmd": "..."} -> {"id": "..."} (Submit-Token)
- GET /jobs/<id>/wait?timeout=25 -> Job-Zustand; wartet bis das Result
  vorliegt oder der Timeout abläuft (1-300 s, Default 25). Ersetzt das
  Pollen des Absenders: ein Request genügt je Job (curl --max-time).
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
- GET /admin -> Verwaltungsoberfläche (Login admin, 401-Prüfung je
  API-Aufruf, Klartext-Token nur einmal sichtbar)

Die automatischen Doku-Oberflächen sind abgeschaltet (docs_url=None,
redoc_url=None, openapi_url=None): /docs, /redoc und /openapi.json
antworten 404. Die API-Doku liefert GET /usage (token-geschützt).

Verwaltung-API (alles hinter Admin-Login): GET /admin/api/tokens,
POST /admin/api/tokens {"role": "worker"|"submitter", "name": "..."
(optional, leer -> automatisch benannt), "worker_id": "<worker-id,
Pflicht bei submitter>"}, PATCH /admin/api/tokens/<id> {"name": "..."}
(Umbenennen), POST /admin/api/tokens/<id>/assign {"worker_id": "..."}
(nur Submitter), DELETE /admin/api/tokens/<id>,
GET /admin/api/jobs (Jobs je Submitter: offene queued/paused/running und
die zuletzt erledigten, Default 64, je Job mit abarbeitendem Worker,
cmd nur gekürzt), GET /admin/api/jobs/<id> (Job-Detail mit vollem cmd
für den Dialog der Jobsseite; Output auf 16 KiB gekürzt mit Kennung,
das gespeicherte Result bleibt unangetastet bis 8 MiB),
POST /admin/api/jobs/<id>/pause (wartend -> angehalten;
angehaltene Jobs zieht der Worker nicht), POST /admin/api/jobs/<id>/resume
(angehalten -> zurück in die Queue, Worker wird geweckt),
DELETE /admin/api/jobs/<id> (jeder Status; belegtes Budget wird bei
erledigten Jobs zurückgegeben), GET /admin/api/audit?limit=50,
GET /admin/api/state (Budget/Jobs/Dateien/Zähler),
GET /admin/api/settings (zentrale Einstellungen: änderbares Budget und
Anzeige-Limit mit Quelle datei|env|default, Container-Speichergrenze
mem_limit, Budget-Maximum budget_max = mem_limit - 64 MiB Headroom,
budget_headroom plus die nur per Stack-Umgebung fixen Grenzen), POST
/admin/api/settings {"budget": "384m",
"exec_jobs_shown": 64} (Validierung: Budget in Env-Syntax und nicht unter
dem Output-Deckel (422), nicht über dem Container-Maximum
mem_limit - 64 MiB (422), nicht unter dem belegten Speicher (409), Limit
1-500 (422); wirksam sofort und dauerhaft in DATA_DIR/settings.json,
beim Start fail-closed geladen wie tokens.json; das Maximum liest der
Relay aus der eigenen cgroup: /sys/fs/cgroup/memory.max (v2) bzw.
/sys/fs/cgroup/memory/memory.limit_in_bytes (v1) - der Container sieht
dank cgroup-Namespace seine eigene Grenze, "max" oder der v1-Default
9223372036854771712 bedeuten unbegrenzt, dann gilt kein Maximum),
GET /admin/api/usage (API-Doku für die Verwaltungsoberfläche; dieselbe
Struktur wie GET /usage, aber mit der Admin-Session statt Bearer-Token).

## Environment

- AGENT_SANDBOX_RELAY_TOKEN: Legacy-Token. Pflicht, solange der
  Token-Speicher leer ist (Migration, siehe oben); ohne Token und ohne
  Speicher startet der Prozess nicht (fail-closed, SystemExit).
- AGENT_SANDBOX_ADMIN_PASSWORD: Passwort der Verwaltung. Ungesetzt:
  Bootstrap-Modus, das Legacy-Token gilt als Admin-Passwort. Beide
  ungesetzt: Verwaltung antwortet 503.
- AGENT_SANDBOX_ADMIN_USERNAME: Nutzername der Verwaltung (Default admin,
  elfte Runde).
- AGENT_SANDBOX_EXECJOBSHOWN: je Submitter angezeigte erledigte Jobs in
  der Verwaltung (Default 64).
- AGENT_SANDBOX_RELAY_DATA: Verzeichnis für tokens.json und
  audit.jsonl (Default /tmp/agent-sandbox-relay). OHNE persistentes
  Volume geht der Token-Speicher bei jedem Neubau des Containers
  verloren (tmpfs) - die Legacy-Migration fängt das auf, echte Tokens
  sind dann aber weg. Beim Start prüft der Service die Schreibbarkeit
  mit einer Probe: Ist das Verzeichnis nicht schreibbar (typischer Fall:
  Volume gehört root, Container läuft als USER 65534), endet der Start
  mit einer klaren Meldung inklusive Fix (Volume löschen oder einmalig
  chown 65534:65534), nicht mit einem Traceback. Empfehlung für den Stack:

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

- AGENT_SANDBOX_RELAY_BUDGET: Speicherbudget für Output- plus
  Datei-Encoding-Bytes (Default 256m). Syntax wie mem_limit im
  Compose-File (elfte Runde): Einheiten b, k|kb, m|mb, g|gb
  case-insensitiv, ohne Einheit Bytes; Beispiel 384m. Ungültige Angabe
  beendet den Start mit klarer Meldung. OOM-Schutz: Bei Druck räumt
  der Server zuerst erledigte Jobs (älteste zuerst), dann älteste
  Dateien; ein Einzelresult, das nicht mehr ins Restbudget passt, wird
  mit Kennzeichnung gekürzt statt den Container zu killen; Uploads
  über Budget antworten 413.

## Build und Deployment

Dieses Repo enthält absichtlich keine compose.yml; der Service-Block
gehört in den bestehenden Stack auf dem Hetzner-Server. Build-Kontext ist
die Git-URL selbst:

    build: https://github.com/Ramb0N05/agent-sandbox-relay.git

Rebuild mit aktuellem Repo-Stand:

    docker compose up -d --build agent-sandbox-relay

Alternativ der Portainer-Stack-Webhook: POST auf die Webhook-URL des
Stacks stößt das Deployment des aktuellen Git-Stands an; der Aufruf
kehrt zurück, bevor das Deployment fertig ist (Ergebnis separat prüfen,
z. B. GET /healthz und /docs -> 404).

## Härtung

- Code enthält kein eval/exec/subprocess; Jobs und Dateien werden nur
  gespeichert und an authentifizierte Aufrufer ausgeliefert. Dateien
  speichert der Server gzip-komprimiert und base64-kodiert (Python-stdlib
  gzip/base64, keine Shell-Aufrufe).
- Token-Prüfung per sha256-Lookup (Hash inklusive Präfix) mit
  secrets.compare_digest (constant-time Vergleich gegen
  Timing-Seitenkanäle); nicht-ASCII-Header werden vorher mit 401
  abgelehnt, weil compare_digest nur ASCII-Strings akzeptiert. Neue
  Tokens: secrets.token_urlsafe(32) (256 Bit Entropie).
- Admin-Login: HTTP Basic (Nutzername admin bzw.
  AGENT_SANDBOX_ADMIN_USERNAME), compare_digest auf Nutzername und
  Passwort, 0,5 s Verzögerung je Fehlversuch; Lockout je Client-IP
  (hinter Traefik: letzter X-Forwarded-For-Eintrag): nach 5
  Fehlversuchen 300 s Sperre, je weiterem Fehlversuch verdoppelt bis
  3600 s (Antwort 429 mit Retry-After); erfolgreicher Login setzt
  Zähler und Sperre zurück. Digest wurde bewusst nicht umgesetzt: unter
  TLS schützt Basic genauso (OWASP: Basic Authentication must be
  conducted over TLS), Digest-Hashes sind je nach Browser MD5 (nicht
  empfohlen, SHA-256 verstehen erst neuere Firefox), und der
  Sicherheitsgewinn gegenüber Basic unter TLS ist null. Das Passwort
  liegt nur als Env im Stack.
- Admin-UI: HTML-Template relay/templates/admin.html, ausgeliefert mit
  Content-Security-Policy (default-src 'none', script-src mit frischem
  Nonce je Aufruf, style-src 'unsafe-inline', connect-src 'self',
  base-uri/frame-ancestors 'none'), Cache-Control no-store,
  X-Content-Type-Options nosniff, Referrer-Policy no-referrer.
  Keine Inline-Event-Handler im Markup: Nonces und Hashes befreien nur
  script-Elemente, nicht on*-Attribute; statische und dynamische
  Buttons binden deshalb per addEventListener im nonce-gesicherten
  Script.
- Admin-UI-Aufbau: Unterseiten Status (pollt /admin/api/state,
  Auto-Refresh 5 s, Budget-Balken), Tokens (anlegen per dialog, Token
  einmalige Anzeige mit Kopieren, umbenennen, Submitter umziehen,
  löschen mit Rückfrage), Jobs (pause/resume/löschen je Job, cmd
  vollständig per Dialog über GET /admin/api/jobs/<id>), Protokoll
  (Audit, Zeilenlimit einstellbar), Einstellungen (Budget und
  Anzeige-Limit zentral änderbar via POST /admin/api/settings,
  Budget-Maximum aus der Container-Grenze angezeigt) und
  API-Doku (rendert GET /admin/api/usage). Hash-Navigation (#status,
  #tokens, ...), Eingabedialoge als native <dialog>-Elemente mit
  showModal() (Baseline: seit März 2022 in allen Browsern, MDN).
  Login-Session in sessionStorage überlebt Reloads und Restore
  (MDN: Session gilt solange Tab/Browser offen, auch über Reloads);
  doLogin liest die Eingabefelder, der Reload-Pfad nutzt tryLogin mit
  den Session-Werten und verwirft sie bei abgelaufener Session.
- Admin-UI responsiv: Tabellen mit Klasse rwd klappen unter 720px auf
  Karten um (thead versteckt, jede Zelle eine Zeile, Spaltenname per
  td::before{content:attr(data-label)} aus dem Markup - Karten-Pattern
  ohne horizontales Scrollen), Nav mit overflow-x, Dialoge width 94vw.
  Zeitstempel rendert der Browser lokal (Date.prototype.toLocaleString
  mit dateStyle/timeStyle, Sprache und Zeitzone aus den
  Browsereinstellungen); der Server bleibt zeitzone-agnostisch.
- Pydantic erzwingt cmd als String mit max_length=10000 (Verstoss -> 422)
  und die Token-Felder (Rolle nur worker|submitter, Name 1-64 Zeichen).
- Verwaiste Long-Polls nehmen keine Jobs mehr an: GET /jobs/next prüft
  nach dem Aufwachen request.is_disconnected (Starlette) und lässt den
  Job in der Queue, wenn der Client weg ist. Ausserdem setzt der Handler
  das Ereignis erst vor dem Ziehen zurück - ein Job, der zwischen Ziehen
  und Zurücksetzen eintrifft, wird nicht mehr verlorenerweise
  verschlafen. GET /jobs/<id>/wait nutzt dasselbe Muster.
- Result-Output wird serverseitig auf 8 MiB gedeckelt, der Job-Store auf
  500 Einträge, der Datei-Store auf 64 Einträge (älteste zuerst raus);
  Datei-Rohbytes sind auf 8 MiB begrenzt (413 Content Too Large).
- Speicherbudget (siehe Environment) schützt vor OOM-Kills des
  Containers (Docker exit 137): Daten werden geräumt oder gekürzt,
  bevor das Limit erreicht ist. Das Budget liegt per Validation maximal
  bei Container-Grenze minus 64 MiB Headroom (Nutzerentscheidung):
  memory.max ist ein hartes Kernel-Limit mit OOM-Kill, und der 64-MiB-
  Puffer bleibt für Interpreter, Stack und Seiten-Caches des Relay
  selbst, die nicht im Budget zählen.
- Audit-Log (JSON-Zeilen, DATA_DIR/audit.jsonl, Rotation ab 2 MiB auf
  genau ein Backup .1): boot, migrate, submit (nur Länge, nie
  cmd-Inhalt), deliver, result, auth_fail, admin_auth_fail, token_create/
  assign/delete (nur Namen/IDs, nie Token-Werte), evict, trim, file_up/
  file_down. Audit-Fehler brechen den Betrieb nie.
- Container: USER 65534:65534, read_only, tmpfs /tmp, cap_drop ALL,
  no-new-privileges, mem_limit/cpus (Details im Canvas; mem_limit 512m -
  deshalb Budget deutlich darunter, z. B. 384m). Das Dockerfile legt /data
  mit chown 65534:65534 an: bei einer frischen (leeren) Volume-Kopie
  übernimmt Docker Inhalt und Besitzrechte aus dem Image; ein Volume, das
  vor diesem Image-Stand angelegt wurde, behält root und muss einmalig
  gelöscht oder gechownt werden (sonst Schreib-Probe-Fehler beim Start).

## Empfehlung Rate-Limit (Traefik, nicht Teil dieses Services)

Legitime Last: ein Absender mit Status-Long-Poll liegt bei wenigen
Requests je Job; der VM-Agent hält maximal einen /jobs/next offen.
Token-Brute-Force ist gegen 256-Bit-Token zwecklos, ein Rate-Limit
schärmt trotzdem DoS-Volumen ab. Vorschlag als Traefik-Middleware
(Token-Bucket: average/period = Nachfüllrate, burst = Bucket-Größe):

    traefik.http.middlewares.relay-ratelimit.ratelimit.average=30
    traefik.http.middlewares.relay-ratelimit.ratelimit.period=1s
    traefik.http.middlewares.relay-ratelimit.ratelimit.burst=100

    # und am Router des Relays anhängen:
    traefik.http.routers.<relay-router>.middlewares=relay-ratelimit

Zählbasis ist die Client-IP (Remote-Adresse). Im bestehenden Stack
terminiert Traefik selbst das TLS und zeigt direkt ins Internet, die
Remote-Adresse ist also die echte Client-IP und die Zählung stimmt.
