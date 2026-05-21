from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
        dry_run=dry_run,
        allowed_senders=frozenset({"s"}),
        workspaces={"demo": Path(tmp)},
        default_workspace="demo",
        home=Path(tmp) / "home",
        codex_home=Path(tmp) / "codex_home",
        config_path=Path(tmp) / "config.env",
    )


def write_codex_index(cfg: Config, rows: list) -> None:
    cfg.codex_home.mkdir(parents=True, exist_ok=True)
    (cfg.codex_home / "session_index.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
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

    def test_status_one_falls_back_to_codex_session_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(tmp)
            session_id = "019e2ead-c907-7a13-8db8-2c9c14ca3e1b"
            write_codex_index(cfg, [
                {"id": session_id, "thread_name": "cli-thread", "updated_at": "2026-01-01"},
            ])
            controller = BridgeController(cfg, launcher=FakeLauncher())
            reply = controller.status_one(session_id)
            self.assertIn(session_id, reply)
            self.assertIn("cli-thread", reply)
            self.assertIn("not bridge-owned", reply)

    def test_status_one_resolves_thread_by_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(tmp)
            sid = "019e1651-b8cc-7f13-a96d-e569b7ede3a0"
            write_codex_index(cfg, [
                {"id": sid, "thread_name": "improve arbiter", "updated_at": "2026-05-14T23:27:48Z"},
            ])
            controller = BridgeController(cfg, launcher=FakeLauncher())
            reply = controller.status_one("improve arbiter")
            self.assertIn("improve arbiter", reply)
            self.assertIn(sid, reply)

    def test_status_one_lists_candidates_when_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(tmp)
            write_codex_index(cfg, [
                {"id": "019e0000-aaaa", "thread_name": "Inspect Arbiter pipeline", "updated_at": "t1"},
                {"id": "019e0000-bbbb", "thread_name": "improve arbiter", "updated_at": "t2"},
                {"id": "019e0000-cccc", "thread_name": "Review arbiter RFT pipeline", "updated_at": "t3"},
            ])
            controller = BridgeController(cfg, launcher=FakeLauncher())
            with mock.patch("codex_lark_minimal.bridge.live_session_ids", return_value=set()):
                reply = controller.status_one("arbiter")
            self.assertIn("Multiple Codex threads match", reply)
            self.assertIn("improve arbiter", reply)
            self.assertIn("Review arbiter RFT pipeline", reply)
            self.assertIn("Inspect Arbiter pipeline", reply)

    def test_status_one_reports_no_match_for_unknown_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(tmp)
            controller = BridgeController(cfg, launcher=FakeLauncher())
            reply = controller.status_one("nope")
            self.assertIn("No bridge run or Codex session matches", reply)
            self.assertIn("nope", reply)

    def test_status_one_prefers_bridge_record_over_codex_index(self):
        # If the same id exists in both stores (shouldn't happen — bridge
        # run_ids and Codex session_ids have different shapes — but the
        # bridge record wins anyway because it has more information).
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(tmp)
            shared = "clk_shared"
            StateStore(cfg).write(
                JobRecord(
                    run_id=shared,
                    status="completed",
                    workspace_alias="demo",
                    workspace_path=tmp,
                    prompt_sha256="abc",
                    prompt_preview="hi",
                )
            )
            write_codex_index(cfg, [
                {"id": shared, "thread_name": "shouldnt-show", "updated_at": "2026"},
            ])
            controller = BridgeController(cfg, launcher=FakeLauncher())
            reply = controller.status_one(shared)
            self.assertIn("[completed]", reply)
            self.assertNotIn("shouldnt-show", reply)

    def test_status_unified_list_marks_live_and_idle(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(tmp)
            live_id = "019e3645-a73a-71f3-8063-7a9cfd4e26a1"
            idle_id = "019e3647-6e9f-7a12-b86b-cbc92cf62e5e"
            write_codex_index(cfg, [
                {"id": live_id, "thread_name": "live-thread", "updated_at": "2026-01-02T00:00:00"},
                {"id": idle_id, "thread_name": "idle-thread", "updated_at": "2026-01-01T00:00:00"},
            ])
            controller = BridgeController(cfg, launcher=FakeLauncher())
            with mock.patch("codex_lark_minimal.bridge.live_session_ids", return_value={live_id}):
                reply = controller.status_summary()
            self.assertIn("Codex threads:", reply)
            # Compact rows show name + short-id, not the full UUID.
            self.assertIn("[running] live-thread", reply)
            self.assertIn(live_id.split("-", 1)[0], reply)
            self.assertIn("[idle] idle-thread", reply)
            self.assertIn(idle_id.split("-", 1)[0], reply)
            # Raw ISO timestamps are not surfaced in the new format.
            self.assertNotIn("2026-01-02T00:00:00", reply)

    def test_status_unified_renders_bridge_metadata_for_owned_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(tmp)
            owned = "019e3645-a73a-71f3-8063-7a9cfd4e26a1"
            StateStore(cfg).write(
                JobRecord(
                    run_id="clk_owned",
                    status="completed",
                    workspace_alias="demo",
                    workspace_path=tmp,
                    prompt_sha256="abc",
                    prompt_preview="bridge prompt preview",
                    codex_session_id=owned,
                    returncode=0,
                )
            )
            write_codex_index(cfg, [
                {"id": owned, "thread_name": "ignored-by-bridge-row", "updated_at": "2026-01-01"},
            ])
            controller = BridgeController(cfg, launcher=FakeLauncher())
            with mock.patch("codex_lark_minimal.bridge.live_session_ids", return_value=set()):
                reply = controller.status_summary()
            # Bridge metadata wins for owned rows.
            self.assertIn("[completed]", reply)
            self.assertIn("clk_owned", reply)
            self.assertIn("bridge prompt preview", reply)
            # No standalone [idle] row for the same session — bridge view replaces it.
            self.assertNotIn("ignored-by-bridge-row", reply)

    def test_status_unified_prepends_active_bridge_jobs_without_session_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(tmp)
            # Active bridge job that hasn't captured a session_id yet.
            StateStore(cfg).write(
                JobRecord(
                    run_id="clk_fresh",
                    status="running",
                    workspace_alias="demo",
                    workspace_path=tmp,
                    prompt_sha256="abc",
                    prompt_preview="just started",
                    pid=os.getpid(),
                )
            )
            write_codex_index(cfg, [
                {"id": "019e0000-0000-0000-0000-000000000000", "thread_name": "old", "updated_at": "2026"},
            ])
            controller = BridgeController(cfg, launcher=FakeLauncher())
            with mock.patch("codex_lark_minimal.bridge.live_session_ids", return_value=set()):
                reply = controller.status_summary()
            # Fresh active job appears even though Codex's index doesn't have it.
            self.assertIn("clk_fresh", reply)
            self.assertIn("just started", reply)
            # And the order: fresh-active row comes before the codex-index row.
            self.assertLess(reply.index("clk_fresh"), reply.index("old"))

    def test_status_surfaces_live_thread_outside_latest_five(self):
        """A live thread whose updated_at is stale must still show up."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(tmp)
            stale_live = "019e1651-b8cc-7f13-a96d-e569b7ede3a0"
            rows = [
                {"id": stale_live, "thread_name": "improve arbiter", "updated_at": "2026-01-01T00:00:00"},
            ]
            # Pad with six newer entries so `stale_live` falls outside the latest 5.
            for i in range(6):
                rows.append({
                    "id": "019e0000-0000-0000-0000-00000000000%d" % i,
                    "thread_name": "newer-%d" % i,
                    "updated_at": "2026-05-1%d" % i,
                })
            write_codex_index(cfg, rows)
            controller = BridgeController(cfg, launcher=FakeLauncher())
            with mock.patch("codex_lark_minimal.bridge.live_session_ids", return_value={stale_live}):
                reply = controller.status_summary()
            self.assertIn("improve arbiter", reply)
            self.assertIn("[running]", reply)
            # And it appears before the latest-5 block (live extras come first).
            self.assertLess(reply.index("improve arbiter"), reply.index("newer-5"))

    def test_status_unified_empty_when_no_threads_and_no_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config(tmp)
            controller = BridgeController(cfg, launcher=FakeLauncher())
            with mock.patch("codex_lark_minimal.bridge.live_session_ids", return_value=set()):
                reply = controller.status_summary()
            self.assertEqual(reply, "No recent Codex threads found.")

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
