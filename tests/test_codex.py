from __future__ import annotations

import json
import unittest

from codex_lark_minimal.codex import event_tail_text, extract_session_id


class CodexParsingTests(unittest.TestCase):
    def test_extracts_session_id_from_nested_payload(self):
        event = {"type": "thread.started", "payload": {"thread_id": "019e2ead-c907-7a13-8db8-2c9c14ca3e1b"}}
        self.assertEqual(extract_session_id(event), "019e2ead-c907-7a13-8db8-2c9c14ca3e1b")

    def test_event_tail_redacts_secret(self):
        line = json.dumps({"type": "agent_message", "text": "token=abc123 hello"})
        self.assertNotIn("abc123", event_tail_text(line))


if __name__ == "__main__":
    unittest.main()
