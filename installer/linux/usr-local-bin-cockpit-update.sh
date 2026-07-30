#!/bin/bash
# /usr/local/bin/cockpit-update — OPN-Cockpit-Update im Container
#
# Wird vom Proxmox-Helper (proxmox-helper.sh) beim Container-Create und
# bei jedem Update-Run installiert. Ein Symlink 'update' zeigt zusaetzlich
# hierher, damit die kurze Form auch funktioniert.
#
# Warum ein echtes Skript statt Shell-Alias (frueher unter
# /etc/profile.d/opn-cockpit.sh)?
#
# * Sofort verfuegbar — kein 'exit + pct enter'-Neustart der Login-Shell
#   noetig damit der Alias sichtbar wird.
# * Funktioniert auch aus 'pct exec <ct-id> -- update' vom Proxmox-Host.
# * Funktioniert in Non-Interactive-Shells / Skripten.
# * /usr/local/bin ist in jedem gaengigen $PATH.
#
# Das Skript ruft denselben Helper-One-Liner auf, den man beim ersten
# Setup manuell ausgefuehrt hat. Der Helper erkennt beim Start dass er
# im Container laeuft und geht in den Update-Modus.

set -euo pipefail

HELPER_URL="${OPNCOCKPIT_HELPER_URL:-https://raw.githubusercontent.com/ludwig-systems/opn-cockpit/main/installer/linux/proxmox-helper.sh}"

if ! command -v wget >/dev/null 2>&1; then
    echo "Fehler: 'wget' ist nicht installiert. Bitte 'apt install wget' ausfuehren." >&2
    exit 1
fi

exec bash -c "$(wget -qLO - "$HELPER_URL")"
