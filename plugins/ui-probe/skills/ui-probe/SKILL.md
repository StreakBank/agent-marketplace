---
name: ui-probe
description: Check that a mobile app is smooth and efficient by watching it from outside while a scripted scenario runs. Use when asked whether a screen does work while idle (frames, CPU or network requests at rest), how many requests a step sends, whether a tap flickers, flashes a skeleton or replays an animation, to see frame by frame what a step looked like, to gate a change on those counts, to compare two builds' timings honestly, or to run an app under a slow network. Android over adb, and the iOS Simulator for rests, taps and swipes (lookups and typing there in a rehearsal); needs no changes to the app.
allowed-tools:
  - Bash
  - Read
---

# ui-probe — watch an app from outside

The mechanics are in the `ui-probe` CLI (this plugin's `scripts/`, or the `ui-probe` launcher after
`scripts/install.sh --apply`). This skill is the sequence and the rules for reading results.
Project facts (which device, which package, where scenarios live, whether the gate blocks) belong
to the consuming project's shim, not here.

## Before anything

```sh
ui-probe doctor --serial <serial>
```

Stop on a missing required item (exit 2), including an ffmpeg older than 5.1. Never substitute a
different device or build for the one asked for.

## The rules that decide everything

1. **Counts are exact, gate them.** Frames and CPU while the screen is at rest; frames for a step
   that waits on nothing; trace sections per step; requests per step, counted by a front.
2. **Sequences are structural, gate them against an approved signature.**
3. **Durations are statistical. Never gate a duration, and never quote one from an emulator or
   simulator as if it were a phone's.** Report the spread; use `compare` for base against head.
4. **A count that waits on time or on the network is not exact.** Frames in a fling, in a
   cross-fade, or in a skeleton that animates until data arrives depend on how long it lasted.
   Only rest windows and steps that wait on nothing have counts worth a budget.
5. **Counts depend on content.** A list too short to scroll draws no frames. Pin the data a
   scenario sees before approving a baseline.

## Running

```sh
ui-probe run --serial <serial> --scenario <file> --out <fresh-dir> --runs <n> [--trace] [--sections]
```

- Use a fresh `--out` every batch. The tool refuses to overwrite a capture.
- `--trace` adds the compositor's frame timeline (a third frame count and its jank classes).
  It is not needed for the gate: frames are already counted from the platform's records and from
  the compositor's per-layer statistics, whatever toolkit drew them.
- `--sections` adds trace-section counts for the names in the scenario's `count_sections`. A
  name the app never emitted anywhere in the trace is "not observed" (its counts are unknown, and
  a `sections_max` on it fails): the build or the platform may not emit it.
- A trace that lost data (its own statistics say so) gives no counts at all: the gate notes it
  and the other frame counts decide.
- One device, one batch at a time. A second scenario on the same device corrupts both.
- Run one run and gate it before a batch. A scenario mistake (a tap that misses, a dialog on
  first launch, a rest that ends with another app in front) shows in a single run.

Write scenarios with `until_quiet` on. Put a `rest` step after launch and after every
interaction whose aftermath matters. A rest is expected with the app in front; mark a rest that
measures the app behind another one `"background": true`. Coordinates are fractions of the screen. The scenario is
checked strictly: an unknown key, a key that does not apply to the step's kind or a value of the
wrong type stops the run before anything touches the device.

Step kinds are `rest`, `tap`, `swipe`, `key` and `text` (types into the focused field). Prefer
`"find": {"text": ...}` or `{"desc": ...}` on a tap to hard-coded coordinates: the target is
looked up on the device before the step (an element of the app's own package whose bounds held
still over two reads, and on the screen), and the lookup is charged to no step. Under a smaller
screen or a larger font a target can sit below the fold: give its `find` a `find.scroll` (a swipe,
repeated up to `max` times) rather than new coordinates; a lookup that ends `not_visible` says the
element was in the tree but off the screen. A backend on the host behind `ui-probe proxy` needs
`--map-host 10.0.2.2=127.0.0.1` on an emulator. A screen that is
never still by design (a blinking caret) gets an `allow` on its rest step, with a `reason`, and
every step that ends on it a fixed wait (`"until_quiet": false`), since such a screen never goes
quiet. Recorded frames that changed nothing take `allow.unchanged_frames_max` only for a screen
whose own caret composes them (measured on one framework's caret on the iOS Simulator); they are
what an invisible animation leaves, so never allow them to hide one. The README's "Screens that are never still" has the
measured numbers.

On the iOS Simulator (`run --simulator <udid>`) a scenario holds rests of at least 1000 ms, taps
(`x`, `y`) and swipes, the input sent through ui-probe's input agent (built once with `xcodebuild`,
installed on the simulator for the run and removed after). A measured run never looks up and never
types (an accessibility read, or a field that has had focus, changes the app for the rest of its
life). A tap may still name its target with `find`: first run `ui-probe rehearse --simulator <udid>
--scenario <file> --out <dir>`, which drives the scenario once in an app process it then ends,
looking elements up and typing there, then `ui-probe run --simulator <udid> ... --rehearsal
<dir>/rehearsal.json`, which replays the positions found. The run refuses a rehearsal of another
scenario (its digest), simulator, or app (the two versions in `Info.plist`, the executable's sha256
and a digest of every file in the bundle), or one older than `--rehearsal-max-age-s` (3600). Typing, such
as a sign-in, goes in a scenario of its own that only `rehearse` drives. A rehearsal's folder is not
a run: `gate` passes over it and notes one that did not complete; never put runs inside one. A lookup's match counts only
when XCTest's hit test says a tap would reach it; a target near a bar is safest with a
`find.scroll`. Give every replayed tap with a stable transition an `expect.signature`: without
one the gate catches only a replayed tap that changed nothing, not one that landed on another
control. A rehearsal waits every rest in full and requires the app in front after each step. A
rehearsal that types keeps no typed character in its files. Positions hold only while
the screen is the same in both passes: keep the backend's content fixed between them. The run
freezes the status bar, and every frame recorded at rest after the first fails, whatever it
changed: a screen that is never still by design needs `allow.frames_max` and
`allow.changed_frames_max` on its rest. A tap or swipe may carry `expect.signature`. `launch_env`
passes variables to the app (not secrets: they are saved with the run). `zoom` needs `--step <id>`.
A simulator run reads its log store: each step gets the app's `drawables`, `commits` and display
`updates`, gated at rest (`rest_updates_max`, `allow.updates_max`); they are known only when a
second read shows the store was read to the end. With them known and 0 over the whole recording, up
to 2 recorded frames that changed no pixel by more than 24 levels, at most 2000 pixels, and none on
a flat area, are the encoder, not the app. `count_signposts` counts the app's own signposts (names
matched exactly; a name that occurs nowhere in the run is unknown and fails its limit). An agent
whose test run ended with an issue fails the run (check `agent`). `ui-probe tls-front` puts TLS with a throwaway CA
(for a simulator only) in front of a plain local backend.

## Counting requests

The requests the app sends are counted by a front its traffic passes through, never inside the
app: `ui-probe http-front --port <listen> --to-port <backend> --log <file>` for an Android emulator
(the app talks plain HTTP to `http://10.0.2.2:<listen>`), or `ui-probe tls-front ... --log <file>`
for the iOS Simulator. Start the front as its own process and wait for its `listening on` line, then
`ui-probe run ... --requests <file>`: each step gets `requests` (a count and the 20 most frequent
`METHOD /path`), and the gate fails a rest that sent any (`gate.rest_requests_max`, default 0;
`allow.requests_max` with a `reason` for a screen that polls by design), a step over its
`expect.requests_max`, and a launch over the scenario's `launch_requests_max` (the cold start's
budget). For one known request the app sends on a timer at any moment, name it in the gate block,
`"periodic_requests": {"GET /path/shape": 1}`: it is set aside up to that number in every step where
a limit or budget applies, and nothing else is; never give a budget a request of slack for it. A
rest's window runs on to the next step's start (nothing is put into the
app in between). A count is known only when the run shows the front saw everything; otherwise it is
unknown, never 0, and fails wherever a limit applies: no request between the launch and the end of
the last step (the app is probably not pointed at the front), the front not running at the start or
the end, restarted during the run, refused by the backend, or carrying a connection it could not
read (an upgrade or a WebSocket, HTTP/2, anything not HTTP/1.x, even one opened before the run), or
on Android a device clock that could not be mapped (or only to within more than 0.2 s). A front that
is told to stop, restarts, or no longer accepts during or after the run makes its counts unknown;
stop a front before starting another on the same log (it refuses a log another front writes). The
log holds methods and the shape of each path only (a segment made only of lower-case letters, dots,
hyphens and underscores stays, so a user name such as `alice` stays; any other is `:id`; no query
string, header value or body), in a 0600 file. HTTP/1.x only; a streaming response is one request. Keep the front for the app under test
alone during a run: it cannot tell clients apart.

## Reading a run

1. `ui-probe gate <dir>` first. Exit 1 means a violation; exit 2 means the tool could not do its
   job (read the message), never a pass. A `FAIL` line names the check, the step, the number and,
   for frame counts, which of the three counts decided. A `note` line names a witness that was
   unavailable while another one decided: read the notes too. A `FAIL [run]` line is a run that
   did not complete (its folder holds `failed.json` with the error); it is never left out.
2. For any step in question, **read its contact sheet** (`<run>/sheets/<step>.png`) with the Read
   tool. Each frame is labelled `#index +ms-since-step-start gap`, then the platform's render time,
   then `[identical]` when the frame changed nothing visible or `[bars only]` when only the system
   bars changed. Describe what you see in order; do not infer it from the numbers.
3. A change too small for a thumbnail: `ui-probe zoom <run> <a> <b>` and read the image.
4. `ui-probe summary <dir>` for the spread across runs before saying anything about time.

What the at-rest checks mean when they fail:

| Violation | Likely meaning |
|---|---|
| frames at rest, with identical recorded frames | something animates where it cannot be seen (zero alpha, off screen, behind another layer) |
| the app's area changed at rest | a visible animation or content that never settles |
| a note that recorded frames changed nothing or changed the app's area while the app presented no frame | another layer (the status bar made the display compose) or encoder noise; not a violation, `zoom` to see it. With any count above 0 they stay violations |
| CPU at rest without frames | background work on a timer or a busy loop |
| requests at rest | a poll, a refresh on a timer, or a retry loop against a backend that answers: the message names the paths, and how many came in the gap before the next step. Unknown counts say why (often: the app is not pointed at the front; a backend that refused the front's connection makes them unknown, so a retry loop against a backend that is down shows as unknown, not as a count) |
| never went quiet, "the app kept drawing" | loading that does not finish, or an animation with no end |
| never went quiet, "the display kept changing while the app was idle" | something outside the app's own frame count: a system animation, the keyboard, another window, or a surface the app draws outside its views. Read the sheet before blaming the app |
| another app was in front | the rest window did not show the app's screen: an earlier tap left the app, a system dialog covers it, or a back press closed it. The zero it counted says nothing. Read the sheet of the step before, fix the scenario or the app, and run again |
| no frame of the app was counted while it launched | the app never drew, or it draws in a way none of the available counts can see. Do not trust the rest windows of that run |
| the recorder had stopped before the scenario ended | later steps were not recorded: the recording checks for them did not run |
| not analysed | the capture has no current `run.json`: run `ui-probe analyze <dir>` and gate again |
| the recording could not be aligned | the recorder's own timestamps were missing or the platform's frame rows did not line up with the step marks: the recording cannot say what happened in which step, so a signature cannot be checked |

A signature that differs from the approved one is read on the sheet: `TD` where `T` was approved
means an intermediate state appeared (a skeleton over content that was already there, a flash);
an extra `E` means the screen left its state and came back (a replayed animation, a flicker).

## Comparing two builds

Interleave base and head runs on the same device, at least five each (five against five is the
least at which `compare` can say `different` at its default alpha), then:

```sh
ui-probe compare --base <base-dir> --head <head-dir>
```

Report `different` and `same` as such. Report `unknown` as unknown: it is not a pass.

## Conditions

- Record the device profile with every claim; `capture.json` holds it (memory, cores, refresh
  rate, build type, compile state, proxy and network settings).
- A slow network: `ui-probe proxy --delay-ms <n> --kbit <n> --log <file>` and point the device's
  system proxy at it. The proxy listens on the host's loopback address: an emulator reaches it at
  `10.0.2.2`; a physical device needs `adb reverse tcp:<port> tcp:<port>` (the proxy is then the
  device's own `127.0.0.1:<port>`) or the proxy started with `--bind <an address the device
  reaches>`. Start it as its own process, wait for its `listening on` line and stop it by its
  process id; exit 2 at start means another proxy holds the port. A tunnel the app keeps open
  is logged only when it ends or when the proxy stops, so stop the proxy first and then confirm
  its log names the app's hosts; if it does not, the app bypassed the proxy and the run measured
  nothing.
- An emulator gives conditions (memory, cores, a slow network through the proxy), not speed.

## What not to do

- Do not approve a signature or a frame budget from a single run.
- Do not turn a timing into a threshold because it looked stable for an hour.
- Do not explain away a rest-window failure as noise. A clean app draws zero frames at rest. CPU
  alone is the one exception: a single window a few ticks over the limit, with no frame, is run
  again first, because background threads do that now and then; a fault repeats in every window.
- Do not delete or rewrite a run directory; analysis can be redone from it (`analyze`, or
  `sheet` for one run, redoes the whole analysis from the raw files).

## Improving this skill

A fact that is true for any project using the tool (a platform behaviour, a flag, a failure mode)
belongs in this plugin: edit the skill or the scripts, bump the version and the changelog, run
the marketplace's coupling check. A fact about one project stays in that project's shim.
