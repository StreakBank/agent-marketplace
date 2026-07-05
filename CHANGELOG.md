# Changelog

## 0.2.1 — 2026-07-04

### Fixed — coupling admission gate
- **`scripts/check-coupling.sh`** — the project-name grep is now case-INSENSITIVE
  (`grep -IniE`), so a mixed-case brand name in prose (`StreakBank`, `LadderPicks`) is
  caught, not just the lowercase spelling. Attribution/schema URLs are exempted by
  blanking only the URL TOKEN (any `scheme://` URL + bare `github.com` /
  `raw.githubusercontent.com` hosts, case-folded) before the grep — so a plugin's own
  repo/schema URL may name the org while coupling PROSE sharing that line is still
  caught, and a capitalized `GitHub.com` URL is not spuriously flagged. (Same fix as
  cmp-marketplace 2.12.0; surfaced by the cmp-arch-gates adversarial audit.)

## 0.2.0 — 2026-07-03

Hardening from the adversarial reuse-path audit (same day).

- **Compounding loop closed** — CONTRIBUTING §8 "Feeding learnings back" + a plugin
  "Improving this skill" section define the return edge: generic tool discoveries
  upstream to the owning plugin (writable clone via `gh repo clone`), project facts
  stay in the shim. The discriminator is the core/shim test.
- **Mechanical admission gate** — `scripts/check-coupling.sh` is now the single owner
  of the coupling grep (docs point to it instead of re-inlining); `.github/workflows/ci.yml`
  runs it + shellcheck + JSON-manifest validation on push/PR.
- **android-device 0.2.0** — installer now identity-checks the resolved `android`
  (rejects the deprecated SDK `android` tool that would otherwise shadow it), records
  launcher version + payload bundle id in the receipt, and warns on a PATH conflict;
  PROVENANCE documents pin scope honestly (only the launcher is checksummed; payload +
  docs KB float) with a revalidation cadence; `SHIM-TEMPLATE.md` ships for consumers
  to copy; README carries fresh-machine bootstrap (user anchor + https-clone fallback).

## 0.1.0 — 2026-07-03

- Marketplace scaffolded with the core/shim reusability discipline
  (CONTRIBUTING.md is the admission gate).
- **android-device 0.1.0** — first plugin: Android emulator + device control and
  offline Android/Compose docs search wrapping the Android agent CLI, pinned at
  launcher `1.0.15498356` (SHA-256-verified installer for darwin_arm64,
  darwin_x86_64, linux_x86_64; telemetry opted out via `~/.androidrc`). Scope:
  emulator lifecycle, `layout`/`layout --diff`, `screen capture [--annotate]` +
  `resolve`, `run --apks`, `docs search/fetch`. Journeys, `describe`, `create`,
  `sdk`, `studio`, and `update` are explicitly out of scope.
