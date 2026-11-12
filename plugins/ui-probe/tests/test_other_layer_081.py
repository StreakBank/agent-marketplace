"""0.8.1: on Android a recorded frame that changed nothing is held against the app only as far as the app's own
layers can account for it. With L = frames_layers, D = frames_display, A = the recorded frames that changed the
app's area (app_region_frames: any change there, a caret's included) and I = the frames that changed nothing,
the app may own min(I, max(0, L - A)) of them; of the rest, at most max(0, D - L) are set aside, with a note.
Nothing is set aside when L, D or A is unknown. Built from the counts of two rests on a focused field on an
Android 11 emulator (L 10, D 11, A 10, I 1), and of a 150 s rest on Android 14 where the app's own frames
changed nothing (L 4, D 9, A 2, I 2) and must still fail."""
import sys

sys.dont_write_bytecode = True

import json
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import test_gate as tg  # noqa: E402
import ui_probe as u  # noqa: E402

REASON = "the caret of the focused field blinks twice a second"
SCN = {"name": "Example", "package": tg.PKG, "steps": [
    {"id": "open", "do": "tap", "x": 0.5, "y": 0.5},
    {"id": "idle", "do": "rest", "ms": 2000, "allow": {"frames_max": 12, "changed_frames_max": 12, "reason": REASON}}]}


def gate(identical, region=0, real=0, scn=SCN, video_kw=None, **rest_kw):
    vs = tg.vstep("idle", identical=identical, region=region, real=real)
    vs.update(video_kw or {})
    run = tg.make_run(tg.base_steps(**rest_kw), [vs])
    return u.gate_run(run, scn, dict(tg.DEFAULTS))


def video_failures(res):
    return [v for v in res["violations"] if v["check"] == "rest-video"]


def aside_notes(res):
    return [n for n in res["notes"] if "set aside: the display composed" in n]


ANDROID_11 = {"frames": 10, "frames_layers": 10, "frames_display": 11}


class SetAsideTest(unittest.TestCase):
    def test_the_android_11_rest_passes_with_a_note(self):
        # L 10, D 11, A 10 (the caret's blinks changed the app's area), I 1
        res = gate(1, region=10, **ANDROID_11)
        self.assertEqual((res["verdict"], video_failures(res)), ("PASS", []))
        self.assertEqual(aside_notes(res), ["idle: 1 frame of 1 composed at rest that changed nothing on screen was set "
                                            "aside: the display composed 11 frames and the app's own layers 10, so 1 "
                                            "frame was composed for a layer that is not the app's"])

    def test_the_long_rest_still_fails(self):
        # tmp/sb120/runs/and-today-long-rest-20261112T001228Z, rest-today-150s: L 4, D 9, A 2 (1 of them a real change),
        # I 2. The app drew two frames after each of two re-syncs and the second of each changed nothing: its own.
        res = gate(2, region=2, real=1, frames=4, frames_layers=4, frames_display=9)
        self.assertEqual([v["message"] for v in video_failures(res)],
                         ["2 frames were composed at rest that changed nothing on screen"])
        self.assertEqual(aside_notes(res), [])

    def test_more_than_the_difference_is_held_and_fails(self):
        # (b) L 10, D 11, A 10, two unchanged frames: one set aside, one held
        res = gate(2, region=10, **ANDROID_11)
        fails = video_failures(res)
        self.assertEqual([f["message"] for f in fails], ["1 frames were composed at rest that changed nothing on screen (1 "
                                                         "more set aside: the display composed 11 frames, the app's layers 10)"])
        self.assertEqual(fails[0]["observed"], 1)
        self.assertEqual(len(aside_notes(res)), 1)

    def test_the_apps_unaccounted_latches_own_unchanged_frames_first(self):
        # L 10, A 9: one of the app's latches changed nothing it recorded, so it owns one of the three; D - L = 2
        res = gate(3, region=9, frames=10, frames_layers=10, frames_display=12)
        self.assertEqual([v["message"] for v in video_failures(res)],
                         ["1 frames were composed at rest that changed nothing on screen (2 more set aside: the display "
                          "composed 12 frames, the app's layers 10)"])
        self.assertEqual(aside_notes(res), ["idle: 2 frames of 3 composed at rest that changed nothing on screen were set "
                                            "aside: the display composed 12 frames and the app's own layers 10, so 2 "
                                            "frames were composed for a layer that is not the app's"])

    def test_no_more_than_the_unchanged_frames_are_set_aside(self):
        res = gate(2, region=10, frames=10, frames_layers=10, frames_display=15)
        self.assertEqual((res["verdict"], video_failures(res)), ("PASS", []))
        self.assertEqual(aside_notes(res), ["idle: 2 frames of 2 composed at rest that changed nothing on screen were set "
                                            "aside: the display composed 15 frames and the app's own layers 10, so 5 "
                                            "frames were composed for a layer that is not the app's"])

    def test_an_animation_at_zero_alpha_still_fails(self):
        # (a) its latches change nothing it recorded (A 0), so they own the unchanged frames, whatever D is
        for kw in ({"frames": 10, "frames_layers": 10, "frames_display": 15},
                   {"frames": 10, "frames_layers": 10, "frames_display": 10},
                   {"frames": 10, "frames_layers": 11, "frames_display": 10}):
            res = gate(5, region=0, **kw)
            self.assertEqual([v["message"] for v in video_failures(res)],
                             ["5 frames were composed at rest that changed nothing on screen"], kw)
            self.assertEqual(aside_notes(res), [], kw)

    def test_unknown_counts_set_nothing_aside(self):
        # (c) L, D or A unknown, or not a count
        cases = [({"frames": 10, "frames_layers": 10, "frames_display": None}, None),
                 ({"frames": 10, "frames_layers": None, "frames_display": 11}, None),
                 ({"frames": 10, "frames_layers": 10}, None),
                 ({"frames": 10, "frames_layers": False, "frames_display": True}, None),
                 ({"frames": 10, "frames_layers": 10, "frames_display": 11.0}, None),
                 (ANDROID_11, {"app_region_frames": None}),
                 (ANDROID_11, {"app_region_frames": 10.0})]
        for kw, video_kw in cases:
            res = gate(1, region=10, video_kw=video_kw, **kw)
            self.assertEqual([v["message"] for v in video_failures(res)],
                             ["1 frames were composed at rest that changed nothing on screen"], (kw, video_kw))
            self.assertEqual(aside_notes(res), [], (kw, video_kw))
        # more unchanged frames than the app latched (the dump run's shape): an unknown A is not taken as 0
        res = gate(3, video_kw={"app_region_frames": None}, frames=1, frames_layers=1, frames_display=10)
        self.assertEqual([v["message"] for v in video_failures(res)],
                         ["3 frames were composed at rest that changed nothing on screen"])
        self.assertEqual(aside_notes(res), [])

    def test_an_allowance_holds_what_is_left(self):
        scn = json.loads(json.dumps(SCN))
        scn["steps"][1]["allow"]["unchanged_frames_max"] = 1
        res = gate(3, region=10, scn=scn, **ANDROID_11)
        self.assertEqual([v["message"] for v in video_failures(res)],
                         [f"2 frames were composed at rest that changed nothing on screen (1 more set aside: the display "
                          f"composed 11 frames, the app's layers 10), allowed 1 by the step's allowance ({REASON})"])
        res = gate(2, region=10, scn=scn, **ANDROID_11)
        self.assertEqual((res["verdict"], video_failures(res)), ("PASS", []))
        self.assertTrue(any(n.startswith("idle: 1 recorded frames changed nothing on screen at rest (1 more set aside")
                            for n in res["notes"]), res["notes"])
        # all of them set aside: the step did not pass on its allowance, so no allowance note
        res = gate(1, region=10, scn=scn, **ANDROID_11)
        self.assertEqual((res["verdict"], video_failures(res)), ("PASS", []))
        self.assertFalse(any("recorded frames changed nothing on screen at rest" in n for n in res["notes"]), res["notes"])
        self.assertEqual(len(aside_notes(res)), 1)
        # the state5 rest (L 2, D 3, A 1, I 1): the app owns its frame, and the allowance does its work again
        res = gate(1, region=1, scn=scn, frames=2, frames_layers=2, frames_display=3)
        self.assertEqual((res["verdict"], aside_notes(res)), ("PASS", []))
        self.assertTrue(any(n.startswith("idle: 1 recorded frames changed nothing on screen at rest, above the limit of 0")
                            for n in res["notes"]), res["notes"])

    def test_the_app_presenting_nothing_is_judged_as_before(self):
        # quiet_identical keeps its own note; nothing is set aside on top of it
        res = gate(2, frames=0, frames_layers=0, frames_display=3)
        self.assertEqual((res["verdict"], video_failures(res)), ("PASS", []))
        self.assertEqual(aside_notes(res), [])
        self.assertTrue(any("the app presented no frame by any count" in n for n in res["notes"]))

    def test_the_changed_frames_check_is_untouched(self):
        scn = json.loads(json.dumps(SCN))
        del scn["steps"][1]["allow"]["changed_frames_max"]
        res = gate(0, region=2, real=2, scn=scn, **ANDROID_11)
        self.assertEqual([v["message"] for v in video_failures(res)], ["the screen changed at rest: 2 frames in the app's area"])


class DocsTest(unittest.TestCase):
    def test_documented(self):
        root = Path(u.HERE).parent
        readme = " ".join((root / "README.md").read_text().split())
        for phrase in ("held against the app only as far as the app's own layers can account for it",
                       "frames_display", "set aside"):
            self.assertIn(phrase, readme, phrase)
        self.assertIn("## 0.8.1", (root / "CHANGELOG.md").read_text())
        self.assertIn("### Validation, 0.8.1", (root / "PROVENANCE.md").read_text())
        self.assertEqual(u.VERSION, "0.8.1")
        self.assertEqual(json.loads((root / ".claude-plugin" / "plugin.json").read_text())["version"], "0.8.1")
        market = json.loads((root.parent.parent / ".claude-plugin" / "marketplace.json").read_text())
        self.assertGreaterEqual(tuple(int(x) for x in market["metadata"]["version"].split(".")), (0, 13, 2))
        self.assertIn("## 0.13.2", (root.parent.parent / "CHANGELOG.md").read_text())


if __name__ == "__main__":
    unittest.main()
