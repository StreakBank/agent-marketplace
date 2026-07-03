---
name: android-device
description: Control Android emulators and devices, and search Android/Compose documentation offline, via the Android agent CLI. Use when asked to start/stop/list Android emulators (AVDs), inspect a running Android app's UI layout, diff UI changes between interactions, take or annotate device screenshots, resolve on-screen elements to tap coordinates, install and launch an APK, or look up official Android / Jetpack / Compose Multiplatform documentation.
allowed-tools:
  - Bash
  - Read
---

# android-device — emulator, UI inspection, and docs via the Android agent CLI

Thin wrapper over Google's [Android agent CLI](https://developer.android.com/tools/agents/android-cli),
scoped to its proven wins: emulator lifecycle, context-frugal UI inspection,
vision-fallback screenshots, one-command APK deploy, and an offline docs
knowledge base. Everything here is deterministic tooling — JSON and PNG out.

## Binary resolution and setup

Resolve the `android` binary in this order; use the first that exists:

1. `android` on PATH
2. `~/.local/bin/android`
3. `~/.android/bin/android-cli` (payload from a previous bootstrap)

If none exists, run the pinned installer bundled with this plugin:

```bash
sh "$CLAUDE_PLUGIN_ROOT/scripts/install-android-cli.sh"
```

It downloads launcher version **1.0.15498356** (the version this skill was
validated against) from Google's version-pinned URL, verifies its SHA-256,
installs to `~/.local/bin/android`, opts out of telemetry, and bootstraps the
payload. **Never run `android update` on your own** — version bumps are a
human decision followed by re-validation. If the installed version differs
from the pin, note it to the user and proceed; do not "fix" it.

## Telemetry

Telemetry is ON by default. `--no-metrics` is a **root-level flag — it must
come before the subcommand**: `android --no-metrics emulator list`
(after the subcommand it errors). The installer writes `--no-metrics` into
`~/.androidrc`, which applies it to every invocation; if that file is absent,
pass the flag explicitly on every call.

## Emulator lifecycle

```bash
android emulator list                 # AVD names, one per line
android emulator start <avd-name>    # BLOCKS until fully booted; prints the serial (e.g. emulator-5556)
android emulator stop <avd-name>     # clean shutdown
```

`start` returning means the device is genuinely ready — no boot-polling loop
needed. Warm start is typically ~15 s; cold/first boot can take minutes.
Capture the printed serial: `layout` and `run` accept `--device <serial>` when
more than one device is attached, but the `screen` subcommands do **not** —
they target the sole/default device (set `ANDROID_SERIAL` if you must
disambiguate). `emulator create` / `remove` exist but are out of this skill's
scope — creating AVDs with specific API/arch/config needs is a deliberate,
per-project decision.

## UI inspection — prefer `layout`, prefer `--diff`

```bash
android layout [--device <serial>] [-o <path>.json]        # flat JSON list of on-screen elements
android layout --diff [--device <serial>] [-o <path>.json] # ONLY changes since the last layout call
```

`--diff` output is grouped — `{"added": [...], "modified": [...], ...}` — not
a flat list; a tab switch typically diffs to a handful of entries (including
the nav item gaining `selected`) instead of re-dumping the whole tree.

Element properties: `text`, `resourceId`, `contentDesc`,
`interactions` (`clickable` / `scrollable` / `checkable` / `focusable` /
`long-clickable` / `password`), `state` (`checked` / `focused` / `selected`),
`bounds` (`[x1,y1][x2,y2]`), `center` (`[x,y]`), `off-screen` (in hierarchy
but not visible — scroll to reach).

Use `layout` as the primary way to examine an app; after each interaction use
`layout --diff` to see just what changed — it keeps context small. `layout`
can fail while the app shows a WebView or an animation; fall back to an
annotated screenshot (below), and expect it to recover after navigating away.

## Screenshots and visual targeting

```bash
android screen capture -o <path>.png              # plain screenshot
android screen capture --annotate -o <path>.png   # numbered boxes on UI elements
android screen resolve --screenshot <path>.png --string "#7"   # label -> "x y" coordinates
```

(Note: `resolve`'s flag is `--screenshot`; Google's own upstream skill doc
says `--screen`, which this CLI version rejects.)

Write PNGs to your session scratch/temp directory, never into the project
tree. **Always visually Read the PNG before acting on it.** Annotation is
word-granular and can be noisy (~dozens of labels); it exists for what
`layout` can't see — WebViews, images, apps without resource ids. Combine
resolve with input in one line:

```bash
adb shell input tap $(android screen resolve --screenshot s.png --string "#7")
```

`resolve` also substitutes into arbitrary strings: `--string "tap #7"` returns
`tap 929 30`.

## Input (via adb, guided by layout)

- Tap an element at its `center`: `adb shell input tap <x> <y>` — only if its
  `interactions` include `clickable`.
- Before typing into a field, confirm its `state` includes `focused`.
- Scroll `scrollable` elements with `adb shell input swipe <x1> <y1> <x2> <y2> <ms>`;
  scroll slowly (500 ms+) — fast flings overshoot.
- Content loads asynchronously: if an expected element is missing after an
  action, wait a couple of seconds and run `layout --diff` again.

## Deploy and run an APK

```bash
android run --apks <path/to/app.apk> [--device <serial>] [--activity <name>]
```

Installs and launches in one command; auto-selects the main activity when
`--activity` is omitted. Incremental reinstall of an already-present app is
~1 s.

## Documentation knowledge base

```bash
android docs search <keywords...>   # ranked hits from the local KB
android docs fetch <doc-id-or-url>  # full markdown, offline
```

The KB indexes developer.android.com AND the JetBrains Compose Multiplatform
docs (~5k docs). First use downloads ~37 MB; warm queries are sub-second.
Prefer it over web search for Android/Compose API questions — it's local,
fast, and versioned.

## Cautions and out of scope

- **Do not run `android describe`** — it injects a `.gradle/init.gradle.kts`
  into the target project and triggers a cold Gradle sync (minutes); its
  output is thinner than a well-maintained project context file.
- **Journeys are out of scope for this skill.** They are agent-executed,
  model-judged natural-language tests — fine as an ad-hoc smoke protocol, but
  non-deterministic and Android-only. If your project has a deterministic
  instrumentation/e2e harness, journeys do not replace it and must not become
  a regression gate.
- `create`, `sdk`, `skills`, `studio`, and `update` subcommands are out of
  scope here — project scaffolding, SDK management, and IDE bridges are
  deliberate per-project decisions, not device-control tasks.
- A long-running emulator can lose DNS (host reachability fine, in-emulator
  lookups fail); if the app under test can't reach its backend, restart the
  emulator before debugging the app.
- Projects typically carry their own policy for which device/AVD to use and
  how this tool divides labor with other device tooling — check the project's
  rules before assuming defaults.
