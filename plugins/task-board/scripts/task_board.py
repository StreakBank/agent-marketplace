#!/usr/bin/env python3
"""Local Backlog.md lifecycle guard. Standard library only; no model/API calls."""
import argparse
import contextlib
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time

VERSION = "0.1.1"
REPAIR = "Task board update required. "
STATUSES = ["To Do", "In Progress", "Blocked", "Review", "Done"]
DELEGATE = {"Agent", "Workflow", "spawn_agent", "followup_task"}
EDIT = {"Edit", "Write", "apply_patch", "NotebookEdit"}


def normalize_tool_name(name):
    """Keep dotted/plain names and normalize only observed native aliases."""
    if not isinstance(name, str):
        return ""
    name = name.rsplit(".", 1)[-1]
    return {"collaborationspawn_agent": "spawn_agent",
            "collaborationfollowup_task": "followup_task",
            "collaborationsend_message": "send_message"}.get(name, name)


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def read_json(path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".board-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def registry_path():
    return Path(os.environ.get("TASK_BOARD_REGISTRY", str(Path.home() / ".config/task-board/workspaces.json")))


def common_dir(cwd):
    p = subprocess.run(["git", "-C", str(cwd), "rev-parse", "--git-common-dir"], capture_output=True, text=True, timeout=2)
    if p.returncode:
        return None
    return str((Path(cwd) / p.stdout.strip()).resolve())


def workspace(cwd=None, token=None):
    entries = read_json(registry_path(), {"workspaces": []})["workspaces"]
    if token:
        if not re.fullmatch(r"[a-f0-9]{24}", token):
            raise ValueError("Invalid session token")
        matches = [w for w in entries if (Path(w["board"]) / ".task-board/sessions" / (token + ".json")).exists()]
    else:
        common = common_dir(Path(cwd or os.getcwd()).resolve())
        matches = [w for w in entries if common and common in w["git_common_dirs"]]
    if len(matches) > 1:
        raise ValueError("Ambiguous board registration; repair the workspace registry")
    return matches[0] if matches else None


def token_for(harness, session):
    return hashlib.sha256((harness + ":" + session).encode()).hexdigest()[:24]


def state_path(w, token):
    return Path(w["board"]) / ".task-board/sessions" / (token + ".json")


@contextlib.contextmanager
def lock(w):
    path = Path(w["board"]) / ".task-board/lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        deadline = time.monotonic() + 1.5
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise RuntimeError("Board is busy; retry the update")
                time.sleep(0.02)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def backlog(w, args, expect_json=False, timeout=8):
    env = dict(os.environ, BACKLOG_CWD=w["board"])
    p = subprocess.run([w["backlog_bin"]] + args, cwd=w["board"], env=env,
                       capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError("Backlog failed: " + (p.stderr or p.stdout).strip()[:500])
    return json.loads(p.stdout) if expect_json else p.stdout


def task(w, task_id):
    result = backlog(w, ["task", task_id, "--json"], True)
    if result.get("kind") != "task-view" or not isinstance(result.get("task"), dict):
        raise ValueError("Unsupported Backlog JSON schema; expected task-view")
    return result["task"]


def task_hash(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def journal(w, state, event, **fields):
    entry = dict(at=now(), session=state["token"], task=state.get("task"), event=event, **fields)
    path = Path(w["board"]) / ".task-board/events.jsonl"
    with path.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def save(w, s):
    s["last_seen"] = now()
    atomic_json(state_path(w, s["token"]), s)


def context(event, message):
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": message}}


def instructions(w, s, child=False):
    paths = ", ".join(w.get("instruction_files", []))
    base = "Shared task board: " + w["board"] + ". "
    if child:
        return base + "Worker for " + str(s.get("task") or "unbound parent") + ". Return accomplishments, changed paths, evidence, blockers and next action to the lead. Do not create a separate task or checkpoint the lead's record. Read applicable workspace instructions: " + paths
    cmd = "task-board"
    return (base + "Maintain it automatically; do not ask the user to request bookkeeping. "
            "Session token: " + s["token"] + ". Bound task: " + str(s.get("task") or "none") + ". "
            "Before working, reuse the relevant task with `" + cmd + " start --session " + s["token"] +
            " --task TASK-ID`, or create one with `" + cmd + " start --session " + s["token"] +
            " --title TITLE --description SCOPE --ac CRITERION`. Do not claim another lead's active task. "
            "Keep assignments and results in that record. At milestones and before your final response, run `" + cmd +
            " checkpoint --session " + s["token"] + " --status 'In Progress' --summary RESULT --evidence CHECKS --next NEXT-ACTION`. "
            "Choose the truthful status; Done requires checked acceptance criteria and definition of done. "
            "This checkpoint must follow your last work tool. For ordinary task edits use `task-board backlog --session " +
            s["token"] + " -- task edit TASK-ID ...`. A pure conversational reply requires no new task. "
            "Pending interruption observations: " + str(len(s["pending"])) + ". "
            "Read applicable workspace instructions: " + paths)


def board_command(data):
    """Only exempt a single direct helper invocation, never chained shell work."""
    inp = data.get("tool_input") or {}
    command = inp.get("command", inp.get("cmd", ""))
    for _ in range(3):
        if isinstance(command, list):
            words = command
            if not all(isinstance(w, str) for w in words):
                return False
        elif isinstance(command, str):
            quote = None
            escaped = False
            for char in command:
                if escaped:
                    escaped = False
                    continue
                if char == "\\" and quote != "'":
                    escaped = True
                    continue
                if char in {"'", '"'}:
                    if quote == char:
                        quote = None
                    elif quote is None:
                        quote = char
                    continue
                if quote != "'" and char in {"$", "`"}:
                    return False
                if quote is None and char in "\n;|&<>()":
                    return False
            try:
                words = shlex.split(command)
            except ValueError:
                return False
        else:
            return False
        if len(words) == 3 and Path(words[0]).name in {"sh", "bash", "zsh"} and words[1] in {"-c", "-lc"}:
            command = words[2]
            continue
        return bool(words) and Path(words[0]).name == "task-board"
    return False


def new_state(token, harness, sid):
    return dict(token=token, harness=harness, session_id=sid, generation=0,
                checkpoint_generation=-1, repairs=0, workers={}, unresolved_followups={}, pending=[], seen=[])


def require_reconciled_followups(s):
    targets = [target for target, observation in s.get("unresolved_followups", {}).items()
               if observation["state"] == "unknown"]
    if targets:
        raise ValueError("Unresolved follow-up target(s): " + ", ".join(targets) +
                         ". Use reconcile-worker --agent TARGET --state STATE --evidence REASON; "
                         "native lifecycle returns do not establish follow-up completion.")


def observe_followup(s, target):
    # PreToolUse runs before a fast worker can return. Do not reset its state
    # in PostToolUse: that would overwrite a fresh SubagentStop observation.
    # A request is not proof of resumed execution, hence unknown, not started.
    # Even a known UUID does not correlate a returned lifecycle event to this
    # request. An unseen delayed old return (or a replay outside the dedup window)
    # must not clear the follow-up barrier. Only explicit reconciliation does.
    s.setdefault("unresolved_followups", {})[target] = {"state": "unknown", "at": now()}
    if target in s["workers"]:
        s["workers"][target].update(state="unknown", at=now())
        return True
    # The hook payload may expose only a canonical task name, while lifecycle
    # events expose a UUID. Keep the observation separate; never guess an alias.
    return False


def note(w, s, message):
    if s.get("task"):
        try:
            backlog(w, ["task", "edit", s["task"], "--append-notes", message, "--plain"])
        except (OSError, RuntimeError, subprocess.SubprocessError):
            s["pending"].append(message)
            save(w, s)
            raise
    else:
        s["pending"].append(message)


def hook(data, harness):
    event = data.get("hook_event_name", "")
    w = workspace(data.get("cwd"))
    if not w:
        return {}
    sid = data.get("session_id")
    if not isinstance(sid, str) or not sid:
        raise ValueError("Hook has no session_id")
    raw_token = token_for(harness, sid)
    with lock(w):
        aliases_path = Path(w["board"]) / ".task-board/aliases.json"
        aliases = read_json(aliases_path, {})
        token = aliases.get(raw_token, raw_token)
        s = read_json(state_path(w, token), new_state(token, harness, sid))
        agent = str(data.get("agent_id") or "")
        child = token != raw_token or (agent in s["workers"] and event not in {"SubagentStart", "SubagentStop"})
        if not child and event not in {"SubagentStart", "SubagentStop"}:
            s["checkout"] = data.get("cwd", s.get("checkout"))
        tool_name = normalize_tool_name(data.get("tool_name", ""))
        # Replayed lifecycle/tool events must not add duplicate board entries.
        dedup = None
        if data.get("tool_use_id"):
            dedup = event + ":" + data["tool_use_id"]
        elif event in {"SubagentStart", "SubagentStop"} and agent:
            dedup = event + ":" + agent + ":" + str(data.get("turn_id", data.get("prompt_id", "")))
        if dedup and dedup in s["seen"]:
            return {}
        result = {}
        if event in {"SessionStart", "UserPromptSubmit"}:
            if event == "UserPromptSubmit" and not child:
                prompt = data.get("prompt", "")
                if not prompt.startswith(REPAIR):
                    s["repairs"] = 0
            result = context(event, instructions(w, s, child))
            journal(w, s, event, source=data.get("source"))
        elif event == "PreToolUse":
            if not child and (not s.get("task") or s.get("released")) and tool_name in DELEGATE | EDIT:
                message = "Bind or create the board task before editing or dispatch. " + instructions(w, s)
                if w.get("mode") == "enforce":
                    result = {"hookSpecificOutput": {"hookEventName": event, "permissionDecision": "deny",
                              "permissionDecisionReason": message}}
                else:
                    result = context(event, message)
                # Do not deduplicate denied retries.
                dedup = None
            elif tool_name in DELEGATE and not child:
                inp = data.get("tool_input") or {}
                label = str(inp.get("description") or inp.get("task_name") or inp.get("name") or inp.get("target") or "unnamed assignment")[:180]
                s["generation"] += 1
                if tool_name == "followup_task":
                    target = inp.get("target")
                    target = target if isinstance(target, str) and target else "<missing target>"
                    known = observe_followup(s, target)
                    journal(w, s, "followup_requested", target=target, known_worker=known)
                else:
                    s["dispatch_seen"] = True
                note(w, s, "Runtime observation " + now() + ": dispatch requested via " + tool_name + " — " + label + ". Outcome pending; lead owns verification.")
                journal(w, s, "dispatch_requested", tool=tool_name, native_tool=data.get("tool_name"), label=label)
        elif event in {"PostToolUse", "PostToolUseFailure"}:
            if not board_command(data):
                s["generation"] += 1
                journal(w, s, event, tool=tool_name, worker=agent or None)
        elif event == "SubagentStart":
            if not s.get("task") or not s.get("dispatch_seen") or not agent:
                journal(w, s, "unassigned_agent_observed", agent=agent)
                save(w, s)
                return {}
            s["workers"][agent] = {"state": "started", "at": now(), "type": data.get("agent_type")}
            aliases[token_for(harness, agent)] = token
            atomic_json(aliases_path, aliases)
            s["generation"] += 1
            note(w, s, "Runtime observation " + now() + ": agent " + agent + " (" + str(data.get("agent_type") or "unknown type") + ") started; accomplishment pending.")
            journal(w, s, event, agent=agent)
            result = context(event, instructions(w, s, True))
        elif event == "SubagentStop":
            if agent not in s["workers"]:
                journal(w, s, "unassigned_return_observed", agent=agent)
                save(w, s)
                return {}
            s["workers"][agent] = {"state": "returned", "at": now(), "type": data.get("agent_type")}
            s["generation"] += 1
            note(w, s, "Runtime observation " + now() + ": agent " + agent + " returned. Lead review pending; this is not completion evidence.")
            journal(w, s, event, agent=agent)
        elif event in {"Interrupt", "StopFailure", "SessionEnd"}:
            # Keep the board honest within the short native end/interrupt budget.
            journal(w, s, event, reason=data.get("error") or data.get("reason"))
            if s.get("task") and s["generation"] != s["checkpoint_generation"]:
                message = "Runtime observation " + now() + ": " + event + "; work since the last checkpoint remains unreviewed."
                try:
                    backlog(w, ["task", "edit", s["task"], "--append-notes", message, "--plain"], timeout=1)
                except (OSError, RuntimeError, subprocess.SubprocessError):
                    s["pending"].append(message)
                    result = {"systemMessage": "Task board interruption note queued locally; reconcile at the next checkpoint."}
        elif event == "Stop" and not child:
            dirty = s["generation"] != s["checkpoint_generation"] and s["generation"] > 0
            if s.get("task") and s.get("checkpoint_hash") and not s.get("released"):
                dirty = dirty or s["checkpoint_hash"] != task_hash(task(w, s["task"]))
            if dirty:
                journal(w, s, "handoff_missing", mode=w.get("mode", "observe"))
                if w.get("mode") == "enforce" and s["repairs"] < 2:
                    s["repairs"] += 1
                    result = {"decision": "block", "reason": REPAIR + instructions(w, s)}
                else:
                    result = {"systemMessage": "Task board checkpoint is missing or stale. Work is not verified; repair with task-board checkpoint. Automatic retries stopped."}
            else:
                s["repairs"] = 0
                journal(w, s, "handoff_current")
        if dedup:
            s["seen"] = (s["seen"] + [dedup])[-300:]
        save(w, s)
        return result


def session_command(args):
    w = workspace(token=args.session)
    if not w:
        raise ValueError("Session not registered; open a fresh harness session in a registered repository")
    with lock(w):
        s = read_json(state_path(w, args.session))
        if args.command == "start":
            switching = not args.task or args.task.upper() != str(s.get("task", "")).upper()
            if switching and s.get("task") and s["generation"] != s["checkpoint_generation"]:
                raise ValueError("Checkpoint the current task before binding a different one")
            if switching:
                require_reconciled_followups(s)
            if switching and any(v["state"] in {"started", "unknown"} for v in s["workers"].values()):
                raise ValueError("Reconcile outstanding workers before changing tasks")
            if args.task:
                record = task(w, args.task)
                task_id = record["id"]
            else:
                if not args.title or not args.description or not args.ac:
                    raise ValueError("Creation requires --title, --description and at least one --ac")
                created = backlog(w, ["task", "create", args.title, "--description", args.description,
                                      "--status", "In Progress", "--assignee", "@" + s["harness"]] +
                                  (["--project", args.project] if args.project else []) +
                                  [x for criterion in args.ac for x in ["--ac", criterion]] + ["--plain"])
                match = re.search(r"^Task ([A-Za-z][A-Za-z0-9_-]*-\d+) -", created, re.M)
                if not match:
                    raise RuntimeError("Task created, but ID not recognized. Inspect backlog before retrying.")
                task_id = match.group(1)
            if s.get("task") and s["task"] != task_id and s["generation"] != s["checkpoint_generation"]:
                raise ValueError("Checkpoint the current task before binding a different one")
            for path in state_path(w, args.session).parent.glob("*.json"):
                other = read_json(path)
                if other["token"] != s["token"] and other.get("task") == task_id and not other.get("released"):
                    raise ValueError("Task already bound to another lead. Use task-board release for an explicit handoff.")
            if s.get("task") != task_id:
                s["workers"] = {}
                s["unresolved_followups"] = {}
                s["dispatch_seen"] = False
                s.pop("checkpoint_hash", None)
            s["task"] = task_id
            s["released"] = False
            s["generation"] += 1
            message = "Lead bound: " + s["harness"] + ", session " + s["session_id"] + ", checkout " + str(s.get("checkout") or "unavailable") + ", observed " + now() + ". Board maintained automatically."
            if s["pending"]:
                message += "\n" + "\n".join(s["pending"])
            backlog(w, ["task", "edit", task_id, "--status", "In Progress", "--append-notes", message, "--plain"])
            s["pending"] = []
            journal(w, s, "bound")
            save(w, s)
            return {"task": task_id, "session": s["token"], "board": w["board"]}
        if args.command == "status":
            return s
        if args.command == "release":
            if s["generation"] != s["checkpoint_generation"]:
                raise ValueError("Checkpoint before releasing task ownership")
            require_reconciled_followups(s)
            if any(v["state"] in {"started", "unknown"} for v in s["workers"].values()):
                raise ValueError("Reconcile outstanding workers before releasing task ownership")
            s["released"] = True
            journal(w, s, "released")
            save(w, s)
            return {"released": s.get("task")}
        if not s.get("task") or s.get("released"):
            raise ValueError("Bind an active task first")
        if args.command == "reconcile-worker":
            observation = s["workers"].get(args.agent)
            known_worker = observation is not None
            if observation is None:
                observation = s.get("unresolved_followups", {}).get(args.agent)
            if observation is None:
                raise ValueError("Unknown worker ID")
            observation.update(state=args.state, at=now())
            if args.agent in s.get("unresolved_followups", {}):
                s["unresolved_followups"][args.agent].update(state=args.state, at=now())
            s["generation"] += 1
            subject = "worker " if known_worker else "unresolved follow-up target "
            note(w, s, "Lead reconciled " + subject + args.agent + " as " + args.state + ": " + args.evidence)
            journal(w, s, "worker_reconciled", agent=args.agent, worker_state=args.state, known_worker=known_worker)
            save(w, s)
            return {"agent": args.agent, "state": args.state}
        if args.command == "backlog":
            rest = args.args[1:] if args.args and args.args[0] == "--" else args.args
            output = backlog(w, rest)
            s["generation"] += 1
            save(w, s)
            return {"output": output}
        if args.command == "checkpoint":
            record = task(w, s["task"])
            if args.status == "Done":
                criteria = record.get("acceptanceCriteria", [])
                dod = record.get("definitionOfDone", [])
                if not criteria or not all(c.get("checked") for c in criteria + dod):
                    raise ValueError("Done requires checked acceptance criteria and definition of done. Review evidence first.")
                require_reconciled_followups(s)
                outstanding = [agent for agent, observation in s["workers"].items()
                               if observation["state"] in {"started", "unknown"}]
                if outstanding:
                    raise ValueError("Observed workers have no return: " + ", ".join(outstanding) + ". Reconcile them before Done.")
            message = ("Checkpoint " + now() + "\nAccomplished: " + args.summary +
                       "\nEvidence: " + args.evidence + "\nNext action: " + args.next)
            if s["pending"]:
                message += "\n" + "\n".join(s["pending"])
            cli = ["task", "edit", s["task"], "--status", args.status, "--append-notes", message, "--plain"]
            if args.status == "Done":
                cli += ["--final-summary", args.summary + "\nEvidence: " + args.evidence + "\nNext action: " + args.next]
            backlog(w, cli)
            s["pending"] = []
            s["checkpoint_generation"] = s["generation"]
            s["checkpoint_hash"] = task_hash(task(w, s["task"]))
            s["repairs"] = 0
            s["released"] = args.status == "Done"
            journal(w, s, "checkpoint", status=args.status)
            save(w, s)
            return {"task": s["task"], "status": args.status, "checkpoint": "current"}


def doctor():
    results = []
    for w in read_json(registry_path(), {"workspaces": []})["workspaces"]:
        versions = subprocess.run([w["backlog_bin"], "--version"], capture_output=True, text=True, timeout=3)
        sessions = []
        for path in (Path(w["board"]) / ".task-board/sessions").glob("*.json"):
            s = read_json(path)
            if s.get("task") and not s.get("released"):
                sessions.append({"session": s["token"], "task": s["task"], "last_seen": s.get("last_seen"),
                                 "uncheckpointed": s["generation"] != s["checkpoint_generation"],
                                 "pending_observations": len(s["pending"])})
        results.append({"workspace": w["id"], "mode": w["mode"], "board": w["board"],
                        "backlog_version": versions.stdout.strip(),
                        "registered_repositories": len(w["git_common_dirs"]), "active_bindings": sessions})
    return {"version": VERSION, "workspaces": results,
            "coverage": "Configuration only. Native hook trust/loading must be verified in each harness."}


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="command", required=True)
    h = sub.add_parser("hook")
    h.add_argument("--harness", choices=["claude", "codex"], required=True)
    sub.add_parser("doctor")
    for name in ["start", "status", "release", "checkpoint", "backlog", "reconcile-worker"]:
        q = sub.add_parser(name)
        q.add_argument("--session", required=True)
        if name == "start":
            q.add_argument("--task")
            q.add_argument("--title")
            q.add_argument("--description")
            q.add_argument("--project")
            q.add_argument("--ac", action="append", default=[])
        elif name == "checkpoint":
            q.add_argument("--status", choices=STATUSES, required=True)
            for field in ["summary", "evidence", "next"]:
                q.add_argument("--" + field, required=True)
        elif name == "backlog":
            q.add_argument("args", nargs=argparse.REMAINDER)
        elif name == "reconcile-worker":
            q.add_argument("--agent", required=True)
            q.add_argument("--state", choices=["failed", "cancelled", "unknown", "returned"], required=True)
            q.add_argument("--evidence", required=True)
    return p


def main():
    args = parser().parse_args()
    try:
        if args.command == "hook":
            result = hook(json.load(sys.stdin), args.harness)
        elif args.command == "doctor":
            result = doctor()
        else:
            result = session_command(args)
        print(json.dumps(result))
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        if args.command == "hook":
            print(json.dumps({"systemMessage": "Task board hook failed; maintenance is not enforced for this event: " + str(exc)[:500]}))
        else:
            print("task-board: " + str(exc), file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
