"""The host side of `run` with adb replaced by fakes, the device script's clean-up, `doctor`, and
the command line's exit codes. No device, no adb, no ffmpeg."""
import sys

sys.dont_write_bytecode = True

import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import ui_probe as u  # noqa: E402
import ui_video  # noqa: E402

PKG = "com.example.app"
SCN = {"name": "Example", "package": PKG, "steps": [{"id": "idle", "do": "rest", "ms": 1000}]}
FACTS = {"width": 1080, "height": 2400, "uid": 10100, "clk_tck": 100}


def main(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = u.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


class FakeTracer:
    def __init__(self, *a, **k):
        self.stdin = io.BytesIO()
        self.stdin.close = lambda: None
        self.communicated = False
        self.killed = False

    def communicate(self, timeout=None):
        self.communicated = True
        return b"perfetto output", None

    def poll(self):
        return 0 if self.communicated else None

    def kill(self):
        self.killed = True


class FakeDevice:
    """Records every shell command; the device script can be made to fail."""

    def __init__(self, script_error=None, listing="done.txt marks.txt"):
        self.commands = []
        self.script_error = script_error
        self.listing = listing
        self.tracers = []

    def sh(self, serial, command, **kw):
        self.commands.append(command)
        if command.startswith("sh ") and command.endswith("/run.sh") and self.script_error:
            raise self.script_error
        if command.startswith("ls "):
            return self.listing
        return ""

    def adb(self, serial, *args, **kw):
        self.commands.append(" ".join(args))
        return ""

    def popen(self, *a, **k):
        t = FakeTracer()
        self.tracers.append(t)
        return t

    def patches(self):
        return [mock.patch.object(u, "sh", self.sh), mock.patch.object(u, "adb", self.adb),
                mock.patch.object(u, "device_facts", lambda serial, pkg: dict(FACTS)),
                mock.patch.object(u.subprocess, "Popen", self.popen), mock.patch.object(u.time, "sleep", lambda s: None)]


class RunOnceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name) / "run-01"

    def tearDown(self):
        self.tmp.cleanup()

    def run_once(self, device, trace=True):
        with contextlib.ExitStack() as stack:
            for p in device.patches():
                stack.enter_context(p)
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            return u.run_once("<serial>", SCN, self.out, trace, False)

    def test_a_failed_device_script_cleans_up_and_stops_the_tracer(self):
        """Finding 19 (and 17): a host timeout leaves no tracer, recorder or statistics running."""
        device = FakeDevice(script_error=u.ProbeError("adb shell sh /data/local/tmp/uiprobe/run.sh did not finish within 60 s"))
        with self.assertRaises(u.ProbeError):
            self.run_once(device)
        self.assertTrue(device.tracers[0].communicated)
        self.assertIn("pkill -INT -x perfetto", device.commands)
        cleanup = [c for c in device.commands if "--timestats -disable" in c]
        self.assertEqual(len(cleanup), 1)
        self.assertIn("script.pid", cleanup[0])
        self.assertIn("grep -q screenrecord /proc/$p/cmdline", cleanup[0])
        self.assertFalse((self.out / "capture.json").exists())

    def test_an_unfinished_script_cleans_up(self):
        device = FakeDevice(listing="marks.txt")
        with self.assertRaises(u.ProbeError) as ctx:
            self.run_once(device, trace=False)
        self.assertIn("did not finish", str(ctx.exception))
        self.assertTrue(any("--timestats -disable" in c for c in device.commands))

    def test_a_good_run_does_not_clean_up_twice(self):
        device = FakeDevice()
        with mock.patch.object(u, "analyze_run", lambda out: {"steps": []}):
            self.run_once(device)
        self.assertFalse(any("script.pid" in c for c in device.commands))
        self.assertEqual(json.loads((self.out / "capture.json").read_text())["tracer_output"], "perfetto output")

    def test_a_failed_analysis_names_the_run(self):
        """Finding 6: if analysis fails inside `run`, the command fails and names the run directory."""
        device = FakeDevice()
        with self.assertRaises(u.ProbeError) as ctx:
            self.run_once(device)       # nothing was pulled, so raw/marks.txt is missing
        self.assertIn(str(self.out), str(ctx.exception))
        self.assertIn("the capture was saved but its analysis failed", str(ctx.exception))
        self.assertTrue((self.out / "capture.json").exists())

    def test_run_command_exit_codes(self):
        """Finding 17: 2 for the tool's own error, 3 (with a traceback) for an unexpected one."""
        scn = Path(self.tmp.name) / "scenario.json"
        scn.write_text(json.dumps(SCN))
        device = FakeDevice()
        with contextlib.ExitStack() as stack:
            for p in device.patches():
                stack.enter_context(p)
            rc, _, err = main("run", "--serial", "<serial>", "--scenario", str(scn), "--out", str(Path(self.tmp.name) / "a"))
            self.assertEqual(rc, 2)
            self.assertIn("analysis failed", err)
            with mock.patch.object(u, "analyze_run", side_effect=KeyError("boom")):
                rc, _, err = main("run", "--serial", "<serial>", "--scenario", str(scn), "--out", str(Path(self.tmp.name) / "b"))
        self.assertEqual(rc, 3)
        self.assertIn("Traceback", err)
        self.assertIn(str(Path(self.tmp.name) / "b" / "run-01"), err)


class AdbFailureMessageTest(unittest.TestCase):
    """A device script killed mid-run prints nothing: the message must still say what happened."""

    def run_adb(self, code, out=b"", err=b""):
        done = subprocess.CompletedProcess(["adb"], code, stdout=out, stderr=err)
        with mock.patch.object(u.subprocess, "run", return_value=done):
            with self.assertRaises(u.ProbeError) as ctx:
                u.sh("<serial>", "sh /data/local/tmp/uiprobe/run.sh")
        return str(ctx.exception)

    def test_exit_status_is_named_when_nothing_was_printed(self):
        self.assertEqual(self.run_adb(143), "adb shell sh /data/local/tmp/uiprobe/run.sh failed (exit 143): no output")

    def test_stderr_then_stdout(self):
        self.assertTrue(self.run_adb(1, out=b"partial\n", err=b"device offline\n").endswith("(exit 1): device offline"))
        self.assertTrue(self.run_adb(1, out=b"last words\n").endswith("(exit 1): last words"))


class AdbWrapperTest(unittest.TestCase):
    """Finding 17: a timeout or a missing adb is the tool's own error."""

    def test_timeout(self):
        with mock.patch.object(u.subprocess, "run", side_effect=subprocess.TimeoutExpired(["adb"], 5)):
            with self.assertRaises(u.ProbeError) as ctx:
                u.sh("<serial>", "sh run.sh", timeout=5)
        self.assertIn("did not finish within 5 s", str(ctx.exception))

    def test_missing_adb(self):
        with mock.patch.object(u.subprocess, "run", side_effect=FileNotFoundError(2, "No such file or directory")):
            with self.assertRaises(u.ProbeError):
                u.adb("<serial>", "get-state")

    def test_empty_avd_name(self):
        def fake_sh(serial, command, **kw):
            return "Physical size: 1080x2400" if command.startswith("wm size") else (
                f"Package [{PKG}]\n" if command.startswith("dumpsys package '") else "")
        with mock.patch.object(u, "sh", fake_sh), mock.patch.object(u, "adb", lambda *a, **k: ""):
            self.assertIsNone(u.device_facts("emulator-<port>", PKG)["avd"])


class DeviceScriptCleanupTest(unittest.TestCase):
    """Finding 19: the device script restores the device however it ends."""

    def script(self, video=True):
        u.validate_scenario(SCN)
        return u.build_device_script(SCN, 1080, 2400, video, 60)

    def test_traps(self):
        lines = self.script().splitlines()
        for line in ("trap cleanup EXIT", "trap 'cleanup; trap - EXIT; exit 129' HUP",
                     "trap 'cleanup; trap - EXIT; exit 130' INT", "trap 'cleanup; trap - EXIT; exit 143' TERM",
                     "  dumpsys SurfaceFlinger --timestats -disable >/dev/null 2>&1",
                     '  if [ -n "$SR" ]; then kill -2 $SR 2>/dev/null; fi', "echo $$ > $D/script.pid",
                     "echo $SR > $D/sr.pid"):
            self.assertIn(line, lines)
        # the traps are set before the statistics are switched on
        self.assertLess(lines.index("trap cleanup EXIT"), lines.index("dumpsys SurfaceFlinger --timestats -enable -clear >/dev/null 2>&1"))

    def test_cpu_is_read_before_and_after_the_dumps(self):
        """Finding 9: the snapshot reads CPU first and last, each with its own uptime stamp."""
        lines = self.script().splitlines()
        snap = lines[lines.index("snap() {"):]
        snap = snap[:snap.index("}") + 1]
        self.assertEqual(snap, ["snap() {", '  PID=$(pidof "$PKG"); P=${PID%% *}', '  echo "$PID" > $D/pid-$1.txt',
                                '  cpu $1 "$P"',
                                "  dumpsys activity activities 2>/dev/null | grep -m1 -E "
                                "'topResumedActivity|mResumedActivity|ResumedActivity' > $D/fg-$1.txt",
                                '  dumpsys gfxinfo "$PKG" framestats > $D/gfx-$1.txt 2>&1',
                                "  dumpsys SurfaceFlinger --timestats -dump > $D/sf-$1.txt 2>&1",
                                '  if [ "$2" = mem ]; then dumpsys meminfo "$PKG" > $D/mem-$1.txt 2>&1; fi',
                                '  cpu $1.after "$P"', "}"])
        text = "\n".join(lines)
        self.assertIn("    if [ -n \"$2\" ]; then echo process; cat /proc/$2/stat; echo threads; cat /proc/$2/task/*/stat; fi", text)

    @unittest.skipUnless(shutil.which("sh"), "no sh")
    def test_the_script_parses(self):
        """A syntax check with each POSIX shell on the host. The device's mksh is not among them."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.sh"
            scn = {**SCN, "activity": ".Outer$Inner", "until_quiet": True,
                   "steps": SCN["steps"] + [{"id": "open", "do": "tap", "x": 0.5, "y": 0.5}]}
            path.write_text(u.build_device_script(scn, 1080, 2400, True, 60))
            for shell in ("sh", "dash", "bash"):
                if shutil.which(shell):
                    proc = subprocess.run([shell, "-n", str(path)], capture_output=True, text=True)
                    self.assertEqual(proc.returncode, 0, (shell, proc.stderr))


class DoctorTest(unittest.TestCase):
    """Finding 16: doctor checks that ffmpeg is new enough."""

    def doctor(self, ffmpeg_line):
        def fake_run(cmd, **kw):
            line = ffmpeg_line if cmd[0] == "ffmpeg" else f"{cmd[0]} version 1.0"
            return subprocess.CompletedProcess(cmd, 0, stdout=line + "\n", stderr="")
        with mock.patch.object(u.shutil, "which", lambda tool: f"/usr/bin/{tool}"), \
                mock.patch.object(u.subprocess, "run", fake_run):
            return u.doctor(None)

    def check(self, out, name):
        return next(c for c in out["checks"] if c["check"] == name)

    def test_versions(self):
        self.assertEqual(u.ffmpeg_version("ffmpeg version 7.0.1 Copyright (c) 2000-2024"), (7, 0))
        self.assertEqual(u.ffmpeg_version("ffmpeg version n5.1.2 Copyright"), (5, 1))
        self.assertEqual(u.ffmpeg_version("ffmpeg version 4.4.2-0ubuntu0.22.04.1 Copyright"), (4, 4))
        self.assertIsNone(u.ffmpeg_version("ffmpeg version N-112233-gabcdef Copyright"))

    def test_old_ffmpeg_fails(self):
        out = self.doctor("ffmpeg version 4.4.2-0ubuntu0.22.04.1 Copyright")
        c = self.check(out, "ffmpeg version")
        self.assertEqual((c["ok"], c["required"]), (False, True))
        self.assertFalse(out["ok"])

    def test_new_ffmpeg_passes(self):
        out = self.doctor("ffmpeg version 5.1 Copyright")
        self.assertTrue(self.check(out, "ffmpeg version")["ok"])

    def test_unreadable_version_is_a_warning(self):
        out = self.doctor("ffmpeg version N-112233-gabcdef Copyright")
        c = self.check(out, "ffmpeg version")
        self.assertEqual((c["ok"], c["required"], c["warning"]), (False, False, True))
        self.assertEqual([x["check"] for x in out["checks"] if x["required"] and not x["ok"]], [])

    def test_doctor_failure_exits_2(self):
        with mock.patch.object(u, "doctor", lambda serial: {"ok": False, "checks": [
                {"check": "ffmpeg version", "ok": False, "required": True, "warning": False, "detail": "4.4"},
                {"check": "x", "ok": False, "required": False, "warning": True, "detail": "y"}]}):
            rc, out, _ = main("doctor")
        self.assertEqual(rc, 2)
        self.assertIn("MISS ffmpeg version: 4.4", out)
        self.assertIn("WARN x: y", out)


class ZoomExitTest(unittest.TestCase):
    """Finding 17: `zoom` exits 2 when it cannot do its job."""

    def test_missing_recording(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, out, err = main("zoom", tmp, "1", "2")
        self.assertEqual(rc, 2)
        self.assertIn("zoom:", err)

    def test_frames_not_extracted(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "raw").mkdir()
            (Path(tmp) / "raw" / "screen.mp4").write_bytes(b"x")
            with mock.patch.object(ui_video, "_run", lambda cmd: ""):
                rc, _, err = main("zoom", tmp, "1", "2", "--out", str(Path(tmp) / "z.png"))
        self.assertEqual(rc, 2)
        self.assertIn("could not extract both frames", err)


if __name__ == "__main__":
    unittest.main()
