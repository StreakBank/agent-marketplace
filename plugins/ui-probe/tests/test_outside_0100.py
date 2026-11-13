"""0.10.0: `scope`, a change outside the control a step touched.

Every recording here is synthetic, made in this file to be shaped like a real one: 1080 x 2400, a
frame only when the screen changes, frame times that vary (16 to 40 ms apart), and a small encoder
halo around each change (a border of pixels a few levels off and a scatter of single pixels over the
level, none of them a group at the minimum). The decoder is a stand-in that hands the analysis these
frames. No device, no ffmpeg."""
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

W, H = 1080, 2400
ROWS = (0, H)
SWITCH = (900, 600, 1000, 660)            # the control: a switch at the end of a row
ROW_A = (60, 800, 700, 860)               # two other rows of the same screen
ROW_B = (60, 1000, 700, 1060)


def screen():
    f = np.full((H, W, 3), 230, dtype=np.uint8)
    for k in range(10):
        f[400 + 200 * k:430 + 200 * k, 60:600] = 70       # lines of text
    f[SWITCH[1]:SWITCH[3], SWITCH[0]:SWITCH[2]] = 150
    return f


def paint(f, box, value, halo=True, seed=0):
    """A rectangle of that grey, with the encoder's halo around it."""
    x0, y0, x1, y1 = box
    f = f.copy()
    if halo:
        ring = f[max(0, y0 - 2):y1 + 2, max(0, x0 - 2):x1 + 2].astype(np.int16) + 4
        f[max(0, y0 - 2):y1 + 2, max(0, x0 - 2):x1 + 2] = np.clip(ring, 0, 255).astype(np.uint8)
        rng = np.random.default_rng(seed)
        ys = rng.integers(max(0, y0 - 8), min(H, y1 + 8), 12)
        xs = rng.integers(max(0, x0 - 8), min(W, x1 + 8), 12)
        f[ys, xs] = np.clip(f[ys, xs].astype(np.int16) + 14, 0, 255).astype(np.uint8)
    f[y0:y1, x0:x1] = value
    return f


def shifted(f, box, dx):
    """f with the content of box moved dx px to the right (what it uncovers is the background)."""
    x0, y0, x1, y1 = box
    g = f.copy()
    part = f[y0:y1, x0:x1].copy()
    g[y0:y1, x0:x1] = 230
    g[y0:y1, x0 + dx:x1 + dx] = part
    return g


def check(frames, times, box=SWITCH, allowed=(), screen_=False, move=8.0, came=50.0, strip=None):
    """outside_check over frames[0] (the frame before the input) and frames[1:] at times (ms)."""
    def fake_iter(_video, indices, size):
        assert size == (W, H)
        for i in sorted(set(indices)):
            yield i, frames[i]

    with mock.patch.object(v, "iter_frames", side_effect=fake_iter):
        return v.outside_check(Path("rec.mp4"), (W, H), 0, list(zip(range(1, len(frames)), times)), box, list(allowed),
                               screen_, ROWS, move, came, strip)


def flip(f):
    return paint(f, SWITCH, 60, seed=1)


class KnownCasesTest(unittest.TestCase):
    def rows_flicker(self, back_ms):
        """The switch flips; two other rows change at 16 ms and come back after back_ms."""
        before = screen()
        on = paint(paint(flip(before), ROW_A, 200, seed=2), ROW_B, 200, seed=3)
        still_on = on.copy()
        back = flip(before)
        return check([before, on, still_on, back], [16.0, 40.0, 16.0 + back_ms])

    def test_rows_that_change_for_77_ms_and_come_back_fail(self):
        res = self.rows_flicker(77)
        self.assertEqual(res["fails"], {"moved": False, "still_there": False, "slid": False, "came_back": True})
        (c,) = res["changes"]
        self.assertEqual((c["ended"], c["duration_ms"], c["below_the_hand"]), ("came back", 77.0, False))
        self.assertTrue(ROW_A[1] - 8 <= c["box_px"][1] <= ROW_A[1])      # the halo joins within 3 px
        self.assertTrue(ROW_B[3] <= c["box_px"][3] <= ROW_B[3] + 8)

    def test_the_same_at_33_ms_is_below_the_hand(self):
        res = self.rows_flicker(33)
        self.assertFalse(any(res["fails"].values()))
        self.assertEqual((res["changes"][0]["duration_ms"], res["below_the_hand"]), (33.0, 1))

    def test_a_tapped_chip_that_moves_70_px(self):
        chips = [(60 + 220 * n, 1500, 240 + 220 * n, 1560) for n in range(4)]
        before = screen()
        for n, c in enumerate(chips):
            before = paint(before, c, 120 + 30 * n, halo=False)
            before[c[1] + 20:c[1] + 40, c[0] + 20:c[0] + 120] = 20            # each chip's label
        row = (40, 1500, 1000, 1560)
        mid = shifted(before, row, 35)
        end = shifted(before, row, 70)
        res = check([before, mid, end], [24.0, 57.0], box=chips[1])
        self.assertTrue(res["fails"]["moved"])
        self.assertEqual(res["control"]["shift"], [70, 0])
        self.assertTrue(res["fails"]["slid"])
        self.assertTrue(all(abs(sl["shift_px"][0]) >= 8 for sl in res["slid"]))

    def test_a_theme_change_with_the_whole_screen_allowed(self):
        before = screen()
        dark = (255 - before).astype(np.uint8)
        res = check([before, dark], [30.0], screen_=True)
        self.assertFalse(any(res["fails"].values()), res["fails"])
        self.assertEqual(res["changes"], [])
        res = check([before, dark], [30.0])
        self.assertTrue(res["fails"]["still_there"])

    def keyboard(self):
        before = screen()
        frames, times = [before], []
        for n, top in enumerate((2200, 1900, 1600, 1500)):            # it rises over four frames
            f = before.copy()
            f[top:H] = 200
            f[top + 30:top + 60, 100:1000] = 90
            frames.append(f)
            times.append(16.0 + 33 * n)
        return frames, times

    def test_a_keyboard_rising_into_a_declared_region(self):
        frames, times = self.keyboard()
        res = check(frames, times, allowed=[(0, 1500, W, H)])
        self.assertFalse(any(res["fails"].values()), res["fails"])

    def test_a_keyboard_rising_with_no_region(self):
        frames, times = self.keyboard()
        res = check(frames, times)
        self.assertTrue(res["fails"]["still_there"])

    def test_a_caption_that_appears_under_the_control_and_stays(self):
        before = screen()
        caption = (820, 700, 1020, 730)                                # 40 px under the switch
        after = paint(flip(before), caption, 40, seed=4)
        res = check([before, after, after.copy()], [20.0, 52.0])
        self.assertTrue(res["fails"]["still_there"])
        self.assertEqual(res["changes"][0]["ended"], "still there at the step's end")
        app_h = H
        allowed = [(0.7 * W, 0.28 * app_h, W, 0.32 * app_h)]
        res = check([before, after, after.copy()], [20.0, 52.0], allowed=allowed)
        self.assertFalse(any(res["fails"].values()), res["fails"])

    def test_a_glow_just_around_the_control_is_the_controls(self):
        before = screen()
        x0, y0, x1, y1 = SWITCH
        glow = paint(before, (x0 - 14, y0 - 14, x1 + 14, y1 + 14), 180, halo=False)      # 14 px past its box
        glow[y0:y1, x0:x1] = 60
        res = check([before, glow], [20.0])
        self.assertEqual(res["changes"], [])

    def test_a_plain_switch_in_a_list_of_like_switches_did_not_move(self):
        before = screen()
        x0, y0, x1, y1 = SWITCH
        before[y0 + 200:y1 + 200, x0:x1] = 150                      # the next row's switch, alike and plain
        res = check([before, flip(before)], [18.0])
        self.assertEqual((res["fails"]["moved"], res["control"]["flat"]), (False, True))

    def test_thin_strokes_in_other_rows_for_77_ms_fail(self):
        before = screen()
        on = flip(before)
        for x in range(80, 680, 9):                                     # strokes 3 x 20 px, 9 px apart
            on[810:830, x:x + 3] = 40
            on[1010:1030, x:x + 3] = 40
        res = check([before, on, on.copy(), flip(before)], [16.0, 40.0, 93.0])
        self.assertTrue(res["fails"]["came_back"])
        self.assertEqual(res["changes"][0]["duration_ms"], 77.0)

    def test_two_noise_groups_8_px_apart_stay_apart(self):
        before = screen()
        on = flip(before)
        for x in (300, 313):                                            # 5 x 8 px each, 8 px between them
            on[1700:1708, x:x + 5] = 180                                # 50 levels: strong enough to join
        res = check([before, on, flip(before)], [16.0, 93.0])
        self.assertEqual(res["changes"], [])                            # 40 px each, under the minimum of 78
        weak = flip(before)
        for x in (300, 307):                                            # 2 px apart, but 15 levels: they do not join
            weak[1700:1708, x:x + 5] = 245
        self.assertEqual(check([before, weak, flip(before)], [16.0, 93.0])["changes"], [])

    def test_a_still_recording_changes_nothing(self):
        before = screen()
        res = check([before, before.copy(), before.copy()], [20.0, 60.0])
        self.assertEqual((res["changes"], res["slid"], res["fails"]["moved"]), ([], [], False))

    def test_the_switch_alone_and_its_halo_change_nothing_outside(self):
        before = screen()
        res = check([before, flip(before)], [18.0])
        self.assertEqual((res["changes"], res["fails"]["moved"]), ([], False))

    def test_the_strip(self):
        with tempfile.TemporaryDirectory() as tmp:
            before = screen()
            on = paint(flip(before), ROW_A, 200, seed=2)
            res = check([before, on, flip(before)], [16.0, 100.0], strip=Path(tmp) / "sheets" / "outside-tap.png")
            self.assertEqual(res["strip"], "outside-tap.png")
            self.assertTrue((Path(tmp) / "sheets" / "outside-tap.png").read_bytes().startswith(b"\x89PNG"))


class BeforeTheInputTest(unittest.TestCase):
    """The screen must be at rest in the half second before the input."""

    def prior_check(self, prior_frames):
        before = prior_frames[-1]
        frames = prior_frames + [flip(before)]
        n = len(prior_frames)

        def fake_iter(_video, indices, size):
            for i in sorted(set(indices)):
                yield i, frames[i]

        with mock.patch.object(v, "iter_frames", side_effect=fake_iter):
            return v.outside_check(Path("rec.mp4"), (W, H), n - 1, [(n, 20.0)], SWITCH, [], False, ROWS, 8.0, 50.0,
                                   prior=list(range(n)))

    def test_a_toast_fading_out_from_the_step_before(self):
        base = screen()
        toast = (240, 2000, 840, 2100)
        fading = [paint(base, toast, 40 + 60 * k, halo=False) for k in range(3)]   # 40, 100, 160: still fading
        res = self.prior_check(fading)
        self.assertEqual(res["unsettled"]["frame"], 1)
        with mock.patch.object(v, "outside_check", return_value=res):
            got = u.run_outside(Path("."), "tap", {"may_change": []}, Path("rec.mp4"), (W, H), 2, [(3, 20.0)], SWITCH,
                                "declared", ROWS, [0, 1, 2])
        self.assertIn("the screen was not at rest before the input: it changed at [240, 2000, 840, 2100] px", got["unknown"])
        self.assertIn("declare it in scope.may_change, or put a rest before this step", got["unknown"])

    def test_a_quiet_screen_before_the_input_is_judged_as_before(self):
        base = screen()
        res = self.prior_check([base, base.copy()])
        self.assertNotIn("unsettled", res)
        self.assertEqual(res["changes"], [])

    def test_the_wiring_hands_over_the_half_second_before(self):
        frames = [{"i": i, "t": t} for i, t in enumerate([8.0, 9.4, 9.6, 9.8, 10.1])]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "raw").mkdir()
            (out / "video-frames.json").write_text(json.dumps(frames))
            step = {"id": "tap", "do": "tap", "x": 0.5, "y": 0.5, "scope": {"control": [0.4, 0.4, 0.6, 0.6], "may_change": []}}
            rec = {"id": "tap"}
            with mock.patch.object(v, "probe_size", return_value=(W, H)), \
                    mock.patch.object(v, "outside_check", return_value={"box_px": [1, 2, 3, 4], "strip": None}) as oc:
                u.outside_android(out, {"steps": [step]}, {"video": {"clock": "recorder-timestamps"}}, [rec],
                                  {"width": W, "height": H, "stable_insets_top_bottom": [0, 0]},
                                  {"tap.start": 10.0, "tap.end": 11.0}, out / "raw" / "screen.mp4")
        self.assertEqual(oc.call_args.kwargs["prior"], [1, 2, 3])             # 9.4 (the last before 9.5), 9.6, 9.8


def switch(f, box, on):
    """A switch drawn in box: a track and a knob, at the right when on."""
    x0, y0, x1, y1 = box
    f[y0:y1, x0:x1] = 230
    f[y0 + 30:y1 - 30, x0 + 10:x1 - 10] = 120 if on else 205
    k = (x1 - 70, y0 + 20) if on else (x0 + 15, y0 + 20)
    f[k[1]:k[1] + 86, k[0]:k[0] + 55] = 250 if on else 245
    f[k[1] + 20:k[1] + 66, k[0] + 12:k[0] + 43] = 90                # the knob's mark
    return f


SWITCHES = [(890, 1049 + 171 * n, 1027, 1175 + 171 * n) for n in (-1, 0, 1)]   # 171 px apart, as on the emulator


class ControlMoveTest(unittest.TestCase):
    def toggled(self, others):
        before = screen()
        for box in others:
            switch(before, box, True)
        switch(before, SWITCHES[1], True)
        after = switch(before.copy(), SWITCHES[1], False)
        return check([before, after], [17.0], box=SWITCHES[1])

    def test_a_switch_toggled_among_switches_like_it_did_not_move(self):
        res = self.toggled([SWITCHES[0], SWITCHES[2]])
        self.assertFalse(res["fails"]["moved"], res["control"])
        self.assertEqual(res["changes"], [])

    def test_with_one_lookalike_only_it_did_not_move_either(self):
        res = self.toggled([SWITCHES[0]])
        self.assertFalse(res["fails"]["moved"], res["control"])
        self.assertIsNotNone(res["control"]["already_there"])

    def test_a_move_among_identical_chips_cannot_be_placed(self):
        chips = [(60 + 220 * n, 1500, 240 + 220 * n, 1560) for n in range(4)]
        before = screen()
        for c in chips:                                             # all alike: the control's look repeats
            before = paint(before, c, 120, halo=False)
            before[c[1] + 20:c[1] + 40, c[0] + 20:c[0] + 120] = 20
        end = shifted(before, (40, 1500, 1000, 1560), 70)
        res = check([before, end], [30.0], box=chips[1])
        self.assertFalse(res["fails"]["moved"], res["control"])    # a second shift matches as well
        self.assertTrue(res["fails"]["still_there"])                # its neighbours are still judged

    def test_the_scopes_join_level_reaches_the_check(self):
        with mock.patch.object(v, "outside_check", return_value={"box_px": [1, 2, 3, 4], "strip": None}) as oc:
            u.run_outside(Path("."), "tap", {"may_change": [], "join_level": 55}, Path("rec.mp4"), (W, H), 0,
                          [(1, 20.0)], SWITCH, "declared", ROWS)
        self.assertEqual(oc.call_args.kwargs["join_level"], 55)

    def test_a_chip_that_moves_70_px_still_moves(self):
        chips = [(60 + 220 * n, 1500, 240 + 220 * n, 1560) for n in range(4)]
        before = screen()
        for n, c in enumerate(chips):
            before = paint(before, c, 120 + 30 * n, halo=False)
            before[c[1] + 20:c[1] + 40, c[0] + 20 + 10 * n:c[0] + 120] = 20
        end = shifted(before, (40, 1500, 1000, 1560), 70)
        res = check([before, end], [30.0], box=chips[1])
        self.assertEqual((res["fails"]["moved"], res["control"]["shift"]), (True, [70, 0]))


def has_libx264():
    import shutil
    import subprocess
    if not shutil.which("ffmpeg"):
        return False
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return " libx264 " in out


@unittest.skipUnless(has_libx264(), "no ffmpeg with libx264")
class EncodedTextScreenTest(unittest.TestCase):
    """A dense text screen through libx264 at crf 23, a key frame every 30 frames: the encoder's noise
    must not be taken for a change outside, and a real one must still be seen. Measured here: across
    the key frame the screen holds about 168,000 pixels over 10 levels and a few dozen over 30; joining
    every pixel over 10 within 3 px makes them one group of about 168,000 px, joining only those over
    30 leaves none larger than 27 px."""

    @classmethod
    def setUpClass(cls):
        import subprocess
        from PIL import Image
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        from PIL import ImageDraw
        rng = np.random.default_rng(7)
        small = Image.new("RGB", (W // 2, H // 2), (240, 240, 240))       # lines of text, drawn at half size
        draw = ImageDraw.Draw(small)
        letters = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 "
        for y in range(20, H // 2 - 20, 12):
            draw.text((10, y), "".join(letters[i] for i in rng.integers(0, len(letters), 90)), fill=(30, 30, 30))
        text = np.asarray(small.resize((W, H), Image.LANCZOS)).copy()     # and scaled up, so its edges are soft
        text[1000:1230, 870:1050] = 240                                   # room for the switch
        text[1490:1560, 40:850] = 240                                     # a clear line for the strokes
        cls.box = (890, 1049, 1027, 1175)
        frames = []
        for i in range(100):
            f = switch(text.copy(), cls.box, on=i < 60)                # the switch flips at frame 60
            if 75 <= i < 80:                                          # strokes for 5 frames (83 ms)
                for x in range(80, 680, 9):
                    f[1510:1530, x:x + 3] = 40
            frames.append(f)
        for n, f in enumerate(frames):
            Image.fromarray(f).save(root / f"f{n:03d}.png")
        cls.video = root / "film.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-framerate", "60", "-i", str(root / "f%03d.png"), "-c:v", "libx264",
                        "-crf", "23", "-g", "30", "-keyint_min", "30", "-sc_threshold", "0", "-pix_fmt", "yuv420p",
                        str(cls.video)], check=True)
        cls.pts = v.probe_pts(cls.video)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def step(self, first, last, join_level=v.OUTSIDE_JOIN_LEVEL):
        t0 = self.pts[first]
        after = [(i, (self.pts[i] - t0) * 1000 + 16.7) for i in range(first, last)]
        prior = list(range(first - 31, first))                      # the half second before, and the frame before it
        return v.outside_check(self.video, (W, H), first - 1, after, self.box, [], False, (0, H), 8.0, 50.0,
                               prior=prior, join_level=join_level)

    def test_the_switch_flipping_is_not_unknown_and_fails_nothing(self):
        res = self.step(60, 72)                                      # frames 29 to 59 before it hold a key frame
        self.assertNotIn("unsettled", res)
        self.assertFalse(any(res["fails"].values()), res)

    def test_joining_every_pixel_would_take_the_key_frame_for_a_change(self):
        res = self.step(60, 72, join_level=10)
        self.assertIn("unsettled", res)                               # the key frame at 30 lies before the input
        self.assertGreater(res["unsettled"]["px"], 10000)

    def test_the_thin_strokes_still_fail(self):
        res = self.step(75, 95)
        self.assertNotIn("unsettled", res)
        self.assertTrue(res["fails"]["came_back"], res["changes"])
        self.assertAlmostEqual(res["changes"][0]["duration_ms"], 83.3, delta=1)


class AmbiguousSlideTest(unittest.TestCase):
    def test_a_line_like_the_lines_around_it_is_no_slide(self):
        before = screen()
        rng = np.random.default_rng(9)
        text = (rng.random((30, 540)) < 0.4) * 160                          # a textured line, repeated every 200 px
        for k in range(10):
            before[400 + 200 * k:430 + 200 * k, 60:600] = (230 - text)[..., None].astype(np.uint8)
        on = flip(before)
        on[1100:1130, 60:600] = (230 - text)[..., None].astype(np.uint8)     # the same line, between two of them
        res = check([before, on, flip(before)], [16.0, 93.0])
        self.assertEqual(res["slid"], [])
        self.assertTrue(res["fails"]["came_back"])                           # still judged by the other rules

    def test_a_real_slide_is_still_reported(self):
        chips = [(60 + 220 * n, 1500, 240 + 220 * n, 1560) for n in range(4)]
        before = screen()
        for n, c in enumerate(chips):
            before = paint(before, c, 120 + 30 * n, halo=False)
            before[c[1] + 20:c[1] + 40, c[0] + 20 + 10 * n:c[0] + 120] = 20
        end = shifted(before, (40, 1500, 1000, 1560), 70)
        res = check([before, end], [30.0], box=chips[1])
        self.assertTrue(res["fails"]["slid"])


class BoundaryTest(unittest.TestCase):
    def came_back(self, ms):
        before = screen()
        on = paint(flip(before), ROW_A, 200, seed=2)
        return check([before, on, flip(before)], [16.0, 16.0 + ms])

    def test_50_ms_fails_and_49_does_not(self):
        self.assertTrue(self.came_back(50)["fails"]["came_back"])
        res = self.came_back(49)
        self.assertFalse(res["fails"]["came_back"])
        self.assertTrue(res["changes"][0]["below_the_hand"])

    def test_the_came_back_minimum_is_the_scopes(self):
        before = screen()
        on = paint(flip(before), ROW_A, 200, seed=2)
        self.assertFalse(check([before, on, flip(before)], [16.0, 116.0], came=101)["fails"]["came_back"])

    def moved_by(self, dx, move=8.0):
        before = screen()
        box = (300, 1200, 600, 1260)
        before = paint(before, box, 90, halo=False)
        before[1215:1245, 320:420] = 10
        after = shifted(before, (280, 1200, 700, 1260), dx)
        return check([before, after], [30.0], box=box, move=move)

    def test_8_px_is_a_move_and_7_is_not(self):
        res = self.moved_by(8)
        self.assertEqual((res["fails"]["moved"], res["control"]["shift"]), (True, [8, 0]))
        self.assertFalse(self.moved_by(7)["fails"]["moved"])
        self.assertTrue(self.moved_by(7, move=7.0)["fails"]["moved"])

    def test_the_move_threshold_is_a_fraction_of_the_width(self):
        allowed, screen_, move_px, came = u.scope_geometry({"may_change": []}, 540, (0, 1200))
        self.assertEqual((move_px, came), (4.0, 50))
        _a, _s, move_px, _c = u.scope_geometry({"may_change": [], "move_px_min": 16}, 1080, (0, 2400))
        self.assertEqual(move_px, 16.0)


# --------------------------------------------------------------------------------------------
# where the control's box comes from
# --------------------------------------------------------------------------------------------

TREE = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy rotation="0">
 <node index="0" class="android.widget.FrameLayout" clickable="false" bounds="[0,0][1080,2400]">
  <node index="0" class="android.view.View" clickable="true" bounds="[0,500][1080,700]">
   <node index="0" class="android.view.View" clickable="true" bounds="[40,560][1040,690]">
    <node index="0" class="android.widget.TextView" text="Reminders" clickable="false" bounds="[60,600][400,650]"/>
   </node>
  </node>
  <node index="1" class="android.widget.TextView" text="Alone" clickable="false" bounds="[60,900][400,950]"/>
  <node index="2" class="android.view.View" clickable="true" bounds="[0,1000][1080,2400]">
   <node index="0" class="android.widget.TextView" text="Inside a big one" clickable="false" bounds="[60,1100][400,1150]"/>
  </node>
 </node>
</hierarchy>"""
LIMIT = 0.25 * 1080 * 2400


class ControlBoxTest(unittest.TestCase):
    def test_the_smallest_clickable_ancestor(self):
        self.assertEqual(u.clickable_box(TREE, [60, 600, 400, 650], LIMIT),
                         ([40, 560, 1040, 690], "the smallest clickable ancestor of the matched element"))

    def test_an_element_with_no_clickable_ancestor_is_its_own_box(self):
        box, why = u.clickable_box(TREE, [60, 900, 400, 950], LIMIT)
        self.assertEqual(box, [60, 900, 400, 950])
        self.assertIn("nothing clickable holds it", why)

    def test_a_clickable_element_over_a_quarter_of_the_app_is_unknown(self):
        # A large card's own press highlight would otherwise be judged against the label's box.
        self.assertEqual(u.clickable_box(TREE, [60, 1100, 400, 1150], LIMIT, {"text": "Inside a big one"}),
                         (None, "the clickable element is larger than a quarter of the app's area: declare "
                                "'scope.control'"))

    def test_the_search_starts_at_the_node_find_matched_not_a_wrapper(self):
        tree = """<hierarchy rotation="0">
 <node index="0" class="android.widget.FrameLayout" clickable="false" bounds="[0,0][1080,2400]">
  <node index="0" class="android.view.View" clickable="true" bounds="[0,600][1080,760]">
   <node index="0" class="android.view.View" clickable="false" bounds="[900,600][1000,660]">
    <node index="0" class="android.widget.Switch" content-desc="Reminders" clickable="true" bounds="[900,600][1000,660]"/>
   </node>
  </node>
 </node>
</hierarchy>"""
        self.assertEqual(u.clickable_box(tree, [900, 600, 1000, 660], LIMIT, {"desc": "Reminders"})[0],
                         [900, 600, 1000, 660])
        self.assertEqual(u.clickable_box(tree, [900, 600, 1000, 660], LIMIT)[0], [900, 600, 1000, 660])  # the deepest

    def test_unknown(self):
        self.assertEqual(u.clickable_box(TREE, [1, 2, 3, 4], LIMIT)[0], None)
        self.assertIn("no window tree", u.clickable_box(None, [1, 2, 3, 4], LIMIT)[1])
        self.assertIn("cannot be read", u.clickable_box("<hierarchy", [1, 2, 3, 4], LIMIT)[1])
        box, why = u.clickable_box(TREE, [0, 0, 1080, 2400], LIMIT)
        self.assertEqual(box, None)
        self.assertIn("larger than 25 %", why)

    def test_a_rehearsal_replay_carries_the_matched_frame(self):
        rehearsal = {"path": "r/rehearsal.json", "data": {"finished": "t", "screen": [402, 874], "steps": {
            "open": {"do": "tap", "center": [0.5, 0.5], "swipes": [], "match": {"frame": [20.0, 300.0, 120.0, 44.0]}}}}}
        got = u.replay_lookup(None, rehearsal, {"id": "open", "do": "tap", "find": {"text": "x"}})
        self.assertEqual((got["frame"], got["screen"]), ([20.0, 300.0, 120.0, 44.0], [402, 874]))


class WiringTest(unittest.TestCase):
    """outside_android and outside_sim hand outside_check the frames and the box they found."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        (self.out / "raw").mkdir()
        (self.out / "sheets").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def android(self, scope, step_extra=None, rec_extra=None, clock="recorder-timestamps", insets=(120, 60)):
        frames = [{"i": i, "t": t} for i, t in enumerate([9.0, 9.9, 10.03, 10.11, 10.5, 12.2])]
        (self.out / "video-frames.json").write_text(json.dumps(frames))
        (self.out / "raw" / "ui-tap.xml").write_text(TREE)
        step = {"id": "tap", "do": "tap", "find": {"text": "Reminders"}, "scope": scope, **(step_extra or {})}
        rec = {"id": "tap", "find": {"bounds_px": [60, 600, 400, 650]}, **(rec_extra or {})}
        result = {"video": {"clock": clock}}
        facts = {"width": 1080, "height": 2400, "stable_insets_top_bottom": list(insets) if insets else None}
        marks = {"tap.start": 10.0, "tap.end": 12.0}
        with mock.patch.object(v, "probe_size", return_value=(540, 1200)), \
                mock.patch.object(v, "outside_check", return_value={"box_px": [20, 280, 520, 345], "strip": None}) as oc:
            u.outside_android(self.out, {"steps": [step]}, result, [rec], facts, marks, self.out / "raw" / "screen.mp4")
        return rec["outside"], oc

    def test_android_the_tree_box_scaled_and_the_frames_around_the_input(self):
        got, oc = self.android({"may_change": []})
        args = oc.call_args.args
        self.assertEqual((args[1], args[2]), ((540, 1200), 1))                   # the last frame before 10.0 s
        self.assertEqual([(i, round(t)) for i, t in args[3]], [(2, 30), (3, 110), (4, 500)])
        self.assertEqual(args[4], [20.0, 280.0, 520.0, 345.0])                   # [40,560,1040,690] at half size
        self.assertEqual(got["box_source"], "the smallest clickable ancestor of the matched element")
        self.assertEqual(args[7], (60, 1170))                                    # the app's rows from the insets

    def test_android_a_declared_control_wins(self):
        got, oc = self.android({"control": [0.5, 0.5, 0.6, 0.6], "may_change": []})
        self.assertEqual(got["box_source"], "declared")
        self.assertEqual(oc.call_args.args[4], [270.0, 615.0, 324.0, 726.0])

    def test_android_unknowns(self):
        got, oc = self.android({"may_change": []}, clock="matched-to-platform-frames")
        self.assertIn("not aligned", got["unknown"])
        oc.assert_not_called()
        got, _ = self.android({"may_change": []}, insets=None)
        self.assertIn("insets", got["unknown"])
        got, _ = self.android({"may_change": []}, rec_extra={"find": {"bounds_px": [1, 2, 3, 4]}})
        self.assertIn("the control's box is unknown", got["unknown"])

    def test_android_an_xy_tap_without_a_control_is_unknown(self):
        step = {"id": "tap", "do": "tap", "x": 0.5, "y": 0.5, "scope": {"may_change": []}}
        (self.out / "video-frames.json").write_text(json.dumps([{"i": 0, "t": 9.0}]))
        rec = {"id": "tap"}
        with mock.patch.object(v, "probe_size", return_value=(540, 1200)):
            u.outside_android(self.out, {"steps": [step]}, {"video": {"clock": "recorder-timestamps"}}, [rec],
                              {"width": 1080, "height": 2400, "stable_insets_top_bottom": [0, 0]},
                              {"tap.start": 10.0, "tap.end": 12.0}, self.out / "raw" / "screen.mp4")
        self.assertIn("no 'scope.control' declared", rec["outside"]["unknown"])

    def test_sim_the_rehearsal_frame_in_pixels(self):
        sid = "tap"
        (self.out / "video" / sid).mkdir(parents=True)
        (self.out / "video" / sid / "video-frames.json").write_text(json.dumps([
            {"i": 0, "pts": 0.0}, {"i": 1, "pts": 0.25}, {"i": 2, "pts": 0.29}]))
        mp4 = self.out / "raw" / f"rec-{sid}.mp4"
        mp4.write_bytes(b"x")
        rec = {"recording": {"frames": 2}, "resolved": {"frame": [20.0, 300.0, 120.0, 44.0], "screen": [402, 874]}}
        step = {"id": sid, "do": "tap", "find": {"text": "x"}, "scope": {"may_change": []}}
        with mock.patch.object(v, "probe_size", return_value=(1206, 2622)), \
                mock.patch.object(v, "outside_check", return_value={"box_px": [60, 900, 420, 1032], "strip": None}) as oc:
            got = u.outside_sim(self.out, step, rec, mp4)
        args = oc.call_args.args
        self.assertEqual((args[2], [(i, round(t)) for i, t in args[3]]), (0, [(1, 250), (2, 290)]))
        self.assertEqual(args[4], [60.0, 900.0, 420.0, 1032.0])                  # 3 px a point
        self.assertEqual(got["box_source"], "the rehearsal's match")
        rec["resolved"] = {}
        with mock.patch.object(v, "probe_size", return_value=(1206, 2622)):
            self.assertIn("no rehearsal frame", u.outside_sim(self.out, step, rec, mp4)["unknown"])


# --------------------------------------------------------------------------------------------
# the scenario check and the gate
# --------------------------------------------------------------------------------------------

def refused(scope, do="tap"):
    step = {"id": "a", "do": do, "scope": scope}
    step.update({"tap": {"x": 0.5, "y": 0.5}, "key": {"code": 4}, "text": {"text": "x"}, "url": {"url": "x://y"},
                 "swipe": {"from": [0.5, 0.5], "to": [0.5, 0.1]}, "rest": {"ms": 100}}[do])
    with unittest.TestCase().assertRaises(u.ProbeError) as ctx:
        u.validate_scenario({"name": "x", "package": "com.example.app", "steps": [step]})
    return str(ctx.exception)


class ScopeKeyTest(unittest.TestCase):
    def test_accepted_on_tap_url_text_and_key(self):
        scope = {"control": [0.1, 0.1, 0.3, 0.2], "may_change": [{"region": "screen", "reason": "a theme"},
                 {"region": [0, 0.6, 1, 1], "reason": "the keyboard"}], "came_back_ms_min": 80, "move_px_min": 12}
        for do, extra in (("tap", {"x": 0.5, "y": 0.5}), ("key", {"code": 4}), ("text", {"text": "x"}),
                          ("url", {"url": "x://y"})):
            u.validate_scenario({"name": "x", "package": "com.example.app",
                                 "steps": [{"id": "a", "do": do, "scope": scope, **extra}]})

    def test_refused(self):
        self.assertIn("do not apply to a 'swipe' step", refused({"may_change": []}, "swipe"))
        self.assertIn("do not apply to a 'rest' step", refused({"may_change": []}, "rest"))
        self.assertIn("'scope.may_change' is required", refused({}))
        self.assertIn("unknown 'scope' keys ['outside']", refused({"may_change": [], "outside": 1}))
        self.assertIn("'scope.control' must be", refused({"control": [0.5, 0.5, 0.4, 0.6], "may_change": []}))
        self.assertIn("'reason' is required", refused({"may_change": [{"region": "screen"}]}))
        self.assertIn("'region' must be \"screen\"", refused({"may_change": [{"region": "all", "reason": "x"}]}))
        self.assertIn("came_back_ms_min", refused({"may_change": [], "came_back_ms_min": -1}))
        self.assertIn("move_px_min", refused({"may_change": [], "move_px_min": 0}))


def outside(**fails):
    base = {"moved": False, "still_there": False, "slid": False, "came_back": False}
    base.update(fails)
    changes = []
    if fails.get("came_back"):
        changes.append({"ended": "came back", "below_the_hand": False, "duration_ms": 77.0, "start_ms": 16.0,
                        "box_px": [60, 800, 700, 1060]})
    if fails.get("still_there"):
        changes.append({"ended": "still there at the step's end", "below_the_hand": False, "duration_ms": None,
                        "start_ms": 20.0, "box_px": [820, 700, 1020, 730]})
    return {"unknown": None, "box_source": "declared", "fails": base, "changes": changes, "move_px": 8.0,
            "came_back_ms_min": 50, "control": {"shift": [70, 0]}, "strip": "sheets/outside-open.png",
            "slid": [{"t_ms": 40.0, "shift_px": [70, 0], "box_px": [0, 1500, 1080, 1560]}] if fails.get("slid") else []}


class GateTest(unittest.TestCase):
    def scn(self, scope=True):
        scn = copy.deepcopy(tg.SCN)
        if scope:
            scn["steps"][0]["scope"] = {"control": [0.4, 0.4, 0.6, 0.6], "may_change": []}
        return scn

    def gate(self, rec_outside, scope=True):
        steps = tg.base_steps()
        if rec_outside is not ...:
            steps[1]["outside"] = rec_outside
        return u.gate_run(tg.make_run(steps, [tg.vstep("idle")]), self.scn(scope), tg.DEFAULTS)

    def test_each_rule_fails_with_one_line(self):
        res = self.gate(outside(moved=True, still_there=True, slid=True, came_back=True))
        self.assertEqual([(x["check"], x["rule"]) for x in res["violations"]],
                         [("outside", "moved"), ("outside", "came_back"), ("outside", "still_there"), ("outside", "slid")])
        self.assertIn("the control moved: its content shifted by [70, 0] px", res["violations"][0]["message"])
        self.assertIn("came back after 77 ms (the limit is 50 ms)", res["violations"][1]["message"])

    def test_a_clean_step_passes_and_below_the_hand_is_a_note(self):
        o = outside()
        o["changes"] = [{"ended": "came back", "below_the_hand": True, "duration_ms": 33.0, "start_ms": 16.0,
                         "box_px": [60, 800, 700, 1060]}]
        res = self.gate(o)
        self.assertEqual(res["verdict"], "PASS")
        self.assertTrue(any("below the hand" in n for n in res["notes"]))

    def test_unknown_fails(self):
        res = self.gate({"unknown": "the control's box is unknown: no 'scope.control' declared", "box_source": None})
        self.assertEqual([x["check"] for x in res["violations"]], ["outside"])
        self.assertIn("unknown: the control's box is unknown", res["violations"][0]["message"])
        res = self.gate(...)                                               # no outside record at all
        self.assertIn("run `ui-probe analyze`", res["violations"][0]["message"])

    def test_a_step_without_scope_is_judged_as_before(self):
        self.assertEqual(self.gate(..., scope=False), u.gate_run(tg.make_run(tg.base_steps(), [tg.vstep("idle")]),
                                                                tg.SCN, tg.DEFAULTS))
        self.assertEqual(self.gate(..., scope=False)["verdict"], "PASS")

    def test_the_simulator_gate_too(self):
        scn = {"name": "Sim", "package": s040.BUNDLE, "steps": [
            {"id": "tap", "do": "tap", "x": 0.5, "y": 0.5, "scope": {"control": [0.4, 0.4, 0.6, 0.6], "may_change": []}}]}
        run = s040.sim_run()
        rest = run["steps"][1]
        run["steps"][1] = {**rest, "id": "tap", "kind": "tap", "app_state": 4, "agent": {"reply": {"ok": True}},
                           "outside": outside(still_there=True)}
        res = u.gate_sim_run(run, scn, dict(u.GATE_DEFAULTS))
        self.assertIn("outside", [x["check"] for x in res["violations"]])


class AnalyzeRunHookTest(unittest.TestCase):
    """analyze_run calls the outside check after the recording's analysis, and only for a scenario with
    a step with scope."""

    def test_the_hook(self):
        import test_analysis as ta
        with tempfile.TemporaryDirectory() as root:
            scn = copy.deepcopy(ta.SCN)
            cap = ta.Capture(root, scn=scn, video=True)
            (cap.dir / "raw" / "screen.mp4").write_bytes(b"x")
            (cap.dir / "raw" / "sr-exit.txt").write_text("0\n")
            block = {"clock": "recorder-timestamps", "steps": []}
            with mock.patch.object(v, "analyze", return_value=block), mock.patch.object(u, "outside_android") as oa:
                u.analyze_run(cap.dir)
            oa.assert_not_called()
            scn["steps"][1]["scope"] = {"control": [0.1, 0.1, 0.2, 0.2], "may_change": []}
            with mock.patch.object(v, "analyze", return_value=block), mock.patch.object(u, "outside_android") as oa:
                u.analyze_run(cap.dir, scn)
            oa.assert_called_once()


class DocsTest(unittest.TestCase):
    def test_documented(self):
        root = Path(u.HERE).parent
        readme = " ".join((root / "README.md").read_text().split())
        for phrase in ("## A change outside the control: `scope`", "| Outside (`outside`) |", "below the hand",
                       "sheets/outside-<step>.png", "came_back_ms_min", "move_px_min"):
            self.assertIn(phrase, readme, phrase)
        self.assertIn("## 0.10.0", (root / "CHANGELOG.md").read_text())
        self.assertIn("### Validation, 0.10.0", (root / "PROVENANCE.md").read_text())
        self.assertIn("ui-probe 0.10.0", (root.parent.parent / "CHANGELOG.md").read_text())
        # the plugin's own version only; the marketplace's is never pinned here
        self.assertEqual(u.VERSION, "0.10.0")
        self.assertEqual(json.loads((root / ".claude-plugin" / "plugin.json").read_text())["version"], "0.10.0")
        self.assertEqual((v.OUTSIDE_MOVE_PX, v.OUTSIDE_CAME_BACK_MS, v.OUTSIDE_SEARCH_PX, u.CONTROL_AREA_MAX),
                         (8, 50, 300, 0.25))


if __name__ == "__main__":
    unittest.main()
