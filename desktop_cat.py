#!/usr/bin/env python3
"""
desktop_cat.py - Desktop Pet: a tiny pixel pet that wanders your screen, reminds you to take breaks,
fetches files for you and clears out junk in Cobra mode.

What it does
------------
* A borderless, transparent, always-on-top window holds a pixel-art cat that idles,
  walks left/right along the bottom of your screen and naps now and then.
* An activity tracker watches mouse/keyboard input. After 5 minutes with no input
  you count as "away" and the continuous-session counter resets (or pauses, see
  idle_policy in Config).
* After 40 minutes of sitting at the screen without a break, the cat stops, hops in
  place and tells you to get up and walk. Any break of 5+ minutes resets the count.
* Daily routine: a sip of water every 30 minutes of screen time, lunch at 1:00 pm and
  "let's go home" at 5:45 pm (Config.water_every_s / lunch_time / home_time, or
  --water-minutes / --lunch / --home). Lunch and home show once a day. If you're away
  at that moment they show when you return (within 90 min). Each has a "Later" button.
* Click the cat or the walk bubble to acknowledge: the 40-minute timer resets and the cat
  goes back to wandering. The bubble's "x" only snoozes the reminder for a few
  minutes without resetting the timer.
* Double-click the cat and it asks what to do:
  - Fetch a file: type part of a name and results appear instantly from an in-memory
    index of every file name on your fixed drives.
  - Cobra mode: shows how much is in the Recycle Bin and in temp/cache folders, and on
    "Strike!" empties the bin and deletes cache files untouched for 24 h+. In-use and
    locked files are skipped. Nothing is deleted without that confirmation.

Running it
----------
    pip install PyQt6            # or: pip install PySide6
    python desktop_cat.py        # normal mode (40 min sitting limit, 5 min idle)
    python desktop_cat.py --demo # fast timers: alert after 30 s, idle after 20 s
    python desktop_cat.py --install-shortcut   # Desktop + Start menu shortcuts with a cat icon

To pin to the taskbar, right-click the "Desktop Pet" shortcut > Pin to taskbar (Windows
does not allow apps to pin themselves). Launching it again while it runs makes the
existing cat say hello instead of starting a second cat.

Other options: --alert-minutes N, --idle-minutes N, --idle-policy reset|pause, --scale N,
--index PATH (repeatable; index these folders instead of all fixed drives).
Right-click the cat (or the tray icon) for status, "Fetch a file", "Cobra mode",
"Test alert now", "Reset timer", "Rebuild file index" and "Quit". Drag the cat with the
left mouse button to move it. Ctrl+C in the terminal also quits.

Fetching files (how the index works)
------------------------------------
Index file *names* only, not contents, and search them as you type.
* Index: every name sits in one packed lowercase byte blob, with arrays of offsets and
  parent-folder numbers. There are no per-file Python objects, so a million files take
  tens of MB, and a search is a few C-speed bytes.find() calls over the blob.
* Database: the index is saved to %LOCALAPPDATA%\\DesktopCat\\file_index.bin, so
  startup is instant. It is rebuilt in the background when older than
  index_max_age_min.
* Real-time updates: ReadDirectoryChangesW watches each drive root recursively
  (no admin needed). Creates, deletes and renames are applied live.
* Search syntax:  words = AND (all must match),  !word = NOT,  "exact phrase",
  ext:pdf;docx,  file: / folder:,  wild*cards?,  terms with \\ match the full path.
* In the popup: Enter / double-click = go to the location (a file is selected in its
  folder, never opened; a folder is opened), Shift+Enter = copy path, Ctrl+Enter = open
  the file itself, Up/Down = select, Esc = close. You can also drag a result into any app.
* By default Windows, ProgramData, AppData, recycle bins, node_modules, .git and
  similar noisy folders are skipped (see Config.index_exclude_*).

Typeface
--------
Text uses the first available typeface in FONT_CHOICES (installed, or as a .ttf/.otf in a
"fonts" folder next to this script), falling back to system sans-serif fonts.

Liquid-glass bubbles
--------------------
Glass windows sample the screen behind themselves, blur and slightly magnify it, then
add a tint, sheen and bright rim. On Windows 10 2004+ they exclude themselves from
screen capture so they can keep re-sampling live while visible (side effect: bubbles do
not appear in screenshots or screen shares). Elsewhere they sample once when shown.

Idle detection uses the Win32 GetLastInputInfo API on Windows (mouse + keyboard).
On macOS/Linux it falls back to polling the mouse cursor position (mouse only).
"""

from __future__ import annotations

import argparse
import ast
import bisect
import ctypes
import fnmatch
import json
import math
import os
import random
import re
import signal
import stat
import struct
import subprocess
import sys
import threading
import time
from array import array
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

# --- Qt binding shim: PyQt6 preferred, PySide6 as fallback -----------------------
try:
    from PyQt6 import QtCore, QtGui, QtNetwork, QtWidgets

    Signal = QtCore.pyqtSignal
except ImportError:  # pragma: no cover - depends on what is installed
    from PySide6 import QtCore, QtGui, QtNetwork, QtWidgets

    Signal = QtCore.Signal

Qt = QtCore.Qt


SEP = os.sep  # "\\" on Windows, "/" on macOS


def _default_exclude_paths() -> tuple[str, ...]:
    if sys.platform == "darwin":
        paths = [str(Path.home() / "Library"), str(Path.home() / ".Trash")]
    else:
        paths = [os.environ.get("SystemRoot", r"C:\Windows"), os.environ.get("ProgramData", r"C:\ProgramData")]
    return tuple(p.lower().rstrip(SEP) for p in paths if p)


# =================================================================================
# Configuration
# =================================================================================
@dataclass
class Config:
    # --- Health timers (seconds) ---
    alert_after_s: int = 40 * 60       # max continuous sitting (screen time) before "get up and walk"
    walk_on: bool = True               # False = no sitting reminder at all (Settings > Routines)
    idle_threshold_s: int = 5 * 60     # no input for this long = user is away
    idle_policy: str = "reset"         # "reset": away resets session; "pause": away only pauses it
    snooze_s: int = 5 * 60             # "x" on the alert bubble hides it for this long
    alert_rotate_s: int = 45           # pick a new reminder text this often while alerting

    # --- Daily routine (first-run defaults; after that, settings.json holds the list) ---
    water_every_s: int = 30 * 60       # "sip of water" after this much screen time (pauses while away)
    water_later_s: int = 10 * 60       # "Later" on an every-N-minutes bubble asks again after this
    lunch_time: str = "13:00"          # 24 h clock
    home_time: str = "17:45"
    clock_later_s: int = 10 * 60       # "10 more min" on a set-time bubble
    clock_window_min: int = 90         # if you're away at that time, it still shows when you return within this
    routine_days: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)  # Mon=0 ... Sun=6; drop days you don't work

    # --- Saved settings ---
    pet: str = "cat"                   # a key of PETS
    settings_path: str = ""            # empty = settings.json in the app data folder
    overrides: tuple[str, ...] = ()    # settings fixed by command-line flags; nothing is saved while any are set

    # --- Timer intervals (milliseconds) ---
    tracker_interval_ms: int = 1000    # how often activity is polled
    move_interval_ms: int = 40         # movement tick (~25 FPS)
    frame_interval_ms: int = 180       # sprite animation frame step
    glass_refresh_ms: int = 150        # how often glass windows re-sample the screen
    search_debounce_ms: int = 90       # pause after a keystroke before searching

    # --- Pet behaviour ---
    behaviour_min_s: float = 3.0       # min time before picking a new wander behaviour
    behaviour_max_s: float = 8.0       # max time before picking a new wander behaviour
    chatter_min_s: int = 4 * 60        # random small talk interval (min)
    chatter_max_s: int = 9 * 60        # random small talk interval (max)
    walk_speed_px: int = 2             # pixels per movement tick
    pixel_scale: int = 5               # size of one sprite "pixel" on screen

    # --- File fetch (name index) ---
    index_roots: tuple[str, ...] = ()  # empty = every fixed drive
    index_exclude_names: tuple[str, ...] = (
        "$recycle.bin", "system volume information", "appdata", "node_modules", ".git",
        "__pycache__", ".venv", "venv", "$windows.~bt", "$windows.~ws", "$winreagent",
    )
    index_exclude_paths: tuple[str, ...] = field(default_factory=_default_exclude_paths)
    index_max_age_min: int = 60        # cached index older than this is rebuilt in the background
    fetch_max_results: int = 7         # rows shown in the fetch popup
    apps_refresh_min: int = 30         # re-read the Start menu app list this often
    search_candidates: int = 3000      # name matches examined per search before ranking

    # --- Cobra mode (Recycle Bin + temp/cache cleanup) ---
    cobra_min_age_h: int = 24          # only delete temp items untouched for this long (in-use files are newer)
    cobra_extra_dirs: tuple[str, ...] = ()  # more cache folders to clear, beyond the defaults
    cobra_fight_min_s: float = 2.5     # the cat-vs-cobra fight lasts at least this long, even if cleanup is instant

    # --- Fetch trip (cat runs off-screen and brings the item back before opening it) ---
    fetch_trip: bool = True            # False = open/go to the location instantly
    fetch_trip_leg_s: float = 1.0      # each way (out to the screen edge, and back)
    fetch_trip_pause_s: float = 0.5    # time spent "finding" it out of sight

    @classmethod
    def demo(cls) -> "Config":
        """Fast timers so the whole flow can be seen in under a minute."""
        return cls(alert_after_s=30, idle_threshold_s=20, snooze_s=15, alert_rotate_s=10,
                   chatter_min_s=20, chatter_max_s=40, water_every_s=45, water_later_s=20)


# Shown (rotating) when continuous screen time hits alert_after_s.
WALK_REMINDERS = [
    "🚶 Stand up and take a short walk. A few minutes is enough.",
    "🚶 Your back called: it wants a walk. Get off the chair for 5 minutes.",
    "🚶 Walk break: around the office, to the window, anywhere but this chair.",
    "🚶 Stretch your legs. The screen will still be here in five minutes.",
]

DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def default_routines(cfg: "Config") -> list[dict]:
    """The starting routine list. kind "every" = after N minutes of screen time; kind
    "time" = once a day at a clock time. {every} and {time} in a message fill themselves in."""
    days = list(cfg.routine_days)
    return [
        {"id": "water", "title": "💧 Sip of water", "message": "Another {every} on screen. Have a few sips.",
         "kind": "every", "minutes": cfg.water_every_s / 60, "time": "09:00", "days": days, "on": True,
         "ok": "Done 💧", "reply": "Good human. 💧"},
        {"id": "lunch", "title": "🍱 Lunch break!",
         "message": "It's {time}. Step away from the screen and eat something proper.",
         "kind": "time", "minutes": 30, "time": cfg.lunch_time, "days": days, "on": True,
         "ok": "Going 🍱", "reply": "Enjoy your lunch! 🍱 I'll guard the desk."},
        {"id": "home", "title": "🏠 Let's go home!", "message": "It's {time}. Save your work and head out.",
         "kind": "time", "minutes": 30, "time": cfg.home_time, "days": days, "on": True,
         "ok": "Heading out 👋", "reply": "See you tomorrow! 👋🐾"},
        {"id": "eyes", "title": "👀 Eye break",
         "message": "Look at something about 6 m (20 ft) away for 20 seconds.",
         "kind": "every", "minutes": 20, "time": "09:00", "days": days, "on": True,
         "ok": "Done 👀", "reply": "Fresh eyes! 👀"},
    ]


# Everything else the Settings window saves (settings.json "options").
DEFAULT_OPTIONS = {
    "theme": "dark",            # "dark" or "light" glass
    "sounds": False,            # a short sound per pet for reminders
    "quiet_fullscreen": True,   # hide and stay quiet while a full-screen app or presentation runs
    "perch": True,              # hop onto the top of the active window now and then (Windows)
    "hotkeys": True,            # Ctrl+Alt+Space / V / N (Windows)
    "clipboard": True,          # remember copied text for this session (never written to disk)
    "seasonal": True,           # winter hat, night cap, birthday party hat
    "birthday": "",             # "MM-DD"
    "companion": "",            # a second pet's key, or "" for none
    "colours": {},              # pet key -> PET_COLOURS name
    "focus_min": 25,
    "break_min": 5,
}


def clean_routine(raw) -> dict | None:
    """A routine read from settings.json with every field checked, or None if it's unusable."""
    try:
        kind = raw["kind"]
        hhmm = str(raw.get("time") or "09:00")
        fmt_clock(hhmm)  # raises ValueError on a bad time
        minutes = float(raw.get("minutes") or 30)
        days = sorted({int(d) for d in raw.get("days", range(7)) if 0 <= int(d) <= 6})
        if kind not in ("every", "time") or not str(raw["id"]) or not str(raw["title"]).strip() or minutes <= 0:
            return None
        return {"id": str(raw["id"]), "title": str(raw["title"]), "message": str(raw.get("message", "")),
                "kind": kind, "minutes": minutes, "time": hhmm, "days": days, "on": bool(raw.get("on", True)),
                "ok": str(raw.get("ok") or "Done ✅"), "reply": str(raw.get("reply") or "Nice work! 🐾")}
    except (KeyError, TypeError, ValueError):
        return None


def routine_when(routine: dict) -> str:
    """'every 30 min' or '1:00 pm', plus the days unless it runs every day."""
    if routine["kind"] == "every":
        when = f"every {fmt_span(routine['minutes'] * 60)}"
    else:
        when = fmt_clock(routine["time"])
    days = routine["days"]
    if len(days) == 7:
        return when
    if days == [0, 1, 2, 3, 4]:
        return f"{when}, Mon–Fri"
    if days == [5, 6]:
        return f"{when}, weekends"
    return f"{when}, {' '.join(DAY_NAMES[d] for d in days)}"


CHATTER = [
    "I'm keeping an eye on your screen time. 👀",
    "Don't forget to blink!",
    "Double-click me and I'll fetch a file for you. 🐾",
]

# --- Calculator and unit conversion (typed into the Fetch box) ----------------------
_CALC_NAMES = {"pi": math.pi, "e": math.e, "tau": math.tau}
_CALC_FUNCS = {"sqrt": math.sqrt, "sin": math.sin, "cos": math.cos, "tan": math.tan, "log": math.log10,
               "ln": math.log, "abs": abs, "round": round, "floor": math.floor, "ceil": math.ceil, "exp": math.exp}
_CALC_OPS = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b,
             ast.Div: lambda a, b: a / b, ast.FloorDiv: lambda a, b: a // b, ast.Mod: lambda a, b: a % b}


def safe_calc(text: str) -> float | None:
    """Evaluate plain arithmetic ("12*7", "=2^10", "sqrt(2)", "15% of 80") without eval().
    Returns None for anything that isn't a sum, so ordinary searches are left alone."""
    expr = text.strip().lstrip("=").strip().lower()
    expr = re.sub(r"(\d+(?:\.\d+)?)\s*%\s*of\s*", r"(\1/100)*", expr)
    expr = re.sub(r"(?<=\d),(?=\d{3}\b)", "", expr)  # 1,000 -> 1000
    if not re.fullmatch(r"[\d\s.+\-*/^()×÷%,a-z]*", expr):
        return None
    expr = re.sub(r"(?<=[\d)])\s*x\s*(?=[\d(])", "*", expr)  # 3 x 4, but not the x in exp()
    expr = expr.replace("^", "**").replace("×", "*").replace("÷", "/")
    if not expr or not re.search(r"\d", expr):
        return None
    if not re.search(r"[+\-*/%(]|\*\*", expr) and not re.match(r"[a-z]+\(", expr):
        return None  # just a number or a word: a search, not a sum
    try:
        tree = ast.parse(expr, mode="eval")
    except (SyntaxError, ValueError):
        return None

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.Name) and node.id in _CALC_NAMES:
            return _CALC_NAMES[node.id]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            v = ev(node.operand)
            return -v if isinstance(node.op, ast.USub) else v
        if isinstance(node, ast.BinOp):
            a, b = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow):
                if abs(b) > 100 or abs(a) > 1e6:
                    raise ValueError("too big")
                return a ** b
            if type(node.op) in _CALC_OPS:
                return _CALC_OPS[type(node.op)](a, b)
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _CALC_FUNCS
                and not node.keywords and 1 <= len(node.args) <= 2):
            return _CALC_FUNCS[node.func.id](*(ev(a) for a in node.args))
        raise ValueError("not arithmetic")

    try:
        value = ev(tree)
    except (ValueError, TypeError, ZeroDivisionError, OverflowError):
        return None
    if isinstance(value, complex) or value != value or value in (float("inf"), float("-inf")):
        return None
    return float(value)


def fmt_number(value: float) -> str:
    """12.0 -> '12', 3.14159265 -> '3.141593', 1234567 -> '1234567'."""
    if abs(value - round(value)) < 1e-9 and abs(value) < 1e15:
        return str(int(round(value)))
    return f"{value:.6g}" if abs(value) >= 1e6 or abs(value) < 1e-4 else f"{value:.6f}".rstrip("0").rstrip(".")


# unit -> (category, size in the category's base unit)
_UNITS: dict[str, tuple[str, float]] = {}
for _cat, _base, _names in (
    ("length", 1.0, {"mm millimeter millimeters millimetre millimetres": 0.001, "cm centimeter centimeters centimetre centimetres": 0.01,
                     "m meter meters metre metres": 1, "km kilometer kilometers kilometre kilometres": 1000,
                     "in inch inches": 0.0254, "ft foot feet": 0.3048, "yd yard yards": 0.9144, "mi mile miles": 1609.344}),
    ("mass", 1.0, {"mg milligram milligrams": 1e-6, "g gram grams": 0.001, "kg kilo kilos kilogram kilograms": 1,
                   "t tonne tonnes": 1000, "lb lbs pound pounds": 0.45359237, "oz ounce ounces": 0.028349523125,
                   "st stone stones": 6.35029318}),
    ("volume", 1.0, {"ml milliliter milliliters millilitre millilitres": 0.001, "l liter liters litre litres": 1,
                     "gal gallon gallons": 3.785411784, "qt quart quarts": 0.946352946, "pt pint pints": 0.473176473,
                     "cup cups": 0.2365882365, "floz": 0.0295735295625, "tsp teaspoon teaspoons": 0.00492892159375,
                     "tbsp tablespoon tablespoons": 0.01478676478125}),
    ("data", 1.0, {"b byte bytes": 1, "kb kilobyte kilobytes": 1e3, "mb megabyte megabytes": 1e6,
                   "gb gigabyte gigabytes": 1e9, "tb terabyte terabytes": 1e12, "kib": 1024, "mib": 1024 ** 2,
                   "gib": 1024 ** 3, "tib": 1024 ** 4}),
    ("time", 1.0, {"ms millisecond milliseconds": 0.001, "s sec secs second seconds": 1, "min mins minute minutes": 60,
                   "h hr hrs hour hours": 3600, "d day days": 86400, "wk week weeks": 604800}),
    ("speed", 1.0, {"m/s mps": 1, "km/h kmh kph": 1000 / 3600, "mph": 0.44704, "knot knots kn": 1852 / 3600}),
    ("area", 1.0, {"m2 sqm": 1, "km2": 1e6, "ft2 sqft": 0.09290304, "acre acres": 4046.8564224, "ha hectare hectares": 1e4}),
):
    for _group, _size in _names.items():
        for _n in _group.split():
            _UNITS[_n] = (_cat, _size * _base)
_TEMPS = {"c": "c", "°c": "c", "celsius": "c", "f": "f", "°f": "f", "fahrenheit": "f", "k": "k", "kelvin": "k"}


def convert_units(text: str) -> tuple[float, str] | None:
    """'5 km in miles' -> (3.10686, 'mi'); also 'to' or 'as'. None if it isn't a conversion."""
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*([a-z°/0-9]+)\s+(?:in|to|as|=)\s+([a-z°/0-9]+)\s*", text.lower())
    if not m:
        return None
    value, src, dst = float(m.group(1)), m.group(2), m.group(3)
    if src in _TEMPS and dst in _TEMPS:
        a, b = _TEMPS[src], _TEMPS[dst]
        celsius = {"c": value, "f": (value - 32) * 5 / 9, "k": value - 273.15}[a]
        out = {"c": celsius, "f": celsius * 9 / 5 + 32, "k": celsius + 273.15}[b]
        return out, {"c": "°C", "f": "°F", "k": "K"}[b]
    if src in _UNITS and dst in _UNITS and _UNITS[src][0] == _UNITS[dst][0]:
        return value * _UNITS[src][1] / _UNITS[dst][1], dst
    return None


# --- Quick notes: "call bank at 4pm" becomes a reminder; anything else is a note --------
def parse_note(text: str, now: datetime) -> tuple[datetime | None, str]:
    """Find a time in the text and return (when, the text without it). Understands
    'in 20 min', 'in 2 hours', 'in 1 h 30 min', 'at 4pm', 'at 4:30 pm', 'at 16:30',
    '4pm', and 'tomorrow'. A time that has already passed today means tomorrow."""
    rest = " " + text.strip() + " "
    when = None
    tomorrow = re.search(r"\s(tomorrow|tmrw)\s", rest, re.I)
    if tomorrow:
        rest = rest[:tomorrow.start()] + " " + rest[tomorrow.end():]
    rel = re.search(r"\sin\s+(?:(\d+(?:\.\d+)?)\s*(?:h|hr|hrs|hour|hours))?\s*(?:(\d+)\s*(?:m|min|mins|minute|minutes))?\s",
                    rest, re.I)
    if rel and (rel.group(1) or rel.group(2)):
        when = now + timedelta(hours=float(rel.group(1) or 0), minutes=int(rel.group(2) or 0))
        rest = rest[:rel.start()] + " " + rest[rel.end():]
    else:
        clock = (re.search(r"\s(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s", rest, re.I)
                 or re.search(r"\sat\s+(\d{1,2})(?::(\d{2}))?()\s", rest, re.I))
        if clock:
            h, m, ampm = int(clock.group(1)), int(clock.group(2) or 0), (clock.group(3) or "").lower()
            if h <= 23 and m <= 59 and not (ampm and not 1 <= h <= 12):
                if ampm:
                    h = h % 12 + (12 if ampm == "pm" else 0)
                when = now.replace(hour=h, minute=m, second=0, microsecond=0)
                if tomorrow:
                    when += timedelta(days=1)
                elif when <= now:
                    if not ampm and h < 12 and when + timedelta(hours=12) > now:
                        when += timedelta(hours=12)  # "at 4" said at 2 pm means 4 pm
                    else:
                        when += timedelta(days=1)
                rest = rest[:clock.start()] + " " + rest[clock.end():]
    if when is None and tomorrow:
        when = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    rest = re.sub(r"\s+", " ", rest).strip(" ,.;:-")
    return when, rest or text.strip()


# Preferred UI typefaces as (family, italic), first match wins.
FONT_CHOICES = [("Redaction 50", True),
                ("Segoe UI Variable Text", False), ("Segoe UI", False), ("Helvetica Neue", False), ("Arial", False)]


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def fmt_span(seconds: float) -> str:
    """Round, friendly spans for settings: 2400 -> '40 min', 7200 -> '2 h', 5400 -> '1 h 30 min'."""
    minutes = round(seconds / 60)
    if minutes < 1:
        return f"{int(seconds)} s"
    h, m = divmod(minutes, 60)
    return " ".join(part for part in (f"{h} h" if h else "", f"{m} min" if m else "") if part)


def fmt_clock(hhmm: str) -> str:
    """'13:00' -> '1:00 pm'."""
    return datetime.strptime(hhmm, "%H:%M").strftime("%I:%M %p").lstrip("0").lower()


def fmt_count(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n // 1000}k"
    return f"{n:,}"


@dataclass
class UIFont:
    """The app typeface. make() builds sized fonts with the right italic setting."""
    family: str
    italic: bool = False

    def make(self, size: float, weight=QtGui.QFont.Weight.Normal) -> QtGui.QFont:
        font = QtGui.QFont(self.family)
        font.setPointSizeF(size)
        font.setWeight(weight)
        font.setItalic(self.italic)
        return font


def load_ui_font() -> UIFont:
    """Register any fonts bundled in ./fonts, then return the first preferred family available."""
    font_dir = Path(__file__).resolve().parent / "fonts"
    if font_dir.is_dir():
        for path in sorted(font_dir.glob("*.[ot]tf")):
            QtGui.QFontDatabase.addApplicationFont(str(path))
    available = set(QtGui.QFontDatabase.families())
    for family, italic in FONT_CHOICES:
        if family in available:
            return UIFont(family, italic)
    return UIFont(QtWidgets.QApplication.font().family())


# =================================================================================
# Activity tracking
# =================================================================================
class IdleDetector:
    """Reports seconds since the last mouse/keyboard input."""

    def __init__(self) -> None:
        self._use_win32 = sys.platform == "win32"
        if self._use_win32:
            class LASTINPUTINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint32)]

            self._lii = LASTINPUTINFO()
            self._lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
            self._user32 = ctypes.windll.user32
            self._kernel32 = ctypes.windll.kernel32
            self._kernel32.GetTickCount.restype = ctypes.c_uint32
        self._quartz_idle = self._load_quartz_idle()
        # Fallback state: cursor polling
        self._last_pos = None
        self._last_move = time.monotonic()

    @staticmethod
    def _load_quartz_idle():
        """macOS: seconds since any mouse/keyboard event (needs no permissions)."""
        if sys.platform != "darwin":
            return None
        try:
            lib = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
            fn = lib.CGEventSourceSecondsSinceLastEventType
            fn.restype = ctypes.c_double
            fn.argtypes = [ctypes.c_int32, ctypes.c_uint32]
            return fn
        except (OSError, AttributeError):
            return None

    def idle_seconds(self) -> float:
        if self._use_win32 and self._user32.GetLastInputInfo(ctypes.byref(self._lii)):
            # Both values are 32-bit millisecond tick counts; mask handles the 49-day wrap.
            elapsed_ms = (self._kernel32.GetTickCount() - self._lii.dwTime) & 0xFFFFFFFF
            return elapsed_ms / 1000.0

        if self._quartz_idle is not None:
            return float(self._quartz_idle(1, 0xFFFFFFFF))  # combined session state, any input event

        # Cross-platform fallback: only mouse movement counts as activity.
        pos = QtGui.QCursor.pos()
        now = time.monotonic()
        if pos != self._last_pos:
            self._last_pos = pos
            self._last_move = now
        return now - self._last_move


class ActivityTracker(QtCore.QObject):
    """Counts continuous active time and raises the break alert."""

    ticked = Signal(float, float)        # (session_seconds, idle_seconds)
    alert_triggered = Signal()
    user_away = Signal()
    user_returned = Signal()

    def __init__(self, cfg: Config) -> None:
        super().__init__()
        self.cfg = cfg
        self.detector = IdleDetector()
        self.session_s = 0.0             # continuous active time
        self.active_s = 0.0              # total active time, never reset (drives every-N-minutes routines)
        self.away = False
        self.alert_active = False
        self._last_poll = time.monotonic()

        # Poll timer: every tracker_interval_ms we read idle time and update the session.
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(cfg.tracker_interval_ms)
        self._timer.timeout.connect(self.poll)

    def start(self) -> None:
        self._last_poll = time.monotonic()
        self._timer.start()

    def poll(self) -> None:
        now = time.monotonic()
        dt = now - self._last_poll       # real elapsed time, not the nominal interval
        self._last_poll = now
        idle = self.detector.idle_seconds()

        # A huge dt means the machine slept / the app was frozen: treat it as away too.
        if idle >= self.cfg.idle_threshold_s or dt >= self.cfg.idle_threshold_s:
            if not self.away:
                self.away = True
                if self.cfg.idle_policy == "pause":
                    # The idle stretch was counted as active while it was happening; undo it.
                    self.session_s = max(0.0, self.session_s - idle)
                else:
                    self.session_s = 0.0
                # Being away for 5+ minutes *is* a break, so any pending alert is cleared.
                self.alert_active = False
                self.user_away.emit()
        else:
            if self.away:
                self.away = False
                self.user_returned.emit()
            self.session_s += dt
            self.active_s += dt
            if self.cfg.walk_on and not self.alert_active and self.session_s >= self.cfg.alert_after_s:
                self.alert_active = True
                self.alert_triggered.emit()

        self.ticked.emit(self.session_s, idle)

    def acknowledge(self) -> None:
        """User confirmed the break reminder: start a fresh sitting window."""
        self.session_s = 0.0
        self.alert_active = False

    def force_alert(self) -> None:
        """Preview: jump the session to the alert threshold (works even with the reminder off)."""
        self.session_s = max(self.session_s, float(self.cfg.alert_after_s))
        if not self.alert_active:
            self.alert_active = True
            self.alert_triggered.emit()


# =================================================================================
# File index (names only, packed in memory, updated live)
# =================================================================================
FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
# Junctions and symlinks are not followed (they create loops and duplicates). Other
# reparse points ARE walked: cloud-sync folders are reparse points too.
SKIP_REPARSE_TAGS = (0xA0000003, 0xA000000C)  # IO_REPARSE_TAG_MOUNT_POINT, IO_REPARSE_TAG_SYMLINK
CACHE_MAGIC = b"DCATIDX2"
# Results inside these rank first...
USER_CONTENT = tuple(s.replace("\\", SEP) for s in (
    "\\desktop\\", "\\documents\\", "\\downloads\\", "\\onedrive", "\\pictures\\", "\\videos\\", "\\music\\"))
# ...and paths containing these rank last.
NOISE_SEGMENTS = tuple(s.replace("\\", SEP) for s in (
    "\\appdata\\", "\\program files", "\\windows\\", "\\$recycle", "\\node_modules\\",
    "\\site-packages\\", "\\temp\\", "\\cache", "\\vendor\\", "\\registry\\", "packagecache",
    "pip_prebundle", "extscache", "\\dist\\", "\\build\\", "\\target\\", "\\."))


@dataclass
class Hit:
    path: str          # for apps: the .lnk path or "shell:AppsFolder\<AppID>"
    name: str
    is_dir: bool
    kind: str = "file"  # "file" (files and folders), "app", "calc" (an answer) or "clip" (clipboard text)
    note: str = ""      # second line override (e.g. "📌 Pinned", "Calculator · ↵ copy")


class NameTable:
    """Compact name table. Every name lives in one lowercase blob (searched) and one
    original-case blob (displayed), separated by '\\n'. Parallel arrays hold each
    entry's offsets, its parent entry and a directory flag. Full paths are rebuilt
    on demand by walking parents."""

    def __init__(self) -> None:
        self.lower = bytearray(b"\n")
        self.orig = bytearray(b"\n")
        self.lower_off = array("I")
        self.orig_off = array("I")
        self.parent = array("i")
        self.is_dir = bytearray()

    def __len__(self) -> int:
        return len(self.parent)

    def add(self, name: str, parent: int, is_dir: bool) -> int:
        self.lower_off.append(len(self.lower))
        self.lower += name.lower().encode("utf-8", "surrogatepass") + b"\n"
        self.orig_off.append(len(self.orig))
        self.orig += name.encode("utf-8", "surrogatepass") + b"\n"
        self.parent.append(parent)
        self.is_dir.append(1 if is_dir else 0)
        return len(self.parent) - 1

    def name(self, i: int) -> str:
        start = self.orig_off[i]
        return self.orig[start:self.orig.index(b"\n", start)].decode("utf-8", "surrogatepass")

    def lname(self, i: int) -> str:
        start = self.lower_off[i]
        return self.lower[start:self.lower.index(b"\n", start)].decode("utf-8", "surrogatepass")

    def path(self, i: int, removed=(), memo: dict | None = None) -> str | None:
        """Full path of entry i, or None if it or any parent was deleted (tombstoned)."""
        memo = {} if memo is None else memo
        chain = []
        base = None
        j = i
        while j >= 0:
            if j in removed:
                return None
            if j in memo:
                base = memo[j]
                break
            chain.append(j)
            j = self.parent[j]
        for k in reversed(chain):
            part = self.name(k)
            base = part if base is None else base + SEP + part
            memo[k] = base
        return base

    def find(self, needle: bytes):
        """Yield entries whose lowercase name contains needle (C-speed scan of the blob)."""
        blob, offsets, pos = self.lower, self.lower_off, 1
        while True:
            pos = blob.find(needle, pos)
            if pos < 0:
                return
            yield bisect.bisect_right(offsets, pos) - 1
            pos = blob.find(b"\n", pos) + 1  # skip to the next name

    def lookup(self, path_l: str) -> int | None:
        """Entry index for an exact lowercase full path, if present."""
        name = path_l.rstrip(SEP).rsplit(SEP, 1)[-1].encode("utf-8", "surrogatepass")
        blob, offsets, pos = self.lower, self.lower_off, 0
        needle = b"\n" + name + b"\n"
        while True:
            pos = blob.find(needle, pos)
            if pos < 0:
                return None
            i = bisect.bisect_right(offsets, pos + 1) - 1
            p = self.path(i)
            if p is not None and p.lower() == path_l:
                return i
            pos += 1

    # --- on-disk database ----------------------------------------------------------
    def save(self, path: Path, meta: dict) -> None:
        parts = [self.lower, self.orig, self.lower_off.tobytes(), self.orig_off.tobytes(),
                 self.parent.tobytes(), self.is_dir]
        header = json.dumps({**meta, "sizes": [len(p) for p in parts]}).encode()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            f.write(CACHE_MAGIC + struct.pack("<I", len(header)) + header)
            for part in parts:
                f.write(part)
        os.replace(tmp, path)

    @classmethod
    def load(cls, path: Path) -> tuple["NameTable", dict] | None:
        try:
            with open(path, "rb") as f:
                if f.read(len(CACHE_MAGIC)) != CACHE_MAGIC:
                    return None
                (hlen,) = struct.unpack("<I", f.read(4))
                meta = json.loads(f.read(hlen))
                raw = [f.read(n) for n in meta["sizes"]]
        except (OSError, ValueError, KeyError):
            return None
        table = cls()
        table.lower, table.orig = bytearray(raw[0]), bytearray(raw[1])
        for arr, data in ((table.lower_off, raw[2]), (table.orig_off, raw[3]), (table.parent, raw[4])):
            arr.frombytes(data)
        table.is_dir = bytearray(raw[5])
        return table, meta


class Query:
    """Parses search text: AND terms, !NOT, "phrases", ext:, file:/folder:,
    wildcards, and path terms containing a backslash."""

    def __init__(self, text: str) -> None:
        self.terms: list[str] = []
        self.path_terms: list[str] = []
        self.negs: list[str] = []
        self.exts: tuple[str, ...] = ()
        self.kind = None
        self.patterns: list[re.Pattern] = []
        fragments = []
        for tok in re.findall(r'!?"[^"]*"|\S+', text):
            neg = tok.startswith("!")
            tok = (tok[1:] if neg else tok).strip('"').lower().replace("/", SEP).replace("\\", SEP)
            if not tok:
                continue
            if tok.startswith("ext:"):
                self.exts += tuple("." + e.lstrip(".") for e in tok[4:].split(";") if e)
            elif tok in ("file:", "folder:", "app:"):
                self.kind = tok[:-1]
            elif neg:
                self.negs.append(tok)
            elif "*" in tok or "?" in tok:
                self.patterns.append(re.compile(fnmatch.translate(tok)))
                fragments.append(max(re.split(r"[*?]", tok), key=len))
            elif SEP in tok:
                self.path_terms.append(tok)
                fragments.append(tok.rstrip(SEP).rsplit(SEP, 1)[-1])
            else:
                self.terms.append(tok)
        # The "driver" is the longest literal that must appear in the name; it is what we
        # scan the blob for. Every other condition is checked on the candidates.
        candidates = [c for c in self.terms + fragments if c]
        if candidates:
            self.driver = max(candidates, key=len)
        elif self.exts:
            self.driver = max(self.exts, key=len)
        else:
            self.driver = ""

    def name_ok(self, name_l: str, is_dir: bool) -> bool:
        if self.kind == "file" and is_dir or self.kind == "folder" and not is_dir:
            return False
        if self.driver not in name_l:
            return False
        if self.exts and not name_l.endswith(self.exts):
            return False
        return all(p.match(name_l) for p in self.patterns)

    def path_ok(self, path_l: str) -> bool:
        return (all(t in path_l for t in self.terms) and all(t in path_l for t in self.path_terms)
                and not any(t in path_l for t in self.negs))

    def rank(self, name_l: str, path_l: str) -> tuple:
        d = self.driver
        stem = name_l.rsplit(".", 1)[0]
        if name_l == d or stem == d:
            quality = 0
        elif name_l.startswith(d):
            quality = 1
        elif re.search(r"(^|[^a-z0-9])" + re.escape(d), name_l):
            quality = 2
        else:
            quality = 3
        noise = sum(s in path_l for s in NOISE_SEGMENTS)  # "\\." = any dot-folder (.git, .cargo...)
        not_user_content = not any(s in path_l for s in USER_CONTENT)
        return (noise, quality, not_user_content, path_l.count(SEP), len(name_l))


class _DirWatcher(threading.Thread):
    """Blocking ReadDirectoryChangesW loop on one drive root (recursive, no admin needed)."""

    def __init__(self, root: str, on_event, on_overflow) -> None:
        super().__init__(daemon=True, name=f"watch {root}")
        self.root, self.on_event, self.on_overflow = root, on_event, on_overflow

    def run(self) -> None:
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                    wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.ReadDirectoryChangesW.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, wintypes.BOOL,
                                              wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                              wintypes.LPVOID, wintypes.LPVOID]
        FILE_LIST_DIRECTORY, SHARE_ALL, OPEN_EXISTING, BACKUP_SEMANTICS = 0x1, 0x7, 3, 0x02000000
        NOTIFY_NAMES = 0x1 | 0x2  # FILE_NAME | DIR_NAME
        handle = k32.CreateFileW(self.root, FILE_LIST_DIRECTORY, SHARE_ALL, None, OPEN_EXISTING,
                                 BACKUP_SEMANTICS, None)
        if not handle or handle == ctypes.c_void_p(-1).value:
            return
        buf = ctypes.create_string_buffer(256 * 1024)
        got = wintypes.DWORD()
        while k32.ReadDirectoryChangesW(handle, buf, len(buf), True, NOTIFY_NAMES, ctypes.byref(got), None, None):
            if got.value == 0:
                self.on_overflow()  # too many changes at once: the buffer was dropped
                continue
            data, off = buf.raw[:got.value], 0
            while True:
                nxt, action, length = struct.unpack_from("<III", data, off)
                name = data[off + 12:off + 12 + length].decode("utf-16-le", "surrogatepass")
                try:
                    self.on_event(action, os.path.join(self.root, name))
                except Exception:  # never let one bad event kill the watcher
                    pass
                if not nxt:
                    break
                off += nxt


class background_io:
    """Run the current thread at background CPU *and disk* priority (Windows), so big
    scans and deletes always yield to File Explorer and other apps."""

    BEGIN, END = 0x00010000, 0x00020000  # THREAD_MODE_BACKGROUND_BEGIN / _END

    def __enter__(self):
        self._on = False
        if sys.platform == "win32":
            k32 = ctypes.windll.kernel32
            k32.GetCurrentThread.restype = ctypes.c_void_p
            k32.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
            self._on = bool(k32.SetThreadPriority(k32.GetCurrentThread(), self.BEGIN))
        return self

    def __exit__(self, *exc) -> None:
        if self._on:
            k32 = ctypes.windll.kernel32
            k32.SetThreadPriority(k32.GetCurrentThread(), self.END)


def app_data_dir() -> Path:
    """Where the file index and reminder state live."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "DesktopCat"
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / ".cache")) / "DesktopCat"


def default_index_roots() -> list[str]:
    if sys.platform != "win32":
        return [str(Path.home())]
    k32 = ctypes.windll.kernel32
    mask = k32.GetLogicalDrives()
    DRIVE_FIXED = 3
    return [f"{chr(65 + i)}:\\" for i in range(26)
            if mask & (1 << i) and k32.GetDriveTypeW(f"{chr(65 + i)}:\\") == DRIVE_FIXED]


class FileIndex(QtCore.QObject):
    """Background-built, live-updated index of file names on the configured roots."""

    progress = Signal(int)   # entries scanned so far
    changed = Signal()       # a new index is ready

    ACTION_ADDED, ACTION_REMOVED, ACTION_RENAMED_OLD, ACTION_RENAMED_NEW = 1, 2, 4, 5

    def __init__(self, cfg: Config, cache_path: Path | None = None) -> None:
        super().__init__()
        self.cfg = cfg
        self.roots = list(cfg.index_roots) or default_index_roots()
        self.cache_path = cache_path or app_data_dir() / "file_index.bin"
        self.table: NameTable | None = None
        self.scanning = False
        self.scanned = 0
        self.updated_at = 0.0
        self._lock = threading.Lock()          # guards the live overlay below
        self._scan_lock = threading.Lock()     # one scan at a time
        self._added: dict[str, tuple[str, bool]] = {}  # lower path -> (path, is_dir)
        self._removed_idx: set[int] = set()    # tombstoned table entries
        self._removed_paths: set[str] = set()  # same, by path (re-applied after a rescan)
        self._exclude_names = set(cfg.index_exclude_names)
        self._exclude_paths = tuple(p.lower().rstrip(SEP) for p in cfg.index_exclude_paths)

    # --- lifecycle -----------------------------------------------------------------
    def start(self) -> None:
        threading.Thread(target=self._bootstrap, daemon=True, name="file index").start()

    def rescan(self) -> None:
        threading.Thread(target=self._scan_and_swap, daemon=True, name="file rescan").start()

    def _bootstrap(self) -> None:
        loaded = NameTable.load(self.cache_path)
        if loaded and loaded[1].get("roots") == self.roots:
            self.table, meta = loaded
            self.updated_at = meta.get("time", 0.0)
            self.changed.emit()
        if sys.platform == "win32":
            for root in self.roots:
                _DirWatcher(root, self._on_fs_event, self.rescan).start()
        if self.table is None or time.time() - self.updated_at > self.cfg.index_max_age_min * 60:
            self._scan_and_swap()

    # --- full scan -----------------------------------------------------------------
    def _excluded(self, path_l: str, name_l: str) -> bool:
        return name_l in self._exclude_names or path_l in self._exclude_paths

    def _scan_and_swap(self) -> None:
        if not self._scan_lock.acquire(blocking=False):
            return
        try:
            with background_io():
                self._scan()
        finally:
            self.scanning = False
            self._scan_lock.release()

    def _scan(self) -> None:
        self.scanning = True
        table = NameTable()
        next_report = 0
        if self.table is None:
            self.table = table  # first run: let searches see partial results while scanning
        for root in self.roots:
            stack = [(root, table.add(root.rstrip(SEP), -1, True))]
            while stack:
                folder, parent = stack.pop()
                try:
                    it = os.scandir(folder)
                except OSError:
                    continue  # access denied etc.
                with it:
                    for entry in it:
                        try:
                            st = entry.stat(follow_symlinks=False)  # free on Windows (from the dir listing)
                            attrs = getattr(st, "st_file_attributes", None)
                            if attrs is None:  # non-Windows
                                is_dir = entry.is_dir(follow_symlinks=False)
                                walk = is_dir
                            else:
                                is_dir = bool(attrs & FILE_ATTRIBUTE_DIRECTORY)
                                walk = is_dir and not (attrs & FILE_ATTRIBUTE_REPARSE_POINT
                                                       and st.st_reparse_tag in SKIP_REPARSE_TAGS)
                        except OSError:
                            continue
                        idx = table.add(entry.name, parent, is_dir)
                        if walk and not self._excluded(entry.path.lower(), entry.name.lower()):
                            stack.append((entry.path, idx))
                self.scanned = len(table)
                if self.scanned >= next_report:
                    next_report = self.scanned + 20000
                    self.progress.emit(self.scanned)
        with self._lock:
            self.table = table
            found = {p: table.lookup(p) for p in self._removed_paths}
            self._removed_paths = {p for p, i in found.items() if i is not None}
            self._removed_idx = {i for i in found.values() if i is not None}
            self._added = {k: v for k, v in self._added.items() if table.lookup(k) is None}
        self.updated_at = time.time()
        self.scanning = False
        self.changed.emit()
        try:
            table.save(self.cache_path, {"roots": self.roots, "time": self.updated_at})
        except OSError:
            pass

    # --- live updates (watcher threads) --------------------------------------------
    def _path_excluded(self, path_l: str) -> bool:
        if path_l.startswith(self._exclude_paths) or path_l.startswith(str(self.cache_path.parent).lower()):
            return True
        return any(part in self._exclude_names for part in path_l.split(SEP)[:-1])

    def _on_fs_event(self, action: int, path: str) -> None:
        path_l = path.lower()
        if self._path_excluded(path_l):
            return
        if action in (self.ACTION_ADDED, self.ACTION_RENAMED_NEW):
            self._add_live(path)
        elif action in (self.ACTION_REMOVED, self.ACTION_RENAMED_OLD):
            self._remove_live(path_l)

    def _add_live(self, path: str) -> None:
        is_dir = os.path.isdir(path)
        with self._lock:
            self._added[path.lower()] = (path, is_dir)
            self._removed_paths.discard(path.lower())
        if is_dir:  # a folder moved in brings its contents; the watcher only reports the folder
            count = 0
            for folder, dirs, files in os.walk(path):
                with self._lock:
                    for n in dirs:
                        self._added[os.path.join(folder, n).lower()] = (os.path.join(folder, n), True)
                    for n in files:
                        self._added[os.path.join(folder, n).lower()] = (os.path.join(folder, n), False)
                count += len(dirs) + len(files)
                if count > 50000:
                    break

    def _remove_live(self, path_l: str) -> None:
        with self._lock:
            if self._added.pop(path_l, None) is not None:
                prefix = path_l + SEP
                for key in [k for k in self._added if k.startswith(prefix)]:
                    del self._added[key]
                return
            self._removed_paths.add(path_l)
        table = self.table
        idx = table.lookup(path_l) if table is not None else None
        if idx is not None:
            with self._lock:
                self._removed_idx.add(idx)  # children disappear too: path() checks ancestors

    # --- search --------------------------------------------------------------------
    def count(self) -> int:
        table = self.table
        return (len(table) if table is not None else 0) + len(self._added)

    def search(self, text: str, limit: int) -> list[Hit]:
        q = Query(text)
        if not q.driver or q.kind == "app" or limit <= 0:
            return []
        scored: dict[str, tuple] = {}
        table = self.table
        with self._lock:
            removed = set(self._removed_idx)
            overlay = list(self._added.values())
        if table is not None:
            memo: dict[int, str] = {}
            examined = 0
            for i in table.find(q.driver.encode("utf-8", "surrogatepass")):
                is_dir = bool(table.is_dir[i])
                if not q.name_ok(table.lname(i), is_dir):
                    continue
                examined += 1
                if examined > self.cfg.search_candidates:
                    break
                path = table.path(i, removed, memo)
                if path is None:
                    continue
                path_l = path.lower()
                if q.path_ok(path_l):
                    scored[path_l] = (q.rank(path_l.rsplit(SEP, 1)[-1], path_l), path, is_dir)
        for path, is_dir in overlay:
            path_l = path.lower()
            name_l = path_l.rsplit(SEP, 1)[-1]
            if q.name_ok(name_l, is_dir) and q.path_ok(path_l):
                scored[path_l] = (q.rank(name_l, path_l), path, is_dir)
        best = sorted(scored.values(), key=lambda s: s[0])[:limit]
        return [Hit(path, path.rsplit(SEP, 1)[-1] or path, is_dir) for _, path, is_dir in best]


# --- Installed apps ------------------------------------------------------------------
CREATE_NO_WINDOW = 0x08000000  # keep helper PowerShell windows from flashing under pythonw


def start_menu_dirs() -> list[str]:
    env = os.environ
    rel = os.path.join("Microsoft", "Windows", "Start Menu", "Programs")
    return [os.path.join(env[v], rel) for v in ("ProgramData", "APPDATA") if env.get(v)]


def mac_app_bundles() -> list[str]:
    """.app bundles in the Applications folders, one subfolder deep (e.g. Utilities)."""
    if sys.platform != "darwin":
        return []
    bundles = []
    for root in ("/Applications", "/System/Applications", str(Path.home() / "Applications")):
        for folder in (root, *(e.path for e in _scandir_safe(root) if e.is_dir() and not e.name.endswith(".app"))):
            bundles += [e.path for e in _scandir_safe(folder) if e.name.endswith(".app")]
    return bundles


def _scandir_safe(folder: str) -> list[os.DirEntry]:
    try:
        return list(os.scandir(folder))
    except OSError:
        return []


def _start_apps() -> list[tuple[str, str]]:
    """(name, AppID) for everything the Start menu lists, including Store apps."""
    if sys.platform != "win32":
        return []
    ps = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
          "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                             capture_output=True, timeout=60, creationflags=CREATE_NO_WINDOW).stdout
        data = json.loads(out.decode("utf-8", "replace") or "[]")
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    if isinstance(data, dict):
        data = [data]
    return [(d["Name"], d["AppID"]) for d in data if d.get("Name") and d.get("AppID")]


class AppIndex(QtCore.QObject):
    """Installed apps as the Start menu lists them. Start-menu shortcuts come first
    (they carry real icons); Get-StartApps adds the rest, including Store apps, which
    launch through shell:AppsFolder. Refreshed in the background."""

    changed = Signal()

    def __init__(self, refresh_min: int) -> None:
        super().__init__()
        self.apps: list[Hit] = []
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(refresh_min * 60 * 1000)
        self._timer.timeout.connect(self.refresh)

    def start(self) -> None:
        self.refresh()
        self._timer.start()

    def refresh(self) -> None:
        threading.Thread(target=self._load, daemon=True, name="app list").start()

    def _load(self) -> None:
        found: dict[str, Hit] = {}
        for folder in start_menu_dirs():
            for root, _, files in os.walk(folder):
                for f in files:
                    stem, ext = os.path.splitext(f)
                    if ext.lower() in (".lnk", ".appref-ms") and "uninstall" not in stem.lower():
                        found.setdefault(stem.lower(), Hit(os.path.join(root, f), stem, False, "app"))
        for path in mac_app_bundles():
            stem = os.path.splitext(os.path.basename(path))[0]
            found.setdefault(stem.lower(), Hit(path, stem, False, "app"))
        for name, app_id in _start_apps():
            if "uninstall" not in name.lower():
                found.setdefault(name.lower(), Hit("shell:AppsFolder\\" + app_id, name, False, "app"))
        self.apps = sorted(found.values(), key=lambda h: h.name.lower())
        self.changed.emit()

    def search(self, text: str, limit: int) -> list[Hit]:
        q = Query(text)
        if not q.driver or q.exts or q.path_terms or q.kind in ("file", "folder"):
            return []
        scored = []
        for app in self.apps:
            name_l = app.name.lower()
            if q.name_ok(name_l, False) and q.path_ok(name_l):
                quality = q.rank(name_l, name_l)[1]
                if quality <= 2:  # whole word / prefix matches only: "word" ≠ "password"
                    scored.append((quality, len(name_l), app))
        scored.sort(key=lambda s: s[:2])
        return [app for *_, app in scored[:limit]]


def launch_app(target: str) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", target])
    elif target.startswith("shell:AppsFolder\\"):
        subprocess.Popen(["explorer.exe", target], creationflags=CREATE_NO_WINDOW)
    else:
        os.startfile(target)


def open_path(path: str) -> None:
    if sys.platform == "win32":
        os.startfile(path)
    elif not QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(path)):
        raise OSError(f"cannot open {path}")


def force_foreground(widget: QtWidgets.QWidget) -> None:
    """Bring a window to the foreground so it receives typing. Windows normally refuses
    this for background apps, and the cat itself never takes focus, so we briefly attach
    to the current foreground thread's input queue, which Windows allows."""
    widget.activateWindow()
    if sys.platform != "win32":
        return
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    u32.GetForegroundWindow.restype = ctypes.c_void_p
    u32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    u32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    hwnd = int(widget.winId())
    fg_thread = u32.GetWindowThreadProcessId(u32.GetForegroundWindow(), None)
    me = k32.GetCurrentThreadId()
    attached = bool(fg_thread and fg_thread != me and u32.AttachThreadInput(fg_thread, me, True))
    u32.SetForegroundWindow(hwnd)
    if attached:
        u32.AttachThreadInput(fg_thread, me, False)


def reveal_path(path: str) -> None:
    if sys.platform == "win32":
        subprocess.Popen(f'explorer /select,"{path}"')
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])  # Finder, with the item selected
    else:
        open_path(os.path.dirname(path))


# =================================================================================
# Cobra mode - empties the Recycle Bin and clears old temp/cache files
# =================================================================================
def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


class _SHQUERYRBINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint32), ("i64Size", ctypes.c_int64), ("i64NumItems", ctypes.c_int64)]


def recycle_bin_info() -> tuple[int, int]:
    """(bytes, items) in the Recycle Bins of all drives."""
    if sys.platform != "win32":
        return 0, 0
    info = _SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(info)
    if ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info)) != 0:
        return 0, 0
    return info.i64Size, info.i64NumItems


def empty_recycle_bin() -> None:
    SHERB_SILENT = 0x1 | 0x2 | 0x4  # no confirmation, no progress UI, no sound
    ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, SHERB_SILENT)


def _is_link(st: os.stat_result) -> bool:
    """Symlinks and junctions: removed as links, never followed into their target."""
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def _tree_stats(path: str) -> tuple[float, int]:
    """(newest modification time, total bytes) of a file or folder tree."""
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return time.time(), 0  # unreadable: treat as "in use"
    if not stat.S_ISDIR(st.st_mode) or _is_link(st):
        return st.st_mtime, st.st_size if not stat.S_ISDIR(st.st_mode) else 0
    newest, size = st.st_mtime, 0
    try:
        children = os.listdir(path)
    except OSError:
        return time.time(), 0
    for child in children:
        child_newest, child_size = _tree_stats(os.path.join(path, child))
        newest, size = max(newest, child_newest), size + child_size
    return newest, size


def _remove_tree(path: str) -> tuple[int, int]:
    """Delete a file or folder tree; returns (bytes freed, items that could not be deleted)."""
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return 0, 0  # already gone
    if stat.S_ISDIR(st.st_mode) and not _is_link(st):
        freed = failed = 0
        try:
            children = os.listdir(path)
        except OSError:
            return 0, 1
        for child in children:
            f, x = _remove_tree(os.path.join(path, child))
            freed, failed = freed + f, failed + x
        try:
            os.rmdir(path)
        except OSError:
            failed += 0 if failed else 1
        return freed, failed
    try:
        if stat.S_ISDIR(st.st_mode):
            os.rmdir(path)  # junction / directory symlink: removes the link only
            return 0, 0
        if getattr(st, "st_file_attributes", 0) & 0x1:  # FILE_ATTRIBUTE_READONLY
            os.chmod(path, stat.S_IWRITE)
        os.remove(path)
        return st.st_size, 0
    except OSError:
        return 0, 1  # in use or protected: left alone


@dataclass
class CleanupPlan:
    recycle_bytes: int
    recycle_items: int
    temp_items: list[tuple[str, int]]  # (path, bytes) of top-level temp/cache entries to delete

    @property
    def temp_bytes(self) -> int:
        return sum(size for _, size in self.temp_items)

    @property
    def empty(self) -> bool:
        return not self.recycle_items and not self.temp_items


class CobraCleaner(QtCore.QObject):
    """Cobra mode: empties the Recycle Bin and deletes old temp/cache files.

    Only top-level temp entries whose whole tree is older than cobra_min_age_h are
    removed: files that running programs and installers are using are newer, so they
    are left alone, and anything locked is skipped. Ages are re-checked right before
    deleting, and links/junctions are removed as links, never followed.
    """

    planned = Signal(object)    # CleanupPlan
    finished = Signal(int, int)  # (bytes freed, items skipped)

    def __init__(self, cfg: Config) -> None:
        super().__init__()
        self.cfg = cfg
        self.busy = False

    def cache_dirs(self) -> list[str]:
        env = os.environ
        candidates = list(self.cfg.cobra_extra_dirs)
        local = env.get("LOCALAPPDATA")
        if sys.platform == "win32" and local:  # no default cleanup targets on other systems
            candidates += [env.get("TEMP"), env.get("TMP"), os.path.join(local, "Temp"),
                           os.path.join(env.get("SystemRoot", r"C:\Windows"), "Temp"),
                           os.path.join(local, "Microsoft", "Windows", "INetCache"),
                           os.path.join(local, "CrashDumps")]
        seen, dirs = set(), []
        for d in candidates:
            if d and os.path.isdir(d):
                key = os.path.normcase(os.path.realpath(d))
                if key not in seen:
                    seen.add(key)
                    dirs.append(d)
        return dirs

    def _cutoff(self) -> float:
        return time.time() - self.cfg.cobra_min_age_h * 3600

    def build_plan(self) -> CleanupPlan:
        rb_bytes, rb_items = recycle_bin_info()
        cutoff, items = self._cutoff(), []
        for folder in self.cache_dirs():
            try:
                names = os.listdir(folder)
            except OSError:
                continue
            for name in names:
                path = os.path.join(folder, name)
                newest, size = _tree_stats(path)
                if newest < cutoff:
                    items.append((path, size))
        return CleanupPlan(rb_bytes, rb_items, items)

    def execute(self, plan: CleanupPlan) -> tuple[int, int]:
        freed = skipped = 0
        if plan.recycle_items:
            before = recycle_bin_info()[0]
            empty_recycle_bin()
            freed += max(0, before - recycle_bin_info()[0])
        cutoff = self._cutoff()
        for path, _ in plan.temp_items:
            if _tree_stats(path)[0] >= cutoff:
                continue  # touched since the plan was made: now in use, leave it
            f, x = _remove_tree(path)
            freed, skipped = freed + f, skipped + x
        return freed, skipped

    # Threaded wrappers for the UI.
    def plan_async(self) -> None:
        self.busy = True
        threading.Thread(target=self._plan_thread, daemon=True).start()

    def _plan_thread(self) -> None:
        with background_io():
            plan = self.build_plan()
        self._finish_plan(plan)

    def _finish_plan(self, plan: CleanupPlan) -> None:
        self.busy = False
        self.planned.emit(plan)

    def execute_async(self, plan: CleanupPlan) -> None:
        self.busy = True
        threading.Thread(target=self._execute_thread, args=(plan,), daemon=True).start()

    def _execute_thread(self, plan: CleanupPlan) -> None:
        result = (0, 0)
        try:
            with background_io():  # deleting gigabytes must not starve File Explorer of disk time
                result = self.execute(plan)
        finally:
            self._finish_execute(*result)  # always report back, so the cat never stays stuck

    def _finish_execute(self, freed: int, skipped: int) -> None:
        self.busy = False
        self.finished.emit(freed, skipped)


# =================================================================================
# Sprites (placeholder pixel art drawn from strings - swap for PNG frames later)
# =================================================================================
PALETTE = {
    "K": "#3b2a20",  # outline (warm dark brown reads softer than black)
    "O": "#f6a55b",  # orange fur
    "D": "#d9773a",  # tabby stripes
    "W": "#fffaf3",  # white fur (muzzle, chest, socks)
    "P": "#ff8fa3",  # nose / inner ears
    "E": "#1e2630",  # eyes
    "H": "#ffffff",  # eye highlight
}

# 20 x 16 tabby, facing right. Rows 0-12 are head/body/tail; legs are generated.
_BODY = [
    "............K....K..",
    "...........KPK..KPK.",
    "..KK.......KOOKKKOOK",
    ".KOK......KOOOOOOOOK",
    ".KOK......KOEHOOEHOK",
    ".KOK......KOEEOOEEOK",
    "..KOK.....KWOOPPOOWK",
    "...KOK....KWWWKKWWWK",
    "....KOKKKKKKKWWWWWK.",
    "....KOODOODOOOWWWWK.",
    "....KOODOODOOOOWWWK.",
    "....KOODOODOOOOOWWK.",
    "....KOOOOOOOOOOOOOK.",
]


def _legs(starts: tuple[int, ...], width: int = 20) -> list[str]:
    """Three leg rows (fur, white sock, outline) with legs starting at the given columns."""
    rows = []
    for part in ("KOK", "KWK", "KKK"):
        row = ["."] * width
        for s in starts:
            row[s:s + 3] = part
        rows.append("".join(row))
    return rows


def _replace(rows: list[str], changes: dict[int, str]) -> list[str]:
    return [changes.get(i, r) for i, r in enumerate(rows)]


_STAND = _legs((5, 8, 12, 15))
SPRITE_ART = {
    "stand": _BODY + _STAND,
    "blink": _replace(_BODY, {4: ".KOK......KOOOOOOOOK", 5: ".KOK......KOKKOOKKOK"}) + _STAND,
    "walk_a": _BODY + _legs((4, 8, 11, 16)),   # legs spread
    "walk_b": _BODY + _legs((6, 7, 13, 14)),   # legs passing
    "alert": _replace(_BODY, {3: ".KOK......KOEHOOEHOK", 4: ".KOK......KOEEOOEEOK"}) + _STAND,
    "sleep": ["." * 20] * 7 + [
        "..............K...K.",
        ".............KPK.KPK",
        "....KKKKKKKKKKOOKOOK",
        "...KOODOODOOKOOOOOOK",
        "..KOODOODOOKOKKOKKOK",
        "..KOODOODOOKOOOPPOOK",
        ".KOOOOOOOOOOKWWWWWK.",
        ".KOWWWOOOOOOOOOOOK..",
        "..KKKKKKKKKKKKKKK...",
    ],
}
# Two spare columns on the right (in front of the face) give the smashing paw room.
SPRITE_ART = {name: [row + ".." for row in rows] for name, rows in SPRITE_ART.items()}
CAT_ART_W = len(SPRITE_ART["stand"][0])


def _patch(rows: list[str], patches: dict[int, tuple[int, str]]) -> list[str]:
    """Overwrite pixels: {row: (start column, text)}; spaces in text keep the original."""
    out = list(rows)
    for r, (c, text) in patches.items():
        row = list(out[r])
        for i, ch in enumerate(text):
            if ch != " ":
                row[c + i] = ch
        out[r] = "".join(row)
    return out


_THREE_LEGS = [row + ".." for row in _BODY + _legs((5, 8, 12))]  # front leg is busy smashing
SPRITE_ART["smash_up"] = _patch(_THREE_LEGS, {  # paw raised high in front of the face
    0: (19, "KKK"), 1: (19, "KWK"), 2: (20, "WK"), 3: (20, "OK"), 4: (20, "OK"), 5: (20, "OK"), 6: (20, "KK"),
})
SPRITE_ART["smash_down"] = _patch(_THREE_LEGS, {  # paw slammed onto the ground ahead
    11: (19, "KK"), 12: (19, "OOK"), 13: (18, "KOOK"), 14: (18, "KWWK"), 15: (18, "KKKK"),
})

# Animation cycles per pet state (frame names from SPRITE_ART).
ANIMATIONS = {
    "idle": ["stand", "stand", "stand", "stand", "stand", "stand", "blink"],
    "walk": ["walk_a", "stand", "walk_b", "stand"],
    "sleep": ["sleep"],
    "alert": ["alert"],
    "smash": ["smash_up", "smash_up", "smash_down", "smash_down"],
    "zip": ["smash_up"],           # stunt: arm up, firing and riding the grapple line
    "fly": ["fly_a", "fly_b"],     # stunt: only pets with stunts=True have these frames
}

# --- Cobra (Cobra mode opponent) ---------------------------------------------------
COBRA_PALETTE = {
    "G": "#5f9e4c",  # scales
    "L": "#c9df8a",  # belly / hood markings
    "Y": "#ffd23f",  # eyes
    "R": "#e5484d",  # tongue
}
_COBRA = [  # 10 x 13: slim hood, thin neck, small coil; ~80% of the cat's height
    "...KKKK...",
    "..KGGGGK..",
    ".KGYGGYGK.",
    ".KGGGGGGK.",
    ".KGLGGLGK.",
    "..KGGGGK..",
    "...KLLK...",
    "...KGLK...",
    "...KGLK...",
    "..KGGLGK..",
    ".KGGKKGGK.",
    "KGGLLLLGGK",
    ".KKKKKKKK.",
]


def _shift(rows: list[str], upto: int, dx: int) -> list[str]:
    """Slide rows[:upto] sideways by one pixel (dx = -1 or 1) to sway the hood."""
    moved = [("." + r[:-1]) if dx > 0 else (r[1:] + ".") for r in rows[:upto]]
    return moved + rows[upto:]


_COBRA_SHUT = _patch(_COBRA, {2: (3, "K  K")})  # eyes squeezed shut
COBRA_ART = {
    "rear": _COBRA,
    "sway": _patch(_shift(_COBRA, 9, 1), {5: (5, "RR")}),  # head leans, tongue flicks out
    "hit": ["." * 10] + _COBRA_SHUT[:3] + _COBRA_SHUT[4:],  # head squashed down a pixel, eyes shut
    "dead": ["." * 13] * 7 + [  # flat on its back, head on the left (X eyes are drawn on top)
        "..KKK........",
        ".KGGGK.......",
        "KGGGGGKKKKK..",
        "KGGGGGGLLLGKK",
        ".KRKKKKKKKKK.",
        "..R..........",
    ],
}
COBRA_DEAD_EYES = ((2.0, 8.9), (4.2, 8.9))  # art cell centres of the eyes on the "dead" frame


# --- The pets --------------------------------------------------------------------------
@dataclass
class PetKind:
    key: str
    label: str
    art: dict[str, list[str]]            # every frame the same size, facing right
    palette: dict[str, str]
    head_col: float                      # where "!" and "z" float (art column + 1 padding cell)
    mouth: tuple[float, float]           # where a fetched item is held (column, row, + 1 padding cell)
    hello: str                           # opens the "What should I do?" question
    pet_line: str                        # said when clicked
    chatter: tuple[str, ...]             # this pet's own small talk
    stunts: bool = False                 # has fly_a / fly_b frames and does the grapple-and-fly stunt
    hat: tuple[float, float] = (0, 0)    # where a hat sits: centre column and the row its brim rests on (+ padding)
    fur: str = "O"                       # palette letters recoloured by the Colour setting
    shade: str = "D"


def _feet(starts: tuple[int, ...], width: int, parts: tuple[str, ...]) -> list[str]:
    """Leg rows: one row per entry of `parts`, repeated at each start column."""
    rows = []
    for part in parts:
        row = ["."] * width
        for s in starts:
            row[s:s + len(part)] = part
        rows.append("".join(row))
    return rows


def _build_art(body: list[str], legs: dict[str, tuple[int, ...]], parts: tuple[str, ...],
               blink: dict[int, str], alert: dict[int, str], sleep: list[str],
               smash_up: dict[int, tuple[int, str]], smash_down: dict[int, tuple[int, str]],
               spare: int = 2) -> dict[str, list[str]]:
    """All the frames of a pet from its body, legs and a few replaced rows. `spare` empty
    columns on the right give the smashing paw room, as on the cat."""
    width = len(body[0])
    stand = _feet(legs["stand"], width, parts)
    frames = {
        "stand": body + stand,
        "blink": _replace(body, blink) + stand,
        "walk_a": body + _feet(legs["walk_a"], width, parts),
        "walk_b": body + _feet(legs["walk_b"], width, parts),
        "alert": _replace(body, alert) + stand,
        "sleep": ["." * width] * (len(body) + len(parts) - len(sleep)) + sleep,
    }
    frames = {name: [row + "." * spare for row in rows] for name, rows in frames.items()}
    smash_base = [row + "." * spare for row in body + _feet(legs["smash"], width, parts)]
    frames["smash_up"] = _patch(smash_base, smash_up)
    frames["smash_down"] = _patch(smash_base, smash_down)
    return frames


_RETRIEVER_BODY = [
    "..............KKKKK...",
    ".............KOOOOOK..",
    "...........KKDOOOOOOK.",
    "..KK......KDDDOOEHOOK.",
    ".KOOK.....KDDDOOEEOOOK",
    ".KOOOK....KDDDOOOOOWNK",
    "..KOOOK....KDDOOOWWWWK",
    "...KOOK....KDDOOOKKKK.",
    "...KOOOKKKKKKOOOOPPK..",
    "....KOOOOOOOOOOWWKK...",
    "....KOOOOOOOOOOWWWK...",
    "....KOOOOOOOOOOOWWK...",
    "....KDOOOOOOOOOOOOK...",
    "....KDDOOOOOOOOOOOK...",
]
_RETRIEVER = _build_art(
    _RETRIEVER_BODY,
    {"stand": (5, 8, 13, 16), "walk_a": (4, 8, 12, 17), "walk_b": (6, 7, 14, 15), "smash": (5, 8, 13)},
    ("KOK", "KWK", "KKK"),
    blink={3: "..KK......KDDDOOOOOOK.", 4: ".KOOK.....KDDDOOKKOOOK"},
    alert={2: "...........KKDOOEHOOK.", 3: "..KK......KDDDOOEEOOK.", 4: ".KOOK.....KDDDOOOOOOOK"},
    sleep=[
        "...............KKKKK..",
        "..KK..........KDDOOOK.",
        ".KOOKKKKKKKKKKKDDOOOOK",
        ".KOOOOOOOOOOOOKDDKKOWK",
        "..KOOOOOOOOOOOKDOOOWNK",
        "..KDOOOOOOOOOOOOKKKKK.",
        "..KDDOOOOOOOOOOOOOOWK.",
        "...KKKKKKKKKKKKKKKKKK.",
    ],
    smash_up={0: (21, "KKK"), 1: (21, "KWK"), 2: (22, "OK"), 3: (22, "OK"), 4: (22, "OK"), 5: (22, "OK"), 6: (22, "KK")},
    smash_down={12: (19, "K"), 13: (19, "OK"), 14: (18, "KOOK"), 15: (18, "KWWK"), 16: (18, "KKKK")},
)

_HAMSTER_BODY = [
    ".....KK...KK..",
    "....KPPK.KPPK.",
    "...KOOOKKKOOOK",
    "..KOOOOOOOOOOK",
    ".KOOOOOOOEHOOK",
    ".KDOOOOOOEEOOK",
    "KOOOOOOOWWWWWK",
    "KODOOOOWWWPPWK",
    "KODOOOWWWWWWWK",
    "KOOOOOWWWWWWK.",
    ".KOOOOWWWWWK..",
]
_HAMSTER = _build_art(
    _HAMSTER_BODY,
    {"stand": (3, 6, 9), "walk_a": (2, 6, 10), "walk_b": (4, 5, 8), "smash": (3, 6)},
    ("KPK", "KKK"),
    blink={4: ".KOOOOOOOOOOOK", 5: ".KDOOOOOOKKOOK"},
    alert={3: "..KOOOOOOEHOOK", 4: ".KOOOOOOOEEOOK", 5: ".KDOOOOOOOOOOK"},
    sleep=[
        "....KPK.KPK...",
        "...KKKKKKKKK..",
        "..KOOOOOOOOOK.",
        ".KOOODOOOKKOOK",
        ".KOOODOOOOOOPK",
        ".KOOOOOOWWWWWK",
        "..KKKKKKKKKKK.",
    ],
    smash_up={1: (14, "KK"), 2: (14, "PK"), 3: (14, "OK"), 4: (14, "OK"), 5: (14, "OK"), 6: (14, "KK")},
    smash_down={9: (13, "K"), 10: (12, "KOOK"), 11: (12, "KPPK"), 12: (12, "KKKK")},
)

_BUNNY_BODY = [
    "...........K..K...",
    "..........KPKKPK..",
    "..........KPKKPK..",
    "..........KPKKPK..",
    "..........KPKKPK..",
    "..........KOKKOK..",
    ".........KOOOOOOK.",
    "........KOOOOEHOOK",
    "........KOOOOEEOOK",
    "........KOOOOOOWPK",
    ".........KOOOWWWWK",
    "...KKKKKKKKOOWWKK.",
    "..KOOOOOOOOOOWWK..",
    "KKKOOOOOOOOOOOWK..",
    "KWWKOOOOOOOOOOWK..",
    "KWWKDOOOOOOOOOOK..",
    ".KKKDDOOOOOOOOOK..",
]
_BUNNY = _build_art(
    _BUNNY_BODY,
    {"stand": (5, 9, 12), "walk_a": (4, 9, 13), "walk_b": (6, 8, 11), "smash": (5, 9)},
    ("KWK", "KKK"),
    blink={7: "........KOOOOOOOOK", 8: "........KOOOOKKOOK"},
    alert={6: ".........KOOOEHOOK", 7: "........KOOOOEEOOK", 8: "........KOOOOOOOOK"},
    sleep=[
        "............KKKK..",
        "..........KKPPPPK.",
        "..KKKKKKKKKOKKKK..",
        ".KOOOOOOOOKOOOOOK.",
        "KWKOOOOOOKOOKKOOOK",
        "KWKOOOOOOOKOOOOWPK",
        ".KKDOOOOOOOOKWWWK.",
        "...KDDOOOOOOOOOK..",
        "....KKKKKKKKKKKK..",
    ],
    smash_up={5: (18, "KK"), 6: (18, "WK"), 7: (18, "OK"), 8: (18, "OK"), 9: (18, "OK"), 10: (18, "KK")},
    smash_down={16: (15, "K"), 17: (15, "KOOK"), 18: (15, "KKKK")},
)

_PENGUIN_BODY = [
    ".....KKKKKK.....",
    "....KOOOOOOK....",
    "...KOOOOOOOOK...",
    "...KOOOOWEHOK...",
    "...KOOOOWEEOK...",
    "...KOOOOWWWAAAK.",
    "...KOOOOWWWKKK..",
    "..KKOOOOWWWWK...",
    ".KOKOOOWWWWWK...",
    ".KOKOOOWWWWWK...",
    ".KOKOOOWWWWWK...",
    "..KKOOOWWWWWK...",
    "...KOOOWWWWWK...",
    "...KOOOWWWWWK...",
    "...KDOOOWWWK....",
    "....KKKKKKKK....",
]
_PENGUIN = _build_art(
    _PENGUIN_BODY,
    {"stand": (4, 8), "walk_a": (3, 9), "walk_b": (5, 7), "smash": (4, 8)},
    ("KAAK", "KKKK"),
    blink={3: "...KOOOOWWWOK...", 4: "...KOOOOWKKOK..."},
    alert={2: "...KOOOOWEHOK...", 3: "...KOOOOWEEOK...", 4: "...KOOOOWWWOK..."},
    sleep=[
        ".....KKKKKK.....",
        "....KOOOOOOK....",
        "...KOOOOOOOOK...",
        "...KOOOOWWWOK...",
        "...KOOOOWKKOK...",
        "...KOOOOWWWAAAK.",
        "...KOOOOWWWKKK..",
        "..KKOOOOWWWWK...",
        ".KOKOOOWWWWWK...",
        ".KOKOOOWWWWWK...",
        "..KKOOOWWWWWK...",
        "...KDOOOWWWK....",
        "...KKKKKKKKKK...",
        "..KAAAAKKAAAAK..",
        "...KKKK..KKKK...",
    ],
    smash_up={1: (15, "KKK"), 2: (15, "KOK"), 3: (15, "KOK"), 4: (15, "KOK"), 5: (15, "KOK"), 6: (15, "KOK"),
              7: (13, "OOOOK"), 8: (13, "KKKKK")},
    smash_down={9: (12, "KKKKK"), 10: (12, "OOOOK"), 11: (12, "KKKKK")},
)

_HERO_BODY = [
    ".....KKKKKK.....",
    "....KMMMMMMK....",
    "...KMMMMMMMMK...",
    "...KMMMHEMHEK...",
    "...KMMMHEMHEK...",
    "...KSSSSSSSSK...",
    "...KSSSSSKKSK...",
    "....KKKKKKKK....",
    "..KCKOOOOOOK....",
    ".KCCKOOYYOOOK...",
    ".KCCKOOYYOOKOK..",
    "KCCCKOOOOOOKOK..",
    "KCCCKYYYYYYKSK..",
    "KCCCKOOOOOOKKK..",
    "KCCCCKOOKOOK....",
]
_HERO = _build_art(
    _HERO_BODY,
    {"stand": (6, 9), "walk_a": (4, 11), "walk_b": (7, 8), "smash": (6, 9)},
    ("KOK", "KMK", "KKK"),
    blink={3: "...KMMMMMMMMK...", 4: "...KMMMKKMKKK..."},
    alert={2: "...KMMMHEMHEK...", 3: "...KMMMHEMHEK...", 4: "...KMMMMMMMMK..."},
    sleep=[
        "..........KKKKK.",
        ".........KMMMMMK",
        "...KKKKKKKMKKMMK",
        "..KCCCCCCCKMMMMK",
        ".KCCCCCCCCKSSKSK",
        ".KCCCCCCCCCKSSK.",
        "..KKKKKKKKKKKK..",
    ],
    smash_up={0: (14, "KKKK"), 1: (14, "KSSK"), 2: (15, "OK"), 3: (15, "OK"), 4: (15, "OK"), 5: (15, "OK"),
              6: (15, "OK"), 7: (14, "KOK"), 8: (12, "KOOK"), 10: (11, "O..."), 11: (11, "O..."), 12: (11, "Y..."),
              9: (12, "OOOK"), 13: (11, "O...")},
    smash_down={10: (11, "KKKKKKK"), 11: (11, "OOOOSSK"), 12: (11, "KKKKKKK"), 13: (11, "O...")},
)
# Flying level, one arm out in front, the cape flapping behind (the hero's stunt).
_HERO_FLY_LEGS = [
    "...KKOOYYOOOOOOSSK",
    "..KMMOOOOOOKKKKKK.",
    "..KMKKKKKKKK......",
    "...K..............",
    "..................",
    "..................",
    "..................",
]
_HERO["fly_a"] = ["." * 18] * 5 + [
    "...........KKKKK..",
    "..........KMMMMMK.",
    "KKK.......KMMHEHEK",
    "KCCKKKKKKKKSSSSSSK",
    ".KCCCCKOOOOKSSSKSK",
    "..KKCCKYYOOOKKKKK.",
] + _HERO_FLY_LEGS
_HERO["fly_b"] = ["." * 18] * 5 + [
    "...........KKKKK..",
    "..........KMMMMMK.",
    "..........KMMHEHEK",
    ".KKKKKKKKKKSSSSSSK",
    "KCCCCCCKOOOKSSSKSK",
    ".KKCCCKYYOOOKKKKK.",
] + _HERO_FLY_LEGS

PETS = {
    "cat": PetKind("cat", "Tabby cat", SPRITE_ART, PALETTE, 16.5, (20.5, 8.5), "Meow?", "Purr ♥",
                   ("Mrrp.", "*stretches*", "Purr... 💤"), hat=(16, 4)),
    "retriever": PetKind("retriever", "Golden retriever", _RETRIEVER, {
        "K": "#4a2f17", "O": "#ecb860", "D": "#c98a35", "W": "#fdeecb", "P": "#ff8fa3",
        "N": "#2b1d14", "E": "#1e2630", "H": "#ffffff"}, 17.5, (23.5, 9.5), "Woof?", "*wags tail* ♥",
        ("Woof!", "*wags tail*", "Did someone say ball? 🎾"), hat=(17.5, 2)),
    "hamster": PetKind("hamster", "Hamster", _HAMSTER, {
        "K": "#4a2f1c", "O": "#e8a860", "D": "#c9853f", "W": "#fff1d6", "P": "#ffa6b5",
        "E": "#1e2630", "H": "#ffffff"}, 10.5, (16.5, 9.5), "Squeak?", "Squeak ♥",
        ("Squeak!", "*stuffs cheeks*", "*runs on an invisible wheel*"), hat=(9.5, 4)),
    "bunny": PetKind("bunny", "Bunny", _BUNNY, {
        "K": "#5a4a52", "O": "#f4f0ea", "D": "#d3ccc1", "W": "#ffffff", "P": "#ffa6b5",
        "E": "#2a2230", "H": "#ffffff"}, 14.5, (20.0, 10.5), "Thump thump?", "*nose twitch* ♥",
        ("*nibbles*", "*happy hop*", "*twitches nose*"), hat=(14, 8)),
    "penguin": PetKind("penguin", "Penguin", _PENGUIN, {
        "K": "#141a24", "O": "#34465f", "D": "#222f42", "W": "#fbfbf7", "A": "#f5a524",
        "E": "#141a24", "H": "#ffffff"}, 11.5, (17.5, 7.5), "Honk?", "Honk ♥",
        ("Honk!", "*slides on belly*", "Is it cold in here? I hope so. 🧊"), hat=(9, 2)),
    "hero": PetKind("hero", "Caped hero", _HERO, {
        "K": "#1a2430", "O": "#1fa2a8", "M": "#0f3e52", "C": "#f2b134", "Y": "#ffd95e",
        "S": "#f6cfa6", "E": "#1a2430", "H": "#ffffff"}, 9.5, (17.0, 13.5), "Citizen!", "Stay strong ♥",
        ("*cape flutters*", "Justice never sleeps. You should, though.", "Your posture needs saving. 🦸"),
        stunts=True, hat=(9, 2), fur="O", shade="M"),
}

# Pet size choices: (name, sprite pixel size).
SIZES = (("Tiny", 3), ("Small", 4), ("Medium", 5), ("Large", 7), ("Huge", 10))

# Colour choices: the pet's main fur (or suit), with the shade worked out from it.
PET_COLOURS = {"Original": "", "Black": "#3a3a42", "Grey": "#9ba1aa", "White": "#f2efe9", "Brown": "#8a5a3b",
               "Ginger": "#f0973f", "Cream": "#f3dcb0", "Blue": "#5b8bd6", "Pink": "#f19bb6",
               "Mint": "#7fd1b0", "Purple": "#9b7fd6"}


def pet_palette(kind: PetKind, colour: str = "") -> dict[str, str]:
    """The pet's palette with its fur recoloured. Dark fur gets light eyes so they still show."""
    palette = dict(kind.palette)
    hex_colour = PET_COLOURS.get(colour, "")
    if hex_colour:
        base = QtGui.QColor(hex_colour)
        palette[kind.fur] = base.name()
        palette[kind.shade] = base.darker(125 if base.lightness() > 60 else 150).name()
        if base.lightness() < 80 and "E" in palette:
            palette["E"] = "#ffd23f"
    return palette


# Seasonal hats, drawn on the head (brim on the bottom row). B blue, Y gold, R red, P pink, N navy.
HAT_PALETTE = {"K": "#2a2230", "W": "#ffffff", "R": "#e5484d", "P": "#ff6fa0", "B": "#5bc0eb",
               "Y": "#ffd23f", "N": "#3b4a8c"}
HATS = {
    "party": ["...Y...", "...P...", "..PBP..", "..BPB..", ".PBPBP.", "KKKKKKK"],
    "winter": ["...W...", "..RRR..", ".RRRRR.", ".RWRWR.", "KWWWWWK"],
    "night": [".....W.", "....NK.", "...NNK.", "..NNNK.", ".NNNNNK", "KWWWWWK"],
}
BALL_ART = ["..KKKK..", ".KYYYYK.", "KYYWYYYK", "KYWYYYYK", "KYYYYWYK", "KYYYYWYK", ".KYYYYK.", "..KKKK.."]
BALL_PALETTE = {"K": "#4a5a1c", "Y": "#d6f04a", "W": "#ffffff"}


def season_hat(now: datetime, birthday: str) -> str | None:
    """Which hat to wear right now: birthday beats night, night beats winter."""
    if birthday and now.strftime("%m-%d") == birthday:
        return "party"
    if now.hour >= 21 or now.hour < 6:
        return "night"
    if now.month in (12, 1, 2):
        return "winter"
    return None


def render_art(rows: list[str], unit: int, palette: dict[str, str]) -> QtGui.QImage:
    """String art -> image, each art pixel a unit x unit block, with one empty cell of
    padding on every side."""
    width, height = len(rows[0]), len(rows)
    assert all(len(r) == width for r in rows), "sprite has ragged rows"
    img = QtGui.QImage((width + 2) * unit, (height + 2) * unit, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QtGui.QPainter(img)
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            if ch in palette:
                p.fillRect((x + 1) * unit, (y + 1) * unit, unit, unit, QtGui.QColor(palette[ch]))
    p.end()
    return img


class SpriteSheet:
    """Renders the string art into crisp pixmaps, plus mirrored copies for facing left.

    Each art pixel is drawn as a whole number of *device* pixels (scale x screen DPI
    ratio, rounded), so the cat stays sharp on 125%/150% Windows scaling instead of
    getting uneven, smeared pixels.
    """

    def __init__(self, scale: int, dpr: float, kind: PetKind | None = None, colour: str = "") -> None:
        self.dpr = dpr
        self.unit = max(1, round(scale * dpr))
        self.kind = kind or PETS["cat"]
        self.colour = colour
        self.palette = pet_palette(self.kind, colour)
        self.hats = {name: self._to_pixmap(render_art(rows, self.unit, HAT_PALETTE)) for name, rows in HATS.items()}
        self.ball = self._to_pixmap(render_art(BALL_ART, self.unit, BALL_PALETTE))
        self.frames: dict[str, dict[int, QtGui.QPixmap]] = {}
        for name, rows in self.kind.art.items():
            img = self._render(name, rows)
            mirrored = img.transformed(QtGui.QTransform().scale(-1, 1))
            self.frames[name] = {1: self._to_pixmap(img), -1: self._to_pixmap(mirrored)}
        size = self.frames["stand"][1].deviceIndependentSize()
        self.size = QtCore.QSize(math.ceil(size.width()), math.ceil(size.height()))

    def _to_pixmap(self, img: QtGui.QImage) -> QtGui.QPixmap:
        pix = QtGui.QPixmap.fromImage(img)
        pix.setDevicePixelRatio(self.dpr)
        return pix

    def _render(self, name: str, rows: list[str]) -> QtGui.QImage:
        return render_art(rows, self.unit, self.palette)

    def frame_name(self, animation: str, index: int) -> str:
        cycle = ANIMATIONS[animation]
        return cycle[index % len(cycle)]

    def frame(self, animation: str, facing: int, index: int) -> QtGui.QPixmap:
        return self.frames[self.frame_name(animation, index)][facing]

    def cobra_frames(self) -> dict[str, QtGui.QPixmap]:
        """Cobra pixmaps at the cat's pixel size; "dead_r" is the dead frame mirrored."""
        palette = {**PALETTE, **COBRA_PALETTE}
        frames = {}
        for name, rows in COBRA_ART.items():
            img = render_art(rows, self.unit, palette)
            frames[name] = self._to_pixmap(img)
            if name == "dead":
                frames["dead_r"] = self._to_pixmap(img.transformed(QtGui.QTransform().scale(-1, 1)))
        return frames


# =================================================================================
# Cat window
# =================================================================================
class PetState:
    IDLE = "idle"
    WALK_LEFT = "walk_left"
    WALK_RIGHT = "walk_right"
    SLEEP = "sleep"
    ALERT = "alert"
    SMASH = "smash"
    RUN = "run"          # fetch trip: fast walk cycle
    ZIP = "zip"          # stunt: firing and riding the grapple line
    FLY = "fly"          # stunt: flying


PET_WINDOW_FLAGS = (
    Qt.WindowType.FramelessWindowHint
    | Qt.WindowType.WindowStaysOnTopHint
    | Qt.WindowType.Tool                       # no taskbar button
    | Qt.WindowType.WindowDoesNotAcceptFocus   # never steal keyboard focus
)


class GrappleLine(QtWidgets.QWidget):
    """The hero's golden grapple line: a click-through window just big enough for the line."""

    PAD = 5

    def __init__(self) -> None:
        super().__init__(None, PET_WINDOW_FLAGS)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._a = self._b = QtCore.QPoint()

    def stretch(self, a: QtCore.QPoint, b: QtCore.QPoint) -> None:
        box = QtCore.QRect(a, b).normalized().adjusted(-self.PAD, -self.PAD, self.PAD, self.PAD)
        self._a, self._b = a - box.topLeft(), b - box.topLeft()
        self.setGeometry(box)
        if not self.isVisible():
            self.show()
        self.update()

    def paintEvent(self, event) -> None:
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.setPen(QtGui.QPen(QtGui.QColor("#ffd95e"), 2.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(self._a, self._b)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QtGui.QColor("#fff3b8"))
        p.drawEllipse(QtCore.QPointF(self._b), 4, 4)  # the hook at the far end
        p.end()


class CatWindow(QtWidgets.QWidget):
    clicked = Signal()
    double_clicked = Signal()
    moved = Signal(QtCore.QRect)
    context_menu_requested = Signal(QtCore.QPoint)
    paw_down = Signal()  # the smashing paw just hit the ground (Cobra mode)
    trip_done = Signal()  # back home from a fetch trip, carrying the item
    stunt_wanted = Signal()  # a pet with stunts feels like showing off
    stunt_done = Signal()    # landed back where the stunt began
    trip_reached = Signal()  # got to the far end of a trip (e.g. picked up the ball)

    TOP_MARGIN = 40     # room above the sprite for "!" and "z" overlays
    GROUND = 6          # room below the sprite for its soft ground shadow
    MIN_W = 100         # wide enough for the focus badge even on a tiny hamster
    EMOTE_S = 1.4
    STUNT_CHANCE = 0.05          # per wander decision, about one stunt every couple of minutes
    SHOOT_S, ZIP_S, DIVE_S = 0.3, 0.6, 0.9
    FLY_PX_PER_S = 700

    def __init__(self, sprites: SpriteSheet, cfg: Config, ui_font: UIFont) -> None:
        super().__init__(None, PET_WINDOW_FLAGS)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)  # macOS hides tool windows otherwise
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.sprites = sprites
        self.cfg = cfg
        self.ui_font = ui_font
        self.setFixedSize(max(self.MIN_W, sprites.size.width() + 16), sprites.size.height() + self.TOP_MARGIN + self.GROUND)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self.state = PetState.IDLE
        self.facing = 1          # 1 = right, -1 = left
        self.mode = None         # None = free wandering; "alert", "away", "fetch", "busy", "smash" = locked
        self._frame = 0
        self._press_global = None
        self._press_pos = None
        self._dragging = False
        self._swallow_release = False

        # Fetch trip state (see start_trip).
        self._trip_phase = None            # "out", "away", "back" or None
        self._trip_target = 0
        self._trip_home = 0
        self._trip_speed = 0.0
        self._trip_x = 0.0
        self._trip_item: QtGui.QPixmap | None = None
        self.carrying: QtGui.QPixmap | None = None  # drawn in the cat's mouth

        # Stunt state (see start_stunt).
        self._stunt_phase = None           # "shoot", "zip", "fly", "dive" or None
        self._stunt_t0 = 0.0
        self._stunt_home = self._stunt_top = self._stunt_far = QtCore.QPoint()
        self._stunt_anchor = QtCore.QPoint()
        self._line: GrappleLine | None = None

        # Extras drawn over the sprite.
        self.badge = ""                    # small pill above the head, e.g. the focus countdown
        self.hat: str | None = None        # a HATS key
        self._emotes: list[tuple[str, str, float]] = []  # (text, colour, start time) floating up

        # Perching on top of another window (Windows): see perch_provider / _choose_behaviour.
        self.perch_provider = None         # () -> (hwnd, QRect) of a window to stand on, or None
        self.perch_lookup = None           # (hwnd) -> QRect or None while perched
        self._perch: tuple[int, QtCore.QRect] | None = None
        self._perch_until = 0.0
        self._perch_check = 0
        self._hop: tuple[QtCore.QPoint, QtCore.QPoint, float, float] | None = None  # from, to, start, duration

        # Movement tick: moves the window while walking.
        self._move_timer = QtCore.QTimer(self)
        self._move_timer.setInterval(cfg.move_interval_ms)
        self._move_timer.timeout.connect(self._on_move_tick)

        # Animation tick: advances the sprite frame.
        self._frame_timer = QtCore.QTimer(self)
        self._frame_timer.setInterval(cfg.frame_interval_ms)
        self._frame_timer.timeout.connect(self._on_frame_tick)

        # Behaviour timer: single-shot, re-armed with a random delay each time.
        self._behaviour_timer = QtCore.QTimer(self)
        self._behaviour_timer.setSingleShot(True)
        self._behaviour_timer.timeout.connect(self._choose_behaviour)

        # Single clicks wait one double-click interval, so a double-click doesn't also
        # fire the single-click action.
        self._click_timer = QtCore.QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.setInterval(QtWidgets.QApplication.doubleClickInterval())
        self._click_timer.timeout.connect(self.clicked.emit)

    # --- lifecycle -----------------------------------------------------------------
    def set_sprites(self, sprites: SpriteSheet) -> None:
        """Swap the pet or its size, keeping its feet on the ground and its centre in place."""
        old = self.geometry()
        self.sprites = sprites
        self.setFixedSize(max(self.MIN_W, sprites.size.width() + 16), sprites.size.height() + self.TOP_MARGIN + self.GROUND)
        avail = self._screen_geometry()
        x = old.center().x() - self.width() // 2
        self.move(max(avail.left(), min(x, avail.right() - self.width())), old.bottom() - self.height() + 1)
        self.moved.emit(self.geometry())
        self.update()

    def start(self) -> None:
        avail = QtGui.QGuiApplication.primaryScreen().availableGeometry()
        x = random.randint(avail.left(), max(avail.left(), avail.right() - self.width()))
        self.move(x, avail.bottom() - self.height() + 1)  # sit on top of the taskbar
        self.show()
        self._move_timer.start()
        self._frame_timer.start()
        self._schedule_behaviour()

    # --- state machine -------------------------------------------------------------
    def set_mode(self, mode: str | None) -> None:
        """Lock the cat into alert/away/fetch behaviour, or release it (None) to wander."""
        if mode != "stunt":
            self._cancel_stunt()  # anything else that needs the pet brings it straight home
        if mode in ("trip", "smash", "stunt", "away"):
            self.leave_perch(instant=True)  # these happen on the ground
        self.mode = mode
        self._behaviour_timer.stop()
        if mode == "stunt":
            self._set_state(PetState.ZIP)
        elif mode == "alert":
            self._set_state(PetState.ALERT)
        elif mode == "away":
            self._set_state(PetState.SLEEP)
        elif mode in ("fetch", "busy"):
            self._set_state(PetState.IDLE)
        elif mode == "smash":
            self._set_state(PetState.SMASH)
        elif mode == "trip":
            self._set_state(PetState.RUN)
        else:
            self._set_state(PetState.IDLE)
            self._schedule_behaviour()

    @property
    def busy(self) -> bool:
        """Doing a task for the user (fetching, Cobra mode)."""
        return self.mode in ("fetch", "busy", "smash", "trip", "stunt")

    # --- stunt: fire a grapple line, zip to the top of the screen, fly, dive home ------
    def start_stunt(self) -> None:
        avail = self._screen_geometry()
        room_right = avail.right() - self.geometry().right()
        room_left = self.x() - avail.left()
        self.facing = 1 if room_right >= room_left else -1
        self._stunt_home = self.pos()
        top_x = self.x() + self.facing * min(260, max(room_right, room_left))
        self._stunt_top = QtCore.QPoint(top_x, avail.top())
        self._stunt_far = QtCore.QPoint(avail.right() - self.width() if self.facing == 1 else avail.left(), avail.top())
        self._stunt_anchor = QtCore.QPoint(top_x + self.width() // 2, avail.top())
        if self._line is None:
            self._line = GrappleLine()
        self.set_mode("stunt")
        self._stunt_phase, self._stunt_t0 = "shoot", time.monotonic()

    def _hand_point(self) -> QtCore.QPoint:
        """The raised fist on the smash_up frame, in screen coordinates."""
        w, h = self.sprites.size.width(), self.sprites.size.height()
        cells = len(self.sprites.kind.art["stand"][0]) + 2
        cell = w / cells
        left = (self.width() - w) / 2
        col = 17 if self.facing == 1 else cells - 17
        return self.pos() + QtCore.QPoint(round(left + cell * col), round(self.height() - self.GROUND - h + cell * 2))

    def _stunt_tick(self) -> None:
        def lerp(a: QtCore.QPoint, b: QtCore.QPoint, kx: float, ky: float) -> QtCore.QPoint:
            return QtCore.QPoint(round(a.x() + (b.x() - a.x()) * kx), round(a.y() + (b.y() - a.y()) * ky))

        def smooth(k: float) -> float:
            return k * k * (3 - 2 * k)

        now = time.monotonic()
        t = now - self._stunt_t0
        if self._stunt_phase == "shoot":
            k = min(1.0, t / self.SHOOT_S)
            hand = self._hand_point()
            self._line.stretch(hand, lerp(hand, self._stunt_anchor, k, k))
            if k >= 1.0:
                self._stunt_phase, self._stunt_t0 = "zip", now
        elif self._stunt_phase == "zip":
            k = min(1.0, t / self.ZIP_S)
            self.move(lerp(self._stunt_home, self._stunt_top, smooth(k), smooth(k)))
            self._line.stretch(self._hand_point(), self._stunt_anchor)
            if k >= 1.0:
                self._line.hide()
                self._set_state(PetState.FLY)
                self._stunt_phase, self._stunt_t0 = "fly", now
        elif self._stunt_phase == "fly":
            dist = abs(self._stunt_far.x() - self._stunt_top.x())
            k = min(1.0, t * self.FLY_PX_PER_S / max(1, dist))
            self.move(lerp(self._stunt_top, self._stunt_far, k, k))
            if k >= 1.0:
                if self._stunt_home.x() != self.x():
                    self.facing = 1 if self._stunt_home.x() > self.x() else -1
                self._stunt_phase, self._stunt_t0 = "dive", now
        elif self._stunt_phase == "dive":
            k = min(1.0, t / self.DIVE_S)
            self.move(lerp(self._stunt_far, self._stunt_home, smooth(k), k * k))  # swoops down, not a straight line
            if k >= 1.0:
                self._stunt_phase = None
                self._set_state(PetState.IDLE)
                self.stunt_done.emit()
        self.moved.emit(self.geometry())

    def _cancel_stunt(self) -> None:
        if self._stunt_phase is None:
            return
        self._stunt_phase = None
        if self._line is not None:
            self._line.hide()
        self.move(self._stunt_home)
        self.moved.emit(self.geometry())

    # --- fetch trip --------------------------------------------------------------------
    def start_trip(self, item: QtGui.QPixmap | None, leg_s: float, pause_s: float, target: int | None = None) -> None:
        """Dash off the nearest screen edge (or to window x `target`), wait pause_s, run back
        home carrying `item` in the mouth, then emit trip_done. Each leg takes ~leg_s."""
        avail = self._screen_geometry()
        self._trip_home = self.x()
        if target is None:
            go_right = self.geometry().center().x() > avail.center().x()
            self._trip_target = avail.right() + 20 if go_right else avail.left() - self.width() - 20
        else:
            go_right = target > self.x()
            self._trip_target = target
        self.facing = 1 if go_right else -1
        ticks = max(1.0, leg_s * 1000 / self.cfg.move_interval_ms)
        self._trip_speed = max(4.0, abs(self._trip_target - self.x()) / ticks)
        self._trip_x = float(self.x())
        self._trip_item, self._trip_pause_s = item, pause_s
        self.carrying = None
        self._trip_phase = "out"
        self.set_mode("trip")
        self._frame_timer.setInterval(max(60, self.cfg.frame_interval_ms // 2))  # legs go faster

    def _trip_return(self) -> None:
        if self._trip_phase != "away":
            return  # the trip was called off meanwhile
        self._set_state(PetState.RUN)
        self._trip_phase = "back"
        self.facing = -self.facing
        self._trip_target = self._trip_home
        self.carrying = self._trip_item
        self.update()

    def drop_item(self) -> None:
        self.carrying = None
        self.update()

    # --- extras: emotes, ground line, hops and perching ---------------------------------
    def emote(self, text: str, colour: str = "#ff5a7a") -> None:
        """Float a little symbol (♥, a cookie...) up from the head."""
        self._emotes.append((text, colour, time.monotonic()))
        self.update()

    def ground_y(self) -> int:
        """Window y that puts the feet on the taskbar (or Dock)."""
        return self._screen_geometry().bottom() - self.height() + 1

    def hop_to(self, target: QtCore.QPoint, duration: float = 0.45) -> None:
        self.facing = 1 if target.x() >= self.x() else -1
        self._hop = (self.pos(), target, time.monotonic(), duration)
        self._set_state(PetState.RUN)

    def _hop_tick(self) -> None:
        start, end, t0, duration = self._hop
        k = min(1.0, (time.monotonic() - t0) / duration)
        arc = -4 * k * (1 - k) * max(60, abs(end.y() - start.y()) * 0.4 + 40)  # jump up, then land
        self.move(round(start.x() + (end.x() - start.x()) * k), round(start.y() + (end.y() - start.y()) * k + arc))
        self.moved.emit(self.geometry())
        if k >= 1.0:
            self._hop = None
            self.set_mode(self.mode)  # back to the right pose for whatever mode it's in

    def _try_perch(self) -> bool:
        found = self.perch_provider() if self.perch_provider else None
        if not found:
            return False
        hwnd, rect = found
        avail = self._screen_geometry()
        if rect.width() < self.width() * 3 or rect.top() < avail.top() + self.height() or rect.top() > avail.bottom() - 120:
            return False
        x = max(rect.left(), min(self.x(), rect.right() - self.width()))
        self._perch = (hwnd, rect)
        self._perch_until = time.monotonic() + random.uniform(20, 60)
        self.hop_to(QtCore.QPoint(x, rect.top() - self.height() + self.GROUND))
        return True

    def leave_perch(self, instant: bool = False) -> None:
        if self._perch is None and self._hop is None:
            return
        self._perch = None
        ground = QtCore.QPoint(self.x(), self.ground_y())
        if instant:
            self._hop = None
            self.move(ground)
            self.moved.emit(self.geometry())
        else:
            self.hop_to(ground, 0.55)

    @property
    def perched(self) -> bool:
        return self._perch is not None

    def _perch_tick(self) -> None:
        """Every ~0.4 s: follow the window if it moved, jump down if it's gone or time's up."""
        self._perch_check += 1
        if self._perch_check % 10:
            return
        hwnd, _ = self._perch
        rect = self.perch_lookup(hwnd) if self.perch_lookup else None
        if rect is None or time.monotonic() > self._perch_until:
            self.leave_perch()
            return
        self._perch = (hwnd, rect)
        y = rect.top() - self.height() + self.GROUND
        x = max(rect.left(), min(self.x(), rect.right() - self.width()))
        if (x, y) != (self.x(), self.y()):
            self.move(x, y)
            self.moved.emit(self.geometry())

    def wake_up(self) -> None:
        if self.mode is None and self.state == PetState.SLEEP:
            self._set_state(PetState.IDLE)
            self._schedule_behaviour()

    def _set_state(self, state: str) -> None:
        self.state = state
        self._frame = 0
        if state == PetState.WALK_LEFT:
            self.facing = -1
        elif state == PetState.WALK_RIGHT:
            self.facing = 1
        self.update()

    def _schedule_behaviour(self, factor: float = 1.0) -> None:
        delay = random.uniform(self.cfg.behaviour_min_s, self.cfg.behaviour_max_s) * factor
        self._behaviour_timer.start(int(delay * 1000))

    def _choose_behaviour(self) -> None:
        if self.mode is not None or self._hop is not None:
            return
        if self._perch is None and random.random() < 0.08 and self._try_perch():
            return  # hopping up onto a window
        state = random.choices(
            [PetState.IDLE, PetState.WALK_LEFT, PetState.WALK_RIGHT, PetState.SLEEP],
            weights=[35, 25, 25, 15],
        )[0]
        self._set_state(state)
        # Naps last longer than the other behaviours.
        self._schedule_behaviour(factor=3.0 if state == PetState.SLEEP else 1.0)
        if self.sprites.kind.stunts and random.random() < self.STUNT_CHANCE:
            self.stunt_wanted.emit()  # the controller starts it if nothing else is going on

    # --- timers --------------------------------------------------------------------
    def _on_move_tick(self) -> None:
        if self._stunt_phase is not None:
            self._stunt_tick()
            return
        if self._hop is not None and not self._dragging:
            self._hop_tick()
            return
        if self._perch is not None and not self._dragging:
            self._perch_tick()
            if self._perch is None:
                return
        if self._trip_phase in ("out", "back") and not self._dragging:
            step = self._trip_speed * (1 if self._trip_target > self._trip_x else -1)
            if abs(self._trip_target - self._trip_x) <= self._trip_speed:
                self._trip_x = float(self._trip_target)
            else:
                self._trip_x += step
            self.move(round(self._trip_x), self.y())
            self.moved.emit(self.geometry())
            if self._trip_x == self._trip_target:
                if self._trip_phase == "out":
                    self._trip_phase = "away"  # out of sight, "finding" the item
                    self._set_state(PetState.IDLE)
                    self.trip_reached.emit()
                    QtCore.QTimer.singleShot(int(self._trip_pause_s * 1000), self._trip_return)
                else:
                    self._trip_phase = None
                    self._frame_timer.setInterval(self.cfg.frame_interval_ms)
                    self._set_state(PetState.IDLE)
                    self.trip_done.emit()
            return
        if self._dragging or self.state not in (PetState.WALK_LEFT, PetState.WALK_RIGHT):
            return
        avail = self._screen_geometry()
        if self._perch is not None:  # walk along the window's top edge instead
            avail = QtCore.QRect(self._perch[1].left(), avail.top(), self._perch[1].width(), avail.height())
        x = self.x() + self.facing * self.cfg.walk_speed_px
        # Turn around at the screen edges.
        if x <= avail.left():
            x = avail.left()
            self._set_state(PetState.WALK_RIGHT)
        elif x + self.width() >= avail.right():
            x = avail.right() - self.width()
            self._set_state(PetState.WALK_LEFT)
        self.move(x, self.y())
        self.moved.emit(self.geometry())

    def _on_frame_tick(self) -> None:
        self._frame += 1
        if self.state == PetState.SMASH:
            name, prev = (self.sprites.frame_name("smash", i) for i in (self._frame, self._frame - 1))
            if name == "smash_down" and prev != "smash_down":
                self.paw_down.emit()
        self.update()

    def _screen_geometry(self) -> QtCore.QRect:
        screen = QtGui.QGuiApplication.screenAt(self.geometry().center())
        return (screen or QtGui.QGuiApplication.primaryScreen()).availableGeometry()

    # --- painting ------------------------------------------------------------------
    def _snap(self, value: float) -> float:
        """Align a logical coordinate to the device pixel grid so sprite pixels stay crisp."""
        dpr = self.sprites.dpr
        return round(value * dpr) / dpr

    def paintEvent(self, event) -> None:
        animation = {
            PetState.WALK_LEFT: "walk",
            PetState.WALK_RIGHT: "walk",
            PetState.SLEEP: "sleep",
            PetState.ALERT: "alert",
            PetState.SMASH: "smash",
            PetState.RUN: "walk",
            PetState.ZIP: "zip",
            PetState.FLY: "fly",
        }.get(self.state, "idle")
        pix = self.sprites.frame(animation, self.facing, self._frame)
        w, h = self.sprites.size.width(), self.sprites.size.height()
        bounce = 6 if (self.state == PetState.ALERT and self._frame % 2) else 0
        x = self._snap((self.width() - w) / 2)
        y = self._snap(self.height() - self.GROUND - h - bounce)

        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)

        # Soft ground shadow (shrinks while hopping).
        shadow_w = w * (0.55 if bounce else 0.65)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QtGui.QColor(0, 0, 0, 55))
        if self._stunt_phase in (None, "shoot"):  # no ground shadow in mid-air
            p.drawEllipse(QtCore.QPointF(x + w / 2, self.height() - self.GROUND - 2), shadow_w / 2, 4)

        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, False)
        p.drawPixmap(QtCore.QPointF(x, y), pix)

        # Positions are in art cells (with padding); mirror when facing left.
        kind = self.sprites.kind
        cells = len(kind.art["stand"][0]) + 2
        cell = w / cells
        head_x = x + cell * (kind.head_col if self.facing == 1 else cells - kind.head_col)
        if self.hat and self.state not in (PetState.SLEEP, PetState.FLY):
            hat = self.sprites.hats[self.hat]
            size = hat.deviceIndependentSize()
            hx = x + cell * (kind.hat[0] if self.facing == 1 else cells - kind.hat[0])
            top = y + cell * kind.hat[1] - size.height() + cell  # the art's padding cell sits below the brim
            p.save()
            p.translate(self._snap(hx), self._snap(top))
            if self.facing == -1:
                p.scale(-1, 1)
            p.drawPixmap(QtCore.QPointF(self._snap(-size.width() / 2), 0), hat)
            p.restore()
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        if self.carrying is not None:
            # Held in the mouth, just in front of the muzzle.
            size = cell * 4.8
            mouth_x = x + cell * (kind.mouth[0] if self.facing == 1 else cells - kind.mouth[0])
            mouth_y = y + cell * kind.mouth[1]
            p.drawPixmap(QtCore.QRectF(mouth_x - size / 2, mouth_y - size / 3, size, size),
                         self.carrying, QtCore.QRectF(self.carrying.rect()))
        if self.state == PetState.ALERT:
            p.setFont(self.ui_font.make(18, QtGui.QFont.Weight.Black))
            p.setPen(QtGui.QColor("#e5484d"))
            p.drawText(QtCore.QRectF(head_x - 10, y - 30, 20, 28), Qt.AlignmentFlag.AlignCenter, "!")
        elif self.state == PetState.SLEEP:
            # Three drifting z's, phase shifted by the frame counter.
            p.setPen(QtGui.QColor("#6c7a89"))
            base_y = y + h * 0.4
            for i in range(3):
                phase = (self._frame + i * 3) % 9
                p.setFont(self.ui_font.make(8 + phase, QtGui.QFont.Weight.Bold))
                p.drawText(QtCore.QPointF(head_x + phase * 2 - 4, base_y - phase * 4), "z")

        # Floating emotes (hearts, a cookie...), rising and fading over EMOTE_S.
        now = time.monotonic()
        self._emotes = [e for e in self._emotes if now - e[2] < self.EMOTE_S]
        p.setFont(self.ui_font.make(13, QtGui.QFont.Weight.Black))
        for i, (text, colour, t0) in enumerate(self._emotes):
            age = (now - t0) / self.EMOTE_S
            c = QtGui.QColor(colour)
            c.setAlphaF(max(0.0, 1.0 - age ** 2))
            p.setPen(c)
            dx = (i % 3 - 1) * 10
            p.drawText(QtCore.QRectF(head_x - 16 + dx, y - 22 - age * 26, 32, 22), Qt.AlignmentFlag.AlignCenter, text)

        if self.badge:  # pill at the very top, e.g. the focus countdown
            p.setFont(self.ui_font.make(9, QtGui.QFont.Weight.Bold))
            fm = QtGui.QFontMetrics(p.font())
            bw, bh = fm.horizontalAdvance(self.badge) + 14, 18
            bx = min(max(1.0, head_x - bw / 2), self.width() - bw - 1)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QtGui.QColor(28, 28, 30, 215))
            p.drawRoundedRect(QtCore.QRectF(bx, 2, bw, bh), bh / 2, bh / 2)
            p.setPen(QtGui.QColor("#f5f5f7"))
            p.drawText(QtCore.QRectF(bx, 2, bw, bh), Qt.AlignmentFlag.AlignCenter, self.badge)
        p.end()

    # --- mouse ---------------------------------------------------------------------
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_global = event.globalPosition().toPoint()
            self._press_pos = self.pos()
            self._dragging = False

    def mouseMoveEvent(self, event) -> None:
        if self._press_global is None or self._stunt_phase is not None:
            return
        delta = event.globalPosition().toPoint() - self._press_global
        if not self._dragging and delta.manhattanLength() > 4:
            self._dragging = True  # past the threshold this is a drag, not a click
        if self._dragging:
            self.move(self._press_pos + delta)
            self.moved.emit(self.geometry())

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            if self._swallow_release:
                self._swallow_release = False  # second release of a double-click
            elif not self._dragging:
                self._click_timer.start()
            self._press_global = None
            self._dragging = False

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._click_timer.stop()
            self._swallow_release = True
            self._press_global = None
            self.double_clicked.emit()

    def contextMenuEvent(self, event) -> None:
        self.context_menu_requested.emit(event.globalPos())


# =================================================================================
# Cobra (the Cobra-mode fight)
# =================================================================================
class CobraWindow(QtWidgets.QWidget):
    """The Cobra-mode opponent. Rises out of the ground in front of the cat, sways,
    flinches with a "POW!" on every paw strike, then keels over away from the cat with
    X eyes when the cleanup finishes, lingers, fades out and reports `finished`."""

    finished = Signal()

    TOP = 32                     # room above the cobra for the impact burst
    GROUND = CatWindow.GROUND    # same ground line as the cat
    RISE_S, FALL_S, LINGER_S, FADE_S = 0.5, 0.45, 2.5, 0.7
    HIT_S = 0.16                 # how long the flinch + burst shows after a strike
    SWAY_S = 0.36                # hood sway period while fighting
    WORDS = ("POW!", "BAM!", "WHAM!")

    def __init__(self, sprites: SpriteSheet, ui_font: UIFont) -> None:
        super().__init__(None, PET_WINDOW_FLAGS)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)  # macOS hides tool windows otherwise
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)  # clicks pass through
        self.frames = sprites.cobra_frames()
        self.dpr = sprites.dpr
        self.cell = sprites.unit / sprites.dpr  # logical size of one art pixel
        size = self.frames["rear"].deviceIndependentSize()
        self._rw, self._rh = size.width(), size.height()
        # Wide enough for the cobra lying flat on either side of where it stood.
        half = self._rh + self._rw / 2 + 2 * self.cell
        self.setFixedSize(math.ceil(2 * half), math.ceil(self._rh + self.TOP + self.GROUND))
        self._font = ui_font.make(13, QtGui.QFont.Weight.Black)
        self._away = 1        # +1: the cat is on our left, so we fall to the right
        self._phase = "rise"  # rise -> fight -> fall -> dead -> fade
        self._t0 = time.monotonic()
        self._hit_at = -10.0
        self._hits = 0
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(33)  # ~30 FPS while on screen
        self._timer.timeout.connect(self._tick)

    # --- choreography ----------------------------------------------------------------
    def appear(self, cat_geometry: QtCore.QRect, facing: int, cat_sprite_w: float) -> None:
        """Stand just in front of the cat's face, on the same ground line."""
        self._away = facing
        cx = cat_geometry.center().x() + facing * (cat_sprite_w / 2 + self._rw / 2 - 3 * self.cell)
        self.move(round(cx - self.width() / 2), cat_geometry.bottom() - self.height() + 1)
        self._phase, self._t0 = "rise", time.monotonic()
        self.setWindowOpacity(1.0)
        self.show()
        self.raise_()
        self._timer.start()

    def hit(self) -> None:
        if self._phase in ("rise", "fight"):
            self._phase = "fight"
            self._hit_at = time.monotonic()
            self._hits += 1

    def defeat(self) -> None:
        if self._phase in ("rise", "fight"):
            self._phase, self._t0 = "fall", time.monotonic()

    def _tick(self) -> None:
        e = time.monotonic() - self._t0
        if self._phase == "rise" and e >= self.RISE_S:
            self._phase, self._t0 = "fight", time.monotonic()
        elif self._phase == "fall" and e >= self.FALL_S:
            self._phase, self._t0 = "dead", time.monotonic()
        elif self._phase == "dead" and e >= self.LINGER_S:
            self._phase, self._t0 = "fade", time.monotonic()
        elif self._phase == "fade":
            if e >= self.FADE_S:
                self._timer.stop()
                self.hide()
                self.finished.emit()
                return
            self.setWindowOpacity(1.0 - e / self.FADE_S)
        self.update()

    # --- painting --------------------------------------------------------------------
    def _snap(self, v: float) -> float:
        return round(v * self.dpr) / self.dpr

    def paintEvent(self, event) -> None:
        e = time.monotonic() - self._t0
        bottom = self.height() - self.GROUND
        cx = self.width() / 2
        standing = self._phase in ("rise", "fight")
        pivot = cx + self._away * self._rw / 2  # base corner it tips over on

        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QtGui.QColor(0, 0, 0, 55))
        # The shadow slides from under the standing cobra to under the fallen one as it topples.
        fallen = 0.0 if standing else 1.0
        if self._phase == "fall":
            fallen = min(1.0, e / self.FALL_S) ** 2
        shadow_cx = cx + (pivot + self._away * self._rh / 2 - cx) * fallen
        shadow_w = self._rw * 0.75 + (self._rh * 0.85 - self._rw * 0.75) * fallen
        p.drawEllipse(QtCore.QPointF(shadow_cx, bottom - 2), shadow_w / 2, 4)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, False)

        if self._phase == "rise":
            f = min(1.0, e / self.RISE_S)
            p.setClipRect(QtCore.QRectF(0, 0, self.width(), bottom))  # emerges from the ground
            p.drawPixmap(QtCore.QPointF(self._snap(cx - self._rw / 2), self._snap(bottom - self._rh * f)),
                         self.frames["rear"])
            p.setClipping(False)
        elif self._phase == "fight":
            hit = time.monotonic() - self._hit_at < self.HIT_S
            name = "hit" if hit else ("rear", "sway")[int(e / self.SWAY_S) % 2]
            p.drawPixmap(QtCore.QPointF(self._snap(cx - self._rw / 2), self._snap(bottom - self._rh)),
                         self.frames[name])
            if hit:
                self._paint_burst(p, cx, bottom)
        elif self._phase == "fall":
            f = min(1.0, e / self.FALL_S)
            p.save()
            p.translate(pivot, bottom)
            p.rotate(90 * f * f * self._away)  # accelerating topple away from the cat
            left = -self._rw if self._away > 0 else 0
            p.drawPixmap(QtCore.QPointF(left, -self._rh), self.frames["rear"])
            p.restore()
        else:  # dead / fade: flat on the ground where it fell, X eyes
            pix = self.frames["dead_r" if self._away > 0 else "dead"]
            dsize = pix.deviceIndependentSize()
            left = pivot if self._away > 0 else pivot - dsize.width()
            top = bottom - dsize.height()
            p.drawPixmap(QtCore.QPointF(self._snap(left), self._snap(top)), pix)
            art_w = len(COBRA_ART["dead"][0])
            p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            p.setPen(QtGui.QPen(QtGui.QColor("#2b1a14"), max(1.5, self.cell * 0.45),
                                Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            s = self.cell * 0.55
            for col, row in COBRA_DEAD_EYES:
                col = art_w - col if self._away > 0 else col  # mirrored frame
                ex, ey = left + (1 + col) * self.cell, top + (1 + row) * self.cell
                p.drawLine(QtCore.QPointF(ex - s, ey - s), QtCore.QPointF(ex + s, ey + s))
                p.drawLine(QtCore.QPointF(ex - s, ey + s), QtCore.QPointF(ex + s, ey - s))
        p.end()

    def _paint_burst(self, p: QtGui.QPainter, cx: float, bottom: float) -> None:
        """Impact star on the hood (cat's side) plus a comic-book word above it."""
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        ix = cx - self._away * self._rw * 0.3   # the hood's edge facing the cat
        iy = bottom - self._rh * 0.72
        r1, r2 = self.cell * 1.3, self.cell * 2.8
        p.setPen(QtGui.QPen(QtGui.QColor("#ffd23f"), max(1.5, self.cell * 0.45),
                            Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        for k in range(8):
            a = k * math.pi / 4 + 0.35
            p.drawLine(QtCore.QPointF(ix + math.cos(a) * r1, iy + math.sin(a) * r1),
                       QtCore.QPointF(ix + math.cos(a) * r2, iy + math.sin(a) * r2))
        word = self.WORDS[self._hits % len(self.WORDS)]
        width = QtGui.QFontMetricsF(self._font).horizontalAdvance(word)
        path = QtGui.QPainterPath()
        path.addText(QtCore.QPointF(ix - width / 2, bottom - self._rh - 6), self._font, word)
        p.strokePath(path, QtGui.QPen(QtGui.QColor("#1c1c1e"), 4, Qt.PenStyle.SolidLine,
                                      Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.fillPath(path, QtGui.QColor("#ffffff"))


# =================================================================================
# Liquid glass base window
# =================================================================================
@dataclass(frozen=True)
class GlassStyle:
    tint: tuple[int, int, int, int]   # glass body colour over the blurred backdrop
    rim: tuple[int, int, int, int]    # inner edge highlight
    text: str
    accent: str                       # primary button fill
    accent_text: str                  # primary button label
    sheen: int = 34                   # strength of the top highlight (alpha)
    overlay: tuple[int, int, int] = (255, 255, 255)  # hover/selection/chip colour: white on dark, black on light

    def text_color(self, alpha: int = 255) -> QtGui.QColor:
        c = QtGui.QColor(self.text)
        c.setAlpha(alpha)
        return c

    def ov(self, alpha: int) -> QtGui.QColor:
        return QtGui.QColor(*self.overlay, alpha)


GLASS_THEMES = {
    "dark": {
        # Dark grey smoked glass, white text.
        "info": GlassStyle(tint=(30, 30, 32, 222), rim=(255, 255, 255, 110), text="#f5f5f7",
                           accent="#f5f5f7", accent_text="#1c1c1e"),
        # Same glass warmed with red for break reminders and Cobra mode.
        "alert": GlassStyle(tint=(46, 24, 24, 228), rim=(255, 105, 95, 190), text="#fff3f0",
                            accent="#e5484d", accent_text="#ffffff"),
    },
    "light": {
        "info": GlassStyle(tint=(246, 246, 248, 226), rim=(255, 255, 255, 200), text="#1c1c1e",
                           accent="#1c1c1e", accent_text="#ffffff", sheen=60, overlay=(0, 0, 0)),
        "alert": GlassStyle(tint=(255, 236, 234, 232), rim=(255, 255, 255, 210), text="#3a1414",
                            accent="#e5484d", accent_text="#ffffff", sheen=60, overlay=(0, 0, 0)),
    },
}


def set_glass_theme(name: str) -> None:
    """Every glass window reads GlassWindow.STYLES when it paints, so this re-themes them all."""
    GlassWindow.STYLES = GLASS_THEMES.get(name, GLASS_THEMES["dark"])


def current_theme() -> str:
    return "light" if GlassWindow.STYLES is GLASS_THEMES["light"] else "dark"


class GlassWindow(QtWidgets.QWidget):
    """Frameless, always-on-top window painted as liquid glass with a tail pointing
    down at an anchor (the cat). Subclasses draw their content after paint_glass()."""

    TAIL_H = 10
    RADIUS = 18
    SHADOW = 12            # transparent margin around the glass for the drop shadow
    OVERLAP = 6            # how far the tail dips into the cat window's empty top margin
    BLUR_DOWNSCALE = 6     # backdrop is shrunk by this factor and smoothly re-enlarged = blur
    LENS = 0.04            # backdrop magnification inside the glass (fake refraction)

    STYLES = GLASS_THEMES["dark"]   # swapped for every glass window by set_glass_theme()

    def __init__(self, flags, refresh_ms: int, activates: bool) -> None:
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)  # macOS hides tool windows otherwise
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, not activates)
        self._style = "info"
        self._tail_x = 0.0
        self._backdrop: QtGui.QImage | None = None

        # If we can hide from screen capture we can re-sample live without seeing ourselves.
        self._live_backdrop = self._exclude_from_capture()

        # Glass refresh: re-sample what's behind the window while it is visible.
        self._glass_timer = QtCore.QTimer(self)
        self._glass_timer.setInterval(refresh_ms)
        self._glass_timer.timeout.connect(self._refresh_backdrop)

    # --- backdrop ------------------------------------------------------------------
    def _exclude_from_capture(self) -> bool:
        if sys.platform != "win32":
            return False
        try:
            WDA_EXCLUDEFROMCAPTURE = 0x11  # Windows 10 2004+
            return bool(ctypes.windll.user32.SetWindowDisplayAffinity(int(self.winId()), WDA_EXCLUDEFROMCAPTURE))
        except (AttributeError, OSError):
            return False

    def _refresh_backdrop(self) -> None:
        """Grab the screen area behind the window and keep a small (= blurred) copy."""
        if sys.platform == "darwin":
            return  # would trigger the Screen Recording permission prompt; plain tint instead
        if self.isVisible() and not self._live_backdrop:
            return  # would capture ourselves
        screen = QtGui.QGuiApplication.screenAt(self.geometry().center()) or QtGui.QGuiApplication.primaryScreen()
        origin = screen.geometry().topLeft()
        shot = screen.grabWindow(0, self.x() - origin.x(), self.y() - origin.y(), self.width(), self.height())
        if shot.isNull():
            self._backdrop = None
            return
        img = shot.toImage()
        self._backdrop = img.scaled(max(1, img.width() // self.BLUR_DOWNSCALE),
                                    max(1, img.height() // self.BLUR_DOWNSCALE),
                                    Qt.AspectRatioMode.IgnoreAspectRatio,
                                    Qt.TransformationMode.SmoothTransformation)
        self.update()

    def show_glass(self) -> None:
        if not self.isVisible():
            self._refresh_backdrop()  # sample before showing, so the first frame is already glass
        self.show()
        self.raise_()

    def showEvent(self, event) -> None:
        if self._live_backdrop:
            self._glass_timer.start()

    def hideEvent(self, event) -> None:
        self._glass_timer.stop()

    # --- geometry ------------------------------------------------------------------
    def follow(self, anchor: QtCore.QRect) -> None:
        """Position the window centred above the anchor (the cat), clamped to the screen."""
        screen = QtGui.QGuiApplication.screenAt(anchor.center()) or QtGui.QGuiApplication.primaryScreen()
        avail = screen.availableGeometry()
        s = self.SHADOW
        cx = anchor.center().x()
        x = min(max(cx - self.width() // 2, avail.left() - s), avail.right() - self.width() + s + 1)
        y = max(anchor.top() + self.OVERLAP - (self.height() - s), avail.top() - s)
        self._tail_x = min(max(cx - x, s + self.RADIUS + 10), self.width() - s - self.RADIUS - 10)
        if self.isVisible() and not self._live_backdrop and self.pos() != QtCore.QPoint(x, y):
            self._backdrop = None  # stale snapshot would no longer line up; fall back to frost
        self.move(x, y)
        self.update()

    def _glass_rect(self) -> QtCore.QRectF:
        s = self.SHADOW
        return QtCore.QRectF(s, s, self.width() - 2 * s, self.height() - 2 * s - self.TAIL_H)

    def _shape(self) -> QtGui.QPainterPath:
        body = self._glass_rect()
        path = QtGui.QPainterPath()
        path.addRoundedRect(body, self.RADIUS, self.RADIUS)
        tail = QtGui.QPainterPath()
        tail.moveTo(self._tail_x - 9, body.bottom() - 2)
        tail.quadTo(self._tail_x - 2, body.bottom() + 2, self._tail_x, body.bottom() + self.TAIL_H)
        tail.quadTo(self._tail_x + 2, body.bottom() + 2, self._tail_x + 9, body.bottom() - 2)
        tail.closeSubpath()
        return path.united(tail)

    # --- painting ------------------------------------------------------------------
    def paint_glass(self, p: QtGui.QPainter) -> None:
        style = self.STYLES[self._style]
        rim_c = QtGui.QColor(*style.rim)
        shape = self._shape()
        body = self._glass_rect()
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform)

        # 1. Soft drop shadow, painted only outside the glass.
        outside = QtGui.QPainterPath()
        outside.addRect(QtCore.QRectF(self.rect()))
        p.setClipPath(outside.subtracted(shape))
        stroker = QtGui.QPainterPathStroker()
        for i in range(1, self.SHADOW // 2 + 1):
            stroker.setWidth(i * 4)
            p.fillPath(stroker.createStroke(shape).translated(0, 3), QtGui.QColor(0, 0, 0, 8))

        # 2. Blurred, slightly magnified backdrop = the "liquid" lens.
        p.setClipPath(shape)
        if self._backdrop is not None:
            mx, my = self.width() * self.LENS, self.height() * self.LENS
            p.drawImage(QtCore.QRectF(self.rect()).adjusted(-mx, -my, mx, my), self._backdrop)

        # 3. Tint, top sheen and a soft caustic glow near the bottom edge.
        p.fillPath(shape, QtGui.QColor(*style.tint))
        sheen = QtGui.QLinearGradient(0, body.top(), 0, body.bottom())
        sheen.setColorAt(0.0, QtGui.QColor(255, 255, 255, style.sheen))
        sheen.setColorAt(0.45, QtGui.QColor(255, 255, 255, style.sheen // 5))
        sheen.setColorAt(1.0, QtGui.QColor(255, 255, 255, 0))
        p.fillPath(shape, sheen)
        glow = QtGui.QRadialGradient(body.right() - body.width() * 0.25, body.bottom(), body.width() * 0.45)
        glow.setColorAt(0.0, QtGui.QColor(255, 255, 255, style.sheen // 2))
        glow.setColorAt(1.0, QtGui.QColor(255, 255, 255, 0))
        p.fillPath(shape, glow)

        # 4. Inner rim: brightest top-left, fading, catching light again bottom-right.
        rim_grad = QtGui.QLinearGradient(body.topLeft(), body.bottomRight())
        rim_grad.setColorAt(0.0, rim_c)
        rim_grad.setColorAt(0.5, QtGui.QColor(rim_c.red(), rim_c.green(), rim_c.blue(), 20))
        rim_grad.setColorAt(1.0, QtGui.QColor(rim_c.red(), rim_c.green(), rim_c.blue(), rim_c.alpha() // 2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QtGui.QPen(QtGui.QBrush(rim_grad), 3))  # clipped: only the inner 1.5px shows
        p.drawPath(shape)
        p.setClipping(False)

        # 5. Hairline outer edge so the glass separates from light backgrounds.
        p.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0, 40), 1))
        p.drawPath(shape)


# =================================================================================
# Speech bubble
# =================================================================================
class SpeechBubble(GlassWindow):
    clicked = Signal()      # body clicked
    dismissed = Signal()    # "x" clicked
    chosen = Signal(str)    # a choice button was clicked (its key)

    PAD = 14
    CLOSE = 16
    MAX_TEXT_W = 250
    BUTTON_H = 32
    BUTTON_GAP = 8

    def __init__(self, ui_font: UIFont, refresh_ms: int) -> None:
        super().__init__(PET_WINDOW_FLAGS, refresh_ms, activates=False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)
        self._title = ""
        self._body = ""
        self._buttons: list[tuple[str, str]] = []   # (key, label); the first one is primary
        self._button_widths: list[int] = []
        self._hover = -1
        self._title_font = ui_font.make(12, QtGui.QFont.Weight.Bold)
        self._body_font = ui_font.make(11)
        self._button_font = ui_font.make(11, QtGui.QFont.Weight.DemiBold)
        self._text_w = self._title_h = self._body_h = 0

        # Auto-hide timer for transient messages (not used for alerts).
        self._hide_timer = QtCore.QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)

    @property
    def has_buttons(self) -> bool:
        return self.isVisible() and bool(self._buttons)

    @property
    def button_keys(self) -> list[str]:
        """Keys of the last message's buttons (still set after the bubble hides)."""
        return [key for key, _ in self._buttons]

    def show_message(self, body: str, anchor: QtCore.QRect, title: str = "",
                     style: str = "info", timeout_ms: int = 0,
                     buttons: list[tuple[str, str]] | tuple = (), owner: str = "cat") -> None:
        self.owner = owner  # which pet window the bubble follows ("cat" or "buddy")
        self._title, self._body, self._style = title, body, style
        self._buttons = list(buttons)
        self._hover = -1
        self._relayout()
        self.follow(anchor)
        self.show_glass()
        if timeout_ms:
            self._hide_timer.start(timeout_ms)
        else:
            self._hide_timer.stop()

    def _relayout(self) -> None:
        flags = int(Qt.TextFlag.TextWordWrap.value)
        box = QtCore.QRect(0, 0, self.MAX_TEXT_W, 2000)
        title_r = (QtGui.QFontMetrics(self._title_font).boundingRect(box, flags, self._title)
                   if self._title else QtCore.QRect())
        body_r = (QtGui.QFontMetrics(self._body_font).boundingRect(box, flags, self._body)
                  if self._body else QtCore.QRect())
        button_fm = QtGui.QFontMetrics(self._button_font)
        self._button_widths = [button_fm.horizontalAdvance(label) + 30 for _, label in self._buttons]
        # Buttons flow into rows no wider than the text column (at least the widest button).
        limit = max([self.MAX_TEXT_W + 100] + self._button_widths)
        self._button_pos: list[tuple[int, int]] = []  # (x offset, row) per button
        x = row = 0
        for w in self._button_widths:
            if x and x + w > limit:
                x, row = 0, row + 1
            self._button_pos.append((x, row))
            x += w + self.BUTTON_GAP
        buttons_w = max([pos[0] + w for pos, w in zip(self._button_pos, self._button_widths)] or [0])
        rows = (self._button_pos[-1][1] + 1) if self._buttons else 0
        self._text_w = max(title_r.width(), body_r.width(), buttons_w, 60)
        self._title_h = title_r.height() + (4 if self._title else 0)
        self._body_h = body_r.height()
        buttons_h = rows * (self.BUTTON_H + self.BUTTON_GAP) + 4 if self._buttons else 0
        glass_w = self.PAD * 2 + self._text_w + self.CLOSE + 4
        glass_h = self.PAD * 2 + self._title_h + self._body_h + buttons_h + self.TAIL_H
        self.setFixedSize(glass_w + 2 * self.SHADOW, glass_h + 2 * self.SHADOW)

    def _button_rects(self) -> list[QtCore.QRectF]:
        body = self._glass_rect()
        x = body.left() + self.PAD
        y = body.top() + self.PAD + self._title_h + self._body_h + (12 if self._title or self._body else 2)
        return [QtCore.QRectF(x + dx, y + row * (self.BUTTON_H + self.BUTTON_GAP), w, self.BUTTON_H)
                for (dx, row), w in zip(self._button_pos, self._button_widths)]

    def _close_rect(self) -> QtCore.QRectF:
        body = self._glass_rect()
        return QtCore.QRectF(body.right() - self.PAD - self.CLOSE + 6, body.top() + 8, self.CLOSE, self.CLOSE)

    def paintEvent(self, event) -> None:
        style = self.STYLES[self._style]
        body = self._glass_rect()
        p = QtGui.QPainter(self)
        self.paint_glass(p)

        # Dismiss "x" on a small glass chip.
        cr = self._close_rect()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(style.ov(38))
        p.drawEllipse(cr)
        x_rect = cr.adjusted(5, 5, -5, -5)
        p.setPen(QtGui.QPen(style.text_color(220), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(x_rect.topLeft(), x_rect.bottomRight())
        p.drawLine(x_rect.topRight(), x_rect.bottomLeft())

        # Text.
        flags = int(Qt.TextFlag.TextWordWrap.value)
        left, top = int(body.left()) + self.PAD, int(body.top()) + self.PAD
        p.setPen(style.text_color())
        if self._title:
            p.setFont(self._title_font)
            p.drawText(QtCore.QRect(left, top, self._text_w, self._title_h), flags, self._title)
        p.setFont(self._body_font)
        p.drawText(QtCore.QRect(left, top + self._title_h, self._text_w, self._body_h), flags, self._body)

        # Choice buttons: the first is a solid primary pill, the rest are glass pills.
        p.setFont(self._button_font)
        for i, (rect, (_, label)) in enumerate(zip(self._button_rects(), self._buttons)):
            primary = i == 0
            fill = QtGui.QColor(style.accent) if primary else style.ov(28)
            if i == self._hover:
                fill = fill.darker(112) if primary else style.ov(60)
            p.setPen(QtGui.QPen(style.ov(0 if primary else 70), 1))
            p.setBrush(fill)
            p.drawRoundedRect(rect, self.BUTTON_H / 2, self.BUTTON_H / 2)
            p.setPen(QtGui.QColor(style.accent_text) if primary else style.text_color())
            p.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), label)
        p.end()

    def _button_at(self, pos: QtCore.QPointF) -> int:
        for i, rect in enumerate(self._button_rects()):
            if rect.contains(pos):
                return i
        return -1

    def mouseMoveEvent(self, event) -> None:
        hover = self._button_at(event.position())
        if hover != self._hover:
            self._hover = hover
            self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position()
        if not self._shape().contains(pos):
            return  # click landed in the transparent shadow margin
        button = self._button_at(pos)
        if button >= 0:
            self.hide()
            self.chosen.emit(self._buttons[button][0])
            return
        if self._close_rect().adjusted(-4, -4, 4, 4).contains(pos):
            self.hide()
            self.dismissed.emit()
            return
        if self._buttons:
            return  # a question is open: only its buttons or "x" answer it
        self.hide()
        self.clicked.emit()


# =================================================================================
# Fetch popup (search box + live results)
# =================================================================================
class FetchPopup(GlassWindow):
    fetched = Signal(str, str)   # (action, path); open / reveal / copy / drag / openwith / value / clip
    launched = Signal(str, str)  # (app target, app name): an app result was activated
    pins_changed = Signal()
    closed = Signal()

    GLASS_W = 470
    PAD = 16
    INPUT_H = 38
    ROW_H = 46
    FOOTER_H = 28
    HINT = "↵ go to / launch  ·  Shift+↵ copy path  ·  Ctrl+↵ open  ·  right-click for more"
    CLIP_HINT = "↵ copy back to the clipboard  ·  kept until you quit, never saved"
    APPS_ON_TOP = 3      # app matches shown above file results (all of them with "app:")
    PLACEHOLDERS = {"files": "Fetch a file, or sum: 12*7 · 5 km in miles",
                    "clip": "Search what you've copied…"}

    def __init__(self, index: FileIndex, apps: AppIndex, cfg: Config, ui_font: UIFont) -> None:
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
        super().__init__(flags, cfg.glass_refresh_ms, activates=True)
        self.index = index
        self.apps = apps
        self._tile_font = ui_font.make(28, QtGui.QFont.Weight.Bold)
        self.cfg = cfg
        self._anchor = QtCore.QRect()
        self._results: list[Hit] = []
        self._sel = 0
        self._press_row = -1
        self._press_pos = None
        self._was_active = False
        self._name_font = ui_font.make(11, QtGui.QFont.Weight.DemiBold)
        self._path_font = ui_font.make(9.5)
        self._hint_font = ui_font.make(8.5)
        self._icon_provider = QtWidgets.QFileIconProvider()
        self._icon_cache: dict[str, QtGui.QIcon] = {}

        self.mode = "files"                  # "files" or "clip" (clipboard history)
        self.pins: list[dict] = []           # {"path", "name", "is_dir", "kind"}, shown first on an empty search
        self.recent: list[dict] = []         # last fetched items, newest first
        self.clips_provider = lambda: []     # -> list of copied texts, newest first

        self.edit = QtWidgets.QLineEdit(self)
        self.edit.setFont(ui_font.make(14))
        self.edit.setFrame(False)
        self.restyle()
        self.edit.installEventFilter(self)

        # Debounce: search a moment after typing pauses, not on every keystroke.
        self._debounce = QtCore.QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(cfg.search_debounce_ms)
        self._debounce.timeout.connect(self._run_search)
        self.edit.textChanged.connect(self._debounce.start)
        index.progress.connect(self._on_index_progress)
        index.changed.connect(self._on_index_progress)
        apps.changed.connect(self._on_index_progress)

    def restyle(self) -> None:
        """Text colours for the current glass theme."""
        style = self.STYLES["info"]
        self.edit.setStyleSheet(f"QLineEdit {{ background: transparent; border: none; color: {style.text}; }}")
        palette = self.edit.palette()
        palette.setColor(QtGui.QPalette.ColorRole.PlaceholderText, style.text_color(120))
        self.edit.setPalette(palette)
        self.update()

    # --- open / close ----------------------------------------------------------------
    def open_at(self, anchor: QtCore.QRect, mode: str = "files") -> None:
        self._anchor = anchor
        self.mode = mode
        self.edit.setPlaceholderText(self.PLACEHOLDERS[mode])
        self.edit.blockSignals(True)
        self.edit.clear()
        self.edit.blockSignals(False)
        self._run_search()  # an empty box shows pins and recent items, or the clipboard
        self.show_glass()
        force_foreground(self)
        self.edit.setFocus(Qt.FocusReason.PopupFocusReason)

    def close_popup(self) -> None:
        if self.isVisible():
            self.hide()
            self.closed.emit()

    def changeEvent(self, event) -> None:
        # Clicking anywhere else closes the popup, like a menu.
        if event.type() == QtCore.QEvent.Type.ActivationChange:
            if self.isActiveWindow():
                self._was_active = True
            elif self._was_active and self.isVisible():
                self._was_active = False
                self.close_popup()
        super().changeEvent(event)

    # --- search ----------------------------------------------------------------------
    def _run_search(self) -> None:
        text = self.edit.text().strip()
        limit = self.cfg.fetch_max_results
        if self.mode == "clip" or text.lower().startswith("clip:"):
            self._results = self._clip_hits(text[5:].strip() if text.lower().startswith("clip:") else text, limit)
        elif not text:
            self._results = self._saved_hits(limit)
        else:
            answer = self._answer(text)
            room = limit - len(answer)
            apps = self.apps.search(text, room if "app:" in text.lower() else self.APPS_ON_TOP)
            files = self.index.search(text, room - len(apps))
            self._results = answer + apps + files
        self._sel = 0
        self._relayout()

    @staticmethod
    def _answer(text: str) -> list[Hit]:
        """A sum or a unit conversion, shown as the top row."""
        converted = convert_units(text)
        if converted:
            value = fmt_number(converted[0])
            return [Hit(value, f"= {value} {converted[1]}", False, "calc", "Unit conversion · ↵ copy")]
        value = safe_calc(text)
        if value is not None:
            return [Hit(fmt_number(value), f"= {fmt_number(value)}", False, "calc", "Calculator · ↵ copy")]
        return []

    def _clip_hits(self, query: str, limit: int) -> list[Hit]:
        q = query.lower()
        hits = []
        for text in self.clips_provider():
            if q in text.lower():
                first = text.strip().splitlines()[0] if text.strip() else text
                lines = text.count("\n") + 1
                note = f"{len(text):,} characters" + (f" · {lines} lines" if lines > 1 else "")
                hits.append(Hit(text, first[:120], False, "clip", note))
        return hits[:limit]

    def _saved_hits(self, limit: int) -> list[Hit]:
        """Pinned items, then recent ones, skipping files that no longer exist."""
        hits, seen = [], set()
        for items, tag in ((self.pins, "📌 Pinned"), (self.recent, "🕘 Recent")):
            for item in items:
                key = item["path"].lower()
                if key in seen or (item["kind"] != "app" and not os.path.exists(item["path"])):
                    continue
                seen.add(key)
                hits.append(Hit(item["path"], item["name"], item.get("is_dir", False), item["kind"], tag))
        return hits[:limit]

    def is_pinned(self, path: str) -> bool:
        return any(p["path"].lower() == path.lower() for p in self.pins)

    def toggle_pin(self, hit: Hit) -> None:
        if self.is_pinned(hit.path):
            self.pins[:] = [p for p in self.pins if p["path"].lower() != hit.path.lower()]
        else:
            self.pins.insert(0, {"path": hit.path, "name": hit.name, "is_dir": hit.is_dir, "kind": hit.kind})
        self.pins_changed.emit()
        if not self.edit.text().strip():
            self._run_search()

    def _on_index_progress(self, *_) -> None:
        if self.isVisible():
            if self.edit.text().strip() and not self._results:
                self._run_search()
            self.update()

    def _status_line(self) -> str:
        n = self.index.count()
        if self.mode == "clip":
            return ("Nothing copied yet. Copy some text and it shows up here." if not self.edit.text().strip()
                    else "Nothing like that on the clipboard. 😿")
        if self.index.scanning:
            return f"Sniffing around your drives… {fmt_count(n)} items so far 🐾"
        if not self.edit.text().strip():
            return f"Type a name. I know {fmt_count(n)} files and folders and {len(self.apps.apps)} apps."
        return "Nothing found. 😿 Try fewer words, or ext:pdf, folder:, app:, !exclude"

    # --- layout ----------------------------------------------------------------------
    def _rows_top(self) -> float:
        return self._glass_rect().top() + self.PAD + self.INPUT_H + 8

    def _relayout(self) -> None:
        rows = max(1, len(self._results))
        glass_h = self.PAD + self.INPUT_H + 8 + rows * self.ROW_H + self.FOOTER_H + 6
        self.setFixedSize(self.GLASS_W + 2 * self.SHADOW, glass_h + self.TAIL_H + 2 * self.SHADOW)
        g = self._glass_rect()
        self.edit.setGeometry(int(g.left()) + self.PAD, int(g.top()) + self.PAD - 4,
                              int(g.width()) - 2 * self.PAD, self.INPUT_H)
        self.follow(self._anchor)

    def _icon_for(self, hit: Hit) -> QtGui.QIcon:
        """The real shell icon. Cached per extension, except types whose icon is per file."""
        if hit.kind in ("calc", "clip"):
            key = "<" + hit.kind + ">"
            if key not in self._icon_cache:
                self._icon_cache[key] = self._letter_tile("=" if hit.kind == "calc" else "¶")
            return self._icon_cache[key]
        if hit.kind == "app":
            key = "app:" + hit.path.lower()
            if key not in self._icon_cache:
                self._icon_cache[key] = (self._icon_provider.icon(QtCore.QFileInfo(hit.path))
                                         if not hit.path.startswith("shell:") else self._letter_tile(hit.name))
            return self._icon_cache[key]
        ext = os.path.splitext(hit.name)[1].lower()
        key = "<dir>" if hit.is_dir else (hit.path.lower() if ext in (".exe", ".lnk", ".ico", ".url") else ext)
        if key not in self._icon_cache:
            self._icon_cache[key] = self._icon_provider.icon(QtCore.QFileInfo(hit.path))
        return self._icon_cache[key]

    def _letter_tile(self, name: str) -> QtGui.QIcon:
        """Icon for Store apps without a shortcut: a coloured rounded tile with the first letter."""
        pix = QtGui.QPixmap(64, 64)
        pix.fill(Qt.GlobalColor.transparent)
        p = QtGui.QPainter(pix)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QtGui.QColor.fromHsl(sum(map(ord, name)) * 37 % 360, 140, 110))
        p.drawRoundedRect(QtCore.QRectF(4, 4, 56, 56), 14, 14)
        p.setPen(QtGui.QColor("#ffffff"))
        p.setFont(self._tile_font)
        p.drawText(QtCore.QRectF(4, 4, 56, 56), int(Qt.AlignmentFlag.AlignCenter), name[:1].upper())
        p.end()
        return QtGui.QIcon(pix)

    def _row_rect(self, r: int) -> QtCore.QRectF:
        g = self._glass_rect()
        return QtCore.QRectF(g.left() + 8, self._rows_top() + r * self.ROW_H, g.width() - 16, self.ROW_H - 4)

    def _row_at(self, pos: QtCore.QPointF) -> int:
        for r in range(len(self._results)):
            if self._row_rect(r).contains(pos):
                return r
        return -1

    # --- painting --------------------------------------------------------------------
    def paintEvent(self, event) -> None:
        style = self.STYLES[self._style]
        fg, dim = style.text_color(), style.text_color(140)
        g = self._glass_rect()
        p = QtGui.QPainter(self)
        self.paint_glass(p)

        # Divider under the input.
        y = self._rows_top() - 5
        p.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0, 70), 1))
        p.drawLine(QtCore.QPointF(g.left() + self.PAD, y), QtCore.QPointF(g.right() - self.PAD, y))
        p.setPen(QtGui.QPen(style.ov(40), 1))
        p.drawLine(QtCore.QPointF(g.left() + self.PAD, y + 1), QtCore.QPointF(g.right() - self.PAD, y + 1))

        if not self._results:
            p.setFont(self._path_font)
            p.setPen(dim)
            p.drawText(self._row_rect(0).adjusted(10, 0, -10, 0),
                       int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft), self._status_line())

        name_fm = QtGui.QFontMetrics(self._name_font)
        path_fm = QtGui.QFontMetrics(self._path_font)
        for r, hit in enumerate(self._results):
            row = self._row_rect(r)
            if r == self._sel:
                p.setPen(QtGui.QPen(style.ov(55), 1))
                p.setBrush(style.ov(30))
                p.drawRoundedRect(row, 11, 11)
            self._icon_for(hit).paint(p, QtCore.QRect(int(row.left()) + 9, int(row.top()) + 8, 26, 26))
            p.setPen(fg)
            text_x = row.left() + 42
            text_w = int(row.width() - 52)
            p.setFont(self._name_font)
            p.drawText(QtCore.QRectF(text_x, row.top() + 4, text_w, 20), int(Qt.AlignmentFlag.AlignVCenter),
                       name_fm.elidedText(hit.name, Qt.TextElideMode.ElideRight, text_w))
            p.setFont(self._path_font)
            p.setPen(dim)
            if hit.kind == "app":
                folder = "Application"
            elif hit.kind in ("calc", "clip"):
                folder = ""
            else:
                folder = hit.path.rsplit(SEP, 1)[0] if SEP in hit.path else hit.path
            if hit.note:
                folder = hit.note + (f"  ·  {folder}" if folder else "")
            p.drawText(QtCore.QRectF(text_x, row.top() + 22, text_w, 18), int(Qt.AlignmentFlag.AlignVCenter),
                       path_fm.elidedText(folder, Qt.TextElideMode.ElideMiddle, text_w))

        # Footer: key hints + index size.
        foot = QtCore.QRectF(g.left() + self.PAD, g.bottom() - self.FOOTER_H - 2,
                             g.width() - 2 * self.PAD, self.FOOTER_H)
        p.setFont(self._hint_font)
        p.setPen(dim)
        p.drawText(foot, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                   self.CLIP_HINT if self.mode == "clip" else self.HINT)
        if self._results and self.mode == "files" and self.edit.text().strip():
            p.drawText(foot, int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight),
                       f"{fmt_count(self.index.count())} indexed")
        p.end()

    # --- keyboard / mouse ------------------------------------------------------------
    def eventFilter(self, obj, event) -> bool:
        if obj is self.edit and event.type() == QtCore.QEvent.Type.KeyPress:
            key, mods = event.key(), event.modifiers()
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up) and self._results:
                step = 1 if key == Qt.Key.Key_Down else -1
                self._sel = (self._sel + step) % len(self._results)
                self.update()
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                if self._debounce.isActive():  # typed fast and hit Enter: search first
                    self._debounce.stop()
                    self._run_search()
                if mods & Qt.KeyboardModifier.ControlModifier:
                    self._activate("open")
                elif mods & Qt.KeyboardModifier.ShiftModifier:
                    self._activate("copy")
                else:
                    self._activate("reveal")  # default: go to the location, don't open the file
                return True
            if key == Qt.Key.Key_Escape:
                self.close_popup()
                return True
        return super().eventFilter(obj, event)

    def _activate(self, action: str, row: int | None = None) -> None:
        row = self._sel if row is None else row
        if not 0 <= row < len(self._results):
            return
        hit = self._results[row]
        self.hide()
        if hit.kind == "calc":
            self.fetched.emit("value", hit.path)    # answers are copied
        elif hit.kind == "clip":
            self.fetched.emit("clip", hit.path)     # clipboard history: put it back on the clipboard
        elif hit.kind == "app" and action not in ("copy", "openwith"):
            self.launched.emit(hit.path, hit.name)  # apps launch on any activation
        else:
            self.fetched.emit(action, hit.path)

    def contextMenuEvent(self, event) -> None:
        row = self._row_at(QtCore.QPointF(event.pos()))
        if row < 0:
            return
        self._sel = row
        self.update()
        hit = self._results[row]
        menu = QtWidgets.QMenu(self)
        if hit.kind in ("calc", "clip"):
            menu.addAction("Copy", lambda: self._activate("copy", row))
        else:
            if hit.kind == "app":
                menu.addAction("Launch", lambda: self._activate("reveal", row))
            else:
                menu.addAction("Go to location", lambda: self._activate("reveal", row))
                menu.addAction("Open", lambda: self._activate("open", row))
                if not hit.is_dir:
                    menu.addAction("Open with…", lambda: self._activate("openwith", row))
            menu.addAction("Copy path", lambda: self._activate("copy", row))
            menu.addSeparator()
            menu.addAction("Unpin" if self.is_pinned(hit.path) else "📌 Pin to the top", lambda: self.toggle_pin(hit))
        self._was_active = False  # the menu takes focus; that isn't "clicked elsewhere"
        menu.exec(event.globalPos())
        if self.isVisible():
            force_foreground(self)
            self.edit.setFocus()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_row = self._row_at(event.position())
            self._press_pos = event.position()
            if self._press_row >= 0:
                self._sel = self._press_row
                self.update()

    def mouseMoveEvent(self, event) -> None:
        # Drag a result out of the popup = drop the real file into Explorer, mail, chat...
        if self._press_row < 0 or self._press_pos is None:
            return
        if (event.position() - self._press_pos).manhattanLength() < QtWidgets.QApplication.startDragDistance():
            return
        hit = self._results[self._press_row]
        path = hit.path
        self._press_row = -1
        if path.startswith("shell:") or hit.kind in ("calc", "clip"):
            return  # Store apps, answers and clipboard text have no file to drag
        mime = QtCore.QMimeData()
        mime.setUrls([QtCore.QUrl.fromLocalFile(path)])
        mime.setText(path)
        drag = QtGui.QDrag(self)
        drag.setMimeData(mime)
        self._was_active = False  # the drag takes focus away; don't treat it as "clicked elsewhere"
        drag.exec(Qt.DropAction.CopyAction | Qt.DropAction.LinkAction)
        self.hide()
        self.fetched.emit("drag", path)

    def mouseReleaseEvent(self, event) -> None:
        self._press_row = -1
        self._press_pos = None

    def mouseDoubleClickEvent(self, event) -> None:
        row = self._row_at(event.position())
        if row >= 0:
            self._activate("reveal", row)


# =================================================================================
# Quick note popup (type a note or "call bank at 4pm")
# =================================================================================
class NotePopup(GlassWindow):
    saved = Signal(str)      # the typed text
    deleted = Signal(str)    # id of a note or reminder to remove
    closed = Signal()

    GLASS_W = 430
    PAD = 16
    INPUT_H = 38
    ROW_H = 30
    FOOTER_H = 24
    MAX_ROWS = 8

    def __init__(self, cfg: Config, ui_font: UIFont) -> None:
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
        super().__init__(flags, cfg.glass_refresh_ms, activates=True)
        self.items_provider = lambda: []   # -> [(id, line)], reminders first
        self._anchor = QtCore.QRect()
        self._was_active = False
        self._rows: list[tuple[str, str]] = []
        self._row_font = ui_font.make(10)
        self._hint_font = ui_font.make(8.5)
        self.edit = QtWidgets.QLineEdit(self)
        self.edit.setFont(ui_font.make(13))
        self.edit.setFrame(False)
        self.edit.setPlaceholderText("Note it, or: call bank at 4pm · tea in 20 min")
        self.edit.installEventFilter(self)
        self.edit.textChanged.connect(self.update)
        self.restyle()

    def restyle(self) -> None:
        style = self.STYLES["info"]
        self.edit.setStyleSheet(f"QLineEdit {{ background: transparent; border: none; color: {style.text}; }}")
        palette = self.edit.palette()
        palette.setColor(QtGui.QPalette.ColorRole.PlaceholderText, style.text_color(120))
        self.edit.setPalette(palette)
        self.update()

    def open_at(self, anchor: QtCore.QRect) -> None:
        self._anchor = anchor
        self.edit.clear()
        self.refresh()
        self.show_glass()
        force_foreground(self)
        self.edit.setFocus(Qt.FocusReason.PopupFocusReason)

    def refresh(self) -> None:
        self._rows = list(self.items_provider())[:self.MAX_ROWS]
        glass_h = self.PAD + self.INPUT_H + 26 + max(1, len(self._rows)) * self.ROW_H + self.FOOTER_H + 6
        self.setFixedSize(self.GLASS_W + 2 * self.SHADOW, glass_h + self.TAIL_H + 2 * self.SHADOW)
        g = self._glass_rect()
        self.edit.setGeometry(int(g.left()) + self.PAD, int(g.top()) + self.PAD - 4,
                              int(g.width()) - 2 * self.PAD, self.INPUT_H)
        self.follow(self._anchor)

    def close_popup(self) -> None:
        if self.isVisible():
            self.hide()
            self.closed.emit()

    def changeEvent(self, event) -> None:
        if event.type() == QtCore.QEvent.Type.ActivationChange:
            if self.isActiveWindow():
                self._was_active = True
            elif self._was_active and self.isVisible():
                self._was_active = False
                self.close_popup()
        super().changeEvent(event)

    def _preview(self) -> str:
        text = self.edit.text().strip()
        if not text:
            return "Press ↵ to save. Times like 4pm or 'in 20 min' become reminders."
        when, what = parse_note(text, datetime.now())
        if when is None:
            return f"📝 Note: {what}"
        day = "" if when.date() == datetime.now().date() else when.strftime("%a ")
        return f"⏰ Reminder {day}at {fmt_clock(when.strftime('%H:%M'))}: {what}"

    def _row_rect(self, r: int) -> QtCore.QRectF:
        g = self._glass_rect()
        return QtCore.QRectF(g.left() + 8, g.top() + self.PAD + self.INPUT_H + 26 + r * self.ROW_H,
                             g.width() - 16, self.ROW_H - 2)

    def _delete_rect(self, r: int) -> QtCore.QRectF:
        row = self._row_rect(r)
        return QtCore.QRectF(row.right() - 26, row.top() + 4, 20, row.height() - 8)

    def paintEvent(self, event) -> None:
        style = self.STYLES[self._style]
        fg, dim = style.text_color(), style.text_color(140)
        g = self._glass_rect()
        p = QtGui.QPainter(self)
        self.paint_glass(p)
        p.setFont(self._hint_font)
        p.setPen(dim)
        top = g.top() + self.PAD + self.INPUT_H
        p.drawText(QtCore.QRectF(g.left() + self.PAD, top - 2, g.width() - 2 * self.PAD, 20),
                   int(Qt.AlignmentFlag.AlignVCenter), self._preview())
        p.setPen(QtGui.QPen(style.ov(40), 1))
        p.drawLine(QtCore.QPointF(g.left() + self.PAD, top + 22), QtCore.QPointF(g.right() - self.PAD, top + 22))
        p.setFont(self._row_font)
        fm = QtGui.QFontMetrics(self._row_font)
        if not self._rows:
            p.setPen(dim)
            p.drawText(self._row_rect(0).adjusted(8, 0, 0, 0), int(Qt.AlignmentFlag.AlignVCenter),
                       "No notes yet.")
        for r, (_, line) in enumerate(self._rows):
            row = self._row_rect(r)
            p.setPen(fg)
            p.drawText(row.adjusted(8, 0, -34, 0), int(Qt.AlignmentFlag.AlignVCenter),
                       fm.elidedText(line, Qt.TextElideMode.ElideRight, int(row.width() - 42)))
            p.setPen(dim)
            p.drawText(self._delete_rect(r), int(Qt.AlignmentFlag.AlignCenter), "×")
        p.setFont(self._hint_font)
        p.setPen(dim)
        p.drawText(QtCore.QRectF(g.left() + self.PAD, g.bottom() - self.FOOTER_H - 2, g.width() - 2 * self.PAD,
                                 self.FOOTER_H), int(Qt.AlignmentFlag.AlignVCenter),
                   "↵ save  ·  × delete  ·  Esc close")
        p.end()

    def eventFilter(self, obj, event) -> bool:
        if obj is self.edit and event.type() == QtCore.QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                text = self.edit.text().strip()
                if text:
                    self.hide()
                    self.saved.emit(text)
                    self.closed.emit()
                return True
            if event.key() == Qt.Key.Key_Escape:
                self.close_popup()
                return True
        return super().eventFilter(obj, event)

    def mousePressEvent(self, event) -> None:
        for r, (item_id, _) in enumerate(self._rows):
            if self._delete_rect(r).adjusted(-4, -4, 4, 4).contains(event.position()):
                self.deleted.emit(item_id)
                self.refresh()
                self.update()
                return


# =================================================================================
# Play: the ball
# =================================================================================
class BallWindow(QtWidgets.QWidget):
    """A click-through ball that is thrown in an arc and bounces once."""

    landed = Signal()

    def __init__(self, pixmap: QtGui.QPixmap) -> None:
        super().__init__(None, PET_WINDOW_FLAGS)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.pixmap = pixmap
        size = pixmap.deviceIndependentSize()
        self.setFixedSize(math.ceil(size.width()), math.ceil(size.height()))
        self._path: tuple[QtCore.QPoint, QtCore.QPoint, float, float] | None = None
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)

    def throw(self, start: QtCore.QPoint, end: QtCore.QPoint, duration: float = 0.8) -> None:
        """start/end are where the ball's bottom centre is."""
        self._path = (start, end, time.monotonic(), duration)
        self._tick()
        self.show()
        self.raise_()
        self._timer.start()

    def _tick(self) -> None:
        start, end, t0, duration = self._path
        k = min(1.0, (time.monotonic() - t0) / duration)
        if k < 0.75:  # the throw: a tall arc
            q = k / 0.75
            lift = 4 * q * (1 - q) * 140
            x = start.x() + (end.x() - start.x()) * q * 0.85
            y = start.y() + (end.y() - start.y()) * q - lift
        else:         # one small bounce
            q = (k - 0.75) / 0.25
            x = start.x() + (end.x() - start.x()) * (0.85 + 0.15 * q)
            y = end.y() - 4 * q * (1 - q) * 30
        self.move(round(x - self.width() / 2), round(y - self.height()))
        if k >= 1.0:
            self._timer.stop()
            self.landed.emit()

    def paintEvent(self, event) -> None:
        p = QtGui.QPainter(self)
        p.drawPixmap(0, 0, self.pixmap)
        p.end()


# =================================================================================
# System helpers: clipboard history, global hotkeys, full-screen check, window tops,
# sounds, screenshots, "Open with", daily stats
# =================================================================================
class ClipboardHistory(QtCore.QObject):
    """Remembers copied text for this session only (never written to disk). Skips anything
    a password manager marks as private."""

    image_arrived = Signal(QtGui.QImage)
    PRIVATE_MARKERS = ("ExcludeClipboardContentFromMonitorProcessing", "Clipboard Viewer Ignore",
                       "CanIncludeInClipboardHistory", "org.nspasteboard.ConcealedType",
                       "org.nspasteboard.TransientType")
    LIMIT = 25
    MAX_CHARS = 20_000

    def __init__(self) -> None:
        super().__init__()
        self.items: list[str] = []
        self.enabled = True
        self._clipboard = QtGui.QGuiApplication.clipboard()
        self._clipboard.dataChanged.connect(self._changed)

    def _changed(self) -> None:
        md = self._clipboard.mimeData()
        if md is None:
            return
        formats = " ".join(md.formats())
        if any(marker in formats for marker in self.PRIVATE_MARKERS):
            return
        if md.hasImage():
            image = self._clipboard.image()
            if not image.isNull():
                self.image_arrived.emit(image)
            return
        if self.enabled and md.hasText():
            self.add(md.text())

    def add(self, text: str) -> None:
        if not text.strip() or len(text) > self.MAX_CHARS:
            return
        self.items = [text] + [t for t in self.items if t != text]
        del self.items[self.LIMIT:]

    def clear(self) -> None:
        self.items.clear()


class GlobalHotkeys(QtCore.QAbstractNativeEventFilter):
    """System-wide shortcuts (Windows): RegisterHotKey + WM_HOTKEY through Qt's event loop."""

    # id -> (action, [(virtual key, label), ...]): the first free combination wins.
    KEYS = {1: ("fetch", [(0x20, "Ctrl+Alt+Space"), (0x46, "Ctrl+Alt+F")]),
            2: ("clip", [(0x56, "Ctrl+Alt+V")]),
            3: ("note", [(0x4E, "Ctrl+Alt+N")])}
    MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, WM_HOTKEY = 0x1, 0x2, 0x4000, 0x0312

    def __init__(self, on_hotkey) -> None:
        super().__init__()
        self.on_hotkey = on_hotkey
        self.registered: dict[int, str | None] = {}   # id -> the label in use, or None if every option was taken

    @property
    def supported(self) -> bool:
        return sys.platform == "win32"

    def enable(self) -> None:
        if not self.supported or self.registered:
            return
        user32 = ctypes.windll.user32
        for key_id, (_, options) in self.KEYS.items():
            self.registered[key_id] = None
            for vk, label in options:
                if user32.RegisterHotKey(None, key_id, self.MOD_CONTROL | self.MOD_ALT | self.MOD_NOREPEAT, vk):
                    self.registered[key_id] = label
                    break

    def disable(self) -> None:
        if not self.supported:
            return
        for key_id, label in self.registered.items():
            if label:
                ctypes.windll.user32.UnregisterHotKey(None, key_id)
        self.registered.clear()

    def label(self, action: str) -> str:
        """The shortcut in use for an action, or "" if none."""
        for key_id, (name, _) in self.KEYS.items():
            if name == action:
                return self.registered.get(key_id) or ""
        return ""

    def status(self) -> list[tuple[str, str, bool | None]]:
        """(keys, action, True registered / False taken by other apps / None off)."""
        out = []
        for key_id, (action, options) in self.KEYS.items():
            if key_id not in self.registered:
                out.append((options[0][1], action, None))
            else:
                label = self.registered[key_id]
                out.append((label or " / ".join(o[1] for o in options), action, bool(label)))
        return out

    def nativeEventFilter(self, event_type, message):
        if self.supported and self.registered and bytes(event_type) == b"windows_generic_MSG":
            import ctypes.wintypes
            msg = ctypes.wintypes.MSG.from_address(int(message))
            if msg.message == self.WM_HOTKEY and self.registered.get(msg.wParam):
                action = self.KEYS[msg.wParam][0]
                QtCore.QTimer.singleShot(0, lambda: self.on_hotkey(action))
                return True, 0
        return False, 0


def fullscreen_busy() -> bool:
    """True while a full-screen app, game or presentation runs (Windows)."""
    if sys.platform != "win32":
        return False
    try:
        state = ctypes.c_int(0)
        if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
            return False
        return state.value in (2, 3, 4)  # QUNS_BUSY, QUNS_RUNNING_D3D_FULL_SCREEN, QUNS_PRESENTATION_MODE
    except (AttributeError, OSError):
        return False


def window_top_rect(hwnd: int) -> QtCore.QRect | None:
    """A normal, visible window's frame in Qt coordinates (primary screen only), or None."""
    if sys.platform != "win32":
        return None
    import ctypes.wintypes
    user32 = ctypes.windll.user32
    if not hwnd or not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd) \
            or user32.IsIconic(hwnd) or user32.IsZoomed(hwnd):
        return None
    rect = ctypes.wintypes.RECT()
    cloaked = ctypes.c_int(0)
    dwm = ctypes.windll.dwmapi.DwmGetWindowAttribute
    if dwm(hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect)) != 0:  # DWMWA_EXTENDED_FRAME_BOUNDS
        return None
    dwm(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))     # DWMWA_CLOAKED (other desktop etc.)
    if cloaked.value:
        return None
    screen = QtGui.QGuiApplication.primaryScreen()
    dpr = screen.devicePixelRatio()
    qrect = QtCore.QRect(round(rect.left / dpr), round(rect.top / dpr),
                         round((rect.right - rect.left) / dpr), round((rect.bottom - rect.top) / dpr))
    return qrect if screen.geometry().contains(qrect.topLeft()) else None


def foreground_window(own: set[int]) -> tuple[int, QtCore.QRect] | None:
    """The active window to perch on, unless it's ours, the desktop or the taskbar."""
    if sys.platform != "win32":
        return None
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd or hwnd in own:
        return None
    name = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, name, 64)
    if name.value in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
        return None
    rect = window_top_rect(hwnd)
    return (hwnd, rect) if rect else None


# Per-pet reminder sounds: (start Hz, end Hz, seconds, wave); 0 Hz = a pause. Generated, not recorded.
PET_SOUNDS = {
    "cat": [(620, 900, 0.09, "sine"), (900, 700, 0.12, "sine")],
    "retriever": [(260, 200, 0.09, "square"), (0, 0, 0.06, ""), (280, 210, 0.11, "square")],
    "hamster": [(1500, 2000, 0.06, "sine"), (0, 0, 0.03, ""), (1700, 2200, 0.07, "sine")],
    "bunny": [(180, 90, 0.08, "sine"), (0, 0, 0.05, ""), (900, 1200, 0.05, "sine")],
    "penguin": [(330, 300, 0.16, "square")],
    "hero": [(523, 523, 0.08, "sine"), (659, 659, 0.08, "sine"), (784, 784, 0.16, "sine")],
}


def synth_wav(notes: list[tuple[float, float, float, str]], rate: int = 22050) -> bytes:
    """A short mono 16-bit WAV from tone sweeps, with soft edges so it doesn't click."""
    samples = array("h")
    phase = 0.0
    for f0, f1, seconds, wave_kind in notes:
        n = int(rate * seconds)
        for i in range(n):
            if f0 <= 0:
                samples.append(0)
                continue
            freq = f0 + (f1 - f0) * i / max(1, n - 1)
            phase += 2 * math.pi * freq / rate
            s = math.sin(phase)
            if wave_kind == "square":
                s = 0.6 * (1 if s >= 0 else -1) * 0.5 + 0.5 * s
            edge = min(1.0, i / (rate * 0.008), (n - i) / (rate * 0.02))
            samples.append(int(s * edge * 0.35 * 32767))
    if sys.byteorder != "little":
        samples.byteswap()  # WAV is little-endian
    data = samples.tobytes()
    return struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(data), b"WAVE", b"fmt ", 16, 1, 1, rate, rate * 2,
                       2, 16, b"data", len(data)) + data


def sound_file(pet: str) -> Path:
    path = app_data_dir() / "sounds" / f"{pet}.wav"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(synth_wav(PET_SOUNDS.get(pet, PET_SOUNDS["cat"])))
    return path


def play_sound(path: Path) -> None:
    try:
        if sys.platform == "win32":
            import winsound
            winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        elif sys.platform == "darwin":
            subprocess.Popen(["afplay", str(path)])
    except (OSError, RuntimeError):
        pass


def start_screen_snip() -> bool:
    """Open the system's area-snip tool; the result lands on the clipboard."""
    try:
        if sys.platform == "win32":
            os.startfile("ms-screenclip:")
            return True
        if sys.platform == "darwin":
            subprocess.Popen(["screencapture", "-i", "-c"])
            return True
    except OSError:
        pass
    return False


def open_with(path: str) -> None:
    """The system's "Open with" chooser for a file."""
    if sys.platform == "win32":
        subprocess.Popen(["rundll32.exe", "shell32.dll,OpenAs_RunDLL", path], creationflags=CREATE_NO_WINDOW)
    elif sys.platform == "darwin":
        app = QtWidgets.QFileDialog.getExistingDirectory(None, "Open with which app?", "/Applications")
        if app.endswith(".app"):
            subprocess.Popen(["open", "-a", app, path])
    else:
        raise OSError("not supported")


class StatsBook:
    """Per-day numbers for the summary and the weekly chart: stats.json, last 60 days."""

    KEEP_DAYS = 60
    FIELDS = ("screen_s", "breaks", "water", "focus", "done")

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.days = {k: v for k, v in data.get("days", {}).items() if isinstance(v, dict)}
        except (OSError, ValueError, AttributeError):
            self.days = {}

    def day(self, date) -> dict:
        entry = self.days.setdefault(date.isoformat(), {})
        for f in self.FIELDS:
            entry.setdefault(f, 0)
        return entry

    def add(self, field_name: str, amount: float, now: datetime) -> None:
        self.day(now.date())[field_name] += amount

    def week(self, now: datetime) -> list[tuple[datetime, dict]]:
        out = []
        for back in range(6, -1, -1):
            d = (now - timedelta(days=back)).date()
            out.append((d, dict(self.days.get(d.isoformat(), {}))))
        return out

    def save(self) -> None:
        cutoff = (datetime.now() - timedelta(days=self.KEEP_DAYS)).date().isoformat()
        self.days = {k: v for k, v in self.days.items() if k >= cutoff}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"days": self.days}), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass


def mood_label(mood: float) -> str:
    if mood >= 80:
        return "😊 Happy"
    if mood >= 50:
        return "🙂 Content"
    if mood >= 25:
        return "😕 A bit down"
    return "😢 Sad"


SAD_CHATTER = ["I'd feel better if you took a break. 🥺", "*sighs*", "A walk would cheer us both up.",
               "Some water? For me? (For you.)"]
TREAT_LINES = ["Nom nom! 🍪", "Crunchy! ♥", "Best. Treat. Ever.", "*happy munching*"]


# =================================================================================
# App controller - wires tracker, cat, bubble and fetch together
# =================================================================================
class DesktopPet(QtCore.QObject):
    def __init__(self, cfg: Config) -> None:
        super().__init__()
        self.cfg = cfg
        self.ui_font = ui_font = load_ui_font()
        self._settings_path = Path(cfg.settings_path) if cfg.settings_path else app_data_dir() / "settings.json"
        self.opts = dict(DEFAULT_OPTIONS)
        self.pins: list[dict] = []
        self.recent: list[dict] = []
        self.notes: list[dict] = []                    # {"id", "text"}
        self.reminders: list[dict] = []                # {"id", "text", "at": ISO time}
        self.routines = self._load_settings()          # also applies the saved pet, size, sitting limit and options
        set_glass_theme(self.opts["theme"])
        self.settings_window: SettingsWindow | None = None
        self._look_pending = False                     # a pet/size change waiting for the pet to finish a task
        self.dpr = QtGui.QGuiApplication.primaryScreen().devicePixelRatio()
        self.sprites = self._make_sprites(cfg.pet)
        self.cat = CatWindow(self.sprites, cfg, ui_font)
        self.bubble = SpeechBubble(ui_font, cfg.glass_refresh_ms)
        self.bubble.owner = "cat"
        self.tracker = ActivityTracker(cfg)
        self.index = FileIndex(cfg)
        self.apps = AppIndex(cfg.apps_refresh_min)
        self.fetch = FetchPopup(self.index, self.apps, cfg, ui_font)
        self.fetch.pins, self.fetch.recent = self.pins, self.recent
        self.notes_popup = NotePopup(cfg, ui_font)
        self.notes_popup.items_provider = self._note_rows
        self.clipboard = ClipboardHistory()
        self.clipboard.enabled = self.opts["clipboard"]
        self.fetch.clips_provider = lambda: self.clipboard.items
        self.hotkeys = GlobalHotkeys(self._on_hotkey)
        data_dir = self._settings_path.parent
        self.stats = StatsBook(data_dir / "stats.json")
        self.cobra = CobraCleaner(cfg)
        self.buddy: CatWindow | None = None            # the optional second pet
        self._ball: BallWindow | None = None
        self._playing = False                          # the pet is fetching the ball
        self._treats: list[float] = []                 # times of recent treats (at most 5 an hour)
        self.focus: dict | None = None                 # {"phase": "focus"/"break", "left": seconds}
        self._fullscreen = False                       # hiding for a full-screen app
        self._snip_until = 0.0                         # waiting for a screenshot to land on the clipboard
        self._snip_image: QtGui.QImage | None = None
        self._last_tick = time.monotonic()
        self._tick_count = 0
        self._cobra_plan: CleanupPlan | None = None   # plan waiting for the user's "Strike!"
        self._arena: CobraWindow | None = None        # the cobra on screen during a fight
        self._fight_t0 = 0.0
        self._last_reminder = None

        # Daily routine.
        self._now = datetime.now                       # swappable in tests
        self._later: dict[str, datetime] = {}          # routine id -> when its "Later" is up
        self._every_mark: dict[str, float] = {}        # routine id -> tracker.active_s when it last fired
        self._state_path = self.index.cache_path.parent / "state.json"
        self._state = self._load_state()               # e.g. {"lunch": "2026-11-04"} = already shown today
        try:
            self.mood = max(0.0, min(100.0, float(self._state.get("mood", 70))))
        except (TypeError, ValueError):
            self.mood = 70.0

        # While alerting, swap in a fresh reminder every alert_rotate_s.
        self._rotate_timer = QtCore.QTimer(self)
        self._rotate_timer.setInterval(cfg.alert_rotate_s * 1000)
        self._rotate_timer.timeout.connect(self._show_alert_bubble)

        # After "x" on an alert, bring the bubble back after snooze_s.
        self._snooze_timer = QtCore.QTimer(self)
        self._snooze_timer.setSingleShot(True)
        self._snooze_timer.timeout.connect(self._resume_alert)

        # Occasional small talk while wandering.
        self._chatter_timer = QtCore.QTimer(self)
        self._chatter_timer.setSingleShot(True)
        self._chatter_timer.timeout.connect(self._chatter)

        self.menu = self._build_menu()
        self.tray = self._build_tray()

        self.tracker.alert_triggered.connect(self._enter_alert)
        self.tracker.user_away.connect(self._on_away)
        self.tracker.user_returned.connect(self._on_returned)
        self.tracker.ticked.connect(self._on_tick)
        self.cat.clicked.connect(self._on_cat_clicked)
        self.cat.double_clicked.connect(self._ask_action)
        self.cat.moved.connect(self._on_cat_moved)
        self.cat.context_menu_requested.connect(self.menu.popup)
        self.bubble.clicked.connect(self._on_bubble_clicked)
        self.bubble.dismissed.connect(self._on_bubble_dismissed)
        self.bubble.chosen.connect(self._on_choice)
        self.fetch.fetched.connect(self._on_fetched)
        self.fetch.launched.connect(self._on_launched)
        self.fetch.closed.connect(self._release_cat)
        self.cobra.planned.connect(self._on_cobra_planned)
        self.cobra.finished.connect(self._on_cobra_finished)
        self.cat.paw_down.connect(lambda: self._arena and self._arena.hit())
        self.cat.trip_done.connect(self._on_trip_done)
        self.cat.trip_reached.connect(self._on_trip_reached)
        self.cat.stunt_wanted.connect(self._stunt)
        self.cat.stunt_done.connect(self._on_stunt_done)
        self._trip_delivery: tuple[str, str] | None = None  # (action, path) waiting for the cat
        self.fetch.pins_changed.connect(self._save_settings)
        self.notes_popup.saved.connect(self._on_note_saved)
        self.notes_popup.deleted.connect(self._on_note_deleted)
        self.notes_popup.closed.connect(self._release_cat)
        self.clipboard.image_arrived.connect(self._on_clip_image)
        self._set_perch(self.cat)
        self._update_hat()

    def _make_sprites(self, pet: str) -> SpriteSheet:
        return SpriteSheet(self.cfg.pixel_scale, self.dpr, PETS[pet], self.opts["colours"].get(pet, ""))

    def _own_windows(self) -> set[int]:
        windows = [self.cat, self.bubble, self.fetch, self.notes_popup, self.buddy, self.settings_window]
        return {int(w.winId()) for w in windows if w is not None}

    def _set_perch(self, pet: CatWindow) -> None:
        on = self.opts["perch"] and sys.platform == "win32"
        pet.perch_provider = (lambda: foreground_window(self._own_windows())) if on else None
        pet.perch_lookup = window_top_rect if on else None
        if not on:
            pet.leave_perch()

    def start(self) -> None:
        self.cat.start()
        self.tracker.start()
        self.index.start()
        self.apps.start()
        app = QtCore.QCoreApplication.instance()
        app.installNativeEventFilter(self.hotkeys)
        app.aboutToQuit.connect(self._on_quit)
        if self.opts["hotkeys"]:
            self.hotkeys.enable()
            self._label_hotkeys()
        self._apply_companion()
        self._schedule_chatter()
        lines = [f"🚶 no sitting over {fmt_span(self.cfg.alert_after_s)}"] if self.cfg.walk_on else []
        active = [r for r in self.routines if r["on"]]
        lines += [f"{r['title'].rstrip('!')}: {routine_when(r)}" for r in active[:5]]
        if len(active) > 5:
            lines.append(f"…and {len(active) - 5} more")
        self._say("\n".join(lines) + "\n\nDouble-click me for tricks.", 8000, title="Hi! I'll nudge you: 🐾")

    def wake_from_launch(self) -> None:
        """Another launch (e.g. the taskbar pin) while running: just say hello."""
        if not self.cat.busy:
            self._say("I'm already here! 🐾\nDouble-click me for tricks.", 3500)

    # --- menu / tray ---------------------------------------------------------------
    def _build_menu(self) -> QtWidgets.QMenu:
        menu = QtWidgets.QMenu()
        self._status_action = menu.addAction("Session: 0s")
        self._status_action.setEnabled(False)
        menu.addSeparator()
        self._hotkey_actions = {
            "fetch": (menu.addAction("", self._open_fetch), "🔎 Fetch a file…"),
            "clip": (menu.addAction("", lambda: self._open_fetch("clip")), "📋 Clipboard history…"),
            "note": (menu.addAction("", self._open_note), "📝 Notes and reminders…"),
        }
        self._label_hotkeys()
        self._focus_action = menu.addAction("🍅 Start focus", self._toggle_focus)
        menu.addAction("📸 Screenshot", self._snip)
        play = menu.addMenu("🐾 Play")
        play.addAction("🍪 Give a treat", self._treat)
        play.addAction("🎾 Throw the ball", self._play_ball)
        menu.addAction("📊 Today's summary", self._show_summary)
        menu.addAction("🐍 Cobra mode…", self._start_cobra)
        menu.addAction("Reset break timer", self._reset_timer)
        preview = menu.addMenu("Preview reminders")
        preview.aboutToShow.connect(lambda: self._fill_preview(preview))  # the routine list can change
        self._fill_preview(preview)
        menu.addAction("⚙ Settings…", self.open_settings)
        menu.addAction("Rebuild file + app index", lambda: (self.index.rescan(), self.apps.refresh()))
        menu.addSeparator()
        menu.addAction("Quit", QtWidgets.QApplication.quit)
        return menu

    def _fill_preview(self, preview: QtWidgets.QMenu) -> None:
        preview.clear()
        preview.addAction(f"🚶 Walk (after {fmt_span(self.cfg.alert_after_s)} sitting)", self.tracker.force_alert)
        for routine in self.routines:
            preview.addAction(routine["title"], lambda checked=False, key=routine["id"]: self._show_routine(key))
        preview.addAction("🐍 Cobra fight (practice, deletes nothing)", self._practice_fight)
        if self.sprites.kind.stunts:
            preview.addAction("🦸 Hero stunt", self._stunt)

    def _build_tray(self):
        if not QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
            return None
        tray = QtWidgets.QSystemTrayIcon(QtGui.QIcon(self.sprites.frames["stand"][1]), self)
        tray.setToolTip("Desktop Pet")
        tray.setContextMenu(self.menu)
        tray.show()
        return tray

    # --- helpers -------------------------------------------------------------------
    def _say(self, text: str, timeout_ms: int = 4000, **kwargs) -> None:
        self.bubble.show_message(text, self.cat.geometry(), timeout_ms=timeout_ms, **kwargs)

    def _chime(self) -> None:
        if self.opts["sounds"]:
            try:
                play_sound(sound_file(self.cfg.pet))
            except OSError:
                pass

    def _quiet(self) -> bool:
        """Focus time or a full-screen app: no reminders, chatter or stunts until it's over."""
        return self._fullscreen or (self.focus is not None and self.focus["phase"] == "focus")

    def _schedule_chatter(self) -> None:
        delay = random.randint(self.cfg.chatter_min_s, self.cfg.chatter_max_s)
        self._chatter_timer.start(delay * 1000)

    def _chatter(self) -> None:
        if self.cat.mode is None and not self.bubble.isVisible() and not self._quiet():
            lines = CHATTER + list(self.sprites.kind.chatter) + [
                f"You've been at it for {fmt_duration(self.tracker.session_s)}."]
            if self.mood < 30 and random.random() < 0.5:
                lines = SAD_CHATTER
            self._say(random.choice(lines), 3500)
        self._schedule_chatter()

    def _status_text(self) -> str:
        left = max(0, self.cfg.alert_after_s - self.tracker.session_s)
        lines = [f"Active for {fmt_duration(self.tracker.session_s)}."]
        if self.cfg.walk_on:
            lines.append(f"Time to get up in {fmt_duration(left)}.")
        lines.append(f"Mood: {mood_label(self.mood)} ({round(self.mood)}%)")
        if self.notes or self.reminders:
            lines.append(f"📝 {len(self.notes)} notes · ⏰ {len(self.reminders)} reminders")
        return "\n".join(lines)

    # --- mood ------------------------------------------------------------------------
    def _mood(self, delta: float, emote: bool = True) -> None:
        self.mood = max(0.0, min(100.0, self.mood + delta))
        self._state["mood"] = round(self.mood, 1)
        if abs(delta) >= 1:
            self._save_state()
        if emote and delta >= 4:
            for i in range(3):
                QtCore.QTimer.singleShot(i * 180, lambda: self.cat.emote("♥"))
        elif emote and delta <= -2:
            self.cat.emote("…", "#8e8e93")

    # --- play: treats and the ball -----------------------------------------------------
    def _treat(self) -> None:
        if self.cat.busy:
            return
        now = time.monotonic()
        self._treats = [t for t in self._treats if now - t < 3600]
        if len(self._treats) >= 5:
            self._say("I'm full! 🐾 Maybe later.", 2500)
            return
        self._treats.append(now)
        self.cat.wake_up()
        self.cat.emote("🍪", "#c98a35")
        QtCore.QTimer.singleShot(500, lambda: self._mood(6))
        self._say(random.choice(TREAT_LINES), 2500)

    def _play_ball(self) -> None:
        if self.cat.busy or self._playing:
            return
        self.bubble.hide()
        self.cat.leave_perch(instant=True)
        self.cat.set_mode(None)
        avail = self.cat._screen_geometry()
        geo = self.cat.geometry()
        room_right, room_left = avail.right() - geo.right(), geo.left() - avail.left()
        facing = 1 if room_right >= room_left else -1
        distance = max(80, min(340, max(room_right, room_left) - 20))
        target_x = geo.x() + facing * distance
        self.cat.facing = facing
        self._playing = True
        if self._ball is None:
            self._ball = BallWindow(self.sprites.ball)
            self._ball.landed.connect(self._on_ball_landed)
        self._ball.pixmap = self.sprites.ball
        size = self.sprites.ball.deviceIndependentSize()
        self._ball.setFixedSize(math.ceil(size.width()), math.ceil(size.height()))
        ground = geo.bottom() - CatWindow.GROUND - 1
        self._ball_target = target_x
        self._ball.throw(QtCore.QPoint(geo.center().x(), ground - self.sprites.size.height() // 2),
                         QtCore.QPoint(target_x + geo.width() // 2 + facing * self.sprites.size.width() // 3, ground))
        self.cat.set_mode("busy")

    def _on_ball_landed(self) -> None:
        if not self._playing:
            return
        self.cat.set_mode(None)
        leg = max(0.35, abs(self._ball_target - self.cat.x()) / 700)
        self.cat.start_trip(self.sprites.ball, leg, 0.25, target=self._ball_target)

    def _on_trip_reached(self) -> None:
        if self._playing and self._ball is not None:
            self._ball.hide()  # picked up: now it's in the mouth

    # --- focus timer -------------------------------------------------------------------
    def _toggle_focus(self) -> None:
        if self.focus is None:
            self._start_focus()
        else:
            self._say(f"{'Focus' if self.focus['phase'] == 'focus' else 'Break'}: "
                      f"{fmt_duration(self.focus['left'])} left.", 0, title="🍅 Focus timer",
                      buttons=[("focus_keep", "Keep going"), ("focus_stop", "Stop")])

    def _start_focus(self) -> None:
        self.focus = {"phase": "focus", "left": float(self.opts["focus_min"] * 60)}
        self.bubble.hide()
        self._say(f"Focus time! I'll keep quiet for {self.opts['focus_min']} min. 🍅", 2500)
        self._update_focus_badge()

    def _stop_focus(self, message: str = "Focus stopped. 🍅") -> None:
        self.focus = None
        self._update_focus_badge()
        if message:
            self._say(message, 2500)

    def _update_focus_badge(self) -> None:
        if self.focus is None:
            self.cat.badge = ""
            self._focus_action.setText("🍅 Start focus")
        else:
            icon = "🍅" if self.focus["phase"] == "focus" else "☕"
            m, s = divmod(int(max(0, self.focus["left"])), 60)
            self.cat.badge = f"{icon} {m}:{s:02d}"
            self._focus_action.setText("🍅 Focus timer…")
        self.cat.update()

    def _focus_tick(self, dt: float) -> None:
        if self.focus is None or self.tracker.away:
            return  # away pauses the countdown
        self.focus["left"] -= dt
        if self.focus["left"] > 0:
            self._update_focus_badge()
            return
        if self.focus["phase"] == "focus":
            self.focus = None
            self._update_focus_badge()
            self.stats.add("focus", 1, self._now())
            self._mood(3)
            self._chime()
            self._say(f"Nice work. Take a {self.opts['break_min']} min break.", 0, title="🍅 Focus done!",
                      buttons=[("focus_break", "Start break ☕"), ("focus_skip", "Skip")])
        else:
            self.focus = None
            self._update_focus_badge()
            self._chime()
            self._say("Ready for another round?", 0, title="☕ Break's over",
                      buttons=[("focus_again", "Another focus 🍅"), ("focus_done", "Done for now")])

    # --- notes and one-off reminders ----------------------------------------------------
    def _open_note(self) -> None:
        if self.cat.busy:
            return
        self.bubble.hide()
        self._occupy_cat("fetch")
        self.notes_popup.open_at(self.cat.geometry())

    def _note_rows(self) -> list[tuple[str, str]]:
        rows = []
        for r in sorted(self.reminders, key=lambda r: r["at"]):
            when = datetime.fromisoformat(r["at"])
            day = "" if when.date() == self._now().date() else when.strftime("%a ")
            rows.append((r["id"], f"⏰ {day}{fmt_clock(when.strftime('%H:%M'))}  {r['text']}"))
        rows += [(n["id"], f"📝 {n['text']}") for n in self.notes]
        return rows

    def _on_note_saved(self, text: str) -> None:
        when, what = parse_note(text, self._now())
        item_id = "n" + os.urandom(4).hex()
        if when is None:
            self.notes.insert(0, {"id": item_id, "text": what})
            msg = "Noted! 📝 Click me to see how many you have."
        else:
            self.reminders.append({"id": item_id, "text": what, "at": when.isoformat(timespec="minutes")})
            day = "" if when.date() == self._now().date() else when.strftime("%A ")
            msg = f"⏰ I'll remind you {day}at {fmt_clock(when.strftime('%H:%M'))}:\n{what}"
        self._save_settings()
        self._release_cat()
        self._say(msg, 3500)

    def _on_note_deleted(self, item_id: str) -> None:
        self.notes = [n for n in self.notes if n["id"] != item_id]
        self.reminders = [r for r in self.reminders if r["id"] != item_id]
        self._save_settings()

    def _due_reminder(self, now: datetime) -> dict | None:
        for r in sorted(self.reminders, key=lambda r: r["at"]):
            try:
                if datetime.fromisoformat(r["at"]) <= now:
                    return r
            except ValueError:
                continue
        return None

    def _show_reminder(self, reminder: dict, now: datetime) -> None:
        due = datetime.fromisoformat(reminder["at"])
        late = now - due > timedelta(minutes=10) and not reminder.get("snoozed")
        body = reminder["text"] + (f"\n\n(It was due at {fmt_clock(due.strftime('%H:%M'))}.)" if late else "")
        # Ignored or closed with ×, it comes back in 10 minutes; Done removes it.
        reminder["at"] = (now + timedelta(seconds=self.cfg.clock_later_s)).isoformat(timespec="minutes")
        reminder["snoozed"] = True
        self._save_settings()
        self._chime()
        self.bubble.show_message(body, self.cat.geometry(), title="⏰ Reminder", timeout_ms=0,
                                 buttons=[(f"{reminder['id']}_rdone", "Done ✅"),
                                          (f"{reminder['id']}_rlater", f"{self.cfg.clock_later_s // 60} more min")])

    # --- screenshot helper ---------------------------------------------------------------
    def _snip(self) -> None:
        if start_screen_snip():
            self._snip_until = time.monotonic() + 90
            self._say("Pick an area… I'll catch it! 📸", 2500)
        else:
            self._say("I couldn't open the screenshot tool. 😿", 3000)

    def _on_clip_image(self, image: QtGui.QImage) -> None:
        if time.monotonic() > self._snip_until:
            return
        self._snip_until = 0.0
        self._snip_image = image
        self._say(f"It's on your clipboard ({image.width()}×{image.height()}).", 20_000, title="📸 Got it!",
                  buttons=[("snip_save", "Save to Pictures"), ("snip_keep", "Just keep it")])

    def _save_snip(self) -> None:
        if self._snip_image is None:
            return
        folder = Path(QtCore.QStandardPaths.writableLocation(
            QtCore.QStandardPaths.StandardLocation.PicturesLocation) or Path.home()) / "Desktop Pet"
        path = folder / f"Screenshot {datetime.now():%Y-%m-%d %H%M%S}.png"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if not self._snip_image.save(str(path), "PNG"):
                raise OSError("could not save")
            reveal_path(str(path))
            self._say(f"Saved! 📸\n{path.name}", 3500)
        except OSError:
            self._say("I couldn't save it. 😿 It's still on your clipboard.", 3500)
        self._snip_image = None

    # --- hotkeys, quiet mode, summary ----------------------------------------------------
    def _label_hotkeys(self) -> None:
        """Show the shortcut actually in use next to each menu item."""
        for action, (item, text) in self._hotkey_actions.items():
            keys = self.hotkeys.label(action)
            item.setText(f"{text}  ({keys})" if keys else text)

    def _on_hotkey(self, action: str) -> None:
        if self.cat.busy and not (self.fetch.isVisible() or self.notes_popup.isVisible()):
            return
        self.fetch.hide()
        self.notes_popup.hide()
        self.cat.set_mode(None)
        if action == "note":
            self._open_note()
        else:
            self._open_fetch("clip" if action == "clip" else "files")

    def _check_fullscreen(self) -> None:
        busy = self.opts["quiet_fullscreen"] and fullscreen_busy()
        if busy == self._fullscreen:
            return
        self._fullscreen = busy
        pets = [w for w in (self.cat, self.buddy) if w is not None]
        if busy:
            self.bubble.hide()
            for w in pets:
                w.hide()
        else:
            for w in pets:
                w.show()

    def _summary_text(self) -> str:
        today = self.stats.day(self._now().date())
        return (f"Screen time: {fmt_span(today['screen_s']) if today['screen_s'] >= 60 else 'under a minute'}\n"
                f"Breaks: {int(today['breaks'])}  ·  Water: {int(today['water'])}  ·  Focus: {int(today['focus'])}\n"
                f"Reminders done: {int(today['done'])}\n"
                f"Mood: {mood_label(self.mood)}")

    def _show_summary(self) -> None:
        self._say(self._summary_text(), 9000, title="📊 Today so far")

    def _on_quit(self) -> None:
        self.stats.save()
        self._save_state()
        self.hotkeys.disable()

    # --- the second pet ------------------------------------------------------------------
    def _apply_companion(self) -> None:
        key = self.opts["companion"]
        if self.buddy is not None and (not key or self.buddy.sprites.kind.key != key
                                       or self.buddy.sprites.unit != self.sprites.unit
                                       or self.buddy.sprites.colour != self.opts["colours"].get(key, "")):
            if key and self.buddy.sprites.kind.key == key:
                self.buddy.set_sprites(self._make_sprites(key))  # same pet, new size or colour
                return
            self.buddy.hide()
            self.buddy.deleteLater()
            self.buddy = None
        if not key or key not in PETS or self.buddy is not None:
            return
        buddy = CatWindow(self._make_sprites(key), self.cfg, self.ui_font)
        buddy.hat = self.cat.hat
        buddy.clicked.connect(lambda: self._say_buddy(buddy.sprites.kind.pet_line))
        buddy.double_clicked.connect(lambda: (buddy.emote("♥"), self._mood(1, emote=False)))
        buddy.context_menu_requested.connect(self.menu.popup)
        buddy.moved.connect(lambda g: self.bubble.isVisible() and self.bubble.owner == "buddy"
                            and self.bubble.follow(g))
        buddy.stunt_wanted.connect(lambda: (buddy.mode is None and not self._quiet() and buddy.start_stunt()))
        buddy.stunt_done.connect(lambda: buddy.set_mode(None))
        self._set_perch(buddy)
        self.buddy = buddy
        buddy.start()
        if self._fullscreen:
            buddy.hide()

    def _say_buddy(self, text: str) -> None:
        if self.buddy is not None and not self.bubble.has_buttons:
            self.bubble.show_message(text, self.buddy.geometry(), timeout_ms=2500, owner="buddy")

    def _buddy_tick(self) -> None:
        """Every ~10 s: if the two pets are close and both free, they say hello."""
        b = self.buddy
        if b is None or b.mode is not None or self.cat.mode is not None or random.random() > 0.35:
            return
        a_x, b_x = self.cat.geometry().center().x(), b.geometry().center().x()
        if abs(a_x - b_x) < 170 and abs(self.cat.y() - b.y()) < 40:
            self.cat.facing, b.facing = (1, -1) if a_x < b_x else (-1, 1)
            self.cat._set_state(PetState.IDLE)
            b._set_state(PetState.IDLE)
            self.cat.emote("♥")
            b.emote("♥")

    # --- seasonal hat ----------------------------------------------------------------------
    def _update_hat(self) -> None:
        hat = season_hat(self._now(), self.opts["birthday"]) if self.opts["seasonal"] else None
        for w in (self.cat, self.buddy):
            if w is not None and w.hat != hat:
                w.hat = hat
                w.update()

    # --- options (Settings > More) --------------------------------------------------------
    def set_option(self, key: str, value) -> None:
        self.opts[key] = value
        self._save_settings()
        if key == "theme":
            set_glass_theme(value)
            for w in (self.fetch, self.notes_popup):
                w.restyle()
            self.bubble.update()
            if self.settings_window is not None:
                self.settings_window.restyle()
        elif key == "clipboard":
            self.clipboard.enabled = value
            if not value:
                self.clipboard.clear()
        elif key == "hotkeys":
            self.hotkeys.enable() if value else self.hotkeys.disable()
            self._label_hotkeys()
        elif key == "perch":
            for w in (self.cat, self.buddy):
                if w is not None:
                    self._set_perch(w)
        elif key in ("seasonal", "birthday"):
            self._update_hat()
        elif key == "companion":
            self._apply_companion()
        elif key == "colours":
            self._look_pending = True
            self._apply_look()
            self._apply_companion()
        elif key == "quiet_fullscreen" and not value:
            self._check_fullscreen()

    def _occupy_cat(self, mode: str) -> None:
        """Hold the cat still for a task ("fetch" or "busy") and pause alert reminders."""
        self._rotate_timer.stop()  # don't pop reminders over the task
        self.cat.set_mode(mode)

    def _release_cat(self) -> None:
        """Task over: back to the pending alert, or to wandering."""
        if not self.cat.busy:
            return
        if self.tracker.alert_active:
            self.cat.set_mode("alert")
            self._rotate_timer.start()
        else:
            self.cat.set_mode(None)

    # --- double-click: choose a trick -------------------------------------------------
    def _ask_action(self) -> None:
        if self.cobra.busy:
            self._say("Still hissing at your caches… 🐍", 2500)
            return
        if self.cat.mode == "stunt" or self._playing:
            return  # it'll be back in a moment
        focus = "🍅 Focus" if self.focus is None else "🍅 " + ("Focus" if self.focus["phase"] == "focus" else "Break") \
            + f" {int(self.focus['left'] // 60)}m"
        self._say("", 12_000, title=f"{self.sprites.kind.hello} What should I do? 🐾",
                  buttons=[("fetch", "🔎 Fetch"), ("focus", focus), ("note", "📝 Note"), ("play", "🐾 Play"),
                           ("snip", "📸 Screenshot"), ("cobra", "🐍 Cobra mode"), ("settings", "⚙ Settings")])

    def _on_choice(self, key: str) -> None:
        simple = {
            "fetch": self._open_fetch, "cobra": self._start_cobra, "settings": self.open_settings,
            "cobra_go": self._strike, "cobra_cancel": self._cancel_cobra, "note": self._open_note,
            "focus": self._toggle_focus, "snip": self._snip, "snip_save": self._save_snip,
            "play_treat": self._treat, "play_ball": self._play_ball, "focus_break": self._start_break,
            "focus_again": self._start_focus, "focus_stop": self._stop_focus,
        }
        if key in simple:
            simple[key]()
        elif key == "play":
            self._say("", 9000, title="Play with me? 🐾",
                      buttons=[("play_ball", "🎾 Throw the ball"), ("play_treat", "🍪 Give a treat")])
        elif key in ("focus_keep", "focus_skip", "focus_done", "snip_keep"):
            if key == "focus_done":
                self._say("Good session! 🐾", 2000)
        elif key.endswith(("_rdone", "_rlater")):
            self._reminder_answer(key)
        elif key.endswith(("_ok", "_later")):
            self._routine_answer(key)

    def _start_break(self) -> None:
        self.focus = {"phase": "break", "left": float(self.opts["break_min"] * 60)}
        self._update_focus_badge()
        self._say("Break time! Stretch, water, look away. ☕", 2500)

    def _reminder_answer(self, key: str) -> None:
        item_id, answer = key.rsplit("_", 1)
        reminder = next((r for r in self.reminders if r["id"] == item_id), None)
        if reminder is None:
            return
        if answer == "rdone":
            self.reminders.remove(reminder)
            self._mood(2, emote=False)
            self.stats.add("done", 1, self._now())
            self._say("Ticked off! ✅", 2000)
        else:
            reminder["at"] = (self._now() + timedelta(seconds=self.cfg.clock_later_s)).isoformat(timespec="minutes")
            self._say(f"Okay, again in {self.cfg.clock_later_s // 60} min. ⏰", 2500)
        self._save_settings()

    # --- hero stunt ------------------------------------------------------------------
    def _stunt(self) -> None:
        """Only when nothing else is going on, and never over an open bubble."""
        if (self.sprites.kind.stunts and self.cat.mode is None and not self.tracker.away
                and not self.bubble.isVisible() and not self._quiet() and not self.cat.perched):
            self.cat.start_stunt()

    def _on_stunt_done(self) -> None:
        if self.tracker.away:
            self.cat.set_mode("away")
        else:
            self._release_cat()

    # --- settings: pet, size, routines (saved to settings.json) ----------------------
    def _load_settings(self) -> list[dict]:
        """Apply the saved pet, size and sitting limit to cfg; return the routine list.
        Command-line flags (cfg.overrides) win over anything saved."""
        cfg = self.cfg
        try:
            saved = json.loads(self._settings_path.read_text(encoding="utf-8"))
            saved = saved if isinstance(saved, dict) else {}
        except (OSError, ValueError):
            saved = {}
        if saved.get("pet") in PETS:
            cfg.pet = saved["pet"]
        if cfg.pet not in PETS:
            cfg.pet = "cat"
        cfg.walk_on = bool(saved.get("walk_on", cfg.walk_on))
        try:
            if "scale" not in cfg.overrides and "scale" in saved:
                cfg.pixel_scale = max(1, min(20, int(saved["scale"])))
            if "alert" not in cfg.overrides and "sit_minutes" in saved:
                cfg.alert_after_s = int(max(1.0, float(saved["sit_minutes"])) * 60)
        except (TypeError, ValueError):
            pass
        options = saved.get("options")
        if isinstance(options, dict):
            for key, default in DEFAULT_OPTIONS.items():
                if key in options and isinstance(options[key], type(default)):
                    self.opts[key] = options[key]
        self.opts["colours"] = {k: v for k, v in self.opts["colours"].items() if k in PETS and v in PET_COLOURS}
        if self.opts["companion"] not in PETS:
            self.opts["companion"] = ""
        if self.opts["theme"] not in GLASS_THEMES:
            self.opts["theme"] = "dark"
        for name, target in (("pins", self.pins), ("recent", self.recent)):
            for item in saved.get(name, []) if isinstance(saved.get(name), list) else []:
                if isinstance(item, dict) and isinstance(item.get("path"), str) and item.get("kind") in ("file", "app"):
                    target.append({"path": item["path"], "name": str(item.get("name") or item["path"]),
                                   "is_dir": bool(item.get("is_dir")), "kind": item["kind"]})
        for item in saved.get("notes", []) if isinstance(saved.get("notes"), list) else []:
            if isinstance(item, dict) and str(item.get("text", "")).strip():
                self.notes.append({"id": str(item.get("id") or "n" + os.urandom(4).hex()), "text": str(item["text"])})
        for item in saved.get("reminders", []) if isinstance(saved.get("reminders"), list) else []:
            try:
                datetime.fromisoformat(item["at"])
                self.reminders.append({"id": str(item["id"]), "text": str(item["text"]), "at": item["at"],
                                       "snoozed": bool(item.get("snoozed"))})
            except (KeyError, TypeError, ValueError):
                continue
        raw = saved.get("routines")
        routines = default_routines(cfg)
        self._known_defaults = [r["id"] for r in routines]
        if isinstance(raw, list):
            routines = [r for r in map(clean_routine, raw) if r]
            # Defaults added in a later version (e.g. the eye break) join an existing list once.
            known = saved.get("known_defaults", ["water", "lunch", "home"])
            ids = {r["id"] for r in routines}
            routines += [r for r in default_routines(cfg) if r["id"] not in known and r["id"] not in ids]
        for routine in routines:
            if routine["id"] == "water" and "water" in cfg.overrides:
                routine["minutes"] = cfg.water_every_s / 60
            if routine["id"] == "lunch" and "lunch" in cfg.overrides:
                routine["time"] = cfg.lunch_time
            if routine["id"] == "home" and "home" in cfg.overrides:
                routine["time"] = cfg.home_time
        return routines

    def _save_settings(self) -> None:
        if self.cfg.overrides:
            return  # flags like --demo are for this run only; don't save their fast timers
        data = {"pet": self.cfg.pet, "scale": self.cfg.pixel_scale, "walk_on": self.cfg.walk_on,
                "sit_minutes": self.cfg.alert_after_s / 60, "routines": self.routines,
                "known_defaults": self._known_defaults, "options": self.opts, "pins": self.pins,
                "recent": self.recent, "notes": self.notes, "reminders": self.reminders}
        try:
            self._settings_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._settings_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self._settings_path)
        except OSError:
            pass

    def open_settings(self) -> None:
        if self.settings_window is None:
            self.settings_window = SettingsWindow(self)
        self.settings_window.refresh()
        self.settings_window.show()
        self.settings_window.raise_()
        self.settings_window.activateWindow()
        force_foreground(self.settings_window)

    def set_look(self, pet: str, scale: int) -> None:
        """Change the pet and/or its size. Waits if the pet is in the middle of a task."""
        changed_pet = pet != self.cfg.pet
        self.cfg.pet, self.cfg.pixel_scale = pet, scale
        self._save_settings()
        self._look_pending = True
        self._apply_look()
        if changed_pet and not self._look_pending and not self.bubble.has_buttons:
            self._say(f"{self.sprites.kind.hello.rstrip('?!')}! 🐾", 2000)

    def _apply_look(self) -> None:
        if not self._look_pending or self.cat.busy or self._arena is not None:
            return
        self._look_pending = False
        self.sprites = self._make_sprites(self.cfg.pet)
        self.cat.set_sprites(self.sprites)
        if self.cat.perched or self.cat.y() > self.cat.ground_y():
            self.cat.leave_perch(instant=True)
            self.cat.move(self.cat.x(), self.cat.ground_y())
        if self.tray:
            self.tray.setIcon(QtGui.QIcon(self.sprites.frames["stand"][1]))
        if self.buddy is not None:
            self._apply_companion()

    def set_walk(self, on: bool, minutes: float) -> None:
        self.cfg.walk_on, self.cfg.alert_after_s = on, int(minutes * 60)
        self._save_settings()
        if not on and self.tracker.alert_active:
            self.tracker.acknowledge()
            self._rotate_timer.stop()
            self._snooze_timer.stop()
            self.bubble.hide()
            if not self.cat.busy:
                self.cat.set_mode(None)

    def set_routines(self, routines: list[dict]) -> None:
        """Replace the routine list. A routine whose schedule changed starts afresh: it may
        fire again today at its new time, and its minutes count from now."""
        old = {r["id"]: r for r in self.routines}
        for routine in routines:
            before = old.get(routine["id"])
            if before is None or any(before[f] != routine[f] for f in ("kind", "time", "minutes")):
                self._state.pop(routine["id"], None)
                self._every_mark[routine["id"]] = self.tracker.active_s
                self._later.pop(routine["id"], None)
            if not routine["on"]:
                self._later.pop(routine["id"], None)
        kept = {r["id"] for r in routines}
        for key in set(old) - kept:
            self._state.pop(key, None)
            self._later.pop(key, None)
            self._every_mark.pop(key, None)
        self.routines = routines
        self._save_state()
        self._save_settings()

    # --- daily routine -----------------------------------------------------------
    def _routine(self, key: str) -> dict | None:
        return next((r for r in self.routines if r["id"] == key), None)

    def _every_left(self, routine: dict) -> float:
        """Seconds of screen time until an every-N-minutes routine is due."""
        mark = self._every_mark.setdefault(routine["id"], self.tracker.active_s)
        return routine["minutes"] * 60 - (self.tracker.active_s - mark)

    def _check_routine(self) -> None:
        """Runs every tracker tick. Waits while you're away, the cat is mid-task or a
        question bubble is open, so reminders never stack on top of each other."""
        if self.tracker.away or self.cat.busy or self.bubble.has_buttons or self._quiet():
            return
        now = self._now()
        reminder = self._due_reminder(now)
        if reminder is not None:
            self._show_reminder(reminder, now)
            return
        for key, due in list(self._later.items()):
            if now >= due:
                del self._later[key]
                self._show_routine(key)
                return
        today = now.date().isoformat()
        for routine in self.routines:
            key = routine["id"]
            if not routine["on"] or now.weekday() not in routine["days"] or key in self._later:
                continue
            if routine["kind"] == "time":
                h, m = (int(x) for x in routine["time"].split(":"))
                at = now.replace(hour=h, minute=m, second=0, microsecond=0)
                if self._state.get(key) != today and at <= now < at + timedelta(minutes=self.cfg.clock_window_min):
                    self._state[key] = today  # once a day, even across restarts
                    self._save_state()
                    self._show_routine(key)
                    return
            elif self._every_left(routine) <= 0 and not self.tracker.alert_active:  # the walk alert comes first
                self._every_mark[key] = self.tracker.active_s
                self._show_routine(key)
                return

    def _show_routine(self, key: str) -> None:
        routine = self._routine(key)
        if routine is not None:
            self.show_routine_bubble(routine)

    def show_routine_bubble(self, routine: dict) -> None:
        every = routine["kind"] == "every"
        later = "Later" if every else f"{self.cfg.clock_later_s // 60} more min"
        text = (routine["message"].replace("{time}", fmt_clock(routine["time"]))
                .replace("{every}", fmt_span(routine["minutes"] * 60)))
        self._chime()
        self.bubble.show_message(text, self.cat.geometry(), title=routine["title"],
                                 buttons=[(f"{routine['id']}_ok", routine["ok"]), (f"{routine['id']}_later", later)],
                                 timeout_ms=60_000 if every else 0)  # set-time routines wait for an answer

    def _routine_answer(self, key: str) -> None:
        routine = self._routine(key.rsplit("_", 1)[0])
        if routine is None:
            return  # a preview of a routine that isn't saved yet, or one just deleted
        if key.endswith("_later"):
            delay = self.cfg.water_later_s if routine["kind"] == "every" else self.cfg.clock_later_s
            self._later[routine["id"]] = self._now() + timedelta(seconds=delay)
            when = f"{delay // 60} min" if delay >= 60 else f"{delay} s"
            self._mood(-2)
            self._say(f"Okay, I'll ask again in {when}. 🐾", 2500)
        else:
            now = self._now()
            self.stats.add("done", 1, now)
            if routine["id"] == "water":
                self.stats.add("water", 1, now)
            self._mood(4)
            self._say(routine["reply"], 3000)
            if routine["id"] == "home":  # end of the day: show how it went
                QtCore.QTimer.singleShot(3500, lambda: not self.bubble.has_buttons and self._show_summary())

    def _load_state(self) -> dict:
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps(self._state), encoding="utf-8")
        except OSError:
            pass

    # --- fetch flow ----------------------------------------------------------------
    def _open_fetch(self, mode: str = "files") -> None:
        self.bubble.hide()
        self._occupy_cat("fetch")
        self.fetch.open_at(self.cat.geometry(), mode)

    def _remember(self, path: str, name: str, kind: str) -> None:
        """Keep the last 15 fetched files/apps for the empty search box."""
        item = {"path": path, "name": name, "is_dir": kind == "file" and os.path.isdir(path), "kind": kind}
        self.recent[:] = [item] + [r for r in self.recent if r["path"].lower() != path.lower()][:14]
        self._save_settings()

    def _on_fetched(self, action: str, path: str) -> None:
        if action in ("reveal", "open") and self.cfg.fetch_trip:
            # The cat runs off-screen and comes back with the item before it opens.
            name = path.rstrip(SEP).rsplit(SEP, 1)[-1] or path
            icon = self.fetch._icon_for(Hit(path, name, os.path.isdir(path))).pixmap(32, 32)
            self._trip_delivery = (action, path)
            self.bubble.hide()
            self.cat.start_trip(icon, self.cfg.fetch_trip_leg_s, self.cfg.fetch_trip_pause_s)
            return
        self._deliver(action, path)

    def _on_launched(self, target: str, name: str) -> None:
        """App results launch straight away (no fetch trip)."""
        try:
            launch_app(target)
            msg = f"Launching {name}… 🚀"
            self._remember(target, name, "app")
        except OSError:
            msg = f"I couldn't start {name}. 😿"
        self._release_cat()
        self._say(msg, 3000)

    def _on_trip_done(self) -> None:
        if self._playing:  # back with the ball
            self._playing = False
            self._release_cat()
            self._mood(8)
            self._say(random.choice(["Again! Again! 🎾", "Got it! 🎾", "*proud*"]), 2000)
            QtCore.QTimer.singleShot(1200, self.cat.drop_item)
            return
        if self._trip_delivery is None:
            return
        action, path = self._trip_delivery
        self._trip_delivery = None
        self._deliver(action, path)
        QtCore.QTimer.singleShot(1500, self.cat.drop_item)  # hold it up for a moment, then drop

    def _deliver(self, action: str, path: str) -> None:
        name = path.rstrip(SEP).rsplit(SEP, 1)[-1] or path
        wrapped = path.replace(SEP, SEP + "\u200b")  # zero-width spaces let long paths wrap
        try:
            if action == "open":
                open_path(path)
                msg = f"Here you go! 🐾\nOpened {name}"
            elif action == "reveal":
                if os.path.isdir(path):
                    open_path(path)  # a folder: go inside it
                    msg = f"Found it! 🐾\nHere's the {name} folder."
                else:
                    reveal_path(path)  # a file: its folder, with the file selected (not opened)
                    msg = f"Found it! 🐾\n{name} is selected in its folder."
            elif action == "copy":
                QtGui.QGuiApplication.clipboard().setText(path)
                msg = f"Path copied! 🐾\n{wrapped}"
            elif action == "value":
                QtGui.QGuiApplication.clipboard().setText(path)
                msg = f"Copied {path} 🧮"
            elif action == "clip":
                QtGui.QGuiApplication.clipboard().setText(path)
                msg = "Back on your clipboard. 📋"
            elif action == "openwith":
                open_with(path)
                msg = f"Pick an app for {name}. 🐾"
            else:
                msg = f"Delivered {name}! 🐾"
            if action in ("open", "reveal", "copy", "drag", "openwith"):
                self._remember(path, name, "app" if path.startswith("shell:") else "file")
        except OSError:
            msg = f"I couldn't open {name}. 😿"
        self._release_cat()
        self._say(msg, 4500)

    # --- cobra flow: size up -> confirm -> strike -----------------------------------
    def _start_cobra(self) -> None:
        if self.cobra.busy:
            return
        self._occupy_cat("busy")
        self._say("Sizing up your Recycle Bin and caches… 🐍", 0)
        self.cobra.plan_async()

    def _on_cobra_planned(self, plan: CleanupPlan) -> None:
        if plan.empty:
            self._release_cat()
            self._say("Nothing to strike: your Recycle Bin and caches are already clean. 🐍✨", 4000)
            return
        self._cobra_plan = plan
        lines = []
        if plan.recycle_items:
            lines.append(f"• Recycle Bin: {fmt_bytes(plan.recycle_bytes)} ({plan.recycle_items:,} items)")
        if plan.temp_items:
            lines.append(f"• Temp & cache files: {fmt_bytes(plan.temp_bytes)} ({len(plan.temp_items):,} items)")
        lines.append(f"\nOnly cache files untouched for {self.cfg.cobra_min_age_h} h+ are removed.\n"
                     "This deletes them for good.")
        self._say("\n".join(lines), 0, title="🐍 Cobra mode", style="alert",
                  buttons=[("cobra_go", "🐍 Strike!"), ("cobra_cancel", "Not now")])

    def _strike(self) -> None:
        plan, self._cobra_plan = self._cobra_plan, None
        if plan is None:
            return
        self._start_fight()
        self.cobra.execute_async(plan)

    def _cancel_cobra(self) -> None:
        self._cobra_plan = None
        self._release_cat()
        self._say("Okay, no striking today. 🐍", 2500)

    def _on_cobra_finished(self, freed: int, skipped: int) -> None:
        msg = f"Strike complete! 🐍\nFreed {fmt_bytes(freed)}."
        if skipped:
            msg += f"\n{skipped:,} items were in use, so I left them alone."
        self._finish_fight(msg)

    # --- the fight: cobra appears, cat smashes it, cobra keels over ------------------
    def _start_fight(self) -> None:
        if self._arena is not None:
            return
        self.bubble.hide()
        avail = self.cat._screen_geometry()
        room_right = avail.right() - self.cat.geometry().right()
        facing = 1 if room_right > self.sprites.size.width() * 1.5 else -1  # face where there's room
        self.cat.facing = facing
        self.cat.set_mode("smash")
        self._arena = CobraWindow(self.sprites, self.ui_font)
        self._arena.finished.connect(self._arena_gone)
        self._arena.appear(self.cat.geometry(), facing, self.sprites.size.width())
        self._fight_t0 = time.monotonic()

    def _finish_fight(self, message: str) -> None:
        wait = self.cfg.cobra_fight_min_s - (time.monotonic() - self._fight_t0)
        if wait > 0:  # quick cleanups still get a proper fight
            QtCore.QTimer.singleShot(int(wait * 1000), lambda: self._finish_fight(message))
            return
        if self._arena is not None:
            self._arena.defeat()
        self._release_cat()
        self._say(message, 6000)

    def _arena_gone(self) -> None:
        self._arena = None

    def _practice_fight(self) -> None:
        if self._arena is not None or self.cat.busy:
            return
        self._occupy_cat("busy")
        self._start_fight()
        self._finish_fight("Practice round! 🐍\nNothing was deleted.")

    # --- alert flow ----------------------------------------------------------------
    def _enter_alert(self) -> None:
        if self.cat.busy or self._quiet():
            return  # finishing a task, or focus / full screen: the tick brings it back afterwards
        self.cat.set_mode("alert")
        self._chime()
        self._show_alert_bubble()
        self._rotate_timer.start()

    def _show_alert_bubble(self) -> None:
        if not self.tracker.alert_active or self.bubble.has_buttons:
            return  # don't replace an open question
        choices = [r for r in WALK_REMINDERS if r != self._last_reminder]
        self._last_reminder = random.choice(choices)
        self.bubble.show_message(
            self._last_reminder + "\n\n(Click me when you're back.)",
            self.cat.geometry(),
            title=f"🪑 Sitting for {fmt_span(self.tracker.session_s)} straight!",
            style="alert",
        )

    def _resume_alert(self) -> None:
        self._show_alert_bubble()
        self._rotate_timer.start()

    def _acknowledge(self) -> None:
        self.tracker.acknowledge()
        self._rotate_timer.stop()
        self._snooze_timer.stop()
        self.cat.set_mode(None)
        self.stats.add("breaks", 1, self._now())
        self._mood(8)
        self._say(f"Thanks! Next walk in {fmt_span(self.cfg.alert_after_s)}. 🐾", 3500)

    def _reset_timer(self) -> None:
        if self.tracker.alert_active:
            self._acknowledge()
        else:
            self.tracker.acknowledge()
            self._say("Break timer reset.", 2500)

    # --- signal handlers -----------------------------------------------------------
    def _on_tick(self, session_s: float, idle_s: float) -> None:
        state = "away" if self.tracker.away else f"active {fmt_duration(session_s)}"
        waiting = [(self._every_left(r), r) for r in self.routines if r["on"] and r["kind"] == "every"]
        soonest = ""
        if waiting:
            left, routine = min(waiting, key=lambda pair: pair[0])
            soonest = f"{routine['title']} in {fmt_duration(max(0.0, left))}  ·  "
        self._status_action.setText(f"Session: {state}  ·  {soonest}{fmt_count(self.index.count())} files indexed")
        if self.tray:
            self.tray.setToolTip(f"Desktop Pet - {state}")

        now_m = time.monotonic()
        dt = min(5.0, now_m - self._last_tick)  # a long gap is sleep, not screen time
        self._last_tick = now_m
        self._tick_count += 1
        if not self.tracker.away:
            self.stats.add("screen_s", dt, self._now())
            self._mood(-dt / 900, emote=False)  # about 1 point per 15 min at the screen
        self._focus_tick(dt)
        if self._tick_count % 2 == 0:
            self._check_fullscreen()
        if self._tick_count % 10 == 0:
            self._buddy_tick()
        if self._tick_count % 60 == 0:
            self.stats.save()
            self._save_state()
            self._update_hat()
        if self._snip_until and now_m > self._snip_until:
            self._snip_until = 0.0
        self._apply_look()
        # A walk alert that waited for focus time or a full-screen app comes back now.
        if (self.tracker.alert_active and not self._quiet() and self.cat.mode is None
                and not self.bubble.has_buttons):
            self._enter_alert()
        self._check_routine()

    def _on_cat_moved(self, geometry: QtCore.QRect) -> None:
        if self.bubble.isVisible() and self.bubble.owner == "cat":
            self.bubble.follow(geometry)

    def _on_cat_clicked(self) -> None:
        if self.cat.busy:
            return
        if self.focus is not None and not self.tracker.alert_active:
            self._toggle_focus()
            return
        if self.tracker.alert_active:
            self._acknowledge()
        else:
            self.cat.wake_up()
            self._say(self.sprites.kind.pet_line + "\n" + self._status_text(), 4000)

    def _on_bubble_clicked(self) -> None:
        if self.tracker.alert_active and not self.cat.busy:
            self._acknowledge()

    def _on_bubble_dismissed(self) -> None:
        if self._cobra_plan is not None:
            self._cancel_cobra()  # "x" on the confirmation = not now
        elif self.bubble.button_keys:
            pass  # "x" on any other question (trick menu, water/lunch/home) just closes it
        elif self.tracker.alert_active and not self.cat.busy:
            # "x" snoozes the reminder; the timer is NOT reset and the cat keeps waiting.
            self._rotate_timer.stop()
            self._snooze_timer.start(self.cfg.snooze_s * 1000)
            self._mood(-3)

    def _on_away(self) -> None:
        # The tracker already cleared any alert: time away counts as the break.
        self._rotate_timer.stop()
        self._snooze_timer.stop()
        if self.cat.busy:
            return
        self.bubble.hide()
        self.cat.set_mode("away")

    def _on_returned(self) -> None:
        self.stats.add("breaks", 1, self._now())  # 5+ minutes away from the screen is a break
        self._mood(5, emote=False)
        if self.cat.busy:
            return
        self.cat.set_mode(None)
        if self.cfg.idle_policy == "pause":
            self._say(f"Welcome back! Resuming at {fmt_duration(self.tracker.session_s)}.", 3500)
        else:
            self._say("Welcome back! Fresh session started. 🐾", 3500)


# =================================================================================
# Settings window: pet, size, routines
# =================================================================================
_SETTINGS_QSS = """
QDialog, QWidget#page { background: $bg; color: $fg; }
QLabel, QCheckBox, QRadioButton { color: $fg; background: transparent; }
QLabel#hint { color: $hint; }
QLabel#section { color: $hint; font-weight: bold; }
QCheckBox::indicator, QListWidget::indicator { width: 14px; height: 14px; border: 2px solid $ind; border-radius: 4px; background: transparent; }
QRadioButton::indicator { width: 14px; height: 14px; border: 2px solid $ind; border-radius: 9px; background: transparent; }
QCheckBox::indicator:checked, QListWidget::indicator:checked, QRadioButton::indicator:checked { background: $fg; border-color: $fg; }
QCheckBox::indicator:disabled { border-color: $disabled; }
QCheckBox:disabled { color: $disabled; }
QTabWidget::pane { border: 1px solid $line; border-radius: 8px; top: -1px; }
QTabBar::tab { background: transparent; color: $hint; padding: 8px 18px; }
QTabBar::tab:selected { color: $fg; border-bottom: 2px solid $fg; }
QToolButton#tile { background: $panel; color: $fg; border: 2px solid transparent; border-radius: 10px; padding: 6px; }
QToolButton#tile:hover { background: $line; }
QToolButton#tile:checked { border-color: $fg; background: $line; }
QToolButton#day { background: $panel; color: $hint; border: none; border-radius: 6px; padding: 6px 8px; }
QToolButton#day:checked { background: $fg; color: $acc_text; }
QPushButton { background: $line; color: $fg; border: none; border-radius: 8px; padding: 7px 14px; }
QPushButton:hover { background: $hover; }
QPushButton:disabled { color: $disabled; background: $panel; }
QPushButton#primary { background: $fg; color: $acc_text; }
QLineEdit, QSpinBox, QTimeEdit, QDateEdit, QComboBox { background: $panel; color: $fg; border: 1px solid $line; border-radius: 6px; padding: 5px; }
QComboBox QAbstractItemView { background: $panel; color: $fg; selection-background-color: $hover; selection-color: $fg; }
QListWidget { background: $panel; color: $fg; border: none; border-radius: 8px; padding: 4px; outline: 0; }
QListWidget::item { padding: 7px 4px; border-radius: 6px; }
QListWidget::item:selected { background: $hover; color: $fg; }
QSlider::groove:horizontal { height: 4px; background: $hover; border-radius: 2px; }
QSlider::handle:horizontal { background: $fg; width: 16px; height: 16px; margin: -6px 0; border-radius: 8px; }
"""
SETTINGS_COLOURS = {
    "dark": {"bg": "#1e1e20", "fg": "#f5f5f7", "hint": "#a1a1a6", "ind": "#8e8e93", "line": "#3a3a3c",
             "panel": "#2c2c2e", "hover": "#48484a", "disabled": "#636366", "acc_text": "#1c1c1e",
             "bar": "#7aa2e8", "grid": "#3a3a3c"},
    "light": {"bg": "#f2f2f7", "fg": "#1c1c1e", "hint": "#6e6e73", "ind": "#8e8e93", "line": "#d1d1d6",
              "panel": "#ffffff", "hover": "#e5e5ea", "disabled": "#aeaeb2", "acc_text": "#ffffff",
              "bar": "#3b6fc4", "grid": "#d1d1d6"},
}


def settings_qss(theme: str | None = None) -> str:
    from string import Template
    return Template(_SETTINGS_QSS).substitute(SETTINGS_COLOURS[theme or current_theme()])


def dark_title_bar(window: QtWidgets.QWidget) -> None:
    """Match the title bar to the window's theme (Windows 10 2004+; harmless elsewhere)."""
    if sys.platform != "win32":
        return
    try:
        value = ctypes.c_int(1 if current_theme() == "dark" else 0)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(int(window.winId()), 20, ctypes.byref(value), ctypes.sizeof(value))
    except (AttributeError, OSError):
        pass


class RoutineEditor(QtWidgets.QDialog):
    """Add or change one routine. `result_routine()` gives the edited copy after accept()."""

    def __init__(self, pet: "DesktopPet", routine: dict | None, parent: QtWidgets.QWidget) -> None:
        super().__init__(parent)
        self.pet = pet
        self.setWindowTitle("Edit routine" if routine else "New routine")
        self.setStyleSheet(settings_qss())
        self.setMinimumWidth(420)
        self._base = dict(routine) if routine else {
            "id": "r" + os.urandom(4).hex(), "title": "", "message": "", "kind": "time", "minutes": 60,
            "time": "09:00", "days": list(range(7)), "on": True, "ok": "Done ✅", "reply": "Nice work! 🐾"}
        base = self._base

        self.name = QtWidgets.QLineEdit(base["title"])
        self.name.setPlaceholderText("🧘 Stretch")
        self.name.setMaxLength(40)
        self.message = QtWidgets.QLineEdit(base["message"])
        self.message.setPlaceholderText("Roll your shoulders and look away from the screen.")
        self.message.setMaxLength(160)

        self.at_time = QtWidgets.QRadioButton("Once a day at")
        self.every = QtWidgets.QRadioButton("Every")
        self.time = QtWidgets.QTimeEdit(QtCore.QTime.fromString(base["time"], "HH:mm"))
        self.time.setDisplayFormat("h:mm AP")
        self.minutes = QtWidgets.QSpinBox()
        self.minutes.setRange(1, 600)
        self.minutes.setValue(max(1, round(base["minutes"])))
        self.minutes.setSuffix(" min of screen time")
        self.time.setMinimumHeight(32)  # the row would otherwise clip the text on scaled displays
        self.minutes.setMinimumHeight(32)
        (self.every if base["kind"] == "every" else self.at_time).setChecked(True)
        self.at_time.toggled.connect(self._sync)

        self.day_buttons = []
        days = QtWidgets.QHBoxLayout()
        days.setSpacing(4)
        for i, name in enumerate(DAY_NAMES):
            button = QtWidgets.QToolButton()
            button.setObjectName("day")
            button.setText(name)
            button.setCheckable(True)
            button.setChecked(i in base["days"])
            button.toggled.connect(self._sync)
            self.day_buttons.append(button)
            days.addWidget(button)
        days.addStretch()

        hint = QtWidgets.QLabel("{time} and {every} in the message fill themselves in.")
        hint.setObjectName("hint")
        schedule = QtWidgets.QGridLayout()
        schedule.addWidget(self.at_time, 0, 0)
        schedule.addWidget(self.time, 0, 1)
        schedule.addWidget(self.every, 1, 0)
        schedule.addWidget(self.minutes, 1, 1)
        schedule.setColumnStretch(1, 1)
        schedule.setVerticalSpacing(6)
        schedule.setRowMinimumHeight(0, 34)
        schedule.setRowMinimumHeight(1, 34)

        form = QtWidgets.QFormLayout()
        form.addRow("Name", self.name)
        form.addRow("Message", self.message)
        form.addRow("", hint)
        schedule.setContentsMargins(0, 0, 0, 0)
        schedule_box = QtWidgets.QWidget()  # a nested layout alone gets squeezed by the form
        schedule_box.setLayout(schedule)
        form.addRow("When", schedule_box)
        form.addRow("Days", days)

        try_it = QtWidgets.QPushButton("Try it")
        try_it.clicked.connect(lambda: self.pet.show_routine_bubble(self.result_routine()))
        cancel = QtWidgets.QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.save = QtWidgets.QPushButton("Save")
        self.save.setObjectName("primary")
        self.save.setDefault(True)
        self.save.clicked.connect(self.accept)
        self.name.textChanged.connect(self._sync)
        buttons = QtWidgets.QHBoxLayout()
        buttons.addWidget(try_it)
        buttons.addStretch()
        buttons.addWidget(cancel)
        buttons.addWidget(self.save)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(form)
        layout.addSpacing(8)
        layout.addLayout(buttons)
        self._sync()
        dark_title_bar(self)

    def _sync(self) -> None:
        self.time.setEnabled(self.at_time.isChecked())
        self.minutes.setEnabled(self.every.isChecked())
        self.save.setEnabled(bool(self.name.text().strip()) and any(b.isChecked() for b in self.day_buttons))

    def result_routine(self) -> dict:
        routine = dict(self._base)
        routine["title"] = self.name.text().strip() or "🐾 Reminder"
        routine["message"] = self.message.text().strip()
        routine["kind"] = "time" if self.at_time.isChecked() else "every"
        routine["time"] = self.time.time().toString("HH:mm")
        if self.minutes.value() != max(1, round(self._base["minutes"])):
            routine["minutes"] = self.minutes.value()  # untouched fractions (demo timers) stay as they are
        routine["days"] = [i for i, b in enumerate(self.day_buttons) if b.isChecked()]
        return routine


class WeekChart(QtWidgets.QWidget):
    """Seven bars of screen time (one series, so one colour and no legend); hover a bar for its value."""

    def __init__(self, ui_font: UIFont) -> None:
        super().__init__()
        self.setMinimumHeight(170)
        self.setMouseTracking(True)
        self._font = ui_font.make(9)
        self._data: list[tuple] = []
        self._bars: list[QtCore.QRectF] = []

    def update_data(self, week: list[tuple]) -> None:
        self._data = week
        self.update()

    def paintEvent(self, event) -> None:
        c = SETTINGS_COLOURS[current_theme()]
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        p.setFont(self._font)
        left, top, bottom = 44, 22, self.height() - 24
        width = self.width() - left - 8
        hours = [d.get("screen_s", 0) / 3600 for _, d in self._data] or [0]
        top_h = max(1, math.ceil(max(hours)))
        # Recessive grid: a baseline and one line per whole hour step.
        step = max(1, top_h // 4)
        for h in range(0, top_h + 1, step):
            y = bottom - (bottom - top) * h / top_h
            p.setPen(QtGui.QPen(QtGui.QColor(c["grid"]), 1))
            p.drawLine(QtCore.QPointF(left, y), QtCore.QPointF(left + width, y))
            p.setPen(QtGui.QColor(c["hint"]))
            p.drawText(QtCore.QRectF(0, y - 8, left - 8, 16),
                       int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), f"{h} h")
        self._bars = []
        n = max(1, len(self._data))
        slot = width / n
        bar_w = min(36, slot - 2)  # thin bars with at least a 2 px gap
        for i, ((day, _), h) in enumerate(zip(self._data, hours)):
            x = left + slot * i + (slot - bar_w) / 2
            height = (bottom - top) * h / top_h
            rect = QtCore.QRectF(x, bottom - height, bar_w, height)
            self._bars.append(QtCore.QRectF(left + slot * i, top, slot, bottom - top))
            if height > 0:
                path = QtGui.QPainterPath()
                r = min(4.0, height, bar_w / 2)  # rounded top, square at the baseline
                path.moveTo(rect.left(), rect.bottom())
                path.lineTo(rect.left(), rect.top() + r)
                path.quadTo(rect.left(), rect.top(), rect.left() + r, rect.top())
                path.lineTo(rect.right() - r, rect.top())
                path.quadTo(rect.right(), rect.top(), rect.right(), rect.top() + r)
                path.lineTo(rect.right(), rect.bottom())
                path.closeSubpath()
                p.fillPath(path, QtGui.QColor(c["bar"]))
            p.setPen(QtGui.QColor(c["fg"] if i == n - 1 else c["hint"]))
            label = "Today" if i == n - 1 else day.strftime("%a")
            p.drawText(QtCore.QRectF(left + slot * i, bottom + 4, slot, 18), int(Qt.AlignmentFlag.AlignCenter), label)
            if i == n - 1 and h > 0:  # one direct label: today's value
                p.setPen(QtGui.QColor(c["fg"]))
                text = fmt_span(h * 3600) if h * 60 >= 1 else "<1 min"
                p.drawText(QtCore.QRectF(left + slot * i - 20, rect.top() - 18, slot + 40, 16),
                           int(Qt.AlignmentFlag.AlignCenter), text)
        if not any(hours):
            p.setPen(QtGui.QColor(c["hint"]))
            p.drawText(QtCore.QRectF(left, top, width, bottom - top), int(Qt.AlignmentFlag.AlignCenter),
                       "No screen time recorded yet.")
        p.end()

    def mouseMoveEvent(self, event) -> None:
        for (day, d), rect in zip(self._data, self._bars):
            if rect.contains(event.position()):
                s = d.get("screen_s", 0)
                QtWidgets.QToolTip.showText(
                    event.globalPosition().toPoint(),
                    f"{day:%A %d %b}\nScreen time: {fmt_span(s) if s >= 60 else 'under a minute'}\n"
                    f"Breaks: {int(d.get('breaks', 0))} · Water: {int(d.get('water', 0))} · "
                    f"Focus: {int(d.get('focus', 0))}", self)
                return
        QtWidgets.QToolTip.hideText()


class SettingsWindow(QtWidgets.QDialog):
    """Pet picker, size, colours, routines, more options and stats. Every change applies and saves at once."""

    PREVIEW_UNIT = 3

    def __init__(self, pet: "DesktopPet") -> None:
        super().__init__(None)
        self.pet = pet
        self.setWindowTitle("Desktop Pet settings")
        self.setStyleSheet(settings_qss())
        self.setFont(pet.ui_font.make(10))
        self.setMinimumWidth(520)

        self._loading = False
        tabs = QtWidgets.QTabWidget()
        tabs.addTab(self._build_pet_tab(), "Pet")
        tabs.addTab(self._build_routine_tab(), "Routines")
        tabs.addTab(self._build_more_tab(), "More")
        tabs.addTab(self._build_stats_tab(), "Stats")
        tabs.currentChanged.connect(lambda _: self.chart.update_data(self.pet.stats.week(self.pet._now())))
        self.tabs = tabs
        close = QtWidgets.QPushButton("Close")
        close.setObjectName("primary")
        close.clicked.connect(self.hide)
        bottom = QtWidgets.QHBoxLayout()
        if pet.cfg.overrides:
            note = QtWidgets.QLabel("Started with command-line options: changes last until you quit.")
            note.setObjectName("hint")
            bottom.addWidget(note)
        bottom.addStretch()
        bottom.addWidget(close)
        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addLayout(bottom)

        # The chosen pet walks in its tile while the window is open.
        self._walk_frame = 0
        self._walk_timer = QtCore.QTimer(self)
        self._walk_timer.setInterval(pet.cfg.frame_interval_ms)
        self._walk_timer.timeout.connect(self._walk_tick)
        dark_title_bar(self)

    # --- pet tab ---------------------------------------------------------------------
    def _build_pet_tab(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        page.setObjectName("page")
        grid = QtWidgets.QGridLayout()
        grid.setSpacing(8)
        self._previews = {key: SpriteSheet(self.PREVIEW_UNIT, self.pet.dpr, kind) for key, kind in PETS.items()}
        self._tiles: dict[str, QtWidgets.QToolButton] = {}
        group = QtWidgets.QButtonGroup(self)
        for i, (key, kind) in enumerate(PETS.items()):
            tile = QtWidgets.QToolButton()
            tile.setObjectName("tile")
            tile.setText(kind.label)
            tile.setCheckable(True)
            tile.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            tile.setIconSize(QtCore.QSize(90, 72))
            tile.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding, QtWidgets.QSizePolicy.Policy.Preferred)
            tile.setIcon(QtGui.QIcon(self._previews[key].frames["stand"][1]))
            tile.clicked.connect(lambda checked=False, k=key: self._choose_pet(k))
            group.addButton(tile)
            self._tiles[key] = tile
            grid.addWidget(tile, i // 3, i % 3)

        self.size_slider = QtWidgets.QSlider(Qt.Orientation.Horizontal)
        self.size_slider.setRange(0, len(SIZES) - 1)
        self.size_slider.setPageStep(1)
        self.size_label = QtWidgets.QLabel()
        self.size_label.setMinimumWidth(60)
        self.size_slider.valueChanged.connect(self._choose_size)
        size = QtWidgets.QHBoxLayout()
        size.addWidget(QtWidgets.QLabel("Size"))
        size.addWidget(self.size_slider, 1)
        size.addWidget(self.size_label)

        self.colour = QtWidgets.QComboBox()
        self.colour.addItems(list(PET_COLOURS))
        self.colour.currentTextChanged.connect(self._choose_colour)
        self.companion = QtWidgets.QComboBox()
        self.companion.addItem("None", "")
        for key, kind in PETS.items():
            self.companion.addItem(kind.label, key)
        self.companion.currentIndexChanged.connect(
            lambda _: self._set("companion", self.companion.currentData()))
        self.seasonal = QtWidgets.QCheckBox("Seasonal hats (winter hat, night cap)")
        self.seasonal.toggled.connect(lambda on: self._set("seasonal", on))
        self.birthday_on = QtWidgets.QCheckBox("Party hat on my birthday:")
        self.birthday = QtWidgets.QDateEdit()
        self.birthday.setDisplayFormat("d MMMM")
        self.birthday.setMinimumHeight(30)
        self.birthday_on.toggled.connect(self._birthday_changed)
        self.birthday.dateChanged.connect(self._birthday_changed)
        extras = QtWidgets.QGridLayout()
        extras.addWidget(QtWidgets.QLabel("Colour"), 0, 0)
        extras.addWidget(self.colour, 0, 1)
        extras.addWidget(QtWidgets.QLabel("Second pet"), 1, 0)
        extras.addWidget(self.companion, 1, 1)
        extras.addWidget(self.seasonal, 2, 0, 1, 2)
        extras.addWidget(self.birthday_on, 3, 0)
        extras.addWidget(self.birthday, 3, 1)
        extras.setColumnStretch(1, 1)

        layout = QtWidgets.QVBoxLayout(page)
        layout.addLayout(grid)
        layout.addSpacing(10)
        layout.addLayout(size)
        layout.addSpacing(6)
        layout.addLayout(extras)
        return page

    def _set(self, key: str, value) -> None:
        if not self._loading:
            self.pet.set_option(key, value)

    def _choose_colour(self, name: str) -> None:
        if self._loading:
            return
        colours = dict(self.pet.opts["colours"])
        colours[self.pet.cfg.pet] = "" if name == "Original" else name
        self.pet.set_option("colours", colours)
        self._rebuild_preview(self.pet.cfg.pet)

    def _rebuild_preview(self, key: str) -> None:
        self._previews[key] = SpriteSheet(self.PREVIEW_UNIT, self.pet.dpr, PETS[key],
                                          self.pet.opts["colours"].get(key, ""))
        self._tiles[key].setIcon(QtGui.QIcon(self._previews[key].frames["stand"][1]))

    def _birthday_changed(self, *_) -> None:
        self.birthday.setEnabled(self.birthday_on.isChecked())
        self._set("birthday", self.birthday.date().toString("MM-dd") if self.birthday_on.isChecked() else "")

    # --- more tab --------------------------------------------------------------------
    def _build_more_tab(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        page.setObjectName("page")
        windows = sys.platform == "win32"

        def section(text: str) -> QtWidgets.QLabel:
            label = QtWidgets.QLabel(text)
            label.setObjectName("section")
            return label

        self.theme_dark = QtWidgets.QRadioButton("Dark glass")
        self.theme_light = QtWidgets.QRadioButton("Light glass")
        self.theme_light.toggled.connect(lambda on: self._set("theme", "light" if on else "dark"))
        theme = QtWidgets.QHBoxLayout()
        theme.addWidget(self.theme_dark)
        theme.addWidget(self.theme_light)
        theme.addStretch()

        self.sounds = QtWidgets.QCheckBox("Play a short sound with reminders")
        self.sounds.toggled.connect(lambda on: self._set("sounds", on))
        test = QtWidgets.QPushButton("Test")
        test.clicked.connect(lambda: play_sound(sound_file(self.pet.cfg.pet)))
        sound = QtWidgets.QHBoxLayout()
        sound.addWidget(self.sounds)
        sound.addWidget(test)
        sound.addStretch()

        self.quiet = QtWidgets.QCheckBox("Hide and stay quiet while a full-screen app or presentation runs")
        self.quiet.toggled.connect(lambda on: self._set("quiet_fullscreen", on))
        self.perch = QtWidgets.QCheckBox("Hop onto the top of the window you're using now and then")
        self.perch.toggled.connect(lambda on: self._set("perch", on))
        self.hotkeys = QtWidgets.QCheckBox("Keyboard shortcuts that work anywhere")
        self.hotkeys.toggled.connect(self._hotkeys_changed)
        self.hotkey_status = QtWidgets.QLabel()
        self.hotkey_status.setObjectName("hint")
        self.clip = QtWidgets.QCheckBox("Remember copied text (this session only, never saved)")
        self.clip.toggled.connect(lambda on: self._set("clipboard", on))
        clear = QtWidgets.QPushButton("Clear")
        clear.clicked.connect(self.pet.clipboard.clear)
        clip = QtWidgets.QHBoxLayout()
        clip.addWidget(self.clip)
        clip.addWidget(clear)
        clip.addStretch()
        if not windows:
            for box in (self.quiet, self.perch, self.hotkeys):
                box.setEnabled(False)
                box.setToolTip("Windows only for now")

        layout = QtWidgets.QVBoxLayout(page)
        layout.addWidget(section("LOOK AND SOUND"))
        layout.addLayout(theme)
        layout.addLayout(sound)
        layout.addSpacing(8)
        layout.addWidget(section("BEHAVIOUR"))
        layout.addWidget(self.quiet)
        layout.addWidget(self.perch)
        layout.addSpacing(8)
        layout.addWidget(section("SHORTCUTS AND CLIPBOARD"))
        layout.addWidget(self.hotkeys)
        layout.addWidget(self.hotkey_status)
        layout.addLayout(clip)
        layout.addStretch()
        return page

    def _hotkeys_changed(self, on: bool) -> None:
        self._set("hotkeys", on)
        self._show_hotkeys()

    def _show_hotkeys(self) -> None:
        if not self.pet.hotkeys.supported:
            self.hotkey_status.setText("Shortcuts are Windows only for now.")
            return
        names = {"fetch": "Fetch", "clip": "Clipboard history", "note": "Notes"}
        parts = []
        for keys, action, ok in self.pet.hotkeys.status():
            state = "" if ok else ("  (off)" if ok is None else "  (taken by other apps)")
            parts.append(f"{keys}  {names[action]}{state}")
        self.hotkey_status.setText("\n".join(parts))

    # --- stats tab -------------------------------------------------------------------
    def _build_stats_tab(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        page.setObjectName("page")
        self.today = QtWidgets.QLabel()
        self.today.setWordWrap(True)
        title = QtWidgets.QLabel("Screen time, last 7 days")
        title.setObjectName("section")
        self.chart = WeekChart(self.pet.ui_font)
        layout = QtWidgets.QVBoxLayout(page)
        layout.addWidget(self.today)
        layout.addSpacing(8)
        layout.addWidget(title)
        layout.addWidget(self.chart, 1)
        return page

    def _fill_stats(self) -> None:
        self.today.setText("<b>Today</b><br>" + self.pet._summary_text().replace("\n", "<br>"))
        self.chart.update_data(self.pet.stats.week(self.pet._now()))

    def restyle(self) -> None:
        for w in (self, *self.findChildren(QtWidgets.QDialog)):
            w.setStyleSheet(settings_qss())
            dark_title_bar(w)
        self.chart.update()

    def _size_index(self) -> int:
        scale = self.pet.cfg.pixel_scale
        return min(range(len(SIZES)), key=lambda i: abs(SIZES[i][1] - scale))

    def _choose_pet(self, key: str) -> None:
        for other, tile in self._tiles.items():
            tile.setIcon(QtGui.QIcon(self._previews[other].frames["stand"][1]))
        self.pet.set_look(key, self.pet.cfg.pixel_scale)
        self._loading = True
        self.colour.setCurrentText(self.pet.opts["colours"].get(key) or "Original")
        self._loading = False

    def _choose_size(self, index: int) -> None:
        name, scale = SIZES[index]
        self.size_label.setText(name)
        if scale != self.pet.cfg.pixel_scale and self.isVisible():
            self.pet.set_look(self.pet.cfg.pet, scale)

    def _walk_tick(self) -> None:
        self._walk_frame += 1
        name = ("walk_a", "stand", "walk_b", "stand")[self._walk_frame % 4]
        key = self.pet.cfg.pet
        self._tiles[key].setIcon(QtGui.QIcon(self._previews[key].frames[name][1]))

    # --- routines tab ----------------------------------------------------------------
    def _build_routine_tab(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        page.setObjectName("page")
        self.walk_on = QtWidgets.QCheckBox("🚶 Get up and walk after sitting for")
        self.walk_minutes = QtWidgets.QSpinBox()
        self.walk_minutes.setRange(5, 240)
        self.walk_minutes.setSuffix(" min")
        self.walk_on.toggled.connect(self._walk_changed)
        self.walk_minutes.valueChanged.connect(self._walk_changed)
        walk = QtWidgets.QHBoxLayout()
        walk.addWidget(self.walk_on)
        walk.addWidget(self.walk_minutes)
        walk.addStretch()
        self.focus_min = QtWidgets.QSpinBox()
        self.focus_min.setRange(5, 120)
        self.focus_min.setSuffix(" min focus")
        self.break_min = QtWidgets.QSpinBox()
        self.break_min.setRange(1, 60)
        self.break_min.setSuffix(" min break")
        self.focus_min.valueChanged.connect(lambda v: self._set("focus_min", v))
        self.break_min.valueChanged.connect(lambda v: self._set("break_min", v))
        focus = QtWidgets.QHBoxLayout()
        focus.addWidget(QtWidgets.QLabel("🍅 Focus timer"))
        focus.addWidget(self.focus_min)
        focus.addWidget(self.break_min)
        focus.addStretch()

        self.list = QtWidgets.QListWidget()
        self.list.setMinimumHeight(220)
        self.list.itemChanged.connect(self._item_toggled)
        self.list.itemDoubleClicked.connect(lambda item: self._edit())
        self.list.currentRowChanged.connect(self._sync_buttons)

        add = QtWidgets.QPushButton("Add")
        add.setObjectName("primary")
        add.clicked.connect(self._add)
        self.edit_button = QtWidgets.QPushButton("Edit")
        self.edit_button.clicked.connect(self._edit)
        self.delete_button = QtWidgets.QPushButton("Delete")
        self.delete_button.clicked.connect(self._delete)
        reset = QtWidgets.QPushButton("Restore defaults")
        reset.clicked.connect(self._reset)
        buttons = QtWidgets.QHBoxLayout()
        for button in (add, self.edit_button, self.delete_button):
            buttons.addWidget(button)
        buttons.addStretch()
        buttons.addWidget(reset)

        layout = QtWidgets.QVBoxLayout(page)
        layout.addLayout(walk)
        layout.addLayout(focus)
        layout.addSpacing(6)
        layout.addWidget(self.list, 1)
        layout.addLayout(buttons)
        return page

    def _fill_list(self) -> None:
        row = self.list.currentRow()
        self.list.blockSignals(True)
        self.list.clear()
        for routine in self.pet.routines:
            item = QtWidgets.QListWidgetItem(f"{routine['title']}   ·   {routine_when(routine)}")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if routine["on"] else Qt.CheckState.Unchecked)
            self.list.addItem(item)
        self.list.blockSignals(False)
        self.list.setCurrentRow(min(row, self.list.count() - 1))
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        has_row = self.list.currentRow() >= 0
        self.edit_button.setEnabled(has_row)
        self.delete_button.setEnabled(has_row)

    def _walk_changed(self) -> None:
        self.walk_minutes.setEnabled(self.walk_on.isChecked())
        if not self.signalsBlocked():
            self.pet.set_walk(self.walk_on.isChecked(), self.walk_minutes.value())

    def _item_toggled(self, item: QtWidgets.QListWidgetItem) -> None:
        row = self.list.row(item)
        routines = [dict(r) for r in self.pet.routines]
        routines[row]["on"] = item.checkState() == Qt.CheckState.Checked
        self.pet.set_routines(routines)

    def _add(self) -> None:
        editor = RoutineEditor(self.pet, None, self)
        if editor.exec():
            self.pet.set_routines(self.pet.routines + [editor.result_routine()])
            self._fill_list()
            self.list.setCurrentRow(self.list.count() - 1)

    def _edit(self) -> None:
        row = self.list.currentRow()
        if row < 0:
            return
        editor = RoutineEditor(self.pet, self.pet.routines[row], self)
        if editor.exec():
            routines = list(self.pet.routines)
            routines[row] = editor.result_routine()
            self.pet.set_routines(routines)
            self._fill_list()

    def _confirm(self, question: str) -> bool:
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Desktop Pet")
        box.setText(question)
        box.setStyleSheet(settings_qss())
        box.setStandardButtons(QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.Cancel)
        return box.exec() == QtWidgets.QMessageBox.StandardButton.Yes

    def _delete(self) -> None:
        row = self.list.currentRow()
        if row >= 0 and self._confirm(f"Delete “{self.pet.routines[row]['title']}”?"):
            self.pet.set_routines(self.pet.routines[:row] + self.pet.routines[row + 1:])
            self._fill_list()

    def _reset(self) -> None:
        if self._confirm("Restore water, lunch, home and eye breaks to their defaults?\n"
                         "Routines you added will be removed."):
            self.pet.set_routines(default_routines(Config()))
            self._fill_list()

    # --- window ----------------------------------------------------------------------
    def refresh(self) -> None:
        """Show the current values, without treating that as a change."""
        cfg = self.pet.cfg
        self.blockSignals(True)
        self.walk_on.setChecked(cfg.walk_on)
        self.walk_minutes.setValue(round(cfg.alert_after_s / 60))
        self.walk_minutes.setEnabled(cfg.walk_on)
        self.blockSignals(False)
        self._tiles[cfg.pet].setChecked(True)
        index = self._size_index()
        self.size_slider.blockSignals(True)
        self.size_slider.setValue(index)
        self.size_slider.blockSignals(False)
        self.size_label.setText(SIZES[index][0])
        self._fill_list()

        opts = self.pet.opts
        self._loading = True
        for key in PETS:
            self._rebuild_preview(key)
        self.colour.setCurrentText(opts["colours"].get(cfg.pet) or "Original")
        self.companion.setCurrentIndex(max(0, self.companion.findData(opts["companion"])))
        self.seasonal.setChecked(opts["seasonal"])
        self.birthday_on.setChecked(bool(opts["birthday"]))
        if opts["birthday"]:
            self.birthday.setDate(QtCore.QDate.fromString("2000-" + opts["birthday"], "yyyy-MM-dd"))
        self.birthday.setEnabled(bool(opts["birthday"]))
        (self.theme_light if opts["theme"] == "light" else self.theme_dark).setChecked(True)
        self.sounds.setChecked(opts["sounds"])
        self.quiet.setChecked(opts["quiet_fullscreen"])
        self.perch.setChecked(opts["perch"])
        self.hotkeys.setChecked(opts["hotkeys"])
        self.clip.setChecked(opts["clipboard"])
        self.focus_min.setValue(opts["focus_min"])
        self.break_min.setValue(opts["break_min"])
        self._loading = False
        self._show_hotkeys()
        self._fill_stats()

    def showEvent(self, event) -> None:
        self._walk_timer.start()
        super().showEvent(event)

    def hideEvent(self, event) -> None:
        self._walk_timer.stop()
        super().hideEvent(event)


# =================================================================================
# Single instance + shortcut install
# =================================================================================
INSTANCE_KEY = f"DesktopCat-{os.environ.get('USERNAME') or os.environ.get('USER', 'user')}"


def notify_running_instance() -> bool:
    """If the cat is already running, poke it and return True."""
    sock = QtNetwork.QLocalSocket()
    sock.connectToServer(INSTANCE_KEY)
    if not sock.waitForConnected(300):
        return False
    sock.write(b"wake")
    sock.waitForBytesWritten(300)
    sock.disconnectFromServer()
    return True


def listen_for_launches(pet: DesktopPet) -> QtNetwork.QLocalServer:
    server = QtNetwork.QLocalServer(pet)
    QtNetwork.QLocalServer.removeServer(INSTANCE_KEY)  # clear a stale socket after a crash
    server.listen(INSTANCE_KEY)
    server.newConnection.connect(lambda: (server.nextPendingConnection(), pet.wake_from_launch()))
    return server


def app_icon_image(size: int) -> QtGui.QImage:
    """The dedicated icon: the standing cat, centred on a transparent square."""
    unit = size // (CAT_ART_W + 2)
    art = render_art(SPRITE_ART["stand"], unit, PALETTE)
    canvas = QtGui.QImage(size, size, QtGui.QImage.Format.Format_ARGB32)
    canvas.fill(Qt.GlobalColor.transparent)
    p = QtGui.QPainter(canvas)
    # +unit: the two spare paw columns on the right would otherwise push the cat off-centre.
    p.drawImage((size - art.width()) // 2 + unit, (size - art.height()) // 2, art)
    p.end()
    return canvas


def write_icns(path: Path) -> None:
    """macOS icon file: a header, then one (type, length, PNG data) entry per size."""
    body = b""
    for code, size in ((b"ic07", 128), (b"ic08", 256), (b"ic09", 512)):
        data = QtCore.QByteArray()
        buffer = QtCore.QBuffer(data)
        buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
        if not app_icon_image(size).save(buffer, "PNG"):
            raise OSError("could not encode the icon")
        png = bytes(data)
        body += code + struct.pack(">I", len(png) + 8) + png
    path.write_bytes(b"icns" + struct.pack(">I", len(body) + 8) + body)


def install_shortcuts() -> list[str]:
    """Create the 'Desktop Pet' launcher(s) with the pet icon; returns what was created."""
    if sys.platform == "win32":
        return _install_windows_shortcuts()
    if sys.platform == "darwin":
        return _install_mac_app()
    raise OSError("shortcuts are only supported on Windows and macOS")


def _install_windows_shortcuts() -> list[str]:
    """desktop_cat.ico + shortcuts on the Desktop and in the Start menu (they launch with
    pythonw, so no console window appears)."""
    script = Path(__file__).resolve()
    icon = script.with_name("desktop_cat.ico")
    if not app_icon_image(256).save(str(icon), "ICO"):
        raise OSError("could not write the icon")

    pythonw = Path(sys.executable).with_name("pythonw.exe")
    target = pythonw if pythonw.exists() else Path(sys.executable)

    def q(value) -> str:  # PowerShell single-quoted literal
        return "'" + str(value).replace("'", "''") + "'"

    ps = (
        "$s = New-Object -ComObject WScript.Shell; "
        "foreach ($d in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) { "
        # The app used to be called Desktop Cat: remove that shortcut, but only if it is ours.
        "$old = Join-Path $d 'Desktop Cat.lnk'; "
        "if (Test-Path -LiteralPath $old) { if ($s.CreateShortcut($old).Arguments -like '*desktop_cat.py*') "
        "{ Remove-Item -LiteralPath $old } }; "
        "$path = Join-Path $d 'Desktop Pet.lnk'; $l = $s.CreateShortcut($path); "
        f"$l.TargetPath = {q(target)}; $l.Arguments = {q(chr(34) + str(script) + chr(34))}; "
        f"$l.WorkingDirectory = {q(script.parent)}; $l.IconLocation = {q(str(icon) + ',0')}; "
        "$l.Description = 'Desktop Pet: break reminders, file fetching and Cobra mode'; $l.Save(); $path }"
    )
    result = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, check=True)
    return [line for line in result.stdout.splitlines() if line.strip()]


def build_mac_app(app: Path, python: str, script: str) -> None:
    """A minimal .app bundle: Info.plist, the icon, and a launcher that runs the script."""
    contents = app / "Contents"
    (contents / "MacOS").mkdir(parents=True, exist_ok=True)
    (contents / "Resources").mkdir(parents=True, exist_ok=True)
    write_icns(contents / "Resources" / "desktop_cat.icns")
    launcher = contents / "MacOS" / "DesktopCat"
    launcher.write_text(f'#!/bin/bash\nexec "{python}" "{script}"\n', encoding="utf-8", newline="\n")
    launcher.chmod(0o755)
    (contents / "Info.plist").write_text("""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Desktop Pet</string>
    <key>CFBundleDisplayName</key><string>Desktop Pet</string>
    <key>CFBundleIdentifier</key><string>local.desktopcat.app</string>
    <key>CFBundleExecutable</key><string>DesktopCat</string>
    <key>CFBundleIconFile</key><string>desktop_cat</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleVersion</key><string>1.0</string>
    <key>LSUIElement</key><true/>
</dict>
</plist>
""", encoding="utf-8", newline="\n")


def _install_mac_app() -> list[str]:
    old = Path.home() / "Desktop" / "Desktop Cat.app"  # the app's earlier name; removed only if we built it
    if (old / "Contents" / "MacOS" / "DesktopCat").exists():
        import shutil
        shutil.rmtree(old, ignore_errors=True)
    app = Path.home() / "Desktop" / "Desktop Pet.app"
    build_mac_app(app, sys.executable, str(Path(__file__).resolve()))
    subprocess.run(["touch", str(app)], check=False)  # makes Finder pick up the new icon
    return [str(app)]


# =================================================================================
# Entry point
# =================================================================================
def parse_args(argv: list[str]) -> tuple[Config, argparse.Namespace]:
    parser = argparse.ArgumentParser(description="Desktop Pet: break reminders, file fetching and Cobra mode.")
    parser.add_argument("--demo", action="store_true", help="fast timers for testing (30 s alert, 20 s idle)")
    parser.add_argument("--alert-minutes", type=float, help="continuous activity before the alert (default 120)")
    parser.add_argument("--idle-minutes", type=float, help="inactivity that counts as away (default 5)")
    parser.add_argument("--idle-policy", choices=["reset", "pause"], help="what being away does to the session")
    parser.add_argument("--scale", type=int, help="sprite pixel size (default 5)")
    parser.add_argument("--index", action="append", metavar="PATH",
                        help="index this folder instead of all fixed drives (repeatable)")
    parser.add_argument("--water-minutes", type=float, help="sip-of-water reminder interval (default 30)")
    parser.add_argument("--lunch", metavar="HH:MM", help="lunch reminder time, 24 h (default 13:00)")
    parser.add_argument("--home", metavar="HH:MM", help="go-home reminder time, 24 h (default 17:45)")
    parser.add_argument("--install-shortcut", action="store_true",
                        help="create the Desktop Pet launcher with its icon (Windows: Desktop + Start menu; macOS: Desktop app)")
    args = parser.parse_args(argv)

    cfg = Config.demo() if args.demo else Config()
    overrides = ["alert", "water"] if args.demo else []
    if args.alert_minutes:
        cfg.alert_after_s = int(args.alert_minutes * 60)
        overrides.append("alert")
    if args.idle_minutes:
        cfg.idle_threshold_s = int(args.idle_minutes * 60)
    if args.idle_policy:
        cfg.idle_policy = args.idle_policy
    if args.scale:
        cfg.pixel_scale = max(1, args.scale)
        overrides.append("scale")
    if args.index:
        cfg.index_roots = tuple(str(Path(p).resolve()) for p in args.index)
    if args.water_minutes:
        cfg.water_every_s = int(args.water_minutes * 60)
        overrides.append("water")
    for name in ("lunch", "home"):
        value = getattr(args, name)
        if value:
            fmt_clock(value)  # validates HH:MM (raises ValueError otherwise)
            setattr(cfg, f"{name}_time", value)
            overrides.append(name)
    cfg.overrides = tuple(overrides)
    return cfg, args


def main() -> int:
    cfg, args = parse_args(sys.argv[1:])
    signal.signal(signal.SIGINT, signal.SIG_DFL)  # let Ctrl+C in the terminal quit
    app = QtWidgets.QApplication(sys.argv)
    if args.install_shortcut:
        for path in install_shortcuts():
            print("Created", path)
        if sys.platform == "win32":
            print("To pin it: right-click the Desktop Pet shortcut > Show more options > Pin to taskbar.")
        return 0
    if notify_running_instance():
        return 0  # already running: the existing cat says hello instead
    app.setQuitOnLastWindowClosed(False)          # bubble/menu closing must not quit
    pet = DesktopPet(cfg)
    server = listen_for_launches(pet)  # noqa: F841 - keep a reference so it stays alive
    pet.start()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
