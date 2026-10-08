import http.server
import socketserver

from .routes_get import handle_get
from .routes_post import handle_post


class TabletHandler(http.server.SimpleHTTPRequestHandler):
    plugin_ref = None

    def do_GET(self):
        return handle_get(self, self.plugin_ref)

    def do_POST(self):
        return handle_post(self, self.plugin_ref)


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_handler(plugin):
    TabletHandler.plugin_ref = plugin
    return TabletHandler
