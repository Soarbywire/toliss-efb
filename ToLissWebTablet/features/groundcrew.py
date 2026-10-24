"""The ground crew's voice during the pushback, on X-Plane's radio.

Recorded lines (sounds/groundcrew/*.wav: mono or stereo, 16-bit PCM) are played through X-Plane's own sound system on
the pilot's radio bus, so X-Plane's radio volume controls them. Lines never overlap: each waits for the one before it.
A missing or unreadable file is skipped (noted once in the log). Everything here runs on X-Plane's main thread.
"""
from __future__ import annotations

import os
import time
import wave

try:
    import XPLMSound as _snd
except Exception:                      # older X-Plane / XPPython3 without the sound API: no voice, nothing else changes
    _snd = None

GC_LINES = {
    "connected": "01_connected",
    "push_left": "02_push_tail_left",
    "push_right": "03_push_tail_right",
    "push_straight": "04_push_straight",
    "pull_forward": "05_pull_forward",
    "stopping": "06_stopping",
    "holding": "07_holding",
    "continuing": "08_continuing",
    "complete": "09_complete",
    "brakes_set": "10_brakes_set",
    "disconnected": "11_disconnected",
    "cancelled": "12_cancelled",
}
GC_GAP = 0.35                          # s of quiet between two lines


def _const(name, default):
    return getattr(_snd, name, default) if _snd is not None else default


class GroundCrewMixin:
    def _gc(self):
        g = self.__dict__.get("_gc_state")
        if g is None:
            g = self.__dict__["_gc_state"] = {"queue": [], "busy_until": 0.0, "playing": None, "cache": {}, "warned": set()}
        return g

    def _gc_enabled(self):
        s = self.config.get("push_settings") if isinstance(getattr(self, "config", None), dict) else None
        return (s or {}).get("voice", True) is not False

    def _gc_say(self, key, delay=0.0):
        """Queue a line (played after any line before it, and not before <delay> seconds from now)."""
        if not self._gc_enabled() or key not in GC_LINES:
            return
        self._gc()["queue"].append((key, time.time() + delay))

    def _gc_free_at(self):
        """When the lines already said or queued will have finished (now if none): the push waits for the ground crew."""
        if not self._gc_enabled() or _snd is None:
            return 0.0
        g = self._gc()
        t = max(time.time(), g["busy_until"])
        for key, not_before in g["queue"]:
            clip = self._gc_load(key)
            if clip:
                t = max(t, not_before) + clip["secs"] + GC_GAP
        return t

    def _gc_clear(self):
        """Forget lines not yet played (a new push, or the push ended differently)."""
        self._gc()["queue"].clear()

    def _gc_load(self, key):
        g = self._gc()
        if key in g["cache"]:
            return g["cache"][key]
        path = os.path.join(self.plugin_dir, "sounds", "groundcrew", GC_LINES[key] + ".wav")
        clip = None
        try:
            with wave.open(path, "rb") as w:
                if w.getsampwidth() == 2 and w.getcomptype() == "NONE":
                    data = w.readframes(w.getnframes())
                    clip = {"data": data, "rate": w.getframerate(), "ch": w.getnchannels(),
                            "secs": w.getnframes() / float(w.getframerate() or 44100)}
        except Exception as e:
            if key not in g["warned"]:
                g["warned"].add(key)
                self.log.xplane(f"ToLiss EFB: ground crew line {GC_LINES[key]}.wav not played: {e}\n")
        if clip is None and key not in g["warned"]:
            g["warned"].add(key)
            self.log.xplane(f"ToLiss EFB: ground crew line {GC_LINES[key]}.wav is not 16-bit PCM WAV: skipped\n")
        g["cache"][key] = clip
        return clip

    def _gc_play(self, clip):
        if _snd is None or not clip:
            return False
        try:
            bus = _const("xplm_AudioRadioPilot", 2)
            fmt = _const("FMOD_SOUND_FORMAT_PCM16", 2)
            ch = _snd.XPLMPlayPCMOnBus(clip["data"], len(clip["data"]), fmt, clip["rate"], clip["ch"], 0, bus, None, None)
            self._gc()["playing"] = (ch, clip)        # keep the buffer alive while it plays
            return True
        except Exception as e:
            g = self._gc()
            if "play" not in g["warned"]:
                g["warned"].add("play")
                self.log.xplane(f"ToLiss EFB: ground crew voice could not play: {e}\n")
            return False

    def _gc_tick(self):
        g = self.__dict__.get("_gc_state")
        if not g or not g["queue"]:
            return
        now = time.time()
        if now < g["busy_until"]:
            return
        key, not_before = g["queue"][0]
        if now < not_before:
            return
        g["queue"].pop(0)
        clip = self._gc_load(key)
        if clip and self._gc_play(clip):
            g["busy_until"] = now + clip["secs"] + GC_GAP

    def _gc_stop(self):
        g = self.__dict__.get("_gc_state")
        if not g:
            return
        g["queue"].clear()
        p, g["playing"] = g["playing"], None
        if p and _snd is not None:
            try:
                _snd.XPLMStopAudio(p[0])
            except Exception:
                pass
