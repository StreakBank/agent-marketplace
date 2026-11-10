#!/usr/bin/env python3
"""ui-probe: watch a mobile app from outside while a scripted scenario runs.

The probe knows nothing about the app under test. A scenario file supplies the package, the
launch component and a list of steps in normalized screen coordinates. Everything is read through
adb from an unmodified build:

  * frame counts and per-frame timing  : dumpsys gfxinfo <package> framestats   (HWUI apps)
  * compositor frame counts per layer  : dumpsys SurfaceFlinger --timestats     (any toolkit)
  * compositor jank classes            : Perfetto FrameTimeline (--trace; any toolkit)
  * trace sections                     : android.os.Trace sections (--sections; names from the scenario)
  * CPU time                           : /proc/<pid>/stat (the process, including threads that have
                                         exited) and /proc/<pid>/task/*/stat (the live threads)
  * memory                             : dumpsys meminfo <package>
  * what the screen showed             : screenrecord, one video frame per screen change

Three kinds of result, three rules:
  exact       counts (frames while nothing happens, CPU at rest, frames for a step that waits on
              nothing): gate them, no tolerance.
  structural  the order of visual states a step passes through (its signature): gate against an
              approved signature.
  statistical every duration, and any count that time or the network drives: report the
              distribution, never gate on an emulator or simulator.

Commands: run, analyze, summary, gate, compare, sheet, zoom, proxy, doctor. See README.md.
Exit codes: 0 success, 1 a gate failed, 2 an error the tool reports itself, 3 an unexpected error.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
import traceback
from pathlib import Path

VERSION = "0.1.0"
HERE = Path(__file__).resolve().parent
DEV_DIR = "/data/local/tmp/uiprobe"
RECORD_LIMIT_S = 180          # screenrecord's documented maximum
STEP_KINDS = ("rest", "tap", "swipe", "key")
RESERVED_IDS = ("launch", "T0", "END")
FFMPEG_MIN = (5, 1)           # -fps_mode, used to extract recorded frames, arrived in FFmpeg 5.1

# Scenario keys. A key outside these sets is an error (a typo would otherwise be ignored and change
# what is gated), except a key whose name ends in "comment".
PACKAGE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+")
ACTIVITY_RE = re.compile(r"[A-Za-z0-9_.$]+")
TOP_KEYS = {"name", "package", "activity", "until_quiet", "quiet", "launch_settle_ms", "steps",
            "count_sections", "gate"}
STEP_KEYS = {
    "rest": {"ms", "meminfo", "background"},
    "tap": {"x", "y", "settle_ms", "until_quiet"},
    "swipe": {"from", "to", "ms", "judge", "settle_ms", "until_quiet"},
    "key": {"code", "settle_ms", "until_quiet"},
}
COMMON_STEP_KEYS = {"id", "do", "expect"}
EXPECT_KEYS = {"signature", "frames", "frames_max", "sections_max"}

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

GATE_DEFAULTS = {"rest_frames_max": 0, "rest_cpu_pct_max": 2.0}

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

def quiet_settings(scn: dict) -> tuple[int, int]:
    q_ = scn.get("quiet") or {}
    return int(q_.get("ms", 800)), int(q_.get("max_ms", 15000))


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


def validate_scenario(scn: dict) -> None:
    """Reject a malformed scenario with a message that names the problem. No silent defaults."""
    def need(cond, msg):
        if not cond:
            raise ProbeError(f"scenario: {msg}")

    number = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
    count = lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0
    unit = lambda v: number(v) and 0 <= v <= 1

    need(isinstance(scn, dict), "must be a JSON object")
    unknown = sorted(k for k in scn if k not in TOP_KEYS and not _is_comment(k))
    need(not unknown, f"unknown keys {unknown}")
    need(isinstance(scn.get("name"), str) and scn["name"], "'name' is required")
    need(isinstance(scn.get("package"), str) and scn["package"], "'package' is required")
    need(PACKAGE_RE.fullmatch(scn["package"]),
         "'package' must be an application id: dot-separated names of letters, digits and '_', such as com.example.app")
    if "activity" in scn:
        need(isinstance(scn["activity"], str) and ACTIVITY_RE.fullmatch(scn["activity"]),
             "'activity' must be letters, digits, '_', '.' or '$'")
    if "until_quiet" in scn:
        need(isinstance(scn["until_quiet"], bool), "'until_quiet' must be true or false")
    steps = scn.get("steps")
    need(isinstance(steps, list) and steps, "'steps' must be a non-empty list")
    need(number(scn.get("launch_settle_ms", 0)) and scn.get("launch_settle_ms", 0) >= 0, "'launch_settle_ms' must be a number >= 0")
    quiet = scn.get("quiet", {})
    need(isinstance(quiet, dict), "'quiet' must be an object with 'ms' and 'max_ms'")
    unknown = sorted(k for k in quiet if k not in ("ms", "max_ms"))
    need(not unknown, f"unknown 'quiet' keys {unknown}")
    for k in ("ms", "max_ms"):
        if k in quiet:
            need(number(quiet[k]) and quiet[k] >= 100, f"'quiet.{k}' must be a number of at least 100")
    need(quiet.get("max_ms", 15000) >= quiet.get("ms", 800), "'quiet.max_ms' must be at least 'quiet.ms'")
    if "count_sections" in scn:
        sec = scn["count_sections"]
        need(isinstance(sec, list) and all(isinstance(n, str) and n for n in sec),
             "'count_sections' must be a list of non-empty strings")
    if "gate" in scn:
        g = scn["gate"]
        need(isinstance(g, dict), "'gate' must be an object")
        unknown = sorted(k for k in g if k not in GATE_DEFAULTS)
        need(not unknown, f"unknown 'gate' keys {unknown}; known: {', '.join(GATE_DEFAULTS)}")
        if "rest_frames_max" in g:
            need(count(g["rest_frames_max"]), "'gate.rest_frames_max' must be an integer >= 0")
        if "rest_cpu_pct_max" in g:
            need(number(g["rest_cpu_pct_max"]) and g["rest_cpu_pct_max"] >= 0, "'gate.rest_cpu_pct_max' must be a number >= 0")
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
        if do == "rest":
            need(number(step.get("ms")) and step["ms"] > 0, f"step '{sid}': rest needs 'ms' > 0")
            for k in ("meminfo", "background"):
                if k in step:
                    need(isinstance(step[k], bool), f"step '{sid}': '{k}' must be true or false")
        elif do == "tap":
            need(unit(step.get("x")) and unit(step.get("y")), f"step '{sid}': tap needs 'x' and 'y' between 0 and 1")
        elif do == "swipe":
            need(number(step.get("ms", 300)) and step.get("ms", 300) > 0, f"step '{sid}': swipe 'ms' must be a number > 0")
            for end in ("from", "to"):
                pt = step.get(end)
                need(isinstance(pt, list) and len(pt) == 2 and unit(pt[0]) and unit(pt[1]),
                     f"step '{sid}': swipe needs '{end}' as [x, y] between 0 and 1")
            need(step.get("judge", "motion") in ("motion", "state"), f"step '{sid}': 'judge' is 'motion' or 'state'")
        elif do == "key":
            need(isinstance(step.get("code"), int) and not isinstance(step.get("code"), bool), f"step '{sid}': key needs an integer 'code'")
        if "expect" in step:
            exp = step["expect"]
            need(isinstance(exp, dict), f"step '{sid}': 'expect' must be an object")
            unknown = set(exp) - EXPECT_KEYS
            need(not unknown, f"step '{sid}': unknown 'expect' keys {sorted(unknown)}")
            if "signature" in exp:
                need(isinstance(exp["signature"], str), f"step '{sid}': 'expect.signature' must be a string")
            for k in ("frames", "frames_max"):
                if k in exp:
                    need(count(exp[k]), f"step '{sid}': 'expect.{k}' must be an integer >= 0")
            if "sections_max" in exp:
                sm = exp["sections_max"]
                need(isinstance(sm, dict) and all(isinstance(k, str) and k and count(v) for k, v in sm.items()),
                     f"step '{sid}': 'expect.sections_max' must map section names to integers >= 0")
    fixed = scenario_duration_s(scn, worst_case=False)
    need(fixed <= RECORD_LIMIT_S - 10,
         f"fixed waits add up to about {fixed} s; the screen recorder stops at {RECORD_LIMIT_S} s. Split the scenario")


def scenario_duration_s(scn: dict, worst_case: bool = True) -> int:
    """Seconds the device script needs. worst_case counts every wait-for-quiet at its limit."""
    q_ms, q_max = quiet_settings(scn)
    quiet_cost = (q_max + 1500 if worst_case else q_ms + 600) / 1000
    total = 1 + 1.5 + 4 + scn.get("launch_settle_ms", 6000) / 1000
    if step_until_quiet(scn, None):
        total += quiet_cost
    for step in scn["steps"]:
        if step["do"] == "rest":
            total += step["ms"] / 1000
        elif step_until_quiet(scn, step):
            total += step.get("ms", 0) / 1000 + step.get("settle_ms", 300) / 1000 + quiet_cost
        else:
            total += step.get("ms", 0) / 1000 + step.get("settle_ms", 3000 if step["do"] == "tap" else 2500) / 1000
        total += 0.6  # snapshot cost allowance
    return int(total + 6)


# --------------------------------------------------------------------------------------------
# device facts
# --------------------------------------------------------------------------------------------

def device_facts(serial: str, package: str) -> dict:
    def prop(name):
        return sh(serial, f"getprop {name}").strip()

    size = re.search(r"(\d+)x(\d+)", sh(serial, "wm size"))
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
         "$D/am-start.txt $D/sr-exit.txt $D/sr-alive.txt $D/sr.pid $D/script.pid",
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
    if video:
        L += [f"screenrecord --bit-rate 8000000 --time-limit {limit_s} $D/screen.mp4 &", "SR=$!",
              "echo $SR > $D/sr.pid", "sleep 1.5"]
    launch = scn.get("activity")
    L += ["mark launch.start"]
    if launch:
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
        L.append(f"mark {sid}.start")
        if do == "rest":
            L.append(f"sleep {step['ms'] / 1000}")
        else:
            if do == "tap":
                L.append(f"input tap {px(step['x'], width)} {px(step['y'], height)}")
            elif do == "swipe":
                (x1, y1), (x2, y2) = step["from"], step["to"]
                L.append(f"input swipe {px(x1, width)} {px(y1, height)} {px(x2, width)} {px(y2, height)} {step.get('ms', 300)}")
            elif do == "key":
                L.append(f"input keyevent {step['code']}")
            else:
                raise ProbeError(f"unknown step kind {do}")
            L.append(f"mark {sid}.injected")
            fixed = step.get("settle_ms", 3000 if do == "tap" else 2500) / 1000
            if step_until_quiet(scn, step):
                # An explicit settle_ms is a floor before polling; the fallback is the fixed wait.
                floor = step["settle_ms"] / 1000 if "settle_ms" in step else 0.3
                L += [f"sleep {floor}", f"wait_quiet {sid} {q_ms} {q_max} {round(max(0.0, fixed - floor), 3)}"]
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

    One layer can appear in several blocks (the compositor keeps a record per display refresh rate
    and render rate, among other keys); its frames are the sum over its blocks."""
    head, *blocks = re.split(r"^uid = ", text, flags=re.M)
    total = re.search(r"^totalFrames = (\d+)", head, re.M)
    layers = {}
    for block in blocks:
        uid = re.match(r"(\d+)", block)
        name = re.search(r"^layerName = (.*)$", block, re.M)
        frames = re.search(r"^totalFrames = (\d+)", block, re.M)
        if uid and name and frames and name.group(1).strip() != "none":
            key = (int(uid.group(1)), name.group(1).strip())
            layers[key] = layers.get(key, 0) + int(frames.group(1))
    return {"total": int(total.group(1)) if total else None, "layers": layers}


def app_layer_frames(stats: dict, package: str, uid: int | None) -> dict:
    """The layers that belong to the app under test: owned by its uid and, when that uid is shared
    with the system, also named after its package."""
    out = {}
    for (layer_uid, name), frames in stats["layers"].items():
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


def run_once(serial: str, scn: dict, out: Path, trace: bool, video: bool, sections: bool = False) -> dict:
    validate_scenario(scn)
    out.mkdir(parents=True, exist_ok=True)
    if (out / "capture.json").exists():
        raise ProbeError(f"{out} already holds a run; choose a fresh directory (raw evidence is never overwritten)")
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
        started = time.time()
        # A stalled device (an ANR being processed, for instance) slows every shell call; allow for it.
        sh(serial, f"sh {DEV_DIR}/run.sh", timeout=total_s + 420)
        wall = time.time() - started
        load_after = host_load()
        if tracer is not None:
            # The trace runs for the scenario's worst case; stop it once the scenario is done.
            running, tracer = tracer, None
            tracer_output = stop_tracer(serial, running)
            adb(serial, "pull", trace_dev, str(out / "trace.pftrace"))
            sh(serial, f"rm -f {trace_dev}", check=False)

        raw = out / "raw"
        raw.mkdir(exist_ok=True)
        listing = sh(serial, f"ls {DEV_DIR}").split()
        if "done.txt" not in listing:
            raise ProbeError("the device script did not finish (no done marker); raw files were left on the device")
        for name in listing:
            if name == "run.sh":
                continue
            adb(serial, "pull", f"{DEV_DIR}/{name}", str(raw / name))
        finished = True
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
        "record_limit_s": limit_s, "tracer_output": tracer_output,
        "host": {"load1_before": round(load_before, 2), "load1_after": round(load_after, 2), "wall_s": round(wall, 1)},
    }, indent=1))
    try:
        return analyze_run(out)
    except ProbeError as exc:
        raise ProbeError(f"{out}: the capture was saved but its analysis failed: {exc}") from exc
    except Exception:
        print(f"ui-probe: {out}: the capture was saved but its analysis failed unexpectedly", file=sys.stderr)
        raise


def _read_raw(raw: Path, name: str) -> str | None:
    p = raw / name
    return p.read_text(errors="replace") if p.exists() else None


def analyze_run(out: Path, scn: dict | None = None) -> dict:
    """Everything below reads only files already on disk, so analysis can be redone without a device."""
    cap = read_json(out / "capture.json")
    scn = scn or cap.get("scenario")
    if scn is None:
        raise ProbeError(f"{out}: the capture holds no scenario; pass --scenario")
    validate_scenario(scn)
    # A failed analysis must not leave an earlier result standing for gate, summary and compare.
    result_path = out / "run.json"
    if result_path.exists():
        result_path.unlink()
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
    sf_ok = any(v["total"] is not None for v in sf.values())
    hwui = any(g.get("present") and g.get("total_frames") is not None for g in gfx.values())

    # union of per-frame rows across snapshots (the platform keeps only the most recent 120 per window)
    seen = {}
    for sid in order:
        for row in gfx.get(sid, {}).get("rows", []):
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
        start = marks.get("launch.start" if sid == "launch" else f"{sid}.start")
        end = marks.get(f"{sid}.end") if sid != "launch" else marks.get(scn["steps"][0]["id"] + ".start")
        missing = [label for label, tbl in (("frame stats", gfx), ("compositor statistics", sf), ("cpu", cpu_end),
                                            ("process id", pids)) if sid not in tbl]
        g, pg = gfx.get(sid), (None if first else gfx.get(prev_sid))
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
        if kind in ("tap", "swipe", "key") and in_step:
            inj = marks.get(f"{sid}.start")
            done = [r["FrameCompleted"] for r in in_step if r.get("FrameCompleted", 0) > 0]
            if done:
                first_frame_ms = round(min(done) / 1e6 - inj * 1000, 1)

        frames_display = frames_layers = None
        now_sf = sf.get(sid)
        prev_sf = None if first else sf.get(prev_sid)
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

    if trace:
        trace_path = out / "trace.pftrace"
        if not trace_path.exists():
            result["trace_missing"] = True
        else:
            _analyze_trace(result, steps_out, trace_path, scn, pids, order, bool(cap.get("sections")))

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


def _analyze_trace(result: dict, steps_out: list, trace_path: Path, scn: dict, pids: dict, order: list,
                   sections: bool) -> None:
    sys.path.insert(0, str(HERE))
    import pftrace
    ftrace = {"bundles": 0, "lost_bundles": 0}
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
    # The span the trace holds. A trace that stopped early, or whose ring buffer overwrote its
    # beginning, does not cover every step; a step outside the span has no trace-based counts.
    first_ts, last_ts = tr["first_ts"], tr["last_ts"]
    result["trace_span_s"] = [first_ts / 1e9, last_ts / 1e9] if first_ts is not None else None
    result["trace_truncated"] = tr["truncated"]

    def covered(st):
        return (not loss and first_ts is not None and st["start_s"] is not None and st["end_s"] is not None
                and first_ts <= st["start_s"] * 1e9 and st["end_s"] * 1e9 <= last_ts)

    # The compositor's own count per step: frames of the app's layers from any of its processes
    # (a restart changes the pid), whatever toolkit drew them. Trace and marks share the boot clock.
    own_pids = {int(t) for p in pids.values() for t in p.split() if t.isdigit()}
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
        wanted = list(dict.fromkeys(DEFAULT_SECTIONS + list(scn.get("count_sections", []))))
        begins = [b for b in begins_all if b["tgid"] in own_pids]
        match = lambda b, w: b["name"] == w or b["name"].startswith(w + " ")
        # A name the app never emitted anywhere in the trace was not observed: the build or the
        # platform may not emit it. Its count is unknown, not zero.
        observed = {w for w in wanted if any(match(b, w) for b in begins)}
        result["sections_total"] = len(begins)
        result["sections_not_observed"] = [w for w in wanted if w not in observed]
        for st in steps_out:
            if not st["trace_covered"] or sections_loss:
                st["sections"] = {w: None for w in wanted}
                st["sections_all"] = None
                continue
            lo, hi = st["start_s"] * 1e9, st["end_s"] * 1e9
            inside = [b for b in begins if lo <= b["ts"] < hi]
            st["sections"] = {w: (sum(1 for b in inside if match(b, w)) if w in observed else None) for w in wanted}
            st["sections_all"] = len(inside)


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


def find_runs(dirs: list[str]) -> list[tuple[Path, str | None]]:
    """Every run directory under dirs (one that holds capture.json or run.json) with the reason it
    cannot be used, or None. Captures are enumerated, not analyses: a capture whose analysis is
    missing or older than the capture is reported, never skipped."""
    found = set()
    for d in dirs:
        root = Path(d)
        if not root.exists():
            raise ProbeError(f"{d} does not exist")
        for name in ("capture.json", "run.json"):
            found.update(p.parent for p in root.glob(f"**/{name}"))
    if not found:
        raise ProbeError("no capture.json or run.json found under " + ", ".join(dirs))
    out = []
    for run_dir in sorted(found):
        cap, res = run_dir / "capture.json", run_dir / "run.json"
        problem = None
        if not cap.exists():
            problem = "run.json without capture.json: the analysis cannot be traced to a capture"
        elif not res.exists():
            problem = "not analysed: the capture has no run.json (the analysis failed or never ran); run `ui-probe analyze`"
        elif res.stat().st_mtime_ns < cap.stat().st_mtime_ns:
            problem = "not analysed: run.json is older than capture.json; run `ui-probe analyze`"
        out.append((run_dir, problem))
    return out


def load_runs(dirs: list[str]) -> list[tuple[str, dict]]:
    runs = []
    for path, problem in find_runs(dirs):
        if problem:
            raise ProbeError(f"{path}: {problem}")
        runs.append((str(path), read_json(path / "run.json")))
    return runs


def run_row(path: str, r: dict) -> dict:
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
    raw_rows = [run_row(path, r) for path, r in load_runs(dirs)]
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
    return {"runs": raw_rows, "stats": table}


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
        for sid in r.get("trace_uncovered_steps") or []:
            notes.append(f"{sid}: the trace did not cover this step; its trace counts are unavailable")
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
            if counted is not None and counted > opts["rest_frames_max"]:
                fail("rest-frames", sid, f"{counted} frames drawn at rest ({source} count), allowed {opts['rest_frames_max']}",
                     observed=counted, allowed=opts["rest_frames_max"], source=source)
            elif counted is None and usable:
                notes.append(f"{sid}: no platform frame count for this app; only the recording was checked")
            if vid is not None and not shows_app:
                notes.append(f"{sid}: another app was in front, so the recording shows that app; it was not used for this step")
            elif vid is not None:
                findings = []
                if vid["identical_frames"] > 0:
                    findings.append((vid["identical_frames"],
                                     f"{vid['identical_frames']} frames were composed at rest that changed nothing on screen"))
                real = vid.get("app_changed_frames_real", vid["app_region_frames"])
                if real > 0:
                    findings.append((real, f"the screen changed at rest: {real} frames in the app's area"))
                for observed, message in findings:
                    if aligned:
                        fail("rest-video", sid, message, observed=observed)
                    else:
                        notes.append(f"{sid}: {message} (the recording is not aligned with the step marks, so this is not a violation)")
            ticks = s.get("cpu_ticks_total")
            window_s = s.get("cpu_window_s")
            if ticks is None or not window_s or window_s <= 0:
                notes.append(f"{sid}: CPU at rest not available")
            else:
                cpu_pct = round(100 * ticks * (1000 / tck) / (window_s * 1000), 2)
                if cpu_pct > opts["rest_cpu_pct_max"]:
                    fail("rest-cpu", sid, f"{cpu_pct}% of one core used at rest ({ticks} ticks), allowed {opts['rest_cpu_pct_max']}%",
                         observed=cpu_pct, allowed=opts["rest_cpu_pct_max"])
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
                elif s.get("trace_covered") is False:
                    msg = f"'{name}' was not counted: the trace did not cover this step"
                else:
                    msg = f"'{name}' was not counted; record with --sections and list it in count_sections"
                fail("sections", sid, msg)
            elif got > limit:
                fail("sections", sid, f"{got} '{name}' sections, approved at most {limit}", observed=got, allowed=limit)
    for sid in records:
        if sid not in order:
            notes.append(f"{sid}: in the run but not in the scenario; not checked")
    if front_unknown:
        notes.append("which app was in front was not recorded at the end of: " + ", ".join(front_unknown)
                     + " (a capture from an older version, or a device that reports no resumed activity)")
    if r.get("clock_mismatch_suspected"):
        notes.append("per-frame rows do not line up with the step marks (the device may have slept since boot); frame "
                     "timings are unreliable and the recording cannot be placed in the step windows; frame counts are not affected")
    return {"verdict": "FAIL" if violations else "PASS", "violations": violations, "notes": notes}


def gate(dirs: list[str], overrides: dict, scenario: dict | None = None) -> dict:
    if scenario is not None:
        validate_scenario(scenario)
    out = {"tool": {"name": "ui-probe", "version": VERSION}, "runs": []}
    for path, problem in find_runs(dirs):
        if problem:
            name = scenario["name"] if scenario else None
            out["runs"].append({"run": str(path), "scenario": name, "frame_sources": None, "limits": None,
                                "verdict": "FAIL", "notes": [],
                                "violations": [{"check": "analysis", "step": None, "message": problem}]})
            continue
        cap = read_json(path / "capture.json")
        r = read_json(path / "run.json")
        scn = scenario or cap.get("scenario")
        if scn is None:
            raise ProbeError(f"{path}: the capture holds no scenario; pass --scenario")
        if scenario is None:
            try:
                validate_scenario(scn)
            except ProbeError as exc:
                raise ProbeError(f"{path}: {exc}") from exc
        opts = dict(GATE_DEFAULTS)
        opts.update(scn.get("gate") or {})
        opts.update({k: v for k, v in overrides.items() if v is not None})
        res = gate_run(r, scn, opts)
        out["runs"].append({"run": str(path), "scenario": scn["name"], "frame_sources": r.get("frame_sources") or [],
                            "limits": opts, **res})
    out["verdict"] = "FAIL" if any(x["verdict"] == "FAIL" for x in out["runs"]) else "PASS"
    return out


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
    base = [run_row(p, r) for p, r in load_runs(base_dirs)]
    head = [run_row(p, r) for p, r in load_runs(head_dirs)]
    keys = metrics or sorted({k for row in base + head for k, v in row.items()
                              if k not in ("run", "host_load1") and isinstance(v, (int, float))})
    table = {}
    for k in keys:
        col_a, col_b = [row.get(k) for row in base], [row.get(k) for row in head]
        if all(isinstance(v, str) or v is None for v in col_a + col_b):
            sa, sb = sorted({v for v in col_a if v}), sorted({v for v in col_b if v})
            table[k] = {"n_base": len(col_a), "n_head": len(col_b), "base": sa, "head": sb,
                        "verdict": "same" if sa == sb else "different", "reason": "set of values"}
        else:
            table[k] = compare_metric(col_a, col_b, alpha, min_runs)
    order = {"different": 0, "unknown": 1, "same": 2}
    return {"tool": {"name": "ui-probe", "version": VERSION}, "alpha": alpha, "min_runs": min_runs,
            "metrics": dict(sorted(table.items(), key=lambda kv: (order[kv[1]["verdict"]], kv[0])))}


# --------------------------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------------------------

def ffmpeg_version(text: str) -> tuple[int, int] | None:
    """(major, minor) from `ffmpeg -version`'s first line, or None if it cannot be read (a build
    from a source snapshot, for instance, names a revision instead of a release)."""
    m = re.search(r"ffmpeg version n?(\d+)\.(\d+)", text)
    return (int(m.group(1)), int(m.group(2))) if m else None


def doctor(serial: str | None) -> dict:
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
    for tool, cmd in (("adb", ["adb", "version"]), ("ffmpeg", ["ffmpeg", "-version"]), ("ffprobe", ["ffprobe", "-version"])):
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
        except (ProbeError, subprocess.TimeoutExpired) as exc:
            add("device", False, str(exc))
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
    r.add_argument("--serial", required=True)
    r.add_argument("--scenario", required=True)
    r.add_argument("--out", required=True, help="a directory that does not yet hold runs")
    r.add_argument("--runs", type=int, default=1)
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
    z = sub.add_parser("zoom", help="full-resolution before/after/difference of two recorded frames")
    z.add_argument("run_dir")
    z.add_argument("a", type=int, help="frame index as printed on a sheet (#N)")
    z.add_argument("b", type=int)
    z.add_argument("--out", default="")
    sub.add_parser("proxy", help="a shaping forward proxy: latency and a rate limit per connection", add_help=False)
    d = sub.add_parser("doctor", help="check the host tools, and a device if --serial is given")
    d.add_argument("--serial", default="")

    argv = list(sys.argv[1:] if argv is None else argv)
    try:
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


def _dispatch(args) -> int:
    if args.cmd == "run":
        scn = load_scenario(args.scenario)
        validate_scenario(scn)
        base = Path(args.out)
        for i in range(1, args.runs + 1):
            rid = f"run-{i:02d}"
            res = run_once(args.serial, scn, base / rid, args.trace or args.sections, not args.no_video, args.sections)
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
        for run in found:
            try:
                analyze_run(run, scn)
            except ProbeError as exc:
                raise ProbeError(f"{run}: analysis failed: {exc}") from exc
            print("analysed", run if args.cmd == "analyze" else run / "sheets")
    elif args.cmd == "summary":
        out = summary(args.dirs)
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
        print(f"gate: {out['verdict']}" + (" (advisory)" if args.advisory and out["verdict"] == "FAIL" else ""))
        return 1 if out["verdict"] == "FAIL" and not args.advisory else 0
    elif args.cmd == "compare":
        out = compare(args.base, args.head, args.metric, args.alpha, args.min_runs)
        if args.json:
            Path(args.json).write_text(json.dumps(out, indent=1))
        for k, v in out["metrics"].items():
            extra = f"median {v.get('median_base')} -> {v.get('median_head')}" if "median_base" in v else \
                    (f"{v.get('base')} -> {v.get('head')}" if "base" in v else "")
            print(f"{v['verdict']:9s} {k:44s} {extra}  ({v['reason']})")
    elif args.cmd == "zoom":
        sys.path.insert(0, str(HERE))
        import ui_video
        run = Path(args.run_dir)
        out = Path(args.out) if args.out else run / "sheets" / f"zoom-{args.a}-{args.b}.png"
        try:
            res = ui_video.zoom(run / "raw" / "screen.mp4", args.a, args.b, out)
        except ui_video.VideoError as exc:
            raise ProbeError(f"zoom: {exc}") from exc
        print(json.dumps(res))
    elif args.cmd == "doctor":
        out = doctor(args.serial or None)
        for chk in out["checks"]:
            state = "ok  " if chk["ok"] else ("WARN" if chk.get("warning") else ("MISS" if chk["required"] else "none"))
            print(f"{state} {chk['check']}: {chk['detail']}")
        return 0 if out["ok"] else 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
