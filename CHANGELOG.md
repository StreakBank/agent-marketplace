# Changelog

## 0.3.0 — 2026-07-06

### New plugin — migration-harness 0.1.0
- **`plugins/migration-harness/`** — staged, ledger-tracked codebase migrations.
  Extracted (not invented) from six real migrations on a production multi-module
  codebase: the stage spine (discover → classify → batch-escalate → transform →
  verify-each → gate → record), the mechanical vs semantic-escalate discriminator
  ("can the completion gate tell right from wrong?", default-escalate), and the
  observed failure-mode catalog (`references/SPINE.md`).
- **`scripts/migrate-ledger.mjs`** — zero-dep Node ≥ 18.3 ledger CLI
  (`init|import|add-site|classify|decide|assign|check-partition|add-verify|verify|outcome|gate|status|hook`).
  The `gate` is a mechanical block-until-signed-off: exit 1 while any
  semantic-escalate site lacks a recorded human decision, any outcome is pending, or
  any done site lacks passing tool-recorded verifies (`--reverify` re-derives them
  from real runs). Hardened by a pre-commit 35-agent adversarial review (23 findings
  fixed): verify evidence carries a working-tree fingerprint and reads STALE at the
  gate after later source changes; `grep-absent` fails on a scope matching no files
  (no vacuous pass) and sees untracked files; re-classification clears a stale
  decision and semantic→mechanical downgrades need `--force --reason`; template
  placeholders are shell-quoted (space-safe, injection-inert); the commit hook fails
  CLOSED on malformed ledgers and honors `git -C`. `hook` mode blocks `git commit`
  as an optional PreToolUse hook. Per-agent file-set partitioning is checked for
  pairwise disjointness. 15 fixture tests (`node --test`).
- **`references/`** — SPINE.md (stage spine + variance points + failure modes),
  RECIPE-CONTRACT.md (the slots a stack-specific recipe must fill),
  ESCALATION-PACKET.md (concrete-diff / default-escalate / batch decision-round
  format). Recipes themselves stay with stack-specific catalogs.
- **CI** — admission gate now also runs `node --test` over any `plugins/**/*.test.mjs`.
- **Validated by real use** (same day): a 16-site production migration ran the full
  ledger lifecycle end-to-end; the mechanical per-site verify caught 2 test
  regressions transform agents self-reported as clean. Queued for 0.1.1:
  `check-changes` (diff VCS status against the declared agent file sets — the one
  gap the run had to cover manually).

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
