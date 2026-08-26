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
top-level `return`. Syntax was verified by checking the body inside an `async function`
wrapper on Node 23.10.0, against the source as a control.

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
- **Revalidate by:** the next plugin version bump, or whenever the dynamic-workflow
  runtime's `args` handling changes (see the README's "Known gaps" — this version assumes
  a pre-parsed `args` object and performs no argument validation).

## If a future addition vendors upstream material

Record here: upstream repo URL, pinned commit hash, exact files taken, every
pruning/edit applied; add the upstream LICENSE adjacent to the vendored content; scan
third-party skill content per CONTRIBUTING §6 before it enters the repo.
