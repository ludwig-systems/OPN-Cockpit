#!/usr/bin/env bash
# Proxmox-Helper-Skript fuer OPN-Cockpit.
#
# Laeuft auf einem Proxmox-VE-Host und legt automatisch einen Debian-12-
# LXC-Container an, in dem OPN-Cockpit als systemd-Service installiert wird.
#
# Aufruf:
#   bash -c "$(wget -qLO - https://raw.githubusercontent.com/ludwig-systems/opn-cockpit/main/installer/linux/proxmox-helper.sh)"
#
# Pattern nach community-scripts/ProxmoxVE, aber bewusst minimal — du hast
# die volle Kontrolle ueber die Schritte, kein verstecktes "phone home".
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
CT_HOSTNAME="${CT_HOSTNAME:-opn-cockpit}"
REPO_URL="${OPNCOCKPIT_REPO_URL:-https://github.com/ludwig-systems/opn-cockpit.git}"
REPO_BRANCH="${OPNCOCKPIT_REPO_BRANCH:-main}"
DEFAULT_DISK_GB="2"
DEFAULT_CPU="1"
DEFAULT_RAM_MB="512"
DEFAULT_NETWORK="vmbr0"

# Farben fuer hervorgehobene Hinweise
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

err() { echo -e "${RED}ERROR: $*${NC}" >&2; exit 1; }
log() { echo -e "${GREEN}[$APP]${NC} $*"; }
warn() { echo -e "${YELLOW}WARN: $*${NC}"; }

# ---------------------------------------------------------------------------
# Voraussetzungen
# ---------------------------------------------------------------------------
[[ $EUID -eq 0 ]] || err "Bitte auf dem Proxmox-Host als root ausfuehren."
command -v pveam &>/dev/null || err "pveam fehlt. Dieses Skript laeuft nur auf Proxmox VE."

# ---------------------------------------------------------------------------
# Interaktive Konfiguration
# ---------------------------------------------------------------------------
log "OPN-Cockpit Proxmox-Container-Installation"
echo
echo "Einfach Enter druecken um die [Default]-Werte zu uebernehmen."
echo

# ----- Container -----
NEXTID=$(pvesh get /cluster/nextid)
read -rp "Container-ID [$NEXTID]: " CT_ID
CT_ID="${CT_ID:-$NEXTID}"

read -rp "Hostname [$CT_HOSTNAME]: " HN
CT_HOSTNAME="${HN:-$CT_HOSTNAME}"

# ----- Storage -----
mapfile -t STORAGES < <(pvesm status -content rootdir | awk 'NR>1 {print $1}')
if [[ ${#STORAGES[@]} -eq 0 ]]; then
    err "Kein Storage mit content=rootdir verfuegbar."
fi
DEFAULT_STORAGE="${STORAGES[0]}"
echo "Verfuegbare Storage-Pools: ${STORAGES[*]}"
read -rp "Storage [$DEFAULT_STORAGE]: " CT_STORAGE
CT_STORAGE="${CT_STORAGE:-$DEFAULT_STORAGE}"

# ----- Ressourcen -----
read -rp "Disk-Groesse in GB [$DEFAULT_DISK_GB]: " CT_DISK
CT_DISK="${CT_DISK:-$DEFAULT_DISK_GB}"

read -rp "CPUs [$DEFAULT_CPU]: " CT_CPU
CT_CPU="${CT_CPU:-$DEFAULT_CPU}"

read -rp "RAM in MB [$DEFAULT_RAM_MB]: " CT_RAM
CT_RAM="${CT_RAM:-$DEFAULT_RAM_MB}"

# ----- Netzwerk -----
echo
log "Netzwerk-Konfiguration"

read -rp "Netzwerk-Bridge [$DEFAULT_NETWORK]: " CT_NETWORK
CT_NETWORK="${CT_NETWORK:-$DEFAULT_NETWORK}"

read -rp "IP-Konfiguration [d=DHCP / s=Statisch, Default d]: " IP_MODE
IP_MODE="${IP_MODE:-d}"

IPV4_CIDR=""
IPV4_GW=""
if [[ "$IP_MODE" =~ ^[sS]$ ]]; then
    read -rp "IPv4-Adresse mit CIDR (z. B. 192.168.1.100/24): " IPV4_CIDR
    [[ -n "$IPV4_CIDR" ]] || err "IPv4-Adresse fehlt."
    # Plausibilitaet: Muster a.b.c.d/nn — bewusst nicht zu streng,
    # Proxmox lehnt unbrauchbares spaeter eh ab.
    [[ "$IPV4_CIDR" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+/[0-9]+$ ]] ||
        err "IPv4-CIDR sieht nicht plausibel aus: $IPV4_CIDR"
    read -rp "Gateway-IPv4: " IPV4_GW
    [[ -n "$IPV4_GW" ]] || err "Gateway fehlt."
fi

read -rp "VLAN-Tag (leer = ohne VLAN): " VLAN_TAG
read -rp "MAC-Adresse (leer = Proxmox waehlt automatisch): " MAC_ADDR
read -rp "DNS-Server, kommagetrennt (leer = Host-DNS uebernehmen): " DNS_SERVERS
read -rp "DNS-Search-Domain (leer = keine): " DNS_SEARCH

# Net-String fuer pct zusammenbauen
NET_STR="name=eth0,bridge=$CT_NETWORK"
if [[ -n "$IPV4_CIDR" ]]; then
    NET_STR="$NET_STR,ip=$IPV4_CIDR,gw=$IPV4_GW"
else
    NET_STR="$NET_STR,ip=dhcp"
fi
[[ -n "$VLAN_TAG" ]] && NET_STR="$NET_STR,tag=$VLAN_TAG"
[[ -n "$MAC_ADDR" ]] && NET_STR="$NET_STR,hwaddr=$MAC_ADDR"

# ----- Zusammenfassung -----
if [[ -n "$IPV4_CIDR" ]]; then
    NET_SUMMARY="$CT_NETWORK | $IPV4_CIDR via $IPV4_GW (statisch)"
else
    NET_SUMMARY="$CT_NETWORK | DHCP"
fi
[[ -n "$VLAN_TAG" ]] && NET_SUMMARY="$NET_SUMMARY, VLAN $VLAN_TAG"
[[ -n "$MAC_ADDR" ]] && NET_SUMMARY="$NET_SUMMARY, MAC $MAC_ADDR"

echo
log "Konfiguration:"
echo "  ID:        $CT_ID"
echo "  Hostname:  $CT_HOSTNAME"
echo "  Storage:   $CT_STORAGE"
echo "  Disk:      ${CT_DISK} GB"
echo "  CPUs:      $CT_CPU"
echo "  RAM:       ${CT_RAM} MB"
echo "  Netz:      $NET_SUMMARY"
[[ -n "$DNS_SERVERS" ]] && echo "  DNS:       $DNS_SERVERS"
[[ -n "$DNS_SEARCH" ]]  && echo "  Search:    $DNS_SEARCH"
echo "  Repo:      $REPO_URL ($REPO_BRANCH)"
echo
read -rp "Fortfahren? [j/N] " GO
[[ "$GO" =~ ^[jJyY]$ ]] || err "Abgebrochen."

# ---------------------------------------------------------------------------
# Debian-12-Template sicherstellen
# ---------------------------------------------------------------------------
log "Debian-12-Template sicherstellen..."
TEMPLATE=$(pveam available --section system | awk '/debian-12-standard/{print $2}' | tail -1)
[[ -n "$TEMPLATE" ]] || err "Kein Debian-12-Template im pveam-Katalog."

# Template-Storage waehlen (irgendein vztmpl-faehiger Pool)
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
# pct create-Argumente in Array bauen, damit optionale Flags sauber
# uebergeben werden (sonst leere "" als Argument bei Shell).
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

# Warten bis IP da ist. Bei statischer IP brauchen wir streng genommen nicht
# zu warten (steht ja in der Config), pruefen aber trotzdem ob das Interface
# UP ist.
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
# Konvertiere Git-URL -> Raw-Content-URL fuer install.sh-Download.
# Funktioniert fuer github.com (auch private nicht — da brauchst du eh Token).
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
# Fertig
# ---------------------------------------------------------------------------
echo
log "Installation fertig."
echo
echo "  Container-ID:  $CT_ID"
echo "  Hostname:      $CT_HOSTNAME"
echo "  IP:            $CT_IP"
echo "  URL:           http://${CT_IP}:9876"
echo
echo "  Logs:          pct exec $CT_ID -- journalctl -u opn-cockpit -f"
echo "  Shell:         pct enter $CT_ID"
echo "  Stop:          pct stop $CT_ID"
echo "  Restart:       pct exec $CT_ID -- systemctl restart opn-cockpit"
echo
warn "Beim ersten Aufruf der UI:"
warn "  1. Default-Admin einloggen: admin / OPN-Cockpit!"
warn "  2. Neues Admin-Passwort vergeben (>= 12 Zeichen, Pflicht-Wechsel)"
warn "  3. Vault-Pfad: /var/lib/opn-cockpit/firewalls.opnvault"
warn "  4. 'Tresor neu anlegen' aktivieren, Master-Passwort vergeben"
