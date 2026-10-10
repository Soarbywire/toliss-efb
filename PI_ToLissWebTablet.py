"""XPPython3 entrypoint for ToLiss EFB.

The EFB itself is the ToLissWebTablet package next to this file. This file only loads it, and it does two jobs
so updates work:

1. Reload scripts: XPPython3 reloads this file but keeps modules it has already imported, so the package's
   modules are dropped first and the freshly installed code is the code that runs.
2. Updating from 0.66 or earlier: the old built-in updater only copies this file and index.html, so the new
   package files are missing. In that case this file finishes the update itself: it downloads the matching
   release from GitHub, checks its SHA-256 checksum, adds the package files (your settings, recordings and other
   data are not touched) and asks for one more Reload scripts. Meanwhile the EFB address shows the progress.
"""
import os
import sys

STUB_VERSION = "0.67.5"          # make_release.py checks this matches ToLissWebTablet/__init__.py
UPDATE_REPO = "Soarbywire/toliss-efb"

for _name in list(sys.modules):
    if _name == "ToLissWebTablet" or _name.startswith("ToLissWebTablet."):
        del sys.modules[_name]

try:
    _ROOT = os.path.dirname(os.path.abspath(__file__))
except NameError:              # not expected under XPPython3, but keep a sensible default
    from XPLMUtilities import XPLMGetSystemPath
    _ROOT = os.path.join(XPLMGetSystemPath(), "Resources", "plugins", "PythonPlugins")
_PKG = os.path.join(_ROOT, "ToLissWebTablet")
_REQUIRED = ("__init__.py", "plugin.py", os.path.join("core", "bridge.py"), os.path.join("web", "server.py"))


def _package_complete():
    return all(os.path.isfile(os.path.join(_PKG, r)) for r in _REQUIRED)


if _package_complete():
    from ToLissWebTablet.plugin import PythonInterface
else:
    import hashlib
    import http.server
    import io
    import json
    import queue
    import re
    import shutil
    import socketserver
    import tempfile
    import threading
    import urllib.request
    import zipfile

    from XPLMUtilities import XPLMDebugString
    from XPLMProcessing import XPLMRegisterFlightLoopCallback, XPLMUnregisterFlightLoopCallback

    _RELEASES = f"https://github.com/{UPDATE_REPO}/releases"

    class _Status:
        lock = threading.Lock()
        state = "working"       # working / done / failed
        message = "Finishing the update to ToLiss EFB " + STUB_VERSION + "..."

        @classmethod
        def set(cls, state, message):
            with cls.lock:
                cls.state, cls.message = state, message

    def _status_page():
        with _Status.lock:
            state, message = _Status.state, _Status.message
        colour = {"working": "#4fc3f7", "done": "#66bb6a", "failed": "#ef5350"}[state]
        refresh = '<meta http-equiv="refresh" content="3">' if state != "failed" else ""
        esc = lambda t: t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return (f'<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                f'{refresh}<title>ToLiss EFB</title></head><body style="background:#111;color:#eee;font-family:sans-serif;'
                f'padding:24px;max-width:640px;margin:auto"><h2>ToLiss EFB {STUB_VERSION}</h2>'
                f'<p style="color:{colour};font-size:1.1em">{esc(message)}</p></body></html>').encode("utf-8")

    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, ctype, body):
            try:
                self.send_response(code)
                self.send_header("Content-type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                pass

        def do_GET(self):
            if self.path.startswith("/api/"):
                with _Status.lock:
                    msg = _Status.message
                return self._send(503, "application/json", json.dumps({"status": "error", "message": msg}).encode("utf-8"))
            return self._send(200, "text/html; charset=utf-8", _status_page())

        do_POST = do_GET

    class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    def _get(url, limit):
        req = urllib.request.Request(url, headers={"User-Agent": f"ToLissEFB/{STUB_VERSION}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read(limit + 1)
        if len(data) > limit:
            raise ValueError("the download is larger than expected")
        return data

    class PythonInterface:
        """Finishes an update from 0.66 or earlier, then asks for Reload scripts."""

        def XPluginStart(self):
            self.Name = "ToLiss EFB"
            self.Sig = "soarbywire.tolissefb"
            self.Desc = "ToLiss EFB (finishing an update)"
            self.log_q = queue.Queue()
            self.stop = threading.Event()
            self.httpd = None
            self.flCB = self._flight_loop
            XPLMRegisterFlightLoopCallback(self.flCB, 1.0, 0)
            threading.Thread(target=self._serve, name="ToLissEFB-UpdateStatus", daemon=True).start()
            self.worker = threading.Thread(target=self._finish, name="ToLissEFB-FinishUpdate", daemon=True)
            self.worker.start()
            return self.Name, self.Sig, self.Desc

        def XPluginStop(self):
            self.stop.set()
            try:
                XPLMUnregisterFlightLoopCallback(self.flCB, 0)
            except Exception:
                pass
            if self.httpd:
                try:
                    self.httpd.shutdown()
                    self.httpd.server_close()
                except Exception:
                    pass
            self._drain()

        def XPluginEnable(self):
            return 1

        def XPluginDisable(self):
            return None

        def XPluginReceiveMessage(self, inFromWho, inMessage, inParam):
            return None

        def _log(self, text):
            self.log_q.put("ToLiss EFB: " + text + "\n")      # written by the flight loop (X-Plane's main thread)

        def _drain(self):
            while True:
                try:
                    XPLMDebugString(self.log_q.get_nowait())
                except queue.Empty:
                    return

        def _flight_loop(self, elapsedMe, elapsedSim, counter, refcon):
            self._drain()
            return 1.0

        def _serve(self):
            port = 8080
            try:
                with open(os.path.join(_PKG, "config.json"), encoding="utf-8") as f:
                    port = int(json.load(f).get("port", 8080))
            except Exception:
                pass
            for p in range(port, port + 50):
                if self.stop.is_set():
                    return
                try:
                    self.httpd = _Server(("", p), _Handler)
                except OSError:
                    continue
                self._log(f"update status page on port {p}")
                self.httpd.serve_forever(poll_interval=0.25)
                return

        def _finish(self):
            try:
                v_ = STUB_VERSION.replace(".", "_")
                base = f"{_RELEASES}/download/v{STUB_VERSION}/ToLiss_EFB_{v_}.zip"
                self._log(f"finishing the update to {STUB_VERSION}: downloading {base}")
                _Status.set("working", f"Finishing the update to ToLiss EFB {STUB_VERSION}: downloading the new files...")
                data = _get(base, 30 * 1024 * 1024)
                m = re.search(r"[0-9a-fA-F]{64}", _get(base + ".sha256", 4096).decode("utf-8", "ignore"))
                if not m:
                    raise ValueError("the release has no SHA-256 checksum")
                if hashlib.sha256(data).hexdigest().lower() != m.group(0).lower():
                    raise ValueError("the download does not match its SHA-256 checksum")
                _Status.set("working", f"Finishing the update to ToLiss EFB {STUB_VERSION}: installing...")
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    names = [n.replace("\\", "/") for n in z.namelist()]
                    roots = {n[:n.index("ToLissWebTablet/__init__.py")] for n in names if n.endswith("ToLissWebTablet/__init__.py")
                             and n[:n.index("ToLissWebTablet/__init__.py")].count("/") <= 1}
                    if len(roots) != 1:
                        raise ValueError("the release does not contain the ToLissWebTablet package")
                    prefix = roots.pop() + "ToLissWebTablet/"
                    with tempfile.TemporaryDirectory(prefix="tolissefb-finish-", dir=_ROOT) as tmp:
                        files = []
                        for info, name in zip(z.infolist(), names):
                            if not name.startswith(prefix) or name.endswith("/"):
                                continue
                            rel = name[len(prefix):]
                            parts = rel.split("/")
                            if not rel or any(p in ("", ".", "..") for p in parts) or ":" in rel:
                                raise ValueError(f"unsafe path in the release: {name}")
                            if ((info.external_attr >> 16) & 0o170000) == 0o120000:
                                raise ValueError(f"links are not allowed in the release: {name}")
                            target = os.path.join(tmp, *parts)
                            os.makedirs(os.path.dirname(target), exist_ok=True)
                            with open(target, "wb") as out:
                                out.write(z.read(info))
                            files.append(parts)
                        for parts in files:                     # every file must be valid Python before anything is copied
                            if parts[-1].endswith(".py"):
                                path = os.path.join(tmp, *parts)
                                with open(path, encoding="utf-8") as f:
                                    compile(f.read(), path, "exec")
                        # copy in: the package's own files only; settings and data in the folder stay as they are
                        for parts in sorted(files, key=lambda p: p[-1] == "__init__.py" and len(p) == 1):
                            dest = os.path.join(_PKG, *parts)
                            os.makedirs(os.path.dirname(dest), exist_ok=True)
                            shutil.copy2(os.path.join(tmp, *parts), dest + ".new")
                            os.replace(dest + ".new", dest)
                if not _package_complete():
                    raise ValueError("the package is still incomplete after copying")
                msg = (f"Update to {STUB_VERSION} finished. In X-Plane choose Plugins > XPPython3 > Reload scripts "
                       f"(or restart X-Plane), then reload this page.")
                _Status.set("done", msg)
                self._log(msg)
            except Exception as exc:
                msg = (f"Could not finish the update to {STUB_VERSION} automatically ({exc}). Download ToLiss EFB "
                       f"{STUB_VERSION} from {_RELEASES} and copy PI_ToLissWebTablet.py and the ToLissWebTablet folder "
                       f"into X-Plane 12/Resources/plugins/PythonPlugins/ (your settings are kept).")
                _Status.set("failed", msg)
                self._log(msg)
