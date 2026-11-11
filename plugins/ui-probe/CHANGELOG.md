# Changelog — ui-probe

## 0.7.1 — 2026-11-11

- A request log replaced during a run is told by its first bytes as well as by its inode and device.
  A file system may give a new file the number of one just removed: on Linux (GitHub's runner) a log
  removed and written again read as the same file, cut, where macOS gives another number. The run
  keeps the log's first 4096 bytes at the start (in memory; nothing new is written) and compares
  them with the same bytes of the file that has the name at the end, read from one open file. A
  different file is called replaced; only the same file grown shorter is called cut (a file shorter
  than the bytes kept is cut when what it still has is their start, replaced otherwise). Either way
  the run's counts are unknown, as before.
- A front's command line, for the check that no other front writes the same log, is read from
  `/proc/<pid>/cmdline` where there is one and from `ps -o command=` elsewhere: a Linux container
  without `ps` checked nothing.
- Tests: the replaced-or-cut test means the same on every file system (an in-place rewrite stands
  for a reused inode number, `os.replace` of a file made beside the log for a new one); the test of
  a folder that cannot be written is skipped when the suite runs as root, as many containers do.

## 0.7.0 — 2026-11-11

Requests counted from outside the app, per step, by the front the app's traffic passes through. A
count is known only when the run can show the front saw everything the app sent; anything else is
unknown, never 0, and fails every rest.

- The fronts share a module, `httppipe.py`: the byte pipe, the log, and a reader of HTTP/1.x
  requests that observes the client's bytes after they were handed on (never holding one back,
  reordering or changing it). `tls-front` keeps its command line, its state folder rules and its
  messages; it moved onto the shared pipe.
- `ui-probe http-front --port P --to-port Q [--log FILE]`: the same pipe for a plain backend, with
  no TLS and no state folder, on 127.0.0.1 only, printing `listening on http://127.0.0.1:P ->
  http://127.0.0.1:Q`; for an Android emulator, which reaches this machine as 10.0.2.2. `--port` and
  `--to-port` must differ. Installed with the other scripts and reachable through the launcher.
- With `--log`, both fronts write (one `write` per line, appended; the file is made 0600 at every
  write, whether it existed or not) a `listening` line when they start listening (`port`, `to_port`,
  `pid`), a `request` line the moment a request's head is complete (`conn`, `method`, the **shape**
  of the path without its query string, cut to 300 characters, `query` true or false), an
  `unparsed` line once for a connection that is not HTTP/1.x (an HTTP/2 preface, a head over 64 KiB,
  a start line or a header that does not parse, a body length that cannot be read), and an
  `upgrade` line after a request with `Connection: upgrade` or a CONNECT. A path segment is written
  as it is only if it matches `[a-z][a-z._-]{0,31}` (lower case only) or `v[0-9]{1,3}`; any other,
  a camelCase word included, is `:id` (after the host of an absolute-form target is removed). The `closed` line gains `conn` and `requests`;
  `backend-refused` is unchanged. The reader handles `Content-Length`, chunked transfer coding
  (sizes, extensions, trailers), keep-alive and pipelining, and heads and chunk sizes split at any
  byte. No header value, query string or body is ever logged. **Changed:** a front's log now starts
  with the `listening` line, holds request lines, where it held only `closed` and `backend-refused`
  lines, and is 0600.
- Both fronts stop when told to: on SIGTERM, SIGINT, or SIGHUP unless it was ignored when they started
  (as under `nohup`), they write `{"t", "event": "stopping", "signal"}`, stop accepting, close every
  connection they hold (each gets its `closed` line) and exit within 2 s, whatever the clients do. **Changed:** `tls-front` used to wait for its clients'
  connections to close, so an app's keep-alive connection kept a stopped front alive, and a script
  that restarted it left two processes writing one log. One log, one front: a front refuses to start
  (exit 2, before it listens) on a log whose last `listening` line names another running front
  (`ps -o command=` holds `http-front` or `tls-front`) whose `--log` resolves to the same file; a pid
  that is gone, is another program, or is a front on another log or none is no obstacle.
- `ui-probe run ... --requests FILE` (Android and the iOS Simulator): refused before a run folder
  exists when FILE is absent, holds no `listening` line, or the front process its last `listening`
  line names is not running. Each run notes where the log ends and whether the front runs; after the
  device part it checks the front again, keeps the `request`, `unparsed`, `upgrade` and
  `backend-refused` lines appended in the run's window (from the launch to the end of the last
  step, 2 s margin), follows every connection the same front process stopped reading (from its
  `upgrade` or `unparsed` line to its `closed` line, back to that process's `listening` line), and
  checks that the front's port still accepts a TCP connection within 2 s (the probe sends nothing),
  and saves `raw/requests.jsonl` and `raw/requests-meta.json` (path, offsets, window, clock mapping,
  the front's pid and state at the start and the end, whether it accepted at the end, those
  connections, reason), both 0600; a run that cannot write them fails. `capture.json` records
  `requests_log`.
- Android: the device script is unchanged (byte for byte the same as 0.6.0's). The host reads the
  device's `/proc/uptime` through `adb shell` three times before the device script and three times
  after it, keeps the read with the shortest round trip in each group, and maps uptime to its own
  clock by the straight line through the two. Its uncertainty (half of each round trip, plus 0.01 s,
  plus the difference of the two offsets) is a zone at each border between a rest and an input step
  (the launch counts as one), in either order: the input step takes the zone, at most half of the
  rest's length with its gap (between two rests, or two input steps, the mark is the border), so that
  every request is counted in one step at most. The zone goes to the step because an input can reach
  the app about 0.01 s after its step's start mark on Android 14 (0.43 to 0.46 s on Android 11): a
  certifying run counted two taps' own requests in the rests before them when the rest took it. A
  request sent at rest within the uncertainty of an input step is therefore counted in the step
  (about 1 to 2 % of a 5 s rest). Unknown when a group has no read, the offsets differ by more than
  0.1 s plus half of each round trip, the uncertainty is above 0.2 s (a device with a long adb round
  trip cannot have its requests counted), or the mapped launch or end of the last step lies outside
  the time this machine ran the device script.
- `run.json`: each step, launch included, gets `requests` (`count` and the 20 most frequent
  `METHOD /path` in `by_path`, or `count` null with its `reason`; a rest also `gap_s` and `in_gap`),
  and the run a `requests` block (known, reason, totals, `between_steps` and `between_paths`,
  `in_margins`, the front, the clock). A rest's request window runs on to where the next step starts
  putting anything into the app, and for the last step to the end of the run's window; the counts of
  the steps plus `between_steps` plus `in_margins` equal the requests kept. Counts are unknown when
  the front was not running at the start or the end, no longer accepted at the end, started listening
  again at any time after the run began, changed its pid, was told to stop after the run began,
  logged `backend-refused` during the run, or carried a
  connection it could not read at any time during the run (one opened before it included); when no
  request lies between the launch and the end of the last step; when the log was cut or replaced or
  holds a line since the front's `listening` line that is not its JSON; when a run given a log kept
  no record of it; and, on Android, when the clock could not be mapped. `summary` and `compare` see
  `<step>.requests` (Android) and `<step>.sim_requests` (simulator) as numbers; a run whose count is
  unknown is left out of that metric. **Changed:** `compare` of a metric with no value on either side
  now says `unknown` ("missing on both sides"), where it said `same`, and each printed row of
  `compare` now ends with how many values each side had ("values: 5 base, 0 head").
- The gate: a rest with more requests than `gate.rest_requests_max` (new, default 0) or its
  `allow.requests_max` (new, with the step's `reason`) fails as `rest-requests`, naming the paths and
  how many came in the gap before the next step; any step over `expect.requests_max` (new) fails as
  `requests`, and so does the launch over the scenario's `launch_requests_max` (new, top level, a whole
  number of 0 or more, both platforms: the budget for the launch step's request count, read from the
  scenario kept with the run). With a request log and unknown counts, every rest and every step with
  a request budget fails with the reason. Without a request log there is no request check, except
  that a request budget fails "not counted: the run was given no request log". For an app that sends
  a request on a timer, `gate.periodic_requests` (new: a request as the log gives it, "METHOD
  /path", to a whole number of at least 1) sets requests of that kind aside up to their number in
  every step where a request limit or budget applies (a rest, `expect.requests_max`,
  `launch_requests_max`); what remains is held, a failure names what was set aside, a passing step
  gets a note naming it, and such a step whose `by_path` was cut to the 20 most frequent paths fails
  as unknown. `run.json`, `summary` and `compare` keep the raw counts; `gate.json`'s `limits` shows
  the map only when the scenario has it. On the
  iOS Simulator `expect.requests_max` is accepted on rests, taps and swipes. Notes for requests
  between steps (after an input step's window) and for a clock uncertainty above 0.1 s.
- **`gate.json`:** every run's `limits` now holds `rest_requests_max` (0 unless the scenario's gate
  block sets it). Runs captured before 0.7.0 hold no request counts and gate exactly as before
  otherwise (checked: the gate's text, exit status and JSON for 846 saved captures of 0.1.0 to
  0.6.0, 381 of them on a simulator, in 451 folders, equal under 0.6.0 as released and this version
  apart from that key).
- **Behaviour change, Android 11 and older:** re-analysing a capture from Android 11 or older with
  0.7.0 gives per-layer frames (`frames_layers`) and section coverage where older versions gave
  none. Nothing changes for Android 12 and later, nor for a capture that gives no API level: the new
  rules below apply only when the capture gives an API level below 31 and the trace holds no
  frame-timeline packet.
  - Sections: every step of a `--sections` run had failed `[sections]` with "the trace did not cover
    this step", because the covered span came from stamped packets only and an ftrace event bundle
    carries no packet timestamp. Without a frame timeline the span is now that of the ftrace events
    themselves (`read_atrace` reports it), widened only when the trace holds the ftrace data
    source's statistics (`FtraceStats`) exactly once for the start and once for the end of the trace
    (`read_trace` reports them): their per-CPU `now_ts` are then the bounds when every one is above 0
    and the latest start lies before the earliest end, or, in statistics without one, the session
    perfetto printed. Anything else leaves the events' own span.
    `run.json` gives `sections_span_s`, `sections_span_from`, per step `sections_covered`, and
    `sections_uncovered_steps`.
  - A trace with no frame-timeline packet (`read_trace` now counts them) reports the frame timeline
    as unavailable (`frametimeline` null, `frametimeline_unavailable`, `frames_compositor` and
    `trace_covered` null, no `compositor-trace` source) with a note naming the device's API level,
    never 0 frames.
  - Per-layer frames: `--timestats` there prints no `uid = ` line and the parser split the layers on
    it. A record now also starts at its `layerName = ` line, with an unknown uid, and such a layer is
    the app's only when its name starts with `<package>/` or `SurfaceView - <package>/`.

## 0.6.0 — 2026-11-11

A rehearsal on the iOS Simulator: lookups and typing in an app process that is thrown away, and
the positions found replayed by coordinate in the process that is measured.

- `ui-probe rehearse --simulator <udid> --scenario <file> --out <dir>` launches the app, then starts
  the input agent for a rehearsal (`TEST_RUNNER_UI_PROBE_AGENT_LOOKUPS=1`; the agent's first read of the app ties its later
  reads to the process running then, so the app comes first) and drives the scenario once: a tap with
  `find` is looked up by the Android rule (exactly one match on the screen without `nth`, the nth
  in the order of the tree with it, the same frame in two reads in a row, `find.timeout_ms`,
  `find.scroll`, the statuses `found`, `not_found`, `not_visible` and `unstable`), where a match
  counts only when part of it is on the display and XCTest's hit test says a tap would reach it,
  and the tap goes to the middle of the part on the display (moved into the part a scrolling area
  leaves when the middle is cut off); a `text` step
  types into the field with keyboard focus; rests wait their full `ms`, so a lookup reads the screen
  the run will tap; after the launch settle and after every step the app must be in front (state
  4), recorded per step. A simulator that is not booted or an app not installed is a refusal that
  leaves no folder. Nothing is recorded,
  measured or gated. A failed lookup writes `lookup-failed.txt` with the matches of the last read
  and the app's labelled elements (no element value when the scenario types). When the scenario
  types, the agent's log is rewritten without the typed characters and its result bundle and
  derived data are removed. However it ends, the app is terminated and its process must be
  gone within 5 s. On success `rehearsal.json` holds the scenario's digest, the udid, the screen
  size, the app's identity (version, build, the sha256 of its executable and a digest of every file
  in the bundle: sorted relative paths with each file's sha256, a link by its target text), the
  finish time and,
  per step, the match used, its centre and the swipes made; for a `text` step only the number of
  characters typed.
- `ui-probe run --simulator ... --rehearsal <file> [--rehearsal-max-age-s N]`: a simulator scenario
  may hold taps with `find`, and such a run needs a rehearsal. Before the first run the rehearsal
  must match the scenario (digest), the simulator, every tap with `find`, the installed app (an
  identity that cannot be read fails), and be at most N seconds old (default 3600); at the agent's
  first `ping` the screen size must equal the rehearsal's. Before a tap with `find` starts its
  recorder, the rehearsal's swipes for it are sent, each with its settle time; the tap goes to the
  rehearsed centre. `run.json` records it as `resolved`; a replayed swipe the agent did not
  perform fails the step (check `input`), and so does a replayed tap whose recording shows no
  visual change (signature `-`) unless its `expect.signature` is `-`; the gate adds one note per
  run that used a rehearsal. Only an approved `expect.signature` verifies where a replayed tap
  landed.
- A rehearsal folder is not a run: `rehearse` writes `rehearsal-started.json` into it before anything
  else; `gate`, `summary` and `compare` pass over such a folder (a failed or killed rehearsal
  included) instead of counting it as a run that did not complete, and `gate` prints one note for
  each one whose rehearsal did not complete. A run marker inside a rehearsal folder (other than
  its own `raw/` and `failed.json`) stops them with an error naming it. `run --out` refuses a
  folder holding a rehearsal or inside one, and `rehearse --out` one holding a run at any depth or
  a rehearsal, or inside another rehearsal.
- On the simulator a step's `settle_ms` and `find.scroll.settle_ms` are at most 60000, and the
  agent's idle limit covers every wait the host makes between two commands (replayed swipes with
  their settle times; in a rehearsal each lookup up to its timeout).
- `allow.unchanged_frames_max` (a whole number of at least 1, with the step's `reason`) on a rest,
  on both platforms: recorded frames that changed nothing fail only above it. Without it the check
  stays at zero, and `frames_max` or `changed_frames_max` does not reach it. Measured need: a
  focused text field on the iOS Simulator whose caret composed 15 to 19 such frames per 5 s rest.
- **Changed:** the excuse for the video encoder's own frames at rest on a simulator covers up to 6
  recorded frames, where it covered 2. In 523 still rest windows of a clean build, 52 held such
  frames: 32 one, 18 two and 2 three, and those two windows failed runs in which every count was
  zero. The other conditions are unchanged (every count known and zero over the recording, no
  pixel changed by more than 24 levels, at most 2000 pixels changed, none where the picture was
  flat). A simulator run of 0.5.0 that failed on exactly three to six such frames passes when gated
  again.
- The agent's mode is checked at its first `ping` in both commands (`"lookups"`): an agent in the
  other mode is a failed start, and the measured run's agent never inherits the switch from
  ui-probe's environment.
- Validation for the simulator has two modes. In both: `find` on a tap allowed (`text` or `desc`,
  `nth`, `timeout_ms`, `scroll`); `find.package` and `find_settle_ms` refused; `key` refused. A
  `find` value holding a control character is refused on the simulator in words of its own (a
  character outside the Basic Multilingual Plane, which only Android's window-tree dump cannot
  write, is allowed there). A
  `text` step is allowed in `rehearse` only, and its refusal in a run says to put the typing in a
  scenario of its own and drive it with `ui-probe rehearse`. `expect`, `allow` and rest lengths are
  checked alike in both, so that one file serves both commands.
- The agent (`ios-agent/`) serves `find`, `type` and `labels` only when started for a rehearsal,
  and reports its mode in `ping`. Its source changed, so it is built anew on first use.
- Android is unchanged.

## 0.5.0 — 2026-11-11

Input on the iOS Simulator: taps and swipes by fractions of the display, a signature per step.

- A scenario on a simulator may hold `tap` steps (`x`, `y`) and `swipe` steps (`from`, `to`, `ms`,
  `judge`) besides rests; each waits its `settle_ms` (as on Android) and may carry
  `expect.signature`, compared by the gate as on Android. `find` and `text` are refused (a lookup
  or typing reads the app's accessibility tree, which changed the app at rest when measured), and
  so is `key` (a simulator has no keys to press).
- The input goes through ui-probe's input agent (`ios-agent/`): one UI test with no host app and no
  app name, built once per agent source and Xcode version into
  `${XDG_CACHE_HOME:-$HOME/Library/Caches}/ui-probe/ios-agent/<key>/`, started with
  `xcodebuild test-without-building` for the run, and spoken to on a loopback port with a random
  token and a fresh nonce per command, whose keyed hash (HMAC-SHA256 with the token) every reply
  must carry. It taps and drags through the system app and reads nothing of the app under test but
  its state; it ends by itself after an idle limit. A command the agent reports it did not perform
  (an error, or an issue the test framework recorded while handling it) fails its step; a test run
  that ended with a status other than 0 after `quit` fails the run (check `agent`); the app not in
  front at the end of any step fails the foreground check (which a run with input now checks). A
  build counts only once it exited 0 (a marker, a temporary folder renamed into place, a lock per
  key); the test run's derived data and result bundle go under the run's `raw/` and are removed
  after a status of 0.
- On every way out of a run (normal end, error, interrupt, SIGTERM, SIGHUP), in order: each step's
  recorder stopped at the end of its step; the agent's group ended and its runner app terminated
  and uninstalled from the simulator; the status bar cleared. Each part runs whatever the one before
  it raised, every wait has a limit, and a signal during the clean-up is held until it is done.
  Rest-only scenarios start no agent and are unchanged. `doctor --simulator` reports the agent's
  source and whether it is built for this Xcode. `install.sh` copies `ios-agent/` beside the
  scripts.
- **0.4.0 was wrong** to say that a simulator keeps no exact frame count of the app a host can read.
  Its log store does: `log show --signpost` gives, for the app's process and with nothing added to
  the app, one event per frame its Metal layer drew (drawables), per layer-tree commit (commits) and
  per display update it was woken for (updates). A simulator run now reads its log store once,
  after the last step (`raw/signposts.ndjson`), and a second time, every process, from the end of
  the last step (`raw/signposts-tail.json`); each step gets `drawables`, `commits` and `updates`
  (unknown for every step, with a note, when the store did not answer, holds no drawable or commit
  of the app between its launch and the first step, or the second read found no event after the
  end of the last step). The run's gate header names both sources of frame counts when they are
  known. At rest, drawables plus commits are held
  against `rest_frames_max` or `allow.frames_max` (source "log store"), and updates against the new
  `gate.rest_updates_max` (default 0) or `allow.updates_max` (check `rest-updates`).
- Frames recorded at rest still fail as in 0.4.0, except when the counts are known and all 0 for
  the window and for the recording's own span, at most 2 frames were recorded, no pixel changed by
  more than 24 levels on any colour channel, no frame changed more than 2000 pixels by 8 levels or
  more, and none of those lies where the picture was flat: the encoder refining a still picture
  along its edges after motion (measured on 17 rest windows: at most 17 levels in 28 to 193 pixels,
  more on a screen with more text, none on a flat area; a growing 1 pt hairline changed 16 levels
  with 4 or 5 pixels on a flat area and is held), which is now a note. With the counts unknown,
  0.4.0's rule stands.
- `count_signposts` (simulator only) counts an app's own signposts per step; `expect.signposts_max`
  gates them on rests and input steps (check `signposts`), and fails as unknown, asking whether the
  app's tracing is switched on, when the run holds none of that subsystem; a name is matched by
  exact equality with the signpost's name or message, and one that occurs nowhere in the run is
  unknown and fails its limit. `summary` and `compare`
  add `sim_drawables`, `sim_commits`, `sim_updates` and `sim_signposts.<name>`.
- `ui-probe tls-front`: TLS in front of a plain local backend on 127.0.0.1, with a throwaway CA for a
  simulator (never for a phone's or a Mac's own trust store), so that an app reaches the backend over
  HTTPS without loosening its transport security; `--ca` prints the CA's path for
  `xcrun simctl keychain <udid> add-root-cert`. The CA has pathlen:0 and may name only localhost,
  and its key is deleted once the certificate is signed; a state folder that is not empty and not
  marked as the tool's is refused, and nothing in a folder the tool did not make is changed.
- The tests are sealed: stand-ins for `xcrun`, `xcodebuild` and `ps` first on PATH (checked before
  anything runs), and `XDG_CACHE_HOME` and `HOME` in a temporary folder.
- `launch_env`: names to strings handed to the app on a simulator, set as `SIMCTL_CHILD_<NAME>` on
  the `xcrun simctl launch` call only, the only source (`SIMCTL_CHILD_*` in ui-probe's own
  environment are not passed, and named in a note); saved in `capture.json`, so not a place for
  secrets; refused on Android. A swipe on a simulator lasts at most 60000 ms.

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
