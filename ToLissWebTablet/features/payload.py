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

class PayloadMixin:
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
        while not self._payload_requests.empty():
            try:
                request = self._payload_requests.get_nowait()
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
                self.log.xplane(f"ToLiss EFB: Payload apply error: {e}\n")
                result = {"status": "error", "message": str(e)}

            if response_q is not None:
                try:
                    response_q.put_nowait(result)
                except queue.Full:
                    pass
