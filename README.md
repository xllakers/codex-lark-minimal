# codex-lark-minimal

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
mid-turn steering, no raw prompt/output persistence, no public webhook tunnel.
If you need any of that, look at [chenhg5/cc-connect](https://github.com/chenhg5/cc-connect).

Design principles, in priority order: **safe → minimalist → easy to maintain.**
See `AGENTS.md` for the full rules.

## Prerequisites

- macOS or Linux
- Python 3.9+
- [Codex CLI](https://github.com/openai/codex) installed and authenticated
  (`codex --version` should work)
- A Feishu or Lark account with permission to create a custom app

## Setup — step by step

### 1. Create a Feishu/Lark custom app

1. Go to [open.feishu.cn](https://open.feishu.cn) (Feishu, mainland China) or
   [open.larksuite.com](https://open.larksuite.com) (Lark, global) and create
   a **custom app** with **Bot** capability.
2. Under **Events & Callbacks → Events**, add `im.message.receive_v1`.
3. Choose **Long connection** as the event delivery method (this is what lets
   the bridge work without a public IP / webhook tunnel).
4. Under **Permissions**, grant the bot:
   - `im:message` (receive messages)
   - `im:message:send_as_bot` (reply)
5. Publish a version of the app and wait for approval.
6. Copy the **App ID** and **App Secret** from the app's credentials page —
   you'll need them in step 3.

### 2. Clone and install

```bash
git clone https://github.com/xllakers/codex-lark-minimal.git
cd codex-lark-minimal
./install.sh
```

This creates an isolated venv at `~/.codex/bridges/codex-lark-minimal/`, copies
`config.env.example` to `config.env` with `chmod 600`, and writes a `run.sh`
wrapper. It does **not** modify your system Python or shell.

### 3. Configure

Open the config file:

```bash
$EDITOR ~/.codex/bridges/codex-lark-minimal/config.env
```

At minimum, fill in:

- `FEISHU_APP_ID` and `FEISHU_APP_SECRET` from step 1.
- `FEISHU_CODEX_WORKSPACES` — comma-separated `alias=/abs/path` pairs naming
  the project directories the bot is allowed to act on. **Only these paths
  can be targeted from chat.** Example:
  ```
  FEISHU_CODEX_WORKSPACES=myproj=/Users/you/Projects/myproj,site=/Users/you/Projects/site
  FEISHU_CODEX_DEFAULT_WORKSPACE=myproj
  ```
- For global Lark (not Feishu CN), also set:
  `FEISHU_DOMAIN=https://open.larksuite.com`

Leave `FEISHU_CODEX_DRY_RUN=1` and `FEISHU_CODEX_ALLOW_ALL=1` for now — they
are the safe defaults for the discovery step below.

Run diagnostics:

```bash
~/.codex/bridges/codex-lark-minimal/run.sh doctor
```

Fix anything it reports before going further.

### 4. Discover your sender / chat IDs (dry run)

Start the daemon in the foreground:

```bash
~/.codex/bridges/codex-lark-minimal/run.sh daemon
```

Add the bot to a group (or DM it) and send:

```
codex status
```

In dry-run mode the bridge will not launch Codex — it just logs the event.
Watch the daemon's stdout (or `~/.codex/bridges/codex-lark-minimal/logs/bridge.log`)
for the inbound `sender_id` and `chat_id`. Copy them.

Stop the daemon with `Ctrl-C`.

### 5. Flip to real mode

Edit `config.env` again:

```
FEISHU_CODEX_DRY_RUN=0
FEISHU_CODEX_ALLOW_ALL=0
FEISHU_CODEX_ALLOWED_SENDERS=<your sender_id>
# optionally also:
FEISHU_CODEX_ALLOWED_CHATS=<your chat_id>
```

The bridge **refuses to start in real mode without** app credentials AND at
least one allowlist value AND `ALLOW_ALL=0`. This is by design.

Re-run the doctor, then start the daemon again:

```bash
~/.codex/bridges/codex-lark-minimal/run.sh doctor
~/.codex/bridges/codex-lark-minimal/run.sh daemon
```

Send the bot `codex help` to confirm it responds. You're done.

### 6. (Optional) Autostart on macOS via launchd

```bash
~/.codex/bridges/codex-lark-minimal/run.sh service install
~/.codex/bridges/codex-lark-minimal/run.sh service start
~/.codex/bridges/codex-lark-minimal/run.sh service status
```

Stop with `service stop`. On Linux, run the daemon under your usual supervisor
(systemd user unit, tmux, etc.) — the script is a plain long-lived process.

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

- **`config error: real mode requires ...`** — you flipped `DRY_RUN=0` without
  filling in credentials or the allowlist. Re-read step 5.
- **Bot doesn't reply** — check that the app version is published, the
  `im.message.receive_v1` event is added, and the bot has been invited to the
  group. The doctor command can verify the token.
- **Codex not found** — set `FEISHU_CODEX_CODEX_BIN` in `config.env` to the
  absolute path of your `codex` binary.
- **Persistent `lost` jobs** — a worker process died between writes. Safe to
  ignore; the bridge will not restart them automatically (by design).

## Development

```bash
make test                                              # run unit tests
PYTHONPATH=src python -m codex_lark_minimal.cli ...    # run from a checkout
```

Repo guardrails for AI agents live in `AGENTS.md` (read automatically by
Codex) and `CLAUDE.md` (which imports `AGENTS.md` for Claude Code). For
non-trivial changes, see the `skills/codex-lark-design` and
`skills/codex-lark-review` checklists.

## License

MIT — see [LICENSE](LICENSE).
