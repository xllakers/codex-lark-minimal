"""JSON-file state store for bridge-started jobs."""

from __future__ import annotations

import fcntl
import json
import os
import signal
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

from codex_lark_minimal.config import Config, ensure_dirs

ACTIVE_STATUSES = {"starting", "running"}
TERMINAL_STATUSES = {"dry_run", "completed", "failed", "stopped", "lost"}
IMMUTABLE_FIELDS = frozenset({"run_id", "created_at"})


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class JobRecord:
    run_id: str
    status: str
    workspace_alias: str
    workspace_path: str
    prompt_sha256: str
    prompt_preview: str
    created_at: str = field(default_factory=now)
    updated_at: str = field(default_factory=now)
    event_id: str = ""
    message_id: str = ""
    chat_id: str = ""
    sender_id: str = ""
    pid: Optional[int] = None
    codex_pid: Optional[int] = None
    codex_session_id: Optional[str] = None
    continuation_of: Optional[str] = None
    returncode: Optional[int] = None
    command_preview: str = ""
    redacted_output_tail: str = ""
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "JobRecord":
        known = {field.name for field in cls.__dataclass_fields__.values()}
        return cls(**{key: value for key, value in data.items() if key in known})

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES


class StateStore:
    def __init__(self, config: Config):
        self.config = config
        ensure_dirs(config)

    def path_for(self, run_id: str) -> Path:
        safe = "".join(ch for ch in run_id if ch.isalnum() or ch in {"_", "-"})
        return self.config.jobs_dir / ("%s.json" % safe)

    def lock_path_for(self, run_id: str) -> Path:
        safe = "".join(ch for ch in run_id if ch.isalnum() or ch in {"_", "-"})
        return self.config.jobs_dir / ("%s.lock" % safe)

    def new_run_id(self) -> str:
        return "clk_%s" % uuid.uuid4().hex[:12]

    @contextmanager
    def _record_lock(self, run_id: str) -> Iterator[None]:
        """Serialize read-modify-write for a single run record across processes.

        Daemon and worker can race to update the same JSON file; whole-record
        serialization under flock keeps disjoint-field updates from clobbering
        each other.
        """
        lock_path = self.lock_path_for(run_id)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def write(self, record: JobRecord) -> None:
        record.updated_at = now()
        path = self.path_for(record.run_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(record.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
        os.replace(str(tmp), str(path))

    def update(self, run_id: str, **updates: Any) -> JobRecord:
        bad = [key for key in updates if key in IMMUTABLE_FIELDS]
        if bad:
            raise ValueError("cannot update immutable field(s): %s" % ", ".join(sorted(bad)))
        with self._record_lock(run_id):
            record = self._read(run_id)
            if record is None:
                raise KeyError(run_id)
            for key, value in updates.items():
                if hasattr(record, key):
                    setattr(record, key, value)
            self.write(record)
            self._cleanup_lock_if_terminal(record)
            return record

    def _read(self, run_id: str) -> Optional[JobRecord]:
        """Pure read from disk; no liveness side-effects, safe inside a lock."""
        path = self.path_for(run_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return None
            return JobRecord.from_dict(data)
        except (OSError, json.JSONDecodeError, TypeError):
            return None

    def get(self, run_id: str) -> Optional[JobRecord]:
        record = self._read(run_id)
        if record is None:
            return None
        return self.refresh_record(record)

    def list(self, limit: Optional[int] = None) -> List[JobRecord]:
        records: List[JobRecord] = []
        for path in sorted(self.config.jobs_dir.glob("*.json"), reverse=True):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    records.append(self.refresh_record(JobRecord.from_dict(data)))
            except (OSError, json.JSONDecodeError, TypeError):
                continue
        records.sort(key=lambda item: item.updated_at, reverse=True)
        return records[:limit] if limit is not None else records

    def active_jobs(self) -> List[JobRecord]:
        return [record for record in self.list() if record.status in ACTIVE_STATUSES]

    def refresh_record(self, record: JobRecord) -> JobRecord:
        if record.status not in ACTIVE_STATUSES or not record.pid or pid_alive(record.pid):
            return record
        with self._record_lock(record.run_id):
            fresh = self._read(record.run_id)
            if fresh is None or fresh.status not in ACTIVE_STATUSES:
                return fresh or record
            fresh.status = "lost"
            fresh.error = fresh.error or "worker process is no longer running"
            self.write(fresh)
            self._cleanup_lock_if_terminal(fresh)
            return fresh

    def stop(self, run_id: str) -> JobRecord:
        with self._record_lock(run_id):
            record = self._read(run_id)
            if record is None:
                raise KeyError(run_id)
            stopped_any = False
            for pid in [record.pid, record.codex_pid]:
                if pid and pid_alive(pid):
                    try:
                        os.kill(pid, signal.SIGTERM)
                        stopped_any = True
                    except OSError:
                        pass
            if record.status in ACTIVE_STATUSES or stopped_any:
                record.status = "stopped"
                record.error = "stopped by request"
                self.write(record)
            self._cleanup_lock_if_terminal(record)
            return record

    def _cleanup_lock_if_terminal(self, record: JobRecord) -> None:
        """Remove the `<run_id>.lock` file once a job has reached a terminal
        status. Called inside the lock: the FD stays valid until close, so any
        writer already queued on `flock` finishes against the same inode. New
        writers arriving after the unlink would `O_CREAT` a fresh inode — but
        by contract no further legitimate writes happen on a terminal record.
        """
        if record.status not in TERMINAL_STATUSES:
            return
        try:
            self.lock_path_for(record.run_id).unlink()
        except FileNotFoundError:
            pass


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def summarize_job(record: JobRecord, *, include_tail: bool = False) -> str:
    session = " session=%s" % short(record.codex_session_id) if record.codex_session_id else ""
    pid = " pid=%s" % record.pid if record.pid else ""
    code = " exit=%s" % record.returncode if record.returncode is not None else ""
    line = "[%s] %s %s%s%s%s\n%s" % (
        record.status,
        record.run_id,
        record.workspace_alias,
        session,
        pid,
        code,
        record.prompt_preview,
    )
    if include_tail and record.redacted_output_tail:
        line += "\n--- tail ---\n" + record.redacted_output_tail
    if include_tail and record.error:
        line += "\nerror: " + record.error
    return line


def summarize_jobs(records: Iterable[JobRecord]) -> str:
    items = list(records)
    if not items:
        return "No bridge jobs recorded."
    return "\n\n".join(summarize_job(record) for record in items)


def short(value: Optional[str], length: int = 8) -> str:
    if not value:
        return ""
    return value[:length]
