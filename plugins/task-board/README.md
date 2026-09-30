# Task board

Automatically maintain a shared Backlog.md board from native Claude Code and Codex
sessions. Local command hooks record lifecycle facts and require a fresh lead
checkpoint before a normal handoff. No daemon, model API, or extra desktop app.

## Install

Requires macOS or Linux, Python 3.9+, Git, and Backlog.md **1.52.0** already installed. This plugin
does not install or update the wrapped CLI. Both harness manifests package the
same source. Native user hook registration is explicit through the installer;
there is no second plugin hook configuration to load twice.

Run from this plugin directory, substituting your existing paths:

```sh
python3 scripts/install.py --id my-workspace --board /path/to/board \
  --repo /path/to/board --repo /path/to/code \
  --instructions /path/to/workspace-instructions.md \
  --backlog /path/to/backlog --mode observe
```

This previews the exact files. Add `--apply` to install. Repeat with
`--mode enforce --apply` after validating hook loading. The installer preserves
other settings and hooks, saves backups, and creates a user-local `task-board`
executable. It does not alter permission modes or hook trust. Review the new
Codex definitions in `/hooks`. Open fresh harness sessions to validate both.

Repositories are registered by canonical Git common directory. Linked worktrees
therefore share a board; unrelated repositories do nothing. A separate clone
requires its own `--repo` entry. Re-running installation replaces that workspace's
registration, so include its complete repository list. Machine paths live only
in the local registry. A sandboxed code session needs write access to its board
directory for helper commands; grant the specific directory using the harness's
normal mechanism when working outside that board's workspace.

## Agent protocol

Hooks supply the session token and board location automatically. The agent should
not ask the user to maintain the board. A task has one lead; workers return to the
lead instead of creating competing records.

1. Reuse the relevant task with `task-board start --session TOKEN --task ID`, or
   create one with `--title TITLE --description SCOPE --ac CRITERION`. Repeat `--ac`
   as needed; `--project NAME` is optional. Inspect existing tasks before creating.
2. Record scope, assignments, dependencies, and expected evidence through
   `task-board backlog --session TOKEN -- task edit ID ...`. This serializes the
   CLI operation at the home board. Read ordinary CLI guidance with `backlog
   instructions overview`.
3. At milestones and immediately before handing back, run:

   ```sh
   task-board checkpoint --session TOKEN --status 'In Progress' \
     --summary 'What was accomplished' --evidence 'Checks, scope, results' \
     --next 'Next action or concrete blocker'
   ```

4. Use `Review` for unchecked returns, `Blocked` for a concrete blocker, and `Done`
   only after reviewing and checking all acceptance criteria and definition of
   done with the board CLI. Done also requires observed workers to be reconciled.
5. Before another lead takes unfinished work, checkpoint and run `task-board release
   --session TOKEN`. Done checkpoints release ownership automatically. Process
   idleness never releases unfinished work.

`task-board status --session TOKEN` shows that session's binding and observations.
`task-board doctor` shows registrations, last observations, outstanding bindings,
and pending recovery notes. It does not certify native hook loading or trust.
To reconcile a missing worker, use `task-board reconcile-worker --session TOKEN
--agent ID --state unknown --evidence REASON`. Allowed states are `unknown`,
`failed`, `cancelled`, and `returned`; unknown remains a barrier to Done.

Delegation recognizes Claude `Agent`/`Workflow`, plain or dotted Codex
`spawn_agent`/`followup_task`, and native journal names
`collaborationspawn_agent`/`collaborationfollowup_task`. `send_message` (including
`collaborationsend_message`) is not a new assignment. A follow-up to an exact
known worker ID invalidates its previous return before dispatch. A later distinct
native return is recorded even if no new SubagentStart fires. It does not prove
which assignment returned: a delayed old event or replay outside the dedup window
can look new. Every follow-up therefore keeps a separate reconciliation barrier
until the lead explicitly reviews its outcome, including when the target is a
known worker ID. If no distinct turn/prompt receipt is supplied, the old return
may remain deduplicated; neither observation is completion evidence.

Canonical task names are not assumed to identify a lifecycle UUID. Follow-up
targets are recorded in `unresolved_followups`; an unmatched target creates no
worker binding or child-session alias. The barrier blocks Done, release and task switching until
the lead runs `reconcile-worker --agent TARGET --state STATE --evidence REASON`
using that exact observed target. `unknown` keeps the barrier; failure/cancellation
or a reviewed return clears it. This also applies to failed or interrupted
dispatches whose native outcome cannot be correlated. No worker liveness or
successful execution is inferred from a dispatch request.

The last work operation must precede the checkpoint. Later tool activity or task
content changes invalidate it. A normal stop requests at most two repair passes;
then it surfaces a warning rather than trapping the user in an endless loop.
Pure conversation without tool activity does not need a new task. Hooks record
dispatch/start/return observations; they never infer successful implementation.

## Persistence and failure handling

The board's `.task-board/` holds private local runtime state, session bindings,
and an event journal. Ignore this directory in Git. Durable accomplishments and
evidence remain in Backlog task Markdown. Event receipts omit raw prompts, tool
arguments, transcripts, and full agent responses. Each board has a bounded lock;
individual JSON files are replaced atomically. Replayed tool/agent events are
deduplicated over a bounded recent window.

End and interruption hooks attempt a short board note and queue it locally on
failure. The next lead checkpoint reconciles queued observations. Abrupt process
death may produce no hook; the last observation is never proof of current
liveness. Hook errors are visible and fail open. This is a workflow guardrail,
not a security boundary or proof that the lead's evidence is true.

The native definitions use synchronous command hooks. They preserve existing
permission decisions and do not approve tool calls. Tool paths outside native
hook coverage and disabled/untrusted hooks remain gaps. Workflow-internal event
coverage must be verified on each installed harness version; a parent workflow
checkpoint remains required even when individual events are unavailable.

## Test and remove

```sh
python3 tests/test_task_board.py -v
python3 scripts/task_board.py --version
```

Tests create temporary Git repositories and real local boards. Set
`BACKLOG_TEST_BIN` if the pinned CLI is outside the usual user-local bin directory.
The installer test creates its helper symlink only inside a temporary test home.

To disable just this integration, remove the native hook entries whose command
ends in `task-board hook --harness claude` or `task-board hook --harness codex`.
Leave other hooks intact. The installer lists the exact settings and backup
paths. Remove the workspace registration to stop observing just that workspace.
Retain board tasks and runtime recovery records until any active work is handed off.
