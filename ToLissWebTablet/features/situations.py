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

class SituationMixin:
    def _read_string_dataref(self, ref):
        if ref is None:
            return ""
        try:
            length = XPLMGetDatab(ref, None, 0, 0)
            if not length:
                return ""
            out = []
            copied = XPLMGetDatab(ref, out, 0, length)
            if copied is None:
                copied = len(out)
            raw = bytes(out[:copied])
            return raw.split(b'\x00', 1)[0].decode('utf-8', errors='ignore')
        except Exception:
            return ""
    def _write_string_dataref(self, ref, value):
        if ref is None:
            return False
        try:
            encoded = str(value).replace('\x00', '').encode('utf-8')
            capacity = XPLMGetDatab(ref, None, 0, 0)
            if capacity and capacity > 0:
                encoded = encoded[:max(0, capacity - 1)]
            payload = encoded + b'\x00'
            try:
                XPLMSetDatab(ref, list(payload), 0, len(payload))
            except Exception:
                XPLMSetDatab(ref, payload, 0, len(payload))
            return True
        except Exception as e:
            self.log.xplane(f"ToLiss EFB: Situation name write error: {e}\n")
            return False
    def _get_situation_interface(self):
        refs = {
            "total": XPLMFindDataRef("toliss_airbus/iscsinterface/total_number_situations"),
            "index": XPLMFindDataRef("toliss_airbus/iscsinterface/current_situation_index"),
            "name": XPLMFindDataRef("toliss_airbus/iscsinterface/current_sit_name"),
            "save_name": XPLMFindDataRef("toliss_airbus/iscsinterface/sit_saving_name"),
        }
        cmds = {
            "full": XPLMFindCommand("toliss_airbus/iscsinterface/load_current_sit"),
            "config": XPLMFindCommand("toliss_airbus/iscsinterface/load_current_sit_as_config"),
            "mcdu": XPLMFindCommand("toliss_airbus/iscsinterface/load_current_sit_as_mcdu"),
            "save": XPLMFindCommand("toliss_airbus/iscsinterface/save_sit"),
        }
        return refs, cmds
    def _process_situation_requests(self):
        while not self._situation_requests.empty():
            try:
                request = self._situation_requests.get_nowait()
            except queue.Empty:
                break

            response_q = request.get("response")
            result = {"status": "error", "supported": False, "message": "Unknown request."}

            try:
                refs, cmds = self._get_situation_interface()
                can_list = refs["total"] is not None and refs["index"] is not None and refs["name"] is not None
                can_save = refs["save_name"] is not None and cmds["save"] is not None
                action = request.get("action")

                if action == "list":
                    if not can_list:
                        result = {"status": "error", "supported": False, "message": "ToLiss Pro features not detected. Situation interface is unavailable."}
                    else:
                        total = max(0, min(int(XPLMGetDatai(refs["total"])), 2000))
                        original_index = int(XPLMGetDatai(refs["index"]))
                        situations = []
                        try:
                            for index in range(total):
                                XPLMSetDatai(refs["index"], index)
                                name = self._read_string_dataref(refs["name"]).strip()
                                situations.append({"index": index, "name": name if name else f"Situation {index + 1}"})
                        finally:
                            if total > 0 and 0 <= original_index < total:
                                XPLMSetDatai(refs["index"], original_index)

                        result = {
                            "status": "success",
                            "supported": True,
                            "total": total,
                            "current_index": original_index,
                            "can_save": can_save,
                            "situations": situations
                        }

                elif action == "load":
                    if not can_list:
                        result = {"status": "error", "supported": False, "message": "ToLiss Pro features not detected. Situation interface is unavailable."}
                    else:
                        try:
                            index = int(request.get("index"))
                        except (TypeError, ValueError):
                            index = -1
                        mode = str(request.get("mode", "full")).lower()
                        command = cmds.get(mode)
                        total = max(0, int(XPLMGetDatai(refs["total"])))

                        if index < 0 or index >= total:
                            result = {"status": "error", "supported": True, "message": "Invalid situation index."}
                        elif command is None:
                            result = {"status": "error", "supported": True, "message": "Situation load command is unavailable."}
                        else:
                            XPLMSetDatai(refs["index"], index)
                            name = self._read_string_dataref(refs["name"]).strip()
                            XPLMCommandBegin(command)
                            self.active_commands.append(command)
                            result = {"status": "success", "supported": True, "index": index, "name": name, "mode": mode}

                elif action == "save":
                    if not can_save:
                        result = {"status": "error", "supported": can_list, "message": "Situation saving is unavailable."}
                    else:
                        name = str(request.get("name", "")).strip().replace('\x00', '')
                        if not name:
                            result = {"status": "error", "supported": True, "message": "Situation name is required."}
                        elif len(name.encode('utf-8')) > 240:
                            result = {"status": "error", "supported": True, "message": "Situation name is too long."}
                        elif not self._write_string_dataref(refs["save_name"], name):
                            result = {"status": "error", "supported": True, "message": "Could not set the situation name."}
                        else:
                            XPLMCommandBegin(cmds["save"])
                            self.active_commands.append(cmds["save"])
                            result = {"status": "success", "supported": True, "name": name}
            except Exception as e:
                self.log.xplane(f"ToLiss EFB: Situation error: {e}\n")
                result = {"status": "error", "supported": False, "message": str(e)}

            if response_q is not None:
                try:
                    response_q.put_nowait(result)
                except queue.Full:
                    pass
