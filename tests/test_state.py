from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
