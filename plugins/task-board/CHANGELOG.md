# Changelog

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
