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

from XPLMDefs import *
from XPLMUtilities import *
from XPLMDataAccess import *
from XPLMProcessing import *
from XPLMPlanes import *
from XPLMGraphics import *
from XPLMScenery import *

from ..services.airports import _gm_dist_m, parse_ground_map

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
                self.plugin.log.xplane(f"ToLiss EFB: FDR {self.last_error}\n")
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
            self.plugin.log.xplane(f"ToLiss EFB: FDR recording started: {self.name}\n")

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
        self.finalise_thread = threading.Thread(
            target=self.finalise, args=(name,), name="ToLissEFB-FDR-Finalise", daemon=True
        )
        self.finalise_thread.start()

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
            self.plugin.log.xplane(f"ToLiss EFB: FDR analysis failed for {name}: {e}\n")

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
