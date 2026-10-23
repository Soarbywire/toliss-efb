"""Taxi routing: a route along the airport's taxi network from the aircraft to a stand or a runway holding point.

The network is the scenery's ATC taxi routing data (apt.dat 1201 nodes, 1202 segments with their names, size limits
and one-way rules, 1204 runway protection zones). Routes use only segments wide enough for the aircraft, respect
one-way segments, avoid sharp turns and U-turns, keep off runways except to cross them, and can be made to follow an
ATC clearance ("B B4 A"). The page follows the aircraft along the route and gives the instructions.
Everything here is plain Python (no X-Plane calls), so it runs on the web server's threads.
"""
from __future__ import annotations

import heapq
import math
import re

from .airports import label_duplicate_stands

SIZE_ORDER = "ABCDEF"
TURN_FORBID = 150.0          # no U-turns on the network
TURN_SHARP = 100.0
RUNWAY_FACTOR = 4.0          # taxiing along a runway costs four times as much (and a fixed 150 m): only if no other way


def _xy(lat, lon, lat0, lon0):
    return ((lon - lon0) * 111320.0 * math.cos(math.radians(lat0)), (lat - lat0) * 110540.0)


def _bearing(a, b):
    return (math.degrees(math.atan2(b[0] - a[0], b[1] - a[1])) + 360.0) % 360.0


def _turn(b1, b2):
    """Signed turn from bearing b1 to b2 (deg, + right)."""
    return ((b2 - b1 + 540.0) % 360.0) - 180.0


class TaxiNetwork:
    def __init__(self):
        self.nodes = {}          # id -> (lat, lon)
        self.xy = {}             # id -> (x, y) metres
        self.edges = []          # dicts: a, b, oneway, runway, size, name, zones {rwy,...}, len
        self.adj = {}            # node -> [(edge index, other node)]
        self.runways = []        # {n1, lat1, lon1, n2, lat2, lon2}
        self.stands = []         # {name, lat, lon, hdg}
        self.names = set()
        self.lat0 = self.lon0 = 0.0

    @property
    def usable(self):
        return len(self.edges) >= 3


def parse_taxi_network(path, pos):
    net = TaxiNetwork()
    raw_nodes, raw_edges, last = {}, [], None
    with open(path, "rb") as f:
        f.seek(pos)
        f.readline()
        for raw in f:
            parts = raw.decode("utf-8", errors="ignore").split()
            if not parts:
                continue
            code = parts[0]
            if code in ("1", "16", "17", "99"):
                break
            try:
                if code == "1201" and len(parts) >= 5:
                    raw_nodes[parts[4]] = (float(parts[1]), float(parts[2]))
                elif code == "1202" and len(parts) >= 5:
                    kind = parts[4].lower()
                    m = re.match(r"taxiway_([a-f])", kind)
                    last = {"a": parts[1], "b": parts[2], "oneway": parts[3].lower() == "oneway",
                            "runway": kind.startswith("runway"), "size": m.group(1).upper() if m else "",
                            "name": " ".join(parts[5:]).strip(), "zones": set()}
                    raw_edges.append(last)
                elif code == "1204" and last is not None and len(parts) >= 3:
                    if parts[1].lower() in ("departure", "arrival", "ils"):
                        last["zones"].update(x.strip().upper() for x in parts[2].split(",") if x.strip())
                elif code == "100" and len(parts) >= 20:
                    net.runways.append({"n1": parts[8].upper(), "lat1": float(parts[9]), "lon1": float(parts[10]),
                                        "n2": parts[17].upper(), "lat2": float(parts[18]), "lon2": float(parts[19])})
                elif code == "1300" and len(parts) >= 4:
                    net.stands.append({"name": " ".join(parts[6:]) or "STAND", "lat": float(parts[1]), "lon": float(parts[2]),
                                       "hdg": float(parts[3]), "size": ""})
                elif code == "1301" and len(parts) >= 2 and net.stands:
                    net.stands[-1]["size"] = parts[1].upper()
            except (ValueError, IndexError):
                continue
    label_duplicate_stands(net.stands)
    if raw_nodes:
        lats = [p[0] for p in raw_nodes.values()]
        lons = [p[1] for p in raw_nodes.values()]
        net.lat0, net.lon0 = (min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2
    net.nodes = raw_nodes
    net.xy = {k: _xy(v[0], v[1], net.lat0, net.lon0) for k, v in raw_nodes.items()}
    for e in raw_edges:
        if e["a"] not in raw_nodes or e["b"] not in raw_nodes or e["a"] == e["b"]:
            continue
        a, b = net.xy[e["a"]], net.xy[e["b"]]
        e["len"] = math.hypot(b[0] - a[0], b[1] - a[1])
        i = len(net.edges)
        net.edges.append(e)
        net.adj.setdefault(e["a"], []).append((i, e["b"]))
        net.adj.setdefault(e["b"], []).append((i, e["a"]))
        e["keys"] = name_keys(e["name"]) if not e["runway"] else set()
        net.names.update(e["keys"])
    return net


PHONETIC = {"ALPHA": "A", "ALFA": "A", "BRAVO": "B", "CHARLIE": "C", "DELTA": "D", "ECHO": "E", "FOXTROT": "F", "GOLF": "G",
            "HOTEL": "H", "INDIA": "I", "JULIET": "J", "JULIETT": "J", "KILO": "K", "LIMA": "L", "MIKE": "M", "NOVEMBER": "N",
            "OSCAR": "O", "PAPA": "P", "QUEBEC": "Q", "ROMEO": "R", "SIERRA": "S", "TANGO": "T", "UNIFORM": "U",
            "VICTOR": "V", "WHISKEY": "W", "WHISKY": "W", "XRAY": "X", "X-RAY": "X", "YANKEE": "Y", "ZULU": "Z"}


def name_keys(name):
    """The names a taxiway segment answers to. Sceneries label them in many ways ("C", "C_1", "TWY C", "C (north)",
    "C/C1", "Charlie"); ATC says "C". A trailing number glued to the letter ("C1") is a different taxiway."""
    up = str(name or "").upper().strip()
    if not up:
        return set()
    keys = {up}
    for t in re.split(r"[\s/,;()_\-.]+", up):
        if not t or t in ("TWY", "TAXIWAY", "TWYS", "TAXILANE", "TL", "NORTH", "SOUTH", "EAST", "WEST") and t != up:
            continue
        if not t.isdigit():
            keys.add(PHONETIC.get(t, t))
    m = re.match(r"^(?:TWY|TAXIWAY)\s*([A-Z]{1,2}\d{0,2})\b", up)
    if m:
        keys.add(m.group(1))
    return keys


def runway_ident_match(edge_name, ident):
    return ident.upper() in [x.strip().upper() for x in re.split(r"[/\s]+", edge_name or "") if x.strip()]


def parse_clearance(text, net):
    """'TAXI VIA B B4 A HOLD SHORT 16R' -> ['B', 'B4', 'A']; unknown taxiways are reported."""
    words = [w for w in re.split(r"[\s,;]+", str(text or "").upper()) if w]
    out, unknown, i = [], [], 0
    skip = {"TAXI", "VIA", "TO", "THEN", "AND", "ON", "CROSS", "CONTINUE", "STAND", "GATE", "BAY", "RUNWAY", "RWY",
            "HOLDING", "POINT", "TWY", "TAXIWAY"}
    while i < len(words):
        w = words[i]
        if w == "HOLD" and i + 1 < len(words) and words[i + 1] == "SHORT":
            i += 2
            while i < len(words) and (words[i] in ("RWY", "RUNWAY", "OF") or re.match(r"^\d{1,2}[LRC]?$", words[i])):
                i += 1
            continue
        w = PHONETIC.get(w, w)
        if w in skip or re.match(r"^\d{1,2}[LRC]?$", w) and w not in net.names:
            i += 1
            continue
        if w in net.names:
            if not out or out[-1] != w:
                out.append(w)
        else:
            unknown.append(w)
        i += 1
    return out, unknown


def plan_route(net, start, hdg, dest, ac_size="C", clearance=None):
    """start (lat, lon); dest {"type": "stand", "lat", "lon", "hdg", "name"} or {"type": "runway", "ident"}.
    Returns a dict: ok, message, points [[lat, lon, dist]], steps, summary, length_m, dest."""
    if not net.usable:
        return {"ok": False, "message": "This airport's scenery has no taxi routing network, so routing isn't available here."}
    seq = [s.upper() for s in (clearance or [])]
    sx, sy = _xy(start[0], start[1], net.lat0, net.lon0)
    size_i = SIZE_ORDER.index(ac_size) if ac_size in SIZE_ORDER else 2

    def allowed(e):
        return not e["size"] or SIZE_ORDER.index(e["size"]) >= size_i

    # where the route may begin: network points near the aircraft, preferring those ahead of the nose
    near = sorted(((math.hypot(p[0] - sx, p[1] - sy), n) for n, p in net.xy.items() if n in net.adj), key=lambda t: t[0])[:8]
    if not near or near[0][0] > 400:
        return {"ok": False, "message": "The aircraft is not near this airport's taxi network."}
    starts = []
    for d, n in near:
        if d > max(150.0, near[0][0] + 60):
            continue
        p = net.xy[n]
        behind = d > 8 and abs(_turn(hdg, _bearing((sx, sy), p))) > 100
        starts.append((d + (120.0 if behind else 0.0), n))

    # where it ends
    goals = {}                       # node -> extra cost
    end_xy, dest_label, runway_ident = None, "", None
    if dest["type"] == "stand":
        st_xy = _xy(dest["lat"], dest["lon"], net.lat0, net.lon0)
        h = math.radians(dest.get("hdg", 0.0))
        lead = (st_xy[0] + 35 * math.sin(h), st_xy[1] + 35 * math.cos(h))      # the lead-in line, in front of the nose
        cands = sorted(((math.hypot(p[0] - lead[0], p[1] - lead[1]), n) for n, p in net.xy.items() if n in net.adj))
        close = [c for c in cands if c[0] <= max(120.0, cands[0][0] + 40.0)][:4] if cands else []
        for d, n in close:
            goals[n] = d * 3.0
        end_xy, dest_label = st_xy, f"Stand {dest.get('name', '')}".strip()
    else:
        runway_ident = str(dest.get("ident", "")).upper()
        rw = next((r for r in net.runways if runway_ident in (r["n1"], r["n2"])), None)
        if not rw:
            return {"ok": False, "message": f"Runway {runway_ident} was not found at this airport."}
        thr = (rw["lat1"], rw["lon1"]) if rw["n1"] == runway_ident else (rw["lat2"], rw["lon2"])
        txy = _xy(thr[0], thr[1], net.lat0, net.lon0)
        rnodes = {n for e in net.edges if e["runway"] and runway_ident_match(e["name"], runway_ident) for n in (e["a"], e["b"])}
        if not rnodes:   # some sceneries don't name runway segments: any runway node near this threshold
            rnodes = {n for e in net.edges if e["runway"] for n in (e["a"], e["b"])}
        cands = sorted((math.hypot(net.xy[n][0] - txy[0], net.xy[n][1] - txy[1]), n) for n in rnodes)
        cands = [c for c in cands if c[0] <= cands[0][0] + 250.0][:3] if cands else []
        if not cands or cands[0][0] > 800:
            return {"ok": False, "message": f"No taxi route reaches runway {runway_ident} in this scenery."}
        for d, n in cands:
            goals[n] = d
        dest_label = f"Holding point RWY {runway_ident}"

    # A* over (node, previous node, clearance progress)
    gx = [net.xy[n] for n in goals]

    def h_est(n):
        p = net.xy[n]
        return min(math.hypot(p[0] - g[0], p[1] - g[1]) for g in gx)

    def advance(k, keys):
        """Clearance progress after using a segment answering to these names (None = not allowed)."""
        if not seq:
            return k
        if k < len(seq) and seq[k] in keys:
            return k + 1
        if k > 0 and seq[k - 1] in keys:
            return k
        if not keys:
            return k
        if k == 0 or k == len(seq):          # leaving the stand area, or after the last cleared taxiway
            return k
        return None

    openq, best, came = [], {}, {}
    tick = [0]

    def push(f, g, st):
        tick[0] += 1
        heapq.heappush(openq, (f, g, tick[0], st))
    start_b = {}                                 # direction the aircraft travels to reach each start point
    for c, n in starts:
        st = (n, None, 0)
        best[st] = c
        p = net.xy[n]
        start_b[n] = _bearing((sx, sy), p) if math.hypot(p[0] - sx, p[1] - sy) > 5 else hdg
        push(c + h_est(n), c, st)
    found = None
    while openq:
        f, g, _, st = heapq.heappop(openq)
        if g > best.get(st, 1e18):
            continue
        if st[0] == "__END__":
            found = st[1]
            break
        n, prev, k = st
        if n in goals and (not seq or k == len(seq)):
            end = ("__END__", st)
            g_end = g + goals[n]
            if g_end < best.get(end, 1e18):
                best[end] = g_end
                push(g_end, g_end, end)
        b_in = _bearing(net.xy[prev], net.xy[n]) if prev else start_b.get(n)
        for ei, m in net.adj.get(n, []):
            e = net.edges[ei]
            if e["oneway"] and e["a"] != n:
                continue
            if not allowed(e) or m == prev:
                continue
            k2 = advance(k, e.get("keys", set()) if not e["runway"] else set())
            if k2 is None:
                continue
            b_out = _bearing(net.xy[n], net.xy[m])
            cost = e["len"]
            if b_in is not None:
                t = abs(_turn(b_in, b_out))
                if t > TURN_FORBID:
                    if prev is not None:
                        continue
                    cost += 150.0                # turning round right at the start (e.g. on a stand after pushback)
                cost += 300.0 if t > TURN_SHARP else (25.0 if t > 60 else 0.0)
            if e["runway"]:
                cost = cost * RUNWAY_FACTOR + 150.0
            st2 = (m, n, k2)
            g2 = g + cost
            if g2 < best.get(st2, 1e18):
                best[st2] = g2
                came[st2] = st
                push(g2 + h_est(m), g2, st2)
    if not found:
        if seq:
            return {"ok": False, "message": "No legal route follows that clearance (" + " ".join(seq) + "). Check the taxiways, or clear the clearance to get the best route."}
        return {"ok": False, "message": "No taxi route was found to " + dest_label + " for this aircraft size."}

    # the node path
    path, st = [], found
    while st is not None:
        path.append(st[0])
        st = came.get(st)
    path.reverse()

    # the segments used, and the cut at the holding point for a departure
    def edge_between(a, b):
        for ei, m in net.adj.get(a, []):
            if m == b:
                return net.edges[ei]
        return None

    segs = [edge_between(path[i], path[i + 1]) for i in range(len(path) - 1)]
    if runway_ident:
        cut = None
        for i, e in enumerate(segs):
            if e and ((e["runway"] and runway_ident_match(e["name"], runway_ident)) or runway_ident in e["zones"]):
                cut = i
                break
        if cut is not None:
            path, segs = path[:cut + 1], segs[:cut]

    # points with distance along the route
    pts, dist = [], 0.0
    xy_path = [(sx, sy)] + [net.xy[n] for n in path]
    ll_path = [tuple(start)] + [net.nodes[n] for n in path]
    if end_xy is not None:
        xy_path.append(end_xy)
        ll_path.append((dest["lat"], dest["lon"]))
    for i, (la, lo) in enumerate(ll_path):
        if i:
            dist += math.hypot(xy_path[i][0] - xy_path[i - 1][0], xy_path[i][1] - xy_path[i - 1][1])
        pts.append([round(la, 7), round(lo, 7), round(dist, 1)])

    # instructions: taxiway changes with left / right, runway holds, the destination
    steps, names = [], []
    seg_names = [("" if (e is None or e["runway"]) else e["name"].upper()) for e in segs]
    cur = None
    for i, e in enumerate(segs):
        pi = i + 1                                   # point index of the segment start (pts[0] is the aircraft)
        nm = seg_names[i]
        if nm and nm != cur:
            j = max(0, pi - 1)
            b1 = _bearing(xy_path[j], xy_path[pi]) if pi > j else hdg
            b2 = _bearing(xy_path[pi], xy_path[pi + 1]) if pi + 1 < len(xy_path) else b1
            t = _turn(b1, b2)
            way = "Continue on" if abs(t) < 25 else ("Right onto" if t > 0 else "Left onto")
            if cur is None:
                way = "Taxi via"
            steps.append({"i": pi, "d": pts[pi][2], "kind": "turn", "text": f"{way} {nm}", "turn": round(t)})
            names.append(nm)
            cur = nm
        # entering a runway's protected area or the runway itself: hold short
        zones = set(e["zones"]) if e else set()
        prev_zones = set(segs[i - 1]["zones"]) if i and segs[i - 1] else set()
        rwy_name = e["name"].upper() if e and e["runway"] else ""
        if (zones - prev_zones) or (rwy_name and not (i and segs[i - 1] and segs[i - 1]["runway"])):
            label = "/".join(sorted(zones - prev_zones)) if (zones - prev_zones) else rwy_name
            steps.append({"i": pi, "d": pts[pi][2], "kind": "hold", "text": f"HOLD SHORT RWY {label}"})
    steps.append({"i": len(pts) - 1, "d": pts[-1][2], "kind": "dest", "text": dest_label})
    # sharp turns along the way (for the speed cue)
    sharp = []
    for i in range(1, len(xy_path) - 1):
        a, b, c = xy_path[i - 1], xy_path[i], xy_path[i + 1]
        if math.hypot(b[0] - a[0], b[1] - a[1]) < 1 or math.hypot(c[0] - b[0], c[1] - b[1]) < 1:
            continue
        t = _turn(_bearing(a, b), _bearing(b, c))
        if abs(t) >= 60:
            sharp.append({"i": i, "d": pts[i][2], "turn": round(t)})
    summary = " → ".join(names + [dest_label])
    return {"ok": True, "points": pts, "steps": steps, "sharp": sharp, "summary": summary,
            "length_m": round(dist), "dest": {"label": dest_label, "lat": pts[-1][0], "lon": pts[-1][1]},
            "clearance": seq}


# ---------------- the plugin side: which airport, which destination, the aircraft's position ----------------
def _natural(name):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", str(name))]


def taxi_network_for(plugin, icao):
    cache = plugin.__dict__.setdefault("_taxi_cache", {})
    if icao not in cache:
        loc = getattr(plugin, "apt_index", {}).get(icao)
        if not loc:
            return None
        if len(cache) >= 4:
            cache.pop(next(iter(cache)))
        cache[icao] = parse_taxi_network(loc[0], loc[1])
    return cache[icao]


def taxi_position(plugin):
    v = plugin.bridge.call("read_datarefs", ["sim/flightmodel/position/latitude", "sim/flightmodel/position/longitude",
                                             "sim/flightmodel/position/psi", "sim/flightmodel/position/groundspeed",
                                             "sim/flightmodel/failures/onground_any"], timeout=1.5)
    return (float(v.get("sim/flightmodel/position/latitude", 0) or 0), float(v.get("sim/flightmodel/position/longitude", 0) or 0),
            float(v.get("sim/flightmodel/position/psi", 0) or 0), float(v.get("sim/flightmodel/position/groundspeed", 0) or 0),
            float(v.get("sim/flightmodel/failures/onground_any", 0) or 0) >= 1)


def _ofp_info(plugin):
    ops = getattr(plugin, "ops", None)
    o = (ops.ofp if ops else None) or {}
    g = lambda *k: (lambda d: d if isinstance(d, str) else "")(_dig(o, *k))
    fl = (ops.flight if ops else None) or {}
    stand = (fl.get("stand") or {}).get("name") if isinstance(fl.get("stand"), dict) else ""
    ofp_id = str(_dig(o, "params", "request_id") or _dig(o, "params", "time_generated") or "")
    chosen = fl.get("chosen_stand") if ops and fl.get("ofp_id") else getattr(plugin, "_taxi_chosen", None)
    if not isinstance(chosen, dict) or (chosen.get("ofp_id") or "") != ofp_id:
        chosen = None
    return {"origin": g("origin", "icao_code").upper(), "dest": g("destination", "icao_code").upper(),
            "dep_rwy": g("origin", "plan_rwy").upper(), "arr_rwy": g("destination", "plan_rwy").upper(),
            "type": g("aircraft", "icaocode").upper(), "stand": stand or "", "ofp_id": ofp_id,
            "chosen": chosen}


def _dig(d, *keys):
    for k in keys:
        if not isinstance(d, dict):
            return ""
        d = d.get(k, "")
    return d


def _atis_runways(plugin, icao):
    """Departure runways named in the airport's VATSIM ATIS (empty if there is none)."""
    ops = getattr(plugin, "ops", None)
    if not ops or not icao or not getattr(plugin, "vatsim", None):
        return None, []
    try:
        letter, text = ops._atis(icao, arrival=False)
    except Exception:
        return None, []
    if not text:
        return None, []
    up = text.upper()
    dep = re.findall(r"(?:RWY|RUNWAY)S?\s*(\d{2}[LRC]?)(?=[^.]*?(?:DEP|DEPARTURE))", up) or re.findall(r"(?:RWY|RUNWAY)S?\s*(\d{2}[LRC]?)", up)
    return letter, list(dict.fromkeys(dep))


def _pick_airport(plugin, lat, lon, on_ground, info):
    """The airport the panel is for: where the aircraft is on the ground; in flight, the OFP destination."""
    index = getattr(plugin, "apt_index", {})
    near = plugin.find_nearest_airport(lat, lon, 8000.0) if (lat or lon) else None
    if on_ground and near:
        return near, False
    if not on_ground and info["dest"] and info["dest"] in index:
        return info["dest"], near != info["dest"]
    if near:
        return near, False
    if info["dest"] and info["dest"] in index:
        return info["dest"], True
    return None, False


def taxi_options(plugin):
    """What the Taxi panel offers: the airport (where the aircraft is, or in flight the destination), its stands and
    runways, and a suggested destination with where the suggestion came from."""
    lat, lon, hdg, gs, on_ground = taxi_position(plugin)
    if not getattr(plugin, "db_ready", False):
        return {"status": "error", "message": "The airport index is still loading."}
    info = _ofp_info(plugin)
    icao, remote = _pick_airport(plugin, lat, lon, on_ground, info)
    if not icao:
        return {"status": "error", "message": "The aircraft is not at an airport in your scenery, and no SimBrief destination is loaded."}
    net = taxi_network_for(plugin, icao)
    if net is None:
        return {"status": "error", "message": f"{icao} is not in your scenery."}
    runways = sorted({r[k] for r in net.runways for k in ("n1", "n2")}, key=_natural)
    stands = sorted(({"name": s["name"], "lat": s["lat"], "lon": s["lon"]} for s in net.stands), key=lambda s: _natural(s["name"]))
    names = {s["name"] for s in stands}
    chosen = info["chosen"]["name"] if info["chosen"] and info["chosen"].get("icao") == icao else ""
    stand, stand_src, rwy, rwy_src, note = "", "", "", "", ""
    at_dest = bool(info["dest"]) and icao == info["dest"]
    at_origin = bool(info["origin"]) and icao == info["origin"] and not at_dest
    if at_dest:
        dtype = "stand"
        if info["stand"] in names:
            stand, stand_src = info["stand"], "ops"
        elif chosen in names:
            stand, stand_src = chosen, "chosen"
        if info["arr_rwy"] in runways:
            rwy, rwy_src = info["arr_rwy"], "ofp"
    elif at_origin:
        dtype = "runway"
        plan = info["dep_rwy"] if info["dep_rwy"] in runways else ""
        letter, atis = _atis_runways(plugin, icao)
        atis = [r for r in atis if r in runways]
        if atis and plan not in atis:
            rwy, rwy_src = atis[0], "atis"
            note = f"RWY {atis[0]} (ATIS{' ' + letter if letter else ''}); OFP planned {plan or info['dep_rwy'] or 'none'}"
        elif plan:
            rwy, rwy_src = plan, "ofp"
    else:
        dtype = "runway" if not on_ground else "stand"
    if not rwy:
        rwy = runways[0] if runways else ""
    return {"status": "success", "icao": icao, "network": net.usable, "runways": runways, "stands": stands,
            "suggest": {"type": dtype, "stand": stand, "runway": rwy, "stand_src": stand_src, "runway_src": rwy_src, "note": note},
            "assigned_stand": info["stand"], "chosen_stand": chosen, "on_ground": on_ground, "remote": remote,
            "origin": info["origin"], "dest": info["dest"], "ofp_id": info["ofp_id"], "at_origin": at_origin, "at_dest": at_dest}


def taxi_set_stand(plugin, data):
    """Remember the arrival stand the pilot chose for this flight (no Ops Centre needed). An empty name clears it."""
    info = _ofp_info(plugin)
    name = str(data.get("stand") or "").strip()
    icao = str(data.get("icao") or info["dest"] or "").upper().strip()
    if name:
        net = taxi_network_for(plugin, icao) if icao else None
        if net is None or not any(s["name"] == name for s in net.stands):
            return {"status": "error", "message": f"Stand {name} was not found at {icao or 'the destination'}."}
    val = {"icao": icao, "name": name, "ofp_id": info["ofp_id"]} if name else None
    ops = getattr(plugin, "ops", None)
    if ops and ops.flight.get("ofp_id"):
        with ops.lock:
            ops.flight["chosen_stand"] = val
        try:
            ops._save_flight()
        except Exception:
            pass
    else:
        plugin._taxi_chosen = val
    if not name:
        return {"status": "success", "chosen_stand": "", "message": "Arrival stand cleared."}
    return {"status": "success", "chosen_stand": name, "icao": icao,
            "message": f"Stand {name} at {icao} saved as your arrival stand for this flight."}


def taxi_route(plugin, data, size_codes):
    lat, lon, hdg, gs, on_ground = taxi_position(plugin)
    icao = plugin.find_nearest_airport(lat, lon, 8000.0) if (lat or lon) else None
    if not icao:
        return {"status": "error", "message": "The aircraft is not at an airport in your scenery."}
    net = taxi_network_for(plugin, icao)
    if net is None:
        return {"status": "error", "message": f"{icao} is not in your scenery."}
    info = _ofp_info(plugin)
    size = size_codes.get(info["type"], "C")
    if str(data.get("dest_type")) == "stand":
        name = str(data.get("stand") or "")
        st = next((s for s in net.stands if s["name"] == name), None)
        if not st:
            return {"status": "error", "message": f"Stand {name or '(none chosen)'} was not found at {icao}."}
        dest = {"type": "stand", "lat": st["lat"], "lon": st["lon"], "hdg": st["hdg"], "name": st["name"]}
    else:
        dest = {"type": "runway", "ident": str(data.get("runway") or "").upper()}
    seq, unknown = parse_clearance(data.get("clearance", ""), net)
    if unknown and data.get("strict"):
        return {"status": "error", "message": f"Not a taxiway at {icao}: {', '.join(unknown)}."}
    res = plan_route(net, (lat, lon), hdg, dest, size, seq)
    note = None
    if unknown:
        note = f"{', '.join(unknown)} {'is' if len(unknown) == 1 else 'are'} not named in {icao}'s taxi network in your scenery, so the route follows the rest of the clearance."
    if not res.get("ok") and seq:
        # the scenery's names don't allow that sequence: the best route instead, said plainly
        res = plan_route(net, (lat, lon), hdg, dest, size, [])
        if res.get("ok"):
            note = (note + " " if note else "") + f"No route in your scenery follows {' '.join(seq)} exactly: this is the best route to the same place."
    if not res.get("ok"):
        return {"status": "error", "message": res.get("message", "No route found.")}
    res.update(status="success", icao=icao, size=size)
    if note:
        res["note"] = note
    return res
