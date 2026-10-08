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

MCDU_COLS = 24

def mcdu_display_refs(side):
    p = f"AirbusFBW/MCDU{side}"
    refs = []   # (dataref, line, colour, small)
    for c in "bgswy":
        refs.append((f"{p}title{c}", 0, c, False))
    for c in "wy":
        refs.append((f"{p}stitle{c}", 0, c, True))
    for n in range(1, 7):
        for c in "abgwy":
            refs.append((f"{p}label{n}{c}", 2 * n - 1, c, True))
        refs.append((f"{p}label{n}Lg", 2 * n - 1, "g", False))
        for c in "abgmwy":
            refs.append((f"{p}scont{n}{c}", 2 * n, c, True))
        for c in "abgmswy":   # a "c" colour is listed by some references but ToLiss does not publish it
            refs.append((f"{p}cont{n}{c}", 2 * n, c, False))
    refs.append((f"{p}spw", 13, "w", False))
    refs.append((f"{p}spa", 13, "a", False))
    return refs

# Symbol-colour ("s") characters and what they draw
MCDU_SYMBOLS = {"A": ("[", "b"), "B": ("]", "b"), "0": ("\u2190", "b"), "1": ("\u2192", "b"),
                "2": ("\u2190", "w"), "3": ("\u2192", "w"), "4": ("\u2190", "a"), "5": ("\u2192", "a"),
                "E": ("\u2610", "a")}

MCDU_KEYS = set(["LSK%d%s" % (n, s) for n in range(1, 7) for s in "LR"] +
                ["DirTo", "Prog", "Perf", "Init", "Data", "Fpln", "RadNav", "FuelPred", "SecFpln", "ATC", "Menu", "Airport",
                 "SlewLeft", "SlewRight", "SlewUp", "SlewDown", "KeyDecimal", "KeyPM", "KeySlash", "KeySpace",
                 "KeyOverfly", "KeyClear", "KeyBright", "KeyDim"] +
                ["Key%d" % d for d in range(10)] + ["Key%s" % chr(c) for c in range(ord('A'), ord('Z') + 1)])

def mcdu_text_to_keys(text):
    keys = []
    for ch in str(text).upper():
        if "A" <= ch <= "Z" or "0" <= ch <= "9":
            keys.append("Key" + ch)
        elif ch == ".":
            keys.append("KeyDecimal")
        elif ch == "/":
            keys.append("KeySlash")
        elif ch == " ":
            keys.append("KeySpace")
        elif ch in "-+":
            keys.append("KeyPM")
    return keys
