# Copy this into your project as .claude/rules/android-device.md and fill it in.
# Delete these comment lines. Keep it thin — facts and policy only, never restate the
# plugin skill's mechanics (those live in the plugin and would drift if duplicated).
---
description: <project> shim for the android-device plugin skill — canonical device, tool-ownership boundaries, journeys policy. Mechanics live in the plugin; this rule states project facts and policy only.
paths:
  - "<glob for the app source, e.g. app/**  or  mobile/**>"
---

# android-device — <project> shim

The generic core is the `android-device` plugin skill (agent-marketplace); it owns
the mechanics. This rule adds only what the plugin deliberately doesn't know.

## Device facts

- **Canonical test AVD: `<avd-name>`** (<api / arch / geometry, and any create-time
  config that matters>). Boot with `android emulator start <avd-name>`.
- **Identify devices by AVD name, never by serial** — serials are boot-order-dependent.
  Confirm with `adb -s <serial> emu avd name`.
- <Build/deploy facts ONLY if not already in your CLAUDE.md: debug package id + APK path.>

## Ownership boundaries (one owner per niche)

- **android-device (plugin)** owns: <the niches you use it for — emulator lifecycle,
  Android UI inspection, screenshots, APK deploy, docs lookup>.
- **<other tool>** remains owner for: <e.g. iOS, crash reports, typing — whatever an
  MCP server or another tool owns in this project>.

## Policy

- **Journeys are <never a QA layer here / allowed as ad-hoc smoke only>** — model-judged,
  Android-only, no seeded-backend control. State the ruling so it can't silently become
  a regression gate.
- <Any project-specific policy: telemetry, which builds are safe to install, etc.>
