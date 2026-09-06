#!/bin/sh
# Recoverably uninstall the user-local app while preserving maps and settings.
set -eu

prefix=${NETWORKMAP_PREFIX:-"$HOME/.local"}
if [ "$(id -u)" -eq 0 ]; then
    echo "uninstall.sh: run this as the desktop user who installed NetworkMap" >&2
    exit 1
fi
data_home=${XDG_DATA_HOME:-"$HOME/.local/share"}
state_home=${XDG_STATE_HOME:-"$HOME/.local/state"}
applications_dir="$data_home/applications"
icons_dir="$data_home/icons/hicolor/scalable/apps"
units_dir="$data_home/systemd/user"
backup_dir="$state_home/networkmap/uninstall-backups/$(date -u +%Y%m%dT%H%M%SZ)-$$"
moved=0

stash() {
    source_path=$1
    backup_name=$2
    if [ -e "$source_path" ] || [ -L "$source_path" ]; then
        install -d -m 0700 "$backup_dir"
        mv "$source_path" "$backup_dir/$backup_name"
        moved=1
    fi
}

# Stop and unlink the packaged user service before preserving its unit file.
if [ "${NETWORKMAP_SKIP_REFRESH:-0}" != 1 ] && \
   [ -f "$units_dir/networkmap.service" ] && \
   command -v systemctl >/dev/null 2>&1; then
    systemctl --user disable --now networkmap.service >/dev/null 2>&1 || true
fi

stash "$prefix/lib/networkmap" "app"
stash "$prefix/bin/networkmap" "networkmap-launcher"
stash "$applications_dir/networkmap.desktop" "networkmap.desktop"
stash "$icons_dir/networkmap.svg" "networkmap.svg"
stash "$units_dir/networkmap.service" "networkmap.service"

if [ "${NETWORKMAP_SKIP_REFRESH:-0}" != 1 ]; then
    if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database "$applications_dir" >/dev/null 2>&1 || true
    fi
    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
        gtk-update-icon-cache -f -t "$data_home/icons/hicolor" >/dev/null 2>&1 || true
    fi
    if command -v systemctl >/dev/null 2>&1; then
        systemctl --user daemon-reload >/dev/null 2>&1 || true
    fi
fi

if [ "$moved" -eq 1 ]; then
    echo "NetworkMap application files were moved to:"
    echo "  $backup_dir"
    echo "They can be restored manually until you remove that backup."
else
    echo "No installed NetworkMap application files were found."
fi
echo "Map data and configuration were left untouched."
