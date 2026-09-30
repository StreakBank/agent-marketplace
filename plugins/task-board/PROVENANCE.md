# Provenance

Original implementation; no upstream code or skill text vendored. Standard-library
Python only. Wrapped CLI validated: Backlog.md 1.52.0. The integration invokes its
public CLI and validates the task-view JSON envelope.

Protocol references inspected 2026-09-24:

- [Claude Code hook reference](https://code.claude.com/docs/en/hooks)
- [Codex hook reference](https://learn.chatgpt.com/docs/hooks)
- [Claude dynamic workflows](https://code.claude.com/docs/en/workflows)

Local target versions: Claude Code 2.1.282; Codex CLI 0.153.0-alpha.5.
Tests exercise real Backlog files and native-shaped event payloads. Native session
validation is recorded separately in the consuming workspace's installation task;
passing fixtures alone does not establish live hook or workflow event coverage.

2026-09-25 adapter correction: native event journals exposed
`collaborationspawn_agent`, `collaborationfollowup_task` and
`collaborationsend_message`. Regression fixtures cover these exact tool names and
the preexisting plain/dotted/Claude forms. Full native payloads were not retained,
so no canonical-task-name to lifecycle-UUID mapping is inferred. All follow-ups
require an explicit evidence-bearing reconciliation: even distinct lifecycle
receipts do not establish a causal link to the latest dispatch.
This correction does not certify installed hook trust or live callback ordering.
