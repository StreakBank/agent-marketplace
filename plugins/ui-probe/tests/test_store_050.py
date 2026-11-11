"""0.5.0, round 9: the iOS Simulator's log store (drawables, commits, display updates and an app's own
signposts), the rest rules that use them, and sealed tests. Stand-ins on PATH for `xcrun` (its
`log show` answers from a spec, placed in the windows the run recorded), `xcodebuild` and `ps`; a
cut-down, renamed real answer of the store as a fixture. No simulator, no real Xcode."""
import sys

sys.dont_write_bytecode = True

import copy
import datetime
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import test_simulator_040 as s040  # noqa: E402  (its stand-ins and base class)
import test_input_050 as s050  # noqa: E402
import ui_probe as u  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sim-store-sample.ndjson"
FIXTURE_PID = 39368
BUNDLE, UDID, main = s040.BUNDLE, s040.UDID, s040.main
SCN = {"name": "Sim rest", "package": BUNDLE, "launch_settle_ms": 0,
       "steps": [{"id": "rest-a", "do": "rest", "ms": 100}, {"id": "rest-b", "do": "rest", "ms": 100}]}
TRACE = {"subsystem": "com.example.tracing", "names": ["Renderer:draw", "Layout:measure"]}


def ts(text):
    return datetime.datetime.strptime(text, "%Y-%m-%d %H:%M:%S.%f%z").timestamp()


def raw_dir(root, store_text, exit_code=0, pid=FIXTURE_PID, launch=None, windows=None, tail=(0, 3)):
    """A run's raw folder with a log-store answer, the second read's result (exit, events after the end),
    its launch time and its steps' windows."""
    raw = Path(root) / "raw"
    raw.mkdir(parents=True)
    (raw / "signposts.ndjson").write_text(store_text)
    (raw / "signposts.json").write_text(json.dumps({"exit": exit_code, "stderr": "log: Could not open the store",
                                                    "pid": pid}))
    if tail is not None:
        (raw / "signposts-tail.json").write_text(json.dumps({"exit": tail[0], "events": tail[1],
                                                             "events_after_end": tail[1]}))
    if launch is not None:
        (raw / "launch-time.json").write_text(json.dumps({"wall": launch}))
    for sid, (lo, hi) in (windows or {}).items():
        (raw / f"step-{sid}.json").write_text(json.dumps({"wall_before": lo, "wall_after": hi}))
    return raw


def steps_for(windows, pid=FIXTURE_PID):
    return [{"id": "launch", "kind": "launch", "pid": pid}] + [{"id": sid, "kind": "rest"} for sid in windows]


# --------------------------------------------------------------------------------------------
# reading the store
# --------------------------------------------------------------------------------------------

class StoreParseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        lines = [json.loads(l) for l in FIXTURE.read_text().splitlines()]
        events = [e for e in lines if e.get("eventType") == "signpostEvent"]
        stamps = sorted(ts(e["timestamp"]) for e in events)
        self.first, self.last = stamps[0], stamps[-1]
        # the first drawable or commit: the fixture's launch window ends just after it
        self.td = min(ts(e["timestamp"]) for e in events if e["signpostName"] in ("ClientDrawable", "Commit"))

    def tearDown(self):
        self.tmp.cleanup()

    def counts(self, text=None, exit_code=0, pid=FIXTURE_PID, launch=None, windows=None, scn=None, tail=(0, 3)):
        launch = self.first - 0.5 if launch is None else launch
        windows = windows or {"rest-a": (self.td + 0.0001, self.last + 1)}
        raw = raw_dir(self.root / str(len(list(self.root.iterdir()))), FIXTURE.read_text() if text is None else text,
                      exit_code, pid, launch, windows, tail)
        steps = steps_for(windows, pid)
        result = {}
        u.store_counts(raw, scn or {**SCN, "count_signposts": TRACE}, steps, result)
        return {s["id"]: s for s in steps}, result

    def test_a_real_answer(self):
        """The fixture (a real answer, cut down and renamed): after its first commit, 40 drawables (38 + 2 of
        a category that starts the same), 3 commits, 39 updates, and the app's own intervals by their
        message; the summary line, a log line and the intervals' ends are not counted."""
        # a launch window that holds no drawable or commit: the store is taken as unread
        st, result = self.counts(windows={"rest-a": (self.first + 0.0005, self.last + 1)})
        self.assertFalse(result["log_store"]["known"])
        st, result = self.counts()
        self.assertTrue(result["log_store"]["known"], result)
        self.assertEqual(result["log_store"]["events"], 149)
        self.assertEqual((st["rest-a"]["drawables"], st["rest-a"]["commits"], st["rest-a"]["updates"]), (40, 3, 39))
        self.assertEqual(st["rest-a"]["signposts"], {"Renderer:draw": 3, "Layout:measure": 2})

    def test_another_process_is_not_the_app(self):
        st, result = self.counts(pid=4242)
        self.assertFalse(result["log_store"]["known"])
        self.assertIn("no drawable or commit of the app between its launch and the first step", result["log_store"]["reason"])
        self.assertIsNone(st["rest-a"]["drawables"])

    def test_unknown_for_every_step_when_the_store_did_not_answer(self):
        for text, code, fragment in (("", 1, "the simulator's log store did not answer (`log show` exit 1: log: Could not "
                                                  "open the store)"),
                                     ("Filtering the log data\n", 0, "answered something that is not JSON (line 1)"),
                                     ("[1, 2]\n", 0, "not a JSON object (line 1)"),
                                     ('{"eventType": "signpostEvent", "processID": 39368}\n', 0, "without a readable time"),
                                     ('{"count": 0, "finished": 1}\n', 0, "so the store was not read")):
            st, result = self.counts(text=text, exit_code=code)
            self.assertFalse(result["log_store"]["known"], fragment)
            self.assertIn(fragment, result["log_store"]["reason"])
            self.assertEqual([st["rest-a"][k] for k in ("drawables", "commits", "updates")], [None] * 3)
            self.assertEqual(st["rest-a"]["signposts"], {"Renderer:draw": None, "Layout:measure": None})

    def test_no_read_at_all(self):
        raw = self.root / "x" / "raw"
        raw.mkdir(parents=True)
        result = {}
        steps = steps_for({"rest-a": (0, 1)})
        u.store_counts(raw, SCN, steps, result)
        self.assertEqual(result["log_store"]["reason"], "the run holds no read of the simulator's log store")

    def test_an_app_that_emits_nothing_of_the_subsystem(self):
        scn = {**SCN, "count_signposts": {"subsystem": "com.example.silent", "names": ["x"]}}
        st, result = self.counts(scn=scn)
        self.assertTrue(result["log_store"]["known"])
        self.assertFalse(result["log_store"]["signposts_known"])
        self.assertIn("the run holds no event of subsystem 'com.example.silent' from the app, launch included: is the "
                      "app's tracing switched on?", result["log_store"]["signposts_reason"])
        self.assertEqual(st["rest-a"]["signposts"], {"x": None})
        self.assertEqual(st["rest-a"]["drawables"], 40)

    def test_an_interval_counts_once_and_by_name_or_message(self):
        mk = lambda t, kind, name, msg: json.dumps(
            {"eventType": "signpostEvent", "timestamp": datetime.datetime.fromtimestamp(t).astimezone()
             .strftime("%Y-%m-%d %H:%M:%S.%f%z"), "subsystem": "com.example.tracing", "category": "app",
             "signpostName": name, "signpostType": kind, "eventMessage": msg, "processID": 7})
        commit = json.dumps({"eventType": "signpostEvent", "timestamp": datetime.datetime.fromtimestamp(100.5).astimezone()
                             .strftime("%Y-%m-%d %H:%M:%S.%f%z"), "subsystem": "com.apple.coreanimation",
                             "category": "Transaction", "signpostName": "Commit", "signpostType": "event", "processID": 7})
        text = "\n".join([commit, mk(101.2, "begin", "AppInterval", "Renderer:draw"),
                          mk(101.3, "end", "AppInterval", "Renderer:draw"), mk(101.4, "event", "Layout:measure", ""),
                          mk(101.5, "begin", "Other", "Other")]) + "\n"
        st, result = self.counts(text=text, pid=7, launch=100.0, windows={"a": (101.0, 102.0)},
                                 scn={**SCN, "count_signposts": TRACE})
        self.assertEqual(st["a"]["signposts"], {"Renderer:draw": 1, "Layout:measure": 1})

    def test_events_go_to_the_window_that_holds_them(self):
        e = lambda t, name="ClientDrawable", cat="CAMetalLayer", sub="com.apple.coreanimation": json.dumps(
            {"eventType": "signpostEvent", "timestamp": datetime.datetime.fromtimestamp(t).astimezone()
             .strftime("%Y-%m-%d %H:%M:%S.%f%z"), "subsystem": sub, "category": cat, "signpostName": name,
             "signpostType": "event", "eventMessage": "", "processID": 7})
        text = "\n".join([e(100.5), e(101.5, "Commit", "Transaction"), e(102.5), e(102.6), e(102.7, "UpdateSequence",
                          "UpdateCycle", "com.apple.UIKit"), e(103.5), e(104.0, "NotifyIdleObservers", "UpdateCycleIdleSched",
                          "com.apple.UIKit")]) + "\n"
        st, result = self.counts(text=text, pid=7, launch=100.0, windows={"a": (101.0, 102.0), "b": (102.0, 103.0),
                                                                           "c": (103.0, 104.5)}, scn=SCN)
        self.assertTrue(result["log_store"]["known"])
        self.assertEqual([(st[k]["drawables"], st[k]["commits"], st[k]["updates"]) for k in "abc"],
                         [(0, 1, 0), (2, 0, 1), (1, 0, 0)])


# --------------------------------------------------------------------------------------------
# the gate with the counts
# --------------------------------------------------------------------------------------------

def sim_run(known=True, drawables=0, commits=0, updates=0, signposts=None, largest_change=0.0, changed_px=0, flat_px=0,
            span=None, **rest):
    run = s040.sim_run(**rest)
    step = run["steps"][1]
    if step.get("recording") is not None:
        step["recording"].update({"largest_change": largest_change, "most_changed_px": changed_px,
                                  "most_flat_changed_px": flat_px})
    step["recording_span"] = (span if span is not None else {"drawables": 0, "commits": 0, "updates": 0}) if known else None
    step.update({"drawables": drawables if known else None, "commits": commits if known else None,
                 "updates": updates if known else None})
    if signposts is not None:
        step["signposts"] = signposts
    run["log_store"] = {"known": known, "reason": None if known else "the simulator's log store did not answer"}
    return run


class StoreGateTest(unittest.TestCase):
    def gate(self, run, scn=None, opts=None):
        return u.gate_sim_run(run, scn or s040.sim_scn(), {**u.GATE_DEFAULTS, **(opts or {})})

    def checks(self, res):
        return [v["check"] for v in res["violations"]]

    def test_known_counts_at_rest(self):
        self.assertEqual(self.gate(sim_run())["verdict"], "PASS")
        res = self.gate(sim_run(drawables=2, commits=1))
        self.assertEqual(self.checks(res), ["rest-frames"])
        v = res["violations"][0]
        self.assertEqual((v["observed"], v["source"]), (3, "log store"))
        self.assertIn("3 frames presented at rest (2 drawables, 1 commits; log store count), allowed 0", v["message"])
        res = self.gate(sim_run(updates=4))
        self.assertEqual(self.checks(res), ["rest-updates"])
        self.assertIn("the app was woken for 4 display updates at rest, allowed 0", res["violations"][0]["message"])

    def test_limits_and_allowances(self):
        self.assertEqual(self.gate(sim_run(updates=4), opts={"rest_updates_max": 4})["verdict"], "PASS")
        res = self.gate(sim_run(updates=4), s040.sim_scn(updates_max=5))
        self.assertEqual(res["verdict"], "PASS")
        self.assertTrue(any("passed on the step's allowance of 5" in n for n in res["notes"]))
        res = self.gate(sim_run(updates=6), s040.sim_scn(updates_max=5))
        self.assertEqual(res["violations"][0]["limit_from"], "allowance")
        self.assertEqual(self.gate(sim_run(drawables=10, updates=10, frames=10, changed_app=10),
                                   s040.sim_scn(frames_max=12, changed_frames_max=12, updates_max=12))["verdict"], "PASS")

    def test_frames_of_a_refining_encoder_are_a_note(self):
        res = self.gate(sim_run(frames=2, changed_app_noise=2, largest_change=14.0, changed_px=38, flat_px=0))
        self.assertEqual(res["verdict"], "PASS", res)
        note = next(n for n in res["notes"] if n.startswith("rest-a: 2 frames recorded at rest"))
        self.assertIn("it presented nothing by any count over the whole recording (0 drawables, 0 commits, 0 display "
                      "updates), no pixel changed by more than 14 levels on any colour channel (the limit is 24), 38 "
                      "pixels changed at most (the limit is 2000) and none where the picture was flat", note)
        self.assertIn("ui-probe zoom <run-dir> <a> <b> --step rest-a", note)
        self.assertEqual(self.gate(sim_run(frames=1, identical=1, largest_change=0.0, changed_px=0, flat_px=0))["verdict"],
                         "PASS")
        # three frames, as two clean runs recorded them (one changed, one noise, one identical): the
        # frame that changed nothing is excused with the others, not held as composed for nothing
        res = self.gate(sim_run(frames=3, changed_app=1, changed_app_noise=1, identical=1, largest_change=14.0,
                                changed_px=66, flat_px=0))
        self.assertEqual(res["verdict"], "PASS", res)
        self.assertTrue(any(n.startswith("rest-a: 3 frames recorded at rest") for n in res["notes"]), res["notes"])
        # the limit itself
        top = u.SIM_REFINE_FRAMES_MAX
        res = self.gate(sim_run(frames=top, changed_app_noise=top, largest_change=14.0, changed_px=38, flat_px=0))
        self.assertEqual(res["verdict"], "PASS", res)
        self.assertTrue(any(n.startswith(f"rest-a: {top} frames recorded at rest") for n in res["notes"]), res["notes"])

    def test_otherwise_the_frames_are_held(self):
        held = {"frames": 2, "changed_app_noise": 2, "largest_change": 14.0}
        for run, checks in ((sim_run(known=False, **held), ["rest-frames", "rest-video"]),
                            (sim_run(frames=u.SIM_REFINE_FRAMES_MAX + 1, changed_app_noise=u.SIM_REFINE_FRAMES_MAX + 1,
                                     largest_change=14.0), ["rest-frames", "rest-video"]),
                            (sim_run(frames=2, changed_app_noise=2, largest_change=24.5), ["rest-frames", "rest-video"]),
                            (sim_run(frames=2, changed_app_noise=2, largest_change=None), ["rest-frames", "rest-video"]),
                            (sim_run(updates=1, **held), ["rest-updates", "rest-frames", "rest-video"]),
                            (sim_run(drawables=1, **held), ["rest-frames", "rest-frames", "rest-video"])):
            res = self.gate(run)
            self.assertEqual(self.checks(res), checks, run["steps"][1])
            self.assertFalse(any("refining" in n for n in res["notes"]))

    def test_unknown_counts_say_why_once(self):
        res = self.gate(sim_run(known=False))
        self.assertEqual(res["verdict"], "PASS")
        self.assertEqual(len([n for n in res["notes"] if "are unknown for every step" in n]), 1)
        self.assertIn("the simulator's log store did not answer", res["notes"][1])

    def test_signposts(self):
        scn = {**s040.sim_scn(), "count_signposts": TRACE}
        scn["steps"][0]["expect"] = {"signposts_max": {"Renderer:draw": 0}}
        self.assertEqual(self.gate(sim_run(signposts={"Renderer:draw": 0, "Layout:measure": 3}), scn)["verdict"], "PASS")
        res = self.gate(sim_run(signposts={"Renderer:draw": 2, "Layout:measure": 0}), scn)
        self.assertEqual(self.checks(res), ["signposts"])
        self.assertIn("2 'Renderer:draw' signposts, approved at most 0", res["violations"][0]["message"])
        run = sim_run(signposts={"Renderer:draw": None, "Layout:measure": None})
        run["log_store"]["signposts_reason"] = "the run holds no event of subsystem 'com.example.tracing' from the app, " \
                                               "launch included: is the app's tracing switched on?"
        res = self.gate(run, scn)
        self.assertEqual(self.checks(res), ["signposts"])
        self.assertIn("'Renderer:draw' was not counted: the run holds no event of subsystem", res["violations"][0]["message"])
        self.assertIn("is the app's tracing switched on?", res["violations"][0]["message"])


# --------------------------------------------------------------------------------------------
# the scenario
# --------------------------------------------------------------------------------------------

class StoreScenarioTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(u, "SIM_REST_MIN_MS", 100)
        p.start()
        self.addCleanup(p.stop)

    def reject(self, scn, fragment, platform=u.SIM_PLATFORM):
        with self.assertRaises(u.ProbeError) as cm:
            u.validate_scenario(scn, platform)
        self.assertIn(fragment, str(cm.exception))

    def test_count_signposts(self):
        ok = {**SCN, "count_signposts": TRACE,
              "steps": [{"id": "r", "do": "rest", "ms": 100, "expect": {"signposts_max": {"Renderer:draw": 0}}},
                        {"id": "t", "do": "tap", "x": 0.5, "y": 0.5, "expect": {"signature": "T", "signposts_max":
                                                                                  {"Layout:measure": 9}}}]}
        u.validate_scenario(ok, u.SIM_PLATFORM)
        android = {**ok, "package": "com.example.app", "steps": [{"id": "r", "do": "rest", "ms": 100}]}
        self.reject(android, "'count_signposts' is refused on Android", platform=u.ANDROID)
        for cs, fragment in (([], "must be an object with 'subsystem' and 'names'"),
                             ({"subsystem": "a b", "names": ["x"]}, "'count_signposts.subsystem' must be letters"),
                             ({"subsystem": 'x" OR 1', "names": ["x"]}, "'count_signposts.subsystem' must be letters"),
                             ({"subsystem": "x", "names": []}, "'count_signposts.names' must be a list of 1 to 50"),
                             ({"subsystem": "x", "names": ["a", "a"]}, "'count_signposts.names' must be a list"),
                             ({"subsystem": "x", "names": ["a\nb"]}, "'count_signposts.names' must be a list"),
                             ({"subsystem": "x", "names": ["a"], "extra": 1}, "must be an object with")):
            self.reject({**SCN, "count_signposts": cs}, fragment)
        self.reject({**ok, "steps": [{"id": "r", "do": "rest", "ms": 100, "expect": {"signposts_max": {"Other": 0}}}]},
                    "'expect.signposts_max' names ['Other'], which 'count_signposts.names' does not list")
        self.reject({**SCN, "steps": [{"id": "r", "do": "rest", "ms": 100, "expect": {"signposts_max": {"x": 0}}}]},
                    "which 'count_signposts.names' does not list")
        self.reject({**SCN, "count_sections": ["x"]}, "'count_sections': trace sections are not recorded")

    def test_android_refuses_the_simulator_counts(self):
        base = {"name": "A", "package": "com.example.app", "steps": [{"id": "r", "do": "rest", "ms": 100}]}
        self.reject({**base, "gate": {"rest_updates_max": 1}}, "'gate.rest_updates_max' applies to the iOS Simulator only",
                    platform=u.ANDROID)
        self.reject({**base, "steps": [{"id": "r", "do": "rest", "ms": 100, "allow": {"updates_max": 1, "reason": "x"}}]},
                    "'allow.updates_max' applies to the iOS Simulator only", platform=u.ANDROID)
        self.reject({**base, "steps": [{"id": "r", "do": "rest", "ms": 100,
                                        "expect": {"signposts_max": {"x": 1}}}]},
                    "'expect.signposts_max' applies to the iOS Simulator only", platform=u.ANDROID)
        u.validate_scenario({**SCN, "gate": {"rest_updates_max": 2}, "steps": [
            {"id": "r", "do": "rest", "ms": 100, "allow": {"updates_max": 3, "reason": "a caret"}}]}, u.SIM_PLATFORM)


# --------------------------------------------------------------------------------------------
# runs: the store read once, after the last step; the counts in run.json, summary and the gate
# --------------------------------------------------------------------------------------------

class StoreRunTest(s050.InputBase):
    def spec(self, counts, signposts=None):
        spec = {"counts": counts}
        if signposts:
            spec["signposts"] = signposts
        (self.state / "store.spec.json").write_text(json.dumps(spec))

    def test_a_rest_only_run_reads_the_store_once_after_the_last_step(self):
        self.spec({"launch": {"drawables": 3, "commits": 2}, "rest-a": {}, "rest-b": {"updates": 2}})
        rc, _, err = self.run_cli(scn=SCN)
        self.assertEqual(rc, 0, err)
        log = self.log()
        reads = [l for l in log if l.startswith(f"simctl spawn {UDID} log show") and "--predicate" in l]
        self.assertEqual(len(reads), 1)
        tails = [l for l in log if l.startswith(f"simctl spawn {UDID} log show") and "--predicate" not in l]
        self.assertEqual(len(tails), 1)
        self.assertLess(log.index(reads[0]), log.index(tails[0]))
        self.assertIn("--style ndjson", tails[0])
        self.assertLess(max(i for i, l in enumerate(log) if "recordVideo" in l), log.index(reads[0]))
        self.assertLess(log.index(reads[0]), log.index(f"simctl status_bar {UDID} clear"))
        self.assertIn("--signpost --start ", reads[0])
        self.assertIn(f"--style ndjson --predicate processID == {s040.PID} AND (subsystem == \"com.apple.UIKit\" OR "
                      "subsystem == \"com.apple.coreanimation\")", reads[0])
        self.assertEqual(self.lines("xcodebuild.log"), [])
        run = self.root / "out" / "run-01"
        meta = json.loads((run / "raw" / "signposts.json").read_text())
        datetime.datetime.strptime(meta["start"], "%Y-%m-%d %H:%M:%S")
        res = json.loads((run / "run.json").read_text())
        st = {s["id"]: s for s in res["steps"]}
        self.assertEqual((st["rest-a"]["drawables"], st["rest-a"]["commits"], st["rest-a"]["updates"]), (0, 0, 0))
        self.assertEqual(st["rest-b"]["updates"], 2)
        self.assertTrue(res["log_store"]["known"])
        rc, text, _ = self.gate()
        self.assertEqual(rc, 1)
        self.assertIn("FAIL [rest-updates] rest-b: the app was woken for 2 display updates at rest, allowed 0", text)
        row = u.summary([str(self.root / "out")])["runs"][0]
        self.assertEqual((row["rest-b.sim_drawables"], row["rest-b.sim_commits"], row["rest-b.sim_updates"]), (0, 0, 2))

    def test_input_steps_and_signposts(self):
        scn = copy.deepcopy(s050.INPUT_SCN)
        scn["count_signposts"] = TRACE
        scn["steps"][1]["expect"] = {"signposts_max": {"Renderer:draw": 0}}
        self.spec({"launch": {"drawables": 5}, "open": {"drawables": 30, "updates": 30}},
                  {"subsystem": TRACE["subsystem"], "counts": {"launch": {"Renderer:draw": 4},
                                                               "open": {"Renderer:draw": 30, "Layout:measure": 2}}})
        rc, _, err = self.run_cli(scn=scn)
        self.assertEqual(rc, 0, err)
        read = next(l for l in self.log() if " log show " in l)
        self.assertIn('subsystem == "com.example.tracing"', read)
        res = json.loads((self.root / "out" / "run-01" / "run.json").read_text())
        st = {s["id"]: s for s in res["steps"]}
        self.assertEqual((st["open"]["drawables"], st["open"]["updates"], st["open"]["signposts"]),
                         (30, 30, {"Renderer:draw": 30, "Layout:measure": 2}))
        self.assertEqual(st["rest-a"]["signposts"], {"Renderer:draw": 0, "Layout:measure": 0})
        rc, text, _ = self.gate()
        self.assertEqual(rc, 0, text)                       # an input step's counts are information
        row = u.summary([str(self.root / "out")])["runs"][0]
        self.assertEqual(row["open.sim_signposts.Renderer:draw"], 30)

    def test_a_store_that_does_not_answer(self):
        (self.state / "log.rc").write_text("1")
        self.video["rest-a"] = s040.block("rest-a", frames=2, region=0, real=0, largest=14.0)
        rc, _, err = self.run_cli(scn=SCN)
        self.assertEqual(rc, 0, err)
        rc, text, _ = self.gate()
        self.assertEqual(rc, 1)
        self.assertIn("the drawables, commits and display updates are unknown for every step: the simulator's log store "
                      "did not answer (`log show` exit 1", text)
        self.assertIn("FAIL [rest-frames] rest-a: 2 frames recorded at rest", text)   # 0.4.0's rule

    def test_a_refining_encoder_at_rest_passes_with_a_note(self):
        self.spec({"launch": {"commits": 1}})
        self.video["rest-a"] = s040.block("rest-a", frames=2, region=0, real=0, largest=14.0, changed_px=38, flat_px=0)
        rc, _, err = self.run_cli(scn=SCN)
        self.assertEqual(rc, 0, err)
        rc, text, _ = self.gate()
        self.assertEqual(rc, 0, text)
        self.assertIn("note: rest-a: 2 frames recorded at rest", text)

    def test_r4c_a_build_that_fails_after_writing_its_xctestrun(self):
        (self.state / "build.mode").write_text("fail-after-writing")
        rc, _, err = self.run_cli(scn=s050.INPUT_SCN)
        self.assertEqual(rc, 2)
        self.assertIn("the input agent could not be built (xcodebuild exit 65)", err)
        self.assertFalse([l for l in self.lines("xcodebuild.log") if l.startswith("test-without-building")])


class SealedTest(s040.SimBase):
    def test_every_tool_is_a_stand_in(self):
        b = self.root / "bin"
        for tool in ("xcrun", "xcodebuild", "ps"):
            self.assertEqual(shutil.which(tool), str(b / tool), tool)
        self.assertEqual(os.environ["XDG_CACHE_HOME"], str(self.cache))
        self.assertEqual(Path.home(), self.home)
        self.assertTrue(str(u.agent_cache_root()).startswith(str(self.root)))


# --------------------------------------------------------------------------------------------
# the refinement limit on real videos, made here (a still picture refined, a spinner, a caret)
# --------------------------------------------------------------------------------------------

@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "no ffmpeg")
class RefinementCalibrationTest(unittest.TestCase):
    """The measures that decide the excuse, on lossless videos made here: a still picture refined along
    its edges (as the two real recordings were: up to 14 levels, no change where the picture is flat),
    the reviewer's 1 pt grey hairline that grows twice (largest change 16, so only the flat-area count
    holds it), a small spinner and a caret."""

    def counts(self, root, frames):
        for n, img in enumerate(frames):
            img.convert("RGB").save(root / f"f{n:02d}.png")
        out = root / "rec.mkv"
        subprocess.run(["ffmpeg", "-v", "error", "-framerate", "2", "-i", str(root / "f%02d.png"), "-c:v", "png", str(out)],
                       check=True)
        return u.sim_recording_counts(out, root / "run" / "video" / "r", "r", "calibration")

    def excused(self, rec):
        run = sim_run(frames=rec["frames"], identical=rec["identical"], changed_app=rec["changed_app"],
                      changed_app_noise=rec["changed_app_noise"], bars_only=rec["bars_only"],
                      largest_change=rec["largest_change"], changed_px=rec["most_changed_px"],
                      flat_px=rec["most_flat_changed_px"])
        return u.gate_sim_run(run, s040.sim_scn(), dict(u.GATE_DEFAULTS))["verdict"] == "PASS"

    def test_refinement_is_excused_and_small_real_changes_are_not(self):
        import math
        import numpy as np
        from PIL import Image, ImageDraw
        W, H = 1179, 2556
        base = Image.new("RGB", (W, H), (255, 255, 255))
        d = ImageDraw.Draw(base)
        for row in range(6):
            d.text((60, 400 + 120 * row), "Text on a still screen, row %d" % row, fill=(30, 30, 30))
        rng = np.random.default_rng(1)
        grey = np.asarray(base.convert("L"), dtype=np.int16)
        edges = np.argwhere(np.abs(np.diff(grey, axis=1)) > 60)          # pixels on the text's edges

        def refined(levels, n=400):
            a = np.asarray(base, dtype=np.int16).copy()
            pick = edges[rng.choice(len(edges), n, replace=False)]
            a[pick[:, 0], pick[:, 1]] = np.clip(a[pick[:, 0], pick[:, 1]] + rng.integers(-levels, levels + 1, (n, 1)),
                                                0, 255)
            return Image.fromarray(a.astype(np.uint8))

        def bar(x, colour=(229, 229, 234)):
            img = base.copy()
            ImageDraw.Draw(img).rectangle([60, 1800, 60 + x, 1802], fill=colour)
            return img

        def spinner(n):
            img = base.copy()
            dd = ImageDraw.Draw(img)
            for k in range(8):
                ang = 2 * math.pi * k / 8
                shade = int(40 + 200 * (((k - n) % 8) / 8))
                dd.line([590 + math.cos(ang) * 14, 1300 + math.sin(ang) * 14, 590 + math.cos(ang) * 30,
                         1300 + math.sin(ang) * 30], fill=(shade,) * 3, width=7)
            return img

        def caret(n):
            img = base.copy()
            if n % 2 == 0:
                ImageDraw.Draw(img).rectangle([200, 1500, 206, 1560], fill=(0, 122, 255))
            return img
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            results = {}
            for name, frames in (("refined", [base, refined(14), refined(12)]),
                                 ("hairline", [bar(300), bar(330), bar(360)]),
                                 ("spinner", [spinner(n) for n in range(3)]),
                                 ("caret", [caret(n) for n in range(3)])):
                (root / name).mkdir()
                rec = self.counts(root / name, frames)
                results[name] = (rec["largest_change"], rec["most_changed_px"], rec["most_flat_changed_px"],
                                 self.excused(rec))
        self.assertTrue(results["refined"][3], results)
        self.assertEqual(results["refined"][2], 0, results)
        self.assertLessEqual(results["hairline"][0], u.SIM_REFINE_DELTA_MAX, results)   # amplitude alone would excuse it
        self.assertGreater(results["hairline"][2], 0, results)                         # the flat-area count holds it
        for name in ("hairline", "spinner", "caret"):
            self.assertFalse(results[name][3], (name, results))


class WordsTest(unittest.TestCase):
    def test_the_wrong_claim_is_gone_and_the_new_rules_are_documented(self):
        root = Path(u.HERE).parent
        for p in root.rglob("*"):
            if p.is_file() and p.suffix in (".py", ".md") and p.name != "test_store_050.py":
                self.assertNotIn("keeps none a host can read", p.read_text(), p)
        readme = " ".join((root / "README.md").read_text().split())
        for phrase in ("`log show --signpost`", "drawables", "commits", "display updates", "\"count_signposts\": {\"subsystem\"",
                       "its recorded frames exceed its commits", "`rest_updates_max`", "`allow.updates_max`",
                       "A simulator run reads its log store once, after the last step and before the clean-up"):
            self.assertIn(phrase, readme, phrase)
        changelog = " ".join((root / "CHANGELOG.md").read_text().split("## 0.4.0")[0].split())
        self.assertIn("0.4.0 was wrong", changelog)


if __name__ == "__main__":
    unittest.main()
