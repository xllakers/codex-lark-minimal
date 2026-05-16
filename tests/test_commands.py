from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_lark_minimal.commands import parse_message
from codex_lark_minimal.config import Config


def config(tmp: str) -> Config:
    return Config(
        app_id=None,
        app_secret=None,
        allow_all=True,
        dry_run=True,
        workspaces={"agent-foundry": Path(tmp), "opencode": Path(tmp)},
        default_workspace="agent-foundry",
        home=Path(tmp) / "home",
    )


class CommandTests(unittest.TestCase):
    def test_start_with_workspace_alias(self):
        with tempfile.TemporaryDirectory() as tmp:
            parsed = parse_message("codex opencode: inspect README", config(tmp))
        self.assertEqual(parsed.kind, "start")
        self.assertEqual(parsed.workspace_alias, "opencode")
        self.assertEqual(parsed.task_text, "inspect README")

    def test_start_uses_default_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            parsed = parse_message("codex run tests", config(tmp))
        self.assertEqual(parsed.kind, "start")
        self.assertEqual(parsed.workspace_alias, "agent-foundry")
        self.assertEqual(parsed.task_text, "run tests")

    def test_status_stop_continue_recent(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(tmp)
            self.assertEqual(parse_message("codex status", cfg).kind, "status")
            self.assertEqual(parse_message("codex status clk_abc", cfg).run_id, "clk_abc")
            self.assertEqual(parse_message("codex stop clk_abc", cfg).kind, "stop")
            cont = parse_message("codex continue clk_abc: do next", cfg)
            self.assertEqual(cont.kind, "continue")
            self.assertEqual(cont.run_id, "clk_abc")
            self.assertEqual(cont.task_text, "do next")
            self.assertEqual(parse_message("codex recent", cfg).kind, "recent")

    def test_ignores_without_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(parse_message("hello", config(tmp)))

    def test_start_preserves_multiline_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = "codex opencode: please refactor this:\ndef foo():\n    pass"
            parsed = parse_message(text, config(tmp))
        self.assertEqual(parsed.kind, "start")
        self.assertEqual(parsed.workspace_alias, "opencode")
        self.assertEqual(
            parsed.task_text,
            "please refactor this:\ndef foo():\n    pass",
        )

    def test_start_preserves_internal_whitespace_with_default_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = "codex agent-foundry: line1\n\n  line2"
            parsed = parse_message(text, config(tmp))
        self.assertEqual(parsed.kind, "start")
        self.assertEqual(parsed.task_text, "line1\n\n  line2")

    def test_continue_preserves_multiline_instruction(self):
        with tempfile.TemporaryDirectory() as tmp:
            text = "codex continue clk_abc: code:\n  def foo()\n  return 1"
            parsed = parse_message(text, config(tmp))
        self.assertEqual(parsed.kind, "continue")
        self.assertEqual(parsed.run_id, "clk_abc")
        self.assertEqual(parsed.task_text, "code:\n  def foo()\n  return 1")


if __name__ == "__main__":
    unittest.main()
