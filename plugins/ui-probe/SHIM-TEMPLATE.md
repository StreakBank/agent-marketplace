# Copy this into your project as .claude/rules/ui-probe.md and fill it in.
# Delete these comment lines. Keep it thin: facts and policy only. The mechanics live in the
# plugin and would drift if restated here.
---
description: <project> shim for the ui-probe plugin — which device and build, where scenarios live, the reference profile, and what the gate may block. Mechanics live in the plugin.
paths:
  - "<glob for the scenario directory>"
  - "<glob for the app's UI source, if a UI change should load this rule>"
---

# ui-probe — <project> shim

The generic core is the `ui-probe` plugin; it owns the mechanics and the rules for reading
results. This rule adds only what the plugin deliberately does not know.

## Subject

- **App under test:** `<applicationId>`, launch component `<activity or blank>`.
- **Build measured:** <which variant for counts; which for anything timed or for memory>.
- **Scenarios live in:** `<directory>`. One file per <screen / tab / flow>.
- **Runs are kept in:** `<directory outside version control>`.
- **Links the scenarios open (`url` steps), if any:** <the app's scheme; which screen each link reaches and what it needs first (signed in, seeded data); made-up ids only>.

## Device and reference profile

- **Device:** `<avd or device name>` (<api, resolution, memory, cores, refresh rate>).
- **iOS Simulator, if used:** `<simulator name and runtime>`, identified by name and
  resolved to its udid at run time; which scenarios run there (rests, taps and swipes) and with what
  limits; any `launch_env` the app needs there (no secrets: it is saved with each run).
  Identify it by name, never by serial.
- **Rehearsals on the simulator, if used:** <which scenarios are rehearsed (`ui-probe rehearse`) before
  they run, which scenario only types (a sign-in) and when it runs, where rehearsal folders are kept
  (apart from the run folders the gate reads), the `--rehearsal-max-age-s` used, and how the
  backend's content is held fixed between a rehearsal and its runs>.
- **Reference profile:** <the settings every baseline assumes: network path, proxy or none,
  font scale, animation scales, compile state>.
- **Other profiles run on the gate:** <low memory, slow network with its numbers, large font>.
- **How each profile is imposed and undone:** <e.g. `wm size` override for a small screen, font scale,
  `ui-probe proxy` with `--map-host` for a backend the emulator reaches as 10.0.2.2>, and which scenarios
  carry `find.scroll` for targets that move below the fold.
- **Content:** <how the data a scenario sees is pinned: seed, account, fixture backend>.
- **Request counts, if used:** <which front (`ui-probe http-front` on Android, `tls-front` on the
  simulator), its ports and log file, how the app is pointed at it, and which scenarios run with
  `--requests`>.

## Gate policy

- **Where it runs:** <before a landing / in CI / on demand>.
- **Blocking or advisory:** <which checks block; which only report>.
- **Limits that differ from the defaults:** <e.g. rest CPU budget, with the reason>.
- **Rest steps with an `allow`:** <which screens are never still by design, and who approves an allowance>.
- **Request budgets:** <`rest_requests_max` if not 0, each scenario's `launch_requests_max` (the
  cold start), which steps carry `expect.requests_max`, the request an app that polls by design
  sends on a timer (`gate.periodic_requests`: name it, as the log gives it), which rests poll by design
  (`allow.requests_max`), and who approves a change>.
- **Approved baselines:** <where `expect` values are reviewed and who approves a change>.
- **Timings:** <never gated here / gated only on real devices through `compare`>.

## Ownership boundaries

- **ui-probe** owns: <frames and CPU at rest, step signatures, motion evidence>.
- **<other tool>** remains owner for: <settled-state pixels, journeys, device lifecycle>.
