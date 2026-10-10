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

def sanitize_data(data):
    if isinstance(data, dict):
        return {k: sanitize_data(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [sanitize_data(v) for v in data]
    elif isinstance(data, float):
        if math.isnan(data) or math.isinf(data):
            return 0.0
        return data
    return data

# ===== Airport scenery helpers (ground map) =====
def get_apt_search_paths(xp_path):
    """apt.dat files in X-Plane priority order: scenery_packs.ini order, then global/default airports."""
    paths = []
    def add(p):
        if p and os.path.isfile(p) and p not in paths:
            paths.append(p)
    def_apt1 = os.path.join(xp_path, "Global Scenery", "Global Airports", "Earth nav data", "apt.dat")
    def_apt2 = os.path.join(xp_path, "Resources", "default scenery", "default apt dat", "Earth nav data", "apt.dat")
    cs_dir = os.path.join(xp_path, "Custom Scenery")
    ini = os.path.join(cs_dir, "scenery_packs.ini")
    used_ini = False
    if os.path.isfile(ini):
        try:
            with open(ini, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    line = line.strip()
                    if not line.startswith('SCENERY_PACK ') or line.startswith('SCENERY_PACK_DISABLED'):
                        continue
                    used_ini = True
                    pack = line[len('SCENERY_PACK '):].strip()
                    if pack == '*GLOBAL_AIRPORTS*':
                        add(def_apt1)
                        continue
                    pack_dir = pack if os.path.isabs(pack) else os.path.join(xp_path, pack)
                    add(os.path.join(pack_dir, "Earth nav data", "apt.dat"))
        except Exception:
            pass
    if not used_ini and os.path.exists(cs_dir):
        try:
            for item in sorted(os.listdir(cs_dir)):
                add(os.path.join(cs_dir, item, "Earth nav data", "apt.dat"))
        except Exception:
            pass
    add(def_apt1)
    add(def_apt2)
    return paths


_GM_LINE_CLASS = {}
for _t in (1, 2, 7, 8, 9, 10, 11, 12, 13, 14, 51, 52, 57, 58, 59, 60, 61, 62, 63, 64):
    _GM_LINE_CLASS[_t] = 'cl'
for _t in (3, 53):
    _GM_LINE_CLASS[_t] = 'edge'
for _t in (4, 5, 6, 54, 55, 56):
    _GM_LINE_CLASS[_t] = 'hold'
for _t in (20, 21, 22, 23, 24):
    _GM_LINE_CLASS[_t] = 'white'


def _gm_parse_node(parts):
    """Returns (lat, lon, ctrl_lat, ctrl_lon or None, line_type)."""
    code = parts[0]
    lat, lon = float(parts[1]), float(parts[2])
    if code in ('112', '114', '116'):
        clat, clon = float(parts[3]), float(parts[4])
        lt = int(parts[5]) if len(parts) > 5 and parts[5].lstrip('-').isdigit() else 0
        return (lat, lon, clat, clon, lt)
    lt = int(parts[3]) if len(parts) > 3 and parts[3].lstrip('-').isdigit() else 0
    return (lat, lon, None, None, lt)


def _gm_segment(a, b, steps=8):
    """Points after a, up to and including b. apt.dat: a node's control point shapes the curve
    leaving it; the curve arriving at a node uses the mirrored control point."""
    p0 = (a[0], a[1]); p3 = (b[0], b[1])
    c1 = (a[2], a[3]) if a[2] is not None else None
    c2 = (2 * b[0] - b[2], 2 * b[1] - b[3]) if b[2] is not None else None
    if c1 is None and c2 is None:
        return [p3]
    pts = []
    for i in range(1, steps + 1):
        t = i / steps
        u = 1 - t
        if c1 is not None and c2 is not None:
            x = u*u*u*p0[0] + 3*u*u*t*c1[0] + 3*u*t*t*c2[0] + t*t*t*p3[0]
            y = u*u*u*p0[1] + 3*u*u*t*c1[1] + 3*u*t*t*c2[1] + t*t*t*p3[1]
        else:
            c = c1 if c1 is not None else c2
            x = u*u*p0[0] + 2*u*t*c[0] + t*t*p3[0]
            y = u*u*p0[1] + 2*u*t*c[1] + t*t*p3[1]
        pts.append((x, y))
    return pts


def _gm_r(pt):
    return [round(pt[0], 6), round(pt[1], 6)]


def _gm_ring(nodes):
    if len(nodes) < 3:
        return None
    out = [(nodes[0][0], nodes[0][1])]
    n = len(nodes)
    for i in range(n):
        out.extend(_gm_segment(nodes[i], nodes[(i + 1) % n]))
    return [_gm_r(p) for p in out]


def _gm_lines(nodes, closed):
    """Split a linear feature into polylines by marking class (line type of each segment's start node)."""
    result = []
    if len(nodes) < 2:
        return result
    n = len(nodes)
    seg_count = n if closed else n - 1
    cur_cls, cur_pts = None, []
    for i in range(seg_count):
        a, b = nodes[i], nodes[(i + 1) % n]
        cls = _GM_LINE_CLASS.get(a[4])
        pts = _gm_segment(a, b)
        if cls is None:
            if cur_cls and len(cur_pts) > 1:
                result.append({"k": cur_cls, "c": [_gm_r(p) for p in cur_pts]})
            cur_cls, cur_pts = None, []
            continue
        if cls != cur_cls:
            if cur_cls and len(cur_pts) > 1:
                result.append({"k": cur_cls, "c": [_gm_r(p) for p in cur_pts]})
            cur_cls, cur_pts = cls, [(a[0], a[1])]
        cur_pts.extend(pts)
    if cur_cls and len(cur_pts) > 1:
        result.append({"k": cur_cls, "c": [_gm_r(p) for p in cur_pts]})
    return result


def _gm_dist_m(lat1, lon1, lat2, lon2):
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    y = math.radians(lat2 - lat1)
    return math.hypot(x, y) * 6371000.0


# ATC frequencies in apt.dat: 50-56 (old, in 10 kHz) and 1050-1056 (8.33 kHz era, in kHz)
APT_FREQ_TYPES = {0: "ATIS", 1: "CTAF", 2: "DEL", 3: "GND", 4: "TWR", 5: "APP", 6: "DEP"}
APT_FREQ_ORDER = ["ATIS", "DEL", "GND", "TWR", "DEP", "APP", "CTAF"]


def parse_airport_freqs(path, pos, max_lines=400000):
    """The airport's radio stations (real-world frequencies from the scenery): [{type, name, khz, mhz}]."""
    found = {}
    with open(path, "rb") as f:
        f.seek(pos)
        f.readline()                                   # the airport's own header line
        for n, raw in enumerate(f):
            if n > max_lines:
                break
            parts = raw.split(None, 2)
            if not parts:
                continue
            code = parts[0]
            if code in (b"1", b"16", b"17", b"99"):    # the next airport (or the end of the file)
                break
            if not code.isdigit():
                continue
            c = int(code)
            if 50 <= c <= 56 or 1050 <= c <= 1056:
                try:
                    v = int(parts[1])
                except (IndexError, ValueError):
                    continue
                khz = v * 10 if c < 1000 else v
                if not 108000 <= khz <= 137000:
                    continue
                typ = APT_FREQ_TYPES[c % 50 if c < 1000 else c - 1050]
                name = parts[2].decode("utf-8", errors="ignore").strip() if len(parts) > 2 else typ
                key = (c >= 1000, typ, khz)
                found.setdefault(key, {"type": typ, "name": name, "khz": khz, "mhz": f"{khz / 1000:.3f}"})
    # an airport with the newer 8.33 kHz rows lists every station there: the older rows are then duplicates
    new = [v for k, v in found.items() if k[0]]
    out = new if new else list(found.values())
    out.sort(key=lambda x: (APT_FREQ_ORDER.index(x["type"]), x["name"], x["khz"]))
    return out


def parse_ground_map(path, pos):
    """Parse one airport block of an apt.dat starting at byte offset pos."""
    data = {"pavement": [], "lines": [], "runways": [], "stands": [], "labels": [], "boundary": [], "taxi": []}
    feat_kind, feat_nodes, feat_rings = None, [], []
    taxi_nodes, taxi_edges, runway_edges = {}, [], []
    have_1300 = False
    legacy_stands = []

    def finish():
        nonlocal feat_kind, feat_nodes, feat_rings
        if feat_kind == 'pav':
            if feat_nodes:
                feat_rings.append(feat_nodes)
            rings = [r for r in (_gm_ring(x) for x in feat_rings) if r]
            if rings:
                data["pavement"].append(rings)
        elif feat_kind == 'line' and len(feat_nodes) > 1:
            data["lines"].extend(_gm_lines(feat_nodes, False))
        feat_kind, feat_nodes, feat_rings = None, [], []

    with open(path, 'rb') as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            mm.seek(pos)
            header = mm.readline().decode('utf-8', errors='ignore').split(maxsplit=5)
            data["icao"] = header[4].upper() if len(header) > 4 else ""
            data["name"] = header[5].strip() if len(header) > 5 else data["icao"]
            try: data["elev_ft"] = float(header[1])
            except Exception: data["elev_ft"] = 0.0
            while True:
                raw = mm.readline()
                if not raw:
                    break
                parts = raw.decode('utf-8', errors='ignore').split()
                if not parts:
                    continue
                code = parts[0]
                if code in ('1', '16', '17', '99'):
                    break
                try:
                    if code in ('111', '112', '113', '114', '115', '116'):
                        if feat_kind is None:
                            continue
                        feat_nodes.append(_gm_parse_node(parts))
                        if code in ('113', '114'):
                            if feat_kind == 'pav':
                                feat_rings.append(feat_nodes)
                                feat_nodes = []
                            elif feat_kind == 'line':
                                data["lines"].extend(_gm_lines(feat_nodes, True))
                                feat_nodes = []
                            elif feat_kind == 'bnd':
                                ring = _gm_ring(feat_nodes)
                                if ring and not data["boundary"]:
                                    data["boundary"].append(ring)
                                feat_nodes = []
                            else:
                                feat_nodes = []
                        elif code in ('115', '116'):
                            if feat_kind == 'line':
                                data["lines"].extend(_gm_lines(feat_nodes, False))
                            feat_nodes = []
                        continue
                    finish()
                    if code == '110':
                        feat_kind = 'pav'
                    elif code == '120':
                        feat_kind = 'line'
                    elif code == '130':
                        feat_kind = 'bnd'
                    elif code == '100' and len(parts) >= 20:
                        data["runways"].append({
                            "w": float(parts[1]), "s": int(float(parts[2])),
                            "n1": parts[8], "lat1": float(parts[9]), "lon1": float(parts[10]), "d1": float(parts[11]),
                            "n2": parts[17], "lat2": float(parts[18]), "lon2": float(parts[19]), "d2": float(parts[20])
                        })
                    elif code == '1300' and len(parts) >= 4:
                        have_1300 = True
                        name = " ".join(parts[6:]) if len(parts) >= 7 else ""
                        data["stands"].append({"n": name, "lat": float(parts[1]), "lon": float(parts[2]), "h": float(parts[3])})
                    elif code == '15' and len(parts) >= 4:
                        legacy_stands.append({"n": " ".join(parts[4:]), "lat": float(parts[1]), "lon": float(parts[2]), "h": float(parts[3])})
                    elif code == '1201' and len(parts) >= 5:
                        taxi_nodes[parts[4]] = (float(parts[1]), float(parts[2]))
                    elif code == '1202' and len(parts) >= 6:
                        edge_name = " ".join(parts[5:]).strip()
                        if parts[4].lower().startswith('runway'):
                            runway_edges.append((parts[1], parts[2], edge_name))
                        else:
                            taxi_edges.append((parts[1], parts[2], edge_name))
                except Exception:
                    continue
            finish()
        finally:
            mm.close()

    if not have_1300:
        data["stands"] = legacy_stands

    # Taxi network segments (for "nearest taxiway"): [lat1, lon1, lat2, lon2, name, is_runway]
    for edges, is_rwy in ((taxi_edges, 0), (runway_edges, 1)):
        for n1, n2, name in edges:
            if name and n1 in taxi_nodes and n2 in taxi_nodes:
                a, b = taxi_nodes[n1], taxi_nodes[n2]
                data["taxi"].append([round(a[0], 6), round(a[1], 6), round(b[0], 6), round(b[1], 6), name, is_rwy])

    # Taxiway labels: midpoints of the longest edges of each named taxiway, spaced apart
    by_name = {}
    for n1, n2, name in taxi_edges:
        if not name or n1 not in taxi_nodes or n2 not in taxi_nodes:
            continue
        a, b = taxi_nodes[n1], taxi_nodes[n2]
        by_name.setdefault(name, []).append(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, _gm_dist_m(a[0], a[1], b[0], b[1])))
    for name, segs in by_name.items():
        segs.sort(key=lambda x: -x[2])
        placed = []
        for lat, lon, _ in segs:
            if len(placed) >= 8:
                break
            if all(_gm_dist_m(lat, lon, p[0], p[1]) > 300 for p in placed):
                placed.append((lat, lon))
                data["labels"].append({"n": name, "lat": round(lat, 6), "lon": round(lon, 6)})
    return data

class AirportMixin:
    _APT_CACHE_VERSION = 2
    _APT_HEADER_RE = re.compile(
        rb"(?m)^(1|16|17)\s+\S+\s+\S+\s+\S+\s+(\S+)\s+([^\r\n]+)"
    )

    def _apt_cache_file(self):
        base = getattr(self, 'plugin_dir', None)
        if not base:
            base = os.path.join(self.xp_path, "Resources", "plugins", "PythonPlugins", "ToLissWebTablet")
        return os.path.join(base, 'apt_index_cache.json')
    def _apt_signature(self, search_paths):
        sig = []
        for p in search_paths:
            try:
                st = os.stat(p)
                sig.append([p, st.st_size, int(st.st_mtime)])
            except Exception:
                sig.append([p, 0, 0])
        return sig
    def build_apt_db(self):
        t_start = time.time()
        search_paths = get_apt_search_paths(self.xp_path)
        signature = self._apt_signature(search_paths)

        # 1. Reuse the saved index if no scenery has changed since it was written
        try:
            with open(self._apt_cache_file(), 'r', encoding='utf-8') as f:
                cache = json.load(f)
            if cache.get('version') == self._APT_CACHE_VERSION and cache.get('signature') == signature:
                paths = cache['paths']
                self.apt_lite_db = [{"icao": a[0], "name": a[1]} for a in cache['airports']]
                self.apt_index = {a[0]: (paths[a[2]], a[3]) for a in cache['airports']}
                self.apt_coords = [(c[0], c[1], c[2]) for c in cache['coords']]
                self.db_ready = True
                self.log.xplane(f"ToLiss EFB: Airport index loaded from cache ({len(self.apt_lite_db)} airports, {time.time() - t_start:.1f}s)")
                return
        except Exception:
            pass

        # 2. Otherwise scan the apt.dat files (regex runs at C speed over the memory-mapped file)
        airports = []      # [icao, name, path_idx, offset]
        apt_coords = []
        seen = set()
        for path_idx, p in enumerate(search_paths):
            try:
                with open(p, 'rb') as f:
                    mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
                    try:
                        headers = [(m.start(), m.group(1), m.group(2), m.group(3)) for m in self._APT_HEADER_RE.finditer(mm)]
                        for i, (pos, code, icao_b, name_b) in enumerate(headers):
                            icao = icao_b.decode('utf-8', errors='ignore').upper()
                            if icao in seen:
                                continue
                            seen.add(icao)
                            name = name_b.decode('utf-8', errors='ignore').strip() or "Airport"
                            airports.append([icao, name, path_idx, pos])
                            if code == b'1':
                                end = headers[i + 1][0] if i + 1 < len(headers) else len(mm)
                                rpos = mm.find(b'\n100 ', pos, end)
                                if rpos >= 0:
                                    line_end = mm.find(b'\n', rpos + 1, end)
                                    parts = mm[rpos + 1:line_end if line_end > 0 else end].split()
                                    try:
                                        lat = (float(parts[9]) + float(parts[18])) / 2.0
                                        lon = (float(parts[10]) + float(parts[19])) / 2.0
                                        apt_coords.append((round(lat, 6), round(lon, 6), icao))
                                    except Exception:
                                        pass
                    finally:
                        mm.close()
            except Exception:
                pass

        self.apt_lite_db = [{"icao": a[0], "name": a[1]} for a in airports]
        self.apt_index = {a[0]: (search_paths[a[2]], a[3]) for a in airports}
        self.apt_coords = apt_coords
        self.db_ready = True
        self.log.xplane(f"ToLiss EFB: Airport index built ({len(airports)} airports, {time.time() - t_start:.1f}s)")

        try:
            tmp = self._apt_cache_file() + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump({"version": self._APT_CACHE_VERSION, "signature": signature, "paths": search_paths,
                           "airports": airports, "coords": [list(c) for c in apt_coords]}, f, separators=(',', ':'))
            os.replace(tmp, self._apt_cache_file())
        except Exception as e:
            self.log.xplane(f"ToLiss EFB: Could not save airport index cache: {e}")
    def find_nearest_airport(self, lat, lon, max_m=8000.0):
        best, best_d = None, max_m
        coslat = math.cos(math.radians(lat))
        dlat_max = max_m / 111000.0
        for alat, alon, icao in getattr(self, 'apt_coords', []):
            if abs(alat - lat) > dlat_max:
                continue
            d = math.hypot((alon - lon) * coslat, alat - lat) * 111195.0
            if d < best_d:
                best, best_d = icao, d
        return best
    def get_airport_freqs(self, icao):
        icao = str(icao or "").upper().strip()
        cache = self.__dict__.setdefault("_freq_cache", {})
        if icao in cache:
            return cache[icao]
        loc = getattr(self, 'apt_index', {}).get(icao)
        if not loc:
            return None
        name = next((a["name"] for a in getattr(self, 'apt_lite_db', []) if a["icao"] == icao), icao)
        res = {"icao": icao, "name": name, "freqs": parse_airport_freqs(loc[0], loc[1])}
        if len(cache) >= 30:
            cache.pop(next(iter(cache)))
        cache[icao] = res
        return res
    def get_ground_map(self, icao):
        cache = self.ground_map_cache
        if icao in cache:
            return cache[icao]
        loc = getattr(self, 'apt_index', {}).get(icao)
        if not loc:
            return None
        data = parse_ground_map(loc[0], loc[1])
        payload = json.dumps({"status": "success", "airport": data}, separators=(',', ':')).encode('utf-8')
        if len(cache) >= 6:
            cache.pop(next(iter(cache)))
        cache[icao] = payload
        return payload
