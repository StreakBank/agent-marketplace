"""0.7.0: `gate.periodic_requests`, requests an app sends on a timer at any moment, set aside up to their
number in every step where a request limit or budget applies (a rest, an input step's
`expect.requests_max`, the launch's `launch_requests_max`), on Android and on the iOS Simulator. The
requests here are made up. No device, no simulator."""
import sys

sys.dont_write_bytecode = True

import copy
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import test_analysis as ta  # noqa: E402
import test_requests_070 as t70  # noqa: E402
import test_simulator_040 as s040  # noqa: E402
import ui_probe as u  # noqa: E402

SYNC = "GET /api/v1/x/sync"
FEED = "GET /api/v1/feed"
PERIODIC = {SYNC: 1}


def rec(by_path, count=None):
    return {"count": sum(by_path.values()) if count is None else count, "by_path": dict(by_path)}


class Platform:
    """A run and its scenario with a launch, a rest and a tap, on one platform; `counts` gives each step's
    request record (None: unknown)."""

    def __init__(self, platform):
        self.platform = platform

    def gate(self, counts, periodic=PERIODIC, tap_budget=None, launch_budget=None, rest_allow=None, reason="x"):
        if self.platform == u.ANDROID:
            scn = copy.deepcopy(t70.ANDROID_SCN)          # idle (rest), open (tap)
            rest_id, tap_id = "idle", "open"
            run = t70.android_run()
        else:
            scn = {"name": "Sim", "package": s040.BUNDLE, "steps": [{"id": "rest-a", "do": "rest", "ms": 5000},
                                                                     {"id": "tap", "do": "tap", "x": 0.5, "y": 0.5}]}
            rest_id, tap_id = "rest-a", "tap"
            run = s040.sim_run()
            run["steps"].append({"id": "tap", "kind": "tap", "pid": s040.PID, "process_missing": False,
                                 "process_restarted": False})
            run["requests"] = {"known": True, "reason": None, "requests": 0, "clock": {}}
        if periodic is not None:
            scn["gate"] = {"periodic_requests": dict(periodic)}
        if tap_budget is not None:
            scn["steps"][1]["expect"] = {"requests_max": tap_budget}
        if launch_budget is not None:
            scn["launch_requests_max"] = launch_budget
        if rest_allow is not None:
            scn["steps"][0]["allow"] = {"requests_max": rest_allow, "reason": "a heartbeat"}
        u.validate_scenario(scn, self.platform)
        ids = {"launch": "launch", "rest": rest_id, "tap": tap_id}
        for st in run["steps"]:
            name = next(k for k, v in ids.items() if v == st["id"])
            given = counts.get(name)
            if given is None:
                st["requests"] = {"count": None, "reason": reason}
            else:
                st["requests"] = rec(*given) if isinstance(given, tuple) else rec(given)
        opts = {**u.GATE_DEFAULTS, **(scn.get("gate") or {})}
        res = (u.gate_run if self.platform == u.ANDROID else u.gate_sim_run)(run, scn, opts)
        return res, ids


def request_checks(res, sid):
    return [v for v in res["violations"] if v["check"] in ("requests", "rest-requests") and v["step"] == sid]


def periodic_notes(res, sid):
    return [n for n in res["notes"] if n.startswith(f"{sid}: ") and "periodic kind set aside" in n]


PLATFORMS = (Platform(u.ANDROID), Platform(u.SIM_PLATFORM))
QUIET = {"launch": {}, "rest": {}, "tap": {}}


class EveryStepTest(unittest.TestCase):
    def test_a_periodic_request_passes_each_kind_of_step_with_a_note(self):
        for p in PLATFORMS:
            for where, kw in (("rest", {}), ("tap", {"tap_budget": 0}), ("launch", {"launch_budget": 0})):
                counts = {**QUIET, where: {SYNC: 1}}
                res, ids = p.gate(counts, **kw)
                sid = ids[where]
                self.assertEqual(request_checks(res, sid), [], (p.platform, where))
                self.assertEqual(periodic_notes(res, sid),
                                 [f"{sid}: 1 request of a periodic kind set aside (GET /api/v1/x/sync x1), by the "
                                  "scenario's gate block (periodic_requests)"], (p.platform, where))

    def test_what_remains_is_held(self):
        for p in PLATFORMS:
            res, ids = p.gate({**QUIET, "rest": {FEED: 1, SYNC: 2}})
            v = request_checks(res, ids["rest"])[0]
            self.assertEqual(v["message"], "2 requests sent at rest (GET /api/v1/feed x1, GET /api/v1/x/sync x2; 1 of a "
                                           "periodic kind set aside: GET /api/v1/x/sync x1), allowed 0", p.platform)
            self.assertEqual((v["observed"], v["set_aside"]), (2, {SYNC: 1}))
            res, ids = p.gate({**QUIET, "tap": {FEED: 1, SYNC: 1}}, tap_budget=0)
            v = request_checks(res, ids["tap"])[0]
            self.assertEqual(v["message"], "1 request (GET /api/v1/feed x1, GET /api/v1/x/sync x1; 1 of a periodic kind "
                                           "set aside: GET /api/v1/x/sync x1), approved at most 0", p.platform)
            self.assertEqual((v["observed"], v["allowed"], v["set_aside"]), (1, 0, {SYNC: 1}))
            res, ids = p.gate({**QUIET, "launch": {FEED: 2, SYNC: 1}}, launch_budget=1)
            v = request_checks(res, "launch")[0]
            self.assertEqual(v["message"], "2 requests (GET /api/v1/feed x2, GET /api/v1/x/sync x1; 1 of a periodic kind "
                                           "set aside: GET /api/v1/x/sync x1), approved at most 1", p.platform)
            self.assertEqual(periodic_notes(res, "launch"), [])            # it failed: no note

    def test_under_the_budget_with_a_request_of_its_own(self):
        """A step that sends the periodic request itself (a refresh that includes it) has one of its own set
        aside too: its budget is the count that remains."""
        for p in PLATFORMS:
            res, ids = p.gate({**QUIET, "tap": {FEED: 1, SYNC: 2}}, tap_budget=2)
            self.assertEqual(request_checks(res, ids["tap"]), [], p.platform)
            res, ids = p.gate({**QUIET, "tap": {FEED: 1, SYNC: 2}}, tap_budget=1)
            self.assertEqual(request_checks(res, ids["tap"])[0]["observed"], 2, p.platform)

    def test_unknown_still_fails(self):
        for p in PLATFORMS:
            res, ids = p.gate({"launch": None, "rest": None, "tap": None}, tap_budget=5, launch_budget=5,
                              reason="the front was told to stop during the run")
            for sid in (ids["rest"], ids["tap"], "launch"):
                self.assertIn("the front was told to stop during the run", request_checks(res, sid)[0]["message"],
                              (p.platform, sid))
                self.assertEqual(periodic_notes(res, sid), [])

    def test_a_cut_by_path_is_unknown_wherever_a_limit_applies(self):
        """run.json keeps a step's 20 most frequent paths; when they do not add up to its count a periodic
        request may be among those cut, so the step fails as unknown rather than guess."""
        cut = ({f"GET /p/{chr(97 + n)}": 2 for n in range(20)}, 41)
        for p in PLATFORMS:
            for where, kw in (("rest", {}), ("tap", {"tap_budget": 50}), ("launch", {"launch_budget": 50})):
                res, ids = p.gate({**QUIET, where: cut}, **kw)
                self.assertEqual([v["message"] for v in request_checks(res, ids[where])],
                                 ["requests of a periodic kind could not be set aside: the run kept only the 20 most "
                                  "frequent paths of this step's 41 requests, so a periodic one may be among those cut"],
                                 (p.platform, where))
                # without the map the count alone decides, as before
                res, ids = p.gate({**QUIET, where: cut}, periodic=None, **kw)
                self.assertFalse(any("could not be set aside" in v["message"] for v in request_checks(res, ids[where])))

    def test_no_limit_and_no_budget_no_note(self):
        for p in PLATFORMS:
            res, ids = p.gate({**QUIET, "tap": {SYNC: 1}, "launch": {SYNC: 1}})       # no budget on the tap or the launch
            self.assertEqual(periodic_notes(res, ids["tap"]), [], p.platform)
            self.assertEqual(periodic_notes(res, "launch"), [], p.platform)

    def test_with_the_steps_allowance(self):
        res, _ = PLATFORMS[0].gate({**QUIET, "rest": {SYNC: 2, FEED: 1}}, rest_allow=2)
        self.assertEqual(request_checks(res, "idle"), [])
        self.assertIn("idle: 2 requests sent at rest, above the limit of 0 that would otherwise apply; passed on the "
                      "step's allowance of 2 (a heartbeat)", res["notes"])
        self.assertEqual(len(periodic_notes(res, "idle")), 1)

    def test_without_the_map_nothing_is_set_aside(self):
        for p in PLATFORMS:
            res, ids = p.gate({**QUIET, "rest": {SYNC: 1}}, periodic=None)
            self.assertEqual(request_checks(res, ids["rest"])[0]["message"], "1 request sent at rest (GET /api/v1/x/sync "
                                                                             "x1), allowed 0", p.platform)


class ValidationTest(unittest.TestCase):
    def scn(self, periodic, platform=u.ANDROID, key="periodic_requests"):
        scn = copy.deepcopy(t70.ANDROID_SCN if platform == u.ANDROID else s040.sim_scn())
        scn["gate"] = {key: periodic}
        u.validate_scenario(scn, platform)

    def test_accepted(self):
        for platform in (u.ANDROID, u.SIM_PLATFORM):
            self.scn({SYNC: 1, "POST /api/v1/:id/ping": 3, "OPTIONS /": 1}, platform)

    def test_refused(self):
        for bad in (["GET /a"], {}, "GET /a", None):
            with self.assertRaises(u.ProbeError, msg=repr(bad)) as cm:
                self.scn(bad)
            self.assertIn("'gate.periodic_requests' must be an object of requests as the log gives them",
                          str(cm.exception))
        for key in ("get /a", "GET  /a", "GET a", "GET /a b", "/a", "GET", " GET /a", "GET /a "):
            with self.assertRaises(u.ProbeError, msg=key) as cm:
                self.scn({key: 1})
            self.assertIn(f"'gate.periodic_requests' key {key!r} must be a request as the log gives it", str(cm.exception))
        for n in (0, -1, 1.5, True, "1", None):
            with self.assertRaises(u.ProbeError, msg=repr(n)) as cm:
                self.scn({SYNC: n})
            self.assertIn(f"'gate.periodic_requests.{SYNC}' must be a whole number of at least 1", str(cm.exception))

    def test_the_old_name_is_gone(self):
        with self.assertRaises(u.ProbeError) as cm:
            self.scn({SYNC: 1}, key="rest_requests_allowed")
        self.assertIn("unknown 'gate' keys ['rest_requests_allowed']", str(cm.exception))
        self.assertIn("periodic_requests", str(cm.exception))


class FromTheCaptureTest(ta.Base):
    """The map is read from the scenario kept with the run; gate.json's limits show it only when present;
    run.json keeps the raw counts."""

    def test_limits(self):
        scn = {**ta.SCN, "gate": {"periodic_requests": {SYNC: 1}}}
        cap = ta.Capture(self.root, scn=scn)
        cap.analyze()
        self.assertEqual(u.gate([str(cap.dir)], {})["runs"][0]["limits"]["periodic_requests"], {SYNC: 1})
        plain = ta.Capture(self.root, name="run-02")
        plain.analyze()
        self.assertNotIn("periodic_requests", u.gate([str(plain.dir)], {})["runs"][0]["limits"])

    def test_raw_counts_stay_raw(self):
        run = s040.sim_run()
        run["requests"] = {"known": True, "reason": None, "requests": 1, "clock": {}}
        run["steps"][0]["requests"] = rec({})
        run["steps"][1]["requests"] = rec({SYNC: 1})
        scn = {**s040.sim_scn(), "gate": {"periodic_requests": {SYNC: 1}}}
        res = u.gate_sim_run(run, scn, {**u.GATE_DEFAULTS, **scn["gate"]})
        self.assertEqual(request_checks(res, "rest-a"), [])
        self.assertEqual(run["steps"][1]["requests"], rec({SYNC: 1}))          # the gate changed nothing
        self.assertEqual(u.sim_run_row("run-01", run)["rest-a.sim_requests"], 1)


class DocsTest(unittest.TestCase):
    def test_documented(self):
        root = Path(u.HERE).parent
        readme = " ".join((root / "README.md").read_text().split())
        for phrase in ("`periodic_requests`", "of a periodic kind set aside", "the 20 most frequent paths",
                       "has one of its own set aside too"):
            self.assertIn(phrase, readme, phrase)
        for rel in ("skills/ui-probe/SKILL.md", "SHIM-TEMPLATE.md", "CHANGELOG.md"):
            self.assertIn("periodic_requests", (root / rel).read_text(), rel)
        for rel in ("README.md", "skills/ui-probe/SKILL.md", "SHIM-TEMPLATE.md", "CHANGELOG.md", "PROVENANCE.md"):
            self.assertNotIn("rest_requests_allowed", (root / rel).read_text(), rel)


if __name__ == "__main__":
    unittest.main()
