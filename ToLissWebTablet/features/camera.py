"""Camera views from the EFB (IOS > Replay > Camera), mostly for watching a replay.

Only X-Plane's own view commands are used: the built-in views, the orbit/zoom controls and the pilot's saved Quick
Looks. Commands are looked up once; views this X-Plane version lacks are reported to the page, which hides them.

Replay extras, run here in the flight loop so they keep working whichever device (or none) has the page open:
- auto-cycle: change view every N seconds through the chosen views, while X-Plane is in replay;
- touchdown camera: in replay, switch to the runway view when the aircraft descends through 150 ft above the ground,
  and back to the previous view once it has slowed down on the runway (or climbs away);
- jump to the landing: enter replay, go to the end and rewind fast until the aircraft is airborne about 400 ft above
  the ground before the last landing in X-Plane's recording, then pause there (optionally in the runway view).
  The aircraft's replayed position and height are read from X-Plane, so no replay clock is needed.
Everything here runs on X-Plane's main thread (bridge request and flight loop).
"""
from __future__ import annotations

import time

from XPLMUtilities import *

VIEWS = [  # (key, label, command)
    ("cockpit", "Cockpit", "sim/view/3d_cockpit_cmnd_look"),
    ("chase", "Chase", "sim/view/chase"),
    ("circle", "Orbit", "sim/view/circle"),
    ("tower", "Tower", "sim/view/tower"),
    ("runway", "Runway", "sim/view/runway"),
    ("spot", "Fly-by", "sim/view/still_spot"),
    ("linear", "Moving spot", "sim/view/linear_spot"),
    ("free", "Free camera", "sim/view/free_camera"),
]
MOVES = {"left": "sim/general/left", "right": "sim/general/right", "up": "sim/general/up", "down": "sim/general/down",
         "in": "sim/general/zoom_in", "out": "sim/general/zoom_out"}
QUICK = [(i, f"sim/view/quick_look_{i}") for i in range(10)]
NUDGE_S = 0.25
TD_ARM_FT, TD_BACK_KT = 150.0, 40.0
JUMP_FT, JUMP_TIMEOUT = 400.0, 240.0


class CameraMixin:
    def _cam(self):
        return self.__dict__.setdefault("camera", {"current": None, "cycle": [], "interval": 20, "cycle_next": 0.0, "cycle_i": 0,
                                                   "td": False, "td_state": None, "td_prev": None, "jump": None,
                                                   "holds": [], "message": ""})

    def _cam_cmd(self, name):
        cache = self.__dict__.setdefault("_cam_cmd_cache", {})
        if name not in cache:
            try:
                cache[name] = XPLMFindCommand(name) or None
            except Exception:
                cache[name] = None
        return cache[name]

    def _cam_view(self, key):
        cmd = dict((k, c) for k, _, c in VIEWS).get(key)
        ref = self._cam_cmd(cmd) if cmd else None
        if not ref:
            return False
        XPLMCommandOnce(ref)
        self._cam()["current"] = key
        return True

    def _cam_state(self):
        c = self._cam()
        j = c.get("jump")
        return {"views": [{"key": k, "label": l} for k, l, cmd in VIEWS if self._cam_cmd(cmd)],
                "moves": [k for k, cmd in MOVES.items() if self._cam_cmd(cmd)],
                "quick": [i for i, cmd in QUICK if self._cam_cmd(cmd)],
                "current": c["current"], "cycle": list(c["cycle"]), "interval": c["interval"], "td": c["td"],
                "jumping": bool(j), "jump_phase": j.get("phase") if j else None, "message": c.get("message", "")}

    def _camera_request(self, item):
        c = self._cam()
        cmd = str(item.get("cmd") or "state")
        if cmd == "state":
            return {"ok": True, "message": c.get("message", ""), "state": self._cam_state()}
        if cmd != "jump_landing":
            c["message"] = ""
        if cmd == "view":
            ok = self._cam_view(str(item.get("view") or ""))
            c["message"] = "" if ok else "This X-Plane version does not have that view."
            return {"ok": ok, "message": c["message"], "state": self._cam_state()}
        if cmd == "move":
            ref = self._cam_cmd(MOVES.get(str(item.get("dir") or ""), ""))
            if not ref:
                return {"ok": False, "message": "This X-Plane version cannot move the view that way.", "state": self._cam_state()}
            XPLMCommandBegin(ref)
            c["holds"].append((ref, time.time() + NUDGE_S))
            return {"ok": True, "state": self._cam_state()}
        if cmd == "quick":
            try:
                i = int(item.get("n"))
            except (TypeError, ValueError):
                i = -1
            ref = self._cam_cmd(f"sim/view/quick_look_{i}") if 0 <= i <= 19 else None
            if not ref:
                return {"ok": False, "message": "That Quick Look does not exist in this X-Plane version.", "state": self._cam_state()}
            XPLMCommandOnce(ref)
            c["current"] = f"q{i}"
            return {"ok": True, "state": self._cam_state()}
        if cmd == "settings":
            keys = {k for k, _, _ in VIEWS}
            if isinstance(item.get("cycle"), list):
                c["cycle"] = [k for k in item["cycle"] if k in keys or (isinstance(k, str) and k.startswith("q") and k[1:].isdigit())][:12]
                c["cycle_i"], c["cycle_next"] = 0, time.time() + 1.0
            if item.get("interval") is not None:
                try:
                    c["interval"] = max(5, min(120, int(item["interval"])))
                except (TypeError, ValueError):
                    pass
            if item.get("td") is not None:
                c["td"], c["td_state"] = bool(item["td"]), None
            return {"ok": True, "state": self._cam_state()}
        if cmd == "jump_landing":
            if getattr(self, "push", None) or getattr(self, "slew", None):
                return {"ok": False, "message": "End the pushback or slew first.", "state": self._cam_state()}
            if not (self._replay_cmd("end") and self._replay_cmd("fast_rev") and self._replay_cmd("pause")):
                return {"ok": False, "message": "This X-Plane version lacks the replay commands needed.", "state": self._cam_state()}
            if not self._in_replay() and self._replay_cmd("toggle"):
                XPLMCommandOnce(self._replay_cmd("toggle"))
            c["jump"] = {"phase": "enter", "t0": time.time(), "frames": 4, "seen_ground": False, "runway": bool(item.get("runway", True))}
            c["message"] = "Finding the landing in X-Plane's replay..."
            return {"ok": True, "message": c["message"], "state": self._cam_state()}
        if cmd == "jump_cancel":
            c["jump"] = None
            if self._in_replay() and self._replay_cmd("pause"):
                XPLMCommandOnce(self._replay_cmd("pause"))
            c["message"] = "Stopped."
            return {"ok": True, "message": c["message"], "state": self._cam_state()}
        return {"ok": False, "message": "Unknown camera request.", "state": self._cam_state()}

    def _camera_tick(self):
        c = self.__dict__.get("camera")
        if not c:
            return
        now = time.time()
        if c["holds"]:
            keep = []
            for ref, until in c["holds"]:
                if now >= until:
                    XPLMCommandEnd(ref)
                else:
                    keep.append((ref, until))
            c["holds"] = keep
        if c.get("jump"):
            self._camera_jump_tick(c, now)
            return
        in_replay = self._in_replay()
        # touchdown camera: the runway view for the landing, then back
        if c["td"] and in_replay:
            agl = self._get_f("sim/flightmodel/position/y_agl") * 3.28084
            gnd = self._get_f("sim/flightmodel/failures/onground_any") >= 1
            gs = self._get_f("sim/flightmodel/position/groundspeed") * 1.94384
            vs = self._get_f("sim/flightmodel/position/vh_ind_fpm")
            st = c["td_state"]
            if st is None and not gnd and agl < TD_ARM_FT and vs < -100:
                c["td_prev"] = c["current"]
                if self._cam_view("runway"):
                    c["td_state"] = "on"
            elif st == "on" and ((gnd and gs < TD_BACK_KT) or (not gnd and agl > TD_ARM_FT * 3)):
                prev = c.get("td_prev")
                if prev and not str(prev).startswith("q"):
                    self._cam_view(prev)
                elif prev:
                    ref = self._cam_cmd(f"sim/view/quick_look_{prev[1:]}")
                    if ref:
                        XPLMCommandOnce(ref)
                        c["current"] = prev
                c["td_state"] = "done" if gnd else None
            elif st == "done" and not gnd and agl > TD_ARM_FT * 3:
                c["td_state"] = None
            if c["td_state"] == "on":
                return                        # no auto-cycling during the landing
        elif not in_replay:
            c["td_state"] = None
        # auto-cycle while in replay
        if c["cycle"] and in_replay and now >= c["cycle_next"]:
            key = c["cycle"][c["cycle_i"] % len(c["cycle"])]
            c["cycle_i"] += 1
            c["cycle_next"] = now + c["interval"]
            if key.startswith("q"):
                ref = self._cam_cmd(f"sim/view/quick_look_{key[1:]}")
                if ref:
                    XPLMCommandOnce(ref)
                    c["current"] = key
            else:
                self._cam_view(key)

    def _camera_jump_tick(self, c, now):
        j = c["jump"]
        if now - j["t0"] > JUMP_TIMEOUT:
            c["jump"], c["message"] = None, "Could not find a landing in X-Plane's replay in time."
            if self._replay_cmd("pause"):
                XPLMCommandOnce(self._replay_cmd("pause"))
            return
        if j["phase"] == "enter":
            j["frames"] -= 1
            if j["frames"] > 0 or (not self._in_replay() and now - j["t0"] < 3.0):
                return
            XPLMCommandOnce(self._replay_cmd("end"))
            j["phase"], j["frames"] = "end", 6
            return
        if j["phase"] == "end":
            j["frames"] -= 1
            if j["frames"] > 0:
                return
            XPLMCommandOnce(self._replay_cmd("fast_rev"))
            j["phase"], j["last_move"], j["last_pos"] = "rewind", now, None
            return
        if j["phase"] == "rewind":
            agl = self._get_f("sim/flightmodel/position/y_agl") * 3.28084
            gnd = self._get_f("sim/flightmodel/failures/onground_any") >= 1
            pos = (round(self._get_f("sim/flightmodel/position/latitude"), 5), round(self._get_f("sim/flightmodel/position/longitude"), 5))
            if pos != j.get("last_pos"):
                j["last_pos"], j["last_move"] = pos, now
            elif now - j["last_move"] > 6.0:
                # nothing moves any more: the start of the recording, without a landing in it
                c["jump"], c["message"] = None, "No landing found in X-Plane's replay (it keeps the last part of the flight only)."
                XPLMCommandOnce(self._replay_cmd("pause"))
                return
            if gnd:
                j["seen_ground"] = True
            elif j["seen_ground"] and agl >= JUMP_FT:
                XPLMCommandOnce(self._replay_cmd("pause"))
                if j.get("runway"):
                    self._cam_view("runway")
                c["jump"] = None
                c["message"] = "At the landing: about 30 s before touchdown. Press Play."
