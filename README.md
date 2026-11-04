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
- files/ttyd-worker.service: systemd-Unit des Web-Terminals (Port 7681, nur vom Host
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
  aktiv); ttyd laeuft ohne eigenes -c-Credential (31. Runde: kein eigenes
  ttyd-Passwort mehr - das Terminal ist in die vmadmin-UI eingebettet und
  der Zugriff ist auf den Host beschraenkt, libvirt-NAT ohne DNAT)
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
  (zweiunddreißigste Runde). Install: holt den aktuellen worker-Zweig per
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
  und die Seed-vdb abgehängt (live+config). 27. Runde: LC_ALL=C für alle
  Host-Tools (virsh-Ausgabe ist gettext-übersetzt, z.B. domstate "laufend"
  statt "running" unter de_DE); --recreate macht jetzt virsh destroy VOR undefine
  (undefine einer laufenden Domain macht sie nur transient und stoppt sie nicht -
  die Disk blieb danach "in use" und virt-install brach mit path_in_use ab);
  Self-Update arbeitet mit dem absoluten Skript-Pfad. 28. Runde: Self-Update ist
  Standard (nur --no-self-update schaltet ab); Konsolenauswertung liest eine
  CR-bereinigte Kopie (serielle Konsole sendet CRLF, termios ONLCR - sonst matcht
  ^active$ nie) und console_run kehrt sofort zurück, sobald der RC-Marker
  im Output steht (statt die volle Wartezeit zu schlafen); boot_id der ersten
  Phase wird als ERSTES gelesen, damit ein Neustart waehrend der cloud-init-
  Phase nicht als "noch kein Neustart" fehlinterpretiert wird. 29. Runde:
  Reboot-Erkennung nochmal robust: boot_id wird SOFORT beim ersten Konsolen-
  kontakt gelesen (Phase 1a, noch vor cloud-init-done), damit boot1 sicher die
  erste Boot-Phase ist; als zweites Signal gilt ein Uptime-Abfall gegen den
  Stand bei cloud-init-done (/proc/uptime, proc(5)); dazu frueherkennung per
  marker=[0-9] (das tty-Echo der Marker-Zeile enthaelt marker= ohne Ziffer und
  darf den Feeder nicht frueh beenden). 30. Runde: Readiness-Wait pollt statt der
  seriellen Konsole die vmadmin-API in der VM (GET /admin/api/state mit Basic-Auth;
  read_state liefert jetzt boot_id, cloud_init, system_running, vm_admin) - der Host
  erreicht die VM-IP direkt über virbr0 (libvirt-default-NAT). Grund: die serielle
  Konsole ist eine EINZIGE Session; eine parallel geöffnete virt-manager-Konsole
  macht virsh console kaputt ("Active console session exists for this domain" /
  gegenseitiges Kicken per --force). Konsole bleibt nur Fallback und für den Update-
  Pfad. Image-Pfade kommen aus der libvirt-Konfig
  (virsh pool-dumpxml default -> <target><path>), nicht aus einem
  hartkodierten Pfad. Update: ausschließlich über die serielle Konsole,
  KEIN Relay-API-Zugriff (keine POST /jobs, keine /files); die VM lädt
  die Dateien selbst aus dem öffentlichen Repo nach /tmp/vm-update,
  gerenderte Dateien kommen als base64-Payload an die vmadmin-API (POST
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
--worker-token, --admin-password, --no-self-update,
--recreate, --yes, --dry-run. --relay/--submitter-token gibt es nicht mehr:
das Skript spricht die Relay-API an keiner Stelle an.

Ohne Root läuft es wie gehabt: libvirt-Socket (Gruppe libvirt, Arch
Wiki), Overlay über die Storage-Pool-API (virtinst build_vol_install mit
backing_store), Seed-ISO im virtinst-Nutzer-Scratchdir (make_scratchdir,
0751), Custom-Pfade als automatisch verwalteter dir-Pool (diskbackend.py
manage_path), Overlay-Löschung über virsh vol-delete.

Installation (VM existiert nicht): fetch der user-data, des Agenten, von
vmadmin und der Units vom aktuellen worker-Zweig (nur temporär); Ersetzen
von <agent-sandbox-relay-domain> (nur mit --domain) und
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
gerenderten Passwörter). Danach wartet das Skript ausschließlich über die vmadmin-API in der VM
(keine serielle Konsole - 31. Runde; die Konsole ist eine einzige Session und
eine parallel geöffnete virt-manager-Ansicht stoßert: "Active console
session exists for this domain", virsh(1)): IP per DHCP-Lease (net-dhcp-leases,
MAC-Zeile matchen - vsh-table.c), dann Poll von GET /admin/api/state (Basic
admin) im 10-s-Takt. vmadmin read_state liefert boot_id, cloud_init,
system_running, uptime_s und die Unit-States. Erkannt wird der einmalige
power_state-Neustart am Wechsel von /proc/sys/kernel/random/boot_id (Kernel-
sysctl-Doku: ändert sich genau einmal pro Boot) oder am Uptime-Abfall
(/proc/uptime, proc(5)); zusätzlich Safety-Net nach 240 s. Fertig, wenn
cloud-init "status: done" (cloud-init CLI-Referenz, Exit 0/1/2) UND agent,
vm-admin, ttyd aktiv UND system running/degraded. power_state timeout ist
auf 60 s gesetzt (cc_power_state_change run_after_pid_gone führt die Aktion
auch nach timeout aus - sonst erst nach 300 s). Erst dann Seed-vdb detach
(vorher schon) und Initialwerte (IP, Admin-Passwort, Endpoint, ttyd-URL).

Update (VM existiert, ohne --recreate): ausschließlich über die vmadmin-API,
keine serielle Konsole und keine Relay-API-Zugriffe (kein POST /jobs, kein
/files, keine Token). Ablauf: Status per GET /admin/api/state lesen (daraus
ohne --domain den laufenden Endpoint übernehmen); Agent mit Endpoint
hostseitig rendern; dann POST /admin/api/update mit den Dateien base64
(agent, vmadmin, 2 Units) - vmadmin legt sie in /tmp/vm-update (temporaer),
installiert per sudo install (agent/vmadmin worker:worker 0700, Units
root:root 0644), daemon-reload, restart vm-admin.service und danach
agent-sandbox-agent.service. Verifikation per state.agent. Worker-Token-
Datei und Admin-Hash bleiben unberührt; die ttyd-Unit wird nicht mehr
angefasst (kein ttyd-Passwort mehr - ttyd läuft ohne -c und ist in die
vmadmin-UI als Terminal-Seite (iframe http://<host>:7681/, CSP frame-src)
eingebettet). 32. Runde: (a) vm-admin Bounding-Set enthaelt CAP_SETUID/SETGID/
  CHOWN/DAC_OVERRIDE/FOWNER, damit sudo in der Unit wieder klappt (systemd.exec(5):
  Bounding-Set begrenzt, was setuid-Kinder wie sudo gewinnen duerfen);
  (b) ttyd lauscht nur auf 127.0.0.1:7681 mit -b /terminal und ist ausschliesslich
  ueber den authentifizierten vmadmin-Proxy /terminal/* erreichbar (WS /terminal/ws
  wird raw durchgereicht; iframe same-origin, CSP frame-src 'self'); (c) PAGES um
  "terminal" ergaenzt (sonst sprang die Terminal-Seite auf Status) und Seiten ohne
  Login nicht mehr aufrufbar (nur Login-Seite, Nav versteckt); (d) ttyd-Unit mit
  Restart=always + StartLimitIntervalSec=0.

Self-Update: vor jedem echten Lauf vergleicht das Skript sha256($0) mit der
Repo-Kopie unter $RAW_BASE/vm/vm-install.sh (raw.githubusercontent.com liefert
Rohbytes; GitHub REST Contents API: download_url = raw.githubusercontent.com,
docs.github.com/en/rest/repos/contents). Bei Abweichung wird $0
automatisch durch die Repo-Version ersetzt (Backup $0.bak-<ts>, Re-Start mit
Original-Argumenten); Abschalten mit --no-self-update.

--recreate: verwirft die Domain (virsh undefine) und löscht das Overlay.
DAS LÖSCHT ALLE DATEN DER VM (cloud-init läuft pro Overlay nur einmal,
per-instance); Bestätigung mit dem Wort "ja" (oder --yes), danach wie
Installation. --dry-run testet fetch und Ersetzung ohne libvirt und ohne
Konsolenzugriff und legt nichts an. Eine bestehende VM nur umbenennen:
virsh domrename - nur im ausgeschalteten Zustand (virsh(1)); der
Benutzername steht in der cloud-init und ändert sich nur bei einer
Neu-Anlage.
