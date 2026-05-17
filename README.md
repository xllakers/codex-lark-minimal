# codex-lark-minimal

[![test](https://github.com/xllakers/codex-lark-minimal/actions/workflows/test.yml/badge.svg)](https://github.com/xllakers/codex-lark-minimal/actions/workflows/test.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A minimal, local-first Feishu/Lark bridge that lets a chat bot start, track,
stop, and resume [Codex](https://github.com/openai/codex) jobs on your machine.

```
Feishu/Lark bot message  ──►  long-connection bridge  ──►  codex exec --json
```

## What this is — and isn't

**Is:** one agent (Codex), one platform (Feishu/Lark), one long-lived daemon
that spawns Codex as a subprocess per job. About 1.4K lines of Python plus a
single dependency (`lark-oapi`).

**Isn't:** a multi-platform/multi-agent control plane. No web UI, no live
mid-turn steering, no raw prompt/output persistence, no public webhook tunnel,
no in-chat tool-approval flow. If you need any of that, see
[Related projects](#related-projects) below.

Design principles, in priority order: **safe → minimalist → easy to maintain.**
See `AGENTS.md` for the full rules.

## Prerequisites

- macOS or Linux
- Python 3.9+
- [Codex CLI](https://github.com/openai/codex) installed and authenticated
  (`codex --version` should work)
- A Feishu or Lark account with permission to create a custom app

## Setup

Two paths, same plumbing:

- **You're installing this yourself.** Use the wizard below.
- **You're asking an AI agent (Codex, Claude Code, …) to install it for you.**
  Point the agent at [`INSTALL_WITH_AGENT.md`](INSTALL_WITH_AGENT.md) — it's
  a short playbook that tells the agent what to ask you for vs. what to run
  itself, using the same non-interactive `configure` / `discover` /
  `doctor` commands the wizard uses under the hood.

### 1. Create a Feishu/Lark custom app

1. Go to [open.feishu.cn](https://open.feishu.cn) (Feishu, mainland China) or
   [open.larksuite.com](https://open.larksuite.com) (Lark, global) and create
   a **custom app** with **Bot** capability.
2. Under **Events & Callbacks → Events**, add `im.message.receive_v1`.
3. Choose **Long connection** as the event delivery method (this is what lets
   the bridge work without a public IP / webhook tunnel).
4. Under **Permissions**, grant the bot `im:message` and
   `im:message:send_as_bot`.
5. Publish a version and wait for approval. Copy the **App ID** and **App
   Secret** — the wizard will ask for them.

### 2. Run the installer

```bash
git clone https://github.com/xllakers/codex-lark-minimal.git
cd codex-lark-minimal
./install.sh
```

The installer creates an isolated venv at `~/.codex/bridges/codex-lark-minimal/`,
opportunistically symlinks `codex-lark` into `~/.local/bin` if that's on your
PATH, and then launches the **setup wizard** which:

1. Prompts for App ID / Secret / Lark domain and validates the credentials
   against Feishu's auth endpoint.
2. Prompts for workspace `alias=path` pairs.
3. Opens a one-shot long connection. **Send any message to your bot from the
   target chat** — the wizard prints the observed `sender_id` / `chat_id` and
   asks which to add to the allowlist.
4. Writes the chosen values to `config.env` as a dated `# --- codex-lark setup
   YYYY-MM-DD ---` block (`chmod 600` preserved, existing values kept).
5. Optionally installs the macOS LaunchAgent.

That's it. Verify and start:

```bash
codex-lark doctor
codex-lark daemon
```

(If `~/.local/bin` isn't on your PATH, the installer prints an alias line you
can paste into your shell rc — or use the full path
`~/.codex/bridges/codex-lark-minimal/run.sh`.)

### Re-run the wizard or skip it

- Run `codex-lark setup` again any time — defaults are pre-filled from the
  current config and a new dated block is appended.
- `./install.sh --no-setup` installs the bridge without launching the wizard;
  edit `config.env` by hand and run `codex-lark doctor` when ready.

### Dry-run vs real mode

The mode is **derived from the allowlist**: an empty
`FEISHU_CODEX_ALLOWED_SENDERS` and `FEISHU_CODEX_ALLOWED_CHATS` keeps the
daemon in dry-run automatically (the bridge logs events but never launches
Codex). Populate either to go live. Set `FEISHU_CODEX_DRY_RUN=1` explicitly
to force dry-run while keeping a populated allowlist (useful for staging).

### macOS autostart (optional)

```bash
codex-lark service install
codex-lark service start
codex-lark service status
```

Stop with `service stop`. On Linux, run `codex-lark daemon` under your usual
supervisor (systemd user unit, tmux, etc.) — the script is a plain long-lived
process.

## Lark commands

Send these to the bot (replace `codex` with your `FEISHU_CODEX_TRIGGER_PREFIX`
if you changed it):

```
codex help
codex workspaces
codex status
codex status <run_id>
codex stop <run_id>
codex continue <run_id>: <follow-up instruction>
codex recent
codex <workspace-alias>: <task description>
```

`codex continue` only works on completed/idle Codex sessions started via the
bridge. For running jobs, stop them first or wait.

## Local CLI

```bash
codex-lark status                 # list recent bridge jobs
codex-lark status <run_id>        # show one job with redacted output tail
codex-lark recent                 # list Codex's own session index (resumable)
codex-lark simulate "codex status"  # parse a message without sending it
codex-lark doctor
```

The bridge's job state is the source of truth for what's running. Codex's own
session index is a recent/resume index only — not proof a thread is live.

## What is — and isn't — persisted

Persisted (under `~/.codex/bridges/codex-lark-minimal/state/`, `chmod 600`):

- run IDs, timestamps, status, PIDs, return codes
- **SHA-256** of the prompt + a redacted ~200-char preview
- a redacted, length-capped tail of Codex output (max 4 KB)
- the Codex session ID (for resume)

**Never** persisted by the bridge:

- raw prompts
- raw Codex JSONL output
- app secrets, tokens, or Codex auth (those live in `config.env` or
  `~/.codex/` and never touch bridge state)

Secret regex masking is applied to every log line, error message, and stored
tail (see `src/codex_lark_minimal/redaction.py`).

## Troubleshooting

- **`config error: real mode requires ...`** — the allowlist is non-empty (so
  the bridge is in real mode) but `FEISHU_APP_ID` / `FEISHU_APP_SECRET` are
  missing. Run `codex-lark setup` to fill them in, or clear the allowlist to
  fall back to dry-run.
- **Bot doesn't reply** — check that the app version is published, the
  `im.message.receive_v1` event is added, and the bot has been invited to the
  group. The doctor command can verify the token.
- **Codex not found** — set `FEISHU_CODEX_CODEX_BIN` in `config.env` to the
  absolute path of your `codex` binary.
- **Persistent `lost` jobs** — a worker process died between writes. Safe to
  ignore; the bridge will not restart them automatically (by design).

## Development

Install with dev tools, then run the same three checks CI runs:

```bash
pip install -e ".[dev]"

ruff check src/ tests/    # lint
mypy                      # type check (lenient — strict is a separate effort)
pytest -q                 # 26 tests, <1s
```

`make test` runs pytest only. CI (GitHub Actions, `.github/workflows/test.yml`)
runs all three on Python 3.9 and 3.12 for every push and PR.

Run the CLI from a checkout without installing:

```bash
PYTHONPATH=src python -m codex_lark_minimal.cli ...
```

Repo guardrails for AI agents live in `AGENTS.md` (read automatically by
Codex) and `CLAUDE.md` (which imports `AGENTS.md` for Claude Code). For
non-trivial changes, see the `skills/codex-lark-design` and
`skills/codex-lark-review` checklists.

## Related projects

If this repo's three principles (safe → minimalist → easy to maintain) don't
match what you need, two larger projects in the same space are worth a look:

| | This repo | [chenhg5/cc-connect](https://github.com/chenhg5/cc-connect) | [op7418/Claude-to-IM-skill](https://github.com/op7418/Claude-to-IM-skill) |
|---|---|---|---|
| **Language** | Python | Go | TypeScript |
| **Size** | ~1.4K LOC, 1 dep | binary + web UI, much larger | ~2.5k★, larger |
| **Agents** | Codex | 10+ (Codex, CC, Gemini, …) | Claude Code + Codex |
| **Chat platforms** | Feishu/Lark | 11 (Slack, Discord, Telegram, DingTalk, …) | 5 (Telegram, Discord, Feishu, QQ, WeChat) |
| **In-chat tool approval** | ❌ (out of scope) | ✅ `/mode` + per-tool prompt | ✅ SSE `canUseTool()` + inline buttons |
| **Live streaming** | ❌ | ✅ | ✅ |
| **Web admin UI** | ❌ | ✅ embedded | ❌ |
| **Install target** | local daemon + launchd | local daemon (npm/Homebrew) | drop-in `~/.claude/skills/` or `~/.codex/skills/` |
| **Default-deny + dry-run** | ✅ | partial | partial |

**Why pick this one over the others:** you want a small, single-purpose,
auditable bridge that you can read end-to-end in an afternoon. **Why pick
one of the others:** you need multiple platforms or agents, in-chat tool
approval, or live streaming, and you're willing to take on a much larger
codebase to get them.

> A small but real advantage of this project: it's plain Python with one
> dependency. For anyone who wants to *understand* and modify the bridge
> rather than just install it, that's an order of magnitude less code to read
> than a Go binary or a TypeScript codebase with a build step.

### Top things worth borrowing from cc-connect and Claude-to-IM-skill

Lessons we've taken from reading both — some applied here, some explicitly
declined as out of scope:

1. **Doctor commands that probe live, not just files.** Both projects ship
   diagnostic commands that hit the platform's auth endpoint to confirm the
   token actually works, not just that the env var is non-empty. (We do this
   too, in `src/codex_lark_minimal/doctor.py`.) Worth doing in any bridge.
2. **Allowlists per identity, not per network.** Neither project trusts the
   transport — both gate on stable IM-side identifiers (sender / chat ID).
   This is what makes "no public IP needed" actually safe.
3. **Secret redaction by *pattern*, not by registered token.** Both mask
   anything that looks like a token in logs without needing to know the value
   in advance. Cheap to add, catches the long tail.
4. **State directory is `chmod 600` and outside the repo.** Every serious
   variant puts state in `~/.<something>/`, not in the source tree. Stops
   accidental commits of run artifacts.
5. **(Declined) In-chat tool approval gateway.** Claude-to-IM's
   `canUseTool()` block-and-await pattern is genuinely clever, but it
   inverts the trust model — the IM becomes part of the control loop, not
   just a transport. Our `AGENTS.md` keeps live mid-turn steering out of
   scope on purpose.
6. **(Declined) Multi-platform abstraction.** cc-connect's per-platform
   adapter layer is well-designed, but every adapter is surface area. We'd
   rather one platform, well-understood.
7. **(Optional, worth porting) OS-user isolation.** cc-connect's
   `run_as_user` config lets the agent subprocess run under a different
   Unix uid. ~30 lines wrapping `subprocess.Popen` would add a real layer
   of defense for shared hosts. Open to a PR.
8. **(Optional, worth considering) Skill-first install.** Claude-to-IM lives
   in `~/.claude/skills/<name>/`, which makes it discoverable from inside
   the agent. A future version of this project could ship as an install
   target there in addition to `~/.codex/bridges/`.

## License

MIT — see [LICENSE](LICENSE).
