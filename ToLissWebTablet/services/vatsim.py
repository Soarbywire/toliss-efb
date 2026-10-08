from __future__ import annotations

import copy
import json
import math
import threading
import time
import urllib.request


class VatsimCache:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data = None
        self._transceivers = {}
        self._last_fetch = 0.0

    def update_data(self, value) -> None:
        with self._lock:
            self._data = value
            self._last_fetch = time.time()

    def update_transceivers(self, value) -> None:
        with self._lock:
            self._transceivers = value

    def snapshot(self) -> dict:
        with self._lock:
            return {"data": copy.deepcopy(self._data),
                    "transceivers": copy.deepcopy(self._transceivers),
                    "last_fetch": self._last_fetch}


class VatsimService:
    def __init__(self, log, stop_event: threading.Event) -> None:
        self.log = log
        self.stop_event = stop_event
        self.cache = VatsimCache()
        self.thread = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._loop, name="ToLissEFB-VATSIM", daemon=True)
        self.thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout)

    def _loop(self) -> None:
        last_tx = 0.0
        while not self.stop_event.is_set():
            try:
                request = urllib.request.Request(
                    "https://data.vatsim.net/v3/vatsim-data.json",
                    headers={"User-Agent": "ToLissEFB/1.0"},
                )
                with urllib.request.urlopen(request, timeout=15) as response:
                    self.cache.update_data(json.loads(response.read().decode("utf-8")))
            except Exception as exc:
                self.log.xplane(f"ToLiss EFB: VATSIM fetch error: {exc}")
            if time.time() - last_tx > 55 and not self.stop_event.is_set():
                try:
                    data = self.cache.snapshot().get("data") or {}
                    callsigns = {x.get("callsign", "") for x in data.get("controllers", [])}
                    callsigns |= {x.get("callsign", "") for x in data.get("atis", [])}
                    request = urllib.request.Request(
                        "https://data.vatsim.net/v3/transceivers-data.json",
                        headers={"User-Agent": "ToLissEFB/1.0"},
                    )
                    with urllib.request.urlopen(request, timeout=20) as response:
                        raw = json.loads(response.read().decode("utf-8"))
                    transmitters = {}
                    for entry in raw:
                        callsign = entry.get("callsign", "")
                        if callsign in callsigns:
                            transmitters[callsign] = [
                                (t.get("latDeg", 0.0), t.get("lonDeg", 0.0),
                                 t.get("heightAglM", 0.0), t.get("frequency", 0))
                                for t in entry.get("transceivers", [])
                            ]
                    self.cache.update_transceivers(transmitters)
                    last_tx = time.time()
                except Exception as exc:
                    self.log.xplane(f"ToLiss EFB: VATSIM transceivers error: {exc}")
            self.stop_event.wait(30.0)


def nearby_atc(snapshot: dict, lat: float, lon: float, agl_ft: float,
               max_nm: float = 400.0, limit: int = 40) -> list[dict]:
    data = snapshot.get("data") or {}
    transmitters = snapshot.get("transceivers") or {}

    def distance_nm(a, b, c, d):
        p1, p2 = math.radians(a), math.radians(c)
        dp, dl = math.radians(c - a), math.radians(d - b)
        h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return 3440.065 * 2 * math.atan2(math.sqrt(h), math.sqrt(max(0.0, 1 - h)))

    result = []
    for controller in list(data.get("controllers", [])) + list(data.get("atis", [])):
        callsign = controller.get("callsign", "")
        positions = transmitters.get(callsign, [])
        if not positions:
            visual = controller.get("visual_position") or {}
            if "lat" in visual and "lon" in visual:
                positions = [(visual["lat"], visual["lon"], 0.0, controller.get("frequency", 0))]
        distances = [distance_nm(lat, lon, p[0], p[1]) for p in positions]
        if distances and min(distances) <= max_nm:
            item = copy.deepcopy(controller)
            item["distance"] = round(min(distances), 1)
            item["aircraft_agl_ft"] = agl_ft
            result.append(item)
    result.sort(key=lambda item: item["distance"])
    return result[:limit]

