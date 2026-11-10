"""Statistics (mann_whitney, cliffs_delta, compare_metric, compare), summary / run_row and the
`run` and `summary` commands of ui_probe.py. No device is used: the device entry points are
replaced by functions that fail the test if called."""
import sys

sys.dont_write_bytecode = True

import contextlib
import csv
import io
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import ui_probe as u  # noqa: E402

PKG = "com.example.app"


def p_two_sided(z):
    return math.erfc(z / math.sqrt(2))


class MannWhitneyTest(unittest.TestCase):
    def test_separated_samples(self):
        # ranks of a = 1, 2, 3 -> R1 = 6, U1 = 0. No ties and three a side: the exact distribution
        # of U applies; one arrangement in C(6, 3) = 20 gives U = 0, so p = 2 / 20.
        u_stat, p = u.mann_whitney([1, 2, 3], [4, 5, 6])
        self.assertEqual(u_stat, 0)
        self.assertAlmostEqual(p, 2 / 20, places=12)
        u2, p2 = u.mann_whitney([4, 5, 6], [1, 2, 3])
        self.assertEqual((u2, round(p2, 12)), (0, round(p, 12)))

    def test_large_samples_use_the_normal_approximation(self):
        # 13 a side, no ties, complete separation: U1 = 0; var = 13*13/12 * 27
        a, b = list(range(13)), list(range(100, 113))
        u_stat, p = u.mann_whitney(a, b)
        self.assertEqual(u_stat, 0)
        self.assertAlmostEqual(p, p_two_sided((13 * 13 / 2 - 0.5) / math.sqrt(13 * 13 / 12 * 27)), places=12)

    def test_ties(self):
        # pooled 1 2 2 2 3 4 -> ranks 1, 3, 3, 3, 5, 6; R1 = 1 + 3 + 3 = 7, U1 = 7 - 6 = 1
        # tie term 3^3 - 3 = 24; var = 9/12 * (7 - 24/30) = 4.65
        u_stat, p = u.mann_whitney([1, 2, 2], [2, 3, 4])
        self.assertEqual(u_stat, 1)
        self.assertAlmostEqual(p, p_two_sided((4.5 - 1 - 0.5) / math.sqrt(4.65)), places=12)

    def test_all_tied(self):
        # every value equal: variance 0, U = 2 (all ranks 2.5), p = 1
        self.assertEqual(u.mann_whitney([5, 5], [5, 5]), (2.0, 1.0))

    def test_identical_samples_p_is_one(self):
        u_stat, p = u.mann_whitney([1, 2, 3, 4, 5], [1, 2, 3, 4, 5])
        self.assertEqual(u_stat, 12.5)
        self.assertEqual(p, 1.0)


class CliffsDeltaTest(unittest.TestCase):
    def test_values(self):
        # head > base in 6 of 9 pairs, head < base in 1 (3 vs 2): (6 - 1) / 9
        self.assertAlmostEqual(u.cliffs_delta([1, 2, 3], [2, 3, 4]), 5 / 9)
        self.assertEqual(u.cliffs_delta([1, 2], [3, 4]), 1.0)
        self.assertEqual(u.cliffs_delta([3, 4], [1, 2]), -1.0)
        self.assertEqual(u.cliffs_delta([1, 2, 3], [1, 2, 3]), 0.0)
        self.assertEqual(u.cliffs_delta([2, 2], [2, 2]), 0.0)


class CompareMetricTest(unittest.TestCase):
    def test_different(self):
        out = u.compare_metric([1, 2, 3, 4, 5, 6], [11, 12, 13, 14, 15, 16], 0.01, 5)
        self.assertEqual(out["verdict"], "different")
        self.assertEqual(out["reason"], "significant and large")
        self.assertEqual(out["cliffs_delta"], 1.0)
        self.assertEqual(out["u"], 0)
        self.assertLess(out["p"], 0.01)
        self.assertEqual((out["median_base"], out["median_head"]), (3.5, 13.5))

    def test_five_against_five_can_be_different(self):
        """Finding 8: at the documented minimum of five runs a side, complete separation is
        significant: the exact two-sided p is 2 / C(10, 5) = 2 / 252."""
        out = u.compare_metric([1, 2, 3, 4, 5], [6, 7, 8, 9, 10], 0.01, 5)
        self.assertEqual(out["p"], round(2 / 252, 5))
        self.assertEqual(out["verdict"], "different")
        self.assertEqual(u.mann_whitney([1, 2, 3, 4, 5], [6, 7, 8, 9, 10]), (0, 2 / 252))

    def test_exact_distribution_counts(self):
        # U for 2 against 2 values: 0, 1, 2, 2, 3, 4 over the C(4, 2) = 6 arrangements
        self.assertEqual(u._u_counts(2, 2), [1, 1, 2, 1, 1])
        self.assertEqual(sum(u._u_counts(6, 7)), 1716)

    def test_same(self):
        out = u.compare_metric([1, 2, 3, 4, 5, 6], [6, 5, 4, 3, 2, 1], 0.01, 5)
        self.assertEqual(out["verdict"], "same")
        self.assertEqual(out["reason"], "not significant and negligible")
        self.assertEqual(out["p"], 1.0)

    def test_unknown_between(self):
        # delta = (15 - 6) / 25 = 0.36: neither negligible nor large, and not significant
        out = u.compare_metric([1, 2, 3, 4, 5], [2, 3, 4, 5, 6], 0.01, 5)
        self.assertEqual(out["verdict"], "unknown")
        self.assertEqual(out["reason"], "between the two")
        self.assertEqual(out["cliffs_delta"], 0.36)

    def test_constant_both_sides(self):
        self.assertEqual(u.compare_metric([0, 0, 0], [0, 0, 0], 0.01, 5)["verdict"], "same")
        out = u.compare_metric([0, 0, 0], [1, 1, 1], 0.01, 5)
        self.assertEqual(out["verdict"], "different")
        self.assertEqual(out["reason"], "constant on both sides and not equal")

    def test_missing_data(self):
        out = u.compare_metric([None, None], [1, 2], 0.01, 5)
        self.assertEqual(out, {"n_base": 0, "n_head": 2, "verdict": "unknown", "reason": "missing on one side"})
        self.assertEqual(u.compare_metric([1], [], 0.01, 5)["verdict"], "unknown")

    def test_none_values_dropped(self):
        out = u.compare_metric([1, None, 2, 3, 4, 5], [None, 1, 2, 3, 4, 5], 0.01, 5)
        self.assertEqual((out["n_base"], out["n_head"], out["verdict"]), (5, 5, "same"))

    def test_too_few_runs(self):
        out = u.compare_metric([1, 2, 3], [4, 5, 6], 0.01, 5)
        self.assertEqual(out["verdict"], "unknown")
        self.assertEqual(out["reason"], "fewer than 5 runs on a side")
        self.assertNotIn("p", out)

    def test_constant_needs_two_runs_unless_single_runs_are_accepted(self):
        # One run per side is not an observation of "constant": unknown under the default.
        out = u.compare_metric([1], [2], 0.01, 5)
        self.assertEqual(out["verdict"], "unknown")
        self.assertEqual(out["reason"], "fewer than 5 runs on a side")
        # Two runs per side that agree with themselves and differ from the other side.
        self.assertEqual(u.compare_metric([1, 1], [2, 2], 0.01, 5)["verdict"], "different")
        self.assertEqual(u.compare_metric([3, 3], [3, 3], 0.01, 5)["verdict"], "same")
        # A caller who accepts single runs gets the exact comparison.
        self.assertEqual(u.compare_metric([1], [2], 0.01, 1)["verdict"], "different")


def make_run(load=1.0, extra_step=False, signature="T", frames=12):
    steps = [
        {"id": "launch", "kind": "launch", "frames": 30, "janky_frames": 1, "frame_ms_p50": 8.0, "frame_ms_p90": 12.0,
         "frame_ms_max": 40.0, "first_frame_completed_after_step_start_ms": None,
         "cpu_ticks_by_thread": None, "cpu_ticks_total": None, "quiet": "reached", "quiet_after_ms": 900},
        {"id": "open", "kind": "tap", "frames": frames, "frames_compositor": 13, "janky_frames": 0,
         "frame_ms_p50": 6.0, "frame_ms_p90": 9.0, "frame_ms_max": 11.0,
         "first_frame_completed_after_step_start_ms": 45.0,
         "cpu_ticks_by_thread": {"<main>": 20, "RenderThread": 8, "other": 2}, "cpu_ticks_total": 30, "quiet": None,
         "sections": {"Choreographer#doFrame": 12, "draw": 12}, "sections_all": 40},
        {"id": "idle", "kind": "rest", "frames": 0, "janky_frames": 0, "frame_ms_p50": None, "frame_ms_p90": None,
         "frame_ms_max": None, "first_frame_completed_after_step_start_ms": None,
         "cpu_ticks_by_thread": {}, "cpu_ticks_total": 0, "quiet": None,
         "meminfo": {"total_pss_kb": 51234}},
    ]
    if extra_step:
        steps.append({"id": "back", "kind": "key", "frames": 4, "janky_frames": 0, "frame_ms_p50": 5.0,
                      "frame_ms_p90": 6.0, "frame_ms_max": 7.0, "first_frame_completed_after_step_start_ms": 30.0,
                      "cpu_ticks_by_thread": {"<main>": 3}, "cpu_ticks_total": 3, "quiet": None})
    video_steps = [{"id": "open", "video_frames": 9, "app_region_frames": 8, "identical_frames": 0, "bursts": 1,
                    "signature": signature, "first_change_after_step_start_ms": 60.0,
                    "last_change_after_step_start_ms": 320.0, "max_gap_in_burst_ms": 33.0,
                    "detour_frames": 0, "detour_ms": 0.0}]
    return {"host": {"load1_before": load}, "launch": {"total_time_ms": 800}, "steps": steps,
            "video": {"frames": 25, "steps": video_steps}, "frametimeline": {"app_buffer_frames": 55}}


class RunRowTest(unittest.TestCase):
    def test_keys(self):
        row = u.run_row(str(Path("runs") / "run-01"), make_run())
        self.assertEqual(row["run"], "run-01")
        self.assertEqual(row["host_load1"], 1.0)
        self.assertEqual(row["launch_total_time_ms"], 800)
        self.assertEqual(row["launch.quiet"], "reached")
        self.assertEqual(row["launch.quiet_after_ms"], 900)
        self.assertIsNone(row["launch.cpu_ticks"])
        self.assertIsNone(row["launch.render_thread_ticks"])
        self.assertNotIn("launch.frames_compositor", row)
        self.assertEqual(row["open.frames"], 12)
        self.assertEqual(row["open.frames_compositor"], 13)
        self.assertEqual(row["open.cpu_ticks"], 30)
        self.assertEqual(row["open.render_thread_ticks"], 8)
        self.assertEqual(row["open.main_thread_ticks"], 20)
        self.assertEqual(row["open.first_frame_ms"], 45.0)
        self.assertEqual(row["open.sec.Choreographer#doFrame"], 12)
        self.assertEqual(row["open.sec.draw"], 12)
        self.assertEqual(row["open.sec.ALL"], 40)
        self.assertNotIn("open.quiet", row)
        self.assertEqual(row["idle.render_thread_ticks"], 0)
        self.assertEqual(row["idle.pss_kb"], 51234)
        self.assertEqual(row["video.frames"], 25)
        for key, value in {"open.video_frames": 9, "open.video_app_region_frames": 8, "open.video_identical_frames": 0,
                           "open.bursts": 1, "open.signature": "T", "open.first_change_ms": 60.0,
                           "open.settled_ms": 320.0, "open.max_gap_ms": 33.0, "open.detour_frames": 0,
                           "open.detour_ms": 0.0}.items():
            self.assertEqual(row[key], value, key)
        self.assertEqual(row["frametimeline.app_buffer_frames"], 55)

    def test_without_video_or_trace(self):
        run = make_run()
        del run["video"], run["frametimeline"]
        row = u.run_row("run-01", run)
        self.assertFalse(any(k.startswith("video.") or k.startswith("frametimeline.") for k in row))
        self.assertNotIn("open.signature", row)


def write_runs(root, runs):
    # capture first, analysis second: a run.json is current only if it is not older than its capture
    for name, run in runs.items():
        d = Path(root) / name
        d.mkdir(parents=True)
        (d / "capture.json").write_text(json.dumps({"scenario": {"name": "Example", "package": PKG, "steps": []}}))
        (d / "run.json").write_text(json.dumps(run))


class SummaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_keys_kept_across_runs(self):
        write_runs(self.root, {"run-01": make_run(load=1.0, signature="T"),
                               "run-02": make_run(load=2.0, extra_step=True, signature="T E", frames=14)})
        out = u.summary([self.root])
        self.assertEqual([r["run"] for r in out["runs"]], ["run-01", "run-02"])
        stats = out["stats"]
        self.assertIn("back.frames", stats)          # only in the second run
        self.assertEqual(stats["back.frames"]["n"], 1)
        self.assertEqual(stats["open.frames"], {"n": 2, "min": 12, "max": 14, "median": 13.0, "mean": 13.0,
                                                "distinct": 2, "stdev": 1.414, "cv_pct": 10.9})
        self.assertEqual(stats["host_load1"]["median"], 1.5)
        self.assertEqual(stats["open.signature"], {"n": 2, "distinct": 2, "values": sorted(['"T"', '"T E"'])})
        self.assertEqual(stats["launch.cpu_ticks"], {"n": 0})
        self.assertEqual(stats["idle.frames"]["cv_pct"], None)    # mean 0
        self.assertNotIn("run", stats)

    def test_summary_csv_command(self):
        write_runs(self.root, {"run-01": make_run(), "run-02": make_run(extra_step=True)})
        csv_path = Path(self.root) / "rows.csv"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = u.main(["summary", self.root, "--csv", str(csv_path)])
        self.assertEqual(rc, 0)
        self.assertIn("open.frames", out.getvalue())
        with open(csv_path, newline="") as fh:
            rows = list(csv.DictReader(fh))
        self.assertEqual([r["run"] for r in rows], ["run-01", "run-02"])
        self.assertEqual(rows[0]["back.frames"], "")
        self.assertEqual(rows[1]["back.frames"], "4")
        self.assertEqual(rows[0]["open.signature"], "T")

    def test_summary_json_command(self):
        write_runs(self.root, {"run-01": make_run()})
        js = Path(self.root) / "summary.json"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(u.main(["summary", self.root, "--json", str(js)]), 0)
        self.assertEqual(len(json.loads(js.read_text())["runs"]), 1)

    def test_compare_dirs(self):
        base = Path(self.root) / "base"
        head = Path(self.root) / "head"
        write_runs(base, {f"run-{i:02d}": make_run(frames=12) for i in range(5)})
        write_runs(head, {f"run-{i:02d}": make_run(frames=20, signature="T E") for i in range(5)})
        out = u.compare([str(base)], [str(head)], [], 0.01, 5)
        m = out["metrics"]
        self.assertEqual(m["open.frames"]["verdict"], "different")
        self.assertEqual(m["open.cpu_ticks"]["verdict"], "same")
        self.assertNotIn("host_load1", m)
        self.assertNotIn("open.signature", m)       # strings are compared only when named
        self.assertEqual(list(m)[0], "open.frames")  # 'different' sorts first
        named = u.compare([str(base)], [str(head)], ["open.signature"], 0.01, 5)["metrics"]
        self.assertEqual(named["open.signature"]["verdict"], "different")
        self.assertEqual(named["open.signature"]["head"], ["T E"])


def no_device(*_a, **_k):
    raise AssertionError("the device was touched")


class RunCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cmd(self, scenario_path):
        err = io.StringIO()
        with mock.patch.object(u, "adb", no_device), mock.patch.object(u, "sh", no_device), \
                mock.patch.object(u.subprocess, "run", no_device), mock.patch.object(u.subprocess, "Popen", no_device), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = u.main(["run", "--serial", "<serial>", "--scenario", str(scenario_path),
                         "--out", str(self.root / "out")])
        return rc, err.getvalue()

    def test_invalid_scenario_returns_2(self):
        p = self.root / "scenario.json"
        p.write_text(json.dumps({"name": "Example", "package": PKG, "steps": [{"id": "launch", "do": "key", "code": 4}]}))
        rc, err = self.run_cmd(p)
        self.assertEqual(rc, 2)
        self.assertIn("id 'launch' is reserved", err)
        self.assertFalse((self.root / "out").exists())

    def test_missing_scenario_file_returns_2(self):
        """Fixed defect: main() catches only ProbeError, so a missing scenario file raises
        FileNotFoundError out of main() instead of returning 2."""
        self.assertEqual(self.run_cmd(self.root / "absent.json")[0], 2)

    def test_malformed_json_returns_2(self):
        """Fixed defect: a scenario file that is not JSON raises json.JSONDecodeError out of main()."""
        p = self.root / "scenario.json"
        p.write_text("{not json")
        self.assertEqual(self.run_cmd(p)[0], 2)


if __name__ == "__main__":
    unittest.main()
