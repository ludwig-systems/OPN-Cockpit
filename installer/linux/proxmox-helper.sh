#!/usr/bin/env bash
# Proxmox-Helper-Skript fuer OPN-Cockpit.
#
# Laeuft auf einem Proxmox-VE-Host und legt automatisch einen Debian-12-
# LXC-Container an, in dem OPN-Cockpit als systemd-Service installiert wird.
#
# Aufruf:
#   bash -c "$(wget -qLO - https://raw.githubusercontent.com/your-org/opn-cockpit/main/installer/linux/proxmox-helper.sh)"
#
# Pattern nach community-scripts/ProxmoxVE, aber bewusst minimal — du hast
# die volle Kontrolle ueber die Schritte, kein verstecktes "phone home".

set -euo pipefail

APP="opn-cockpit"
CT_HOSTNAME="${CT_HOSTNAME:-opn-cockpit}"
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

# CT-ID frei finden
NEXTID=$(pvesh get /cluster/nextid)
read -rp "Container-ID [$NEXTID]: " CT_ID
CT_ID="${CT_ID:-$NEXTID}"

# Storage-Backend ermitteln
mapfile -t STORAGES < <(pvesm status -content rootdir | awk 'NR>1 {print $1}')
if [[ ${#STORAGES[@]} -eq 0 ]]; then
    err "Kein Storage mit content=rootdir verfuegbar."
fi
DEFAULT_STORAGE="${STORAGES[0]}"
echo "Verfuegbare Storage-Pools: ${STORAGES[*]}"
read -rp "Storage [$DEFAULT_STORAGE]: " CT_STORAGE
CT_STORAGE="${CT_STORAGE:-$DEFAULT_STORAGE}"

# Resources
read -rp "Disk-Groesse in GB [$DEFAULT_DISK_GB]: " CT_DISK
CT_DISK="${CT_DISK:-$DEFAULT_DISK_GB}"

read -rp "CPUs [$DEFAULT_CPU]: " CT_CPU
CT_CPU="${CT_CPU:-$DEFAULT_CPU}"

read -rp "RAM in MB [$DEFAULT_RAM_MB]: " CT_RAM
CT_RAM="${CT_RAM:-$DEFAULT_RAM_MB}"

read -rp "Netzwerk-Bridge [$DEFAULT_NETWORK]: " CT_NETWORK
CT_NETWORK="${CT_NETWORK:-$DEFAULT_NETWORK}"

echo
log "Konfiguration:"
echo "  ID:       $CT_ID"
echo "  Name:     $CT_HOSTNAME"
echo "  Storage:  $CT_STORAGE"
echo "  Disk:     ${CT_DISK} GB"
echo "  CPUs:     $CT_CPU"
echo "  RAM:      ${CT_RAM} MB"
echo "  Netz:     $CT_NETWORK (DHCP)"
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
pct create "$CT_ID" "${TPL_STORAGE}:vztmpl/$TEMPLATE" \
    --hostname "$CT_HOSTNAME" \
    --cores "$CT_CPU" \
    --memory "$CT_RAM" \
    --rootfs "${CT_STORAGE}:${CT_DISK}" \
    --net0 "name=eth0,bridge=${CT_NETWORK},ip=dhcp" \
    --features "nesting=1" \
    --unprivileged 1 \
    --onboot 1 \
    >/dev/null

log "Container starten..."
pct start "$CT_ID"

# Warten bis IP da ist
log "Auf Netzwerk warten..."
for _ in {1..30}; do
    CT_IP=$(pct exec "$CT_ID" -- hostname -I 2>/dev/null | awk '{print $1}' || true)
    [[ -n "${CT_IP:-}" ]] && break
    sleep 1
done
[[ -n "${CT_IP:-}" ]] || err "Container hat keine IP bekommen."

# ---------------------------------------------------------------------------
# OPN-Cockpit installieren (im Container)
# ---------------------------------------------------------------------------
log "OPN-Cockpit im Container installieren..."
pct exec "$CT_ID" -- bash -c "
set -e
apt-get update -qq
apt-get install -y --no-install-recommends curl ca-certificates >/dev/null
curl -fsSL https://raw.githubusercontent.com/your-org/opn-cockpit/main/installer/linux/install.sh -o /tmp/install.sh
chmod +x /tmp/install.sh
/tmp/install.sh
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
warn "Beim ersten Aufruf der UI: 'Neuen Tresor anlegen', Master-Passwort vergeben."
warn "Tresor liegt unter /var/lib/opn-cockpit im Container."
