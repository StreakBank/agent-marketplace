# Changelog

## 0.1.3 — 2026-11-12

- Let a session bind its next task after a released one. A Done checkpoint (or
  `release`) leaves the session with a released binding; once the lead then ran any
  tool, `checkpoint` refused the released binding while `release` and `start` both
  refused the uncheckpointed work, so no command could succeed and the only way out
  was to rebind the Done task, which reopened it. `start` now asks for a checkpoint
  only when the binding it leaves is active. The active-task guard is unchanged.
- Record on the new binding how many lead work events were counted after the
  previous task was released, in the binding note and in the journal's `bound`
  entry (`unbound_events`), since that work has no other record.
- Make `release` succeed when the binding is already released, and say "No task is
  bound" when there is none.
- Name the way out in the refusals: `checkpoint`, `backlog` and `reconcile-worker`
  on a released or missing binding now give the `start` commands that bind one,
  and the hook's instructions read "Bound task: none (T is released; ...)" instead
  of naming the released task as bound.
- Add regression coverage: work after Done and after `release` bound to a new
  task with the first task left as it was, reopening the released task, the
  idempotent release, the refusal texts, and a control that an active stale
  binding still refuses a switch and creates no orphan task.

## 0.1.2 — 2026-10-05

- Count only the lead's own tool calls toward checkpoint staleness. Worker and
  child tool events keep their journal attribution but no longer invalidate the
  lead's checkpoint, so a background fan-out cannot strand it within seconds.
- Treat an agent start as an observation rather than lead work, and require a new
  lead checkpoint only for a directly delegated worker's return; a
  workflow-internal return is recorded and reviewed at its workflow's completion.
- Keep a runtime note the hook itself appended from invalidating a checkpoint
  that was current immediately before the append. A change by anyone else still
  mismatches the stored task hash and keeps the checkpoint stale.
- Limit that refresh to exactly the appended text. Backlog takes no task-board
  lock, so the re-read after the note can observe another writer's edit; unless
  the notes end with the hook's own text and every other field is unchanged, the
  checkpoint stays stale instead of hashing the foreign edit over.
- Add regression coverage for the staleness policy with controls for the lead's
  own tool call, dispatch, a foreign task edit before, after and inside the hook's
  note window, a directly delegated return, and the agent-id attribution
  assumption.

## 0.1.1 — 2026-09-25

- Recognize native Codex collaboration tool names while preserving plain,
  dotted and Claude delegation names; messages do not imply new assignments.
- Invalidate stale worker returns before follow-up dispatch, preserve fast new
  returns, and require explicit reconciliation for all follow-ups without
  guessing canonical-name/UUID bindings.
- Add isolated-board regression coverage for naming, lifecycle replay, follow-up
  barriers and older session-state compatibility.

## 0.1.0 — 2026-09-24

- Add native command hook adapters, workspace routing by Git identity, lead task
  binding, lifecycle observations, and fresh checkpoint checks.
- Preserve existing native hook configuration and store installation backups.
- Add real-board lifecycle, concurrency, failure, and installer tests.
