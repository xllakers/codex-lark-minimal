from __future__ import annotations

import os
import sys
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


class ConfigureGuardTests(unittest.TestCase):
    """Configure refuses sensitive-shaped keys and malformed workspaces."""

    def _stub(self, path: Path):
        from types import SimpleNamespace

        return SimpleNamespace(config_path=path)

    def test_rejects_secret_via_set(self):
        from codex_lark_minimal.cli import configure_command

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            path = tmp / "config.env"
            path.touch()
            rc = configure_command(
                self._stub(path),
                set_kv=["FEISHU_APP_SECRET=leak_via_argv"],
                from_stdin=False,
            )
            self.assertEqual(rc, 2)
            # Nothing should have been written.
            self.assertEqual(path.read_text(encoding="utf-8"), "")

    def test_rejects_token_and_password_suffixes(self):
        from codex_lark_minimal.cli import configure_command

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            path = tmp / "config.env"
            path.touch()
            for key in ("SOMETHING_TOKEN", "DB_PASSWORD"):
                rc = configure_command(
                    self._stub(path),
                    set_kv=["%s=x" % key],
                    from_stdin=False,
                )
                self.assertEqual(rc, 2, msg=key)

    def test_rejects_malformed_workspaces(self):
        from codex_lark_minimal.cli import configure_command

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            path = tmp / "config.env"
            path.touch()
            rc = configure_command(
                self._stub(path),
                set_kv=["FEISHU_CODEX_WORKSPACES=missing_equals_sign"],
                from_stdin=False,
            )
            self.assertEqual(rc, 2)
            self.assertEqual(path.read_text(encoding="utf-8"), "")


class DiscoverJsonShapeTests(unittest.TestCase):
    """discover --json keeps a single consistent shape across success / empty / failure."""

    def _stub_config(self, *, with_creds: bool):
        from types import SimpleNamespace

        return SimpleNamespace(
            app_id="cli_x" if with_creds else None,
            app_secret="secret" if with_creds else None,
            domain="https://open.feishu.cn",
        )

    def test_missing_creds_returns_structured_error(self):
        import io
        import json

        from codex_lark_minimal.cli import discover_command

        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            rc = discover_command(self._stub_config(with_creds=False), timeout=10, as_json=True)
        finally:
            sys.stdout = old_stdout
        self.assertEqual(rc, 2)
        payload = json.loads(captured.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["timeout_seconds"], 10)
        self.assertIn("error", payload)

    def test_empty_events_returns_structured_error(self):
        import io
        import json
        from unittest.mock import patch

        from codex_lark_minimal.cli import discover_command

        async def fake_listen(*_args, **_kwargs):
            return ([], None)  # listener saw nothing

        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            with patch("codex_lark_minimal.setup._listen_for_events", side_effect=fake_listen):
                rc = discover_command(self._stub_config(with_creds=True), timeout=12, as_json=True)
        finally:
            sys.stdout = old_stdout
        self.assertEqual(rc, 1)
        payload = json.loads(captured.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["timeout_seconds"], 12)
        self.assertIn("12s", payload["error"])

    def test_success_returns_events_and_timeout(self):
        import io
        import json
        from unittest.mock import patch

        from codex_lark_minimal.bridge import EventMeta
        from codex_lark_minimal.cli import discover_command

        async def fake_listen(*_args, **_kwargs):
            return ([(EventMeta(sender_id="ou_a", chat_id="oc_b"), "hello world")], None)

        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            with patch("codex_lark_minimal.setup._listen_for_events", side_effect=fake_listen):
                rc = discover_command(self._stub_config(with_creds=True), timeout=30, as_json=True)
        finally:
            sys.stdout = old_stdout
        self.assertEqual(rc, 0)
        payload = json.loads(captured.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["timeout_seconds"], 30)
        self.assertEqual(len(payload["events"]), 1)
        self.assertEqual(payload["events"][0]["sender_id"], "ou_a")
        self.assertEqual(payload["events"][0]["chat_id"], "oc_b")
        self.assertIn("hello world", payload["events"][0]["text_preview"])


class DiscoverHandshakeTests(unittest.TestCase):
    """--handshake-token: auto-pick safe, reply-test surfaces send-permission failures."""

    def _stub_config(self):
        from types import SimpleNamespace

        return SimpleNamespace(
            app_id="cli_x", app_secret="secret", domain="https://open.feishu.cn"
        )

    def _run_with_listener(self, fake_listen, *, handshake_token):
        import io
        import json
        from unittest.mock import patch

        from codex_lark_minimal.cli import discover_command

        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            with patch("codex_lark_minimal.setup._listen_for_events", side_effect=fake_listen):
                rc = discover_command(
                    self._stub_config(), timeout=30, as_json=True, handshake_token=handshake_token
                )
        finally:
            sys.stdout = old_stdout
        return rc, json.loads(captured.getvalue())

    def test_handshake_success_sets_reply_verified(self):
        from codex_lark_minimal.bridge import EventMeta

        async def fake_listen(*_args, **kwargs):
            self.assertEqual(kwargs.get("handshake_token"), "t_abc123")
            return ([(EventMeta(sender_id="ou_a", chat_id="oc_b"), "codex-lark setup t_abc123")], {"ok": True})

        rc, payload = self._run_with_listener(fake_listen, handshake_token="t_abc123")
        self.assertEqual(rc, 0)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["reply_verified"])
        self.assertEqual(len(payload["events"]), 1)

    def test_handshake_reply_failure_surfaces_as_ok_false(self):
        from codex_lark_minimal.bridge import EventMeta

        async def fake_listen(*_args, **_kwargs):
            return (
                [(EventMeta(sender_id="ou_a", chat_id="oc_b"), "codex-lark setup t_xyz")],
                {"ok": False, "error": "permission denied: im:message:send_as_bot"},
            )

        rc, payload = self._run_with_listener(fake_listen, handshake_token="t_xyz")
        self.assertEqual(rc, 2)
        self.assertFalse(payload["ok"])
        self.assertIn("reply send failed", payload["error"])
        self.assertIn("im:message:send_as_bot", payload["error"])
        # Captured event is still surfaced so the agent can show the human.
        self.assertEqual(payload["events"][0]["sender_id"], "ou_a")

    def test_handshake_no_match_uses_specific_error(self):
        async def fake_listen(*_args, **_kwargs):
            return ([], None)

        rc, payload = self._run_with_listener(fake_listen, handshake_token="t_none")
        self.assertEqual(rc, 1)
        self.assertFalse(payload["ok"])
        self.assertIn("handshake token", payload["error"])


class ConfigureDomainAutoDetectTests(unittest.TestCase):
    """Domain auto-detect probes both endpoints and writes the working one."""

    def test_auto_detect_e2e_via_set_and_stdin(self):
        import io
        from types import SimpleNamespace
        from unittest.mock import patch

        from codex_lark_minimal.cli import configure_command

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            path = tmp / "config.env"
            path.touch()
            stub = SimpleNamespace(config_path=path)

            def fake_probe(_aid, _sec, candidate):
                return (candidate == "https://open.larksuite.com", "")

            old_stdin = sys.stdin
            sys.stdin = io.StringIO("FEISHU_APP_SECRET=topsecret\n")
            try:
                with patch("codex_lark_minimal.setup.feishu_token_check_proxy", side_effect=fake_probe):
                    rc = configure_command(
                        stub,
                        set_kv=["FEISHU_APP_ID=cli_x"],
                        from_stdin=True,
                    )
            finally:
                sys.stdin = old_stdin
            self.assertEqual(rc, 0)
            text = path.read_text(encoding="utf-8")
            self.assertIn("FEISHU_DOMAIN=https://open.larksuite.com", text)
            self.assertIn("FEISHU_APP_SECRET=topsecret", text)

    def test_auto_detect_skipped_when_domain_already_set(self):
        import io
        from types import SimpleNamespace
        from unittest.mock import patch

        from codex_lark_minimal.cli import configure_command

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            path = tmp / "config.env"
            path.touch()
            stub = SimpleNamespace(config_path=path)

            probe_calls = []

            def fake_probe(*args):
                probe_calls.append(args)
                return True, ""

            old_stdin = sys.stdin
            sys.stdin = io.StringIO("FEISHU_APP_SECRET=s\n")
            try:
                with patch("codex_lark_minimal.setup.feishu_token_check_proxy", side_effect=fake_probe):
                    rc = configure_command(
                        stub,
                        set_kv=["FEISHU_APP_ID=cli_x", "FEISHU_DOMAIN=https://open.feishu.cn"],
                        from_stdin=True,
                    )
            finally:
                sys.stdin = old_stdin
            self.assertEqual(rc, 0)
            self.assertEqual(probe_calls, [])  # auto-detect did not run


class ConfigureCommandTests(unittest.TestCase):
    """`codex-lark configure` writes an append-block that load_config picks up."""

    def test_configure_round_trip(self):
        from types import SimpleNamespace

        from codex_lark_minimal.cli import configure_command

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            ws = tmp / "ws"
            ws.mkdir()
            path = write_env(tmp, "FEISHU_CODEX_WORKSPACES=demo=%s\n" % ws)
            # Use a stub Config with only what configure_command reads.
            stub = SimpleNamespace(config_path=path)
            rc = configure_command(
                stub,
                set_kv=["FEISHU_APP_ID=cli_x", "FEISHU_CODEX_ALLOWED_SENDERS=ou_abc"],
                from_stdin=False,
            )
            self.assertEqual(rc, 0)
            text = path.read_text(encoding="utf-8")
            self.assertIn("FEISHU_APP_ID=cli_x", text)
            self.assertIn("FEISHU_CODEX_ALLOWED_SENDERS=ou_abc", text)
            self.assertIn("# --- codex-lark setup ", text)

            cfg = load_config(path)
            self.assertEqual(cfg.app_id, "cli_x")
            self.assertIn("ou_abc", cfg.allowed_senders)
            self.assertFalse(cfg.dry_run)  # populated allowlist ⇒ real mode

    def test_configure_stdin_reads_pairs(self):
        import io
        from types import SimpleNamespace

        from codex_lark_minimal.cli import configure_command

        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            path = tmp / "config.env"
            path.touch()
            stub = SimpleNamespace(config_path=path)
            old_stdin = sys.stdin
            sys.stdin = io.StringIO("FEISHU_APP_SECRET=topsecret\n# comment\n\nEMPTY=\n")
            try:
                rc = configure_command(stub, set_kv=[], from_stdin=True)
            finally:
                sys.stdin = old_stdin
            self.assertEqual(rc, 0)
            text = path.read_text(encoding="utf-8")
            self.assertIn("FEISHU_APP_SECRET=topsecret", text)
            self.assertNotIn("# comment", text)


if __name__ == "__main__":
    unittest.main()
