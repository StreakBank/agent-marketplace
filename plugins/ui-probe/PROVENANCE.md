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
| 2026-11-11 | A rest borders an input step: the input step takes the zone | the certifying run `and-certR-20261111T212004Z` (Android 14 emulator, this machine under a load of 16 to 18): with an uncertainty of 0.0617 s two rests failed `rest-requests`, each holding the next tap's own request, which mapped 10 to 26 ms after the tap's start mark; on the API 34 lab emulator `/system/bin/input` is `cmd input "$@"` and `input tap 1 1` returned in 0.01 s (3 of 3), where an Android 11 emulator takes 0.43 to 0.46 s. `test_borders_070.py`: that case rebuilt from its numbers; both orders; the cap at half a rest with two steps around a 0.3 s rest; two rests and two input steps; a lookup that scrolls; the first and the last rest; u = 0 | the taps hold their own requests and the rests hold 0 (the gate passes them; the same rest holding the request fails, as in the run); the windows are disjoint and meet in a short rest's middle; the marks decide between two rests and two input steps; a first or last rest is widened outward. Re-analysed copies of the three Android 0.7.0 captures with request logs: 87 captures (13 of `and-certR`, 61 of `and-reqA`, 13 of `and-req-1`), copied without their `frames/` and `sheets/` (the analysis writes them again) and analysed again: of 816 step records exactly four changed, all in `and-certR`'s `signed-in-tabs-at-rest` run-01 (`rest-browse` 1 to 0 and `open-streak` 0 to 1, `rest-streak` 1 to 0 and `open-ranks` 0 to 1); nothing else in any `run.json` differs. Gate: 82 of 87 pass before, 83 after; that run went from FAIL to PASS, and the four `and-req-1` failures (a session request at rest, in scenarios with no `periodic_requests`) are the same text before and after. The 30 `and-reqA` captures whose kept scenario holds `periodic_requests` under its earlier, unreleased name were analysed and gated, before and after, with `--scenario` naming the same scenario with the key renamed `periodic_requests` |
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
