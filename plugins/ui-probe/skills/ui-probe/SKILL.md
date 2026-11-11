---
name: ui-probe
description: Check that a mobile app is smooth and efficient by watching it from outside while a scripted scenario runs. Use when asked whether a screen does work while idle (frames or CPU at rest), whether a tap flickers, flashes a skeleton or replays an animation, to see frame by frame what a step looked like, to gate a change on those counts, to compare two builds' timings honestly, or to run an app under a slow network. Android over adb, and the iOS Simulator for rests, taps and swipes; needs no changes to the app.
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
   that waits on nothing; trace sections per step.
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
quiet; never an allowance for recorded frames that changed nothing (there is none). The README's "Screens that are never still" has the
measured numbers.

On the iOS Simulator (`run --simulator <udid>`) a scenario holds rests of at least 1000 ms, taps
(`x`, `y`) and swipes, the input sent through ui-probe's input agent (built once with `xcodebuild`,
installed on the simulator for the run and removed after). There are no lookups and no typing
there: an accessibility read can change the app it reads, so give positions as fractions. The run
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
