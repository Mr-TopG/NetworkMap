#!/bin/sh
# Install NetworkMap for the current user. No root privileges are used.
set -eu

source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ "$(id -u)" -eq 0 ]; then
    echo "install.sh: run this as your normal desktop user, without sudo" >&2
    exit 1
fi
prefix=${NETWORKMAP_PREFIX:-"$HOME/.local"}
lib_dir="$prefix/lib"
app_dir="$prefix/lib/networkmap"
bin_dir="$prefix/bin"
data_home=${XDG_DATA_HOME:-"$HOME/.local/share"}
config_home=${XDG_CONFIG_HOME:-"$HOME/.config"}
state_home=${XDG_STATE_HOME:-"$HOME/.local/state"}
applications_dir="$data_home/applications"
icons_dir="$data_home/icons/hicolor/scalable/apps"
units_dir="$data_home/systemd/user"

for required in server.py native.py networkmap_sync.py networkmap static packaging/networkmap.desktop assets/networkmap.svg; do
    if [ ! -e "$source_dir/$required" ]; then
        echo "install.sh: missing required project file: $required" >&2
        exit 1
    fi
done

install -d -m 0755 "$lib_dir" "$bin_dir"
install -d -m 0755 "$applications_dir" "$icons_dir" "$units_dir"
install -d -m 0700 "$data_home/networkmap" "$config_home/networkmap"

stage_dir=$(mktemp -d "$lib_dir/.networkmap-stage.XXXXXX")
chmod 0755 "$stage_dir"
desktop_tmp=
previous_app=
upgrade_backup=
cleanup() {
    if [ -n "$previous_app" ] && [ ! -e "$app_dir" ]; then
        mv "$previous_app" "$app_dir" 2>/dev/null || true
    fi
    if [ -n "$stage_dir" ] && [ -d "$stage_dir" ]; then
        rm -rf -- "$stage_dir"
    fi
    if [ -n "$desktop_tmp" ]; then
        rm -f -- "$desktop_tmp"
    fi
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

install -d -m 0755 "$stage_dir/static" "$stage_dir/packaging/systemd" "$stage_dir/packaging/nginx" "$stage_dir/packaging/routeros"
install -m 0755 "$source_dir/server.py" "$stage_dir/server.py"
install -m 0755 "$source_dir/native.py" "$stage_dir/native.py"
install -m 0644 "$source_dir/networkmap_sync.py" "$stage_dir/networkmap_sync.py"
cp -R "$source_dir/static/." "$stage_dir/static/"
chmod -R u=rwX,go=rX "$stage_dir/static"

for documentation in README.md BACKEND.md LICENSE; do
    if [ -f "$source_dir/$documentation" ]; then
        install -m 0644 "$source_dir/$documentation" "$stage_dir/$documentation"
    fi
done
for example in \
    packaging/systemd/networkmap.service \
    packaging/systemd/server.env.example \
    packaging/nginx/networkmap.conf.example \
    packaging/routeros/README.md; do
    if [ -f "$source_dir/$example" ]; then
        install -m 0644 "$source_dir/$example" "$stage_dir/$example"
    fi
done

# Assemble a complete version before replacing the running installation. This
# prevents removed assets or an interrupted copy from mixing old and new code.
if [ -e "$app_dir" ] || [ -L "$app_dir" ]; then
    upgrade_backup="$state_home/networkmap/install-backups/$(date -u +%Y%m%dT%H%M%SZ)-$$"
    install -d -m 0700 "$upgrade_backup"
    previous_app="$upgrade_backup/app"
    mv "$app_dir" "$previous_app"
fi
mv "$stage_dir" "$app_dir"
stage_dir=
previous_app=

install -m 0755 "$source_dir/networkmap" "$bin_dir/networkmap"

install -m 0644 "$source_dir/assets/networkmap.svg" "$icons_dir/networkmap.svg"

# The desktop specification does not expand $HOME. Put the absolute launcher
# path in the installed copy while keeping the checked-in template portable.
desktop_tmp=$(mktemp "${TMPDIR:-/tmp}/networkmap-desktop.XXXXXX")
NETWORKMAP_DESKTOP_EXEC="$bin_dir/networkmap" \
NETWORKMAP_DESKTOP_SOURCE="$source_dir/packaging/networkmap.desktop" \
NETWORKMAP_DESKTOP_TARGET="$desktop_tmp" \
python3 -c '
import os
from pathlib import Path

source = Path(os.environ["NETWORKMAP_DESKTOP_SOURCE"]).read_text(encoding="utf-8")
executable = os.environ["NETWORKMAP_DESKTOP_EXEC"]
escaped = executable.replace("\\", "\\\\").replace("\"", "\\\"").replace("`", "\\`").replace("$", "\\$")
source = source.replace("Exec=networkmap", f"Exec=\"{escaped}\"")
Path(os.environ["NETWORKMAP_DESKTOP_TARGET"]).write_text(source, encoding="utf-8")
'
install -m 0644 "$desktop_tmp" "$applications_dir/networkmap.desktop"

if [ -f "$source_dir/packaging/systemd/networkmap.service" ]; then
    install -m 0644 \
        "$source_dir/packaging/systemd/networkmap.service" \
        "$units_dir/networkmap.service"
fi
if [ -f "$source_dir/packaging/systemd/server.env.example" ]; then
    install -m 0600 \
        "$source_dir/packaging/systemd/server.env.example" \
        "$config_home/networkmap/server.env.example"
fi

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

echo "NetworkMap was installed for $(id -un)."
echo "  Desktop app: $applications_dir/networkmap.desktop"
echo "  Launcher:    $bin_dir/networkmap"
echo "  App files:   $app_dir"
if [ -n "$upgrade_backup" ]; then
    echo "  Previous app: $upgrade_backup/app"
fi
echo
echo "Run it now with: $bin_dir/networkmap"
case ":${PATH:-}:" in
    *":$bin_dir:"*) ;;
    *) echo "Tip: add $bin_dir to PATH for the 'networkmap' command." ;;
esac
echo "The optional user service is installed but not enabled. See README.md."
