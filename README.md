# agent-marketplace

General-purpose [Claude Code plugins](https://docs.claude.com/en/docs/claude-code/plugins)
built to a strict reusability discipline: every plugin is a **generic core** — tool
mechanics, protocols, pinned references — with **zero project coupling**. Project
policy (which tool owns which niche in your estate, instance names, QA-layer rulings)
lives in a thin shim on *your* side, in your project's `.claude/` rules.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the discipline; it is the admission gate
for every plugin here.

## Install

```bash
# once per machine
claude plugin marketplace add StreakBank/agent-marketplace

# per plugin — user scope (all projects on this machine)
claude plugin install android-device@agent-marketplace

# or project scope (travels with a repo)
claude plugin install android-device@agent-marketplace --scope project
```

Then write your project's shim: a short rule in your project's `.claude/rules/`
stating the project facts and policies the generic skill deliberately doesn't know
(e.g. which AVD is your test device, how the plugin divides labor with your other
device tooling). Plugins that need a shim ship a `SHIM-TEMPLATE.md` to copy.

If `claude plugin marketplace add StreakBank/agent-marketplace` fails on a machine
without SSH keys (the CLI may resolve a github repo over SSH), register the https URL
explicitly instead: `claude plugin marketplace add https://github.com/StreakBank/agent-marketplace.git`.

### Fresh-machine bootstrap

The two commands above, plus — once, per machine — anchor the authoring discipline so
sessions in *any* project know these plugins exist and how new tooling should be
built. Add ~5 lines to `~/.claude/CLAUDE.md` (user-level, loads in every project):

```
## Reusable agent tooling
New Claude plugins/skills/CLIs are built for cross-project reuse: generic core in a
marketplace (StreakBank/agent-marketplace; cmp-marketplace for CMP) + a thin project
shim in .claude/rules/. Full discipline + the learning-return loop:
~/.claude/plugins/marketplaces/agent-marketplace/CONTRIBUTING.md (cloned by install).
Writable clone to contribute: gh repo clone StreakBank/agent-marketplace.
```

The pointer target is cloned onto every machine by the install, so it resolves
everywhere the marketplace is registered.

### Contributing back

Plugins compound by use. A **generic** discovery about a plugin's tool goes back into
the plugin (see [CONTRIBUTING §8](CONTRIBUTING.md)); project-specific facts stay in
your shim. This return edge is the point — learnings don't die in whatever project
first hit them.

## Plugins

| Plugin | What it does |
|---|---|
| [`android-device`](plugins/android-device/) | Android emulator + device control and offline Android/Compose docs search via the pinned Android agent CLI: blocking AVD start/stop/list, UI layout inspection with change diffs (`layout --diff`), annotated screenshots with label→coordinate resolution, one-command APK install+launch, local docs knowledge base. |

## Versioning

Semver per plugin (`plugin.json`); the marketplace `metadata.version` bumps on any
plugin change; [CHANGELOG.md](CHANGELOG.md) records every bump. Wrapped binaries are
pinned by version + SHA-256 independently of plugin versions (see each plugin's
`PROVENANCE.md`).

## License

Apache-2.0 (see [LICENSE](LICENSE)). Vendored upstream content keeps its own upstream
license adjacent to the content, recorded in the owning plugin's `PROVENANCE.md`.
