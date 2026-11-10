"""analyze_run and gate on synthetic captures: raw files written to a temporary directory in the
shapes the device script leaves, then analysed and gated with no device. Each test names the
review finding it covers."""
import sys

sys.dont_write_bytecode = True

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pftrace  # noqa: E402
import ui_probe as u  # noqa: E402
import ui_video  # noqa: E402

PKG = "com.example.app"
UID = 10100
PID = 4321

SCN = {"name": "Example", "package": PKG, "steps": [
    {"id": "idle", "do": "rest", "ms": 2000},
    {"id": "open", "do": "tap", "x": 0.5, "y": 0.5},
    {"id": "idle2", "do": "rest", "ms": 2000},
]}
ORDER = ["launch", "idle", "open", "idle2"]

MARKS = {"T0": 100.0, "launch.start": 101.5, "launch.end": 102.0, "idle.start": 110.0, "idle.end": 112.0,
         "open.start": 112.5, "open.injected": 112.6, "open.end": 115.0, "idle2.start": 115.6,
         "idle2.end": 117.6, "END": 118.0}
# uptime stamps of the two CPU readings each snapshot takes: (first, after the dumps)
STAMPS = {"launch": (109.0, 109.4), "idle": (112.1, 112.4), "open": (115.1, 115.5), "idle2": (117.7, 118.0)}
# process CPU ticks at each reading
PROC = {"launch": (50, 53), "idle": (53, 54), "open": (80, 82), "idle2": (82, 83)}
GFX = {"launch": 30, "idle": 30, "open": 42, "idle2": 42}
SF_TOTAL = {"launch": 100, "idle": 110, "open": 160, "idle2": 170}
SF_APP = {"launch": 30, "idle": 30, "open": 42, "idle2": 42}

AM_OK = "Starting: Intent { cmp=com.example.app/.Main }\nStatus: ok\nLaunchState: COLD\nTotalTime: 500\nComplete\n"


def stat(tid, comm, ticks):
    return f"{tid} ({comm}) S 1 2 3 4 5 6 7 8 9 10 {ticks} 0 0 0 20 0 1 0"


def gfx_text(total):
    if total is None:
        return f"No process found for: {PKG}\n"
    return f"** Graphics info for pid {PID} [{PKG}] **\nTotal frames rendered: {total}\nJanky frames: 0 (0.00%)\n"


def sf_text(total, app_frames, uid=UID):
    out = f"SurfaceFlinger TimeStats:\ntotalFrames = {total}\nmissedFrames = 0\n"
    if app_frames is not None:
        out += f"uid = {uid}\nlayerName = {PKG}/{PKG}.Main#1\npackageName = {PKG}\ntotalFrames = {app_frames}\n"
    out += "uid = 1000\nlayerName = StatusBar#2\npackageName =\ntotalFrames = 5\n"
    return out


def fg_text(package=PKG):
    """The line the device script saves: the resumed activity."""
    return f"    topResumedActivity=ActivityRecord{{c0ffee1 u0 {package}/.Main t12}}\n"


def cpu_text(uptime, proc, threads, pid=PID):
    lines = [f"uptime {uptime}", "process", stat(pid, PKG, proc), "threads"]
    lines += [stat(tid, comm, t) for tid, (comm, t) in threads.items()]
    return "\n".join(lines) + "\n"


def healthy_files():
    files = {"marks.txt": "".join(f"{k} {v}\n" for k, v in MARKS.items()), "am-start.txt": AM_OK}
    for sid in ORDER:
        a, b = PROC[sid]
        files[f"gfx-{sid}.txt"] = gfx_text(GFX[sid])
        files[f"sf-{sid}.txt"] = sf_text(SF_TOTAL[sid], SF_APP[sid])
        files[f"pid-{sid}.txt"] = f"{PID}\n"
        files[f"fg-{sid}.txt"] = fg_text()
        files[f"cpu-{sid}.txt"] = cpu_text(STAMPS[sid][0], a, {str(PID): (PKG, a - 10), "4330": ("RenderThread", 10)})
        files[f"cpu-{sid}.after.txt"] = cpu_text(STAMPS[sid][1], b, {str(PID): (PKG, b - 10), "4330": ("RenderThread", 10)})
    return files


class Capture:
    """A run directory with a capture.json and raw files."""

    def __init__(self, root, files=None, scn=SCN, video=False, trace=False, sections=False, name="run-01"):
        self.dir = Path(root) / name
        raw = self.dir / "raw"
        raw.mkdir(parents=True)
        (self.dir / "capture.json").write_text(json.dumps({
            "tool": {"name": "ui-probe", "version": u.VERSION}, "scenario": scn,
            "device": {"uid": UID, "clk_tck": 100, "width": 1080, "height": 2400, "stable_insets_top_bottom": None},
            "trace": trace, "video": video, "sections": sections, "record_limit_s": 120,
            "host": {"load1_before": 1.0, "load1_after": 1.0, "wall_s": 20.0}}))
        for name_, text in (healthy_files() if files is None else files).items():
            if text is not None:
                (raw / name_).write_text(text)

    def analyze(self, scn=None):
        return u.analyze_run(self.dir, scn)

    def gate(self, scn=None, overrides=None):
        return u.gate([str(self.dir)], overrides or {}, scn)["runs"][0]


def checks(res):
    return [v["check"] for v in res["violations"]]


def steps_by_id(run):
    return {s["id"]: s for s in run["steps"]}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()


class HealthyRunTest(Base):
    def test_a_healthy_capture_passes(self):
        cap = Capture(self.root)
        run = cap.analyze()
        st = steps_by_id(run)
        self.assertEqual([s["id"] for s in run["steps"]], ORDER)
        self.assertEqual((st["idle"]["frames"], st["idle"]["frames_layers"], st["idle"]["frames_display"]), (0, 0, 10))
        self.assertEqual((st["open"]["frames"], st["open"]["frames_layers"]), (12, 12))
        res = cap.gate()
        self.assertEqual(res["verdict"], "PASS", res)


class AppNeverStartedTest(Base):
    """Finding 1: a run where the app never started must fail, not pass."""

    def files(self):
        files = healthy_files()
        files["am-start.txt"] = ("Starting: Intent { cmp=com.example.app/.Outer }\n"
                                 "Error type 3\nError: Activity class {com.example.app/com.example.app.Outer} does not exist.\n")
        for sid in ORDER:
            files[f"gfx-{sid}.txt"] = gfx_text(None)
            files[f"sf-{sid}.txt"] = sf_text(SF_TOTAL[sid], None)
            files[f"pid-{sid}.txt"] = "\n"
            files[f"fg-{sid}.txt"] = fg_text("com.example.launcher")
            files[f"cpu-{sid}.txt"] = f"uptime {STAMPS[sid][0]}\n"
            files[f"cpu-{sid}.after.txt"] = f"uptime {STAMPS[sid][1]}\n"
        return files

    def test_gate_fails(self):
        cap = Capture(self.root, self.files())
        run = cap.analyze()
        res = cap.gate()
        self.assertEqual(res["verdict"], "FAIL")
        self.assertEqual(run["launch"]["error"], "Error type 3")
        for s in run["steps"]:
            self.assertIsNone(s["frames_layers"], s["id"])      # no app layer: unknown, not 0
            self.assertTrue(s["process_missing"], s["id"])
        self.assertIn("launch", checks(res))
        self.assertIn("process", checks(res))
        self.assertIn("foreground", checks(res))
        self.assertIn("cannot count frames", " ".join(v["message"] for v in res["violations"] if v["check"] == "rest-frames"))

    def test_launch_error_detected_by_does_not_exist_alone(self):
        self.assertEqual(u.parse_launch_error("Activity class {x/y} does not exist.\n"), "Activity class {x/y} does not exist.")
        self.assertIsNone(u.parse_launch_error(AM_OK))
        self.assertEqual(u.parse_launch_error(None), "the launch command left no output")

    def test_empty_pid_at_one_snapshot_is_a_violation(self):
        files = healthy_files()
        files["pid-idle2.txt"] = "\n"
        cap = Capture(self.root, files)
        cap.analyze()
        res = cap.gate()
        self.assertIn({"check": "process", "step": "idle2",
                       "message": "the app's process was not running at the end of 'idle2'"}, res["violations"])

    def test_missing_pid_file_is_a_violation(self):
        files = healthy_files()
        files["pid-open.txt"] = None
        cap = Capture(self.root, files)
        cap.analyze()
        self.assertIn({"check": "process", "step": "open",
                       "message": "no process id was recorded at the end of 'open'"}, cap.gate()["violations"])


class MissingSnapshotTest(Base):
    """Finding 3: a missing snapshot is unknown, never zero; no step is skipped."""

    def test_middle_compositor_dump_empty(self):
        files = healthy_files()
        files["sf-open.txt"] = ""
        cap = Capture(self.root, files)
        st = steps_by_id(cap.analyze())
        self.assertIsNone(st["open"]["frames_layers"])
        self.assertIsNone(st["open"]["frames_display"])
        # the step after it has no baseline either: the totals since the clear are not charged to it
        self.assertIsNone(st["idle2"]["frames_layers"])
        self.assertIsNone(st["idle2"]["frames_display"])

    def test_gfx_total_missing_in_one_dump(self):
        files = healthy_files()
        files["gfx-open.txt"] = f"** Graphics info for pid {PID} [{PKG}] **\n"
        st = steps_by_id(Capture(self.root, files).analyze())
        self.assertIsNone(st["open"]["counters"]["total_frames"])
        self.assertIsNone(st["idle2"]["counters"]["total_frames"])     # not 42 - 0

    def test_missing_gfx_file_keeps_the_step(self):
        files = healthy_files()
        files["gfx-open.txt"] = None
        cap = Capture(self.root, files)
        run = cap.analyze()
        st = steps_by_id(run)
        self.assertEqual([s["id"] for s in run["steps"]], ORDER)
        self.assertIn("frame stats", st["open"]["snapshot_missing"])
        self.assertIsNone(st["open"]["frames"])
        self.assertIsNone(st["idle2"]["frames"])     # its baseline is missing: not charged with open's frames

    def test_launch_is_measured_from_zero(self):
        st = steps_by_id(Capture(self.root).analyze())
        self.assertEqual((st["launch"]["frames"], st["launch"]["frames_layers"], st["launch"]["frames_display"]), (30, 30, 100))

    def test_a_missing_baseline_does_not_fail_a_rest_window(self):
        files = healthy_files()
        files["sf-open.txt"] = ""
        files["gfx-open.txt"] = None
        cap = Capture(self.root, files)
        cap.analyze()
        res = cap.gate()
        self.assertFalse([v for v in res["violations"] if v["check"] == "rest-frames" and v.get("observed")], res)


class ExpectLargestCountTest(Base):
    """Finding 4: expectations use the largest available count and name its source."""

    def test_frames_max_uses_the_largest(self):
        files = healthy_files()
        files["sf-open.txt"] = sf_text(SF_TOTAL["open"], SF_APP["idle"] + 50)
        scn = json.loads(json.dumps(SCN))
        scn["steps"][1]["expect"] = {"frames_max": 20}
        cap = Capture(self.root, files, scn=scn)
        cap.analyze()
        res = cap.gate()
        v = [v for v in res["violations"] if v["check"] == "frames"]
        self.assertEqual(len(v), 1, res)
        self.assertEqual((v[0]["observed"], v[0]["source"]), (50, "compositor-stats"))
        self.assertIn("compositor-stats count", v[0]["message"])

    def test_gate_run_directly(self):
        run = {"device": {"clk_tck": 100}, "steps": [
            {"id": "launch", "kind": "launch"},
            {"id": "open", "kind": "tap", "frames": 8, "frames_layers": 50}]}
        scn = {"name": "x", "package": PKG, "steps": [{"id": "open", "do": "tap", "x": 0.5, "y": 0.5,
                                                         "expect": {"frames_max": 10}}]}
        res = u.gate_run(run, scn, dict(u.GATE_DEFAULTS))
        self.assertEqual(checks(res), ["frames"])
        self.assertEqual(res["violations"][0]["observed"], 50)


class GateWalksScenarioTest(Base):
    """Finding 5: gate checks the scenario's steps; a step with no record is a violation; an
    override scenario is validated."""

    def test_override_with_an_extra_step(self):
        cap = Capture(self.root)
        cap.analyze()
        scn = json.loads(json.dumps(SCN))
        scn["steps"].append({"id": "extra", "do": "tap", "x": 0.5, "y": 0.5, "expect": {"frames": 0, "signature": "T"}})
        res = cap.gate(scn)
        self.assertEqual(res["verdict"], "FAIL")
        self.assertIn({"check": "missing-step", "step": "extra", "message": "the run holds no record of step 'extra'"},
                      res["violations"])

    def test_override_is_validated(self):
        cap = Capture(self.root)
        cap.analyze()
        bad = {**SCN, "gate": {"rest_frame_max": 5}}
        with self.assertRaises(u.ProbeError):
            cap.gate(bad)

    def test_a_kind_mismatch_is_a_violation(self):
        cap = Capture(self.root)
        cap.analyze()
        scn = json.loads(json.dumps(SCN))
        scn["steps"][1] = {"id": "open", "do": "rest", "ms": 1000}
        res = cap.gate(scn)
        self.assertIn("scenario", checks(res))


class CapturesNotAnalysesTest(Base):
    """Finding 6: gate, summary and compare enumerate captures; an unanalysed or stale one is never
    passed over."""

    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = u.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_capture_without_run_json(self):
        good = Capture(self.root, name="run-01")
        good.analyze()
        Capture(self.root, name="run-02")            # never analysed
        out = u.gate([self.root], {})
        self.assertEqual([r["verdict"] for r in out["runs"]], ["PASS", "FAIL"])
        self.assertEqual(out["runs"][1]["violations"][0]["check"], "analysis")
        self.assertIn("not analysed", out["runs"][1]["violations"][0]["message"])
        self.assertEqual(out["verdict"], "FAIL")
        rc, _, err = self.main("summary", self.root)
        self.assertEqual(rc, 2)
        self.assertIn("not analysed", err)
        rc, _, err = self.main("compare", "--base", self.root, "--head", self.root)
        self.assertEqual(rc, 2)

    def test_stale_run_json(self):
        cap = Capture(self.root)
        cap.analyze()
        old = (cap.dir / "capture.json").stat().st_mtime_ns - 10 ** 9
        os.utime(cap.dir / "run.json", ns=(old, old))
        out = u.gate([self.root], {})
        self.assertEqual(out["verdict"], "FAIL")
        self.assertIn("older than capture.json", out["runs"][0]["violations"][0]["message"])
        self.assertEqual(self.main("summary", self.root)[0], 2)

    def test_failed_reanalysis_removes_the_old_result(self):
        cap = Capture(self.root)
        cap.analyze()
        self.assertTrue((cap.dir / "run.json").exists())
        (cap.dir / "raw" / "marks.txt").unlink()
        with self.assertRaises(u.ProbeError):
            cap.analyze()
        self.assertFalse((cap.dir / "run.json").exists())
        self.assertEqual(u.gate([self.root], {})["runs"][0]["violations"][0]["check"], "analysis")

    def test_run_json_without_capture(self):
        d = Path(self.root) / "orphan"
        d.mkdir()
        (d / "run.json").write_text("{}")
        self.assertEqual(u.gate([self.root], {})["verdict"], "FAIL")
        self.assertEqual(self.main("summary", self.root)[0], 2)


class RecordingAlignmentTest(Base):
    """Finding 7: a recording that cannot be placed in the step windows does not decide a rest
    window or a signature."""

    def run_with(self, clock, mismatch=False, frames=0, identical=2, signature="T"):
        steps = [{"id": "launch", "kind": "launch"},
                 {"id": "idle", "kind": "rest", "start_s": 10.0, "end_s": 12.0, "frames": frames,
                  "cpu_ticks_total": 0, "cpu_window_s": 2.0},
                 {"id": "open", "kind": "tap", "frames": 3}]
        video = {"clock": clock, "steps": [{"id": "idle", "identical_frames": identical, "app_region_frames": 0,
                                            "app_changed_frames_real": 0, "signature": "-"},
                                           {"id": "open", "identical_frames": 0, "app_region_frames": 3,
                                            "signature": signature}]}
        return {"device": {"clk_tck": 100}, "steps": steps, "video": video, "clock_mismatch_suspected": mismatch}

    SCN = {"name": "x", "package": PKG, "steps": [{"id": "idle", "do": "rest", "ms": 2000},
                                                  {"id": "open", "do": "tap", "x": 0.5, "y": 0.5,
                                                   "expect": {"signature": "T"}}]}

    def test_aligned_recording_gates(self):
        res = u.gate_run(self.run_with("recorder-timestamps"), self.SCN, dict(u.GATE_DEFAULTS))
        self.assertEqual(checks(res), ["rest-video"])

    def test_fallback_clock_makes_rest_video_a_note_and_signature_a_violation(self):
        for clock, mismatch in (("start-mark-fallback", False), ("matched-to-platform-frames", False),
                                ("recorder-timestamps", True)):
            res = u.gate_run(self.run_with(clock, mismatch), self.SCN, dict(u.GATE_DEFAULTS))
            self.assertEqual(checks(res), ["signature"], (clock, res))
            self.assertIn("could not be aligned", res["violations"][0]["message"])
            self.assertTrue(any("composed at rest" in n and "not a violation" in n for n in res["notes"]), res["notes"])

    def test_frame_counts_still_gate_when_unaligned(self):
        res = u.gate_run(self.run_with("start-mark-fallback", frames=4), self.SCN, dict(u.GATE_DEFAULTS))
        self.assertIn("rest-frames", checks(res))

    def test_no_count_and_unaligned_recording_cannot_observe(self):
        res = u.gate_run(self.run_with("start-mark-fallback", frames=None), self.SCN, dict(u.GATE_DEFAULTS))
        msgs = [v["message"] for v in res["violations"] if v["check"] == "rest-frames"]
        self.assertEqual(len(msgs), 1)
        self.assertIn("cannot count frames", msgs[0])

    def test_missing_t0_fails_loudly(self):
        from PIL import Image
        frames_dir = Path(self.root) / "pngs"
        frames_dir.mkdir()
        files = []
        for i in range(3):
            f = frames_dir / f"f{i:05d}.png"
            Image.new("L", (27, 60), color=i * 40).save(f)
            files.append(f)
        video = Path(self.root) / "screen.mp4"
        video.write_bytes(b"no recorder metadata")
        with mock.patch.object(ui_video, "probe_pts", return_value=[0.0, 0.1, 0.2]), \
                mock.patch.object(ui_video, "extract", return_value=files):
            with self.assertRaises(ui_video.VideoError) as ctx:
                ui_video.analyze(video, Path(self.root), [], {"launch.start": 1.0}, SCN,
                                 {"height": 2400, "stable_insets_top_bottom": None})
        self.assertIn("T0", str(ctx.exception))


class CpuWindowTest(Base):
    """Findings 9 and 10: a step's CPU is its first reading against the previous step's last one,
    over the time between their stamps, from the process total."""

    def test_window_and_ticks(self):
        st = steps_by_id(Capture(self.root).analyze())
        # idle: 53 (its first reading) - 53 (launch's last reading) over 112.1 - 109.4
        self.assertEqual((st["idle"]["cpu_ticks_total"], st["idle"]["cpu_window_s"]), (0, 2.7))
        # open: 80 - 54 over 115.1 - 112.4
        self.assertEqual((st["open"]["cpu_ticks_total"], st["open"]["cpu_window_s"]), (26, 2.7))
        # launch is measured from zero (a fresh process) from the launch mark
        self.assertEqual((st["launch"]["cpu_ticks_total"], st["launch"]["cpu_window_s"]), (50, 7.5))

    def test_the_probe_dumps_are_not_charged_to_a_rest_step(self):
        # 30 ticks spent between a snapshot's two readings (the dumps) appear in no step
        files = healthy_files()
        files["cpu-open.after.txt"] = cpu_text(115.5, 110, {str(PID): (PKG, 100), "4330": ("RenderThread", 10)})
        files["cpu-idle2.txt"] = cpu_text(117.7, 110, {str(PID): (PKG, 100), "4330": ("RenderThread", 10)})
        st = steps_by_id(Capture(self.root, files).analyze())
        self.assertEqual(st["idle2"]["cpu_ticks_total"], 0)

    def test_percentage_uses_the_stamp_window(self):
        run = {"device": {"clk_tck": 100}, "steps": [
            {"id": "launch", "kind": "launch"},
            {"id": "idle", "kind": "rest", "start_s": 10.0, "end_s": 12.0, "frames": 0,
             "cpu_ticks_total": 5, "cpu_window_s": 2.5}]}
        scn = {"name": "x", "package": PKG, "steps": [{"id": "idle", "do": "rest", "ms": 2000}]}
        # 5 ticks = 50 ms over 2.5 s = 2.0 %: allowed. Over the 2.0 s mark window it would be 2.5 %.
        self.assertEqual(u.gate_run(run, scn, dict(u.GATE_DEFAULTS))["verdict"], "PASS")
        run["steps"][1]["cpu_window_s"] = None
        self.assertIn("idle: CPU at rest not available", u.gate_run(run, scn, dict(u.GATE_DEFAULTS))["notes"])

    def test_an_exited_thread_is_counted(self):
        # idle2: a worker burned 7 ticks and exited before the reading; only the process line has them
        files = healthy_files()
        files["cpu-idle2.txt"] = cpu_text(117.7, 89, {str(PID): (PKG, 72), "4330": ("RenderThread", 10)})
        st = steps_by_id(Capture(self.root, files).analyze())
        self.assertEqual(st["idle2"]["cpu_ticks_total"], 7)
        cap = Capture(self.root, files, name="run-02")
        cap.analyze()
        res = cap.gate()
        # 7 ticks = 70 ms over 117.7 - 115.5 = 2.2 s = 3.18 %
        self.assertEqual([(v["check"], v["step"], v["observed"]) for v in res["violations"]], [("rest-cpu", "idle2", 3.18)])

    def test_parse_cpu(self):
        text = cpu_text(12.5, 40, {"4321": (PKG, 30), "4330": ("RenderThread", 5)})
        self.assertEqual(u.parse_cpu(text), {"uptime": 12.5, "process": 40,
                                             "threads": {"4321": (PKG, 30), "4330": ("RenderThread", 5)}})
        self.assertEqual(u.parse_cpu("uptime 3.0\n"), {"uptime": 3.0, "process": None, "threads": {}})


class QuietCauseTest(Base):
    """Finding 11: a quiet timeout says which count kept changing."""

    def gate_with(self, cause):
        run = {"device": {"clk_tck": 100}, "steps": [
            {"id": "launch", "kind": "launch"},
            {"id": "open", "kind": "tap", "frames": 3, "quiet": "timeout", "quiet_timeout_cause": cause}]}
        scn = {"name": "x", "package": PKG, "steps": [{"id": "open", "do": "tap", "x": 0.5, "y": 0.5}]}
        return u.gate_run(run, scn, dict(u.GATE_DEFAULTS))["violations"]

    def test_messages(self):
        self.assertIn("the app kept drawing", self.gate_with("app")[0]["message"])
        msg = self.gate_with("display")[0]["message"]
        self.assertIn("the display kept changing while the app was idle (a system animation, the keyboard, another window)", msg)
        self.assertNotIn("the app kept drawing", msg)
        self.assertIn("whether the app drew is unknown", self.gate_with("unattributed")[0]["message"])
        for cause in ("app", "display", "unattributed", None):
            v = self.gate_with(cause)
            self.assertEqual((v[0]["check"], v[0]["cause"]), ("quiet", cause))

    def test_analysis_reads_the_cause_mark(self):
        files = healthy_files()
        files["marks.txt"] += "open.quiet_timeout_display 114.9\nopen.quiet_timeout 114.9\n"
        st = steps_by_id(Capture(self.root, files).analyze())
        self.assertEqual((st["open"]["quiet"], st["open"]["quiet_timeout_cause"]), ("timeout", "display"))

    def test_script_marks_the_cause(self):
        scn = {"name": "x", "package": PKG, "until_quiet": True, "steps": [{"id": "open", "do": "tap", "x": 0.5, "y": 0.5}]}
        text = u.build_device_script(scn, 1080, 2400, False, 60)
        self.assertIn("      elif [ $(( (t - ca) * 10 )) -lt $2 ]; then mark $1.quiet_timeout_app", text)
        self.assertIn("      else mark $1.quiet_timeout_display; fi", text)
        self.assertIn('      if [ -z "$fa" ]; then mark $1.quiet_timeout_unattributed', text)


CLEAN_STATS = {"packets": 1, "chunks_overwritten": 0, "chunks_discarded": 0, "patches_failed": 0,
               "writer_packet_loss": 0, "flushes_failed": 0, "final_flush": 1}


def fake_trace(first_ts, last_ts, surface=(), truncated=False, **stats):
    return {"packets": 10, "surface_frames": list(surface), "display_frames": [], "processes": {}, "network": {},
            "unterminated": 0, "first_ts": first_ts, "last_ts": last_ts, "truncated": truncated,
            "stats": {**CLEAN_STATS, **stats}}


def frame(ts):
    return {"layer": f"{PKG}/{PKG}.Main#1", "is_buffer": True, "pid": PID, "start": int(ts * 1e9),
            "present": "On-time", "jank": ["None"]}


class TraceCoverageAndSectionsTest(Base):
    """Findings 13 and 14: sections the app never emitted are unknown, and steps the trace does not
    cover have no trace counts."""

    def analyse(self, trace, begins, scn=SCN):
        cap = Capture(self.root, scn=scn, trace=True, sections=True)
        (cap.dir / "trace.pftrace").write_bytes(b"")
        with mock.patch.object(pftrace, "read_trace", return_value=trace), \
                mock.patch.object(pftrace, "read_atrace", return_value=begins):
            run = cap.analyze()
        return cap, run

    def test_section_never_emitted_is_not_observed(self):
        scn = {**SCN, "count_sections": ["Custom#section"]}
        scn["steps"] = [dict(s) for s in SCN["steps"]]
        scn["steps"][2]["expect"] = {"sections_max": {"Custom#section": 0}}
        begins = [{"ts": int(113e9), "tid": PID, "tgid": PID, "name": "Choreographer#doFrame 1", "dur": 5}]
        cap, run = self.analyse(fake_trace(int(99e9), int(119e9), [frame(113.0)]), begins, scn)
        st = steps_by_id(run)
        self.assertIn("Custom#section", run["sections_not_observed"])
        self.assertIsNone(st["idle2"]["sections"]["Custom#section"])
        self.assertEqual(st["open"]["sections"]["Choreographer#doFrame"], 1)
        self.assertEqual(st["idle2"]["sections"]["Choreographer#doFrame"], 0)
        res = cap.gate()
        v = [v for v in res["violations"] if v["check"] == "sections"]
        self.assertEqual(len(v), 1)
        self.assertIn("never appeared", v[0]["message"])
        self.assertIn("may not emit it", v[0]["message"])

    def test_steps_outside_the_trace_span(self):
        # the trace holds 111.0 .. 116.0 s: launch, idle (110-112) and idle2 (115.6-117.6) are not covered
        cap, run = self.analyse(fake_trace(int(111e9), int(116e9), [frame(113.0)]), [])
        st = steps_by_id(run)
        self.assertEqual(run["trace_span_s"], [111.0, 116.0])
        self.assertEqual(run["trace_uncovered_steps"], ["launch", "idle", "idle2"])
        self.assertTrue(st["open"]["trace_covered"])
        self.assertEqual(st["open"]["frames_compositor"], 1)
        for sid in ("launch", "idle", "idle2"):
            self.assertIsNone(st[sid]["frames_compositor"], sid)
            self.assertIsNone(st[sid]["sections_all"], sid)
        res = cap.gate()
        self.assertIn("idle: the trace did not cover this step; its trace counts are unavailable", res["notes"])

    def test_requested_trace_missing(self):
        cap = Capture(self.root, trace=True)
        run = cap.analyze()
        self.assertTrue(run["trace_missing"])
        self.assertIn("trace", checks(cap.gate()))


class RecordingWitnessTest(Base):
    """Finding 15 (and the zero-frame half of 17): a missing or broken recording is reported."""

    def test_missing_recording(self):
        files = healthy_files()
        files["sr-exit.txt"] = "0\n"
        files["sr-alive.txt"] = "alive\n"
        cap = Capture(self.root, files, video=True)
        run = cap.analyze()
        self.assertTrue(run["video_missing"])
        res = cap.gate()
        self.assertEqual([(v["check"], v["message"]) for v in res["violations"]],
                         [("recording", "a recording was requested but none was saved; the recording checks could not run")])

    def test_recorder_exit_status(self):
        files = healthy_files()
        files["sr-exit.txt"] = "1\n"
        files["sr-alive.txt"] = "alive\n"
        files["screen.mp4"] = "x"
        block = {"frames": 3, "clock": "recorder-timestamps", "steps": [
            {"id": sid, "identical_frames": 0, "app_region_frames": 0, "app_changed_frames_real": 0, "signature": "-"}
            for sid in ORDER]}
        cap = Capture(self.root, files, video=True)
        with mock.patch.object(ui_video, "analyze", return_value=block):
            run = cap.analyze()
        self.assertEqual(run["video_recorder_exit"], 1)
        res = cap.gate()
        self.assertEqual(checks(res), ["recording"])
        self.assertIn("exited with status 1", res["violations"][0]["message"])
        files["sr-exit.txt"] = None
        cap2 = Capture(self.root, files, video=True, name="run-02")
        with mock.patch.object(ui_video, "analyze", return_value=block):
            cap2.analyze()
        self.assertIn("exit status was not recorded", cap2.gate()["violations"][0]["message"])

    def test_zero_frame_recording(self):
        files = healthy_files()
        files["sr-exit.txt"] = "0\n"
        files["screen.mp4"] = "x"
        cap = Capture(self.root, files, video=True)
        with mock.patch.object(ui_video, "probe_pts", return_value=[]), mock.patch.object(ui_video, "extract", return_value=[]):
            run = cap.analyze()
        self.assertEqual(run["video_error"], "the recording holds no video frames")
        self.assertIn("the recording could not be used", cap.gate()["violations"][0]["message"])

    def test_ffmpeg_failure_is_a_probe_error(self):
        """Finding 16: an ffmpeg failure during analysis is the tool's own error, not a crash."""
        import subprocess
        files = healthy_files()
        files["sr-exit.txt"] = "0\n"
        files["screen.mp4"] = "x"
        cap = Capture(self.root, files, video=True)
        err = subprocess.CalledProcessError(8, ["ffmpeg"], output="", stderr="Unrecognized option 'fps_mode'.\n")
        with mock.patch.object(ui_video.subprocess, "run", side_effect=err):
            with self.assertRaises(u.ProbeError) as ctx:
                cap.analyze()
        self.assertIn("Unrecognized option 'fps_mode'", str(ctx.exception))


class AmbiguousPidTest(Base):
    """Finding 17: a pid file holding two pids does not crash the analysis."""

    def test_two_pids(self):
        files = healthy_files()
        files["pid-idle2.txt"] = f"{PID} 5555\n"
        cap = Capture(self.root, files, trace=True)
        (cap.dir / "trace.pftrace").write_bytes(b"")
        with mock.patch.object(pftrace, "read_trace", return_value=fake_trace(int(99e9), int(119e9))):
            run = cap.analyze()
        st = steps_by_id(run)
        self.assertTrue(st["idle2"]["process_ambiguous"])
        self.assertIsNone(st["idle2"]["cpu_ticks_total"])
        self.assertIn("more than one process", " ".join(v["message"] for v in cap.gate()["violations"]))

    def test_truncated_trace_is_read_up_to_the_cut(self):
        cap = Capture(self.root, trace=True)
        # a single packet whose declared length runs past the end of the file
        (cap.dir / "trace.pftrace").write_bytes(b"\x0a\x20" + b"\x40\x05")
        run = cap.analyze()
        self.assertTrue(run["trace_truncated"])
        self.assertIsNone(run["trace_span_s"])
        self.assertIn("ends in a partial packet", " ".join(cap.gate()["notes"]))


if __name__ == "__main__":
    unittest.main()
