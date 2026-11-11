"""0.5.0, after the independent review: the agent's own end (an issue XCTest recorded), proofs that a
reply came from the agent, the excuse for recorded frames decided over the recording's span and on
how many pixels changed where, signpost names that match nothing, the log store read to the end, a
clean-up that a signal cannot cut short, the agent's cache, its derived data, the launch environment,
long swipes, and the three mutants of the review that survived. Stand-ins on PATH as in the other
simulator tests; no simulator, no real Xcode."""
import sys

sys.dont_write_bytecode = True

import copy
import fcntl
import json
import os
import signal
import subprocess
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import test_simulator_040 as s040  # noqa: E402
import test_input_050 as s050  # noqa: E402
import test_store_050 as s050s  # noqa: E402
import ui_probe as u  # noqa: E402

BUNDLE, UDID, PID = s040.BUNDLE, s040.UDID, s040.PID
TAP_SCN = {"name": "Sim tap", "package": BUNDLE, "steps": [
    {"id": "open", "do": "tap", "x": 0.5, "y": 0.9, "settle_ms": 100, "expect": {"signature": "T"}}]}


def input_run(**step):
    """A run of TAP_SCN that passes; keyword arguments replace fields of its tap step."""
    tap = {"id": "open", "kind": "tap", "pid": PID, "process_missing": False, "process_restarted": False,
           "window_s": 0.5, "cpu_s": 0.1, "cpu_pct": 20.0, "recorder_exit": 0, "recorder_killed": False,
           "recorder_alive_at_end": True, "app_state": 4, "agent": {"reply": {"ok": True}},
           "recording": {"frames": 3, "changed_app": 3, "changed_app_noise": 0, "identical": 0, "bars_only": 0,
                         "signature": "T"}}
    tap.update(step)
    return {"platform": u.SIM_PLATFORM, "launch": {"pid": PID, "error": None}, "host": {"load1_before": 1.0},
            "agent": {"stop": {"quit": {"ok": True}, "exit": 0, "terminated": False, "killed": False}},
            "steps": [{"id": "launch", "kind": "launch", "pid": PID, "process_missing": False,
                       "process_restarted": False}, tap]}


def parse_counts(**kw):
    """store_counts on the fixture, through the store tests' own helper."""
    t = s050s.StoreParseTest("test_a_real_answer")
    t.setUp()
    try:
        return t.counts(**kw)
    finally:
        t.tearDown()


class GateTest(unittest.TestCase):
    def gate(self, run, scn=None):
        return u.gate_sim_run(run, copy.deepcopy(scn or TAP_SCN), dict(u.GATE_DEFAULTS))

    def failed(self, res):
        return [(v["check"], v["message"]) for v in res["violations"]]

    def test_the_passing_run(self):
        res = self.gate(input_run())
        self.assertEqual(res["verdict"], "PASS", self.failed(res))

    # the review's mutants that survived

    def test_m5_an_agent_that_reported_no_state_fails_foreground(self):
        res = self.gate(input_run(app_state=None))
        self.assertEqual(self.failed(res), [("foreground", "the agent did not report which app was in front at the end "
                                                           "of 'open'")])

    def test_m6_a_signature_with_an_unusable_recording_fails_signature(self):
        for rec in (None, {"error": "the video could not be read"}):
            res = self.gate(input_run(recording=rec))
            self.assertIn(("signature", "no recording, so the signature could not be read"), self.failed(res))

    def test_m15_a_recorder_that_stopped_early_fails_an_input_step(self):
        for alive in (False, None):
            res = self.gate(input_run(recorder_alive_at_end=alive))
            self.assertEqual([c for c, _ in self.failed(res)], ["recording"])
            self.assertIn("the recorder had stopped before the window ended", self.failed(res)[0][1])

    # ruling 1: the agent's test run ended with an issue

    def test_an_agent_that_ended_with_an_issue(self):
        run = input_run()
        run["agent"]["stop"]["exit"] = 65
        res = self.gate(run)
        self.assertEqual(self.failed(res), [("agent", "the agent's test run ended with status 65: XCTest recorded an "
                                                      "issue (raw/agent.log, raw/agent.xcresult)")])

    def test_an_agent_the_tool_ended_is_a_note(self):
        for how in ("terminated", "killed"):
            run = input_run()
            run["agent"]["stop"].update({"exit": -9, how: True})
            res = self.gate(run)
            self.assertEqual(res["verdict"], "PASS", self.failed(res))
            self.assertTrue(any("the agent's test run did not end after 'quit' and was ended by the tool" in n
                                for n in res["notes"]))

    def test_no_record_of_the_agent_s_end(self):
        run = input_run()
        del run["agent"]["stop"]
        self.assertEqual([c for c, _ in self.failed(self.gate(run))], ["agent"])

    # ruling 11: variables dropped from the launch

    def test_dropped_launch_variables_are_a_note(self):
        run = input_run()
        run["launch_env_dropped"] = ["SIMCTL_CHILD_A"]
        res = self.gate(run)
        self.assertIn("SIMCTL_CHILD_A in ui-probe's own environment were not passed to the app: the scenario's "
                      "launch_env is the only source", res["notes"])


class RefinementGateTest(unittest.TestCase):
    """Ruling 2: the excuse needs zero counts over the recording's own span, a small largest change on
    the largest colour channel, few changed pixels and none where the picture was flat."""

    def gate(self, **kw):
        base = dict(frames=2, changed_app_noise=2, largest_change=14.0, changed_px=38, flat_px=0)
        base.update(kw)
        return u.gate_sim_run(s050s.sim_run(**base), s040.sim_scn(), dict(u.GATE_DEFAULTS))

    def test_excused(self):
        self.assertEqual(self.gate()["verdict"], "PASS")

    def test_held(self):
        for kw in ({"span": {"drawables": 1, "commits": 0, "updates": 0}},
                   {"span": {"drawables": 0, "commits": 0, "updates": 1}},
                   {"span": {"drawables": 0, "commits": 0, "updates": None}},
                   {"changed_px": u.SIM_REFINE_CHANGED_PX_MAX + 1},
                   {"flat_px": 1}, {"flat_px": None}, {"changed_px": None},
                   {"largest_change": u.SIM_REFINE_DELTA_MAX + 1}):
            res = self.gate(**kw)
            self.assertEqual(res["verdict"], "FAIL", kw)
            self.assertIn("rest-frames", [v["check"] for v in res["violations"]], kw)

    def test_the_change_is_measured_on_the_largest_colour_channel(self):
        import numpy as np
        import ui_video as v
        prev = np.full((40, 40, 3), 255, dtype=np.int16)
        cur = prev.copy()
        cur[20, 20, 2] = 255 - 40                     # blue alone: a change of about 4 in luma
        m = v.change_measures(prev, cur)
        self.assertEqual((m["max_delta"], m["changed_px"], m["flat_changed_px"]), (40.0, 1, None))
        cur[20, 20, 2] = 255 - 12
        cur[0:3, 0, 0] = 255 - 9
        m = v.change_measures(prev, cur)
        self.assertEqual((m["max_delta"], m["changed_px"], m["flat_changed_px"]), (12.0, 4, 4))
        edge = prev.copy()
        edge[:, 20:] = 30                             # a dark half: pixels beside the edge are not flat
        cur = edge.copy()
        cur[10, 19, :] = 255 - 12
        self.assertEqual(v.change_measures(edge, cur)["flat_changed_px"], 0)
        self.assertEqual(v.change_measures(None, cur), {"max_delta": 0.0, "changed_px": 0, "flat_changed_px": 0})

    def test_the_limits(self):
        self.assertEqual((u.SIM_REFINE_DELTA_MAX, u.SIM_REFINE_CHANGED_PX_MAX, u.SIM_REFINE_FLAT_PX_MAX), (24, 2000, 0))


class SignpostNameTest(unittest.TestCase):
    """Ruling 3: a name that occurs nowhere in the run is unknown, and a limit on it fails."""

    def test_a_name_that_matches_nothing(self):
        spec = {"subsystem": s050s.TRACE["subsystem"], "names": ["Renderer:draw", "Renderer:Draw"]}
        steps, res = parse_counts(scn={**s050s.SCN, "count_signposts": spec})
        self.assertEqual(res["log_store"]["signposts_unmatched"], ["Renderer:Draw"])
        step = steps["rest-a"]
        self.assertIsNone(step["signposts"]["Renderer:Draw"])
        self.assertIsNotNone(step["signposts"]["Renderer:draw"])
        fails = []
        u.gate_signposts(lambda check, sid, msg, **_: fails.append((check, msg)), "rest-a", step,
                         {"signposts_max": {"Renderer:Draw": 5}}, spec, res["log_store"])
        self.assertEqual(fails, [("signposts", "'Renderer:Draw' was not counted: it occurs nowhere in the run, launch "
                                               "included. A name is matched by exact equality with the signpost's name "
                                               "or its message")])


class StoreEndTest(unittest.TestCase):
    """Ruling 4: the store is known only if a second read of every process holds an event after the end
    of the last step."""

    def test_known_only_with_an_event_after_the_end(self):
        self.assertTrue(parse_counts(tail=(0, 3))[1]["log_store"]["known"])
        for tail, said in (((0, 0), "found no event after the end of the last step, so"),
                           ((1, 0), "(it did not answer)"), (None, "(it did not answer)")):
            steps, res = parse_counts(tail=tail)
            self.assertFalse(res["log_store"]["known"], tail)
            self.assertIn(said, res["log_store"]["reason"])
            self.assertTrue(all(s.get("drawables") is None for sid, s in steps.items() if sid != "launch"))


class RunTest(s050.InputBase):
    def test_an_issue_xctest_recorded(self):
        """Ruling 1 and 10: the command's reply says what XCTest recorded, the agent's status fails the run,
        and its derived data and result bundle are kept."""
        (self.state / "agent.mode").write_text("issue")
        rc, _, err = self.run_cli(scn=s050.INPUT_SCN)
        self.assertEqual(rc, 0, err)
        run = self.root / "out" / "run-01"
        stop = json.loads((run / "raw" / "agent.json").read_text())["stop"]
        self.assertEqual((stop["exit"], stop["terminated"], stop["killed"]), (65, False, False))
        self.assertTrue((run / "raw" / "agent-derived-data").is_dir())
        self.assertTrue((run / "raw" / "agent.xcresult").is_dir())
        rc, text, _ = self.gate()
        self.assertEqual(rc, 1)
        self.assertIn("FAIL [input] open: the agent did not perform the tap: XCTest recorded: Failed to synthesize event",
                      text)
        self.assertIn("FAIL [agent] -: the agent's test run ended with status 65: XCTest recorded an issue", text)

    def test_a_reply_without_proof_is_not_the_agent_s(self):
        """Ruling 7: the first ping too."""
        (self.state / "agent.mode").write_text("noproof")
        rc, _, err = self.run_cli(scn=s050.INPUT_SCN)
        self.assertEqual(rc, 2)
        self.assertIn("the input agent answered but refused the run's first command: the reply was not proven to come "
                      "from the agent (no keyed hash of the command's nonce)", err)
        self.cleaned_up()

    def test_a_proof_for_another_nonce_is_refused(self):
        agent = u.SimAgent.__new__(u.SimAgent)
        agent.token = "t" * 32
        self.assertNotEqual(agent.proof("a"), agent.proof("b"))
        self.assertEqual(len(agent.proof("a")), 64)

    def test_the_launch_takes_its_variables_from_the_scenario_only(self):
        """Ruling 11."""
        scn = {**s040.SCN, "launch_env": {"MODE": "x"}}
        with mock.patch.dict(os.environ, {"SIMCTL_CHILD_LEAK": "1", "SIMCTL_CHILD_MODE": "inherited"}):
            rc, _, err = self.run_cli(scn=scn)
        self.assertEqual(rc, 0, err)
        launch = [l for l in self.lines("xcrun-env.log") if l.startswith("simctl launch")]
        self.assertEqual(launch, ["simctl launch | SIMCTL_CHILD_MODE=x"])
        run = self.root / "out" / "run-01"
        cap = json.loads((run / "capture.json").read_text())
        self.assertEqual(cap["launch_env_dropped"], ["SIMCTL_CHILD_LEAK", "SIMCTL_CHILD_MODE"])
        rc, text, _ = self.gate()
        self.assertIn("SIMCTL_CHILD_LEAK, SIMCTL_CHILD_MODE in ui-probe's own environment were not passed to the app",
                      text)

    def test_the_store_read_to_the_end(self):
        """Ruling 4: what the second read asked and found is saved; without an event after the end the
        counts are unknown."""
        self.spec_store({"launch": {"drawables": 3}})
        rc, _, err = self.run_cli(scn=s050s.SCN)
        self.assertEqual(rc, 0, err)
        run = self.root / "out" / "run-01"
        tail = json.loads((run / "raw" / "signposts-tail.json").read_text())
        self.assertEqual(tail["asked"][:5], ["simctl", "spawn", UDID, "log", "show"])
        self.assertNotIn("--predicate", tail["asked"])
        self.assertEqual((tail["exit"], tail["events"], tail["events_after_end"]), (0, 3, 3))
        self.assertTrue(json.loads((run / "run.json").read_text())["log_store"]["known"])
        rc, text, _ = self.gate()
        self.assertIn("frames counted by: recording, log store", text)
        (self.state / "tail.mode").write_text("none")
        rc, _, err = self.run_cli(scn=s050s.SCN, out="out2")
        res = json.loads((self.root / "out2" / "run-01" / "run.json").read_text())
        self.assertFalse(res["log_store"]["known"])
        self.assertIn("found no event after the end of the last step", res["log_store"]["reason"])
        rc, text, _ = self.gate(out="out2")
        self.assertIn("frames counted by: recording)", text)
        (self.state / "tail.mode").write_text("old")                  # events, but none after the end
        rc, _, err = self.run_cli(scn=s050s.SCN, out="out3")
        res = json.loads((self.root / "out3" / "run-01" / "run.json").read_text())
        self.assertEqual(res["log_store"]["tail"]["events"], 3)
        self.assertEqual(res["log_store"]["tail"]["events_after_end"], 0)
        self.assertFalse(res["log_store"]["known"])

    def spec_store(self, counts, **more):
        (self.state / "store.spec.json").write_text(json.dumps({"counts": counts, **more}))

    def test_events_while_the_recorder_stopped_hold_the_frames(self):
        """Ruling 2(a): a drawable inside the recording but after the CPU window: the frames are not excused."""
        self.video["rest-a"] = s040.block("rest-a", frames=2, region=0, real=0, largest=14.0, changed_px=38, flat_px=0)
        self.spec_store({"launch": {"drawables": 3}}, span_counts={"rest-a": {"drawables": 1}})
        rc, _, err = self.run_cli(scn=s050s.SCN)
        self.assertEqual(rc, 0, err)
        run = self.root / "out" / "run-01"
        step = json.loads((run / "raw" / "step-rest-a.json").read_text())
        self.assertLess(step["rec_wall_start"], step["wall_before"] + 1e-3)
        self.assertGreater(step["rec_wall_end"], step["wall_after"])
        res = json.loads((run / "run.json").read_text())
        st = {s["id"]: s for s in res["steps"]}
        self.assertEqual(st["rest-a"]["drawables"], 0)
        self.assertEqual(st["rest-a"]["recording_span"], {"drawables": 1, "commits": 0, "updates": 0})
        rc, text, _ = self.gate()
        self.assertEqual(rc, 1, text)
        self.assertIn("FAIL [rest-frames] rest-a", text)

    def test_a_signal_during_the_clean_up_does_not_cut_it_short(self):
        """Ruling 5: SIGTERM and SIGINT while the agent is being stopped (it ignores quit and SIGTERM, so
        stopping it takes the full waits): every clean-up step still runs, and the run ends terminated."""
        (self.state / "agent.mode").write_text("deaf")
        scn = copy.deepcopy(s050.INPUT_SCN)
        scn["steps"][1]["ms"] = 1000          # the subprocess has the real minimum (patched lower in this one)
        scn_path = self.scenario(scn)
        out = self.root / "sig"
        (self.state / "run.dir").write_text(str(out / "run-01"))
        proc = subprocess.Popen([sys.executable, "-B", str(Path(u.__file__)), "run", "--simulator", UDID, "--scenario",
                                 scn_path, "--out", str(out)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 60
        while not any(c.get("cmd") == "quit" for c in self.cmds()) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(any(c.get("cmd") == "quit" for c in self.cmds()),
                        proc.poll() is not None and proc.communicate()[1])
        time.sleep(0.3)
        proc.send_signal(signal.SIGTERM)
        time.sleep(0.3)
        proc.send_signal(signal.SIGINT)
        _, err = proc.communicate(timeout=90)
        self.assertEqual(proc.returncode, 143, err)
        self.cleaned_up()
        self.assertTrue(self.gone("agent-grandchild-pids"))
        stop = json.loads((out / "run-01" / "raw" / "agent.json").read_text())["stop"]
        self.assertEqual((stop["terminated"], stop["killed"]), (True, True))
        self.assertEqual(json.loads((out / "run-01" / "failed.json").read_text())["error"], "terminated by SIGTERM")

    def test_a_signal_while_the_status_bar_is_cleared(self):
        """Ruling 5: the status bar's clear, the last part of the clean-up, is not cut short either."""
        (self.state / "statusbar.clear.sleep").write_text("2")
        scn = copy.deepcopy(s040.SCN)
        for st in scn["steps"]:
            st["ms"] = max(st.get("ms", 0), 1000)        # the subprocess has the real minimum
        out = self.root / "sigbar"
        (self.state / "run.dir").write_text(str(out / "run-01"))
        proc = subprocess.Popen([sys.executable, "-B", str(Path(u.__file__)), "run", "--simulator", UDID, "--scenario",
                                 self.scenario(scn), "--out", str(out)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.monotonic() + 60
        while f"simctl status_bar {UDID} clear" not in self.log() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertIn(f"simctl status_bar {UDID} clear", self.log())
        time.sleep(0.5)
        proc.send_signal(signal.SIGTERM)
        _, err = proc.communicate(timeout=60)
        self.assertEqual(proc.returncode, 143, err)
        self.assertTrue((self.state / "statusbar-cleared").exists())

    def test_a_clean_up_step_that_raises_does_not_stop_the_next(self):
        """Ruling 5: each phase runs whatever the one before it raised."""
        ran = []

        def boom():
            ran.append("boom")
            raise OSError("disk full")
        u.run_phases(boom, lambda: ran.append("next"), lambda: ran.append("last"))
        self.assertEqual(ran, ["boom", "next", "last"])

    def test_the_shield_holds_a_signal_until_the_clean_up_is_done(self):
        done = []
        with self.assertRaises(u.Terminated):
            with u.CleanupShield():
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(0.05)
                done.append("after the signal")
        self.assertEqual(done, ["after the signal"])
        with self.assertRaises(KeyboardInterrupt):
            with u.CleanupShield():
                os.kill(os.getpid(), signal.SIGINT)
                time.sleep(0.05)


class CacheTest(s050.InputBase):
    """Ruling 8: "built" means the marker; a build goes into a temporary folder renamed into place; a lock
    serialises builds of one key."""

    def folder(self):
        return u.agent_cache_root() / u.agent_cache_key(u.xcode_version())

    def test_a_folder_without_the_marker_is_built_again(self):
        folder = self.folder()
        products = folder / "Build" / "Products"
        products.mkdir(parents=True)
        (products / "Agent_old.xctestrun").write_text("{}")         # an unfinished build: no marker
        built = u.ensure_agent_built()
        self.assertTrue((folder / "built.ok").is_file())
        self.assertEqual(built.parent, products)
        self.assertFalse((products / "Agent_old.xctestrun").exists())
        self.assertEqual(len([l for l in self.lines("xcodebuild.log") if l.startswith("build-for-testing")]), 1)

    def test_a_build_waits_for_the_one_that_holds_the_lock(self):
        folder = self.folder()
        folder.parent.mkdir(parents=True, exist_ok=True)
        result = {}
        with open(folder.parent / f"{folder.name}.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            worker = threading.Thread(target=lambda: result.setdefault("built", u.ensure_agent_built()))
            worker.start()
            time.sleep(1.0)
            self.assertTrue(worker.is_alive())                       # waiting for the lock
            # the build the lock's holder finished
            products = folder / "Build" / "Products"
            products.mkdir(parents=True)
            (products / "Agent_other.xctestrun").write_text("{}")
            (folder / "built.ok").write_text("x")
            fcntl.flock(lock, fcntl.LOCK_UN)
        worker.join(30)
        self.assertEqual(result["built"], products / "Agent_other.xctestrun")
        self.assertEqual([l for l in self.lines("xcodebuild.log") if l.startswith("build-for-testing")], [])


class ScenarioTest(unittest.TestCase):
    """Ruling 12."""

    def test_a_swipe_longer_than_a_minute_is_refused_on_a_simulator(self):
        scn = copy.deepcopy(s050.INPUT_SCN)
        with mock.patch.object(u, "SIM_REST_MIN_MS", 100):
            scn["steps"][2]["ms"] = 60000
            u.validate_scenario(copy.deepcopy(scn), u.SIM_PLATFORM)
            scn["steps"][2]["ms"] = 60001
            with self.assertRaises(u.ProbeError) as cm:
                u.validate_scenario(copy.deepcopy(scn), u.SIM_PLATFORM)
        self.assertIn("step 'pull': a swipe on the iOS Simulator lasts at most 60000 ms", str(cm.exception))


class AgentSourceTest(unittest.TestCase):
    """Rulings 1, 6 and 7 in the agent's source (compiled with xcodebuild build-for-testing; not run here)."""

    def test_the_source(self):
        src = (u.AGENT_DIR / "AgentUITests" / "AgentUITests.swift").read_text()
        self.assertIn("override func record(_ issue: XCTIssue)", src)
        self.assertIn("issues.append(issue.compactDescription)", src)
        self.assertIn("issues = []", src)
        self.assertIn("super.record(issue)", src)
        self.assertIn('reply = ["ok": false, "error": "XCTest recorded: "', src)
        self.assertIn("let deadline = Date().addingTimeInterval(5)", src)
        self.assertIn("while running && Date().timeIntervalSince(lastCommand) <= idle", src)
        self.assertIn("HMAC<SHA256>.authenticationCode(for: Data(nonce.utf8), using: SymmetricKey(data: Data(token.utf8)))",
                      src)
        self.assertIn('reply["proof"]', src)
        self.assertNotIn("SO_RCVTIMEO", src)


if __name__ == "__main__":
    unittest.main()
