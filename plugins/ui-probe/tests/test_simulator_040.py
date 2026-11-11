"""0.4.0: the iOS Simulator, rest steps only. `xcrun` and `ps` are stand-ins on PATH (a recorder that
answers SIGINT, ignores it, never starts or dies at once); no simulator, no device is touched. One
test runs the real recording analysis on a short video made with ffmpeg, when ffmpeg is present."""
import sys

sys.dont_write_bytecode = True

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import ui_probe as u  # noqa: E402
import ui_video  # noqa: E402

BUNDLE = "com.example-co.app"
UDID = "11111111-2222-3333-4444-555555555555"
PID = 4242
SCN = {"name": "Sim rest", "package": BUNDLE, "launch_settle_ms": 0,
       "steps": [{"id": "rest-a", "do": "rest", "ms": 100}, {"id": "rest-b", "do": "rest", "ms": 100}]}

XCRUN = f"""#!{sys.executable} -B
import json, os, signal, sys, time
S = os.environ["SIMSTATE"]
def read(name, default=""):
    p = os.path.join(S, name)
    return open(p).read() if os.path.exists(p) else default
with open(os.path.join(S, "xcrun.log"), "a") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
a = sys.argv[1:]
if a[:1] != ["simctl"]:
    sys.exit(64)
cmd = a[1] if len(a) > 1 else ""
if cmd == "help":
    print("usage: simctl"); sys.exit(0)
if cmd == "list":
    print(read("devices.json", '{{"devices": {{}}}}')); sys.exit(0)
if cmd == "get_app_container":
    if os.path.exists(os.path.join(S, "installed")):
        print("/containers/app"); sys.exit(0)
    sys.stderr.write("No such app\\n"); sys.exit(2)
if cmd == "terminate":
    sys.exit(3)
if cmd == "status_bar":
    if a[3] == "override":
        sys.exit(int(read("statusbar.rc", "0")))
    sys.exit(0)
if cmd == "launch":
    out = read("launch.out", f"{{a[3]}}: {PID}\\n")
    sys.stdout.write(out); sys.exit(int(read("launch.rc", "0")))
if cmd == "io" and a[3] == "recordVideo":
    path = a[-1]
    with open(os.path.join(S, "recorder-pids"), "a") as f:
        f.write(f"{{os.getpid()}}\\n")
    mode = read("rec.mode", "ok").strip()
    if mode == "die":
        sys.stderr.write("Error: no display\\n"); sys.exit(1)
    def write():
        # the file names the recording it is, so a stand-in analysis can tell one step's from another's
        open(path, "w").write("video " + path)
    def stop(*_):
        write()
        sys.exit(int(read("rec.rc", "0")))
    signal.signal(signal.SIGINT, signal.SIG_IGN if mode == "deaf" else stop)
    if mode == "deaf":
        import subprocess
        child = subprocess.Popen([sys.executable, "-c", "import signal, time; signal.signal(signal.SIGINT, "
                                  "signal.SIG_IGN); time.sleep(300)"])
        with open(os.path.join(S, "grandchild-pids"), "a") as f:
            f.write(f"{{child.pid}}\\n")
    if mode != "silent":
        sys.stderr.write("Recording started\\n"); sys.stderr.flush()
    if mode == "early":
        time.sleep(0.02); write(); sys.exit(0)
    while True:
        time.sleep(0.05)
sys.exit(65)
"""

PS = f"""#!{sys.executable} -B
import os, sys
S = os.environ["SIMSTATE"]
with open(os.path.join(S, "ps.log"), "a") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
seq = os.path.join(S, "cpu.txt")
lines = open(seq).read().splitlines() if os.path.exists(seq) else []
n = int(open(os.path.join(S, "cpu.n")).read()) if os.path.exists(os.path.join(S, "cpu.n")) else 0
open(os.path.join(S, "cpu.n"), "w").write(str(n + 1))
value = lines[n] if n < len(lines) else (lines[-1] if lines else "0:00.00")
if value == "GONE":
    sys.exit(1)
print("  " + value)
"""


def devices(state="Booted", udid=UDID):
    return json.dumps({"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-26-1": [
        {"udid": udid, "name": "Test Phone", "state": state, "isAvailable": True}]}})


def block(sid, frames=0, identical=0, region=0, real=None):
    return {"frames": frames + 1, "clock": "start-mark-fallback", "steps": [
        {"id": sid, "video_frames": frames, "identical_frames": identical, "app_region_frames": region,
         "app_changed_frames_real": region if real is None else real, "signature": "-"}]}


def main(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = u.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


class SimBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        b = self.root / "bin"
        b.mkdir()
        for name, text in (("xcrun", XCRUN), ("ps", PS)):
            (b / name).write_text(text)
            (b / name).chmod(0o755)
        (self.state / "devices.json").write_text(devices())
        (self.state / "installed").write_text("")
        self.env = mock.patch.dict(os.environ, {"SIMSTATE": str(self.state), "PATH": f"{b}:{os.environ['PATH']}"})
        self.env.start()
        self.patches = [mock.patch.object(u, "SIM_TERMINATE_WAIT_S", 0), mock.patch.object(u, "SIM_REST_MIN_MS", 100),
                        mock.patch.object(u, "SIM_RECORD_START_TIMEOUT_S", 3),
                        mock.patch.object(u, "SIM_RECORD_STOP_TIMEOUT_S", 3)]
        for p in self.patches:
            p.start()
        self.video = {}          # step id -> the block ui_video.analyze returns for it
        self.vpatch = mock.patch.object(ui_video, "analyze", self.fake_analyze)
        self.vpatch.start()

    def tearDown(self):
        self.vpatch.stop()
        for p in self.patches:
            p.stop()
        self.env.stop()
        for pid in self.recorder_pids() + self.recorder_pids("grandchild-pids"):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, 9)
        self.tmp.cleanup()

    def fake_analyze(self, video, out, rows, marks, scn, facts):
        """The counts prepared for the step whose recording this is, read from the file itself."""
        sid = scn["steps"][0]["id"]
        named = Path(video).read_text().split("rec-", 1)[1].rsplit(".mp4", 1)[0]
        b = json.loads(json.dumps(self.video.get(named, block(named))))
        b["steps"][0]["id"] = sid
        return b

    def recorder_pids(self, name="recorder-pids"):
        p = self.state / name
        return [int(x) for x in p.read_text().split()] if p.exists() else []

    def alive(self, pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True

    def log(self, name="xcrun.log"):
        p = self.state / name
        return p.read_text().splitlines() if p.exists() else []

    def scenario(self, scn=None):
        path = self.root / "scenario.json"
        path.write_text(json.dumps(scn or SCN))
        return str(path)

    def run_cli(self, *extra, scn=None, out="out"):
        return main("run", "--simulator", UDID, "--scenario", self.scenario(scn), "--out", str(self.root / out), *extra)

    def gate(self, *extra, out="out"):
        return main("gate", str(self.root / out), *extra)


# --------------------------------------------------------------------------------------------
# the scenario
# --------------------------------------------------------------------------------------------

class SimScenarioTest(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(u, "SIM_REST_MIN_MS", 100)
        patch.start()
        self.addCleanup(patch.stop)

    def reject(self, scn, fragment):
        with self.assertRaises(u.ProbeError) as cm:
            u.validate_scenario(scn, u.SIM_PLATFORM)
        self.assertIn(fragment, str(cm.exception))

    def test_valid(self):
        u.validate_scenario(SCN, u.SIM_PLATFORM)
        u.validate_scenario({**SCN, "until_quiet": False, "gate": {"rest_cpu_pct_max": 3},
                             "steps": [{"id": "r", "do": "rest", "ms": 5000,
                                        "allow": {"cpu_pct_max": 4, "frames_max": 12, "reason": "caret"}}]}, u.SIM_PLATFORM)

    def test_only_rest_steps(self):
        for st in ({"id": "t", "do": "tap", "x": 0.5, "y": 0.5}, {"id": "k", "do": "key", "code": 4},
                   {"id": "s", "do": "swipe", "from": [0, 0], "to": [1, 1]}, {"id": "x", "do": "text", "text": "a"},
                   {"id": "f", "do": "tap", "find": {"text": "Go"}}):
            self.reject({**SCN, "steps": [st]}, f"step '{st['id']}': the iOS Simulator has no input in this version, "
                                                f"so only rest steps run; a '{st['do']}' step is refused")

    def test_refused_keys(self):
        self.reject({**SCN, "activity": ".Main"}, "'activity': the iOS Simulator launches the app by its bundle identifier")
        self.reject({**SCN, "until_quiet": True}, "'until_quiet' must be false or absent")
        self.reject({**SCN, "quiet": {"ms": 500}}, "'quiet': there is no quiet detection on the iOS Simulator")
        self.reject({**SCN, "count_sections": ["x"]}, "'count_sections': trace sections are not recorded")
        for key, value in (("meminfo", True), ("background", True), ("expect", {"frames": 0})):
            self.reject({**SCN, "steps": [{"id": "r", "do": "rest", "ms": 100, key: value}]}, f"step 'r': '{key}':")

    def test_a_bundle_identifier(self):
        u.validate_scenario({**SCN, "package": "com.my-company.app2"}, u.SIM_PLATFORM)
        with self.assertRaises(u.ProbeError):
            u.validate_scenario({**SCN, "package": "com.my-company.app2"})          # not an Android id
        for bad in ("nodots", "com.ex ample", "com.x;rm", "com..x"):
            self.reject({**SCN, "package": bad}, "'package' must be a bundle identifier")

    def test_no_recorder_limit_on_the_whole_scenario(self):
        long = {**SCN, "steps": [{"id": f"r{n}", "do": "rest", "ms": 60000} for n in range(5)]}
        u.validate_scenario(long, u.SIM_PLATFORM)
        with self.assertRaises(u.ProbeError):
            u.validate_scenario({**long, "package": "com.example.app"})


class SimRestMinimumTest(unittest.TestCase):
    def test_a_rest_lasts_at_least_a_second(self):
        self.assertEqual(u.SIM_REST_MIN_MS, 1000)
        for ms in (999, 100, 0.5):
            with self.assertRaises(u.ProbeError) as cm:
                u.validate_scenario({**SCN, "steps": [{"id": "r", "do": "rest", "ms": ms}]}, u.SIM_PLATFORM)
            self.assertIn("step 'r': a rest on the iOS Simulator lasts at least 1000 ms", str(cm.exception))
        u.validate_scenario({**SCN, "steps": [{"id": "r", "do": "rest", "ms": 1000}]}, u.SIM_PLATFORM)
        u.validate_scenario({**SCN, "package": "com.example.app", "steps": [{"id": "r", "do": "rest", "ms": 100}]})


class ParseCputimeTest(unittest.TestCase):
    def test_formats(self):
        for text, secs in (("0:00.03", 0.03), ("  1:02.50 ", 62.5), ("1:02:03.45", 3723.45), ("2-01:00:00", 176400.0),
                           ("0.07", 0.07)):
            self.assertEqual(u.parse_cputime(text), secs, text)
        for bad in ("", None, "abc", "1:x", "a-01:00", "-1:00", "1:2:3:4"):
            self.assertIsNone(u.parse_cputime(bad), bad)


# --------------------------------------------------------------------------------------------
# a run, with stand-ins for xcrun and ps
# --------------------------------------------------------------------------------------------

class SimRunTest(SimBase):
    def test_a_clean_run(self):
        (self.state / "cpu.txt").write_text("0:01.00\n0:01.00\n0:01.00\n0:01.00\n0:01.00\n")
        rc, out, err = self.run_cli()
        self.assertEqual(rc, 0, err)
        run = self.root / "out" / "run-01"
        for name in ("launch.txt", "pid-launch.txt", "rec-rest-a.mp4", "rec-rest-a.log", "step-rest-a.json",
                     "pid-rest-a.txt", "rec-rest-b.mp4", "step-rest-b.json"):
            self.assertTrue((run / "raw" / name).exists(), name)
        self.assertEqual(json.loads((run / "capture.json").read_text())["platform"], u.SIM_PLATFORM)
        log = self.log()
        self.assertEqual(log[2:5], [f"simctl status_bar {UDID} override --time 9:41", f"simctl terminate {UDID} {BUNDLE}",
                                    f"simctl launch {UDID} {BUNDLE}"])
        self.assertEqual(log[-1], f"simctl status_bar {UDID} clear")
        rec = [l for l in log if "recordVideo" in l]
        self.assertEqual(rec[0], f"simctl io {UDID} recordVideo --codec h264 --force {run / 'raw' / 'rec-rest-a.mp4'}")
        self.assertEqual(len(rec), 2)
        self.assertTrue(all(l == f"-o cputime= -p {PID}" for l in self.log("ps.log")))
        step = json.loads((run / "raw" / "step-rest-a.json").read_text())
        self.assertEqual((step["pid"], step["cputime_before"], step["cputime_after"], step["recorder_exit"],
                          step["recorder_alive_at_end"]), (PID, "0:01.00", "0:01.00", 0, True))
        self.assertEqual(json.loads((run / "capture.json").read_text())["status_bar_override"], ["--time", "9:41"])
        self.assertGreaterEqual(step["window_s"], 0.09)
        res = json.loads((run / "run.json").read_text())
        st = {s["id"]: s for s in res["steps"]}
        self.assertEqual((st["rest-a"]["cpu_s"], st["rest-a"]["cpu_pct"], st["rest-a"]["recording"]["frames"]), (0.0, 0.0, 0))
        self.assertEqual(json.loads(out.splitlines()[0])["rest-a"], {"cpu_pct": 0.0, "frames": 0})
        self.assertFalse([p for p in self.recorder_pids() if self.alive(p)])
        rc, text, _ = self.gate()
        self.assertEqual(rc, 0, text)
        self.assertIn("note: iOS Simulator: not checked, since a simulator does not give them", text)

    def test_the_counts_reach_the_run(self):
        (self.state / "cpu.txt").write_text("0:01.00\n0:01.00\n0:01.05\n0:01.05\n0:01.05\n")
        self.video["rest-a"] = block("rest-a", frames=9, identical=5, region=3, real=2)
        self.video["rest-b"] = block("rest-b", frames=4, identical=0, region=4, real=4)
        rc, _, err = self.run_cli()
        self.assertEqual(rc, 0, err)
        st = {s["id"]: s for s in json.loads((self.root / "out" / "run-01" / "run.json").read_text())["steps"]}
        # each step is analysed from its own recording
        self.assertEqual((st["rest-b"]["recording"]["frames"], st["rest-b"]["recording"]["changed_app"]), (4, 4))
        self.assertEqual(st["rest-a"]["cpu_s"], 0.05)
        self.assertEqual(st["rest-a"]["cpu_pct"], round(100 * 0.05 / st["rest-a"]["window_s"], 2))
        self.assertGreater(st["rest-a"]["cpu_pct"], 10)
        self.assertEqual(st["rest-a"]["recording"], {"frames": 9, "changed_app": 2, "changed_app_noise": 1, "identical": 5,
                                                     "bars_only": 1, "signature": "-", "sheet": None})
        self.assertEqual(self.gate()[0], 1)

    def test_a_recorder_that_never_starts(self):
        (self.state / "rec.mode").write_text("silent")
        rc, _, err = self.run_cli()
        self.assertEqual(rc, 2)
        self.assertIn("did not report 'Recording started' within 3 s", err)
        failed = json.loads((self.root / "out" / "run-01" / "failed.json").read_text())
        self.assertIn("Recording started", failed["error"])
        self.assertFalse((self.root / "out" / "run-01" / "capture.json").exists())
        self.assertEqual(self.log()[-1], f"simctl status_bar {UDID} clear")
        self.assertTrue(self.recorder_pids())
        self.assertFalse([p for p in self.recorder_pids() if self.alive(p)])
        rc, text, _ = self.gate()
        self.assertEqual(rc, 1)
        self.assertIn("FAIL [run]", text)

    def test_a_recorder_that_dies(self):
        (self.state / "rec.mode").write_text("die")
        rc, _, err = self.run_cli()
        self.assertEqual(rc, 2)
        self.assertIn("the simulator's recorder exited (status 1) before it started recording: Error: no display", err)

    def test_a_recorder_deaf_to_sigint_is_killed(self):
        (self.state / "rec.mode").write_text("deaf")
        with mock.patch.object(u, "SIM_RECORD_STOP_TIMEOUT_S", 0.5):
            rc, _, err = self.run_cli(scn={**SCN, "steps": SCN["steps"][:1]})
        self.assertEqual(rc, 0, err)
        self.assertFalse([p for p in self.recorder_pids() if self.alive(p)])
        # what the recorder started behind it went with it: the whole group is signalled
        self.assertTrue(self.recorder_pids("grandchild-pids"))
        deadline = time.monotonic() + 3
        while any(self.alive(p) for p in self.recorder_pids("grandchild-pids")) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse([p for p in self.recorder_pids("grandchild-pids") if self.alive(p)])
        step = json.loads((self.root / "out" / "run-01" / "raw" / "step-rest-a.json").read_text())
        self.assertEqual((step["recorder_exit"], step["recorder_killed"]), (-9, True))
        rc, text, _ = self.gate()
        self.assertEqual(rc, 1)
        self.assertIn("FAIL [recording] rest-a: no recording was saved", text)

    def test_a_recorder_with_a_bad_exit_status(self):
        (self.state / "rec.rc").write_text("5")
        rc, _, err = self.run_cli(scn={**SCN, "steps": SCN["steps"][:1]})
        self.assertEqual(rc, 0, err)
        rc, text, _ = self.gate()
        self.assertEqual(rc, 1)
        self.assertIn("FAIL [recording] rest-a: the recorder exited with status 5", text)

    def test_an_interrupt_stops_the_recorder_and_leaves_failed_json(self):
        calls = []
        real = u.host_cputime

        def cpu(pid):
            calls.append(pid)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return real(pid)
        with mock.patch.object(u, "host_cputime", cpu):
            with self.assertRaises(KeyboardInterrupt):
                u.run_once_sim(UDID, SCN, self.root / "out" / "run-01")
        self.assertFalse([p for p in self.recorder_pids() if self.alive(p)])
        self.assertEqual(json.loads((self.root / "out" / "run-01" / "failed.json").read_text())["error"], "interrupted")
        self.assertEqual(self.log()[-1], f"simctl status_bar {UDID} clear")

    def test_a_process_gone_at_the_end_of_a_window(self):
        (self.state / "cpu.txt").write_text("0:01.00\n0:01.00\nGONE\nGONE\n")
        rc, _, err = self.run_cli()
        self.assertEqual(rc, 0, err)
        rc, text, _ = self.gate()
        self.assertEqual(rc, 1)
        self.assertIn("FAIL [process] rest-a: the app's process was not running at the end of 'rest-a'", text)
        self.assertIn("FAIL [rest-cpu] rest-a: CPU at rest could not be read", text)

    def test_the_launch_must_name_a_process(self):
        (self.state / "launch.out").write_text("An error was encountered processing the command\n")
        rc, _, err = self.run_cli()
        self.assertEqual(rc, 2)
        self.assertIn("`xcrun simctl launch` printed no process id", err)
        (self.state / "launch.rc").write_text("4")
        rc, _, err = self.run_cli(out="out2")
        self.assertEqual(rc, 2)
        self.assertIn(f"xcrun simctl launch {UDID} {BUNDLE} failed (exit 4)", err)

    def test_the_simulator_must_be_booted_and_hold_the_app(self):
        (self.state / "devices.json").write_text(devices(state="Shutdown"))
        rc, _, err = self.run_cli(out="o1")
        self.assertEqual(rc, 2)
        self.assertIn(f"simulator {UDID} is Shutdown; boot it first", err)
        (self.state / "devices.json").write_text(devices(udid="other"))
        self.assertIn(f"no simulator with udid {UDID}", self.run_cli(out="o2")[2])
        (self.state / "devices.json").write_text(devices())
        (self.state / "installed").unlink()
        self.assertIn(f"{BUNDLE} is not installed on simulator {UDID}", self.run_cli(out="o3")[2])
        self.assertFalse([l for l in self.log() if "recordVideo" in l or "launch" in l])

    def test_refused_before_anything_runs(self):
        rc, _, err = self.run_cli(scn={**SCN, "steps": [{"id": "t", "do": "tap", "x": 0.5, "y": 0.5}]})
        self.assertEqual(rc, 2)
        self.assertIn("the iOS Simulator has no input in this version", err)
        for flag in ("--trace", "--sections", "--no-video"):
            rc, _, err = self.run_cli(flag)
            self.assertEqual(rc, 2, flag)
            self.assertIn(f"{flag} cannot be used with --simulator", err)
        used = self.root / "used" / "run-01"
        (used / "raw").mkdir(parents=True)
        rc, _, err = self.run_cli(out="used")
        self.assertEqual(rc, 2)
        self.assertIn("already holds a run or what is left of one (raw)", err)
        self.assertEqual(self.log(), [])

    def test_exactly_one_target(self):
        for argv in (["run", "--scenario", self.scenario(), "--out", str(self.root / "x")],
                     ["run", "--serial", "s", "--simulator", UDID, "--scenario", self.scenario(), "--out", str(self.root / "x")]):
            with self.assertRaises(SystemExit) as cm, contextlib.redirect_stderr(io.StringIO()):
                u.main(argv)
            self.assertEqual(cm.exception.code, 2)


class SimRunRulesTest(SimBase):
    """The fix round: the status bar, an early recorder, negative CPU, signals, empty targets."""

    def test_the_status_bar_must_be_frozen(self):
        (self.state / "statusbar.rc").write_text("1")
        rc, _, err = self.run_cli()
        self.assertEqual(rc, 2)
        self.assertIn("the simulator's status bar could not be frozen", err)
        self.assertFalse([l for l in self.log() if l.startswith(("simctl launch", "simctl io"))])
        self.assertTrue((self.root / "out" / "run-01" / "failed.json").exists())

    def test_a_recorder_that_stops_early_fails_the_recording(self):
        (self.state / "rec.mode").write_text("early")
        rc, _, err = self.run_cli(scn={**SCN, "steps": SCN["steps"][:1]})
        self.assertEqual(rc, 0, err)
        step = json.loads((self.root / "out" / "run-01" / "raw" / "step-rest-a.json").read_text())
        self.assertEqual((step["recorder_exit"], step["recorder_alive_at_end"]), (0, False))
        rc, text, _ = self.gate()
        self.assertEqual(rc, 1)
        self.assertIn("FAIL [recording] rest-a: the recorder had stopped before the window ended", text)

    def test_a_cpu_time_that_goes_down_is_unreadable(self):
        (self.state / "cpu.txt").write_text("0:01.00\n0:02.00\n0:01.00\n0:01.00\n0:01.00\n")
        rc, _, err = self.run_cli()
        self.assertEqual(rc, 0, err)
        st = {s["id"]: s for s in json.loads((self.root / "out" / "run-01" / "run.json").read_text())["steps"]}
        self.assertEqual((st["rest-a"]["cpu_s"], st["rest-a"]["cpu_pct"]), (None, None))
        rc, text, _ = self.gate()
        self.assertEqual(rc, 1)
        self.assertIn("FAIL [rest-cpu] rest-a: CPU at rest could not be read (the CPU time went down between the two "
                      "readings (2.0 s, then 1.0 s))", text)

    def test_an_empty_target_is_refused(self):
        rc, _, err = main("run", "--simulator", "", "--scenario", self.scenario(), "--out", str(self.root / "x"))
        self.assertEqual(rc, 2)
        self.assertIn("--simulator needs a value", err)
        rc, _, err = main("run", "--serial", " ", "--scenario", self.scenario(), "--out", str(self.root / "y"))
        self.assertEqual(rc, 2)
        self.assertIn("--serial needs a value", err)
        rc, _, err = main("doctor", "--simulator", "")
        self.assertEqual(rc, 2)
        self.assertIn("--simulator needs a value", err)
        self.assertEqual(self.log(), [])

    def test_sigterm_and_sighup_stop_the_recorder_clear_the_status_bar_and_leave_failed_json(self):
        script = Path(u.__file__)
        for sig, code in ((15, 143), (1, 129)):
            out = self.root / f"sig{sig}"
            scn = self.scenario({**SCN, "steps": [{"id": "rest-a", "do": "rest", "ms": 30000}]})
            before = len(self.recorder_pids())
            proc = subprocess.Popen([sys.executable, "-B", str(script), "run", "--simulator", UDID, "--scenario", scn,
                                     "--out", str(out)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            deadline = time.monotonic() + 20
            while len(self.recorder_pids()) <= before and time.monotonic() < deadline:
                time.sleep(0.05)
            time.sleep(0.5)                     # the recorder has started and the rest is under way
            proc.send_signal(sig)
            _, err = proc.communicate(timeout=30)
            self.assertEqual(proc.returncode, code, err)
            self.assertFalse([p for p in self.recorder_pids() if self.alive(p)], sig)
            self.assertEqual(json.loads((out / "run-01" / "failed.json").read_text())["error"],
                             "terminated by " + ("SIGTERM" if sig == 15 else "SIGHUP"))
            self.assertEqual(self.log()[-1], f"simctl status_bar {UDID} clear")


# --------------------------------------------------------------------------------------------
# saved simulator runs: analyze, gate, summary, compare, zoom
# --------------------------------------------------------------------------------------------

class SavedSimRunTest(SimBase):
    def make(self, out="out", cpu="0:01.00\n", runs=1, **video):
        (self.state / "cpu.txt").write_text(cpu)
        (self.state / "cpu.n").write_text("0")
        self.video.update(video)
        rc, _, err = self.run_cli("--runs", str(runs), out=out)
        self.assertEqual(rc, 0, err)
        return self.root / out

    def test_analyze_and_sheet_redo_it_without_a_device(self):
        base = self.make()
        (self.state / "xcrun.log").unlink()
        self.video["rest-a"] = block("rest-a", frames=4, identical=4, region=0)
        rc, out, err = main("analyze", str(base))
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.log(), [])
        st = {s["id"]: s for s in json.loads((base / "run-01" / "run.json").read_text())["steps"]}
        self.assertEqual(st["rest-a"]["recording"]["identical"], 4)
        self.assertEqual(main("sheet", str(base / "run-01"))[0], 0)

    def test_a_damaged_step_record_is_an_error_not_a_defect(self):
        base = self.make()
        (base / "run-01" / "raw" / "step-rest-a.json").write_text("{not json")
        rc, _, err = main("analyze", str(base))
        self.assertEqual(rc, 2)
        self.assertIn("raw/step-rest-a.json is not valid JSON", err)
        rc, text, _ = main("gate", str(base))
        self.assertEqual(rc, 1)
        self.assertIn("not analysed", text)

    def test_summary_and_compare(self):
        base = self.make(out="base", runs=2)
        head = self.make(out="head", runs=2, cpu="0:01.00\n0:01.00\n0:01.50\n")
        out = u.summary([str(base)])
        self.assertEqual(out["stats"]["rest-a.sim_cpu_pct"]["n"], 2)
        for key in ("sim_window_s", "sim_cpu_s", "sim_recorded_frames", "sim_changed_app_frames", "sim_noise_frames",
                    "sim_bars_only_frames", "sim_identical_frames"):
            self.assertIn(f"rest-b.{key}", out["stats"], key)
        self.assertFalse([k for k in out["stats"] if k.startswith("rest-a.video_") or k == "rest-a.cpu_s"])
        res = u.compare([str(base)], [str(head)], [], 0.01, 2)
        self.assertIn("rest-a.sim_cpu_s", res["metrics"])

    def test_zoom_needs_the_step(self):
        base = self.make()
        rc, _, err = main("zoom", str(base / "run-01"), "1", "2")
        self.assertEqual(rc, 2)
        self.assertIn("a simulator run has one recording per rest step; pass --step <id>", err)
        with mock.patch.object(ui_video, "zoom", lambda video, a, b, out: {"video": str(video), "out": str(out)}):
            rc, out, _ = main("zoom", str(base / "run-01"), "1", "2", "--step", "rest-b")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["video"], str(base / "run-01" / "raw" / "rec-rest-b.mp4"))
        self.assertTrue(json.loads(out)["out"].endswith("sheets/zoom-rest-b-1-2.png"))

    def test_gate_override(self):
        base = self.make()
        other = self.root / "o.json"
        other.write_text(json.dumps({**SCN, "name": "Another"}))
        rc, _, err = main("gate", str(base), "--scenario", str(other))
        self.assertEqual(rc, 2)
        self.assertIn("--scenario is 'Another' but the run was captured with 'Sim rest'", err)
        revised = self.root / "r.json"
        revised.write_text(json.dumps({**SCN, "steps": [{"id": "rest-a", "do": "tap", "x": 0.5, "y": 0.5}, SCN["steps"][1]]}))
        rc, _, err = main("gate", str(base), "--scenario", str(revised))
        self.assertEqual(rc, 2)
        self.assertIn("the iOS Simulator has no input in this version", err)
        revised.write_text(json.dumps({**SCN, "gate": {"rest_cpu_pct_max": 5}}))
        self.assertEqual(main("gate", str(base), "--scenario", str(revised))[0], 0)


# --------------------------------------------------------------------------------------------
# the gate for a simulator run
# --------------------------------------------------------------------------------------------

def sim_run(**rest):
    rec = {"frames": 0, "changed_app": 0, "changed_app_noise": 0, "identical": 0, "bars_only": 0, "signature": "-"}
    step = {"id": "rest-a", "kind": "rest", "pid": PID, "process_missing": False, "process_restarted": False,
            "window_s": 5.0, "cpu_s": 0.0, "cpu_pct": 0.0, "recorder_exit": 0, "recorder_killed": False,
            "recorder_alive_at_end": True, "recording": rec}
    for k, v in rest.items():
        if k in rec:
            rec[k] = v
        else:
            step[k] = v
    return {"platform": u.SIM_PLATFORM, "launch": {"pid": PID, "error": None}, "host": {"load1_before": 1.0},
            "steps": [{"id": "launch", "kind": "launch", "pid": PID, "process_missing": False, "process_restarted": False},
                      step]}


def sim_scn(**allow):
    st = {"id": "rest-a", "do": "rest", "ms": 5000}
    if allow:
        st["allow"] = {**allow, "reason": "the caret blinks"}
    return {"name": "Sim rest", "package": BUNDLE, "steps": [st]}


class SimGateTest(unittest.TestCase):
    def gate(self, run, scn=None):
        return u.gate_sim_run(run, scn or sim_scn(), dict(u.GATE_DEFAULTS))

    def checks(self, res):
        return [v["check"] for v in res["violations"]]

    def test_clean(self):
        res = self.gate(sim_run())
        self.assertEqual(res["verdict"], "PASS")
        self.assertTrue(res["notes"][0].startswith("iOS Simulator: not checked, since a simulator does not give them: "
                                                   "waiting for quiet, frames at launch, which app is in front, find, trace sections"))

    def test_frames_that_changed_nothing_fail_with_no_exemption(self):
        res = self.gate(sim_run(frames=3, identical=3))
        self.assertEqual(self.checks(res), ["rest-frames", "rest-video"])
        self.assertIn("3 frames were composed at rest that changed nothing on screen", res["violations"][1]["message"])
        self.assertFalse(any("presented no frame" in n for n in res["notes"]))

    def test_a_change_in_the_app_area_fails(self):
        res = self.gate(sim_run(frames=2, changed_app=2))
        self.assertEqual(self.checks(res), ["rest-frames", "rest-video"])
        self.assertIn("2 frames recorded at rest (2 changed the app's area, 0 changed it by encoder noise only, 0 changed "
                      "only the system bars, 0 changed nothing), allowed 0", res["violations"][0]["message"])

    def test_every_recorded_frame_counts_whatever_its_class(self):
        """The reviewer's cases: a small spinner classed as noise, a progress bar inside the bar bands."""
        for kw, named in (({"frames": 59, "changed_app_noise": 59}, "59 changed it by encoder noise only"),
                          ({"frames": 3, "bars_only": 3}, "3 changed only the system bars")):
            res = self.gate(sim_run(**kw))
            self.assertEqual(self.checks(res), ["rest-frames", "rest-video"], kw)
            self.assertIn(named, res["violations"][0]["message"])
            self.assertIn(f"the screen changed at rest: {kw['frames']} recorded frames changed pixels",
                          res["violations"][1]["message"])
        res = self.gate(sim_run(frames=5, bars_only=3, changed_app_noise=2), sim_scn(frames_max=5, changed_frames_max=5))
        self.assertEqual(res["verdict"], "PASS")
        self.assertTrue(any("passed on the step's allowance of 5" in n for n in res["notes"]))

    def test_cpu(self):
        res = self.gate(sim_run(cpu_s=1.45, cpu_pct=29.0))
        self.assertEqual(self.checks(res), ["rest-cpu"])
        self.assertIn("29.0% of one core used at rest (1.45 s of CPU in 5.0 s), allowed 2.0%", res["violations"][0]["message"])
        res = self.gate(sim_run(cpu_pct=3.0), sim_scn(cpu_pct_max=4))
        self.assertEqual(res["verdict"], "PASS")
        self.assertTrue(any("passed on the step's allowance of 4%" in n for n in res["notes"]))
        res = self.gate(sim_run(cpu_pct=5.0), sim_scn(cpu_pct_max=4))
        self.assertEqual(res["violations"][0]["limit_from"], "allowance")
        res = self.gate(sim_run(cpu_pct=2.5), None)
        self.assertEqual(self.checks(res), ["rest-cpu"])
        res = u.gate_sim_run(sim_run(cpu_pct=2.5), sim_scn(), {**u.GATE_DEFAULTS, "rest_cpu_pct_max": 3})
        self.assertEqual(res["verdict"], "PASS")

    def test_cpu_unknown_fails(self):
        self.assertEqual(self.checks(self.gate(sim_run(cpu_s=None, cpu_pct=None))), ["rest-cpu"])

    def test_allowances(self):
        scn = sim_scn(frames_max=12, changed_frames_max=12)
        res = self.gate(sim_run(frames=10, changed_app=10), scn)
        self.assertEqual(res["verdict"], "PASS")
        self.assertTrue(any("passed on the step's allowance of 12 (the caret blinks)" in n for n in res["notes"]))
        res = self.gate(sim_run(frames=13, changed_app=13), scn)
        self.assertEqual(self.checks(res), ["rest-frames", "rest-video"])
        self.assertEqual({v["limit_from"] for v in res["violations"]}, {"allowance"})
        # frames that changed nothing have no allowance
        res = self.gate(sim_run(frames=2, identical=2), scn)
        self.assertEqual(self.checks(res), ["rest-video"])

    def test_a_missing_or_unusable_recording(self):
        res = self.gate(sim_run(recording=None))
        self.assertEqual(self.checks(res), ["recording", "rest-frames"])
        res = self.gate(sim_run(recording={"error": "the recording holds no video frames"}))
        self.assertEqual(self.checks(res), ["recording", "rest-frames"])
        self.assertIn("the recording holds no video frames", res["violations"][0]["message"])
        self.assertEqual(self.checks(self.gate(sim_run(recorder_exit=1))), ["recording"])

    def test_a_recorder_that_stopped_before_the_window_ended(self):
        for alive in (False, None):
            res = self.gate(sim_run(recorder_alive_at_end=alive))
            self.assertEqual(self.checks(res), ["recording"], alive)
            self.assertIn("the recorder had stopped before the window ended", res["violations"][0]["message"])

    def test_process_and_missing_step(self):
        self.assertEqual(self.checks(self.gate(sim_run(process_missing=True))), ["process"])
        self.assertEqual(self.checks(self.gate(sim_run(process_restarted=True))), ["process"])
        run = sim_run()
        run["steps"] = run["steps"][:1]
        self.assertEqual(self.checks(self.gate(run)), ["missing-step"])
        run = sim_run()
        run["launch"]["error"] = "the launch printed no process id"
        self.assertEqual(self.checks(self.gate(run)), ["launch"])


# --------------------------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------------------------

class SimDoctorTest(SimBase):
    def check(self, out, name):
        return next((c for c in out["checks"] if c["check"] == name), None)

    def test_booted(self):
        out = u.doctor(None, UDID)
        self.assertTrue(self.check(out, "xcrun")["ok"])
        self.assertTrue(self.check(out, "xcrun simctl")["ok"])
        self.assertEqual(self.check(out, "simulator")["detail"],
                         f"{UDID}: Test Phone, com.apple.CoreSimulator.SimRuntime.iOS-26-1")
        self.assertTrue(self.check(out, "simulator booted")["ok"])
        self.assertIsNone(self.check(out, "adb"))
        self.assertIsNotNone(self.check(out, "ffprobe"))

    def test_shutdown_or_unknown(self):
        (self.state / "devices.json").write_text(devices(state="Shutdown"))
        out = u.doctor(None, UDID)
        self.assertFalse(self.check(out, "simulator booted")["ok"])
        self.assertFalse(out["ok"])
        (self.state / "devices.json").write_text(devices(udid="other"))
        out = u.doctor(None, UDID)
        self.assertFalse(self.check(out, "simulator")["ok"])
        self.assertFalse(out["ok"])
        rc, text, _ = main("doctor", "--simulator", UDID)
        self.assertEqual(rc, 2)
        self.assertIn(f"MISS simulator: {UDID}: no such simulator", text)


# --------------------------------------------------------------------------------------------
# the recording analysis on a real file
# --------------------------------------------------------------------------------------------

@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "no ffmpeg")
class SimRecordingCountsTest(unittest.TestCase):
    def test_counts_from_a_real_video(self):
        from PIL import Image, ImageDraw
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            w, h = 270, 586
            frames = []
            base = Image.new("L", (w, h), 40)
            ImageDraw.Draw(base).text((20, 200), "Hello", fill=220)
            frames.append(base)
            frames.append(base.copy())                                   # changed nothing
            bars = base.copy()
            ImageDraw.Draw(bars).rectangle([200, 5, 260, 30], fill=255)    # the status bar only (top 7%)
            frames.append(bars)
            app = bars.copy()
            ImageDraw.Draw(app).rectangle([40, 250, 230, 400], fill=200)   # the app's area
            frames.append(app)
            for n, img in enumerate(frames):
                img.convert("RGB").save(root / f"f{n:02d}.png")
            video = root / "rec.mkv"
            subprocess.run(["ffmpeg", "-v", "error", "-framerate", "1", "-i", str(root / "f%02d.png"), "-c:v", "png",
                            str(video)], check=True)
            run = root / "run"
            counts = u.sim_recording_counts(video, run / "video" / "rest-a", "rest-a", "Sim rest")
        self.assertEqual({k: counts[k] for k in ("frames", "identical", "bars_only", "changed_app")},
                         {"frames": 3, "identical": 1, "bars_only": 1, "changed_app": 1})
        self.assertEqual(counts["sheet"], "video/rest-a/sheets/rest-a.png")


class DocsTest(unittest.TestCase):
    def test_documented(self):
        root = Path(u.HERE).parent
        readme = " ".join((root / "README.md").read_text().split())
        for phrase in ("## The iOS Simulator", "--simulator <udid>", "`ps -o cputime=", "recordVideo",
                       "never applies on a simulator", "any frame recorded at rest fails", "The app is left running after the run",
                       "about 6.8 s long, held 408 to 412 frames", "299 to 304 frames", "SIGKILL cannot be caught",
                       "status_bar <udid> override --time 9:41", "`<step>.sim_cpu_s`"):
            self.assertIn(phrase, readme)
        self.assertIn("## 0.4.0", (root / "CHANGELOG.md").read_text())
        skill = " ".join((root / "skills" / "ui-probe" / "SKILL.md").read_text().split())
        self.assertIn("--simulator", skill)
        self.assertIn("every frame recorded at rest after the first fails, whatever it changed", skill)
        self.assertEqual(u.VERSION, "0.4.0")
        self.assertEqual(json.loads((root / ".claude-plugin" / "plugin.json").read_text())["version"], "0.4.0")


if __name__ == "__main__":
    unittest.main()
