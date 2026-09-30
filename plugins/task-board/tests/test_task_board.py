"""Exercise real hook payloads against an isolated, real Backlog.md board."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
SCRIPT = Path(__file__).resolve().parents[1] / "scripts/task_board.py"
spec = importlib.util.spec_from_file_location("task_board", SCRIPT)
tb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tb)
BACKLOG = os.environ.get("BACKLOG_TEST_BIN", str(Path.home() / ".local/bin/backlog"))


class ToolNames(unittest.TestCase):
    def test_native_plain_and_dotted_names(self):
        for expected, names in {
            "Agent": ["Agent", "tools.Agent"],
            "Workflow": ["Workflow", "tools.Workflow"],
            "spawn_agent": ["spawn_agent", "collaboration.spawn_agent", "functions.collaboration.spawn_agent", "collaborationspawn_agent"],
            "followup_task": ["followup_task", "collaboration.followup_task", "collaborationfollowup_task"],
            "send_message": ["send_message", "collaboration.send_message", "collaborationsend_message"],
        }.items():
            for name in names:
                with self.subTest(name=name):
                    self.assertEqual(tb.normalize_tool_name(name), expected)

    def test_unknown_names_are_not_suffix_matched_to_delegation(self):
        for name in ["customspawn_agent", "mcp__collaboration__spawn_agent", "collaborationspawn_agent_extra", None, 1]:
            with self.subTest(name=name):
                self.assertNotIn(tb.normalize_tool_name(name), tb.DELEGATE)


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.board = self.root / "board"
        self.board.mkdir()
        subprocess.run(["git", "init", "-q", str(self.board)], check=True)
        (self.board / "backlog.config.yml").write_text('project_name: Test\nstatuses: ["To Do", "In Progress", "Blocked", "Review", "Done"]\ndefault_status: "To Do"\nfilesystem_only: true\nremote_operations: false\nauto_commit: false\ntask_prefix: T\nbacklog_directory: backlog\ndefinition_of_done: ["Evidence reviewed"]\n')
        self.reg = self.root / "registry.json"
        self.w = {"id": "test", "board": str(self.board), "git_common_dirs": [tb.common_dir(self.board)],
                  "backlog_bin": BACKLOG, "mode": "enforce", "instruction_files": []}
        tb.atomic_json(self.reg, {"workspaces": [self.w]})
        self.old = os.environ.get("TASK_BOARD_REGISTRY")
        os.environ["TASK_BOARD_REGISTRY"] = str(self.reg)
        self.sid = "session-a"
        self.token = tb.token_for("claude", self.sid)
        self.event("SessionStart")

    def tearDown(self):
        if self.old is None:
            os.environ.pop("TASK_BOARD_REGISTRY", None)
        else:
            os.environ["TASK_BOARD_REGISTRY"] = self.old
        self.temp.cleanup()

    def event(self, name, **extra):
        payload = dict(cwd=str(self.board), session_id=self.sid, hook_event_name=name, **extra)
        return tb.hook(payload, "claude")

    def command(self, command, *args, token=None):
        return tb.session_command(tb.parser().parse_args([command, "--session", token or self.token] + list(args)))

    def start(self):
        return self.command("start", "--title", "Test change", "--description", "Exercise hooks", "--ac", "Result checked")["task"]

    def checkpoint(self, status="In Progress"):
        return self.command("checkpoint", "--status", status, "--summary", "Observed change", "--evidence", "Targeted checks passed", "--next", "Continue review")

    def test_conversation_has_no_forced_task(self):
        self.assertEqual(self.event("Stop"), {})

    def test_edit_before_binding_is_denied(self):
        decision = self.event("PreToolUse", tool_name="Edit", tool_use_id="one")["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")

    def test_all_delegate_spellings_require_binding_and_record_dispatch(self):
        names = ["Agent", "Workflow", "spawn_agent", "followup_task",
                 "collaboration.spawn_agent", "collaboration.followup_task",
                 "collaborationspawn_agent", "collaborationfollowup_task"]
        for name in names:
            with self.subTest(name=name):
                decision = self.event("PreToolUse", tool_name=name, tool_use_id=name)["hookSpecificOutput"]
                self.assertEqual(decision["permissionDecision"], "deny")
        self.start()
        for name in names:
            self.event("PreToolUse", tool_name=name, tool_input={"task_name": "parser", "target": "/root/parser"}, tool_use_id=name)
        events = [json.loads(line) for line in (self.board / ".task-board/events.jsonl").read_text().splitlines()]
        dispatches = [entry for entry in events if entry["event"] == "dispatch_requested"]
        self.assertEqual(len(dispatches), len(names))
        self.assertEqual([entry["native_tool"] for entry in dispatches], names)
        self.assertTrue(self.command("status")["dispatch_seen"])

    def test_messages_and_unknown_tools_do_not_invent_dispatches(self):
        for name in ["collaborationsend_message", "collaboration.send_message", "send_message", "customspawn_agent"]:
            self.assertEqual(self.event("PreToolUse", tool_name=name, tool_input={"target": "worker-a"}), {})
        self.start()
        self.event("PreToolUse", tool_name="collaborationsend_message", tool_input={"target": "worker-a"})
        self.event("SubagentStart", agent_id="worker-a")
        state = self.command("status")
        self.assertEqual(state["workers"], {})
        self.assertFalse(state["dispatch_seen"])

    def test_observe_mode_does_not_deny_tools(self):
        self.w["mode"] = "observe"
        tb.atomic_json(self.reg, {"workspaces": [self.w]})
        decision = self.event("PreToolUse", tool_name="Edit")["hookSpecificOutput"]
        self.assertNotIn("permissionDecision", decision)

    def test_checkpoint_satisfies_stop_and_new_work_invalidates_it(self):
        self.start()
        self.checkpoint()
        self.assertEqual(self.event("Stop"), {})
        self.event("PostToolUse", tool_name="Bash", tool_input={"command": "pwd"}, tool_use_id="read")
        self.assertEqual(self.event("Stop")["decision"], "block")
        self.checkpoint()
        self.assertEqual(self.event("Stop"), {})

    def test_helper_exemption_does_not_allow_chained_commands(self):
        self.start()
        self.checkpoint()
        self.event("PostToolUse", tool_name="Bash", tool_input={"command": "task-board status --session " + self.token})
        self.assertEqual(self.event("Stop"), {})
        self.event("PostToolUse", tool_name="Bash", tool_input={"command": "task-board status; touch app.py"})
        self.assertEqual(self.event("Stop")["decision"], "block")

    def test_quoted_checkpoint_prose_and_native_shell_wrapper(self):
        command = "task-board checkpoint --session abc --summary 'Passed; next step is review' --evidence 'check A | check B' --next 'Report $variable literally'"
        self.assertTrue(tb.board_command({"tool_input": {"command": command}}))
        self.assertTrue(tb.board_command({"tool_input": {"command": ["/bin/zsh", "-lc", command]}}))
        self.assertFalse(tb.board_command({"tool_input": {"command": command + "; touch app.py"}}))
        self.assertFalse(tb.board_command({"tool_input": {"command": 'task-board status --session "$(touch app.py)"'}}))

    def test_raw_board_change_invalidates_checkpoint(self):
        task_id = self.start()
        self.checkpoint()
        tb.backlog(self.w, ["task", "edit", task_id, "--append-notes", "New material result", "--plain"])
        self.assertEqual(self.event("Stop")["decision"], "block")

    def test_dispatch_and_return_are_distinct_from_done(self):
        task_id = self.start()
        self.event("PreToolUse", tool_name="Agent", tool_input={"description": "Review parser"}, tool_use_id="dispatch")
        self.event("SubagentStart", agent_id="worker-a", agent_type="Explore")
        self.event("SubagentStop", agent_id="worker-a", agent_type="Explore")
        record = tb.task(self.w, task_id)
        self.assertEqual(record["status"], "In Progress")
        self.assertIn("Review parser", record["implementationNotes"])
        self.assertIn("Lead review pending", record["implementationNotes"])
        self.assertEqual(self.event("SubagentStop", agent_id="worker-a", agent_type="Explore"), {})
        record2 = tb.task(self.w, task_id)
        self.assertEqual(record2["implementationNotes"], record["implementationNotes"])

    def test_done_requires_checks_and_worker_reconciliation(self):
        task_id = self.start()
        with self.assertRaises(ValueError):
            self.checkpoint("Done")
        self.command("backlog", "--", "task", "edit", task_id, "--check-ac", "1", "--check-dod", "1", "--plain")
        self.event("PreToolUse", tool_name="Agent", tool_input={"description": "Check"})
        self.event("SubagentStart", agent_id="worker-a", agent_type="Explore")
        with self.assertRaises(ValueError):
            self.checkpoint("Done")
        self.event("SubagentStop", agent_id="worker-a", agent_type="Explore")
        self.checkpoint("Done")
        self.assertEqual(tb.task(self.w, task_id)["status"], "Done")

    def test_retry_budget_and_compaction(self):
        self.start()
        self.assertEqual(self.event("Stop")["decision"], "block")
        self.event("SessionStart", source="compact")
        self.event("UserPromptSubmit", prompt=tb.REPAIR + "Repair now")
        self.assertEqual(self.event("Stop")["decision"], "block")
        self.assertIn("systemMessage", self.event("Stop"))

    def test_second_lead_needs_explicit_handoff(self):
        task_id = self.start()
        self.sid = "session-b"
        token = tb.token_for("claude", self.sid)
        self.event("SessionStart")
        with self.assertRaises(ValueError):
            self.command("start", "--task", task_id, token=token)
        self.checkpoint()
        self.command("release")
        self.command("start", "--task", task_id, token=token)

    def test_interruption_preserves_unreviewed_work(self):
        task_id = self.start()
        result = self.event("Interrupt")
        state = self.command("status")
        record = tb.task(self.w, task_id)
        # The native interruption budget permits durable queueing when the
        # real CLI exceeds one second. Either path must preserve the observation.
        self.assertIn("unreviewed", record.get("implementationNotes", "") + "\n".join(state["pending"]))
        if state["pending"]:
            self.assertIn("queued locally", result["systemMessage"])
        self.assertEqual(record["status"], "In Progress")
        self.checkpoint("Blocked")
        self.assertEqual(self.command("status")["pending"], [])
        self.assertIn("unreviewed", tb.task(self.w, task_id)["implementationNotes"])

    def test_unknown_internal_agent_does_not_dirty_board(self):
        self.start()
        self.checkpoint()
        self.event("SubagentStop", agent_id="suggestion", agent_type="")
        self.assertEqual(self.event("Stop"), {})

    def test_codex_contract_and_child_session_alias(self):
        self.sid = "codex-main"
        payload = dict(cwd=str(self.board), session_id=self.sid)
        out = tb.hook(dict(payload, hook_event_name="SessionStart"), "codex")
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.token = tb.token_for("codex", self.sid)
        self.start()
        tb.hook(dict(payload, hook_event_name="PreToolUse", tool_name="collaborationspawn_agent", tool_input={"task_name": "parser"}), "codex")
        tb.hook(dict(payload, hook_event_name="SubagentStart", agent_id="codex-child", agent_type="worker"), "codex")
        out = tb.hook(dict(payload, session_id="codex-child", hook_event_name="SessionStart"), "codex")
        self.assertIn("Worker for", out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual(tb.hook(dict(payload, session_id="codex-child", hook_event_name="Stop"), "codex"), {})

    def test_followup_invalidates_old_return_and_preserves_fast_new_return(self):
        task_id = self.start()
        self.command("backlog", "--", "task", "edit", task_id, "--check-ac", "1", "--check-dod", "1", "--plain")
        self.event("PreToolUse", tool_name="spawn_agent", tool_use_id="spawn")
        self.event("SubagentStart", agent_id="worker-a", turn_id="old")
        self.event("SubagentStop", agent_id="worker-a", turn_id="old")
        self.checkpoint()
        followup = dict(tool_name="collaborationfollowup_task", tool_input={"target": "worker-a"}, tool_use_id="followup")
        self.event("PreToolUse", **followup)
        self.assertEqual(self.command("status")["workers"]["worker-a"]["state"], "unknown")
        with self.assertRaisesRegex(ValueError, "worker-a"):
            self.checkpoint("Done")
        # Replayed old return is not the new task's return.
        self.event("SubagentStop", agent_id="worker-a", turn_id="old")
        self.assertEqual(self.command("status")["workers"]["worker-a"]["state"], "unknown")
        # A follow-up may have no SubagentStart; the new return can beat PostToolUse.
        self.event("SubagentStop", agent_id="worker-a", turn_id="new")
        self.event("PostToolUse", **followup)
        self.event("PreToolUse", **followup)  # duplicate receipt must not reset it
        self.assertEqual(self.command("status")["workers"]["worker-a"]["state"], "returned")
        self.assertEqual(self.command("status")["unresolved_followups"]["worker-a"]["state"], "unknown")
        with self.assertRaisesRegex(ValueError, "worker-a"):
            self.checkpoint("Done")
        self.command("reconcile-worker", "--agent", "worker-a", "--state", "returned", "--evidence", "Lead reviewed the follow-up return")
        self.checkpoint("Done")

    def test_unseen_old_return_and_replay_after_eviction_cannot_settle_followup(self):
        task_id = self.start()
        self.command("backlog", "--", "task", "edit", task_id, "--check-ac", "1", "--check-dod", "1", "--plain")
        self.event("PreToolUse", tool_name="spawn_agent")
        self.event("SubagentStart", agent_id="worker-a", turn_id="old")
        # First old return arrives late, after the follow-up request.
        self.event("PreToolUse", tool_name="followup_task", tool_input={"target": "worker-a"}, tool_use_id="followup-1")
        self.event("SubagentStop", agent_id="worker-a", turn_id="old")
        self.assertEqual(self.command("status")["workers"]["worker-a"]["state"], "returned")
        with self.assertRaisesRegex(ValueError, "worker-a"):
            self.checkpoint("Done")
        self.command("reconcile-worker", "--agent", "worker-a", "--state", "returned", "--evidence", "Lead separately reviewed first follow-up")
        self.event("PreToolUse", tool_name="followup_task", tool_input={"target": "worker-a"}, tool_use_id="followup-2")
        for i in range(300):
            self.event("PostToolUse", tool_name="Read", tool_use_id="evict-" + str(i))
        self.assertNotIn("SubagentStop:worker-a:old", self.command("status")["seen"])
        self.event("SubagentStop", agent_id="worker-a", turn_id="old")
        self.assertEqual(self.command("status")["workers"]["worker-a"]["state"], "returned")
        self.assertEqual(self.command("status")["unresolved_followups"]["worker-a"]["state"], "unknown")
        with self.assertRaisesRegex(ValueError, "worker-a"):
            self.checkpoint("Done")
        self.checkpoint()
        with self.assertRaisesRegex(ValueError, "worker-a"):
            self.command("release")
        with self.assertRaisesRegex(ValueError, "worker-a"):
            self.start()

    def test_followup_without_distinct_lifecycle_receipt_requires_reconcile(self):
        self.start()
        self.event("PreToolUse", tool_name="Agent")
        self.event("SubagentStart", agent_id="worker-a")
        self.event("SubagentStop", agent_id="worker-a")
        self.event("PreToolUse", tool_name="followup_task", tool_input={"target": "worker-a"})
        self.event("SubagentStop", agent_id="worker-a")
        self.assertEqual(self.command("status")["workers"]["worker-a"]["state"], "unknown")
        self.command("reconcile-worker", "--agent", "worker-a", "--state", "returned", "--evidence", "Lead reviewed the follow-up return")
        self.assertEqual(self.command("status")["workers"]["worker-a"]["state"], "returned")

    def test_canonical_followup_target_is_not_guessed_to_be_a_worker_id(self):
        task_id = self.start()
        self.command("backlog", "--", "task", "edit", task_id, "--check-ac", "1", "--check-dod", "1", "--plain")
        self.event("PreToolUse", tool_name="spawn_agent", tool_input={"task_name": "parser"})
        self.event("SubagentStart", agent_id="worker-uuid")
        self.event("SubagentStop", agent_id="worker-uuid")
        target = "/root/parser"
        self.event("PreToolUse", tool_name="collaborationfollowup_task", tool_input={"target": target})
        self.event("SubagentStop", agent_id=target)
        state = self.command("status")
        self.assertEqual(set(state["workers"]), {"worker-uuid"})
        self.assertEqual(state["workers"]["worker-uuid"]["state"], "returned")
        self.assertEqual(state["unresolved_followups"][target]["state"], "unknown")
        aliases = tb.read_json(self.board / ".task-board/aliases.json")
        self.assertNotIn(tb.token_for("claude", target), aliases)
        with self.assertRaisesRegex(ValueError, "/root/parser"):
            self.checkpoint("Done")
        self.checkpoint()
        with self.assertRaisesRegex(ValueError, "/root/parser"):
            self.command("release")
        with self.assertRaisesRegex(ValueError, "/root/parser"):
            self.start()
        with self.assertRaisesRegex(ValueError, "Unknown worker"):
            self.command("reconcile-worker", "--agent", "parser", "--state", "returned", "--evidence", "Not an observed target")
        self.command("reconcile-worker", "--agent", target, "--state", "unknown", "--evidence", "Still unavailable")
        with self.assertRaisesRegex(ValueError, "/root/parser"):
            self.checkpoint("Done")
        self.command("reconcile-worker", "--agent", target, "--state", "returned", "--evidence", "Lead reviewed exact target return")
        self.checkpoint("Done")
        self.assertNotIn(tb.token_for("claude", target), tb.read_json(self.board / ".task-board/aliases.json"))

    def test_missing_target_and_old_state_remain_visible_without_aliases(self):
        self.start()
        state = self.command("status")
        state.pop("unresolved_followups")  # state written by the previous version
        tb.save(self.w, state)
        self.event("PreToolUse", tool_name="collaborationfollowup_task", tool_input={})
        self.event("SubagentStart", agent_id="unrelated")
        state = self.command("status")
        self.assertEqual(state["workers"], {})
        self.assertFalse(state["dispatch_seen"])
        self.assertEqual(state["unresolved_followups"]["<missing target>"]["state"], "unknown")
        self.assertFalse((self.board / ".task-board/aliases.json").exists())
        self.command("reconcile-worker", "--agent", "<missing target>", "--state", "failed", "--evidence", "Request had no target")
        self.checkpoint()
        self.start()
        self.assertEqual(self.command("status")["unresolved_followups"], {})

    def test_unrelated_repo_is_untouched_and_worktree_routes(self):
        other = self.root / "other"
        other.mkdir()
        subprocess.run(["git", "init", "-q", str(other)], check=True)
        self.assertIsNone(tb.workspace(other))
        subprocess.run(["git", "-C", str(self.board), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "init"], check=True)
        wt = self.root / "worktree"
        subprocess.run(["git", "-C", str(self.board), "worktree", "add", "--detach", str(wt)], check=True, capture_output=True)
        self.assertEqual(tb.workspace(wt)["board"], str(self.board))

    def test_malformed_json_is_visible_and_does_not_hang(self):
        p = subprocess.run([sys.executable, str(SCRIPT), "hook", "--harness", "claude"], input="{bad", text=True, capture_output=True)
        self.assertEqual(p.returncode, 0)
        self.assertIn("not enforced", json.loads(p.stdout)["systemMessage"])

    def test_concurrent_events_do_not_lose_updates(self):
        self.start()
        children = []
        for i in range(10):
            payload = dict(cwd=str(self.board), session_id=self.sid, hook_event_name="PostToolUse", tool_name="Read", tool_use_id="read-" + str(i))
            proc = subprocess.Popen([sys.executable, str(SCRIPT), "hook", "--harness", "claude"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            proc.stdin.write(json.dumps(payload))
            proc.stdin.close()
            children.append(proc)
        for proc in children:
            proc.wait(timeout=10)
            output = json.loads(proc.stdout.read())
            proc.stdout.close()
            proc.stderr.close()
            self.assertEqual(output, {})
        self.assertEqual(self.command("status")["generation"], 11)

    def test_missing_binary_reports_failure_and_preserves_pending_note(self):
        self.start()
        self.w["backlog_bin"] = str(self.root / "missing")
        tb.atomic_json(self.reg, {"workspaces": [self.w]})
        self.event("Interrupt")
        self.assertEqual(len(self.command("status")["pending"]), 1)

    def test_installer_preserves_existing_hooks_and_is_idempotent(self):
        home = self.root / "home"
        settings = home / ".claude/settings.json"
        original = {"env": {"SAMPLE_SETTING": "preserve"}, "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "existing-guard"}]}]}}
        tb.atomic_json(settings, original)
        command = [sys.executable, str(SCRIPT.parent / "install.py"), "--home", str(home), "--id", "test", "--board", str(self.board), "--repo", str(self.board), "--backlog", BACKLOG, "--apply"]
        for _ in range(2):
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        actual = tb.read_json(settings)
        self.assertEqual(actual["env"], original["env"])
        self.assertEqual(actual["hooks"]["PreToolUse"][0], original["hooks"]["PreToolUse"][0])
        self.assertEqual(len(actual["hooks"]["PreToolUse"]), 2)
        self.assertTrue((home / ".local/bin/task-board").is_symlink())

    def test_reconcile_missing_worker_does_not_guess_success(self):
        self.start()
        self.event("PreToolUse", tool_name="Agent", tool_input={"description": "Check"})
        self.event("SubagentStart", agent_id="worker-a", agent_type="Explore")
        self.command("reconcile-worker", "--agent", "worker-a", "--state", "unknown", "--evidence", "No process or report available")
        self.assertEqual(self.command("status")["workers"]["worker-a"]["state"], "unknown")

    def test_switch_before_checkpoint_does_not_create_orphan_task(self):
        self.start()
        with self.assertRaises(ValueError):
            self.start()
        listing = tb.backlog(self.w, ["task", "list", "--plain"])
        self.assertNotIn("T-2", listing)

    def test_done_releases_ownership_for_next_lead(self):
        task_id = self.start()
        self.command("backlog", "--", "task", "edit", task_id, "--check-ac", "1", "--check-dod", "1", "--plain")
        self.checkpoint("Done")
        self.assertTrue(self.command("status")["released"])
        self.assertEqual(self.event("Stop"), {})
        self.sid = "next-lead"
        self.event("SessionStart")
        self.command("start", "--task", task_id, token=tb.token_for("claude", self.sid))
        # The previous lead must not reclaim a released task just because its
        # new owner changed it between the final checkpoint and the Stop hook.
        self.sid = "session-a"
        self.assertEqual(self.event("Stop"), {})

    def test_binding_records_actual_checkout(self):
        task_id = self.start()
        self.assertIn(str(self.board), tb.task(self.w, task_id)["implementationNotes"])

    def test_new_task_does_not_inherit_old_workers(self):
        self.start()
        self.event("PreToolUse", tool_name="Agent", tool_input={"description": "Check"})
        self.event("SubagentStart", agent_id="old-worker", agent_type="Explore")
        self.event("SubagentStop", agent_id="old-worker", agent_type="Explore")
        self.checkpoint()
        self.start()
        self.assertEqual(self.command("status")["workers"], {})


if __name__ == "__main__":
    import sys
    unittest.main()
