# agent-sandbox-relay

Job-Queue-Service (Docker-Service-Name: agent-sandbox-relay) fuer die
vibe-sandbox-VM. Referenz-Doku: Canvas "vibe-sandbox-vm", Abschnitt 10.5
(Transportoption E: Pull-Agent mit Job-Queue auf dem Hetzner-Server).

Der Service fuehrt selbst keine Befehle aus. Er nimmt Jobs entgegen und
liefert sie an den authentifizierten Pull-Agent in der VM aus; die
Ausfuehrung passiert ausschliesslich in der VM.

Branches: main = Server-Seite (dieser Service), worker = VM-Seite
(cloud-init, Pull-Agent, systemd-Units, virt-install, Ziel-XML).

## API-Vertrag (alle Endpunkte: Bearer-Token im Authorization-Header, TLS via Traefik)

- POST /jobs mit {"cmd": "..."} -> {"id": "..."} (Absender)
- GET /jobs/next -> {"id": "...", "cmd": "..."} oder 204 (VM-Agent, Long-Poll bis 300 s)
- POST /jobs/<id>/result mit {"output": "...", "rc": 0} (VM-Agent)
- GET /jobs/<id> -> Status/Output (Absender)
- GET /healthz -> {"ok": true} (Healthcheck, ohne Token)

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

- Code enthaelt kein eval/exec/subprocess; Jobs werden nur gespeichert und
  an den authentifizierten Worker ausgeliefert.
- Pydantic erzwingt cmd als String mit max_length=10000 (Verstoss -> 422).
- Result-Output wird serverseitig auf 4 MiB gedeckelt, Job-Store auf 500
  Eintraege (In-Memory, aelteste zuerst raus).
- Container: USER 65534:65534, read_only, tmpfs /tmp, cap_drop ALL,
  no-new-privileges, mem_limit/cpus (Details im Canvas).
