from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codex_lark_minimal.codex import (
    _SESSION_INDEX_MAX_LINE_BYTES,
    append_session_index_entry,
    build_exec_command,
    build_resume_command,
    event_tail_text,
    extract_session_id,
    extract_thread_highlights,
    find_session,
    format_codex_thread_status,
    humanize_time,
    live_session_ids,
    read_rollout_tail,
    resolve_session,
    rollout_path_for,
)
from codex_lark_minimal.config import Config


class CodexParsingTests(unittest.TestCase):
    def test_extracts_session_id_from_nested_payload(self):
        event = {"type": "thread.started", "payload": {"thread_id": "019e2ead-c907-7a13-8db8-2c9c14ca3e1b"}}
        self.assertEqual(extract_session_id(event), "019e2ead-c907-7a13-8db8-2c9c14ca3e1b")

    def test_event_tail_redacts_secret(self):
        line = json.dumps({"type": "agent_message", "text": "token=abc123 hello"})
        self.assertNotIn("abc123", event_tail_text(line))


class CodexCommandShapeTests(unittest.TestCase):
    """Pin the codex flag order. codex 0.130+ rejected the old layout with
    'unexpected argument --ask-for-approval'; lock the new layout in."""

    def _config(self) -> Config:
        return Config(
            app_id=None,
            app_secret=None,
            codex_bin="codex",
            codex_sandbox="workspace-write",
            codex_approval="never",
        )

    def test_exec_command_puts_top_level_flags_before_exec(self):
        cmd = build_exec_command(self._config(), Path("/tmp/ws"))
        exec_idx = cmd.index("exec")
        # Top-level flags must appear before `exec`.
        self.assertLess(cmd.index("--sandbox"), exec_idx)
        self.assertLess(cmd.index("--ask-for-approval"), exec_idx)
        # `--color` is an exec subcommand flag; must come after.
        self.assertGreater(cmd.index("--color"), exec_idx)
        # stdin marker is last so prompt-stdin works.
        self.assertEqual(cmd[-1], "-")

    def test_resume_command_puts_top_level_flags_before_exec(self):
        cmd = build_resume_command(self._config(), "00000000-0000-4000-8000-000000000000")
        exec_idx = cmd.index("exec")
        self.assertLess(cmd.index("--sandbox"), exec_idx)
        self.assertLess(cmd.index("--ask-for-approval"), exec_idx)
        # resume positionals: session_id then "-" (stdin prompt) at the end.
        self.assertEqual(cmd[-2], "00000000-0000-4000-8000-000000000000")
        self.assertEqual(cmd[-1], "-")


class LiveSessionIdsTests(unittest.TestCase):
    def _lsof_output(self, *paths: str) -> str:
        # `lsof -Fn -c codex` interleaves p/c/f/n lines. Only `n` lines (the
        # filename field) carry rollout paths; everything else is ignored.
        lines = ["p1234", "ccodex"]
        for path in paths:
            lines.append("n" + path)
        return "\n".join(lines) + "\n"

    def test_extracts_session_ids_from_rollout_paths(self):
        base = "/Users/x/.codex/sessions/2026/05/17/rollout-2026-05-17T22-"
        rollout_a = base + "09-55-019e3645-a73a-71f3-8063-7a9cfd4e26a1.jsonl"
        rollout_b = base + "11-51-019e3647-6e9f-7a12-b86b-cbc92cf62e5e.jsonl"
        result = mock.Mock(stdout=self._lsof_output(rollout_a, rollout_b, "/etc/passwd"))
        with mock.patch("codex_lark_minimal.codex.subprocess.run", return_value=result):
            ids = live_session_ids()
        self.assertEqual(
            ids,
            {
                "019e3645-a73a-71f3-8063-7a9cfd4e26a1",
                "019e3647-6e9f-7a12-b86b-cbc92cf62e5e",
            },
        )

    def test_returns_empty_when_lsof_missing(self):
        with mock.patch("codex_lark_minimal.codex.subprocess.run", side_effect=FileNotFoundError):
            self.assertEqual(live_session_ids(), set())

    def test_returns_empty_when_lsof_times_out(self):
        with mock.patch(
            "codex_lark_minimal.codex.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="lsof", timeout=2),
        ):
            self.assertEqual(live_session_ids(), set())

    def test_ignores_non_rollout_filenames(self):
        result = mock.Mock(stdout=self._lsof_output("/tmp/random.log", "/var/db/something.sqlite"))
        with mock.patch("codex_lark_minimal.codex.subprocess.run", return_value=result):
            self.assertEqual(live_session_ids(), set())


class FindSessionTests(unittest.TestCase):
    def test_returns_row_for_matching_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "session_index.jsonl").write_text(
                json.dumps({"id": "a", "thread_name": "alpha", "updated_at": "t1"}) + "\n"
                + json.dumps({"id": "b", "thread_name": "beta", "updated_at": "t2"}) + "\n",
                encoding="utf-8",
            )
            cfg = Config(app_id=None, app_secret=None, codex_home=home)
            row = find_session(cfg, "b")
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row["thread_name"], "beta")

    def test_returns_none_when_index_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(app_id=None, app_secret=None, codex_home=Path(tmp))
            self.assertIsNone(find_session(cfg, "anything"))

    def test_keeps_latest_entry_when_id_is_duplicated(self):
        """Codex appends a fresh line on rename; the latest one is canonical."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "session_index.jsonl").write_text(
                json.dumps({"id": "x", "thread_name": "old-name", "updated_at": "t1"}) + "\n"
                + json.dumps({"id": "x", "thread_name": "new-name", "updated_at": "t2"}) + "\n",
                encoding="utf-8",
            )
            cfg = Config(app_id=None, app_secret=None, codex_home=home)
            row = find_session(cfg, "x")
            assert row is not None
            self.assertEqual(row["thread_name"], "new-name")

    def test_format_codex_thread_status_handles_missing_fields(self):
        rendered = format_codex_thread_status({"id": "abc", "thread_name": "", "updated_at": ""})
        self.assertIn("id: abc", rendered)
        self.assertIn("(unnamed)", rendered)
        self.assertIn("(unknown)", rendered)
        self.assertIn("codex exec resume abc", rendered)


class HumanizeTimeTests(unittest.TestCase):
    def test_handles_odd_fractional_precision(self):
        """Codex writes 4–5 digit microseconds; pre-3.11 fromisoformat would
        reject them, so humanize_time normalizes the precision first."""
        # Should not return the raw string (which would mean parse failed).
        result = humanize_time("2026-05-15T12:11:16.25168Z")
        self.assertNotEqual(result, "2026-05-15T12:11:16.25168Z")

    def test_empty_returns_placeholder(self):
        self.assertEqual(humanize_time(""), "?")

    def test_unparseable_falls_back_to_input(self):
        self.assertEqual(humanize_time("not-a-date"), "not-a-date")


class ResolveSessionTests(unittest.TestCase):
    def _config_with_sessions(self, tmp: str, rows: list) -> Config:
        home = Path(tmp)
        (home / "session_index.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
        )
        return Config(app_id=None, app_secret=None, codex_home=home)

    def test_resolves_by_exact_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config_with_sessions(tmp, [
                {"id": "019e1651-aaa", "thread_name": "improve arbiter", "updated_at": "t1"},
            ])
            match, candidates = resolve_session(cfg, "019e1651-aaa")
            assert match is not None
            self.assertEqual(match["thread_name"], "improve arbiter")
            self.assertEqual(candidates, [])

    def test_resolves_by_exact_name_case_insensitive(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config_with_sessions(tmp, [
                {"id": "a", "thread_name": "Improve Arbiter", "updated_at": "t1"},
                {"id": "b", "thread_name": "other", "updated_at": "t2"},
            ])
            match, _ = resolve_session(cfg, "improve arbiter")
            assert match is not None
            self.assertEqual(match["id"], "a")

    def test_resolves_by_id_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config_with_sessions(tmp, [
                {"id": "019e1651-aaa", "thread_name": "alpha", "updated_at": "t1"},
                {"id": "019e2222-bbb", "thread_name": "beta", "updated_at": "t2"},
            ])
            match, _ = resolve_session(cfg, "019e1651")
            assert match is not None
            self.assertEqual(match["thread_name"], "alpha")

    def test_resolves_by_name_substring_when_unique(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config_with_sessions(tmp, [
                {"id": "a", "thread_name": "Inspect Arbiter pipeline", "updated_at": "t1"},
                {"id": "b", "thread_name": "Other thing", "updated_at": "t2"},
            ])
            match, _ = resolve_session(cfg, "pipeline")
            assert match is not None
            self.assertEqual(match["id"], "a")

    def test_ambiguous_name_substring_returns_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config_with_sessions(tmp, [
                {"id": "a", "thread_name": "Inspect Arbiter pipeline", "updated_at": "t1"},
                {"id": "b", "thread_name": "improve arbiter", "updated_at": "t2"},
                {"id": "c", "thread_name": "Review arbiter RFT pipeline", "updated_at": "t3"},
            ])
            match, candidates = resolve_session(cfg, "arbiter")
            self.assertIsNone(match)
            self.assertEqual({c["id"] for c in candidates}, {"a", "b", "c"})

    def test_no_match_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config_with_sessions(tmp, [
                {"id": "a", "thread_name": "alpha", "updated_at": "t"},
            ])
            match, candidates = resolve_session(cfg, "missing")
            self.assertIsNone(match)
            self.assertEqual(candidates, [])


class RolloutTailTests(unittest.TestCase):
    def _make_rollout(self, codex_home: Path, session_id: str, lines: list) -> Path:
        day = codex_home / "sessions" / "2026" / "05" / "11"
        day.mkdir(parents=True, exist_ok=True)
        path = day / ("rollout-2026-05-11T00-00-00-%s.jsonl" % session_id)
        path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
        return path

    def test_rollout_path_for_locates_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            sid = "019e1651-b8cc-7f13-a96d-e569b7ede3a0"
            expected = self._make_rollout(home, sid, [{"type": "session_meta"}])
            cfg = Config(app_id=None, app_secret=None, codex_home=home)
            self.assertEqual(rollout_path_for(cfg, sid), expected)
            self.assertIsNone(rollout_path_for(cfg, "missing-id"))

    def test_read_rollout_tail_truncates_to_max_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            # Leading line is far past the tail window; trailing line lives
            # within it. The partial-line discard should drop the head but
            # leave the trailing complete line intact.
            path.write_text("head_line\n" + ("x" * 80) + "\nlast_line\n", encoding="utf-8")
            tail = read_rollout_tail(path, max_bytes=40)
            self.assertNotIn("head_line", tail)
            self.assertIn("last_line", tail)

    def test_extract_thread_highlights_picks_latest_per_kind(self):
        events = [
            {"payload": {"type": "thread_goal_updated", "goal": {"objective": "Old goal"}}},
            {"payload": {"type": "user_message", "message": "first user"}},
            {"payload": {"type": "agent_message", "message": "agent A"}},
            {"payload": {"type": "agent_message", "message": "agent B"}},
            {"payload": {"type": "thread_goal_updated", "goal": {"objective": "New goal"}}},
            {"payload": {"type": "user_message", "message": "latest user"}},
            {"payload": {"type": "agent_message", "message": "agent C"}},
            {"payload": {"type": "agent_message", "message": "agent D"}},
        ]
        text = "\n".join(json.dumps(e) for e in events)
        goal, last_user, agents = extract_thread_highlights(text, max_agent=3)
        self.assertEqual(goal, "New goal")
        self.assertEqual(last_user, "latest user")
        self.assertEqual(agents, ["agent B", "agent C", "agent D"])

    def test_format_codex_thread_status_includes_rollout_highlights(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            sid = "019e1651-b8cc-7f13-a96d-e569b7ede3a0"
            self._make_rollout(home, sid, [
                {"payload": {"type": "thread_goal_updated", "goal": {"objective": "Ship the feature"}}},
                {"payload": {"type": "user_message", "message": "please continue"}},
                {"payload": {"type": "agent_message", "message": "Working on the diff now."}},
            ])
            cfg = Config(app_id=None, app_secret=None, codex_home=home)
            session = {"id": sid, "thread_name": "improve arbiter", "updated_at": "2026-05-14T23:27:48Z"}
            rendered = format_codex_thread_status(session, cfg)
            self.assertIn("improve arbiter", rendered)
            self.assertIn("Ship the feature", rendered)
            self.assertIn("please continue", rendered)
            self.assertIn("Working on the diff now.", rendered)
            self.assertIn("codex exec resume " + sid, rendered)


class SessionIndexAppendTests(unittest.TestCase):
    """The session_index.jsonl appender is a system-boundary writer that
    touches another tool's index file, so it gets thorough coverage:
    validation refusals, atomicity-size bound, name scrubbing, error
    swallowing, no clobbering of existing content, and the post-condition
    that downstream resolvers see the new row.
    """

    SESSION_ID = "019e3878-ffad-7022-995d-c62e10ca73d1"

    def _config(self, tmp: str, *, append: bool = True) -> Config:
        return Config(
            app_id=None,
            app_secret=None,
            codex_home=Path(tmp),
            session_index_append=append,
        )

    def _read_rows(self, codex_home: Path) -> list:
        path = codex_home / "session_index.jsonl"
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_appends_valid_row_with_three_required_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(tmp)
            self.assertTrue(append_session_index_entry(cfg, self.SESSION_ID, "my task"))
            rows = self._read_rows(Path(tmp))
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["id"], self.SESSION_ID)
            self.assertEqual(row["thread_name"], "my task")
            self.assertTrue(row["updated_at"])
            # Trailing newline so subsequent appenders write a clean new line.
            content = (Path(tmp) / "session_index.jsonl").read_text(encoding="utf-8")
            self.assertTrue(content.endswith("\n"))

    def test_rejects_session_id_that_is_not_uuid_shaped(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(tmp)
            for bad in ("", "abc", "not-a-uuid", "019e1651-x", "${malicious}", "../etc/passwd"):
                self.assertFalse(append_session_index_entry(cfg, bad, "name"), bad)
            self.assertEqual(self._read_rows(Path(tmp)), [])

    def test_rejects_non_string_session_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(tmp)
            self.assertFalse(append_session_index_entry(cfg, None, "name"))  # type: ignore[arg-type]
            self.assertFalse(append_session_index_entry(cfg, 123, "name"))  # type: ignore[arg-type]
            self.assertEqual(self._read_rows(Path(tmp)), [])

    def test_skip_when_session_index_append_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(tmp, append=False)
            self.assertFalse(append_session_index_entry(cfg, self.SESSION_ID, "x"))
            self.assertFalse((Path(tmp) / "session_index.jsonl").exists())

    def test_strips_newlines_and_control_chars_from_thread_name(self):
        """A name with \\n or other controls must not split the JSONL row, and
        must not embed null bytes that downstream tools might mishandle."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(tmp)
            evil = "first line\nsecond line\twith tab\x00\x07and bell"
            self.assertTrue(append_session_index_entry(cfg, self.SESSION_ID, evil))
            content = (Path(tmp) / "session_index.jsonl").read_text(encoding="utf-8")
            # Exactly one record (one newline at end, no internal record-splits).
            self.assertEqual(content.count("\n"), 1)
            rows = self._read_rows(Path(tmp))
            self.assertEqual(len(rows), 1)
            name = rows[0]["thread_name"]
            for forbidden in ("\n", "\r", "\t", "\x00", "\x07"):
                self.assertNotIn(forbidden, name)

    def test_truncates_long_thread_name_within_byte_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(tmp)
            long_name = "x" * 500
            self.assertTrue(append_session_index_entry(cfg, self.SESSION_ID, long_name))
            content_bytes = (Path(tmp) / "session_index.jsonl").read_bytes()
            self.assertLessEqual(len(content_bytes), _SESSION_INDEX_MAX_LINE_BYTES)
            rows = self._read_rows(Path(tmp))
            # Name is at most 80 chars (the documented cap).
            self.assertLessEqual(len(rows[0]["thread_name"]), 80)

    def test_appends_without_clobbering_existing_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(tmp)
            path = Path(tmp) / "session_index.jsonl"
            pre = [
                {"id": "019eaaaa-1111-2222-3333-444455556666", "thread_name": "older", "updated_at": "t0"},
                {"id": "019ebbbb-1111-2222-3333-444455556666", "thread_name": "older2", "updated_at": "t1"},
            ]
            path.write_text("\n".join(json.dumps(r) for r in pre) + "\n", encoding="utf-8")
            self.assertTrue(append_session_index_entry(cfg, self.SESSION_ID, "new"))
            rows = self._read_rows(Path(tmp))
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[0]["thread_name"], "older")
            self.assertEqual(rows[1]["thread_name"], "older2")
            self.assertEqual(rows[2]["thread_name"], "new")
            self.assertEqual(rows[2]["id"], self.SESSION_ID)

    def test_swallows_oserror_from_os_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(tmp)
            with mock.patch("codex_lark_minimal.codex.os.open", side_effect=PermissionError("nope")):
                # Must not raise.
                self.assertFalse(append_session_index_entry(cfg, self.SESSION_ID, "n"))
            self.assertEqual(self._read_rows(Path(tmp)), [])

    def test_swallows_oserror_from_os_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(tmp)
            with mock.patch("codex_lark_minimal.codex.os.write", side_effect=OSError("disk full")):
                self.assertFalse(append_session_index_entry(cfg, self.SESSION_ID, "n"))

    def test_creates_codex_home_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "nested" / "codex_home"
            cfg = Config(
                app_id=None, app_secret=None,
                codex_home=codex_home, session_index_append=True,
            )
            self.assertFalse(codex_home.exists())
            self.assertTrue(append_session_index_entry(cfg, self.SESSION_ID, "new"))
            self.assertTrue((codex_home / "session_index.jsonl").exists())

    def test_appended_row_is_resolvable_by_name_and_id(self):
        """Post-condition: after append, the downstream resolver finds the row
        through the same paths the user-facing `codex status <arg>` uses."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(tmp)
            self.assertTrue(append_session_index_entry(cfg, self.SESSION_ID, "improve arbiter"))
            by_id = find_session(cfg, self.SESSION_ID)
            assert by_id is not None
            self.assertEqual(by_id["thread_name"], "improve arbiter")
            by_name, _ = resolve_session(cfg, "improve arbiter")
            assert by_name is not None
            self.assertEqual(by_name["id"], self.SESSION_ID)

    def test_redaction_applies_to_thread_name(self):
        """thread_name passes through redact(); a secret-shaped string in the
        prompt preview should be scrubbed before it lands in the index."""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._config(tmp)
            self.assertTrue(append_session_index_entry(
                cfg, self.SESSION_ID, "token=sk-AbCdEf0123456789zzzzzzzz hello"
            ))
            rows = self._read_rows(Path(tmp))
            self.assertEqual(len(rows), 1)
            self.assertNotIn("sk-AbCdEf0123456789zzzzzzzz", rows[0]["thread_name"])


if __name__ == "__main__":
    unittest.main()
