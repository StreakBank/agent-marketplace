# Contributing — the core/shim discipline

Every plugin in this marketplace is built for **cross-project reuse**. The rules below
are the admission gate; a plugin that fails them doesn't merge.

## 1. Generic core only

A plugin contains tool mechanics, protocols, command references, orchestration
patterns, and vendored+pinned upstream references — nothing else. Project policy
(ownership decisions between overlapping tools, device/AVD/instance names, QA-layer
rulings, severity mappings, output-path conventions) lives in a **thin shim** in the
consuming project's `.claude/` estate, never here.

**The grep test** — run before every merge; all three must return nothing:

```bash
grep -riE '<any project name>' plugins/<name>/          # zero project names
grep -rE '/Users/|/home/|~/Projects' plugins/<name>/    # zero absolute project paths
grep -rE '\.claude/(rules|skills)/[a-z0-9._-]+' plugins/<name>/  # zero NAMED project-rule/skill refs
```

(Telling the consumer "write a shim in your project's `.claude/rules/`" is fine —
that's the discipline. Citing a *named* rule file is coupling.)

**Exempt from the grep test:** the hosting org's name in repo-address lines of
install docs (`claude plugin marketplace add <org>/agent-marketplace` must name the
org) and `author`/`owner` attribution fields in `.claude-plugin/*.json`. Those are
distribution metadata, not content. Everything the model reads as instructions —
SKILL.md, scripts, references, PROVENANCE — gets zero exemptions.

Instance identifiers are the subtle case: an emulator serial, an AVD name, an
applicationId are project facts even when they look like tool arguments. The core
writes `<avd>` / `<serial>` / `<applicationId>`; the caller supplies values.

The core also never *references* a shim — it must be complete without one. Shims
narrow; they don't complete.

## 2. Parameterization order

Prefer the earliest mechanism that fits: (1) environment detection when unambiguous;
(2) invocation arguments for per-call facts; (3) a project config file for durable
machine-readable facts consumed by deterministic tooling; (4) the project shim for
policy. Data flows through detection/args/config; policy flows through shims; nothing
project-shaped flows through the core.

## 3. Naming and versioning

- Capability-based kebab-case, named for what it does, not what it wraps
  (`android-device`, not `android-cli`). Scope prefixes only when the scope is real.
- Semver in `plugin.json`, starting `0.1.0`. Bump `marketplace.json`
  `metadata.version` on any plugin change. Every version bump gets a `CHANGELOG.md`
  line — the changelog is the load-bearing artifact; Claude Code does not enforce
  semver.

## 4. Wrapped binaries pin

A plugin wrapping an external binary records the exact validated version and installs
from a version-pinned URL with SHA-256 verification when the vendor supports it
(see `plugins/android-device/scripts/install-android-cli.sh` for the reference shape).
Skills never auto-update their wrapped binary; bumping a pin = edit checksums,
re-validate, commit.

## 5. Provenance and licensing

Vendored upstream content (reference specs, guidelines, skill fragments) requires:

- the upstream LICENSE file adjacent to the vendored content;
- a `PROVENANCE.md` in the plugin recording the upstream repo, **commit hash**, exact
  files taken, and every pruning/edit applied (this is what makes refresh diffs
  possible);
- no live-fetching of unpinned upstream content at runtime, ever.

## 6. Third-party vetting

Any third-party skill content gets scanned with
[`cisco-ai-skill-scanner`](https://github.com/cisco-ai-defense/ai-skill-scanner)
before it enters this repo, and re-scanned on every refresh.

## 7. Skill quality bar

- Skills are thin: the SKILL.md documents mechanics and sequencing; deterministic
  logic beyond a screenful belongs in a script (or a standalone CLI the skill calls).
- Frontmatter: `name`, a `description` written for model invocation (triggers, not
  marketing), `allowed-tools` as narrow as practical.
- The README quickstart must make sense to a stranger with none of the authoring
  project's context.

## 8. Feeding learnings back (the compounding loop)

Plugins improve by *use*, not just at authoring time — this is the return edge that
makes learnings compound across projects instead of dying in whatever project
happened to hit the gotcha. When a session (in ANY consuming project) discovers a
**generic** fact about a plugin's tool — a new CLI gotcha, a flag that behaves
differently than documented, an installer bug, a better protocol — it belongs in the
**plugin**, not in the consuming project's notes:

1. Clone the marketplace if it isn't already local: `gh repo clone StreakBank/agent-marketplace`.
2. Edit the owning plugin's `SKILL.md` (behavior/protocol), `scripts/` (installer/tool
   bug), or `PROVENANCE.md` (validation/version facts).
3. Bump the plugin's `plugin.json` semver + add a `CHANGELOG.md` line.
4. Run `scripts/check-coupling.sh <plugin>` and push. CI re-gates on merge.

The discriminator is the same core/shim test (§1): if the learning is true for any
project using the tool, it's generic → upstream to the plugin. If it's true only for
this project (which AVD, which package, an ownership ruling), it's a **shim** fact →
it stays in the consuming project's `.claude/rules/` and never comes here.

A consuming project that captures agent learnings in its own always-loaded knowledge
file (e.g. a `.claude/agent-learnings.md`) should carry a carve-out telling sessions
to route generic tool learnings here instead of into the project silo — otherwise the
force-loaded "log it locally" instruction silently swallows every generic discovery.

## 9. Validation before merge

- Run the grep test (§1).
- Exercise every documented command against a real target (device, emulator, repo —
  whatever the plugin drives) and note the validation date + tool version in
  `PROVENANCE.md`.
- Confirm the authoring project still works with the plugin installed globally and its
  local copy (if any) deleted — extraction that breaks the origin is a regression.
