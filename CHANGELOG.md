# Changelog

## 0.5.2 — 2026-10-05

- Task Board 0.1.2: a worker's tool calls and a hook-authored runtime note no longer stale the lead's checkpoint; a directly delegated return still does.

## 0.5.1 — 2026-09-25

- Task Board 0.1.1: recognize native Codex delegation aliases and require explicit evidence-bearing reconciliation for every follow-up; lifecycle returns remain observations.

## 0.5.0 — 2026-09-24

- Add task-board: native lifecycle observations, shared board routing, and checkpoint checks for Claude Code and Codex.

## 0.4.2 — 2026-08-30

### audit-fleet 0.1.2 — the promoter's verdict is machine-shaped, and "no gate covers this" must name a gate

- **`PROMOTER-VERDICT:` — a greppable last line on every promoter report.** The promoter
  prompt now requires the report to END with, and to return as its final text, one line
  alone: `PROMOTER-VERDICT: <finding-id> | OVERTURNED|UPHELD-REFUTATION | P<0-3> | <reach>`.
  The briefs template's §C synthesis gate says what the lead does with it: `grep -h
  '^PROMOTER-VERDICT:'` over the promoter reports, **paste verbatim** into the register
  block, re-grep the register and diff the two sets — never re-describe the outcome.
  *Why:* measured on a five-promoter sitting, **two outcomes were transcribed into the
  register inverted** (an `UPHELD-REFUTATION` recorded as overturned, and the reverse) and
  **no gate could catch it** — nothing downstream re-reads the promoter's argument, so a
  tally script reads the register's disposition field and believes it. A verbatim quote of
  a machine-shaped line is the only mechanically checkable step in that chain, which is
  why the requirement sits on the promoter's output rather than in advice to the lead.
  The workflow can enforce the line's *production*, not its transcription — the README's
  "Known gaps" now says so.
- **`nearest_gate` is a MANDATORY manifest column for finders, not prose.** Every claim
  carries one line
  `nearest_gate: <artifact — test/lint/CI step, as file:line or a named CI step> | covers: yes/no | why not`,
  and the artifact must be named **even when it lives outside the audited unit** and
  **even when the finder believes it does not cover the case**; the only legal empty value
  is `none found — searched: <commands>`. A missing or prose-only answer makes the manifest
  **deficient** however many files it lists, and skeptics are told to return
  `MANIFEST: deficient` for it and to refuse to fill the column in on the finder's behalf.
  *Why:* on one production fleet **11 of 12 lanes were called manifest-deficient on exactly
  this**, and essentially every skeptic verdict turned on it — an unnamed gate cannot be
  re-read, so the verdict turns on the finder's adjective instead of on an artifact. The
  0.1.1 wording ("answer why the nearest existing gate does not cover this") was prose in
  the rules list and produced blank answers; the two excuses it produced (out of unit /
  believed non-covering) are now named and refused in the prompt itself.
- **README "Manifest sanity".** A lane's own proposed finding ids are **always wrong** —
  lanes run concurrently and each numbers against a denominator the rest of the fleet is
  moving underneath it. Finder ids are lane-local labels; **the lead renumbers at
  synthesis** in one pass, rewriting every cross-reference. Do not make lanes coordinate
  ids — that reintroduces a barrier between lanes. Same caution for `files_examined` and a
  class finding's `instances`: take the skeptics' re-derived numbers, not the finder's.
- **Compatibility.** The stage graph, the `args` contract, `blindProtocol` and the
  `MONEY_LEGAL_REFUTES:` contract are unchanged. Two prompt additions are unconditional
  (they apply with `blindProtocol` omitted): the finder's nearest-gate paragraph and the
  promoter's verdict line. A lead that parses the promoter's final text against 0.1.1's
  bare `<id> | OVERTURNED|…` shape must accept the `PROMOTER-VERDICT: ` prefix.
- **Tests: 14 → 16**, both new ones asserting prompt text (the promoter verdict line and
  its rationale; the finder's nearest-gate column with and without `blindProtocol`).

## 0.4.1 — 2026-08-26

### audit-fleet 0.1.1 — the blind-reader protocol is now enforced, not just documented

- **`blindProtocol` (optional arg).** 0.1.0 shipped the blind-reader ordering defect as a
  README *lesson*: if a criterion is scored by several readers, a skeptic brief that says
  "read the finder's report" before it says "score" voids every such column. A lesson in a
  README is only as good as the brief someone writes from it — so the ordering is now in
  the prompts the workflow emits. Declare
  `{ criteria, dimensions, poolPredicate, scale?, drawCount?, poolScope?, multiplier?,
  fallbackMultiplier? }` and, for each lane whose `criteria` include one of yours:
  - the **finder** is told it is reader 1 of three, writes the seeded draw to
    `30-finder-<lane>-draw.md` (seed string, `H` in **hex and decimal**, `M`, the pool
    predicate stated mechanically, the enumeration command it actually ran, the
    `<multiplier> mod M ∉ {0,1}` degeneracy check with the fallback declared if used,
    every index, the drawn `file:line item` list — and **nothing** about scores) and its
    own column to `30-finder-<lane>-scores.md`; every score, sub-score, aggregate and
    median is **banned from its main report and final text** (a *finding* is not a score);
  - each **skeptic** gets a **STEP 0 that precedes opening the finder's report**: open
    only the draw file, reproduce `H`/`M`/the indices, score blind from source, write
    `31-skeptic-<lane>-<k>-scores.md` carrying the literal line
    `scored before reading any report` — then, and only then, read the report, with no
    revision of the step-0 numbers. Skeptic 2 also reproduces the draw byte-for-byte; a
    draw that does not reproduce **invalidates the sample** rather than licensing a
    substitute. A skeptic that read anything first declares its column **VOID** — void is
    recoverable, contaminated silently corrupts the median.
  - the skeptic's returned data gains `BLIND: clean` / `BLIND: VOID — <why>` (and skeptic
    2's `DRAW:` line), so contamination is visible in the workflow's own output instead of
    only in a file someone has to open.
- **The gate is mechanical.** Whether a lane runs the blind stage is computed from
  `lanes[].criteria`, not asked of the agent — the source project's local version left
  that to model judgement.
- **Two measured facts folded into the README:** an `H` published hex-only is where a
  production draw stopped reproducing, and a **union pool across split lanes is
  unexecutable** under blind scoring (neither half's skeptics can enumerate the other
  half's files, so no one can reproduce the draw blind) — draw per half, grade per half.
  On the re-run with the enforced protocol, the criterion that had been voided fleet-wide
  produced **three clean columns** and a byte-for-byte reproduced draw.
- **`BRIEFS-TEMPLATE.md`** rewritten to match: §A.3 is now the two-file split plus the
  score ban, §B leads with STEP 0 (blind scoring) ahead of the REFUTE posture and the
  numbered method, §B's old in-line item 10 becomes a back-reference plus "report a score
  in the finder's main report as a protocol breach", and §C's single bullet becomes five —
  three columns in three files, **median over clean columns only**, VOID never averaged,
  fewer than three clean columns ⇒ NOT SCORED, draw reproduced, split units graded per
  half.

### audit-fleet 0.1.1 — both documented 0.1.0 gaps closed

- **Argument validation before the first dispatch.** Every field of `lanes[]`
  (`id/repo/units/paths/loc/criteria/baseSha/note`) plus `briefsPath`, `outDir`,
  `contextNote` and `blindProtocol` is checked for presence and type, and a failure throws
  **naming the field and the value** — `lens-triple-audit: args.lanes[2].loc must be a
  positive finite number — got "7592"`. A fan-out this expensive must not die on a raw
  `TypeError` three agents deep. `lanes[].id` must be filename-safe and unique (it is
  interpolated into every output path, and two lanes sharing an id overwrite each other's
  reports). A raw JSON string for `args` now gets a diagnostic that says exactly that.
- **Path-traversal guard.** `briefsPath` / `outDir` must be absolute and are rejected for
  `..` segments, control characters (a newline breaks the prompt line it lands in) and
  shell metacharacters (agents paste these paths into shell commands). New optional
  **`allowedRoot`** confines both inside a declared root. The guard is lexical — the
  runtime is not guaranteed to expose Node's `path`/`fs` — so it does not resolve
  symlinks; the README says so.
- **`scripts/lens-triple-audit.test.mjs`** — 14 tests, no subagent tokens, runs in CI on
  the existing `node --test` step. It reproduces the dynamic-workflow runtime's function
  wrapper and drives the pipeline with stub agents, which makes it the **syntax gate** for
  a file `node --check` cannot meaningfully check (its top-level `return` is legal only
  inside the runtime's wrapper) as well as the behaviour gate: stage graph and model
  pinning, the promoter id-union across skeptics, every
  validation message, all five path-rejection classes, `allowedRoot` confinement, the
  deterministic blind gate, and `STEP 0` preceding the report read in both skeptic prompts.
- **One coupling-adjacent wording fix:** the finder prompt said "findings in
  REGISTER-SCHEMA shape", naming an artifact only the authoring project has; it now points
  at the caller's own row schema, matching `BRIEFS-TEMPLATE.md`.
- **Compatibility:** with `blindProtocol` omitted, the emitted prompts are 0.1.0's (that
  wording fix aside) and the stage graph is unchanged — asserted by a test.

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
