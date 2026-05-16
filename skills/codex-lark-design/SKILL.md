---
name: codex-lark-design
description: Use before hard, ambiguous, or high-impact codex-lark-minimal changes involving Feishu/Lark transport, auth, allowlists, Codex subprocess control, local state/logging, redaction, install/service behavior, or status/continue semantics.
---

# Codex Lark Design Panel

Use this as a short design review loop before building risky bridge behavior.

## 1. Verify First

- Define the user-visible success path and the smallest local smoke test.
- Identify all secrets, prompts, outputs, logs, and state touched by the change.
- Confirm which inputs are trusted, allowlisted, redacted, or rejected.
- Name the rollback path and how a bad daemon/job is stopped.

## 2. Expert Lenses

- Codex/tooling principal: Codex CLI semantics, `codex exec --json`, resume
  behavior, noninteractive failures, prompt boundaries, and session-id capture.
- Messaging/security principal: Feishu/Lark long connection behavior, app scopes,
  sender/chat allowlists, replay/dedup concerns, and secret handling.
- Local SRE/tooling principal: process lifecycle, launchd, install/doctor UX,
  state/log rotation, crash recovery, and operator visibility.

## 3. Stress The Shape

- What fails if a message arrives twice, arrives from the wrong chat, or contains
  a malicious workspace name?
- What fails if Codex hangs, exits before emitting a session id, or emits malformed
  JSON?
- What information is persisted, and can it expose raw prompts, secrets, or
  sensitive output?
- Does the feature stay useful with many recent jobs without becoming a session
  manager clone?

## 4. Scale Ladder

- Unit test parser/state/command behavior.
- Dry-run `codex-lark simulate ...`.
- Run `make test`.
- Run the installed doctor when install/config/daemon behavior changes.
- Foreground daemon with a dry-run allowlisted test account.
- Live smoke only after allowlist and app permissions are explicit.

## 5. V1 Constraints

- Use long connection, not public webhook tunnels.
- Keep configured workspace aliases as the only chat-selectable execution targets.
- Store redacted previews/tails, hashes, ids, pids, timestamps, and session ids;
  do not persist raw prompt/output.
- Keep live mid-turn steering out of scope until Codex exposes a stable active-turn
  control API.
- Prefer deleting complexity over making this a general multi-platform session
  control plane.
