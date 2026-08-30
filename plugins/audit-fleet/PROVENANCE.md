# Provenance — audit-fleet

## workflows/lens-triple-audit.js

**Authored-original.** No upstream repository was vendored; there is no third-party
LICENSE to carry and nothing to attribute.

The script is the **byte-for-byte** workflow that ran the authoring organization's
production standing-tree audit fleet (2026-08), extracted with exactly two edits, both
required by the coupling gate: the `meta.description` and the promoter prompt each
cited an internal planning document by filename, section and line range. Those citations
became the generic statements of the same rule. **Nothing else changed** — no logic, no
prompt structure, no stage ordering, no model pinning. Verified by diff against the
source at extraction: 2 changed lines out of 46.

The script takes every project-shaped input through `args` (`lanes`, `briefsPath`,
`outDir`, `contextNote`), which is why the extraction needed no parameterization work.

**0.1.1 — no longer byte-for-byte.** Three changes, all made here rather than in the
authoring project, so every consumer gets them:

1. **The blind-reader protocol, generalized.** The authoring project amended its own
   local copy of this workflow after a fleet run in which every skeptic column for a
   multi-reader criterion was voided by ordering (the skeptic prompt said "read the
   finder's report" before any scoring step), one draw did not reproduce (`H` published
   hex-only, the pool predicate stated in prose) and one draw was degenerate (the
   multiplier's residue mod the pool size was 1, yielding a contiguous block). That
   amendment was written against that project's own criterion numbering, its own
   sub-score dimensions and its own language-specific pool predicate. Ported here it
   became the **optional `blindProtocol` arg**: the caller declares which criterion ids
   are multi-reader, the sub-score dimensions, the scale, the draw size, the pool
   predicate, the pool scope and the multipliers; the workflow contributes only the
   *ordering* and the *file separation*, which are the generic part. Two mechanics were
   improved in the port rather than copied: the per-lane gate is computed from
   `lanes[].criteria` instead of asking the agent to check whether the criterion is in
   scope, and the skeptic's returned data carries `BLIND:` / `DRAW:` lines so
   contamination is visible in the workflow's own output.
2. **Argument validation + a path guard** (the two gaps 0.1.0 shipped documented).
3. **One wording fix:** the finder prompt's "findings in REGISTER-SCHEMA shape" named an
   artifact only the authoring project has; it now points at the caller's own row schema,
   matching `BRIEFS-TEMPLATE.md`.

With `blindProtocol` omitted, the emitted prompts are the 0.1.0 prompts (that wording fix
aside) and the stage graph is unchanged — asserted by
`scripts/lens-triple-audit.test.mjs`.

**0.1.2 — two prompt requirements added, both sourced from a post-fleet synthesis.** Both
come from the authoring project's **s18 session, records 45 and 47** (the post-fleet
synthesis and the refute pass over it), and both are measurements from that fleet rather
than authored advice:

1. **The promoter's `PROMOTER-VERDICT:` last line.** In a five-promoter sitting, two
   promoter outcomes reached the register **inverted** — transcribed by hand, in the
   opposite disposition — and no gate downstream could catch it, because nothing
   downstream re-reads a promoter's argument and the tally reads the register's
   disposition field. Generalized here as a machine-shaped final line the lead quotes
   verbatim; the field set (`OVERTURNED|UPHELD-REFUTATION`, a priority token, a reach
   token) is the generic part, the priority *scale* remains the caller's.
2. **The finder's mandatory `nearest_gate` manifest column.** 11 of the 12 lanes in that
   fleet were called manifest-deficient on the nearest-gate question specifically, and
   skeptic verdicts turned on it more often than on any other manifest field. 0.1.1
   already asked the question, as prose in the finder brief's rules list; 0.1.2 makes it a
   column and names the two excuses that produced blank answers (the artifact is outside
   the audited unit; the finder believes it does not cover the case) as explicitly
   insufficient.

The lead-side halves — grep-and-diff the transcription, renumber lane-local ids at
synthesis — went into `BRIEFS-TEMPLATE.md` §C and the README, since the workflow can
require a promoter's *output* but cannot see what a lead writes down. Both additions are
unconditional: they are in the emitted prompts with `blindProtocol` omitted, so from 0.1.2
the finder and promoter prompts are no longer 0.1.0's.

## scripts/lens-triple-audit.test.mjs

Authored-original, added at 0.1.1. It reproduces the dynamic-workflow runtime's function
wrapper (globals injected as parameters, top-level `return` legal) and drives the
pipeline with stub agents, so it is simultaneously the **syntax gate** for a file
`node --check` cannot accept and the behaviour gate for validation, the path guard and
the blind-protocol prompt ordering. It spends no subagent tokens and runs in CI via the
repo's existing `node --test plugins/**/*.test.mjs` step.

## BRIEFS-TEMPLATE.md, README.md

Authored-original, distilled from the finder/skeptic brief skeletons and the post-fleet
synthesis gate used on that same run. The distillation removed all project-identifying
content (organization and repository names, absolute paths, internal document and rule
filenames, internal finding ids, dated in-session rulings) per the marketplace coupling
gate. The originals remain in that project's private records and are not vendored here.

The lessons recorded in the README's "Lessons the shape is built on" section are
outcomes measured on that run, not authored advice — in particular the blind-reader
ordering defect (a multi-reader criterion voided fleet-wide because the skeptic brief's
"read the finder's report" step preceded the scoring step) and the two measured
verification mixes.

## Runtime

The workflow depends on the Claude Code dynamic-workflow runtime globals
(`args`, `agent`, `pipeline`, `parallel`, `log`) and on top-level `return` being legal,
i.e. the script body is evaluated inside a function wrapper by the host. It is not a
standalone Node module and `node --check` at module scope will (correctly) reject its
top-level `return` (measured on Node 23.10.0: exit 1, `SyntaxError: Illegal return
statement`, when the same bytes are checked as `.mjs`; `node --check` on the `.js` path
exits 0 without exercising the wrapper, so it proves little either way). Syntax is
verified by reproducing that wrapper — since 0.1.1 in the committed
`scripts/lens-triple-audit.test.mjs`, which reads the workflow file, wraps it and runs
it.

No wrapped binary, no pinned external tool, no runtime fetch.

## Validation + revalidation

- **2026-08-26 — extracted and coupling-gated.** `scripts/check-coupling.sh audit-fleet`
  clean; syntax verified as above; diff against the production source confirms the two
  intended edits and no others.
- **Validated by real use BEFORE extraction (2026-08-26, the authoring project):** the
  full three-stage pipeline drove 31 agents (8 finders, 18 skeptics, 3 promoters, plus
  one extra lens) over 8,112,759 subagent tokens, producing 89 findings across 178
  verdict slots — 32.6% double-confirmed, 49.4% adjusted, 15.7% contested, 2.2%
  both-refuted; 3 promoter-survived findings; 2 findings killed. The promoter stage fired
  as designed on money/legal refutes. **The run also surfaced this plugin's most valuable
  documented lesson** (the blind-reader ordering defect), which is why the README carries
  it rather than the CHANGELOG.
- **UNVERIFIED at 0.1.0:** the workflow has not been exercised through the plugin
  install path (`/audit-fleet:lens-triple-audit`) — only as a project-local workflow with
  identical script text. Behavior is expected to be identical since resolution differs
  only in where the file is loaded from; confirm on first plugin-resolved run.
- **2026-08-26 — 0.1.1 (blind protocol + hardening).** `scripts/check-coupling.sh
  audit-fleet` clean; `node --test plugins/audit-fleet/scripts/lens-triple-audit.test.mjs`
  **14/14 pass** on Node 23.10.0 (14 tests covering the stage graph, the promoter union,
  every validated field, the path guard's five rejection classes, `allowedRoot`
  confinement, the deterministic blind gate, and STEP-0-before-report ordering in both
  skeptic prompts). Syntax is verified by that same wrapper — see the Runtime section for
  what `node --check` does and does not prove here.
- **Validated by real use BEFORE the port (2026-08-26, the authoring project):** the
  amended protocol — separate draw file, skeptics scoring before opening any report —
  ran a four-lane second fleet (13 agents + one re-dispatched skeptic, ≈3.45M subagent
  tokens) and produced, on the criterion that had been voided fleet-wide the run before,
  **three clean blind columns** and a draw reproduced byte-for-byte by skeptic 2. That
  run's amendment text is what was generalized here; the project-specific criterion
  numbering, dimensions and predicate stayed on that side, in `blindProtocol` args.
- **UNVERIFIED at 0.1.1:** `blindProtocol` itself has not been exercised through the
  plugin install path either, and the port's parameterization (arg-declared criteria /
  dimensions / predicate) has not yet driven a live fleet — only the project-local
  hard-coded equivalent has. Confirm on the first plugin-resolved run with
  `blindProtocol` set.
- **2026-08-30 — 0.1.2 (promoter verdict line + mandatory nearest-gate column).**
  `scripts/check-coupling.sh audit-fleet` clean; `node --test
  plugins/audit-fleet/scripts/lens-triple-audit.test.mjs` **16/16 pass** on Node 23.10.0
  (the 14 prior tests plus two asserting the new prompt text: the promoter's
  `PROMOTER-VERDICT:` line with its verbatim-transcription rationale, and the finder's
  `nearest_gate` column with and without `blindProtocol`).
- **Sourced from real use, not authored (2026-08-30, the authoring project's s18 session,
  records 45 and 47):** both requirements are outcomes of a fleet's post-run synthesis and
  the refute pass over that synthesis — the two inverted promoter transcriptions out of
  five, and the 11-of-12 lanes called manifest-deficient on the nearest-gate question.
  **UNVERIFIED at 0.1.2:** neither requirement has yet driven a fleet in the amended form;
  they are corrections derived from a completed run, not re-measured on a new one. Confirm
  on the next fleet — specifically that the promoter reports' last lines grep cleanly and
  that the `nearest_gate` column moves the `MANIFEST:` verdict rate.
- **Revalidate by:** the next plugin version bump, or whenever the dynamic-workflow
  runtime's `args` handling changes (the workflow now validates `args` but still assumes
  the runtime hands it a parsed object; see the README's "Known gaps").

## If a future addition vendors upstream material

Record here: upstream repo URL, pinned commit hash, exact files taken, every
pruning/edit applied; add the upstream LICENSE adjacent to the vendored content; scan
third-party skill content per CONTRIBUTING §6 before it enters the repo.
