"""Centre of gravity for the load sheet: MACTOW from ToLiss, MACZFW worked out from it.

ToLiss gives the current CG with everything on board (AirbusFBW/CGLocationPercent, % MAC). The zero-fuel CG is that
CG with the fuel taken out:

    W x CG  =  ZFW x ZFWCG  +  sum over tanks of (fuel in the tank x the tank's arm)

where an arm is the tank's position expressed in % MAC. (% MAC is a straight-line function of position, so these
weighted sums hold exactly.) The arms are not guessed: they are learned for each aircraft type from how ToLiss's CG
actually moves when the fuel changes while the payload stays the same (loading fuel, refuelling, or burning it in
flight). Each such change gives one equation,

    change in (W x CG)  =  sum over tanks of (change in fuel x arm)

and the arms are the least-squares fit of all of them. Tanks at the same position along the aircraft (left and
right wing) share one arm. The fit's error is checked against ToLiss's own CG; MACZFW is reported as confirmed
only when every tank holding fuel has been seen to change at least twice and the fit agrees within 0.15 % MAC,
otherwise it is marked EST (or left out if a tank holding fuel has never changed).
"""
from __future__ import annotations

import json
import math
import os
import threading
import time

READS = ["AirbusFBW/CGLocationPercent", "sim/flightmodel/weight/m_total", "sim/flightmodel/weight/m_fuel",
         "sim/aircraft/overflow/acf_tank_Z", "sim/aircraft/view/acf_ICAO"]
MIN_STEP_KG = 150.0          # a fuel change at least this big makes one equation
SAME_ZFW_KG = 30.0           # the payload must stay the same within this
MAX_ROWS = 300
CONFIRM_RMS = 0.15           # % MAC


def _solve(rows, groups):
    """Least squares for the arms: rows are (target, {group: dm}). Returns {group: arm}, or None if singular."""
    n = len(groups)
    if n == 0:
        return {}
    idx = {g: i for i, g in enumerate(groups)}
    ata = [[0.0] * n for _ in range(n)]
    atb = [0.0] * n
    for target, dm in rows:
        vec = [0.0] * n
        for g, v in dm.items():
            if g in idx:
                vec[idx[g]] = v
        for i in range(n):
            atb[i] += vec[i] * target
            for j in range(n):
                ata[i][j] += vec[i] * vec[j]
    scale = max(ata[i][i] for i in range(n)) or 1.0
    for i in range(n):
        ata[i][i] += scale * 1e-9                  # a whisker of ridge so exactly-collinear groups still solve
    # Gaussian elimination with partial pivoting
    m = [ata[i][:] + [atb[i]] for i in range(n)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[p][c]) < scale * 1e-12:
            return None
        m[c], m[p] = m[p], m[c]
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                for k in range(c, n + 1):
                    m[r][k] -= f * m[c][k]
    return {g: m[idx[g]][n] / m[idx[g]][idx[g]] for g in groups}


class CgTracker:
    def __init__(self, plugin):
        self.plugin = plugin
        self.path = os.path.join(plugin.plugin_dir, "cg_calibration.json")
        self.lock = threading.Lock()
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
            if not isinstance(self.data, dict):
                self.data = {}
        except Exception:
            self.data = {}
        self.why = "no reading yet"   # why there is no CG, if there is none
        self.last = None             # the sample the next fuel change is measured from
        self.now = None              # the latest sample
        self._last_status = {}

    # ---------- samples ----------
    def _read(self):
        v = self.plugin.bridge.call("read_datarefs", READS, timeout=1.5)
        cg = float(v.get(READS[0]) or 0.0)
        w = float(v.get(READS[1]) or 0.0)
        fuel = [float(x or 0.0) for x in (v.get(READS[2]) or [])]
        tz = [float(x or 0.0) for x in (v.get(READS[3]) or [])]
        icao = str(v.get(READS[4]) or "").strip().upper()
        if READS[0] in (v.get("__missing__") or []) or (cg == 0.0 and READS[0] not in v):
            self.why = "this aircraft has no AirbusFBW/CGLocationPercent"
            return None
        if not (5.0 < cg < 60.0) or w <= 0 or not fuel:
            self.why = f"the aircraft is not ready (CG {cg:.1f}, weight {w:.0f} kg)"
            return None                                  # not a ToLiss aircraft (or not ready)
        self.why = ""
        # tanks at the same position share one arm (left and right wing); without positions, each tank is its own
        groups = [("z%.1f" % tz[i]) if i < len(tz) and any(tz) else f"t{i}" for i in range(len(fuel))]
        return {"t": time.time(), "cg": cg, "w": w, "fuel": fuel, "groups": groups, "icao": icao or "ACF",
                "f": sum(fuel), "zfw": w - sum(fuel)}

    def sample(self):
        """Read the sim once and learn from it if the fuel has changed with the same payload."""
        s = self._read()
        if s is None:
            return None
        with self.lock:
            self.now = s
            last = self.last
            if last is None or last["icao"] != s["icao"] or abs(s["zfw"] - last["zfw"]) > SAME_ZFW_KG:
                self.last = s                            # first sample, another aircraft, or the payload changed
                return s
            if abs(s["f"] - last["f"]) < MIN_STEP_KG:
                return s                                 # wait for a bigger change (slow burn adds up)
            dm = {}
            for i, g in enumerate(s["groups"]):
                d = s["fuel"][i] - (last["fuel"][i] if i < len(last["fuel"]) else 0.0)
                if abs(d) > 1.0:
                    dm[g] = dm.get(g, 0.0) + d
            target = s["w"] * s["cg"] - last["w"] * last["cg"]
            ac = self.data.setdefault(s["icao"], {"rows": []})
            ac["rows"] = (ac.get("rows", []) + [[target, dm, s["w"]]])[-MAX_ROWS:]
            self.last = s
            self._fit(s["icao"])
        self._save()
        return s

    def _fit(self, icao):
        ac = self.data.get(icao) or {}
        rows = [(r[0], r[1]) for r in ac.get("rows", [])]
        weights = [r[2] for r in ac.get("rows", [])]
        groups = sorted({g for _, dm in rows for g in dm})
        arms = _solve(rows, groups) if rows else None
        if arms is None:
            return
        res = []
        for (target, dm), w in zip(rows, weights):
            pred = sum(v * arms.get(g, 0.0) for g, v in dm.items())
            res.append((target - pred) / (w or 1.0))     # the error in % MAC at that weight
        ac["arms"] = arms
        ac["rms"] = math.sqrt(sum(x * x for x in res) / len(res)) if res else None
        ac["seen"] = {g: sum(1 for _, dm in rows if abs(dm.get(g, 0.0)) >= MIN_STEP_KG / 2) for g in groups}

    def _save(self):
        try:
            with self.lock:
                text = json.dumps(self.data)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, self.path)
        except Exception:
            pass

    # ---------- results ----------
    def figures(self, fresh=True, burn=None):
        """{"tow_cg", "zfw_cg", "zfw_status": confirmed/est/none, "law_cg", "rms", "rows", "why"} from the latest
        sample. burn: the fuel used before landing (taxi + trip), for the landing CG."""
        s = None
        if fresh:
            try:
                s = self.sample()
            except Exception:
                s = None
        with self.lock:
            s = s or self.now
            if s is None:
                return {"tow_cg": None, "zfw_cg": None, "zfw_status": "none", "law_cg": None, "why": self.why}
            ac = self.data.get(s["icao"]) or {}
            arms, seen, rms = ac.get("arms") or {}, ac.get("seen") or {}, ac.get("rms")
            out = {"tow_cg": round(s["cg"], 1), "zfw_cg": None, "zfw_status": "none", "law_cg": None, "rms": rms,
                   "rows": len(ac.get("rows", [])), "icao": s["icao"], "why": ""}
            held = {}
            for i, g in enumerate(s["groups"]):
                if s["fuel"][i] > 1.0:
                    held[g] = held.get(g, 0.0) + s["fuel"][i]
            if s["zfw"] <= 0:
                return out
            if not held:
                out.update(zfw_cg=round(s["cg"], 1), zfw_status="confirmed")   # no fuel: the CG is the ZFW CG
                return out
            if any(g not in arms for g in held):
                return out                                # a tank holding fuel has never been seen to change
            zcg = (s["w"] * s["cg"] - sum(m * arms[g] for g, m in held.items())) / s["zfw"]
            if not (5.0 < zcg < 60.0):
                return out
            ok = rms is not None and rms <= CONFIRM_RMS and all(seen.get(g, 0) >= 2 for g in held)
            out.update(zfw_cg=round(zcg, 1), zfw_status="confirmed" if ok else "est")
            if burn is not None and burn > 0:
                out["law_cg"] = self._landing_cg(s, held, arms, zcg, float(burn))
        self._note(out)
        return out

    @staticmethod
    def _landing_cg(s, held, arms, zcg, burn):
        """The CG at landing: the fuel burnt in the standard Airbus order, the centre tank (and any single tank, such
        as extra tanks feeding it) first, then the wing tanks evenly."""
        count = {}
        for g in s["groups"]:
            count[g] = count.get(g, 0) + 1
        land = dict(held)
        single = [g for g in land if count.get(g, 1) == 1]
        paired = [g for g in land if count.get(g, 1) > 1]
        left = burn
        for g in single:
            use = min(left, land[g]); land[g] -= use; left -= use
        wings = sum(land[g] for g in paired)
        if left > 0 and wings > 0:
            k = max(0.0, (wings - left) / wings)
            for g in paired:
                land[g] *= k
        fuel = sum(land.values())
        lw = s["zfw"] + fuel
        if lw <= 0:
            return None
        c = (s["zfw"] * zcg + sum(m * arms[g] for g, m in land.items())) / lw
        return round(c, 1) if 5.0 < c < 60.0 else None

    def _note(self, out):
        if self._last_status.get(out.get("icao")) != out.get("zfw_status"):
            self._last_status[out.get("icao")] = out.get("zfw_status")
            rms = out.get("rms")
            try:
                self.plugin.log.xplane(
                    f"ToLiss EFB: ZFW CG for {out.get('icao')}: {out.get('zfw_status')} "
                    f"({out.get('rows')} fuel changes learned" + (f", fit within {rms:.2f} % MAC" if rms is not None else "") + ")\n")
            except Exception:
                pass

    def needs_learning(self):
        """True until this aircraft type's ZFW CG is confirmed (used to load fuel in steps, which teaches it)."""
        with self.lock:
            s = self.now
            ac = self.data.get(s["icao"]) if s else None
            return not ac or len(ac.get("rows", [])) < 3 or (ac.get("rms") or 1.0) > CONFIRM_RMS

    # ---------- learning in the background ----------
    def run(self, stop_event):
        while not stop_event.wait(5.0):
            if getattr(self.plugin, "asleep", False):
                continue
            try:
                self.sample()
            except Exception:
                pass
