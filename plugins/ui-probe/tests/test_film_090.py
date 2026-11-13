"""0.9.0: `ui-probe film`, a film no run made, and `sheet --full --distinct`.

Every film here is synthetic, made in this file to look like a phone's screen capture measured once
(H.264, 60 a second): 1080 x 2400, no two consecutive frames bit-identical (a level of change in a few
pixels every frame), two places of five pixels that flicker by 50 levels every frame, and every 30th
frame a key frame that makes two consecutive changed pairs, each with about 0.03 % of the picture over
the level in groups of up to 40 px placed anywhere (groups of neighbouring frames and of frames 30
apart may touch). Each test adds the changes it needs. Most tests hand the
frames to the analysis through a stand-in for the decoder; the classes marked so make real files with
ffmpeg (a lossless PNG stream, as test_simulator_040 does) when ffmpeg is present. No device."""
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

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import ui_probe as u  # noqa: E402
import ui_video as v  # noqa: E402

FW, FH = 1080, 2400
MIN = v.film_min_group(FW, FH)              # 78 px
HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
FLICKER = ((331, 1213), (817, 517))        # (x, y): the centre of a plus of five pixels each
RIPPLE_SIZES = ((5, 8), (4, 6), (6, 6), (3, 9), (4, 9))   # up to 40 px a group


class Film:
    """Frame i of a synthetic capture (see the module's docstring). events: (first, end, (x0, y0, x1, y1),
    value) paints a rectangle of that grey from frame first up to frame end. shade(i): levels added to
    the whole picture in frame i."""

    def __init__(self, n, fps=60.0, key=30, flicker=True, noise=True, events=(), shade=None):
        self.n, self.fps, self.key, self.flicker, self.noise = n, fps, key, flicker, noise
        self.events, self.shade = list(events), shade
        self.base = np.full((FH, FW, 3), 210, dtype=np.uint8)
        for row in range(6):
            self.base[400 + 160 * row:440 + 160 * row, 100:900] = 40      # lines of something on the screen
        self._patterns = {}

    def pattern(self, p):
        """Ripple pattern p: twelve groups of up to 40 px, +15 levels, anywhere in the picture: groups of
        one pattern, of two patterns in a row and of patterns 30 frames apart may touch."""
        if p not in self._patterns:
            rng = np.random.default_rng(5000 + p)
            self._patterns[p] = [(int(rng.integers(0, FW - w)), int(rng.integers(0, FH - h)), (w, h))
                                 for w, h in (RIPPLE_SIZES[n % len(RIPPLE_SIZES)] for n in range(12))]
        return self._patterns[p]

    def frame(self, i):
        f = self.base.copy()
        if self.shade is not None and self.shade(i):
            f = np.clip(f.astype(np.int16) + self.shade(i), 0, 255).astype(np.uint8)
        if self.key:
            k = i // self.key
            for x, y, (w, h) in self.pattern(2 * k if i % self.key == 0 else 2 * k + 1):
                f[y:y + h, x:x + w] += 15
        if self.noise:
            rng = np.random.default_rng(i)
            f[rng.integers(0, FH, 20), rng.integers(0, FW, 20)] += 1
        if self.flicker:
            for n, (x, y) in enumerate(FLICKER):
                if (i + n) % 2:
                    for dx, dy in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)):
                        f[y + dy, x + dx] = np.minimum(f[y + dy, x + dx].astype(np.int16) + 50, 255)
        for first, end, (x0, y0, x1, y1), value in self.events:
            if first <= i < end:
                f[y0:y1, x0:x1] = value
        return f

    def pts(self):
        return [round(i / self.fps, 6) for i in range(self.n)]


@contextlib.contextmanager
def decoded(film, header=None):
    """The analysis reads `film` through stand-ins for ffprobe and the decoder."""
    def fake_iter(_video, indices, size):
        assert size == (FW, FH), size
        for i in sorted(set(indices)):
            yield i, film.frame(i)

    with mock.patch.object(v, "probe_pts", return_value=film.pts()), \
            mock.patch.object(v, "probe_size", return_value=(FW, FH)), \
            mock.patch.object(v, "probe_header_frames", return_value=header), \
            mock.patch.object(v, "iter_frames", side_effect=fake_iter):
        yield


def main(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = u.main([str(a) for a in argv])
    return rc, out.getvalue(), err.getvalue()


WINDOW = [{"id": "sit", "start_s": 0.2, "end_s": 1.6}]       # frames 12 to 95


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.video = self.root / "capture.mov"
        self.video.write_bytes(b"a film the stand-in decodes")
        self.n = 0

    def tearDown(self):
        self.tmp.cleanup()

    def windows(self, data):
        self.n += 1
        p = self.root / f"windows-{self.n}.json"
        p.write_text(json.dumps(data))
        return p

    def film(self, film, windows=WINDOW, name="film-run", header=None):
        out = self.root / name
        with decoded(film, header):
            res = u.film_run(self.video, str(self.windows(windows)), out)
        return out, res

    def rec(self, res, wid="sit"):
        return next(s for s in res["steps"] if s["id"] == wid)["recording"]

    def changed(self, res, wid="sit"):
        return [f["i"] for f in self.rec(res, wid)["drawing_frames"]]

    def gate(self, out, scn=None):
        return u.gate([str(out)], {}, scn)["runs"][0]


class PixelGroupsTest(unittest.TestCase):
    def test_groups_join_along_rows_and_diagonals(self):
        m = np.zeros((10, 12), dtype=bool)
        m[1, 1:4] = True
        m[2, 4] = True             # touches the run above only at a corner
        m[3, 5:7] = True           # and again
        m[6, 0] = True             # alone
        m[8:10, 9:12] = True       # a block
        got = sorted(v.pixel_groups(m))
        self.assertEqual(got, [(1, [0, 6, 1, 7]), (6, [1, 1, 7, 4]), (6, [9, 8, 12, 10])])

    def test_a_u_shape_is_one_group(self):
        m = np.zeros((5, 7), dtype=bool)
        m[0:4, 1] = True
        m[0:4, 5] = True
        m[4, 1:6] = True
        self.assertEqual(v.pixel_groups(m), [(13, [1, 0, 6, 5])])

    def test_empty_and_minimum(self):
        self.assertEqual(v.pixel_groups(np.zeros((3, 3), dtype=bool)), [])
        self.assertEqual((MIN, v.film_min_group(1206, 2622)), (78, 95))


# --------------------------------------------------------------------------------------------
# what the recording does is left out, and says so; what the app does counts
# --------------------------------------------------------------------------------------------

class FloorTest(Base):
    def test_a_rest_holds_no_change(self):
        out, res = self.film(Film(100))
        rec = self.rec(res)
        self.assertEqual((rec["frames"], rec["changed_app"], rec["scattered"], rec["still"]), (84, 0, 84, 0))
        self.assertEqual((rec["largest_scattered_group_px"], rec["min_group_px"], rec["caution"]), (40, MIN, False))
        boxes = sorted(p["box_px"] for p in rec["steady_places"])
        self.assertEqual(len(boxes), 2)
        for (x, y), box in zip(sorted(FLICKER), boxes):
            self.assertTrue(box[0] <= x - 1 and box[2] >= x + 2 and box[1] <= y - 1 and box[3] >= y + 2, (box, x, y))
        slow, ends = rec["slow"], rec["ends"]
        self.assertEqual((slow["frames_apart"], slow["compared"], rec["changed_by_slow_only"]), (30, 55, 0))   # frames 41 to 95
        self.assertEqual((slow["largest_scattered_group_px"], slow["caution"]), (40, False))
        self.assertEqual((ends["first_frame"], ends["last_frame"], ends["differ"], ends["largest_group_px"]),
                         (12, 95, False, 40))
        run = self.gate(out)
        self.assertEqual(run["verdict"], "PASS", run)
        notes = " ".join(run["notes"])
        self.assertIn("sit: 84 of 84 frames held only scattered changes and were left out; the largest scattered group "
                      "was 40 px, the minimum for a change 78 px", notes)
        self.assertIn("sit: each frame was also compared with the one 30 frames (0.5 s) earlier, 55 times: 0 frames "
                      "counted only by that comparison; its largest scattered group was 40 px, the minimum 78 px", notes)
        self.assertIn("sit: the window's last frame (#95) against its first (#12): they do not differ, the largest "
                      "group 40 px, the minimum 78 px", notes)
        self.assertIn("pixels went over the level in at least a quarter of the frames at", notes)
        self.assertNotIn("caution", notes)

    def test_a_caret_is_counted_on_every_blink_also_on_key_frames(self):
        caret = (500, 1500, 506, 1560)                          # 6 x 60 px
        events = [(a, a + 15, caret, 0) for a in (15, 45, 75)]   # on at 15, 45, 75; off at 30, 60, 90 (key frames)
        _out, res = self.film(Film(100, events=events))
        self.assertEqual(self.changed(res), [15, 30, 45, 60, 75, 90])

    def test_a_digit_changing_once_is_counted_once(self):
        out, res = self.film(Film(100, events=[(75, 100, (600, 900, 630, 950), 20)]))
        self.assertEqual(self.changed(res), [75])
        run = self.gate(out)
        self.assertEqual([x["check"] for x in run["violations"]], ["rest-video"])
        self.assertEqual(run["violations"][0]["observed"], 1)

    def test_a_dot_of_6_by_6_is_not_seen(self):
        _out, res = self.film(Film(100, events=[(75, 100, (600, 900, 606, 906), 20)]))
        self.assertEqual(self.changed(res), [])

    def test_an_app_dot_beside_the_recordings_flicker_is_counted(self):
        x, y = FLICKER[0]
        _out, res = self.film(Film(100, events=[(75, 100, (x + 2, y - 6, x + 14, y + 6), 20)]))     # 12 x 12 px
        self.assertEqual(self.changed(res), [75])

    def test_a_faint_change_of_the_whole_picture(self):
        _out, res = self.film(Film(100, shade=lambda i: 5 * (i % 2)))
        self.assertEqual(self.changed(res), [])
        _out, res = self.film(Film(100, shade=lambda i: 15 * (i % 2)), name="r15")
        self.assertEqual(self.changed(res), list(range(12, 96)))

    def test_a_low_contrast_change_every_other_frame(self):
        film = Film(100, shade=None)
        real = film.frame

        def frame(i):
            f = real(i)
            if (i // 2) % 2:
                f[1000:1100, 300:400] = np.minimum(f[1000:1100, 300:400].astype(np.int16) + 15, 255)
            return f

        film.frame = frame
        _out, res = self.film(film)
        self.assertEqual(self.changed(res), [i for i in range(12, 96) if i % 2 == 0])

    def test_a_scattered_group_near_the_minimum_earns_a_caution(self):
        out, res = self.film(Film(100, events=[(50, 51, (700, 2000, 706, 2010), 0)]))     # 60 px, one frame
        rec = self.rec(res)
        self.assertEqual((self.changed(res), rec["largest_scattered_group_px"], rec["caution"]), ([], 60, True))
        notes = " ".join(self.gate(out)["notes"])
        self.assertIn("sit: caution: a scattered group of 60 px reached 70 % of the minimum or more", notes)
        self.assertEqual((rec["slow"]["largest_scattered_group_px"], rec["slow"]["caution"]), (60, True))
        self.assertIn("sit: caution: in the comparison 30 frames apart a scattered group of 60 px reached 70 % of the "
                      "minimum or more", notes)
        self.assertFalse(rec["ends"]["caution"])
        out, res = self.film(Film(100, events=[(95, 96, (700, 2000, 706, 2010), 0)]), name="ends")   # the last frame only
        rec = self.rec(res)
        self.assertEqual((rec["ends"]["largest_group_px"], rec["ends"]["differ"], rec["ends"]["caution"]), (60, False, True))
        self.assertIn("sit: caution: between the window's ends a scattered group of 60 px reached 70 % of the minimum",
                      " ".join(self.gate(out)["notes"]))

    def test_system_bands_only(self):
        film = Film(100, events=[(50, 70, (200, 0, 300, 30), 0)])      # the top 30 rows only
        out, res = self.film(film, {"insets_px": [40, 30], "windows": WINDOW})
        rec = self.rec(res)
        self.assertEqual((rec["changed_app"], rec["bars_only"]), (0, 2))
        self.assertEqual(res["film"]["app_rows"], [40, FH - 30])
        self.assertEqual(self.gate(out)["verdict"], "PASS")
        out2, res2 = self.film(film, name="no-insets")
        self.assertEqual(self.changed(res2), [50, 70])
        run = self.gate(out2)
        self.assertEqual(run["verdict"], "FAIL")
        self.assertIn("the whole picture was taken as the app's area", " ".join(run["notes"]))

    def test_frames_full_holds_the_changes_and_the_ends_only(self):
        _out, res = self.film(Film(100, events=[(75, 100, (600, 900, 630, 950), 20)]))
        names = sorted(p.name for p in (self.root / "film-run" / "frames-full" / "sit").glob("*.png"))
        self.assertEqual(names, ["f000012.png", "f000075.png", "f000095.png"])

    def test_a_header_that_differs_from_the_decoded_count_is_reported(self):
        out, res = self.film(Film(100), header=130)
        self.assertEqual((res["film"]["frames"], res["film"]["header_frames"]), (100, 130))
        self.assertIn("the film's header states 130 frames, but 100 were decoded", " ".join(self.gate(out)["notes"]))
        out, _ = self.film(Film(100), header=100, name="same")
        self.assertNotIn("header states", " ".join(self.gate(out)["notes"]))


def painted(film, paint):
    """film with paint(i, frame) applied to each frame after the rest."""
    real = film.frame

    def frame(i):
        f = real(i).copy()
        paint(i, f)
        return f

    film.frame = frame
    return film


def add(f, box, levels):
    x0, y0, x1, y1 = box
    f[y0:y1, x0:x1] = np.clip(f[y0:y1, x0:x1].astype(np.int16) + int(levels), 0, 255).astype(np.uint8)


class SlowChangeTest(Base):
    """A change too slow to show between neighbours: each one below passed with 0 changed frames when a
    frame was compared with the one before only."""

    def slow_only(self, paint):
        out, res = self.film(painted(Film(100), paint))
        rec = self.rec(res)
        self.assertTrue(rec["drawing_frames"], "nothing counted")
        self.assertEqual({f["by"] for f in rec["drawing_frames"]}, {"slow"})
        self.assertEqual(rec["changed_by_slow_only"], rec["changed_app"])
        run = self.gate(out)
        self.assertEqual([x["check"] for x in run["violations"]], ["rest-video"])
        return rec

    def test_the_whole_picture_darkening_4_levels_a_frame(self):
        self.slow_only(lambda i, f: add(f, (0, 0, FW, FH), -4 * max(0, i - 20)))

    def test_a_skeleton_pulse_of_30_levels_over_60_frames_returning_to_its_start(self):
        rec = self.slow_only(lambda i, f: add(f, (100, 1500, 400, 1580), max(0, 30 - abs(i - 50)) if 20 <= i <= 80 else 0))
        self.assertLess(max(x["i"] for x in rec["drawing_frames"]), 96)
        self.assertFalse(rec["ends"]["differ"])                  # it came back: the ends alone would not see it

    def test_a_bar_1_px_high_growing_3_px_a_frame(self):
        self.slow_only(lambda i, f: add(f, (100, 2000, 100 + 3 * max(0, i - 20), 2001), -100))

    def test_a_bar_3_px_high_growing_8_px_a_frame(self):
        self.slow_only(lambda i, f: add(f, (100, 2100, 100 + 8 * max(0, i - 20), 2103), -100))

    def test_a_2_px_underline_sliding_4_px_a_frame(self):
        self.slow_only(lambda i, f: add(f, (100 + 4 * i, 1800, 160 + 4 * i, 1802), -100))

    def test_a_change_slower_than_half_a_second_is_seen_between_the_windows_ends(self):
        out, res = self.film(painted(Film(100), lambda i, f: add(f, (0, 0, FW, FH), -int(0.3 * max(0, i - 12)))))
        rec = self.rec(res)
        self.assertEqual((rec["changed_app"], rec["ends"]["differ"]), (0, True))
        run = self.gate(out)
        self.assertEqual([x["check"] for x in run["violations"]], ["rest-video"])
        self.assertIn("the window's last frame (#95) differs from its first (#12)", run["violations"][0]["message"])

    def test_a_change_made_once_is_counted_once(self):
        _out, res = self.film(Film(100, events=[(40, 100, (600, 900, 630, 950), 20)]))
        self.assertEqual([(f["i"], f["by"]) for f in self.rec(res)["drawing_frames"]], [(40, "frame")])

    def test_the_slow_settings(self):
        fade = lambda i, f: add(f, (100, 2000, 100 + 3 * max(0, i - 20), 2001), -100)     # noqa: E731
        _out, res = self.film(painted(Film(100), fade), {"slow_frames": 10, "slow_min_group": 400, "windows": WINDOW})
        rec = self.rec(res)
        self.assertEqual((rec["slow"]["frames_apart"], rec["slow"]["min_group_px"], rec["ends"]["min_group_px"]),
                         (10, 400, 400))
        self.assertEqual(rec["changed_app"], 0)                   # 30 px in 10 frames, 225 px at the ends: under 400
        for bad in (0, 1.5, True):
            with self.assertRaises(u.ProbeError) as ctx:
                u.load_windows(str(self.windows({"slow_frames": bad, "windows": WINDOW})), "x")
            self.assertIn("'slow_frames' must be a whole number of at least 1", str(ctx.exception))


DIGIT = (600, 900, 630, 950)                    # 30 x 50 px


def ticking(i, f):
    """A stopwatch digit that changes every 6 frames."""
    if (i // 6) % 2:
        f[DIGIT[1]:DIGIT[3], DIGIT[0]:DIGIT[2]] = 20


class AllowanceHidesNothingTest(Base):
    """A slow change beside a change that is allowed is still a slow change."""

    ALLOWED = [{**WINDOW[0], "allow": {"changed_frames_max": 30, "reason": "a stopwatch ticks"}}]

    def run_(self, paint, windows=None, name="film-run"):
        out, res = self.film(painted(Film(100), paint), windows or self.ALLOWED, name=name)
        return self.rec(res), self.gate(out)

    def test_a_ticking_allowed_digit_alone_passes(self):
        rec, run = self.run_(ticking)
        self.assertEqual((rec["changed_by_slow_only"], rec["ends"]["differ"], rec["ends"]["differ_unexplained"]),
                         (0, True, False))
        self.assertEqual(run["verdict"], "PASS", run["violations"])

    def test_a_label_fading_beside_the_allowed_digit(self):
        def paint(i, f):
            ticking(i, f)
            add(f, (650, 900, 710, 950), -max(0, i - 20))        # 20 px to the right, 1 level a frame
        rec, run = self.run_(paint)
        self.assertGreater(rec["changed_by_slow_only"], 30)
        self.assertEqual(run["verdict"], "FAIL")

    def test_a_bar_growing_beside_the_allowed_digit(self):
        def paint(i, f):
            ticking(i, f)
            add(f, (640, 900, 700, 900 + max(0, i - 20)), -100)   # 10 px to its right, 1 row of 60 px a frame
        rec, run = self.run_(paint)
        self.assertGreater(rec["changed_by_slow_only"], 30)          # it stays beside the digit all the while
        self.assertEqual(run["verdict"], "FAIL")

    def test_a_fade_beside_an_allowed_caret_fails_between_the_ends(self):
        caret = (500, 1500, 506, 1560)
        events = [(a, a + 15, caret, 0) for a in (15, 45, 75)]

        def paint(i, f):
            add(f, (100, 1700, 700, 2300), -int(0.25 * max(0, i - 12)))       # 600 x 600 px, 0.25 level a frame
        film = painted(Film(100, events=events), paint)
        windows = [{**WINDOW[0], "allow": {"changed_frames_max": 10, "reason": "a caret blinks"}}]
        out, res = self.film(film, windows)
        rec = self.rec(res)
        self.assertEqual((rec["changed_by_slow_only"], rec["ends"]["differ_unexplained"]), (0, True))
        run = self.gate(out)
        self.assertEqual([x["check"] for x in run["violations"]], ["rest-video"])
        self.assertIn("lies outside every change counted in the window and every region", run["violations"][0]["message"])

    def test_a_change_at_the_end_of_one_window_explains_nothing_in_the_next(self):
        # Window a ends with a 100 x 100 change at frame 47; window b begins at frame 48, where a bar
        # inside that box grows slowly. A guard: the slow comparison of b starts only once its frame
        # k earlier lies in b, so no change of a is ever among b's last k frames.
        def paint(i, f):
            if i >= 47:
                f[1000:1100, 300:400] = 0
            add(f, (310, 1010, 310 + max(0, i - 50), 1013), 100)
        windows = [{"id": "a", "start_s": 0.2, "end_s": 0.8}, {"id": "b", "start_s": 0.8, "end_s": 1.6}]
        out, res = self.film(painted(Film(100), paint), windows)
        self.assertEqual([f["i"] for f in self.rec(res, "a")["drawing_frames"]], [47])
        self.assertGreater(self.rec(res, "b")["changed_by_slow_only"], 0)


class ExplainedByPixelsTest(Base):
    """A slow group is a counted change seen again only on that change's own pixels (within 3 px), and
    only within half a second of it."""

    def test_four_allowed_counters_with_labels_fading_in_the_gaps(self):
        counters = [(100 + 70 * n, 900, 140 + 70 * n, 940) for n in range(4)]         # 40 x 40, 30 px apart

        def paint(i, f):
            for x0, y0, x1, y1 in counters:
                if (i // 6) % 2:
                    f[y0:y1, x0:x1] = 20
            for x0, _y0, _x1, _y1 in counters[:3]:
                add(f, (x0 + 45, 900, x0 + 65, 940), -max(0, i - 20))          # 20 x 40 px, 5 px from each counter
        windows = [{"id": "sit", "start_s": 0.2, "end_s": 3.3,
                    "allow": {"changed_frames_max": 60, "reason": "four stopwatches tick"}}]
        out, res = self.film(painted(Film(200), paint), windows)
        rec = self.rec(res)
        self.assertGreater(rec["changed_by_slow_only"], 100)
        self.assertEqual(self.gate(out)["verdict"], "FAIL")

    DIGIT_AT_40 = [(40, 100, DIGIT, 20)]

    def fading(self, box):
        return lambda i, f: add(f, box, -max(0, i - 40))           # 1 level a frame from frame 41

    def counted(self, film, windows=WINDOW, name="film-run"):
        _out, res = self.film(film, windows, name=name)
        return [x["i"] for x in self.rec(res)["drawing_frames"]]

    def test_a_slow_group_on_the_counted_pixels_within_half_a_second_is_explained(self):
        got = self.counted(painted(Film(100, events=self.DIGIT_AT_40), self.fading(DIGIT)))
        self.assertEqual([i for i in got if i <= 70], [40])            # the digit, once; its fade is that change
        self.assertIn(71, got)                                          # half a second later it is a change again

    def test_the_same_group_4_px_beside_is_not(self):
        x0, y0, x1, y1 = DIGIT
        beside = (x1 + 3, y0, x1 + 3 + (x1 - x0), y1)                  # its first column 4 px from the digit's last
        got = self.counted(painted(Film(100, events=self.DIGIT_AT_40), self.fading(beside)))
        self.assertEqual([i for i in got if i <= 70], [40] + list(range(51, 71)))
        line = (x1 + 3, y0, x1 + 5, y1)                                # 2 x 50 px, 4 and 5 px from the digit
        got = self.counted(painted(Film(100, events=self.DIGIT_AT_40), self.fading(line)), name="line")
        self.assertEqual([i for i in got if i <= 70], [40] + list(range(51, 71)))       # 100 px: counted
        windows = {"explain_px": 4, "windows": WINDOW}
        got = self.counted(painted(Film(100, events=self.DIGIT_AT_40), self.fading(line)), windows, name="r4")
        self.assertEqual([i for i in got if i <= 70], [40])            # at 4 px one column is explained: 50 px left
        with self.assertRaises(u.ProbeError) as ctx:
            u.load_windows(str(self.windows({"explain_px": -1, "windows": WINDOW})), "x")
        self.assertIn("'explain_px' must be a whole number of at least 0", str(ctx.exception))


class BoundaryTest(Base):
    """A group of exactly the minimum counts; one pixel less does not."""

    def test_between_neighbours(self):
        _out, res = self.film(Film(100, events=[(75, 100, (600, 900, 606, 913), 20)]))       # 6 x 13 = 78
        self.assertEqual([f["i"] for f in self.rec(res)["drawing_frames"]], [75])
        _out, res = self.film(Film(100, events=[(75, 100, (600, 900, 607, 911), 20)]), name="less")   # 7 x 11 = 77
        self.assertEqual(self.rec(res)["drawing_frames"], [])

    def test_in_the_slow_comparisons(self):
        def fading(box):
            return lambda i, f: add(f, box, -2 * max(0, i - 40))          # 2 levels a frame: never over 10 between neighbours
        _out, res = self.film(painted(Film(100), fading((600, 900, 606, 913))))
        rec = self.rec(res)
        self.assertEqual(rec["drawing_frames"][0]["i"], 46)                # 12 levels against frame 16
        self.assertEqual(rec["drawing_frames"][0]["slow_largest_group_px"], MIN)
        self.assertEqual((rec["ends"]["differ"], rec["ends"]["largest_group_px"]), (True, MIN))
        _out, res = self.film(painted(Film(100), fading((600, 900, 607, 911))), name="less")
        rec = self.rec(res)
        self.assertEqual((rec["drawing_frames"], rec["ends"]["differ"], rec["ends"]["largest_group_px"]), ([], False, 77))


class RepeatsTest(Base):
    """The same pictures at 60 and at 10 frames a second count the same, and bursts are formed from
    the frames that changed: two changes a second apart are two bursts."""

    EVENTS = [(1.0, 9.9, (40, 1600, 240, 1800), 0), (2.0, 9.9, (600, 1600, 800, 1800), 0)]

    def make(self, fps):
        events = [(int(a * fps), int(b * fps), box, val) for a, b, box, val in self.EVENTS]
        film = Film(int(3 * fps), fps=fps, key=int(fps // 2), events=events)
        return self.film(film, [{"id": "sit", "start_s": 0.5, "end_s": 2.9}], name=f"r{fps:g}")

    def test_sixty_and_ten_a_second(self):
        _o60, res60 = self.make(60.0)
        _o10, res10 = self.make(10.0)
        a, b = self.rec(res60), self.rec(res10)
        self.assertEqual((a["changed_app"], b["changed_app"]), (2, 2))
        self.assertEqual((a["bursts"], b["bursts"]), (2, 2))
        self.assertEqual(a["signature"], b["signature"])
        self.assertEqual((a["first_change_ms"], a["last_change_ms"]), (500.0, 1500.0))


class RegionsOnAFilmTest(Base):
    REGION = {"region": [0.75, 0.0, 1.0, 0.1], "changed_frames_max": 3, "reason": "a countdown ticks each second"}

    def test_a_countdown_inside_and_a_change_across_its_edge(self):
        box = (900, 60, 1000, 160)                                  # x 0.83 to 0.93, y 0.03 to 0.07
        events = [(60, 120, box, 0), (120, 180, box, 100), (180, 240, box, 0)]     # a digit each second
        events.append((200, 240, (750, 60, 900, 160), 0))           # from x 0.69 to 0.83: crosses x = 0.75
        windows = [{"id": "sit", "start_s": 0.5, "end_s": 3.9, "allow": {"regions": [self.REGION]}}]
        out, res = self.film(Film(240, events=events), windows)
        rec = self.rec(res)
        self.assertEqual(rec["rest_changed_frames"], 1)
        rep = rec["regions"][0]
        self.assertEqual((rep["changed_frames"], rep["times"], rep["median_interval_ms"]), (3, 3, 1000.0))
        self.assertEqual((rep["first_ms"], rep["last_ms"]), (500.0, 2500.0))
        run = self.gate(out)
        self.assertEqual([x["check"] for x in run["violations"]], ["rest-video"])
        self.assertEqual(run["violations"][0]["observed"], 1)
        self.assertTrue(any("region 1 [0.75, 0, 1, 0.1] changed in 3 frames of the film, within its allowance" in n
                            for n in run["notes"]))
        tighter = json.loads((out / "capture.json").read_text())["scenario"]
        tighter["steps"][0]["allow"]["regions"][0]["changed_frames_max"] = 2
        run = self.gate(out, tighter)
        self.assertEqual(sorted(x["check"] for x in run["violations"]), ["rest-region", "rest-video"])


IMAGE_OR_VIDEO_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".mp4", ".mov",
                           ".m4v", ".mkv", ".webm", ".avi", ".h264", ".hevc"}


def picture_bytes(head: bytes) -> bool:
    """The first bytes of an image or video file: PNG, JPEG, GIF, BMP, TIFF, RIFF (WebP, AVI), Matroska
    or WebM, an ISO media file (MP4, MOV, HEIC), a raw H.264 stream."""
    return (head.startswith((b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"BM", b"II*\x00", b"MM\x00*", b"RIFF",
                             b"\x1aE\xdf\xa3", b"\x00\x00\x00\x01", b"\x00\x00\x01"))
            or head[4:8] in (b"ftyp", b"moov", b"mdat", b"wide", b"free"))


class NoPicturesTest(Base):
    """`film --no-pictures`: the run folder holds no picture of any kind and no copy of the film, and
    the gate gives what it gives with pictures."""

    DIGIT = [(75, 100, (600, 900, 630, 950), 20)]

    def test_no_picture_and_the_same_verdict(self):
        film = Film(100, events=self.DIGIT)
        w = self.windows({"windows": WINDOW})
        bare, full = self.root / "bare", self.root / "full"
        with decoded(film):
            rc, text, err = main("film", self.video, "--windows", w, "--out", bare, "--no-pictures")
            self.assertEqual(rc, 0, err)
            self.assertEqual(main("film", self.video, "--windows", w, "--out", full)[0], 0)
        files = [f for f in bare.rglob("*") if f.is_file()]
        self.assertEqual(sorted(f.name for f in files), ["capture.json", "run.json"])
        for f in files:
            self.assertNotIn(f.suffix.lower(), IMAGE_OR_VIDEO_SUFFIXES, f)
            self.assertFalse(picture_bytes(f.read_bytes()[:16]), f)
        self.assertTrue(any(picture_bytes(f.read_bytes()[:16]) for f in full.rglob("*.png")))     # the check can see one
        self.assertFalse(json.loads((bare / "capture.json").read_text())["pictures"])
        a, b = (json.loads((d / "run.json").read_text())["steps"][0]["recording"] for d in (bare, full))
        self.assertEqual(a["drawing_frames"], b["drawing_frames"])
        self.assertEqual(a["drawing_frames"][0]["t_ms"], 1050.0)
        for key in ("frames", "changed_app", "scattered", "still", "largest_scattered_group_px", "min_group_px",
                    "steady_places", "scattered_frames", "signature"):
            self.assertEqual(a[key], b[key], key)
        self.assertEqual((a["sheet"], a["distinct_sheets"], a["frames_full"]), (None, [], None))
        ga, gb = self.gate(bare), self.gate(full)
        self.assertEqual((ga["verdict"], [(x["check"], x["observed"]) for x in ga["violations"]]),
                         (gb["verdict"], [(x["check"], x["observed"]) for x in gb["violations"]]))
        self.assertIn("made with --no-pictures", " ".join(ga["notes"]))
        self.assertIn("run.json gives each frame's time and boxes", ga["violations"][0]["message"])
        rc, text, _ = main("summary", bare)
        self.assertEqual(rc, 0)
        self.assertIn("sit.film_changed_app_frames", text)
        for verb in (("zoom", bare, 74, 75), ("sheet", bare)):
            rc, _, err = main(*verb)
            self.assertEqual(rc, 2)
            self.assertEqual(err.strip().splitlines(), [f"ui-probe: {verb[0]}: {bare} was made with --no-pictures and "
                                                        f"holds no picture; {verb[0]} would make one"])
        with decoded(film):
            rc, _, err = main("analyze", bare)
        self.assertEqual(rc, 0, err)
        self.assertEqual(sorted(f.name for f in bare.rglob("*") if f.is_file()), ["capture.json", "run.json"])

    def test_the_film_is_read_from_its_path_and_checked(self):
        out, _ = self.film(Film(100), name="r")
        kept = (out / "run.json").read_bytes()
        self.video.write_bytes(b"another film at the same path")
        with decoded(Film(100)):
            rc, _, err = main("analyze", out)
        self.assertEqual(rc, 2)
        self.assertIn("is not the film this run was made from", err)
        self.assertEqual((out / "run.json").read_bytes(), kept)          # a refused re-analysis loses nothing
        self.video.unlink()
        rc, _, err = main("zoom", out, 1, 2)
        self.assertEqual(rc, 2)
        self.assertIn("named by capture.json is not there; a film run keeps no copy of it", err)


class SlowInsideARegionTest(Base):
    def test_a_slow_change_inside_a_region_is_excused(self):
        region = {"region": [0.75, 0.0, 1.0, 0.1], "changed_frames_max": 3, "reason": "a progress ring fills"}
        fade = lambda i, f: add(f, (900, 60, 1000, 160), -max(0, i - 20))         # noqa: E731  inside the region
        windows = [{**WINDOW[0], "allow": {"regions": [region]}}]
        out, res = self.film(painted(Film(100), fade), windows)
        rec = self.rec(res)
        self.assertEqual((rec["changed_by_slow_only"], rec["rest_changed_frames"], rec["ends"]["differ"],
                          rec["ends"]["differ_unexplained"]), (0, 0, True, False))
        self.assertEqual(self.gate(out)["verdict"], "PASS")
        out, res = self.film(painted(Film(100), fade), name="no-region")
        self.assertGreater(self.rec(res)["changed_by_slow_only"], 0)            # the same fade, not excused


class WindowsAgainstTheFilmTest(Base):
    def test_a_change_at_60_s_in_a_70_s_watched_rest(self):
        film = Film(150, fps=2.0, key=30, events=[(120, 150, (100, 1000, 200, 1100), 0)])
        out, res = self.film(film, [{"id": "sit", "start_s": 2.0, "end_s": 72.0, "watch": True}])
        self.assertEqual([f["t_ms"] for f in self.rec(res)["drawing_frames"]], [58000.0])
        self.assertTrue(res["steps"][0]["watch"])
        self.assertEqual(self.gate(out)["violations"][0]["check"], "rest-video")

    def test_a_watched_window_of_69_s_is_refused(self):
        with self.assertRaises(u.ProbeError) as ctx:
            self.film(Film(150, fps=2.0), [{"id": "sit", "start_s": 2.0, "end_s": 71.0, "watch": True}])
        self.assertIn("a watched rest lasts at least 70000 ms", str(ctx.exception))
        self.assertFalse((self.root / "film-run").exists())

    def test_a_window_past_the_films_end_is_refused(self):
        with self.assertRaises(u.ProbeError) as ctx:
            self.film(Film(120), [{"id": "sit", "start_s": 0.5, "end_s": 80.5, "watch": True}])
        self.assertIn("window 'sit' ends at 80.5 s, beyond the film's last frame at 1.983 s by more than one frame "
                      "interval (16.7 ms)", str(ctx.exception))
        with self.assertRaises(u.ProbeError) as ctx:
            self.film(Film(120), [{"id": "late", "start_s": 5, "end_s": 9}], name="late")
        self.assertIn("window 'late' starts at 5 s", str(ctx.exception))
        _out, res = self.film(Film(120), [{"id": "sit", "start_s": 0.5, "end_s": 2.0}], name="edge")   # within one
        self.assertEqual(self.rec(res)["last_frame"], 119)


# --------------------------------------------------------------------------------------------
# the windows file, the folder, and the verbs that read it
# --------------------------------------------------------------------------------------------

class WindowsFileTest(Base):
    def refused(self, data):
        with self.assertRaises(u.ProbeError) as ctx:
            u.load_windows(str(self.windows(data)), "x")
        return str(ctx.exception)

    def test_shapes(self):
        scn, insets, _options = u.load_windows(str(self.windows([{"id": "a", "start_s": 0, "end_s": 1}])), "cap")
        self.assertEqual((scn["name"], scn["film"], scn["steps"][0]["do"], insets), ("cap", True, "rest", [0, 0]))
        scn, insets, _options = u.load_windows(str(self.windows({"name": "n", "insets_px": [10, 5], "watch_min_ms": 20000,
                                                       "windows": [{"id": "a", "start_s": 0, "end_s": 1}]})), "cap")
        self.assertEqual((scn["name"], scn["watch_min_ms"], insets), ("n", 20000, [10, 5]))

    def test_refusals(self):
        self.assertIn("overlap", self.refused([{"id": "a", "start_s": 0, "end_s": 2}, {"id": "b", "start_s": 1, "end_s": 3}]))
        self.assertIn("do not apply to a film", self.refused([{"id": "a", "start_s": 0, "end_s": 2,
                                                                "allow": {"frames_max": 3, "reason": "x"}}]))
        self.assertIn("0 <= start_s < end_s", self.refused([{"id": "a", "start_s": 2, "end_s": 2}]))
        self.assertIn("do not apply to a film's window", self.refused([{"id": "a", "start_s": 0, "end_s": 2, "x": 1}]))
        self.assertIn("a film's window is a rest", self.refused([{"id": "a", "do": "tap", "start_s": 0, "end_s": 2}]))
        self.assertIn("'insets_px' must be", self.refused({"insets_px": [1], "windows": []}))
        self.assertIn("unknown keys ['scope']", self.refused({"scope": 1, "windows": []}))
        self.assertIn("a film needs at least one window", self.refused([]))
        self.assertIn("'allow.reason' is required", self.refused([{"id": "a", "start_s": 0, "end_s": 2,
                                                                  "allow": {"changed_frames_max": 1}}]))

    def test_a_film_scenario_is_not_a_device_scenario(self):
        scn, _insets, _options = u.load_windows(str(self.windows([{"id": "a", "start_s": 0, "end_s": 1}])), "cap")
        for platform in (u.ANDROID, u.SIM_PLATFORM):
            with self.assertRaises(u.ProbeError) as ctx:
                u.validate_scenario(scn, platform)
            self.assertIn("which no device runs", str(ctx.exception))
        with self.assertRaises(u.ProbeError):
            u.validate_scenario({"name": "x", "package": "com.example.app", "steps": [{"id": "a", "do": "rest", "ms": 1}]},
                                u.FILM_PLATFORM)


class FilmVerbTest(Base):
    DIGIT = [(75, 100, (600, 900, 630, 950), 20)]

    def test_film_gate_summary_zoom(self):
        film = Film(100, events=self.DIGIT)
        w = self.windows({"windows": WINDOW})
        out = self.root / "f"
        with decoded(film):
            rc, text, err = main("film", self.video, "--windows", w, "--out", out)
        self.assertEqual(rc, 0, err)
        first = json.loads(text.splitlines()[0])
        self.assertEqual((first["window"], first["changed_app"], first["scattered"], first["min_group_px"]),
                         ("sit", 1, 83, MIN))
        self.assertIn("left out: sit: 83 of 84 frames held only scattered changes", text)
        cap = json.loads((out / "capture.json").read_text())
        import hashlib
        self.assertEqual((cap["platform"], cap["pictures"], cap["film"]),
                         ("film", True, {"path": str(self.video.resolve()), "bytes": self.video.stat().st_size,
                                         "sha256": hashlib.sha256(self.video.read_bytes()).hexdigest()}))
        self.assertFalse((out / "raw").exists())                     # the film is never copied
        for name in ("run.json", "sheets/sit.png", "sheets/sit-distinct-01.png", "frames-full/sit/f000075.png"):
            self.assertTrue((out / name).exists(), name)
        rc, text, _ = main("gate", out)
        self.assertEqual(rc, 1)
        self.assertIn("FAIL [rest-video] sit: the screen changed at rest: 1 frames of the film", text)
        rc, text, _ = main("summary", out)
        self.assertEqual(rc, 0)
        for key in ("sit.film_changed_app_frames", "sit.film_scattered_frames", "sit.film_largest_scattered_group_px"):
            self.assertIn(key, text)
        with mock.patch.object(v, "zoom", return_value={"changed_pixels": 1}) as z:
            rc, _, _ = main("zoom", out, 74, 75)
        self.assertEqual(rc, 0)
        self.assertEqual(z.call_args.args[:3], (self.video.resolve(), 74, 75))
        rc, _, err = main("zoom", out, 74, 75, "--step", "sit")
        self.assertEqual(rc, 2)
        self.assertIn("--step applies only to a simulator run", err)
        with decoded(film):
            rc, _, err = main("film", self.video, "--windows", w, "--out", out)
        self.assertEqual(rc, 2)
        self.assertIn("already holds a run", err)

    def test_an_allowance_passes_and_says_so(self):
        out, _ = self.film(Film(100, events=self.DIGIT), [{**WINDOW[0], "allow": {"changed_frames_max": 1,
                                                                                    "reason": "one toast"}}])
        run = self.gate(out)
        self.assertEqual(run["verdict"], "PASS")
        self.assertTrue(any("passed on the step's allowance of 1 (one toast)" in n for n in run["notes"]))

    def test_a_refused_window_leaves_a_record(self):
        out = self.root / "f"
        with decoded(Film(60)):
            rc, _, err = main("film", self.video, "--windows", self.windows([{"id": "late", "start_s": 5, "end_s": 9}]),
                              "--out", out)
        self.assertEqual(rc, 2)
        self.assertIn("window 'late' starts at 5 s, beyond the film's last frame", err)
        self.assertTrue((out / u.FAILED_RUN).exists())
        failed = self.gate(out)["violations"][0]
        self.assertEqual(failed["check"], "analysis")
        self.assertIn("beyond the film's last frame", failed["message"])

    def test_sheet_redoes_it(self):
        film = Film(100, events=self.DIGIT)
        out, _ = self.film(film)
        (out / "run.json").unlink()
        with decoded(film):
            rc, text, err = main("sheet", out, "--full", "--distinct")
        self.assertEqual(rc, 0, err)
        self.assertIn("add nothing", text)
        self.assertEqual(json.loads((out / "run.json").read_text())["steps"][0]["recording"]["changed_app"], 1)

    def test_a_low_watch_minimum_is_named_by_the_gate(self):
        out, _ = self.film(Film(100), {"watch_min_ms": 20000, "windows": WINDOW})
        self.assertIn("the scenario's watch_min_ms is 20000 ms, below the default of 70000 ms",
                      " ".join(self.gate(out)["notes"]))
        out, _ = self.film(Film(100), name="plain")
        self.assertNotIn("watch_min_ms", " ".join(self.gate(out)["notes"]))


# --------------------------------------------------------------------------------------------
# sheet --full --distinct on a run, and the decoder's count
# --------------------------------------------------------------------------------------------

def save_frames(root, frames):
    from PIL import Image
    root.mkdir(parents=True, exist_ok=True)
    for n, f in enumerate(frames):
        Image.fromarray(f).save(root / f"f{n:05d}.png")


def encode(root, frames, out, fps):
    save_frames(root, frames)
    subprocess.run(["ffmpeg", "-v", "error", "-framerate", str(fps), "-i", str(root / "f%05d.png"), "-c:v", "png",
                    "-f", "matroska", str(out)], check=True)


@unittest.skipUnless(HAS_FFMPEG, "no ffmpeg")
class SheetFullDistinctTest(unittest.TestCase):
    """A simulator run's step recording of 30 frames: frame 0 is the screen as the recording began,
    frames 1 to 26 each move a bar, 27 to 29 repeat 26."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.run = self.root / "run-01"
        raw = self.run / "raw"
        raw.mkdir(parents=True)
        frames = []
        for i in range(30):
            f = np.full((400, 240, 3), 230, dtype=np.uint8)
            k = min(i, 26)
            f[100:160, 10 + 8 * (k % 26): 18 + 8 * (k % 26)] = 0 if k else 230
            frames.append(f)
        self.video = raw / "rec-sit.mp4"            # Matroska inside; ffmpeg reads it by its content
        encode(self.root / "src", frames, self.video, 10)
        step = {"id": "sit", "do": "rest", "ms": 3000}
        rec = u.sim_recording_counts(self.video, self.run / "video" / "sit", step, "S")
        self.assertEqual((rec["frames"], rec["identical"]), (29, 3))
        (self.run / "capture.json").write_text(json.dumps({"platform": u.SIM_PLATFORM}))
        (self.run / "run.json").write_text(json.dumps({"scenario": "S", "steps": [
            {"id": "launch", "kind": "launch"}, {"id": "sit", "kind": "rest", "recording": rec}]}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_and_distinct(self):
        lines = u.sheet_extras(self.run, True, True)
        full = sorted((self.run / "frames-full" / "sit").glob("f*.png"))
        self.assertEqual([p.name for p in full], [f"f{i:06d}.png" for i in range(30)])
        from PIL import Image
        self.assertEqual(Image.open(full[5]).size, (240, 400))
        pages = sorted((self.run / "sheets").glob("sit-distinct-*.png"))
        self.assertEqual([p.name for p in pages], ["sit-distinct-01.png", "sit-distinct-02.png"])   # 26 = 24 + 2
        self.assertIn("sit: 30 full-resolution frames in frames-full/sit/ (frames 0 to 29; #0 is the last frame "
                      "before the step)", lines)
        self.assertIn("sit: 26 frames changed the app's area: sheets/sit-distinct-01.png, sheets/sit-distinct-02.png",
                      lines)

    def test_distinct_alone_uses_the_thumbnails(self):
        u.sheet_extras(self.run, False, True)
        self.assertFalse((self.run / "frames-full").exists())
        self.assertEqual(len(list((self.run / "sheets").glob("sit-distinct-*.png"))), 2)

    def test_refused_when_the_recordings_count_differs_from_the_analysis(self):
        with mock.patch.object(v, "probe_pts", return_value=[0.1 * i for i in range(31)]):
            with self.assertRaises(u.ProbeError) as ctx:
                u.sheet_extras(self.run, True, True)
        self.assertIn("the recording's own list holds 31 frames but the analysis read 30", str(ctx.exception))
        self.assertFalse((self.run / "frames-full").exists())

    def test_refused_when_fewer_frames_are_extracted(self):
        real_run = v._run

        def short(cmd):
            out = real_run(cmd)
            if cmd[0] == "ffmpeg":
                sorted(Path(cmd[-1]).parent.glob("x*.png"))[-1].unlink()
            return out

        with mock.patch.object(v, "_run", side_effect=short):
            with self.assertRaises(u.ProbeError) as ctx:
                u.sheet_extras(self.run, True, False)
        self.assertIn("29 frames were extracted where the recording's own list has 30", str(ctx.exception))

    def test_the_command(self):
        sheet = mock.patch.object(u, "analyze_run")      # the run here is not a whole capture: skip the redo
        with sheet:
            rc, text, err = main("sheet", self.run, "--full", "--distinct")
        self.assertEqual(rc, 0, err)
        self.assertIn("26 frames changed the app's area", text)

    def test_android_windows_and_the_frame_before(self):
        run = self.root / "run-02"
        (run / "raw").mkdir(parents=True)
        shutil.copyfile(self.video, run / "raw" / "screen.mp4")
        frames = [{"i": i, "t": 10.0 + 0.1 * i, "app_changed": 1 <= i <= 26} for i in range(30)]
        (run / "video-frames.json").write_text(json.dumps(frames))
        (run / "capture.json").write_text(json.dumps({"platform": u.ANDROID}))
        (run / "run.json").write_text(json.dumps({"scenario": "S", "video": {"steps": [
            {"id": "launch", "start_s": 9.0, "first_frame": 0, "last_frame": 4},
            {"id": "sit", "start_s": 10.5, "first_frame": 5, "last_frame": 12},
            {"id": "gone", "start_s": 20.0, "first_frame": None, "last_frame": None}]}}))
        lines = u.sheet_extras(run, True, True)
        self.assertEqual(sorted(p.name for p in (run / "frames-full" / "launch").glob("*.png")),
                         [f"f{i:06d}.png" for i in range(0, 5)])
        self.assertEqual(sorted(p.name for p in (run / "frames-full" / "sit").glob("*.png")),
                         [f"f{i:06d}.png" for i in range(4, 13)])
        self.assertIn("sit: 9 full-resolution frames in frames-full/sit/ (frames 4 to 12; #4 is the last frame before "
                      "the step)", lines)
        self.assertIn("launch: 5 full-resolution frames in frames-full/launch/ (frames 0 to 4)", lines)
        self.assertFalse((run / "frames-full" / "gone").exists())


@unittest.skipUnless(HAS_FFMPEG, "no ffmpeg")
class RealFilmTest(unittest.TestCase):
    """The film verb on a real file: 70 frames of the synthetic capture at 60 a second, with one digit
    at frame 45, then gate and zoom on what it wrote."""

    def test_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            film = Film(70, events=[(45, 70, (600, 900, 630, 950), 20)])
            video = root / "capture.mkv"
            encode(root / "src", [film.frame(i) for i in range(70)], video, 60)
            self.assertEqual(len(v.probe_pts(video)), 70)
            self.assertEqual(v.probe_size(video), (FW, FH))
            got = [i for i, _ in v.iter_frames(video, [3, 4, 5, 40], (FW, FH))]
            self.assertEqual(got, [3, 4, 5, 40])
            self.assertTrue(np.array_equal(next(v.iter_frames(video, [61], (FW, FH)))[1], film.frame(61)))
            windows = root / "w.json"
            windows.write_text(json.dumps([{"id": "sit", "start_s": 0.1, "end_s": 1.15}]))
            out = root / "f"
            rc, text, err = main("film", video, "--windows", windows, "--out", out)
            self.assertEqual(rc, 0, err)
            rec = json.loads((out / "run.json").read_text())["steps"][0]["recording"]
            self.assertEqual([f["i"] for f in rec["drawing_frames"]], [45])
            self.assertEqual((rec["largest_scattered_group_px"], rec["scattered"]), (40, rec["frames"] - 1))
            rc, text, _ = main("gate", out)
            self.assertEqual(rc, 1)
            self.assertIn("1 frames of the film changed the app's area", text)
            rc, text, _ = main("zoom", out, 44, 45)
            self.assertEqual(rc, 0)
            self.assertGreater(json.loads(text)["changed_pixels"], 1500)

    def test_the_decoder_refuses_a_count_it_cannot_give(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "c.mkv"
            encode(root / "src", [Film(5, key=None).frame(i) for i in range(5)], video, 60)
            with self.assertRaises(v.VideoError) as ctx:
                list(v.iter_frames(video, [3, 4, 5, 6], (FW, FH)))
            self.assertIn("the decoder gave 2 of the 4 frames asked for", str(ctx.exception))


class HelpersTest(unittest.TestCase):
    def test_frame_ranges(self):
        self.assertEqual(v.frame_ranges([5, 1, 2, 3, 9, 10, 2]), [(1, 3), (5, 5), (9, 10)])
        self.assertEqual(v._select([1, 2, 7]), "select='between(n\\,1\\,2)+between(n\\,7\\,7)'")

    def test_distinct_pages_hold_24(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            tiles = [(Image.new("RGB", (540, 1200), "white"), f"#{i}") for i in range(49)]
            pages = v.distinct_pages(tiles, Path(tmp), "s", "t")
            self.assertEqual(pages, ["s-distinct-01.png", "s-distinct-02.png", "s-distinct-03.png"])
            self.assertEqual(v.PER_PAGE, 24)
            self.assertEqual(v.distinct_pages(tiles[:3], Path(tmp), "s", "t"), ["s-distinct-01.png"])
            self.assertEqual(sorted(p.name for p in Path(tmp).glob("s-distinct-*")), ["s-distinct-01.png"])


class DocsTest(unittest.TestCase):
    def test_documented(self):
        root = Path(u.HERE).parent
        readme = " ".join((root / "README.md").read_text().split())
        for phrase in ("## A film no run made: `ui-probe film`", "## Regions that change on their own", "## Watched rests",
                       "`sheet <run-dir> --full --distinct`", "| Region at rest (`rest-region`) |",
                       "frames-full/<step>/", "sheets/<step>-distinct-NN.png", "it must last at least 70 s",
                       "one phone capture, H.264, 60 a second", "a connected group of pixels",
                       "**A film is judged by its picture alone.**"):
            self.assertIn(phrase, readme, phrase)
        self.assertIn("ui-probe film", " ".join((root / "skills" / "ui-probe" / "SKILL.md").read_text().split()))
        self.assertIn("## 0.9.0", (root / "CHANGELOG.md").read_text())
        self.assertIn("### Validation, 0.9.0", (root / "PROVENANCE.md").read_text())
        self.assertIn("ui-probe 0.9.0", (root.parent.parent / "CHANGELOG.md").read_text())
        # the plugin's version is pinned by the newest release's test (test_outside_0100), the marketplace's never
        self.assertEqual((u.WATCH_MIN_MS, v.BLOCK_OF_WIDTH, v.BLOCK_PIXEL_LEVEL, v.BLOCK_SHARE), (70000, 24 / 1080, 10, 0.40))
        self.assertEqual((u.WATCH_MIN_FLOOR_MS, v.FILM_LEVEL, v.FILM_GROUP_SHARE, u.FILM_CAUTION_PCT / 100),
                         (10000, 10, 3e-5, v.FILM_CAUTION))


if __name__ == "__main__":
    unittest.main()
