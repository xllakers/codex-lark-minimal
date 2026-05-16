from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from codex_lark_minimal.config import Config
from codex_lark_minimal.state import JobRecord, StateStore


def config(tmp: str) -> Config:
    return Config(
        app_id=None,
        app_secret=None,
        dry_run=True,
        workspaces={"demo": Path(tmp)},
        default_workspace="demo",
        home=Path(tmp) / "home",
    )


class StateTests(unittest.TestCase):
    def test_write_get_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(config(tmp))
            record = JobRecord(
                run_id="clk_test",
                status="dry_run",
                workspace_alias="demo",
                workspace_path=tmp,
                prompt_sha256="abc",
                prompt_preview="hello",
            )
            store.write(record)
            self.assertEqual(store.get("clk_test").status, "dry_run")
            store.update("clk_test", status="completed", returncode=0)
            self.assertEqual(store.get("clk_test").returncode, 0)

    def test_refresh_marks_dead_pid_lost(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(config(tmp))
            store.write(
                JobRecord(
                    run_id="clk_dead",
                    status="running",
                    workspace_alias="demo",
                    workspace_path=tmp,
                    prompt_sha256="abc",
                    prompt_preview="hello",
                    pid=99999999,
                )
            )
            self.assertEqual(store.get("clk_dead").status, "lost")

    def test_update_rejects_immutable_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(config(tmp))
            store.write(
                JobRecord(
                    run_id="clk_imm",
                    status="dry_run",
                    workspace_alias="demo",
                    workspace_path=tmp,
                    prompt_sha256="abc",
                    prompt_preview="hi",
                )
            )
            with self.assertRaises(ValueError):
                store.update("clk_imm", created_at="2000-01-01")
            # `run_id` is also rejected via Python's own duplicate-argument check,
            # which is fine — both raise before any mutation happens.
            with self.assertRaises((TypeError, ValueError)):
                store.update("clk_imm", **{"run_id": "clk_other"})
            self.assertEqual(store.get("clk_imm").run_id, "clk_imm")

    def test_concurrent_updates_preserve_disjoint_fields(self):
        """Two writers updating disjoint fields under flock should not clobber each other."""
        with tempfile.TemporaryDirectory() as tmp:
            store = StateStore(config(tmp))
            store.write(
                JobRecord(
                    run_id="clk_race",
                    status="starting",
                    workspace_alias="demo",
                    workspace_path=tmp,
                    prompt_sha256="abc",
                    prompt_preview="hi",
                )
            )
            errors = []

            def writer_a():
                try:
                    for i in range(100):
                        store.update("clk_race", pid=1000 + i)
                except Exception as exc:
                    errors.append(exc)

            def writer_b():
                try:
                    for i in range(100):
                        store.update("clk_race", command_preview="cmd-%d" % i)
                except Exception as exc:
                    errors.append(exc)

            t_a = threading.Thread(target=writer_a)
            t_b = threading.Thread(target=writer_b)
            t_a.start()
            t_b.start()
            t_a.join()
            t_b.join()

            self.assertEqual(errors, [])
            final = store.get("clk_race")
            self.assertEqual(final.pid, 1099, "writer_a's final pid must persist")
            self.assertEqual(final.command_preview, "cmd-99", "writer_b's final preview must persist")


if __name__ == "__main__":
    unittest.main()
