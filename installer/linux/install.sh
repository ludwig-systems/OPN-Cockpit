#!/usr/bin/env bash
# OPN-Cockpit: Linux-Installation als systemd-Service.
#
# Funktioniert auf Debian 12 / Ubuntu 22.04+. Muss als root laufen.
#
# Modi:
#   ./install.sh                  - klont aktuelles Repo + installiert
#   ./install.sh --source /path/to/local-repo  - nutzt lokalen Clone
#
# Was passiert:
#   - User 'opncockpit' (uid wird auto-allokiert)
#   - Verzeichnisse: /opt/opn-cockpit, /var/lib/opn-cockpit
#   - Python venv + pip install -e .
#   - systemd-Unit aktiviert, gestartet
#   - Bind auf 0.0.0.0:9876 (Default), Daten in /var/lib/opn-cockpit

set -euo pipefail

REPO_URL="${OPNCOCKPIT_REPO_URL:-${OPNCOCKPIT_REPO:-https://github.com/ludwig-systems/opn-cockpit.git}}"
REPO_BRANCH="${OPNCOCKPIT_REPO_BRANCH:-main}"
INSTALL_DIR="${OPNCOCKPIT_INSTALL_DIR:-/opt/opn-cockpit}"
DATA_DIR="${OPNCOCKPIT_DATA_DIR:-/var/lib/opn-cockpit}"
SVC_USER="opncockpit"

err() { echo "ERROR: $*" >&2; exit 1; }
log() { echo "[opn-cockpit] $*"; }

# ---------------------------------------------------------------------------
# Argumente
# ---------------------------------------------------------------------------
SOURCE_DIR=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --source) SOURCE_DIR="$2"; shift 2;;
        *) err "Unbekanntes Argument: $1";;
    esac
done

[[ $EUID -eq 0 ]] || err "Bitte als root ausfuehren (sudo)."

# ---------------------------------------------------------------------------
# Pakete
# ---------------------------------------------------------------------------
log "Pakete installieren (python3, venv, git)..."
apt-get update -qq
apt-get install -y --no-install-recommends \
    python3 python3-venv python3-dev build-essential git ca-certificates \
    >/dev/null

# ---------------------------------------------------------------------------
# User + Verzeichnisse
# ---------------------------------------------------------------------------
if ! id "$SVC_USER" &>/dev/null; then
    log "Service-User '$SVC_USER' anlegen..."
    useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$SVC_USER"
fi

mkdir -p "$INSTALL_DIR" "$DATA_DIR"
chown -R "$SVC_USER:$SVC_USER" "$DATA_DIR"

# ---------------------------------------------------------------------------
# Source holen
# ---------------------------------------------------------------------------
if [[ -n "$SOURCE_DIR" ]]; then
    log "Source aus '$SOURCE_DIR' kopieren..."
    apt-get install -y --no-install-recommends rsync >/dev/null
    rsync -a --exclude='.git' --exclude='.venv' --exclude='installer/bundle' \
          --exclude='__pycache__' "$SOURCE_DIR/" "$INSTALL_DIR/"
elif [[ -d "$INSTALL_DIR/.git" ]]; then
    log "Repo unter '$INSTALL_DIR' auf Branch '$REPO_BRANCH' aktualisieren..."
    git -C "$INSTALL_DIR" fetch --depth 1 origin "$REPO_BRANCH"
    git -C "$INSTALL_DIR" reset --hard "origin/$REPO_BRANCH"
else
    log "Repo nach '$INSTALL_DIR' klonen (Branch '$REPO_BRANCH')..."
    git clone --depth 1 --branch "$REPO_BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi

chown -R "$SVC_USER:$SVC_USER" "$INSTALL_DIR"

# ---------------------------------------------------------------------------
# venv + dependencies
# ---------------------------------------------------------------------------
log "Python venv + Dependencies (kann eine Minute dauern)..."
su -s /bin/bash "$SVC_USER" -c "cd $INSTALL_DIR && \
    python3 -m venv .venv && \
    .venv/bin/pip install --quiet --upgrade pip && \
    .venv/bin/pip install --quiet -e ."

# ---------------------------------------------------------------------------
# systemd-Unit
# ---------------------------------------------------------------------------
log "systemd-Unit installieren..."
cp "$INSTALL_DIR/installer/linux/opn-cockpit.service" /etc/systemd/system/
# DATA_DIR im Service-File anpassen (Default ist /var/lib/opn-cockpit).
sed -i "s|/var/lib/opn-cockpit|$DATA_DIR|g" /etc/systemd/system/opn-cockpit.service

systemctl daemon-reload
systemctl enable --now opn-cockpit.service

# ---------------------------------------------------------------------------
# Smoke-Test
# ---------------------------------------------------------------------------
sleep 3
if systemctl is-active --quiet opn-cockpit.service; then
    HOST_IP=$(hostname -I | awk '{print $1}')
    echo
    log "Installation fertig. Service laeuft."
    echo
    echo "  URL:           http://${HOST_IP}:9876"
    echo "  Default-Login: admin / OPN-Cockpit!  (Pflicht-PW-Wechsel beim Erst-Login)"
    echo
    echo "  Logs:          journalctl -u opn-cockpit -f"
    echo "  Stop:          systemctl stop opn-cockpit"
    echo "  Status:        systemctl status opn-cockpit"
    echo
    log "Im Setup-Wizard:"
    log "  - Default-Admin einloggen + neues PW vergeben (min. 12 Zeichen)"
    log "  - Vault-Pfad: /var/lib/opn-cockpit/firewalls.opnvault"
    log "  - 'Tresor neu anlegen' aktivieren, Master-PW vergeben"
else
    err "Service ist nicht aktiv. Logs: journalctl -u opn-cockpit -n 50"
fi
