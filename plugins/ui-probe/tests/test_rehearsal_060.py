"""0.6.0: a rehearsal on the iOS Simulator. `ui-probe rehearse` drives a scenario once, with lookups and
typing, in an app process it then ends; `ui-probe run --rehearsal` replays the positions it found in
a fresh process, which is the one measured. The agent is the stand-in of test_simulator_040 (it
serves find, type and labels when started for a rehearsal); the lookup rule is also tested against
an agent object in this process with a fake clock. No simulator, no device, no real Xcode."""
import sys

sys.dont_write_bytecode = True

import json
import os
import plistlib
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import test_input_050 as t050  # noqa: E402  (its base class; its tests are not imported here)
import test_simulator_040 as s040  # noqa: E402
import ui_probe as u  # noqa: E402

BUNDLE, UDID, main = s040.BUNDLE, s040.UDID, s040.main
TEXT = "5550001234"


def match(frame=(20.0, 400.0, 100.0, 40.0), on=True, hit=None, label="Sign in", typ=9, screen=(393.0, 852.0), **extra):
    """A match as the 0.6.0 agent answers it. hit defaults to on (a plain visible element); extra holds
    'under', 'moved', 'not_asked' or 'visible'."""
    x, y, w, h = frame
    m = {"frame": list(frame), "center": [round((x + w / 2) / screen[0], 4), round((y + h / 2) / screen[1], 4)],
         "on_screen": on, "hittable": on if hit is None else hit, "type": typ, "label": label}
    m.update(extra)
    return m


def reply(*matches, found=None):
    return {"ok": True, "found": len(matches) if found is None else found, "matches": list(matches),
            "screen": [393.0, 852.0]}


class FakeAgent:
    """The agent's replies to find (one per read, the last repeated), swipe, tap, labels."""

    def __init__(self, finds, swipe_ok=True, labels=None):
        self.finds, self.swipe_ok, self.sent, self.n = list(finds), swipe_ok, [], 0
        self.labels = labels if labels is not None else [{"label": "Welcome", "value": "", "type": 48,
                                                          "frame": [0, 0, 10, 10]}]

    def send(self, command, timeout=None):
        self.sent.append(command)
        c = command["cmd"]
        if c == "find":
            r = self.finds[min(self.n, len(self.finds) - 1)]
            self.n += 1
            return r, 40
        if c == "swipe":
            return ({"ok": True} if self.swipe_ok else {"ok": False, "error": "swipe failed"}), 300
        if c == "labels":
            return {"ok": True, "truncated": False, "elements": self.labels, "proof": "p"}, 100
        return {"ok": True}, 50


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def scn_with(find, **extra):
    step = {"id": "open", "do": "tap", "find": find, **extra}
    return {"name": "Lookup", "package": BUNDLE, "steps": [step]}, step


# --------------------------------------------------------------------------------------------
# the lookup rule (mirrors find_el in the Android device script)
# --------------------------------------------------------------------------------------------

class LookupRuleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = s040.tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)
        self.clock = Clock()

    def look(self, agent, find, **extra):
        scn, step = scn_with(find, **extra)
        return u.sim_lookup(agent, scn, step, self.out, clock=self.clock, sleep=self.clock.sleep)

    def fails(self, agent, find, status):
        with self.assertRaises(u.LookupFailed) as cm:
            self.look(agent, find)
        self.assertEqual(cm.exception.step, "open")
        text = (self.out / u.LOOKUP_FAILED).read_text()
        self.assertIn(f"status: {status}", text)
        return str(cm.exception), text

    def test_found_after_two_reads_that_agree(self):
        agent = FakeAgent([reply(match())])
        rec = self.look(agent, {"text": "Sign in"})
        self.assertEqual((rec["status"], rec["reads"], rec["matches"], rec["visible"]), ("found", 2, 1, 1))
        self.assertEqual(rec["center"], match()["center"])
        self.assertEqual(rec["match"], {"label": "Sign in", "type": 9, "frame": [20.0, 400.0, 100.0, 40.0]})
        self.assertEqual(rec["swipes"], [])
        self.assertEqual([c["cmd"] for c in agent.sent], ["find", "find"])
        self.assertEqual(agent.sent[0], {"cmd": "find", "text": "Sign in"})
        # desc is sent as desc
        agent = FakeAgent([reply(match())])
        self.look(agent, {"desc": "Settings"})
        self.assertEqual(agent.sent[0], {"cmd": "find", "desc": "Settings"})

    def test_one_read_is_not_enough(self):
        agent = FakeAgent([reply(), reply(match()), reply(match())])
        rec = self.look(agent, {"text": "Sign in"})
        self.assertEqual(rec["reads"], 3)
        self.assertAlmostEqual(self.clock.t, 2 * u.SIM_FIND_POLL_S)

    def test_a_match_that_moves_is_unstable(self):
        moving = [reply(match(frame=(20.0, 400.0 - 10 * i, 100.0, 40.0))) for i in range(100)]
        msg, text = self.fails(FakeAgent(moving), {"text": "Sign in", "timeout_ms": 1000}, "unstable")
        self.assertIn("step 'open': the element with the label or value \"Sign in\" did not hold still within 1000 ms", msg)
        self.assertIn("two reads in a row never gave the same frame", msg)
        self.assertIn("The rehearsal stopped there", msg)

    def test_a_match_first_seen_at_the_timeout_gets_one_more_read(self):
        finds = [reply()] * 4 + [reply(match())]
        agent = FakeAgent(finds)
        rec = self.look(agent, {"text": "Sign in", "timeout_ms": 1000})
        self.assertEqual(rec["reads"], 6)
        self.assertGreaterEqual(rec["lookup_ms"], 1000)

    def test_several_matches_without_nth(self):
        two = reply(match(), match(frame=(20.0, 600.0, 100.0, 40.0)))
        msg, _ = self.fails(FakeAgent([two]), {"text": "Sign in", "timeout_ms": 1000}, "not_found")
        self.assertIn("did not appear within 1000 ms", msg)
        self.assertIn("2 matched at the last read (exactly one needed)", msg)

    def test_nth_counts_from_one_in_the_order_of_the_tree(self):
        three = reply(match(frame=(0.0, 100.0, 50.0, 20.0)), match(frame=(0.0, 200.0, 50.0, 20.0), on=False),
                      match(frame=(0.0, 300.0, 50.0, 20.0)), match(frame=(0.0, 400.0, 50.0, 20.0)))
        rec = self.look(FakeAgent([three]), {"text": "Sign in", "nth": 2})
        self.assertEqual(rec["match"]["frame"], [0.0, 300.0, 50.0, 20.0])       # the second that counts
        self.assertEqual((rec["matches"], rec["visible"]), (4, 3))
        # too few count: the one off the screen is named, as on Android
        msg, _ = self.fails(FakeAgent([three]), {"text": "Sign in", "nth": 4, "timeout_ms": 500}, "not_visible")
        self.assertIn("(match 4 in the order of the tree) was not on the screen", msg)
        self.assertIn("4 matched at the last read, 3 of them on the screen where a tap reaches them (at least 4 needed): the element "
                      "is in the app's tree but off the screen (frame [0.0, 200.0, 50.0, 20.0]", msg)
        four = reply(*(match(frame=(0.0, 100.0 * i, 50.0, 20.0)) for i in range(1, 4)))
        msg, _ = self.fails(FakeAgent([four]), {"text": "Sign in", "nth": 4, "timeout_ms": 500}, "not_found")
        self.assertIn("3 matched at the last read (at least 4 needed)", msg)

    def test_off_the_screen(self):
        msg, _ = self.fails(FakeAgent([reply(match(frame=(20.0, 900.0, 100.0, 40.0), on=False))]),
                            {"text": "Sign in", "timeout_ms": 500}, "not_visible")
        self.assertIn("was not on the screen within 500 ms", msg)
        self.assertIn("1 matched at the last read, 0 of them on the screen where a tap reaches them (exactly one needed): the element "
                      "is in the app's tree but off the screen (frame [20.0, 900.0, 100.0, 40.0] on a 393 x 852 point "
                      "screen); add 'find.scroll' to swipe it into view", msg)

    def test_on_the_screen_but_not_hittable(self):
        msg, text = self.fails(FakeAgent([reply(match(hit=False, under="Today"))]), {"text": "Sign in", "timeout_ms": 500},
                               "not_visible")
        self.assertIn("could not be tapped within 500 ms", msg)
        self.assertIn("0 of them on the screen where a tap reaches them (exactly one needed): the element is in the "
                      "app's tree but on the screen (frame [20.0, 400.0, 100.0, 40.0]) where a tap would not reach it: "
                      "XCTest's hit test says a tap there would reach something else, probably \"Today\" (a hint from "
                      "the order of the app's tree, not a verdict); add 'find.scroll' to bring it clear of what lies "
                      "over it", msg)
        self.assertIn('"hittable": false', text)
        self.assertIn('"under": "Today"', text)
        # without the hint, no guess is made
        msg, _ = self.fails(FakeAgent([reply(match(hit=False))]), {"text": "Sign in", "timeout_ms": 500}, "not_visible")
        self.assertIn("would reach something else; add 'find.scroll'", msg)
        self.assertNotIn("probably", msg)

    def test_moved_and_not_asked_are_told_apart(self):
        msg, text = self.fails(FakeAgent([reply(match(hit=False, moved=True))]), {"text": "Sign in", "timeout_ms": 500},
                               "not_visible")
        self.assertIn("the app's tree changed between the agent's read and its hit test, so it could not be asked", msg)
        self.assertIn('"moved": true', text)
        msg, text = self.fails(FakeAgent([reply(match(hit=False, not_asked=True))]), {"text": "Sign in", "timeout_ms": 500},
                               "not_visible")
        self.assertIn("the hit test was not asked (more than ten matches on the display, or more than twenty elements "
                      "carry the label)", msg)

    def test_under_decides_nothing(self):
        # a hittable match with an 'under' hint counts; an on-screen match that is not hittable does not
        rec = self.look(FakeAgent([reply(match(hit=False), match(frame=(20.0, 300.0, 100.0, 40.0), under="Bar"))]),
                        {"text": "Sign in"})
        self.assertEqual(rec["match"]["frame"], [20.0, 300.0, 100.0, 40.0])
        self.assertEqual(rec["visible"], 1)

    def test_the_visible_part_is_recorded(self):
        cut = match(frame=(20.0, 700.0, 100.0, 80.0), visible=[20.0, 700.0, 100.0, 30.0])
        cut["center"] = [0.178, 0.8392]
        rec = self.look(FakeAgent([reply(cut)]), {"text": "Sign in"})
        self.assertEqual(rec["match"], {"label": "Sign in", "type": 9, "frame": [20.0, 700.0, 100.0, 80.0],
                                        "visible": [20.0, 700.0, 100.0, 30.0]})
        self.assertEqual(rec["center"], [0.178, 0.8392])
        rec = self.look(FakeAgent([reply(match())]), {"text": "Sign in"})
        self.assertNotIn("visible", rec["match"])

    def test_an_on_screen_match_that_cannot_be_tapped_is_named_before_one_off_the_screen(self):
        msg, _ = self.fails(FakeAgent([reply(match(frame=(20.0, 900.0, 100.0, 40.0), on=False),
                                             match(frame=(20.0, 50.0, 100.0, 40.0), hit=False))]),
                            {"text": "Sign in", "nth": 1, "timeout_ms": 500}, "not_visible")
        self.assertIn("on the screen (frame [20.0, 50.0, 100.0, 40.0]) where a tap would not reach it", msg)

    def test_scroll_reaches_it(self):
        off = reply(match(frame=(20.0, 900.0, 100.0, 40.0), on=False))
        on = reply(match())
        agent = FakeAgent([off, off, on, on])
        sc = {"from": [0.5, 0.8], "to": [0.5, 0.3], "max": 5, "settle_ms": 200}
        rec = self.look(agent, {"text": "Sign in", "scroll": sc})
        self.assertEqual([c["cmd"] for c in agent.sent], ["find", "swipe", "find", "swipe", "find", "find"])
        self.assertEqual(agent.sent[1], {"cmd": "swipe", "from": [0.5, 0.8], "to": [0.5, 0.3], "ms": 600})
        self.assertEqual(rec["swipes"], [{"from": [0.5, 0.8], "to": [0.5, 0.3], "ms": 600, "settle_ms": 200}] * 2)
        self.assertEqual(rec["reads"], 4)

    def test_scroll_runs_out(self):
        off = reply(match(frame=(20.0, 900.0, 100.0, 40.0), on=False))
        agent = FakeAgent([off])
        sc = {"from": [0.5, 0.8], "to": [0.5, 0.3], "max": 2, "settle_ms": 100}
        msg, _ = self.fails(agent, {"text": "Sign in", "scroll": sc, "timeout_ms": 2000}, "not_visible")
        self.assertEqual(sum(1 for c in agent.sent if c["cmd"] == "swipe"), 2)
        self.assertIn("(", msg)
        self.assertIn(", 2 swipes): ", msg)
        self.assertIn("; 2 swipes did not bring it onto the screen", msg)

    def test_a_swipe_the_agent_did_not_perform_fails_at_once(self):
        sc = {"from": [0.5, 0.8], "to": [0.5, 0.3], "max": 2}
        with self.assertRaises(u.ProbeError) as cm:
            self.look(FakeAgent([reply()], swipe_ok=False), {"text": "Sign in", "scroll": sc})
        self.assertIn("step 'open': the agent did not perform swipe 1 of the lookup's 'find.scroll': swipe failed", str(cm.exception))

    def test_timeout_writes_what_the_screen_held(self):
        agent = FakeAgent([reply()], labels=[{"label": "Welcome", "value": "", "type": 48, "frame": [0, 0, 1, 1]},
                                             {"label": "Phone", "value": "(555) 000-1234", "type": 49, "frame": [0, 0, 1, 1]}])
        scn = {"name": "x", "package": BUNDLE, "steps": [
            {"id": "phone", "do": "text", "text": TEXT},
            {"id": "open", "do": "tap", "find": {"text": "Sign in", "timeout_ms": 900}}]}
        with self.assertRaises(u.LookupFailed) as cm:
            u.sim_lookup(agent, scn, scn["steps"][1], self.out, clock=self.clock, sleep=self.clock.sleep)
        msg = str(cm.exception)
        self.assertIn("did not appear within 900 ms (4 reads): 0 matched at the last read (exactly one needed); what the "
                      f"screen held is in {self.out / u.LOOKUP_FAILED}", msg)
        text = (self.out / u.LOOKUP_FAILED).read_text()
        for line in ("step: open", 'looked for: the element with the label or value "Sign in"', "status: not_found",
                     "reads: 4", "timeout_ms: 900", '"label": "Welcome"', "what the app's tree held (labels;"):
            self.assertIn(line, text)
        self.assertEqual(agent.sent[-1], {"cmd": "labels"})
        # with a 'text' step, no element value is written at all (a field shows typed text reformatted)
        self.assertNotIn(TEXT, text)
        self.assertNotIn('"value"', text)
        self.assertIn("no element value is written, since the scenario types text that a field may show reformatted",
                      text)
        self.assertIn('"label": "Phone"', text)

    def test_values_are_kept_when_nothing_is_typed(self):
        agent = FakeAgent([reply()], labels=[{"label": "Phone", "value": "(555) 000-1234", "type": 49, "frame": [0, 0, 1, 1]}])
        with self.assertRaises(u.LookupFailed):
            self.look(agent, {"text": "Sign in", "timeout_ms": 300})
        text = (self.out / u.LOOKUP_FAILED).read_text()
        self.assertIn('"value": "(555) 000-1234"', text)
        self.assertNotIn("no element value is written", text)

    def test_more_matches_than_listed_cannot_be_exactly_one(self):
        msg, _ = self.fails(FakeAgent([reply(match(), found=60)]), {"text": "Sign in", "timeout_ms": 500}, "not_found")
        self.assertIn("60 matched at the last read (exactly one needed); only the first 1 are listed", msg)

    def test_a_find_the_agent_refuses_fails_at_once(self):
        with self.assertRaises(u.ProbeError) as cm:
            self.look(FakeAgent([{"ok": False, "error": "the app is not running"}]), {"text": "Sign in"})
        self.assertIn("step 'open': the agent did not perform the lookup of the element with the label or value "
                      "\"Sign in\": the app is not running; the rehearsal stopped there", str(cm.exception))
        self.assertNotIsInstance(cm.exception, u.LookupFailed)


# --------------------------------------------------------------------------------------------
# the scenario, in its two modes
# --------------------------------------------------------------------------------------------

class ScrubTest(unittest.TestCase):
    def test_the_typed_characters_leave_the_log(self):
        with s040.tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "agent.log"
            log.write_text("Test Suite 'Selected tests' started\n"
                           "    t =    14.11s Type 'q' into Any (First Match)\n"
                           "    t =    15.62s Type \"Z\" into TextField\n"
                           "    t =    16.00s Type \u2018k\u2019 into Any (First Match)\n"
                           "    t =    17.20s Tap \"Sign in\" Button\n")
            self.assertEqual(u.scrub_agent_log(log), 3)
            self.assertEqual(log.read_text().splitlines(), [
                "Test Suite 'Selected tests' started",
                f"    t =    14.11s Type '{u.SIM_REDACTED}' into Any (First Match)",
                f'    t =    15.62s Type "{u.SIM_REDACTED}" into TextField',
                f"    t =    16.00s Type \u2018{u.SIM_REDACTED}\u2019 into Any (First Match)",
                '    t =    17.20s Tap "Sign in" Button'])
            self.assertEqual(u.scrub_agent_log(Path(tmp) / "absent.log"), 0)


class SimModesTest(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(u, "SIM_REST_MIN_MS", 100)
        p.start()
        self.addCleanup(p.stop)

    def reject(self, step, fragment, mode="run"):
        with self.assertRaises(u.ProbeError) as cm:
            u.validate_scenario({"name": "m", "package": BUNDLE, "steps": [step]}, u.SIM_PLATFORM, mode)
        self.assertIn(fragment, str(cm.exception))

    def ok(self, step, mode="run"):
        u.validate_scenario({"name": "m", "package": BUNDLE, "steps": [step]}, u.SIM_PLATFORM, mode)

    def test_find_in_both_modes(self):
        sc = {"from": [0.5, 0.8], "to": [0.5, 0.3], "max": 3}
        for mode in u.SIM_MODES:
            self.ok({"id": "f", "do": "tap", "find": {"text": "Go", "nth": 2, "timeout_ms": 500, "scroll": sc},
                     "expect": {"signature": "T"}}, mode)
            self.ok({"id": "f", "do": "tap", "find": {"desc": "Settings"}}, mode)
            self.reject({"id": "f", "do": "tap", "find": {"text": "Go", "package": "com.android.systemui"}},
                        "step 'f': 'find.package' is refused on the iOS Simulator: it names the package of an Android "
                        "window", mode)
            self.reject({"id": "f", "do": "tap", "find": {"text": "Go"}, "find_settle_ms": 500},
                        "step 'f': 'find_settle_ms' is refused on the iOS Simulator", mode)
            self.reject({"id": "f", "do": "tap", "find": {"text": "Go", "scroll": {**sc, "ms": 60001}}},
                        "step 'f': 'find.scroll.ms' on the iOS Simulator is at most 60000", mode)
            self.reject({"id": "k", "do": "key", "code": 4}, "step 'k': a 'key' step is refused on the iOS Simulator", mode)

    def test_text_in_a_rehearsal_only(self):
        self.ok({"id": "t", "do": "text", "text": TEXT, "settle_ms": 100}, "rehearse")
        self.reject({"id": "t", "do": "text", "text": TEXT},
                    "step 't': a 'text' step is refused in a measured run on the iOS Simulator: a field that has had "
                    "focus changes the app for the rest of its life, so a measured run never types: put the typing in a "
                    "scenario of its own and drive it with `ui-probe rehearse` (for example to sign in before the "
                    "measured scenarios)")
        self.reject({"id": "t", "do": "text", "text": "a%b"}, "'text' may hold only letters", "rehearse")

    def test_rehearse_checks_expect_allow_and_rests_like_a_run(self):
        self.ok({"id": "r", "do": "rest", "ms": 5000, "allow": {"frames_max": 3, "reason": "caret"}}, "rehearse")
        self.reject({"id": "r", "do": "rest", "ms": 50}, "lasts at least 100 ms", "rehearse")
        self.reject({"id": "r", "do": "rest", "ms": 500, "expect": {"frames": 0}}, "takes only 'signposts_max'",
                    "rehearse")

    def test_a_value_that_cannot_be_matched_has_its_own_wording(self):
        self.reject({"id": "f", "do": "tap", "find": {"text": "a\tb"}},
                    "step 'f': 'find.text' holds '\\t' (U+0009), a control character or a lone surrogate; on the iOS "
                    "Simulator a lookup value must be printable text")
        with self.assertRaises(u.ProbeError) as cm:
            u.validate_scenario({"name": "m", "package": BUNDLE, "steps": [
                {"id": "f", "do": "tap", "find": {"text": "a\tb"}}]}, u.SIM_PLATFORM)
        self.assertNotIn("window-tree dump", str(cm.exception))
        self.ok({"id": "f", "do": "tap", "find": {"text": "Go \U0001F600"}})       # a label can hold an emoji
        with self.assertRaises(u.ProbeError) as cm:
            u.validate_scenario({"name": "a", "package": "com.example.app", "steps": [
                {"id": "f", "do": "tap", "find": {"text": "a\tb"}}]})
        self.assertIn("window-tree dump", str(cm.exception))              # Android's wording, unchanged

    def test_android_is_unchanged(self):
        u.validate_scenario({"name": "a", "package": "com.example.app", "steps": [
            {"id": "f", "do": "tap", "find": {"text": "Go", "package": "com.android.systemui"}, "find_settle_ms": 500},
            {"id": "t", "do": "text", "text": "abc"}]})


# --------------------------------------------------------------------------------------------
# digest, app identity and the checks of a run
# --------------------------------------------------------------------------------------------

def make_app(folder: Path, short="1.2", version="34", exe=b"\x00binary") -> Path:
    app = folder / "Example.app"
    app.mkdir(parents=True, exist_ok=True)
    with open(app / "Info.plist", "wb") as fh:
        plistlib.dump({"CFBundleShortVersionString": short, "CFBundleVersion": version, "CFBundleExecutable": "Example"},
                      fh, fmt=plistlib.FMT_BINARY)
    (app / "Example").write_bytes(exe)
    return app


FIND_SCN = {"name": "Sim find", "package": BUNDLE, "launch_settle_ms": 0, "steps": [
    {"id": "open", "do": "tap", "settle_ms": 50, "expect": {"signature": "T"},
     "find": {"text": "Sign in", "scroll": {"from": [0.5, 0.8], "to": [0.5, 0.3], "max": 3, "ms": 250, "settle_ms": 20}}},
    {"id": "rest-a", "do": "rest", "ms": 100},
    {"id": "pull", "do": "swipe", "from": [0.5, 0.3], "to": [0.5, 0.7], "ms": 200, "judge": "state", "settle_ms": 50}]}


class ChecksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = s040.tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = make_app(Path(self.tmp.name))
        self.ident = u.app_identity(str(self.app))
        self.now = 2_000_000_000.0
        self.data = {
            "tool": {"name": "ui-probe", "version": u.VERSION}, "platform": u.SIM_PLATFORM, "scenario": FIND_SCN["name"],
            "scenario_digest": u.scenario_digest(FIND_SCN), "udid": UDID, "screen": [393.0, 852.0], "app": self.ident,
            "finished": u.utc_stamp(self.now - 60),
            "steps": {"open": {"do": "tap", "center": [0.2, 0.5], "swipes": [
                {"from": [0.5, 0.8], "to": [0.5, 0.3], "ms": 250, "settle_ms": 20}]}}}

    def check(self, data=None, scn=FIND_SCN, udid=UDID, max_age=3600, ident=None):
        u.check_rehearsal({"path": "r/rehearsal.json", "data": data or self.data}, scn, udid, max_age,
                          ident or self.ident, self.now)

    def refused(self, fragment, **kw):
        with self.assertRaises(u.ProbeError) as cm:
            self.check(**kw)
        self.assertTrue(str(cm.exception).startswith("--rehearsal r/rehearsal.json: "), str(cm.exception))
        self.assertIn(fragment, str(cm.exception))

    def test_digest_is_canonical(self):
        a = {"b": 1, "a": [1, {"y": 2, "x": "é"}]}
        b = json.loads('{"a": [1, {"x": "é", "y": 2}], "b": 1}')
        self.assertEqual(u.scenario_digest(a), u.scenario_digest(b))
        import hashlib
        self.assertEqual(u.scenario_digest(a),
                         hashlib.sha256(b'{"a":[1,{"x":"\\u00e9","y":2}],"b":1}').hexdigest())
        self.assertNotEqual(u.scenario_digest(a), u.scenario_digest({**a, "b": 2}))

    def test_app_identity(self):
        self.assertEqual((self.ident["short_version"], self.ident["version"], self.ident["executable"]), ("1.2", "34", "Example"))
        import hashlib
        self.assertEqual(self.ident["executable_sha256"], hashlib.sha256(b"\x00binary").hexdigest())
        self.assertEqual(self.ident["unknown"], [])
        (self.app / "Info.plist").write_text("not a plist")
        bad = u.app_identity(str(self.app))
        self.assertIsNone(bad["version"])
        self.assertIn("Info.plist cannot be read", bad["unknown"][0])
        make_app(Path(self.tmp.name))
        (self.app / "Example").unlink()
        self.assertIsNone(u.app_identity(str(self.app))["executable_sha256"])
        self.assertIsNone(u.app_identity(None, "not installed")["short_version"])

    def test_a_good_rehearsal(self):
        self.check()

    def test_another_scenario(self):
        changed = json.loads(json.dumps(FIND_SCN))
        changed["steps"][1]["ms"] = 200
        self.refused("it rehearsed another scenario, or this one before it changed", scn=changed)

    def test_another_simulator(self):
        self.refused(f"it was made on simulator {UDID}, not on 99999999-0000-0000-0000-000000000000",
                     udid="99999999-0000-0000-0000-000000000000")

    def test_a_step_it_does_not_cover(self):
        self.refused("it holds no position for the tap with 'find' 'open'", data={**self.data, "steps": {}})
        bad = {**self.data, "steps": {"open": {"do": "tap", "center": [1.5, 0.5], "swipes": []}}}
        self.refused("it holds no position for the tap with 'find' 'open'", data=bad)

    def test_the_app_changed(self):
        other = u.app_identity(str(make_app(Path(self.tmp.name) / "b", exe=b"other")))
        self.refused("the app changed since the rehearsal: its executable's sha256 was", ident=other)
        other = u.app_identity(str(make_app(Path(self.tmp.name) / "c", version="35")))
        self.refused("the app changed since the rehearsal: its CFBundleVersion was 34, is 35", ident=other)

    def test_an_identity_that_cannot_be_read_fails(self):
        self.refused("the app's CFBundleShortVersionString is unknown now (not installed), so whether the app changed "
                     "since cannot be told", ident=u.app_identity(None, "not installed"))
        self.refused("is unknown in the rehearsal", data={**self.data, "app": u.app_identity(None, "gone")})

    def test_too_old_and_from_the_future(self):
        self.refused("it finished 60 s ago", max_age=59)
        self.check(max_age=60)
        self.refused("after now; the host's clock has moved back", data={**self.data, "finished": u.utc_stamp(self.now + 30)})
        self.refused("its finish time 'yesterday' cannot be read", data={**self.data, "finished": "yesterday"})

    def test_not_a_rehearsal(self):
        self.refused("not a rehearsal.json written by `ui-probe rehearse`", data={"tool": {"name": "x"}})


# --------------------------------------------------------------------------------------------
# the two commands, end to end with the stand-ins
# --------------------------------------------------------------------------------------------

OFF = reply(match(frame=(20.0, 900.0, 100.0, 40.0), on=False))
ON = reply(match())


class RehearsalBase(t050.InputBase):
    def setUp(self):
        super().setUp()
        for name, value in (("SIM_FIND_POLL_S", 0.01), ("REHEARSAL_GONE_WAIT_S", 0.5)):
            p = mock.patch.object(u, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.app = make_app(self.root / "apps")
        (self.state / "container").write_text(str(self.app))
        (self.state / "finds.json").write_text(json.dumps([OFF, ON]))

    def rehearse(self, *extra, scn=FIND_SCN, out="reh"):
        return main("rehearse", "--simulator", UDID, "--scenario", self.scenario(scn), "--out", str(self.root / out), *extra)

    def run_with(self, *extra, scn=FIND_SCN, out="out", reh="reh"):
        return self.run_cli("--rehearsal", str(self.root / reh / "rehearsal.json"), *extra, scn=scn, out=out)

    def app_ended(self, at=None):
        """The app terminated after the rehearsal's steps, and its process checked with ps after that."""
        log = self.log()
        last_launch = max(i for i, l in enumerate(log) if l == f"simctl launch {UDID} {BUNDLE}")
        self.assertIn(f"simctl terminate {UDID} {BUNDLE}", log[last_launch + 1:])
        self.assertTrue(self.lines("ps.log"))


class RehearseTest(RehearsalBase):
    def test_a_rehearsal(self):
        scn = {**FIND_SCN, "steps": FIND_SCN["steps"] + [
            {"id": "phone", "do": "text", "text": TEXT, "settle_ms": 10}]}
        scn["steps"][1] = {**scn["steps"][1], "ms": 400}                  # a rest waits its full ms
        started = time.monotonic()
        rc, out, err = self.rehearse(scn=scn)
        self.assertGreaterEqual(time.monotonic() - started, 0.4)
        self.assertEqual(rc, 0, err)
        folder = self.root / "reh"
        rec = json.loads((folder / "rehearsal.json").read_text())
        # what it holds
        self.assertEqual(rec["tool"], {"name": "ui-probe", "version": u.VERSION})
        self.assertEqual((rec["platform"], rec["scenario"], rec["udid"]), (u.SIM_PLATFORM, "Sim find", UDID))
        self.assertEqual(rec["scenario_digest"], u.scenario_digest(scn))
        self.assertEqual(rec["screen"], [393.0, 852.0])
        self.assertEqual(rec["device"]["app_container"], str(self.app))
        self.assertEqual((rec["app"]["short_version"], rec["app"]["version"]), ("1.2", "34"))
        self.assertEqual(len(rec["app"]["executable_sha256"]), 64)
        for k in ("started", "finished"):
            self.assertIsNotNone(u.parse_utc_stamp(rec[k]), k)
        self.assertEqual(list(rec["steps"]), ["open", "rest-a", "pull", "phone"])
        o = rec["steps"]["open"]
        self.assertEqual(o["find"], FIND_SCN["steps"][0]["find"])
        self.assertEqual(o["match"], {"label": "Sign in", "type": 9, "frame": [20.0, 400.0, 100.0, 40.0]})
        self.assertEqual(o["center"], match()["center"])
        self.assertEqual(o["swipes"], [{"from": [0.5, 0.8], "to": [0.5, 0.3], "ms": 250, "settle_ms": 20}])
        self.assertEqual((o["reads"], o["matches"]), (3, 1))
        self.assertIsInstance(o["lookup_ms"], int)
        self.assertEqual(rec["steps"]["phone"]["typed"], len(TEXT))
        self.assertEqual(rec["steps"]["rest-a"], {"do": "rest", "waited_ms": 400, "app_state": 4})
        self.assertEqual({sid: st["app_state"] for sid, st in rec["steps"].items()},
                         {"open": 4, "rest-a": 4, "pull": 4, "phone": 4})
        self.assertEqual(rec["app_ended"]["gone"], True)
        # never the text
        for path in folder.rglob("*"):
            if path.is_file():
                self.assertNotIn(TEXT, path.read_text(errors="replace"), path)
        self.assertNotIn(TEXT, out)
        # the commands, in order: the lookup (a read, a swipe, two reads), the tap at the centre found
        cmds = self.cmds()
        # the ready ping, a ping after the launch settle, then each step ends with a ping for the app's state
        self.assertEqual([c["cmd"] for c in cmds], ["ping", "ping", "find", "swipe", "find", "find", "tap", "ping", "ping",
                                                    "swipe", "ping", "type", "ping", "quit"])
        self.assertEqual(cmds[6], {"cmd": "tap", "x": match()["center"][0], "y": match()["center"][1]})
        env = json.loads((self.state / "agent-env.json").read_text())["env"]
        self.assertEqual(env["TEST_RUNNER_UI_PROBE_AGENT_LOOKUPS"], "1")
        self.assertGreaterEqual(int(env["TEST_RUNNER_UI_PROBE_AGENT_IDLE_S"]), int(u.type_timeout_s(TEXT)) + 120)
        # printed: one line per step, then the file
        lines = [json.loads(l) for l in out.splitlines()]
        self.assertEqual([l.get("step") for l in lines[:-1]], ["open", "rest-a", "pull", "phone"])
        self.assertEqual(lines[0]["center"], match()["center"])
        self.assertEqual(lines[3], {"step": "phone", "do": "text", "typed": len(TEXT)})
        self.assertEqual(lines[-1], {"rehearsal": str(folder / "rehearsal.json")})
        # nothing measured, the status bar untouched; the app ended, the agent stopped
        log = self.log()
        self.assertFalse([l for l in log if "status_bar" in l or "recordVideo" in l or "log show" in l])
        self.app_ended()
        self.assertTrue(self.gone("agent-pids"))
        self.assertEqual([l for l in log if l.endswith(u.AGENT_RUNNER)],
                         [f"simctl terminate {UDID} {u.AGENT_RUNNER}", f"simctl uninstall {UDID} {u.AGENT_RUNNER}"])
        self.assertEqual({p.name for p in (folder / "raw").iterdir()} >= {"agent.log", "agent.json", "launch.txt"}, True)
        self.assertFalse((folder / "failed.json").exists())
        self.assertTrue(json.loads((folder / "raw" / "agent.json").read_text())["lookups"])
        # the agent's log keeps its typing lines without the characters; its results are not kept
        log = (folder / "raw" / "agent.log").read_text()
        typed = [l for l in log.splitlines() if " Type " in l]
        self.assertEqual(len(typed), len(TEXT))
        self.assertTrue(all(l.endswith(f"Type '{u.SIM_REDACTED}' into Any (First Match)") for l in typed), typed)
        self.assertEqual(rec["agent_results"], {"kept": False, "why": u.AGENT_RESULTS_NOT_KEPT})
        self.assertFalse((folder / "raw" / "agent-derived-data").exists())
        self.assertFalse((folder / "raw" / "agent.xcresult").exists())

    def test_the_app_is_launched_before_the_agent_starts(self):
        rc, _, err = self.rehearse()
        self.assertEqual(rc, 0, err)
        # the stand-in's launch notes whether the agent had started then
        self.assertEqual(self.lines("launch-order"), ["no agent yet"])
        log = self.log()
        self.assertLess(log.index(f"simctl launch {UDID} {BUNDLE}"),
                        len(log) - log[::-1].index(f"simctl terminate {UDID} {BUNDLE}") - 1)
        self.assertEqual(self.cmds()[0]["cmd"], "ping")

    def test_the_launch_settle_is_not_cut_short(self):
        first_find = []
        real_send = u.SimAgent.send

        def send(agent, command, timeout=u.AGENT_COMMAND_TIMEOUT_S):
            if command["cmd"] == "find" and not first_find:
                first_find.append(time.time())
            return real_send(agent, command, timeout)
        with mock.patch.object(u.SimAgent, "send", send):
            rc, _, err = self.rehearse(scn={**FIND_SCN, "launch_settle_ms": 1500})
        self.assertEqual(rc, 0, err)
        launch = json.loads((self.root / "reh" / "raw" / "launch-time.json").read_text())["wall"]
        self.assertGreaterEqual(first_find[0] - launch, 1.5)

    def test_an_agent_that_fails_to_start_after_the_launch(self):
        (self.state / "agent.mode").write_text("exit")
        rc, _, err = self.rehearse()
        self.assertEqual(rc, 2)
        self.assertIn("xcodebuild exited (status 70) before the input agent answered", err)
        self.app_ended()
        self.assertTrue((self.root / "reh" / "failed.json").exists())
        self.assertTrue((self.root / "reh" / u.REHEARSAL_MARKER).exists())
        self.assertFalse((self.root / "reh" / "rehearsal.json").exists())
        self.assertTrue(self.gone("agent-pids"))

    def test_refusals_before_the_folder_exists(self):
        for argv, fragment in (
                (["--serial", "emulator-x"], "rehearse is for the iOS Simulator (--simulator); on Android a lookup "
                                             "happens inside the run"),):
            rc, _, err = main("rehearse", *argv, "--scenario", self.scenario(FIND_SCN), "--out", str(self.root / "x"))
            self.assertEqual(rc, 2)
            self.assertIn(fragment, err)
        rc, _, err = main("rehearse", "--scenario", self.scenario(FIND_SCN), "--out", str(self.root / "x"))
        self.assertEqual((rc, "rehearse needs --simulator" in err), (2, True))
        rc, _, err = self.rehearse(scn={**FIND_SCN, "steps": [{"id": "k", "do": "key", "code": 4}]}, out="x")
        self.assertEqual(rc, 2)
        self.assertIn("a 'key' step is refused on the iOS Simulator", err)
        self.assertFalse((self.root / "x").exists())
        for marker in ("rehearsal.json", "raw", "run.json"):
            used = self.root / f"used-{marker}"
            used.mkdir()
            (used / marker).mkdir() if marker == "raw" else (used / marker).write_text("{}")
            rc, _, err = self.rehearse(out=used.name)
            self.assertEqual(rc, 2, marker)
            self.assertIn("holds a rehearsal or what is left of one" if marker == "rehearsal.json"
                          else "already holds a run or what is left of one", err)
        self.assertEqual(self.cmds(), [])
        self.assertEqual(self.log(), [])

    def test_a_lookup_that_fails(self):
        (self.state / "finds.json").write_text(json.dumps([OFF]))
        (self.state / "labels.json").write_text(json.dumps([{"label": "Welcome", "value": "", "type": 48,
                                                             "frame": [0, 0, 1, 1]}]))
        scn = json.loads(json.dumps(FIND_SCN))
        scn["steps"][0]["find"]["timeout_ms"] = 300
        rc, out, err = self.rehearse(scn=scn)
        self.assertEqual(rc, 2)
        folder = self.root / "reh"
        self.assertIn("step 'open': the element with the label or value \"Sign in\" was not on the screen within 300 ms", err)
        self.assertIn("3 swipes did not bring it onto the screen", err)
        self.assertIn('"label": "Welcome"', (folder / u.LOOKUP_FAILED).read_text())
        failed = json.loads((folder / "failed.json").read_text())
        self.assertEqual((failed["step"], failed["scenario"]), ("open", "Sim find"))
        self.assertFalse((folder / "rehearsal.json").exists())
        self.app_ended()
        self.assertTrue(self.gone("agent-pids"))
        self.assertEqual(self.cmds()[-2:], [{"cmd": "labels"}, {"cmd": "quit"}])

    def test_typing_the_agent_refuses(self):
        (self.state / "type.mode").write_text("nofocus")
        rc, _, err = self.rehearse(scn={**FIND_SCN, "steps": [{"id": "phone", "do": "text", "text": TEXT}]})
        self.assertEqual(rc, 2)
        self.assertIn("step 'phone': the agent did not perform the typing: no element has keyboard focus (typed 0 of 10 "
                      "characters); tap the field first; the rehearsal stopped there", err)
        self.app_ended()

    def test_results_are_not_kept_when_the_scenario_types_whatever_the_status(self):
        (self.state / "agent.mode").write_text("issue")         # the test run ends with status 65 after quit
        rc, _, err = self.rehearse(scn={**FIND_SCN, "steps": [{"id": "phone", "do": "text", "text": TEXT}]})
        self.assertEqual(rc, 0, err)
        raw = self.root / "reh" / "raw"
        self.assertEqual(json.loads((raw / "agent.json").read_text())["stop"]["exit"], 65)
        self.assertFalse((raw / "agent-derived-data").exists())
        self.assertFalse((raw / "agent.xcresult").exists())
        # without typing, a status other than 0 keeps them, as after a run
        rc, _, err = self.rehearse(scn={**FIND_SCN, "steps": [{"id": "r", "do": "rest", "ms": 100}]}, out="reh2")
        self.assertEqual(rc, 0, err)
        self.assertTrue((self.root / "reh2" / "raw" / "agent.xcresult").exists())
        self.assertNotIn("agent_results", json.loads((self.root / "reh2" / "rehearsal.json").read_text()))
        # a rehearsal that types and fails says so in failed.json
        (self.state / "agent.mode").write_text("ok")
        (self.state / "type.mode").write_text("nofocus")
        rc, _, _ = self.rehearse(scn={**FIND_SCN, "steps": [{"id": "phone", "do": "text", "text": TEXT}]}, out="reh3")
        self.assertEqual(rc, 2)
        failed = json.loads((self.root / "reh3" / "failed.json").read_text())
        self.assertEqual(failed["agent_results"], {"kept": False, "why": u.AGENT_RESULTS_NOT_KEPT})
        self.assertFalse((self.root / "reh3" / "raw" / "agent.xcresult").exists())

    def test_the_app_must_be_in_front(self):
        (self.state / "appstate").write_text("2")
        rc, _, err = self.rehearse()
        self.assertEqual(rc, 2)
        self.assertIn("the app was not in front after the launch (its state: running in the background, suspended); "
                      "the rehearsal stopped there", err)
        self.assertNotIn("find", [c["cmd"] for c in self.cmds()])
        self.app_ended()
        # after a step: the state changes once the tap is sent
        (self.state / "appstate").write_text("4")
        real_send = u.SimAgent.send

        def send(agent, command, timeout=u.AGENT_COMMAND_TIMEOUT_S):
            reply = real_send(agent, command, timeout)
            if command["cmd"] == "tap":
                (self.state / "appstate").write_text("3")
            return reply
        with mock.patch.object(u.SimAgent, "send", send):
            rc, _, err = self.rehearse(out="reh2")
        self.assertEqual(rc, 2)
        self.assertIn("the app was not in front at the end of 'open' (its state: running in the background); the "
                      "rehearsal stopped there", err)
        self.assertEqual(json.loads((self.root / "reh2" / "failed.json").read_text())["step"], "open")

    def test_typing_that_loses_a_character(self):
        (self.state / "type.mode").write_text("short")
        rc, _, err = self.rehearse(scn={**FIND_SCN, "steps": [{"id": "phone", "do": "text", "text": TEXT}]})
        self.assertEqual(rc, 2)
        self.assertIn("step 'phone': the agent typed 9 of 10 characters; the rehearsal stopped there", err)
        self.assertNotIn(TEXT, err)

    def test_an_app_that_outlives_its_termination(self):
        (self.state / "undying").write_text("")
        rc, _, err = self.rehearse()
        self.assertEqual(rc, 2)
        self.assertIn(f"the rehearsed app's process {s040.PID} was still running 0.5 s after it was terminated, and a run "
                      "would measure it", err)
        self.assertFalse((self.root / "reh" / "rehearsal.json").exists())
        self.assertTrue((self.root / "reh" / "failed.json").exists())
        self.assertTrue(self.gone("agent-pids"))
        # on a rehearsal that failed already, the first failure is reported and this one added
        (self.state / "finds.json").write_text(json.dumps([OFF]))
        scn = json.loads(json.dumps(FIND_SCN))
        scn["steps"][0]["find"]["timeout_ms"] = 200
        rc, _, err = self.rehearse(scn=scn, out="reh2")
        self.assertEqual(rc, 2)
        self.assertIn("was not on the screen within 200 ms", err)
        self.assertIn("ui-probe: also: the rehearsed app's process", err)

    def test_an_agent_in_the_wrong_mode(self):
        (self.state / "lookups.mode").write_text("off")
        rc, _, err = self.rehearse()
        self.assertEqual(rc, 2)
        self.assertIn("the input agent was started for a rehearsal but its first answer does not say that it serves "
                      "lookups ('lookups': false)", err)
        self.app_ended()                     # launched before the agent started, so terminated after
        self.assertTrue(self.gone("agent-pids"))
        (self.state / "xcrun.log").unlink()
        (self.state / "lookups.mode").write_text("on")
        rc, _, err = self.run_cli(scn=t050.INPUT_SCN, out="m")
        self.assertEqual(rc, 2)
        self.assertIn("the input agent was started for a measured run but its first answer does not say that lookups "
                      "are off ('lookups': true); the app was not launched", err)
        self.assertFalse([l for l in self.log() if l == f"simctl launch {UDID} {BUNDLE}"])

    def test_a_measured_agent_never_inherits_the_lookup_switch(self):
        with mock.patch.dict(u.os.environ, {u.AGENT_LOOKUPS_ENV: "1"}):
            rc, _, err = self.run_cli(scn=t050.INPUT_SCN)
        self.assertEqual(rc, 0, err)
        env = json.loads((self.state / "agent-env.json").read_text())["env"]
        self.assertNotIn(u.AGENT_LOOKUPS_ENV, env)


class RunWithRehearsalTest(RehearsalBase):
    def setUp(self):
        super().setUp()
        self.video["open"] = t050.sig_block("open", "T")
        self.video["pull"] = t050.sig_block("pull", "E")

    def rehearsed(self, scn=FIND_SCN):
        rc, _, err = self.rehearse(scn=scn)
        self.assertEqual(rc, 0, err)
        (self.state / "agent-cmds").unlink()
        return json.loads((self.root / "reh" / "rehearsal.json").read_text())

    def test_the_replay(self):
        rec = self.rehearsed()
        events = []
        real_rec, real_send = u.SimRecorder, u.SimAgent.send

        class Recorder(real_rec):
            def __init__(self, udid, path):
                events.append(("recorder", Path(path).name))
                super().__init__(udid, path)

        def send(agent, command, timeout=u.AGENT_COMMAND_TIMEOUT_S):
            events.append(("cmd", command["cmd"]))
            return real_send(agent, command, timeout)

        with mock.patch.object(u, "SimRecorder", Recorder), mock.patch.object(u.SimAgent, "send", send):
            rc, out, err = self.run_with("--runs", "2")
        self.assertEqual(rc, 0, err)
        # the rehearsal's swipe before the step's recorder starts, then the tap at the centre found
        first = events[:events.index(("recorder", "rec-rest-a.mp4"))]
        self.assertEqual(first, [("cmd", "swipe"), ("recorder", "rec-open.mp4"), ("cmd", "tap"), ("cmd", "ping")])
        cmds = self.cmds()
        self.assertEqual([c["cmd"] for c in cmds[:7]], ["ping", "swipe", "tap", "ping", "ping", "swipe", "ping"])
        self.assertEqual(cmds[1], {"cmd": "swipe", "from": [0.5, 0.8], "to": [0.5, 0.3], "ms": 250})
        self.assertEqual(cmds[2], {"cmd": "tap", "x": rec["steps"]["open"]["center"][0], "y": rec["steps"]["open"]["center"][1]})
        self.assertNotIn("find", [c["cmd"] for c in cmds])
        env = json.loads((self.state / "agent-env.json").read_text())["env"]
        self.assertNotIn(u.AGENT_LOOKUPS_ENV, env)
        for rid in ("run-01", "run-02"):                         # --runs N: the same rehearsal for each
            run = self.root / "out" / rid
            step = json.loads((run / "raw" / "step-open.json").read_text())
            resolved = step["resolved"]
            self.assertEqual((resolved["x"], resolved["y"]), tuple(rec["steps"]["open"]["center"]))
            self.assertEqual(resolved["rehearsal"], str(self.root / "reh" / "rehearsal.json"))
            self.assertEqual(resolved["rehearsal_finished"], rec["finished"])
            self.assertEqual(len(resolved["swipes"]), 1)
            sw = resolved["swipes"][0]
            self.assertEqual((sw["from"], sw["to"], sw["ms"], sw["settle_ms"], sw["reply"]["ok"]),
                             ([0.5, 0.8], [0.5, 0.3], 250, 20, True))
            self.assertIsInstance(sw["round_trip_ms"], int)
            res = json.loads((run / "run.json").read_text())
            self.assertEqual(next(s for s in res["steps"] if s["id"] == "open")["resolved"], resolved)
        rc, text, _ = self.gate()
        self.assertEqual(rc, 0, text)
        notes = [l for l in text.splitlines() if "went to positions found by the rehearsal" in l]
        self.assertEqual(len(notes), 2)
        self.assertIn(f"1 tap by 'find' went to positions found by the rehearsal {self.root / 'reh' / 'rehearsal.json'} "
                      f"(finished {rec['finished']}); the measured run looked nothing up", notes[0])

    def test_a_replayed_swipe_the_agent_did_not_perform_is_gated(self):
        self.rehearsed()
        (self.state / "agent.mode").write_text("refuse-swipe")
        rc, _, err = self.run_with()
        self.assertEqual(rc, 0, err)
        rc, text, _ = self.gate()
        self.assertEqual(rc, 1)
        self.assertIn("FAIL [input] open: the agent did not perform swipe 1 of 1 replayed from the rehearsal before the "
                      "tap: swipe failed: the drag was not delivered", text)
        self.assertIn("FAIL [input] pull: the agent did not perform the swipe: swipe failed", text)

    def test_a_screen_of_another_size(self):
        self.rehearsed()
        (self.state / "screen.json").write_text("[430.0, 932.0]")
        rc, _, err = self.run_with()
        self.assertEqual(rc, 2)
        self.assertIn("the simulator's screen is [430.0, 932.0] points but the rehearsal", err)
        self.assertIn("found its positions on [393.0, 852.0]; rehearse again on this simulator", err)
        self.assertFalse([l for l in self.log()[-6:] if l == f"simctl launch {UDID} {BUNDLE}"])
        self.assertTrue((self.root / "out" / "run-01" / "failed.json").exists())

    def test_refusals_before_the_first_run(self):
        rc, _, err = self.run_cli(scn=FIND_SCN)
        self.assertEqual(rc, 2)
        self.assertIn("step 'open': a tap with 'find' on the iOS Simulator goes to a position found by a rehearsal, since "
                      f"a measured run never reads the app: run `ui-probe rehearse --simulator {UDID} --scenario "
                      f"{self.root / 'scenario.json'} --out <dir>` first and pass its rehearsal.json with --rehearsal", err)
        self.rehearsed()
        cases = [
            (["--rehearsal-max-age-s", "0"], "--rehearsal-max-age-s must be a whole number of seconds of at least 1, not '0'"),
            (["--rehearsal-max-age-s", "1.5"], "must be a whole number of seconds of at least 1, not '1.5'"),
        ]
        for extra, fragment in cases:
            rc, _, err = self.run_with(*extra)
            self.assertEqual(rc, 2, extra)
            self.assertIn(fragment, err)
        rc, _, err = self.run_cli("--rehearsal-max-age-s", "10", scn=FIND_SCN)
        self.assertIn("--rehearsal-max-age-s applies only with --rehearsal", err)
        rc, _, err = self.run_with(scn=t050.INPUT_SCN)
        self.assertIn("--rehearsal applies only to a scenario with a tap by 'find'", err)
        path = self.root / "a.json"
        path.write_text(json.dumps({**s040.SCN, "package": "com.example.app"}))
        rc, _, err = main("run", "--serial", "emulator-x", "--scenario", str(path), "--out", str(self.root / "a"),
                          "--rehearsal", str(self.root / "reh" / "rehearsal.json"))
        self.assertEqual(rc, 2)
        self.assertIn("--rehearsal and --rehearsal-max-age-s apply only with --simulator", err)
        # the checks against what is about to be measured
        changed = json.loads(json.dumps(FIND_SCN))
        changed["steps"][1]["ms"] = 200
        rc, _, err = self.run_with(scn=changed)
        self.assertIn("it rehearsed another scenario, or this one before it changed", err)
        (self.app / "Example").write_bytes(b"rebuilt")
        rc, _, err = self.run_with()
        self.assertIn("the app changed since the rehearsal: its executable's sha256 was", err)
        (self.state / "container").unlink()                     # /containers/app: no Info.plist there
        rc, _, err = self.run_with()
        self.assertIn("is unknown now", err)
        self.assertFalse((self.root / "out").exists())
        self.assertEqual(self.cmds(), [])

    def test_too_old(self):
        self.rehearsed()
        path = self.root / "reh" / "rehearsal.json"
        rec = json.loads(path.read_text())
        rec["finished"] = u.utc_stamp(time.time() - 7200)
        path.write_text(json.dumps(rec))
        rc, _, err = self.run_with()
        self.assertEqual(rc, 2)
        self.assertIn("more than --rehearsal-max-age-s 3600; rehearse again", err)
        rc, _, err = self.run_with("--rehearsal-max-age-s", "10000")
        self.assertEqual(rc, 0, err)

    def test_another_simulator(self):
        self.rehearsed()
        path = self.root / "reh" / "rehearsal.json"
        rec = json.loads(path.read_text())
        rec["udid"] = "99999999-0000-0000-0000-000000000000"
        path.write_text(json.dumps(rec))
        rc, _, err = self.run_with()
        self.assertIn(f"it was made on simulator 99999999-0000-0000-0000-000000000000, not on {UDID}", err)

    def test_a_step_missing_from_the_rehearsal(self):
        self.rehearsed()
        path = self.root / "reh" / "rehearsal.json"
        rec = json.loads(path.read_text())
        del rec["steps"]["open"]
        path.write_text(json.dumps(rec))
        rc, _, err = self.run_with()
        self.assertIn("it holds no position for the tap with 'find' 'open'", err)

    def test_android_gate_output_is_unchanged_by_the_note(self):
        res = u.gate_sim_run({"steps": [], "launch": {}}, {"steps": []}, dict(u.GATE_DEFAULTS))
        self.assertFalse([n for n in res["notes"] if "rehearsal" in n])


class RehearsalFolderTest(RehearsalBase):
    """A folder `rehearse` wrote into is never taken for a run."""

    def setUp(self):
        super().setUp()
        self.video["open"] = t050.sig_block("open", "T")
        self.video["pull"] = t050.sig_block("pull", "E")

    rehearsed = RunWithRehearsalTest.rehearsed

    def test_a_simulator_or_app_missing_is_a_refusal(self):
        (self.state / "devices.json").write_text(s040.devices(state="Shutdown"))
        rc, _, err = self.rehearse(out="early")
        self.assertEqual(rc, 2)
        self.assertIn("is Shutdown; boot it first", err)
        self.assertFalse((self.root / "early").exists())
        (self.state / "devices.json").write_text(s040.devices())
        (self.state / "installed").unlink()
        rc, _, err = self.rehearse(out="early")
        self.assertEqual(rc, 2)
        self.assertIn("is not installed on simulator", err)
        self.assertFalse((self.root / "early").exists())

    def test_the_marker_comes_first(self):
        # a failure after the start leaves the marker and failed.json
        with mock.patch.object(u, "app_identity", side_effect=u.ProbeError("bundle gone")):
            rc, _, err = self.rehearse(out="early")
        self.assertEqual(rc, 2)
        self.assertEqual(sorted(p.name for p in (self.root / "early").iterdir()), [u.FAILED_RUN, u.REHEARSAL_MARKER])
        marker = json.loads((self.root / "early" / u.REHEARSAL_MARKER).read_text())
        self.assertEqual((marker["tool"]["name"], marker["scenario"]), ("ui-probe", "Sim find"))

    def test_gate_and_summary_pass_over_rehearsals(self):
        self.rehearsed()                                                   # root/reh: completed
        (self.state / "finds.json").write_text(json.dumps([OFF]))
        scn = json.loads(json.dumps(FIND_SCN))
        scn["steps"][0]["find"]["timeout_ms"] = 200
        self.assertEqual(self.rehearse(scn=scn, out="all/reh-failed")[0], 2)        # failed, with raw/ and failed.json
        (self.root / "all" / "reh-killed" / "raw").mkdir(parents=True)              # killed: marker and raw/ only
        (self.root / "all" / "reh-killed" / u.REHEARSAL_MARKER).write_text("{}")
        (self.state / "finds.json").write_text(json.dumps([OFF, ON]))
        rc, _, err = self.run_with(out="all/runs")
        self.assertEqual(rc, 0, err)
        import shutil
        shutil.copytree(self.root / "reh", self.root / "all" / "reh")
        rc, text, _ = main("gate", str(self.root / "all"), "--json", str(self.root / "g.json"))
        self.assertEqual(rc, 0, text)
        self.assertEqual([l.split()[1] for l in text.splitlines() if l.startswith(("PASS", "FAIL"))],
                         [str(self.root / "all" / "runs" / "run-01")])
        notes = [l for l in text.splitlines() if l.startswith("note: a rehearsal")]
        self.assertEqual(len(notes), 2)
        self.assertIn(f"note: a rehearsal that did not complete was passed over (not a run): "
                      f"{self.root / 'all' / 'reh-failed'}: step 'open': the element", notes[0])
        self.assertIn("was not on the screen within 200 ms", notes[0])
        self.assertIn(f"{self.root / 'all' / 'reh-killed'}: it did not finish (no rehearsal.json and no failed.json)",
                      notes[1])
        self.assertEqual(len(json.loads((self.root / "g.json").read_text())["notes"]), 2)
        rows = u.summary([str(self.root / "all")])
        self.assertEqual(([r["run"] for r in rows["runs"]], rows["incomplete"]), (["run-01"], []))
        # only rehearsals: no run, and the error says what was passed over
        rc, _, err = main("gate", str(self.root / "all" / "reh-killed"))
        self.assertEqual(rc, 2)
        self.assertIn("no run found", err)
        self.assertIn("; 1 rehearsal folder passed over", err)

    def test_a_gate_with_no_rehearsal_has_no_notes_key(self):
        self.rehearsed()
        self.assertEqual(self.run_with()[0], 0)
        main("gate", str(self.root / "out"), "--json", str(self.root / "g.json"))
        self.assertNotIn("notes", json.loads((self.root / "g.json").read_text()))

    def test_each_command_refuses_the_other_kind(self):
        self.rehearsed()
        rc, _, err = self.run_with(out="reh")                       # run --out on the rehearsal's own folder
        self.assertEqual(rc, 2)
        self.assertIn(f"{self.root / 'reh'} holds a rehearsal or what is left of one (rehearsal-started.json, "
                      "rehearsal.json); a run and a rehearsal each need a folder of their own", err)
        killed = self.root / "killed" / "run-01"
        killed.mkdir(parents=True)
        (killed / u.REHEARSAL_MARKER).write_text("{}")
        rc, _, err = self.run_with(out="killed")
        self.assertIn("holds a rehearsal or what is left of one (rehearsal-started.json)", err)
        self.assertEqual(self.run_with(out="runs")[0], 0)
        rc, _, err = self.rehearse(out="runs")                     # rehearse --out on a folder of runs
        self.assertEqual(rc, 2)
        self.assertIn(f"{self.root / 'runs'} holds runs (run-01); a rehearsal needs a folder of its own", err)
        rc, _, err = self.rehearse(out="runs/run-01")
        self.assertIn("already holds a run or what is left of one", err)
        rc, _, err = self.rehearse(out="killed/run-01")
        self.assertIn("holds a rehearsal or what is left of one (rehearsal-started.json)", err)


class UnchangedFramesAllowanceTest(unittest.TestCase):
    """allow.unchanged_frames_max: recorded frames that changed nothing pass up to a number, on both
    platforms; without it that check stays at zero."""

    def setUp(self):
        import test_steps_020 as t020
        self.t020 = t020

    def android(self, allow, identical, frames=11):
        t = self.t020
        return u.gate_run(t.make_run(t.base_steps(frames=frames), [t.vstep("idle", identical=identical)],
                                     clock="recorder-timestamps"), t.allow_scn(**allow), t.DEFAULTS)

    def sim(self, allow, identical):
        scn = s040.sim_scn(**allow)
        return u.gate_sim_run(s040.sim_run(frames=identical, identical=identical), scn, dict(u.GATE_DEFAULTS, rest_frames_max=0))

    def test_android(self):
        res = self.android({"frames_max": 12, "unchanged_frames_max": 3}, identical=3)
        self.assertEqual((res["verdict"], res["violations"]), ("PASS", []))
        self.assertIn("idle: 3 recorded frames changed nothing on screen at rest, above the limit of 0 that would otherwise "
                      "apply; passed on the step's allowance of 3 (the caret blinks)", res["notes"])
        res = self.android({"frames_max": 12, "unchanged_frames_max": 3}, identical=4)
        self.assertEqual(self.t020.checks(res), ["rest-video"])
        v = res["violations"][0]
        self.assertEqual((v["observed"], v["allowed"], v["limit_from"]), (4, 3, "allowance"))
        self.assertEqual(v["message"], "4 frames were composed at rest that changed nothing on screen, allowed 3 by the "
                                       "step's allowance (the caret blinks)")
        # absent: zero, and frames_max does not reach it; the message is as before
        res = self.android({"frames_max": 12}, identical=1)
        self.assertEqual(self.t020.checks(res), ["rest-video"])
        self.assertEqual(res["violations"][0]["message"], "1 frames were composed at rest that changed nothing on screen")
        self.assertNotIn("limit_from", res["violations"][0])

    def test_simulator(self):
        allow = {"frames_max": 20, "changed_frames_max": 20, "unchanged_frames_max": 19}
        res = self.sim(allow, 19)
        self.assertEqual(res["verdict"], "PASS", res["violations"])
        self.assertTrue(any(n.startswith("rest-a: 19 recorded frames changed nothing on screen at rest, above the limit "
                                         "of 0 that would otherwise apply; passed on the step's allowance of 19 (the caret "
                                         "blinks)") for n in res["notes"]), res["notes"])
        res = self.sim(allow, 20)
        v = next(v for v in res["violations"] if v["check"] == "rest-video")
        self.assertEqual((v["observed"], v["allowed"], v["limit_from"]), (20, 19, "allowance"))
        self.assertEqual(v["message"], "20 frames were composed at rest that changed nothing on screen, allowed 19 by the "
                                       "step's allowance (the caret blinks)")
        res = self.sim({"frames_max": 20, "changed_frames_max": 20}, 2)
        v = next(v for v in res["violations"] if v["check"] == "rest-video")
        self.assertEqual(v["message"], "2 frames were composed at rest that changed nothing on screen")
        self.assertNotIn("allowed", v)

    def test_validation(self):
        rest = lambda allow, pkg="com.example.app": {"name": "a", "package": pkg, "steps": [
            {"id": "r", "do": "rest", "ms": 5000, "allow": allow}]}
        for pkg, platform in (("com.example.app", u.ANDROID), (BUNDLE, u.SIM_PLATFORM)):
            u.validate_scenario(rest({"unchanged_frames_max": 1, "reason": "caret"}, pkg), platform)
            for bad in (0, -1, 1.5, True, "3", None):
                with self.assertRaises(u.ProbeError) as cm:
                    u.validate_scenario(rest({"unchanged_frames_max": bad, "reason": "caret"}, pkg), platform)
                self.assertIn("'allow.unchanged_frames_max' must be a whole number of at least 1", str(cm.exception))
            with self.assertRaises(u.ProbeError) as cm:
                u.validate_scenario(rest({"unchanged_frames_max": 3}, pkg), platform)
            self.assertIn("'allow.reason' is required", str(cm.exception))

    def test_the_gate_lists_it(self):
        with s040.tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "out" / "run-01"
            run.mkdir(parents=True)
            scn = s040.sim_scn(unchanged_frames_max=19, frames_max=20, changed_frames_max=20)
            (run / "capture.json").write_text(json.dumps({"platform": u.SIM_PLATFORM, "scenario": scn}))
            r = s040.sim_run(frames=15, identical=15)
            r["frame_sources"] = ["recording"]
            (run / "run.json").write_text(json.dumps(r))
            gj = Path(tmp) / "g.json"
            rc, text, err = main("gate", str(Path(tmp) / "out"), "--json", str(gj))
            self.assertEqual(rc, 0, text + err)
            self.assertIn("  allowance: rest-a: frames_max 20, changed_frames_max 20, unchanged_frames_max 19 "
                          "(the caret blinks)", text)
            self.assertEqual(json.loads(gj.read_text())["runs"][0]["allowances"]["rest-a"]["unchanged_frames_max"], 19)


def write_sim_run(run: Path, identical: int):
    """A saved simulator run whose rest fails when identical > 0 (the reviewer's f1 sequence)."""
    run.mkdir(parents=True)
    scn = s040.sim_scn(frames_max=20, changed_frames_max=20)
    (run / "capture.json").write_text(json.dumps({"platform": u.SIM_PLATFORM, "scenario": scn}))
    r = s040.sim_run(frames=max(identical, 1), identical=identical)
    r["frame_sources"] = ["recording"]
    (run / "run.json").write_text(json.dumps(r))


class NestedFoldersTest(unittest.TestCase):
    """A run inside a rehearsal folder must never vanish (review finding 1)."""

    def setUp(self):
        p = mock.patch.object(u, "SIM_REST_MIN_MS", 100)
        p.start()
        self.addCleanup(p.stop)
        self.tmp = s040.tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "results"
        write_sim_run(self.root / "good" / "run-01", identical=0)
        self.reh = self.root / "reh"
        self.reh.mkdir(parents=True)
        (self.reh / u.REHEARSAL_MARKER).write_text("{}")
        (self.reh / u.REHEARSAL_FILE).write_text("{}")
        (self.reh / "raw").mkdir()

    def test_run_refuses_a_folder_inside_a_rehearsal(self):
        for out in (self.reh / "batch", self.reh / "a" / "b"):
            with self.assertRaises(u.ProbeError) as cm:
                u.refuse_rehearsal_folder(out)
            self.assertIn(f"lies inside the rehearsal folder {self.reh.absolute()}", str(cm.exception))
        scn_path = Path(self.tmp.name) / "s.json"
        scn_path.write_text(json.dumps(s040.SCN))
        rc, _, err = main("run", "--simulator", UDID, "--scenario", str(scn_path), "--out", str(self.reh / "batch"))
        self.assertEqual(rc, 2)
        self.assertIn("lies inside the rehearsal folder", err)
        self.assertFalse((self.reh / "batch").exists())
        with self.assertRaises(u.ProbeError):                  # and each run-NN, as run_once_sim checks it
            u.run_once_sim(UDID, s040.SCN, self.reh / "batch" / "run-01")
        self.assertFalse((self.reh / "batch").exists())

    def test_rehearse_refuses_runs_at_any_depth_and_a_folder_inside_a_rehearsal(self):
        older = Path(self.tmp.name) / "older"
        write_sim_run(older / "2026-11-11" / "run-01", identical=7)
        with self.assertRaises(u.ProbeError) as cm:
            u.rehearse(UDID, FIND_SCN, older)
        self.assertIn(f"{older} holds runs (2026-11-11/run-01); a rehearsal needs a folder of its own", str(cm.exception))
        with self.assertRaises(u.ProbeError) as cm:
            u.rehearse(UDID, FIND_SCN, self.reh / "inner")
        self.assertIn("lies inside the rehearsal folder", str(cm.exception))
        self.assertFalse((self.reh / "inner").exists())

    def test_a_run_inside_a_rehearsal_stops_gate_summary_and_compare(self):
        write_sim_run(self.reh / "batch" / "run-01", identical=7)           # made by hand: the commands refuse it
        rc, text, err = main("gate", str(self.root))
        self.assertEqual(rc, 2)
        self.assertIn(f"{self.reh / 'batch' / 'run-01'} holds a run marker (capture.json) inside the rehearsal folder "
                      f"{self.reh}; a rehearsal folder is passed over, so that run would not be read", err)
        for argv in (("summary", str(self.root)), ("compare", "--base", str(self.root), "--head", str(self.root))):
            rc, _, err = main(*argv)
            self.assertEqual(rc, 2, argv)
            self.assertIn("inside the rehearsal folder", err)
        # gated on its own folder it is read, and fails, as it should
        rc, _, _ = main("gate", str(self.reh / "batch"))
        self.assertEqual(rc, 1)

    def test_the_rehearsals_own_raw_and_failed_json_are_passed_over(self):
        (self.reh / "raw" / "agent-derived-data" / "raw").mkdir(parents=True)
        (self.reh / u.FAILED_RUN).write_text(json.dumps({"error": "x"}))
        rc, text, err = main("gate", str(self.root))
        self.assertEqual(rc, 0, text + err)
        self.assertEqual([l.split()[1] for l in text.splitlines() if l.startswith(("PASS", "FAIL"))],
                         [str(self.root / "good" / "run-01")])


class ReplayedTapGateTest(unittest.TestCase):
    """A replayed tap whose recording shows no visual change fails as input (review findings 2 and 3)."""

    def gate(self, signature="-", expect=None, recording=True):
        step = {"id": "open", "do": "tap", "find": {"text": "Sign in"}, **({"expect": expect} if expect else {})}
        scn = {"name": "Replay", "package": BUNDLE, "steps": [step]}
        rec = {"frames": 3, "changed_app": 3, "changed_app_noise": 0, "identical": 0, "bars_only": 0, "signature": signature}
        s = {"id": "open", "kind": "tap", "pid": s040.PID, "process_missing": False, "process_restarted": False,
             "window_s": 3.0, "cpu_s": 0.0, "cpu_pct": 0.0, "recorder_exit": 0, "recorder_killed": False,
             "recorder_alive_at_end": True, "recording": rec if recording is True else recording, "app_state": 4,
             "agent": {"command": {"cmd": "tap"}, "reply": {"ok": True}, "round_trip_ms": 5, "end_ping": {"ok": True}},
             "resolved": {"x": 0.5, "y": 0.5, "swipes": [], "rehearsal": "r.json", "rehearsal_finished": "t"}}
        r = {"platform": u.SIM_PLATFORM, "launch": {"pid": s040.PID, "error": None}, "host": {"load1_before": 1.0},
             "agent": {"stop": {"exit": 0}},
             "steps": [{"id": "launch", "kind": "launch", "pid": s040.PID, "process_missing": False,
                        "process_restarted": False}, s]}
        return u.gate_sim_run(r, scn, dict(u.GATE_DEFAULTS))

    def test_a_tap_that_changed_nothing(self):
        res = self.gate("-")
        self.assertEqual([(v["check"], v["message"]) for v in res["violations"]], [
            ("input", "the replayed tap changed nothing on the screen (signature '-'), so it probably missed its target; "
                      "give the step expect.signature '-' if nothing is meant to change")])
        self.assertEqual(self.gate("-", {"signature": "-"})["verdict"], "PASS")
        self.assertEqual(self.gate("T")["verdict"], "PASS")
        self.assertEqual([v["check"] for v in self.gate("T", {"signature": "E"})["violations"]], ["signature"])

    def test_an_unknown_signature(self):
        res = self.gate(recording={"error": "the recording holds no video frames"})
        self.assertEqual([v["check"] for v in res["violations"]], ["recording", "input"])
        self.assertIn("whether the replayed tap reached its target cannot be told: the recording could not be used",
                      res["violations"][1]["message"])
        res = self.gate(signature=None)
        self.assertEqual(res["verdict"], "PASS")
        self.assertIn("open: the replayed tap's signature is unknown, so whether it changed anything on the screen was "
                      "not checked", res["notes"])

    def test_a_tap_by_position_is_not_held_to_it(self):
        step = {"id": "open", "do": "tap", "x": 0.5, "y": 0.5}
        r = s040.sim_run()
        r["steps"][1].update({"id": "open", "kind": "tap", "app_state": 4, "agent": {"reply": {"ok": True}}})
        r["steps"][1]["recording"]["signature"] = "-"
        r["agent"] = {"stop": {"exit": 0}}
        res = u.gate_sim_run(r, {"name": "x", "package": BUNDLE, "steps": [step]}, dict(u.GATE_DEFAULTS))
        self.assertEqual(res["verdict"], "PASS", res["violations"])


class BundleIdentityTest(unittest.TestCase):
    """The identity covers the whole bundle (review finding 4)."""

    def setUp(self):
        self.tmp = s040.tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = make_app(Path(self.tmp.name))
        fw = self.app / "Frameworks" / "Shared.framework"
        fw.mkdir(parents=True)
        (fw / "Shared").write_bytes(b"kotlin code v1")
        (self.app / "compose-resources").mkdir()
        (self.app / "compose-resources" / "strings.xml").write_text("<string name='cta'>Sign in</string>")

    def test_a_framework_or_a_resource_changes_the_digest(self):
        before = u.app_identity(str(self.app))
        self.assertEqual(len(before["bundle_sha256"]), 64)
        (self.app / "compose-resources" / "strings.xml").write_text("<string name='cta'>Log in</string>")
        after = u.app_identity(str(self.app))
        self.assertEqual([k for k in u.APP_IDENTITY if before[k] != after[k]], ["bundle_sha256"])
        data = {"tool": {"name": "ui-probe"}, "platform": u.SIM_PLATFORM, "scenario_digest": u.scenario_digest(FIND_SCN),
                "udid": UDID, "screen": [393.0, 852.0], "app": before, "finished": u.utc_stamp(time.time() - 5),
                "steps": {"open": {"do": "tap", "center": [0.2, 0.5], "swipes": []}}}
        with self.assertRaises(u.ProbeError) as cm:
            u.check_rehearsal({"path": "r", "data": data}, FIND_SCN, UDID, 3600, after, time.time())
        self.assertIn("the app changed since the rehearsal: its bundle digest (over every file's path and sha256) was "
                      f"{before['bundle_sha256']}, is {after['bundle_sha256']}", str(cm.exception))
        (self.app / "Frameworks" / "Shared.framework" / "Shared").write_bytes(b"kotlin code v2")
        self.assertNotEqual(u.app_identity(str(self.app))["bundle_sha256"], after["bundle_sha256"])

    def test_the_digest_is_paths_and_contents(self):
        a = u.bundle_digest(str(self.app))
        (self.app / "compose-resources" / "strings.xml").rename(self.app / "compose-resources" / "other.xml")
        self.assertNotEqual(u.bundle_digest(str(self.app)), a)          # a renamed file changes it
        import hashlib
        tiny = Path(self.tmp.name) / "Tiny.app"
        (tiny / "d").mkdir(parents=True)
        (tiny / "d" / "f").write_bytes(b"x")
        (tiny / "l").symlink_to("d/f")
        want = hashlib.sha256(("d/f\tsha256:" + hashlib.sha256(b"x").hexdigest() + "\n" + "l\tlink:d/f\n").encode())
        self.assertEqual(u.bundle_digest(str(tiny)), want.hexdigest())

    def test_a_link_is_never_followed(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / "big").write_bytes(b"1")
        (self.app / "Linked").symlink_to(outside, target_is_directory=True)
        a = u.bundle_digest(str(self.app))
        (outside / "big").write_bytes(b"2")
        self.assertEqual(u.bundle_digest(str(self.app)), a)

    def test_an_unreadable_bundle_is_unknown(self):
        f = self.app / "compose-resources" / "strings.xml"
        f.chmod(0)
        self.addCleanup(f.chmod, 0o644)
        if os.access(f, os.R_OK):
            self.skipTest("the file is readable anyway (running as root)")
        ident = u.app_identity(str(self.app))
        self.assertIsNone(ident["bundle_sha256"])
        self.assertTrue(any("the bundle cannot be read in full" in w for w in ident["unknown"]), ident["unknown"])
        data = {"tool": {"name": "ui-probe"}, "platform": u.SIM_PLATFORM, "scenario_digest": u.scenario_digest(FIND_SCN),
                "udid": UDID, "screen": [393.0, 852.0], "app": u.app_identity(str(make_app(Path(self.tmp.name) / "b"))),
                "finished": u.utc_stamp(time.time() - 5),
                "steps": {"open": {"do": "tap", "center": [0.2, 0.5], "swipes": []}}}
        with self.assertRaises(u.ProbeError) as cm:
            u.check_rehearsal({"path": "r", "data": data}, FIND_SCN, UDID, 3600, ident, time.time())
        self.assertIn("bundle digest (over every file's path and sha256) is unknown now", str(cm.exception))


class IdleAndBoundsTest(unittest.TestCase):
    """The agent's idle limit covers every wait between two commands; the waits are bounded (finding 5)."""

    def scn(self, settle, mx=2, step_settle=None):
        st = {"id": "open", "do": "tap", "find": {"text": "Go", "scroll": {"from": [0.5, 0.8], "to": [0.5, 0.3],
                                                                         "max": mx, "settle_ms": settle}}}
        if step_settle is not None:
            st["settle_ms"] = step_settle
        return {"name": "i", "package": BUNDLE, "launch_settle_ms": 6000, "steps": [st]}

    def test_the_reviewers_example_is_refused(self):
        for mode in u.SIM_MODES:
            with self.assertRaises(u.ProbeError) as cm:
                u.validate_scenario(self.scn(200000), u.SIM_PLATFORM, mode)
            self.assertIn("'find.scroll.settle_ms' on the iOS Simulator is at most 60000", str(cm.exception))
            with self.assertRaises(u.ProbeError) as cm:
                u.validate_scenario(self.scn(100, step_settle=60001), u.SIM_PLATFORM, mode)
            self.assertIn("step 'open': 'settle_ms' on the iOS Simulator is at most 60000", str(cm.exception))
            u.validate_scenario(self.scn(60000, step_settle=60000), u.SIM_PLATFORM, mode)

    def test_the_idle_limit_covers_scrolling(self):
        scn = self.scn(60000, mx=2)
        # a run replays 2 swipes of 600 ms, each followed by 60 s
        # (whole seconds, the margin of 120 s on top of the wait)
        self.assertGreaterEqual(u.agent_idle_s(scn), int(2 * (600 + 60000) / 1000 + u.AGENT_IDLE_MARGIN_S))
        # a rehearsal's lookup polls up to its timeout, which holds every swipe and its settle time
        self.assertGreaterEqual(u.rehearsal_idle_s(scn),
                                (u.find_timeout_ms(scn, scn["steps"][0]) + 60600) / 1000 + u.AGENT_IDLE_MARGIN_S - 1)
        self.assertGreater(u.rehearsal_idle_s(scn), u.agent_idle_s(scn))


class DocsTest(unittest.TestCase):
    def test_documented(self):
        root = Path(u.HERE).parent
        readme = " ".join((root / "README.md").read_text().split())
        for phrase in ("## A rehearsal on the iOS Simulator", "`ui-probe rehearse --simulator <udid> --scenario <file> "
                       "--out <dir>`", "--out <dir> --rehearsal <dir>/rehearsal.json`", "--rehearsal-max-age-s",
                       "the backend's content must not change between the two passes",
                       "a step that changes server state must be the last lookup of its scenario",
                       "A rehearsal folder is not a run", "`rehearsal-started.json`",
                       "What verifies a replayed tap in the measured run is an approved `expect.signature`",
                       "a tap that lands on another control and changes the screen is not caught",
                       "a rest waits its full `ms`",
                       "XCTest's own hit test says a tap would reach it (`hittable`)", "**Typed text in kept files.**",
                       "When the scenario types, `lookup-failed.txt` holds no element value at all"):
            self.assertIn(phrase, readme, phrase)
        self.assertIn("## 0.6.0", (root / "CHANGELOG.md").read_text())
        self.assertIn("### Validation, 0.6.0", (root / "PROVENANCE.md").read_text())
        skill = " ".join((root / "skills" / "ui-probe" / "SKILL.md").read_text().split())
        self.assertIn("ui-probe rehearse", skill)
        # 0.7.0: the version moved on; the manifest still names the code's version
        self.assertEqual(json.loads((root / ".claude-plugin" / "plugin.json").read_text())["version"], u.VERSION)


if __name__ == "__main__":
    unittest.main()
