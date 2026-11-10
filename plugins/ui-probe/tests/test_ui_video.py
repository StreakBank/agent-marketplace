"""ui_video.py: change detection, recorder timestamps and burst classes on synthetic frames.

Nothing here calls ffmpeg or ffprobe. Frames are greyscale float32 arrays, as analyze() builds them.
classify_burst receives frame dictionaries {'i': index into app, 't': seconds, 'app_changed': bool},
the frame before the burst and the list of app-area arrays.
"""
import sys

sys.dont_write_bytecode = True

import struct
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import ui_video as v  # noqa: E402

H, W = 40, 30


def grey(value=0.0):
    return np.full((H, W), value, dtype=np.float32)


class VisiblyChangedTest(unittest.TestCase):
    def test_scattered_single_pixels_are_noise(self):
        a = grey()
        b = a.copy()
        b[::5, ::5] = 60.0          # 48 isolated pixels, none with a changed neighbour
        self.assertGreater(int((np.abs(a - b) > v.VISIBLE_PIXEL).sum()), v.VISIBLE_COUNT)
        self.assertFalse(v.visibly_changed(a, b))

    def test_dense_block_is_a_change(self):
        a = grey()
        b = a.copy()
        b[10:14, 10:14] = 60.0
        self.assertTrue(v.visibly_changed(a, b))

    def test_too_few_pixels(self):
        a = grey()
        b = a.copy()
        b[0, 0:v.VISIBLE_COUNT - 1] = 60.0
        self.assertFalse(v.visibly_changed(a, b))

    def test_below_pixel_threshold(self):
        self.assertFalse(v.visibly_changed(grey(), grey(v.VISIBLE_PIXEL)))
        self.assertTrue(v.visibly_changed(grey(), grey(v.VISIBLE_PIXEL + 1)))

    def test_identical(self):
        self.assertFalse(v.visibly_changed(grey(10), grey(10)))


class DistBboxTest(unittest.TestCase):
    def test_dist(self):
        self.assertEqual(v.dist(grey(0), grey(3)), 3.0)
        b = grey(0)
        b[:20, :] = 10.0
        self.assertAlmostEqual(v.dist(grey(0), b), 5.0)

    def test_bbox(self):
        a = grey()
        b = a.copy()
        b[8:16, 6:15] = 50.0
        self.assertEqual(v.bbox(a, b), [round(6 / W, 3), round(8 / H, 3), round(15 / W, 3), round(16 / H, 3)])
        self.assertIsNone(v.bbox(a, a.copy()))
        small = a.copy()
        small[0, 0] = v.CHANGED_PIXEL        # not above the threshold
        self.assertIsNone(v.bbox(a, small))


class RecorderTimestampsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def blob(self, data):
        p = Path(self.tmp.name) / "screen.mp4"
        p.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x07" * 50 + data + b"moov-tail" * 3)
        return p

    def test_v2(self):
        ns = [1_000_000_000, 1_500_000_000, 2_250_000_000]
        data = b"#VV1NSC0PET1ME2#" + struct.pack("<IqI", 2, -123456789, len(ns)) + struct.pack(f"<{len(ns)}q", *ns)
        got = v.recorder_timestamps(self.blob(data))
        self.assertEqual(len(got), 3)
        for g, want in zip(got, [1.0, 1.5, 2.25]):
            self.assertAlmostEqual(g, want, places=9)

    def test_v1(self):
        us = [1_000_000, 1_016_000, 1_033_000]
        data = b"#VV1NSC0PET1ME!#" + struct.pack("<I", len(us)) + struct.pack(f"<{len(us)}q", *us)
        got = v.recorder_timestamps(self.blob(data))
        for g, want in zip(got, [1.0, 1.016, 1.033]):
            self.assertAlmostEqual(g, want, places=9)

    def test_no_magic(self):
        self.assertIsNone(v.recorder_timestamps(self.blob(b"no metadata here")))

    def test_v2_magic_with_other_version(self):
        data = b"#VV1NSC0PET1ME2#" + struct.pack("<IqI", 3, 0, 1) + struct.pack("<q", 5)
        self.assertIsNone(v.recorder_timestamps(self.blob(data)))

    def test_zero_count(self):
        data = b"#VV1NSC0PET1ME!#" + struct.pack("<I", 0)
        self.assertIsNone(v.recorder_timestamps(self.blob(data)))

    def test_count_past_end_of_file(self):
        """Fixed: a count larger than the bytes that follow raises struct.error instead of
        returning None (analyze() would then fail instead of falling back to frame matching)."""
        p = Path(self.tmp.name) / "short.mp4"
        p.write_bytes(b"#VV1NSC0PET1ME!#" + struct.pack("<I", 1000) + struct.pack("<q", 5))
        self.assertIsNone(v.recorder_timestamps(p))


def frames_for(indices, t0=10.0, step_s=0.016, changed=True):
    return [{"i": i, "t": round(t0 + k * step_s, 6), "app_changed": changed} for k, i in enumerate(indices)]


A_VALUE, B_VALUE, C_VALUE = 0.0, 100.0, 200.0


class ClassifyBurstTest(unittest.TestCase):
    def classify(self, kind, app, changed=True):
        frames = frames_for(range(1, len(app)), changed=changed)
        return v.classify_burst(kind, frames, app[0], app)

    def blend(self):
        return [grey(A_VALUE + (B_VALUE - A_VALUE) * k / 4) for k in range(5)]   # A .. B in four steps

    def test_transition(self):
        info = self.classify("tap", self.blend())
        self.assertEqual(info["class"], "T")
        self.assertEqual(info["detour_frames"], 0)
        self.assertEqual(info["n"], 4)
        self.assertEqual(info["d_start_end"], 100.0)
        self.assertEqual(info["duration_ms"], 48.0)
        self.assertEqual(info["bbox"], [0.0, 0.0, 1.0, 1.0])

    def test_transition_with_detour(self):
        app = [grey(A_VALUE), grey(C_VALUE), grey(B_VALUE)]
        frames = frames_for([1, 2], step_s=0.05)
        info = v.classify_burst("tap", frames, app[0], app)
        self.assertEqual(info["class"], "TD")
        self.assertEqual(info["detour_frames"], 1)
        self.assertEqual(info["detour_ms"], 50.0)
        self.assertEqual(frames[0]["detour_ratio"], 3.0)    # (200 + 100) / 100
        self.assertEqual(info["max_gap_ms"], 50.0)
        self.assertEqual(info["gaps_over_34ms"], 1)

    def test_excursion(self):
        a = grey(A_VALUE)
        c = a.copy()
        c[5:15, 5:15] = C_VALUE
        info = self.classify("tap", [a, c, a.copy()])
        self.assertEqual(info["class"], "E")
        self.assertEqual(info["d_start_end"], 0.0)
        self.assertEqual(info["bbox"], [round(5 / W, 3), round(5 / H, 3), round(15 / W, 3), round(15 / H, 3)])

    def test_minor_transition(self):
        info = self.classify("tap", [grey(A_VALUE), grey(A_VALUE + (v.MICRO_TAU + v.STATE_TAU) / 2)])
        self.assertEqual(info["class"], "t")

    def test_micro(self):
        info = self.classify("tap", [grey(A_VALUE), grey(A_VALUE + (v.NOISE_TAU + v.MICRO_TAU) / 2), grey(A_VALUE)])
        self.assertEqual(info["class"], "m")

    def test_noise(self):
        info = self.classify("tap", [grey(A_VALUE), grey(A_VALUE + v.NOISE_TAU / 2)])
        self.assertEqual(info["class"], "n")

    def test_system_only(self):
        info = self.classify("tap", self.blend(), changed=False)
        self.assertEqual(info["class"], "s")

    def test_swipe_is_motion(self):
        info = self.classify("swipe", self.blend())
        self.assertEqual(info["class"], "M")

    def test_swipe_judged_by_state_is_classed_like_a_tap(self):
        self.assertEqual(self.classify("drag-state", self.blend())["class"], "T")
        a = grey(A_VALUE)
        c = a.copy()
        c[5:15, 5:15] = C_VALUE
        self.assertEqual(self.classify("drag-state", [a, c, a.copy()])["class"], "E")

    def test_major_classes(self):
        self.assertEqual(set(v.MAJOR), {"T", "TD", "E", "M"})


if __name__ == "__main__":
    unittest.main()
