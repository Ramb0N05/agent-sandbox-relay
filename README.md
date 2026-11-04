# agent-sandbox-relay - Branch worker (VM-Seite)

Dieser Branch enthält die VM-seitigen Dateien der sandbox-worker-VM
(Canonical-Doku: Canvas "vibe-sandbox-vm"). Branch main enthält die
Server-Seite (Queue-Service agent-sandbox-relay: Dockerfile,
agent_sandbox_relay.py, README.md). Der Branch enthält nur die
VM-seitigen Dateien; die beim Anlegen aus main geerbten Dateien
(Dockerfile, agent_sandbox_relay.py) sind gelöscht.

## Inhalt

- cloud-init/sandbox-worker-user-data.yaml: komplette cloud-init user-data
  (User worker mit NOPASSWD-sudo, Pakete, write_files, runcmd). Ohne SSH:
  ssh_genkeytypes: [] verhindert die Host-Key-Generierung durch das
  cc_ssh-Modul (leere Liste statt Default rsa/ecdsa/ed25519), runcmd
  deinstalliert openssh per pacman -Rns --noconfirm und löscht die
  generierten Host-Keys (das cloud-init packages-Modul kann nur
  installieren, keine Pakete entfernen). Auf dem Host
  unter /root/sandbox-worker-user-data.yaml ablegen; virt-install hängt sie
  als NoCloud-ISO nur beim ersten Boot an.
- files/agent-sandbox-agent: Pull-Agent (elfte Runde: Python statt bash;
  Long-Poll gegen die Queue mit persistenter HTTP-Verbindung und einem
  Reconnect-Retry je Aufruf statt einem curl-Prozess je Request, kein
  Leerlauf-Sleep - /jobs/next blockiert serverseitig, sleep 1 nur im
  Fehlerfall; Ausführung mit timeout 900 bash -lc als User worker,
  Präfixe VIBE_FETCH und VIBE_SEND für den Datei-Transport über die
  Files-Endpunkte; ssl-Default-Kontext mit Zertifikatsprüfung).
  Installationsziel in der VM: /usr/local/bin/agent-sandbox-agent, owner
  worker:worker, 0700. Normalfall ist die Provisionierung über die
  cloud-init-Datei (Paket python3 in der Paketliste); die Einzeldatei ist
  die Referenzkopie.
- files/agent-sandbox-agent.service: systemd-Unit des Pull-Agents
  (Restart=always), Ziel /etc/systemd/system/.
- files/ttyd-worker.service: systemd-Unit des Web-Terminals (nur vom Host
  erreichbar, Basic Auth, max. 2 Clients, ZMODEM aktiviert), Ziel
  /etc/systemd/system/.
- files/vmadmin: Verwaltungs-UI der VM auf Port 80 (fünfzehnte Runde,
  Stil wie die Relay-Verwaltung: Dark-Theme, Hash-Navigation, native
  dialog-Modals, CSP-Nonce, Login-Card mit sessionStorage, Auto-Refresh,
  401 ohne WWW-Authenticate-Challenge). Nur Python-stdlib (http.server,
  ThreadingHTTPServer); Port 80 als User worker läuft dank
  AmbientCapabilities=CAP_NET_BIND_SERVICE in der Unit (Ports unter 1024
  brauchen sonst root; CapabilityBoundingSet schneidet weitere Caps ab,
  NoNewPrivileges entfällt, weil vmadmin sudo für systemctl/journalctl
  benutzt). Funktionen: Status (Hostname, Kernel, Uptime, Last, RAM- und
  Disk-Belegung als Balken, Dienst-Zustände, Token-Maske), Worker-Token
  setzen (8-128 ASCII ohne Whitespace, atomar nach
  /home/worker/.config/agent-sandbox/worker-token, danach Neustart des
  Agents, weil der das Token nur beim Start liest), ttyd-Passwort setzen
  (1-64 druckbare ASCII ohne Doppelpunkt und Whitespace - ttyd kennt nur
  -c user:password und kein Credential-File; die Unit wird neu geschrieben,
  daemon-reload und Neustart), eigenes Admin-Passwort ändern (sha256-Hash
  in /home/worker/.config/agent-sandbox/admin-hash; der Hash liegt nur als
  sha256 in der Datei), Dienst-Neustarts und das Agent-Journal
  (journalctl -n 30). Login-Lockout wie im Relay (5 Fehlversuche, 300 s,
  Verdopplung bis 3600 s, 429 mit Retry-After). Installationsziel
  /usr/local/bin/vmadmin, owner worker:worker, 0700.
- files/vm-admin.service: systemd-Unit der Verwaltung (User worker,
  Restart=always, AmbientCapabilities=CAP_NET_BIND_SERVICE,
  CapabilityBoundingSet=CAP_NET_BIND_SERVICE), Ziel
  /etc/systemd/system/.
- vm/virt-install.sh: Anlage-Befehl für virt-install auf dem Host
  (q35, SeaBIOS-Import, Device-Minimierung, Serial-Only).
- vm/domain-xml-target.xml: Soll-Zustand der Device-/Feature-Sektion der
  Domain-XML (Prüfung/Nachzug per virsh edit sandbox-worker).
- vm/vm-install.sh: Installations- und Update-Skript für den Host
  (neunzehnte Runde). Install: holt den aktuellen worker-Zweig per
  raw.githubusercontent, ersetzt die drei user-data-Platzhalter, generiert
  fehlende Passwörter aus /dev/urandom (24 alphanumerische Zeichen,
  random(4)), legt die VM per virt-install an (identisch zu
  vm/virt-install.sh, zusätzlich --noautoconsole, damit das Skript
  zurückkehrt) und wartet auf die DHCP-Lease (virsh net-dhcp-leases mit
  der MAC aus domiflist). Update: VM-Daten bleiben unberührt, Dateien
  wandern über die Job-Queue (fünfzehnte Runde) in die VM; ohne
  erreichbaren Agent druckt das Skript die manuelle serielle Prozedur.
  Details und Parameter: Abschnitt "VM anlegen oder aktualisieren" unten.

## Platzhalter vor dem ersten Boot ersetzen

- <agent-sandbox-relay-worker-token> in cloud-init und files/agent-sandbox-agent:
  Worker-Token der VM (Rolle worker). Erstellung über die
  Verwaltungsoberfläche des Relays (GET /admin, Login mit
  AGENT_SANDBOX_ADMIN_USERNAME und AGENT_SANDBOX_ADMIN_PASSWORD; das
  Token wird nur einmal bei der Erstellung angezeigt, Namen sind optional
  und nachträglich umbenennbar). Solange keine echten Tokens existieren,
  arbeitet das Legacy-Token aus AGENT_SANDBOX_RELAY_TOKEN im Docker-Stack
  als Worker- und Submit-Token weiter (Migration beim Serverstart).
- <agent-sandbox-relay-domain> in cloud-init und files/agent-sandbox-agent:
  Subdomain des Queue-Services (ENDPOINT des Agenten).
- <ttyd-passwort> in cloud-init und files/ttyd-worker.service: Basic-Auth-
  Passwort des Web-Terminals.
- <vm-admin-passwort> in cloud-init (nur runcmd): Anfangs-Passwort der
  VM-Verwaltung (Login "admin"). In der VM liegt nur der sha256-Hash
  (/home/worker/.config/agent-sandbox/admin-hash, 0600, owner worker);
  änderbar über die UI. Das Worker-Token braucht keinen Platzhalter
  mehr: der Agent liest es aus worker-token, falls die Datei existiert
  (sonst greift die Konstante als Fallback).

## Dateitransfer

- VM -> Host manuell: im ttyd-Web-Terminal lrzsz-sz <datei> (Browser bietet
  Download an; ZMODEM per -t enableZmodem=true aktiviert).
- Programmatisch (beide Richtungen) über die Files-Endpunkte des Relays
  (je Datei maximal 8 MiB roh; der Server speichert gzip+base64-kodiert
  und wandelt beim Auslesen in Rohbytes zurück):
  - VM -> Absender: Job cmd "VIBE_SEND <pfad>"; das Result enthält
    "VIBE_SEND id=<id>"; der Absender holt die Rohbytes mit
    GET /files/<id>.
  - Absender -> VM: POST /files mit Rohbytes liefert die id; danach Job
    cmd "VIBE_FETCH <id> <zielpfad>".
- Fallback ohne Datei-Endpunkte: Job cmd "base64 -w0 <datei>" (oder
  "gzip -c <datei> | base64 -w0"), Output im Result ist der Base64-Text
  (Deckel 8 MiB).

## VM anlegen oder aktualisieren (vm/vm-install.sh, zwanzigste Runde)

Alle Pfade sind Parameter mit dokumentierten Defaults, kein Projektpfad
im Skript: --vm-name (sandbox-worker), --connect (qemu:///system), --disk
(/var/lib/libvirt/images/sandbox-worker.qcow2 - Standardverzeichnis des
libvirt-Default-Pools, virtinst _preferred_default_pool_path), --backing
(/var/lib/libvirt/images/Arch-Linux-x86_64-cloudimg.qcow2 - Arch-Cloud-
Image im Standardverzeichnis, realen Pfad bei Bedarf angeben; muss für
den QEMU-Laufzeitbenutzer lesbar sein), --disk-gb (64), --memory (8192),
--vcpus (4), --user-data (leer - die gerenderte user-data liegt nur
temporär im Skript-Tempverzeichnis, nichts unter /root; mit Angabe wird
eine zusätzliche dauerhafte Kopie unter dem Pfad abgelegt), --raw-base
(raw.githubusercontent.com, Branch worker). Weitere Optionen: --domain
(Relay-Endpoint), --worker-token, --ttyd-password, --admin-password,
--relay, --submitter-token, --recreate, --yes, --dry-run.

Ohne Root lauffähig: Der Zugang läuft über den libvirt-Socket (Mitglieder
der Gruppe libvirt haben passwortlosen Zugriff auf den RW-Socket, Arch
Wiki "Libvirt"); das Overlay wird über die Storage-Pool-API angelegt
(virtinst build_vol_install mit backing_store, die Datei erzeugt libvirtd
im Pool), das Cloud-init-Seed-ISO baut virt-install bei fehlendem
Schreibzugriff auf /var/lib/libvirt/boot im eigenen Nutzer-Cache
(virtinst installertreemedia.py make_scratchdir, Mode 0751). Custom-Pfade
für --disk: das Verzeichnis muss ein bekannter libvirt-Storage-Pool sein
oder das Overlay muss existieren (virt-install legt Volumes nur über
Pools an). Overlay-Löschung bei --recreate über virsh vol-delete
(vol-name-or-key-or-path, klappt ohne Root), mit rm als Fallback.

Installation (VM existiert nicht): fetch der user-data, des Agenten, von
vmadmin und der Units vom aktuellen worker-Zweig; Ersetzen von
<agent-sandbox-relay-domain>, <ttyd-passwort> und <vm-admin-passwort>
(ohne Angabe generiert, 24 alphanumerische Zeichen aus /dev/urandom).
Danach virt-install (plus --noautoconsole, virt-install(1)), Warteschleife
bis 120 s auf die DHCP-Lease (virsh net-dhcp-leases default <mac>; die
Lease-Tabelle druckt nach dem Header eine Trennlinie, das Skript matcht
die MAC-Zeile statt eine Zeilennummer zu raten - libvirt vsh-table.c) und
Ausgabe der Initialwerte in der Shell (IP, Passwörter, Konsole-Hinweis,
Autologin worker). Ohne --worker-token läuft der Agent mit dem
Konstanten-Fallback (401 auf der Queue); das Token wird nach dem Boot
über die vmadmin-UI gesetzt (Datei geht der Konstante vor).

Update (VM existiert, ohne --recreate): keine VM-Daten werden verändert.
Voraussetzungen: --domain, falls der Agent noch den Platzhalter enthält
(Fehler der fünfzehnten Runde, wird dabei behoben), außerdem --relay und
--submitter-token für den Queue-Weg (das Skript ist Absender: POST /jobs
und POST /files gehören zur Submitter-Rolle; das Worker-Token gehört zum
Agenten in der VM und verlässt sie nie). Ablauf: Lebensprobe (echo
vm-install-probe), Prüfung des VM-Benutzers (Job id -un muss worker
liefern - eine VM der alten Bereitstellung wird abgewiesen), dann je
Datei POST /files -> Job VIBE_FETCH nach /home/worker/.vm-update/ ->
sudo install (vmadmin und Agent als worker:worker 0700, Units als
root:root 0644, den Defaults von cloud-init write_files entsprechend),
daemon-reload, vmadmin-Neustart; die ttyd-Unit wird nur mit
--ttyd-password angetastet (das Passwort steckt in ExecStart), der
Agent-Restart läuft als letzter Job (nohup mit sleep 3, damit das Result
noch gepostet wird). Worker-Token-Datei, Admin-Hash und ttyd-Passwort
bleiben unberührt. Beantwortet der Agent keine Jobs, druckt das Skript
die manuelle Prozedur für die serielle Konsole (virsh console, Autologin
worker, Fetch direkt vom öffentlichen Repo) und endet mit Exit-Code 3;
ohne --relay/--submitter-token endet es mit Exit-Code 2.

--recreate: verwirft die Domain (virsh undefine) und löscht das Overlay.
DAS LÖSCHT ALLE DATEN DER VM (cloud-init läuft pro Overlay nur einmal,
per-instance); Bestätigung mit dem Wort "ja" (oder --yes), danach wie
Installation. --dry-run testet fetch und Ersetzung ohne libvirt (auch
ohne virsh/virt-install lauffähig) und legt nichts an. Eine bestehende
VM nur umbenennen (ohne Neu-Anlage): virsh domrename - nur im
ausgeschalteten Zustand (virsh(1)); der Benutzername steht in der
cloud-init und ändert sich nur bei einer Neu-Anlage.
