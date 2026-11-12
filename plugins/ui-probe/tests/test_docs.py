"""The README and the skill say what the code does (review findings 25 to 31), and the plugin keeps
its own naming rules (finding 33). Whitespace is normalised, so line wrapping does not matter."""
import sys

sys.dont_write_bytecode = True

import re
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import ui_probe as u  # noqa: E402

PLUGIN = Path(__file__).resolve().parent.parent


def text(rel):
    return " ".join((PLUGIN / rel).read_text().split())


README = text("README.md")
SKILL = text("skills/ui-probe/SKILL.md")


class ReadmeMatchesCodeTest(unittest.TestCase):
    def test_25_largest_count_decides_everywhere(self):
        self.assertIn("the largest available count decides, for the rest checks and for `expect` alike", README)

    def test_26_no_count_and_no_aligned_recording_fails(self):
        self.assertIn("If no count and no aligned recording exist, the gate fails", README)
        self.assertIn("is unknown, never zero", README)

    def test_27_debuggable_and_sections(self):
        self.assertNotIn("No root, no debuggable build and nothing added to the app are needed", README)
        self.assertIn("recorded on Android 14 from a build that was neither debuggable nor profileable", README)
        self.assertIn("reported as not observed", README)

    def test_28_quiet_wording(self):
        for doc in (README, SKILL):
            self.assertNotIn("stop drawing", doc)
        self.assertIn("whether the app's own frame count kept changing, or only the display's", README)
        self.assertIn("the display kept changing while the app was idle", SKILL)
        self.assertIn("the display kept changing while the app was idle (a system animation, the keyboard, another window)",
                      u.QUIET_MESSAGES["display"])

    def test_29_five_runs_a_side(self):
        self.assertIn("five against five is the least at which `compare` can say `different`", SKILL)
        self.assertIn("five against five with no overlap gives p = 2/252", README)

    def test_30_proxy_on_a_physical_device(self):
        self.assertIn("`--bind <host-address>`", README)
        self.assertIn("adb -s <serial> reverse tcp:8899 tcp:8899", README)
        self.assertIn("adb reverse", SKILL)

    def test_31_files_sheet_and_ffmpeg(self):
        for name in ("frames/", "video-frames.json", "cpu-<step>.after.txt", "sr-exit.txt", "trace.pftrace",
                     "sheets/zoom-<a>-<b>.png"):
            self.assertIn(name, README)
        self.assertIn("`sheet <run-dir>` does the same for one run", README)
        self.assertIn("`ffmpeg` 5.1 or newer", README)
        self.assertEqual(u.FFMPEG_MIN, (5, 1))
        help_text = u.main.__code__.co_consts
        self.assertTrue(any(isinstance(c, str) and c.startswith("redo the analysis of one saved run") for c in help_text))

    def test_install_line(self):
        """The form the plugin command line accepts: plugin@marketplace, no --marketplace option."""
        readme = (PLUGIN / "README.md").read_text()
        self.assertIn("claude plugin install ui-probe@agent-marketplace\n", readme)
        self.assertNotIn("--marketplace", readme)

    def test_presence_and_loss_checks_documented(self):
        self.assertIn("| Foreground | another app's activity was the resumed one at the end of launch or of a rest step", README)
        self.assertIn('mark the rest `"background": true`', README)
        self.assertIn("no frame of the app was counted while it launched", README)
        self.assertIn("had stopped before the scenario ended", README)
        self.assertIn("A trace's counts are used only if the trace is whole", README)
        for name in ("fg-<step>.txt", "sr-alive.txt"):
            self.assertIn(name, README)
        self.assertIn("prints `listening on <address>:<port>` once the port is bound", README)
        self.assertIn("Only an aligned recording decides, and only while the app is in front", README)
        self.assertIn("wait for its `listening on` line", SKILL)
        self.assertIn('marked "open when the proxy stopped"', README)
        self.assertIn("stop the proxy first and then confirm its log names the app's hosts", SKILL)
        for phrase in ("another app was in front", "no frame of the app was counted while it launched",
                       "the recorder had stopped before the scenario ended", "A trace that lost data"):
            self.assertIn(phrase, SKILL)

    def test_020_additions_documented(self):
        self.assertIn("## Screens that are never still", README)
        # 0.6.0: an allowance of their own, never reached by frames_max or changed_frames_max
        self.assertIn("Recorded frames that changed nothing are allowed only by `unchanged_frames_max`", README)
        self.assertNotIn("identical_frames_max", " ".join(u.ALLOW_LIMITS))
        self.assertIn("a blinking caret drew 10 or 11 frames in a 5 s rest window (9 of 9 windows)", README)
        self.assertIn("produced 1 frame by the platform's count, 9 recorded frames that changed nothing and 9 CPU ticks",
                      README)
        self.assertIn("That is also why `find` is not offered on a rest", README)
        self.assertIn('`"until_quiet": false` with a `settle_ms` that covers the transition', README)
        self.assertIn("the poll that watches for quiet takes about as long as the gap between two", README)
        self.assertNotIn('"quiet": {"ms": 400}', README)        # measured: it times out between blinks
        self.assertIn("Limits come from the step's `allow`, then the command line", README)
        for name in ("find-<step>.txt", "ui-<step>.xml", "gfx-<step>.base.txt", "lookup-failed.txt", "failed.json"):
            self.assertIn(name, README)
        self.assertNotIn("find-failed.txt", README)
        self.assertIn('{"id": "type-code", "do": "text", "text": "000000"}', README)
        self.assertEqual(u.default_settle_ms("text"), 2500)
        self.assertIn("It takes the keys a `key` step takes and the same fixed wait (2500 ms)", README)
        self.assertEqual(u.FIND_SETTLE_MS, 1000)
        self.assertIn("`find_settle_ms` (default 1000)", README)
        self.assertIn("`rest`, `tap`, `swipe`, `key`, `text` (types into the focused field) and `url` (opens a", " ".join(SKILL.split()))  # 0.8.0
        self.assertEqual(u.STEP_KINDS, ("rest", "tap", "swipe", "key", "text", "url"))

    def test_exit_codes_documented(self):
        self.assertIn("| 1 | the gate found a violation (and only that) |", README)
        self.assertIn("| 3 | an unexpected error", README)


class NamingRulesTest(unittest.TestCase):
    """Finding 33: no instance identifiers and no UI framework names in the plugin."""

    def test_no_serials_or_framework_names(self):
        pattern = re.compile(r"emulator-\d+|#compose\b|\bjetpack\b|\bflutter\b|\breact native\b|\bswiftui\b", re.I)
        hits = []
        for path in PLUGIN.rglob("*"):
            if path.is_file() and path.suffix in (".py", ".md", ".json", ".sh") and path.name != "test_docs.py":
                for n, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
                    if pattern.search(line):
                        hits.append(f"{path.relative_to(PLUGIN)}:{n}: {line.strip()}")
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
