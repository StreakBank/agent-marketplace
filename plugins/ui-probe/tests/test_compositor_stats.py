"""The compositor's per-layer statistics as a frame witness, the real-time wait for quiet, and the
smaller behaviours fixed after the first review: scenario loading errors, rounding, no double wait."""
import sys

sys.dont_write_bytecode = True

import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import ui_probe as u  # noqa: E402

PKG = "com.example.app"

DUMP = """SurfaceFlinger TimeStats:
Legacy stats are as follows:
statsStart = 100
statsEnd = 106
totalFrames = 130
missedFrames = 3
clientCompositionFrames = 0
displayOnTime = 5480 ms
uid = 10162
layerName = com.example.app/com.example.app.MainActivity#3129
packageName = com.example.app
totalFrames = 97
droppedFrames = 0
uid = 10162
layerName = SurfaceView[com.example.app/com.example.app.MainActivity]#3130
packageName = com.example.app
totalFrames = 12
droppedFrames = 1
uid = 1000
layerName = StatusBar#91
packageName =
totalFrames = 14
droppedFrames = 0
uid = 1000
layerName = none
packageName =
totalFrames = 0
droppedFrames = 0
"""


class TimeStatsTest(unittest.TestCase):
    def test_total_and_layers(self):
        out = u.parse_timestats(DUMP)
        self.assertEqual(out["total"], 130)
        self.assertEqual(out["layers"], {
            (10162, "com.example.app/com.example.app.MainActivity#3129"): 97,
            (10162, "SurfaceView[com.example.app/com.example.app.MainActivity]#3130"): 12,
            (1000, "StatusBar#91"): 14,
        })

    def test_a_layer_in_several_blocks_is_summed(self):
        """Finding 2: the compositor keeps one record per layer and refresh-rate bucket; the frames of
        every block of a layer count, wherever the bucket lines sit in the block."""
        dump = ("totalFrames = 400\n"
                "uid = 10162\ndisplayRefreshRate = 90 fps\nrenderRate = 90 fps\n"
                "layerName = SurfaceView[com.example.app/com.example.app.MainActivity]#3130\n"
                "packageName = com.example.app\ntotalFrames = 300\n"
                "uid = 10162\nlayerName = SurfaceView[com.example.app/com.example.app.MainActivity]#3130\n"
                "displayRefreshRate = 60 fps\nrenderRate = 60 fps\n"
                "packageName = com.example.app\ntotalFrames = 7\n")
        out = u.parse_timestats(dump)
        self.assertEqual(out["layers"], {(10162, "SurfaceView[com.example.app/com.example.app.MainActivity]#3130"): 307})

    def test_unavailable(self):
        self.assertEqual(u.parse_timestats("Can't find service: SurfaceFlinger"), {"total": None, "layers": {}})

    def test_layers_of_an_ordinary_app_are_chosen_by_uid(self):
        stats = u.parse_timestats(DUMP)
        self.assertEqual(sum(u.app_layer_frames(stats, PKG, 10162).values()), 109)

    def test_a_system_uid_app_needs_its_package_in_the_layer_name(self):
        stats = {"total": 5, "layers": {(1000, "com.example.settings/.Main#1"): 4, (1000, "StatusBar#91"): 9}}
        self.assertEqual(u.app_layer_frames(stats, "com.example.settings", 1000), {"com.example.settings/.Main#1": 4})

    def test_unknown_uid_falls_back_to_the_name(self):
        stats = u.parse_timestats(DUMP)
        self.assertEqual(sum(u.app_layer_frames(stats, PKG, None).values()), 109)


def rest_run(**rest):
    launch = {"id": "launch", "kind": "launch", "start_s": 1.0, "end_s": 10.0, "frames": 20, "quiet": None,
              "process_restarted": False, "foreground": True}
    step = {"id": "idle", "kind": "rest", "start_s": 10.0, "end_s": 12.0, "frames": 0, "cpu_ticks_total": 0,
            "cpu_window_s": 2.0, "quiet": None, "process_restarted": False, "foreground": True}
    step.update(rest)
    return {"device": {"clk_tck": 100}, "steps": [launch, step], "video_truncated": False,
            "clock_mismatch_suspected": False}


SCN = {"name": "Example", "package": PKG, "steps": [{"id": "idle", "do": "rest", "ms": 2000}]}


class GateWitnessTest(unittest.TestCase):
    def test_a_second_surface_is_caught_when_the_view_system_is_idle(self):
        # The view system drew nothing, but a surface the app owns kept producing frames.
        res = u.gate_run(rest_run(frames=0, frames_layers=40), SCN, dict(u.GATE_DEFAULTS))
        self.assertEqual(res["verdict"], "FAIL")
        self.assertEqual(res["violations"][0]["observed"], 40)
        self.assertIn("compositor-stats count", res["violations"][0]["message"])

    def test_the_largest_available_count_decides(self):
        res = u.gate_run(rest_run(frames=3, frames_layers=1, frames_compositor=2), SCN, dict(u.GATE_DEFAULTS))
        self.assertEqual(res["violations"][0]["observed"], 3)
        self.assertIn("platform count", res["violations"][0]["message"])

    def test_statistics_alone_are_enough(self):
        res = u.gate_run(rest_run(frames=None, frames_layers=0), SCN, dict(u.GATE_DEFAULTS))
        self.assertEqual(res, {"verdict": "PASS", "violations": [], "notes": []})


class DeviceScriptStatsTest(unittest.TestCase):
    def lines(self, scn):
        u.validate_scenario(scn)
        return u.build_device_script(scn, 1080, 2400, False, 60).splitlines()

    def test_statistics_are_enabled_snapshotted_and_disabled(self):
        lines = self.lines({"name": "Example", "package": PKG, "steps": [{"id": "idle", "do": "rest", "ms": 1000}]})
        self.assertIn("dumpsys SurfaceFlinger --timestats -enable -clear >/dev/null 2>&1", lines)
        self.assertIn("  dumpsys SurfaceFlinger --timestats -dump > $D/sf-$1.txt 2>&1", lines)
        self.assertLess(lines.index("dumpsys SurfaceFlinger --timestats -enable -clear >/dev/null 2>&1"), lines.index("mark T0"))
        disable = "dumpsys SurfaceFlinger --timestats -disable >/dev/null 2>&1"
        self.assertGreater(len(lines) - 1 - lines[::-1].index(disable), lines.index("mark END"))

    def test_quiet_is_measured_in_real_time_on_both_counters(self):
        text = "\n".join(self.lines({"name": "Example", "package": PKG, "until_quiet": True,
                                     "steps": [{"id": "open", "do": "tap", "x": 0.5, "y": 0.5}]}))
        self.assertIn("fa=$(frames); fd=$(flips)", text)
        self.assertIn("now_cs()", text)
        self.assertIn("    last=$ca; if [ $cdisp -gt $last ]; then last=$cdisp; fi", text)
        self.assertIn("    if [ $(( (t - last) * 10 )) -ge $2 ]; then mark $1.quiet; return; fi", text)
        self.assertIn("    if [ $(( (t - t0) * 10 )) -ge $3 ]; then", text)
        self.assertIn('  if [ -z "$fa" ] && [ -z "$fd" ]; then mark $1.quiet_unavailable; sleep $4; return; fi', text)


class LoadScenarioTest(unittest.TestCase):
    def test_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(u.ProbeError) as ctx:
                u.load_scenario(str(Path(tmp) / "absent.json"))
            self.assertIn("cannot read scenario", str(ctx.exception))

    def test_not_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bad.json"
            p.write_text("{not json")
            with self.assertRaises(u.ProbeError) as ctx:
                u.load_scenario(str(p))
            self.assertIn("is not valid JSON", str(ctx.exception))


class SmallFixesTest(unittest.TestCase):
    def test_pct_rounds_halves_up(self):
        self.assertEqual(u.pct([1, 2], 0.5), 2)
        self.assertEqual(u.pct([1, 2, 3], 0.5), 2)

    def test_booleans_are_not_numbers(self):
        for bad in ({"id": "a", "do": "tap", "x": True, "y": 0.5},
                    {"id": "a", "do": "key", "code": True},
                    {"id": "a", "do": "tap", "x": 0.5, "y": 0.5, "settle_ms": "fast"}):
            with self.assertRaises(u.ProbeError):
                u.validate_scenario({"name": "Example", "package": PKG, "steps": [bad]})
        with self.assertRaises(u.ProbeError):
            u.validate_scenario({"name": "Example", "package": PKG, "quiet": {"ms": "a"},
                                 "steps": [{"id": "a", "do": "rest", "ms": 100}]})


if __name__ == "__main__":
    unittest.main()
