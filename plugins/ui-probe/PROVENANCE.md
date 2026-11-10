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
