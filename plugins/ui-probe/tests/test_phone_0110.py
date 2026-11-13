"""0.11.0: `ui-probe phone`, the recorder of a cabled iPhone's screen.

The compiled recorder is replaced by a stand-in (a short Python script) in every behavioural test,
and `osascript` by a stand-in on PATH: nothing here opens a camera device or posts a notification.
One test compiles the Swift source where swiftc exists, and does not run the result. The static
tests read the Swift source, this plugin's phone module and its command line."""
import sys

sys.dont_write_bytecode = True

import ast
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import phone  # noqa: E402
import ui_probe as u  # noqa: E402

SWIFT = (SCRIPTS / "phone_screen.swift").read_text()
PHONE_PY = (SCRIPTS / "phone.py").read_text()
UI_PROBE_PY = (SCRIPTS / "ui_probe.py").read_text()
SOUND_CHECK = SWIFT[SWIFT.index("// sound-check: begin"):SWIFT.index("// sound-check: end")]
# The recorder's last lines, from the stop: the picture-only rule's decision, held word for word.
FINAL_TAIL = """
if !recorder.finished {
    summary["failure"] = "the film was not finalised"
    emit(summary)
    exit(10)
}
// After the film is finalised and before anything is reported as done: its tracks, and those that hold sound.
let check = soundCheck(url)
let picture = check.tracks?.contains(AVMediaType.video.rawValue) ?? false
summary["tracks"] = check.tracks ?? NSNull()
summary["audio_tracks"] = check.sound >= 0 ? check.sound as Any : NSNull() as Any
summary["sound_checked"] = check.sound >= 0
if check.sound == 0, !picture, recorder.failure == nil { summary["failure"] = "the film holds no picture track" }
if starterEnded { summary["failure"] = "the program that started the recording ended: the recording was stopped" }
emit(summary)
if check.sound > 0 { exit(8) }              // the film holds sound
if recorder.failure != nil { exit(6) }
if check.sound != 0 || !picture { exit(9) } // the film could not be checked, or holds no picture
if starterEnded { exit(12) }                // stopped early: the program that started it ended
exit(0)
"""
ONE = [{"id": "00008110-001A2B3C4D5E6F70", "name": "Owner's iPhone", "model": "iPhone15,2"}]

STAND_IN = f"""#!{sys.executable} -B
import json, os, pathlib, select, sys, time
cfg = json.loads(pathlib.Path(__file__).with_suffix(".json").read_text())
log = pathlib.Path(cfg["log"])
# A slow machine, for the tests: seconds before the offer (or the list) and before each exit.
OFFER_DELAY = float(os.environ.get("PHONE_TEST_OFFER_DELAY_S", "0"))
EXIT_DELAY = float(os.environ.get("PHONE_TEST_EXIT_DELAY_S", "0"))


def leave(code):
    time.sleep(EXIT_DELAY)
    sys.exit(code)


def note(text):
    with log.open("a") as fh:
        fh.write(text + "\\n")


if sys.argv[1] == "list":
    note("recorder list")
    time.sleep(OFFER_DELAY)
    print(json.dumps({{"devices": cfg["devices"]}}))
    leave(cfg.get("list_exit", 0))
note("recorder record " + " ".join(sys.argv[2:]) + " pid " + str(os.getpid()))
time.sleep(OFFER_DELAY)
out = pathlib.Path(sys.argv[3])
film = out.parent / "film.json"
if out.exists():
    leave(7)
offer = cfg.get("offer", "devices")
if offer == "exit":                     # ends before it offers, as without camera access
    print("no camera access", file=sys.stderr, flush=True)
    leave(3)
if offer == "hang":                     # never offers
    time.sleep(120)
    leave(0)
print("waiting for a phone", file=sys.stderr, flush=True)
print("not an offer" if offer == "rubbish" else json.dumps({{"offered": cfg["devices"]}}), flush=True)
note("recorder offered; film.json " + ("exists" if film.exists() else "MISSING"))
line = sys.stdin.readline()
note("recorder got " + json.dumps(line))
if not line.endswith("\\n") or line[:-1] not in [d["id"] for d in cfg["devices"]]:
    note("recorder stopped")
    leave(11)
note("film.json at the start: " + (film.read_text().replace("\\n", " ") if film.exists() else "MISSING"))
note("lock during the recording: " + str((out.parent / ".ui-probe-phone.lock").exists()))
note("recorder recording " + line[:-1])
if cfg.get("write_film", True):
    out.write_bytes(b"a film")
# While it records (record_s seconds), it watches its stdin as the recorder does: the end of input stops
# it at once; bytes are ignored.
stopped, until = False, time.time() + cfg.get("record_s", 0)
while time.time() < until:
    if select.select([0], [], [], 0.05)[0]:
        if not os.read(0, 256):
            note("recorder stopped: end of input")
            stopped = True
            break
        note("recorder ignored bytes")
if not stopped:
    note("recorder finished")
failure = "the program that started the recording ended: the recording was stopped" if stopped else cfg.get("failure")
try:
    if "raw_summary" in cfg:            # a summary missing (None), cut short or not a JSON object
        if cfg["raw_summary"] is not None:
            print(cfg["raw_summary"])
    else:
        print(json.dumps({{"device": "Owner's iPhone", "model": "iPhone15,2", "start_utc": "2026-11-13T10:00:00.000Z",
                          "stop_utc": "2026-11-13T10:00:05.100Z", "seconds_recorded": float(sys.argv[2]),
                          "failure": failure, "ports": cfg.get("ports", ["vide", "soun"]),
                          "connected": cfg.get("connected", "video"), "tracks": cfg.get("tracks", ["vide"]),
                          "audio_tracks": cfg.get("audio_tracks", 0), "sound_checked": cfg.get("sound_checked", True)}}),
              flush=True)
except BrokenPipeError:
    note("recorder: nobody reads its summary")
leave(12 if stopped else cfg.get("record_exit", 0))
"""
OSASCRIPT = f"""#!{sys.executable} -B
import json, os, sys, time
with open(os.environ["PHONE_TEST_LOG"], "a", encoding="utf-8") as fh:
    fh.write("notify " + json.dumps(sys.argv[1:], ensure_ascii=False) + "\\n")
time.sleep(float(os.environ.get("PHONE_TEST_EXIT_DELAY_S", "0")))
sys.exit(int(os.environ.get("PHONE_TEST_NOTIFY_EXIT", "0")))
"""


def when_the_id_is_written(effect):
    """Patch phone.drain so that effect(proc) runs once, right after the chosen id is written to the
    recorder and before its output is read; the reading itself is real."""
    real, fired = phone.drain, []

    def drain(proc, timeout):
        if not fired:
            fired.append(proc)
            effect(proc)
        return real(proc, timeout)

    return mock.patch.object(phone, "drain", drain)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.log = self.root / "log.txt"
        self.log.write_text("")
        bindir = self.root / "bin"
        bindir.mkdir()
        self.recorder = self.root / "phone-screen"
        self.recorder.write_text(STAND_IN)
        self.recorder.chmod(0o755)
        (bindir / "osascript").write_text(OSASCRIPT)
        (bindir / "osascript").chmod(0o755)
        self.config({})
        self.env = mock.patch.dict(os.environ, {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
                                                "PHONE_TEST_LOG": str(self.log)})
        self.env.start()
        self.built = mock.patch.object(phone, "build", return_value=self.recorder)
        self.built.start()
        self.darwin = mock.patch.object(phone.sys, "platform", "darwin")
        self.darwin.start()

    def tearDown(self):
        for p in (self.darwin, self.built, self.env):
            p.stop()
        self.tmp.cleanup()

    def config(self, extra):
        self.recorder.with_suffix(".json").write_text(json.dumps({"log": str(self.log), "devices": ONE, **extra}))

    def phone(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = u.main(["phone", *[str(a) for a in argv]])
            except SystemExit as exc:
                rc = exc.code
        return rc, out.getvalue(), err.getvalue()

    def record(self, *extra, seconds="5", out=None, go="go given at 10:00 for this test", build="example/app 1a2b3c4 Debug"):
        args = ["record", "--seconds", seconds, "--out", out or self.root / "film"]
        if go is not None:
            args += ["--go", go]
        if build is not None:
            args += ["--build", build]
        return self.phone(*args, *extra)

    def lines(self):
        return self.log.read_text().splitlines()


class RecordTest(Base):
    def test_film_json_before_and_after(self):
        rc, out, err = self.record()
        self.assertEqual(rc, 0, err)
        at_start = next(x for x in self.lines() if x.startswith("film.json at the start: "))
        before = json.loads(at_start.split(": ", 1)[1])
        self.assertEqual((before["status"], before["go"], before["build"], before["seconds_asked"], before["tool"]),
                         ("recording", "go given at 10:00 for this test", "example/app 1a2b3c4 Debug", 5.0,
                          {"name": "ui-probe", "version": u.VERSION}))
        after = json.loads((self.root / "film" / "film.json").read_text())
        self.assertEqual((after["status"], after["exit_status"], after["seconds_recorded"]), ("done", 0, 5.0))
        import hashlib
        self.assertEqual(after["recorder"]["sha256"], hashlib.sha256(self.recorder.read_bytes()).hexdigest())
        self.assertEqual((after["ports"], after["connected"], after["audio_tracks"]), (["vide", "soun"], "video", 0))
        self.assertIn("lock during the recording: True", self.lines())
        self.assertEqual(sorted(p.name for p in (self.root / "film").iterdir()), ["film.json", "screen.mov"])
        self.assertEqual((after["device"]["name"], after["device"]["model"]), ("Owner's iPhone", "iPhone15,2"))
        self.assertEqual((after["start_utc"], after["stop_utc"]), ("2026-11-13T10:00:00.000Z", "2026-11-13T10:00:05.100Z"))
        self.assertEqual((self.root / "film" / "screen.mov").read_bytes(), b"a film")
        self.assertIn("the phone's status bar reads 9:41", out)

    def test_the_mac_posts_a_notice_before_the_recording_starts(self):
        self.assertEqual(self.record()[0], 0)
        lines = [" ".join(x.split()[:2]) for x in self.lines() if x.split()[0] in ("notify", "recorder")]
        self.assertEqual([x for x in lines if x != "recorder got"],          # one recorder: offer, notice, recording
                         ["recorder record", "recorder offered;", "notify [\"-e\",", "recorder recording",
                          "recorder finished"])
        notice = json.loads(next(x for x in self.lines() if x.startswith("notify"))[len("notify "):])
        self.assertEqual(notice[:7], ["-e", "on run argv", "-e",
                                      "display notification (item 1 of argv) with title (item 2 of argv)", "-e", "end run", "--"])
        self.assertIn("go given at 10:00 for this test", notice[7])
        self.assertEqual(notice[8:], ["ui-probe"])

    def test_the_owners_own_words_reach_the_notice_as_they_are(self):
        words = "\u201cGo, record two minutes\u201d \u2014 the owner"          # curly quotes and a dash
        self.assertEqual(self.record(go=words)[0], 0)
        notice = json.loads(next(x for x in self.lines() if x.startswith("notify"))[len("notify "):])
        self.assertIn(words, notice[7])
        self.assertNotIn("\\u201c", " ".join(notice))                  # no escapes AppleScript would not read
        self.assertEqual(json.loads((self.root / "film" / "film.json").read_text())["go"], words)

    @unittest.skipUnless(shutil.which("osacompile"), "no osacompile")
    def test_the_notice_script_compiles(self):
        # Compiled only, into a file: osacompile runs nothing and posts nothing.
        with tempfile.TemporaryDirectory() as tmp:
            lines = [part for line in phone.NOTICE_SCRIPT for part in ("-e", line)]
            out = subprocess.run(["osacompile", "-o", str(Path(tmp) / "notice.scpt"), *lines], capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)

    def test_no_notice_no_recording(self):
        with mock.patch.dict(os.environ, {"PHONE_TEST_NOTIFY_EXIT": "1"}):
            rc, _out, err = self.record()
        self.assertEqual(rc, 2)
        self.assertIn("could not be posted", err)
        self.assertFalse(any(x.startswith("recorder recording") for x in self.lines()))
        self.assertIn('recorder got ""', self.lines())                    # no id: its input was closed
        self.assertEqual(json.loads((self.root / "film" / "film.json").read_text())["status"], "not recorded")

    def test_a_failed_recording_is_recorded_as_failed(self):
        self.config({"record_exit": 6, "failure": "Cannot Record"})
        rc, _out, err = self.record()
        self.assertEqual(rc, 2)
        self.assertIn("the recording failed (exit 6): Cannot Record", err)
        after = json.loads((self.root / "film" / "film.json").read_text())
        self.assertEqual((after["status"], after["exit_status"], after["failure"]), ("failed", 6, "Cannot Record"))

    def test_a_phone_with_a_muxed_port_only_says_so_in_film_json(self):
        self.config({"ports": ["muxx"], "connected": "muxed (no video port offered)"})
        self.assertEqual(self.record()[0], 0)
        after = json.loads((self.root / "film" / "film.json").read_text())
        self.assertEqual((after["ports"], after["connected"]), (["muxx"], "muxed (no video port offered)"))

    def test_a_film_that_holds_sound_fails_and_is_not_deleted(self):
        self.config({"audio_tracks": 1, "record_exit": 8, "ports": ["muxx"], "connected": "muxed (no video port offered)"})
        rc, _out, err = self.record()
        self.assertEqual(rc, 2)
        self.assertIn("the film holds sound (1 audio track): it breaks the picture-only rule. Whoever ran this should "
                      "delete", err)
        self.assertIn("ui-probe deletes nothing", err)
        after = json.loads((self.root / "film" / "film.json").read_text())
        self.assertEqual((after["status"], after["audio_tracks"], after["exit_status"]),
                         ("failed: the film holds sound", 1, 8))
        self.assertTrue((self.root / "film" / "screen.mov").exists())         # the tool deletes nothing

    def test_a_film_not_checked_for_sound_fails(self):
        self.config({"audio_tracks": None})
        rc, _out, err = self.record()
        self.assertEqual(rc, 2)
        self.assertIn("the picture-only rule is unchecked", err)
        after = json.loads((self.root / "film" / "film.json").read_text())
        self.assertEqual((after["status"], after["audio_tracks"]), ("failed: the film could not be checked for sound", None))

    def test_an_interrupted_recording_says_so(self):
        def interrupted(proc):
            raise KeyboardInterrupt

        with when_the_id_is_written(interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.record()
        after = json.loads((self.root / "film" / "film.json").read_text())
        self.assertEqual((after["status"], after["failure"]), ("interrupted", "KeyboardInterrupt"))
        self.assertFalse((self.root / "film" / phone.LOCK).exists())

    def test_film_json_is_written_whole_or_not_at_all(self):
        film = self.root / "film.json"
        film.write_text('{"status": "recording"}')
        real = Path.write_text

        def full(self_, data, *a, **k):
            if self_.name.endswith(".tmp"):
                real(self_, data[:3], *a, **k)
                raise OSError(28, "No space left on device")
            return real(self_, data, *a, **k)

        with mock.patch.object(Path, "write_text", full):
            with self.assertRaises(OSError):
                phone.write_json(film, {"status": "done"})
        self.assertEqual(film.read_text(), '{"status": "recording"}')
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.endswith(".tmp")], [])

    def test_list_says_the_status_bar_reads_9_41(self):
        rc, out, _ = self.phone("list")
        self.assertEqual(rc, 0)
        self.assertIn("the phone's status bar reads 9:41", out)
        self.assertIn("00008110-001A2B3C4D5E6F70  Owner's iPhone  (model iPhone15,2)", out)


class SecondReviewTest(Base):
    """The second review's findings, each through the stand-in recorder."""

    def after(self, out=None):
        return json.loads(((out or self.root / "film") / "film.json").read_text())

    def listing(self, out):
        return sorted(p.name for p in out.iterdir())

    def recorded(self):
        return any(x.startswith("recorder recording") for x in self.lines())

    # 1. every track is listed; a film is done only when the count says no sound
    def test_a_done_film_json_says_its_tracks_and_that_its_sound_was_checked(self):
        self.config({"tracks": ["vide", "tmcd"]})
        rc, _out, err = self.record()
        self.assertEqual(rc, 0, err)
        after = self.after()
        self.assertEqual((after["status"], after["tracks"], after["audio_tracks"], after["sound_checked"],
                          after["film_on_disk"]), ("done", ["vide", "tmcd"], 0, True, True))

    def test_a_muxed_track_counted_as_sound_fails(self):
        self.config({"tracks": ["muxx"], "audio_tracks": 1, "record_exit": 8})
        rc, _out, err = self.record()
        self.assertEqual(rc, 2)
        after = self.after()
        self.assertEqual((after["status"], after["tracks"], after["sound_checked"]),
                         ("failed: the film holds sound", ["muxx"], True))
        self.assertIn("Whoever ran this should delete", err)

    # 2, 8. exits 8, 9 and 10 are mapped even with no summary, or one cut short
    def test_exits_8_9_and_10_are_mapped_without_a_summary(self):
        statuses = {8: "failed: the film holds sound", 9: "failed: the film could not be checked for sound",
                    10: "failed: the film was not finalised"}
        for raw in (None, '{"device": "Owner\'s iPhone", "audio_tr'):
            for code, status in statuses.items():
                with self.subTest(code=code, raw=raw):
                    out = self.root / f"film-{code}-{raw is None}"
                    self.config({"record_exit": code, "raw_summary": raw})
                    rc, _out, err = self.record(out=out)
                    self.assertEqual(rc, 2)
                    after = self.after(out)
                    self.assertEqual((after["status"], after["exit_status"], after["sound_checked"],
                                      after["film_on_disk"], after["audio_tracks"]), (status, code, False, True, None))
                    self.assertIn(f"Whoever ran this should delete {out / 'screen.mov'}; ui-probe deletes nothing", err)
                    if code == 8:                       # the recorder found sound: the rule is broken, not unchecked
                        self.assertIn("the film holds sound (exit 8): it breaks the picture-only rule", err)
                        self.assertNotIn("unchecked", err)
                    else:
                        self.assertIn("so the picture-only rule is unchecked", err)

    def test_a_film_not_finalised_is_never_done_even_with_a_count(self):
        self.config({"record_exit": 10, "failure": "the film was not finalised", "audio_tracks": 0})
        self.assertEqual(self.record()[0], 2)
        self.assertEqual(self.after()["status"], "failed: the film was not finalised")

    # 4. the lock is taken first; another run's record is never overwritten
    def test_a_second_run_while_the_first_compiles_refuses_and_writes_nothing(self):
        out = self.root / "film"
        seen = {}

        def slow_build():
            if not seen:
                seen["before"] = self.listing(out)
                seen["rc"], _o, seen["err"] = self.record(out=out, go="a second go")
                seen["after"] = self.listing(out)
            return self.recorder

        with mock.patch.object(phone, "build", side_effect=slow_build):
            rc, _out, err = self.record(out=out)
        self.assertEqual(seen["rc"], 2)
        self.assertIn("is held by another recording", seen["err"])
        self.assertIn("record into a new folder", seen["err"])
        self.assertEqual(seen["before"], [phone.LOCK])
        self.assertEqual(seen["after"], seen["before"])                 # the second run wrote nothing
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.after()["go"], "go given at 10:00 for this test")

    def test_a_record_that_appears_while_the_folder_is_held_is_not_replaced(self):
        out = self.root / "film"

        def build_meanwhile():
            (out / "film.json").write_text("another run's record")
            return self.recorder

        with mock.patch.object(phone, "build", side_effect=build_meanwhile):
            rc, _out, err = self.record(out=out)
        self.assertEqual(rc, 2)
        self.assertIn("appeared while this run held the folder; it is not replaced", err)
        self.assertEqual((out / "film.json").read_text(), "another run's record")
        self.assertFalse(self.recorded())
        self.assertEqual(self.listing(out), ["film.json"])               # our lock is gone, theirs kept

    def test_a_left_lock_refuses_and_names_a_new_folder(self):
        out = self.root / "film"
        out.mkdir()
        (out / phone.LOCK).write_text("123\n")
        rc, _out, err = self.record(out=out)
        self.assertEqual(rc, 2)
        self.assertIn("record into a new folder", err)
        self.assertEqual(self.listing(out), [phone.LOCK])
        self.assertEqual((out / phone.LOCK).read_text(), "123\n")
        self.assertFalse(self.recorded())

    def test_the_lock_refuses_a_folder_that_holds_anything_else(self):
        out = self.root / "film"
        out.mkdir()
        (out / ".hidden").write_text("x")
        with self.assertRaises(phone.PhoneError) as ctx:
            phone.take_lock(out)
        self.assertIn("holds .hidden", str(ctx.exception))
        self.assertEqual(self.listing(out), [".hidden"])
        self.assertIn("os.O_CREAT | os.O_EXCL", PHONE_PY[PHONE_PY.index("def create_json("):])

    # 5. film.json never says "recording" when that is false
    def test_ctrl_c_while_the_notice_is_posted_records_nothing_and_says_so(self):
        with mock.patch.object(phone, "notify", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.record()
        after = self.after()
        self.assertEqual((after["status"], after["failure"]),
                         ("interrupted before recording: nothing was recorded", "KeyboardInterrupt"))
        self.assertFalse(self.recorded())
        self.assertFalse((self.root / "film" / phone.LOCK).exists())

    def test_a_summary_that_is_not_a_json_object_could_not_be_read(self):
        for i, raw in enumerate(("0", "null", "[1]", '"done"', "true")):
            with self.subTest(raw=raw):
                out = self.root / f"film-{i}"
                self.config({"raw_summary": raw})
                rc, _out, err = self.record(out=out)
                self.assertEqual(rc, 2)
                after = self.after(out)
                self.assertEqual((after["status"], after["sound_checked"], after["film_on_disk"]),
                                 ("failed: the recorder's summary could not be read", False, True))
                self.assertIn("Whoever ran this should delete", err)

    # 6. only the integer 0 is no sound
    def test_zeros_that_are_not_integers_are_not_no_sound(self):
        for i, audio in enumerate((False, 0.0, -0.0, True, "0")):
            with self.subTest(audio=audio):
                out = self.root / f"film-{i}"
                self.config({"audio_tracks": audio})
                rc, _out, err = self.record(out=out)
                self.assertEqual(rc, 2)
                after = self.after(out)
                self.assertEqual((after["status"], after["sound_checked"], after["audio_tracks"]),
                                 ("failed: the film could not be checked for sound", False, None))
                self.assertIn("the picture-only rule is unchecked", err)

    def test_a_count_the_recorder_did_not_say_it_made_is_not_a_check(self):
        self.config({"sound_checked": False})
        self.assertEqual(self.record()[0], 2)
        self.assertEqual(self.after()["status"], "failed: the film could not be checked for sound")

    # 7. --go and --build must be visible
    def test_an_invisible_go_or_build_is_refused(self):
        for go in ("​", "​‍⁠", "﻿ ", "-- !"):
            with self.subTest(go=go):
                rc, _out, err = self.record(go=go)
                self.assertEqual(rc, 2)
                self.assertIn("--go is required", err)
        for build in ("​ ​ ​", "example/app 1a2b3c4 ​", "a - b"):
            with self.subTest(build=build):
                rc, _out, err = self.record(build=build)
                self.assertEqual(rc, 2)
                self.assertIn("--build is required", err)
        self.assertFalse(self.recorded())
        self.assertFalse((self.root / "film").exists())
        self.assertEqual(self.record(go="9", build="r 1 D")[0], 0)

    # third review, 1. a letter that shows nothing is no letter; every script's letters and digits are
    def test_invisible_letters_are_refused_and_every_script_is_accepted(self):
        for go in ("\u3164", "\uffa0", "\u115f", "\u1160", "\u3164\u200b\uffa0", "\U00013441", "\U00013442",
                   "\U00013441\U00013442\u3164"):
            with self.subTest(go=go):
                rc, _out, err = self.record(go=go)
                self.assertEqual((rc, "--go is required" in err), (2, True))
        for build in ("\u3164 \uffa0 \u115f", "example/app 1a2b3c4 \u1160", "\u3164\u3164 a b",
                      "\U00013441 \U00013442 \U00013441", "example/app 1a2b3c4 \U00013442"):
            with self.subTest(build=build):
                rc, _out, err = self.record(build=build)
                self.assertEqual((rc, "--build is required" in err), (2, True))
        self.assertFalse(self.recorded())
        for i, (go, build) in enumerate((("\u9332\u753b\u3057\u3066\u304f\u3060\u3055\u3044", "\u30a2\u30d7\u30ea 1a2b \u958b\u767a"),
                                         ("\u0627\u0628\u062f\u0623 \u0627\u0644\u062a\u0633\u062c\u064a\u0644",
                                          "\u0645\u0633\u062a\u0648\u062f\u0639 \u0661\u0662\u0663 \u062a\u0635\u062d\u064a\u062d"),
                                         ("\u0436\u043c\u0438", "\u0440\u0435\u043f\u043e \u0661 \u0939\u093f"))):
            with self.subTest(go=go, build=build):
                rc, _out, err = self.record(go=go, build=build, out=self.root / f"script-{i}")
                self.assertEqual(rc, 0, err)
                self.assertEqual((self.after(self.root / f"script-{i}")["go"], self.after(self.root / f"script-{i}")["build"]),
                                 (go, build))

    # third review, 3. Ctrl-C after the recorder returned and before film.json is completed
    def test_ctrl_c_after_the_recorder_returned_says_interrupted(self):
        err = io.StringIO()
        with mock.patch.object(phone, "read_summary", side_effect=KeyboardInterrupt), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            with self.assertRaises(KeyboardInterrupt):
                u.main(["phone", "record", "--seconds", "5", "--out", str(self.root / "film"), "--go", "go",
                        "--build", "example/app 1a2b3c4 Debug"])
        after = self.after()
        self.assertEqual((after["status"], after["film_on_disk"], after["sound_checked"], after["failure"]),
                         ("interrupted", True, False, "KeyboardInterrupt"))
        self.assertIn("Whoever ran this should delete", err.getvalue())
        self.assertFalse((self.root / "film" / phone.LOCK).exists())

    # last review, 2. a Ctrl-C after the final write keeps the final record byte for byte
    def test_ctrl_c_after_the_final_write_keeps_the_final_record(self):
        for i, (cfg, final, advice) in enumerate((
                ({"audio_tracks": 1, "tracks": ["vide", "soun"], "record_exit": 8}, "failed: the film holds sound", True),
                ({}, "done", False),
                ({"record_exit": 6, "failure": "Cannot Record"}, "failed", False))):
            with self.subTest(final=final):
                out = self.root / f"film-{i}"
                self.config(cfg)
                real, kept = phone.write_json, {}

                def then_ctrl_c(path, record_):
                    real(path, record_)
                    if record_.get("status") == final:
                        kept["bytes"] = path.read_bytes()
                        raise KeyboardInterrupt

                err = io.StringIO()
                with mock.patch.object(phone, "write_json", side_effect=then_ctrl_c), \
                        contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                    with self.assertRaises(KeyboardInterrupt):
                        u.main(["phone", "record", "--seconds", "5", "--out", str(out), "--go", "go",
                                "--build", "example/app 1a2b3c4 Debug"])
                self.assertEqual((out / "film.json").read_bytes(), kept["bytes"])
                self.assertEqual(self.after(out)["status"], final)
                self.assertEqual("Whoever ran this should delete" in err.getvalue(), advice, err.getvalue())
                self.assertFalse((out / phone.LOCK).exists())

    # third review, 4. done only for a film whose tracks show a picture and no track that can hold sound
    def test_a_film_whose_tracks_are_not_picture_only_is_not_checked(self):
        for i, tracks in enumerate(([], ["tmcd"], ["soun"], ["vide", "soun"], ["vide", "muxx"], ["muxx"], None,
                                    "vide", ["vide", 1])):
            with self.subTest(tracks=tracks):
                out = self.root / f"film-{i}"
                self.config({"tracks": tracks, "audio_tracks": 0})
                rc, _out, err = self.record(out=out)
                self.assertEqual(rc, 2)
                after = self.after(out)
                self.assertEqual((after["status"], after["sound_checked"], after["audio_tracks"], after["film_on_disk"]),
                                 ("failed: the film could not be checked for sound", False, None, True))
                self.assertIn("Whoever ran this should delete", err)
        self.assertEqual(phone.SOUND_TRACKS, {"soun", "muxx"})
        self.assertEqual(phone.PICTURE_TRACK, "vide")

    def test_a_film_with_no_picture_track_says_so(self):
        self.config({"tracks": ["tmcd"], "audio_tracks": 0, "record_exit": 9, "failure": "the film holds no picture track"})
        rc, _out, err = self.record()
        self.assertEqual(rc, 2)
        self.assertIn("the film could not be checked for sound (exit 9): the film holds no picture track", err)
        self.assertEqual((self.after()["status"], self.after()["failure"]),
                         ("failed: the film could not be checked for sound", "the film holds no picture track"))

    # 8. a film on disk whose sound was not checked: film.json says so and the command asks for it deleted
    def test_a_failed_recording_with_a_film_not_checked_asks_for_it_deleted(self):
        self.config({"record_exit": 6, "failure": "Cannot Record", "audio_tracks": None, "sound_checked": False})
        rc, _out, err = self.record()
        self.assertEqual(rc, 2)
        after = self.after()
        self.assertEqual((after["status"], after["sound_checked"], after["film_on_disk"]), ("failed", False, True))
        self.assertIn("the recording failed (exit 6): Cannot Record, so the picture-only rule is unchecked", err)
        self.assertIn("Whoever ran this should delete", err)

    def test_a_failed_recording_with_no_film_asks_nothing(self):
        self.config({"record_exit": 6, "failure": "Cannot Record", "audio_tracks": None, "sound_checked": False,
                     "write_film": False})
        rc, _out, err = self.record()
        self.assertEqual(rc, 2)
        self.assertEqual((self.after()["film_on_disk"], self.after()["sound_checked"]), (False, False))
        self.assertNotIn("should delete", err)

    def test_a_recorder_that_does_not_end_is_its_own_status(self):
        def hangs(proc):
            Path(proc.args[3]).write_bytes(b"part of a film")
            raise subprocess.TimeoutExpired(proc.args, 125, output=b"")

        with when_the_id_is_written(hangs):
            rc, _out, err = self.record()
        self.assertEqual(rc, 2)
        after = self.after()
        self.assertEqual((after["status"], after["exit_status"], after["sound_checked"], after["film_on_disk"]),
                         ("failed: the recorder did not end within 125 s", None, False, True))
        self.assertIn("Whoever ran this should delete", err)

    def test_ctrl_c_during_the_recording_with_a_film_on_disk_asks_for_it_deleted(self):
        def interrupted(proc):
            Path(proc.args[3]).write_bytes(b"part of a film")
            raise KeyboardInterrupt

        err = io.StringIO()
        with when_the_id_is_written(interrupted), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            with self.assertRaises(KeyboardInterrupt):
                u.main(["phone", "record", "--seconds", "5", "--out", str(self.root / "film"), "--go", "go",
                        "--build", "example/app 1a2b3c4 Debug"])
        after = self.after()
        self.assertEqual((after["status"], after["sound_checked"], after["film_on_disk"]), ("interrupted", False, True))
        self.assertIn("Whoever ran this should delete", err.getvalue())


class HandoverTest(Base):
    """The first live run (macOS 26, one iPhone): a phone a process saw is not offered to the next process
    for about 11 to 32 s after it exits, so `record` lists and records in ONE recorder process."""

    # Bounds for a slow machine: the stand-in answers at once, so these cost nothing when the machine is
    # fast; only the test of a recorder that never offers uses short ones (its subject is the bound).
    def setUp(self):
        super().setUp()
        self.bounds = mock.patch.multiple(phone, OFFER_TIMEOUT_S=60, STOP_TIMEOUT_S=60)
        self.bounds.start()

    def tearDown(self):
        self.bounds.stop()
        super().tearDown()

    def tagged(self):
        return [x for x in self.lines() if x.startswith(("recorder", "notify", "film.json at the start"))]

    def gone(self, pid):
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def pid(self):
        return int(next(x for x in self.lines() if x.startswith("recorder record")).rsplit(" pid ", 1)[1])

    def test_one_recorder_offers_then_film_json_then_the_notice_then_the_id(self):
        rc, _out, err = self.record()
        self.assertEqual(rc, 0, err)
        lines = self.tagged()
        self.assertFalse(any(x.startswith("recorder list") for x in lines))          # no listing process first
        self.assertEqual([x.split()[0] + " " + x.split()[1] for x in lines],
                         ["recorder record", "recorder offered;", "notify [\"-e\",", "recorder got",
                          "film.json at", "recorder recording", "recorder finished"])
        self.assertEqual(lines[1], "recorder offered; film.json MISSING")          # nothing written before the choice
        self.assertEqual(lines[3], "recorder got " + json.dumps(ONE[0]["id"] + "\n"))   # the id, after the notice
        self.assertIn('"status": "recording"', lines[4])                            # film.json before the id
        self.assertEqual(len([x for x in lines if x.startswith("recorder record ")]), 1)       # one process
        self.gone(self.pid())

    def test_the_recorders_wait_note_reaches_the_person_as_it_waits(self):
        rc, _out, err = self.record()
        self.assertEqual(rc, 0, err)
        self.assertIn("waiting for a phone", err)

    def test_a_refusal_writes_no_id_and_records_nothing(self):
        two = ONE + [{"id": "00008120-0099AABBCCDD0011", "name": "Another iPhone", "model": "iPhone16,1"}]
        for i, (devices, extra, said) in enumerate(((two, [], "2 phones are offered"), ([], [], "no cabled iPhone"),
                                                    (ONE, ["--device", "1234"], "matches 0 of the 1"))):
            with self.subTest(said=said):
                self.log.write_text("")
                out = self.root / f"film-{i}"
                self.config({"devices": devices})
                rc, _out, err = self.record(*extra, out=out)
                self.assertEqual(rc, 2)
                self.assertIn(said, err)
                self.assertEqual(self.tagged()[1:], ["recorder offered; film.json MISSING", 'recorder got ""',
                                                     "recorder stopped"])
                self.assertEqual(sorted(p.name for p in out.iterdir()), [])     # no film.json: nothing was chosen
                self.gone(self.pid())
                if not devices:
                    self.assertIn("offered again only after up to about half a minute (11 to 32 s", err)

    def test_a_failed_notice_writes_no_id(self):
        with mock.patch.dict(os.environ, {"PHONE_TEST_NOTIFY_EXIT": "1"}):
            rc, _out, err = self.record()
        self.assertEqual(rc, 2)
        self.assertEqual([x for x in self.tagged() if x.startswith("recorder")][1:],
                         ["recorder offered; film.json MISSING", 'recorder got ""', "recorder stopped"])
        self.assertEqual(self.after_status(), "not recorded")
        self.gone(self.pid())

    def after_status(self, out=None):
        return json.loads(((out or self.root / "film") / "film.json").read_text())["status"]

    def test_a_recorder_that_never_offers_is_stopped_at_the_bound(self):
        # Its subject is the bound, so the bounds are short here: 1 s for the offer, 2 s to stop. The
        # stand-in sleeps 120 s and never offers; the run ends long before that and the stand-in is killed.
        self.config({"offer": "hang"})
        with mock.patch.multiple(phone, OFFER_TIMEOUT_S=1, STOP_TIMEOUT_S=2):
            began = time.monotonic()
            rc, _out, err = self.record()
            self.assertLess(time.monotonic() - began, 60)
        self.assertEqual(rc, 2)
        self.assertIn("did not offer its devices within 1 s; nothing was recorded", err)
        self.assertEqual(sorted(p.name for p in (self.root / "film").iterdir()), [])
        self.assertEqual([x for x in self.lines() if x.startswith("recorder")][1:], [])    # it said nothing more
        self.gone(self.pid())

    def test_a_recorder_that_offers_nothing_readable_is_a_clean_failure(self):
        # Nothing was chosen, so no film.json: the go has not been acted on, and the folder is left empty
        # for the next run.
        cases = (("rubbish", "offer is not readable: b'not an offer'"),
                 ("exit", "ended before it offered a device (exit 3): no camera access"))
        for i, (offer, said) in enumerate(cases):
            with self.subTest(offer=offer):
                self.log.write_text("")
                out = self.root / f"film-{i}"
                self.config({"offer": offer})
                rc, _out, err = self.record(out=out)
                self.assertEqual(rc, 2)
                self.assertIn(said, err)
                self.assertIn("nothing was recorded", err)
                self.assertEqual(sorted(p.name for p in out.iterdir()), [])
                self.assertFalse(any(x.startswith("recorder recording") for x in self.lines()))
                self.gone(self.pid())

    def test_list_says_a_phone_looked_at_a_moment_ago_comes_back(self):
        self.config({"devices": []})
        rc, out, _err = self.phone("list")
        self.assertEqual(rc, 0)
        self.assertIn("no phone is offered to this Mac", out)
        self.assertIn("11 to 32 s, measured on one iPhone and one Mac", out)
        self.config({})
        self.assertNotIn("no phone is offered", self.phone("list")[1])

    def test_the_offer_wait_is_bounded_in_python(self):
        self.assertIn("            offered, early = await_offer(proc, OFFER_TIMEOUT_S)\n", PHONE_PY)
        self.assertIn("OFFER_TIMEOUT_S = 135\n", PHONE_PY)
        body = PHONE_PY[PHONE_PY.index("def await_offer("):PHONE_PY.index("def stop(")]
        self.assertIn("        if left <= 0:\n", body)
        self.assertIn("ready, _, _ = select.select(list(open_), [], [], min(left, 1.0))", body)
        stop = PHONE_PY[PHONE_PY.index("def stop("):PHONE_PY.index("def _record(")]
        for line in ("proc.stdin.close()", "proc.wait(timeout=STOP_TIMEOUT_S)", "proc.kill()"):
            self.assertIn(line, stop)


DRIVER = """import sys
sys.dont_write_bytecode = True
from pathlib import Path
from unittest import mock
sys.path.insert(0, sys.argv[1])
import phone, ui_probe as u
with mock.patch.object(phone, "build", return_value=Path(sys.argv[2])), mock.patch.object(phone.sys, "platform", "darwin"):
    sys.exit(u.main(["phone", "record", "--seconds", "30", "--out", sys.argv[3], "--go", "go", "--build", "a b c"]))
"""


class OrphanTest(Base):
    """ui-probe holds the recorder's stdin open for the whole recording; if ui-probe is killed, the end of
    input stops the recorder. Real processes: ui-probe runs in its own process, which is killed."""

    def wait_for(self, text, limit):
        until = time.monotonic() + limit
        while time.monotonic() < until:
            if any(text in x for x in self.lines()):
                return True
            time.sleep(0.05)
        return False

    def test_ui_probe_killed_during_the_recording_stops_the_recorder(self):
        import signal
        self.config({"record_s": 600})                  # it would record 600 s if nothing stopped it
        driver = self.root / "driver.py"
        driver.write_text(DRIVER)
        out = self.root / "film"
        ui = subprocess.Popen([sys.executable, "-B", str(driver), str(SCRIPTS), str(self.recorder), str(out)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=dict(os.environ))
        try:
            self.assertTrue(self.wait_for("recorder recording", 120), self.lines())
            os.kill(ui.pid, signal.SIGKILL)
            killed = time.monotonic()
            ui.wait()
            self.assertTrue(self.wait_for("recorder stopped: end of input", 120), self.lines())
            self.assertLess(time.monotonic() - killed, 120)                     # far below its 600 s
        finally:
            if ui.poll() is None:
                ui.kill()
                ui.wait()
        pid = int(next(x for x in self.lines() if x.startswith("recorder record")).rsplit(" pid ", 1)[1])
        until = time.monotonic() + 120
        while time.monotonic() < until:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail("the recorder still runs")
        events = [x for x in self.lines() if x.startswith("recorder")]
        self.assertLess(events.index("recorder recording " + ONE[0]["id"]), events.index("recorder stopped: end of input"))
        self.assertNotIn("recorder finished", " ".join(self.lines()))
        # What the README says a person finds: film.json still "recording", the lock left, the film on disk.
        self.assertEqual(json.loads((out / "film.json").read_text())["status"], "recording")
        self.assertEqual(sorted(p.name for p in out.iterdir()), [phone.LOCK, "film.json", "screen.mov"])

    def test_a_normal_recording_is_not_cut_short(self):
        self.config({"record_s": 1.5})
        began = time.monotonic()
        rc, _out, err = self.record()
        self.assertEqual(rc, 0, err)
        self.assertGreaterEqual(time.monotonic() - began, 1.5)
        self.assertIn("recorder finished", self.lines())
        self.assertNotIn("recorder stopped: end of input", self.lines())
        self.assertEqual(json.loads((self.root / "film" / "film.json").read_text())["status"], "done")

    def test_exit_12_is_mapped(self):
        self.config({"record_exit": 12, "failure": "the program that started the recording ended: the recording was "
                                                    "stopped"})
        rc, _out, err = self.record()
        self.assertEqual(rc, 2)
        after = json.loads((self.root / "film" / "film.json").read_text())
        self.assertEqual((after["status"], after["exit_status"]),
                         ("failed: the recording was stopped: the recorder's input ended before the recording did", 12))
        self.assertIn("(exit 12): the program that started the recording ended", err)

    def test_stdin_stays_open_from_the_id_to_the_recorders_end(self):
        self.assertNotIn("communicate(", PHONE_PY)                              # it would close stdin
        self.assertEqual(PHONE_PY.count("stdin.close()"), 1)
        self.assertIn("proc.stdin.close()", PHONE_PY[PHONE_PY.index("def stop("):PHONE_PY.index("def _record(")])
        self.assertIn('            proc.stdin.write((device["id"] + "\\n").encode())\n            proc.stdin.flush()\n',
                      PHONE_PY)
        self.assertIn("        rest_out, rest_err = drain(proc, seconds + 120)\n", PHONE_PY)
        self.assertEqual(phone.EXIT_STATUS[12],
                         "failed: the recording was stopped: the recorder's input ended before the recording did")

    def test_the_recorders_wait_reads_stdin(self):
        self.assertIn("let starterEnded = recordFor(seconds)\n", SWIFT)
        self.assertNotIn("spin(seconds)", SWIFT)
        body = SWIFT[SWIFT.index("func recordFor("):SWIFT.index("let arguments = CommandLine.arguments")]
        for line in ("    let deadline = Date().addingTimeInterval(limit)",
                     "        let ready = poll(&fds, 1, 50)",
                     "        if ready < 0 && errno != EINTR { return true }",
                     "            let n = read(0, &bytes, bytes.count)",
                     "            if n == 0 || (n < 0 && errno != EINTR && errno != EAGAIN) { return true }",
                     "        spin(0.05)",
                     "    return false"):
            self.assertIn(line + "\n", body)
        # the watch comes after startRecording and before stopRecording
        order = [SWIFT.index(x) for x in ("output.startRecording(to: url", "let starterEnded = recordFor(seconds)",
                                          "output.stopRecording()")]
        self.assertEqual(order, sorted(order))


SLOW_SUBSET = ("HandoverTest.test_one_recorder_offers_then_film_json_then_the_notice_then_the_id",
               "HandoverTest.test_a_failed_notice_writes_no_id",
               "HandoverTest.test_a_refusal_writes_no_id_and_records_nothing",
               "HandoverTest.test_a_recorder_that_never_offers_is_stopped_at_the_bound",
               "RecordTest.test_a_failed_recording_is_recorded_as_failed",
               "OrphanTest.test_a_normal_recording_is_not_cut_short")


@unittest.skipIf(os.environ.get("PHONE_TEST_OFFER_DELAY_S"), "already running under the delay")
class SlowMachineTest(unittest.TestCase):
    """A slow machine, made without loading this one: the stand-in recorder waits 3 s before its offer
    and 2 s before each exit, and the stand-in osascript 2 s before its exit. The tests whose subject is
    not a hang still pass (a loaded Mac once made the offer miss a 1 s bound that every test then used)."""

    def test_the_tests_do_not_race_a_slow_machine(self):
        env = dict(os.environ, PHONE_TEST_OFFER_DELAY_S="3", PHONE_TEST_EXIT_DELAY_S="2")
        run = subprocess.run([sys.executable, "-B", "-m", "unittest", *[f"test_phone_0110.{t}" for t in SLOW_SUBSET]],
                             cwd=HERE, env=env, capture_output=True, text=True, timeout=900)
        self.assertEqual(run.returncode, 0, run.stderr[-3000:])
        self.assertIn(f"Ran {len(SLOW_SUBSET)} tests", run.stderr)


class RefusalTest(Base):
    def refused(self, *args, **kw):
        rc, _out, err = self.record(*args, **kw)
        self.assertEqual(rc, 2)
        self.assertFalse(any(x.startswith("recorder recording") for x in self.lines()))
        self.assertFalse(any(x.startswith("recorder got") and x != 'recorder got ""' for x in self.lines()))
        return err

    def test_the_hard_cap_of_300_s(self):
        for bad in ("301", "300.5", "0", "-1", "x"):
            self.assertIn("phone:", self.refused(seconds=bad))
        self.assertIn("at most 300 s", self.refused(seconds="301"))
        self.assertEqual(phone.CAP_S, 300)
        self.assertEqual(self.record(seconds="300")[0], 0)

    def test_go_and_build_are_required(self):
        self.assertIn("--go", self.refused(go=None))
        self.assertIn("--build", self.refused(build=None))
        self.assertIn("--go is required", self.refused(go="  "))
        self.assertIn("--build is required", self.refused(build="example/app"))

    def test_go_and_build_are_never_read_from_the_environment(self):
        with mock.patch.dict(os.environ, {"GO": "x", "UI_PROBE_GO": "x", "BUILD": "a b c", "UI_PROBE_BUILD": "a b c"}):
            self.assertIn("--go", self.refused(go=None))
        tree = ast.parse(PHONE_PY)
        # The environment is read in one place only: the cache folder.
        env = {"environ", "environb", "getenv", "getenvb", "putenv", "unsetenv"}
        reads = [ast.unparse(n) for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr in env]
        reads += [n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id in env]
        reads += [a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names if a.name in env | {"*"}]
        self.assertEqual(reads, ["os.environ"])
        self.assertEqual(PHONE_PY.count('os.environ.get("XDG_CACHE_HOME")'), 1)
        # --go and --build: required, with no default at all; nothing sets defaults or reads options from a file
        args = {n.args[0].value: n for n in ast.walk(tree) if isinstance(n, ast.Call)
                and getattr(n.func, "attr", "") == "add_argument" and n.args and isinstance(n.args[0], ast.Constant)}
        for name in ("--go", "--build"):
            self.assertEqual(len(args[name].args), 1, name)
            self.assertEqual(sorted(k.arg for k in args[name].keywords), ["help", "required"], name)
            required = next(k.value for k in args[name].keywords if k.arg == "required")
            self.assertTrue(isinstance(required, ast.Constant) and required.value is True, name)
        self.assertEqual(sum(1 for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "add_argument"), 5)
        parsers = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "ArgumentParser"]
        self.assertEqual([sorted(k.arg for k in n.keywords) for n in parsers], [["description", "prog"]])
        self.assertNotIn("set_defaults", PHONE_PY)
        self.assertNotIn("fromfile", PHONE_PY)
        # no file is read for options: the builtin open is never called (os.open makes the lock and film.json)
        self.assertEqual([n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                          and n.func.id == "open"], [])

    def test_an_existing_output_is_refused(self):
        out = self.root / "film"
        out.mkdir()
        (out / "screen.mov").write_bytes(b"an earlier film")
        self.assertIn("is never replaced", self.refused(out=out))
        self.assertEqual((out / "screen.mov").read_bytes(), b"an earlier film")
        afile = self.root / "a-file"
        afile.write_text("x")
        self.assertIn("is never replaced", self.refused(out=afile))
        self.assertIn("!FileManager.default.fileExists(atPath: url.path)", SWIFT)
        self.assertNotIn("removeItem", SWIFT)

    def test_more_than_one_device_without_device_is_refused(self):
        two = ONE + [{"id": "00008120-0099AABBCCDD0011", "name": "Another iPhone", "model": "iPhone16,1"}]
        self.config({"devices": two})
        self.assertIn("2 phones are offered; say which with --device", self.refused())
        self.assertIn("matches 2 of the 2", self.refused("--device", "0000"))
        self.assertIn("matches 0 of the 2", self.refused("--device", "1234"))
        self.assertEqual(self.record("--device", "00008120")[0], 0)
        self.assertEqual([x for x in self.lines() if x.startswith("recorder recording")],
                         ["recorder recording 00008120-0099AABBCCDD0011"])
        self.assertNotIn("devices.first", SWIFT)                                  # no fallback in the recorder

    def test_a_device_that_is_not_a_phone_needs_device(self):
        camera = {"id": "0x1420000005ac8600", "name": "Desk camera", "model": "UVC Camera VendorID_1452"}
        self.config({"devices": [camera]})
        self.assertIn("other devices offered, recorded only when --device names one", self.refused())
        self.assertEqual(self.record("--device", "0x142", out=self.root / "camera")[0], 0)
        self.config({"devices": [camera] + ONE})
        self.assertEqual(self.record(out=self.root / "phone")[0], 0)
        recorded = [x for x in self.lines() if x.startswith("recorder recording")]
        self.assertTrue(recorded[-1].endswith(ONE[0]["id"]))
        self.assertTrue(phone.is_phone({"model": "iPad13,4"}) and not phone.is_phone(camera))

    def test_two_runs_cannot_share_a_folder(self):
        out = self.root / "shared"
        out.mkdir()
        lock = phone.take_lock(out)
        with self.assertRaises(phone.PhoneError) as ctx:
            phone.take_lock(out)
        self.assertIn("is held by another recording", str(ctx.exception))
        lock.unlink()

    def test_no_device(self):
        self.config({"devices": []})
        self.assertIn("no cabled iPhone", self.refused())

    def test_any_system_but_macos(self):
        with mock.patch.object(phone.sys, "platform", "linux"):
            rc, _out, err = self.phone("list")
            self.assertEqual(rc, 2)
            self.assertIn("only macOS", err)
            self.assertIn("only macOS", self.refused())
        self.assertEqual(self.lines(), [])


class BuildTest(unittest.TestCase):
    def test_the_cache_is_keyed_by_the_sources_sha256_and_compiled_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bindir = root / "bin"
            bindir.mkdir()
            calls = root / "calls"
            (bindir / "swiftc").write_text(f"""#!{sys.executable} -B
import pathlib, sys
open({str(calls)!r}, "a").write(" ".join(sys.argv[1:]) + "\\n")
pathlib.Path(sys.argv[sys.argv.index("-o") + 1]).write_text("a binary")
""")
            (bindir / "swiftc").chmod(0o755)
            source = root / "phone_screen.swift"
            source.write_text("// one\n")
            env = {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", "XDG_CACHE_HOME": str(root / "cache")}
            with mock.patch.dict(os.environ, env), mock.patch.object(phone, "SOURCE", source):
                first = phone.build()
                again = phone.build()
                source.write_text("// two\n")
                other = phone.build()
            import hashlib
            self.assertEqual(first, root / "cache" / "ui-probe" / "phone" / hashlib.sha256(b"// one\n").hexdigest() / "phone-screen")
            self.assertEqual(first, again)
            self.assertNotEqual(first, other)
            self.assertEqual(len(calls.read_text().splitlines()), 2)
            self.assertIn("-O", calls.read_text())

    def test_a_failed_compile_is_an_error_and_caches_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bindir = root / "bin"
            bindir.mkdir()
            (bindir / "swiftc").write_text(  # it fails after writing an output, as a compiler can
                f"#!{sys.executable} -B\nimport pathlib, sys\npathlib.Path(sys.argv[sys.argv.index('-o') + 1]).write_text('half')\n"
                "print('error: no such module', file=sys.stderr)\nsys.exit(1)\n")
            (bindir / "swiftc").chmod(0o755)
            with mock.patch.dict(os.environ, {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
                                              "XDG_CACHE_HOME": str(root / "cache")}):
                with self.assertRaises(phone.PhoneError) as ctx:
                    phone.build()
            self.assertIn("swiftc failed to compile the recorder: error: no such module", str(ctx.exception))
            self.assertFalse(list((root / "cache").glob("**/built.ok")))

    def test_a_replaced_binary_is_not_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bindir = root / "bin"
            bindir.mkdir()
            ran = root / "ran"
            compiled = f"#!{sys.executable} -B\nopen({str(ran)!r}, 'a').write('compiled ran\\n')\n"
            swiftc = bindir / "swiftc"
            swiftc.write_text(f"#!{sys.executable} -B\nimport pathlib, sys\n"
                              f"pathlib.Path(sys.argv[sys.argv.index('-o') + 1]).write_text({compiled!r})\n")
            swiftc.chmod(0o755)
            env = {"PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}", "XDG_CACHE_HOME": str(root / "cache")}
            with mock.patch.dict(os.environ, env):
                binary = phone.build()
                marker = (binary.parent / "built.ok").read_text().splitlines()
                self.assertEqual(marker, [f"source {phone.source_key()}", f"binary {phone.sha256_of(binary)}"])
                binary.write_text(f"#!{sys.executable} -B\nopen({str(ran)!r}, 'a').write('replaced ran\\n')\n")
                binary.chmod(0o755)
                swiftc.write_text(f"#!{sys.executable} -B\nimport sys\nsys.exit(1)\n")      # the rebuild fails
                with self.assertRaises(phone.PhoneError):
                    phone.build()
                with mock.patch.object(phone.sys, "platform", "darwin"):
                    rc = phone.main(["list"], "test")
                self.assertEqual(rc, 2)
                self.assertFalse(ran.exists())                               # the replaced binary never ran
                swiftc.write_text(f"#!{sys.executable} -B\nimport pathlib, sys\n"
                                  f"pathlib.Path(sys.argv[sys.argv.index('-o') + 1]).write_text({compiled!r})\n")
                self.assertEqual(phone.build().read_text(), compiled)      # compiled again, and checked

    @unittest.skipUnless(sys.platform == "darwin" and shutil.which("swiftc"), "no swiftc on macOS")
    def test_the_source_compiles(self):
        # Compiled only: the result is never run (it would open a camera device).
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": tmp}):
                binary = phone.build()
            self.assertTrue(binary.is_file())
            self.assertTrue(os.access(binary, os.X_OK))


PROCESS_MODULES = {"os", "subprocess", "pty", "shutil", "asyncio", "multiprocessing", "commands", "popen2"}
PROCESS_FUNCS = {"system", "popen", "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe",
                 "execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe", "fork", "forkpty",
                 "posix_spawn", "posix_spawnp", "kill", "killpg", "run", "call", "check_call", "check_output", "Popen",
                 "getoutput", "getstatusoutput", "spawn", "create_subprocess_exec", "create_subprocess_shell", "Process",
                 "which"}
SWIFT_DENIED = ("Process", "NSTask", "posix_spawn", "execl", "execv", "vfork", "system(", "dlopen", "dlsym",
                "NSAppleScript", "OSAScript", "NSUserUnixTask", "NSWorkspace", "popen", "fork(", "MobileDevice", "AMDevice",
                "devicectl", "simctl", "openURL", "CGEvent")


DYNAMIC = {"__import__", "eval", "exec", "compile", "getattr", "vars", "globals", "locals", "importlib"}


def process_refs(source: str) -> list[str]:
    """Every REFERENCE in source to a process function of the os, subprocess, pty or shutil modules (and
    the like), called or not: an attribute access ('module.function', with ' (not called)' when it is
    not the function of a call, as in `opener = os.popen`), a from-import ('from module import name'),
    a process module bound or passed under another name ('module as a value'); and every dynamic
    import, evaluation or lookup by name ('getattr', '__dict__')."""
    tree = ast.parse(source)
    modules = {}
    found = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name.split(".")[0] in PROCESS_MODULES:
                    modules[a.asname or a.name.split(".")[0]] = a.asname and a.name or a.name.split(".")[0]
        elif isinstance(n, ast.ImportFrom) and n.module and n.module.split(".")[0] in PROCESS_MODULES:
            for a in n.names:
                if a.name in PROCESS_FUNCS or a.name == "*" or a.name in PROCESS_MODULES:
                    found.append(f"from {n.module} import {a.name}")
    called = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    values = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in modules \
                and n.attr in PROCESS_FUNCS:
            found.append(f"{modules[n.value.id]}.{n.attr}" + ("" if id(n) in called else " (not called)"))
        elif isinstance(n, ast.Attribute) and n.attr == "__dict__":
            found.append("__dict__")
        elif isinstance(n, ast.Name) and n.id in modules and id(n) not in values:
            found.append(f"{modules[n.id]} as a value")
        elif isinstance(n, ast.Name) and n.id in DYNAMIC:
            found.append(n.id)
    return found


class StaticTest(unittest.TestCase):
    """What the code may do, read from the code: allow-lists, with deny-lists beside them."""

    def test_the_phone_module_starts_only_its_four_known_processes(self):
        # swiftc, the recorder's list and osascript run to their end; the recorder's record is the one
        # process held open, from its offer to the end of the recording
        self.assertEqual(sorted(process_refs(PHONE_PY)), ["subprocess.Popen"] + ["subprocess.run"] * 3)

    def test_the_allow_list_sees_what_it_must(self):
        for bad in ("import os\nos.system('x')", "import subprocess\nsubprocess.check_call(['x'])",
                    "import subprocess as s\ns.Popen(['x'])", "from subprocess import call\ncall(['x'])",
                    "import pty\npty.spawn('x')", "import os\nos.execv('x', [])", "__import__('os')",
                    "import os\nopener = os.popen\nopener('x')", "from os import popen as p", "from os import *",
                    "import os\nm = os\nm.system('x')", "import os\nrun = [os][0].system", "import os\nf = getattr(os, 'popen')",
                    "import os\nos.__dict__['popen']('x')", "import subprocess\nr = subprocess.run",
                    "import shutil as sh\nw = sh.which", "import os.path\nos.system('x')"):
            self.assertTrue(process_refs(bad), bad)

    def test_the_swift_source_imports_three_frameworks_and_calls_nothing_denied(self):
        kinds = "(?:(?:class|struct|enum|func|var|let|protocol|typealias)[ \\t]+)?"
        imports = re.findall(r"^[ \t]*(?:@\w+[ \t]+)?import[ \t]+" + kinds + r"([\w.]+)", SWIFT, re.M)
        self.assertEqual(len(imports), len(re.findall(r"\bimport\b", SWIFT)))      # every import is seen
        self.assertEqual(sorted(imports), ["AVFoundation", "CoreMediaIO", "Foundation"])
        for word in SWIFT_DENIED:
            self.assertNotIn(word, SWIFT, word)

    def test_the_swift_source_attaches_the_picture_only(self):
        self.assertIn("session.addInputWithNoConnections(input)", SWIFT)
        self.assertIn('"ports": offered,', SWIFT)                          # what the phone offered goes in film.json
        self.assertIn('"connected": connected', SWIFT)                     # and what was connected
        # The ports connected: the video ports, else the muxed ones, never an audio port.
        filters = re.findall(r"input\.ports\.filter \{ \$0\.mediaType == \.(\w+) \}", SWIFT)
        self.assertEqual(filters, ["video", "muxed"])
        self.assertEqual(len(re.findall(r"input\.ports\.filter", SWIFT)), 2)
        self.assertEqual(re.findall(r"\bvideoPorts = .*", SWIFT), ["videoPorts = videoOffered.isEmpty ? muxedOffered : videoOffered"])
        self.assertEqual(re.findall(r"AVCaptureConnection\(.*", SWIFT), ["AVCaptureConnection(inputPorts: videoPorts, output: output)"])
        self.assertEqual(re.findall(r"addConnection\(\w+\)", SWIFT), ["addConnection(pictureOnly)"])
        for word in ("addInput(", "AVCaptureAudio"):
            self.assertNotIn(word, SWIFT, word)
        # `.audio` only where the finished film's tracks are read, never for a port
        line = ("                if track.mediaType == .audio || track.mediaType == .muxed || "
                "characteristics.contains(.audible) {")
        self.assertEqual(re.findall(r".*\.audio\b.*", SWIFT), [line])
        self.assertIn(line, SOUND_CHECK)

    def test_every_track_is_read_and_audio_muxed_or_audible_is_sound(self):
        self.assertIn("for track in try await AVURLAsset(url: url).load(.tracks) {", SOUND_CHECK)
        self.assertIn("kinds.append(track.mediaType.rawValue)", SOUND_CHECK)
        self.assertNotIn("withMediaType", SWIFT)                       # all tracks, not those of one type
        self.assertIn('summary["tracks"] = check.tracks ?? NSNull()', SWIFT)

    def test_the_count_waits_at_most_20_s_and_turns_the_run_loop(self):
        # Bounded by the clock, not by turns: RunLoop.run(until:) can return at once on a run loop with no
        # source, so each turn first waits on the semaphore itself, and the deadline is a Date.
        self.assertIn("func soundCheck(_ url: URL, limit: Double = 20) -> SoundCheck {", SOUND_CHECK)
        wait = SOUND_CHECK[SOUND_CHECK.index("    let deadline"):SOUND_CHECK.index("    return result")]
        self.assertEqual(wait, "    let deadline = Date().addingTimeInterval(limit)\n"
                               "    while done.wait(timeout: .now() + 0.05) == .timedOut {\n"
                               "        if Date() >= deadline { return SoundCheck() }\n"
                               "        spin(0.05)\n"
                               "    }\n")
        self.assertNotIn("waited", SOUND_CHECK)
        self.assertNotIn("done.wait()", SWIFT)                          # no wait without a bound
        self.assertNotIn(".wait(timeout: .distantFuture)", SWIFT)

    def test_the_film_is_checked_for_sound_after_it_is_finalised_and_before_success(self):
        stop = SWIFT.index("output.stopRecording()")
        settle = SWIFT.index("while !recorder.finished && settle < 10 { spin(0.25); settle += 0.25 }")
        self.assertLess(stop, settle)
        # Between the stop and the end of the file: nothing but this, word for word. A film whose end the
        # recorder has not reported exits 10 before it is checked; the count runs once; exit(0) only after it.
        tail = SWIFT[SWIFT.index("if !recorder.finished {"):]
        self.assertEqual(tail.strip(), FINAL_TAIL.strip())
        between = SWIFT[stop:SWIFT.index("if !recorder.finished {")]
        self.assertEqual(re.findall(r"\b(?:exit|fail|return)\b", between), [])
        self.assertEqual(SWIFT.count("soundCheck(url)"), 1)

    def test_no_exit_0_skips_the_count(self):
        # Every exit names its status as a literal, every fail a status other than 0, and exit(0) is
        # reached in two places only: the end of `list`, and the last line of the file after the count.
        self.assertEqual([a for a in re.findall(r"\bexit\(([^)]*)\)", SWIFT) if not re.fullmatch(r"\d+", a)], ["code"])
        self.assertIn("func fail(_ code: Int32, _ message: String) -> Never {\n"
                      "    FileHandle.standardError.write((message + \"\\n\").data(using: .utf8)!)\n    exit(code)\n}", SWIFT)
        self.assertEqual([a for a in re.findall(r"\bfail\(([^,)]*)", SWIFT)
                          if not re.fullmatch(r"[1-9]\d*", a.strip()) and a.strip() != "_ code: Int32"], [])
        self.assertEqual(SWIFT.count("exit(0)"), 2)
        listing = SWIFT[SWIFT.index('if arguments[1] == "list" {'):]
        self.assertTrue(listing.split("}\n", 1)[0].rstrip().endswith("exit(0)"))
        self.assertTrue(SWIFT.rstrip().endswith("\nexit(0)"))
        self.assertLess(SWIFT.index("let check = soundCheck(url)"), SWIFT.rindex("exit(0)"))
        self.assertNotIn("return", SWIFT[SWIFT.index("let matching = devices.filter"):])
        # The recording path, from the stop: the finished guard (exit 10) first, then the one count, then
        # the one exit(0), last.
        path = SWIFT[SWIFT.index("output.stopRecording()"):]
        guard, guard_exit = path.index("if !recorder.finished {"), path.index("    exit(10)\n}")
        count, zero = path.index("soundCheck(url)"), path.index("exit(0)")
        self.assertTrue(guard < guard_exit < count < zero, (guard, guard_exit, count, zero))
        self.assertEqual(path.count("exit(0)"), 1)
        self.assertEqual(path.count("soundCheck("), 1)
        self.assertEqual(SWIFT[:SWIFT.index("output.stopRecording()")].count("soundCheck(url"), 0)
        self.assertEqual(SWIFT[:SWIFT.index("output.stopRecording()")].count("exit(0)"), 1)    # the end of `list`

    def test_the_recorder_offers_once_and_waits_for_its_line_at_most_60_s(self):
        self.assertIn('//   phone-screen record <seconds> <out.mov>  the same devices as one line {"offered": [...]}, then one',
                      SWIFT)
        # discovery by the clock: 60 s for `record`, 8 s for `list`, and the note said once after 3 s
        self.assertIn('let lookFor = arguments[1] == "record" ? 60.0 : 8.0\n', SWIFT)
        self.assertIn("while !devices.contains(where: isPhone) && Date() < looking.addingTimeInterval(lookFor) {", SWIFT)
        self.assertIn("    if !noted && Date() >= looking.addingTimeInterval(3) {", SWIFT)
        self.assertIn("11 to 32 s, measured on one iPhone and one Mac", SWIFT)
        self.assertIn('return text.contains("iphone") || text.contains("ipad")', SWIFT)
        self.assertEqual(phone.PHONE_WORDS, ("iphone", "ipad"))                  # the same words in both
        # one offered line, flushed; then one line read, bounded at 60 s by the clock
        self.assertEqual(SWIFT.count('emit(["offered": found])'), 1)
        self.assertIn("    fflush(stdout)", SWIFT[SWIFT.index("func emit("):SWIFT.index("func say(")])
        self.assertEqual(re.findall(r"readChoice\(within[^)]*\)", SWIFT), ["readChoice(within limit: Double)",
                                                                            "readChoice(within: 60)"])
        reader = SWIFT[SWIFT.index("func readChoice("):SWIFT.index("let arguments = CommandLine.arguments")]
        for line in ("    let deadline = Date().addingTimeInterval(limit)",
                     "        let left = deadline.timeIntervalSinceNow",
                     "        if left <= 0 { return nil }",
                     "        let ready = poll(&fds, 1, Int32(min(left, 1.0) * 1000) + 1)",
                     "        if read(0, &byte, 1) != 1 { return nil }"):
            self.assertIn(line + "\n", reader)
        # the order: offer, the line, the exact match, and only then the capture session
        order = [SWIFT.index(x) for x in ('emit(["offered": found])', "let choice = readChoice(within: 60)",
                                          "let matching = devices.filter { $0.uniqueID == choice }",
                                          'fail(11, "no device chosen: nothing was recorded")',
                                          "let session = AVCaptureSession()")]
        self.assertEqual(order, sorted(order))
        self.assertEqual(SWIFT.count("AVCaptureSession("), 1)                     # the one session, after the choice
        self.assertEqual(SWIFT.count("AVCaptureDeviceInput("), 1)
        self.assertNotIn("arguments[4]", SWIFT)                                   # no id on the command line

    def test_the_wait_accepts_the_connection_it_made(self):
        self.assertIn("if session.isRunning, pictureOnly.isActive, pictureOnly.isEnabled { break }", SWIFT)
        self.assertNotIn("output.connection(with: .video)", SWIFT)

    def test_the_swift_source_has_its_own_hard_cap_and_no_watch(self):
        # One cap constant, 300; one guard comparing the seconds asked with it and nothing else (no
        # arithmetic, no second comparison OR-ed or AND-ed in); the seconds recorded are those asked.
        self.assertEqual(re.findall(r".*\bcapSeconds\b.*", SWIFT), [
            "let capSeconds = 300.0          // the hard cap of one recording; nothing raises it",
            '    guard asked > 0, asked <= capSeconds else { fail(2, "a recording lasts more than 0 and at most '
            '\\(Int(capSeconds)) s") }'])
        self.assertEqual(re.findall(r".*\basked\b.*", SWIFT), [
            "    guard arguments.count == 4, let asked = Double(arguments[2]) else {",
            '    guard asked > 0, asked <= capSeconds else { fail(2, "a recording lasts more than 0 and at most '
            '\\(Int(capSeconds)) s") }',
            "    seconds = asked"])
        self.assertEqual(re.findall(r".*\bseconds\b\s*[-+*/]?=(?!=).*", SWIFT), ["var seconds = 0.0", "    seconds = asked"])
        # the recording lasts recordFor(seconds), which watches stdin; every spin is a literal number of seconds
        self.assertEqual([a for a in re.findall(r"\bspin\(([^)]*)\)", SWIFT) if not re.fullmatch(r"[\d.]+", a)],
                         ["_ seconds: Double"])
        tree = ast.parse(PHONE_PY)
        caps = [n for n in ast.walk(tree) if isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign))
                and "CAP_S" in ast.unparse(n.targets[0] if isinstance(n, ast.Assign) else n.target)]
        self.assertEqual([ast.unparse(n) for n in caps], ["CAP_S = 300"])
        self.assertEqual(re.findall(r".*\bCAP_S\b\s*[<>=!]=?.*|.*[<>]=?\s*CAP_S\b.*", PHONE_PY),
                         ["CAP_S = 300", "    if not (0 < seconds <= CAP_S):"])
        for word in ("WasConnected", "wasConnected", "NotificationCenter", "addObserver", "while true", "daemon",
                     "dispatchMain", "RunLoop.main.run()"):
            self.assertNotIn(word, SWIFT, word)

    def test_the_phone_module_imports_nothing_of_the_probe(self):
        tree = ast.parse(PHONE_PY)
        modules = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        modules |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        self.assertEqual(modules, {"__future__", "argparse", "hashlib", "json", "os", "select", "subprocess", "sys", "time",
                                   "pathlib",
                                   "unicodedata"})
        for word in ("ui_probe", "ui_video", "run_once", "rehearse", "adb", "simctl", "xcrun", "devicectl", "--watch",
                     "daemon", "launchd", "while True"):
            self.assertNotIn(word, PHONE_PY, word)

    def test_the_phone_module_runs_only_swiftc_osascript_and_its_recorder(self):
        tree = ast.parse(PHONE_PY)
        heads = []
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and getattr(n.func, "attr", "") in ("run", "Popen", "call", "check_output"):
                cmd = n.args[0]
                self.assertIsInstance(cmd, ast.List)
                head = cmd.elts[0]
                heads.append(head.value if isinstance(head, ast.Constant) else ast.unparse(head))
        self.assertEqual(sorted(heads), sorted(["swiftc", "osascript", "str(binary)", "str(binary)"]))

    def test_no_other_verb_calls_it(self):
        self.assertEqual(len(re.findall(r"^\s*import phone\b", UI_PROBE_PY, re.M)), 1)
        block = UI_PROBE_PY[UI_PROBE_PY.index('if argv[:1] == ["phone"]:'):]
        self.assertTrue(block.split("\n", 4)[3].strip() == "import phone")
        self.assertEqual(re.findall(r"\bphone\.\w+\(", UI_PROBE_PY), ["phone.main("])
        for name in ("run_once", "rehearse", "run_once_sim"):
            fn = UI_PROBE_PY[UI_PROBE_PY.index(f"def {name}("):]
            fn = fn[:fn.index("\ndef ", 10)]
            self.assertNotIn("phone", fn, name)


class DoctorTest(unittest.TestCase):
    def test_doctor_phone(self):
        with mock.patch.object(u.sys, "platform", "darwin"), \
                mock.patch.object(u.shutil, "which", side_effect=lambda t: f"/usr/bin/{t}" if t != "adb" else None), \
                mock.patch.object(u.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="v 1\n", stderr="")):
            out = u.doctor(None, phone=True)
        checks = {c["check"]: c for c in out["checks"]}
        self.assertTrue(checks["macOS"]["ok"] and checks["swiftc"]["ok"])
        self.assertNotIn("adb", checks)
        with mock.patch.object(u.sys, "platform", "linux"), \
                mock.patch.object(u.shutil, "which", return_value=None):
            out = u.doctor(None, phone=True)
        checks = {c["check"]: c for c in out["checks"]}
        self.assertFalse(checks["macOS"]["ok"] or checks["swiftc"]["ok"])
        self.assertFalse(out["ok"])


    def test_the_command_line_asks_for_the_phone_checks(self):
        seen = []

        def fake(serial, simulator=None, **kw):
            seen.append(kw)
            return {"ok": True, "checks": []}

        with mock.patch.object(u, "doctor", fake), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(u.main(["doctor", "--phone"]), 0)
            self.assertEqual(u.main(["doctor"]), 0)
        self.assertEqual(seen, [{"phone": True}, {}])


class InstallerTest(unittest.TestCase):
    def test_the_installer_copies_the_recorders_source_and_no_binary(self):
        install = (SCRIPTS / "install.sh").read_text()
        files = re.search(r'^FILES="([^"]+)"', install, re.M).group(1).split()
        self.assertIn("phone.py", files)
        self.assertIn("phone_screen.swift", files)
        self.assertNotIn("swiftc", install)
        self.assertEqual(sorted(p.name for p in SCRIPTS.iterdir() if p.name.startswith("phone")),
                         ["phone.py", "phone_screen.swift"])


class DocsTest(unittest.TestCase):
    def test_documented(self):
        root = SCRIPTS.parent
        readme = " ".join((root / "README.md").read_text().split())
        for phrase in ("## Recording a cabled iPhone: `ui-probe phone`", "ui-probe phone record --seconds",
                       "the phone's status bar reads 9:41", "display notification", "at most 300 s",
                       "`doctor --phone`"):
            self.assertIn(phrase, readme, phrase)
        self.assertIn("## 0.11.0", (root / "CHANGELOG.md").read_text())
        self.assertIn("### Validation, 0.11.0", (root / "PROVENANCE.md").read_text())
        self.assertIn("ui-probe 0.11.0", (root.parent.parent / "CHANGELOG.md").read_text())
        # the plugin's own version only; the marketplace's is never pinned here
        self.assertEqual(u.VERSION, "0.11.0")
        self.assertEqual(json.loads((root / ".claude-plugin" / "plugin.json").read_text())["version"], "0.11.0")


if __name__ == "__main__":
    unittest.main()
