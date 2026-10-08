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

from .mcdu import MCDU_COLS, MCDU_SYMBOLS, mcdu_display_refs

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
    # FCU and EFIS control panels
    "fcu_spd_dial": ("sim/cockpit2/autopilot/airspeed_dial_kts_mach", None), "spd_is_mach": ("sim/cockpit/autopilot/airspeed_is_mach", None),
    "spd_dashed": ("AirbusFBW/SPDdashed", None), "hdg_dashed": ("AirbusFBW/HDGdashed", None), "vs_dashed": ("AirbusFBW/VSdashed", None),
    "alt_managed": ("AirbusFBW/ALTmanaged", None), "hdgtrk": ("AirbusFBW/HDGTRKmode", None),
    "fcu_vs": ("sim/cockpit/autopilot/vertical_velocity", None), "fcu_fpa": ("sim/cockpit2/autopilot/fpa", None),
    "loc_lt": ("AirbusFBW/LOCilluminated", None), "appr_lt": ("AirbusFBW/APPRilluminated", None),
    "exped_lt": ("AirbusFBW/EXPEDilluminated", None), "fd2": ("AirbusFBW/FD2Engage", None), "ls_fo": ("AirbusFBW/ILSonFO", None),
    "baro_fo": ("sim/cockpit2/gauges/actuators/barometer_setting_in_hg_copilot", None),
    "baro_std_fo": ("AirbusFBW/BaroStdFO", None), "baro_hpa_fo": ("AirbusFBW/BaroUnitFO", None),
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

class DisplayMixin:
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
        elif not self._mcdu_keys.empty():
            try:
                side, key = self._mcdu_keys.get_nowait()
                cmd = XPLMFindCommand(f"AirbusFBW/MCDU{side}{key}")
                if cmd:
                    XPLMCommandBegin(cmd)
                    self._mcdu_key_active = cmd
                else:
                    self.log.xplane(f"ToLiss EFB: MCDU key not found: AirbusFBW/MCDU{side}{key}\n")
            except Exception:
                pass
        # Screen reads
        while not self._mcdu_requests.empty():
            try:
                side, reply_q = self._mcdu_requests.get_nowait()
                reply_q.put(self._mcdu_screen(side))
            except Exception as e:
                self.log.xplane(f"ToLiss EFB: MCDU read error: {e}\n")
