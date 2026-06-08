#!/usr/bin/env bash
# =============================================================================
#  OPN-Cockpit  -  SSH-Key-Helper (Linux / macOS / Bash)
# =============================================================================
#
#  Erzeugt ein Ed25519-Key-Paar fuer das Safety-Net-Feature,
#  oeffnet beide Keys im Default-Editor (xdg-open / open),
#  legt den Public-Key in die System-Zwischenablage
#  und zeigt eine kurze Anleitung wohin welcher Key gehoert.
#
#  Verwendung:
#    1. Datei aus dem OPN-Cockpit-SSH-Anleitung-Modal herunterladen.
#    2. chmod +x opncockpit-ssh-helper.sh
#    3. ./opncockpit-ssh-helper.sh
#
#  Voraussetzung: ssh-keygen (OpenSSH-Client).
#
# =============================================================================

set -euo pipefail

bold=$(tput bold 2>/dev/null || true)
reset=$(tput sgr0 2>/dev/null || true)
cyan=$(tput setaf 6 2>/dev/null || true)
yellow=$(tput setaf 3 2>/dev/null || true)
green=$(tput setaf 2 2>/dev/null || true)
red=$(tput setaf 1 2>/dev/null || true)

if ! command -v ssh-keygen >/dev/null 2>&1; then
    printf '\n  %sssh-keygen nicht gefunden.%s\n' "$red$bold" "$reset"
    printf '  Installiere openssh-client:\n'
    printf '    Debian/Ubuntu : sudo apt install openssh-client\n'
    printf '    Fedora        : sudo dnf install openssh-clients\n'
    printf '    macOS         : sollte schon dabei sein\n\n'
    exit 1
fi

work_dir="${HOME}/opncockpit-ssh"
mkdir -p "$work_dir"
chmod 700 "$work_dir"

stamp="$(date +%Y%m%d-%H%M%S)"
host_safe="$(hostname | tr -c 'A-Za-z0-9._-' '_')"
comment="opncockpit-safetynet-${host_safe}-${stamp}"
key_path="$work_dir/opncockpit-safetynet-${stamp}"

if [[ -e "$key_path" || -e "$key_path.pub" ]]; then
    printf '\n  %sDatei existiert schon:%s %s\n' "$red" "$reset" "$key_path"
    exit 1
fi

printf '\n%s=================================================================%s\n' "$cyan$bold" "$reset"
printf '%s  OPN-Cockpit  Safety-Net  SSH-Key-Helper%s\n' "$cyan$bold" "$reset"
printf '%s=================================================================%s\n\n' "$cyan$bold" "$reset"

printf '  Erzeuge Ed25519-Key-Paar...\n'
printf '  Verzeichnis: %s\n\n' "$work_dir"

ssh-keygen -t ed25519 -f "$key_path" -C "$comment" -N '' -q
chmod 600 "$key_path"
chmod 644 "$key_path.pub"

# ---------- Public-Key in Clipboard (best effort) -----------------------------

pub_key="$(cat "$key_path.pub")"
clip_msg=""
if command -v xclip >/dev/null 2>&1; then
    printf '%s' "$pub_key" | xclip -selection clipboard
    clip_msg="(Public-Key liegt in der X11-Zwischenablage)"
elif command -v wl-copy >/dev/null 2>&1; then
    printf '%s' "$pub_key" | wl-copy
    clip_msg="(Public-Key liegt in der Wayland-Zwischenablage)"
elif command -v pbcopy >/dev/null 2>&1; then
    printf '%s' "$pub_key" | pbcopy
    clip_msg="(Public-Key liegt in der macOS-Zwischenablage)"
else
    clip_msg="(kein xclip/wl-copy/pbcopy gefunden - Datei manuell oeffnen)"
fi

# ---------- Editor oeffnen (best effort) -------------------------------------

opener=""
if command -v xdg-open >/dev/null 2>&1; then
    opener="xdg-open"
elif command -v open >/dev/null 2>&1; then
    opener="open"
fi
if [[ -n "$opener" ]]; then
    "$opener" "$key_path.pub" >/dev/null 2>&1 || true
    sleep 0.3
    "$opener" "$key_path" >/dev/null 2>&1 || true
fi

# ---------- Anleitung --------------------------------------------------------

printf '%s=================================================================%s\n' "$yellow$bold" "$reset"
printf '%s  SCHRITT 1  -  Public-Key -> OPNsense%s\n' "$yellow$bold" "$reset"
printf '%s=================================================================%s\n\n' "$yellow$bold" "$reset"

printf '  Public-Key (eine Zeile, beginnt mit "ssh-ed25519 ..."):\n'
printf '    %s.pub\n\n' "$key_path"
printf '  %s\n\n' "$clip_msg"
printf '  In der OPNsense-Web-GUI:\n'
printf '    System -> Access -> Users  ->  [dein SSH-User]\n'
printf '    Feld "authorized keys"  ->  einfuegen\n'
printf '    Save (unten)\n\n'

printf '%s=================================================================%s\n' "$green$bold" "$reset"
printf '%s  SCHRITT 2  -  Private-Key -> OPN-Cockpit%s\n' "$green$bold" "$reset"
printf '%s=================================================================%s\n\n' "$green$bold" "$reset"

printf '  Private-Key (Block, beginnt mit "-----BEGIN OPENSSH ..."):\n'
printf '    %s\n\n' "$key_path"
printf '  Diesen Block KOMPLETT ins OPN-Cockpit:\n'
printf '    Geraet -> Bearbeiten -> "SSH-Private-Key (PEM)"\n'
printf '    Speichern\n\n'

printf '%s=================================================================%s\n' "$red$bold" "$reset"
printf '%s  DANACH (wichtig)%s\n' "$red$bold" "$reset"
printf '%s=================================================================%s\n\n' "$red$bold" "$reset"

printf '  Die zwei Dateien hier liegen UNVERSCHLUESSELT auf der Platte:\n'
printf '    %s\n' "$key_path"
printf '    %s.pub\n\n' "$key_path"
printf '  Nach erfolgreichem Speichern im Cockpit:\n'
printf '    rm "%s" "%s.pub"\n' "$key_path" "$key_path"
printf '\n  Der Private-Key liegt im Cockpit-Tresor verschluesselt mit dem\n'
printf '  Master-Passwort - das reicht als Persistenz.\n\n'
