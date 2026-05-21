from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from codex_lark_minimal.config import load_config
from codex_lark_minimal.state import JobRecord, StateStore


def _write_fake_codex(path: Path, session_id: str = "019e2ead-c907-7a13-8db8-2c9c14ca3e1b") -> None:
    path.write_text(
        "#!/usr/bin/env bash\n"
        "cat >/dev/null\n"
        "echo '{\"type\":\"thread.started\",\"thread_id\":\"%s\"}'\n"
        "echo '{\"type\":\"agent_message\",\"text\":\"done\"}'\n" % session_id,
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _write_worker_config(
    config_path: Path,
    *,
    root: Path,
    fake_codex: Path,
    codex_home: Path,
    extra: str = "",
) -> None:
    lines = [
        "FEISHU_CODEX_HOME=%s" % (root / "home"),
        "CODEX_HOME=%s" % codex_home,
        "FEISHU_CODEX_WORKSPACES=demo=%s" % root,
        "FEISHU_CODEX_DEFAULT_WORKSPACE=demo",
        "FEISHU_CODEX_CODEX_BIN=%s" % fake_codex,
        "FEISHU_CODEX_DRY_RUN=0",
        "FEISHU_APP_ID=cli_test",
        "FEISHU_APP_SECRET=unit_test_placeholder",
        "FEISHU_CODEX_ALLOWED_SENDERS=s",
    ]
    if extra:
        lines.append(extra)
    config_path.write_text("\n".join(lines), encoding="utf-8")


def _run_worker(config_path: Path, run_id: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "codex_lark_minimal.worker",
            "--config",
            str(config_path),
            "--run-id",
            run_id,
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


class WorkerTests(unittest.TestCase):
    def test_worker_captures_codex_session_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_codex = root / "fake-codex"
            _write_fake_codex(fake_codex)
            config_path = root / "config.env"
            # CODEX_HOME pinned to the tempdir so the worker's index append
            # can't escape into the real ~/.codex during testing.
            _write_worker_config(
                config_path,
                root=root,
                fake_codex=fake_codex,
                codex_home=root / "codex_home",
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
            completed = _run_worker(config_path, "clk_worker")
            self.assertEqual(completed.returncode, 0, completed.stdout)
            record = store.get("clk_worker")
            self.assertEqual(record.status, "completed")
            self.assertEqual(record.codex_session_id, "019e2ead-c907-7a13-8db8-2c9c14ca3e1b")
            self.assertIn("done", record.redacted_output_tail)

    def test_worker_appends_session_index_after_capture(self):
        """End-to-end: a real worker invocation produces a session_index.jsonl
        row carrying the captured session_id and the (redacted) prompt preview.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_codex = root / "fake-codex"
            session_id = "019e3878-ffad-7022-995d-c62e10ca73d1"
            _write_fake_codex(fake_codex, session_id=session_id)
            codex_home = root / "codex_home"
            config_path = root / "config.env"
            _write_worker_config(
                config_path,
                root=root,
                fake_codex=fake_codex,
                codex_home=codex_home,
            )
            cfg = load_config(config_path)
            StateStore(cfg).write(
                JobRecord(
                    run_id="clk_worker",
                    status="starting",
                    workspace_alias="demo",
                    workspace_path=str(root),
                    prompt_sha256="abc",
                    prompt_preview="investigate flaky run",
                )
            )
            completed = _run_worker(config_path, "clk_worker")
            self.assertEqual(completed.returncode, 0, completed.stdout)
            index_path = codex_home / "session_index.jsonl"
            self.assertTrue(index_path.exists(), "session_index.jsonl not created")
            rows = [
                json.loads(line)
                for line in index_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            matches = [row for row in rows if row.get("id") == session_id]
            self.assertEqual(len(matches), 1, "expected exactly one row for our session id")
            self.assertEqual(matches[0]["thread_name"], "investigate flaky run")
            self.assertTrue(matches[0]["updated_at"], "updated_at must be set")

    def test_worker_does_not_append_when_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_codex = root / "fake-codex"
            _write_fake_codex(fake_codex)
            codex_home = root / "codex_home"
            config_path = root / "config.env"
            _write_worker_config(
                config_path,
                root=root,
                fake_codex=fake_codex,
                codex_home=codex_home,
                extra="FEISHU_CODEX_SESSION_INDEX_APPEND=0",
            )
            cfg = load_config(config_path)
            StateStore(cfg).write(
                JobRecord(
                    run_id="clk_worker",
                    status="starting",
                    workspace_alias="demo",
                    workspace_path=str(root),
                    prompt_sha256="abc",
                    prompt_preview="hello",
                )
            )
            completed = _run_worker(config_path, "clk_worker")
            self.assertEqual(completed.returncode, 0, completed.stdout)
            self.assertFalse(
                (codex_home / "session_index.jsonl").exists(),
                "session_index.jsonl must not be created when append is disabled",
            )


if __name__ == "__main__":
    unittest.main()
