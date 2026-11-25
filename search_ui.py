"""Search page and archive.org file picker for Downloader."""

import threading

from gi.repository import Adw, GLib, Gtk

import search

MAX_ROWS = 300
SORTS = ["Top", "Largest", "Newest", "Name"]


def fmt_size(n):
    return GLib.format_size(int(n))


def enabled_engines(cfg):
    on = cfg.get("engines", {})
    return [e for e in search.ENGINES if on.get(e.id, e.default_enabled)]


def open_uri(win, uri):
    Gtk.UriLauncher(uri=uri).launch(win, None, lambda l, r: _finish(l, r))


def _finish(launcher, res):
    try:
        launcher.launch_finish(res)
    except GLib.Error:
        pass


def flat_button(icon, tooltip):
    b = Gtk.Button(icon_name=icon, tooltip_text=tooltip, valign=Gtk.Align.CENTER)
    b.add_css_class("flat")
    return b


# ---------------------------------------------------------------- one result


class ResultRow(Adw.ActionRow):
    def __init__(self, page, r):
        super().__init__(title=r.name, use_markup=False, title_lines=1, activatable=True)
        self.page = page
        self.r = r
        self.set_tooltip_text(r.name)

        icon = Gtk.Image(icon_name="network-transmit-receive-symbolic" if r.kind == "torrent"
                         else "web-browser-symbolic")
        icon.add_css_class("dl-icon")
        icon.add_css_class("search-icon")
        if r.kind != "torrent":
            icon.add_css_class("link")
        self.add_prefix(icon)

        if r.size:
            self.set_subtitle(fmt_size(r.size))
        if r.kind == "torrent":
            self.action = flat_button("folder-download-symbolic", "Download")
        else:
            self.action = flat_button("view-list-bullet-symbolic", "Choose Files to Download")
        self.action.add_css_class("circular")
        self.action.connect("clicked", lambda *_: self.activate_main())
        self.add_suffix(self.action)
        self.connect("activated", lambda *_: self.activate_main())
        if r.link and r.link in page.added:
            self.mark_added()

    def mark_added(self):
        self.action.set_icon_name("object-select-symbolic")
        self.action.set_tooltip_text("Added to Downloads")
        self.action.set_sensitive(False)

    def activate_main(self):
        r = self.r
        if r.kind == "archive":
            ArchiveDialog(self.page.win, r).present(self.page.win)
            return
        if r.link in self.page.added:
            return
        kind = "magnet" if r.link.startswith("magnet:") else "torrent-url"
        self.page.win.add_links([(kind, r.link)], self.page.win.app.cfg["download_dir"])
        self.page.added.add(r.link)
        self.mark_added()


# ---------------------------------------------------------------- search page


class SearchPage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win = win
        self.gen = 0
        self.query = ""
        self.results = []
        self.status = {}       # engine name -> count or error text
        self.pending = []      # engine names still searching
        self.added = set()     # links added during this session
        self._rebuilding_sources = False

        top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        bar = Gtk.Box(spacing=8)
        self.title = Gtk.Label(xalign=0, hexpand=True, ellipsize=3)
        self.title.add_css_class("heading")
        self.spinner = Adw.Spinner(visible=False)
        bar.append(self.title)
        bar.append(self.spinner)
        self.sources = Gtk.StringList.new(["All sources"])
        self.source_dd = Gtk.DropDown(model=self.sources, tooltip_text="Show results from")
        self.source_dd.connect("notify::selected", lambda *_: (not self._rebuilding_sources) and self.refresh())
        self.sort_dd = Gtk.DropDown.new_from_strings(SORTS)
        self.sort_dd.set_tooltip_text("Sort by")
        self.sort_dd.connect("notify::selected", lambda *_: self.refresh())
        engines_btn = flat_button("emblem-system-symbolic", "Search Engines")
        engines_btn.connect("clicked", lambda *_: self.win.app.show_prefs("search"))
        bar.append(self.source_dd)
        bar.append(self.sort_dd)
        bar.append(engines_btn)
        top.append(bar)
        self.engine_line = Gtk.Label(xalign=0, wrap=True)
        self.engine_line.add_css_class("caption")
        self.engine_line.add_css_class("dim-label")
        top.append(self.engine_line)
        self.top = Adw.Clamp(maximum_size=960, child=top, margin_top=12, margin_start=12, margin_end=12,
                             visible=False)
        self.append(self.top)

        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, valign=Gtk.Align.START)
        self.list.add_css_class("boxed-list")
        self.more = Gtk.Label(wrap=True, margin_top=12, visible=False)
        self.more.add_css_class("dim-label")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.list)
        box.append(self.more)
        clamp = Adw.Clamp(maximum_size=960, child=box, margin_top=10, margin_bottom=12,
                          margin_start=12, margin_end=12)
        scroll = Gtk.ScrolledWindow(child=clamp, hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
        self.start_page = Adw.StatusPage(
            icon_name="system-search-symbolic", title="Search for Torrents and Downloads",
            description="Type in the search box at the top and press Enter.\n"
                        "Choose which sites to search with the ⚙ button or in Preferences → Search.")
        self.stack.add_named(self.start_page, "start")
        loading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, valign=Gtk.Align.CENTER)
        sp = Adw.Spinner(width_request=40, height_request=40, halign=Gtk.Align.CENTER)
        self.loading_label = Gtk.Label()
        self.loading_label.add_css_class("dim-label")
        loading.append(sp)
        loading.append(self.loading_label)
        self.stack.add_named(loading, "loading")
        self.empty = Adw.StatusPage(icon_name="system-search-symbolic", title="No Results")
        self.stack.add_named(self.empty, "empty")
        self.stack.add_named(scroll, "list")
        self.append(self.stack)

    # -- searching

    def search(self, query):
        query = query.strip()
        if not query:
            return
        engines = enabled_engines(self.win.app.cfg)
        self.gen += 1
        gen = self.gen
        self.query = query
        self.results = []
        self.status = {}
        self.pending = [e.name for e in engines]
        self.top.set_visible(True)
        if not engines:
            btn = Gtk.Button(label="Choose Search Engines", halign=Gtk.Align.CENTER)
            btn.add_css_class("pill")
            btn.connect("clicked", lambda *_: self.win.app.show_prefs("search"))
            self.empty.set_title("No Search Engines Turned On")
            self.empty.set_description(None)
            self.empty.set_child(btn)
            self.refresh()
            return

        def work(engine):
            try:
                res, err = engine.search(query), None
            except search.SearchError as e:
                res, err = [], str(e)
            except Exception as e:  # a site changed its format, etc.
                res, err = [], f"Unexpected answer ({type(e).__name__})"
            GLib.idle_add(self._engine_done, gen, engine, res, err)

        for e in engines:
            threading.Thread(target=work, args=(e,), daemon=True).start()
        self.refresh()

    def _engine_done(self, gen, engine, res, err):
        if gen != self.gen:
            return False
        self.results += res
        self.status[engine.name] = err if err else len(res)
        if engine.name in self.pending:
            self.pending.remove(engine.name)
        self.refresh()
        return False

    # -- showing

    def visible_results(self):
        src = self.source_dd.get_selected_item()
        src = src.get_string() if src else "All sources"
        best = {}
        for r in self.results:
            if src != "All sources" and r.engine != src:
                continue
            if r.kind == "torrent" and r.seeds <= 0:
                continue  # nobody is sharing it: it would never download
            # The same torrent from two sites: keep the copy with more seeds.
            key = r.link.split("&")[0].lower() if r.link.startswith("magnet:") else (r.link or r.page)
            if key not in best or r.seeds > best[key].seeds:
                best[key] = r
        out = list(best.values())
        sort = SORTS[self.sort_dd.get_selected()]
        if sort == "Top":  # most seeds first; archive.org items (no seeds) by downloads after
            out.sort(key=lambda r: (r.kind != "torrent", -(r.seeds if r.kind == "torrent" else r.popularity)))
        elif sort == "Largest":
            out.sort(key=lambda r: -r.size)
        elif sort == "Newest":
            out.sort(key=lambda r: -r.date)
        else:
            out.sort(key=lambda r: r.name.lower())
        return out

    def _rebuild_sources(self):
        names = ["All sources"] + [e.name for e in search.ENGINES
                                   if any(r.engine == e.name for r in self.results)]
        current = [self.sources.get_string(i) for i in range(self.sources.get_n_items())]
        if names == current:
            return
        sel = self.source_dd.get_selected_item()
        sel = sel.get_string() if sel else "All sources"
        self._rebuilding_sources = True
        self.sources.splice(0, self.sources.get_n_items(), names)
        self.source_dd.set_selected(names.index(sel) if sel in names else 0)
        self._rebuilding_sources = False

    def refresh(self):
        if not self.query:
            self.stack.set_visible_child_name("start")
            return
        self._rebuild_sources()
        results = self.visible_results()
        self.title.set_label(f"{len(results):,} results for “{self.query}”" if results or not self.pending
                             else f"Searching for “{self.query}”…")
        self.spinner.set_visible(bool(self.pending))
        failed = [f"{name}: {v}" for name, v in self.status.items() if not isinstance(v, int)]
        self.engine_line.set_label("\n".join(failed))
        self.engine_line.set_visible(bool(failed))

        self.list.remove_all()
        for r in results[:MAX_ROWS]:
            self.list.append(ResultRow(self, r))
        hidden = len(results) - MAX_ROWS
        self.more.set_visible(hidden > 0)
        if hidden > 0:
            self.more.set_label(f"{hidden:,} more results not shown. Try a more specific search.")

        if results:
            self.stack.set_visible_child_name("list")
        elif self.pending:
            self.loading_label.set_label("Searching " + ", ".join(self.pending) + "…")
            self.stack.set_visible_child_name("loading")
        else:
            if enabled_engines(self.win.app.cfg):
                self.empty.set_title("No Results")
                errors = [v for v in self.status.values() if not isinstance(v, int)]
                self.empty.set_description(
                    "None of the search engines could be reached. See the notes above." if errors and
                    len(errors) == len(self.status) else "Try other words, or turn on more search engines.")
                self.empty.set_child(None)
            self.stack.set_visible_child_name("empty")


# ---------------------------------------------------------------- archive.org files


class ArchiveDialog(Adw.Dialog):
    def __init__(self, win, result):
        super().__init__(content_width=620, content_height=600)
        self.win = win
        self.r = result
        self.files = []
        self.added = set()

        tv = Adw.ToolbarView()
        header = Adw.HeaderBar(title_widget=Adw.WindowTitle(title=result.name))
        tv.add_top_bar(header)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        sp = Adw.Spinner(width_request=32, height_request=32, halign=Gtk.Align.CENTER,
                         valign=Gtk.Align.CENTER)
        self.stack.add_named(sp, "loading")
        self.error = Adw.StatusPage(icon_name="dialog-warning-symbolic", title="Couldn't Load Files")
        self.stack.add_named(self.error, "error")

        page = Adw.PreferencesPage()
        g = Adw.PreferencesGroup()
        all_row = Adw.ButtonRow(title="Download Everything as a Torrent",
                                start_icon_name="folder-download-symbolic")
        all_row.add_css_class("suggested-action")
        all_row.connect("activated", self.on_all)
        g.add(all_row)
        page.add(g)
        self.files_group = Adw.PreferencesGroup(title="Files")
        self.converted = Gtk.CheckButton(label="Show converted copies", valign=Gtk.Align.CENTER)
        self.converted.connect("toggled", lambda *_: self.show_files())
        self.files_group.set_header_suffix(self.converted)
        page.add(self.files_group)
        self.stack.add_named(page, "files")
        tv.set_content(self.stack)
        self.set_child(tv)
        self._rows = []

        def work():
            try:
                files, err = search.archive_files(result.identifier), None
            except Exception as e:
                files, err = [], str(e)
            GLib.idle_add(self.loaded, files, err)

        threading.Thread(target=work, daemon=True).start()

    def loaded(self, files, err):
        if err:
            self.error.set_description(err)
            self.stack.set_visible_child_name("error")
            return False
        self.files = files
        if files and not any(f.original for f in files):
            self.converted.set_active(True)
        self.converted.set_visible(any(not f.original for f in files) and any(f.original for f in files))
        self.show_files()
        self.stack.set_visible_child_name("files")
        return False

    def show_files(self):
        for row in self._rows:
            self.files_group.remove(row)
        self._rows = []
        show_all = self.converted.get_active()
        for f in self.files:
            if not f.original and not show_all:
                continue
            row = Adw.ActionRow(title=f.name, use_markup=False, activatable=True, title_lines=1)
            if f.size:
                row.set_subtitle(fmt_size(f.size))
            btn = flat_button("folder-download-symbolic", "Download")
            btn.add_css_class("circular")
            row.add_suffix(btn)
            if f.url in self.added:
                self._mark(btn)
            btn.connect("clicked", lambda b, f=f: self.download(f, b))
            row.connect("activated", lambda _r, f=f, b=btn: self.download(f, b))
            self.files_group.add(row)
            self._rows.append(row)
        self.files_group.set_description(None if self._rows else "No files to show.")

    @staticmethod
    def _mark(btn):
        btn.set_icon_name("object-select-symbolic")
        btn.set_tooltip_text("Added to Downloads")
        btn.set_sensitive(False)

    def download(self, f, btn):
        if f.url in self.added:
            return
        self.added.add(f.url)
        self._mark(btn)
        self.win.add_links([("link", f.url)], self.win.app.cfg["download_dir"])

    def on_all(self, *_):
        self.win.add_links([("torrent-url", search.archive_torrent_url(self.r.identifier))],
                           self.win.app.cfg["download_dir"])
        self.close()
