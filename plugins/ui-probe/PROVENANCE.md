# Provenance — ui-probe

## Origin

All code in `scripts/` was written for this plugin. No upstream code or text is vendored, and
nothing is fetched at run time.

## Formats the code reads, and where they are defined

| Format | Used by | Defined in (upstream) |
|---|---|---|
| `dumpsys gfxinfo <package> framestats` text | `ui_probe.parse_gfx` | Android platform, `frameworks/base/libs/hwui/` (JankTracker, FrameInfo); column names are read from the header row at run time |
| `dumpsys SurfaceFlinger --timestats -dump` text | `ui_probe.parse_timestats` | Android platform, `frameworks/native/services/surfaceflinger/TimeStats/`; readable by the shell user on a `user` build (checked on API 34). Android 11 (API 30, an emulator) prints no `uid = ` line: a record starts at `layerName = ` (format read from a kept capture of 0.5.0; the test fixture is written anew under a neutral package). One layer can have several `uid = ` blocks (a record per refresh-rate and render-rate bucket, read from the platform source, not yet observed on a device); the parser sums them |
| `/proc/<pid>/stat` and `/proc/<pid>/task/<tid>/stat` | `ui_probe.parse_cpu`, `ui_probe.parse_thread_cpu` | Linux `proc(5)`: fields 14 and 15 (utime, stime) in clock ticks. The process file's values include threads that have exited; the task files give the live threads |
| Perfetto trace protobuf | `pftrace.py` | Perfetto `protos/perfetto/trace/`: `trace_packet.proto` (ftrace_events = 1, process_tree = 2, timestamp = 8, trace_stats = 35, frame_timeline_event = 76), `android/frame_timeline_event.proto`, `ps/process_tree.proto`, `ftrace/ftrace_event_bundle.proto` (event = 2, lost_events = 3), `ftrace/ftrace_event.proto`, `ftrace/ftrace.proto` (print) |
| Ftrace event times, the frame-timeline packet count, and the ftrace data source's statistics | `pftrace.read_atrace` (`events_first_ts`, `events_last_ts`), `pftrace.read_trace` (`frametimeline_packets`, `ftrace_stats`) | `ftrace/ftrace_event.proto` (timestamp = 1); `ftrace/ftrace_stats.proto`: `TracePacket.ftrace_stats` = 34, `FtraceStats` (phase = 1: 1 start of trace, 2 end of trace; cpu_stats = 2), `FtraceCpuStats` (now_ts = 7, a double in seconds). In a kept trace from an Android 11 emulator the 186 event bundles and both `FtraceStats` packets carry no packet timestamp; each `FtraceStats` holds four per-CPU `now_ts`, 38.192 s at the start and 101.102 s at the end, on the clock of the events and the marks (perfetto printed 38.122 when it connected and 101.102 when it stopped) |
| perfetto's own output lines (`[<seconds>] ... Connected to the Perfetto traced service`, `... disabling tracing`) | `ui_probe.tracer_session` | perfetto's command-line client; the stamp is the boot clock (the `Connected` line of a kept API 30 run read 38.122, its trace's first clock snapshot 38.1226) |
| HTTP/1.x request messages | `httppipe.RequestReader` | RFC 9112 (request line, field lines, `Content-Length`, chunked transfer coding with extensions and trailers, message body length); RFC 9113 §3.4 for the HTTP/2 connection preface; RFC 9110 §7.8 for `Connection: upgrade`. Written from the RFCs; nothing vendored |
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

### Validation, 0.7.0

| Date | What | Against | Result |
|---|---|---|---|
| 2026-11-11 | Unit tests | macOS (Python 3.12), `python3 -B -m unittest discover -s plugins/ui-probe/tests -q` | 781 tests pass (the run printed `Ran 781 tests` and `OK`; 775 before the border rule below); none touches a device, a simulator or the network beyond 127.0.0.1 |
| 2026-11-11 | The request reader | `test_requests_070.py`: every split point (whole, byte by byte, cut in two everywhere) of a request with a query, a `Content-Length` body that looks like a request, chunked with extensions and trailers, pipelined requests; a head over 64 KiB, an HTTP/2 preface, an upgrade and a CONNECT, binary garbage, malformed framing, a path over 300 characters, a reader defect; and the review's differential fuzz (4000 generated streams of pipelined requests with bodies, fed whole, in random pieces and byte by byte), its expectation changed only to the shape of each path | as designed; 0 mismatches in the fuzz; no header value, query string, body or non-word path segment in any event |
| 2026-11-11 | Both fronts end to end | loopback sockets only (`http-front`; `tls-front` with the `openssl` on PATH): pipelined requests sent in odd pieces, a client that sends half a head and waits, a backend that streams; a backend that refuses; a declined upgrade on a kept connection; a real `http-front` process started and stopped | the bytes arrive unchanged in both directions, half a head reaches the backend at once and its request line comes when the head completes, a streamed answer reaches the client piece by piece, the log is 0600 (an existing 0644 file included) and holds no header value, query string or body; the refusals, the opaque connection and the stopped front each make the counts unknown with their reason |
| 2026-11-11 | `/proc/uptime` and the clock mapping | a faked `adb` on PATH, this machine's clock replaced in the test; probe groups built in the test | the read with the shortest round trip is kept, no usable read is unknown with its reason, the line through the two groups follows a slow drift, offsets 4 s apart, a 0.6 s round trip and a mapped run outside the script's bracket are each unknown with the times |
| 2026-11-11 | The device script | 0.6.0's `build_device_script` (`26887f1`) for two scenarios, digests stored in the test | byte for byte the same |
| 2026-11-11 | `run --requests` on Android | `test_run_and_cli`'s fake adb: `/proc/uptime` answers this machine's clock less a base, the fake script waits a second and writes the scenario's marks compressed into it, and appends to the log | three probes before and three after the script, counts per step, the front at the start and the end, the gate's `rest-requests` naming the path, `summary` |
| 2026-11-11 | `run --requests` on the simulator | the stand-ins of 0.4.0, a thread writing a request every 20 ms | counts per step, known, every request of the run in a step or between steps |
| 2026-11-11 | Runs of earlier versions | the gate of 0.6.0 as released (`5b627cb`) against this version's, `gate --json`, on 846 saved captures of 0.1.0 to 0.6.0 (381 on the iOS Simulator), 451 folders | text output, exit status and JSON equal in every folder, apart from `limits.rest_requests_max`. The 42 live captures of this version in the same tree (29 folders, taken with `--requests`) gate the same under this version's previous snapshot and this one; none of them holds `periodic_requests` |
| 2026-11-11 | Android 12 and later unchanged | the review's "api33" capture (a frame timeline whose stamped packets stop at 113 s while the ftrace events run to 119 s) and "api34-noftl" capture (API 34, no frame-timeline packet, `FtraceStats` for both phases), analysed and gated by 0.6.0 (`26887f1`) and by this version; the latter also with no API level in the capture | the same covered steps, section counts, frame-timeline counts, violation and notes (api34-noftl: no step covered, FAIL, under both) |
| 2026-11-11 | `FtraceStats` that are not bounds | the review's three cases: a start `now_ts` of 0, an end `now_ts` of 0 on one CPU, two start/end pairs | each leaves the events' own span; no step the events do not cover is covered |
| 2026-11-11 | Fronts that stop, restart, share a log or no longer accept | real `http-front` processes on 127.0.0.1 (and `tls-front` in this process): SIGTERM, SIGINT and SIGHUP with a keep-alive connection held; a front killed and started again on its log; a second front on the same log; a front whose server closed while its process lived; the end-of-run probe against both fronts | each front stopped within 1.5 s with a `stopping` and a `closed` line, the connection closed; the run unknown with its reason in every case; the second front refused (exit 2) before listening; the probe left one `closed` line with no request on `http-front` and nothing on `tls-front`, and a later run counted as without it |
| 2026-11-11 | A front under `nohup`, and a stale log | real `http-front` processes: one started with SIGHUP ignored (as `nohup` starts it), one without; a log whose last `listening` line names a front writing another log, one started without `--log`, and one whose relative `--log` names the same file from its own folder | the first survives SIGHUP, serves on, writes no `stopping` line and stops on SIGTERM; the second stops on SIGHUP; the fronts of another log or none are no obstacle; the same file named relatively is refused |
| 2026-11-11 | `launch_requests_max` | runs built in the test on Android and on the simulator, and a capture whose kept scenario holds the key | over the budget fails `requests` on `launch` naming the paths, at the budget passes, unknown fails with the reason, no request log fails "not counted", no key means no launch check; validation refuses -1, 1.5, true, "2" and null |
| 2026-11-11 | `gate.periodic_requests` | runs built in the test on Android and on the simulator, each with a launch, a rest and a tap, and a capture whose kept scenario holds the map | a periodic request passes a rest, a tap with `expect.requests_max: 0` and a launch with `launch_requests_max: 0`, each with one note; what remains is held on each kind of step, the failure naming every path, the held number and what was set aside; a step that sends the request itself has one of its own set aside; unknown counts still fail; a cut `by_path` fails as unknown wherever a limit or budget applies (and, without the map, is judged on its count as before); a step with no limit and no budget gets no note; with the step's `allow.requests_max` both apply; run.json and summary keep the raw counts; validation refuses each malformed key and value and the key's earlier name in this unreleased version; `limits` shows the map only when present |
| 2026-11-11 | Every request in one step | requests placed at every kind of border (launch and rest, two rests, rest and tap both ways, a gap after a tap, both margins) with an uncertainty of 0.19 s; and a tap of 0.3 s between two rests (the review's "short-tap") | each counted once; the steps plus `between_steps` plus `in_margins` equal the requests kept; the windows are disjoint, and the short tap takes the zone on both sides (rewritten for the border rule below) |
| 2026-11-11 | A rest borders an input step: the input step takes the zone | a certifying run of the whole gate (Android 14 emulator, this machine under a load of 16 to 18): with an uncertainty of 0.0617 s two rests failed `rest-requests`, each holding the next tap's own request, which mapped 10 to 26 ms after the tap's start mark; on the API 34 lab emulator `/system/bin/input` is `cmd input "$@"` and `input tap 1 1` returned in 0.01 s (3 of 3), where an Android 11 emulator takes 0.43 to 0.46 s. `test_borders_070.py`: that case rebuilt from its numbers; both orders; the cap at half a rest with two steps around a 0.3 s rest; two rests and two input steps; a lookup that scrolls; the first and the last rest; u = 0 | the taps hold their own requests and the rests hold 0 (the gate passes them; the same rest holding the request fails, as in the run); the windows are disjoint and meet in a short rest's middle; the marks decide between two rests and two input steps; a first or last rest is widened outward. Re-analysed copies of the three Android 0.7.0 captures with request logs: 87 captures (13 of the certifying run, 61 of a run of request scenarios, 13 of a third run), copied without their `frames/` and `sheets/` (the analysis writes them again) and analysed again: of 816 step records exactly four changed, all in the certifying run's scenario of rests between taps on tabs, run-01 (a rest's count 1 to 0 and the next tap's 0 to 1, twice); nothing else in any `run.json` differs. Gate: 82 of 87 pass before, 83 after; that run went from FAIL to PASS, and the four failures of the third run (a session request at rest, in scenarios with no `periodic_requests`) are the same text before and after. The 30 captures of the run of request scenarios whose kept scenario holds `periodic_requests` under its earlier, unreleased name were analysed and gated, before and after, with `--scenario` naming the same scenario with the key renamed `periodic_requests` |
| 2026-11-11 | Sections and per-layer counts on Android 11 | a kept 0.5.0 run on an API 30 emulator, read in place (nothing written there): the trace through `pftrace` and `_analyze_trace`, the `--timestats` files through `parse_timestats` | the trace holds `FtraceStats` for both phases with `now_ts` 38.192 and 101.102 s, which cover every step (launch from 42.15 s, the last step to 100.73 s); the ftrace events alone run from 42.17 to 94.68 s. Frame timeline reported as unavailable on API 30. The app's per-layer frames are read (4 at launch, 37 at the end), where 0.5.0 read none. The review's "api30-died" capture (events to 111 s, no `FtraceStats`) leaves idle2 uncovered and fails as 0.6.0 did |
| 2026-11-11 | That the tests can fail | Fix round 1: 42 mutants, one exact replacement each in a copy of the plugin, each removing one new rule (a refused backend, the front running at the start and the end, the opaque connections and where they are followed from, a record that cannot be written or is missing, the clock's drift, uncertainty and bracket limits, a rest's widening and the note, the probes before and after, the line through them, the shortest round trip, the device script, the section span with and without a frame timeline and `FtraceStats`, the layer names, the proof inside the run, a rest's tail, the gap's message and the between-steps note, the log's and the record's mode, the path's shape, a restart in the margins, `compare` of nulls), then `test_requests_070`, `test_requests_fix1_070`, `test_android11_070`, `test_compositor_stats` and `test_pftrace`. Before that round, 63 mutants of the first version (some on code since removed) | 42 turned a test red. The border rule (the input step takes the zone): 14 more (the rule reverted to the rest taking the zone, red through the rebuilt certifying case alone; each side's zone and its border, the cap at half a rest and at half of which neighbour, two input steps and two rests left to their marks, the first and the last rest widened outward, the scroll mark, the note's wording), all 14 red through their own tests on the first pass. `periodic_requests`: 18 (the gate key accepted, the key's form, the value, an empty map, the cap at the named number, setting aside at all, the cut computed and only with a map, the cut on a rest and on a budget, the held number on a rest and on a budget, the note only where a limit applies, never on a failure, at all, the failure's set-aside text and `set_aside`, reading the map from the limits), all 18 red through their own tests on the first pass (the allowance for rests it replaces had 10). `launch_requests_max`: 5 more (its validation, its place among the scenario keys, the Android launch check, the simulator launch check, the budget's value), all 5 red through their own tests on the first pass. Fix round 3: 7 more (SIGHUP kept ignored, SIGHUP still handled, each side's cap at half a step, the owner check's same-file condition in two forms, a relative `--log` read against the other front's folder), all 7 red through their own tests on the first pass. Fix round 2: 27 more (G1 to G8: the restart and pid rules, the `stopping` line and its rule, the end-of-run probe and its rule, the stop's abort, signals and time limit, the owner check and its two conditions, the `FtraceStats` bound and once rules, the 0.2 s limit, the disjoint windows, the lower-case segment rule, the API-level condition, the `compare` row; and round 1's rules whose code changed: a rest's tail, the end-of-trace count, the session branch, the front gone at the end), all 27 red; a first pass of round 2 left five alive (the stop's abort and time limit, the owner check's own pid, the window that shrinks next to a rest, and a guard against counting twice that the disjoint windows made redundant): tests were added for the first four and the guard was removed. Round 1's: a first pass left two alive and hung two (a test that left a connection open waited for it on failure; fixed): following from the current front's `listening` line had no test (one added: a garbage line and an open connection of an earlier front process), and ending the last step's window at the run's end was equivalent to its own end (the line was removed). The 63 of the first round all turned a test red |

Not validated by these tests: a front carrying a real app's traffic; `/proc/uptime` read through a
real `adb` on a real device, and the uncertainty it gives; `run --requests` on a simulator against a
real `tls-front` log. The lead validates live.

### Live validation, 0.7.0

2026-11-11, on an Android 14 emulator (API 34, a `user` image), an Android 11 one (API 30,
`userdebug`) and an iOS 26.1 simulator of an iPhone 16e, against debug builds of an app that reaches a
backend on the same machine: through `http-front` on Android and through `tls-front --log` on the
simulator. The harness that seeds the backend talked to it directly. An independent reviewer read
the request counting three times (the reader, the fronts, the clock, the windows) and every finding
was fixed; the launch's budget, the periodic set-aside and the border rule below came after that
and were reviewed by the lead only.

- **Counts are known, and they repeat.** Five runs of every scenario on one seed: 61 runs on
  Android and 55 on the simulator, the count known in every one. Signed out, every input step and
  every launch held the same number in five of five runs. Signed in, they agreed once the one
  request the app sends on a timer was set aside, but for one step a platform, in which the app's
  access token ran out about half an hour after it had signed in (one refresh and one repeat of the
  refused request).
- **The device's clock** was placed against this machine's to between 0.027 and 0.062 s in 127
  runs on the two emulators, to 0.045 s or less in 100 of them; the 0.062 s was read with the
  machine under a load of 16 to 18.
- **Changed on what a device showed: who takes the uncertain zone at a rest's border.** As first
  built, a rest took it, on the reading that an input needs 0.3 s to reach the app. A certifying
  run on Android 14 then failed two rests, each with the next tap's own request: `input tap`
  returns in 0.01 s there (`input` is `cmd input`), against 0.47 to 0.55 s on Android 11 (it starts
  a runtime). The input step takes the zone now. The same capture analysed again passes, 4 of 816
  step records in 87 saved captures change (those two rests and two taps), and a new certifying
  run passed 13 of 13 with the machine's load between 13 and 36.
- **Added on what the app showed: `gate.periodic_requests`.** The first full run on each platform
  failed on a request at rest that the app sends once a minute by design. Named in the gate block,
  it is set aside in any step, with a note: 13 rests of 220 on Android and 13 of 230 on the
  simulator held it and nothing else.
- **Budgets hold, and they can fail.** With `expect.requests_max` on 97 steps and
  `launch_requests_max` on 23 launches, a blocking run of every scenario passed on Android (13 of
  13) and on the simulator (11 of 11). On five saved runs a platform: a step's budget lowered
  by one fails five of five, a launch's lowered by two fails five of five, and without the periodic
  request named five of five fail.
- **An unknown count fails.** A run during which the front was stopped and started again fails
  every request check: "the front started listening again after the run began".
- **Android 11.** One scenario with `--sections` and `--requests` on the API 30 emulator: every
  step covered by the trace, the per-layer frame counts read, the request counts known (the clock
  to 0.033 s) and equal to those of Android 14. 0.5.0 failed the same scenario there on a trace
  that ended at the launch.
- **Two clients, one front.** A limit, found the hard way: while a run was going on one emulator,
  the same build was used by hand on a second one, and both reach the backend through the one
  front. That client's requests were counted in the run's steps and failed two scenarios. The log
  cannot tell clients apart (README, Known limits).

Not validated live: a physical device; a count refused because the clock could not be placed (in
the tests only); `http-front` or `tls-front` in front of anything but a development backend on the
same machine.

### Validation, 0.7.1

**What failed.** 0.7.0's CI run (agent-marketplace `be4546d`, GitHub's ubuntu runner, Python
3.12.16, `python -B -m unittest discover -s plugins/ui-probe/tests`): 781 ran, 1 failed, 2 skipped.
`test_requests_070.SegmentTest.test_a_replaced_or_cut_log` removed the log and wrote a new one, and
expected "replaced during the run"; it got "the request log was cut during the run". The run told a
replaced log by its inode and device alone, and Linux gave the new file the inode number of the one
just removed, where macOS (APFS) gives another; the new file was shorter, so it read as the same
file cut. The counts were refused either way; but a replacement longer than the old offset with the
old number would have been read on as the same file. On macOS the test passed.

**The Linux run is the lead's.** The rows of the table ran on macOS only. Before the push the lead
ran the suite in a Linux container the way CI does (2026-11-11: `python:3.12-slim`, Linux 6.10,
Python 3.12.16, `procps` and `openssl` installed, the CI's `requirements-ci.txt`, a user that is not
root, `python -B -m unittest discover -s plugins/ui-probe/tests`). On the released 0.7.0 tree that
run gave exactly what CI gave: 781 ran, 1 failed (the same test), 2 skipped. On this tree: 796 ran,
none failed, 2 skipped. Run as root, the 0.7.0 tree failed one test more, the one this version
skips as root.

| Date | What | Against | Result |
|---|---|---|---|
| 2026-11-11 | Unit tests | macOS (Python 3.12), `python3 -B -m unittest discover -s plugins/ui-probe/tests -q` | 796 tests pass (the run printed `Ran 796 tests` and `OK`; none skipped on this Mac); none touches a device, a simulator or the network beyond 127.0.0.1 |
| 2026-11-11 | A replaced log, on every file system | `test_requests_070.SegmentTest`: the log rewritten in place with other bytes (its inode certainly the same: a reused number on any file system); removed and written again with other bytes (its number reused or not); replaced by `os.replace` with a file made beside it holding the same first bytes and more (another number for certain); cut with its first bytes kept, and to nothing; cut and rewritten with other bytes; grown; a log over 4096 bytes whose 4096th byte changed; removed | replaced (by its bytes), replaced, replaced (by its inode), cut, cut, replaced (by its bytes), read on, replaced, could not be read; nothing of the bytes kept is in `raw/requests.jsonl` or `raw/requests-meta.json` |
| 2026-11-11 | A front's command line | `test_portable_071`: `/proc/<pid>/cmdline` answered (with `ps` made to fail), absent with `ps` answering, both failing, an empty cmdline, another program; this process on this host | `/proc` first, `ps` where there is none, None where neither reads; the owner check sees a front on a host without `ps` |
| 2026-11-11 | The other tests of 0.7.0, read for what they assume of the host | the eight 0.7.0 test files: inode numbers, file modes on creation, signal delivery order, `ps` output, `/tmp` paths, timestamp resolution, the user running them | inode numbers: only the replaced-or-cut test (rewritten). File modes: every 0600 asserted is set by `fchmod`, whatever the umask (left). The user: the folder-that-cannot-be-written test assumed it does not run as root, which a Linux container often does (skipped as root, as the rehearsal's like test already was). Signal order: the fronts set their handlers before the `listening` line the tests wait for, and the tests set SIGHUP's disposition themselves (left). `ps`: no test parses it, but the owner check read `ps -o command=`, which a slim container lacks (now `/proc` first). `lsof`: read only where there is no `/proc` (left). `/tmp`: only device paths (`/data/local/tmp`) in strings; host files are in temporary folders (left). Timestamps: no 0.7.0 test reads a file's time; two bounds on elapsed time (1.5 s and 1.0 s) depend on load, not the system (left). Ports: bound to 0 and released before use, a race on any system (left) |
| 2026-11-11 | Runs of earlier versions | the gate of 0.7.0 as released (`be4546d`) against this version's, `gate --json`, on the saved folders under the lead's run trees | text output, exit status and JSON (the tool's version aside) equal in all 668 folders |
| 2026-11-11 | That the tests can fail | 15 mutants, one exact replacement each in a copy of the plugin, then `test_requests_070.SegmentTest` or `test_portable_071`: the number of bytes kept, the bytes kept, the bytes compared, the inode and device compared, a shorter file compared with the start of what was kept, cut decided before the bytes, a file not grown read on, the problem used, the bytes never written; `/proc` read first, `ps` where there is none, an empty cmdline, the arguments split at NUL, the owner check using it, `ps` failing | all 15 red. On the first pass 13: a log that had not grown at all was untested (a mutant calling it cut survived), and `ps` exiting non-zero while printing something was untested; a test was added for each |

### Validation, 0.8.0

**The rows of the table are unit tests.** None of them touched an Android device, an emulator or
a simulator: `am`, `adb`, `xcrun`, `xcodebuild` and the input agent were stand-ins, and the agent's
Swift was only compiled (`xcodebuild build-for-testing` for `generic/platform=iOS Simulator`, into
a scratch folder; no simulator booted, launched or read). What the devices gave is in "Live
validation, 0.8.0" below.

What the devices said by hand, the reason for the alert and for `launch_url` (the lead,
2026-11-11, an API 34 emulator and an iOS 26.1 simulator):

- Android, app running: `am start -W -a android.intent.action.VIEW -d '<link>' -p <package>`
  printed `Status: ok`, `LaunchState: WARM`, `Activity: <package>/<activity>`, `TotalTime`,
  `WaitTime`, `Complete` and exited 0.
- Android, a link nothing handles: `Error: Activity not started, unable to resolve Intent {...}`,
  exit 0. The `Error` line is what catches it, not the exit status.
- Android, app force-stopped: the same command started it cold (`LaunchState: COLD`) and showed
  the link's screen.
- iOS Simulator: `xcrun simctl openurl <udid> <link with a custom scheme>` exited 0, and the system
  showed an alert, `Open in "<app>"?`, buttons `Cancel` and `Open`; nothing reached the app until
  Open was tapped. The alert stayed after the app was terminated and sat above the app after the
  next launch.

The command lines the code builds, to try by hand:

- Android, a `url` step, one line of the device script (run by `sh` on the device, `$PKG` set to the
  scenario's package, single-quoted): `am start -W -a android.intent.action.VIEW -d 'example://join/ABC123' -p "$PKG" > $D/url-open-invite.txt 2>&1; echo "exit $?" >> $D/url-open-invite.txt`.
- Android, `launch_url`, the launch's line of the device script: `am start -W -a android.intent.action.VIEW -d 'example://join/ABC123' -p "$PKG" > $D/am-start.txt 2>&1`.
  Either by hand from a host: `adb -s <serial> shell "am start -W -a android.intent.action.VIEW -d 'example://join/ABC123' -p <package>"`.
- iOS Simulator, from the host, each word one argument with no shell: `xcrun simctl openurl <udid> example://join/ABC123`;
  then the agent's command `{"token": T, "nonce": N, "cmd": "alert", "button": "Open", "timeout_s": 5}`, answered
  `{"ok": true, "tapped": true}` (or `false` when no such button appeared), with `proof` and `elapsed_ms`. Before
  the launch of a run with a link: the same command with `"button": "Cancel", "timeout_s": 1`.

| Date | What | Against | Result |
|---|---|---|---|
| 2026-11-11 | Unit tests | macOS (Python 3.12), `python3 -B -m unittest discover -s plugins/ui-probe/tests -q` | 835 tests pass once moved onto 0.7.1 (the run printed `Ran 835 tests` and `OK`; 820 before the move); none touches a device, a simulator or the network beyond 127.0.0.1. The lead ran the same suite in a Linux container the way CI does (Python 3.12, a user that is not root): 835 ran, none failed, 2 skipped |
| 2026-11-11 | Validation of the link | `test_url_080.py`: accepted forms on both platforms (a query, `%`, `@`, `,`, `_`, `#`, a scheme with `+ . -`, 500 characters), each of the 18 refused characters named (the space, the three quotes, the backslash and the 13 others), controls, DEL, non-ASCII, the first one named, the scheme, the length, a missing or non-string link, every key of another kind, the simulator's own refusals; the same rules for `launch_url` | as designed |
| 2026-11-11 | The Android command lines | `build_device_script` for a scenario with a `url` step, for one with `launch_url`, and 0.6.0's two digest scenarios (neither) | the exact lines above; the step's between the start and injected marks with a tap's wait after it and `$D/url-*.txt` in the clean-up list; the launch's between `launch.start` and `launch.end` in place of the launcher's, every other line of the script the same; the digest scripts byte for byte the same |
| 2026-11-11 | What am prints | `parse_url_open`, the launch's analysis and the gate on captures built in the test: am's success output, a link delivered to a running top-most activity, the lead's unresolved-link output exactly (exit 0), a non-zero exit, no `Activity:` line, no exit line, no file; a cold start by link | the step opened only for the success and the delivered link, each other fails `input` with am's words or the reason; the unresolved link fails the launch (`the app did not start: Error: Activity not started, ...`); the cold start by link reads as a launch (COLD, ok, its total time) with every step, first frames included, as for a start by the launcher |
| 2026-11-11 | `launch_url`'s place | validation, the simulator's refusal, `request_windows` and `launch_requests_max` with and without it | `activity` with it refused; the simulator's reason; the same windows and budget |
| 2026-11-11 | The simulator | the xcrun and agent stand-ins of 0.4.0 and 0.5.0, the agent's taught `alert` (Open tapped, none, the agent failing): a scenario of rests and a link; a link with taps and swipes; `simctl openurl` exiting 1; a stale alert there, none, and the agent failing to look; the stale record removed; the app not in front after the link; a scenario without a link | the exact line above; the agent started for a link; `Cancel` asked once it was up and before the launch, `Open` right after simctl inside the step, a ping at the end of every step; `url_open.alert_confirmed`, the summary row and the printed line; no alert passes, the agent failing fails `input` on the step, a failed simctl sends no `Open`; a stale alert cancelled and said in a note, the agent failing to look or no record fails `input` on the launch; the app not in front fails `foreground`; without a link no `alert` is sent and no record is kept |
| 2026-11-11 | The agent's source | `test_input_050`: the served code outside the rehearsal-only functions | the only query besides the app's state is `system.alerts.buttons` in `alert`, which takes no app and is dispatched for `alert` |
| 2026-11-11 | Rehearse and replay | the stand-ins of 0.6.0: a link before a tap by `find` and a swipe | the rehearsal asks `Cancel` before its first step, opens the link and confirms it before the lookup, and records both; a stale alert cancelled is said; no alert is recorded; a link it could not open, or an agent failing either look, stops it with no `rehearsal.json`; the run opens and confirms again and looks nothing up; a rehearsal without a link sends no `alert` |
| 2026-11-11 | Judging, counts and request windows | `classify_burst` with kind `url`; `request_windows` with a `url` step in place of a tap; a request in the link's step against `expect.requests_max: 0` | judged as a tap (a state change), not as motion; the same windows as a tap; the request fails `requests` on the step |
| 2026-11-12 | Runs of earlier versions | the gate of 0.7.1 as released (`ed6fb86`) against this version's, `gate --json`, on the saved folders under the lead's run trees (the live runs of 0.1.0 to 0.7.1) | text output, exit status and JSON (the tool's version aside) equal in all 669 folders compared; 8 folders written by this version itself, most of them holding a link, were left out. Before the move onto 0.7.1 the same held against 0.7.0 (`e8da0b0`) in all 593 folders there were then |
| 2026-11-11 | That the tests can fail | 72 mutants, one exact replacement each in a copy of the plugin, then `test_url_080` (the agent's two through `test_input_050`): the 39 rules of the first round (the kind and its keys, the default wait, the length, the refused set, the printable range, the first refused character named, the scheme's four parts, a string, the clean-up list, `-p`, the quoting, the exit status kept, the time allowance, the analysis, am's failure readings and the delivered link, the Android gate, the simulator's validation, opening, exit, words, step JSON, analysis, gate checks, summary row, printed line, the rehearsal's opening, stop and record), 24 of the alert (the agent needed for a link, the step's `Open`, its label and timeout, `alert_confirmed`, the gate's agent check, the stale `Cancel` in a run and before the launch, its label, timeout and `cleared`, its note, failure and missing record, nothing new without a link, the stale record in the analysis, the rehearsal's confirming, stop, clearing, its failure, its line and its record, the summary row, the printed `confirmed`), 7 of `launch_url` (the key, `activity` with it, its link checked, the simulator's refusal, the launch line used, `-p`, the quoting) and 2 in the agent's Swift (the alert querying the app, `alert` not dispatched) | all 72 red. On the first pass 71: `alert` not dispatched survived, since the test checked the call and not the `case` that reaches it; the test now checks both, and the mutant is red |

### Live validation, 0.8.0

2026-11-11 and 2026-11-12, by the lead, on the devices of 0.7.0's live validation (an Android 14
emulator, an Android 11 one, an iOS 26.1 simulator of an iPhone 16e) against debug builds of an app
that claims a custom scheme. The rehearsal was run on the code as released; the other runs on the
code before it was moved onto 0.7.1, which changed nothing a link passes through.

- **iOS Simulator, a link into the running app.** A `url` step opened a link to a profile. The
  agent found the system's alert and tapped Open (`alert_confirmed: true`), the app showed the
  profile, and the two rests after it held no frame, no layer commit and no display update: the
  query of SpringBoard did not disturb the app under test.
- **The system asks once.** On the same simulator every later link opened without an alert, also
  after the app had been uninstalled and installed again and the simulator's keychain reset. The
  step then waits out its five seconds, reports `alert_confirmed: false` and passes. The recording
  of a `url` step therefore differs between a simulator's first link and its later ones: its
  signature read `T TD` with the alert and `TD` without (README, Known limits).
- **A stale alert.** On a simulator made for the purpose and deleted afterwards, a link was opened
  by hand and left unanswered. The next run cancelled that alert before its first step
  (`stale_alert.cleared: true`, a note at the gate), opened its own link and confirmed the new
  alert; its rests were still. The machine's load was 182 during that run.
- **Rehearse and replay.** A scenario with a link, a rest, and a tap found by its description on
  the link's screen: the rehearsal opened the link and read the position on that screen (two
  reads), and the measured run replayed it in a fresh process. It passed with every rest still.
- **Android 14, a start by link.** `launch_url` started the app cold on the link's screen
  (`LaunchState: COLD`, 2.1 s); the rests were still and the launch's requests were counted.
- **Android 14, a link into the running app.** The step is recorded as opened, with the activity am
  named; its frames and its four requests were counted in the step, and the rest after it sent
  nothing.
- **Android 11.** A start by link passed (1.4 s). A link into the running app was opened, and the
  rest after it failed on one request: the app under test starts a second instance of its activity
  for a link (launch mode `standard`), and that instance opened another event stream. The tool
  measured what the app did (README, Known limits).
- **A link delivered to an activity that is already on top** (a launch mode that reuses it): am
  prints a `Warning:` line, `Status: ok` and the `Activity:` line and exits 0, on Android 14 and
  on Android 11 (read with the system's Settings app). The step counts that as opened.

Not validated: a physical device; the alert's labels in another language or on another iOS
version; an `https` link (a universal link) on the simulator; an app that opens a link in a scene
of its own.

### Validation, 0.8.1

**The rows of the table are unit tests and the gate run again over captures already saved.** The
live runs were the lead's, on 2026-11-12, on the emulators and the simulator of 0.7.0's live
validation, against a debug build and a backend on the same machine:

- **Android 11 (API 30).** The whole gate of the project under its older-device profile: 15 of 15
  scenarios pass, where 0.8.0 had failed two caret rests in the run this version was made for. In
  that run no rest recorded an identical frame, so the rule had nothing to do. Six more runs of
  the scenario with the focused field then met the case once in twelve caret rests (frames 10,
  the app's layers 10, the display 11, one identical frame): set aside with the note, and six of
  six runs pass.
- **Android 14 (API 34).** The whole gate, blocking: 15 of 15. A phase of eleven further
  scenarios: all pass but two rests that the project holds at zero on a known defect of its app.
  Nothing was set aside in either.
- **iOS Simulator.** Seven scenarios in a rehearsal and a measured run: the simulator's gate is
  untouched by this version, and it gave what 0.8.0 gave on five earlier seeds.

The lead's suite runs on the final tree: Linux container 846 ran, none failed, 2 skipped. macOS:
of two runs one passed all 846; the other had one error in a test of the TLS front that dates
from 0.5.0 (`test_tlsfront_050`, a handshake that had not completed after 60 s while two other
suites and an emulator batch shared the machine). That test alone then passed five times in five.
Not established why the handshake stalled; this version does not touch the fronts.

Why: on an Android 11 emulator (the lead's profile run of 2026-11-11), two rests on a focused field,
whose caret blinks and whose allowance covers frames and changed frames but not unchanged ones,
failed `rest-video` with one recorded frame that changed nothing. Both steps hold frames 10,
frames_layers 10, frames_display 11, and the recording 10 frames that changed the app's area (the
caret's blinks; none large enough to count as a real change) and 1 that changed nothing. Read from
the saved captures: over each of those rests the compositor's statistics show the app's layer
rising by 10 and no other layer's count rising (status bar, navigation bar, launcher, input method),
while the display's total rose by 11; every composition was a client composition. The same kind of
rest with the keyboard hidden had 10 and 10. Which layer asked for the extra composition is not
established.

The rule's first form (set aside up to frames_display - frames_layers) was tightened after the gate
comparison showed it excusing the app's own frames: a 150 s rest on an app's home screen on
Android 14 (L 4, D 9, A 2, I 2), where the lead lined
up the recording by hand: the app drew two frames after each of two session re-syncs, the second of
each changing nothing, and the display's five extra composites were the status bar's, kept by the
recorder as frames that changed only outside the app's area. The app now owns as many unchanged
frames as its latches beyond the recorded changes in its area, and only the rest, up to D - L, are
set aside. A is `app_region_frames` (any change in the app's area), not `app_changed_frames_real`:
with the real changes only, the Android 11 rests (A 0 by that count) would still fail.

| Date | What | Against | Result |
|---|---|---|---|
| 2026-11-12 | Unit tests | macOS (Python 3.12), `python3 -B -m unittest discover -s plugins/ui-probe/tests -q` | 846 tests pass (the run printed `Ran 846 tests` and `OK`); none touches a device, a simulator or the network beyond 127.0.0.1. One earlier test changed its expectation: `test_presence_and_loss`'s app drawing at rest, whose files have the display composing 10 frames for other layers during the rest (D 40, L 30, A 40, I 3), now has its 3 unchanged frames set aside and still fails on the frames drawn and the 40 changed |
| 2026-11-12 | The rule | `test_other_layer_081.py`, runs built in the test: the Android 11 rest (L 10, D 11, A 10, I 1); the long rest (L 4, D 9, A 2, I 2); two unchanged frames with one composite to spare; L 10 and A 9 with three unchanged frames and D 12; D 15 with two; an animation at zero alpha (A 0 with D 15, 10, and below L); L, D or A unknown, a boolean or a float, and an unknown A with more unchanged frames than latches; an `unchanged_frames_max` allowance with three, two and one unchanged frames, and a rest whose one unaccounted latch changed nothing (L 2, D 3, A 1, I 1); the app presenting nothing; the changed-frames check | passes with the note; fails as in 0.8.0 with no note; one set aside and one held; the app owns one, two set aside, one held; both set aside; fails as before with no note; nothing set aside; fails on two, passes on one with its note, passes with no allowance note, and that rest passes on its allowance again; the earlier note alone; unchanged |
| 2026-11-12 | Runs of earlier versions | the gate of 0.8.0 (`7b57cb1`) against this version's, `gate --json`, on every saved run folder under the lead's run trees (755 folders) | 751 the same (text, exit status and JSON, the tool's version aside); 4 differ, each by this rule alone. Two pass that failed: the Android 11 profile's two scenarios with a rest on a focused text field (L 10, D 11, A 10, I 1 in each; 1 set aside). Two still fail: a rest during which the system's state was dumped (L 1, D 10, A 0, I 9; 8 set aside, 1 held, still `rest-video` and `rest-frames`) and a rest after the app recovered from being offline (L 4, D 7, A 4 of which 3 real, I 1; 1 set aside; still `rest-frames` and `rest-video` for 3 changed frames). The first form's other differences (a settings screen's runs, a comparison's head, a control run, the long rest, and the rest with L 2, D 3, A 1, I 1) are gone |
| 2026-11-12 | That the tests can fail | 20 mutants, one exact replacement each in a copy of the plugin, then `test_other_layer_081`: set aside at all, the L - A bound, the bound being L - A, A unknown taken as 0, A the real changes only, the min with D - L, D - L as it is, D known, L known, a boolean and a float not counts, not on top of the case where the app presented nothing, the held number judged, reported and in the message, the failure saying how many were set aside, the allowance judging the held number, its note saying it, the note, the note's counts | all 20 red. On the first pass 19: an unknown A taken as 0 survived, since every unknown-A case had no more unchanged frames than latches; a case with more (the dump run's shape) was added. The first form's round: 16 of 16 red after one equivalent mutant was dropped and one test added |

### Validation, 0.8.2

**What the author of this version ran, on macOS (Python 3), on 2026-11-12: single test files and
the gate run again over captures already saved. No device, emulator, simulator or network.** The
author did not run the whole suite or anything live. The last three rows are the lead's, who did.

Why: an independent review of 0.8.1 built gate inputs in which the rule set aside frames the app
had drawn. A pop-up window whose layer name does not carry the package (Android 11 and older) draws
three frames that change nothing: the platform counts them (3), the layers' count misses them (0),
the display composed them (3). Under a step's `frames_max` allowance 0.8.1 passed that rest with all
three set aside. The same review found that a negative count set frames aside, that an unconfirmed
link's alert passed without a word on the iOS Simulator, and that a rehearsal and a run cancel a
stale alert at different moments with no reason written down.

| Date | What | Against | Result |
|---|---|---|---|
| 2026-11-12 | Unit tests, one file at a time | `python3 -B -m unittest <file>` in `plugins/ui-probe/tests`: `test_other_layer_081`, `test_fixes_082`, `test_presence_and_loss`, `test_gate`, `test_borders_070`, `test_docs`, `test_url_080`, `test_analysis`, `test_simulator_040` | 14, 6, 32, 30, 6, 12, 39, 41 and 43 tests, each file `OK` (`test_fixes_082` has 5 as released: see the lead's rows below). One earlier test changed its expectation: `test_presence_and_loss`'s app drawing at rest names the app's count (30, platform and layers alike) in the note instead of the layers' |
| 2026-11-12 | The rule | `test_other_layer_081.py`: the cases of 0.8.1 with F in place of L; the pop-up case with and without the `frames_max` allowance; a trace that counted one more frame than the platform and the layers; an unknown or boolean platform count; a trace count that is a boolean or negative; negative counts (the platform's and the layers' at -5, the layers' alone at -5, a trace's at -1, A at -3), and `_count` itself | the pop-up case fails on `rest-video` with nothing set aside (and also on `rest-frames` without the allowance); the trace's frame is the app's and fails; every unknown or negative count sets nothing aside |
| 2026-11-12 | The iOS Simulator | `test_fixes_082.py`, with the xcrun and agent stand-ins of 0.4.0 and 0.6.0: a link whose alert was not confirmed, one that was, one the simulator did not open; the order of the stale alert's `Cancel` and the launch in a rehearsal and in a run | a note on the unconfirmed link's step and none on the others, with the same two `alert` commands as before; a rehearsal launches and then cancels, a run cancels and then launches |
| 2026-11-12 | Saved runs, 0.8.1 against this version | `gate` (in-process, read only: nothing in the folders changed, checked by modification time) on four saved run trees, 35 run folders: an Android 11 profile run (13), six runs of a focused-field scenario on Android 11 (6), a certifying run on Android 11 (15), a 150 s rest on an app's home screen on Android 14 (1) | verdicts and violations the same in all 35. Three rests had a frame set aside under 0.8.1 (platform 10, layers 10, display 11, A 10, I 1) and have it set aside under this version, with the note now naming the app's count (10). The long rest fails as under 0.8.1 (`rest-frames`, two `rest-video`, `rest-requests`), nothing set aside. The certifying run had nothing set aside under either version: its four rests with an unchanged frame are ones where the app presented nothing |
| 2026-11-12 | That the tests can fail | one change at a time in a copy of the plugin, then the test file named: F as the layers' count alone; the platform's count optional; the trace left out of F; a trace count not checked; a negative number a count; no note for an unconfirmed link; the rehearsal cancelling before the launch; the run cancelling after the launch | each turned its test red: `test_a_surface_the_layers_miss_stays_the_apps` and `test_a_trace_count_is_part_of_the_apps_account`; `test_unknown_counts_set_nothing_aside`; `test_a_trace_count_is_part_of_the_apps_account`; `test_unknown_counts_set_nothing_aside` and `test_negative_counts_set_nothing_aside`; the same two; `test_a_link_whose_alert_was_not_confirmed_passes_with_a_note`; `test_a_rehearsal_cancels_after_the_launch_and_a_run_before_it` (twice) |
| 2026-11-12 | The whole suite, by the lead | `python3 -B -m unittest discover -s plugins/ui-probe/tests`, on macOS and in a Linux container (python:3.12-slim, a user that is not root) | 854 tests, `OK` on both. `test_fixes_082` has 5: the lead removed a sixth, which kept the run and scenario names of the project the evidence came from out of this plugin by listing them in it. `scripts/check-coupling.sh ui-probe` passes |
| 2026-11-12 | The gate of 0.8.1 against this version, by the lead | the released 0.8.1 as installed (the SHA-256 of the file tagged for it) and this version's script from a frozen copy: `gate` on every saved run folder of the project that uses the probe, 967 folders, Android and the iOS Simulator | the same exit status and text in 951; in 16 only notes differ; no verdict differs. Five of the 16 carry the reworded set-aside note. Eleven carry the new note for a link whose alert was not confirmed: every saved link run on the iOS Simulator. In the certified runs among them the requests the link causes were counted, so there the link had opened without the system asking, and that project will see the note on every such run |
| 2026-11-12 | Live, by the lead, from a frozen copy of this version | the signed-out half of the consuming project's gate on an Android 14 emulator (seven scenarios, blocking, one of them a focused-field scenario whose rests carry a frames allowance) and on the iOS Simulator (five scenarios) | 7 of 7 and 5 of 5 pass, and nothing was set aside on Android 14. No run was made on Android 11 for this version: the rule is checked there on the saved captures above. The capture code is unchanged by this version: the diff of `ui_probe.py` is the version, a comment in the rehearsal, and the gate's analysis and notes |

### Validation, 0.9.0

**What the author of this version ran, on macOS (Python 3, ffmpeg 7.0.1), on 2026-11-12: single test
files, planted faults in scratch copies, and the repository's coupling check. No device, emulator,
simulator, phone or network.** The author did not run the whole suite, did not run anything live and
never saw a real phone's film: every film and frame in the tests is synthetic, made by the tests
(arrays, or lossless PNG streams written with ffmpeg as earlier tests do).

The first form of `film` failed on a real capture. It dropped frames bit-identical to the one before,
masked a fixed point of at most 4 pixels in a 3-pixel box, and left out key frames found by a period
of spread changes. The lead ran it on one phone capture (H.264, 60 a second, 1206 x 2622, 113 s) and
it counted every frame of both rests as a change (1,560 of 1,560 and 186 of 186): no two frames of
that capture are bit-identical, about ten pixels in two places flicker in most frames (more than 4),
and no period was found. The author's synthetic films had been made with exactly the properties that
form assumed, so its tests proved nothing about a capture. The lead measured the capture (the
numbers in the README's "A film no run made"); the rule was rebuilt on them, on connected groups,
and both mechanisms were removed.

A review of the rebuilt rule found that it compared a frame only with the one before, so a change
that moves less than the minimum or the level each frame passed however far it went (a fade of 4
levels a frame, a skeleton pulse, bars that grow 3 or 8 px a frame, an underline that slides). Each
frame is now also compared with the frame 0.5 s earlier and each window's last frame with its first,
under the same rule. The same review found that pixels outside a run's block groups went into one
box, so scattered noise failed a rest that changed only inside its region, and that a refused
re-analysis of a film run removed its stored result; both are fixed. A second reading found two ways
an allowance still hid a slow change: a slow group that only touched a counted change (within a
block) was taken for it, so a label fading or a bar growing beside an allowed ticking digit passed;
and the ends failed only when no frame counted, so a large area fading 0.25 level a frame beside an
allowed caret passed. A slow group is now a counted change seen again only when its box lies inside
the union of the counted boxes (each widened by a block) and the window's regions, and the ends fail
whenever their difference is not covered by that union. Inside one widened box alone was tried
first: a growing bar's ends (the union of its segments) and a counted change merged with a digit
inside its region then failed, so the union was used. A third reading showed the box rule itself was
weak: four allowed counters 40 x 40 px, 30 px apart, with a label in each gap fading 1 level a frame,
passed, since every gap lay inside the widened boxes. The rule now works on pixels: per pixel the
last frame it was part of a change counted between neighbours; a slow group's pixels within 3 px of
one counted in the last half second, or in a region, are explained, and what is left is grouped
again. Writing the slow changes counted into that memory too was tried first: a fade counted once
then explained itself for the next half second and the window's ends, and the label beside an
allowed digit passed again, so only changes counted between neighbours explain a slow group (the
ends are explained by both). Every test that passed before passes unchanged.

What the films are made of now: 1080 x 2400 at 60 frames a second; every frame changes 20 pixels by
one level; two places of five pixels flicker by 50 levels every frame; every 30th frame is a key frame
that makes two consecutive changed pairs, each with about 0.03 % of the picture over the level in
groups of up to 40 px placed anywhere, so that groups of one pattern, of neighbouring frames and of
frames 30 apart may touch. Over 400 frames of the still film (13 key frames) the largest group was
40 px between neighbours, 40 px 30 frames apart and 40 px between frames 12 and 95: no two groups
touched with these seeds, so the margin between 40 and 78 px is not tested against touching noise.
Each test adds what it needs: a caret of 6 x 60 px blinking in and out of step with the key frames, a
digit changing once, a 6 x 6 px dot, a 12 x 12 px dot beside a flickering place, a whole-picture
change of 5 and of 15 levels every other frame, a 100 x 100 px change of 15 levels every other frame,
a 60 px group once, a change in the top bar only, a digit each second inside a region and a change
across its edge; the review's slow probes (the whole picture darkening 4 levels a frame, a pulse of
30 levels over 60 frames that returns, a bar 1 px high growing 3 px a frame, one 3 px high growing
8 px a frame, a 2 px underline sliding 4 px a frame); a fade of 0.3 levels a frame seen only between
a window's ends; and groups of exactly 78 px and of 77 px, between neighbours and fading in at 2
levels a frame.

| Date | What | Against | Result |
|---|---|---|---|
| 2026-11-12 | New tests, one file at a time | `python3 -B -m unittest <file>` in `plugins/ui-probe/tests`: `test_regions_090` (watched rests and their 10,000 ms floor, region allowances in the scenario check, the block map, the placing of changes including glyph-sized pieces and scattered pixels beside a solid one, the gate on Android and the iOS Simulator, the Android analysis placing frames at full resolution through a stand-in decoder), `test_film_090` (connected groups; the film's rule on the synthetic capture; the slow comparisons and the window's ends; a slow change beside an allowed change (a label fading and a bar growing beside a ticking digit, a fade beside a blinking caret, adjacent windows) and the ticking digit alone; four allowed counters with labels fading in their gaps; a slow group on the counted pixels and 4 px beside them, and `explain_px`; a slow change inside a region; groups at the minimum and one pixel under it; a refused re-analysis keeping `run.json` byte for byte; regions and a watched window on a film; windows past the end; the header's count; `frames-full`; `--no-pictures` (no file in the folder an image or a video by extension or by its first bytes, the same gate verdict as with pictures, `zoom` and `sheet` refused) and the film read from its path and checked; the windows file; the verb with `gate`, `summary`, `zoom` and `sheet`; `sheet --full --distinct` on a simulator run and on Android windows; the decoder's count; a real lossless film end to end) | 41 and 59 tests, each file `OK` |
| 2026-11-12 | Existing tests, one file at a time | `test_ui_video`, `test_analysis`, `test_gate`, `test_scenario_and_parsers`, `test_simulator_040`, `test_store_050`, `test_steps_020`, `test_docs`, `test_fixes_082`, `test_rehearsal_060`, `test_input_050`, `test_review_050`, `test_profiles_030`, `test_presence_and_loss`, `test_other_layer_081`, `test_borders_070`, `test_run_and_cli`, `test_url_080`, `test_compare_summary_cli` | each file `OK`. One existing test changed: `test_fixes_082`'s docs test no longer pins the plugin's version (0.8.2); `test_film_090` pins 0.9.0, as 0.8.2 moved the pin from `test_other_layer_081` |
| 2026-11-12 | That the tests can fail | 48 faults, one at a time, each in its own copy of the plugin, then the one test named in the table below; every named test passes on the unchanged code (33 tests, run together) | all 48 turned their test red. A first run of four of them was void: their test failed on the unchanged code (a wrong expected count); after the test was corrected they were run again and turned it red |
| 2026-11-12 | Coupling | `scripts/check-coupling.sh ui-probe` at the repository root | passes |
| 2026-11-13 | The whole suite, by the lead | `python3 -B -m unittest discover -s plugins/ui-probe/tests`, from an archive of the reviewed commit, on macOS and in a Linux container (Python 3.12, ffmpeg 7.1, a user that is not root) | 954 tests, `OK` on both; nothing skipped in Linux, where ffmpeg was installed so that the films made with a real encoder are decoded too |
| 2026-11-13 | The gate of 0.8.2 against this version, by the lead | the released 0.8.2 as installed and this version's script from the same archive: `gate` on every saved run folder of the project that uses the probe, 1,007 folders, Android and the iOS Simulator | the same exit status and text in all 1,007: a run that uses none of the new keys is judged as before |
| 2026-11-12 | One real phone capture, by the lead | an iPhone's screen recorded through a Mac, H.264, 1206 x 2622, 60 a second, 113 s; three windows: 26 s and 3 s of a screen at rest, and 3.4 s holding two swipes | the first form of this version (identical frames dropped, a fixed point masked, a key-frame period) counted every frame of all three windows as a change: no two frames of the capture are identical. The final form, run with `--no-pictures` (the folder held `capture.json` and `run.json` only): the two rests 0 of 1,560 and 0 of 186 frames changed, the swipes 198 of 202 and their ends differ, so the rests pass and the swipes fail. Largest scattered groups at rest: 57, 67 and 38 px (neighbours, 0.5 s apart, ends) against a minimum of 95 px |
| 2026-11-13 | Independent review | a reviewer that did not write the code, default-refute, over five rounds on synthetic films with a stand-in decoder and, in the last, films encoded with libx264 | every finding was fixed and the case that showed it became a test that fails without the fix: a faint change every other frame taken for the recording, a region swallowing a thin change, a window past the film's end, a 1 ms watch minimum, slow changes under the minimum a frame, and allowed changes excusing a slow change beside them or in the gaps between them. Left as stated limits: an allowance is a budget for the whole step; a change made of pieces each under the minimum; a heavily compressed film |

| Fault planted | Test that turned red |
|---|---|
| the film's minimum group 1 px | `test_a_rest_holds_no_change` |
| 8-connected groups joined along rows and columns only | `test_groups_join_along_rows_and_diagonals` |
| any pixel over the level a change, no group minimum (the first form's behaviour on a real capture) | `test_a_rest_holds_no_change` |
| a film's level 4 instead of 10 | `test_a_faint_change_of_the_whole_picture` |
| `frames-full` holding every frame of a window | `test_frames_full_holds_the_changes_and_the_ends_only` |
| a header frame count that differs not reported | `test_a_header_that_differs_from_the_decoded_count_is_reported` |
| a window past the film's end not refused | `test_a_window_past_the_films_end_is_refused` |
| no caution near the minimum | `test_a_scattered_group_near_the_minimum_earns_a_caution` |
| no steady places reported | `test_a_rest_holds_no_change` |
| a watch minimum of 1 ms accepted | `test_bad_values` |
| no gate note for a watch minimum below 70,000 ms | `test_a_low_watch_minimum_is_named_by_the_gate` |
| a film's system bars ignored | `test_system_bands_only` |
| a film's scattered groups placed as changes | `test_a_countdown_inside_and_a_change_across_its_edge` |
| `sheet --full`: no refusal when fewer frames are extracted | `test_refused_when_fewer_frames_are_extracted` |
| `sheet`: no refusal when the recording's count differs from the analysis | `test_refused_when_the_recordings_count_differs_from_the_analysis` |
| `sheet --distinct` sheets of every frame | `test_full_and_distinct` |
| a watched minimum of 60 s | `test_a_watched_rest_of_69_s_is_refused_and_70_s_is_not` |
| a scenario's `watch_min_ms` ignored | `test_a_scenario_may_set_its_own_minimum` |
| a change that crosses a region's edge counted inside it | `test_crossing_an_edge_counts_against_the_rest` |
| a region's reason not required | `test_each_region_needs_its_reason_and_limit` |
| Android changes inside a region still counted against the rest | `test_changes_inside_the_region_pass` |
| a region report made for other regions accepted | `test_a_report_for_other_regions_fails` |
| a block of 24 px whatever the width | `test_a_block_is_24_of_1080_of_the_width` |
| the Android app's area from fixed rows instead of the run's insets | `test_inside_crossing_and_bars` |
| a simulator frame inside the regions still counted as a recorded frame | `test_frames_inside_the_region_leave_both_rest_counts` |
| `--no-pictures` still writing the sheets | `test_no_picture_and_the_same_verdict` |
| `--no-pictures` still copying the film into the folder | `test_no_picture_and_the_same_verdict` |
| `zoom` not refused on a run made without pictures | `test_no_picture_and_the_same_verdict` |
| the film read from its path without checking its size and SHA-256 | `test_the_film_is_read_from_its_path_and_checked` |
| no comparison with the frame 0.5 s earlier | `test_the_whole_picture_darkening_4_levels_a_frame` |
| the ends failing only when no frame counted | `test_a_fade_beside_an_allowed_caret_fails_between_the_ends` |
| explained within a block of a counted pixel, not within 3 px | `test_four_allowed_counters_with_labels_fading_in_the_gaps` |
| `explain_px` ignored | `test_the_same_group_4_px_beside_is_not` |
| a slow change explaining the later slow groups | `test_a_label_fading_beside_the_allowed_digit` |
| the ends not explained by the slow changes counted | `test_the_whole_picture_darkening_4_levels_a_frame` |
| the regions excusing nothing | `test_a_slow_change_inside_a_region_is_excused` |
| the ends not explained by the pixels counted | `test_a_ticking_allowed_digit_alone_passes` |
| a slow group explained by a change counted at any time in the window | `test_a_slow_group_on_the_counted_pixels_within_half_a_second_is_explained` |
| `sheet --full`: no refusal when fewer frames are extracted | `test_refused_when_fewer_frames_are_extracted` |
| `sheet`: no refusal when the recording's count differs from the analysis | `test_refused_when_the_recordings_count_differs_from_the_analysis` |
| `sheet --distinct` sheets of every frame | `test_full_and_distinct` |
| a watched minimum of 60 s | `test_a_watched_rest_of_69_s_is_refused_and_70_s_is_not` |
| a scenario's `watch_min_ms` ignored | `test_a_scenario_may_set_its_own_minimum` |
| a change that crosses a region's edge counted inside it | `test_crossing_an_edge_counts_against_the_rest` |
| a region's reason not required | `test_each_region_needs_its_reason_and_limit` |
| Android changes inside a region still counted against the rest | `test_changes_inside_the_region_pass` |
| a region report made for other regions accepted | `test_a_report_for_other_regions_fails` |
| a block of 24 px whatever the width | `test_a_block_is_24_of_1080_of_the_width` |
| the Android app's area from fixed rows instead of the run's insets | `test_inside_crossing_and_bars` |
| a simulator frame inside the regions still counted as a recorded frame | `test_frames_inside_the_region_leave_both_rest_counts` |
| `--no-pictures` still writing the sheets | `test_no_picture_and_the_same_verdict` |
| `--no-pictures` still copying the film into the folder | `test_no_picture_and_the_same_verdict` |
| `zoom` not refused on a run made without pictures | `test_no_picture_and_the_same_verdict` |
| the film read from its path without checking its size and SHA-256 | `test_the_film_is_read_from_its_path_and_checked` |
| no comparison with the frame 0.5 s earlier | `test_the_whole_picture_darkening_4_levels_a_frame` |
| a slow group explained by touching a counted change, not by lying inside it (a label) | `test_a_label_fading_beside_the_allowed_digit` |
| the same (a bar) | `test_a_bar_growing_beside_the_allowed_digit` |
| the ends failing only when no frame counted | `test_a_fade_beside_an_allowed_caret_fails_between_the_ends` |
| the ends not explained by the changes the window counted | `test_a_ticking_allowed_digit_alone_passes` |
| inside one widened box only, not the union of the boxes and the regions | `test_a_countdown_inside_and_a_change_across_its_edge` |
| `>` for `>=` between neighbours | `test_between_neighbours` |
| `>` for `>=` in the slow comparison (on the largest group) | `test_in_the_slow_comparisons` |
| `slow_min_group` ignored | `test_the_slow_settings` |
| `slow_frames` ignored | `test_the_slow_settings` |
| the pixels outside a run's block groups one box again | `test_scattered_pixels_beside_a_solid_change_are_left_out` |
| glyph-sized pieces outside a run's block groups left out | `test_glyphs_beside_a_solid_change_get_boxes_of_their_own` |
| the film checked only after `run.json` is removed | `test_the_film_is_read_from_its_path_and_checked` |
| the slow comparisons not reported in the notes | `test_a_rest_holds_no_change` |
| no caution in the slow comparison | `test_a_scattered_group_near_the_minimum_earns_a_caution` |
| no caution between the window's ends | `test_a_scattered_group_near_the_minimum_earns_a_caution` |

Faults of earlier rounds that no longer apply to the changed code (the box rules, the per-window
clearing of the boxes, `>` on the slow boxes) were dropped; the rows above are faults on this code.
The per-window clearing had no observable effect: a window's slow comparison starts only once its
frame half a second earlier lies in the window, and the pixel rule takes only changes counted from
the window's first frame on.

Time and memory, measured on the synthetic capture at 1206 x 2622 with the stand-in decoder (240
frames, 222 judged, `--no-pictures`): 66 ms of analysis a frame, the two decodes by ffmpeg not
included; a peak of 290 MB above the process before the analysis.

Not established by this version's author: the rule on a real phone's capture (the lead's rows),
another encoder or bit rate, the region check on a real recording, `sheet --full` on a long real
recording.

### Validation, 0.10.0

**What the author of this version ran, on macOS (Python 3), on 2026-11-13: single test files, planted
faults in scratch copies, and the repository's coupling check. No device, emulator, simulator or
network, and no saved recording.** The author did not run the whole suite or anything live.

The recordings of the tests are synthetic, made by `test_outside_0100.py` to be shaped like real ones:
1080 x 2400, a frame only when the screen changes, frame times 16 to 40 ms apart, and around each
change a border of pixels 4 levels off and a scatter of single pixels 14 levels off (no group at the
minimum). The known cases are generic shapes: a switch flipped while two rows of the same screen
change and come back after 77 ms and after 33 ms; a tapped chip in a row of four that moves 70 px
with its neighbours; a theme change with the whole screen allowed and without; a keyboard rising
over four frames into a declared region and with none; a caption that appears 40 px under the
control and stays, allowed and not; a glow 14 px around the control; a plain switch in a list of
identical switches; changes of 50 and 49 ms; moves of 8 and 7 px.

The first form of the slide search failed its own test and was changed before release: it looked
for where the old content of a changed band went, and on lines that repeat every 200 px it found it
(a row that changed colour was taken for a slide of 200 px); it now looks for where the new content
came from, refuses a patch too plain to place, and requires the match to leave a mean change of at
most 4 levels.

A review of the first form found five things, all fixed with tests: the control's search started at
the first node with the matched bounds, so a wrapper with the same bounds as a clickable switch
made the whole row the control; a clickable element over the cap fell back to the label's box; a
toast of the step before, still fading, failed the step; on content that repeats every 200 px a
change undone during the step was reported as a slide; and rows that changed only by strokes 3 px
wide, 9 px apart, were not seen. The review's cases are tests here.

The lead then ran the second form live on an Android 14 emulator (a settings screen with three
identical switches, the middle one flipped off and on, three runs): the rows beside the switch,
redrawn for one frame, were reported every time (17 to 54 ms), and the control's box was the switch
itself; but every flip off failed "the control moved" by 171 px, a match on the neighbouring switch
with a second match also within the tolerance. The control's own move now needs a unique match, its
content gone from its old place, and not at the new place before the input. A reviewer's libx264
films showed the 3 px join turning key-frame noise on dense text into one large group; only pixels
changed by more than 30 levels now join. The author measured that on a synthetic screen of
antialiased text through libx264 at crf 23 (a key frame every 30 frames): across the key frame about
168,000 pixels over 10 levels and a few dozen over 30; joining every pixel over 10 made one group of
about 168,000 px, joining only those over 30 left none over 27 px. On a screen of random texture
inside glyph shapes the key frame's noise made a group of 85 px with no joining at all (the release 1
rule), over the minimum of 78; that screen is not used in the tests and is said as a limit.

| Date | What | Against | Result |
|---|---|---|---|
| 2026-11-13 | New tests, one file at a time | `python3 -B -m unittest test_outside_0100` in `plugins/ui-probe/tests`: the known cases, the boundaries, where the box comes from (a window tree, a rehearsal's frame, a declared box), the Android and simulator wiring with a stand-in decoder, the scenario check, the gate on both platforms, the hook in `analyze_run`, the docs | 51 tests, `OK` (the three libx264 tests run where ffmpeg has libx264; they ran here) |
| 2026-11-13 | Existing tests, one file at a time | `test_film_090`, `test_regions_090`, `test_ui_video`, `test_analysis`, `test_gate`, `test_scenario_and_parsers`, `test_simulator_040`, `test_store_050`, `test_steps_020`, `test_docs`, `test_fixes_082`, `test_rehearsal_060`, `test_input_050`, `test_review_050`, `test_profiles_030`, `test_presence_and_loss`, `test_other_layer_081`, `test_borders_070`, `test_run_and_cli`, `test_url_080`, `test_compare_summary_cli` | each file `OK`. One existing test changed: `test_film_090`'s docs test no longer pins the plugin's version (0.9.0); `test_outside_0100` pins 0.10.0 |
| 2026-11-13 | That the tests can fail | 31 faults, one at a time, each in its own copy of the plugin, then the one test named below; every named test passes on the unchanged code | each turned its test red. The closing of 5 px (below) first stayed green once the join level came in, since the test's noise was too weak to join at any radius; its noise was made strong (50 levels), a weak pair added, and it turned red |
| 2026-11-13 | Coupling | `scripts/check-coupling.sh ui-probe` at the repository root | passes |
| 2026-11-13 | The whole suite, by the lead | `python3 -B -m unittest discover -s plugins/ui-probe/tests` from an archive of the reviewed code, on macOS and in a Linux container (Python 3.12, ffmpeg 7.1 with libx264, a user that is not root) | 1,005 tests, `OK` on both, nothing skipped |
| 2026-11-13 | The gate of 0.9.0 against this version, by the lead | the released 0.9.0 as installed and this version's script from the same archive: `gate` on every saved run folder of the project that uses the probe, 1,007 folders | the same exit status and text in all 1,007: a step without `scope` is judged as before |
| 2026-11-13 | Live, by the lead, on an Android 14 emulator | a settings screen with three identical switches; the middle one flipped off and on, `scope` with nothing allowed outside, three runs, twice: first with the second form of this version, then with the final one; the Mac was loaded (13 to 22) | in every flip the two neighbouring rows were redrawn for one frame and the check saw it, 17 to 62 ms, below the hand mostly and failing `came_back` when over 50 ms. The second form reported every flip off as a move of the control: the switch had only changed its look and the search matched the identical switch above it. The final form (a move needs a unique match, gone from the old place, not already at the new) reported no move in six flips |
| 2026-11-13 | Independent review | a reviewer that did not write the code, default-refute, over three rounds on synthetic recordings and films encoded with libx264 | fixed, each with a test that fails without it: a wrapper node making the whole row the control; a large card falling back to its label; the step before still moving; slides on repeating content; thin strokes not seen; key-frame noise joined into a change. Stated as limits: faint thin text under `join_level`, a constant-rate recording compressed harder than about crf 23, a slide on rows of identical height |

| Fault planted | Test that turned red |
|---|---|
| a change of exactly 50 ms taken as below the hand | `test_50_ms_fails_and_49_does_not` |
| a change still there at the step's end not failed | `test_a_caption_that_appears_under_the_control_and_stays` |
| the control's move not failed | `test_a_tapped_chip_that_moves_70_px` |
| no band searched for a slide | `test_a_tapped_chip_that_moves_70_px` |
| the control's box not widened by a block | `test_a_glow_just_around_the_control_is_the_controls` |
| the allowed regions excusing nothing | `test_a_keyboard_rising_into_a_declared_region` |
| the whole screen allowed excusing nothing | `test_a_theme_change_with_the_whole_screen_allowed` |
| a slide searched from the old content, where a repeating screen matches | `test_rows_that_change_for_77_ms_and_come_back_fail` |
| a loose match taken for a move (no absolute closeness) | `test_rows_that_change_for_77_ms_and_come_back_fail` |
| a plain patch searched for a shift | `test_a_plain_switch_in_a_list_of_like_switches_did_not_move` |
| the move threshold not scaled to the width | `test_the_move_threshold_is_a_fraction_of_the_width` |
| the search started at the first node with the matched bounds (a wrapper) | `test_the_search_starts_at_the_node_find_matched_not_a_wrapper` |
| a clickable element over the cap falling back to the label's box | `test_a_clickable_element_over_a_quarter_of_the_app_is_unknown` |
| no check that the screen was at rest before the input | `test_a_toast_fading_out_from_the_step_before` |
| a screen not at rest not made unknown | `test_a_toast_fading_out_from_the_step_before` |
| the frame before the half second not handed over | `test_the_wiring_hands_over_the_half_second_before` |
| an ambiguous match taken for a slide | `test_a_line_like_the_lines_around_it_is_no_slide` |
| no closing before grouping | `test_thin_strokes_in_other_rows_for_77_ms_fail` |
| a closing of 5 px instead of 3 | `test_two_noise_groups_8_px_apart_stay_apart` |
| the control's move not required to be unique | `test_a_move_among_identical_chips_cannot_be_placed` |
| content already at the new place before the input still taken for a move | `test_with_one_lookalike_only_it_did_not_move_either` |
| every pixel over 10 joining within the closing | `test_the_switch_flipping_is_not_unknown_and_fails_nothing` (libx264) |
| the same | `test_two_noise_groups_8_px_apart_stay_apart` |
| the scope's `join_level` not handed over | `test_the_scopes_join_level_reaches_the_check` |
| the not-at-rest message without its remedies | `test_a_toast_fading_out_from_the_step_before` |
| an unknown check passing | `test_unknown_fails` |
| the first frame after the input taken as the frame before | `test_android_the_tree_box_scaled_and_the_frames_around_the_input` |
| the lookup's box winning over a declared control | `test_android_a_declared_control_wins` |
| a rehearsal replay keeping no frame | `test_a_rehearsal_replay_carries_the_matched_frame` |
| each frame compared with the one before it, not with the frame before the input | `test_rows_that_change_for_77_ms_and_come_back_fail` |
| the gate ignoring `scope` | `test_each_rule_fails_with_one_line` |

Not established by this version's author: the check on any real recording, the defaults (50 ms,
8 px, 25 %) on real screens, and a step whose control resizes itself.

### Validation, 0.11.0

**What the author of this version ran, on macOS 26 (Python 3, Swift 6.2.4), on 2026-11-13: single
test files, planted faults in scratch copies, one compile of the Swift source, and the repository's
coupling check. No recording, no camera device opened, no phone, no notification posted.** The
compiled recorder was never run: every behavioural test hands the Python module a stand-in recorder
(a short Python script) and a stand-in `osascript` on PATH. The live recording is the lead's, on the
owner's go, after review.

The Swift source is adapted from a recorder another lead wrote and ran on a real iPhone for 113 s
(read only, not edited or run here). What changed from it: the device is chosen by the Python module
from the recorder's `list` and handed to it by its whole id, so more than one phone is refused
without `--device` instead of the first being recorded; an existing output is refused instead of
deleted; the video ports are connected, or the muxed ports when the phone offers no video port, as
the original did (it recorded a phone for 113 s that way), and never an audio port; the recorder
holds its own cap of 300 s; it prints a JSON
summary (device, model, UTC start and stop, seconds recorded, failure, the ports offered) for
`film.json`.

A review of the first form (stand-ins only) found: the notice put the owner's words into the script's
text through a JSON escape, so curly quotes or a dash made osascript fail and every recording refused;
`os.system`, `subprocess.check_call` and `NSAppleScript` each left the static tests green, which were
deny-lists; `film.json` was written in place and said nothing of an interruption; the cached binary
was trusted without a check; a device that is not a phone could be recorded; two runs could share a
folder; nothing recorded what ports the phone offered. All are fixed with tests. The author checked
how osascript passes arguments once, with a script that only returns them (it posts nothing):
`osascript -e 'on run argv' -e 'return ...' -e 'end run' -- "<curly quotes and a dash>" "ui-probe"`
returned the text unchanged and a count of two.

An addendum by the lead set the port choice: a first form of this round connected video ports only
and would have refused a phone that offers its picture on a muxed port alone, which the recorder
that filmed a phone may have used; nobody kept which port type that phone offered. The recorder
now connects the video ports, else the muxed ones, waits on the connection it made, and reports
the port types offered and which were connected.

Last, the sound guard: a muxed port is the combined stream, so a film recorded from one may hold
sound. After the recording stops and before success is reported, the recorder opens the film with
AVURLAsset and counts its audio tracks (exit 8 with sound, 9 when the file could not be read).
The author checked the counter once, as a separate scratch program, on two movies made with ffmpeg
for the purpose (a grey picture, and the same with a sine tone): it read 0 and 1, and -1 for a
missing file; the program and the movies were deleted. No camera was involved.

| Date | What | Against | Result |
|---|---|---|---|
| 2026-11-13 | New tests | `python3 -B -m unittest test_phone_0110` in `plugins/ui-probe/tests`: `film.json` before and after a recording (written whole, `interrupted` on Ctrl-C), the notice before the recording with the owner's words passed as an argument and no recording without it, the notice script compiled with `osacompile` (into a file, not run), a replaced cached binary not run, the lock, a device that is not a phone, a failed recording, `list`, every refusal (the cap at 300 s, `--go` and `--build`, the environment, an existing output, several phones, none, a system other than macOS), the cache keyed by the source's SHA-256 with a stand-in `swiftc`, a failed compile, a real compile, the static checks on the Swift source, the phone module and the command line, `doctor --phone`, the installer's file list, the docs | 39 tests, `OK`; the real compile ran here (Swift 6.2.4, no warnings when compiled by hand the same way into a scratch folder; that binary was deleted, never run) |
| 2026-11-13 | Existing tests, one file at a time | the 23 files of 0.10.0, `test_installers` included (the review's round touched only `phone.py`, `phone_screen.swift`, the README, the CHANGELOG and `test_phone_0110`; after it `test_docs`, `test_installers` and `test_run_and_cli` were run again) | each file `OK`. One existing test changed: `test_outside_0100`'s docs test no longer pins the plugin's version; `test_phone_0110` pins 0.11.0 |
| 2026-11-13 | That the tests can fail | 48 faults, one at a time, each in its own copy of the plugin, then the one test named below | each turned its test red. Two first stayed green and were fixed in the tests: a planted call from another verb was first only a comment, and a compiler that fails after writing its output was not tried |
| 2026-11-13 | Coupling | `scripts/check-coupling.sh ui-probe` at the repository root | passes |

| Fault planted | Test that turned red |
|---|---|
| the cap at 600 s | `test_the_hard_cap_of_300_s` |
| no cap check in Python | `test_the_hard_cap_of_300_s` |
| no cap in the recorder | `test_the_swift_source_has_its_own_hard_cap_and_no_watch` |
| --go read from the environment | `test_go_and_build_are_never_read_from_the_environment` |
| a --build of one word accepted | `test_go_and_build_are_required` |
| film.json not written before the recording | `test_film_json_before_and_after` |
| no notice posted | `test_the_mac_posts_a_notice_before_the_recording_starts` |
| a notice that failed ignored | `test_no_notice_no_recording` |
| list without the 9:41 note | `test_list_says_the_status_bar_reads_9_41` |
| with several phones, the first | `test_more_than_one_device_without_device_is_refused` |
| an existing output accepted | `test_an_existing_output_is_refused` |
| the recorder deletes an existing output | `test_an_existing_output_is_refused` |
| any system accepted | `test_any_system_but_macos` |
| the recorder starts a process | `test_the_swift_source_starts_no_process_and_manages_no_device` |
| the phone module imports the probe | `test_the_phone_module_imports_nothing_of_the_probe` |
| another verb calls the recorder | `test_no_other_verb_calls_it` |
| the cache not keyed by the source | `test_the_cache_is_keyed_by_the_sources_sha256_and_compiled_once` |
| a failed compile taken as built | `test_a_failed_compile_is_an_error_and_caches_nothing` |
| doctor --phone without swiftc | `test_doctor_phone` |
| a start on connect | `test_the_swift_source_has_its_own_hard_cap_and_no_watch` |
| a failed recording recorded as done | `test_a_failed_recording_is_recorded_as_failed` |
| the installer copies no recorder source | `test_the_installer_copies_the_recorders_source_and_no_binary` |
| doctor --phone not handed to the checks | `test_the_command_line_asks_for_the_phone_checks` |
| the owner's words written into the script | `test_the_owners_own_words_reach_the_notice_as_they_are` |
| os.system in the phone module | `test_the_phone_module_starts_only_its_four_known_processes` |
| subprocess.check_call in the phone module | `test_the_phone_module_starts_only_its_four_known_processes` |
| pty.spawn in the phone module | `test_the_phone_module_starts_only_its_four_known_processes` |
| NSAppleScript in the recorder | `test_the_swift_source_imports_three_frameworks_and_calls_nothing_denied` |
| another framework imported by the recorder | `test_the_swift_source_imports_three_frameworks_and_calls_nothing_denied` |
| an exec call in the recorder | `test_the_swift_source_imports_three_frameworks_and_calls_nothing_denied` |
| film.json written in place | `test_film_json_is_written_whole_or_not_at_all` |
| an interruption not recorded | `test_an_interrupted_recording_says_so` |
| the cached binary's SHA-256 not checked | `test_a_replaced_binary_is_not_run` |
| the recorder's SHA-256 not in film.json | `test_film_json_before_and_after` |
| a device that is not a phone taken without --device | `test_a_device_that_is_not_a_phone_needs_device` |
| the lock not exclusive | `test_two_runs_cannot_share_a_folder` |
| the ports not in film.json | `test_film_json_before_and_after` |
| the recorder emits no ports | `test_the_swift_source_attaches_the_picture_only` |
| an audio port connected | `test_the_swift_source_attaches_the_picture_only` |
| muxed ports connected beside the video ones | `test_the_swift_source_attaches_the_picture_only` |
| the wait looks for a video connection only | `test_the_wait_accepts_the_connection_it_made` |
| what was connected not in film.json | `test_a_phone_with_a_muxed_port_only_says_so_in_film_json` |
| the recorder does not say what it connected | `test_the_swift_source_attaches_the_picture_only` |
| the sound tracks not counted | `test_the_film_is_checked_for_sound_after_it_stops_and_before_success` |
| a film that holds sound not failed in phone.py | `test_a_film_that_holds_sound_fails_and_is_not_deleted` |
| audio_tracks not in film.json | `test_a_film_that_holds_sound_fails_and_is_not_deleted` |
| the recorder reports success with sound | `test_the_film_is_checked_for_sound_after_it_stops_and_before_success` |
| a film not checked for sound passed | `test_a_film_not_checked_for_sound_fails` |

Not established by this version's author: a recording of any phone; which program macOS grants the
camera access to when an agent runs the recorder; whether the phone must be unlocked; whether a
phone offers its picture on a video port alone (if not, the recorder refuses it).

### Validation, 0.11.0: the recorder's reviews and the handover

**What this author ran, on macOS 26 (Python 3.12.8, Swift 6.2.4, ffmpeg), on 2026-11-13: single test
files, planted faults in scratch copies of the plugin, compiles of the Swift source, and scratch
programs holding only the recorder's track reading or only its line reader. The compiled recorder,
`ui-probe phone list` and `ui-probe phone record` were never run; no capture device was opened, no
notice posted, no phone touched.**

The first live run (the lead, on the owner's go: macOS 26, an iPhone on a cable, unlocked) found
what no stand-in could: `ui-probe phone list` found the phone in 0.8 s, but `record`, 7 s later,
listed it in one process and recorded in a second, and the second found no phone (exit 4, "0
offered"; nothing recorded, `film.json` "failed" as designed). Five listings back to back then found
the phone, none, none (each about 13.4 s: the old 8 s wait counted turns, not time), the phone again
about 32 s after the first ended, and none; earlier that day it came back about 11 s after a listing
ended. So once a process that saw a phone exits, the phone is not offered to the next process for
about 11 to 32 s (one iPhone, one Mac). This version keeps one recorder process from the listing to
the end of the recording.

Three review rounds before that (stand-ins only) are in this author's earlier commits: every track of
the film read and audio, muxed and audible tracks counted as sound; a film not reported finished
never counted (exit 10); the count's wait bounded by the clock; the lock taken first and `film.json`
created only if absent; `sound_checked`, `film_on_disk` and `tracks` always in `film.json`; exits 8,
9 and 10 mapped without a summary; `--go` and `--build` refused when nothing in them can be read; a
final status kept on a late Ctrl-C; static allow-lists for every reference to a process function, for
the environment and for the cap.

| Date | What | Against | Result |
|---|---|---|---|
| 2026-11-13 | The track reading alone | a scratch program of the marked block in `phone_screen.swift` (no capture code: a grep for AVCapture, startRecording, CMIO and DiscoverySession found none), on movies ffmpeg made from its test sources, then deleted | picture only `["vide"]` 0; picture and a tone `["vide", "soun"]` 1; picture and timecode `["vide", "tmcd"]` 0; a tone alone `["soun"]` 1; a missing file and a file cut in half -1. A file with its index at the front, cut in half, still read `["vide", "soun"]` 1, which is why a film not finalised is never counted |
| 2026-11-13 | The count's bound | the same block, limit 1 s, on a FIFO nobody writes | 1.10 s on the main thread; called from a secondary thread whose run loop has no source, 1.02 s. The first form (a bound counted in turns) gave up there after 0.003 s |
| 2026-11-13 | The recorder's line reader alone | a scratch program of `readChoice` from `phone_screen.swift` (no capture code), fed on stdin | a line: the line, 0.00 s; end of input: nil, 0.00 s; input held open with no line, limit 2 s: nil, 2.00 s; a line after 1 s: the line, 1.01 s; 5000 bytes: nil; text without a newline then the end: nil |
| 2026-11-13 | The recording's wait alone | a scratch program of `recordFor` from `phone_screen.swift` (no capture code), fed on stdin | stdin held open, limit 2 s: ran its time, 2.08 s; stdin closed after 1 s, limit 10 s: stopped at 1.00 s; bytes at 0.5 s then closed at 1.5 s: stopped at 1.52 s (the bytes ignored); bytes and held open, limit 2 s: ran its time, 2.02 s; already at end of input: stopped at once |
| 2026-11-13 | Compiles | `swiftc -O phone_screen.swift` after each round, into a scratch folder | no warnings, no errors; each binary deleted, never run |
| 2026-11-13 | Tests, one file at a time | `test_phone_0110`, `test_docs`, `test_installers`, `test_run_and_cli` in `plugins/ui-probe/tests` | 77, 12, 5 and 20 tests, each file `OK`; no unclosed pipe (`-W always::ResourceWarning`), and no stand-in left running |
| 2026-11-13 | That the tests can fail | 72 faults, one at a time, each in its own copy of the plugin: 32 from the review rounds, 14 from the reviewer's addendum, 20 for the handover, 6 for an orphaned recorder | each turned its test red. Of the handover's, one first stayed green (a capture session opened before the choice) until the one session was pinned |
| 2026-11-13 | An orphaned recorder (a review of the handover reproduced it: `ui-probe` killed with SIGKILL 7 s into a recording, and the recorder ran to its end) | `ui-probe` run in its own process with the stand-in recorder recording for 30 s, then killed with SIGKILL | the stand-in logged the end of its stdin and stopped within a second, instead of after 30 s; `film.json` stayed `recording` and the lock stayed, as the README tells whoever finds them. A normal recording of 1.5 s ran its whole time |
| 2026-11-13 | Coupling | `scripts/check-coupling.sh ui-probe` at the repository root | passes |

The handover's planted faults: a listing process before the recorder; the id written before
`film.json` and the notice; a refusal, or a failed notice, that leaves the recorder's stdin open; the
offer waited for without a bound; an unreadable offer taken as no device; a recorder that ended
before offering waited out; a recorder that will not stop left running; the recorder's waiting note
not passed on; `list` silent when no phone is offered; the refusal without the half-minute note;
Python's offer wait raised; in the recorder, the line wait unbounded, its deadline unchecked,
discovery cut to 8 s or counted in turns, the offer not flushed, the id taken from the command line,
a capture session opened before the choice, and an unmatched line taken as a choice. The orphaned
recorder's: `communicate()` restored (it closes stdin with the id); stdin closed right after the id;
the stdin watch taken out of the recorder; end of input ignored by it; exit 12 not given; exit 12 not
mapped.

Moved static pins: the phone module's process calls are three `subprocess.run` and one
`subprocess.Popen` (were four `subprocess.run`); the recorder's argument guard reads
`arguments.count == 4` (was 5: no id on the command line); the phone module imports `select`; the
order test reads the one recorder's offer, the notice, the id and the recording (were list, notice,
recording), and then "recorder finished". For the orphaned recorder: the pinned ending gained two
lines (the failure set and `exit(12)` when the starter ended, after exit 9 and before the single
`exit(0)`), and the recording's `spin(seconds)` became `recordFor(seconds)`, so the only spin that is
not a literal is `spin`'s own parameter. The cap, the finished guard, the sound check and the single
`exit(0)` kept their order.

Not established by this author: a recording of any phone; whether the phone stays offered to the
one recorder process for the whole 60 s it may wait for its line (the live run showed a phone
offered within 0.8 s to a fresh process and withheld from the next one, not how long one process
keeps it); which port types a phone offers.

### Validation, 0.11.0: the control's move

**What the author of this fix ran, on macOS 26 (Python 3, numpy, Pillow, ffmpeg), on 2026-11-13:
`analyze` and `gate` on copies of seven saved runs, single test files, planted faults in scratch
copies, and the repository's coupling check. No device, emulator, simulator or phone; no network.**
The runs were Android 14 emulator recordings (1080 x 2400) of a test account, made by others before
this work and read only; every command that writes ran on copies. No picture from them is in the
plugin, its tests or its docs: numbers only.

The measure was chosen on those runs. For each step with `scope` (26: 18 taps on a day chip whose
strip moves 70 px up or down while the chip becomes the selected one, 8 flips of a switch among
identical switches), the frame before the input and the step's last frame were taken, and the
zero-mean normalised cross-correlation was measured over the same search as the check, once of the
grey patch and once of its gradient magnitude (corrected 2026-11-13 at the second review: the
steps split 9 up and 9 down, three of each in each of the three runs, not 12 and 6 as first written;
the ranges were right):

| Steps | Measure | At the best shift | Second best | At no shift | Already there | Moved |
|---|---|---|---|---|---|---|
| day chip, strip up (9) | grey | 0.914 to 0.934 at [0, -70] | 0.659 to 0.769 | 0.125 to 0.156 | 0.061 to 0.082 | |
| day chip, strip up (9) | gradient | 0.877 to 0.901 at [0, -70] | 0.447 to 0.549 | 0.091 to 0.128 | 0.039 to 0.102 | yes, all 9 |
| day chip, strip down (9) | grey | 0.824 to 0.826 at [0, 70] | 0.581 to 0.583 | -0.046 | -0.038 to -0.036 | |
| day chip, strip down (9) | gradient | 0.866 to 0.869 at [0, 70] | 0.490 to 0.494 | -0.050 to -0.047 | -0.040 to -0.031 | yes, all 9 |
| switch flipped off (4) | grey | 0.998 to 0.999 at [0, ±171] | 0.984 | 0.401 | 0.998 | |
| switch flipped off (4) | gradient | 0.987 at [0, ±171] | 0.783 to 0.784 | 0.727 to 0.728 | 0.987 | no, all 4 |
| switch flipped on (4) | grey | 0.572 at [52, 0] | 0.516 | 0.401 | 0.064 | |
| switch flipped on (4) | gradient | 0.727 to 0.728 at [0, 0] | 0.564 to 0.565 | 0.727 to 0.728 | none | no, all 4 |

The gradient was chosen: a move's gap to its next best shift is 0.347 or more against 0.164 for the
grey (its moves leave 0.164 to 0.255), and the flipped-on switch's grey peak lies on a
spurious shift (0.572 at 52 px, 0.056 above the next) where the gradient's lies at no shift. The
minimum, 0.7, lies 0.166 under the weakest move (0.866) and 0.135 over the best shift away from its
place on the flipped-on switch (0.565). The margin, 0.15, lies 0.197 under the smallest gap a move
left to any of the three (0.347, to its next best shift) and 0.15 over the flipped-off switch's gap
to the neighbour that was already there (0.000).

| Date | What | Against | Result |
|---|---|---|---|
| 2026-11-13 | `analyze` then `gate`, before and after | copies of the seven runs, analysed first with the plugin at `be538f5` and then with this change | the control moved in 18 of the 18 day-chip steps (before: 0), at [0, -70] or [0, 70]; in none of the 8 switch steps (before: none). Every other key of every step's record and of the run is the same, and so is every other verdict, but the slides at exactly [0, 300], which are gone (19 to 34 frames in each of the six live up-steps); the slides at [0, -91] stay. The gate adds one `moved` line per day step and says so; the saved switch run still fails `came_back` at 99 and 100 ms, and one live switch run at 62 ms, as before |
| 2026-11-13 | Tests, one file at a time | `test_outside_0100`, `test_docs`, `test_film_090`, `test_regions_090`, `test_ui_video`, `test_gate` in `plugins/ui-probe/tests` | each `OK` (73, 12, 59, 23, 41 and 30 tests) |
| 2026-11-13 | That the tests can fail | 15 faults, one at a time, each in its own copy of the plugin, then `test_outside_0100` | each turned the test named below red. In a copy, `DocsTest.test_documented` also fails, the same as in an unpatched copy: it reads the repository's CHANGELOG, which a copy of the plugin alone does not hold |
| 2026-11-13 | Coupling | `scripts/check-coupling.sh ui-probe` at the repository root | passes |

Tests changed: three tests of 0.10.0 drew chips or a label as flat rectangles told apart by their
grey alone (`test_a_tapped_chip_that_moves_70_px`, `test_a_chip_that_moves_70_px_still_moves`, and
`BoundaryTest.moved_by`). To a measure that ignores contrast those are lookalikes (0.967 and 0.885
the next best shift beside 1.0), or a few long straight edges that match a few pixels along
themselves (0.861); they now draw glyph-like strokes, and their verdicts are unchanged.

| Fault planted | Test that turned red |
|---|---|
| the control judged by the grey difference of 0.10.0 | `test_a_day_strip_that_moves_70_px_up_while_the_tapped_chip_turns_bold`, `test_and_back_down`, `test_a_button_label_that_moves_10_px_and_changes_colour`, and five more |
| no minimum | `ControlMoveBoundaryTest.test_the_minimum` |
| no uniqueness | `test_two_places_alike_cannot_be_told`, `test_a_move_among_identical_chips_cannot_be_placed` |
| not required gone from its place | `test_a_copy_that_appears_beside_a_control_that_stayed` |
| not required new at its place | `test_a_move_onto_a_place_where_a_lookalike_stood_before`, `test_with_one_lookalike_only_it_did_not_move_either` |
| already there by the margin exactly counted as new | `test_not_already_there_by_more_than_the_margin` |
| the margin at 0.05 | `test_a_copy_that_appears_beside_a_control_that_stayed` and the boundary tests |
| the control's shift at the search limit counted | `test_a_shift_at_the_search_limit_is_no_move` |
| only the 300 px limit taken for a limit, not the frame's edge | `test_the_frames_edge_is_a_limit_too` |
| a slide at the search limit told | `test_content_from_beyond_the_search_is_no_slide` |
| the move judged with the whole screen allowed | `test_the_whole_screen_allowed_is_not_judged` |
| no texture check | `test_a_flat_control_has_no_shift`, `test_a_plain_switch_in_a_list_of_like_switches_did_not_move` |
| no `move_px_min` | `test_8_px_is_a_move_and_7_is_not`, `test_the_move_threshold_and_flat` |
| what stood at the new place not measured | `test_a_move_onto_a_place_where_a_lookalike_stood_before` and the moves |
| the peak's own neighbours taken for the second best | `test_a_day_strip_that_moves_70_px_up_while_the_tapped_chip_turns_bold` and the other moves |

Not established by this fix's author: the measure on any screen but the two above; a control on the
iOS Simulator; the defaults on a phone's own recording.

### Validation, 0.11.0: the control's move, second review

**What the author of this fix ran, on macOS 26, on 2026-11-13, after an independent reviewer's
synthetic probes of the first form: the reviewer's probes (read, run against this tree), `analyze`
and `gate` again on the same seven copied runs, single test files, planted faults in scratch copies,
and the coupling check. No device, emulator, simulator or phone; no network.**

What the review found, and what changed:

- A false move: a still icon drawn over a list that scrolls 100 px was told as moved (0.708 at
  -100 px, 0.501 at no shift). The old place must now also keep at most half of the best, or at
  most 0.15 over what it would keep had everything around moved by the shift too
  (`no_shift_if_moved`: in the frame before, the content against the place as far on the other
  side). The half alone would have cost the short moves the review had confirmed: a strip moved 8
  to 11 px up keeps 0.559 to 0.572 at no shift against 0.901, with 0.601 to 0.624 expected from
  the overlap; the icon keeps 0.501 with 0.233 expected. That allowance is this author's addition
  to the lead's rule.
- The frame's edge: a chip moved to x = 0 (1.0) was refused as at the search limit. The edge is now
  a limit only when the patch one step farther, cut off by the edge, scores at least as well on what
  is left of it.
- Lookalike neighbours: a strip moved sideways 8 to 20 px, or left 40 or 70 px, was missed, another
  chip scoring 0.794 at the pitch plus the move. Uniqueness is replaced by places: every local best,
  less those whose content already stood there before the input, within 0.15 of the best; the move
  is told at the place nearest to the old one, and refused when one lies nearer than `move_px_min`.
- A stronger look change found the right shift under 0.7: dark fill 0.662, fill and bold 0.599, a
  3 px outline 0.561. From 0.4 up to 0.7 the move is now `suspected` and the gate prints a note.
- Places replacing uniqueness turned this author's synthetic switch (its knob dominating its
  edges) into a move: the knob matches 52 px away at 0.744 once its identical neighbours are
  dropped. A move now also needs something to have arrived at the new place: the part of it outside
  the old box holds at least half the edge spread the control's leading part had. The knob left
  11.5 before and 0.0 after (off to on: 2.61 and 0.0); every move measured kept 0.93 of it or more
  (real down-moves 11.4 to 11.5 before and 11.7 after; up-moves 16.6 to 18.5 and 26.6 to 29.1).
  Also this author's addition.

The real runs, analysed again (the plugin at `107ab1b` plus this change):

| Steps | Shift told | Correlation | Places | Lookalikes dropped | At no shift | No shift if moved | Edge spread ahead, before and after | Verdict |
|---|---|---|---|---|---|---|---|---|
| day chip, strip up (9) | [0, -70] | 0.877 to 0.901 | 1 | 0 to 2 | 0.091 to 0.128 | 0.085 to 0.110 | 16.6 to 18.5, 26.6 to 29.1 | moved, all 9 |
| day chip, strip down (9) | [0, 70] | 0.866 to 0.869 | 1 | 0 to 4 | -0.050 to -0.047 | -0.050 to -0.047 | 11.4 to 11.5, 11.7 | moved, all 9 |
| switch flipped off (4) | [0, 0] | 0.727 to 0.728 | 1 | 5 | 0.727 to 0.728 | none | none | not moved, all 4 |
| switch flipped on (4) | [0, 0] | 0.727 to 0.728 | 1 | 5 to 6 | 0.727 to 0.728 | none | none | not moved, all 4 |

None suspected. Every other key of every record, and every gate line, is the same as with the first
form of this fix.

| Date | What | Against | Result |
|---|---|---|---|
| 2026-11-13 | Tests, one file at a time | `test_outside_0100`, `test_docs`, `test_film_090`, `test_regions_090`, `test_ui_video`, `test_gate` | each `OK` |
| 2026-11-13 | That the tests can fail | 25 faults (the first form's 15, adapted to the new rule, with uniqueness's replaced by a place near the old one ignored and by places not limited to the margin; and the review's), one at a time in a copy, then `test_outside_0100` | each turned a test red; `DocsTest` also fails in any copy, as before |
| 2026-11-13 | Coupling | `scripts/check-coupling.sh ui-probe` | passes |

Tests changed: `test_a_move_among_identical_chips_cannot_be_placed` is now
`test_a_move_among_identical_chips_is_told_at_the_nearest_place` (the chips did move: every place
lies away from the old one and none stood there before); `test_two_places_alike_cannot_be_told` is
now `test_two_places_alike_away_from_it_are_a_move_to_the_nearer` for the same reason;
`test_a_copy_that_appears_beside_a_control_that_stayed` now asserts the place at the control's own;
the lookalike test's old place holds "HI" instead of "KEEP 81", which correlates 0.775 with the
label that left and is no longer gone by the half; the boundary tests read the peaks.

| Fault planted | Test that turned red |
|---|---|
| the grey difference of 0.10.0 | the day strip, the button, the frame's edge, and more |
| no minimum | `test_the_minimum_and_the_suspected_band`, `test_a_stronger_look_change_is_suspected_and_said` |
| a place nearer than `move_px_min` ignored | `test_places_within_the_margin`, `test_8_px_is_a_move_and_7_is_not` |
| not required gone | `test_a_still_icon_over_a_list_that_scrolls_did_not_move`, the gone boundaries |
| no half for gone (A) | `test_a_still_icon_over_a_list_that_scrolls_did_not_move`, `test_gone_by_half_of_the_best` |
| no allowance for a short move's overlap | `test_a_short_move_overlaps_its_old_place_and_is_told` |
| the margin half of gone removed | `test_gone_by_the_margin` |
| lookalikes already there kept | `test_a_move_onto_a_place_where_a_lookalike_stood_before`, the switch tests |
| already there by the margin exactly kept | `test_a_lookalike_already_there_is_dropped` |
| what stood at a place not measured | the lookalike and switch tests |
| the margin at 0.05 | `test_a_strip_that_moves_sideways_while_the_chip_turns_bold` and the boundaries |
| places not limited to the margin | the day strip and the moves |
| uniqueness restored (C) | `test_a_strip_that_moves_sideways_while_the_chip_turns_bold`, the identical chips, two places alike |
| the frame's edge always a limit (B) | `test_a_chip_against_the_frames_edge_is_no_limit` |
| the frame's edge never a limit | `test_the_frames_edge_is_a_limit_when_the_patch_would_be_cut_off` |
| the control's shift at the search limit counted | `test_a_shift_at_the_search_limit_is_no_move` |
| a slide at the search limit told | `test_content_from_beyond_the_search_is_no_slide` |
| the move judged with the whole screen allowed | `test_the_whole_screen_allowed_is_not_judged` |
| no texture check | `test_a_flat_control_has_no_shift` |
| `move_px_min` ignored | `test_the_move_threshold_and_flat`, `test_8_px_is_a_move_and_7_is_not` |
| suspected never said (D) | `test_a_stronger_look_change_is_suspected_and_said`, the band boundaries |
| the gate's note dropped (D) | `test_a_suspected_move_is_a_note` |
| the band's floor at 0.3 (D) | `test_the_minimum_and_the_suspected_band` |
| the grey for the gradient (E) | `test_an_inverted_chip_that_moves_70_px`, and three more |
| nothing need arrive | `test_a_switch_alone_whose_knob_slides_did_not_move`, the switch tests |

An addendum of the same day, from a real clip the lead holds: a counter's "0" turned "2", a copy
of the "2" of "24" standing 52 px to its right, and the slides' backward search reported a slide of
52 px. A slide now needs its source gone: the place the content came from, in the frame where the
slide is found, differs from itself in the frame before by more than the slide's tolerance
(`source_change`). On a synthetic counter the search finds the "2" at [52, 0] (0.0 levels off, 41.1
with no shift) and the source changed 0.0: no slide; bands that slide 10, 70 and 174 px (the source
vacated or overwritten) changed theirs by 18.5 to 64.3 and stay slides. On the seven copied runs no
slide verdict changes: the day steps' slides at [0, -91] stay, 50 frames before and 50 after (9, 4,
6, 11, 4, 6, 4, 4 and 2 a step), their sources changed by 16.3 to 17.0 levels against a tolerance
of at most 4; the switch steps have none; every gate line is the same. Planted: the source test
removed, and the source measured at the new place instead; each turned
`test_a_digit_that_turns_into_a_copy_of_one_that_stayed_is_no_slide` red. The rerun of every
earlier plant then found one green: with a slide told at the search limit,
`test_content_from_beyond_the_search_is_no_slide` stayed green because its source had not changed,
so the new rule refused the slide as well. Its source is now vacated (it asserts the source changed
by more than the tolerance), and the plant turns it red again. All 27 plants are red.

A third reading by the reviewer found a regression in this author's "arrived" rule: it read the
strip just past the OLD box, as deep as the shift but at most the box, which for a move longer than
the control along its axis is the gap, not the new place. A 24 px icon moved 70 px right with its
look unchanged (1.0) and a 60 px label moved 150, 200 or 280 px down were not told (2aa205b told
the icon). It now reads the part of the new box outside the old one (all of the new box for a long
move) against the patch's leading part as deep. On synthetic frames the icon moved 70 px right and
left and the label moved 150, 200 and 280 px down and up are told at the exact shift; the reviewer's
probe tells 20 to 280 px down (a 60 x 60 "OK" moved 300 px is the search limit, not told). On the
seven copied runs nothing changed, `texture_ahead` included: every move there is shorter than the
chip (168 px). Planted, the old slice on either axis turned
`test_a_move_longer_than_the_control_is_read_at_the_new_place` red; all 29 plants are red.

The margin half of "gone" (0.15 under the best) is implied by the half whenever the best is 0.3 or
more; it matters only when the overlap allowance lets a high value at no shift through. Removed
alone, it turned `test_gone_by_the_margin` red (which reads the peaks directly) and no frame test.
Not established by this fix's author: the review's look changes, diagonals and grounds on any real
recording; a real switch whose knob dominates its edges; the measure on the iOS Simulator.

### Validation, 0.11.0: by the lead

**What the lead ran or had run, on 2026-11-13. Every suite and comparison ran from `git archive` of the exact
commit under review, never from a tree a worker could still edit.**

| When | What | How | Result |
|---|---|---|---|
| 2026-11-13 | The whole suite | `python3 -B -m unittest discover -s plugins/ui-probe/tests` from an archive of the reviewed code, on macOS and in a Linux container (Python 3.12, ffmpeg with libx264, a user that is not root) | 1,120 tests, `OK` on both; in Linux two are skipped, the compile of the Swift source and of the notice script, which need macOS |
| 2026-11-13 | The gate of 0.10.0 against this version | the released 0.10.0 as installed and this version's script from the same archive: `gate` on every saved run folder of the project that uses the probe, 1,007 folders | the same exit status and text in all 1,007: a step without `scope` and a run without `phone` are judged as before |
| 2026-11-13 | The control's move on real recordings | four run folders of an Android 14 emulator, 1080 x 2400, with `scope` on the tapped steps: two live runs made with 0.10.0 (a day strip tapped six times, twice; a settings switch flipped off and on, three times) and copies of two run folders another team had saved with 0.7.1 (the same two acts); re-analysed and gated from the archive | 0.10.0 found the 70 px shift in 18 of 18 day taps and called none of them a move. This version: 18 of 18 moved, 9 at [0, -70] and 9 at [0, 70], correlation 0.866 to 0.901; 8 of 8 switch flips not moved; the saved switch run fails `came_back` at 99 and 100 ms |
| 2026-11-13 | 31 saved one-tap clips | plain screen recordings of an emulator with the window tree before and after each tap, through a glue script that supplies what a run folder would hold and calls this version's `run_outside` and `gate_outside`; a strict pass (nothing allowed) and, for the named acts, a pass with the regions a scenario author would declare | six switch flips fail `came_back` (the other rows redrawn for 84 to 144 ms) and are not moved; two day taps fail `moved` at -70 and +70 px; three theme changes pass with the whole screen allowed and fail without; a search field taking focus passes with the keyboard's region; a counter that changes beside a field is seen. One wrong verdict was found here and fixed: a counter digit told as a slide because a lookalike digit stood beside it (a slide now needs its source gone); after the fix that verdict alone changed |
| 2026-11-13 | The recorder on a real phone, first run, on its owner's go | `ui-probe phone list`, then `ui-probe phone record --seconds 20` (the form that listed in one process and recorded in a second), macOS 26, an iPhone on a cable, unlocked | the listing found the phone in under a second; the recording failed before it began: exit 4, the device not offered to the second process; `film.json` said failed, no film on disk, as designed. Five listings back to back then showed why: once a process that saw the phone exits, the phone is not offered to the next for about 11 to 32 s. This led to the one-process handover |
| 2026-11-13 | The recorder on a real phone, second run, on its owner's go | `ui-probe phone record --seconds 20` from an archive of the one-process form | recording began about 3 s after the command and ran 19.93 s; ports offered `vide`, `soun`, `clcp`, `mobj`, connected `video`, so the muxed fallback was not used on this phone; `film.json`: done, tracks `vide` only, `audio_tracks` 0, `sound_checked` true. Read by numbers only: ffprobe lists one stream, h264, 1206 x 2622, about 60 frames a second, 11.1 Mbit/s; mean brightness 136.6 of 255 in every frame with a spread of 136 inside the frame; `film --no-pictures` on a 17 s window counts 1,019 frames, 9 of them changed (by the half-second comparison, a few text-sized places), largest scattered groups 32 and 62 px against a minimum of 95. The film was deleted afterwards; no picture of it was opened or kept |
| 2026-11-13 | A test that raced a slow machine | the whole suite on macOS while the machine was heavily loaded (load average 15 to 45; 805 s instead of about 670) | one error: a handover test gave its stand-in recorder 1 s to answer. The recorder was not at fault. The class's short bounds now apply only to the test of a recorder that never offers, and `SlowMachineTest` runs six tests with the stand-ins delayed by 2 to 3 s |
| 2026-11-13 | Independent review of the recorder | a reviewer that wrote none of it, default-refute, six reads, stand-ins and real stand-in processes only (it never ran the recorder) | fixed, each with a test that fails without it: sound inside a muxed track not counted; success possible before the film was finalised; an unbounded, then a turn-counted wait; a second run overwriting a record; statuses left at "recording"; non-integer zeros read as no sound; invisible statements accepted; six planted faults the static tests had missed; a recorder that kept recording after its starter was killed. Its 36 planted faults all turn a test red. Its last line: point it at the phone, yes |
| 2026-11-13 | Independent review of the control's move and the slide rule | the same reviewer, four reads, synthetic probes through the test file's helpers | fixed with tests: a still control over a scrolling list called moved; a move among lookalike neighbours, to the frame's edge, or farther than the control's own size not told. Stated as limits: the cases under "Limits" in the README. Its last line: ship, with the limits the README states |

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
