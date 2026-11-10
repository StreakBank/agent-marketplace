# ui-probe — watch a mobile app from outside

`ui-probe` runs a scripted scenario against an Android app and records what the platform and the
screen did, without anything inside the app. It answers two questions a functional test does not:

- **Is the app doing work while nothing happens?** Frames drawn, CPU used and frames composed while
  the screen is at rest.
- **What did each step look like on the way?** The sequence of visual states a tap passes through,
  read from a screen recording: a clean transition, a transition through a skeleton or a flash, a
  flicker that leaves a state and returns to it.

It knows a package name and screen coordinates. It names no app, screen, endpoint or UI framework.

## Three kinds of result, three rules

| Kind | What it is | What to do with it |
|---|---|---|
| Exact | A count that happens because the app did something: frames and CPU at rest, frames for a step that waits on nothing, trace sections per step | Gate it. Equality or a budget, no tolerance |
| Structural | The order of visual states a step passes through (its signature) | Gate it against an approved signature |
| Statistical | Every duration, and any count that time or the network drives (frames in a fling, in a cross-fade, in a skeleton that animates until data arrives) | Report the distribution. Never gate it on an emulator or simulator. Compare base and head with `compare` |

The same emulator on the same build can report a launch time anywhere within a factor of two
depending on host load, while its rest-window frame count stays at zero. Keep the two apart.

## Requirements

- Python 3.9 or newer with `numpy` and `Pillow`
- `adb` on `PATH`, and `ffmpeg` 5.1 or newer with its `ffprobe` (older releases lack the
  `-fps_mode` option the recording analysis uses; Ubuntu 22.04, for one, ships 4.4)
- An Android device or emulator reachable over adb, with the app installed. No root and nothing
  added to the app are needed. A debuggable build is not needed for the frame, CPU and recording
  checks. Trace sections the app's own code emits (`--sections` with `count_sections`) were
  recorded on Android 14 from a build that was neither debuggable nor profileable; other Android
  versions are untested. A section name that never appears in the trace for the app is reported
  as not observed, never as a count of zero.

`ui-probe doctor [--serial <serial>]` checks all of this, including the ffmpeg version (a version
string it cannot read is a warning, not a failure).

## Install

As a Claude Code plugin:

```sh
claude plugin marketplace add StreakBank/agent-marketplace   # once per machine
claude plugin install ui-probe@agent-marketplace
```

The command-line tool:

```sh
sh scripts/install.sh                    # prints the plan
sh scripts/install.sh --apply            # copies this version and writes a `ui-probe` launcher
sh scripts/install.sh --apply --force    # also replaces a same-version copy whose files differ
```

The launcher runs the copied version until the installer is run again. A copy of the same version
with different files is not replaced without `--force`. The scripts can also be run in place:
`python3 scripts/ui_probe.py <command>`.

## Quickstart

1. Write a scenario (see below) for your app: launch, rest, the interactions you care about, rest.
2. Run it a few times. A fresh output directory per batch; raw evidence is never overwritten.

   ```sh
   ui-probe run --serial <serial> --scenario <scenario.json> --out <dir> --runs 5 --trace
   ```
3. Check the counts.

   ```sh
   ui-probe gate <dir>
   ```
4. Look at what happened. Each run directory has `sheets/<step>.png`: every recorded frame of the
   step, labelled with its time, the gap since the previous frame and the platform's render time.
   `ui-probe zoom <run-dir> <a> <b>` magnifies what changed between two frames.
5. Read the spread before believing any timing: `ui-probe summary <dir>`.

## Scenario file

```json
{
  "name": "open-and-return",
  "package": "<applicationId>",
  "activity": "<launch component, optional>",
  "until_quiet": true,
  "quiet": {"ms": 800, "max_ms": 15000},
  "steps": [
    {"id": "rest-start", "do": "rest", "ms": 5000, "meminfo": true},
    {"id": "open", "do": "tap", "x": 0.85, "y": 0.93},
    {"id": "rest-open", "do": "rest", "ms": 5000},
    {"id": "pull", "do": "swipe", "from": [0.5, 0.3], "to": [0.5, 0.75], "ms": 600, "judge": "state"},
    {"id": "back", "do": "key", "code": 4, "expect": {"signature": "T"}},
    {"id": "rest-end", "do": "rest", "ms": 5000}
  ],
  "count_sections": ["<a trace section name your framework emits>"],
  "gate": {"rest_cpu_pct_max": 2.0}
}
```

- Coordinates are fractions of the screen, so one file serves any resolution.
- `rest` does nothing for `ms` and is where the at-rest checks apply. Put one after launch and
  after each interaction you care about. `"meminfo": true` on a rest step records the app's
  memory at its end.
- A rest is expected to happen with the app in front. To measure the app at rest behind another
  app (after a key step that sends it to the background, say), mark the rest
  `"background": true`; the gate then expects another app in front and fails if the app still is.
- `until_quiet` (scenario default or per step) waits after an action until neither the app's own
  frame count nor the display's has changed for `quiet.ms` of real time, up to `quiet.max_ms`,
  before the step ends. Watching the display as well covers animations the system draws for the
  app. Without it a step waits a fixed `settle_ms`. Use it: a fixed wait is wrong the moment the
  network is slower. With it, a step's `settle_ms` becomes a floor before polling starts.
- Launch always waits `launch_settle_ms` (default 6000) first, then for quiet if asked. Apps keep
  working after their first frame: the stock Settings app draws about thirty frames that change
  nothing visible between 1.5 and 6 seconds after it starts. A rest window placed too early
  reports that as work at rest, which it is, but it is start-up work.
- A `swipe` is judged as motion. Mark it `"judge": "state"` when the screen should end where it
  began (a pull that springs back, an overscroll), so that it is classed like a tap.
- `count_sections` is the adapter for framework knowledge: with `--sections` the probe counts the
  trace sections you name, per step. It learns nothing about the framework itself.
- `expect` per step: `signature` (a string), `frames` and `frames_max` (integers),
  `sections_max` (section name to integer). These are the baselines you approve; add them once a
  screen is stable.
- Fixed waits must add up to less than the recorder's 180 s limit. Split longer scenarios.

The file is checked strictly before anything runs, because a key the tool ignored would silently
change what is gated:

- `package` is an application id (dot-separated names of letters, digits and `_`). `activity` is
  letters, digits, `_`, `.` and `$`, so an inner class such as `.Outer$Inner` works.
- `quiet.ms` and `quiet.max_ms` are numbers of at least 100, with `max_ms` at least `ms`.
  `until_quiet`, `meminfo` and `background` are `true` or `false`. `count_sections` is a list
  of names.
- The `gate` block takes only `rest_frames_max` (an integer) and `rest_cpu_pct_max` (a number).
- A key that is unknown, or that does not apply to the step's kind (`meminfo` on a tap,
  `settle_ms` on a rest), is an error. Keys whose name ends in `comment` are allowed anywhere.

## What a run leaves on disk

```
<dir>/run-01/
  capture.json          device facts, the scenario as run, host load, the tracer's output
  device-script.sh      the exact script that ran on the device
  raw/                  what the device script wrote:
    marks.txt             step marks on the device's boot clock
    am-start.txt          the launch command's output
    gfx-<step>.txt        the platform's frame records at the end of each step (launch included)
    sf-<step>.txt         the compositor's statistics at the end of each step
    cpu-<step>.txt        the CPU reading taken first at the end of each step, with its time stamp
    cpu-<step>.after.txt  the CPU reading taken last, after the dumps
    pid-<step>.txt        the app's process id at the end of each step
    fg-<step>.txt         the resumed activity at the end of each step: which app was in front
    mem-<step>.txt        memory, for rest steps with "meminfo": true
    screen.mp4            the recording (unless --no-video)
    sr-exit.txt           the recorder's exit status
    sr-alive.txt          whether the recorder was still running when the scenario ended
    done.txt              written by the script's last line
  trace.pftrace         with --trace or --sections
  run.json              the analysis
  video-frames.json     every recorded frame: time, gap, whether it changed the app's area
  frames/               one thumbnail per recorded frame
  sheets/<step>.png     one contact sheet per step
  sheets/zoom-<a>-<b>.png and sheets/zoom-<a>-<b>-zoomtmp/   written by `zoom`
```

`analyze`, `gate`, `summary` and `compare` read only these files. No device is needed to redo
them. `analyze <dir>...` redoes the whole analysis of every capture it finds; `sheet <run-dir>`
does the same for one run. Both rewrite `run.json`, `video-frames.json`, `frames/` and the sheets
from the raw files, and remove the old `run.json` first, so a failed analysis never leaves an
earlier result standing.

## Signatures

Frames of a step are grouped into bursts (a pause over 350 ms ends one) and each burst is classed
from the pixels alone:

| Class | Meaning |
|---|---|
| `T` | transition: ends in a different state; every frame lies between the start and the end |
| `TD` | transition with a detour: passed through frames that resemble neither (a skeleton, a blank, a flash) |
| `E` | excursion: left its state and came back (flicker, a replayed animation, a pull that springs back) |
| `M` | motion: a swipe, judged by continuity |
| `t`, `m` | a small change that stays, or one too small to be a state change. Counted, not part of the signature |
| `s`, `n` | only the system bars changed, or encoder noise. Never the app |

A step's signature is its `T`, `TD`, `E` and `M` bursts in order, for example `TD` for a first open
that shows a skeleton and `T` for the repeat.

## The gate

`ui-probe gate <dir>...` checks every capture it finds against every step of its scenario, and
exits 1 on a violation.

| Check | Fails when |
|---|---|
| Frames at rest | the app drew more than `rest_frames_max` frames (default 0) in a rest step, or no count of its frames and no aligned recording exist |
| Recording at rest | frames were composed that changed nothing on screen, or the app's area changed. Only an aligned recording decides, and only while the app is in front; otherwise these are notes |
| CPU at rest | the app used more than `rest_cpu_pct_max` percent of one core (default 2.0) |
| Quiet | a step with `until_quiet` never went quiet. The message says whether the app's own frame count kept changing, or only the display's (a system animation, the keyboard, another window) |
| Process | the app's process was not running at the end of a step, more than one process carried its name, or it changed during the scenario |
| Launch | the launch command printed an error or did not report completion, or no frame of the app was counted while it launched |
| Foreground | another app's activity was the resumed one at the end of launch or of a rest step (a tap that left the app, a permission dialog, the launcher after a back press too many), or the app was in front in a rest marked `"background": true` |
| Recording | a requested recording is missing or unusable, the recorder exited with a non-zero status or had stopped before the scenario ended, or the scenario ran past the recorder's limit |
| Trace | a requested trace is missing |
| Expectations | a step's `expect` is not met, or cannot be checked (no aligned recording for a signature, a section the app never emitted, a trace that lost data) |
| Missing step, scenario | a step of the scenario has no record in the run, or its kind differs |
| Analysis | a capture has no `run.json`, or one older than the capture: run `ui-probe analyze` |

Frames are counted three ways and the largest available count decides, for the rest checks and
for `expect` alike; the violation names the source. The three are the platform's own records for
apps drawn by its standard renderer, the compositor's statistics for every layer the app owns
(any toolkit, including a surface the app draws itself), and, with `--trace`, the compositor's
frame timeline. An app whose views are idle while a surface of its own keeps drawing is caught.
A count that could not be read (a snapshot missing, the app's layers absent from both snapshots,
a step outside the trace's span, a trace that lost data) is unknown, never zero. If no count and
no aligned recording exist, the gate fails rather than pass a rest window it could not observe.

A rest window with zero frames proves a quiet screen only if the app's screen was the one in
front. Each snapshot therefore saves the resumed activity, and the gate fails a rest (and the
launch) that ended with another app in front. Where the device reports no resumed activity the
gate says so in a note and the counts still decide. Steps that are not rests may end anywhere.
While another app is in front the recording shows that app, so it is not used for the step: in a
rest marked `"background": true` the frame counts and CPU decide alone.

A trace's counts are used only if the trace is whole. The tracing service writes its own
statistics into the trace; if they report chunks overwritten or discarded in a buffer, packet
loss in a writer or a failed flush, or if the trace holds no statistics at all, every trace-based
count of the run is unknown and the gate notes why. With `--sections`, an event bundle that
reports events dropped by the kernel makes the section counts unknown in the same way; frame
counts from the trace are kept.

The at-rest CPU percentage is the app's CPU time between its reading at the end of the previous
step and its reading at the end of this one, over the time between those two readings. Each
snapshot reads CPU first and again after its dumps, so the probe's own work inside the app is
charged to no step. The process total is used, so threads that exited during the step count.

A recording is placed into the step windows only by the recorder's own per-frame timestamps.
When they are absent (the analysis then matches frames to platform records, or falls back to the
recorder's start mark), or when the platform's frame rows do not line up with the step marks,
the recording's at-rest findings become notes, frame counts still gate, and an
`expect.signature` fails with "the recording could not be aligned".

Limits come from the command line, then the scenario's `gate` block, then the defaults.
`--advisory` reports the same violations and exits 0.

## Comparing two builds

```sh
ui-probe compare --base <dir-of-base-runs> --head <dir-of-head-runs>
```

Per metric the answer is `different`, `same` or `unknown`. Values that are constant on both
sides are compared exactly, given at least two runs a side.
Everything else uses a Mann-Whitney test and Cliff's delta: `different` needs both significance
(`--alpha`, default 0.01) and a large effect; `same` needs neither; anything between is
`unknown`, as is any metric with fewer than `--min-runs` (default 5) runs on a side. With no tied
values and at most 12 runs a side the p-value is exact; otherwise it is a normal approximation.
Five runs a side is the least at which `different` is reachable at the default alpha: five
against five with no overlap gives p = 2/252, about 0.008. Interleave base and head runs on the
same device so drift affects both.

`summary` and `compare` refuse a capture that has not been analysed (exit 2).

## A slow network without administrator rights

```sh
ui-probe proxy --port 8899 --delay-ms 200 --kbit 400 --log tunnels.jsonl &
adb -s <serial> shell settings put global http_proxy 10.0.2.2:8899    # an emulator reaches the host at 10.0.2.2
# ... run scenarios ...
adb -s <serial> shell settings put global http_proxy :0
```

The proxy prints `listening on <address>:<port>` once the port is bound: wait for that line
before pointing a device at it. If the port is already taken it exits 2. Without that, an older
proxy still holding the port would carry your run with its own settings, and this one's log
would stay empty.

The proxy listens on `127.0.0.1` by default. An emulator reaches that as `10.0.2.2`; a physical
device cannot reach it at all. For a physical device, either forward the port over adb and point
the device at its own loopback address:

```sh
adb -s <serial> reverse tcp:8899 tcp:8899
adb -s <serial> shell settings put global http_proxy 127.0.0.1:8899
```

or listen on an address the device can reach with `--bind <host-address>` (`--bind 0.0.0.0`
listens on every interface, and anyone on that network can then use the proxy).

The proxy adds a fixed latency per direction and paces every chunk at the rate limit, per
connection. It never decrypts: HTTPS passes as opaque tunnels, so certificate pinning in the app
is unaffected. A receiver that reads slowly slows the sender too (at most about 1 MB is held per
direction). A target that refuses the connection gets `502 Bad Gateway`. A reset or failure on
either side resets the whole connection, so neither end mistakes a broken transfer for a complete
one. Plain HTTP carries one request per connection: the request is forwarded with
`Connection: close`, and the connection ends with its response.

The log gets one line per connection when the connection ends: target, duration, bytes each way
and the error that ended it, if any. An app usually keeps one tunnel open for a whole run, so
that line is not there while the run is in progress. Stop the proxy (an interrupt or a terminate
signal is enough): it then logs every connection still open, marked "open when the proxy
stopped", and the log is a complete count of the connections it carried.

Limits: it shapes an app only if the app's network stack honours the system proxy; stop the
proxy and check that its log names the app's hosts before trusting a run. It works per connection, not per packet, so it
imposes a condition for count and sequence checks and is not a model of a radio link. It cannot
produce packet loss.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | success, including a gate that passed |
| 1 | the gate found a violation (and only that) |
| 2 | an error the tool reports itself: a bad scenario, an unanalysed capture, a device or adb failure, a missing or too old tool found by `doctor`, a `zoom` that could not extract its frames |
| 3 | an unexpected error: the traceback is printed, and it is a defect in the tool |

## Known limits

- **Android only in this version.** A recording from another platform can be analysed by the
  same classifier, but nothing here drives one.
- **Per-frame timings assume the device has not slept since boot.** Frame rows use the monotonic
  clock and step marks the boot clock; they agree only then. The run is flagged when they do not
  line up. Counts are unaffected; the recording then no longer decides a rest window.
- **Per-frame records hold the last 120 frames.** Keep steps short enough that fewer are drawn
  between two snapshots, or rely on the totals, which are not limited.
- **A trace covers only the time it holds.** A trace that stopped early gives no trace counts
  for the steps outside its span; the run lists those steps. A trace whose buffer overwrote data
  cannot say which steps lost it, so all of its counts are dropped.
- **The foreground check looks at the end of a step, at launch and at rests only.** An app that
  left the foreground and returned within one step is not noticed, and a dialog the app shows in
  its own window is still the app.
- **The detour threshold was calibrated on two cases in one app.** Treat `TD` against `T` as a
  signal to look at the sheet, and tune `DETOUR_RATIO` if your screens disagree.
- **`--trace` stops every on-device trace owned by the shell user** when the scenario ends.
- **The compositor's statistics are a device-wide switch.** A run turns them on and clears them
  at its start and turns them off at its end, however it ends: the device script restores them on
  an error or a signal, and the host switches them off and stops the recorder when a run fails.
- **Timings from an emulator or simulator say nothing about a phone.** The host's processor and
  graphics stack set them.

## Project shim

Device names, package ids, scenario locations, the reference device profile and whether the gate
blocks a landing are project facts. Keep them in your project's own rules; `SHIM-TEMPLATE.md`
shows the shape.
