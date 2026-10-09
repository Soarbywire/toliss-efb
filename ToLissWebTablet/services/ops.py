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

from .. import EFB_VERSION
from ..services.airports import _gm_dist_m, parse_ground_map

HOPPIE_URL = "https://www.hoppie.nl/acars/system/connect.html"
HOPPIE_POLL_MIN_S, HOPPIE_POLL_MAX_S = 45, 75      # Hoppie asks clients to poll at a random 45-75 s interval
CALLSIGN_RE = re.compile(r"^[A-Z0-9]{2,8}$")


def hoppie_parse(reply):
    """Parse 'ok {FROM type {packet}} {FROM type {packet}}' (packets may contain nested braces)."""
    reply = (reply or "").strip()
    if not reply.lower().startswith("ok"):
        raise ValueError(reply[5:].strip(" {}") if reply.lower().startswith("error") else (reply or "empty reply"))
    msgs, i, n = [], 2, len(reply)
    while i < n:
        if reply[i] != "{":
            i += 1
            continue
        depth, j = 0, i
        while j < n:
            if reply[j] == "{":
                depth += 1
            elif reply[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        inner = reply[i + 1:j].strip()
        m = re.match(r"(\S+)\s+(\S+)\s+\{(.*)\}\s*$", inner, re.S)
        if m:
            msgs.append({"from": m.group(1).upper(), "type": m.group(2).lower(), "text": m.group(3)})
        elif inner:
            msgs.append({"from": "", "type": "info", "text": inner})
        i = j + 1
    return msgs


OPS_AUTO_KEYS = ["release", "wx", "notams", "prelim", "final", "todata", "atis", "oooi", "depreport", "eta", "fuel",
                 "enroute_wx", "arr_atis", "stand", "ldgdata", "blockin", "exp_rwy"]
OPS_AUTO_OFF_BY_DEFAULT = {"exp_rwy"}      # optional: expected runway from the wind when there is no ATIS
# ICAO aerodrome reference code (wingspan) of the ToLiss family
OPS_SIZE_CODE = {"A318": "C", "A319": "C", "A320": "C", "A20N": "C", "A321": "C", "A21N": "C",
                 "A332": "E", "A333": "E", "A338": "E", "A339": "E", "A342": "E", "A343": "E", "A345": "E", "A346": "E"}


def parse_stands(path, pos):
    """Stands of one airport (apt.dat 1300 lines with their 1301 details: size code, operation, airlines)."""
    stands = []
    with open(path, 'rb') as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            mm.seek(pos)
            mm.readline()
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
                    if code == '1300' and len(parts) >= 6:
                        stands.append({"name": " ".join(parts[6:]) or "STAND", "lat": float(parts[1]), "lon": float(parts[2]),
                                       "hdg": float(parts[3]), "kind": parts[4].lower(), "equip": parts[5].lower(),
                                       "size": "", "op": "", "airlines": []})
                    elif code == '1301' and len(parts) >= 3 and stands:
                        st = stands[-1]
                        st["size"] = parts[1].upper()
                        st["op"] = parts[2].lower()
                        if len(parts) >= 4:
                            st["airlines"] = [a.strip().upper() for a in " ".join(parts[3:]).replace(" ", ",").split(",") if a.strip()]
                except Exception:
                    continue
        finally:
            mm.close()
    return stands


def parse_runway_ends(path, pos):
    """Runway ends of one airport: [(name, true heading)] from apt.dat 100 lines."""
    ends = []
    with open(path, 'rb') as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            mm.seek(pos)
            mm.readline()
            while True:
                raw = mm.readline()
                if not raw:
                    break
                parts = raw.decode('utf-8', errors='ignore').split()
                if not parts:
                    continue
                if parts[0] in ('1', '16', '17', '99'):
                    break
                if parts[0] == '100' and len(parts) >= 20:
                    n1, la1, lo1, n2, la2, lo2 = parts[8], float(parts[9]), float(parts[10]), parts[17], float(parts[18]), float(parts[19])
                    def brg(a1, b1, a2, b2):
                        p1, p2, dl = math.radians(a1), math.radians(a2), math.radians(b2 - b1)
                        return (math.degrees(math.atan2(math.sin(dl) * math.cos(p2), math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl))) + 360) % 360
                    ends += [(n1, brg(la1, lo1, la2, lo2)), (n2, brg(la2, lo2, la1, lo1))]
        finally:
            mm.close()
    return ends


# stands never assigned to an airliner, by name (many sceneries don't mark their operation type)
STAND_NAME_EXCLUDE = re.compile(r"\b(CARGO|FREIGHT|FRT|CGO|MAINT\w*|MRO|HANGAR|HGR|GA|GEN(?:ERAL)?\s*AV\w*|HELI\w*|"
                                r"DE-?ICE\w*|DEICING|CATERING|FIRE|POLICE|MIL\w*|RFDS|AIR\s*AMBULANCE|BIZ\s*JET|FBO|EXEC\w*)\b", re.I)


def stand_key(name):
    """A stand name without words like GATE/STAND/BAY, e.g. 'Gate 14' -> '14', 'Stand A3' -> 'A3'."""
    n = re.sub(r"\b(GATE|STAND|BAY|PARKING|RAMP|POSITION|POS)\b", " ", str(name).upper())
    return re.sub(r"[\s_]+", "", n).strip("-")


def stand_matches(name, pattern):
    """Patterns: exact ('14', 'A3'), ranges ('1-16', 'A1-A10') and prefixes ('T3*', '5*')."""
    k, p = stand_key(name), str(pattern).upper().replace(" ", "")
    if not p:
        return False
    if p.endswith("*"):
        return k.startswith(p[:-1])
    m = re.match(r"^([A-Z]*)(\d+)-([A-Z]*)(\d+)$", p)
    if m:
        pre = m.group(1) or m.group(3)
        km = re.match(r"^([A-Z]*)(\d+)([A-Z]?)$", k)
        return bool(km) and km.group(1) == pre and int(m.group(2)) <= int(km.group(2)) <= int(m.group(4))
    return k == stand_key(p)


def ops_is_domestic(a, b):
    """Same country, judged from the ICAO codes (single-letter regions: USA, Canada, Australia, China, Russia)."""
    a, b = (a or "").upper(), (b or "").upper()
    if len(a) < 2 or len(b) < 2:
        return False
    if a[0] == b[0] and a[0] in "KCYZU":
        return True
    return a[:2] == b[:2]


def stand_unsuitable(st, need, excl):
    """None if the stand suits an airliner of size code <need>; otherwise the reason."""
    if st["op"] in ("cargo", "general_aviation", "military"):
        return {"cargo": "freight stand", "general_aviation": "general aviation", "military": "military"}[st["op"]]
    if STAND_NAME_EXCLUDE.search(st["name"] or ""):
        word = STAND_NAME_EXCLUDE.search(st["name"]).group(1).upper()
        plain = [("FREIGHT", "freight"), ("FRT", "freight"), ("CGO", "cargo"), ("CARGO", "cargo"), ("MAINT", "maintenance"),
                 ("MRO", "maintenance"), ("HANGAR", "hangar"), ("HGR", "hangar"), ("GEN", "general aviation"), ("GA", "general aviation"),
                 ("HELI", "helicopters"), ("DE", "de-icing"), ("CATERING", "catering"), ("FIRE", "fire service"), ("POLICE", "police"),
                 ("MIL", "military"), ("RFDS", "flying doctor"), ("AIR", "air ambulance"), ("BIZ", "business jets"), ("FBO", "business aviation"),
                 ("EXEC", "business aviation")]
        return "name suggests " + next((t for k, t in plain if word.startswith(k)), word.lower())
    if any(stand_matches(st["name"], p) for p in excl or []):
        return "excluded at this airport"
    if st["size"] and st["size"] < need:
        return f"too small (code {st['size']})"
    if not st["size"] and "jets" not in st["equip"] and "heavy" not in st["equip"] and st["equip"] not in ("", "all"):
        return "not for jets"
    if st["kind"] not in ("gate", "tie_down", "tie-down", "misc"):
        return st["kind"].replace("_", " ")
    return None


def choose_stand(stands, acft_icao, airline, seed="", rules=None, domestic=True, avoid=None):
    """Pick a stand suited to the aircraft and airline.
    rules (per airport, from stand_rules.json): {"exclude": [patterns],
        "airlines": {"QFA": {"domestic": [patterns], "international": [patterns], "any": [patterns]}},
        "learned": {"QFA": {"14": 3}}}"""
    import random as _r
    rules = rules or {}
    need = OPS_SIZE_CODE.get((acft_icao or "").upper(), "C")
    airline = (airline or "").upper()
    excl = rules.get("exclude", []) or []
    arule = (rules.get("airlines", {}) or {}).get(airline, {}) or {}
    rule_pats = (arule.get("domestic" if domestic else "international", []) or []) + (arule.get("any", []) or [])
    learned = (rules.get("learned", {}) or {}).get(airline, {}) or {}
    avoid = {stand_key(a) for a in (avoid or [])}
    cands = [s for s in stands if stand_unsuitable(s, need, excl) is None and stand_key(s["name"]) not in avoid]
    if not cands:
        return None
    def score(st):
        if rule_pats and any(stand_matches(st["name"], p) for p in rule_pats):
            tier = 0                                   # the airline's rule for this kind of flight
        elif learned.get(stand_key(st["name"])):
            tier = 1                                   # where this airline has parked before
        elif airline and airline in st["airlines"]:
            tier = 2                                   # the scenery lists the airline
        elif not st["airlines"]:
            tier = 3                                   # unrestricted
        else:
            tier = 4                                   # another airline's stand: last resort
        return (tier, 0 if st["kind"] == "gate" else 1, 0 if st["size"] == need else (1 if st["size"] else 2))
    best = min(score(c) for c in cands)
    top = sorted([c for c in cands if score(c) == best], key=lambda c: c["name"])
    if best[0] == 1:
        # prefer the stands used most often
        most = max(learned.get(stand_key(c["name"]), 0) for c in top)
        top = [c for c in top if learned.get(stand_key(c["name"]), 0) == most]
    return _r.Random(seed).choice(top)


OPS_WIDTH = 24          # MCDU line width


def _ops_g(d, *path, default=""):
    for k in path:
        if isinstance(d, dict):
            d = d.get(k)
        else:
            return default
    return default if d in (None, {}, []) else d


def _ops_num(v, default=0):
    try:
        return int(round(float(v)))
    except Exception:
        return default


def ops_wrap(text, width=OPS_WIDTH):
    """Wrap free text (METAR, route, NOTAM) to MCDU-width lines."""
    import textwrap
    out = []
    for para in str(text or "").upper().splitlines():
        out += textwrap.wrap(para, width) or [""]
    return out


def ops_hhmm(epoch):
    try:
        return time.strftime("%H%MZ", time.gmtime(int(float(epoch))))
    except Exception:
        return "----Z"


class OpsCentre:
    def __init__(self, plugin):
        self.plugin = plugin
        cfg = plugin.config if isinstance(getattr(plugin, 'config', None), dict) else {}
        self.logon = cfg.get("ops_logon", "")
        self.ops_callsign = cfg.get("ops_callsign", "")
        self.aircraft = cfg.get("ops_aircraft", "")
        self.enabled = bool(cfg.get("ops_enabled", False))
        self.log = []
        self.lock = threading.RLock()
        self.last_poll = 0.0
        self.next_poll = 0.0
        self.aircraft_online = None
        self.error = ""
        self.unread = 0
        # the flight being dispatched
        saved_auto = cfg.get("ops_auto", {}) if isinstance(cfg.get("ops_auto"), dict) else {}
        self.auto = {k: bool(saved_auto.get(k, k not in OPS_AUTO_OFF_BY_DEFAULT)) for k in OPS_AUTO_KEYS}
        self.ofp = None
        self.ofp_user = cfg.get("simbrief_user", "")
        self.autoload = bool(cfg.get("ops_autoload", False))    # load the latest SimBrief plan at start (off by default)
        self.flight = self._new_flight(None)
        self._saved_flight = self._load_flight()                 # the last dispatched flight, carried over a reload
        self._flight_saved_json = None
        self._next_autoload = 0.0
        self._autoload_tries = 0
        self.queue = []
        self.next_send = 0.0
        self._last_auto = 0.0
        self._load_log()
        self.thread = threading.Thread(target=self._loop, name="ToLissEFB-Ops", daemon=True)
        self.thread.start()

    # --- storage ---
    def _log_path(self):
        d = os.path.join(self.plugin.plugin_dir, "ops")
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "messages.json")

    def _load_log(self):
        try:
            with open(self._log_path(), 'r', encoding='utf-8') as f:
                self.log = json.load(f)[-500:]
        except Exception:
            self.log = []

    def _save_log(self):
        try:
            with self.lock:
                snapshot = list(self.log[-500:])
                tmp = self._log_path() + ".tmp"
                with open(tmp, 'w', encoding='utf-8') as f:
                    json.dump(snapshot, f)
                os.replace(tmp, self._log_path())
        except Exception as e:
            self.plugin.log.xplane(f"ToLiss EFB: ops log save failed: {e}\n")

    # the dispatched flight's progress, so a reload (or an X-Plane restart) carries on without re-sending anything
    def _flight_path(self):
        return os.path.join(os.path.dirname(self._log_path()), "flight.json")

    @staticmethod
    def _json_default(o):
        return {"__set__": sorted(o, key=str)} if isinstance(o, set) else str(o)

    def _load_flight(self):
        try:
            with open(self._flight_path(), 'r', encoding='utf-8') as f:
                fl = json.load(f, object_hook=lambda d: set(d["__set__"]) if set(d) == {"__set__"} else d)
            return fl if isinstance(fl, dict) and fl.get("ofp_id") else None
        except Exception:
            return None

    def _save_flight(self):
        try:
            with self.lock:
                if not self.flight.get("ofp_id"):
                    return
                text = json.dumps(self.flight, default=self._json_default)
            if text == self._flight_saved_json:
                return
            tmp = self._flight_path() + ".tmp"
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(text)
            os.replace(tmp, self._flight_path())
            self._flight_saved_json = text
        except Exception as e:
            self.plugin.log.xplane(f"ToLiss EFB: ops flight save failed: {e}\n")

    # the latest SimBrief plan, fetched by the plugin itself (when "load automatically" is ticked)
    def fetch_ofp(self):
        user = (self.ofp_user or "").strip()
        if not user:
            return False, "No SimBrief username saved yet: fetch the plan once from Dispatch & OFP."
        param = "userid" if user.isdigit() else "username"
        url = f"https://www.simbrief.com/api/xml.fetcher.php?{param}={urllib.parse.quote(user)}&json=1"
        try:
            req = urllib.request.Request(url, headers={'User-Agent': f'ToLissEFB/{EFB_VERSION}'})
            with urllib.request.urlopen(req, timeout=15) as r:
                ofp = json.loads(r.read().decode("utf-8"))
            self.set_ofp(ofp)
            return bool(self.ofp), "loaded" if self.ofp else "SimBrief returned no flight plan."
        except Exception as e:
            return False, f"SimBrief: {e}"

    def save_config(self):
        try:
            self.plugin.config.update({"ops_logon": self.logon, "ops_callsign": self.ops_callsign,
                                       "ops_aircraft": self.aircraft, "ops_enabled": self.enabled,
                                       "ops_auto": self.auto, "simbrief_user": self.ofp_user, "ops_autoload": self.autoload})
            self.plugin.save_config(self.plugin.config)
        except Exception:
            pass

    def configured(self):
        return bool(self.logon and CALLSIGN_RE.match(self.ops_callsign or "") and CALLSIGN_RE.match(self.aircraft or ""))

    # --- network ---
    def _request(self, to, mtype, packet="", frm=None):
        data = urllib.parse.urlencode({"logon": self.logon, "from": frm or self.ops_callsign, "to": to,
                                       "type": mtype, "packet": packet}).encode("utf-8")
        req = urllib.request.Request(HOPPIE_URL, data=data, headers={'User-Agent': f'ToLissEFB/{EFB_VERSION}'})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode("utf-8", errors="replace")

    def _add(self, direction, frm, to, mtype, text, status="ok", kind=None):
        entry = {"id": int(time.time() * 1000) + len(self.log), "t": int(time.time()), "dir": direction,
                 "from": frm, "to": to, "type": mtype, "text": text, "status": status}
        if kind:
            entry["kind"] = kind          # which automatic message it is (final, stand, ...): the DCDU offers the right responses
        with self.lock:
            self.log.append(entry)
            self.log = self.log[-500:]
            if direction == "in":
                self.unread += 1
        self._save_log()
        return entry

    def poll(self):
        if not (self.enabled and self.configured()):
            return
        try:
            msgs = hoppie_parse(self._request("SERVER", "poll"))
            for m in msgs:
                if m["type"] in ("telex", "cpdlc", "progress", "inforeq", "position"):
                    self._add("in", m["from"], self.ops_callsign, m["type"], m["text"])
                    if m["from"] == self.aircraft and m["type"] == "telex":
                        try:
                            self._crew_message(m["text"])
                        except Exception as e:
                            self.plugin.log.xplane(f"ToLiss EFB: ops reply failed: {e}\n")
            # is the aircraft's ATSU (ToLiss) logged on to Hoppie?
            online = hoppie_parse(self._request("SERVER", "ping", self.aircraft))
            names = " ".join(x["text"] for x in online) + " " + " ".join(x["from"] for x in online)
            self.aircraft_online = self.aircraft in names.upper().split()
            self.error = ""
        except Exception as e:
            self.error = f"Hoppie: {e}"
        self.last_poll = time.time()

    def send(self, text, to=None, as_crew=False, log_id=None, kind=None):
        # as_crew: send from the aircraft's callsign to the ops centre (ToLiss's ATSU has no AOC free-text page).
        # Only sending under the aircraft's name; collecting its messages stays with ToLiss.
        frm = self.aircraft if as_crew else self.ops_callsign
        to = self.ops_callsign if as_crew else (to or self.aircraft or "").upper()
        text = re.sub(r"[{}]", "", str(text or "")).strip().upper()
        if not self.configured():
            return False, "Set the Hoppie logon code, ops callsign and aircraft callsign first."
        if not text:
            return False, "The message is empty."
        try:
            reply = self._request(to, "telex", text, frm=frm)
            hoppie_parse(reply)
            if as_crew:
                # it arrives in the ops mailbox: collect it shortly instead of waiting up to a minute
                self.next_poll = min(self.next_poll, time.time() + 4)
            elif log_id is not None:
                self._update_entry(log_id, "ok")       # a held message, now sent
            else:
                self._add("out", frm, to, "telex", text, kind=kind)
            return True, "sent"
        except Exception as e:
            if log_id is not None:
                self._update_entry(log_id, "failed")
            else:
                self._add("in" if as_crew else "out", frm, to, "telex", text, status="failed", kind=kind)
            return False, f"Not sent: {e}"

    def _update_entry(self, entry_id, status):
        with self.lock:
            for m in self.log:
                if m["id"] == entry_id:
                    m["status"] = status
                    m["t"] = int(time.time())
                    m["rev"] = m.get("rev", 0) + 1
                    m["rt"] = int(time.time())
        self._save_log()

    # ---------- DCDU responses: the crew answers an uplink with a response key ----------
    DCDU_RESPONSES = ("ACCEPT", "REJECT", "ROGER", "STBY", "UNABLE", "REQ CHANGE", "CLOSE")

    def respond(self, entry_id, resp):
        """A DCDU response key: send the answer to the ops centre as the crew, and record it on the message."""
        resp = str(resp or "").upper()
        if resp not in self.DCDU_RESPONSES:
            return False, "Unknown response."
        with self.lock:
            m = next((x for x in self.log if x["id"] == entry_id), None)
            if not m:
                return False, "That message is no longer in the log."
            if resp == "CLOSE":
                m["dcdu"] = "closed"
                m["rev"], m["rt"] = m.get("rev", 0) + 1, int(time.time())
                self._save_log()
                return True, "Closed."
            first = next((l for l in str(m.get("text", "")).split("\n") if l.strip()), "")[:20].strip()
            edno = re.search(r"EDNO\s+(\d+)", str(m.get("text", "")))
            text = {"ACCEPT": f"LOADSHEET EDNO {edno.group(1) if edno else ''} ACCEPTED".replace("  ", " "),
                    "REJECT": f"LOADSHEET EDNO {edno.group(1) if edno else ''} REJECTED".replace("  ", " "),
                    "REQ CHANGE": "REQUEST STAND CHANGE",
                    "STBY": f"STANDBY\nRE {first}", "UNABLE": f"UNABLE\nRE {first}", "ROGER": f"ROGER\nRE {first}"}[resp]
            m["resp"], m["resp_status"] = resp, "sending"
            m["rev"], m["rt"] = m.get("rev", 0) + 1, int(time.time())
        ok, msg = self.send(text, as_crew=True)
        with self.lock:
            m["resp_status"] = "sent" if ok else "failed"
            m["rev"], m["rt"] = m.get("rev", 0) + 1, int(time.time())
        self._save_log()
        return ok, ("Sent." if ok else msg)

    def _loop(self):
        import random as _r
        stop_event = self.plugin.stop_event
        while not stop_event.is_set():
            try:
                now = time.time()
                if self.autoload and not self.ofp and self.ofp_user and now >= self._next_autoload and self._autoload_tries < 10:
                    self._autoload_tries += 1
                    ok, msg = self.fetch_ofp()
                    self._next_autoload = now + 60          # try again in a minute (e.g. no internet yet)
                    if not ok:
                        self.error = self.error or msg
                if now - getattr(self, "_last_flight_save", 0) >= 5:
                    self._last_flight_save = now
                    self._save_flight()
                if self.enabled and self.configured() and now >= self.next_poll:
                    self.poll()
                    self.next_poll = time.time() + _r.uniform(HOPPIE_POLL_MIN_S, HOPPIE_POLL_MAX_S)
                if self.enabled and self.configured():
                    if now - self._last_auto >= 5:
                        self._last_auto = now
                        self._automation()
                    self._send_queued()
                    if self.flight.get("final_mode"):
                        self._sample_touchdown()
            except Exception as e:
                self.error = str(e)
            stop_event.wait(0.2 if self.flight.get("final_mode") else 2)

    # ================= the flight and its pre-flight package =================
    @staticmethod
    def _new_flight(ofp_id):
        return {"ofp_id": ofp_id, "sent": {}, "queued": [], "ack": None, "atis_letter": None,
                "airborne": False, "beacon_seen": False, "released": False, "edno": 0,
                "oooi": {}, "eta": None, "eta_sent": None, "efob": None, "fuel_level": 0, "dest_obs": None,
                "next_eta": 0, "next_fuel": 0, "next_wx": 0, "dist_rem": None,
                "arrival_sent": False, "arr_atis_letter": None, "stand": None, "final_mode": False,
                "last_vs": None, "td_vs": None, "summary_sent": False}

    def set_ofp(self, ofp, user=""):
        with self.lock:
            return self._set_ofp_locked(ofp, user)

    def _set_ofp_locked(self, ofp, user=""):
        """Called whenever the EFB loads a SimBrief plan."""
        if not isinstance(ofp, dict) or not ofp.get("origin"):
            return
        if user and user != self.ofp_user:
            self.ofp_user = user
            self.save_config()
        ofp_id = str(_ops_g(ofp, "params", "request_id") or _ops_g(ofp, "params", "time_generated"))
        self.ofp = ofp
        if ofp_id != self.flight.get("ofp_id"):
            saved = self._saved_flight
            if saved and saved.get("ofp_id") == ofp_id:
                # the same plan as before the reload: carry on where it left off (nothing is sent again)
                fresh = self._new_flight(ofp_id)
                fresh.update(saved)
                fresh["queued"] = []
                self.flight = fresh
            else:
                self.flight = self._new_flight(ofp_id)
            self._saved_flight = None
            self.queue = [q for q in self.queue if q[0] == "reply"]

    def _sim(self, names, timeout=1.5):
        try:
            v = self.plugin.bridge.call("read_datarefs", names, timeout=timeout)
            v.pop("__missing__", None)
            return v
        except Exception:
            return {}

    def _queue(self, key, text):
        with self.lock:
            if key != "reply":
                self.flight["queued"].append(key)
            self.queue.append([key, text, None])

    def _send_queued(self):
        with self.lock:
            return self._send_queued_locked()

    def _send_queued_locked(self):
        if not self.queue or time.time() < self.next_send:
            return
        # hold messages while Hoppie shows the aircraft's ATSU offline (e.g. ToLiss powered down after shutdown);
        # they are shown in the log as waiting and sent when the ATSU is back online
        if self.aircraft_online is False:
            item = self.queue[0]
            if item[2] is None:
                item[2] = self._add("out", self.ops_callsign, self.aircraft, "telex", item[1], status="waiting", kind=item[0])["id"]
            self.flight["holding"] = True
            if self.next_poll - time.time() > 30:
                self.next_poll = time.time() + 30      # check sooner for the aircraft coming back online
            return
        self.flight["holding"] = False
        key, text, log_id = self.queue.pop(0)
        # every automatic message is wrapped to the MCDU's 24 characters per line
        text = "\n".join(l for line in str(text).split("\n") for l in (ops_wrap(line) if len(line) > OPS_WIDTH else [line]))
        ok, _ = self.send(text, log_id=log_id, kind=key)
        if ok and key != "reply":
            self.flight["sent"][key] = int(time.time())
        if key in self.flight["queued"]:
            self.flight["queued"].remove(key)
        self.next_send = time.time() + 6          # a few seconds between messages, like a real uplink sequence

    def _cs(self):
        o = self.ofp or {}
        return (_ops_g(o, "atc", "callsign") or (_ops_g(o, "general", "icao_airline") + _ops_g(o, "general", "flight_number")) or self.aircraft).upper()

    def _route(self):
        o = self.ofp or {}
        return f'{_ops_g(o, "origin", "icao_code")}-{_ops_g(o, "destination", "icao_code")}'

    # ---------- message builders ----------
    def msg_release(self):
        o, f, w = self.ofp, self.ofp.get("fuel", {}), self.ofp.get("weights", {})
        lines = [f"{self._cs()} {self._route()}", "FLIGHT RELEASE",
                 f'STD {ops_hhmm(_ops_g(o, "times", "sched_out"))} STA {ops_hhmm(_ops_g(o, "times", "sched_in"))}',
                 f'{_ops_g(o, "aircraft", "icaocode")} {_ops_g(o, "aircraft", "reg")}'.strip(),
                 f'FL{_ops_num(_ops_g(o, "general", "initial_altitude")) // 100:03d} CI{_ops_g(o, "general", "costindex")}',
                 f'ALTN {_ops_g(o, "alternate", "icao_code") or "NIL"}', "ROUTE:"]
        lines += ops_wrap(_ops_g(o, "general", "route"))
        lines += ["FUEL (KG):",
                  f'BLOCK {_ops_num(f.get("plan_ramp"))}', f'TAXI {_ops_num(f.get("taxi"))}  TRIP {_ops_num(f.get("enroute_burn"))}',
                  f'CONT {_ops_num(f.get("contingency"))}  ALTN {_ops_num(f.get("alternate_burn"))}',
                  f'FINRES {_ops_num(f.get("reserve"))}  EXTRA {_ops_num(f.get("extra"))}',
                  f'EST ZFW {_ops_num(w.get("est_zfw"))}', f'EST TOW {_ops_num(w.get("est_tow"))}',
                  f'EST LAW {_ops_num(w.get("est_ldw"))}', f"DISPATCH {self.ops_callsign}"]
        return "\n".join(l for l in lines if l is not None)

    def _metar(self, icao):
        try:
            r = urllib.request.Request(f"https://metar.vatsim.net/{icao}", headers={'User-Agent': f'ToLissEFB/{EFB_VERSION}'})
            with urllib.request.urlopen(r, timeout=8) as resp:
                t = resp.read().decode("utf-8", errors="ignore").strip()
                return t or None
        except Exception:
            return None

    def _taf(self, icao):
        try:
            r = urllib.request.Request(f"https://aviationweather.gov/api/data/taf?ids={icao}&format=raw", headers={'User-Agent': f'ToLissEFB/{EFB_VERSION}'})
            with urllib.request.urlopen(r, timeout=8) as resp:
                t = resp.read().decode("utf-8", errors="ignore").strip()
                return t or None
        except Exception:
            return None

    def msg_wx(self, icao, use_ofp=True):
        o = self.ofp or {}
        role = {_ops_g(o, "origin", "icao_code"): "orig", _ops_g(o, "destination", "icao_code"): "dest", _ops_g(o, "alternate", "icao_code"): "altn"}.get(icao)
        metar = self._metar(icao) or (use_ofp and role and _ops_g(o, "weather", f"{role}_metar")) or None
        taf = self._taf(icao) or (use_ofp and role and _ops_g(o, "weather", f"{role}_taf")) or None
        lines = [f"WX {icao}"]
        lines += ["METAR"] + ops_wrap(metar) if metar else ["METAR NOT AVBL"]
        lines += ["TAF"] + ops_wrap(taf) if taf else ["TAF NOT AVBL"]
        return "\n".join(lines)

    def _notams(self, icao, role):
        o = self.ofp or {}
        items = _ops_g(o, role, "notam", default=[])
        if isinstance(items, dict):
            items = [items]
        if not items:
            allrec = _ops_g(o, "notams", "notamdrec", default=[])
            items = [n for n in (allrec if isinstance(allrec, list) else [allrec]) if isinstance(n, dict) and str(n.get("icao_id", "")).upper() == icao]
        out = []
        for n in items:
            if not isinstance(n, dict):
                continue
            txt = n.get("notam_text") or n.get("notam_raw") or n.get("notam_report") or ""
            nid = n.get("notam_id") or n.get("source_id") or ""
            if txt:
                out.append((str(nid), re.sub(r"\s+", " ", str(txt)).strip()))
        return out

    def msg_notams(self):
        o = self.ofp or {}
        msgs, nil = [], []
        for role in ("origin", "destination", "alternate"):
            icao = _ops_g(o, role, "icao_code")
            if not icao:
                continue
            ns = self._notams(icao, role)
            if not ns:
                nil.append(icao)
                continue
            lines = [f"NOTAMS {icao} ({len(ns)})"]
            for nid, txt in ns[:6]:
                lines += ops_wrap(f"{nid} {txt[:220]}".strip()) + [""]
            if len(ns) > 6:
                lines.append(f"+{len(ns) - 6} MORE IN THE OFP")
            msgs.append("\n".join(lines).strip())
        if nil:
            msgs.append("NOTAMS\n" + " ".join(nil) + ": NIL IN OFP")
        return msgs

    def msg_prelim(self):
        o, w = self.ofp, self.ofp.get("weights", {})
        lines = ["PRELIMINARY LOADSHEET", f"{self._cs()} {self._route()}",
                 f'{_ops_g(o, "aircraft", "icaocode")} {_ops_g(o, "aircraft", "reg")}'.strip(),
                 f'PAX {_ops_num(w.get("pax_count"))}  BAGS {_ops_num(w.get("bag_count"))}',
                 f'CARGO {_ops_num(w.get("cargo"))}',
                 f'EST ZFW {_ops_num(w.get("est_zfw"))}', f'  MAX {_ops_num(w.get("max_zfw"))}',
                 f'EST TOW {_ops_num(w.get("est_tow"))}', f'  MAX {_ops_num(w.get("max_tow"))}',
                 f'EST LAW {_ops_num(w.get("est_ldw"))}', f'  MAX {_ops_num(w.get("max_ldw"))}',
                 "FINAL LDSHT TO FOLLOW"]
        return "\n".join(lines)

    def _actual_weights(self):
        """ZFW from the sim (gross weight minus fuel), take-off fuel = fuel on board minus planned taxi fuel."""
        w, f = self.ofp.get("weights", {}), self.ofp.get("fuel", {})
        v = self._sim(["sim/flightmodel/weight/m_total", "sim/flightmodel/weight/m_fuel_total",
                       "AirbusFBW/NoPax", "AirbusFBW/FwdCargo", "AirbusFBW/AftCargo"])
        total, fuel = float(v.get("sim/flightmodel/weight/m_total", 0) or 0), float(v.get("sim/flightmodel/weight/m_fuel_total", 0) or 0)
        zfw = _ops_num(total - fuel) if total > 0 else _ops_num(w.get("est_zfw"))
        taxi, trip = _ops_num(f.get("taxi")), _ops_num(f.get("enroute_burn"))
        tof = max(0, _ops_num(fuel) - taxi) if fuel > 0 else _ops_num(f.get("plan_takeoff"))
        return v, zfw, tof, zfw + tof, zfw + tof - trip, trip

    def msg_final(self):
        o, w, f = self.ofp, self.ofp.get("weights", {}), self.ofp.get("fuel", {})
        v, zfw, tof, tow, law, trip = self._actual_weights()
        mzfw, mtow, mlaw = _ops_num(w.get("max_zfw")), _ops_num(w.get("max_tow")), _ops_num(w.get("max_ldw"))
        margins = [m for m in (mzfw - zfw if mzfw else None, mtow - tow if mtow else None, mlaw - law if mlaw else None) if m is not None]
        under = min(margins) if margins else None
        pax = _ops_num(v.get("AirbusFBW/NoPax"), _ops_num(w.get("pax_count")))
        fwd, aft = _ops_num(v.get("AirbusFBW/FwdCargo")), _ops_num(v.get("AirbusFBW/AftCargo"))
        self.flight["edno"] += 1
        lines = [f'LOADSHEET FINAL {time.strftime("%H%MZ", time.gmtime())}', f'EDNO {self.flight["edno"]}',
                 f"{self._cs()} {self._route()}",
                 f'{_ops_g(o, "aircraft", "icaocode")} {_ops_g(o, "aircraft", "reg")}'.strip(),
                 f"PAX {pax}", f"HOLD FWD {fwd}  AFT {aft}",
                 f"ZFW {zfw}  MAX {mzfw}", f"TOF {tof}",
                 f"TOW {tow}  MAX {mtow}", f"TIF {trip}",
                 f"LAW {law}  MAX {mlaw}"]
        if under is not None:
            lines.append(f"UNDERLOAD {under}" if under >= 0 else f"OVERWEIGHT {-under} CHECK")
        lines += ["LMC NIL", "PLEASE ACKNOWLEDGE"]
        self.flight["ack"] = None
        return "\n".join(lines)

    def msg_todata(self):
        o = self.ofp or {}
        to = _ops_g(o, "tlr", "takeoff", default={})
        rwys = to.get("runway") if isinstance(to, dict) else None
        rwys = rwys if isinstance(rwys, list) else ([rwys] if isinstance(rwys, dict) else [])
        plan = str(_ops_g(o, "origin", "plan_rwy") or (_ops_g(to, "conditions", "planned_runway"))).upper()
        r = next((x for x in rwys if str(x.get("identifier", "")).upper() == plan), rwys[0] if rwys else None)
        c = to.get("conditions", {}) if isinstance(to, dict) else {}
        if not r:
            return f"TAKEOFF DATA\n{self._cs()} {_ops_g(o, 'origin', 'icao_code')}\nNOT AVBL IN THE OFP"
        flex = r.get("flex_temperature")
        thrust = f"FLEX {flex}" if flex not in (None, "", {}) and str(r.get("thrust_setting", "")).upper() != "TOGA" else "TOGA"
        lines = ["TAKEOFF DATA", f'{self._cs()} {_ops_g(o, "origin", "icao_code")} RWY {r.get("identifier", "")}',
                 f'OAT {c.get("temperature", "--")} QNH {c.get("altimeter", "--")}',
                 f'WIND {str(c.get("wind_direction", "---")).zfill(3)}/{c.get("wind_speed", "--")}',
                 f'TOW {_ops_num(c.get("planned_weight"))}',
                 f'{str(r.get("flap_setting", "")).replace("CONF ", "CONF ")}  {thrust}'.strip(),
                 f'V1 {r.get("speeds_v1", "---")} VR {r.get("speeds_vr", "---")} V2 {r.get("speeds_v2", "---")}',
                 f'PACKS {r.get("bleed_setting", "--")}  A/ICE {r.get("anti_ice_setting", "--")}']
        cautions = []
        try:
            planned_w = _ops_num(c.get("planned_weight"))
            _, _, _, tow, _, _ = self._actual_weights()
            if planned_w and tow > planned_w + 100:
                cautions.append(f"ACTUAL TOW {tow} ABOVE PLANNED {planned_w}")
        except Exception:
            pass
        orig = _ops_g(o, "origin", "icao_code")
        letter, atis = self._atis(orig) if orig else (None, None)
        if atis:
            dep = re.findall(r"(?:RWY|RUNWAY)S?\s*(\d{2}[LRC]?)(?=[^.]*?(?:DEP|DEPARTURE))", atis.upper()) or re.findall(r"(?:RWY|RUNWAY)S?\s*(\d{2}[LRC]?)", atis.upper())
            if dep and str(r.get("identifier", "")).upper() not in [d.upper() for d in dep]:
                cautions.append(f"ATIS {letter or ''} RWY {'/'.join(dict.fromkeys(dep))} NOT PLANNED RWY {r.get('identifier', '')}".replace("  ", " "))
        metar = self._metar(orig) if orig else None
        mt = re.search(r"\s(M?\d{2})/(M?\d{2})\s", f" {metar} ") if metar else None
        if mt and str(c.get("temperature", "")).lstrip("-").isdigit():
            now_t = -int(mt.group(1)[1:]) if mt.group(1).startswith("M") else int(mt.group(1))
            if now_t > int(c.get("temperature")) + 3:
                cautions.append(f"OAT NOW {now_t} ABOVE PLANNED {c.get('temperature')}")
        if cautions:
            lines += ["CAUTION:"] + [x for msg in cautions for x in ops_wrap(msg)] + ["RECALCULATE BEFORE", "DEPARTURE"]
            self.flight["todata_caution"] = True
        return "\n".join(lines)

    def _atis(self, icao, arrival=False):
        data = self.plugin.vatsim.cache.snapshot().get("data") or {}
        found = [a for a in data.get("atis", []) if str(a.get("callsign", "")).upper().startswith(icao) and "ATIS" in str(a.get("callsign", "")).upper()]
        if not found:
            return None, None
        pref = "_A_" if arrival else "_D_"
        found.sort(key=lambda a: 0 if pref in a.get("callsign", "") else 1)     # arrival or departure ATIS first
        a = found[0]
        return (a.get("atis_code") or "").upper() or None, " ".join(a.get("text_atis") or [])

    def _expected_runway(self, icao, metar):
        """The runway end most into the METAR wind (an estimate, not an ATIS)."""
        m = re.search(r"\b(\d{3}|VRB)(\d{2,3})(?:G\d{2,3})?KT\b", metar or "")
        if not m:
            return None
        if m.group(1) == "VRB" or int(m.group(2)) < 3:
            return "WIND CALM OR VRB"
        loc = getattr(self.plugin, 'apt_index', {}).get(icao)
        if not loc:
            return None
        ends = parse_runway_ends(loc[0], loc[1])
        if not ends:
            return None
        wdir = int(m.group(1))
        best = max(ends, key=lambda e: math.cos(math.radians(wdir - e[1])))
        return f"RWY {best[0]} (INTO WIND)"

    def msg_no_atis(self, icao, arrival=False):
        metar = self._metar(icao)
        if not metar and self.ofp:
            role = "dest" if arrival else "orig"
            metar = _ops_g(self.ofp, "weather", f"{role}_metar") or None
        lines = [f"{icao} D-ATIS NOT AVBL", "LATEST METAR:"] + (ops_wrap(metar) if metar else ["NOT AVBL"])
        if self.auto.get("exp_rwy") and metar:
            rwy = self._expected_runway(icao, metar)
            if rwy:
                lines += ["EXPECTED, ESTIMATE ONLY:"] + ops_wrap(rwy)
        return "\n".join(lines)

    def msg_atis(self, icao, arrival=False):
        letter, text = self._atis(icao, arrival)
        if not letter and not text:
            return None, None
        return letter, "\n".join([f"{icao} ATIS {letter or ''}".strip()] + ops_wrap(text))

    # ---------- automation ----------
    def _automation(self):
        with self.lock:
            return self._automation_locked()

    def _automation_locked(self):
        if not self.ofp:
            return
        fl = self.flight
        v = self._sim(["sim/flightmodel/failures/onground_any", "sim/cockpit2/switches/beacon_on",
                       "sim/flightmodel/position/latitude", "sim/flightmodel/position/longitude",
                       "sim/flightmodel/position/groundspeed", "sim/cockpit2/controls/parking_brake_ratio",
                       "sim/flightmodel/engine/ENGN_running", "sim/flightmodel/weight/m_fuel_total",
                       "sim/cockpit2/engine/indicators/fuel_flow_kg_sec", "sim/flightmodel/position/y_agl",
                       "sim/flightmodel/weight/m_total"])
        if not v:
            return
        on_ground = float(v.get("sim/flightmodel/failures/onground_any", 1) or 0) >= 1
        try:
            self._inflight(v)
        except Exception as e:
            self.plugin.log.xplane(f"ToLiss EFB: ops in-flight check failed: {e}\n")
        if not on_ground:
            fl["airborne"] = True
        if fl["airborne"]:
            return                                  # pre-flight items only until takeoff
        orig = _ops_g(self.ofp, "origin", "icao_code")
        if not fl["released"]:
            def wx_msgs():
                return [self.msg_wx(i) for i in dict.fromkeys(x for x in (orig, _ops_g(self.ofp, "destination", "icao_code"), _ops_g(self.ofp, "alternate", "icao_code")) if x)]
            done = fl.setdefault("built", set())
            for key, build in (("release", lambda: [self.msg_release()]), ("wx", wx_msgs),
                               ("notams", self.msg_notams), ("prelim", lambda: [self.msg_prelim()])):
                if key in done or not self.auto[key]:
                    continue
                try:
                    for m in build():
                        self._queue(key, m)
                    done.add(key)
                except Exception as e:
                    self.plugin.log.xplane(f"ToLiss EFB: ops could not prepare {key}: {e}\n")
            fl["released"] = all(k in done or not self.auto[k] for k in ("release", "wx", "notams", "prelim"))
        beacon = float(v.get("sim/cockpit2/switches/beacon_on", 0) or 0) >= 1
        if beacon and not fl["beacon_seen"]:
            fl["beacon_seen"] = True
            for key, build in (("final", self.msg_final), ("todata", self.msg_todata)):
                if self.auto[key] and key not in fl["sent"]:
                    try:
                        self._queue(key, build())
                    except Exception as e:
                        self.plugin.log.xplane(f"ToLiss EFB: ops could not prepare {key}: {e}\n")
        if self.auto["atis"] and orig:
            letter, text = self.msg_atis(orig)
            if letter and letter != fl["atis_letter"]:
                fl["atis_letter"] = letter
                self._queue("atis", text)
            elif not letter and fl["released"] and not fl.get("no_atis_sent") and not fl["atis_letter"]:
                fl["no_atis_sent"] = True
                try:
                    self._queue("atis", self.msg_no_atis(orig))
                except Exception as e:
                    self.plugin.log.xplane(f"ToLiss EFB: ops could not prepare the no-ATIS message: {e}\n")

    # ---------- in flight ----------
    def _event(self, text):
        """An OOOI or other flight event, recorded in the log (not sent over Hoppie)."""
        self._add("in", self.aircraft, self.ops_callsign, "event", text)

    @staticmethod
    def _gc_nm(lat1, lon1, lat2, lon2):
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dlat, dlon = p2 - p1, math.radians(lon2 - lon1)
        a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
        return 3440.065 * 2 * math.asin(min(1.0, math.sqrt(a)))

    def _remaining_nm(self, lat, lon):
        """Distance to go along the SimBrief route (great circle to the destination if there is no navlog)."""
        o = self.ofp or {}
        fixes = _ops_g(o, "navlog", "fix", default=[])
        fixes = fixes if isinstance(fixes, list) else [fixes]
        pts = []
        for f in fixes:
            try:
                pts.append((float(f["pos_lat"]), float(f["pos_long"]), float(f.get("distance") or 0)))
            except Exception:
                pass
        if len(pts) >= 2:
            near = min(range(len(pts)), key=lambda i: self._gc_nm(lat, lon, pts[i][0], pts[i][1]))
            nxt = min(near + 1, len(pts) - 1)
            if near + 1 < len(pts):
                # past the nearest fix if it is closer to the next fix than we are
                a, b = pts[near], pts[nxt]
                if self._gc_nm(lat, lon, b[0], b[1]) > self._gc_nm(a[0], a[1], b[0], b[1]):
                    nxt = near
            rem = self._gc_nm(lat, lon, pts[nxt][0], pts[nxt][1]) + sum(p[2] for p in pts[nxt + 1:])
            return rem
        try:
            return self._gc_nm(lat, lon, float(_ops_g(o, "destination", "pos_lat")), float(_ops_g(o, "destination", "pos_long")))
        except Exception:
            return None

    def _progress(self, v):
        """ETA (wheels on) and estimated fuel at destination from the current state."""
        lat, lon = float(v.get("sim/flightmodel/position/latitude", 0) or 0), float(v.get("sim/flightmodel/position/longitude", 0) or 0)
        gs = float(v.get("sim/flightmodel/position/groundspeed", 0) or 0) * 1.94384
        rem = self._remaining_nm(lat, lon)
        if rem is None or gs < 120:
            return None
        hours = rem / gs
        ff = v.get("sim/cockpit2/engine/indicators/fuel_flow_kg_sec") or []
        ff = sum(float(x) for x in ff) if isinstance(ff, list) else float(ff or 0)
        fob = float(v.get("sim/flightmodel/weight/m_fuel_total", 0) or 0)
        efob = fob - ff * 3600 * hours if ff > 0 else None
        return {"eta": int(time.time() + hours * 3600), "rem": int(rem), "efob": int(efob) if efob is not None else None}

    def _delay_text(self, eta):
        sched = _ops_num(_ops_g(self.ofp, "times", "sched_on"), 0)
        if not sched:
            return None
        mins = int(round((eta - sched) / 60))
        return "ON TIME" if abs(mins) <= 5 else (f"{mins} MIN LATE" if mins > 0 else f"{-mins} MIN EARLY")

    def _inflight(self, v):
        o, fl = self.ofp, self.flight
        now = time.time()
        on_ground = float(v.get("sim/flightmodel/failures/onground_any", 1) or 0) >= 1
        gs_kt = float(v.get("sim/flightmodel/position/groundspeed", 0) or 0) * 1.94384
        brake = float(v.get("sim/cockpit2/controls/parking_brake_ratio", 0) or 0)
        run = v.get("sim/flightmodel/engine/ENGN_running") or []
        engines_on = any(float(x) >= 1 for x in (run if isinstance(run, list) else [run]))
        x = fl["oooi"]
        dest = _ops_g(o, "destination", "icao_code")
        # ---- OOOI ----
        fob_now = float(v.get("sim/flightmodel/weight/m_fuel_total", 0) or 0)
        if "out" not in x and on_ground and ((brake < 0.5 and gs_kt > 3) or getattr(self.plugin, 'push', None)):
            x["out"] = int(now)
            fl["fob_out"] = fob_now
            if self.auto["oooi"]:
                self._event(f"OUT {ops_hhmm(now)}")
        if "off" not in x and not on_ground and gs_kt > 60:
            x["off"] = int(now)
            x.setdefault("out", int(now))
            if self.auto["oooi"]:
                self._event(f"OFF {ops_hhmm(now)}")
            planned_air = _ops_num(_ops_g(o, "times", "est_time_enroute"), 0)
            eta = int(now + planned_air) if planned_air else None
            fl["eta"] = fl["eta_sent"] = eta
            if self.auto["depreport"]:
                lines = [f"{self._cs()} DEP REPORT", f'OUT {ops_hhmm(x["out"])}  OFF {ops_hhmm(now)}']
                if eta:
                    lines.append(f"ETA {dest} {ops_hhmm(eta)}")
                    dly = self._delay_text(eta)
                    if dly:
                        lines.append(f'STA {ops_hhmm(_ops_g(o, "times", "sched_in"))} {dly}'.strip())
                lines.append("HAVE A GOOD FLIGHT")
                self._queue("depreport", "\n".join(lines))
            fl["next_eta"] = now + 600
            fl["next_fuel"] = now + 900
            fl["next_wx"] = now + 900
        if "off" in x and "on" not in x and on_ground and gs_kt > 30:
            x["on"] = int(now)
            if self.auto["oooi"]:
                self._event(f"ON {ops_hhmm(now)}")
        if "on" in x and "in" not in x and on_ground and gs_kt < 1 and brake >= 0.5:
            # IN = parking brake set on stand, as real ACARS reports it, while the ATSU still has power
            x["in"] = int(now)
            fl["fob_in"] = fob_now
            try:
                self._learn_stand(float(v.get("sim/flightmodel/position/latitude", 0) or 0), float(v.get("sim/flightmodel/position/longitude", 0) or 0))
            except Exception as e:
                self.plugin.log.xplane(f"ToLiss EFB: could not note the stand: {e}\n")
            if self.auto["oooi"]:
                self._event(f"IN {ops_hhmm(now)}")
            if self.auto["blockin"] and not fl["summary_sent"]:
                fl["summary_sent"] = True
                self._queue("blockin", self.msg_blockin())
        # below 300 ft: sample vertical speed several times a second to catch the touchdown rate
        agl_ft = float(v.get("sim/flightmodel/position/y_agl", 9999) or 9999) * 3.28084
        if "off" in x and "on" not in x and not on_ground and agl_ft < 300:
            fl["final_mode"] = True
        if on_ground or "off" not in x or "on" in x:
            return
        # ---- ETA, fuel and destination weather in flight ----
        prog = None
        if now >= fl["next_eta"] or now >= fl["next_fuel"] or not fl["arrival_sent"] and now >= fl.get("next_arr", 0):
            fl["next_arr"] = now + 60
            prog = self._progress(v)
            if prog:
                fl["dist_rem"], fl["efob"] = prog["rem"], prog["efob"]
        if now >= fl["next_eta"]:
            fl["next_eta"] = now + 300
            if prog and self.auto["eta"]:
                fl["eta"] = prog["eta"]
                if fl["eta_sent"] is None or abs(prog["eta"] - fl["eta_sent"]) >= 600:
                    change = int(round((prog["eta"] - fl["eta_sent"]) / 60)) if fl["eta_sent"] else None
                    lines = ["ETA UPDATE", f"{dest} {ops_hhmm(prog['eta'])}" + (f" ({change:+d} MIN)" if change else ""),
                             f"{prog['rem']} NM TO GO"]
                    dly = self._delay_text(prog["eta"])
                    if dly:
                        lines.append(dly)
                    fl["eta_sent"] = prog["eta"]
                    self._queue("eta", "\n".join(lines))
        if now >= fl["next_fuel"]:
            fl["next_fuel"] = now + 600
            if prog and prog["efob"] is not None and self.auto["fuel"]:
                f = o.get("fuel", {})
                planned = _ops_num(f.get("plan_landing")) or (_ops_num(f.get("plan_takeoff")) - _ops_num(f.get("enroute_burn")))
                reserve = _ops_num(f.get("alternate_burn")) + _ops_num(f.get("reserve"))
                level = 2 if reserve and prog["efob"] < reserve else (1 if planned and prog["efob"] < planned - 500 else 0)
                if level > fl["fuel_level"]:
                    fl["fuel_level"] = level
                    lines = ["FUEL CHECK", f"EFOB {dest} {prog['efob']} KG", f"PLANNED {planned} KG"]
                    lines += (["BELOW ALTN + FINAL RES", f"({reserve} KG)", "CONSIDER OPTIONS, ADVISE"] if level == 2
                              else ["BELOW PLAN, PLEASE", "MONITOR"])
                    self._queue("fuel", "\n".join(lines))
        # ---- arrival package, about 150 nm or 30 minutes out ----
        if prog and not fl["arrival_sent"] and (prog["rem"] <= 150 or prog["eta"] - now <= 1800):
            fl["arrival_sent"] = True
            self._arrival_package(v)
        if fl["arrival_sent"] and self.auto["arr_atis"] and dest:
            letter, text = self.msg_atis(dest, arrival=True)
            if letter and letter != fl["arr_atis_letter"]:
                fl["arr_atis_letter"] = letter
                self._queue("arr_atis", text)
        if now >= fl["next_wx"] and dest:
            fl["next_wx"] = now + 900
            if self.auto["enroute_wx"]:
                metar = self._metar(dest)
                obs = re.search(r"\b(\d{6})Z\b", metar or "")
                if metar and obs and obs.group(1) != fl["dest_obs"]:
                    first = fl["dest_obs"] is None
                    fl["dest_obs"] = obs.group(1)
                    lines = [f"WX UPDATE {dest}"] + ops_wrap(metar)
                    low = re.search(r"\s(0[0-7]\d\d|[0-7]\d\d)\s", f" {metar} ") or re.search(r"(BKN|OVC|VV)00[0-2]", metar)
                    if low:
                        lines += ["CAUTION: LOW VIS OR", "CEILING AT DESTINATION"]
                    if not first or low:
                        self._queue("enroute_wx", "\n".join(lines))

    # ---------- stand rules: exclusions, airline rules and stands learned from where you parked ----------
    def _rules_path(self):
        return os.path.join(self.plugin.plugin_dir, "stand_rules.json")

    def _load_rules(self):
        try:
            with open(self._rules_path(), 'r', encoding='utf-8') as f:
                r = json.load(f)
                return r if isinstance(r, dict) else {}
        except Exception:
            return {}

    def _save_rules(self, rules):
        try:
            tmp = self._rules_path() + ".tmp"
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(rules, f, indent=2, sort_keys=True)
            os.replace(tmp, self._rules_path())
        except Exception as e:
            self.plugin.log.xplane(f"ToLiss EFB: could not save stand rules: {e}\n")

    def _learn_stand(self, lat, lon):
        """At IN: note the stand the aircraft is parked on (within 40 m) for this airline at this airport."""
        o = self.ofp or {}
        dest, airline = _ops_g(o, "destination", "icao_code"), _ops_g(o, "general", "icao_airline").upper()
        loc = getattr(self.plugin, 'apt_index', {}).get(dest) if dest else None
        if not (loc and airline and lat):
            return None
        try:
            stands = parse_stands(loc[0], loc[1])
        except Exception:
            return None
        near = min(stands, key=lambda st: self._gc_nm(lat, lon, st["lat"], st["lon"]), default=None)
        if not near or self._gc_nm(lat, lon, near["lat"], near["lon"]) * 1852 > 40:
            return None
        rules = self._load_rules()
        apt = rules.setdefault(dest, {})
        key = stand_key(near["name"])
        cnt = apt.setdefault("learned", {}).setdefault(airline, {})
        cnt[key] = cnt.get(key, 0) + 1
        self._save_rules(rules)
        self.flight["parked_stand"] = near["name"]
        return near["name"]

    def stand_list(self):
        """The destination's stands with details for the Change stand window."""
        o = self.ofp or {}
        dest, airline = _ops_g(o, "destination", "icao_code"), _ops_g(o, "general", "icao_airline").upper()
        loc = getattr(self.plugin, 'apt_index', {}).get(dest) if dest else None
        if not loc:
            return {"dest": dest, "stands": [], "error": f"No scenery stands found for {dest or 'the destination'}."}
        domestic = ops_is_domestic(_ops_g(o, "origin", "icao_code"), dest)
        need = OPS_SIZE_CODE.get(_ops_g(o, "aircraft", "icaocode").upper(), "C")
        rules = self._load_rules().get(dest, {})
        excl = rules.get("exclude", []) or []
        arule = (rules.get("airlines", {}) or {}).get(airline, {}) or {}
        pats = (arule.get("domestic" if domestic else "international", []) or []) + (arule.get("any", []) or [])
        learned = (rules.get("learned", {}) or {}).get(airline, {}) or {}
        out = []
        for st in parse_stands(loc[0], loc[1]):
            k = stand_key(st["name"])
            out.append({"name": st["name"], "kind": st["kind"], "size": st["size"], "op": st["op"], "airlines": st["airlines"],
                        "lat": st["lat"], "lon": st["lon"], "reason": stand_unsuitable(st, need, excl),
                        "used": learned.get(k, 0), "rule": any(stand_matches(st["name"], p) for p in pats),
                        "excluded": any(stand_matches(st["name"], p) for p in excl)})
        cur = (self.flight.get("stand") or {}).get("name")
        return {"dest": dest, "airline": airline, "domestic": domestic, "size": need, "current": cur, "stands": out}

    def change_stand(self, mode, name=None, exclude_old=False, remember=False):
        """From the Change stand window: assign another automatically or a chosen stand."""
        o = self.ofp or {}
        dest, airline = _ops_g(o, "destination", "icao_code"), _ops_g(o, "general", "icao_airline").upper()
        loc = getattr(self.plugin, 'apt_index', {}).get(dest) if dest else None
        if not loc:
            return False, f"No scenery stands found for {dest or 'the destination'}."
        stands = parse_stands(loc[0], loc[1])
        old = (self.flight.get("stand") or {}).get("name")
        rules = self._load_rules()
        apt = rules.setdefault(dest, {})
        if exclude_old and old:
            ex = apt.setdefault("exclude", [])
            if stand_key(old) not in [stand_key(x) for x in ex]:
                ex.append(stand_key(old))
        domestic = ops_is_domestic(_ops_g(o, "origin", "icao_code"), dest)
        if mode == "choose":
            new = next((st for st in stands if st["name"] == name), None)
            if not new:
                return False, f"Stand {name} was not found in the scenery."
        else:
            new = choose_stand(stands, _ops_g(o, "aircraft", "icaocode"), airline, seed=str(time.time()),
                               rules=apt, domestic=domestic, avoid=[old] if old else None)
            if not new:
                self._save_rules(rules)
                return False, "No other suitable stand was found. Choose one yourself, with Show all stands if needed."
        if remember and airline and mode == "choose":
            lst = apt.setdefault("airlines", {}).setdefault(airline, {}).setdefault("domestic" if domestic else "international", [])
            if stand_key(new["name"]) not in [stand_key(x) for x in lst]:
                lst.append(stand_key(new["name"]))
        self._save_rules(rules)
        self.flight["stand"] = new
        notes = []
        if exclude_old and old:
            notes.append(f"stand {old} excluded at {dest}")
        if remember and mode == "choose" and airline:
            notes.append(f"stand {new['name']} saved for {airline} {'domestic' if domestic else 'international'} flights")
        if "in" in self.flight["oooi"]:
            msg = f"Stand {new['name']} noted (already parked, no message sent)."
        elif self.flight.get("arrival_sent") or "stand" in self.flight["sent"]:
            self._queue("stand", self.msg_stand(revised=True))
            self.next_send = min(self.next_send, time.time() + 1)
            msg = f"Revised stand assignment: {new['name']}, being sent."
        else:
            msg = f"Stand {new['name']} set: it is sent with the arrival package (or press Send now)."
        notes_txt = "; ".join(notes)
        return True, msg + (" " + notes_txt[:1].upper() + notes_txt[1:] + "." if notes else "")

    def reassign_stand(self):
        """The assigned stand is unsuitable: exclude it at this airport for good and assign another."""
        o = self.ofp or {}
        dest = _ops_g(o, "destination", "icao_code")
        cur = self.flight.get("stand")
        if cur and dest:
            rules = self._load_rules()
            ex = rules.setdefault(dest, {}).setdefault("exclude", [])
            key = stand_key(cur["name"])
            if key not in [stand_key(x) for x in ex]:
                ex.append(key)
            self._save_rules(rules)
        self.flight["stand"] = None
        return self.msg_stand(revised=True)

    # ---------- arrival ----------
    def _assign_stand(self):
        if self.flight.get("stand"):
            return self.flight["stand"]
        o = self.ofp or {}
        dest = _ops_g(o, "destination", "icao_code")
        loc = getattr(self.plugin, 'apt_index', {}).get(dest) if dest else None
        if not loc:
            return None
        try:
            st = choose_stand(parse_stands(loc[0], loc[1]), _ops_g(o, "aircraft", "icaocode"), _ops_g(o, "general", "icao_airline"),
                              seed=str(self.flight.get("ofp_id")), rules=self._load_rules().get(dest, {}),
                              domestic=ops_is_domestic(_ops_g(o, "origin", "icao_code"), dest))
        except Exception as e:
            self.plugin.log.xplane(f"ToLiss EFB: ops stand lookup failed: {e}\n")
            st = None
        self.flight["stand"] = st
        return st

    def msg_stand(self, revised=False):
        o = self.ofp or {}
        dest = _ops_g(o, "destination", "icao_code")
        st = self._assign_stand()
        if not st:
            return f"STAND ASSIGNMENT {dest}\nNOT AVBL: NO SUITABLE\nSTAND IN THE SCENERY\nCONTACT GROUND"
        kind = {"gate": "CONTACT GATE", "tie_down": "REMOTE STAND", "tie-down": "REMOTE STAND"}.get(st["kind"], "STAND")
        lines = [f"{'REVISED ' if revised else ''}STAND ASSIGNMENT", dest, f"STAND {st['name']}".upper(), kind]
        if st["size"]:
            lines.append(f"CODE {st['size']}")
        lines.append("GROUND POWER AVBL")
        return "\n".join(lines)

    def msg_ldgdata(self, v=None):
        o = self.ofp or {}
        ld = _ops_g(o, "tlr", "landing", default={})
        dest = _ops_g(o, "destination", "icao_code")
        if not isinstance(ld, dict) or not ld:
            return f"LANDING DATA {dest}\nNOT AVBL IN THE OFP"
        c, rw = ld.get("conditions", {}) or {}, ld.get("runway", {}) or {}
        if isinstance(rw, list):
            plan = str(_ops_g(o, "destination", "plan_rwy")).upper()
            rw = next((x for x in rw if str(x.get("identifier", "")).upper() == plan), rw[0] if rw else {})
        dry, wet = ld.get("distance_dry", {}) or {}, ld.get("distance_wet", {}) or {}
        lda = _ops_num(rw.get("length_lda") or rw.get("length"))
        metres = str(self.plugin.config.get("dist_unit", "ft")).lower() == "m"
        unit = "M" if metres else "FT"
        dist = lambda ft: int(round(ft * 0.3048)) if metres else ft      # SimBrief's runway analysis is in feet
        lines = ["LANDING DATA", f'{self._cs()} {dest} RWY {rw.get("identifier", "")}'.strip(),
                 f"LDA {dist(lda)} {unit}" if lda else "LDA ---",
                 f'WIND {str(c.get("wind_direction", "---")).zfill(3)}/{c.get("wind_speed", "--")}  OAT {c.get("temperature", "--")}',
                 f'LW {_ops_num(c.get("planned_weight"))}',
                 f'CONF {dry.get("flap_setting", "FULL")}  VREF {dry.get("speeds_vref", "---")}'.replace("CONF CONF", "CONF")]
        for label, d in (("DRY", dry), ("WET", wet)):
            if d:
                lines += [f'{label}: AUTOBRK {d.get("brake_setting", "--")}', f' RQD {dist(_ops_num(d.get("factored_distance")))} {unit}']
        cautions = []
        letter, atis = self._atis(dest, arrival=True) if dest else (None, None)
        if atis:
            arr = re.findall(r"(?:RWY|RUNWAY)S?\s*(\d{2}[LRC]?)(?=[^.]*?(?:ARR|APCH|APPROACH|LDG|LANDING))", atis.upper()) or re.findall(r"(?:RWY|RUNWAY)S?\s*(\d{2}[LRC]?)", atis.upper())
            if arr and str(rw.get("identifier", "")).upper() not in [a.upper() for a in arr]:
                cautions.append(f"ATIS {letter or ''} RWY {'/'.join(dict.fromkeys(arr))} NOT PLANNED RWY {rw.get('identifier', '')}".replace("  ", " "))
        try:
            v = v or self._sim(["sim/flightmodel/position/latitude", "sim/flightmodel/position/longitude",
                                "sim/flightmodel/position/groundspeed", "sim/flightmodel/weight/m_fuel_total",
                                "sim/cockpit2/engine/indicators/fuel_flow_kg_sec", "sim/flightmodel/weight/m_total"])
            prog = self._progress(v)
            gross = float(v.get("sim/flightmodel/weight/m_total", 0) or 0)
            fob = float(v.get("sim/flightmodel/weight/m_fuel_total", 0) or 0)
            planned_lw = _ops_num(c.get("planned_weight"))
            if prog and prog["efob"] is not None and gross > 0 and planned_lw:
                est_lw = int(gross - (fob - prog["efob"]))
                if est_lw > planned_lw + 500:
                    cautions.append(f"EST LW {est_lw} ABOVE PLANNED {planned_lw}")
        except Exception:
            pass
        if lda and _ops_num(wet.get("factored_distance")) > lda:
            cautions.append("WET RQD DISTANCE EXCEEDS LDA")
        if cautions:
            lines += ["CAUTION:"] + [x for m in cautions for x in ops_wrap(m)] + ["RECALCULATE BEFORE", "APPROACH"]
            self.flight["ldg_caution"] = True
        return "\n".join(lines)

    def _arrival_package(self, v):
        o = self.ofp or {}
        dest = _ops_g(o, "destination", "icao_code")
        if self.auto["arr_atis"] and dest:
            letter, text = self.msg_atis(dest, arrival=True)
            if text:
                self.flight["arr_atis_letter"] = letter
                self._queue("arr_atis", text)
            else:
                self.flight["no_arr_atis_sent"] = True
                self._queue("arr_atis", self.msg_no_atis(dest, arrival=True))
        for key, build in (("stand", self.msg_stand), ("ldgdata", lambda: self.msg_ldgdata(v))):
            if self.auto[key]:
                try:
                    self._queue(key, build())
                except Exception as e:
                    self.plugin.log.xplane(f"ToLiss EFB: ops could not prepare {key}: {e}\n")

    def _sample_touchdown(self):
        """Called several times a second in the last 300 ft: keep the vertical speed until the wheels touch."""
        v = self._sim(["sim/flightmodel/failures/onground_any", "sim/flightmodel/position/vh_ind_fpm"], timeout=0.5)
        if not v:
            return
        if float(v.get("sim/flightmodel/failures/onground_any", 0) or 0) >= 1:
            self.flight["td_vs"] = self.flight.get("last_vs")
            self.flight["final_mode"] = False
        else:
            self.flight["last_vs"] = float(v.get("sim/flightmodel/position/vh_ind_fpm", 0) or 0)

    def msg_blockin(self):
        o, fl = self.ofp or {}, self.flight
        x = fl["oooi"]
        def hm(sec):
            sec = max(0, int(sec))
            return f"{sec // 3600}H{(sec % 3600) // 60:02d}"
        lines = ["BLOCK IN SUMMARY", f"{self._cs()} {self._route()}",
                 f'OUT {ops_hhmm(x.get("out"))}  OFF {ops_hhmm(x.get("off"))}',
                 f'ON {ops_hhmm(x.get("on"))}  IN {ops_hhmm(x.get("in"))}']
        if x.get("out") and x.get("in") and x.get("off") and x.get("on"):
            lines.append(f'BLOCK {hm(x["in"] - x["out"])} FLT {hm(x["on"] - x["off"])}')
        sched_in = _ops_num(_ops_g(o, "times", "sched_in"), 0)
        if sched_in and x.get("in"):
            mins = int(round((x["in"] - sched_in) / 60))
            lines.append("ON TIME" if abs(mins) <= 5 else (f"{mins} MIN LATE" if mins > 0 else f"{-mins} MIN EARLY"))
        if fl.get("fob_out") and fl.get("fob_in") is not None:
            f = o.get("fuel", {})
            lines += [f'FUEL USED {int(fl["fob_out"] - fl["fob_in"])} KG',
                      f'PLAN {_ops_num(f.get("taxi")) + _ops_num(f.get("enroute_burn"))} KG']
        if fl.get("td_vs") is not None:
            lines.append(f'TOUCHDOWN {int(round(fl["td_vs"]))} FPM')
        if fl.get("parked_stand") or fl.get("stand"):
            lines.append(f'STAND {fl.get("parked_stand") or fl["stand"]["name"]}'.upper())
        lines.append(f'WELCOME TO {_ops_g(o, "destination", "icao_code")}')
        return "\n".join(lines)

    def action(self, what):
        with self.lock:
            return self._action_locked(what)

    def _action_locked(self, what):
        """Send one item now (from the Pre-flight panel)."""
        if not self.ofp:
            return False, "Load a SimBrief flight plan first (Dispatch & OFP)."
        o = self.ofp
        if what == "release":
            self._queue("release", self.msg_release())
        elif what == "wx":
            for icao in dict.fromkeys(x for x in (_ops_g(o, "origin", "icao_code"), _ops_g(o, "destination", "icao_code"), _ops_g(o, "alternate", "icao_code")) if x):
                self._queue("wx", self.msg_wx(icao))
        elif what == "notams":
            for m in self.msg_notams():
                self._queue("notams", m)
        elif what == "prelim":
            self._queue("prelim", self.msg_prelim())
        elif what == "final":
            self._queue("final", self.msg_final())
        elif what == "todata":
            self._queue("todata", self.msg_todata())
        elif what == "atis":
            letter, text = self.msg_atis(_ops_g(o, "origin", "icao_code"))
            if not text:
                self._queue("atis", self.msg_no_atis(_ops_g(o, "origin", "icao_code")))
                return True, "No VATSIM ATIS online: the latest METAR was sent instead."
            self.flight["atis_letter"] = letter
            self._queue("atis", text)
        elif what == "eta":
            self._crew_message("REQUEST ETA")
        elif what == "fuel":
            self.flight["next_fuel"] = 0
            self.flight["fuel_level"] = -1          # report even if fuel is on plan
            v = self._sim(["sim/flightmodel/position/latitude", "sim/flightmodel/position/longitude",
                           "sim/flightmodel/position/groundspeed", "sim/flightmodel/weight/m_fuel_total",
                           "sim/cockpit2/engine/indicators/fuel_flow_kg_sec"])
            prog = self._progress(v)
            if not prog or prog["efob"] is None:
                self.flight["fuel_level"] = 0
                return False, "Fuel at destination can be estimated once in flight."
            f = o.get("fuel", {})
            planned = _ops_num(f.get("plan_landing")) or (_ops_num(f.get("plan_takeoff")) - _ops_num(f.get("enroute_burn")))
            diff = prog["efob"] - planned
            self.flight["fuel_level"] = 0
            self._queue("fuel", "\n".join(["FUEL CHECK", f'EFOB {_ops_g(o, "destination", "icao_code")} {prog["efob"]} KG',
                                            f"PLANNED {planned} KG", f"{'ABOVE' if diff >= 0 else 'BELOW'} PLAN {abs(diff)} KG"]))
        elif what == "depreport":
            x = self.flight["oooi"]
            if "off" not in x:
                return False, "The departure report is sent after takeoff."
            lines = [f"{self._cs()} DEP REPORT", f'OUT {ops_hhmm(x.get("out"))}  OFF {ops_hhmm(x["off"])}']
            if self.flight.get("eta"):
                lines.append(f'ETA {_ops_g(o, "destination", "icao_code")} {ops_hhmm(self.flight["eta"])}')
            self._queue("depreport", "\n".join(lines))
        elif what == "arr_atis":
            letter, text = self.msg_atis(_ops_g(o, "destination", "icao_code"), arrival=True)
            if not text:
                self._queue("arr_atis", self.msg_no_atis(_ops_g(o, "destination", "icao_code"), arrival=True))
                return True, "No VATSIM ATIS online: the latest METAR was sent instead."
            self.flight["arr_atis_letter"] = letter
            self._queue("arr_atis", text)
        elif what == "stand":
            self._queue("stand", self.msg_stand())
        elif what == "reassign":
            if "in" in self.flight["oooi"]:
                # already parked: just exclude the stand for future flights, no message to the aircraft
                name = (self.flight.get("stand") or {}).get("name", "")
                self.reassign_stand()
                self.flight["stand"] = {"name": name, "lat": 0, "lon": 0, "kind": "", "size": "", "op": "", "airlines": [], "equip": ""} if name else None
                return True, f"Stand {name} is excluded at {_ops_g(o, 'destination', 'icao_code')} for future flights."
            self._queue("stand", self.reassign_stand())
            self.next_send = min(self.next_send, time.time() + 1)
            return True, "A revised stand assignment is being sent."
        elif what == "ldgdata":
            self._queue("ldgdata", self.msg_ldgdata())
        elif what == "blockin":
            self._queue("blockin", self.msg_blockin())
        elif what == "enroute_wx":
            self._queue("enroute_wx", self.msg_wx(_ops_g(o, "destination", "icao_code"), use_ofp=False))
        elif what == "reset":
            self.flight = self._new_flight(self.flight.get("ofp_id"))
            self.queue = []
            return True, "The flight's pre-flight items are reset; they will be sent again."
        else:
            return False, "Unknown item."
        self.next_send = min(self.next_send, time.time() + 1)
        return True, "queued"

    # ---------- messages from the crew ----------
    def _crew_message(self, text):
        t = re.sub(r"\s+", " ", str(text or "").upper()).strip()
        o = self.ofp or {}
        if re.search(r"(LOAD ?SHEET|LDSHT)\b.*\bREJECT", t):
            if not self.flight.get("edno"):
                self._queue("reply", "FINAL LOADSHEET NOT YET\nISSUED. IT FOLLOWS WHEN\nBOARDING IS COMPLETE")
                return
            self._queue("reply", f'LOADSHEET EDNO {self.flight["edno"]}\nREJECTED: NOTED\nREVISED LOADSHEET FOLLOWS')
            self._queue("reply", self.msg_final())
            return
        if re.match(r"^(?:REQ(?:UEST)?\s+)?(STAND|GATE)\s+CHANGE\b", t):
            if not self.ofp:
                self._queue("reply", "NO FLIGHT PLAN LOADED")
                return
            ok, msg = self.change_stand("auto")
            if not ok:
                self._queue("reply", "NO OTHER SUITABLE STAND\nAVAILABLE\nCONTACT GROUND ON ARRIVAL")
            elif not (self.flight.get("arrival_sent") or "stand" in self.flight["sent"]):
                self._queue("stand", self.msg_stand(revised=True))
            return
        if re.search(r"(LOAD ?SHEET|LDSHT|LS)\b.*\b(ACK|RECEIVED|RCVD|ACCEPT)", t) or re.search(r"\b(ACK|ACCEPT)\w*\b.*LOAD ?SHEET", t):
            if not self.flight.get("edno"):
                self._queue("reply", "FINAL LOADSHEET NOT YET\nISSUED. IT FOLLOWS WHEN\nBOARDING IS COMPLETE")
                return
            self.flight["ack"] = int(time.time())
            self._queue("reply", f'LOADSHEET EDNO {self.flight["edno"]}\nACKNOWLEDGED\nHAVE A GOOD FLIGHT')
            return
        m = re.match(r"^(?:REQ(?:UEST)?\s+)?(WX|WEATHER|METAR)\b\s*(.*)$", t)
        if m:
            icaos = re.findall(r"\b[A-Z]{4}\b", m.group(2)) or [x for x in (_ops_g(o, "origin", "icao_code"), _ops_g(o, "destination", "icao_code"), _ops_g(o, "alternate", "icao_code")) if x]
            for icao in list(dict.fromkeys(icaos))[:4]:
                self._queue("reply", self.msg_wx(icao, use_ofp=False))
            if not icaos:
                self._queue("reply", "WX REQUEST: PLEASE ADD THE AIRPORT, E.G. REQUEST WX YMML")
            return
        m = re.match(r"^(?:REQ(?:UEST)?\s+)?(?:D-?)?ATIS\b\s*(.*)$", t)
        if m:
            icao = (re.findall(r"\b[A-Z]{4}\b", m.group(1)) or [_ops_g(o, "destination" if self.flight["airborne"] else "origin", "icao_code")])[0]
            letter, txt = self.msg_atis(icao, arrival=self.flight["airborne"]) if icao else (None, None)
            self._queue("reply", txt or (self.msg_no_atis(icao, arrival=self.flight["airborne"]) if icao else "PLEASE ADD THE AIRPORT"))
            return
        if re.match(r"^(?:REQ(?:UEST)?\s+)?(?:FINAL\s+)?(LOAD ?SHEET|LDSHT)\b", t):
            self._queue("reply", self.msg_final() if self.ofp else "NO FLIGHT PLAN LOADED")
            return
        if re.match(r"^(?:REQ(?:UEST)?\s+)?(STAND|GATE|PARKING)\b", t):
            self._queue("reply", self.msg_stand() if self.ofp else "NO FLIGHT PLAN LOADED")
            return
        if re.match(r"^(?:REQ(?:UEST)?\s+)?(LANDING DATA|LDG DATA|LAND DATA|LDGDATA)\b", t):
            self._queue("reply", self.msg_ldgdata() if self.ofp else "NO FLIGHT PLAN LOADED")
            return
        if re.match(r"^(?:REQ(?:UEST)?\s+)?(ETA|PROGRESS|PROG)\b", t):
            v = self._sim(["sim/flightmodel/position/latitude", "sim/flightmodel/position/longitude",
                           "sim/flightmodel/position/groundspeed", "sim/flightmodel/weight/m_fuel_total",
                           "sim/cockpit2/engine/indicators/fuel_flow_kg_sec"])
            prog = self._progress(v) if self.ofp else None
            dest = _ops_g(o, "destination", "icao_code")
            if not prog:
                self._queue("reply", "PROGRESS NOT AVBL\nNO FLIGHT PLAN OR\nNOT YET IN FLIGHT")
            else:
                lines = [f"PROGRESS {dest}", f"ETA {ops_hhmm(prog['eta'])}", f"{prog['rem']} NM TO GO"]
                if prog["efob"] is not None:
                    lines.append(f"EFOB {prog['efob']} KG")
                dly = self._delay_text(prog["eta"])
                if dly:
                    lines.append(dly)
                self._queue("reply", "\n".join(lines))
            return
        if re.match(r"^(?:REQ(?:UEST)?\s+)?(TO DATA|TAKE ?OFF DATA|TODATA)\b", t):
            self._queue("reply", self.msg_todata() if self.ofp else "NO FLIGHT PLAN LOADED")
            return

    def flight_status(self):
        with self.lock:
            return self._flight_status_locked()

    def _flight_status_locked(self):
        o = self.ofp or {}
        fl = self.flight
        return {"loaded": bool(self.ofp), "callsign": self._cs() if self.ofp else "", "route": self._route() if self.ofp else "",
                "generated": _ops_num(_ops_g(o, "params", "time_generated")), "auto": self.auto, "sent": fl["sent"],
                "queued": list(fl["queued"]), "ack": fl["ack"], "atis_letter": fl["atis_letter"],
                "airborne": fl["airborne"], "beacon_seen": fl["beacon_seen"], "oooi": fl["oooi"],
                "eta": fl["eta"], "efob": fl["efob"], "dist_rem": fl["dist_rem"], "fuel_level": fl["fuel_level"],
                "todata_caution": bool(fl.get("todata_caution")), "ldg_caution": bool(fl.get("ldg_caution")),
                "stand": (fl.get("stand") or {}).get("name"), "arr_atis_letter": fl.get("arr_atis_letter"),
                "arrival_sent": fl.get("arrival_sent"), "td_vs": fl.get("td_vs"),
                "phase": ("done" if "in" in fl["oooi"] else "arrival" if (fl.get("arrival_sent") or "on" in fl["oooi"])
                          else "inflight" if "off" in fl["oooi"] else "pre"),
                "acft": f'{_ops_g(o, "aircraft", "icaocode")} {_ops_g(o, "aircraft", "reg")}'.strip(),
                "dep_atis_online": bool(self._atis(_ops_g(o, "origin", "icao_code"))[0]) if self.ofp else False,
                "arr_atis_online": bool(self._atis(_ops_g(o, "destination", "icao_code"), True)[0]) if self.ofp else False,
                "no_atis_sent": bool(fl.get("no_atis_sent")), "no_arr_atis_sent": bool(fl.get("no_arr_atis_sent")),
                "holding": bool(fl.get("holding")), "aircraft_online": self.aircraft_online,
                "parked_stand": fl.get("parked_stand"), "rules_path": self._rules_path(),
                "stand_pos": [fl["stand"]["lat"], fl["stand"]["lon"]] if fl.get("stand") and fl["stand"].get("lat") else None,
                "sta": _ops_num(_ops_g(o, "times", "sched_in"), 0), "dest": _ops_g(o, "destination", "icao_code")}

    def status(self):
        with self.lock:
            return {"enabled": self.enabled, "configured": self.configured(), "logon_set": bool(self.logon),
                    "ops_callsign": self.ops_callsign, "aircraft": self.aircraft, "aircraft_online": self.aircraft_online,
                    "last_poll": int(self.last_poll), "next_poll_in": max(0, int(self.next_poll - time.time())) if self.enabled else None,
                    "error": self.error, "unread": self.unread, "count": len(self.log), "autoload": self.autoload,
                    "simbrief_user": self.ofp_user, "ofp_loaded": bool(self.ofp)}
