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

from .fdr import KT_PER_MS

class InstructorMixin:
    WX = "sim/weather/region/"

    def _instr_state(self):
        g = self._get_f
        psi, mag = g("sim/flightmodel/position/psi"), g("sim/flightmodel/position/mag_psi")
        st = getattr(self, 'instr', {})
        return {"alt_ft": round(g("sim/flightmodel/position/elevation") / 0.3048), "ias": round(g("sim/flightmodel/position/indicated_airspeed"), 1),
                "tas": round(g("sim/flightmodel/position/true_airspeed") * KT_PER_MS, 1), "gs": round(g("sim/flightmodel/position/groundspeed") * KT_PER_MS, 1),
                "hdg_mag": round(mag % 360, 1), "hdg_true": round(psi % 360, 1), "pitch": round(g("sim/flightmodel/position/theta"), 1),
                "roll": round(g("sim/flightmodel/position/phi"), 1), "vs": round(g("sim/flightmodel/position/vh_ind_fpm")),
                "agl_ft": round(g("sim/flightmodel/position/y_agl") / 0.3048), "on_ground": g("sim/flightmodel/failures/onground_any") >= 1,
                "paused": g("sim/time/paused") >= 1, "sim_rate": g("sim/time/sim_speed", 1) or 1,
                "alt_freeze": st.get("alt_y") is not None, "fuel_freeze": st.get("fuel") is not None,
                "lat": round(g("sim/flightmodel/position/latitude"), 6), "lon": round(g("sim/flightmodel/position/longitude"), 6),
                "slew": bool(getattr(self, 'slew', None)), "slew_ground": bool((getattr(self, 'slew', None) or {}).get("ground")),
                "slew_rate": (getattr(self, 'slew', None) or {}).get("rate"), "busy": bool(getattr(self, 'instr_op', None)),
                **self._push_state(), "park_brake": self._brake_set(),
                "shear": (getattr(self, 'shear_arm', None) or {}).get("state"),
                "fob": round(g("sim/flightmodel/weight/m_fuel_total"))}
    def _instr_set(self, req):
        """Set altitude, airspeed, heading, pitch, bank and/or vertical speed immediately (in flight only)."""
        g = self._get_f
        if g("sim/flightmodel/failures/onground_any") >= 1:
            return {"ok": False, "message": "The aircraft is on the ground: speed, attitude and altitude can only be set in flight."}
        psi, mag = g("sim/flightmodel/position/psi"), g("sim/flightmodel/position/mag_psi")
        var = psi - mag
        hdg_true = (float(req["hdg"]) + var) % 360 if req.get("hdg") is not None else psi
        pitch = float(req["pitch"]) if req.get("pitch") is not None else g("sim/flightmodel/position/theta")
        roll = float(req["roll"]) if req.get("roll") is not None else g("sim/flightmodel/position/phi")
        elev = g("sim/flightmodel/position/elevation")
        new_elev = float(req["alt"]) * 0.3048 if req.get("alt") is not None else elev
        if req.get("alt") is not None:
            agl = g("sim/flightmodel/position/y_agl")
            if agl - (elev - new_elev) < 30:
                return {"ok": False, "message": "That altitude is below the terrain here (less than 100 ft above ground)."}
            self._set_num("sim/flightmodel/position/local_y", g("sim/flightmodel/position/local_y") + (new_elev - elev))
        # airspeed: indicated -> true at the (new) altitude, standard atmosphere
        if req.get("ias") is not None or req.get("alt") is not None:
            ias = float(req["ias"]) if req.get("ias") is not None else g("sim/flightmodel/position/indicated_airspeed")
            h_ft = new_elev / 0.3048
            sigma = (1 - 6.8756e-6 * min(h_ft, 36089)) ** 4.2559 * (math.exp(-(h_ft - 36089) / 20806) if h_ft > 36089 else 1)
            tas = ias * 0.514444 / math.sqrt(max(sigma, 0.05))
        else:
            tas = g("sim/flightmodel/position/true_airspeed")
        vy = float(req["vs"]) * 0.00508 if req.get("vs") is not None else g("sim/flightmodel/position/local_vy")
        vy = max(-abs(tas) * 0.9, min(abs(tas) * 0.9, vy))
        horiz = math.sqrt(max(tas * tas - vy * vy, 0.0))
        h = math.radians(hdg_true)
        # ground velocity = air velocity + wind (local frame: +x east, +y up, -z north)
        wx, wz = g("sim/weather/aircraft/wind_now_x_msc", 0.0), g("sim/weather/aircraft/wind_now_z_msc", 0.0)
        self._set_num("sim/flightmodel/position/local_vx", horiz * math.sin(h) + wx)
        self._set_num("sim/flightmodel/position/local_vy", vy)
        self._set_num("sim/flightmodel/position/local_vz", -horiz * math.cos(h) + wz)
        q_ref = self._dref("sim/flightmodel/position/q")
        if q_ref is not None:
            XPLMSetDatavf(q_ref, self._euler_to_quat(hdg_true, pitch, roll), 0, 4)
        self._set_num("sim/flightmodel/position/psi", hdg_true)
        self._set_num("sim/flightmodel/position/theta", pitch)
        self._set_num("sim/flightmodel/position/phi", roll)
        for d in ("local_ax", "local_ay", "local_az", "P", "Q", "R", "Prad", "Qrad", "Rrad", "P_dot", "Q_dot", "R_dot"):
            self._set_num("sim/flightmodel/position/" + d, 0.0)
        st = getattr(self, 'instr', {})
        if st.get("alt_y") is not None:
            st["alt_y"] = g("sim/flightmodel/position/local_y")      # keep the altitude freeze at the new altitude
        return {"ok": True, "message": "Applied."}
    def _instr_freeze(self, what, on):
        st = self.__dict__.setdefault('instr', {})
        if what == "pause":
            self._sim_pause(bool(on))
            return {"ok": True}
        if what == "alt":
            st["alt_y"] = self._get_f("sim/flightmodel/position/local_y") if on else None
            return {"ok": True}
        if what == "fuel":
            if on:
                ref = self._dref("sim/flightmodel/weight/m_fuel")
                out = []
                if ref is not None:
                    XPLMGetDatavf(ref, out, 0, 9)
                st["fuel"] = out or None
            else:
                st["fuel"] = None
            return {"ok": True}
        return {"ok": False, "message": "Unknown freeze."}
    def _instr_op_step(self):
        op = self.instr_op
        op["frames"] += 1
        def finish(res):
            self.instr_op = None
            res["state"] = self._instr_state()
            try:
                op["reply"].put_nowait(res)
            except Exception:
                pass
        if op["phase"] == "resume":
            if op["frames"] == 1:
                self._sim_pause(False)
            if op["frames"] >= 2 and self._get_f("sim/time/paused") < 1:
                op["phase"], op["frames"] = "apply", 0
            elif op["frames"] > 90:
                finish({"ok": False, "message": "X-Plane could not be unpaused to apply the change."})
        elif op["phase"] == "apply" and op.get("wx") is not None:
            if op["frames"] == 1:
                op["t0"] = time.time()
                res = self._wx_apply(op["wx"])
                if not res.get("ok"):
                    if op["repause"]:
                        self._sim_pause(True)
                    return finish(res)
                op["wx_res"] = res
            elif op["wx"].get("_restore_winds") and time.time() - op.get("t0", 0) >= 0.5:
                for name, (ref, vals) in op["wx"].pop("_restore_winds").items():
                    XPLMSetDatavf(ref, vals, 0, len(vals))
                self._set_num(self.WX + "update_immediately", 1)
            elif time.time() - op.get("t0", 0) >= 1.0:          # let X-Plane's weather engine take the values up
                if op["repause"]:
                    self._sim_pause(True)
                op["phase"], op["frames"] = "verify", 0
        elif op["phase"] == "verify" and op.get("wx") is not None:
            if op["frames"] < 4:
                return
            res = op.get("wx_res") or {"ok": True, "message": "Applied."}
            changed = self._wx_check(res.get("expect"))
            note = " (briefly unpaused to apply, then paused again)" if op["repause"] else ""
            msg = res["message"].rstrip(".") + note + "."
            if op["wx"].get("_field_m") is not None:
                _, bases = self._wx_arr("cloud_base_msl_m", 1)
                if bases:
                    agl = round((bases[0] - op["wx"]["_field_m"]) / 0.3048 / 10) * 10
                    asked = op["wx"].get("ceiling_ft")
                    if asked is not None and abs(agl - float(asked)) > 30:
                        msg += f" X-Plane set the cloud base to {max(0, agl):,} ft above the field."
            if op["wx"].get("type") == "clear":
                _, cov = self._wx_arr("cloud_coverage_percent", 3)
                if cov:
                    msg += " Cloud cover now " + " / ".join(f"{round(c * 100)}%" for c in cov) + " (layers 1-3)."
            if changed:
                return finish({"ok": False, "message": msg + " But X-Plane did not keep: " + ", ".join(changed) + "."})
            finish({"ok": True, "message": msg + " X-Plane has taken the new values."})
        elif op["phase"] == "apply":
            res = self._instr_set(op["req"]) if op["req"] else {"ok": True}
            if not res.get("ok"):
                if op["repause"]:
                    self._sim_pause(True)
                return finish(res)
            if op["frames"] >= op["hold"]:
                if op["repause"]:
                    self._sim_pause(True)
                op["phase"], op["frames"] = "verify", 0
        elif op["phase"] == "verify":
            if op["frames"] < 4:
                return
            bad = self._instr_mismatch(op["req"])
            if bad and op["tries"] < 1:
                # ToLiss took longer to take up the new state: hold it for longer and try once more
                op.update(phase="resume", frames=0, hold=15, tries=op["tries"] + 1)
                return
            if bad:
                return finish({"ok": False, "message": "The change did not stick while paused (" + bad + "). Try again unpaused."})
            note = " (briefly unpaused to apply, then paused again)" if op["repause"] else ""
            finish({"ok": True, "message": op.get("done_msg", "Applied") + note + "."})
    def _instr_mismatch(self, req):
        """What, if anything, is not as requested (after a paused apply)."""
        if not req:
            return ""
        g = self._get_f
        if req.get("alt") is not None and abs(g("sim/flightmodel/position/elevation") / 0.3048 - float(req["alt"])) > 60:
            return "altitude"
        if req.get("hdg") is not None and abs(((g("sim/flightmodel/position/mag_psi") - float(req["hdg"])) + 540) % 360 - 180) > 3:
            return "heading"
        if req.get("roll") is not None and abs(g("sim/flightmodel/position/phi") - float(req["roll"])) > 2:
            return "bank"
        if req.get("pitch") is not None and abs(g("sim/flightmodel/position/theta") - float(req["pitch"])) > 2:
            return "pitch"
        return ""
    def _override_path(self, on):
        ref = self._dref("sim/operation/override/override_planepath")
        if ref is not None:
            XPLMSetDatavi(ref, [1 if on else 0], 0, 1)
    def _slew_start(self, item):
        g = self._get_f
        if getattr(self, 'slew', None):
            return {"ok": True, "message": "Slew is already on."}
        if getattr(self, 'push', None):
            return {"ok": False, "message": "Stop the pushback first."}
        was_paused = g("sim/time/paused") >= 1
        if was_paused:
            self._sim_pause(False)          # the position is held by the override, so the sim can run
        on_ground = g("sim/flightmodel/failures/onground_any") >= 1
        self._track_gear_height()
        self.slew = {"ground": on_ground, "was_paused": was_paused, "hdg": g("sim/flightmodel/position/psi"),
                     "pitch": 0.0 if not on_ground else g("sim/flightmodel/position/theta"),
                     "agl_m": self._height_above_terrain(g("sim/flightmodel/position/local_x"), g("sim/flightmodel/position/local_z")) if on_ground
                              else (getattr(self, 'gear_height_m', None) or max(2.0, g("sim/flightmodel/position/y_agl"))),
                     "y": g("sim/flightmodel/position/local_y"), "inp": {"fwd": 0, "side": 0, "up": 0, "turn": 0},
                     "rate": float(item.get("rate") or 20.0), "last_input": 0.0, "t": time.time(),
                     "resume_ias": float(item.get("resume_ias") or g("sim/flightmodel/position/indicated_airspeed") or 250)}
        self._override_path(True)
        return {"ok": True, "message": "Slew on" + (" (unpaused; the aircraft is held in place)" if was_paused else "") + "."}
    def _slew_place(self, x, z, sl):
        """Put the aircraft at local x/z: on the ground at gear height, in the air at the slew altitude (>= 50 ft AGL)."""
        ground_y = self._probe_terrain_y(x, sl["y"] + 2000.0, z)
        if sl["ground"]:
            y = (ground_y if ground_y is not None else sl["y"] - sl["agl_m"]) + sl["agl_m"]
        else:
            y = sl["y"]
            if ground_y is not None and y < ground_y + 15.24:
                y = ground_y + 15.24
        sl["y"] = y
        self._set_num("sim/flightmodel/position/local_x", x)
        self._set_num("sim/flightmodel/position/local_y", y)
        self._set_num("sim/flightmodel/position/local_z", z)
        q_ref = self._dref("sim/flightmodel/position/q")
        if q_ref is not None:
            XPLMSetDatavf(q_ref, self._euler_to_quat(sl["hdg"], sl["pitch"], 0.0), 0, 4)
        self._set_num("sim/flightmodel/position/psi", sl["hdg"])
        self._set_num("sim/flightmodel/position/theta", sl["pitch"])
        self._set_num("sim/flightmodel/position/phi", 0.0)
        for d in ("local_vx", "local_vy", "local_vz", "local_ax", "local_ay", "local_az", "P", "Q", "R", "Prad", "Qrad", "Rrad"):
            self._set_num("sim/flightmodel/position/" + d, 0.0)
    def _slew_tick(self):
        sl = self.slew
        now = time.time()
        dt, sl["t"] = min(0.1, max(0.0, now - sl["t"])), now
        inp = sl["inp"] if now - sl["last_input"] < 1.0 else {"fwd": 0, "side": 0, "up": 0, "turn": 0}   # page gone quiet: stop
        rate = max(0.2, min(5000.0, sl["rate"]))
        sl["hdg"] = (sl["hdg"] + float(inp.get("turn", 0)) * 20.0 * dt) % 360
        h = math.radians(sl["hdg"])
        fwd, side = float(inp.get("fwd", 0)), float(inp.get("side", 0))
        east = (fwd * math.sin(h) + side * math.cos(h)) * rate * dt
        north = (fwd * math.cos(h) - side * math.sin(h)) * rate * dt
        if not sl["ground"]:
            sl["y"] += float(inp.get("up", 0)) * max(1.0, min(60.0, rate * 0.5)) * dt
        x = self._get_f("sim/flightmodel/position/local_x") + east
        z = self._get_f("sim/flightmodel/position/local_z") - north      # -z is north
        self._slew_place(x, z, sl)
    def _slew_stop(self):
        sl = self.slew
        self.slew = None
        self._override_path(False)
        if sl["ground"]:
            for d in ("local_vx", "local_vy", "local_vz"):
                self._set_num("sim/flightmodel/position/" + d, 0.0)
            if sl["was_paused"]:
                self._sim_pause(True)
            return {"ok": True, "message": "Slew off." + (" Paused again." if sl["was_paused"] else "")}
        # in the air: hand back at the resume airspeed, level, on the current heading (and pause again if it was paused)
        return {"op": {"req": {"ias": sl["resume_ias"], "pitch": 2.5, "roll": 0.0, "vs": 0.0}, "repause": sl["was_paused"],
                       "done_msg": f"Slew off: flying at {round(sl['resume_ias'])} kt"}}
    def _wx_arr(self, name, n):
        ref = self._dref(self.WX + name)
        if ref is None:
            return None, None
        out = []
        XPLMGetDatavf(ref, out, 0, n)
        return ref, out
    def _turb_scale(self):
        """Return the documented effective turbulence ratio range.

        Some XP12 DataRefs.txt builds advertise storage bounds of 0-10 for the
        regional array.  The weather SDK and aircraft point sample define the
        value as a ratio, and the flight-model effect saturates above 1.  The
        old code multiplied UI ratios by the storage bound, collapsing Light,
        Moderate and Severe into the saturated range.
        """
        if getattr(self, '_turb_max', None):
            return self._turb_max
        self._turb_max = 1.0
        try:
            path = os.path.join(self.xp_path, "Resources", "plugins", "DataRefs.txt")
            with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    if line.startswith("sim/weather/region/turbulence"):
                        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:-|\.\.|to)\s*(\d+(?:\.\d+)?)", line)
                        storage_max = float(m.group(2)) if m and float(m.group(2)) > 0 else 1.0
                        self.log.xplane(
                            f"ToLiss EFB: turbulence storage metadata is 0-{storage_max:g}; "
                            "using the SDK's effective 0-1 ratio"
                        )
                        break
        except Exception as e:
            self.log.xplane(f"ToLiss EFB: could not read DataRefs.txt ({e}); using turbulence ratio 0-1")
        return self._turb_max
    def _wx_apply(self, item):
        g = self._get_f
        missing = []
        if self._dref(self.WX + "change_mode") is None:
            return {"ok": False, "message": "This X-Plane version has no regional weather controls."}
        self._set_num(self.WX + "change_mode", 3)                    # static weather, as the presets use
        field_m = g("sim/flightmodel/position/elevation") - g("sim/flightmodel/position/y_agl")
        hdg = g("sim/flightmodel/position/psi")
        kind = item.get("type")
        expect = []                                  # (name, index or None, value, tolerance) checked after applying
        alt_ref, alts = self._wx_arr("wind_altitude_msl_m", 13)
        low = [i for i, a in enumerate(alts or []) if a < field_m + 3000] or [0, 1, 2]
        ac_m = g("sim/flightmodel/position/elevation")
        # every layer up to the aircraft, plus the first one above it (X-Plane blends between the layers either side)
        above = [i for i, a in enumerate(alts or []) if a >= ac_m]
        around = sorted(set(low) | {i for i, a in enumerate(alts or []) if a < ac_m} | ({min(above, key=lambda i: alts[i])} if above else set()))
        def set_layers(name, values_by_layer):
            ref, cur = self._wx_arr(name, 13)
            if ref is None:
                missing.append(name)
                return
            for i, v in values_by_layer.items():
                if i < len(cur):
                    cur[i] = float(v)
            XPLMSetDatavf(ref, cur, 0, len(cur))
            i0 = min(values_by_layer)
            expect.append((name, i0, float(values_by_layer[i0]), 1.5 if name.endswith("_msc") else (6.0 if name.endswith("degt") else 0.05)))
        msg = ""
        if kind in ("wind", "calm"):
            spd = 0.0 if kind == "calm" else float(item.get("speed", 0))
            gust = 0.0 if kind == "calm" else max(0.0, float(item.get("gust", 0) or 0) - spd)
            wdir = (hdg + float(item.get("rel", 0))) % 360                # wind FROM this direction (true)
            set_layers("wind_direction_degt", {i: wdir for i in low})
            set_layers("wind_speed_msc", {i: spd * 0.514444 for i in low})
            set_layers("shear_speed_msc", {i: gust * 0.514444 for i in low})
            msg = "Calm wind." if kind == "calm" else f"Wind {round(wdir):03d}°T at {round(spd)} kt" + (f" gusting {round(spd + gust)}" if gust else "") + "."
        elif kind == "clear":
            # X-Plane keeps its cloud cover when a plugin writes zero, but its own Clear/CAVOK preset clears the sky.
            # Apply that preset (like the preset button), keeping the current low-level winds, then visibility and rain.
            saved = {}
            for name in ("wind_direction_degt", "wind_speed_msc", "shear_speed_msc", "turbulence"):
                ref, cur = self._wx_arr(name, 13)
                if ref is not None:
                    saved[name] = (ref, list(cur))
            if self._dref(self.WX + "weather_preset") is None:
                missing.append("weather_preset")
            else:
                self._set_num(self.WX + "weather_preset", 0)            # 0 = Clear / CAVOK, as the preset button
            item["_restore_winds"] = saved                              # put back once X-Plane has applied the preset
            if self._dref(self.WX + "visibility_reported_sm") is not None:
                self._set_num(self.WX + "visibility_reported_sm", 10000 / 1609.34)
                expect.append(("visibility_reported_sm", None, 10000 / 1609.34, 0.6))
            if self._dref(self.WX + "rain_percent") is not None:
                self._set_num(self.WX + "rain_percent", 0.0)
                expect.append(("rain_percent", None, 0.0, 0.05))
            msg = "Clear sky (X-Plane's Clear/CAVOK weather, your winds kept), visibility 10 km. Clouds take about a minute to clear."
        elif kind == "turb":
            frac = max(0.0, min(1.0, float(item.get("level", 0))))
            top = self._turb_scale()
            set_layers("turbulence", {i: frac for i in around})
            msg = f"Turbulence {item.get('label', round(frac, 2))} ({frac:g} ratio, up to {round(max(alts[i] for i in around) / 0.3048) if alts else '?'} ft)."
        elif kind == "vis":
            vis_m = float(item.get("vis_m", 10000))
            if self._dref(self.WX + "visibility_reported_sm") is None:
                missing.append("visibility_reported_sm")
            else:
                self._set_num(self.WX + "visibility_reported_sm", vis_m / 1609.34)
                expect.append(("visibility_reported_sm", None, vis_m / 1609.34, max(0.1, vis_m / 1609.34 * 0.1)))
            ceil = item.get("ceiling_ft")
            base_ref, bases = self._wx_arr("cloud_base_msl_m", 3)
            if base_ref is None:
                missing.append("cloud_base_msl_m")
            else:
                cov_ref, cov = self._wx_arr("cloud_coverage_percent", 3)
                top_ref, tops = self._wx_arr("cloud_tops_msl_m", 3)
                typ_ref, typ = self._wx_arr("cloud_type", 3)
                missing += [n for n, r in (("cloud_coverage_percent", cov_ref), ("cloud_tops_msl_m", top_ref), ("cloud_type", typ_ref)) if r is None]
                if ceil is None:
                    if cov_ref is not None:
                        cov[0] = 0.0
                        XPLMSetDatavf(cov_ref, cov, 0, len(cov))
                else:
                    bases[0] = field_m + float(ceil) * 0.3048
                    XPLMSetDatavf(base_ref, bases, 0, len(bases))
                    item["_field_m"] = field_m                  # to report the base X-Plane chooses, above the field
                    if top_ref is not None:
                        tops[0] = bases[0] + 1200.0
                        XPLMSetDatavf(top_ref, tops, 0, len(tops))
                    if cov_ref is not None:
                        cov[0] = 1.0                                     # overcast
                        XPLMSetDatavf(cov_ref, cov, 0, len(cov))
                    if typ_ref is not None:
                        typ[0] = 1.0                                     # stratus
                        XPLMSetDatavf(typ_ref, typ, 0, len(typ))
            msg = f"Visibility {round(vis_m):,} m" + (f", overcast at {int(ceil)} ft" if ceil is not None else ", no low cloud") + "."
        elif kind == "shear_arm":
            self.shear_arm = {"state": "armed", "field_m": field_m, "t": time.time()}
            return {"ok": True, "message": "Wind shear armed: it hits when you descend through about 1,000 ft above the field.", "expect": []}
        elif kind == "shear_cancel":
            sa = getattr(self, 'shear_arm', None)
            if sa and sa.get("saved"):
                self._shear_restore(sa)
            self.shear_arm = None
            return {"ok": True, "message": "Wind shear cancelled.", "expect": []}
        elif kind == "shear":
            # tailwind at the surface, strong headwind from about 1,000 ft: the wind changes sharply on short final
            order = sorted(low, key=lambda i: alts[i] if alts else i)
            surface, aloft = order[:1], order[1:]
            set_layers("wind_direction_degt", {**{i: (hdg + 180) % 360 for i in surface}, **{i: hdg % 360 for i in aloft}})
            set_layers("wind_speed_msc", {**{i: 15 * 0.514444 for i in surface}, **{i: 35 * 0.514444 for i in aloft}})
            set_layers("shear_speed_msc", {i: 10 * 0.514444 for i in order})
            set_layers("turbulence", {i: 0.35 for i in order})
            msg = "Wind shear: tailwind 15 kt at the surface, headwind 35 kt from about 1,000 ft, with gusts and turbulence."
        else:
            return {"ok": False, "message": "Unknown weather change."}
        self._set_num(self.WX + "update_immediately", 1)
        if missing:
            msg += " Not available in this X-Plane version: " + ", ".join(missing) + "."
        return {"ok": True, "message": msg, "expect": expect}
    def _shear_restore(self, sa):
        for name, (ref, vals) in sa["saved"].items():
            XPLMSetDatavf(ref, vals, 0, len(vals))
        self._set_num(self.WX + "update_immediately", 1)
    def _shear_tick(self):
        sa = self.shear_arm
        g = self._get_f
        now = time.time()
        agl_ft = g("sim/flightmodel/position/y_agl") / 0.3048
        on_ground = g("sim/flightmodel/failures/onground_any") >= 1
        if sa["state"] == "armed":
            if not on_ground and agl_ft < 1000 and g("sim/flightmodel/position/vh_ind_fpm") < -200:
                hdg = g("sim/flightmodel/position/psi")
                saved = {}
                for name in ("wind_direction_degt", "wind_speed_msc", "shear_speed_msc", "turbulence"):
                    ref, cur = self._wx_arr(name, 13)
                    if ref is not None:
                        saved[name] = (ref, list(cur))
                sa.update(state="increase", t=now, hdg=hdg, saved=saved)
                self._shear_set(hdg, 20, 0.25)               # first a sudden headwind gust ...
            return
        if sa["state"] == "increase" and now - sa["t"] >= 4:
            sa.update(state="decrease", t=now)
            self._shear_set((sa["hdg"] + 180) % 360, 25, 0.5)   # ... then the headwind collapses into a strong tailwind
        elif sa["state"] == "decrease" and (now - sa["t"] >= 20 or on_ground):
            self._shear_restore(sa)
            self.shear_arm = None
    def _shear_set(self, from_dir, speed_kt, turb_frac):
        _, alts = self._wx_arr("wind_altitude_msl_m", 13)
        ac_m = self._get_f("sim/flightmodel/position/elevation")
        layers = [i for i, a in enumerate(alts or []) if a < ac_m + 1000] or [0, 1, 2]
        for name, val in (("wind_direction_degt", from_dir), ("wind_speed_msc", speed_kt * 0.514444), ("shear_speed_msc", 8 * 0.514444),
                          ("turbulence", turb_frac * self._turb_scale())):
            ref, cur = self._wx_arr(name, 13)
            if ref is None:
                continue
            for i in layers:
                if i < len(cur):
                    cur[i] = float(val)
            XPLMSetDatavf(ref, cur, 0, len(cur))
        self._set_num(self.WX + "update_immediately", 1)
    def _wx_check(self, expect):
        """Read the weather back: which values X-Plane did not keep."""
        changed = []
        for name, idx, val, tol in expect or []:
            ref = self._dref(self.WX + name)
            if ref is None:
                continue
            if idx is None:
                cur = self._get_f(self.WX + name)
            else:
                _, arr = self._wx_arr(name, idx + 1)
                cur = arr[idx] if arr and len(arr) > idx else None
            if cur is not None and abs(cur - val) > tol:
                changed.append(name.replace("_", " "))
        return sorted(set(changed))
    def _instr_tick(self):
        """Every frame: carry out instructor commands and hold the altitude / fuel freezes."""
        if getattr(self, 'instr_op', None):
            self._instr_op_step()
        if getattr(self, 'slew', None):
            self._slew_tick()
        if getattr(self, 'shear_arm', None):
            try:
                self._shear_tick()
            except Exception as e:
                self.log.xplane(f"ToLiss EFB: wind shear: {e}\n")
                self.shear_arm = None
        if getattr(self, 'push', None):
            try:
                self._push_tick()
            except Exception as e:
                self._push_end(f"Pushback stopped: {e}")
        while True:
            try:
                item, reply = self._instructor_requests.get_nowait()
            except queue.Empty:
                break
            try:
                kind = item.get("kind")
                if kind == "state":
                    res = {"ok": True, "state": self._instr_state()}
                elif kind == "set":
                    if getattr(self, 'instr_op', None):
                        res = {"ok": False, "message": "Still applying the previous change; try again in a moment."}
                    elif getattr(self, 'slew', None):
                        res = {"ok": False, "message": "End slew first."}
                    elif self._get_f("sim/time/paused") >= 1 and self._get_f("sim/flightmodel/failures/onground_any") < 1:
                        self.instr_op = {"req": item, "reply": reply, "phase": "resume", "frames": 0, "hold": 4, "tries": 0, "repause": True}
                        continue                    # answered when the change has been applied and checked
                    else:
                        res = self._instr_set(item)
                elif kind == "wx":
                    if getattr(self, 'instr_op', None):
                        res = {"ok": False, "message": "Still applying the previous change; try again in a moment."}
                    else:
                        paused = self._get_f("sim/time/paused") >= 1
                        self.instr_op = {"wx": item, "req": None, "reply": reply, "phase": "resume" if paused else "apply",
                                         "frames": 0, "hold": 0, "tries": 1, "repause": paused}
                        continue                    # answered after the weather has been applied and read back
                elif kind.startswith("push_"):
                    res = self._push_request(kind, item)
                elif kind == "slew_on":
                    res = self._slew_start(item)
                elif kind == "slew_off":
                    if not getattr(self, 'slew', None):
                        res = {"ok": True, "message": "Slew is off."}
                    else:
                        res = self._slew_stop()
                        if "op" in res:
                            op = res["op"]
                            self.instr_op = {"req": op["req"], "reply": reply, "phase": "apply", "frames": 0, "hold": 4, "tries": 1,
                                             "repause": op["repause"], "done_msg": op["done_msg"]}
                            continue
                elif kind == "slew_place":
                    sl = getattr(self, 'slew', None)
                    if not sl:
                        res = {"ok": False, "message": "Start slew first."}
                    else:
                        x, _, z = XPLMWorldToLocal(float(item["lat"]), float(item["lon"]), self._get_f("sim/flightmodel/position/elevation"))[:3]
                        self._slew_place(x, z, sl)
                        res = {"ok": True, "message": "Placed."}
                elif kind == "slew_state":
                    res = {"ok": True}
                elif kind == "freeze":
                    res = self._instr_freeze(item.get("what"), item.get("on"))
                elif kind == "rate":
                    self._set_num("sim/time/sim_speed", int(max(1, min(16, int(item.get("rate", 1))))))
                    res = {"ok": True}
                else:
                    res = {"ok": False, "message": "Unknown request."}
                if kind != "state":
                    res["state"] = self._instr_state()
            except Exception as e:
                res = {"ok": False, "message": f"Could not apply: {e}"}
            try:
                reply.put_nowait(res)
            except Exception:
                pass
        st = getattr(self, 'instr', None)
        if st and getattr(self, 'slew', None):
            st["alt_y"] = None                         # slew moves the aircraft; the altitude freeze would fight it
        if not st:
            return
        if st.get("alt_y") is not None:
            if self._get_f("sim/flightmodel/failures/onground_any") >= 1:
                st["alt_y"] = None                     # never hold an altitude on the ground
            else:
                self._set_num("sim/flightmodel/position/local_y", st["alt_y"])
                self._set_num("sim/flightmodel/position/local_vy", 0.0)
        if st.get("fuel"):
            ref = self._dref("sim/flightmodel/weight/m_fuel")
            if ref is not None:
                XPLMSetDatavf(ref, st["fuel"], 0, len(st["fuel"]))
