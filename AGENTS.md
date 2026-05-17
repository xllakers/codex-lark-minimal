# Agent Instructions

## Mission

Build a minimal, local-first Feishu/Lark bridge for starting, tracking, stopping,
and resuming trusted Codex jobs.

## Hard Boundaries

- Never commit Feishu/Lark app secrets, Codex auth, tokens, SSH keys, raw prompts,
  raw Codex JSONL, logs, job state, run artifacts, or local config.
- Persist only redacted previews/tails, hashes, ids, pids, timestamps, and Codex
  session ids under ignored local install/state paths.
- Lark-triggered work must use configured workspace aliases only; never accept
  arbitrary filesystem paths from chat.
- Real daemon mode is default-deny: app credentials and a sender/chat allowlist
  are required. Empty allowlist ⇒ dry-run automatically (the bridge logs but
  never spawns Codex). Use `codex-lark setup` (humans) or `codex-lark discover
  --handshake-token …` (agents) to populate the allowlist safely.
- Keep live mid-turn steering out of scope until Codex exposes a stable API for
  active-turn intervention.

## Defaults

- Simple, explicit, testable, easy to delete.
- Prefer narrow stdlib wrappers and the official `lark-oapi` transport over a
  broad framework.
- Add abstraction only for real repetition or risk.
- Setup/install with `./install.sh`.
- Test with `make test`; keep `PYTHONPATH=src` for direct module runs.
- Run the installed doctor after config, installer, service, or daemon changes:
  `~/.codex/bridges/codex-lark-minimal/run.sh doctor`.
- Do not commit generated/local files such as `.venv/`, caches, logs, `state/`,
  `config.env`, `*.egg-info/`, or copied run output.

## Work Loop

- Read: goal, constraints, current state.
- Act: smallest useful change/check.
- Learn: verify and note surprises.
- Handoff: result, risks, next step.

## Quality

- Non-trivial implementation: run `make test`.
- Config/install/daemon changes: run the installed doctor when feasible.
- Hard ambiguous or high-impact auth, transport, subprocess, state, install, or
  service changes: use `skills/codex-lark-design`.
- Before submitting non-trivial changes, especially changes touching Lark event
  handling, allowlists, Codex command construction, subprocess lifecycle, local
  state, redaction, installer, or launchd service behavior: use
  `skills/codex-lark-review`.
- Panels stay short: findings, verdict, fixes. No ceremony for small edits.
- Iterate until boring, secure, scalable, and testable.

## Skills

- Create skills only for repeated or high-risk procedures.
- Keep skills as short checklists/wrappers.
- Retire stale or unused skills.
- Use the project-local design/review skills instead of copying generic
  checklists into task threads.
