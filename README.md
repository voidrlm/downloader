# Downloader

A simple torrent and download manager for GNOME — like qBittorrent, minus the 40 columns.

- **Torrents & magnet links** via libtorrent (the engine qBittorrent uses): DHT, peer exchange,
  local discovery, UPnP/NAT-PMP, resume after restart, optional seeding.
- **Direct links** (http/https/ftp) via aria2 with up to 16 connections per file, so servers
  that throttle each connection still give you full speed. Partial files resume.
- One list for both, with All / Downloading / Finished filters, total speed and free space.
- Paste links with **Ctrl+V**, drop `.torrent` files or links on the window, or pass them on
  the command line. Magnet links and `.torrent` files from the browser open here.
- **Search** from the header bar (Ctrl+F): The Pirate Bay, Torrents-CSV, Nyaa and
  Internet Archive, each switchable in Preferences → Search (like qBittorrent's search
  plugins). Results are sorted by seeds; torrents with 0 seeds are hidden. Internet Archive
  items open a file list for direct downloads, or download the whole item as a web-seeded torrent.
- Speed limits, connections per download, notifications when finished.

## Install (Fedora)

```sh
sudo dnf install rb_libtorrent-python3 aria2
./install.sh
```

Either engine is optional: without libtorrent you get direct links only, and the reverse.

## Data

- Settings: `~/.config/downloader/config.json`
- Torrent resume data: `~/.local/share/downloader/torrents/`
- Direct-link list: `~/.local/share/downloader/links.json`
