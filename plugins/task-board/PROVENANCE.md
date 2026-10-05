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

2026-10-05 staleness-scope correction, measured on a real board journal rather
than inferred: one lead session's event log held 20,411 entries, 15,671 of them
worker-attributed `PostToolUse` against 1,879 from the lead, and 762 Stop events
journaled `handoff_missing` against 158 `handoff_current`. Every child tool event
counted toward the lead's generation, and both lifecycle events additionally
appended a runtime note that changed the task hash, so the Stop check fired after
almost every lead turn while a background fan-out ran and then exhausted its
two-repair budget. The counts come from one workspace's journal, so they size the
observed failure; they do not enumerate every harness's event shape. The
workflow-internal `agent_type` value was read from that journal. Regression
coverage asserts the policy with controls for each case that must still be
caught; it does not certify installed hook trust or live callback ordering.
Validated 2026-10-05 against Backlog.md 1.52.0: 42 tests, all passing.
