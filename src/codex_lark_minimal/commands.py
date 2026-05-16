"""Message command parsing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from codex_lark_minimal.config import Config


@dataclass(frozen=True)
class ParsedCommand:
    kind: str
    workspace_alias: Optional[str] = None
    task_text: str = ""
    run_id: str = ""


def parse_message(text: str, config: Config) -> Optional[ParsedCommand]:
    # Two-track parse: `body_flat` collapses whitespace for command/alias
    # detection (tolerant of how users type the prefix); `raw` keeps the
    # user's original line breaks so multi-line code in `task_text` survives.
    raw = text.replace(" ", " ").strip()
    if not raw:
        return None
    body_flat = " ".join(raw.split())

    prefix = config.trigger_prefix.strip()
    if prefix:
        if not body_flat.lower().startswith(prefix.lower()):
            return None
        body_flat = body_flat[len(prefix) :].strip()
    if not body_flat:
        return ParsedCommand(kind="help")

    lowered = body_flat.lower()
    if lowered == "help":
        return ParsedCommand(kind="help")
    if lowered == "workspaces":
        return ParsedCommand(kind="workspaces")
    if lowered == "recent":
        return ParsedCommand(kind="recent")
    if lowered == "status":
        return ParsedCommand(kind="status")
    if lowered.startswith("status "):
        return ParsedCommand(kind="status_one", run_id=body_flat.split(None, 1)[1].strip())
    if lowered.startswith("stop "):
        return ParsedCommand(kind="stop", run_id=body_flat.split(None, 1)[1].strip())
    if lowered.startswith("continue "):
        rest = body_flat.split(None, 1)[1].strip()
        if ":" not in rest:
            return ParsedCommand(kind="bad_continue", task_text="Use: codex continue <run_id>: <instruction>")
        run_id_flat, flat_instruction = rest.split(":", 1)
        run_id = run_id_flat.strip()
        # Pull the instruction from raw so newlines/indentation survive; fall
        # back to the flat form only if the marker can't be located in raw.
        instruction = _text_after_marker(raw, run_id + ":") or flat_instruction.strip()
        return ParsedCommand(kind="continue", run_id=run_id, task_text=instruction)

    alias, flat_task = route_start(body_flat, config)
    if not alias or not flat_task:
        return ParsedCommand(kind="unknown", task_text=body_flat)
    task = _text_after_marker(raw, alias + ":") or flat_task
    return ParsedCommand(kind="start", workspace_alias=alias, task_text=task)


def route_start(body: str, config: Config) -> Tuple[str, str]:
    for alias in sorted(config.workspaces, key=len, reverse=True):
        marker = alias + ":"
        slash_marker = "/" + alias + " "
        if body.lower().startswith(marker.lower()):
            return alias, body[len(marker) :].strip()
        if body.lower().startswith(slash_marker.lower()):
            return alias, body[len(slash_marker) :].strip()
    return config.default_workspace, body.strip()


def normalize(text: str) -> str:
    return " ".join(text.replace(" ", " ").split()).strip()


def _text_after_marker(raw: str, marker: str) -> str:
    """Locate the first case-insensitive occurrence of `marker` in `raw` and
    return everything after it, with outer whitespace stripped but internal
    whitespace (newlines, indentation) preserved. Returns "" if not found.
    """
    idx = raw.lower().find(marker.lower())
    if idx < 0:
        return ""
    return raw[idx + len(marker) :].strip()


def help_text(config: Config) -> str:
    prefix = config.trigger_prefix or "codex"
    return "\n".join(
        [
            "codex-lark-minimal commands:",
            "%s help" % prefix,
            "%s workspaces" % prefix,
            "%s status" % prefix,
            "%s status <run_id>" % prefix,
            "%s stop <run_id>" % prefix,
            "%s continue <run_id>: <instruction>" % prefix,
            "%s recent" % prefix,
            "%s <workspace>: <task>" % prefix,
        ]
    )
