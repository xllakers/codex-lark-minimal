from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_lark_minimal.config import load_config, validate_config


def write_env(tmp: Path, body: str) -> Path:
    path = tmp / "config.env"
    path.write_text(body, encoding="utf-8")
    return path


class ConfigDryRunTests(unittest.TestCase):
    """Empty allowlist ⇒ dry-run automatically; explicit FEISHU_CODEX_DRY_RUN wins."""

    def _isolate_env(self) -> mock._patch:
        # Strip any FEISHU_/LARK_/CODEX_ env vars the user may have set so the
        # test reads only what we put in the file.
        cleaned = {
            k: v
            for k, v in os.environ.items()
            if not (k.startswith("FEISHU_") or k.startswith("LARK_") or k == "CODEX_HOME")
        }
        return mock.patch.dict(os.environ, cleaned, clear=True)

    def test_empty_allowlist_derives_dry_run(self):
        with tempfile.TemporaryDirectory() as raw, self._isolate_env():
            tmp = Path(raw)
            ws = tmp / "ws"
            ws.mkdir()
            path = write_env(
                tmp,
                "FEISHU_APP_ID=cli_x\n"
                "FEISHU_APP_SECRET=secret\n"
                "FEISHU_CODEX_WORKSPACES=demo=%s\n" % ws,
            )
            cfg = load_config(path)
            self.assertTrue(cfg.dry_run)

    def test_populated_allowlist_derives_real_mode(self):
        with tempfile.TemporaryDirectory() as raw, self._isolate_env():
            tmp = Path(raw)
            ws = tmp / "ws"
            ws.mkdir()
            path = write_env(
                tmp,
                "FEISHU_APP_ID=cli_x\n"
                "FEISHU_APP_SECRET=secret\n"
                "FEISHU_CODEX_WORKSPACES=demo=%s\n"
                "FEISHU_CODEX_ALLOWED_SENDERS=ou_abc\n" % ws,
            )
            cfg = load_config(path)
            self.assertFalse(cfg.dry_run)
            self.assertIn("ou_abc", cfg.allowed_senders)

    def test_explicit_dry_run_overrides_allowlist(self):
        with tempfile.TemporaryDirectory() as raw, self._isolate_env():
            tmp = Path(raw)
            ws = tmp / "ws"
            ws.mkdir()
            path = write_env(
                tmp,
                "FEISHU_APP_ID=cli_x\n"
                "FEISHU_APP_SECRET=secret\n"
                "FEISHU_CODEX_WORKSPACES=demo=%s\n"
                "FEISHU_CODEX_ALLOWED_SENDERS=ou_abc\n"
                "FEISHU_CODEX_DRY_RUN=1\n" % ws,
            )
            cfg = load_config(path)
            self.assertTrue(cfg.dry_run)

    def test_validate_real_mode_requires_creds_and_allowlist(self):
        with tempfile.TemporaryDirectory() as raw, self._isolate_env():
            tmp = Path(raw)
            ws = tmp / "ws"
            ws.mkdir()
            # Real-mode (allowlist non-empty) but no creds.
            path = write_env(
                tmp,
                "FEISHU_CODEX_WORKSPACES=demo=%s\n"
                "FEISHU_CODEX_ALLOWED_SENDERS=ou_abc\n" % ws,
            )
            cfg = load_config(path)
            errors = validate_config(cfg)
            self.assertFalse(cfg.dry_run)
            self.assertTrue(any("FEISHU_APP_ID" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
