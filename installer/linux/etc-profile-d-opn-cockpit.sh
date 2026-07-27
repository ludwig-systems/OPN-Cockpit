# /etc/profile.d/opn-cockpit.sh — Convenience-Aliase im Container
#
# Wird vom Proxmox-Helper (proxmox-helper.sh) beim Container-Create und
# beim Update-Run in den Container kopiert. Wird von jeder interaktiven
# Login-Shell (bash, sh) automatisch geladen — Nutzer sehen den Alias
# beim naechsten Terminal-Öffnen bzw. nach `pct enter`.
#
# 'update' und 'cockpit-update' triggern den One-Liner, den der Nutzer
# beim allerersten Setup auf dem Proxmox-Host aufgerufen hat. Das Skript
# erkennt beim Start selbstständig, ob es auf einem Proxmox-Host läuft
# (Container anlegen) oder in einem bereits installierten Container
# (Update). Im Container geht es also automatisch in den Update-Modus.
#
# Beide Aliase machen dasselbe — 'update' ist die kurze Variante,
# 'cockpit-update' die explizite (falls jemand mal ein anderes Tool
# 'update' als Command belegt).

if [ -n "${BASH_VERSION:-}${ZSH_VERSION:-}" ]; then
    alias update='bash -c "$(wget -qLO - https://raw.githubusercontent.com/ludwig-systems/opn-cockpit/main/installer/linux/proxmox-helper.sh)"'
    alias cockpit-update='bash -c "$(wget -qLO - https://raw.githubusercontent.com/ludwig-systems/opn-cockpit/main/installer/linux/proxmox-helper.sh)"'
fi
