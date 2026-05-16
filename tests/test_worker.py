from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from codex_lark_minimal.config import load_config
from codex_lark_minimal.state import JobRecord, StateStore


class WorkerTests(unittest.TestCase):
    def test_worker_captures_codex_session_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_codex = root / "fake-codex"
            fake_codex.write_text(
                "#!/usr/bin/env bash\n"
                "cat >/dev/null\n"
                "echo '{\"type\":\"thread.started\",\"thread_id\":\"019e2ead-c907-7a13-8db8-2c9c14ca3e1b\"}'\n"
                "echo '{\"type\":\"agent_message\",\"text\":\"done\"}'\n",
                encoding="utf-8",
            )
            fake_codex.chmod(fake_codex.stat().st_mode | stat.S_IXUSR)
            config_path = root / "config.env"
            config_path.write_text(
                "\n".join(
                    [
                        "FEISHU_CODEX_HOME=%s" % (root / "home"),
                        "FEISHU_CODEX_WORKSPACES=demo=%s" % root,
                        "FEISHU_CODEX_DEFAULT_WORKSPACE=demo",
                        "FEISHU_CODEX_CODEX_BIN=%s" % fake_codex,
                        "FEISHU_CODEX_DRY_RUN=0",
                        "FEISHU_APP_ID=cli_test",
                        "FEISHU_APP_SECRET=unit_test_placeholder",
                        "FEISHU_CODEX_ALLOWED_SENDERS=s",
                    ]
                ),
                encoding="utf-8",
            )
            cfg = load_config(config_path)
            store = StateStore(cfg)
            store.write(
                JobRecord(
                    run_id="clk_worker",
                    status="starting",
                    workspace_alias="demo",
                    workspace_path=str(root),
                    prompt_sha256="abc",
                    prompt_preview="hello",
                )
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "codex_lark_minimal.worker",
                    "--config",
                    str(config_path),
                    "--run-id",
                    "clk_worker",
                    "--mode",
                    "start",
                ],
                input="hello",
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                timeout=20,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout)
            record = store.get("clk_worker")
            self.assertEqual(record.status, "completed")
            self.assertEqual(record.codex_session_id, "019e2ead-c907-7a13-8db8-2c9c14ca3e1b")
            self.assertIn("done", record.redacted_output_tail)


if __name__ == "__main__":
    unittest.main()
