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


class _NullLogger:
    def xplane(self, message):
        return None

APPROACH_TYPES = {"I": "ILS", "L": "LOC", "B": "LOC BC", "R": "RNAV", "H": "RNP", "J": "GLS", "P": "GPS",
                  "D": "VOR/DME", "V": "VOR", "S": "VOR", "N": "NDB", "Q": "NDB/DME", "X": "LDA", "U": "SDF",
                  "G": "IGS", "T": "TACAN", "F": "FMS", "W": "MLS", "Y": "MLS", "M": "MLS"}
WPT_ROLE = {"A": "IAF", "B": "IF", "C": "IAF", "D": "IAF", "F": "FAF", "I": "FACF", "M": "MAP", "H": "HOLD"}


def nav_data_files(xp_path):
    """Navigation data in X-Plane's priority order: Custom Data (e.g. Navigraph) first, then the default data."""
    custom = os.path.join(xp_path, "Custom Data")
    default = os.path.join(xp_path, "Resources", "default data")
    def pick(name):
        return [p for p in (os.path.join(custom, name), os.path.join(default, name)) if os.path.isfile(p)]
    cifp_dirs = [d for d in (os.path.join(custom, "CIFP"), os.path.join(default, "CIFP")) if os.path.isdir(d)]
    return pick("earth_fix.dat"), pick("earth_nav.dat"), cifp_dirs


def _num(v, scale=1.0):
    try:
        v = str(v).strip()
        return float(v) / scale if v else None
    except Exception:
        return None


def _alt(v):
    v = str(v).strip()
    if not v:
        return None
    if v.startswith("FL"):
        try:
            return int(v[2:]) * 100
        except Exception:
            return None
    try:
        return int(v)
    except Exception:
        return None


def _dms(v):
    """CIFP coordinates, e.g. N33562113 / E151105120 (degrees, minutes, seconds with hundredths)."""
    v = str(v).strip()
    try:
        hemi, body = v[0], v[1:]
        if hemi in "NS":
            d, m, sec = int(body[0:2]), int(body[2:4]), int(body[4:]) / 100.0
        else:
            d, m, sec = int(body[0:3]), int(body[3:5]), int(body[5:]) / 100.0
        val = d + m / 60.0 + sec / 3600.0
        return -val if hemi in "SW" else val
    except Exception:
        return None


class NavDatabase:
    CACHE_VERSION = 2      # 2: adds localisers by runway

    def __init__(self, xp_path, cache_dir=None, logger=None, stop_event=None):
        self.xp_path = xp_path
        self.cache_dir = cache_dir
        self.state = "idle"    # idle / loading / ready / error
        self.load_seconds = None
        self.fixes = None      # ident -> [(region, airport, lat, lon)]
        self.navaids = None    # ident -> [(region, airport, lat, lon, code, freq)]
        self.locs = None       # (airport, ident) -> (freq_mhz, true_bearing)
        self.locs_rwy = None   # (airport, runway) -> (ident, freq_mhz, true_bearing)
        self.lock = threading.Lock()
        self.cache = {}
        self.source = ""
        self.logger = logger or _NullLogger()
        self.stop_event = stop_event
        self.thread = None

    def _signature(self, files):
        sig = []
        for p in files:
            try:
                st = os.stat(p)
                sig.append([p, st.st_size, int(st.st_mtime)])
            except Exception:
                sig.append([p, 0, 0])
        return sig

    def _cache_path(self):
        return os.path.join(self.cache_dir, "navdata_cache.pkl") if self.cache_dir else None

    def load_async(self):
        if self.state in ("loading", "ready"):
            return
        self.state = "loading"
        self.thread = threading.Thread(target=self.load, name="ToLissEFB-NavData", daemon=True)
        self.thread.start()

    def load(self):
        with self.lock:
            if self.fixes is not None:
                self.state = "ready"
                return
            self.state = "loading"
            t0 = time.time()
            fix_files, nav_files, cifp_dirs = nav_data_files(self.xp_path)
            self.source = "Custom Data" if any("Custom Data" in f for f in fix_files + cifp_dirs) else "X-Plane default"
            signature = [self.CACHE_VERSION] + self._signature(fix_files + nav_files)
            cache_file = self._cache_path()
            # 1. Reuse the saved index when the navigation data has not changed
            if cache_file and os.path.isfile(cache_file):
                try:
                    with open(cache_file, 'rb') as f:
                        cached = pickle.load(f)
                    if cached.get("signature") == signature:
                        self.fixes, self.navaids, self.locs = cached["fixes"], cached["navaids"], cached["locs"]
                        self.locs_rwy = cached.get("locs_rwy", {})
                        self.state = "ready"
                        self.load_seconds = round(time.time() - t0, 2)
                        self.logger.xplane(f"ToLiss EFB: Navigation index loaded from cache in {self.load_seconds}s\n")
                        return
                except Exception as e:
                    self.logger.xplane(f"ToLiss EFB: Navigation cache unreadable, rebuilding ({e})\n")
            fixes, navaids, locs, locs_rwy = {}, {}, {}, {}
            for path in reversed(fix_files):        # later (higher priority) files overwrite earlier ones
                seen = set()
                try:
                    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                        for line in f:
                            parts = line.split(None, 5)
                            if len(parts) < 5 or parts[0][-1:].isalpha():
                                continue
                            try:
                                lat, lon = float(parts[0]), float(parts[1])
                            except ValueError:
                                continue
                            ident = parts[2]
                            lst = fixes.get(ident)
                            if lst is None or ident not in seen:
                                lst = fixes[ident] = []
                                seen.add(ident)
                            lst.append((parts[4], parts[3], lat, lon))
                except Exception as e:
                    self.logger.xplane(f"ToLiss EFB: earth_fix read error: {e}\n")
            for path in reversed(nav_files):
                seen = set()
                try:
                    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                        for line in f:
                            parts = line.split()
                            if len(parts) < 10:
                                continue
                            try:
                                code = int(parts[0])
                                lat, lon = float(parts[1]), float(parts[2])
                            except ValueError:
                                continue
                            ident, apt, region = parts[7], parts[8], parts[9]
                            freq = parts[4]
                            if code in (4, 5):
                                try:
                                    bearing = float(parts[6]) % 360.0
                                except ValueError:
                                    bearing = None
                                f_mhz = int(freq) / 100.0 if freq.isdigit() else None
                                locs[(apt, ident)] = (f_mhz, bearing)
                                if len(parts) > 10:          # the runway the localiser serves, e.g. 14 or 34L
                                    locs_rwy[(apt, parts[10].upper())] = (ident, f_mhz, bearing)
                            elif code in (2, 3, 12, 13):
                                key = ident
                                if (key, "n") not in seen:
                                    navaids[key] = []
                                    seen.add((key, "n"))
                                navaids[key].append((region, apt, lat, lon, code, freq))
                except Exception as e:
                    self.logger.xplane(f"ToLiss EFB: earth_nav read error: {e}\n")
            self.fixes, self.navaids, self.locs, self.locs_rwy = fixes, navaids, locs, locs_rwy
            self.state = "ready"
            self.load_seconds = round(time.time() - t0, 2)
            self.logger.xplane(f"ToLiss EFB: Navigation index built in {self.load_seconds}s\n")
            if cache_file:
                try:
                    tmp = cache_file + ".tmp"
                    with open(tmp, 'wb') as f:
                        pickle.dump({"signature": signature, "fixes": fixes, "navaids": navaids, "locs": locs, "locs_rwy": locs_rwy}, f, protocol=pickle.HIGHEST_PROTOCOL)
                    os.replace(tmp, cache_file)
                except Exception as e:
                    self.logger.xplane(f"ToLiss EFB: Could not save navigation cache: {e}\n")

    def _lookup(self, ident, region, section, subsection, airport, runways):
        ident = ident.strip()
        if not ident:
            return None
        if section == "P" and subsection == "G" or ident.startswith("RW") and ident in runways:
            r = runways.get(ident)
            return (r["lat"], r["lon"]) if r else None
        def best(cands, want_nav):
            if not cands:
                return None
            scored = []
            for c in cands:
                score = 0
                if c[0] == region:
                    score += 2
                if c[1] == airport:
                    score += 3
                elif c[1] == "ENRT":
                    score += 1
                scored.append((score, c))
            scored.sort(key=lambda x: -x[0])
            return (scored[0][1][2], scored[0][1][3])
        is_nav = section == "D" or (section == "P" and subsection == "N")
        first, second = (self.navaids, self.fixes) if is_nav else (self.fixes, self.navaids)
        return best(first.get(ident), is_nav) or best(second.get(ident), not is_nav)

    def procedures(self, icao):
        icao = icao.upper()
        if icao in self.cache:
            return self.cache[icao]
        if self.fixes is None:
            self.load_async()
            return {"status": "loading", "icao": icao}
        _, _, cifp_dirs = nav_data_files(self.xp_path)
        path = next((os.path.join(d, icao + ".dat") for d in cifp_dirs if os.path.isfile(os.path.join(d, icao + ".dat"))), None)
        if not path:
            return None
        runways, rows = {}, []
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                line = line.rstrip("\n")
                if line.startswith("RWY:"):
                    groups = line[4:].rstrip(";").split(";")
                    a = groups[0].split(",")
                    b = groups[1].split(",") if len(groups) > 1 else []
                    ident = a[0].strip()
                    rw = {"loc": a[5].strip() if len(a) > 5 else "", "tch": _num(a[7]) if len(a) > 7 else None,
                          "elev": _num(a[3]) if len(a) > 3 else None,
                          "lat": _dms(b[0]) if b else None, "lon": _dms(b[1]) if len(b) > 1 else None,
                          "disp": _num(b[2]) if len(b) > 2 else None}
                    if rw["lat"] is not None and rw["lon"] is not None:
                        runways[ident] = rw
                elif line.startswith("APPCH:"):
                    rows.append(line[6:].rstrip(";").split(","))

        procs = {}
        for r in rows:
            r = r + [""] * (40 - len(r))
            proc, rtype, trans = r[2].strip(), r[1].strip(), r[3].strip()
            p = procs.setdefault(proc, {"id": proc, "final_type": None, "legs": {}})
            if rtype != "A" and rtype != "Z":
                p["final_type"] = p["final_type"] or rtype
            key = trans if rtype == "A" else ("__missed__" if rtype == "Z" else "__final__")
            fix = r[4].strip()
            desc = r[8]
            role = WPT_ROLE.get(desc[3:4], "") if len(desc) >= 4 else ""
            if not role and r[11].strip() == "IF":
                role = "IF"
            pos = self._lookup(fix, r[5].strip(), r[6].strip(), r[7].strip(), icao, runways) if fix else None
            centre = None
            if r[30].strip():
                centre = self._lookup(r[30].strip(), r[31].strip(), r[32].strip(), r[33].strip(), icao, runways)
            if r[11].strip() == "AF" and r[13].strip():
                centre = self._lookup(r[13].strip(), r[14].strip(), "D", "", icao, runways)
            alt_desc = r[22].strip()
            leg = {"seq": int(r[0]) if r[0].strip().isdigit() else 0, "fix": fix, "role": role, "pt": r[11].strip(),
                   "nav": r[13].strip(),
                   "turn": r[9].strip(), "course": _num(r[20], 10.0), "dist": _num(r[21], 10.0),
                   "alt_desc": alt_desc, "alt1": _alt(r[23]), "alt2": _alt(r[24]),
                   "speed": _alt(r[27]), "vangle": _num(r[28], 100.0), "radius": _num(r[17], 1000.0),
                   "lat": pos[0] if pos else None, "lon": pos[1] if pos else None,
                   "clat": centre[0] if centre else None, "clon": centre[1] if centre else None}
            p["legs"].setdefault(key, []).append(leg)

        out = []
        for pid, p in procs.items():
            final = sorted(p["legs"].get("__final__", []), key=lambda l: l["seq"])
            missed = sorted(p["legs"].get("__missed__", []), key=lambda l: l["seq"])
            # Legs after the missed approach point belong to the missed approach
            for i, l in enumerate(final):
                if l["role"] == "MAP":
                    missed = final[i + 1:] + missed
                    final = final[:i + 1]
                    break
            m = re.match(r"^([A-Z])(\d{2}[LRCBT]?)-?([A-Z])?$", pid)
            letter = (p["final_type"] or pid[0])
            tname = APPROACH_TYPES.get(letter, APPROACH_TYPES.get(pid[0], pid[0]))
            if m:
                rwy, suffix = m.group(2), m.group(3) or ""
                name = f"{tname}{' ' + suffix if suffix else ''} {rwy}"
            else:
                rwy, suffix = "", ""
                name = f"{tname} {pid[1:].lstrip('-')}".strip()
            rw = runways.get("RW" + rwy) if rwy else None
            loc = None
            if tname in ("ILS", "LOC", "LOC BC", "LDA", "SDF", "IGS"):
                # 1. the runway record's localiser ident, 2. the recommended navaid on the final legs, 3. by runway
                candidates = []
                if rw and rw.get("loc"):
                    candidates.append(rw["loc"])
                candidates += [l["nav"] for l in final if l.get("nav")]
                for ident in candidates:
                    li = self.locs.get((icao, ident))
                    if li:
                        loc = {"ident": ident, "freq": li[0], "bearing": li[1]}
                        break
                if not loc and rwy:
                    lr = (self.locs_rwy or {}).get((icao, rwy.upper()))
                    if lr:
                        loc = {"ident": lr[0], "freq": lr[1], "bearing": lr[2]}
            fin_course = next((l["course"] for l in reversed(final) if l["course"] is not None), None)
            gp = next((l["vangle"] for l in final if l["vangle"]), None)
            transitions = {k: sorted(v, key=lambda l: l["seq"]) for k, v in p["legs"].items() if k not in ("__final__", "__missed__")}
            out.append({"id": pid, "name": name, "type": tname, "runway": rwy, "suffix": suffix,
                        "final": final, "missed": missed, "transitions": transitions,
                        "loc": loc, "course": fin_course, "glidepath": abs(gp) if gp else None,
                        "threshold": ({"lat": rw["lat"], "lon": rw["lon"], "elev": rw.get("elev"), "tch": rw.get("tch")} if rw else None)})
        order = {"ILS": 0, "GLS": 1, "RNP": 2, "RNAV": 3, "GPS": 4, "LOC": 5}
        out.sort(key=lambda a: (a["runway"] or "zz", order.get(a["type"], 9), a["suffix"]))
        result = {"status": "success", "icao": icao, "source": self.source,
                  "file": os.path.relpath(path, self.xp_path), "approaches": out}
        if len(self.cache) > 8:
            self.cache.pop(next(iter(self.cache)))
        self.cache[icao] = result
        return result
