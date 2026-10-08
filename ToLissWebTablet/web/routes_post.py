from __future__ import annotations

import glob
import json
import math
import mmap
import os
import pickle
import queue
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import http.server
import socketserver

from ..core.utils import get_local_ip
from ..features.fdr import FT_PER_M, fdr_analyse, fdr_dir, fdr_read_csv, fdr_read_events, fdr_safe_name
from .. import EFB_VERSION
from ..features.checklists import checklist_dir, checklist_safe_name
from ..services.airports import _gm_dist_m, get_apt_search_paths, parse_ground_map, sanitize_data
from ..services.navdata import NavDatabase
from ..services.ops import CALLSIGN_RE
from ..services.vatsim import nearby_atc
from ..sim.displays import STREAM_RATE_HZ
from ..sim.mcdu import MCDU_KEYS, mcdu_text_to_keys

def handle_post(req, plugin):
    try:
        content_length = int(req.headers.get('Content-Length', 0))
        post_data = req.rfile.read(content_length) if content_length > 0 else b'{}'
        data = json.loads(post_data)

        if req.path == '/api/settings':
            new_port = data.get('port')
            if new_port:
                plugin.config["port"] = int(new_port)
                plugin.save_config(plugin.config)
                threading.Timer(0.5, plugin.restart_server_async).start()
            req.send_response(200)
            req.end_headers()
            req.wfile.write(b'{"status":"success"}')

        elif req.path == '/api/push/settings':
            # the pushback page's settings, kept in config.json so every device and every session shares them
            s = data.get("settings") if isinstance(data, dict) else None
            if s is None:
                plugin.config.pop("push_settings", None)
            else:
                lim = {"back": (0, 150), "turn": (0, 180), "final": (0, 100), "speed": (1, 5)}
                clean = {}
                for k, (lo, hi) in lim.items():
                    if k in s:
                        clean[k] = max(lo, min(hi, float(s[k])))
                if s.get("side") in ("L", "R"):
                    clean["side"] = s["side"]
                if s.get("tight") in ("gentle", "normal", "tight"):
                    clean["tight"] = s["tight"]
                if "fwd" in s:
                    clean["fwd"] = bool(s["fwd"])
                plugin.config["push_settings"] = clean
            plugin.save_config(plugin.config)
            req.send_response(200)
            req.send_header('Content-type', 'application/json')
            req.end_headers()
            req.wfile.write(json.dumps({"status": "success", "settings": plugin.config.get("push_settings")}).encode('utf-8'))

        elif req.path in ('/api/replay/cmd', '/api/traffic/add', '/api/traffic/remove', '/api/traffic/clear'):
            item = dict(data) if isinstance(data, dict) else {}
            if req.path == '/api/replay/cmd':
                op = "replay"
            else:
                op, item["kind"] = "traffic", req.path.rsplit('/', 1)[1]
            try:
                res = plugin.bridge.call(op, item, timeout=3.0)
                result = {**(res.get("state") or {}), "status": "success" if res.get("ok") else "error", "message": res.get("message", "")}
            except Exception as exc:
                result = {"status": "error", "message": "No response from the sim" if isinstance(exc, TimeoutError) else f"Could not do that: {exc}"}
            req.send_response(200)
            req.send_header('Content-type', 'application/json')
            req.end_headers()
            req.wfile.write(json.dumps(result).encode('utf-8'))

        elif req.path == '/command':
            if data.get('command'):
                plugin.bridge.submit("command", data.get('command'))
            req.send_response(200)
            req.end_headers()
            req.wfile.write(b'{"status":"success"}')

        elif req.path == '/dataref':
            dref = data.get('dataref')
            val = data.get('value')
            if dref and val is not None:
                plugin.bridge.submit("write_dataref", (dref, float(val)))
            req.send_response(200)
            req.end_headers()
            req.wfile.write(b'{"status":"success"}')

        elif req.path == '/api/toliss/payload/apply':
            payload = {
                "pax": data.get("pax"),
                "cargo_fwd": data.get("cargo_fwd"),
                "cargo_aft": data.get("cargo_aft"),
                "fuel": data.get("fuel")
            }
            try:
                result = plugin.bridge.call("payload_apply", payload, timeout=2.0)
                req.send_response(200 if result.get("status") == "success" else 400)
                req.send_header('Content-type', 'application/json')
                req.end_headers()
                req.wfile.write(json.dumps(result).encode('utf-8'))
            except Exception:
                req.send_response(504)
                req.send_header('Content-type', 'application/json')
                req.end_headers()
                req.wfile.write(json.dumps({"status": "error", "message": "Timed out waiting for X-Plane."}).encode('utf-8'))

        elif req.path == '/api/slew/input':
            # movement while a pad button is held: stored here, used by the flight loop every frame
            sl = getattr(plugin, 'slew', None)
            if sl:
                sl["inp"] = {k: max(-1.0, min(1.0, float(data.get(k, 0) or 0))) for k in ("fwd", "side", "up", "turn")}
                if data.get("rate"):
                    sl["rate"] = max(0.2, min(5000.0, float(data["rate"])))
                sl["last_input"] = time.time()
            req.send_response(200)
            req.send_header('Content-type', 'application/json')
            req.end_headers()
            req.wfile.write(json.dumps({"status": "success" if sl else "error", "slew": bool(sl)}).encode('utf-8'))
            return

        elif req.path in ('/api/instructor/set', '/api/instructor/freeze', '/api/instructor/rate', '/api/slew/on', '/api/slew/off', '/api/slew/place', '/api/slew/state', '/api/instructor/wx',
                          '/api/push/start', '/api/push/pause', '/api/push/resume', '/api/push/stop', '/api/push/state', '/api/push/plan'):
            kind = req.path.rsplit('/', 1)[1]
            if req.path.startswith('/api/slew/'):
                kind = "slew_" + kind
            elif req.path.startswith('/api/push/'):
                kind = "push_" + kind
            item = dict(data) if isinstance(data, dict) else {}
            item["kind"] = kind
            try:
                res = plugin.bridge.call("instructor", item, timeout=6.0)
                result = {"status": "success" if res.get("ok") else "error", "message": res.get("message", ""), **(res.get("state") or {}),
                          **{k: res[k] for k in ("cg", "tail", "geom", "hdg0", "length_m") if k in res}}
            except Exception:
                result = {"status": "error", "message": "No response from the sim"}
            req.send_response(200)
            req.send_header('Content-type', 'application/json')
            req.end_headers()
            req.wfile.write(json.dumps(result).encode('utf-8'))
            return

        elif req.path in ('/api/ops/config', '/api/ops/send', '/api/ops/poll', '/api/ops/clear', '/api/ops/auto', '/api/ops/action', '/api/ops/stand'):
            ops = getattr(plugin, 'ops', None)
            result = {"status": "error", "message": "Ops Centre not available"}
            if ops and req.path == '/api/ops/config':
                problems = []
                if "logon" in data and str(data["logon"]).strip():
                    ops.logon = str(data["logon"]).strip()
                for key, attr, label in (("ops_callsign", "ops_callsign", "Ops callsign"), ("aircraft", "aircraft", "Aircraft callsign")):
                    if key in data:
                        v = re.sub(r"\s", "", str(data[key] or "")).upper()
                        if v and not CALLSIGN_RE.match(v):
                            problems.append(f"{label} must be 2-8 letters or digits.")
                        else:
                            setattr(ops, attr, v)
                if ops.ops_callsign and ops.ops_callsign == ops.aircraft:
                    problems.append("The ops callsign must differ from the aircraft callsign.")
                    ops.enabled = False
                if "enabled" in data:
                    ops.enabled = bool(data["enabled"]) and not problems
                    ops.next_poll = 0
                ops.save_config()
                result = {"status": "error" if problems else "success", "message": " ".join(problems), **ops.status()}
            elif ops and req.path == '/api/ops/send':
                ok, msg = ops.send(data.get("text", ""), data.get("to"), bool(data.get("as_crew")))
                result = {"status": "success" if ok else "error", "message": msg}
            elif ops and req.path == '/api/ops/poll':
                if time.time() - ops.last_poll < 20:
                    result = {"status": "success", "message": "checked less than 20 seconds ago", **ops.status()}
                else:
                    ops.poll()
                    ops.next_poll = time.time() + 60
                    result = {"status": "success", **ops.status()}
            elif ops and req.path == '/api/ops/auto':
                key = data.get("key")
                if key in ops.auto:
                    ops.auto[key] = bool(data.get("on"))
                    ops.save_config()
                result = {"status": "success", **ops.flight_status()}
            elif ops and req.path == '/api/ops/action':
                ok, msg = ops.action(str(data.get("what", "")))
                result = {"status": "success" if ok else "error", "message": msg, **ops.flight_status()}
            elif ops and req.path == '/api/ops/stand':
                if not ops.ofp:
                    result = {"status": "error", "message": "Load a SimBrief flight plan first."}
                else:
                    ok, msg = ops.change_stand(str(data.get("mode", "auto")), data.get("name"),
                                               bool(data.get("exclude_old")), bool(data.get("remember")))
                    result = {"status": "success" if ok else "error", "message": msg, **ops.flight_status()}
            elif ops and req.path == '/api/ops/clear':
                with ops.lock:
                    ops.log = []
                    ops.unread = 0
                ops._save_log()
                result = {"status": "success"}
            req.send_response(200)
            req.send_header('Content-type', 'application/json')
            req.end_headers()
            req.wfile.write(json.dumps(result).encode('utf-8'))
            return

        elif req.path in ('/api/update/install', '/api/update/rollback', '/api/update/settings'):
            up = getattr(plugin, 'updater', None)
            result = {"status": "error", "message": "Updater not available"}
            if up:
                if req.path == '/api/update/settings':
                    up.enabled = bool(data.get("enabled", True))
                    try:
                        plugin.config["update_check"] = up.enabled
                        plugin.save_config(plugin.config)
                    except Exception:
                        pass
                    result = {"status": "success", **up.status()}
                else:
                    # only update or restore while parked: read ground contact and speed on the sim thread
                    try:
                        v = plugin.bridge.call(
                            "read_datarefs",
                            ["sim/flightmodel/failures/onground_any", "sim/flightmodel/position/groundspeed"],
                            timeout=1.5,
                        )
                    except Exception:
                        v = {}
                    parked = float(v.get("sim/flightmodel/failures/onground_any", 1) or 0) >= 1 and float(v.get("sim/flightmodel/position/groundspeed", 0) or 0) < 1.0
                    if not parked:
                        result = {"status": "error", "message": "Updates can only be installed while the aircraft is parked."}
                    else:
                        target = up.install if req.path == '/api/update/install' else up.rollback
                        threading.Thread(target=target, daemon=True).start()
                        result = {"status": "success", "message": "started"}
            req.send_response(200)
            req.send_header('Content-type', 'application/json')
            req.end_headers()
            req.wfile.write(json.dumps(result).encode('utf-8'))
            return

        elif req.path == '/api/fdr/control':
            fdr = getattr(plugin, 'fdr', None)
            if fdr:
                if "enabled" in data:
                    fdr.enabled = bool(data["enabled"])
                if data.get("mode") in ("auto", "manual"):
                    fdr.mode = data["mode"]
                if data.get("rate"):
                    try: fdr.rate = max(1.0, min(10.0, float(data["rate"])))
                    except Exception: pass
                fdr.save_settings()
                action = data.get("action")
                if action in ("start", "stop"):
                    # carried out on X-Plane's main thread by the flight loop (see FlightRecorder.request)
                    fdr.request(action, "manual")
                    for _ in range(20):
                        time.sleep(0.05)
                        if getattr(fdr, 'pending', None) is None:
                            break
            req.send_response(200)
            req.send_header('Content-type', 'application/json')
            req.end_headers()
            req.wfile.write(json.dumps({"status": "success", **(fdr.status() if fdr else {})}).encode('utf-8'))
            return

        elif req.path == '/api/fdr/delete':
            name = fdr_safe_name(data.get("name", ""))
            fdr = getattr(plugin, 'fdr', None)
            ok = False
            if name and not (fdr and fdr.recording and fdr.name == name):
                d = fdr_dir(plugin.plugin_dir)
                for ext in (".csv", ".events.csv", ".json"):
                    try:
                        os.remove(os.path.join(d, name + ext)); ok = True
                    except Exception:
                        pass
            req.send_response(200)
            req.send_header('Content-type', 'application/json')
            req.end_headers()
            req.wfile.write(json.dumps({"status": "success" if ok else "error"}).encode('utf-8'))
            return

        elif req.path == '/api/checklist_upload':
            name = checklist_safe_name(data.get("name", ""))
            xml = data.get("xml", "")
            ok, msg = False, ""
            if not name:
                msg = "Please give the checklist a name."
            elif not isinstance(xml, str) or not xml.strip():
                msg = "The file is empty."
            elif len(xml.encode('utf-8')) > 2 * 1024 * 1024:
                msg = "The file is larger than 2 MB."
            else:
                try:
                    path = os.path.join(checklist_dir(plugin.plugin_dir), name + ".xml")
                    with open(path, 'w', encoding='utf-8') as f:
                        f.write(xml)
                    ok, msg = True, name
                except Exception as e:
                    msg = f"Could not save the file: {e}"
            req.send_response(200)
            req.send_header('Content-type', 'application/json')
            req.end_headers()
            req.wfile.write(json.dumps({"status": "success" if ok else "error", "message": msg}).encode('utf-8'))
            return

        elif req.path == '/api/checklist_delete':
            name = checklist_safe_name(data.get("name", ""))
            path = os.path.join(checklist_dir(plugin.plugin_dir), name + ".xml")
            ok = False
            try:
                if name and os.path.isfile(path):
                    os.remove(path)
                    ok = True
            except Exception:
                pass
            req.send_response(200)
            req.send_header('Content-type', 'application/json')
            req.end_headers()
            req.wfile.write(json.dumps({"status": "success" if ok else "error"}).encode('utf-8'))
            return

        elif req.path == '/api/mcdu_key':
            side = 2 if str(data.get("side", 1)) == "2" else 1
            keys = []
            if data.get("key"):
                keys = [str(data["key"])]
            elif data.get("text") is not None:
                keys = mcdu_text_to_keys(data["text"])
            accepted = [k for k in keys if k in MCDU_KEYS][:60]
            for k in accepted:
                plugin.bridge.submit("mcdu_key", (side, k))
            req.send_response(200)
            req.send_header('Content-type', 'application/json')
            req.end_headers()
            req.wfile.write(json.dumps({"status": "success", "queued": len(accepted)}).encode('utf-8'))
            return

        elif req.path == '/api/teleport':
            if data.get('lat') and data.get('lon'):
                plugin.bridge.submit("teleport", data)
            req.send_response(200)
            req.end_headers()
            req.wfile.write(b'{"status":"success"}')

        elif req.path == '/api/toliss/situations/load':
            payload = {
                "action": "load",
                "index": data.get('index'),
                "mode": data.get('mode', 'full')
            }
            try:
                result = plugin.bridge.call("situation", payload, timeout=3.0)
                req.send_response(200 if result.get("status") == "success" else 400)
                req.send_header('Content-type', 'application/json')
                req.end_headers()
                req.wfile.write(json.dumps(result).encode('utf-8'))
            except Exception:
                req.send_response(504)
                req.send_header('Content-type', 'application/json')
                req.end_headers()
                req.wfile.write(json.dumps({"status": "error", "message": "Timed out waiting for X-Plane."}).encode('utf-8'))

        elif req.path == '/api/toliss/situations/save':
            payload = {
                "action": "save",
                "name": data.get('name', '')
            }
            try:
                result = plugin.bridge.call("situation", payload, timeout=3.0)
                req.send_response(200 if result.get("status") == "success" else 400)
                req.send_header('Content-type', 'application/json')
                req.end_headers()
                req.wfile.write(json.dumps(result).encode('utf-8'))
            except Exception:
                req.send_response(504)
                req.send_header('Content-type', 'application/json')
                req.end_headers()
                req.wfile.write(json.dumps({"status": "error", "message": "Timed out waiting for X-Plane."}).encode('utf-8'))

        elif req.path == '/api/toliss/failures':
            payload = {
                "action": data.get('action'),
                "fault_index": data.get('fault_index'),
                "condition": data.get('condition'),
                "phase": data.get('phase'),
                "parameter": data.get('parameter'),
                "slot": data.get('slot')
            }
            try:
                result = plugin.bridge.call("failure", payload, timeout=5.0)
                req.send_response(200 if result.get("status") == "success" else 400)
                req.send_header('Content-type', 'application/json')
                req.end_headers()
                req.wfile.write(json.dumps(result).encode('utf-8'))
            except Exception:
                req.send_response(504)
                req.send_header('Content-type', 'application/json')
                req.end_headers()
                req.wfile.write(json.dumps({"status": "error", "message": "Timed out waiting for X-Plane."}).encode('utf-8'))

        elif req.path == '/load_file':
            filename = data.get('filename', '').strip()
            if filename:
                plugin.bridge.submit("load_file", filename)
            req.send_response(200)
            req.end_headers()
            req.wfile.write(b'{"status":"success"}')

        else:
            req.send_response(404)
            req.end_headers()
    except Exception as e:
        plugin.log.xplane(f"ToLiss EFB POST Error: {e}")
        req.send_response(500)
        req.end_headers()
