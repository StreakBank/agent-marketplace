"""0.7.0, after the certifying run of 2026-11-11: at a border between a rest and an input step (the launch
counts as one), in either order, the INPUT step takes the zone of width u that the clock cannot place, at
most half of the rest's length (with its gap). On an Android 14 emulator `input tap` returns in about 0.01 s,
so a tap's own request can follow its start mark by tens of milliseconds; under the earlier rule (the rest
took the zone) it was counted in the rest before the tap and failed it. Paths are made up."""
import sys

sys.dont_write_bytecode = True

import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import test_analysis as ta  # noqa: E402
import test_requests_070 as t70  # noqa: E402
import ui_probe as u  # noqa: E402

request_line = t70.request_line
h = t70.host


def clock_within(rtt):
    """A mapping whose uncertainty is rtt (half of each of two round trips) plus 0.01 s."""
    clock, why = u.android_request_clock(t70.probe_group(4890.0 + 100.0, 100.0, rtt=rtt),
                                         t70.probe_group(4890.0 + 130.0, 130.0, rtt=rtt))
    assert why is None, why
    return clock


def count(steps):
    return {s["id"]: sorted(k.split(" ", 1)[1] for k in s["requests"]["by_path"]) for s in steps}


class LiveCaseTest(t70.Segment):
    """tmp/sb120/runs/and-certR-20261111T212004Z/signed-in-tabs-at-rest/run-01, rebuilt from its numbers: u =
    0.062 s, each tap's own request reaching the front 10 to 26 ms after the tap's start mark."""

    SCN = {"name": "x", "package": ta.PKG, "steps": [
        {"id": "rest-a", "do": "rest", "ms": 5000}, {"id": "open-b", "do": "tap", "x": 0.3, "y": 0.95},
        {"id": "rest-b", "do": "rest", "ms": 5000}, {"id": "open-c", "do": "tap", "x": 0.6, "y": 0.95},
        {"id": "rest-c", "do": "rest", "ms": 5000}]}
    MARKS = {"launch.start": 101.0, "rest-a.start": 105.0, "rest-a.end": 110.0, "open-b.start": 110.4,
             "open-b.end": 113.4, "rest-b.start": 113.7, "rest-b.end": 118.7, "open-c.start": 119.1,
             "open-c.end": 122.1, "rest-c.start": 122.4, "rest-c.end": 127.4}

    def test_a_taps_own_request_10_ms_after_its_start_mark_is_the_taps(self):
        clock = clock_within(0.052)
        self.assertAlmostEqual(clock["uncertainty_s"], 0.062)
        start = u.log_position(self.log)
        self.append(request_line(h(102.0), "/api/v1/home")
                    + request_line(h(110.41), "/api/v1/screens/b/sync")       # 10 ms after open-b.start
                    + request_line(h(119.126), "/api/v1/screens/c/sync"))     # 26 ms after open-c.start
        u.save_requests(self.raw, self.log, start, (h(101.0), h(127.4)), clock, None)
        steps = [{"id": "launch", "start_s": 101.0, "end_s": 105.0}, {"id": "rest-a", "start_s": 105.0, "end_s": 110.0},
                 {"id": "open-b", "start_s": 110.4, "end_s": 113.4}, {"id": "rest-b", "start_s": 113.7, "end_s": 118.7},
                 {"id": "open-c", "start_s": 119.1, "end_s": 122.1}, {"id": "rest-c", "start_s": 122.4, "end_s": 127.4}]
        result = {}
        u.request_counts(self.raw, steps, result, u.ANDROID, self.SCN, True, self.MARKS)
        self.assertEqual(count(steps), {"launch": ["/api/v1/home"], "rest-a": [], "open-b": ["/api/v1/screens/b/sync"],
                                        "rest-b": [], "open-c": ["/api/v1/screens/c/sync"], "rest-c": []})
        self.assertEqual((steps[1]["requests"]["in_gap"], steps[3]["requests"]["in_gap"]), (0, 0))
        # and the gate passes both rests (default rest_requests_max 0)
        rows = {s["id"]: s for s in steps}

        def gated(sid, rec):
            fails = []
            u.gate_requests(lambda check, step, msg, **kw: fails.append((check, step)), [], result, sid, rec,
                            next(s for s in self.SCN["steps"] if s["id"] == sid), dict(u.GATE_DEFAULTS), rest=True)
            return fails
        for sid in ("rest-a", "rest-b", "rest-c"):
            self.assertEqual(gated(sid, rows[sid]), [], sid)
        # control: the same rest holding the tap's request fails, as it did in the certifying run
        held = {**rows["rest-a"], "requests": {**rows["rest-a"]["requests"], "count": 1,
                                               "by_path": {"GET /api/v1/screens/b/sync": 1}}}
        self.assertEqual(gated("rest-a", held), [("rest-requests", "rest-a")])


class BothOrdersTest(t70.Segment):
    """The input step's window reaches u into the rest before it and u into the rest after it; between two rests
    and between two input steps the mark is the border."""

    SCN = {"name": "x", "package": ta.PKG, "steps": [
        {"id": "r1", "do": "rest", "ms": 2000}, {"id": "r2", "do": "rest", "ms": 2000},
        {"id": "t1", "do": "tap", "x": 0.5, "y": 0.5}, {"id": "t2", "do": "key", "code": 4},
        {"id": "r3", "do": "rest", "ms": 2000}]}
    MARKS = {"launch.start": 101.0, "r1.start": 105.0, "r1.end": 107.0, "r2.start": 107.0, "r2.end": 109.0,
             "t1.start": 109.5, "t1.end": 111.0, "t2.start": 111.0, "t2.end": 112.0, "r3.start": 112.0, "r3.end": 114.0}
    STEPS = [{"id": "launch", "start_s": 101.0, "end_s": 105.0}, {"id": "r1", "start_s": 105.0, "end_s": 107.0},
             {"id": "r2", "start_s": 107.0, "end_s": 109.0}, {"id": "t1", "start_s": 109.5, "end_s": 111.0},
             {"id": "t2", "start_s": 111.0, "end_s": 112.0}, {"id": "r3", "start_s": 112.0, "end_s": 114.0}]

    def test_the_windows(self):
        clock = clock_within(0.09)                            # u = 0.1
        w = u.request_windows(self.raw, [dict(s) for s in self.STEPS], u.ANDROID, clock, self.SCN, self.MARKS)
        approx = lambda a, b: self.assertAlmostEqual(a, b, places=6)
        approx(w["launch"][1], h(105.0) + 0.1)                # the launch is an input step: it takes the zone
        approx(w["r1"][0], h(105.0) + 0.1)
        approx(w["r1"][1], h(107.0))                          # two rests: the mark
        approx(w["r2"][0], h(107.0))
        approx(w["r2"][1], h(109.5) - 0.1)                    # rest then input: the rest's end (its gap's end) less u
        approx(w["t1"][0], h(109.5) - 0.1)
        self.assertEqual((w["r2"][2], w["r2"][3]), (h(109.0), 0.5))      # the gap itself is as measured
        approx(w["t1"][1], h(111.0))                          # two input steps: the mark
        approx(w["t2"][0], h(111.0))
        approx(w["t2"][1], h(112.0) + 0.1)                    # input then rest: the step's end plus u
        approx(w["r3"][0], h(112.0) + 0.1)
        approx(w["r3"][1], h(114.0) + 0.1)                    # the last step is a rest: widened outward
        spans = sorted((lo, hi) for lo, hi, _, _ in w.values())
        self.assertTrue(all(a[1] <= b[0] + 1e-9 for a, b in zip(spans, spans[1:])), spans)

    def test_a_first_rest_is_widened_outward(self):
        steps = [dict(s) for s in self.STEPS]
        steps[0] = {"id": "launch", "start_s": None, "end_s": None}      # no launch window: r1 is the first placed
        w = u.request_windows(self.raw, steps, u.ANDROID, clock_within(0.09), self.SCN, self.MARKS)
        self.assertAlmostEqual(w["r1"][0], h(105.0) - 0.1)
        self.assertEqual(w["launch"][:2], (None, None))

    def test_the_simulator_is_unchanged(self):
        # u = 0 there: every border is the mark (request_windows of the simulator reads its step files)
        clock = {**clock_within(0.09), "uncertainty_s": 0.0}
        w = u.request_windows(self.raw, [dict(s) for s in self.STEPS], u.ANDROID, clock, self.SCN, self.MARKS)
        self.assertAlmostEqual(w["r2"][1], h(109.5))
        self.assertAlmostEqual(w["t1"][0], h(109.5))
        self.assertAlmostEqual(w["t2"][1], h(112.0))
        self.assertAlmostEqual(w["r3"][0], h(112.0))


class ShortRestTest(t70.Segment):
    """Two input steps around a rest of 0.3 s (0.2 s and a 0.1 s gap) with u = 0.19 s: each takes at most half
    the rest, so they meet in its middle and never cross; every request is counted once."""

    SCN = {"name": "x", "package": ta.PKG, "steps": [
        {"id": "t1", "do": "tap", "x": 0.5, "y": 0.5}, {"id": "r", "do": "rest", "ms": 1000},
        {"id": "t2", "do": "tap", "x": 0.5, "y": 0.6}]}
    MARKS = {"launch.start": 101.0, "t1.start": 105.0, "t1.end": 110.0, "r.start": 110.0, "r.end": 110.2,
             "t2.start": 110.3, "t2.end": 115.0}

    def test_they_meet_in_the_middle(self):
        clock = clock_within(0.18)
        self.assertAlmostEqual(clock["uncertainty_s"], 0.19)
        start = u.log_position(self.log)
        self.append("".join(request_line(h(t), f"/r{n}") for n, t in enumerate((102.0, 110.05, 110.149, 110.25, 113.0))))
        u.save_requests(self.raw, self.log, start, (h(101.0), h(115.0)), clock, None)
        steps = [{"id": "launch", "start_s": 101.0, "end_s": 105.0}, {"id": "t1", "start_s": 105.0, "end_s": 110.0},
                 {"id": "r", "start_s": 110.0, "end_s": 110.2}, {"id": "t2", "start_s": 110.3, "end_s": 115.0}]
        result = {}
        u.request_counts(self.raw, steps, result, u.ANDROID, self.SCN, True, self.MARKS)
        self.assertEqual(count(steps), {"launch": ["/r0"], "t1": ["/r1", "/r2"], "r": [], "t2": ["/r3", "/r4"]})
        block = result["requests"]
        self.assertEqual(sum(s["requests"]["count"] for s in steps) + block["between_steps"] + block["in_margins"],
                         block["requests"])
        w = u.request_windows(self.raw, steps, u.ANDROID, clock, self.SCN, self.MARKS)
        self.assertAlmostEqual(w["t1"][1], h(110.15))
        self.assertAlmostEqual(w["r"][0], h(110.15))
        self.assertAlmostEqual(w["r"][1], h(110.15))
        self.assertAlmostEqual(w["t2"][0], h(110.15))
        self.assertAlmostEqual(w["launch"][1], h(105.0))      # launch and tap: two input steps, the mark


class DocsTest(unittest.TestCase):
    def test_documented(self):
        root = Path(u.HERE).parent
        readme = " ".join((root / "README.md").read_text().split())
        for phrase in ("the input step takes the zone", "about 1 to 2 % of the rest",
                       "`input tap` returned in about 0.01 s on an Android 14 emulator"):
            self.assertIn(phrase, readme, phrase)
        self.assertNotIn("the rest takes the zone", readme)
        self.assertNotIn("at least 0.3 s after its step's start mark", readme)
        changelog = " ".join((root / "CHANGELOG.md").read_text().split())
        self.assertIn("the input step takes the zone", changelog)
        self.assertNotIn("at least 0.3 s after its step's start mark", changelog)
        self.assertIn("A rest borders an input step", " ".join((root / "PROVENANCE.md").read_text().split()))


if __name__ == "__main__":
    unittest.main()
