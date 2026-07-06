---
name: migration-harness
description: Run a staged, ledger-tracked codebase migration — discover sites, classify mechanical vs judgment, batch-escalate the judgment decisions, fan out transforms with partitioned file sets, verify each site mechanically, and gate completion. Use when migrating N call sites / modules / configs to a new invariant (rename or removal sweeps, boundary re-authoring, config reconciliation, visibility sweeps), when asked to "run a migration", or when a refactor spans more sites than one sitting can hold. Triggers — "migration", "sweep", "migrate all call sites", "staged refactor", "migration ledger".
argument-hint: "[migration-id or a short description of the invariant to migrate to]"
allowed-tools: [Bash, Read, Grep, Glob, Edit, Write, Task]
---

# migration-harness

A migration is a repo-wide change to a stated **target invariant** across N discovered
sites. This skill is the protocol; the deterministic accounting lives in the bundled
ledger tool (`$CLAUDE_PLUGIN_ROOT/scripts/migrate-ledger.mjs`, zero-dep Node ≥ 18 —
alias it as `ML` below). The model does the judgment work (reading, transforming,
authoring escalation packets); the ledger owns everything a human must be able to
audit: which sites exist, how each was classified, who decided the contested ones,
what mechanically verified each, and how every site was closed.

The protocol was extracted from real large-scale migration runs, not designed on
paper — the stage spine, its variance points, and the observed failure modes are in
`$CLAUDE_PLUGIN_ROOT/references/SPINE.md`. Read it once per session before running a
migration. Recipes (per-migration-class instances of the contract in
`references/RECIPE-CONTRACT.md`) live with whatever stack-specific plugin or catalog
the consuming project uses; this harness is stack-agnostic.

## Stage protocol

Run the stages in order. Never start transforming before stage 4 completes.

### 1. Discover → a verified inventory

Produce the inventory artifact BEFORE any transform:

- every candidate site with file (+ line, owning module) — use the recipe's discovery
  mechanics (greps, compiler reports, dependency parses), then READ enough of each hit
  to confirm it is a real site, not a string collision;
- for judgment-heavy migrations: the **consumed-distinction inventory** — per consumer,
  which accessor / branch / behavior it actually depends on (this is what lets the new
  design be *derived* from evidence instead of invented);
- **premise verification** — re-verify the task brief's claims against the code. Real
  runs have falsified their own brief at this stage; a wrong premise discovered after
  transform is a rollback.

```sh
node "$ML" init --id <migration-id> --invariant "<target invariant>" [--ledger <path>]
node "$ML" import --sites inventory.json     # [{id?, title?, file, line?, module?}, ...]
```

Keep the ledger at the consuming repo's root on the migration branch (default
`./migration-ledger.json`) so the gate and hook can find it.

### 2. Classify every site

The discriminator, per site: **"can the completion gate tell right from wrong?"**

- **NO — a wrong transform trips the gate** (won't compile, fails a test, trips the
  invariant lint, moves pixels a zero-drift gate would catch) → `mechanical`.
- **YES — but the gate is blind**: the site compiles, passes, satisfies the lint under
  two or more semantically different choices a human could reasonably disagree over →
  `semantic`. **Default-escalate on uncertainty** — over-escalating costs one batched
  question; guessing wrong ships a silent semantic error that passes every gate.

```sh
node "$ML" classify --site s3 --as semantic --reason "<why the gate can't tell>"
node "$ML" classify --site s1 --as mechanical
```

### 3. Batch-escalate the semantic set — ONE round

Collect every `semantic` site and present them together as one decision packet —
format in `$CLAUDE_PLUGIN_ROOT/references/ESCALATION-PACKET.md`. Each decision gets
concrete options (diffs/previews, per-consumer mapping tables), not abstract
questions. Record each answer:

```sh
node "$ML" decide --site s3 --choice "<the chosen option>" --by <owner> [--notes "..."]
```

The gate (and the optional commit hook) refuses to pass while any semantic site lacks
a recorded decision — this is a mechanical block, not a convention.

### 4. Plan the fan-out — foundation inline, consumers partitioned

- **Foundation is lead-authored inline**: the new type / chokepoint / shared helper /
  integration glue (DI wiring, entry points) is ONE semantic unit — do it yourself
  before fanning out.
- **Consumer sites are embarrassingly parallel** once the foundation contract is
  fixed. Default isolation is `partition`: assign each transform agent an explicit,
  disjoint file set, tell each agent which files NOT to touch, and brief it on the
  other agents' scopes.

```sh
node "$ML" assign --agent a1 --sites s1,s4 --files "path/one,path/two"
node "$ML" check-partition        # exit 1 on any overlap — fix before dispatch
```

Reserve `--isolation worktree` (per-site worktrees, lead merges serially) for the two
cases partitioning can't serve: transforms that must touch the same files, or
migrations needing cheap per-site revert / isolated per-site compiles.

### 5. Transform

Dispatch the transform agents (or edit inline for small N). Keep dispatch prompts
lean — reference the ledger and recipe rather than inlining large code snippets. A
transform agent reports what it changed; it does NOT get to declare itself verified.

### 6. Verify each site — mechanically

Declare the recipe's typed verify entries once, then run them; results are recorded
on each site by the tool, from real exit codes:

```sh
node "$ML" add-verify --id compile --type command --template "<compile probe, may use {module} {file} {line}>" --granularity module
node "$ML" add-verify --id residue --type grep-absent --pattern "<retired symbol>" --paths src
node "$ML" verify --all           # or --site s3 after a fix loop
```

Placeholders are shell-quoted automatically — never quote them in the template
(`test -f {file}`, not `test -f "{file}"`). `grep-absent` fails when its `--paths`
scope matches no files (absence must never pass vacuously) and searches untracked
files too. Each run records a working-tree fingerprint; evidence that predates later
source changes reads as STALE at the gate.

Command templates come from the consuming project's config/shim — this skill and the
tool hardcode no build system. **Never substitute a transform agent's self-report for
this step** — real runs caught an agent attesting past a build-breaking missing
dependency that the mechanical probe found immediately. For judgment migrations, add
adversarial verification on top: a residual sweep, a behavior/copy-equivalence check
(for reactive code, equivalence includes **emission order**, not just values), and a
completeness critic.

### 7. Close every site + gate

```sh
node "$ML" outcome --site s1 --as done
node "$ML" outcome --site s7 --as reverted --reason "<what broke>"
node "$ML" gate [--reverify]      # exit 1 while anything is unresolved
```

Outcomes are a closed set: `done | skipped | deferred | reverted | wontfix` — every
non-done outcome requires a reason. Real migrations produce reverts and reasoned
skips; a ledger with only "done" entries on a judgment migration is a smell. The
migration's own completion gate (build, full tests, invariant lint, visual
zero-drift — whatever the recipe declares) runs ONCE by the lead after the fan-out,
on top of the per-site verifies. `gate --reverify` re-runs every done site's
verifies from scratch — use it before commit.

### 8. Record

Write the execution record where the consuming project keeps migration history:
per-site outcomes (including reverts/skips with reasons), the decisions made, gate
results, and any new failure mode observed. Route learnings: generic harness/tool
discoveries → this plugin (see below); project facts → the project's shim.

## Blocking (how sign-off is enforced)

Three layers, strongest first:

1. `gate` exits 1 while any semantic site is undecided, any outcome is pending, or
   any done site lacks passing verifies. Run it before every commit; it is CI-runnable
   on the migration branch.
2. Orchestration should be structured so transform work for semantic sites cannot be
   scheduled until the ledger holds their decisions (read decisions from the ledger,
   throw if absent).
3. Optionally, the consuming project wires `hook` mode as a PreToolUse hook on Bash:
   it blocks `git commit` (exit 2) while the gate fails and stays silent otherwise.
   See `$CLAUDE_PLUGIN_ROOT/SHIM-TEMPLATE.md` — hooks are project/machine config, so
   wiring is a shim decision, not something this plugin can do for you.

## Cautions and out of scope

- The harness does not pick migrations or invariants — that's an audit's job. It
  starts from a stated invariant and (ideally) a recipe.
- Do not run two migrations against one ledger file; one migration, one ledger, one
  branch.
- The ledger file is human-readable JSON committed to the migration branch — treat a
  hand-edit of verify results as what it is (falsified evidence); `gate --reverify`
  re-derives them from real runs.
- Projects typically carry their own policy for build/verify command templates, which
  gates apply, and how agents may be dispatched — check the project's rules before
  assuming defaults. If the project has no such rule yet, offer to create one from
  `$CLAUDE_PLUGIN_ROOT/SHIM-TEMPLATE.md`.

## Improving this skill

Generic discoveries (a ledger-tool bug, a better stage protocol, a new failure mode
observed in a real run) belong in THIS plugin: clone the marketplace repo, edit
SKILL.md / `scripts/` / `references/`, bump the plugin semver + CHANGELOG, run the
coupling gate, push. Project-specific facts (which build commands, which gates, which
agents policy) stay in the consuming project's shim. New *recipes* belong with the
stack-specific catalog that owns them, written against
`references/RECIPE-CONTRACT.md`.
