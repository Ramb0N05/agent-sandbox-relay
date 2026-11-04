#!/bin/bash
# vm-install.sh - install or update the sandbox-worker VM on the local libvirt host (25th round).
# Install (VM does not exist): fetches the current worker-branch files from the public
# GitHub repo into a temporary directory (mktemp -d, removed via trap on exit - only the
# VM images persist on the host), replaces the user-data placeholders, generates the
# initial passwords, renders the user-data, builds a NoCloud seed ISO (volid cidata +
# user-data + meta-data, genisoimage/xorriso) in the temp dir, runs virt-install in a
# SINGLE phase (--import + seed disk, no --cloud-init) with
# --events on_reboot=restart,on_poweroff=destroy,on_crash=restart and then WAITS over the
# serial console until cloud-init finished AND the first power_state reboot happened;
# only then the initial values are printed (VM fully ready) and the seed disk is detached
# again (live+config). --domain is optional: without it the endpoint placeholder stays
# and the relay endpoint is set after the boot via the vmadmin UI (agent reads the
# endpoint file first).
# Why no --cloud-init: with --cloud-init virt-install is a TWO-phase install
# (virtinst install/installer.py: requires_postboot_xml_changes() is True when
# has_cloudinit(), and _alter_bootconfig sets on_reboot="destroy" on the LIVE initial
# XML; _create_guest starts the live domain with that initial XML). The power_state
# reboot then destroyed the VM (observed in the 26th round). Single-phase --import keeps
# live == persistent == final XML with our --events values (virt-install(1) --events).
# Update (VM exists): exclusively over the VM serial console (virsh console --force
# --safe, autologin worker via serial-getty@ttyS0 with agetty --autologin, agetty(8));
# the VM downloads the current files itself from the public repo into /tmp/vm-update
# (temporary), rendered files (agent with endpoint, ttyd unit) go through the console as
# a base64 heredoc. NO relay API access anywhere in this script (no POST /jobs, no /files).
# Self-update: the script compares its own sha256 with the repo copy
# ($RAW_BASE/vm/vm-install.sh; raw.githubusercontent.com serves raw file bytes, GitHub REST
# contents API download_url) and can replace itself (--self-update, backup included).
# Image paths default to the libvirt default pool directory, read at runtime from the
# libvirt config (virsh pool-dumpxml default -> <target><path>, libvirt formatstorage.html;
# persistent definition /etc/libvirt/storage/default.xml). The documented standard
# /var/lib/libvirt/images (virtinst _preferred_default_pool_path) is only the dry-run
# fallback when no libvirt is reachable.
# No root and no sudo needed on the host: libvirt access through the daemon socket
# (libvirt group, Arch Wiki), volume creation through the storage pool API (virtinst
# build_vol_install with backing_store) and the cloud-init seed ISO in the virtinst user
# scratchdir when /var/lib/libvirt/boot is not writable (make_scratchdir).
# Sources: virt-install(1) (--noautoconsole, --console pty,target_type=serial,
# --events on_poweroff/on_reboot/on_crash), virtinst install/installer.py
# (requires_postboot_xml_changes/_alter_bootconfig on_reboot=destroy/_create_guest),
# libvirt formatdomain.html (on_* lifecycle events), cloud-init NoCloud datasource docs
# (seed ISO: volume label cidata + files user-data and meta-data, genisoimage -output
# seed.iso -volid cidata -joliet -rock user-data meta-data),
# virsh(1) (console [--force] [--safe], pool-dumpxml, net-dhcp-leases network [mac],
# vol-info/vol-delete vol-name-or-key-or-path, domrename requires an inactive domain),
# libvirt formatstorage.html (pool <target><path>), libvirt vsh-table.c (lease table
# prints a dividing line after the header - the data row is not line 2), cloud-init CLI
# reference (status --wait; exit 0 ok, 1 crash, 2 recoverable errors), cloud-init boot
# stages (status --wait blocks external scripts until done), cloud-init internal files
# (/var/lib/cloud/instance/boot-finished is the last file cloud-init creates), kernel
# sysctl docs (/proc/sys/kernel/random/boot_id changes exactly once per boot), cloud-init
# power_state (once per instance, after all other config modules), agetty(8) --autologin,
# systemd-run(1) --on-active, mktemp(1), base64(1), sha256sum(1), timeout(1),
# kernel random(4) (/dev/urandom), GitHub REST contents API (raw download URLs).
# German output, English comments/variables (PEP 8 convention, 18th round).
set -euo pipefail
# virsh/virt-install output is gettext-translated (libvirt ships .mo catalogs, e.g. de);
# under a non-C locale "virsh domstate" prints e.g. "laufend" instead of "running" and
# parsing breaks (observed 27th round). Force the C locale for all host tools; this
# script's own messages stay German.
export LC_ALL=C

VM_NAME="sandbox-worker"
LIBVIRT_URI="qemu:///system"
DISK=""      # default: <libvirt default pool>/sandbox-worker.qcow2 (resolved at runtime)
BACKING=""   # default: <libvirt default pool>/Arch-Linux-x86_64-cloudimg.qcow2
DISK_GB="64"
MEM_MIB="8192"
VCPUS="4"
USER_DATA=""
RAW_BASE="https://raw.githubusercontent.com/Ramb0N05/agent-sandbox-relay/worker"
DOMAIN=""
WORKER_TOKEN=""
TTYD_PW=""
ADMIN_PW=""
RECREATE=0
ASSUME_YES=0
DRY_RUN=0
SELF_UPDATE=0
WORKDIR=""
VM_USER="worker"
ORIG_ARGS=()
CONSOLE_RC=""
SEED_ISO=""
SELF_PATH=""   # absolute path of $0, resolved in main (used by the self-update)

# repo files fetched to the VM (name in repo); all downloads stay in the temp dir


die() { printf 'Fehler: %s\n' "$1" >&2; exit 1; }
info() { printf '%s\n' "$1"; }

need_cmd() { command -v "$1" >/dev/null 2>&1 || die "benötigtes Werkzeug fehlt: $1 (pacman -S $1 oder Paket nachtragen)"; }

gen_password() {
  # alphanumeric (fits every placeholder rule: no colon, no whitespace, printable ASCII)
  # read a fixed amount first, then filter: tr|head would break under pipefail
  # (head closes early, tr dies with SIGPIPE, set -o pipefail aborts the script)
  local raw
  raw="$(head -c 512 /dev/urandom | tr -dc 'A-Za-z0-9')"
  [ "${#raw}" -ge 24 ] || die "Zufallsausgabe zu kurz (unerwartet, Kernel-random(4))"
  printf '%s' "${raw:0:24}"
}

usage() {
  cat <<'EOF'
vm-install.sh - sandbox-worker-VM anlegen oder aktualisieren (Update nur über serielle Konsole, kein Relay-API-Zugriff)

  --vm-name NAME         libvirt-Name der VM (Default: sandbox-worker)
  --connect URI          libvirt-URI (Default: qemu:///system)
  --disk PATH            Overlay-qcow2 (Default: <libvirt-Default-Pool>/sandbox-worker.qcow2,
                         Pfad wird zur Laufzeit aus virsh pool-dumpxml default gelesen).
                         Eigenes Verzeichnis: virt-install verwaltet unbekannte Verzeichnisse
                         als automatisch angelegten dir-Pool; Volumes entstehen nur über
                         die Storage-Pool-API.
  --backing PATH         Basisimage (Default: <libvirt-Default-Pool>/Arch-Linux-x86_64-cloudimg.qcow2
                         - Pfad des Arch-Cloud-Images im Default-Pool; realen Pfad bei Bedarf
                         angeben). Muss für den QEMU-Laufzeitbenutzer lesbar sein (0644 genügt).
  --disk-gb N            Overlay-Größe in GiB (Default: 64)
  --memory MIB           RAM in MiB (Default: 8192)
  --vcpus N              vCPUs (Default: 4)
  --user-data PATH       optional: gerenderte user-data zusätzlich dauerhaft unter PATH ablegen
                         (Default: nur temporär im Skript-Tempverzeichnis; dauerhaft bleiben
                         nur die Images)
  --domain URL           Relay-Endpoint (optional; ohne Angabe bleibt der Platzhalter in der
                         Konfiguration, das Endpoint lässt sich nach dem Boot über die
                         vmadmin-UI setzen; beim Update übernimmt das Skript den laufenden
                         Endpoint der VM, falls konfiguriert)
  --worker-token TOKEN   Worker-Token für die Erstinstallation (optional; sonst nach dem Boot
                         über die vmadmin-UI setzen, der Agent läuft solange mit dem
                         Konstanten-Fallback und bekommt 401)
  --ttyd-password PW     ttyd-Passwort (Installation: sonst wird eines generiert;
                         Update: nur mit Angabe wird die ttyd-Unit angetastet)
  --admin-password PW    vmadmin-Passwort (Installation: sonst wird eines generiert;
                         Update: bleibt unberührt, Hash liegt in der VM)
  --raw-base URL         Repo-Quelle (Default: raw.githubusercontent.com, Branch worker)
  --self-update          vor dem Lauf sha256 des Skripts mit der Repo-Kopie (vm/vm-install.sh)
                         vergleichen; bei Abweichung $0 durch die Repo-Version ersetzen
                         (Backup inklusive) und neu starten
  --recreate             bestehende VM verwerfen und neu anlegen (LÖSCHT ALLE VM-DATEN)
  --yes                  Bestätigungen abkürzen
  --dry-run              nur holen, ersetzen und anzeigen; nichts anlegen oder ändern
  -h | --help            diese Hilfe

Ohne Root nutzbar: Zugang über den libvirt-Socket (Mitgliedschaft in der Gruppe
libvirt, Arch Wiki "Libvirt"), Volume-Anlage über den Storage-Pool (libvirtd
erzeugt die Datei), Löschung über virsh vol-delete. VM-Benutzer in der VM: worker.
Bestehende VM umbenennen: virsh domrename (nur im ausgeschalteten Zustand, virsh(1)).
EOF
}

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --vm-name) VM_NAME="${2:?--vm-name braucht einen Wert}"; shift 2 ;;
      --connect) LIBVIRT_URI="${2:?--connect braucht einen Wert}"; shift 2 ;;
      --disk) DISK="${2:?--disk braucht einen Wert}"; shift 2 ;;
      --backing) BACKING="${2:?--backing braucht einen Wert}"; shift 2 ;;
      --disk-gb) DISK_GB="${2:?--disk-gb braucht einen Wert}"; shift 2 ;;
      --memory) MEM_MIB="${2:?--memory braucht einen Wert}"; shift 2 ;;
      --vcpus) VCPUS="${2:?--vcpus braucht einen Wert}"; shift 2 ;;
      --user-data) USER_DATA="${2:?--user-data braucht einen Wert}"; shift 2 ;;
      --domain) DOMAIN="${2:?--domain braucht einen Wert}"; shift 2 ;;
      --worker-token) WORKER_TOKEN="${2:?--worker-token braucht einen Wert}"; shift 2 ;;
      --ttyd-password) TTYD_PW="${2:?--ttyd-password braucht einen Wert}"; shift 2 ;;
      --admin-password) ADMIN_PW="${2:?--admin-password braucht einen Wert}"; shift 2 ;;
      --raw-base) RAW_BASE="${2:?--raw-base braucht einen Wert}"; shift 2 ;;
      --self-update) SELF_UPDATE=1; shift ;;
      --recreate) RECREATE=1; shift ;;
      --yes) ASSUME_YES=1; shift ;;
      --dry-run) DRY_RUN=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) die "unbekannte Option: $1 (siehe --help)" ;;
    esac
  done
}

virsh_cmd() { LC_ALL=C virsh --connect "$LIBVIRT_URI" "$@"; }

vm_exists() { virsh_cmd dominfo "$VM_NAME" >/dev/null 2>&1; }

check_libvirt_access() {
  # fail early with a clear message when the socket refuses the connection
  virsh_cmd uri >/dev/null 2>&1 || die "kein Zugriff auf $LIBVIRT_URI.
Lösung: als root ausführen oder den eigenen Nutzer in die Gruppe libvirt aufnehmen
(Mitglieder haben passwortlosen Zugriff auf den RW-Socket, Arch Wiki \"Libvirt\")."
}

default_pool_path() {
  # libvirt pool XML carries the directory in <target><path>...</path></target>
  # (libvirt formatstorage.html); the persistent definition is /etc/libvirt/storage/default.xml
  virsh_cmd pool-dumpxml default 2>/dev/null | tr -d '\n' \
    | sed -n 's/.*<target><path>\([^<]*\)<\/path>.*/\1/p'
}

resolve_paths() {
  # image paths default to the libvirt default pool directory read from the libvirt
  # config at runtime; dry-run fallback without libvirt: the documented standard
  # /var/lib/libvirt/images (virtinst _preferred_default_pool_path)
  if [ -n "$DISK" ] && [ -n "$BACKING" ]; then return 0; fi
  local pool=""
  if [ "$DRY_RUN" -eq 0 ] || command -v virsh >/dev/null 2>&1; then
    pool="$(default_pool_path || true)"
  fi
  if [ -z "$pool" ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
      pool="/var/lib/libvirt/images"
      info "Dry-Run: libvirt nicht erreichbar - dokumentierter Standard-Pool-Pfad angenommen: $pool"
    else
      die "kein libvirt-Default-Pool gefunden (virsh --connect $LIBVIRT_URI pool-list).
Default-Pool anlegen: virsh pool-define-as default dir --target <pfad> && virsh pool-build default && virsh pool-start default
oder Pfade explizit angeben: --disk ... --backing ..."
    fi
  else
    info "libvirt-Default-Pool (virsh pool-dumpxml default, <target><path>): $pool"
  fi
  if [ -z "$DISK" ]; then DISK="$pool/sandbox-worker.qcow2"; fi
  if [ -z "$BACKING" ]; then BACKING="$pool/Arch-Linux-x86_64-cloudimg.qcow2"; fi
  info "Image-Pfade: Overlay $DISK, Backing $BACKING"
}

check_self_update() {
  # compare sha256 of this script with the repo copy (raw.githubusercontent.com serves
  # raw file bytes; GitHub REST contents API: download_url = raw.githubusercontent.com/...)
  local remote="$WORKDIR/vm-install.remote.sh"
  if ! curl -fsSL "$RAW_BASE/vm/vm-install.sh" -o "$remote"; then
    info "Self-Update-Check: Repo-Kopie nicht erreichbar ($RAW_BASE/vm/vm-install.sh) - übersprungen."
    return 0
  fi
  local remote_sha
  remote_sha="$(sha256sum "$remote" | awk '{print $1}')"
  local self="$SELF_PATH"
  if [ -z "$self" ]; then self="$0"; fi
  if [ ! -f "$self" ]; then
    info "Self-Update-Check: \$0 ist keine Datei (z.B. per pipe ausgeführt) - Remote-sha256: ${remote_sha:0:12}..."
    return 0
  fi
  local local_sha
  local_sha="$(sha256sum "$self" | awk '{print $1}')"
  if [ "$local_sha" = "$remote_sha" ]; then
    info "Self-Update: Skript ist aktuell (sha256 ${local_sha:0:12}...)."
    return 0
  fi
  info "Self-Update: Repo-Version abweichend (lokal ${local_sha:0:12}, remote ${remote_sha:0:12})."
  if [ "$SELF_UPDATE" -eq 1 ]; then
    local bak="$self.bak-$(date +%Y%m%d-%H%M%S)"
    cp -a "$self" "$bak" || die "Backup fehlgeschlagen: $bak"
    install -m 0755 "$remote" "$self" || die "Ersetzen von $self fehlgeschlagen (Rechte?)"
    info "Self-Update: $self durch die Repo-Version ersetzt (sha256 ${remote_sha:0:12}..., Backup: $bak) - starte mit Original-Argumenten neu."
    exec bash "$self" "${ORIG_ARGS[@]}"
  else
    info "Self-Update: neuere Repo-Version vorhanden - Update mit: $self --self-update <bisherige Optionen> (ersetzt die Datei, Backup inklusive, startet danach neu)."
  fi
}

disk_exists() {
  # volume inside a libvirt pool is not visible to an unprivileged caller on disk
  # (pool directory permissions), so ask the daemon as well
  [ -e "$DISK" ] || virsh_cmd vol-info "$DISK" >/dev/null 2>&1
}

fetch_repo_files() {
  # all downloads stay in the temp dir (mktemp -d, removed by the EXIT trap);
  # only the VM images persist on the host
  need_cmd curl
  WORKDIR="$(mktemp -d)"
  trap 'rm -rf "$WORKDIR"' EXIT
  # current worker-branch HEAD via raw.githubusercontent (repo is public)
  local f
  for f in cloud-init/sandbox-worker-user-data.yaml \
           files/agent-sandbox-agent \
           files/agent-sandbox-agent.service \
           files/ttyd-worker.service \
           files/vmadmin \
           files/vm-admin.service; do
    local target="$WORKDIR/$(printf '%s' "$f" | tr '/' '_')"
    curl -fsSL "$RAW_BASE/$f" -o "$target" || die "Download fehlgeschlagen: $RAW_BASE/$f"
    [ -s "$target" ] || die "leere Datei vom Repo: $f"
  done
  info "Aktuelle worker-Dateien geholt (nur temporär in $WORKDIR): user-data, Agent, vmadmin, 3 Units."
}

render_user_data() {
  # three placeholders; the worker-token fallback constant stays untouched on purpose
  # 21st round: --domain optional - without it the endpoint placeholder stays in the
  # user-data and the relay endpoint is set via vmadmin after the boot
  [ -n "$TTYD_PW" ] || TTYD_PW="$(gen_password)"
  [ -n "$ADMIN_PW" ] || ADMIN_PW="$(gen_password)"
  local ud
  ud="$(cat "$WORKDIR/cloud-init_sandbox-worker-user-data.yaml")"
  if [ -n "$DOMAIN" ]; then
    ud="${ud//<agent-sandbox-relay-domain>/$DOMAIN}"
  fi
  ud="${ud//<ttyd-passwort>/$TTYD_PW}"
  ud="${ud//<vm-admin-passwort>/$ADMIN_PW}"
  case "$ud" in
    *"<ttyd-passwort>"*|*"<vm-admin-passwort>"*)
      die "Platzhalter nach der Ersetzung noch vorhanden (unerwarteter Repo-Stand)" ;;
  esac
  if [ -n "$DOMAIN" ]; then
    case "$ud" in
      *"<agent-sandbox-relay-domain>"*)
        die "Domain-Platzhalter nach der Ersetzung noch vorhanden (unerwarteter Repo-Stand)" ;;
    esac
  else
    info "Hinweis: kein --domain angegeben - Relay-Endpoint nach dem Boot über die"
    info "vmadmin-UI setzen (Karte Relay-Endpoint; Agent liest die Endpoint-Datei beim Start)."
  fi
  # the temp copy is what virt-install gets; --user-data only adds a persistent copy
  printf '%s' "$ud" > "$WORKDIR/user-data.rendered"
  chmod 600 "$WORKDIR/user-data.rendered"
  if [ -n "$USER_DATA" ] && [ "$DRY_RUN" -eq 0 ]; then
    local target
    target="$(dirname "$USER_DATA")"
    [ -d "$target" ] || die "Zielverzeichnis der user-data existiert nicht: $target"
    if [ -e "$USER_DATA" ]; then
      mv "$USER_DATA" "$USER_DATA.bak-$(date +%Y%m%d-%H%M%S)"
      info "Bestehende user-data gesichert ($USER_DATA.bak-...)."
    fi
    printf '%s' "$ud" > "$USER_DATA"
    chmod 600 "$USER_DATA"
  fi
  if [ -z "$WORKER_TOKEN" ]; then
    info "Hinweis: kein --worker-token angegeben - Worker-Token nach dem Boot über die vmadmin-UI setzen."
  fi
}

# ---------- serial console ----------

console_run() {
  # $1 = command for the VM shell (may span multiple lines), $2 = seconds to wait for the RC marker
  # output lands in $WORKDIR/console.out, the command's exit code in $CONSOLE_RC
  # virsh(1): console [--force] [--safe] connects the guest serial console to stdin/stdout;
  # --force disconnects stale sessions, --safe requires safe console handling
  local cmd="$1" wait_s="${2:-60}"
  local out="$WORKDIR/console.out"
  local marker="__RC${RANDOM}__"
  : > "$out"
  CONSOLE_RC=""
  {
    sleep 2
    printf '\n'
    sleep 1
    printf '%s\n' "$cmd"
    printf 'echo "%s=$?"\n' "$marker"
    sleep "$wait_s"
  } | LC_ALL=C timeout $((wait_s + 25)) virsh --connect "$LIBVIRT_URI" console "$VM_NAME" --force --safe >"$out" 2>&1 || true
  # || true: while the VM is still booting there is no marker yet; without it pipefail
  # fails the assignment and set -e kills the script silently (bug, 26th round)
  CONSOLE_RC="$(grep -o "${marker}=[0-9]*" "$out" | tail -1 | cut -d= -f2)" || true
  if [ -z "$CONSOLE_RC" ]; then CONSOLE_RC="none"; fi
}

vm_running() {
  [ "$(virsh_cmd domstate "$VM_NAME" 2>/dev/null || true)" = "running" ]
}

wait_vm_ready() {
  # waits until cloud-init finished AND the first power_state reboot happened, then until
  # the worker services are active; only then the caller prints the initial values
  # sources: cloud-init CLI (status --wait, exit 0/1/2), boot stages doc (--wait blocks
  # external scripts until done), internal files (boot-finished = last file cloud-init
  # creates), kernel sysctl (/proc/sys/kernel/random/boot_id changes once per boot),
  # power_state module (once per instance, after all other config modules)
  need_cmd timeout
  local deadline=$((SECONDS + 1500))
  local boot1="" b=""
  info "Warte auf cloud-init (erster Boot) über die serielle Konsole ..."
  while [ $SECONDS -lt $deadline ]; do
    vm_running || die "VM $VM_NAME ist nicht mehr running (domstate: $(virsh_cmd domstate "$VM_NAME" 2>/dev/null || true)).
Ursache z.B. on_reboot=destroy in der Live-XML (virt-install-Zweiphasen-Install) - dieses Skript setzt jetzt --events on_reboot=restart (einphasig). Log: journalctl -u libvirtd."
    console_run 'cloud-init status --wait --timeout 300; echo "BOOTID=$(cat /proc/sys/kernel/random/boot_id)"; cloud-init status' 320 >/dev/null
    b="$(grep -o 'BOOTID=[0-9a-f-]*' "$WORKDIR/console.out" | tail -1 | cut -d= -f2)" || true
    if grep -q 'status: done' "$WORKDIR/console.out" && [ -n "$b" ]; then boot1="$b"; break; fi
    sleep 10
  done
  [ -n "$boot1" ] || die "cloud-init nicht fertig geworden (Timeout). Konsole: virsh --connect $LIBVIRT_URI console $VM_NAME"
  info "cloud-init fertig (boot_id $boot1). Warte auf den power_state-Neustart ..."
  local reboot_deadline=$((SECONDS + 900))
  local rebooted=0
  while [ $SECONDS -lt $reboot_deadline ]; do
    if ! vm_running; then
      info "VM ist während des power_state-Neustarts ausgegangen - starte neu (virsh start; die persistente XML trägt on_reboot=restart)."
      virsh_cmd start "$VM_NAME" || die "virsh start fehlgeschlagen"
    fi
    console_run 'cat /proc/sys/kernel/random/boot_id' 30 >/dev/null
    b="$(tr -d '[:space:]' < "$WORKDIR/console.out" | grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | tail -1)" || true
    if [ -n "$b" ] && [ "$b" != "$boot1" ]; then rebooted=1; break; fi
    sleep 15
  done
  if [ "$rebooted" -eq 1 ]; then
    info "Neustart erkannt (neue boot_id $b). Warte bis die VM wieder voll einsatzbereit ist ..."
  else
    info "WARNUNG: kein Neustart innerhalb von 900 s erkannt - prüfe weiter."
  fi
  local ready_deadline=$((SECONDS + 900))
  while [ $SECONDS -lt $ready_deadline ]; do
    vm_running || die "VM $VM_NAME ist während des Neustarts ausgegangen (domstate: $(virsh_cmd domstate "$VM_NAME" 2>/dev/null || true)). Manuell: virsh --connect $LIBVIRT_URI start $VM_NAME"
    console_run 'cloud-init status --wait --timeout 300; systemctl is-active agent-sandbox-agent.service vm-admin.service ttyd-worker.service; systemctl is-system-running; cloud-init status' 320 >/dev/null
    if grep -q 'status: done' "$WORKDIR/console.out" \
       && [ "$(grep -c '^active$' "$WORKDIR/console.out")" -ge 3 ] \
       && grep -qE '^(running|degraded)$' "$WORKDIR/console.out"; then
      info "VM vollständig einsatzbereit (cloud-init done, agent/vm-admin/ttyd aktiv, System running/degraded)."
      return 0
    fi
    sleep 15
  done
  die "VM nicht einsatzbereit geworden (Timeout). Konsole: virsh --connect $LIBVIRT_URI console $VM_NAME"
}

build_seed() {
  # NoCloud seed ISO built locally in the temp dir: volume label cidata with the files
  # user-data and meta-data (cloud-init NoCloud datasource docs). It contains the
  # rendered passwords, so it stays in WORKDIR and is detached after the first boot.
  printf 'instance-id: iid-%s-01\nlocal-hostname: %s\n' "$VM_NAME" "$VM_NAME" > "$WORKDIR/meta-data"
  cp "$WORKDIR/user-data.rendered" "$WORKDIR/user-data"
  chmod 600 "$WORKDIR/user-data" "$WORKDIR/meta-data"
  chmod 755 "$WORKDIR"   # the qemu process must be able to traverse/read the seed ISO
  if command -v genisoimage >/dev/null 2>&1; then
    (cd "$WORKDIR" && genisoimage -quiet -output seed.iso -volid cidata -joliet -rock user-data meta-data)       || die "genisoimage fehlgeschlagen"
  elif command -v xorriso >/dev/null 2>&1; then
    (cd "$WORKDIR" && xorriso -as mkisofs -quiet -o seed.iso -volid cidata -joliet -rock user-data meta-data)       || die "xorriso -as mkisofs fehlgeschlagen"
  else
    die "kein ISO-Tool gefunden - installieren: pacman -S genisoimage (oder xorriso)"
  fi
  chmod 644 "$WORKDIR/seed.iso"
  SEED_ISO="$WORKDIR/seed.iso"
}

detach_seed() {
  # detach the seed disk (vdb) live+config so neither XML references the temp ISO anymore
  if virsh_cmd detach-disk "$VM_NAME" vdb --live --config >/dev/null 2>&1; then
    info "Seed-ISO (vdb) abgehängt (live+config) - die temp-Datei wird beim Beenden entfernt."
  else
    info "WARNUNG: Seed-ISO (vdb) konnte nicht abgehängt werden - manuell: virsh detach-disk $VM_NAME vdb --live --config"
  fi
}

install_vm() {
  render_user_data
  if [ "$DRY_RUN" -eq 1 ]; then
    # a dry run must not require host resources (no backing image, no libvirt)
    if [ ! -e "$BACKING" ]; then
      info "Hinweis: Basisimage fehlt (bei der echten Anlage Pflicht): $BACKING (--backing)"
    fi
    if [ -e "$DISK" ]; then
      info "Hinweis: Overlay existiert bereits: $DISK (echte Anlage bricht hier ab, --recreate für Neu-Anlage)"
    fi
    info "Dry-Run: würde Seed-ISO bauen (genisoimage/xorriso, volid cidata, user-data+meta-data, nur temporaer) und virt-install starten ($MEM_MIB MiB, $VCPUS vCPUs, Disk $DISK_GB GiB auf $DISK, Backing $BACKING, Seed als vdb, --console pty,target_type=serial, --events on_reboot=restart,on_poweroff=destroy,on_crash=restart, einphasig)."
    info "Dry-Run: danach Warten auf cloud-init + power_state-Neustart über die serielle Konsole, dann Seed detach, erst dann Initialwerte."
    print_initial_values
    return 0
  fi
  [ -e "$BACKING" ] || die "Basisimage fehlt: $BACKING (--backing)"
  if disk_exists; then
    die "Overlay existiert bereits: $DISK
cloud-init läuft nur einmal pro Overlay (per-instance); eine Neu-Provisionierung braucht
ein neues Overlay. Neu anlegen mit --recreate (LÖSCHT ALLE DATEN der bestehenden VM)."
  fi
  need_cmd virt-install
  need_cmd timeout
  build_seed
  # --noautoconsole: no automatic console connect, virt-install returns (virt-install(1))
  # --console pty,target_type=serial: serial console on a host pty, reachable via
  # "virsh console" (virt-install(1)); the guest runs serial-getty@ttyS0 with
  # agetty --autologin worker (user-data), so the console logs in automatically
  # --events on_reboot=restart,...: virt-install's two-phase install (--cloud-init) sets
  # on_reboot=destroy on the LIVE xml (virtinst installer.py _alter_bootconfig), which
  # destroyed the VM at the power_state reboot (26th round). Single-phase --import keeps
  # live == persistent XML with these events, so the reboot really restarts the VM.
  # the seed ISO goes in as a second readonly virtio disk (vdb); NoCloud finds it by the
  # cidata volume label on any block device
  # size+backing_store make virtinst create the volume through the parent storage pool
  # (build_vol_install), so no direct write access to the pool directory is needed
  virt-install \
    --connect "$LIBVIRT_URI" \
    --name "$VM_NAME" \
    --memory "$MEM_MIB" --vcpus "$VCPUS" \
    --cpu host-model \
    --machine q35 \
    --osinfo archlinux \
    --import \
    --disk "path=$DISK,size=$DISK_GB,format=qcow2,bus=virtio,backing_store=$BACKING" \
    --disk "path=$SEED_ISO,device=disk,bus=virtio,format=raw,readonly=on" \
    --network network=default,model=virtio \
    --graphics none \
    --console pty,target_type=serial \
    --events on_reboot=restart,on_poweroff=destroy,on_crash=restart \
    --memballoon none \
    --controller usb,model=none \
    --noautoconsole
  info "VM angelegt. Warte bis cloud-init fertig ist und der power_state-Neustart durch ist ..."
  wait_vm_ready
  detach_seed
  info "Warte auf DHCP-Lease (Netzwerk default) ..."
  local ip=""
  local tries=0
  while [ $tries -lt 24 ]; do
    ip="$(vm_ip)"
    if [ -n "$ip" ]; then break; fi
    sleep 5
    tries=$((tries + 1))
  done
  if [ -z "$ip" ]; then
    info "Keine DHCP-Lease innerhalb von 120 s gefunden (virsh net-dhcp-leases default)."
    info "IP später nachschlagen: virsh net-dhcp-leases default"
  fi
  print_initial_values "$ip"
}

vm_ip() {
  # MAC from domiflist, then the matching lease row
  # (net-dhcp-leases takes the MAC as an argument; columns: expiry date, time,
  # MAC, protocol, IP/prefix, hostname, client id - so the IP is field 5)
  # match the MAC instead of counting lines: vshTable prints a header, then a
  # dividing line, then the data rows (libvirt vsh-table.c) - the data row is
  # line 3, not line 2
  local mac
  mac="$(virsh_cmd domiflist "$VM_NAME" 2>/dev/null | awk '/network/ {print $NF; exit}')" || true
  [ -n "$mac" ] || return 0
  virsh_cmd net-dhcp-leases default "$mac" 2>/dev/null \
    | awk -v m="$mac" 'index($0, m) {sub(/\/.*/, "", $5); print $5; exit}'
}

print_initial_values() {
  local ip="${1:-}"
  info ""
  info "================ Initialwerte (bitte notieren) ================"
  if [ -n "$ip" ]; then info "VM-IP (DHCP):        $ip"; fi
  info "ttyd-Passwort:      $TTYD_PW   (Web-Terminal, Login $VM_USER)"
  info "vmadmin-Passwort:   $ADMIN_PW   (Login admin; bitte in der UI ändern)"
  if [ -n "$ip" ]; then info "vmadmin-UI:         http://$ip/admin"; fi
  if [ -n "$WORKER_TOKEN" ]; then
    info "Worker-Token:       im Agent eingebaut (Datei vor Konstante: nach Boot über vmadmin aenderbar)"
  else
    info "Worker-Token:       nicht gesetzt - über die vmadmin-UI setzen (Karte Einstellungen)"
  fi
  if [ -n "$DOMAIN" ]; then
    info "Relay-Endpoint:     $DOMAIN (in der user-data gesetzt)"
  else
    info "Relay-Endpoint:     Platzhalter - über die vmadmin-UI setzen (Karte Relay-Endpoint)"
  fi
  info "Konsole:             virsh --connect $LIBVIRT_URI console $VM_NAME (Autologin $VM_USER)"
  info "==============================================================="
  if [ "$DRY_RUN" -eq 1 ]; then
    info "Dry-Run: die gerenderte user-data liegt nur im Temporärverzeichnis, es wird nichts geschrieben."
  else
    info "Die Passwörter stehen im Klartext in dieser Ausgabe"
    if [ -n "$USER_DATA" ]; then
      info "sowie in $USER_DATA (0600); Datei nach dem Boot nach Bedarf löschen."
    else
      info "(die user-data liegt nur temporär, nichts wurde dauerhaft gespeichert)."
    fi
    info "Passwörter nach dem Boot in der vmadmin-UI ändern."
  fi
}

# ---------- update path (serial console only, no relay API) ----------

ship_console_file() {
  # $1 = local temp file, $2 = name in /tmp/vm-update; transferred as base64 heredoc
  # through the serial console (lines <= 76 chars stay below the tty canonical line
  # length, base64(1) -w 76)
  local f="$1" name="$2"
  {
    printf "cat > /tmp/vm-update/%s.b64 <<'__B64__'\n" "$name"
    base64 -w 76 "$f"
    printf '__B64__\n'
    printf 'base64 -d /tmp/vm-update/%s.b64 > /tmp/vm-update/%s && rm -f /tmp/vm-update/%s.b64\n' "$name" "$name" "$name"
  } > "$WORKDIR/ship.cmd"
  console_run "$(cat "$WORKDIR/ship.cmd")" 120 >/dev/null
  [ "$CONSOLE_RC" = "0" ] || die "base64-Transfer fehlgeschlagen: $name (Konsole erreichbar?)"
  info "  $name übertragen (base64 über die serielle Konsole)."
}

render_agent() {
  # agent file: replace the placeholder endpoint only if it is still in there
  # 21st round: --domain optional - without it the placeholder stays; the agent
  # reads the endpoint file first (written by vmadmin POST /admin/api/endpoint)
  local agent="$WORKDIR/files_agent-sandbox-agent"
  if grep -q '<agent-sandbox-relay-domain>' "$agent"; then
    if [ -n "$DOMAIN" ]; then
      local content
      content="$(cat "$agent")"
      content="${content//<agent-sandbox-relay-domain>/$DOMAIN}"
      printf '%s' "$content" > "$WORKDIR/agent.rendered"
      info "Domain-Platzhalter im Agent ersetzt (Endpoint $DOMAIN)."
    else
      cp "$agent" "$WORKDIR/agent.rendered"
      info "Kein --domain und kein laufender Endpoint in der VM - Platzhalter bleibt."
      info "Relay-Endpoint über die vmadmin-UI setzen (Karte Relay-Endpoint); der"
      info "Agent liest die Endpoint-Datei beim Start."
    fi
  else
    cp "$agent" "$WORKDIR/agent.rendered"
    info "Agent enthält bereits eine echte Domain - bleibt unberührt (Initialwert)."
  fi
}

update_vm() {
  local state
  state="$(virsh_cmd domstate "$VM_NAME" 2>/dev/null || true)"
  if [ "$state" != "running" ]; then
    die "VM ist nicht gestartet (Zustand: ${state:-unbekannt}). Erst starten:
  virsh --connect $LIBVIRT_URI start $VM_NAME"
  fi
  need_cmd timeout
  info "Update ausschließlich über die serielle Konsole (virsh console --force --safe; kein Relay-API-Zugriff) ..."
  # VM-Benutzer prüfen: eine VM der alten Bereitstellung (anderer Benutzer) wird abgewiesen
  console_run 'echo "USER=$(id -un)"' 30 >/dev/null
  local user
  user="$(grep -oE 'USER=[A-Za-z0-9_.-]+' "$WORKDIR/console.out" | tail -1 | cut -d= -f2-)" || true
  if [ "$user" != "$VM_USER" ]; then
    die "In der VM ist Benutzer \"${user:-?}\" eingeloggt, erwartet ist \"$VM_USER\".
Diese VM traegt noch die alte Bereitstellung (vor der Umbenennung). Neu anlegen:
  $0 --recreate --yes ...   (LÖSCHT ALLE VM-DATEN)"
  fi
  info "VM-Benutzer bestätigt: $user (Autologin über serial-getty@ttyS0, agetty --autologin)"
  if [ -z "$DOMAIN" ]; then
    # keep the running endpoint when --domain is not given (endpoint file first,
    # then the ENDPOINT constant of the installed agent)
    console_run "ep=\$(cat /home/$VM_USER/.config/agent-sandbox/endpoint 2>/dev/null); [ -z \"\$ep\" ] && ep=\$(grep -o 'ENDPOINT = \"[^\"]*\"' /usr/local/bin/agent-sandbox-agent | head -1 | cut -d'\"' -f2); echo \"EP=\$ep\"" 30 >/dev/null
    local ep
    ep="$(grep -oE 'EP=https?://[^ ]*' "$WORKDIR/console.out" | tail -1 | cut -d= -f2-)" || true
    case "$ep" in
      ""|*"<agent-sandbox-relay-domain>"*) ;;
      *) DOMAIN="$ep"; info "Laufender Relay-Endpoint aus der VM übernommen: $DOMAIN" ;;
    esac
  fi
  render_agent
  console_run 'mkdir -p /tmp/vm-update' 30 >/dev/null
  [ "$CONSOLE_RC" = "0" ] || die "Serielle Konsole nicht erreichbar. VM läuft? Manuell: virsh --connect $LIBVIRT_URI console $VM_NAME"
  # unmodified files: the VM fetches them itself from the public repo into /tmp/vm-update
  # (curl is installed by cloud-init, packages list in the user-data)
  console_run "cd /tmp/vm-update && curl -fsSL $RAW_BASE/files/vmadmin -o vmadmin && curl -fsSL $RAW_BASE/files/agent-sandbox-agent.service -o agent.service && curl -fsSL $RAW_BASE/files/vm-admin.service -o vm-admin.service" 120 >/dev/null
  [ "$CONSOLE_RC" = "0" ] || die "Fetch in der VM fehlgeschlagen (curl $RAW_BASE/files/...)"
  # rendered files (agent with endpoint, ttyd unit with password) via base64 over the console
  ship_console_file "$WORKDIR/agent.rendered" "agent"
  if [ -n "$TTYD_PW" ]; then
    # only with an explicit password: the unit carries it in ExecStart
    local unit
    unit="$(cat "$WORKDIR/files_ttyd-worker.service")"
    unit="${unit//<ttyd-passwort>/$TTYD_PW}"
    printf '%s' "$unit" > "$WORKDIR/ttyd-unit.rendered"
    ship_console_file "$WORKDIR/ttyd-unit.rendered" "ttyd-worker.service"
  else
    info "  ttyd-worker.service unberührt (Passwort steckt in der Unit; --ttyd-password zum Ändern)."
  fi
  # units carry no placeholders; root:root 0644 like the cloud-init write_files defaults
  console_run "sudo install -o $VM_USER -g $VM_USER -m 0700 /tmp/vm-update/vmadmin /usr/local/bin/vmadmin && sudo install -o $VM_USER -g $VM_USER -m 0700 /tmp/vm-update/agent /usr/local/bin/agent-sandbox-agent && sudo install -o root -g root -m 0644 /tmp/vm-update/agent.service /etc/systemd/system/agent-sandbox-agent.service && sudo install -o root -g root -m 0644 /tmp/vm-update/vm-admin.service /etc/systemd/system/vm-admin.service" 90 >/dev/null
  [ "$CONSOLE_RC" = "0" ] || die "install in der VM fehlgeschlagen"
  if [ -n "$TTYD_PW" ]; then
    console_run "sudo install -o root -g root -m 0644 /tmp/vm-update/ttyd-worker.service /etc/systemd/system/ttyd-worker.service" 60 >/dev/null
    [ "$CONSOLE_RC" = "0" ] || die "ttyd-Unit-Install fehlgeschlagen"
  fi
  console_run "sudo systemctl daemon-reload && sudo systemctl restart vm-admin.service" 60 >/dev/null
  [ "$CONSOLE_RC" = "0" ] || die "daemon-reload / vm-admin-Restart fehlgeschlagen"
  if [ -n "$TTYD_PW" ]; then
    console_run "sudo systemctl restart ttyd-worker.service" 60 >/dev/null
    [ "$CONSOLE_RC" = "0" ] || die "ttyd-Restart fehlgeschlagen"
  fi
  # agent last, delayed through a transient timer (systemd-run --on-active=3s,
  # systemd-run(1)) so this console session can still finish cleanly
  console_run "sudo systemd-run --on-active=3s systemctl restart agent-sandbox-agent.service" 30 >/dev/null
  [ "$CONSOLE_RC" = "0" ] || die "Agent-Restart (systemd-run) fehlgeschlagen"
  sleep 6
  console_run "systemctl is-active agent-sandbox-agent.service vm-admin.service ttyd-worker.service" 30 >/dev/null
  if [ "$CONSOLE_RC" = "0" ]; then
    info "Update abgeschlossen: agent/vm-admin/ttyd aktiv. Worker-Token, Admin-Hash und ttyd-Passwort wurden nicht angetastet."
  else
    info "WARNUNG: nicht alle Units aktiv - prüfen: virsh --connect $LIBVIRT_URI console $VM_NAME"
  fi
  info "Heruntergeladene Dateien liegen nur in /tmp/vm-update (VM, temporär) und im mktemp-Verzeichnis des Skripts (per trap entfernt); dauerhaft bleiben nur die Images."
}

confirm_recreate() {
  info "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
  info "! --recreate LÖSCHT ALLE DATEN DER VM (Overlay wird gelöscht)!"
  info "! Betroffen: $DISK"
  info "! Alles in der VM (Dateien, Jobs-Results, Konfiguration) ist weg."
  info "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
  [ "$ASSUME_YES" -eq 1 ] && return 0
  local answer
  printf 'Weiter? Wort "ja" eingeben: '
  read -r answer
  [ "$answer" = "ja" ] || die "abgebrochen"
}

delete_disk() {
  # volume deletion without root: the daemon deletes pool volumes
  # (virsh vol-delete takes a vol-name-or-key-or-path); fall back to rm for
  # overlays outside any pool that the invoking user owns
  if virsh_cmd vol-delete "$DISK" >/dev/null 2>&1; then
    return 0
  fi
  rm -f -- "$DISK" || true
  if disk_exists; then
    die "Overlay konnte nicht gelöscht werden: $DISK
(virsh vol-delete und rm sind gescheitert - Dateirechte prüfen.)"
  fi
}

main() {
  ORIG_ARGS=("$@")
  case "$0" in
    /*) SELF_PATH="$0" ;;
    *)  SELF_PATH="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")" ;;
  esac
  parse_args "$@"
  fetch_repo_files
  if [ "$DRY_RUN" -eq 0 ]; then
    need_cmd virsh
    check_libvirt_access
    resolve_paths
    check_self_update
  else
    resolve_paths
    info "Dry-Run: Self-Update-Check würde sha256(\$0) mit $RAW_BASE/vm/vm-install.sh vergleichen (kein Schreiben)."
  fi
  if [ "$DRY_RUN" -eq 1 ]; then
    if command -v virsh >/dev/null 2>&1 && vm_exists; then
      if [ "$RECREATE" -eq 1 ]; then
        info "Dry-Run: VM $VM_NAME existiert - mit --recreate würde sie verworfen (virsh destroy falls laufend, dann undefine + Overlay löschen: $DISK) und danach neu angelegt (Install-Pfad)."
        return 0
      fi
      info "Dry-Run: VM $VM_NAME existiert - es würde der Update-Pfad über die serielle Konsole laufen (virsh console --force --safe, keine Relay-API)."
      render_agent
      return 0
    fi
    info "Dry-Run: keine VM-Anlage, keine Änderung an Bestehendem."
    install_vm
    return 0
  fi
  if vm_exists; then
    if [ "$RECREATE" -eq 1 ]; then
      confirm_recreate
      # virsh(1): "undefine" on a RUNNING domain converts it to a transient domain
      # WITHOUT stopping it - the transient keeps holding the disk, so virt-install's
      # path_in_use check then fails with "already in use by other guests" (observed
      # 27th round). Therefore: destroy first (immediate stop; data loss was confirmed
      # with --recreate), then undefine, then delete the volume.
      local rstate
      rstate="$(virsh_cmd domstate "$VM_NAME" 2>/dev/null || true)"
      if [ -n "$rstate" ] && [ "$rstate" != "shut off" ]; then
        info "VM ist '$rstate' - virsh destroy (sofortiges Beenden, virsh(1) destroy)."
        virsh_cmd destroy "$VM_NAME" || die "virsh destroy fehlgeschlagen"
      fi
      virsh_cmd undefine "$VM_NAME" || die "virsh undefine fehlgeschlagen"
      delete_disk
      info "Alte VM verworfen, Overlay gelöscht - Neu-Anlage läuft."
      install_vm
    else
      info "VM $VM_NAME existiert - Update-Pfad (serielle Konsole)."
      update_vm
    fi
  else
    install_vm
  fi
}

main "$@"
