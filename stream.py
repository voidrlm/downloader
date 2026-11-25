"""Local HTTP server that lets a video player watch a torrent while it downloads.

The player asks for byte ranges (seeking is just a new range).  For each piece the reply
needs, the server asks libtorrent to fetch it next (piece deadlines) and waits until it's
downloaded and verified before reading it from disk and sending it.  Listens on 127.0.0.1
only, under a random token, so nothing else on the network can use it.
"""

import mimetypes
import os
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

READAHEAD = 24 << 20   # bytes to fetch ahead of what the player is reading
CHUNK = 256 << 10

mimetypes.add_type("video/x-matroska", ".mkv")
mimetypes.add_type("text/plain", ".srt")
mimetypes.add_type("text/vtt", ".vtt")


class StreamServer:
    def __init__(self, torrents):
        self.torrents = torrents
        self.token = secrets.token_urlsafe(12)
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_HEAD(self):
                server.serve(self, body=False)

            def do_GET(self):
                server.serve(self, body=True)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, key, index, name):
        return (f"http://127.0.0.1:{self.port}/{self.token}/{key}/{index}/"
                + urllib.parse.quote(name))

    def stop(self):
        self.httpd.shutdown()

    # -- serving

    def serve(self, req, body):
        parts = urllib.parse.urlparse(req.path).path.strip("/").split("/")
        if len(parts) < 3 or parts[0] != self.token or not parts[2].isdigit():
            req.send_error(404)
            return
        key, index = parts[1], int(parts[2])
        target = self.torrents.stream_target(key, index)
        if not target:
            req.send_error(404)
            return
        h, ti, path, size = target

        start, end = 0, size - 1
        rng = req.headers.get("Range", "")
        partial = rng.startswith("bytes=")
        if partial:
            a, _, b = rng[6:].split(",")[0].partition("-")
            try:
                if a:
                    start = int(a)
                    end = int(b) if b else size - 1
                else:  # "bytes=-N": the last N bytes
                    start = max(0, size - int(b))
            except ValueError:
                req.send_error(400)
                return
            end = min(end, size - 1)
            if start > end:
                req.send_response(416)
                req.send_header("Content-Range", f"bytes */{size}")
                req.send_header("Content-Length", "0")
                req.end_headers()
                return

        req.send_response(206 if partial else 200)
        req.send_header("Content-Type", mimetypes.guess_type(path)[0] or "application/octet-stream")
        req.send_header("Accept-Ranges", "bytes")
        req.send_header("Content-Length", str(end - start + 1))
        if partial:
            req.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        req.end_headers()
        if not body:
            return

        try:
            self._send_range(req, h, ti, index, path, start, end)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass  # the player seeked elsewhere or closed

    def _send_range(self, req, h, ti, index, path, start, end):
        key = str(h.info_hash())
        pl = ti.piece_length()
        last_piece = ti.map_file(index, ti.files().file_size(index) - 1, 0).piece
        pos = start
        prioritized_from = -1
        while pos <= end:
            pr = ti.map_file(index, pos, 0)
            piece = pr.piece
            if piece != prioritized_from:
                self._prioritize(h, piece, min(last_piece, piece + max(4, READAHEAD // pl)))
                prioritized_from = piece
            if not self._wait_for(h, piece):
                return
            data = self.torrents.read_piece(key, h, piece)
            if data is None:
                return
            n = min(len(data) - pr.start, end - pos + 1)
            view = memoryview(data)[pr.start:pr.start + n]
            for i in range(0, n, CHUNK):
                req.wfile.write(view[i:i + CHUNK])
            pos += n

    @staticmethod
    def _prioritize(h, first, last):
        for k, p in enumerate(range(first, last + 1)):
            if not h.have_piece(p):
                h.set_piece_deadline(p, k * 40)

    @staticmethod
    def _wait_for(h, piece):
        """Block until the piece is downloaded; False if the torrent went away."""
        while True:
            if not h.is_valid():
                return False
            if h.have_piece(piece):
                return True
            time.sleep(0.05)
