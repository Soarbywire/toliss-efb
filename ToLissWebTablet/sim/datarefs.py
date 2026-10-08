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

from .mcdu import MCDU_COLS

class DataRefMixin:
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
    def _read_bytes(self, ref, n=MCDU_COLS):
        try:
            out = []
            copied = XPLMGetDatab(ref, out, 0, n)
            if copied is None:
                copied = len(out)
            return [int(b) & 0xFF for b in out[:copied]]
        except Exception:
            return []
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
            self.log.xplane(f"ToLiss EFB: Terrain probe unavailable: {e}\n")
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
