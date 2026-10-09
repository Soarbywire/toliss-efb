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

def handle_get(req, plugin):
    try:
        if req.path == '/' or req.path == '/index.html':
            filepath = os.path.join(plugin.plugin_dir, 'index.html')
            if os.path.exists(filepath):
                with open(filepath, 'rb') as f:
                    content = f.read()
                req.send_response(200)
                req.send_header('Content-type', 'text/html; charset=utf-8')
                # Never cache the EFB page, so an updated index.html is always picked up
                req.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
                req.send_header('Pragma', 'no-cache')
                req.send_header('Expires', '0')
                req.end_headers()
                req.wfile.write(content)
            else:
                req.send_response(404)
                req.end_headers()
                req.wfile.write(b"index.html not found in PythonPlugins/ToLissWebTablet/")

        elif req.path == '/api/toliss/situations':
            try:
                result = plugin.bridge.call("situation", {"action": "list"}, timeout=3.0)
                req.send_response(200 if result.get("status") == "success" else 503)
                req.send_header('Content-type', 'application/json')
                req.end_headers()
                req.wfile.write(json.dumps(result).encode('utf-8'))
            except Exception:
                req.send_response(504)
                req.send_header('Content-type', 'application/json')
                req.end_headers()
                req.wfile.write(json.dumps({"status": "error", "supported": False, "message": "Timed out waiting for X-Plane."}).encode('utf-8'))

        elif req.path == '/api/toliss/failures':
            try:
                result = plugin.bridge.call("failure", {"action": "list"}, timeout=5.0)
                req.send_response(200 if result.get("status") == "success" else 503)
                req.send_header('Content-type', 'application/json')
                req.end_headers()
                req.wfile.write(json.dumps(result).encode('utf-8'))
            except Exception:
                req.send_response(504)
                req.send_header('Content-type', 'application/json')
                req.end_headers()
                req.wfile.write(json.dumps({"status": "error", "supported": False, "message": "Timed out waiting for X-Plane."}).encode('utf-8'))

        elif req.path == '/api/settings':
            nav_cycle = "AIRAC Unknown"
            custom_cycle = os.path.join(plugin.xp_path, "Custom Data", "cycle.json")
            default_cycle = os.path.join(plugin.xp_path, "Resources", "default data", "cycle.json")

            cycle_path = custom_cycle if os.path.exists(custom_cycle) else default_cycle
            if os.path.exists(cycle_path):
                try:
                    with open(cycle_path, 'r') as cf:
                        cdata = json.load(cf)
                        nav_cycle = f"AIRAC {cdata.get('cycle', '')}"
                except Exception: pass
            elif os.path.exists(os.path.join(plugin.xp_path, "Custom Data", "cycle_info.txt")):
                try:
                    with open(os.path.join(plugin.xp_path, "Custom Data", "cycle_info.txt"), 'r') as cf:
                        lines = cf.readlines()
                        if len(lines) > 0:
                            nav_cycle = lines[0].strip()
                except Exception: pass

            req.send_response(200)
            req.send_header('Content-type', 'application/json')
            req.end_headers()
            req.wfile.write(json.dumps({
                "ip": get_local_ip(), 
                "port": plugin.config.get("port", 8080),
                "navdata": nav_cycle
            }).encode())

        elif req.path.startswith('/api/metar'):
            parsed = urllib.parse.urlparse(req.path)
            qs = urllib.parse.parse_qs(parsed.query)
            icao = qs.get("icao", [""])[0]
            if icao:
                try:
                    s_req = urllib.request.Request(f"https://metar.vatsim.net/{icao}", headers={'User-Agent': 'ToLissEFB/1.0'})
                    with urllib.request.urlopen(s_req, timeout=5) as response:
                        metar_txt = response.read().decode('utf-8').strip()
                        req.send_response(200)
                        req.send_header('Content-type', 'application/json')
                        req.end_headers()
                        req.wfile.write(json.dumps({"status": "success", "metar": metar_txt}).encode('utf-8'))
                        return
                except Exception: pass
            req.send_response(500)
            req.end_headers()
            req.wfile.write(b'{"status":"error"}')

        elif req.path.startswith('/api/search_apt'):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(req.path).query)
            q = qs.get("q", [""])[0].upper()
            res = []
            ready = getattr(plugin, 'db_ready', False)
            if q and ready:
                exact, starts, contains = [], [], []
                for apt in plugin.apt_lite_db:
                    ic = apt["icao"]
                    if ic == q:
                        exact.append(apt)
                    elif ic.startswith(q):
                        if len(starts) < 20: starts.append(apt)
                    elif q in ic or q in apt["name"].upper():
                        if len(contains) < 20: contains.append(apt)
                res = (exact + starts + contains)[:20]

            req.send_response(200)
            req.send_header('Content-type', 'application/json')
            req.end_headers()
            req.wfile.write(json.dumps({"status": "success" if ready else "loading", "data": res}).encode('utf-8'))
            return

        elif req.path.startswith('/api/ground_map'):
            parsed = urllib.parse.urlparse(req.path)
            qs = urllib.parse.parse_qs(parsed.query)
            body = None
            if not getattr(plugin, 'db_ready', False):
                body = b'{"status":"loading"}'
            else:
                icao = qs.get("icao", [""])[0].upper()
                if not icao:
                    try:
                        lat = float(qs.get("lat", ["0"])[0]); lon = float(qs.get("lon", ["0"])[0])
                        icao = plugin.find_nearest_airport(lat, lon) or ""
                    except Exception:
                        icao = ""
                if icao:
                    try:
                        body = plugin.get_ground_map(icao)
                    except Exception as e:
                        plugin.log.xplane(f"ToLiss EFB: ground map error for {icao}: {e}")
                        body = None
                if body is None:
                    body = b'{"status":"none"}'
            req.send_response(200)
            req.send_header('Content-type', 'application/json')
            req.end_headers()
            req.wfile.write(body)

        elif req.path.startswith('/api/get_gates'):
            parsed = urllib.parse.urlparse(req.path)
            qs = urllib.parse.parse_qs(parsed.query)
            icao = qs.get("icao", [""])[0].upper()

            if icao:
                gates = []
                runways_draw = []
                elev_ft = 0.0
                apt_name = icao
                seen_names = set()

                known = plugin.apt_index.get(icao) if getattr(plugin, 'db_ready', False) else None
                if known:
                    search_paths = [known[0]]
                else:
                    search_paths = get_apt_search_paths(plugin.xp_path)
                found = False

                def calc_hdg(lat1, lon1, lat2, lon2):
                    try:
                        l1, ln1, l2, ln2 = map(math.radians, [lat1, lon1, lat2, lon2])
                        dlon = ln2 - ln1
                        x = math.sin(dlon) * math.cos(l2)
                        y = math.cos(l1) * math.sin(l2) - (math.sin(l1) * math.cos(l2) * math.cos(dlon))
                        initial_bearing = math.atan2(x, y)
                        return (math.degrees(initial_bearing) + 360) % 360
                    except Exception: return 0.0

                icao_b = icao.encode('utf-8')
                pattern = re.compile(br'^1\s+[-.\d]+\s+\d+\s+\d+\s+' + icao_b + br'\s', re.MULTILINE)

                for p in search_paths:
                    try:
                        with open(p, 'rb') as f:
                            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
                            start_pos = known[1] if (known and p == known[0]) else None
                            if start_pos is None:
                                match = pattern.search(mm)
                                start_pos = match.start() if match else None
                            if start_pos is not None:
                                found = True
                                mm.seek(start_pos)

                                header = mm.readline().decode('utf-8', errors='ignore')
                                try: 
                                    parts = header.split(maxsplit=5)
                                    elev_ft = float(parts[1])
                                    if len(parts) > 5:
                                        apt_name = parts[5].strip()
                                except Exception: pass

                                for raw_line in iter(mm.readline, b""):
                                    s_line = raw_line.lstrip() 
                                    if not s_line: continue

                                    if s_line.startswith(b'1 ') or s_line.startswith(b'16 ') or s_line.startswith(b'17 ') or s_line.startswith(b'99'):
                                        break

                                    if not (s_line.startswith(b'100 ') or s_line.startswith(b'1300 ') or 
                                            s_line.startswith(b'1301 ') or s_line.startswith(b'1302 ') or s_line.startswith(b'15 ')):
                                        continue

                                    try:
                                        line = s_line.decode('utf-8', errors='ignore')
                                        parts = line.split()
                                        code = parts[0]

                                        if code == '100' and len(parts) >= 20:
                                            name1 = parts[8]
                                            lat1 = float(parts[9])
                                            lon1 = float(parts[10])
                                            name2 = parts[17]
                                            lat2 = float(parts[18])
                                            lon2 = float(parts[19])

                                            runways_draw.append({"lat1": lat1, "lon1": lon1, "lat2": lat2, "lon2": lon2})

                                            if name1 != "xxx" and name1 not in seen_names:
                                                hdg1 = calc_hdg(lat1, lon1, lat2, lon2)
                                                gates.append({"type": "RWY", "name": name1, "lat": lat1, "lon": lon1, "hdg": hdg1})
                                                seen_names.add(name1)

                                            if name2 != "xxx" and name2 not in seen_names:
                                                hdg2 = calc_hdg(lat2, lon2, lat1, lon1)
                                                gates.append({"type": "RWY", "name": name2, "lat": lat2, "lon": lon2, "hdg": hdg2})
                                                seen_names.add(name2)

                                        elif code in ('1300', '1301', '1302') and len(parts) >= 4:
                                            lat = float(parts[1])
                                            lon = float(parts[2])
                                            hdg = float(parts[3])
                                            name = " ".join(parts[6:]) if len(parts) >= 7 else f"Stand {parts[4]}"
                                            if name not in seen_names:
                                                gates.append({"type": "STAND", "name": name, "lat": lat, "lon": lon, "hdg": hdg})
                                                seen_names.add(name)

                                        elif code == '15' and len(parts) >= 4:
                                            lat = float(parts[1])
                                            lon = float(parts[2])
                                            hdg = float(parts[3])
                                            name = " ".join(parts[4:]) if len(parts) >= 5 else "Ramp"
                                            if name not in seen_names:
                                                gates.append({"type": "STAND", "name": name, "lat": lat, "lon": lon, "hdg": hdg})
                                                seen_names.add(name)
                                    except Exception: pass
                            mm.close()
                    except Exception: pass
                    if found:
                        break

                req.send_response(200)
                req.send_header('Content-type', 'application/json')
                req.end_headers()
                req.wfile.write(json.dumps({
                    "status": "success", 
                    "gates": gates, 
                    "runways": runways_draw,
                    "elev_ft": elev_ft,
                    "name": apt_name
                }).encode())
                return

            req.send_response(400)
            req.end_headers()

        elif req.path.startswith('/api/simbrief'):
            parsed = urllib.parse.urlparse(req.path)
            qs = urllib.parse.parse_qs(parsed.query)
            user = qs.get("user", [""])[0]
            if user:
                param = "userid" if user.isdigit() else "username"
                try:
                    s_req = urllib.request.Request(f"https://www.simbrief.com/api/xml.fetcher.php?{param}={user}&json=1", headers={'User-Agent': 'ToLissEFB/1.0'})
                    with urllib.request.urlopen(s_req, timeout=10) as response:
                        data = response.read()
                        try:
                            ops = getattr(plugin, 'ops', None)
                            if ops:
                                ops.set_ofp(json.loads(data.decode('utf-8')), user)
                        except Exception as e:
                            plugin.log.xplane(f"ToLiss EFB: ops could not use the SimBrief plan: {e}")
                        req.send_response(200)
                        req.send_header('Content-type', 'application/json')
                        req.end_headers()
                        req.wfile.write(data)
                        return
                except urllib.error.HTTPError as e:
                    req.send_response(e.code)
                    req.end_headers()
                    req.wfile.write(b'{"status":"error"}')
                    return
                except Exception: pass
            req.send_response(500)
            req.end_headers()
            req.wfile.write(b'{"status":"error"}')

        elif req.path == '/api/vatsim':
            lat = 0.0
            lon = 0.0
            try:
                res = plugin.bridge.call(
                    "read_datarefs",
                    ["sim/flightmodel/position/latitude", "sim/flightmodel/position/longitude"],
                    timeout=1.0,
                )
                lat = res.get("sim/flightmodel/position/latitude", 0.0)
                lon = res.get("sim/flightmodel/position/longitude", 0.0)
            except Exception: pass

            vatsim_snapshot = plugin.vatsim.cache.snapshot()
            vatsim = vatsim_snapshot.get("data")
            if not vatsim:
                req.send_response(200)
                req.send_header('Content-type', 'application/json')
                req.end_headers()
                req.wfile.write(json.dumps({"status": "loading"}).encode('utf-8'))
                return

            filtered = {"pilots": [], "controllers": [], "atis": [], "stats": {}}

            p_count = len(vatsim.get("pilots", []))
            a_count = 0
            o_count = 0
            for c in vatsim.get("controllers", []):
                if c.get("facility") == 1:
                    o_count += 1
                else:
                    a_count += 1

            filtered["stats"] = {"pilots": p_count, "atc": a_count, "obs": o_count}

            def calc_dist(lat1, lon1, lat2, lon2):
                if lat1 == 0.0 or lon1 == 0.0 or lat2 == 0.0 or lon2 == 0.0: return 9999
                R = 3440.065 
                dlat = math.radians(lat2 - lat1)
                dlon = math.radians(lon2 - lon1)
                a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
                c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
                return R * c

            for p in vatsim.get("pilots", []):
                plat = p.get("latitude", 0)
                plon = p.get("longitude", 0)
                p["distance"] = round(calc_dist(lat, lon, plat, plon), 1)
                filtered["pilots"].append(p)

            for c in vatsim.get("controllers", []):
                if c.get("facility") != 1:
                    filtered["controllers"].append(c)

            for a in vatsim.get("atis", []):
                vp = a.get("visual_position")
                if isinstance(vp, dict) and "lat" in vp and "lon" in vp:
                    alat = vp.get("lat")
                    alon = vp.get("lon")
                    a["latitude"] = alat
                    a["longitude"] = alon
                    a["distance"] = round(calc_dist(lat, lon, alat, alon), 1)
                else:
                    a["distance"] = 9999
                filtered["atis"].append(a)

            req.send_response(200)
            req.send_header('Content-type', 'application/json')
            req.end_headers()
            req.wfile.write(json.dumps({"status": "success", "data": filtered}).encode('utf-8'))

        elif req.path.startswith('/api/procedures'):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(req.path).query)
            icao = qs.get("icao", [""])[0].upper()
            try:
                if getattr(plugin, 'navdb', None) is None:
                    plugin.navdb = NavDatabase(plugin.xp_path, getattr(plugin, 'plugin_dir', None))
                body = plugin.navdb.procedures(icao) if icao else None
                if body is None:
                    body = {"status": "none", "message": f"No approach procedures found for {icao} in X-Plane's navigation data."}
            except Exception as e:
                plugin.log.xplane(f"ToLiss EFB: procedures error for {icao}: {e}")
                body = {"status": "error", "message": str(e)}
            req.send_response(200)
            req.send_header('Content-type', 'application/json')
            req.end_headers()
            req.wfile.write(json.dumps(body, separators=(',', ':')).encode('utf-8'))

        elif req.path.startswith('/api/read'):
            # Read a handful of datarefs (used to check automated cockpit actions)
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(req.path).query)
            names = [n for n in qs.get("d", [""])[0].split(",") if n][:30]
            try:
                vals = plugin.bridge.call("read_datarefs", names, timeout=1.5)
                for name in vals.pop("__missing__", []):
                    vals.pop(name, None)
                body = {"status": "success", "data": vals}
            except Exception:
                body = {"status": "error", "message": "No response from the sim"}
            req.send_response(200)
            req.send_header('Content-type', 'application/json')
            req.end_headers()
            req.wfile.write(json.dumps(sanitize_data(body)).encode('utf-8'))

        elif req.path.startswith('/api/mcdu'):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(req.path).query)
            side = 2 if qs.get("side", ["1"])[0] == "2" else 1
            try:
                body = plugin.bridge.call("mcdu_screen", side, timeout=1.5)
            except Exception:
                body = {"status": "error", "message": "No response from the sim"}
            req.send_response(200)
            req.send_header('Content-type', 'application/json')
            req.end_headers()
            req.wfile.write(json.dumps(body).encode('utf-8'))

        elif req.path == '/api/xpilot':
            drefs = ["xpilot/version", "xpilot/login/status", "xpilot/login/callsign",
                     "xpilot/com1_station_callsign", "xpilot/com2_station_callsign",
                     "xpilot/audio/com1_rx", "xpilot/audio/com2_rx",
                     "xpilot/audio/split_audio_channels",
                     "xpilot/selcal", "xpilot/selcal_received", "xpilot/num_aircraft",
                     "sim/cockpit2/radios/actuators/com1_frequency_hz_833",
                     "sim/cockpit2/radios/actuators/com1_standby_frequency_hz_833",
                     "sim/cockpit2/radios/actuators/com2_frequency_hz_833",
                     "sim/cockpit2/radios/actuators/com2_standby_frequency_hz_833",
                     "sim/flightmodel/position/latitude", "sim/flightmodel/position/longitude",
                     "sim/flightmodel/position/y_agl"]
            try:
                vals = plugin.bridge.call("read_datarefs", drefs, timeout=1.5)
            except Exception:
                vals = {}
            lat = float(vals.get("sim/flightmodel/position/latitude", 0) or 0)
            lon = float(vals.get("sim/flightmodel/position/longitude", 0) or 0)
            agl_ft = float(vals.get("sim/flightmodel/position/y_agl", 0) or 0) * 3.28084
            vatsim_snapshot = plugin.vatsim.cache.snapshot()
            nearby = nearby_atc(vatsim_snapshot, lat, lon, agl_ft) if (lat or lon) else []
            body = {"status": "success", "xpilot": vals, "nearby": nearby,
                    "vatsim_age_s": round(time.time() - vatsim_snapshot.get("last_fetch", 0)) if vatsim_snapshot.get("last_fetch") else None}
            req.send_response(200)
            req.send_header('Content-type', 'application/json')
            req.end_headers()
            req.wfile.write(json.dumps(sanitize_data(body)).encode('utf-8'))

        elif req.path == '/api/get_states':
            drefs = [
                "AirbusFBW/EnableExternalPower",
                "AirbusFBW/Chocks",
                "AirbusFBW/GroundHPAir",
                "AirbusFBW/GroundLPAir",
                "AirbusFBW/PaxDoorModeArray[0]",
                "AirbusFBW/PaxDoorModeArray[1]",
                "AirbusFBW/PaxDoorModeArray[2]",
                "AirbusFBW/PaxDoorModeArray[3]",
                "AirbusFBW/PaxDoorModeArray[4]",
                "AirbusFBW/PaxDoorModeArray[5]",
                "AirbusFBW/PaxDoorModeArray[6]",
                "AirbusFBW/PaxDoorModeArray[7]",
                "AirbusFBW/CargoDoorModeArray[0]",
                "AirbusFBW/CargoDoorModeArray[1]",
                "AirbusFBW/CargoDoorModeArray[2]",
                "sim/weather/region/change_mode",
                "sim/time/use_system_time",
                "sim/time/paused",
                "sim/cockpit2/gauges/indicators/wind_speed_kts",
                "sim/cockpit2/gauges/indicators/wind_heading_deg_mag",
                "sim/weather/region/sealevel_pressure_pas", 
                "sim/weather/aircraft/temperature_ambient_deg_c",
                "sim/weather/region/visibility_reported_sm",
                "sim/time/zulu_time_sec",
                "sim/aircraft/view/acf_ICAO"
            ]
            try:
                results = plugin.bridge.call("read_datarefs", drefs, timeout=1.0)
                clean_results = sanitize_data(results)
                req.send_response(200)
                req.send_header('Content-type', 'application/json')
                req.end_headers()
                req.wfile.write(json.dumps({"status": "success", "states": clean_results}).encode('utf-8', errors='ignore'))
            except Exception:
                req.send_response(500)
                req.end_headers()

        elif req.path == '/api/get_flight_data':
            drefs = [
                "sim/cockpit2/gauges/indicators/airspeed_kts_pilot",
                "sim/flightmodel/position/elevation",
                "sim/cockpit2/gauges/indicators/heading_vacuum_deg_mag_pilot",
                "sim/cockpit2/radios/actuators/com1_frequency_hz_833",
                "sim/cockpit2/radios/actuators/com1_standby_frequency_hz_833",
                "sim/weather/region/sealevel_pressure_pas",
                "sim/cockpit2/gauges/indicators/wind_speed_kts",
                "sim/cockpit2/gauges/indicators/wind_heading_deg_mag",
                "sim/flightmodel/position/latitude",
                "sim/flightmodel/position/longitude",
                "sim/weather/aircraft/temperature_ambient_deg_c",
                "sim/flightmodel/misc/machno",
                "sim/flightmodel/position/vh_ind_fpm",
                "AirbusFBW/AP1Engage",
                "AirbusFBW/AP2Engage",
                "AirbusFBW/ATHRmode",
                "sim/flightmodel/engine/ENGN_N1_[0]",
                "sim/flightmodel/engine/ENGN_N1_[1]",
                "sim/flightmodel/position/psi",
                "sim/flightmodel/failures/onground_any",
                "sim/flightmodel/position/groundspeed",
                "sim/flightmodel/weight/m_total",
                "sim/cockpit2/controls/parking_brake_ratio",
                "toliss_web/current_fuel_kg",
                "sim/cockpit2/gauges/indicators/radio_altimeter_height_ft_pilot",
                "sim/cockpit2/controls/flap_ratio",
                "sim/flightmodel2/controls/flap1_deploy_ratio",
                "sim/cockpit2/controls/speedbrake_ratio",
                "sim/cockpit2/controls/gear_handle_down",
                "sim/flightmodel2/gear/deploy_ratio",
                "sim/cockpit/radios/transponder_code",
                "sim/cockpit2/radios/actuators/transponder_mode",
                "sim/cockpit2/engine/indicators/fuel_flow_kg_sec",
                "sim/weather/aircraft/temperature_leadingedge_deg_c",
                "sim/cockpit2/switches/beacon_on",
                "sim/cockpit2/switches/strobe_lights_on",
                "sim/cockpit/switches/fasten_seat_belts",
                "AirbusFBW/SeatBeltSignsOn",
                "toliss_web/local_traffic",
                "sim/time/is_in_replay"
            ]
            try:
                results = plugin.bridge.call("read_datarefs", drefs, timeout=1.0)
                clean_results = sanitize_data(results)
                req.send_response(200)
                req.send_header('Content-type', 'application/json')
                req.end_headers()
                req.wfile.write(json.dumps({"status": "success", "data": clean_results}).encode('utf-8', errors='ignore'))
            except Exception:
                req.send_response(500)
                req.end_headers()

        elif req.path == '/api/get_payload':
            drefs = [
                "AirbusFBW/NoPax", "AirbusFBW/FwdCargo", "AirbusFBW/AftCargo", "toliss_web/current_fuel_kg", "toliss_web/max_fuel_kg"
            ]
            try:
                results = plugin.bridge.call("read_datarefs", drefs, timeout=1.5)
                clean_results = sanitize_data(results)
                req.send_response(200)
                req.send_header('Content-type', 'application/json')
                req.end_headers()
                req.wfile.write(json.dumps({
                    "status": "success",
                    "pax": clean_results.get(drefs[0], 0),
                    "cargo_fwd": clean_results.get(drefs[1], 0),
                    "cargo_aft": clean_results.get(drefs[2], 0),
                    "fuel": clean_results.get(drefs[3], 0),
                    "fuel_max": clean_results.get(drefs[4], 0)
                }).encode('utf-8', errors='ignore'))
            except Exception:
                req.send_response(500)
                req.end_headers()

        elif req.path.startswith('/api/stream'):
            # Server-sent events: the latest display snapshot, 15 times a second, while the page is open
            plugin.stream_clients = getattr(plugin, 'stream_clients', 0) + 1
            try:
                req.send_response(200)
                req.send_header('Content-Type', 'text/event-stream')
                req.send_header('Cache-Control', 'no-cache')
                req.send_header('Connection', 'keep-alive')
                req.end_headers()
                last = -1
                idle = 0.0
                while True:
                    snap = getattr(plugin, 'stream_snapshot', None)
                    if snap and snap.get("seq") != last:
                        last = snap["seq"]
                        req.wfile.write(b"data: " + json.dumps(sanitize_data(snap), separators=(',', ':')).encode('utf-8') + b"\n\n")
                        req.wfile.flush()
                        idle = 0.0
                    else:
                        idle += 1.0 / STREAM_RATE_HZ
                        if idle >= 5.0:          # keep the connection alive while the sim is paused
                            req.wfile.write(b": keep-alive\n\n")
                            req.wfile.flush()
                            idle = 0.0
                    time.sleep(1.0 / STREAM_RATE_HZ)
            except Exception:
                pass
            finally:
                plugin.stream_clients = max(0, getattr(plugin, 'stream_clients', 1) - 1)
            return

        elif req.path.startswith('/api/nearest_airport'):
            try:
                v = plugin.bridge.call(
                    "read_datarefs",
                    ["sim/flightmodel/position/latitude", "sim/flightmodel/position/longitude"],
                    timeout=1.5,
                )
                lat, lon = float(v.get("sim/flightmodel/position/latitude", 0)), float(v.get("sim/flightmodel/position/longitude", 0))
                icao = plugin.find_nearest_airport(lat, lon, 200000.0) if (lat or lon) else None
                body = {"status": "success", "icao": icao} if icao else {"status": "error", "message": "No airport found within 200 km."}
            except Exception:
                body = {"status": "error", "message": "No response from the sim"}
            req.send_response(200)
            req.send_header('Content-type', 'application/json')
            req.end_headers()
            req.wfile.write(json.dumps(body).encode('utf-8'))

        elif req.path == '/api/units':
            body = {"status": "success", "dist_unit": "m" if str(plugin.config.get("dist_unit", "ft")).lower() == "m" else "ft"}
            req.send_response(200)
            req.send_header('Content-type', 'application/json')
            req.end_headers()
            req.wfile.write(json.dumps(body).encode('utf-8'))

        elif req.path == '/api/push/settings':
            saved = plugin.config.get("push_settings")
            body = {"status": "success", "settings": saved if isinstance(saved, dict) else None}
            req.send_response(200)
            req.send_header('Content-type', 'application/json')
            req.end_headers()
            req.wfile.write(json.dumps(body).encode('utf-8'))

        elif req.path in ('/api/replay/state', '/api/traffic/state'):
            op = "replay" if req.path.startswith('/api/replay/') else "traffic"
            try:
                res = plugin.bridge.call(op, {"cmd": "state", "kind": "state"}, timeout=1.5)
                body = {"status": "success", **res.get("state", {})}
            except Exception:
                body = {"status": "error", "message": "No response from the sim"}
            req.send_response(200)
            req.send_header('Content-type', 'application/json')
            req.end_headers()
            req.wfile.write(json.dumps(body).encode('utf-8'))

        elif req.path.startswith('/api/instructor/state'):
            try:
                res = plugin.bridge.call("instructor", {"kind": "state"}, timeout=1.5)
                body = {"status": "success", **res["state"]}
            except Exception:
                body = {"status": "error", "message": "No response from the sim"}
            req.send_response(200)
            req.send_header('Content-type', 'application/json')
            req.end_headers()
            req.wfile.write(json.dumps(body).encode('utf-8'))

        elif req.path.startswith('/api/ops/'):
            ops = getattr(plugin, 'ops', None)
            parsed = urllib.parse.urlparse(req.path)
            qs = urllib.parse.parse_qs(parsed.query)
            if parsed.path == '/api/ops/status':
                body = {"status": "success", **ops.status()} if ops else {"status": "error"}
            elif parsed.path == '/api/ops/config':
                # the saved setup, to fill in the Ops Centre page (served only on your local network)
                body = {"status": "success", "logon": ops.logon, "ops_callsign": ops.ops_callsign,
                        "aircraft": ops.aircraft, "enabled": ops.enabled, "autoload": ops.autoload,
                        "simbrief_user": ops.ofp_user, "ofp_loaded": bool(ops.ofp)} if ops else {"status": "error"}
            elif parsed.path == '/api/ops/stands':
                try:
                    body = {"status": "success", **ops.stand_list()} if ops and ops.ofp else {"status": "error", "message": "Load a SimBrief flight plan first."}
                except Exception as e:
                    body = {"status": "error", "message": f"Could not read the stands: {e}"}
            elif parsed.path == '/api/ops/flight':
                body = {"status": "success", **ops.flight_status()} if ops else {"status": "error"}
            elif parsed.path == '/api/ops/log':
                since = int(qs.get("since", ["0"])[0] or 0)
                upd = int(qs.get("updated", ["0"])[0] or 0)
                # new entries, plus earlier ones whose status changed (e.g. a held message now sent)
                items = []
                if ops:
                    with ops.lock:
                        items = [dict(m) for m in ops.log if m["id"] > since or (m.get("rev") and max(m["t"], m.get("rt", 0)) >= upd)]
                        if qs.get("read", ["0"])[0] == "1":
                            ops.unread = 0
                body = {"status": "success", "messages": items}
            else:
                body = {"status": "error", "message": "Unknown request"}
            req.send_response(200)
            req.send_header('Content-type', 'application/json')
            req.end_headers()
            req.wfile.write(json.dumps(body).encode('utf-8'))

        elif req.path.startswith('/api/update/status'):
            up = getattr(plugin, 'updater', None)
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(req.path).query)
            if up and up.state not in ("downloading", "checking"):
                if qs.get("check", ["0"])[0] == "1":
                    up.check(force=True)
                else:
                    threading.Thread(target=up.check, daemon=True).start()
            body = up.status() if up else {"current": EFB_VERSION}
            req.send_response(200)
            req.send_header('Content-type', 'application/json')
            req.end_headers()
            req.wfile.write(json.dumps({"status": "success", **body}).encode('utf-8'))

        elif req.path.startswith('/api/fdr/'):
            fdr = getattr(plugin, 'fdr', None)
            parsed = urllib.parse.urlparse(req.path)
            qs = urllib.parse.parse_qs(parsed.query)
            d = fdr_dir(plugin.plugin_dir)
            name = fdr_safe_name(qs.get("name", [""])[0])
            def send_json(obj, code=200):
                req.send_response(code)
                req.send_header('Content-type', 'application/json')
                req.end_headers()
                req.wfile.write(json.dumps(sanitize_data(obj), separators=(',', ':')).encode('utf-8'))
            if parsed.path == '/api/fdr/status':
                send_json({"status": "success", **(fdr.status() if fdr else {"recording": False})})
            elif parsed.path == '/api/fdr/list':
                items = []
                for f in sorted(os.listdir(d), reverse=True):
                    if f.endswith(".csv") and not f.endswith(".events.csv"):
                        base = f[:-4]
                        meta = {}
                        try:
                            with open(os.path.join(d, base + ".json"), 'r', encoding='utf-8') as mf:
                                meta = json.load(mf)
                        except Exception:
                            pass
                        meta["name"] = base
                        meta["size"] = os.path.getsize(os.path.join(d, f))
                        meta["active"] = bool(fdr and fdr.recording and fdr.name == base)
                        items.append(meta)
                send_json({"status": "success", "recordings": items})
            elif parsed.path == '/api/fdr/data' and name:
                path = os.path.join(d, name + ".csv")
                if not os.path.isfile(path):
                    send_json({"status": "error", "message": "Recording not found"}, 404)
                else:
                    if fdr and fdr.recording and fdr.name == name:
                        try: fdr.fh.flush(); fdr.eh.flush()
                        except Exception: pass
                    header, rows = fdr_read_csv(path)
                    events = fdr_read_events(os.path.join(d, name + ".events.csv"))
                    meta = {}
                    try:
                        with open(os.path.join(d, name + ".json"), 'r', encoding='utf-8') as mf:
                            meta = json.load(mf)
                    except Exception:
                        pass
                    if "landings" not in meta:
                        meta["landings"] = fdr_analyse(plugin, rows, events)
                    cols = ["t", "lat", "lon", "alt_msl_ft", "ra_ft", "ias_kt", "gs_kt", "vs_fpm", "pitch_deg", "roll_deg",
                            "g_normal", "stick_pitch", "stick_roll", "n1_1", "n1_2", "thr_lever_1", "flap_lever", "gear",
                            "on_ground", "ap1", "ap2", "athr", "hdg_true"]
                    try:
                        maxn = max(200, min(20000, int(qs.get("max", ["4000"])[0])))
                    except ValueError:
                        maxn = 4000
                    step = max(1, int(math.ceil(len(rows) / float(maxn))))
                    keep = rows[::step]
                    data = {c: [r.get(c, "") for r in keep] for c in cols}
                    send_json({"status": "success", "name": name, "meta": meta, "data": data, "events": events,
                               "samples": len(rows), "step": step})
            elif parsed.path == '/api/fdr/download' and name:
                kind = qs.get("kind", ["csv"])[0]
                if kind == "kml":
                    path = os.path.join(d, name + ".csv")
                    if not os.path.isfile(path):
                        send_json({"status": "error"}, 404)
                    else:
                        _, rows = fdr_read_csv(path)
                        coords = " ".join(f"{r['lon']},{r['lat']},{r['alt_msl_ft'] / FT_PER_M:.1f}" for r in rows
                                          if isinstance(r.get('lat'), float) and isinstance(r.get('alt_msl_ft'), float))
                        kml = ('<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
                               f'<name>{name}</name><Style id="t"><LineStyle><color>ff00d4ff</color><width>3</width></LineStyle></Style>'
                               f'<Placemark><name>{name}</name><styleUrl>#t</styleUrl><LineString><altitudeMode>absolute</altitudeMode>'
                               f'<coordinates>{coords}</coordinates></LineString></Placemark></Document></kml>').encode('utf-8')
                        req.send_response(200)
                        req.send_header('Content-type', 'application/vnd.google-earth.kml+xml')
                        req.send_header('Content-Disposition', f'attachment; filename="{name}.kml"')
                        req.end_headers()
                        req.wfile.write(kml)
                else:
                    fname = name + (".events.csv" if kind == "events" else ".csv")
                    path = os.path.join(d, fname)
                    if not os.path.isfile(path):
                        send_json({"status": "error"}, 404)
                    else:
                        with open(path, 'rb') as f:
                            content = f.read()
                        req.send_response(200)
                        req.send_header('Content-type', 'text/csv')
                        req.send_header('Content-Disposition', f'attachment; filename="{fname}"')
                        req.end_headers()
                        req.wfile.write(content)
            else:
                send_json({"status": "error", "message": "Unknown request"}, 404)

        elif req.path.startswith('/api/checklists'):
            items = []
            try:
                d = checklist_dir(plugin.plugin_dir)
                for f in sorted(os.listdir(d)):
                    if f.lower().endswith(".xml"):
                        st = os.stat(os.path.join(d, f))
                        items.append({"name": f[:-4], "size": st.st_size, "modified": int(st.st_mtime)})
            except Exception as e:
                plugin.log.xplane(f"ToLiss EFB: checklist list error: {e}")
            req.send_response(200)
            req.send_header('Content-type', 'application/json')
            req.end_headers()
            req.wfile.write(json.dumps({"status": "success", "checklists": items}).encode('utf-8'))

        elif req.path.startswith('/api/get_checklist?') and 'custom=' in req.path:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(req.path).query)
            name = checklist_safe_name(qs.get("custom", [""])[0])
            path = os.path.join(checklist_dir(plugin.plugin_dir), name + ".xml")
            if name and os.path.isfile(path):
                with open(path, 'rb') as xfile:
                    content = xfile.read().decode('utf-8', errors='ignore')
                req.send_response(200)
                req.send_header('Content-type', 'application/json')
                req.end_headers()
                req.wfile.write(json.dumps({"status": "success", "path": path, "xml": content, "custom": name}).encode('utf-8'))
            else:
                req.send_response(404)
                req.end_headers()
                req.wfile.write(b'{"status":"error", "message":"Custom checklist not found"}')

        elif req.path.startswith('/api/get_checklist'):
            try:
                acf_path = plugin.bridge.call("aircraft_path", None, timeout=2.0)
                if acf_path:
                    ac_dir = os.path.dirname(acf_path)
                    plugins_dir = os.path.join(ac_dir, "plugins")
                    if os.path.exists(plugins_dir):
                        for root, dirs, files in os.walk(plugins_dir):
                            for f in files:
                                if f.lower() == "checklist.xml":
                                    full_path = os.path.join(root, f)
                                    with open(full_path, 'rb') as xfile:
                                        content = xfile.read().decode('utf-8', errors='ignore')
                                    req.send_response(200)
                                    req.send_header('Content-type', 'application/json')
                                    req.end_headers()
                                    req.wfile.write(json.dumps({"status": "success", "path": full_path, "xml": content}).encode())
                                    return
            except Exception: pass
            req.send_response(404)
            req.end_headers()
            req.wfile.write(b'{"status":"error", "message":"Checklist not found"}')

        else:
            req.send_response(404)
            req.end_headers()
    except Exception as e:
        plugin.log.xplane(f"ToLiss EFB GET Error: {e}")
