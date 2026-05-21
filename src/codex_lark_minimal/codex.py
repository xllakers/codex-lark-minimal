"""Codex command construction and JSON event parsing."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

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
    """Read session_index.jsonl, collapsing repeat lines per id to the latest.

    Codex appends a new line whenever a thread is renamed/touched, so the same
    `id` can show up with different `thread_name` values. We keep the last
    occurrence (most recent) and preserve its file-order position so callers
    that interpret the list as chronological still see the right ordering.
    """
    index = config.codex_home / "session_index.jsonl"
    if not index.exists():
        return []
    latest: Dict[str, Dict[str, str]] = {}
    order: Dict[str, int] = {}
    seq = 0
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
        latest[session_id] = {
            "id": session_id,
            "thread_name": redact(str(data.get("thread_name") or ""), max_chars=80),
            "updated_at": str(data.get("updated_at") or ""),
        }
        order[session_id] = seq
        seq += 1
    return [latest[sid] for sid in sorted(order, key=order.__getitem__)]


# macOS PIPE_BUF — POSIX guarantees writes up to this size to a single fd are
# atomic with respect to other writers. Larger writes can interleave across
# concurrent appenders, so we cap our line below this bound. Other writers on
# this file (Codex itself, Desktop app) also do single-line appends, so as long
# as everyone stays ≤ PIPE_BUF, no one ever sees a half-written line.
_SESSION_INDEX_MAX_LINE_BYTES = 512
# Codex's `_all_sessions` truncates thread_name to 80 chars on read, so capping
# the written name here keeps the on-disk representation aligned with what
# downstream readers (us, Codex, anything else) actually use.
_SESSION_INDEX_THREAD_NAME_MAX_CHARS = 80


def append_session_index_entry(
    config: Config, session_id: str, thread_name: str
) -> bool:
    """Append one bridge-spawned session row to `<codex_home>/session_index.jsonl`.

    Why this exists: the npm `codex` 0.130 CLI writes a rollout file for each
    bridge-spawned session but does not update Codex's session_index.jsonl, so
    `codex status <name>` can't find bridge-spawned threads by their prompt
    preview. Appending a minimal entry here closes that gap without touching
    Codex's SQLite state (which is schema-versioned, write-locked, and out of
    scope).

    Contract:
    - Returns True iff a line was written. Never raises; all failures swallowed.
    - Idempotent at the call site's discretion: this function does not dedupe,
      since `_all_sessions` already collapses repeats by id (latest wins).
    - Strict input validation:
        * session_id must be UUID-shaped — rejects anything else so chat-derived
          content cannot reach this code path.
        * thread_name is re-redacted, truncated to 80 chars, and stripped of
          non-printable characters (newlines/tabs would otherwise split the row
          across JSONL lines).
    - Bounded line size (≤ PIPE_BUF on macOS) so a single os.write under
      O_APPEND is atomic relative to other writers on the same file.
    - File path is fixed to `<codex_home>/session_index.jsonl`; never chat input.
    """
    if not config.session_index_append:
        return False
    if not isinstance(session_id, str) or not UUIDISH_RE.match(session_id):
        return False
    safe_name = redact((thread_name or "").strip(), max_chars=_SESSION_INDEX_THREAD_NAME_MAX_CHARS)
    # str.isprintable() rejects \n \r \t and all other C0/C1 controls, while
    # accepting regular spaces and Unicode letters/punctuation. Strip after.
    safe_name = "".join(ch for ch in safe_name if ch.isprintable()).strip()
    payload = {
        "id": session_id,
        "thread_name": safe_name,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    }
    try:
        encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    except (TypeError, ValueError):
        return False
    if len(encoded) > _SESSION_INDEX_MAX_LINE_BYTES:
        # One retry with a tighter name budget. Compute the byte cost of every
        # other field, then trim the name to fit within what's left, minus a
        # safety margin. If that's still too tight (would-be-empty name), bail.
        name_bytes = safe_name.encode("utf-8")
        overhead = len(encoded) - len(name_bytes)
        budget = _SESSION_INDEX_MAX_LINE_BYTES - overhead - 1
        if budget < 1:
            return False
        # Trim by byte budget but avoid splitting a UTF-8 codepoint.
        safe_name = name_bytes[:budget].decode("utf-8", errors="ignore").rstrip()
        payload["thread_name"] = safe_name
        try:
            encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        except (TypeError, ValueError):
            return False
        if len(encoded) > _SESSION_INDEX_MAX_LINE_BYTES:
            return False
    path = config.codex_home / "session_index.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    except OSError:
        return False
    try:
        os.write(fd, encoded)
    except OSError:
        return False
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    return True


def recent_sessions(config: Config, limit: int = 8) -> List[Dict[str, str]]:
    return _all_sessions(config)[-limit:][::-1]


def find_session(config: Config, session_id: str) -> Optional[Dict[str, str]]:
    for row in _all_sessions(config):
        if row["id"] == session_id:
            return row
    return None


def resolve_session(
    config: Config, arg: str
) -> Tuple[Optional[Dict[str, str]], List[Dict[str, str]]]:
    """Resolve `arg` to a Codex session by id, name, or partial match.

    Returns (match, candidates):
      - (session, [])              uniquely resolved
      - (None, [session, ...])     ambiguous — caller should ask the user
      - (None, [])                 no match at all
    Resolution order: exact id → exact thread_name (case-insensitive)
    → id-prefix → thread_name substring. Stops at the first tier that hits.
    """
    sessions = _all_sessions(config)
    for row in sessions:
        if row["id"] == arg:
            return row, []
    arg_lower = arg.lower()
    exact_name = [row for row in sessions if (row["thread_name"] or "").lower() == arg_lower]
    if exact_name:
        return exact_name[-1], []
    id_prefix = [row for row in sessions if row["id"].startswith(arg_lower)]
    if len(id_prefix) == 1:
        return id_prefix[0], []
    if len(id_prefix) > 1:
        return None, id_prefix[:8]
    substring = [row for row in sessions if arg_lower in (row["thread_name"] or "").lower()]
    if len(substring) == 1:
        return substring[0], []
    if len(substring) > 1:
        return None, substring[:8]
    return None, []


def rollout_path_for(config: Config, session_id: str) -> Optional[Path]:
    """Locate the rollout-*-<session_id>.jsonl file under the session tree."""
    base = config.codex_home / "sessions"
    if not base.is_dir():
        return None
    matches = list(base.glob("*/*/*/rollout-*-%s.jsonl" % session_id))
    return matches[0] if matches else None


def read_rollout_tail(path: Path, max_bytes: int = 1_000_000) -> str:
    """Read at most the last `max_bytes` of a rollout file, dropping any partial
    leading line. Returns "" if the file can't be read.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    try:
        with path.open("rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()
            data = fh.read()
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def extract_thread_highlights(
    rollout_text: str, *, max_agent: int = 3
) -> Tuple[str, str, List[str]]:
    """Pick the latest goal objective, latest user message, and last N agent
    messages from a slice of rollout JSONL.
    """
    goal = ""
    last_user = ""
    agents: List[str] = []
    for line in rollout_text.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        ptype = payload.get("type", "")
        if ptype == "thread_goal_updated":
            goal_obj = payload.get("goal")
            if isinstance(goal_obj, dict):
                objective = goal_obj.get("objective")
                if isinstance(objective, str) and objective.strip():
                    goal = objective.strip()
        elif ptype == "user_message":
            message = payload.get("message")
            if isinstance(message, str) and message.strip():
                last_user = message.strip()
        elif ptype == "agent_message":
            message = payload.get("message")
            if isinstance(message, str) and message.strip():
                agents.append(message.strip())
    return goal, last_user, agents[-max_agent:]


def humanize_time(iso_str: str) -> str:
    """Render an ISO 8601 timestamp as a short relative form like '5m ago'.

    Falls back to the raw string if parsing fails so the UI never goes blank.
    """
    if not iso_str:
        return "?"
    raw = iso_str
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    # Codex sometimes writes fractional seconds with 4–5 digits; Python's
    # fromisoformat before 3.11 only accepts 3 or 6 digits. Pad/truncate.
    frac = re.search(r"\.(\d+)", raw)
    if frac and len(frac.group(1)) not in (3, 6):
        digits = (frac.group(1) + "000000")[:6]
        raw = raw[: frac.start() + 1] + digits + raw[frac.end() :]
    try:
        ts = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return iso_str
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    secs = int((datetime.now(timezone.utc) - ts).total_seconds())
    if secs < 0:
        return "soon"
    if secs < 60:
        return "just now"
    if secs < 3600:
        return "%dm ago" % (secs // 60)
    if secs < 86400:
        return "%dh ago" % (secs // 3600)
    if secs < 86400 * 14:
        return "%dd ago" % (secs // 86400)
    if secs < 86400 * 60:
        return "%dw ago" % (secs // 86400 // 7)
    return ts.strftime("%Y-%m-%d")


def short_session_id(value: str) -> str:
    """First UUID segment — unique enough at small scales to use as a tail tag."""
    if "-" in value:
        return value.split("-", 1)[0]
    return value[:8]


def format_thread_row(thread: Dict[str, str], tag: str) -> str:
    name = thread.get("thread_name") or "(unnamed)"
    when = humanize_time(thread.get("updated_at", ""))
    sid = short_session_id(thread.get("id", ""))
    return "[%s] %s · %s · %s" % (tag, name, when, sid)


def format_recent_sessions(config: Config, limit: int = 8) -> str:
    rows = recent_sessions(config, limit=limit)
    if not rows:
        return "No recent Codex sessions found. These are recent/resumable, not guaranteed running."
    lines = ["Recent Codex sessions (not guaranteed running):"]
    for row in rows:
        lines.append(
            "- %s · %s · %s"
            % (row["thread_name"] or "(unnamed)", humanize_time(row["updated_at"]), short_session_id(row["id"]))
        )
    return "\n".join(lines)


def format_codex_thread_status(
    session: Dict[str, str], config: Optional[Config] = None
) -> str:
    """Render a Codex (non-bridge) thread, with rollout-tail highlights when
    `config` is provided and the rollout file can be located.
    """
    name = session["thread_name"] or "(unnamed)"
    sid = session["id"]
    when = humanize_time(session["updated_at"]) if session["updated_at"] else "(unknown)"
    lines = [
        "Codex thread · %s (not bridge-owned)" % name,
        "id: %s · updated %s" % (sid, when),
    ]
    if config is not None:
        path = rollout_path_for(config, sid)
        if path is not None:
            goal, last_user, agents = extract_thread_highlights(read_rollout_tail(path))
            if goal:
                lines += ["", "Goal:", "  " + redact(goal, max_chars=240)]
            if last_user:
                lines += ["", "Latest user:", "  " + redact(last_user, max_chars=240)]
            if agents:
                lines += ["", "Latest agent (last %d):" % len(agents)]
                for message in agents:
                    lines.append("  - " + redact(message, max_chars=320))
    lines += ["", "Resume locally with: codex exec resume " + sid]
    return "\n".join(lines)
