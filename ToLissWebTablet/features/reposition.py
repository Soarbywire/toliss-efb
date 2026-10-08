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

from ..services.airports import _gm_dist_m

class RepositionMixin:
    _NOSE_GEAR_FALLBACK_M = {
        "A319": 14.0,
        "A320": 12.7,
        "A20N": 12.7,
        "A321": 16.8,
        "A21N": 16.8,
        "A339": 22.8,
        "A346": 27.0,
    }

    def _sim_pause(self, on):
        try:
            cmd = XPLMFindCommand("sim/operation/pause_on" if on else "sim/operation/pause_off")
            if cmd:
                XPLMCommandOnce(cmd)
        except Exception as e:
            self.log.xplane(f"ToLiss EFB: Pause command failed: {e}\n")
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
                    self.log.xplane(f"ToLiss EFB: LS command not found ({cmds[0]})\n")
    def _track_gear_height(self):
        """Remember how high the aircraft sits above the ground when parked, so we can place it the same way."""
        try:
            if self._get_f("sim/flightmodel/failures/onground_any") >= 1 and self._get_f("sim/flightmodel/position/groundspeed") < 1.0:
                h = self._get_f("sim/flightmodel/position/y_agl", -1.0)
                if 0.3 < h < 15.0:
                    self.gear_height_m = h
        except Exception:
            pass
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
            self.log.xplane(f"ToLiss EFB: Could not read gear positions: {e}\n")
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
                self.log.xplane("ToLiss EFB: Could not unpause for repositioning; trying anyway.\n")
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
                    self.log.xplane(f"ToLiss EFB: PlaceUserAtAirport failed ({e}); snapping directly.\n")
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
                self.log.xplane(f"ToLiss EFB: In-air reposition error: {e}\n")
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
            self.log.xplane(f"ToLiss EFB: Teleport error: {e}\n")
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
