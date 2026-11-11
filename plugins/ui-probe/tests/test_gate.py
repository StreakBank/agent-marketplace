"""gate_run / gate / the `gate` command of ui_probe.py, on synthetic run dictionaries.

The run shape follows what analyze_run writes to run.json: a 'steps' list of per-step records
(id, kind, start_s, end_s, frames, cpu_ticks_total, cpu_window_s, quiet, process_restarted,
optional frames_compositor and sections), a device block with clk_tck and an optional video block
(clock, steps) whose 'steps' carry identical_frames, app_region_frames, app_changed_frames_real
and signature.
"""
import sys

sys.dont_write_bytecode = True

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import ui_probe as u  # noqa: E402

PKG = "com.example.app"
DEFAULTS = dict(u.GATE_DEFAULTS)


def step(sid, kind, start, end, frames=0, cpu=0, **kw):
    rec = {"id": sid, "kind": kind, "start_s": start, "end_s": end, "frames": frames, "janky_frames": 0,
           "counters": {}, "process_restarted": False, "rows_in_window": 0, "rows_flagged": 0,
           "frame_ms_p50": None, "frame_ms_p90": None, "frame_ms_max": None,
           "first_frame_completed_after_step_start_ms": None,
           "cpu_ticks_by_thread": {} if cpu is not None else None, "cpu_ticks_total": cpu,
           "cpu_window_s": (end - start) if cpu is not None else None, "quiet": None,
           "foreground": True, "foreground_component": f"{PKG}/.Main"}
    rec.update(kw)
    return rec


def vstep(sid, identical=0, region=0, real=None, signature="-"):
    rec = {"id": sid, "identical_frames": identical, "app_region_frames": region, "signature": signature}
    if real is not None:
        rec["app_changed_frames_real"] = real
    return rec


def make_run(steps, video_steps=None, clk_tck=100, clock="recorder-timestamps", **kw):
    run = {"tool": {"name": "ui-probe", "version": u.VERSION}, "scenario": "Example", "package": PKG,
           "device": {"clk_tck": clk_tck}, "host": {"load1_before": 1.0, "load1_after": 1.0, "wall_s": 20.0},
           "launch": {"state": "COLD", "total_time_ms": 800}, "marks": {}, "steps": steps,
           "frames_total_rows": 0, "hwui_reported": True, "frame_sources": ["platform"],
           "clock_mismatch_suspected": False, "video_truncated": False}
    if video_steps is not None:
        run["video"] = {"frames": 10, "clock": clock, "steps": video_steps}
    run.update(kw)
    return run


SCN = {"name": "Example", "package": PKG, "steps": [
    {"id": "open", "do": "tap", "x": 0.5, "y": 0.5},
    {"id": "idle", "do": "rest", "ms": 2000},
]}


def base_steps(**rest_kw):
    # rest window 10.0 .. 12.0 s = 2000 ms
    return [step("launch", "launch", 1.0, 5.0, frames=30, cpu=None),
            step("open", "tap", 5.0, 10.0, frames=12, cpu=40),
            step("idle", "rest", 10.0, 12.0, **rest_kw)]


def checks(res):
    return [v["check"] for v in res["violations"]]


class GateRunTest(unittest.TestCase):
    def test_clean_run_passes(self):
        res = u.gate_run(make_run(base_steps(), [vstep("idle")]), SCN, DEFAULTS)
        self.assertEqual(res, {"verdict": "PASS", "violations": [], "notes": []})

    def test_frames_at_rest_fail(self):
        res = u.gate_run(make_run(base_steps(frames=3)), SCN, DEFAULTS)
        self.assertEqual(res["verdict"], "FAIL")
        v = res["violations"][0]
        self.assertEqual((v["check"], v["step"], v["observed"], v["allowed"]), ("rest-frames", "idle", 3, 0))
        self.assertIn("platform count", v["message"])
        self.assertEqual(u.gate_run(make_run(base_steps(frames=3)), SCN, {**DEFAULTS, "rest_frames_max": 3})["verdict"],
                         "PASS")

    def test_cpu_at_rest_percentage(self):
        # clk_tck 100: one tick = 10 ms. 4 ticks in 2000 ms = 2.0 % (allowed, the limit is exclusive)
        self.assertEqual(u.gate_run(make_run(base_steps(cpu=4)), SCN, DEFAULTS)["verdict"], "PASS")
        res = u.gate_run(make_run(base_steps(cpu=5)), SCN, DEFAULTS)
        self.assertEqual(checks(res), ["rest-cpu"])
        self.assertEqual(res["violations"][0]["observed"], 2.5)
        self.assertEqual(res["violations"][0]["allowed"], 2.0)
        self.assertIn("(5 ticks)", res["violations"][0]["message"])
        # the same 5 ticks at clk_tck 1000 are 5 ms: 0.25 %
        self.assertEqual(u.gate_run(make_run(base_steps(cpu=5), clk_tck=1000), SCN, DEFAULTS)["verdict"], "PASS")
        # clk_tck missing falls back to 100
        res = u.gate_run(make_run(base_steps(cpu=5), clk_tck=None), SCN, DEFAULTS)
        self.assertEqual(res["violations"][0]["observed"], 2.5)

    def test_cpu_unavailable_is_a_note(self):
        res = u.gate_run(make_run(base_steps(cpu=None)), SCN, DEFAULTS)
        self.assertEqual(res["verdict"], "PASS")
        self.assertIn("idle: CPU at rest not available", res["notes"])

    def test_identical_video_frames_at_rest_fail(self):
        res = u.gate_run(make_run(base_steps(), [vstep("idle", identical=2)]), SCN, DEFAULTS)
        self.assertEqual(checks(res), ["rest-video"])
        self.assertEqual(res["violations"][0]["observed"], 2)
        self.assertIn("changed nothing on screen", res["violations"][0]["message"])

    def test_real_app_area_change_at_rest_fails(self):
        res = u.gate_run(make_run(base_steps(), [vstep("idle", region=4, real=4)]), SCN, DEFAULTS)
        self.assertEqual(checks(res), ["rest-video"])
        self.assertEqual(res["violations"][0]["observed"], 4)
        self.assertIn("the screen changed at rest", res["violations"][0]["message"])

    def test_noise_only_change_at_rest_passes(self):
        res = u.gate_run(make_run(base_steps(), [vstep("idle", region=4, real=0)]), SCN, DEFAULTS)
        self.assertEqual(res["verdict"], "PASS")

    def test_region_count_used_without_real_count(self):
        # a run analysed before app_changed_frames_real existed falls back to app_region_frames
        res = u.gate_run(make_run(base_steps(), [vstep("idle", region=2)]), SCN, DEFAULTS)
        self.assertEqual(checks(res), ["rest-video"])

    def test_quiet_timeout_fails_unavailable_is_note(self):
        steps = base_steps()
        steps[1]["quiet"] = "timeout"
        res = u.gate_run(make_run(steps), SCN, DEFAULTS)
        self.assertEqual(checks(res), ["quiet"])
        self.assertEqual(res["violations"][0]["step"], "open")
        steps[1]["quiet"] = "unavailable"
        res = u.gate_run(make_run(steps), SCN, DEFAULTS)
        self.assertEqual(res["verdict"], "PASS")
        self.assertTrue(any(n.startswith("open: frames could not be polled") for n in res["notes"]))
        steps[1]["quiet"] = "reached"
        self.assertEqual(u.gate_run(make_run(steps), SCN, DEFAULTS), {"verdict": "PASS", "violations": [], "notes": []})

    def test_process_restart_fails(self):
        steps = base_steps()
        steps[1]["process_restarted"] = True
        res = u.gate_run(make_run(steps), SCN, DEFAULTS)
        self.assertEqual(checks(res), ["process"])
        self.assertEqual(res["violations"][0]["step"], "open")

    def test_truncated_recording_fails(self):
        res = u.gate_run(make_run(base_steps(), video_truncated=True), SCN, DEFAULTS)
        self.assertEqual(checks(res), ["recording"])
        self.assertIsNone(res["violations"][0]["step"])

    def test_rest_with_no_witness_fails(self):
        res = u.gate_run(make_run(base_steps(frames=None)), SCN, DEFAULTS)
        self.assertEqual(checks(res), ["rest-frames"])
        self.assertIn("cannot count frames", res["violations"][0]["message"])

    def test_rest_with_no_witness_has_no_contradictory_note(self):
        """Fixed: with no frame count and no recording, gate_run records the violation and also
        the note 'only the recording was checked', although there was no recording."""
        res = u.gate_run(make_run(base_steps(frames=None)), SCN, DEFAULTS)
        self.assertFalse(any("only the recording was checked" in n for n in res["notes"]))

    def test_rest_recording_only(self):
        res = u.gate_run(make_run(base_steps(frames=None), [vstep("idle")]), SCN, DEFAULTS)
        self.assertEqual(res["verdict"], "PASS")
        self.assertIn("idle: no platform frame count for this app; only the recording was checked", res["notes"])

    def test_compositor_count_used_without_hwui(self):
        res = u.gate_run(make_run(base_steps(frames=None, frames_compositor=2)), SCN, DEFAULTS)
        self.assertEqual(checks(res), ["rest-frames"])
        self.assertEqual(res["violations"][0]["observed"], 2)
        self.assertIn("compositor-trace count", res["violations"][0]["message"])
        res = u.gate_run(make_run(base_steps(frames=None, frames_compositor=0)), SCN, DEFAULTS)
        self.assertEqual(res, {"verdict": "PASS", "violations": [], "notes": []})
        # every available count is consulted and the largest decides: an app can draw through a
        # surface of its own while its views are idle
        res = u.gate_run(make_run(base_steps(frames=0, frames_compositor=9)), SCN, DEFAULTS)
        self.assertEqual(res["verdict"], "FAIL")
        self.assertEqual(res["violations"][0]["observed"], 9)
        self.assertIn("compositor-trace count", res["violations"][0]["message"])

    def test_clock_mismatch_note(self):
        res = u.gate_run(make_run(base_steps(), clock_mismatch_suspected=True), SCN, DEFAULTS)
        self.assertEqual(res["verdict"], "PASS")
        self.assertTrue(any("do not line up" in n for n in res["notes"]))


def with_expect(expect):
    return {**SCN, "steps": [{**SCN["steps"][0], "expect": expect}, SCN["steps"][1]]}


class ExpectTest(unittest.TestCase):
    def gate(self, expect, open_kw=None, video=None):
        steps = base_steps()
        steps[1].update(open_kw or {})
        return u.gate_run(make_run(steps, video), with_expect(expect), DEFAULTS)

    def test_signature(self):
        video = [vstep("open", signature="T"), vstep("idle")]
        self.assertEqual(self.gate({"signature": "T"}, video=video)["verdict"], "PASS")
        res = self.gate({"signature": "T E"}, video=video)
        self.assertEqual(checks(res), ["signature"])
        self.assertEqual((res["violations"][0]["observed"], res["violations"][0]["allowed"]), ("T", "T E"))
        res = self.gate({"signature": "T"}, open_kw={})
        self.assertIn("no recording", res["violations"][0]["message"])

    def test_frames_exact(self):
        self.assertEqual(self.gate({"frames": 12})["verdict"], "PASS")
        res = self.gate({"frames": 11})
        self.assertEqual(checks(res), ["frames"])
        self.assertEqual((res["violations"][0]["observed"], res["violations"][0]["allowed"]), (12, 11))
        # compositor count when HWUI has none
        self.assertEqual(self.gate({"frames": 7}, {"frames": None, "frames_compositor": 7})["verdict"], "PASS")

    def test_frames_max(self):
        self.assertEqual(self.gate({"frames_max": 12})["verdict"], "PASS")
        res = self.gate({"frames_max": 11})
        self.assertEqual(checks(res), ["frames"])
        self.assertIn("approved at most 11", res["violations"][0]["message"])
        res = self.gate({"frames_max": 100}, {"frames": None})
        self.assertEqual(checks(res), ["frames"])
        self.assertIsNone(res["violations"][0]["observed"])

    def test_sections_max(self):
        kw = {"sections": {"Choreographer#doFrame": 5, "draw": 2}}
        self.assertEqual(self.gate({"sections_max": {"Choreographer#doFrame": 5}}, kw)["verdict"], "PASS")
        res = self.gate({"sections_max": {"Choreographer#doFrame": 4}}, kw)
        self.assertEqual(checks(res), ["sections"])
        self.assertEqual((res["violations"][0]["observed"], res["violations"][0]["allowed"]), (5, 4))
        res = self.gate({"sections_max": {"Custom#section": 1}}, kw)
        self.assertIn("'Custom#section' was not counted", res["violations"][0]["message"])
        res = self.gate({"sections_max": {"draw": 9}})
        self.assertIn("was not counted", res["violations"][0]["message"])


def write_run(root, name, run, scn):
    d = Path(root) / name
    d.mkdir(parents=True)
    (d / "capture.json").write_text(json.dumps({"scenario": scn}))
    (d / "run.json").write_text(json.dumps(run))
    return d


class GateEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_scenario_block_and_override(self):
        # 5 ticks over 2000 ms at clk_tck 100 = 2.5 %: fails the default 2.0, passes a scenario limit of 3.0
        scn = {**SCN, "gate": {"rest_cpu_pct_max": 3.0}}
        write_run(self.root, "run-01", make_run(base_steps(cpu=5)), scn)
        out = u.gate([self.root], {})
        self.assertEqual(out["verdict"], "PASS")
        self.assertEqual(out["runs"][0]["limits"], {"rest_frames_max": 0, "rest_cpu_pct_max": 3.0, "rest_updates_max": 0,
                                                    "rest_requests_max": 0})
        self.assertEqual(out["runs"][0]["scenario"], "Example")
        self.assertEqual(out["runs"][0]["frame_sources"], ["platform"])
        # a None override is ignored
        self.assertEqual(u.gate([self.root], {"rest_cpu_pct_max": None})["verdict"], "PASS")
        # a command-line override wins over the scenario block and the default
        out = u.gate([self.root], {"rest_cpu_pct_max": 1.0, "rest_frames_max": None})
        self.assertEqual(out["verdict"], "FAIL")
        self.assertEqual(out["runs"][0]["limits"]["rest_cpu_pct_max"], 1.0)

    def test_default_applies_without_block(self):
        write_run(self.root, "run-01", make_run(base_steps(cpu=5)), SCN)
        self.assertEqual(u.gate([self.root], {})["verdict"], "FAIL")
        # a scenario passed explicitly replaces the one in the capture
        self.assertEqual(u.gate([self.root], {}, {**SCN, "gate": {"rest_cpu_pct_max": 5}})["verdict"], "PASS")

    def test_one_failing_run_fails_the_whole(self):
        write_run(self.root, "run-01", make_run(base_steps()), SCN)
        write_run(self.root, "run-02", make_run(base_steps(frames=1)), SCN)
        out = u.gate([self.root], {})
        self.assertEqual([r["verdict"] for r in out["runs"]], ["PASS", "FAIL"])
        self.assertEqual(out["verdict"], "FAIL")

    def test_capture_without_scenario(self):
        d = Path(self.root) / "run-01"
        d.mkdir()
        (d / "capture.json").write_text("{}")
        (d / "run.json").write_text(json.dumps(make_run(base_steps())))
        with self.assertRaises(u.ProbeError):
            u.gate([self.root], {})

    def test_no_runs(self):
        with self.assertRaises(u.ProbeError):
            u.gate([self.root], {})


class GateCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = u.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_pass_returns_0(self):
        write_run(self.root, "run-01", make_run(base_steps()), SCN)
        rc, out, _ = self.main("gate", self.root)
        self.assertEqual(rc, 0)
        self.assertIn("gate: PASS", out)

    def test_fail_returns_1_and_advisory_0(self):
        write_run(self.root, "run-01", make_run(base_steps(frames=2)), SCN)
        rc, out, _ = self.main("gate", self.root)
        self.assertEqual(rc, 1)
        self.assertIn("FAIL [rest-frames] idle:", out)
        self.assertIn("gate: FAIL", out)
        rc, out, _ = self.main("gate", self.root, "--advisory")
        self.assertEqual(rc, 0)
        self.assertIn("gate: FAIL (advisory)", out)

    def test_cli_limit_and_json(self):
        write_run(self.root, "run-01", make_run(base_steps(frames=2)), SCN)
        report = Path(self.root) / "gate.json"
        rc, _, _ = self.main("gate", self.root, "--rest-frames-max", "2", "--json", str(report))
        self.assertEqual(rc, 0)
        data = json.loads(report.read_text())
        self.assertEqual(data["verdict"], "PASS")
        self.assertEqual(data["runs"][0]["limits"]["rest_frames_max"], 2)

    def test_scenario_file_override(self):
        write_run(self.root, "run-01", make_run(base_steps(cpu=5)), SCN)
        scn_file = Path(self.root) / "scenario.json"
        scn_file.write_text(json.dumps({**SCN, "gate": {"rest_cpu_pct_max": 3}}))
        self.assertEqual(self.main("gate", self.root)[0], 1)
        self.assertEqual(self.main("gate", self.root, "--scenario", str(scn_file))[0], 0)

    def test_no_runs_returns_2(self):
        rc, _, err = self.main("gate", self.root)
        self.assertEqual(rc, 2)
        self.assertIn("no run found", err)


if __name__ == "__main__":
    unittest.main()
