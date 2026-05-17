---
name: codex-lark-review
description: Use before submitting non-trivial codex-lark-minimal changes, especially auth, Lark transport, Codex command construction, subprocess lifecycle, state persistence, redaction, installer, or service changes.
---

# Codex Lark Principal Review

Review the diff as production local tooling that can launch Codex from chat.
Lead with findings, then verdict and required fixes.

## Checklist

- Inspect the diff for behavioral regressions, missing tests, unsafe defaults, and
  unclear operator failure modes.
- Run `make test` for non-trivial implementation changes.
- If install/config/daemon/service behavior changed, run the installed doctor when
  feasible: `~/.codex/bridges/codex-lark-minimal/run.sh doctor`.
- Confirm no `config.env`, app secrets, Codex auth, tokens, logs, state files,
  raw prompts, raw Codex JSONL, `.venv/`, caches, or generated artifacts are
  added to git.
- Confirm real daemon mode remains default-deny: credentials plus a non-empty
  sender/chat allowlist are required. Empty allowlist ⇒ dry-run automatically;
  there is no separate `ALLOW_ALL` knob.
- Confirm chat input cannot select arbitrary filesystem paths; workspace aliases
  remain the execution boundary.
- Confirm subprocess calls use argument lists, not shell interpolation, and raw
  prompt content is passed through stdin only.
- Confirm persisted state/output is redacted and bounded, and session ids are
  captured only for resume/status behavior.
- Confirm `continue` refuses running jobs and only resumes jobs with captured
  Codex session ids.
- Confirm Lark event handling returns quickly enough and long work is delegated to
  a worker process.

## Verdict

- Block on auth bypasses, arbitrary path execution, secret/raw-output persistence,
  unsafe subprocess construction, broken stop/continue semantics, or missing tests
  for changed control behavior.
- Non-blocking notes should be small, concrete, and worth doing soon.
- Approve only when the result is boring, secure, scalable, and testable.
