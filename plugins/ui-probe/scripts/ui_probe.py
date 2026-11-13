#!/usr/bin/env python3
"""ui-probe: watch a mobile app from outside while a scripted scenario runs.

The probe knows nothing about the app under test. A scenario file supplies the package, the
launch component and a list of steps in normalized screen coordinates, or with tap targets named
by their text or content description and looked up on the device. Everything is read through adb
from an unmodified build:

  * frame counts and per-frame timing  : dumpsys gfxinfo <package> framestats   (HWUI apps)
  * compositor frame counts per layer  : dumpsys SurfaceFlinger --timestats     (any toolkit)
  * compositor jank classes            : Perfetto FrameTimeline (--trace; any toolkit)
  * trace sections                     : android.os.Trace sections (--sections; names from the scenario)
  * CPU time                           : /proc/<pid>/stat (the process, including threads that have
                                         exited) and /proc/<pid>/task/*/stat (the live threads)
  * memory                             : dumpsys meminfo <package>
  * tap targets (find)                 : uiautomator dump, matched on the device
  * what the screen showed             : screenrecord, one video frame per screen change
  * requests the app sent (--requests) : the log of a front the app's traffic passes through
                                         (`ui-probe http-front`, or `tls-front` on the iOS Simulator)

Three kinds of result, three rules:
  exact       counts (frames while nothing happens, CPU at rest, frames for a step that waits on
              nothing): gate them, no tolerance.
  structural  the order of visual states a step passes through (its signature): gate against an
              approved signature.
  statistical every duration, and any count that time or the network drives: report the
              distribution, never gate on an emulator or simulator.

Commands: run, rehearse, analyze, summary, gate, compare, sheet, zoom, film, proxy, tls-front, http-front, doctor.
See README.md.
Exit codes: 0 success, 1 a gate failed, 2 an error the tool reports itself, 3 an unexpected error.
"""
from __future__ import annotations

import argparse
import datetime
import fcntl
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

VERSION = "0.9.0"
HERE = Path(__file__).resolve().parent
DEV_DIR = "/data/local/tmp/uiprobe"
RECORD_LIMIT_S = 180          # screenrecord's documented maximum
STEP_KINDS = ("rest", "tap", "swipe", "key", "text", "url")
ACTION_KINDS = ("tap", "swipe", "key", "text", "url")     # every kind but rest: an input, then a wait
RESERVED_IDS = ("launch", "T0", "END")
FFMPEG_MIN = (5, 1)           # -fps_mode, used to extract recorded frames, arrived in FFmpeg 5.1

# Scenario keys. A key outside these sets is an error (a typo would otherwise be ignored and change
# what is gated), except a key whose name ends in "comment".
PACKAGE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+")
ACTIVITY_RE = re.compile(r"[A-Za-z0-9_.$]+")
# An iOS bundle identifier: dot-separated names of letters, digits and '-'.
BUNDLE_RE = re.compile(r"[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")
ANDROID = "android"
SIM_PLATFORM = "ios-simulator"
LAUNCH_ENV_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]*")
LAUNCH_ENV_MAX = 20
LAUNCH_ENV_VALUE_MAX = 200
TOP_KEYS = {"name", "package", "activity", "launch_url", "until_quiet", "quiet", "launch_settle_ms", "launch_env", "count_signposts", "steps",
            "count_sections", "gate", "launch_requests_max", "watch_min_ms"}
STEP_KEYS = {
    "rest": {"ms", "meminfo", "background", "allow", "watch"},
    "tap": {"x", "y", "find", "find_settle_ms", "settle_ms", "until_quiet", "quiet"},
    "swipe": {"from", "to", "ms", "judge", "settle_ms", "until_quiet", "quiet"},
    "key": {"code", "settle_ms", "until_quiet", "quiet"},
    "text": {"text", "settle_ms", "until_quiet", "quiet"},
    "url": {"url", "settle_ms", "until_quiet", "quiet"},
}
COMMON_STEP_KEYS = {"id", "do", "expect"}
EXPECT_KEYS = {"signature", "frames", "frames_max", "sections_max", "signposts_max", "requests_max"}
QUIET_DEFAULTS = {"ms": 800, "max_ms": 15000}
# A rest step's allowance: limits that replace the gate's for that step. unchanged_frames_max lets
# recorded frames that changed nothing pass up to a number (some carets compose them); without it
# that check stays at zero, and it is what catches an invisible animation.
ALLOW_LIMITS = ("frames_max", "changed_frames_max", "cpu_pct_max", "updates_max", "unchanged_frames_max", "requests_max")
# allow.regions: parts of the app's area (fractions [x0, y0, x1, y1]) where a change counts against the
# region's own limit and not against the rest. Each carries its limit and its reason.
REGION_KEYS = ("region", "changed_frames_max", "reason")
REGIONS_MAX = 16
# A rest marked "watch": true is one a person or an agent watches for a change made rarely; it lasts at
# least this long (a window of exactly one minute can miss a change made once a minute). A scenario may
# set its own minimum with a top-level "watch_min_ms".
WATCH_MIN_MS = 70000
WATCH_MIN_FLOOR_MS = 10000    # the least a scenario's watch_min_ms may be
# A film no run made (`ui-probe film`): its scenario is built from the windows file and marked
# "film": true; every step is a rest with "start_s" and "end_s" in the film's own seconds.
FILM_PLATFORM = "film"
FILM_TOP_KEYS = {"film", "name", "steps", "watch_min_ms"}
FILM_STEP_KEYS = {"id", "do", "start_s", "end_s", "watch", "allow"}
FILM_ALLOW_KEYS = ("changed_frames_max", "regions", "reason")
FILM_NOT_CHECKED = ("frames by any count", "CPU", "requests", "the app's process", "which app is in front")
FILM_CAUTION_PCT = 70         # ui_video.FILM_CAUTION, as a percentage for messages
FIND_KEYS = {"text", "desc", "nth", "timeout_ms", "package", "scroll"}
# find.scroll: swipes made while a read shows no acceptable match. max is required; from and to are
# screen fractions like a swipe step's; the swipe must move at least SCROLL_MIN_TRAVEL of the screen
# on one axis, since a swipe that barely moves is a tap.
SCROLL_KEYS = {"from", "to", "max", "ms", "settle_ms"}
SCROLL_DEFAULTS = {"ms": 600, "settle_ms": 800}
SCROLL_MAX = 20
SCROLL_MIN_TRAVEL = 0.1
FIND_ATTRS = {"text": "text", "desc": "content-desc"}   # scenario key -> attribute of the window tree
FIND_SETTLE_MS = 1000         # after a lookup, before the baseline reading: the lookup's own work settles
FIND_READ_S = 2.5             # about how long one read of the window tree takes (measured: about 2 s)
FIND_TIMEOUT_MIN_MS = 100
LOOKUP_FAILED = "lookup-failed.txt"   # the step whose element never appeared; outside the find-*.txt names
FAILED_RUN = "failed.json"            # written into a run folder when the run did not complete
TEXT_MAX = 200
# A link a 'url' step opens: a scheme, a colon, then at least one character, at most URL_MAX in all.
# Every character is printable ASCII. Refused, though the argument is single-quoted on the device:
# what a shell reads specially anywhere it can reach (a quote of any kind, the backslash, $ ; | < > ( )
# { } * ! ~ ^ and the space), so that the link means the same in every shell it may pass through. `#`
# is allowed (a fragment): the only place a shell reads it specially is at the start of a word, and the
# link is always one quoted word that starts with its scheme.
URL_MAX = 500
URL_SCHEME_RE = re.compile(r"[a-z][a-z0-9+.-]*:.+", re.S)
URL_REFUSED = set(" '\"`\\$;|<>(){}*!~^")
def link_problem(value, key: str) -> str | None:
    """What is wrong with a link a 'url' step or 'launch_url' opens (see URL_REFUSED), or None; the
    message names the first refused character."""
    if not isinstance(value, str) or not value:
        return f"'{key}' must be a non-empty string"
    if len(value) > URL_MAX:
        return f"'{key}' is {len(value)} characters; at most {URL_MAX}"
    bad = next((ch for ch in value if not 0x21 <= ord(ch) <= 0x7E or ch in URL_REFUSED), None)
    if bad is not None:
        return (f"'{key}' holds {bad!r} (U+{ord(bad):04X}): a link is printable ASCII without spaces, quotes, "
                "backslashes or any of $ ; | < > ( ) { } * ! ~ ^")
    if not URL_SCHEME_RE.fullmatch(value):
        return (f"'{key}' must start with a scheme (a lower-case letter, then lower-case letters, digits, '+', '.' "
                "or '-'), a colon, then the rest of the link, such as example://join/abc")
    return None


TEXT_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 @._+-")


def default_settle_ms(do: str) -> int:
    """The fixed wait after an action that does not wait for quiet."""
    return 3000 if do in ("tap", "url") else 2500


def xml_escape(value: str) -> str:
    """A value as the window-tree dump writes it inside an attribute: & < > and " become entities;
    an apostrophe and non-ASCII text are written as they are."""
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def find_needle(find: dict) -> str:
    """The text that identifies the element in one node of the window tree: the attribute with a
    leading space (so that no attribute whose name merely ends the same way matches), exactly."""
    key = "text" if "text" in find else "desc"
    return f' {FIND_ATTRS[key]}="{xml_escape(find[key])}"'


def find_scroll(step: dict) -> dict | None:
    """The step's find.scroll with its defaults filled in, or None."""
    sc = step["find"].get("scroll")
    return None if sc is None else {**SCROLL_DEFAULTS, **sc}


def find_timeout_ms(scn: dict, step: dict) -> int:
    """find.timeout_ms, else the scenario's quiet.max_ms, plus, with scroll, room for every swipe:
    the swipe, its settle time and one read of the window tree after it."""
    if "timeout_ms" in step["find"]:
        return int(step["find"]["timeout_ms"])
    sc = find_scroll(step)
    room = 0 if sc is None else sc["max"] * (sc["ms"] + sc["settle_ms"] + FIND_READ_S * 1000)
    return int(quiet_settings(scn)[1] + room)


def find_package(scn: dict, step: dict) -> str:
    """The package whose element a lookup may match: the step's 'find.package', else the app's."""
    return step["find"].get("package", scn["package"])


def unmatchable_char(value: str) -> str | None:
    """The first character of a lookup value that the window-tree dump never writes as it is, or
    None. The dump writes a tab, a newline and a carriage return as numeric references, a character
    outside the Basic Multilingual Plane as a numeric reference, and replaces control characters
    and the characters XML does not allow; a value holding one can never match."""
    for ch in value:
        o = ord(ch)
        if (o < 0x20 or 0x7F <= o <= 0x9F or 0xD800 <= o <= 0xDFFF or o > 0xFFFF or o in (0xFFFE, 0xFFFF)):
            return ch
    return None

TRACE_CONFIG = """\
buffers { size_kb: 65536 fill_policy: RING_BUFFER }
buffers { size_kb: 8192 fill_policy: RING_BUFFER }
data_sources { config { name: "android.surfaceflinger.frametimeline" } }
data_sources { config { name: "linux.process_stats" target_buffer: 1 process_stats_config { scan_all_processes_on_start: true } } }
duration_ms: %d
"""

# Added with --sections: userspace trace sections (android.os.Trace). The "view" category carries the
# platform view system's own per-frame sections; atrace_apps turns on the sections the app itself
# emits. The graphics category is left out on purpose: on an emulator it adds thousands of slices.
SECTIONS_CONFIG = """data_sources { config { name: "linux.ftrace" ftrace_config {
  atrace_categories: "view" atrace_categories: "input" atrace_apps: "%s"
  buffer_size_kb: 16384 } } }
"""

# Counted for every app: the platform view system's own sections. A scenario file may add
# framework-specific names under "count_sections"; the probe counts whatever names it is given.
DEFAULT_SECTIONS = ["Choreographer#doFrame", "traversal", "draw", "Record View#draw()", "deliverInputEvent"]

# A key of gate.periodic_requests: a request as the front's log gives it, "METHOD /path/shape".
REQUEST_KEY_RE = re.compile(r"[A-Z]+ /[^ ]*")
GATE_DEFAULTS = {"rest_frames_max": 0, "rest_cpu_pct_max": 2.0, "rest_updates_max": 0, "rest_requests_max": 0}
SIM_ONLY = "the iOS Simulator only: its log store is what counts it"

# The line of `dumpsys activity activities` that names the resumed activity (the first match is
# taken; the name of the line differs between platform versions).
FOREGROUND_GREP = "topResumedActivity|mResumedActivity|ResumedActivity"

# The three counts of an app's frames, by source name and run.json key.
FRAME_COUNTS = (("platform", "frames"), ("compositor-stats", "frames_layers"), ("compositor-trace", "frames_compositor"))


class ProbeError(RuntimeError):
    """A condition the caller must see: never downgraded to a default."""


def adb(serial: str, *args: str, check: bool = True, timeout: int = 600, binary: bool = False):
    try:
        proc = subprocess.run(["adb", "-s", serial, *args], capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"adb {' '.join(args)[:200]} did not finish within {timeout} s") from exc
    except OSError as exc:
        raise ProbeError(f"cannot run adb: {exc.strerror or exc}") from exc
    if check and proc.returncode != 0:
        said = proc.stderr.decode(errors="replace").strip()[:400] or proc.stdout.decode(errors="replace").strip()[-400:]
        raise ProbeError(f"adb {' '.join(args)} failed (exit {proc.returncode}): {said or 'no output'}")
    return proc.stdout if binary else proc.stdout.decode(errors="replace")


def sh(serial: str, command: str, **kw) -> str:
    return adb(serial, "shell", command, **kw)


def q(value: str) -> str:
    """Single-quote a value for the device shell (and any POSIX shell): nothing inside is expanded."""
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


def host_load() -> float:
    return os.getloadavg()[0]


def read_json(path: Path) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except OSError as exc:
        raise ProbeError(f"cannot read {path}: {exc.strerror or exc}") from exc
    except json.JSONDecodeError as exc:
        raise ProbeError(f"{path} is not valid JSON: {exc}") from exc


# --------------------------------------------------------------------------------------------
# scenario
# --------------------------------------------------------------------------------------------

def quiet_settings(scn: dict, step: dict | None = None) -> tuple[int, int]:
    """(quiet ms, max ms) for the launch (step None) or a step. A step's own 'quiet' replaces the
    scenario's values for the keys it gives; the scenario's fill in the rest."""
    merged = {**QUIET_DEFAULTS, **(scn.get("quiet") or {}), **((step or {}).get("quiet") or {})}
    return int(merged["ms"]), int(merged["max_ms"])


def step_until_quiet(scn: dict, step: dict | None) -> bool:
    if step is not None and "until_quiet" in step:
        return bool(step["until_quiet"])
    return bool(scn.get("until_quiet", False))


def load_scenario(path: str) -> dict:
    try:
        return json.loads(Path(path).read_text())
    except OSError as exc:
        raise ProbeError(f"cannot read scenario {path}: {exc.strerror or exc}") from exc
    except json.JSONDecodeError as exc:
        raise ProbeError(f"scenario {path} is not valid JSON: {exc}") from exc


def _is_comment(key) -> bool:
    return isinstance(key, str) and key.endswith("comment")


def validate_scenario(scn: dict, platform: str = ANDROID, sim_mode: str = "run") -> None:
    """Reject a malformed scenario with a message that names the problem. No silent defaults.
    platform is ANDROID, SIM_PLATFORM (see validate_sim_rules) or "any" (the package may be either
    kind of id; used for a gate override before the run's platform is known). sim_mode, for
    SIM_PLATFORM only, is "run" (a measured run) or "rehearse" (`ui-probe rehearse`, which may type)."""
    def need(cond, msg):
        if not cond:
            raise ProbeError(f"scenario: {msg}")

    number = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
    count = lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0
    unit = lambda v: number(v) and 0 <= v <= 1

    need(isinstance(scn, dict), "must be a JSON object")
    if scn.get("film") is True or platform == FILM_PLATFORM:
        need(platform in (FILM_PLATFORM, "any"), "it is a film's windows (\"film\": true), which no device runs; "
                                                 "`ui-probe film` reads a film by them")
        need(scn.get("film") is True, "a film's run holds a scenario not marked \"film\": true; gate it with its own")
        validate_film_scenario(scn, need)
        return
    unknown = sorted(k for k in scn if k not in TOP_KEYS and not _is_comment(k))
    need(not unknown, f"unknown keys {unknown}")
    need(isinstance(scn.get("name"), str) and scn["name"], "'name' is required")
    need(isinstance(scn.get("package"), str) and scn["package"], "'package' is required")
    if platform == SIM_PLATFORM:
        need(BUNDLE_RE.fullmatch(scn["package"]),
             "'package' must be a bundle identifier: dot-separated names of letters, digits and '-', such as "
             "com.example.app")
    elif platform == "any":
        need(PACKAGE_RE.fullmatch(scn["package"]) or BUNDLE_RE.fullmatch(scn["package"]),
             "'package' must be an application id or a bundle identifier, such as com.example.app")
    else:
        need(PACKAGE_RE.fullmatch(scn["package"]),
             "'package' must be an application id: dot-separated names of letters, digits and '_', such as com.example.app")
    if "launch_env" in scn:
        need(platform != ANDROID, "'launch_env' is refused on Android: an Android launch takes no environment")
        env = scn["launch_env"]
        need(isinstance(env, dict), "'launch_env' must be an object of names to string values")
        need(len(env) <= LAUNCH_ENV_MAX, f"'launch_env' holds {len(env)} pairs; at most {LAUNCH_ENV_MAX}")
        for name, value in env.items():
            need(LAUNCH_ENV_NAME_RE.fullmatch(name), f"'launch_env' name {name!r} must be capital letters, digits "
                                                     "and '_', starting with a letter")
            need(isinstance(value, str) and len(value) <= LAUNCH_ENV_VALUE_MAX,
                 f"'launch_env.{name}' must be a string of at most {LAUNCH_ENV_VALUE_MAX} characters")
            bad = next((ch for ch in value if ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F), None)
            need(bad is None, f"'launch_env.{name}' holds a control character {bad!r}")
    if "activity" in scn:
        need(isinstance(scn["activity"], str) and ACTIVITY_RE.fullmatch(scn["activity"]),
             "'activity' must be letters, digits, '_', '.' or '$'")
    if "launch_url" in scn:
        need("activity" not in scn, "'activity' and 'launch_url' cannot both be given: a start by link opens whatever "
                                    "activity the app gives the link")
        problem = link_problem(scn["launch_url"], "launch_url")
        need(problem is None, problem)
    if "until_quiet" in scn:
        need(isinstance(scn["until_quiet"], bool), "'until_quiet' must be true or false")
    check_watch_min(need, scn)
    steps = scn.get("steps")
    need(isinstance(steps, list) and steps, "'steps' must be a non-empty list")
    need(number(scn.get("launch_settle_ms", 0)) and scn.get("launch_settle_ms", 0) >= 0, "'launch_settle_ms' must be a number >= 0")
    def check_quiet(block, prefix, inherited):
        """The top-level 'quiet' block (prefix '') or a step's (inherited: the scenario's values,
        which fill in a key the step leaves out)."""
        need(isinstance(block, dict), f"{prefix}'quiet' must be an object with 'ms' and 'max_ms'")
        unknown_ = sorted(k for k in block if k not in QUIET_DEFAULTS)
        need(not unknown_, f"{prefix}unknown 'quiet' keys {unknown_}")
        for k in QUIET_DEFAULTS:
            if k in block:
                need(number(block[k]) and block[k] >= 100, f"{prefix}'quiet.{k}' must be a number of at least 100")
        eff = {**inherited, **block}
        need(eff["max_ms"] >= eff["ms"], f"{prefix}'quiet.max_ms' must be at least 'quiet.ms'"
             + ("" if not prefix else f" ({eff['max_ms']} and {eff['ms']}; a key the step leaves out comes from the scenario)"))

    quiet = scn.get("quiet", {})
    check_quiet(quiet, "", QUIET_DEFAULTS)
    scenario_quiet = {**QUIET_DEFAULTS, **quiet}
    if "launch_requests_max" in scn:
        need(count(scn["launch_requests_max"]), "'launch_requests_max' must be a whole number of at least 0")
    if "count_sections" in scn:
        sec = scn["count_sections"]
        need(isinstance(sec, list) and all(isinstance(n, str) and n for n in sec),
             "'count_sections' must be a list of non-empty strings")
    if "gate" in scn:
        g = scn["gate"]
        need(isinstance(g, dict), "'gate' must be an object")
        unknown = sorted(k for k in g if k not in GATE_DEFAULTS and k != "periodic_requests")
        need(not unknown, f"unknown 'gate' keys {unknown}; known: {', '.join(GATE_DEFAULTS)}, periodic_requests")
        if "rest_frames_max" in g:
            need(count(g["rest_frames_max"]), "'gate.rest_frames_max' must be an integer >= 0")
        if "rest_cpu_pct_max" in g:
            need(number(g["rest_cpu_pct_max"]) and g["rest_cpu_pct_max"] >= 0, "'gate.rest_cpu_pct_max' must be a number >= 0")
        if "rest_updates_max" in g:
            need(platform != ANDROID, f"'gate.rest_updates_max' applies to {SIM_ONLY}")
            need(count(g["rest_updates_max"]), "'gate.rest_updates_max' must be an integer >= 0")
        if "rest_requests_max" in g:
            need(count(g["rest_requests_max"]), "'gate.rest_requests_max' must be an integer >= 0")
        if "periodic_requests" in g:
            allowed = g["periodic_requests"]
            need(isinstance(allowed, dict) and allowed,
                 "'gate.periodic_requests' must be an object of requests as the log gives them (\"METHOD /path\") "
                 "to whole numbers of at least 1")
            for key, n in allowed.items():
                need(isinstance(key, str) and REQUEST_KEY_RE.fullmatch(key),
                     f"'gate.periodic_requests' key {key!r} must be a request as the log gives it: an upper-case "
                     "method, one space and a path that starts with '/', such as \"GET /api/v1/x/sync\"")
                need(isinstance(n, int) and not isinstance(n, bool) and n >= 1,
                     f"'gate.periodic_requests.{key}' must be a whole number of at least 1")
    if "count_signposts" in scn:
        need(platform != ANDROID, "'count_signposts' is refused on Android: it reads the iOS Simulator's log store "
                                  "('count_sections' counts trace sections there)")
        cs = scn["count_signposts"]
        need(isinstance(cs, dict) and set(cs) == {"subsystem", "names"},
             "'count_signposts' must be an object with 'subsystem' and 'names'")
        need(isinstance(cs["subsystem"], str) and SUBSYSTEM_RE.fullmatch(cs["subsystem"]),
             "'count_signposts.subsystem' must be letters, digits, '.', '_' or '-'")
        need(isinstance(cs["names"], list) and cs["names"] and len(cs["names"]) <= 50
             and all(isinstance(n, str) and n and len(n) <= 200 and unmatchable_char(n) is None for n in cs["names"])
             and len(set(cs["names"])) == len(cs["names"]),
             "'count_signposts.names' must be a list of 1 to 50 different names, each a string of 1 to 200 characters "
             "with no control character")
    seen = set()
    for n, step in enumerate(steps):
        where = f"step {n + 1}"
        need(isinstance(step, dict), f"{where} must be an object")
        sid = step.get("id")
        need(isinstance(sid, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", sid or ""),
             f"{where}: 'id' must be letters, digits, '-' or '_'")
        need(sid not in RESERVED_IDS, f"{where}: id '{sid}' is reserved")
        need(sid not in seen, f"{where}: id '{sid}' is used twice")
        seen.add(sid)
        do = step.get("do")
        need(do in STEP_KINDS, f"step '{sid}': 'do' must be one of {', '.join(STEP_KINDS)}")
        unknown = sorted(k for k in step if k not in COMMON_STEP_KEYS | STEP_KEYS[do] and not _is_comment(k))
        need(not unknown, f"step '{sid}': keys {unknown} do not apply to a '{do}' step")
        need(number(step.get("settle_ms", 0)) and step.get("settle_ms", 0) >= 0, f"step '{sid}': 'settle_ms' must be a number >= 0")
        if "until_quiet" in step:
            need(isinstance(step["until_quiet"], bool), f"step '{sid}': 'until_quiet' must be true or false")
        if "quiet" in step:
            check_quiet(step["quiet"], f"step '{sid}': ", scenario_quiet)
            # A window the step never waits for would be ignored in silence.
            need(step_until_quiet(scn, step),
                 f"step '{sid}': 'quiet' applies only to a step that waits for quiet ('until_quiet' true here or in the scenario)")
        if do == "rest":
            need(number(step.get("ms")) and step["ms"] > 0, f"step '{sid}': rest needs 'ms' > 0")
            for k in ("meminfo", "background"):
                if k in step:
                    need(isinstance(step[k], bool), f"step '{sid}': '{k}' must be true or false")
            check_watch(need, scn, step, step["ms"])
            if "allow" in step:
                allow = step["allow"]
                need(isinstance(allow, dict), f"step '{sid}': 'allow' must be an object")
                unknown = sorted(k for k in allow if k not in ALLOW_LIMITS + ("regions", "reason"))
                need(not unknown, f"step '{sid}': unknown 'allow' keys {unknown}; known: {', '.join(ALLOW_LIMITS)}, "
                                  "regions, reason")
                need(any(k in allow for k in ALLOW_LIMITS) or "regions" in allow,
                     f"step '{sid}': 'allow' needs at least one of {', '.join(ALLOW_LIMITS)} or 'regions'")
                # Each region carries its own reason; the step's reason covers its limits.
                if any(k in allow for k in ALLOW_LIMITS) or "reason" in allow:
                    need(isinstance(allow.get("reason"), str) and allow["reason"].strip(),
                         f"step '{sid}': 'allow.reason' is required: say why this screen is never still")
                if "regions" in allow:
                    check_regions(need, allow["regions"], f"step '{sid}'")
                for k in ("frames_max", "changed_frames_max", "updates_max", "requests_max"):
                    if k in allow:
                        need(count(allow[k]), f"step '{sid}': 'allow.{k}' must be an integer >= 0")
                if "updates_max" in allow:
                    need(platform != ANDROID, f"step '{sid}': 'allow.updates_max' applies to {SIM_ONLY}")
                if "unchanged_frames_max" in allow:
                    v = allow["unchanged_frames_max"]
                    need(isinstance(v, int) and not isinstance(v, bool) and v >= 1,
                         f"step '{sid}': 'allow.unchanged_frames_max' must be a whole number of at least 1")
                if "cpu_pct_max" in allow:
                    need(number(allow["cpu_pct_max"]) and allow["cpu_pct_max"] >= 0,
                         f"step '{sid}': 'allow.cpu_pct_max' must be a number >= 0")
        elif do == "tap":
            if "find" in step:
                need("x" not in step and "y" not in step,
                     f"step '{sid}': a tap takes either 'find' or 'x' and 'y', not both")
                find = step["find"]
                need(isinstance(find, dict), f"step '{sid}': 'find' must be an object")
                unknown = sorted(k for k in find if k not in FIND_KEYS)
                need(not unknown, f"step '{sid}': unknown 'find' keys {unknown}; known: {', '.join(sorted(FIND_KEYS))}")
                named = [k for k in FIND_ATTRS if k in find]
                need(len(named) == 1, f"step '{sid}': 'find' needs exactly one of 'text' or 'desc'")
                value = find[named[0]]
                need(isinstance(value, str) and value and len(value) <= TEXT_MAX and not ("\n" in value or "\r" in value),
                     f"step '{sid}': 'find.{named[0]}' must be a non-empty string of at most {TEXT_MAX} characters, on one line")
                if platform == SIM_PLATFORM:
                    bad = next((ch for ch in value if ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F
                                or 0xD800 <= ord(ch) <= 0xDFFF), None)
                    need(bad is None,
                         f"step '{sid}': 'find.{named[0]}' holds {bad!r} (U+{ord(bad or ' '):04X}), a control character "
                         "or a lone surrogate; on the iOS Simulator a lookup value must be printable text (it is "
                         "compared with an element's accessibility label or value)")
                bad = unmatchable_char(value) if platform != SIM_PLATFORM else None
                need(bad is None,
                     f"step '{sid}': 'find.{named[0]}' holds {bad!r} (U+{ord(bad or ' '):04X}), which the window-tree dump "
                     "never writes as it is (a tab, a control character, a surrogate, a character outside the Basic "
                     "Multilingual Plane or one XML does not allow), so it could never match")
                if "nth" in find:
                    need(isinstance(find["nth"], int) and not isinstance(find["nth"], bool) and find["nth"] >= 1,
                         f"step '{sid}': 'find.nth' must be an integer >= 1")
                if "timeout_ms" in find:
                    need(number(find["timeout_ms"]) and find["timeout_ms"] >= FIND_TIMEOUT_MIN_MS,
                         f"step '{sid}': 'find.timeout_ms' must be a number of at least {FIND_TIMEOUT_MIN_MS}")
                if "package" in find:
                    need(isinstance(find["package"], str) and PACKAGE_RE.fullmatch(find["package"]),
                         f"step '{sid}': 'find.package' must be an application id: dot-separated names of letters, "
                         "digits and '_', such as com.example.app")
                if "scroll" in find:
                    sc = find["scroll"]
                    need(isinstance(sc, dict), f"step '{sid}': 'find.scroll' must be an object")
                    unknown = sorted(k for k in sc if k not in SCROLL_KEYS)
                    need(not unknown, f"step '{sid}': unknown 'find.scroll' keys {unknown}; "
                                      f"known: {', '.join(sorted(SCROLL_KEYS))}")
                    for end in ("from", "to"):
                        pt = sc.get(end)
                        need(isinstance(pt, list) and len(pt) == 2 and unit(pt[0]) and unit(pt[1]),
                             f"step '{sid}': 'find.scroll' needs '{end}' as [x, y] between 0 and 1")
                    need(max(abs(sc["to"][0] - sc["from"][0]), abs(sc["to"][1] - sc["from"][1])) >= SCROLL_MIN_TRAVEL - 1e-9,
                         f"step '{sid}': 'find.scroll' must move at least {SCROLL_MIN_TRAVEL} of the screen on one "
                         "axis; a swipe that barely moves is a tap")
                    need(isinstance(sc.get("max"), int) and not isinstance(sc.get("max"), bool)
                         and 1 <= sc["max"] <= SCROLL_MAX,
                         f"step '{sid}': 'find.scroll.max' must be an integer from 1 to {SCROLL_MAX}")
                    if "ms" in sc:
                        need(number(sc["ms"]) and sc["ms"] >= 1, f"step '{sid}': 'find.scroll.ms' must be a number >= 1")
                    if "settle_ms" in sc:
                        need(number(sc["settle_ms"]) and sc["settle_ms"] >= 0,
                             f"step '{sid}': 'find.scroll.settle_ms' must be a number >= 0")
                if "find_settle_ms" in step:
                    need(number(step["find_settle_ms"]) and step["find_settle_ms"] >= 0,
                         f"step '{sid}': 'find_settle_ms' must be a number >= 0")
            else:
                need("find_settle_ms" not in step, f"step '{sid}': 'find_settle_ms' applies only to a tap with 'find'")
                need(unit(step.get("x")) and unit(step.get("y")),
                     f"step '{sid}': tap needs 'x' and 'y' between 0 and 1, or 'find'")
        elif do == "text":
            value = step.get("text")
            need(isinstance(value, str) and value, f"step '{sid}': text needs a non-empty string 'text'")
            need(len(value) <= TEXT_MAX, f"step '{sid}': 'text' is {len(value)} characters; at most {TEXT_MAX}")
            bad = next((ch for ch in value if ch not in TEXT_CHARS), None)
            need(bad is None, f"step '{sid}': 'text' may hold only letters, digits, space and @ . _ + -; "
                              f"{bad!r} is not allowed")
        elif do == "swipe":
            need(number(step.get("ms", 300)) and step.get("ms", 300) > 0, f"step '{sid}': swipe 'ms' must be a number > 0")
            for end in ("from", "to"):
                pt = step.get(end)
                need(isinstance(pt, list) and len(pt) == 2 and unit(pt[0]) and unit(pt[1]),
                     f"step '{sid}': swipe needs '{end}' as [x, y] between 0 and 1")
            need(step.get("judge", "motion") in ("motion", "state"), f"step '{sid}': 'judge' is 'motion' or 'state'")
        elif do == "key":
            need(isinstance(step.get("code"), int) and not isinstance(step.get("code"), bool), f"step '{sid}': key needs an integer 'code'")
        elif do == "url":
            problem = link_problem(step.get("url"), "url")
            need(problem is None, f"step '{sid}': {problem}")
        if "expect" in step:
            exp = step["expect"]
            need(isinstance(exp, dict), f"step '{sid}': 'expect' must be an object")
            unknown = set(exp) - EXPECT_KEYS
            need(not unknown, f"step '{sid}': unknown 'expect' keys {sorted(unknown)}")
            if "signature" in exp:
                need(isinstance(exp["signature"], str), f"step '{sid}': 'expect.signature' must be a string")
            for k in ("frames", "frames_max", "requests_max"):
                if k in exp:
                    need(count(exp[k]), f"step '{sid}': 'expect.{k}' must be an integer >= 0")
            if "sections_max" in exp:
                sm = exp["sections_max"]
                need(isinstance(sm, dict) and all(isinstance(k, str) and k and count(v) for k, v in sm.items()),
                     f"step '{sid}': 'expect.sections_max' must map section names to integers >= 0")
            if "signposts_max" in exp:
                need(platform != ANDROID, f"step '{sid}': 'expect.signposts_max' applies to {SIM_ONLY}")
                sp = exp["signposts_max"]
                names = (scn.get("count_signposts") or {}).get("names") or []
                need(isinstance(sp, dict) and sp and all(isinstance(k, str) and count(v) for k, v in sp.items()),
                     f"step '{sid}': 'expect.signposts_max' must map signpost names to integers >= 0")
                unlisted = sorted(k for k in sp if k not in names)
                need(not unlisted, f"step '{sid}': 'expect.signposts_max' names {unlisted}, which 'count_signposts.names' "
                                   "does not list")
    if platform == SIM_PLATFORM:
        # One recording per rest step on a simulator, by a recorder with no time limit: the Android
        # recorder's 180 s limit on the whole scenario does not apply.
        validate_sim_rules(scn, need, sim_mode)
        return
    fixed = scenario_duration_s(scn, worst_case=False)
    need(fixed <= RECORD_LIMIT_S - 10,
         f"fixed waits add up to about {fixed} s; the screen recorder stops at {RECORD_LIMIT_S} s. Split the scenario")


def check_watch_min(need, scn: dict) -> None:
    if "watch_min_ms" in scn:
        v = scn["watch_min_ms"]
        need(isinstance(v, (int, float)) and not isinstance(v, bool) and v >= WATCH_MIN_FLOOR_MS,
             f"'watch_min_ms' must be a number of milliseconds of at least {WATCH_MIN_FLOOR_MS}")


def watch_min_ms(scn: dict) -> float:
    return scn.get("watch_min_ms", WATCH_MIN_MS)


def check_watch(need, scn: dict, step: dict, ms: float) -> None:
    """A rest marked "watch": true lasts at least the scenario's watch_min_ms (default WATCH_MIN_MS)."""
    sid = step["id"]
    if "watch" not in step:
        return
    need(isinstance(step["watch"], bool), f"step '{sid}': 'watch' must be true or false")
    if step["watch"]:
        least = watch_min_ms(scn)
        need(ms >= least, f"step '{sid}': a watched rest lasts at least {least:g} ms ("
                          + ("the scenario's 'watch_min_ms'" if "watch_min_ms" in scn else
                             "a window of exactly one minute can miss a change made once a minute")
                          + f"); this one lasts {ms:g} ms")


def check_regions(need, regions, where: str) -> None:
    """allow.regions: a list of {"region": [x0, y0, x1, y1] (fractions of the app's area, x0 < x1,
    y0 < y1), "changed_frames_max": a whole number, "reason": why that part of the screen changes}."""
    unit = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1   # noqa: E731
    need(isinstance(regions, list) and 1 <= len(regions) <= REGIONS_MAX,
         f"{where}: 'allow.regions' must be a list of 1 to {REGIONS_MAX} regions")
    for n, reg in enumerate(regions, 1):
        at = f"{where}: 'allow.regions' item {n}"
        need(isinstance(reg, dict), f"{at} must be an object")
        unknown = sorted(k for k in reg if k not in REGION_KEYS and not _is_comment(k))
        need(not unknown, f"{at}: unknown keys {unknown}; known: {', '.join(REGION_KEYS)}")
        box = reg.get("region")
        need(isinstance(box, list) and len(box) == 4 and all(unit(v) for v in box) and box[0] < box[2] and box[1] < box[3],
             f"{at}: 'region' must be [x0, y0, x1, y1], fractions of the app's area between 0 and 1 with x0 < x1 "
             "and y0 < y1")
        v = reg.get("changed_frames_max")
        need(isinstance(v, int) and not isinstance(v, bool) and v >= 0,
             f"{at}: 'changed_frames_max' is required, a whole number of at least 0")
        need(isinstance(reg.get("reason"), str) and reg["reason"].strip(),
             f"{at}: 'reason' is required: say why this part of the screen changes")


def validate_film_scenario(scn: dict, need) -> None:
    """A film's windows, as `ui-probe film` stores them: {"film": true, "name", "steps": [{"id", "do":
    "rest", "start_s", "end_s", "watch"?, "allow"?}], "watch_min_ms"?}. Windows do not overlap."""
    number = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)     # noqa: E731
    unknown = sorted(k for k in scn if k not in FILM_TOP_KEYS and not _is_comment(k))
    need(not unknown, f"unknown keys {unknown} in a film's windows; known: {', '.join(sorted(FILM_TOP_KEYS))}")
    need(isinstance(scn.get("name"), str) and scn["name"], "'name' is required")
    check_watch_min(need, scn)
    steps = scn.get("steps")
    need(isinstance(steps, list) and steps, "a film needs at least one window")
    seen, spans = set(), []
    for n, step in enumerate(steps):
        need(isinstance(step, dict), f"window {n + 1} must be an object")
        sid = step.get("id")
        need(isinstance(sid, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", sid or ""),
             f"window {n + 1}: 'id' must be letters, digits, '-' or '_'")
        need(sid not in RESERVED_IDS, f"window {n + 1}: id '{sid}' is reserved")
        need(sid not in seen, f"window {n + 1}: id '{sid}' is used twice")
        seen.add(sid)
        unknown = sorted(k for k in step if k not in FILM_STEP_KEYS and not _is_comment(k))
        need(not unknown, f"window '{sid}': keys {unknown} do not apply to a film's window; known: "
                          f"{', '.join(sorted(FILM_STEP_KEYS - {'do'}))}")
        need(step.get("do") == "rest", f"window '{sid}': a film's window is a rest ('do': 'rest')")
        start, end = step.get("start_s"), step.get("end_s")
        need(number(start) and number(end) and 0 <= start < end,
             f"window '{sid}': 'start_s' and 'end_s' are seconds of the film, 0 <= start_s < end_s")
        spans.append((start, end, sid))
        check_watch(need, scn, step, (end - start) * 1000)
        if "allow" in step:
            allow = step["allow"]
            need(isinstance(allow, dict), f"window '{sid}': 'allow' must be an object")
            unknown = sorted(k for k in allow if k not in FILM_ALLOW_KEYS)
            need(not unknown, f"window '{sid}': 'allow' keys {unknown} do not apply to a film, which holds only the "
                              f"picture; known: {', '.join(FILM_ALLOW_KEYS)}")
            need("changed_frames_max" in allow or "regions" in allow,
                 f"window '{sid}': 'allow' needs 'changed_frames_max' or 'regions'")
            if "changed_frames_max" in allow:
                v = allow["changed_frames_max"]
                need(isinstance(v, int) and not isinstance(v, bool) and v >= 0,
                     f"window '{sid}': 'allow.changed_frames_max' must be an integer >= 0")
                need(isinstance(allow.get("reason"), str) and allow["reason"].strip(),
                     f"window '{sid}': 'allow.reason' is required: say why this screen is never still")
            if "regions" in allow:
                check_regions(need, allow["regions"], f"window '{sid}'")
    spans.sort()
    for (a0, a1, aid), (b0, b1, bid) in zip(spans, spans[1:]):
        need(b0 >= a1, f"windows '{aid}' and '{bid}' overlap ({a0:g} to {a1:g} s and {b0:g} to {b1:g} s)")


# What a scenario for the iOS Simulator may hold: there is no input from a command line, no
# window-tree dump, no platform frame record and no quiet detection. Input goes through the input
# agent (ios-agent/), by position in a measured run.
SIM_REST_MIN_MS = 1000
SIM_REFUSED_TOP = {
    "activity": "the iOS Simulator launches the app by its bundle identifier ('package'); 'activity' does not apply",
    "launch_url": "an app started by a link on the iOS Simulator cannot be given its launch environment; launch it "
                  "and open the link in a 'url' step right after the launch",
    "quiet": "there is no quiet detection on the iOS Simulator; 'quiet' would be ignored",
    "count_sections": "trace sections are not recorded on the iOS Simulator",
}
SIM_REFUSED_STEP = {
    "meminfo": "memory is not read on the iOS Simulator",
    "background": "which app is in front is not read on the iOS Simulator",
}


SIM_INPUT_KINDS = ("tap", "swipe")
SIM_SWIPE_MS_MAX = 60000
SIM_SETTLE_MS_MAX = 60000     # a wait the host makes between two agent commands; the agent has an idle limit
SIM_MODES = ("run", "rehearse")
# Why a measured run never reads the app or types (measured: either changes the app at rest for the
# rest of its process's life), and what to do instead.
SIM_NO_TYPING = ("a field that has had focus changes the app for the rest of its life, so a measured run never types: "
                 "put the typing in a scenario of its own and drive it with `ui-probe rehearse` (for example to sign "
                 "in before the measured scenarios)")


def validate_sim_rules(scn: dict, need, mode: str = "run") -> None:
    """The simulator's rules. mode "run" (a measured run): a tap may name its target with 'find', to
    be replayed from a rehearsal; a 'text' step is refused. mode "rehearse": the same, and a 'text'
    step is allowed. 'expect', 'allow' and rest lengths are checked alike in both modes, so that one
    file serves both commands; a rehearsal ignores them."""
    if mode not in SIM_MODES:
        raise ValueError(f"unknown simulator mode {mode!r}")
    for key, why in SIM_REFUSED_TOP.items():
        need(key not in scn, f"'{key}': {why}")
    need(not scn.get("until_quiet", False), "'until_quiet' must be false or absent: there is no quiet detection on the "
                                            "iOS Simulator; the launch waits 'launch_settle_ms', an input step its "
                                            "'settle_ms'")
    for step in scn["steps"]:
        sid, do = step["id"], step["do"]
        if do == "tap" and "find" in step:
            need("package" not in step["find"],
                 f"step '{sid}': 'find.package' is refused on the iOS Simulator: it names the package of an Android "
                 "window; a lookup on a simulator reads the app under test only")
            need("find_settle_ms" not in step,
                 f"step '{sid}': 'find_settle_ms' is refused on the iOS Simulator: a measured run looks nothing up "
                 "(the tap is replayed from the rehearsal), and the swipes it replays wait their 'find.scroll.settle_ms'")
            if "scroll" in step["find"]:
                need(find_scroll(step)["ms"] <= SIM_SWIPE_MS_MAX,
                     f"step '{sid}': 'find.scroll.ms' on the iOS Simulator is at most {SIM_SWIPE_MS_MAX} (what the "
                     "input agent accepts)")
                need(find_scroll(step)["settle_ms"] <= SIM_SETTLE_MS_MAX,
                     f"step '{sid}': 'find.scroll.settle_ms' on the iOS Simulator is at most {SIM_SETTLE_MS_MAX}: it is a "
                     "wait between two commands to the input agent, which ends itself after an idle limit")
        if do != "rest" and "settle_ms" in step:
            need(step["settle_ms"] <= SIM_SETTLE_MS_MAX,
                 f"step '{sid}': 'settle_ms' on the iOS Simulator is at most {SIM_SETTLE_MS_MAX}: it is a wait between "
                 "two commands to the input agent, which ends itself after an idle limit")
        if do == "text":
            need(mode == "rehearse", f"step '{sid}': a 'text' step is refused in a measured run on the iOS Simulator: "
                                     f"{SIM_NO_TYPING}")
        need(do != "key", f"step '{sid}': a 'key' step is refused on the iOS Simulator: a simulator has no keys to "
                          "press; tap the screen's own control or swipe from the edge")
        need(do == "rest" or do in SIM_INPUT_KINDS or do in ("text", "url"),
             f"step '{sid}': a '{do}' step is refused on the iOS Simulator")
        if do == "rest":
            for key, why in SIM_REFUSED_STEP.items():
                need(key not in step, f"step '{sid}': '{key}': {why}")
            unknown = sorted(set(step.get("expect") or {}) - {"signposts_max", "requests_max"})
            need(not unknown, f"step '{sid}': 'expect' on a rest on the iOS Simulator takes only 'signposts_max' and "
                              f"'requests_max' (frames at rest are gated by the limits and 'allow'); refused: {unknown}")
            need(step["ms"] >= SIM_REST_MIN_MS,
                 f"step '{sid}': a rest on the iOS Simulator lasts at least {SIM_REST_MIN_MS} ms: the CPU time is read "
                 "in steps of 0.01 s, too coarse for a shorter window")
        else:
            need(not step.get("until_quiet", False),
                 f"step '{sid}': 'until_quiet' must be false or absent: there is no quiet detection on the iOS "
                 "Simulator; the step waits its 'settle_ms'")
            if do == "swipe":
                need(step.get("ms", 300) <= SIM_SWIPE_MS_MAX,
                     f"step '{sid}': a swipe on the iOS Simulator lasts at most {SIM_SWIPE_MS_MAX} ms (what the input "
                     "agent accepts)")
            unknown = sorted(set(step.get("expect") or {}) - {"signature", "signposts_max", "requests_max"})
            need(not unknown, f"step '{sid}': 'expect' on a tap or a swipe on the iOS Simulator takes only 'signature', "
                              f"'signposts_max' and 'requests_max'; refused: {unknown}")


def sim_find_steps(scn: dict) -> list[dict]:
    """The taps that name their target with 'find'."""
    return [s for s in scn["steps"] if s["do"] == "tap" and "find" in s]


def sim_needs_agent(scn: dict) -> bool:
    """A tap or a swipe is the agent's; a link needs it too, to confirm the system's alert."""
    return any(s["do"] in SIM_INPUT_KINDS or s["do"] == "url" for s in scn["steps"])


def sim_has_url(scn: dict) -> bool:
    return any(s["do"] == "url" for s in scn["steps"])


def scenario_duration_s(scn: dict, worst_case: bool = True) -> int:
    """Seconds the device script needs. worst_case counts every wait-for-quiet at its limit."""
    def quiet_cost(step):
        q_ms, q_max = quiet_settings(scn, step)
        return (q_max + 1500 if worst_case else q_ms + 600) / 1000

    total = 1 + 1.5 + 4 + scn.get("launch_settle_ms", 6000) / 1000
    if step_until_quiet(scn, None):
        total += quiet_cost(None)
    for step in scn["steps"]:
        if step["do"] == "rest":
            total += step["ms"] / 1000
        elif step_until_quiet(scn, step):
            total += step.get("ms", 0) / 1000 + step.get("settle_ms", 300) / 1000 + quiet_cost(step)
        else:
            total += step.get("ms", 0) / 1000 + step.get("settle_ms", default_settle_ms(step["do"])) / 1000
        if step["do"] == "url":
            total += 2          # am start -W waits for the activity the link leads to
        if "find" in step:
            # The lookup: two reads when the element is there and still, reads until the timeout
            # (and up to two more) when it is not; then the settle time and the baseline reading. With
            # find.scroll the default timeout holds every swipe (find_timeout_ms), and a swipe begun
            # just before the timeout runs on past it. The expected case counts no swipe.
            sc = find_scroll(step)
            overrun = 0 if sc is None else (sc["ms"] + sc["settle_ms"]) / 1000
            lookup = (find_timeout_ms(scn, step) / 1000 + overrun + 2 * FIND_READ_S + 0.5 if worst_case
                      else 2 * FIND_READ_S + 0.5)
            total += lookup + step.get("find_settle_ms", FIND_SETTLE_MS) / 1000 + 0.6
        total += 0.6  # snapshot cost allowance
    return int(total + 6)


# --------------------------------------------------------------------------------------------
# device facts
# --------------------------------------------------------------------------------------------

def device_facts(serial: str, package: str) -> dict:
    def prop(name):
        return sh(serial, f"getprop {name}").strip()

    wm_size = sh(serial, "wm size")
    physical = re.search(r"Physical size: (\d+)x(\d+)", wm_size) or re.search(r"(\d+)x(\d+)", wm_size)
    override = re.search(r"Override size: (\d+)x(\d+)", wm_size)
    # Taps, swipes and the window tree all use the size the display presents: the override when a
    # smaller screen is emulated with `wm size`, else the physical size.
    size = override or physical
    if not size:
        raise ProbeError("could not read the display size (wm size)")
    density = re.search(r"(\d+)", sh(serial, "wm density"))
    memtotal = re.search(r"MemTotal:\s+(\d+)", sh(serial, "cat /proc/meminfo"))
    display = sh(serial, "dumpsys display")
    fps = re.search(r"renderFrameRate ([0-9.]+)", display) or re.search(r"fps=([0-9.]+)", display)
    pkg = sh(serial, f"dumpsys package {q(package)}")
    if f"Package [{package}]" not in pkg:
        raise ProbeError(f"package {package} is not installed on {serial}")
    version = re.search(r"versionName=(\S+)", pkg)
    code = re.search(r"versionCode=(\d+)", pkg)
    flags = re.search(r"flags=\[([^\]]*)\]", pkg)
    uid = re.search(r"userId=(\d+)", pkg)
    apk_path = sh(serial, f"pm path {q(package)}").strip().splitlines()
    apk_sha = ""
    if apk_path:
        apk_sha = sh(serial, f"sha256sum {q(apk_path[0].replace('package:', '').strip())}", check=False).split(" ")[0]
    dex = sh(serial, "dumpsys package dexopt", check=False)
    dex_block = dex.split(f"[{package}]", 1)[1][:400] if f"[{package}]" in dex else ""
    dex_status = re.search(r"\[status=([^\]]+)\]", dex_block)
    insets = sh(serial, "dumpsys window displays")
    status_bar = re.search(r"ROTATION_0=\{[^}]*configInsets=\[\d+,(\d+)\]\[\d+,(\d+)\]", insets) or \
        re.search(r"mStableInsets=Rect\(\d+, (\d+) - \d+, (\d+)\)", insets)
    tck = sh(serial, "getconf CLK_TCK", check=False).strip()
    thermal = re.search(r"Thermal Status: (\d+)", sh(serial, "dumpsys thermalservice", check=False))
    avd = None
    if serial.startswith("emulator"):
        lines = adb(serial, "emu", "avd", "name", check=False).splitlines()
        avd = (lines[0].strip() or None) if lines else None
    cpus = sh(serial, "nproc").strip()
    return {
        "serial": serial,
        "avd": avd,
        "model": prop("ro.product.model"), "sdk": prop("ro.build.version.sdk"),
        "release": prop("ro.build.version.release"), "build_type": prop("ro.build.type"),
        "fingerprint": prop("ro.build.fingerprint"), "abi": prop("ro.product.cpu.abi"),
        "low_ram": prop("ro.config.low_ram"),
        "width": int(size.group(1)), "height": int(size.group(2)),
        "physical_size": [int(physical.group(1)), int(physical.group(2))] if physical else None,
        "override_size": [int(override.group(1)), int(override.group(2))] if override else None,
        "density": int(density.group(1)) if density else None,
        "mem_total_kb": int(memtotal.group(1)) if memtotal else None,
        "cpus": int(cpus) if cpus.isdigit() else None,
        "clk_tck": int(tck) if tck.isdigit() else 100,
        "refresh_hz": float(fps.group(1)) if fps else None,
        "animator_duration_scale": sh(serial, "settings get global animator_duration_scale").strip(),
        "transition_animation_scale": sh(serial, "settings get global transition_animation_scale").strip(),
        "font_scale": sh(serial, "settings get system font_scale").strip(),
        "wifi_on": sh(serial, "settings get global wifi_on").strip(),
        "http_proxy": sh(serial, "settings get global http_proxy").strip(),
        "low_power": sh(serial, "settings get global low_power").strip(),
        "thermal_status": thermal.group(1) if thermal else None,
        "package": package, "uid": int(uid.group(1)) if uid else None, "version_name": version.group(1) if version else None,
        "version_code": code.group(1) if code else None,
        "package_flags": flags.group(1) if flags else None,
        "debuggable": bool(flags and "DEBUGGABLE" in flags.group(1)),
        "apk_sha256": apk_sha,
        "art_compile_status": dex_status.group(1) if dex_status else None,
        "stable_insets_top_bottom": [int(status_bar.group(1)), int(status_bar.group(2))] if status_bar else None,
    }


# --------------------------------------------------------------------------------------------
# device-side script
# --------------------------------------------------------------------------------------------

def build_device_script(scn: dict, width: int, height: int, video: bool, limit_s: int) -> str:
    pkg = scn["package"]
    q_ms, q_max = quiet_settings(scn)
    px = lambda v, full: str(int(v * full + 0.5))
    L = ["#!/system/bin/sh", f"D={DEV_DIR}", f"PKG={q(pkg)}", "SR=",
         "rm -f $D/marks.txt $D/gfx-*.txt $D/sf-*.txt $D/cpu-*.txt $D/pid-*.txt $D/mem-*.txt $D/fg-*.txt $D/screen.mp4 "
         "$D/am-start.txt $D/sr-exit.txt $D/sr-alive.txt $D/sr.pid $D/script.pid "
         f"$D/find-*.txt $D/ui-*.xml $D/ui.tmp $D/hits.tmp $D/cand.tmp $D/vis.tmp $D/display.txt $D/{LOOKUP_FAILED}",
         "echo $$ > $D/script.pid",
         "# However the script ends (its last line, an error, a signal, a lost adb session), leave the",
         "# device as it found it: compositor statistics off and the recorder stopped.",
         "cleanup() {",
         "  dumpsys SurfaceFlinger --timestats -disable >/dev/null 2>&1",
         "  if [ -n \"$SR\" ]; then kill -2 $SR 2>/dev/null; fi",
         "  rm -f $D/script.pid $D/sr.pid",
         "}",
         "trap cleanup EXIT",
         "trap 'cleanup; trap - EXIT; exit 129' HUP",
         "trap 'cleanup; trap - EXIT; exit 130' INT",
         "trap 'cleanup; trap - EXIT; exit 143' TERM",
         "mark() { read up rest < /proc/uptime; echo \"$1 $up\" >> $D/marks.txt; }",
         "# cpu <name> <pid>: an uptime stamp, then the process's CPU time (it includes threads that have",
         "# exited), then each live thread's.",
         "cpu() {",
         "  read up rest < /proc/uptime",
         "  { echo \"uptime $up\"",
         "    if [ -n \"$2\" ]; then echo process; cat /proc/$2/stat; echo threads; cat /proc/$2/task/*/stat; fi",
         "  } > $D/cpu-$1.txt 2>/dev/null",
         "}",
         "# snap <id> [mem]: the end of a step. CPU is read first and again last, so the dumps in between",
         "# (served partly inside the app) are charged to no step. The resumed activity says which app",
         "# was in front when the step ended.",
         "snap() {",
         "  PID=$(pidof \"$PKG\"); P=${PID%% *}",
         "  echo \"$PID\" > $D/pid-$1.txt",
         "  cpu $1 \"$P\"",
         f"  dumpsys activity activities 2>/dev/null | grep -m1 -E '{FOREGROUND_GREP}' > $D/fg-$1.txt",
         "  dumpsys gfxinfo \"$PKG\" framestats > $D/gfx-$1.txt 2>&1",
         "  dumpsys SurfaceFlinger --timestats -dump > $D/sf-$1.txt 2>&1",
         "  if [ \"$2\" = mem ]; then dumpsys meminfo \"$PKG\" > $D/mem-$1.txt 2>&1; fi",
         "  cpu $1.after \"$P\"",
         "}",
         "# base <id>: every counter a snapshot reads, taken after an element lookup and its settle time.",
         "# A lookup makes the app and the compositor work; the step's counts start here instead of at",
         "# the previous step's end, so that work is charged to no step. CPU is read last.",
         "base() {",
         "  PID=$(pidof \"$PKG\"); P=${PID%% *}",
         "  echo \"$PID\" > $D/pid-$1.base.txt",
         "  dumpsys gfxinfo \"$PKG\" framestats > $D/gfx-$1.base.txt 2>&1",
         "  dumpsys SurfaceFlinger --timestats -dump > $D/sf-$1.base.txt 2>&1",
         "  cpu $1.base \"$P\"",
         "}",
         "# find_el <id> <needle> <nth, 0 for a unique match> <timeout ms> <package needle> <scroll>: read the",
         "# window tree until the element is there, on the screen and still, then set FX FY to its centre.",
         "# One node per line after splitting at '>'; the needle and the node's own package attribute are",
         "# matched as fixed strings. A match counts only when its bounds are not empty and its centre lies",
         "# on the display (DW x DH), and is accepted only when two reads in a row give the same bounds. A",
         "# read that fails (the screen still animating), a wrong number of matches, a match off the screen",
         "# or bounds that moved is 'not yet': wait 0.5 s and read again, up to the timeout; a match first",
         "# seen at the timeout gets one more read to hold still. <scroll> is 'max x1 y1 x2 y2 ms settle_s'",
         "# (coordinates in ten-thousandths of the display, max 0 for none): while a read shows no",
         "# acceptable match and fewer than max swipes were made, swipe, wait settle_s and read again. The",
         "# last tree read is kept as ui-<id>.xml and the lookup's record as find-<id>.txt (on a timeout:",
         "# not_found, not_visible when matches were there but off the screen, or unstable when a match",
         "# moved). Returns 1 on a timeout, or at once when the display size is unknown.",
         "find_el() {",
         "  fid=$1; needle=$2; nth=$3; tmo=$4; pkn=$5",
         "  set -- $6",
         "  smax=$1; sx1=$2; sy1=$3; sx2=$4; sy2=$5; sms=$6; sst=$7",
         "  t0=$(now_cs); reads=0; swipes=0; n=unread; vn=0; b=; pb=; grace=",
         "  if [ -z \"$DW\" ] || [ -z \"$DH\" ]; then",
         "    { echo \"status no_display_size\"; echo \"reads 0\"; echo \"swipes 0\"; echo \"elapsed_ms 0\"; } > $D/find-$fid.txt",
         "    return 1",
         "  fi",
         "  while :; do",
         "    reads=$((reads + 1)); b=; vn=0; off=; ok=",
         "    rm -f $D/ui.tmp $D/vis.tmp",
         "    uiautomator dump $D/ui.tmp >/dev/null 2>&1",
         "    if [ -s $D/ui.tmp ]; then",
         "      ok=1",
         "      mv $D/ui.tmp $D/ui-$fid.xml",
         "      tr '>' '\\n' < $D/ui-$fid.xml | grep -F -- \"$needle\" > $D/cand.tmp",
         "      grep -F -- \"$pkn\" $D/cand.tmp > $D/hits.tmp",
         "      n=$(wc -l < $D/hits.tmp | tr -dc '0-9')",
         "      : > $D/vis.tmp",
         "      while IFS= read -r line; do",
         "        set -- $(printf '%s\\n' \"$line\" | sed -nE 's/.*bounds=\"\\[(-?[0-9]+),(-?[0-9]+)\\]\\[(-?[0-9]+),(-?[0-9]+)\\]\".*/\\1 \\2 \\3 \\4/p')",
         "        if [ $# -ne 4 ]; then continue; fi",
         "        cx=$(( ($1 + $3) / 2 )); cy=$(( ($2 + $4) / 2 ))",
         "        if [ $3 -gt $1 ] && [ $4 -gt $2 ] && [ $cx -ge 0 ] && [ $cx -lt $DW ] && [ $cy -ge 0 ] && [ $cy -lt $DH ]; then",
         "          vn=$((vn + 1)); printf '%s\\n' \"$line\" >> $D/vis.tmp",
         "        elif [ -z \"$off\" ]; then off=\"$1 $2 $3 $4\"; fi",
         "      done < $D/hits.tmp",
         "      k=$nth; if [ \"$k\" -eq 0 ]; then k=1; fi",
         "      if { [ \"$nth\" -eq 0 ] && [ \"$vn\" -eq 1 ]; } || { [ \"$nth\" -gt 0 ] && [ \"$vn\" -ge \"$nth\" ]; }; then",
         "        b=$(sed -n \"${k}p\" $D/vis.tmp | sed -nE 's/.*bounds=\"\\[(-?[0-9]+),(-?[0-9]+)\\]\\[(-?[0-9]+),(-?[0-9]+)\\]\".*/\\1 \\2 \\3 \\4/p')",
         "      fi",
         "      # matches off the screen count only when too few are on it (not when several are)",
         "      if [ -n \"$b\" ] || { [ \"$nth\" -eq 0 ] && [ \"$vn\" -gt 0 ]; }; then off=; fi",
         "    else",
         "      n=unread",
         "    fi",
         "    t=$(now_cs); ms=$(( (t - t0) * 10 ))",
         "    if [ -n \"$b\" ] && [ \"$b\" = \"$pb\" ]; then",
         "      pk=$(sed -n \"${k}p\" $D/vis.tmp | sed -nE 's/.* package=\"([^\"]*)\".*/\\1/p')",
         "      set -- $b",
         "      FX=$(( ($1 + $3) / 2 )); FY=$(( ($2 + $4) / 2 ))",
         "      { echo \"status found\"; echo \"matches $n\"; echo \"visible $vn\"; echo \"reads $reads\"; echo \"swipes $swipes\"",
         "        echo \"elapsed_ms $ms\"; echo \"bounds $1 $2 $3 $4\"; echo \"centre $FX $FY\"; echo \"package $pk\"",
         "        echo \"display $DW $DH\"; } > $D/find-$fid.txt",
         "      rm -f $D/hits.tmp $D/cand.tmp $D/vis.tmp",
         "      return 0",
         "    fi",
         "    if [ $ms -ge $tmo ]; then",
         "      if [ -n \"$b\" ] && [ -z \"$grace\" ]; then grace=1; pb=$b; continue; fi",
         "      st=not_found; if [ -n \"$b\" ]; then st=unstable; elif [ -n \"$off\" ]; then st=not_visible; fi",
         "      { echo \"status $st\"; echo \"matches $n\"; echo \"visible $vn\"; echo \"reads $reads\"; echo \"swipes $swipes\"",
         "        echo \"elapsed_ms $ms\"; echo \"display $DW $DH\"",
         "        if [ -n \"$b\" ]; then echo \"last_bounds $b\"; echo \"previous_bounds $pb\"; fi",
         "        if [ -n \"$off\" ]; then echo \"offscreen_bounds $off\"; fi",
         "        if [ -s $D/cand.tmp ]; then",
         "          echo \"other_packages $(grep -v -F -- \"$pkn\" $D/cand.tmp | sed -nE 's/.* package=\"([^\"]*)\".*/\\1/p' | tr '\\n' ' ')\"",
         "        fi; } > $D/find-$fid.txt",
         "      rm -f $D/hits.tmp $D/cand.tmp $D/ui.tmp $D/vis.tmp",
         "      return 1",
         "    fi",
         "    pb=$b",
         "    if [ -n \"$ok\" ] && [ -z \"$b\" ] && [ $swipes -lt $smax ]; then",
         "      input swipe $(( (DW * sx1 + 5000) / 10000 )) $(( (DH * sy1 + 5000) / 10000 )) $(( (DW * sx2 + 5000) / 10000 )) $(( (DH * sy2 + 5000) / 10000 )) $sms",
         "      swipes=$((swipes + 1)); pb=",
         "      sleep $sst",
         "      continue",
         "    fi",
         "    sleep 0.5",
         "  done",
         "}",
         "frames() { dumpsys gfxinfo \"$PKG\" 2>/dev/null | grep -m1 'Total frames rendered' | tr -dc '0-9'; }",
         "flips() { dumpsys SurfaceFlinger --timestats -dump -maxlayers 0 2>/dev/null | grep -m1 '^totalFrames' | tr -dc '0-9'; }",
         "now_cs() { read u r < /proc/uptime; echo \"${u%.*}${u#*.}\"; }",
         "# wait_quiet <id> <quiet ms> <max ms> <fallback s>: return once neither the app's frame count nor",
         "# the display's has changed for <quiet ms> of real time. The display counter also covers animations",
         "# the system draws for the app (window transitions) and apps without HWUI frame stats. On a",
         "# timeout a second mark names the count still changing: the app's, or only the display's.",
         "wait_quiet() {",
         "  fa=$(frames); fd=$(flips)",
         "  if [ -z \"$fa\" ] && [ -z \"$fd\" ]; then mark $1.quiet_unavailable; sleep $4; return; fi",
         "  t0=$(now_cs); ca=$t0; cdisp=$t0",
         "  while :; do",
         "    sleep 0.1; na=$(frames); nd=$(flips); t=$(now_cs)",
         "    if [ \"$na\" != \"$fa\" ]; then fa=$na; ca=$t; fi",
         "    if [ \"$nd\" != \"$fd\" ]; then fd=$nd; cdisp=$t; fi",
         "    last=$ca; if [ $cdisp -gt $last ]; then last=$cdisp; fi",
         "    if [ $(( (t - last) * 10 )) -ge $2 ]; then mark $1.quiet; return; fi",
         "    if [ $(( (t - t0) * 10 )) -ge $3 ]; then",
         "      if [ -z \"$fa\" ]; then mark $1.quiet_timeout_unattributed",
         "      elif [ $(( (t - ca) * 10 )) -lt $2 ]; then mark $1.quiet_timeout_app",
         "      else mark $1.quiet_timeout_display; fi",
         "      mark $1.quiet_timeout; return",
         "    fi",
         "  done",
         "}",
         "dumpsys SurfaceFlinger --timestats -enable -clear >/dev/null 2>&1",
         "am force-stop \"$PKG\"", "sleep 1", "mark T0"]
    if any(s["do"] == "url" for s in scn["steps"]):
        # what am printed for each link a 'url' step opened (only in a scenario that has one, so that
        # the script of every other scenario stays as it was)
        n = next(k for k, line in enumerate(L) if line.startswith("rm -f $D/marks.txt"))
        L[n] += " $D/url-*.txt"
    if any("find" in s for s in scn["steps"]):
        # The display a lookup checks a match against: the override size when one is set (a smaller
        # screen emulated with `wm size`), else the physical size. Read once, kept in display.txt.
        L[L.index("dumpsys SurfaceFlinger --timestats -enable -clear >/dev/null 2>&1"):0] = [
            "WMS=$(wm size 2>/dev/null)",
            "DS=$(printf '%s\\n' \"$WMS\" | sed -nE 's/^Override size: ([0-9]+)x([0-9]+).*/\\1 \\2/p' | head -n 1)",
            "if [ -z \"$DS\" ]; then DS=$(printf '%s\\n' \"$WMS\" | sed -nE 's/^Physical size: ([0-9]+)x([0-9]+).*/\\1 \\2/p' | head -n 1); fi",
            "DW=; DH=; if [ -n \"$DS\" ]; then DW=${DS% *}; DH=${DS#* }; fi",
            "echo \"$DS\" > $D/display.txt"]
    if video:
        L += [f"screenrecord --bit-rate 8000000 --time-limit {limit_s} $D/screen.mp4 &", "SR=$!",
              "echo $SR > $D/sr.pid", "sleep 1.5"]
    launch = scn.get("activity")
    L += ["mark launch.start"]
    if "launch_url" in scn:
        # a start by link: the same am line as a 'url' step, to the scenario's package only; parsed as a
        # launch (status, launch state, total time, an Error line fails it)
        L += [f"am start -W -a android.intent.action.VIEW -d {q(scn['launch_url'])} -p \"$PKG\" > $D/am-start.txt 2>&1"]
    elif launch:
        L += [f"am start -W -n {q(pkg + '/' + launch)} > $D/am-start.txt 2>&1"]
    else:
        L += ["monkey -p \"$PKG\" -c android.intent.category.LAUNCHER 1 > $D/am-start.txt 2>&1"]
    L += ["mark launch.end"]
    # Apps keep working after their first frame (deferred set-up, late content). The fixed wait is
    # a floor; waiting for quiet on top of it covers a slow start without shortening a fast one.
    L += [f"sleep {scn.get('launch_settle_ms', 6000) / 1000}"]
    if step_until_quiet(scn, None):
        L += [f"wait_quiet launch {q_ms} {q_max} 0"]
    L += ["snap launch"]
    for step in scn["steps"]:
        sid, do = step["id"], step["do"]
        if "find" in step:
            # The lookup comes before the step's start mark, so it lies outside every step's window
            # by time; the baseline after it keeps it out of the step's counts as well.
            nth = step["find"].get("nth", 0)
            pkn = q(' package="' + find_package(scn, step) + '"')
            sc = find_scroll(step)
            if sc is None:
                scroll = "'0 0 0 0 0 0 0'"
            else:
                tenk = lambda v: str(int(round(v * 10000)))
                scroll = q(" ".join([str(sc["max"]), *(tenk(v) for v in sc["from"] + sc["to"]), str(int(sc["ms"])),
                                     str(sc["settle_ms"] / 1000)]))
            L += [f"mark {sid}.find_start",
                  f"if ! find_el {sid} {q(find_needle(step['find']))} {nth} {find_timeout_ms(scn, step)} {pkn} {scroll}; then "
                  f"mark {sid}.find_failed; echo {sid} > $D/{LOOKUP_FAILED}; exit 3; fi",
                  f"mark {sid}.found",
                  f"sleep {step.get('find_settle_ms', FIND_SETTLE_MS) / 1000}",
                  f"base {sid}"]
        L.append(f"mark {sid}.start")
        if do == "rest":
            L.append(f"sleep {step['ms'] / 1000}")
        else:
            if do == "tap" and "find" in step:
                L.append("input tap $FX $FY")
            elif do == "tap":
                L.append(f"input tap {px(step['x'], width)} {px(step['y'], height)}")
            elif do == "swipe":
                (x1, y1), (x2, y2) = step["from"], step["to"]
                L.append(f"input swipe {px(x1, width)} {px(y1, height)} {px(x2, width)} {px(y2, height)} {step.get('ms', 300)}")
            elif do == "key":
                L.append(f"input keyevent {step['code']}")
            elif do == "text":
                # `input text` spells a space as %s; the validated text holds no '%' of its own.
                L.append(f"input text {q(step['text'].replace(' ', '%s'))}")
            elif do == "url":
                # The link goes to the app under test only (-p), so a link another app also claims never
                # opens a chooser. -W waits until the activity reports; am's output and exit status are kept
                # for the gate (a link nothing handled is an input that failed).
                L.append(f"am start -W -a android.intent.action.VIEW -d {q(step['url'])} -p \"$PKG\" "
                         f"> $D/url-{sid}.txt 2>&1; echo \"exit $?\" >> $D/url-{sid}.txt")
            else:
                raise ProbeError(f"unknown step kind {do}")
            L.append(f"mark {sid}.injected")
            fixed = step.get("settle_ms", default_settle_ms(do)) / 1000
            if step_until_quiet(scn, step):
                # An explicit settle_ms is a floor before polling; the fallback is the fixed wait.
                floor = step["settle_ms"] / 1000 if "settle_ms" in step else 0.3
                s_ms, s_max = quiet_settings(scn, step)
                L += [f"sleep {floor}", f"wait_quiet {sid} {s_ms} {s_max} {round(max(0.0, fixed - floor), 3)}"]
            else:
                L.append(f"sleep {fixed}")
        L.append(f"mark {sid}.end")
        L.append(f"snap {sid} mem" if do == "rest" and step.get("meminfo", False) else f"snap {sid}")
    L.append("mark END")
    L.append("dumpsys SurfaceFlinger --timestats -disable >/dev/null 2>&1")
    if video:
        # Was the recorder still running when the scenario ended? One that stopped by itself exits
        # with status 0, so the exit status alone cannot tell.
        L += ["if kill -0 $SR 2>/dev/null; then echo alive > $D/sr-alive.txt; else echo gone > $D/sr-alive.txt; fi",
              "kill -2 $SR 2>/dev/null", "wait $SR", "echo $? > $D/sr-exit.txt", "SR="]
    L.append("echo done > $D/done.txt")
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------------------------
# parsers
# --------------------------------------------------------------------------------------------

GFX_TOTALS = {
    "total_frames": r"Total frames rendered: (\d+)",
    "janky_frames": r"Janky frames: (\d+)",
    "janky_legacy": r"Janky frames \(legacy\): (\d+)",
    "missed_vsync": r"Number Missed Vsync: (\d+)",
    "high_input_latency": r"Number High input latency: (\d+)",
    "slow_ui_thread": r"Number Slow UI thread: (\d+)",
    "slow_bitmap_uploads": r"Number Slow bitmap uploads: (\d+)",
    "slow_issue_draw": r"Number Slow issue draw commands: (\d+)",
    "frame_deadline_missed": r"Number Frame deadline missed: (\d+)",
}


def parse_gfx(text: str) -> dict:
    out = {"present": "Graphics info for pid" in text}
    for key, pat in GFX_TOTALS.items():
        m = re.search(pat, text)
        out[key] = int(m.group(1)) if m else None
    rows = []
    header = None
    in_block = False
    for line in text.splitlines():
        if line.startswith("---PROFILEDATA---"):
            in_block = not in_block
            header = None
            continue
        if not in_block:
            continue
        cells = [c for c in line.strip().split(",") if c != ""]
        if header is None:
            header = cells
            continue
        if len(cells) >= len(header) and cells[0].lstrip("-").isdigit():
            rows.append({h: int(c) for h, c in zip(header, cells)})
    out["rows"] = rows
    return out


def parse_timestats(text: str) -> dict:
    """`dumpsys SurfaceFlinger --timestats -dump`: the display-wide frame total and, per layer, the
    frames the compositor took from it. Returns {'total': n or None, 'layers': {(uid, name): n}}.

    Android 12 and newer start each layer's record with a `uid = ` line; Android 11 and older print
    no uid, and a record starts at its `layerName = ` line: such a layer's uid is None. One layer can
    appear in several records (the compositor keeps one per display refresh rate and render rate,
    among other keys); its frames are the sum over its records."""
    total = None
    layers: dict = {}
    blocks: list[dict] = []
    current = None
    for line in text.splitlines():
        if line.startswith("uid = "):
            uid = re.match(r"uid = (\d+)", line)
            current = {"uid": int(uid.group(1)) if uid else None, "uid_line": True}
            blocks.append(current)
        elif line.startswith("layerName = "):
            if current is None or "name" in current:
                current = {"uid": None, "uid_line": False}       # a record without a uid line
                blocks.append(current)
            current["name"] = line[len("layerName = "):].strip()
        elif line.startswith("totalFrames = "):
            m = re.match(r"totalFrames = (\d+)", line)
            if current is None:
                if total is None and m:
                    total = int(m.group(1))
            elif "frames" not in current and m:
                current["frames"] = int(m.group(1))
    for b in blocks:
        # a uid line that did not parse leaves the record out, as before
        if b.get("name") and b["name"] != "none" and "frames" in b and (b["uid"] is not None or not b["uid_line"]):
            key = (b["uid"], b["name"])
            layers[key] = layers.get(key, 0) + b["frames"]
    return {"total": total, "layers": layers}


def app_layer_frames(stats: dict, package: str, uid: int | None) -> dict:
    """The layers that belong to the app under test: owned by its uid and, when that uid is shared
    with the system or is not known, also named after its package. A layer without a uid (Android 11
    and older print none) is the app's only when its name starts with "<package>/" or with
    "SurfaceView - <package>/": a name that merely holds the package is another app's."""
    out = {}
    for (layer_uid, name), frames in stats["layers"].items():
        if layer_uid is None:
            if not (name.startswith(f"{package}/") or name.startswith(f"SurfaceView - {package}/")):
                continue
        else:
            if uid is not None and layer_uid != uid:
                continue
            if (uid is None or uid < 10000) and package not in name:
                continue
        out[name] = frames
    return out


def parse_thread_cpu(text: str) -> dict:
    """/proc/<pid>/task/<tid>/stat lines -> {tid: (comm, utime+stime ticks)}."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"(\d+) \((.*)\) (.*)", line)
        if not m:
            continue
        rest = m.group(3).split()
        if len(rest) < 13:
            continue
        out[m.group(1)] = (m.group(2), int(rest[11]) + int(rest[12]))
    return out


def parse_cpu(text: str) -> dict:
    """One CPU reading written by the device script's cpu(): {'uptime': seconds or None,
    'process': utime+stime ticks of the whole process or None, 'threads': {tid: (comm, ticks)}}.

    The process line counts threads that have exited since the process started; the thread lines
    are the breakdown of the threads still alive."""
    uptime, section = None, None
    proc_lines, thread_lines = [], []
    for line in text.splitlines():
        if line.startswith("uptime "):
            try:
                uptime = float(line.split()[1])
            except (IndexError, ValueError):
                uptime = None
        elif line in ("process", "threads"):
            section = line
        elif section == "process":
            proc_lines.append(line)
        elif section == "threads":
            thread_lines.append(line)
    proc = parse_thread_cpu("\n".join(proc_lines))
    return {"uptime": uptime, "process": next(iter(proc.values()))[1] if len(proc) == 1 else None,
            "threads": parse_thread_cpu("\n".join(thread_lines))}


def single_pid(text: str | None) -> int | None:
    """The process id in a pid file, or None when it holds none or more than one."""
    parts = (text or "").split()
    return int(parts[0]) if len(parts) == 1 and parts[0].isdigit() else None


def parse_meminfo(text: str) -> dict:
    out = {}
    m = re.search(r"TOTAL PSS:\s+(\d+)", text) or re.search(r"TOTAL\s+(\d+)", text)
    out["total_pss_kb"] = int(m.group(1)) if m else None
    for key, pat in {"java_heap_kb": r"Java Heap:\s+(\d+)", "native_heap_kb": r"Native Heap:\s+(\d+)",
                     "graphics_kb": r"Graphics:\s+(\d+)", "code_kb": r"Code:\s+(\d+)",
                     "views": r"Views:\s+(\d+)", "view_root_impl": r"ViewRootImpl:\s+(\d+)",
                     "activities": r"Activities:\s+(\d+)"}.items():
        mm = re.search(pat, text)
        out[key] = int(mm.group(1)) if mm else None
    return out


def parse_marks(text: str) -> dict:
    marks = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                marks[parts[0]] = float(parts[1])
            except ValueError as exc:
                raise ProbeError(f"marks.txt holds a line that is not a mark: {line!r}") from exc
    return marks


def parse_launch_error(am: str | None) -> str | None:
    """The launch command's error line, if it printed one."""
    if am is None:
        return "the launch command left no output"
    for line in am.splitlines():
        s = line.strip()
        if s.startswith("Error") or "does not exist" in s or "Exception" in s or "monkey aborted" in s:
            return s
    return None


def parse_url_open(text: str | None) -> dict:
    """What `am start -W -a android.intent.action.VIEW -d <url> -p <package>` printed for a 'url' step, with
    the exit status the device script appended: {'opened', 'exit', 'activity', 'error'}. The link was
    opened only if am exited 0, printed no `Error` line and named the activity it reached."""
    if text is None:
        return {"opened": False, "exit": None, "activity": None,
                "error": "the run holds no record of the link being opened (url-<step>.txt)"}
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    exit_line = next((line for line in reversed(lines) if re.fullmatch(r"exit -?\d+", line)), None)
    code = int(exit_line.split()[1]) if exit_line else None
    said = [line for line in lines if line is not exit_line and not re.fullmatch(r"exit -?\d+", line)]
    error = next((line for line in said if line.startswith("Error")), None)
    activity = next((line.split(":", 1)[1].strip() for line in said if line.startswith("Activity:")), None)
    if code is None:
        why = "am's exit status was not recorded"
    elif error:
        why = error
    elif code != 0:
        why = f"am exited with status {code}: {' / '.join(said[-3:]) or 'it printed nothing'}"
    elif not activity:
        why = f"am named no activity: {' / '.join(said[-3:]) or 'it printed nothing'}"
    else:
        why = None
    return {"opened": why is None, "exit": code, "activity": activity, "error": why}


def parse_foreground(text: str) -> str | None:
    """The component (package/activity) of the resumed activity from the line the device script
    saved, or None when the line names none (no activity resumed, or a platform that prints it
    differently)."""
    m = re.search(r"ActivityRecord\{\S+ \S+ ([^\s/}]+/[^\s}]+)", text or "")
    return m.group(1) if m else None


def pct(values, q):
    if not values:
        return None
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(q * (len(s) - 1) + 0.5)))   # nearest rank, halves up
    return s[idx]


def frame_count(s: dict) -> tuple:
    """The largest available count of a step's frames and the source that decided it. An app can
    draw through more than one surface (its views, plus a surface of its own), so every available
    count is consulted."""
    counts = {name: s.get(key) for name, key in FRAME_COUNTS if s.get(key) is not None}
    if not counts:
        return None, None
    source = max(counts, key=counts.get)
    return counts[source], source


# --------------------------------------------------------------------------------------------
# one run
# --------------------------------------------------------------------------------------------

def start_tracer(serial: str, config: str, trace_dev: str):
    """Start the on-device trace through adb, feeding it the config on stdin."""
    try:
        tracer = subprocess.Popen(["adb", "-s", serial, "shell", f"perfetto -c - --txt -o {trace_dev}"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as exc:
        raise ProbeError(f"cannot run adb: {exc.strerror or exc}") from exc
    tracer.stdin.write(config.encode())
    tracer.stdin.close()
    # communicate() flushes stdin while it is attached, and a closed pipe makes that raise:
    # detach it, since nothing more will be written.
    tracer.stdin = None
    return tracer


def stop_tracer(serial: str, tracer) -> str:
    """Stop the on-device trace and the local adb process that runs it; return what it printed."""
    sh(serial, "pkill -INT -x perfetto", check=False, timeout=30)
    try:
        output, _ = tracer.communicate(timeout=60)
    except subprocess.TimeoutExpired as exc:
        tracer.kill()
        tracer.communicate()
        raise ProbeError("the on-device trace did not stop; the trace file may be incomplete") from exc
    return (output or b"").decode(errors="replace")[-2000:]


def device_cleanup(serial: str) -> None:
    """Best effort after a failed run: stop the device script and the recorder this tool started
    (checked by command line, so a stale pid file cannot kill anything else) and switch the
    compositor's statistics off. Never raises."""
    cmd = (f"p=$(cat {DEV_DIR}/script.pid 2>/dev/null); "
           f"if [ -n \"$p\" ] && grep -q run.sh /proc/$p/cmdline 2>/dev/null; then kill $p; fi; "
           f"p=$(cat {DEV_DIR}/sr.pid 2>/dev/null); "
           f"if [ -n \"$p\" ] && grep -q screenrecord /proc/$p/cmdline 2>/dev/null; then kill -2 $p; fi; "
           "dumpsys SurfaceFlinger --timestats -disable >/dev/null 2>&1; true")
    try:
        sh(serial, cmd, check=False, timeout=30)
    except ProbeError as exc:
        print(f"ui-probe: clean-up on the device failed ({exc}); compositor statistics may still be on", file=sys.stderr)


def pull_raw(serial: str, out: Path, listing: list[str]) -> Path:
    """Copy what the device script wrote into <out>/raw."""
    raw = out / "raw"
    raw.mkdir(exist_ok=True)
    for name in listing:
        if name == "run.sh":
            continue
        adb(serial, "pull", f"{DEV_DIR}/{name}", str(raw / name))
    return raw


def parse_find(text: str | None) -> dict | None:
    """The record find_el() writes: status (found, not_found, not_visible, unstable or
    no_display_size), matches in the wanted package and how many of them were on the screen (a
    number, or None when the last read of the window tree failed), reads, swipes, elapsed_ms and
    the display size the matches were checked against; when found, bounds and centre in pixels and
    the matched node's package; on a timeout, the last two bounds of a match that moved, the bounds
    of a match that was off the screen and the packages of matching elements in other packages."""
    if text is None:
        return None
    out = {}
    integer = re.compile(r"-?\d+")
    for line in text.splitlines():
        key, _, rest = line.partition(" ")
        vals = rest.split()
        if key in ("status", "package"):
            out[key] = rest.strip() or None
        elif key in ("matches", "visible", "reads", "swipes", "elapsed_ms"):
            out[key] = int(vals[0]) if vals and vals[0].isdigit() else None
        elif key in ("bounds", "centre", "last_bounds", "previous_bounds", "offscreen_bounds", "display"):
            out[key] = [int(v) for v in vals] if vals and all(integer.fullmatch(v) for v in vals) else None
        elif key == "other_packages":
            out[key] = list(dict.fromkeys(vals))
    return out


class LookupFailed(ProbeError):
    """The device script stopped because an element never appeared (or never held still)."""

    def __init__(self, message: str, step: str | None):
        super().__init__(message)
        self.step = step


def describe_find(find: dict) -> str:
    key = "text" if "text" in find else "desc"
    what = f"the element with {'text' if key == 'text' else 'content description'} {json.dumps(find[key])}"
    return what + (f" (match {find['nth']} in document order)" if "nth" in find else "")


def lookup_failure(serial: str, scn: dict, out: Path, check: bool = True) -> LookupFailed | None:
    """If the device script stopped because an element never appeared (or never held still), pull
    its files and return the error to raise: it names the step, what was looked for, the matches at
    the last read and where the last tree read is saved. None if it did not stop for that. With
    check=False (the script already failed), a device that cannot be asked returns None so that the
    original failure is reported."""
    try:
        listing = sh(serial, f"ls {DEV_DIR}").split()
        if LOOKUP_FAILED not in listing:
            return None
        raw = pull_raw(serial, out, listing)
    except ProbeError:
        if check:
            raise
        return None
    sid = (_read_raw(raw, LOOKUP_FAILED) or "").strip()
    step = next((s for s in scn["steps"] if s["id"] == sid and "find" in s), None)
    if step is None:
        return LookupFailed(f"the device script stopped at an element lookup of an unknown step {sid!r}; "
                            f"its files are in {raw}", None)
    rec = parse_find(_read_raw(raw, f"find-{sid}.txt")) or {}
    timeout = find_timeout_ms(scn, step)
    pkg = find_package(scn, step)
    want = "exactly one" if "nth" not in step["find"] else f"at least {step['find']['nth']}"
    status = rec.get("status")
    scroll = find_scroll(step)
    swipes = rec.get("swipes")
    if status == "no_display_size":
        return LookupFailed(f"step '{sid}': the display size could not be read on the device (`wm size`), so whether "
                            f"{describe_find(step['find'])} is on the screen cannot be told; the lookup did not start. "
                            "The device script stopped there and restored the device; no capture was saved", sid)
    if status == "unstable":
        moved = ""
        if rec.get("previous_bounds") and rec.get("last_bounds"):
            moved = f" (bounds {rec['previous_bounds']} then {rec['last_bounds']})"
        seen = f"a match was there but did not hold still: two reads in a row never gave the same bounds{moved}"
    elif status == "not_visible":
        display = rec.get("display")
        on = f"a {display[0]} x {display[1]} display" if display and len(display) == 2 else "the display"
        seen = (f"{rec.get('matches')} matched in {pkg} at the last read, {rec.get('visible')} of them on the screen "
                f"({want} needed): the element is in the window tree but off the screen (bounds "
                f"{rec.get('offscreen_bounds')} on {on})")
        seen += ("; add 'find.scroll' to swipe it into view" if scroll is None else
                 f"; {swipes if swipes is not None else 'an unknown number of'} swipes did not bring it onto the screen")
    elif rec.get("matches") is not None:
        seen = f"{rec['matches']} matched in {pkg} at the last read ({want} needed)"
        if rec.get("visible") is not None and rec["visible"] != rec["matches"]:
            seen += f", {rec['visible']} of them on the screen"
    else:
        seen = "the last read of the window tree failed"
    others = [p for p in rec.get("other_packages") or [] if p != pkg]
    if others:
        seen += f"; matching elements in other packages: {', '.join(others)}"
    tree = raw / f"ui-{sid}.xml"
    where = f"the last tree read is saved in {tree}" if tree.exists() else "no read of the window tree succeeded, so no tree was saved"
    reads = rec.get("reads")
    verb = {"unstable": "did not hold still", "not_visible": "was not on the screen"}.get(status, "did not appear")
    swiped = "" if scroll is None else f", {swipes if swipes is not None else 'an unknown number of'} swipes"
    return LookupFailed(f"step '{sid}': {describe_find(step['find'])} {verb} within {timeout} ms"
                        f" ({reads if reads is not None else 'an unknown number of'} reads{swiped}): {seen}; {where}."
                        " The device script stopped there and restored the device; no capture was saved", sid)


# What a run folder holds once a run has used it, completely or not: run refuses such a folder.
RUN_MARKERS = ("capture.json", "run.json", FAILED_RUN, "device-script.sh", "raw")


def write_failed(out: Path, exc: BaseException, scenario: str | None = None, extra: dict | None = None) -> None:
    """Leave failed.json in a run folder whose run did not complete, so that gate, summary and
    compare see the run instead of passing over it. Never raises: the original failure is reported."""
    if isinstance(exc, ProbeError):
        error = str(exc)
    elif isinstance(exc, Terminated):
        error = f"terminated by {exc.signame}"
    elif isinstance(exc, KeyboardInterrupt):
        error = "interrupted"
    else:
        error = f"{type(exc).__name__}: {exc}"
    try:
        (out / FAILED_RUN).write_text(json.dumps({
            "tool": {"name": "ui-probe", "version": VERSION}, "scenario": scenario, "error": error,
            "step": getattr(exc, "step", None), "time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            **(extra or {}),
        }, indent=1))
    except OSError as err:
        print(f"ui-probe: could not write {out / FAILED_RUN}: {err.strerror or err}", file=sys.stderr)


def refuse_used(out: Path) -> None:
    held = [name for name in RUN_MARKERS if (out / name).exists()]
    if held:
        raise ProbeError(f"{out} already holds a run or what is left of one ({', '.join(held)}); choose a fresh "
                         "directory (raw evidence is never overwritten)")


def run_once(serial: str, scn: dict, out: Path, trace: bool, video: bool, sections: bool = False,
             requests: Path | None = None) -> dict:
    """One measured run on Android. requests: a front's log (checked by check_request_log) whose new
    lines are kept and counted per step."""
    validate_scenario(scn)
    refuse_used(out)
    refuse_rehearsal_folder(out)
    out.mkdir(parents=True, exist_ok=True)
    try:
        return _run_once(serial, scn, out, trace, video, sections, requests)
    except BaseException as exc:
        write_failed(out, exc, scn.get("name"))
        raise


def _run_once(serial: str, scn: dict, out: Path, trace: bool, video: bool, sections: bool,
              requests: Path | None = None) -> dict:
    req_start = log_position(requests) if requests else None
    facts = device_facts(serial, scn["package"])
    total_s = scenario_duration_s(scn)
    limit_s = min(RECORD_LIMIT_S, total_s + 60)
    script = build_device_script(scn, facts["width"], facts["height"], video, limit_s)
    (out / "device-script.sh").write_text(script)
    sh(serial, f"mkdir -p {DEV_DIR} && rm -f {DEV_DIR}/done.txt")
    adb(serial, "push", str(out / "device-script.sh"), f"{DEV_DIR}/run.sh")

    load_before = host_load()
    tracer = None
    tracer_output = None
    trace_dev = f"/data/misc/perfetto-traces/uiprobe-{int(time.time())}.pftrace"
    finished = False
    try:
        if trace:
            cfg = TRACE_CONFIG % ((total_s + 4) * 1000)
            if sections:
                cfg += SECTIONS_CONFIG % scn["package"]
            tracer = start_tracer(serial, cfg, trace_dev)
            time.sleep(1.5)
        # The device's uptime against this machine's clock, read from outside the device script: it places
        # the request log's lines in the step windows.
        clock_before = uptime_probes(serial) if requests else None
        started = time.time()
        # A stalled device (an ANR being processed, for instance) slows every shell call; allow for it.
        looks_up = any("find" in s for s in scn["steps"])
        try:
            sh(serial, f"sh {DEV_DIR}/run.sh", timeout=total_s + 420)
        except ProbeError as exc:
            # An element that never appeared ends the script on purpose (it restores the device on
            # its way out): say so, with the evidence, instead of reporting a bare exit status.
            failure = lookup_failure(serial, scn, out, check=False) if looks_up else None
            if failure:
                raise failure from exc
            raise
        # An adb that does not pass the script's exit status on reports success: look anyway.
        failure = lookup_failure(serial, scn, out) if looks_up else None
        if failure:
            raise failure
        script_end = time.time()
        wall = script_end - started
        clock_after = uptime_probes(serial) if requests else None
        load_after = host_load()
        if tracer is not None:
            # The trace runs for the scenario's worst case; stop it once the scenario is done.
            running, tracer = tracer, None
            tracer_output = stop_tracer(serial, running)
            adb(serial, "pull", trace_dev, str(out / "trace.pftrace"))
            sh(serial, f"rm -f {trace_dev}", check=False)

        listing = sh(serial, f"ls {DEV_DIR}").split()
        if "done.txt" not in listing:
            raise ProbeError("the device script did not finish (no done marker); raw files were left on the device")
        raw = pull_raw(serial, out, listing)
        finished = True
        if requests:
            capture_requests_android(raw, requests, req_start, (clock_before, clock_after), (started, script_end), scn)
    finally:
        if tracer is not None:
            try:
                stop_tracer(serial, tracer)
            except Exception:     # the original failure is the one to report
                if tracer.poll() is None:
                    tracer.kill()
                    tracer.communicate()
        if not finished:
            device_cleanup(serial)
    (out / "capture.json").write_text(json.dumps({
        "tool": {"name": "ui-probe", "version": VERSION},
        "scenario": scn, "device": facts, "trace": trace, "video": video, "sections": sections,
        "record_limit_s": limit_s, "tracer_output": tracer_output, "requests_log": str(requests) if requests else None,
        "host": {"load1_before": round(load_before, 2), "load1_after": round(load_after, 2), "wall_s": round(wall, 1)},
    }, indent=1))
    try:
        return analyze_run(out)
    except ProbeError as exc:
        raise ProbeError(f"{out}: the capture was saved but its analysis failed: {exc}") from exc
    except Exception:
        print(f"ui-probe: {out}: the capture was saved but its analysis failed unexpectedly", file=sys.stderr)
        raise


# --------------------------------------------------------------------------------------------
# iOS Simulator, driven from the host
# --------------------------------------------------------------------------------------------
#
# The app is a process on the host, so its CPU time is read with `ps`; the display is recorded with
# `xcrun simctl io <udid> recordVideo`, which keeps a frame only when the display changes. There is
# no input from a command line, no window-tree dump, no platform frame record and no quiet
# detection: input goes through the input agent (below), and a run is judged by CPU, the log store
# and the recording.

SIM_RECORD_START_TIMEOUT_S = 20   # until the recorder prints "Recording started"
SIM_RECORD_STOP_TIMEOUT_S = 20    # after SIGINT, until the recorder has written its file and exited
SIM_RECORD_STARTED = "Recording started"
SIM_TERMINATE_WAIT_S = 1          # after terminating the app, before launching it
# The status bar and the home indicator, in thousandths of the recorded height, used only to name the
# class of a recorded frame for the reader ("changed only the system bars"): on a simulator every
# recorded frame after the first counts against the app, whatever its class. Chosen to cover a status
# bar of 54 of 852 points and a home indicator of 34 (about 63 and 40).
SIM_BARS_PERMILLE = (70, 45)
SIM_NOT_CHECKED = ("waiting for quiet", "frames at launch", "which app is in front", "find", "trace sections")


def xcrun(*args: str, check: bool = True, timeout: int = 60, env: dict | None = None) -> str:
    try:
        proc = subprocess.run(["xcrun", *args], capture_output=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"xcrun {' '.join(args)[:200]} did not finish within {timeout} s") from exc
    except OSError as exc:
        raise ProbeError(f"cannot run xcrun: {exc.strerror or exc}") from exc
    if check and proc.returncode != 0:
        said = proc.stderr.decode(errors="replace").strip()[:400] or proc.stdout.decode(errors="replace").strip()[-400:]
        raise ProbeError(f"xcrun {' '.join(args)} failed (exit {proc.returncode}): {said or 'no output'}")
    return proc.stdout.decode(errors="replace")


def sim_device(udid: str) -> dict | None:
    """The simulator with this udid as `simctl list devices -j` describes it (name, state, runtime),
    or None when there is none."""
    try:
        data = json.loads(xcrun("simctl", "list", "devices", "-j"))
    except json.JSONDecodeError as exc:
        raise ProbeError(f"`xcrun simctl list devices -j` did not print JSON: {exc}") from exc
    for runtime, devices in (data.get("devices") or {}).items():
        for dev in devices or []:
            if dev.get("udid") == udid:
                return {**dev, "runtime": runtime}
    return None


def sim_facts(udid: str, bundle: str) -> dict:
    dev = sim_device(udid)
    if dev is None:
        raise ProbeError(f"no simulator with udid {udid} (`xcrun simctl list devices`)")
    if dev.get("state") != "Booted":
        raise ProbeError(f"simulator {udid} is {dev.get('state')}; boot it first (`xcrun simctl boot {udid}`)")
    try:
        container = xcrun("simctl", "get_app_container", udid, bundle).strip()
    except ProbeError as exc:
        raise ProbeError(f"{bundle} is not installed on simulator {udid}: {exc}") from exc
    return {"platform": SIM_PLATFORM, "udid": udid, "name": dev.get("name"), "runtime": dev.get("runtime"),
            "state": dev.get("state"), "package": bundle, "app_container": container, "host_cpus": os.cpu_count()}


def parse_cputime(text: str | None) -> float | None:
    """Seconds from `ps -o cputime=`: m:ss.cc, h:mm:ss.cc or d-hh:mm:ss. None when empty or malformed."""
    t = (text or "").strip()
    if not t:
        return None
    days = 0
    if "-" in t:
        d, t = t.split("-", 1)
        if not d.isdigit():
            return None
        days = int(d)
    try:
        parts = [float(p) for p in t.split(":")]
    except ValueError:
        return None
    if not 1 <= len(parts) <= 3 or any(p < 0 for p in parts):
        return None
    secs = 0.0
    for p in parts:
        secs = secs * 60 + p
    return round(days * 86400 + secs, 2)


def host_cputime(pid: int) -> str:
    """The process's CPU time as `ps` prints it, or "" when there is no such process."""
    try:
        proc = subprocess.run(["ps", "-o", "cputime=", "-p", str(pid)], capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProbeError(f"cannot read the CPU time of process {pid}: {exc}") from exc
    return proc.stdout.decode(errors="replace").strip()


class SimRecorder:
    """`xcrun simctl io <udid> recordVideo` into a file, in a process group of its own, so that what
    `xcrun` starts behind it is signalled with it. stop() always ends the group: SIGINT, so that the
    recorder writes its file, then SIGKILL if it does not exit in time, then SIGKILL to anything
    left in the group."""

    def __init__(self, udid: str, path: Path):
        try:
            self.proc = subprocess.Popen(["xcrun", "simctl", "io", udid, "recordVideo", "--codec", "h264", "--force",
                                          str(path)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as exc:
            raise ProbeError(f"cannot start the simulator's recorder: {exc.strerror or exc}") from exc
        self.lines: list[str] = []
        self.started = threading.Event()
        self.killed = False
        self.alive_at_stop: bool | None = None
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        for raw in iter(self.proc.stdout.readline, b""):
            line = raw.decode(errors="replace").rstrip("\n")
            self.lines.append(line)
            if SIM_RECORD_STARTED in line:
                self.started.set()

    def wait_started(self, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        while not self.started.wait(0.05):
            if self.proc.poll() is not None:
                self.reader.join(2)
                raise ProbeError(f"the simulator's recorder exited (status {self.proc.returncode}) before it started "
                                 f"recording: {' | '.join(self.lines)[-400:] or 'no output'}")
            if time.monotonic() > deadline:
                raise ProbeError(f"the simulator's recorder did not report '{SIM_RECORD_STARTED}' within {timeout_s} s: "
                                 f"{' | '.join(self.lines)[-400:] or 'no output'}")

    def _signal_group(self, sig: int) -> None:
        try:
            os.killpg(self.proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass        # the group is gone already

    def stop(self) -> int | None:
        # Was it still recording when the window ended? One that stopped early (even with status 0)
        # leaves a recording that ends before the window did and looks clean.
        self.alive_at_stop = self.proc.poll() is None
        if self.alive_at_stop:
            self._signal_group(signal.SIGINT)
            try:
                self.proc.wait(SIM_RECORD_STOP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                self.killed = True
                self._signal_group(signal.SIGKILL)
                try:
                    self.proc.wait(10)
                except subprocess.TimeoutExpired:
                    pass
        self._signal_group(signal.SIGKILL)      # anything the recorder left behind in its group
        self.reader.join(5)
        return self.proc.returncode


class Terminated(SystemExit):
    """SIGTERM or SIGHUP during a simulator run: raised in the main thread, so that every finally
    (the recorder stopped, the status bar cleared) runs and failed.json is written."""

    def __init__(self, signum: int):
        super().__init__(128 + signum)
        self.signame = signal.Signals(signum).name


# --------------------------------------------------------------------------------------------
# the input agent for the iOS Simulator (ios-agent/): taps and swipes by fractions of the display
# --------------------------------------------------------------------------------------------
#
# A UI-testing bundle with no host app, built once per agent source and Xcode version into a cache,
# started with `xcodebuild test-without-building`, and spoken to over a loopback socket with a
# token. It queries the app under test for nothing but its state (see the agent's source).

AGENT_DIR = HERE / "ios-agent" if (HERE / "ios-agent").is_dir() else HERE.parent / "ios-agent"
AGENT_RUNNER = "dev.uiprobe.agent.uitests.xctrunner"   # what xcodebuild installs on the simulator
AGENT_TEST = "AgentUITests/AgentUITests/testServe"
AGENT_BUILD_TIMEOUT_S = 600
AGENT_START_TIMEOUT_S = 120       # from xcodebuild's start to the agent's first answer
AGENT_QUIT_WAIT_S = 15            # after "quit", for xcodebuild to exit before its group is signalled
AGENT_TERM_WAIT_S = 5             # after SIGTERM to its group, before SIGKILL
AGENT_IDLE_MARGIN_S = 120         # the agent's idle limit: the scenario's longest wait plus this
AGENT_COMMAND_TIMEOUT_S = 90


def agent_source_files() -> list[Path]:
    """The agent's source: every file under ios-agent/ but what Xcode writes beside it for one user."""
    if not AGENT_DIR.is_dir():
        return []
    return sorted(p for p in AGENT_DIR.rglob("*") if p.is_file() and "xcuserdata" not in p.parts
                  and not p.name.startswith("."))


def agent_cache_key(xcode_version: str) -> str:
    """A hash of the agent's source files and of what `xcodebuild -version` printed: a changed agent or
    another Xcode builds anew."""
    h = hashlib.sha256()
    h.update(xcode_version.strip().encode())
    for p in agent_source_files():
        h.update(b"\0" + str(p.relative_to(AGENT_DIR)).encode() + b"\0" + p.read_bytes())
    return h.hexdigest()[:16]


def agent_cache_root() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / "Library" / "Caches")
    return Path(base) / "ui-probe" / "ios-agent"


def xcode_version() -> str:
    try:
        proc = subprocess.run(["xcodebuild", "-version"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProbeError(f"cannot run `xcodebuild -version`: {exc}") from exc
    if proc.returncode != 0 or not proc.stdout.strip():
        raise ProbeError(f"`xcodebuild -version` failed (exit {proc.returncode}): {(proc.stderr or proc.stdout).strip()[:300]}")
    return proc.stdout


AGENT_BUILT_MARKER = "built.ok"       # written only after a build that exited 0: what "built" means
AGENT_LOCK_TIMEOUT_S = 900


def agent_built(folder: Path) -> Path | None:
    """The .xctestrun of a finished build in this folder, or None: the marker is written only after a
    build that exited 0, and a build is renamed into place only then."""
    if not (folder / AGENT_BUILT_MARKER).is_file():
        return None
    found = sorted((folder / "Build" / "Products").glob("*.xctestrun"))
    return found[0] if found else None


def ensure_agent_built() -> Path:
    """The agent's .xctestrun for this Xcode, built into the cache on first use. A build goes into a
    temporary folder under the cache root and is renamed into place once it exited 0 and the marker is
    written; a lock file serialises concurrent builds of one key. A failed build fails the run and
    names its saved log."""
    if not agent_source_files():
        raise ProbeError(f"the input agent's source is missing ({AGENT_DIR}); reinstall the plugin")
    key = agent_cache_key(xcode_version())
    root = agent_cache_root()
    folder = root / key
    built = agent_built(folder)
    if built is not None:
        return built
    root.mkdir(parents=True, exist_ok=True)
    with open(root / f"{key}.lock", "w") as lock:
        deadline = time.monotonic() + AGENT_LOCK_TIMEOUT_S
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise ProbeError(f"another build of the input agent held {root / (key + '.lock')} for "
                                     f"{AGENT_LOCK_TIMEOUT_S} s") from None
                time.sleep(0.5)
        built = agent_built(folder)             # built by the one that held the lock
        if built is not None:
            return built
        work = Path(tempfile.mkdtemp(prefix=f"{key}.building-", dir=root))
        log = root / f"{key}.build.log"
        cmd = ["xcodebuild", "build-for-testing", "-project", str(AGENT_DIR / "Agent.xcodeproj"), "-scheme", "Agent",
               "-destination", "generic/platform=iOS Simulator", "-derivedDataPath", str(work)]
        try:
            with open(log, "wb") as f:
                rc = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    timeout=AGENT_BUILD_TIMEOUT_S).returncode
        except subprocess.TimeoutExpired as exc:
            shutil.rmtree(work, ignore_errors=True)
            raise ProbeError(f"the input agent did not build within {AGENT_BUILD_TIMEOUT_S} s; its log is {log}") from exc
        except OSError as exc:
            shutil.rmtree(work, ignore_errors=True)
            raise ProbeError(f"cannot run xcodebuild to build the input agent: {exc.strerror or exc}") from exc
        found = sorted((work / "Build" / "Products").glob("*.xctestrun"))
        if rc != 0 or not found:
            shutil.rmtree(work, ignore_errors=True)
            raise ProbeError(f"the input agent could not be built (xcodebuild exit {rc}"
                             + ("" if rc else ", and no .xctestrun was written") + f"); its log is {log}")
        (work / AGENT_BUILT_MARKER).write_text(f"{time.time()}\n")
        if folder.exists():
            shutil.rmtree(folder)               # an unfinished build from before markers, or a stale one
        os.rename(work, folder)
        return agent_built(folder)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def tail(path: Path, lines: int = 12) -> str:
    try:
        return " | ".join(path.read_text(errors="replace").strip().splitlines()[-lines:]) or "no output"
    except OSError:
        return "no log"


AGENT_LOOKUPS_ENV = "TEST_RUNNER_UI_PROBE_AGENT_LOOKUPS"


class SimAgent:
    """The input agent under `xcodebuild test-without-building`, in a process group of its own.
    lookups=True starts it for a rehearsal (it then also serves find, type and labels); a measured
    run never sets it, and the variable is removed from what the agent inherits."""

    def __init__(self, udid: str, bundle: str, idle_s: int, raw: Path, lookups: bool = False, keep_results: bool = True):
        self.udid, self.raw, self.lookups, self.keep_results = udid, raw, lookups, keep_results
        xctestrun = ensure_agent_built()
        self.port, self.token = free_port(), secrets.token_hex(16)
        env = dict(os.environ, TEST_RUNNER_UI_PROBE_AGENT_PORT=str(self.port), TEST_RUNNER_UI_PROBE_AGENT_TOKEN=self.token,
                   TEST_RUNNER_UI_PROBE_AGENT_BUNDLE=bundle, TEST_RUNNER_UI_PROBE_AGENT_IDLE_S=str(idle_s))
        env.pop(AGENT_LOOKUPS_ENV, None)
        if lookups:
            env[AGENT_LOOKUPS_ENV] = "1"
        self.log_path = raw / "agent.log"
        self.log = open(self.log_path, "wb")
        # Without these, every agent run leaves a folder and a result bundle in Xcode's default derived
        # data. Removed after a test run that ended with status 0; kept otherwise.
        self.derived, self.result_bundle = raw / "agent-derived-data", raw / "agent.xcresult"
        try:
            self.proc = subprocess.Popen(["xcodebuild", "test-without-building", "-xctestrun", str(xctestrun),
                                          "-destination", f"platform=iOS Simulator,id={udid}", f"-only-testing:{AGENT_TEST}",
                                          "-derivedDataPath", str(self.derived), "-resultBundlePath", str(self.result_bundle)],
                                         stdin=subprocess.DEVNULL, stdout=self.log, stderr=subprocess.STDOUT, env=env,
                                         start_new_session=True)
        except OSError as exc:
            self.log.close()
            raise ProbeError(f"cannot start the input agent with xcodebuild: {exc.strerror or exc}") from exc
        self.record = {"port": self.port, "xctestrun": str(xctestrun), "idle_s": idle_s, "lookups": lookups}

    def proof(self, nonce: str) -> str:
        return hmac.new(self.token.encode(), nonce.encode(), hashlib.sha256).hexdigest()

    def _exchange(self, command: dict, timeout: float) -> dict:
        """Send one command with the run's token and a fresh nonce and read the one reply, which must carry
        the keyed hash of the nonce (HMAC-SHA256 with the token): a reply without it did not come from
        the agent and is a failed command. OSError when nothing answered."""
        nonce = secrets.token_hex(16)
        with socket.create_connection(("127.0.0.1", self.port), timeout=timeout) as sock:
            sock.sendall(json.dumps({**command, "token": self.token, "nonce": nonce}).encode() + b"\n")
            data = b""
            while not data.endswith(b"\n") and len(data) < 1_000_000:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                data += chunk
        try:
            reply = json.loads(data) if data.strip() else None
        except ValueError:
            reply = None
        if not isinstance(reply, dict):
            return {"ok": False, "error": f"the agent's reply is not a JSON object: {data[:200]!r}"}
        if not hmac.compare_digest(str(reply.get("proof", "")), self.proof(nonce)):
            return {"ok": False, "error": "the reply was not proven to come from the agent (no keyed hash of the "
                                          f"command's nonce); it said: {reply.get('error') or reply}",
                    "unproven": True}
        return reply

    def send(self, command: dict, timeout: float = AGENT_COMMAND_TIMEOUT_S) -> tuple[dict, int]:
        """(the agent's reply, the round trip in ms). A reply that never came is {"ok": false, ...}."""
        started = time.monotonic()
        try:
            reply = self._exchange(command, timeout)
        except OSError as exc:
            reply = {"ok": False, "error": f"no reply from the agent: {exc}"}
        return reply, round((time.monotonic() - started) * 1000)

    def wait_ready(self, timeout_s: float) -> dict:
        deadline = time.monotonic() + timeout_s
        while True:
            if self.proc.poll() is not None:
                raise ProbeError(f"xcodebuild exited (status {self.proc.returncode}) before the input agent answered; "
                                 f"its log ({self.log_path}) ends: {tail(self.log_path)}")
            try:
                reply = self._exchange({"cmd": "ping"}, timeout=5)
            except OSError:
                reply = None
            if reply is not None:
                if not reply.get("ok"):
                    raise ProbeError(f"the input agent answered but refused the run's first command: {reply.get('error')}")
                self.record["ready"] = reply
                # An agent in the wrong mode is a failed start: a measured run must never be able to
                # read the app, and a rehearsal needs to.
                if self.lookups and reply.get("lookups") is not True:
                    raise ProbeError("the input agent was started for a rehearsal but its first answer does not say "
                                     f"that it serves lookups ('lookups': {json.dumps(reply.get('lookups'))}); "
                                     "nothing was done")
                if not self.lookups and reply.get("lookups") is not False:
                    raise ProbeError("the input agent was started for a measured run but its first answer does not say "
                                     f"that lookups are off ('lookups': {json.dumps(reply.get('lookups'))}); the app "
                                     "was not launched")
                return reply
            if time.monotonic() > deadline:
                raise ProbeError(f"the input agent did not answer within {timeout_s} s; its log ({self.log_path}) "
                                 f"ends: {tail(self.log_path)}")
            time.sleep(0.25)

    def _signal_group(self, sig: int) -> None:
        try:
            os.killpg(self.proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def stop(self) -> None:
        """quit, then xcodebuild's group ended (SIGTERM, then SIGKILL), then the runner app terminated and
        uninstalled from the simulator, then the agent's derived data and result bundle removed if its
        test run ended with status 0. Every phase runs whatever the one before it raised, and every wait
        has a limit. Never raises."""
        info = {"quit": None, "exit": None, "terminated": False, "killed": False}

        def end_xcodebuild():
            if self.proc.poll() is None:
                info["quit"], _ = self.send({"cmd": "quit"}, timeout=5)
                try:
                    self.proc.wait(AGENT_QUIT_WAIT_S)
                except subprocess.TimeoutExpired:
                    info["terminated"] = True
                    self._signal_group(signal.SIGTERM)
                    try:
                        self.proc.wait(AGENT_TERM_WAIT_S)
                    except subprocess.TimeoutExpired:
                        info["killed"] = True
                        self._signal_group(signal.SIGKILL)
                        self.proc.wait(10)

        def remove_runner():
            for action in ("terminate", "uninstall"):
                try:
                    xcrun("simctl", action, self.udid, AGENT_RUNNER, check=False, timeout=60)
                except ProbeError:
                    pass        # failures ignored: the runner may not be there

        def tidy():
            # keep_results False (a rehearsal that types): never kept, since they log every typed character
            if info["exit"] == 0 or not self.keep_results:
                for path in (self.derived, self.result_bundle):
                    shutil.rmtree(path, ignore_errors=True)

        def record():
            self.record["stop"] = info
            (self.raw / "agent.json").write_text(json.dumps(self.record, indent=1))

        run_phases(end_xcodebuild,
                   lambda: self._signal_group(signal.SIGKILL),          # anything xcodebuild left in its group
                   lambda: info.__setitem__("exit", self.proc.poll()),
                   self.log.close, remove_runner, tidy, record)


def run_phases(*phases) -> None:
    """Run every phase of a clean-up, whatever the one before it raised; report what failed."""
    for phase in phases:
        try:
            phase()
        except Exception as exc:        # noqa: BLE001 - a clean-up phase must not stop the next one
            print(f"ui-probe: a clean-up step failed ({type(exc).__name__}: {exc}); the next ones still ran",
                  file=sys.stderr)


class CleanupShield:
    """Hold SIGINT, SIGTERM and SIGHUP while a clean-up runs, and deliver the first one afterwards, so
    that a signal cannot cut a clean-up short and the run still ends as interrupted or terminated."""

    def __enter__(self):
        self.pending: list[int] = []
        self.saved = {}
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                self.saved[sig] = signal.signal(sig, lambda s, f: self.pending.append(s))
        return self

    def __exit__(self, exc_type, exc, tb):
        for sig, handler in self.saved.items():
            signal.signal(sig, handler)
        if self.pending:
            first = self.pending[0]
            if first == signal.SIGINT:
                raise KeyboardInterrupt
            raise Terminated(first)
        return False


def agent_idle_s(scn: dict) -> int:
    """The agent's idle limit: the scenario's longest wait plus a margin."""
    waits = [scn.get("launch_settle_ms", 6000)]
    for s in scn["steps"]:
        waits += [s.get("ms", 0), s.get("settle_ms", default_settle_ms(s["do"]) if s["do"] != "rest" else 0)]
        if s["do"] == "tap" and "find" in s and find_scroll(s) is not None:
            # a run replays up to max swipes before the tap, each followed by its settle time
            sc = find_scroll(s)
            waits.append(sc["max"] * (sc["ms"] + sc["settle_ms"]))
    return int(max(waits) / 1000 + AGENT_IDLE_MARGIN_S)


def run_once_sim(udid: str, scn: dict, out: Path, rehearsal: dict | None = None, requests: Path | None = None) -> dict:
    """One measured run. rehearsal ({"path", "data"}, checked by check_rehearsal) is required when the
    scenario has a tap with 'find'. requests: a front's log (checked by check_request_log)."""
    validate_scenario(scn, SIM_PLATFORM)
    if sim_find_steps(scn) and rehearsal is None:
        raise ProbeError(no_rehearsal_message(scn, udid))
    refuse_used(out)
    refuse_rehearsal_folder(out)
    out.mkdir(parents=True, exist_ok=True)
    previous = {}

    def on_signal(signum, frame):
        for sig in previous:            # one is enough: let the clean-up finish
            signal.signal(sig, signal.SIG_IGN)
        raise Terminated(signum)

    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGHUP):
            previous[sig] = signal.signal(sig, on_signal)
    try:
        return _run_once_sim(udid, scn, out, rehearsal, requests)
    except BaseException as exc:
        write_failed(out, exc, scn.get("name"))
        raise
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


# The status bar is frozen for a simulator run: its clock would otherwise change during a long rest,
# and the recorder would keep the frames (with encoder noise that reaches the app's area).
SIM_STATUS_BAR = ("--time", "9:41")
# XCUIApplication.State, as the agent's ping reports it.
SIM_APP_IN_FRONT = 4
SIM_APP_STATES = {0: "unknown", 1: "not running", 2: "running in the background, suspended",
                  3: "running in the background", 4: "running in front"}


def _run_once_sim(udid: str, scn: dict, out: Path, rehearsal: dict | None = None,
                  requests: Path | None = None) -> dict:
    bundle = scn["package"]
    req_start = log_position(requests) if requests else None
    facts = sim_facts(udid, bundle)
    raw = out / "raw"
    raw.mkdir()
    load_before = host_load()
    started = time.time()
    try:
        xcrun("simctl", "status_bar", udid, "override", *SIM_STATUS_BAR)
    except ProbeError as exc:
        raise ProbeError(f"the simulator's status bar could not be frozen, so its clock would change the screen at "
                         f"rest: {exc}") from exc
    try:
        agent = None
        try:
            if sim_needs_agent(scn):
                agent = SimAgent(udid, bundle, agent_idle_s(scn), raw)
                ready = agent.wait_ready(AGENT_START_TIMEOUT_S)
                if rehearsal is not None and ready.get("screen") != rehearsal["data"]["screen"]:
                    raise ProbeError(f"the simulator's screen is {ready.get('screen')} points but the rehearsal "
                                     f"{rehearsal['path']} found its positions on {rehearsal['data']['screen']}; "
                                     "rehearse again on this simulator")
                if sim_has_url(scn):
                    sim_clear_stale_alert(agent, raw)
            _sim_device_part(udid, scn, raw, bundle, agent, rehearsal)
        finally:
            if agent is not None:
                with CleanupShield():
                    agent.stop()
    finally:
        with CleanupShield():
            try:
                xcrun("simctl", "status_bar", udid, "clear")
            except ProbeError as exc:
                print(f"ui-probe: the simulator's status bar override could not be cleared ({exc}); clear it with "
                      f"`xcrun simctl status_bar {udid} clear`", file=sys.stderr)
    if requests:
        capture_requests_sim(raw, requests, req_start, (started, time.time()), scn)
    wall = time.time() - started
    (out / "capture.json").write_text(json.dumps({
        "tool": {"name": "ui-probe", "version": VERSION}, "platform": SIM_PLATFORM,
        "scenario": scn, "device": facts, "trace": False, "video": True, "sections": False,
        "status_bar_override": list(SIM_STATUS_BAR), "launch_env": scn.get("launch_env") or {},
        "requests_log": str(requests) if requests else None,
        "launch_env_dropped": json.loads(_read_raw(raw, "launch-env.json") or "{}").get("dropped", []),
        "host": {"load1_before": round(load_before, 2), "load1_after": round(host_load(), 2), "wall_s": round(wall, 1)},
    }, indent=1))
    try:
        return analyze_run(out)
    except ProbeError as exc:
        raise ProbeError(f"{out}: the capture was saved but its analysis failed: {exc}") from exc


def sim_launch(udid: str, scn: dict, raw: Path, bundle: str) -> tuple[int, float]:
    """Terminate the app, launch it with the scenario's launch_env and return (its process id, the
    wall-clock time of the launch). Saves raw/launch-env.json, raw/launch.txt, raw/launch-time.json."""
    xcrun("simctl", "terminate", udid, bundle, check=False)       # not running is not an error
    time.sleep(SIM_TERMINATE_WAIT_S)
    # launch_env reaches the app's process through simctl's SIMCTL_CHILD_ prefix, on this call only.
    launch_env = scn.get("launch_env") or {}
    dropped = sorted(k for k in os.environ if k.startswith("SIMCTL_CHILD_"))
    env = {k: v for k, v in os.environ.items() if not k.startswith("SIMCTL_CHILD_")}
    env.update({f"SIMCTL_CHILD_{k}": v for k, v in launch_env.items()})
    (raw / "launch-env.json").write_text(json.dumps({"dropped": dropped}))
    launch_wall = time.time()
    launched = xcrun("simctl", "launch", udid, bundle, env=env)
    (raw / "launch.txt").write_text(launched)
    (raw / "launch-time.json").write_text(json.dumps({"wall": launch_wall}))
    m = re.search(rf"^{re.escape(bundle)}:\s*(\d+)\s*$", launched.strip(), re.M)
    if not m:
        raise ProbeError(f"`xcrun simctl launch` printed no process id for {bundle}: {launched.strip()[:200]!r}")
    return int(m.group(1)), launch_wall


SIM_OPENURL_TIMEOUT_S = 60
# After `simctl openurl` with a custom scheme the system asks "Open in <app>?" (Cancel, Open) and the app
# gets nothing until Open is tapped (measured on an iOS 26.1 simulator, 2026-11-11). The alert is the
# system's own: it outlives the app and sits above it after the next launch. The agent answers it by
# querying SpringBoard alone, never the app under test.
SIM_ALERT_OPEN = "Open"
SIM_ALERT_CANCEL = "Cancel"
SIM_ALERT_CONFIRM_TIMEOUT_S = 5     # no alert in this time is not a failure: a link may open directly
SIM_ALERT_CLEAR_TIMEOUT_S = 1


def sim_alert_command(button: str, timeout_s: float) -> dict:
    return {"cmd": "alert", "button": button, "timeout_s": timeout_s}


def sim_clear_stale_alert(agent: "SimAgent", raw: Path) -> dict:
    """Before the first step of a scenario with a link: cancel a system alert a link left behind (an
    earlier run that opened a link and stopped before confirming it). Saved as raw/stale-alert.json:
    the command, the agent's reply, the round trip and whether an alert was cleared."""
    command = sim_alert_command(SIM_ALERT_CANCEL, SIM_ALERT_CLEAR_TIMEOUT_S)
    reply, round_trip = agent.send(command)
    record = {"command": command, "reply": reply, "round_trip_ms": round_trip,
              "cleared": bool(reply.get("ok") and reply.get("tapped"))}
    (raw / "stale-alert.json").write_text(json.dumps(record, indent=1))
    return record


def sim_open_url(udid: str, url: str) -> dict:
    """`xcrun simctl openurl <udid> <url>`: {'opened', 'exit', 'error'} (error: what simctl printed when
    it failed). The link is one argument, never read by a shell."""
    code, out, err = xcrun_result("simctl", "openurl", udid, url, timeout=SIM_OPENURL_TIMEOUT_S)
    said = (err.strip() or out.strip())[-400:] or None
    return {"opened": code == 0, "exit": code,
            "error": None if code == 0 else (said or ("simctl did not finish" if code is None else "simctl said nothing"))}


def replay_lookup(agent: SimAgent, rehearsal: dict, step: dict) -> dict:
    """Before a tap with 'find' in a measured run, and before its recorder starts: the swipes the
    rehearsal made to reach the element, each followed by its settle_ms, as Android's lookup and its
    scrolling come before the step's window. Returns the step's 'resolved' record: where the tap goes
    and every swipe with the agent's reply and round trip. A swipe the agent did not perform is
    recorded, and the gate fails the step for it as for a tap it did not perform."""
    done = rehearsal["data"]["steps"][step["id"]]
    swipes = []
    for sw in done["swipes"]:
        reply, round_trip = agent.send({"cmd": "swipe", "from": sw["from"], "to": sw["to"], "ms": sw["ms"]})
        swipes.append({**sw, "reply": reply, "round_trip_ms": round_trip})
        time.sleep(sw["settle_ms"] / 1000)
    return {"x": done["center"][0], "y": done["center"][1], "swipes": swipes, "rehearsal": rehearsal["path"],
            "rehearsal_finished": rehearsal["data"]["finished"]}


def _sim_device_part(udid: str, scn: dict, raw: Path, bundle: str, agent: SimAgent | None = None,
                     rehearsal: dict | None = None) -> None:
    """Launch the app and record every step: a rest waits its 'ms'; a tap or swipe is sent to the agent
    and the step waits its 'settle_ms'; a tap with 'find' goes to the position the rehearsal found, after
    the rehearsal's swipes for it. With the agent, every step ends with a ping for the app's state. The
    app is left running afterwards."""
    pid, launch_wall = sim_launch(udid, scn, raw, bundle)
    time.sleep(scn.get("launch_settle_ms", 6000) / 1000)
    (raw / "pid-launch.txt").write_text(f"{pid}\n" if host_cputime(pid) else "")
    last_wall = None
    for step in scn["steps"]:
        sid = step["id"]
        resolved = replay_lookup(agent, rehearsal, step) if "find" in step else None
        recorder = SimRecorder(udid, raw / f"rec-{sid}.mp4")
        before = after = ""
        t0 = t1 = w0 = w1 = rec_start = rec_end = None
        command = reply = ping = url_open = None
        round_trip = None
        try:
            recorder.wait_started(SIM_RECORD_START_TIMEOUT_S)
            rec_start = time.time()
            before, t0, w0 = host_cputime(pid), time.monotonic(), time.time()
            if step["do"] == "rest":
                time.sleep(step["ms"] / 1000)
            elif step["do"] == "url":
                # the simulator opens the link, as a tap on a link outside the app would; the system then
                # asks whether to open it in the app, and the agent confirms that alert (SpringBoard only)
                url_open = sim_open_url(udid, step["url"])
                if url_open["opened"]:
                    command = sim_alert_command(SIM_ALERT_OPEN, SIM_ALERT_CONFIRM_TIMEOUT_S)
                    reply, round_trip = agent.send(command)
                    url_open["alert_confirmed"] = bool(reply.get("ok") and reply.get("tapped"))
                time.sleep(step.get("settle_ms", default_settle_ms(step["do"])) / 1000)
            else:
                command = sim_command(step, resolved)
                reply, round_trip = agent.send(command)
                time.sleep(step.get("settle_ms", default_settle_ms(step["do"])) / 1000)
            after, t1, w1 = host_cputime(pid), time.monotonic(), time.time()
            if agent is not None:
                ping, _ = agent.send({"cmd": "ping"}, timeout=30)
        finally:
            with CleanupShield():
                code = recorder.stop()
                rec_end = time.time()
                (raw / f"rec-{sid}.log").write_text("\n".join(recorder.lines) + "\n")
        (raw / f"pid-{sid}.txt").write_text(f"{pid}\n" if after else "")
        (raw / f"step-{sid}.json").write_text(json.dumps({
            "pid": pid, "cputime_before": before, "cputime_after": after, "window_s": round(t1 - t0, 3),
            "wall_before": w0, "wall_after": w1, "rec_wall_start": rec_start, "rec_wall_end": rec_end,
            "recorder_exit": code, "recorder_killed": recorder.killed,
            "recorder_alive_at_end": recorder.alive_at_stop,
            **({} if agent is None else {"command": command, "reply": reply, "round_trip_ms": round_trip,
                                         "end_ping": ping,
                                         "app_state": ping.get("app_state") if ping and ping.get("ok") else None}),
            **({} if resolved is None else {"resolved": resolved}),
            **({} if url_open is None else {"url_open": url_open})},
            indent=1))
        last_wall = w1
    read_log_store(udid, scn, raw, pid, launch_wall, last_wall)


# --------------------------------------------------------------------------------------------
# a rehearsal on the iOS Simulator: lookups and typing in an app process that is thrown away
# --------------------------------------------------------------------------------------------
#
# Reading an app's accessibility tree, and a text field that has had focus, change the app for the
# rest of its process's life (measured). So a measured run never reads the app or types: `ui-probe
# rehearse` drives the scenario once in a process it then ends, looking elements up and typing there,
# and the positions it found are replayed by coordinate in the fresh process the run measures.

REHEARSAL_FILE = "rehearsal.json"
# Written first into a rehearsal's folder, before anything else, so that gate, summary and compare
# never take that folder (completed, failed or killed) for a run.
REHEARSAL_MARKER = "rehearsal-started.json"
REHEARSAL_GONE_WAIT_S = 5         # after the app is terminated, for its process to be gone
REHEARSAL_MAX_AGE_S = 3600        # the default of `run --rehearsal-max-age-s`
SIM_FIND_POLL_S = 0.3             # between two reads of the app's tree while a lookup waits
SIM_FIND_COMMAND_TIMEOUT_S = 30   # one find or labels command (measured: 30 to 120 ms)
SIM_TYPE_CHAR_S = 3.0             # one typed character, for the command's time limit (measured: about 1.5 s)
SIM_TYPE_MARGIN_S = 30
SIM_REDACTED = "<typed text>"
# What a rehearsal records of the installed app, and how a message names each value.
APP_IDENTITY = {"short_version": "CFBundleShortVersionString", "version": "CFBundleVersion",
                "executable_sha256": "executable's sha256",
                "bundle_sha256": "bundle digest (over every file's path and sha256)"}


def bundle_digest(bundle_path: str) -> str:
    """sha256 over the sorted list of every file in the bundle: its path relative to the bundle and its
    sha256, or for a symbolic link its target text (never followed). Lines are 'path\\tsha256:<hex>' or
    'path\\tlink:<target>'. OSError when any part cannot be read: the identity is then unknown."""
    root = Path(bundle_path)
    if not root.is_dir():
        raise OSError(f"{root} is not a folder")
    entries = []

    def fail(exc):
        raise exc
    for dirpath, dirnames, filenames in os.walk(root, onerror=fail, followlinks=False):
        here = Path(dirpath)
        for name in list(dirnames):
            if (here / name).is_symlink():
                entries.append((str((here / name).relative_to(root)), "link:" + os.readlink(here / name)))
                dirnames.remove(name)
        for name in filenames:
            path = here / name
            rel = str(path.relative_to(root))
            if path.is_symlink():
                entries.append((rel, "link:" + os.readlink(path)))
                continue
            h = hashlib.sha256()
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            entries.append((rel, "sha256:" + h.hexdigest()))
    total = hashlib.sha256()
    for rel, what in sorted(entries):
        total.update(f"{rel}\t{what}\n".encode("utf-8", "surrogateescape"))
    return total.hexdigest()


def utc_stamp(when: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if when is None else when))


def parse_utc_stamp(text) -> float | None:
    try:
        return datetime.datetime.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc).timestamp()
    except ValueError:
        return None


def scenario_digest(scn: dict) -> str:
    """sha256 of the scenario as loaded, as canonical JSON (sorted keys, compact separators)."""
    return hashlib.sha256(json.dumps(scn, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def app_identity(bundle_path: str | None, reason: str | None = None) -> dict:
    """The installed app as a rehearsal and a run compare it: CFBundleShortVersionString and
    CFBundleVersion from the bundle's Info.plist and the sha256 of its executable (CFBundleExecutable).
    A value that cannot be read is None, and 'unknown' says why."""
    ident = {"bundle_path": bundle_path, "short_version": None, "version": None, "executable": None,
             "executable_sha256": None, "bundle_sha256": None, "unknown": []}
    if bundle_path is None:
        ident["unknown"].append(reason or "the app's bundle was not found")
        return ident
    try:
        ident["bundle_sha256"] = bundle_digest(bundle_path)
    except OSError as exc:
        ident["unknown"].append(f"the bundle cannot be read in full: {exc.strerror or exc}")
    import plistlib
    try:
        with open(Path(bundle_path) / "Info.plist", "rb") as fh:
            info = plistlib.load(fh)
    except (OSError, ValueError, plistlib.InvalidFileException) as exc:
        ident["unknown"].append(f"{Path(bundle_path) / 'Info.plist'} cannot be read: {exc}")
        return ident
    if not isinstance(info, dict):
        ident["unknown"].append("Info.plist does not hold a dictionary")
        return ident
    for key, name in (("short_version", "CFBundleShortVersionString"), ("version", "CFBundleVersion"),
                      ("executable", "CFBundleExecutable")):
        value = info.get(name)
        if isinstance(value, str) and value:
            ident[key] = value
        else:
            ident["unknown"].append(f"Info.plist has no {name}")
    exe = ident["executable"]
    if exe is not None:
        if "/" in exe or exe in (".", ".."):
            ident["unknown"].append(f"CFBundleExecutable {exe!r} is not a file name")
        else:
            h = hashlib.sha256()
            try:
                with open(Path(bundle_path) / exe, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 20), b""):
                        h.update(chunk)
                ident["executable_sha256"] = h.hexdigest()
            except OSError as exc:
                ident["unknown"].append(f"the executable {exe} cannot be read: {exc.strerror or exc}")
    return ident


def sim_app_identity(udid: str, bundle: str) -> dict:
    try:
        return app_identity(xcrun("simctl", "get_app_container", udid, bundle).strip() or None,
                            "`xcrun simctl get_app_container` printed no path")
    except ProbeError as exc:
        return app_identity(None, f"{bundle} is not installed on simulator {udid}: {exc}")


def no_rehearsal_message(scn: dict, udid: str, scenario_path: str = "<scenario>") -> str:
    sid = sim_find_steps(scn)[0]["id"]
    return (f"step '{sid}': a tap with 'find' on the iOS Simulator goes to a position found by a rehearsal, since a "
            f"measured run never reads the app: run `ui-probe rehearse --simulator {udid} --scenario {scenario_path} "
            "--out <dir>` first and pass its rehearsal.json with --rehearsal")


def describe_find_sim(find: dict) -> str:
    key = "text" if "text" in find else "desc"
    what = (f"the element with the label or value {json.dumps(find[key])}" if key == "text"
            else f"the element with the label {json.dumps(find[key])}")
    return what + (f" (match {find['nth']} in the order of the tree)" if "nth" in find else "")


def type_timeout_s(text: str) -> float:
    return SIM_TYPE_MARGIN_S + SIM_TYPE_CHAR_S * len(text)


def rehearsal_idle_s(scn: dict) -> int:
    """The agent's idle limit in a rehearsal: as in a run, and longer than the longest typing, since the
    agent counts its idle time from the start of the command it last received."""
    typing = [int(type_timeout_s(s["text"]) + AGENT_IDLE_MARGIN_S) for s in scn["steps"] if s["do"] == "text"]
    # a lookup polls up to its timeout, which holds every swipe and its settle time, and a swipe begun
    # just before the timeout runs on past it
    lookups = []
    for s in sim_find_steps(scn):
        sc = find_scroll(s)
        overrun = 0 if sc is None else sc["ms"] + sc["settle_ms"]
        lookups.append(int((find_timeout_ms(scn, s) + overrun) / 1000 + AGENT_IDLE_MARGIN_S))
    return max([agent_idle_s(scn)] + typing + lookups)


def redact(value, secrets_: list[str]):
    """value with every string that holds a text the scenario types replaced by SIM_REDACTED."""
    if isinstance(value, str):
        return SIM_REDACTED if any(t and t in value for t in secrets_) else value
    if isinstance(value, list):
        return [redact(v, secrets_) for v in value]
    if isinstance(value, dict):
        return {k: redact(v, secrets_) for k, v in value.items()}
    return value


def _fraction_pair(v) -> bool:
    return (isinstance(v, list) and len(v) == 2
            and all(isinstance(x, (int, float)) and not isinstance(x, bool) and 0 <= x <= 1 for x in v))


def sim_lookup(agent: SimAgent, scn: dict, step: dict, out: Path, clock=time.monotonic, sleep=time.sleep) -> dict:
    """Find a tap's element through the agent, by the rule an Android lookup follows (find_el in the
    device script): read the app's tree until the element is there, on the screen and still. A match
    counts when part of it is on the display ('on_screen') and XCTest's hit test says a tap would reach
    it ('hittable'); 'under' is only a hint for the message and decides nothing. Without
    'nth' exactly one match may count; with it, at least nth, and the nth in the order of the tree is
    taken. It is found when it has the same frame in two reads in a row. A read with no acceptable match
    is 'not yet': wait SIM_FIND_POLL_S and read again, up to the timeout (find_timeout_ms); a match first
    seen at the timeout gets one more read to hold still. With find.scroll, a read that shows no
    acceptable match is followed by a swipe and its settle time while fewer than max swipes were made.
    On a timeout the status is unstable (a match moved), not_visible (matches were there but none
    counted: off the screen, or on it where a tap would not reach them) or not_found; lookup-failed.txt
    is written and LookupFailed
    raised. A find reply that is not ok, or a swipe the agent did not perform, fails at once."""
    sid, find = step["id"], step["find"]
    key = "text" if "text" in find else "desc"
    nth = find.get("nth", 0)
    timeout = find_timeout_ms(scn, step)
    scroll = find_scroll(step)
    t0 = clock()
    reads = 0
    swipes: list[dict] = []
    pb = None
    grace = False
    while True:
        reads += 1
        reply, _ = agent.send({"cmd": "find", key: find[key]}, timeout=SIM_FIND_COMMAND_TIMEOUT_S)
        if not reply.get("ok"):
            raise ProbeError(f"step '{sid}': the agent did not perform the lookup of {describe_find_sim(find)}: "
                             f"{reply.get('error') or 'no reply'}; the rehearsal stopped there")
        matches = reply.get("matches")
        found = reply.get("found")
        if not isinstance(matches, list) or not all(isinstance(m, dict) for m in matches) or not isinstance(found, int):
            raise ProbeError(f"step '{sid}': the agent's answer to a lookup is malformed (no 'found' count or no "
                             "'matches' list); the rehearsal stopped there")
        counted = [m for m in matches if m.get("on_screen") is True and m.get("hittable") is True]
        vn = len(counted)
        # Only the first matches are listed: without nth, "exactly one" cannot be told when more exist.
        truncated = found > len(matches)
        # The match named when too few count: one on the display that a tap would not reach says more
        # than one off the screen, so it is named first.
        rest = [m for m in matches if not any(m is c for c in counted)]
        off = next((m for m in rest if m.get("on_screen") is True), None) or (rest[0] if rest else None)
        chosen = None
        if (nth == 0 and vn == 1 and not truncated) or (nth > 0 and vn >= nth):
            chosen = counted[max(nth, 1) - 1]
        b = chosen.get("frame") if chosen is not None else None
        # matches that do not count matter only when too few count (not when several do)
        if chosen is not None or (nth == 0 and vn > 0):
            off = None
        elapsed_ms = round((clock() - t0) * 1000)
        if b is not None and b == pb:
            centre = chosen.get("center")
            if not _fraction_pair(centre):
                raise ProbeError(f"step '{sid}': the agent gave the match a centre that is not two fractions of the "
                                 f"display ({centre!r}); the rehearsal stopped there")
            return {"status": "found", "match": {k: chosen.get(k) for k in ("label", "type", "frame", "visible")
                                                 if k in chosen},
                    "center": centre, "swipes": swipes, "reads": reads, "matches": found, "visible": vn,
                    "lookup_ms": elapsed_ms}
        if elapsed_ms >= timeout:
            if b is not None and not grace:
                grace = True
                pb = b
                continue
            status = "unstable" if b is not None else ("not_visible" if off is not None else "not_found")
            raise sim_lookup_failure(agent, scn, step, out, {
                "status": status, "reads": reads, "swipes": swipes, "elapsed_ms": elapsed_ms, "found": found,
                "visible": vn, "truncated": truncated, "matches": matches, "off": off, "last_frame": b,
                "previous_frame": pb, "screen": reply.get("screen")})
        pb = b
        if b is None and scroll is not None and len(swipes) < scroll["max"]:
            sw = {"from": scroll["from"], "to": scroll["to"], "ms": scroll["ms"], "settle_ms": scroll["settle_ms"]}
            sw_reply, _ = agent.send({"cmd": "swipe", "from": sw["from"], "to": sw["to"], "ms": sw["ms"]})
            if not sw_reply.get("ok"):
                raise ProbeError(f"step '{sid}': the agent did not perform swipe {len(swipes) + 1} of the lookup's "
                                 f"'find.scroll': {sw_reply.get('error') or 'no reply'}; the rehearsal stopped there")
            swipes.append(sw)
            pb = None
            sleep(sw["settle_ms"] / 1000)
            continue
        sleep(SIM_FIND_POLL_S)


def sim_lookup_failure(agent: SimAgent, scn: dict, step: dict, out: Path, rec: dict) -> LookupFailed:
    """Write lookup-failed.txt (what was looked for, the status, the matches of the last read and the
    app's labelled elements) and return the error, built as lookup_failure builds Android's."""
    sid, find = step["id"], step["find"]
    typed = [s["text"] for s in scn["steps"] if s["do"] == "text"]
    labels, _ = agent.send({"cmd": "labels"}, timeout=SIM_FIND_COMMAND_TIMEOUT_S)
    labels = {k: v for k, v in labels.items() if k != "proof"}
    want = "exactly one" if "nth" not in find else f"at least {find['nth']}"
    status, off, scroll = rec["status"], rec["off"], find_scroll(step)
    count_note = f"; only the first {len(rec['matches'])} are listed" if rec["truncated"] else ""
    if status == "unstable":
        seen = ("a match was there but did not hold still: two reads in a row never gave the same frame "
                f"(frame {rec['previous_frame']} then {rec['last_frame']})")
    elif status == "not_visible":
        if off.get("on_screen") is True:
            if off.get("moved"):
                why = "the app's tree changed between the agent's read and its hit test, so it could not be asked"
            elif off.get("not_asked"):
                why = ("the hit test was not asked (more than ten matches on the display, or more than twenty "
                       "elements carry the label)")
            else:
                why = "XCTest's hit test says a tap there would reach something else"
                if off.get("under"):
                    why += (f", probably {json.dumps(off['under'])} (a hint from the order of the app's tree, "
                            "not a verdict)")
            where_off = (f"on the screen (frame {off.get('frame')}) where a tap would not reach it: {why}")
            hint = ("; add 'find.scroll' to bring it clear of what lies over it" if scroll is None else
                    f"; {len(rec['swipes'])} swipes did not bring it where a tap reaches it")
        else:
            screen = rec.get("screen")
            on = f"a {screen[0]:g} x {screen[1]:g} point screen" if _number_pair(screen) else "the screen"
            where_off = f"off the screen (frame {off.get('frame')} on {on})"
            hint = ("; add 'find.scroll' to swipe it into view" if scroll is None else
                    f"; {len(rec['swipes'])} swipes did not bring it onto the screen")
        seen = (f"{rec['found']} matched at the last read, {rec['visible']} of them on the screen where a tap reaches "
                f"them ({want} needed){count_note}: the element is in the app's tree but {where_off}{hint}")
    else:
        seen = f"{rec['found']} matched at the last read ({want} needed){count_note}"
        if rec["visible"] != rec["found"]:
            seen += f", {rec['visible']} of them on the screen where a tap reaches them"
    path = out / LOOKUP_FAILED
    lines = [f"step: {sid}", f"looked for: {describe_find_sim(find)}", f"status: {status}",
             f"reads: {rec['reads']}", f"swipes: {len(rec['swipes'])}", f"elapsed_ms: {rec['elapsed_ms']}",
             f"timeout_ms: {find_timeout_ms(scn, step)}",
             f"matches at the last read: {rec['found']} found, {len(rec['matches'])} listed, {rec['visible']} on the "
             "screen where a tap reaches them (on_screen and hittable both true; 'under' is a hint only; 'moved': "
             "the tree changed before the hit test; 'not_asked': the hit test was not asked; 'visible': the part "
             "the tap point was moved into)"]
    lines += ["  " + json.dumps(m) for m in redact(rec["matches"], typed)]
    if typed and isinstance(labels.get("elements"), list):
        # A field shows typed text reformatted, which no substring check can catch: no value at all.
        labels = {**labels, "elements": [{k: v for k, v in e.items() if k != "value"} if isinstance(e, dict) else e
                                         for e in labels["elements"]]}
        lines += ["what the app's tree held (labels; no element value is written, since the scenario types text "
                  "that a field may show reformatted):"]
    else:
        lines += ["what the app's tree held (labels):"]
    lines += [json.dumps(redact(labels, typed), indent=1)]
    try:
        path.write_text("\n".join(lines) + "\n")
        where = f"what the screen held is in {path}"
    except OSError as exc:
        where = f"{path} could not be written ({exc.strerror or exc})"
    verb = {"unstable": "did not hold still",
            "not_visible": ("could not be tapped" if off is not None and off.get("on_screen") is True
                            else "was not on the screen")
            }.get(status, "did not appear")
    swiped = "" if scroll is None else f", {len(rec['swipes'])} swipes"
    return LookupFailed(f"step '{sid}': {describe_find_sim(find)} {verb} within {find_timeout_ms(scn, step)} ms "
                        f"({rec['reads']} reads{swiped}): {seen}; {where}. The rehearsal stopped there", sid)


def _number_pair(v) -> bool:
    return (isinstance(v, list) and len(v) == 2
            and all(isinstance(x, (int, float)) and not isinstance(x, bool) and x > 0 for x in v))


def _agent_did(reply: dict, sid: str, what: str, typed: list[str]) -> None:
    if not reply.get("ok"):
        raise ProbeError(f"step '{sid}': the agent did not perform the {what}: "
                         f"{redact(reply.get('error') or 'no reply', typed)}; the rehearsal stopped there")


def rehearse_step(agent: SimAgent, scn: dict, step: dict, out: Path, udid: str | None = None) -> tuple[dict, dict]:
    """Drive one step in a rehearsal: (what rehearsal.json records for it, the line printed for it).
    Nothing records the text a 'text' step types, only how many characters were typed."""
    sid, do = step["id"], step["do"]
    typed = [s["text"] for s in scn["steps"] if s["do"] == "text"]
    if do == "rest":
        # the full rest, as in a run: the screen a lookup reads must be the one the run will tap
        waited = step["ms"]
        time.sleep(waited / 1000)
        return {"do": "rest", "waited_ms": waited}, {"waited_ms": waited}
    settle = step.get("settle_ms", default_settle_ms(do)) / 1000
    if do == "text":
        reply, round_trip = agent.send({"cmd": "type", "text": step["text"]}, timeout=type_timeout_s(step["text"]))
        _agent_did(reply, sid, "typing", typed)
        if reply.get("typed") != len(step["text"]):
            raise ProbeError(f"step '{sid}': the agent typed {reply.get('typed')} of {len(step['text'])} characters; "
                             "the rehearsal stopped there")
        time.sleep(settle)
        return ({"do": "text", "typed": reply["typed"], "round_trip_ms": round_trip},
                {"typed": reply["typed"]})
    if do == "url":
        # the link is opened here too, and the system's alert confirmed, so that the steps after it are
        # rehearsed on the screen it leads to; nothing of it is replayed (a run opens it again itself)
        opened = sim_open_url(udid, step["url"])
        if not opened["opened"]:
            raise ProbeError(f"step '{sid}': the simulator did not open the link: {opened['error']}; the rehearsal "
                             "stopped there")
        reply, _ = agent.send(sim_alert_command(SIM_ALERT_OPEN, SIM_ALERT_CONFIRM_TIMEOUT_S))
        if not reply.get("ok"):
            raise ProbeError(f"step '{sid}': the agent could not look for the system's alert to confirm the link: "
                             f"{reply.get('error') or 'no reply'}; the rehearsal stopped there")
        time.sleep(settle)
        confirmed = bool(reply.get("tapped"))
        return {"do": "url", "opened": True, "alert_confirmed": confirmed}, {"opened": True, "alert_confirmed": confirmed}
    if do == "tap" and "find" in step:
        found = sim_lookup(agent, scn, step, out)
        reply, round_trip = agent.send({"cmd": "tap", "x": found["center"][0], "y": found["center"][1]})
        _agent_did(reply, sid, "tap", typed)
        time.sleep(settle)
        return ({"do": "tap", "find": step["find"], **found, "tap_round_trip_ms": round_trip},
                {"find": step["find"], "center": found["center"], "reads": found["reads"],
                 "swipes": len(found["swipes"]), "lookup_ms": found["lookup_ms"]})
    command = sim_command(step)
    reply, round_trip = agent.send(command)
    _agent_did(reply, sid, do, typed)
    time.sleep(settle)
    return {"do": do, "command": command, "round_trip_ms": round_trip}, {"ok": True}


# XCTest's activity log names each typed character: "    t =    14.11s Type '5' into Any (First Match)".
TYPED_LINE_RE = re.compile(r"""(\bType\s+)(['"\u2018\u201c])(.*?)(['"\u2019\u201d])""")
AGENT_RESULTS_NOT_KEPT = ("the scenario types text, and the agent's result bundle and derived data log every typed "
                          "character, so they were removed whatever the test run's status")


def scrub_agent_log(path: Path) -> int:
    """Rewrite the agent's log with the quoted text of every "Type '...'" line removed (the rest of the
    line kept). Returns the number of lines changed. A log that cannot be rewritten is deleted, so the
    text is never kept."""
    try:
        text = path.read_text(errors="replace")
    except FileNotFoundError:
        return 0
    changed = 0

    def strip(m):
        nonlocal changed
        changed += 1
        return f"{m.group(1)}{m.group(2)}{SIM_REDACTED}{m.group(4)}"
    lines = [TYPED_LINE_RE.sub(strip, line) for line in text.splitlines(keepends=True)]
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text("".join(lines))
        tmp.replace(path)
    except OSError as exc:
        path.unlink(missing_ok=True)
        print(f"ui-probe: {path} could not be rewritten without the typed text ({exc.strerror or exc}); it was removed",
              file=sys.stderr)
    return changed


def end_rehearsed_app(udid: str, bundle: str, pid: int | None) -> str | None:
    """Terminate the app and wait for the launched process to be gone. None when it is gone (or never
    started), else why the rehearsal fails: a run would measure the rehearsed process. Never raises
    ProbeError: a failure to ask is itself the answer."""
    try:
        xcrun("simctl", "terminate", udid, bundle, check=False)
        if pid is None or sim_app_gone(pid, REHEARSAL_GONE_WAIT_S):
            return None
        what = f"was still running {REHEARSAL_GONE_WAIT_S} s after it was terminated"
    except ProbeError as exc:
        what = f"could not be terminated or checked ({exc})"
    return (f"the rehearsed app's process {pid} {what}, and a run would measure it: end it (`xcrun simctl terminate "
            f"{udid} {bundle}`) and rehearse again")


def sim_app_gone(pid: int, wait_s: float) -> bool:
    """Whether the process is gone, waiting up to wait_s for it to go."""
    deadline = time.monotonic() + wait_s
    while host_cputime(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)
    return True


def rehearse(udid: str, scn: dict, out: Path, emit=None) -> dict:
    """Drive the scenario once in an app process that is then ended (see the section's comment).
    Writes out/rehearsal.json on success, out/failed.json otherwise; out/raw keeps the agent's log,
    agent.json and launch.txt. emit(line) is called with one dict per step."""
    validate_scenario(scn, SIM_PLATFORM, "rehearse")
    refuse_used(out)
    refuse_rehearsal_folder(out)
    below = runs_below(out)
    if below:
        raise ProbeError(f"{out} holds runs ({', '.join(below[:5])}{', ...' if len(below) > 5 else ''}); a rehearsal "
                         "needs a folder of its own")
    # A simulator that is not booted, or an app that is not installed, is a refusal: nothing is left.
    facts = sim_facts(udid, scn["package"])
    out.mkdir(parents=True, exist_ok=True)
    (out / REHEARSAL_MARKER).write_text(json.dumps({"tool": {"name": "ui-probe", "version": VERSION},
                                                    "scenario": scn.get("name"), "started": utc_stamp()}, indent=1))
    previous = {}

    def on_signal(signum, frame):
        for sig in previous:            # one is enough: let the clean-up finish
            signal.signal(sig, signal.SIG_IGN)
        raise Terminated(signum)

    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGHUP):
            previous[sig] = signal.signal(sig, on_signal)
    typing = any(s["do"] == "text" for s in scn["steps"])
    try:
        return _rehearse(udid, scn, out, emit or (lambda line: None), facts)
    except BaseException as exc:
        write_failed(out, exc, scn.get("name"),
                     {"agent_results": {"kept": False, "why": AGENT_RESULTS_NOT_KEPT}} if typing else None)
        raise
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def in_front(agent: SimAgent, where: str, sid: str | None = None) -> int:
    """Ping the agent and require the app in front (state 4), in the words of the run's foreground
    check; returns the state. The error carries the step (sid) for failed.json."""
    reply, _ = agent.send({"cmd": "ping"}, timeout=30)
    state = reply.get("app_state") if reply.get("ok") else None
    if state is None:
        exc = ProbeError(f"the agent did not report which app was in front {where}: "
                         f"{reply.get('error') or 'no reply'}; the rehearsal stopped there")
    elif state != SIM_APP_IN_FRONT:
        exc = ProbeError(f"the app was not in front {where} (its state: {SIM_APP_STATES.get(state, state)}); the "
                         "rehearsal stopped there")
    else:
        return state
    exc.step = sid
    raise exc


def _rehearse(udid: str, scn: dict, out: Path, emit, facts: dict) -> dict:
    bundle = scn["package"]
    identity = app_identity(facts.get("app_container") or None)
    raw = out / "raw"
    raw.mkdir()
    started = utc_stamp()
    done: dict = {}
    agent = pid = screen = None
    typing = any(s["do"] == "text" for s in scn["steps"])
    try:
        failed = None
        try:
            # The app is launched before the agent starts: the agent's first read of the app (the
            # ping reads its state) ties its later tree reads to the process that was running then,
            # and a lookup after a relaunch then waits on the process that is gone. The agent's start
            # overlaps the launch's settle time, which is counted from the launch.
            pid, launch_wall = sim_launch(udid, scn, raw, bundle)
            agent = SimAgent(udid, bundle, rehearsal_idle_s(scn), raw, lookups=True, keep_results=not typing)
            ready = agent.wait_ready(AGENT_START_TIMEOUT_S)
            screen = ready.get("screen")
            if not _number_pair(screen):
                raise ProbeError(f"the input agent's first answer gives no screen size ({screen!r})")
            if sim_has_url(scn):
                # After the launch here, where a measured run does it before the launch: in a rehearsal the agent
                # starts after the app (see above), and the agent is what looks for the alert. So an alert with a
                # Cancel button that the app itself raises in the time the agent takes to start is cancelled in a
                # rehearsal and not in a run (README, known limits).
                stale = sim_clear_stale_alert(agent, raw)
                if not stale["reply"].get("ok"):
                    raise ProbeError("the agent could not look for a system alert left by an earlier link: "
                                     f"{stale['reply'].get('error') or 'no reply'}")
                if stale["cleared"]:
                    emit({"stale_alert": "cancelled a system alert left by an earlier link, before the first step"})
            left = scn.get("launch_settle_ms", 6000) / 1000 - (time.time() - launch_wall)
            if left > 0:
                time.sleep(left)
            in_front(agent, "after the launch")
            for step in scn["steps"]:
                done[step["id"]], line = rehearse_step(agent, scn, step, out, udid)
                done[step["id"]]["app_state"] = in_front(agent, f"at the end of '{step['id']}'", step["id"])
                emit({"step": step["id"], "do": step["do"], **line})
        except BaseException as exc:
            failed = exc
            raise
        finally:
            # The rehearsed process must not be the one a run measures.
            with CleanupShield():
                problem = end_rehearsed_app(udid, bundle, pid)
            if problem is not None:
                if failed is None:
                    raise ProbeError(problem)
                print(f"ui-probe: also: {problem}", file=sys.stderr)
    finally:
        if agent is not None:
            with CleanupShield():
                agent.stop()
                if typing:
                    scrub_agent_log(raw / "agent.log")
    record = {
        "tool": {"name": "ui-probe", "version": VERSION}, "platform": SIM_PLATFORM,
        "scenario": scn["name"], "scenario_digest": scenario_digest(scn), "udid": udid, "device": facts,
        "screen": screen, "app": identity, "started": started, "finished": utc_stamp(),
        "app_ended": {"pid": pid, "gone": True}, "steps": done,
        **({"stale_alert": json.loads(_read_raw(raw, "stale-alert.json"))} if _read_raw(raw, "stale-alert.json") else {}),
        **({"agent_results": {"kept": False, "why": AGENT_RESULTS_NOT_KEPT},
            "agent_log": f"raw/agent.log, with the quoted text of every 'Type' line replaced by {SIM_REDACTED}"}
           if typing else {}),
    }
    tmp = out / (REHEARSAL_FILE + ".tmp")
    tmp.write_text(json.dumps(record, indent=1))
    tmp.replace(out / REHEARSAL_FILE)
    return record


def refuse_rehearsal_folder(out: Path) -> None:
    """Refuse a folder that holds a rehearsal, or lies inside one (a folder above it holds the marker):
    gate, summary and compare never read inside a rehearsal folder, so a run there would vanish."""
    held = [name for name in (REHEARSAL_MARKER, REHEARSAL_FILE) if (out / name).exists()]
    if held:
        raise ProbeError(f"{out} holds a rehearsal or what is left of one ({', '.join(held)}); a run and a rehearsal "
                         "each need a folder of their own")
    above = next((a for a in Path(out).absolute().parents if (a / REHEARSAL_MARKER).exists()), None)
    if above is not None:
        raise ProbeError(f"{out} lies inside the rehearsal folder {above} ({REHEARSAL_MARKER}); a run or a rehearsal "
                         "there would never be read by gate, summary or compare: choose a folder outside it")


def runs_below(out: Path) -> list[str]:
    """The folders at any depth under out that hold a run marker (RUN_MARKERS), relative to out."""
    if not out.is_dir():
        return []
    held = set()
    for name in RUN_MARKERS:
        for hit in out.rglob(name):
            if hit.parent != out:
                held.add(str(hit.parent.relative_to(out)))
    return sorted(held)


def rehearsal_folders(dirs: list[str]) -> list[tuple[Path, str | None]]:
    """Every folder under dirs that `ui-probe rehearse` wrote into (it holds REHEARSAL_MARKER), with None
    when the rehearsal completed, else why it did not."""
    out = []
    for d in dirs:
        root = Path(d)
        for marker in sorted(root.glob(f"**/{REHEARSAL_MARKER}")):
            folder = marker.parent
            if (folder / REHEARSAL_FILE).exists():
                out.append((folder, None))
            else:
                saved = failed_record(folder)
                out.append((folder, saved or f"it did not finish (no {REHEARSAL_FILE} and no {FAILED_RUN})"))
    return out


def load_rehearsal(path: str) -> dict:
    return {"path": str(path), "data": read_json(Path(path))}


def _replayable(done) -> bool:
    """A rehearsal's record of a tap with 'find' that a run can replay: a centre and valid swipes."""
    if not isinstance(done, dict) or done.get("do") != "tap" or not _fraction_pair(done.get("center")):
        return False
    swipes = done.get("swipes")
    num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
    return isinstance(swipes, list) and all(
        isinstance(sw, dict) and _fraction_pair(sw.get("from")) and _fraction_pair(sw.get("to"))
        and num(sw.get("ms")) and 0 < sw["ms"] <= SIM_SWIPE_MS_MAX and num(sw.get("settle_ms")) and sw["settle_ms"] >= 0
        for sw in swipes)


def check_rehearsal(rehearsal: dict, scn: dict, udid: str, max_age_s: int, identity_now: dict, now: float) -> None:
    """Refuse a rehearsal that does not describe what is about to be measured: another scenario (by
    digest), another simulator, a tap with 'find' it holds no position for, an app that changed (or
    whose identity cannot be read, then or now), or one that finished more than max_age_s ago."""
    d, where = rehearsal["data"], rehearsal["path"]

    def refuse(msg):
        raise ProbeError(f"--rehearsal {where}: {msg}")

    if not isinstance(d, dict) or (d.get("tool") or {}).get("name") != "ui-probe" or d.get("platform") != SIM_PLATFORM:
        refuse(f"not a {REHEARSAL_FILE} written by `ui-probe rehearse`")
    digest = scenario_digest(scn)
    if d.get("scenario_digest") != digest:
        refuse(f"it rehearsed another scenario, or this one before it changed (its scenario digest is "
               f"{d.get('scenario_digest')}, this scenario's is {digest}); rehearse this scenario again")
    if d.get("udid") != udid:
        refuse(f"it was made on simulator {d.get('udid')}, not on {udid}; rehearse on this simulator")
    steps = d.get("steps") if isinstance(d.get("steps"), dict) else {}
    missing = [s["id"] for s in sim_find_steps(scn) if not _replayable(steps.get(s["id"]))]
    if missing:
        refuse(f"it holds no position for the tap{'s' if len(missing) > 1 else ''} with 'find' "
               f"{', '.join(repr(m) for m in missing)}; rehearse this scenario again")
    if not _number_pair(d.get("screen")):
        refuse("it holds no screen size")
    then = d.get("app") if isinstance(d.get("app"), dict) else {}
    for key, name in APP_IDENTITY.items():
        a, b = then.get(key), identity_now.get(key)
        if a is None or b is None:
            why = "; ".join((then.get("unknown") or []) if a is None else (identity_now.get("unknown") or []))
            refuse(f"the app's {name} is unknown {'in the rehearsal' if a is None else 'now'}"
                   + (f" ({why})" if why else "") + ", so whether the app changed since cannot be told")
        if a != b:
            refuse(f"the app changed since the rehearsal: its {name} was {a}, is {b}; rehearse again")
    finished = parse_utc_stamp(d.get("finished"))
    if finished is None:
        refuse(f"its finish time {d.get('finished')!r} cannot be read")
    age = now - finished
    if age < 0:
        refuse(f"it finished at {d.get('finished')}, after now; the host's clock has moved back")
    if age > max_age_s:
        refuse(f"it finished {int(age)} s ago ({d.get('finished')}), more than --rehearsal-max-age-s {max_age_s}; "
               "rehearse again")


# --------------------------------------------------------------------------------------------
# the simulator's log store: exact counts of the app's frames, commits and display updates
# --------------------------------------------------------------------------------------------
#
# `log show --signpost` inside the simulator returns, for the app's process and with nothing added
# to the app, one event per frame its Metal layer drew (a drawable), per layer-tree commit, and per
# display update it was woken for. Read once per run, after the last step.

SIM_COUNTS = {   # name: (subsystem, category prefix, signpost name)
    "drawables": ("com.apple.coreanimation", "CAMetalLayer", "ClientDrawable"),
    "commits": ("com.apple.coreanimation", "Transaction", "Commit"),
    "updates": ("com.apple.UIKit", "UpdateCycle", "UpdateSequence"),
}
SIM_LOG_MARGIN_S = 2          # around the run, for the store read
SIM_LOG_FLUSH_WAIT_S = 1      # before the read, for the store to hold the last events
SIM_LOG_TIMEOUT_S = 180
# A rest's recorded frames are excused (a note, not a violation) when the app presented nothing by
# any count, there are at most this many, and none changed any pixel by more than this many grey
# levels at the analysis scale: the encoder refining a still picture after motion. Measured on 523
# still rest windows of one app: 52 held such frames (32 one, 18 two, 2 three), each changing at
# most 17 levels; a 20 pt spinner and a caret changed 165 or more. The count sits at twice the
# largest measured (it was 2 until three frames failed two clean runs): anything an app or the
# render server animates lasts longer than six frames.
SIM_REFINE_FRAMES_MAX = 6
SIM_REFINE_DELTA_MAX = 24
# ... and changed at most this many pixels by ui_video.CHANGE_LEVEL (8) or more, none of them where the
# previous picture was flat. Measured: 17 rest windows on five screens of one app changed 28 to 193
# pixels, more on a screen with more text, none in a flat area; a 1 pt grey hairline that grew
# (largest change 16) changed 6 or 7, of which 4 or 5 in a flat area. So the flat-area count is what
# tells the two apart, and the pixel limit only bounds a wide subtle change: it sits a factor of ten
# above the largest refinement measured, because a limit near it fails the next busier screen.
SIM_REFINE_CHANGED_PX_MAX = 2000
SIM_REFINE_FLAT_PX_MAX = 0
SUBSYSTEM_RE = re.compile(r"[A-Za-z0-9._-]+")


def xcrun_result(*args: str, timeout: int = 60) -> tuple[int | None, str, str]:
    """(exit status, stdout, stderr) of an xcrun call; (None, "", why) when it could not run."""
    try:
        proc = subprocess.run(["xcrun", *args], capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "", f"did not finish within {timeout} s"
    except OSError as exc:
        return None, "", f"cannot run xcrun: {exc.strerror or exc}"
    return proc.returncode, proc.stdout.decode(errors="replace"), proc.stderr.decode(errors="replace")


def log_store_predicate(scn: dict, pid: int) -> str:
    subsystems = sorted({s for s, _, _ in SIM_COUNTS.values()} | ({scn["count_signposts"]["subsystem"]}
                                                                  if "count_signposts" in scn else set()))
    return f"processID == {pid} AND (" + " OR ".join(f'subsystem == "{s}"' for s in subsystems) + ")"


def read_log_store(udid: str, scn: dict, raw: Path, pid: int, launch_wall: float, last_wall: float | None) -> None:
    """One `log show` for the app's process and the whole run, saved as raw/signposts.ndjson with
    what was asked and how it answered in raw/signposts.json. Never fails the run: a store that did
    not answer leaves the counts unknown."""
    time.sleep(SIM_LOG_FLUSH_WAIT_S)
    fmt = "%Y-%m-%d %H:%M:%S"
    start = datetime.datetime.fromtimestamp(launch_wall - SIM_LOG_MARGIN_S).strftime(fmt)
    end = datetime.datetime.fromtimestamp((last_wall or time.time()) + SIM_LOG_MARGIN_S).strftime(fmt)
    predicate = log_store_predicate(scn, pid)
    code, out, err = xcrun_result("simctl", "spawn", udid, "log", "show", "--signpost", "--start", start, "--end", end,
                                  "--style", "ndjson", "--predicate", predicate, timeout=SIM_LOG_TIMEOUT_S)
    (raw / "signposts.ndjson").write_text(out)
    (raw / "signposts.json").write_text(json.dumps({"exit": code, "stderr": err[-2000:], "start": start, "end": end,
                                                    "predicate": predicate, "pid": pid}, indent=1))
    # A simulator has no `logger`, so no marker can be written from outside. Instead a second, small
    # read of every process, from the end of the last step to now: other processes log all the time,
    # so an event stamped after the end of the last step shows the store was read to the end.
    after = last_wall or launch_wall
    asked = ["simctl", "spawn", udid, "log", "show", "--start", datetime.datetime.fromtimestamp(after).strftime(fmt),
             "--end", datetime.datetime.fromtimestamp(time.time() + 1).strftime(fmt), "--style", "ndjson"]
    code2, out2, err2 = xcrun_result(*asked, timeout=SIM_LOG_TIMEOUT_S)
    total = later = 0
    for line in out2.splitlines():
        try:
            e = json.loads(line)
            ts = datetime.datetime.strptime(e["timestamp"], "%Y-%m-%d %H:%M:%S.%f%z").timestamp()
        except (ValueError, KeyError, TypeError):
            continue
        total += 1
        later += ts > after
    (raw / "signposts-tail.json").write_text(json.dumps({"asked": asked, "exit": code2, "stderr": err2[-500:],
                                                         "events": total, "events_after_end": later, "after": after},
                                                        indent=1))


def parse_log_store(raw: Path, pid: int | None) -> tuple[list[tuple[float, dict]] | None, str | None]:
    """The app's signpost events with their times, or (None, why they cannot be used)."""
    meta_text, text = _read_raw(raw, "signposts.json"), _read_raw(raw, "signposts.ndjson")
    if meta_text is None or text is None:
        return None, "the run holds no read of the simulator's log store"
    meta = json.loads(meta_text)
    if meta.get("exit") != 0:
        return None, (f"the simulator's log store did not answer (`log show` exit {meta.get('exit')}: "
                      f"{(meta.get('stderr') or '').strip()[-200:] or 'no message'})")
    events = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except ValueError:
            return None, f"the simulator's log store answered something that is not JSON (line {n})"
        if not isinstance(e, dict):
            return None, f"the simulator's log store answered something that is not a JSON object (line {n})"
        if e.get("eventType") != "signpostEvent" or (pid is not None and e.get("processID") != pid):
            continue
        try:
            ts = datetime.datetime.strptime(e["timestamp"], "%Y-%m-%d %H:%M:%S.%f%z").timestamp()
        except (KeyError, TypeError, ValueError):
            return None, f"the simulator's log store gave an event without a readable time (line {n})"
        events.append((ts, e))
    return events, None


def count_kind(events: list[tuple[float, dict]], kind: str, lo: float, hi: float) -> int:
    subsystem, prefix, name = SIM_COUNTS[kind]
    return sum(1 for ts, e in events if lo <= ts <= hi and e.get("subsystem") == subsystem
               and str(e.get("category") or "").startswith(prefix) and e.get("signpostName") == name)


def count_signposts(events: list[tuple[float, dict]], spec: dict, lo: float, hi: float) -> dict:
    """Per listed name, the app's events of the subsystem whose type is begin or event and whose
    signpost name or message is that name."""
    out = {}
    for name in spec["names"]:
        out[name] = sum(1 for ts, e in events if lo <= ts <= hi and e.get("subsystem") == spec["subsystem"]
                        and e.get("signpostType") in ("begin", "event")
                        and (e.get("signpostName") == name or e.get("eventMessage") == name))
    return out


def store_counts(raw: Path, scn: dict, steps_out: list[dict], result: dict) -> None:
    """Each step's drawables, commits and updates (and the scenario's own signposts) from the log store,
    by the wall-clock window of its two CPU readings, and the same three counts over its recording's
    span. Known for the run only if the store answered, holds a drawable or a commit of the app between
    its launch and the first step (an app that started has drawn or committed something), and a second
    read of every process holds an event stamped after the end of the last step (so the store was read
    to the end)."""
    launch_pid = next((s.get("pid") for s in steps_out if s["id"] == "launch"), None)
    meta = json.loads(_read_raw(raw, "signposts.json") or "{}")
    events, why = parse_log_store(raw, meta.get("pid", launch_pid))
    launch_time = json.loads(_read_raw(raw, "launch-time.json") or "{}").get("wall")
    windows, spans = {}, {}
    for st in steps_out:
        if st["id"] == "launch":
            continue
        data = json.loads(_read_raw(raw, f"step-{st['id']}.json") or "{}")
        windows[st["id"]] = (data.get("wall_before"), data.get("wall_after"))
        spans[st["id"]] = (data.get("rec_wall_start"), data.get("rec_wall_end"))
    first = next(iter(windows.values()), (None, None))[0]
    tail = json.loads(_read_raw(raw, "signposts-tail.json") or "null")
    if why is None:
        if launch_time is None or first is None:
            why = "the run holds no time of its launch or of its first step, so the log store's events cannot be placed"
        elif not (count_kind(events, "drawables", launch_time, first) or count_kind(events, "commits", launch_time, first)):
            why = ("the log store holds no drawable or commit of the app between its launch and the first step; an app "
                   "that started has drawn or committed something, so the store was not read")
        elif tail is None or tail.get("exit") != 0 or not tail.get("events_after_end"):
            why = ("a second read of the log store, of every process, found no event after the end of the last step "
                   + ("(it did not answer)" if tail is None or tail.get("exit") != 0 else "") +
                   ", so the store may not have been read to the end").replace(" ,", ",")
    spec = scn.get("count_signposts")
    sign_why = why
    unmatched = []
    if spec is not None and why is None:
        if not any(e.get("subsystem") == spec["subsystem"] for _, e in events):
            sign_why = (f"the run holds no event of subsystem '{spec['subsystem']}' from the app, launch included: is the "
                        "app's tracing switched on?")
        else:
            whole = count_signposts(events, spec, float("-inf"), float("inf"))
            unmatched = [name for name, n in whole.items() if n == 0]
    result["log_store"] = {"known": why is None, "reason": why, "events": None if events is None else len(events),
                           "tail": tail}
    if spec is not None:
        result["log_store"]["signposts_known"] = sign_why is None
        result["log_store"]["signposts_reason"] = sign_why
        result["log_store"]["signposts_unmatched"] = unmatched
    for st in steps_out:
        if st["id"] == "launch":
            continue
        lo, hi = windows.get(st["id"], (None, None))
        placed = why is None and lo is not None and hi is not None
        for kind in SIM_COUNTS:
            st[kind] = count_kind(events, kind, lo, hi) if placed else None
        s0, s1 = spans.get(st["id"], (None, None))
        st["recording_span"] = ({kind: count_kind(events, kind, s0, s1) for kind in SIM_COUNTS}
                                if why is None and s0 is not None and s1 is not None else None)
        if spec is not None:
            counted = (count_signposts(events, spec, lo, hi) if placed and sign_why is None
                       else {name: None for name in spec["names"]})
            st["signposts"] = {name: (None if name in unmatched else n) for name, n in counted.items()}


def sim_command(step: dict, resolved: dict | None = None) -> dict:
    """The agent command for an input step (the token is added when it is sent); a tap with 'find' goes
    where the rehearsal found its element (resolved)."""
    if step["do"] == "tap" and "find" in step:
        return {"cmd": "tap", "x": resolved["x"], "y": resolved["y"]}
    if step["do"] == "tap":
        return {"cmd": "tap", "x": step["x"], "y": step["y"]}
    return {"cmd": "swipe", "from": step["from"], "to": step["to"], "ms": step.get("ms", 300)}


def sim_recording_counts(mp4: Path, workdir: Path, step: dict | str, name: str) -> dict:
    """The existing recording analysis on one step's recording: the whole file is the step's window,
    and the first frame (the screen as recording began) is left out. Counts of the frames after it
    that changed the app's area (with the encoder noise left out), changed it by noise only, changed
    only the system bars, or changed nothing, and the step's signature: a tap is judged as a state
    change, a swipe as motion unless its 'judge' is 'state', as on Android."""
    sys.path.insert(0, str(HERE))
    import ui_video
    if isinstance(step, str):
        step = {"id": step, "do": "rest"}
    sid = step["id"]
    mini_step = {"id": sid, "do": step["do"], "ms": 1}
    if "judge" in step:
        mini_step["judge"] = step["judge"]
    regions = (step.get("allow") or {}).get("regions") if step["do"] == "rest" else None
    if regions:
        mini_step["allow"] = {"regions": regions}
    mini = {"name": name, "steps": [mini_step]}
    # The recording has no per-frame device clock: its own frame times are used, placed from 0.
    marks = {"T0": -0.3, f"{sid}.start": -1e12, f"{sid}.end": 1e12}
    facts = {"height": 1000, "stable_insets_top_bottom": list(SIM_BARS_PERMILLE)}
    try:
        # Every frame that changed anything counts on a simulator, so those are the ones placed in regions.
        res = ui_video.analyze(mp4, workdir, [], marks, mini, facts, **({"locate": "changed"} if regions else {}))
    except ui_video.UnusableRecording as exc:
        return {"error": str(exc)}
    except ui_video.VideoError as exc:
        raise ProbeError(f"the recording of '{sid}' cannot be analysed: {exc}") from exc
    seg = next(s for s in res["steps"] if s["id"] == sid)
    frames, region, identical = seg["video_frames"], seg["app_region_frames"], seg["identical_frames"]
    real = seg.get("app_changed_frames_real", region)
    sheet = workdir / "sheets" / f"{sid}.png"
    return {"frames": frames, "changed_app": real, "changed_app_noise": region - real, "identical": identical,
            "bars_only": max(0, frames - region - identical), "signature": seg["signature"],
            "largest_change": seg.get("largest_change"), "most_changed_px": seg.get("most_changed_px"),
            "most_flat_changed_px": seg.get("most_flat_changed_px"),
            "sheet": str(sheet.relative_to(workdir.parent.parent)) if sheet.exists() else None,
            **{k: seg[k] for k in ("regions", "rest_changed_frames", "rest_changed_frame_indices", "regions_error")
               if k in seg}}


def analyze_sim(out: Path, scn: dict, cap: dict) -> dict:
    raw = out / "raw"
    launched = _read_raw(raw, "launch.txt")
    if launched is None:
        raise ProbeError(f"{out}: raw/launch.txt is missing; the capture is incomplete")
    launch_pid = single_pid(_read_raw(raw, "pid-launch.txt"))
    m = re.search(rf"^{re.escape(scn['package'])}:\s*(\d+)\s*$", launched.strip(), re.M)
    steps_out = [{"id": "launch", "kind": "launch", "pid": launch_pid, "process_missing": launch_pid is None,
                  "process_restarted": False}]
    prev = launch_pid
    for step in scn["steps"]:
        sid = step["id"]
        text = _read_raw(raw, f"step-{sid}.json")
        if text is None:
            continue           # the gate reports a step the run holds no record of
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProbeError(f"{out}: raw/step-{sid}.json is not valid JSON: {exc}") from exc
        pid_now = single_pid(_read_raw(raw, f"pid-{sid}.txt"))
        before, after = parse_cputime(data.get("cputime_before")), parse_cputime(data.get("cputime_after"))
        window = data.get("window_s")
        restarted = bool(pid_now is not None and prev is not None and pid_now != prev)
        cpu_s = round(after - before, 2) if None not in (before, after) and not restarted else None
        cpu_problem = None
        if cpu_s is not None and cpu_s < 0:
            # The CPU time went down: the pid is held by another process now. Unreadable, not negative.
            cpu_s, cpu_problem = None, f"the CPU time went down between the two readings ({before} s, then {after} s)"
        cpu_pct = round(100 * cpu_s / window, 2) if cpu_s is not None and window and window > 0 else None
        mp4 = raw / f"rec-{sid}.mp4"
        if mp4.exists() and mp4.stat().st_size > 0:
            recording = sim_recording_counts(mp4, out / "video" / sid, step, scn["name"])
            if recording.get("sheet"):
                (out / "sheets").mkdir(exist_ok=True)
                shutil.copyfile(out / recording["sheet"], out / "sheets" / f"{sid}.png")
                recording["sheet"] = f"sheets/{sid}.png"
        else:
            recording = None
        steps_out.append({
            "id": sid, "kind": step["do"], "pid": pid_now, "process_missing": pid_now is None,
            "process_restarted": restarted, "window_s": window, "cpu_s": cpu_s, "cpu_pct": cpu_pct,
            "recorder_exit": data.get("recorder_exit"), "recorder_killed": data.get("recorder_killed"),
            "recorder_alive_at_end": data.get("recorder_alive_at_end"), "cpu_problem": cpu_problem,
            "recording": recording,
            **({"signature": (recording or {}).get("signature")} if step["do"] != "rest" else {}),
            **({} if "app_state" not in data else {
                "app_state": data.get("app_state"),
                "agent": {"command": data.get("command"), "reply": data.get("reply"),
                          "round_trip_ms": data.get("round_trip_ms"), "end_ping": data.get("end_ping")}}),
            **({} if "resolved" not in data else {"resolved": data["resolved"]}),
            **({} if "url_open" not in data else {"url_open": data["url_open"]})})
        prev = pid_now if pid_now is not None else prev
    result = {
        "tool": cap.get("tool", {"name": "ui-probe", "version": VERSION}), "platform": SIM_PLATFORM,
        "scenario": scn["name"], "package": scn["package"], "device": cap["device"], "host": cap["host"],
        "launch": {"pid": int(m.group(1)) if m else None, "total_time_ms": None,
                   "error": None if m else f"the launch printed no process id: {launched.strip()[:200]!r}"},
        "steps": steps_out, "frame_sources": ["recording"], "video_requested": True, "trace_requested": False,
        "not_checked": list(SIM_NOT_CHECKED),
    }
    store_counts(raw, scn, steps_out, result)
    request_counts(raw, steps_out, result, SIM_PLATFORM, scn, bool(cap.get("requests_log")))
    result["frame_sources"] = ["recording"] + (["log store"] if result["log_store"]["known"] else [])
    result["launch_env_dropped"] = cap.get("launch_env_dropped") or []
    if sim_has_url(scn):
        stale = _read_raw(raw, "stale-alert.json")
        result["stale_alert"] = json.loads(stale) if stale else None
    if sim_needs_agent(scn):
        agent_text = _read_raw(raw, "agent.json")
        result["agent"] = json.loads(agent_text) if agent_text else None
        result["not_checked"] = [c for c in SIM_NOT_CHECKED if c != "which app is in front"]
    tmp = out / "run.json.tmp"
    tmp.write_text(json.dumps(result, indent=1))
    tmp.replace(out / "run.json")
    return result


def gate_sim_run(r: dict, scn: dict, opts: dict) -> dict:
    """A simulator run: CPU at rest, the log store's exact counts (drawables, commits, display
    updates) and the recording. The status bar is frozen for the run, so nothing but the app has a
    reason to make the display compose: every recorded frame after the first counts against the app,
    whatever its class, unless the counts are known, all 0 for the window, and the frames are at most
    SIM_REFINE_FRAMES_MAX that changed no pixel by more than SIM_REFINE_DELTA_MAX grey levels (the
    encoder refining a still picture). A recorder that had stopped before the window ended fails the
    recording check."""
    violations, notes = [], []

    def fail(check, sid, message, **extra):
        violations.append({"check": check, "step": sid, "message": message, **extra})

    uses_agent = "agent" in r
    not_checked = [c for c in SIM_NOT_CHECKED if not (uses_agent and c == "which app is in front")]
    store = r.get("log_store") or {"known": False, "reason": "the run holds no read of the simulator's log store"}
    notes.append("iOS Simulator: not checked, since a simulator does not give them: " + ", ".join(not_checked)
                 + ("; a tap's or a swipe's frames, counts and CPU are information, never gated" if uses_agent else ""))
    if not store.get("known"):
        notes.append(f"the drawables, commits and display updates are unknown for every step: {store.get('reason')}; "
                     "frames at rest are counted from the recording only")
    spec = scn.get("count_signposts")
    if r.get("launch_env_dropped"):
        notes.append(f"{', '.join(r['launch_env_dropped'])} in ui-probe's own environment were not passed to the app: "
                     "the scenario's launch_env is the only source")
    resolved = [s["resolved"] for s in r.get("steps", []) if s.get("resolved")]
    if resolved:
        notes.append(f"{len(resolved)} tap{'s' if len(resolved) != 1 else ''} by 'find' went to positions found by the "
                     f"rehearsal {resolved[0].get('rehearsal')} (finished {resolved[0].get('rehearsal_finished')}); the "
                     "measured run looked nothing up")
    if uses_agent:
        stop = (r.get("agent") or {}).get("stop")
        if stop is None:
            fail("agent", None, "the run holds no record of how the agent's test run ended")
        elif stop.get("terminated") or stop.get("killed"):
            notes.append("the agent's test run did not end after 'quit' and was ended by the tool ("
                         + ("SIGKILL" if stop.get("killed") else "SIGTERM") + "); its exit status says nothing")
        elif stop.get("exit") != 0:
            fail("agent", None, f"the agent's test run ended with status {stop.get('exit')}: XCTest recorded an issue "
                 "(raw/agent.log, raw/agent.xcresult)", observed=stop.get("exit"))
    launch = r.get("launch") or {}
    if launch.get("error"):
        fail("launch", "launch", f"the app did not start: {launch['error']}")
    if sim_has_url(scn):
        stale = r.get("stale_alert")
        if stale is None:
            fail("input", "launch", "the run holds no record of the check for a system alert left by an earlier link")
        elif not (stale.get("reply") or {}).get("ok"):
            fail("input", "launch", "the agent could not look for a system alert left by an earlier link, which would "
                 f"take the run's taps: {(stale.get('reply') or {}).get('error') or 'no reply'}")
        elif stale.get("cleared"):
            notes.append("a system alert left by an earlier link (opened, never confirmed) was cancelled before the "
                         "launch")
    records = {s["id"]: s for s in r.get("steps", [])}
    by_id = {s["id"]: s for s in scn["steps"]}
    for sid in ["launch"] + [s["id"] for s in scn["steps"]]:
        s = records.get(sid)
        if s is None:
            fail("missing-step", sid, f"the run holds no record of step '{sid}'")
            continue
        step = by_id.get(sid)
        want = "launch" if step is None else step["do"]
        if s.get("kind") != want:
            fail("scenario", sid, f"step '{sid}' is a {s.get('kind')} in the run but a {want} in the scenario")
            continue
        if s.get("process_missing"):
            fail("process", sid, f"the app's process was not running at the end of '{sid}'")
        if s.get("process_restarted"):
            fail("process", sid, "the app's process changed during the scenario (a crash or a kill)")
        if uses_agent and want != "launch":
            # The agent's ping at the end of every step reports the app's state (4: in front).
            state = s.get("app_state")
            if state is None:
                fail("foreground", sid, f"the agent did not report which app was in front at the end of '{sid}'")
            elif state != SIM_APP_IN_FRONT:
                fail("foreground", sid, f"the app was not in front at the end of '{sid}' (its state: "
                     f"{SIM_APP_STATES.get(state, state)})", observed=state)
        if want in SIM_INPUT_KINDS or want == "url":
            replayed = (s.get("resolved") or {}).get("swipes") or []
            for n, sw in enumerate(replayed, 1):
                sw_reply = sw.get("reply") or {}
                if not sw_reply.get("ok"):
                    fail("input", sid, f"the agent did not perform swipe {n} of {len(replayed)} replayed from the "
                         f"rehearsal before the tap: {sw_reply.get('error') or 'no reply'}")
            if want == "url":
                opened = s.get("url_open") or {}
                reply = (s.get("agent") or {}).get("reply") or {}
                if not opened.get("opened"):
                    fail("input", sid, f"the link was not opened: {opened.get('error') or 'the run holds no record of it'}")
                elif not reply.get("ok"):
                    fail("input", sid, "the agent could not look for the system's alert to confirm the link: "
                         f"{reply.get('error') or 'no reply'}")
                elif not opened.get("alert_confirmed"):
                    # Not a failure: a link may open without asking. But if the system asked after the wait, the
                    # alert is still up and the app has not opened the link.
                    notes.append(f"{sid}: no system alert was confirmed within the {SIM_ALERT_CONFIRM_TIMEOUT_S} s "
                                 "wait; if the system asked later, the app has not opened the link and the alert is "
                                 "still above it")
            else:
                reply = (s.get("agent") or {}).get("reply") or {}
                if not reply.get("ok"):
                    fail("input", sid, f"the agent did not perform the {want}: {reply.get('error') or 'no reply'}")
            rec = s.get("recording")
            if rec is None or rec.get("error"):
                fail("recording", sid, "no recording was saved" if rec is None else
                     f"the recording could not be used: {rec['error']}")
            else:
                if s.get("recorder_alive_at_end") is not True:
                    fail("recording", sid, "the recorder had stopped before the window ended (or whether it was still "
                         "running was not recorded); its recording may end early")
                if s.get("recorder_exit") != 0:
                    fail("recording", sid, f"the recorder exited with status {s.get('recorder_exit')}; the recording may "
                         "be incomplete", observed=s.get("recorder_exit"))
            expect = step.get("expect") or {}
            gate_signposts(fail, sid, s, expect, spec, store)
            if want == "tap" and "find" in step:
                # A replayed tap goes to a position, not to an element: one that changed nothing on the
                # screen probably missed its target. An approved signature of '-' says nothing is expected.
                got = None if rec is None or rec.get("error") else rec.get("signature")
                if rec is None or rec.get("error"):
                    fail("input", sid, "whether the replayed tap reached its target cannot be told: "
                         + ("no recording was saved" if rec is None else f"the recording could not be used: {rec['error']}"))
                elif got is None:
                    notes.append(f"{sid}: the replayed tap's signature is unknown, so whether it changed anything on the "
                                 "screen was not checked")
                elif got == "-" and expect.get("signature") != "-":
                    fail("input", sid, "the replayed tap changed nothing on the screen (signature '-'), so it probably "
                         "missed its target; give the step expect.signature '-' if nothing is meant to change",
                         observed=got)
            gate_requests(fail, notes, r, sid, s, step, opts, rest=False)
            if "signature" in expect:
                got = None if rec is None or rec.get("error") else rec.get("signature")
                if got is None:
                    fail("signature", sid, "no recording, so the signature could not be read")
                elif got != expect["signature"]:
                    fail("signature", sid, f"signature '{got}', approved '{expect['signature']}'",
                         observed=got, allowed=expect["signature"])
            continue
        if want == "launch":
            gate_requests(fail, notes, r, sid, s, launch_budget(scn), opts, rest=False)
        if want != "rest":
            continue
        allow = step.get("allow") or {}
        reason = allow.get("reason")
        frames_max = allow.get("frames_max", opts["rest_frames_max"])
        changed_max = allow.get("changed_frames_max", 0)
        cpu_max = allow.get("cpu_pct_max", opts["rest_cpu_pct_max"])
        by = lambda key: f" by the step's allowance ({reason})" if key in allow else ""
        extra = lambda key: {"limit_from": "allowance"} if key in allow else {}
        gate_signposts(fail, sid, s, step.get("expect") or {}, spec, store)
        gate_requests(fail, notes, r, sid, s, step, opts, rest=True)
        known = s.get("drawables") is not None and s.get("commits") is not None and s.get("updates") is not None
        if known:
            presented = s["drawables"] + s["commits"]
            if presented > frames_max:
                fail("rest-frames", sid, f"{presented} frames presented at rest ({s['drawables']} drawables, "
                     f"{s['commits']} commits; log store count), allowed {frames_max}" + by("frames_max"),
                     observed=presented, allowed=frames_max, source="log store", **extra("frames_max"))
            elif "frames_max" in allow and presented > opts["rest_frames_max"]:
                notes.append(f"{sid}: {presented} frames presented at rest (log store count), above the limit of "
                             f"{opts['rest_frames_max']} that would otherwise apply; passed on the step's allowance of "
                             f"{frames_max} ({reason})")
            updates_max = allow.get("updates_max", opts["rest_updates_max"])
            if s["updates"] > updates_max:
                fail("rest-updates", sid, f"the app was woken for {s['updates']} display updates at rest, allowed "
                     f"{updates_max}" + by("updates_max"), observed=s["updates"], allowed=updates_max,
                     **extra("updates_max"))
            elif "updates_max" in allow and s["updates"] > opts["rest_updates_max"]:
                notes.append(f"{sid}: the app was woken for {s['updates']} display updates at rest, above the limit of "
                             f"{opts['rest_updates_max']} that would otherwise apply; passed on the step's allowance of "
                             f"{updates_max} ({reason})")
        rec = s.get("recording")
        code = s.get("recorder_exit")
        if rec is None or rec.get("error"):
            why = "no recording was saved" if rec is None else f"the recording could not be used: {rec['error']}"
            fail("recording", sid, why)
            if not known:
                fail("rest-frames", sid, f"cannot count frames: {why}, and the log store's counts are unknown")
        else:
            if s.get("recorder_alive_at_end") is not True:
                fail("recording", sid, "the recorder had stopped before the window ended (or whether it was still "
                     "running was not recorded); its recording may end early and look clean")
            if code != 0:
                fail("recording", sid, f"the recorder exited with status {code}"
                     + (" after it was killed" if s.get("recorder_killed") else "") + "; the recording may be incomplete",
                     observed=code)
            # With the status bar frozen, nothing but the app has a reason to make the display compose,
            # and a simulator gives no second witness: every recorded frame after the first counts,
            # whatever its class. The classes are named for the reader; they decide nothing.
            total = rec["frames"]
            changed = total - rec["identical"]
            regions = allow.get("regions")
            in_regions = 0
            if regions:
                # A frame whose every change lies inside the step's regions counts against those
                # regions, not against the rest: neither as a frame recorded nor as a frame changed.
                rest_n, _why = region_counts(regions, rec)
                if rest_n is not None:
                    in_regions = max(0, changed - rest_n)
                gate_regions(fail, notes, sid, regions, rec)
                total, changed = total - in_regions, changed - in_regions
            classes = (f"{rec['changed_app']} changed the app's area, {rec['changed_app_noise']} changed it by "
                       f"encoder noise only, {rec['bars_only']} changed only the system bars, {rec['identical']} "
                       "changed nothing")
            largest = rec.get("largest_change")
            span = s.get("recording_span") or {}
            span_zero = known and all(span.get(kind) == 0 for kind in SIM_COUNTS)
            refined = (span_zero and s["drawables"] == 0 and s["commits"] == 0 and s["updates"] == 0
                       and total <= SIM_REFINE_FRAMES_MAX and largest is not None and largest <= SIM_REFINE_DELTA_MAX
                       and (rec.get("most_changed_px") is not None and rec["most_changed_px"] <= SIM_REFINE_CHANGED_PX_MAX)
                       and (rec.get("most_flat_changed_px") is not None
                            and rec["most_flat_changed_px"] <= SIM_REFINE_FLAT_PX_MAX))
            if refined:
                if total:
                    notes.append(f"{sid}: {total} frames recorded at rest ({classes}) are not held against the app: it "
                                 "presented nothing by any count over the whole recording (0 drawables, 0 commits, 0 "
                                 f"display updates), no pixel changed by more than {largest:g} levels on any colour "
                                 f"channel (the limit is {SIM_REFINE_DELTA_MAX}), {rec['most_changed_px']} pixels "
                                 f"changed at most (the limit is {SIM_REFINE_CHANGED_PX_MAX}) and none where the picture "
                                 "was flat: the encoder refining a still picture. Look at them on "
                                 f"sheets/{sid}.png and with `ui-probe zoom <run-dir> <a> <b> --step {sid}`")
                total = changed = 0
                rec = {**rec, "identical": 0}
            if in_regions:
                classes += f"; {in_regions} more changed only inside the step's regions"
            if total > frames_max:
                fail("rest-frames", sid, f"{total} frames recorded at rest ({classes}), allowed {frames_max}"
                     + by("frames_max"), observed=total, allowed=frames_max, source="recording", **extra("frames_max"))
            elif "frames_max" in allow and total > opts["rest_frames_max"]:
                notes.append(f"{sid}: {total} frames recorded at rest ({classes}), above the limit of "
                             f"{opts['rest_frames_max']} that would otherwise apply; passed on the step's allowance of "
                             f"{frames_max} ({reason})")
            unchanged_max = allow.get("unchanged_frames_max", 0)
            if rec["identical"] > unchanged_max:
                fail("rest-video", sid, f"{rec['identical']} frames were composed at rest that changed nothing on screen"
                     + (f", allowed {unchanged_max}{by('unchanged_frames_max')}" if "unchanged_frames_max" in allow else ""),
                     observed=rec["identical"],
                     **({"allowed": unchanged_max} if "unchanged_frames_max" in allow else {}),
                     **extra("unchanged_frames_max"))
            elif "unchanged_frames_max" in allow and rec["identical"] > 0:
                notes.append(f"{sid}: {rec['identical']} recorded frames changed nothing on screen at rest, above the "
                             f"limit of 0 that would otherwise apply; passed on the step's allowance of {unchanged_max} "
                             f"({reason})")
            if changed > changed_max:
                fail("rest-video", sid, f"the screen changed at rest: {changed} recorded frames changed pixels "
                     f"({rec['changed_app']} in the app's area, {rec['changed_app_noise']} by encoder noise only, "
                     f"{rec['bars_only']} in the system bars only)"
                     + (f", allowed {changed_max}{by('changed_frames_max')}" if "changed_frames_max" in allow else ""),
                     observed=changed, **({"allowed": changed_max} if "changed_frames_max" in allow else {}),
                     **extra("changed_frames_max"))
            elif "changed_frames_max" in allow and changed > 0:
                notes.append(f"{sid}: {changed} recorded frames changed pixels at rest, above the limit of 0 that would "
                             f"otherwise apply; passed on the step's allowance of {changed_max} ({reason})")
        pct = s.get("cpu_pct")
        if pct is None:
            fail("rest-cpu", sid, "CPU at rest could not be read ("
                 + (s.get("cpu_problem") or "the process was gone, or `ps` printed nothing")
                 + "); on a simulator it is one of only two witnesses")
        elif pct > cpu_max:
            fail("rest-cpu", sid, f"{pct}% of one core used at rest ({s.get('cpu_s')} s of CPU in {s.get('window_s')} s), "
                 f"allowed {cpu_max}%" + by("cpu_pct_max"), observed=pct, allowed=cpu_max, **extra("cpu_pct_max"))
        elif "cpu_pct_max" in allow and pct > opts["rest_cpu_pct_max"]:
            notes.append(f"{sid}: {pct}% of one core used at rest, above the limit of {opts['rest_cpu_pct_max']}% that "
                         f"would otherwise apply; passed on the step's allowance of {cpu_max}% ({reason})")
    for sid in records:
        if sid != "launch" and sid not in by_id:
            notes.append(f"{sid}: in the run but not in the scenario; not checked")
    notes.extend(request_notes(r))
    return {"verdict": "FAIL" if violations else "PASS", "violations": violations, "notes": notes}


def region_counts(declared: list | None, rec: dict | None) -> tuple[int | None, str | None]:
    """For a rest with allow.regions: (the frames its record counts against the rest, None) when the
    record's region report was made for these regions, else (None, why it cannot be used)."""
    if not declared:
        return None, None
    rec = rec or {}
    if rec.get("regions_error"):
        return None, rec["regions_error"]
    report = rec.get("regions")
    if report is None or "rest_changed_frames" not in rec:
        return None, "the run holds no report of its regions (an analysis from before 0.9.0, or one made with " \
                     "another scenario): run `ui-probe analyze` with this scenario"
    if [list(r.get("region") or []) for r in report] != [list(r["region"]) for r in declared]:
        return None, "the run's region report was made for other regions: run `ui-probe analyze --scenario` with " \
                     "this scenario"
    return rec["rest_changed_frames"], None


def gate_regions(fail, notes: list, sid: str, declared: list, rec: dict, aligned: bool = True,
                 what: str = "recorded frames") -> None:
    """Each declared region against its own limit (check 'rest-region'). A region with no usable report
    fails; region_counts says why."""
    _rest, why = region_counts(declared, rec)
    if why is not None:
        fail("rest-region", sid, f"the step's regions could not be checked: {why}")
        return
    for k, (reg, rep) in enumerate(zip(declared, rec["regions"]), 1):
        got, limit = rep["changed_frames"], reg["changed_frames_max"]
        where = "[" + ", ".join(f"{v:g}" for v in reg["region"]) + "]"
        when = ("" if not got else f"; from {rep['first_ms']:.0f} to {rep['last_ms']:.0f} ms after the step's start, "
                f"{rep['times']} time{'s' if rep['times'] != 1 else ''}, the longest {rep['longest_ms']:.0f} ms")
        if got > limit:
            message = (f"region {k} {where} changed in {got} {what}, allowed {limit} by the region's allowance "
                       f"({reg['reason']}){when}")
            if aligned:
                fail("rest-region", sid, message, observed=got, allowed=limit, region=reg["region"])
            else:
                notes.append(f"{sid}: {message} (the recording is not aligned with the step marks, so this is not a "
                             "violation)")
        elif got:
            notes.append(f"{sid}: region {k} {where} changed in {got} {what}, within its allowance of {limit} "
                         f"({reg['reason']}){when}")


def gate_signposts(fail, sid: str, s: dict, expect: dict, spec: dict | None, store: dict) -> None:
    """expect.signposts_max against the step's counts of the app's own signposts (check 'signposts')."""
    for name, limit in (expect.get("signposts_max") or {}).items():
        got = (s.get("signposts") or {}).get(name)
        if got is None and name in (store.get("signposts_unmatched") or []):
            fail("signposts", sid, f"'{name}' was not counted: it occurs nowhere in the run, launch included. A name is "
                 "matched by exact equality with the signpost's name or its message")
        elif got is None:
            why = store.get("signposts_reason") or store.get("reason") or "the run holds no count of it"
            fail("signposts", sid, f"'{name}' was not counted: {why}")
        elif got > limit:
            fail("signposts", sid, f"{got} '{name}' signposts, approved at most {limit}", observed=got, allowed=limit)


def sim_run_row(path: str, r: dict) -> dict:
    row = {"run": Path(path).name, "host_load1": r["host"]["load1_before"]}
    for s in r["steps"]:
        if "requests" in s:
            # a count of its own key, like every simulator value; the launch window has one too
            row[f"{s['id']}.sim_requests"] = (s["requests"] or {}).get("count")
        if s["kind"] == "launch":
            continue
        sid, rec = s["id"], s.get("recording") or {}
        if s["kind"] == "url":
            row[f"{sid}.sim_alert_confirmed"] = (s.get("url_open") or {}).get("alert_confirmed")
        if s["kind"] in SIM_INPUT_KINDS or s["kind"] == "url":
            agent = s.get("agent") or {}
            row[f"{sid}.sim_signature"] = s.get("signature")
            row[f"{sid}.sim_round_trip_ms"] = agent.get("round_trip_ms")
            row[f"{sid}.sim_agent_ms"] = (agent.get("reply") or {}).get("elapsed_ms")
        # Keys of their own: a simulator's CPU is in seconds (Android's in ticks), and its recorded
        # frames are those of a whole per-step recording, every one of which counts.
        for kind in SIM_COUNTS:
            row[f"{sid}.sim_{kind}"] = s.get(kind)
        for name, value in (s.get("signposts") or {}).items():
            row[f"{sid}.sim_signposts.{name}"] = value
        row[f"{sid}.sim_window_s"] = s.get("window_s")
        row[f"{sid}.sim_cpu_s"] = s.get("cpu_s")
        row[f"{sid}.sim_cpu_pct"] = s.get("cpu_pct")
        row[f"{sid}.sim_recorded_frames"] = rec.get("frames")
        row[f"{sid}.sim_changed_app_frames"] = rec.get("changed_app")
        row[f"{sid}.sim_noise_frames"] = rec.get("changed_app_noise")
        row[f"{sid}.sim_bars_only_frames"] = rec.get("bars_only")
        row[f"{sid}.sim_identical_frames"] = rec.get("identical")
    return row


def _read_raw(raw: Path, name: str) -> str | None:
    p = raw / name
    return p.read_text(errors="replace") if p.exists() else None


def launch_end_mark(scn: dict) -> str:
    """The launch window ends where the first step begins: at its element lookup if it has one, so
    that the lookup lies in no window."""
    first = scn["steps"][0]
    return f"{first['id']}.find_start" if "find" in first else f"{first['id']}.start"


def find_record(text: str | None, step: dict, facts: dict, baseline: dict | None) -> dict | None:
    """What run.json says about a step's element lookup: what was looked for, the centre tapped (in
    pixels and as fractions of the screen), the bounds, the package of the matched element, the
    matches, the reads and the time it took, and when the baseline was read. None when the lookup
    left no record."""
    rec = parse_find(text)
    if rec is None:
        return None
    sid = step["id"]
    display = rec.get("display") if rec.get("display") and len(rec["display"]) == 2 else None
    centre, w, h = rec.get("centre"), *(display or (facts.get("width"), facts.get("height")))
    return {
        **{k: step["find"][k] for k in ("text", "desc", "nth") if k in step["find"]},
        "status": rec.get("status"),
        "centre_px": centre,
        "centre": [round(centre[0] / w, 4), round(centre[1] / h, 4)] if centre and len(centre) == 2 and w and h else None,
        "bounds_px": rec.get("bounds"),
        "package": rec.get("package"),
        "matches": rec.get("matches"), "visible": rec.get("visible"), "reads": rec.get("reads"),
        "swipes": rec.get("swipes"), "display_px": display, "lookup_ms": rec.get("elapsed_ms"),
        "settle_ms": step.get("find_settle_ms", FIND_SETTLE_MS),
        # The uptime of the baseline's CPU reading: the step's counts start there.
        "baseline_s": baseline.get("uptime") if baseline else None,
        "tree": f"raw/ui-{sid}.xml",
    }


def analyze_run(out: Path, scn: dict | None = None) -> dict:
    """Everything below reads only files already on disk, so analysis can be redone without a device."""
    cap = read_json(out / "capture.json")
    scn = scn or cap.get("scenario")
    if scn is None:
        raise ProbeError(f"{out}: the capture holds no scenario; pass --scenario")
    platform = cap.get("platform", ANDROID)
    validate_scenario(scn, platform)
    if platform == FILM_PLATFORM:
        film_source(out, cap)       # a film that moved or changed is refused before anything is removed
    # A failed analysis must not leave an earlier result standing for gate, summary and compare.
    result_path = out / "run.json"
    if result_path.exists():
        result_path.unlink()
    if platform == SIM_PLATFORM:
        return analyze_sim(out, scn, cap)
    if platform == FILM_PLATFORM:
        return analyze_film_run(out, scn, cap)
    facts, trace, video = cap["device"], cap["trace"], cap["video"]
    raw = out / "raw"
    marks_text = _read_raw(raw, "marks.txt")
    if marks_text is None:
        raise ProbeError(f"{out}: raw/marks.txt is missing; the capture is incomplete")
    marks = parse_marks(marks_text)

    order = ["launch"] + [s["id"] for s in scn["steps"]]
    by_id = {s["id"]: s for s in scn["steps"]}

    def table(prefix, parse, suffix=".txt"):
        out_ = {}
        for sid in order:
            text = _read_raw(raw, f"{prefix}-{sid}{suffix}")
            if text is not None:
                out_[sid] = parse(text)
        return out_

    gfx = table("gfx", parse_gfx)
    cpu_end = table("cpu", parse_cpu)
    cpu_after = table("cpu", parse_cpu, ".after.txt")
    pids = table("pid", str.strip)
    sf = table("sf", parse_timestats)
    # The baseline readings taken after an element lookup, for the steps that have one.
    gfx_base = table("gfx", parse_gfx, ".base.txt")
    cpu_base = table("cpu", parse_cpu, ".base.txt")
    pids_base = table("pid", str.strip, ".base.txt")
    sf_base = table("sf", parse_timestats, ".base.txt")
    sf_ok = any(v["total"] is not None for v in sf.values())
    hwui = any(g.get("present") and g.get("total_frames") is not None for g in gfx.values())

    # union of per-frame rows across snapshots (the platform keeps only the most recent 120 per window)
    seen = {}
    for sid in order:
        for row in gfx.get(sid, {}).get("rows", []) + gfx_base.get(sid, {}).get("rows", []):
            key = (row.get("IntendedVsync"), row.get("FrameTimelineVsyncId"), row.get("FrameCompleted"))
            seen[key] = row
    frames = sorted(seen.values(), key=lambda r: r.get("IntendedVsync", 0))

    am = _read_raw(raw, "am-start.txt")
    total_time = re.search(r"TotalTime: (\d+)", am or "")
    launch_state = re.search(r"LaunchState: (\w+)", am or "")
    launch_status = re.search(r"Status: (\w+)", am or "")

    def delta(now, before, key, first):
        """now - before, or None unless both readings hold the value. The launch snapshot is measured
        from zero: the process is fresh and the compositor statistics were cleared at the start."""
        if now is None or now.get(key) is None:
            return None
        if first:
            return now[key]
        if before is None or before.get(key) is None:
            return None
        return now[key] - before[key]

    steps_out = []
    for n, sid in enumerate(order):
        first = n == 0
        prev_sid = None if first else order[n - 1]
        step = by_id.get(sid)
        kind = "launch" if sid == "launch" else step["do"]
        # A step that looked up its target counts from the baseline taken after the lookup, never
        # from the previous step's end (the lookup's own work lies between the two).
        found = step is not None and "find" in step
        start = marks.get("launch.start" if sid == "launch" else f"{sid}.start")
        end = marks.get(f"{sid}.end") if sid != "launch" else marks.get(launch_end_mark(scn))
        missing = [label for label, tbl in (("frame stats", gfx), ("compositor statistics", sf), ("cpu", cpu_end),
                                            ("process id", pids)) if sid not in tbl]
        if found:
            missing += [f"baseline {label}" for label, tbl in (("frame stats", gfx_base), ("compositor statistics", sf_base),
                                                               ("cpu", cpu_base), ("process id", pids_base)) if sid not in tbl]
        g = gfx.get(sid)
        pg = None if first else (gfx_base.get(sid) if found else gfx.get(prev_sid))
        pid_now = pids.get(sid)
        pid_prev = None if first else pids.get(prev_sid)
        single = single_pid(pid_now)
        process_missing = not (pid_now or "").strip()
        process_ambiguous = single is None and not process_missing
        restarted = bool(not first and single is not None and single_pid(pid_prev) is not None
                         and single != single_pid(pid_prev))
        counters = {k: delta(g, pg, k, first) for k in GFX_TOTALS}
        if restarted:
            # The counters belong to a new process: a delta against the old one means nothing.
            counters = {k: None for k in GFX_TOTALS}
        in_step = [r for r in frames if start is not None and end is not None
                   and start * 1e9 <= r.get("IntendedVsync", 0) < end * 1e9]
        # Timing uses only rows the platform marks as ordinary (Flags == 0): first-draw and skipped
        # frames are counted as frames but left out of durations, as the platform advises.
        durs = [(r["FrameCompleted"] - r["IntendedVsync"]) / 1e6 for r in in_step
                if r.get("Flags", 0) == 0 and r.get("IntendedVsync", 0) > 0
                and 0 < r.get("FrameCompleted", 0) - r["IntendedVsync"] < 10e9]
        flagged = sum(1 for r in in_step if r.get("Flags", 0) != 0)

        # CPU: this step's reading (taken first at its end) against the previous step's last reading,
        # over the time between the two stamps.
        cpu_delta = cpu_total = cpu_window = None
        now_c = cpu_end.get(sid)
        if now_c is not None and single is not None and not restarted:
            if first:
                base = {"uptime": marks.get("launch.start"), "process": 0, "threads": {}}
            elif found:
                base = cpu_base.get(sid) if single_pid(pids_base.get(sid)) == single else None
            elif single_pid(pid_prev) == single:
                base = cpu_after.get(prev_sid)
            else:
                base = None
            if (base is not None and None not in (base["uptime"], base["process"], now_c["uptime"], now_c["process"])):
                cpu_total = now_c["process"] - base["process"]
                cpu_window = round(now_c["uptime"] - base["uptime"], 3)
                agg = {}
                for tid, (comm, ticks) in now_c["threads"].items():
                    before = base["threads"].get(tid, (comm, 0))[1]
                    if ticks - before:
                        name = "<main>" if tid == str(single) else comm
                        agg[name] = agg.get(name, 0) + (ticks - before)
                cpu_delta = dict(sorted(agg.items(), key=lambda kv: -kv[1]))

        first_frame_ms = None
        if kind in ACTION_KINDS and in_step:
            inj = marks.get(f"{sid}.start")
            done = [r["FrameCompleted"] for r in in_step if r.get("FrameCompleted", 0) > 0]
            if done:
                first_frame_ms = round(min(done) / 1e6 - inj * 1000, 1)

        frames_display = frames_layers = None
        now_sf = sf.get(sid)
        prev_sf = None if first else (sf_base.get(sid) if found else sf.get(prev_sid))
        if now_sf is not None and now_sf["total"] is not None:
            prev_ok = first or (prev_sf is not None and prev_sf["total"] is not None)
            if prev_ok:
                frames_display = now_sf["total"] - (0 if first else prev_sf["total"])
                now_layers = app_layer_frames(now_sf, scn["package"], facts.get("uid"))
                old_layers = {} if first else app_layer_frames(prev_sf, scn["package"], facts.get("uid"))
                # No app layer in either snapshot means the compositor never saw the app: unknown, not 0.
                if now_layers or old_layers:
                    frames_layers = sum(max(0, c - old_layers.get(name, 0)) for name, c in now_layers.items())

        quiet = cause = None
        if f"{sid}.quiet" in marks:
            quiet = "reached"
        elif f"{sid}.quiet_timeout" in marks:
            quiet = "timeout"
            cause = next((c for c in ("app", "display", "unattributed") if f"{sid}.quiet_timeout_{c}" in marks), None)
        elif f"{sid}.quiet_unavailable" in marks:
            quiet = "unavailable"
        fg_text = _read_raw(raw, f"fg-{sid}.txt")
        component = parse_foreground(fg_text)
        step_rec = {
            "id": sid, "kind": kind, "start_s": start, "end_s": end,
            # Was the app's activity the resumed one when the step ended? None: not recorded.
            "foreground": None if component is None else component.split("/")[0] == scn["package"],
            "foreground_component": component,
            "frames": counters["total_frames"] if hwui else None, "janky_frames": counters["janky_frames"] if hwui else None,
            "frames_layers": frames_layers, "frames_display": frames_display,
            "counters": counters, "process_restarted": restarted,
            "process_missing": process_missing, "process_ambiguous": process_ambiguous,
            "snapshot_missing": missing,
            "rows_in_window": len(in_step), "rows_flagged": flagged,
            "frame_ms_p50": round(pct(durs, 0.5), 2) if durs else None,
            "frame_ms_p90": round(pct(durs, 0.9), 2) if durs else None,
            "frame_ms_max": round(max(durs), 2) if durs else None,
            "first_frame_completed_after_step_start_ms": first_frame_ms,
            "cpu_ticks_by_thread": cpu_delta,
            "cpu_ticks_total": cpu_total,
            "cpu_window_s": cpu_window,
            "quiet": quiet,
        }
        if quiet == "timeout":
            step_rec["quiet_timeout_cause"] = cause
        ref = marks.get(f"{sid}.injected", marks.get("launch.end") if sid == "launch" else None)
        if quiet == "reached" and ref is not None:
            step_rec["quiet_after_ms"] = round((marks[f"{sid}.quiet"] - ref) * 1000)
        mem_text = _read_raw(raw, f"mem-{sid}.txt")
        if mem_text is not None:
            step_rec["meminfo"] = parse_meminfo(mem_text)
        if found:
            step_rec["find"] = find_record(_read_raw(raw, f"find-{sid}.txt"), step, facts, cpu_base.get(sid))
        if kind == "url":
            step_rec["url_open"] = parse_url_open(_read_raw(raw, f"url-{sid}.txt"))
        steps_out.append(step_rec)

    rows_total = sum(s["rows_in_window"] for s in steps_out)
    frames_total = sum(s["frames"] or 0 for s in steps_out)
    limit = cap.get("record_limit_s")
    result = {
        "tool": cap.get("tool", {"name": "ui-probe", "version": VERSION}),
        "scenario": scn["name"], "package": scn["package"], "device": facts,
        "host": cap["host"],
        "launch": {"state": launch_state.group(1) if launch_state else None,
                   "status": launch_status.group(1) if launch_status else None,
                   "total_time_ms": int(total_time.group(1)) if total_time else None,
                   "error": parse_launch_error(am)},
        "marks": marks, "steps": steps_out, "frames_total_rows": len(frames),
        "hwui_reported": hwui, "compositor_stats": sf_ok,
        "frame_sources": [name for name, ok in (("platform", hwui), ("compositor-stats", sf_ok)) if ok],
        "video_requested": bool(video), "trace_requested": bool(trace),
        # Per-frame rows are stamped on the monotonic clock and step marks on the boot clock. They
        # agree unless the device has slept since boot; a large shortfall of rows inside the step
        # windows is the sign. Counts are unaffected (they come from totals, not from the rows).
        "clock_mismatch_suspected": bool(hwui and frames_total >= 20 and rows_total < 0.5 * min(frames_total, len(frames))),
        "video_truncated": bool(video and limit and "END" in marks and "T0" in marks
                                and marks["END"] - marks["T0"] > limit - 1.5),
    }

    request_counts(raw, steps_out, result, ANDROID, scn, bool(cap.get("requests_log")), marks)

    if trace:
        trace_path = out / "trace.pftrace"
        if not trace_path.exists():
            result["trace_missing"] = True
        else:
            _analyze_trace(result, steps_out, trace_path, scn, pids, order, bool(cap.get("sections")),
                           cap.get("tracer_output"), facts.get("sdk"))

    if video:
        exit_text = (_read_raw(raw, "sr-exit.txt") or "").strip()
        result["video_recorder_exit"] = int(exit_text) if exit_text.lstrip("-").isdigit() else None
        alive = (_read_raw(raw, "sr-alive.txt") or "").strip()
        result["video_recorder_alive_at_end"] = {"alive": True, "gone": False}.get(alive)
        mp4 = raw / "screen.mp4"
        if not mp4.exists():
            result["video_missing"] = True
        else:
            sys.path.insert(0, str(HERE))
            import ui_video
            try:
                result["video"] = ui_video.analyze(mp4, out, frames, marks, scn, facts)
            except ui_video.UnusableRecording as exc:
                result["video_error"] = str(exc)
            except ui_video.VideoError as exc:
                raise ProbeError(str(exc)) from exc

    tmp = out / "run.json.tmp"
    tmp.write_text(json.dumps(result, indent=1))
    tmp.replace(result_path)
    return result


# perfetto's own lines, stamped on the boot clock (the clock of the trace and of the marks): the
# session's start and the moment it stopped recording.
TRACER_CONNECTED_RE = re.compile(r"^\[\s*([0-9]+\.[0-9]+)\][^\n]*Connected to the Perfetto traced service", re.M)
TRACER_DISABLED_RE = re.compile(r"^\[\s*([0-9]+\.[0-9]+)\][^\n]*disabling tracing", re.M)
FRAMETIMELINE_SDK = 31      # the frame timeline starts with Android 12


def tracer_session(output: str | None) -> tuple[int | None, int | None]:
    """(start, stop) of the tracing session in nanoseconds on the boot clock, from what perfetto
    printed, or None for a time it did not print."""
    a = TRACER_CONNECTED_RE.search(output or "")
    b = TRACER_DISABLED_RE.search(output or "")
    return (int(round(float(a.group(1)) * 1e9)) if a else None, int(round(float(b.group(1)) * 1e9)) if b else None)


def ftrace_span(fs: dict, ftrace: dict, tracer_output: str | None) -> tuple:
    """(lo, hi, what decided it) for the sections of a trace without a frame timeline (Android 11 and
    older), in nanoseconds; (None, None, None) when nothing covers.

    The span of the ftrace events themselves, widened to the ftrace data source's own run only when the
    trace holds its statistics once for the start and once for the end of the trace: then their per-CPU
    `now_ts` are the bounds, if every one of them is above 0 and the latest start lies before the
    earliest end; statistics that hold no `now_ts` let the session perfetto printed widen the events'
    span instead. Anything else (a phase missing or written twice, a `now_ts` of 0, a start after an
    end) leaves the events' own span."""
    ev_lo, ev_hi = ftrace.get("events_first_ts"), ftrace.get("events_last_ts")
    once = fs.get("start_packets") == 1 and fs.get("end_packets") == 1
    starts, ends = fs.get("start_now_ns") or [], fs.get("end_now_ns") or []
    if once and starts and ends:
        if all(v > 0 for v in starts + ends) and max(starts) < min(ends):
            return max(starts), min(ends), ["the ftrace data source's statistics (start and end of trace)"]
    elif once and not starts and not ends and ev_lo is not None:
        sess_lo, sess_hi = tracer_session(tracer_output)
        lo = min(x for x in (ev_lo, sess_lo) if x is not None)
        hi = max(x for x in (ev_hi, sess_hi) if x is not None)
        return lo, hi, ["ftrace events"] + (["the tracer's output"] if (sess_lo, sess_hi) != (None, None) else [])
    if ev_lo is None:
        return None, None, None
    return ev_lo, ev_hi, ["ftrace events"]


def _analyze_trace(result: dict, steps_out: list, trace_path: Path, scn: dict, pids: dict, order: list,
                   sections: bool, tracer_output: str | None = None, sdk=None) -> None:
    sys.path.insert(0, str(HERE))
    import pftrace
    ftrace = {"bundles": 0, "lost_bundles": 0, "events_first_ts": None, "events_last_ts": None}
    try:
        tr = pftrace.read_trace(str(trace_path))
        begins_all = pftrace.read_atrace(str(trace_path), ftrace) if sections else []
    except pftrace.TraceError as exc:
        raise ProbeError(f"the trace cannot be read: {exc}") from exc
    # Did the trace lose data? The service's own counters say so; nothing else in the file does.
    stats = tr["stats"]
    loss = []
    if stats["packets"] == 0:
        loss.append("the trace holds no buffer statistics, so data loss cannot be ruled out")
    if stats["chunks_overwritten"]:
        loss.append(f"the trace buffer overwrote {stats['chunks_overwritten']} chunks before they were read")
    if stats["chunks_discarded"]:
        loss.append(f"the trace buffer discarded {stats['chunks_discarded']} chunks")
    if stats["patches_failed"]:
        loss.append(f"the tracing service could not complete {stats['patches_failed']} chunks a writer had left open")
    if stats["writer_packet_loss"]:
        loss.append(f"a trace writer reported packet loss {stats['writer_packet_loss']} times")
    if stats["flushes_failed"] or stats["final_flush"] == 2:
        loss.append("a flush of the trace failed, so its last data may be missing")
    result["trace_stats"] = stats
    result["trace_loss"] = loss
    sections_loss = None
    if sections and ftrace["lost_bundles"]:
        sections_loss = (f"the kernel dropped trace events ({ftrace['lost_bundles']} of {ftrace['bundles']} "
                         "event bundles report a loss before them)")
    result["sections_loss"] = sections_loss
    pkg = scn["package"]
    # The span the trace's stamped packets hold. A trace that stopped early, or whose ring buffer
    # overwrote its beginning, does not cover every step; a step outside the span has no frame
    # timeline counts. Event bundles carry no packet timestamp and are not in this span.
    first_ts, last_ts = tr["first_ts"], tr["last_ts"]
    result["trace_span_s"] = [first_ts / 1e9, last_ts / 1e9] if first_ts is not None else None
    result["trace_truncated"] = tr["truncated"]
    own_pids = {int(t) for p in pids.values() for t in p.split() if t.isdigit()}
    # Android 11 and older: no frame timeline, and stamped packets that end early. Only a capture that says
    # so (its API level) and a trace without a single frame-timeline packet take this path; anything
    # else is read exactly as 0.6.0 read it.
    level = int(sdk) if str(sdk).strip().isdigit() else None
    old_android = level is not None and level < FRAMETIMELINE_SDK and not tr.get("frametimeline_packets")
    if old_android:
        # No frame timeline: its counts are unknown, never 0.
        result["frametimeline"] = None
        result["frametimeline_unavailable"] = (
            f"the device has no frame timeline (Android API {level}; it starts with Android 12, API {FRAMETIMELINE_SDK})")
        for st in steps_out:
            st["trace_covered"] = None
            st["frames_compositor"] = None
        result["trace_uncovered_steps"] = []
    else:
        app_rows = [r for r in tr["surface_frames"] if pkg in r.get("layer", "") and r.get("is_buffer")]
        pid_set = sorted({r["pid"] for r in app_rows})
        app_pid = single_pid(pids.get(order[-1]))
        own = [r for r in app_rows if r["pid"] == app_pid]
        jank, present = {}, {}
        for r in own:
            present[r["present"]] = present.get(r["present"], 0) + 1
            for j in r["jank"]:
                jank[j] = jank.get(j, 0) + 1
        result["frametimeline"] = {
            "surface_frames_all_layers": len(tr["surface_frames"]), "display_frames": len(tr["display_frames"]),
            "app_buffer_frames": len(own), "pids_on_app_layers": pid_set,
            "app_jank_types": jank, "app_present_types": present,
            "layers": sorted({r["layer"] for r in own}),
        }

        def covered(st):
            return (not loss and first_ts is not None and st["start_s"] is not None and st["end_s"] is not None
                    and first_ts <= st["start_s"] * 1e9 and st["end_s"] * 1e9 <= last_ts)

        # The compositor's own count per step: frames of the app's layers from any of its processes
        # (a restart changes the pid), whatever toolkit drew them. Trace and marks share the boot clock.
        uncovered = []
        for st in steps_out:
            st["trace_covered"] = covered(st)
            if not st["trace_covered"]:
                st["frames_compositor"] = None
                uncovered.append(st["id"])
                continue
            lo, hi = st["start_s"] * 1e9, st["end_s"] * 1e9
            st["frames_compositor"] = sum(1 for r in app_rows if r["pid"] in own_pids and lo <= (r.get("start") or 0) < hi)
        result["trace_uncovered_steps"] = uncovered
        result["frame_sources"].append("compositor-trace")
    if sections:
        # The span that decides whether a step's sections were recorded. With a frame timeline (Android
        # 12 and later) it is the stamped packets' span, as for the frame timeline. Without one, the
        # stamped packets end early (an ftrace bundle carries no packet timestamp), so it is the span of
        # the ftrace events themselves; it is widened to the ftrace data source's own run only when the
        # trace holds its statistics for both the start and the end of the trace: then their `now_ts`
        # are the bounds, or, if they hold none, the session perfetto printed. A screen at rest emits
        # no event, so the last event can come well before the end of the last step.
        if not old_android:
            sec_lo, sec_hi = first_ts, last_ts
            span_from = ["stamped packets"] if first_ts is not None else None
        else:
            sec_lo, sec_hi, span_from = ftrace_span(tr.get("ftrace_stats") or {}, ftrace, tracer_output)
        result["sections_span_s"] = [sec_lo / 1e9, sec_hi / 1e9] if sec_lo is not None else None
        result["sections_span_from"] = span_from

        def sections_covered(st):
            return (not loss and sec_lo is not None and st["start_s"] is not None and st["end_s"] is not None
                    and sec_lo <= st["start_s"] * 1e9 and st["end_s"] * 1e9 <= sec_hi)

        wanted = list(dict.fromkeys(DEFAULT_SECTIONS + list(scn.get("count_sections", []))))
        begins = [b for b in begins_all if b["tgid"] in own_pids]
        match = lambda b, w: b["name"] == w or b["name"].startswith(w + " ")
        # A name the app never emitted anywhere in the trace was not observed: the build or the
        # platform may not emit it. Its count is unknown, not zero.
        observed = {w for w in wanted if any(match(b, w) for b in begins)}
        result["sections_total"] = len(begins)
        result["sections_not_observed"] = [w for w in wanted if w not in observed]
        sec_uncovered = []
        for st in steps_out:
            st["sections_covered"] = sections_covered(st)
            if not st["sections_covered"]:
                sec_uncovered.append(st["id"])
            if not st["sections_covered"] or sections_loss:
                st["sections"] = {w: None for w in wanted}
                st["sections_all"] = None
                continue
            lo, hi = st["start_s"] * 1e9, st["end_s"] * 1e9
            inside = [b for b in begins if lo <= b["ts"] < hi]
            st["sections"] = {w: (sum(1 for b in inside if match(b, w)) if w in observed else None) for w in wanted}
            st["sections_all"] = len(inside)
        # With lost data every count is unknown and the gate says so once; this list is for the rest.
        result["sections_uncovered_steps"] = [] if loss else sec_uncovered


# --------------------------------------------------------------------------------------------
# requests, counted from the log of a front the app's traffic passes through
# --------------------------------------------------------------------------------------------
#
# `ui-probe http-front` (an Android emulator reaches it as 10.0.2.2) and `ui-probe tls-front` (the iOS
# Simulator) log every HTTP/1.x request they pipe, stamped with this machine's clock. A run given the
# log with --requests notes where the log ends at its start, reads what was appended (and, to follow
# the connections the front could not read, what the same front process wrote before), and places
# the requests in the step windows: on the simulator they are this machine's clock already; on
# Android the marks are the device's /proc/uptime, mapped to this machine's clock by reading
# /proc/uptime through adb before and after the device script. A count is known only when the run
# can show the front saw everything the app sent in that window.

REQUEST_EVENTS = ("request", "unparsed", "upgrade", "backend-refused")
REQUEST_MARGIN_S = SIM_LOG_MARGIN_S   # lines kept around the run's window, as for the log store
REQUEST_TOP_PATHS = 20
REQUESTS_FILE = "requests.jsonl"
REQUESTS_META = "requests-meta.json"
UPTIME_PROBES = 3               # reads of /proc/uptime before and after the device script
UPTIME_RESOLUTION_S = 0.01      # /proc/uptime has two decimals
CLOCK_DRIFT_MAX_S = 0.1         # the two offsets may differ by this plus half of each round trip
# Above this the counts are unknown ("adb answered too slowly"). No bound on the uncertainty keeps a
# tap's own request out of a neighbouring rest: an input takes 0.43 to 0.46 s from its step's start mark
# to the app on an Android 11 emulator (`input` starts a runtime first), but about 0.01 s on an Android 14
# one (`input` is `cmd input`; `input tap` returned in 0.01 s, 3 of 3), so a tap's request can follow
# its start mark by a few tens of milliseconds. That is why the zone at a border between a rest and an
# input step goes to the step (request_windows), not to the rest.
CLOCK_UNCERTAINTY_MAX_S = 0.2
CLOCK_NOTE_ABOVE_S = 0.1        # the gate says how far each rest's window was narrowed above this
NO_REQUEST_LOG = "not counted: the run was given no request log"


def _count(value) -> bool:
    """A known count: an int that is not a bool (JSON's true is not a count) and is not negative."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def front_pid(text: str) -> int | None:
    """The pid in the log's last `listening` line, or None when there is no such line with a pid."""
    pid = None
    found = False
    for line in text.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict) and e.get("event") == "listening":
            found = True
            pid = e.get("pid") if isinstance(e.get("pid"), int) and not isinstance(e.get("pid"), bool) else None
    return pid if found else None


def process_running(pid: int | None) -> bool:
    """Whether a process with this id exists (one of another user counts)."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def check_request_log(path: str) -> Path:
    """The front's log a run counts requests from; refused unless it exists, holds the line a front
    writes when it starts listening, and the front that wrote the last such line is running."""
    p = Path(path)
    if not p.is_file():
        raise ProbeError(f"--requests {path}: no such file. Start the front with `--log {path}` first "
                         "(`ui-probe http-front` for an Android emulator, `ui-probe tls-front` for the iOS Simulator)")
    try:
        text = p.read_text(errors="replace")
    except OSError as exc:
        raise ProbeError(f"--requests {path} cannot be read: {exc.strerror or exc}") from exc
    if not any(isinstance(e, dict) and e.get("event") == "listening" for e in _json_lines(text)):
        raise ProbeError(f"--requests {path} holds no 'listening' line, so it is not the log of a front: start "
                         f"`ui-probe http-front` or `ui-probe tls-front` with `--log {path}`")
    pid = front_pid(text)
    if not process_running(pid):
        raise ProbeError(f"the front that wrote {path} is not running (pid {pid}): start it again with --log {path}")
    return p.resolve()


def _json_lines(text: str):
    for line in text.splitlines():
        try:
            yield json.loads(line)
        except ValueError:
            yield None


# How much of the log's start a run keeps (in memory, never written) to tell, at its end, that the file
# with the log's name is still the same file. A file system may give a new file the number of one just
# removed (Linux does, measured on GitHub's runner): the inode and device alone cannot tell.
LOG_HEAD_BYTES = 4096


def log_position(path: Path) -> dict:
    """Where the log ends now, which file it is (its inode and device, and its first LOG_HEAD_BYTES
    bytes, kept in this process only), and whether the front that writes it is running."""
    try:
        with open(path, "rb") as fh:
            st = os.fstat(fh.fileno())
            data = fh.read()
    except OSError as exc:
        raise ProbeError(f"the request log {path} cannot be read: {exc.strerror or exc}") from exc
    pid = front_pid(data[:st.st_size].decode("utf-8", "replace"))
    return {"size": st.st_size, "inode": st.st_ino, "device": st.st_dev,
            "head": data[:min(st.st_size, LOG_HEAD_BYTES)], "front_pid": pid, "front_alive": process_running(pid)}


def log_identity_problem(start: dict, st: os.stat_result, data: bytes) -> str | None:
    """Why the file that has the log's name now (its fstat and its bytes) is not the log a run began
    with, grown or not: None when it is. Another file is called replaced (another inode or device, or
    first bytes that differ from those kept at the start); the same file grown shorter is called cut
    (a file shorter than the bytes kept is cut when what it still has is their start, replaced
    otherwise)."""
    if (st.st_ino, st.st_dev) != (start["inode"], start["device"]):
        return "the request log was replaced during the run (another file has its name now)"
    head = start["head"]
    now = data[:min(len(head), st.st_size)]
    if now != head[:len(now)]:
        return "the request log was replaced during the run (its first bytes are not those it had at the start)"
    if st.st_size < start["size"]:
        return "the request log was cut during the run (it is shorter than at the start)"
    return None


def _write_private(path: Path, text: str) -> None:
    """A file only its owner can read (0600), whether it existed or not."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        data = text.encode()
        while data:
            data = data[os.write(fd, data):]
    finally:
        os.close(fd)


def save_requests(raw: Path, source: Path, start: dict, window: tuple, clock: dict, reason: str | None) -> None:
    """Read the log after the run and save raw/requests.jsonl and raw/requests-meta.json (both 0600).

    Kept: the request, unparsed, upgrade and backend-refused lines appended since `start` whose time
    lies in the run's window (this machine's clock) widened by REQUEST_MARGIN_S. Followed: every
    connection the front could not read (from its upgrade or unparsed line to its closed line) since
    the last `listening` line before `start`, so that one that became opaque before the run and was
    still open during it is seen. Recorded: whether the front was running at the start and at the end.
    What could not be read is a reason (the run's counts are then unknown); a file that cannot be
    written raises ProbeError."""
    lo, hi = window
    meta = {"source": str(source), "offset_start": start["size"], "offset_end": None,
            "window": [lo, hi], "margin_s": REQUEST_MARGIN_S, "clock": clock, "reason": reason,
            "kept": 0, "unreadable_lines": 0, "partial_tail_bytes": 0, "restarts": [], "stops": [], "opaque": [],
            "front": {"pid_at_start": start.get("front_pid"), "alive_at_start": start.get("front_alive"),
                      "pid_at_end": None, "alive_at_end": None, "port": None, "accepting_at_end": None}}
    kept = []
    sys.path.insert(0, str(HERE))
    import httppipe
    try:
        # Does the front still accept? Its last `listening` line names the port. This comes after the
        # device part, so after the last request line of the run could be written; the probe's own
        # `closed` line (on http-front) lies after the run's window.
        last = httppipe.last_listening(str(source)) or {}
        meta["front"]["port"] = last.get("port")
        meta["front"]["accepting_at_end"] = httppipe.port_accepts(last.get("port"))
        with open(source, "rb") as fh:            # one open file for the checks and the reading
            st = os.fstat(fh.fileno())
            data = fh.read(st.st_size)
        meta["offset_end"] = st.st_size
        problem = log_identity_problem(start, st, data)
        if problem is not None:
            meta["reason"] = meta["reason"] or problem
        else:
            pid_end = front_pid(data.decode("utf-8", "replace"))
            meta["front"].update(pid_at_end=pid_end, alive_at_end=process_running(pid_end))
            # Follow the connections from the last `listening` line before the start: the front process
            # that was running when the run began.
            begin = 0
            for m in re.finditer(rb"[^\n]*\n", data[:start["size"]]):
                try:
                    e = json.loads(m.group(0))
                except ValueError:
                    continue
                if isinstance(e, dict) and e.get("event") == "listening":
                    begin = m.start()
            *lines, tail = data[begin:].split(b"\n")
            meta["partial_tail_bytes"] = len(tail)      # a line still being written, after the run
            keep_lo = None if lo is None else lo - REQUEST_MARGIN_S
            keep_hi = None if hi is None else hi + REQUEST_MARGIN_S
            opaque_open: dict = {}
            intervals = []
            offset = begin
            for line in lines:
                appended = offset >= start["size"]
                offset += len(line) + 1
                if not line.strip():
                    continue
                try:
                    e = json.loads(line)
                    t = e["t"] if isinstance(e, dict) else None
                except (ValueError, KeyError):
                    t = e = None
                if not isinstance(t, (int, float)) or isinstance(t, bool):
                    meta["unreadable_lines"] += 1
                    continue
                event = e.get("event")
                if event == "listening":
                    # a new front process: the connections of the last one are gone with it
                    for iv in opaque_open.values():
                        iv["to"] = t
                    opaque_open = {}
                    if appended:
                        meta["restarts"].append(t)
                    continue
                if event == "stopping" and appended:
                    meta["stops"].append(t)
                if event in ("upgrade", "unparsed") and e.get("conn") not in opaque_open:
                    iv = {"conn": e.get("conn"), "kind": event, "why": e.get("why"), "from": t, "to": None}
                    opaque_open[e.get("conn")] = iv
                    intervals.append(iv)
                elif event == "closed" and e.get("conn") in opaque_open:
                    opaque_open.pop(e.get("conn"))["to"] = t
                if appended and event in REQUEST_EVENTS and keep_lo is not None and keep_lo <= t <= keep_hi:
                    kept.append(line.decode("utf-8", "replace"))
            if keep_lo is not None:
                meta["opaque"] = [iv for iv in intervals if iv["from"] <= keep_hi and (iv["to"] is None or iv["to"] >= keep_lo)]
    except OSError as exc:
        meta["reason"] = meta["reason"] or f"the request log could not be read at the end of the run: {exc.strerror or exc}"
    meta["kept"] = len(kept)
    try:
        _write_private(raw / REQUESTS_FILE, "".join(line + "\n" for line in kept))
        _write_private(raw / REQUESTS_META, json.dumps(meta, indent=1))
    except OSError as exc:
        raise ProbeError(f"the requests of the run could not be saved in {raw}: {exc.strerror or exc}") from exc


def uptime_probes(serial: str, probes: int = UPTIME_PROBES, clock=time.time) -> dict:
    """`probes` reads of the device's /proc/uptime, each bracketed by this machine's clock; the read
    with the shortest round trip is kept: its offset is the middle of the bracket minus the uptime.
    {'known': False, 'reason'} when no read gave a value."""
    tried = []
    for _ in range(probes):
        h0 = clock()
        try:
            text = sh(serial, "cat /proc/uptime", timeout=30)
        except ProbeError as exc:
            tried.append({"error": str(exc)[:200]})
            continue
        h1 = clock()
        m = re.match(r"\s*([0-9]+(?:\.[0-9]+)?)\s", text + " ")
        if not m:
            tried.append({"error": f"the device answered {text.strip()[:80]!r}", "round_trip_s": round(h1 - h0, 6)})
            continue
        tried.append({"host_before": h0, "host_after": h1, "uptime": float(m.group(1)), "round_trip_s": round(h1 - h0, 6)})
    good = [t for t in tried if "uptime" in t]
    if not good:
        return {"known": False, "probes": tried,
                "reason": "the device's /proc/uptime could not be read from this machine (" +
                          ("; ".join(t["error"] for t in tried) or "no probe ran") + ")"}
    best = min(good, key=lambda t: t["round_trip_s"])
    mid = (best["host_before"] + best["host_after"]) / 2
    return {"known": True, "uptime": best["uptime"], "host": mid, "offset_s": mid - best["uptime"],
            "round_trip_s": best["round_trip_s"], "probes": tried}


def android_request_clock(before: dict | None, after: dict | None) -> tuple[dict, str | None]:
    """The mapping of the device's uptime to this machine's clock: the straight line through the two
    kept probes (before and after the device script), its uncertainty, and why it is unknown (None
    when known)."""
    clock = {"platform": ANDROID, "known": False, "before": before, "after": after}
    for name, g in (("before", before), ("after", after)):
        if g is None or not g.get("known"):
            return clock, (g or {}).get("reason") or f"the device's clock was not read {name} the device script"
    drift = abs(after["offset_s"] - before["offset_s"])
    half = before["round_trip_s"] / 2 + after["round_trip_s"] / 2
    u = round(half + UPTIME_RESOLUTION_S + drift, 6)
    clock.update(drift_s=round(drift, 6), uncertainty_s=u)
    if drift > CLOCK_DRIFT_MAX_S + half:
        return clock, (f"the device's clock and this machine's moved apart by {drift:.3f} s during the run (the emulator "
                       "was paused, or a clock was set)")
    if u > CLOCK_UNCERTAINTY_MAX_S:
        return clock, (f"the device's clock is mapped to this machine's only to within {u:.3f} s (above "
                       f"{CLOCK_UNCERTAINTY_MAX_S} s): adb answered too slowly")
    clock["known"] = True
    return clock, None


def uptime_to_host(clock: dict, uptime: float | None) -> float | None:
    if uptime is None or not clock.get("known"):
        return None
    b, a = clock["before"], clock["after"]
    if a["uptime"] == b["uptime"]:
        return uptime + b["offset_s"]
    slope = (a["host"] - b["host"]) / (a["uptime"] - b["uptime"])
    return b["host"] + (uptime - b["uptime"]) * slope


def capture_requests_android(raw: Path, source: Path, start: dict, probes: tuple, bracket: tuple, scn: dict) -> None:
    """After the device part of an Android run: the clock mapping (probes: the groups read before and
    after the device script), checked against this machine's own bracket of the script, then the log.
    Without a mapping the lines between the device script's start and end on this machine's clock are
    kept, and the counts are unknown."""
    clock, reason = android_request_clock(*probes)
    window = bracket
    if reason is None:
        try:
            marks = parse_marks(_read_raw(raw, "marks.txt") or "")
        except ProbeError as exc:
            marks, reason = {}, str(exc)
        lo = uptime_to_host(clock, marks.get("launch.start"))
        hi = uptime_to_host(clock, marks.get(f"{scn['steps'][-1]['id']}.end"))
        u = clock["uncertainty_s"]
        if lo is None or hi is None:
            reason = reason or "the run holds no mark of its launch or of the end of its last step"
        elif not (bracket[0] - u <= lo <= bracket[1] + u and bracket[0] - u <= hi <= bracket[1] + u):
            clock["known"] = False
            reason = (f"the clock mapping puts the launch at {lo:.3f} and the end of the last step at {hi:.3f}, outside "
                      f"the time this machine ran the device script ({bracket[0]:.3f} to {bracket[1]:.3f}, widened by "
                      f"{u:.3f} s)")
        else:
            window = (lo, hi)
    save_requests(raw, source, start, window, clock, reason)


def capture_requests_sim(raw: Path, source: Path, start: dict, bracket: tuple, scn: dict) -> None:
    """After the device part of a simulator run: the steps are timed on this machine's clock."""
    launch = json.loads(_read_raw(raw, "launch-time.json") or "{}").get("wall")
    last = json.loads(_read_raw(raw, f"step-{scn['steps'][-1]['id']}.json") or "{}").get("wall_after")
    clock = {"platform": SIM_PLATFORM, "known": True, "uncertainty_s": 0.0,
             "mapping": "none: the steps are timed on this machine's clock"}
    reason = None if launch is not None and last is not None else \
        "the run holds no time of its launch or of the end of its last step"
    save_requests(raw, source, start, (launch, last) if reason is None else bracket, clock, reason)


def request_windows(raw: Path, steps_out: list[dict], platform: str, clock: dict, scn: dict, marks: dict) -> dict:
    """Each step's request window on this machine's clock, (lo, hi, gap start or None, gap length in
    seconds), half-open and
    disjoint, so that every request is counted in one step at most. A rest runs on to where the next
    step starts putting anything into the app (its lookup's first swipe on Android, a replayed swipe on
    the simulator, else its start): the tail after the rest's own end is its gap. The last step ends
    where the run's window ends, at its own end. Where a rest borders an input step (the launch counts
    as one), in either order, the input step takes the zone of width u (the clock's uncertainty) on the
    rest's side, and the rest's window shrinks by as much, at most half of the rest's length (its own
    length plus its gap), so that two input steps around a short rest meet in its middle; between two
    rests, or two input steps, the border is the mark itself. A first or last step that is a rest is
    widened outward by u (it has no neighbour there)."""
    u = clock.get("uncertainty_s") or 0.0
    by_id = {s["id"]: s for s in scn["steps"]}
    order = [st["id"] for st in steps_out]
    own, next_start = {}, {}
    if platform == SIM_PLATFORM:
        data = {sid: json.loads(_read_raw(raw, f"step-{sid}.json") or "{}") for sid in order if sid != "launch"}
        first = next((data[sid].get("wall_before") for sid in order if sid != "launch"), None)
        own["launch"] = (json.loads(_read_raw(raw, "launch-time.json") or "{}").get("wall"), first)
        for sid in order:
            if sid != "launch":
                own[sid] = (data[sid].get("wall_before"), data[sid].get("wall_after"))
        for n, sid in enumerate(order[:-1]):
            if sid == "launch":
                continue
            nxt = order[n + 1]
            replayed = (data[nxt].get("resolved") or {}).get("swipes")
            next_start[sid] = data[sid].get("rec_wall_end") if replayed else data[nxt].get("wall_before")
    else:
        for st in steps_out:
            own[st["id"]] = (uptime_to_host(clock, st.get("start_s")), uptime_to_host(clock, st.get("end_s")))
        for n, sid in enumerate(order[:-1]):
            nxt = by_id.get(order[n + 1]) or {}
            scroll = "find" in nxt and "scroll" in nxt["find"]
            mark = f"{nxt['id']}.find_start" if scroll else f"{nxt['id']}.start"
            next_start[sid] = uptime_to_host(clock, marks.get(mark))
    rests = {sid for sid in order if (by_id.get(sid) or {}).get("do") == "rest"}
    win = {}
    for sid in order:
        lo, hi = own.get(sid, (None, None))
        if lo is None or hi is None:
            continue
        gap_from, gap_s = None, 0.0
        if sid in rests and next_start.get(sid) is not None and next_start[sid] > hi:
            gap_from, gap_s, hi = hi, next_start[sid] - hi, next_start[sid]
        win[sid] = [lo, hi, gap_from, gap_s]
    placed = [sid for sid in order if sid in win]
    # What an input step takes from a neighbouring rest is u, at most half of the rest's length (with its
    # gap), so that two input steps on either side of a short rest meet in its middle and never cross. An
    # input's own request can follow its start mark by 0.01 s on newer Android: no bound on u keeps it out
    # of the rest before it, so the zone the clock cannot place goes to the step, whose budget holds it.
    length = {sid: max(0.0, win[sid][1] - win[sid][0]) for sid in placed}
    for a, b in zip(placed, placed[1:]):
        if (a in rests) == (b in rests):
            continue                                  # two rests (or two input steps): the marks decide
        if a in rests:
            win[a][1] -= min(u, length[a] / 2)
            win[b][0] = min(win[b][0], win[a][1])
        else:
            win[a][1] += min(u, length[b] / 2)
            win[b][0] = max(win[b][0], win[a][1])
    if placed and placed[0] in rests:
        win[placed[0]][0] -= u
    if placed and placed[-1] in rests:
        win[placed[-1]][1] += u
    windows = {sid: (None, None, None, 0.0) for sid in order}
    for sid, (lo, hi, gap_from, gap_s) in win.items():
        windows[sid] = (lo, max(lo, hi), gap_from, round(gap_s, 3))
    return windows


def request_counts(raw: Path, steps_out: list[dict], result: dict, platform: str, scn: dict,
                   requested: bool, marks: dict | None = None) -> None:
    """Each step's requests: {'count', 'by_path'} (the REQUEST_TOP_PATHS most frequent 'METHOD /path'; a
    rest also 'gap_s' and 'in_gap'), or {'count': None, 'reason'}. A run captured without --requests
    gets no 'requests'; one captured with it always does. Known only when the run can show the front
    saw everything the app sent: the front ran from the start to the end, did not start again during
    the run, refused no connection to the backend, carried no connection it could not read, and passed
    at least one request between the launch and the end of the last step; and, on Android, the clock
    was mapped. A value not read is unknown, never zero."""
    meta_text = _read_raw(raw, REQUESTS_META)
    if meta_text is None and not requested:
        return
    meta = None
    if meta_text is None:
        reason = f"the run was given a request log, but raw/{REQUESTS_META} is missing: the run kept no record of it"
    else:
        try:
            meta = json.loads(meta_text)
            reason = meta.get("reason") if isinstance(meta, dict) else None
        except ValueError:
            meta = None
        if not isinstance(meta, dict):
            meta, reason = None, f"raw/{REQUESTS_META} cannot be read: the run kept no usable record of its request log"
    meta = meta or {}
    clock = meta.get("clock") or {}
    u = clock.get("uncertainty_s") or 0.0
    lo, hi = (meta.get("window") or [None, None])[:2]
    front = meta.get("front") or {}
    if reason is None and front.get("alive_at_start") is not True:
        reason = (f"the front (pid {front.get('pid_at_start')}) was not running at the start of the run: what the app "
                  "sent was not piped and is not counted")
    if reason is None and meta.get("restarts"):
        reason = ("the front started listening again after the run began (it was restarted): what the app sent while "
                  "it was down was not piped and is not counted")
    if reason is None and front.get("pid_at_end") != front.get("pid_at_start"):
        reason = (f"the front's pid changed during the run ({front.get('pid_at_start')}, then {front.get('pid_at_end')}): "
                  "what the app sent while it was down was not piped and is not counted")
    if reason is None and meta.get("stops"):
        reason = "the front was told to stop during the run: what the app sent after that was not piped and is not counted"
    if reason is None and front.get("alive_at_end") is not True:
        reason = (f"the front (pid {front.get('pid_at_end')}) was no longer running at the end of the run: what the app "
                  "sent after it stopped was not piped and is not counted")
    if reason is None and front.get("accepting_at_end") is not True:
        reason = (f"the front (pid {front.get('pid_at_end')}) was running but no longer accepted connections on port "
                  f"{front.get('port')} at the end of the run: what the app sent was not piped and is not counted")
    if reason is None and meta.get("unreadable_lines"):
        n = meta["unreadable_lines"]
        reason = (f"{_plural(n, 'line')} the front process wrote {'is' if n == 1 else 'are'} not the front's JSON, so "
                  "what the front saw cannot be followed")
    if reason is None and (lo is None or hi is None):
        reason = "the run holds no window of its launch and its last step"
    events = []
    for n, line in enumerate((_read_raw(raw, REQUESTS_FILE) or "").splitlines(), 1):
        try:
            e = json.loads(line)
        except ValueError:
            e = None
        if not isinstance(e, dict) or not isinstance(e.get("t"), (int, float)):
            reason = reason or f"raw/{REQUESTS_FILE} line {n} is not a front's JSON line"
            continue
        events.append(e)
    requests = [e for e in events if e.get("event") == "request"]
    if reason is None:
        refused = [e for e in events if e.get("event") == "backend-refused" and lo - u <= e["t"] <= hi + u]
        if refused:
            reason = (f"the backend refused {_plural(len(refused), 'connection')} from the front during the run (the "
                      f"first {max(0.0, refused[0]['t'] - lo):.1f} s after the launch): what the app sent on them was "
                      "not read")
    if reason is None:
        opaque = [iv for iv in meta.get("opaque") or []
                  if iv["from"] <= hi + u and (iv.get("to") is None or iv["to"] >= lo - u)]
        upgrades = [iv for iv in opaque if iv.get("kind") == "upgrade"]
        unparsed = [iv for iv in opaque if iv.get("kind") == "unparsed"]
        if upgrades:
            one = len(upgrades) == 1
            reason = (f"{_plural(len(upgrades), 'connection')} asked for an upgrade (`Connection: upgrade` or CONNECT) "
                      f"and {'was' if one else 'were'} open during the run: what followed on {'it' if one else 'them'} is "
                      "not HTTP/1.x requests the front can count")
        elif unparsed:
            whys = sorted({str(iv.get("why")) for iv in unparsed})
            reason = (f"{_plural(len(unparsed), 'connection')} carried bytes the front could not read as HTTP/1.x "
                      f"({'; '.join(whys)}), so their requests are not counted")
    if reason is None and not any(lo <= e["t"] <= hi for e in requests):
        reason = ("no request passed through the front between the launch and the end of the last step: the app's "
                  "traffic is probably not going through the front (is the app pointed at it?)")
    windows = request_windows(raw, steps_out, platform, clock, scn, marks or {}) if reason is None else {}
    placed = set()
    rests = {s["id"] for s in scn["steps"] if s["do"] == "rest"}
    for st in steps_out:
        if reason is not None:
            st["requests"] = {"count": None, "reason": reason}
            continue
        w_lo, w_hi, gap_from, gap_s = windows.get(st["id"], (None, None, None, 0.0))
        if w_lo is None or w_hi is None:
            st["requests"] = {"count": None, "reason": "the run holds no window for this step"}
            continue
        inside = [e for e in requests if w_lo <= e["t"] < w_hi]          # the windows are disjoint
        placed.update(id(e) for e in inside)
        tally: dict = {}
        for e in inside:
            key = f"{e.get('method')} {e.get('path')}"
            tally[key] = tally.get(key, 0) + 1
        top = sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))[:REQUEST_TOP_PATHS]
        st["requests"] = {"count": len(inside), "by_path": dict(top)}
        if st["id"] in rests:
            st["requests"].update(gap_s=gap_s,
                                  in_gap=sum(1 for e in inside if gap_from is not None and e["t"] >= gap_from))
    loose = [e for e in requests if id(e) not in placed]
    between = [e for e in loose if lo is not None and lo <= e["t"] <= hi] if reason is None else None
    between_paths: dict = {}
    for e in between or []:
        key = f"{e.get('method')} {e.get('path')}"
        between_paths[key] = between_paths.get(key, 0) + 1
    summary_clock = {k: clock.get(k) for k in ("platform", "known", "uncertainty_s", "drift_s", "mapping") if k in clock}
    result["requests"] = {"known": reason is None, "reason": reason, "log": meta.get("source"),
                          "requests": len(requests),
                          "between_steps": len(between) if between is not None else None,
                          "between_paths": dict(sorted(between_paths.items(), key=lambda kv: (-kv[1], kv[0]))[:REQUEST_TOP_PATHS]),
                          "in_margins": len(loose) - len(between) if reason is None else None,
                          "front": front or None, "clock": summary_clock}


def describe_paths(by_path: dict | None) -> str:
    return ", ".join(f"{k} x{v}" for k, v in (by_path or {}).items()) or "no path"


def request_notes(r: dict) -> list[str]:
    """What the gate says once per run about its request counts (nothing for a run without a log)."""
    block = r.get("requests")
    if not block or not block.get("known"):
        return []
    out = []
    u = (block.get("clock") or {}).get("uncertainty_s") or 0.0
    if u > CLOCK_NOTE_ABOVE_S:
        out.append(f"the device's clock was mapped to this machine's to within {u:.3f} s, so each rest's request window "
                   "was narrowed by that much (at most half of the rest) where it meets an input step or the launch: a "
                   "request that close to an input step is counted in that step, not in the rest")
    if block.get("between_steps"):
        out.append(f"{_plural(block['between_steps'], 'request')} fell between steps, after an input step's window, and "
                   f"{'was' if block['between_steps'] == 1 else 'were'} not gated: "
                   f"{describe_paths(block.get('between_paths'))}")
    return out


def launch_budget(scn: dict) -> dict | None:
    """The scenario's `launch_requests_max` as the launch step's `expect.requests_max`, or None."""
    if "launch_requests_max" not in scn:
        return None
    return {"id": "launch", "expect": {"requests_max": scn["launch_requests_max"]}}


def periodic_split(rec: dict, periodic: dict) -> tuple:
    """(held, set aside, cut) for one step's request record against the scenario's periodic requests:
    requests of a periodic kind are set aside up to their number, the rest is held. `cut` is True when
    the step's `by_path` (the 20 most frequent paths) does not add up to its count, so that a periodic
    request may be among the paths cut away and the set-aside cannot be computed."""
    count = rec.get("count")
    by_path = rec.get("by_path") or {}
    if count is None:
        return None, {}, False
    cut = bool(periodic) and sum(by_path.values()) != count
    aside = {k: min(by_path[k], n) for k, n in periodic.items() if by_path.get(k)}
    return count - sum(aside.values()), aside, cut


def gate_requests(fail, notes: list, r: dict, sid: str, s: dict, step: dict | None, opts: dict, rest: bool) -> None:
    """The request checks of one step: 'rest-requests' (a rest, against gate.rest_requests_max or the
    step's allow.requests_max) when the run was given a request log, and 'requests' (expect.requests_max,
    or launch_requests_max on the launch) on any step. Requests of a kind the scenario's gate block names
    in `periodic_requests` (a request an app sends on a timer, at any moment) are set aside up to their
    number wherever a limit or a budget applies; the rest is held against it. An unknown count fails
    wherever a limit applies."""
    block = r.get("requests")
    expect = (step or {}).get("expect") or {}
    if block is None:
        if "requests_max" in expect:
            fail("requests", sid, NO_REQUEST_LOG, allowed=expect["requests_max"])
        return
    rec = s.get("requests") or {}
    count = rec.get("count")
    why = rec.get("reason") or block.get("reason") or "the run holds no count for this step"
    by_path = rec.get("by_path") or {}
    periodic = opts.get("periodic_requests") or {}
    held, aside, cut = periodic_split(rec, periodic)
    set_aside = f"; {sum(aside.values())} of a periodic kind set aside: {describe_paths(aside)}" if aside else ""
    cut_why = (f"requests of a periodic kind could not be set aside: the run kept only the {len(by_path)} most "
               f"frequent paths of this step's {count} requests, so a periodic one may be among those cut")
    extra = {"set_aside": aside} if aside else {}
    failed = applies = False
    if rest:
        applies = True
        allow = (step or {}).get("allow") or {}
        limit = allow.get("requests_max", opts["rest_requests_max"])
        granted = "requests_max" in allow
        by = f" by the step's allowance ({allow.get('reason')})" if granted else ""
        gap = (f", {rec.get('in_gap')} of them in the {rec.get('gap_s')} s between the end of this rest and the next step"
               if rec.get("in_gap") else "")
        if count is None:
            failed = True
            fail("rest-requests", sid, f"requests at rest were not counted: {why}")
        elif cut:
            failed = True
            fail("rest-requests", sid, cut_why)
        elif held > limit:
            failed = True
            fail("rest-requests", sid, f"{_plural(held, 'request')} sent at rest "
                 f"({describe_paths(by_path)}{gap}{set_aside}), allowed {limit}{by}", observed=held, allowed=limit,
                 paths=by_path, **extra, **({"limit_from": "allowance"} if granted else {}))
        elif granted and held > opts["rest_requests_max"]:
            notes.append(f"{sid}: {held} requests sent at rest, above the limit of {opts['rest_requests_max']} that "
                         f"would otherwise apply; passed on the step's allowance of {limit} ({allow.get('reason')})")
    if "requests_max" in expect:
        applies = True
        n = expect["requests_max"]
        if count is None:
            failed = True
            fail("requests", sid, f"requests were not counted: {why}", allowed=n)
        elif cut:
            failed = True
            fail("requests", sid, cut_why, allowed=n)
        elif held > n:
            failed = True
            fail("requests", sid, f"{_plural(held, 'request')} ({describe_paths(by_path)}{set_aside}), "
                 f"approved at most {n}", observed=held, allowed=n, paths=by_path, **extra)
    if applies and aside and not failed:
        total = sum(aside.values())
        notes.append(f"{sid}: {total} request{'s' if total != 1 else ''} of a periodic kind set aside "
                     f"({describe_paths(aside)}), by the scenario's gate block (periodic_requests)")


# --------------------------------------------------------------------------------------------
# summary across runs
# --------------------------------------------------------------------------------------------

def describe(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return {"n": 0}
    out = {"n": len(vals), "min": min(vals), "max": max(vals), "median": statistics.median(vals),
           "mean": round(statistics.fmean(vals), 3), "distinct": len(set(vals))}
    if len(vals) > 1:
        sd = statistics.stdev(vals)
        out["stdev"] = round(sd, 3)
        out["cv_pct"] = round(100 * sd / out["mean"], 1) if out["mean"] else None
    return out


def failed_record(run_dir: Path) -> str | None:
    """What a run's failed.json says went wrong, or None when there is no such file."""
    p = run_dir / FAILED_RUN
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        error = data.get("error") if isinstance(data, dict) else None
        step = data.get("step") if isinstance(data, dict) else None
    except (OSError, ValueError) as exc:
        return f"its {FAILED_RUN} cannot be read ({exc})"
    if not error:
        return f"its {FAILED_RUN} holds no error"
    # A lookup failure already names its step at the start of its own message.
    named = f"step '{step}': "
    if step and str(error).startswith(named):
        return str(error)
    return (f"at step '{step}': " if step else "") + str(error)


def failed_scenario(run_dir: Path) -> str | None:
    """The scenario name a run's failed.json carries, if any."""
    try:
        data = json.loads((run_dir / FAILED_RUN).read_text())
    except (OSError, ValueError):
        return None
    return data.get("scenario") if isinstance(data, dict) and isinstance(data.get("scenario"), str) else None


def find_runs(dirs: list[str], rehearsals: list | None = None) -> list[tuple[Path, str | None, str | None]]:
    """Every run folder under dirs with the reason it cannot be used and the check that reason
    belongs to, or (path, None, None). Captures are enumerated, not analyses: a capture whose
    analysis is missing or older than the capture is reported ('analysis'), never skipped. A folder
    a run started but did not complete (one with failed.json, device-script.sh or raw/ and neither
    capture.json nor run.json) is reported too ('run'), so a batch with a failed run never passes
    on the runs that completed. A folder `ui-probe rehearse` wrote into is not a run and is passed
    over, with what lies inside it; rehearsals, when given, receives (folder, why it did not
    complete or None) for each."""
    skipped = rehearsal_folders(dirs)
    if rehearsals is not None:
        rehearsals.extend(skipped)
    found, started = set(), set()
    hits: list[tuple[Path, str]] = []
    for d in dirs:
        root = Path(d)
        if not root.exists():
            raise ProbeError(f"{d} does not exist")
        for name in ("capture.json", "run.json"):
            hits += [(p.parent, name) for p in root.glob(f"**/{name}")]
            found.update(p.parent for p in root.glob(f"**/{name}"))
        for name in (FAILED_RUN, "device-script.sh"):
            hits += [(p.parent, name) for p in root.glob(f"**/{name}")]
            started.update(p.parent for p in root.glob(f"**/{name}"))
        hits += [(p.parent, "raw") for p in root.glob("**/raw") if p.is_dir()]
        started.update(p.parent for p in root.glob("**/raw") if p.is_dir())

    def rehearsal_of(p: Path) -> Path | None:
        return next((f for f, _ in skipped if p == f or f in p.parents), None)

    # A rehearsal folder is passed over, but only its own raw/ and failed.json: any other run marker in
    # it (a run written inside it) stops here, since skipping it would hide that run.
    for parent, name in sorted(hits):
        f = rehearsal_of(parent)
        if f is None:
            continue
        own = (parent == f and name in ("raw", FAILED_RUN)) or parent == f / "raw" or (f / "raw") in parent.parents
        if not own:
            raise ProbeError(f"{parent} holds a run marker ({name}) inside the rehearsal folder {f}; a rehearsal folder "
                             "is passed over, so that run would not be read: move it out of the rehearsal folder")
    found = {p for p in found if rehearsal_of(p) is None}
    started = {p for p in started if rehearsal_of(p) is None}
    incomplete = started - found
    if not found and not incomplete:
        raise ProbeError("no run found (no capture.json, run.json, failed.json, device-script.sh or raw/) under "
                         + ", ".join(dirs)
                         + (f"; {len(skipped)} rehearsal folder{'s' if len(skipped) != 1 else ''} passed over"
                            if skipped else ""))
    out = []
    for run_dir in sorted(found | incomplete):
        if run_dir in incomplete:
            saved = failed_record(run_dir)
            out.append((run_dir, "the run did not complete" + (f": {saved}" if saved else
                                                               f" (no {FAILED_RUN}, capture.json or run.json)"), "run"))
            continue
        cap, res = run_dir / "capture.json", run_dir / "run.json"
        problem = None
        if not cap.exists():
            problem = "run.json without capture.json: the analysis cannot be traced to a capture"
        elif not res.exists():
            problem = "not analysed: the capture has no run.json (the analysis failed or never ran); run `ui-probe analyze`"
        elif res.stat().st_mtime_ns < cap.stat().st_mtime_ns:
            problem = "not analysed: run.json is older than capture.json; run `ui-probe analyze`"
        if problem:
            saved = failed_record(run_dir)
            if saved:
                problem += f"; the run reported: {saved}"
        out.append((run_dir, problem, "analysis" if problem else None))
    return out


def load_runs(dirs: list[str]) -> tuple[list[tuple[str, dict]], list[dict]]:
    """The analysed runs under dirs, and the runs that did not complete (left out, by name). Any
    other problem (a capture that was not analysed) is an error."""
    runs, incomplete = [], []
    for path, problem, check in find_runs(dirs):
        if check == "run":
            incomplete.append({"run": str(path), "problem": problem})
        elif problem:
            raise ProbeError(f"{path}: {problem}")
        else:
            runs.append((str(path), read_json(path / "run.json")))
    return runs, incomplete


def run_row(path: str, r: dict) -> dict:
    if r.get("platform") == SIM_PLATFORM:
        return sim_run_row(path, r)
    if r.get("platform") == FILM_PLATFORM:
        return film_run_row(path, r)
    row = {"run": Path(path).name, "host_load1": r["host"]["load1_before"],
           "launch_total_time_ms": r["launch"]["total_time_ms"]}
    for s in r["steps"]:
        sid = s["id"]
        row[f"{sid}.frames"] = s["frames"]
        if s.get("frames_layers") is not None:
            row[f"{sid}.frames_layers"] = s["frames_layers"]
            row[f"{sid}.frames_display"] = s["frames_display"]
        if "frames_compositor" in s:
            row[f"{sid}.frames_compositor"] = s["frames_compositor"]
        row[f"{sid}.janky"] = s["janky_frames"]
        row[f"{sid}.frame_ms_p50"] = s["frame_ms_p50"]
        row[f"{sid}.frame_ms_p90"] = s["frame_ms_p90"]
        row[f"{sid}.frame_ms_max"] = s["frame_ms_max"]
        row[f"{sid}.first_frame_ms"] = s["first_frame_completed_after_step_start_ms"]
        row[f"{sid}.cpu_ticks"] = s["cpu_ticks_total"]
        by_thread = s["cpu_ticks_by_thread"]
        row[f"{sid}.render_thread_ticks"] = by_thread.get("RenderThread", 0) if by_thread is not None else None
        row[f"{sid}.main_thread_ticks"] = by_thread.get("<main>", 0) if by_thread is not None else None
        if s.get("quiet"):
            row[f"{sid}.quiet"] = s["quiet"]
            row[f"{sid}.quiet_after_ms"] = s.get("quiet_after_ms")
        if "meminfo" in s:
            row[f"{sid}.pss_kb"] = s["meminfo"]["total_pss_kb"]
        for name, count in (s.get("sections") or {}).items():
            row[f"{sid}.sec.{name}"] = count
        if "sections_all" in s:
            row[f"{sid}.sec.ALL"] = s["sections_all"]
        if "requests" in s:
            row[f"{sid}.requests"] = (s["requests"] or {}).get("count")
    v = r.get("video")
    if v:
        row["video.frames"] = v["frames"]
        for seg in v["steps"]:
            sid = seg["id"]
            row[f"{sid}.video_frames"] = seg["video_frames"]
            row[f"{sid}.video_app_region_frames"] = seg["app_region_frames"]
            row[f"{sid}.video_identical_frames"] = seg["identical_frames"]
            row[f"{sid}.bursts"] = seg["bursts"]
            row[f"{sid}.signature"] = seg["signature"]
            row[f"{sid}.first_change_ms"] = seg["first_change_after_step_start_ms"]
            row[f"{sid}.settled_ms"] = seg["last_change_after_step_start_ms"]
            row[f"{sid}.max_gap_ms"] = seg["max_gap_in_burst_ms"]
            row[f"{sid}.detour_frames"] = seg["detour_frames"]
            row[f"{sid}.detour_ms"] = seg["detour_ms"]
    ft = r.get("frametimeline")
    if ft:
        row["frametimeline.app_buffer_frames"] = ft["app_buffer_frames"]
    return row


def summary(dirs: list[str]) -> dict:
    runs, incomplete = load_runs(dirs)
    if not runs:
        raise ProbeError("no run under " + ", ".join(dirs) + " completed: "
                         + "; ".join(f"{x['run']}: {x['problem']}" for x in incomplete))
    raw_rows = [run_row(path, r) for path, r in runs]
    keys = []
    for row in raw_rows:
        keys += [k for k in row if k != "run" and k not in keys]
    table = {}
    for k in keys:
        vals = [row.get(k) for row in raw_rows]
        if all(isinstance(v, (int, float)) or v is None for v in vals):
            table[k] = describe(vals)
        else:
            distinct = sorted({json.dumps(v) for v in vals})
            table[k] = {"n": len(vals), "distinct": len(distinct), "values": distinct[:6]}
    return {"runs": raw_rows, "incomplete": incomplete, "stats": table}


# --------------------------------------------------------------------------------------------
# gate: the checks that need no baseline, plus any expectations the scenario states
# --------------------------------------------------------------------------------------------

QUIET_MESSAGES = {
    "app": "the app kept drawing: no quiet period within the limit after '{sid}'",
    "display": "the display kept changing while the app was idle (a system animation, the keyboard, another "
               "window): no quiet period within the limit after '{sid}'. A surface the app draws outside its "
               "view system is not in the app's own count, so it is reported here too",
    "unattributed": "the display kept changing and the app's own frame count is not available, so whether the "
                    "app drew is unknown: no quiet period within the limit after '{sid}'",
    None: "no quiet period within the limit after '{sid}'",
}


def gate_run(r: dict, scn: dict, opts: dict) -> dict:
    """Evaluate one run against its scenario. Returns {'verdict', 'violations', 'notes'}. A violation
    is a fact about the app (or about a run that cannot be trusted); a note says a witness was
    unavailable while another one still decided. Every step of the scenario is checked; a step the
    run holds no record of is a violation."""
    violations, notes = [], []

    def fail(check, sid, message, **extra):
        violations.append({"check": check, "step": sid, "message": message, **extra})

    by_id = {s["id"]: s for s in scn["steps"]}
    records = {s["id"]: s for s in r.get("steps", [])}
    vblock = r.get("video") or {}
    video = {v["id"]: v for v in vblock.get("steps", [])}
    # Only the recorder's own per-frame timestamps place video frames into the step windows reliably.
    aligned = bool(vblock) and vblock.get("clock") == "recorder-timestamps" and not r.get("clock_mismatch_suspected")
    tck = (r.get("device") or {}).get("clk_tck") or 100

    if r.get("video_truncated"):
        fail("recording", None, "the scenario ran past the recording limit; later steps were not recorded. Split the scenario")
    if r.get("video_missing"):
        fail("recording", None, "a recording was requested but none was saved; the recording checks could not run")
    if r.get("video_error"):
        fail("recording", None, f"the recording could not be used: {r['video_error']}")
    if r.get("video_requested"):
        code = r.get("video_recorder_exit")
        if code is None:
            fail("recording", None, "the recorder's exit status was not recorded")
        elif code != 0:
            fail("recording", None, f"the recorder exited with status {code}; the recording may have stopped early",
                 observed=code)
        alive = r.get("video_recorder_alive_at_end")
        if alive is None:
            fail("recording", None, "whether the recorder was still running when the scenario ended was not recorded")
        elif not alive:
            fail("recording", None, "the recorder had stopped before the scenario ended; later steps were not recorded")
    if r.get("trace_missing"):
        fail("trace", None, "a trace was requested but none was saved")
    if r.get("trace_truncated"):
        notes.append("the trace file ends in a partial packet; it was read up to that point")
    trace_loss = r.get("trace_loss") or []
    if trace_loss:
        notes.append("the trace lost data (" + "; ".join(trace_loss) + "); its counts are unavailable")
    else:
        # A run from 0.7.0 on says which steps its ftrace events covered, apart from its frame timeline.
        sec_uncovered = r.get("sections_uncovered_steps")
        ft_uncovered = r.get("trace_uncovered_steps") or []
        for sid in ft_uncovered:
            if sec_uncovered is not None and sid not in sec_uncovered:
                notes.append(f"{sid}: the trace's frame timeline did not cover this step; its frame-timeline counts are "
                             "unavailable")
            else:
                notes.append(f"{sid}: the trace did not cover this step; its trace counts are unavailable")
        for sid in sec_uncovered or []:
            if sid not in ft_uncovered:
                notes.append(f"{sid}: the trace's ftrace events did not cover this step; its section counts are unavailable")
    if r.get("frametimeline_unavailable"):
        notes.append(f"{r['frametimeline_unavailable']}: the compositor's frame-timeline counts are unavailable, not 0")
    if r.get("sections_loss"):
        notes.append(f"{r['sections_loss']}; section counts are unavailable")
    launch = r.get("launch") or {}
    if launch.get("error"):
        fail("launch", "launch", f"the app did not start: {launch['error']}")
    elif launch.get("status") not in (None, "ok"):
        fail("launch", "launch", f"the launch did not complete (status: {launch['status']})")
    if vblock and not aligned:
        why = f"clock: {vblock.get('clock')}" + (", per-frame rows do not line up with the marks"
                                                  if r.get("clock_mismatch_suspected") else "")
        notes.append(f"the recording could not be aligned with the step marks ({why}); its at-rest findings are "
                     "notes, not violations, and signatures cannot be checked")

    order = ["launch"] + [st["id"] for st in scn["steps"]]
    front_unknown = []
    for sid in order:
        s = records.get(sid)
        if s is None:
            fail("missing-step", sid, f"the run holds no record of step '{sid}'")
            continue
        step = by_id.get(sid)
        want_kind = "launch" if step is None else step["do"]
        if s.get("kind") != want_kind:
            fail("scenario", sid, f"step '{sid}' is a {s.get('kind')} in the run but a {want_kind} in the scenario")
            continue
        if s.get("process_missing"):
            if "process id" in (s.get("snapshot_missing") or []):
                fail("process", sid, f"no process id was recorded at the end of '{sid}'")
            else:
                fail("process", sid, f"the app's process was not running at the end of '{sid}'")
        if step is not None and "find" in step:
            rec = s.get("find") or {}
            if not rec.get("centre_px"):
                fail("find", sid, f"the run holds no record of where {describe_find(step['find'])} was found, "
                     "so what the step tapped is unknown")
            elif rec.get("package") != find_package(scn, step):
                fail("find", sid, f"{describe_find(step['find'])} was matched in package {rec.get('package')!r}, "
                     f"not in {find_package(scn, step)!r}: the step may have tapped another app")
        if want_kind == "url" and not (s.get("url_open") or {}).get("opened"):
            fail("input", sid, f"the link was not opened: {(s.get('url_open') or {}).get('error') or 'the run holds no record of it'}")
        if s.get("process_ambiguous"):
            fail("process", sid, f"more than one process carried the app's name at the end of '{sid}' (a restart in progress)")
        if s.get("process_restarted"):
            fail("process", sid, "the app's process changed during the scenario (a crash or a kill)")
        if s.get("quiet") == "timeout":
            cause = s.get("quiet_timeout_cause")
            fail("quiet", sid, QUIET_MESSAGES.get(cause, QUIET_MESSAGES[None]).format(sid=sid), cause=cause)
        elif s.get("quiet") == "unavailable":
            notes.append(f"{sid}: frames could not be polled on this device; a fixed wait was used")
        if s["kind"] in ("launch", "rest"):
            # A rest window says something about the app's screen only while the app is in front.
            in_background = bool((step or {}).get("background", False))
            front = s.get("foreground")
            if front is None:
                front_unknown.append(sid)
            elif front and in_background:
                fail("foreground", sid, f"the app was in front at the end of '{sid}', but the step says it rests in the background")
            elif not front and not in_background:
                fail("foreground", sid, f"another app was in front at the end of '{sid}' ({s.get('foreground_component')}); "
                     "the app's screen was not being observed")
        if s["kind"] == "launch":
            counted, source = frame_count(s)
            if counted == 0:
                fail("launch", sid, f"no frame of the app was counted while it launched ({source} count): it drew nothing "
                     "in the launch window, or this count cannot see how it draws")
            elif counted is None and r.get("compositor_stats"):
                # The compositor's statistics work on this device and hold no layer of the app.
                fail("launch", sid, "the compositor saw no layer of the app while it launched: nothing of the app was on screen")
        if s["kind"] == "rest":
            # A step's allowance replaces, for this step only, the limit the command line, the
            # scenario's gate block or the default would set. Recorded frames that changed nothing
            # are allowed only by unchanged_frames_max; that check is what catches an invisible animation.
            allow = (step or {}).get("allow") or {}
            reason = allow.get("reason")

            def step_limit(key, otherwise):
                """(limit, the limit that would otherwise apply, whether the allowance set it)."""
                return (allow[key], otherwise, True) if key in allow else (otherwise, otherwise, False)

            def by_allowance(granted):
                return f" by the step's allowance ({reason})" if granted else ""

            def within_allowance(observed, otherwise, allowed, what):
                notes.append(f"{sid}: {observed} {what}, above the limit of {otherwise} that would otherwise apply; "
                             f"passed on the step's allowance of {allowed} ({reason})")

            frames_max, frames_otherwise, frames_allowed = step_limit("frames_max", opts["rest_frames_max"])
            changed_max, changed_otherwise, changed_allowed = step_limit("changed_frames_max", 0)
            unchanged_max, unchanged_otherwise, unchanged_allowed = step_limit("unchanged_frames_max", 0)
            cpu_max, cpu_otherwise, cpu_allowed = step_limit("cpu_pct_max", opts["rest_cpu_pct_max"])
            allowance = lambda granted: {"limit_from": "allowance"} if granted else {}
            counted, source = frame_count(s)
            vid = video.get(sid)
            # With another app in front the recording shows that app; it says nothing about this one.
            shows_app = s.get("foreground") is not False
            usable = vid is not None and aligned and shows_app
            if counted is None and not usable:
                why = ("and no recording" if vid is None else
                       "and a recording that shows another app" if not shows_app else
                       "and a recording that could not be aligned")
                fail("rest-frames", sid, "cannot count frames: no frame stats from the platform or the compositor, no trace " + why)
            if counted is not None and counted > frames_max:
                fail("rest-frames", sid, f"{counted} frames drawn at rest ({source} count), allowed {frames_max}"
                     + by_allowance(frames_allowed),
                     observed=counted, allowed=frames_max, source=source, **allowance(frames_allowed))
            elif counted is not None and frames_allowed and counted > frames_otherwise:
                within_allowance(counted, frames_otherwise, frames_max, f"frames drawn at rest ({source} count)")
            elif counted is None and usable:
                notes.append(f"{sid}: no platform frame count for this app; only the recording was checked")
            if allow.get("regions") and (vid is None or not shows_app):
                notes.append(f"{sid}: the step's regions were not checked: there is no recording of the app for it")
            if vid is not None and not shows_app:
                notes.append(f"{sid}: another app was in front, so the recording shows that app; it was not used for this step")
            elif vid is not None:
                findings = []
                # What the recording shows at rest is the app's doing only if the app presented a
                # frame. When every exact count of its frames is known and zero (the platform's and
                # the compositor's per-layer count, and a trace's if one was recorded), the display
                # composed for another layer (a status-bar change, say) and the recorder kept those
                # frames, some changing nothing, some carrying re-encoding noise in the app's area.
                # An animation nobody can see presents frames, so its counts are above zero and both
                # recording checks still apply. A step with its own changed-frames allowance is judged
                # against that allowance as before.
                presented_none = (s.get("frames") == 0 and s.get("frames_layers") == 0
                                  and not s.get("frames_compositor"))
                identical = vid["identical_frames"]
                real = vid.get("app_changed_frames_real", vid["app_region_frames"])
                regions = allow.get("regions")
                if regions:
                    # A change inside one of the step's regions counts against that region's own limit,
                    # not against the rest; one that crosses a region's edge counts against the rest.
                    rest_n, _why = region_counts(regions, vid)
                    if rest_n is not None:
                        real = rest_n
                    gate_regions(fail, notes, sid, regions, vid, aligned=aligned,
                                 what="recorded frames in the app's area")
                quiet_identical = identical > 0 and presented_none
                quiet_change = real > 0 and presented_none and not changed_allowed
                # A recorded frame that changed nothing is held against the app only as far as the app's own
                # account of its frames allows. That account (F) is the largest of the app-side counts, the one
                # the frame check uses: the platform's, the app's layers' and a trace's. A frame of a surface the
                # layers' count misses (a pop-up window whose layer name does not carry the package, on Android
                # 11 and older) is still in the platform's count, so it stays the app's. The app may own as many
                # such frames as F exceeds the recorded frames that changed its area (any change there, the
                # caret's included); only the rest can have been composed for another layer, and no more of them
                # than the display composed beyond F. Nothing is set aside unless the platform's count, the
                # layers' count, the display's count and A are all known counts (and a trace's, when there is one).
                display, layers, platform = s.get("frames_display"), s.get("frames_layers"), s.get("frames")
                trace = s.get("frames_compositor")
                area = vid.get("app_region_frames")
                other_layer, app_frames = 0, None
                if (not quiet_identical and _count(display) and _count(layers) and _count(platform) and _count(area)
                        and (trace is None or _count(trace))):
                    app_frames = max(platform, layers, trace or 0)
                    app_may_own = min(identical, max(0, app_frames - area))
                    other_layer = min(identical - app_may_own, max(0, display - app_frames))
                held = identical - other_layer
                set_aside = (f" ({other_layer} more set aside: the display composed {display} frames, the app "
                             f"{app_frames})" if other_layer else "")
                if other_layer:
                    was = lambda n: "was" if n == 1 else "were"
                    notes.append(f"{sid}: {_plural(other_layer, 'frame')} of {identical} composed at rest that changed "
                                 f"nothing on screen {was(other_layer)} set aside: the display composed {display} frames "
                                 f"and the app {app_frames} by the largest of its own counts, so "
                                 f"{_plural(display - app_frames, 'frame')} {was(display - app_frames)} composed for a "
                                 "layer that is not the app's")
                if held > unchanged_max and not quiet_identical:
                    findings.append((held, unchanged_max, unchanged_allowed,
                                     f"{held} frames were composed at rest that changed nothing on screen" + set_aside
                                     + (f", allowed {unchanged_max}{by_allowance(True)}" if unchanged_allowed else "")))
                elif unchanged_allowed and held > unchanged_otherwise and not quiet_identical:
                    within_allowance(held, unchanged_otherwise, unchanged_max,
                                     "recorded frames changed nothing on screen at rest" + set_aside)
                if quiet_change and quiet_identical:
                    notes.append(f"{sid}: {identical} frames were composed at rest that changed nothing on screen and the "
                                 f"screen changed in {real} recorded frames in the app's area, but the app presented no "
                                 "frame by any count (platform 0, compositor per layer 0), so the display composed them "
                                 "for a layer that is not the app's and the changes came from the recording's encoder; "
                                 f"not a violation. Look at them on sheets/{sid}.png and with `ui-probe zoom <run-dir> <a> <b>`")
                elif quiet_identical:
                    notes.append(f"{sid}: {identical} frames were composed at rest that changed nothing on screen, but the "
                                 "app presented no frame by any count (platform 0, compositor per layer 0), so the display "
                                 "composed them for a layer that is not the app's (a status-bar change, say); not a "
                                 f"violation. Look at them on sheets/{sid}.png and with `ui-probe zoom <run-dir> <a> <b>`")
                if quiet_change:
                    if not quiet_identical:      # otherwise the note above covers both
                        notes.append(f"{sid}: the screen changed at rest in {real} recorded frames in the app's area, but "
                                     "the app presented no frame by either count (platform 0, compositor per layer 0), so "
                                     "the change came from the recording's encoder or from a layer that is not the app's; "
                                     f"not a violation. Look at it on sheets/{sid}.png and with `ui-probe zoom <run-dir> <a> <b>`")
                elif real > changed_max:
                    findings.append((real, changed_max, changed_allowed,
                                     f"the screen changed at rest: {real} frames in the app's area"
                                     + (f", allowed {changed_max}{by_allowance(True)}" if changed_allowed else "")))
                elif changed_allowed and real > changed_otherwise:
                    within_allowance(real, changed_otherwise, changed_max, "recorded frames changed the app's area at rest")
                for observed, allowed, granted, message in findings:
                    if aligned:
                        fail("rest-video", sid, message, observed=observed, **({"allowed": allowed} if granted else {}),
                             **allowance(granted))
                    else:
                        notes.append(f"{sid}: {message} (the recording is not aligned with the step marks, so this is not a violation)")
            ticks = s.get("cpu_ticks_total")
            window_s = s.get("cpu_window_s")
            if ticks is None or not window_s or window_s <= 0:
                notes.append(f"{sid}: CPU at rest not available")
            else:
                cpu_pct = round(100 * ticks * (1000 / tck) / (window_s * 1000), 2)
                if cpu_pct > cpu_max:
                    fail("rest-cpu", sid, f"{cpu_pct}% of one core used at rest ({ticks} ticks), allowed {cpu_max}%"
                         + by_allowance(cpu_allowed),
                         observed=cpu_pct, allowed=cpu_max, **allowance(cpu_allowed))
                elif cpu_allowed and cpu_pct > cpu_otherwise:
                    within_allowance(f"{cpu_pct}%", f"{cpu_otherwise}%", f"{cpu_max}%", "of one core used at rest")
        expect = (step or {}).get("expect") or {}
        if "signature" in expect:
            got = (video.get(sid) or {}).get("signature")
            if got is None:
                fail("signature", sid, "no recording, so the signature could not be read")
            elif not aligned:
                fail("signature", sid, "the recording could not be aligned with the step marks, so the signature could not be read")
            elif got != expect["signature"]:
                fail("signature", sid, f"signature '{got}', approved '{expect['signature']}'",
                     observed=got, allowed=expect["signature"])
        count, source = frame_count(s)
        said = f"{count} frames ({source} count)" if count is not None else "no frame count is available"
        if "frames" in expect and count != expect["frames"]:
            fail("frames", sid, f"{said}, approved exactly {expect['frames']}", observed=count, allowed=expect["frames"], source=source)
        if "frames_max" in expect and (count is None or count > expect["frames_max"]):
            fail("frames", sid, f"{said}, approved at most {expect['frames_max']}", observed=count, allowed=expect["frames_max"],
                 source=source)
        for name, limit in (expect.get("sections_max") or {}).items():
            got = (s.get("sections") or {}).get(name)
            if got is None:
                if trace_loss:
                    msg = f"'{name}' was not counted: the trace lost data"
                elif r.get("sections_loss"):
                    msg = f"'{name}' was not counted: {r['sections_loss']}"
                elif name in (r.get("sections_not_observed") or []):
                    msg = f"'{name}' never appeared in the trace for this app; the build or the platform may not emit it"
                elif s.get("sections_covered", s.get("trace_covered")) is False:
                    msg = f"'{name}' was not counted: the trace did not cover this step"
                else:
                    msg = f"'{name}' was not counted; record with --sections and list it in count_sections"
                fail("sections", sid, msg)
            elif got > limit:
                fail("sections", sid, f"{got} '{name}' sections, approved at most {limit}", observed=got, allowed=limit)
        gate_requests(fail, notes, r, sid, s, step if step is not None else launch_budget(scn), opts,
                      rest=s["kind"] == "rest")
    for sid in records:
        if sid not in order:
            notes.append(f"{sid}: in the run but not in the scenario; not checked")
    if front_unknown:
        notes.append("which app was in front was not recorded at the end of: " + ", ".join(front_unknown)
                     + " (a capture from an older version, or a device that reports no resumed activity)")
    if r.get("clock_mismatch_suspected"):
        notes.append("per-frame rows do not line up with the step marks (the device may have slept since boot); frame "
                     "timings are unreliable and the recording cannot be placed in the step windows; frame counts are not affected")
    notes.extend(request_notes(r))
    return {"verdict": "FAIL" if violations else "PASS", "violations": violations, "notes": notes}


def gate(dirs: list[str], overrides: dict, scenario: dict | None = None) -> dict:
    if scenario is not None:
        validate_scenario(scenario, "any")      # strictly again for each run, once its platform is known
    out = {"tool": {"name": "ui-probe", "version": VERSION}, "runs": []}
    rehearsals: list = []
    for path, problem, check in find_runs(dirs, rehearsals):
        if problem:
            name = scenario["name"] if scenario else failed_scenario(path)
            out["runs"].append({"run": str(path), "scenario": name, "frame_sources": None, "limits": None, "allowances": None,
                                "allowances_from": None, "verdict": "FAIL", "notes": [],
                                "violations": [{"check": check, "step": None, "message": problem}]})
            continue
        cap = read_json(path / "capture.json")
        r = read_json(path / "run.json")
        stored = cap.get("scenario")
        if scenario is not None and isinstance(stored, dict) and stored.get("name") != scenario["name"]:
            # An override is the same scenario revised (a limit, an allowance), never another one.
            raise ProbeError(f"{path}: --scenario is '{scenario['name']}' but the run was captured with "
                             f"'{stored.get('name')}'; gate it with its own scenario")
        scn = scenario or stored
        if scn is None:
            raise ProbeError(f"{path}: the capture holds no scenario; pass --scenario")
        platform = cap.get("platform", ANDROID)
        try:
            validate_scenario(scn, platform)
        except ProbeError as exc:
            raise ProbeError(f"{path}: {exc}") from exc
        if (r.get("platform") or ANDROID) != platform:
            raise ProbeError(f"{path}: run.json is for {r.get('platform') or ANDROID} but the capture for {platform}; "
                             "run `ui-probe analyze`")
        opts = dict(GATE_DEFAULTS)
        opts.update(scn.get("gate") or {})
        opts.update({k: v for k, v in overrides.items() if v is not None})
        if platform == SIM_PLATFORM:
            res = gate_sim_run(r, scn, opts)
        elif platform == FILM_PLATFORM:
            res = gate_film_run(r, scn, opts)
        else:
            res = gate_run(r, scn, opts)
        if watch_min_ms(scn) < WATCH_MIN_MS:
            res["notes"].append(f"the scenario's watch_min_ms is {watch_min_ms(scn):g} ms, below the default of "
                                f"{WATCH_MIN_MS} ms: its watched rests may be too short to see a change made once a "
                                "minute")
        allowances = {s["id"]: s["allow"] for s in scn["steps"] if "allow" in s}
        out["runs"].append({"run": str(path), "scenario": scn["name"], "frame_sources": r.get("frame_sources") or [],
                            "limits": opts, "allowances": allowances,
                            "allowances_from": "--scenario" if scenario is not None else "capture", **res})
    out["verdict"] = "FAIL" if any(x["verdict"] == "FAIL" for x in out["runs"]) else "PASS"
    unfinished = [f"a rehearsal that did not complete was passed over (not a run): {folder}: {why}"
                  for folder, why in rehearsals if why is not None]
    if unfinished:
        out["notes"] = unfinished
    return out


# --------------------------------------------------------------------------------------------
# a film no run made: `ui-probe film <video> --windows <json> --out <dir>`
# --------------------------------------------------------------------------------------------

WINDOWS_FILE_KEYS = {"windows", "name", "insets_px", "watch_min_ms", "slow_frames", "slow_min_group", "explain_px"}


def load_windows(path: str, default_name: str) -> tuple[dict, list[int], dict]:
    """A windows file: a list of windows, or {"windows": [...], "name"?, "insets_px"? [top, bottom] in the
    film's pixels, "watch_min_ms"?}. A window: {"id", "start_s", "end_s", "watch"?, "allow"?}, the
    untouched stretches of the film to judge as rests. slow_frames and slow_min_group (whole numbers of
    at least 1) replace the slow comparisons' defaults. Returns the film's scenario, its insets and
    those options."""
    data = read_json(Path(path))
    if isinstance(data, list):
        data = {"windows": data}
    if not isinstance(data, dict):
        raise ProbeError(f"{path}: a windows file is a list of windows or an object with 'windows'")
    unknown = sorted(k for k in data if k not in WINDOWS_FILE_KEYS and not _is_comment(k))
    if unknown:
        raise ProbeError(f"{path}: unknown keys {unknown}; known: {', '.join(sorted(WINDOWS_FILE_KEYS))}")
    windows = data.get("windows")
    if not isinstance(windows, list) or not all(isinstance(w, dict) for w in windows):
        raise ProbeError(f"{path}: 'windows' must be a list of objects with 'id', 'start_s' and 'end_s'")
    insets = data.get("insets_px", [0, 0])
    if not (isinstance(insets, list) and len(insets) == 2
            and all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in insets)):
        raise ProbeError(f"{path}: 'insets_px' must be [top, bottom], whole numbers of the film's pixels, 0 or more")
    options = {}
    for key, least in (("slow_frames", 1), ("slow_min_group", 1), ("explain_px", 0)):
        if key in data:
            v = data[key]
            if not (isinstance(v, int) and not isinstance(v, bool) and v >= least):
                raise ProbeError(f"{path}: '{key}' must be a whole number of at least {least}")
            options[key] = v
    scn = {"film": True, "name": data.get("name", default_name),
           "steps": [{"do": "rest", **w} for w in windows]}
    if "watch_min_ms" in data:
        scn["watch_min_ms"] = data["watch_min_ms"]
    try:
        validate_scenario(scn, FILM_PLATFORM)
    except ProbeError as exc:
        raise ProbeError(f"{path}: {exc}") from exc
    return scn, insets, options


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def film_run(video: Path, windows_path: str, out: Path, name: str | None = None, pictures: bool = True) -> dict:
    """Judge the untouched windows of a film no run made, as rests, into a run folder that `gate` and
    `summary` read: capture.json (the film named by its path, size and SHA-256; the film is never
    copied), run.json, and, unless pictures is False, frames-full/ and sheets/ (then `zoom` and `sheet`
    read it too, from the film at its path)."""
    if not video.is_file():
        raise ProbeError(f"{video} is not a file")
    scn, insets, options = load_windows(windows_path, name or video.stem)
    if name:
        scn["name"] = name
    refuse_used(out)
    refuse_rehearsal_folder(out)
    out.mkdir(parents=True, exist_ok=True)
    try:
        source = video.resolve()
        (out / "capture.json").write_text(json.dumps({
            "tool": {"name": "ui-probe", "version": VERSION}, "platform": FILM_PLATFORM, "scenario": scn,
            "film": {"path": str(source), "bytes": source.stat().st_size, "sha256": file_sha256(source)},
            "pictures": pictures, "insets_px": insets, **options, "trace": False, "video": True, "sections": False,
        }, indent=1))
        return analyze_run(out)
    except BaseException as exc:
        write_failed(out, exc, scn["name"])
        raise


def film_source(run: Path, cap: dict) -> Path:
    """The film a film run was made from, at the path its capture names, checked against the size and
    SHA-256 recorded there: the film is never copied into the run folder."""
    film = cap.get("film") or {}
    path = Path(film.get("path") or "")
    if not film.get("path") or not path.is_file():
        raise ProbeError(f"{run}: the film {film.get('path')} named by capture.json is not there; a film run keeps "
                         "no copy of it")
    if path.stat().st_size != film.get("bytes") or file_sha256(path) != film.get("sha256"):
        raise ProbeError(f"{run}: the file at {path} is not the film this run was made from (its size or SHA-256 "
                         "differs from capture.json)")
    return path


def refuse_no_pictures(run: Path, cap: dict, verb: str) -> None:
    if cap.get("platform") == FILM_PLATFORM and cap.get("pictures") is False:
        raise ProbeError(f"{verb}: {run} was made with --no-pictures and holds no picture; {verb} would make one")


def analyze_film_run(out: Path, scn: dict, cap: dict) -> dict:
    sys.path.insert(0, str(HERE))
    import ui_video
    film = cap.get("film") or {}
    video = film_source(out, cap)
    pictures = cap.get("pictures", True) is not False
    windows = [{"id": st["id"], "start_s": st["start_s"], "end_s": st["end_s"],
                "regions": (st.get("allow") or {}).get("regions")} for st in scn["steps"]]
    try:
        res = ui_video.analyze_film(video, out, windows, cap.get("insets_px") or [0, 0], scn["name"], pictures,
                                    cap.get("slow_frames"), cap.get("slow_min_group"), cap.get("explain_px"))
    except ui_video.VideoError as exc:
        raise ProbeError(f"the film cannot be analysed: {exc}") from exc
    by_id = {st["id"]: st for st in scn["steps"]}
    result = {
        "tool": cap.get("tool", {"name": "ui-probe", "version": VERSION}), "platform": FILM_PLATFORM,
        "scenario": scn["name"], "package": None, "pictures": pictures,
        "film": {"path": film.get("path"), "bytes": film.get("bytes"), "sha256": film.get("sha256"),
                 **{k: res[k] for k in ("frames", "header_frames", "size", "duration_s", "frame_interval_ms_median",
                                        "insets_px", "app_rows", "block_px")}},
        "recording_floor": res["recording_floor"],
        "steps": [{**st, "watch": bool(by_id[st["id"]].get("watch"))} for st in res["steps"]],
        "frame_sources": ["film"], "video_requested": True, "trace_requested": False,
        "not_checked": list(FILM_NOT_CHECKED),
    }
    tmp = out / "run.json.tmp"
    tmp.write_text(json.dumps(result, indent=1))
    tmp.replace(out / "run.json")
    return result


def film_floor_lines(r: dict) -> list[str]:
    """What the film's analysis left out as the recording's own, and why, in sentences: the rule, the
    header's frame count when it differs, and per window the frames with only scattered changes, the
    largest scattered group beside the minimum (with a caution near it) and the steady places."""
    floor = r.get("recording_floor")
    if not floor:
        return ["the run holds no measure of the recording's floor"]
    lines = [floor["why"]]
    if floor.get("header_frames") is not None and floor["header_frames"] != floor.get("decoded_frames"):
        lines.append(f"the film's header states {floor['header_frames']} frames, but {floor['decoded_frames']} were "
                     "decoded; the decoded frames are the ones counted")
    for st in r.get("steps", []):
        rec = st.get("recording") or {}
        if "scattered" not in rec:
            continue
        sid = st["id"]
        lines.append(f"{sid}: {rec['scattered']} of {rec['frames']} frames held only scattered changes and were left "
                     f"out; the largest scattered group was {rec['largest_scattered_group_px']} px, the minimum for a "
                     f"change {rec['min_group_px']} px")
        if rec.get("caution"):
            lines.append(f"{sid}: caution: a scattered group of {rec['largest_scattered_group_px']} px reached "
                         f"{int(FILM_CAUTION_PCT)} % of the minimum or more; a small change of the app's may be near the "
                         "line, so look at the frames")
        slow = rec.get("slow") or {}
        if slow:
            lines.append(f"{sid}: each frame was also compared with the one {slow['frames_apart']} frames "
                         f"({slow['seconds_apart']:g} s) earlier, {slow['compared']} times: "
                         f"{rec.get('changed_by_slow_only', 0)} frames counted only by that comparison; its largest "
                         f"scattered group was {slow['largest_scattered_group_px']} px, the minimum "
                         f"{slow['min_group_px']} px")
            if slow.get("caution"):
                lines.append(f"{sid}: caution: in the comparison {slow['frames_apart']} frames apart a scattered group "
                             f"of {slow['largest_scattered_group_px']} px reached {int(FILM_CAUTION_PCT)} % of the "
                             "minimum or more")
        ends = rec.get("ends") or {}
        if ends:
            lines.append(f"{sid}: the window's last frame (#{ends['last_frame']}) against its first "
                         f"(#{ends['first_frame']}): " + ("they differ" if ends["differ"] else "they do not differ")
                         + f", the largest group {ends['largest_group_px']} px, the minimum {ends['min_group_px']} px")
            if ends.get("caution"):
                lines.append(f"{sid}: caution: between the window's ends a scattered group of "
                             f"{ends['largest_group_px']} px reached {int(FILM_CAUTION_PCT)} % of the minimum or more")
        for place in rec.get("steady_places") or []:
            lines.append(f"{sid}: pixels went over the level in at least a quarter of the frames at {place['box_px']} "
                         f"({place['pixels']} px; x0, y0, x1, y1 in the app's area), for information")
    return lines


def gate_film_run(r: dict, scn: dict, opts: dict) -> dict:
    """A film no run made: each window is a rest judged by the picture alone. A frame that changed the
    app's area (a connected group of changed pixels at the film's minimum; scattered pixels are the
    recording's) counts against the rest (check 'rest-video') or against the region it lies in (check
    'rest-region')."""
    violations, notes = [], []

    def fail(check, sid, message, **extra):
        violations.append({"check": check, "step": sid, "message": message, **extra})

    notes.append("a film no run made holds only the picture: " + ", ".join(FILM_NOT_CHECKED) + " are not checked")
    if r.get("pictures") is False:
        notes.append("made with --no-pictures: the run folder holds no picture and no copy of the film")
    film = r.get("film") or {}
    if list(film.get("insets_px") or [0, 0]) == [0, 0]:
        notes.append("the film's system bars were not given (insets_px), so the whole picture was taken as the app's area")
    notes.extend(film_floor_lines(r))
    records = {s["id"]: s for s in r.get("steps", [])}
    for step in scn["steps"]:
        sid = step["id"]
        s = records.get(sid)
        if s is None:
            fail("missing-step", sid, f"the run holds no record of window '{sid}'")
            continue
        rec = s.get("recording")
        if not rec or rec.get("error"):
            fail("recording", sid, "the window could not be read from the film" + (f": {rec['error']}" if rec else ""))
            continue
        allow = step.get("allow") or {}
        changed_max = allow.get("changed_frames_max", 0)
        drawing = rec["changed_app"]
        regions = allow.get("regions")
        if regions:
            rest_n, _why = region_counts(regions, rec)
            if rest_n is not None:
                drawing = rest_n
            gate_regions(fail, notes, sid, regions, rec, what="frames of the film")
        granted = "changed_frames_max" in allow
        look = (f"; look at sheets/{sid}-distinct-01.png and frames-full/{sid}/" if r.get("pictures", True) is not False
                else "; the run holds no picture (--no-pictures): run.json gives each frame's time and boxes")
        if drawing > changed_max:
            fail("rest-video", sid, f"the screen changed at rest: {drawing} frames of the film changed the app's area"
                 + (f", allowed {changed_max} by the step's allowance ({allow['reason']})" if granted else "") + look,
                 observed=drawing, **({"allowed": changed_max, "limit_from": "allowance"} if granted else {}))
        elif granted and drawing:
            notes.append(f"{sid}: {drawing} frames of the film changed the app's area at rest, above the limit of 0 that "
                         f"would otherwise apply; passed on the step's allowance of {changed_max} ({allow['reason']})")
        ends = rec.get("ends") or {}
        if ends.get("differ_unexplained"):
            fail("rest-video", sid, f"the window's last frame (#{ends['last_frame']}) differs from its first "
                 f"(#{ends['first_frame']}) by a group of {ends['largest_group_px']} px (the minimum is "
                 f"{ends['min_group_px']} px) that lies outside every change counted in the window and every "
                 "region: a slow change" + look, observed=ends["largest_group_px"])
    for sid in records:
        if sid not in {st["id"] for st in scn["steps"]}:
            notes.append(f"{sid}: in the run but not in the scenario; not checked")
    return {"verdict": "FAIL" if violations else "PASS", "violations": violations, "notes": notes}


def film_run_row(path: str, r: dict) -> dict:
    row = {"run": Path(path).name}
    for s in r["steps"]:
        sid, rec = s["id"], s.get("recording") or {}
        row[f"{sid}.film_frames"] = rec.get("frames")
        row[f"{sid}.film_still_frames"] = rec.get("still")
        row[f"{sid}.film_changed_app_frames"] = rec.get("changed_app")
        row[f"{sid}.film_scattered_frames"] = rec.get("scattered")
        row[f"{sid}.film_largest_scattered_group_px"] = rec.get("largest_scattered_group_px")
        row[f"{sid}.film_slow_only_frames"] = rec.get("changed_by_slow_only")
        row[f"{sid}.film_slow_largest_scattered_group_px"] = (rec.get("slow") or {}).get("largest_scattered_group_px")
        row[f"{sid}.film_ends_largest_group_px"] = (rec.get("ends") or {}).get("largest_group_px")
        row[f"{sid}.film_bars_only_frames"] = rec.get("bars_only")
        row[f"{sid}.film_signature"] = rec.get("signature")
        if "rest_changed_frames" in rec:
            row[f"{sid}.film_rest_changed_frames"] = rec["rest_changed_frames"]
        for k, reg in enumerate(rec.get("regions") or [], 1):
            row[f"{sid}.film_region{k}_changed_frames"] = reg["changed_frames"]
    return row


# --------------------------------------------------------------------------------------------
# sheet --full --distinct: full-resolution frames of each step's window, and sheets of the frames
# that changed the app's area
# --------------------------------------------------------------------------------------------

def sheet_extras(run: Path, full: bool, distinct: bool) -> list[str]:
    """After `sheet` has redone a run's analysis: with full, frames-full/<step>/f<index>.png for every
    recorded frame of each step's window and the last one before it; with distinct, sheets
    sheets/<step>-distinct-NN.png of only the frames that changed the app's area, PER_PAGE to a page,
    each labelled with its index and its time since the step's start. Refuses (ProbeError) when the
    recording's own frame count differs from the count the analysis read, or from the count extracted."""
    sys.path.insert(0, str(HERE))
    import ui_video
    from PIL import Image
    cap = read_json(run / "capture.json")
    r = read_json(run / "run.json")
    platform = cap.get("platform", ANDROID)
    if platform == FILM_PLATFORM:
        return ["a film's analysis writes frames-full/<window>/ (the frames that changed the app's area and the "
                "window's first and last frames) and sheets/<window>-distinct-NN.png itself; --full and --distinct add "
                "nothing"]
    jobs = []      # (step id, recording, frame list, first, last, base or None, seconds since the step's start, thumbs)
    if platform == SIM_PLATFORM:
        for s in r.get("steps", []):
            rec = s.get("recording")
            if s["kind"] == "launch" or not rec or rec.get("error"):
                continue
            sid = s["id"]
            frames = read_json(run / "video" / sid / "video-frames.json")
            if len(frames) < 2:
                continue
            t0 = frames[0]["pts"]
            jobs.append((sid, run / "raw" / f"rec-{sid}.mp4", frames, 1, len(frames) - 1, 0,
                         (lambda f0: (lambda i, fr: fr[i]["pts"] - f0))(t0), run / "video" / sid / "frames"))
    else:
        v = r.get("video")
        if not v:
            raise ProbeError(f"{run}: the run has no analysed recording, so there are no frames to write")
        frames = read_json(run / "video-frames.json")
        for seg in v["steps"]:
            if seg.get("first_frame") is None:
                continue
            a = seg["first_frame"]
            jobs.append((seg["id"], run / "raw" / "screen.mp4", frames, a, seg["last_frame"], a - 1 if a > 0 else None,
                         (lambda st: (lambda i, fr: fr[i]["t"] - st))(seg["start_s"]), run / "frames"))
    lines, counted = [], {}
    for sid, video, frames, a, b, base, since, thumbs in jobs:
        if video not in counted:
            try:
                own = len(ui_video.probe_pts(video))
            except ui_video.VideoError as exc:
                raise ProbeError(f"{video}: {exc}") from exc
            if own != len(frames):
                raise ProbeError(f"{video}: the recording's own list holds {own} frames but the analysis read "
                                 f"{len(frames)}; frames by index would not match the analysis, so nothing is written")
            counted[video] = own
        lo = a if base is None else base
        if full:
            try:
                written = ui_video.extract_full(video, list(range(lo, b + 1)), run / "frames-full" / sid)
            except ui_video.VideoError as exc:
                raise ProbeError(f"{sid}: {exc}") from exc
            lines.append(f"{sid}: {len(written)} full-resolution frames in frames-full/{sid}/ (frames {lo} to {b}"
                         + (f"; #{base} is the last frame before the step" if base is not None else "") + ")")
        if distinct:
            changed = [i for i in range(a, b + 1) if frames[i].get("app_changed")]
            thumb_files = sorted(thumbs.glob("f*.png"))
            tiles = []
            for i in changed:
                src = run / "frames-full" / sid / f"f{i:06d}.png" if full else thumb_files[i]
                tiles.append((Image.open(src), f"#{i} +{since(i, frames):.3f} s"))
            pages = ui_video.distinct_pages(tiles, run / "sheets", sid,
                                            f"{r.get('scenario')} / step {sid} - {len(changed)} frames changed the app's area")
            lines.append(f"{sid}: {len(changed)} frames changed the app's area: "
                         + (", ".join(f"sheets/{p}" for p in pages) if pages else "no sheet"))
    return lines


# --------------------------------------------------------------------------------------------
# compare: base against head, three-state verdict per metric
# --------------------------------------------------------------------------------------------

EXACT_MAX_N = 12          # the exact distribution of U is used up to this many values a side
_U_COUNTS: dict = {}


def _u_counts(n: int, m: int) -> list[int]:
    """Arrangements of n + m distinct values giving each value of U (pairs where the first sample is
    larger). The largest value is either from the first sample (it beats all m of the second) or not."""
    if n == 0 or m == 0:
        return [1]
    key = (n, m)
    if key not in _U_COUNTS:
        out = [0] * (n * m + 1)
        for u, c in enumerate(_u_counts(n - 1, m)):
            out[u + m] += c
        for u, c in enumerate(_u_counts(n, m - 1)):
            out[u] += c
        _U_COUNTS[key] = out
    return _U_COUNTS[key]


def mann_whitney(a: list[float], b: list[float]) -> tuple[float, float]:
    """Two-sided Mann-Whitney U. Returns (U, p). Without ties and with at most EXACT_MAX_N values a
    side, p is exact (from the distribution of U); otherwise a normal approximation with tie
    correction and continuity correction."""
    n, m = len(a), len(b)
    pooled = sorted([(v, 0) for v in a] + [(v, 1) for v in b])
    ranks = [0.0] * len(pooled)
    ties = 0.0
    i = 0
    while i < len(pooled):
        j = i
        while j + 1 < len(pooled) and pooled[j + 1][0] == pooled[i][0]:
            j += 1
        rank = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[k] = rank
        t = j - i + 1
        ties += t ** 3 - t
        i = j + 1
    r1 = sum(rank for rank, (_, side) in zip(ranks, pooled) if side == 0)
    u1 = r1 - n * (n + 1) / 2
    u = min(u1, n * m - u1)
    if ties == 0 and 0 < n <= EXACT_MAX_N and 0 < m <= EXACT_MAX_N:
        counts = _u_counts(n, m)
        tail = sum(counts[:int(round(u)) + 1])
        return u, min(1.0, 2 * tail / math.comb(n + m, n))
    total = n + m
    var = n * m / 12 * ((total + 1) - ties / (total * (total - 1)))
    if var <= 0:
        return u, 1.0
    z = (abs(u1 - n * m / 2) - 0.5) / math.sqrt(var)
    return u, max(0.0, min(1.0, math.erfc(max(z, 0.0) / math.sqrt(2))))


def cliffs_delta(a: list[float], b: list[float]) -> float:
    """(pairs where head > base minus pairs where head < base) / all pairs; a = base, b = head."""
    gt = sum(1 for x in a for y in b if y > x)
    lt = sum(1 for x in a for y in b if y < x)
    return (gt - lt) / (len(a) * len(b))


def compare_metric(base: list, head: list, alpha: float, min_runs: int) -> dict:
    a = [v for v in base if v is not None]
    b = [v for v in head if v is not None]
    out = {"n_base": len(a), "n_head": len(b)}
    if not a or not b:
        return {**out, "verdict": "unknown", "reason": "missing on one side"}
    out.update(median_base=statistics.median(a), median_head=statistics.median(b))
    # "Constant" is an observation only with at least two runs behind it (or one, if the caller
    # explicitly accepts single runs).
    enough = min(2, min_runs)
    if len(set(a)) == 1 and len(set(b)) == 1 and len(a) >= enough and len(b) >= enough:
        same = a[0] == b[0]
        return {**out, "verdict": "same" if same else "different",
                "reason": "constant on both sides and equal" if same else "constant on both sides and not equal"}
    if len(a) < min_runs or len(b) < min_runs:
        return {**out, "verdict": "unknown", "reason": f"fewer than {min_runs} runs on a side"}
    u, p = mann_whitney(a, b)
    delta = cliffs_delta(a, b)
    out.update(u=u, p=round(p, 5), cliffs_delta=round(delta, 3))
    # Effect-size bands for Cliff's delta: below 0.147 negligible, 0.474 and above large.
    if p < alpha and abs(delta) >= 0.474:
        return {**out, "verdict": "different", "reason": "significant and large"}
    if p >= alpha and abs(delta) < 0.147:
        return {**out, "verdict": "same", "reason": "not significant and negligible"}
    return {**out, "verdict": "unknown", "reason": "between the two"}


def compare(base_dirs: list[str], head_dirs: list[str], metrics: list[str], alpha: float, min_runs: int) -> dict:
    base_runs, base_incomplete = load_runs(base_dirs)
    head_runs, head_incomplete = load_runs(head_dirs)
    base = [run_row(p, r) for p, r in base_runs]
    head = [run_row(p, r) for p, r in head_runs]
    keys = metrics or sorted({k for row in base + head for k, v in row.items()
                              if k not in ("run", "host_load1") and isinstance(v, (int, float))})
    table = {}
    for k in keys:
        col_a, col_b = [row.get(k) for row in base], [row.get(k) for row in head]
        if all(v is None for v in col_a + col_b):
            # nothing was read on either side (a count that is unknown): never "same"
            table[k] = {"n_base": 0, "n_head": 0, "verdict": "unknown", "reason": "missing on both sides"}
        elif all(isinstance(v, str) or v is None for v in col_a + col_b):
            sa, sb = sorted({v for v in col_a if v}), sorted({v for v in col_b if v})
            table[k] = {"n_base": len(col_a), "n_head": len(col_b), "base": sa, "head": sb,
                        "verdict": "same" if sa == sb else "different", "reason": "set of values"}
        else:
            table[k] = compare_metric(col_a, col_b, alpha, min_runs)
    order = {"different": 0, "unknown": 1, "same": 2}
    return {"tool": {"name": "ui-probe", "version": VERSION}, "alpha": alpha, "min_runs": min_runs,
            "incomplete": {"base": base_incomplete, "head": head_incomplete},
            "metrics": dict(sorted(table.items(), key=lambda kv: (order[kv[1]["verdict"]], kv[0])))}


# --------------------------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------------------------

def ffmpeg_version(text: str) -> tuple[int, int] | None:
    """(major, minor) from `ffmpeg -version`'s first line, or None if it cannot be read (a build
    from a source snapshot, for instance, names a revision instead of a release)."""
    m = re.search(r"ffmpeg version n?(\d+)\.(\d+)", text)
    return (int(m.group(1)), int(m.group(2))) if m else None


def doctor(serial: str | None, simulator: str | None = None) -> dict:
    checks = []

    def add(name, ok, detail, required=True, warning=False):
        checks.append({"check": name, "ok": bool(ok), "required": required, "warning": warning, "detail": detail})

    def version_of(cmd):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            return (out.stdout or out.stderr).strip().splitlines()[0] if (out.stdout or out.stderr).strip() else ""
        except (OSError, subprocess.TimeoutExpired):
            return ""

    add("python", sys.version_info >= (3, 9), sys.version.split()[0])
    tools = (("adb", ["adb", "version"]), ("ffmpeg", ["ffmpeg", "-version"]), ("ffprobe", ["ffprobe", "-version"]))
    for tool, cmd in tools:
        if tool == "adb" and simulator:
            continue            # a simulator run needs no adb
        found = shutil.which(tool)
        detail = version_of(cmd) if found else "not on PATH"
        add(tool, bool(found), detail)
        if tool == "ffmpeg" and found:
            need = f"{FFMPEG_MIN[0]}.{FFMPEG_MIN[1]}"
            v = ffmpeg_version(detail)
            if v is None:
                add("ffmpeg version", False, f"cannot read a release number from '{detail}'; {need} or newer is needed",
                    required=False, warning=True)
            else:
                add("ffmpeg version", v >= FFMPEG_MIN, f"{v[0]}.{v[1]}; {need} or newer is needed")
    for mod in ("numpy", "PIL"):
        try:
            m = __import__(mod)
            add(mod, True, getattr(m, "__version__", "present"))
        except ImportError:
            add(mod, False, "not importable: the recording analysis needs numpy and Pillow")
    tp = shutil.which("trace_processor_shell") or shutil.which("trace_processor")
    add("trace processor (optional)", bool(tp), tp or "not installed; the built-in trace reader is used", required=False)
    if serial:
        try:
            state = adb(serial, "get-state", check=False).strip()
            add("device", state == "device", f"{serial}: {state or 'not found'}")
            if state == "device":
                add("device sdk", True, sh(serial, "getprop ro.build.version.sdk").strip(), required=False)
                add("device build type", True, sh(serial, "getprop ro.build.type").strip(), required=False)
                for tool in ("screenrecord", "perfetto", "dumpsys", "input", "pidof"):
                    path = sh(serial, f"command -v {tool}", check=False).strip()
                    add(f"device {tool}", bool(path), path or "missing", required=tool != "perfetto")
                # Needed only by a tap with "find"; a scenario without one runs without it.
                path = sh(serial, "command -v uiautomator", check=False).strip()
                add("device uiautomator (for find)", bool(path),
                    path or "missing: a tap with \"find\" needs it to read the window tree; other steps do not",
                    required=False)
        except (ProbeError, subprocess.TimeoutExpired) as exc:
            add("device", False, str(exc))
    if simulator:
        found = shutil.which("xcrun")
        add("xcrun", bool(found), found or "not on PATH (Xcode's command-line tools)")
        if found:
            try:
                xcrun("simctl", "help")
                add("xcrun simctl", True, "present")
                dev = sim_device(simulator)
                add("simulator", dev is not None, f"{simulator}: " + (f"{dev.get('name')}, {dev.get('runtime')}"
                                                                       if dev else "no such simulator"))
                if dev is not None:
                    add("simulator booted", dev.get("state") == "Booted", f"{simulator}: {dev.get('state')}")
            except ProbeError as exc:
                add("xcrun simctl", False, str(exc))
        # The input agent: needed only by a scenario with a tap or a swipe on a simulator.
        add("input agent source (for tap and swipe)", bool(agent_source_files()),
            str(AGENT_DIR) if agent_source_files() else f"missing ({AGENT_DIR}): reinstall the plugin", required=False)
        if shutil.which("xcodebuild") and agent_source_files():
            try:
                folder = agent_cache_root() / agent_cache_key(xcode_version())
                built = agent_built(folder)
                add("input agent built for this Xcode", built is not None,
                    str(built) if built else "not yet: it is built on the first run with a tap or a swipe "
                                              f"(into {folder})", required=False, warning=built is None)
            except ProbeError as exc:
                add("input agent built for this Xcode", False, str(exc), required=False, warning=True)
        else:
            add("input agent built for this Xcode", False, "xcodebuild is not on PATH (Xcode); a tap or a swipe on a "
                "simulator needs it", required=False, warning=True)
    ok = all(c["ok"] for c in checks if c["required"])
    return {"tool": {"name": "ui-probe", "version": VERSION}, "ok": ok, "checks": checks}


# --------------------------------------------------------------------------------------------
# command line
# --------------------------------------------------------------------------------------------

def write_csv(path: str, rows: list[dict]) -> None:
    import csv
    cols = ["run"]
    for row in rows:
        cols += [k for k in row if k not in cols]
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k) for k in cols})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ui-probe", description=__doc__.split("\n\n")[0])
    ap.add_argument("--version", action="version", version=f"ui-probe {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run a scenario on a device and analyse it")
    target = r.add_mutually_exclusive_group(required=True)
    target.add_argument("--serial", help="an Android device or emulator")
    target.add_argument("--simulator", metavar="UDID", help="a booted iOS Simulator")
    r.add_argument("--scenario", required=True)
    r.add_argument("--out", required=True, help="a directory that does not yet hold runs")
    r.add_argument("--runs", type=int, default=1)
    r.add_argument("--rehearsal", default=None, metavar="FILE",
                   help="a simulator scenario with a tap by 'find': the rehearsal.json of `ui-probe rehearse`")
    r.add_argument("--rehearsal-max-age-s", default=None, metavar="N",
                   help=f"refuse a rehearsal that finished more than N seconds ago (default {REHEARSAL_MAX_AGE_S})")
    r.add_argument("--requests", default=None, metavar="FILE",
                   help="count the app's requests per step from this log of a running front "
                        "(`ui-probe http-front --log` or `tls-front --log`)")
    h = sub.add_parser("rehearse", help="drive a simulator scenario once, with lookups and typing, in an app process "
                                        "that is then ended; nothing is measured")
    h.add_argument("--simulator", metavar="UDID", default=None, help="a booted iOS Simulator")
    h.add_argument("--serial", default=None, help=argparse.SUPPRESS)       # refused with a message of its own
    h.add_argument("--scenario", required=True)
    h.add_argument("--out", required=True, help="a directory that holds no run and no rehearsal")
    r.add_argument("--trace", action="store_true", help="record the compositor's frame timeline")
    r.add_argument("--sections", action="store_true", help="also record trace sections (implies --trace)")
    r.add_argument("--no-video", action="store_true")
    a = sub.add_parser("analyze", help="redo the analysis of saved runs; no device needed")
    a.add_argument("dirs", nargs="+")
    a.add_argument("--scenario", default="", help="override the scenario stored with the run")
    s = sub.add_parser("summary", help="statistics across runs")
    s.add_argument("dirs", nargs="+")
    s.add_argument("--json", default="")
    s.add_argument("--csv", default="", help="write one row per run with every raw value")
    g = sub.add_parser("gate", help="check saved runs; exit 1 on a violation")
    g.add_argument("dirs", nargs="+")
    g.add_argument("--scenario", default="", help="override the scenario stored with the run")
    g.add_argument("--rest-frames-max", type=int, default=None)
    g.add_argument("--rest-cpu-pct-max", type=float, default=None)
    g.add_argument("--advisory", action="store_true", help="report violations but exit 0")
    g.add_argument("--json", default="")
    c = sub.add_parser("compare", help="base runs against head runs: different, same or unknown per metric")
    c.add_argument("--base", nargs="+", required=True)
    c.add_argument("--head", nargs="+", required=True)
    c.add_argument("--metric", action="append", default=[], help="a summary key; repeat. Default: every numeric key")
    c.add_argument("--alpha", type=float, default=0.01)
    c.add_argument("--min-runs", type=int, default=5)
    c.add_argument("--json", default="")
    t = sub.add_parser("sheet", help="redo the analysis of one saved run: rewrites run.json, video-frames.json, "
                                     "frames/ and the contact sheets")
    t.add_argument("run_dir")
    t.add_argument("--full", action="store_true", help="also write every recorded frame of each step's window, and the "
                                                       "last one before it, at full resolution under frames-full/<step>/")
    t.add_argument("--distinct", action="store_true", help="also write sheets of only the frames that changed the "
                                                           "app's area, 24 to a page: sheets/<step>-distinct-NN.png")
    f = sub.add_parser("film", help="judge the untouched windows of a film no run made (a phone filmed by another "
                                    "machine) as rests, into a run folder gate, summary and zoom read")
    f.add_argument("video")
    f.add_argument("--windows", required=True, help="a JSON file: the windows (id, start_s, end_s) to judge")
    f.add_argument("--out", required=True, help="a directory that does not yet hold a run")
    f.add_argument("--name", default=None, help="the name the run carries (default: the windows file's name, else the "
                                                "film's file name)")
    f.add_argument("--no-pictures", action="store_true", help="write no picture of any kind into the run folder (no "
                                                              "frames, no sheets); every number is kept")
    z = sub.add_parser("zoom", help="full-resolution before/after/difference of two recorded frames")
    z.add_argument("run_dir")
    z.add_argument("a", type=int, help="frame index as printed on a sheet (#N)")
    z.add_argument("b", type=int)
    z.add_argument("--out", default="")
    z.add_argument("--step", default="", help="a simulator run: the rest step whose recording holds the frames")
    sub.add_parser("proxy", help="a shaping forward proxy: latency and a rate limit per connection", add_help=False)
    sub.add_parser("tls-front", help="TLS in front of a plain local backend, for an app on the iOS Simulator",
                   add_help=False)
    sub.add_parser("http-front", help="a plain front for a plain local backend, for an app on an Android emulator; "
                                      "with --log it logs the requests it pipes", add_help=False)
    d = sub.add_parser("doctor", help="check the host tools, and a device if --serial or --simulator is given")
    dt = d.add_mutually_exclusive_group()
    dt.add_argument("--serial", default=None)
    dt.add_argument("--simulator", default=None, metavar="UDID")

    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        if argv[:1] == ["tls-front"]:
            sys.path.insert(0, str(HERE))
            import tlsfront
            return tlsfront.main(argv[1:])
        if argv[:1] == ["http-front"]:
            sys.path.insert(0, str(HERE))
            import httpfront
            return httpfront.main(argv[1:])
        if argv[:1] == ["proxy"]:
            sys.path.insert(0, str(HERE))
            import shapeproxy
            try:
                return shapeproxy.main(argv[1:])
            except OSError as exc:
                raise ProbeError(f"proxy: {exc.strerror or exc}") from exc
        args = ap.parse_args(argv)
        return _dispatch(args)
    except ProbeError as exc:
        print(f"ui-probe: {exc}", file=sys.stderr)
        return 2
    except Exception:
        traceback.print_exc()
        print("ui-probe: unexpected error (exit 3); the traceback above is a defect in the tool", file=sys.stderr)
        return 3


def rehearsal_for_run(args, scn: dict) -> dict | None:
    """The checked rehearsal a simulator run replays its taps with 'find' from, or None when the scenario
    has none. Every refusal comes before the first run starts."""
    finds = sim_find_steps(scn)
    if args.rehearsal_max_age_s is not None and args.rehearsal is None:
        raise ProbeError("--rehearsal-max-age-s applies only with --rehearsal")
    if args.rehearsal is None:
        if finds:
            raise ProbeError(no_rehearsal_message(scn, args.simulator, args.scenario))
        return None
    if not finds:
        raise ProbeError("--rehearsal applies only to a scenario with a tap by 'find'; this one has none, so there is "
                         "nothing to replay")
    max_age = REHEARSAL_MAX_AGE_S
    if args.rehearsal_max_age_s is not None:
        text = str(args.rehearsal_max_age_s).strip()
        if not re.fullmatch(r"[0-9]+", text) or int(text) < 1:
            raise ProbeError(f"--rehearsal-max-age-s must be a whole number of seconds of at least 1, not {text!r}")
        max_age = int(text)
    rehearsal = load_rehearsal(args.rehearsal)
    check_rehearsal(rehearsal, scn, args.simulator, max_age, sim_app_identity(args.simulator, scn["package"]),
                    time.time())
    return rehearsal


def _dispatch(args) -> int:
    if args.cmd == "rehearse":
        if args.serial is not None:
            raise ProbeError("rehearse is for the iOS Simulator (--simulator); on Android a lookup happens inside the run, "
                             "so there is nothing to rehearse")
        if args.simulator is None or not args.simulator.strip():
            raise ProbeError("rehearse needs --simulator with the udid of a booted iOS Simulator")
        scn = load_scenario(args.scenario)
        out = Path(args.out)
        rehearse(args.simulator, scn, out, emit=lambda line: print(json.dumps(line), flush=True))
        print(json.dumps({"rehearsal": str(out / REHEARSAL_FILE)}), flush=True)
    elif args.cmd == "run":
        scn = load_scenario(args.scenario)
        for flag, value in (("--serial", args.serial), ("--simulator", args.simulator)):
            if value is not None and not value.strip():
                raise ProbeError(f"{flag} needs a value; an empty one names no device")
        rehearsal = None
        if args.simulator:
            unsupported = [flag for flag, on in (("--trace", args.trace), ("--sections", args.sections),
                                                 ("--no-video", args.no_video)) if on]
            if unsupported:
                raise ProbeError(f"{', '.join(unsupported)} cannot be used with --simulator: there is no trace on the iOS "
                                 "Simulator, and the recording is its only witness of frames")
            validate_scenario(scn, SIM_PLATFORM)
            rehearsal = rehearsal_for_run(args, scn)
        else:
            if args.rehearsal is not None or args.rehearsal_max_age_s is not None:
                raise ProbeError("--rehearsal and --rehearsal-max-age-s apply only with --simulator: on Android a "
                                 "lookup happens inside the run")
            validate_scenario(scn)
        # Refused before a run folder exists: the log must be a running front's.
        requests = check_request_log(args.requests) if args.requests is not None else None
        base = Path(args.out)
        refuse_rehearsal_folder(base)
        for i in range(1, args.runs + 1):
            rid = f"run-{i:02d}"
            if args.simulator:
                res = run_once_sim(args.simulator, scn, base / rid, rehearsal, requests)
                line = {"run": rid, "load": res["host"]["load1_before"]}
                for st in res["steps"]:
                    if st["kind"] == "rest":
                        line[st["id"]] = {"cpu_pct": st["cpu_pct"], "frames": (st.get("recording") or {}).get("frames")}
                    elif st["kind"] == "url":
                        opened = st.get("url_open") or {}
                        line[st["id"]] = {"signature": st.get("signature"), "ok": opened.get("opened"),
                                          "confirmed": opened.get("alert_confirmed")}
                    elif st["kind"] in SIM_INPUT_KINDS:
                        reply = (st.get("agent") or {}).get("reply") or {}
                        line[st["id"]] = {"signature": st.get("signature"), "ok": reply.get("ok")}
                print(json.dumps(line), flush=True)
                continue
            res = run_once(args.serial, scn, base / rid, args.trace or args.sections, not args.no_video, args.sections,
                           requests)
            line = {"run": rid, "load": res["host"]["load1_before"], "launch_ms": res["launch"]["total_time_ms"]}
            for st in res["steps"]:
                line[st["id"]] = frame_count(st)[0]
            print(json.dumps(line), flush=True)
    elif args.cmd in ("analyze", "sheet"):
        scn = load_scenario(args.scenario) if getattr(args, "scenario", "") else None
        dirs = args.dirs if args.cmd == "analyze" else [args.run_dir]
        found = [cap.parent for d in dirs for cap in sorted(Path(d).glob("**/capture.json"))]
        if not found:
            raise ProbeError("no capture.json found under " + ", ".join(dirs))
        if args.cmd == "sheet":
            for run in found:
                refuse_no_pictures(run, read_json(run / "capture.json"), "sheet")
        for run in found:
            try:
                analyze_run(run, scn)
            except ProbeError as exc:
                raise ProbeError(f"{run}: analysis failed: {exc}") from exc
            print("analysed", run if args.cmd == "analyze" else run / "sheets")
            if args.cmd == "sheet" and (args.full or args.distinct):
                for line in sheet_extras(run, args.full, args.distinct):
                    print(line)
    elif args.cmd == "film":
        res = film_run(Path(args.video), args.windows, Path(args.out), args.name, pictures=not args.no_pictures)
        for st in res["steps"]:
            rec = st["recording"]
            print(json.dumps({"window": st["id"], "seconds": st["window_s"], "frames": rec["frames"],
                              "changed_app": rec["changed_app"], "scattered": rec["scattered"], "still": rec["still"],
                              "bars_only": rec["bars_only"], "largest_scattered_group_px": rec["largest_scattered_group_px"],
                              "min_group_px": rec["min_group_px"], "signature": rec["signature"],
                              **({"rest_changed": rec["rest_changed_frames"],
                                  "regions": [x["changed_frames"] for x in rec["regions"]]} if "regions" in rec else {})}))
        for line in film_floor_lines(res):
            print(f"left out: {line}")
        print(f"film: {args.out} (gate it with `ui-probe gate {args.out}`)")
    elif args.cmd == "summary":
        out = summary(args.dirs)
        for x in out["incomplete"]:
            print(f"incomplete, left out of every statistic: {x['run']}: {x['problem']}")
        if args.json:
            Path(args.json).write_text(json.dumps(out, indent=1))
        if args.csv:
            write_csv(args.csv, out["runs"])
        for k, v in out["stats"].items():
            print(f"{k:48s} {json.dumps(v)}")
    elif args.cmd == "gate":
        scn = load_scenario(args.scenario) if args.scenario else None
        out = gate(args.dirs, {"rest_frames_max": args.rest_frames_max, "rest_cpu_pct_max": args.rest_cpu_pct_max}, scn)
        if args.json:
            Path(args.json).write_text(json.dumps(out, indent=1))
        for run in out["runs"]:
            if run["frame_sources"] is None:
                how = "not analysed"
            else:
                how = "frames counted by: " + (", ".join(run["frame_sources"]) or "recording only")
            print(f"{run['verdict']}  {run['run']}  ({run['scenario'] or '?'}; {how})")
            for v in run["violations"]:
                print(f"  FAIL [{v['check']}] {v['step'] or '-'}: {v['message']}")
            for n in run["notes"]:
                print(f"  note: {n}")
            source = " (from --scenario, not the scenario stored with the run)" \
                if run.get("allowances_from") == "--scenario" else ""
            for sid, allow in (run.get("allowances") or {}).items():
                granted = ", ".join(f"{k} {allow[k]}" for k in ALLOW_LIMITS if k in allow)
                if granted:
                    print(f"  allowance: {sid}: {granted} ({allow['reason']}){source}")
                for k, reg in enumerate(allow.get("regions") or [], 1):
                    print(f"  allowance: {sid}: region {k} {reg['region']}: changed_frames_max {reg['changed_frames_max']} "
                          f"({reg['reason']}){source}")
        for n in out.get("notes") or []:
            print(f"note: {n}")
        print(f"gate: {out['verdict']}" + (" (advisory)" if args.advisory and out["verdict"] == "FAIL" else ""))
        return 1 if out["verdict"] == "FAIL" and not args.advisory else 0
    elif args.cmd == "compare":
        out = compare(args.base, args.head, args.metric, args.alpha, args.min_runs)
        for side in ("base", "head"):
            if out["incomplete"][side]:
                print(f"note: {side}: left out {len(out['incomplete'][side])} incomplete runs: "
                      + ", ".join(x["run"] for x in out["incomplete"][side]))
        if args.json:
            Path(args.json).write_text(json.dumps(out, indent=1))
        for k, v in out["metrics"].items():
            extra = f"median {v.get('median_base')} -> {v.get('median_head')}" if "median_base" in v else \
                    (f"{v.get('base')} -> {v.get('head')}" if "base" in v else "")
            # how many values each side had: a run whose value is unknown is left out of the metric
            print(f"{v['verdict']:9s} {k:44s} {extra}  ({v['reason']}; values: {v.get('n_base')} base, "
                  f"{v.get('n_head')} head)")
    elif args.cmd == "zoom":
        sys.path.insert(0, str(HERE))
        import ui_video
        run = Path(args.run_dir)
        cap = read_json(run / "capture.json") if (run / "capture.json").exists() else {}
        sim = cap.get("platform") == SIM_PLATFORM
        film = cap.get("platform") == FILM_PLATFORM
        refuse_no_pictures(run, cap, "zoom")
        if sim and not args.step:
            raise ProbeError("zoom: a simulator run has one recording per rest step; pass --step <id> (the frame "
                             "numbers are those on sheets/<id>.png)")
        if args.step and not sim:
            raise ProbeError("zoom: --step applies only to a simulator run; this run has one recording")
        video = film_source(run, cap) if film else run / "raw" / (f"rec-{args.step}.mp4" if sim else "screen.mp4")
        out = Path(args.out) if args.out else run / "sheets" / (f"zoom-{args.step}-{args.a}-{args.b}.png" if sim
                                                                else f"zoom-{args.a}-{args.b}.png")
        try:
            res = ui_video.zoom(video, args.a, args.b, out)
        except ui_video.VideoError as exc:
            raise ProbeError(f"zoom: {exc}") from exc
        print(json.dumps(res))
    elif args.cmd == "doctor":
        for flag, value in (("--serial", args.serial), ("--simulator", args.simulator)):
            if value is not None and not value.strip():
                raise ProbeError(f"{flag} needs a value; an empty one names no device")
        out = doctor(args.serial or None, args.simulator or None)
        for chk in out["checks"]:
            state = "ok  " if chk["ok"] else ("WARN" if chk.get("warning") else ("MISS" if chk["required"] else "none"))
            print(f"{state} {chk['check']}: {chk['detail']}")
        return 0 if out["ok"] else 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
