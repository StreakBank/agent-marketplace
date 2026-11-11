"""0.5.0: input on the iOS Simulator through the input agent, and a launch environment. Stand-ins on
PATH for `xcodebuild` (a build that writes an .xctestrun; a test run that starts a small agent
speaking the agent's protocol on the port the host passed), `xcrun` and `ps`. No simulator, no device,
no real Xcode."""
import sys

sys.dont_write_bytecode = True

import contextlib
import json
import os
import shutil
import subprocess
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import test_simulator_040 as s040  # noqa: E402  (its stand-ins and base class; its tests are not imported here)
import ui_probe as u  # noqa: E402

BUNDLE, UDID, main = s040.BUNDLE, s040.UDID, s040.main
INPUT_SCN = {"name": "Sim input", "package": BUNDLE, "launch_settle_ms": 0, "steps": [
    {"id": "open", "do": "tap", "x": 0.5, "y": 0.9, "settle_ms": 100, "expect": {"signature": "T"}},
    {"id": "rest-a", "do": "rest", "ms": 100},
    {"id": "pull", "do": "swipe", "from": [0.5, 0.3], "to": [0.5, 0.7], "ms": 200, "judge": "state", "settle_ms": 100}]}


def sig_block(sid, signature, frames=3):
    return {"frames": frames + 1, "clock": "start-mark-fallback", "steps": [
        {"id": sid, "video_frames": frames, "identical_frames": 0, "app_region_frames": frames,
         "app_changed_frames_real": frames, "signature": signature}]}


class InputBase(s040.SimBase):
    def setUp(self):
        super().setUp()
        for name, value in (("AGENT_START_TIMEOUT_S", 10), ("AGENT_QUIT_WAIT_S", 3), ("AGENT_TERM_WAIT_S", 1)):
            p = mock.patch.object(u, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.analysed = []
        self.video["open"] = sig_block("open", "T")
        self.video["pull"] = sig_block("pull", "E")

    def fake_analyze(self, video, out, rows, marks, scn, facts):
        self.analysed.append(dict(scn["steps"][0]))
        return super().fake_analyze(video, out, rows, marks, scn, facts)

    def lines(self, name):
        p = self.state / name
        return p.read_text().splitlines() if p.exists() else []

    def cmds(self):
        """The commands the agent received, without the nonce each one carried."""
        return [{k: v for k, v in json.loads(l).items() if k != "nonce"} for l in self.lines("agent-cmds")]

    def key_folder(self):
        folder = self.cache / "ui-probe" / "ios-agent"
        return next(p for p in folder.iterdir() if p.is_dir() and "." not in p.name)

    def pids(self, name):
        return [int(x) for x in self.lines(name)]

    def gone(self, name, wait_s=3.0):
        deadline = time.monotonic() + wait_s
        while any(self.alive(p) for p in self.pids(name)) and time.monotonic() < deadline:
            time.sleep(0.05)
        return not [p for p in self.pids(name) if self.alive(p)]

    def cleaned_up(self):
        """The agent and its group gone, the runner terminated and uninstalled, the status bar cleared last."""
        log = self.log()
        self.assertTrue(self.pids("agent-pids"))
        self.assertTrue(self.gone("agent-pids"))
        runner = [l for l in log if l.endswith(u.AGENT_RUNNER)]
        self.assertEqual(runner, [f"simctl terminate {UDID} {u.AGENT_RUNNER}", f"simctl uninstall {UDID} {u.AGENT_RUNNER}"])
        self.assertLess(log.index(runner[-1]), len(log) - 1)
        self.assertEqual(log[-1], f"simctl status_bar {UDID} clear")

    def tearDown(self):
        for pid in self.pids("agent-pids") + self.pids("agent-grandchild-pids"):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, 9)
        super().tearDown()


# --------------------------------------------------------------------------------------------
# the scenario
# --------------------------------------------------------------------------------------------

class InputScenarioTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(u, "SIM_REST_MIN_MS", 100)
        p.start()
        self.addCleanup(p.stop)

    def reject(self, scn, fragment, platform=u.SIM_PLATFORM):
        with self.assertRaises(u.ProbeError) as cm:
            u.validate_scenario(scn, platform)
        self.assertIn(fragment, str(cm.exception))

    def test_taps_and_swipes_are_accepted(self):
        u.validate_scenario(INPUT_SCN, u.SIM_PLATFORM)
        self.assertTrue(u.sim_needs_agent(INPUT_SCN))
        self.assertFalse(u.sim_needs_agent(s040.SCN))

    def test_refusals(self):
        one = lambda st: {**INPUT_SCN, "steps": [st]}
        lookup = "a lookup or typing reads the app's accessibility tree, which changed the app at rest when measured, so " \
                 "this version takes positions as fractions"
        self.reject(one({"id": "f", "do": "tap", "find": {"text": "Go"}}), f"step 'f': 'find' is refused on the iOS "
                                                                           f"Simulator: {lookup}")
        self.reject(one({"id": "x", "do": "text", "text": "abc"}), f"step 'x': a 'text' step is refused on the iOS "
                                                                    f"Simulator: {lookup}")
        self.reject(one({"id": "k", "do": "key", "code": 4}), "step 'k': a 'key' step is refused on the iOS Simulator: a "
                    "simulator has no keys to press; tap the screen's own control or swipe from the edge")
        self.reject(one({"id": "t", "do": "tap", "x": 0.5, "y": 0.5, "until_quiet": True}),
                    "step 't': 'until_quiet' must be false or absent")
        self.reject({**INPUT_SCN, "until_quiet": True}, "'until_quiet' must be false or absent")
        for exp in ({"frames": 1}, {"signature": "T", "frames_max": 2}, {"sections_max": {"x": 1}}):
            self.reject(one({"id": "t", "do": "tap", "x": 0.5, "y": 0.5, "expect": exp}),
                        "step 't': 'expect' on a tap or a swipe on the iOS Simulator takes only 'signature' and "
                        "'signposts_max'")
        self.reject(one({"id": "r", "do": "rest", "ms": 1000, "expect": {"signature": "-"}}),
                    "step 'r': 'expect' on a rest on the iOS Simulator takes only 'signposts_max'")
        u.validate_scenario(one({"id": "t", "do": "tap", "x": 0.5, "y": 0.5, "until_quiet": False,
                                 "expect": {"signature": "T"}}), u.SIM_PLATFORM)

    def test_launch_env(self):
        ok = {**s040.SCN, "launch_env": {"API_ORIGIN": "https://localhost:18443", "A1_B": ""}}
        u.validate_scenario(ok, u.SIM_PLATFORM)
        u.validate_scenario(ok, "any")
        self.reject({**ok, "package": "com.example.app"}, "'launch_env' is refused on Android: an Android launch takes "
                    "no environment", platform=u.ANDROID)
        for env, fragment in (([], "'launch_env' must be an object of names to string values"),
                              ({"api": "x"}, "'launch_env' name 'api' must be capital letters"),
                              ({"1A": "x"}, "'launch_env' name '1A' must be capital letters"),
                              ({"A-B": "x"}, "'launch_env' name 'A-B' must be capital letters"),
                              ({"A": 5}, "'launch_env.A' must be a string of at most 200 characters"),
                              ({"A": "x" * 201}, "'launch_env.A' must be a string of at most 200 characters"),
                              ({"A": "a\nb"}, "'launch_env.A' holds a control character"),
                              ({"A": "a\x7fb"}, "'launch_env.A' holds a control character"),
                              ({f"N{i}": "x" for i in range(21)}, "'launch_env' holds 21 pairs; at most 20")):
            self.reject({**s040.SCN, "launch_env": env}, fragment)
        u.validate_scenario({**s040.SCN, "launch_env": {f"N{i}": "x" * 200 for i in range(20)}}, u.SIM_PLATFORM)

    def test_idle_limit(self):
        self.assertEqual(u.agent_idle_s(INPUT_SCN), 120)
        self.assertEqual(u.agent_idle_s({**INPUT_SCN, "launch_settle_ms": 9000}), 129)
        self.assertEqual(u.agent_idle_s({**INPUT_SCN, "steps": [{"id": "t", "do": "tap", "x": 0.5, "y": 0.5}]}), 123)


class CacheKeyTest(unittest.TestCase):
    def test_the_key_follows_xcode_and_the_source(self):
        with s040.tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "ios-agent"
            shutil.copytree(u.AGENT_DIR, copy)
            with mock.patch.object(u, "AGENT_DIR", copy):
                a = u.agent_cache_key("Xcode 26.3\nBuild version 17C1\n")
                self.assertEqual(a, u.agent_cache_key("Xcode 26.3\nBuild version 17C1\n"))
                self.assertNotEqual(a, u.agent_cache_key("Xcode 26.4\nBuild version 17E1\n"))
                swift = copy / "AgentUITests" / "AgentUITests.swift"
                swift.write_text(swift.read_text() + "\n// changed\n")
                self.assertNotEqual(a, u.agent_cache_key("Xcode 26.3\nBuild version 17C1\n"))
                # what Xcode writes for one user is not source
                (copy / "Agent.xcodeproj" / "xcuserdata").mkdir()
                b = u.agent_cache_key("v")
                (copy / "Agent.xcodeproj" / "xcuserdata" / "me.xcuserdatad").write_text("x")
                self.assertEqual(b, u.agent_cache_key("v"))

    def test_the_cache_folder(self):
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": "/somewhere"}):
            self.assertEqual(u.agent_cache_root(), Path("/somewhere/ui-probe/ios-agent"))
        env = {k: v for k, v in os.environ.items() if k != "XDG_CACHE_HOME"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(u.agent_cache_root(), Path.home() / "Library" / "Caches" / "ui-probe" / "ios-agent")

    def test_the_shipped_agent(self):
        files = {str(p.relative_to(u.AGENT_DIR)) for p in u.agent_source_files()}
        self.assertEqual(files, {"Agent.xcodeproj/project.pbxproj", "Agent.xcodeproj/xcshareddata/xcschemes/Agent.xcscheme",
                                 "AgentUITests/AgentUITests.swift"})
        swift = (u.AGENT_DIR / "AgentUITests" / "AgentUITests.swift").read_text()
        code = "\n".join(l for l in swift.splitlines() if not l.strip().startswith("//"))
        # no lookup, no typing, no read of the app under test but its state; loopback only; a token
        for banned in ("typeText", "descendants", "matching(", ".label", "staticTexts", "buttons", "keys", "app.frame",
                       "app.tap", "app.coordinate"):
            self.assertNotIn(banned, code, banned)
        self.assertEqual([l.strip() for l in code.splitlines() if "app." in l and "XCUIApplication" not in l],
                         ['return ["ok": true, "app_state": Int(app.state.rawValue), "screen": [Double(screen.width), '
                          'Double(screen.height)]]'])
        for needed in ('inet_addr("127.0.0.1")', 'env["UI_PROBE_AGENT_TOKEN"]', '(obj["token"] as? String) != token',
                       "UI_PROBE_AGENT_IDLE_S", "lastCommand", '"ok": false'):
            self.assertIn(needed, code, needed)
        self.assertIn("PRODUCT_BUNDLE_IDENTIFIER = dev.uiprobe.agent.uitests;",
                      (u.AGENT_DIR / "Agent.xcodeproj" / "project.pbxproj").read_text())


# --------------------------------------------------------------------------------------------
# runs with input
# --------------------------------------------------------------------------------------------

class InputRunTest(InputBase):
    def test_a_run_with_input(self):
        rc, out, err = self.run_cli(scn=INPUT_SCN)
        self.assertEqual(rc, 0, err)
        run = self.root / "out" / "run-01"
        xb = self.lines("xcodebuild.log")
        folder = self.cache / "ui-probe" / "ios-agent"
        key = self.key_folder().name
        self.assertEqual(xb[0], "-version")
        # built in a temporary folder under the cache root, renamed into place with its marker
        self.assertTrue(xb[1].startswith(f"build-for-testing -project {u.AGENT_DIR / 'Agent.xcodeproj'} -scheme Agent "
                                         f"-destination generic/platform=iOS Simulator -derivedDataPath "
                                         f"{folder / key}.building-"), xb[1])
        self.assertTrue((folder / key / "built.ok").is_file())
        self.assertFalse([p for p in folder.iterdir() if ".building-" in p.name])
        xctestrun = folder / key / "Build" / "Products" / "Agent_iphonesimulator26.1-arm64.xctestrun"
        self.assertEqual(xb[2], f"test-without-building -xctestrun {xctestrun} -destination platform=iOS Simulator,"
                                f"id={UDID} -only-testing:AgentUITests/AgentUITests/testServe -derivedDataPath "
                                f"{run / 'raw' / 'agent-derived-data'} -resultBundlePath {run / 'raw' / 'agent.xcresult'}")
        # every command carried a fresh nonce
        nonces = self.lines("agent-nonces")
        self.assertEqual(len(nonces), len(self.cmds()))
        self.assertEqual(len(set(nonces)), len(nonces))
        # a test run that ended with status 0 leaves no derived data and no result bundle
        self.assertFalse((run / "raw" / "agent-derived-data").exists())
        self.assertFalse((run / "raw" / "agent.xcresult").exists())
        env = json.loads((self.state / "agent-env.json").read_text())["env"]
        self.assertEqual(env["TEST_RUNNER_UI_PROBE_AGENT_BUNDLE"], BUNDLE)
        self.assertEqual(env["TEST_RUNNER_UI_PROBE_AGENT_IDLE_S"], "120")
        self.assertEqual(len(env["TEST_RUNNER_UI_PROBE_AGENT_TOKEN"]), 32)
        # every command carried the run's token; the order of a run
        self.assertEqual([c["cmd"] for c in self.cmds()], ["ping", "tap", "ping", "ping", "swipe", "ping", "quit"])
        self.assertEqual(self.cmds()[1], {"cmd": "tap", "x": 0.5, "y": 0.9})
        self.assertEqual(self.cmds()[4], {"cmd": "swipe", "from": [0.5, 0.3], "to": [0.5, 0.7], "ms": 200})
        # the agent was ready before the app was launched
        self.assertEqual(self.lines("launch-order"), ["agent started before the launch"])
        log = self.log()
        self.assertLess(log.index(f"simctl status_bar {UDID} override --time 9:41"), log.index(f"simctl launch {UDID} {BUNDLE}"))
        self.cleaned_up()
        step = json.loads((run / "raw" / "step-open.json").read_text())
        self.assertEqual((step["command"], step["reply"]["ok"], step["app_state"]), ({"cmd": "tap", "x": 0.5, "y": 0.9}, True, 4))
        self.assertIsInstance(step["round_trip_ms"], int)
        res = json.loads((run / "run.json").read_text())
        st = {s["id"]: s for s in res["steps"]}
        self.assertEqual((st["open"]["signature"], st["pull"]["signature"], st["rest-a"]["app_state"]), ("T", "E", 4))
        self.assertEqual(res["agent"]["stop"]["quit"]["ok"], True)
        self.assertNotIn("which app is in front", res["not_checked"])
        # each step judged by its kind
        self.assertEqual([(s["do"], s.get("judge")) for s in self.analysed], [("tap", None), ("rest", None), ("swipe", "state")])
        rc, text, _ = self.gate()
        self.assertEqual(rc, 0, text)
        note = next(l for l in text.splitlines() if "iOS Simulator: not checked" in l)
        self.assertNotIn("which app is in front", note)
        self.assertIn("a tap's or a swipe's frames, counts and CPU are information, never gated", note)
        self.assertEqual(json.loads(out.splitlines()[0])["open"], {"signature": "T", "ok": True})

    def test_a_rest_only_run_calls_no_xcodebuild(self):
        rc, _, err = self.run_cli()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.lines("xcodebuild.log"), [])
        self.assertEqual(self.cmds(), [])
        run = self.root / "out" / "run-01"
        step = json.loads((run / "raw" / "step-rest-a.json").read_text())
        self.assertEqual(set(step), {"pid", "cputime_before", "cputime_after", "window_s", "wall_before", "wall_after",
                                     "rec_wall_start", "rec_wall_end", "recorder_exit", "recorder_killed",
                                     "recorder_alive_at_end"})
        res = json.loads((run / "run.json").read_text())
        self.assertNotIn("agent", res)
        self.assertIn("which app is in front", res["not_checked"])
        self.assertFalse(any(u.AGENT_RUNNER in l for l in self.log()))

    def test_the_agent_is_built_once(self):
        self.assertEqual(self.run_cli(scn=INPUT_SCN)[0], 0)
        self.assertEqual(self.run_cli(scn=INPUT_SCN, out="out2")[0], 0)
        self.assertEqual(len([l for l in self.lines("xcodebuild.log") if l.startswith("build-for-testing")]), 1)
        (self.state / "xcode.version").write_text("Xcode 26.4\nBuild version 17E1")
        self.assertEqual(self.run_cli(scn=INPUT_SCN, out="out3")[0], 0)
        self.assertEqual(len([l for l in self.lines("xcodebuild.log") if l.startswith("build-for-testing")]), 2)

    def test_a_signature_that_does_not_match(self):
        self.video["open"] = sig_block("open", "TD")
        self.assertEqual(self.run_cli(scn=INPUT_SCN)[0], 0)
        rc, text, _ = self.gate()
        self.assertEqual(rc, 1)
        self.assertIn("FAIL [signature] open: signature 'TD', approved 'T'", text)

    def test_a_tap_the_agent_refuses(self):
        (self.state / "agent.mode").write_text("refuse-tap")
        self.assertEqual(self.run_cli(scn=INPUT_SCN)[0], 0)
        rc, text, _ = self.gate()
        self.assertEqual(rc, 1)
        self.assertIn("FAIL [input] open: the agent did not perform the tap: tap failed: the touch was not delivered", text)
        self.cleaned_up()

    def test_an_app_not_in_front(self):
        (self.state / "appstate").write_text("2")
        self.assertEqual(self.run_cli(scn=INPUT_SCN)[0], 0)
        rc, text, _ = self.gate()
        self.assertEqual(rc, 1)
        for sid in ("open", "rest-a", "pull"):
            self.assertIn(f"FAIL [foreground] {sid}: the app was not in front at the end of '{sid}' (its state: running in "
                          "the background, suspended)", text)

    def test_a_build_that_fails(self):
        (self.state / "build.mode").write_text("fail")
        rc, _, err = self.run_cli(scn=INPUT_SCN)
        self.assertEqual(rc, 2)
        folder = self.cache / "ui-probe" / "ios-agent"
        log = next(folder.glob("*.build.log"))
        self.assertIn(f"the input agent could not be built (xcodebuild exit 65); its log is {log}", err)
        self.assertFalse([p for p in folder.iterdir() if p.is_dir()])          # nothing half-built is kept
        self.assertIn("error: the agent did not compile", log.read_text())
        self.assertFalse([l for l in self.lines("xcodebuild.log") if l.startswith("test-without-building")])
        self.assertEqual(self.log()[-1], f"simctl status_bar {UDID} clear")
        self.assertTrue((self.root / "out" / "run-01" / "failed.json").exists())

    def test_an_agent_that_never_answers(self):
        (self.state / "agent.mode").write_text("silent")
        with mock.patch.object(u, "AGENT_START_TIMEOUT_S", 1.5):
            rc, _, err = self.run_cli(scn=INPUT_SCN)
        self.assertEqual(rc, 2)
        self.assertIn("the input agent did not answer within 1.5 s", err)
        self.assertIn("Testing started", err)
        self.cleaned_up()
        self.assertFalse([l for l in self.log() if l == f"simctl launch {UDID} {BUNDLE}"])

    def test_an_xcodebuild_that_exits_before_the_first_answer(self):
        (self.state / "agent.mode").write_text("exit")
        rc, _, err = self.run_cli(scn=INPUT_SCN)
        self.assertEqual(rc, 2)
        self.assertIn("xcodebuild exited (status 70) before the input agent answered", err)
        self.assertIn("Testing failed: Unable to boot the test runner", err)
        self.cleaned_up()

    def test_an_agent_that_refuses_the_token(self):
        (self.state / "agent.mode").write_text("badtoken")
        rc, _, err = self.run_cli(scn=INPUT_SCN)
        self.assertEqual(rc, 2)
        self.assertIn("the input agent answered but refused the run's first command: the reply was not proven to "
                      "come from the agent (no keyed hash of the command's nonce); it said: wrong or missing token", err)
        self.cleaned_up()

    def test_an_error_during_the_steps(self):
        (self.state / "rec.mode").write_text("die")
        rc, _, err = self.run_cli(scn=INPUT_SCN)
        self.assertEqual(rc, 2)
        self.assertEqual(self.cmds()[-1], {"cmd": "quit"})
        self.cleaned_up()

    def test_an_interrupt(self):
        calls = []
        real = u.host_cputime

        def cpu(pid):
            calls.append(pid)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return real(pid)
        with mock.patch.object(u, "host_cputime", cpu), self.assertRaises(KeyboardInterrupt):
            u.run_once_sim(UDID, INPUT_SCN, self.root / "out" / "run-01")
        self.cleaned_up()
        self.assertEqual(json.loads((self.root / "out" / "run-01" / "failed.json").read_text())["error"], "interrupted")

    def test_an_agent_deaf_to_quit_and_sigterm(self):
        (self.state / "agent.mode").write_text("deaf")
        with mock.patch.object(u, "AGENT_QUIT_WAIT_S", 0.5), mock.patch.object(u, "AGENT_TERM_WAIT_S", 0.5):
            rc, _, err = self.run_cli(scn=INPUT_SCN)
        self.assertEqual(rc, 0, err)
        self.cleaned_up()
        self.assertTrue(self.pids("agent-grandchild-pids"))
        self.assertTrue(self.gone("agent-grandchild-pids"))
        stop = json.loads((self.root / "out" / "run-01" / "raw" / "agent.json").read_text())["stop"]
        self.assertEqual((stop["terminated"], stop["killed"]), (True, True))

    def test_sigterm_and_sighup(self):
        scn = {**INPUT_SCN, "steps": [INPUT_SCN["steps"][0], {"id": "rest-a", "do": "rest", "ms": 30000}]}
        for sig, code in ((15, 143), (1, 129)):
            out = self.root / f"sig{sig}"
            (self.state / "xcrun.log").unlink(missing_ok=True)       # each signal's run checked on its own
            before = len(self.lines("agent-cmds"))
            proc = subprocess.Popen([sys.executable, "-B", str(Path(u.__file__)), "run", "--simulator", UDID, "--scenario",
                                     self.scenario(scn), "--out", str(out)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            deadline = time.monotonic() + 30
            # wait for the ping that ends the tap: the long rest is then under way
            while sum(1 for c in self.cmds()[before:] if c.get("cmd") == "ping") < 2 and time.monotonic() < deadline:
                time.sleep(0.05)
            time.sleep(0.5)
            proc.send_signal(sig)
            _, err = proc.communicate(timeout=60)
            self.assertEqual(proc.returncode, code, err)
            self.cleaned_up()
            self.assertEqual(json.loads((out / "run-01" / "failed.json").read_text())["error"],
                             "terminated by " + ("SIGTERM" if sig == 15 else "SIGHUP"))


class LaunchEnvRunTest(InputBase):
    def test_the_variables_reach_the_launch_only(self):
        scn = {**s040.SCN, "launch_env": {"API_ORIGIN": "https://localhost:18443", "MODE": "a b"}}
        rc, _, err = self.run_cli(scn=scn)
        self.assertEqual(rc, 0, err)
        env_log = self.lines("xcrun-env.log")
        launch = [l for l in env_log if l.startswith("simctl launch")]
        self.assertEqual(launch, ["simctl launch | SIMCTL_CHILD_API_ORIGIN=https://localhost:18443 SIMCTL_CHILD_MODE=a b"])
        self.assertFalse([l for l in env_log if not l.startswith("simctl launch") and "SIMCTL_CHILD_" in l])
        cap = json.loads((self.root / "out" / "run-01" / "capture.json").read_text())
        self.assertEqual(cap["launch_env"], scn["launch_env"])

    def test_refused_on_android_before_anything_runs(self):
        path = self.root / "a.json"
        path.write_text(json.dumps({**s040.SCN, "package": "com.example.app", "launch_env": {"A": "b"}}))
        rc, _, err = main("run", "--serial", "emulator-x", "--scenario", str(path), "--out", str(self.root / "a"))
        self.assertEqual(rc, 2)
        self.assertIn("'launch_env' is refused on Android", err)


class InputSavedRunTest(InputBase):
    def test_summary_rows_and_doctor(self):
        out = u.doctor(None, UDID)
        check = lambda name: next(c for c in out["checks"] if c["check"] == name)
        self.assertTrue(check("input agent source (for tap and swipe)")["ok"])
        self.assertFalse(check("input agent built for this Xcode")["ok"])
        self.assertIn("not yet: it is built on the first run with a tap or a swipe", check("input agent built for this Xcode")["detail"])
        self.assertEqual(self.run_cli(scn=INPUT_SCN)[0], 0)
        out = u.doctor(None, UDID)
        self.assertTrue(next(c for c in out["checks"] if c["check"] == "input agent built for this Xcode")["ok"])
        row = u.summary([str(self.root / "out")])["runs"][0]
        self.assertEqual((row["open.sim_signature"], row["pull.sim_signature"], row["open.sim_agent_ms"]), ("T", "E", 400))
        self.assertIsInstance(row["open.sim_round_trip_ms"], int)


class DocsAndInstallTest(unittest.TestCase):
    def test_documented(self):
        root = Path(u.HERE).parent
        readme = " ".join((root / "README.md").read_text().split())
        for phrase in ("## Input on the iOS Simulator", "An accessibility read of an app can change the app it reads",
                       "a rest, one lookup, the same rest again", "dev.uiprobe.agent.uitests.xctrunner",
                       "`SIMCTL_CHILD_<NAME>=<value>`", "not a place for secrets", "On Android `launch_env` is refused",
                       "${XDG_CACHE_HOME:-$HOME/Library/Caches}/ui-probe/ios-agent/`, which is renamed to `<key>/` "
                       "with a `built.ok` marker only after a build that exited 0",
                       "SIMCTL_CHILD_*` variables already in ui-probe's own environment are not passed",
                       "A swipe lasts at most 60000 ms", "HMAC-SHA256 of the nonce keyed with the token",
                       "A request must arrive in full within 5 s of its connection",
                       "-resultBundlePath raw/agent.xcresult", "check `agent`: XCTest recorded an issue",
                       "a signal that arrives during it is held until it is done",
                       "until the agent's idle limit ends them", "A name is matched by exact equality",
                       "the second read answered with at least one event stamped after the end of the last step",
                       "none of those lies where the previous picture was flat", "pathlen:0",
                       "permitting DNS:localhost only", "The CA's key is deleted as soon as the certificate is signed",
                       "a folder that is not empty and unmarked is refused",
                       "On a simulator the app's frame counts come from its log store",
                       "There are no lookups and no typing on a simulator in this version"):
            self.assertIn(phrase, readme, phrase)
        self.assertIn("## 0.5.0", (root / "CHANGELOG.md").read_text())
        skill = " ".join((root / "skills" / "ui-probe" / "SKILL.md").read_text().split())
        self.assertIn("There are no lookups and no typing there", skill)
        self.assertEqual(u.VERSION, "0.5.0")
        self.assertEqual(json.loads((root / ".claude-plugin" / "plugin.json").read_text())["version"], "0.5.0")

    @unittest.skipUnless(shutil.which("sh"), "no sh")
    def test_install_copies_the_agent_beside_the_scripts(self):
        with s040.tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {**os.environ, "HOME": str(root), "XDG_DATA_HOME": str(root / "data"), "PREFIX": str(root / "bin"),
                   "PYTHONDONTWRITEBYTECODE": "1"}
            proc = subprocess.run(["sh", str(Path(u.HERE) / "install.sh"), "--apply"], capture_output=True, text=True,
                                  env=env, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            data = root / "data" / "ui-probe" / u.VERSION
            for f in ("Agent.xcodeproj/project.pbxproj", "Agent.xcodeproj/xcshareddata/xcschemes/Agent.xcscheme",
                      "AgentUITests/AgentUITests.swift"):
                self.assertEqual((data / "ios-agent" / f).read_bytes(), (u.AGENT_DIR / f).read_bytes(), f)
            proc = subprocess.run([sys.executable, "-B", "-c", f"import sys; sys.path.insert(0, {str(data)!r}); "
                                   "import ui_probe; print(ui_probe.AGENT_DIR)"], capture_output=True, text=True, env=env)
            self.assertEqual(proc.stdout.strip(), str((data / "ios-agent").resolve()))


if __name__ == "__main__":
    unittest.main()
