#!/usr/bin/env bash
# Proxmox-Helper-Skript fuer OPN-Cockpit.
#
# Laeuft auf einem Proxmox-VE-Host und legt automatisch einen Debian-12-
# LXC-Container an, in dem OPN-Cockpit als systemd-Service installiert wird.
#
# Aufruf:
#   bash -c "$(wget -qLO - https://raw.githubusercontent.com/ludwig-systems/opn-cockpit/main/installer/linux/proxmox-helper.sh)"
#
# Pattern angelehnt an community-scripts.org/ProxmoxVE: whiptail-TUI mit
# echten Menus fuer Storage + Bridge — kein Frei-Text mehr, du kannst nur
# aus tatsaechlich vorhandenen Ressourcen waehlen.
#
# Konfiguration via Env-Variablen (alles optional):
#   OPNCOCKPIT_REPO_URL     - Git-URL (Default: ludwig-systems/opn-cockpit)
#   OPNCOCKPIT_REPO_BRANCH  - Branch (Default: main)
#   CT_HOSTNAME             - Container-Hostname (Default: opn-cockpit)
#
# Beispiel mit eigenem Fork/Branch:
#   OPNCOCKPIT_REPO_URL=https://github.com/foo/opn-cockpit.git \
#   OPNCOCKPIT_REPO_BRANCH=feature/xyz \
#   bash -c "$(wget -qLO - .../proxmox-helper.sh)"

set -euo pipefail

APP="opn-cockpit"
CT_HOSTNAME_DEFAULT="${CT_HOSTNAME:-opn-cockpit}"
REPO_URL="${OPNCOCKPIT_REPO_URL:-https://github.com/ludwig-systems/opn-cockpit.git}"
REPO_BRANCH="${OPNCOCKPIT_REPO_BRANCH:-main}"
DEFAULT_DISK_GB="2"
DEFAULT_CPU="1"
DEFAULT_RAM_MB="512"

BACKTITLE="OPN-Cockpit Installer | $REPO_BRANCH"

# Farben fuer Konsolen-Ausgabe (Whiptail uebernimmt sein eigenes Styling)
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log() { echo -e "${GREEN}[$APP]${NC} $*"; }
warn() { echo -e "${YELLOW}WARN: $*${NC}"; }

err() {
    # Versuche zuerst Whiptail-Msgbox, fallback Konsole
    if command -v whiptail &>/dev/null; then
        whiptail --backtitle "$BACKTITLE" --title "Fehler" --msgbox "$*" 12 78 || true
    fi
    echo -e "${RED}ERROR: $*${NC}" >&2
    exit 1
}

# ---------------------------------------------------------------------------
# Voraussetzungen
# ---------------------------------------------------------------------------
[[ $EUID -eq 0 ]] || err "Bitte auf dem Proxmox-Host als root ausfuehren."
command -v pveam &>/dev/null || err "pveam fehlt. Dieses Skript laeuft nur auf Proxmox VE."

# whiptail sollte auf Debian/Proxmox immer da sein — defensiv nachziehen
if ! command -v whiptail &>/dev/null; then
    log "whiptail wird installiert..."
    apt-get update -qq
    apt-get install -y --no-install-recommends whiptail >/dev/null
fi

# ---------------------------------------------------------------------------
# Wizard-Helpers — REPLY-Pattern, damit Abbruch via Esc/Cancel sauber endet
# ---------------------------------------------------------------------------
ASK() {
    # $1=title, $2=prompt, $3=default
    REPLY=$(whiptail --backtitle "$BACKTITLE" --title "$1" \
        --inputbox "$2" 10 78 "$3" 3>&1 1>&2 2>&3) || err "Abgebrochen."
}

ASK_MENU() {
    # $1=title, $2=prompt, $3..=tag1 desc1 tag2 desc2 ...
    local title=$1 prompt=$2
    shift 2
    REPLY=$(whiptail --backtitle "$BACKTITLE" --title "$title" \
        --menu "$prompt" 22 78 12 "$@" 3>&1 1>&2 2>&3) || err "Abgebrochen."
}

# ---------------------------------------------------------------------------
# Wizard
# ---------------------------------------------------------------------------
whiptail --backtitle "$BACKTITLE" --title "OPN-Cockpit Installer" --msgbox \
"OPN-Cockpit Proxmox-Container-Installation

Dieser Wizard legt einen Debian-12-LXC-Container an, installiert
darin OPN-Cockpit als systemd-Service und meldet am Ende die IP
+ den Default-Admin-Login.

Du kannst mit Esc jederzeit abbrechen." 16 78

# ----- Container-ID -----
NEXTID=$(pvesh get /cluster/nextid)
ASK "Container-ID" "ID des neuen LXC-Containers" "$NEXTID"
[[ "$REPLY" =~ ^[0-9]+$ ]] || err "Container-ID muss eine Zahl sein: $REPLY"
CT_ID="$REPLY"

# ----- Hostname -----
ASK "Hostname" "Hostname / CT-Name" "$CT_HOSTNAME_DEFAULT"
[[ -n "$REPLY" ]] || err "Hostname darf nicht leer sein."
CT_HOSTNAME="$REPLY"

# ----- Storage-Pool -----
STORAGE_ITEMS=()
while IFS= read -r line; do
    name=$(echo "$line" | awk '{print $1}')
    type=$(echo "$line" | awk '{print $2}')
    avail_kib=$(echo "$line" | awk '{print $5}')
    if [[ "$avail_kib" =~ ^[0-9]+$ ]]; then
        avail_gb=$((avail_kib / 1024 / 1024))
        descr="$type | ~${avail_gb} GB frei"
    else
        descr="$type"
    fi
    STORAGE_ITEMS+=("$name" "$descr")
done < <(pvesm status -content rootdir 2>/dev/null | awk 'NR>1')

if [[ ${#STORAGE_ITEMS[@]} -eq 0 ]]; then
    err "Kein Storage mit Content 'Container' verfuegbar.\n\nIn Proxmox-UI: Datacenter -> Storage -> Pool auswaehlen -> Edit -> 'Content' -> 'Container' anhaken."
fi

ASK_MENU "Storage-Pool" \
"Storage-Pool fuer den Container-Disk waehlen.\nNur Pools mit Content=Container werden gelistet." \
"${STORAGE_ITEMS[@]}"
CT_STORAGE="$REPLY"

# ----- Disk-Groesse -----
ASK "Disk-Groesse (GB)" "Wie viel Speicher soll der Container bekommen?" "$DEFAULT_DISK_GB"
[[ "$REPLY" =~ ^[0-9]+$ ]] || err "Disk-Groesse muss eine Zahl sein: $REPLY"
CT_DISK="$REPLY"

# ----- CPUs -----
ASK "CPUs" "Anzahl CPU-Kerne" "$DEFAULT_CPU"
[[ "$REPLY" =~ ^[0-9]+$ ]] || err "CPUs muss eine Zahl sein: $REPLY"
CT_CPU="$REPLY"

# ----- RAM -----
ASK "RAM (MB)" "Arbeitsspeicher in MB" "$DEFAULT_RAM_MB"
[[ "$REPLY" =~ ^[0-9]+$ ]] || err "RAM muss eine Zahl sein: $REPLY"
CT_RAM="$REPLY"

# ----- Netzwerk-Bridge -----
BRIDGE_ITEMS=()
while IFS= read -r br; do
    BRIDGE_ITEMS+=("$br" "Linux Bridge")
done < <(ip -br link show 2>/dev/null | awk '$1 ~ /^vmbr/ {print $1}')

if [[ ${#BRIDGE_ITEMS[@]} -eq 0 ]]; then
    err "Keine vmbr* Bridge gefunden. Erst eine Linux Bridge in Proxmox anlegen."
fi
ASK_MENU "Netzwerk-Bridge" \
"Auf welcher Bridge soll der Container haengen?" \
"${BRIDGE_ITEMS[@]}"
CT_NETWORK="$REPLY"

# ----- IP-Modus -----
ASK_MENU "IP-Konfiguration" \
"Wie soll die IPv4-Adresse vergeben werden?" \
"dhcp"   "DHCP (empfohlen wenn DHCP-Server im LAN)" \
"static" "Statisch (CIDR + Gateway eingeben)"
IP_MODE="$REPLY"

IPV4_CIDR=""
IPV4_GW=""
if [[ "$IP_MODE" == "static" ]]; then
    ASK "IPv4-Adresse + CIDR" "Beispiel: 192.168.1.100/24" ""
    [[ "$REPLY" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/[0-9]+$ ]] || err "IPv4-CIDR ungueltig: $REPLY"
    IPV4_CIDR="$REPLY"
    ASK "Gateway" "Gateway-IPv4-Adresse" ""
    [[ "$REPLY" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || err "Gateway-IPv4 ungueltig: $REPLY"
    IPV4_GW="$REPLY"
fi

# ----- Optional: VLAN, MAC, DNS, Search -----
ASK "VLAN-Tag (optional)" "VLAN-ID (leer = kein VLAN)" ""
VLAN_TAG="$REPLY"

ASK "MAC-Adresse (optional)" "MAC-Adresse (leer = Proxmox waehlt automatisch)" ""
MAC_ADDR="$REPLY"

ASK "DNS-Server (optional)" "DNS-Server, kommagetrennt (leer = vom Host uebernehmen)" ""
DNS_SERVERS="$REPLY"

ASK "DNS-Search-Domain (optional)" "Search-Domain (leer = keine)" ""
DNS_SEARCH="$REPLY"

# ----- Net-String fuer pct zusammenbauen -----
NET_STR="name=eth0,bridge=$CT_NETWORK"
if [[ -n "$IPV4_CIDR" ]]; then
    NET_STR="$NET_STR,ip=$IPV4_CIDR,gw=$IPV4_GW"
else
    NET_STR="$NET_STR,ip=dhcp"
fi
[[ -n "$VLAN_TAG" ]] && NET_STR="$NET_STR,tag=$VLAN_TAG"
[[ -n "$MAC_ADDR" ]] && NET_STR="$NET_STR,hwaddr=$MAC_ADDR"

# ----- Zusammenfassung + Bestaetigung -----
if [[ -n "$IPV4_CIDR" ]]; then
    NET_SUMMARY="$CT_NETWORK | $IPV4_CIDR via $IPV4_GW"
else
    NET_SUMMARY="$CT_NETWORK | DHCP"
fi
[[ -n "$VLAN_TAG" ]] && NET_SUMMARY="$NET_SUMMARY, VLAN $VLAN_TAG"
[[ -n "$MAC_ADDR" ]] && NET_SUMMARY="$NET_SUMMARY, MAC $MAC_ADDR"

SUMMARY="Container
  ID:        $CT_ID
  Hostname:  $CT_HOSTNAME
  Storage:   $CT_STORAGE
  Disk:      ${CT_DISK} GB
  CPUs:      $CT_CPU
  RAM:       ${CT_RAM} MB

Netzwerk
  $NET_SUMMARY"
[[ -n "$DNS_SERVERS" ]] && SUMMARY="$SUMMARY
  DNS:       $DNS_SERVERS"
[[ -n "$DNS_SEARCH" ]] && SUMMARY="$SUMMARY
  Search:    $DNS_SEARCH"

SUMMARY="$SUMMARY

Repository
  $REPO_URL
  Branch:    $REPO_BRANCH

Container anlegen und OPN-Cockpit installieren?"

whiptail --backtitle "$BACKTITLE" --title "Zusammenfassung" \
    --yesno "$SUMMARY" 24 78 || err "Abgebrochen."

# ---------------------------------------------------------------------------
# Debian-12-Template sicherstellen
# ---------------------------------------------------------------------------
log "Debian-12-Template sicherstellen..."
TEMPLATE=$(pveam available --section system | awk '/debian-12-standard/{print $2}' | tail -1)
[[ -n "$TEMPLATE" ]] || err "Kein Debian-12-Template im pveam-Katalog."

mapfile -t TPL_STORAGES < <(pvesm status -content vztmpl | awk 'NR>1 {print $1}')
TPL_STORAGE="${TPL_STORAGES[0]:-local}"

LOCAL_TPL="/var/lib/vz/template/cache/$TEMPLATE"
if [[ ! -f "$LOCAL_TPL" ]]; then
    log "Template herunterladen: $TEMPLATE..."
    pveam download "$TPL_STORAGE" "$TEMPLATE"
fi

# ---------------------------------------------------------------------------
# Container anlegen + starten
# ---------------------------------------------------------------------------
log "LXC-Container $CT_ID anlegen..."
PCT_ARGS=(
    --hostname "$CT_HOSTNAME"
    --cores "$CT_CPU"
    --memory "$CT_RAM"
    --rootfs "${CT_STORAGE}:${CT_DISK}"
    --net0 "$NET_STR"
    --features "nesting=1"
    --unprivileged 1
    --onboot 1
)
[[ -n "$DNS_SERVERS" ]] && PCT_ARGS+=(--nameserver "$DNS_SERVERS")
[[ -n "$DNS_SEARCH"  ]] && PCT_ARGS+=(--searchdomain "$DNS_SEARCH")

pct create "$CT_ID" "${TPL_STORAGE}:vztmpl/$TEMPLATE" "${PCT_ARGS[@]}" >/dev/null

log "Container starten..."
pct start "$CT_ID"

log "Auf Netzwerk warten..."
for _ in {1..30}; do
    CT_IP=$(pct exec "$CT_ID" -- hostname -I 2>/dev/null | awk '{print $1}' || true)
    [[ -n "${CT_IP:-}" ]] && break
    sleep 1
done
[[ -n "${CT_IP:-}" ]] || err "Container hat keine IP bekommen. Pruefe Netz-Konfiguration."

# ---------------------------------------------------------------------------
# OPN-Cockpit installieren (im Container)
# ---------------------------------------------------------------------------
log "OPN-Cockpit im Container installieren..."

RAW_INSTALL_SH=$(echo "$REPO_URL" |
    sed -E 's|^(https?://)github\.com/([^/]+)/([^/.]+)(\.git)?/?$|\1raw.githubusercontent.com/\2/\3|')
RAW_INSTALL_SH="${RAW_INSTALL_SH}/${REPO_BRANCH}/installer/linux/install.sh"

pct exec "$CT_ID" -- bash -c "
set -e
apt-get update -qq
apt-get install -y --no-install-recommends curl ca-certificates >/dev/null
curl -fsSL '${RAW_INSTALL_SH}' -o /tmp/install.sh
chmod +x /tmp/install.sh
OPNCOCKPIT_REPO_URL='${REPO_URL}' OPNCOCKPIT_REPO_BRANCH='${REPO_BRANCH}' /tmp/install.sh
"

# ---------------------------------------------------------------------------
# Fertig — sowohl Whiptail-Msgbox als auch Konsolen-Ausgabe
# ---------------------------------------------------------------------------
SUCCESS="OPN-Cockpit ist installiert und gestartet.

Container-ID:  $CT_ID
Hostname:      $CT_HOSTNAME
IP:            $CT_IP
URL:           http://${CT_IP}:9876

Default-Login: admin / OPN-Cockpit!
Beim ersten Login MUSS das Admin-PW gewechselt werden.

Befehle (auf Proxmox-Host):
  pct exec $CT_ID -- journalctl -u opn-cockpit -f
  pct enter $CT_ID
  pct stop $CT_ID"

whiptail --backtitle "$BACKTITLE" --title "Fertig" --msgbox "$SUCCESS" 22 78 || true

echo
log "Installation fertig."
echo
echo "  Container-ID:  $CT_ID"
echo "  Hostname:      $CT_HOSTNAME"
echo "  IP:            $CT_IP"
echo "  URL:           http://${CT_IP}:9876"
echo
echo "  Default-Login: admin / OPN-Cockpit!  (Pflicht-PW-Wechsel beim Erst-Login)"
echo
echo "  Logs:          pct exec $CT_ID -- journalctl -u opn-cockpit -f"
echo "  Shell:         pct enter $CT_ID"
echo "  Stop:          pct stop $CT_ID"
echo
