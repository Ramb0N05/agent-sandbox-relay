# agent-sandbox-relay - Branch worker (VM-Seite)

Dieser Branch enthaelt die VM-seitigen Dateien der vibe-sandbox-VM
(Canonical-Doku: Canvas "vibe-sandbox-vm"). Branch main enthaelt die
Server-Seite (Queue-Service agent-sandbox-relay: Dockerfile,
agent_sandbox_relay.py, README.md). Der Branch enthaelt nur die
VM-seitigen Dateien; die beim Anlegen aus main geerbten Dateien
(Dockerfile, agent_sandbox_relay.py) sind geloescht.

## Inhalt

- cloud-init/vibe-sandbox-user-data.yaml: komplette cloud-init user-data
  (User vibe mit NOPASSWD-sudo, Pakete, write_files, runcmd). Ohne SSH:
  ssh_genkeytypes: [] verhindert die Host-Key-Generierung durch das
  cc_ssh-Modul (leere Liste statt Default rsa/ecdsa/ed25519), runcmd
  deinstalliert openssh per pacman -Rns --noconfirm und loescht die
  generierten Host-Keys (das cloud-init packages-Modul kann nur
  installieren, keine Pakete entfernen). Auf dem Host
  unter /root/vibe-sandbox-user-data.yaml ablegen; virt-install haengt sie
  als NoCloud-ISO nur beim ersten Boot an.
- files/agent-sandbox-agent: Pull-Agent (elfte Runde: Python statt bash;
  Long-Poll gegen die Queue mit persistenter HTTP-Verbindung und einem
  Reconnect-Retry je Aufruf statt einem curl-Prozess je Request, kein
  Leerlauf-Sleep - /jobs/next blockiert serverseitig, sleep 1 nur im
  Fehlerfall; Ausfuehrung mit timeout 900 bash -lc als User vibe,
  Praefixe VIBE_FETCH und VIBE_SEND fuer den Datei-Transport ueber die
  Files-Endpunkte; ssl-Default-Kontext mit Zertifikatspruefung).
  Installationsziel in der VM: /usr/local/bin/agent-sandbox-agent, owner
  vibe:vibe, 0700. Normalfall ist die Provisionierung ueber die
  cloud-init-Datei (Paket python3 in der Paketliste); die Einzeldatei ist
  die Referenzkopie.
- files/agent-sandbox-agent.service: systemd-Unit des Pull-Agents
  (Restart=always), Ziel /etc/systemd/system/.
- files/ttyd-vibe.service: systemd-Unit des Web-Terminals (nur vom Host
  erreichbar, Basic Auth, max. 2 Clients, ZMODEM aktiviert), Ziel
  /etc/systemd/system/.
- files/vmadmin: Verwaltungs-UI der VM auf Port 80 (fuenfzehnte Runde,
  Stil wie die Relay-Verwaltung: Dark-Theme, Hash-Navigation, native
  dialog-Modals, CSP-Nonce, Login-Card mit sessionStorage, Auto-Refresh,
  401 ohne WWW-Authenticate-Challenge). Nur Python-stdlib (http.server,
  ThreadingHTTPServer); Port 80 als User vibe laeuft dank
  AmbientCapabilities=CAP_NET_BIND_SERVICE in der Unit (Ports unter 1024
  brauchen sonst root; CapabilityBoundingSet schneidet weitere Caps ab,
  NoNewPrivileges entfaellt, weil vmadmin sudo fuer systemctl/journalctl
  benutzt). Funktionen: Status (Hostname, Kernel, Uptime, Last, RAM- und
  Disk-Belegung als Balken, Dienst-Zustaende, Token-Maske), Worker-Token
  setzen (8-128 ASCII ohne Whitespace, atomar nach
  /home/vibe/.config/agent-sandbox/worker-token, danach Neustart des
  Agents, weil der das Token nur beim Start liest), ttyd-Passwort setzen
  (1-64 druckbare ASCII ohne Doppelpunkt und Whitespace - ttyd kennt nur
  -c user:password und kein Credential-File; die Unit wird neu geschrieben,
  daemon-reload und Neustart), eigenes Admin-Passwort aendern (sha256-Hash
  in /home/vibe/.config/agent-sandbox/admin-hash; der Hash liegt nur als
  sha256 in der Datei), Dienst-Neustarts und das Agent-Journal
  (journalctl -n 30). Login-Lockout wie im Relay (5 Fehlversuche, 300 s,
  Verdopplung bis 3600 s, 429 mit Retry-After). Installationsziel
  /usr/local/bin/vmadmin, owner vibe:vibe, 0700.
- files/vm-admin.service: systemd-Unit der Verwaltung (User vibe,
  Restart=always, AmbientCapabilities=CAP_NET_BIND_SERVICE,
  CapabilityBoundingSet=CAP_NET_BIND_SERVICE), Ziel
  /etc/systemd/system/.
- vm/virt-install.sh: Anlage-Befehl fuer virt-install auf dem Host
  (q35, SeaBIOS-Import, Device-Minimierung, Serial-Only).
- vm/domain-xml-target.xml: Soll-Zustand der Device-/Feature-Sektion der
  Domain-XML (Pruefung/Nachzug per virsh edit vibe-sandbox).

## Platzhalter vor dem ersten Boot ersetzen

- <agent-sandbox-relay-worker-token> in cloud-init und files/agent-sandbox-agent:
  Worker-Token der VM (Rolle worker). Erstellung ueber die
  Verwaltungsoberflaeche des Relays (GET /admin, Login mit
  AGENT_SANDBOX_ADMIN_USERNAME und AGENT_SANDBOX_ADMIN_PASSWORD; das
  Token wird nur einmal bei der Erstellung angezeigt, Namen sind optional
  und nachtraeglich umbenennbar). Solange keine echten Tokens existieren,
  arbeitet das Legacy-Token aus AGENT_SANDBOX_RELAY_TOKEN im Docker-Stack
  als Worker- und Submit-Token weiter (Migration beim Serverstart).
- <agent-sandbox-relay-domain> in cloud-init und files/agent-sandbox-agent:
  Subdomain des Queue-Services (ENDPOINT des Agenten).
- <ttyd-passwort> in cloud-init und files/ttyd-vibe.service: Basic-Auth-
  Passwort des Web-Terminals.
- <vm-admin-passwort> in cloud-init (nur runcmd): Anfangs-Passwort der
  VM-Verwaltung (Login "admin"). In der VM liegt nur der sha256-Hash
  (/home/vibe/.config/agent-sandbox/admin-hash, 0600, owner vibe);
  aenderbar ueber die UI. Das Worker-Token braucht keinen Platzhalter
  mehr: der Agent liest es aus worker-token, falls die Datei existiert
  (sonst greift die Konstante als Fallback).

## Dateitransfer

- VM -> Host manuell: im ttyd-Web-Terminal lrzsz-sz <datei> (Browser bietet
  Download an; ZMODEM per -t enableZmodem=true aktiviert).
- Programmatisch (beide Richtungen) ueber die Files-Endpunkte des Relays
  (je Datei maximal 8 MiB roh; der Server speichert gzip+base64-kodiert
  und wandelt beim Auslesen in Rohbytes zurueck):
  - VM -> Absender: Job cmd "VIBE_SEND <pfad>"; das Result enthaelt
    "VIBE_SEND id=<id>"; der Absender holt die Rohbytes mit
    GET /files/<id>.
  - Absender -> VM: POST /files mit Rohbytes liefert die id; danach Job
    cmd "VIBE_FETCH <id> <zielpfad>".
- Fallback ohne Datei-Endpunkte: Job cmd "base64 -w0 <datei>" (oder
  "gzip -c <datei> | base64 -w0"), Output im Result ist der Base64-Text
  (Deckel 8 MiB).
