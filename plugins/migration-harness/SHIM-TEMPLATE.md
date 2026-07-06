# Shim template — copy into your project as `.claude/rules/migration-harness.md`

Copy the block below into your project's `.claude/rules/migration-harness.md`, fill
the placeholders, delete what doesn't apply, and keep it thin — facts and policy
only; never restate the plugin skill's mechanics (the plugin owns those). Path-key it
on your ledger locations; do NOT force-load it.

```markdown
---
description: Project facts + policy for the migration-harness plugin (staged migrations)
paths:
  - "migration-ledger.json"
  - "**/migration-ledger.json"
  - ".migration/**"
---

# migration-harness — project shim

The generic core is the `migration-harness` plugin skill + ledger tool
(agent-marketplace); it owns the stage protocol, the ledger/gate mechanics, and the
escalation-packet format. This rule adds only what the plugin deliberately doesn't
know about this project.

## Verify command templates (fill from your build system)

Register per-recipe verify entries with THESE templates (placeholders `{module}`,
`{file}`, `{line}` are resolved per site by the tool):

- compile probe (granularity module): `<e.g. your build tool's per-module compile task>`
- targeted tests (granularity module): `<per-module test task>`
- residue check: `grep-absent` with the retired symbol pattern over `<src roots>`

## Completion gate for migrations here

After the fan-out, the lead runs ONCE: `<full build> && <full test suite> &&
<invariant lints> && <visual zero-drift compare / platform canaries as applicable>`.
<Note any project rule about who runs builds — e.g. leads only, never subagents.>

## Escalation

Decisions are recorded with `--by <owner-handle>`. <Name who signs off here.>

## Optional: hard commit block (PreToolUse hook)

To physically block `git commit` while a migration gate fails, add to
`.claude/settings.json` hooks (PreToolUse, matcher `Bash`):
`node <plugin-root>/plugins/migration-harness/scripts/migrate-ledger.mjs hook`
The hook is silent unless a ledger exists upward of the command's cwd (or its
`git -C` target) AND the command is a git commit AND the gate fails. Caveats: it
only finds default-named ledgers (`migration-ledger.json` / `.migration/ledger.json`
— keep the default name, or export `MIGRATE_LEDGER=<path>`); it is best-effort
against accidents, not tamper-proof. Hooks snapshot at session start — verify
liveness in a fresh session after wiring.

## Policy

- <Agent-dispatch policy: which model transform agents run on, prompt-size rules,
  parallelism caps.>
- <Which repos/branches migrations may run on; any deploy-coupled branches to avoid.>
- A generic harness/tool discovery goes back to the plugin (marketplace repo);
  a fact true only for this project stays here.
```
