"""The weather strip under the top bar: the airport that matters now, its METAR / ATIS / TAF in short, the wind
components for the runway in use, and alerts only when something matters.

Which airport: on the ground, where the aircraft is (the OFP origin or destination if it is one of them); in flight,
the departure airport while still within 40 nm of it (climbing out), then the destination. The page can ask for a
particular airport instead (the strip's tap switches between departure and destination).

Sources, all already used elsewhere in the EFB: METAR from VATSIM (else the OFP's), TAF from aviationweather.gov
(else the OFP's), ATIS from the VATSIM data feed, runways from the airport's apt.dat. Network fetches run in the
background; a request always answers at once from what is cached.
"""
from __future__ import annotations

import math
import re
import threading
import time

from .ops import parse_runway_ends, _ops_g

METAR_TTL, TAF_TTL = 120.0, 1800.0
XW_AMBER, XW_RED = 20.0, 33.0          # kt crosswind (with gusts)
TW_AMBER, TW_RED = 5.0, 10.0           # kt tailwind (with gusts)
CLIMB_OUT_NM, ARRIVAL_NM = 40.0, 150.0
RED_WX = {"TS": "THUNDERSTORM", "WS": "WIND SHEAR", "GR": "HAIL", "FC": "FUNNEL CLOUD", "VA": "VOLCANIC ASH",
          "SS": "SANDSTORM", "DS": "DUSTSTORM"}
AMBER_WX = {"CB": "CB", "TCU": "TCU", "FZ": "FREEZING", "SN": "SNOW", "SG": "SNOW GRAINS", "PL": "ICE PELLETS",
            "GS": "SMALL HAIL", "SQ": "SQUALLS", "+RA": "HEAVY RAIN", "+SHRA": "HEAVY SHOWERS"}


# ---------------- METAR parsing ----------------
def parse_metar(metar: str) -> dict:
    """Wind, visibility (m), ceiling (ft), QNH, temperature and weather groups from a METAR."""
    m = " " + (metar or "").upper().split(" RMK ")[0].split(" TEMPO ")[0].split(" BECMG ")[0] + " "
    out = {"wind_dir": None, "wind_spd": None, "gust": None, "vrb": False, "vis_m": None, "ceiling_ft": None,
           "qnh": None, "qnh_unit": None, "temp": None, "dew": None, "wx": [], "cavok": " CAVOK " in m}
    w = re.search(r"\s(\d{3}|VRB)(\d{2,3})(?:G(\d{2,3}))?(KT|MPS)\s", m)
    if w:
        k = 1.94384 if w.group(4) == "MPS" else 1.0
        out["vrb"] = w.group(1) == "VRB"
        out["wind_dir"] = None if out["vrb"] else int(w.group(1))
        out["wind_spd"] = round(int(w.group(2)) * k)
        out["gust"] = round(int(w.group(3)) * k) if w.group(3) else None
    if out["cavok"]:
        out["vis_m"] = 10000
    else:
        v = re.search(r"\s(\d{4})(?:NDV)?\s", m)
        if v:
            out["vis_m"] = int(v.group(1))
        else:
            sm = re.search(r"\s(M|P)?(?:(\d+)\s)?(?:(\d+)/(\d+)|(\d+))SM\s", m)
            if sm:
                miles = float(sm.group(5)) if sm.group(5) else (float(sm.group(2) or 0) + float(sm.group(3)) / float(sm.group(4)))
                out["vis_m"] = int(round(min(miles, 10) * 1609.34))
    cig = [int(h) * 100 for kind, h in re.findall(r"\s(BKN|OVC|VV)(\d{3})", m)]
    out["ceiling_ft"] = min(cig) if cig else None
    q = re.search(r"\sQ(\d{4})\s", m)
    a = re.search(r"\sA(\d{4})\s", m)
    if q:
        out["qnh"], out["qnh_unit"] = int(q.group(1)), "hPa"
    elif a:
        out["qnh"], out["qnh_unit"] = int(a.group(1)) / 100.0, "inHg"
    t = re.search(r"\s(M?\d{2})/(M?\d{2})?\s", m)
    if t:
        conv = lambda s: -int(s[1:]) if s.startswith("M") else int(s)
        out["temp"] = conv(t.group(1))
        out["dew"] = conv(t.group(2)) if t.group(2) else None
    groups = re.findall(r"\s([+-]?(?:VC)?(?:MI|BC|PR|DR|BL|SH|TS|FZ)?(?:DZ|RA|SN|SG|IC|PL|GR|GS|UP|BR|FG|FU|VA|DU|SA|HZ|PY|PO|SQ|FC|SS|DS)+|TS|VCTS)(?=\s)", m)
    out["wx"] = groups
    out["cb"] = bool(re.search(r"\s(?:FEW|SCT|BKN|OVC)\d{3}CB\s", m))
    out["tcu"] = bool(re.search(r"\s(?:FEW|SCT|BKN|OVC)\d{3}TCU\s", m))
    out["ws"] = bool(re.search(r"\sWS\s", m))
    return out


def flight_category(vis_m, ceiling_ft):
    """VMC / MVFR / IFR / LIFR from visibility and ceiling (FAA bands)."""
    v = vis_m if vis_m is not None else 10000
    c = ceiling_ft if ceiling_ft is not None else 99999
    if c < 500 or v < 1600:
        return "LIFR"
    if c < 1000 or v < 4800:
        return "IFR"
    if c <= 3000 or v <= 8000:
        return "MVFR"
    return "VMC"


def _atis_runways(text: str, arrival: bool):
    """Runways the ATIS names for arrivals (or departures): those in a clause that says so, else every runway named."""
    up = (text or "").upper()
    key = r"\b(?:ARR\w*|LDG|LANDING|LAND|APCH|APPROACH\w*|ILS|RNP|RNAV|VOR|EXPECT)\b" if arrival else r"\b(?:DEP\w*|TKOF|TAKE ?OFF\w*)\b"
    rw = []
    for clause in re.split(r"[.,;]", up):
        if re.search(key, clause):
            rw += re.findall(r"(?:RWY|RUNWAY)S?\s*(?:IN USE\s*:?\s*)?(\d{2}[LRC]?)", clause)
            rw += re.findall(r"(?:RWY|RUNWAY)S?\s*\d{2}[LRC]?\s*(?:AND|/)\s*(\d{2}[LRC]?)", clause)
    if not rw:
        rw = re.findall(r"(?:RWY|RUNWAY)S?\s*(?:IN USE\s*:?\s*)?(\d{2}[LRC]?)", up)
    return list(dict.fromkeys(rw))


def _dist_nm(a, b):
    if not a or not b:
        return None
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 3440.065 * math.asin(min(1.0, math.sqrt(h)))


class WxBar:
    def __init__(self, plugin):
        self.plugin = plugin
        self.lock = threading.Lock()
        self.cache = {}            # icao -> {"metar", "metar_t", "taf", "taf_t", "busy"}
        self._coords = None

    # ---------------- data ----------------
    def _coord(self, icao):
        if self._coords is None or len(self._coords) != len(getattr(self.plugin, "apt_coords", []) or []):
            self._coords = {c[2]: (c[0], c[1]) for c in (getattr(self.plugin, "apt_coords", []) or [])}
        return self._coords.get(icao)

    def _refresh(self, icao):
        ops = getattr(self.plugin, "ops", None)
        e = {}
        try:
            now = time.time()
            with self.lock:
                e = dict(self.cache.get(icao) or {})
            if ops is not None and now - e.get("metar_t", 0) > METAR_TTL:
                m = ops._metar(icao)
                e["metar"], e["metar_t"] = (m or e.get("metar")), now
            if ops is not None and now - e.get("taf_t", 0) > TAF_TTL:
                t = ops._taf(icao)
                e["taf"], e["taf_t"] = (t or e.get("taf")), now if t else now - TAF_TTL + 300
        except Exception:
            pass
        finally:
            e["busy"] = False
            with self.lock:
                self.cache[icao] = e

    def _weather(self, icao):
        """Cached METAR/TAF for an airport; starts a background refresh when they are stale."""
        now = time.time()
        with self.lock:
            e = self.cache.setdefault(icao, {})
            stale = now - e.get("metar_t", 0) > METAR_TTL or now - e.get("taf_t", 0) > TAF_TTL
            if stale and not e.get("busy"):
                e["busy"] = True
                threading.Thread(target=self._refresh, args=(icao,), daemon=True, name="EFB-wx").start()
            return e.get("metar"), e.get("taf"), e.get("metar_t", 0)

    # ---------------- the strip ----------------
    def status(self, want_icao=None):
        p = self.plugin
        if not getattr(p, "db_ready", False):
            return {"status": "error", "message": "The airport index is still loading."}
        v = p.bridge.call("read_datarefs", ["sim/flightmodel/position/latitude", "sim/flightmodel/position/longitude",
                                            "sim/flightmodel/failures/onground_any", "sim/weather/region/change_mode"], timeout=1.5)
        lat = float(v.get("sim/flightmodel/position/latitude") or 0)
        lon = float(v.get("sim/flightmodel/position/longitude") or 0)
        on_ground = float(v.get("sim/flightmodel/failures/onground_any") or 0) >= 1
        sim_real_wx = int(float(v.get("sim/weather/region/change_mode") or 0)) == 7
        ops = getattr(p, "ops", None)
        o = (ops.ofp if ops else None) or {}
        orig = str(_ops_g(o, "origin", "icao_code") or "").upper()
        dest = str(_ops_g(o, "destination", "icao_code") or "").upper()
        here = (lat, lon) if (lat or lon) else None
        near = p.find_nearest_airport(lat, lon, 8000.0) if here else None
        d_orig = _dist_nm(here, self._coord(orig)) if orig else None
        d_dest = _dist_nm(here, self._coord(dest)) if dest else None
        # the airport that matters now
        if on_ground:
            auto = near or (orig if orig else None)
        elif orig and d_orig is not None and d_orig < CLIMB_OUT_NM and not (d_dest is not None and d_dest < ARRIVAL_NM):
            auto = orig
        else:
            auto = dest or None
        icao = (want_icao or "").upper().strip() or auto
        if not icao or not re.fullmatch(r"[A-Z0-9]{3,4}", icao):
            return {"status": "success", "icao": None, "orig": orig, "dest": dest}
        role = "dep" if icao == orig else ("arr" if icao == dest else "here")
        arrival = role == "arr" or (role == "here" and not on_ground)
        metar, taf, metar_t = self._weather(icao)
        if not metar:
            metar = _ops_g(o, "weather", "orig_metar" if role == "dep" else "dest_metar") if role in ("dep", "arr") else None
            metar = metar if isinstance(metar, str) and metar.strip() else None
        if not taf and role in ("dep", "arr"):
            t = _ops_g(o, "weather", "orig_taf" if role == "dep" else "dest_taf")
            taf = t if isinstance(t, str) and t.strip() else None
        letter, atis_text = (None, None)
        if ops is not None:
            try:
                letter, atis_text = ops._atis(icao, arrival=arrival)
            except Exception:
                letter, atis_text = None, None
        mt = parse_metar(metar) if metar else None
        # the runway: the ATIS's (the planned one if the ATIS names it too), else the OFP's, else the most into wind
        planned = str(_ops_g(o, "origin" if role == "dep" else "destination", "plan_rwy") or "").upper() if role in ("dep", "arr") else ""
        atis_rwys = _atis_runways(atis_text, arrival) if atis_text else []
        ends = []
        loc = getattr(p, "apt_index", {}).get(icao)
        if loc:
            try:
                ends = parse_runway_ends(loc[0], loc[1])
            except Exception:
                ends = []
        names = {n.upper(): h for n, h in ends}
        rwy, rwy_src = None, None
        if atis_rwys:
            rwy = planned if planned in atis_rwys else atis_rwys[0]
            rwy_src = "atis"
        elif planned:
            rwy, rwy_src = planned, "ofp"
        elif mt and mt["wind_dir"] is not None and (mt["wind_spd"] or 0) >= 3 and ends:
            rwy = max(ends, key=lambda e: math.cos(math.radians(mt["wind_dir"] - e[1])))[0].upper()
            rwy_src = "wind"
        comp = None
        if rwy and mt and rwy.upper() in names and mt["wind_spd"] is not None:
            hdg = names[rwy.upper()]
            if mt["vrb"] or mt["wind_dir"] is None:
                comp = {"vrb": True, "spd": mt["wind_spd"], "gust": mt["gust"]}
            else:
                a = math.radians(mt["wind_dir"] - hdg)
                comp = {"vrb": False, "hw": round(mt["wind_spd"] * math.cos(a)), "xw": round(abs(mt["wind_spd"] * math.sin(a))),
                        "xw_side": "L" if math.sin(a) < 0 else "R"}
                if mt["gust"]:
                    comp["hw_g"] = round(mt["gust"] * math.cos(a))
                    comp["xw_g"] = round(abs(mt["gust"] * math.sin(a)))
        # alerts: only what matters
        alerts = []
        if mt:
            for code, txt in RED_WX.items():
                if any(code in g for g in mt["wx"]) or (code == "WS" and mt["ws"]):
                    alerts.append({"level": "red", "text": txt})
            for code, txt in AMBER_WX.items():
                if (code == "CB" and mt["cb"]) or (code == "TCU" and mt["tcu"]) or \
                        (code not in ("CB", "TCU") and any(g == code or (code[0] != "+" and code in g) for g in mt["wx"])):
                    if not any(a["text"] == txt for a in alerts):
                        alerts.append({"level": "amber", "text": txt})
            if (mt["vis_m"] is not None and mt["vis_m"] < 550) or (mt["ceiling_ft"] is not None and mt["ceiling_ft"] < 200):
                alerts.append({"level": "red", "text": "BELOW CAT I"})
        if comp and not comp["vrb"]:
            xw = max(comp["xw"], comp.get("xw_g") or 0)
            tw = -min(comp["hw"], comp.get("hw_g", comp["hw"]))
            if xw >= XW_RED:
                alerts.append({"level": "red", "text": f"XWIND {xw} KT"})
            elif xw >= XW_AMBER:
                alerts.append({"level": "amber", "text": f"XWIND {xw} KT"})
            if tw > TW_RED:
                alerts.append({"level": "red", "text": f"TAILWIND {tw} KT"})
            elif tw > TW_AMBER:
                alerts.append({"level": "amber", "text": f"TAILWIND {tw} KT"})
        if planned and atis_rwys and planned not in atis_rwys:
            alerts.append({"level": "amber", "text": f"ATIS RWY {'/'.join(atis_rwys)}, PLANNED {planned}"})
        return {"status": "success", "icao": icao, "role": role, "auto": auto, "orig": orig, "dest": dest,
                "metar": metar, "metar_age_s": int(time.time() - metar_t) if metar_t else None, "taf": taf,
                "atis": {"letter": letter, "text": atis_text, "runways": atis_rwys} if (letter or atis_text) else None,
                "runway": rwy, "runway_src": rwy_src, "planned_rwy": planned or None,
                "wx": mt, "category": flight_category(mt["vis_m"], mt["ceiling_ft"]) if mt else None,
                "components": comp, "alerts": alerts, "sim_manual_wx": not sim_real_wx}
