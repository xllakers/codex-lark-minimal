"""Message command parsing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from codex_lark_minimal.config import Config


@dataclass(frozen=True)
class ParsedCommand:
    kind: str
    workspace_alias: Optional[str] = None
    task_text: str = ""
    run_id: str = ""


def parse_message(text: str, config: Config) -> Optional[ParsedCommand]:
    body = normalize(text)
    if not body:
        return None
    prefix = config.trigger_prefix.strip()
    if prefix:
        if not body.lower().startswith(prefix.lower()):
            return None
        body = body[len(prefix) :].strip()
    if not body:
        return ParsedCommand(kind="help")

    lowered = body.lower()
    if lowered == "help":
        return ParsedCommand(kind="help")
    if lowered == "workspaces":
        return ParsedCommand(kind="workspaces")
    if lowered == "recent":
        return ParsedCommand(kind="recent")
    if lowered == "status":
        return ParsedCommand(kind="status")
    if lowered.startswith("status "):
        return ParsedCommand(kind="status_one", run_id=body.split(None, 1)[1].strip())
    if lowered.startswith("stop "):
        return ParsedCommand(kind="stop", run_id=body.split(None, 1)[1].strip())
    if lowered.startswith("continue "):
        rest = body.split(None, 1)[1].strip()
        if ":" not in rest:
            return ParsedCommand(kind="bad_continue", task_text="Use: codex continue <run_id>: <instruction>")
        run_id, instruction = rest.split(":", 1)
        return ParsedCommand(kind="continue", run_id=run_id.strip(), task_text=instruction.strip())

    alias, task = route_start(body, config)
    if not alias or not task:
        return ParsedCommand(kind="unknown", task_text=body)
    return ParsedCommand(kind="start", workspace_alias=alias, task_text=task)


def route_start(body: str, config: Config) -> tuple:
    for alias in sorted(config.workspaces, key=len, reverse=True):
        marker = alias + ":"
        slash_marker = "/" + alias + " "
        if body.lower().startswith(marker.lower()):
            return alias, body[len(marker) :].strip()
        if body.lower().startswith(slash_marker.lower()):
            return alias, body[len(slash_marker) :].strip()
    return config.default_workspace, body.strip()


def normalize(text: str) -> str:
    return " ".join(text.replace("\u00a0", " ").split()).strip()


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
