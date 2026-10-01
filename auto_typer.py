"""
Keystroke-trace simulator / auto-typer.

Architecture (the model never imports Tk or pynput):

    TypingProfile   tunable model parameters (hand-chosen, NOT fit to any dataset)
    TypingState     mutable simulated state (pace, fatigue, elapsed time)
    TypingModel     stochastic model: IKI, dwell, typo probability, state update
    TypingPlanner   text -> immutable, absolute-timestamped Event trace (pure, seedable)
    TracePlayer     plays a trace against absolute deadlines (Tk-free, clock injectable)
    KeyboardOutput  thin pynput wrapper (imported lazily, fails loudly)
    AutoTyperApp    Tk UI + worker thread that hands a pre-planned trace to TracePlayer

CONVENTIONS
-----------
* typo_rate is ALWAYS a per-character probability in [0.0, 1.0] in every Python
  API (build_trace, simulate, run_benchmark). Only the UI spinbox and the CLI
  flag use percent, and they convert exactly once at the boundary.
* Two WPM definitions (user-selectable, `mode`):
    "gross": mean rate of consecutive keystrokes during uninterrupted typing,
             60 / (5 * mean_IKI). Excludes pauses, Backspace and Enter. NOTE: the
             wrong keys typed inside error episodes ARE counted (they are real
             keystrokes), so a sloppy trace does not lower this number. It is a
             keystroke rate, not corrected-text throughput; use "net" for that.
    "net"  : logical characters / 5 / total elapsed minutes, including thinking
             pauses, typo corrections and newlines. NOTE: to hit a net target,
             the model types faster between errors/pauses to compensate.
* PHYSICAL vs LOGICAL state (deliberately different, do not "fix"):
    physical motor state = last key actually struck overall (prev_phys, may be
                           Backspace/Enter/Nav; drives hand alternation) plus the last
                           key struck by EACH hand (hand_pos; the Fitts movement
                           distance is measured from the moving hand's own last
                           key, since the two hands move independently).
    logical text state   = last character currently in the on-screen buffer
                           -> drives digraph speed-ups and pause rules.
  After a correction the right hand is on Backspace (physical) while the buffer
  tail is the last correct character (logical).
* SHIFT is a real modifier in the trace. A shifted character ("A", "?", "{") is
  a "shift" Event (SHIFT_L/SHIFT_R, the hand opposite the base key) that starts
  slightly BEFORE the base-key Event and ends slightly AFTER it. Consequently
  trace events are ordered by press_at but are not strictly non-overlapping;
  use events_to_transitions() to get the flat down/up sequence.
  Shift is always released before the next key is pressed.

* EDITOR AUTO-INDENT (IndentPolicy): off by default. "copy"/"smart" predict the
  whitespace the editor inserts after Enter and clear exactly that; flat code
  clears nothing. "fixed" is the legacy always-N-backspaces behaviour.
  Indentation is counted in logical levels (tabs and space-runs), so mixed
  tab/space files are handled instead of assuming one whitespace style.

* CODING MODE (Pascal / Structured Block Lookahead):
  When enabled, reads ahead to find matching `begin` and `end` blocks. It writes
  out `begin`, creates the block structure and types `end`, then navigates back
  up to fill in the inner body statements before navigating down past the closing
  `end`. Fully supports nested blocks, indentation policies, and arbitrary code.

CLI benchmark (no keys pressed, no GUI):
    python auto_typer.py --benchmark --wpm 110 --mode gross --typo 3 --seed 12345 --csv trace.csv
    python auto_typer.py --benchmark --indent smart --file my_code.py
    python auto_typer.py --benchmark --coding-mode --file program.pas
"""

import argparse
import csv
import math
import queue
import random
import re
import statistics
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

try:
    import tkinter as tk
    from tkinter import messagebox, ttk
except ImportError:          # headless box: planner, benchmark and tests still work
    tk = messagebox = ttk = None

# --------------------------------------------------------------------------
# Physical keyboard geometry
# --------------------------------------------------------------------------
BACKSPACE = "<BS>"
ENTER = "<ENTER>"
TAB = "<TAB>"
SHIFT_L = "<SHIFT_L>"
SHIFT_R = "<SHIFT_R>"
UP = "<UP>"
DOWN = "<DOWN>"
LEFT = "<LEFT>"
RIGHT = "<RIGHT>"
HOME = "<HOME>"
END_KEY = "<END>"
_SPECIAL_TOKENS = frozenset({
    BACKSPACE, ENTER, TAB, SHIFT_L, SHIFT_R,
    UP, DOWN, LEFT, RIGHT, HOME, END_KEY,
})

# Key centres in key-width units (row, column)
_KEY_MAP = {
    '`': (0, -1.0),
    '1': (0, 0), '2': (0, 1), '3': (0, 2), '4': (0, 3), '5': (0, 4),
    '6': (0, 5), '7': (0, 6), '8': (0, 7), '9': (0, 8), '0': (0, 9), '-': (0, 10), '=': (0, 11),
    'q': (1, 0.5), 'w': (1, 1.5), 'e': (1, 2.5), 'r': (1, 3.5), 't': (1, 4.5),
    'y': (1, 5.5), 'u': (1, 6.5), 'i': (1, 7.5), 'o': (1, 8.5), 'p': (1, 9.5),
    '[': (1, 10.5), ']': (1, 11.5), '\\': (1, 12.5),
    'a': (2, 0.75), 's': (2, 1.75), 'd': (2, 2.75), 'f': (2, 3.75), 'g': (2, 4.75),
    'h': (2, 5.75), 'j': (2, 6.75), 'k': (2, 7.75), 'l': (2, 8.75), ';': (2, 9.75), "'": (2, 10.75),
    'z': (3, 1.25), 'x': (3, 2.25), 'c': (3, 3.25), 'v': (3, 4.25), 'b': (3, 5.25),
    'n': (3, 6.25), 'm': (3, 7.25), ',': (3, 8.25), '.': (3, 9.25), '/': (3, 10.25),
    ' ': (4, 5.0),
    BACKSPACE: (0, 13.0), ENTER: (2, 12.25), TAB: (1, -0.5),
    SHIFT_L: (3, -0.4), SHIFT_R: (3, 12.1),
    UP: (3, 14.5), DOWN: (4, 14.5), LEFT: (4, 13.5), RIGHT: (4, 15.5),
    HOME: (1, 14.5), END_KEY: (2, 14.5),
}

# Shifted symbol -> unshifted key that physically produces it (US layout).
_SHIFT_MAP = {
    '~': '`', '!': '1', '@': '2', '#': '3', '$': '4', '%': '5', '^': '6',
    '&': '7', '*': '8', '(': '9', ')': '0', '_': '-', '+': '=',
    '{': '[', '}': ']', '|': '\\', ':': ';', '"': "'",
    '<': ',', '>': '.', '?': '/',
}
_UNSHIFT_MISSING = [v for v in _SHIFT_MAP.values() if v not in _KEY_MAP]
if _UNSHIFT_MISSING:
    raise RuntimeError(f"_SHIFT_MAP targets with no key geometry: {_UNSHIFT_MISSING!r}")

LEFT_HAND_KEYS = set("`12345qwertasdfgzxcvb")
RIGHT_HAND_KEYS = {k for k in _KEY_MAP if len(k) == 1 and k != ' ' and k not in LEFT_HAND_KEYS}
COMMON_DIGRAPHS = {"th", "he", "in", "er", "an", "re", "on", "at", "en", "nd", "st", "es", "or", "te", "of"}
SYNTAX_CHARS = set("={}()[]:;,.")

MIN_IKI = 0.014   # floor on press-to-press interval (s)
MIN_GAP = 0.008   # floor on release-to-press gap (s)


def _base_key(ch):
    """Map a character (or special-key token) to the physical key that produces it."""
    if not ch:
        return ""
    if ch == "\t":
        return TAB
    if ch == "\n":
        return ENTER
    if ch in _SPECIAL_TOKENS:
        return ch
    return _SHIFT_MAP.get(ch, ch.lower() if isinstance(ch, str) else ch)


def _build_neighbours():
    out = {}
    for ch, (r, c) in _KEY_MAP.items():
        if ch == ' ' or len(ch) > 1:
            continue
        out[ch] = [
            o for o, (orr, oc) in _KEY_MAP.items()
            if o != ch and o != ' ' and len(o) == 1 and math.hypot(r - orr, c - oc) <= 1.5
        ]
    return out


NEIGHBOURS = _build_neighbours()


def hand(ch):
    """'L', 'R', or None for keys with no hand assignment (space/thumb, specials, unknown)."""
    if not ch:
        return None
    base = _base_key(ch)
    if base in LEFT_HAND_KEYS:
        return "L"
    if base in RIGHT_HAND_KEYS:
        return "R"
    return None


def motor_hand(ch):
    """Like hand(), but also assigns pinky/nav special keys (for movement tracking)."""
    if not ch:
        return None
    base = _base_key(ch)
    if base in (BACKSPACE, ENTER, UP, DOWN, LEFT, RIGHT, HOME, END_KEY, SHIFT_R):
        return "R"
    if base in (TAB, SHIFT_L):
        return "L"
    return hand(ch)


def needs_shift(ch):
    """True if typing `ch` on a US layout physically requires Shift (and its base key is known)."""
    if not isinstance(ch, str) or len(ch) != 1:
        return False
    if ch in _SHIFT_MAP:
        return True
    return ch.isupper() and ch.lower() in _KEY_MAP


def shift_key_for(ch):
    """SHIFT_L / SHIFT_R for a shifted character (the hand opposite the base key, as touch
    typists do), or None if `ch` needs no Shift."""
    if not needs_shift(ch):
        return None
    return SHIFT_R if hand(ch) == "L" else SHIFT_L


def physical_key(ch):
    """The key actually struck for `ch`: its unshifted base key when Shift is involved,
    the token itself for special keys, otherwise the character as-is."""
    if ch in _SPECIAL_TOKENS:
        return ch
    if needs_shift(ch):
        return _base_key(ch)
    return ch


def get_key_distance(ch1, ch2):
    p1 = _KEY_MAP.get(_base_key(ch1), (2, 5))
    p2 = _KEY_MAP.get(_base_key(ch2), (2, 5))
    return math.hypot(p1[0] - p2[0], p1[1] - p2[1])


def fitts_index_of_difficulty(ch1, ch2, target_width=1.0):
    """ID = log2(1 + D/W)."""
    d = get_key_distance(ch1, ch2)
    return 0.0 if d <= 0.0 else math.log2(1.0 + d / target_width)


# --------------------------------------------------------------------------
# Editor auto-indent handling
# --------------------------------------------------------------------------
INDENT_OPENERS = ":{(["          # last char that makes a "smart" editor add a level
SMART_WORDS = {"begin", "then", "do", "try", "else", "repeat", "of", "record"}
MAX_INDENT_BACKSPACES = 64       # hard safety cap per line (only guards pathological whitespace)
INDENT_NOTE = "indent"           # Event.note on every Backspace that clears auto-indent
INDENT_FIRST_NOTE = "indent:first"   # ...and on the first one of each line


def leading_ws(line):
    return line[:len(line) - len(line.lstrip(" \t"))]


def detect_space_width(lines):
    """Width of one space-indent level: the most common indent *increase* between
    consecutive non-blank lines, clamped to 2-8 (default 4)."""
    increases, prev = Counter(), 0
    for line in lines:
        if not line.strip():
            continue
        cur = len(line) - len(line.lstrip(" "))
        if cur > prev:
            increases[cur - prev] += 1
        prev = cur
    width = increases.most_common(1)[0][0] if increases else 4
    return width if 2 <= width <= 8 else 4


def detect_indent_unit(lines):
    """
    One indent level of the source text as the string an editor would insert:
    a tab if tab-led lines are at least as common as space-led lines, otherwise
    spaces (width from detect_space_width).
    """
    tab_lines = sum(1 for ln in lines if ln.startswith("\t"))
    space_lines = sum(1 for ln in lines if ln.startswith(" "))
    if tab_lines and tab_lines >= space_lines:
        return "\t"
    return " " * detect_space_width(lines)


def indent_levels(ws, space_width=4):
    """
    Backspace presses needed to clear `ws` in an editor whose Backspace removes
    one indent level (tab stop) at a time: each tab is one level, and a run of
    n spaces is ceil(n / space_width) levels.
    """
    levels = run = 0
    for ch in ws:
        if ch == "\t":
            levels += -(-run // space_width) + 1
            run = 0
        else:
            run += 1
    return levels + -(-run // space_width)


def expected_auto_indent(line, style, unit):
    """
    Whitespace an editor inserts on the new line after Enter at the end of `line`
    (as it currently appears on screen).
      "off"  : nothing (flat code / disabled auto-indent)
      "copy" : the line's own leading whitespace
      "smart": copy, plus one level if the line ends with an opener (: { ( [ or begin/then/do...)
    A blank line yields "" (nothing to copy), so nothing is cleared after it.
    """
    if style == "off":
        return ""
    auto = leading_ws(line)
    if style == "smart":
        code = line.rstrip()
        tokens = re.findall(r"\b[a-zA-Z_]\w*\b", code.lower())
        if code and (code[-1] in INDENT_OPENERS or (tokens and tokens[-1] in SMART_WORDS)):
            auto += unit
    return auto


@dataclass(frozen=True)
class IndentPolicy:
    """
    mode:
      "off"   type text as-is, never clear anything (default)
      "copy"  auto-detect, editor copies the previous line's indent
      "smart" auto-detect, editor also adds a level after : { ( [ or block keywords
      "fixed" always press Backspace `fixed_count` times after Enter (legacy; can over-delete)
    tab_stop_backspace: the editor's Backspace removes a whole indent level at once
    """
    mode: str = "off"
    fixed_count: int = 0
    tab_stop_backspace: bool = False

    def __post_init__(self):
        if self.mode not in ("off", "copy", "smart", "fixed"):
            raise ValueError(f"unknown indent mode {self.mode!r}")

    def presses_after_enter(self, finished_line, unit, space_width=4):
        """Number of Backspace presses to issue right after Enter ends `finished_line`."""
        if self.mode == "off":
            return 0
        if self.mode == "fixed":
            return max(0, min(MAX_INDENT_BACKSPACES, self.fixed_count))
        auto = expected_auto_indent(finished_line, self.mode, unit)
        if not auto:
            return 0
        presses = indent_levels(auto, space_width) if self.tab_stop_backspace else len(auto)
        return min(MAX_INDENT_BACKSPACES, presses)


# --------------------------------------------------------------------------
# Pascal / Structured Code Block Parsing (for Coding Mode)
# --------------------------------------------------------------------------
def _strip_comments_and_strings(line: str) -> str:
    """Strip line comments, block comments, and string literals for accurate token matching."""
    # Strip line comments //
    line = re.sub(r"//.*$", "", line)
    # Strip block comments (* ... *) and { ... }
    line = re.sub(r"\(\*.*?\*\)", "", line)
    line = re.sub(r"\{.*?\}", "", line)
    # Strip string literals '...'
    line = re.sub(r"'[^']*'", "", line)
    return line


def find_pascal_blocks(lines: List[str], max_lookahead: int = 100) -> Dict[int, int]:
    """
    Scan forward to find matching `begin` and `end` blocks.
    Returns a dict mapping `start_line_index -> end_line_index`.
    Properly handles arbitrary nesting, comments, strings, and lookahead limits.
    """
    blocks = {}
    n = len(lines)
    for i in range(n):
        tokens = re.findall(r"\b[a-zA-Z_]\w*\b", _strip_comments_and_strings(lines[i]).lower())
        begins = tokens.count("begin")
        ends = tokens.count("end")
        if begins > ends:
            needed = begins - ends
            depth = needed
            limit = min(n, i + 1 + max_lookahead)
            for j in range(i + 1, limit):
                j_tokens = re.findall(r"\b[a-zA-Z_]\w*\b", _strip_comments_and_strings(lines[j]).lower())
                j_begins = j_tokens.count("begin")
                j_ends = j_tokens.count("end")
                depth += (j_begins - j_ends)
                if depth == 0:
                    blocks[i] = j
                    break
    return blocks


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------
@dataclass
class TypingProfile:
    """
    Tunable model parameters. Defaults are hand-chosen, NOT fit to any dataset.
    Absolute speed comes from the requested WPM (via calibration against planned
    traces); the Fitts parameters only shape *relative* timing, so only the
    ratio a:b matters.
    """
    # Relative key-distance effect: factor = (a + b*ID) / (a + b*ID_ref)
    fitts_a: float = 0.075
    fitts_b: float = 0.045
    fitts_ref_id: float = 1.6

    # Pace drift: OU process with mean 1.0. Specify the wanted stationary
    # coefficient of variation directly; sigma is derived (see ou_sigma).
    pace_cv: float = 0.05
    pace_theta: float = 0.40       # mean-reversion rate (1/s)
    pace_min: float = 0.75
    pace_max: float = 1.30

    # Fatigue (dimensionless). workload_rate is fatigue per second of ACTIVE
    # typing time; recovery_lambda is 1/s and only pauses let it recover.
    workload_rate: float = 0.002
    recovery_lambda: float = 0.02
    fatigue_max: float = 0.35
    fatigue_slowdown: float = 0.40   # speed reduced by this * fatigue
    fatigue_typo_gain: float = 3.0   # typo probability *= 1 + gain * fatigue

    # Key hold time: log-normal
    dwell_mu: float = -2.85          # exp(-2.85) ~ 0.058 s
    dwell_sigma: float = 0.20

    # Shift, modelled as a real modifier held around the base key (seconds / factors)
    shift_iki_lo: float = 1.05       # shifted characters take this much longer press-to-press
    shift_iki_hi: float = 1.18
    shift_lead_lo: float = 0.018     # Shift goes down this long before the base key...
    shift_lead_hi: float = 0.050     # (clamped to fit inside the gap after the previous key)
    shift_lag_lo: float = 0.006      # ...and comes up this long after the base key is released
    shift_lag_hi: float = 0.030

    @property
    def ou_sigma(self):
        """Diffusion coefficient implied by pace_cv: sigma = cv * sqrt(2*theta)."""
        return self.pace_cv * math.sqrt(2.0 * self.pace_theta)


@dataclass
class TypingState:
    pace_factor: float = 1.0
    fatigue: float = 0.0
    elapsed: float = 0.0      # simulated seconds
    prev_dwell: float = 0.0   # time the previous key (plus any Shift tail) stayed down


class TypingModel:
    def __init__(self, profile, rng, target_wpm, base_typo_rate):
        self.profile = profile
        self.rng = rng
        self.target_wpm = target_wpm
        self.base_typo_rate = base_typo_rate   # probability in [0, 1]
        self.timing_scale = 1.0

    # -- state evolution (exact OU discretisation, simulated time) ---------
    def advance(self, state, dt, working=True):
        p = self.profile
        dt = max(1e-3, dt)
        decay = math.exp(-p.pace_theta * dt)
        noise = p.pace_cv * math.sqrt(max(0.0, 1.0 - decay * decay)) * self.rng.gauss(0.0, 1.0)
        state.pace_factor = max(p.pace_min, min(p.pace_max, 1.0 + (state.pace_factor - 1.0) * decay + noise))

        work = p.workload_rate * dt if working else 0.0
        state.fatigue = max(0.0, min(p.fatigue_max, state.fatigue * math.exp(-p.recovery_lambda * dt) + work))
        state.elapsed += dt

    # -- timing -------------------------------------------------------------
    def sample_iki(self, state, prev_phys, prev_logical, cur, rng=None, origin=None):
        """
        Press-to-press interval.
        prev_phys    = last physical key struck overall (may be a special-key token)
        prev_logical = last character in the on-screen buffer
        origin       = key the moving hand travels FROM (defaults to prev_phys)
        """
        rng = rng or self.rng
        p = self.profile

        base = 60.0 / (5.0 * self.target_wpm) * self.timing_scale
        src = origin or prev_phys
        id_val = fitts_index_of_difficulty(src, cur) if src else p.fitts_ref_id
        fitts_factor = (p.fitts_a + p.fitts_b * id_val) / (p.fitts_a + p.fitts_b * p.fitts_ref_id)

        speed = max(0.1, state.pace_factor * (1.0 - p.fatigue_slowdown * state.fatigue))
        mean = max(MIN_IKI, base * fitts_factor / speed)

        alpha = max(1.8, 3.2 - 2.5 * state.fatigue)
        iki = rng.gammavariate(alpha, mean / alpha)

        if prev_logical and (prev_logical + cur).lower() in COMMON_DIGRAPHS:
            iki *= rng.uniform(0.60, 0.78)
        else:
            prev_hand = hand(prev_phys) if prev_phys else None
            cur_hand = hand(cur)
            if prev_hand is not None and cur_hand is not None:
                if prev_hand == cur_hand:
                    iki *= rng.uniform(0.92, 1.12)
                else:
                    iki *= rng.uniform(0.70, 0.86)

        if cur in SYNTAX_CHARS:
            iki *= rng.uniform(1.15, 1.45)

        if needs_shift(cur):      # reaching for Shift costs time before the key itself
            iki *= rng.uniform(p.shift_iki_lo, p.shift_iki_hi)

        return max(MIN_IKI, iki)

    def dwell_time(self, state, key):
        """`key` may be a text character or a special token (BACKSPACE/ENTER/TAB/NAV); normalised here."""
        p = self.profile
        key = _base_key(key)
        hold = self.rng.lognormvariate(p.dwell_mu + 0.12 * (1.0 - state.pace_factor), p.dwell_sigma)
        if key == " ":
            hold *= 1.22
        elif key in (BACKSPACE, ENTER, TAB):
            hold *= 1.35
        elif key in (UP, DOWN, LEFT, RIGHT, HOME, END_KEY):
            hold *= 1.15
        return max(0.018, min(0.140, hold))

    # -- errors -------------------------------------------------------------
    def typo_probability(self, state, origin, cur):
        p = self.profile
        id_val = fitts_index_of_difficulty(origin, cur) if origin else 0.0
        prob = self.base_typo_rate * (1.0 + 0.20 * id_val) * (1.0 + p.fatigue_typo_gain * state.fatigue)
        return min(0.5, prob)

    def misstrike(self, ch):
        near = NEIGHBOURS.get(ch.lower() if isinstance(ch, str) else ch)
        if not near:
            return ch
        wrong = self.rng.choice(near)
        return wrong.upper() if isinstance(ch, str) and ch.isupper() else wrong

    # -- first-stage calibration -------------------------------------------
    def calibrate(self, sample_text, samples=20000, iterations=3):
        """
        Solve for timing_scale so the mean neutral (pace=1, fatigue=0) inter-key
        interval over `sample_text` matches 60 / (5 * target_wpm).
        """
        text = sample_text.replace("\r", "").replace("\n", " ")
        if len(text) < 2:
            text = DEFAULT_SAMPLE.replace("\n", " ")
        pairs = [(text[k - 1], text[k]) for k in range(1, len(text))]
        target = 60.0 / (5.0 * self.target_wpm)
        neutral = TypingState()
        self.timing_scale = 1.0
        for _ in range(iterations):
            rng = random.Random(12345)
            total = 0.0
            for n in range(samples):
                prev, cur = pairs[n % len(pairs)]
                total += self.sample_iki(neutral, prev, prev, cur, rng)
            self.timing_scale *= target / (total / samples)
        return self.timing_scale


# --------------------------------------------------------------------------
# Planner: text -> absolute-timestamped event trace (pure, deterministic given RNG)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Event:
    """One canonical record of simulated behaviour. Times are seconds from trace start."""
    action: str        # "key" | "backspace" | "enter" | "shift" | "pause" | "nav"
    char: str          # logical char, token, "\n" for enter, "" for pauses
    press_at: float    # key-down time (for "pause": start of the pause)
    release_at: float  # key-up time   (for "pause": end of the pause)
    line: int          # source line index
    note: str = ""     # "typo:<kind>" | "indent[:first]" | "block_nav"

    @property
    def dwell(self):
        return self.release_at - self.press_at

    @property
    def key(self):
        """The physical key for this event ('' for pauses): base key for shifted chars."""
        return "" if self.action == "pause" else physical_key(self.char)


@dataclass(frozen=True)
class Transition:
    """One physical key-down or key-up, derived from the Event trace."""
    at: float
    down: bool
    key: str
    line: int


def events_to_transitions(events):
    """Flatten the trace into the time-ordered sequence of key-down / key-up transitions."""
    tagged = []
    for idx, ev in enumerate(events):
        if ev.action == "pause":
            continue
        tagged.append((ev.press_at, 1, idx, Transition(ev.press_at, True, ev.key, ev.line)))
        tagged.append((ev.release_at, 0, idx, Transition(ev.release_at, False, ev.key, ev.line)))
    tagged.sort(key=lambda t: t[:3])
    return [t[3] for t in tagged]


ERROR_KINDS = ["substitution", "transposition", "omission", "insertion", "overshoot"]
ERROR_WEIGHTS = [0.40, 0.20, 0.15, 0.15, 0.10]


class TypingPlanner:
    """
    Emits the physical keystroke stream. Supports standard typing as well as
    Coding Mode with block lookahead and realistic navigation.
    """

    def __init__(self, model, rng, indent=None, coding_mode: bool = False, max_lookahead: int = 100):
        self.model = model
        self.rng = rng
        self.indent = indent or IndentPolicy()
        self.coding_mode = coding_mode
        self.max_lookahead = max_lookahead
        self.unit = "    "         # detected per plan() call
        self.space_width = 4      # detected per plan() call
        self.state = TypingState()
        self.events = []
        self.buffer = []          # logical text on screen for the current line
        self.prev_phys = None     # last physical key struck (BACKSPACE, "\n", UP, etc.)
        self.hand_pos = {"L": None, "R": None}   # last key struck by each hand
        self.clock = 0.0          # simulated seconds since trace start
        self.line = 0

    # -- event emission -----------------------------------------------------
    def _emit(self, action, char, delay, dwell, note="", hold_extra=0.0):
        """Append one key event."""
        press_at = self.clock + delay
        release_at = press_at + dwell
        self.events.append(Event(action, char, press_at, release_at, self.line, note))
        self.clock = release_at + hold_extra
        self.model.advance(self.state, delay + dwell + hold_extra, working=True)
        self.state.prev_dwell = dwell + hold_extra

    def _pause(self, duration, note="pause"):
        start, end = self.clock, self.clock + duration
        self.events.append(Event("pause", "", start, end, self.line, note))
        self.clock = end
        self.model.advance(self.state, duration, working=False)
        self.state.prev_dwell = 0.0

    def _origin(self, ch):
        """Key the responsible hand moves from (falls back to the last key struck overall)."""
        h = motor_hand(ch)
        return (self.hand_pos[h] if h else None) or self.prev_phys

    def _struck(self, key):
        self.prev_phys = key
        h = motor_hand(key)
        if h:
            self.hand_pos[h] = key

    def _press(self, ch, note=""):
        tail = self.buffer[-1] if self.buffer else None
        iki = self.model.sample_iki(self.state, self.prev_phys, tail, ch, origin=self._origin(ch))
        dwell = self.model.dwell_time(self.state, ch)
        delay = max(MIN_GAP, iki - self.state.prev_dwell)

        shift = shift_key_for(ch)
        hold_extra = 0.0
        if shift:
            p = self.model.profile
            lead = min(self.rng.uniform(p.shift_lead_lo, p.shift_lead_hi), 0.75 * delay)
            hold_extra = self.rng.uniform(p.shift_lag_lo, p.shift_lag_hi)
            down = self.clock + delay
            self.events.append(Event("shift", shift, down - lead, down + dwell + hold_extra, self.line))

        self._emit("key", ch, delay, dwell, note, hold_extra)
        self.buffer.append(ch)
        self._struck(ch)

    def _nav(self, nav_key, note=""):
        """Emit a navigation keypress (e.g. UP, DOWN, END_KEY)."""
        iki = self.model.sample_iki(self.state, self.prev_phys, None, nav_key, origin=self._origin(nav_key))
        dwell = self.model.dwell_time(self.state, nav_key)
        delay = max(MIN_GAP, iki - self.state.prev_dwell)
        self._emit("nav", nav_key, delay, dwell, note)
        self._struck(nav_key)

    def _backspaces(self, count, first_delay, note="", first_note=None):
        for idx in range(count):
            if idx == 0:
                delay = self.rng.uniform(*first_delay)
            else:
                delay = max(0.022, 0.060 - idx * 0.004) + self.rng.uniform(-0.004, 0.006)
            dwell = self.model.dwell_time(self.state, BACKSPACE)
            self._emit("backspace", BACKSPACE, delay, dwell, first_note if (idx == 0 and first_note) else note)
            if self.buffer:
                self.buffer.pop()
            self._struck(BACKSPACE)

    def _enter(self, finished_line):
        dwell = self.model.dwell_time(self.state, ENTER)
        self._emit("enter", "\n", self.rng.uniform(0.06, 0.18), dwell)
        self.buffer.clear()
        self._struck("\n")
        presses = self.indent.presses_after_enter(finished_line, self.unit, self.space_width)
        if presses:
            self._backspaces(presses, (0.05, 0.12), note=INDENT_NOTE, first_note=INDENT_FIRST_NOTE)

    # -- error episodes ----------------------------------------------------
    def _build_episode(self, kind, text, i):
        """Return the episode keys (typed, then erased) for this error kind ([] if it would be invisible)."""
        cur = text[i]
        rest = text[i + 1:]
        rng = self.rng

        if kind == "substitution":
            wrong = self.model.misstrike(cur)
            return [wrong] if wrong != cur else []

        if kind == "transposition":
            nxt = rest[:1]
            if nxt and nxt.isalnum() and nxt != cur:
                return [nxt, cur] + list(rest[1:1 + rng.randint(0, 2)])
            return []

        if kind == "omission":
            keys = list(rest[:rng.randint(1, 3)])
            return keys if keys and keys[0] != cur else []

        if kind == "insertion":
            extra = self.model.misstrike(cur)
            if extra != cur:
                return [extra, cur] + list(rest[:rng.randint(0, 2)])
            return []

        # overshoot
        wrong = self.model.misstrike(cur)
        if wrong == cur:
            return []
        return [wrong] + list(rest[:rng.randint(1, 3)])

    def _episode_is_safe(self, episode_keys):
        """With a tab-stop Backspace, erasing whitespace typed on an otherwise blank line
        would remove a whole tab stop of *correct* indentation instead of one key."""
        if not self.indent.tab_stop_backspace:
            return True
        line_is_blank = not "".join(self.buffer).strip()
        return not (line_is_blank and any(k.isspace() for k in episode_keys))

    def _maybe_typo(self, text, i):
        cur = text[i]
        if not cur.isalnum():
            return
        if self.rng.random() >= self.model.typo_probability(self.state, self._origin(cur), cur):
            return

        kind = self.rng.choices(ERROR_KINDS, weights=ERROR_WEIGHTS)[0]
        episode_keys = self._build_episode(kind, text, i)
        if not episode_keys or not self._episode_is_safe(episode_keys):
            return

        for n, ch in enumerate(episode_keys):
            self._press(ch, note=f"typo:{kind}" if n == 0 else "typo")

        if kind == "substitution":
            first = (0.14, 0.28)
        else:
            self._pause(self.rng.uniform(0.18, 0.45), "realize")
            first = (0.055, 0.095)
        self._backspaces(len(episode_keys), first)

    # -- line / text planning ----------------------------------------------
    def _plan_line(self, text):
        i = 0
        burst = self.rng.randint(3, 9)
        while i < len(text):
            tail = self.buffer[-1] if self.buffer else None
            # Only trigger word-boundary thinking pauses when transitioning to a new non-whitespace token
            # to avoid unnatural pauses in the middle of indentation or multiple consecutive spaces.
            at_word_boundary = (tail and tail in " ;:()[],." and text[i] not in " \t")
            if burst <= 0 or at_word_boundary:
                if self.rng.random() < 0.20:
                    self._pause(min(1.2, self.rng.gammavariate(2.0, 0.18)), "think")
                burst = self.rng.randint(3, 10)

            self._maybe_typo(text, i)
            self._press(text[i])
            burst -= 1
            i += 1

    def _plan_lines_range(self, lines: List[str], start: int, end: int, blocks: Dict[int, int]):
        """Plan lines in range [start, end], applying block lookahead when in coding mode."""
        i = start
        while i <= end:
            if self.coding_mode and i in blocks and blocks[i] <= end:
                end_block = blocks[i]

                # 1. Type the begin/block-opener line
                self.line = i
                self._plan_line(lines[i])
                self._enter(lines[i])
                self._pause(self.rng.uniform(0.12, 0.35), "newline")

                inner_start = i + 1
                inner_end = end_block - 1

                if inner_start <= inner_end:
                    # 2. Create blank line between begin and end
                    dwell = self.model.dwell_time(self.state, ENTER)
                    self._emit("enter", "\n", self.rng.uniform(0.06, 0.18), dwell)
                    self.buffer.clear()
                    self._struck("\n")
                    presses = self.indent.presses_after_enter("", self.unit, self.space_width)
                    if presses:
                        self._backspaces(presses, (0.05, 0.12), note=INDENT_NOTE)
                    self._pause(self.rng.uniform(0.08, 0.20), "block_prep")

                    # 3. Type the end line
                    self.line = end_block
                    self._plan_line(lines[end_block])
                    self._pause(self.rng.uniform(0.12, 0.25), "block_close")

                    # 4. Navigate UP back to the blank line between begin and end
                    self._nav(UP, note="block_up")
                    self._pause(self.rng.uniform(0.10, 0.25), "nav_pause")

                    # 5. Plan all inner statements recursively
                    self._plan_lines_range(lines, inner_start, inner_end, blocks)

                    # 6. Navigate DOWN past the already-typed end line
                    self._nav(DOWN, note="block_down")
                    self._nav(END_KEY, note="block_end")
                    self._pause(self.rng.uniform(0.10, 0.25), "nav_pause")
                else:
                    # Empty block (begin immediately followed by end)
                    self.line = end_block
                    self._plan_line(lines[end_block])

                # If there are more lines following this block in this scope, press enter
                if end_block < end:
                    self._enter(lines[end_block])
                    self._pause(self.rng.uniform(0.12, 0.40), "newline")

                i = end_block + 1
            else:
                # Normal sequential line planning
                self.line = i
                self._plan_line(lines[i])
                if i < end:
                    self._enter(lines[i])
                    self._pause(self.rng.uniform(0.12, 0.40), "newline")
                i += 1

    def plan(self, text: str):
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        self.unit = detect_indent_unit(lines)
        self.space_width = detect_space_width(lines)
        blocks = find_pascal_blocks(lines, self.max_lookahead) if self.coding_mode else {}

        self._pause(self.rng.uniform(0.06, 0.22), "start")
        if lines:
            self._plan_lines_range(lines, 0, len(lines) - 1, blocks)
        return self.events


# --------------------------------------------------------------------------
# Trace analysis
# --------------------------------------------------------------------------
DEFAULT_SAMPLE = (
    "The quick brown fox jumps over the lazy dog while the rain falls on the hills.\n"
    "def add(a, b):\n    result = a + b  # sum the values\n    return result\n"
    "Typing is a motor skill; speed and accuracy improve with practice over time.\n"
)


def sample_corpus(n_chars):
    reps = n_chars // len(DEFAULT_SAMPLE) + 1
    return (DEFAULT_SAMPLE * reps)[:n_chars]


def trace_duration(events):
    """End of the trace. Not events[-1].release_at: a trailing Shift outlives its key."""
    return max((e.release_at for e in events), default=0.0)


def chain_intervals(events):
    """Press-to-press intervals between consecutive typed keys."""
    out, prev = [], None
    for ev in events:
        if ev.action == "shift":
            continue
        if ev.action == "key":
            if prev is not None:
                out.append((ev.press_at - prev.press_at, prev.char, ev.char))
            prev = ev
        else:
            prev = None
    return out


def _pct(sorted_vals, q):
    if not sorted_vals:
        return float("nan")
    return sorted_vals[min(len(sorted_vals) - 1, int(q * (len(sorted_vals) - 1) + 0.5))]


def summarize_trace(events, text):
    logical_chars = len(text.replace("\r\n", "\n").replace("\r", "\n"))
    total = trace_duration(events)
    chain = chain_intervals(events)
    ikis = [c[0] for c in chain]
    alt = [c[0] for c in chain if hand(c[1]) and hand(c[2]) and hand(c[1]) != hand(c[2])]
    same = [c[0] for c in chain if hand(c[1]) and hand(c[2]) and hand(c[1]) == hand(c[2])]
    dwells = [e.dwell for e in events if e.action not in ("pause", "shift")]
    shift_holds = [e.dwell for e in events if e.action == "shift"]
    kinds = Counter(e.note.split(":", 1)[1] for e in events if e.note.startswith("typo:"))
    episodes = sum(kinds.values())
    indent_backspaces = sum(1 for e in events if e.action == "backspace" and e.note.startswith(INDENT_NOTE))
    indent_lines = sum(1 for e in events if e.note == INDENT_FIRST_NOTE)
    backspaces = sum(1 for e in events if e.action == "backspace" and not e.note.startswith(INDENT_NOTE))
    nav_presses = sum(1 for e in events if e.action == "nav")
    s = sorted(ikis)
    nan = float("nan")
    mean_iki = statistics.fmean(ikis) if ikis else nan

    stats = {
        "events": len(events),
        "logical_chars": logical_chars,
        "total_seconds": total,
        "net_wpm": logical_chars / 5.0 / (total / 60.0) if total > 0 else nan,
        "gross_wpm": 60.0 / (5.0 * mean_iki) if ikis else nan,
        "mean_iki": mean_iki,
        "std_iki": statistics.pstdev(ikis) if ikis else nan,
        "median_iki": statistics.median(ikis) if ikis else nan,
        "p05_iki": _pct(s, 0.05),
        "p95_iki": _pct(s, 0.95),
        "mean_iki_alternating_hand": statistics.fmean(alt) if alt else nan,
        "mean_iki_same_hand": statistics.fmean(same) if same else nan,
        "mean_dwell": statistics.fmean(dwells) if dwells else nan,
        "std_dwell": statistics.pstdev(dwells) if dwells else nan,
        "shift_presses": len(shift_holds),
        "mean_shift_hold": statistics.fmean(shift_holds) if shift_holds else nan,
        "error_episodes": episodes,
        "error_rate_per_char": episodes / logical_chars if logical_chars else nan,
        "backspaces": backspaces,
        "correction_rate_per_char": backspaces / logical_chars if logical_chars else nan,
        "pauses": sum(1 for e in events if e.action == "pause"),
        "nav_presses": nav_presses,
        "indent_clear_lines": indent_lines,
        "indent_clear_backspaces": indent_backspaces,
    }
    for kind in ERROR_KINDS:
        stats[f"errors_{kind}"] = kinds.get(kind, 0)
    return stats


def format_stats(stats):
    lines = []
    for k, v in stats.items():
        lines.append(f"{k:30s} {v:.4f}" if isinstance(v, float) else f"{k:30s} {v}")
    return "\n".join(lines)


def export_trace_csv(events, path):
    """`char` is the logical character; `key` is the physical key struck."""
    def esc(s):
        return s.encode("unicode_escape").decode()

    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["action", "char", "press_at", "release_at", "line", "note", "key"])
        for e in events:
            w.writerow([e.action, esc(e.char), f"{e.press_at:.6f}", f"{e.release_at:.6f}",
                        e.line, e.note, esc(e.key)])


# --------------------------------------------------------------------------
# Calibration (closed loop against full planned traces)
# --------------------------------------------------------------------------
def _calibration_events(text, wpm, typo_rate, indent, scale, k, coding_mode=False):
    """Plan one deterministic calibration trace."""
    model = TypingModel(TypingProfile(), random.Random(999 + k), wpm, typo_rate)
    model.timing_scale = scale
    return TypingPlanner(model, random.Random(2000 + k), indent, coding_mode=coding_mode).plan(text)


def calibrate_trace(model, text, typo_rate, indent, mode, n_traces=3, iterations=3, scatter_traces=8, coding_mode=False):
    """Adjust timing_scale so the chosen WPM definition matches the target."""
    sample = text.replace("\r\n", "\n").replace("\r", "\n")[:4000]
    if len(sample) < 200:
        sample = sample_corpus(4000)

    if mode == "net":
        target = len(sample) / 5.0 * 60.0 / model.target_wpm
        to_wpm = lambda q: len(sample) / 5.0 * 60.0 / q
        measure = trace_duration
    else:
        target = 60.0 / (5.0 * model.target_wpm)
        to_wpm = lambda q: 60.0 / (5.0 * q)
        measure = lambda ev: statistics.fmean([c[0] for c in chain_intervals(ev)]) if chain_intervals(ev) else target

    def measurements(scale, n):
        return [measure(_calibration_events(sample, model.target_wpm, typo_rate, indent, scale, k, coding_mode))
                for k in range(n)]

    def quantity(scale):
        return statistics.fmean(measurements(scale, n_traces))

    s0 = model.timing_scale
    lo, hi = s0 * 0.3, s0 * 3.0
    s1 = s0 * 0.8
    q0 = quantity(s0)
    for _ in range(iterations):
        q1 = quantity(s1)
        if abs(q1 - q0) < 1e-9:
            break
        s2 = s1 + (target - q1) * (s1 - s0) / (q1 - q0)
        s0, q0 = s1, q1
        s1 = max(lo, min(hi, s2))
    model.timing_scale = s1

    final = measurements(s1, max(n_traces, scatter_traces))
    mean_q = statistics.fmean(final)
    cv = statistics.stdev(final) / mean_q if len(final) > 1 and mean_q > 0 else float("nan")
    return {"timing_scale": s1, "estimated_wpm": to_wpm(mean_q), "calibration_cv": cv}


def build_trace(text, wpm, typo_rate, indent, rng, mode="net", coding_mode=False, max_lookahead=100):
    """typo_rate is a probability in [0, 1] (NOT percent). Returns (events, info)."""
    if not 0.0 <= typo_rate <= 1.0:
        raise ValueError(f"typo_rate must be a probability in [0, 1], got {typo_rate!r}")
    if wpm <= 0:
        raise ValueError("wpm must be positive")
    if mode not in ("net", "gross"):
        raise ValueError("mode must be 'net' or 'gross'")

    model = TypingModel(TypingProfile(), rng, wpm, typo_rate)
    model.calibrate(text)
    cal = calibrate_trace(model, text, typo_rate, indent, mode, coding_mode=coding_mode)
    events = TypingPlanner(model, rng, indent, coding_mode=coding_mode, max_lookahead=max_lookahead).plan(text)
    return events, {"mode": mode, "coding_mode": coding_mode, **cal}


def simulate(text, wpm, typo_rate, indent, seed, n_chars=10000, mode="net", coding_mode=False):
    """Plan a trace without pressing any keys. Returns (events, info, text_used)."""
    if not text.strip():
        text = sample_corpus(n_chars)
    rng = random.Random(seed) if seed is not None else random.SystemRandom()
    events, info = build_trace(text, wpm, typo_rate, indent, rng, mode, coding_mode=coding_mode)
    return events, info, text


def run_benchmark(text, wpm, typo_rate, indent, seed, n_chars=10000, mode="net", coding_mode=False):
    events, info, used = simulate(text, wpm, typo_rate, indent, seed, n_chars, mode, coding_mode=coding_mode)
    stats = summarize_trace(events, used)
    stats["requested_wpm"] = wpm
    stats["mode"] = mode
    stats["coding_mode"] = coding_mode
    stats["calibration_estimate_wpm"] = info["estimated_wpm"]
    stats["calibration_cv"] = info["calibration_cv"]
    return stats


# --------------------------------------------------------------------------
# Keyboard output
# --------------------------------------------------------------------------
class KeyboardOutput:
    """Thin pynput wrapper. No fallbacks: any failure propagates to the caller."""

    def __init__(self):
        from pynput.keyboard import Controller, Key  # lazy: model/benchmark work without it
        self._ctl = Controller()
        shift_l = getattr(Key, "shift_l", getattr(Key, "shift", None))
        shift_r = getattr(Key, "shift_r", getattr(Key, "shift", None))
        self._special = {
            "\t": Key.tab, "\n": Key.enter, TAB: Key.tab, ENTER: Key.enter,
            BACKSPACE: Key.backspace, SHIFT_L: shift_l, SHIFT_R: shift_r,
            UP: Key.up, DOWN: Key.down, LEFT: Key.left, RIGHT: Key.right,
            HOME: Key.home, END_KEY: Key.end,
        }

    def resolve(self, key):
        return self._special.get(key, key)

    def press(self, key):
        self._ctl.press(key)

    def release(self, key):
        self._ctl.release(key)


# --------------------------------------------------------------------------
# Playback (no Tk: takes any keyboard / stop-event / clock, so it is unit-testable)
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class PlaybackPolicy:
    stall_threshold: float = 0.25
    resync_on_stall: bool = True


def sleep_until(deadline, stop_event, clock=time.monotonic, poll=0.015):
    """Interruptible sleep to an absolute deadline on `clock`. False if stopped."""
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            return not stop_event.is_set()
        if stop_event.wait(min(remaining, poll)):
            return False


class TracePlayer:
    """Plays an Event trace as absolute-deadline key transitions (t0 + timestamp)."""

    def __init__(self, keyboard, stop_event, policy=None, clock=time.monotonic, on_line=None):
        self.keyboard = keyboard
        self.stop_event = stop_event
        self.policy = policy or PlaybackPolicy()
        self.clock = clock
        self.on_line = on_line

    def play(self, events):
        """Returns True if the whole trace was played, False if stopped."""
        policy = self.policy
        t0 = self.clock()
        held = []
        current_line = -1
        try:
            for tr in events_to_transitions(events):
                if self.stop_event.is_set():
                    return False

                if tr.down and policy.resync_on_stall:
                    lag = self.clock() - (t0 + tr.at)
                    if lag > policy.stall_threshold:
                        t0 += lag

                if not sleep_until(t0 + tr.at, self.stop_event, self.clock):
                    return False

                key = self.keyboard.resolve(tr.key)
                if tr.down:
                    if tr.line != current_line:
                        current_line = tr.line
                        if self.on_line:
                            self.on_line(current_line)
                    self.keyboard.press(key)
                    held.append(key)
                else:
                    self.keyboard.release(key)
                    if key in held:
                        held.remove(key)
            return True
        finally:
            for key in reversed(held):
                self.keyboard.release(key)


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
MODE_LABELS = {
    "Net (incl. pauses & fixes)": "net",
    "Gross (keystroke rate)": "gross",
}

INDENT_LABELS = {
    "Off (type text as-is)": "off",
    "Auto-detect: copy previous line's indent": "copy",
    "Auto-detect: smart (+1 level after : { ( [ / begin)": "smart",
    "Fixed count (use box below)": "fixed",
}


@dataclass(frozen=True)
class RunConfig:
    """Immutable snapshot of UI settings, taken on the Tk thread."""
    wpm: int
    mode: str
    typo_rate: float      # probability in [0, 1]
    countdown: int
    indent: IndentPolicy
    coding_mode: bool
    seed: Optional[int]
    playback: PlaybackPolicy = PlaybackPolicy()


_TkBase = tk.Tk if tk is not None else object


class AutoTyperApp(_TkBase):
    def __init__(self):
        super().__init__()
        self.title("Keystroke Trace Simulator")
        self.geometry("660x920")
        self.resizable(True, True)

        self.is_running = False
        self.stop_event = threading.Event()
        self.msg_queue = queue.Queue()
        self.worker_thread = None

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_id = self.after(50, self._poll_queue)

    # -- UI ----------------------------------------------------------------
    def _build_ui(self):
        ttk.Label(self, text="Keystroke Trace Simulator", font=("Segoe UI", 16, "bold")).pack(pady=12)

        cfg = ttk.LabelFrame(self, text=" Typing & Execution Config ", padding=12)
        cfg.pack(fill="x", padx=20, pady=5)

        def spin_row(r, label, var, **kw):
            ttk.Label(cfg, text=label).grid(row=r, column=0, sticky="w", pady=4)
            ttk.Spinbox(cfg, textvariable=var, width=10, **kw).grid(row=r, column=1, sticky="e")

        self.wpm_var = tk.IntVar(value=110)
        spin_row(0, "Target Speed (WPM):", self.wpm_var, from_=40, to=160)

        ttk.Label(cfg, text="Speed definition:").grid(row=1, column=0, sticky="w", pady=4)
        self.mode_var = tk.StringVar(value="Net (incl. pauses & fixes)")
        ttk.Combobox(cfg, textvariable=self.mode_var, values=list(MODE_LABELS), state="readonly", width=26
                     ).grid(row=1, column=1, sticky="e")

        self.typo_var = tk.DoubleVar(value=3.0)
        spin_row(2, "Base Typo Rate (%):", self.typo_var, from_=0.0, to=15.0, increment=0.5)
        self.delay_var = tk.IntVar(value=4)
        spin_row(3, "Countdown Delay (sec):", self.delay_var, from_=1, to=30)

        ttk.Label(cfg, text="Editor auto-indent:").grid(row=4, column=0, sticky="w", pady=4)
        self.indent_mode_var = tk.StringVar(value="Off (type text as-is)")
        ttk.Combobox(cfg, textvariable=self.indent_mode_var, values=list(INDENT_LABELS), state="readonly", width=38
                     ).grid(row=4, column=1, sticky="e")
        self.indent_var = tk.IntVar(value=4)
        spin_row(5, "Fixed mode only: Backspaces after every Enter:", self.indent_var, from_=0, to=16)
        self.tabstop_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(cfg, text="My editor's Backspace removes a whole indent level at once (VS Code-style)",
                        variable=self.tabstop_var).grid(row=6, column=0, columnspan=2, sticky="w", pady=(2, 4))

        self.coding_mode_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(cfg, text="Coding mode: Look ahead for begin/end blocks (types blocks then fills body)",
                        variable=self.coding_mode_var).grid(row=7, column=0, columnspan=2, sticky="w", pady=(2, 4))

        self.det_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(cfg, text="Deterministic test mode (fixed seed)", variable=self.det_var
                        ).grid(row=8, column=0, sticky="w", pady=(8, 2))
        self.seed_var = tk.IntVar(value=12345)
        ttk.Spinbox(cfg, textvariable=self.seed_var, from_=0, to=2**31 - 1, width=10
                    ).grid(row=8, column=1, sticky="e")

        items = ttk.LabelFrame(self, text=" Paste Your Text Below ", padding=10)
        items.pack(fill="both", expand=True, padx=20, pady=10)

        tools = ttk.Frame(items)
        tools.pack(fill="x", pady=(0, 5))
        ttk.Button(tools, text="Clear Text", command=self._clear_text).pack(side="left", padx=(0, 5))
        ttk.Button(tools, text="Paste Clipboard", command=self._paste_clipboard).pack(side="left", padx=(0, 5))
        ttk.Button(tools, text="Run Benchmark (no keys)", command=self._benchmark).pack(side="left")

        box = ttk.Frame(items)
        box.pack(fill="both", expand=True)
        self.text_box = tk.Text(box, height=10, font=("Consolas", 10), wrap="none")
        sy = ttk.Scrollbar(box, orient="vertical", command=self.text_box.yview)
        sx = ttk.Scrollbar(box, orient="horizontal", command=self.text_box.xview)
        self.text_box.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        sy.pack(side="right", fill="y")
        sx.pack(side="bottom", fill="x")
        self.text_box.pack(side="left", fill="both", expand=True)
        self.text_box.bind("<Control-a>", self._select_all_text)
        self.text_box.bind("<Control-A>", self._select_all_text)

        status = ttk.Frame(self, padding=10)
        status.pack(fill="x", padx=20)
        self.status_label = ttk.Label(status, text="Status: Ready", font=("Segoe UI", 10, "bold"), foreground="gray")
        self.status_label.pack(anchor="w")
        self.progress = ttk.Progressbar(status, mode="determinate")
        self.progress.pack(fill="x", pady=5)

        btns = ttk.Frame(self, padding=10)
        btns.pack(fill="x", padx=20, pady=5)
        self.start_btn = ttk.Button(btns, text="Start Auto-Typer", command=self.start_process)
        self.start_btn.pack(side="left", fill="x", expand=True, padx=5)
        self.stop_btn = ttk.Button(btns, text="Stop", command=self.stop_process, state="disabled")
        self.stop_btn.pack(side="right", fill="x", expand=True, padx=5)

    def _clear_text(self):
        self.text_box.delete("1.0", tk.END)

    def _paste_clipboard(self):
        try:
            content = self.clipboard_get()
        except tk.TclError:
            messagebox.showinfo("Clipboard", "Clipboard is empty or contains non-text data.")
            return
        self.text_box.delete("1.0", tk.END)
        self.text_box.insert("1.0", content)

    def _select_all_text(self, event=None):
        self.text_box.tag_add("sel", "1.0", "end")
        return "break"

    # -- config snapshot (Tk thread only) ------------------------------------
    def _snapshot_config(self):
        """The only place percent is converted to a probability."""
        return RunConfig(
            wpm=max(40, min(160, self.wpm_var.get())),
            mode=MODE_LABELS[self.mode_var.get()],
            typo_rate=max(0.0, min(0.5, self.typo_var.get() / 100.0)),
            countdown=max(1, self.delay_var.get()),
            indent=IndentPolicy(
                mode=INDENT_LABELS[self.indent_mode_var.get()],
                fixed_count=max(0, min(MAX_INDENT_BACKSPACES, self.indent_var.get())),
                tab_stop_backspace=self.tabstop_var.get(),
            ),
            coding_mode=self.coding_mode_var.get(),
            seed=self.seed_var.get() if self.det_var.get() else None,
        )

    def _benchmark(self):
        try:
            cfg = self._snapshot_config()
        except tk.TclError:
            messagebox.showerror("Error", "Please enter valid numeric configuration values.")
            return
        text = self.text_box.get("1.0", "end-1c")
        stats = run_benchmark(text, cfg.wpm, cfg.typo_rate, cfg.indent, cfg.seed,
                              mode=cfg.mode, coding_mode=cfg.coding_mode)
        src = "your text" if text.strip() else "a 10,000-char sample"
        cm_note = ", coding mode ON" if cfg.coding_mode else ""
        messagebox.showinfo("Benchmark (simulated, no keys pressed)",
                            f"Requested: {cfg.wpm} WPM ({cfg.mode}){cm_note}, typo {cfg.typo_rate:.1%}, "
                            f"indent {cfg.indent.mode} on {src}\n\n"
                            + format_stats(stats))

    # -- worker -> GUI message queue --------------------------------------
    def _post(self, *msg):
        self.msg_queue.put(msg)

    def _poll_queue(self):
        try:
            while True:
                msg = self.msg_queue.get_nowait()
                kind = msg[0]
                if kind == "status":
                    self.status_label.config(text=msg[1], foreground=msg[2])
                elif kind == "progress":
                    if msg[2] is not None:
                        self.progress["maximum"] = msg[2]
                    self.progress["value"] = msg[1]
                elif kind == "done":
                    self._finish(msg[1])
                elif kind == "error":
                    self._finish("Failed")
                    messagebox.showerror("Typing failed", msg[1])
        except queue.Empty:
            pass
        self._poll_id = self.after(50, self._poll_queue)

    def _finish(self, final_status):
        self.is_running = False
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self.status_label.config(text=f"Status: {final_status}", foreground="gray")

    # -- control ------------------------------------------------------------
    def start_process(self):
        full_text = self.text_box.get("1.0", "end-1c")
        if not full_text.strip():
            messagebox.showwarning("Warning", "Text box cannot be empty! Paste or type text first.")
            return
        try:
            config = self._snapshot_config()
        except tk.TclError:
            messagebox.showerror("Error", "Please enter valid numeric configuration values.")
            return

        self.stop_event.clear()
        self.is_running = True
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.worker_thread = threading.Thread(target=self._worker, args=(full_text, config), daemon=True)
        self.worker_thread.start()

    def stop_process(self):
        self.stop_event.set()
        self.status_label.config(text="Status: Stopping...", foreground="red")

    def _on_close(self):
        self.stop_event.set()
        self.after_cancel(self._poll_id)
        self.destroy()

    # -- worker thread (never touches Tk) -----------------------------------
    def _worker(self, text, config):
        try:
            self._post("status", "Status: Planning...", "gray")
            rng = random.Random(config.seed) if config.seed is not None else random.SystemRandom()
            events, info = build_trace(text, config.wpm, config.typo_rate, config.indent, rng,
                                       config.mode, coding_mode=config.coding_mode)
            total_lines = max((e.line for e in events), default=0) + 1 if events else 1
            est = info["estimated_wpm"]
            note = ""
            if est < 0.97 * config.wpm:
                note = f" (max reachable {config.mode} speed with these settings ~{est:.0f} WPM)"

            for sec in range(config.countdown, 0, -1):
                self._post("status", f"Click into target box! Starting in {sec}s...{note}", "orange")
                if not sleep_until(time.monotonic() + 1.0, self.stop_event):
                    self._post("done", "Cancelled")
                    return

            def on_line(line):
                self._post("status", f"Typing line {line + 1} of {total_lines}...", "green")
                self._post("progress", line + 1, None)

            keyboard = KeyboardOutput()
            self._post("progress", 0, total_lines)
            player = TracePlayer(keyboard, self.stop_event, config.playback, on_line=on_line)
            finished = player.play(events)
            self._post("done", "Completed" if finished else "Stopped")
        except Exception as err:
            self._post("error", f"{type(err).__name__}: {err}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Keystroke trace simulator")
    ap.add_argument("--benchmark", action="store_true", help="simulate only; print statistics and exit")
    ap.add_argument("--wpm", type=int, default=110)
    ap.add_argument("--mode", choices=["net", "gross"], default="net", help="WPM definition (see module docstring)")
    ap.add_argument("--typo", type=float, default=3.0, help="base typo rate in PERCENT (converted once below)")
    ap.add_argument("--indent", choices=["off", "copy", "smart", "fixed"], default="off",
                    help="editor auto-indent handling (see IndentPolicy)")
    ap.add_argument("--indent-fixed", type=int, default=4, help="Backspace count for --indent fixed")
    ap.add_argument("--tab-stop-backspace", action="store_true",
                    help="editor Backspace removes a whole indent level at once")
    ap.add_argument("--coding-mode", action="store_true",
                    help="enable coding mode: look ahead for begin/end blocks, write block frame, then fill body")
    ap.add_argument("--max-lookahead", type=int, default=100,
                    help="maximum lines to look ahead for matching end in coding mode")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--chars", type=int, default=10000)
    ap.add_argument("--file", type=str, default=None, help="text file to simulate instead of the built-in sample")
    ap.add_argument("--csv", type=str, default=None, help="export the simulated trace to this CSV path")
    args = ap.parse_args(argv)

    if args.benchmark:
        typo_probability = args.typo / 100.0
        text = ""
        if args.file:
            with open(args.file, encoding="utf-8") as fh:
                text = fh.read()
        policy = IndentPolicy(args.indent, args.indent_fixed, args.tab_stop_backspace)
        events, info, used = simulate(text, args.wpm, typo_probability, policy,
                                      args.seed, args.chars, args.mode, coding_mode=args.coding_mode)
        stats = summarize_trace(events, used)
        cm_str = " (coding-mode)" if args.coding_mode else ""
        print(f"Requested: {args.wpm} WPM ({args.mode}){cm_str}   typo: {args.typo}%   seed: {args.seed}")
        print(f"Calibration estimate: {info['estimated_wpm']:.2f} WPM ({args.mode}), "
              f"measured trace-to-trace scatter (CV): {info['calibration_cv']:.2%}\n")
        print(format_stats(stats))
        if args.csv:
            export_trace_csv(events, args.csv)
            print(f"\nTrace written to {args.csv}")
        return 0

    if tk is None:
        print("tkinter is not available in this Python install; use --benchmark for headless runs.",
              file=sys.stderr)
        return 1
    AutoTyperApp().mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
