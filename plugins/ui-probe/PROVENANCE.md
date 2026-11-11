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
