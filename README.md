# agent-sandbox-relay - Branch worker (VM-Seite)

Dieser Branch enthält die VM-seitigen Dateien der sandbox-worker-VM
(Canonical-Doku: Canvas "agent-sandbox-relay"). Branch main enthält die
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
  installieren, keine Pakete entfernen). power_state (mode: reboot,
  delay: now) startet die VM nach Abschluss aller cloud-init-Module
  einmal selbst neu (cc_power_state_change, Frequenz once-per-instance,
  deshalb kein Reboot bei jedem Boot). vm/vm-install.sh rendert sie
  beim Anlegen nur temporär (Abschnitt unten); virt-install hängt sie
  als NoCloud-ISO nur beim ersten Boot an.
- files/agent-sandbox-agent: Pull-Agent (elfte Runde: Python statt bash;
  Long-Poll gegen die Queue mit persistenter HTTP-Verbindung und einem
  Reconnect-Retry je Aufruf statt einem curl-Prozess je Request, kein
  Leerlauf-Sleep - /jobs/next blockiert serverseitig, sleep 1 nur im
  Fehlerfall; Ausführung mit timeout 900 bash -lc als User worker,
  Präfixe FETCH und SEND für den Datei-Transport über die
  Files-Endpunkte (einundzwanzigste Runde: vorher VIBE_FETCH/VIBE_SEND,
  der Server behandelt cmd als opak); ssl-Default-Kontext mit
  Zertifikatsprüfung; Token und Endpoint liest der Agent beim Start aus
  Dateien (/home/worker/.config/agent-sandbox/worker-token bzw. endpoint,
  schreibbar über die vmadmin-UI), Fallback sind die Platzhalter-
  Konstanten).
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
  Disk-Belegung als Balken, Dienst-Zustände, Token-Maske, Endpoint),
  Worker-Token setzen (8-128 ASCII ohne Whitespace, atomar nach
  /home/worker/.config/agent-sandbox/worker-token, danach Neustart des
  Agents, weil der das Token nur beim Start liest), Relay-Endpoint setzen
  (https-URL mit Host, 8-200 ASCII-Zeichen ohne Whitespace, atomar nach
  /home/worker/.config/agent-sandbox/endpoint, danach Neustart des
  Agents; ohne Datei bleibt der Platzhalter aus der cloud-init-Konfiguration
  aktiv), ttyd-Passwort setzen
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
  (sechsundzwanzigste Runde). Install: holt den aktuellen worker-Zweig per
  raw.githubusercontent (nur temporär in mktemp -d, per trap entfernt; dauerhaft
  bleiben nur die Images), ersetzt die drei user-data-Platzhalter,
  generiert fehlende Passwörter aus /dev/urandom (24 alphanumerische
  Zeichen, random(4)), legt die VM per virt-install an (identisch zu
  vm/virt-install.sh, zusätzlich --noautoconsole und --console
  pty,target_type=serial für die serielle Konsole), baut eine NoCloud-
  Seed-ISO (volid cidata + user-data + meta-data, genisoimage/xorriso, nur
  temporär) und startet virt-install EINPHASIG (--import + Seed als vdb,
  --events on_reboot=restart,on_poweroff=destroy,on_crash=restart - sonst
  zerstört libvirt die VM beim power_state-Reboot, weil virt-install bei
  --cloud-init on_reboot=destroy in die Live-XML schreibt, virtinst
  installer.py _alter_bootconfig) und wartet über die serielle Konsole
  (virsh console --force --safe, Autologin worker per agetty --autologin auf
  serial-getty@ttyS0), bis cloud-init fertig ist (cloud-init status --wait)
  UND der einmalige power_state-Neustart durch ist (boot_id-Wechsel); erst
  wenn agent/vm-admin/ttyd aktiv sind, werden die Initialwerte ausgegeben
  und die Seed-vdb abgehängt (live+config). Image-Pfade kommen aus der libvirt-Konfig
  (virsh pool-dumpxml default -> <target><path>), nicht aus einem
  hartkodierten Pfad. Update: ausschließlich über die serielle Konsole,
  KEIN Relay-API-Zugriff (keine POST /jobs, keine /files); die VM lädt
  die Dateien selbst aus dem öffentlichen Repo nach /tmp/vm-update,
  gerenderte Dateien (Agent, ttyd-Unit) kommen als base64-Heredoc über
  die Konsole. Self-Update: sha256-Vergleich mit vm/vm-install.sh im Repo
  (--self-update ersetzt $0, Backup inklusive). Details und Parameter:
  Abschnitt "VM anlegen oder aktualisieren" unten.

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
  Subdomain des Queue-Services (ENDPOINT des Agenten). Seit der
  einundzwanzigsten Runde optional: ohne Ersetzung bleibt der Platzhalter
  als Fallback-Konstante, das echte Endpoint wird nach dem Boot über die
  vmadmin-UI gesetzt (Datei geht der Konstante vor).
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
  - VM -> Absender: Job cmd "SEND <pfad>"; das Result enthält
    "SEND id=<id>"; der Absender holt die Rohbytes mit
    GET /files/<id>.
  - Absender -> VM: POST /files mit Rohbytes liefert die id; danach Job
    cmd "FETCH <id> <zielpfad>".
- Fallback ohne Datei-Endpunkte: Job cmd "base64 -w0 <datei>" (oder
  "gzip -c <datei> | base64 -w0"), Output im Result ist der Base64-Text
  (Deckel 8 MiB).

## VM anlegen oder aktualisieren (vm/vm-install.sh, fünfundzwanzigste Runde)

Alle Pfade sind Parameter; die Image-Pfade kommen jetzt aus der
libvirt-Konfiguration statt aus einem hartkodierten Default: ohne --disk/
--backing liest das Skript den Default-Pool zur Laufzeit aus
`virsh --connect <URI> pool-dumpxml default` (<target><path> in der
Pool-XML, libvirt formatstorage.html; persistente Definition
/etc/libvirt/storage/default.xml) und setzt <pool>/sandbox-worker.qcow2
bzw. <pool>/Arch-Linux-x86_64-cloudimg.qcow2. Im Dry-Run ohne erreichbares
libvirt greift der dokumentierte Standard /var/lib/libvirt/images
(virtinst _preferred_default_pool_path). Weitere Optionen: --vm-name
(sandbox-worker), --connect (qemu:///system), --disk-gb (64), --memory
(8192), --vcpus (4), --user-data (leer - die gerenderte user-data und alle
heruntergeladenen worker-Dateien liegen nur temporär in mktemp -d und
werden per trap entfernt; dauerhaft bleiben nur die Images; mit Angabe
wird eine zusätzliche dauerhafte Kopie unter dem Pfad abgelegt),
--raw-base (raw.githubusercontent.com, Branch worker), --domain (optional),
--worker-token, --ttyd-password, --admin-password, --self-update,
--recreate, --yes, --dry-run. --relay/--submitter-token gibt es nicht mehr:
das Skript spricht die Relay-API an keiner Stelle an.

Ohne Root läuft es wie gehabt: libvirt-Socket (Gruppe libvirt, Arch
Wiki), Overlay über die Storage-Pool-API (virtinst build_vol_install mit
backing_store), Seed-ISO im virtinst-Nutzer-Scratchdir (make_scratchdir,
0751), Custom-Pfade als automatisch verwalteter dir-Pool (diskbackend.py
manage_path), Overlay-Löschung über virsh vol-delete.

Installation (VM existiert nicht): fetch der user-data, des Agenten, von
vmadmin und der Units vom aktuellen worker-Zweig (nur temporär); Ersetzen
von <agent-sandbox-relay-domain> (nur mit --domain), <ttyd-passwort> und
<vm-admin-passwort> (sonst generiert aus /dev/urandom, random(4)). Danach
wird die Seed-ISO lokal gebaut (genisoimage -output seed.iso -volid cidata
-joliet -rock user-data meta-data; NoCloud-Doku: Volume-Label cidata mit
den Dateien user-data und meta-data) und virt-install gestartet
(--import + Seed als zweite readonly virtio-Disk vdb, zusätzlich
--noautoconsole, --console pty,target_type=serial und --events
on_reboot=restart,on_poweroff=destroy,on_crash=restart; virt-install(1)
--events, libvirt formatdomain.html on_*; Soll-XML inkl. on_* in
vm/domain-xml-target.xml). Einphasig, damit Live- == persistente XML ist:
bei --cloud-init (Zweiphasen-Install) setzt virtinst in der initialen
Live-XML on_reboot=destroy (virtinst/install/installer.py
_alter_bootconfig), sodass der power_state-Reboot die VM zerstört hat
(beobachtet in der 26. Runde). Nach dem Warten wird die Seed-vdb per
virsh detach-disk --live --config entfernt (die temp-ISO enthält die
gerenderten Passwörter). Die VM bootet serial-getty@ttyS0 mit agetty
--autologin worker (user-data), die Konsole loggt also von selbst als
worker ein (agetty(8) --autologin). Dann wartet das Skript über die
Konsole: erst `cloud-init status --wait` (blockiert bis cloud-init fertig;
Exit 0 ok / 1 Crash / 2 behebbare Fehler - cloud-init CLI-Referenz) bzw.
"status: done", dann der einmalige power_state-Neustart (Modul läuft once
per instance nach allen anderen Config-Modulen - cloud-init Modul-
Referenz; erkennbar am Wechsel von /proc/sys/kernel/random/boot_id, das
sich genau einmal pro Boot ändert - Kernel-sysctl-Doku), dann nach dem
Neustart erneut cloud-init done plus `systemctl is-active
agent-sandbox-agent vm-admin ttyd-worker` und `systemctl is-system-running`
(running/degraded). Erst wenn die VM so vollständig einsatzbereit ist,
kommen DHCP-Lease (net-dhcp-leases; MAC-Zeile matchen statt Zeilennummer -
vsh-table.c) und die Initialwerte (IP, Passwörter, Endpoint, Konsole).
cloud-init legt /var/lib/cloud/instance/boot-finished als letzte Datei an
(cloud-init internal files).

Update (VM existiert, ohne --recreate): ausschließlich über die serielle
Konsole, keine Relay-API-Zugriffe (kein POST /jobs, kein /files, keine
Token): `virsh console --force --safe` (--force trennt verwaiste Sitzungen,
--safe erzwingt exclusive access - virsh(1)). Ablauf: Benutzer prüfen
(id -un muss worker liefern, sonst --recreate-Hinweis); ohne --domain den
laufenden Endpoint aus der VM lesen (Endpoint-Datei vor ENDPOINT-Konstante
des installierten Agenten); dann lädt die VM die ungeänderten Dateien
selbst aus dem öffentlichen Repo nach /tmp/vm-update (curl ist per
cloud-init installiert, packages in der user-data); gerenderte Dateien
(Agent mit Endpoint, ttyd-Unit nur mit --ttyd-password wegen ExecStart)
kommen als base64-Heredoc über die Konsole (Zeilen <= 76 Zeichen mit
base64(1) -w 76, unter der kanonischen Zeilenlänge des tty); dann sudo
install (vmadmin und Agent worker:worker 0700, Units root:root 0644 wie
die cloud-init write_files-Defaults), daemon-reload, vm-admin- und
ttyd-Neustart, Agent-Restart zuletzt verzögert über einen transienten
Timer (systemd-run --on-active=3s, systemd-run(1)), Verifikation per
systemctl is-active. Worker-Token-Datei, Admin-Hash und ttyd-Passwort
bleiben unberührt.

Self-Update: vor jedem echten Lauf vergleicht das Skript sha256($0) mit der
Repo-Kopie unter $RAW_BASE/vm/vm-install.sh (raw.githubusercontent.com liefert
Rohbytes; GitHub REST Contents API: download_url = raw.githubusercontent.com,
docs.github.com/en/rest/repos/contents). Bei Abweichung nur Hinweis; mit
--self-update wird $0 durch die Repo-Version ersetzt (Backup $0.bak-<ts>)
und das Skript startet mit den Original-Argumenten neu.

--recreate: verwirft die Domain (virsh undefine) und löscht das Overlay.
DAS LÖSCHT ALLE DATEN DER VM (cloud-init läuft pro Overlay nur einmal,
per-instance); Bestätigung mit dem Wort "ja" (oder --yes), danach wie
Installation. --dry-run testet fetch und Ersetzung ohne libvirt und ohne
Konsolenzugriff und legt nichts an. Eine bestehende VM nur umbenennen:
virsh domrename - nur im ausgeschalteten Zustand (virsh(1)); der
Benutzername steht in der cloud-init und ändert sich nur bei einer
Neu-Anlage.
