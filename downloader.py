"""Downloader: a simple torrent and direct-link downloader for GNOME.

Torrents and magnet links go through libtorrent; http/https/ftp links go through aria2
with many connections per file.  One list shows both.
"""

import json
import os
import subprocess
import sys
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

import engines  # noqa: E402
import search  # noqa: E402
import search_ui  # noqa: E402
import stream  # noqa: E402
from engines import (CHECKING, CONNECTING, DONE, DOWNLOADING, ERROR, FINISHED_STATES,  # noqa: E402
                     METADATA, PAUSED, QUEUED, SEEDING)

APP_ID = os.environ.get("DOWNLOADER_APP_ID", "io.github.voidrlm.Downloader")
APP_NAME = "Downloader"
VERSION = "1.0"

STATE_DIR = os.environ.get("DOWNLOADER_STATE") or os.path.join(GLib.get_user_data_dir(), "downloader")
# Written while a closing copy saves its state, so a copy started meanwhile waits for it.
CLOSING_FILE = os.path.join(STATE_DIR, "closing.pid")
CONFIG_FILE = os.path.join(os.environ.get("DOWNLOADER_CONFIG_DIR") or
                           os.path.join(GLib.get_user_config_dir(), "downloader"), "config.json")

DEFAULTS = {
    "download_dir": GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
                    or os.path.expanduser("~/Downloads"),
    "max_down_kib": 0,     # 0 = unlimited
    "max_up_kib": 0,
    "connections": 16,     # per direct link
    "max_active": 5,       # direct links downloading at once
    "engines": {},         # search engine id -> on/off; missing = engine's default
}

CSS = """
.dl-name { font-weight: 600; }
.dl-status { font-size: 0.9em; font-feature-settings: "tnum"; }
.dl-icon { border-radius: 10px; min-width: 40px; min-height: 40px; background: alpha(var(--accent-bg-color), 0.15); color: var(--accent-color); }
.dl-icon.link { background: alpha(var(--purple-3, #9141ac), 0.15); color: var(--purple-3, #9141ac); }
.dl-icon.finished { background: alpha(var(--success-color), 0.15); color: var(--success-color); }
.dl-icon.failed { background: alpha(var(--error-color), 0.15); color: var(--error-color); }
progressbar.dl > trough { min-height: 6px; border-radius: 99px; }
progressbar.dl > trough > progress { min-height: 6px; border-radius: 99px; }
progressbar.dl.finished > trough > progress { background-color: var(--success-color); }
progressbar.dl.paused > trough > progress { background-color: alpha(var(--window-fg-color), 0.35); }
progressbar.dl.failed > trough > progress { background-color: var(--error-color); }
.status-bar { padding: 4px 12px; font-size: 0.9em; font-feature-settings: "tnum"; }
.drop-highlight { background: alpha(var(--accent-bg-color), 0.08); }
"""


def load_config():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_FILE) as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    for gone in ("seed", "max_up_kib", "notify"):  # settings that no longer exist
        cfg.pop(gone, None)
    return cfg


def save_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=1)
    os.replace(tmp, CONFIG_FILE)


def fmt_size(n):
    return GLib.format_size(int(n))


def fmt_speed(n):
    return (fmt_size(n) if n else "0 B") + "/s"


def fmt_eta(sec):
    sec = int(sec)
    if sec < 60:
        return f"{sec} s left"
    if sec < 3600:
        return f"{sec // 60} min left"
    if sec < 86400:
        return f"{sec // 3600} h {sec % 3600 // 60} min left"
    return f"{sec // 86400} d {sec % 86400 // 3600} h left"


def short_dir(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


def describe(it):
    """The grey line under a download's name."""
    parts = []
    if it.state == METADATA:
        parts.append("Getting torrent info")
        if it.peers:
            parts.append(f"{it.peers} peers")
    elif it.state == CHECKING:
        parts.append(f"Checking files · {int(it.progress * 100)}%")
    elif it.state == CONNECTING:
        parts.append("Connecting…")
    elif it.state == QUEUED:
        parts.append("Waiting")
        if it.total:
            parts.append(f"{fmt_size(it.done)} of {fmt_size(it.total)}")
    elif it.state == PAUSED:
        parts.append("Paused")
        if it.total:
            parts.append(f"{fmt_size(it.done)} of {fmt_size(it.total)} ({int(it.progress * 100)}%)")
    elif it.state == DOWNLOADING:
        parts.append(f"{fmt_size(it.done)} of {fmt_size(it.total)}" if it.total else fmt_size(it.done))
        parts.append("↓ " + fmt_speed(it.down))
        if it.eta is not None:
            parts.append(fmt_eta(it.eta))
        if it.kind == "torrent":
            parts.append(f"{it.peers} peers")
        elif it.peers > 1:
            parts.append(f"{it.peers} connections")
    elif it.state == SEEDING:
        parts.append(f"{fmt_size(it.total)} · Done, sharing")
        parts.append("↑ " + fmt_speed(it.up))
        parts.append(f"ratio {it.ratio:.2f}")
    elif it.state == DONE:
        parts.append(f"{fmt_size(it.total)} · Done" if it.total else "Done")
    elif it.state == ERROR:
        msg = it.error or "Failed"
        if msg.startswith("CUID#"):
            msg = msg.split(" - ", 1)[-1]
        msg = msg.replace("failed:", "failed: ")
        parts.append(msg)
    return " · ".join(parts)


# ---------------------------------------------------------------- download row


class DownloadRow(Gtk.ListBoxRow):
    def __init__(self, win, item):
        super().__init__(activatable=True)
        self.win = win
        self.item = item
        self.add_css_class("dl-row")

        box = Gtk.Box(spacing=12, margin_top=10, margin_bottom=10, margin_start=12, margin_end=8)
        self.icon = Gtk.Image(pixel_size=20, valign=Gtk.Align.CENTER)
        self.icon.add_css_class("dl-icon")
        box.append(self.icon)

        mid = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5, hexpand=True,
                      valign=Gtk.Align.CENTER)
        self.name = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.MIDDLE)
        self.name.add_css_class("dl-name")
        self.bar = Gtk.ProgressBar()
        self.bar.add_css_class("dl")
        self.status = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.status.add_css_class("dl-status")
        self.status.add_css_class("dim-label")
        mid.append(self.name)
        mid.append(self.bar)
        mid.append(self.status)
        box.append(mid)

        self.play = Gtk.Button(icon_name="media-playback-start-symbolic", valign=Gtk.Align.CENTER,
                               tooltip_text="Play", visible=False)
        self.play.add_css_class("circular")
        self.play.add_css_class("suggested-action")
        self.play.connect("clicked", lambda *_: self.win.app.play(self.item))
        box.append(self.play)

        self.toggle = Gtk.Button(valign=Gtk.Align.CENTER)
        self.toggle.add_css_class("flat")
        self.toggle.add_css_class("circular")
        self.toggle.connect("clicked", lambda *_: self.win.toggle_item(self.item))
        box.append(self.toggle)

        self.folder = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER,
                                 tooltip_text="Show in Folder")
        self.folder.add_css_class("flat")
        self.folder.add_css_class("circular")
        self.folder.connect("clicked", lambda *_: self.win.show_in_folder(self.item))
        box.append(self.folder)

        # The "more" popover is a single row of icon buttons (tooltips name them).
        pop = Gtk.Popover(has_arrow=True)
        icons = Gtk.Box(spacing=2, margin_top=2, margin_bottom=2, margin_start=2, margin_end=2)
        for entry in (("open", "document-open-symbolic", "Open"),
                      ("folder", "folder-open-symbolic", "Show in Folder"),
                      ("copy", "edit-copy-symbolic", "Copy Link"),
                      None,
                      ("remove", "list-remove-symbolic", "Remove from List"),
                      ("delete", "user-trash-symbolic", "Remove and Delete Files")):
            if entry is None:
                icons.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL, margin_start=4,
                                           margin_end=4))
                continue
            action, icon, tip = entry
            b = Gtk.Button(icon_name=icon, tooltip_text=tip, action_name="row." + action)
            b.add_css_class("flat")
            if action == "delete":
                b.add_css_class("destructive-action")
            b.connect("clicked", lambda *_: pop.popdown())
            icons.append(b)
        pop.set_child(icons)
        more = Gtk.MenuButton(icon_name="view-more-symbolic", popover=pop,
                              valign=Gtk.Align.CENTER, tooltip_text="More")
        more.add_css_class("flat")
        more.add_css_class("circular")
        box.append(more)

        group = Gio.SimpleActionGroup()
        for name, fn in (("open", lambda: self.win.open_item(self.item)),
                         ("folder", lambda: self.win.show_in_folder(self.item)),
                         ("copy", lambda: self.win.copy_link(self.item)),
                         ("remove", lambda: self.win.remove_item(self.item, False)),
                         ("delete", lambda: self.win.remove_item(self.item, True))):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda _a, _p, fn=fn: fn())
            group.add_action(a)
        self.actions = group
        self.insert_action_group("row", group)

        self.set_child(box)
        self._last = None
        self.update(item)

    def update(self, it):
        self.item = it
        sig = (it.name, it.state, it.done, it.total, it.down, it.up, it.peers, it.error, it.stream)
        if sig == self._last:
            return
        self._last = sig
        self.name.set_label(it.name)
        self.name.set_tooltip_text(it.name)
        self.bar.set_fraction(it.progress)
        self.status.set_label(describe(it))
        self.status.set_tooltip_text(it.error or None)

        finished = it.state in FINISHED_STATES
        failed = it.state == ERROR
        for w in (self.bar, self.icon):
            w.remove_css_class("finished")
            w.remove_css_class("failed")
            if finished:
                w.add_css_class("finished")
            elif failed:
                w.add_css_class("failed")
        if it.state in (PAUSED, QUEUED):
            self.bar.add_css_class("paused")
        else:
            self.bar.remove_css_class("paused")
        if it.kind == "link":
            self.icon.add_css_class("link")
        if finished:
            self.icon.set_from_icon_name("object-select-symbolic")
        elif failed:
            self.icon.set_from_icon_name("dialog-warning-symbolic")
        elif it.kind == "torrent":
            self.icon.set_from_icon_name("network-transmit-receive-symbolic")
        else:
            self.icon.set_from_icon_name("web-browser-symbolic")

        if it.state == SEEDING:
            self.toggle.set_icon_name("media-playback-stop-symbolic")
            self.toggle.set_tooltip_text("Stop Sharing")
        elif it.state == ERROR:
            self.toggle.set_icon_name("view-refresh-symbolic")
            self.toggle.set_tooltip_text("Retry")
        elif it.state == PAUSED:
            self.toggle.set_icon_name("media-playback-start-symbolic")
            self.toggle.set_tooltip_text("Resume")
        else:
            self.toggle.set_icon_name("media-playback-pause-symbolic")
            self.toggle.set_tooltip_text("Pause")
        self.toggle.set_visible(it.state != DONE)
        self.folder.set_visible(finished)
        self.play.set_visible(it.stream and it.state != ERROR)
        self.actions.lookup_action("open").set_enabled(finished or it.kind == "torrent")
        self.actions.lookup_action("copy").set_enabled(bool(it.link))


# ---------------------------------------------------------------- add dialog


class AddDialog(Adw.Dialog):
    def __init__(self, win, text=""):
        super().__init__(title="Add Download", content_width=560)
        self.win = win
        self.folder = win.app.cfg["download_dir"]

        tv = Adw.ToolbarView()
        header = Adw.HeaderBar()
        tv.add_top_bar(header)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=6,
                       margin_bottom=24, margin_start=24, margin_end=24)

        intro = Gtk.Label(xalign=0, wrap=True,
                          label="Paste magnet links, .torrent links or direct download links — one per line.")
        intro.add_css_class("dim-label")
        body.append(intro)

        self.buffer = Gtk.TextBuffer()
        self.view = Gtk.TextView(buffer=self.buffer, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                 top_margin=10, bottom_margin=10, left_margin=12, right_margin=12,
                                 accepts_tab=False)
        self.view.add_css_class("monospace")
        self.view.add_css_class("card")
        sw = Gtk.ScrolledWindow(min_content_height=130, max_content_height=260,
                                propagate_natural_height=True, child=self.view)
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        body.append(sw)

        self.summary = Gtk.Label(xalign=0)
        self.summary.add_css_class("caption")
        self.summary.add_css_class("dim-label")

        pick = Gtk.Button(child=Adw.ButtonContent(icon_name="document-open-symbolic",
                                                  label="Choose .torrent Files…"))
        pick.add_css_class("flat")
        pick.connect("clicked", self.on_pick_files)
        row = Gtk.Box(spacing=12)
        row.append(pick)
        self.summary.set_hexpand(True)
        self.summary.set_xalign(1)
        row.append(self.summary)
        body.append(row)

        group = Adw.PreferencesGroup()
        self.folder_row = Adw.ActionRow(title="Save to", subtitle=short_dir(self.folder),
                                        activatable=True)
        self.folder_row.add_prefix(Gtk.Image(icon_name="folder-symbolic"))
        self.folder_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        self.folder_row.connect("activated", self.on_pick_folder)
        group.add(self.folder_row)
        body.append(group)

        self.add_btn = Gtk.Button(label="Download", halign=Gtk.Align.CENTER)
        self.add_btn.add_css_class("pill")
        self.add_btn.add_css_class("suggested-action")
        self.add_btn.connect("clicked", self.on_add)
        body.append(self.add_btn)

        tv.set_content(body)
        self.set_child(tv)
        self.set_default_widget(self.add_btn)

        self.buffer.connect("changed", lambda *_: self.refresh())
        if text:
            self.buffer.set_text(text.strip() + "\n")
        self.refresh()
        self.set_focus(self.view)

    def text(self):
        return self.buffer.get_text(self.buffer.get_start_iter(), self.buffer.get_end_iter(), False)

    def refresh(self):
        links = engines.split_links(self.text())
        n_t = sum(1 for k, _ in links if k != "link")
        n_l = len(links) - n_t
        bits = []
        if n_t:
            bits.append(f"{n_t} torrent" + ("s" if n_t > 1 else ""))
        if n_l:
            bits.append(f"{n_l} link" + ("s" if n_l > 1 else ""))
        self.summary.set_label(" · ".join(bits) if bits else ("Nothing recognised yet" if self.text().strip() else ""))
        self.add_btn.set_sensitive(bool(links))
        self.add_btn.set_label(f"Download {len(links)}" if len(links) > 1 else "Download")

    def on_pick_files(self, *_):
        f = Gtk.FileFilter(name="Torrent files")
        f.add_pattern("*.torrent")
        f.add_mime_type("application/x-bittorrent")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(f)
        dlg = Gtk.FileDialog(title="Choose Torrent Files", filters=filters, default_filter=f)

        def done(d, res):
            try:
                files = d.open_multiple_finish(res)
            except GLib.Error:
                return
            paths = [files.get_item(i).get_path() for i in range(files.get_n_items())]
            cur = self.text().rstrip("\n")
            self.buffer.set_text((cur + "\n" if cur else "") + "\n".join(p for p in paths if p) + "\n")

        dlg.open_multiple(self.win, None, done)

    def on_pick_folder(self, *_):
        dlg = Gtk.FileDialog(title="Save Downloads To", initial_folder=Gio.File.new_for_path(self.folder))

        def done(d, res):
            try:
                self.folder = d.select_folder_finish(res).get_path()
            except GLib.Error:
                return
            self.folder_row.set_subtitle(short_dir(self.folder))

        dlg.select_folder(self.win, None, done)

    def on_add(self, *_):
        links = engines.split_links(self.text())
        if not links:
            return
        self.close()
        self.win.add_links(links, self.folder)


# ---------------------------------------------------------------- preferences


class PrefsDialog(Adw.PreferencesDialog):
    def __init__(self, win):
        super().__init__(title="Preferences")
        self.win = win
        cfg = win.app.cfg
        page = Adw.PreferencesPage()

        g = Adw.PreferencesGroup(title="Downloads")
        self.folder_row = Adw.ActionRow(title="Save to", subtitle=short_dir(cfg["download_dir"]),
                                        activatable=True)
        self.folder_row.add_suffix(Gtk.Image(icon_name="folder-open-symbolic"))
        self.folder_row.connect("activated", self.on_folder)
        g.add(self.folder_row)
        page.add(g)

        page.set_title("General")
        page.set_icon_name("emblem-system-symbolic")
        self.add(page)

        page = Adw.PreferencesPage(name="search", title="Search", icon_name="system-search-symbolic")
        g = Adw.PreferencesGroup(title="Search Engines")
        on = cfg.get("engines", {})
        for e in search.ENGINES:
            row = Adw.SwitchRow(title=e.name, active=on.get(e.id, e.default_enabled))
            row.connect("notify::active", lambda r, _p, eid=e.id: self.set_engine(eid, r.get_active()))
            g.add(row)
        page.add(g)
        self.add(page)

    def set_engine(self, eid, active):
        engines_on = dict(self.win.app.cfg.get("engines", {}))
        engines_on[eid] = active
        self.win.app.set_pref("engines", engines_on)

    def switch(self, title, subtitle_or_key, key=None):
        if key is None:
            key, subtitle = subtitle_or_key, None
        else:
            subtitle = subtitle_or_key
        row = Adw.SwitchRow(title=title, active=bool(self.win.app.cfg[key]))
        if subtitle:
            row.set_subtitle(subtitle)
        row.connect("notify::active", lambda r, _p: self.win.app.set_pref(key, r.get_active()))
        return row

    def spin(self, title, subtitle, key, lo, hi, step):
        row = Adw.SpinRow.new_with_range(lo, hi, step)
        row.set_title(title)
        if subtitle:
            row.set_subtitle(subtitle)
        row.set_value(self.win.app.cfg[key])
        row.connect("notify::value", lambda r, _p: self.win.app.set_pref(key, int(r.get_value())))
        return row

    def on_folder(self, *_):
        dlg = Gtk.FileDialog(title="Save Downloads To",
                             initial_folder=Gio.File.new_for_path(self.win.app.cfg["download_dir"]))

        def done(d, res):
            try:
                path = d.select_folder_finish(res).get_path()
            except GLib.Error:
                return
            self.win.app.set_pref("download_dir", path)
            self.folder_row.set_subtitle(short_dir(path))

        dlg.select_folder(self.win, None, done)


# ---------------------------------------------------------------- main window


class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME, default_width=860, default_height=580)
        self.set_size_request(360, 300)
        self.app = app
        self.rows = {}
        self.filter = "all"

        tv = Adw.ToolbarView()
        header = Adw.HeaderBar()
        add = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add Download (Ctrl+N)")
        add.connect("clicked", lambda *_: self.show_add())
        header.pack_start(add)
        self.search_entry = Gtk.SearchEntry(placeholder_text="Search", width_chars=12,
                                            max_width_chars=12, tooltip_text="Search (Ctrl+F)")
        self.search_entry.connect("activate", lambda e: self.run_search(e.get_text()))
        self.search_entry.connect("stop-search", lambda *_: self.leave_search())
        header.pack_start(self.search_entry)

        self.tabs = Adw.ToggleGroup()
        for name, icon, tip in (("all", "view-list-bullet-symbolic", "All"),
                                ("active", "folder-download-symbolic", "Downloading"),
                                ("done", "object-select-symbolic", "Finished"),
                                ("search", "system-search-symbolic", "Search Results")):
            self.tabs.add(Adw.Toggle(name=name, icon_name=icon, tooltip=tip))
        self.tabs.set_active_name("all")
        self.tabs.connect("notify::active-name", self.on_tab)
        header.set_title_widget(self.tabs)

        menu = Gio.Menu()
        s = Gio.Menu()
        s.append("Clear Finished", "win.clear-finished")
        menu.append_section(None, s)
        s = Gio.Menu()
        s.append("Open Downloads Folder", "win.open-dir")
        s.append("Preferences", "app.preferences")
        s.append("About Downloader", "app.about")
        menu.append_section(None, s)
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                       primary=True, tooltip_text="Main Menu"))
        for icon, tip, action in (("media-playback-start-symbolic", "Resume All", "win.resume-all"),
                                  ("media-playback-stop-symbolic", "Stop All", "win.pause-all")):
            header.pack_end(Gtk.Button(icon_name=icon, tooltip_text=tip, action_name=action))
        tv.add_top_bar(header)
        self.banner = Adw.Banner()
        self.banner.connect("button-clicked", lambda *_: app.install_engines())
        tv.add_top_bar(self.banner)
        self.show_missing_banner()

        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, valign=Gtk.Align.START)
        self.list.add_css_class("boxed-list")
        self.list.set_sort_func(lambda a, b: (b.item.added > a.item.added) - (b.item.added < a.item.added))
        self.list.set_filter_func(self.filter_row)
        self.list.connect("row-activated", lambda _l, row: self.open_item(row.item))
        clamp = Adw.Clamp(maximum_size=960, child=self.list, margin_top=12, margin_bottom=12,
                          margin_start=12, margin_end=12)
        self.scroll = Gtk.ScrolledWindow(child=clamp, hscrollbar_policy=Gtk.PolicyType.NEVER)

        empty_btn = Gtk.Button(label="Add Download", halign=Gtk.Align.CENTER)
        empty_btn.add_css_class("pill")
        empty_btn.add_css_class("suggested-action")
        empty_btn.connect("clicked", lambda *_: self.show_add())
        self.empty = Adw.StatusPage(
            icon_name="folder-download-symbolic", title="No Downloads",
            description="Add a magnet link, a .torrent file or any direct download link.\n"
                        "You can also paste with Ctrl+V or drop files here.",
            child=empty_btn)
        self.empty_filtered = Adw.StatusPage(icon_name="folder-download-symbolic", title="Nothing Here")

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self.empty, "empty")
        self.stack.add_named(self.empty_filtered, "empty-filtered")
        self.stack.add_named(self.scroll, "list")
        self.search_page = search_ui.SearchPage(self)
        self.stack.add_named(self.search_page, "search")
        self.toasts = Adw.ToastOverlay(child=self.stack)
        tv.set_content(self.toasts)

        bar = Gtk.Box(spacing=18)
        bar.add_css_class("status-bar")
        self.speed_label = Gtk.Label(xalign=0, hexpand=True)
        self.free_label = Gtk.Label(xalign=1)
        for l in (self.speed_label, self.free_label):
            l.add_css_class("dim-label")
        bar.append(self.speed_label)
        bar.append(self.free_label)
        tv.add_bottom_bar(bar)
        self.set_content(tv)

        for name, fn in (("pause-all", lambda: self.all_items(pause=True)),
                         ("resume-all", lambda: self.all_items(pause=False)),
                         ("clear-finished", self.clear_finished),
                         ("open-dir", lambda: self.launch_folder(self.app.cfg["download_dir"])),
                         ("add", self.show_add), ("paste", self.paste)):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda _a, _p, fn=fn: fn())
            self.add_action(a)

        # Ctrl+V anywhere outside a text field opens the add dialog with the clipboard.
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self.on_key)
        self.add_controller(keys)

        drop = Gtk.DropTarget(actions=Gdk.DragAction.COPY)
        drop.set_gtypes([Gdk.FileList, str])
        drop.connect("drop", self.on_drop)
        drop.connect("enter", lambda *_: (self.toasts.add_css_class("drop-highlight"), Gdk.DragAction.COPY)[1])
        drop.connect("leave", lambda *_: self.toasts.remove_css_class("drop-highlight"))
        self.add_controller(drop)

    def show_missing_banner(self):
        pkgs = self.app.missing_packages()
        if not pkgs:
            self.banner.set_revealed(False)
        else:
            if len(pkgs) == 2:
                title = "Torrents and direct links need two small packages to work"
            elif pkgs[0] == "aria2":
                title = "Direct links need the aria2 package to work"
            else:
                title = "Torrents need the libtorrent package to work"
            self.banner.set_title(title)
            self.banner.set_button_label("Install")
            self.banner.set_revealed(True)

    # -- refresh

    def update(self, items):
        seen = set()
        for it in items:
            seen.add(it.key)
            row = self.rows.get(it.key)
            if row is None:
                row = DownloadRow(self, it)
                self.rows[it.key] = row
                self.list.append(row)
            else:
                row.update(it)
        for key in list(self.rows):
            if key not in seen:
                self.list.remove(self.rows.pop(key))
        self.list.invalidate_filter()
        visible = any(self.filter_row(r) for r in self.rows.values())
        if self.filter == "search":
            pass  # the search page stays up; the status bar below still updates
        elif not self.rows:
            self.stack.set_visible_child_name("empty")
        elif not visible:
            self.empty_filtered.set_title("Nothing Downloading" if self.filter == "active" else "Nothing Finished Yet")
            self.stack.set_visible_child_name("empty-filtered")
        else:
            self.stack.set_visible_child_name("list")

        down = sum(i.down for i in items)
        up = sum(i.up for i in items)
        active = sum(1 for i in items if i.state in (DOWNLOADING, METADATA, CONNECTING))
        text = f"↓ {fmt_speed(down)}    ↑ {fmt_speed(up)}"
        if active:
            text += f"    {active} active"
        self.speed_label.set_label(text)
        try:
            info = Gio.File.new_for_path(self.app.cfg["download_dir"]).query_filesystem_info(
                "filesystem::free", None)
            self.free_label.set_label(f"{fmt_size(info.get_attribute_uint64('filesystem::free'))} free")
        except GLib.Error:
            self.free_label.set_label("")

    def filter_row(self, row):
        st = row.item.state
        if self.filter == "active":
            return st not in FINISHED_STATES
        if self.filter == "done":
            return st in FINISHED_STATES
        return True

    def on_tab(self, *_):
        self.filter = self.tabs.get_active_name() or "all"
        if self.filter == "search":
            self.stack.set_visible_child_name("search")
            self.search_page.refresh()
            if not self.search_page.query:
                self.search_entry.grab_focus()
        self.app.tick()

    # -- search

    def run_search(self, text):
        if not text.strip():
            return
        self.tabs.set_active_name("search")
        self.search_page.search(text)

    def leave_search(self):
        if self.filter == "search":
            self.tabs.set_active_name("all")

    # -- adding

    def show_add(self, text=""):
        AddDialog(self, text).present(self)

    def paste(self):
        cb = self.get_clipboard()

        def got(c, res):
            try:
                text = c.read_text_finish(res) or ""
            except GLib.Error:
                text = ""
            self.show_add(text if engines.split_links(text) else "")

        cb.read_text_async(None, got)

    def on_key(self, _ctl, keyval, _code, state):
        ctrl = state & Gdk.ModifierType.CONTROL_MASK
        if not ctrl or self.get_visible_dialog() is not None:
            return False
        if keyval in (Gdk.KEY_v, Gdk.KEY_V):
            focus = self.get_focus()
            if isinstance(focus, (Gtk.Text, Gtk.TextView, Gtk.Editable)):
                return False
            self.paste()
            return True
        if keyval in (Gdk.KEY_f, Gdk.KEY_F):
            self.search_entry.grab_focus()
            return True
        if keyval in (Gdk.KEY_n, Gdk.KEY_N, Gdk.KEY_o, Gdk.KEY_O):
            self.show_add()
            return True
        return False

    def on_drop(self, _t, value, _x, _y):
        self.toasts.remove_css_class("drop-highlight")
        if isinstance(value, Gdk.FileList):
            text = "\n".join(f.get_path() or f.get_uri() for f in value.get_files())
        else:
            text = str(value)
        links = engines.split_links(text)
        if not links:
            self.toast("Drop .torrent files, magnet links or download links")
            return False
        self.add_links(links, self.app.cfg["download_dir"])
        return True

    def add_links(self, links, folder):
        n = self.app.add_links(links, folder)
        if n:
            self.toast(f"Added {n} download" + ("s" if n > 1 else ""))

    # -- item actions

    def toggle_item(self, it):
        if it.state == DONE:
            self.open_item(it)
        elif it.state in (PAUSED, ERROR):
            self.app.engine(it).resume(it.key[2:])
        else:
            self.app.engine(it).pause(it.key[2:])
        self.app.tick()

    def all_items(self, pause):
        for row in self.rows.values():
            it = row.item
            eng = self.app.engine(it)
            if pause and it.state not in (PAUSED, DONE, ERROR):
                eng.pause(it.key[2:])
            elif not pause and it.state in (PAUSED, ERROR):
                eng.resume(it.key[2:])
        self.app.tick()

    def clear_finished(self):
        for row in list(self.rows.values()):
            if row.item.state == DONE:
                self.app.engine(row.item).remove(row.item.key[2:], False)
        self.app.tick()

    def remove_item(self, it, delete_files):
        if not delete_files:
            self.app.engine(it).remove(it.key[2:], False)
            self.app.tick()
            return
        dlg = Adw.AlertDialog(heading="Delete Files?",
                              body=f"“{it.name}” will be removed and its files deleted from disk.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("delete", "Delete")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")

        def on_resp(_d, resp):
            if resp == "delete":
                self.app.engine(it).remove(it.key[2:], True)
                self.app.tick()

        dlg.connect("response", on_resp)
        dlg.present(self)

    def open_item(self, it):
        if it.state not in FINISHED_STATES:
            self.show_in_folder(it)
            return
        if not os.path.exists(it.path):
            self.toast("The file isn't there anymore")
            return
        Gtk.FileLauncher(file=Gio.File.new_for_path(it.path)).launch(self, None, self._launched)

    def show_in_folder(self, it):
        if os.path.exists(it.path):
            Gtk.FileLauncher(file=Gio.File.new_for_path(it.path)).open_containing_folder(
                self, None, self._launched)
        else:
            self.launch_folder(it.save_dir)

    def launch_folder(self, path):
        Gtk.FileLauncher(file=Gio.File.new_for_path(path)).launch(self, None, self._launched)

    def _launched(self, launcher, res):
        try:
            launcher.launch_finish(res)
        except GLib.Error:
            try:
                launcher.open_containing_folder_finish(res)
            except GLib.Error as e:
                if not e.matches(Gtk.dialog_error_quark(), Gtk.DialogError.DISMISSED):
                    self.toast("Couldn't open it")

    def copy_link(self, it):
        if it.link:
            self.get_clipboard().set(it.link)
            self.toast("Link copied")

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=3))


# ---------------------------------------------------------------- application


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.window = None
        self.streamer = None
        self.torrents = None
        self.links = None
        self.cfg = load_config()
        self.add_main_option("version", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Show the version", None)

    def do_handle_local_options(self, opts):
        if opts.contains("version"):
            print(APP_NAME, VERSION)
            return 0
        return -1

    def do_startup(self):
        Adw.Application.do_startup(self)
        css = Gtk.CssProvider()
        css.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        os.makedirs(STATE_DIR, exist_ok=True)
        if engines.TorrentEngine.available:
            self.torrents = engines.TorrentEngine(STATE_DIR, self.cfg)
        self.links = engines.LinkEngine(STATE_DIR, self.cfg)

        for name, fn, accels in (("preferences", self.show_prefs, ["<Control>comma"]),
                                 ("about", self.show_about, []),
                                 ("quit", self.quit, ["<Control>q"])):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda _a, _p, fn=fn: fn())
            self.add_action(a)
            if accels:
                self.set_accels_for_action("app." + name, accels)
        self.set_accels_for_action("win.add", ["<Control>n"])
        GLib.timeout_add(1000, self.tick)

    def ensure_window(self):
        if self.window is None or self.window not in self.get_windows():
            self.window = Window(self)
            self.window.connect("close-request", lambda *_: (self.quit(), False)[1])
            self.tick()
        self.window.present()
        return self.window

    def do_activate(self):
        self.ensure_window()

    def do_command_line(self, cmdline):
        win = self.ensure_window()
        args = cmdline.get_arguments()[1:]
        text = []
        for a in args:
            if os.path.exists(os.path.join(cmdline.get_cwd() or "", a)):
                a = cmdline.create_file_for_arg(a).get_path()
            text.append(a)
        links = engines.split_links("\n".join(text))
        if links:
            win.add_links(links, self.cfg["download_dir"])
        elif args:
            win.toast("Nothing to download in: " + " ".join(args))
        return 0

    def do_shutdown(self):
        if self.window:
            self.window.set_visible(False)
        try:
            with open(CLOSING_FILE, "w") as f:
                f.write(str(os.getpid()))
        except OSError:
            pass
        if self.links:
            self.links.shutdown()
        if self.torrents:
            self.torrents.shutdown()
        Adw.Application.do_shutdown(self)

    # -- engines

    def engine(self, it):
        return self.torrents if it.kind == "torrent" else self.links

    def add_links(self, links, folder):
        os.makedirs(folder, exist_ok=True)
        added = 0
        for kind, value in links:
            try:
                if kind == "link":
                    self.links.add(value, folder)
                elif not self.torrents:
                    raise RuntimeError(engines.TorrentEngine.missing_hint)
                elif kind == "magnet":
                    self.torrents.add_magnet(value, folder)
                elif kind == "torrent-file":
                    self.torrents.add_file(value, folder)
                elif kind == "torrent-url":
                    self.fetch_torrent(value, folder)
                added += 1
            except Exception as e:
                self.window.toast(str(e))
        self.tick()
        return added

    # -- watching videos

    def play(self, it, waited=0):
        """Open the torrent's video in a player, streaming it if it isn't finished yet."""
        if it.kind != "torrent" or not self.torrents:
            return
        key = it.key[2:]
        info = self.torrents.video_info(key)
        if not info:
            self.window.toast("No video in this download")
            return
        if it.state == PAUSED:
            self.torrents.resume(key)
        # Subtitles are fetched first; give them a few seconds so the player can load them.
        pending_subs = [p for p, done in info["subs"] if not done]
        if pending_subs and not any(done for _p, done in info["subs"]) and waited < 15 and not info["complete"]:
            if waited == 0:
                self.window.toast("Getting subtitles first…")
            GLib.timeout_add(1000, lambda: (self.play(it, waited + 1), False)[1])
            return
        subs = [p for p, done in info["subs"] if done]
        if info["complete"]:
            target = info["path"]
        else:
            if self.streamer is None:
                self.streamer = stream.StreamServer(self.torrents)
            target = self.streamer.url(key, info["index"], info["name"])
        self.launch_player(target, info["name"], subs)

    def launch_player(self, target, title, subs):
        is_url = target.startswith("http")
        mime = Gio.content_type_guess(title, None)[0]
        app = Gio.AppInfo.get_default_for_type(mime, False) or Gio.AppInfo.get_default_for_type("video/mp4", False)
        app_id = (app.get_id() or "") if app else ""
        cmd = None
        if "reel" in app_id.lower():
            reel = os.path.expanduser("~/.local/bin/reel")
            cmd = [reel if os.path.exists(reel) else "reel", f"--title={title}"] + \
                  [f"--sub-file={s}" for s in subs[:1]] + [target]
        elif "vlc" in app_id.lower():
            base = ["flatpak", "run", "org.videolan.VLC"] if app_id == "org.videolan.VLC.desktop" else ["vlc"]
            cmd = base + [f"--meta-title={title}"] + [f"--sub-file={s}" for s in subs[:1]] + [target]
        elif "celluloid" in app_id.lower():
            cmd = ["celluloid"] + [f"--mpv-sub-file={s}" for s in subs] + [target]
        elif "mpv" in app_id.lower():
            cmd = ["mpv", "--force-window=immediate", f"--force-media-title={title}"] + \
                  [f"--sub-file={s}" for s in subs] + [target]
        if cmd:
            try:
                subprocess.Popen(cmd, start_new_session=True, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except OSError:
                pass
        uri = target if is_url else Gio.File.new_for_path(target).get_uri()
        try:
            if app:
                app.launch_uris([uri], None)
                return
        except GLib.Error:
            pass
        Gtk.UriLauncher(uri=uri).launch(self.window, None, None)

    def fetch_torrent(self, url, folder):
        def work():
            try:
                path = engines.fetch_torrent_url(url)
            except Exception as e:
                GLib.idle_add(self.window.toast, f"Couldn't get the torrent: {e}")
                return
            GLib.idle_add(finish, path)

        def finish(path):
            try:
                self.torrents.add_file(path, folder)
            except Exception as e:
                self.window.toast(f"Not a valid torrent: {e}")
            finally:
                os.remove(path)
            self.tick()

        threading.Thread(target=work, daemon=True).start()

    def tick(self):
        items = []
        if self.torrents:
            items += self.torrents.poll()
        if self.links:
            items += self.links.poll()
        if self.window:
            self.window.update(items)
        for eng in (self.torrents, self.links):
            if eng:
                eng.pop_finished()  # no notifications: the user doesn't want dock badges
        return True

    def set_pref(self, key, value):
        self.cfg[key] = value
        save_config(self.cfg)
        if self.torrents:
            self.torrents.apply_settings(self.cfg)
        if self.links:
            self.links.apply_settings(self.cfg)

    # -- dialogs

    def show_prefs(self, page=None):
        dlg = PrefsDialog(self.window)
        if page:
            dlg.set_visible_page_name(page)
        dlg.present(self.window)

    def show_about(self):
        about = Adw.AboutDialog(application_name=APP_NAME, application_icon=APP_ID,
                                version=VERSION, developer_name="Arjun",
                                comments="Torrents, magnet links and fast direct downloads.",
                                license_type=Gtk.License.MIT_X11)
        engines_info = []
        engines_info.append("libtorrent " + engines.lt.__version__ if engines.lt else "libtorrent: not installed")
        engines_info.append("aria2: " + ("ready" if self.links.available else "not installed"))
        about.set_debug_info("\n".join(engines_info))
        about.present(self.window)

    # -- installing the engines

    def missing_packages(self):
        pkgs = []
        if not engines.TorrentEngine.available:
            pkgs.append("rb_libtorrent-python3")
        if not self.links.binary:
            pkgs.append("aria2")
        return pkgs

    def install_engines(self):
        """Install the missing engines with dnf behind the normal GNOME password prompt."""
        pkgs = self.missing_packages()
        win = self.window
        if not pkgs:
            return
        win.banner.set_title("Installing… enter your password if asked")
        win.banner.set_button_label(None)
        try:
            proc = Gio.Subprocess.new(["pkexec", "dnf", "install", "-y", *pkgs],
                                      Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_PIPE)
        except GLib.Error as e:
            win.toast(f"Couldn't start the installer: {e.message}")
            win.show_missing_banner()
            return

        def done(p, res):
            try:
                _ok, _out, err = p.communicate_utf8_finish(res)
            except GLib.Error as e:
                err = e.message
            code = p.get_exit_status() if p.get_if_exited() else -1
            if code == 0:
                win.banner.set_title("Installed — restarting Downloader…")
                self.restart = True
                GLib.timeout_add(800, lambda: (self.quit(), False)[1])
                return
            if code in (126, 127):  # pkexec: password dialog dismissed / not authorised
                win.toast("Install cancelled")
            else:
                last = (err or "").strip().splitlines()[-1:] or ["unknown error"]
                win.toast(f"Install failed: {last[0]}")
            win.show_missing_banner()

        proc.communicate_utf8_async(None, None, done)


def wait_for_closing_copy(timeout=10.0):
    """If a previous Downloader is still saving its state on the way out, wait for it to exit.

    Otherwise this copy would hand its request to the closing one (which owns the app's
    D-Bus name until it exits) and nothing would open.
    """
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with open(CLOSING_FILE) as f:
                pid = int(f.read().strip() or 0)
            os.kill(pid, 0)  # raises if that process is gone
        except (OSError, ValueError):
            break
        if pid == os.getpid():
            break
        time.sleep(0.1)
    try:
        os.remove(CLOSING_FILE)
    except OSError:
        pass


def main():
    wait_for_closing_copy()
    for attempt in range(3):
        app = App()
        app.restart = False
        start = time.monotonic()
        rc = app.run(sys.argv)
        # run() returns 1 at once when registering with D-Bus failed (e.g. the old copy
        # disconnected mid-handover); try again rather than silently not opening.
        if rc == 1 and not app.get_is_registered() and time.monotonic() - start < 5:
            time.sleep(0.5)
            continue
        break
    if app.restart:
        # Start a fresh copy once this one has let go of its D-Bus name, so the newly
        # installed engines get loaded.
        launcher = os.path.realpath(sys.argv[0])
        subprocess.Popen(["sh", "-c", 'sleep 1; exec "$0"', launcher], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # Everything is saved by now.  Exit without running destructors: tearing down the
    # libtorrent session waits ~30 s on the router (UPnP), which kept the app's D-Bus name
    # taken and stopped it from opening again.
    try:
        os.remove(CLOSING_FILE)
    except OSError:
        pass
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(rc)
