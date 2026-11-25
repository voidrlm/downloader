"""Search engines for Downloader (no GTK in here).

Each engine turns a query into a list of Result objects.  Engines run in worker threads and
only use public search APIs/feeds with ordinary requests.  If a site blocks automated
requests (e.g. with a bot-check page), the engine reports that instead of results.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

USER_AGENT = "Downloader/1.0 (+https://github.com/voidrlm/downloader)"

# Public trackers added to magnet links built from a bare info hash.
TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://tracker.dler.org:6969/announce",
    "udp://explodie.org:6969/announce",
]


class SearchError(Exception):
    pass


class Result:
    __slots__ = ("name", "size", "seeds", "leeches", "link", "kind", "engine", "page",
                 "date", "identifier", "popularity")

    def __init__(self, **kw):
        self.name = ""
        self.size = 0          # bytes, 0 = unknown
        self.seeds = -1        # -1 = not applicable (direct downloads)
        self.leeches = -1
        self.link = ""         # magnet / .torrent URL; empty for archive items (pick files first)
        self.kind = "torrent"  # "torrent" or "archive"
        self.engine = ""
        self.page = ""         # web page about the result
        self.date = 0          # unix time, 0 = unknown
        self.identifier = ""   # archive.org item id
        self.popularity = 0    # downloads count, for archive items
        for k, v in kw.items():
            setattr(self, k, v)


def magnet(info_hash, name):
    parts = ["xt=urn:btih:" + info_hash, "dn=" + urllib.parse.quote(name, safe="")]
    parts += ["tr=" + urllib.parse.quote(t, safe="") for t in TRACKERS]
    return "magnet:?" + "&".join(parts)


def fetch(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json, application/xml, */*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(8 * 1024 * 1024)
    except urllib.error.HTTPError as e:
        body = e.read(4096)
        if e.code in (403, 503) and (b"Just a moment" in body or b"cf-" in body or b"Cloudflare" in body):
            raise SearchError("The site is blocking automated searches right now")
        raise SearchError(f"The site answered with error {e.code}")
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if "Name or service not known" in str(reason) or "resolve" in str(reason).lower():
            raise SearchError("Couldn't reach the site (it may be blocked on your network)")
        raise SearchError(f"Couldn't connect: {reason}")
    except TimeoutError:
        raise SearchError("The site took too long to answer")


def parse_size(text):
    """'1.4 GiB' -> bytes."""
    try:
        num, unit = text.split()
        mult = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3, "TiB": 1024 ** 4,
                "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3, "TB": 1000 ** 4}[unit]
        return int(float(num) * mult)
    except (ValueError, KeyError):
        return 0


# ------------------------------------------------------------------ engines


class Engine:
    id = ""
    name = ""
    site = ""
    kind_label = "Torrents"
    description = ""
    default_enabled = True

    def search(self, query):
        raise NotImplementedError


class PirateBay(Engine):
    id = "piratebay"
    name = "The Pirate Bay"
    site = "thepiratebay.org"
    description = "Large general torrent index"

    def search(self, query):
        data = json.loads(fetch("https://apibay.org/q.php?" + urllib.parse.urlencode({"q": query, "cat": 0})))
        out = []
        for d in data:
            if d.get("id") in ("0", 0) or d.get("info_hash", "0" * 40) == "0" * 40:
                continue  # apibay's "No results returned" placeholder
            out.append(Result(
                name=d["name"], size=int(d.get("size", 0)), seeds=int(d.get("seeders", 0)),
                leeches=int(d.get("leechers", 0)), link=magnet(d["info_hash"].lower(), d["name"]),
                engine=self.name, page=f"https://thepiratebay.org/description.php?id={d['id']}",
                date=int(d.get("added", 0))))
        return out


class TorrentsCsv(Engine):
    id = "torrentscsv"
    name = "Torrents-CSV"
    site = "torrents-csv.com"
    description = "Open database of millions of torrents"

    def search(self, query):
        data = json.loads(fetch("https://torrents-csv.com/service/search?" +
                                urllib.parse.urlencode({"q": query, "size": 100})))
        return [Result(name=d["name"], size=int(d.get("size_bytes", 0)), seeds=int(d.get("seeders", 0)),
                       leeches=int(d.get("leechers", 0)), link=magnet(d["infohash"], d["name"]),
                       engine=self.name, date=int(d.get("created_unix", 0)),
                       page="https://torrents-csv.com/search?q=" + urllib.parse.quote(d["name"]))
                for d in data.get("torrents", [])]


class Nyaa(Engine):
    id = "nyaa"
    name = "Nyaa"
    site = "nyaa.si"
    description = "Anime, manga and Asian media"
    default_enabled = False

    NS = "{https://nyaa.si/xmlns/nyaa}"

    def search(self, query):
        raw = fetch("https://nyaa.si/?" + urllib.parse.urlencode(
            {"page": "rss", "q": query, "c": "0_0", "f": "0"}))
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            raise SearchError("Got an unexpected answer")
        out = []
        for it in root.iterfind("channel/item"):
            def t(tag):
                return (it.findtext(tag) or "").strip()
            name = t("title")
            ih = t(self.NS + "infoHash")
            date = 0
            try:
                date = int(time.mktime(time.strptime(t("pubDate")[:25], "%a, %d %b %Y %H:%M:%S")))
            except ValueError:
                pass
            out.append(Result(name=name, size=parse_size(t(self.NS + "size")),
                              seeds=int(t(self.NS + "seeders") or 0), leeches=int(t(self.NS + "leechers") or 0),
                              link=magnet(ih, name) if ih else t("link"), engine=self.name,
                              page=t("guid"), date=date))
        return out


class InternetArchive(Engine):
    id = "archive"
    name = "Internet Archive"
    site = "archive.org"
    kind_label = "Direct downloads"
    description = "Public-domain films, music, books and software, as direct downloads"

    MEDIA = "(movies OR audio OR software OR texts OR image OR etree OR data)"

    def search(self, query):
        params = [("q", f"({query}) AND mediatype:{self.MEDIA}"), ("rows", "75"), ("output", "json")]
        params += [("fl[]", f) for f in ("identifier", "title", "item_size", "downloads", "mediatype", "date")]
        data = json.loads(fetch("https://archive.org/advancedsearch.php?" + urllib.parse.urlencode(params)))
        out = []
        for d in data.get("response", {}).get("docs", []):
            title = d.get("title") or d["identifier"]
            if isinstance(title, list):
                title = title[0]
            date = 0
            if d.get("date"):
                try:
                    date = int(time.mktime(time.strptime(str(d["date"])[:10], "%Y-%m-%d")))
                except ValueError:
                    pass
            out.append(Result(name=title, size=int(d.get("item_size") or 0), kind="archive",
                              engine=self.name, identifier=d["identifier"], date=date,
                              popularity=int(d.get("downloads") or 0),
                              page="https://archive.org/details/" + d["identifier"]))
        return out


ENGINES = [PirateBay(), TorrentsCsv(), Nyaa(), InternetArchive()]
BY_ID = {e.id: e for e in ENGINES}


# ------------------------------------------------------------------ archive.org files

SKIP_FORMATS = {"Metadata", "Item Tile", "Archive BitTorrent", "Thumbnail", "JSON", "Columbia Peaks",
                "Spectrogram", "PNG", "Web Archive GZ", "Activity Log", "BitTorrent",
                "BitTorrentContents"}


class ArchiveFile:
    __slots__ = ("name", "size", "format", "original", "url")

    def __init__(self, name, size, fmt, original, url):
        self.name, self.size, self.format, self.original, self.url = name, size, fmt, original, url


def archive_files(identifier):
    """Files in an archive.org item (call from a worker thread). Originals come first."""
    data = json.loads(fetch("https://archive.org/metadata/" + urllib.parse.quote(identifier)))
    if not data.get("files"):
        raise SearchError("This item has no downloadable files")
    files = []
    for f in data["files"]:
        name = f.get("name", "")
        fmt = f.get("format", "")
        if (fmt in SKIP_FORMATS or name.endswith(("_meta.xml", "_files.xml", "_meta.sqlite",
                                                   "_reviews.xml", "__ia_thumb.jpg"))
                or f.get("private") == "true"):
            continue
        url = f"https://archive.org/download/{urllib.parse.quote(identifier)}/{urllib.parse.quote(name)}"
        files.append(ArchiveFile(name, int(f.get("size") or 0), fmt, f.get("source") == "original", url))
    files.sort(key=lambda f: (not f.original, f.name.lower()))
    return files


def archive_torrent_url(identifier):
    """Whole-item torrent; archive.org web-seeds it, so it downloads even with no peers."""
    q = urllib.parse.quote(identifier)
    return f"https://archive.org/download/{q}/{q}_archive.torrent"
