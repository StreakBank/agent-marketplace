"""Checks that a pass means the app was really observed: it was in front, it drew while launching,
the recorder ran to the end, and the trace lost nothing.

Each of these was a way to report "nothing happened" when the truth was "nothing was seen".
Captures are synthetic: raw files in the shapes the device script leaves, analysed with no device."""
import sys

sys.dont_write_bytecode = True

import copy
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import pftrace  # noqa: E402
import ui_probe as u  # noqa: E402
import ui_video  # noqa: E402
from test_analysis import (GFX, ORDER, PKG, SCN, Base, Capture, checks, fake_trace, fg_text, frame,  # noqa: E402
                           gfx_text, healthy_files, sf_text, steps_by_id, SF_TOTAL)

OTHER = "com.example.launcher"


def messages(res, check):
    return [v["message"] for v in res["violations"] if v["check"] == check]


class ForegroundTest(Base):
    """A rest window says something about the app's screen only while the app is in front."""

    def test_parse(self):
        line = "    topResumedActivity=ActivityRecord{c0ffee1 u0 com.example.app/.Main t12}\n"
        self.assertEqual(u.parse_foreground(line), "com.example.app/.Main")
        older = "  mResumedActivity: ActivityRecord{1f2e3d u0 com.example.app/com.example.app.Main t7}\n"
        self.assertEqual(u.parse_foreground(older), "com.example.app/com.example.app.Main")
        closed = "  ResumedActivity: ActivityRecord{1f2e3d u10 com.example.app/.Outer$Inner}\n"
        self.assertEqual(u.parse_foreground(closed), "com.example.app/.Outer$Inner")
        for nothing in ("  topResumedActivity=null\n", "", None):
            self.assertIsNone(u.parse_foreground(nothing))

    def test_the_app_in_front_is_recorded(self):
        st = steps_by_id(Capture(self.root).analyze())
        self.assertEqual((st["idle"]["foreground"], st["idle"]["foreground_component"]), (True, f"{PKG}/.Main"))

    def test_a_package_that_only_starts_with_the_same_name_is_another_app(self):
        files = healthy_files()
        files["fg-idle.txt"] = fg_text(PKG + ".debug")
        self.assertIs(steps_by_id(Capture(self.root, files).analyze())["idle"]["foreground"], False)

    def test_a_rest_with_another_app_in_front_fails(self):
        files = healthy_files()
        files["fg-idle2.txt"] = fg_text(OTHER)
        cap = Capture(self.root, files)
        cap.analyze()
        res = cap.gate()
        self.assertEqual(res["verdict"], "FAIL")
        self.assertEqual(messages(res, "foreground"),
                         [f"another app was in front at the end of 'idle2' ({OTHER}/.Main); the app's screen was not being observed"])

    def test_another_app_in_front_after_launch_fails(self):
        files = healthy_files()
        files["fg-launch.txt"] = fg_text("com.example.permissions")
        cap = Capture(self.root, files)
        cap.analyze()
        res = cap.gate()
        self.assertEqual([v["step"] for v in res["violations"] if v["check"] == "foreground"], ["launch"])

    def test_a_step_that_is_not_a_rest_may_end_outside_the_app(self):
        files = healthy_files()
        files["fg-open.txt"] = fg_text(OTHER)
        cap = Capture(self.root, files)
        cap.analyze()
        self.assertEqual(cap.gate()["verdict"], "PASS")

    def scenario_with_background_rest(self):
        scn = copy.deepcopy(SCN)
        scn["steps"][2]["background"] = True
        return scn

    def test_a_background_rest_expects_another_app_in_front(self):
        scn = self.scenario_with_background_rest()
        files = healthy_files()
        files["fg-idle2.txt"] = fg_text(OTHER)
        cap = Capture(self.root, files, scn=scn)
        cap.analyze()
        self.assertEqual(cap.gate()["verdict"], "PASS")

    def test_a_background_rest_with_the_app_in_front_fails(self):
        cap = Capture(self.root, scn=self.scenario_with_background_rest())
        cap.analyze()
        res = cap.gate()
        self.assertEqual(messages(res, "foreground"),
                         ["the app was in front at the end of 'idle2', but the step says it rests in the background"])

    def recorded(self, files, scn=SCN, busy="idle2"):
        """A capture with a recording in which the step `busy` shows a changing screen."""
        files = dict(files, **{"sr-exit.txt": "0\n", "sr-alive.txt": "alive\n", "screen.mp4": "x"})
        block = {"frames": 60, "clock": "recorder-timestamps", "steps": [
            {"id": sid, "identical_frames": 3 if sid == busy else 0, "app_region_frames": 40 if sid == busy else 0,
             "app_changed_frames_real": 40 if sid == busy else 0, "signature": "-"} for sid in ORDER]}
        cap = Capture(self.root, files, scn=scn, video=True)
        with mock.patch.object(ui_video, "analyze", return_value=block):
            cap.analyze()
        return cap.gate()

    def test_with_the_app_in_front_a_changing_screen_at_rest_fails(self):
        res = self.recorded(healthy_files())
        self.assertEqual(checks(res), ["rest-video", "rest-video"])

    def test_a_background_rest_is_not_judged_by_what_the_other_app_draws(self):
        """Seen on a device: behind the launcher the recording showed 49 changing frames, all the
        launcher's, while every count of the app's own frames read 0."""
        files = healthy_files()
        files["fg-idle2.txt"] = fg_text(OTHER)
        res = self.recorded(files, scn=self.scenario_with_background_rest())
        self.assertEqual(res["verdict"], "PASS", res)
        self.assertEqual(res["notes"], ["idle2: another app was in front, so the recording shows that app; it was not used for this step"])

    def test_a_rest_behind_another_app_fails_on_the_foreground_alone(self):
        files = healthy_files()
        files["fg-idle2.txt"] = fg_text(OTHER)
        res = self.recorded(files)
        self.assertEqual(checks(res), ["foreground"])

    def test_no_count_and_a_recording_of_another_app_cannot_observe(self):
        files = healthy_files()
        files["fg-idle2.txt"] = fg_text(OTHER)
        files["gfx-idle2.txt"] = gfx_text(None)
        files["sf-idle2.txt"] = "unsupported\n"
        res = self.recorded(files, scn=self.scenario_with_background_rest())
        self.assertEqual(messages(res, "rest-frames"),
                         ["cannot count frames: no frame stats from the platform or the compositor, no trace "
                          "and a recording that shows another app"])

    def test_unknown_is_one_note_not_a_pass_in_silence(self):
        files = {k: v for k, v in healthy_files().items() if not k.startswith("fg-")}
        files["fg-open.txt"] = "  topResumedActivity=null\n"
        cap = Capture(self.root, files)
        run = cap.analyze()
        self.assertIsNone(steps_by_id(run)["idle"]["foreground"])
        res = cap.gate()
        self.assertEqual(res["verdict"], "PASS")
        self.assertEqual(len(res["notes"]), 1)
        self.assertTrue(res["notes"][0].startswith("which app was in front was not recorded at the end of: launch, idle, idle2"),
                        res["notes"])

    def test_background_must_be_true_or_false(self):
        scn = copy.deepcopy(SCN)
        scn["steps"][0]["background"] = "yes"
        with self.assertRaises(u.ProbeError) as ctx:
            u.validate_scenario(scn)
        self.assertIn("'background' must be true or false", str(ctx.exception))
        scn["steps"][0].pop("background")
        scn["steps"][1]["background"] = True          # a tap cannot rest in the background
        with self.assertRaises(u.ProbeError):
            u.validate_scenario(scn)

    def test_the_script_saves_the_resumed_activity_inside_the_snapshot(self):
        lines = u.build_device_script(SCN, 1080, 2400, False, 60).splitlines()
        snap = lines[lines.index("snap() {"):]
        snap = snap[:snap.index("}")]
        saved = [n for n, line in enumerate(snap) if "dumpsys activity activities" in line and "> $D/fg-$1.txt" in line]
        self.assertEqual(len(saved), 1)
        # between the two CPU readings, so the dump is charged to no step
        self.assertLess(snap.index('  cpu $1 "$P"'), saved[0])
        self.assertLess(saved[0], snap.index('  cpu $1.after "$P"'))
        self.assertIn("$D/fg-*.txt", lines[4])       # cleared before a run


class LaunchTest(Base):
    def test_no_frame_while_launching_fails(self):
        files = healthy_files()
        for sid in ORDER:
            files[f"gfx-{sid}.txt"] = gfx_text(0)
            files[f"sf-{sid}.txt"] = sf_text(SF_TOTAL[sid], None)
        cap = Capture(self.root, files)
        run = cap.analyze()
        self.assertEqual(u.frame_count(steps_by_id(run)["launch"]), (0, "platform"))
        res = cap.gate()
        self.assertEqual(messages(res, "launch"),
                         ["no frame of the app was counted while it launched (platform count): it drew nothing in the "
                          "launch window, or this count cannot see how it draws"])

    def test_a_live_process_without_a_window_fails(self):
        """Seen on a device: the launch failed, yet the app's process was running in the background.
        The process check passes; the compositor holds no layer of the app."""
        files = healthy_files()
        for sid in ORDER:
            files[f"gfx-{sid}.txt"] = gfx_text(None)
            files[f"sf-{sid}.txt"] = sf_text(SF_TOTAL[sid], None)
        cap = Capture(self.root, files)
        run = cap.analyze()
        st = steps_by_id(run)
        self.assertEqual((st["launch"]["process_missing"], u.frame_count(st["launch"])), (False, (None, None)))
        res = cap.gate()
        self.assertEqual(messages(res, "launch"),
                         ["the compositor saw no layer of the app while it launched: nothing of the app was on screen"])

    def test_no_count_at_all_is_left_to_the_rest_checks(self):
        """Without frame stats and without compositor statistics nothing can be said at launch."""
        files = healthy_files()
        for sid in ORDER:
            files[f"gfx-{sid}.txt"] = gfx_text(None)
            files[f"sf-{sid}.txt"] = "unsupported\n"
        cap = Capture(self.root, files)
        cap.analyze()
        res = cap.gate()
        self.assertEqual(messages(res, "launch"), [])
        self.assertEqual(len(messages(res, "rest-frames")), 2)

    def test_a_launch_that_drew_passes(self):
        cap = Capture(self.root)
        self.assertEqual(steps_by_id(cap.analyze())["launch"]["frames"], GFX["launch"])
        self.assertEqual(cap.gate()["verdict"], "PASS")

    def test_a_launch_that_timed_out_fails(self):
        files = healthy_files()
        files["am-start.txt"] = "Starting: Intent { cmp=com.example.app/.Main }\nStatus: timeout\nLaunchState: UNKNOWN (0)\nComplete\n"
        cap = Capture(self.root, files)
        run = cap.analyze()
        self.assertEqual(run["launch"]["status"], "timeout")
        self.assertEqual(messages(cap.gate(), "launch"), ["the launch did not complete (status: timeout)"])

    def test_launch_errors(self):
        self.assertEqual(u.parse_launch_error("  bash arg: -p\n** No activities found to run, monkey aborted.\n"),
                         "** No activities found to run, monkey aborted.")
        denied = "java.lang.SecurityException: Permission Denial: starting Intent { cmp=com.example.app/.Hidden } not exported"
        self.assertEqual(u.parse_launch_error("Starting: Intent\n" + denied + "\n"), denied)
        self.assertIsNone(u.parse_launch_error("Events injected: 1\n## Network stats: elapsed time=12ms\n"))


class RecorderStoppedEarlyTest(Base):
    """A recorder that stops by itself exits with status 0; whether it was still running when the
    scenario ended is recorded separately."""

    BLOCK = {"frames": 3, "clock": "recorder-timestamps", "steps": [
        {"id": sid, "identical_frames": 0, "app_region_frames": 0, "app_changed_frames_real": 0, "signature": "-"}
        for sid in ORDER]}

    def run_with(self, alive, name="run-01"):
        files = healthy_files()
        files["sr-exit.txt"] = "0\n"
        files["sr-alive.txt"] = alive
        files["screen.mp4"] = "x"
        cap = Capture(self.root, files, video=True, name=name)
        with mock.patch.object(ui_video, "analyze", return_value=self.BLOCK):
            run = cap.analyze()
        return run, cap.gate()

    def test_running_at_the_end_passes(self):
        run, res = self.run_with("alive\n")
        self.assertIs(run["video_recorder_alive_at_end"], True)
        self.assertEqual(res["verdict"], "PASS", res)

    def test_stopped_before_the_end_fails(self):
        run, res = self.run_with("gone\n")
        self.assertIs(run["video_recorder_alive_at_end"], False)
        self.assertEqual(messages(res, "recording"),
                         ["the recorder had stopped before the scenario ended; later steps were not recorded"])

    def test_not_recorded_fails(self):
        run, res = self.run_with(None)
        self.assertIsNone(run["video_recorder_alive_at_end"])
        self.assertEqual(messages(res, "recording"),
                         ["whether the recorder was still running when the scenario ended was not recorded"])

    def test_the_script_checks_before_it_stops_the_recorder(self):
        lines = u.build_device_script(SCN, 1080, 2400, True, 60).splitlines()
        alive = [n for n, line in enumerate(lines) if "> $D/sr-alive.txt" in line]
        self.assertEqual(len(alive), 1)
        self.assertLess(lines.index("mark END"), alive[0])
        self.assertLess(alive[0], lines.index("kill -2 $SR 2>/dev/null"))
        without = u.build_device_script(SCN, 1080, 2400, False, 60).splitlines()
        self.assertEqual([line for line in without if "sr-alive" in line and not line.startswith("rm -f")], [])


class TraceLossTest(Base):
    """The trace's own statistics say whether it lost data. Lost data makes its counts unknown."""

    def scenario(self):
        scn = copy.deepcopy(SCN)
        scn["count_sections"] = ["Custom#section"]
        scn["steps"][2]["expect"] = {"sections_max": {"Custom#section": 0}}
        return scn

    def analyse(self, trace, begins=(), lost_bundles=0, scn=None):
        cap = Capture(self.root, scn=scn or SCN, trace=True, sections=True)
        (cap.dir / "trace.pftrace").write_bytes(b"")

        def atrace(path, info=None):
            if info is not None:
                info.update(bundles=40, lost_bundles=lost_bundles)
            return list(begins)

        with mock.patch.object(pftrace, "read_trace", return_value=trace), \
                mock.patch.object(pftrace, "read_atrace", side_effect=atrace):
            run = cap.analyze()
        return cap, run

    BEGINS = [{"ts": int(113e9), "tid": 4321, "tgid": 4321, "name": "Custom#section", "dur": 5}]

    def test_a_complete_trace_counts(self):
        cap, run = self.analyse(fake_trace(int(99e9), int(119e9), [frame(113.0)]), self.BEGINS, scn=self.scenario())
        st = steps_by_id(run)
        self.assertEqual(run["trace_loss"], [])
        self.assertIsNone(run["sections_loss"])
        self.assertEqual((st["open"]["frames_compositor"], st["open"]["sections"]["Custom#section"]), (1, 1))
        self.assertEqual(st["idle2"]["sections"]["Custom#section"], 0)
        self.assertEqual(cap.gate()["verdict"], "PASS")

    def test_an_overwritten_buffer_makes_every_trace_count_unknown(self):
        trace = fake_trace(int(99e9), int(119e9), [frame(113.0)], chunks_overwritten=7)
        cap, run = self.analyse(trace, self.BEGINS, scn=self.scenario())
        st = steps_by_id(run)
        self.assertEqual(run["trace_loss"], ["the trace buffer overwrote 7 chunks before they were read"])
        for sid in ORDER:
            self.assertIs(st[sid]["trace_covered"], False, sid)
            self.assertIsNone(st[sid]["frames_compositor"], sid)
            self.assertIsNone(st[sid]["sections_all"], sid)
        # the other counts still decide the rest windows
        self.assertEqual(u.frame_count(st["idle"]), (0, "platform"))
        res = cap.gate()
        self.assertEqual(messages(res, "sections"), ["'Custom#section' was not counted: the trace lost data"])
        self.assertEqual([n for n in res["notes"] if "trace" in n],
                         ["the trace lost data (the trace buffer overwrote 7 chunks before they were read); its counts are unavailable"])

    def test_each_kind_of_loss_is_named(self):
        cases = {
            "chunks_discarded": "the trace buffer discarded 3 chunks",
            "patches_failed": "the tracing service could not complete 3 chunks a writer had left open",
            "writer_packet_loss": "a trace writer reported packet loss 3 times",
            "flushes_failed": "a flush of the trace failed, so its last data may be missing",
        }
        for n, (key, text) in enumerate(cases.items()):
            cap = Capture(self.root, trace=True, name=f"run-{n}")
            (cap.dir / "trace.pftrace").write_bytes(b"")
            with mock.patch.object(pftrace, "read_trace", return_value=fake_trace(int(99e9), int(119e9), **{key: 3})):
                self.assertEqual(cap.analyze()["trace_loss"], [text], key)

    def test_a_failed_final_flush_is_a_loss_and_an_unreported_one_is_not(self):
        for n, (outcome, lost) in enumerate(((2, True), (1, False), (0, False), (None, False))):
            cap = Capture(self.root, trace=True, name=f"run-{n}")
            (cap.dir / "trace.pftrace").write_bytes(b"")
            with mock.patch.object(pftrace, "read_trace", return_value=fake_trace(int(99e9), int(119e9), final_flush=outcome)):
                self.assertEqual(bool(cap.analyze()["trace_loss"]), lost, outcome)

    def test_a_trace_without_statistics_cannot_rule_loss_out(self):
        cap = Capture(self.root, trace=True)
        (cap.dir / "trace.pftrace").write_bytes(b"")
        with mock.patch.object(pftrace, "read_trace", return_value=fake_trace(int(99e9), int(119e9), [frame(113.0)], packets=0)):
            run = cap.analyze()
        self.assertEqual(run["trace_loss"], ["the trace holds no buffer statistics, so data loss cannot be ruled out"])
        self.assertIsNone(steps_by_id(run)["open"]["frames_compositor"])

    def test_events_the_kernel_dropped_make_sections_unknown_but_not_frames(self):
        trace = fake_trace(int(99e9), int(119e9), [frame(113.0)])
        cap, run = self.analyse(trace, self.BEGINS, lost_bundles=2, scn=self.scenario())
        st = steps_by_id(run)
        self.assertEqual(run["sections_loss"], "the kernel dropped trace events (2 of 40 event bundles report a loss before them)")
        self.assertEqual(st["open"]["frames_compositor"], 1)
        self.assertIsNone(st["open"]["sections"]["Custom#section"])
        self.assertIsNone(st["idle2"]["sections_all"])
        res = cap.gate()
        self.assertEqual(len(messages(res, "sections")), 1)
        self.assertIn("the kernel dropped trace events", messages(res, "sections")[0])
        self.assertIn("section counts are unavailable", " ".join(res["notes"]))


if __name__ == "__main__":
    unittest.main()
