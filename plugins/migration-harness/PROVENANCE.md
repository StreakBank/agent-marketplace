# Provenance — migration-harness

## references/

`SPINE.md`, `RECIPE-CONTRACT.md`, `ESCALATION-PACKET.md` are **authored-original**
for this plugin. No upstream repository was vendored; there is no third-party LICENSE
to carry and nothing to attribute. This file exists to satisfy the marketplace
admission gate (`check-coupling.sh`: a `references/` directory requires a
PROVENANCE.md), and to keep the provenance ledger honest rather than absent.

The content was distilled from the execution records of six real migrations run on
the authoring organization's production codebase (2026-07); the records themselves
remain in that project's private documentation and are not vendored here. The
distillation removed all project-identifying content (names, paths, rule files,
internal finding ids) per the marketplace coupling gate.

## scripts/

`migrate-ledger.mjs` + `migrate-ledger.test.mjs` are authored-original, zero-dep
Node ≥ 18.3 (`util.parseArgs`; validated on Node 23.10.0). No wrapped binary, no
pinned external tool.

## Validation + revalidation

- 2026-07-06 — authored; adversarially reviewed pre-commit (35-agent default-refute
  pass; all 23 surviving findings fixed, incl. the grep-absent vacuous-scope
  silent-green and stale-evidence gate gaps); 15/15 unit tests green (`node --test
  plugins/migration-harness/scripts/*.test.mjs`, Node 23.10.0).
- 2026-07-06 (same day) — **validated by real use** on a bounded production
  migration in the authoring project (a 16-site flow start-mode sweep across 8
  ViewModels, 2 partitioned transform agents): full ledger lifecycle exercised
  end-to-end (init → import → classify 10 mechanical / 6 semantic →
  block-until-decided → decide → check-partition → verify → fix-loop → outcome →
  `gate --reverify` green). The mechanical per-site verify caught 2 real test
  regressions the transform agents' self-reports missed — the tool's core promise,
  demonstrated. Learnings queued for 0.1.1: a `check-changes` command (VCS status vs
  declared agent file sets — done manually this run).
- Revalidate by: the next plugin version bump, or whenever the ledger schema
  (`schemaVersion`) changes — schema changes require a migration note in the
  CHANGELOG and a major-ish semver bump.

## If a future references/ addition vendors upstream material

Record here: upstream repo URL, pinned commit hash, exact files taken, every
pruning/edit applied; add the upstream LICENSE adjacent to the vendored content; scan
third-party skill content per CONTRIBUTING §6 before it enters the repo.
