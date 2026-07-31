import hashlib
import http.server
import json
import socketserver
import urllib.parse
from datetime import datetime
from pathlib import Path

from arr_common import config

from . import data

PORT = 8099

BASE_DIR = Path(__file__).resolve().parent.parent.parent
HTML_DIR = BASE_DIR / "HTML"
CSS_DIR = BASE_DIR / "CSS"
JS_DIR = BASE_DIR / "JS"
BG_IMAGE_PATH = Path(__file__).resolve().parent.parent.parent / "bg.png"


def sniff_image_content_type(image_bytes):
    """The background gets swapped out from time to time and isn't always
    actually the format its filename suggests, so detect the real type from
    the file's magic bytes rather than assuming PNG."""
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"GIF87a") or image_bytes.startswith(b"GIF89a"):
        return "image/gif"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "application/octet-stream"


try:
    with open(BG_IMAGE_PATH, "rb") as f:
        BG_IMAGE_BYTES = f.read()
except FileNotFoundError:
    BG_IMAGE_BYTES = b""

BG_IMAGE_CONTENT_TYPE = sniff_image_content_type(BG_IMAGE_BYTES)
# Content hash in the URL so swapping the background file always busts any
# browser cache -- no stale image left behind after an update.
BG_VERSION = hashlib.sha256(BG_IMAGE_BYTES).hexdigest()[:10]


def _load_page(name):
    text = (HTML_DIR / f"{name}.html").read_text()
    return text.replace("/bg.png", f"/bg.png?v={BG_VERSION}")


PAGES = {
    "/": "overview",
    "/index.html": "overview",
    "/history": "history",
    "/calendar": "calendar",
    "/add": "add",
    "/library": "library",
}
PAGE_BODIES = {route: _load_page(name).encode() for route, name in PAGES.items()}

CSS_FILES = {p.name: p.read_bytes() for p in CSS_DIR.glob("*.css")}
JS_FILES = {p.name: p.read_bytes() for p in JS_DIR.glob("*.js")}


class Handler(http.server.BaseHTTPRequestHandler):
    def _send_bytes(self, body, content_type, cache_control=None):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if cache_control:
            self.send_header("Cache-Control", cache_control)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj):
        self._send_bytes(json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        split = urllib.parse.urlsplit(self.path)
        path = split.path
        query = urllib.parse.parse_qs(split.query)

        if path in PAGE_BODIES:
            self._send_bytes(PAGE_BODIES[path], "text/html; charset=utf-8")
        elif path.startswith("/css/") and path[len("/css/"):] in CSS_FILES:
            self._send_bytes(CSS_FILES[path[len("/css/"):]], "text/css; charset=utf-8")
        elif path.startswith("/js/") and path[len("/js/"):] in JS_FILES:
            self._send_bytes(JS_FILES[path[len("/js/"):]], "text/javascript; charset=utf-8")
        elif path == "/bg.png":
            # the URL is content-hash-versioned (?v=...), so it's always safe
            # to cache hard -- a changed background gets a new URL entirely
            self._send_bytes(BG_IMAGE_BYTES, BG_IMAGE_CONTENT_TYPE, cache_control="public, max-age=31536000, immutable")
        elif path == "/api/calendar":
            start = query.get("start", [""])[0]
            end = query.get("end", [""])[0]
            if not start or not end:
                self.send_response(400)
                self.end_headers()
                return
            self._send_json(data.get_calendar_events(start, end))
        elif path == "/api/lookup":
            kind = query.get("type", [""])[0]
            term = query.get("term", [""])[0]
            if kind not in ("show", "movie") or not term:
                self.send_response(400)
                self.end_headers()
                return
            try:
                self._send_json(data.search_lookup(kind, term))
            except Exception as e:
                self._send_json({"error": str(e)})
        elif path == "/api/add-defaults":
            kind = query.get("type", [""])[0]
            if kind not in ("show", "movie"):
                self.send_response(400)
                self.end_headers()
                return
            try:
                self._send_json(data.get_add_defaults(kind))
            except Exception as e:
                self._send_json({"error": str(e)})
        elif path == "/api/library":
            kind = query.get("type", [""])[0]
            if kind not in ("show", "movie"):
                self.send_response(400)
                self.end_headers()
                return
            try:
                self._send_json(data.get_library(kind))
            except Exception as e:
                self._send_json({"error": str(e)})
        elif path == "/api/status":
            self._send_json({
                "next_run": data.next_run_time().isoformat(),
                "next_health_check_run": data.next_run_time(minutes=data.HEALTH_CHECK_CRON_MINUTES).isoformat(),
                "server_time": datetime.now().isoformat(),
            })
        elif path == "/api/log":
            self._send_json({"runs": data.parse_log()[:50]})
        elif path == "/api/downloads":
            self._send_json({"items": data.get_active_downloads()})
        elif path == "/api/health":
            self._send_json(data.get_server_health())
        elif path == "/api/commands":
            self._send_json(data.get_command_queue())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path

        if path == "/api/add":
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length))
                kind = body.get("type")
                if kind not in ("show", "movie"):
                    raise ValueError("type must be 'show' or 'movie'")
                result = data.add_media(kind, body)
                self._send_json({"ok": True, "title": result.get("title")})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)})
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # keep the console quiet, this runs as a background service


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    missing = config.missing_required()
    if missing:
        # flush explicitly -- stdout is fully buffered (not line-buffered)
        # once redirected to a file/journal, and serve_forever() never
        # returns to flush it on exit like a normal script would.
        print(f"WARNING: missing required environment variables: {', '.join(missing)} -- see .env.example", flush=True)

    with ThreadingHTTPServer(("0.0.0.0", PORT), Handler) as httpd:
        httpd.serve_forever()


if __name__ == "__main__":
    main()
