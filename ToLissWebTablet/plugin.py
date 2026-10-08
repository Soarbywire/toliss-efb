from __future__ import annotations

import os
import queue
import re
import shutil
import threading

from XPLMDefs import *
from XPLMUtilities import *
from XPLMDataAccess import *
from XPLMProcessing import *
from XPLMPlanes import *
from XPLMGraphics import *
from XPLMScenery import *

from .core.bridge import SimBridge
from .core.config import ConfigStore
from .core.context import AppContext
from .core.logging import MainThreadLogger
from .features.failures import FailureMixin
from .features.fdr import FlightRecorder
from .features.instructor import InstructorMixin
from .features.payload import PayloadMixin
from .features.pushback import PushbackMixin
from .features.replay import ReplayMixin
from .features.reposition import RepositionMixin
from .features.situations import SituationMixin
from .features.traffic import TrafficMixin
from .services.airports import AirportMixin
from .services.navdata import NavDatabase
from .services.ops import OpsCentre
from .services.updater import Updater
from .services.vatsim import VatsimService
from .sim.datarefs import DataRefMixin
from .sim.displays import DisplayMixin
from .web.server import ThreadingHTTPServer, make_handler


class PythonInterface(
    AirportMixin, SituationMixin, FailureMixin, PayloadMixin, DataRefMixin,
    DisplayMixin, InstructorMixin, PushbackMixin, RepositionMixin, ReplayMixin, TrafficMixin,
):
    """Thin coordinator. Feature behavior lives in injected mixins/services."""

    def load_config(self):
        return self.config_store.load()

    def save_config(self, config):
        self.config_store.save(dict(config))

    def XPluginStart(self):
        self.Name = "ToLiss EFB"
        self.Sig = "soarbywire.tolissefb"
        self.Desc = "Native Web interface for ToLiss EFB"
        self.xp_path = XPLMGetSystemPath()
        self.plugin_dir = os.path.join(self.xp_path, "Resources", "plugins", "PythonPlugins", "ToLissWebTablet")
        os.makedirs(self.plugin_dir, exist_ok=True)
        self.sit_dir_toliss = os.path.join(self.xp_path, "Resources", "plugins", "ToLissData", "Situations")
        os.makedirs(self.sit_dir_toliss, exist_ok=True)

        self.stop_event = threading.Event()
        self.log = MainThreadLogger()
        self.bridge = SimBridge()
        self.config_file = os.path.join(self.plugin_dir, "config.json")
        self.config_store = ConfigStore(self.config_file)
        self.context = AppContext(self.bridge, self.config_store, self.log, self.stop_event)
        self.config = self.load_config()

        self._situation_requests = queue.Queue()
        self._failure_requests = queue.Queue()
        self._payload_requests = queue.Queue()
        self._instructor_requests = queue.Queue()
        self._mcdu_requests = queue.Queue()
        self._mcdu_keys = queue.Queue()
        self.active_commands = []
        self.httpd = None
        self.server_thread = None
        self.apt_lite_db, self.apt_index, self.apt_coords = [], {}, []
        self.ground_map_cache = {}
        self.db_ready = False
        self.stream_clients, self.stream_snapshot, self.stream_seq = 0, {}, 0
        self.stream_lock = threading.Lock()
        self._dref_cache, self._mcdu_cache = {}, {}

        try:
            self.probe = XPLMCreateProbe(xplm_ProbeY)
        except Exception:
            self.probe = None
        self.terrain_probe = self.probe

        self.fdr = FlightRecorder(self)
        self.updater = Updater(self)
        self.ops = OpsCentre(self)
        self.navdb = NavDatabase(self.xp_path, self.plugin_dir, self.log, self.stop_event)
        self.navdb.load_async()
        self.vatsim = VatsimService(self.log, self.stop_event)
        self.vatsim.start()
        self.airport_thread = threading.Thread(target=self.build_apt_db, name="ToLissEFB-Airports", daemon=True)
        self.airport_thread.start()
        self.update_thread = threading.Thread(target=self.updater.check, name="ToLissEFB-Updater", daemon=True)
        self.update_thread.start()

        self.handler_class = make_handler(self)
        self.start_server_thread()
        self.flCB = self.flightLoopCallback
        XPLMRegisterFlightLoopCallback(self.flCB, -1.0, 0)
        return self.Name, self.Sig, self.Desc

    def XPluginStop(self):
        self.stop_event.set()
        try:
            self._tfc_release()                      # hand X-Plane's traffic back
        except Exception:
            pass
        self.bridge.close()
        try:
            if getattr(self, "fdr", None) and self.fdr.recording:
                self.fdr.stop("X-Plane closing")
        except Exception as exc:
            self.log.xplane(f"ToLiss EFB: FDR shutdown error: {exc}")
        try:
            XPLMUnregisterFlightLoopCallback(self.flCB, 0)
        except Exception:
            pass
        if self.httpd:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass
        for worker in (getattr(self, "server_thread", None), getattr(self, "airport_thread", None),
                       getattr(self, "update_thread", None), getattr(getattr(self, "vatsim", None), "thread", None)):
            if worker and worker.is_alive() and worker is not threading.current_thread():
                worker.join(2.0)
        for worker in (getattr(getattr(self, "ops", None), "thread", None),
                       getattr(getattr(self, "navdb", None), "thread", None),
                       getattr(getattr(self, "fdr", None), "finalise_thread", None)):
            if worker and worker.is_alive() and worker is not threading.current_thread():
                worker.join(2.0)
        if getattr(self, "probe", None) is not None:
            try:
                XPLMDestroyProbe(self.probe)
            except Exception:
                pass

    def XPluginEnable(self):
        return 1

    def XPluginDisable(self):
        try:
            self._tfc_release()
        except Exception:
            pass
        return None

    def XPluginReceiveMessage(self, inFromWho, inMessage, inParam):
        try:
            self._traffic_message(inMessage)
        except Exception as exc:
            self.log.xplane(f"ToLiss EFB: traffic message error: {exc}")
        return None

    def _read_datarefs(self, names):
        request_queue, reply_queue = queue.Queue(maxsize=1), queue.Queue(maxsize=1)
        request_queue.put((list(names or []), reply_queue))
        self.process_dref_read_queue(request_queue, None)
        return reply_queue.get_nowait()

    def _run_feature_request(self, request_queue, processor, payload):
        reply = queue.Queue(maxsize=1)
        value = dict(payload or {})
        value["response"] = reply
        request_queue.put(value)
        processor()
        return reply.get_nowait()

    def _dispatch_bridge(self, operation, payload):
        """Only flightLoopCallback invokes this method."""
        if operation == "read_datarefs":
            return self._read_datarefs(payload)
        if operation == "situation":
            return self._run_feature_request(self._situation_requests, self._process_situation_requests, payload)
        if operation == "failure":
            return self._run_feature_request(self._failure_requests, self._process_fault_requests, payload)
        if operation == "payload_apply":
            return self._run_feature_request(self._payload_requests, self._process_payload_requests, payload)
        if operation == "instructor":
            reply = queue.Queue(maxsize=1)
            self._instructor_requests.put((dict(payload or {}), reply))
            self._instr_tick()
            return reply.get_nowait()
        if operation == "replay":
            return self._replay_request(dict(payload or {}))
        if operation == "traffic":
            return self._traffic_request(dict(payload or {}))
        if operation == "mcdu_screen":
            return self._mcdu_screen(2 if int(payload or 1) == 2 else 1)
        if operation == "mcdu_key":
            self._mcdu_keys.put(payload)
            return True
        if operation == "aircraft_path":
            _, aircraft_path = XPLMGetNthAircraftModel(0)
            if aircraft_path and not os.path.isabs(aircraft_path):
                aircraft_path = os.path.join(self.xp_path, aircraft_path)
            return aircraft_path or ""
        if operation == "command":
            command = XPLMFindCommand(str(payload))
            if command:
                XPLMCommandBegin(command)
                self.active_commands.append(command)
            return bool(command)
        if operation == "write_dataref":
            self._apply_dataref_write(*payload)
            return True
        if operation == "teleport":
            self.start_teleport(payload)
            return True
        if operation == "load_file":
            self._load_legacy_situation(str(payload))
            return True
        raise ValueError(f"unknown sim bridge operation: {operation}")

    def _load_legacy_situation(self, filename):
        output = os.path.join(self.xp_path, "Output", "situations")
        os.makedirs(output, exist_ok=True)
        source_dat = os.path.join(self.sit_dir_toliss, f"{filename}.dat")
        if not os.path.isfile(source_dat):
            raise FileNotFoundError(source_dat)
        target_sit = os.path.join(output, "ToLissWebTemp.sit")
        shutil.copy2(source_dat, target_sit)
        shutil.copy2(source_dat, os.path.join(output, "ToLissWebTemp.dat"))
        for suffix in (".qps", "_pilotitems.dat", "_pilotitems.qps"):
            source = os.path.join(self.sit_dir_toliss, filename + suffix)
            if os.path.isfile(source):
                shutil.copy2(source, os.path.join(output, "ToLissWebTemp" + suffix))
        XPLMLoadSituation(target_sit.encode("utf-8"))

    def _apply_dataref_write(self, raw_name, value):
        if raw_name == "toliss_web/set_fuel":
            requested = max(0.0, float(value))
            maximum = self._get_fuel_capacity_kg()
            self._set_block_fuel_kg(min(requested, maximum) if maximum > 0 else requested)
            return
        if raw_name == "toliss_web/set_time":
            self._set_num("sim/time/use_system_time", 0)
            self._set_num("sim/time/zulu_time_sec", float(value))
            return
        if raw_name == "toliss_web/weather_live/on":
            self._set_num("sim/weather/region/change_mode", 7)
            return
        if raw_name == "toliss_web/weather_live/off":
            self._set_num("sim/weather/region/change_mode", 3)
            return
        if raw_name == "toliss_web/weather_preset/":
            self._set_num("sim/weather/region/change_mode", 3)
            self._set_num("sim/weather/region/weather_preset", int(value))
            self._set_num("sim/weather/region/update_immediately", 1)
            return
        match = re.match(r"(.+)\[(\d+)\]$", raw_name)
        if match:
            dataref, index = XPLMFindDataRef(match.group(1)), int(match.group(2))
            if dataref:
                types = XPLMGetDataRefTypes(dataref)
                if types & xplmType_FloatArray:
                    XPLMSetDatavf(dataref, [float(value)], index, 1)
                elif types & xplmType_IntArray:
                    XPLMSetDatavi(dataref, [int(value)], index, 1)
            return
        self._set_num(raw_name, value)

    def flightLoopCallback(self, elapsedMe, elapsedSim, counter, refcon):
        try:
            for command in self.active_commands:
                XPLMCommandEnd(command)
            self.active_commands.clear()
            self.bridge.process_pending(self._dispatch_bridge)
            self.process_teleport()
            self._process_mcdu()
            self._instr_tick()
            try:
                self._replay_tick()
            except Exception as exc:
                self.log.xplane(f"ToLiss EFB: replay error: {exc}")
            try:
                self._traffic_tick()
            except Exception as exc:
                self._tfc_release(f"Training traffic stopped: {exc}")
                self.log.xplane(f"ToLiss EFB: traffic error: {exc}")
            try:
                self._update_stream()
            except Exception as exc:
                self.log.xplane(f"ToLiss EFB: display stream error: {exc}")
            try:
                self.fdr.tick()
            except Exception as exc:
                self.log.xplane(f"ToLiss EFB: FDR error: {exc}")
        except Exception as exc:
            self.log.xplane(f"ToLiss EFB: flight loop error: {exc}")
        finally:
            self.log.drain_to_xplane(XPLMDebugString)
        return -1.0

    def start_server_thread(self):
        if self.stop_event.is_set():
            return
        self.server_thread = threading.Thread(target=self.run_server, name="ToLissEFB-HTTP", daemon=True)
        self.server_thread.start()

    def run_server(self):
        start_port = int(self.config.get("port", 8080))
        for offset in range(50):
            if self.stop_event.is_set():
                return
            port = start_port + offset
            try:
                self.httpd = ThreadingHTTPServer(("", port), self.handler_class)
                self.config["port"] = port
                self.save_config(self.config)
                self.log.xplane(f"ToLiss EFB successfully bound to port {port}")
                self.httpd.serve_forever(poll_interval=0.25)
                return
            except OSError as exc:
                self.log.xplane(f"ToLiss EFB: port {port} unavailable ({exc})")
            except Exception as exc:
                self.log.xplane(f"ToLiss EFB: server error: {exc}")
                return
        self.log.xplane(f"ToLiss EFB: could not bind a port starting at {start_port}")

    def restart_server_async(self):
        self.log.xplane("ToLiss EFB: restarting server")
        old = self.httpd
        self.httpd = None
        if old:
            old.shutdown()
            old.server_close()
        self.start_server_thread()

