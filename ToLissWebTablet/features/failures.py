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

class FailureMixin:
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
        while not self._failure_requests.empty():
            try:
                request = self._failure_requests.get_nowait()
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
                self.log.xplane(f"ToLiss EFB: Failure error: {e}\n")
                result = {"status": "error", "supported": False, "message": str(e)}

            if response_q is not None:
                try:
                    response_q.put_nowait(result)
                except queue.Full:
                    pass
