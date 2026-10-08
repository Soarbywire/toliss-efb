"""Replay controls: X-Plane's own replay, driven from the EFB (IOS > Replay).

The EFB only sends X-Plane's replay commands; X-Plane records and plays back. Commands are looked up when first
needed, and any that this X-Plane version lacks are reported to the page, which hides those buttons.
Everything here runs on X-Plane's main thread (bridge request and flight loop).
"""
from __future__ import annotations

import time

from XPLMUtilities import *

REPLAY_CMDS = {
    "toggle": "sim/replay/replay_toggle",
    "start": "sim/replay/rep_begin",
    "fast_rev": "sim/replay/rep_play_fr",
    "rev": "sim/replay/rep_play_rr",
    "slow_rev": "sim/replay/rep_play_sr",
    "pause": "sim/replay/rep_pause",
    "slow_fwd": "sim/replay/rep_play_sf",
    "play": "sim/replay/rep_play_rf",
    "fast_fwd": "sim/replay/rep_play_ff",
    "end": "sim/replay/rep_end",
}
REPLAY_LABELS = {"start": "At the start", "fast_rev": "Fast reverse", "rev": "Reverse", "slow_rev": "Slow reverse",
                 "pause": "Paused", "slow_fwd": "Slow forward", "play": "Playing", "fast_fwd": "Fast forward", "end": "At the end"}
REPLAY_STATE_REFS = ("sim/time/is_in_replay", "sim/operation/prefs/replay_mode")


class ReplayMixin:
    def _replay_cmd(self, key):
        cache = self.__dict__.setdefault("_replay_cmd_cache", {})
        if key not in cache:
            try:
                cache[key] = XPLMFindCommand(REPLAY_CMDS[key]) or None
            except Exception:
                cache[key] = None
        return cache[key]

    def _in_replay(self):
        return any(self._dref(n) is not None and self._get_f(n) >= 1 for n in REPLAY_STATE_REFS)

    def _replay_state(self):
        rp = self.__dict__.setdefault("replay", {"mode": None, "pending": None})
        on = self._in_replay()
        if not on and not rp.get("pending"):
            rp["mode"] = None
        known = any(self._dref(n) is not None for n in REPLAY_STATE_REFS)
        return {"in_replay": on, "state_known": known, "mode": rp.get("mode"),
                "mode_text": ("Replay: " + REPLAY_LABELS.get(rp.get("mode"), "on")) if on else "Real-time (live)",
                "available": [k for k in REPLAY_CMDS if self._replay_cmd(k)],
                "message": rp.get("message", "")}

    def _replay_request(self, item):
        rp = self.__dict__.setdefault("replay", {"mode": None, "pending": None})
        cmd = str(item.get("cmd") or "state")
        if cmd == "state":
            return {"ok": True, "state": self._replay_state()}
        on = self._in_replay()
        if not on and cmd != "exit" and (getattr(self, "push", None) or getattr(self, "slew", None)):
            return {"ok": False, "message": "End the pushback or slew first.", "state": self._replay_state()}
        if cmd == "exit":                     # Exit to Real-Time: leave replay, back to the live flight
            if not on:
                rp["mode"], rp["message"] = None, "Already in real-time."
                return {"ok": True, "message": rp["message"], "state": self._replay_state()}
            if not self._replay_cmd("toggle"):
                return {"ok": False, "message": "This X-Plane version has no replay toggle command.", "state": self._replay_state()}
            XPLMCommandOnce(self._replay_cmd("toggle"))
            rp["mode"], rp["pending"], rp["message"] = None, None, "Exited to real-time."
            return {"ok": True, "message": rp["message"], "state": self._replay_state()}
        if cmd == "toggle":
            if not self._replay_cmd("toggle"):
                return {"ok": False, "message": "This X-Plane version has no replay toggle command.", "state": self._replay_state()}
            XPLMCommandOnce(self._replay_cmd("toggle"))
            rp["mode"] = None if on else "pause"
            rp["message"] = "Replay off." if on else "Replay on."
            return {"ok": True, "message": rp["message"], "state": self._replay_state()}
        if cmd not in REPLAY_CMDS:
            return {"ok": False, "message": "Unknown replay command.", "state": self._replay_state()}
        ref = self._replay_cmd(cmd)
        if not ref:
            return {"ok": False, "message": f"This X-Plane version has no '{REPLAY_CMDS[cmd]}' command.", "state": self._replay_state()}
        if not on and self._replay_cmd("toggle"):
            # enter replay first; the transport command follows a few frames later, once X-Plane is in replay
            XPLMCommandOnce(self._replay_cmd("toggle"))
            rp["pending"] = {"cmd": cmd, "frames": 3, "until": time.time() + 2.0}
        else:
            XPLMCommandOnce(ref)
        rp["mode"] = cmd
        rp["message"] = ""
        return {"ok": True, "message": REPLAY_LABELS.get(cmd, ""), "state": self._replay_state()}

    def _replay_tick(self):
        rp = self.__dict__.get("replay")
        if not rp or not rp.get("pending"):
            return
        p = rp["pending"]
        p["frames"] -= 1
        if p["frames"] > 0:
            return
        known = any(self._dref(n) is not None for n in REPLAY_STATE_REFS)
        if known and not self._in_replay() and time.time() < p["until"]:
            return                            # not in replay yet: wait (up to 2 s)
        rp["pending"] = None
        ref = self._replay_cmd(p["cmd"])
        if ref:
            XPLMCommandOnce(ref)
