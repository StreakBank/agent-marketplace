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

## Device and reference profile

- **Device:** `<avd or device name>` (<api, resolution, memory, cores, refresh rate>).
  Identify it by name, never by serial.
- **Reference profile:** <the settings every baseline assumes: network path, proxy or none,
  font scale, animation scales, compile state>.
- **Other profiles run on the gate:** <low memory, slow network with its numbers, large font>.
- **Content:** <how the data a scenario sees is pinned: seed, account, fixture backend>.

## Gate policy

- **Where it runs:** <before a landing / in CI / on demand>.
- **Blocking or advisory:** <which checks block; which only report>.
- **Limits that differ from the defaults:** <e.g. rest CPU budget, with the reason>.
- **Rest steps with an `allow`:** <which screens are never still by design, and who approves an allowance>.
- **Approved baselines:** <where `expect` values are reviewed and who approves a change>.
- **Timings:** <never gated here / gated only on real devices through `compare`>.

## Ownership boundaries

- **ui-probe** owns: <frames and CPU at rest, step signatures, motion evidence>.
- **<other tool>** remains owner for: <settled-state pixels, journeys, device lifecycle>.
