from __future__ import annotations

import copy
import json
import math
import threading
import time
import urllib.request

from ..core.net import urlopen as net_urlopen


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
                with net_urlopen(request, timeout=15) as response:
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
                    with net_urlopen(request, timeout=20) as response:
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


VATSIM_FACILITY = {1: "FSS", 2: "DEL", 3: "GND", 4: "TWR", 5: "APP", 6: "CTR"}


def _distance_nm(a, b, c, d):
    p1, p2 = math.radians(a), math.radians(c)
    dp, dl = math.radians(c - a), math.radians(d - b)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 3440.065 * 2 * math.atan2(math.sqrt(h), math.sqrt(max(0.0, 1 - h)))


def online_stations(snapshot: dict) -> list[dict]:
    """The VATSIM controllers and ATIS stations that can be tuned, with the frequency in kHz, the station type and
    where it transmits from (its transceivers; else the controller's visual centre). Observers, supervisors without a
    position and the 199.998 'no frequency' entries are left out."""
    data = snapshot.get("data") or {}
    tx = snapshot.get("transceivers") or {}
    out = []
    entries = [(c, False) for c in data.get("controllers", []) or []] + [(a, True) for a in data.get("atis", []) or []]
    for c, is_atis in entries:
        cs = str(c.get("callsign", "") or "")
        freq = str(c.get("frequency", "") or "")
        if not cs or cs.upper().endswith("_OBS") or freq.startswith("199.998"):
            continue
        if not is_atis and int(c.get("facility", 0) or 0) == 0:
            continue
        try:
            khz = int(round(float(freq) * 1000))
        except (TypeError, ValueError):
            continue
        if khz < 108000 or khz > 137000:
            continue
        positions = [(float(t[0]), float(t[1]), float(t[2] or 0.0)) for t in tx.get(cs, []) if t and len(t) >= 3]
        if not positions:
            vis = c.get("visual_position") or {}
            if "lat" in vis and "lon" in vis:
                positions = [(float(vis["lat"]), float(vis["lon"]), 0.0)]
        text = c.get("text_atis") or []
        out.append({"callsign": cs, "name": str(c.get("name", "") or ""), "frequency": f"{khz / 1000:.3f}", "khz": khz,
                    "type": "ATIS" if is_atis else VATSIM_FACILITY.get(int(c.get("facility", 0) or 0), ""),
                    "atis_code": str(c.get("atis_code") or ""),
                    "text": [str(t) for t in text][:12] if isinstance(text, list) else [], "positions": positions})
    return out


def nearby_atc(snapshot: dict, lat: float, lon: float, agl_ft: float,
               max_nm: float = 400.0, limit: int = 40) -> list[dict]:
    """Online ATC within max_nm, flagged in_range by VHF line-of-sight between the aircraft and the station's
    transmitters (the model Audio for VATSIM uses), stations in range first, then nearest first."""
    out = []
    for st in online_stations(snapshot):
        best, in_range = None, False
        for (tlat, tlon, tagl_m) in st["positions"]:
            d = _distance_nm(lat, lon, tlat, tlon)
            los = 1.23 * (math.sqrt(max(agl_ft, 10.0)) + math.sqrt(max(tagl_m * 3.28084, 10.0)))
            best = d if best is None or d < best else best
            in_range = in_range or d <= los
        if best is None or best > max_nm:
            continue
        item = {k: v for k, v in st.items() if k != "positions"}
        item.update(distance=round(best, 1), in_range=in_range)
        out.append(item)
    out.sort(key=lambda x: (not x["in_range"], x["distance"]))
    return out[:limit]


# how far from the airport a station on one of its frequencies may transmit and still be that airport's
_LOCAL_NM = {"ATIS": 25.0, "DEL": 25.0, "GND": 25.0, "TWR": 25.0, "APP": 80.0, "FSS": 400.0, "CTR": 400.0}


def airport_online(snapshot: dict, icao: str, apt_lat, apt_lon) -> dict:
    """{kHz: callsign} of VATSIM stations online on an airport's frequencies: the callsign names the airport
    (e.g. YSSY_TWR, or LAX_TWR for KLAX), or the station transmits close enough to it for its type."""
    icao = (icao or "").upper()
    prefixes = {icao} | ({icao[1:]} if len(icao) == 4 and icao[0] in "KC" else set())
    out = {}
    for st in online_stations(snapshot):
        cs = st["callsign"].upper()
        named = cs.split("_")[0] in prefixes
        near = False
        if apt_lat is not None and apt_lon is not None and st["positions"]:
            d = min(_distance_nm(apt_lat, apt_lon, p[0], p[1]) for p in st["positions"])
            near = d <= _LOCAL_NM.get(st["type"], 25.0)
        if named or near:
            out.setdefault(st["khz"], st["callsign"])
    return out
