from __future__ import annotations

import unittest

from codex_lark_minimal.redaction import redact


class RedactionTests(unittest.TestCase):
    def test_shell_assignment(self):
        line = "TOKEN=ghp_abcdef12345"
        out = redact(line)
        self.assertNotIn("ghp_abcdef12345", out)
        self.assertIn("<redacted>", out)

    def test_export_keyword(self):
        line = "export API_KEY=sk-live-001122"
        out = redact(line)
        self.assertNotIn("sk-live-001122", out)
        self.assertIn("<redacted>", out)

    def test_shell_var_ref(self):
        line = "Authorization: $TOKEN"
        out = redact(line)
        self.assertNotIn("$TOKEN", out)
        self.assertIn("$<redacted>", out)

    def test_shell_var_braced(self):
        line = "use ${SECRET} here"
        out = redact(line)
        self.assertNotIn("${SECRET}", out)
        self.assertIn("$<redacted>", out)

    def test_password_assign(self):
        line = "  password = hunter2"
        out = redact(line)
        self.assertNotIn("hunter2", out)

    def test_existing_token_equals_still_redacted(self):
        # Regression: F5 must not break the original SECRET_RE behavior.
        line = "token=abc123"
        out = redact(line)
        self.assertNotIn("abc123", out)

    def test_non_secret_text_untouched(self):
        line = "just talking about a token in prose"
        out = redact(line)
        self.assertIn("just talking", out)


if __name__ == "__main__":
    unittest.main()
