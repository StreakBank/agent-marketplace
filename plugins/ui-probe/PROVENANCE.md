# Provenance — ui-probe

## Origin

All code in `scripts/` was written for this plugin. No upstream code or text is vendored, and
nothing is fetched at run time.

## Formats the code reads, and where they are defined

| Format | Used by | Defined in (upstream) |
|---|---|---|
| `dumpsys gfxinfo <package> framestats` text | `ui_probe.parse_gfx` | Android platform, `frameworks/base/libs/hwui/` (JankTracker, FrameInfo); column names are read from the header row at run time |
| `dumpsys SurfaceFlinger --timestats -dump` text | `ui_probe.parse_timestats` | Android platform, `frameworks/native/services/surfaceflinger/TimeStats/`; readable by the shell user on a `user` build (checked on API 34). One layer can have several `uid = ` blocks (a record per refresh-rate and render-rate bucket, read from the platform source, not yet observed on a device); the parser sums them |
| `/proc/<pid>/stat` and `/proc/<pid>/task/<tid>/stat` | `ui_probe.parse_cpu`, `ui_probe.parse_thread_cpu` | Linux `proc(5)`: fields 14 and 15 (utime, stime) in clock ticks. The process file's values include threads that have exited; the task files give the live threads |
| Perfetto trace protobuf | `pftrace.py` | Perfetto `protos/perfetto/trace/`: `trace_packet.proto` (ftrace_events = 1, process_tree = 2, timestamp = 8, trace_stats = 35, frame_timeline_event = 76), `android/frame_timeline_event.proto`, `ps/process_tree.proto`, `ftrace/ftrace_event_bundle.proto` (event = 2, lost_events = 3), `ftrace/ftrace_event.proto`, `ftrace/ftrace.proto` (print) |
| The trace's own loss counters | `pftrace.buffer_loss` | Perfetto `protos/perfetto/common/trace_stats.proto` at v59.1: `TraceStats` (buffer_stats = 1, flushes_failed = 14, final_flush_outcome = 15) and `BufferStats` (chunks_overwritten = 3, chunks_discarded = 18, trace_writer_packet_loss = 19). The definitions describe the first two as "loss of data" |
| The resumed-activity line of `dumpsys activity activities` | `ui_probe.parse_foreground` | Android platform, `frameworks/base/services/core/java/com/android/server/wm/`; the line is matched by any of three names it has carried, and the component is read from the `ActivityRecord{...}` text. Format confirmed on API 34 only |
| Per-frame timestamp track in a `screenrecord` file | `ui_video.recorder_timestamps` | Android platform, `frameworks/av/cmds/screenrecord/`; layout confirmed against a capture on API 34 (255 timestamps for 255 frames). The platform source describes the values as system time in the elapsed-realtime clock, the clock of the step marks |
| An installed app's `Info.plist` on the iOS Simulator | `ui_probe.app_identity` | Apple's Information Property List keys `CFBundleShortVersionString`, `CFBundleVersion` and `CFBundleExecutable` (Bundle Resources documentation), read with Python's `plistlib` (binary or XML) from the path `xcrun simctl get_app_container` prints; checked by the unit tests on a binary plist they write, not yet against a real simulator's bundle |

Field numbers in `pftrace.py` were read from the published protobuf definitions, not recalled.

## Validation

| Date | What | Against | Result |
|---|---|---|---|
| 2026-11-10 | `pftrace.read_trace`, `read_atrace` | Perfetto trace processor v59.1 (`1e9c61e98`) on two real traces | Equal on every count compared: display frames, app-layer frames, and eight trace-section counts. The trace processor counts one more surface frame, which the reader reports separately as unterminated |
| 2026-11-10 | The trace's loss counters (`pftrace.buffer_loss`) | The trace processor's `stats` table, on a device trace recorded with a 64 kB buffer | Equal: 249 chunks overwritten, 193 failed patches |
| 2026-11-10 | Events dropped by the kernel (`read_atrace`'s `lost_bundles`) | The trace processor, on a device trace recorded with a 4 kB kernel event buffer | The reader reports a loss on 15 of 24 event bundles; the trace processor flags data loss on all four CPUs. The buffer counters of that trace are clean in both |
| 2026-11-10 | The loss counters on healthy traces | 158 saved device traces of 40 to 90 s | One statistics packet each; every loss counter zero. The final-flush outcome is "succeeded" in the 93 that ran to their set duration and "unspecified" in the 65 that the probe stopped early, so only "failed" is treated as a loss |
| 2026-11-10 | The resumed-activity line | `dumpsys activity activities` on API 34 with the app in front, the launcher in front and a second app in front | `topResumedActivity=ActivityRecord{<id> u0 <package>/<activity> t<n>}` is the first match each time; reading it took under 10 ms |
| 2026-11-10 | Whether a recorder the device shell started is still running | A recorder that stopped by itself at its time limit, and one still recording | `kill -0` fails for the first and succeeds for the second; `wait` reports status 0 for both, which is why the exit status alone is not used |
| 2026-11-10 | The analysis (frames, signatures, section counts) | The prototype this plugin was built from, on five saved runs of two apps | Identical |
| 2026-11-10 | Unit tests | macOS (Python 3.12.8, numpy 2.5.3, Pillow 12.3.0) and Linux containers (Python 3.12 and 3.9; numpy 2.0.2 and Pillow 11.3.0 on 3.9) | 281 tests pass on all three |
| 2026-11-10 | The code as a whole | An independent code review before release | 33 findings, among them 15 ways to report a result that had not been observed. All are addressed, each with a test |

Two things no unit test could have shown were found by running the tool for real, and are why
the live table below is redone for every release: stopping the tracer crashed in the process
plumbing that the tests had replaced with a stand-in, and a proxy left over from an earlier run
kept its port, so a whole validation pass went through old code with nothing to say so.

### Live validation, 0.1.0

Date 2026-11-10. Host: macOS 26.1 on Apple Silicon; Python 3.12.8, numpy 2.5.3, Pillow 12.3.0;
adb 36.0.2; ffmpeg 7.0.1; Android emulator 37.1.4.0. Device: an emulator with an API 34 Google
Play arm64 image (`user` build, no root), 1080 x 2400, 4 cores, 4 GB. The host was busy with
other work throughout (load average 7 to 36); timings below are therefore loose, and no count
moved with the load.

| Command | Exercised against | Result |
|---|---|---|
| `doctor --serial` | the emulator | every required item found, exit 0; the optional trace processor reported as absent |
| `run --sections`, `run --trace` | `examples/system-settings.json`, 5 runs each | completed; all 30 rest windows read 0 on all three frame counts. The three counts are equal on the three swipe steps in 10 of 10 runs (71, 71, 71 for one scroll); they differ by one or two frames on the two steps that change window (46, 46, 47) and by about ten at launch (38, 49, 38), which is why the largest decides. The spring-back step is `E` in 10 of 10 |
| `run` with `until_quiet` | a tabbed app under test, three scenarios (each tab at rest, a pull on each tab, a pushed screen and back), 7 runs | completed; all 31 rest windows read 0 on every available count, with at most 0.6% of one core; two tab opens drew exactly 5 and 6 frames in 5 of 5 runs on all three counts, a third drew 9 to 13 (it waits on content); all 8 pulls are `E`; the app's activity was the resumed one at the end of every step |
| `gate` | all 17 runs above | PASS, exit 0, no notes |
| `gate`, foreground | a scenario that sends the app to the background and rests | FAIL, exit 1, on `foreground`, naming the launcher's activity. The same scenario with the rest marked `"background": true` passes: the app's own frame counts read 0, and the recording, which showed the launcher changing 49 frames, was not used |
| `gate`, failed launch | a launch component that does not exist | FAIL, exit 1: the launch error, the launcher in front, no layer of the app in the compositor, and a rest that could not be observed. The app's process was nevertheless running in the background, so a process check alone would have passed |
| `gate`, faulty build | the same app built with an animation left running at zero alpha, 1 run | FAIL, exit 1: 16 violations (4 each of quiet, rest-frames, rest-video, rest-cpu); 218 frames and 41% of one core in a 5 s rest window. `--advisory`: the same violations, exit 0 |
| `run`, interrupted | the device script killed 14 s into a run | exit 2, naming the script's exit status (143); no recorder and no tracer left on the device, the compositor's statistics switched off; no capture left behind, and `gate` on the empty directory is exit 2 |
| `analyze` | every capture of this validation, 23 runs | rewritten; the re-analysis of a run is identical to its first analysis, and every verdict is unchanged |
| `summary --json --csv` | a batch of 3 | written; one CSV row per run |
| `compare` | two batches of the same build, 5 runs each | 95 metrics same, 140 unknown, 0 different. A clean build against the faulty one: rest frames different. Three runs against two of a count that went from 9 to 45: unknown, as it should be at that size |
| `sheet`, `zoom` | a saved run | sheets rewritten; the zoom image written; a zoom of frames that do not exist is exit 2 |
| `proxy` | curl on the host, then the app with the device's system proxy pointed at it | Prints its ready line; a second proxy on the same port is exit 2. With 200 ms each way and 400 kbit/s: an HTTPS request took 1.0 s (0.1 s direct), 8 kB took 0.49 s, 100 kB took 2.4 s; a refused target got 502. In three runs the app's first opens drew 20 to 23 and 45 to 46 frames instead of 5 and 9 to 13 while its placeholders animated; rest windows stayed at 0 and the gate passed. The app carried a whole run over one tunnel, still open when the run ended: the log held no line for it until the proxy was stopped, and then one, marked "open when the proxy stopped". Stopped by a terminate signal the proxy exits 0 and prints nothing |
| `install.sh --apply` | a scratch prefix | the launcher runs and reports the version; a second run changes nothing |
| `install-trace-processor.sh --apply` | a scratch prefix, mac-arm64 | downloaded, checksum verified, the launcher reports v59.1 (`1e9c61e98`); a second run changes nothing and leaves no partial download |

Three behaviours of real apps and of the platform found during validation shaped the defaults.
The stock Settings app draws about thirty frames that change nothing visible in the seconds
after it starts and after it returns to its home page, so launch keeps a fixed floor before any
wait for quiet and a step may set one. A wait that watched only the app's own frames let window
animations drawn by the system run on into the next step, so the wait also watches the
display's frame counter. And the launcher redraws for seconds after the home key, which a
recording attributes to whatever is on screen, so a recording is evidence about the app only
while the app is in front.

Not validated: a physical device; a device that has slept since boot; Android versions other
than 14; a Linux host running against a device (the unit tests run there); the multi-block
layout of the compositor's statistics (the parser sums blocks; none was seen on this device).

### Validation, 0.2.0

| Date | What | Against | Result |
|---|---|---|---|
| 2026-11-10 | Unit tests | macOS (Python 3.12.8) | 379 tests pass: the 281 of 0.1.0 and 98 new |
| 2026-11-10 | The element lookup's shell code (`find_el`) | The host's `sh` (bash 3.2 in POSIX mode), `dash` and `bash`, with a stand-in for the window-tree dump | Unique, nth, exact, entity-escaped and failed-read cases behave as designed, and so do two reads that must agree, a match that never holds still (`unstable`), a match first seen at the timeout, and the package filter (another package's element is never matched; `find.package` names one). The device's own shell and its `uiautomator` were not exercised by these tests |
| 2026-11-10 | That the new tests can fail | 17 single mutations of the gate, the analysis, the device script and the host (the allowance ignored or widened, the baseline unused or replaced by the previous step, the lookup inside the step, no retry, the host error switched off) | Each one turns at least one test red |
| 2026-11-10 | That the tests added for the review's findings can fail | 33 single mutations (failed.json not written; incomplete folders not seen, or seen under another check; summary and compare not leaving them out or not naming them; the first read accepted; no extra read at the timeout; a failed read not breaking the pair; the package filter, its override, its record and its gate check removed; the old marker name; the value and timeout checks relaxed; another scenario accepted by `gate --scenario`; the allowance's source not shown; `run` reusing a used folder; `doctor` requiring or skipping `uiautomator`; the README example without `until_quiet`) | Each one turns at least one test red |

### Live validation, 0.2.0

2026-11-10, on the Android 14 emulator of the 0.1.0 validation (API 34, arm64, a `user` build),
against the debug build of a real app with a backend seeded for the purpose. Every command went
through the launcher. The scenarios were the app's own: its tabs, eight pushed screens, pull to
refresh and three forms, signed out and signed in.

| What | Against | Result |
|---|---|---|
| `run` and `gate`, 13 scenarios in three phases | a clean build; 39 taps that find their target by text or description, 3 `text` steps (one of them signs in through the app), 4 rests with an allowance | PASS, exit 0. 44 gated rest windows: 40 at 0 frames, 4 on a focused field at 10 frames each, inside their allowance of 12. No recorded frame at rest that changed nothing. Every lookup: 2 reads, 4.6 to 4.9 s, the app's package recorded |
| a `quiet` window shorter than the caret's blink | 400 ms on the two steps that end on a focused field | both timed out: the poll takes about as long as the gap between blinks. A fixed wait is what the README prescribes |
| `gate`, faulty build 1 | an animation left running at zero alpha on the tab screens | FAIL, exit 1. Every rest on a tab: 140 to 190 frames, 133 to 166 recorded frames that changed nothing, 44% to 49% of one core. A rest on a form the fault does not reach passed on its allowance with 10 frames |
| `gate`, faulty build 2 | an element redrawn on every frame behind zero alpha, above every screen; four scenarios | FAIL, exit 1. 17 of 17 rest windows fail frames (236 to 306, against 0, or against 12 on the three with an allowance) and CPU; 15 of 17 fail on recorded frames that changed nothing, and the other two lie after the recorder's 180 s limit, which the gate reports as such |
| `gate`, faulty build 3 | an animation clock that runs for ever and draws nothing | FAIL, exit 1, on CPU alone: 65 to 86 ticks (13% to 17% of one core) in 5 of 5 rest windows, and no frame by any count |
| lookup with `nth` | the second of five elements with the same text | found in 2 reads, the second in document order |
| a lookup that cannot succeed, `--runs 2` | a text that is on no screen, 3 s timeout | exit 2 naming the step, the text and the package searched. `failed.json`, `raw/lookup-failed.txt` and the last tree are kept; the second run is not started. `gate` on the folder: FAIL (check `run`), exit 1, and exit 0 with `--advisory`. A rerun into the folder is refused. No recorder left on the device, the app in front |
| a lookup that stops a flow part way | a sign-in of seven lookups whose last one named an element of another screen | exit 2 at that step; the six lookups before it kept their records, and the saved tree showed the screen the flow had reached |
| `doctor --serial` | the emulator | reports `uiautomator` |

Found on the way, and now in the README: a clean app's CPU at rest is not always zero. Three of 80
still rest windows read 6 to 9 ticks in 5 s from background threads (the runtime's profile saver,
a thread pool finishing a request), against a default limit of 10; a focused field read 6 to 9 in
8 of 8 windows, so its allowance should raise the CPU limit as well as the frame limit.

Not validated on a device: `find.package` naming another package and the refusal of a match in
another package (the shell code is covered by the unit tests only); a match that never holds
still; a lookup value containing an XML entity; an Android version other than 14; a physical
device.

The lookup's format facts (the dump is one line of XML, attributes end with
`bounds="[x1,y1][x2,y2]"`, values escaped as described in the README) and the measurements that
shaped the allowance and the baseline (a caret's 10 or 11 frames per 5 s window in 9 of 9
windows; two tree reads producing 1 frame, 9 recorded frames that changed nothing and 9 CPU
ticks) were taken on an Android 14 device before this release; they are not repeated by the unit
tests.

### Validation, 0.3.0

| Date | What | Against | Result |
|---|---|---|---|
| 2026-11-10 | Unit tests | macOS (Python 3.12.8) | 422 tests pass: the 379 of 0.2.0 (the lookup harness, its fixtures and the README-example test adjusted for the new argument and record lines; one recording test given an app that draws, for the rule on what the recording shows while the app presented no frame) and 43 new |
| 2026-11-10 | The lookup's shell code with `scroll` and the on-screen rule, and the display-size reading | The host's `sh` (bash 3.2 in POSIX mode), `dash` and `bash`, with stand-ins for the window-tree dump, `input` and `wm` | Matches off the screen, empty bounds, a centre past an edge, a partly clipped match, an override size, `nth` among matches on the screen, swipes until the element is on the screen, the swipe limit, no swipe after a failed read or while a match waits for its second read, pixels from the display size, an unknown display size, and `wm size` with and without an override behave as designed. The device's own shell, `uiautomator`, `input` and `wm` were not exercised by these tests |
| 2026-11-10 | `proxy --map-host` | Servers on 127.0.0.1, every other connection refused by the test | A CONNECT target and a plain HTTP request named by the mapped host reach the server mapped to, the request passed on as sent; an unmapped target is connected as named; the log carries both |
| 2026-11-10 | That the new tests can fail | 56 single mutations: each `find.scroll` check relaxed; no room for swipes in the default timeout; an explicit timeout overridden; no overrun in the worst case; the scroll not passed or in thousandths; the display not read, or its override ignored on the device or on the host; every match, or an empty one, counted as on the screen; `nth` counting matches off the screen; several on the screen called `not_visible`; `not_visible` never reported; a swipe after a failed read, while a match waits, or past `max`; no check for an unknown display size; a swipe's y from the width; the message's suggestion and swipe count removed; fractions of the physical screen; swipes left out of `run.json`; negative bounds not parsed or not selected; the version not bumped; and for `--map-host` the mapping not applied, case compared, `connected_to` not logged, and each argument check relaxed; and for the rule on a recorded change while the app presented no frame, the rule removed, either count not required, an unknown count taken as zero, the trace count ignored, the rule reaching a `changed_frames_max` allowance, and the note without the `zoom` command; then for its extension to frames that changed nothing, the extension removed or applied whatever the counts, the trace count ignored, an unknown count taken as zero, no note or two notes where both kinds occur, the note without the `zoom` command, and an allowance taking those frames out of the rule | Each one turns at least one test red |

### Live validation, 0.3.0

2026-11-10, on the emulator and against the app of the 0.2.0 validation, its backend seeded for
the purpose.

| What | Against | Result |
|---|---|---|
| `find.scroll` | a row below the fold of a pushed screen | found after 1 swipe (3 reads, 8.0 s); the swipe lies before the step's start mark; gate PASS. The same lookup without `scroll`: exit 2 with `not_found` (the list does not build rows it does not show, so `not_visible` was not produced on this app) |
| the display's override size | `wm size 720x1280` at 320 dpi | `raw/display.txt` and `capture.json` carry 720 x 1280 beside the physical size; bounds, taps and swipes were in the override's coordinates (the flow reached its target after 2 swipes); gate PASS |
| `proxy --map-host` | the app's backend, named by the emulator's alias of the host; 200 ms each way and 400 kbit/s | one scenario: 11 connections named by the alias, each logged with the loopback address it was connected to, and the app worked through it. A batch of 13 scenarios: 103 such connections, PASS |
| a recording while the app presents no frame | 60 runs of a clean build saved under 0.2.0: 200 rest windows on still screens, in 37 of which the display composed a frame | 0.2.0's gate: 6 runs fail on one recorded change in the app's area. 0.3.0's gate on the same files: none fails, 6 notes. A window with two display frames 14 ms apart for a status-bar update: a note |
| the same rule against faults | three faulty builds: an animation at zero alpha on some screens, one above every screen, an animation clock that draws nothing | all still FAIL. The first two on frames, on recorded frames that changed nothing and on CPU in every rest window (their counts are in the hundreds); the third on CPU and on `sections_max` |
| `expect` with `signature` and `sections_max` | baselines taken from the 60 runs, a sixth run, and two faulty builds | the sixth run passes 12 of 12 gated scenarios once a baseline that was never exact is withdrawn (its counts had agreed in five runs while its signature had not). A flash of the whole screen after every touch: 5 steps fail on their signature (`T` became `T E`, `TD` became `TD E`), each with exactly two more composition passes. An animation clock that draws nothing: 300 frame callbacks in a 5 s rest against an approved 0 |
| a consuming project's profiles, 13 scenarios each | a font scale of 1.3; a display of 720 x 1600 at 320 dpi; every connection through the proxy above; 2 cores and less memory | Font scale 1.3: PASS, 13 of 13, one lookup scrolled once. 720 x 1600: 12 of 13; the thirteenth failed only because a caret counted as a visible change, and passes once its rest allows that. The proxy: PASS, 13 of 13. 2 cores and about 2.5 GB (what the emulator gives for `-memory 2048`): PASS, 13 of 13. Before that, on a display 640 dp tall, a lookup that no swipe could satisfy stopped the batch at a form whose layout had lost a row while the keyboard was up: a finding about the app, which is what a failed lookup under a profile is for |

Not validated on a device: `not_visible` (a match in the tree but off the screen); `nth` with
`scroll`; `find.package` naming another package; an Android version other than 14; a physical
device.

### Validation, 0.4.0

| Date | What | Against | Result |
|---|---|---|---|
| 2026-11-10 | Unit tests | macOS (Python 3.12.8) | 465 tests pass: the 422 of 0.3.0 (two adjusted: the doctor's new argument, and a version check moved to the 0.4.0 tests) and 43 new |
| 2026-11-10 | A simulator run end to end | stand-ins for `xcrun` (a device list, `launch`, a recorder that answers SIGINT, ignores it, never starts or exits at once, or exits with an error) and for `ps` on PATH | runs, recordings, CPU readings, the status bar frozen and cleared, the recorder's group stopped (or killed) on every path out of a step including an interrupt, SIGTERM and SIGHUP (a real subprocess), a recorder that stops early, `failed.json`, the gate, `analyze`, `sheet`, `summary`, `compare`, `zoom --step` and `doctor --simulator` behave as designed. No simulator was touched |
| 2026-11-10 | The recording analysis on one step's file | a short lossless video made with ffmpeg: a frame that changed nothing, one that changed only the top bar, one that changed the app's area | counted 1, 1 and 1 |
| 2026-11-10 | That the new tests can fail | 33 single mutations of the first version, then 16 of the fix round (the status bar not frozen, not cleared on an error, or a failed freeze ignored; the classes exempt again, changed pixels counted only in the app's area; an early recorder passed, or its aliveness not observed; no signal handlers, a termination named as an exception; a falling CPU time accepted; short rests accepted; only the recorder signalled, not its group; an empty `--simulator` accepted by `run` or `doctor`; every step analysed from the first step's recording; Android's keys for a simulator). The first 33: the simulator rules not applied, any step kind, `activity` or `until_quiet` true accepted, a hyphen refused in a bundle identifier; the recorder's start not awaited, the recorder not stopped on an error, a kill not recorded, SIGTERM instead of SIGINT; the app not terminated first, any launch output accepted; CPU percent not over the window, a misread CPU time; bars-only frames held against the app, frames that changed nothing passed or allowed by `frames_max`, unknown CPU passed, no note of what was not checked, the recorder's status ignored, a gone process passed; the Android gate on a simulator run, no simulator row in `summary`, `zoom` without `--step`; `doctor` wanting `adb` or ignoring the state; a used folder not refused, no `failed.json`, `--trace` accepted; narrower system bars, the first frame counted, a simulator that is not booted accepted, the version not bumped, a damaged step record crashing the analysis | Each one turns at least one test red |

The facts the simulator code is built on (the launch prints `<bundle id>: <pid>`; the app is a
process on the host whose CPU time `ps -o cputime=` reads; the recorder keeps a frame only when the
display changes and prints `Recording started`; a clean build used 0.00 to 0.03 s of CPU per 5 s
window and recorded 1 frame, a faulty one 1.45 to 1.46 s and, in recordings about 6.8 s long, 408
to 412 frames) were measured by hand on an iOS 26.1 simulator before the code was written.

### Live validation, 0.4.0

2026-11-10, on an iOS 26.1 simulator of an iPhone 16e, against a debug build of an app that draws
its own surface, on its launch screen, and against two faulty builds of it: one redraws an element
behind zero alpha every frame, the other runs an animation clock whose value nothing draws. An
independent reviewer read the first version of the code. Its findings (a small animation classed
as encoder noise and passed; app content inside the fixed system-bar bands passed; a recorder that
stops early with status 0 passed; a recorder left behind by SIGTERM; a falling CPU time passed)
and one found on the simulator (a change of the status-bar clock counted against the app) were
fixed before release. The rows below were run on the released code unless a row says otherwise.

| What | Against | Result |
|---|---|---|
| `doctor --simulator` | the booted simulator | `xcrun`, `xcrun simctl`, the simulator and its state reported ok |
| What is refused | an empty `--simulator` on `run` and on `doctor`; a rest of 500 ms; a `tap` step | exit 2 each, before anything ran |
| A clean build | the launch and rests of 5 s to 65 s: 20 windows over 9 runs, 16 of the windows under the first version | 0.2% to 0.6% of one core in 20 of 20, and no frame after the first in 19. The other window, 65 s long under the first version, recorded 2 frames when the status-bar clock changed, and the gate failed it; the same rest under the released code, which freezes the status bar, recorded none and passes. A recording of a still screen holds exactly one frame, the one the recorder writes when it starts |
| The status bar | `simctl status_bar list` during and after a run | 9:41 during the run; no override after it, and none after SIGTERM, SIGHUP or an interrupt |
| The two faulty builds | three rests of 5 s, each build twice (first version and released code) | FAIL in each of 12 windows, on recorded frames, on frames that changed nothing and on CPU: 299 to 304 frames and 25% to 33% of one core. The animation clock that draws nothing presents every frame on iOS |
| Signals in the middle of a rest | SIGTERM, SIGHUP and SIGINT sent to the tool | exit 143, 129 and 130; no recorder left; `failed.json` names the cause; the gate fails the run |
| A recorder stopped from outside during a rest | SIGINT to the recorder's process | it exits with status 0; the gate fails the recording check, because it was not running when the window ended |
| The app terminated during a rest | `simctl terminate` | the gate fails the process check, the recorded frames and the unreadable CPU |
| The recorder's process group | `ps -o pid,pgid` during a rest | the recorder leads a group of its own |
| `summary`, `compare`, `analyze`, `sheet`, `zoom --step` | saved simulator runs of the clean and a faulty build | `summary` and `compare` show the `sim_*` keys (CPU 0.6% against 32.5%, recorded frames 0 against 301); `analyze` on a copy reproduces the run's result exactly; `sheet` and `zoom --step` write their pictures, and `zoom` without `--step` is refused |
| Android under this version | runs saved by earlier versions (13 scenarios of a clean build, 7 folders of faulty builds, 13 scenarios under another profile), and one full live run on the Android 14 emulator of the 0.3.0 validation | `gate` gives the same verdicts, text and JSON as 0.3.0 on every saved folder, and `analyze` on a saved run gives a result identical to 0.3.0's in every field; the live run of 13 scenarios (lookups, typed text, trace sections, baselines) passes with no violation |

Not validated on a simulator: a screen shape other than one phone's (an iPad, landscape); a
recorder that ignores SIGINT or writes no file (stand-ins only); an allowance for a caret (no input
reaches a text field); another Xcode or iOS version.

### Validation, 0.5.0

| Date | What | Against | Result |
|---|---|---|---|
| 2026-11-11 | Unit tests | macOS (Python 3.12.8) | 554 tests pass; none runs the real `xcodebuild`, `xcrun` or `ps`, or writes outside its temporary folder (stand-ins first on PATH, checked before each test; `XDG_CACHE_HOME` and `HOME` in the test's folder) |
| 2026-11-11 | Runs with input end to end | stand-ins on PATH for `xcodebuild` (`-version`; a build that writes an `.xctestrun` or fails; a test run that serves the agent's protocol, refuses a tap, refuses the token, reports the app in the background, never answers, exits first, or ignores `quit` and SIGTERM), `xcrun` and `ps` | the build into the cache and its reuse, the agent's start, every command with the token, the order of commands, the signature per step, the foreground check, and the agent and its group gone, the runner terminated and uninstalled and the status bar cleared after a normal end, an error, an interrupt, SIGTERM and SIGHUP behave as designed. No simulator was touched |
| 2026-11-11 | The agent's Swift compiles | `xcodebuild build-for-testing` of `ios-agent/` for a generic iOS Simulator (Xcode 26.3), the derived data under a scratch folder; again after the review's changes (issues recorded per command, a 5 s deadline per connection, the keyed hash of each command's nonce with CryptoKit) | built, no warning in the agent's source; not run |
| 2026-11-11 | The excuse for recorded frames at rest | lossless recordings made by the tests at the full display size: a still picture refined along its edges, a 1 pt grey hairline that grows twice (largest change 16), a 20 pt spinner and a caret; and the two real recordings it was made for (largest change 16 and 12 on the largest colour channel, 38 and 8 pixels changed by 8 levels or more) | the refinement and the two real recordings are excused (no changed pixel on a flat area); the hairline is held (4 or 5 changed pixels on a flat area), as are the spinner and the caret. A limit on the number of changed pixels alone could not separate them: the hairline changes fewer pixels (6 or 7) than the real refinement |
| 2026-11-11 | `tls-front` | the `openssl` on PATH (OpenSSL 3.6.3), Python's `ssl` as the client | the CA carries pathlen:0 and a critical name constraint permitting DNS:localhost, and a client that trusts it reaches the backend; its key is gone after signing; an unmarked folder that is not empty, or that others can open, is refused and left as it was; the server's minimum version is TLS 1.2 (a setting read back, no older client tried) |
| 2026-11-11 | That the new tests can fail | 120 mutants, one exact replacement each in a fresh copy of the plugin, then the simulator, input, store, review and tls-front test files: 0.5.0's input agent and launch environment, the log store and its rules, `tls-front`, the review's sixteen, and at least one per ruling of the review (the agent's issues and status, the recording's span, the colour channel, the pixel and flat-area limits, unknown signpost names, the second read, the clean-up's shield and phases, the per-connection deadline, the nonce and its proof, the cache's marker and lock, the CA's constraints and key, the derived data and result bundle, the launch environment's single source, the swipe limit, the frame sources) | 118 turned a test red on the first pass; the 2 that survived (the agent's issue record removed, no shield around the status bar's clear) got tests, and they and the 15 mutants of the refinement code changed after the pass were run again: all red. Each one turns at least one test red. The agent's Swift is held by tests of its source only |

Not validated by these tests: the agent itself on a simulator (its socket, its token check, its
nonce proofs, its 5 s deadline per connection, its idle end, its gestures, an issue recorded while
a command is handled and the test run's status after it); `SIMCTL_CHILD_` reaching the app; the log
store's second read on a simulator (that other processes log after the last step); `-derivedDataPath`
and `-resultBundlePath` on a real test run; the cache's lock under two real concurrent builds;
`tls-front` reached from a simulator with its CA installed.

### Live validation, 0.5.0

2026-11-11, on an iOS 26.1 simulator of an iPhone 16e with Xcode 26.3, against a debug build of an
app that draws its own surface through Metal and emits signposts when asked to, reaching a backend
on the same machine through `tls-front`; and against two faulty builds of it (one redraws an
element behind zero alpha every frame, the other runs an animation clock whose value nothing
draws). An independent reviewer read the whole change. Its findings (a gesture XCTest could not
perform reported as done; the excuse for encoder frames too wide; a signpost name that never
matches passing as zero; the store proven read only for the launch; a signal during the clean-up;
a slow client holding the agent; replies not proven to come from the agent; a failed agent build
reused; `tls-front` taking over a folder that was not its own) were fixed before release.

One limit was then changed on what the simulator showed. The excuse's limit on changed pixels was
200, and the 17 rest windows with encoder frames in these recordings changed 28 to 193 pixels, most
on the screen with the most text: the next busier screen would have failed at rest with the app
idle. The limit is now 2000. The suite and the 15 mutants of that code were run again on the
change, and all 15 turned a test red.

The rows below were run with the released code. Where a row says "captured earlier", the
recordings were made by the code as it stood before that change and one other (a reported measure
that is null where it was a placeholder), neither of which touches a capture, and they were
analysed and gated again with the released code.

| What | Against | Result |
|---|---|---|
| The input agent | built by the tool from the plugin's source into its cache, then 65 taps and 5 swipes in 20 runs (captured earlier) | built once and reused by every run; a tap takes 342 to 453 ms and a swipe of 600 ms 1114 to 1186 ms; all 70 replies carry the keyed hash of their command's nonce; the agent's test run ended with status 0 after `quit` in 20 of 20, and its derived data and result bundle were removed |
| Scenarios with input | five scenarios (a launch, four tabs each opened twice, a pushed screen and one pushed from it, a modal screen, a pull that springs back), five runs each, through a project's gate script in blocking mode with eleven signatures as baselines (captured earlier) | 25 of 25 runs pass. Each of the 14 input steps had one signature in its five runs (`T` on ten, `E` on the pull, `TD` on three first openings); a batch of the same size captured by a draft of this version gave the same 14 signatures in its five runs when analysed again |
| The three frame counts of a transition | the 70 input steps of those runs, the app's own signposts switched on | the store's drawables equal the app's own count of its draws in 70 of 70; the recording holds as many frames in 29, one more in 26 and 3 to 14 more in 15, never fewer. The app's layout and scheduling counts move by one or two between runs of one transition, so they suit a rest (zero) better than a transition |
| Rest steps of a clean build | the 95 rest windows of those runs | the store's counts are known in every run (the second read found 106 to 1017 events after the last step); 0 drawables, 0 commits, 0 updates and 0 of each named signpost in 95 of 95; 0.0% to 0.4% of one core; in 7 windows the recording held one frame of the encoder's, each reported as a note |
| Encoder frames at rest | the 17 rest windows that recorded any, over all recordings of this validation (five screens) | one or two frames per window; the largest change 14 to 17 levels; 28 to 193 pixels changed by 8 levels or more, most on the screen with the most text; none on a flat area |
| Two faulty builds | five scenarios each, through the gate script in blocking mode (captured earlier) | the script exits 1 and all five scenarios fail: each of the 19 rests fails on the store's count (300 to 302 drawables in 5 s), on display updates, on recorded frames, on CPU (11% to 23% of one core) and on the app's own signposts, a screen pushed from a pushed screen included |
| A gesture that leaves the app's display updates running | found in the clean build: a pull on a screen that cannot scroll, then rests of 5 s and 20 s | 300 and 1201 display updates, with no drawable and no commit; the 20 s rest fails on `rest-updates` alone (no recorded frame, 0.9% of one core, which is under the CPU limit); none after the screen is left |
| Signposts that cannot be counted | the app launched without its tracing; a listed name the app never emits (that run captured earlier) | both fail `signposts` as not counted, each with its own message: no event of the subsystem in the run, and a name that occurs nowhere in the run |
| The foreground check | another app launched in front during a scenario with input, and during one without | with input, `foreground` fails for each step that ended with the app in the background, and the step's signature fails. Without input there is no agent to ask, and the rest fails on its recording and its counts instead |
| The agent's protocol | during a run: a tap and a `quit` with a wrong token, a tap with none; in another run, a client that connects and sends nothing | each command is refused with an error that carries no proof and nothing is performed (the rest it arrived in drew no frame, and the agent went on serving); the silent client is answered with an error after 5.0 s; both runs pass |
| Signals | SIGTERM and SIGHUP in the middle of a run with the agent; SIGKILL | exit 143 and 129 within about a second, `failed.json` names the signal, and no recorder, agent, runner app, status-bar override, derived data or result bundle is left. After SIGKILL the recorder, the agent's `xcodebuild`, the runner app and the override are left, as documented; the agent ended by itself 135 s later, at its idle limit, and the documented commands restored the simulator. A SIGTERM sent while the tool was cleaning up after the last step was tried on the code before the last change: exit 143 and nothing left |
| `launch_env` | two variables in a scenario; a stray `SIMCTL_CHILD_` variable in the tool's own environment (tried on the code before the last change) | the app reached the backend the first names and emitted the signposts the second switches on, in every run above; the stray one is not passed and is named in a note |
| What is refused on a simulator | `find`, `text`, `key`, a swipe of 70 s | exit 2 each, before anything ran and before the run's folder was made |
| `tls-front` | a plain backend on loopback, started through a project's script; the simulator told to trust the CA | every run above reached the backend at `https://localhost:<port>` with no change to the app's transport security; the CA carries the path length and the name constraint, and its key is not kept; a state folder made by an earlier draft, without the marker file, is refused |
| Xcode's derived data | the number of `Agent-*` folders in Xcode's default derived data before and after the runs above | unchanged; an earlier draft left one folder of about 1.3 MB there per run |
| Android under this version | eight folders of runs saved by earlier versions, two that pass and six that fail (faulty builds, a small-screen profile), gated by 0.4.0 and by this version; one full live run of 13 scenarios on the Android 14 emulator of the earlier validations (captured earlier) | `gate` exits and prints the same under both versions on all eight; its JSON differs in the version and in one new key, `limits.rest_updates_max`, which only a simulator run uses; the live run passes 13 of 13 |

Not validated on a simulator: an issue recorded by XCTest during a gesture (the path that reports it is held by
unit tests and by tests of the agent's source; none could be provoked); the proof on a reply against another
listener on the port; two runs at once, and the build lock under two real builds; an app drawn by UIKit through
the tool (read by hand once: no drawables, 62 commits against 110 recorded frames on a tap, since the render
server animates without the app, so its count at rest is its commits); a software keyboard (the simulator had a
hardware keyboard connected) and any typed text; an iPad; landscape; another Xcode or iOS version.

### Validation, 0.6.0

| Date | What | Against | Result |
|---|---|---|---|
| 2026-11-11 | Unit tests | macOS (Python 3.12.8), `python3 -B -m unittest discover -s plugins/ui-probe/tests -q` | 633 tests pass (the run printed `Ran 633 tests` and `OK`); none runs the real `xcodebuild`, `xcrun` or `ps` (the stand-ins of 0.5.0, whose agent now also serves `find`, `type` and `labels` when started with `TEST_RUNNER_UI_PROBE_AGENT_LOOKUPS=1`) |
| 2026-11-11 | The lookup rule | an agent object in the test process with a fake clock: found after two reads that agree, a read with no match first, a match that moves, a match first seen at the timeout, two matches without `nth`, `nth` counted from one in the order of the tree, a match off the screen, a match on the screen that the hit test says a tap would not reach (with and without the `under` hint, and `moved` and `not_asked` told apart), a match with a `visible` part, scrolling that reaches it and that runs out, more matches than listed, a `find` or a scroll swipe the agent refused | each behaves as Android's `find_el` does for the same reads, with the statuses and messages built as `lookup_failure` builds them |
| 2026-11-11 | `rehearse` and `run --rehearsal` end to end | the stand-ins on PATH, a binary `Info.plist` and an executable written by the test | `rehearsal.json`'s content (no typed text in any file of the folder, the agent's log included: the stand-in agent writes XCTest's typing lines; the result bundle and derived data removed when the scenario types, whatever the test run's status), the commands in order, the agent's mode at the first `ping` in both commands, the app launched before the agent starts in a rehearsal and its settle counted from the launch, the app terminated and its process checked after a rehearsal that succeeded, one that failed and one whose agent did not start, each refusal of `run` (digest, udid, a step not covered, an app that changed or whose identity cannot be read, too old, a screen of another size), the rehearsal's swipes sent before the step's recorder starts and then the tap, a replayed swipe the agent refused gated as `input`, `resolved` in `run.json`, the gate's note, and rehearsal folders passed over by `gate` and `summary` behave as designed. No simulator was touched |
| 2026-11-11 | That the new tests can fail | 91 mutants (and four more on the encoder-frame limit, all red), one exact replacement each in a copy of the plugin, then `test_rehearsal_060.py` (or the class named for the mutant): the lookup rule (on the screen, covered, two reads, exactly one, `nth`, the grace read, the swipe limit, the listed matches), the agent's mode and its environment, the screen size, the replay and its order, the gate's check and note, `resolved` in the analysis, each check of `run --rehearsal`, the app's termination and the process check, the typed text kept out of `rehearsal.json` and `lookup-failed.txt`, the two validation modes, `find.package` and `find_settle_ms`, the refusals of both commands, the rest's cap, the typed count, `lookup-failed.txt` and the `labels` read, the rehearsal folder's marker, its skipping, the gate's notes and the refusals of each folder kind, and the rehearsal's order (the app launched before the agent starts, the settle not cut short, the app terminated when the agent fails to start), and fix round 1 (the hit-test verdict, the match named first, `visible` recorded, the agent log's rewrite, the result bundle's removal, element values left out, the note in `failed.json`, the simulator's wording for a value that cannot be matched, `moved` and `under` in the message), and `allow.unchanged_frames_max` (applied on each platform, its note on each, its validation, its place among the allowances, not reached by `frames_max`), and the review's fixes (a rehearsal marker in an ancestor, runs at any depth, no silent skip of a run inside a rehearsal folder and the rehearsal's own `raw/` excused, the full rest, a replayed tap that changed nothing and its `-` signature, an unusable recording, the bundle digest with its paths, its links not followed and an unreadable bundle, both waits bounded, both idle limits, the refusal before the folder, the app in front after the launch and after each step) | 89 turned a test red; two could not be applied as written (one anchor occurred twice, one not at all) and, applied again with a corrected anchor, turned a test red too; one mutant turned another test red while its own test skipped itself; with the test's guard fixed, its own test went red too |

Not validated by these tests, and validated live below: the agent's `find`, `type` and `labels` on a
simulator; what XCTest's hit test says on real screens; a real bundle read through `xcrun simctl
get_app_container`; a replayed tap landing on the element it was found for.

### Live validation, 0.6.0

2026-11-11, on an iOS 26.1 simulator of an iPhone 16e with Xcode 26.3, against a debug build of an
app that draws its own surface through Metal and emits signposts when asked to, reaching a backend
on the same machine through `tls-front`; and against a faulty build of it that redraws an element
behind zero alpha every frame. An independent reviewer read the whole change. Its nine findings (a
run written inside a rehearsal folder vanished from `gate`; a rehearsal that capped its rests could
look a target up on a screen still loading; a sentence that said signatures verified every replayed
tap; an app identity that covered only the versions and the executable; an idle limit that left out
the waits around scroll swipes; a refusal that left a folder behind; a rehearsal that never checked
the app was in front; a `truncated` flag set when nothing was left out; a typed character in the
text of an XCTest issue) were fixed before release.

Three things were changed on what the simulator showed.

- **What a tap would reach.** A first rule read it from the tree (an element later in the tree
  over the target's centre). It called a Back button covered by text that had scrolled under the top
  bar, and stopped a full run. A second (the part of the frame that the ancestors' frames leave) was
  wrong on a screen whose framework reports some frames cut under a floating bar. XCTest's own hit
  test was right in 19 of 20 cases on two screens (a top bar over scrolled content, a floating tab
  bar, elements scrolled out); its one error was a button cut by the lower edge of its scrolling
  area, whose centre lay outside the part on the display. The agent now asks the hit test, and moves
  the tap point into the part that the scrolling area leaves.
- **The order of the app and the agent.** When the agent first met an earlier process of the app,
  its first read of the tree after a relaunch waited 63 s (three reproductions; a new application
  object per read changed nothing). A rehearsal launches the app first and starts the agent after.
- **How many frames the encoder may add to a still screen.** The excuse allowed two. In the
  certifying batch two rest windows of a clean build held three, with every count zero, and failed
  their runs. Across 523 still rest windows in four batches, 52 held such frames: 32 one, 18 two,
  2 three (12 to 17 levels, 19 to 190 pixels, none in a flat area). The limit is now 6. The suite
  and four mutants of that rule were run on the change, and all four turned a test red.

The rows below were run with the released code. Where a row says "captured earlier", the
recordings were made by the code as it stood before that last change, which is read only when a
run is gated, and they were gated again with the released code.

| What | Against | Result |
|---|---|---|
| Lookups by label in a rehearsal | ten rehearsals in one batch: nine scenarios and a sign-in (captured earlier) | 47 taps found by text or description, each after 2 or 3 reads of the tree that agreed, in 415 to 2512 ms; 4 scroll swipes brought a target clear of a bar; a rehearsal took 34 to 88 s, its rests at full length |
| Typing | the sign-in: a phone number and a code, 10 and 6 characters, into the field with keyboard focus, with the software keyboard up (captured earlier) | both accepted by the app (the next lookup only succeeds for a signed-in user); 1.4 and 1.7 s a character. The kept folder holds neither text: `rehearsal.json` gives the two counts, the 16 typing lines of the agent's log are rewritten, and the result bundle and derived data are gone |
| What a rehearsal leaves in the app | the app relaunched after the rehearsal's process was thrown away, signed in (captured earlier) | 199 of 200 rest windows without an allowance are zero on every count (drawables, commits, display updates, three signposts), 0.0% to 1.2% of one core; the one that is not is the app's own doing (below). In a process that was read, the same app keeps a display link running at rest (measured for 0.5.0) |
| Replayed positions | 55 runs of eleven scenarios in blocking mode, 26 signatures as baselines (captured earlier) | 185 taps and 20 scroll swipes replayed by position, a tap in 346 to 478 ms; none failed `input`; the approved signature held on 130 of 130 steps, and 80 further steps (first openings, classed `TD` or varying) changed the screen every time |
| The gate on those runs | the same 55 runs, gated with the released code | 54 pass. One fails `rest-updates` alone: after a pull on one list the app was woken for 123 display updates in the 5 s rest, with no frame drawn (the pull's own window held 211, against 129 to 138 in the 19 other pulls of that scenario in two batches). That is the app, found by this run. Gated with the limit of two, two more failed on three encoder frames each |
| A rest that draws by design | a focused text field: one rest in five runs (captured earlier), and one run of the same rest without its allowance | 30 frames drawn and 70 display updates in every window, 30 to 32 recorded, 13 to 15 of them a visible change and 15 to 19 none, 1.8% to 2.4% of one core: passes on `frames_max`, `changed_frames_max`, `unchanged_frames_max`, `updates_max` and `cpu_pct_max` set just above. Without the allowance it fails on each of them |
| A faulty build through the same replay | the build that redraws every frame, eleven scenarios, one run each, rehearsed and signed in the same way | every run fails; 41 of 41 rest steps fail (300 to 303 drawables and display updates in 5 s, 11% to 27% of one core), the focused-field rest on 301 draws against its 34; no signature and no `input` failure, so every replayed tap landed |
| What `run` refuses | a scenario with `find` and no rehearsal; the rehearsal of another scenario; a rehearsal older than `--rehearsal-max-age-s` | exit 2 each, with the reason, before a run folder exists |
| The app's identity | a rehearsal, then another build of the app installed, then the first build installed again | with the other build `run` refuses the rehearsal (exit 2, no folder) and names the sha256 of the executable then and now; with the first build back it accepts the rehearsal again, so installing a build again leaves its bundle digest as it was |
| A lookup that cannot succeed | a label that is on no screen; a label that is on the screen under a floating bar | the rehearsal stops with exit 2: `not_found` after 13 reads, with `lookup-failed.txt` listing what the screen held, and the app no longer running; and "a tap would not reach it", naming as a hint what lies over the target |
| Signals | SIGTERM in the middle of a rehearsal | exit 143, `failed.json` names the signal, and no agent, runner app, status-bar override or app process is left |
| Earlier versions' runs | ten folders of runs saved by 0.2.0, 0.3.0 and 0.5.0, six from Android and four from a simulator, four that pass and six that fail, gated by 0.5.0 and by this version | `gate` prints the same and exits the same on all ten; its JSON differs in the version only |

Not validated on a simulator: an app that moved to the background during a rehearsal (held by unit
tests; not provoked); `nth`; a target that needs more than one scroll swipe; two rehearsals at once; a
simulator with a hardware keyboard connected; an iPad; landscape; another Xcode or iOS version.

## Pinned external tool (optional)

`scripts/install-trace-processor.sh` installs the Perfetto trace processor **v59.1** from
`commondatastorage.googleapis.com/perfetto-luci-artifacts/v59.1/<platform>/trace_processor_shell`
and verifies SHA-256. Checksums come from the vendor's own launcher manifest for that release:

| Platform | SHA-256 |
|---|---|
| mac-arm64 | `8488edbefadfc4be152cda1d42d9bfcede8b47638295eb85eb3bad8cd148fdf4` |
| mac-amd64 | `df38039845244c353c399994748599345cf08f740491b5832e7feb9a4dbcc476` |
| linux-amd64 | `2434129a3af4dc9e2034a476f7b76447b490975213e301769310d3bd0d5d6458` |
| linux-arm64 | `a08e4836171c87be3ba5834eef3489aa8803476d35f292ed7feaccae252747d0` |

Only the mac-arm64 build was downloaded, verified and run for this release; the other three
checksums are the manifest's and were not exercised. The probe does not call the trace processor.

## Method sources

- The three-state verdict of `compare` (different, same, unknown) follows the practice of
  performance-regression tooling that refuses to call an inconclusive comparison a pass.
- Mann-Whitney U: the exact distribution of U (counted by the standard recurrence) when there are
  no ties and at most 12 values a side; otherwise a normal approximation with tie and continuity
  correction. Cliff's delta with the commonly used bands (below 0.147 negligible, 0.474 and
  above large). The band values were not re-fetched for this release.
- The burst classes and the detour ratio are this plugin's own. The detour threshold rests on two
  observed cases in one app.
