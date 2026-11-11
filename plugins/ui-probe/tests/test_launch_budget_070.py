"""0.7.0: `launch_requests_max`, a request budget on the launch step, gated as `expect.requests_max` is on a
step, on Android and on the iOS Simulator. No device, no simulator."""
import sys

sys.dont_write_bytecode = True

import copy
import json
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import test_analysis as ta  # noqa: E402
import test_requests_070 as t70  # noqa: E402
import test_simulator_040 as s040  # noqa: E402
import ui_probe as u  # noqa: E402

PATHS = {"GET /api/v1/config": 2, "GET /api/v1/feed": 1}


def android(budget=None, launch_count=3, known=True, with_log=True):
    scn = copy.deepcopy(t70.ANDROID_SCN)
    if budget is not None:
        scn["launch_requests_max"] = budget
    u.validate_scenario(scn)
    run = t70.android_run(known=known, with_log=with_log, reason="the clock was not read")
    if with_log and known:
        run["steps"][0]["requests"] = {"count": launch_count, "by_path": dict(PATHS) if launch_count == 3 else {}}
    return run, scn


def sim(budget=None, launch_count=3, known=True, with_log=True):
    scn = s040.sim_scn()
    if budget is not None:
        scn["launch_requests_max"] = budget
    u.validate_scenario(scn, u.SIM_PLATFORM)
    run = s040.sim_run()
    if with_log:
        run["requests"] = {"known": known, "reason": None if known else "the front was told to stop during the run",
                           "requests": launch_count, "clock": {}}
        for st in run["steps"]:
            st["requests"] = ({"count": launch_count if st["id"] == "launch" else 0,
                               "by_path": dict(PATHS) if st["id"] == "launch" else {}} if known else
                              {"count": None, "reason": run["requests"]["reason"]})
    return run, scn


def launch_failures(res):
    return [(v["check"], v["message"]) for v in res["violations"] if v["step"] == "launch"]


class LaunchBudgetTest(unittest.TestCase):
    def gate(self, platform, **kw):
        if platform == "android":
            run, scn = android(**kw)
            return u.gate_run(run, scn, dict(u.GATE_DEFAULTS))
        run, scn = sim(**kw)
        return u.gate_sim_run(run, scn, dict(u.GATE_DEFAULTS))

    def test_over_the_budget_fails_naming_the_paths(self):
        for platform in ("android", "sim"):
            res = self.gate(platform, budget=2)
            self.assertEqual(launch_failures(res), [("requests", "3 requests (GET /api/v1/config x2, GET /api/v1/feed x1), "
                                                                 "approved at most 2")], platform)
            v = [v for v in res["violations"] if v["step"] == "launch"][0]
            self.assertEqual((v["observed"], v["allowed"]), (3, 2))

    def test_within_the_budget_passes(self):
        for platform in ("android", "sim"):
            self.assertEqual(launch_failures(self.gate(platform, budget=3)), [], platform)

    def test_unknown_fails_with_the_reason(self):
        for platform, why in (("android", "the clock was not read"), ("sim", "the front was told to stop during the run")):
            self.assertIn(("requests", f"requests were not counted: {why}"),
                          launch_failures(self.gate(platform, budget=5, known=False)), platform)

    def test_no_request_log(self):
        for platform in ("android", "sim"):
            self.assertEqual(launch_failures(self.gate(platform, budget=5, with_log=False)),
                             [("requests", "not counted: the run was given no request log")], platform)

    def test_without_the_key_there_is_no_launch_check(self):
        for platform in ("android", "sim"):
            self.assertEqual(launch_failures(self.gate(platform, launch_count=99)), [], platform)
            self.assertEqual(launch_failures(self.gate(platform, with_log=False)), [], platform)

    def test_validation(self):
        for good in (0, 7):
            android(budget=good)
            sim(budget=good)
        for bad in (-1, 1.5, True, "2", None):
            for platform in (u.ANDROID, u.SIM_PLATFORM):
                scn = copy.deepcopy(t70.ANDROID_SCN if platform == u.ANDROID else s040.sim_scn())
                scn["launch_requests_max"] = bad
                with self.assertRaises(u.ProbeError) as cm:
                    u.validate_scenario(scn, platform)
                self.assertIn("'launch_requests_max' must be a whole number of at least 0", str(cm.exception))


class FromTheCaptureTest(ta.Base):
    """The budget is read from the scenario kept with the run, like every other limit."""

    def test_kept_with_the_capture(self):
        scn = {**ta.SCN, "launch_requests_max": 0}
        cap = ta.Capture(self.root, scn=scn)
        cap.analyze()
        res = cap.gate()
        self.assertEqual(launch_failures(res), [("requests", "not counted: the run was given no request log")])
        plain = ta.Capture(self.root, name="run-02")       # a capture without the key: gated as before
        plain.analyze()
        self.assertEqual(plain.gate()["verdict"], "PASS")


class DocsTest(unittest.TestCase):
    def test_documented(self):
        root = Path(u.HERE).parent
        readme = " ".join((root / "README.md").read_text().split())
        for phrase in ("`launch_requests_max`", "the budget for the launch step's request count"):
            self.assertIn(phrase, readme, phrase)
        self.assertIn("launch_requests_max", (root / "CHANGELOG.md").read_text())


if __name__ == "__main__":
    unittest.main()
