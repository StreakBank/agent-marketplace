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
Independent review then showed the hook-authored-note refresh re-hashed whatever
the live task held after its own append, so a raw task edit landing inside that
window was hashed over; this was reproduced with a patched note call on a real
board before it was scoped to exactly the appended text. The scope ignores the
edit stamp an append moves on its own, so a content-free stamp bump inside the
same window remains unreported. A foreign write that lands after the re-read is
caught by the ordinary Stop comparison, not by this scope.
Validated 2026-10-05 against Backlog.md 1.52.0: 45 tests, all passing.

2026-11-12 released-binding correction (0.1.3), from a live session rather than
inferred: a lead checkpointed its task Done, ran further tools, and found
`checkpoint` ("Bind an active task first"), `release` ("Checkpoint before releasing
task ownership") and `start` ("Checkpoint the current task before binding a
different one") all refusing; rebinding the Done task was the only command that
worked, and it set that task back to In Progress. Validated with Backlog.md 1.52.0:
the unit suite (54 tests, real Backlog files, native-shaped payloads), and one pass
of the script as a subprocess on a throwaway board (hook payloads on stdin, commands
as argv), run against 0.1.2 and against this version: 0.1.2 refused all three
commands, this version bound the second task and left the first Done. That pass does
not certify installed hook trust or live callback ordering in either harness.
