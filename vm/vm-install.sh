#!/bin/bash
# vm-install.sh - install or update the vibe-sandbox VM on the local libvirt host (19th round).
# Install (VM does not exist): fetches the current worker-branch files from the public
# GitHub repo, replaces the three user-data placeholders, generates the initial
# passwords, runs virt-install and prints the initial values.
# Update (VM exists): VM data stays untouched - worker-token file, admin-hash and the
# ttyd unit (which carries the ttyd password in ExecStart) are kept; the program files
# and units ship through the relay job queue (documented deployment path, 15th round)
# as long as the agent answers. If the agent does not answer, the script prints the
# manual serial-console procedure instead.
# All paths are parameters; no host path is hardcoded beyond documented defaults.
# Sources: virt-install(1) (--noautoconsole: no automatic console connect), cloud-init
# per-instance semantics (write_files/runcmd run once per overlay; a re-provision needs
# a fresh overlay), libvirt virsh(1) (dominfo/domstate/net-dhcp-leases), kernel
# random(4) (/dev/urandom), relay README (API contract), cc_write_files.py
# (unit files land root:root 0644 without owner/permissions keys).
# German output, English comments/variables (PEP 8 convention, 18th round).
set -euo pipefail

VM_NAME="vibe-sandbox"
LIBVIRT_URI="qemu:///system"
DISK="/mnt/GamesSSD0/libvirt/images/vibe-sandbox.qcow2"
BACKING="/mnt/GamesHDD/libvirt/images/Arch-Linux-x86_64-cloudimg.qcow2"
DISK_GB="64"
MEM_MIB="8192"
VCPUS="4"
USER_DATA="/root/vibe-sandbox-user-data.yaml"
RAW_BASE="https://raw.githubusercontent.com/Ramb0N05/agent-sandbox-relay/worker"
DOMAIN=""
RELAY=""
SUBMIT=""
WORKER_TOKEN=""
TTYD_PW=""
ADMIN_PW=""
RECREATE=0
ASSUME_YES=0
DRY_RUN=0
WORKDIR=""

# repo files shipped to the VM (name in repo -> target in VM, owner, mode)
# units carry no placeholders; ttyd-vibe.service is only touched with --ttyd-password
# (it contains the ttyd password in ExecStart and would otherwise lose it).

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
vm-install.sh - vibe-sandbox-VM anlegen oder aktualisieren

  --vm-name NAME         libvirt-Name der VM (Default: vibe-sandbox)
  --connect URI          libvirt-URI (Default: qemu:///system)
  --disk PATH            Overlay-qcow2 (Default: /mnt/GamesSSD0/libvirt/images/vibe-sandbox.qcow2)
  --backing PATH         Basisimage (Default: /mnt/GamesHDD/libvirt/images/Arch-Linux-x86_64-cloudimg.qcow2)
  --disk-gb N           Overlay-Größe in GiB (Default: 64)
  --memory MIB          RAM in MiB (Default: 8192)
  --vcpus N             vCPUs (Default: 4)
  --user-data PATH      Ziel der gerenderten user-data (Default: /root/vibe-sandbox-user-data.yaml)
  --domain URL          Relay-Endpoint (Installation: Pflicht; Update: Pflicht, falls der
                         Agent noch den Platzhalter enthält)
  --worker-token TOKEN  Worker-Token für die Erstinstallation (optional; sonst nach dem
                         Boot über die vmadmin-UI setzen, der Agent läuft solange mit dem
                         Konstanten-Fallback und bekommt 401)
  --ttyd-password PW    ttyd-Passwort (Installation: sonst wird eines generiert;
                         Update: nur mit Angabe wird die ttyd-Unit angetastet)
  --admin-password PW    vmadmin-Passwort (Installation: sonst wird eines generiert;
                         Update: bleibt unberührt, Hash liegt in der VM)
  --relay URL           Relay-Basis-URL für das Update über die Job-Queue
  --submitter-token T   Submitter-Token für das Update über die Job-Queue
  --raw-base URL        Repo-Quelle (Default: raw.githubusercontent.com, Branch worker)
  --recreate            bestehende VM verwerfen und neu anlegen (LÖSCHT ALLE VM-DATEN)
  --yes                 Bestätigungen abkürzen
  --dry-run             nur holen, ersetzen und anzeigen; nichts anlegen oder ändern
  -h | --help           diese Hilfe
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
      --relay) RELAY="${2:?--relay braucht einen Wert}"; shift 2 ;;
      --submitter-token) SUBMIT="${2:?--submitter-token braucht einen Wert}"; shift 2 ;;
      --raw-base) RAW_BASE="${2:?--raw-base braucht einen Wert}"; shift 2 ;;
      --recreate) RECREATE=1; shift ;;
      --yes) ASSUME_YES=1; shift ;;
      --dry-run) DRY_RUN=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) die "unbekannte Option: $1 (siehe --help)" ;;
    esac
  done
}

virsh_cmd() { virsh --connect "$LIBVIRT_URI" "$@"; }

vm_exists() { virsh_cmd dominfo "$VM_NAME" >/dev/null 2>&1; }

fetch_repo_files() {
  need_cmd curl
  WORKDIR="$(mktemp -d)"
  trap 'rm -rf "$WORKDIR"' EXIT
  # current worker-branch HEAD via raw.githubusercontent (repo is public)
  local f
  for f in cloud-init/vibe-sandbox-user-data.yaml \
           files/agent-sandbox-agent \
           files/agent-sandbox-agent.service \
           files/ttyd-vibe.service \
           files/vmadmin \
           files/vm-admin.service; do
    local target="$WORKDIR/$(printf '%s' "$f" | tr '/' '_')"
    curl -fsSL "$RAW_BASE/$f" -o "$target" || die "Download fehlgeschlagen: $RAW_BASE/$f"
    [ -s "$target" ] || die "leere Datei vom Repo: $f"
  done
  info "Aktuelle worker-Dateien geholt: user-data, Agent, vmadmin, 3 Units."
}

render_user_data() {
  # three placeholders; the worker-token fallback constant stays untouched on purpose
  [ -n "$DOMAIN" ] || die "Installation braucht --domain (Relay-Endpoint, z. B. https://sandbox-relay.example)"
  [ -n "$TTYD_PW" ] || TTYD_PW="$(gen_password)"
  [ -n "$ADMIN_PW" ] || ADMIN_PW="$(gen_password)"
  local ud target
  ud="$(cat "$WORKDIR/cloud-init_vibe-sandbox-user-data.yaml")"
  ud="${ud//<agent-sandbox-relay-domain>/$DOMAIN}"
  ud="${ud//<ttyd-passwort>/$TTYD_PW}"
  ud="${ud//<vm-admin-passwort>/$ADMIN_PW}"
  case "$ud" in
    *"<agent-sandbox-relay-domain>"*|*"<ttyd-passwort>"*|*"<vm-admin-passwort>"*)
      die "Platzhalter nach der Ersetzung noch vorhanden (unerwarteter Repo-Stand)" ;;
  esac
  target="$(dirname "$USER_DATA")"
  [ -d "$target" ] || die "Zielverzeichnis der user-data existiert nicht: $target"
  if [ "$DRY_RUN" -eq 0 ] && [ -e "$USER_DATA" ]; then
    mv "$USER_DATA" "$USER_DATA.bak-$(date +%Y%m%d-%H%M%S)"
    info "Bestehende user-data gesichert ($USER_DATA.bak-...)."
  fi
  if [ "$DRY_RUN" -eq 0 ]; then
    printf '%s' "$ud" > "$USER_DATA"
    chmod 600 "$USER_DATA"
  else
    printf '%s' "$ud" > "$WORKDIR/user-data.rendered"
  fi
  if [ -z "$WORKER_TOKEN" ]; then
    info "Hinweis: kein --worker-token angegeben - Worker-Token nach dem Boot über die vmadmin-UI setzen."
  fi
}

install_vm() {
  [ -n "$DOMAIN" ] || die "--domain fehlt (siehe --help)"
  [ -e "$BACKING" ] || die "Basisimage fehlt: $BACKING (--backing)"
  if [ -e "$DISK" ]; then
    die "Overlay existiert bereits: $DISK
cloud-init läuft nur einmal pro Overlay (per-instance); eine Neu-Provisionierung braucht
ein neues Overlay. Neu anlegen mit --recreate (LÖSCHT ALLE DATEN der bestehenden VM)."
  fi
  render_user_data
  if [ "$DRY_RUN" -eq 1 ]; then
    info "Dry-Run: würde virt-install starten ($MEM_MIB MiB, $VCPUS vCPUs, Disk $DISK_GB GiB auf $DISK, Backing $BACKING)."
    print_initial_values
    return 0
  fi
  need_cmd virt-install
  # --noautoconsole: no automatic console connect, virt-install returns (virt-install(1))
  virt-install \
    --connect "$LIBVIRT_URI" \
    --name "$VM_NAME" \
    --memory "$MEM_MIB" --vcpus "$VCPUS" \
    --cpu host-model \
    --machine q35 \
    --osinfo archlinux \
    --import \
    --disk "path=$DISK,size=$DISK_GB,format=qcow2,bus=virtio,backing_store=$BACKING" \
    --network network=default,model=virtio \
    --graphics none \
    --memballoon none \
    --controller usb,model=none \
    --cloud-init user-data="$USER_DATA",disable=on \
    --noautoconsole
  info "VM angelegt, wartet auf DHCP-Lease (Netzwerk default) ..."
  local ip=""
  local tries=0
  while [ $tries -lt 24 ]; do
    ip="$(vm_ip)"
    [ -n "$ip" ] && break
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
  local mac
  mac="$(virsh_cmd domiflist "$VM_NAME" 2>/dev/null | awk '/network/ {print $NF; exit}')" || true
  [ -n "$mac" ] || return 0
  virsh_cmd net-dhcp-leases default "$mac" 2>/dev/null \
    | awk 'NR == 2 {sub(/\/.*/, "", $5); print $5; exit}'
}

print_initial_values() {
  local ip="${1:-}"
  info ""
  info "================ Initialwerte (bitte notieren) ================"
  [ -n "$ip" ] && info "VM-IP (DHCP):        $ip"
  info "ttyd-Passwort:      $TTYD_PW   (Web-Terminal, Login vibe)"
  info "vmadmin-Passwort:   $ADMIN_PW   (Login admin; bitte in der UI ändern)"
  [ -n "$ip" ] && info "vmadmin-UI:         http://$ip/admin"
  if [ -n "$WORKER_TOKEN" ]; then
    info "Worker-Token:       im Agent eingebaut (Datei vor Konstante: nach Boot über vmadmin änderbar)"
  else
    info "Worker-Token:       nicht gesetzt - über die vmadmin-UI setzen (Karte Einstellungen)"
  fi
  info "Konsole:             virsh --connect $LIBVIRT_URI console $VM_NAME (Autologin vibe)"
  info "==============================================================="
  if [ "$DRY_RUN" -eq 1 ]; then
    info "Dry-Run: die gerenderte user-data liegt nur im Temporärverzeichnis, es wird nichts geschrieben."
  else
    info "Achtung: die Passwörter stehen im Klartext in $USER_DATA (0600) und in"
    info "dieser Ausgabe. Datei nach dem Boot nach Bedarf löschen; Passwörter in der"
    info "vmadmin-UI ändern."
  fi
}

# ---------- update path (job queue) ----------

queue_job() {
  # $1 = cmd; prints the job id
  local cmd="$1"
  local body
  body="$(jq -n --arg c "$cmd" '{cmd: $c}')"
  curl -fsS -X POST "$RELAY/jobs" \
    -H "Authorization: Bearer $SUBMIT" -H "Content-Type: application/json" \
    -d "$body" | jq -r '.id' || die "Job-Einreichung fehlgeschlagen: $cmd"
}

wait_job() {
  # $1 = job id; prints "status rc" (waits up to 120 s for the result)
  local id="$1" resp status rc
  curl -fsS --max-time 130 "$RELAY/jobs/$id/wait?timeout=120" \
    -H "Authorization: Bearer $SUBMIT" >/dev/null 2>&1 || true
  resp="$(curl -fsS "$RELAY/jobs/$id" -H "Authorization: Bearer $SUBMIT")" \
    || die "Job-Abfrage fehlgeschlagen (id $id)"
  status="$(printf '%s' "$resp" | jq -r '.status')"
  rc="$(printf '%s' "$resp" | jq -r '.rc // "null"')"
  printf '%s %s' "$status" "$rc"
}

run_job() {
  # $1 = description, $2 = cmd; aborts when the job does not finish cleanly
  local desc="$1" cmd="$2" id out
  id="$(queue_job "$cmd")"
  out="$(wait_job "$id")"
  local status rc
  status="${out%% *}"
  rc="${out##* }"
  info "  Job ($desc): $status rc=$rc"
  [ "$status" = "done" ] || die "Job nicht abgeschlossen ($desc): $status - Abbruch, VM-Zustand bleibt wie er ist"
  [ "$rc" = "0" ] || die "Job fehlgeschlagen ($desc, rc=$rc) - Abbruch, VM-Zustand bleibt wie er ist"
}

ship_file() {
  # $1 = local file, $2 = name inside the VM, $3 = target path, $4 = owner, $5 = mode
  local local_file="$1" vmname="$2" target="$3" owner="$4" mode="$5" fid
  fid="$(curl -fsS -X POST "$RELAY/files" \
    -H "Authorization: Bearer $SUBMIT" -H "Content-Type: application/octet-stream" \
    --data-binary "@$local_file" | jq -r '.id')" \
    || die "Upload fehlgeschlagen: $vmname"
  run_job "VIBE_FETCH $vmname" "VIBE_FETCH $fid /home/vibe/.vm-update/$vmname"
  run_job "install $vmname" "sudo install -o $owner -g $owner -m $mode /home/vibe/.vm-update/$vmname $target"
}

agent_alive() {
  # probe job: the agent only answers when endpoint and token work
  local id out status rc
  id="$(queue_job "echo vm-install-probe")"
  out="$(wait_job "$id")"
  status="${out%% *}"
  rc="${out##* }"
  info "Lebensprobe des Agents: $status rc=$rc"
  [ "$status" = "done" ] && [ "$rc" = "0" ]
}

manual_instructions() {
  info ""
  info "Der Agent beantwortet keine Jobs (Endpoint-/Token-Problem oder VM gerade"
  info "ohne funktionierenden Agent). manueller Weg über die serielle Konsole:"
  info ""
  info "  virsh --connect $LIBVIRT_URI console $VM_NAME    (Autologin vibe, sudo frei)"
  info ""
  info "Dann in der VM (Dateien direkt vom öffentlichen Repo holen):"
  info ""
  local dom="${DOMAIN:-<relay-domain>}"
  cat <<EOF
    mkdir -p /tmp/vm-update
    curl -fsSL $RAW_BASE/files/agent-sandbox-agent -o /tmp/vm-update/agent
    curl -fsSL $RAW_BASE/files/vmadmin            -o /tmp/vm-update/vmadmin
    curl -fsSL $RAW_BASE/files/agent-sandbox-agent.service -o /tmp/vm-update/agent.service
    curl -fsSL $RAW_BASE/files/vm-admin.service    -o /tmp/vm-update/vm-admin.service
    # Agent-Platzhalter ersetzen (Endpoint des Pull-Agents):
    sed -i 's#<agent-sandbox-relay-domain>#<deine-relay-domain>#' /tmp/vm-update/agent
    sudo install -o vibe -g vibe -m 0700 /tmp/vm-update/agent   /usr/local/bin/agent-sandbox-agent
    sudo install -o vibe -g vibe -m 0700 /tmp/vm-update/vmadmin /usr/local/bin/vmadmin
    sudo install -o root -g root -m 0644 /tmp/vm-update/agent.service /etc/systemd/system/agent-sandbox-agent.service
    sudo install -o root -g root -m 0644 /tmp/vm-update/vm-admin.service /etc/systemd/system/vm-admin.service
    sudo systemctl daemon-reload
    sudo systemctl restart vm-admin.service
    sudo systemctl restart agent-sandbox-agent
EOF
  info ""
  info "Worker-Token danach über die vmadmin-UI setzen (Datei geht der Konstante vor)."
  info "Alternativ Neu-Anlage: $0 --recreate --yes --domain <relay-domain> ... (LÖSCHT ALLE VM-DATEN)."
}

render_agent() {
  # agent file: replace the placeholder endpoint only if it is still in there
  local agent="$WORKDIR/files_agent-sandbox-agent"
  if grep -q '<agent-sandbox-relay-domain>' "$agent"; then
    [ -n "$DOMAIN" ] || die "Der Agent enthält noch den Domain-Platzhalter (Fehler der 15. Runde); --domain ist für das Update Pflicht."
    local content
    content="$(cat "$agent")"
    content="${content//<agent-sandbox-relay-domain>/$DOMAIN}"
    printf '%s' "$content" > "$WORKDIR/agent.rendered"
    info "Domain-Platzhalter im Agent ersetzt (Endpoint $DOMAIN)."
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
  render_agent

  if [ -z "$RELAY" ] || [ -z "$SUBMIT" ]; then
    info "--relay/--submitter-token fehlen - kein Update über die Job-Queue möglich."
    manual_instructions
    exit 2
  fi
  need_cmd jq
  need_cmd curl
  info "Update über die Job-Queue (Daten der VM bleiben unberührt) ..."
  if ! agent_alive; then
    manual_instructions
    exit 3
  fi
  run_job "Update-Verzeichnis" "mkdir -p /home/vibe/.vm-update"

  # 1) vmadmin (runs as vibe, port 80) - restart after install
  ship_file "$WORKDIR/files_vmadmin" "vmadmin" "/usr/local/bin/vmadmin" "vibe" "0700"
  # 2) units (no placeholders; root:root 0644 like cloud-init write_files defaults)
  ship_file "$WORKDIR/files_agent-sandbox-agent.service" "agent.service" \
    "/etc/systemd/system/agent-sandbox-agent.service" "root" "0644"
  ship_file "$WORKDIR/files_vm-admin.service" "vm-admin.service" \
    "/etc/systemd/system/vm-admin.service" "root" "0644"
  run_job "daemon-reload" "sudo systemctl daemon-reload"
  if [ -n "$TTYD_PW" ]; then
    # only with an explicit password: unit carries it in ExecStart
    local unit
    unit="$(cat "$WORKDIR/files_ttyd-vibe.service")"
    unit="${unit//<ttyd-passwort>/$TTYD_PW}"
    printf '%s' "$unit" > "$WORKDIR/ttyd-unit.rendered"
    ship_file "$WORKDIR/ttyd-unit.rendered" "ttyd-vibe.service" \
      "/etc/systemd/system/ttyd-vibe.service" "root" "0644"
    run_job "daemon-reload (ttyd)" "sudo systemctl daemon-reload"
    run_job "ttyd neu starten" "sudo systemctl restart ttyd-vibe.service"
  else
    info "  ttyd-vibe.service unberührt (Passwort steckt in der Unit; --ttyd-password zum Ändern)."
  fi
  # 3) vmadmin restart (not part of the job chain of the agent)
  run_job "vm-admin neu starten" "sudo systemctl restart vm-admin.service"
  # 4) agent last; delayed restart so this job can still post its result
  ship_file "$WORKDIR/agent.rendered" "agent" "/usr/local/bin/agent-sandbox-agent" "vibe" "0700"
  run_job "Agent-Restart (verzögert)" \
    "sudo sh -c 'nohup sh -c \"sleep 3; systemctl restart agent-sandbox-agent\" >/dev/null 2>&1 &'"
  info "Update abgeschlossen. Worker-Token, vmadmin-Hash und ttyd-Passwort wurden nicht angetastet."
  info "Agent startet nach ca. 3 s neu; Funktion prüfen: vmadmin-UI (Status) oder das Agent-Journal."
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

main() {
  parse_args "$@"
  fetch_repo_files
  if [ "$DRY_RUN" -eq 0 ]; then
    need_cmd virsh
  fi
  if [ "$DRY_RUN" -eq 1 ]; then
    if command -v virsh >/dev/null 2>&1 && vm_exists; then
      info "Dry-Run: VM $VM_NAME existiert - es würde der Update-Pfad laufen (hier: nur Rendering, keine Jobs)."
      render_agent
      if [ -n "$RELAY" ]; then
        info "Update würde über die Job-Queue laufen: $RELAY (Lebensprobe, dann Datei-Jobs, Agent-Restart zuletzt)."
      else
        info "ohne --relay/--submitter-token: nur die manuelle Anleitung, keine Jobs."
      fi
      return 0
    fi
    info "Dry-Run: keine VM-Anlage, keine Änderung an Bestehendem."
    install_vm
    return 0
  fi
  if vm_exists; then
    if [ "$RECREATE" -eq 1 ]; then
      confirm_recreate
      virsh_cmd undefine "$VM_NAME" || die "virsh undefine fehlgeschlagen"
      rm -f -- "$DISK" || die "Overlay konnte nicht gelöscht werden: $DISK"
      info "Alte VM verworfen, Overlay gelöscht - Neu-Anlage läuft."
      install_vm
    else
      info "VM $VM_NAME existiert - Update-Pfad."
      update_vm
    fi
  else
    install_vm
  fi
}

main "$@"
