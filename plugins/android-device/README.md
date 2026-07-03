# android-device

Android emulator + device control and offline Android/Compose docs search for Claude
Code, wrapping Google's [Android agent CLI](https://developer.android.com/tools/agents/android-cli)
— pinned, checksum-verified, telemetry opted out.

## What you get

- **Emulator lifecycle without the boot-poll dance** — `android emulator start <avd>`
  blocks until the device is fully booted and prints the serial; `stop` shuts down
  cleanly.
- **Context-frugal UI inspection** — `android layout` dumps the on-screen elements as
  flat JSON with interaction affordances (`clickable`, `scrollable`, …) and state
  (`focused`, `selected`); `layout --diff` returns only what changed since the last
  call, so an agent loop reads a handful of elements instead of re-dumping the tree.
- **Vision fallback** — annotated screenshots (`screen capture --annotate`) plus
  `screen resolve` to turn a numbered label into tap coordinates, for WebViews and
  anything the layout dump can't see.
- **One-command deploy** — `android run --apks <apk>` installs and launches.
- **Offline docs** — a local knowledge base indexing developer.android.com and the
  JetBrains Compose Multiplatform docs (~5k docs, sub-second warm queries).

## Install

```bash
claude plugin install android-device@agent-marketplace
```

The skill installs the CLI on first use if it's missing, via the bundled pinned
installer (launcher `1.0.15498356`, SHA-256-verified per platform, installed to
`~/.local/bin/android`, `--no-metrics` written to `~/.androidrc`). To install
manually:

```bash
sh plugins/android-device/scripts/install-android-cli.sh          # opts out of telemetry
sh plugins/android-device/scripts/install-android-cli.sh --allow-metrics
```

Requires an existing Android SDK for device work (the CLI auto-detects it; it can
also install one via `android sdk`, which this skill leaves to you).

## Project shim

This plugin knows *how* to drive Android devices; it deliberately doesn't know *your*
project. Add a short rule to your project's `.claude/rules/` covering: which AVD is
your canonical test device, where this tool's ownership ends if you run other device
tooling (e.g. an MCP server that owns iOS or crash logs), and your policy on
model-judged tests (journeys are out of scope here and shouldn't silently become a QA
gate). Keep it thin — state facts and policy, don't restate this skill's mechanics.

## Scope boundaries

Journeys, `android describe` (injects a Gradle init script + cold sync), project
creation, SDK management, Android Studio bridges, and CLI self-update are out of
scope — see `skills/android-device/SKILL.md` § "Cautions and out of scope" for why.

## Provenance

See [PROVENANCE.md](PROVENANCE.md): pinned launcher URLs + SHA-256 per platform, the
consulted upstream (`android/skills` @ `07302ca`, Apache-2.0, not vendored), and the
empirical validation basis.
