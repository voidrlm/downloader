"""Download engines for Downloader (no GTK in here).

TorrentEngine wraps libtorrent (the engine qBittorrent uses) for magnet links and .torrent
files.  LinkEngine drives an aria2c child process over JSON-RPC for http/https/ftp links,
splitting each file across many connections so direct downloads go as fast as the server
allows.  Both report their downloads as Item objects so the UI treats them the same way.
"""

import base64
import json
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request

try:
    import libtorrent as lt
except ImportError:  # the app still works for direct links without it
    lt = None

# Item.state values
METADATA = "metadata"      # magnet link: still fetching the torrent's file list
CHECKING = "checking"      # verifying data already on disk
CONNECTING = "connecting"  # direct link: waiting for the server's first reply
DOWNLOADING = "downloading"
QUEUED = "queued"
PAUSED = "paused"
SEEDING = "seeding"        # finished torrent that is still being shared
DONE = "done"
ERROR = "error"

FINISHED_STATES = (SEEDING, DONE)

VIDEO_EXT = (".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm", ".wmv", ".ts", ".m2ts", ".mpg",
             ".mpeg", ".flv", ".ogv")
SUB_EXT = (".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".smi")


class Item:
    """One download as the UI sees it."""

    __slots__ = ("key", "kind", "name", "total", "done", "down", "up", "state", "peers",
                 "seeds", "error", "save_dir", "path", "added", "link", "ratio", "stream")

    def __init__(self, key, kind):
        self.key = key
        self.kind = kind  # "torrent" or "link"
        self.name = ""
        self.total = 0
        self.done = 0
        self.down = 0
        self.up = 0
        self.state = QUEUED
        self.peers = 0
        self.seeds = 0
        self.error = ""
        self.save_dir = ""
        self.path = ""
        self.added = 0.0
        self.link = ""
        self.ratio = 0.0
        self.stream = False  # has a video that can be played while downloading

    @property
    def progress(self):
        if self.state in FINISHED_STATES:
            return 1.0
        return self.done / self.total if self.total else 0.0

    @property
    def eta(self):
        if self.state != DOWNLOADING or self.down <= 0 or not self.total:
            return None
        return max(0, self.total - self.done) / self.down


# ------------------------------------------------------------------ input parsing

def classify(text):
    """Return (kind, value) for one pasted token, or None if it isn't something we download.

    kind is "magnet", "torrent-file", "torrent-url" or "link".
    """
    t = text.strip().strip("<>\"'")
    if not t:
        return None
    low = t.lower()
    if low.startswith("magnet:?"):
        return ("magnet", t)
    if low.startswith("file://"):
        t = urllib.parse.unquote(urllib.parse.urlparse(t).path)
        low = t.lower()
    if low.startswith(("http://", "https://", "ftp://", "ftps://")):
        path = urllib.parse.urlparse(t).path.lower()
        return ("torrent-url", t) if path.endswith(".torrent") else ("link", t)
    if os.path.isfile(os.path.expanduser(t)) and low.endswith(".torrent"):
        return ("torrent-file", os.path.abspath(os.path.expanduser(t)))
    # A bare info hash: 40 hex characters (v1) or 32 base32 characters.
    if len(t) == 40 and all(c in "0123456789abcdef" for c in low):
        return ("magnet", "magnet:?xt=urn:btih:" + low)
    if len(t) == 32 and all(c in "abcdefghijklmnopqrstuvwxyz234567" for c in low):
        return ("magnet", "magnet:?xt=urn:btih:" + t.upper())
    return None


def split_links(text):
    """All downloadable tokens in a blob of pasted text."""
    found = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if os.path.isfile(os.path.expanduser(line)):  # a file path that contains spaces
            c = classify(line)
            if c:
                found.append(c)
            continue
        for token in line.split():
            c = classify(token)
            if c:
                found.append(c)
    seen, out = set(), []
    for c in found:
        if c[1] not in seen:
            seen.add(c[1])
            out.append(c)
    return out


def fetch_torrent_url(url, timeout=30):
    """Download a .torrent file to a temporary path (call from a worker thread)."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 Downloader"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(20 * 1024 * 1024)
    if not data.startswith(b"d"):
        raise ValueError("That link didn't return a .torrent file")
    fd, path = tempfile.mkstemp(suffix=".torrent")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


# ------------------------------------------------------------------ torrents

class TorrentEngine:
    available = lt is not None
    missing_hint = "Torrents need libtorrent:  sudo dnf install rb_libtorrent-python3"

    def __init__(self, state_dir, settings):
        self.dir = os.path.join(state_dir, "torrents")
        os.makedirs(self.dir, exist_ok=True)
        self.handles = {}       # key -> torrent_handle
        self.errors = {}        # key -> last error text
        self.finished = []      # names finished since the last pop_finished()
        self._was_done = set()  # keys already seen finished (so we notify once)
        self._seed_stopped = set()
        self._last_resume_save = time.monotonic()
        self.media = {}         # key -> {"video": file index or None, "subs": [file indexes]}
        self.seed = True
        self.ses = lt.session({
            "listen_interfaces": "0.0.0.0:6881,[::]:6881",
            "enable_dht": True, "enable_lsd": True, "enable_upnp": True, "enable_natpmp": True,
            "dht_bootstrap_nodes": "dht.libtorrent.org:25401,router.bittorrent.com:6881,"
                                   "router.utorrent.com:6881,dht.transmissionbt.com:6881",
            "alert_mask": (lt.alert.category_t.error_notification
                           | lt.alert.category_t.status_notification
                           | lt.alert.category_t.storage_notification),
            "user_agent": "Downloader/1.0 libtorrent/" + lt.__version__,
            "connections_limit": 800,
            "active_downloads": -1, "active_seeds": -1, "active_limit": -1,
        })
        self.apply_settings(settings)
        self._piece_waiters = {}  # (key, piece) -> [[threading.Event, data]]
        self._lock = threading.Lock()
        self._alive = True
        self._load_saved()
        # Alerts are handled on their own thread so piece reads for the stream server
        # come back in milliseconds, not on the UI's 1 s refresh.
        self._alert_thread = threading.Thread(target=self._alert_loop, daemon=True)
        self._alert_thread.start()

    def _alert_loop(self):
        # Not ses.wait_for_alert(): the Python binding holds the GIL while it waits, which
        # starves the GTK thread.  A 20 ms sleep releases it and still answers quickly.
        while self._alive:
            self._handle_alerts()
            time.sleep(0.02)

    def read_piece(self, key, h, piece, timeout=60):
        """Piece data straight from libtorrent (call from a worker thread, after have_piece).

        Reading the file instead could return blocks libtorrent hasn't written yet.
        """
        slot = [threading.Event(), None]
        with self._lock:
            self._piece_waiters.setdefault((key, piece), []).append(slot)
        h.read_piece(piece)
        slot[0].wait(timeout)
        return slot[1]
    # -- settings / persistence

    def apply_settings(self, s):
        self.seed = bool(s.get("seed", True))
        self.ses.apply_settings({
            "download_rate_limit": int(s.get("max_down_kib", 0)) * 1024,
            "upload_rate_limit": int(s.get("max_up_kib", 0)) * 1024,
        })
        if self.seed:
            for key in list(self._seed_stopped):
                h = self.handles.get(key)
                if h and h.is_valid():
                    h.resume()
            self._seed_stopped.clear()

    def _resume_file(self, key):
        return os.path.join(self.dir, key + ".fastresume")

    def _load_saved(self):
        for fn in sorted(os.listdir(self.dir)):
            if not fn.endswith(".fastresume"):
                continue
            path = os.path.join(self.dir, fn)
            try:
                with open(path, "rb") as f:
                    atp = lt.read_resume_data(f.read())
                self._add(atp)
            except Exception as e:  # a corrupt resume file shouldn't stop the app
                print("Couldn't restore", fn, e)

    @staticmethod
    def _key(h):
        return str(h.info_hash())

    def _add(self, atp, start=False):
        # Not auto-managed: torrents start right away and pause/resume do exactly that.
        atp.flags = (atp.flags & ~lt.torrent_flags.auto_managed) & ~lt.torrent_flags.duplicate_is_error
        if start:  # new torrents; restored ones keep the paused state they were saved with
            atp.flags = atp.flags & ~lt.torrent_flags.paused
        h = self.ses.add_torrent(atp)
        key = self._key(h)
        new = key not in self.handles
        self.handles[key] = h
        st = h.status()
        if st.is_finished or st.is_seeding:
            self._was_done.add(key)
        if new:
            h.save_resume_data(lt.torrent_handle.save_info_dict)
        self._media(key, h)
        return key

    # -- video: play while downloading

    @staticmethod
    def piece_range(ti, index):
        size = ti.files().file_size(index)
        if size <= 0:
            return None
        return ti.map_file(index, 0, 0).piece, ti.map_file(index, size - 1, 0).piece

    def _media(self, key, h):
        """Find the main video and subtitle files once the torrent's file list is known, and
        set the torrent up for watching while it downloads: pieces in order, subtitles and the
        video's first and last pieces (where players look for the index) before anything else.
        """
        if key in self.media:
            return self.media[key]
        ti = h.torrent_file()
        if ti is None:
            return None  # magnet link still fetching its file list
        fs = ti.files()
        videos, subs = [], []
        for i in range(fs.num_files()):
            path = fs.file_path(i).lower()
            name = os.path.basename(path)
            if path.endswith(VIDEO_EXT) and not name.startswith("sample") and ".sample." not in name:
                videos.append((fs.file_size(i), i))
            elif path.endswith(SUB_EXT):
                subs.append(i)
        m = {"video": max(videos)[1] if videos else None, "subs": subs}
        self.media[key] = m
        if m["video"] is None:
            return m
        h.set_flags(lt.torrent_flags.sequential_download)
        if subs:
            prios = list(h.get_file_priorities())
            for i in subs:
                prios[i] = 7
            h.prioritize_files(prios)
            for i in subs:
                r = self.piece_range(ti, i)
                if r:
                    for p in range(r[0], r[1] + 1):
                        h.set_piece_deadline(p, 0)
        r = self.piece_range(ti, m["video"])
        if r:
            first, last = r
            pl = ti.piece_length()
            head = max(2, (8 << 20) // pl)
            tail = max(1, (4 << 20) // pl)
            for k, p in enumerate(range(first, min(last, first + head) + 1)):
                h.set_piece_deadline(p, 300 + k * 50)
            for p in range(max(first, last - tail + 1), last + 1):
                h.set_piece_deadline(p, 800)
        return m

    def video_info(self, key):
        """Where the torrent's video and subtitles are, and whether they're complete."""
        h = self.handles.get(key)
        if not h:
            return None
        m = self._media(key, h)
        if not m or m["video"] is None:
            return None
        ti = h.torrent_file()
        fs = ti.files()
        save = h.status().save_path
        prog = h.file_progress()
        vid = m["video"]
        subs = sorted(m["subs"], key=lambda i: (not any(w in fs.file_path(i).lower() for w in ("eng", "english")),
                                                fs.file_path(i).lower()))
        return {
            "index": vid,
            "name": os.path.basename(fs.file_path(vid)),
            "path": os.path.join(save, fs.file_path(vid)),
            "size": fs.file_size(vid),
            "complete": prog[vid] >= fs.file_size(vid),
            "subs": [(os.path.join(save, fs.file_path(i)), prog[i] >= fs.file_size(i)) for i in subs],
        }

    def stream_target(self, key, index):
        """(handle, torrent_info, path on disk, size) for the stream server."""
        h = self.handles.get(key)
        if not h or not h.is_valid():
            return None
        ti = h.torrent_file()
        if ti is None or index >= ti.files().num_files():
            return None
        fs = ti.files()
        return h, ti, os.path.join(h.status().save_path, fs.file_path(index)), fs.file_size(index)

    # -- adding

    def add_magnet(self, uri, save_dir):
        atp = lt.parse_magnet_uri(uri)
        atp.save_path = save_dir
        return self._add(atp, start=True)

    def add_file(self, path, save_dir):
        atp = lt.add_torrent_params()
        atp.ti = lt.torrent_info(path)
        atp.save_path = save_dir
        return self._add(atp, start=True)

    # -- actions

    def pause(self, key):
        h = self.handles.get(key)
        if h:
            h.pause()
            h.save_resume_data(lt.torrent_handle.save_info_dict)

    def resume(self, key):
        h = self.handles.get(key)
        if h:
            self._seed_stopped.discard(key)
            self.errors.pop(key, None)
            if h.status().errc.value():
                h.clear_error()
            h.resume()

    def remove(self, key, delete_files=False):
        h = self.handles.pop(key, None)
        if not h:
            return
        self.ses.remove_torrent(h, lt.session.delete_files if delete_files else 0)
        for s in (self._was_done, self._seed_stopped):
            s.discard(key)
        self.media.pop(key, None)
        try:
            os.remove(self._resume_file(key))
        except FileNotFoundError:
            pass

    # -- polling

    def _handle_alerts(self):
        for a in self.ses.pop_alerts():
            if isinstance(a, lt.save_resume_data_alert):
                key = self._key(a.handle)
                if key in self.handles:
                    try:
                        with open(self._resume_file(key), "wb") as f:
                            f.write(lt.write_resume_data_buf(a.params))
                    except OSError as e:
                        print("Couldn't save resume data:", e)
            elif isinstance(a, lt.read_piece_alert):
                with self._lock:
                    waiters = self._piece_waiters.pop((self._key(a.handle), a.piece), [])
                data = None if a.error.value() else bytes(a.buffer)
                for slot in waiters:
                    slot[1] = data
                    slot[0].set()
            elif isinstance(a, lt.metadata_received_alert):
                a.handle.save_resume_data(lt.torrent_handle.save_info_dict)
                self._media(self._key(a.handle), a.handle)
            elif isinstance(a, (lt.torrent_error_alert, lt.file_error_alert)):
                try:
                    self.errors[self._key(a.handle)] = a.message()
                except Exception:
                    pass

    def poll(self):
        now = time.monotonic()
        periodic = now - self._last_resume_save > 60
        if periodic:
            self._last_resume_save = now
        S = lt.torrent_status
        items = []
        for key, h in list(self.handles.items()):
            if not h.is_valid():
                self.handles.pop(key)
                continue
            st = h.status()
            it = Item("t:" + key, "torrent")
            it.name = st.name or "Fetching torrent info…"
            it.total = st.total_wanted
            it.done = st.total_wanted_done
            it.down = st.download_payload_rate
            it.up = st.upload_payload_rate
            it.peers = st.num_peers
            it.seeds = st.num_seeds
            it.save_dir = st.save_path
            it.path = os.path.join(st.save_path, st.name) if st.name else st.save_path
            it.added = st.added_time
            it.ratio = (st.all_time_upload / st.total_wanted) if st.total_wanted else 0.0
            m = self._media(key, h) if st.has_metadata else None
            it.stream = bool(m and m["video"] is not None)
            paused = bool(st.flags & lt.torrent_flags.paused)
            finished = st.is_finished or st.is_seeding
            if finished and key not in self._was_done:
                self._was_done.add(key)
                self.finished.append(it.name)
                h.save_resume_data(lt.torrent_handle.save_info_dict)
                if not self.seed:
                    h.pause()
                    self._seed_stopped.add(key)
                    paused = True
            err = st.errc.message() if st.errc.value() else self.errors.get(key, "")
            if err and paused:
                it.state, it.error = ERROR, err
            elif finished:
                it.state = DONE if (paused or not self.seed) else SEEDING
            elif paused:
                it.state = PAUSED
            elif st.state == S.downloading_metadata:
                it.state = METADATA
            elif st.state in (S.checking_files, S.checking_resume_data):
                it.state = CHECKING
                it.done = int(st.progress * st.total_wanted)
            else:
                it.state = DOWNLOADING
            try:
                it.link = lt.make_magnet_uri(h)
            except Exception:
                it.link = ""
            items.append(it)
            if periodic and h.need_save_resume_data():
                h.save_resume_data(lt.torrent_handle.save_info_dict)
        return items

    def pop_finished(self):
        out, self.finished = self.finished, []
        return out

    def shutdown(self, timeout=6.0):
        """Save resume data for every torrent, then stop the session."""
        self._alive = False
        self._alert_thread.join(1)
        self.ses.pause()
        waiting = 0
        for h in self.handles.values():
            if h.is_valid():
                h.save_resume_data(lt.torrent_handle.save_info_dict)
                waiting += 1
        end = time.monotonic() + timeout
        while waiting > 0 and time.monotonic() < end:
            if self.ses.wait_for_alert(500) is None:
                continue
            for a in self.ses.pop_alerts():
                if isinstance(a, lt.save_resume_data_alert):
                    waiting -= 1
                    key = self._key(a.handle)
                    try:
                        with open(self._resume_file(key), "wb") as f:
                            f.write(lt.write_resume_data_buf(a.params))
                    except OSError:
                        pass
                elif isinstance(a, lt.save_resume_data_failed_alert):
                    waiting -= 1
        # Destroying the session isn't done here: with UPnP on, libtorrent then waits ~30 s
        # for the router to drop its port mappings.  main() ends the process with os._exit
        # instead; the mappings expire on the router by themselves.


# ------------------------------------------------------------------ direct links (aria2)

class LinkEngine:
    missing_hint = "Direct links need aria2:  sudo dnf install aria2"

    def __init__(self, state_dir, settings):
        self.binary = os.environ.get("DOWNLOADER_ARIA2C") or shutil.which("aria2c")
        self.available = bool(self.binary)
        self.file = os.path.join(state_dir, "links.json")
        self.records = {}   # gid -> dict (what we persist)
        self.finished = []
        self._pending_delete = {}  # gid -> [paths] to remove once aria2 lets go of them
        self._dirty = False
        self.proc = None
        try:
            with open(self.file) as f:
                for r in json.load(f):
                    self.records[r["gid"]] = r
        except (OSError, ValueError, KeyError):
            pass
        if not self.available:
            return
        self.connections = int(settings.get("connections", 16))
        self._start(settings)
        for r in sorted(self.records.values(), key=lambda r: r.get("added", 0)):
            if r.get("state") != DONE:
                self._submit(r, paused=r.get("state") == PAUSED)

    # -- aria2 process / RPC

    def _start(self, s):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()
        self.secret = secrets.token_hex(16)
        conns = str(max(1, min(16, int(s.get("connections", 16)))))
        args = [
            self.binary, "--enable-rpc", "--rpc-listen-all=false",
            f"--rpc-listen-port={self.port}", f"--rpc-secret={self.secret}",
            f"--stop-with-process={os.getpid()}",
            # Speed: many connections per file, small split size so even mid-size files use them.
            f"--max-connection-per-server={conns}", f"--split={conns}", "--min-split-size=1M",
            "--piece-length=1M", "--disk-cache=64M", "--file-allocation=none",
            "--optimize-concurrent-downloads=true", "--uri-selector=adaptive",
            f"--max-concurrent-downloads={int(s.get('max_active', 5))}",
            f"--max-overall-download-limit={int(s.get('max_down_kib', 0))}K",
            # Reliability: resume partial files, keep retrying flaky servers.
            "--continue=true", "--always-resume=false", "--max-resume-failure-tries=5",
            "--max-tries=10", "--retry-wait=3", "--connect-timeout=20", "--timeout=30",
            "--auto-file-renaming=true", "--allow-overwrite=false", "--remote-time=true",
            "--content-disposition-default-utf8=true", "--follow-torrent=false",
            "--follow-metalink=false", "--enable-dht=false", "--bt-enable-lpd=false",
            "--console-log-level=warn", "--summary-interval=0", "--quiet=true",
            "--user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0 Safari/537.36",
        ]
        self.proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True)
        end = time.monotonic() + 5
        while time.monotonic() < end:
            try:
                self.call("aria2.getVersion")
                return
            except OSError:
                time.sleep(0.05)
        self.available = False
        self.missing_hint = "aria2c didn't start"

    def call(self, method, *params):
        if not method.startswith("system."):  # system.* methods take no token
            params = ("token:" + self.secret, *params)
        body = json.dumps({"jsonrpc": "2.0", "id": "d", "method": method,
                           "params": list(params)}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/jsonrpc", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                reply = json.load(r)
        except urllib.error.HTTPError as e:  # aria2 answers RPC errors with 400 + a JSON body
            reply = json.load(e)
        if "error" in reply:
            raise RuntimeError(reply["error"].get("message", "aria2 error"))
        return reply["result"]

    def _save(self):
        if self._dirty:
            _write_json(self.file, list(self.records.values()))
            self._dirty = False

    # -- adding

    @staticmethod
    def _guess_name(url):
        p = urllib.parse.urlparse(url)
        name = urllib.parse.unquote(os.path.basename(p.path.rstrip("/")))
        return name or p.hostname or url

    def _submit(self, r, paused=False):
        opts = {"dir": r["dir"], "gid": r["gid"]}
        if r.get("out"):
            opts["out"] = r["out"]
        if paused:
            opts["pause"] = "true"
        try:
            self.call("aria2.removeDownloadResult", r["gid"])
        except (RuntimeError, OSError):
            pass
        self.call("aria2.addUri", [r["url"]], opts)

    def add(self, url, save_dir):
        if not self.available:
            raise RuntimeError(self.missing_hint)
        gid = secrets.token_hex(8)
        r = {"gid": gid, "url": url, "dir": save_dir, "added": time.time(),
             "name": self._guess_name(url), "state": QUEUED, "total": 0, "path": "", "out": ""}
        self._submit(r)
        self.records[gid] = r
        self._dirty = True
        self._save()
        return gid

    # -- actions

    def pause(self, gid):
        try:
            self.call("aria2.forcePause", gid)
        except RuntimeError:
            pass
        r = self.records.get(gid)
        if r:
            r["state"] = PAUSED
            self._dirty = True

    def resume(self, gid):
        r = self.records.get(gid)
        if not r:
            return
        try:
            self.call("aria2.unpause", gid)
        except RuntimeError:
            # Errored or no longer known to aria2: add it again; it continues the partial file.
            r["error"] = ""
            self._submit(r)
        r["state"] = QUEUED
        self._dirty = True

    def remove(self, gid, delete_files=False):
        r = self.records.pop(gid, None)
        self._dirty = True
        if self.available:
            try:
                self.call("aria2.forceRemove", gid)
            except (RuntimeError, OSError):
                pass
        if delete_files and r and r.get("path"):
            self._pending_delete[gid] = [r["path"], r["path"] + ".aria2"]
        self._save()

    def _delete_pending(self, live_gids):
        for gid in list(self._pending_delete):
            if gid in live_gids:
                continue  # still being torn down by aria2
            for p in self._pending_delete.pop(gid):
                try:
                    os.remove(p)
                except OSError:
                    pass
            try:
                self.call("aria2.removeDownloadResult", gid)
            except (RuntimeError, OSError):
                pass

    # -- polling

    KEYS = ["gid", "status", "totalLength", "completedLength", "downloadSpeed", "connections",
            "errorMessage", "dir", "files"]

    def poll(self):
        live = {}
        if self.available:
            try:
                res = self.call("system.multicall", [
                    {"methodName": "aria2.tellActive", "params": ["token:" + self.secret, self.KEYS]},
                    {"methodName": "aria2.tellWaiting", "params": ["token:" + self.secret, 0, 1000, self.KEYS]},
                    {"methodName": "aria2.tellStopped", "params": ["token:" + self.secret, 0, 1000, self.KEYS]},
                ])
                for group in res:
                    for d in group[0] if isinstance(group, list) else []:
                        live[d["gid"]] = d
            except (OSError, RuntimeError, ValueError):
                pass
            self._delete_pending({g for g, d in live.items() if d["status"] in ("active", "waiting", "paused")})

        items = []
        for gid, r in list(self.records.items()):
            it = Item("h:" + gid, "link")
            d = live.get(gid)
            if d:
                total, done = int(d["totalLength"]), int(d["completedLength"])
                path = d["files"][0]["path"] if d.get("files") else ""
                if path and path != r.get("path"):
                    r["path"], r["out"] = path, os.path.basename(path)
                    r["name"] = os.path.basename(path)
                    self._dirty = True
                status = d["status"]
                state = {"active": DOWNLOADING, "waiting": QUEUED, "paused": PAUSED,
                         "error": ERROR, "complete": DONE}.get(status, r.get("state", QUEUED))
                if status == "active" and total == 0:
                    state = CONNECTING
                if state == DONE and r.get("state") != DONE:
                    self.finished.append(r["name"])
                if state == ERROR:
                    r["error"] = d.get("errorMessage") or "Download failed"
                if state != r.get("state") or total != r.get("total"):
                    r["state"], r["total"] = state, total
                    self._dirty = True
                it.total, it.done = total, done if state != DONE else total
                it.down = int(d["downloadSpeed"])
                it.peers = int(d["connections"])
            else:
                state = r.get("state", QUEUED)
                if state not in (DONE, ERROR):
                    state = PAUSED if not self.available else state
                it.total = r.get("total", 0)
                it.done = it.total if state == DONE else 0
            it.state = state
            it.error = r.get("error", "") if state == ERROR else ""
            it.name = r.get("name") or self._guess_name(r["url"])
            it.save_dir = r["dir"]
            it.path = r.get("path") or os.path.join(r["dir"], it.name)
            it.added = r.get("added", 0)
            it.link = r["url"]
            items.append(it)
        self._save()
        return items

    def apply_settings(self, s):
        if not self.available:
            return
        conns = str(max(1, min(16, int(s.get("connections", 16)))))
        try:
            self.call("aria2.changeGlobalOption", {
                "max-overall-download-limit": f"{int(s.get('max_down_kib', 0))}K",
                "max-concurrent-downloads": str(int(s.get("max_active", 5))),
                "max-connection-per-server": conns, "split": conns,
            })
        except (OSError, RuntimeError) as e:
            print("aria2 settings:", e)

    def pop_finished(self):
        out, self.finished = self.finished, []
        return out

    def shutdown(self):
        """Pause everything (which writes each .aria2 control file), then stop aria2.

        Pausing first matters: a killed aria2 leaves stale control files and the downloads
        start over from zero next time.  aria2's own shutdown call takes ~4 s; SIGTERM on an
        idle aria2 is instant.
        """
        self._save()
        if not self.proc or self.proc.poll() is not None:
            return
        try:
            self.call("aria2.forcePauseAll")
            end = time.monotonic() + 3
            while time.monotonic() < end and self.call("aria2.tellActive", ["gid"]):
                time.sleep(0.05)
        except Exception:
            pass
        self.proc.terminate()
        try:
            self.proc.wait(3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
