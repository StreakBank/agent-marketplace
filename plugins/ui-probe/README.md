# ui-probe — watch a mobile app from outside

`ui-probe` runs a scripted scenario against an Android app and records what the platform and the
screen did, without anything inside the app. It answers two questions a functional test does not:

- **Is the app doing work while nothing happens?** Frames drawn, CPU used and frames composed while
  the screen is at rest.
- **What did each step look like on the way?** The sequence of visual states a tap passes through,
  read from a screen recording: a clean transition, a transition through a skeleton or a flash, a
  flicker that leaves a state and returns to it.

It knows a package name, screen coordinates and, for a tap that names its target, the target's
text or content description. It names no app, screen, endpoint or UI framework.

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
string it cannot read is a warning, not a failure). With a serial it also says whether the device
has `uiautomator`, which a tap with `find` needs to read the window tree; a scenario without
`find` does not, so its absence is reported but is not a failure.

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

More keys, one example each:

```json
{"id": "type-code", "do": "text", "text": "000000"}
{"id": "open", "do": "tap", "find": {"text": "Sign up"}}
{"id": "open-below", "do": "tap", "find": {"text": "Settings", "scroll": {"from": [0.5, 0.8], "to": [0.5, 0.3], "max": 5}}}
{"id": "open-slow", "do": "tap", "find": {"desc": "Phone number", "nth": 1, "timeout_ms": 15000}, "until_quiet": true, "quiet": {"max_ms": 30000}}
{"id": "rest-field", "do": "rest", "ms": 5000, "allow": {"frames_max": 12, "reason": "the caret blinks twice a second"}}
```

- `text` types into whatever has focus. The text is at most 200 characters of letters, digits,
  space and `@ . _ + -`; anything else is an error that names the character. It takes the keys a
  `key` step takes and the same fixed wait (2500 ms), and its recording is judged like a tap's: a
  state change, not motion.
- `find` on a tap names its target instead of `x` and `y` (one or the other, never both):
  exactly one of `text` (the element's text) or `desc` (its content description), matched exactly,
  on an element of the scenario's `package` (`package` in `find` names another one, for a system
  dialog say). Without `nth` the match must be unique; `nth` (from 1) picks among several in
  document order. Only an element on the screen counts. The lookup retries until the element is
  there, on the screen and still, so it doubles as a wait for the screen, up to `timeout_ms`
  (default: the scenario's `quiet.max_ms`, plus room for every swipe when `scroll` is given).
  `find_settle_ms` (default 1000) is the wait after it, before the counts of the step start.
- `scroll` in `find` swipes a target into view that a smaller screen or a larger font has pushed
  below the fold: while a read shows no acceptable match, the script swipes from `from` to `to`
  (screen fractions, as in a swipe step) over `ms` (default 600), waits `settle_ms` (default 800)
  and reads again, at most `max` times (1 to 20). See "Tap targets found on the device".
- `quiet` on a tap, swipe, key or text step replaces the scenario's `quiet` values for that step
  (a key it leaves out comes from the scenario). It applies only to a step that waits for quiet.
- `allow` on a rest step raises its limits on purpose, with a reason. See "Screens that are never
  still".

The file is checked strictly before anything runs, because a key the tool ignored would silently
change what is gated:

- `package` is an application id (dot-separated names of letters, digits and `_`). `activity` is
  letters, digits, `_`, `.` and `$`, so an inner class such as `.Outer$Inner` works.
- `quiet.ms` and `quiet.max_ms` are numbers of at least 100, with `max_ms` at least `ms`, in the
  scenario's block and in a step's (where the check uses the scenario's value for a key the step
  leaves out). `until_quiet`, `meminfo` and `background` are `true` or `false`. `count_sections`
  is a list of names.
- The `gate` block takes only `rest_frames_max` (an integer) and `rest_cpu_pct_max` (a number).
- `allow` takes `frames_max` and `changed_frames_max` (integers), `cpu_pct_max` (a number) and
  `reason` (required), and at least one of the three limits. `find` takes `text` or `desc` (a
  non-empty string of at most 200 characters on one line, with no tab, no other control
  character, no surrogate and no character outside the Basic Multilingual Plane: the window-tree
  dump never writes those as they are, so they could never match; the error names the character),
  `nth` (an integer of at least 1), `timeout_ms` (a number of at least 100), `package` (an
  application id) and `scroll`. `scroll` takes `from` and `to` (required, `[x, y]` between 0 and
  1, at least 0.1 apart on one axis, since a swipe that barely moves is a tap), `max` (required,
  an integer from 1 to 20), `ms` (a number of at least 1) and `settle_ms` (a number of at least 0).
- A key that is unknown, or that does not apply to the step's kind (`meminfo` on a tap,
  `settle_ms` on a rest, `allow` on a tap, `find` on a swipe, `quiet` on a step that does not wait
  for quiet), is an error. Keys whose name ends in `comment` are allowed in the scenario and in a
  step; inside `quiet`, `gate`, `expect`, `allow`, `find` and `scroll` every key must be one of the
  listed.

## Screens that are never still

Some screens draw at rest by design: a focused text field blinks its caret. Measured on a real
app on Android 14, a blinking caret drew 10 or 11 frames in a 5 s rest window (9 of 9 windows),
with no recorded frame that changed nothing and no recorded change large enough to count. Drawing
them cost 3 to 9 CPU ticks (0.6% to 1.8% of one core over 17 windows), which at the top is one
tick short of the default limit. A rest on such a screen takes an allowance for both:

```json
{"id": "rest-field", "do": "rest", "ms": 5000, "allow": {"frames_max": 12, "cpu_pct_max": 4.0, "reason": "the caret blinks twice a second"}}
```

- `frames_max` replaces the frames-at-rest limit and `cpu_pct_max` the CPU-at-rest limit for that
  step; `changed_frames_max` is how many recorded frames may change the app's area (otherwise
  none). A step's allowance wins over the command line and the scenario's `gate` block for that
  step, and applies to no other step.
- Whether a caret's blink counts as a change in the app's area depends on how large the caret
  is in the recording. On a display 1080 px wide it did not (0 changed frames in 20 windows); on
  one 720 px wide the caret of a tall code field did, in 10 of 10 frames. Give such a rest
  `changed_frames_max` as well as `frames_max`, with the same number.
- There is no allowance for recorded frames that changed nothing. A caret does not produce them;
  an animation nobody can see does, and that check is what catches it. Such frames are excused
  only when the app presented no frame by any count (see "The gate"), and that exemption cannot
  hide such an animation: it draws and presents frames, so its counts are above zero, and both the
  frame check and this one still fail it.
- A violation on such a step says the limit came from the step's allowance. A step that passed
  only because of its allowance gets a note with the value observed, the allowance and the reason,
  and `gate` lists every allowance in force under its run.
- A screen with a blinking caret never goes quiet, so the step that focuses the field (and any
  step that ends on it) would time out waiting. Give that step a fixed wait instead:
  `"until_quiet": false` with a `settle_ms` that covers the transition. A shorter quiet window
  does not help: the poll that watches for quiet takes about as long as the gap between two
  blinks. Measured on an emulator, a 400 ms window timed out on 2 of 2 such steps while the
  caret blinked every 500 ms.

## Tap targets found on the device

Coordinates break when a layout changes. A tap with `find` looks its target up on the device, in
the device script, before the step starts, so one pass serves a flow that changes state. Each read
dumps the window tree (`uiautomator dump`, about 2 s a read) and matches the attribute exactly as
the dump writes it: `&`, `<`, `>` and `"` as entities, an apostrophe and every other character of
the Basic Multilingual Plane as it is. The characters the dump does not write as they are (a tab,
control characters, characters outside that plane) are refused when the scenario is checked. Only
an element whose own `package` attribute is the scenario's package (or the step's `find.package`)
counts, so an element of the same text in another app's window is never tapped. A match counts
only when two reads in a row give it the same bounds, so a tap never goes to where an element was
while it still moved. A wrong number of matches, a read that failed while the screen animated, or
bounds that changed since the previous read is "not yet": the script waits 0.5 s and reads again,
up to the timeout; a match first seen at the timeout gets one more read to hold still. The tap
goes to the centre of the element's bounds, and `find-<step>.txt` and `run.json` record the
package it was found in.

A match counts only when it is on the screen: its bounds are not empty and its centre lies on the
display. The display's size is read once when the device script starts (`wm size`): the override
size when one is set, as when a smaller screen is emulated with `wm size <w>x<h>`, else the
physical size; the host reads it the same way, so coordinate taps and swipes use it too. Without
`nth` the match must be the only one on the screen; with it, `nth` counts the ones on the screen.
A match that is in the tree but off the screen is "not yet". With `scroll`, every read that
succeeds without an acceptable match triggers a swipe: a match off the screen, no match at all (a
list often builds only the rows it shows), or several where one is needed. A swipe is never made
after a read that failed, or while a match waits for its second read. The lookup ends `not_visible` when matches were in the tree but none (or too few) on the
screen at the timeout; the message gives their bounds and the display size, and suggests `scroll`
when the step has none. `find-<step>.txt` and `run.json` record the swipes made. The default
timeout with `scroll` is `quiet.max_ms` plus, for each of the `max` swipes, its `ms`, its
`settle_ms` and one read (about 2.5 s); the scenario's worst-case duration counts that, and one
swipe that runs on past the timeout, while the fixed-wait estimate checked against the
recorder's limit counts no swipe. Everything a lookup does, swipes included, happens before the
step's start mark and its baseline, so it is charged to no step.

A lookup is charged to no step. Reading the tree makes the app and the compositor work: two reads
inside one rest window on a perfectly still screen produced 1 frame by the platform's count, 9
recorded frames that changed nothing and 9 CPU ticks, where the windows before and after had 0. So
after a lookup the script waits `find_settle_ms`, takes a baseline reading of every counter a
snapshot reads (the platform's frames, the compositor's, CPU), and only then marks the step's
start: the step's counts run from that baseline, not from the previous step's end, and the lookup
lies outside every step's window by time, so neither the trace nor the recording places it in a
step. That is also why `find` is not offered on a rest: a lookup never falls inside a rest window.
A baseline that could not be read leaves the step's counts unknown; they are never taken from the
previous step.

When the element never appears, is never on the screen (`not_visible`) or never holds still
(`unstable`), the script
stops there, restores the device as on every other error path (the recorder stopped, the
compositor's statistics off), and the run exits 2 with a message that names the step, what was
looked for, how many elements matched at the last read (and the packages of matching elements in
other apps, if any) and where the last tree read is saved. No capture is written: the run folder
keeps `failed.json`, and `gate` fails the run (see "Runs that did not complete").

## What a run leaves on disk

```
<dir>/run-01/
  capture.json          device facts, the scenario as run, host load, the tracer's output
  failed.json           only when the run did not complete: the error, the step if known, the time (UTC)
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
    display.txt           a scenario with "find": the display size lookups checked matches against
    find-<step>.txt       a tap with "find": the lookup's result, matches and how many were on the screen,
                          reads, swipes, time, bounds, centre, package, the display size
    ui-<step>.xml         a tap with "find": the last window tree read
    gfx-<step>.base.txt, sf-<step>.base.txt, cpu-<step>.base.txt, pid-<step>.base.txt
                          a tap with "find": the baseline the step's counts start from
    lookup-failed.txt     the step whose element never appeared or never held still, when a run stopped there
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
them. `run` refuses a folder that already holds any of `capture.json`, `run.json`, `failed.json`,
`device-script.sh` or `raw/`, so the files of one attempt are never mixed with another's.

### Runs that did not complete

A run that fails after its folder exists (a lookup that timed out, a device that went away, an
interrupt) writes `failed.json` there. A folder that holds `failed.json`, `device-script.sh` or
`raw/` but neither `capture.json` nor `run.json` is a run that did not complete: `gate` fails it
(check `run`, with the saved error), `summary` lists it and leaves it out of every statistic, and
`compare` names the ones it left out on each side. A batch that stopped at its third run therefore
never passes on the two before it, and a folder holding only such runs fails the gate. `analyze <dir>...` redoes the whole analysis of every capture it finds; `sheet <run-dir>`
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
| Frames at rest | the app drew more than `rest_frames_max` frames (default 0, or the step's `allow.frames_max`) in a rest step, or no count of its frames and no aligned recording exist |
| Recording at rest | frames were composed that changed nothing on screen, or the app's area changed in more recorded frames than the step's `allow.changed_frames_max` (default none). Only an aligned recording decides, and only while the app is in front; otherwise these are notes. Both are notes, not violations, when the app presented no frame by any count (platform and per-layer both known and 0, a trace's 0 or absent); a step with `allow.changed_frames_max` keeps its allowance for changes in the app's area |
| CPU at rest | the app used more than `rest_cpu_pct_max` percent of one core (default 2.0, or the step's `allow.cpu_pct_max`) |
| Find | a tap with `find` holds no record of where its element was found, or the element was matched in another package than the one asked for |
| Run | a run folder holds `failed.json`, `device-script.sh` or `raw/` but no capture: the run did not complete |
| Quiet | a step with `until_quiet` never went quiet. The message says whether the app's own frame count kept changing, or only the display's (a system animation, the keyboard, another window) |
| Process | the app's process was not running at the end of a step, more than one process carried its name, or it changed during the scenario |
| Launch | the launch command printed an error or did not report completion, or no frame of the app was counted while it launched |
| Foreground | another app's activity was the resumed one at the end of launch or of a rest step (a tap that left the app, a permission dialog, the launcher after a back press too many), or the app was in front in a rest marked `"background": true` |
| Recording | a requested recording is missing or unusable, the recorder exited with a non-zero status or had stopped before the scenario ended, or the scenario ran past the recorder's limit |
| Trace | a requested trace is missing |
| Expectations | a step's `expect` is not met, or cannot be checked (no aligned recording for a signature, a section the app never emitted, a trace that lost data) |
| Missing step, scenario | a step of the scenario has no record in the run, or its kind differs |
| Analysis | a capture has no `run.json`, or one older than the capture: run `ui-probe analyze` |

A clean app's frames at rest are zero; its CPU at rest is not always. On a real app three of 80
still rest windows read 6 to 9 ticks in 5 s from background threads (the runtime's profile saver,
a thread pool finishing a request), against the default limit of 10. A fault repeats: an animation
clock that drew nothing read 65 to 86 ticks in 5 of 5 windows. So one window a few ticks over the
limit, with no frame, is run again before it is judged; the same step failing twice is a finding.

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

What the recording shows during a rest is the app's doing only if the app presented a frame. The
display composes a frame whenever anything on it changes (a status-bar icon, say), sometimes two
in quick succession, and the recorder keeps them: one that changed nothing on screen, or one whose
re-encoding noise along text edges puts a small patch in the app's area over the change threshold.
So when the app presented no frame by any count (the platform's count and the compositor's
per-layer count both known and 0, and a trace, if recorded, counted none either), recorded frames
that changed nothing and a recorded change in the app's area are a note, not a violation: the
display composed them for a layer that is not the app's, or the change came from the encoder. One
note covers both when both occur, and it names the command to look at them
(`ui-probe zoom <run-dir> <a> <b>`). If any count is unknown or above 0, both remain violations as
before; an animation nobody can see presents frames, so its counts are above zero and it still
fails both the frame check and the recording check. A step with `allow.changed_frames_max` is
judged against that allowance for changes in the app's area as before; there is still no
allowance key for frames that changed nothing.

Limits come from the step's `allow`, then the command line, then the scenario's `gate` block,
then the defaults. `--advisory` reports the same violations and exits 0. `gate --scenario <file>`
gates with a revised copy of the run's own scenario (a limit, an allowance): it refuses (exit 2) a
scenario whose `name` differs from the one stored with the run, and each `allowance:` line then
says that it came from `--scenario`.

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

`summary` and `compare` refuse a capture that has not been analysed (exit 2). A run that did not
complete is listed by `summary` and left out of every statistic; `compare` prints a note naming the
ones it left out on each side.

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

A backend on the host, reached from an emulator, needs one more argument. The guest names the
host's loopback by an alias, `10.0.2.2`, that the host itself cannot connect to: an app that talks
to `10.0.2.2:8080` reaches the host directly, but once the system proxy is set the proxy (on the
host) is asked to connect to `10.0.2.2` and fails. `--map-host 10.0.2.2=127.0.0.1` connects a
CONNECT target or a plain HTTP request whose host is `10.0.2.2` to `127.0.0.1` instead, on the
same port. It is repeatable (`--map-host FROM=TO` once per host); each side is a host name or an
IP address with no port, scheme or path (an IPv6 address with or without brackets), names compare
without regard to case, and a host mapped twice or to itself is refused. Only the connection is
redirected: the request goes on as it was sent, so a plain request keeps its `Host` header and its
absolute URI.

```sh
ui-probe proxy --port 8899 --delay-ms 200 --kbit 400 --map-host 10.0.2.2=127.0.0.1 --log tunnels.jsonl &
```

The proxy adds a fixed latency per direction and paces every chunk at the rate limit, per
connection. It never decrypts: HTTPS passes as opaque tunnels, so certificate pinning in the app
is unaffected. A receiver that reads slowly slows the sender too (at most about 1 MB is held per
direction). A target that refuses the connection gets `502 Bad Gateway`. A reset or failure on
either side resets the whole connection, so neither end mistakes a broken transfer for a complete
one. Plain HTTP carries one request per connection: the request is forwarded with
`Connection: close`, and the connection ends with its response.

The log gets one line per connection when the connection ends: the target as the client named it,
the address connected to (`connected_to`: the target itself unless `--map-host` changed it),
duration, bytes each way and the error that ended it, if any. An app usually keeps one tunnel open for a whole run, so
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
| 2 | an error the tool reports itself: a bad scenario, an unanalysed capture, a device or adb failure, an element a `find` tap never found, a missing or too old tool found by `doctor`, a `zoom` that could not extract its frames |
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
- **A lookup sees what the window tree exposes.** An element with no text and no content
  description cannot be found by `find`; a screen that animates for longer than the timeout
  fails every read. Each read takes about 2 s, and a match needs two reads that agree, so a lookup
  overshoots its timeout by up to two reads (and, with `scroll`, by one swipe and its settle time).
  "On the screen" means within the display: an element under the keyboard, the navigation bar or
  another window counts as on the screen, and a tap there reaches whatever covers it.
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
