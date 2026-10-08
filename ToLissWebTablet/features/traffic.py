"""TCAS training traffic (IOS > TCAS Traffic): intruders injected into X-Plane's TCAS so the crew can practise
traffic and resolution advisories.

The EFB takes over X-Plane's traffic (XPLMAcquirePlanes + sim/operation/override/override_TCAS), and every frame
writes each intruder's position and velocity into sim/cockpit2/tcas/targets/ (slot 0 is the user's aircraft).
Intruders fly a straight path planned to meet the aircraft at a chosen closest point of approach (CPA): time to CPA,
horizontal miss distance and vertical separation. Their positions are kept in world coordinates (latitude,
longitude, altitude) so they stay right when X-Plane shifts its local coordinates. Only one plugin can supply
traffic: if xPilot, LiveTraffic or another one has it, nothing is taken; if another plugin asks for it, it is given
back at once. Everything here runs on X-Plane's main thread (bridge requests, flight loop, plugin messages).
"""
from __future__ import annotations

import math
import time

from XPLMDataAccess import *
from XPLMGraphics import *
from XPLMPlanes import *

try:
    from XPLMPlugin import XPLMGetPluginInfo
except Exception:  # pragma: no cover - older XPPython3
    XPLMGetPluginInfo = None

XPLM_MSG_RELEASE_PLANES = 111
TCAS_T = "sim/cockpit2/tcas/targets/"
TCAS_P = TCAS_T + "position/"
KT = 0.514444
FT = 0.3048
NM = 1852.0
MAX_INTRUDERS = 8

# relative track (deg from the aircraft's track), speed (None = like the aircraft's, at least 250 kt;
# a number starting with + is added to the aircraft's speed), vertical speed (fpm)
TRAFFIC_SCENARIOS = {
    "head_on": {"label": "Head-on", "rel": 180.0, "speed": None, "vs": 0.0},
    "cross_left": {"label": "Crossing from the left", "rel": 90.0, "speed": None, "vs": 0.0},
    "cross_right": {"label": "Crossing from the right", "rel": -90.0, "speed": None, "vs": 0.0},
    "overtake": {"label": "Overtaking from behind", "rel": 0.0, "speed": "+80", "vs": 0.0},
    "climb_below": {"label": "Climbing from below", "rel": 150.0, "speed": None, "vs": 2000.0},
    "descend_above": {"label": "Descending from above", "rel": -150.0, "speed": None, "vs": -2000.0},
}


class TrafficMixin:
    def _tfc(self):
        return self.__dict__.setdefault("traffic", {"owned": False, "intruders": [], "next_id": 1, "message": "",
                                                    "count_set": None, "t": None})

    # ---- owning X-Plane's traffic ----
    def _tfc_owner_name(self):
        try:
            total, active, owner = XPLMCountAircraft()
            if owner is None or owner < 0 or XPLMGetPluginInfo is None:
                return ""
            info = XPLMGetPluginInfo(owner)
            name = getattr(info, "name", None)
            if name is None and isinstance(info, (tuple, list)) and info:
                name = info[0]
            return str(name or "")
        except Exception:
            return ""

    def _tfc_acquire(self):
        tf = self._tfc()
        if tf["owned"]:
            return True, ""
        try:
            got = XPLMAcquirePlanes(None, None, None)
        except Exception as exc:
            return False, f"X-Plane did not give the EFB the traffic ({exc})."
        if not got:
            who = self._tfc_owner_name()
            return False, ((f"{who} is supplying traffic. " if who else "Another plugin (e.g. xPilot or LiveTraffic) is supplying traffic. ")
                           + "Disconnect it or turn its traffic off, then try again.")
        tf["owned"] = True
        self._set_num("sim/operation/override/override_TCAS", 1)
        tf["count_set"] = None
        return True, ""

    def _tfc_release(self, message=None):
        tf = self._tfc()
        tf["intruders"] = []
        if tf["owned"]:
            try:
                self._set_num("sim/operation/override/override_TCAS", 0)
            except Exception:
                pass
            try:
                XPLMSetActiveAircraftCount(1)
            except Exception:
                pass
            try:
                XPLMReleasePlanes()
            except Exception:
                pass
        tf["owned"], tf["count_set"] = False, None
        if message is not None:
            tf["message"] = message

    def _traffic_message(self, in_message):
        """From XPluginReceiveMessage: another plugin (e.g. xPilot connecting) wants the traffic: give it back."""
        if in_message == XPLM_MSG_RELEASE_PLANES and self._tfc()["owned"]:
            self._tfc_release("Another plugin asked for the traffic (e.g. xPilot connecting): the training traffic was removed.")

    # ---- the aircraft and the intruders ----
    def _tfc_own(self):
        g = self._get_f
        P = "sim/flightmodel/position/"
        x, y, z = g(P + "local_x"), g(P + "local_y"), g(P + "local_z")
        ve, vn, vu = g(P + "local_vx"), -g(P + "local_vz"), g(P + "local_vy")
        gs = math.hypot(ve, vn)
        trk = math.degrees(math.atan2(ve, vn)) % 360 if gs > 5 else g(P + "psi")
        return {"x": x, "y": y, "z": z, "ve": ve, "vn": vn, "vu": vu, "gs": gs, "trk": trk,
                "agl_ft": g(P + "y_agl") / FT, "on_ground": g("sim/flightmodel/failures/onground_any") >= 1}

    def _tfc_plan(self, item, own):
        sc = TRAFFIC_SCENARIOS.get(str(item.get("scenario")))
        if not sc:
            raise ValueError("Unknown scenario.")
        T = max(15.0, min(180.0, float(item.get("cpa_s") or 50)))
        miss = max(0.0, min(5.0, float(item.get("miss_nm") or 0))) * NM
        vert = max(-3000.0, min(3000.0, float(item.get("vert_ft") or 0))) * FT
        own_kt = own["gs"] / KT
        spd_in = float(item.get("speed_kt") or 0)
        if spd_in > 0:
            spd = spd_in
        elif sc["speed"] is None:
            spd = max(250.0, own_kt)
        else:
            spd = max(60.0, own_kt) + float(sc["speed"])
        spd = max(60.0, min(600.0, spd)) * KT
        vs = float(item["vs_fpm"]) if item.get("vs_fpm") not in (None, "") else sc["vs"]
        vs = max(-6000.0, min(6000.0, vs)) * FT / 60.0
        trk = (own["trk"] + sc["rel"]) % 360
        vi_e, vi_n = spd * math.sin(math.radians(trk)), spd * math.cos(math.radians(trk))
        # the aircraft at CPA (straight ahead, at its present climb or descent)
        ce, cn, cu = own["x"] + own["ve"] * T, -own["z"] + own["vn"] * T, own["y"] + own["vu"] * T
        # the miss distance is across the line of relative motion
        re_, rn = vi_e - own["ve"], vi_n - own["vn"]
        rl = math.hypot(re_, rn)
        if rl < 1.0:
            re_, rn, rl = math.sin(math.radians(trk)), math.cos(math.radians(trk)), 1.0
        side = -1.0 if str(item.get("miss_side", "R")).upper() == "L" else 1.0
        oe, on_ = side * rn / rl * miss, -side * re_ / rl * miss
        ie, in_, iu = ce + oe, cn + on_, cu + vert
        se, sn, su = ie - vi_e * T, in_ - vi_n * T, iu - vs * T
        lat, lon, alt = XPLMLocalToWorld(se, su, -sn)[:3]
        return {"label": sc["label"], "scenario": item.get("scenario"), "lat0": lat, "lon0": lon, "alt0": alt,
                "ve": vi_e, "vn": vi_n, "vu": vs, "trk": trk, "t": 0.0, "cpa_s": T, "life": T + 60.0,
                "spd_kt": round(spd / KT), "vs_fpm": round(vs / FT * 60.0)}

    def _tfc_pos(self, it):
        t = it["t"]
        lat = it["lat0"] + it["vn"] * t / 111320.0
        lon = it["lon0"] + it["ve"] * t / (111320.0 * max(0.05, math.cos(math.radians(it["lat0"]))))
        alt = it["alt0"] + it["vu"] * t
        x, y, z = XPLMWorldToLocal(lat, lon, alt)[:3]
        return x, y, z

    def _tfc_add(self, item):
        tf = self._tfc()
        if getattr(self, "push", None) or getattr(self, "slew", None):
            return {"ok": False, "message": "End the pushback or slew first."}
        if len(tf["intruders"]) >= MAX_INTRUDERS:
            return {"ok": False, "message": f"At most {MAX_INTRUDERS} intruders at once."}
        own = self._tfc_own()
        it = self._tfc_plan(item, own)
        ok, why = self._tfc_acquire()
        if not ok:
            return {"ok": False, "message": why}
        n = tf["next_id"]
        tf["next_id"] = n % 99 + 1
        it["id"] = n
        it["flight_id"] = f"TFC{n:02d}"
        it["mode_s"] = 0xE6D000 + n          # unique 24-bit Mode S addresses for the training traffic
        tf["intruders"].append(it)
        notes = []
        if own["on_ground"]:
            notes.append("the aircraft is on the ground, where TCAS gives no RAs")
        elif own["agl_ft"] < 1000:
            notes.append("below 1,000 ft above the ground TCAS gives no RAs (TA only)")
        tf["message"] = f"{it['flight_id']} injected: {it['label'].lower()}, closest approach in {round(it['cpa_s'])} s." + \
            (" Note: " + "; ".join(notes) + "." if notes else "")
        return {"ok": True, "message": tf["message"]}

    def _traffic_request(self, item):
        tf = self._tfc()
        kind = item.get("kind")
        if kind == "add":
            try:
                res = self._tfc_add(item)
            except (ValueError, TypeError) as exc:
                res = {"ok": False, "message": str(exc) or "Could not plan that intruder."}
        elif kind == "remove":
            tf["intruders"] = [i for i in tf["intruders"] if i["id"] != int(item.get("id", -1))]
            res = {"ok": True, "message": "Removed."}
            if not tf["intruders"]:
                self._tfc_release("All training traffic removed; traffic handed back to X-Plane.")
        elif kind == "clear":
            self._tfc_release("All training traffic removed; traffic handed back to X-Plane.")
            res = {"ok": True, "message": tf["message"]}
        elif kind == "state":
            res = {"ok": True}
        else:
            res = {"ok": False, "message": "Unknown traffic request."}
        res["state"] = self._traffic_state()
        return res

    def _traffic_state(self):
        tf = self._tfc()
        own = self._tfc_own()
        out = []
        threat = []
        try:
            ref = self._dref(TCAS_T + "threat")
            if ref is not None and tf["owned"]:
                threat = []
                XPLMGetDatavi(ref, threat, 0, MAX_INTRUDERS + 1)
        except Exception:
            threat = []
        for slot, it in enumerate(tf["intruders"], start=1):
            x, y, z = it.get("xyz") or self._tfc_pos(it)
            de, dn = x - own["x"], -(z - own["z"])
            rng = math.hypot(de, dn)
            brg = (math.degrees(math.atan2(de, dn)) - self._get_f("sim/flightmodel/position/psi")) % 360
            out.append({"id": it["id"], "flight_id": it["flight_id"], "label": it["label"], "range_nm": round(rng / NM, 2),
                        "rel_alt_ft": round((y - own["y"]) / FT / 100) * 100, "clock": int(round(brg / 30.0)) % 12 or 12,
                        "cpa_in_s": round(it["cpa_s"] - it["t"]), "spd_kt": it["spd_kt"], "vs_fpm": it["vs_fpm"],
                        "threat": int(threat[slot]) if len(threat) > slot and threat[slot] is not None else None})
        adv = None
        try:
            if self._dref("sim/cockpit2/tcas/indicators/tcas_active_advisory") is not None:
                adv = int(self._get_f("sim/cockpit2/tcas/indicators/tcas_active_advisory"))
        except Exception:
            adv = None
        return {"owned": tf["owned"], "intruders": out, "message": tf["message"], "advisory": adv,
                "agl_ft": round(own["agl_ft"]), "on_ground": own["on_ground"], "gs_kt": round(own["gs"] / KT),
                "scenarios": {k: v["label"] for k, v in TRAFFIC_SCENARIOS.items()}, "max": MAX_INTRUDERS}

    def _traffic_tick(self):
        tf = self.__dict__.get("traffic")
        if not tf or not tf["owned"]:
            return
        now = time.time()
        dt = 0.0 if tf["t"] is None else min(0.25, max(0.0, now - tf["t"]))
        tf["t"] = now
        frozen = self._get_f("sim/time/paused") >= 1 or (hasattr(self, "_in_replay") and self._in_replay())
        if not frozen:
            dt *= max(1.0, self._get_f("sim/time/sim_speed", 1.0) or 1.0)
            for it in tf["intruders"]:
                it["t"] += dt
        done = [it for it in tf["intruders"] if it["t"] > it["life"]]
        if done:
            tf["intruders"] = [it for it in tf["intruders"] if it["t"] <= it["life"]]
            tf["message"] = ", ".join(it["flight_id"] for it in done) + " has passed and was removed."
        if not tf["intruders"]:
            self._tfc_release(tf["message"] + " Traffic handed back to X-Plane." if done else None)
            return
        n = len(tf["intruders"])
        if tf["count_set"] != n:
            try:
                XPLMSetActiveAircraftCount(n + 1)
            except Exception:
                pass
            tf["count_set"] = n
        arrays = {k: [] for k in ("x", "y", "z", "vx", "vy", "vz", "psi", "the", "phi")}
        ids, modes = [], []
        for it in tf["intruders"]:
            x, y, z = self._tfc_pos(it)
            it["xyz"] = (x, y, z)
            for k, v in (("x", x), ("y", y), ("z", z), ("vx", it["ve"]), ("vy", it["vu"]), ("vz", -it["vn"]),
                         ("psi", it["trk"]), ("the", math.degrees(math.atan2(it["vu"], max(1.0, math.hypot(it["ve"], it["vn"]))))),
                         ("phi", 0.0)):
                arrays[k].append(float(v))
            ids.append(int(it["mode_s"]))
            modes.append(3)                       # Mode C: reports altitude, so it takes part in TCAS
        for k, vals in arrays.items():
            ref = self._dref(TCAS_P + k)
            if ref is not None:
                XPLMSetDatavf(ref, vals, 1, n)
        for name, vals in (("modeS_id", ids), ("ssr_mode", modes)):
            ref = self._dref(TCAS_T + name)
            if ref is not None:
                XPLMSetDatavi(ref, vals, 1, n)
        ref = self._dref(TCAS_T + "flight_id")
        if ref is not None:
            for slot, it in enumerate(tf["intruders"], start=1):
                try:
                    XPLMSetDatab(ref, it["flight_id"].encode("ascii")[:7].ljust(8, b"\0"), slot * 8, 8)
                except Exception:
                    break
