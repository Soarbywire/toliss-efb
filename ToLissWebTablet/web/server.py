import json
import http.server
import socketserver

from .routes_get import handle_get
from .routes_post import handle_post


class TabletHandler(http.server.SimpleHTTPRequestHandler):
    plugin_ref = None

    # while no ToLiss aircraft is loaded (and the EFB is set to run only with one), only these still answer
    AWAKE_PATHS = ("/api/efb/", "/api/update", "/api/settings")

    def _asleep_reply(self):
        p = self.plugin_ref
        if not getattr(p, "asleep", False):
            return False
        path = self.path.split("?")[0]
        if not (path.startswith("/api/") or path in ("/command", "/dataref", "/load_file")) or path.startswith(self.AWAKE_PATHS):
            return False
        if self.command == "POST":
            try:
                n = int(self.headers.get("Content-Length", 0) or 0)
                if n:
                    self.rfile.read(n)
            except Exception:
                pass
        body = json.dumps({"status": "error", "asleep": True, "status_ok": False,
                           "message": "ToLiss EFB is paused: no ToLiss aircraft is loaded."}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(body)
        return True

    def do_GET(self):
        if self._asleep_reply():
            return None
        return handle_get(self, self.plugin_ref)

    def do_POST(self):
        if self._asleep_reply():
            return None
        return handle_post(self, self.plugin_ref)


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_handler(plugin):
    TabletHandler.plugin_ref = plugin
    return TabletHandler
