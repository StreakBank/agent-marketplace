# Changelog

## 0.4.0 — 2026-08-26

### New plugin — audit-fleet 0.1.0
- **`plugins/audit-fleet/`** — adversarial verification for an audit/measurement
  fan-out. Ships one dynamic workflow, `lens-triple-audit`: per lane, one **finder**
  (mandatory coverage manifest — `files_examined: 0` is a failure, not a result) → two
  independent, mutually blind **default-REFUTE skeptics** whose brief is to kill each
  finding, not grade it → one **promoter** per money-path or legal-obligation
  refutation, whose brief is to overturn it (a refutation on those surfaces is itself a
  decision, so it gets an adversary too). Lanes pipeline independently; the two-skeptic
  barrier is within a lane only.
- **Extracted, not invented.** The script is byte-for-byte the workflow that ran a
  production standing-tree audit fleet, with exactly two edits — both citations of an
  internal planning document, generalized to statements of the same rule (2 changed
  lines of 46; diff recorded in `PROVENANCE.md`). Every project-shaped input already
  travelled through `args` (`lanes`, `briefsPath`, `outDir`, `contextNote`), so the
  extraction needed no parameterization work.
- **`BRIEFS-TEMPLATE.md`** — the generic `§A FINDER` / `§B SKEPTIC` skeletons the
  workflow points `briefsPath` at, plus `§C`, the calling session's synthesis gate.
  Without it the plugin would be unusable by a stranger; with it the core is complete
  with no shim.
- **The lessons are the payload.** README records the **blind-reader ordering defect**
  (a skeptic brief whose "read the finder's report" step preceded its scoring step
  voided a multi-reader criterion **fleet-wide** — hand skeptics the drawn list
  separately, score before the report is opened, never a finder's report before
  scoring); the seeded-draw rejection rule (`printf` not `echo`; reject a degenerate
  modulus); and that a **diff-derived verification mix does not transfer to a
  standing-tree audit** (66/29/6/0 measured on diffs vs **32.6/49.4/15.7/2.2** measured
  over 178 standing-tree verdict slots).
- **Measured cost: ≈0.9M subagent tokens per lane triple on Opus** — 31 agents /
  8,112,759 subagent tokens on the validating run. A ~0.55M diff-derived estimate
  under-predicted by ~40%; the README says so, because a fleet sized from the low number
  runs out of budget mid-campaign.
- **Known gaps, documented not hidden (0.1.0):** no argument validation (a malformed
  `args` fails with a raw `TypeError`, and there is no traversal/flag guard on
  `briefsPath` / `outDir` before they reach prompts), and `args` is assumed pre-parsed.
  Shipped as validated rather than hardened-in-flight; queued for 0.1.1.

### Fixed — coupling gate was blind to whole content types
- **`scripts/check-coupling.sh`** now enumerates model-facing content by **exclusion**
  (everything under a plugin except `.claude-plugin/`) rather than by an allow-list of
  `skills/` + `scripts/` + `references/` + `*.md`. The allow-list silently skipped
  content types as plugin layouts grew: `workflows/*.js` (whose prompt strings are pure
  model-facing instruction) and `hooks/*.sh` were **entirely unscanned**. Verified by
  positive control — a planted `streakbank` / `/Users/` / named-rule string in a
  workflow file **passed** the old gate and **fails** the new one on all three checks.
  All three existing plugins stay clean under the wider scan.

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
