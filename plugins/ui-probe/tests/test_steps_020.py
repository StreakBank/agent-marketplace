"""The 0.2.0 additions: a text step, an allowance on a rest step, a per-step quiet window, and tap
targets found on the device. Validation, the device script (including the lookup run in the host's
POSIX shells against a stand-in for the window-tree dump), the analysis from the baseline, the gate
with an allowance, and the error the host reports when an element never appears. No device."""
import sys

sys.dont_write_bytecode = True

import contextlib
import copy
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import ui_probe as u  # noqa: E402
import ui_video  # noqa: E402
from test_analysis import (PID, PKG, SCN as ANALYSIS_SCN, Base, Capture, checks, cpu_text, gfx_text,  # noqa: E402
                           healthy_files, sf_text, steps_by_id)
from test_gate import DEFAULTS, base_steps, make_run, step, vstep  # noqa: E402
from test_run_and_cli import FakeDevice, main  # noqa: E402


def scenario(steps, **kw):
    return {"name": "Example", "package": PKG, "steps": steps, **kw}


def reject(case, scn, fragment):
    with case.assertRaises(u.ProbeError) as cm:
        u.validate_scenario(scn)
    case.assertIn(fragment, str(cm.exception))
    case.assertTrue(str(cm.exception).startswith("scenario: "), str(cm.exception))


REST_ALLOW = {"id": "rest-field", "do": "rest", "ms": 5000,
              "allow": {"frames_max": 12, "reason": "the caret blinks twice a second"}}


# --------------------------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------------------------

class TextStepValidationTest(unittest.TestCase):
    def test_valid(self):
        u.validate_scenario(scenario([{"id": "type-code", "do": "text", "text": "000000"}]))
        u.validate_scenario(scenario([{"id": "t", "do": "text", "text": "Jo Doe+1@example-mail.co_uk",
                                       "settle_ms": 500, "until_quiet": True, "quiet": {"ms": 400}}]))
        u.validate_scenario(scenario([{"id": "t", "do": "text", "text": "a" * 200}]))

    def test_listed_among_the_kinds(self):
        reject(self, scenario([{"id": "a", "do": "type"}]), "'do' must be one of rest, tap, swipe, key, text")

    def test_text_required(self):
        reject(self, scenario([{"id": "t", "do": "text"}]), "text needs a non-empty string 'text'")
        reject(self, scenario([{"id": "t", "do": "text", "text": ""}]), "text needs a non-empty string 'text'")
        reject(self, scenario([{"id": "t", "do": "text", "text": 123}]), "text needs a non-empty string 'text'")

    def test_length(self):
        reject(self, scenario([{"id": "t", "do": "text", "text": "a" * 201}]), "'text' is 201 characters; at most 200")

    def test_offending_character_is_named(self):
        for value, bad in (("a#b", "'#'"), ("50%", "'%'"), ("it's", '"\'"'), ("é", "'é'"), ("a\nb", "'\\n'"),
                           ('a"b', "'\"'"), ("a;reboot", "';'")):
            reject(self, scenario([{"id": "t", "do": "text", "text": value}]), f"{bad} is not allowed")
        reject(self, scenario([{"id": "t", "do": "text", "text": "a&b"}]),
               "'text' may hold only letters, digits, space and @ . _ + -")

    def test_keys_of_other_kinds_do_not_apply(self):
        reject(self, scenario([{"id": "t", "do": "text", "text": "a", "code": 4}]),
               "keys ['code'] do not apply to a 'text' step")
        reject(self, scenario([{"id": "t", "do": "text", "text": "a", "find": {"text": "x"}}]),
               "keys ['find'] do not apply to a 'text' step")


class AllowValidationTest(unittest.TestCase):
    def allow(self, allow):
        return scenario([{"id": "r", "do": "rest", "ms": 2000, "allow": allow}])

    def test_valid(self):
        u.validate_scenario(scenario([REST_ALLOW]))
        u.validate_scenario(self.allow({"changed_frames_max": 0, "reason": "x"}))
        u.validate_scenario(self.allow({"cpu_pct_max": 1.5, "frames_max": 0, "changed_frames_max": 2, "reason": "x"}))

    def test_rest_steps_only(self):
        reject(self, scenario([{"id": "a", "do": "tap", "x": 0.5, "y": 0.5, "allow": {"frames_max": 1, "reason": "x"}}]),
               "keys ['allow'] do not apply to a 'tap' step")

    def test_shape_and_unknown_keys(self):
        reject(self, self.allow([12]), "'allow' must be an object")
        reject(self, self.allow({"frames_max": 1, "reason": "x", "identical_frames_max": 3}),
               "unknown 'allow' keys ['identical_frames_max']")
        reject(self, self.allow({"frames_max": 1, "reason": "x", "comment": "y"}), "unknown 'allow' keys ['comment']")

    def test_reason_required(self):
        reject(self, self.allow({"frames_max": 1}), "'allow.reason' is required")
        reject(self, self.allow({"frames_max": 1, "reason": " "}), "'allow.reason' is required")
        reject(self, self.allow({"frames_max": 1, "reason": 3}), "'allow.reason' is required")

    def test_a_limit_required(self):
        reject(self, self.allow({"reason": "x"}), "'allow' needs at least one of frames_max, changed_frames_max, cpu_pct_max")

    def test_types(self):
        for key in ("frames_max", "changed_frames_max"):
            for bad in (-1, "3", True, 1.5):
                reject(self, self.allow({key: bad, "reason": "x"}), f"'allow.{key}' must be an integer >= 0")
        for bad in (-0.1, "2", True):
            reject(self, self.allow({"cpu_pct_max": bad, "reason": "x"}), "'allow.cpu_pct_max' must be a number >= 0")


class StepQuietValidationTest(unittest.TestCase):
    def test_valid_on_every_action_kind(self):
        for st in ({"id": "a", "do": "tap", "x": 0.5, "y": 0.5}, {"id": "a", "do": "swipe", "from": [0, 0], "to": [1, 1]},
                   {"id": "a", "do": "key", "code": 4}, {"id": "a", "do": "text", "text": "x"}):
            u.validate_scenario(scenario([{**st, "quiet": {"ms": 400, "max_ms": 9000}}], until_quiet=True))
            u.validate_scenario(scenario([{**st, "until_quiet": True, "quiet": {"ms": 400}}]))

    def test_not_on_a_rest(self):
        reject(self, scenario([{"id": "r", "do": "rest", "ms": 100, "quiet": {"ms": 400}}], until_quiet=True),
               "keys ['quiet'] do not apply to a 'rest' step")

    def test_same_checks_as_the_top_level_block(self):
        tap = {"id": "a", "do": "tap", "x": 0.5, "y": 0.5}
        reject(self, scenario([{**tap, "quiet": 400}], until_quiet=True), "step 'a': 'quiet' must be an object")
        reject(self, scenario([{**tap, "quiet": {"ms": 50}}], until_quiet=True),
               "step 'a': 'quiet.ms' must be a number of at least 100")
        reject(self, scenario([{**tap, "quiet": {"max_ms": "9000"}}], until_quiet=True),
               "step 'a': 'quiet.max_ms' must be a number of at least 100")
        reject(self, scenario([{**tap, "quiet": {"ms": 400, "window": 2}}], until_quiet=True),
               "step 'a': unknown 'quiet' keys ['window']")
        reject(self, scenario([{**tap, "quiet": {"ms": 900, "max_ms": 800}}], until_quiet=True),
               "step 'a': 'quiet.max_ms' must be at least 'quiet.ms'")

    def test_a_key_the_step_leaves_out_comes_from_the_scenario(self):
        tap = {"id": "a", "do": "tap", "x": 0.5, "y": 0.5}
        reject(self, scenario([{**tap, "quiet": {"ms": 600}}], until_quiet=True, quiet={"ms": 300, "max_ms": 500}),
               "'quiet.max_ms' must be at least 'quiet.ms' (500 and 600")
        u.validate_scenario(scenario([{**tap, "quiet": {"ms": 600, "max_ms": 700}}], until_quiet=True,
                                     quiet={"ms": 300, "max_ms": 500}))

    def test_a_step_that_never_waits_for_quiet_cannot_set_one(self):
        reject(self, scenario([{"id": "a", "do": "key", "code": 4, "quiet": {"ms": 400}}]),
               "step 'a': 'quiet' applies only to a step that waits for quiet")
        reject(self, scenario([{"id": "a", "do": "key", "code": 4, "until_quiet": False, "quiet": {"ms": 400}}],
                              until_quiet=True), "'quiet' applies only to a step that waits for quiet")


class FindValidationTest(unittest.TestCase):
    def tap(self, **kw):
        return scenario([{"id": "open", "do": "tap", **kw}])

    def test_valid(self):
        u.validate_scenario(self.tap(find={"text": "Sign up"}))
        u.validate_scenario(self.tap(find={"desc": "Phone number", "nth": 1, "timeout_ms": 15000}, find_settle_ms=0))
        u.validate_scenario(self.tap(find={"text": 'What\'s "this" & <that> — now'}))
        u.validate_scenario(self.tap(find={"text": "a" * 200}))

    def test_find_or_coordinates(self):
        reject(self, self.tap(find={"text": "x"}, x=0.5, y=0.5), "a tap takes either 'find' or 'x' and 'y', not both")
        reject(self, self.tap(find={"text": "x"}, x=0.5), "a tap takes either 'find' or 'x' and 'y', not both")
        reject(self, self.tap(), "tap needs 'x' and 'y' between 0 and 1, or 'find'")

    def test_exactly_one_of_text_or_desc(self):
        reject(self, self.tap(find={"text": "x", "desc": "y"}), "'find' needs exactly one of 'text' or 'desc'")
        reject(self, self.tap(find={"nth": 1}), "'find' needs exactly one of 'text' or 'desc'")
        reject(self, self.tap(find="Sign up"), "'find' must be an object")

    def test_unknown_find_key(self):
        reject(self, self.tap(find={"text": "x", "id": "button"}), "unknown 'find' keys ['id']")

    def test_value(self):
        for bad in ("", "a" * 201, "two\nlines", "cr\r", 5, None):
            reject(self, self.tap(find={"text": bad}), "'find.text' must be a non-empty string of at most 200 characters")
        reject(self, self.tap(find={"desc": ""}), "'find.desc' must be a non-empty string")

    def test_nth_and_timeout(self):
        for bad in (0, -1, True, "1", 1.0):
            reject(self, self.tap(find={"text": "x", "nth": bad}), "'find.nth' must be an integer >= 1")
        for bad in (0, -5, 99, 99.9, "100", True):
            reject(self, self.tap(find={"text": "x", "timeout_ms": bad}), "'find.timeout_ms' must be a number of at least 100")
        u.validate_scenario(self.tap(find={"text": "x", "timeout_ms": 100}))

    def test_find_settle_ms(self):
        reject(self, self.tap(find={"text": "x"}, find_settle_ms=-1), "'find_settle_ms' must be a number >= 0")
        reject(self, self.tap(find={"text": "x"}, find_settle_ms="1s"), "'find_settle_ms' must be a number >= 0")
        reject(self, self.tap(x=0.5, y=0.5, find_settle_ms=500), "'find_settle_ms' applies only to a tap with 'find'")

    def test_tap_steps_only(self):
        reject(self, scenario([{"id": "s", "do": "swipe", "from": [0, 0], "to": [1, 1], "find": {"text": "x"}}]),
               "keys ['find'] do not apply to a 'swipe' step")
        reject(self, scenario([{"id": "r", "do": "rest", "ms": 100, "find": {"text": "x"}}]),
               "keys ['find'] do not apply to a 'rest' step")

    def test_needle(self):
        self.assertEqual(u.find_needle({"text": "Sign up"}), ' text="Sign up"')
        self.assertEqual(u.find_needle({"desc": "Phone number"}), ' content-desc="Phone number"')
        # the dump writes & < > " as entities, an apostrophe and non-ASCII text as they are
        self.assertEqual(u.find_needle({"text": 'Tom & "Jerry" <3> it\'s — ok'}),
                         ' text="Tom &amp; &quot;Jerry&quot; &lt;3&gt; it\'s — ok"')

    def test_duration_counts_the_lookup(self):
        plain = scenario([{"id": "a", "do": "tap", "x": 0.5, "y": 0.5}])
        found = scenario([{"id": "a", "do": "tap", "find": {"text": "x", "timeout_ms": 10000}}])
        # expected: two reads that agree (2 x 2.5 s and the half-second wait between them) + the 1 s
        # settle + 0.6 s for the baseline: 7.1 s
        self.assertEqual(u.scenario_duration_s(found, worst_case=False) - u.scenario_duration_s(plain, worst_case=False), 7)
        # worst case: the timeout, two more reads and the half-second wait, then the same: 17.1 s
        self.assertEqual(u.scenario_duration_s(found) - u.scenario_duration_s(plain), 17)


# --------------------------------------------------------------------------------------------
# the device script
# --------------------------------------------------------------------------------------------

class DeviceScriptTest(unittest.TestCase):
    def lines(self, scn):
        u.validate_scenario(scn)
        return u.build_device_script(scn, 1080, 2400, True, 77).splitlines()

    def test_text_step(self):
        lines = self.lines(scenario([{"id": "type-code", "do": "text", "text": "me@x.com 00"}]))
        seq = ["mark type-code.start", "input text 'me@x.com%s00'", "mark type-code.injected", "sleep 2.5",
               "mark type-code.end", "snap type-code"]
        idx = [lines.index(s) for s in seq]
        self.assertEqual(idx, list(range(idx[0], idx[0] + len(seq))))
        self.assertEqual(u.default_settle_ms("text"), u.default_settle_ms("key"))

    def test_step_quiet_replaces_the_scenarios_for_that_step(self):
        lines = self.lines(scenario([{"id": "focus", "do": "tap", "x": 0.5, "y": 0.5, "quiet": {"ms": 400}},
                                     {"id": "type", "do": "text", "text": "1", "quiet": {"ms": 300, "max_ms": 4000}},
                                     {"id": "back", "do": "key", "code": 4}],
                                    until_quiet=True, quiet={"ms": 800, "max_ms": 9000}))
        self.assertIn("wait_quiet launch 800 9000 0", lines)
        self.assertIn("wait_quiet focus 400 9000 2.7", lines)
        self.assertIn("wait_quiet type 300 4000 2.2", lines)
        self.assertIn("wait_quiet back 800 9000 2.2", lines)

    def test_step_quiet_in_the_duration(self):
        base = scenario([{"id": "a", "do": "tap", "x": 0.5, "y": 0.5}], until_quiet=True)
        short = scenario([{"id": "a", "do": "tap", "x": 0.5, "y": 0.5, "quiet": {"max_ms": 5000}}], until_quiet=True)
        self.assertEqual(u.scenario_duration_s(base) - u.scenario_duration_s(short), 10)

    def test_lookup_precedes_the_start_mark_and_the_baseline_follows_it(self):
        lines = self.lines(scenario([{"id": "rest", "do": "rest", "ms": 2000},
                                     {"id": "open", "do": "tap", "find": {"text": 'Sign "up"'}}]))
        call = (f"if ! find_el open ' text=\"Sign &quot;up&quot;\"' 0 15000 ' package=\"{PKG}\"' '0 0 0 0 0 0 0'; then "
                "mark open.find_failed; echo open > $D/lookup-failed.txt; exit 3; fi")
        seq = ["snap rest", "mark open.find_start", call, "mark open.found", "sleep 1.0", "base open",
               "mark open.start", "input tap $FX $FY", "mark open.injected"]
        idx = [lines.index(s) for s in seq]
        self.assertEqual(idx, list(range(idx[0], idx[0] + len(seq))))
        self.assertFalse([l for l in lines if l.startswith("input tap ") and l != "input tap $FX $FY"])

    def test_lookup_options(self):
        lines = self.lines(scenario([{"id": "focus", "do": "tap", "find_settle_ms": 2500,
                                      "find": {"desc": "Phone number", "nth": 2, "timeout_ms": 9000}}],
                                    quiet={"ms": 500, "max_ms": 12000}))
        self.assertTrue(any(l.startswith(f"if ! find_el focus ' content-desc=\"Phone number\"' 2 9000 ' package=\"{PKG}\"' "
                                         "'0 0 0 0 0 0 0'; then")
                            for l in lines))
        self.assertEqual(lines[lines.index("base focus") - 1], "sleep 2.5")
        # the timeout defaults to the scenario's quiet.max_ms
        lines = self.lines(scenario([{"id": "o", "do": "tap", "find": {"text": "x"}}], quiet={"ms": 500, "max_ms": 12000}))
        self.assertTrue(any(l.startswith("if ! find_el o ' text=\"x\"' 0 12000 ' package=") for l in lines))

    def test_an_apostrophe_reaches_the_shell_quoted(self):
        lines = self.lines(scenario([{"id": "o", "do": "tap", "find": {"text": "What's your number?"}}]))
        self.assertTrue(any(l.startswith("if ! find_el o ' text=\"What'\"'\"'s your number?\"' 0 15000 ' package=") for l in lines))

    def test_baseline_reads_every_counter_cpu_last(self):
        lines = self.lines(scenario([{"id": "o", "do": "tap", "find": {"text": "x"}}]))
        body = lines[lines.index("base() {"):]
        body = body[:body.index("}") + 1]
        self.assertEqual(body, ["base() {", '  PID=$(pidof "$PKG"); P=${PID%% *}', '  echo "$PID" > $D/pid-$1.base.txt',
                                '  dumpsys gfxinfo "$PKG" framestats > $D/gfx-$1.base.txt 2>&1',
                                "  dumpsys SurfaceFlinger --timestats -dump > $D/sf-$1.base.txt 2>&1",
                                '  cpu $1.base "$P"', "}"])

    def test_old_lookup_files_are_removed_first(self):
        first = self.lines(scenario([{"id": "o", "do": "tap", "find": {"text": "x"}}]))[4]
        self.assertTrue(first.startswith("rm -f "))
        for name in ("$D/find-*.txt", "$D/ui-*.xml", "$D/gfx-*.txt", "$D/cpu-*.txt", "$D/pid-*.txt", "$D/sf-*.txt",
                     "$D/lookup-failed.txt", "$D/cand.tmp", "$D/hits.tmp", "$D/ui.tmp"):
            self.assertIn(name, first.split())

    def test_a_failed_lookup_ends_through_the_clean_up(self):
        lines = self.lines(scenario([{"id": "o", "do": "tap", "find": {"text": "x"}}]))
        call = next(l for l in lines if l.startswith("if ! find_el"))
        self.assertTrue(call.endswith("exit 3; fi"))
        self.assertLess(lines.index("trap cleanup EXIT"), lines.index(call))
        self.assertIn("  dumpsys SurfaceFlinger --timestats -disable >/dev/null 2>&1", lines)
        self.assertIn('  if [ -n "$SR" ]; then kill -2 $SR 2>/dev/null; fi', lines)

    def test_no_lookup_no_baseline(self):
        lines = self.lines(scenario([{"id": "a", "do": "tap", "x": 0.5, "y": 0.5}]))
        self.assertFalse([l for l in lines if l.startswith(("base ", "if ! find_el", "mark a.find"))])

    @unittest.skipUnless(shutil.which("sh"), "no sh")
    def test_the_script_parses(self):
        scn = scenario([{"id": "r", "do": "rest", "ms": 1000, "allow": {"frames_max": 3, "reason": "x"}},
                        {"id": "o", "do": "tap", "find": {"text": "What's up?"}, "quiet": {"ms": 400}},
                        {"id": "t", "do": "text", "text": "a b"}], until_quiet=True)
        u.validate_scenario(scn)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.sh"
            path.write_text(u.build_device_script(scn, 1080, 2400, True, 60))
            for shell in ("sh", "dash", "bash"):
                if shutil.which(shell):
                    proc = subprocess.run([shell, "-n", str(path)], capture_output=True, text=True)
                    self.assertEqual(proc.returncode, 0, (shell, proc.stderr))


def node(text="", desc="", bounds="[0,0][10,10]", leaf=True):
    return (f'<node index="0" text="{text}" resource-id="" class="android.widget.TextView" package="{PKG}" '
            f'content-desc="{desc}" checkable="false" checked="false" clickable="true" enabled="true" focusable="true" '
            f'focused="false" scrollable="false" long-clickable="false" password="false" selected="false" '
            f'bounds="{bounds}"' + (" />" if leaf else ">"))


def dump(*nodes):
    """One line, no trailing newline: the shape of the device's window-tree dump."""
    return ("<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy rotation=\"0\">"
            + node(bounds="[0,0][1080,2400]", leaf=False) + "".join(nodes) + "</node></hierarchy>")


STUB = """#!/bin/sh
# uiautomator dump <file>: write the next prepared tree, or nothing (a read that failed)
n=$(cat "$SEQ/count"); n=$((n + 1)); echo $n > "$SEQ/count"
if [ -f "$SEQ/fail-$n" ]; then :
elif [ -f "$SEQ/dump-$n.xml" ]; then cp "$SEQ/dump-$n.xml" "$2"; elif [ -f "$SEQ/always.xml" ]; then cp "$SEQ/always.xml" "$2"; fi
echo "UI hierchary dumped to: $2"
"""

INPUT_STUB = """#!/bin/sh
# input <args>: record the call; a swipe takes the place of one read in the dump sequence only if
# the test prepared it that way (the sequence counts reads, not swipes)
echo "$*" >> "$SEQ/input.log"
"""


@unittest.skipUnless(shutil.which("sh"), "no sh")
class LookupInTheShellTest(unittest.TestCase):
    """find_el() from the generated script, run in each POSIX shell on the host with a stand-in for
    the dump and a clock that advances half a second per reading. The device's mksh is not among
    the shells, and the stand-in is not the device's dump."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        scn = scenario([{"id": "o", "do": "tap", "find": {"text": "x"}}])
        lines = u.build_device_script(scn, 1080, 2400, False, 60).splitlines()
        body = lines[lines.index("find_el() {"):]
        self.function = "\n".join(body[:body.index("}") + 1])
        (self.root / "bin").mkdir()
        (self.root / "bin" / "uiautomator").write_text(STUB)
        (self.root / "bin" / "uiautomator").chmod(0o755)
        (self.root / "bin" / "input").write_text(INPUT_STUB)
        (self.root / "bin" / "input").chmod(0o755)

    def tearDown(self):
        self.tmp.cleanup()

    def look(self, shell, trees, find, nth=0, timeout_ms=1500, always=None, package=PKG, scroll="0 0 0 0 0 0 0",
             display="1080 2400"):
        d = self.root / shell
        if d.exists():
            shutil.rmtree(d)
        d.mkdir()
        (d / "count").write_text("0\n")
        (d / "clock").write_text("0\n")
        for n, tree in enumerate(trees, 1):
            if tree is not None:
                (d / f"dump-{n}.xml").write_text(tree)
            else:
                (d / f"fail-{n}").write_text("")       # this read fails, whatever comes after
        if always is not None:
            (d / "always.xml").write_text(always)
        dw, _, dh = display.partition(" ")
        script = "\n".join([
            f"D={d}", f"export SEQ={d}", f"PATH={self.root / 'bin'}:$PATH", f"DW={dw}", f"DH={dh}",
            "now_cs() { c=$(cat $D/clock); echo $((c + 50)) > $D/clock; echo $c; }",
            "sleep() { :; }",
            self.function,
            f"if find_el o {u.q(u.find_needle(find))} {nth} {timeout_ms} {u.q(' package=' + chr(34) + package + chr(34))} "
            f"{u.q(scroll)}; then echo \"FX=$FX FY=$FY\"; else echo NOT_FOUND; fi"])
        proc = subprocess.run([shell, "-c", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rec = u.parse_find((d / "find-o.txt").read_text())
        return proc.stdout.strip(), rec, d

    def shells(self):
        return [s for s in ("sh", "dash", "bash") if shutil.which(s)]

    def test_a_unique_match_gives_its_centre(self):
        for shell in self.shells():
            out, rec, d = self.look(shell, [], {"text": "Sign up"}, always=dump(node("Log in", bounds="[0,0][100,50]"),
                                                                               node("Sign up", bounds="[100,200][301,261]")))
            self.assertEqual(out, "FX=200 FY=230", shell)
            # two reads that agree: the clock moves half a second per reading
            self.assertEqual(rec, {"status": "found", "matches": 1, "visible": 1, "reads": 2, "swipes": 0, "elapsed_ms": 1000,
                                   "bounds": [100, 200, 301, 261], "centre": [200, 230], "package": PKG,
                                   "display": [1080, 2400]}, shell)
            self.assertTrue((d / "ui-o.xml").exists())
            self.assertFalse((d / "hits.tmp").exists())

    def test_the_match_is_exact(self):
        for shell in self.shells():
            out, rec, _ = self.look(shell, [], {"text": "Sign up"}, always=dump(node("Sign up now"), node("Sign"),
                                                                                node(desc="Sign up")))
            self.assertEqual(out, "NOT_FOUND", shell)
            self.assertEqual((rec["status"], rec["matches"], rec["reads"]), ("not_found", 0, 3), shell)

    def test_content_description(self):
        for shell in self.shells():
            out, _, _ = self.look(shell, [], {"desc": "Phone number"}, always=dump(node(desc="Phone number", bounds="[10,20][30,40]")))
            self.assertEqual(out, "FX=20 FY=30", shell)

    def test_entities_and_raw_text_match_as_the_dump_writes_them(self):
        for shell in self.shells():
            tree = dump(node("Tom &amp; &quot;Jerry&quot;", bounds="[0,0][2,2]"), node("What's your number? — now", bounds="[4,4][6,6]"))
            self.assertEqual(self.look(shell, [], {"text": 'Tom & "Jerry"'}, always=tree)[0], "FX=1 FY=1", shell)
            self.assertEqual(self.look(shell, [], {"text": "What's your number? — now"}, always=tree)[0], "FX=5 FY=5", shell)

    def test_two_matches_without_nth_wait_then_time_out(self):
        for shell in self.shells():
            out, rec, _ = self.look(shell, [], {"text": "OK"}, always=dump(node("OK"), node("OK")))
            self.assertEqual(out, "NOT_FOUND", shell)
            self.assertEqual((rec["matches"], rec["reads"], rec["elapsed_ms"]), (2, 3, 1500), shell)

    def test_nth_picks_in_document_order(self):
        for shell in self.shells():
            tree = dump(node("OK", bounds="[0,0][10,10]"), node("OK", bounds="[20,20][40,40]"), node("OK", bounds="[50,50][60,60]"))
            out, rec, _ = self.look(shell, [], {"text": "OK", "nth": 2}, nth=2, always=tree)
            self.assertEqual(out, "FX=30 FY=30", shell)
            self.assertEqual(rec["matches"], 3)
            out, rec, _ = self.look(shell, [], {"text": "OK", "nth": 4}, nth=4, always=tree)
            self.assertEqual((out, rec["matches"]), ("NOT_FOUND", 3), shell)

    def test_a_failed_read_is_not_yet(self):
        for shell in self.shells():
            out, rec, _ = self.look(shell, [None, dump(node("Loading"))], {"text": "Sign up"},
                                    always=dump(node("Sign up", bounds="[0,0][4,4]")))
            self.assertEqual(out, "FX=2 FY=2", shell)
            self.assertEqual(rec["reads"], 4, shell)

    def test_no_read_succeeds(self):
        for shell in self.shells():
            out, rec, d = self.look(shell, [], {"text": "Sign up"})
            self.assertEqual(out, "NOT_FOUND", shell)
            self.assertEqual((rec["status"], rec["matches"], rec["reads"]), ("not_found", None, 3), shell)
            self.assertFalse((d / "ui-o.xml").exists())

    def test_the_last_good_tree_is_kept_when_a_later_read_fails(self):
        for shell in self.shells():
            out, rec, d = self.look(shell, [dump(node("Loading")), None, None], {"text": "Sign up"})
            self.assertEqual((out, rec["matches"]), ("NOT_FOUND", None), shell)
            self.assertIn("Loading", (d / "ui-o.xml").read_text())


    def test_a_match_counts_only_when_two_reads_agree(self):
        moving = [dump(node("Sign up", bounds=f"[0,{y}][10,{y + 10}]")) for y in (0, 10)]
        still = dump(node("Sign up", bounds="[0,20][10,30]"))
        for shell in self.shells():
            out, rec, _ = self.look(shell, moving, {"text": "Sign up"}, timeout_ms=5000, always=still)
            self.assertEqual(out, "FX=5 FY=25", shell)
            self.assertEqual(rec["reads"], 4, shell)

    def test_a_failed_read_between_two_matches_breaks_the_pair(self):
        tree = dump(node("Sign up", bounds="[0,0][10,10]"))
        for shell in self.shells():
            out, rec, _ = self.look(shell, [tree, None], {"text": "Sign up"}, timeout_ms=5000, always=tree)
            self.assertEqual((out, rec["reads"]), ("FX=5 FY=5", 4), shell)

    def test_a_match_that_never_holds_still_is_unstable(self):
        moving = [dump(node("Sign up", bounds=f"[0,{y}][10,{y + 10}]")) for y in range(0, 100, 10)]
        for shell in self.shells():
            out, rec, _ = self.look(shell, moving, {"text": "Sign up"}, timeout_ms=1500)
            self.assertEqual(out, "NOT_FOUND", shell)
            # three reads up to the timeout, then one more for the match seen at the timeout
            self.assertEqual((rec["status"], rec["reads"], rec["matches"]), ("unstable", 4, 1), shell)
            self.assertEqual((rec["previous_bounds"], rec["last_bounds"]), ([0, 20, 10, 30], [0, 30, 10, 40]), shell)

    def test_a_match_first_seen_at_the_timeout_gets_one_more_read(self):
        tree = dump(node("Sign up", bounds="[0,0][10,10]"))
        for shell in self.shells():
            out, rec, _ = self.look(shell, [None, None], {"text": "Sign up"}, timeout_ms=1500, always=tree)
            self.assertEqual((out, rec["reads"]), ("FX=5 FY=5", 4), shell)

    def test_only_an_element_of_the_package_counts(self):
        other = dump(node("Allow", bounds="[0,0][10,10]").replace(f'package="{PKG}"', 'package="com.other.dialog"'),
                     node("Allow", bounds="[20,20][30,30]").replace(f'package="{PKG}"', 'package="com.other.dialog"'))
        for shell in self.shells():
            out, rec, _ = self.look(shell, [], {"text": "Allow"}, always=other)
            self.assertEqual(out, "NOT_FOUND", shell)
            self.assertEqual((rec["status"], rec["matches"], rec["other_packages"]),
                             ("not_found", 0, ["com.other.dialog"]), shell)
            # the step may name the other package
            out, rec, _ = self.look(shell, [], {"text": "Allow", "nth": 2}, nth=2, always=other,
                                    package="com.other.dialog")
            self.assertEqual((out, rec["package"]), ("FX=25 FY=25", "com.other.dialog"), shell)

    def test_an_element_of_another_package_does_not_count_against_a_unique_match(self):
        tree = dump(node("OK", bounds="[0,0][10,10]").replace(f'package="{PKG}"', 'package="com.other.dialog"'),
                    node("OK", bounds="[20,20][40,40]"))
        for shell in self.shells():
            out, rec, _ = self.look(shell, [], {"text": "OK"}, always=tree)
            self.assertEqual((out, rec["matches"], rec["package"]), ("FX=30 FY=30", 1, PKG), shell)


# --------------------------------------------------------------------------------------------
# analysis: a found step counts from its baseline
# --------------------------------------------------------------------------------------------

FIND_SCN = copy.deepcopy(ANALYSIS_SCN)
FIND_SCN["steps"][1] = {"id": "open", "do": "tap", "find": {"text": "Sign up"}}
FOUND = (f"status found\nmatches 1\nvisible 1\nreads 2\nswipes 0\nelapsed_ms 4100\nbounds 100 200 300 260\n"
         f"centre 200 230\npackage {PKG}\ndisplay 1080 2400\n")


def found_files():
    """idle ends with 30 frames, 30 app-layer frames, 110 display frames and 54 CPU ticks. The
    lookup then draws 6 frames and spends 16 ticks; the baseline after it reads 36, 36, 130 and 70.
    open ends at 42, 42, 160 and 80."""
    files = healthy_files()
    files["marks.txt"] = files["marks.txt"].replace("open.start 112.5\n",
                                                    "open.find_start 112.45\nopen.found 112.46\nopen.start 112.5\n")
    files["gfx-open.base.txt"] = gfx_text(36)
    files["sf-open.base.txt"] = sf_text(130, 36)
    files["pid-open.base.txt"] = f"{PID}\n"
    files["cpu-open.base.txt"] = cpu_text(112.9, 70, {str(PID): (PKG, 60), "4330": ("RenderThread", 10)})
    files["find-open.txt"] = FOUND
    return files


class FoundStepAnalysisTest(Base):
    def test_counts_start_at_the_baseline(self):
        cap = Capture(self.root, found_files(), scn=FIND_SCN)
        st = steps_by_id(cap.analyze())
        # against idle's end the step would be charged 12 frames, 12 layer frames, 50 display frames
        # and 26 ticks over 2.7 s: the lookup's 6, 6, 20 and 16 belong to no step
        self.assertEqual((st["open"]["frames"], st["open"]["frames_layers"], st["open"]["frames_display"]), (6, 6, 30))
        self.assertEqual((st["open"]["cpu_ticks_total"], st["open"]["cpu_window_s"]), (10, 2.2))
        self.assertEqual(st["open"]["snapshot_missing"], [])
        # the steps around it are unchanged
        self.assertEqual((st["idle"]["frames"], st["idle"]["cpu_ticks_total"]), (0, 0))
        self.assertEqual((st["idle2"]["frames"], st["idle2"]["cpu_ticks_total"]), (0, 0))
        self.assertEqual(cap.gate()["verdict"], "PASS")

    def test_the_lookup_is_recorded(self):
        cap = Capture(self.root, found_files(), scn=FIND_SCN)
        find = steps_by_id(cap.analyze())["open"]["find"]
        self.assertEqual(find, {"text": "Sign up", "status": "found", "centre_px": [200, 230],
                                "centre": [round(200 / 1080, 4), round(230 / 2400, 4)], "bounds_px": [100, 200, 300, 260],
                                "package": PKG, "matches": 1, "visible": 1, "reads": 2, "swipes": 0,
                                "display_px": [1080, 2400], "lookup_ms": 4100, "settle_ms": 1000, "baseline_s": 112.9,
                                "tree": "raw/ui-open.xml"})

    def test_a_missing_baseline_is_unknown_not_the_previous_step(self):
        for name, unknown in (("gfx-open.base.txt", "frames"), ("sf-open.base.txt", "frames_layers"),
                              ("cpu-open.base.txt", "cpu_ticks_total")):
            files = found_files()
            files[name] = None
            cap = Capture(self.root, files, scn=FIND_SCN, name=f"run-{name}")
            st = steps_by_id(cap.analyze())
            self.assertIsNone(st["open"][unknown], name)
            self.assertTrue(any(m.startswith("baseline ") for m in st["open"]["snapshot_missing"]), name)

    def test_a_baseline_from_another_process_gives_no_cpu(self):
        files = found_files()
        files["pid-open.base.txt"] = "9999\n"
        st = steps_by_id(Capture(self.root, files, scn=FIND_SCN).analyze())
        self.assertIsNone(st["open"]["cpu_ticks_total"])

    def test_a_missing_lookup_record_fails_the_gate(self):
        files = found_files()
        files["find-open.txt"] = None
        cap = Capture(self.root, files, scn=FIND_SCN)
        self.assertIsNone(steps_by_id(cap.analyze())["open"]["find"])
        res = cap.gate()
        self.assertEqual(checks(res), ["find"])
        self.assertIn("the element with text \"Sign up\"", res["violations"][0]["message"])

    def test_the_launch_window_ends_before_a_first_lookup(self):
        scn = copy.deepcopy(ANALYSIS_SCN)
        scn["steps"][0] = {"id": "idle", "do": "tap", "find": {"text": "Go"}}
        files = healthy_files()
        files["marks.txt"] = files["marks.txt"].replace("idle.start 110.0\n", "idle.find_start 107.0\nidle.start 110.0\n")
        for k, text in (("gfx", gfx_text(30)), ("sf", sf_text(100, 30)), ("pid", f"{PID}\n"),
                        ("cpu", cpu_text(109.6, 53, {}))):
            files[f"{k}-idle.base.txt"] = text
        files["find-idle.txt"] = FOUND
        st = steps_by_id(Capture(self.root, files, scn=scn).analyze())
        self.assertEqual(st["launch"]["end_s"], 107.0)
        self.assertEqual(u.launch_end_mark(scn), "idle.find_start")
        self.assertEqual(u.launch_end_mark(ANALYSIS_SCN), "idle.start")

    def test_the_recording_uses_the_same_launch_window(self):
        from PIL import Image
        files = []
        for i in range(4):
            f = Path(self.root) / f"f{i:05d}.png"
            Image.new("L", (27, 60), color=0).save(f)
            files.append(f)
        scn = scenario([{"id": "o", "do": "tap", "find": {"text": "Go"}}])
        marks = {"T0": 100.0, "launch.start": 101.0, "o.find_start": 103.0, "o.start": 106.0, "o.end": 108.0}
        video = Path(self.root) / "screen.mp4"
        video.write_bytes(b"no recorder metadata")
        (Path(self.root) / "sheets").mkdir()
        with mock.patch.object(ui_video, "probe_pts", return_value=[0.0, 1.0, 2.0, 3.0]), \
                mock.patch.object(ui_video, "extract", return_value=files), \
                mock.patch.object(ui_video, "recorder_timestamps", return_value=[101.5, 102.5, 104.0, 106.5]):
            res = ui_video.analyze(video, Path(self.root), [], marks, scn, {"height": 2400, "stable_insets_top_bottom": None})
        by_id = {s["id"]: s for s in res["steps"]}
        # frames at 102.5 (launch) and 104.0 (during the lookup: in no window) and 106.5 (the tap)
        self.assertEqual((by_id["launch"]["video_frames"], by_id["o"]["video_frames"]), (1, 1))


# --------------------------------------------------------------------------------------------
# the gate with an allowance
# --------------------------------------------------------------------------------------------

def allow_scn(**allow):
    return {"name": "Example", "package": PKG, "steps": [
        {"id": "open", "do": "tap", "x": 0.5, "y": 0.5},
        {"id": "idle", "do": "rest", "ms": 2000, "allow": {**allow, "reason": "the caret blinks"}}]}


class AllowanceGateTest(unittest.TestCase):
    def gate(self, scn, video=None, opts=None, clock="recorder-timestamps", **rest_kw):
        return u.gate_run(make_run(base_steps(**rest_kw), video, clock=clock), scn, opts or DEFAULTS)

    def test_within_the_allowance_passes_with_a_note(self):
        res = self.gate(allow_scn(frames_max=12), [vstep("idle")], frames=11)
        self.assertEqual((res["verdict"], res["violations"]), ("PASS", []))
        self.assertEqual(res["notes"], ["idle: 11 frames drawn at rest (platform count), above the limit of 0 that would "
                                        "otherwise apply; passed on the step's allowance of 12 (the caret blinks)"])

    def test_no_note_when_the_allowance_was_not_needed(self):
        res = self.gate(allow_scn(frames_max=12, cpu_pct_max=5), [vstep("idle")], frames=0, cpu=1)
        self.assertEqual(res, {"verdict": "PASS", "violations": [], "notes": []})

    def test_above_the_allowance_fails_and_names_it(self):
        res = self.gate(allow_scn(frames_max=12), [vstep("idle")], frames=13)
        self.assertEqual(checks(res), ["rest-frames"])
        v = res["violations"][0]
        self.assertEqual((v["observed"], v["allowed"], v["limit_from"]), (13, 12, "allowance"))
        self.assertEqual(v["message"], "13 frames drawn at rest (platform count), allowed 12 by the step's allowance "
                                       "(the caret blinks)")

    def test_frames_that_changed_nothing_still_fail(self):
        res = self.gate(allow_scn(frames_max=12, changed_frames_max=5), [vstep("idle", identical=1)], frames=11)
        self.assertEqual(checks(res), ["rest-video"])
        self.assertIn("changed nothing on screen", res["violations"][0]["message"])
        self.assertNotIn("limit_from", res["violations"][0])

    def test_changed_frames(self):
        res = self.gate(allow_scn(changed_frames_max=3), [vstep("idle", region=2, real=2)])
        self.assertEqual(res["verdict"], "PASS")
        self.assertEqual(res["notes"], ["idle: 2 recorded frames changed the app's area at rest, above the limit of 0 "
                                        "that would otherwise apply; passed on the step's allowance of 3 (the caret blinks)"])
        res = self.gate(allow_scn(changed_frames_max=3), [vstep("idle", region=4, real=4)])
        self.assertEqual(checks(res), ["rest-video"])
        v = res["violations"][0]
        self.assertEqual((v["observed"], v["allowed"], v["limit_from"]), (4, 3, "allowance"))
        self.assertIn("allowed 3 by the step's allowance (the caret blinks)", v["message"])
        # an allowance without changed_frames_max keeps the default of none
        res = self.gate(allow_scn(frames_max=12), [vstep("idle", region=1, real=1)])
        self.assertEqual(checks(res), ["rest-video"])
        self.assertNotIn("allowance", res["violations"][0]["message"])

    def test_cpu(self):
        # 5 ticks over 2 s at 100 ticks a second = 2.5 %
        res = self.gate(allow_scn(cpu_pct_max=3), cpu=5)
        self.assertEqual(res["verdict"], "PASS")
        self.assertIn("idle: 2.5% of one core used at rest, above the limit of 2.0% that would otherwise apply; "
                      "passed on the step's allowance of 3% (the caret blinks)", res["notes"])
        res = self.gate(allow_scn(cpu_pct_max=1), cpu=3)
        self.assertEqual(checks(res), ["rest-cpu"])
        self.assertEqual((res["violations"][0]["allowed"], res["violations"][0]["limit_from"]), (1, "allowance"))
        self.assertIn("allowed 1% by the step's allowance (the caret blinks)", res["violations"][0]["message"])

    def test_the_allowance_wins_over_the_command_line_for_its_step_only(self):
        scn = allow_scn(frames_max=5)
        scn["steps"].append({"id": "idle2", "do": "rest", "ms": 2000})
        steps = base_steps(frames=6) + [step("idle2", "rest", 12.0, 14.0, frames=6)]
        res = u.gate_run(make_run(steps), scn, {**DEFAULTS, "rest_frames_max": 20})
        self.assertEqual([(v["check"], v["step"]) for v in res["violations"]], [("rest-frames", "idle")])
        steps = base_steps(frames=1) + [step("idle2", "rest", 12.0, 14.0, frames=1)]
        res = u.gate_run(make_run(steps), scn, DEFAULTS)
        self.assertEqual([(v["check"], v["step"]) for v in res["violations"]], [("rest-frames", "idle2")])

    def test_an_unaligned_recording_still_only_notes(self):
        res = self.gate(allow_scn(changed_frames_max=1), [vstep("idle", region=2, real=2)], clock=None)
        self.assertEqual(checks(res), [])

    def test_gate_and_command_line_show_the_allowance(self):
        with tempfile.TemporaryDirectory() as root:
            d = Path(root) / "run-01"
            d.mkdir()
            scn = allow_scn(frames_max=12)
            (d / "capture.json").write_text(json.dumps({"scenario": scn}))
            (d / "run.json").write_text(json.dumps(make_run(base_steps(frames=11))))
            out = u.gate([root], {"rest_frames_max": 0})
            self.assertEqual(out["runs"][0]["allowances"], {"idle": scn["steps"][1]["allow"]})
            self.assertEqual(out["verdict"], "PASS")
            rc, text, _ = main("gate", root)
        self.assertEqual(rc, 0)
        self.assertIn("  allowance: idle: frames_max 12 (the caret blinks)", text)
        self.assertIn("passed on the step's allowance of 12", text)


# --------------------------------------------------------------------------------------------
# the host when an element never appears
# --------------------------------------------------------------------------------------------

FIND_RUN_SCN = {"name": "Example", "package": PKG, "steps": [
    {"id": "idle", "do": "rest", "ms": 1000},
    {"id": "open", "do": "tap", "find": {"text": "Sign up", "timeout_ms": 6000}}]}


class LookupDevice(FakeDevice):
    """The device script stopped at a lookup: its files are on the device and can be pulled."""

    def __init__(self, files, exit_status=3):
        err = u.ProbeError(f"adb shell sh /data/local/tmp/uiprobe/run.sh failed (exit {exit_status}): no output") \
            if exit_status else None
        super().__init__(script_error=err, listing=" ".join(["run.sh"] + sorted(files)))
        self.files = files

    def adb(self, serial, *args, **kw):
        self.commands.append(" ".join(args))
        if args[0] == "pull":
            Path(args[2]).write_text(self.files[Path(args[1]).name])
        return ""


class LookupFailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.scn = self.root / "scenario.json"
        self.scn.write_text(json.dumps(FIND_RUN_SCN))

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, device):
        with contextlib.ExitStack() as stack:
            for p in device.patches():
                stack.enter_context(p)
            return main("run", "--serial", "<serial>", "--scenario", str(self.scn), "--out", str(self.root / "out"))

    def files(self, matches="2", tree=True):
        files = {"marks.txt": "T0 1\n", "lookup-failed.txt": "open\n",
                 "find-open.txt": f"status not_found\nmatches {matches}\nreads 3\nelapsed_ms 6400\n"}
        if tree:
            files["ui-open.xml"] = dump(node("Sign up"), node("Sign up"))
        return files

    def test_the_error_names_the_step_the_element_the_matches_and_the_tree(self):
        device = LookupDevice(self.files())
        rc, _, err = self.run_cli(device)
        self.assertEqual(rc, 2)
        self.assertNotIn("Traceback", err)
        tree = self.root / "out" / "run-01" / "raw" / "ui-open.xml"
        self.assertIn("step 'open': the element with text \"Sign up\" did not appear within 6000 ms (3 reads): "
                      f"2 matched in {PKG} at the last read (exactly one needed); the last tree read is saved in {tree}", err)
        self.assertTrue(tree.exists())
        # the host cleans up as on every other failure, and leaves no capture
        self.assertTrue(any("--timestats -disable" in c and "script.pid" in c for c in device.commands))
        self.assertFalse((self.root / "out" / "run-01" / "capture.json").exists())

    def test_an_adb_that_reports_success_is_caught_too(self):
        rc, _, err = self.run_cli(LookupDevice(self.files(matches="0"), exit_status=None))
        self.assertEqual(rc, 2)
        self.assertIn(f"0 matched in {PKG} at the last read (exactly one needed)", err)

    def test_no_tree_was_ever_read(self):
        rc, _, err = self.run_cli(LookupDevice(self.files(matches="unread", tree=False)))
        self.assertEqual(rc, 2)
        self.assertIn("the last read of the window tree failed; no read of the window tree succeeded, so no tree was saved", err)

    def test_nth_is_named(self):
        scn = copy.deepcopy(FIND_RUN_SCN)
        scn["steps"][1]["find"]["nth"] = 3
        self.scn.write_text(json.dumps(scn))
        rc, _, err = self.run_cli(LookupDevice(self.files()))
        self.assertEqual(rc, 2)
        self.assertIn("the element with text \"Sign up\" (match 3 in document order)", err)
        self.assertIn(f"2 matched in {PKG} at the last read (at least 3 needed)", err)

    def test_another_failure_is_reported_as_it_was(self):
        device = FakeDevice(script_error=u.ProbeError("adb shell sh run.sh did not finish within 60 s"))
        rc, _, err = self.run_cli(device)
        self.assertEqual(rc, 2)
        self.assertIn("did not finish within 60 s", err)


class LookupRecordTest(unittest.TestCase):
    """The host's reading of a lookup that stopped the script, and of one that matched elsewhere."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.scn = self.root / "scenario.json"
        self.scn.write_text(json.dumps(FIND_RUN_SCN))

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, files):
        device = LookupDevice(files)
        with contextlib.ExitStack() as stack:
            for p in device.patches():
                stack.enter_context(p)
            return main("run", "--serial", "<serial>", "--scenario", str(self.scn), "--out", str(self.root / "out"))

    def test_an_unstable_match_says_so(self):
        rc, _, err = self.run_cli({"marks.txt": "T0 1\n", "lookup-failed.txt": "open\n", "find-open.txt":
                                   "status unstable\nmatches 1\nreads 4\nelapsed_ms 6500\n"
                                   "last_bounds 0 30 10 40\nprevious_bounds 0 20 10 30\nother_packages \n"})
        self.assertEqual(rc, 2)
        self.assertIn("step 'open': the element with text \"Sign up\" did not hold still within 6000 ms (4 reads): a match "
                      "was there but did not hold still: two reads in a row never gave the same bounds "
                      "(bounds [0, 20, 10, 30] then [0, 30, 10, 40])", err)

    def test_matches_in_other_packages_are_named(self):
        rc, _, err = self.run_cli({"marks.txt": "T0 1\n", "lookup-failed.txt": "open\n", "find-open.txt":
                                   "status not_found\nmatches 0\nreads 3\nelapsed_ms 6400\n"
                                   "other_packages com.other.a com.other.b com.other.a \n"})
        self.assertEqual(rc, 2)
        self.assertIn(f"0 matched in {PKG} at the last read (exactly one needed); matching elements in other packages: "
                      "com.other.a, com.other.b", err)

    def test_a_step_named_failed_is_not_taken_for_the_marker(self):
        """The marker's name is not one a step's own record (find-<id>.txt) can take."""
        files = {"find-failed.txt": FOUND, "done.txt": "done\n"}
        device = LookupDevice(files, exit_status=None)
        scn = {"name": "Example", "package": PKG, "steps": [{"id": "failed", "do": "tap", "find": {"text": "x"}}]}
        u.validate_scenario(scn)
        with contextlib.ExitStack() as stack:
            for p in device.patches():
                stack.enter_context(p)
            self.assertIsNone(u.lookup_failure("<serial>", scn, self.root / "out-failed"))
        self.assertNotIn("find-failed.txt", u.build_device_script(scn, 1080, 2400, False, 60))

    def test_a_find_record_in_another_package_fails_the_gate(self):
        files = found_files()
        files["find-open.txt"] = FOUND.replace(f"package {PKG}", "package com.other.dialog")
        root = tempfile.mkdtemp(dir=self.root)
        cap = Capture(root, files, scn=FIND_SCN)
        cap.analyze()
        res = cap.gate()
        self.assertEqual(checks(res), ["find"])
        self.assertIn("was matched in package 'com.other.dialog', not in 'com.example.app'", res["violations"][0]["message"])
        # a step that names that package passes
        scn = copy.deepcopy(FIND_SCN)
        scn["steps"][1]["find"]["package"] = "com.other.dialog"
        self.assertEqual(self.gate_with(files, scn), "PASS")

    def gate_with(self, files, scn):
        cap = Capture(tempfile.mkdtemp(dir=self.root), files, scn=scn)
        cap.analyze()
        return cap.gate()["verdict"]


class FindValueAndPackageValidationTest(unittest.TestCase):
    def tap(self, **find):
        return scenario([{"id": "open", "do": "tap", "find": find}])

    def test_characters_the_dump_never_writes_as_they_are(self):
        for ch in ("\t", "\x00", "\x1b", "\x7f", "\x85", "\ud800", "\U0001F600", "\ufffe"):
            for key in ("text", "desc"):
                reject(self, self.tap(**{key: f"Smile {ch} now"}), f"'find.{key}' holds {ch!r} (U+{ord(ch):04X})")
        for good in ("Café — naïve", "日本語", "€ 5", "a\u00a0b"):
            u.validate_scenario(self.tap(text=good))

    def test_package(self):
        u.validate_scenario(self.tap(text="Allow", package="com.other.dialog"))
        for bad in ("", "nodots", "com.ex ample", 5, None, "com.x;rm"):
            reject(self, self.tap(text="Allow", package=bad), "'find.package' must be an application id")

    def test_the_package_reaches_the_lookup(self):
        lines = u.build_device_script(scenario([{"id": "o", "do": "tap", "find": {"text": "x"}},
                                                {"id": "p", "do": "tap", "find": {"text": "y", "package": "com.other.dialog"}}]),
                                      1080, 2400, False, 60).splitlines()
        self.assertTrue(any(l.startswith(f"if ! find_el o ' text=\"x\"' 0 15000 ' package=\"{PKG}\"' '0 ") for l in lines))
        self.assertTrue(any(l.startswith("if ! find_el p ' text=\"y\"' 0 15000 ' package=\"com.other.dialog\"' '0 ")
                            for l in lines))


# --------------------------------------------------------------------------------------------
# runs that did not complete
# --------------------------------------------------------------------------------------------

def incomplete(root, name, failed=None, script=False, raw=False):
    d = Path(root) / name
    d.mkdir(parents=True)
    if failed is not None:
        (d / "failed.json").write_text(json.dumps(failed))
    if script:
        (d / "device-script.sh").write_text("#!/system/bin/sh\n")
    if raw:
        (d / "raw").mkdir()
        (d / "raw" / "marks.txt").write_text("T0 1\n")
    return d


FAILED = {"tool": {"name": "ui-probe", "version": u.VERSION}, "scenario": "Example", "error": "the element never came",
          "step": "open", "time_utc": "2026-11-10T12:00:00Z"}


class FailedRunRecordTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.scn = self.root / "scenario.json"
        self.scn.write_text(json.dumps(FIND_RUN_SCN))

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, device, out="out"):
        with contextlib.ExitStack() as stack:
            for p in device.patches():
                stack.enter_context(p)
            return main("run", "--serial", "<serial>", "--scenario", str(self.scn), "--out", str(self.root / out))

    def failed(self, out="out"):
        return json.loads((self.root / out / "run-01" / "failed.json").read_text())

    def test_a_lookup_that_stopped_the_run(self):
        files = {"marks.txt": "T0 1\n", "lookup-failed.txt": "open\n",
                 "find-open.txt": "status not_found\nmatches 0\nreads 3\nelapsed_ms 6400\n"}
        rc, _, err = self.run_cli(LookupDevice(files))
        self.assertEqual(rc, 2)
        rec = self.failed()
        self.assertEqual((rec["step"], rec["scenario"], rec["tool"]["version"]), ("open", "Example", u.VERSION))
        self.assertIn("did not appear within 6000 ms", rec["error"])
        self.assertRegex(rec["time_utc"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_any_other_failure(self):
        rc, _, _ = self.run_cli(FakeDevice(script_error=u.ProbeError("adb shell sh run.sh did not finish within 60 s")))
        self.assertEqual(rc, 2)
        self.assertEqual((self.failed()["step"], self.failed()["error"]), (None, "adb shell sh run.sh did not finish within 60 s"))

    def test_an_unexpected_error(self):
        device = FakeDevice()

        def boom(serial, pkg):
            raise RuntimeError("boom")
        with mock.patch.object(u, "device_facts", boom):
            with contextlib.ExitStack() as stack:
                for p in device.patches()[:2]:
                    stack.enter_context(p)
                rc, _, _ = main("run", "--serial", "<serial>", "--scenario", str(self.scn), "--out", str(self.root / "out"))
        self.assertEqual(rc, 3)
        self.assertEqual(self.failed()["error"], "RuntimeError: boom")

    def test_run_refuses_a_folder_that_holds_part_of_a_run(self):
        for marker in ("raw", "device-script.sh", "failed.json", "capture.json", "run.json"):
            run = self.root / marker / "run-01"
            run.mkdir(parents=True)
            if marker == "raw":
                (run / "raw").mkdir()
            else:
                (run / marker).write_text("{}")
            device = FakeDevice()
            rc, _, err = self.run_cli(device, out=marker)
            self.assertEqual(rc, 2, marker)
            self.assertIn("already holds a run or what is left of one", err)
            self.assertIn(marker, err)
            self.assertEqual(device.commands, [], marker)            # nothing reached the device
            self.assertEqual(sorted(p.name for p in run.iterdir()), [marker], marker)   # and nothing was written


class IncompleteRunsTest(Base):
    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = u.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_gate_fails_a_batch_with_a_run_that_did_not_complete(self):
        Capture(self.root, name="run-01").analyze()
        incomplete(self.root, "run-02", failed=FAILED, script=True, raw=True)
        out = u.gate([self.root], {})
        self.assertEqual([r["verdict"] for r in out["runs"]], ["PASS", "FAIL"])
        self.assertEqual(out["verdict"], "FAIL")
        v = out["runs"][1]["violations"]
        self.assertEqual([x["check"] for x in v], ["run"])
        self.assertEqual(v[0]["message"], "the run did not complete: at step 'open': the element never came")
        self.assertEqual(out["runs"][1]["scenario"], "Example")
        rc, text, _ = self.main("gate", self.root)
        self.assertEqual(rc, 1)
        self.assertIn("FAIL [run] -: the run did not complete", text)
        self.assertEqual(self.main("gate", self.root, "--advisory")[0], 0)

    def test_a_saved_error_that_names_its_step_is_not_prefixed_with_it_again(self):
        # What a real lookup failure saves (seen on a device): the error starts with its own step.
        failed = dict(FAILED, error="step 'open': the element with text \"Go\" did not appear within 3000 ms")
        incomplete(self.root, "run-01", failed=failed, script=True, raw=True)
        v = u.gate([self.root], {})["runs"][0]["violations"]
        self.assertEqual(v[0]["message"],
                         "the run did not complete: step 'open': the element with text \"Go\" did not appear within 3000 ms")

    def test_a_folder_of_incomplete_runs_only_fails(self):
        for n, kw in enumerate(({"script": True}, {"raw": True}, {"failed": FAILED}), 1):
            root = Path(self.root) / f"batch-{n}"
            incomplete(root, "run-01", **kw)
            out = u.gate([str(root)], {})
            self.assertEqual((out["verdict"], [x["check"] for x in out["runs"][0]["violations"]]), ("FAIL", ["run"]), kw)
            self.assertEqual(self.main("gate", str(root))[0], 1, kw)
        self.assertIn("(no failed.json, capture.json or run.json)",
                      u.gate([str(Path(self.root) / "batch-1")], {})["runs"][0]["violations"][0]["message"])

    def test_an_unreadable_failed_json_still_fails(self):
        d = incomplete(self.root, "run-01")
        (d / "failed.json").write_text("{not json")
        msg = u.gate([self.root], {})["runs"][0]["violations"][0]["message"]
        self.assertTrue(msg.startswith("the run did not complete: its failed.json cannot be read"), msg)

    def test_summary_lists_them_and_leaves_them_out(self):
        Capture(self.root, name="run-01").analyze()
        Capture(self.root, name="run-02").analyze()
        incomplete(self.root, "run-03", failed=FAILED)
        out = u.summary([self.root])
        self.assertEqual([x["run"] for x in out["incomplete"]], [str(Path(self.root) / "run-03")])
        self.assertEqual([r["run"] for r in out["runs"]], ["run-01", "run-02"])
        self.assertEqual(out["stats"]["idle.frames"]["n"], 2)
        rc, text, _ = self.main("summary", self.root)
        self.assertEqual(rc, 0)
        self.assertIn(f"incomplete, left out of every statistic: {Path(self.root) / 'run-03'}: the run did not complete", text)

    def test_summary_of_incomplete_runs_only_is_an_error(self):
        incomplete(self.root, "run-01", failed=FAILED)
        rc, _, err = self.main("summary", self.root)
        self.assertEqual(rc, 2)
        self.assertIn("completed", err)

    def test_compare_names_what_it_left_out(self):
        base, head = Path(self.root) / "base", Path(self.root) / "head"
        Capture(base, name="run-01").analyze()
        incomplete(base, "run-02", failed=FAILED)
        Capture(head, name="run-01").analyze()
        incomplete(head, "run-02", script=True)
        incomplete(head, "run-03", raw=True)
        out = u.compare([str(base)], [str(head)], [], 0.01, 5)
        self.assertEqual([x["run"] for x in out["incomplete"]["base"]], [str(base / "run-02")])
        self.assertEqual([x["run"] for x in out["incomplete"]["head"]], [str(head / "run-02"), str(head / "run-03")])
        self.assertEqual(out["metrics"]["idle.frames"]["n_base"], 1)
        rc, text, _ = self.main("compare", "--base", str(base), "--head", str(head))
        self.assertEqual(rc, 0)
        self.assertIn(f"note: base: left out 1 incomplete runs: {base / 'run-02'}", text)
        self.assertIn(f"note: head: left out 2 incomplete runs: {head / 'run-02'}, {head / 'run-03'}", text)

    def test_a_complete_run_is_not_taken_for_an_incomplete_one(self):
        Capture(self.root, name="run-01").analyze()        # it has raw/ and its device files
        (Path(self.root) / "run-01" / "device-script.sh").write_text("#!/system/bin/sh\n")
        (Path(self.root) / "run-01" / "failed.json").write_text(json.dumps(FAILED))
        self.assertEqual(u.gate([self.root], {})["verdict"], "PASS")


class GateScenarioOverrideTest(Base):
    def main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = u.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_another_scenario_is_refused(self):
        Capture(self.root, name="run-01").analyze()
        other = copy.deepcopy(ANALYSIS_SCN)
        other["name"] = "Another"
        path = Path(self.root) / "other.json"
        path.write_text(json.dumps(other))
        rc, _, err = self.main("gate", str(Path(self.root) / "run-01"), "--scenario", str(path))
        self.assertEqual(rc, 2)
        self.assertIn("--scenario is 'Another' but the run was captured with 'Example'", err)

    def test_an_allowance_from_the_override_says_so(self):
        Capture(self.root, name="run-01").analyze()
        revised = copy.deepcopy(ANALYSIS_SCN)
        rest = next(s for s in revised["steps"] if s["do"] == "rest")
        rest["allow"] = {"frames_max": 3, "reason": "the caret blinks"}
        path = Path(self.root) / "revised.json"
        path.write_text(json.dumps(revised))
        rc, text, _ = self.main("gate", str(Path(self.root) / "run-01"), "--scenario", str(path))
        self.assertEqual(rc, 0)
        self.assertIn(f"  allowance: {rest['id']}: frames_max 3 (the caret blinks) (from --scenario, not the scenario "
                      "stored with the run)", text)
        rc, text, _ = self.main("gate", str(Path(self.root) / "run-01"))
        self.assertNotIn("from --scenario", text)


class DocsAndDoctorTest(unittest.TestCase):
    def test_every_example_step_in_the_readme_validates(self):
        readme = (Path(u.HERE).parent / "README.md").read_text()
        block = readme.split("More keys, one example each:", 1)[1].split("```json", 1)[1].split("```", 1)[0]
        steps = [json.loads(line) for line in block.strip().splitlines()]
        self.assertEqual(len(steps), 5)
        for st in steps:
            u.validate_scenario(scenario([st]))

    def doctor(self, has_uiautomator):
        def sh(serial, command, **kw):
            if command.startswith("command -v "):
                tool = command.split()[-1]
                return "" if tool == "uiautomator" and not has_uiautomator else f"/system/bin/{tool}\n"
            return "34\n"

        def fake_run(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 0, stdout=("ffmpeg version 7.0 x" if cmd[0] == "ffmpeg" else "v 1") + "\n",
                                               stderr="")
        with mock.patch.object(u, "sh", sh), mock.patch.object(u, "adb", lambda *a, **k: "device\n"), \
                mock.patch.object(u.shutil, "which", lambda tool: f"/usr/bin/{tool}"), \
                mock.patch.object(u.subprocess, "run", fake_run):
            return u.doctor("<serial>")

    def test_doctor_reports_uiautomator_without_failing_on_it(self):
        with_it, without = self.doctor(True), self.doctor(False)
        check = next(c for c in without["checks"] if c["check"].startswith("device uiautomator"))
        self.assertEqual((check["ok"], check["required"]), (False, False))
        self.assertIn('a tap with "find" needs it', check["detail"])
        self.assertTrue(next(c for c in with_it["checks"] if c["check"].startswith("device uiautomator"))["ok"])
        self.assertEqual(with_it["ok"], without["ok"])


if __name__ == "__main__":
    unittest.main()
