from __future__ import annotations

from pathlib import Path
from unittest import mock
import os
import shutil
import tempfile
import unittest

from codex_lark_minimal.bridge import BridgeController, EventMeta, WorkerLauncher
from codex_lark_minimal.config import Config
from codex_lark_minimal.state import JobRecord, StateStore


class FakeLauncher:
    def __init__(self):
        self.calls = []

    def launch(self, run_id, task_text, *, mode, session_id=None):
        self.calls.append((run_id, task_text, mode, session_id))


def config(tmp: str, *, dry_run: bool = True) -> Config:
    return Config(
        app_id="cli_xxx",
        app_secret="secret",
        allow_all=True,
        dry_run=dry_run,
        allowed_senders=frozenset({"s"}),
        workspaces={"demo": Path(tmp)},
        default_workspace="demo",
        home=Path(tmp) / "home",
        config_path=Path(tmp) / "config.env",
    )


class BridgeTests(unittest.TestCase):
    def test_dry_run_start_does_not_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            launcher = FakeLauncher()
            controller = BridgeController(config(tmp), launcher=launcher)
            reply = controller.handle_text("codex demo: say hi", EventMeta(chat_id="c", sender_id="s"))
            self.assertIn("Dry run", reply)
            self.assertEqual(launcher.calls, [])

    def test_continue_refuses_running_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(tmp)
            store = StateStore(cfg)
            store.write(
                JobRecord(
                    run_id="clk_run",
                    status="running",
                    workspace_alias="demo",
                    workspace_path=tmp,
                    prompt_sha256="abc",
                    prompt_preview="hello",
                    pid=os.getpid(),
                    codex_session_id="019e2ead-c907-7a13-8db8-2c9c14ca3e1b",
                )
            )
            controller = BridgeController(cfg, launcher=FakeLauncher())
            reply = controller.handle_text("codex continue clk_run: next", EventMeta(chat_id="c", sender_id="s"))
            self.assertIn("still running", reply)

    def test_continue_completed_job_requires_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(tmp)
            store = StateStore(cfg)
            store.write(
                JobRecord(
                    run_id="clk_done",
                    status="completed",
                    workspace_alias="demo",
                    workspace_path=tmp,
                    prompt_sha256="abc",
                    prompt_preview="hello",
                )
            )
            controller = BridgeController(cfg, launcher=FakeLauncher())
            reply = controller.handle_text("codex continue clk_done: next", EventMeta(chat_id="c", sender_id="s"))
            self.assertIn("no captured Codex session id", reply)

    def test_continue_completed_job_starts_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(tmp, dry_run=False)
            launcher = FakeLauncher()
            store = StateStore(cfg)
            session_id = "019e2ead-c907-7a13-8db8-2c9c14ca3e1b"
            store.write(
                JobRecord(
                    run_id="clk_done",
                    status="completed",
                    workspace_alias="demo",
                    workspace_path=tmp,
                    prompt_sha256="abc",
                    prompt_preview="hello",
                    codex_session_id=session_id,
                )
            )
            controller = BridgeController(cfg, launcher=launcher)
            reply = controller.handle_text("codex continue clk_done: next", EventMeta(chat_id="c", sender_id="s"))
            self.assertIn("continuation started", reply)
            self.assertEqual(launcher.calls[0][2], "resume")
            self.assertEqual(launcher.calls[0][3], session_id)

    def test_start_rejects_missing_workspace(self):
        """Workspace deleted after daemon startup must not admit new jobs."""
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            cfg = Config(
                app_id="cli_xxx",
                app_secret="secret",
                allow_all=True,
                dry_run=True,
                allowed_senders=frozenset({"s"}),
                workspaces={"ws": workspace},
                default_workspace="ws",
                home=Path(tmp) / "home",
                config_path=Path(tmp) / "config.env",
            )
            launcher = FakeLauncher()
            controller = BridgeController(cfg, launcher=launcher)
            shutil.rmtree(workspace)
            reply = controller.handle_text("codex ws: do thing", EventMeta(chat_id="c", sender_id="s"))
            self.assertIn("no longer exists", reply)
            self.assertEqual(launcher.calls, [])

    def test_start_rejects_control_chars(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = BridgeController(config(tmp), launcher=FakeLauncher())
            reply = controller.handle_text(
                "codex demo: hello\x00world",
                EventMeta(chat_id="c", sender_id="s"),
            )
            self.assertIn("control characters", reply)
            self.assertEqual(StateStore(config(tmp)).list(), [])

    def test_continue_rejects_control_chars(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(tmp, dry_run=False)
            store = StateStore(cfg)
            store.write(
                JobRecord(
                    run_id="clk_done",
                    status="completed",
                    workspace_alias="demo",
                    workspace_path=tmp,
                    prompt_sha256="abc",
                    prompt_preview="hi",
                    codex_session_id="019e2ead-c907-7a13-8db8-2c9c14ca3e1b",
                )
            )
            controller = BridgeController(cfg, launcher=FakeLauncher())
            reply = controller.handle_text(
                "codex continue clk_done: bad\x01input",
                EventMeta(chat_id="c", sender_id="s"),
            )
            self.assertIn("control characters", reply)

    def test_launch_closes_log_handle_on_popen_error(self):
        """Popen raising must not leak the parent's log file handle."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(tmp, dry_run=False)
            StateStore(cfg).write(
                JobRecord(
                    run_id="clk_pf",
                    status="starting",
                    workspace_alias="demo",
                    workspace_path=tmp,
                    prompt_sha256="abc",
                    prompt_preview="hi",
                )
            )
            launcher = WorkerLauncher(cfg)
            with mock.patch("codex_lark_minimal.bridge.subprocess.Popen", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    launcher.launch("clk_pf", "task", mode="start")
            # The log file should exist (opened by the `with` block) and be
            # re-openable in write mode — meaning the previous handle was closed.
            with cfg.log_path.open("a", encoding="utf-8") as second:
                second.write("ok")


if __name__ == "__main__":
    unittest.main()
