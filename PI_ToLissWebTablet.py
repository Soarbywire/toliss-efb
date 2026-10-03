import os
import json
import time
import threading
import queue
import socket
import socketserver
import http.server
import urllib.request
import urllib.error
import urllib.parse
import math
import re
import mmap
import pickle
import shutil
import glob
from XPLMDefs import *
from XPLMUtilities import *
from XPLMDataAccess import *
from XPLMProcessing import *
from XPLMPlanes import *
from XPLMGraphics import *
from XPLMScenery import *

# Independent Queues to prevent Race Conditions
# ===== Version and updates =====
EFB_VERSION = "0.65"
# GitHub repository that publishes releases ("owner/name"). Change the owner if your GitHub username differs.
UPDATE_REPO = "soarbywire/toliss-efb"
UPDATE_API = "https://api.github.com/repos/{repo}/releases/latest"
# Fallback that does not use GitHub's API: the web address of the latest release redirects to its tag
UPDATE_WEB = "https://github.com/{repo}/releases/latest"
UPDATE_DL = "https://github.com/{repo}/releases/download/v{ver}/{name}"
UPDATE_RAW = "https://raw.githubusercontent.com/{repo}/v{ver}/CHANGELOG.md"
UPDATE_CHECK_INTERVAL_S = 6 * 3600
UPDATE_MAX_BYTES = 30 * 1024 * 1024

q_cmd = queue.Queue()
q_dref_w = queue.Queue()
q_teleport = queue.Queue()
q_load_sit = queue.Queue()
q_sit_req = queue.Queue()
q_fault_req = queue.Queue()
q_payload_apply = queue.Queue()

vatsim_cache = {"data": None, "last_fetch": 0}

# Separate lanes for each specific data type
q_state_req = queue.Queue()
q_state_res = queue.Queue()
q_payload_req = queue.Queue()
q_payload_res = queue.Queue()
q_telem_req = queue.Queue()
q_mcdu_req = queue.Queue()     # (side, reply_queue) screen reads
q_mcdu_keys = queue.Queue()    # (side, key) presses, applied one at a time
q_telem_res = queue.Queue()
q_path_req = queue.Queue()
q_path_res = queue.Queue()

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except:
        return "127.0.0.1"

def fetch_vatsim_loop():
    last_tx = 0
    while True:
        try:
            req = urllib.request.Request("https://data.vatsim.net/v3/vatsim-data.json", headers={'User-Agent': 'ToLissEFB/1.0'})
            with urllib.request.urlopen(req, timeout=15) as response:
                vatsim_cache["data"] = json.loads(response.read().decode('utf-8'))
                vatsim_cache["last_fetch"] = time.time()
        except Exception as e:
            XPLMDebugString(f"ToLiss EFB: VATSIM Fetch Error: {e}\n")
        # Radio transmitter positions of ATC stations (Audio for VATSIM), refreshed every 60 s
        if time.time() - last_tx > 55:
            try:
                data = vatsim_cache.get("data") or {}
                atc = set(c.get("callsign", "") for c in data.get("controllers", []))
                atc |= set(a.get("callsign", "") for a in data.get("atis", []))
                req = urllib.request.Request("https://data.vatsim.net/v3/transceivers-data.json", headers={'User-Agent': 'ToLissEFB/1.0'})
                with urllib.request.urlopen(req, timeout=20) as response:
                    raw = json.loads(response.read().decode('utf-8'))
                tx = {}
                for entry in raw:
                    cs = entry.get("callsign", "")
                    if cs in atc:
                        tx[cs] = [(t.get("latDeg", 0.0), t.get("lonDeg", 0.0), t.get("heightAglM", 0.0), t.get("frequency", 0))
                                  for t in entry.get("transceivers", [])]
                vatsim_cache["transceivers"] = tx
                last_tx = time.time()
            except Exception as e:
                XPLMDebugString(f"ToLiss EFB: VATSIM transceivers fetch error: {e}\n")
        time.sleep(30)


# ===== Flight data recorder =====
KT_PER_MS = 1.943844
FT_PER_M = 3.28084

# (column, dataref, array index or None, conversion)
FDR_CHANNELS = [
    ("lat", "sim/flightmodel/position/latitude", None, lambda v: round(v, 6)),
    ("lon", "sim/flightmodel/position/longitude", None, lambda v: round(v, 6)),
    ("alt_msl_ft", "sim/flightmodel/position/elevation", None, lambda v: round(v * FT_PER_M, 1)),
    ("alt_baro_ft", "sim/cockpit2/gauges/indicators/altitude_ft_pilot", None, lambda v: round(v, 0)),
    ("ra_ft", "sim/cockpit2/gauges/indicators/radio_altimeter_height_ft_pilot", None, lambda v: round(v, 1)),
    ("agl_ft", "sim/flightmodel/position/y_agl", None, lambda v: round(v * FT_PER_M, 1)),
    ("ias_kt", "sim/cockpit2/gauges/indicators/airspeed_kts_pilot", None, lambda v: round(v, 1)),
    ("tas_kt", "sim/flightmodel/position/true_airspeed", None, lambda v: round(v * KT_PER_MS, 1)),
    ("gs_kt", "sim/flightmodel/position/groundspeed", None, lambda v: round(v * KT_PER_MS, 1)),
    ("mach", "sim/flightmodel/misc/machno", None, lambda v: round(v, 3)),
    ("vs_fpm", "sim/flightmodel/position/vh_ind_fpm", None, lambda v: round(v, 0)),
    ("pitch_deg", "sim/flightmodel/position/theta", None, lambda v: round(v, 2)),
    ("roll_deg", "sim/flightmodel/position/phi", None, lambda v: round(v, 2)),
    ("hdg_true", "sim/flightmodel/position/psi", None, lambda v: round(v, 1)),
    ("hdg_mag", "sim/flightmodel/position/mag_psi", None, lambda v: round(v, 1)),
    ("track_true", "sim/flightmodel/position/hpath", None, lambda v: round(v, 1)),
    ("aoa_deg", "sim/flightmodel/position/alpha", None, lambda v: round(v, 2)),
    ("g_normal", "sim/flightmodel/forces/g_nrml", None, lambda v: round(v, 3)),
    ("g_lateral", "sim/flightmodel/forces/g_side", None, lambda v: round(v, 3)),
    ("g_long", "sim/flightmodel/forces/g_axil", None, lambda v: round(v, 3)),
    ("stick_pitch", "sim/joystick/yoke_pitch_ratio", None, lambda v: round(v, 3)),
    ("stick_roll", "sim/joystick/yoke_roll_ratio", None, lambda v: round(v, 3)),
    ("rudder", "sim/joystick/yoke_heading_ratio", None, lambda v: round(v, 3)),
    ("brake_left", "sim/cockpit2/controls/left_brake_ratio", None, lambda v: round(v, 2)),
    ("brake_right", "sim/cockpit2/controls/right_brake_ratio", None, lambda v: round(v, 2)),
    ("park_brake", "sim/cockpit2/controls/parking_brake_ratio", None, lambda v: round(v, 2)),
    ("thr_lever_1", "AirbusFBW/throttle_input", 0, lambda v: round(v, 3)),
    ("thr_lever_2", "AirbusFBW/throttle_input", 1, lambda v: round(v, 3)),
    ("n1_1", "sim/cockpit2/engine/indicators/N1_percent", 0, lambda v: round(v, 1)),
    ("n1_2", "sim/cockpit2/engine/indicators/N1_percent", 1, lambda v: round(v, 1)),
    ("egt_1", "sim/cockpit2/engine/indicators/EGT_deg_C", 0, lambda v: round(v, 0)),
    ("egt_2", "sim/cockpit2/engine/indicators/EGT_deg_C", 1, lambda v: round(v, 0)),
    ("ff_1_kgh", "sim/cockpit2/engine/indicators/fuel_flow_kg_sec", 0, lambda v: round(v * 3600, 0)),
    ("ff_2_kgh", "sim/cockpit2/engine/indicators/fuel_flow_kg_sec", 1, lambda v: round(v * 3600, 0)),
    ("reverser_1", "sim/flightmodel2/engines/thrust_reverser_deploy_ratio", 0, lambda v: round(v, 2)),
    ("reverser_2", "sim/flightmodel2/engines/thrust_reverser_deploy_ratio", 1, lambda v: round(v, 2)),
    ("fuel_kg", "sim/flightmodel/weight/m_fuel_total", None, lambda v: round(v, 0)),
    ("weight_kg", "sim/flightmodel/weight/m_total", None, lambda v: round(v, 0)),
    ("flap_lever", "sim/cockpit2/controls/flap_ratio", None, lambda v: round(v, 3)),
    ("flaps", "sim/flightmodel2/controls/flap1_deploy_ratio", None, lambda v: round(v, 3)),
    ("slats", "sim/flightmodel2/controls/slat1_deploy_ratio", None, lambda v: round(v, 3)),
    ("gear_lever", "sim/cockpit2/controls/gear_handle_down", None, lambda v: int(v)),
    ("gear", "sim/flightmodel2/gear/deploy_ratio", 0, lambda v: round(v, 2)),
    ("speedbrake", "sim/cockpit2/controls/speedbrake_ratio", None, lambda v: round(v, 2)),
    ("ap1", "AirbusFBW/AP1Engage", None, lambda v: int(v)),
    ("ap2", "AirbusFBW/AP2Engage", None, lambda v: int(v)),
    ("athr", "AirbusFBW/ATHRmode", None, lambda v: int(v)),
    ("fd1", "AirbusFBW/FD1Engage", None, lambda v: int(v)),
    ("appr_armed", "AirbusFBW/APPRilluminated", None, lambda v: int(v)),
    ("loc_armed", "AirbusFBW/LOCilluminated", None, lambda v: int(v)),
    ("fcu_spd", "sim/cockpit/autopilot/airspeed", None, lambda v: round(v, 0)),
    ("fcu_hdg", "sim/cockpit/autopilot/heading_mag", None, lambda v: round(v, 0)),
    ("fcu_alt", "sim/cockpit/autopilot/altitude", None, lambda v: round(v, 0)),
    ("wind_dir_mag", "sim/cockpit2/gauges/indicators/wind_heading_deg_mag", None, lambda v: round(v, 0)),
    ("wind_kt", "sim/cockpit2/gauges/indicators/wind_speed_kts", None, lambda v: round(v, 0)),
    ("oat_c", "sim/weather/aircraft/temperature_ambient_deg_c", None, lambda v: round(v, 1)),
    ("baro_inhg", "sim/cockpit2/gauges/actuators/barometer_setting_in_hg_pilot", None, lambda v: round(v, 2)),
    ("master_warning", "sim/cockpit2/annunciators/master_warning", None, lambda v: int(v)),
    ("master_caution", "sim/cockpit2/annunciators/master_caution", None, lambda v: int(v)),
    ("on_ground", "sim/flightmodel/failures/onground_any", None, lambda v: int(v)),
]

# ECAM E/WD message lines published by ToLiss: AirbusFBW/EWD<line><colour>Text
ECAM_COLOURS = {"r": "red", "a": "amber", "g": "green", "b": "blue", "w": "white"}
ECAM_EVENT_COLOURS = ("r", "a", "g")      # blue/white are action and title lines, logged as part of the messages


def fdr_dir(plugin_dir):
    d = os.path.join(plugin_dir, "fdr")
    os.makedirs(d, exist_ok=True)
    return d


def fdr_safe_name(name):
    return re.sub(r"[^A-Za-z0-9_.-]", "", str(name or ""))[:120]


def fdr_read_csv(path):
    import csv
    with open(path, 'r', encoding='utf-8', newline='') as f:
        rd = csv.reader(f)
        header = next(rd, None) or []
        rows = []
        for row in rd:
            rec = {}
            for k, v in zip(header, row):
                try:
                    rec[k] = float(v)
                except ValueError:
                    rec[k] = v
            rows.append(rec)
    return header, rows


def fdr_read_events(path):
    import csv
    out = []
    if not os.path.isfile(path):
        return out
    with open(path, 'r', encoding='utf-8', newline='') as f:
        rd = csv.DictReader(f)
        for r in rd:
            try:
                r["t"] = float(r.get("t", 0))
                r["lat"] = float(r.get("lat") or 0)
                r["lon"] = float(r.get("lon") or 0)
                r["alt_msl_ft"] = float(r.get("alt_msl_ft") or 0)
            except ValueError:
                pass
            out.append(r)
    return out


def fdr_find_runway(plugin, lat, lon, track):
    """The runway end the aircraft landed on: nearest aligned runway at the nearest airport."""
    try:
        icao = plugin.find_nearest_airport(lat, lon, 6000.0)
        if not icao:
            return None
        loc = plugin.apt_index.get(icao)
        if not loc:
            return None
        gm = parse_ground_map(loc[0], loc[1])
    except Exception:
        return None
    best = None
    for r in gm.get("runways", []):
        for (n, la, lo, disp, la2, lo2) in ((r["n1"], r["lat1"], r["lon1"], r["d1"], r["lat2"], r["lon2"]),
                                            (r["n2"], r["lat2"], r["lon2"], r["d2"], r["lat1"], r["lon1"])):
            kx = 111320.0 * math.cos(math.radians(la))
            dx, dy = (lo2 - lo) * kx, (la2 - la) * 111320.0
            length = math.hypot(dx, dy) or 1.0
            ux, uy = dx / length, dy / length
            hdg = (math.degrees(math.atan2(ux, uy)) + 360) % 360
            diff = abs(((track - hdg) + 180) % 360 - 180)
            if diff > 35:
                continue
            px, py = (lon - lo) * kx, (lat - la) * 111320.0
            along = px * ux + py * uy
            cross = px * uy - py * ux          # positive = right of centreline
            if abs(cross) > max(150.0, r["w"]) or along < -300 or along > length + 300:
                continue
            score = abs(cross) + diff * 5
            if best is None or score < best["score"]:
                best = {"score": score, "icao": icao, "runway": n, "thr_lat": la, "thr_lon": lo, "disp_m": disp,
                        "ux": ux, "uy": uy, "kx": kx, "length_m": length, "width_m": r["w"], "hdg": hdg,
                        "elev_ft": gm.get("elev_ft", 0.0)}
    return best


def fdr_analyse(plugin, rows, events):
    """Landing reports for every landing in a recording."""
    reports = []
    n = len(rows)
    if n < 10:
        return reports
    def g(i, k, d=0.0):
        v = rows[i].get(k, d)
        return v if isinstance(v, float) else d
    # touchdowns: ground contact after at least 30 s airborne, while moving fast
    i = 1
    # a recording that starts in the air (e.g. approach training) is airborne from its first sample
    last_air_start = g(0, "t") if g(0, "on_ground") < 1 else None
    while i < n:
        # a repositioning jump starts a new airborne segment
        if abs(g(i, "lat") - g(i - 1, "lat")) + abs(g(i, "lon") - g(i - 1, "lon")) > 0.03 and g(i, "on_ground") < 1:
            last_air_start = g(i, "t")
        og_prev, og = g(i - 1, "on_ground"), g(i, "on_ground")
        if og_prev >= 1 and og < 1:
            last_air_start = g(i, "t")
        if og_prev < 1 and og >= 1 and g(i, "ias_kt") > 60 and last_air_start is not None and g(i, "t") - last_air_start > 30:
            td = i
            t_td = g(td, "t")
            # vertical speed and g at touchdown
            # landing rate: vertical speed at the moment of contact (last airborne sample)
            vs_td = g(td - 1, "vs_fpm") if td > 0 else g(td, "vs_fpm")
            win = [j for j in range(max(0, td - 4), min(n, td + 12)) if abs(g(j, "t") - t_td) <= 2.0]
            g_max = max([g(j, "g_normal", 1.0) for j in win] or [1.0])
            rep = {"t": t_td, "lat": g(td, "lat"), "lon": g(td, "lon"), "vs_fpm": round(vs_td),
                   "g": round(g_max, 2), "ias_kt": round(g(td, "ias_kt")), "gs_kt": round(g(td, "gs_kt")),
                   "pitch_deg": round(g(td, "pitch_deg"), 1), "roll_deg": round(g(td, "roll_deg"), 1),
                   "crab_deg": round(((g(td, "hdg_true") - g(td, "track_true")) + 180) % 360 - 180, 1),
                   "wind": f"{int(g(td, 'wind_dir_mag')):03d}/{int(g(td, 'wind_kt'))}",
                   "flap_lever": g(td, "flap_lever"), "bounces": 0}
            # bounces: airborne again within 10 s of touchdown
            j = td + 1
            while j < n and g(j, "t") - t_td < 10:
                if g(j - 1, "on_ground") >= 1 and g(j, "on_ground") < 1:
                    rep["bounces"] += 1
                j += 1
            # runway, touchdown point and threshold crossing
            rw = fdr_find_runway(plugin, rep["lat"], rep["lon"], g(td, "track_true"))
            elev_ft = g(td, "alt_msl_ft") - g(td, "agl_ft")
            if rw:
                def along_cross(k):
                    px = (g(k, "lon") - rw["thr_lon"]) * rw["kx"]
                    py = (g(k, "lat") - rw["thr_lat"]) * 111320.0
                    return px * rw["ux"] + py * rw["uy"], px * rw["uy"] - py * rw["ux"]
                a_td, c_td = along_cross(td)
                disp = rw["disp_m"] or 0.0
                rep.update({"airport": rw["icao"], "runway": rw["runway"],
                            "td_from_threshold_m": round(a_td - disp), "centreline_m": round(c_td, 1),
                            "runway_length_m": round(rw["length_m"])})
                # threshold crossing: last sample before touchdown still short of the landing threshold
                for k in range(td, max(0, td - 400), -1):
                    a_k, _ = along_cross(k)
                    if a_k <= disp:
                        rep["threshold_height_ft"] = round(g(k, "ra_ft") or g(k, "agl_ft"))
                        rep["threshold_ias_kt"] = round(g(k, "ias_kt"))
                        break
            # stabilised-approach gates (height above the touchdown elevation)
            gates = {}
            for gate in (1000, 500):
                for k in range(td, max(0, td - 2400), -1):
                    if g(k, "alt_msl_ft") - elev_ft >= gate:
                        vs = g(k, "vs_fpm")
                        lever = g(k, "flap_lever")
                        gear_down = g(k, "gear") > 0.99
                        issues = []
                        if not gear_down: issues.append("gear not down")
                        if lever < 0.74: issues.append("not in landing flaps")
                        if vs < -1000: issues.append(f"descending {int(-vs)} fpm")
                        if g(k, "thr_lever_1", 0.0) == 0.0 and g(k, "athr", 0.0) == 0 and g(k, "n1_1") < 30: issues.append("thrust at idle")
                        gates[str(gate)] = {"ias_kt": round(g(k, "ias_kt")), "vs_fpm": round(vs), "gear_down": gear_down,
                                            "flap_lever": lever, "ap": int(g(k, "ap1") or g(k, "ap2")),
                                            "stable": not issues, "issues": issues}
                        break
            rep["gates"] = gates
            # rollout: until below 30 kt ground speed
            for k in range(td, n):
                if g(k, "gs_kt") < 30:
                    rep["rollout_m"] = round(_gm_dist_m(g(td, "lat"), g(td, "lon"), g(k, "lat"), g(k, "lon")))
                    rep["rollout_s"] = round(g(k, "t") - t_td)
                    break
            rep["reversers"] = any(g(k, "reverser_1") > 0.5 for k in range(td, min(n, td + 240)))
            reports.append(rep)
            i = td + 1
            continue
        i += 1
    return reports


class FlightRecorder:
    def __init__(self, plugin):
        self.plugin = plugin
        cfg = plugin.config if isinstance(getattr(plugin, 'config', None), dict) else {}
        self.enabled = bool(cfg.get("fdr_enabled", True))  # Settings switch: when off nothing is recorded
        self.mode = cfg.get("fdr_mode", "manual")         # manual (default) / auto
        if not cfg.get("fdr_mode_v2"):
            # From 0.59 the recorder starts in manual mode; switch older saved settings once
            self.mode = "manual"
            try:
                plugin.config["fdr_mode"] = "manual"
                plugin.config["fdr_mode_v2"] = True
                plugin.save_config(plugin.config)
            except Exception:
                pass
        self.rate = float(cfg.get("fdr_rate", 4))          # samples per second
        self.recording = False
        self.name = None
        self.fh = None
        self.eh = None
        self.t0 = 0.0
        self.samples = 0
        self.last_sample = 0.0
        self.last_flush = 0.0
        self.last_ecam = 0.0
        self.ecam_now = {}
        self.prev = {}
        self.stop_timer = None
        self.lock = threading.Lock()
        self.last_error = ""

    def save_settings(self):
        try:
            self.plugin.config["fdr_enabled"] = self.enabled
            self.plugin.config["fdr_mode"] = self.mode
            self.plugin.config["fdr_rate"] = self.rate
            self.plugin.save_config(self.plugin.config)
        except Exception:
            pass

    # --- reading ---
    def _val(self, dref, idx):
        p = self.plugin
        ref = p._dref(dref)
        if ref is None:
            return None
        try:
            if idx is None:
                return p._get_f(dref, None)
            types = XPLMGetDataRefTypes(ref)
            out = []
            if types & xplmType_FloatArray:
                XPLMGetDatavf(ref, out, idx, 1)
            elif types & xplmType_IntArray:
                XPLMGetDatavi(ref, out, idx, 1)
            else:
                return None
            return float(out[0]) if out else None
        except Exception:
            return None

    def sample(self):
        row = {}
        for col, dref, idx, conv in FDR_CHANNELS:
            v = self._val(dref, idx)
            try:
                row[col] = conv(v) if v is not None else ""
            except Exception:
                row[col] = ""
        return row

    def ecam_messages(self):
        msgs = {}
        p = self.plugin
        for line in range(1, 8):
            for c in ECAM_COLOURS:
                ref = p._dref(f"AirbusFBW/EWD{line}{c}Text")
                if ref is None:
                    continue
                raw = bytes(b for b in p._read_bytes(ref, 48) if b).decode('latin-1', errors='ignore')
                for part in re.split(r"\s{2,}", raw):
                    part = part.strip()
                    if len(part) >= 2:
                        msgs[(c, part)] = line
        return msgs

    # --- recording ---
    def status(self):
        return {"recording": self.recording, "enabled": self.enabled, "name": self.name, "mode": self.mode, "rate": self.rate,
                "duration_s": round(time.time() - self.t0) if self.recording else 0, "samples": self.samples,
                "error": self.last_error}

    def start(self, reason="manual"):
        with self.lock:
            if self.recording or not self.enabled:
                return
            p = self.plugin
            try:
                icao = p._read_string_dataref(p._dref("sim/aircraft/view/acf_ICAO")).strip() or "ACFT"
            except Exception:
                icao = "ACFT"
            lat = p._get_f("sim/flightmodel/position/latitude")
            lon = p._get_f("sim/flightmodel/position/longitude")
            apt = ""
            try:
                apt = p.find_nearest_airport(lat, lon, 15000.0) or ""
            except Exception:
                pass
            stamp = time.strftime("%Y-%m-%d_%H%M%S")
            self.name = fdr_safe_name(f"{stamp}_{icao}" + (f"_{apt}" if apt else ""))
            d = fdr_dir(p.plugin_dir)
            try:
                self.fh = open(os.path.join(d, self.name + ".csv"), 'w', encoding='utf-8', newline='')
                self.eh = open(os.path.join(d, self.name + ".events.csv"), 'w', encoding='utf-8', newline='')
                self.fh.write(",".join(["t", "utc"] + [c[0] for c in FDR_CHANNELS]) + "\n")
                self.eh.write("t,utc,lat,lon,alt_msl_ft,type,colour,text\n")
                meta = {"name": self.name, "aircraft": icao, "start_airport": apt, "started": stamp,
                        "rate": self.rate, "reason": reason}
                with open(os.path.join(d, self.name + ".json"), 'w', encoding='utf-8') as f:
                    json.dump(meta, f)
            except Exception as e:
                self.last_error = f"Could not create the recording files: {e}"
                XPLMDebugString(f"ToLiss EFB: FDR {self.last_error}\n")
                self.fh = self.eh = None
                return
            self.recording = True
            self.last_error = ""
            self.t0 = time.time()
            self.samples = 0
            self.last_sample = 0.0
            self.ecam_now = {}
            self.prev = {}
            self.stop_timer = None
            self.event("RECORDING", "", f"Recording started ({reason})")
            XPLMDebugString(f"ToLiss EFB: FDR recording started: {self.name}\n")

    def stop(self, reason="manual"):
        with self.lock:
            if not self.recording:
                return
            self.event("RECORDING", "", f"Recording stopped ({reason})")
            self.recording = False
            try:
                self.fh.close(); self.eh.close()
            except Exception:
                pass
            name = self.name
            self.fh = self.eh = None
        threading.Thread(target=self.finalise, args=(name,), daemon=True).start()

    def finalise(self, name):
        """Work out the landing report(s) once a recording has finished."""
        d = fdr_dir(self.plugin.plugin_dir)
        try:
            header, rows = fdr_read_csv(os.path.join(d, name + ".csv"))
            events = fdr_read_events(os.path.join(d, name + ".events.csv"))
            meta_path = os.path.join(d, name + ".json")
            meta = {}
            if os.path.isfile(meta_path):
                with open(meta_path, 'r', encoding='utf-8') as f:
                    meta = json.load(f)
            meta["duration_s"] = round(rows[-1]["t"]) if rows else 0
            meta["samples"] = len(rows)
            meta["landings"] = fdr_analyse(self.plugin, rows, events)
            meta["ecam_count"] = sum(1 for e in events if e.get("type") == "ECAM")
            if rows:
                try:
                    meta["end_airport"] = self.plugin.find_nearest_airport(rows[-1]["lat"], rows[-1]["lon"], 15000.0) or ""
                except Exception:
                    pass
            with open(meta_path, 'w', encoding='utf-8') as f:
                json.dump(meta, f)
        except Exception as e:
            XPLMDebugString(f"ToLiss EFB: FDR analysis failed for {name}: {e}\n")

    def event(self, etype, colour, text, row=None):
        if not self.eh:
            return
        row = row or {}
        t = round(time.time() - self.t0, 2)
        utc = time.strftime("%H:%M:%S", time.gmtime())
        txt = str(text).replace('"', "'")
        self.eh.write(f'{t},{utc},{row.get("lat", "")},{row.get("lon", "")},{row.get("alt_msl_ft", "")},{etype},{colour},"{txt}"\n')

    def request(self, action, reason="manual"):
        """Start/stop asked for from the web page. Starting reads X-Plane values, which is only allowed on
        X-Plane's main thread (calling it from the web server's thread can crash X-Plane, notably on macOS),
        so the request is carried out by tick() in the flight loop, normally within one frame."""
        self.pending = (action, reason)

    def tick(self):
        pending, self.pending = getattr(self, 'pending', None), None
        if pending:
            action, reason = pending
            if action == "start":
                self.start(reason)
            elif action == "stop":
                self.stop(reason)
        if not self.enabled:
            if self.recording:
                self.stop("flight recorder switched off")
            return
        now = time.time()
        p = self.plugin
        try:
            paused = p._get_f("sim/time/paused") >= 1
        except Exception:
            paused = False
        # automatic start/stop
        if self.mode == "auto" and not paused:
            if not self.recording:
                if now - self.last_sample >= 1.0:
                    self.last_sample = now
                    running = (self._val("sim/flightmodel/engine/ENGN_running", 0) or 0) >= 1 or (self._val("sim/flightmodel/engine/ENGN_running", 1) or 0) >= 1
                    airborne = p._get_f("sim/flightmodel/failures/onground_any") < 1 and p._get_f("sim/cockpit2/gauges/indicators/airspeed_kts_pilot") > 60
                    if running or airborne:
                        self.start("automatic: " + ("engines running" if running else "airborne"))
                return
            else:
                engines_off = (self._val("sim/flightmodel/engine/ENGN_running", 0) or 0) < 1 and (self._val("sim/flightmodel/engine/ENGN_running", 1) or 0) < 1
                parked = p._get_f("sim/flightmodel/failures/onground_any") >= 1 and p._get_f("sim/flightmodel/position/groundspeed") < 0.5
                if engines_off and parked:
                    if self.stop_timer is None:
                        self.stop_timer = now
                    elif now - self.stop_timer > 30:
                        self.stop("automatic: engines off and parked")
                        return
                else:
                    self.stop_timer = None
        if not self.recording or paused:
            return
        if now - self.last_sample < 1.0 / max(0.5, self.rate):
            return
        self.last_sample = now
        row = self.sample()
        t = round(now - self.t0, 2)
        utc = time.strftime("%H:%M:%S", time.gmtime())
        try:
            self.fh.write(",".join([str(t), utc] + [str(row[c[0]]) for c in FDR_CHANNELS]) + "\n")
            self.samples += 1
        except Exception as e:
            self.last_error = str(e)
            return
        self.detect_events(row)
        if now - self.last_ecam >= 1.0:
            self.last_ecam = now
            msgs = self.ecam_messages()
            for key in msgs.keys() - self.ecam_now.keys():
                if key[0] in ECAM_EVENT_COLOURS or key[0] == "b":
                    self.event("ECAM", ECAM_COLOURS[key[0]], key[1], row)
            for key in self.ecam_now.keys() - msgs.keys():
                if key[0] in ECAM_EVENT_COLOURS:
                    self.event("ECAM_CLEARED", ECAM_COLOURS[key[0]], key[1], row)
            self.ecam_now = msgs
        if now - self.last_flush > 3:
            self.last_flush = now
            try:
                self.fh.flush(); self.eh.flush()
            except Exception:
                pass

    def detect_events(self, row):
        prev = self.prev
        def num(r, k):
            v = r.get(k, "")
            return v if isinstance(v, (int, float)) else None
        if prev:
            # repositioning: a jump no aircraft could fly between two samples
            if None not in (num(prev, "lat"), num(row, "lat")):
                d = _gm_dist_m(prev["lat"], prev["lon"], row["lat"], row["lon"])
                if d > 3000:
                    self.event("REPOSITION", "", f"Aircraft moved {d / 1852:.1f} nm", row)
            for key, label in (("master_warning", "MASTER WARNING"), ("master_caution", "MASTER CAUTION")):
                if num(prev, key) is not None and num(row, key) is not None and prev[key] != row[key]:
                    self.event(label, "red" if key == "master_warning" else "amber", "ON" if row[key] else "OFF", row)
            og0, og1 = num(prev, "on_ground"), num(row, "on_ground")
            if og0 is not None and og1 is not None and og0 != og1:
                ias = num(row, "ias_kt")
                vs = num(prev, "vs_fpm") if og1 else num(row, "vs_fpm")      # touchdown: vertical speed at contact
                self.event("TOUCHDOWN" if og1 else "LIFTOFF", "",
                           f"{int(round(ias)) if ias is not None else '?'} kt, {int(round(vs)) if vs is not None else '?'} fpm", row)
            for key, label in (("ap1", "AP1"), ("ap2", "AP2")):
                if num(prev, key) is not None and num(row, key) is not None and prev[key] != row[key]:
                    self.event("AUTOPILOT", "", f"{label} {'ON' if row[key] else 'OFF'}", row)
            if num(prev, "gear_lever") is not None and num(row, "gear_lever") is not None and prev["gear_lever"] != row["gear_lever"]:
                self.event("GEAR", "", "DOWN" if row["gear_lever"] else "UP", row)
            fl0, fl1 = num(prev, "flap_lever"), num(row, "flap_lever")
            if fl0 is not None and fl1 is not None and round(fl0 * 4) != round(fl1 * 4):
                self.event("FLAPS", "", ["0", "1", "2", "3", "FULL"][max(0, min(4, round(fl1 * 4)))], row)
        self.prev = row


# ===== Cockpit displays: live data stream =====
STREAM_RATE_HZ = 15.0

# Numeric values for the PFD and E/WD: key -> (dataref, array index or None)
DISPLAY_VALUES = {
    "pitch": ("sim/flightmodel/position/theta", None), "roll": ("sim/flightmodel/position/phi", None),
    "hdg": ("sim/flightmodel/position/mag_psi", None), "psi": ("sim/flightmodel/position/psi", None),
    "hpath": ("sim/flightmodel/position/hpath", None), "slip": ("sim/cockpit2/gauges/indicators/slip_deg", None),
    "ias": ("sim/cockpit2/gauges/indicators/airspeed_kts_pilot", None), "mach": ("sim/flightmodel/misc/machno", None),
    "gs": ("sim/flightmodel/position/groundspeed", None), "tas": ("sim/flightmodel/position/true_airspeed", None),
    "alt": ("sim/cockpit2/gauges/indicators/altitude_ft_pilot", None), "vs": ("sim/flightmodel/position/vh_ind_fpm", None),
    "ra": ("sim/cockpit2/gauges/indicators/radio_altimeter_height_ft_pilot", None),
    "baro": ("sim/cockpit2/gauges/actuators/barometer_setting_in_hg_pilot", None),
    "baro_std": ("AirbusFBW/BaroStdCapt", None), "baro_hpa": ("AirbusFBW/BaroUnitCapt", None),
    "fcu_spd": ("sim/cockpit/autopilot/airspeed", None), "fcu_alt": ("sim/cockpit/autopilot/altitude", None),
    "fcu_hdg": ("sim/cockpit/autopilot/heading_mag", None),
    "spd_managed": ("AirbusFBW/SPDmanaged", None), "hdg_managed": ("AirbusFBW/HDGmanaged", None),
    "fd1": ("AirbusFBW/FD1Engage", None), "fd_hor": ("AirbusFBW/FD1HorBar", None), "fd_ver": ("AirbusFBW/FD1VerBar", None),
    "fd_pitch": ("sim/cockpit2/autopilot/flight_director_pitch_deg", None), "fd_roll": ("sim/cockpit2/autopilot/flight_director_roll_deg", None),
    "ls": ("AirbusFBW/ILSonCapt", None),
    "loc_dev": ("sim/cockpit2/radios/indicators/nav1_hdef_dots_pilot", None), "gs_dev": ("sim/cockpit2/radios/indicators/nav1_vdef_dots_pilot", None),
    "loc_ok": ("sim/cockpit2/radios/indicators/nav1_display_horizontal", None), "gs_ok": ("sim/cockpit2/radios/indicators/nav1_display_vertical", None),
    "stick_p": ("sim/joystick/yoke_pitch_ratio", None), "stick_r": ("sim/joystick/yoke_roll_ratio", None),
    "on_ground": ("sim/flightmodel/failures/onground_any", None),
    "mw": ("sim/cockpit2/annunciators/master_warning", None), "mc": ("sim/cockpit2/annunciators/master_caution", None),
    "vls": ("toliss_airbus/pfdoutputs/general/VLS_value", None), "vmax": ("toliss_airbus/pfdoutputs/general/VMAX_value", None),
    "vsw": ("toliss_airbus/pfdoutputs/general/VSW_value", None), "vr": ("toliss_airbus/pfdoutputs/general/VR_value", None),
    "vs_speed": ("toliss_airbus/pfdoutputs/general/VS_value", None), "v1": ("toliss_airbus/pfdoutputs/general/V1_value", None),
    "flap_lever": ("sim/cockpit2/controls/flap_ratio", None), "flaps": ("sim/flightmodel2/controls/flap1_deploy_ratio", None),
    "slats": ("sim/flightmodel2/controls/slat1_deploy_ratio", None),
    "fob": ("sim/flightmodel/weight/m_fuel_total", None), "gw": ("sim/flightmodel/weight/m_total", None),
    "engines": ("sim/aircraft/engine/acf_num_engines", None),
    "athr": ("AirbusFBW/ATHRmode", None), "ap1": ("AirbusFBW/AP1Engage", None), "ap2": ("AirbusFBW/AP2Engage", None),
    # lower ECAM (SD)
    "tat": ("sim/weather/aircraft/temperature_leadingedge_deg_c", None), "sat": ("sim/weather/aircraft/temperature_ambient_deg_c", None),
    "zulu": ("sim/time/zulu_time_sec", None),
    "cab_alt": ("sim/cockpit2/pressurization/indicators/cabin_altitude_ft", None),
    "cab_vs": ("sim/cockpit2/pressurization/indicators/cabin_vvi_fpm", None),
    "dp": ("sim/cockpit2/pressurization/indicators/pressure_diffential_psi", None),
    "ail_l": ("sim/flightmodel/controls/lail1def", None), "ail_r": ("sim/flightmodel/controls/rail1def", None),
    "elev_l": ("sim/flightmodel/controls/hstab1_elv1def", None), "elev_r": ("sim/flightmodel/controls/hstab2_elv1def", None),
    "rud": ("sim/flightmodel/controls/vstab1_rud1def", None), "pitch_trim": ("sim/flightmodel/controls/elv_trim", None),
    "rud_trim": ("sim/cockpit2/controls/rudder_trim", None), "spd_brk": ("sim/flightmodel2/controls/speedbrake_ratio", None),
    "abrk_lo": ("AirbusFBW/AutoBrkLo", None), "abrk_med": ("AirbusFBW/AutoBrkMed", None), "abrk_max": ("AirbusFBW/AutoBrkMax", None),
    "park_brake": ("sim/cockpit2/controls/parking_brake_ratio", None), "gear_lever": ("sim/cockpit2/controls/gear_handle_down", None),
    "sd_page": ("AirbusFBW/SDPage", None), "apu_n": ("sim/cockpit2/electrical/APU_N1_percent", None),
    "fuel_used": ("sim/cockpit2/fuel/fuel_totalizer_sum_kg", None), "fuel_init": ("sim/cockpit2/fuel/fuel_totalizer_init_kg", None),
    "ldg_elev": ("AirbusFBW/LandElev", None),
}
# Other arrays for the lower ECAM: key -> (dataref, length)
SD_ARRAYS = {"tanks": ("sim/flightmodel/weight/m_fuel", 9), "gear_dep": ("sim/flightmodel2/gear/deploy_ratio", 3),
             "brk_t": ("AirbusFBW/BrakeTemperatureArray", 4)}
ENGINE_ARRAYS = {"n1": "sim/cockpit2/engine/indicators/N1_percent", "n2": "sim/cockpit2/engine/indicators/N2_percent",
                 "egt": "sim/cockpit2/engine/indicators/EGT_deg_C", "ff": "sim/cockpit2/engine/indicators/fuel_flow_kg_sec",
                 "running": "sim/flightmodel/engine/ENGN_running", "lever": "AirbusFBW/throttle_input",
                 "oil_p": "sim/cockpit2/engine/indicators/oil_pressure_psi", "oil_t": "sim/cockpit2/engine/indicators/oil_temperature_deg_C",
                 "oil_q": "sim/cockpit2/engine/indicators/oil_quantity_ratio"}
# Coloured text displays: (key, dataref prefix, lines, colours, suffix)
FMA_REFS = [(1, "w"), (1, "g"), (1, "b"), (1, "m"), (1, "a"), (2, "w"), (2, "g"), (2, "b"), (2, "m"), (2, "a"),
            (3, "w"), (3, "g"), (3, "b"), (3, "m"), (3, "a")]


def merge_coloured(parts, width):
    """parts: list of (bytes, colour). Returns (text, colours) with each character in its own colour."""
    chars, cols = [" "] * width, ["w"] * width
    for data, colour in parts:
        for i, b in enumerate(data[:width]):
            if b and b != 0x20:
                ch = chr(b)
                chars[i] = "\u00b0" if ch == "`" else ch
                cols[i] = colour
    return "".join(chars).rstrip(), "".join(cols)


# ===== Ops Centre: an airline operations station on the Hoppie ACARS network =====
HOPPIE_URL = "https://www.hoppie.nl/acars/system/connect.html"
HOPPIE_POLL_MIN_S, HOPPIE_POLL_MAX_S = 45, 75      # Hoppie asks clients to poll at a random 45-75 s interval
CALLSIGN_RE = re.compile(r"^[A-Z0-9]{2,8}$")


def hoppie_parse(reply):
    """Parse 'ok {FROM type {packet}} {FROM type {packet}}' (packets may contain nested braces)."""
    reply = (reply or "").strip()
    if not reply.lower().startswith("ok"):
        raise ValueError(reply[5:].strip(" {}") if reply.lower().startswith("error") else (reply or "empty reply"))
    msgs, i, n = [], 2, len(reply)
    while i < n:
        if reply[i] != "{":
            i += 1
            continue
        depth, j = 0, i
        while j < n:
            if reply[j] == "{":
                depth += 1
            elif reply[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        inner = reply[i + 1:j].strip()
        m = re.match(r"(\S+)\s+(\S+)\s+\{(.*)\}\s*$", inner, re.S)
        if m:
            msgs.append({"from": m.group(1).upper(), "type": m.group(2).lower(), "text": m.group(3)})
        elif inner:
            msgs.append({"from": "", "type": "info", "text": inner})
        i = j + 1
    return msgs


OPS_AUTO_KEYS = ["release", "wx", "notams", "prelim", "final", "todata", "atis", "oooi", "depreport", "eta", "fuel",
                 "enroute_wx", "arr_atis", "stand", "ldgdata", "blockin", "exp_rwy"]
OPS_AUTO_OFF_BY_DEFAULT = {"exp_rwy"}      # optional: expected runway from the wind when there is no ATIS
# ICAO aerodrome reference code (wingspan) of the ToLiss family
OPS_SIZE_CODE = {"A318": "C", "A319": "C", "A320": "C", "A20N": "C", "A321": "C", "A21N": "C",
                 "A332": "E", "A333": "E", "A338": "E", "A339": "E", "A342": "E", "A343": "E", "A345": "E", "A346": "E"}


def parse_stands(path, pos):
    """Stands of one airport (apt.dat 1300 lines with their 1301 details: size code, operation, airlines)."""
    stands = []
    with open(path, 'rb') as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            mm.seek(pos)
            mm.readline()
            while True:
                raw = mm.readline()
                if not raw:
                    break
                parts = raw.decode('utf-8', errors='ignore').split()
                if not parts:
                    continue
                code = parts[0]
                if code in ('1', '16', '17', '99'):
                    break
                try:
                    if code == '1300' and len(parts) >= 6:
                        stands.append({"name": " ".join(parts[6:]) or "STAND", "lat": float(parts[1]), "lon": float(parts[2]),
                                       "hdg": float(parts[3]), "kind": parts[4].lower(), "equip": parts[5].lower(),
                                       "size": "", "op": "", "airlines": []})
                    elif code == '1301' and len(parts) >= 3 and stands:
                        st = stands[-1]
                        st["size"] = parts[1].upper()
                        st["op"] = parts[2].lower()
                        if len(parts) >= 4:
                            st["airlines"] = [a.strip().upper() for a in " ".join(parts[3:]).replace(" ", ",").split(",") if a.strip()]
                except Exception:
                    continue
        finally:
            mm.close()
    return stands


def parse_runway_ends(path, pos):
    """Runway ends of one airport: [(name, true heading)] from apt.dat 100 lines."""
    ends = []
    with open(path, 'rb') as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            mm.seek(pos)
            mm.readline()
            while True:
                raw = mm.readline()
                if not raw:
                    break
                parts = raw.decode('utf-8', errors='ignore').split()
                if not parts:
                    continue
                if parts[0] in ('1', '16', '17', '99'):
                    break
                if parts[0] == '100' and len(parts) >= 20:
                    n1, la1, lo1, n2, la2, lo2 = parts[8], float(parts[9]), float(parts[10]), parts[17], float(parts[18]), float(parts[19])
                    def brg(a1, b1, a2, b2):
                        p1, p2, dl = math.radians(a1), math.radians(a2), math.radians(b2 - b1)
                        return (math.degrees(math.atan2(math.sin(dl) * math.cos(p2), math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl))) + 360) % 360
                    ends += [(n1, brg(la1, lo1, la2, lo2)), (n2, brg(la2, lo2, la1, lo1))]
        finally:
            mm.close()
    return ends


# stands never assigned to an airliner, by name (many sceneries don't mark their operation type)
STAND_NAME_EXCLUDE = re.compile(r"\b(CARGO|FREIGHT|FRT|CGO|MAINT\w*|MRO|HANGAR|HGR|GA|GEN(?:ERAL)?\s*AV\w*|HELI\w*|"
                                r"DE-?ICE\w*|DEICING|CATERING|FIRE|POLICE|MIL\w*|RFDS|AIR\s*AMBULANCE|BIZ\s*JET|FBO|EXEC\w*)\b", re.I)


def stand_key(name):
    """A stand name without words like GATE/STAND/BAY, e.g. 'Gate 14' -> '14', 'Stand A3' -> 'A3'."""
    n = re.sub(r"\b(GATE|STAND|BAY|PARKING|RAMP|POSITION|POS)\b", " ", str(name).upper())
    return re.sub(r"[\s_]+", "", n).strip("-")


def stand_matches(name, pattern):
    """Patterns: exact ('14', 'A3'), ranges ('1-16', 'A1-A10') and prefixes ('T3*', '5*')."""
    k, p = stand_key(name), str(pattern).upper().replace(" ", "")
    if not p:
        return False
    if p.endswith("*"):
        return k.startswith(p[:-1])
    m = re.match(r"^([A-Z]*)(\d+)-([A-Z]*)(\d+)$", p)
    if m:
        pre = m.group(1) or m.group(3)
        km = re.match(r"^([A-Z]*)(\d+)([A-Z]?)$", k)
        return bool(km) and km.group(1) == pre and int(m.group(2)) <= int(km.group(2)) <= int(m.group(4))
    return k == stand_key(p)


def ops_is_domestic(a, b):
    """Same country, judged from the ICAO codes (single-letter regions: USA, Canada, Australia, China, Russia)."""
    a, b = (a or "").upper(), (b or "").upper()
    if len(a) < 2 or len(b) < 2:
        return False
    if a[0] == b[0] and a[0] in "KCYZU":
        return True
    return a[:2] == b[:2]


def stand_unsuitable(st, need, excl):
    """None if the stand suits an airliner of size code <need>; otherwise the reason."""
    if st["op"] in ("cargo", "general_aviation", "military"):
        return {"cargo": "freight stand", "general_aviation": "general aviation", "military": "military"}[st["op"]]
    if STAND_NAME_EXCLUDE.search(st["name"] or ""):
        word = STAND_NAME_EXCLUDE.search(st["name"]).group(1).upper()
        plain = [("FREIGHT", "freight"), ("FRT", "freight"), ("CGO", "cargo"), ("CARGO", "cargo"), ("MAINT", "maintenance"),
                 ("MRO", "maintenance"), ("HANGAR", "hangar"), ("HGR", "hangar"), ("GEN", "general aviation"), ("GA", "general aviation"),
                 ("HELI", "helicopters"), ("DE", "de-icing"), ("CATERING", "catering"), ("FIRE", "fire service"), ("POLICE", "police"),
                 ("MIL", "military"), ("RFDS", "flying doctor"), ("AIR", "air ambulance"), ("BIZ", "business jets"), ("FBO", "business aviation"),
                 ("EXEC", "business aviation")]
        return "name suggests " + next((t for k, t in plain if word.startswith(k)), word.lower())
    if any(stand_matches(st["name"], p) for p in excl or []):
        return "excluded at this airport"
    if st["size"] and st["size"] < need:
        return f"too small (code {st['size']})"
    if not st["size"] and "jets" not in st["equip"] and "heavy" not in st["equip"] and st["equip"] not in ("", "all"):
        return "not for jets"
    if st["kind"] not in ("gate", "tie_down", "tie-down", "misc"):
        return st["kind"].replace("_", " ")
    return None


def choose_stand(stands, acft_icao, airline, seed="", rules=None, domestic=True, avoid=None):
    """Pick a stand suited to the aircraft and airline.
    rules (per airport, from stand_rules.json): {"exclude": [patterns],
        "airlines": {"QFA": {"domestic": [patterns], "international": [patterns], "any": [patterns]}},
        "learned": {"QFA": {"14": 3}}}"""
    import random as _r
    rules = rules or {}
    need = OPS_SIZE_CODE.get((acft_icao or "").upper(), "C")
    airline = (airline or "").upper()
    excl = rules.get("exclude", []) or []
    arule = (rules.get("airlines", {}) or {}).get(airline, {}) or {}
    rule_pats = (arule.get("domestic" if domestic else "international", []) or []) + (arule.get("any", []) or [])
    learned = (rules.get("learned", {}) or {}).get(airline, {}) or {}
    avoid = {stand_key(a) for a in (avoid or [])}
    cands = [s for s in stands if stand_unsuitable(s, need, excl) is None and stand_key(s["name"]) not in avoid]
    if not cands:
        return None
    def score(st):
        if rule_pats and any(stand_matches(st["name"], p) for p in rule_pats):
            tier = 0                                   # the airline's rule for this kind of flight
        elif learned.get(stand_key(st["name"])):
            tier = 1                                   # where this airline has parked before
        elif airline and airline in st["airlines"]:
            tier = 2                                   # the scenery lists the airline
        elif not st["airlines"]:
            tier = 3                                   # unrestricted
        else:
            tier = 4                                   # another airline's stand: last resort
        return (tier, 0 if st["kind"] == "gate" else 1, 0 if st["size"] == need else (1 if st["size"] else 2))
    best = min(score(c) for c in cands)
    top = sorted([c for c in cands if score(c) == best], key=lambda c: c["name"])
    if best[0] == 1:
        # prefer the stands used most often
        most = max(learned.get(stand_key(c["name"]), 0) for c in top)
        top = [c for c in top if learned.get(stand_key(c["name"]), 0) == most]
    return _r.Random(seed).choice(top)


OPS_WIDTH = 24          # MCDU line width


def _ops_g(d, *path, default=""):
    for k in path:
        if isinstance(d, dict):
            d = d.get(k)
        else:
            return default
    return default if d in (None, {}, []) else d


def _ops_num(v, default=0):
    try:
        return int(round(float(v)))
    except Exception:
        return default


def ops_wrap(text, width=OPS_WIDTH):
    """Wrap free text (METAR, route, NOTAM) to MCDU-width lines."""
    import textwrap
    out = []
    for para in str(text or "").upper().splitlines():
        out += textwrap.wrap(para, width) or [""]
    return out


def ops_hhmm(epoch):
    try:
        return time.strftime("%H%MZ", time.gmtime(int(float(epoch))))
    except Exception:
        return "----Z"


class OpsCentre:
    def __init__(self, plugin):
        self.plugin = plugin
        cfg = plugin.config if isinstance(getattr(plugin, 'config', None), dict) else {}
        self.logon = cfg.get("ops_logon", "")
        self.ops_callsign = cfg.get("ops_callsign", "")
        self.aircraft = cfg.get("ops_aircraft", "")
        self.enabled = bool(cfg.get("ops_enabled", False))
        self.log = []
        self.lock = threading.Lock()
        self.last_poll = 0.0
        self.next_poll = 0.0
        self.aircraft_online = None
        self.error = ""
        self.unread = 0
        # the flight being dispatched
        saved_auto = cfg.get("ops_auto", {}) if isinstance(cfg.get("ops_auto"), dict) else {}
        self.auto = {k: bool(saved_auto.get(k, k not in OPS_AUTO_OFF_BY_DEFAULT)) for k in OPS_AUTO_KEYS}
        self.ofp = None
        self.ofp_user = cfg.get("simbrief_user", "")
        self.flight = self._new_flight(None)
        self.queue = []
        self.next_send = 0.0
        self._last_auto = 0.0
        self._load_log()
        threading.Thread(target=self._loop, daemon=True).start()

    # --- storage ---
    def _log_path(self):
        d = os.path.join(self.plugin.plugin_dir, "ops")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "messages.json")

    def _load_log(self):
        try:
            with open(self._log_path(), 'r', encoding='utf-8') as f:
                self.log = json.load(f)[-500:]
        except Exception:
            self.log = []

    def _save_log(self):
        try:
            tmp = self._log_path() + ".tmp"
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.log[-500:], f)
            os.replace(tmp, self._log_path())
        except Exception as e:
            XPLMDebugString(f"ToLiss EFB: ops log save failed: {e}\n")

    def save_config(self):
        try:
            self.plugin.config.update({"ops_logon": self.logon, "ops_callsign": self.ops_callsign,
                                       "ops_aircraft": self.aircraft, "ops_enabled": self.enabled,
                                       "ops_auto": self.auto, "simbrief_user": self.ofp_user})
            self.plugin.save_config(self.plugin.config)
        except Exception:
            pass

    def configured(self):
        return bool(self.logon and CALLSIGN_RE.match(self.ops_callsign or "") and CALLSIGN_RE.match(self.aircraft or ""))

    # --- network ---
    def _request(self, to, mtype, packet="", frm=None):
        data = urllib.parse.urlencode({"logon": self.logon, "from": frm or self.ops_callsign, "to": to,
                                       "type": mtype, "packet": packet}).encode("utf-8")
        req = urllib.request.Request(HOPPIE_URL, data=data, headers={'User-Agent': f'ToLissEFB/{EFB_VERSION}'})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode("utf-8", errors="replace")

    def _add(self, direction, frm, to, mtype, text, status="ok"):
        entry = {"id": int(time.time() * 1000) + len(self.log), "t": int(time.time()), "dir": direction,
                 "from": frm, "to": to, "type": mtype, "text": text, "status": status}
        with self.lock:
            self.log.append(entry)
            self.log = self.log[-500:]
            if direction == "in":
                self.unread += 1
        self._save_log()
        return entry

    def poll(self):
        if not (self.enabled and self.configured()):
            return
        try:
            msgs = hoppie_parse(self._request("SERVER", "poll"))
            for m in msgs:
                if m["type"] in ("telex", "cpdlc", "progress", "inforeq", "position"):
                    self._add("in", m["from"], self.ops_callsign, m["type"], m["text"])
                    if m["from"] == self.aircraft and m["type"] == "telex":
                        try:
                            self._crew_message(m["text"])
                        except Exception as e:
                            XPLMDebugString(f"ToLiss EFB: ops reply failed: {e}\n")
            # is the aircraft's ATSU (ToLiss) logged on to Hoppie?
            online = hoppie_parse(self._request("SERVER", "ping", self.aircraft))
            names = " ".join(x["text"] for x in online) + " " + " ".join(x["from"] for x in online)
            self.aircraft_online = self.aircraft in names.upper().split()
            self.error = ""
        except Exception as e:
            self.error = f"Hoppie: {e}"
        self.last_poll = time.time()

    def send(self, text, to=None, as_crew=False, log_id=None):
        # as_crew: send from the aircraft's callsign to the ops centre (ToLiss's ATSU has no AOC free-text page).
        # Only sending under the aircraft's name; collecting its messages stays with ToLiss.
        frm = self.aircraft if as_crew else self.ops_callsign
        to = self.ops_callsign if as_crew else (to or self.aircraft or "").upper()
        text = re.sub(r"[{}]", "", str(text or "")).strip().upper()
        if not self.configured():
            return False, "Set the Hoppie logon code, ops callsign and aircraft callsign first."
        if not text:
            return False, "The message is empty."
        try:
            reply = self._request(to, "telex", text, frm=frm)
            hoppie_parse(reply)
            if as_crew:
                # it arrives in the ops mailbox: collect it shortly instead of waiting up to a minute
                self.next_poll = min(self.next_poll, time.time() + 4)
            elif log_id is not None:
                self._update_entry(log_id, "ok")       # a held message, now sent
            else:
                self._add("out", frm, to, "telex", text)
            return True, "sent"
        except Exception as e:
            if log_id is not None:
                self._update_entry(log_id, "failed")
            else:
                self._add("in" if as_crew else "out", frm, to, "telex", text, status="failed")
            return False, f"Not sent: {e}"

    def _update_entry(self, entry_id, status):
        with self.lock:
            for m in self.log:
                if m["id"] == entry_id:
                    m["status"] = status
                    m["t"] = int(time.time())
                    m["rev"] = m.get("rev", 0) + 1
        self._save_log()

    def _loop(self):
        import random as _r
        while True:
            try:
                now = time.time()
                if self.enabled and self.configured() and now >= self.next_poll:
                    self.poll()
                    self.next_poll = time.time() + _r.uniform(HOPPIE_POLL_MIN_S, HOPPIE_POLL_MAX_S)
                if self.enabled and self.configured():
                    if now - self._last_auto >= 5:
                        self._last_auto = now
                        self._automation()
                    self._send_queued()
                    if self.flight.get("final_mode"):
                        self._sample_touchdown()
            except Exception as e:
                self.error = str(e)
            time.sleep(0.2 if self.flight.get("final_mode") else 2)

    # ================= the flight and its pre-flight package =================
    @staticmethod
    def _new_flight(ofp_id):
        return {"ofp_id": ofp_id, "sent": {}, "queued": [], "ack": None, "atis_letter": None,
                "airborne": False, "beacon_seen": False, "released": False, "edno": 0,
                "oooi": {}, "eta": None, "eta_sent": None, "efob": None, "fuel_level": 0, "dest_obs": None,
                "next_eta": 0, "next_fuel": 0, "next_wx": 0, "dist_rem": None,
                "arrival_sent": False, "arr_atis_letter": None, "stand": None, "final_mode": False,
                "last_vs": None, "td_vs": None, "summary_sent": False}

    def set_ofp(self, ofp, user=""):
        """Called whenever the EFB loads a SimBrief plan."""
        if not isinstance(ofp, dict) or not ofp.get("origin"):
            return
        if user and user != self.ofp_user:
            self.ofp_user = user
            self.save_config()
        ofp_id = str(_ops_g(ofp, "params", "request_id") or _ops_g(ofp, "params", "time_generated"))
        self.ofp = ofp
        if ofp_id != self.flight.get("ofp_id"):
            self.flight = self._new_flight(ofp_id)
            self.queue = [q for q in self.queue if q[0] == "reply"]

    def _sim(self, names, timeout=1.5):
        rq = queue.Queue(maxsize=1)
        q_telem_req.put((names, rq))
        try:
            v = rq.get(timeout=timeout)
            v.pop("__missing__", None)
            return v
        except Exception:
            return {}

    def _queue(self, key, text):
        if key != "reply":
            self.flight["queued"].append(key)
        self.queue.append([key, text, None])

    def _send_queued(self):
        if not self.queue or time.time() < self.next_send:
            return
        # hold messages while Hoppie shows the aircraft's ATSU offline (e.g. ToLiss powered down after shutdown);
        # they are shown in the log as waiting and sent when the ATSU is back online
        if self.aircraft_online is False:
            item = self.queue[0]
            if item[2] is None:
                item[2] = self._add("out", self.ops_callsign, self.aircraft, "telex", item[1], status="waiting")["id"]
            self.flight["holding"] = True
            if self.next_poll - time.time() > 30:
                self.next_poll = time.time() + 30      # check sooner for the aircraft coming back online
            return
        self.flight["holding"] = False
        key, text, log_id = self.queue.pop(0)
        # every automatic message is wrapped to the MCDU's 24 characters per line
        text = "\n".join(l for line in str(text).split("\n") for l in (ops_wrap(line) if len(line) > OPS_WIDTH else [line]))
        ok, _ = self.send(text, log_id=log_id)
        if ok and key != "reply":
            self.flight["sent"][key] = int(time.time())
        if key in self.flight["queued"]:
            self.flight["queued"].remove(key)
        self.next_send = time.time() + 6          # a few seconds between messages, like a real uplink sequence

    def _cs(self):
        o = self.ofp or {}
        return (_ops_g(o, "atc", "callsign") or (_ops_g(o, "general", "icao_airline") + _ops_g(o, "general", "flight_number")) or self.aircraft).upper()

    def _route(self):
        o = self.ofp or {}
        return f'{_ops_g(o, "origin", "icao_code")}-{_ops_g(o, "destination", "icao_code")}'

    # ---------- message builders ----------
    def msg_release(self):
        o, f, w = self.ofp, self.ofp.get("fuel", {}), self.ofp.get("weights", {})
        lines = [f"{self._cs()} {self._route()}", "FLIGHT RELEASE",
                 f'STD {ops_hhmm(_ops_g(o, "times", "sched_out"))} STA {ops_hhmm(_ops_g(o, "times", "sched_in"))}',
                 f'{_ops_g(o, "aircraft", "icaocode")} {_ops_g(o, "aircraft", "reg")}'.strip(),
                 f'FL{_ops_num(_ops_g(o, "general", "initial_altitude")) // 100:03d} CI{_ops_g(o, "general", "costindex")}',
                 f'ALTN {_ops_g(o, "alternate", "icao_code") or "NIL"}', "ROUTE:"]
        lines += ops_wrap(_ops_g(o, "general", "route"))
        lines += ["FUEL (KG):",
                  f'BLOCK {_ops_num(f.get("plan_ramp"))}', f'TAXI {_ops_num(f.get("taxi"))}  TRIP {_ops_num(f.get("enroute_burn"))}',
                  f'CONT {_ops_num(f.get("contingency"))}  ALTN {_ops_num(f.get("alternate_burn"))}',
                  f'FINRES {_ops_num(f.get("reserve"))}  EXTRA {_ops_num(f.get("extra"))}',
                  f'EST ZFW {_ops_num(w.get("est_zfw"))}', f'EST TOW {_ops_num(w.get("est_tow"))}',
                  f'EST LAW {_ops_num(w.get("est_ldw"))}', f"DISPATCH {self.ops_callsign}"]
        return "\n".join(l for l in lines if l is not None)

    def _metar(self, icao):
        try:
            r = urllib.request.Request(f"https://metar.vatsim.net/{icao}", headers={'User-Agent': f'ToLissEFB/{EFB_VERSION}'})
            with urllib.request.urlopen(r, timeout=8) as resp:
                t = resp.read().decode("utf-8", errors="ignore").strip()
                return t or None
        except Exception:
            return None

    def _taf(self, icao):
        try:
            r = urllib.request.Request(f"https://aviationweather.gov/api/data/taf?ids={icao}&format=raw", headers={'User-Agent': f'ToLissEFB/{EFB_VERSION}'})
            with urllib.request.urlopen(r, timeout=8) as resp:
                t = resp.read().decode("utf-8", errors="ignore").strip()
                return t or None
        except Exception:
            return None

    def msg_wx(self, icao, use_ofp=True):
        o = self.ofp or {}
        role = {_ops_g(o, "origin", "icao_code"): "orig", _ops_g(o, "destination", "icao_code"): "dest", _ops_g(o, "alternate", "icao_code"): "altn"}.get(icao)
        metar = self._metar(icao) or (use_ofp and role and _ops_g(o, "weather", f"{role}_metar")) or None
        taf = self._taf(icao) or (use_ofp and role and _ops_g(o, "weather", f"{role}_taf")) or None
        lines = [f"WX {icao}"]
        lines += ["METAR"] + ops_wrap(metar) if metar else ["METAR NOT AVBL"]
        lines += ["TAF"] + ops_wrap(taf) if taf else ["TAF NOT AVBL"]
        return "\n".join(lines)

    def _notams(self, icao, role):
        o = self.ofp or {}
        items = _ops_g(o, role, "notam", default=[])
        if isinstance(items, dict):
            items = [items]
        if not items:
            allrec = _ops_g(o, "notams", "notamdrec", default=[])
            items = [n for n in (allrec if isinstance(allrec, list) else [allrec]) if isinstance(n, dict) and str(n.get("icao_id", "")).upper() == icao]
        out = []
        for n in items:
            if not isinstance(n, dict):
                continue
            txt = n.get("notam_text") or n.get("notam_raw") or n.get("notam_report") or ""
            nid = n.get("notam_id") or n.get("source_id") or ""
            if txt:
                out.append((str(nid), re.sub(r"\s+", " ", str(txt)).strip()))
        return out

    def msg_notams(self):
        o = self.ofp or {}
        msgs, nil = [], []
        for role in ("origin", "destination", "alternate"):
            icao = _ops_g(o, role, "icao_code")
            if not icao:
                continue
            ns = self._notams(icao, role)
            if not ns:
                nil.append(icao)
                continue
            lines = [f"NOTAMS {icao} ({len(ns)})"]
            for nid, txt in ns[:6]:
                lines += ops_wrap(f"{nid} {txt[:220]}".strip()) + [""]
            if len(ns) > 6:
                lines.append(f"+{len(ns) - 6} MORE IN THE OFP")
            msgs.append("\n".join(lines).strip())
        if nil:
            msgs.append("NOTAMS\n" + " ".join(nil) + ": NIL IN OFP")
        return msgs

    def msg_prelim(self):
        o, w = self.ofp, self.ofp.get("weights", {})
        lines = ["PRELIMINARY LOADSHEET", f"{self._cs()} {self._route()}",
                 f'{_ops_g(o, "aircraft", "icaocode")} {_ops_g(o, "aircraft", "reg")}'.strip(),
                 f'PAX {_ops_num(w.get("pax_count"))}  BAGS {_ops_num(w.get("bag_count"))}',
                 f'CARGO {_ops_num(w.get("cargo"))}',
                 f'EST ZFW {_ops_num(w.get("est_zfw"))}', f'  MAX {_ops_num(w.get("max_zfw"))}',
                 f'EST TOW {_ops_num(w.get("est_tow"))}', f'  MAX {_ops_num(w.get("max_tow"))}',
                 f'EST LAW {_ops_num(w.get("est_ldw"))}', f'  MAX {_ops_num(w.get("max_ldw"))}',
                 "FINAL LDSHT TO FOLLOW"]
        return "\n".join(lines)

    def _actual_weights(self):
        """ZFW from the sim (gross weight minus fuel), take-off fuel = fuel on board minus planned taxi fuel."""
        w, f = self.ofp.get("weights", {}), self.ofp.get("fuel", {})
        v = self._sim(["sim/flightmodel/weight/m_total", "sim/flightmodel/weight/m_fuel_total",
                       "AirbusFBW/NoPax", "AirbusFBW/FwdCargo", "AirbusFBW/AftCargo"])
        total, fuel = float(v.get("sim/flightmodel/weight/m_total", 0) or 0), float(v.get("sim/flightmodel/weight/m_fuel_total", 0) or 0)
        zfw = _ops_num(total - fuel) if total > 0 else _ops_num(w.get("est_zfw"))
        taxi, trip = _ops_num(f.get("taxi")), _ops_num(f.get("enroute_burn"))
        tof = max(0, _ops_num(fuel) - taxi) if fuel > 0 else _ops_num(f.get("plan_takeoff"))
        return v, zfw, tof, zfw + tof, zfw + tof - trip, trip

    def msg_final(self):
        o, w, f = self.ofp, self.ofp.get("weights", {}), self.ofp.get("fuel", {})
        v, zfw, tof, tow, law, trip = self._actual_weights()
        mzfw, mtow, mlaw = _ops_num(w.get("max_zfw")), _ops_num(w.get("max_tow")), _ops_num(w.get("max_ldw"))
        margins = [m for m in (mzfw - zfw if mzfw else None, mtow - tow if mtow else None, mlaw - law if mlaw else None) if m is not None]
        under = min(margins) if margins else None
        pax = _ops_num(v.get("AirbusFBW/NoPax"), _ops_num(w.get("pax_count")))
        fwd, aft = _ops_num(v.get("AirbusFBW/FwdCargo")), _ops_num(v.get("AirbusFBW/AftCargo"))
        self.flight["edno"] += 1
        lines = [f'LOADSHEET FINAL {time.strftime("%H%MZ", time.gmtime())}', f'EDNO {self.flight["edno"]}',
                 f"{self._cs()} {self._route()}",
                 f'{_ops_g(o, "aircraft", "icaocode")} {_ops_g(o, "aircraft", "reg")}'.strip(),
                 f"PAX {pax}", f"HOLD FWD {fwd}  AFT {aft}",
                 f"ZFW {zfw}  MAX {mzfw}", f"TOF {tof}",
                 f"TOW {tow}  MAX {mtow}", f"TIF {trip}",
                 f"LAW {law}  MAX {mlaw}"]
        if under is not None:
            lines.append(f"UNDERLOAD {under}" if under >= 0 else f"OVERWEIGHT {-under} CHECK")
        lines += ["LMC NIL", "PLEASE ACKNOWLEDGE"]
        self.flight["ack"] = None
        return "\n".join(lines)

    def msg_todata(self):
        o = self.ofp or {}
        to = _ops_g(o, "tlr", "takeoff", default={})
        rwys = to.get("runway") if isinstance(to, dict) else None
        rwys = rwys if isinstance(rwys, list) else ([rwys] if isinstance(rwys, dict) else [])
        plan = str(_ops_g(o, "origin", "plan_rwy") or (_ops_g(to, "conditions", "planned_runway"))).upper()
        r = next((x for x in rwys if str(x.get("identifier", "")).upper() == plan), rwys[0] if rwys else None)
        c = to.get("conditions", {}) if isinstance(to, dict) else {}
        if not r:
            return f"TAKEOFF DATA\n{self._cs()} {_ops_g(o, 'origin', 'icao_code')}\nNOT AVBL IN THE OFP"
        flex = r.get("flex_temperature")
        thrust = f"FLEX {flex}" if flex not in (None, "", {}) and str(r.get("thrust_setting", "")).upper() != "TOGA" else "TOGA"
        lines = ["TAKEOFF DATA", f'{self._cs()} {_ops_g(o, "origin", "icao_code")} RWY {r.get("identifier", "")}',
                 f'OAT {c.get("temperature", "--")} QNH {c.get("altimeter", "--")}',
                 f'WIND {str(c.get("wind_direction", "---")).zfill(3)}/{c.get("wind_speed", "--")}',
                 f'TOW {_ops_num(c.get("planned_weight"))}',
                 f'{str(r.get("flap_setting", "")).replace("CONF ", "CONF ")}  {thrust}'.strip(),
                 f'V1 {r.get("speeds_v1", "---")} VR {r.get("speeds_vr", "---")} V2 {r.get("speeds_v2", "---")}',
                 f'PACKS {r.get("bleed_setting", "--")}  A/ICE {r.get("anti_ice_setting", "--")}']
        cautions = []
        try:
            planned_w = _ops_num(c.get("planned_weight"))
            _, _, _, tow, _, _ = self._actual_weights()
            if planned_w and tow > planned_w + 100:
                cautions.append(f"ACTUAL TOW {tow} ABOVE PLANNED {planned_w}")
        except Exception:
            pass
        orig = _ops_g(o, "origin", "icao_code")
        letter, atis = self._atis(orig) if orig else (None, None)
        if atis:
            dep = re.findall(r"(?:RWY|RUNWAY)S?\s*(\d{2}[LRC]?)(?=[^.]*?(?:DEP|DEPARTURE))", atis.upper()) or re.findall(r"(?:RWY|RUNWAY)S?\s*(\d{2}[LRC]?)", atis.upper())
            if dep and str(r.get("identifier", "")).upper() not in [d.upper() for d in dep]:
                cautions.append(f"ATIS {letter or ''} RWY {'/'.join(dict.fromkeys(dep))} NOT PLANNED RWY {r.get('identifier', '')}".replace("  ", " "))
        metar = self._metar(orig) if orig else None
        mt = re.search(r"\s(M?\d{2})/(M?\d{2})\s", f" {metar} ") if metar else None
        if mt and str(c.get("temperature", "")).lstrip("-").isdigit():
            now_t = -int(mt.group(1)[1:]) if mt.group(1).startswith("M") else int(mt.group(1))
            if now_t > int(c.get("temperature")) + 3:
                cautions.append(f"OAT NOW {now_t} ABOVE PLANNED {c.get('temperature')}")
        if cautions:
            lines += ["CAUTION:"] + [x for msg in cautions for x in ops_wrap(msg)] + ["RECALCULATE BEFORE", "DEPARTURE"]
            self.flight["todata_caution"] = True
        return "\n".join(lines)

    def _atis(self, icao, arrival=False):
        data = (vatsim_cache.get("data") or {}) if isinstance(vatsim_cache, dict) else {}
        found = [a for a in data.get("atis", []) if str(a.get("callsign", "")).upper().startswith(icao) and "ATIS" in str(a.get("callsign", "")).upper()]
        if not found:
            return None, None
        pref = "_A_" if arrival else "_D_"
        found.sort(key=lambda a: 0 if pref in a.get("callsign", "") else 1)     # arrival or departure ATIS first
        a = found[0]
        return (a.get("atis_code") or "").upper() or None, " ".join(a.get("text_atis") or [])

    def _expected_runway(self, icao, metar):
        """The runway end most into the METAR wind (an estimate, not an ATIS)."""
        m = re.search(r"\b(\d{3}|VRB)(\d{2,3})(?:G\d{2,3})?KT\b", metar or "")
        if not m:
            return None
        if m.group(1) == "VRB" or int(m.group(2)) < 3:
            return "WIND CALM OR VRB"
        loc = getattr(self.plugin, 'apt_index', {}).get(icao)
        if not loc:
            return None
        ends = parse_runway_ends(loc[0], loc[1])
        if not ends:
            return None
        wdir = int(m.group(1))
        best = max(ends, key=lambda e: math.cos(math.radians(wdir - e[1])))
        return f"RWY {best[0]} (INTO WIND)"

    def msg_no_atis(self, icao, arrival=False):
        metar = self._metar(icao)
        if not metar and self.ofp:
            role = "dest" if arrival else "orig"
            metar = _ops_g(self.ofp, "weather", f"{role}_metar") or None
        lines = [f"{icao} D-ATIS NOT AVBL", "LATEST METAR:"] + (ops_wrap(metar) if metar else ["NOT AVBL"])
        if self.auto.get("exp_rwy") and metar:
            rwy = self._expected_runway(icao, metar)
            if rwy:
                lines += ["EXPECTED, ESTIMATE ONLY:"] + ops_wrap(rwy)
        return "\n".join(lines)

    def msg_atis(self, icao, arrival=False):
        letter, text = self._atis(icao, arrival)
        if not letter and not text:
            return None, None
        return letter, "\n".join([f"{icao} ATIS {letter or ''}".strip()] + ops_wrap(text))

    # ---------- automation ----------
    def _automation(self):
        if not self.ofp:
            return
        fl = self.flight
        v = self._sim(["sim/flightmodel/failures/onground_any", "sim/cockpit2/switches/beacon_on",
                       "sim/flightmodel/position/latitude", "sim/flightmodel/position/longitude",
                       "sim/flightmodel/position/groundspeed", "sim/cockpit2/controls/parking_brake_ratio",
                       "sim/flightmodel/engine/ENGN_running", "sim/flightmodel/weight/m_fuel_total",
                       "sim/cockpit2/engine/indicators/fuel_flow_kg_sec", "sim/flightmodel/position/y_agl",
                       "sim/flightmodel/weight/m_total"])
        if not v:
            return
        on_ground = float(v.get("sim/flightmodel/failures/onground_any", 1) or 0) >= 1
        try:
            self._inflight(v)
        except Exception as e:
            XPLMDebugString(f"ToLiss EFB: ops in-flight check failed: {e}\n")
        if not on_ground:
            fl["airborne"] = True
        if fl["airborne"]:
            return                                  # pre-flight items only until takeoff
        orig = _ops_g(self.ofp, "origin", "icao_code")
        if not fl["released"]:
            def wx_msgs():
                return [self.msg_wx(i) for i in dict.fromkeys(x for x in (orig, _ops_g(self.ofp, "destination", "icao_code"), _ops_g(self.ofp, "alternate", "icao_code")) if x)]
            done = fl.setdefault("built", set())
            for key, build in (("release", lambda: [self.msg_release()]), ("wx", wx_msgs),
                               ("notams", self.msg_notams), ("prelim", lambda: [self.msg_prelim()])):
                if key in done or not self.auto[key]:
                    continue
                try:
                    for m in build():
                        self._queue(key, m)
                    done.add(key)
                except Exception as e:
                    XPLMDebugString(f"ToLiss EFB: ops could not prepare {key}: {e}\n")
            fl["released"] = all(k in done or not self.auto[k] for k in ("release", "wx", "notams", "prelim"))
        beacon = float(v.get("sim/cockpit2/switches/beacon_on", 0) or 0) >= 1
        if beacon and not fl["beacon_seen"]:
            fl["beacon_seen"] = True
            for key, build in (("final", self.msg_final), ("todata", self.msg_todata)):
                if self.auto[key] and key not in fl["sent"]:
                    try:
                        self._queue(key, build())
                    except Exception as e:
                        XPLMDebugString(f"ToLiss EFB: ops could not prepare {key}: {e}\n")
        if self.auto["atis"] and orig:
            letter, text = self.msg_atis(orig)
            if letter and letter != fl["atis_letter"]:
                fl["atis_letter"] = letter
                self._queue("atis", text)
            elif not letter and fl["released"] and not fl.get("no_atis_sent") and not fl["atis_letter"]:
                fl["no_atis_sent"] = True
                try:
                    self._queue("atis", self.msg_no_atis(orig))
                except Exception as e:
                    XPLMDebugString(f"ToLiss EFB: ops could not prepare the no-ATIS message: {e}\n")

    # ---------- in flight ----------
    def _event(self, text):
        """An OOOI or other flight event, recorded in the log (not sent over Hoppie)."""
        self._add("in", self.aircraft, self.ops_callsign, "event", text)

    @staticmethod
    def _gc_nm(lat1, lon1, lat2, lon2):
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dlat, dlon = p2 - p1, math.radians(lon2 - lon1)
        a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
        return 3440.065 * 2 * math.asin(min(1.0, math.sqrt(a)))

    def _remaining_nm(self, lat, lon):
        """Distance to go along the SimBrief route (great circle to the destination if there is no navlog)."""
        o = self.ofp or {}
        fixes = _ops_g(o, "navlog", "fix", default=[])
        fixes = fixes if isinstance(fixes, list) else [fixes]
        pts = []
        for f in fixes:
            try:
                pts.append((float(f["pos_lat"]), float(f["pos_long"]), float(f.get("distance") or 0)))
            except Exception:
                pass
        if len(pts) >= 2:
            near = min(range(len(pts)), key=lambda i: self._gc_nm(lat, lon, pts[i][0], pts[i][1]))
            nxt = min(near + 1, len(pts) - 1)
            if near + 1 < len(pts):
                # past the nearest fix if it is closer to the next fix than we are
                a, b = pts[near], pts[nxt]
                if self._gc_nm(lat, lon, b[0], b[1]) > self._gc_nm(a[0], a[1], b[0], b[1]):
                    nxt = near
            rem = self._gc_nm(lat, lon, pts[nxt][0], pts[nxt][1]) + sum(p[2] for p in pts[nxt + 1:])
            return rem
        try:
            return self._gc_nm(lat, lon, float(_ops_g(o, "destination", "pos_lat")), float(_ops_g(o, "destination", "pos_long")))
        except Exception:
            return None

    def _progress(self, v):
        """ETA (wheels on) and estimated fuel at destination from the current state."""
        lat, lon = float(v.get("sim/flightmodel/position/latitude", 0) or 0), float(v.get("sim/flightmodel/position/longitude", 0) or 0)
        gs = float(v.get("sim/flightmodel/position/groundspeed", 0) or 0) * 1.94384
        rem = self._remaining_nm(lat, lon)
        if rem is None or gs < 120:
            return None
        hours = rem / gs
        ff = v.get("sim/cockpit2/engine/indicators/fuel_flow_kg_sec") or []
        ff = sum(float(x) for x in ff) if isinstance(ff, list) else float(ff or 0)
        fob = float(v.get("sim/flightmodel/weight/m_fuel_total", 0) or 0)
        efob = fob - ff * 3600 * hours if ff > 0 else None
        return {"eta": int(time.time() + hours * 3600), "rem": int(rem), "efob": int(efob) if efob is not None else None}

    def _delay_text(self, eta):
        sched = _ops_num(_ops_g(self.ofp, "times", "sched_on"), 0)
        if not sched:
            return None
        mins = int(round((eta - sched) / 60))
        return "ON TIME" if abs(mins) <= 5 else (f"{mins} MIN LATE" if mins > 0 else f"{-mins} MIN EARLY")

    def _inflight(self, v):
        o, fl = self.ofp, self.flight
        now = time.time()
        on_ground = float(v.get("sim/flightmodel/failures/onground_any", 1) or 0) >= 1
        gs_kt = float(v.get("sim/flightmodel/position/groundspeed", 0) or 0) * 1.94384
        brake = float(v.get("sim/cockpit2/controls/parking_brake_ratio", 0) or 0)
        run = v.get("sim/flightmodel/engine/ENGN_running") or []
        engines_on = any(float(x) >= 1 for x in (run if isinstance(run, list) else [run]))
        x = fl["oooi"]
        dest = _ops_g(o, "destination", "icao_code")
        # ---- OOOI ----
        fob_now = float(v.get("sim/flightmodel/weight/m_fuel_total", 0) or 0)
        if "out" not in x and on_ground and brake < 0.5 and gs_kt > 3:
            x["out"] = int(now)
            fl["fob_out"] = fob_now
            if self.auto["oooi"]:
                self._event(f"OUT {ops_hhmm(now)}")
        if "off" not in x and not on_ground and gs_kt > 60:
            x["off"] = int(now)
            x.setdefault("out", int(now))
            if self.auto["oooi"]:
                self._event(f"OFF {ops_hhmm(now)}")
            planned_air = _ops_num(_ops_g(o, "times", "est_time_enroute"), 0)
            eta = int(now + planned_air) if planned_air else None
            fl["eta"] = fl["eta_sent"] = eta
            if self.auto["depreport"]:
                lines = [f"{self._cs()} DEP REPORT", f'OUT {ops_hhmm(x["out"])}  OFF {ops_hhmm(now)}']
                if eta:
                    lines.append(f"ETA {dest} {ops_hhmm(eta)}")
                    dly = self._delay_text(eta)
                    if dly:
                        lines.append(f'STA {ops_hhmm(_ops_g(o, "times", "sched_in"))} {dly}'.strip())
                lines.append("HAVE A GOOD FLIGHT")
                self._queue("depreport", "\n".join(lines))
            fl["next_eta"] = now + 600
            fl["next_fuel"] = now + 900
            fl["next_wx"] = now + 900
        if "off" in x and "on" not in x and on_ground and gs_kt > 30:
            x["on"] = int(now)
            if self.auto["oooi"]:
                self._event(f"ON {ops_hhmm(now)}")
        if "on" in x and "in" not in x and on_ground and gs_kt < 1 and brake >= 0.5:
            # IN = parking brake set on stand, as real ACARS reports it, while the ATSU still has power
            x["in"] = int(now)
            fl["fob_in"] = fob_now
            try:
                self._learn_stand(float(v.get("sim/flightmodel/position/latitude", 0) or 0), float(v.get("sim/flightmodel/position/longitude", 0) or 0))
            except Exception as e:
                XPLMDebugString(f"ToLiss EFB: could not note the stand: {e}\n")
            if self.auto["oooi"]:
                self._event(f"IN {ops_hhmm(now)}")
            if self.auto["blockin"] and not fl["summary_sent"]:
                fl["summary_sent"] = True
                self._queue("blockin", self.msg_blockin())
        # below 300 ft: sample vertical speed several times a second to catch the touchdown rate
        agl_ft = float(v.get("sim/flightmodel/position/y_agl", 9999) or 9999) * 3.28084
        if "off" in x and "on" not in x and not on_ground and agl_ft < 300:
            fl["final_mode"] = True
        if on_ground or "off" not in x or "on" in x:
            return
        # ---- ETA, fuel and destination weather in flight ----
        prog = None
        if now >= fl["next_eta"] or now >= fl["next_fuel"] or not fl["arrival_sent"] and now >= fl.get("next_arr", 0):
            fl["next_arr"] = now + 60
            prog = self._progress(v)
            if prog:
                fl["dist_rem"], fl["efob"] = prog["rem"], prog["efob"]
        if now >= fl["next_eta"]:
            fl["next_eta"] = now + 300
            if prog and self.auto["eta"]:
                fl["eta"] = prog["eta"]
                if fl["eta_sent"] is None or abs(prog["eta"] - fl["eta_sent"]) >= 600:
                    change = int(round((prog["eta"] - fl["eta_sent"]) / 60)) if fl["eta_sent"] else None
                    lines = ["ETA UPDATE", f"{dest} {ops_hhmm(prog['eta'])}" + (f" ({change:+d} MIN)" if change else ""),
                             f"{prog['rem']} NM TO GO"]
                    dly = self._delay_text(prog["eta"])
                    if dly:
                        lines.append(dly)
                    fl["eta_sent"] = prog["eta"]
                    self._queue("eta", "\n".join(lines))
        if now >= fl["next_fuel"]:
            fl["next_fuel"] = now + 600
            if prog and prog["efob"] is not None and self.auto["fuel"]:
                f = o.get("fuel", {})
                planned = _ops_num(f.get("plan_landing")) or (_ops_num(f.get("plan_takeoff")) - _ops_num(f.get("enroute_burn")))
                reserve = _ops_num(f.get("alternate_burn")) + _ops_num(f.get("reserve"))
                level = 2 if reserve and prog["efob"] < reserve else (1 if planned and prog["efob"] < planned - 500 else 0)
                if level > fl["fuel_level"]:
                    fl["fuel_level"] = level
                    lines = ["FUEL CHECK", f"EFOB {dest} {prog['efob']} KG", f"PLANNED {planned} KG"]
                    lines += (["BELOW ALTN + FINAL RES", f"({reserve} KG)", "CONSIDER OPTIONS, ADVISE"] if level == 2
                              else ["BELOW PLAN, PLEASE", "MONITOR"])
                    self._queue("fuel", "\n".join(lines))
        # ---- arrival package, about 150 nm or 30 minutes out ----
        if prog and not fl["arrival_sent"] and (prog["rem"] <= 150 or prog["eta"] - now <= 1800):
            fl["arrival_sent"] = True
            self._arrival_package(v)
        if fl["arrival_sent"] and self.auto["arr_atis"] and dest:
            letter, text = self.msg_atis(dest, arrival=True)
            if letter and letter != fl["arr_atis_letter"]:
                fl["arr_atis_letter"] = letter
                self._queue("arr_atis", text)
        if now >= fl["next_wx"] and dest:
            fl["next_wx"] = now + 900
            if self.auto["enroute_wx"]:
                metar = self._metar(dest)
                obs = re.search(r"\b(\d{6})Z\b", metar or "")
                if metar and obs and obs.group(1) != fl["dest_obs"]:
                    first = fl["dest_obs"] is None
                    fl["dest_obs"] = obs.group(1)
                    lines = [f"WX UPDATE {dest}"] + ops_wrap(metar)
                    low = re.search(r"\s(0[0-7]\d\d|[0-7]\d\d)\s", f" {metar} ") or re.search(r"(BKN|OVC|VV)00[0-2]", metar)
                    if low:
                        lines += ["CAUTION: LOW VIS OR", "CEILING AT DESTINATION"]
                    if not first or low:
                        self._queue("enroute_wx", "\n".join(lines))

    # ---------- stand rules: exclusions, airline rules and stands learned from where you parked ----------
    def _rules_path(self):
        return os.path.join(self.plugin.plugin_dir, "stand_rules.json")

    def _load_rules(self):
        try:
            with open(self._rules_path(), 'r', encoding='utf-8') as f:
                r = json.load(f)
                return r if isinstance(r, dict) else {}
        except Exception:
            return {}

    def _save_rules(self, rules):
        try:
            tmp = self._rules_path() + ".tmp"
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(rules, f, indent=2, sort_keys=True)
            os.replace(tmp, self._rules_path())
        except Exception as e:
            XPLMDebugString(f"ToLiss EFB: could not save stand rules: {e}\n")

    def _learn_stand(self, lat, lon):
        """At IN: note the stand the aircraft is parked on (within 40 m) for this airline at this airport."""
        o = self.ofp or {}
        dest, airline = _ops_g(o, "destination", "icao_code"), _ops_g(o, "general", "icao_airline").upper()
        loc = getattr(self.plugin, 'apt_index', {}).get(dest) if dest else None
        if not (loc and airline and lat):
            return None
        try:
            stands = parse_stands(loc[0], loc[1])
        except Exception:
            return None
        near = min(stands, key=lambda st: self._gc_nm(lat, lon, st["lat"], st["lon"]), default=None)
        if not near or self._gc_nm(lat, lon, near["lat"], near["lon"]) * 1852 > 40:
            return None
        rules = self._load_rules()
        apt = rules.setdefault(dest, {})
        key = stand_key(near["name"])
        cnt = apt.setdefault("learned", {}).setdefault(airline, {})
        cnt[key] = cnt.get(key, 0) + 1
        self._save_rules(rules)
        self.flight["parked_stand"] = near["name"]
        return near["name"]

    def stand_list(self):
        """The destination's stands with details for the Change stand window."""
        o = self.ofp or {}
        dest, airline = _ops_g(o, "destination", "icao_code"), _ops_g(o, "general", "icao_airline").upper()
        loc = getattr(self.plugin, 'apt_index', {}).get(dest) if dest else None
        if not loc:
            return {"dest": dest, "stands": [], "error": f"No scenery stands found for {dest or 'the destination'}."}
        domestic = ops_is_domestic(_ops_g(o, "origin", "icao_code"), dest)
        need = OPS_SIZE_CODE.get(_ops_g(o, "aircraft", "icaocode").upper(), "C")
        rules = self._load_rules().get(dest, {})
        excl = rules.get("exclude", []) or []
        arule = (rules.get("airlines", {}) or {}).get(airline, {}) or {}
        pats = (arule.get("domestic" if domestic else "international", []) or []) + (arule.get("any", []) or [])
        learned = (rules.get("learned", {}) or {}).get(airline, {}) or {}
        out = []
        for st in parse_stands(loc[0], loc[1]):
            k = stand_key(st["name"])
            out.append({"name": st["name"], "kind": st["kind"], "size": st["size"], "op": st["op"], "airlines": st["airlines"],
                        "lat": st["lat"], "lon": st["lon"], "reason": stand_unsuitable(st, need, excl),
                        "used": learned.get(k, 0), "rule": any(stand_matches(st["name"], p) for p in pats),
                        "excluded": any(stand_matches(st["name"], p) for p in excl)})
        cur = (self.flight.get("stand") or {}).get("name")
        return {"dest": dest, "airline": airline, "domestic": domestic, "size": need, "current": cur, "stands": out}

    def change_stand(self, mode, name=None, exclude_old=False, remember=False):
        """From the Change stand window: assign another automatically or a chosen stand."""
        o = self.ofp or {}
        dest, airline = _ops_g(o, "destination", "icao_code"), _ops_g(o, "general", "icao_airline").upper()
        loc = getattr(self.plugin, 'apt_index', {}).get(dest) if dest else None
        if not loc:
            return False, f"No scenery stands found for {dest or 'the destination'}."
        stands = parse_stands(loc[0], loc[1])
        old = (self.flight.get("stand") or {}).get("name")
        rules = self._load_rules()
        apt = rules.setdefault(dest, {})
        if exclude_old and old:
            ex = apt.setdefault("exclude", [])
            if stand_key(old) not in [stand_key(x) for x in ex]:
                ex.append(stand_key(old))
        domestic = ops_is_domestic(_ops_g(o, "origin", "icao_code"), dest)
        if mode == "choose":
            new = next((st for st in stands if st["name"] == name), None)
            if not new:
                return False, f"Stand {name} was not found in the scenery."
        else:
            new = choose_stand(stands, _ops_g(o, "aircraft", "icaocode"), airline, seed=str(time.time()),
                               rules=apt, domestic=domestic, avoid=[old] if old else None)
            if not new:
                self._save_rules(rules)
                return False, "No other suitable stand was found. Choose one yourself, with Show all stands if needed."
        if remember and airline and mode == "choose":
            lst = apt.setdefault("airlines", {}).setdefault(airline, {}).setdefault("domestic" if domestic else "international", [])
            if stand_key(new["name"]) not in [stand_key(x) for x in lst]:
                lst.append(stand_key(new["name"]))
        self._save_rules(rules)
        self.flight["stand"] = new
        notes = []
        if exclude_old and old:
            notes.append(f"stand {old} excluded at {dest}")
        if remember and mode == "choose" and airline:
            notes.append(f"stand {new['name']} saved for {airline} {'domestic' if domestic else 'international'} flights")
        if "in" in self.flight["oooi"]:
            msg = f"Stand {new['name']} noted (already parked, no message sent)."
        elif self.flight.get("arrival_sent") or "stand" in self.flight["sent"]:
            self._queue("stand", self.msg_stand(revised=True))
            self.next_send = min(self.next_send, time.time() + 1)
            msg = f"Revised stand assignment: {new['name']}, being sent."
        else:
            msg = f"Stand {new['name']} set: it is sent with the arrival package (or press Send now)."
        notes_txt = "; ".join(notes)
        return True, msg + (" " + notes_txt[:1].upper() + notes_txt[1:] + "." if notes else "")

    def reassign_stand(self):
        """The assigned stand is unsuitable: exclude it at this airport for good and assign another."""
        o = self.ofp or {}
        dest = _ops_g(o, "destination", "icao_code")
        cur = self.flight.get("stand")
        if cur and dest:
            rules = self._load_rules()
            ex = rules.setdefault(dest, {}).setdefault("exclude", [])
            key = stand_key(cur["name"])
            if key not in [stand_key(x) for x in ex]:
                ex.append(key)
            self._save_rules(rules)
        self.flight["stand"] = None
        return self.msg_stand(revised=True)

    # ---------- arrival ----------
    def _assign_stand(self):
        if self.flight.get("stand"):
            return self.flight["stand"]
        o = self.ofp or {}
        dest = _ops_g(o, "destination", "icao_code")
        loc = getattr(self.plugin, 'apt_index', {}).get(dest) if dest else None
        if not loc:
            return None
        try:
            st = choose_stand(parse_stands(loc[0], loc[1]), _ops_g(o, "aircraft", "icaocode"), _ops_g(o, "general", "icao_airline"),
                              seed=str(self.flight.get("ofp_id")), rules=self._load_rules().get(dest, {}),
                              domestic=ops_is_domestic(_ops_g(o, "origin", "icao_code"), dest))
        except Exception as e:
            XPLMDebugString(f"ToLiss EFB: ops stand lookup failed: {e}\n")
            st = None
        self.flight["stand"] = st
        return st

    def msg_stand(self, revised=False):
        o = self.ofp or {}
        dest = _ops_g(o, "destination", "icao_code")
        st = self._assign_stand()
        if not st:
            return f"STAND ASSIGNMENT {dest}\nNOT AVBL: NO SUITABLE\nSTAND IN THE SCENERY\nCONTACT GROUND"
        kind = {"gate": "CONTACT GATE", "tie_down": "REMOTE STAND", "tie-down": "REMOTE STAND"}.get(st["kind"], "STAND")
        lines = [f"{'REVISED ' if revised else ''}STAND ASSIGNMENT", dest, f"STAND {st['name']}".upper(), kind]
        if st["size"]:
            lines.append(f"CODE {st['size']}")
        lines.append("GROUND POWER AVBL")
        return "\n".join(lines)

    def msg_ldgdata(self, v=None):
        o = self.ofp or {}
        ld = _ops_g(o, "tlr", "landing", default={})
        dest = _ops_g(o, "destination", "icao_code")
        if not isinstance(ld, dict) or not ld:
            return f"LANDING DATA {dest}\nNOT AVBL IN THE OFP"
        c, rw = ld.get("conditions", {}) or {}, ld.get("runway", {}) or {}
        if isinstance(rw, list):
            plan = str(_ops_g(o, "destination", "plan_rwy")).upper()
            rw = next((x for x in rw if str(x.get("identifier", "")).upper() == plan), rw[0] if rw else {})
        dry, wet = ld.get("distance_dry", {}) or {}, ld.get("distance_wet", {}) or {}
        lda = _ops_num(rw.get("length_lda") or rw.get("length"))
        lines = ["LANDING DATA", f'{self._cs()} {dest} RWY {rw.get("identifier", "")}'.strip(),
                 f"LDA {lda} FT" if lda else "LDA ---",
                 f'WIND {str(c.get("wind_direction", "---")).zfill(3)}/{c.get("wind_speed", "--")}  OAT {c.get("temperature", "--")}',
                 f'LW {_ops_num(c.get("planned_weight"))}',
                 f'CONF {dry.get("flap_setting", "FULL")}  VREF {dry.get("speeds_vref", "---")}'.replace("CONF CONF", "CONF")]
        for label, d in (("DRY", dry), ("WET", wet)):
            if d:
                lines += [f'{label}: AUTOBRK {d.get("brake_setting", "--")}', f' RQD {_ops_num(d.get("factored_distance"))} FT']
        cautions = []
        letter, atis = self._atis(dest, arrival=True) if dest else (None, None)
        if atis:
            arr = re.findall(r"(?:RWY|RUNWAY)S?\s*(\d{2}[LRC]?)(?=[^.]*?(?:ARR|APCH|APPROACH|LDG|LANDING))", atis.upper()) or re.findall(r"(?:RWY|RUNWAY)S?\s*(\d{2}[LRC]?)", atis.upper())
            if arr and str(rw.get("identifier", "")).upper() not in [a.upper() for a in arr]:
                cautions.append(f"ATIS {letter or ''} RWY {'/'.join(dict.fromkeys(arr))} NOT PLANNED RWY {rw.get('identifier', '')}".replace("  ", " "))
        try:
            v = v or self._sim(["sim/flightmodel/position/latitude", "sim/flightmodel/position/longitude",
                                "sim/flightmodel/position/groundspeed", "sim/flightmodel/weight/m_fuel_total",
                                "sim/cockpit2/engine/indicators/fuel_flow_kg_sec", "sim/flightmodel/weight/m_total"])
            prog = self._progress(v)
            gross = float(v.get("sim/flightmodel/weight/m_total", 0) or 0)
            fob = float(v.get("sim/flightmodel/weight/m_fuel_total", 0) or 0)
            planned_lw = _ops_num(c.get("planned_weight"))
            if prog and prog["efob"] is not None and gross > 0 and planned_lw:
                est_lw = int(gross - (fob - prog["efob"]))
                if est_lw > planned_lw + 500:
                    cautions.append(f"EST LW {est_lw} ABOVE PLANNED {planned_lw}")
        except Exception:
            pass
        if lda and _ops_num(wet.get("factored_distance")) > lda:
            cautions.append("WET RQD DISTANCE EXCEEDS LDA")
        if cautions:
            lines += ["CAUTION:"] + [x for m in cautions for x in ops_wrap(m)] + ["RECALCULATE BEFORE", "APPROACH"]
            self.flight["ldg_caution"] = True
        return "\n".join(lines)

    def _arrival_package(self, v):
        o = self.ofp or {}
        dest = _ops_g(o, "destination", "icao_code")
        if self.auto["arr_atis"] and dest:
            letter, text = self.msg_atis(dest, arrival=True)
            if text:
                self.flight["arr_atis_letter"] = letter
                self._queue("arr_atis", text)
            else:
                self.flight["no_arr_atis_sent"] = True
                self._queue("arr_atis", self.msg_no_atis(dest, arrival=True))
        for key, build in (("stand", self.msg_stand), ("ldgdata", lambda: self.msg_ldgdata(v))):
            if self.auto[key]:
                try:
                    self._queue(key, build())
                except Exception as e:
                    XPLMDebugString(f"ToLiss EFB: ops could not prepare {key}: {e}\n")

    def _sample_touchdown(self):
        """Called several times a second in the last 300 ft: keep the vertical speed until the wheels touch."""
        v = self._sim(["sim/flightmodel/failures/onground_any", "sim/flightmodel/position/vh_ind_fpm"], timeout=0.5)
        if not v:
            return
        if float(v.get("sim/flightmodel/failures/onground_any", 0) or 0) >= 1:
            self.flight["td_vs"] = self.flight.get("last_vs")
            self.flight["final_mode"] = False
        else:
            self.flight["last_vs"] = float(v.get("sim/flightmodel/position/vh_ind_fpm", 0) or 0)

    def msg_blockin(self):
        o, fl = self.ofp or {}, self.flight
        x = fl["oooi"]
        def hm(sec):
            sec = max(0, int(sec))
            return f"{sec // 3600}H{(sec % 3600) // 60:02d}"
        lines = ["BLOCK IN SUMMARY", f"{self._cs()} {self._route()}",
                 f'OUT {ops_hhmm(x.get("out"))}  OFF {ops_hhmm(x.get("off"))}',
                 f'ON {ops_hhmm(x.get("on"))}  IN {ops_hhmm(x.get("in"))}']
        if x.get("out") and x.get("in") and x.get("off") and x.get("on"):
            lines.append(f'BLOCK {hm(x["in"] - x["out"])} FLT {hm(x["on"] - x["off"])}')
        sched_in = _ops_num(_ops_g(o, "times", "sched_in"), 0)
        if sched_in and x.get("in"):
            mins = int(round((x["in"] - sched_in) / 60))
            lines.append("ON TIME" if abs(mins) <= 5 else (f"{mins} MIN LATE" if mins > 0 else f"{-mins} MIN EARLY"))
        if fl.get("fob_out") and fl.get("fob_in") is not None:
            f = o.get("fuel", {})
            lines += [f'FUEL USED {int(fl["fob_out"] - fl["fob_in"])} KG',
                      f'PLAN {_ops_num(f.get("taxi")) + _ops_num(f.get("enroute_burn"))} KG']
        if fl.get("td_vs") is not None:
            lines.append(f'TOUCHDOWN {int(round(fl["td_vs"]))} FPM')
        if fl.get("parked_stand") or fl.get("stand"):
            lines.append(f'STAND {fl.get("parked_stand") or fl["stand"]["name"]}'.upper())
        lines.append(f'WELCOME TO {_ops_g(o, "destination", "icao_code")}')
        return "\n".join(lines)

    def action(self, what):
        """Send one item now (from the Pre-flight panel)."""
        if not self.ofp:
            return False, "Load a SimBrief flight plan first (Dispatch & OFP)."
        o = self.ofp
        if what == "release":
            self._queue("release", self.msg_release())
        elif what == "wx":
            for icao in dict.fromkeys(x for x in (_ops_g(o, "origin", "icao_code"), _ops_g(o, "destination", "icao_code"), _ops_g(o, "alternate", "icao_code")) if x):
                self._queue("wx", self.msg_wx(icao))
        elif what == "notams":
            for m in self.msg_notams():
                self._queue("notams", m)
        elif what == "prelim":
            self._queue("prelim", self.msg_prelim())
        elif what == "final":
            self._queue("final", self.msg_final())
        elif what == "todata":
            self._queue("todata", self.msg_todata())
        elif what == "atis":
            letter, text = self.msg_atis(_ops_g(o, "origin", "icao_code"))
            if not text:
                self._queue("atis", self.msg_no_atis(_ops_g(o, "origin", "icao_code")))
                return True, "No VATSIM ATIS online: the latest METAR was sent instead."
            self.flight["atis_letter"] = letter
            self._queue("atis", text)
        elif what == "eta":
            self._crew_message("REQUEST ETA")
        elif what == "fuel":
            self.flight["next_fuel"] = 0
            self.flight["fuel_level"] = -1          # report even if fuel is on plan
            v = self._sim(["sim/flightmodel/position/latitude", "sim/flightmodel/position/longitude",
                           "sim/flightmodel/position/groundspeed", "sim/flightmodel/weight/m_fuel_total",
                           "sim/cockpit2/engine/indicators/fuel_flow_kg_sec"])
            prog = self._progress(v)
            if not prog or prog["efob"] is None:
                self.flight["fuel_level"] = 0
                return False, "Fuel at destination can be estimated once in flight."
            f = o.get("fuel", {})
            planned = _ops_num(f.get("plan_landing")) or (_ops_num(f.get("plan_takeoff")) - _ops_num(f.get("enroute_burn")))
            diff = prog["efob"] - planned
            self.flight["fuel_level"] = 0
            self._queue("fuel", "\n".join(["FUEL CHECK", f'EFOB {_ops_g(o, "destination", "icao_code")} {prog["efob"]} KG',
                                            f"PLANNED {planned} KG", f"{'ABOVE' if diff >= 0 else 'BELOW'} PLAN {abs(diff)} KG"]))
        elif what == "depreport":
            x = self.flight["oooi"]
            if "off" not in x:
                return False, "The departure report is sent after takeoff."
            lines = [f"{self._cs()} DEP REPORT", f'OUT {ops_hhmm(x.get("out"))}  OFF {ops_hhmm(x["off"])}']
            if self.flight.get("eta"):
                lines.append(f'ETA {_ops_g(o, "destination", "icao_code")} {ops_hhmm(self.flight["eta"])}')
            self._queue("depreport", "\n".join(lines))
        elif what == "arr_atis":
            letter, text = self.msg_atis(_ops_g(o, "destination", "icao_code"), arrival=True)
            if not text:
                self._queue("arr_atis", self.msg_no_atis(_ops_g(o, "destination", "icao_code"), arrival=True))
                return True, "No VATSIM ATIS online: the latest METAR was sent instead."
            self.flight["arr_atis_letter"] = letter
            self._queue("arr_atis", text)
        elif what == "stand":
            self._queue("stand", self.msg_stand())
        elif what == "reassign":
            if "in" in self.flight["oooi"]:
                # already parked: just exclude the stand for future flights, no message to the aircraft
                name = (self.flight.get("stand") or {}).get("name", "")
                self.reassign_stand()
                self.flight["stand"] = {"name": name, "lat": 0, "lon": 0, "kind": "", "size": "", "op": "", "airlines": [], "equip": ""} if name else None
                return True, f"Stand {name} is excluded at {_ops_g(o, 'destination', 'icao_code')} for future flights."
            self._queue("stand", self.reassign_stand())
            self.next_send = min(self.next_send, time.time() + 1)
            return True, "A revised stand assignment is being sent."
        elif what == "ldgdata":
            self._queue("ldgdata", self.msg_ldgdata())
        elif what == "blockin":
            self._queue("blockin", self.msg_blockin())
        elif what == "enroute_wx":
            self._queue("enroute_wx", self.msg_wx(_ops_g(o, "destination", "icao_code"), use_ofp=False))
        elif what == "reset":
            self.flight = self._new_flight(self.flight.get("ofp_id"))
            self.queue = []
            return True, "The flight's pre-flight items are reset; they will be sent again."
        else:
            return False, "Unknown item."
        self.next_send = min(self.next_send, time.time() + 1)
        return True, "queued"

    # ---------- messages from the crew ----------
    def _crew_message(self, text):
        t = re.sub(r"\s+", " ", str(text or "").upper()).strip()
        o = self.ofp or {}
        if re.search(r"(LOAD ?SHEET|LDSHT|LS)\b.*\b(ACK|RECEIVED|RCVD|ACCEPT)", t) or re.search(r"\b(ACK|ACCEPT)\w*\b.*LOAD ?SHEET", t):
            if not self.flight.get("edno"):
                self._queue("reply", "FINAL LOADSHEET NOT YET\nISSUED. IT FOLLOWS WHEN\nBOARDING IS COMPLETE")
                return
            self.flight["ack"] = int(time.time())
            self._queue("reply", f'LOADSHEET EDNO {self.flight["edno"]}\nACKNOWLEDGED\nHAVE A GOOD FLIGHT')
            return
        m = re.match(r"^(?:REQ(?:UEST)?\s+)?(WX|WEATHER|METAR)\b\s*(.*)$", t)
        if m:
            icaos = re.findall(r"\b[A-Z]{4}\b", m.group(2)) or [x for x in (_ops_g(o, "origin", "icao_code"), _ops_g(o, "destination", "icao_code"), _ops_g(o, "alternate", "icao_code")) if x]
            for icao in list(dict.fromkeys(icaos))[:4]:
                self._queue("reply", self.msg_wx(icao, use_ofp=False))
            if not icaos:
                self._queue("reply", "WX REQUEST: PLEASE ADD THE AIRPORT, E.G. REQUEST WX YMML")
            return
        m = re.match(r"^(?:REQ(?:UEST)?\s+)?(?:D-?)?ATIS\b\s*(.*)$", t)
        if m:
            icao = (re.findall(r"\b[A-Z]{4}\b", m.group(1)) or [_ops_g(o, "destination" if self.flight["airborne"] else "origin", "icao_code")])[0]
            letter, txt = self.msg_atis(icao, arrival=self.flight["airborne"]) if icao else (None, None)
            self._queue("reply", txt or (self.msg_no_atis(icao, arrival=self.flight["airborne"]) if icao else "PLEASE ADD THE AIRPORT"))
            return
        if re.match(r"^(?:REQ(?:UEST)?\s+)?(?:FINAL\s+)?(LOAD ?SHEET|LDSHT)\b", t):
            self._queue("reply", self.msg_final() if self.ofp else "NO FLIGHT PLAN LOADED")
            return
        if re.match(r"^(?:REQ(?:UEST)?\s+)?(STAND|GATE|PARKING)\b", t):
            self._queue("reply", self.msg_stand() if self.ofp else "NO FLIGHT PLAN LOADED")
            return
        if re.match(r"^(?:REQ(?:UEST)?\s+)?(LANDING DATA|LDG DATA|LAND DATA|LDGDATA)\b", t):
            self._queue("reply", self.msg_ldgdata() if self.ofp else "NO FLIGHT PLAN LOADED")
            return
        if re.match(r"^(?:REQ(?:UEST)?\s+)?(ETA|PROGRESS|PROG)\b", t):
            v = self._sim(["sim/flightmodel/position/latitude", "sim/flightmodel/position/longitude",
                           "sim/flightmodel/position/groundspeed", "sim/flightmodel/weight/m_fuel_total",
                           "sim/cockpit2/engine/indicators/fuel_flow_kg_sec"])
            prog = self._progress(v) if self.ofp else None
            dest = _ops_g(o, "destination", "icao_code")
            if not prog:
                self._queue("reply", "PROGRESS NOT AVBL\nNO FLIGHT PLAN OR\nNOT YET IN FLIGHT")
            else:
                lines = [f"PROGRESS {dest}", f"ETA {ops_hhmm(prog['eta'])}", f"{prog['rem']} NM TO GO"]
                if prog["efob"] is not None:
                    lines.append(f"EFOB {prog['efob']} KG")
                dly = self._delay_text(prog["eta"])
                if dly:
                    lines.append(dly)
                self._queue("reply", "\n".join(lines))
            return
        if re.match(r"^(?:REQ(?:UEST)?\s+)?(TO DATA|TAKE ?OFF DATA|TODATA)\b", t):
            self._queue("reply", self.msg_todata() if self.ofp else "NO FLIGHT PLAN LOADED")
            return

    def flight_status(self):
        o = self.ofp or {}
        fl = self.flight
        return {"loaded": bool(self.ofp), "callsign": self._cs() if self.ofp else "", "route": self._route() if self.ofp else "",
                "generated": _ops_num(_ops_g(o, "params", "time_generated")), "auto": self.auto, "sent": fl["sent"],
                "queued": list(fl["queued"]), "ack": fl["ack"], "atis_letter": fl["atis_letter"],
                "airborne": fl["airborne"], "beacon_seen": fl["beacon_seen"], "oooi": fl["oooi"],
                "eta": fl["eta"], "efob": fl["efob"], "dist_rem": fl["dist_rem"], "fuel_level": fl["fuel_level"],
                "todata_caution": bool(fl.get("todata_caution")), "ldg_caution": bool(fl.get("ldg_caution")),
                "stand": (fl.get("stand") or {}).get("name"), "arr_atis_letter": fl.get("arr_atis_letter"),
                "arrival_sent": fl.get("arrival_sent"), "td_vs": fl.get("td_vs"),
                "phase": ("done" if "in" in fl["oooi"] else "arrival" if (fl.get("arrival_sent") or "on" in fl["oooi"])
                          else "inflight" if "off" in fl["oooi"] else "pre"),
                "acft": f'{_ops_g(o, "aircraft", "icaocode")} {_ops_g(o, "aircraft", "reg")}'.strip(),
                "dep_atis_online": bool(self._atis(_ops_g(o, "origin", "icao_code"))[0]) if self.ofp else False,
                "arr_atis_online": bool(self._atis(_ops_g(o, "destination", "icao_code"), True)[0]) if self.ofp else False,
                "no_atis_sent": bool(fl.get("no_atis_sent")), "no_arr_atis_sent": bool(fl.get("no_arr_atis_sent")),
                "holding": bool(fl.get("holding")), "aircraft_online": self.aircraft_online,
                "parked_stand": fl.get("parked_stand"), "rules_path": self._rules_path(),
                "stand_pos": [fl["stand"]["lat"], fl["stand"]["lon"]] if fl.get("stand") and fl["stand"].get("lat") else None,
                "sta": _ops_num(_ops_g(o, "times", "sched_in"), 0), "dest": _ops_g(o, "destination", "icao_code")}

    def status(self):
        return {"enabled": self.enabled, "configured": self.configured(), "logon_set": bool(self.logon),
                "ops_callsign": self.ops_callsign, "aircraft": self.aircraft, "aircraft_online": self.aircraft_online,
                "last_poll": int(self.last_poll), "next_poll_in": max(0, int(self.next_poll - time.time())) if self.enabled else None,
                "error": self.error, "unread": self.unread, "count": len(self.log)}


# ===== Updater (GitHub Releases) =====
def version_tuple(v):
    nums = re.findall(r"\d+", str(v or ""))
    return tuple(int(x) for x in nums) if nums else (0,)


class Updater:
    """Checks GitHub for a newer release, downloads it, verifies its SHA-256 checksum,
    backs up the installed files and installs the new ones."""

    def __init__(self, plugin):
        self.plugin = plugin
        cfg = plugin.config if isinstance(getattr(plugin, 'config', None), dict) else {}
        self.enabled = bool(cfg.get("update_check", True))
        self.latest = None          # {"version", "notes", "published", "zip_url", "zip_name", "sha_url", "sha_in_notes", "page"}
        self.checked_at = 0
        self.error = ""
        self.state = "idle"         # idle / checking / downloading / installed / failed / restored
        self.message = ""
        self.lock = threading.Lock()

    def paths(self):
        plugin_dir = self.plugin.plugin_dir
        return {"html": os.path.join(plugin_dir, "index.html"),
                "py": os.path.join(os.path.dirname(plugin_dir), "PI_ToLissWebTablet.py"),
                "backup": os.path.join(plugin_dir, "backup")}

    def status(self):
        latest = self.latest or {}
        available = bool(latest) and version_tuple(latest.get("version")) > version_tuple(EFB_VERSION)
        backups = []
        try:
            backups = sorted(os.listdir(self.paths()["backup"]), reverse=True)
        except Exception:
            pass
        return {"current": EFB_VERSION, "repo": UPDATE_REPO, "enabled": self.enabled, "latest": latest.get("version"),
                "notes": latest.get("notes", ""), "published": latest.get("published", ""), "page": latest.get("page", ""),
                "available": available, "checked_at": int(self.checked_at), "error": self.error,
                "state": self.state, "message": self.message, "backup": backups[0] if backups else None}

    def _get(self, url, limit=None):
        req = urllib.request.Request(url, headers={'User-Agent': f'ToLissEFB/{EFB_VERSION}', 'Accept': 'application/vnd.github+json'})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read(limit + 1 if limit else -1)
        if limit and len(data) > limit:
            raise ValueError("download is larger than expected")
        return data

    def _check_api(self):
        req = urllib.request.Request(UPDATE_API.format(repo=UPDATE_REPO),
                                     headers={'User-Agent': f'ToLissEFB/{EFB_VERSION}', 'Accept': 'application/vnd.github+json'})
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
        try:
            rel = json.loads(raw.decode('utf-8'))
        except ValueError:
            raise ValueError("GitHub's release service sent an unexpected reply: " + raw[:80].decode('utf-8', errors='replace').strip())
        tag = rel.get("tag_name") or rel.get("name") or ""
        assets = rel.get("assets") or []
        zip_a = next((x for x in assets if str(x.get("name", "")).lower().endswith(".zip")), None)
        sha_a = next((x for x in assets if str(x.get("name", "")).lower().endswith(".sha256")), None)
        notes = rel.get("body") or ""
        m = re.search(r"SHA-?256[^0-9a-fA-F]*([0-9a-fA-F]{64})", notes)
        return {"version": tag.lstrip("vV"), "notes": notes[:6000], "published": rel.get("published_at", ""),
                "page": rel.get("html_url", ""),
                "zip_url": zip_a.get("browser_download_url") if zip_a else None,
                "zip_name": zip_a.get("name") if zip_a else None,
                "sha_url": sha_a.get("browser_download_url") if sha_a else None,
                "sha_in_notes": m.group(1).lower() if m else None}

    def _check_web(self):
        """Without the API: follow the 'latest release' address to its tag and use the standard file names."""
        req = urllib.request.Request(UPDATE_WEB.format(repo=UPDATE_REPO), headers={'User-Agent': f'ToLissEFB/{EFB_VERSION}'})
        with urllib.request.urlopen(req, timeout=20) as r:
            final = r.geturl()
        m = re.search(r"/releases/tag/v?([\d.]+)", final)
        if not m:
            raise LookupError("No release has been published yet.")
        ver = m.group(1)
        name = f"ToLiss_EFB_{ver.replace('.', '_')}.zip"
        notes = ""
        try:
            text = self._get(UPDATE_RAW.format(repo=UPDATE_REPO, ver=ver), 400000).decode('utf-8', errors='ignore')
            sec = re.search(rf"^## {re.escape(ver)}\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
            notes = sec.group(1).strip() if sec else ""
        except Exception:
            pass
        return {"version": ver, "notes": notes[:6000], "published": "", "page": final,
                "zip_url": UPDATE_DL.format(repo=UPDATE_REPO, ver=ver, name=name), "zip_name": name,
                "sha_url": UPDATE_DL.format(repo=UPDATE_REPO, ver=ver, name=name + ".sha256"), "sha_in_notes": None}

    def check(self, force=False):
        if not force and (not self.enabled or time.time() - self.checked_at < UPDATE_CHECK_INTERVAL_S):
            return
        with self.lock:
            self.state = "checking" if self.state in ("idle", "failed") else self.state
            errors = []
            for method in (self._check_api, self._check_web):
                try:
                    self.latest = method()
                    self.error = ""
                    break
                except LookupError as e:
                    self.error = str(e)
                    errors = []
                    break
                except urllib.error.HTTPError as e:
                    errors.append("No release has been published yet." if e.code == 404 else f"GitHub answered {e.code}.")
                except Exception as e:
                    errors.append(str(e) if isinstance(e, ValueError) else f"Could not reach GitHub ({e.__class__.__name__}).")
            else:
                self.error = errors[-1] if errors else "Could not check for updates."
            if errors:
                XPLMDebugString("ToLiss EFB: update check: " + " | ".join(errors) + "\n")
            self.checked_at = time.time()
            if self.state == "checking":
                self.state = "idle"

    def install(self):
        import hashlib, zipfile, io
        with self.lock:
            latest = self.latest or {}
            try:
                if version_tuple(latest.get("version")) <= version_tuple(EFB_VERSION):
                    raise ValueError("no newer version to install")
                if not latest.get("zip_url"):
                    raise ValueError("the release has no zip file attached")
                self.state, self.message = "downloading", f"Downloading {latest.get('zip_name')}..."
                data = self._get(latest["zip_url"], UPDATE_MAX_BYTES)
                expected = latest.get("sha_in_notes")
                if latest.get("sha_url"):
                    txt = self._get(latest["sha_url"], 4096).decode('utf-8', errors='ignore')
                    m = re.search(r"[0-9a-fA-F]{64}", txt)
                    expected = m.group(0).lower() if m else expected
                if not expected:
                    raise ValueError("the release has no SHA-256 checksum, so it cannot be verified")
                actual = hashlib.sha256(data).hexdigest()
                if actual != expected:
                    raise ValueError("the download does not match its checksum (damaged or altered); nothing was changed")
                zf = zipfile.ZipFile(io.BytesIO(data))
                names = zf.namelist()
                py_name = next((n for n in names if n.replace("\\", "/").split("/")[-1] == "PI_ToLissWebTablet.py"), None)
                html_name = next((n for n in names if n.replace("\\", "/").endswith("ToLissWebTablet/index.html")), None)
                if not py_name or not html_name:
                    raise ValueError("the zip does not contain PI_ToLissWebTablet.py and ToLissWebTablet/index.html")
                new_py, new_html = zf.read(py_name), zf.read(html_name)
                compile(new_py, "PI_ToLissWebTablet.py", "exec")      # refuse a plugin file that would not load
                p = self.paths()
                bdir = os.path.join(p["backup"], f"{EFB_VERSION}_{time.strftime('%Y%m%d_%H%M%S')}")
                os.makedirs(bdir, exist_ok=True)
                shutil.copy2(p["py"], os.path.join(bdir, "PI_ToLissWebTablet.py"))
                shutil.copy2(p["html"], os.path.join(bdir, "index.html"))
                for target, content in ((p["html"], new_html), (p["py"], new_py)):
                    tmp = target + ".new"
                    with open(tmp, 'wb') as f:
                        f.write(content)
                    os.replace(tmp, target)
                self.state = "installed"
                self.message = (f"Version {latest.get('version')} installed. Reload this page now; the plugin part takes effect "
                                f"after Plugins > XPPython3 > Reload scripts, or restarting X-Plane.")
                XPLMDebugString(f"ToLiss EFB: updated to {latest.get('version')} (backup in {bdir})\n")
            except Exception as e:
                self.state, self.message = "failed", f"Update not installed: {e}"
                XPLMDebugString(f"ToLiss EFB: {self.message}\n")

    def rollback(self):
        with self.lock:
            try:
                p = self.paths()
                backups = sorted(os.listdir(p["backup"]), reverse=True)
                if not backups:
                    raise ValueError("there is no backup to restore")
                bdir = os.path.join(p["backup"], backups[0])
                for name, target in (("index.html", p["html"]), ("PI_ToLissWebTablet.py", p["py"])):
                    shutil.copy2(os.path.join(bdir, name), target + ".new")
                    os.replace(target + ".new", target)
                shutil.rmtree(bdir, ignore_errors=True)
                self.state = "restored"
                self.message = (f"Restored version {backups[0].split('_')[0]}. Reload this page; the plugin part takes effect after "
                                f"Reload scripts or restarting X-Plane.")
            except Exception as e:
                self.state, self.message = "failed", f"Could not restore: {e}"


# ===== Custom checklists (ToLiss checklist.xml format), stored in <plugin folder>/checklists =====
def checklist_dir(plugin_dir):
    d = os.path.join(plugin_dir, "checklists")
    os.makedirs(d, exist_ok=True)
    return d


def checklist_safe_name(name):
    name = re.sub(r"[^A-Za-z0-9 _().-]", "", str(name or "")).strip().strip(".")
    if name.lower().endswith(".xml"):
        name = name[:-4]
    return name[:60]


# ===== Instrument approach procedures (X-Plane CIFP + fix/navaid data) =====
APPROACH_TYPES = {"I": "ILS", "L": "LOC", "B": "LOC BC", "R": "RNAV", "H": "RNP", "J": "GLS", "P": "GPS",
                  "D": "VOR/DME", "V": "VOR", "S": "VOR", "N": "NDB", "Q": "NDB/DME", "X": "LDA", "U": "SDF",
                  "G": "IGS", "T": "TACAN", "F": "FMS", "W": "MLS", "Y": "MLS", "M": "MLS"}
WPT_ROLE = {"A": "IAF", "B": "IF", "C": "IAF", "D": "IAF", "F": "FAF", "I": "FACF", "M": "MAP", "H": "HOLD"}


def nav_data_files(xp_path):
    """Navigation data in X-Plane's priority order: Custom Data (e.g. Navigraph) first, then the default data."""
    custom = os.path.join(xp_path, "Custom Data")
    default = os.path.join(xp_path, "Resources", "default data")
    def pick(name):
        return [p for p in (os.path.join(custom, name), os.path.join(default, name)) if os.path.isfile(p)]
    cifp_dirs = [d for d in (os.path.join(custom, "CIFP"), os.path.join(default, "CIFP")) if os.path.isdir(d)]
    return pick("earth_fix.dat"), pick("earth_nav.dat"), cifp_dirs


def _num(v, scale=1.0):
    try:
        v = str(v).strip()
        return float(v) / scale if v else None
    except Exception:
        return None


def _alt(v):
    v = str(v).strip()
    if not v:
        return None
    if v.startswith("FL"):
        try:
            return int(v[2:]) * 100
        except Exception:
            return None
    try:
        return int(v)
    except Exception:
        return None


def _dms(v):
    """CIFP coordinates, e.g. N33562113 / E151105120 (degrees, minutes, seconds with hundredths)."""
    v = str(v).strip()
    try:
        hemi, body = v[0], v[1:]
        if hemi in "NS":
            d, m, sec = int(body[0:2]), int(body[2:4]), int(body[4:]) / 100.0
        else:
            d, m, sec = int(body[0:3]), int(body[3:5]), int(body[5:]) / 100.0
        val = d + m / 60.0 + sec / 3600.0
        return -val if hemi in "SW" else val
    except Exception:
        return None


class NavDatabase:
    CACHE_VERSION = 2      # 2: adds localisers by runway

    def __init__(self, xp_path, cache_dir=None):
        self.xp_path = xp_path
        self.cache_dir = cache_dir
        self.state = "idle"    # idle / loading / ready / error
        self.load_seconds = None
        self.fixes = None      # ident -> [(region, airport, lat, lon)]
        self.navaids = None    # ident -> [(region, airport, lat, lon, code, freq)]
        self.locs = None       # (airport, ident) -> (freq_mhz, true_bearing)
        self.locs_rwy = None   # (airport, runway) -> (ident, freq_mhz, true_bearing)
        self.lock = threading.Lock()
        self.cache = {}
        self.source = ""

    def _signature(self, files):
        sig = []
        for p in files:
            try:
                st = os.stat(p)
                sig.append([p, st.st_size, int(st.st_mtime)])
            except Exception:
                sig.append([p, 0, 0])
        return sig

    def _cache_path(self):
        return os.path.join(self.cache_dir, "navdata_cache.pkl") if self.cache_dir else None

    def load_async(self):
        if self.state in ("loading", "ready"):
            return
        self.state = "loading"
        threading.Thread(target=self.load, daemon=True).start()

    def load(self):
        with self.lock:
            if self.fixes is not None:
                self.state = "ready"
                return
            self.state = "loading"
            t0 = time.time()
            fix_files, nav_files, cifp_dirs = nav_data_files(self.xp_path)
            self.source = "Custom Data" if any("Custom Data" in f for f in fix_files + cifp_dirs) else "X-Plane default"
            signature = [self.CACHE_VERSION] + self._signature(fix_files + nav_files)
            cache_file = self._cache_path()
            # 1. Reuse the saved index when the navigation data has not changed
            if cache_file and os.path.isfile(cache_file):
                try:
                    with open(cache_file, 'rb') as f:
                        cached = pickle.load(f)
                    if cached.get("signature") == signature:
                        self.fixes, self.navaids, self.locs = cached["fixes"], cached["navaids"], cached["locs"]
                        self.locs_rwy = cached.get("locs_rwy", {})
                        self.state = "ready"
                        self.load_seconds = round(time.time() - t0, 2)
                        XPLMDebugString(f"ToLiss EFB: Navigation index loaded from cache in {self.load_seconds}s\n")
                        return
                except Exception as e:
                    XPLMDebugString(f"ToLiss EFB: Navigation cache unreadable, rebuilding ({e})\n")
            fixes, navaids, locs, locs_rwy = {}, {}, {}, {}
            for path in reversed(fix_files):        # later (higher priority) files overwrite earlier ones
                seen = set()
                try:
                    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                        for line in f:
                            parts = line.split(None, 5)
                            if len(parts) < 5 or parts[0][-1:].isalpha():
                                continue
                            try:
                                lat, lon = float(parts[0]), float(parts[1])
                            except ValueError:
                                continue
                            ident = parts[2]
                            lst = fixes.get(ident)
                            if lst is None or ident not in seen:
                                lst = fixes[ident] = []
                                seen.add(ident)
                            lst.append((parts[4], parts[3], lat, lon))
                except Exception as e:
                    XPLMDebugString(f"ToLiss EFB: earth_fix read error: {e}\n")
            for path in reversed(nav_files):
                seen = set()
                try:
                    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                        for line in f:
                            parts = line.split()
                            if len(parts) < 10:
                                continue
                            try:
                                code = int(parts[0])
                                lat, lon = float(parts[1]), float(parts[2])
                            except ValueError:
                                continue
                            ident, apt, region = parts[7], parts[8], parts[9]
                            freq = parts[4]
                            if code in (4, 5):
                                try:
                                    bearing = float(parts[6]) % 360.0
                                except ValueError:
                                    bearing = None
                                f_mhz = int(freq) / 100.0 if freq.isdigit() else None
                                locs[(apt, ident)] = (f_mhz, bearing)
                                if len(parts) > 10:          # the runway the localiser serves, e.g. 14 or 34L
                                    locs_rwy[(apt, parts[10].upper())] = (ident, f_mhz, bearing)
                            elif code in (2, 3, 12, 13):
                                key = ident
                                if (key, "n") not in seen:
                                    navaids[key] = []
                                    seen.add((key, "n"))
                                navaids[key].append((region, apt, lat, lon, code, freq))
                except Exception as e:
                    XPLMDebugString(f"ToLiss EFB: earth_nav read error: {e}\n")
            self.fixes, self.navaids, self.locs, self.locs_rwy = fixes, navaids, locs, locs_rwy
            self.state = "ready"
            self.load_seconds = round(time.time() - t0, 2)
            XPLMDebugString(f"ToLiss EFB: Navigation index built in {self.load_seconds}s\n")
            if cache_file:
                try:
                    tmp = cache_file + ".tmp"
                    with open(tmp, 'wb') as f:
                        pickle.dump({"signature": signature, "fixes": fixes, "navaids": navaids, "locs": locs, "locs_rwy": locs_rwy}, f, protocol=pickle.HIGHEST_PROTOCOL)
                    os.replace(tmp, cache_file)
                except Exception as e:
                    XPLMDebugString(f"ToLiss EFB: Could not save navigation cache: {e}\n")

    def _lookup(self, ident, region, section, subsection, airport, runways):
        ident = ident.strip()
        if not ident:
            return None
        if section == "P" and subsection == "G" or ident.startswith("RW") and ident in runways:
            r = runways.get(ident)
            return (r["lat"], r["lon"]) if r else None
        def best(cands, want_nav):
            if not cands:
                return None
            scored = []
            for c in cands:
                score = 0
                if c[0] == region:
                    score += 2
                if c[1] == airport:
                    score += 3
                elif c[1] == "ENRT":
                    score += 1
                scored.append((score, c))
            scored.sort(key=lambda x: -x[0])
            return (scored[0][1][2], scored[0][1][3])
        is_nav = section == "D" or (section == "P" and subsection == "N")
        first, second = (self.navaids, self.fixes) if is_nav else (self.fixes, self.navaids)
        return best(first.get(ident), is_nav) or best(second.get(ident), not is_nav)

    def procedures(self, icao):
        icao = icao.upper()
        if icao in self.cache:
            return self.cache[icao]
        if self.fixes is None:
            self.load_async()
            return {"status": "loading", "icao": icao}
        _, _, cifp_dirs = nav_data_files(self.xp_path)
        path = next((os.path.join(d, icao + ".dat") for d in cifp_dirs if os.path.isfile(os.path.join(d, icao + ".dat"))), None)
        if not path:
            return None
        runways, rows = {}, []
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                line = line.rstrip("\n")
                if line.startswith("RWY:"):
                    groups = line[4:].rstrip(";").split(";")
                    a = groups[0].split(",")
                    b = groups[1].split(",") if len(groups) > 1 else []
                    ident = a[0].strip()
                    rw = {"loc": a[5].strip() if len(a) > 5 else "", "tch": _num(a[7]) if len(a) > 7 else None,
                          "elev": _num(a[3]) if len(a) > 3 else None,
                          "lat": _dms(b[0]) if b else None, "lon": _dms(b[1]) if len(b) > 1 else None,
                          "disp": _num(b[2]) if len(b) > 2 else None}
                    if rw["lat"] is not None and rw["lon"] is not None:
                        runways[ident] = rw
                elif line.startswith("APPCH:"):
                    rows.append(line[6:].rstrip(";").split(","))

        procs = {}
        for r in rows:
            r = r + [""] * (40 - len(r))
            proc, rtype, trans = r[2].strip(), r[1].strip(), r[3].strip()
            p = procs.setdefault(proc, {"id": proc, "final_type": None, "legs": {}})
            if rtype != "A" and rtype != "Z":
                p["final_type"] = p["final_type"] or rtype
            key = trans if rtype == "A" else ("__missed__" if rtype == "Z" else "__final__")
            fix = r[4].strip()
            desc = r[8]
            role = WPT_ROLE.get(desc[3:4], "") if len(desc) >= 4 else ""
            if not role and r[11].strip() == "IF":
                role = "IF"
            pos = self._lookup(fix, r[5].strip(), r[6].strip(), r[7].strip(), icao, runways) if fix else None
            centre = None
            if r[30].strip():
                centre = self._lookup(r[30].strip(), r[31].strip(), r[32].strip(), r[33].strip(), icao, runways)
            if r[11].strip() == "AF" and r[13].strip():
                centre = self._lookup(r[13].strip(), r[14].strip(), "D", "", icao, runways)
            alt_desc = r[22].strip()
            leg = {"seq": int(r[0]) if r[0].strip().isdigit() else 0, "fix": fix, "role": role, "pt": r[11].strip(),
                   "nav": r[13].strip(),
                   "turn": r[9].strip(), "course": _num(r[20], 10.0), "dist": _num(r[21], 10.0),
                   "alt_desc": alt_desc, "alt1": _alt(r[23]), "alt2": _alt(r[24]),
                   "speed": _alt(r[27]), "vangle": _num(r[28], 100.0), "radius": _num(r[17], 1000.0),
                   "lat": pos[0] if pos else None, "lon": pos[1] if pos else None,
                   "clat": centre[0] if centre else None, "clon": centre[1] if centre else None}
            p["legs"].setdefault(key, []).append(leg)

        out = []
        for pid, p in procs.items():
            final = sorted(p["legs"].get("__final__", []), key=lambda l: l["seq"])
            missed = sorted(p["legs"].get("__missed__", []), key=lambda l: l["seq"])
            # Legs after the missed approach point belong to the missed approach
            for i, l in enumerate(final):
                if l["role"] == "MAP":
                    missed = final[i + 1:] + missed
                    final = final[:i + 1]
                    break
            m = re.match(r"^([A-Z])(\d{2}[LRCBT]?)-?([A-Z])?$", pid)
            letter = (p["final_type"] or pid[0])
            tname = APPROACH_TYPES.get(letter, APPROACH_TYPES.get(pid[0], pid[0]))
            if m:
                rwy, suffix = m.group(2), m.group(3) or ""
                name = f"{tname}{' ' + suffix if suffix else ''} {rwy}"
            else:
                rwy, suffix = "", ""
                name = f"{tname} {pid[1:].lstrip('-')}".strip()
            rw = runways.get("RW" + rwy) if rwy else None
            loc = None
            if tname in ("ILS", "LOC", "LOC BC", "LDA", "SDF", "IGS"):
                # 1. the runway record's localiser ident, 2. the recommended navaid on the final legs, 3. by runway
                candidates = []
                if rw and rw.get("loc"):
                    candidates.append(rw["loc"])
                candidates += [l["nav"] for l in final if l.get("nav")]
                for ident in candidates:
                    li = self.locs.get((icao, ident))
                    if li:
                        loc = {"ident": ident, "freq": li[0], "bearing": li[1]}
                        break
                if not loc and rwy:
                    lr = (self.locs_rwy or {}).get((icao, rwy.upper()))
                    if lr:
                        loc = {"ident": lr[0], "freq": lr[1], "bearing": lr[2]}
            fin_course = next((l["course"] for l in reversed(final) if l["course"] is not None), None)
            gp = next((l["vangle"] for l in final if l["vangle"]), None)
            transitions = {k: sorted(v, key=lambda l: l["seq"]) for k, v in p["legs"].items() if k not in ("__final__", "__missed__")}
            out.append({"id": pid, "name": name, "type": tname, "runway": rwy, "suffix": suffix,
                        "final": final, "missed": missed, "transitions": transitions,
                        "loc": loc, "course": fin_course, "glidepath": abs(gp) if gp else None,
                        "threshold": ({"lat": rw["lat"], "lon": rw["lon"], "elev": rw.get("elev"), "tch": rw.get("tch")} if rw else None)})
        order = {"ILS": 0, "GLS": 1, "RNP": 2, "RNAV": 3, "GPS": 4, "LOC": 5}
        out.sort(key=lambda a: (a["runway"] or "zz", order.get(a["type"], 9), a["suffix"]))
        result = {"status": "success", "icao": icao, "source": self.source,
                  "file": os.path.relpath(path, self.xp_path), "approaches": out}
        if len(self.cache) > 8:
            self.cache.pop(next(iter(self.cache)))
        self.cache[icao] = result
        return result


# ===== ToLiss MCDU (AirbusFBW) =====
# Screen: 14 lines x 24 chars. Line 0 title, odd lines labels (small), even lines 2-12 content, line 13 scratchpad.
# Each line is published once per colour: <MCDU1|2><title|stitle|label|cont|scont|sp><line><colour>.
MCDU_COLS = 24

def mcdu_display_refs(side):
    p = f"AirbusFBW/MCDU{side}"
    refs = []   # (dataref, line, colour, small)
    for c in "bgswy":
        refs.append((f"{p}title{c}", 0, c, False))
    for c in "wy":
        refs.append((f"{p}stitle{c}", 0, c, True))
    for n in range(1, 7):
        for c in "abgwy":
            refs.append((f"{p}label{n}{c}", 2 * n - 1, c, True))
        refs.append((f"{p}label{n}Lg", 2 * n - 1, "g", False))
        for c in "abgmwy":
            refs.append((f"{p}scont{n}{c}", 2 * n, c, True))
        for c in "abgmswy":   # a "c" colour is listed by some references but ToLiss does not publish it
            refs.append((f"{p}cont{n}{c}", 2 * n, c, False))
    refs.append((f"{p}spw", 13, "w", False))
    refs.append((f"{p}spa", 13, "a", False))
    return refs

# Symbol-colour ("s") characters and what they draw
MCDU_SYMBOLS = {"A": ("[", "b"), "B": ("]", "b"), "0": ("\u2190", "b"), "1": ("\u2192", "b"),
                "2": ("\u2190", "w"), "3": ("\u2192", "w"), "4": ("\u2190", "a"), "5": ("\u2192", "a"),
                "E": ("\u2610", "a")}

MCDU_KEYS = set(["LSK%d%s" % (n, s) for n in range(1, 7) for s in "LR"] +
                ["DirTo", "Prog", "Perf", "Init", "Data", "Fpln", "RadNav", "FuelPred", "SecFpln", "ATC", "Menu", "Airport",
                 "SlewLeft", "SlewRight", "SlewUp", "SlewDown", "KeyDecimal", "KeyPM", "KeySlash", "KeySpace",
                 "KeyOverfly", "KeyClear", "KeyBright", "KeyDim"] +
                ["Key%d" % d for d in range(10)] + ["Key%s" % chr(c) for c in range(ord('A'), ord('Z') + 1)])

def mcdu_text_to_keys(text):
    keys = []
    for ch in str(text).upper():
        if "A" <= ch <= "Z" or "0" <= ch <= "9":
            keys.append("Key" + ch)
        elif ch == ".":
            keys.append("KeyDecimal")
        elif ch == "/":
            keys.append("KeySlash")
        elif ch == " ":
            keys.append("KeySpace")
        elif ch in "-+":
            keys.append("KeyPM")
    return keys


VATSIM_FACILITY = {1: "FSS", 2: "DEL", 3: "GND", 4: "TWR", 5: "APP", 6: "CTR"}


def vatsim_nearby_atc(lat, lon, agl_ft, max_nm=400.0, limit=40):
    """Online ATC sorted nearest first, flagged in_range using VHF line-of-sight between the aircraft
    and the station's transmitters (the model Audio for VATSIM uses)."""
    data = vatsim_cache.get("data") or {}
    tx = vatsim_cache.get("transceivers") or {}
    out = []
    entries = [(c, False) for c in data.get("controllers", [])] + [(a, True) for a in data.get("atis", [])]
    for c, is_atis in entries:
        cs = c.get("callsign", "")
        freq = str(c.get("frequency", ""))
        if not cs or cs.endswith("_OBS") or freq.startswith("199.998"):
            continue
        if not is_atis and c.get("facility", 0) == 0:
            continue
        try:
            khz = int(round(float(freq) * 1000))
        except Exception:
            continue
        best_nm, in_range = None, False
        for (tlat, tlon, tagl_m, _f) in tx.get(cs, []):
            d_nm = _gm_dist_m(lat, lon, tlat, tlon) / 1852.0
            los = 1.23 * (math.sqrt(max(agl_ft, 10.0)) + math.sqrt(max(tagl_m * 3.28084, 10.0)))
            if best_nm is None or d_nm < best_nm:
                best_nm = d_nm
            if d_nm <= los:
                in_range = True
        if best_nm is None or best_nm > max_nm:
            continue
        text = c.get("text_atis") or []
        out.append({
            "callsign": cs, "name": c.get("name", ""), "frequency": freq, "khz": khz,
            "type": "ATIS" if is_atis else VATSIM_FACILITY.get(c.get("facility", 0), ""),
            "atis_code": c.get("atis_code") or "", "text": [str(t) for t in text][:12] if isinstance(text, list) else [],
            "distance": round(best_nm, 1), "in_range": in_range
        })
    out.sort(key=lambda x: (not x["in_range"], x["distance"]))
    return out[:limit]

# ==========================================
# NaN / Infinity Sanitizer to prevent JSON crashes
# ==========================================
def sanitize_data(data):
    if isinstance(data, dict):
        return {k: sanitize_data(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [sanitize_data(v) for v in data]
    elif isinstance(data, float):
        if math.isnan(data) or math.isinf(data):
            return 0.0
        return data
    return data

# ===== Airport scenery helpers (ground map) =====
def get_apt_search_paths(xp_path):
    """apt.dat files in X-Plane priority order: scenery_packs.ini order, then global/default airports."""
    paths = []
    def add(p):
        if p and os.path.isfile(p) and p not in paths:
            paths.append(p)
    def_apt1 = os.path.join(xp_path, "Global Scenery", "Global Airports", "Earth nav data", "apt.dat")
    def_apt2 = os.path.join(xp_path, "Resources", "default scenery", "default apt dat", "Earth nav data", "apt.dat")
    cs_dir = os.path.join(xp_path, "Custom Scenery")
    ini = os.path.join(cs_dir, "scenery_packs.ini")
    used_ini = False
    if os.path.isfile(ini):
        try:
            with open(ini, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    line = line.strip()
                    if not line.startswith('SCENERY_PACK ') or line.startswith('SCENERY_PACK_DISABLED'):
                        continue
                    used_ini = True
                    pack = line[len('SCENERY_PACK '):].strip()
                    if pack == '*GLOBAL_AIRPORTS*':
                        add(def_apt1)
                        continue
                    pack_dir = pack if os.path.isabs(pack) else os.path.join(xp_path, pack)
                    add(os.path.join(pack_dir, "Earth nav data", "apt.dat"))
        except Exception:
            pass
    if not used_ini and os.path.exists(cs_dir):
        try:
            for item in sorted(os.listdir(cs_dir)):
                add(os.path.join(cs_dir, item, "Earth nav data", "apt.dat"))
        except Exception:
            pass
    add(def_apt1)
    add(def_apt2)
    return paths


_GM_LINE_CLASS = {}
for _t in (1, 2, 7, 8, 9, 10, 11, 12, 13, 14, 51, 52, 57, 58, 59, 60, 61, 62, 63, 64):
    _GM_LINE_CLASS[_t] = 'cl'
for _t in (3, 53):
    _GM_LINE_CLASS[_t] = 'edge'
for _t in (4, 5, 6, 54, 55, 56):
    _GM_LINE_CLASS[_t] = 'hold'
for _t in (20, 21, 22, 23, 24):
    _GM_LINE_CLASS[_t] = 'white'


def _gm_parse_node(parts):
    """Returns (lat, lon, ctrl_lat, ctrl_lon or None, line_type)."""
    code = parts[0]
    lat, lon = float(parts[1]), float(parts[2])
    if code in ('112', '114', '116'):
        clat, clon = float(parts[3]), float(parts[4])
        lt = int(parts[5]) if len(parts) > 5 and parts[5].lstrip('-').isdigit() else 0
        return (lat, lon, clat, clon, lt)
    lt = int(parts[3]) if len(parts) > 3 and parts[3].lstrip('-').isdigit() else 0
    return (lat, lon, None, None, lt)


def _gm_segment(a, b, steps=8):
    """Points after a, up to and including b. apt.dat: a node's control point shapes the curve
    leaving it; the curve arriving at a node uses the mirrored control point."""
    p0 = (a[0], a[1]); p3 = (b[0], b[1])
    c1 = (a[2], a[3]) if a[2] is not None else None
    c2 = (2 * b[0] - b[2], 2 * b[1] - b[3]) if b[2] is not None else None
    if c1 is None and c2 is None:
        return [p3]
    pts = []
    for i in range(1, steps + 1):
        t = i / steps
        u = 1 - t
        if c1 is not None and c2 is not None:
            x = u*u*u*p0[0] + 3*u*u*t*c1[0] + 3*u*t*t*c2[0] + t*t*t*p3[0]
            y = u*u*u*p0[1] + 3*u*u*t*c1[1] + 3*u*t*t*c2[1] + t*t*t*p3[1]
        else:
            c = c1 if c1 is not None else c2
            x = u*u*p0[0] + 2*u*t*c[0] + t*t*p3[0]
            y = u*u*p0[1] + 2*u*t*c[1] + t*t*p3[1]
        pts.append((x, y))
    return pts


def _gm_r(pt):
    return [round(pt[0], 6), round(pt[1], 6)]


def _gm_ring(nodes):
    if len(nodes) < 3:
        return None
    out = [(nodes[0][0], nodes[0][1])]
    n = len(nodes)
    for i in range(n):
        out.extend(_gm_segment(nodes[i], nodes[(i + 1) % n]))
    return [_gm_r(p) for p in out]


def _gm_lines(nodes, closed):
    """Split a linear feature into polylines by marking class (line type of each segment's start node)."""
    result = []
    if len(nodes) < 2:
        return result
    n = len(nodes)
    seg_count = n if closed else n - 1
    cur_cls, cur_pts = None, []
    for i in range(seg_count):
        a, b = nodes[i], nodes[(i + 1) % n]
        cls = _GM_LINE_CLASS.get(a[4])
        pts = _gm_segment(a, b)
        if cls is None:
            if cur_cls and len(cur_pts) > 1:
                result.append({"k": cur_cls, "c": [_gm_r(p) for p in cur_pts]})
            cur_cls, cur_pts = None, []
            continue
        if cls != cur_cls:
            if cur_cls and len(cur_pts) > 1:
                result.append({"k": cur_cls, "c": [_gm_r(p) for p in cur_pts]})
            cur_cls, cur_pts = cls, [(a[0], a[1])]
        cur_pts.extend(pts)
    if cur_cls and len(cur_pts) > 1:
        result.append({"k": cur_cls, "c": [_gm_r(p) for p in cur_pts]})
    return result


def _gm_dist_m(lat1, lon1, lat2, lon2):
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    y = math.radians(lat2 - lat1)
    return math.hypot(x, y) * 6371000.0


def parse_ground_map(path, pos):
    """Parse one airport block of an apt.dat starting at byte offset pos."""
    data = {"pavement": [], "lines": [], "runways": [], "stands": [], "labels": [], "boundary": [], "taxi": []}
    feat_kind, feat_nodes, feat_rings = None, [], []
    taxi_nodes, taxi_edges, runway_edges = {}, [], []
    have_1300 = False
    legacy_stands = []

    def finish():
        nonlocal feat_kind, feat_nodes, feat_rings
        if feat_kind == 'pav':
            if feat_nodes:
                feat_rings.append(feat_nodes)
            rings = [r for r in (_gm_ring(x) for x in feat_rings) if r]
            if rings:
                data["pavement"].append(rings)
        elif feat_kind == 'line' and len(feat_nodes) > 1:
            data["lines"].extend(_gm_lines(feat_nodes, False))
        feat_kind, feat_nodes, feat_rings = None, [], []

    with open(path, 'rb') as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            mm.seek(pos)
            header = mm.readline().decode('utf-8', errors='ignore').split(maxsplit=5)
            data["icao"] = header[4].upper() if len(header) > 4 else ""
            data["name"] = header[5].strip() if len(header) > 5 else data["icao"]
            try: data["elev_ft"] = float(header[1])
            except Exception: data["elev_ft"] = 0.0
            while True:
                raw = mm.readline()
                if not raw:
                    break
                parts = raw.decode('utf-8', errors='ignore').split()
                if not parts:
                    continue
                code = parts[0]
                if code in ('1', '16', '17', '99'):
                    break
                try:
                    if code in ('111', '112', '113', '114', '115', '116'):
                        if feat_kind is None:
                            continue
                        feat_nodes.append(_gm_parse_node(parts))
                        if code in ('113', '114'):
                            if feat_kind == 'pav':
                                feat_rings.append(feat_nodes)
                                feat_nodes = []
                            elif feat_kind == 'line':
                                data["lines"].extend(_gm_lines(feat_nodes, True))
                                feat_nodes = []
                            elif feat_kind == 'bnd':
                                ring = _gm_ring(feat_nodes)
                                if ring and not data["boundary"]:
                                    data["boundary"].append(ring)
                                feat_nodes = []
                            else:
                                feat_nodes = []
                        elif code in ('115', '116'):
                            if feat_kind == 'line':
                                data["lines"].extend(_gm_lines(feat_nodes, False))
                            feat_nodes = []
                        continue
                    finish()
                    if code == '110':
                        feat_kind = 'pav'
                    elif code == '120':
                        feat_kind = 'line'
                    elif code == '130':
                        feat_kind = 'bnd'
                    elif code == '100' and len(parts) >= 20:
                        data["runways"].append({
                            "w": float(parts[1]), "s": int(float(parts[2])),
                            "n1": parts[8], "lat1": float(parts[9]), "lon1": float(parts[10]), "d1": float(parts[11]),
                            "n2": parts[17], "lat2": float(parts[18]), "lon2": float(parts[19]), "d2": float(parts[20])
                        })
                    elif code == '1300' and len(parts) >= 4:
                        have_1300 = True
                        name = " ".join(parts[6:]) if len(parts) >= 7 else ""
                        data["stands"].append({"n": name, "lat": float(parts[1]), "lon": float(parts[2]), "h": float(parts[3])})
                    elif code == '15' and len(parts) >= 4:
                        legacy_stands.append({"n": " ".join(parts[4:]), "lat": float(parts[1]), "lon": float(parts[2]), "h": float(parts[3])})
                    elif code == '1201' and len(parts) >= 5:
                        taxi_nodes[parts[4]] = (float(parts[1]), float(parts[2]))
                    elif code == '1202' and len(parts) >= 6:
                        edge_name = " ".join(parts[5:]).strip()
                        if parts[4].lower().startswith('runway'):
                            runway_edges.append((parts[1], parts[2], edge_name))
                        else:
                            taxi_edges.append((parts[1], parts[2], edge_name))
                except Exception:
                    continue
            finish()
        finally:
            mm.close()

    if not have_1300:
        data["stands"] = legacy_stands

    # Taxi network segments (for "nearest taxiway"): [lat1, lon1, lat2, lon2, name, is_runway]
    for edges, is_rwy in ((taxi_edges, 0), (runway_edges, 1)):
        for n1, n2, name in edges:
            if name and n1 in taxi_nodes and n2 in taxi_nodes:
                a, b = taxi_nodes[n1], taxi_nodes[n2]
                data["taxi"].append([round(a[0], 6), round(a[1], 6), round(b[0], 6), round(b[1], 6), name, is_rwy])

    # Taxiway labels: midpoints of the longest edges of each named taxiway, spaced apart
    by_name = {}
    for n1, n2, name in taxi_edges:
        if not name or n1 not in taxi_nodes or n2 not in taxi_nodes:
            continue
        a, b = taxi_nodes[n1], taxi_nodes[n2]
        by_name.setdefault(name, []).append(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, _gm_dist_m(a[0], a[1], b[0], b[1])))
    for name, segs in by_name.items():
        segs.sort(key=lambda x: -x[2])
        placed = []
        for lat, lon, _ in segs:
            if len(placed) >= 8:
                break
            if all(_gm_dist_m(lat, lon, p[0], p[1]) > 300 for p in placed):
                placed.append((lat, lon))
                data["labels"].append({"n": name, "lat": round(lat, 6), "lon": round(lon, 6)})
    return data


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True

class PythonInterface:
    def load_config(self):
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r') as f:
                    return json.load(f)
            except Exception:
                pass
        return {"port": 8080}

    def save_config(self, conf):
        try:
            with open(self.config_file, 'w') as f:
                json.dump(conf, f)
        except Exception:
            pass

    _APT_HEADER_RE = re.compile(rb'^[ \t]*(1|16|17)[ \t]+-?[\d.]+[ \t]+\d+[ \t]+\d+[ \t]+(\S+)[ \t]*([^\r\n]*)', re.MULTILINE)
    _APT_CACHE_VERSION = 2

    def _apt_cache_file(self):
        base = getattr(self, 'plugin_dir', None)
        if not base:
            base = os.path.join(self.xp_path, "Resources", "plugins", "PythonPlugins", "ToLissWebTablet")
        return os.path.join(base, 'apt_index_cache.json')

    def _apt_signature(self, search_paths):
        sig = []
        for p in search_paths:
            try:
                st = os.stat(p)
                sig.append([p, st.st_size, int(st.st_mtime)])
            except Exception:
                sig.append([p, 0, 0])
        return sig

    def build_apt_db(self):
        t_start = time.time()
        search_paths = get_apt_search_paths(self.xp_path)
        signature = self._apt_signature(search_paths)

        # 1. Reuse the saved index if no scenery has changed since it was written
        try:
            with open(self._apt_cache_file(), 'r', encoding='utf-8') as f:
                cache = json.load(f)
            if cache.get('version') == self._APT_CACHE_VERSION and cache.get('signature') == signature:
                paths = cache['paths']
                self.apt_lite_db = [{"icao": a[0], "name": a[1]} for a in cache['airports']]
                self.apt_index = {a[0]: (paths[a[2]], a[3]) for a in cache['airports']}
                self.apt_coords = [(c[0], c[1], c[2]) for c in cache['coords']]
                self.db_ready = True
                XPLMDebugString(f"ToLiss EFB: Airport index loaded from cache ({len(self.apt_lite_db)} airports, {time.time() - t_start:.1f}s)\n")
                return
        except Exception:
            pass

        # 2. Otherwise scan the apt.dat files (regex runs at C speed over the memory-mapped file)
        airports = []      # [icao, name, path_idx, offset]
        apt_coords = []
        seen = set()
        for path_idx, p in enumerate(search_paths):
            try:
                with open(p, 'rb') as f:
                    mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
                    try:
                        headers = [(m.start(), m.group(1), m.group(2), m.group(3)) for m in self._APT_HEADER_RE.finditer(mm)]
                        for i, (pos, code, icao_b, name_b) in enumerate(headers):
                            icao = icao_b.decode('utf-8', errors='ignore').upper()
                            if icao in seen:
                                continue
                            seen.add(icao)
                            name = name_b.decode('utf-8', errors='ignore').strip() or "Airport"
                            airports.append([icao, name, path_idx, pos])
                            if code == b'1':
                                end = headers[i + 1][0] if i + 1 < len(headers) else len(mm)
                                rpos = mm.find(b'\n100 ', pos, end)
                                if rpos >= 0:
                                    line_end = mm.find(b'\n', rpos + 1, end)
                                    parts = mm[rpos + 1:line_end if line_end > 0 else end].split()
                                    try:
                                        lat = (float(parts[9]) + float(parts[18])) / 2.0
                                        lon = (float(parts[10]) + float(parts[19])) / 2.0
                                        apt_coords.append((round(lat, 6), round(lon, 6), icao))
                                    except Exception:
                                        pass
                    finally:
                        mm.close()
            except Exception:
                pass

        self.apt_lite_db = [{"icao": a[0], "name": a[1]} for a in airports]
        self.apt_index = {a[0]: (search_paths[a[2]], a[3]) for a in airports}
        self.apt_coords = apt_coords
        self.db_ready = True
        XPLMDebugString(f"ToLiss EFB: Airport index built ({len(airports)} airports, {time.time() - t_start:.1f}s)\n")

        try:
            tmp = self._apt_cache_file() + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump({"version": self._APT_CACHE_VERSION, "signature": signature, "paths": search_paths,
                           "airports": airports, "coords": [list(c) for c in apt_coords]}, f, separators=(',', ':'))
            os.replace(tmp, self._apt_cache_file())
        except Exception as e:
            XPLMDebugString(f"ToLiss EFB: Could not save airport index cache: {e}\n")

    def find_nearest_airport(self, lat, lon, max_m=8000.0):
        best, best_d = None, max_m
        coslat = math.cos(math.radians(lat))
        dlat_max = max_m / 111000.0
        for alat, alon, icao in getattr(self, 'apt_coords', []):
            if abs(alat - lat) > dlat_max:
                continue
            d = math.hypot((alon - lon) * coslat, alat - lat) * 111195.0
            if d < best_d:
                best, best_d = icao, d
        return best

    def get_ground_map(self, icao):
        cache = self.ground_map_cache
        if icao in cache:
            return cache[icao]
        loc = getattr(self, 'apt_index', {}).get(icao)
        if not loc:
            return None
        data = parse_ground_map(loc[0], loc[1])
        payload = json.dumps({"status": "success", "airport": data}, separators=(',', ':')).encode('utf-8')
        if len(cache) >= 6:
            cache.pop(next(iter(cache)))
        cache[icao] = payload
        return payload

    def _read_string_dataref(self, ref):
        if ref is None:
            return ""
        try:
            length = XPLMGetDatab(ref, None, 0, 0)
            if not length:
                return ""
            out = []
            copied = XPLMGetDatab(ref, out, 0, length)
            if copied is None:
                copied = len(out)
            raw = bytes(out[:copied])
            return raw.split(b'\x00', 1)[0].decode('utf-8', errors='ignore')
        except Exception:
            return ""

    def _write_string_dataref(self, ref, value):
        if ref is None:
            return False
        try:
            encoded = str(value).replace('\x00', '').encode('utf-8')
            capacity = XPLMGetDatab(ref, None, 0, 0)
            if capacity and capacity > 0:
                encoded = encoded[:max(0, capacity - 1)]
            payload = encoded + b'\x00'
            try:
                XPLMSetDatab(ref, list(payload), 0, len(payload))
            except Exception:
                XPLMSetDatab(ref, payload, 0, len(payload))
            return True
        except Exception as e:
            XPLMDebugString(f"ToLiss EFB: Situation name write error: {e}\n")
            return False

    def _get_situation_interface(self):
        refs = {
            "total": XPLMFindDataRef("toliss_airbus/iscsinterface/total_number_situations"),
            "index": XPLMFindDataRef("toliss_airbus/iscsinterface/current_situation_index"),
            "name": XPLMFindDataRef("toliss_airbus/iscsinterface/current_sit_name"),
            "save_name": XPLMFindDataRef("toliss_airbus/iscsinterface/sit_saving_name"),
        }
        cmds = {
            "full": XPLMFindCommand("toliss_airbus/iscsinterface/load_current_sit"),
            "config": XPLMFindCommand("toliss_airbus/iscsinterface/load_current_sit_as_config"),
            "mcdu": XPLMFindCommand("toliss_airbus/iscsinterface/load_current_sit_as_mcdu"),
            "save": XPLMFindCommand("toliss_airbus/iscsinterface/save_sit"),
        }
        return refs, cmds

    def _process_situation_requests(self):
        while not q_sit_req.empty():
            try:
                request = q_sit_req.get_nowait()
            except queue.Empty:
                break

            response_q = request.get("response")
            result = {"status": "error", "supported": False, "message": "Unknown request."}

            try:
                refs, cmds = self._get_situation_interface()
                can_list = refs["total"] is not None and refs["index"] is not None and refs["name"] is not None
                can_save = refs["save_name"] is not None and cmds["save"] is not None
                action = request.get("action")

                if action == "list":
                    if not can_list:
                        result = {"status": "error", "supported": False, "message": "ToLiss Pro features not detected. Situation interface is unavailable."}
                    else:
                        total = max(0, min(int(XPLMGetDatai(refs["total"])), 2000))
                        original_index = int(XPLMGetDatai(refs["index"]))
                        situations = []
                        try:
                            for index in range(total):
                                XPLMSetDatai(refs["index"], index)
                                name = self._read_string_dataref(refs["name"]).strip()
                                situations.append({"index": index, "name": name if name else f"Situation {index + 1}"})
                        finally:
                            if total > 0 and 0 <= original_index < total:
                                XPLMSetDatai(refs["index"], original_index)

                        result = {
                            "status": "success",
                            "supported": True,
                            "total": total,
                            "current_index": original_index,
                            "can_save": can_save,
                            "situations": situations
                        }

                elif action == "load":
                    if not can_list:
                        result = {"status": "error", "supported": False, "message": "ToLiss Pro features not detected. Situation interface is unavailable."}
                    else:
                        try:
                            index = int(request.get("index"))
                        except (TypeError, ValueError):
                            index = -1
                        mode = str(request.get("mode", "full")).lower()
                        command = cmds.get(mode)
                        total = max(0, int(XPLMGetDatai(refs["total"])))

                        if index < 0 or index >= total:
                            result = {"status": "error", "supported": True, "message": "Invalid situation index."}
                        elif command is None:
                            result = {"status": "error", "supported": True, "message": "Situation load command is unavailable."}
                        else:
                            XPLMSetDatai(refs["index"], index)
                            name = self._read_string_dataref(refs["name"]).strip()
                            XPLMCommandBegin(command)
                            self.active_commands.append(command)
                            result = {"status": "success", "supported": True, "index": index, "name": name, "mode": mode}

                elif action == "save":
                    if not can_save:
                        result = {"status": "error", "supported": can_list, "message": "Situation saving is unavailable."}
                    else:
                        name = str(request.get("name", "")).strip().replace('\x00', '')
                        if not name:
                            result = {"status": "error", "supported": True, "message": "Situation name is required."}
                        elif len(name.encode('utf-8')) > 240:
                            result = {"status": "error", "supported": True, "message": "Situation name is too long."}
                        elif not self._write_string_dataref(refs["save_name"], name):
                            result = {"status": "error", "supported": True, "message": "Could not set the situation name."}
                        else:
                            XPLMCommandBegin(cmds["save"])
                            self.active_commands.append(cmds["save"])
                            result = {"status": "success", "supported": True, "name": name}
            except Exception as e:
                XPLMDebugString(f"ToLiss EFB: Situation error: {e}\n")
                result = {"status": "error", "supported": False, "message": str(e)}

            if response_q is not None:
                try:
                    response_q.put_nowait(result)
                except queue.Full:
                    pass


    def _get_fault_interface(self):
        return {
            "check_index": XPLMFindDataRef("toliss_airbus/faultinjection/index_check_rw"),
            "check_max": XPLMFindDataRef("toliss_airbus/faultinjection/index_check_maxNum"),
            "check_name": XPLMFindDataRef("toliss_airbus/faultinjection/index_check_name"),
            "check_system": XPLMFindDataRef("toliss_airbus/faultinjection/index_check_system"),
            "state": XPLMFindDataRef("toliss_airbus/faultinjection/fault_set_clear"),
            "count": XPLMFindDataRef("toliss_airbus/faultinjection/numberOfFaults_rw"),
            "fault_index": XPLMFindDataRef("toliss_airbus/faultinjection/fault_index_rw"),
            "fault_system": XPLMFindDataRef("toliss_airbus/faultinjection/fault_system_index_ro"),
            "condition": XPLMFindDataRef("toliss_airbus/faultinjection/trigger_condition_rw"),
            "phase": XPLMFindDataRef("toliss_airbus/faultinjection/fault_phase_rw"),
            "parameter": XPLMFindDataRef("toliss_airbus/faultinjection/trigger_parameter_rw"),
            "apply": XPLMFindDataRef("toliss_airbus/faultinjection/arm_all_defined_Faults"),
            "delete": XPLMFindDataRef("toliss_airbus/faultinjection/delete_fault_index"),
        }

    def _read_int_array(self, ref, count):
        if ref is None or count <= 0:
            return []
        try:
            out = []
            XPLMGetDatavi(ref, out, 0, count)
            values = [int(v) for v in out[:count]]
            if len(values) < count:
                values.extend([0] * (count - len(values)))
            return values
        except Exception:
            return [0] * count

    def _read_float_array(self, ref, count):
        if ref is None or count <= 0:
            return []
        try:
            out = []
            XPLMGetDatavf(ref, out, 0, count)
            values = [float(v) for v in out[:count]]
            if len(values) < count:
                values.extend([0.0] * (count - len(values)))
            return values
        except Exception:
            return [0.0] * count

    def _fault_definitions(self, refs):
        max_index = max(-1, min(int(XPLMGetDatai(refs["check_max"])), 10000))
        original_index = int(XPLMGetDatai(refs["check_index"]))
        definitions = []
        try:
            for fault_index in range(max_index + 1):
                XPLMSetDatai(refs["check_index"], fault_index)
                if int(XPLMGetDatai(refs["check_index"])) != fault_index:
                    continue
                name = self._read_string_dataref(refs["check_name"]).strip()
                if not name:
                    continue
                system = int(XPLMGetDatai(refs["check_system"]))
                definitions.append({"index": fault_index, "name": name, "system": system})
        finally:
            if original_index >= 0:
                XPLMSetDatai(refs["check_index"], original_index)
        return definitions

    def _process_fault_requests(self):
        while not q_fault_req.empty():
            try:
                request = q_fault_req.get_nowait()
            except queue.Empty:
                break

            response_q = request.get("response")
            result = {"status": "error", "supported": False, "message": "Unknown request."}

            try:
                refs = self._get_fault_interface()
                required = ["check_index", "check_max", "check_name", "check_system", "state", "count", "fault_index", "fault_system", "condition", "phase", "parameter", "apply", "delete"]
                supported = all(refs[key] is not None for key in required)
                action = request.get("action")

                if not supported:
                    result = {"status": "error", "supported": False, "message": "ToLiss Pro failure interface is unavailable."}
                elif action == "list":
                    definitions = self._fault_definitions(refs)
                    by_index = {item["index"]: item for item in definitions}
                    count = max(0, min(int(XPLMGetDatai(refs["count"])), 20))
                    fault_indices = self._read_int_array(refs["fault_index"], count)
                    systems = self._read_int_array(refs["fault_system"], count)
                    conditions = self._read_int_array(refs["condition"], count)
                    phases = self._read_int_array(refs["phase"], count)
                    parameters = self._read_float_array(refs["parameter"], count)
                    states = self._read_int_array(refs["state"], count)
                    defined = []
                    for slot in range(count):
                        fault_index = fault_indices[slot]
                        definition = by_index.get(fault_index, {})
                        defined.append({
                            "slot": slot,
                            "fault_index": fault_index,
                            "name": definition.get("name", f"Fault {fault_index}"),
                            "system": systems[slot],
                            "condition": conditions[slot],
                            "phase": phases[slot],
                            "parameter": parameters[slot],
                            "state": states[slot]
                        })
                    result = {"status": "success", "supported": True, "definitions": definitions, "defined": defined, "count": count}

                elif action == "add":
                    count = max(0, min(int(XPLMGetDatai(refs["count"])), 20))
                    if count >= 20:
                        result = {"status": "error", "supported": True, "message": "Maximum of 20 defined faults reached."}
                    else:
                        try:
                            fault_index = int(request.get("fault_index"))
                            condition = int(request.get("condition", 1))
                            phase = int(request.get("phase", 5))
                            parameter = float(request.get("parameter", 0.0))
                        except (TypeError, ValueError):
                            fault_index = -1
                            condition = 0
                            phase = -1
                            parameter = 0.0

                        if condition < 1 or condition > 5:
                            result = {"status": "error", "supported": True, "message": "Invalid trigger condition."}
                        elif phase < 0 or phase > 5:
                            result = {"status": "error", "supported": True, "message": "Invalid flight phase."}
                        else:
                            original_index = int(XPLMGetDatai(refs["check_index"]))
                            XPLMSetDatai(refs["check_index"], fault_index)
                            accepted = int(XPLMGetDatai(refs["check_index"])) == fault_index
                            name = self._read_string_dataref(refs["check_name"]).strip() if accepted else ""
                            if original_index >= 0:
                                XPLMSetDatai(refs["check_index"], original_index)

                            if not accepted or not name:
                                result = {"status": "error", "supported": True, "message": "Invalid fault selection."}
                            else:
                                XPLMSetDatai(refs["count"], count + 1)
                                if int(XPLMGetDatai(refs["count"])) != count + 1:
                                    result = {"status": "error", "supported": True, "message": "Could not add the fault."}
                                else:
                                    XPLMSetDatavi(refs["fault_index"], [fault_index], count, 1)
                                    XPLMSetDatavi(refs["condition"], [condition], count, 1)
                                    XPLMSetDatavi(refs["phase"], [phase], count, 1)
                                    XPLMSetDatavf(refs["parameter"], [parameter], count, 1)
                                    XPLMSetDatavi(refs["state"], [0], count, 1)
                                    result = {"status": "success", "supported": True, "name": name, "slot": count}

                elif action == "apply":
                    XPLMSetDatai(refs["apply"], 1)
                    result = {"status": "success", "supported": True}

                elif action == "reset":
                    try:
                        slot = int(request.get("slot"))
                    except (TypeError, ValueError):
                        slot = -1
                    count = max(0, min(int(XPLMGetDatai(refs["count"])), 20))
                    if slot < 0 or slot >= count:
                        result = {"status": "error", "supported": True, "message": "Invalid fault slot."}
                    else:
                        XPLMSetDatavi(refs["state"], [0], slot, 1)
                        result = {"status": "success", "supported": True}

                elif action == "reset_all":
                    count = max(0, min(int(XPLMGetDatai(refs["count"])), 20))
                    if count > 0:
                        XPLMSetDatavi(refs["state"], [0] * count, 0, count)
                    result = {"status": "success", "supported": True}

                elif action == "delete":
                    try:
                        slot = int(request.get("slot"))
                    except (TypeError, ValueError):
                        slot = -1
                    count = max(0, min(int(XPLMGetDatai(refs["count"])), 20))
                    if slot < 0 or slot >= count:
                        result = {"status": "error", "supported": True, "message": "Invalid fault slot."}
                    else:
                        XPLMSetDatai(refs["delete"], slot)
                        result = {"status": "success", "supported": True}
            except Exception as e:
                XPLMDebugString(f"ToLiss EFB: Failure error: {e}\n")
                result = {"status": "error", "supported": False, "message": str(e)}

            if response_q is not None:
                try:
                    response_q.put_nowait(result)
                except queue.Full:
                    pass

    def _get_current_fuel_kg(self):
        generic_value = 0.0
        fuel_ref = XPLMFindDataRef("sim/flightmodel/weight/m_fuel_total")
        if fuel_ref is not None:
            try:
                generic_value = max(0.0, float(XPLMGetDataf(fuel_ref)))
            except Exception:
                generic_value = 0.0
        fob_ref = XPLMFindDataRef("AirbusFBW/WriteFOB")
        if fob_ref is not None:
            try:
                types = XPLMGetDataRefTypes(fob_ref)
                if types & xplmType_Float:
                    value = max(0.0, float(XPLMGetDataf(fob_ref)))
                elif types & xplmType_Int:
                    value = max(0.0, float(XPLMGetDatai(fob_ref)))
                elif types & xplmType_Double:
                    value = max(0.0, float(XPLMGetDatad(fob_ref)))
                else:
                    value = 0.0
                if value > 0.0 or generic_value <= 0.0:
                    return value
            except Exception:
                pass
        return generic_value

    def _set_block_fuel_kg(self, value):
        target = max(0.0, float(value))
        fob_ref = XPLMFindDataRef("AirbusFBW/WriteFOB")
        if fob_ref is not None:
            types = XPLMGetDataRefTypes(fob_ref)
            if types & xplmType_Float:
                XPLMSetDataf(fob_ref, target)
            elif types & xplmType_Int:
                XPLMSetDatai(fob_ref, int(round(target)))
            elif types & xplmType_Double:
                XPLMSetDatad(fob_ref, target)
            else:
                raise RuntimeError("AirbusFBW/WriteFOB has an unsupported dataref type.")
            return "WriteFOB"

        fuel_ref = XPLMFindDataRef("toliss_airbus/iscsinterface/setNewBlockFuel")
        if fuel_ref is not None:
            XPLMSetDataf(fuel_ref, target)
            return "setNewBlockFuel"

        raise RuntimeError("No compatible ToLiss fuel interface is available.")

    def _get_fuel_capacity_kg(self):
        icao_ref = XPLMFindDataRef("sim/aircraft/view/acf_ICAO")
        icao = self._read_string_dataref(icao_ref).strip().upper() if icao_ref is not None else ""

        if icao in ("A321", "A21N"):
            extra_ref = XPLMFindDataRef("AirbusFBW/FuelNumExtraTanks")
            lr_ref = XPLMFindDataRef("AirbusFBW/HasLRFuelPanel")
            xlr_ref = XPLMFindDataRef("AirbusFBW/HasXLRFuelPanel")
            if extra_ref is not None and lr_ref is not None and xlr_ref is not None:
                extra = max(0, int(XPLMGetDatai(extra_ref)))
                has_lr = int(XPLMGetDatai(lr_ref)) != 0
                has_xlr = int(XPLMGetDatai(xlr_ref)) != 0
                if has_xlr:
                    return 18511.0 + 10126.0 + (min(extra, 1) * 2450.0)
                if has_lr:
                    return 18511.0 + (min(extra, 3) * 2450.0)
                return 18511.0 + (min(extra, 2) * 2348.0)

        cap_ref = XPLMFindDataRef("sim/aircraft/weight/acf_m_fuel_tot")
        if cap_ref is None:
            return 0.0
        raw_capacity = float(XPLMGetDataf(cap_ref))
        if raw_capacity <= 0:
            return 0.0

        factor = 0.45359237
        empty_ref = XPLMFindDataRef("sim/aircraft/weight/acf_m_empty")
        total_ref = XPLMFindDataRef("sim/flightmodel/weight/m_total")
        current_fuel_ref = XPLMFindDataRef("sim/flightmodel/weight/m_fuel_total")
        payload_ref = XPLMFindDataRef("sim/flightmodel/weight/m_fixed")
        if empty_ref is not None and total_ref is not None and current_fuel_ref is not None and payload_ref is not None:
            raw_empty = float(XPLMGetDataf(empty_ref))
            estimated_empty = float(XPLMGetDataf(total_ref)) - float(XPLMGetDataf(current_fuel_ref)) - float(XPLMGetDataf(payload_ref))
            if raw_empty > 0 and estimated_empty > 0:
                if abs(raw_empty - estimated_empty) < abs((raw_empty * 0.45359237) - estimated_empty):
                    factor = 1.0

        return raw_capacity * factor

    def _process_payload_requests(self):
        while not q_payload_apply.empty():
            try:
                request = q_payload_apply.get_nowait()
            except queue.Empty:
                break

            response_q = request.get("response")
            result = {"status": "error", "message": "Payload request failed."}

            try:
                pax = request.get("pax")
                cargo_fwd = request.get("cargo_fwd")
                cargo_aft = request.get("cargo_aft")
                fuel = request.get("fuel")

                payload_requested = pax is not None or cargo_fwd is not None or cargo_aft is not None
                if payload_requested:
                    if pax is None or cargo_fwd is None or cargo_aft is None:
                        raise ValueError("PAX, FWD cargo and AFT cargo are required together.")

                    pax_ref = XPLMFindDataRef("AirbusFBW/NoPax")
                    fwd_ref = XPLMFindDataRef("AirbusFBW/FwdCargo")
                    aft_ref = XPLMFindDataRef("AirbusFBW/AftCargo")
                    apply_cmd = XPLMFindCommand("AirbusFBW/SetWeightAndCG")
                    if pax_ref is None or fwd_ref is None or aft_ref is None or apply_cmd is None:
                        raise RuntimeError("ToLiss payload interface is unavailable.")

                    XPLMSetDataf(pax_ref, float(pax))
                    XPLMSetDataf(fwd_ref, float(cargo_fwd))
                    XPLMSetDataf(aft_ref, float(cargo_aft))
                    XPLMCommandBegin(apply_cmd)
                    self.active_commands.append(apply_cmd)

                fuel_requested = None
                fuel_applied = None
                fuel_max = self._get_fuel_capacity_kg()
                fuel_clamped = False
                if fuel is not None:
                    fuel_requested = max(0.0, float(fuel))
                    target_fuel = min(fuel_requested, fuel_max) if fuel_max > 0 else fuel_requested
                    fuel_clamped = target_fuel < fuel_requested
                    fuel_interface = self._set_block_fuel_kg(target_fuel)
                    actual_fuel = self._get_current_fuel_kg()
                    fuel_applied = actual_fuel if abs(actual_fuel - target_fuel) <= 100.0 else target_fuel

                result = {
                    "status": "success",
                    "fuel_requested": fuel_requested,
                    "fuel_applied": fuel_applied,
                    "fuel_max": fuel_max if fuel_max > 0 else None,
                    "fuel_clamped": fuel_clamped
                }
            except Exception as e:
                XPLMDebugString(f"ToLiss EFB: Payload apply error: {e}\n")
                result = {"status": "error", "message": str(e)}

            if response_q is not None:
                try:
                    response_q.put_nowait(result)
                except queue.Full:
                    pass

    def XPluginStart(self):
        self.Name = "ToLiss EFB"
        self.Sig = "soarbywire.tolissefb"
        self.Desc = "Native Web interface for ToLiss EFB"

        self.xp_path = XPLMGetSystemPath()
        self.plugin_dir = os.path.join(self.xp_path, "Resources", "plugins", "PythonPlugins", "ToLissWebTablet")
        # read the saved settings first: the flight recorder, updater and ops centre use them when created
        self.config_file = os.path.join(self.plugin_dir, "config.json")
        self.config = self.load_config()
        self.fdr = FlightRecorder(self)
        self.updater = Updater(self)
        self.ops = OpsCentre(self)
        threading.Thread(target=self.updater.check, daemon=True).start()
        # Index X-Plane's waypoint/navaid data in the background so approach lookups are instant
        try:
            self.navdb = NavDatabase(self.xp_path, self.plugin_dir)
            self.navdb.load_async()
        except Exception as e:
            XPLMDebugString(f"ToLiss EFB: navigation preload failed: {e}\n")
        self.sit_dir_toliss = os.path.join(self.xp_path, "Resources", "plugins", "ToLissData", "Situations")
        
        try:
            if not os.path.exists(self.plugin_dir):
                os.makedirs(self.plugin_dir)
            if not os.path.exists(self.sit_dir_toliss): 
                os.makedirs(self.sit_dir_toliss)
        except Exception as e:
            XPLMDebugString(f"ToLiss EFB ERROR: Could not create plugin directories: {e}\n")

        self.httpd = None
        self.server_thread = None
        self.active_commands = [] 
        
        self.apt_lite_db = []
        self.apt_index = {}
        self.apt_coords = []
        self.ground_map_cache = {}
        self.db_ready = False
        threading.Thread(target=self.build_apt_db, daemon=True).start()
        self.navdb = None
        
        try:
            self.probe = XPLMCreateProbe(xplm_ProbeY)
        except Exception:
            self.probe = None

        self.vatsim_thread = threading.Thread(target=fetch_vatsim_loop)
        self.vatsim_thread.daemon = True
        self.vatsim_thread.start()

        class TabletHandler(http.server.SimpleHTTPRequestHandler):
            def do_GET(req):
                try:
                    if req.path == '/' or req.path == '/index.html':
                        filepath = os.path.join(TabletHandler.plugin_ref.plugin_dir, 'index.html')
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
                        response_q = queue.Queue(maxsize=1)
                        q_sit_req.put({"action": "list", "response": response_q})
                        try:
                            result = response_q.get(timeout=3.0)
                            req.send_response(200 if result.get("status") == "success" else 503)
                            req.send_header('Content-type', 'application/json')
                            req.end_headers()
                            req.wfile.write(json.dumps(result).encode('utf-8'))
                        except queue.Empty:
                            req.send_response(504)
                            req.send_header('Content-type', 'application/json')
                            req.end_headers()
                            req.wfile.write(json.dumps({"status": "error", "supported": False, "message": "Timed out waiting for X-Plane."}).encode('utf-8'))

                    elif req.path == '/api/toliss/failures':
                        response_q = queue.Queue(maxsize=1)
                        q_fault_req.put({"action": "list", "response": response_q})
                        try:
                            result = response_q.get(timeout=5.0)
                            req.send_response(200 if result.get("status") == "success" else 503)
                            req.send_header('Content-type', 'application/json')
                            req.end_headers()
                            req.wfile.write(json.dumps(result).encode('utf-8'))
                        except queue.Empty:
                            req.send_response(504)
                            req.send_header('Content-type', 'application/json')
                            req.end_headers()
                            req.wfile.write(json.dumps({"status": "error", "supported": False, "message": "Timed out waiting for X-Plane."}).encode('utf-8'))

                    elif req.path == '/api/settings':
                        nav_cycle = "AIRAC Unknown"
                        custom_cycle = os.path.join(TabletHandler.plugin_ref.xp_path, "Custom Data", "cycle.json")
                        default_cycle = os.path.join(TabletHandler.plugin_ref.xp_path, "Resources", "default data", "cycle.json")
                        
                        cycle_path = custom_cycle if os.path.exists(custom_cycle) else default_cycle
                        if os.path.exists(cycle_path):
                            try:
                                with open(cycle_path, 'r') as cf:
                                    cdata = json.load(cf)
                                    nav_cycle = f"AIRAC {cdata.get('cycle', '')}"
                            except Exception: pass
                        elif os.path.exists(os.path.join(TabletHandler.plugin_ref.xp_path, "Custom Data", "cycle_info.txt")):
                            try:
                                with open(os.path.join(TabletHandler.plugin_ref.xp_path, "Custom Data", "cycle_info.txt"), 'r') as cf:
                                    lines = cf.readlines()
                                    if len(lines) > 0:
                                        nav_cycle = lines[0].strip()
                            except Exception: pass

                        req.send_response(200)
                        req.send_header('Content-type', 'application/json')
                        req.end_headers()
                        req.wfile.write(json.dumps({
                            "ip": get_local_ip(), 
                            "port": TabletHandler.plugin_ref.config.get("port", 8080),
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
                        ready = getattr(TabletHandler.plugin_ref, 'db_ready', False)
                        if q and ready:
                            exact, starts, contains = [], [], []
                            for apt in TabletHandler.plugin_ref.apt_lite_db:
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
                        plugin = TabletHandler.plugin_ref
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
                                    XPLMDebugString(f"ToLiss EFB: ground map error for {icao}: {e}\n")
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

                            plugin = TabletHandler.plugin_ref
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
                                        ops = getattr(TabletHandler.plugin_ref, 'ops', None)
                                        if ops:
                                            ops.set_ofp(json.loads(data.decode('utf-8')), user)
                                    except Exception as e:
                                        XPLMDebugString(f"ToLiss EFB: ops could not use the SimBrief plan: {e}\n")
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
                        q_telem_req.put(["sim/flightmodel/position/latitude", "sim/flightmodel/position/longitude"])
                        lat = 0.0
                        lon = 0.0
                        try:
                            res = q_telem_res.get(timeout=1.0)
                            lat = res.get("sim/flightmodel/position/latitude", 0.0)
                            lon = res.get("sim/flightmodel/position/longitude", 0.0)
                        except Exception: pass
                        
                        vatsim = vatsim_cache.get("data")
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
                        plugin = TabletHandler.plugin_ref
                        try:
                            if getattr(plugin, 'navdb', None) is None:
                                plugin.navdb = NavDatabase(plugin.xp_path, getattr(plugin, 'plugin_dir', None))
                            body = plugin.navdb.procedures(icao) if icao else None
                            if body is None:
                                body = {"status": "none", "message": f"No approach procedures found for {icao} in X-Plane's navigation data."}
                        except Exception as e:
                            XPLMDebugString(f"ToLiss EFB: procedures error for {icao}: {e}\n")
                            body = {"status": "error", "message": str(e)}
                        req.send_response(200)
                        req.send_header('Content-type', 'application/json')
                        req.end_headers()
                        req.wfile.write(json.dumps(body, separators=(',', ':')).encode('utf-8'))

                    elif req.path.startswith('/api/read'):
                        # Read a handful of datarefs (used to check automated cockpit actions)
                        qs = urllib.parse.parse_qs(urllib.parse.urlparse(req.path).query)
                        names = [n for n in qs.get("d", [""])[0].split(",") if n][:30]
                        response_q = queue.Queue(maxsize=1)
                        q_telem_req.put((names, response_q))
                        try:
                            vals = response_q.get(timeout=1.5)
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
                        response_q = queue.Queue(maxsize=1)
                        q_mcdu_req.put((side, response_q))
                        try:
                            body = response_q.get(timeout=1.5)
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
                        response_q = queue.Queue(maxsize=1)
                        q_telem_req.put((drefs, response_q))
                        try:
                            vals = response_q.get(timeout=1.5)
                        except Exception:
                            vals = {}
                        lat = float(vals.get("sim/flightmodel/position/latitude", 0) or 0)
                        lon = float(vals.get("sim/flightmodel/position/longitude", 0) or 0)
                        agl_ft = float(vals.get("sim/flightmodel/position/y_agl", 0) or 0) * 3.28084
                        nearby = vatsim_nearby_atc(lat, lon, agl_ft) if (lat or lon) else []
                        body = {"status": "success", "xpilot": vals, "nearby": nearby,
                                "vatsim_age_s": round(time.time() - vatsim_cache.get("last_fetch", 0)) if vatsim_cache.get("last_fetch") else None}
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
                        q_state_req.put(drefs)
                        try:
                            results = q_state_res.get(timeout=1.0)
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
                            "toliss_web/local_traffic"
                        ]
                        q_telem_req.put(drefs)
                        try:
                            results = q_telem_res.get(timeout=1.0)
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
                        q_payload_req.put(drefs)
                        try:
                            results = q_payload_res.get(timeout=1.5)
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
                        plugin = TabletHandler.plugin_ref
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

                    elif req.path.startswith('/api/ops/'):
                        ops = getattr(TabletHandler.plugin_ref, 'ops', None)
                        parsed = urllib.parse.urlparse(req.path)
                        qs = urllib.parse.parse_qs(parsed.query)
                        if parsed.path == '/api/ops/status':
                            body = {"status": "success", **ops.status()} if ops else {"status": "error"}
                        elif parsed.path == '/api/ops/config':
                            # the saved setup, to fill in the Ops Centre page (served only on your local network)
                            body = {"status": "success", "logon": ops.logon, "ops_callsign": ops.ops_callsign,
                                    "aircraft": ops.aircraft, "enabled": ops.enabled} if ops else {"status": "error"}
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
                            items = [m for m in ops.log if m["id"] > since or (m.get("rev") and m["t"] >= upd)] if ops else []
                            if ops and qs.get("read", ["0"])[0] == "1":
                                ops.unread = 0
                            body = {"status": "success", "messages": items}
                        else:
                            body = {"status": "error", "message": "Unknown request"}
                        req.send_response(200)
                        req.send_header('Content-type', 'application/json')
                        req.end_headers()
                        req.wfile.write(json.dumps(body).encode('utf-8'))

                    elif req.path.startswith('/api/update/status'):
                        up = getattr(TabletHandler.plugin_ref, 'updater', None)
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
                        plugin = TabletHandler.plugin_ref
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
                            d = checklist_dir(TabletHandler.plugin_ref.plugin_dir)
                            for f in sorted(os.listdir(d)):
                                if f.lower().endswith(".xml"):
                                    st = os.stat(os.path.join(d, f))
                                    items.append({"name": f[:-4], "size": st.st_size, "modified": int(st.st_mtime)})
                        except Exception as e:
                            XPLMDebugString(f"ToLiss EFB: checklist list error: {e}\n")
                        req.send_response(200)
                        req.send_header('Content-type', 'application/json')
                        req.end_headers()
                        req.wfile.write(json.dumps({"status": "success", "checklists": items}).encode('utf-8'))

                    elif req.path.startswith('/api/get_checklist?') and 'custom=' in req.path:
                        qs = urllib.parse.parse_qs(urllib.parse.urlparse(req.path).query)
                        name = checklist_safe_name(qs.get("custom", [""])[0])
                        path = os.path.join(checklist_dir(TabletHandler.plugin_ref.plugin_dir), name + ".xml")
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
                        q_path_req.put("get_path")
                        try:
                            acf_path = q_path_res.get(timeout=2.0)
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
                    XPLMDebugString(f"ToLiss EFB GET Error: {e}\n")

            def do_POST(req):
                try:
                    content_length = int(req.headers.get('Content-Length', 0))
                    post_data = req.rfile.read(content_length) if content_length > 0 else b'{}'
                    data = json.loads(post_data)

                    if req.path == '/api/settings':
                        new_port = data.get('port')
                        if new_port:
                            TabletHandler.plugin_ref.config["port"] = int(new_port)
                            TabletHandler.plugin_ref.save_config(TabletHandler.plugin_ref.config)
                            threading.Timer(0.5, TabletHandler.plugin_ref.restart_server_async).start()
                        req.send_response(200)
                        req.end_headers()
                        req.wfile.write(b'{"status":"success"}')

                    elif req.path == '/command':
                        if data.get('command'):
                            q_cmd.put(data.get('command'))
                        req.send_response(200)
                        req.end_headers()
                        req.wfile.write(b'{"status":"success"}')

                    elif req.path == '/dataref':
                        dref = data.get('dataref')
                        val = data.get('value')
                        if dref and val is not None:
                            q_dref_w.put((dref, float(val)))
                        req.send_response(200)
                        req.end_headers()
                        req.wfile.write(b'{"status":"success"}')
                        
                    elif req.path == '/api/toliss/payload/apply':
                        response_q = queue.Queue(maxsize=1)
                        q_payload_apply.put({
                            "pax": data.get("pax"),
                            "cargo_fwd": data.get("cargo_fwd"),
                            "cargo_aft": data.get("cargo_aft"),
                            "fuel": data.get("fuel"),
                            "response": response_q
                        })
                        try:
                            result = response_q.get(timeout=2.0)
                            req.send_response(200 if result.get("status") == "success" else 400)
                            req.send_header('Content-type', 'application/json')
                            req.end_headers()
                            req.wfile.write(json.dumps(result).encode('utf-8'))
                        except queue.Empty:
                            req.send_response(504)
                            req.send_header('Content-type', 'application/json')
                            req.end_headers()
                            req.wfile.write(json.dumps({"status": "error", "message": "Timed out waiting for X-Plane."}).encode('utf-8'))

                    elif req.path in ('/api/ops/config', '/api/ops/send', '/api/ops/poll', '/api/ops/clear', '/api/ops/auto', '/api/ops/action', '/api/ops/stand'):
                        ops = getattr(TabletHandler.plugin_ref, 'ops', None)
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
                        plugin = TabletHandler.plugin_ref
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
                                response_q = queue.Queue(maxsize=1)
                                q_telem_req.put((["sim/flightmodel/failures/onground_any", "sim/flightmodel/position/groundspeed"], response_q))
                                try:
                                    v = response_q.get(timeout=1.5)
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
                        fdr = getattr(TabletHandler.plugin_ref, 'fdr', None)
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
                        plugin = TabletHandler.plugin_ref
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
                                path = os.path.join(checklist_dir(TabletHandler.plugin_ref.plugin_dir), name + ".xml")
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
                        path = os.path.join(checklist_dir(TabletHandler.plugin_ref.plugin_dir), name + ".xml")
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
                            q_mcdu_keys.put((side, k))
                        req.send_response(200)
                        req.send_header('Content-type', 'application/json')
                        req.end_headers()
                        req.wfile.write(json.dumps({"status": "success", "queued": len(accepted)}).encode('utf-8'))
                        return

                    elif req.path == '/api/teleport':
                        if data.get('lat') and data.get('lon'):
                            q_teleport.put(data)
                        req.send_response(200)
                        req.end_headers()
                        req.wfile.write(b'{"status":"success"}')

                    elif req.path == '/api/toliss/situations/load':
                        response_q = queue.Queue(maxsize=1)
                        q_sit_req.put({
                            "action": "load",
                            "index": data.get('index'),
                            "mode": data.get('mode', 'full'),
                            "response": response_q
                        })
                        try:
                            result = response_q.get(timeout=3.0)
                            req.send_response(200 if result.get("status") == "success" else 400)
                            req.send_header('Content-type', 'application/json')
                            req.end_headers()
                            req.wfile.write(json.dumps(result).encode('utf-8'))
                        except queue.Empty:
                            req.send_response(504)
                            req.send_header('Content-type', 'application/json')
                            req.end_headers()
                            req.wfile.write(json.dumps({"status": "error", "message": "Timed out waiting for X-Plane."}).encode('utf-8'))

                    elif req.path == '/api/toliss/situations/save':
                        response_q = queue.Queue(maxsize=1)
                        q_sit_req.put({
                            "action": "save",
                            "name": data.get('name', ''),
                            "response": response_q
                        })
                        try:
                            result = response_q.get(timeout=3.0)
                            req.send_response(200 if result.get("status") == "success" else 400)
                            req.send_header('Content-type', 'application/json')
                            req.end_headers()
                            req.wfile.write(json.dumps(result).encode('utf-8'))
                        except queue.Empty:
                            req.send_response(504)
                            req.send_header('Content-type', 'application/json')
                            req.end_headers()
                            req.wfile.write(json.dumps({"status": "error", "message": "Timed out waiting for X-Plane."}).encode('utf-8'))

                    elif req.path == '/api/toliss/failures':
                        response_q = queue.Queue(maxsize=1)
                        q_fault_req.put({
                            "action": data.get('action'),
                            "fault_index": data.get('fault_index'),
                            "condition": data.get('condition'),
                            "phase": data.get('phase'),
                            "parameter": data.get('parameter'),
                            "slot": data.get('slot'),
                            "response": response_q
                        })
                        try:
                            result = response_q.get(timeout=5.0)
                            req.send_response(200 if result.get("status") == "success" else 400)
                            req.send_header('Content-type', 'application/json')
                            req.end_headers()
                            req.wfile.write(json.dumps(result).encode('utf-8'))
                        except queue.Empty:
                            req.send_response(504)
                            req.send_header('Content-type', 'application/json')
                            req.end_headers()
                            req.wfile.write(json.dumps({"status": "error", "message": "Timed out waiting for X-Plane."}).encode('utf-8'))

                    elif req.path == '/load_file':
                        filename = data.get('filename', '').strip()
                        if filename:
                            q_load_sit.put(filename)
                        req.send_response(200)
                        req.end_headers()
                        req.wfile.write(b'{"status":"success"}')
                        
                    else:
                        req.send_response(404)
                        req.end_headers()
                except Exception as e:
                    XPLMDebugString(f"ToLiss EFB POST Error: {e}\n")
                    req.send_response(500)
                    req.end_headers()

        TabletHandler.plugin_ref = self
        self.handler_class = TabletHandler
        self.start_server_thread()
        self.flCB = self.flightLoopCallback
        XPLMRegisterFlightLoopCallback(self.flCB, -1.0, 0)
        return self.Name, self.Sig, self.Desc

    def XPluginStop(self):
        try:
            if getattr(self, 'fdr', None) and self.fdr.recording:
                self.fdr.stop("X-Plane closing")
        except Exception:
            pass
        XPLMUnregisterFlightLoopCallback(self.flCB, 0)
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        if getattr(self, 'probe', None) is not None:
            XPLMDestroyProbe(self.probe)

    def XPluginEnable(self):
        return 1

    def XPluginDisable(self):
        pass

    def XPluginReceiveMessage(self, inFromWho, inMessage, inParam):
        pass

    def process_dref_read_queue(self, req_q, res_q):
        while not req_q.empty():
            try:
                item = req_q.get_nowait()
                # A request can carry its own reply queue, so parallel requests never receive each other's results
                if isinstance(item, tuple):
                    drefs, reply_q = item
                else:
                    drefs, reply_q = item, res_q
                results = {}
                missing = []
                for d in drefs:
                    try:
                        if d == "toliss_web/max_fuel_kg":
                            results[d] = self._get_fuel_capacity_kg()
                            continue

                        if d == "toliss_web/current_fuel_kg":
                            results[d] = self._get_current_fuel_kg()
                            continue

                        if d == "toliss_web/local_traffic":
                            mp_list = []
                            for i in range(1, 20):
                                x_ref = XPLMFindDataRef(f"sim/multiplayer/position/plane{i}_x")
                                y_ref = XPLMFindDataRef(f"sim/multiplayer/position/plane{i}_y")
                                z_ref = XPLMFindDataRef(f"sim/multiplayer/position/plane{i}_z")
                                psi_ref = XPLMFindDataRef(f"sim/multiplayer/position/plane{i}_psi")
                                
                                if x_ref and y_ref and z_ref:
                                    x = XPLMGetDataf(x_ref)
                                    y = XPLMGetDataf(y_ref)
                                    z = XPLMGetDataf(z_ref)
                                    psi = XPLMGetDataf(psi_ref) if psi_ref else 0.0

                                    if abs(x) > 0.1 and abs(z) > 0.1: 
                                        world = XPLMLocalToWorld(x, y, z)
                                        if world:
                                            lat, lon, alt_m = world[0], world[1], world[2]
                                            callsign = f"TRFC{i}"
                                            
                                            ref_id = XPLMFindDataRef("sim/cockpit2/tcas/targets/flight_id")
                                            if ref_id:
                                                out_b = []
                                                l = XPLMGetDatab(ref_id, out_b, 0, 512)
                                                if l > 0:
                                                    id_str = bytes(out_b).decode('utf-8', errors='ignore')
                                                    if len(id_str) >= (i*8)+8:
                                                        cs = id_str[i*8:(i*8)+8].replace('\x00', '').strip()
                                                        if cs: callsign = cs

                                            mp_list.append({
                                                "lat": lat, "lon": lon, "alt": int(alt_m * 3.28084),
                                                "hdg": int(psi), "callsign": callsign
                                            })
                            results[d] = mp_list
                            continue

                        match = re.match(r"(.+)\[(\d+)\]", d)
                        if match:
                            dref_str = match.group(1)
                            idx = int(match.group(2))
                            ref = XPLMFindDataRef(dref_str)
                            if ref:
                                types = XPLMGetDataRefTypes(ref)
                                if types & xplmType_FloatArray:
                                    arr_len = XPLMGetDatavf(ref, None, 0, 0)
                                    if idx < arr_len:
                                        out = []
                                        XPLMGetDatavf(ref, out, idx, 1)
                                        results[d] = out[0] if out else 0.0
                                    else:
                                        results[d] = 0.0
                                elif types & xplmType_IntArray:
                                    arr_len = XPLMGetDatavi(ref, None, 0, 0)
                                    if idx < arr_len:
                                        out = []
                                        XPLMGetDatavi(ref, out, idx, 1)
                                        results[d] = float(out[0]) if out else 0.0
                                    else:
                                        results[d] = 0.0
                                else:
                                    results[d] = 0.0
                            else:
                                results[d] = 0.0
                        else:
                            ref = XPLMFindDataRef(d)
                            if ref:
                                types = XPLMGetDataRefTypes(ref)
                                if types & xplmType_FloatArray:
                                    arr_len = XPLMGetDatavf(ref, None, 0, 0)
                                    if arr_len > 0:
                                        if arr_len > 64: arr_len = 64
                                        out = []
                                        XPLMGetDatavf(ref, out, 0, arr_len)
                                        results[d] = out
                                    else: results[d] = []
                                elif types & xplmType_IntArray:
                                    # whole-number lists, e.g. sim/flightmodel/engine/ENGN_running
                                    arr_len = XPLMGetDatavi(ref, None, 0, 0)
                                    if arr_len > 0:
                                        if arr_len > 64: arr_len = 64
                                        out = []
                                        XPLMGetDatavi(ref, out, 0, arr_len)
                                        results[d] = [float(x) for x in out]
                                    else: results[d] = []
                                elif types & xplmType_Data:
                                    arr_len = XPLMGetDatab(ref, None, 0, 0)
                                    if arr_len > 0:
                                        out = []
                                        XPLMGetDatab(ref, out, 0, arr_len)
                                        results[d] = bytes(out).decode('utf-8', errors='ignore').rstrip('\x00')
                                    else:
                                        results[d] = ""
                                elif types & xplmType_Float:
                                    results[d] = XPLMGetDataf(ref)
                                elif types & xplmType_Int:
                                    results[d] = float(XPLMGetDatai(ref))
                                elif types & xplmType_Double:
                                    results[d] = float(XPLMGetDatad(ref))
                                else:
                                    results[d] = 0.0
                            else:
                                results[d] = 0.0
                                missing.append(d)
                    except Exception:
                        results[d] = 0.0
                if missing:
                    results["__missing__"] = missing
                reply_q.put(results)
            except Exception:
                pass

    # ===== Cockpit displays =====
    def _display_snapshot(self, with_text):
        snap = {"t": round(time.time(), 3)}
        for key, (dref, idx) in DISPLAY_VALUES.items():
            if self._dref(dref) is None:
                continue
            v = self._get_f(dref, None)
            if v is not None:
                snap[key] = round(v, 3)
        n_eng = int(snap.get("engines", 2) or 2)
        n_eng = max(1, min(4, n_eng))
        for key, dref in ENGINE_ARRAYS.items():
            ref = self._dref(dref)
            if ref is None:
                continue
            try:
                out = []
                types = XPLMGetDataRefTypes(ref)
                if types & xplmType_FloatArray:
                    XPLMGetDatavf(ref, out, 0, n_eng)
                elif types & xplmType_IntArray:
                    XPLMGetDatavi(ref, out, 0, n_eng)
                snap[key] = [round(float(x), 3) for x in out[:n_eng]]
            except Exception:
                pass
        for key, (dref, n) in SD_ARRAYS.items():
            ref = self._dref(dref)
            if ref is None:
                continue
            try:
                out = []
                types = XPLMGetDataRefTypes(ref)
                if types & xplmType_FloatArray:
                    XPLMGetDatavf(ref, out, 0, n)
                elif types & xplmType_IntArray:
                    XPLMGetDatavi(ref, out, 0, n)
                snap[key] = [round(float(x), 2) for x in out[:n]]
            except Exception:
                pass
        if with_text:
            # FMA (3 lines) and E/WD messages (7 lines), each as text plus per-character colours
            fma = []
            for line in (1, 2, 3):
                parts = []
                for c in "wgbma":
                    ref = self._dref(f"AirbusFBW/FMA{line}{c}")
                    if ref is not None:
                        parts.append((self._read_bytes(ref, 64), c))
                fma.append(merge_coloured(parts, 64))
            snap["fma"] = fma
            # SD text lines (e.g. the STATUS page)
            sdl = []
            for line in range(1, 19):
                parts = []
                for c in "wgbarm":
                    ref = self._dref(f"AirbusFBW/SDline{line}{c}")
                    if ref is not None:
                        parts.append((self._read_bytes(ref, 40), c))
                if parts:
                    sdl.append(merge_coloured(parts, 40))
            snap["sdl"] = sdl
            ewd = []
            for line in range(1, 8):
                parts = []
                for c in "wgbar":
                    ref = self._dref(f"AirbusFBW/EWD{line}{c}Text")
                    if ref is not None:
                        parts.append((self._read_bytes(ref, 48), c))
                ewd.append(merge_coloured(parts, 48))
            snap["ewd"] = ewd
        return snap

    def _update_stream(self):
        if getattr(self, 'stream_clients', 0) <= 0:
            return
        now = time.time()
        if now - getattr(self, '_stream_last', 0) < 1.0 / STREAM_RATE_HZ:
            return
        self._stream_last = now
        with_text = now - getattr(self, '_stream_text_last', 0) >= 0.25
        snap = self._display_snapshot(with_text)
        if with_text:
            self._stream_text_last = now
            self._stream_text = {"fma": snap.get("fma"), "ewd": snap.get("ewd"), "sdl": snap.get("sdl")}
        else:
            snap.update(getattr(self, '_stream_text', {}) or {})
        snap["seq"] = getattr(self, 'stream_seq', 0) + 1
        self.stream_seq = snap["seq"]
        self.stream_snapshot = snap

    # ===== ToLiss MCDU mirror =====
    def _read_bytes(self, ref, n=MCDU_COLS):
        try:
            out = []
            copied = XPLMGetDatab(ref, out, 0, n)
            if copied is None:
                copied = len(out)
            return [int(b) & 0xFF for b in out[:copied]]
        except Exception:
            return []

    def _mcdu_screen(self, side):
        grid = [[[" ", "w", False] for _ in range(MCDU_COLS)] for _ in range(14)]
        found = 0
        missing = []
        refs = mcdu_display_refs(side)
        cache = self.__dict__.setdefault('_mcdu_ref_cache', {})
        for name, line, colour, small in refs:
            if name not in cache:
                cache[name] = XPLMFindDataRef(name)
            ref = cache[name]
            if ref is None:
                missing.append(name.replace(f"AirbusFBW/MCDU{side}", ""))
                continue
            found += 1
            raw = self._read_bytes(ref)
            for i, b in enumerate(raw[:MCDU_COLS]):
                if b == 0:
                    if line == 13:
                        break      # scratchpad text ends at the first zero byte
                    continue
                if b == 0x20:
                    continue
                ch, col, sm = chr(b), colour, small
                if colour == "s":
                    ch, col = MCDU_SYMBOLS.get(ch, (ch, "m"))
                elif ch == "`":
                    ch = "\u00b0"
                grid[line][i] = [ch, col, sm]
        return {"status": "success", "side": side, "found": found, "total": len(refs), "missing": missing,
                "lines": ["".join(c[0] for c in row) for row in grid],
                "colours": ["".join(c[1] for c in row) for row in grid],
                "small": ["".join("1" if c[2] else "0" for c in row) for row in grid]}

    def _process_mcdu(self):
        # Key presses: begin a key, end it on the next frame, then take the next one
        active = getattr(self, '_mcdu_key_active', None)
        if active is not None:
            try:
                XPLMCommandEnd(active)
            except Exception:
                pass
            self._mcdu_key_active = None
        elif not q_mcdu_keys.empty():
            try:
                side, key = q_mcdu_keys.get_nowait()
                cmd = XPLMFindCommand(f"AirbusFBW/MCDU{side}{key}")
                if cmd:
                    XPLMCommandBegin(cmd)
                    self._mcdu_key_active = cmd
                else:
                    XPLMDebugString(f"ToLiss EFB: MCDU key not found: AirbusFBW/MCDU{side}{key}\n")
            except Exception:
                pass
        # Screen reads
        while not q_mcdu_req.empty():
            try:
                side, reply_q = q_mcdu_req.get_nowait()
                reply_q.put(self._mcdu_screen(side))
            except Exception as e:
                XPLMDebugString(f"ToLiss EFB: MCDU read error: {e}\n")

    # ===== Aircraft repositioning =====
    # X-Plane moves an aircraft through its local OpenGL coordinates (local_x/y/z) and its
    # orientation quaternion (q). Latitude/longitude/elevation are read-only, and psi/theta/phi
    # are rebuilt from q every frame, so writing those alone gives wrong headings or attitudes.

    def _dref(self, name):
        cache = self.__dict__.setdefault('_dref_cache', {})
        if name not in cache:
            cache[name] = XPLMFindDataRef(name)
        return cache[name]

    def _get_f(self, name, default=0.0):
        ref = self._dref(name)
        if ref is None:
            return default
        try:
            types = XPLMGetDataRefTypes(ref)
            if types & xplmType_Double:
                return float(XPLMGetDatad(ref))
            if types & xplmType_Float:
                return float(XPLMGetDataf(ref))
            if types & xplmType_Int:
                return float(XPLMGetDatai(ref))
        except Exception:
            pass
        return default

    def _set_num(self, name, value):
        ref = self._dref(name)
        if ref is None:
            return False
        try:
            types = XPLMGetDataRefTypes(ref)
            if types & xplmType_Double:
                XPLMSetDatad(ref, float(value))
            elif types & xplmType_Float:
                XPLMSetDataf(ref, float(value))
            elif types & xplmType_Int:
                XPLMSetDatai(ref, int(value))
            else:
                return False
            return True
        except Exception:
            return False

    def _probe_terrain_y(self, x, y, z):
        """Local Y of the ground (runway/pavement or terrain) below x,z, or None if not available."""
        try:
            if getattr(self, 'terrain_probe', None) is None:
                self.terrain_probe = XPLMCreateProbe(xplm_ProbeY)
            info = XPLMProbeTerrainXYZ(self.terrain_probe, x, y, z)
            result = getattr(info, 'result', None)
            if result == xplm_ProbeHitTerrain:
                return float(info.locationY)
        except Exception as e:
            XPLMDebugString(f"ToLiss EFB: Terrain probe unavailable: {e}\n")
        return None

    @staticmethod
    def _euler_to_quat(heading_deg, pitch_deg, roll_deg):
        """X-Plane's convention (Laminar 'Moving the plane' tech note)."""
        psi = math.pi / 360.0 * heading_deg
        theta = math.pi / 360.0 * pitch_deg
        phi = math.pi / 360.0 * roll_deg
        return [
            math.cos(psi) * math.cos(theta) * math.cos(phi) + math.sin(psi) * math.sin(theta) * math.sin(phi),
            math.cos(psi) * math.cos(theta) * math.sin(phi) - math.sin(psi) * math.sin(theta) * math.cos(phi),
            math.cos(psi) * math.sin(theta) * math.cos(phi) + math.sin(psi) * math.cos(theta) * math.sin(phi),
            -math.cos(psi) * math.sin(theta) * math.sin(phi) + math.sin(psi) * math.cos(theta) * math.cos(phi),
        ]

    def _sim_pause(self, on):
        try:
            cmd = XPLMFindCommand("sim/operation/pause_on" if on else "sim/operation/pause_off")
            if cmd:
                XPLMCommandOnce(cmd)
        except Exception as e:
            XPLMDebugString(f"ToLiss EFB: Pause command failed: {e}\n")

    def _begin_teleport(self, tp):
        """X-Plane ignores position changes made while paused (the flight model puts the aircraft back
        when it next runs), so unpause for the move and pause again afterwards."""
        tp["was_paused"] = self._get_f("sim/time/paused") >= 1
        if tp["was_paused"]:
            self._sim_pause(False)
            tp["next_phase"] = tp["phase"]
            tp["phase"] = "resume"
            tp["resume_frames"] = 0
        self.teleport = tp

    def _run_command(self, name):
        try:
            cmd = XPLMFindCommand(name)
            if cmd:
                XPLMCommandOnce(cmd)
                return True
        except Exception:
            pass
        return False

    def _approach_setup(self, tp):
        """Optionally press LS on both EFIS panels after an in-air move.
        LS is a toggle, so the current state is checked first where ToLiss exposes it."""
        if tp.get("press_ls"):
            for state_ref, cmds in (("AirbusFBW/ILSonCapt", ("toliss_airbus/dispcommands/CaptLSButtonPush",)),
                                    ("AirbusFBW/ILSonFO", ("toliss_airbus/dispcommands/CoLSButtonPush",))):
                if self._dref(state_ref) is not None and self._get_f(state_ref) >= 1:
                    continue
                if not any(self._run_command(c) for c in cmds):
                    XPLMDebugString(f"ToLiss EFB: LS command not found ({cmds[0]})\n")

    def _track_gear_height(self):
        """Remember how high the aircraft sits above the ground when parked, so we can place it the same way."""
        try:
            if self._get_f("sim/flightmodel/failures/onground_any") >= 1 and self._get_f("sim/flightmodel/position/groundspeed") < 1.0:
                h = self._get_f("sim/flightmodel/position/y_agl", -1.0)
                if 0.3 < h < 15.0:
                    self.gear_height_m = h
        except Exception:
            pass

    # Approximate nose-wheel distance ahead of the CG (m), used only if the aircraft's gear data can't be read
    _NOSE_GEAR_FALLBACK_M = {"A319": 10.2, "A320": 11.6, "A20N": 11.6, "A321": 15.6, "A21N": 15.6,
                             "A339": 23.0, "A338": 23.0, "A332": 20.5, "A333": 23.0, "A346": 30.0, "A343": 25.0}

    def _nose_gear_ahead_m(self):
        """Distance (m) from the aircraft's CG forward to its nose wheel, from the loaded aircraft's gear
        definition (sim/aircraft/parts/acf_gear_znodef: metres, relative to CG, negative z = forward)."""
        try:
            ref = self._dref("sim/aircraft/parts/acf_gear_znodef")
            if ref is not None:
                out = []
                XPLMGetDatavf(ref, out, 0, 10)
                zs = [float(z) for z in out if z is not None]
                if zs:
                    ahead = -min(zs)
                    if 1.0 < ahead < 60.0:
                        return ahead
        except Exception as e:
            XPLMDebugString(f"ToLiss EFB: Could not read gear positions: {e}\n")
        try:
            icao = self._read_string_dataref(self._dref("sim/aircraft/view/acf_ICAO")).strip().upper()
            if icao in self._NOSE_GEAR_FALLBACK_M:
                return self._NOSE_GEAR_FALLBACK_M[icao]
        except Exception:
            pass
        return 0.0

    def start_teleport(self, req):
        lat = float(req.get('lat', 0)); lon = float(req.get('lon', 0))
        hdg = float(req.get('hdg', 0)) % 360.0
        elev_m = float(req.get('elev', 0) or 0) * 0.3048
        icao = str(req.get('icao', '') or '').upper()
        kind = str(req.get('type', '')).upper()
        if str(req.get('mode', '')).lower() == 'air':
            self._track_gear_height()
            cur_lat = self._get_f("sim/flightmodel/position/latitude")
            cur_lon = self._get_f("sim/flightmodel/position/longitude")
            far = _gm_dist_m(cur_lat, cur_lon, lat, lon) > 25000.0
            tp = {"lat": lat, "lon": lon, "hdg": hdg, "air": True, "hold_for_scenery": far,
                             "alt_m": float(req.get('alt_ft', 0) or 0) * 0.3048,
                             "speed_mps": max(0.0, float(req.get('speed_kt', 0) or 0)) * 0.514444,
                             "path_deg": float(req.get('path_deg', 0) or 0),
                             "pitch_deg": float(req.get('pitch_deg', 2.5) or 0),
                             "pause": bool(req.get('pause', False)),
                             "press_ls": bool(req.get('ls', False)),
                             "phase": "air", "frames": 0, "t0": time.time()}
            # In the air there is no ground to measure, so move directly. This keeps the aircraft's state
            # (MCDU, IRS, engines); X-Plane's own "go to airport" would reset it.
            self._begin_teleport(tp)
            return
        if kind != 'RWY':
            # Ramp starts mark where the nose wheel stops, so move the CG back along the stand's lead-in line
            back = self._nose_gear_ahead_m()
            if back > 0:
                lat -= (back * math.cos(math.radians(hdg))) / 111320.0
                lon -= (back * math.sin(math.radians(hdg))) / (111320.0 * math.cos(math.radians(lat)))
        if kind == 'RWY':
            # Threshold points sit on the very end of the runway; move 40 m along the runway
            d = 40.0
            lat += (d * math.cos(math.radians(hdg))) / 111320.0
            lon += (d * math.sin(math.radians(hdg))) / (111320.0 * math.cos(math.radians(lat)))
        self._track_gear_height()
        cur_lat = self._get_f("sim/flightmodel/position/latitude")
        cur_lon = self._get_f("sim/flightmodel/position/longitude")
        far = _gm_dist_m(cur_lat, cur_lon, lat, lon) > 15000.0
        tp = {"lat": lat, "lon": lon, "hdg": hdg, "elev_m": elev_m,
              "phase": "snap", "frames": 0, "t0": time.time()}
        if far and icao:
            # Let X-Plane load the destination scenery first, then snap to the exact spot.
            # The airport load is requested in process_teleport, after any pause has been lifted,
            # because X-Plane ignores it while paused.
            tp["phase"] = "wait"
            tp["place_icao"] = icao
        self._begin_teleport(tp)

    def process_teleport(self):
        tp = getattr(self, 'teleport', None)
        if not tp:
            self._track_gear_height()
            return
        now = time.time()
        if tp["phase"] == "resume":
            # Give the unpause a couple of frames to take effect before moving the aircraft
            tp["resume_frames"] += 1
            if tp["resume_frames"] >= 2 and self._get_f("sim/time/paused") < 1:
                tp["phase"] = tp.get("next_phase", "snap")
                tp["frames"] = 0
                tp["t0"] = now
            elif tp["resume_frames"] > 60:
                XPLMDebugString("ToLiss EFB: Could not unpause for repositioning; trying anyway.\n")
                tp["phase"] = tp.get("next_phase", "snap")
                tp["frames"] = 0
            return
        if tp["phase"] == "wait":
            if tp.get("place_icao") and not tp.get("placed"):
                tp["placed"] = True
                tp["t0"] = now
                try:
                    XPLMPlaceUserAtAirport(tp["place_icao"])
                except Exception as e:
                    XPLMDebugString(f"ToLiss EFB: PlaceUserAtAirport failed ({e}); snapping directly.\n")
                    tp["phase"] = "snap"
                    tp["frames"] = 0
                return
            loading = self._get_f("sim/graphics/scenery/async_scenery_load_in_progress", 0.0) >= 1
            cur_lat = self._get_f("sim/flightmodel/position/latitude")
            cur_lon = self._get_f("sim/flightmodel/position/longitude")
            near = _gm_dist_m(cur_lat, cur_lon, tp["lat"], tp["lon"]) < 20000.0
            if (now - tp["t0"] > 3.0 and near and not loading) or (now - tp["t0"] > 45.0):
                tp["phase"] = "air" if tp.get("air") else "snap"
                tp["frames"] = 0
            return
        if tp["phase"] == "air":
            try:
                self._place_air(tp)
            except Exception as e:
                XPLMDebugString(f"ToLiss EFB: In-air reposition error: {e}\n")
                self.teleport = None
                return
            tp["frames"] += 1
            if tp["frames"] == 1:
                tp["t_air"] = now
            holding = False
            if tp.get("hold_for_scenery"):
                # Keep the aircraft pinned at the new spot while distant scenery loads (at least 2 s, at most 30 s)
                held = now - tp.get("t_air", now)
                loading = self._get_f("sim/graphics/scenery/async_scenery_load_in_progress", 0.0) >= 1
                holding = held < 2.0 or (loading and held < 30.0)
            if tp["frames"] >= 5 and not holding:
                self.teleport = None
                self._approach_setup(tp)
                # For in-air moves the "Pause after repositioning in the air" switch alone decides
                if tp.get("pause"):
                    self._sim_pause(True)
            return
        if tp["phase"] == "settle":
            try:
                done = self._settle_aircraft(tp)
            except Exception:
                done = True
            if done or now - tp.get("t_settle", now) > 4.0:
                self.teleport = None
                self._track_gear_height()
                if tp.get("was_paused"):
                    self._sim_pause(True)
            return
        try:
            self._snap_aircraft(tp)
        except Exception as e:
            XPLMDebugString(f"ToLiss EFB: Teleport error: {e}\n")
            self.teleport = None
            return
        tp["frames"] += 1
        if tp["frames"] >= 3:
            tp["phase"] = "settle"
            tp["t_settle"] = now

    def _settle_aircraft(self, tp):
        """Lower the aircraft in small steps until the wheels touch, holding it still meanwhile.
        Being airborne at zero airspeed is what triggers the stall warning."""
        on_ground = self._get_f("sim/flightmodel/failures/onground_any") >= 1
        for d in ("local_vx", "local_vy", "local_vz", "P", "Q", "R", "Prad", "Qrad", "Rrad"):
            self._set_num("sim/flightmodel/position/" + d, 0.0)
        if on_ground:
            tp["ground_frames"] = tp.get("ground_frames", 0) + 1
            return tp["ground_frames"] >= 3
        tp["ground_frames"] = 0
        y = self._get_f("sim/flightmodel/position/local_y")
        self._set_num("sim/flightmodel/position/local_y", y - 0.05)
        return False

    def _place_air(self, tp):
        """Place the aircraft in flight: position, attitude and a velocity along the chosen path."""
        x, y, z = XPLMWorldToLocal(tp["lat"], tp["lon"], tp["alt_m"])[:3]
        self._set_num("sim/flightmodel/position/local_x", x)
        self._set_num("sim/flightmodel/position/local_y", y)
        self._set_num("sim/flightmodel/position/local_z", z)
        q_ref = self._dref("sim/flightmodel/position/q")
        if q_ref is not None:
            XPLMSetDatavf(q_ref, self._euler_to_quat(tp["hdg"], tp["pitch_deg"], 0.0), 0, 4)
        self._set_num("sim/flightmodel/position/psi", tp["hdg"])
        self._set_num("sim/flightmodel/position/theta", tp["pitch_deg"])
        self._set_num("sim/flightmodel/position/phi", 0.0)
        spd, path = tp["speed_mps"], math.radians(tp["path_deg"])
        horiz, vert = spd * math.cos(path), -spd * math.sin(path)
        h = math.radians(tp["hdg"])
        # Local OpenGL frame: +x east, +y up, -z north
        self._set_num("sim/flightmodel/position/local_vx", horiz * math.sin(h))
        self._set_num("sim/flightmodel/position/local_vy", vert)
        self._set_num("sim/flightmodel/position/local_vz", -horiz * math.cos(h))
        for d in ("local_ax", "local_ay", "local_az", "P", "Q", "R", "Prad", "Qrad", "Rrad", "P_dot", "Q_dot", "R_dot"):
            self._set_num("sim/flightmodel/position/" + d, 0.0)
        self._set_num("sim/cockpit2/controls/parking_brake_ratio", 0.0)

    def _snap_aircraft(self, tp):
        x, y, z = XPLMWorldToLocal(tp["lat"], tp["lon"], tp["elev_m"])[:3]
        ground_y = self._probe_terrain_y(x, y + 500.0, z)
        if ground_y is None:
            ground_y = y
        gear_h = getattr(self, 'gear_height_m', None) or 4.0
        self._set_num("sim/flightmodel/position/local_x", x)
        self._set_num("sim/flightmodel/position/local_y", ground_y + gear_h)
        self._set_num("sim/flightmodel/position/local_z", z)

        q_ref = self._dref("sim/flightmodel/position/q")
        if q_ref is not None:
            XPLMSetDatavf(q_ref, self._euler_to_quat(tp["hdg"], 0.0, 0.0), 0, 4)
        self._set_num("sim/flightmodel/position/psi", tp["hdg"])
        self._set_num("sim/flightmodel/position/theta", 0.0)
        self._set_num("sim/flightmodel/position/phi", 0.0)

        for d in ("local_vx", "local_vy", "local_vz", "local_ax", "local_ay", "local_az",
                  "P", "Q", "R", "Prad", "Qrad", "Rrad", "P_dot", "Q_dot", "R_dot"):
            self._set_num("sim/flightmodel/position/" + d, 0.0)

        # Hold the aircraft at the new spot
        self._set_num("sim/cockpit2/controls/parking_brake_ratio", 1.0)
        self._set_num("AirbusFBW/ParkBrake", 1)

    def flightLoopCallback(self, elapsedMe, elapsedSim, counter, refcon):
        try:
            for cmd_ref in self.active_commands:
                XPLMCommandEnd(cmd_ref)
            self.active_commands.clear()

            while not q_cmd.empty():
                cmd_str = q_cmd.get_nowait()
                cmd_ref = XPLMFindCommand(cmd_str)
                if cmd_ref:
                    XPLMCommandBegin(cmd_ref)
                    self.active_commands.append(cmd_ref)

            self._process_situation_requests()
            self._process_fault_requests()
            self._process_payload_requests()

            while not q_load_sit.empty():
                filename = q_load_sit.get_nowait()
                
                toliss_dir = self.sit_dir_toliss
                xp_out_dir = os.path.join(self.xp_path, "Output", "situations")
                
                src_dat = os.path.join(toliss_dir, f"{filename}.dat")
                src_qps = os.path.join(toliss_dir, f"{filename}.qps")
                
                target_sit = os.path.join(xp_out_dir, f"ToLissWebTemp.sit")
                target_dat = os.path.join(xp_out_dir, f"ToLissWebTemp.dat")
                target_qps = os.path.join(xp_out_dir, f"ToLissWebTemp.qps")
                
                if os.path.exists(src_dat):
                    shutil.copy2(src_dat, target_sit)
                    shutil.copy2(src_dat, target_dat)
                    if os.path.exists(src_qps):
                        shutil.copy2(src_qps, target_qps)
                    
                    src_pilot_dat = os.path.join(toliss_dir, f"{filename}_pilotitems.dat")
                    src_pilot_qps = os.path.join(toliss_dir, f"{filename}_pilotitems.qps")
                    if os.path.exists(src_pilot_dat):
                        shutil.copy2(src_pilot_dat, os.path.join(xp_out_dir, "ToLissWebTemp_pilotitems.dat"))
                    if os.path.exists(src_pilot_qps):
                        shutil.copy2(src_pilot_qps, os.path.join(xp_out_dir, "ToLissWebTemp_pilotitems.qps"))

                    XPLMLoadSituation(target_sit.encode('utf-8'))

            while not q_teleport.empty():
                req = q_teleport.get_nowait()
                try:
                    self.start_teleport(req)
                except Exception as e:
                    XPLMDebugString(f"ToLiss EFB: Teleport start error: {e}\n")
            self.process_teleport()

            while not q_dref_w.empty():
                raw_dref_str, val = q_dref_w.get_nowait()
                
                if raw_dref_str == "toliss_web/set_fuel":
                    requested = max(0.0, float(val))
                    fuel_max = self._get_fuel_capacity_kg()
                    try:
                        self._set_block_fuel_kg(min(requested, fuel_max) if fuel_max > 0 else requested)
                    except Exception as e:
                        XPLMDebugString(f"ToLiss EFB: Fuel apply error: {e}\n")
                    continue

                if raw_dref_str == "toliss_web/set_time":
                    sys_time_ref = XPLMFindDataRef("sim/time/use_system_time")
                    if sys_time_ref: XPLMSetDatai(sys_time_ref, 0) 
                    zulu_time_ref = XPLMFindDataRef("sim/time/zulu_time_sec")
                    if zulu_time_ref: XPLMSetDataf(zulu_time_ref, float(val)) 
                    continue

                if raw_dref_str == "toliss_web/weather_live/on":
                    mode_ref = XPLMFindDataRef("sim/weather/region/change_mode")
                    if mode_ref: XPLMSetDatai(mode_ref, 7) 
                    continue

                if raw_dref_str == "toliss_web/weather_live/off":
                    mode_ref = XPLMFindDataRef("sim/weather/region/change_mode")
                    if mode_ref: XPLMSetDatai(mode_ref, 3) 
                    continue

                if raw_dref_str == "toliss_web/weather_preset/":
                    mode_ref = XPLMFindDataRef("sim/weather/region/change_mode")
                    if mode_ref: XPLMSetDatai(mode_ref, 3) 
                    preset_ref = XPLMFindDataRef("sim/weather/region/weather_preset")
                    if preset_ref: XPLMSetDatai(preset_ref, int(val)) 
                    update_ref = XPLMFindDataRef("sim/weather/region/update_immediately")
                    if update_ref: XPLMSetDatai(update_ref, 1) 
                    continue

                match = re.match(r"(.+)\[(\d+)\]", raw_dref_str)
                if match:
                    dref_str = match.group(1)
                    idx = int(match.group(2))
                    dref = XPLMFindDataRef(dref_str)
                    if dref:
                        types = XPLMGetDataRefTypes(dref)
                        if types & xplmType_FloatArray:
                            arr_len = XPLMGetDatavf(dref, None, 0, 0)
                            if idx < arr_len:
                                XPLMSetDatavf(dref, [float(val)], idx, 1)
                        elif types & xplmType_IntArray:
                            arr_len = XPLMGetDatavi(dref, None, 0, 0)
                            if idx < arr_len:
                                XPLMSetDatavi(dref, [int(val)], idx, 1)
                else:
                    dref = XPLMFindDataRef(raw_dref_str)
                    if dref is not None:
                        types = XPLMGetDataRefTypes(dref)
                        if types & xplmType_Float:
                            XPLMSetDataf(dref, float(val))
                        elif types & xplmType_Int:
                            XPLMSetDatai(dref, int(val))
                        elif types & xplmType_Double:
                            XPLMSetDatad(dref, float(val))
            
            self.process_dref_read_queue(q_state_req, q_state_res)
            self.process_dref_read_queue(q_payload_req, q_payload_res)
            self.process_dref_read_queue(q_telem_req, q_telem_res)
            self._process_mcdu()
            try:
                self._update_stream()
            except Exception as e:
                XPLMDebugString(f"ToLiss EFB: display stream error: {e}\n")
            try:
                if getattr(self, 'fdr', None):
                    self.fdr.tick()
            except Exception as e:
                XPLMDebugString(f"ToLiss EFB: FDR error: {e}\n")
            
            while not q_path_req.empty():
                try:
                    q_path_req.get_nowait()
                    filename, acf_path = XPLMGetNthAircraftModel(0)
                    if acf_path:
                        if not os.path.isabs(acf_path):
                            acf_path = os.path.join(self.xp_path, acf_path)
                        q_path_res.put(acf_path)
                    else:
                        q_path_res.put("")
                except Exception:
                    pass
                
        except Exception as e:
            XPLMDebugString(f"ToLiss EFB: Flight Loop Error: {e}\n")

        return -1.0

    def start_server_thread(self):
        self.server_thread = threading.Thread(target=self.run_server)
        self.server_thread.daemon = True
        self.server_thread.start()

    def run_server(self):
        start_port = int(self.config.get("port", 8080))
        max_attempts = 50
        bound_port = None

        for offset in range(max_attempts):
            test_port = start_port + offset
            try:
                self.httpd = ThreadingHTTPServer(("", test_port), self.handler_class)
                bound_port = test_port
                break
            except Exception as e:
                XPLMDebugString(f"ToLiss EFB: Port {test_port} occupied ({e}), trying next...\n")

        if bound_port is not None:
            self.config["port"] = bound_port
            self.save_config(self.config)
            XPLMDebugString(f"ToLiss EFB successfully bound to port {bound_port}\n")
            try:
                self.httpd.serve_forever()
            except Exception as e:
                XPLMDebugString(f"ToLiss EFB: Server crashed: {e}\n")
        else:
            XPLMDebugString(f"ToLiss EFB ERROR: Could not bind to any port starting from {start_port}.\n")

    def restart_server_async(self):
        XPLMDebugString("ToLiss EFB: Restarting server...\n")
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        self.start_server_thread()