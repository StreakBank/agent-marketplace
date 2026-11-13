# ui-probe — watch a mobile app from outside

`ui-probe` runs a scripted scenario against an Android app (or, with rests, taps and swipes, an app
on the iOS Simulator) and records what the platform and the screen did, without anything inside the app. It answers two questions a functional test does not:

- **Is the app doing work while nothing happens?** Frames drawn, CPU used and frames composed while
  the screen is at rest, and, through a front the app's traffic passes, the requests it sends.
- **What did each step look like on the way?** The sequence of visual states a tap passes through,
  read from a screen recording: a clean transition, a transition through a skeleton or a flash, a
  flicker that leaves a state and returns to it.

It knows a package name, screen coordinates and, for a tap that names its target, the target's
text or content description. It names no app, screen, endpoint or UI framework. A film of a phone's
screen that no run made can be judged too, by its picture alone (see "A film no run made").

## Three kinds of result, three rules

| Kind | What it is | What to do with it |
|---|---|---|
| Exact | A count that happens because the app did something: frames and CPU at rest, frames for a step that waits on nothing, trace sections per step, requests per step | Gate it. Equality or a budget, no tolerance |
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
  "launch_url": "<a link to start the app by, Android only, optional>",
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
  `sections_max` (section name to integer), `requests_max` (an integer; needs `run --requests`, see
  "Counting requests"). These are the baselines you approve; add them once a screen is stable.
- `launch_requests_max` at the top level (a whole number, 0 or more; both platforms): the budget for
  the launch step's request count, gated as `expect.requests_max` is on a step. A cold start is the
  budget most worth holding.
- Fixed waits must add up to less than the recorder's 180 s limit. Split longer scenarios.
- `"watch": true` on a rest marks it as one watched for a change made rarely; it must last at least
  70 s (see "Watched rests"). `watch_min_ms` at the top level replaces that minimum for the
  scenario.

More keys, one example each:

```json
{"id": "type-code", "do": "text", "text": "000000"}
{"id": "open", "do": "tap", "find": {"text": "Sign up"}}
{"id": "open-below", "do": "tap", "find": {"text": "Settings", "scroll": {"from": [0.5, 0.8], "to": [0.5, 0.3], "max": 5}}}
{"id": "open-slow", "do": "tap", "find": {"desc": "Phone number", "nth": 1, "timeout_ms": 15000}, "until_quiet": true, "quiet": {"max_ms": 30000}}
{"id": "rest-field", "do": "rest", "ms": 5000, "allow": {"frames_max": 12, "reason": "the caret blinks twice a second"}}
{"id": "open-invite", "do": "url", "url": "example://join/ABC123?from=probe"}
```

- `url` opens a link in the app while it runs, as a tap on a link outside the app would: a screen
  reached only by a link (an invitation that joins a group, say) can then be measured. It is an
  input step like a tap: the same waits (`until_quiet`, `quiet`, `settle_ms`, default 3000), the
  same recording, judged like a tap's (a state change), the same counts (frames, sections,
  signposts, requests) and `expect`. It takes only `url` besides those. On Android the device
  script runs `am start -W -a android.intent.action.VIEW -d '<url>' -p "$PKG"`: `-p` sends the link
  to the app under test only, so a link another app also claims never opens a chooser. On the iOS
  Simulator the host runs `xcrun simctl openurl <udid> <url>`, and the system then asks `Open in
  "<app>"?` (`Cancel`, `Open`): nothing reaches the app until `Open` is tapped (iOS 26.1 simulator,
  2026-11-11). So the step has the input agent tap `Open` on that alert, waiting up to 5 s for it
  (see "Input on the iOS Simulator"); no alert in that time is not a failure (another iOS version,
  or a universal link, may open directly), and the step's record says whether one was confirmed. A
  link nothing opened fails the step (check `input`) with the tool's own words: on Android when am
  exits non-zero, prints an `Error` line or names no activity it reached (`raw/url-<step>.txt`
  keeps what it printed; a link nothing handles prints `Error: Activity not started, unable to
  resolve Intent` and exits 0, which the `Error` line catches); on a simulator when `simctl` exits
  non-zero, or when the agent could not look for the alert. The link is a scheme (a
  lower-case letter, then lower-case letters, digits, `+`, `.` or `-`), a colon and the rest, at
  most 500 characters of printable ASCII. Since the link reaches a shell on Android, the error
  names and refuses the first space, quote of any kind, backslash, or any of `$ ; | < > ( ) { } * !
  ~ ^`; the argument is quoted as well. `& ? = % / : @ , _ #` and the rest of printable ASCII work,
  so a link can carry a query and a fragment (`#` is read specially by a shell only at the start of a
  word, and the link is one quoted word that starts with its scheme).

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
  still". `allow.regions` lets parts of the screen change on their own limits (see "Regions that
  change on their own").

The file is checked strictly before anything runs, because a key the tool ignored would silently
change what is gated:

- `package` is an application id (dot-separated names of letters, digits and `_`). `activity` is
  letters, digits, `_`, `.` and `$`, so an inner class such as `.Outer$Inner` works.
- `launch_url` (Android) starts the app by a link instead of its launcher activity, for a screen the
  app opens only from a link: the launch runs `am start -W -a android.intent.action.VIEW -d
  '<launch_url>' -p "$PKG"` (the `url` step's line, to the scenario's package only), and its output
  is read as a launch's (status, launch state, total time; an `Error` line fails the launch, as a
  link nothing handles prints). The app is stopped before the launch, so this is a cold start by
  link. The launch's time, its request window and budget (`launch_requests_max`) and its first
  frames are counted as for any launch. It takes the `url` step's rules for the link. `activity` and
  `launch_url` cannot both be given. The iOS Simulator refuses it: an app started by a link cannot be
  given its launch environment there; launch it and open the link in a `url` step right after.
- `quiet.ms` and `quiet.max_ms` are numbers of at least 100, with `max_ms` at least `ms`, in the
  scenario's block and in a step's (where the check uses the scenario's value for a key the step
  leaves out). `until_quiet`, `meminfo` and `background` are `true` or `false`. `count_sections`
  is a list of names.
- The `gate` block takes only `rest_frames_max` (an integer), `rest_cpu_pct_max` (a number),
  `rest_requests_max` (an integer), `rest_updates_max` (an integer; the iOS Simulator only) and
  `periodic_requests` (an object from a request as the front's log gives it, an upper-case method,
  one space and a path that starts with `/`, to a whole number of at least 1; see "Counting
  requests").
- `allow` takes `frames_max` and `changed_frames_max` (integers), `cpu_pct_max` (a number),
  `unchanged_frames_max` (an integer of at least 1), `updates_max` (an integer; the iOS Simulator
  only), `requests_max` (an integer), `regions` (a list of 1 to 16 regions) and `reason`, and at
  least one of the limits or `regions`. `reason` is required whenever a limit is given; an `allow`
  that holds only `regions` needs none, since each region carries its own. A region takes `region`
  (`[x0, y0, x1, y1]`, fractions of the app's area between 0 and 1 with `x0 < x1` and `y0 < y1`),
  `changed_frames_max` (an integer of at least 0) and `reason` (a non-empty string), all three
  required. `watch` is `true` or `false`; `watch_min_ms` is a number of at least 10000. `find` takes `text` or `desc` (a
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
  step, and applies to no other step. Within the step it is a budget that any change may spend:
  frames allowed for a caret also cover another element that changes as often. To allow one
  element only, give it a region (`allow.regions`, below).
- Whether a caret's blink counts as a change in the app's area depends on how large the caret
  is in the recording. On a display 1080 px wide it did not (0 changed frames in 20 windows); on
  one 720 px wide the caret of a tall code field did, in 10 of 10 frames. Give such a rest
  `changed_frames_max` as well as `frames_max`, with the same number.
- Recorded frames that changed nothing are allowed only by `unchanged_frames_max` (a whole number
  of at least 1); without it none may be recorded at rest, and `frames_max` or
  `changed_frames_max` does not reach them. An animation nobody can see produces such frames, and
  that check is what catches it, so give the allowance only to a screen whose own caret composes
  them. The caret above did not; one framework's caret on the iOS Simulator did: a focused text
  field, six rest windows of 5 s, 30 drawables and 70 display updates every time, 30 to 32
  recorded frames of which 11 to 14 changed the app's area and 15 to 19 changed nothing. Such
  frames are also excused, without an allowance, when the app presented no frame by any count
  (see "The gate"), and that exemption cannot hide such an animation: it draws and presents
  frames, so its counts are above zero, and both the
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

## Regions that change on their own

A screen can be still except for one part that changes by design: a countdown, a clock, a live
score. A rest can allow that part its own changes without allowing them anywhere else:

```json
{"id": "rest-countdown", "do": "rest", "ms": 70000, "watch": true,
 "allow": {"regions": [{"region": [0.6, 0.0, 1.0, 0.12], "changed_frames_max": 80,
                        "reason": "the countdown ticks once a second"}]}}
```

- A region is given as fractions of the app's area (the screen without its system bars), so one
  file serves any resolution. The bars come from the run's own insets on Android
  (`stable_insets_top_bottom`) and from the simulator's bands (70 and 45 thousandths of the height)
  on the iOS Simulator, never from fixed rows. On Android a run whose device gave no insets cannot
  place its regions, and each such rest fails `rest-region`.
- Each recorded frame that counts as a change at rest (on Android a frame that changed the app's
  area by more than encoder noise; on a simulator every frame that changed anything) is placed at
  full resolution. The recording is decoded again for those frames and the one before each, and
  what changed is found on a block map: blocks of 24/1080 of the width, a pixel counting above 10
  levels on any colour channel, a block counting when 40 % of its pixels do. Touching changed
  blocks make one change, whose box is the box of the changed pixels in those blocks and the blocks
  next to them. Outside those groups the pixels over the level are held to the film's rule (see "A
  film no run made"): each connected group of them of at least 3e-5 of the picture (a glyph redrawn
  in a label) is one more change with a box of its own, and smaller pieces (scattered encoder noise)
  are left out. A change too small for any block to count is placed by its changed pixels.
- A change wholly inside a region counts against that region's `changed_frames_max` and not
  against the rest. A change that crosses a region's edge, or lies outside every region, counts
  against the rest as before (`changed_frames_max` of the step's `allow`, default none). A frame
  with both counts against both. On the iOS Simulator a frame whose every change lies inside the
  regions also leaves the step's count of recorded frames.
- `run.json` holds a report per region under the step's recording record (`regions`): how many
  frames changed inside it, when (the first and last, in ms after the step's start), where (the box
  of every change inside it, as fractions of the app's area), for how long (bursts of changes less
  than 350 ms apart, and the longest) and how often (the number of bursts and the median time
  between their starts), with each frame's index and box. `rest_changed_frames` is what counts
  against the rest.
- The gate fails `rest-region` when a region changed in more frames than its limit, or when the run
  holds no report for the step's regions (an analysis from before 0.9.0, or one made for other
  regions: run `ui-probe analyze` with the scenario). A region within its limit gets a note with its
  count, when it changed and how often.
- A region allowance covers the recording only. The frame counts (the platform's, the compositor's,
  a simulator's log store) do not say where a frame was drawn, so a rest whose countdown draws
  frames still needs `frames_max` (and `updates_max` on a simulator) for them.

## Watched rests

A change made once a minute is missed by a rest of a few seconds, and by a window of exactly one
minute that starts just after it. A rest marked `"watch": true` is the one an agent or a person
watches for such changes, and it must last at least 70 s: the scenario check refuses a shorter one
(`a watched rest lasts at least 70000 ms`). `watch_min_ms` at the top level of a scenario (or of a
film's windows file) replaces the 70 s for that scenario; it is at least 10,000 ms, and the gate adds a
note to every run of a scenario whose minimum is below 70,000 ms, naming both numbers. On Android a watched rest still counts
toward the recorder's 180 s limit, so a scenario holds at most two of them with little else.

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
  capture.json          device facts, the scenario as run, host load, the tracer's output, the request log given
  failed.json           only when the run did not complete: the error, the step if known, the time (UTC)
  device-script.sh      the exact script that ran on the device
  raw/                  what the device script wrote:
    marks.txt             step marks on the device's boot clock
    requests.jsonl        with --requests: the front's request, unparsed, upgrade and backend-refused lines of
                          the run (written by the host, not the device; 0600)
    requests-meta.json    with --requests: the log's path, the byte offsets read, the window, the clock mapping,
                          the front's pid and whether it ran at the start and the end, the connections it could
                          not read, and why the counts are unknown when they are (0600)
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
  frames-full/<step>/   written by `sheet --full`: every recorded frame of the step's window and the
                        last one before it, at full resolution, named f<index>.png by the recording's
                        own frame index
  sheets/<step>-distinct-NN.png   written by `sheet --distinct`: only the frames that changed the
                        app's area, 24 to a page, each labelled with its index and its time since the
                        step's start
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

`sheet <run-dir> --full --distinct` also writes what an agent needs to read a step's window frame
by frame. `--full` writes every recorded frame of each step's window and the last frame before it
at full resolution under `frames-full/<step>/` (on a simulator, each step's whole recording, its
first frame being the screen as recording began). `--distinct` writes sheets of only the frames
that changed the app's area, 24 to a page, labelled with the frame's index and its time since the
step's start (from `frames-full/` with `--full`, else from the thumbnails). Either refuses (exit 2)
when the recording's own frame list (ffprobe's) holds another number of frames than the analysis
read, or when ffmpeg writes another number of frames for a window than the list names, since frames
by index would then not match the analysis. On a film (see "A film no run made") the analysis writes
both itself, and the two flags add nothing.

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
| Recording at rest | frames were composed that changed nothing on screen (beyond those the display composed for another layer: `frames_display` above the largest of the app's own counts, all known), or the app's area changed in more recorded frames than the step's `allow.changed_frames_max` (default none). Only an aligned recording decides, and only while the app is in front; otherwise these are notes. Both are notes, not violations, when the app presented no frame by any count (platform and per-layer both known and 0, a trace's 0 or absent); a step with `allow.changed_frames_max` keeps its allowance for changes in the app's area |
| CPU at rest | the app used more than `rest_cpu_pct_max` percent of one core (default 2.0, or the step's `allow.cpu_pct_max`) |
| Find | a tap with `find` holds no record of where its element was found, or the element was matched in another package than the one asked for |
| Run | a run folder holds `failed.json`, `device-script.sh` or `raw/` but no capture: the run did not complete |
| Quiet | a step with `until_quiet` never went quiet. The message says whether the app's own frame count kept changing, or only the display's (a system animation, the keyboard, another window) |
| Process | the app's process was not running at the end of a step, more than one process carried its name, or it changed during the scenario |
| Launch | the launch command printed an error or did not report completion, or no frame of the app was counted while it launched |
| Foreground | another app's activity was the resumed one at the end of launch or of a rest step (a tap that left the app, a permission dialog, the launcher after a back press too many), or the app was in front in a rest marked `"background": true` |
| Recording | a requested recording is missing or unusable, the recorder exited with a non-zero status or had stopped before the scenario ended, or the scenario ran past the recorder's limit |
| Trace | a requested trace is missing |
| Requests at rest | the run was given `--requests` and the app sent more requests in a rest step than `rest_requests_max` (default 0, or the step's `allow.requests_max`); the message names the paths. A count that is unknown fails too |
| Requests | a step's `expect.requests_max` is exceeded, its count is unknown, or the run was given no request log ("not counted: the run was given no request log") |
| Expectations | a step's `expect` is not met, or cannot be checked (no aligned recording for a signature, a section the app never emitted, a trace that lost data) |
| Missing step, scenario | a step of the scenario has no record in the run, or its kind differs |
| Analysis | a capture has no `run.json`, or one older than the capture: run `ui-probe analyze` |
| Region at rest (`rest-region`) | a region of a rest's `allow.regions` changed in more recorded frames than its `changed_frames_max`, or the run holds no usable report of the step's regions (see "Regions that change on their own") |

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

On Android a recorded frame that changed nothing is held against the app only as far as the app's
own account of its frames allows. That account (F) is the largest of the app-side counts, the same
figure the frame check uses: the platform's (`frames`), the one of the frames the app's own layers
latched (`frames_layers`, L) and a trace's when there is one. The compositor's statistics also
count the frames the display composed (`frames_display`, D); the recording counts the frames that
changed the app's area (`app_region_frames`, A: any change there, a caret's blink included) and
those that changed nothing (I). Each of the app's frames that no recorded change in its area
accounts for may have produced a frame that changed nothing, so the app owns up to `min(I, max(0,
F - A))` of them. Only the rest can have been composed for a layer that is not the app's, and no
more of them than the display composed beyond the app's account: `min(I - owned, max(0, D - F))`
are set aside, not held against `allow.unchanged_frames_max` or the default of none, and the gate
says so in a note for the step with D and F. What is left is judged as before, and its failure says
how many were set aside. If the platform's count, L, D or A is not a known count (an integer of 0 or
more), or a trace's count is there and is not one, nothing is set aside, and the changed-frames
check does not use this. A surface whose layer the layers' count does not give to the app (a
pop-up window whose layer name does not carry the package, on Android 11 and older) is still in the
platform's count, so its frames stay the app's. An animation nobody can see still fails: its frames
are the app's and change nothing on screen, so they are the app's own whatever the display did. A frame the app drew that changed nothing (a redraw of the same
picture) is the app's own too: a 150 s rest on Android 14 with L 4, D 9, A 2 and I 2 fails as
before, its five extra composites being the status bar's, which the recorder kept as frames that
changed only outside the app's area. Measured on an Android 11 emulator (2026-11-11): two rests on a focused field
with the keyboard up recorded one frame that changed nothing while the display composed 11 frames
and the app's layers latched 10 (the platform also counted 10); no other layer's count rose in the
compositor's statistics over the rest, and every one of those compositions was a client
composition. The same kind of rest with the keyboard hidden had 10 and 10. Which layer asked for
the extra composition is not established. On Android 14 the three counts were equal in such rests
and no such frame was recorded.

Limits come from the step's `allow`, then the command line, then the scenario's `gate` block,
then the defaults. `--advisory` reports the same violations and exits 0. `gate --scenario <file>`
gates with a revised copy of the run's own scenario (a limit, an allowance): it refuses (exit 2) a
scenario whose `name` differs from the one stored with the run, and each `allowance:` line then
says that it came from `--scenario`.

## A film no run made: `ui-probe film`

A phone's screen can be filmed by another machine while a person uses it, with no run, no step
marks and no counts. `ui-probe film <video> --windows <windows.json> --out <dir>` judges the
stretches of such a film that nobody touched as rests, by the picture alone, into a run folder that
`gate`, `summary`, `zoom` and `sheet` read.

```json
{"name": "evening-sit", "insets_px": [140, 100], "windows": [
  {"id": "sit-home", "start_s": 4.0, "end_s": 76.0, "watch": true},
  {"id": "sit-countdown", "start_s": 90.0, "end_s": 110.0,
   "allow": {"regions": [{"region": [0.6, 0.0, 1.0, 0.12], "changed_frames_max": 25, "reason": "a countdown"}]}}
]}
```

- The windows file is a list of windows, or an object with `windows` and optionally `slow_frames` and
  `slow_min_group` (whole numbers of at least 1) and `explain_px` (a whole number of at least 0; see
  below), `name` (else
  `--name`, else the film's file name), `insets_px` (the system bars' heights in the film's own
  pixels, `[top, bottom]`; without it the whole picture is taken as the app's area, and the gate
  says so; a connected change only in the bars counts as `bars_only`) and `watch_min_ms`. A window has an `id`, `start_s` and `end_s` in the film's own seconds
  (`0 <= start_s < end_s`), and may have `watch` and an `allow` with `changed_frames_max` (with a
  `reason`) and `regions`. Windows may not overlap. Nothing else is accepted: a film holds only the
  picture, so frame counts, CPU, requests and the app's process are not checked.
- The film is never copied into the run folder. `capture.json` holds the windows as a scenario
  marked `"film": true` and names the film by its absolute path, its size and its SHA-256; `analyze`,
  `sheet` and `zoom` read the film at that path again and refuse (exit 2) when it is gone or its size
  or SHA-256 differs. Keep the film where it was, or the run's numbers stand but cannot be redone.
- Without `--no-pictures` the folder also holds `frames-full/<window>/` (at full resolution: the
  frames that changed the app's area and the window's first and last frames, nothing else),
  `sheets/<window>-distinct-NN.png` (the frames that changed, 24 to a page, labelled with the film's
  frame index and the time since the window's start) and `sheets/<window>.png` (the first page, or the
  window's first frame when nothing changed). `zoom <dir> <a> <b>` takes the film's own frame indices;
  `--step` does not apply. `film` refuses a folder that already holds a run, as `run` does.
- `--no-pictures` is for a film that shows what must not be kept: the folder then holds
  `capture.json` and `run.json` and nothing else, no frame, no sheet, no thumbnail and no copy of the
  film. Every number is kept: per window the frames, the frames that changed with their times and
  the boxes of their changes, what was left out and why. `gate`, `summary` and `analyze` work on it
  as on any film run; `zoom` and `sheet` refuse it with one line, since each would make a picture.
- What changed is decided by how connected it is. Measured on one phone capture, H.264, 60 a second,
  1206 x 2622 (6,683 frames decoded in 113 s): no two consecutive frames were bit-identical. At rest
  the mean change was about 0.002 levels a pixel and the 99.9th percentile 1 level; an ordinary
  frame had about 13 pixels over 10 levels (up to 135, some of them changing by 50 to 60), about ten
  of them in two places in at least half of all frames; a key frame (every 30 frames, each making two
  consecutive changed pairs) had 730 to 1,340 such pixels, scattered, touching 5 to 7 % of the blocks
  with no block at 40 %. The largest 8-connected group of pixels over the level in any rest frame was
  32 px on an ordinary frame and 36 px on a key frame (48 px at 6 levels). A real swipe put 690,000
  to 780,000 pixels over the level.
- So a frame of a film changed the app's area only when it holds a connected group of pixels (8-
  connected, each changed by more than 10 levels on some colour channel) of at least 3e-5 of the
  picture: 95 px at 1206 x 2622, 78 px at 1080 x 2400, about twice the noise's largest group at 6
  levels. Every smaller group is scattered: the recording's, not the app's. A frame with no pixel over
  the level is still. Nothing depends on two frames being identical, and nothing is dropped: a frame
  that holds no such group is simply not a change. Bursts are formed from the frames that changed.
- The verb says what it left out, in `left out: ...` lines, in the gate's notes and in `run.json`
  (`recording_floor`, and per window): how many frames held only scattered changes, the largest
  scattered group beside the minimum (with a caution when it reaches 70 % of the minimum: a small
  change of the app's may then be near the line), and, for information, the places where pixels went
  over the level in at least a quarter of the window's frames, as boxes. When the container's header
  states another frame count than the frames decoded (6,986 against 6,683 on the capture above), it
  says so; the decoded frames are the ones counted.
- A change too slow to show between neighbours (a fade of a few levels a frame, a bar that grows a
  few pixels a frame, a line that slides) is caught by two more comparisons under the same rule. Each
  frame is also compared with the frame 0.5 s earlier (30 frames at 60 a second; `slow_frames` in the
  windows file sets another number), when that frame lies in the window or is the one before it, and
  each window's last frame with its first. A frame counts as changed when either of its comparisons
  holds a group at the minimum (`slow_min_group` in the windows file sets another minimum for the
  slow comparisons; by default it is the same). Of a group of the 0.5 s comparison, the pixels within 3 px
  (`explain_px` in the windows file) of a pixel counted as changed between neighbours in that half
  second of the same window, or inside one of its regions, are that change seen again; what is left is
  grouped again, and a group at the minimum counts. A window's ends fail `rest-video`, whatever its
  allowance, when the pixels of their difference that are not within 3 px of a pixel counted in the
  window (between neighbours or by the 0.5 s comparison), nor inside a region, form a group at the
  minimum. A slow change never explains a later slow group, so a fade counts in every frame it
  shows. The memory this needs is one whole number per pixel (13 MB at 1206 x 2622). Per window,
  `run.json` (`slow`, `ends`, `changed_by_slow_only`) and the notes give how many frames counted only
  by the 0.5 s comparison, whether the ends differ, and the largest scattered group of each beside the
  minimum, with the same caution at 70 %. The measured basis is the same capture's: frames 30 apart lie
  in different key-frame groups there, so their noise may be larger than between neighbours, which is
  why these numbers are reported for every window. Measured on that capture with the final rule: in
  26 s of a screen at rest the largest scattered group was 57 px between neighbouring frames, 67 px
  between frames 0.5 s apart and 38 px between the window's ends, against a minimum of 95 px, so the
  0.5 s comparison is the narrowest margin there (70 %, where the caution starts). A film compressed
  harder has larger groups: made with libx264 at crf 38, one key frame of a still picture counted as
  a change (a group of 2,586 px), where at crf 23 nothing counted.
- A window that starts or ends beyond the film's last frame by more than one frame interval is
  refused, and so is a window that holds fewer than two frames.
- Each window is a rest: the gate fails `rest-video` when more frames changed the app's area than
  its `allow.changed_frames_max` (default none), and `rest-region` as on a run; on a film a change is
  placed by its connected groups at the minimum, so the scattered pixels never count against the
  rest. `summary` gives per window the frames, the still ones, those that changed the app's area,
  those with only scattered changes and the largest scattered group (keys `<window>.film_*`).
- 0.9.0's first form of this verb decided by bit-identity, a fixed point of at most 4 pixels and a
  key-frame period. On the capture above it counted every frame of every window as a change: no two
  frames were identical, the flickering pixels were more than 4, and no period was found. It was
  replaced by the rule above before release.

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
store"). This version runs rests, taps and swipes at
fractions of the display through a small UI-test agent (`ios-agent/`, see "Input on the iOS
Simulator"), and links (`url`, opened by `xcrun simctl openurl`), driven from the host; there is no
device script. A measured run never looks an
element up and never types: a tap that names its target with `find` goes to a position found
beforehand by `ui-probe rehearse`, and typing happens only there (see "A rehearsal on the iOS
Simulator"). A simulator run reads its log store.

**What it means for a scenario author: any frame at rest fails.** With the status bar frozen
nothing but the app has a reason to make the display compose: a frame the app presented (its log
store's count) or a frame recorded at rest fails the rest, with one exception, the encoder refining a
still picture (below). A screen that is never still by design (a blinking caret) needs an allowance
on its rest (`allow.frames_max`, `allow.changed_frames_max`, `allow.updates_max`, and
`allow.unchanged_frames_max` for a caret that composes frames that change nothing).

- **The scenario.** `package` is the bundle identifier (letters, digits and `-` in dot-separated
  names). `activity`, `launch_url`, `quiet` and `count_sections` must be absent and `until_quiet` false or
  absent, in the scenario and in a step. A step is a `rest` of at least 1000 ms (the CPU time is
  read in steps of 0.01 s) without `meminfo` or `background`, a `tap` with `x` and `y`, a
  `swipe` with `from`, `to`, `ms` and `judge`, or a `url` with its link; a tap, a swipe or a link
  waits its `settle_ms` after the input (default 3000 for a tap and a link, 2500 for a swipe, as on
  Android). In `expect`, a rest takes only `signposts_max` and `requests_max`, and a tap, a swipe or
  a link only `signature`, `signposts_max` and `requests_max`. A `url` step needs no rehearsal: it
  looks nothing up, a rehearsal opens the link and confirms the system's alert too (so the steps
  after it are rehearsed on the screen it leads to) and a run does both again itself. A swipe lasts at most 60000 ms (what the agent accepts), and so does a step's
  `settle_ms` and a `find.scroll.settle_ms` (each is a wait between two commands to the agent, which
  ends itself after an idle limit). A tap may name its target with `find` (`text` or `desc`,
  `nth`, `timeout_ms`, `scroll`) instead of `x` and `y`; such a run needs `--rehearsal` (see "A
  rehearsal on the iOS Simulator"). Refused before anything runs, each with its own message:
  `find.package` (it names an Android window), `find_settle_ms` (a measured run looks nothing up;
  the swipes it replays wait their `find.scroll.settle_ms`), a `text` step in a measured run (a
  field that has had focus changes the app for the rest of its life: put the typing in a scenario
  of its own and drive it with `ui-probe rehearse`), and a `key` step (a simulator has no keys to
  press: tap the screen's own control or swipe from the edge). The launch waits `launch_settle_ms` (default 6000). `launch_env` (below)
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
  nothing against `allow.unchanged_frames_max` (default none), those that changed any pixel against `allow.changed_frames_max`
  (default none), the violation naming the classes; except that they are a note, the encoder
  refining a still picture a few seconds after motion, when all of these hold: the counts are known
  and all 0 for the window and for the recording's own span (from `Recording started` to the
  recorder's stop); at most 6 frames were recorded; no pixel changed by more than 24 levels on any
  colour channel at the analysis scale (270 pixels wide); no frame changed more than 2000 pixels by
  8 levels or more; and none of those lies where the previous picture was flat. Measured on 523
  still rest windows of one app: 52 held such frames (32 one, 18 two, 2 three), and the encoder's
  refinement changed at most 17 levels in 19 to 190 pixels (up to 193 in the 17 windows measured
  for 0.5.0; more on a screen with more text), all along edges already in the picture; the frame limit is twice the
  most measured, and was 2 until three frames failed two clean runs; a 1 pt grey hairline that
  grew changed 16 levels in 6 or 7 pixels, 4 or 5 of them on a flat area, so the flat-area count
  holds it where the size of the change could not, and the pixel limit, a factor of ten above the
  largest refinement measured, only bounds a wide subtle change; a 20 pt
  spinner and a caret changed 165 or more. With the counts unknown the recorded frames are held as
  just described, without the exception. Android's 0.3.0 rule for
  frames recorded while the app presented none is not used on a simulator. A recorder that had
  stopped before its window ended (even with status 0) fails the recording check: its file ends
  early and would look clean. The checks a simulator cannot feed (waiting for quiet, frames at
  launch, which app is in front unless the run has input, `find`, trace sections) are not run, and every simulator run
  says so in a note rather than passing them by silence. (`find` is among them because a measured
  run looks nothing up: a tap with `find` goes where the rehearsal found its element.)
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
- **Not measured.** Lookups and typing (they happen in a rehearsal, which measures nothing); quiet; trace sections; memory; which app is in front in a
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
once it accepts connections, and runs until it is stopped. With `--log`, it also reads the
requests it pipes and writes one JSON line per request, per closed connection (time, duration,
bytes each way, requests read) and per backend connection that was refused, and one when it starts
listening: see "Counting requests". It offers only `http/1.1` in ALPN, and that is what makes the
requests readable: a client that would speak HTTP/2 is given HTTP/1.1.

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

## Counting requests: `ui-probe http-front` and `run --requests`

A screen that fetches while it is at rest, or a step that fetches the same thing twice, costs
battery and data, and no frame count shows it. The requests the app sends are counted outside the
app, by a front its traffic already passes through, with nothing added to the app and nothing asked
of the backend:

- **Android emulator:** `ui-probe http-front --port <listen> --to-port <backend> [--log <file>]`,
  a plain front for a plain local backend. It listens on 127.0.0.1 only, pipes the bytes both ways
  to 127.0.0.1:<backend>, prints `listening on http://127.0.0.1:<listen> -> http://127.0.0.1:<backend>`
  and runs until it is stopped; `--port` and `--to-port` must differ. The emulator reaches this
  machine's loopback as `10.0.2.2`, so point the app at `http://10.0.2.2:<listen>`.
- **Both fronts, stopping and their log.** A front told to stop (SIGTERM, SIGINT, or SIGHUP) writes a
  `stopping` line, stops accepting, closes every connection it holds (each gets its `closed` line)
  and exits within 2 s, whatever its clients do: an app's keep-alive connection does not keep it
  alive. SIGHUP stops it only when SIGHUP was not ignored when it started: a front started with
  `nohup ... &` keeps nohup's choice and outlives the shell or terminal that started it. One log,
  one front: a front given `--log FILE` refuses to start (exit 2, before it listens) when the file's
  last `listening` line names a running process other than itself that is a front (its command line
  holds `http-front` or `tls-front`) whose `--log` is this same file (a relative `--log` is read
  against that process's folder): "<FILE> is being written by another front (pid N); stop it or give
  this one its own log". A pid that is gone, that is now some other program (after a reboot), or that
  is a front writing another log or none is no obstacle. Stop the old front before starting a new one on the
  same log.
- **iOS Simulator:** `ui-probe tls-front ... --log <file>` (above), which the app reaches as
  `https://localhost:<listen>`.

Then `ui-probe run ... --requests <file>` (either platform) counts the requests per step. A count
is known only when the run can show the front saw everything the app sent; anything else is unknown,
never 0, and an unknown count fails every rest.

**The log.** JSON, one line per event, each line one `write` appended to the file, `t` this machine's
clock with three decimals. The front makes the file 0600 at every write, whether it existed or not:

- `{"t", "event": "listening", "port", "to_port", "pid"}` once, when the front starts listening, so
  the file exists before the first connection;
- `{"t", "event": "request", "conn", "method", "path", "query"}` the moment a request's head is
  complete: the **shape** of the path without its query string, at most 300 characters, and whether
  a query was there. A path segment is written as it is only if it is made of lower-case letters,
  dots, hyphens and underscores, starting with a letter, at most 32 characters
  (`[a-z][a-z._-]{0,31}`), or is a version (`v1` to `v999`); any other segment is written as `:id`,
  so `/api/v1/users/u42/profile` becomes `/api/v1/users/:id/profile`, a camelCase word such as
  `getUser` becomes `:id` (that is the price), and a segment holding `;`, `=`, `@`, `:` or `%`
  becomes `:id`. For a target in absolute form the rule applies after the host is removed. This is a
  reduction, not a guarantee: a segment made only of lower-case letters, dots, hyphens and
  underscores stays, so a user name such as `alice` stays, which is why the file is 0600. There is
  no option to log full paths;
- `{"t", "event": "unparsed", "conn", "why"}` once for a connection whose bytes are not HTTP/1.x
  (an HTTP/2 preface, a head over 64 KiB, a start line or a header that does not parse, a body
  length that cannot be read); its bytes go on being piped and it is not read again;
- `{"t", "event": "upgrade", "conn"}` after a request that carries `Connection: upgrade` or a
  CONNECT (logged with the path `(tunnel)`): the front stops reading that connection, whatever the
  server answers, because it does not see the answer;
- `{"t", "event": "closed", "conn", "duration_s", "bytes_up", "bytes_down", "requests"}`,
  `{"t", "event": "backend-refused", "to", "error"}` and `{"t", "event": "stopping", "signal"}`.

No header value, no query string and no body is ever written. The reader is a side observation:
every byte is forwarded as it arrives and only then read, never held back, reordered or changed,
whatever the reader concludes. It reads the start line and the headers up to the empty line, then
the body by `Content-Length`, by chunked transfer coding (chunk sizes, extensions, trailers) or
none, then the next request on the same connection (keep-alive, pipelining); a head or a chunk size
may arrive split at any byte.

**What a run keeps.** `run --requests <file>` refuses, before a run folder exists, a file that is
absent, holds no `listening` line, or whose last `listening` line names a front process that is not
running ("the front that wrote <file> is not running (pid N): start it again with --log <file>"; a
process of another user counts as running). At the start of each run it notes where the file ends
and whether that front is running; after the device part it checks the front again, reads what was
appended, and keeps the `request`, `unparsed`, `upgrade` and `backend-refused` lines whose `t` lies
in the run's window (from the launch to the end of the last step) widened by a 2 s margin, as for
the simulator's log store. It also reads what the same front process wrote before the run (from its
`listening` line on), to follow every connection the front stopped reading. At the end it also
checks that the front still accepts: a TCP connection to 127.0.0.1 on the `port` of the front's
`listening` line must be accepted within 2 s. That probe sends nothing and closes at once; on
`http-front` (which opens a connection to the backend for it, and closes it) it leaves one `closed`
line with no request, after the run's window, and on `tls-front` nothing (a client that left before
its handshake). It saves `raw/requests.jsonl` and
`raw/requests-meta.json` (the log's path, the offsets, the window, the clock mapping, the front's pid,
whether it ran at the start and at the end and whether it accepted at the end, those connections,
and why the counts are unknown when they are), both 0600; a run that cannot write them fails.
`run.json` gives each step, launch included, `requests`: `{"count": N, "by_path": {"GET /x": 2,
...}}` (the 20 most frequent `METHOD /path`), or `{"count": null, "reason": ...}`. `summary` and
`compare` see the counts as `<step>.requests` (Android) and `<step>.sim_requests` (simulator). A run
whose count is unknown is left out of that metric: `summary` counts only the values it has (`n`),
and each row of `compare` shows how many values each side had ("values: 5 base, 0 head"); with none
on one side or on both the verdict is `unknown`.

**A rest runs on to the next step.** Between the end of a rest and the start of the next step the
tool only reads counters or looks a target up, and nothing is put into the app, so a request there
is a request at rest. For request counting, a rest's window runs from its start to where the next
step starts (on Android its lookup's first swipe when its `find` has `scroll`, else its start; on
the simulator the end of the rest's own recording when the next tap replays swipes, else that tap's
start), and the last step's to the end of the run's window. The rest's `requests` adds `gap_s`, how
long that tail was, and `in_gap`, how many of its requests fell there, and its failure says so.
On Android the tail before a step that looks its target up is long: 5.6 to 5.7 s in the first live
runs on an Android 14 emulator, all of it at rest.
Every request is counted in exactly one step, or in none: requests that fell after an input step's
window and before the next step are `between_steps` (with their paths in `between_paths`), not
gated, and the gate notes them once per run; requests outside the run's window that no step took
are `in_margins`, for information. The counts of the steps plus `between_steps` plus `in_margins`
equal the requests the run kept.

**Known, or unknown with the reason.** The counts of a run are unknown when: the front was not
running at the start or at the end of the run, or no longer accepted connections at the end; it
started listening again at any time after the run began (a `listening` line after the run's start
offset, wherever it lies: the old front died at a time nobody knows), or its pid at the end differs
from the one at the start; it was told to stop after the run began (a `stopping` line); it logged a `backend-refused`
during the run (what the app sent on that connection was not read); any connection was opaque
(from its `upgrade` or `unparsed` line until its `closed` line) at any time during the run, one that
became opaque before the run and was still open included; no request at all lies between the
launch and the end of the last step (a request in the margins proves nothing: the app's traffic is
probably not going through the front); the log was cut or replaced (the run keeps the log's inode
and device and its first 4096 bytes at the start, kept in memory and never written, and at the end
reads the file that has the log's name: another inode or device, or first bytes that differ, is a
replacement, also where the file system gave the new file the old one's number; a file shorter than
at the start whose bytes are the start of those kept is cut, so a different file is called replaced
and only the same file grown shorter is called cut), or holds a line since the
front's `listening` line that is not the front's JSON; a run was given a log but kept no record of
it; or, on Android, the clock could not be mapped (below). That last proof (one request in the run)
is necessary, not sufficient: the front cannot tell two clients apart, so nothing else may use it
during a run.

**The step windows.** On the simulator the steps are timed on this machine's clock already. On
Android the marks are the device's `/proc/uptime`. The device script is the same as without
`--requests`: nothing is added to the measured path. The host reads `/proc/uptime` with `adb shell
cat /proc/uptime` three times right before the device script starts and three times right after it
ends, each read bracketed by its own clock, and in each group keeps the read with the shortest round
trip; its offset is the middle of the bracket minus the uptime. The mapping is the straight line
through the two kept reads, so a slow drift is followed. Its uncertainty is half of each round trip,
plus 0.01 s (the resolution of `/proc/uptime`), plus the difference of the two offsets (in the first
live runs on an Android 14 emulator the mapping was known in every run, with an uncertainty of
0.033 and 0.037 s and offsets 0.001 and 0.005 s apart). The counts
are unknown when a group gave no read, when the two offsets differ by more than 0.1 s plus half of
each round trip (the emulator was paused, or a clock was set), when the uncertainty is above 0.2 s
("adb answered too slowly"), or when the mapped launch or end of the last step falls outside the
time this machine ran the device script (widened by the uncertainty); the reason gives the times.
A device whose adb round trip is long (a remote or rented device) therefore cannot have its
requests counted. Where a rest borders an input step (the launch counts as one), in either order,
the input step takes the zone of width `u` (the uncertainty) on the rest's side and the rest's
window shrinks by as much: a rest followed by a tap ends `u` before where it would end (the tap's
start mark, or its lookup's first swipe), and a rest after a tap starts no earlier than the tap's
end plus `u`. A request the clock cannot place on one side of the border is counted in the step,
once. A step takes at most half of the rest's length (with its gap), so that two steps on either
side of a short rest meet in its middle and never cross; between two rests, or two input steps, the
border is the mark itself. A first or last step that is a rest is widened outward by `u`. The zone
goes to the step because no bound on `u` keeps a tap's own request out of the rest before it: on an
Android 11 emulator an input reaches the app 0.43 to 0.46 s after its step's start mark (`input`
starts a runtime first), but `input tap` returned in about 0.01 s on an Android 14 emulator, where
`input` is `cmd input`, and a certifying run there (uncertainty 0.062 s, the machine under load)
saw two taps' own requests reach the front 10 to 26 ms after their start marks. The cost: a request
the app sends at rest within `u` of an input step is counted in that step, where the step's budget
(`expect.requests_max`) holds it and the rest rule does not; with `u` at 0.03 to 0.06 s and a rest
of 5 s or more that is about 1 to 2 % of the rest. The gate says how far each rest was narrowed
once per run when the uncertainty is above 0.1 s.

**The gate.** A rest step fails (`rest-requests`) when the app sent more requests than
`gate.rest_requests_max` (default 0) or the step's `allow.requests_max` (with its `reason`, as the
other allowances), naming the paths. Any step with `expect.requests_max: N` fails (`requests`) above
N, and so does the launch step above the scenario's `launch_requests_max` (the budget for the launch
step's request count; check `requests` on step `launch`, naming the paths). When the run was given
`--requests` and its counts are unknown, every rest step and every step with a request budget
(`expect.requests_max`, or `launch_requests_max` for the launch) fails with the reason. When the run
was given no request log there is no request check at all, except that a step with a request budget
fails: "not counted: the run was given no request log". The budgets are read from the scenario kept
with the run, so a run captured without them is gated as before.

**An app that sends a request on a timer.** An app that sends one known request at any moment by
design (a session kept alive once a minute, at a random phase) can have it fall in any step. A
budget with one request of slack for it (`allow.requests_max: 1` on a rest, or a step's
`expect.requests_max` raised by one) lets any new request through instead, which is the fault a
budget exists to catch. Name it instead in the scenario's gate block:
`"gate": {"periodic_requests": {"GET /api/v1/x/sync": 1}}`, a request as the front's log gives it
(method, one space, the path's shape) to how many any one step may hold on the timer's account. In
every step where a request limit or budget applies (a rest against `rest_requests_max` or its
`allow.requests_max`, a step with `expect.requests_max`, the launch with `launch_requests_max`),
requests of a periodic kind are set aside up to their number, and what remains (other paths, and
the excess of a periodic one) is held against the limit or budget. A step that sends the same
request itself (a refresh that includes it) has one of its own set aside too, so its budget is the
count that remains. A failure still names every path it counted and gives the held number and what
was set aside ("1 of a periodic kind set aside: GET /api/v1/x/sync x1"); a step that passed with
requests set aside gets one note naming them and the gate block as the source, so every timer
request shows in the gate's output (a step with no limit and no budget gets no note). Setting aside
needs every path of the step, and `run.json` keeps only the 20 most frequent paths of a step: when
any was cut away, a step where a limit or budget applies fails as unknown rather than guess.
`run.json`, `summary` and `compare` keep the raw counts; `gate.json`'s `limits` shows the map when
the scenario has one.

**Limits.** HTTP/1.x only: on Android the app must talk plain HTTP to the front (the emulator
is told nothing; point the app's backend address at it), and tls-front never offers HTTP/2. An app
that keeps a WebSocket open, or a client that asks for an upgrade to HTTP/2 on plain HTTP, cannot
have its requests counted by a front: every run of it is unknown. That includes a client that offers
an upgrade to HTTP/2 on plain HTTP by itself (Java's `java.net.http` does, and curl with `--http2`).
A backend that restarts during a run makes the run unknown too (the front logs the connections it
refused). A streaming response (server-sent events, a long poll) is one request, however long it
lasts. Only traffic through the front is counted: a request to any other host, or one a cache inside
the app answered, is not seen. The front should carry nothing but the app under test during a run,
since it cannot tell two clients apart. On Android a lookup's reads of the window tree (`find`) lie
inside the tail of the rest before the step, so a request such a read caused in the app would be held
against that rest.

## Input on the iOS Simulator

A scenario with a tap, a swipe or a link on a simulator drives them through ui-probe's input agent: one
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
  idle limit (the scenario's longest wait plus 120 s; a tap with `find.scroll` counts every swipe it
  may replay with its settle time, and in a rehearsal each lookup's whole timeout) passed as `TEST_RUNNER_UI_PROBE_AGENT_*`
  variables, which `xcodebuild` hands to the test without the prefix. The run waits for its first
  answer (120 s); an `xcodebuild` that exits first fails the run with the tail of its log
  (`raw/agent.log`). This installs the agent's runner app (`dev.uiprobe.agent.uitests.xctrunner`)
  on the simulator for the run's duration.
- **What it does.** `ping` (the app's state, the display's size in points and whether it serves
  lookups), `tap` (`x`, `y`), `swipe` (`from`, `to`, `ms`), `alert` and `quit`, one JSON object per
  connection. Taps and drags go through the system app at fractions of the display. `alert`
  (`{"cmd": "alert", "button": "Open", "timeout_s": 5}`, answered `{"ok": true, "tapped": true}` or
  `false`) waits up to `timeout_s` (at most 30) for a button with that label on an alert of the
  system's own process (SpringBoard) and taps it if one appears. It queries SpringBoard alone, never
  the app under test, so it is served in a measured run: a read of the measured app's accessibility
  tree would change that app for the rest of its life. Started for a
  measured run, the agent reads nothing of the app under test but its state and refuses `find`,
  `type` and `labels`. Started for a rehearsal (`TEST_RUNNER_UI_PROBE_AGENT_LOOKUPS=1`, set by
  `ui-probe rehearse` only, and removed from what a measured run's agent inherits), it also serves
  `find` (every element whose accessibility label is the text, or with `text` whose value is, in
  the order of the app's tree, the first 50 with their frame, the point to tap, whether part of
  them is on the display, and XCTest's hit test for the first ten on the display), `type` (into the
  element with keyboard focus, one character at a time) and `labels` (the app's labelled
  elements with the visible part of their frame and their depth; with `"all": true` every
  element, up to 3000). The host checks the mode at the first `ping`: an agent in the other mode is a failed
  start. Every command carries the run's token and a fresh random nonce; anything
  without the token is answered with an error and does nothing. Every reply carries the
  HMAC-SHA256 of the nonce keyed with the token, which the host checks: a reply without it (the
  first `ping` included) did not come from the agent, and the command fails. A request must arrive
  in full within 5 s of its connection. An issue the test framework records while a command is
  handled (a gesture it could not perform) makes that command's reply a failure carrying the
  issue's text. It listens on 127.0.0.1 only, and ends by itself once no command has arrived for the
  idle limit, so a killed host cannot leave it running.
- **Per step.** A tap or a swipe: the recorder started and awaited, CPU read, the command sent,
  `settle_ms` waited, CPU read, a `ping` for the app's state, the recorder stopped. A link (`url`):
  the same, with `xcrun simctl openurl` and then the agent's `alert` for `Open` (5 s) as the input
  (`raw/step-<step>.json` keeps `url_open`: whether it opened, simctl's exit status and what it
  printed when it failed, and `alert_confirmed`; the agent's command and reply as for a tap). The
  `ping` at the end checks that the app is in front after the link, as after a tap.
- **A system alert left behind.** The alert outlives the app and sits above it after the next
  launch, where it would take the run's taps. So a run of a scenario with a link, once the agent is
  up and before the launch (a rehearsal: after the launch, before its first step, since a
  rehearsal starts its agent after the app), asks the agent for `Cancel` with a
  1 s timeout, and keeps the answer in `raw/stale-alert.json` (`stale_alert` in `run.json` and in
  `rehearsal.json`). A run that cancelled one says so in a note; an agent that could not look for
  one fails the launch (check `input`), and stops a rehearsal. A run or a rehearsal that opened a
  link and then stopped before confirming it leaves its alert up: the next run of a scenario with a
  link clears it this way. A scenario without a link sends nothing new to the agent. A rest: as
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
- **Why a measured run never looks up or types.** An accessibility read of an app can change the
  app it reads. Measured on one app: after one lookup of a label in it (or typing through one of
  its elements), it used 0.8% to 1.6% of a core at rest for the rest of its process's life, also
  after the agent had quit, and once presented a frame at rest; with the agent idle and reading
  only the app's state, the app at rest was unchanged. To measure this for another app: a rest,
  one lookup, the same rest again, each recorded with CPU. So lookups and typing happen in a
  rehearsal, in an app process that is then ended (next section).

## A rehearsal on the iOS Simulator

`ui-probe rehearse --simulator <udid> --scenario <file> --out <dir>` drives a scenario once in an
app process that is thrown away; there, the agent may look elements up and type. Nothing is
recorded, measured or gated. `ui-probe run --simulator <udid> --scenario <file> --out <dir>
--rehearsal <dir>/rehearsal.json` then replays the positions it found, by coordinate, in a fresh
process, which is the one measured. A scenario with a tap by `find` cannot run on a simulator
without `--rehearsal`, and the refusal says what to run first.

- **The scenario.** One file serves both commands. `rehearse` checks it as `run` does (`expect`,
  `allow` and rest lengths are checked and then ignored), except that a `text` step is allowed.
  A scenario with no `find` and no `text` can be rehearsed: it just drives the app. Typing, such as
  a sign-in, goes in a scenario of its own that only `rehearse` drives, before the measured
  scenarios; a run never types.
- **What `rehearse` does.** It refuses `--serial` (on Android a lookup happens inside the run), an
  `--out` that holds a run at any depth or a rehearsal, or lies inside another rehearsal's folder,
  a simulator that is not booted and an app that is not installed; every refusal comes before
  `<dir>` exists, and leaves nothing.
  It launches the app as a run does (terminated, `xcrun simctl launch` with `launch_env`), then
  starts the agent for a rehearsal and checks that its first `ping` says so, and waits what is left
  of `launch_settle_ms` counted from the launch, then requires the app in front (a `ping`, state 4);
  no status-bar override, no recording, no read of the log store. The app comes first because the agent's first read of the app ties its later
  reads of the app's tree to the process running then, so a lookup after a relaunch would wait on
  a process that is gone; a measured run, which never reads the tree, keeps starting the agent
  first. Then, in order: a tap with `find`
  is looked up (below), tapped at the centre found and given its `settle_ms`; a tap by position
  and a swipe are sent and given their `settle_ms`; a `text` step is typed into the element with
  keyboard focus (about 1.5 s a character) and given its `settle_ms`; a rest waits its full `ms`.
  So a rehearsal waits what a run waits between two inputs, apart from the recorder's own time, and
  its lookups read the screen the run will tap. Every step ends with a `ping`: the app must be in
  front (state 4), else the rehearsal stops naming the step and the state, in the words of the
  run's foreground check. A command the agent did not perform, or a reply not proven to come from
  it, stops the rehearsal and names the step. Then, however it ended, the app is terminated and the launched
  process must be gone within 5 s (else the rehearsal fails: a run would measure the rehearsed
  process), and the agent is stopped as after a run. It prints one JSON line per step and a last
  line naming `rehearsal.json`, and exits 0, or 2 on a failure (`failed.json` then, as for a run).
- **A lookup** follows the Android rule. The agent's `find` reads the app's tree once. A match
  counts when part of it is on the display (`on_screen`) and XCTest's own hit test says a tap would
  reach it (`hittable`); the agent asks the hit test for the first ten matches on the display.
  The verdict knows what is drawn over what: measured on two screens of an app that draws itself
  (20 elements), it was right for a bar at the top over scrolled content, a floating bar at the
  bottom over a list, elements scrolled out of view and every plainly visible element. It was
  wrong once: a button cut by the lower edge of the area it scrolls in, with nothing drawn over the
  cut part, because the hit test tries the middle of the whole frame. The point the tap goes to
  (`center`) deals with that case: it is the middle of the part on the display, and when that point
  is cut off by an element the match lies inside (scrolled content keeps its whole frame; the
  scrolling element's frame cuts it), the middle of the part left (`visible`). `under`, the label
  of an element later in the tree over the tap's point, is a hint for a message and decides
  nothing; `moved` (the tree changed between the agent's read and its hit test) and `not_asked`
  (more than ten matches on the display, or more than twenty elements with the label) come with
  `hittable` false. What verifies a replayed tap in the measured run is an approved
  `expect.signature` on the step: without one, the gate catches only a replayed tap that changed
  nothing on the screen (check `input`, below); a tap that lands on another control and changes the
  screen is not caught. Give every replayed tap whose transition is stable an `expect.signature`,
  and a target near a bar a `find.scroll`, so that it ends up clear of it. Without `nth` exactly one match may count; with `nth`, at least that many, and the nth
  in the order of the tree is taken. It is found when it has the same frame in two reads in a row.
  Reads are about 300 ms apart until `find.timeout_ms` (the default as on Android); a match first
  seen at the timeout gets one more read. With `find.scroll`, a read with no match that counts is
  followed by the swipe and its `settle_ms`, at most `max` times. The statuses are Android's:
  `found`, `not_found`, `not_visible` (matches there, none counted: off the screen, or on it where
  a tap would not reach them, with `under` named as a probable cause and `moved` and `not_asked`
  told apart) and `unstable` (a match that moved). A failed lookup writes `<dir>/lookup-failed.txt`
  (what was looked for, the status, the matches of the last read and the agent's `labels`, so the
  author can see what the screen held) and stops with a message built as Android's. Only the first
  50 matches are listed, so without `nth` a read that found more cannot show exactly one.
- **`rehearsal.json`** holds the tool and version, `"platform": "ios-simulator"`, the scenario's
  name and digest (sha256 of its canonical JSON: sorted keys, compact separators), the udid and the
  simulator's facts, the screen size in points from the first `ping`, the app's identity (the
  installed bundle's `CFBundleShortVersionString` and `CFBundleVersion` from its `Info.plist`, the
  sha256 of its executable, and a digest of the whole bundle: sha256 over the sorted list of every
  file's path relative to the bundle and its sha256, a symbolic link by its target text, never
  followed; a value that cannot be read, a bundle not read in full included, is recorded as
  unknown), `started` and
  `finished` (UTC), and per step what was done: for a lookup the `find` as written, the match used
  (label, type, frame, and `visible` when the tap point was moved into a part of it), its `center`
  as fractions of the display, the swipes made to reach it, the reads, the matches and the time it
  took; for a `text` step the number of characters typed, never the text; for every step the
  app's state at its end (`app_state`). `<dir>/raw/` keeps the
  agent's log, `agent.json` and `launch.txt`. When the scenario types, `lookup-failed.txt` holds no
  element value at all (a field shows typed text reformatted, which no search for the text would
  catch; labels and frames stay), and any other string holding the typed text is written as
  `<typed text>`.
- **What `run --rehearsal` checks** before the first run starts, each refusal saying why: the
  rehearsal's scenario digest is this scenario's; its udid is this simulator; it holds a position
  for every tap with `find`; the app's identity is unchanged, each of its four values compared and
  the one that differs named (a framework or a resource that changed changes the bundle digest; an
  identity that cannot be read, then or now, fails the check); it finished at most `--rehearsal-max-age-s` seconds ago (default
  3600, a whole number of at least 1). `--rehearsal` with `--serial`, or on a scenario with no tap
  by `find`, is refused. At the agent's first `ping` the screen size must equal the rehearsal's,
  else the run fails. `--runs N` uses the same rehearsal for every run.
- **The replay.** Before a tap with `find` starts its recorder, the run sends the rehearsal's
  swipes for that step, each followed by its `settle_ms` (as on Android, where the lookup and its
  scrolling come before the step's window); then the step is a tap at the rehearsed centre.
  `raw/step-<step>.json` and `run.json` record it as `resolved`: `x`, `y`, the swipes with the
  agent's replies and round trips, the rehearsal file and its finish time. A replayed swipe the
  agent did not perform fails the step (check `input`), as a tap it did not perform does. A
  replayed tap whose recording shows no visual change (signature `-`) fails as `input` too (it
  changed nothing on the screen, so it probably missed its target), unless the step's
  `expect.signature` is `-`; a recording that could not be used fails as `recording` and, for a
  replayed tap, as `input` (whether it reached its target cannot be told); a signature that is
  unknown for a usable recording is a note. The gate
  adds one note per run that used a rehearsal (how many taps, the rehearsal's finish time).
- **Limits.** A position is only as good as the screen being the same in both passes: the
  backend's content must not change between the two passes, and a step that changes server state
  must be the last lookup of its scenario (after it, the screen a rehearsal saw is not the screen
  the run will see). The rehearsal's app process is ended, but what the app stored (a signed-in
  session, a preference) stays for the measured run, which is how a sign-in rehearsed in a scenario
  of its own carries over.
- **Typed text in kept files.** The test framework logs one line per typed character in
  `raw/agent.log` (`Type '5' into Any (First Match)`). When the scenario has a `text` step, the log
  is rewritten after the agent has stopped with the quoted text of every such line replaced by
  `<typed text>` (the rest of the line kept), and the agent's result bundle and derived data, which
  hold the same lines, are removed whatever the test run's status; `rehearsal.json` or
  `failed.json` says so (`agent_results`).
- **A rehearsal folder is not a run.** `rehearse` writes `rehearsal-started.json` into `<dir>`
  before anything else, so that a rehearsal that failed or was killed is recognised too. `gate`,
  `summary` and `compare` pass over a folder holding it, with its own `raw/` and `failed.json`, when
  they look for runs; any other run marker inside it (a run written there by hand) stops them with
  an error naming the folder, so no run is ever skipped in silence. `gate` prints one note for each
  rehearsal folder whose rehearsal did not complete, naming why. `run --out` refuses a folder that
  holds a rehearsal (its marker or `rehearsal.json`) or lies inside one, and `rehearse --out` one
  that holds a run at any depth or a rehearsal, or lies inside another rehearsal.

## What each Android version gives

- **Android 12 and newer (API 31+)**: everything above. The compositor's per-layer statistics
  carry each layer's uid; `--trace` records the frame timeline. A trace is read exactly as 0.6.0
  read it, also one that holds no frame-timeline packet, and also any trace from a capture that gives
  no API level.
- **Android 11 and older (API 30 and lower)**, when the capture gives that API level and the trace
  holds no frame-timeline packet: no frame timeline. A `--trace` run reports the
  frame-timeline counts (`frames_compositor`) as unavailable, never 0, with a note that the device
  has none; `--sections` still counts trace sections, over the span the ftrace data source itself
  recorded (see "A trace covers only the time it holds" below). The compositor's per-layer
  statistics print no uid: a layer is the app's only when its name starts with `<package>/` or with
  `SurfaceView - <package>/` (a name that merely holds the package is another app's). Measured on
  an Android 11 emulator, where 0.6.0 and older read no layer and left `frames_layers` unknown, and
  covered no step after the launch with sections: re-analysing such a capture with 0.7.0 gives
  both.

## Known limits

- **Frames that changed nothing are set aside by the compositor's counts (Android).** Only those
  beyond the app's own frames (F, the largest of its counts) that no recorded change in its area
  accounts for, and no more than the display composed beyond F (see "The gate"). The number of the
  app's own frames this can set aside in a rest is at most the sum of two things, and is 0 when both
  are 0. (1) Recorded changes in the app's area that none of the app's frames made: encoder noise
  that the recording counts in the app's area, or another window drawn over it (the keyboard, a
  toast, a system dialog). Each such frame can hide one frame of the app's that changed nothing; the
  frame composed for that window also gives the display its extra composition. (2) Frames of the
  app that every app-side count misses: a surface the app draws outside the platform's renderer (a
  video or GL surface of its own) whose layer the layers' count does not give to the app (on Android
  11 and older a layer name that does not start with the package, or an unknown uid). Each such
  frame can hide itself, and the frame check misses it too. In case (1) the frame check still
  counts the app's frames, and fails them unless the step's `allow.frames_max` covers them.

- **Android, and the iOS Simulator for rests, taps, swipes and links.** Nothing here drives a physical
  iPhone. On a simulator the app's frame counts come from its log store, known only when the run
  read the store to the end; there is no quiet detection, and a measured run's input is by
  position only: lookups and typing happen in a rehearsal (an accessibility read can change the
  app it reads), whose positions hold only while the screen is the same in both passes.
  On a simulator a link's alert is confirmed by its label (`Open`) and a stale one cancelled by
  `Cancel`: another language or iOS version may label them differently. A link whose alert was not
  confirmed within the 5 s wait passes with a note on its step, since a link may open without
  asking: if the system asked later, the app has not opened the link, the alert stays above the
  app, and the steps after it are measured under the alert. Approve an `expect.signature` on a later
  step to catch that. In a rehearsal a stale alert is cancelled after the launch, where a run
  cancels it before: a rehearsal starts its agent after the app (see "A rehearsal on the iOS
  Simulator"), and only the agent
  can look. An alert with a `Cancel` button that the app itself raises while the agent starts is
  therefore cancelled in a rehearsal and not in a run.
- **A simulator asks about a link once.** The first link an app is handed on a simulator brings up
  the system's alert; later links open directly, also after the app is installed again (measured
  on iOS 26.1). The recording of a `url` step holds the alert the first time and not afterwards,
  so its signature differs between the two: do not approve a signature on a `url` step. What
  follows the step is the same either way.
- **A link into a running Android app opens whatever the app's launch mode gives.** An activity
  declared `standard` gets a second instance for the link, above the first, and the step measures
  that (its frames, its requests, and whatever the second instance goes on doing at rest). To
  measure the link's screen itself, start the app by the link (`launch_url`).
- **Per-frame timings assume the device has not slept since boot.** Frame rows use the monotonic
  clock and step marks the boot clock; they agree only then. The run is flagged when they do not
  line up. Counts are unaffected; the recording then no longer decides a rest window.
- **Per-frame records hold the last 120 frames.** Keep steps short enough that fewer are drawn
  between two snapshots, or rely on the totals, which are not limited.
- **A trace covers only the time it holds.** A trace that stopped early gives no trace counts
  for the steps outside its span; the run lists those steps (`trace_uncovered_steps`, and
  `sections_uncovered_steps` for sections). On Android 12 and later, and for any capture that gives
  no API level, the span is that of the trace's stamped packets, for the frame timeline and for
  sections alike, as in 0.6.0. On Android 11 and older without a frame-timeline packet, the stamped
  packets end early (an ftrace event bundle carries no packet timestamp), so the span for sections
  is that of the ftrace events themselves. It is widened to the ftrace data source's own run only
  when the trace holds the data source's statistics (`FtraceStats`) exactly once for the start and
  once for the end of the trace: their per-CPU `now_ts` are then the bounds (the latest start and
  the earliest end) if every one of them is above 0 and the start lies before the end; statistics
  that hold no `now_ts` let the session perfetto printed widen the events' span instead
  (`capture.json`'s tracer output, on the same boot clock). Anything else leaves the events' own
  span. A screen at rest emits no event, so the last event can
  come long before the end of the last step while the data source still recorded. A trace whose
  buffer overwrote data cannot say which steps lost it, so all of its counts are dropped.
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
- **One client per front.** A front's log holds what reached it, not who sent it: a second
  device running the app, a browser or a script that reaches the backend through the same front
  during a run has its requests counted in that run's steps. Give each client that runs at the
  same time a front and a log of its own, or run them one after another.
- **A request sent at rest within the clock's uncertainty of an input step counts in that step.**
  On Android the request windows meet where the clock cannot place a request, and the input step
  takes that zone (see "Counting requests"): the rest rule does not see such a request, the step's
  `expect.requests_max` does. With an uncertainty of 0.03 to 0.06 s and a rest of 5 s or more, that
  is about 1 to 2 % of the rest.

- **A film is judged by its picture alone.** A change smaller than the minimum group (78 px at
  1080 x 2400: a dot of 6 x 6 px, a thin stroke that changes alone), or fainter than 10 levels (a
  whole-picture fade of 5 levels a frame that stops within half a second and between the window's
  ends), is not told from the recording and is not seen. A change made of separate pieces each under
  the minimum (a spinner of 8 x 8 px dots) is not seen either. A slow change counts once it reaches
  the minimum over 0.5 s, or between a window's ends; one that stays under it in both is not seen.
  A change that touches scattered noise or a flickering place joins it into one group, so the group
  can reach the minimum where the app's part alone would not; two scattered groups that touch count
  as one, and two of the largest measured (36 to 40 px) touching would reach it. A slow change on the
  very pixels of an allowed change (within 3 px) within half a second of it is taken as that change.
  The measured basis of every default: the level of 10 and the minimum of 3e-5 of the picture (twice
  the noise's largest group at 6 levels) come from one phone capture (H.264, 60 a second, 1206 x
  2622); the 0.5 s of the slow comparison is half its key-frame interval of 30 frames, and its
  minimum is the same 3e-5 until a real capture's 0.5 s comparisons are measured; the caution at 70 %
  of the minimum and the steady places at a quarter of the frames are this plugin's choices. Another
  encoder or bit rate may leave larger groups, which the caution line is there to show. The film is
  read as stored: a rotation in its metadata is not applied. Its system bars are the `insets_px`
  given, or none.
- **Regions are placed on the recording, and only the recording's count leaves the rest.** The
  frame counts do not say where a frame was drawn, so a countdown still needs `frames_max`. A
  change is placed by the boxes of touching changed blocks, widened by the blocks next to them, so a
  change that comes within a block of a region's edge from inside can be counted against the rest.
  Two changes in one block group (a countdown and a badge beside it) make one box. Outside the block
  groups a change made only of pieces smaller than the minimum (3e-5 of the picture, 78 px at 1080 x
  2400) is not counted against the rest.
- **`sheet --full` writes every recorded frame of a window at full resolution.** On a busy step of
  a long Android recording that can be many megabytes; a film's own folder keeps only the frames
  that changed and each window's first and last.

## Project shim

Device names, package ids, scenario locations, the reference device profile and whether the gate
blocks a landing are project facts. Keep them in your project's own rules; `SHIM-TEMPLATE.md`
shows the shape.
