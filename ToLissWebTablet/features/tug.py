"""The pushback tug: X-Plane's own towbar tractor (Tug_GT110 with its driver), and the signals ToLiss takes as
"tug connected".

The truck: Resources/default scenery/airport scenery/Dynamic_Vehicles/Tug_GT110.obj, a conventional tractor whose
towbar is part of the model. In the model the tug's front is at -Z (its heading), the towbar is hinged 4.25 m ahead of
the model's origin and is 3.73 m long, so its tip is about 7.98 m ahead of the origin, 0.46 m up. The towbar (and the
beacon) show only while ground_traffic/running_misc is above 0.5, so the EFB passes the model's animation datarefs to
the instance and sets them every frame: engine running, towbar shown once connected, wheels turning and steering.

Sequence: created about 30 m off to the side, the tug drives in on a curve, turns to face the aircraft and creeps onto
the nose wheel; the towbar appears and the tug is connected. During the push it faces the aircraft with the towbar tip
on the nose wheel, in line with the nose wheel's angle, so it swings round with the nose wheel in the turn. After the
parking brake is set, the towbar goes, the tug backs off 10 m, turns away and is removed. Without the file, the push
runs without a truck.

NWS DISC: ToLiss shows it while Better Pushback's tug flags say a tug is connected (bp/started, bp/connected), so the
EFB publishes those flags itself (only when Better Pushback is not installed). It also takes over the nose wheel
steering during the push so the nose wheel turns with the tug, and always hands it back when the tug disconnects, on
Stop, on errors and when the plugin stops.
"""
from __future__ import annotations

import math
import os
import time

from XPLMDataAccess import *
from XPLMScenery import *

try:
    from XPLMInstance import XPLMCreateInstance, XPLMDestroyInstance, XPLMInstanceSetPosition
    HAVE_INSTANCE = True
except Exception:                       # very old XPPython3: no truck, everything else still works
    HAVE_INSTANCE = False
try:
    from XPLMPlugin import XPLMFindPluginBySignature
except Exception:
    XPLMFindPluginBySignature = None

TUG_DIR = "Resources/default scenery/airport scenery/Dynamic_Vehicles/"
TUG_OBJ, DRIVER_OBJ = TUG_DIR + "Tug_GT110.obj", TUG_DIR + "Tug_GT110__driver.obj"
GT = "sim/graphics/animation/ground_traffic/"
# the model's animation datarefs, in the order of the values passed to XPLMInstanceSetPosition
TUG_DREFS = [GT + "engine_running", GT + "running_misc", GT + "tire_steer_deg", GT + "towbar_heading_deg",
             GT + "towbar_pitch_deg"] + [GT + f"tire_rotation_angle_deg[{i}]" for i in range(5)] + \
            [GT + "wiper_angle_deg[0]", GT + "wiper_angle_deg[1]"]
DRIVER_DREFS = [GT + "engine_running"]
TIP = 7.98                   # m: the towbar tip ahead of the model's origin
WHEEL_R, BAR_WHEEL_R = 0.76, 0.22
HALF_WB = 1.6                # m: axle to the middle of the tug (all four wheels steer)
BP_SIGNATURES = ("skiselkov.BetterPushback", "BetterPushback", "bp.BetterPushback")
NO_TRUCK_WAIT = 2.0
DRIVE, CREEP, REVERSE = 6.0, 1.0, 2.0          # m/s


def _bezier(p0, p1, p2, n=80):
    return [((1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t * t * p2[0],
             (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t * t * p2[1]) for t in (i / n for i in range(n + 1))]


def _route(segments):
    """segments: [(points, speed, reverse)] of the tug's origin in metres (forward, right) from the nose wheel. Returns
    a timed route [(t, forward, right, heading_offset)]; heading_offset is the tug's heading (its front) relative to
    the aircraft's: the direction of travel, plus 180 when reversing."""
    out, t = [], 0.0
    for pts, v, rev in segments:
        for i in range(len(pts)):
            if out and i == 0:
                d = (pts[1][0] - pts[0][0], pts[1][1] - pts[0][1])     # the joint takes this segment's heading
                out[-1] = out[-1][:3] + ((math.degrees(math.atan2(d[1], d[0])) + (180 if rev else 0)) % 360,)
                continue
            if i:
                t += math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]) / v
            a, b = pts[max(0, i - 1)], pts[min(len(pts) - 1, max(1, i))]
            d = (b[0] - a[0], b[1] - a[1])
            h = math.degrees(math.atan2(d[1], d[0]))        # direction of travel, 0 = the aircraft's heading
            out.append((t, pts[i][0], pts[i][1], (h + (180 if rev else 0)) % 360))
    return out


# arrival (about 8 s): from 30 m off to the right, curving round to face the aircraft, then creeping the last 2 m until
# the towbar tip is on the nose wheel. Departure: straight back 10 m, then away to the left.
ARRIVAL = _route([(_bezier((26.0, 30.0), (17.0, 0.0), (TIP + 2.0, 0.0)), DRIVE, False),
                  ([(TIP + 2.0, 0.0), (TIP, 0.0)], CREEP, False)])
DEPARTURE = _route([([(TIP, 0.0), (TIP + 10.0, 0.0)], REVERSE, True),
                    (_bezier((TIP + 10.0, 0.0), (TIP + 4.0, 0.0), (TIP + 6.0, -32.0)), DRIVE, False)])


def _at(route, t):
    """Interpolated (forward, right, heading_offset) at time t along a route."""
    if t <= 0:
        return route[0][1:]
    for i in range(1, len(route)):
        if route[i][0] >= t:
            a, b = route[i - 1], route[i]
            f = 0.0 if b[0] == a[0] else (t - a[0]) / (b[0] - a[0])
            dh = ((b[3] - a[3] + 540) % 360) - 180
            return (a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f, (a[3] + dh * f) % 360)
    return route[-1][1:]


class TugMixin:
    # ---------------- setup ----------------
    def _tug_setup(self):
        """At plugin start: publish Better Pushback's status flags, unless Better Pushback is installed."""
        self.tug = None
        self._bp_state = {"started": 0, "connected": 0}
        self._bp_handles = []
        self._tug_objs = {}
        self._steer_on = False
        bp_installed = False
        if XPLMFindPluginBySignature is not None:
            for sig in BP_SIGNATURES:
                try:
                    if XPLMFindPluginBySignature(sig) not in (None, -1):
                        bp_installed = True
                except Exception:
                    pass
        if bp_installed or XPLMFindDataRef("bp/connected") is not None:
            self.log.xplane("ToLiss EFB: Better Pushback is installed: the EFB will not publish its tug flags.\n")
            return
        for name in ("started", "connected"):
            try:
                h = XPLMRegisterDataAccessor(f"bp/{name}", xplmType_Int, 0,
                                             lambda ref, n=name: int(self._bp_state.get(n, 0)), None,
                                             None, None, None, None, None, None, None, None, None, None, None, None)
                if h:
                    self._bp_handles.append(h)
            except Exception as e:
                self.log.xplane(f"ToLiss EFB: could not publish bp/{name}: {e}\n")

    def _tug_shutdown(self):
        try:
            self._tug_release()
        except Exception:
            pass
        self._tug_destroy(getattr(self, "tug", None))
        self.tug = None
        for obj in (getattr(self, "_tug_objs", None) or {}).values():
            try:
                XPLMUnloadObject(obj)
            except Exception:
                pass
        self._tug_objs = {}
        for h in getattr(self, "_bp_handles", []) or []:
            try:
                XPLMUnregisterDataAccessor(h)
            except Exception:
                pass
        self._bp_handles = []

    # ---------------- settings ----------------
    def _tug_settings(self):
        s = (self.config.get("push_settings") or {}) if isinstance(getattr(self, "config", None), dict) else {}
        return {"truck": s.get("truck", True) is not False}

    # ---------------- the truck ----------------
    def _tug_load(self, path):
        if path in self._tug_objs:
            return self._tug_objs[path]
        if path in getattr(self, "_tug_bad", set()):
            return None
        obj = None
        if os.path.isfile(os.path.join(getattr(self, "xp_path", "") or "", path)):
            try:
                obj = XPLMLoadObject(path)
            except Exception:
                obj = None
        if obj is None:
            if not hasattr(self, "_tug_bad"):
                self._tug_bad = set()
            self._tug_bad.add(path)
            self.log.xplane(f"ToLiss EFB: could not load {path}" + (": pushing without a truck.\n" if path == TUG_OBJ else ".\n"))
            return None
        self.log.xplane(f"ToLiss EFB: pushback truck: {path}\n")
        self._tug_objs[path] = obj
        return obj

    def _tug_begin(self, phi=0.0):
        """A push is starting: the tug drives in and connects. Returns how long that takes (s)."""
        st = self._tug_settings()
        t = {"phase": "arrive", "t0": time.time(), "phi": phi, "inst": None, "drv": None, "pose": None, "attached": False,
             "dist": 0.0, "steer": 0.0, "last": None}
        if st["truck"] and HAVE_INSTANCE:
            obj = self._tug_load(TUG_OBJ)
            if obj is not None:
                try:
                    t["inst"] = XPLMCreateInstance(obj, TUG_DREFS)
                except Exception as e:
                    self.log.xplane(f"ToLiss EFB: could not show the pushback truck: {e}\n")
                drv = self._tug_load(DRIVER_OBJ) if t["inst"] is not None else None
                if drv is not None:
                    try:
                        t["drv"] = XPLMCreateInstance(drv, DRIVER_DREFS)
                    except Exception:
                        t["drv"] = None
        self._tug_destroy(getattr(self, "tug", None))
        self.tug = t
        if self._bp_handles:
            self._bp_state["started"] = 1
        t["arrive_s"] = ARRIVAL[-1][0] if t["inst"] is not None else NO_TRUCK_WAIT
        return t["arrive_s"]

    def _tug_destroy(self, t):
        if not t:
            return
        for k in ("inst", "drv"):
            if t.get(k) is not None:
                try:
                    XPLMDestroyInstance(t[k])
                except Exception:
                    pass
                t[k] = None

    def _tug_attach(self):
        """The tug has the nose wheel: tug flags on (NWS DISC), nose wheel steering taken over."""
        t = self.tug
        if not t or t["attached"]:
            return
        t["attached"], t["phase"] = True, "attached"
        self._bp_state["started"], self._bp_state["connected"] = 1, 1
        self._tug_steer(0.0, take=True)
        self.log.xplane("ToLiss EFB: tug connected.\n")

    def _tug_release(self):
        """The tug lets go: steering handed back, flags off, the truck drives away."""
        self._tug_steer(None)
        self._bp_state["started"], self._bp_state["connected"] = 0, 0
        t = getattr(self, "tug", None)
        if t and t["phase"] != "leave":
            was = t["phase"]
            t["phase"], t["t0"], t["attached"] = "leave", time.time(), False
            t["leave_from"] = self._tug_nose_frame(t.get("phi", 0.0)) if was == "attached" else None
            if was == "attached":
                self.log.xplane("ToLiss EFB: tug disconnected.\n")

    def _tug_steer(self, phi_deg, take=False):
        """Nose wheel steering: take it over and set the angle, or (None) set it straight and hand it back."""
        try:
            if phi_deg is None:
                if getattr(self, "_steer_on", False):
                    self._tire_steer(0.0)
                    self._set_any("sim/operation/override/override_wheel_steer", 0)
                    self._steer_on = False
                return
            if take and not self._steer_on:
                self._set_any("sim/operation/override/override_wheel_steer", 1)
                self._steer_on = True
            if self._steer_on:
                self._tire_steer(phi_deg)
        except Exception as e:
            self.log.xplane(f"ToLiss EFB: nose wheel steering: {e}\n")

    def _nose_index(self):
        if getattr(self, "_nw_i", None) is None:
            self._nw_i = 0
            try:
                ref = self._dref("sim/aircraft/parts/acf_gear_znodef")
                if ref is not None:
                    out = []
                    XPLMGetDatavf(ref, out, 0, 10)
                    zs = [float(z) for z in out]
                    cand = [i for i, z in enumerate(zs) if abs(z) > 0.01]
                    if cand:
                        self._nw_i = min(cand, key=lambda i: zs[i])      # the most forward gear
            except Exception:
                pass
        return self._nw_i

    def _tire_steer(self, deg):
        ref = self._dref("sim/flightmodel/parts/tire_steer_cmd")
        if ref is not None:
            XPLMSetDatavf(ref, [float(deg)], self._nose_index(), 1)

    def _set_any(self, name, value):
        ref = self._dref(name)
        if ref is None:
            return
        types = XPLMGetDataRefTypes(ref)
        if types & xplmType_IntArray:
            n = XPLMGetDatavi(ref, None, 0, 0) or 1
            XPLMSetDatavi(ref, [int(value)] * n, 0, n)
        elif types & xplmType_Int:
            XPLMSetDatai(ref, int(value))
        elif types & xplmType_FloatArray:
            n = XPLMGetDatavf(ref, None, 0, 0) or 1
            XPLMSetDatavf(ref, [float(value)] * n, 0, n)
        else:
            XPLMSetDataf(ref, float(value))

    def _tug_nose_frame(self, phi):
        """The nose wheel's position and the heading the truck holds on it: (x, z, heading)."""
        g = self._get_f
        x, z, h = g("sim/flightmodel/position/local_x"), g("sim/flightmodel/position/local_z"), g("sim/flightmodel/position/psi")
        geom = getattr(self, "_push_geom_cache", None) or self._push_geometry()
        self._push_geom_cache = geom
        na = geom["nose_ahead"]
        return x + na * math.sin(math.radians(h)), z - na * math.cos(math.radians(h)), (h + phi) % 360

    def _tug_place_rel(self, frame, fwd, right, hoff):
        """Place the tug's origin at (fwd, right) metres from the nose wheel in the given frame, heading frame + hoff."""
        nx, nz, fh = frame
        r = math.radians(fh)
        x = nx + fwd * math.sin(r) + right * math.cos(r)
        z = nz - fwd * math.cos(r) + right * math.sin(r)
        self._tug_place(x, z, (fh + hoff) % 360)

    def _tug_place(self, x, z, hdg):
        t = self.tug
        # wheels: turning with the distance travelled (signed: forwards is the way the tug faces), steering with the
        # rate of turn along the way
        last = t.get("last")
        if last is not None:
            dx, dz = x - last[0], z - last[1]
            ds = math.hypot(dx, dz)
            if ds > 1e-4:
                r = math.radians(hdg)
                fwd = dx * math.sin(r) - dz * math.cos(r)
                sgn = 1.0 if fwd >= 0 else -1.0
                t["dist"] += sgn * ds
                dh = math.radians(((hdg - last[2] + 540) % 360) - 180)
                want = max(-30.0, min(30.0, math.degrees(math.atan(HALF_WB * dh / ds)) * sgn))
                t["steer"] += (want - t["steer"]) * 0.2
        t["last"] = (x, z, hdg)
        t["pose"] = (x, z, hdg)
        if t.get("inst") is None:
            return
        y0 = self._get_f("sim/flightmodel/position/local_y")
        gy = self._probe_terrain_y(x, y0 + 20.0, z)
        y = gy if gy is not None else y0 - (getattr(self, "gear_height_m", None) or 3.0)
        roll = math.degrees(t["dist"] / WHEEL_R) % 360
        bar_roll = math.degrees(t["dist"] / BAR_WHEEL_R) % 360
        vals = [1.0, 1.0 if t["attached"] else 0.0, t["steer"], 0.0, 0.0] + [roll] * 4 + [bar_roll, 0.0, 0.0]
        pos = (x, y, z, 0.0, hdg % 360, 0.0)
        XPLMInstanceSetPosition(t["inst"], pos, vals)
        if t.get("drv") is not None:
            XPLMInstanceSetPosition(t["drv"], pos, [1.0])

    def _tug_tick(self):
        """Every frame while a tug exists: arriving, attached or leaving."""
        t = self.tug
        now = time.time()
        pb = getattr(self, "push", None)
        phi = float(pb.get("phi", 0.0)) if pb else t.get("phi", 0.0)
        t["phi"] = phi
        k = now - t["t0"]
        if t["phase"] == "arrive":
            if t.get("inst") is not None:
                self._tug_place_rel(self._tug_nose_frame(0.0), *_at(ARRIVAL, k))
            if k >= t["arrive_s"]:
                self._tug_attach()
        elif t["phase"] == "attached":
            # facing the aircraft, the towbar tip on the nose wheel, in line with the nose wheel's angle
            self._tug_place_rel(self._tug_nose_frame(phi), TIP, 0.0, 180.0)
            self._tug_steer(phi)
        else:
            frame = t.get("leave_from")
            if frame is None or t.get("inst") is None or k >= DEPARTURE[-1][0]:
                self._tug_destroy(t)
                self.tug = None
                return
            self._tug_place_rel(frame, *_at(DEPARTURE, k))

    def _tug_state(self):
        t = getattr(self, "tug", None)
        st = self._tug_settings()
        eta = None
        if t and t["phase"] == "arrive":
            eta = max(0, int(math.ceil(t["t0"] + t["arrive_s"] - time.time())))
        return {"tug_phase": t["phase"] if t else None, "tug_shown": bool(t and t.get("inst") is not None), "tug_eta": eta,
                "tug_missing": bool(t and t.get("inst") is None and st["truck"]), "show_truck": st["truck"],
                "bp_flags": bool(getattr(self, "_bp_handles", None)),
                "steering_taken": bool(getattr(self, "_steer_on", False))}
