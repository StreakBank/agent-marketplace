# ui-probe — watch a mobile app from outside

`ui-probe` runs a scripted scenario against an Android app (or, with rests, taps and swipes, an app
on the iOS Simulator) and records what the platform and the screen did, without anything inside the app. It answers two questions a functional test does not:

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

- For the iOS Simulator instead: Xcode (`xcrun simctl`, and `xcodebuild` for a scenario with a tap or a
  swipe, which builds the input agent on first use) and a booted simulator
  with the app installed; `adb` is then not needed. See "The iOS Simulator".

`ui-probe doctor [--serial <serial> | --simulator <udid>]` checks all of this, including the ffmpeg version (a version
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

## The iOS Simulator

`ui-probe run --simulator <udid> --scenario <file> --out <dir>` runs a scenario on a booted iOS
Simulator instead of an Android device (exactly one of `--serial` and `--simulator`, neither
empty). A simulator gives no input from a command line (`simctl` has no tap) and nothing to detect
quiet with; its exact counts of the app's frames come from its log store (see "The simulator's log
store"). This version runs rests, and taps and swipes at
fractions of the display through a small UI-test agent (`ios-agent/`, see "Input on the iOS
Simulator"), driven from the host; there is no device script. There are no lookups and no typing
on a simulator in this version. A simulator run reads its log store.

**What it means for a scenario author: any frame at rest fails.** With the status bar frozen
nothing but the app has a reason to make the display compose: a frame the app presented (its log
store's count) or a frame recorded at rest fails the rest, with one exception, the encoder refining a
still picture (below). A screen that is never still by design (a blinking caret) needs an allowance
on its rest (`allow.frames_max`, `allow.changed_frames_max`, `allow.updates_max`); frames that
changed nothing have none.

- **The scenario.** `package` is the bundle identifier (letters, digits and `-` in dot-separated
  names). `activity`, `quiet` and `count_sections` must be absent and `until_quiet` false or
  absent, in the scenario and in a step. A step is a `rest` of at least 1000 ms (the CPU time is
  read in steps of 0.01 s) without `meminfo` or `background`, a `tap` with `x` and `y`, or a
  `swipe` with `from`, `to`, `ms` and `judge`; a tap or a swipe waits its `settle_ms` after the
  input (default 3000 for a tap, 2500 for a swipe, as on Android). In `expect`, a rest takes only
  `signposts_max` and a tap or a swipe only `signature` and `signposts_max`. A swipe lasts at most
  60000 ms (what the agent accepts). Refused before anything runs, each with its own
  message: `find` on a tap and a `text` step (a lookup or typing reads the app's accessibility
  tree, which changed the app at rest when measured, so this version takes positions as
  fractions) and a `key` step (a simulator has no keys to press: tap the screen's own control or
  swipe from the edge). The launch waits `launch_settle_ms` (default 6000). `launch_env` (below)
  is passed to the app. `allow` and the `gate` block work as described below. `--trace`,
  `--sections` and `--no-video` cannot be used with `--simulator`.
- **The launch environment.** `launch_env` at the top of a scenario, an object of up to 20 names
  (capital letters, digits and `_`, starting with a letter) to strings of at most 200 characters
  with no control character, is handed to the app's process: each pair is set as
  `SIMCTL_CHILD_<NAME>=<value>` in the environment of the `xcrun simctl launch` call only, which
  `simctl` passes on to the app without the prefix. Example: `"launch_env": {"API_ORIGIN":
  "https://localhost:18443"}`. The scenario is the only source: `SIMCTL_CHILD_*` variables already
  in ui-probe's own environment are not passed to the launch, and their names are saved in
  `capture.json` (`launch_env_dropped`) and given in a note. The object is saved in `capture.json`
  beside the scenario, so it is not a place for secrets. On Android `launch_env` is refused (an Android launch takes no
  environment).
- **The run.** The status bar is frozen first (`xcrun simctl status_bar <udid> override --time
  9:41`; a run whose override fails stops there). An override the user had set is replaced, and
  cleared (`xcrun simctl status_bar <udid> clear`) when the run ends, however it ends. The app is
  terminated, launched with `xcrun simctl launch` (which prints its process id) and given
  `launch_settle_ms`. Then, for each rest step: the recorder starts in a process group of its own
  (`xcrun simctl io <udid> recordVideo --codec h264 --force raw/rec-<step>.mp4`) and the run waits
  for it to print `Recording started` (20 s, else the run fails); the app's CPU time is read with
  `ps -o cputime= -p <pid>` (the app is a process on the host); the step's `ms` passes; the CPU
  time is read again; whether the recorder is still running is noted, its group gets SIGINT, and
  the run waits for it to write its file and exit (20 s, then the group is killed). The app is
  left running after the run.
- **Clean-up**, on every way out of a run the tool can see (an error, an interrupt (Ctrl-C),
  SIGTERM and SIGHUP, which also leave `failed.json`, "terminated by SIGTERM", and exit with 143 or
  129), in this order: each step's recorder is stopped at the end of that step; after the last
  step (or the one that failed), the input agent is stopped (below); then the status bar is
  cleared. Each part runs whatever the one before it raised, every wait in it has a limit, and a
  signal that arrives during it is held until it is done (the run then still ends as interrupted
  or terminated). SIGKILL cannot be caught. After one, what may be left: a recorder (`pgrep -fl
  recordVideo`, stop it by hand); the agent's `xcodebuild` and its runner app on the simulator,
  until the agent's idle limit ends them (`xcrun simctl uninstall <udid>
  dev.uiprobe.agent.uitests.xctrunner` removes the runner); and the frozen status bar (`xcrun simctl
  status_bar <udid> clear` restores the clock).
- **What is measured.** Per step: the window's real length (between the two CPU readings), the CPU
  seconds and the percentage of one core, the log store's drawables, commits and display updates
  for the window (below), and that step's recording, which keeps a frame only when the display
  changes. Its frames after the first are counted, and the same analysis as
  on Android names their classes for the reader: those that changed the app's area, those whose
  change in the app's area is encoder noise, those that changed only the bands where a status bar
  and a home indicator sit (the top 7% and the bottom 4.5%) and those that changed nothing, with
  the largest change of any pixel (on its most changed colour channel), how many pixels changed by
  8 levels or more, and how many of those where the previous picture was flat (a 5 by 5
  neighbourhood spanning less than 8 levels). The classes decide nothing. A process that is gone at the end of a window (a relaunched app
  included, since only the launched process is watched) fails the process check.
- **The gate.** CPU at rest against `rest_cpu_pct_max` (default 2.0) or the step's
  `allow.cpu_pct_max`; a CPU time that could not be read, or that went down between the two
  readings (another process holding the pid), is a violation. With the log store's counts known:
  drawables plus commits against `rest_frames_max` (default 0) or `allow.frames_max` (check
  `rest-frames`, source "log store"), and display updates against `rest_updates_max` in the `gate`
  block (default 0) or `allow.updates_max` (check `rest-updates`). The frames recorded at rest:
  every one after the first against `rest_frames_max` or `allow.frames_max`, those that changed
  nothing with no allowance, those that changed any pixel against `allow.changed_frames_max`
  (default none), the violation naming the classes; except that they are a note, the encoder
  refining a still picture a few seconds after motion, when all of these hold: the counts are known
  and all 0 for the window and for the recording's own span (from `Recording started` to the
  recorder's stop); at most 2 frames were recorded; no pixel changed by more than 24 levels on any
  colour channel at the analysis scale (270 pixels wide); no frame changed more than 2000 pixels by
  8 levels or more; and none of those lies where the previous picture was flat. Measured on 17 rest
  windows of one app: the encoder's refinement changed at most 17 levels in 28 to 193 pixels (more
  on a screen with more text), all along edges already in the picture; a 1 pt grey hairline that
  grew changed 16 levels in 6 or 7 pixels, 4 or 5 of them on a flat area, so the flat-area count
  holds it where the size of the change could not, and the pixel limit, a factor of ten above the
  largest refinement measured, only bounds a wide subtle change; a 20 pt
  spinner and a caret changed 165 or more. With the counts unknown the recorded frames are held as
  just described, without the exception. Android's 0.3.0 rule for
  frames recorded while the app presented none is not used on a simulator. A recorder that had
  stopped before its window ended (even with status 0) fails the recording check: its file ends
  early and would look clean. The checks a simulator cannot feed (waiting for quiet, frames at
  launch, which app is in front unless the run has input, `find`, trace sections) are not run, and every simulator run
  says so in a note rather than passing them by silence.
- **Measured by hand** (a debug build of an app that draws its own surface, iOS 26.1, its launch
  screen). A clean build used 0.00 to 0.03 s of CPU per 5 s window and its recording held one
  frame, the first; a build that redraws an element behind zero alpha every frame used 1.45 to
  1.46 s (29% of one core), and its recordings, about 6.8 s long, held 408 to 412 frames. Run by
  the tool: a clean build used 0.2% to 0.6% of one core in 20 of 20 rest windows (5 s to 65 s
  long) and recorded no frame after the first in 19 of them; two faulty builds recorded 299 to 304
  frames and used 25% to 33% in each of 12 windows of 5 s. The one clean window with frames was a
  65 s rest before this version froze the status bar: it recorded 2 frames when the clock changed
  (encoder noise reached the app's area), and none once the status bar was frozen.
- **On disk.** `raw/launch.txt` (what the launch printed), `raw/pid-<step>.txt`,
  `raw/rec-<step>.mp4` and `raw/rec-<step>.log` (the recorder's output), `raw/step-<step>.json`
  (the two CPU readings and their wall-clock times, the recording's span by the wall clock, the
  recorder's exit status, whether it was killed and whether it was still running when the window
  ended), `raw/launch-env.json` (the names not passed to the launch), `video/<step>/` (the frames and the analysis of
  that recording) and `sheets/<step>.png`. `analyze`, `sheet`, `gate`, `summary` and `compare` work
  on a simulator run as on any other; `zoom` needs `--step <id>`, since each step has its own
  recording. `summary` and `compare` give a simulator's values keys of their own,
  `<step>.sim_cpu_s`, `sim_cpu_pct`, `sim_window_s`, `sim_recorded_frames`,
  `sim_changed_app_frames`, `sim_noise_frames`, `sim_bars_only_frames`, `sim_identical_frames`,
  `sim_drawables`, `sim_commits`, `sim_updates` and `sim_signposts.<name>`,
  since they mean something else than Android's (CPU in seconds, not ticks; every frame of a
  per-step recording).
- **Not measured.** Lookups and typing; quiet; trace sections; memory; which app is in front in a
  run without input; and timings as a phone's (the host's processor and graphics run
  the simulator). `ui-probe doctor --simulator <udid>` checks `xcrun simctl`, that the simulator
  exists and is booted, and `ffmpeg` and `ffprobe`.

## The simulator's log store

A simulator run reads its log store once, after the last step and before the clean-up:
`xcrun simctl spawn <udid> log show --signpost --start <launch - 2 s> --end <end + 2 s> --style
ndjson --predicate 'processID == <pid> AND (subsystem == "com.apple.UIKit" OR subsystem ==
"com.apple.coreanimation" ...)'`, saved as `raw/signposts.ndjson` (and what was asked and how it
answered, `raw/signposts.json`). Then a second, small read of every process, with no predicate,
from the end of the last step to now (`raw/signposts-tail.json`: what it asked, its exit status,
how many events it returned and how many are stamped after the end of the last step). `log show
--signpost` gives, for the app's process and with nothing added to the app:

- **drawables**: `com.apple.coreanimation`, a category starting `CAMetalLayer`, `ClientDrawable`:
  one per frame the app's Metal layer drew. For an app that draws its own surface they equal its
  frames (measured: 34 recorded = 34 drawables = 34 by the app's own count; at rest 0; a faulty
  build 300 to 302 per 5 s).
- **commits**: `com.apple.coreanimation`, a category starting `Transaction`, `Commit`: one per
  layer-tree commit.
- **display updates**: `com.apple.UIKit`, a category starting `UpdateCycle`, `UpdateSequence`: one
  per display update the app was woken for.

They are not a count of what the screen showed: a UIKit app's frames are composed by the render
server, which animates without the app, so its recorded frames exceed its commits (a tap: 62
commits and 77 updates against 110 recorded frames). An event belongs to the step whose window (its
two CPU readings, by the wall clock) holds its time; `run.json` gives each step its `drawables`,
`commits` and `updates`, at rest and on input steps alike (information on input steps), and the
same three over its recording's span (`recording_span`). The counts are known for the run only if
the store answered (exit 0, every line JSON), holds at least one drawable or commit of the app
between its launch and the first step (an app that started has drawn or committed something, so an
empty answer means the store was not read), and the second read answered with at least one event
stamped after the end of the last step (other processes log all the time, so none means the store
may not have been read to the end; a simulator has no `logger` to write a marker with). Otherwise they are unknown
for every step, and the run says why in one note. The rest rules that use them are under "The
gate" above.

**An app's own signposts.** `"count_signposts": {"subsystem": "<name>", "names": ["<name>", ...]}`
(simulator only; `count_sections` stays Android's) counts, per step, the app's events of that
subsystem whose type is `begin` or `event` and whose signpost name or message is a listed name; the
subsystem is added to the store's predicate. `expect.signposts_max: {"<name>": N}` on a rest or an
input step gates them (check `signposts`). A name is matched by exact equality with the
signpost's name or its message. If the run holds no event at all of that subsystem from the app,
launch included, every named count is unknown and each `expect.signposts_max` fails, asking whether
the app's tracing is switched on. A name that occurs nowhere in the run, launch included, is
unknown too (a misspelt name would otherwise read 0 and pass): its count is null and each
`expect.signposts_max` on it fails, naming it and saying how names are matched.

## A local backend over TLS: `ui-probe tls-front`

An iOS app refuses plain HTTP (App Transport Security), so it cannot reach a local backend that
speaks HTTP unless its security settings are loosened. `ui-probe tls-front --port <listen>
--to-port <backend> --state <dir> [--log <file>]` puts TLS in front of it with no change to the
app: it listens on 127.0.0.1 only (TLS 1.2 or newer, ALPN `http/1.1`), pipes the bytes both ways to
127.0.0.1:<backend>, prints `listening on https://localhost:<listen> -> http://127.0.0.1:<backend>`
once it accepts connections, and runs until it is stopped. With `--log`, one JSON line per closed
connection (time, duration, bytes each way) and one per backend connection that was refused.

The state folder holds a throwaway certificate authority (`ca.pem`: CA:TRUE with pathlen:0,
critical; name constraints permitting DNS:localhost only, critical; key usage certificate signing;
RSA 2048, SHA-256, 365 days) and a certificate for `localhost` (`leaf.pem`, `leaf.key`:
DNS:localhost, server authentication, not a CA, 365 days), made with the `openssl` on PATH on first
use (with configuration files that state the extensions, which LibreSSL needs) and reused while
valid. The CA's key is deleted as soon as the certificate is signed, so the CA can sign nothing
else. A certificate that has expired or is within 7 days of it is made again with a new CA, and the
output says so: the simulator then needs the new CA. The tool marks a folder it uses with
`.ui-probe-tls-front`: a folder it makes is 0700 and its key 0600; an existing folder is used only
if it is empty (and then left at its mode, refused if others can open it) or holds that marker; a
folder that is not empty and unmarked is refused, and the mode of, or the files in, a folder the
tool did not make are never changed. A marked folder or a key that others can read is refused and
not changed. `ui-probe tls-front --state <dir> --ca` prints the path of `ca.pem`
(making the files if they are absent), for `xcrun simctl keychain <udid> add-root-cert <ca.pem>`.
The tool never touches a trust store. The CA is a throwaway for a simulator: it must never be added
to a phone's or a Mac's own trust store. It shapes nothing (the proxy does that). Pass the backend's
address to the app with `launch_env`, for example `{"API_ORIGIN": "https://localhost:18443"}`.

## Input on the iOS Simulator

A scenario with a tap or a swipe on a simulator drives them through ui-probe's input agent: one
UI test (`ios-agent/`, a hand-written Xcode project and one Swift file, naming no app) that serves
commands on a loopback socket.

- **Built once.** On the first run that needs it, `xcodebuild build-for-testing -project
  ios-agent/Agent.xcodeproj -scheme Agent -destination 'generic/platform=iOS Simulator'
  -derivedDataPath <temporary folder>` builds it (about 5 s) into a temporary folder under
  `${XDG_CACHE_HOME:-$HOME/Library/Caches}/ui-probe/ios-agent/`, which is renamed to `<key>/` with
  a `built.ok` marker only after a build that exited 0; a folder without the marker is not a build.
  A lock file (`<key>.lock`) makes concurrent runs build one key once. The key is a hash of the
  agent's source and of what `xcodebuild -version` prints: a new Xcode or a changed agent builds
  anew. A failed build fails the run and names the saved build log (`<key>.build.log`). `doctor --simulator` says
  whether the agent's source is present and whether it is built for this Xcode.
- **Started for the run.** After the status bar is frozen and before the app is launched,
  `xcodebuild test-without-building -xctestrun <file> -destination 'platform=iOS
  Simulator,id=<udid>' -only-testing:AgentUITests/AgentUITests/testServe -derivedDataPath
  raw/agent-derived-data -resultBundlePath raw/agent.xcresult` starts it (both folders are removed
  after a test run that ended with status 0 and kept otherwise), in a process
  group of its own, with a free loopback port, a random token, the app's bundle identifier and an
  idle limit (the scenario's longest wait plus 120 s) passed as `TEST_RUNNER_UI_PROBE_AGENT_*`
  variables, which `xcodebuild` hands to the test without the prefix. The run waits for its first
  answer (120 s); an `xcodebuild` that exits first fails the run with the tail of its log
  (`raw/agent.log`). This installs the agent's runner app (`dev.uiprobe.agent.uitests.xctrunner`)
  on the simulator for the run's duration.
- **What it does.** `ping` (the app's state and the display's size in points), `tap` (`x`, `y`),
  `swipe` (`from`, `to`, `ms`) and `quit`, one JSON object per connection. Taps and drags go
  through the system app at fractions of the display. The agent reads nothing of the app under
  test but its state. Every command carries the run's token and a fresh random nonce; anything
  without the token is answered with an error and does nothing. Every reply carries the
  HMAC-SHA256 of the nonce keyed with the token, which the host checks: a reply without it (the
  first `ping` included) did not come from the agent, and the command fails. A request must arrive
  in full within 5 s of its connection. An issue the test framework records while a command is
  handled (a gesture it could not perform) makes that command's reply a failure carrying the
  issue's text. It listens on 127.0.0.1 only, and ends by itself once no command has arrived for the
  idle limit, so a killed host cannot leave it running.
- **Per step.** A tap or a swipe: the recorder started and awaited, CPU read, the command sent,
  `settle_ms` waited, CPU read, a `ping` for the app's state, the recorder stopped. A rest: as
  before, with the `ping` at the end. `raw/step-<step>.json` keeps the command, the agent's reply,
  the round trip and the app's state. A tap is judged as a state change and a swipe as motion
  unless its `judge` is `state`, as on Android; `run.json` carries each input step's signature,
  its recorded frames and CPU (information, never gated) and the agent's timing.
- **The gate.** A command the agent did not perform fails that step (check `input`); an
  `expect.signature` that does not match fails (check `signature`); an agent whose test run ended
  with a status other than 0 after `quit` fails the run (check `agent`: XCTest recorded an issue);
  one the tool had to end with SIGTERM or SIGKILL is a note, since its status then says nothing; an
  app that was not in front at
  the end of any step, rest or input, fails the foreground check, and the run's note about what a
  simulator did not check then leaves "which app is in front" out. A rest-only scenario starts no
  agent, builds nothing, keeps the records it had, and says as before that the foreground was not
  checked.
- **Stopped on every way out** (normal end, error, interrupt, SIGTERM, SIGHUP), after the last
  step's recorder was stopped: `quit`; up to 15 s for `xcodebuild` to exit, then SIGTERM and 5 s,
  then SIGKILL to its group; `xcrun simctl terminate` and `xcrun simctl uninstall` of the runner
  app (failures ignored); the derived data and result bundle removed if the test run ended with
  status 0; then the status bar cleared. `raw/agent.json` keeps the port, the first answer and how the
  agent ended.
- **Why no lookups and no typing.** An accessibility read of an app can change the app it reads.
  Measured on one app: after one lookup of a label in it (or typing through one of its
  elements), it used 0.8% to 1.6% of a core at rest for the rest of its process's life, also after
  the agent had quit, and once presented a frame at rest; with the agent idle and reading only
  the app's state, the app at rest was unchanged. To measure this for another app: a rest, one
  lookup, the same rest again, each recorded with CPU. Typing through the system app is refused by
  the test framework, and the keyboard's keys are not in the system app's tree, so this version
  has no lookups and no typing on a simulator: tap positions as fractions instead.

## Known limits

- **Android, and the iOS Simulator for rests, taps and swipes.** Nothing here drives a physical
  iPhone. On a simulator the app's frame counts come from its log store, known only when the run
  read the store to the end; there is no quiet detection, and input is by position only: no lookups by text and no typing (an accessibility read can change the app it
  reads).
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
