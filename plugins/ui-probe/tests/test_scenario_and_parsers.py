"""Parsers, scenario validation, duration estimate and the device-side script of ui_probe.py."""
import sys

sys.dont_write_bytecode = True

import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import ui_probe as u  # noqa: E402

PKG = "com.example.app"

GFX_TEXT = f"""Applications Graphics Acceleration Info:
Uptime: 1000 Realtime: 1000

** Graphics info for pid 4321 [{PKG}] **

Stats since: 100ns
Total frames rendered: 42
Janky frames: 5 (11.90%)
Janky frames (legacy): 7 (16.67%)
50th percentile: 8ms
Number Missed Vsync: 1
Number High input latency: 2
Number Slow UI thread: 3
Number Slow bitmap uploads: 0
Number Slow issue draw commands: 4
Number Frame deadline missed: 6
9,9,9,9,9,
Window: {PKG}/{PKG}.MainActivity
---PROFILEDATA---
Flags,FrameTimelineVsyncId,IntendedVsync,Vsync,FrameCompleted,
0,11,1000000000,1000000000,1008000000,
not,a,row,
1,12,1016000000,1016000000,1030000000,
---PROFILEDATA---
5,5,5,5,5,
Window: PopupWindow
---PROFILEDATA---
Flags,IntendedVsync,FrameCompleted,
0,2000000000,2010000000,
---PROFILEDATA---
"""


def stat_line(tid, comm, utime, stime):
    # fields after "(comm)": state ppid pgrp session tty tpgid flags minflt cminflt majflt cmajflt utime stime ...
    return f"{tid} ({comm}) S 1 2 3 4 5 6 7 8 9 10 {utime} {stime} 0 0 20 0 1 0"


class ParseGfxTest(unittest.TestCase):
    def test_totals(self):
        g = u.parse_gfx(GFX_TEXT)
        self.assertTrue(g["present"])
        self.assertEqual(g["total_frames"], 42)
        self.assertEqual(g["janky_frames"], 5)
        self.assertEqual(g["janky_legacy"], 7)
        self.assertEqual(g["missed_vsync"], 1)
        self.assertEqual(g["high_input_latency"], 2)
        self.assertEqual(g["slow_ui_thread"], 3)
        self.assertEqual(g["slow_bitmap_uploads"], 0)
        self.assertEqual(g["slow_issue_draw"], 4)
        self.assertEqual(g["frame_deadline_missed"], 6)

    def test_rows_only_between_markers(self):
        rows = u.parse_gfx(GFX_TEXT)["rows"]
        # two rows from the first window, one from the second; the 9,9,... and 5,5,... lines
        # outside the markers and the non-numeric line inside are ignored
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0], {"Flags": 0, "FrameTimelineVsyncId": 11, "IntendedVsync": 1000000000,
                                   "Vsync": 1000000000, "FrameCompleted": 1008000000})
        self.assertEqual(rows[1]["Flags"], 1)
        self.assertEqual(rows[1]["FrameCompleted"], 1030000000)
        # each block has its own header
        self.assertEqual(rows[2], {"Flags": 0, "IntendedVsync": 2000000000, "FrameCompleted": 2010000000})

    def test_no_graphics_info(self):
        g = u.parse_gfx(f"No process found for: {PKG}\n")
        self.assertFalse(g["present"])
        for key in u.GFX_TOTALS:
            self.assertIsNone(g[key], key)
        self.assertEqual(g["rows"], [])


class ParseThreadCpuTest(unittest.TestCase):
    def test_utime_plus_stime(self):
        text = "\n".join([
            stat_line(4321, PKG, 120, 30),
            stat_line(4330, "RenderThread", 7, 3),
            stat_line(4331, "Render (x) Thread", 1, 1),   # spaces and parentheses inside comm
            "4332 (short) S 1 2 3",                        # too few fields: skipped
            "garbage",
            "",
        ])
        out = u.parse_thread_cpu(text)
        self.assertEqual(out, {"4321": (PKG, 150), "4330": ("RenderThread", 10), "4331": ("Render (x) Thread", 2)})

    def test_empty(self):
        self.assertEqual(u.parse_thread_cpu(""), {})


class ParseMeminfoTest(unittest.TestCase):
    NEW = f"""** MEMINFO in pid 4321 [{PKG}] **
                   Pss  Private  Private
                 Total    Dirty    Clean
         TOTAL    51000    40000     2000
 App Summary
                       Pss(KB)
           Java Heap:    10240
         Native Heap:    20480
                Code:     4096
               Stack:      512
            Graphics:     8192
           TOTAL PSS:    51234            TOTAL RSS:    90000
 Objects
               Views:      123         ViewRootImpl:        2
         AppContexts:        5           Activities:        1
"""

    def test_current_layout(self):
        m = u.parse_meminfo(self.NEW)
        self.assertEqual(m, {"total_pss_kb": 51234, "java_heap_kb": 10240, "native_heap_kb": 20480,
                             "graphics_kb": 8192, "code_kb": 4096, "views": 123, "view_root_impl": 2,
                             "activities": 1})

    def test_legacy_total(self):
        m = u.parse_meminfo("         TOTAL    40000    30000\n")
        self.assertEqual(m["total_pss_kb"], 40000)
        self.assertIsNone(m["java_heap_kb"])

    def test_empty(self):
        m = u.parse_meminfo("")
        self.assertTrue(all(v is None for v in m.values()))
        self.assertIn("total_pss_kb", m)


class ParseMarksAndPctTest(unittest.TestCase):
    def test_marks(self):
        marks = u.parse_marks("T0 100.25\nlaunch.start 101.5\nthree parts here\n\nEND 130\n")
        self.assertEqual(marks, {"T0": 100.25, "launch.start": 101.5, "END": 130.0})

    def test_pct(self):
        self.assertIsNone(u.pct([], 0.5))
        self.assertEqual(u.pct([7], 0.9), 7)
        vals = [10, 3, 8, 1, 6, 2, 9, 4, 7, 5]          # unsorted input
        self.assertEqual(u.pct(vals, 0.0), 1)
        self.assertEqual(u.pct(vals, 1.0), 10)
        self.assertEqual(u.pct(vals, 0.9), 9)            # index round(8.1) = 8
        self.assertEqual(u.pct([1, 2, 3, 4, 5], 0.5), 3)
        self.assertEqual(u.pct([1, 2, 3], 2.0), 3)       # clamped


def scenario(steps, **kw):
    return {"name": "Example", "package": PKG, "steps": steps, **kw}


VALID_STEPS = [
    {"id": "settle", "do": "rest", "ms": 2000, "meminfo": True},
    {"id": "open", "do": "tap", "x": 0.5, "y": 0.25,
     "expect": {"signature": "T", "frames": 3, "frames_max": 5, "sections_max": {"draw": 4}}},
    {"id": "scroll", "do": "swipe", "from": [0.5, 0.8], "to": [0.5, 0.2], "ms": 250},
    {"id": "pull", "do": "swipe", "from": [0.5, 0.3], "to": [0.5, 0.6], "judge": "state"},
    {"id": "back", "do": "key", "code": 4},
]


class ValidateScenarioTest(unittest.TestCase):
    def reject(self, scn, fragment):
        with self.assertRaises(u.ProbeError) as cm:
            u.validate_scenario(scn)
        self.assertIn(fragment, str(cm.exception))
        self.assertTrue(str(cm.exception).startswith("scenario: "))

    def test_valid_every_kind(self):
        u.validate_scenario(scenario(VALID_STEPS))
        for step in VALID_STEPS:
            u.validate_scenario(scenario([step]))

    def test_not_an_object(self):
        self.reject(["x"], "must be a JSON object")

    def test_missing_name(self):
        scn = scenario(VALID_STEPS)
        del scn["name"]
        self.reject(scn, "'name' is required")
        self.reject({**scenario(VALID_STEPS), "name": ""}, "'name' is required")

    def test_missing_package(self):
        scn = scenario(VALID_STEPS)
        del scn["package"]
        self.reject(scn, "'package' is required")

    def test_empty_steps(self):
        self.reject(scenario([]), "'steps' must be a non-empty list")
        scn = scenario([])
        del scn["steps"]
        self.reject(scn, "'steps' must be a non-empty list")

    def test_step_not_object(self):
        self.reject(scenario(["tap"]), "step 1 must be an object")

    def test_bad_id(self):
        self.reject(scenario([{"id": "a b", "do": "key", "code": 4}]), "'id' must be letters")
        self.reject(scenario([{"do": "key", "code": 4}]), "'id' must be letters")

    def test_duplicate_id(self):
        self.reject(scenario([{"id": "a", "do": "key", "code": 4}, {"id": "a", "do": "key", "code": 3}]),
                    "id 'a' is used twice")

    def test_reserved_ids(self):
        for sid in u.RESERVED_IDS:
            self.reject(scenario([{"id": sid, "do": "key", "code": 4}]), f"id '{sid}' is reserved")

    def test_unknown_do(self):
        self.reject(scenario([{"id": "a", "do": "pinch"}]), "'do' must be one of rest, tap, swipe, key")

    def test_coordinates_outside_unit(self):
        self.reject(scenario([{"id": "a", "do": "tap", "x": 1.2, "y": 0.5}]), "tap needs 'x' and 'y' between 0 and 1")
        self.reject(scenario([{"id": "a", "do": "tap", "x": 0.5}]), "tap needs 'x' and 'y'")
        self.reject(scenario([{"id": "a", "do": "swipe", "from": [0.5, 0.5], "to": [0.5, -0.1]}]),
                    "swipe needs 'to' as [x, y] between 0 and 1")
        self.reject(scenario([{"id": "a", "do": "swipe", "from": [0.5], "to": [0.5, 0.1]}]),
                    "swipe needs 'from'")

    def test_rest_without_ms(self):
        self.reject(scenario([{"id": "a", "do": "rest"}]), "rest needs 'ms' > 0")
        self.reject(scenario([{"id": "a", "do": "rest", "ms": 0}]), "rest needs 'ms' > 0")

    def test_swipe_bad_judge(self):
        self.reject(scenario([{"id": "a", "do": "swipe", "from": [0.5, 0.5], "to": [0.5, 0.1], "judge": "speed"}]),
                    "'judge' is 'motion' or 'state'")

    def test_key_needs_int_code(self):
        self.reject(scenario([{"id": "a", "do": "key", "code": "BACK"}]), "key needs an integer 'code'")

    def test_unknown_expect_key(self):
        self.reject(scenario([{"id": "a", "do": "key", "code": 4, "expect": {"fps": 60}}]),
                    "unknown 'expect' keys ['fps']")
        self.reject(scenario([{"id": "a", "do": "key", "code": 4, "expect": "T"}]), "'expect' must be an object")

    def test_fixed_waits_over_recording_limit(self):
        # fixed part: 1 + 1.5 + 4 + 6 (launch settle) + 0.6 (snapshot) + 6 = 19.1 s plus the rest
        u.validate_scenario(scenario([{"id": "a", "do": "rest", "ms": 151000}]))   # int(170.1) = 170: allowed
        self.reject(scenario([{"id": "a", "do": "rest", "ms": 152000}]), "the screen recorder stops at 180 s")

    def test_non_numeric_settle_is_a_probe_error(self):
        """Fixed defect: a non-numeric 'settle_ms' (or 'ms' on a tap/swipe/key, or a 'quiet' value) is not
        validated; scenario_duration_s raises TypeError/ValueError instead of a ProbeError naming it."""
        self.reject(scenario([{"id": "a", "do": "tap", "x": 0.5, "y": 0.5, "settle_ms": "fast"}]), "settle_ms")


class StrictScenarioTest(unittest.TestCase):
    """Finding 12: keys and types that would otherwise change gating silently are rejected; finding
    18: the package and the launch component are checked before they reach a shell."""

    def reject(self, scn, fragment):
        with self.assertRaises(u.ProbeError) as cm:
            u.validate_scenario(scn)
        self.assertIn(fragment, str(cm.exception))

    def test_gate_block(self):
        self.reject(scenario(VALID_STEPS, gate={"rest_frame_max": 5}), "unknown 'gate' keys ['rest_frame_max']")
        self.reject(scenario(VALID_STEPS, gate={"rest_frames_max": "0"}), "'gate.rest_frames_max'")
        self.reject(scenario(VALID_STEPS, gate={"rest_frames_max": -1}), "'gate.rest_frames_max'")
        self.reject(scenario(VALID_STEPS, gate={"rest_cpu_pct_max": True}), "'gate.rest_cpu_pct_max'")
        self.reject(scenario(VALID_STEPS, gate=[]), "'gate' must be an object")
        u.validate_scenario(scenario(VALID_STEPS, gate={"rest_frames_max": 2, "rest_cpu_pct_max": 1.5}))

    def test_count_sections(self):
        self.reject(scenario(VALID_STEPS, count_sections="Foo#bar"), "'count_sections' must be a list of non-empty strings")
        self.reject(scenario(VALID_STEPS, count_sections=["ok", ""]), "'count_sections'")
        self.reject(scenario(VALID_STEPS, count_sections=[3]), "'count_sections'")
        u.validate_scenario(scenario(VALID_STEPS, count_sections=["Foo#bar"]))

    def test_booleans(self):
        self.reject(scenario(VALID_STEPS, until_quiet="false"), "'until_quiet' must be true or false")
        self.reject(scenario([{"id": "a", "do": "tap", "x": 0.5, "y": 0.5, "until_quiet": 1}]), "'until_quiet' must be true or false")
        self.reject(scenario([{"id": "a", "do": "rest", "ms": 100, "meminfo": "yes"}]), "'meminfo' must be true or false")

    def test_quiet(self):
        self.reject(scenario(VALID_STEPS, quiet={"ms": 0.5}), "'quiet.ms' must be a number of at least 100")
        self.reject(scenario(VALID_STEPS, quiet={"max_ms": 50}), "'quiet.max_ms' must be a number of at least 100")
        self.reject(scenario(VALID_STEPS, quiet={"ms": 900, "max_ms": 800}), "'quiet.max_ms' must be at least 'quiet.ms'")
        self.reject(scenario(VALID_STEPS, quiet={"ms": 500, "maxms": 900}), "unknown 'quiet' keys ['maxms']")
        u.validate_scenario(scenario(VALID_STEPS, quiet={"ms": 100, "max_ms": 100}))

    def test_activity(self):
        self.reject(scenario(VALID_STEPS, activity=".Main; reboot"), "'activity' must be letters, digits")
        self.reject(scenario(VALID_STEPS, activity=""), "'activity'")
        self.reject(scenario(VALID_STEPS, activity=4), "'activity'")
        u.validate_scenario(scenario(VALID_STEPS, activity=".Outer$Inner"))

    def test_package(self):
        for bad in ("com.ex.app; touch /data/local/tmp/pwn", "app", "com..app", "1com.app", "com.app'"):
            self.reject({**scenario(VALID_STEPS), "package": bad}, "'package' must be an application id")
        u.validate_scenario({**scenario(VALID_STEPS), "package": "com.example.app_2"})

    def test_expect_types(self):
        for exp, frag in (({"frames_max": "10"}, "'expect.frames_max'"), ({"frames": -1}, "'expect.frames'"),
                          ({"frames": True}, "'expect.frames'"), ({"signature": 1}, "'expect.signature'"),
                          ({"sections_max": {"draw": "2"}}, "'expect.sections_max'"),
                          ({"sections_max": ["draw"]}, "'expect.sections_max'")):
            self.reject(scenario([{"id": "a", "do": "key", "code": 4, "expect": exp}]), frag)

    def test_unknown_keys(self):
        self.reject(scenario(VALID_STEPS, until_quite=True), "unknown keys ['until_quite']")
        self.reject(scenario([{"id": "a", "do": "tap", "x": 0.5, "y": 0.5, "setle_ms": 100}]),
                    "keys ['setle_ms'] do not apply to a 'tap' step")
        self.reject(scenario([{"id": "a", "do": "tap", "x": 0.5, "y": 0.5, "meminfo": True}]),
                    "keys ['meminfo'] do not apply to a 'tap' step")
        # a key whose name ends in "comment" is allowed anywhere
        u.validate_scenario(scenario([{"id": "a", "do": "key", "code": 4, "comment": "back"}], comment="x",
                                     steps_comment="y"))

    def test_the_example_scenario_is_valid(self):
        import json
        example = Path(__file__).resolve().parent.parent / "examples" / "system-settings.json"
        u.validate_scenario(json.loads(example.read_text()))


class ShellQuotingTest(unittest.TestCase):
    """Finding 18: the package and the activity reach every shell single-quoted."""

    def test_quote(self):
        self.assertEqual(u.q("a b"), "'a b'")
        self.assertEqual(u.q("it's"), "'it'\"'\"'s'")

    def test_inner_class_activity_keeps_its_dollar(self):
        scn = scenario([{"id": "a", "do": "key", "code": 4}], activity=".Outer$Inner")
        u.validate_scenario(scn)
        lines = u.build_device_script(scn, 1080, 2400, False, 60).splitlines()
        self.assertIn(f"am start -W -n '{PKG}/.Outer$Inner' > $D/am-start.txt 2>&1", lines)
        self.assertIn(f"PKG='{PKG}'", lines)
        self.assertFalse([l for l in lines if "$PKG" in l and '"$PKG"' not in l])

    def test_device_facts_quotes_the_package(self):
        from unittest import mock
        commands = []

        def fake_sh(serial, command, **kw):
            commands.append(command)
            if command.startswith("wm size"):
                return "Physical size: 1080x2400"
            if command.startswith("dumpsys package '"):
                return f"Package [{PKG}] (abc):\n userId=10100\n"
            if command.startswith("pm path"):
                return "package:/data/app/x y/base.apk\n"
            return ""

        with mock.patch.object(u, "sh", fake_sh), mock.patch.object(u, "adb", lambda *a, **k: ""):
            facts = u.device_facts("<serial>", PKG)
        self.assertIn(f"dumpsys package '{PKG}'", commands)
        self.assertIn(f"pm path '{PKG}'", commands)
        self.assertIn("sha256sum '/data/app/x y/base.apk'", commands)
        self.assertIsNone(facts["cpus"])


class DurationTest(unittest.TestCase):
    def test_fixed_waits(self):
        scn = scenario([{"id": "a", "do": "tap", "x": 0.5, "y": 0.5}])
        # 6.5 + 6 launch + 3 tap settle + 0.6 + 6 = 22.1
        self.assertEqual(u.scenario_duration_s(scn), 22)
        self.assertEqual(u.scenario_duration_s(scn, worst_case=False), 22)

    def test_until_quiet_worst_and_expected(self):
        scn = scenario([{"id": "a", "do": "tap", "x": 0.5, "y": 0.5}], until_quiet=True)
        # launch: the settle wait is a floor, then the quiet wait. Step: a 0.3 s floor, then quiet.
        # worst: quiet cost 15 s + 1.5 s of polling slack; 6.5 + (6 + 16.5) + (0.3 + 16.5) + 0.6 + 6 = 52.4
        self.assertEqual(u.scenario_duration_s(scn), 52)
        # expected: quiet cost 0.8 + 0.6 = 1.4; 6.5 + 7.4 + 1.7 + 0.6 + 6 = 22.2
        self.assertEqual(u.scenario_duration_s(scn, worst_case=False), 22)
        # an explicit settle_ms on a quiet step is its floor: 6.5 + 7.4 + (2 + 1.4) + 0.6 + 6 = 23.9
        scn["steps"][0]["settle_ms"] = 2000
        self.assertEqual(u.scenario_duration_s(scn, worst_case=False), 23)

    def test_rest_and_swipe(self):
        scn = scenario([{"id": "r", "do": "rest", "ms": 4000},
                        {"id": "s", "do": "swipe", "from": [0, 0], "to": [1, 1], "ms": 500}])
        # 6.5 + 6 + (4 + 0.6) + (0.5 + 2.5 + 0.6) + 6 = 26.7
        self.assertEqual(u.scenario_duration_s(scn), 26)


class DeviceScriptTest(unittest.TestCase):
    W, H = 1080, 2400

    def lines(self, scn, video=False, limit_s=77):
        u.validate_scenario(scn)
        text = u.build_device_script(scn, self.W, self.H, video, limit_s)
        self.assertTrue(text.endswith("\n"))
        return text.splitlines()

    def test_tap_and_swipe_pixels(self):
        lines = self.lines(scenario([
            {"id": "a", "do": "tap", "x": 0.25, "y": 0.75},
            {"id": "b", "do": "swipe", "from": [0.5, 0.8], "to": [0.5, 0.2], "ms": 250},
            {"id": "c", "do": "swipe", "from": [0.1, 0.5], "to": [0.9, 0.5]},
            {"id": "d", "do": "key", "code": 4},
        ]))
        self.assertIn("input tap 270 1800", lines)
        self.assertIn("input swipe 540 1920 540 480 250", lines)
        self.assertIn("input swipe 108 1200 972 1200 300", lines)   # default gesture duration
        self.assertIn("input keyevent 4", lines)
        self.assertIn(f"PKG='{PKG}'", lines)

    def test_step_order_and_fixed_sleep(self):
        lines = self.lines(scenario([{"id": "a", "do": "tap", "x": 0.5, "y": 0.5}]))
        seq = ["mark a.start", "input tap 540 1200", "mark a.injected", "sleep 3.0", "mark a.end", "snap a"]
        idx = [lines.index(s) for s in seq]
        self.assertEqual(idx, sorted(idx))
        self.assertEqual(idx[-1] - idx[0], len(seq) - 1)
        # no wait_quiet call anywhere (only the function definition)
        self.assertEqual([l for l in lines if l.startswith("wait_quiet ")], [])
        self.assertIn("sleep 6.0", lines)                   # launch settle
        self.assertEqual(lines[-3], "mark END")
        self.assertEqual(lines[-2], "dumpsys SurfaceFlinger --timestats -disable >/dev/null 2>&1")
        self.assertEqual(lines[-1], "echo done > $D/done.txt")

    def test_custom_settle(self):
        lines = self.lines(scenario([{"id": "a", "do": "key", "code": 4, "settle_ms": 1200}], launch_settle_ms=2500))
        self.assertIn("sleep 1.2", lines)
        self.assertIn("sleep 2.5", lines)

    def test_until_quiet_scenario_level(self):
        lines = self.lines(scenario([{"id": "a", "do": "tap", "x": 0.5, "y": 0.5},
                                     {"id": "b", "do": "key", "code": 4, "until_quiet": False}],
                                    until_quiet=True, quiet={"ms": 500, "max_ms": 9000}))
        # launch: the fixed settle is a floor, then wait for quiet with no further fallback sleep
        launch_sleep = lines.index("sleep 6.0")
        self.assertEqual(lines[launch_sleep - 1], "mark launch.end")
        self.assertEqual(lines[launch_sleep + 1], "wait_quiet launch 500 9000 0")
        self.assertEqual(lines[launch_sleep + 2], "snap launch")
        # the fallback used when frames cannot be polled is the fixed wait less the floor already slept
        self.assertIn("wait_quiet a 500 9000 2.7", lines)
        self.assertFalse(any(l.startswith("wait_quiet b ") for l in lines))
        b_inj = lines.index("mark b.injected")
        self.assertEqual(lines[b_inj + 1], "sleep 2.5")
        self.assertEqual(lines[lines.index("mark a.injected") + 1], "sleep 0.3")

    def test_until_quiet_per_step(self):
        lines = self.lines(scenario([{"id": "a", "do": "tap", "x": 0.5, "y": 0.5, "until_quiet": True}]))
        self.assertIn("wait_quiet a 800 15000 2.7", lines)
        self.assertFalse(any(l.startswith("wait_quiet launch") for l in lines))
        self.assertEqual(lines[lines.index("sleep 6.0") + 1], "snap launch")

    def test_until_quiet_step_settle_is_floor_and_fallback(self):
        lines = self.lines(scenario([{"id": "a", "do": "key", "code": 4, "until_quiet": True, "settle_ms": 1200}]))
        i = lines.index("mark a.injected")
        # an explicit settle_ms is the floor; nothing is left for the fallback, so no double wait
        self.assertEqual(lines[i + 1:i + 4], ["sleep 1.2", "wait_quiet a 800 15000 0.0", "mark a.end"])

    def test_rest_sleeps_and_meminfo(self):
        lines = self.lines(scenario([{"id": "r1", "do": "rest", "ms": 2000, "meminfo": True},
                                     {"id": "r2", "do": "rest", "ms": 500}], until_quiet=True))
        self.assertIn("sleep 2.0", lines)
        self.assertIn("sleep 0.5", lines)
        # the memory dump is taken inside the snapshot, between its two CPU readings
        self.assertIn("snap r1 mem", lines)
        self.assertIn("snap r2", lines)
        self.assertIn('  if [ "$2" = mem ]; then dumpsys meminfo "$PKG" > $D/mem-$1.txt 2>&1; fi', lines)
        self.assertEqual([l for l in lines if l.startswith("wait_quiet r")], [])   # a rest never waits for quiet

    def test_launch_component(self):
        with_act = self.lines(scenario([{"id": "a", "do": "key", "code": 4}], activity=".MainActivity"))
        self.assertIn(f"am start -W -n '{PKG}/.MainActivity' > $D/am-start.txt 2>&1", with_act)
        without = self.lines(scenario([{"id": "a", "do": "key", "code": 4}]))
        self.assertIn('monkey -p "$PKG" -c android.intent.category.LAUNCHER 1 > $D/am-start.txt 2>&1', without)

    def test_recorder_only_with_video(self):
        on = self.lines(scenario([{"id": "a", "do": "key", "code": 4}]), video=True, limit_s=77)
        self.assertIn("screenrecord --bit-rate 8000000 --time-limit 77 $D/screen.mp4 &", on)
        self.assertIn("kill -2 $SR 2>/dev/null", on)
        self.assertEqual(on[on.index("kill -2 $SR 2>/dev/null"):][:4],
                         ["kill -2 $SR 2>/dev/null", "wait $SR", "echo $? > $D/sr-exit.txt", "SR="])
        self.assertLess(on.index("mark T0"), on.index("screenrecord --bit-rate 8000000 --time-limit 77 $D/screen.mp4 &"))
        off = self.lines(scenario([{"id": "a", "do": "key", "code": 4}]), video=False)
        self.assertFalse(any(l.startswith("screenrecord") for l in off))
        self.assertFalse(any(l.startswith("kill -2") for l in off))


if __name__ == "__main__":
    unittest.main()
