"""0.8.2: on the iOS Simulator a link whose system alert was not confirmed within the wait passes with a note on its
step; a rehearsal cancels a stale alert after the launch (its agent starts after the app), a run before it. The
set-aside rule's own cases are in test_other_layer_081. The links are made up. No device, no simulator."""
import sys

sys.dont_write_bytecode = True

import unittest
from unittest import mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "scripts"))

import test_input_050 as t050  # noqa: E402
import test_rehearsal_060 as t060  # noqa: E402
import test_url_080 as t080  # noqa: E402
import ui_probe as u  # noqa: E402

NOTE = ("invite: no system alert was confirmed within the 5 s wait; if the system asked later, the app has not opened "
        "the link and the alert is still above it")


def recording(events):
    """sim_launch and SimAgent.send, each also writing what it did into events."""
    real_launch, real_send = u.sim_launch, u.SimAgent.send

    def launch(*a, **k):
        events.append("launch")
        return real_launch(*a, **k)

    def send(agent, command, timeout=u.AGENT_COMMAND_TIMEOUT_S):
        events.append(command["cmd"] + ("" if command["cmd"] != "alert" else " " + command["button"]))
        return real_send(agent, command, timeout)

    return mock.patch.object(u, "sim_launch", launch), mock.patch.object(u.SimAgent, "send", send)


class UnconfirmedLinkTest(t050.InputBase):
    def setUp(self):
        super().setUp()
        self.video["invite"] = t050.sig_block("invite", "T")

    def test_a_link_whose_alert_was_not_confirmed_passes_with_a_note(self):
        (self.state / "alert.Open").write_text("no")
        rc, _, err = self.run_cli(scn=t080.SIM_URL_SCN)
        self.assertEqual(rc, 0, err)
        rc, text, _ = self.gate()
        self.assertEqual(rc, 0, text)
        self.assertIn(f"note: {NOTE}", text)
        # no extra call to the agent: the stale alert's Cancel and the link's Open, as before
        self.assertEqual([c for c in self.cmds() if c["cmd"] == "alert"], [t080.CANCEL, t080.OPEN])

    def test_a_confirmed_link_has_no_such_note(self):
        rc, _, err = self.run_cli(scn=t080.SIM_URL_SCN)
        self.assertEqual(rc, 0, err)
        rc, text, _ = self.gate()
        self.assertEqual(rc, 0, text)
        self.assertNotIn("no system alert was confirmed", text)

    def test_a_link_that_did_not_open_fails_without_the_note(self):
        (self.state / "openurl.rc").write_text("1")
        rc, _, err = self.run_cli(scn=t080.SIM_URL_SCN)
        self.assertEqual(rc, 0, err)
        rc, text, _ = self.gate()
        self.assertEqual(rc, 1, text)
        self.assertNotIn("no system alert was confirmed", text)


class StaleAlertOrderTest(t060.RehearsalBase):
    def setUp(self):
        super().setUp()
        self.video["open"] = t050.sig_block("open", "T")
        self.video["pull"] = t050.sig_block("pull", "E")
        self.video["invite"] = t050.sig_block("invite", "T")

    def test_a_rehearsal_cancels_after_the_launch_and_a_run_before_it(self):
        events = []
        a, b = recording(events)
        with a, b:
            rc, _, err = self.rehearse(scn=t080.MIXED_SCN)
        self.assertEqual(rc, 0, err)
        # the rehearsal's agent starts after the app, and only the agent can look for the alert
        self.assertEqual(events[:2], ["launch", "alert Cancel"])
        events.clear()
        a, b = recording(events)
        with a, b:
            rc, _, err = self.run_with(scn=t080.MIXED_SCN)
        self.assertEqual(rc, 0, err)
        self.assertEqual(events[:2], ["alert Cancel", "launch"])


class DocsTest(unittest.TestCase):
    def test_documented(self):
        # the plugin's version is pinned by the newest release's test (test_film_090), the marketplace's never
        root = Path(u.HERE).parent
        readme = " ".join((root / "README.md").read_text().split())
        for phrase in ("the largest of the app-side counts", "passes with a note on its step",
                       "In a rehearsal a stale alert is cancelled after the launch, where a run cancels it before"):
            self.assertIn(phrase, readme, phrase)
        self.assertIn("## 0.8.2", (root / "CHANGELOG.md").read_text())
        self.assertIn("### Validation, 0.8.2", (root / "PROVENANCE.md").read_text())
        self.assertIn("ui-probe 0.8.2", (root.parent.parent / "CHANGELOG.md").read_text())


if __name__ == "__main__":
    unittest.main()
