# Installing codex-lark-minimal with an AI agent

This doc is for an **AI coding agent** (Codex, Claude Code, …) helping a human
install codex-lark-minimal on their own machine.

> Humans installing this themselves should just run `./install.sh` — the
> wizard does everything below interactively.

## What only the human can provide

Ask these. Never guess.

1. **App ID + App Secret.** The human creates a custom app at
   `open.feishu.cn` (CN) or `open.larksuite.com` (global) with: Bot capability;
   event `im.message.receive_v1` subscribed; long-connection event delivery;
   `im:message` + `im:message:send_as_bot` permissions; app version published.
2. **Workspace `alias=path` pairs.** Only the human knows which directories
   the bot is allowed to drive.
3. **One short message in Lark** containing a handshake token you'll generate.
   The bridge filters to that exact token, so picking the right identity is
   automatic.

> The domain (Feishu CN vs Lark global) is *auto-detected* from the
> credentials. Don't ask.

> The human's App Secret will pass through your tool-call log. The CLI keeps
> it out of `ps` and shell history (via `--stdin`), but if your agent
> transcripts are logged externally, that log will contain the secret. Tell
> the human they can rotate the secret in the Lark portal after setup if
> that's a concern.

## CLI surface

All commands use the deterministic absolute path that exists after install:

```bash
CODEX_LARK="$HOME/.codex/bridges/codex-lark-minimal/run.sh"
```

| Command | What it does |
|---|---|
| `./install.sh --no-setup` | Install without launching the human wizard |
| `$CODEX_LARK configure --set K=V [...]` | Atomic append-block write. Refuses keys ending `_SECRET`/`_TOKEN`/`_PASSWORD`; auto-detects `FEISHU_DOMAIN` when creds are present but domain isn't; validates `FEISHU_CODEX_WORKSPACES` shape |
| `$CODEX_LARK configure --stdin` | Read `KEY=VALUE` lines from stdin (for secrets) |
| `$CODEX_LARK doctor` | Exit 0 = green; exit 2 = any failure. Live token check, workspace + Codex CLI/login verification |
| `$CODEX_LARK discover --json --handshake-token T --timeout 180` | One-shot listener filtered by token T. Auto-picks the matching event and tests the reverse-direction send permission by replying in the chat |
| `$CODEX_LARK service install / start / stop / status` | macOS LaunchAgent |
| `$CODEX_LARK daemon` | Run the bridge in the foreground (Linux or no autostart) |

## Playbook — 6 phases

### Phase 0 — clone (if needed)

```bash
test -d codex-lark-minimal || git clone https://github.com/xllakers/codex-lark-minimal.git
cd codex-lark-minimal
```

### Phase 1 — gather

Tell the human:

> Create a custom app at **open.feishu.cn** (CN) or **open.larksuite.com**
> (global) with Bot capability + `im.message.receive_v1` event + long-connection
> delivery + `im:message` & `im:message:send_as_bot` permissions, and publish
> a version. Then paste your **App ID** and **App Secret** here, and list the
> project directories the bot may act on as `alias=path` pairs.

### Phase 2 — install

```bash
./install.sh --no-setup
test -x "$CODEX_LARK"
```

### Phase 3 — write config (domain auto-detected)

App Secret on stdin; everything else as flags. **Do not pass `FEISHU_DOMAIN`** —
the CLI probes both Feishu CN and Lark global with the credentials and writes
whichever authenticates:

```bash
$CODEX_LARK configure \
  --set FEISHU_APP_ID=<app_id> \
  --set FEISHU_CODEX_WORKSPACES=<alias=path,alias=path> \
  --stdin <<EOF
FEISHU_APP_SECRET=<secret>
EOF
```

`configure` refuses `--set` for sensitive-shaped keys and rejects malformed
workspaces. If domain auto-detect fails (both endpoints reject the creds), it
prints a stderr error and the next `doctor` will FAIL — ask the human to
re-paste credentials.

### Phase 4 — verify

```bash
$CODEX_LARK doctor
```

Proceed only on exit 0. If FAIL:

| Output line | Cause / fix |
|---|---|
| `Feishu app credentials rejected` | Wrong App ID / Secret — redo Phase 3 |
| `Codex CLI found` FAIL | `codex` not on PATH — human installs Codex |
| `Codex login status` FAIL | Codex not authenticated — human runs `codex login` |
| `workspace does not exist` | Bad path — redo Phase 3 |

### Phase 5 — discover with handshake, then write allowlist

Generate a short unguessable token, then ask the human to send it as part of
their message to the bot:

```bash
TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(4))')
echo "Handshake token: $TOKEN"
```

Tell the human (substitute the actual token):

> Invite the bot to the chat where you want to use it, and send this exact
> message: **`codex-lark setup <TOKEN>`**. You'll see the bot reply
> `codex-lark: setup token <TOKEN> verified.` in the chat — that confirms
> both receive and send permissions are working.

Then:

```bash
$CODEX_LARK discover --json --timeout 180 --handshake-token "$TOKEN"
```

The listener filters to events containing the token — so the result is
unambiguous, **auto-pick is safe**, and you do not need the human to confirm
which event is theirs.

Read the JSON. Shapes:

| Result | Meaning | Action |
|---|---|---|
| `{"ok": true, "reply_verified": true, "events": [{...}], ...}` | Receive + send both work | Continue to allowlist write below |
| `{"ok": false, "error": "no events containing handshake token within 180s", ...}` | Human didn't send the message in time, or bot isn't in the chat | Verify Phase 1 prereqs (published, invited, permissions) and retry |
| `{"ok": false, "error": "handshake matched but reply send failed: ...", "events": [...]}` | Got the message but can't reply — usually missing `im:message:send_as_bot` permission | Ask human to add the permission, republish the app version, and retry |
| `{"ok": false, "error": "connect failed: ...", ...}` | WS handshake failed — bad creds, network, or domain | Redo Phase 3 |

On success, write the allowlist. Default to **sender-only** (only the human
who set up can drive the bot). For shared chats, ask first:

> Lock the bot to **just you** (recommended for personal use), or **anyone
> in this chat** (use when the chat is a trusted team)?

```bash
# Sender only (recommended default)
$CODEX_LARK configure --set FEISHU_CODEX_ALLOWED_SENDERS=<sender_id>

# Chat-wide
$CODEX_LARK configure --set FEISHU_CODEX_ALLOWED_CHATS=<chat_id>
```

### Phase 6 — final verify and start

```bash
$CODEX_LARK doctor
```

Expect `allowlist configured (real mode)` and exit 0.

On macOS, offer autostart:

```bash
$CODEX_LARK service install
$CODEX_LARK service start
$CODEX_LARK service status
```

On Linux: tell the human to run `$CODEX_LARK daemon` under their usual
supervisor (systemd user unit, tmux, etc.).

Final check — ask the human to send `codex help` to the bot. The reply test
in Phase 5 already proved send/receive works; this confirms the deployed
daemon is wired up. If no reply:

- macOS: `$CODEX_LARK service status` — confirm LaunchAgent is loaded.
- Tail `~/.codex/bridges/codex-lark-minimal/logs/bridge.log` for the inbound
  event. If the event arrives but no reply, re-run `$CODEX_LARK doctor`.

## Hard rules for the agent

- **Never pass secrets via `--set`.** The CLI enforces this; don't even try.
- **Always use `--handshake-token` with `discover`.** Without it you have to
  ask the human to manually match a text preview, which is brittle.
- **Never guess workspace paths or domain.** Workspaces: ask the human.
  Domain: let `configure` auto-detect.
- **Gate every phase on `doctor` exit 0.** Don't paper over a failure.
- **Don't skip Phase 5.** Real mode requires an allowlist.
