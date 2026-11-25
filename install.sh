#!/usr/bin/env bash
# Downloader installer: adds a launcher, app-grid entry, and magnet/.torrent handling for the current user.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
APP_ID=io.github.voidrlm.Downloader

if ! python3 - <<'PY' 2>/dev/null
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw
assert (Adw.get_major_version(), Adw.get_minor_version()) >= (1, 7)
PY
then
    echo "Missing dependencies: PyGObject, GTK 4 and libadwaita 1.7+ are required." >&2
    echo "  Fedora: sudo dnf install python3-gobject gtk4 libadwaita" >&2
    exit 1
fi
python3 -c "import libtorrent" 2>/dev/null || echo "Note: torrents need libtorrent:  sudo dnf install rb_libtorrent-python3"
command -v aria2c >/dev/null || echo "Note: direct links need aria2:  sudo dnf install aria2"

chmod +x "$here/downloader"
mkdir -p ~/.local/bin ~/.local/share/applications ~/.local/share/icons/hicolor/scalable/apps
ln -sf "$here/downloader" ~/.local/bin/downloader
ln -sf "$here/$APP_ID.svg" ~/.local/share/icons/hicolor/scalable/apps/$APP_ID.svg
sed "s|^Exec=downloader|Exec=$HOME/.local/bin/downloader|" "$here/$APP_ID.desktop" > ~/.local/share/applications/$APP_ID.desktop
update-desktop-database ~/.local/share/applications 2>/dev/null || true
gtk-update-icon-cache -q -t ~/.local/share/icons/hicolor 2>/dev/null || true
# Open magnet links and .torrent files with Downloader.
xdg-mime default $APP_ID.desktop x-scheme-handler/magnet application/x-bittorrent 2>/dev/null || true
echo "Installed. Open “Downloader” from the app grid, or run: downloader [magnet|url|file.torrent]"
