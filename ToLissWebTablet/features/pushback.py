"""Built-in pushback: the EFB tows the aircraft along a planned path.

The path comes from a towing model: the tug steers the nose wheel (within Airbus's 90 degree towing limit) and the
aircraft pivots about its main gear. The page's preview asks for the same plan (/api/push/plan), so the map shows
exactly what the push will do. Everything here runs on X-Plane's main thread (from the instructor queue and tick).

X-Plane's flight model stays in charge throughout (as with Better Pushback): its gear keeps the wheels on the
surface, whatever the slope or the scenery. The EFB is the tug: each frame it sets the aircraft's horizontal speed
and its turn rate to follow the path, correcting any drift, and holds it still (speed zero) while waiting.
"""
from __future__ import annotations

import math
import time

from XPLMDataAccess import *


class PushbackMixin:
    PARK_BRAKE_REFS = (
        "sim/cockpit2/controls/parking_brake_ratio",
        "AirbusFBW/ParkBrake",
    )
    PUSH_ACCEL = 0.35             # m/s2, gentle start and stop
    PUSH_K_POS = 0.8              # 1/s: how firmly drift from the path is corrected
    PUSH_MAX_CORR = 0.6           # m/s: at most this much correction on top of the towing speed
    PUSH_K_HDG = 0.8              # 1/s: heading correction
    PUSH_MAX_YAW = 6.0            # deg/s
    # Airbus type dimensions: wheelbase (m, nose gear to main gear) as a fallback, fuselage length and span (m)
    PUSH_TYPES = {"A318": (10.3, 31.4, 34.1), "A319": (11.0, 33.8, 35.8), "A320": (12.6, 37.6, 35.8), "A20N": (12.6, 37.6, 35.8),
                  "A321": (16.9, 44.5, 35.8), "A21N": (16.9, 44.5, 35.8), "A332": (22.2, 58.8, 60.3), "A333": (25.4, 63.7, 60.3),
                  "A338": (22.2, 58.8, 64.0), "A339": (25.4, 63.7, 64.0), "A343": (25.6, 63.7, 60.3), "A346": (32.9, 75.4, 63.5)}
    PUSH_STEER = {"gentle": 45.0, "normal": 60.0, "tight": 80.0}     # nose wheel angle; Airbus's towing limit is 90 deg

    def _push_geometry(self):
        """Wheelbase and the CG's position between the gears, from the loaded aircraft (acf_gear_znodef, metres,
        relative to the CG, negative = forward), with type figures as a fallback; plus fuselage length and span."""
        icao = ""
        try:
            icao = self._read_string_dataref(self._dref("sim/aircraft/view/acf_ICAO")).strip().upper()
        except Exception:
            pass
        wb_fb, length, span = self.PUSH_TYPES.get(icao, (12.6, 37.6, 35.8))
        nose, main = None, None
        try:
            ref = self._dref("sim/aircraft/parts/acf_gear_znodef")
            if ref is not None:
                out = []
                XPLMGetDatavf(ref, out, 0, 10)
                zs = [float(z) for z in out if z is not None and abs(float(z)) > 0.01]
                if zs and min(zs) < -1.0 and max(zs) > 0.0:
                    nose, main = -min(zs), max(zs)
        except Exception:
            pass
        if not nose or not (3.0 < nose + main < 45.0):
            nose = self._nose_gear_ahead_m() or wb_fb * 0.92
            main = max(0.3, wb_fb - nose)
        return {"icao": icao, "nose_ahead": round(nose, 2), "main_behind": round(main, 2), "wheelbase": round(nose + main, 2),
                "length": length, "span": span, "tail_behind": round(length * 0.52, 2)}

    @staticmethod
    def push_path(x0, z0, hdg0, back_m, turn_deg, side, final_m, forward=False, geom=None, steer_deg=60.0, step=0.25):
        """A towed push: the tug moves the nose wheel at a steering angle and the aircraft pivots about its main gear.
        Returns poses (s, x, z, heading) of the CG, with s the distance travelled by the nose wheel (the tug).
        Backwards: straight, then the tug steers the nose wheel smoothly into the turn (tail to <side>), holds it and
        straightens out on the requested heading, then the final straight. Forwards: the turn is to <side>."""
        g = geom or {"wheelbase": 12.6, "main_behind": 1.2}
        W, mb = max(3.0, float(g["wheelbase"])), float(g["main_behind"])
        vn = 1.0 if forward else -1.0                 # nose wheel travel along its own direction (+ forwards)
        sgn_phi = -1.0 if side == "L" else 1.0        # tail left / turn left: nose wheel angled left
        target = math.radians(max(0.0, min(180.0, float(turn_deg))))
        u = lambda h: (math.sin(math.radians(h)), math.cos(math.radians(h)))     # (east, north) of the nose direction
        e0, n0 = u(hdg0)
        mx, mz, h = x0 - mb * e0, z0 + mb * n0, hdg0                              # main gear midpoint (local x east, -z north)
        pts, s_, cur = [], 0.0, [0.0]
        def emit():
            e, n = u(h)
            pts.append((s_, mx + mb * e, mz - mb * n, h % 360, math.degrees(cur[0])))   # last: nose wheel angle
        emit()
        def move(phi_rad, ds):
            nonlocal mx, mz, h, s_
            cur[0] = phi_rad
            h += math.degrees(vn * math.sin(phi_rad) * ds / W)
            e, n = u(h)
            dm = vn * math.cos(phi_rad) * ds
            mx += dm * e
            mz -= dm * n
            s_ += ds
            emit()
        for _ in range(int(max(0.0, float(back_m)) / step)):
            move(0.0, step)
        if target > 0:
            Lr = 5.0                                   # nose wheel travel while the tug steers in / out
            phi_max = math.radians(max(5.0, min(90.0, float(steer_deg))))
            ramp_turn = lambda p: (Lr / W) * (1 - math.cos(p)) / p                # heading change of one ramp (rad)
            while phi_max > math.radians(5.0) and 2 * ramp_turn(phi_max) > target:  # a small turn: steer less
                phi_max *= 0.9
            turned, phi, guard = 0.0, 0.0, 0
            while phi < phi_max and guard < 4000:      # steer in
                phi = min(phi_max, phi + phi_max * step / Lr)
                move(sgn_phi * phi, step); turned += math.sin(phi) * step / W; guard += 1
            while turned + ramp_turn(phi_max) < target and guard < 20000:          # hold
                move(sgn_phi * phi, step); turned += math.sin(phi) * step / W; guard += 1
            while phi > 0 and guard < 24000:           # straighten out
                phi = max(0.0, phi - phi_max * step / Lr)
                move(sgn_phi * phi, step); turned += math.sin(phi) * step / W; guard += 1
        for _ in range(int(max(0.0, float(final_m)) / step)):
            move(0.0, step)
        return pts

    def _push_request(self, kind, item):
        """A pushback request from the page (runs on the main thread, from the instructor queue)."""
        if kind == "push_start":
            self.push_done = None
            res = self._push_start(item)
        elif kind in ("push_pause", "push_resume"):
            if getattr(self, 'push', None) and self.push.get("hold"):
                res = {"ok": False, "message": "The push has ended: set the parking brake (or press Release now)."}
            elif getattr(self, 'push', None):
                if kind == "push_resume" and self._brake_set():
                    res = {"ok": False, "message": "Release the parking brake first."}
                else:
                    self.push["paused"] = kind == "push_pause"
                    if kind == "push_resume":
                        self.push["braked"] = False
                    res = {"ok": True, "message": "Paused." if kind == "push_pause" else "Resuming."}
            else:
                res = {"ok": False, "message": "No pushback is running."}
        elif kind == "push_stop":
            pb = getattr(self, 'push', None)
            if pb and (pb.get("hold") or item.get("release")):
                self._push_end("Released without the parking brake.")      # "Release now"
                res = {"ok": True, "message": "Released."}
            elif pb:
                if pb.get("connecting") or pb["await_brake"]:
                    self._push_end("Pushback cancelled.")
                    res = {"ok": True, "message": "Pushback cancelled."}
                else:
                    self._push_start_hold(pb, "Parking brake set: pushback stopped. Disconnect the tug.")
                    res = {"ok": True, "message": "Stopped: holding the aircraft until the parking brake is set."}
            else:
                res = {"ok": True, "message": "Stopped."}
        elif kind == "push_state":
            res = {"ok": True}
        elif kind == "push_plan":
            res = self._push_plan(item)
        else:
            res = {"ok": False, "message": "Unknown pushback request."}
        return res

    def _brake_set(self):
        """The parking brake as the pilot set it: ToLiss's switch (AirbusFBW/ParkBrake) when the aircraft has it.
        X-Plane's own parking_brake_ratio is only the fallback: while a tug is connected (the tug flags that give NWS
        DISC) ToLiss can hold that one on, so it must not decide whether the brake was released."""
        sw = "AirbusFBW/ParkBrake"
        if self._dref(sw) is not None:
            on = self._get_f(sw) >= 0.5
        else:
            on = self._get_f("sim/cockpit2/controls/parking_brake_ratio") >= 0.5
        if getattr(self, "push", None) and on != getattr(self, "_brake_logged", None):
            self._brake_logged = on
            try:
                self.log.xplane(f"ToLiss EFB: parking brake {'set' if on else 'released'} (ToLiss switch "
                                f"{self._get_f(sw) if self._dref(sw) is not None else 'n/a'}, X-Plane ratio "
                                f"{self._get_f('sim/cockpit2/controls/parking_brake_ratio'):.2f})\n")
            except Exception:
                pass
        return on
    def _push_start(self, item):
        g = self._get_f
        if getattr(self, 'slew', None):
            return {"ok": False, "message": "End slew first."}
        if g("sim/flightmodel/failures/onground_any") < 1:
            return {"ok": False, "message": "Pushback works on the ground only."}
        if getattr(self, 'push', None):
            return {"ok": False, "message": "A pushback is already running."}
        if g("sim/time/paused") >= 1:
            self._sim_pause(False)
        self._track_gear_height()
        x0, z0, h0 = g("sim/flightmodel/position/local_x"), g("sim/flightmodel/position/local_z"), g("sim/flightmodel/position/psi")
        forward = bool(item.get("forward"))
        geom = self._push_geometry()
        steer = self.PUSH_STEER.get(str(item.get("tightness", "normal")), 60.0)
        pts = self.push_path(x0, z0, h0, float(item.get("back_m", 0)), float(item.get("turn_deg", 0)), str(item.get("side", "L")),
                             float(item.get("final_m", 0)), forward, geom, steer)
        if len(pts) < 3:
            return {"ok": False, "message": "Set a distance or a turn first."}
        self.push = {"pts": pts, "total": pts[-1][0], "s": 0.0, "v": 0.0, "target": max(0.3, min(3.0, float(item.get("speed_kt", 3)) * 0.514444)),
                     "paused": False, "forward": forward, "t": time.time(), "i": 0, "await_brake": False, "braked": False,
                     "connecting": True, "start_xz": (x0, z0), "start_hdg": h0, "held": False, "go_at": 0.0, "settle": None}
        # the tug drives up and takes the nose wheel first: nothing moves until it is connected (a safety margin
        # covers a truck that never reports in)
        arrive = self._tug_begin()
        self.push["connect_until"] = time.time() + arrive + 5.0
        self.push["phi"] = 0.0
        return {"ok": True, "message": f"Tug on its way: it connects in about {round(arrive)} s."}
    def _height_above_terrain(self, x, z):
        """The aircraft's reference point above the terrain right now (keeps the gear exactly as compressed as it is)."""
        y = self._get_f("sim/flightmodel/position/local_y")
        ty = self._probe_terrain_y(x, y + 50.0, z)
        if ty is None:
            return getattr(self, 'gear_height_m', None) or max(2.0, self._get_f("sim/flightmodel/position/y_agl"))
        return y - ty
    def _push_plan(self, item):
        """The plan for the page's preview, from the same model as the push: CG poses and the tail sweep, as metres
        east/north of the aircraft's position now."""
        h0 = self._get_f("sim/flightmodel/position/psi")
        geom = self._push_geometry()
        steer = self.PUSH_STEER.get(str(item.get("tightness", "normal")), 60.0)
        pts = self.push_path(0.0, 0.0, h0, float(item.get("back_m", 0)), float(item.get("turn_deg", 0)), str(item.get("side", "L")),
                             float(item.get("final_m", 0)), bool(item.get("forward")), geom, steer)
        keep = [p for i, p in enumerate(pts) if i % 4 == 0 or i == len(pts) - 1]           # every metre
        tb = geom["tail_behind"]
        cg = [[round(p[1], 2), round(-p[2], 2), round(p[3], 1)] for p in keep]
        tail = [[round(e - tb * math.sin(math.radians(h)), 2), round(n - tb * math.cos(math.radians(h)), 2)] for e, n, h in cg]
        return {"ok": True, "cg": cg, "tail": tail, "geom": geom, "hdg0": round(h0, 1), "length_m": round(pts[-1][0], 1)}

    def _push_end(self, message):
        self.push = None
        try:
            self._tug_release()
        except Exception:
            pass
        for d in ("local_vx", "local_vz", "Rrad"):
            self._set_num("sim/flightmodel/position/" + d, 0.0)
        self.push_done = message
    def _push_hold(self, pb):
        """Hold the aircraft where the push ended (or was stopped), so idle thrust can't move it, until the brake is set."""
        x, z = pb["hold_xz"]
        self._push_hold_at(x, z, pb["hold_hdg"])
        if self._brake_set():
            self._push_end(pb.get("hold_msg_done", "Parking brake set: pushback complete. Disconnect the tug."))
    def _push_start_hold(self, pb, done_msg):
        pb["hold"] = True
        pb["paused"], pb["v"] = True, 0.0
        pb["hold_xz"] = (self._get_f("sim/flightmodel/position/local_x"), self._get_f("sim/flightmodel/position/local_z"))
        pb["hold_hdg"] = self._get_f("sim/flightmodel/position/psi")
        pb["hold_msg_done"] = done_msg
    def _push_still(self, pb):
        """Keep the aircraft where the push started (waiting for the tug, or for the go after the brake)."""
        pb["held"] = True
        x, z = pb["start_xz"]
        self._push_hold_at(x, z, pb["start_hdg"])

    def _push_drive(self, vx, vz, yaw_deg_s):
        """The tug at work: the aircraft's horizontal speed (local x east, z south, m/s) and turn rate. Height, pitch
        and bank are left to X-Plane's gear, so the wheels stay on the surface."""
        self._set_num("sim/flightmodel/position/local_vx", vx)
        self._set_num("sim/flightmodel/position/local_vz", vz)
        self._set_num("sim/flightmodel/position/Rrad", math.radians(yaw_deg_s))
        self._set_num("sim/flightmodel/position/R", yaw_deg_s)

    def _push_hold_at(self, x, z, hdg):
        """Hold the aircraft at a spot with the tug (idle thrust can't move it): any creep is pulled back gently."""
        g = self._get_f
        ex, ez = x - g("sim/flightmodel/position/local_x"), z - g("sim/flightmodel/position/local_z")
        d = math.hypot(ex, ez)
        k = min(self.PUSH_K_POS, 0.3 / d) if d > 1e-6 else 0.0
        dh = ((hdg - g("sim/flightmodel/position/psi") + 540) % 360) - 180
        self._push_drive(ex * k, ez * k, max(-2.0, min(2.0, dh * self.PUSH_K_HDG)))

    def _push_follow(self, pb, x, z, hdg, vx_path, vz_path, yaw_path):
        """Follow the path: its own speed and turn rate, plus a correction towards where the aircraft should be."""
        g = self._get_f
        ex, ez = x - g("sim/flightmodel/position/local_x"), z - g("sim/flightmodel/position/local_z")
        cx, cz = ex * self.PUSH_K_POS, ez * self.PUSH_K_POS
        c = math.hypot(cx, cz)
        if c > self.PUSH_MAX_CORR:
            cx, cz = cx * self.PUSH_MAX_CORR / c, cz * self.PUSH_MAX_CORR / c
        psi = g("sim/flightmodel/position/psi")
        dh = ((hdg - psi + 540) % 360) - 180
        yaw = max(-self.PUSH_MAX_YAW, min(self.PUSH_MAX_YAW, yaw_path + dh * self.PUSH_K_HDG))
        self._push_drive(vx_path + cx, vz_path + cz, yaw)
        if abs(dh) > 2.0:
            self._push_nudge_heading(psi + dh * 0.25)       # the turn rate isn't taking: turn the aircraft a little directly
        return math.hypot(ex, ez)

    def _push_nudge_heading(self, hdg):
        """Set the heading only (pitch and bank stay as X-Plane's gear has them)."""
        try:
            g = self._get_f
            q_ref = self._dref("sim/flightmodel/position/q")
            if q_ref is not None:
                XPLMSetDatavf(q_ref, self._euler_to_quat(hdg % 360, g("sim/flightmodel/position/theta"), g("sim/flightmodel/position/phi")), 0, 4)
        except Exception:
            pass

    def _push_progress(self, pb):
        """How far along the path the aircraft really is (the nearest path point around where we are)."""
        g = self._get_f
        ax, az = g("sim/flightmodel/position/local_x"), g("sim/flightmodel/position/local_z")
        pts, i = pb["pts"], pb["i"]
        best, bs = None, pb["s"]
        for j in range(max(0, i - 8), min(len(pts), i + 40)):
            d = (pts[j][1] - ax) ** 2 + (pts[j][2] - az) ** 2
            if best is None or d < best:
                best, bs = d, pts[j][0]
        return bs

    def _push_tick(self):
        pb = self.push
        now = time.time()
        if pb.get("hold"):
            return self._push_hold(pb)
        if pb["connecting"]:
            t = getattr(self, "tug", None)
            if t is not None and not t.get("attached") and now < pb["connect_until"]:
                # the tug is still driving up: nothing moves; without the parking brake the EFB holds the aircraft
                if pb["held"] or not self._brake_set():
                    self._push_still(pb)
                pb["t"] = now
                return
            # connected: with the parking brake set, wait for it to be released; otherwise go in a moment
            pb["connecting"] = False
            pb["await_brake"] = self._brake_set()
            if not pb["await_brake"]:
                pb["go_at"] = now + 2.0
        if pb["await_brake"]:
            if self._brake_set():
                if pb["held"]:
                    self._push_still(pb)
                pb["t"] = now
                return                                   # waiting for the brake to be released
            pb["await_brake"] = False
            pb["go_at"] = now + 1.5
        elif not pb["braked"] and self._brake_set():
            # brake set during the push: stop at once (a real tow bar would be at risk); Resume once released
            pb["braked"], pb["paused"], pb["v"] = True, True, 0.0
        if now < pb["go_at"]:
            self._push_still(pb)                         # the tug takes up the load
            pb["t"] = now
            return
        pb["held"] = True
        dt, pb["t"] = min(0.1, max(0.0, now - pb["t"])), now
        remaining = pb["total"] - pb["s"]
        # speed: accelerate gently to towing speed, slow down smoothly to stop at the end (or when paused)
        want = 0.0 if pb["paused"] else min(pb["target"], math.sqrt(max(0.0, 2 * self.PUSH_ACCEL * remaining)))
        if pb["v"] < want:
            pb["v"] = min(want, pb["v"] + self.PUSH_ACCEL * dt)
        else:
            pb["v"] = max(want, pb["v"] - self.PUSH_ACCEL * 2 * dt)
        # the tow point moves on with the towing speed, but never runs away from an aircraft that is held back
        pb["s"] = min(pb["total"], pb["s"] + pb["v"] * dt, self._push_progress(pb) + 1.5)
        pts, i = pb["pts"], pb["i"]
        while i + 1 < len(pts) and pts[i + 1][0] <= pb["s"]:
            i += 1
        pb["i"] = i
        a = pts[i]
        b = pts[min(i + 1, len(pts) - 1)]
        f = 0.0 if b[0] == a[0] else (pb["s"] - a[0]) / (b[0] - a[0])
        x, z = a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f
        dh = ((b[3] - a[3] + 540) % 360) - 180
        pb["phi"] = a[4] + (b[4] - a[4]) * f if len(a) > 4 else 0.0
        hdg = (a[3] + dh * f) % 360
        # the path's own velocity and turn rate at this point (the towing model: no sideways slip at the main gear)
        ds = b[0] - a[0]
        if ds > 1e-6 and pb["v"] > 0:
            vx_p, vz_p = (b[1] - a[1]) / ds * pb["v"], (b[2] - a[2]) / ds * pb["v"]
            yaw_p = dh / ds * pb["v"]
        else:
            vx_p = vz_p = yaw_p = 0.0
        self._push_follow(pb, x, z, hdg, vx_p, vz_p, yaw_p)
        if pb["s"] >= pb["total"] - 0.01 and pb["v"] < 0.05:
            # at the end: settle for a second, then keep holding until the parking brake is set
            if pb["settle"] is None:
                pb["settle"] = now + 1.0
            elif now >= pb["settle"]:
                # keep holding the aircraft until the parking brake is set (engines may be running)
                self._push_start_hold(pb, "Parking brake set: pushback complete. Disconnect the tug.")
    def _push_state(self):
        pb = getattr(self, 'push', None)
        st = {"pushing": bool(pb), "push_done": getattr(self, 'push_done', None)}
        try:
            st.update(self._tug_state())
        except Exception:
            pass
        if pb:
            st.update(push_connecting=bool(pb.get("connecting")), push_paused=pb["paused"], push_s=round(pb["s"], 1), push_total=round(pb["total"], 1),
                      push_kt=round(pb["v"] / 0.514444, 1), push_forward=pb["forward"],
                      push_await_brake=pb["await_brake"], push_braked=pb["braked"], push_hold=bool(pb.get("hold")))
        return st
