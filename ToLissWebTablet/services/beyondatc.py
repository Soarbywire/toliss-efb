"""BeyondATC, read from its log: the ATC transcript, your current clearance, its ATIS and the radios.

BeyondATC (Windows) writes a Unity log, Player.log, in
    %USERPROFILE%\\AppData\\LocalLow\\Skirmish Mode Games, Inc\\BeyondATC\\
and starts it afresh every time it starts. This is BeyondATC's debug log, not an official interface: the EFB only
reads it (never writes or locks it), follows it as it grows, and skips lines it does not recognise. If none of the
lines it relies on appear while BeyondATC is clearly running, it reports that the format was not recognised instead
of showing wrong data.

Lines used:
  [Instruction] <text>                          ATC to you (clean text, digits)
  [Speech Transcription] AI Processed: <text>   you, as BeyondATC understood you (else Parser Processed, else Raw)
  [LocalVoiceInput] sid=.. ls=.. | "<text>"     every voice on the frequency (other aircraft and their ATC)
  [ATIS] ATIS text updated to <ICAO> ATIS <L> ... at <ICAO>
  [Handoff] <callsign> handed off by ControllerAI_<name>_<freq>
  [VoiceAssign] controller=ControllerAI_<name>_<freq>|...   (to name the controller on a frequency)
  [PlayerState] ..., com1: 121.700(On), com2: 121.800(On), transponder: 6326(On)
  [XP-Traffic] / [XP-Inject]                     whether BeyondATC is talking to X-Plane
"""
from __future__ import annotations

import difflib
import os
import re
import threading
import time

DEFAULT_DIR = os.path.join("AppData", "LocalLow", "Skirmish Mode Games, Inc", "BeyondATC")
MAX_EVENTS = 2000
STALE_S = 300.0                 # no log activity for this long: BeyondATC is not running

NUM = {"zero": "0", "one": "1", "two": "2", "three": "3", "tree": "3", "four": "4", "fower": "4", "five": "5",
       "fife": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9", "niner": "9"}
FIXES = {"quantas": "Qantas", "reedback": "readback", "sidd": "SID", "q n h": "QNH", "i f r": "IFR", "v f r": "VFR",
         "a t c": "ATC", "i l s": "ILS", "r n p": "RNP", "v o r": "VOR", "lineup": "line up", "q link": "QLink"}
FACILITY = r"(?:ground|tower|approach|departure|centre|center|delivery|clearance|radio|control|information)"
ATC_FAC = r"(?:ground|tower|approach|departure|centre|center|delivery|radio|control)"
TENS = {"ten", "eleven", "twelve", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety", "hundred", "thousand"}
ACTIVE_MIN = 5.0              # min: a controller heard this recently shows as active
PARTY_DELAY = 2.5             # s: a voice line waits this long in case it is our own exchange (then [Instruction] has it)


def default_path():
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return os.path.join(home, DEFAULT_DIR, "Player.log")


def speech_to_text(s: str) -> str:
    """BeyondATC's text for its speech engine, made readable: number words to digits, spelled letters joined."""
    t = " " + re.sub(r"\s+", " ", (s or "").replace(",", ", ")).strip() + " "
    for k, v in FIXES.items():
        t = re.sub(r"(?i)\b" + re.escape(k) + r"\b", v, t)
    words = t.split(" ")
    out, digits = [], ""
    def flush():
        nonlocal digits
        if digits:
            out.append(digits)
            digits = ""
    i = 0
    while i < len(words):
        w = words[i]
        core = re.sub(r"[^a-z]", "", w.lower())
        trail = w[len(w.rstrip(",.?!")):] if w else ""
        near_tens = any(re.sub(r"[^a-z]", "", words[j].lower()) in TENS for j in (i - 1, i + 1) if 0 <= j < len(words))
        if core in NUM and re.fullmatch(r"[A-Za-z]+[,.?!]?", w) and not near_tens:
            digits += NUM[core]
            if trail:
                out.append(digits + trail)
                digits = ""
        elif core == "decimal" and digits:
            digits += "."
        else:
            flush()
            if w:
                out.append(w)
        i += 1
    flush()
    t = " ".join(out)
    t = re.sub(r"\s+([,.?!])", r"\1", t)
    t = re.sub(r",(?=\S)", ", ", t)
    t = re.sub(r"(?i)\bflight level (\d{2,3})\b", r"FL\1", t)
    t = t.strip()
    return t[:1].upper() + t[1:] if t else t


def _num(s):
    try:
        return int(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _controller_name(raw: str):
    """ControllerAI_YSSY_Tower_124.700 -> ("YSSY Tower", "124.700"); ControllerAI_Center_Melbourne Centre_133.150 ->
    ("Melbourne Centre", "133.150")."""
    m = re.match(r"ControllerAI_(.+)_(\d{3}\.\d{1,3})$", raw.strip())
    if not m:
        return raw, None
    name = m.group(1).replace("_", " ")
    name = re.sub(r"(?i)^center\s+", "", name)
    return name.title() if name.isupper() else name, m.group(2)


def parse_atis_runways(text: str):
    """'RWY 16 LEFT 16 RIGHT IN USE' -> ['16L', '16R']; 'RWY 35 12 IN USE' -> ['35', '12']."""
    m = re.search(r"\bRWYS?\s+(.+?)\s+IN USE", (text or "").upper())
    if not m:
        return []
    out, toks = [], m.group(1).replace(",", " ").split()
    for i, tk in enumerate(toks):
        if re.fullmatch(r"\d{2}[LRC]?", tk):
            nxt = toks[i + 1] if i + 1 < len(toks) else ""
            suf = {"LEFT": "L", "RIGHT": "R", "CENTRE": "C", "CENTER": "C"}.get(nxt, "")
            out.append(tk + suf if len(tk) == 2 else tk)
    return list(dict.fromkeys(out))


class BeyondAtc:
    def __init__(self, plugin):
        self.plugin = plugin
        self.lock = threading.Lock()
        self.thread = None
        self._reset(None)

    def _reset(self, sig):
        self.sig = sig                 # the first bytes of the log: a new session starts a new file
        self.offset = 0
        self.partial = ""
        self.events = []
        self.next_id = 1
        self.callsign = None
        self.callsign_counts = {}
        self.digits = None
        self.clearance = {}
        self.atis = {}
        self.assigned = {"dep": {}, "arr": {}}   # the runways BeyondATC gave us: {"icao", "rwy", "src", "t"}
        self.landed = False
        # frequencies: the airports BeyondATC runs, the rows it dropped, the centres met, who was heard, dead tunes
        self.airports = {}             # icao -> {"kind": "ops"|"dynamic", "n": load order}
        self.rm_rows = {}              # icao -> {freq key}
        self.centres = {}              # freq key -> {"name", "freq", "lc", "t"}
        self.heard = {}                # freq key -> {"name", "lc", "t", "n"}: spoke on our frequency
        self.staffed = {}              # freq key -> {"name", "n"}: BeyondATC has a controller there (it got a voice)
        self.dead = {}                 # freq key -> {"lc", "t"}
        self.lclock = None             # minutes into the week, from BeyondATC's [PlayerState] lines
        self.load_n = 0
        self.radios = {}
        self.freq_names = {}
        self.freq_said = set()         # frequencies whose controller ATC has named: BeyondATC's voice list does not override
        self.controller = None
        self.xp = None                 # "connected" / "paused" / None
        self.pending_pilot = None
        self.pending_party = []
        self.line_no = 0
        self.atc_sids = set()
        self.pilot_sids = set()        # voices reading back with their callsign at the end: pilots          # voices heard reading our own instructions: those are controllers
        self.recognised = 0
        self.session_t = time.time()
        self.last_line_t = 0.0
        self.mtime = 0.0
        self.closed = False

    # ---------------- configuration ----------------
    def path(self):
        cfg = getattr(self.plugin, "config", {}) or {}
        p = str(cfg.get("batc_log_path") or "").strip()
        return p or default_path()

    def start(self, stop_event):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._loop, args=(stop_event,), daemon=True, name="EFB-BeyondATC")
        self.thread.start()

    # ---------------- following the file ----------------
    def _loop(self, stop_event):
        while not stop_event.wait(0.5):
            try:
                self.poll()
            except Exception as e:
                try:
                    self.plugin.log.xplane(f"ToLiss EFB: BeyondATC log: {e}\n")
                except Exception:
                    pass
                stop_event.wait(5.0)

    def poll(self):
        with self.lock:
            self._flush_party(time.time() - PARTY_DELAY)
        p = self.path()
        try:
            st = os.stat(p)
        except OSError:
            with self.lock:
                if self.sig is not None:
                    self._reset(None)
            return
        with open(p, "rb") as f:
            head = f.read(256)
            sig = (head, )
            with self.lock:
                if self.sig is None or head[:len(self.sig[0])] != self.sig[0][:len(head)] or st.st_size < self.offset:
                    self._reset(sig)                  # a new BeyondATC session (or a different file)
                elif len(head) > len(self.sig[0]):
                    self.sig = sig
                off = self.offset
            with self.lock:
                self.mtime = st.st_mtime
            if st.st_size == off:
                return
            f.seek(off)
            data = f.read(min(st.st_size - off, 4 * 1024 * 1024))
        text = data.decode("utf-8", errors="replace")
        with self.lock:
            self.offset = off + len(data)
            text = self.partial + text
            lines = text.split("\n")
            self.partial = lines.pop()               # an unfinished last line waits for the rest
            self.last_line_t = time.time()
            for line in lines:
                self._line(line.rstrip("\r"))
            if len(lines) > 50:
                self._flush_party(None)              # catching up with a log already written: nothing more to wait for

    # ---------------- one line ----------------
    def _event(self, kind, text, **kw):
        ev = {"id": self.next_id, "t": time.time(), "kind": kind, "text": text}
        ev.update({k: v for k, v in kw.items() if v is not None})
        self.next_id += 1
        self.events.append(ev)
        if len(self.events) > MAX_EVENTS:
            del self.events[:len(self.events) - MAX_EVENTS]
        return ev

    def _line(self, line):
        self.line_no += 1
        if self.pending_party and self.line_no - self.pending_party[0][3] > 10:
            self._flush_party(None, self.line_no - 10)       # our own exchange would have shown up by now
        if not line or line[0] != "[":
            self._flight_info(line)
            return
        if line.startswith("[Instruction] "):
            self.recognised += 1
            self._flush_pilot()
            self._instruction(line[14:].strip())
        elif line.startswith("[Speech Transcription] "):
            self.recognised += 1
            m = re.match(r"\[Speech Transcription\] (Raw|AI Processed|Parser Processed|Table Fallback): ?(.*)$", line)
            if m:
                kind, txt = m.group(1), m.group(2).strip()
                if kind == "Raw":
                    self._flush_pilot()
                    self.pending_pilot = {"raw": txt}
                elif self.pending_pilot is not None and kind in ("AI Processed", "Parser Processed") and "best" not in self.pending_pilot:
                    self.pending_pilot["best"] = txt
        elif line.startswith("------------------------------"):
            self._flush_pilot()
        elif line.startswith("[LocalVoiceInput] "):
            self.recognised += 1
            m = re.search(r'\|\s*"(.*)"\s*$', line)
            sid = re.search(r"\bsid=(\d+)", line)
            if m:
                self._party(m.group(1), sid.group(1) if sid else None)
        elif line.startswith("[ATIS] ATIS text updated to "):
            self.recognised += 1
            m = re.match(r"\[ATIS\] ATIS text updated to (([A-Z0-9]{3,4}) ATIS ([A-Z]) .*?)\s+at ([A-Z0-9]{3,4})\s*$", line)
            if m:
                icao, letter, text = m.group(2), m.group(3), m.group(1).strip()
                old = self.atis.get(icao)
                self.atis[icao] = {"icao": icao, "letter": letter, "text": text, "runways": parse_atis_runways(text), "t": time.time()}
                if not old or old.get("letter") != letter:
                    self._event("atis", f"{icao} ATIS {letter}", icao=icao, letter=letter)
        elif line.startswith("[AirportLoad] ") or line.startswith("[DynamicAirports] "):
            m = re.match(r"\[AirportLoad\] X-Plane ([A-Z0-9]{3,4}) loaded from apt\.dat", line) or \
                re.match(r"\[AirportLoad\] ([A-Z0-9]{3,4}) loaded as (dynamic)", line)
            if m:
                icao = m.group(1)
                kind = "dynamic" if m.lastindex and m.lastindex >= 2 else "ops"
                old = self.airports.get(icao)
                if not old or old["kind"] == "dynamic":
                    self.load_n += 1
                    self.airports[icao] = {"kind": "ops" if kind == "ops" else (old or {}).get("kind", "dynamic"), "n": self.load_n}
            m = re.match(r"\[DynamicAirports\] evicting dynamic ([A-Z0-9]{3,4})", line)
            if m and (self.airports.get(m.group(1)) or {}).get("kind") == "dynamic":
                self.airports.pop(m.group(1), None)
        elif line.startswith("[Comms] "):
            m = re.match(r"\[Comms\] ([A-Z0-9]{3,4}): \w+ row at (\d{3}\.\d{1,3}) .*\bremoved\b", line)
            if m:
                typ = {"CLEARANCE": "DEL", "DELIVERY": "DEL", "UNICOM": "CTAF", "CTAF": "CTAF", "GROUND": "GND", "TOWER": "TWR",
                       "APPROACH": "APP", "DEPARTURE": "DEP", "ATIS": "ATIS"}.get(line.split(":", 1)[1].split()[0].upper(), "?")
                self.rm_rows.setdefault(m.group(1), set()).add((typ, self._fkey(m.group(2))))
            m = re.match(r"\[Comms\] Tuned (\d{3}\.\d{1,3}) matched center (.+?) via ", line)
            if m:
                self._centre(m.group(1), m.group(2))
                self.dead.pop(self._fkey(m.group(1)), None)
            m = re.match(r"\[Comms\] (\d{3}\.\d{1,3}) not found\. Aircraft: Player", line)
            if m:
                self.dead[self._fkey(m.group(1))] = self._stamp()
            m = re.match(r"\[Comms\] Duplicate frequency (\d{3}\.\d{1,3}): .* vs center (.+?) -> center wins", line)
            if m:
                self._centre(m.group(1), m.group(2))
        elif line.startswith("[Handoff] "):
            m = re.match(r"\[Handoff\] (.+?) handed off by (ControllerAI_\S.*)$", line)
            if m and (not self.callsign or m.group(1).strip().lower() == self.callsign.lower()):
                name, freq = _controller_name(m.group(2))
                self._heard(freq, name, centre="ControllerAI_Center_" in m.group(2))
                self._event("handoff", f"Handed off by {name}" + (f" {freq}" if freq else ""))
        elif line.startswith("[VoiceAssign] controller="):
            m = re.match(r"\[VoiceAssign\] controller=(ControllerAI_[^|]+)\|", line)
            if m:
                name, freq = _controller_name(m.group(1))
                if freq:
                    self.staffed[self._fkey(freq)] = {"name": name, "n": self.line_no}
                    if "ControllerAI_Center_" in m.group(1):
                        self._centre(freq, name)
                if freq and self._fkey(freq) not in self.freq_said:
                    self.freq_names[self._fkey(freq)] = name
        elif line.startswith("[PlayerState] "):
            self.recognised += 1
            ck = re.match(r"\[PlayerState\] (\w+) (\d{1,2}):(\d{2}),", line)
            if ck:
                days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
                d = days.index(ck.group(1).lower()) if ck.group(1).lower() in days else 0
                self.lclock = d * 1440 + int(ck.group(2)) * 60 + int(ck.group(3))
            m = re.search(r"com1: ([\d.]+)\((\w+)\), com2: ([\d.]+)\((\w+)\), transponder: (\d+)\((\w+)\)", line)
            if m:
                self.radios = {"com1": m.group(1), "com2": m.group(3), "xpdr": m.group(5), "t": time.time()}
                self.controller = self.freq_names.get(self._fkey(m.group(1))) or self.controller
        elif line.startswith("Input System module state changed to: Shutdown"):
            self.closed = True
        elif line.startswith("[XP-Traffic] ") or line.startswith("[XP-Inject] "):
            if "heartbeat lost" in line or "telemetry stopped" in line:
                self.xp = "paused"
            elif "handshake" in line or "resumed" in line or "telemetry received" in line:
                self.xp = "connected"

    def _stamp(self):
        return {"lc": self.lclock, "t": time.time(), "n": self.line_no}

    def _heard(self, freq, name=None, centre=False):
        """A controller spoke (or was assigned a voice, or handed us off) on this frequency."""
        if not freq:
            return
        k = self._fkey(freq)
        e = dict(self._stamp(), name=name or (self.heard.get(k) or {}).get("name") or self.freq_names.get(k))
        self.heard[k] = e
        self.dead.pop(k, None)
        if centre or (name and re.search(r"(?i)\b(centre|center|control)\b", name)):
            self._centre(freq, name)

    def _centre(self, freq, name):
        k = self._fkey(freq)
        nm = re.sub(r"(?i)^center\s+", "", str(name or "").strip())
        nm = nm.title() if nm.isupper() else nm
        self.centres[k] = dict(self._stamp(), name=nm or (self.centres.get(k) or {}).get("name"), freq=k)

    def _recent(self, st):
        """Heard within the last few minutes: by BeyondATC's own clock when it has logged one, else ours."""
        if not st:
            return False
        if st.get("lc") is not None and self.lclock is not None:
            return (self.lclock - st["lc"]) % 10080 <= ACTIVE_MIN
        return time.time() - st.get("t", 0) <= ACTIVE_MIN * 60

    @staticmethod
    def _fkey(f):
        try:
            return f"{float(f):.3f}"
        except (TypeError, ValueError):
            return str(f)

    def _flush_pilot(self):
        p = self.pending_pilot
        self.pending_pilot = None
        if p:
            txt = p.get("best") or p.get("raw") or ""
            txt = txt.strip()
            if txt:
                self._event("pilot", txt[:1].upper() + txt[1:])

    def _learn_callsign(self, text):
        m = re.match(r"^([A-Z][A-Za-z]*(?: [A-Z][A-Za-z]*)? \d{1,4}[A-Z]{0,2})\b", text)
        if m:
            cs = m.group(1)
            self.callsign_counts[cs] = self.callsign_counts.get(cs, 0) + 1
            best = max(self.callsign_counts, key=self.callsign_counts.get)
            if best != self.callsign:
                self.callsign = best
                self.digits = re.sub(r"\D", "", best.split()[-1]) + (re.sub(r"[^A-Z]", "", best.split()[-1]) or "")

    def _instruction(self, text):
        self._learn_callsign(text)
        # the same words were just spoken on the frequency: they are ours, not another aircraft's
        n = self._norm(text)
        keep = []
        for p in self.pending_party:
            if difflib.SequenceMatcher(None, self._norm(p[1]), n).ratio() < 0.75:
                keep.append(p)
            elif p[4]:
                self.atc_sids.add(p[4])                 # this voice reads ATC's words: a controller
        self.pending_party = keep
        fac = None
        FAC = r"((?!Contact\b|Clarify\b|Negative\b)[A-Z][a-z]+(?: [A-Z][a-z]+)? (?:Ground|Tower|Approach|Departure|Centre|Center|Delivery|Radio|Control))\b"
        m = re.match(r"^[^,]+,\s*" + FAC, text) or re.match(r"^" + FAC + r"(?=[ ,]+[A-Z][A-Za-z]* \d)", text)
        freq = self.radios.get("com1")
        if m:
            fac = m.group(1).title()
            if freq:
                self.freq_names[self._fkey(freq)] = fac
                self.freq_said.add(self._fkey(freq))
                self.controller = fac
        who = fac or self.controller
        self._event("atc", text, who=who, freq=freq)
        if freq:
            self._heard(freq, who)
        ct = re.search(r"\bcontact ([A-Za-z]+(?: [A-Za-z]+)*?)(?: on)? (1\d{2}\.\d{1,3})\b", text, re.I)
        if ct and re.search(r"(?i)\b(centre|center|control)\b", ct.group(1)):
            k = self._fkey(ct.group(2))
            if k not in self.centres:
                self.centres[k] = {"name": ct.group(1).title(), "freq": k, "lc": None, "t": 0}
        self._assigned_from(text)
        self._clearance(text)

    def _flight_info(self, line):
        """The FLIGHT INFORMATION block BeyondATC writes when a flight starts: its airports and the runways it assigned."""
        m = re.match(r"(Departure Airport|Destination Airport|Departure Runway|Arrival Runway): *(\S*)\s*$", line)
        if not m:
            return
        key, val = m.group(1), m.group(2).upper()
        side = "dep" if key.startswith("Departure") else "arr"
        a = self.assigned[side]
        if key.endswith("Airport"):
            self.landed = False
            if val != a.get("icao"):
                self.assigned[side] = {"icao": val or None}
        elif re.fullmatch(r"\d{2}[LRC]?", val):
            self._assign(side, val, "flight")

    def _assign(self, side, rwy, src):
        a = self.assigned[side]
        if a.get("rwy") != rwy:
            a.update(rwy=rwy, src=src, t=time.time())
            if a.get("icao"):
                self._event("runway", f"{'Departure' if side == 'dep' else 'Arrival'} runway {rwy} at {a['icao']}", icao=a["icao"], side=side, rwy=rwy)

    def _assigned_from(self, t):
        """A runway named in an instruction to us: the departure runway before takeoff, the arrival runway after."""
        low = t.lower()
        r = (re.search(r"\brunway (\d{2}[LRC]?),? cleared to land\b", t, re.I) or re.search(r"\bcleared to land,? runway (\d{2}[LRC]?)", t, re.I)
             or re.search(r"\bcleared (?:ILS|RNAV|RNP|VOR|LOC|GLS|NDB|visual)\b[^,.]*? approach,? runway (\d{2}[LRC]?)", t, re.I)
             or re.search(r"\bexpect (?:radar vectors for )?(?:[A-Za-z]+ approach )?runway (\d{2}[LRC]?)", t, re.I))
        if r:
            self._assign("arr", r.group(1).upper(), "atc")
            if "cleared to land" in low:
                self.landed = True
            return
        r = (re.search(r"\brunway (\d{2}[LRC]?),? cleared for take ?off\b", t, re.I) or re.search(r"\bcleared for take ?off,? runway (\d{2}[LRC]?)", t, re.I)
             or re.search(r"\brunway (\d{2}[LRC]?),? line ?up and wait\b", t, re.I)
             or re.search(r"\bhold short (?:of )?(?:runway )?(\d{2}[LRC]?) for take ?off\b", t, re.I))
        if r:
            if self.landed and self.assigned["arr"].get("icao"):
                self.assigned["dep"] = {"icao": self.assigned["arr"]["icao"]}     # off again from where we landed
                self.landed = False
            self._assign("dep", r.group(1).upper(), "atc")
            return
        if str((self.clearance.get("runway") or {}).get("value", "")).startswith("LAND"):
            return                                   # taxiing in: runways named now are crossings
        if re.search(r"\bcleared to (?!land)", low) and re.search(r"\bsquawk\b|\bvia\b|\bclimb\b", low):
            r = re.search(r"\brunway (\d{2}[LRC]?)\b", t, re.I)            # departure clearance
        if not r and re.search(r"\btaxi\b", low) and not re.search(r"\b(?:request|ready for) taxi\b", low):
            r = re.search(r"\btaxi to (?:holding point \w+,? )?runway (\d{2}[LRC]?)\b", t, re.I) or \
                re.search(r"\btaxi\b.*?\bholding point \w+,? runway (\d{2}[LRC]?)\b", t, re.I)
        if r:
            self._assign("dep", r.group(1).upper(), "atc")

    def assigned_runway(self, icao, arrival):
        """The runway BeyondATC assigned at this airport (departure or arrival side), or None."""
        with self.lock:
            a = self.assigned["arr" if arrival else "dep"]
            if a.get("rwy") and (not a.get("icao") or a["icao"] == (icao or "").upper()):
                return {"rwy": a["rwy"], "src": a.get("src"), "t": a.get("t")}
            return None

    @staticmethod
    def _norm(t):
        return re.sub(r"[^a-z0-9]", "", speech_to_text(t).lower())

    def _party(self, raw, sid=None):
        txt = speech_to_text(raw)
        if self.digits:
            norm = re.sub(r"\s+", "", txt)
            if re.search(r"(?<!\d)" + re.escape(self.digits) + r"(?![\d])", norm, re.I):
                return                                    # ours: already in the transcript from [Instruction] / transcription
        low = txt.lower()
        callsign_end = re.search(r"(?:,\s*|\s)[a-z]+(?: [a-z]+)? \d+[a-z]?(?: [a-z]+)?[.!]?$", low) and not low.startswith("all aircraft")
        if re.match(r"^[a-z]+(?: [a-z]+)? " + ATC_FAC + r"\b,", low):
            who = "Aircraft"                              # "Canberra ground, Velocity 284, ...": a pilot calling
        elif callsign_end:
            who = "Aircraft"                              # a readback ends with the callsign
        elif low.startswith("all aircraft") or re.match(r"^[^,]+,\s*[a-z]+(?: [a-z]+)? " + ATC_FAC + r"\b[,.]", low):
            who = "ATC"                                   # "Velocity 284, canberra ground, ..."
        elif re.search(r"\b(request|requesting|ready|with you|at gate|at stand|holding short|wilco|roger|leaving|passing|for departure)\b", low):
            who = "Aircraft"
        elif sid and sid in self.pilot_sids and sid not in self.atc_sids:
            who = "Aircraft"
        elif re.match(r"^[a-z]+(?: [a-z]+)? \d+[a-z]?(?: [a-z]+)?,", low):
            who = "ATC"                                   # starts with a callsign: ATC addressing an aircraft
        else:
            who = "ATC" if sid in self.atc_sids else "Aircraft"
        if sid and callsign_end and sid not in self.atc_sids:
            self.pilot_sids.add(sid)
        self.pending_party.append((time.time(), txt, who, self.line_no, sid))

    def _flush_party(self, before, before_line=None):
        keep = []
        for t, txt, who, ln, sid in self.pending_party:
            if (before_line is not None and ln <= before_line) or (before_line is None and (before is None or t <= before)):
                self._event("party", txt, who=who)
                if who and who != "Aircraft" and self.radios.get("com1"):
                    self._heard(self.radios["com1"], None)
            else:
                keep.append((t, txt, who, ln, sid))
        self.pending_party = keep

    # ---------------- the clearance ----------------
    def _set(self, key, value, text):
        if value is None:
            self.clearance.pop(key, None)
        else:
            self.clearance[key] = {"value": value, "text": text, "t": time.time()}

    def _clearance(self, text):
        t = text
        low = t.lower()
        sq = re.search(r"\bsquawk (\d{4})\b", t, re.I)
        if sq:
            self._set("squawk", sq.group(1), t)
        alt = re.search(r"\b(?:climb|descend)(?: and maintain)?(?: via (?:the )?SID)?(?: to)? (FL ?\d{2,3}|flight level \d{2,3}|[\d,]{3,6})(?: feet| ft)?\b", t, re.I) \
            or re.search(r"\bmaintain (FL ?\d{2,3}|flight level \d{2,3}|[\d,]{3,6})(?: feet| ft)?\b", t, re.I)
        if alt:
            a = alt.group(1)
            fl = re.search(r"\d{2,3}", a) if re.match(r"(?i)fl|flight", a) else None
            self._set("altitude", f"FL{int(fl.group(0)):03d}" if fl else f"{_num(a):,} ft" if _num(a) else a, t)
        hd = re.search(r"\bheading (\d{3})\b", t, re.I)
        if hd:
            self._set("heading", hd.group(1), t)
        elif re.search(r"\bfly runway heading\b", low):
            self._set("heading", "RWY HDG", t)
        dct = re.search(r"\b(?:proceed|cleared) direct(?: to)? ([A-Z][A-Za-z0-9]+(?: [A-Z][A-Za-z]+)*)", t) or re.search(r"\bdirect ([A-Z0-9]{2,5})\b", t)
        if dct:
            self._set("direct", dct.group(1), t)
        if re.search(r"resume own navigation|proceed direct|cleared direct|\bdirect [A-Z0-9]{2,5}\b", t, re.I):
            self._set("heading", None, t)
        sp = re.search(r"\b(?:reduce|increase|maintain)?\s*speed (?:to )?(\d{3})\s*(?:kts|knots)\b", t, re.I)
        if sp:
            self._set("speed", f"{sp.group(1)} kt", t)
        elif re.search(r"minimum approach speed", low):
            self._set("speed", "MIN APP", t)
        elif re.search(r"resume normal speed|no speed restriction", low):
            self._set("speed", None, t)
        fr = re.search(r"\bcontact ([A-Za-z]+(?: [A-Za-z]+)*?)(?: on)? (1\d{2}\.\d{1,3})\b", t, re.I) or \
            re.search(r"\b(ground|tower|approach|departure|centre|center) on (1\d{2}\.\d{1,3})\b", t, re.I)
        if fr:
            name = fr.group(1).title()
            if " " not in name and self.controller and " " in self.controller:
                name = self.controller.rsplit(" ", 1)[0] + " " + name      # "contact ground" from Sydney Tower: Sydney Ground
            self._set("next_freq", f"{name} {fr.group(2)}", t)
            self.freq_names[self._fkey(fr.group(2))] = name
            self.freq_said.add(self._fkey(fr.group(2)))
        ap = re.search(r"\bcleared (ILS|RNAV|RNP|VOR|LOC|GLS|NDB|visual)\b[^,.]*? approach,? runway (\d{2}[LRC]?)", t, re.I)
        if ap:
            self._set("approach", f"{ap.group(1).upper()} {ap.group(2)}", t)
        exp = re.search(r"\bexpect (?:radar vectors for )?(?:(ILS|RNAV|RNP|VOR|visual) approach )?runway (\d{2}[LRC]?)", t, re.I)
        if exp and not ap:
            self._set("expect", (f"{exp.group(1).upper()} " if exp.group(1) else "") + exp.group(2), t)
        if re.search(r"cleared to land", low):
            for k in ("heading", "speed", "direct", "expect"):
                self.clearance.pop(k, None)
        if re.search(r"cleared for take ?off", low):
            self.clearance.pop("taxi", None)
        if re.search(r"\btaxi\b", low) and str((self.clearance.get("runway") or {}).get("value", "")).startswith("LAND"):
            for k in ("altitude", "approach", "departure", "heading", "speed", "direct", "expect"):
                self.clearance.pop(k, None)
        land = re.search(r"\brunway (\d{2}[LRC]?),? cleared to land\b", t, re.I) or re.search(r"\bcleared to land,? runway (\d{2}[LRC]?)", t, re.I)
        if land:
            self._set("runway", f"LAND {land.group(1)}", t)
        tko = re.search(r"\brunway (\d{2}[LRC]?),? cleared for take ?off\b", t, re.I) or re.search(r"\bcleared for take ?off,? runway (\d{2}[LRC]?)", t, re.I)
        if tko:
            self._set("runway", f"TAKEOFF {tko.group(1)}", t)
        luw = re.search(r"\brunway (\d{2}[LRC]?),? line ?up and wait\b", t, re.I)
        if luw:
            self._set("runway", f"LINE UP {luw.group(1)}", t)
        # departure clearance
        dep = re.search(r"\bcleared to (.+?)(?: airport)?,", t, re.I)
        if dep and re.search(r"\bsquawk\b|\brunway\b", t, re.I) and "cleared to land" not in low and "taxi" not in low:
            route = re.search(r"\bvia ([A-Za-z0-9 ]+? departure)\b", t, re.I) or re.search(r"\bradar vectors ([A-Z0-9]{2,5})\b", t)
            rw = re.search(r"\brunway (\d{2}[LRC]?)\b", t, re.I)
            parts = [dep.group(1).strip()]
            if route:
                parts.append(("via " if "departure" in route.group(1).lower() else "vectors ") + route.group(1))
            if rw:
                parts.append(f"RWY {rw.group(1)}")
            self._set("departure", " · ".join(parts), t)
        # taxi
        if re.search(r"\btaxi\b", low) and not re.search(r"\b(?:request|ready for) taxi\b", low):
            via = re.search(r"\bvia ((?:[A-Z][0-9]?(?:,| and|\s)+)*[A-Z][0-9]?)\b(?!\w)", t) or \
                re.search(r"\btaxi to ((?:[A-Z][0-9]?(?:,|,? and|,? then|\s)+)*[A-Z][0-9]?)\b(?!\w)", t)
            hs = re.search(r"\bhold short (?:of )?(?:runway )?(\d{2}[LRC]?)", t, re.I)
            dest = re.search(r"\btaxi to (holding point [A-Z0-9]+|stand \w+|gate \w+|runway \d{2}[LRC]?|\d{2}[LRC]?)\b", t, re.I)
            rw = re.search(r"\brunway (\d{2}[LRC]?)\b", t, re.I)
            route = [x for x in re.split(r"[,\s]+", via.group(1)) if x and x.lower() not in ("and", "then")] if via else []
            info = {"route": route, "hold_short": hs.group(1) if hs else None,
                    "to": dest.group(1) if dest else None, "runway": rw.group(1) if rw else None,
                    "clearance": " ".join(route + ([f"HOLD SHORT {hs.group(1)}"] if hs else []))}
            if route or hs or dest:
                self.clearance["taxi"] = {"value": info, "text": t, "t": time.time()}
        elif re.search(r"\bhold short (?:of )?runway (\d{2}[LRC]?)", t, re.I):
            hs = re.search(r"\bhold short (?:of )?runway (\d{2}[LRC]?)", t, re.I)
            old = (self.clearance.get("taxi") or {}).get("value") or {}
            info = dict(old, hold_short=hs.group(1))
            self.clearance["taxi"] = {"value": info, "text": t, "t": time.time()}

    # ---------------- for the page ----------------
    def state(self, since=0):
        with self.lock:
            age = time.time() - self.mtime if self.mtime else None
            exists = self.sig is not None
            running = exists and age is not None and age < STALE_S and not self.closed
            if not exists:
                status = "no_log"
            elif not running:
                status = "not_running"
            elif self.recognised == 0 and self.offset > 200000:
                status = "unrecognised"
            elif self.xp == "connected":
                status = "connected"
            elif self.xp == "paused":
                status = "paused"
            else:
                status = "running"
            ev = [e for e in self.events if e["id"] > since][-400:]
            return {"status": status, "path": self.path(), "callsign": self.callsign, "controller": self.controller,
                    "radios": dict(self.radios), "clearance": {k: dict(v) for k, v in self.clearance.items()},
                    "atis": {k: dict(v) for k, v in self.atis.items()}, "events": ev,
                    "assigned": {k: dict(v) for k, v in self.assigned.items()},
                    "last_id": self.next_id - 1, "age_s": round(age, 1) if age is not None else None,
                    "last_atc": next((dict(e) for e in reversed(self.events) if e["kind"] == "atc"), None),
                    "session_t": self.session_t, "windows": os.name == "nt"}

    def frequencies(self):
        """The frequencies panel: your radios, the airports BeyondATC runs with their stations, the centres it has."""
        with self.lock:
            airports = dict(self.airports)
            rm = {k: set(v) for k, v in self.rm_rows.items()}
            heard = {k: dict(v, active=self._recent(v)) for k, v in self.heard.items()}
            staffed = {k: dict(v) for k, v in self.staffed.items()}
            dead = set(self.dead)
            centres = [dict(c) for c in self.centres.values()]
            radios = dict(self.radios)
            nxt = (self.clearance.get("next_freq") or {}).get("value")
            dep, arr = self.assigned["dep"].get("icao"), self.assigned["arr"].get("icao")
        rest = sorted((a for a in airports if a not in (dep, arr)), key=lambda a: (airports[a]["kind"] != "ops", -airports[a]["n"]))
        order = list(dict.fromkeys([a for a in (dep, arr) if a and a in airports] + rest))
        def same(who, icao, aname, sname):
            """A controller's name fits this airport's station (one frequency can serve several airports)."""
            if not who:
                return True
            w = who.lower()
            words = [x for x in re.split(r"[^a-z]+", f"{aname} {sname}".lower()) if len(x) > 3 and x not in
                     ("airport", "international", "intl", "ground", "tower", "approach", "departure", "departures", "delivery",
                      "clearance", "radio", "information", "centre", "center", "control")]
            return icao.lower() in w or any(x in w for x in words)
        def st(k, icao=None, aname="", sname=""):
            h = heard.get(k)
            if h and icao and not same(h.get("name"), icao, aname, sname):
                h = None
            sf = staffed.get(k)
            if sf and icao and not same(sf.get("name"), icao, aname, sname):
                sf = None
            return {"active": bool(h and h["active"]), "heard": bool(h), "staffed": bool(sf or h)}
        out, quiet = [], []
        for icao in order[:16]:
            info = airports[icao]
            try:
                fa = self.plugin.get_airport_freqs(icao) or {}
            except Exception:
                fa = {}
            rows = []
            for f in fa.get("freqs") or []:
                k = f"{f['khz'] / 1000:.3f}"
                if (f["type"], k) in rm.get(icao, set()):
                    continue
                rows.append(dict(st(k, icao, fa.get("name") or "", f["name"]), type=f["type"], name=f["name"], freq=k, khz=f["khz"]))
            if not any(r["type"] != "CTAF" for r in rows) and icao not in (dep, arr):
                quiet.append(icao)                      # no ATC there: just a CTAF (or nothing)
                continue
            out.append({"icao": icao, "name": fa.get("name") or icao, "kind": info["kind"],
                        "role": "dep" if icao == dep else ("arr" if icao == arr else None), "stations": rows})
        cs = []
        for c in centres:
            cs.append(dict(c, **st(c["freq"]), last=max((heard.get(c["freq"]) or {}).get("n", 0), c.get("n") or 0)))
        cs.sort(key=lambda c: (not c["active"], -c["last"]))
        def radio(f):
            if not f:
                return None
            k = self._fkey(f)
            name = (heard.get(k) or {}).get("name")
            if not name:
                for a in out:
                    name = next((r["name"] for r in a["stations"] if r["freq"] == k), None)
                    if name:
                        name = f"{a['icao']} {name}" if a["icao"] not in name.upper() else name
                        break
            if not name:
                name = next((c["name"] for c in cs if c["freq"] == k), None)
            return dict(st(k), freq=k, name=name, khz=int(round(float(k) * 1000)), dead=k in dead)
        return {"radios": {"com1": radio(radios.get("com1")), "com2": radio(radios.get("com2"))}, "next": nxt,
                "airports": out, "quiet": quiet, "centres": cs[:10]}

    def active(self):
        """BeyondATC is running and writing its log (used to prefer its ATIS)."""
        with self.lock:
            return bool(self.sig is not None and self.mtime and time.time() - self.mtime < STALE_S and not self.closed and self.xp is not None)

    def atis_for(self, icao):
        with self.lock:
            a = self.atis.get(icao)
            return dict(a) if a else None
