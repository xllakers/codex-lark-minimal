from __future__ import annotations

import json
import unittest
from pathlib import Path

from codex_lark_minimal.codex import (
    build_exec_command,
    build_resume_command,
    event_tail_text,
    extract_session_id,
)
from codex_lark_minimal.config import Config


class CodexParsingTests(unittest.TestCase):
    def test_extracts_session_id_from_nested_payload(self):
        event = {"type": "thread.started", "payload": {"thread_id": "019e2ead-c907-7a13-8db8-2c9c14ca3e1b"}}
        self.assertEqual(extract_session_id(event), "019e2ead-c907-7a13-8db8-2c9c14ca3e1b")

    def test_event_tail_redacts_secret(self):
        line = json.dumps({"type": "agent_message", "text": "token=abc123 hello"})
        self.assertNotIn("abc123", event_tail_text(line))


class CodexCommandShapeTests(unittest.TestCase):
    """Pin the codex flag order. codex 0.130+ rejected the old layout with
    'unexpected argument --ask-for-approval'; lock the new layout in."""

    def _config(self) -> Config:
        return Config(
            app_id=None,
            app_secret=None,
            codex_bin="codex",
            codex_sandbox="workspace-write",
            codex_approval="never",
        )

    def test_exec_command_puts_top_level_flags_before_exec(self):
        cmd = build_exec_command(self._config(), Path("/tmp/ws"))
        exec_idx = cmd.index("exec")
        # Top-level flags must appear before `exec`.
        self.assertLess(cmd.index("--sandbox"), exec_idx)
        self.assertLess(cmd.index("--ask-for-approval"), exec_idx)
        # `--color` is an exec subcommand flag; must come after.
        self.assertGreater(cmd.index("--color"), exec_idx)
        # stdin marker is last so prompt-stdin works.
        self.assertEqual(cmd[-1], "-")

    def test_resume_command_puts_top_level_flags_before_exec(self):
        cmd = build_resume_command(self._config(), "00000000-0000-4000-8000-000000000000")
        exec_idx = cmd.index("exec")
        self.assertLess(cmd.index("--sandbox"), exec_idx)
        self.assertLess(cmd.index("--ask-for-approval"), exec_idx)
        # resume positionals: session_id then "-" (stdin prompt) at the end.
        self.assertEqual(cmd[-2], "00000000-0000-4000-8000-000000000000")
        self.assertEqual(cmd[-1], "-")


if __name__ == "__main__":
    unittest.main()
