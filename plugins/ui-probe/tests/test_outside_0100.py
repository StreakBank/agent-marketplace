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


def shifted(f, box, dx, ground=230):
    """f with the content of box moved dx px to the right (what it uncovers is the background)."""
    x0, y0, x1, y1 = box
    g = f.copy()
    part = f[y0:y1, x0:x1].copy()
    g[y0:y1, x0:x1] = ground
    g[y0:y1, x0 + dx:x1 + dx] = part
    return g


# Glyph-like strokes on a flat ground (0.11.0): seven-segment characters, softened by a 3 x 3 mean as a
# renderer's antialiasing softens text. A tapped control's look changes by its strokes (bolder, darker).
SEG = {"0": "abcdef", "1": "bc", "2": "abged", "3": "abgcd", "4": "fgbc", "5": "afgcd", "6": "afgedc", "7": "abc",
       "8": "abcdefg", "9": "abcdfg", "A": "abcefg", "B": "cdefg", "C": "adef", "D": "bcdeg", "E": "afged",
       "F": "afge", "H": "bcefg", "I": "ef", "K": "bcefg", "L": "def", "M": "abcefg", "N": "abcef", "O": "cdeg",
       "P": "abefg", "R": "eg", "S": "afgcd", "T": "fged", "U": "bcdef", "W": "bcdeg", "Y": "bcdfg"}


def text(f, x, y, s, w, h, t, value, gap):
    """Characters of w x h px with strokes t px wide, of that grey, gap px apart, drawn into f."""
    for k, ch in enumerate(s):
        gx, hh = x + k * (w + gap), h // 2
        parts = {"a": (y, y + t, gx, gx + w), "b": (y, y + hh, gx + w - t, gx + w), "c": (y + hh, y + h, gx + w - t, gx + w),
                 "d": (y + h - t, y + h, gx, gx + w), "e": (y + hh, y + h, gx, gx + t), "f": (y, y + hh, gx, gx + t),
                 "g": (y + hh - t // 2, y + hh - t // 2 + t, gx, gx + w)}
        for seg in SEG.get(ch, ""):
            y0, y1, x0, x1 = parts[seg]
            f[y0:y1, x0:x1] = value
    return f


def soft(f):
    g = f.astype(np.float32)
    g[1:-1, 1:-1] = sum(g[1 + dy:H - 1 + dy, 1 + dx:W - 1 + dx] for dy in (-1, 0, 1) for dx in (-1, 0, 1)) / 9
    return np.round(g).astype(np.uint8)


def plain():
    return np.full((H, W, 3), 245, dtype=np.uint8)


DAYS = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"]
CHIP_W, CHIP_H, STRIP_Y = 139, 168, 444          # a day strip's chips, as on a 1080 x 2400 emulator


def chip_box(k, y=STRIP_Y, x=53):
    return (x + CHIP_W * k, y, x + CHIP_W * (k + 1), y + CHIP_H)


def chip(f, k, y=STRIP_Y, selected=False, x=53):
    """Day chip k: a small label above a large number; selected, its strokes are bolder and darker."""
    x0, y0, _x1, _y1 = chip_box(k, y, x)
    t, value = (4, 30) if selected else (3, 120)
    text(f, x0 + 22, y0 + 22, DAYS[k], 18, 26, t, value, 10)
    return text(f, x0 + 30, y0 + 70, str(9 + k), 30, 60, t + 2, value, 12)


def day_screen(strip_y, selected, rows):
    """A header, the day strip at strip_y with chip `selected` selected, and lines of a list below it."""
    f = plain()
    text(f, 60, strip_y - 124, "MON 10 FEB", 24, 40, 4, 40, 12)
    for k in range(7):
        chip(f, k, strip_y, selected=(k == selected))
    for r in range(6):
        text(f, 60, strip_y + 256 + 160 * r, rows[: (r % 4) + 6], 22, 36, 3, 60, 11)
    return soft(f)


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
        # 0.11.0: chips told apart by their labels; chips that differ by their grey only are lookalikes to
        # a measure that ignores contrast (see test_a_move_among_identical_chips_cannot_be_placed).
        before = plain()
        for k in range(4):
            chip(before, k, 1500, x=60)
        before = soft(before)
        row = (40, 1500, 1000, 1500 + CHIP_H)
        mid = shifted(before, row, 35, ground=245)
        end = shifted(before, row, 70, ground=245)
        res = check([before, mid, end], [24.0, 57.0], box=chip_box(1, 1500, x=60))
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

    def test_a_switch_alone_whose_knob_slides_did_not_move(self):
        # 0.11.0: no lookalike to drop. The knob matches 52 px away (0.744), but the part of that place
        # outside the switch's box is empty: the knob slid inside a switch that stayed.
        before = screen()
        switch(before, SWITCHES[1], True)
        res = check([before, switch(before.copy(), SWITCHES[1], False)], [17.0], box=SWITCHES[1])
        c = res["control"]
        self.assertEqual((res["fails"]["moved"], c["suspected"], c["shift"], c["arrived"]), (False, False, [-52, 0], False), c)
        self.assertEqual(c["texture_ahead"][1], 0.0)

    def test_a_switch_toggled_among_switches_like_it_did_not_move(self):
        res = self.toggled([SWITCHES[0], SWITCHES[2]])
        self.assertFalse(res["fails"]["moved"], res["control"])
        self.assertEqual(res["changes"], [])

    def test_with_one_lookalike_only_it_did_not_move_either(self):
        res = self.toggled([SWITCHES[0]])
        self.assertFalse(res["fails"]["moved"], res["control"])
        self.assertIsNotNone(res["control"]["already_there"])

    def test_a_move_among_identical_chips_is_told_at_the_nearest_place(self):
        # Up to the first form of 0.11.0 this was "cannot be placed" (a second shift matched as well). The
        # chips did move: every place that matches lies away from the control's own, and none of them
        # stood there before the input. 0.11.0 tells the move at the place nearest to it.
        chips = [(60 + 220 * n, 1500, 240 + 220 * n, 1560) for n in range(4)]
        before = screen()
        for c in chips:                                             # all alike: the control's look repeats
            before = paint(before, c, 120, halo=False)
            before[c[1] + 20:c[1] + 40, c[0] + 20:c[0] + 120] = 20
        end = shifted(before, (40, 1500, 1000, 1560), 70)
        res = check([before, end], [30.0], box=chips[1])
        c = res["control"]
        self.assertEqual((res["fails"]["moved"], c["shift"]), (True, [70, 0]), c)
        self.assertGreater(c["places"], 1)                          # a second shift matches as well
        self.assertTrue(res["fails"]["still_there"])                # its neighbours are still judged

    def test_the_scopes_join_level_reaches_the_check(self):
        with mock.patch.object(v, "outside_check", return_value={"box_px": [1, 2, 3, 4], "strip": None}) as oc:
            u.run_outside(Path("."), "tap", {"may_change": [], "join_level": 55}, Path("rec.mp4"), (W, H), 0,
                          [(1, 20.0)], SWITCH, "declared", ROWS)
        self.assertEqual(oc.call_args.kwargs["join_level"], 55)

    def test_a_chip_that_moves_70_px_still_moves(self):
        before = plain()
        for k in range(4):
            chip(before, k, 1500, x=60)
        before = soft(before)
        end = shifted(before, (40, 1500, 1000, 1500 + CHIP_H), 70, ground=245)
        res = check([before, end], [30.0], box=chip_box(1, 1500, x=60))
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
        # 0.11.0: a label of strokes; a plain bar's long straight edges match a few px along themselves
        # as well, which the correlation's uniqueness rule refuses.
        before = soft(text(plain(), 320, 1212, "SALE 24", 20, 36, 4, 50, 8))
        box = (300, 1200, 600, 1260)
        after = shifted(before, (280, 1200, 700, 1260), dx, ground=245)
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


class ControlMoveByCorrelationTest(unittest.TestCase):
    """0.11.0: the control's move is judged by a correlation that a change of its look leaves high."""

    def strip(self, down=False):
        """The day strip moves 70 px up (or back down): the tapped chip turns selected and the list below
        is replaced."""
        up = (day_screen(STRIP_Y, 6, "TUE 9 PICS"), day_screen(STRIP_Y - 70, 2, "WED 13 SAL"))
        if down:
            return check([up[1], up[0]], [30.0], box=chip_box(6, STRIP_Y - 70))
        return check([up[0], up[1]], [30.0], box=chip_box(2))

    def test_a_day_strip_that_moves_70_px_up_while_the_tapped_chip_turns_bold(self):
        res = self.strip()
        c = res["control"]
        self.assertEqual((res["fails"]["moved"], c["shift"], c["judged"]), (True, [0, -70], True), c)
        self.assertEqual(c["measure"], v.OUTSIDE_MOVE_MEASURE)
        self.assertGreaterEqual(c["correlation"], v.OUTSIDE_MOVE_CORR_MIN + 0.1)
        self.assertIn("still_there", [k for k, x in res["fails"].items() if x])       # the list below is still judged

    def test_and_back_down(self):
        res = self.strip(down=True)
        self.assertEqual((res["fails"]["moved"], res["control"]["shift"]), (True, [0, 70]), res["control"])

    def test_a_difference_of_grey_levels_cannot_see_it(self):
        # The measure up to 0.10.0, on the same frames: the bolder chip is no close match anywhere (here
        # the flat ground under another shift scores better than the true one).
        a, b = day_screen(STRIP_Y, 6, "TUE 9 PICS"), day_screen(STRIP_Y - 70, 2, "WED 13 SAL")
        found = v.find_shift(v._grey(a), v._grey(b), chip_box(2), v.OUTSIDE_SEARCH_PX)
        self.assertFalse(v.moved(found, 8.0), found)

    def test_the_same_chip_turning_bold_in_place_did_not_move(self):
        res = check([day_screen(STRIP_Y, 6, "TUE 9 PICS"), day_screen(STRIP_Y, 2, "TUE 9 PICS")], [30.0],
                    box=chip_box(2))
        self.assertEqual((res["fails"]["moved"], res["control"]["shift"]), (False, [0, 0]), res["control"])

    def button(self, dx, value):
        f = plain()
        f[1200:1300, 300:700] = 232                                          # a face a little darker than the ground
        return soft(text(f, 380 + dx, 1230, "CONFIRM", 24, 40, 4, value, 8))

    def test_a_button_label_that_moves_10_px_and_changes_colour(self):
        res = check([self.button(0, 60), self.button(10, 150)], [30.0], box=(300, 1200, 700, 1300))
        self.assertEqual((res["fails"]["moved"], res["control"]["shift"]), (True, [10, 0]), res["control"])
        res = check([self.button(0, 60), self.button(0, 150)], [30.0], box=(300, 1200, 700, 1300))
        self.assertEqual((res["fails"]["moved"], res["control"]["shift"]), (False, [0, 0]), res["control"])

    def rows(self, *placed):
        f = plain()
        for y, s in placed:
            text(f, 320, y, s, 20, 36, 4, 50, 8)
        return soft(f)

    LABEL = (300, 1500, 700, 1560)

    def test_a_move_onto_a_place_where_a_lookalike_stood_before(self):
        before = self.rows((1510, "SALE 24"), (1440, "SALE 24"))
        res = check([before, self.rows((1440, "SALE 24"), (1510, "HI"))], [30.0], box=self.LABEL)
        c = res["control"]
        self.assertFalse(res["fails"]["moved"], c)
        found = v.find_move(v._grey(before), v._grey(self.rows((1440, "SALE 24"), (1510, "HI"))), self.LABEL, 300)
        up = [p for p in found["peaks"] if p["shift"] == [0, -70]][0]          # it was found, and dropped: the
        self.assertLessEqual(round(up["correlation"] - up["already_there"], 3), v.OUTSIDE_MOVE_MARGIN)  # lookalike
        self.assertGreaterEqual(c["lookalikes"], 1)
        alone = check([self.rows((1510, "SALE 24")), self.rows((1440, "SALE 24"), (1510, "HI"))], [30.0],
                      box=self.LABEL)
        self.assertEqual((alone["fails"]["moved"], alone["control"]["shift"]), (True, [0, -70]), alone["control"])

    def test_a_copy_that_appears_beside_a_control_that_stayed(self):
        # A copy 70 px up, the control at its place a little bolder: a place at the control's own.
        before = self.rows((1510, "SALE 24"))
        f = plain()
        text(f, 320, 1440, "SALE 24", 20, 36, 4, 50, 8)
        text(f, 320, 1510, "SALE 24", 20, 36, 5, 50, 8)
        res = check([before, soft(f)], [30.0], box=self.LABEL)
        c = res["control"]
        self.assertEqual((res["fails"]["moved"], c["suspected"], c["shift"]), (False, False, [0, 0]), c)

    def test_two_places_alike_away_from_it_are_a_move_to_the_nearer(self):
        before = self.rows((1510, "SALE 24"))
        res = check([before, self.rows((1440, "SALE 24"), (1720, "SALE 24"))], [30.0], box=self.LABEL)
        c = res["control"]
        self.assertEqual((res["fails"]["moved"], c["shift"], c["places"]), (True, [0, -70], 2), c)

    def test_a_still_icon_over_a_list_that_scrolls_did_not_move(self):
        # The list's texture inside the icon's box moved 100 px; the icon stayed at its place.
        words = ["SALE 24", "KEEP 81", "PICS 90", "SEND 13", "HELP 56", "FEED 70", "DATE 11"]

        def scroll(off):
            g = plain()
            for r in range(14):
                text(g, 560, 900 + 70 * r - off, words[r % 7] + " " + words[(r + 3) % 7], 20, 36, 3, 80, 8)
            return soft(text(g, 945, 1294, "K", 26, 36, 6, 20, 6))
        res = check([scroll(0), scroll(100)], [30.0], box=(910, 1270, 1010, 1350))
        c = res["control"]
        self.assertEqual((res["fails"]["moved"], c["suspected"], c["gone"], c["shift"]), (False, False, False, [0, -100]), c)
        self.assertGreater(c["at_no_shift"], v.OUTSIDE_MOVE_GONE_SHARE * c["best_correlation"])
        self.assertGreater(round(c["at_no_shift"] - c["no_shift_if_moved"], 3), v.OUTSIDE_MOVE_MARGIN)
        self.assertGreaterEqual(round(c["best_correlation"] - c["at_no_shift"], 3), v.OUTSIDE_MOVE_MARGIN)

    def test_a_short_move_overlaps_its_old_place_and_is_told(self):
        # 8 px up: no shift keeps 0.56 of the strokes, as much as the strip's own overlap explains.
        res = check([day_screen(STRIP_Y, 6, "TUE 9 PICS"), day_screen(STRIP_Y - 8, 2, "TUE 9 PICS")], [30.0],
                    box=chip_box(2))
        c = res["control"]
        self.assertEqual((res["fails"]["moved"], c["shift"]), (True, [0, -8]), c)
        self.assertGreater(c["at_no_shift"], v.OUTSIDE_MOVE_GONE_SHARE * c["best_correlation"])

    def test_a_strip_that_moves_sideways_while_the_chip_turns_bold(self):
        # Another chip of the strip matches at the pitch plus the move (0.794): it arrived there too.
        for dx in (8, 20, -70):
            f = plain()
            text(f, 60, STRIP_Y - 124, "MON 10 FEB", 24, 40, 4, 40, 12)
            for k in range(7):
                chip(f, k, selected=(k == 2), x=53 + dx)
            res = check([day_screen(STRIP_Y, 6, "TUE 9 PICS"), soft(f)], [30.0], box=chip_box(2))
            c = res["control"]
            self.assertEqual((res["fails"]["moved"], c["shift"]), (True, [dx, 0]), (dx, c))
            self.assertEqual(c["places"], 2, (dx, c))

    def test_a_move_longer_than_the_control_is_read_at_the_new_place(self):
        # What arrived is read in the part of the new box outside the old one: for a move longer than
        # the control along its axis, all of the new box, not the gap just past the old box.
        def icon(x):
            g = plain()
            g[1500:1524, x:x + 24] = 60
            g[1503:1521, x + 3:x + 21] = 245                                 # a 24 px outline
            return soft(g)
        for dx in (70, -70):
            res = check([icon(500), icon(500 + dx)], [30.0], box=(500, 1500, 524, 1524))
            c = res["control"]
            self.assertEqual((res["fails"]["moved"], c["shift"], c["arrived"]), (True, [dx, 0], True), (dx, c))
        for dy in (150, 200, 280, -150, -200, -280):
            b = self.rows((1510 + dy, "SALE 24"))
            res = check([self.rows((1510, "SALE 24")), b], [30.0], box=self.LABEL)
            c = res["control"]
            self.assertEqual((res["fails"]["moved"], c["shift"], c["arrived"]), (True, [0, dy], True), (dy, c))
            self.assertGreaterEqual(c["texture_ahead"][1], v.OUTSIDE_MOVE_ARRIVE_SHARE * c["texture_ahead"][0])

    def test_a_chip_against_the_frames_edge_is_no_limit(self):
        f = plain()
        for k in range(7):
            chip(f, k, selected=(k == 0), x=0)
        res = check([day_screen(STRIP_Y, 6, "TUE 9 PICS"), soft(f)], [30.0], box=chip_box(0))
        c = res["control"]
        self.assertEqual((res["fails"]["moved"], c["shift"], c["at_search_limit"]), (True, [-53, 0], False), c)

    def test_an_inverted_chip_that_moves_70_px(self):
        # Light strokes on a dark pill a little larger than the chip: the grey correlates negatively, the
        # edges correlate fully.
        def strip_(y, inverted=None):
            f = plain()
            text(f, 60, y - 124, "MON 10 FEB", 24, 40, 4, 40, 12)
            for k in range(7):
                if k == inverted:
                    x0, y0, x1, y1 = chip_box(k, y)
                    f[y0 - 6:y1 + 6, x0 - 6:x1 + 6] = 40
                    text(f, x0 + 22, y0 + 22, DAYS[k], 18, 26, 3, 245, 10)
                    text(f, x0 + 30, y0 + 70, str(9 + k), 30, 60, 5, 245, 12)
                else:
                    chip(f, k, y)
            return soft(f)
        res = check([strip_(STRIP_Y), strip_(STRIP_Y - 70, 2)], [30.0], box=chip_box(2))
        c = res["control"]
        self.assertEqual((res["fails"]["moved"], c["shift"]), (True, [0, -70]), c)
        self.assertGreaterEqual(c["best_correlation"], 0.95)

    def test_a_stronger_look_change_is_suspected_and_said(self):
        # A dark fill under light strokes: the right shift at 0.662, under the minimum.
        def strip_(y, filled=None):
            f = plain()
            text(f, 60, y - 124, "MON 10 FEB", 24, 40, 4, 40, 12)
            for k in range(7):
                if k == filled:
                    x0, y0, x1, y1 = chip_box(k, y)
                    f[y0 + 6:y1 - 6, x0 + 6:x1 - 6] = 40
                    text(f, x0 + 22, y0 + 22, DAYS[k], 18, 26, 3, 235, 10)
                    text(f, x0 + 30, y0 + 70, str(9 + k), 30, 60, 5, 235, 12)
                else:
                    chip(f, k, y, selected=(k == 6 and filled is None))
            return soft(f)
        res = check([strip_(STRIP_Y), strip_(STRIP_Y - 70, 2)], [30.0], box=chip_box(2))
        c = res["control"]
        self.assertEqual((res["fails"]["moved"], c["suspected"], c["shift"]), (False, True, [0, -70]), c)
        self.assertTrue(v.OUTSIDE_MOVE_SUSPECT_MIN <= c["best_correlation"] < v.OUTSIDE_MOVE_CORR_MIN, c)

    def test_a_flat_control_has_no_shift(self):
        before = plain()
        before[1500:1560, 300:700] = 200
        after = shifted(before, (280, 1500, 800, 1560), 70, ground=245)
        res = check([before, after], [30.0], box=self.LABEL)
        c = res["control"]
        self.assertEqual((res["fails"]["moved"], c["flat"], c["shift"], c["correlation"]), (False, True, [0, 0], None))

    def test_the_whole_screen_allowed_is_not_judged(self):
        a, b = day_screen(STRIP_Y, 6, "TUE 9 PICS"), day_screen(STRIP_Y - 70, 2, "WED 13 SAL")
        res = check([a, b], [30.0], box=chip_box(2), screen_=True)
        self.assertEqual((res["control"]["judged"], res["control"]["moved"], res["fails"]["moved"]), (False, False, False))
        self.assertIn("the whole screen may change", res["control"]["not_judged"])
        self.assertNotIn("shift", res["control"])
        self.assertFalse(any(res["fails"].values()), res["fails"])

    @staticmethod
    def search(peak_px, h=2400, box=(300, 1000, 400, 1060)):
        """_axis_search over a frame whose grey is its row number, scored best where the patch's mean row
        lies peak_px below the box's: the search finds peak_px, or the farthest shift it reaches."""
        g = np.repeat(np.arange(h, dtype=np.float32)[:, None], 1080, axis=1)
        centre = float(g[box[1]:box[3]:2, 0].mean())
        return v._axis_search(g, g, box, 300, lambda p, q: -abs(float(q.mean()) - (centre + peak_px)))

    def test_a_shift_at_the_search_limit_is_no_move(self):
        _s, where, at_limit, f, limits = self.search(400)               # better still beyond the limit
        self.assertEqual((where, at_limit, f), ((0, 300), True, 2))
        self.assertIn((0, -300), limits)
        self.assertEqual(self.search(-400)[1:3], ((0, -300), True))
        self.assertEqual(self.search(296)[1:3], ((0, 296), False))
        self.assertEqual(self.search(0)[1:3], ((0, 0), False))
        c = ControlMoveBoundaryTest.c(shift=[0, 296])
        self.assertTrue(v.control_moved(c, 8.0))
        c["peaks"][0]["at_search_limit"] = True
        self.assertEqual((v.judge_move(c, 8.0)["moved"], v.judge_move(c, 8.0)["suspected"]), (False, False))

    def test_the_frames_edge_is_a_limit_when_the_patch_would_be_cut_off(self):
        # A box 100 px above the frame's foot: the search stops there. Better still one step on, on the
        # part left inside: a limit. Worse there: the content lies against the edge, no limit.
        box = (300, 1060, 400, 1120)
        self.assertEqual(self.search(400, h=1220, box=box)[1:3], ((0, 100), True))
        self.assertEqual(self.search(100, h=1220, box=box)[1:3], ((0, 100), False))
        self.assertEqual(self.search(60, h=1220, box=box)[1:3], ((0, 60), False))


class ControlMoveBoundaryTest(unittest.TestCase):
    """The rule's numbers, on the rounded measures run.json keeps (judge_move)."""

    @staticmethod
    def c(corr=0.9, z=0.0, shift=(0, -70), already=0.0, echo=0.0, more=()):
        return {"flat": False, "at_no_shift": z,
                "peaks": [{"shift": list(shift), "correlation": corr, "already_there": already,
                           "no_shift_if_moved": echo, "at_search_limit": False}, *more]}

    def judge(self, **kw):
        j = v.judge_move(self.c(**kw), 8.0)
        return "moved" if j["moved"] else "suspected" if j["suspected"] else "no"

    def test_the_defaults(self):
        self.assertEqual((v.OUTSIDE_MOVE_CORR_MIN, v.OUTSIDE_MOVE_MARGIN, v.OUTSIDE_MOVE_GONE_SHARE,
                          v.OUTSIDE_MOVE_SUSPECT_MIN), (0.7, 0.15, 0.5, 0.4))
        self.assertEqual(self.judge(), "moved")

    def test_the_minimum_and_the_suspected_band(self):
        self.assertEqual(self.judge(corr=0.7), "moved")
        self.assertEqual(self.judge(corr=0.699), "suspected")
        self.assertEqual(self.judge(corr=0.4), "suspected")
        self.assertEqual(self.judge(corr=0.399), "no")

    def test_gone_by_the_margin(self):
        self.assertEqual(self.judge(z=0.75, echo=0.7), "moved")
        self.assertEqual(self.judge(z=0.751, echo=0.7), "no")

    def test_gone_by_half_of_the_best(self):
        self.assertEqual(self.judge(z=0.45), "moved")
        self.assertEqual(self.judge(z=0.451), "no")

    def test_or_by_what_a_move_of_everything_around_would_keep(self):
        self.assertEqual(self.judge(z=0.6, echo=0.45), "moved")
        self.assertEqual(self.judge(z=0.6, echo=0.449), "no")
        self.assertEqual(self.judge(z=0.6, echo=None), "no")

    def test_a_lookalike_already_there_is_dropped(self):
        self.assertEqual(self.judge(already=0.75), "no")
        self.assertEqual(self.judge(already=0.749), "moved")
        near = {"shift": [0, 4], "correlation": 0.85, "already_there": 0.85, "no_shift_if_moved": 0.0,
                "at_search_limit": False}
        self.assertEqual(self.judge(z=0.0, more=[near]), "no")              # its own place is never dropped

    def test_places_within_the_margin(self):
        def other(corr, shift=(0, 4)):
            return [{"shift": list(shift), "correlation": corr, "already_there": 0.0, "no_shift_if_moved": 0.0,
                     "at_search_limit": False}]
        self.assertEqual(self.judge(more=other(0.751)), "no")              # a place nearer than move_px
        self.assertEqual(self.judge(more=other(0.75)), "moved")            # not within the margin: no place
        j = v.judge_move(self.c(shift=(0, -140), more=other(0.85, (0, 30))), 8.0)
        self.assertEqual((j["moved"], j["shift"], j["places"], j["correlation"], j["best_correlation"]),
                         (True, [0, 30], 2, 0.85, 0.9))

    def test_something_arrived_at_the_new_place(self):
        def ahead(before, after):
            c = self.c()
            c["peaks"][0]["texture_ahead"] = [before, after]
            j = v.judge_move(c, 8.0)
            return "moved" if j["moved"] else "no"
        self.assertEqual(ahead(10.0, 5.0), "moved")
        self.assertEqual(ahead(10.0, 4.999), "no")
        self.assertEqual(ahead(0.0, 0.0), "moved")                         # nothing to arrive: no evidence

    def test_the_move_threshold_and_flat(self):
        self.assertEqual(self.judge(shift=(8, 0)), "moved")
        self.assertEqual(self.judge(shift=(7, 0)), "no")
        self.assertEqual(v.judge_move({"flat": True, "at_no_shift": None, "peaks": []}, 8.0)["moved"], False)


class SlideAtTheLimitTest(unittest.TestCase):
    """0.11.0: a slide whose best shift is the farthest searched is not told."""

    def frames(self, came_from):
        before = plain()
        y = np.arange(1450, 2300)                                   # a smooth ramp, 0.17 levels a row
        before[1450:2300, 100:980] = np.round(40 + 0.17 * (y - 1450)).astype(np.uint8)[:, None, None]
        end = before.copy()
        end[1900:2050, 100:980] = before[1900 - came_from:2050 - came_from, 100:980]
        end[1900 - came_from:2050 - came_from, 100:980] = 245       # the source vacated: only the limit holds
        return before, end

    def test_content_from_beyond_the_search_is_no_slide(self):
        before, end = self.frames(320)
        found = v.find_shift(v._grey(end), v._grey(before), [100, 1900, 980, 2050], v.OUTSIDE_SEARCH_PX)
        self.assertEqual((found["shift"], found["at_search_limit"]), ([0, -300], True), found)
        # it would pass for a slide but for the limit (its source is gone too)
        self.assertTrue(v.moved({**found, "at_search_limit": False}, 8.0) and v.unique(found), found)
        self.assertGreater(v.source_change(v._grey(end), v._grey(before), [100, 1900, 980, 2050], found["shift"]),
                           min(v.OUTSIDE_MATCH_MAX, v.OUTSIDE_MATCH_RATIO * found["still"]))
        res = check([before, end], [30.0])
        self.assertEqual(res["slid"], [])
        self.assertTrue(res["fails"]["still_there"])                    # the change is still judged

    @staticmethod
    def field(typed, count):
        f = plain()
        f[1200:1260, 300:700] = 235                                          # a text field, the control
        text(f, 310, 1212, typed, 16, 30, 3, 60, 6)
        return soft(text(f, 800, 1300, count, 18, 36, 4, 60, 8))           # a counter beside it, "0 24"

    def test_a_digit_that_turns_into_a_copy_of_one_that_stayed_is_no_slide(self):
        # The counter's "0" turns "2": a copy of the "2" of "24", 52 px to its right, which never moved.
        before, end = self.field("", "0 24"), self.field("HI", "2 24")
        res = check([before, end], [30.0], box=(300, 1200, 700, 1260))
        self.assertEqual(res["slid"], [])
        self.assertEqual(res["fails"], {"moved": False, "still_there": True, "slid": False, "came_back": False})
        (c,) = res["changes"]
        found = v.find_shift(v._grey(end), v._grey(before), c["box_px"], v.OUTSIDE_SEARCH_PX)
        self.assertEqual(found["shift"], [52, 0])                    # the backward search does find the "2"
        self.assertTrue(v.moved(found, 8.0) and v.unique(found), found)
        self.assertEqual(v.source_change(v._grey(end), v._grey(before), c["box_px"], found["shift"]), 0.0)

    def test_a_band_that_slides_leaves_its_source(self):
        def band(d, over=False):
            f = plain()
            text(f, 100, 1600 + d, "SALE 24 KEEP 81 PICS", 20, 36, 4, 50, 8)
            text(f, 100, 1660 + d, "SEND 13 HELP 56", 20, 36, 4, 50, 8)
            if over:
                text(f, 100, 1600, "DATE 11 FEED 70 BIKE", 20, 36, 4, 50, 8)
            return soft(f)
        for d, over in ((10, False), (70, False), (174, False), (174, True)):
            res = check([band(0), band(d, over)], [30.0])
            self.assertTrue(res["slid"], (d, over))
            self.assertEqual({tuple(sl["shift_px"]) for sl in res["slid"]}, {(0, d)}, (d, over))
            self.assertTrue(all(sl["source_change"] > 4.0 for sl in res["slid"]), (d, res["slid"]))

    def test_a_slide_within_the_search_is_told(self):
        before = plain()
        for k in range(4):
            chip(before, k, 1500, x=60)
        before = soft(before)
        end = shifted(before, (40, 1500, 1000, 1500 + CHIP_H), 70, ground=245)
        res = check([before, end], [30.0], box=chip_box(1, 1500, x=60))
        self.assertTrue(res["slid"])
        self.assertTrue(all(sl["shift_px"] == [-70, 0] or abs(sl["shift_px"][0]) >= 8 for sl in res["slid"]))


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
        o = outside(moved=True)
        o["control"].update({"correlation": 0.897, "at_no_shift": 0.094})
        self.assertIn("correlation 0.897 there, 0.094 at its place", self.gate(o)["violations"][0]["message"])

    def test_a_suspected_move_is_a_note(self):
        o = outside()
        o["control"].update({"shift": [0, -70], "suspected": True, "best_correlation": 0.662})
        res = self.gate(o)
        self.assertEqual(res["verdict"], "PASS")
        self.assertTrue(any("the control may have moved by [0, -70] px (correlation 0.662, under the minimum of 0.7 "
                            "for a failure)" in n for n in res["notes"]), res["notes"])

    def test_a_move_not_judged_under_screen_passes(self):
        o = outside()
        o["control"] = {"judged": False, "moved": False, "not_judged": "the whole screen may change"}
        self.assertEqual(self.gate(o)["verdict"], "PASS")

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
        # the plugin's version is pinned by the newest release's test (test_phone_0110), the marketplace's never
        self.assertEqual((v.OUTSIDE_MOVE_PX, v.OUTSIDE_CAME_BACK_MS, v.OUTSIDE_SEARCH_PX, u.CONTROL_AREA_MAX),
                         (8, 50, 300, 0.25))

    def test_documented_0110(self):
        root = Path(u.HERE).parent
        readme = " ".join((root / "README.md").read_text().split())
        for phrase in ("cross-correlation of the gradient magnitude", "reaches 0.7", "`places`", "`suspected`",
                       "`best_correlation`", "`texture_ahead`", "`no_shift_if_moved`", "`at_no_shift`",
                       "`already_there`", "`at_search_limit`", "`lookalikes`", "`not_judged`",
                       "a day strip on an Android 14 emulator whose tapped chip moves 70 px and changes its look",
                       "the farthest searched (300 px, or the frame's edge where the patch would be cut off)",
                       "the control may have moved by"):
            self.assertIn(phrase, readme, phrase)
        changelog = (root / "CHANGELOG.md").read_text()
        self.assertIn("rule (a): the control's move is judged by the zero-mean normalised",
                      " ".join(changelog.split("## 0.10.0")[0].split()))
        self.assertIn("### Validation, 0.11.0: the control's move", (root / "PROVENANCE.md").read_text())
        self.assertIn("### Validation, 0.11.0: the control's move, second review", (root / "PROVENANCE.md").read_text())
        self.assertIn("suspected", " ".join(changelog.split("## 0.10.0")[0].split()))


if __name__ == "__main__":
    unittest.main()
