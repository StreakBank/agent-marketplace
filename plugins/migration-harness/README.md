# migration-harness

Staged, ledger-tracked codebase migrations for Claude Code. The model does the
judgment work (discovery reading, transforms, escalation packets); a bundled zero-dep
ledger tool owns everything a human must be able to audit — which sites exist, how
each was classified, who decided the contested ones, what mechanically verified each,
and how every site was closed.

The protocol was **extracted from six real migrations** on a production multi-module
codebase, not designed on paper — see `references/SPINE.md` for the shared stage
spine, the variance points recipes must supply, and the observed failure modes.

## What you get

- `skills/migration-harness/SKILL.md` — the 8-stage protocol: discover → classify →
  batch-escalate → plan fan-out → transform → verify-each → gate → record.
- `scripts/migrate-ledger.mjs` — zero-dep Node ≥ 18.3 CLI:
  `init | import | add-site | classify | decide | assign | check-partition |
  add-verify | verify | outcome | gate | status | hook`. Exit codes: 0 pass,
  1 gate/check failed, 2 usage error. Tests: `node --test scripts/*.test.mjs`.
- **A mechanical block, not a convention:** `gate` exits 1 while any
  semantic-escalate site lacks a recorded human decision, any site's outcome is
  pending, or any done site lacks passing verifies. Verify evidence is
  freshness-checked in git repos (each run records a tree fingerprint; evidence
  recorded before later source changes reads as STALE), re-classification clears a
  stale decision, and `grep-absent` refuses to pass on a scope that matches no files.
  `hook` mode can additionally block `git commit` via a PreToolUse hook (project's
  choice — see SHIM-TEMPLATE.md).
- `references/` — the extracted spine (SPINE.md), the contract a stack-specific
  recipe must fill (RECIPE-CONTRACT.md), and the decision-packet format
  (ESCALATION-PACKET.md).

## Install

```sh
claude plugin marketplace add StreakBank/agent-marketplace   # once per machine
claude plugin install migration-harness@agent-marketplace
```

## Quickstart (any repo)

```sh
# Marketplace-clone copy; inside a Claude Code skill the same tool is at
# $CLAUDE_PLUGIN_ROOT/scripts/migrate-ledger.mjs (the install cache).
ML=~/.claude/plugins/marketplaces/agent-marketplace/plugins/migration-harness/scripts/migrate-ledger.mjs

node "$ML" init --id my-sweep --invariant "no call site passes legacyParam"
cat > inventory.json <<'JSON'
[
  { "id": "s1", "file": "src/Foo.kt", "module": ":foo" },
  { "id": "s2", "file": "src/Bar.kt", "module": ":bar" }
]
JSON
node "$ML" import --sites inventory.json
node "$ML" classify --site s1 --as mechanical
node "$ML" classify --site s2 --as semantic --reason "public entry point; gate can't tell"
node "$ML" decide --site s2 --choice "keep public, document" --by owner
node "$ML" add-verify --id residue --type grep-absent --pattern legacyParam --paths src
node "$ML" verify --all
node "$ML" outcome --site s1 --as done
node "$ML" outcome --site s2 --as done
node "$ML" gate --reverify   # exit 0 → the migration is committable
```

In a Claude Code session, invoke the `migration-harness` skill instead — it sequences
the stages and authors the escalation packet.

## Project shim

Build/verify command templates, which completion gates apply, and agent-dispatch
policy are project facts — they live in the consuming project's `.claude/` estate,
never here. Copy `SHIM-TEMPLATE.md` into the project's rules to seed one. Recipes
(per-migration-class discovery/transform/verify content) live with the stack-specific
plugin or catalog that owns their domain, written against
`references/RECIPE-CONTRACT.md`.

## Improving this plugin

Generic discoveries (tool bugs, protocol improvements, new observed failure modes) →
edit this plugin, bump semver + CHANGELOG, run `scripts/check-coupling.sh`, push.
Project facts → the consuming project's shim. See the marketplace CONTRIBUTING.md.
