"""0.8.0: a step that opens a link in the app (`"do": "url"`), on Android and on the iOS Simulator.
Validation, the exact command lines (Android's device script; the simulator through the xcrun stand-in
of 0.4.0), a link nothing opened, the step's place in recording, judging, counts and request windows,
and rehearse and replay. The links are made up. No device, no simulator."""
import sys

sys.dont_write_bytecode = True

import copy
import json
import unittest
from unittest import mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import numpy as np  # noqa: E402

import test_analysis as ta  # noqa: E402
import test_input_050 as t050  # noqa: E402
import test_rehearsal_060 as t060  # noqa: E402
import test_requests_070 as t70  # noqa: E402
import test_simulator_040 as s040  # noqa: E402
import ui_probe as u  # noqa: E402
import ui_video  # noqa: E402

LINK = "example://join/ABC123?from=probe&n=2"
URL_STEP = {"id": "open", "do": "url", "url": LINK}


def android_scn(step=None):
    scn = copy.deepcopy(ta.SCN)                         # idle (rest), open (tap), idle2 (rest)
    scn["steps"][1] = dict(step or URL_STEP)
    return scn


def validate(step, platform=u.ANDROID):
    scn = {"name": "x", "package": ta.PKG if platform == u.ANDROID else s040.BUNDLE, "steps": [step]}
    u.validate_scenario(scn, platform)


class ValidationTest(unittest.TestCase):
    def refused(self, step, fragment, platform=u.ANDROID):
        with self.assertRaises(u.ProbeError, msg=repr(step)) as cm:
            validate(step, platform)
        self.assertIn(fragment, str(cm.exception))

    def test_accepted(self):
        for platform in (u.ANDROID, u.SIM_PLATFORM):
            for url in (LINK, "example://a/b?x=%20&y=a@b,c_d#frag", "x-app+v2.0:payload", "example:z", "https://example.com/i/1",
                        "example://[::1]/a=b", "e" + ":" + "a" * 498):
                validate({"id": "o", "do": "url", "url": url}, platform)
            validate({"id": "o", "do": "url", "url": LINK, "until_quiet": False, "settle_ms": 500,
                      "expect": {"signature": "T", "requests_max": 2}, "note-comment": "a made-up link"}, platform)
        validate({"id": "o", "do": "url", "url": LINK, "until_quiet": True, "quiet": {"ms": 500},
                  "expect": {"frames_max": 9, "sections_max": {"draw": 3}}})

    def test_each_refused_character_is_named(self):
        for ch in " '\"`\\$;|<>(){}*!~^":
            self.refused({"id": "o", "do": "url", "url": f"example://a{ch}b"},
                         f"'url' holds {ch!r} (U+{ord(ch):04X})")
        for ch in ("\x01", "\t", "\n", "\x7f", "é", "\u2028"):
            self.refused({"id": "o", "do": "url", "url": f"example://a{ch}b"}, f"'url' holds {ch!r}")
        # the first refused character is the one named
        self.refused({"id": "o", "do": "url", "url": "example://a$b;c"}, "'url' holds '$'")

    def test_the_form(self):
        for url in ("Example://a", "1x://a", "+x://a", "example", "example:", "://a", ":a", "exa_mple:a"):
            self.refused({"id": "o", "do": "url", "url": url}, "'url' must start with a scheme")
        self.refused({"id": "o", "do": "url", "url": "e:" + "a" * 499}, "'url' is 501 characters; at most 500")
        for bad in (None, "", 7, ["example://a"]):
            step = {"id": "o", "do": "url"} if bad is None else {"id": "o", "do": "url", "url": bad}
            self.refused(step, "'url' must be a non-empty string")

    def test_keys_of_other_kinds_are_refused(self):
        extra = {"x": 0.5, "y": 0.5, "find": {"text": "a"}, "code": 4, "text": "a", "from": [0.1, 0.1],
                 "to": [0.2, 0.2], "ms": 100, "judge": "state", "meminfo": True, "background": True,
                 "allow": {"frames_max": 1, "reason": "r"}, "find_settle_ms": 100}
        for platform in (u.ANDROID, u.SIM_PLATFORM):
            for key, value in extra.items():
                self.refused({"id": "o", "do": "url", "url": LINK, key: value},
                             f"step 'o': keys ['{key}'] do not apply to a 'url' step", platform)

    def test_the_simulator_refuses_what_it_cannot_do(self):
        self.refused({"id": "o", "do": "url", "url": LINK, "until_quiet": True}, "'until_quiet' must be false or absent",
                     u.SIM_PLATFORM)
        self.refused({"id": "o", "do": "url", "url": LINK, "expect": {"frames": 1}},
                     "takes only 'signature', 'signposts_max' and 'requests_max'", u.SIM_PLATFORM)


class AndroidScriptTest(unittest.TestCase):
    def test_the_exact_command_line(self):
        scn = android_scn()
        u.validate_scenario(scn)
        lines = u.build_device_script(scn, 1080, 2400, False, 60).splitlines()
        cmd = ("am start -W -a android.intent.action.VIEW -d 'example://join/ABC123?from=probe&n=2' -p \"$PKG\" "
               "> $D/url-open.txt 2>&1; echo \"exit $?\" >> $D/url-open.txt")
        self.assertIn(cmd, lines)
        n = lines.index(cmd)
        self.assertEqual(lines[n - 1], "mark open.start")
        self.assertEqual(lines[n + 1], "mark open.injected")
        self.assertEqual(lines[n + 2], "sleep 3.0")                         # a link waits as a tap does
        self.assertIn("PKG='com.example.app'", lines)
        self.assertTrue(next(l for l in lines if l.startswith("rm -f")).endswith(" $D/url-*.txt"))

    def test_a_link_with_a_fragment_and_a_query_stays_one_quoted_word(self):
        scn = android_scn({"id": "open", "do": "url", "url": "example://a/b?x=1&y=%2F#top"})
        u.validate_scenario(scn)
        text = u.build_device_script(scn, 1080, 2400, False, 60)
        self.assertIn("-d 'example://a/b?x=1&y=%2F#top' -p \"$PKG\"", text)

    def test_a_script_without_a_link_is_unchanged(self):
        lines = u.build_device_script(ta.SCN, 1080, 2400, False, 60).splitlines()
        self.assertFalse(any("url-" in l for l in lines))

    def test_the_duration_allows_for_am_waiting(self):
        tap = u.scenario_duration_s(ta.SCN, worst_case=False)
        self.assertEqual(u.scenario_duration_s(android_scn(), worst_case=False), tap + 2)


OK_AM = ("Starting: Intent { act=android.intent.action.VIEW dat=example://join/... pkg=com.example.app }\n"
         "Status: ok\nLaunchState: HOT\nActivity: com.example.app/.Main\nTotalTime: 120\nWaitTime: 128\nComplete\nexit 0\n")
FAILED_AM = {
    "Error: Activity not started, unable to resolve Intent { act=android.intent.action.VIEW dat=example://x "
    "flg=0x10000000 pkg=com.example.app }": "Starting: Intent { act=android.intent.action.VIEW }\nError: Activity not "
    "started, unable to resolve Intent { act=android.intent.action.VIEW dat=example://x flg=0x10000000 "
    "pkg=com.example.app }\nexit 0\n",
    "am exited with status 255: Exception occurred / java.lang.SecurityException": "Exception occurred\n"
    "java.lang.SecurityException\nexit 255\n",
    "am named no activity: Starting: Intent { act=android.intent.action.VIEW } / Status: timeout / Complete":
        "Starting: Intent { act=android.intent.action.VIEW }\nStatus: timeout\nComplete\nexit 0\n",
    "am's exit status was not recorded": "Starting: Intent { act=android.intent.action.VIEW }\n",
    "the run holds no record of the link being opened (url-<step>.txt)": None,
}


class AndroidAnalysisTest(ta.Base):
    def capture(self, url_text, name="run-01"):
        files = ta.healthy_files()
        files["url-open.txt"] = url_text
        return ta.Capture(self.root, files, scn=android_scn(), name=name)

    def test_a_link_that_opened(self):
        cap = self.capture(OK_AM)
        run = cap.analyze()
        st = ta.steps_by_id(run)["open"]
        self.assertEqual((st["kind"], st["url_open"]), ("url", {"opened": True, "exit": 0,
                                                                "activity": "com.example.app/.Main", "error": None}))
        self.assertEqual(st["frames"], 12)                                   # counted like a tap's
        self.assertEqual(cap.gate()["verdict"], "PASS")

    def test_a_link_delivered_to_the_running_activity_is_opened(self):
        # an activity already on top (single top or single task) receives the link; am warns, not errs
        text = ("Starting: Intent { act=android.intent.action.VIEW dat=example://join/... pkg=com.example.app }\n"
                "Warning: Activity not started, intent has been delivered to currently running top-most instance.\n"
                "Status: ok\nLaunchState: UNKNOWN (0)\nActivity: com.example.app/.Main\nTotalTime: 0\n"
                "WaitTime: 4\nComplete\nexit 0\n")
        self.assertEqual(u.parse_url_open(text), {"opened": True, "exit": 0, "activity": "com.example.app/.Main",
                                                  "error": None})

    def test_a_link_nothing_opened_fails_with_ams_words(self):
        for n, (why, text) in enumerate(FAILED_AM.items()):
            cap = self.capture(text, name=f"run-{n:02d}")
            cap.analyze()
            v = [(x["check"], x["step"], x["message"]) for x in cap.gate()["violations"]]
            self.assertEqual(v, [("input", "open", f"the link was not opened: {why}")], why)


class JudgedLikeATapTest(unittest.TestCase):
    def test_a_burst_is_classed_as_a_taps_and_not_as_motion(self):
        before = np.zeros((12, 12))
        app = [before, np.full((12, 12), 40.0), np.full((12, 12), 80.0)]
        frames = [{"i": 1, "t": 0.00, "app_changed": True}, {"i": 2, "t": 0.02, "app_changed": True}]
        tap = ui_video.classify_burst("tap", [dict(f) for f in frames], before, app)
        url = ui_video.classify_burst("url", [dict(f) for f in frames], before, app)
        swipe = ui_video.classify_burst("swipe", [dict(f) for f in frames], before, app)
        self.assertEqual(url, tap)
        self.assertEqual(url["class"], "T")
        self.assertEqual(swipe["class"], "M")


class CountsTest(t70.Segment):
    def test_request_windows_and_budget_like_a_tap(self):
        clock = t70.ANDROID_CLOCK
        steps = t70.AndroidWindowTest.steps(self)
        w_tap = u.request_windows(self.raw, [dict(s) for s in steps], u.ANDROID, clock, ta.SCN, ta.MARKS)
        w_url = u.request_windows(self.raw, [dict(s) for s in steps], u.ANDROID, clock, android_scn(), ta.MARKS)
        self.assertEqual(w_url, w_tap)
        scn = copy.deepcopy(t70.ANDROID_SCN)
        scn["steps"][1] = {"id": "open", "do": "url", "url": LINK, "expect": {"requests_max": 0}}
        u.validate_scenario(scn)
        run = t70.android_run(open_count=1)
        run["steps"][2]["kind"] = "url"
        run["steps"][2]["url_open"] = {"opened": True, "exit": 0, "activity": "x/.M", "error": None}
        res = u.gate_run(run, scn, dict(u.GATE_DEFAULTS))
        self.assertEqual([(v["check"], v["step"], v["message"]) for v in res["violations"]],
                         [("requests", "open", "1 request (GET /api/v1/x x1), approved at most 0")])


SIM_URL_SCN = {"name": "Sim link", "package": s040.BUNDLE, "launch_settle_ms": 0, "steps": [
    {"id": "rest-a", "do": "rest", "ms": 100},
    {"id": "invite", "do": "url", "url": LINK, "settle_ms": 50, "expect": {"signature": "T"}},
    {"id": "rest-b", "do": "rest", "ms": 100}]}


OPEN = {"cmd": "alert", "button": "Open", "timeout_s": 5}
CANCEL = {"cmd": "alert", "button": "Cancel", "timeout_s": 1}


class SimRunTest(t050.InputBase):
    def setUp(self):
        super().setUp()
        self.video["invite"] = t050.sig_block("invite", "T")

    def step(self, sid="invite"):
        return json.loads((self.root / "out" / "run-01" / "raw" / f"step-{sid}.json").read_text())

    def test_the_exact_command_lines_and_the_alert(self):
        events = []
        real_launch, real_send = u.sim_launch, u.SimAgent.send

        def launch(*a, **k):
            events.append("launch")
            return real_launch(*a, **k)

        def send(agent, command, timeout=u.AGENT_COMMAND_TIMEOUT_S):
            events.append(command["cmd"] + ("" if command["cmd"] != "alert" else " " + command["button"]))
            return real_send(agent, command, timeout)

        with mock.patch.object(u, "sim_launch", launch), mock.patch.object(u.SimAgent, "send", send):
            rc, out, err = self.run_cli(scn=SIM_URL_SCN)
        self.assertEqual(rc, 0, err)
        self.assertIn(f"simctl openurl {s040.UDID} {LINK}", self.log())
        # a link needs the agent: a stale alert is cancelled once it is up, before the launch; the link's own
        # alert is confirmed right after simctl, inside the step; every step ends with a ping
        self.assertEqual(len(self.lines("agent-pids")), 1)
        self.assertEqual(events, ["alert Cancel", "launch", "ping", "alert Open", "ping", "ping", "quit"])
        self.assertEqual(self.cmds()[0]["cmd"], "ping")                      # the agent's first answer comes first
        self.assertEqual([c for c in self.cmds() if c["cmd"] == "alert"], [CANCEL, OPEN])
        step = self.step()
        self.assertEqual(step["url_open"], {"opened": True, "exit": 0, "error": None, "alert_confirmed": True})
        self.assertEqual((step["command"], step["reply"]["tapped"], step["app_state"]), (OPEN, True, u.SIM_APP_IN_FRONT))
        run = self.root / "out" / "run-01"
        stale = json.loads((run / "raw" / "stale-alert.json").read_text())
        self.assertEqual((stale["command"], stale["cleared"], stale["reply"]["ok"]), (CANCEL, False, True))
        res = json.loads((run / "run.json").read_text())
        st = {s["id"]: s for s in res["steps"]}
        self.assertEqual((st["invite"]["kind"], st["invite"]["signature"]), ("url", "T"))
        self.assertEqual(st["invite"]["agent"]["command"], OPEN)
        self.assertEqual(res["stale_alert"]["cleared"], False)
        self.assertTrue((run / "raw" / "rec-invite.mp4").exists())
        self.assertEqual(json.loads(out.splitlines()[0])["invite"], {"signature": "T", "ok": True, "confirmed": True})
        row = u.sim_run_row("run-01", res)
        self.assertEqual((row["invite.sim_signature"], row["invite.sim_alert_confirmed"]), ("T", True))
        self.assertIsInstance(row["invite.sim_round_trip_ms"], int)
        rc, text, _ = self.gate()
        self.assertEqual(rc, 0, text)
        self.assertNotIn("cancelled before the launch", text)
        # recorded and windowed as a step of its own, between the two rests
        recs = [l for l in self.log() if "recordVideo" in l]
        self.assertEqual([Path(l.split()[-1]).name for l in recs], ["rec-rest-a.mp4", "rec-invite.mp4", "rec-rest-b.mp4"])

    def test_no_alert_within_the_timeout_is_not_a_failure(self):
        (self.state / "alert.Open").write_text("no")
        rc, _, err = self.run_cli(scn=SIM_URL_SCN)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.step()["url_open"]["alert_confirmed"], False)
        rc, text, _ = self.gate()
        self.assertEqual(rc, 0, text)

    def test_an_agent_that_cannot_confirm_is_an_input_failure(self):
        (self.state / "alert.Open").write_text("fail")
        rc, _, err = self.run_cli(scn=SIM_URL_SCN)
        self.assertEqual(rc, 0, err)
        rc, text, _ = self.gate()
        self.assertEqual(rc, 1, text)
        self.assertIn("FAIL [input] invite: the agent could not look for the system's alert to confirm the link: "
                      "XCTest recorded: Failed to tap the alert's button", text)

    def test_a_link_the_simulator_did_not_open(self):
        (self.state / "openurl.rc").write_text("1")
        rc, _, err = self.run_cli(scn=SIM_URL_SCN)
        self.assertEqual(rc, 0, err)
        step = self.step()
        self.assertFalse(step["url_open"]["opened"])
        self.assertNotIn("alert_confirmed", step["url_open"])
        self.assertEqual([c for c in self.cmds() if c["cmd"] == "alert"], [CANCEL])     # nothing to confirm
        rc, text, _ = self.gate()
        self.assertEqual(rc, 1, text)
        self.assertIn("FAIL [input] invite: the link was not opened: An error was encountered processing the command "
                      "(domain=NSOSStatusErrorDomain, code=-10814)", text)

    def test_a_stale_alert_is_cancelled_and_said(self):
        # an earlier run handed a link over and stopped before confirming it: its alert is still up
        (self.state / "alert.Cancel").write_text("yes")
        rc, _, err = self.run_cli(scn=SIM_URL_SCN)
        self.assertEqual(rc, 0, err)
        self.assertTrue(json.loads((self.root / "out" / "run-01" / "run.json").read_text())["stale_alert"]["cleared"])
        rc, text, _ = self.gate()
        self.assertEqual(rc, 0, text)
        self.assertIn("note: a system alert left by an earlier link (opened, never confirmed) was cancelled before the "
                      "launch", text)

    def test_a_stale_alert_the_agent_cannot_look_for_fails_the_launch(self):
        (self.state / "alert.Cancel").write_text("fail")
        rc, _, err = self.run_cli(scn=SIM_URL_SCN)
        self.assertEqual(rc, 0, err)
        rc, text, _ = self.gate()
        self.assertEqual(rc, 1, text)
        self.assertIn("FAIL [input] launch: the agent could not look for a system alert left by an earlier link, which "
                      "would take the run's taps: XCTest recorded: Failed to tap the alert's button", text)

    def test_a_run_without_its_stale_alert_record_fails(self):
        rc, _, err = self.run_cli(scn=SIM_URL_SCN)
        self.assertEqual(rc, 0, err)
        run = self.root / "out" / "run-01"
        (run / "raw" / "stale-alert.json").unlink()
        u.analyze_run(run)
        rc, text, _ = self.gate()
        self.assertEqual(rc, 1, text)
        self.assertIn("FAIL [input] launch: the run holds no record of the check for a system alert", text)

    def test_with_taps_the_link_is_confirmed_and_the_state_read(self):
        scn = {**t050.INPUT_SCN, "steps": t050.INPUT_SCN["steps"] + [
            {"id": "invite", "do": "url", "url": LINK, "settle_ms": 50}]}
        rc, _, err = self.run_cli(scn=scn)
        self.assertEqual(rc, 0, err)
        step = self.step()
        self.assertEqual((step["command"], step["app_state"]), (OPEN, u.SIM_APP_IN_FRONT))
        self.assertTrue(step["url_open"]["opened"])
        self.assertEqual([c for c in self.cmds() if c["cmd"] == "alert"], [CANCEL, OPEN])
        rc, text, _ = self.gate()
        self.assertEqual(rc, 0, text)

    def test_the_app_not_in_front_after_the_link_fails(self):
        # the end-of-step ping now runs after a link: a link handed to another app is seen
        rc, _, err = self.run_cli(scn=SIM_URL_SCN)
        self.assertEqual(rc, 0, err)
        run = self.root / "out" / "run-01"
        data = json.loads((run / "raw" / "step-invite.json").read_text())
        data["app_state"] = 3
        (run / "raw" / "step-invite.json").write_text(json.dumps(data))
        u.analyze_run(run)
        rc, text, _ = self.gate()
        self.assertEqual(rc, 1, text)
        self.assertIn("FAIL [foreground] invite: the app was not in front at the end of 'invite'", text)

    def test_a_scenario_without_a_link_sends_nothing_new(self):
        rc, _, err = self.run_cli(scn=t050.INPUT_SCN)
        self.assertEqual(rc, 0, err)
        self.assertNotIn("alert", [c["cmd"] for c in self.cmds()])
        run = self.root / "out" / "run-01"
        self.assertFalse((run / "raw" / "stale-alert.json").exists())
        self.assertNotIn("stale_alert", json.loads((run / "run.json").read_text()))
        self.assertFalse(u.sim_needs_agent({"steps": [{"do": "rest"}]}))
        self.assertTrue(u.sim_needs_agent({"steps": [{"do": "rest"}, {"do": "url"}]}))


MIXED_SCN = {**t060.FIND_SCN, "name": "Sim find and link", "steps": [
    {"id": "invite", "do": "url", "url": LINK, "settle_ms": 20}] + t060.FIND_SCN["steps"]}


class RehearseAndReplayTest(t060.RehearsalBase):
    def setUp(self):
        super().setUp()
        self.video["open"] = t050.sig_block("open", "T")
        self.video["pull"] = t050.sig_block("pull", "E")
        self.video["invite"] = t050.sig_block("invite", "T")

    def test_a_rehearsal_opens_and_confirms_the_link_and_a_run_does_it_again(self):
        rc, out, err = self.rehearse(scn=MIXED_SCN)
        self.assertEqual(rc, 0, err)
        rec = json.loads((self.root / "reh" / "rehearsal.json").read_text())
        self.assertEqual(rec["steps"]["invite"]["do"], "url")
        self.assertEqual((rec["steps"]["invite"]["opened"], rec["steps"]["invite"]["alert_confirmed"]), (True, True))
        self.assertEqual(rec["stale_alert"]["cleared"], False)
        opened = [l for l in self.log() if l.startswith("simctl openurl")]
        self.assertEqual(opened, [f"simctl openurl {s040.UDID} {LINK}"])
        cmds = [c["cmd"] + ("" if c["cmd"] != "alert" else " " + c["button"]) for c in self.cmds()]
        # the stale alert after the agent's first answer, before any step; the link's alert before the lookup
        self.assertEqual(cmds[:2], ["ping", "alert Cancel"])
        self.assertLess(cmds.index("alert Open"), cmds.index("find"))
        self.assertNotIn("stale_alert", out)
        (self.state / "agent-cmds").unlink()
        rc, _, err = self.run_with(scn=MIXED_SCN)
        self.assertEqual(rc, 0, err)
        self.assertEqual([l for l in self.log() if l.startswith("simctl openurl")], opened * 2)
        self.assertNotIn("find", [c["cmd"] for c in self.cmds()])
        self.assertEqual([c for c in self.cmds() if c["cmd"] == "alert"], [CANCEL, OPEN])
        rc, text, _ = self.gate()
        self.assertEqual(rc, 0, text)

    def test_a_rehearsal_cancels_a_stale_alert_and_says_so(self):
        (self.state / "alert.Cancel").write_text("yes")
        rc, out, err = self.rehearse(scn=MIXED_SCN)
        self.assertEqual(rc, 0, err)
        self.assertIn("cancelled a system alert left by an earlier link, before the first step", out)
        self.assertTrue(json.loads((self.root / "reh" / "rehearsal.json").read_text())["stale_alert"]["cleared"])

    def test_a_rehearsal_whose_agent_cannot_look_for_alerts_stops(self):
        for button, message in (("Cancel", "the agent could not look for a system alert left by an earlier link"),
                                ("Open", "step 'invite': the agent could not look for the system's alert to confirm "
                                         "the link")):
            for b in ("Cancel", "Open"):
                (self.state / f"alert.{b}").write_text("fail" if b == button else ("yes" if b == "Open" else "no"))
            rc, _, err = self.rehearse(scn=MIXED_SCN, out=f"reh-{button}")
            self.assertEqual(rc, 2, button)
            self.assertIn(message, err)
            self.assertFalse((self.root / f"reh-{button}" / "rehearsal.json").exists())

    def test_no_alert_in_a_rehearsal_is_recorded(self):
        (self.state / "alert.Open").write_text("no")
        rc, _, err = self.rehearse(scn=MIXED_SCN)
        self.assertEqual(rc, 0, err)
        self.assertFalse(json.loads((self.root / "reh" / "rehearsal.json").read_text())["steps"]["invite"]["alert_confirmed"])

    def test_a_link_the_rehearsal_could_not_open_stops_it(self):
        (self.state / "openurl.rc").write_text("1")
        rc, _, err = self.rehearse(scn=MIXED_SCN)
        self.assertEqual(rc, 2)
        self.assertIn("step 'invite': the simulator did not open the link: An error was encountered", err)
        self.assertFalse((self.root / "reh" / "rehearsal.json").exists())

    def test_a_rehearsal_without_a_link_sends_nothing_new(self):
        rc, _, err = self.rehearse()
        self.assertEqual(rc, 0, err)
        self.assertNotIn("alert", [c["cmd"] for c in self.cmds()])
        self.assertNotIn("stale_alert", json.loads((self.root / "reh" / "rehearsal.json").read_text()))

    def test_a_scenario_with_only_links_needs_no_rehearsal(self):
        rc, _, err = self.run_cli("--rehearsal", str(self.root / "nothing.json"), scn=SIM_URL_SCN)
        self.assertEqual(rc, 2)
        self.assertIn("--rehearsal applies only to a scenario with a tap by 'find'", err)


# --------------------------------------------------------------------------------------------
# B: a start by link on Android (launch_url)
# --------------------------------------------------------------------------------------------

AM_COLD_BY_LINK = ("Starting: Intent { act=android.intent.action.VIEW dat=example://join/... pkg=com.example.app }\n"
                   "Status: ok\nLaunchState: COLD\nActivity: com.example.app/.Main\nTotalTime: 812\nWaitTime: 820\n"
                   "Complete\n")
# what am printed on the lab's API 34 emulator for a link nothing handles (2026-11-11), and it exited 0
AM_UNRESOLVED = ("Starting: Intent { act=android.intent.action.VIEW dat=example://nothing/... pkg=com.example.app }\n"
                 "Error: Activity not started, unable to resolve Intent { act=android.intent.action.VIEW "
                 "dat=example://nothing/... flg=0x10000000 pkg=com.example.app }\n")


def by_link(scn=None, url=LINK):
    out = copy.deepcopy(scn or ta.SCN)
    out["launch_url"] = url
    return out


class LaunchUrlTest(unittest.TestCase):
    def test_validation(self):
        u.validate_scenario(by_link())
        for url, fragment in (("example://a b", "'launch_url' holds ' ' (U+0020)"), ("", "'launch_url' must be a non-empty string"),
                              ("Ex://a", "'launch_url' must start with a scheme"), ("e:" + "a" * 499, "'launch_url' is 501 characters"),
                              (7, "'launch_url' must be a non-empty string")):
            with self.assertRaises(u.ProbeError, msg=url) as cm:
                u.validate_scenario(by_link(url=url))
            self.assertIn(fragment, str(cm.exception))
        with self.assertRaises(u.ProbeError) as cm:
            u.validate_scenario({**by_link(), "activity": ".Main"})
        self.assertIn("'activity' and 'launch_url' cannot both be given", str(cm.exception))

    def test_the_simulator_refuses_it_with_its_reason(self):
        scn = {"name": "x", "package": s040.BUNDLE, "launch_url": LINK, "steps": [{"id": "r", "do": "rest", "ms": 1000}]}
        with self.assertRaises(u.ProbeError) as cm:
            u.validate_scenario(scn, u.SIM_PLATFORM)
        self.assertIn("'launch_url': an app started by a link on the iOS Simulator cannot be given its launch environment; "
                      "launch it and open the link in a 'url' step right after the launch", str(cm.exception))

    def test_the_exact_launch_line(self):
        lines = u.build_device_script(by_link(), 1080, 2400, False, 60).splitlines()
        line = ("am start -W -a android.intent.action.VIEW -d 'example://join/ABC123?from=probe&n=2' -p \"$PKG\" "
                "> $D/am-start.txt 2>&1")
        self.assertIn(line, lines)
        n = lines.index(line)
        self.assertEqual((lines[n - 1], lines[n + 1]), ("mark launch.start", "mark launch.end"))
        self.assertFalse(any(l.startswith("monkey ") for l in lines))
        # nothing else in the script differs from a launch by the launcher
        plain = u.build_device_script(ta.SCN, 1080, 2400, False, 60).splitlines()
        self.assertEqual([l for l in lines if l != line], [l for l in plain if not l.startswith("monkey ")])

    def test_the_request_windows_are_those_of_a_launch(self):
        raw = Path(self.id())
        steps = [{"id": "launch", "start_s": 101.5, "end_s": 110.0}, {"id": "idle", "start_s": 110.0, "end_s": 112.0},
                 {"id": "open", "start_s": 112.5, "end_s": 115.0}, {"id": "idle2", "start_s": 115.6, "end_s": 117.6}]
        self.assertEqual(u.request_windows(raw, [dict(s) for s in steps], u.ANDROID, t70.ANDROID_CLOCK, by_link(), ta.MARKS),
                         u.request_windows(raw, [dict(s) for s in steps], u.ANDROID, t70.ANDROID_CLOCK, ta.SCN, ta.MARKS))
        self.assertEqual(u.launch_budget({**by_link(), "launch_requests_max": 2}),
                         {"id": "launch", "expect": {"requests_max": 2}})


class LaunchUrlAnalysisTest(ta.Base):
    def test_a_cold_start_by_link_is_a_launch(self):
        files = ta.healthy_files()
        files["am-start.txt"] = AM_COLD_BY_LINK
        cap = ta.Capture(self.root, files, scn=by_link(), name="by-link")
        run = cap.analyze()
        self.assertEqual(run["launch"], {"state": "COLD", "status": "ok", "total_time_ms": 812, "error": None})
        plain = ta.Capture(self.root, ta.healthy_files(), name="by-launcher").analyze()
        # every step, the launch's first frames included, reads as for a start by the launcher
        self.assertEqual(run["steps"], plain["steps"])
        self.assertEqual(cap.gate()["verdict"], "PASS")

    def test_a_link_nothing_handles_fails_the_launch(self):
        files = ta.healthy_files()
        files["am-start.txt"] = AM_UNRESOLVED
        cap = ta.Capture(self.root, files, scn=by_link())
        run = cap.analyze()
        self.assertTrue(run["launch"]["error"].startswith("Error: Activity not started, unable to resolve Intent"))
        self.assertIn(("launch", "launch", "the app did not start: " + run["launch"]["error"]),
                      [(v["check"], v["step"], v["message"]) for v in cap.gate()["violations"]])

    def test_a_url_step_on_exactly_that_output_fails(self):
        # the same output from a 'url' step, with the exit status 0 am gave
        self.assertEqual(u.parse_url_open(AM_UNRESOLVED + "exit 0\n")["error"],
                         AM_UNRESOLVED.splitlines()[1])


class DocsTest(unittest.TestCase):
    def test_documented(self):
        root = Path(u.HERE).parent
        readme = " ".join((root / "README.md").read_text().split())
        for phrase in ('{"id": "open-invite", "do": "url", "url": "example://join/ABC123?from=probe"}',
                       "`am start -W -a android.intent.action.VIEW -d '<url>' -p \"$PKG\"`",
                       "`xcrun simctl openurl <udid> <url>`", "`raw/url-<step>.txt`", "or a `url` with its link"):
            self.assertIn(phrase, readme, phrase)
        self.assertIn("`url`", (root / "skills" / "ui-probe" / "SKILL.md").read_text())
        self.assertIn("## 0.8.0", (root / "CHANGELOG.md").read_text())
        self.assertIn("### Validation, 0.8.0", (root / "PROVENANCE.md").read_text())
        self.assertEqual(u.VERSION, "0.8.0")
        self.assertEqual(json.loads((root / ".claude-plugin" / "plugin.json").read_text())["version"], "0.8.0")
        # The marketplace version moves with every plugin's release, not only this one's, so
        # an exact pin here fails on another plugin's bump (task-board 0.1.3 took 0.13.1).
        # What this release owes is its own marketplace entry and a version no older than it.
        market = json.loads((root.parent.parent / ".claude-plugin" / "marketplace.json").read_text())
        self.assertGreaterEqual(tuple(int(part) for part in market["metadata"]["version"].split(".")), (0, 13, 0))
        self.assertIn("## 0.13.0", (root.parent.parent / "CHANGELOG.md").read_text())


if __name__ == "__main__":
    unittest.main()
