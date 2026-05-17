"""Codex command construction and JSON event parsing."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set

from codex_lark_minimal.config import Config
from codex_lark_minimal.redaction import redact

UUIDISH_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
ROLLOUT_RE = re.compile(r"-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$")


def build_exec_command(config: Config, workspace: Path) -> List[str]:
    # codex 0.130+: --sandbox / --ask-for-approval / --model / --profile are
    # top-level and must come BEFORE the `exec` subcommand. --color and -C
    # belong to `exec`. Wrong order → "unexpected argument", exit 2.
    command = [config.codex_bin]
    command.extend(["--sandbox", config.codex_sandbox])
    command.extend(["--ask-for-approval", config.codex_approval])
    if config.codex_model:
        command.extend(["--model", config.codex_model])
    if config.codex_profile:
        command.extend(["--profile", config.codex_profile])
    command.extend([
        "exec",
        "--json",
        "-C",
        str(workspace),
        "--color",
        "never",
    ])
    command.extend(config.codex_extra_args)
    command.append("-")
    return command


def build_resume_command(config: Config, session_id: str) -> List[str]:
    # Same flag-ordering rule as build_exec_command. Positionals
    # (session_id, prompt-stdin) come last.
    command = [config.codex_bin]
    command.extend(["--sandbox", config.codex_sandbox])
    command.extend(["--ask-for-approval", config.codex_approval])
    command.extend(["exec", "resume", "--json"])
    if config.codex_model:
        command.extend(["--model", config.codex_model])
    command.extend([session_id, "-"])
    return command


def command_preview(command: Iterable[str]) -> str:
    return " ".join(shlex.quote(part) for part in command)


def build_start_prompt(
    workspace: Path,
    run_id: str,
    task_text: str,
    *,
    event_id: str = "",
    message_id: str = "",
) -> str:
    # The prompt template is sent verbatim to Codex; keeping each instruction on
    # one line matters for the agent reading it, so don't wrap the long lines.
    return """You were started from a Feishu/Lark bot message.

Work in this workspace: {workspace}
Follow the workspace AGENTS.md and Codex rules. Keep secrets, datasets, checkpoints, raw examples, and run artifacts out of local repos unless the user explicitly requests a safe local artifact.

Bridge metadata:
- run_id: {run_id}
- event_id: {event_id}
- message_id: {message_id}

User request:
{task}
""".format(workspace=workspace, run_id=run_id, event_id=event_id, message_id=message_id, task=task_text)


def build_continue_prompt(run_id: str, task_text: str) -> str:
    return """You are continuing work that was originally started from Feishu/Lark.

Bridge continuation run_id: {run_id}

User follow-up:
{task}
""".format(run_id=run_id, task=task_text)


def extract_session_id(event: Dict[str, Any]) -> Optional[str]:
    for key in ("session_id", "thread_id", "conversation_id"):
        value = event.get(key)
        if isinstance(value, str) and looks_like_session_id(value):
            return value
    payload = event.get("payload")
    if isinstance(payload, dict):
        found = extract_session_id(payload)
        if found:
            return found
    item = event.get("item")
    if isinstance(item, dict):
        found = extract_session_id(item)
        if found:
            return found
    return recursive_session_id(event)


def recursive_session_id(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in {"session_id", "thread_id", "conversation_id"} and isinstance(item, str):
                if looks_like_session_id(item):
                    return item
        for item in value.values():
            found = recursive_session_id(item)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = recursive_session_id(item)
            if found:
                return found
    return None


def looks_like_session_id(value: str) -> bool:
    return bool(UUIDISH_RE.match(value)) or value.startswith("019")


def event_tail_text(line: str, max_chars: int = 1200) -> str:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return redact(line, max_chars=max_chars)
    text = extract_interesting_text(event)
    if not text:
        text = json.dumps(event, ensure_ascii=False, sort_keys=True)
    return redact(text, max_chars=max_chars)


def extract_interesting_text(value: Any) -> str:
    parts: List[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, dict):
            for key in ("text", "message", "aggregated_output", "summary"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    parts.append(value.strip())
            content = item.get("content")
            if isinstance(content, str) and content.strip():
                parts.append(content.strip())
            elif isinstance(content, list):
                visit(content)
            for key in ("payload", "item", "output"):
                if key in item:
                    visit(item[key])
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return "\n".join(parts[-4:])


def live_session_ids() -> Set[str]:
    """Codex session IDs whose rollout-*.jsonl file is currently held open.

    Codex's RolloutRecorder keeps the per-session rollout file open in write
    mode for the lifetime of the session (openai/codex PR #17214). So a
    session is "live" iff some `codex`-named process has the corresponding
    file in its FD table. We ask `lsof` for that table and pull session_ids
    out of the matching filenames. Hard-coded argv, no user input flows in.
    """
    try:
        result = subprocess.run(
            ["lsof", "-nP", "-Fn", "-c", "codex"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return set()
    ids: Set[str] = set()
    for line in result.stdout.splitlines():
        if not line.startswith("n"):
            continue
        match = ROLLOUT_RE.search(line[1:])
        if match:
            ids.add(match.group(1))
    return ids


def _all_sessions(config: Config) -> List[Dict[str, str]]:
    index = config.codex_home / "session_index.jsonl"
    if not index.exists():
        return []
    rows: List[Dict[str, str]] = []
    for line in index.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        session_id = str(data.get("id") or "")
        if not session_id:
            continue
        rows.append(
            {
                "id": session_id,
                "thread_name": redact(str(data.get("thread_name") or ""), max_chars=80),
                "updated_at": str(data.get("updated_at") or ""),
            }
        )
    return rows


def recent_sessions(config: Config, limit: int = 8) -> List[Dict[str, str]]:
    return _all_sessions(config)[-limit:][::-1]


def find_session(config: Config, session_id: str) -> Optional[Dict[str, str]]:
    for row in _all_sessions(config):
        if row["id"] == session_id:
            return row
    return None


def format_recent_sessions(config: Config, limit: int = 8) -> str:
    rows = recent_sessions(config, limit=limit)
    if not rows:
        return "No recent Codex sessions found. These are recent/resumable, not guaranteed running."
    lines = ["Recent Codex sessions (not guaranteed running):"]
    for row in rows:
        lines.append("- %s %s %s" % (row["id"], row["updated_at"], row["thread_name"]))
    return "\n".join(lines)


def format_codex_thread_status(session: Dict[str, str]) -> str:
    return (
        "Codex thread %s (not bridge-owned)\n"
        "thread: %s\n"
        "updated: %s\n"
        "No captured output tail — the bridge doesn't store output for "
        "threads it didn't spawn. Resume locally with: codex exec resume %s"
    ) % (
        session["id"],
        session["thread_name"] or "(none)",
        session["updated_at"] or "(unknown)",
        session["id"],
    )
