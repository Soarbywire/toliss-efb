"""The weather strip under the top bar, and the EFB ATIS.

Which airport: on the ground, where the aircraft is (the OFP origin or destination if it is one of them); in flight,
the departure airport while still within 40 nm of it (climbing out), then the destination. The page can ask for a
particular airport instead (the strip's tap switches between departure and destination).

Which weather depends on X-Plane's weather mode:
- Real weather (X-Plane downloads real METARs): the METAR is the source for both airports. Near the airport, if the
  sim's wind or QNH differs noticeably from it (X-Plane still blending in a new report), a note gives the sim's values.
- Custom weather (manual or injected): X-Plane 12's manual weather is one set of layers wherever you are, so the sim's
  weather is the source for both airports (temperature corrected for each airport's elevation). The real METAR is
  only shown for reference.

ATIS: a VATSIM controller's ATIS when one is online (real weather only: with custom weather it would not match).
Otherwise an EFB ATIS, built from the same weather the strip shows, with a runway in use and a letter that moves on
like a real ATIS: with each new METAR (real weather), or when the custom weather changes enough to matter, or when
the runway in use changes. The Ops Centre uses the same ATIS for its MCDU messages.

Sources: METAR from VATSIM (else the OFP's), TAF from aviationweather.gov (else the OFP's), ATIS from the VATSIM data
feed, runways and elevations from the airport's apt.dat, the sim's weather from X-Plane 12's weather datarefs.
Network fetches run in the background; a request always answers at once from what is cached.
"""
from __future__ import annotations

import math
import mmap
import re
import threading
import time

from .ops import parse_runway_ends, _ops_g

METAR_TTL, TAF_TTL = 120.0, 1800.0
XW_AMBER, XW_RED = 20.0, 33.0          # kt crosswind (with gusts)
TW_AMBER, TW_RED = 5.0, 10.0           # kt tailwind (with gusts)
TW_RUNWAY = 5.0                        # kt: the planned runway stays in use up to this tailwind
CLIMB_OUT_NM, ARRIVAL_NM, NEAR_NM = 40.0, 150.0, 30.0
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
RED_WX = {"TS": "THUNDERSTORM", "WS": "WIND SHEAR", "GR": "HAIL", "FC": "FUNNEL CLOUD", "VA": "VOLCANIC ASH",
          "SS": "SANDSTORM", "DS": "DUSTSTORM"}
AMBER_WX = {"CB": "CB", "TCU": "TCU", "FZ": "FREEZING", "SN": "SNOW", "SG": "SNOW GRAINS", "PL": "ICE PELLETS",
            "GS": "SMALL HAIL", "SQ": "SQUALLS", "+RA": "HEAVY RAIN", "+SHRA": "HEAVY SHOWERS"}
GW = "sim/weather/region/"
SIM_READS = ["sim/flightmodel/position/latitude", "sim/flightmodel/position/longitude",
             "sim/flightmodel/failures/onground_any", GW + "change_mode", "sim/time/zulu_time_sec",
             GW + "sealevel_pressure_pas", GW + "sealevel_temperature_c", GW + "visibility_reported_sm",
             GW + "wind_altitude_msl_m", GW + "wind_direction_degt", GW + "wind_speed_msc", GW + "shear_speed_msc",
             GW + "cloud_base_msl_m", GW + "cloud_coverage_percent", GW + "cloud_type", GW + "rain_percent",
             "sim/weather/aircraft/temperature_ambient_deg_c"]


# ---------------- METAR parsing ----------------
def parse_metar(metar: str) -> dict:
    """Wind, visibility (m), ceiling (ft), clouds, QNH, temperature and weather groups from a METAR."""
    m = " " + (metar or "").upper().split(" RMK ")[0].split(" TEMPO ")[0].split(" BECMG ")[0] + " "
    out = {"wind_dir": None, "wind_spd": None, "gust": None, "vrb": False, "vis_m": None, "ceiling_ft": None,
           "clouds": [], "qnh": None, "qnh_unit": None, "temp": None, "dew": None, "wx": [], "cavok": " CAVOK " in m,
           "time": None}
    tm = re.search(r"\s(\d{6})Z\s", m)
    out["time"] = tm.group(1) if tm else None
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
    out["clouds"] = [a + b + c for a, b, c in re.findall(r"\s(FEW|SCT|BKN|OVC|VV)(\d{3})(CB|TCU)?(?=\s)", m)]
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
    out["wx"] = re.findall(r"\s([+-]?(?:VC)?(?:MI|BC|PR|DR|BL|SH|TS|FZ)?(?:DZ|RA|SN|SG|IC|PL|GR|GS|UP|BR|FG|FU|VA|DU|SA|HZ|PY|PO|SQ|FC|SS|DS)+|TS|VCTS)(?=\s)", m)
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


def _num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _arr(v):
    return [float(x) for x in v] if isinstance(v, (list, tuple)) else []


def sim_weather(v: dict, field_ft: float, at_field: bool, qnh_unit: str) -> dict | None:
    """X-Plane 12's weather at an airport, in the same shape as parse_metar(): the surface wind layer interpolated to
    the field elevation, sea-level pressure as QNH, visibility, the lowest broken/overcast layer as the ceiling."""
    alts, dirs, spds, shears = (_arr(v.get(GW + n)) for n in ("wind_altitude_msl_m", "wind_direction_degt", "wind_speed_msc", "shear_speed_msc"))
    pas = _num(v.get(GW + "sealevel_pressure_pas"))
    if not alts or not dirs or not spds or not pas:
        return None                                   # not X-Plane 12's weather system
    fm = field_ft * 0.3048
    n = min(len(alts), len(dirs), len(spds))
    order = sorted(range(n), key=lambda i: alts[i])
    lo = max([i for i in order if alts[i] <= fm] or [order[0]], key=lambda i: alts[i])
    hi = min([i for i in order if alts[i] >= fm] or [order[-1]], key=lambda i: alts[i])
    f = 0.0 if alts[hi] == alts[lo] else (fm - alts[lo]) / (alts[hi] - alts[lo])
    f = max(0.0, min(1.0, f))
    def vec(i):
        r = math.radians(dirs[i])
        return spds[i] * math.sin(r), spds[i] * math.cos(r)
    (x1, y1), (x2, y2) = vec(lo), vec(hi)
    x, y = x1 + (x2 - x1) * f, y1 + (y2 - y1) * f
    spd_kt = math.hypot(x, y) / 0.514444
    wdir = (math.degrees(math.atan2(x, y)) + 360) % 360
    shear = 0.0
    if shears:
        s1, s2 = shears[lo] if lo < len(shears) else 0.0, shears[hi] if hi < len(shears) else 0.0
        shear = (s1 + (s2 - s1) * f) / 0.514444
    out = {"wind_dir": None, "wind_spd": int(round(spd_kt)), "gust": None, "vrb": False, "vis_m": None, "ceiling_ft": None,
           "clouds": [], "qnh": None, "qnh_unit": qnh_unit, "temp": None, "dew": None, "wx": [], "cavok": False,
           "cb": False, "tcu": False, "ws": False, "time": None}
    if spd_kt >= 1:
        out["wind_dir"] = int(round(wdir / 10.0) * 10) % 360 or 360
    if shear >= 5 and spd_kt + shear >= 10:
        out["gust"] = int(round(spd_kt + shear))
    hpa = pas / 100.0
    out["qnh"] = round(hpa / 33.8639, 2) if qnh_unit == "inHg" else int(round(hpa))
    vis_sm = _num(v.get(GW + "visibility_reported_sm"))
    if vis_sm is not None:
        out["vis_m"] = int(min(10000, round(vis_sm * 1609.34 / 100.0) * 100))
    if at_field and _num(v.get("sim/weather/aircraft/temperature_ambient_deg_c")) is not None:
        out["temp"] = int(round(_num(v.get("sim/weather/aircraft/temperature_ambient_deg_c"))))
    elif _num(v.get(GW + "sealevel_temperature_c")) is not None:
        out["temp"] = int(round(_num(v.get(GW + "sealevel_temperature_c")) - 0.0019812 * field_ft))
    bases, covs, types = _arr(v.get(GW + "cloud_base_msl_m")), _arr(v.get(GW + "cloud_coverage_percent")), _arr(v.get(GW + "cloud_type"))
    layers = []
    for i in range(min(len(bases), len(covs))):
        c = covs[i] if covs[i] <= 1.0 else covs[i] / 100.0
        if c < 0.05:
            continue
        agl = (bases[i] - fm) / 0.3048
        if agl < 0:
            continue
        cover = "FEW" if c < 0.25 else "SCT" if c < 0.5 else "BKN" if c < 0.875 else "OVC"
        cb = i < len(types) and types[i] >= 2.5
        layers.append((agl, cover, cb))
    layers.sort()
    out["clouds"] = [f"{cv}{int(round(agl / 100.0)):03d}{'CB' if cb else ''}" for agl, cv, cb in layers]
    cig = [agl for agl, cv, cb in layers if cv in ("BKN", "OVC")]
    out["ceiling_ft"] = int(round(cig[0] / 100.0) * 100) if cig else None
    out["cb"] = any(cb for agl, cv, cb in layers)
    rain = _num(v.get(GW + "rain_percent"), 0.0) or 0.0
    if rain > 0.05:
        out["wx"] = ["+RA" if rain > 0.6 else ("RA" if rain > 0.25 else "-RA")]
    if not layers and (out["vis_m"] or 0) >= 10000 and not out["wx"]:
        out["cavok"] = True
    return out


def components(wx, rwy_hdg):
    if not wx or wx.get("wind_spd") is None or rwy_hdg is None:
        return None
    if wx.get("vrb") or wx.get("wind_dir") is None:
        return {"vrb": True, "spd": wx["wind_spd"], "gust": wx.get("gust")}
    a = math.radians(wx["wind_dir"] - rwy_hdg)
    c = {"vrb": False, "hw": round(wx["wind_spd"] * math.cos(a)), "xw": round(abs(wx["wind_spd"] * math.sin(a))),
         "xw_side": "L" if math.sin(a) < 0 else "R"}
    if wx.get("gust"):
        c["hw_g"] = round(wx["gust"] * math.cos(a))
        c["xw_g"] = round(abs(wx["gust"] * math.sin(a)))
    return c


class WxBar:
    def __init__(self, plugin):
        self.plugin = plugin
        self.lock = threading.Lock()
        self.cache = {}            # icao -> {"metar", "metar_t", "taf", "taf_t", "busy"}
        self.efb_atis = {}         # icao -> {"letter", "sig", "issued", "text", "runway"}
        self._coords = None
        self._elev = {}

    # ---------------- data ----------------
    def _coord(self, icao):
        if self._coords is None or len(self._coords) != len(getattr(self.plugin, "apt_coords", []) or []):
            self._coords = {c[2]: (c[0], c[1]) for c in (getattr(self.plugin, "apt_coords", []) or [])}
        return self._coords.get(icao)

    def _field_ft(self, icao):
        """Airport elevation (ft) from its apt.dat header line."""
        if icao in self._elev:
            return self._elev[icao]
        elev = 0.0
        loc = getattr(self.plugin, "apt_index", {}).get(icao)
        if loc:
            try:
                with open(loc[0], "rb") as f:
                    mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
                    try:
                        mm.seek(loc[1])
                        parts = mm.readline().decode("utf-8", errors="ignore").split()
                        if len(parts) > 1:
                            elev = float(parts[1])
                    finally:
                        mm.close()
            except Exception:
                elev = 0.0
        self._elev[icao] = elev
        return elev

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

    def _weather(self, icao, wait=False):
        """Cached METAR/TAF for an airport; starts a background refresh when they are stale (or fetches now)."""
        now = time.time()
        with self.lock:
            e = self.cache.setdefault(icao, {})
            stale = now - e.get("metar_t", 0) > METAR_TTL or now - e.get("taf_t", 0) > TAF_TTL
            start = stale and not e.get("busy")
            if start:
                e["busy"] = True
        if start:
            if wait:
                self._refresh(icao)
            else:
                threading.Thread(target=self._refresh, args=(icao,), daemon=True, name="EFB-wx").start()
        with self.lock:
            e = self.cache.get(icao) or {}
            return e.get("metar"), e.get("taf"), e.get("metar_t", 0)

    def _runway_ends(self, icao):
        loc = getattr(self.plugin, "apt_index", {}).get(icao)
        if not loc:
            return []
        try:
            return [(n.upper(), h) for n, h in parse_runway_ends(loc[0], loc[1])]
        except Exception:
            return []

    @staticmethod
    def _pick_runway(ends, wx, planned):
        """The runway a tower would use: the planned one while its tailwind is small, else the one most into wind."""
        names = dict(ends)
        if not ends:
            return planned or None, "ofp" if planned else None
        if not wx or wx.get("wind_spd") is None or wx.get("vrb") or wx.get("wind_dir") is None or wx["wind_spd"] < 3:
            return (planned, "ofp") if planned in names else (None, None)
        if planned in names:
            c = components(wx, names[planned])
            if c and -c["hw"] <= TW_RUNWAY and c["xw"] < XW_RED:
                return planned, "ofp"
        best = max(ends, key=lambda e: math.cos(math.radians(wx["wind_dir"] - e[1])))[0]
        return best, "wind"

    # ---------------- the EFB ATIS ----------------
    def _efb_atis(self, icao, wx, rwy, real, metar_time, zulu_s):
        """The EFB's ATIS for an airport, with a letter that moves on like a real one."""
        if not wx:
            return None
        cat = flight_category(wx.get("vis_m"), wx.get("ceiling_ft"))
        with self.lock:
            st = self.efb_atis.get(icao)
            changed = st is None
            if st is not None:
                if real:
                    changed = (metar_time and metar_time != st["sig"].get("time")) or rwy != st["sig"].get("rwy")
                else:
                    o = st["sig"]
                    dd = abs(((wx.get("wind_dir") or 0) - (o.get("dir") or 0) + 540) % 360 - 180)
                    changed = (rwy != o.get("rwy") or cat != o.get("cat")
                               or abs((wx.get("wind_spd") or 0) - (o.get("spd") or 0)) > 5
                               or ((wx.get("wind_spd") or 0) >= 5 and dd > 30)
                               or (wx.get("qnh") is not None and o.get("qnh") is not None
                                   and abs(float(wx["qnh"]) - float(o["qnh"])) >= (0.03 if wx.get("qnh_unit") == "inHg" else 1)))
            if changed:
                letter = "A" if st is None else LETTERS[(LETTERS.index(st["letter"]) + 1) % 26]
                if real and metar_time:
                    issued = metar_time[2:6]
                else:
                    z = int(zulu_s or time.time()) % 86400
                    issued = f"{z // 3600:02d}{(z % 3600) // 60:02d}"
                st = {"letter": letter, "issued": issued,
                      "sig": {"time": metar_time, "rwy": rwy, "cat": cat, "dir": wx.get("wind_dir"), "spd": wx.get("wind_spd"), "qnh": wx.get("qnh")}}
            # the text always follows the latest weather; only the letter waits for a change that matters
            self.efb_atis[icao] = st
            letter, issued = st["letter"], st["issued"]
        parts = [f"{icao} INFORMATION {letter}", f"TIME {issued}Z"]
        if rwy:
            parts.append(f"RWY {rwy} IN USE FOR ARR AND DEP")
        if wx.get("wind_spd") is not None:
            if wx["wind_spd"] < 3 and not wx.get("gust"):
                parts.append("WIND CALM")
            elif wx.get("vrb") or wx.get("wind_dir") is None:
                parts.append(f"WIND VRB/{wx['wind_spd']:02d}KT")
            else:
                parts.append(f"WIND {wx['wind_dir']:03d}/{wx['wind_spd']:02d}" + (f"G{wx['gust']}" if wx.get("gust") else "") + "KT")
        if wx.get("cavok"):
            parts.append("CAVOK")
        else:
            v = wx.get("vis_m")
            if v is not None:
                parts.append("VIS 10KM OR MORE" if v >= 9999 else (f"VIS {v // 1000}KM" if v >= 5000 else f"VIS {v}M"))
            if wx.get("wx"):
                parts.append("WX " + " ".join(wx["wx"]))
            parts.append("CLOUD " + " ".join(wx["clouds"]) if wx.get("clouds") else "NSC")
        if wx.get("temp") is not None:
            parts.append(f"TEMP {wx['temp']}" + (f" DEWPOINT {wx['dew']}" if wx.get("dew") is not None else ""))
        if wx.get("qnh") is not None:
            parts.append(f"ALTIMETER {float(wx['qnh']):.2f}" if wx.get("qnh_unit") == "inHg" else f"QNH {wx['qnh']}")
        parts.append(f"ACKNOWLEDGE INFORMATION {letter} ON FIRST CONTACT")
        return {"letter": letter, "text": ". ".join(parts) + ".", "source": "efb"}

    # ---------------- one airport ----------------
    def airport(self, icao, ctx, wait=False):
        """Everything the strip and the Ops Centre need for one airport."""
        p = self.plugin
        v, o, real, on_ground, here = ctx["v"], ctx["ofp"], ctx["real"], ctx["on_ground"], ctx["here"]
        orig, dest = ctx["orig"], ctx["dest"]
        role = "dep" if icao == orig else ("arr" if icao == dest else "here")
        arrival = role == "arr" or (role == "here" and not on_ground)
        metar, taf, metar_t = self._weather(icao, wait=wait)
        if not metar and role in ("dep", "arr"):
            m = _ops_g(o, "weather", "orig_metar" if role == "dep" else "dest_metar")
            metar = m if isinstance(m, str) and m.strip() else None
        if not taf and role in ("dep", "arr"):
            t = _ops_g(o, "weather", "orig_taf" if role == "dep" else "dest_taf")
            taf = t if isinstance(t, str) and t.strip() else None
        mt = parse_metar(metar) if metar else None
        field_ft = self._field_ft(icao)
        d_apt = _dist_nm(here, self._coord(icao))
        near = d_apt is not None and d_apt < NEAR_NM
        at_field = on_ground and d_apt is not None and d_apt < 5
        unit = (mt or {}).get("qnh_unit") or ("inHg" if icao[:1] in ("K", "C", "P") or icao[:2] in ("TJ", "RJ", "RO", "MM") else "hPa")
        simwx = sim_weather(v, field_ft, at_field, unit)
        # which weather the strip shows
        if real and mt:
            wx, src = mt, "metar"
        elif simwx and (not real or near):
            wx, src = simwx, "sim"
        else:
            wx, src = mt, "metar" if mt else None
        sim_note = None
        if real and src == "metar" and near and simwx and mt:
            dd = abs((((simwx.get("wind_dir") or 0) - (mt.get("wind_dir") or 0)) + 540) % 360 - 180)
            dq = abs(float(simwx["qnh"] or 0) - float(mt["qnh"] or 0)) if simwx.get("qnh") is not None and mt.get("qnh") is not None else 0
            if ((mt.get("wind_spd") or 0) >= 5 and dd > 30) or abs((simwx.get("wind_spd") or 0) - (mt.get("wind_spd") or 0)) > 8 \
                    or dq > (0.06 if unit == "inHg" else 2):
                sim_note = simwx
        # ATIS: VATSIM's with real weather, else the EFB's
        letter, vtext = (None, None)
        ops = getattr(p, "ops", None)
        if ops is not None:
            try:
                letter, vtext = ops._atis(icao, arrival=arrival)
            except Exception:
                letter, vtext = None, None
        vatsim = {"letter": letter, "text": vtext, "source": "vatsim", "runways": _atis_runways(vtext, arrival)} if (letter or vtext) else None
        # BeyondATC's own ATIS while BeyondATC is connected: its controllers expect that letter and those runways
        batc = None
        b = getattr(p, "batc", None)
        if b is not None:
            try:
                if b.active():
                    a = b.atis_for(icao)
                    if a and a.get("letter"):
                        batc = {"letter": a["letter"], "text": a["text"], "source": "batc", "runways": a.get("runways") or []}
            except Exception:
                batc = None
        planned = str(_ops_g(o, "origin" if role == "dep" else "destination", "plan_rwy") or "").upper() if role in ("dep", "arr") else ""
        ends = self._runway_ends(icao)
        names = dict(ends)
        # which ATIS: the one chosen in Settings when it has one for this airport, else the automatic order
        # (BeyondATC while connected, VATSIM with real weather, else the EFB's own)
        choice = self.atis_source()
        if choice == "batc" and batc:
            ext = batc
        elif choice == "vatsim" and vatsim:
            ext = vatsim
        elif choice == "efb":
            ext = None
        else:
            ext = batc or (vatsim if (vatsim is not None and real) else None)
        use_vatsim = ext is vatsim and vatsim is not None
        # the runway BeyondATC actually gave us (flight set-up, clearance, taxi, approach) beats its ATIS list
        given = None
        if b is not None and role in ("dep", "arr"):
            try:
                given = b.assigned_runway(icao, role == "arr") if b.active() else None
            except Exception:
                given = None
        if given and given.get("rwy"):
            rwy, rwy_src = given["rwy"], "atc"
        elif ext and ext["runways"]:
            rwy = planned if planned in ext["runways"] else ext["runways"][0]
            rwy_src = "batc" if ext is batc else "atis"
        else:
            rwy, rwy_src = self._pick_runway(ends, wx, planned)
        atis = ext if ext else self._efb_atis(icao, wx, rwy, real and src == "metar", (mt or {}).get("time") if src == "metar" else None,
                                                         _num(v.get("sim/time/zulu_time_sec")))
        if atis is not None and not ext:
            atis["runways"] = [rwy] if rwy else []
        comp = components(wx, names.get((rwy or "").upper()))
        # alerts: only what matters
        alerts = []
        if wx:
            if True:
                for code, txt in RED_WX.items():
                    if any(code in g for g in wx["wx"]) or (code == "WS" and wx.get("ws")):
                        alerts.append({"level": "red", "text": txt})
                for code, txt in AMBER_WX.items():
                    if (code == "CB" and wx.get("cb")) or (code == "TCU" and wx.get("tcu")) or \
                            (code not in ("CB", "TCU") and any(g == code or (code[0] != "+" and code in g) for g in wx["wx"])):
                        if not any(a["text"] == txt for a in alerts):
                            alerts.append({"level": "amber", "text": txt})
            if (wx["vis_m"] is not None and wx["vis_m"] < 550) or (wx["ceiling_ft"] is not None and wx["ceiling_ft"] < 200):
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
        if planned and rwy and rwy != planned and rwy_src in ("atc", "atis", "batc", "wind"):
            lab = {"atc": "ATC", "atis": "ATIS", "batc": "ATIS"}.get(rwy_src, "IN USE")
            alerts.append({"level": "amber", "text": f"{lab} RWY {rwy}, PLANNED {planned}"})
        return {"icao": icao, "role": role, "metar": metar, "metar_age_s": int(time.time() - metar_t) if metar_t else None,
                "taf": taf, "atis": atis, "vatsim_atis": vatsim if (vatsim and not use_vatsim) else None,
                "atis_choice": choice, "atis_fallback": choice != "auto" and (atis or {}).get("source") != choice,
                "runway": rwy, "runway_src": rwy_src, "planned_rwy": planned or None,
                "wx": wx, "wx_src": src, "sim_note": sim_note, "category": flight_category(wx["vis_m"], wx["ceiling_ft"]) if wx else None,
                "components": comp, "alerts": alerts, "sim_real_wx": real}

    def _context(self):
        p = self.plugin
        v = p.bridge.call("read_datarefs", SIM_READS, timeout=1.5)
        lat = float(v.get("sim/flightmodel/position/latitude") or 0)
        lon = float(v.get("sim/flightmodel/position/longitude") or 0)
        ops = getattr(p, "ops", None)
        o = (ops.ofp if ops else None) or {}
        return {"v": v, "ofp": o, "real": int(float(v.get(GW + "change_mode") or 0)) == 7,
                "on_ground": float(v.get("sim/flightmodel/failures/onground_any") or 0) >= 1,
                "here": (lat, lon) if (lat or lon) else None,
                "orig": str(_ops_g(o, "origin", "icao_code") or "").upper(),
                "dest": str(_ops_g(o, "destination", "icao_code") or "").upper()}

    # ---------------- the strip ----------------
    def status(self, want_icao=None):
        p = self.plugin
        if not getattr(p, "db_ready", False):
            return {"status": "error", "message": "The airport index is still loading."}
        ctx = self._context()
        orig, dest, here = ctx["orig"], ctx["dest"], ctx["here"]
        near = p.find_nearest_airport(here[0], here[1], 8000.0) if here else None
        d_orig = _dist_nm(here, self._coord(orig)) if orig else None
        d_dest = _dist_nm(here, self._coord(dest)) if dest else None
        if ctx["on_ground"]:
            auto = near or (orig if orig else None)
        elif orig and d_orig is not None and d_orig < CLIMB_OUT_NM and not (d_dest is not None and d_dest < ARRIVAL_NM):
            auto = orig
        else:
            auto = dest or None
        icao = (want_icao or "").upper().strip() or auto
        if not icao or not re.fullmatch(r"[A-Z0-9]{3,4}", icao):
            return {"status": "success", "icao": None, "orig": orig, "dest": dest, "atis_choice": self.atis_source()}
        out = self.airport(icao, ctx)
        out.update(status="success", auto=auto, orig=orig, dest=dest, sim_manual_wx=not ctx["real"])
        return out

    # ---------------- for the Ops Centre ----------------
    def atis_source(self):
        """The ATIS chosen in Settings: auto, batc, vatsim or efb."""
        c = str((getattr(self.plugin, "config", {}) or {}).get("atis_source") or "auto")
        return c if c in ("auto", "batc", "vatsim", "efb") else "auto"

    def atis_info(self, icao, arrival=False):
        """The ATIS and runway the strip shows for an airport (for the Ops Centre's takeoff and landing data).
        Runs on the Ops Centre's thread (reads the sim through the bridge)."""
        try:
            ctx = self._context()
            a = self.airport(icao, ctx, wait=True)
            at = a.get("atis") or {}
            return {"letter": at.get("letter"), "text": at.get("text") or "", "source": at.get("source"),
                    "runways": list(at.get("runways") or []), "runway": a.get("runway"), "runway_src": a.get("runway_src")}
        except Exception:
            return None

    def atis_for(self, icao, arrival=False):
        """(letter, text, source) of the ATIS the strip would show: VATSIM's with real weather, else the EFB's.
        Runs on the Ops Centre's thread (reads the sim through the bridge)."""
        try:
            ctx = self._context()
            a = self.airport(icao, ctx, wait=True)
            at = a.get("atis")
            if at and at.get("letter"):
                return at["letter"], at.get("text") or "", at.get("source")
        except Exception:
            pass
        return None, None, None
