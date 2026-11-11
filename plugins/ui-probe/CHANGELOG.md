# Changelog — ui-probe

## 0.4.0 — 2026-11-10

The iOS Simulator, rest steps only.

- `run --simulator <udid>` (instead of `--serial`; an empty value is refused) runs a scenario of
  rest steps on a booted iOS Simulator, driven from the host. The status bar is frozen for the run
  (`simctl status_bar … override --time 9:41`, replacing any override the user had set, and
  cleared at the end); the app is terminated, launched with `xcrun simctl launch` and given
  `launch_settle_ms`, and left running afterwards. For each rest the recorder (`xcrun simctl io …
  recordVideo`, in a process group of its own) is started and awaited ("Recording started"), the
  app's CPU time is read with `ps` before and after the step's wait, and the recorder's group is
  stopped with SIGINT (killed if it does not exit). The recorder is stopped and the status bar
  cleared on an error, an interrupt, SIGTERM and SIGHUP; SIGKILL cannot be caught.
- A simulator scenario names the app by its bundle identifier and holds rest steps of at least
  1000 ms only; `activity`, `quiet`, `count_sections`, `until_quiet` true, and `meminfo`,
  `background` or `expect` on a step are refused before anything runs, a step of another kind with
  a message that the iOS Simulator has no input in this version.
- Any frame recorded at rest fails: every frame after the first counts against the app (against
  `rest_frames_max` or `allow.frames_max`; those that changed any pixel against
  `allow.changed_frames_max`; those that changed nothing with no allowance), whatever the recording
  analysis classes it as (app's area, encoder noise, system bars only, nothing); the classes are
  named for the reader. CPU at rest is held against `rest_cpu_pct_max` or `allow.cpu_pct_max`; an
  unreadable CPU time, or one that went down, fails. A recorder that had stopped before its window
  ended fails the recording check. There is no exact frame count on a simulator, so the 0.3.0
  rule for frames recorded while the app presented none never applies there. Checks a simulator
  cannot feed are not run, and each run says so in a note.
- `analyze`, `sheet`, `gate`, `summary` (with keys of its own, `<step>.sim_*`), `compare`, the folder
  rules and `failed.json` work on a simulator run; `zoom` takes `--step`. `doctor --simulator
  <udid>` checks `xcrun simctl`, that the simulator exists and is booted, and `ffmpeg`/`ffprobe`,
  without requiring `adb`.

## 0.3.0 — 2026-11-10

For running the same scenarios under other device profiles: a small screen, a large font, a slow
network.

- `find.scroll` on a tap: while a read shows no acceptable match, the device script swipes
  (`from`, `to` as screen fractions, `ms`, default 600), waits (`settle_ms`, default 800) and reads
  again, at most `max` (1 to 20) times. The two-agreeing-reads rule still decides, and
  `timeout_ms` still bounds the whole lookup; without one, the default timeout adds room for every
  swipe (its time, its settle time and one read). The swipes happen before the step's start mark
  and its baseline, so they are charged to no step, and `find-<step>.txt` and `run.json` record
  how many were made. A swipe that would move less than 0.1 of the screen is refused: it would be
  a tap.
- A match must be on the screen: its bounds not empty and its centre on the display. The device
  script reads the display size once (`wm size`, the override size when one is set). A match in
  the tree but off the screen is "not yet", triggers a swipe with `scroll`, and otherwise ends the
  lookup as `not_visible` with its bounds and a message that suggests `find.scroll`. Without `nth`
  the match must be the only one on the screen; `nth` counts the ones on it. A display size the
  script cannot read ends the lookup at once (`no_display_size`).
- The host reads the display size the same way, so a coordinate tap or swipe under an emulated
  smaller screen lands where the scenario says; `capture.json` keeps the physical and the
  override size.
- `ui-probe proxy --map-host FROM=TO` (repeatable) connects a CONNECT target or a plain HTTP
  request whose host is FROM to TO, on the same port, for a backend an emulator names by the
  host's loopback alias (`10.0.2.2`), which the host cannot connect to. Both sides are checked
  (a host name or an IP address, no port); a host mapped twice or to itself is refused. Each log
  line carries `connected_to` beside the target as named.
- What the recording shows during a rest is no longer a violation when the app presented no
  frame by any count (the platform's and the compositor's per-layer count both known and 0, a
  trace's 0 or absent): recorded frames that changed nothing and a recorded change in the app's
  area become a note (one for both) naming another layer or the encoder as the cause, with the
  `zoom` command. Seen on a clean build, on still screens with every count of the app's frames at
  0: a status-bar change made the display compose (once, two frames 14 ms apart), and the recorder
  kept a frame that changed nothing, or one whose re-encoding noise along text edges crossed the
  threshold in the app's area; one run in about ten failed on it. An unknown or non-zero count keeps both violations, so an animation
  nobody can see, which presents frames, still fails; `allow.changed_frames_max` is judged as
  before, and there is still no allowance for frames that changed nothing.
- README, from device runs under other profiles: a caret can count as a visible change on a
  smaller display, so its rest takes `changed_frames_max` beside `frames_max`; the composition
  counts of a first open that waits on the network agreed five times and differed the sixth, so
  a count is a baseline only for a step whose signature also agrees; and a flash inside a
  transition that already has a detour is not told apart by the signature.

## 0.2.0 — 2026-11-10

Four additions for flows that type into a form and screens that are never still.

- A `text` step types into the focused field. Only letters, digits, space and `@ . _ + -` are
  accepted (a space is sent as `input text` spells it); anything else is an error that names the
  character, so nothing reaches the device shell that it could read another way.
- An `allow` on a rest step raises that step's limits for frames, CPU and recorded frames that
  change the app's area, with a required reason. A focused field's caret draws 10 or 11 frames in
  a 5 s rest window, so without it such a screen could not be gated at all. There is no allowance
  for recorded frames that changed nothing: a caret does not produce them, and that check is what
  catches an invisible animation. A violation on such a step says the limit came from the
  allowance, and a step that passed only because of it gets a note with the value, the allowance
  and the reason.
- A per-step `quiet` replaces the scenario's quiet window for that step, for example a longer
  limit on one slow step. It is not the answer to a caret: a step that ends on a focused field
  takes a fixed wait (`"until_quiet": false`), because the poll that watches for quiet takes
  about as long as the gap between two blinks.
- A tap can name its target (`find`: text or content description, exact, optionally the nth
  match) instead of coordinates. The device script looks it up before the step starts, retrying
  until the element is there, so it also waits for the screen. Only an element of the scenario's
  package counts (`find.package` names another), and only when two reads in a row give it the same
  bounds, so the tap never goes to another app's element or to where an element was while it
  moved; a match that never holds still ends the lookup as `unstable`. A lookup value the window
  tree dump never writes as it is (a tab, a control character, a surrogate, a character outside the
  Basic Multilingual Plane) is refused when the scenario is checked, and `find.timeout_ms` is at
  least 100. Reading the window tree makes the
  app and the compositor work (measured on a still screen: 1 frame, 9 recorded frames that changed
  nothing, 9 CPU ticks for two reads), so after a lookup the script waits, takes a baseline of
  every counter, and the step counts from there; the lookup lies in no step's window. The last
  tree read and the lookup's record are kept in `raw/`, and `run.json` carries the centre, the
  bounds, the matches and the reads. An element that never appears ends the run with exit 2 and a
  message naming the step, what was looked for, the matches at the last read (and matching
  elements in other packages) and the saved tree; the device is restored as on every other error
  path. The step that stopped is named in `raw/lookup-failed.txt`, a name no step's own record can
  take.
- A run that does not complete leaves `failed.json` in its folder (the error, the step if known,
  the time). `gate` fails a folder that a run started but did not complete (check `run`), even
  when it is the only one, so a batch that stopped part way never passes on the runs before the
  failure; `summary` lists such runs and leaves them out of every statistic, and `compare` names
  the ones it left out on each side. `run` refuses a folder that already holds any part of a run.
- `gate --scenario` refuses a scenario whose name differs from the one stored with the run, and
  says on each `allowance:` line when the allowance came from `--scenario`.
- `doctor --serial` reports whether the device has `uiautomator` (needed by `find` only).
- The README says what a device run showed about CPU at rest: a caret costs up to 9 ticks in 5 s,
  so its allowance should raise the CPU limit too, and a clean app's still screens are not
  always at zero ticks, so a single CPU-only window just over the limit is run again first.
- The README now says what the code already did: a key ending in `comment` is allowed in the
  scenario and in a step, not inside the nested blocks.

## 0.1.0 — 2026-11-10

First release. Android, driven over adb from an unmodified build of the app under test.

- `run`: play a scenario file (launch, rest, tap, swipe, key) on a device and keep independent
  witnesses of every step: the platform's frame records, the compositor's per-layer statistics
  (any toolkit, including a surface the app draws itself), a screen recording, and optionally
  the compositor's frame timeline (`--trace`) and trace-section counts (`--sections`). Steps can
  wait until the app and the display go quiet. The scenario file is checked strictly: an unknown
  key or a wrong type stops the run before it touches the device.
- `analyze`, `summary`: redo the analysis from the saved capture; statistics across runs.
- `gate`: the checks that need no baseline (frames, CPU and recorded frames while the screen is
  at rest; a screen that never goes quiet; process restarts) plus any expectations a scenario
  states per step. Exit 1 on a violation, `--advisory` to report only.
- A pass means the app was observed. A value that could not be read is unknown, never zero, and
  the gate fails or says so in a note instead of passing in silence: an app that did not start
  or drew nothing while launching, another app in front during a rest, a missing snapshot, a
  capture that was never analysed, a recording that is missing, stopped early or cannot be
  placed in the step windows, a trace that is missing, lost data or does not cover a step, a
  trace section the app never emitted.
- `compare`: base runs against head runs with a rank test (exact for small samples) and an
  effect size; answers different, same or unknown per metric.
- `sheet`, `zoom`: a labelled contact sheet per step, and a full-resolution look at what changed
  between two recorded frames.
- `proxy`: a forward proxy that adds latency and a rate limit per connection without decrypting
  anything, for imposing a slow network with no administrator rights.
- `doctor`: host and device prerequisites, including ffmpeg 5.1 or newer.
- Exit codes: 0 success, 1 a failing gate only, 2 an error the tool reports itself, 3 an
  unexpected error with a traceback.
- The device is left as it was found: the device script restores the compositor's statistics
  switch and stops the recorder however it ends, and the host stops the tracer and cleans up
  when a run fails.
- `scripts/install.sh` installs a versioned copy and a `ui-probe` launcher. An optional pinned
  installer fetches the Perfetto trace processor (v59.1); the probe itself reads traces with a
  built-in reader and does not need it.
