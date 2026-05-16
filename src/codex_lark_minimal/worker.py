"""Detached worker process that runs one Codex job."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional
import argparse
import json
import os
import signal
import subprocess
import sys

from codex_lark_minimal.codex import (
    build_continue_prompt,
    build_exec_command,
    build_resume_command,
    build_start_prompt,
    command_preview,
    event_tail_text,
    extract_session_id,
)
from codex_lark_minimal.config import load_config
from codex_lark_minimal.redaction import redact
from codex_lark_minimal.state import StateStore

CHILD: Optional[subprocess.Popen] = None


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="codex-lark-worker")
    parser.add_argument("--config", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--mode", choices=["start", "resume"], required=True)
    parser.add_argument("--session-id")
    args = parser.parse_args(argv)

    config = load_config(Path(args.config))
    store = StateStore(config)
    record = store.get(args.run_id)
    if record is None:
        print("unknown run id: %s" % args.run_id, file=sys.stderr)
        return 2

    user_text = sys.stdin.read()
    if args.mode == "resume":
        if not args.session_id:
            store.update(args.run_id, status="failed", error="resume requested without session id")
            return 2
        prompt = build_continue_prompt(args.run_id, user_text)
        command = build_resume_command(config, args.session_id)
    else:
        prompt = build_start_prompt(
            Path(record.workspace_path),
            args.run_id,
            user_text,
            event_id=record.event_id,
            message_id=record.message_id,
        )
        command = build_exec_command(config, Path(record.workspace_path))

    def handle_signal(signum, _frame):
        if CHILD is not None and CHILD.poll() is None:
            try:
                CHILD.terminate()
            except OSError:
                pass
        try:
            store.update(args.run_id, status="stopped", error="worker received signal %s" % signum)
        finally:
            raise SystemExit(128 + int(signum))

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    store.update(
        args.run_id,
        status="running",
        pid=os.getpid(),
        command_preview=command_preview(command),
    )
    return run_codex(command, prompt, store, args.run_id)


def run_codex(command: List[str], prompt: str, store: StateStore, run_id: str) -> int:
    global CHILD
    tail = ""
    existing = store.get(run_id)
    session_id = existing.codex_session_id if existing else None
    try:
        CHILD = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except OSError as exc:
        store.update(run_id, status="failed", error=redact(str(exc), max_chars=500))
        return 2

    store.update(run_id, codex_pid=CHILD.pid)
    if CHILD.stdin is not None:
        CHILD.stdin.write(prompt)
        CHILD.stdin.close()

    if CHILD.stdout is not None:
        for line in CHILD.stdout:
            line = line.rstrip("\n")
            parsed_session = session_id_from_line(line)
            if parsed_session:
                session_id = parsed_session
                store.update(run_id, codex_session_id=session_id)
            snippet = event_tail_text(line, max_chars=1200)
            if snippet:
                tail = append_tail(tail, snippet)
                store.update(run_id, redacted_output_tail=tail)

    returncode = CHILD.wait()
    status = "completed" if returncode == 0 else "failed"
    store.update(
        run_id,
        status=status,
        returncode=returncode,
        redacted_output_tail=tail,
        codex_session_id=session_id,
    )
    return 0 if returncode == 0 else 1


def session_id_from_line(line: str) -> Optional[str]:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    if isinstance(event, dict):
        return extract_session_id(event)
    return None


def append_tail(existing: str, addition: str, limit: int = 4000) -> str:
    combined = (existing + "\n" + addition).strip() if existing else addition.strip()
    if len(combined) <= limit:
        return combined
    return combined[-limit:]


if __name__ == "__main__":
    raise SystemExit(main())
