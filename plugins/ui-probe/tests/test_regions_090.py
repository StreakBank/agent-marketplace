"""0.9.0: watched rests, region allowances on a rest, and the block map they are placed with.

Frames are synthetic arrays made here. The recording analysis on Android is run with its file reads
mocked (as in test_analysis): thumbnails written by this test, full-resolution frames handed out by a
stand-in for ui_video.iter_frames. No device, no ffmpeg."""
import sys

sys.dont_write_bytecode = True

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import test_gate as tg  # noqa: E402
import test_simulator_040 as s040  # noqa: E402
import ui_probe as u  # noqa: E402
import ui_video as v  # noqa: E402

PKG = "com.example.app"


def android(steps, **top):
    return {"name": "Example", "package": PKG, "steps": steps, **top}


def refused(scn, platform=u.ANDROID):
    with unittest.TestCase().assertRaises(u.ProbeError) as ctx:
        u.validate_scenario(scn, platform)
    return str(ctx.exception)


# --------------------------------------------------------------------------------------------
# watched rests
# --------------------------------------------------------------------------------------------

class WatchedRestTest(unittest.TestCase):
    def test_a_watched_rest_of_69_s_is_refused_and_70_s_is_not(self):
        msg = refused(android([{"id": "sit", "do": "rest", "ms": 69000, "watch": True}]))
        self.assertIn("a watched rest lasts at least 70000 ms", msg)
        self.assertIn("this one lasts 69000 ms", msg)
        u.validate_scenario(android([{"id": "sit", "do": "rest", "ms": 70000, "watch": True}]))
        self.assertEqual(u.WATCH_MIN_MS, 70000)

    def test_the_same_on_the_simulator(self):
        msg = refused({**s040.SCN, "steps": [{"id": "sit", "do": "rest", "ms": 69999, "watch": True}]}, u.SIM_PLATFORM)
        self.assertIn("a watched rest lasts at least 70000 ms", msg)
        u.validate_scenario({**s040.SCN, "steps": [{"id": "sit", "do": "rest", "ms": 70000, "watch": True}]},
                            u.SIM_PLATFORM)

    def test_an_unwatched_rest_keeps_any_length(self):
        u.validate_scenario(android([{"id": "sit", "do": "rest", "ms": 5000}]))
        u.validate_scenario(android([{"id": "sit", "do": "rest", "ms": 5000, "watch": False}]))

    def test_a_scenario_may_set_its_own_minimum(self):
        u.validate_scenario(android([{"id": "sit", "do": "rest", "ms": 30000, "watch": True}], watch_min_ms=30000))
        msg = refused(android([{"id": "sit", "do": "rest", "ms": 89000, "watch": True}], watch_min_ms=90000))
        self.assertIn("a watched rest lasts at least 90000 ms (the scenario's 'watch_min_ms')", msg)

    def test_bad_values(self):
        self.assertIn("'watch' must be true or false", refused(android([{"id": "a", "do": "rest", "ms": 1, "watch": 1}])))
        for bad in (0, -5, True, "70000", 1, 9999):
            self.assertIn("'watch_min_ms' must be a number of milliseconds of at least 10000",
                          refused(android([{"id": "a", "do": "rest", "ms": 1}], watch_min_ms=bad)))
        u.validate_scenario(android([{"id": "a", "do": "rest", "ms": 10000, "watch": True}], watch_min_ms=10000))
        self.assertIn("do not apply to a 'tap' step",
                      refused(android([{"id": "a", "do": "tap", "x": 0.5, "y": 0.5, "watch": True}])))


# --------------------------------------------------------------------------------------------
# region allowances: the scenario check
# --------------------------------------------------------------------------------------------

REGION = {"region": [0.6, 0.0, 1.0, 0.2], "changed_frames_max": 80, "reason": "a countdown ticks each second"}


def with_regions(*regions, **allow):
    return android([{"id": "sit", "do": "rest", "ms": 5000, "allow": {"regions": list(regions), **allow}}])


class RegionScenarioTest(unittest.TestCase):
    def test_a_region_alone_needs_no_step_reason(self):
        u.validate_scenario(with_regions(REGION))
        u.validate_scenario(with_regions(REGION, changed_frames_max=1, reason="one toast"))

    def test_a_step_limit_still_needs_the_step_reason(self):
        self.assertIn("'allow.reason' is required", refused(with_regions(REGION, frames_max=3)))

    def test_each_region_needs_its_reason_and_limit(self):
        self.assertIn("'reason' is required", refused(with_regions({**REGION, "reason": " "})))
        no_reason = {k: x for k, x in REGION.items() if k != "reason"}
        self.assertIn("'reason' is required", refused(with_regions(no_reason)))
        no_limit = {k: x for k, x in REGION.items() if k != "changed_frames_max"}
        self.assertIn("'changed_frames_max' is required", refused(with_regions(no_limit)))
        self.assertIn("'changed_frames_max' is required", refused(with_regions({**REGION, "changed_frames_max": -1})))

    def test_the_box(self):
        for box in ([0.6, 0.0, 1.0], [0.6, 0.2, 0.5, 0.3], [0.1, 0.5, 0.2, 0.5], [0.0, 0.0, 1.2, 0.5], [0, 0, "1", 1]):
            self.assertIn("'region' must be [x0, y0, x1, y1]", refused(with_regions({**REGION, "region": box})))

    def test_list_shape_and_unknown_keys(self):
        self.assertIn("a list of 1 to 16 regions", refused(with_regions()))
        self.assertIn("a list of 1 to 16 regions", refused(with_regions(*[REGION] * 17)))
        self.assertIn("unknown keys ['scope']", refused(with_regions({**REGION, "scope": "x"})))
        self.assertIn("known: frames_max", refused(android([{"id": "sit", "do": "rest", "ms": 5,
                                                              "allow": {"zones": [], "reason": "x"}}])))


# --------------------------------------------------------------------------------------------
# the block map and where a change lies
# --------------------------------------------------------------------------------------------

def blank(h=240, w=108, value=200):
    return np.full((h, w, 3), value, dtype=np.uint8)


class BlockMapTest(unittest.TestCase):
    def test_a_block_is_24_of_1080_of_the_width(self):
        self.assertEqual(v.block_side(1080), 24)
        self.assertEqual(v.block_side(360), 8)
        self.assertEqual(v.block_side(1179), 26)
        self.assertEqual(v.block_side(10), 1)

    def test_share_and_threshold(self):
        a = blank(120, 1080)
        b = a.copy()
        b[0:24, 0:24][:, :10] = 50     # 10 of 24 columns of the first block: 41.7 %
        b[0:24, 24:48][:, :9] = 50     # 9 of 24: 37.5 %
        bm = v.block_map(v.channel_delta(a, b), (0, 120))
        self.assertEqual(bm["side"], 24)
        self.assertTrue(bm["changed"][0, 0])
        self.assertFalse(bm["changed"][0, 1])
        self.assertAlmostEqual(bm["share"][0, 1], 9 / 24)

    def test_a_pixel_counts_above_ten_levels_on_any_channel(self):
        a = blank(48, 1080)
        b = a.copy()
        b[:24, :24, 2] = 210            # 10 levels on blue: not above
        self.assertFalse(v.block_map(v.channel_delta(a, b), (0, 48))["changed"].any())
        b[:24, :24, 2] = 211
        self.assertTrue(v.block_map(v.channel_delta(a, b), (0, 48))["changed"][0, 0])

    def test_the_bars_are_the_runs_own_rows(self):
        a = blank(240, 1080)
        b = a.copy()
        b[0:20] = 0                      # only the top 20 rows: a status bar
        delta = v.channel_delta(a, b)
        self.assertTrue(v.block_map(delta, (0, 240))["changed"].any())
        self.assertFalse(v.block_map(delta, (20, 240))["changed"].any())
        self.assertEqual(v.change_places(delta, (20, 240)), [])
        self.assertEqual(v.app_rows(2400, 63 / 2400, 0), (63, 2400))
        with self.assertRaises(v.VideoError):
            v.app_rows(100, 0.6, 0.5)

    def test_an_edge_block_is_measured_over_the_pixels_it_holds(self):
        a = blank(30, 1080)              # rows 24..29 are a partial block row of 6 rows
        b = a.copy()
        b[24:30, 0:24] = 0
        bm = v.block_map(v.channel_delta(a, b), (0, 30))
        self.assertEqual(bm["share"].shape, (2, 45))
        self.assertEqual(bm["share"][1, 0], 1.0)

    def test_places_are_groups_of_touching_blocks(self):
        a = blank(240, 1080)
        b = a.copy()
        b[30:60, 30:80] = 0              # one group
        b[150:170, 900:1000] = 0         # another, far away
        places = v.change_places(v.channel_delta(a, b), (0, 240))
        self.assertEqual(sorted(places), [[30, 30, 80, 60], [900, 150, 1000, 170]])

    def test_glyphs_beside_a_solid_change_get_boxes_of_their_own(self):
        a = blank(2400, 1080)            # the minimum for a piece outside the block groups: 78 px
        b = a.copy()
        b[20:80, 900:1000] = 0           # solid: blocks reach the share
        b[150:162, 100:112] = 0          # two glyph-sized pieces elsewhere (144 px each), under the block share
        b[150:162, 140:152] = 0
        places = v.change_places(v.channel_delta(a, b), (0, 2400))
        self.assertEqual(places, [[900, 20, 1000, 80], [100, 150, 112, 162], [140, 150, 152, 162]])
        region = [{"region": [0.8, 0.0, 1.0, 0.05], "changed_frames_max": 9, "reason": "a countdown"}]
        rest, rep = v.account_regions([{"i": 7, "t_ms": 0.0, "boxes": places}], region, 1080, 2400)
        self.assertEqual((rest, rep[0]["changed_frames"]), ([7], 1))

    def test_scattered_pixels_beside_a_solid_change_are_left_out(self):
        a = blank(2400, 1080)
        b = a.copy()
        b[20:80, 900:1000] = 0                                   # a digit inside the region
        rng = np.random.default_rng(3)
        ys, xs = rng.integers(200, 2400, 300), rng.integers(0, 1080, 300)
        b[ys, xs] = 215                                          # 300 isolated pixels at 15 levels
        piece = b.copy()
        piece[1000:1011, 500:507] = 0                            # and one piece of 77 px: one under the minimum
        for frame in (b, piece):
            places = v.change_places(v.channel_delta(a, frame), (0, 2400))
            self.assertEqual(places, [[900, 20, 1000, 80]])
        region = [{"region": [0.8, 0.0, 1.0, 0.05], "changed_frames_max": 9, "reason": "a countdown"}]
        rest, rep = v.account_regions([{"i": 1, "t_ms": 0.0, "boxes": places}], region, 1080, 2400)
        self.assertEqual((rest, rep[0]["changed_frames"]), ([], 1))

    def test_a_change_below_the_block_share_is_placed_by_its_pixels(self):
        a = blank(240, 1080)
        b = a.copy()
        b[100:140, 500:502] = 0          # a caret: 2 px wide, under 40 % of any block
        delta = v.channel_delta(a, b)
        self.assertFalse(v.block_map(delta, (0, 240))["changed"].any())
        self.assertEqual(v.change_places(delta, (0, 240)), [[500, 100, 502, 140]])
        faint = a.copy()
        faint[10:12, 10:12] = 205        # 5 levels: below the level, placed by any change at all
        self.assertEqual(v.change_places(v.channel_delta(a, faint), (0, 240)), [[10, 10, 12, 12]])

    def test_region_box(self):
        self.assertEqual(v.region_box([0.5, 0.25, 0.75, 1.0], 1080, 2000), [540, 500, 810, 2000])
        self.assertEqual(v.region_box([0.1, 0.1, 0.2, 0.2], 105, 105), [10, 10, 21, 21])


class AccountRegionsTest(unittest.TestCase):
    W, H = 1000, 2000
    REGIONS = [{"region": [0.6, 0.0, 1.0, 0.1], "changed_frames_max": 5, "reason": "a countdown"},
               {"region": [0.0, 0.9, 1.0, 1.0], "changed_frames_max": 0, "reason": "a ticker"}]

    def account(self, *changes):
        frames = [{"i": n + 1, "t_ms": 1000.0 * n, "boxes": boxes} for n, boxes in enumerate(changes)]
        return v.account_regions(frames, self.REGIONS, self.W, self.H)

    def test_inside_counts_against_the_region_not_the_rest(self):
        rest, rep = self.account([[700, 50, 800, 150]], [[650, 10, 700, 60]])
        self.assertEqual(rest, [])
        self.assertEqual(rep[0]["changed_frames"], 2)
        self.assertEqual(rep[1]["changed_frames"], 0)

    def test_crossing_an_edge_counts_against_the_rest(self):
        rest, rep = self.account([[550, 50, 700, 150]], [[700, 150, 800, 250]])
        self.assertEqual(rest, [1, 2])
        self.assertEqual(rep[0]["changed_frames"], 0)

    def test_outside_counts_against_the_rest(self):
        rest, rep = self.account([[100, 1000, 200, 1100]])
        self.assertEqual(rest, [1])
        self.assertEqual([r["changed_frames"] for r in rep], [0, 0])

    def test_a_frame_with_both_counts_against_both(self):
        rest, rep = self.account([[700, 50, 800, 150], [100, 1000, 200, 1100]])
        self.assertEqual(rest, [1])
        self.assertEqual(rep[0]["changed_frames"], 1)

    def test_a_change_that_could_not_be_placed_counts_against_the_rest(self):
        rest, _ = self.account([])
        self.assertEqual(rest, [1])

    def test_whole_screen_change_counts_against_the_rest(self):
        rest, rep = self.account([[0, 0, 1000, 2000]])
        self.assertEqual(rest, [1])
        self.assertEqual(rep[0]["changed_frames"], 0)

    def test_the_report_says_when_where_how_long_and_how_often(self):
        frames = [{"i": i, "t_ms": t, "boxes": [[700, 20, 760, 80]]}
                  for i, t in ((3, 1000.0), (4, 1016.7), (5, 1033.3), (9, 2000.0), (12, 3000.0))]
        _rest, rep = v.account_regions(frames, self.REGIONS, self.W, self.H)
        r = rep[0]
        self.assertEqual((r["changed_frames"], r["first_ms"], r["last_ms"], r["times"]), (5, 1000.0, 3000.0, 3))
        self.assertEqual(r["where"], [0.7, 0.01, 0.76, 0.04])
        self.assertEqual(r["longest_ms"], 33.3)
        self.assertEqual(r["median_interval_ms"], 1000.0)
        self.assertEqual(r["reason"], "a countdown")
        self.assertEqual([b["frames"] for b in r["bursts"]], [3, 1, 1])


# --------------------------------------------------------------------------------------------
# the gate: Android, the iOS Simulator
# --------------------------------------------------------------------------------------------

def region_report(n, declared=REGION):
    return {"region": list(declared["region"]), "reason": declared["reason"],
            "changed_frames_max": declared["changed_frames_max"], "changed_frames": n,
            "first_ms": 100.0 if n else None, "last_ms": 900.0 if n else None, "where": None, "times": 1 if n else 0,
            "longest_ms": 0.0 if n else None, "median_interval_ms": None, "bursts": [], "frames": []}


class AndroidGateTest(unittest.TestCase):
    def scn(self, region=REGION, **allow):
        scn = copy.deepcopy(tg.SCN)
        scn["steps"][1]["allow"] = {"regions": [region], **allow}
        return scn

    def run_(self, real, in_region, rest, **vkw):
        vs = tg.vstep("idle", region=real, real=real)
        vs.update({"regions": [region_report(in_region)], "rest_changed_frames": rest, **vkw})
        return tg.make_run(tg.base_steps(frames=0), [vs])

    def gate(self, run, scn):
        return u.gate_run(run, scn, tg.DEFAULTS)

    def test_changes_inside_the_region_pass(self):
        res = self.gate(self.run_(real=6, in_region=6, rest=0), self.scn())
        self.assertEqual(res["verdict"], "PASS", res)
        self.assertTrue(any("region 1 [0.6, 0, 1, 0.2] changed in 6" in n for n in res["notes"]))

    def test_a_change_outside_still_fails_the_rest(self):
        res = self.gate(self.run_(real=7, in_region=6, rest=1), self.scn())
        self.assertEqual(tg.checks(res), ["rest-video"])
        self.assertIn("the screen changed at rest: 1 frames", res["violations"][0]["message"])

    def test_a_region_over_its_limit_fails(self):
        res = self.gate(self.run_(real=9, in_region=9, rest=0), self.scn({**REGION, "changed_frames_max": 8}))
        self.assertEqual(tg.checks(res), ["rest-region"])
        v_ = res["violations"][0]
        self.assertEqual((v_["observed"], v_["allowed"]), (9, 8))
        self.assertIn("allowed 8 by the region's allowance (a countdown ticks each second)", v_["message"])

    def test_a_region_with_no_report_fails(self):
        run = tg.make_run(tg.base_steps(frames=0), [tg.vstep("idle", region=0, real=0)])
        res = self.gate(run, self.scn())
        self.assertEqual(tg.checks(res), ["rest-region"])
        self.assertIn("run `ui-probe analyze`", res["violations"][0]["message"])
        res = self.gate(self.run_(real=0, in_region=0, rest=0, regions_error="no insets"), self.scn())
        self.assertIn("no insets", res["violations"][0]["message"])

    def test_a_report_for_other_regions_fails(self):
        res = self.gate(self.run_(real=2, in_region=2, rest=0), self.scn({**REGION, "region": [0, 0, 0.5, 0.5]}))
        # the report cannot be used, so every change counts against the rest as well
        self.assertEqual(tg.checks(res), ["rest-region", "rest-video"])
        self.assertIn("made for other regions", res["violations"][0]["message"])

    def test_whole_screen_change_with_and_without_the_rest_allowance(self):
        run = self.run_(real=1, in_region=0, rest=1)
        self.assertEqual(tg.checks(self.gate(run, self.scn())), ["rest-video"])
        res = self.gate(run, self.scn(changed_frames_max=1, reason="one theme change"))
        self.assertEqual(res["verdict"], "PASS", res)

    def test_without_regions_nothing_changes(self):
        run = self.run_(real=6, in_region=6, rest=0)
        res = self.gate(run, tg.SCN)
        self.assertEqual(tg.checks(res), ["rest-video"])
        self.assertIn("6 frames in the app's area", res["violations"][0]["message"])


class SimGateTest(unittest.TestCase):
    def scn(self, region=REGION, **allow):
        scn = s040.sim_scn()
        scn["steps"][0]["allow"] = {"regions": [region], **allow}
        return scn

    def gate(self, run, scn):
        return u.gate_sim_run(run, scn, dict(u.GATE_DEFAULTS))

    def run_(self, frames, in_region, rest):
        run = s040.sim_run(frames=frames, changed_app=frames)
        run["steps"][1]["recording"].update({"regions": [region_report(in_region)], "rest_changed_frames": rest})
        return run

    def test_frames_inside_the_region_leave_both_rest_counts(self):
        res = self.gate(self.run_(5, 5, 0), self.scn())
        self.assertEqual(res["verdict"], "PASS", res)

    def test_one_outside_fails_both_rest_counts(self):
        res = self.gate(self.run_(5, 4, 1), self.scn())
        self.assertEqual(sorted(tg.checks(res)), ["rest-frames", "rest-video"])
        self.assertTrue(any("4 more changed only inside the step's regions" in x["message"] for x in res["violations"]))

    def test_the_region_limit(self):
        res = self.gate(self.run_(5, 5, 0), self.scn({**REGION, "changed_frames_max": 4}))
        self.assertEqual(tg.checks(res), ["rest-region"])

    def test_the_recording_counts_pass_the_regions_on(self):
        regions = [region_report(2)]
        with mock.patch.object(v, "analyze", return_value={"steps": [{
                "id": "rest-a", "video_frames": 3, "app_region_frames": 2, "identical_frames": 0,
                "app_changed_frames_real": 2, "signature": "-", "regions": regions, "rest_changed_frames": 0,
                "rest_changed_frame_indices": []}]}) as an:
            with tempfile.TemporaryDirectory() as tmp:
                got = u.sim_recording_counts(Path(tmp) / "x.mp4", Path(tmp) / "video" / "rest-a",
                                             self.scn()["steps"][0], "Sim rest")
        self.assertEqual((got["regions"], got["rest_changed_frames"]), (regions, 0))
        self.assertEqual(an.call_args.kwargs, {"locate": "changed"})
        self.assertEqual(an.call_args.args[4]["steps"][0]["allow"], {"regions": [REGION]})


# --------------------------------------------------------------------------------------------
# the Android recording analysis places the frames it counts
# --------------------------------------------------------------------------------------------

FULL_W, FULL_H = 540, 1200          # the recording's own size
DEVICE_H = 2400                     # the device's, which the insets are given in


class AnalyzePlacesRegionsTest(unittest.TestCase):
    """ui_video.analyze on a rest with a region: frame 1 changes inside the region (top right), frame
    2 crosses its edge, frame 3 changes only the status bar. The system bars come from the run's
    insets (120 and 60 device px, so 60 and 30 rows of the recording)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        f0 = np.full((FULL_H, FULL_W, 3), 230, dtype=np.uint8)
        f1 = f0.copy()
        f1[100:160, 420:500] = 20                      # inside [0.7, 0, 1, 0.2] of the app's area
        f2 = f1.copy()
        f2[250:330, 300:420] = 20                      # from x 0.55 to 0.78, y 0.17 to 0.24: crosses the edge
        f3 = f2.copy()
        f3[0:50, 0:540] = 90                           # rows 0 to 50: inside the top bar (60 rows)
        self.full = [f0, f1, f2, f3]
        self.files = []
        from PIL import Image
        for n, f in enumerate(self.full):
            p = self.root / f"f{n:05d}.png"
            Image.fromarray(f).resize((270, 600)).save(p)
            self.files.append(p)

    def tearDown(self):
        self.tmp.cleanup()

    def analyze(self, insets=(120, 60)):
        scn = {"name": "Example", "package": PKG, "steps": [
            {"id": "sit", "do": "rest", "ms": 5000, "allow": {"regions": [
                {"region": [0.7, 0.0, 1.0, 0.2], "changed_frames_max": 5, "reason": "a countdown"}]}}]}
        marks = {"T0": 0.0, "launch.start": 0.0, "sit.start": 0.0, "sit.end": 10.0}
        video = self.root / "screen.mp4"
        video.write_bytes(b"no recorder metadata")
        frames = self.full

        def fake_iter(_video, indices, size):
            self.assertEqual(size, (FULL_W, FULL_H))
            for i in sorted(set(indices)):
                yield i, frames[i]

        with mock.patch.object(v, "probe_pts", return_value=[0.0, 1.0, 2.0, 3.0]), \
                mock.patch.object(v, "extract", return_value=self.files), \
                mock.patch.object(v, "probe_size", return_value=(FULL_W, FULL_H)), \
                mock.patch.object(v, "iter_frames", side_effect=fake_iter):
            facts = {"height": DEVICE_H, "stable_insets_top_bottom": list(insets) if insets else None}
            (self.root / "out").mkdir(exist_ok=True)
            return v.analyze(video, self.root / "out", [], marks, scn, facts)

    def test_inside_crossing_and_bars(self):
        res = self.analyze()
        sit = next(s for s in res["steps"] if s["id"] == "sit")
        self.assertEqual(sit["app_changed_frames_real"], 2)            # frame 3 is the bars only
        self.assertEqual(sit["rest_changed_frame_indices"], [2])
        self.assertEqual(sit["rest_changed_frames"], 1)
        rep = sit["regions"][0]
        self.assertEqual(rep["changed_frames"], 1)
        self.assertEqual(rep["frames"][0]["i"], 1)
        # x 420..500 of 540, y 100-60 .. 160-60 of the 1110 rows of the app's area
        self.assertEqual(rep["frames"][0]["box"], [round(420 / 540, 4), round(40 / 1110, 4),
                                                   round(500 / 540, 4), round(100 / 1110, 4)])
        self.assertEqual((sit["first_frame"], sit["last_frame"], sit["start_s"]), (1, 3, 0.0))

    def test_no_insets_no_regions(self):
        sit = next(s for s in self.analyze(insets=None)["steps"] if s["id"] == "sit")
        self.assertIn("holds no system-bar insets", sit["regions_error"])
        self.assertNotIn("regions", sit)

    def test_a_step_without_regions_decodes_nothing_at_full_resolution(self):
        scn = {"name": "Example", "package": PKG, "steps": [{"id": "sit", "do": "rest", "ms": 5000}]}
        video = self.root / "screen.mp4"
        video.write_bytes(b"x")
        with mock.patch.object(v, "probe_pts", return_value=[0.0, 1.0, 2.0, 3.0]), \
                mock.patch.object(v, "extract", return_value=self.files), \
                mock.patch.object(v, "iter_frames", side_effect=AssertionError("decoded")):
            (self.root / "out").mkdir(exist_ok=True)
            res = v.analyze(video, self.root / "out", [], {"T0": 0.0, "sit.start": 0.0, "sit.end": 10.0}, scn,
                            {"height": DEVICE_H, "stable_insets_top_bottom": [120, 60]})
        self.assertNotIn("regions", res["steps"][-1])


if __name__ == "__main__":
    unittest.main()
