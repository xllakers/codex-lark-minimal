"""Bridge command handling and job launching."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from codex_lark_minimal.codex import (
    find_session,
    format_codex_thread_status,
    format_recent_sessions,
    format_thread_row,
    humanize_time,
    live_session_ids,
    recent_sessions,
    resolve_session,
)
from codex_lark_minimal.commands import ParsedCommand, help_text, parse_message
from codex_lark_minimal.config import Config, format_workspaces
from codex_lark_minimal.redaction import redact
from codex_lark_minimal.state import JobRecord, StateStore, summarize_job, summarize_jobs

# C0 + C1-C8 + C11-C31 minus tab/lf/cr. These break JSON parsers and shell
# pipelines if they reach the worker prompt or state file.
_FORBIDDEN_CONTROL_CHARS = frozenset(
    chr(c) for c in range(0x20) if c not in (0x09, 0x0A, 0x0D)
) | frozenset({"\x7f"})


@dataclass(frozen=True)
class EventMeta:
    event_id: str = ""
    message_id: str = ""
    chat_id: str = ""
    sender_id: str = ""


class BridgeController:
    def __init__(self, config: Config, launcher: Optional["WorkerLauncher"] = None):
        self.config = config
        self.store = StateStore(config)
        self.launcher = launcher or WorkerLauncher(config)

    def handle_text(self, text: str, meta: EventMeta) -> str:
        if not self.allowed(meta):
            return "Ignored: sender/chat is not allowlisted."
        command = parse_message(text, self.config)
        if command is None:
            return ""
        return self.handle_command(command, meta)

    def handle_command(self, command: ParsedCommand, meta: EventMeta) -> str:
        if command.kind == "help":
            return help_text(self.config)
        if command.kind == "workspaces":
            return format_workspaces(self.config)
        if command.kind == "recent":
            return format_recent_sessions(self.config)
        if command.kind == "status":
            return self.status_summary()
        if command.kind == "status_one":
            return self.status_one(command.run_id)
        if command.kind == "stop":
            return self.stop_job(command.run_id)
        if command.kind == "continue":
            return self.continue_job(command.run_id, command.task_text, meta)
        if command.kind == "bad_continue":
            return command.task_text
        if command.kind == "start":
            return self.start_job(command, meta)
        return "Unknown command.\n\n" + help_text(self.config)

    def allowed(self, meta: EventMeta) -> bool:
        if meta.sender_id and meta.sender_id in self.config.allowed_senders:
            return True
        if meta.chat_id and meta.chat_id in self.config.allowed_chats:
            return True
        return False

    def start_job(self, command: ParsedCommand, meta: EventMeta) -> str:
        if not command.workspace_alias or command.workspace_alias not in self.config.workspaces:
            return "No workspace configured for this request. Try: codex workspaces"
        workspace = self.config.workspaces[command.workspace_alias]
        if not workspace.is_dir():
            return "Workspace alias '%s' no longer exists on disk: %s" % (command.workspace_alias, workspace)
        if not command.task_text:
            return "No task text found."
        if len(command.task_text) > self.config.max_prompt_chars:
            return "Request is too long; max is %s characters." % self.config.max_prompt_chars
        if any(ch in _FORBIDDEN_CONTROL_CHARS for ch in command.task_text):
            return "Request contains invalid control characters."
        active = self.store.active_jobs()
        if len(active) >= self.config.max_running:
            return "Busy: %s job(s) already running.\n%s" % (len(active), summarize_jobs(active))

        record = self.make_record(
            command.workspace_alias,
            workspace,
            command.task_text,
            meta,
            status="dry_run" if self.config.dry_run else "starting",
        )
        self.store.write(record)
        if self.config.dry_run:
            return "Dry run: would start %s [%s]." % (record.run_id, record.workspace_alias)
        self.launcher.launch(record.run_id, command.task_text, mode="start")
        return "Codex started: %s [%s]. Use `codex status %s` for updates." % (
            record.run_id,
            record.workspace_alias,
            record.run_id,
        )

    def continue_job(self, run_id: str, instruction: str, meta: EventMeta) -> str:
        original = self.store.get(run_id)
        if original is None:
            return "No bridge job found for run_id: %s" % run_id
        if original.active:
            return "Job %s is still running. Use `codex status %s` or `codex stop %s` first." % (run_id, run_id, run_id)
        if not original.codex_session_id:
            return "Job %s has no captured Codex session id, so it cannot be continued." % run_id
        if not instruction:
            return "Use: codex continue %s: <instruction>" % run_id
        if any(ch in _FORBIDDEN_CONTROL_CHARS for ch in instruction):
            return "Request contains invalid control characters."
        workspace = Path(original.workspace_path)
        if not workspace.is_dir():
            return "Workspace for %s no longer exists on disk: %s" % (run_id, workspace)
        active = self.store.active_jobs()
        if len(active) >= self.config.max_running:
            return "Busy: %s job(s) already running.\n%s" % (len(active), summarize_jobs(active))
        record = self.make_record(
            original.workspace_alias,
            workspace,
            instruction,
            meta,
            status="dry_run" if self.config.dry_run else "starting",
            continuation_of=run_id,
            codex_session_id=original.codex_session_id,
        )
        self.store.write(record)
        if self.config.dry_run:
            return "Dry run: would continue %s as %s." % (run_id, record.run_id)
        self.launcher.launch(record.run_id, instruction, mode="resume", session_id=original.codex_session_id)
        return "Codex continuation started: %s (from %s)." % (record.run_id, run_id)

    def status_summary(self) -> str:
        """Latest 5 Codex threads plus any live thread that fell outside that window.

        Rows come from Codex's session index. Bridge-spawned threads render
        with bridge metadata (run_id, workspace, exit, prompt preview); threads
        started elsewhere render as `[running] name · time · short-id` or
        `[idle] …`. Live threads whose `updated_at` is older than the latest 5
        (e.g. a long-running session that hasn't logged in a while) are
        surfaced explicitly so they aren't invisible. Active bridge jobs that
        haven't captured a session id yet are prepended for the same reason.
        """
        threads = recent_sessions(self.config, limit=5)
        live = live_session_ids()
        # Map every bridge job's session_id → record (latest write wins, which
        # `store.list()` already returns sorted newest-first).
        bridge_by_session: dict = {}
        for rec in self.store.list():
            sid = rec.codex_session_id
            if sid and sid not in bridge_by_session:
                bridge_by_session[sid] = rec
        thread_ids = {t["id"] for t in threads}
        # Live threads that didn't make the latest-5 cut — without this they
        # silently disappear. find_session walks the full index for metadata;
        # fall back to a placeholder if even that misses (rare: live but not
        # yet flushed to session_index.jsonl).
        live_extras: list = []
        for sid in sorted(live - thread_ids):
            extra = find_session(self.config, sid) or {"id": sid, "thread_name": "", "updated_at": ""}
            live_extras.append(extra)
            thread_ids.add(sid)
        fresh_active = [
            rec for rec in self.store.active_jobs()
            if (rec.codex_session_id or "") not in thread_ids
        ]
        if not threads and not fresh_active and not live_extras:
            return "No recent Codex threads found."
        parts = ["Codex threads:"]
        for rec in fresh_active:
            parts.append(self._compact_bridge_row(rec))
        for thread in live_extras:
            parts.append(format_thread_row(thread, "running"))
        for thread in threads:
            sid = thread["id"]
            owned = bridge_by_session.get(sid)
            if owned is not None:
                parts.append(self._compact_bridge_row(owned))
            else:
                tag = "running" if sid in live else "idle"
                parts.append(format_thread_row(thread, tag))
        return "\n".join(parts)

    def _compact_bridge_row(self, record) -> str:
        """One-line bridge job summary for the status list, prompt preview indented."""
        bits = [record.workspace_alias]
        if record.returncode is not None:
            bits.append("exit %s" % record.returncode)
        bits.append(humanize_time(record.updated_at))
        line = "[%s] %s · %s" % (record.status, record.run_id, " · ".join(bits))
        if record.prompt_preview:
            line += "\n  " + record.prompt_preview
        return line

    def status_one(self, run_id: str) -> str:
        record = self.store.get(run_id)
        if record is not None:
            return summarize_job(record, include_tail=True)
        # Try Codex's session index: arg may be a session_id, a thread name,
        # an id-prefix, or a substring of a name.
        match, candidates = resolve_session(self.config, run_id)
        if match is not None:
            return format_codex_thread_status(match, self.config)
        if candidates:
            live = live_session_ids()
            lines = ["Multiple Codex threads match '%s':" % run_id]
            for session in candidates:
                tag = "running" if session["id"] in live else "idle"
                lines.append("  " + format_thread_row(session, tag))
            lines.append("Refine with a more specific name or the id.")
            return "\n".join(lines)
        return "No bridge run or Codex session matches: %s" % run_id

    def stop_job(self, run_id: str) -> str:
        try:
            record = self.store.stop(run_id)
        except KeyError:
            return "No bridge job found for run_id: %s" % run_id
        return "Stopped %s. Current status: %s" % (run_id, record.status)

    def make_record(
        self,
        workspace_alias: str,
        workspace: Path,
        task_text: str,
        meta: EventMeta,
        *,
        status: str,
        continuation_of: Optional[str] = None,
        codex_session_id: Optional[str] = None,
    ) -> JobRecord:
        return JobRecord(
            run_id=self.store.new_run_id(),
            status=status,
            workspace_alias=workspace_alias,
            workspace_path=str(workspace),
            prompt_sha256=hashlib.sha256(task_text.encode("utf-8")).hexdigest(),
            prompt_preview=redact(task_text, max_chars=180),
            event_id=meta.event_id,
            message_id=meta.message_id,
            chat_id=meta.chat_id,
            sender_id=meta.sender_id,
            continuation_of=continuation_of,
            codex_session_id=codex_session_id,
        )


class WorkerLauncher:
    def __init__(self, config: Config):
        self.config = config

    def launch(self, run_id: str, task_text: str, *, mode: str, session_id: Optional[str] = None) -> None:
        if not self.config.config_path:
            raise RuntimeError("config_path is required to launch worker")
        command = [
            sys.executable,
            "-m",
            "codex_lark_minimal.worker",
            "--config",
            str(self.config.config_path),
            "--run-id",
            run_id,
            "--mode",
            mode,
        ]
        if session_id:
            command.extend(["--session-id", session_id])
        self.config.logs_dir.mkdir(parents=True, exist_ok=True)
        # `with` guarantees the parent's log handle is closed even if Popen raises;
        # Popen duplicates the fd into the child so closing here is correct.
        with self.config.log_path.open("a", encoding="utf-8") as log:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            if process.stdin is not None:
                process.stdin.write(task_text)
                process.stdin.close()
        # Daemon owns the `pid` field only. The worker writes its own `status`
        # transition to "running" once it boots, so the two writers never race
        # on the same field.
        try:
            StateStore(self.config).update(run_id, pid=process.pid)
        except KeyError:
            pass
