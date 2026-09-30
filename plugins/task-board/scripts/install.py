#!/usr/bin/env python3
"""Install a local helper and merge native hook registrations without changing permissions."""
import argparse
import datetime
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from task_board import atomic_json, common_dir, read_json

COMMON_EVENTS = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "SubagentStart", "SubagentStop", "Stop", "SessionEnd"]


def merge_hooks(data, command, events):
    hooks = data.setdefault("hooks", {})
    for event in events:
        groups = hooks.setdefault(event, [])
        if any(h.get("command") == command for g in groups for h in g.get("hooks", [])):
            continue
        groups.append({"hooks": [{"type": "command", "command": command,
                                   "timeout": 3 if event in {"SessionEnd", "Interrupt"} else 15}]})
    return data


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--board", type=Path, required=True)
    p.add_argument("--id", required=True)
    p.add_argument("--repo", type=Path, action="append", required=True)
    p.add_argument("--instructions", type=Path, action="append", default=[])
    p.add_argument("--backlog", type=Path, required=True)
    p.add_argument("--mode", choices=["observe", "enforce"], default="observe")
    p.add_argument("--home", type=Path, default=Path.home(), help="Alternate installation home, useful for isolated tests")
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    board = args.board.resolve()
    if not (board / "backlog.config.yml").is_file():
        p.error("Board has no backlog.config.yml")
    if not args.backlog.is_file():
        p.error("Backlog executable not found")
    version = subprocess.run([str(args.backlog), "--version"], capture_output=True, text=True, timeout=3)
    if version.returncode or version.stdout.strip() != "1.52.0":
        p.error("This adapter requires validated Backlog.md 1.52.0")
    for path in args.instructions:
        if not path.is_file():
            p.error("Instruction file does not exist: " + str(path))
    repositories = set(common_dir(path.resolve()) for path in args.repo)
    if None in repositories:
        p.error("Every --repo must be an existing Git checkout")
    repositories = sorted(repositories)
    registry = args.home / ".config/task-board/workspaces.json"
    existing = read_json(registry, {"workspaces": []})
    entry = {"id": args.id, "board": str(board), "git_common_dirs": repositories,
             "instruction_files": [str(path.resolve()) for path in args.instructions],
             "backlog_bin": str(args.backlog.resolve()), "mode": args.mode}
    for w in existing["workspaces"]:
        if w["id"] != args.id and set(w["git_common_dirs"]) & set(repositories):
            p.error("Repository already registered to another board")
    existing["workspaces"] = [w for w in existing["workspaces"] if w["id"] != args.id] + [entry]
    helper = args.home / ".local/bin/task-board"
    source = Path(__file__).resolve().parent / "task_board.py"
    if helper.exists() and not (helper.is_symlink() and helper.resolve() == source):
        p.error("Refusing to replace an unrelated task-board executable")
    updates = {registry: existing}
    for harness, settings in [("claude", args.home / ".claude/settings.json"), ("codex", args.home / ".codex/hooks.json")]:
        data = read_json(settings, {})
        events = COMMON_EVENTS + (["StopFailure", "PostToolUseFailure"] if harness == "claude" else ["Interrupt"])
        command = shlex.quote(str(helper)) + " hook --harness " + harness
        updates[settings] = merge_hooks(data, command, events)
    plan = {"helper": str(helper), "source": str(source), "files": [str(path) for path in updates],
            "workspace": entry, "apply": args.apply,
            "trust": "Codex hooks require native review/trust in /hooks. No trust or permission bypass is installed."}
    if args.apply:
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = args.home / ".local/state/task-board/install-backups" / stamp
        backup.mkdir(parents=True, exist_ok=True)
        manifest = []
        for path in updates:
            prior = backup / (str(len(manifest)) + ".json")
            if path.exists():
                shutil.copy2(path, prior)
            manifest.append({"path": str(path), "backup": str(prior) if path.exists() else None})
        atomic_json(backup / "manifest.json", manifest)
        helper.parent.mkdir(parents=True, exist_ok=True)
        if not helper.exists():
            helper.symlink_to(source)
        source.chmod(source.stat().st_mode | 0o111)
        for path, data in updates.items():
            atomic_json(path, data)
        plan["backup"] = str(backup)
    print(json.dumps(plan, indent=2))


if __name__ == "__main__":
    main()
