#!/usr/bin/env bash
# Proxmox-Helper-Skript fuer OPN-Cockpit.
#
# Dual-Mode wie bei community-scripts.org:
#
#   - Auf Proxmox-Host ausgefuehrt:
#     -> TUI-Wizard erzeugt einen frischen LXC-Container und installiert
#        OPN-Cockpit als systemd-Service darin.
#
#   - In einem Container mit existierender OPN-Cockpit-Installation:
#     -> Update-Modus: zieht die neue Code-Version aus dem Repo, laesst
#        ALLE User-Daten in /var/lib/opn-cockpit unangetastet (Tresor,
#        Audit, User-DB, Plans, Settings). Migrations laufen automatisch
#        beim naechsten Service-Start und schreiben dabei einen Backup-
#        Snapshot in <data>/backups/.
#
# Aufruf (beide Modi mit dem selben Link):
#   bash -c "$(wget -qLO - https://raw.githubusercontent.com/ludwig-systems/opn-cockpit/main/installer/linux/proxmox-helper.sh)"
#
# Wizard angelehnt an community-scripts.org/ProxmoxVE: whiptail-TUI mit
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
# Voraussetzungen + Mode-Detection
# ---------------------------------------------------------------------------
[[ $EUID -eq 0 ]] || err "Skript braucht root (im Container 'pct enter <CT-ID>' vom PVE-Host nutzen, das gibt direkt Root-Shell)."

# Mode: CREATE = auf Proxmox-Host (pveam existiert), UPDATE = im Container
# mit existierender Installation (kein pveam, /opt/opn-cockpit existiert)
if command -v pveam &>/dev/null; then
    MODE="create"
elif [[ -d /opt/opn-cockpit && -f /etc/systemd/system/opn-cockpit.service ]]; then
    MODE="update"
else
    err "Dieses Skript laeuft entweder auf einem Proxmox-VE-Host (Container anlegen) oder in einem Container mit existierender OPN-Cockpit-Installation (Update).\n\nGefunden weder pveam noch /opt/opn-cockpit."
fi

# whiptail sollte auf Debian/Proxmox immer da sein — defensiv nachziehen
if ! command -v whiptail &>/dev/null; then
    log "whiptail wird installiert..."
    apt-get update -qq
    apt-get install -y --no-install-recommends whiptail >/dev/null
fi

# ---------------------------------------------------------------------------
# Update-Modus (im Container) — komplett separater Pfad, exit nach Abschluss
# ---------------------------------------------------------------------------
if [[ "$MODE" == "update" ]]; then
    BACKTITLE="OPN-Cockpit Update | $REPO_BRANCH"

    log "Update-Modus erkannt (kein Proxmox-Host, Installation in /opt/opn-cockpit)."

    INSTALL_DIR="/opt/opn-cockpit"
    DATA_DIR="/var/lib/opn-cockpit"

    # Aktuelle Version aus __init__.py + Commit-Hash
    CURRENT_COMMIT=$(git -C "$INSTALL_DIR" rev-parse --short HEAD 2>/dev/null || echo "?")
    CURRENT_VERSION=$(grep -oP '__version__ = "\K[^"]+' \
        "$INSTALL_DIR/src/opn_cockpit/__init__.py" 2>/dev/null || echo "?")

    # TUI: Bestaetigen + explizit Code-vs-Daten klar machen.
    # Wichtig fuer User-Vertrauen: Admin-Login wird NICHT zurueckgesetzt.
    whiptail --backtitle "$BACKTITLE" --title "Update" --yesno \
"OPN-Cockpit-Installation gefunden.

Aktuell:    $CURRENT_VERSION  (Commit $CURRENT_COMMIT)
Repository: $REPO_URL
Branch:     $REPO_BRANCH

Aktualisiert (Code in $INSTALL_DIR):
  + Python-Code via git reset --hard
  + Python-Dependencies via pip install
  + Service-Stop und Service-Start
  + Schema-Migrations laufen automatisch und schreiben dabei
    einen Backup-Snapshot in $DATA_DIR/backups/<ts>-pre-<ver>/

UNVERAENDERT (User-Daten in $DATA_DIR):
  - firewalls.opnvault       (Tresor + alle Geraete + API-Keys)
  - users.db                 (Admin-Login + andere User)
  - audit.db, plans.db       (Historie + Plan-Reports)
  - settings.json

Dein Admin-Passwort bleibt das gleiche. Kein erneutes Default-
Setup, kein Force-PW-Wechsel. Nach dem Update loggst du dich
direkt mit deinen jetzigen Credentials weiter ein.

Update jetzt durchfuehren?" 30 78 || err "Abgebrochen."

    log "Service stoppen..."
    systemctl stop opn-cockpit.service

    log "Code aktualisieren (git fetch + reset)..."
    # runuser statt sudo -u: sudo ist in LXC-Minimal-Templates nicht
    # installiert, runuser ist Teil von util-linux und immer da.
    runuser -u opncockpit -- git -C "$INSTALL_DIR" fetch --depth 1 origin "$REPO_BRANCH"
    runuser -u opncockpit -- git -C "$INSTALL_DIR" reset --hard "origin/$REPO_BRANCH"

    log "Python-Dependencies aktualisieren..."
    runuser -u opncockpit -- "$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade -e "$INSTALL_DIR"

    log "Service starten (Migrations laufen automatisch)..."
    systemctl start opn-cockpit.service

    # Auf Service warten (max 10s)
    sleep 3
    for _ in {1..7}; do
        systemctl is-active --quiet opn-cockpit.service && break
        sleep 1
    done

    if systemctl is-active --quiet opn-cockpit.service; then
        NEW_COMMIT=$(git -C "$INSTALL_DIR" rev-parse --short HEAD 2>/dev/null || echo "?")
        NEW_VERSION=$(grep -oP '__version__ = "\K[^"]+' \
            "$INSTALL_DIR/src/opn_cockpit/__init__.py" 2>/dev/null || echo "?")
        HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}')

        whiptail --backtitle "$BACKTITLE" --title "Update fertig" --msgbox \
"Update erfolgreich.

Vorher:    $CURRENT_VERSION  (Commit $CURRENT_COMMIT)
Jetzt:     $NEW_VERSION  (Commit $NEW_COMMIT)

Service:   running
URL:       http://${HOST_IP}:9876

User-Daten und Login bleiben unveraendert. Du kannst dich direkt
mit deinem bestehenden Admin-Konto wieder einloggen." 18 78 || true

        log "Update fertig: $CURRENT_VERSION ($CURRENT_COMMIT) -> $NEW_VERSION ($NEW_COMMIT)"
    else
        err "Service ist nach Update nicht aktiv.\n\njournalctl -u opn-cockpit -n 50"
    fi

    exit 0
fi

# Ab hier: MODE == "create" — Original-Wizard fuer Proxmox-Host

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
# pvesm status-Spalten (stable seit Proxmox 6.x):
#   $1=Name $2=Type $3=Status $4=Total(KiB) $5=Used(KiB) $6=Available(KiB) $7=%
STORAGE_ITEMS=()
while IFS= read -r line; do
    name=$(echo "$line" | awk '{print $1}')
    type=$(echo "$line" | awk '{print $2}')
    total_kib=$(echo "$line" | awk '{print $4}')
    avail_kib=$(echo "$line" | awk '{print $6}')
    if [[ "$avail_kib" =~ ^[0-9]+$ && "$total_kib" =~ ^[0-9]+$ ]]; then
        avail_gb=$((avail_kib / 1024 / 1024))
        total_gb=$((total_kib / 1024 / 1024))
        descr="$type | ${avail_gb} GB frei / ${total_gb} GB total"
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

# ----- Root-Passwort (optional) -----
# Default ist KEIN Root-PW: Zugang ueber 'pct enter' vom Host reicht fuer
# Wartung. Wer SSH ins Container will, kann hier ein PW setzen.
CT_PASSWORD=""
while true; do
    CT_PASSWORD=$(whiptail --backtitle "$BACKTITLE" --title "Root-Passwort (optional)" \
        --passwordbox "Container-Root-Passwort\n\nLeer lassen = kein PW (Zugang nur per 'pct enter' vom Host, sicherste Variante)\nWert setzen = klassischer Root-Login (z.B. fuer SSH von aussen)" \
        14 78 3>&1 1>&2 2>&3) || err "Abgebrochen."

    if [[ -z "$CT_PASSWORD" ]]; then
        break
    fi
    if [[ ${#CT_PASSWORD} -lt 5 ]]; then
        whiptail --backtitle "$BACKTITLE" --title "Zu kurz" \
            --msgbox "Mindestens 5 Zeichen.\nLeer lassen geht auch (kein Root-PW)." 10 60
        continue
    fi
    CT_PASSWORD_CONFIRM=$(whiptail --backtitle "$BACKTITLE" --title "Wiederholen" \
        --passwordbox "Passwort wiederholen" 10 60 3>&1 1>&2 2>&3) || err "Abgebrochen."
    if [[ "$CT_PASSWORD" != "$CT_PASSWORD_CONFIRM" ]]; then
        whiptail --backtitle "$BACKTITLE" --title "Ungleich" \
            --msgbox "Passwoerter stimmen nicht ueberein, nochmal." 10 60
        CT_PASSWORD=""
        continue
    fi
    break
done

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

if [[ -n "$CT_PASSWORD" ]]; then
    PW_SUMMARY="gesetzt (Root-Login per Console/SSH moeglich)"
else
    PW_SUMMARY="kein (Zugang nur per 'pct enter' vom Host)"
fi

SUMMARY="Container
  ID:        $CT_ID
  Hostname:  $CT_HOSTNAME
  Root-PW:   $PW_SUMMARY
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
# pveam-Katalog frisch ziehen — sonst zeigt 'pveam available' eine alte
# Version, deren tar.zst-URL bei Proxmox laengst weg ist (404 beim Download).
log "Template-Katalog aktualisieren..."
pveam update >/dev/null 2>&1 || warn "pveam update fehlgeschlagen — alter Katalog wird verwendet."

log "Aktuelles Debian-12-Template ermitteln..."
TEMPLATE=$(pveam available --section system | awk '/debian-12-standard/{print $2}' | tail -1)
[[ -n "$TEMPLATE" ]] || err "Kein Debian-12-Template im pveam-Katalog."

# Template-Storage waehlen: bevorzugt 'local' (Standard-Proxmox-Setup), sonst
# der erste vztmpl-faehige Pool. Verhindert dass das Template auf einer
# USB-Backup-Platte landet die der User nur fuer Backups eingebunden hat.
log "Template-Storage auswaehlen..."
mapfile -t TPL_STORAGES < <(pvesm status -content vztmpl 2>/dev/null | awk 'NR>1 {print $1}')
if [[ ${#TPL_STORAGES[@]} -eq 0 ]]; then
    err "Kein Storage mit Content 'CT Templates' verfuegbar."
fi

TPL_STORAGE=""
for candidate in "${TPL_STORAGES[@]}"; do
    [[ "$candidate" == "local" ]] && { TPL_STORAGE="local"; break; }
done
# Wenn 'local' nicht in der Liste war, nimm den ersten Eintrag.
: "${TPL_STORAGE:=${TPL_STORAGES[0]}}"

log "Template-Storage: $TPL_STORAGE"

# Template prueft pveam list — pfad-basierter Check funktionierte nur fuer
# 'local' (/var/lib/vz/template/cache). pveam list ist Storage-agnostisch.
if ! pveam list "$TPL_STORAGE" 2>/dev/null | awk '{print $1}' | grep -qF "$TEMPLATE"; then
    log "Template herunterladen: $TEMPLATE -> $TPL_STORAGE..."
    pveam download "$TPL_STORAGE" "$TEMPLATE"
else
    log "Template bereits vorhanden auf $TPL_STORAGE."
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
[[ -n "$CT_PASSWORD" ]] && PCT_ARGS+=(--password "$CT_PASSWORD")

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
if [[ -n "$CT_PASSWORD" ]]; then
    PW_HINT="Container-Root-PW: dein gewaehltes Passwort (auch fuer Console/SSH)"
else
    PW_HINT="Container-Root-Login: KEIN Passwort gesetzt - Zugang nur per 'pct enter $CT_ID' vom Host"
fi

SUCCESS="OPN-Cockpit ist installiert und gestartet.

Container-ID:  $CT_ID
Hostname:      $CT_HOSTNAME
IP:            $CT_IP
URL:           http://${CT_IP}:9876

OPN-Cockpit-Login: admin / OPN-Cockpit!
Beim ersten Login MUSS das Admin-PW gewechselt werden.

$PW_HINT

Befehle (auf Proxmox-Host):
  pct enter $CT_ID                               (Shell im Container)
  pct exec $CT_ID -- journalctl -u opn-cockpit -f  (Logs)
  pct stop $CT_ID                                (Stop)"

whiptail --backtitle "$BACKTITLE" --title "Fertig" --msgbox "$SUCCESS" 24 78 || true

echo
log "Installation fertig."
echo
echo "  Container-ID:  $CT_ID"
echo "  Hostname:      $CT_HOSTNAME"
echo "  IP:            $CT_IP"
echo "  URL:           http://${CT_IP}:9876"
echo
echo "  OPN-Cockpit-Login: admin / OPN-Cockpit!  (Pflicht-PW-Wechsel beim Erst-Login)"
echo "  $PW_HINT"
echo
echo "  Logs:          pct exec $CT_ID -- journalctl -u opn-cockpit -f"
echo "  Shell:         pct enter $CT_ID"
echo "  Stop:          pct stop $CT_ID"
echo
