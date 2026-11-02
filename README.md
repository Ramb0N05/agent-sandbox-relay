# agent-sandbox-relay - Branch worker (VM-Seite)

Dieser Branch enthaelt die VM-seitigen Dateien der vibe-sandbox-VM
(Canonical-Doku: Canvas "vibe-sandbox-vm"). Branch main enthaelt die
Server-Seite (Queue-Service agent-sandbox-relay: Dockerfile,
agent_sandbox_relay.py, README.md).

## Inhalt

- cloud-init/vibe-sandbox-user-data.yaml: komplette cloud-init user-data
  (User vibe mit NOPASSWD-sudo, Pakete, write_files, runcmd). Auf dem Host
  unter /root/vibe-sandbox-user-data.yaml ablegen; virt-install haengt sie
  als NoCloud-ISO nur beim ersten Boot an.
- files/agent-sandbox-agent: Pull-Agent (bash). Installationsziel in der
  VM: /usr/local/bin/agent-sandbox-agent, owner vibe:vibe, 0700. Normalfall
  ist die Provisionierung ueber die cloud-init-Datei; die Einzeldatei ist
  die Referenzkopie.
- files/agent-sandbox-agent.service: systemd-Unit des Pull-Agents
  (Restart=always), Ziel /etc/systemd/system/.
- files/ttyd-vibe.service: systemd-Unit des Web-Terminals (nur vom Host
  erreichbar, Basic Auth, max. 2 Clients, ZMODEM aktiviert), Ziel
  /etc/systemd/system/.
- vm/virt-install.sh: Anlage-Befehl fuer virt-install auf dem Host
  (q35, SeaBIOS-Import, Device-Minimierung, Serial-Only).
- vm/domain-xml-target.xml: Soll-Zustand der Device-/Feature-Sektion der
  Domain-XML (Pruefung/Nachzug per virsh edit vibe-sandbox).

## Platzhalter vor dem ersten Boot ersetzen

- <agent-sandbox-relay-token> in cloud-init und files/agent-sandbox-agent:
  identisch mit AGENT_SANDBOX_RELAY_TOKEN im Docker-Stack auf dem
  Hetzner-Server.
- <agent-sandbox-relay-domain> in cloud-init und files/agent-sandbox-agent:
  Subdomain des Queue-Services (ENDPOINT des Agenten).
- <ttyd-passwort> in cloud-init und files/ttyd-vibe.service: Basic-Auth-
  Passwort des Web-Terminals.

## Dateitransfer

- VM -> Host manuell: im ttyd-Web-Terminal lrzsz-sz <datei> (Browser bietet
  Download an; ZMODEM per -t enableZmodem=true aktiviert).
- VM -> Absender programmatisch: Job mit cmd "base64 -w0 <datei>" (oder
  "gzip -c <datei> | base64 -w0") absetzen, Output im Result ist der
  Base64-Text (Deckel 4 MiB).
