"""Interactive setup wizard.

One command (`codex-lark setup`) prompts for *only* App ID + App Secret, then
runs everything else with sensible defaults:

  1. Token check (fail fast on bad creds).
  2. Domain auto-detected from existing config or defaulted to Feishu CN.
  3. Long-connection discovery: auto-picks the first inbound event and
     allowlists both sender_id and chat_id. No "pick a number" or
     "sender/chat/both" prompts — single message in, allowlist out.
  4. Appends a dated block to config.env (atomic write, 600 perms preserved).
  5. Prints copy-paste commands for LaunchAgent install + workspace config —
     does not run them itself.

The wizard never spawns a worker; the discovery listener has no
BridgeController attached. It exists purely to read identifiers off the wire.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from datetime import date
from getpass import getpass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from codex_lark_minimal.config import Config, default_config_path
from codex_lark_minimal.doctor import feishu_token_check
from codex_lark_minimal.feishu import event_meta, get_nested
from codex_lark_minimal.redaction import redact

DISCOVERY_TIMEOUT = 180  # seconds to wait for first inbound event
GRACE_WINDOW = 5  # seconds after first event to wait for siblings
MAX_OBSERVED = 5


def is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def run_setup(config: Config) -> int:
    if not is_interactive():
        print("codex-lark setup: non-interactive shell; skipping wizard.")
        return 0

    config_path = config.config_path or default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        config_path.touch(mode=0o600)

    print()
    print("== codex-lark setup ==")
    print("Asks for App ID + App Secret. Everything else has sensible defaults.")
    print("Ctrl-C aborts without writing.")
    print()

    try:
        app_id, app_secret = prompt_credentials(config)
        if not (app_id and app_secret):
            print("App ID / App Secret are required. Aborting.")
            return 2

        # Domain: keep existing if set, else default to Feishu CN. No prompt.
        # International Lark users override post-setup with:
        #   codex-lark configure --set FEISHU_DOMAIN=https://open.larksuite.com
        domain = config.domain or "https://open.feishu.cn"

        ok, message = feishu_token_check_proxy(app_id, app_secret, domain)
        print(("OK:   " if ok else "FAIL: ") + message)
        if not ok:
            print("Fix credentials and re-run `codex-lark setup`.")
            return 2

        senders, chats = "", ""
        picked = discover_identity(app_id, app_secret, domain)
        if picked is not None:
            senders, chats = picked

        # Capture absolute path now (operator's shell has full PATH);
        # launchd's minimal PATH would fail at job-spawn otherwise.
        codex_bin = ""
        if not config.codex_bin or config.codex_bin == "codex":
            resolved = shutil.which("codex")
            if resolved:
                codex_bin = resolved
                print("Resolved codex CLI: %s" % resolved)
            else:
                print("WARNING: `codex` not on PATH. Set FEISHU_CODEX_CODEX_BIN later.")

        block = build_append_block({
            "FEISHU_APP_ID": app_id,
            "FEISHU_APP_SECRET": app_secret,
            "FEISHU_DOMAIN": domain,
            "FEISHU_CODEX_ALLOWED_SENDERS": senders,
            "FEISHU_CODEX_ALLOWED_CHATS": chats,
            "FEISHU_CODEX_CODEX_BIN": codex_bin,
        })
        write_append_block(config_path, block)
        print("Wrote setup block to %s" % config_path)

        allowlist_ok = bool(senders or chats)
        print()
        if allowlist_ok:
            print("Setup complete. Next steps (copy-paste):")
        else:
            print("Setup wrote credentials but no allowlist (no message received).")
            print("Daemon will start in dry-run until allowlist is populated.")
            print("Re-run `codex-lark setup` and DM the bot during the discovery window,")
            print("or run `codex-lark discover --handshake-token <token>` later.")
            print()
            print("Next steps once live:")
        print("  codex-lark doctor                       # verify config")
        if not config.workspaces:
            print("  codex-lark configure \\                  # add a workspace (needed for")
            print("    --set FEISHU_CODEX_WORKSPACES=myproj=/path/to/myproj")
            print("                                          # `codex myproj: …` to spawn jobs)")
        if sys.platform == "darwin":
            print("  codex-lark service install              # macOS autostart (optional)")
            print("  codex-lark service start                # start the daemon")
        else:
            print("  codex-lark daemon                       # start the daemon (foreground)")
        print()
        print("Then DM the bot `codex help` to verify end-to-end.")
        return 0 if allowlist_ok else 1
    except KeyboardInterrupt:
        print()
        print("Aborted. Config not modified.")
        return 130


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


def prompt(label: str, *, default: str = "") -> str:
    suffix = (" [%s]" % default) if default else ""
    value = input("%s%s: " % (label, suffix)).strip()
    return value or default


def prompt_secret(label: str, *, default: str = "") -> str:
    hint = " [unchanged]" if default else ""
    raw = getpass("%s%s: " % (label, hint))
    return raw.strip() or default


def prompt_credentials(config: Config) -> Tuple[Optional[str], Optional[str]]:
    app_id = prompt("Feishu/Lark App ID", default=config.app_id or "")
    app_secret = prompt_secret("Feishu/Lark App Secret", default=config.app_secret or "")
    return (app_id or None, app_secret or None)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def feishu_token_check_proxy(app_id: str, app_secret: str, domain: str) -> Tuple[bool, str]:
    """Token check that doesn't need a full Config object."""
    from types import SimpleNamespace

    stub = SimpleNamespace(app_id=app_id, app_secret=app_secret, domain=domain)
    return feishu_token_check(stub)  # type: ignore[arg-type]


def discover_identity(app_id: str, app_secret: str, domain: str) -> Optional[Tuple[str, str]]:
    """Run a one-shot listener. Returns (senders_csv, chats_csv) or None.

    Auto-picks the first inbound event and allowlists both sender and chat.
    This is the right default for the most common case (operator DMing their
    own bot during setup): the first message is the operator, and allowing
    both sender+chat means later messages still match even if Feishu rotates
    the chat_id for the same sender (rare but observed).
    """
    print()
    print("Now DM the bot from Feishu/Lark (any message, e.g. 'hi').")
    print("Listening up to %ds for the first event..." % DISCOVERY_TIMEOUT)
    print("(WS diagnostics print to stderr below.)")
    try:
        events, _reply_test = asyncio.run(
            _listen_for_events(app_id, app_secret, domain)
        )
    except RuntimeError as exc:
        print("Discovery failed: %s" % redact(str(exc), max_chars=200))
        return None
    if not events:
        print("No messages received within %ds." % DISCOVERY_TIMEOUT)
        return None
    meta, text = events[0]
    preview = redact(text, max_chars=60).replace("\n", " ")
    print()
    print("Captured: sender=%s chat=%s text=%r" % (meta.sender_id, meta.chat_id, preview))
    print("Added both to allowlist.")
    if len(events) > 1:
        # Multiple senders during the 180s window means a shared bot — we
        # only allowlist the first. Surface this so the operator knows.
        print(
            "Note: %d other event(s) observed and ignored. Allowlist additional"
            " senders later via `codex-lark configure --set"
            " FEISHU_CODEX_ALLOWED_SENDERS=...`." % (len(events) - 1)
        )
    return (meta.sender_id, meta.chat_id)


def _diag(msg: str) -> None:
    """Discovery diagnostic line — to stderr so it doesn't corrupt JSON stdout."""
    print(msg, file=sys.stderr, flush=True)


_LARK_SECRET_SCRUB_INSTALLED = False


def _install_lark_secret_scrub() -> None:
    """Install a logging filter that masks secrets in lark-oapi log lines.

    Covers two categories of credential that lark logs by default:

      - ``app_secret`` in HTTP request bodies (DEBUG-level — we don't enable
        DEBUG ourselves, but a future lark version may surface this at INFO
        and this is defense in depth).
      - ``access_key`` and ``ticket`` in the WS endpoint URL printed at INFO
        when the connection is established. These are session-scoped tokens,
        rotated per reconnect, but should still not appear in terminal
        scrollback / pasted diagnostic output.

    The filter rewrites the message in-place before any handler sees it, so
    even external handlers (file, syslog, structured) get the masked form.
    Idempotent — installs once per process.
    """
    global _LARK_SECRET_SCRUB_INSTALLED
    if _LARK_SECRET_SCRUB_INSTALLED:
        return
    import logging
    import re

    # App Secret (most sensitive — long-lived).
    json_pat = re.compile(r'("app_secret"\s*:\s*)"[^"]*"')
    py_pat = re.compile(r"('app_secret'\s*:\s*)'[^']*'")
    kv_pat = re.compile(r"(\bapp_secret\s*=\s*)[^\s&'\"]+")

    # Session tokens (lower severity — rotated per WS connect).
    url_token_pat = re.compile(r"(\b(?:access_key|ticket)=)[^&\s'\"]+")

    class _Scrub(logging.Filter):
        def filter(self, record: "logging.LogRecord") -> bool:
            try:
                msg = record.getMessage()
            except Exception:
                return True
            if not ("app_secret" in msg or "access_key=" in msg or "ticket=" in msg):
                return True
            scrubbed = json_pat.sub(r'\1"***"', msg)
            scrubbed = py_pat.sub(r"\1'***'", scrubbed)
            scrubbed = kv_pat.sub(r"\1***", scrubbed)
            scrubbed = url_token_pat.sub(r"\1***", scrubbed)
            if scrubbed != msg:
                record.msg = scrubbed
                record.args = None
            return True

    logging.getLogger("Lark").addFilter(_Scrub())
    _LARK_SECRET_SCRUB_INSTALLED = True


async def _listen_for_events(
    app_id: str,
    app_secret: str,
    domain: str,
    *,
    timeout: int = DISCOVERY_TIMEOUT,
    handshake_token: Optional[str] = None,
) -> Tuple[List[Tuple[Any, str]], Optional[Dict[str, Any]]]:
    """Return (events, reply_test_result).

    Without ``handshake_token``: capture up to the last MAX_OBSERVED events
    that arrive within ``timeout`` seconds. reply_test_result is ``None``.

    With ``handshake_token``: only events whose text *contains* the token are
    captured (so auto-pick is safe). On first match, we post a short
    confirmation reply to the same chat — this validates the
    ``im:message:send_as_bot`` permission during setup, not after deploy.
    reply_test_result is ``{"ok": True}`` on send success or
    ``{"ok": False, "error": "..."}`` on send failure.

    Diagnostic lines (WS-connected, bot identity, each received event,
    drops, post-30s checklist) go to stderr so the user can see what the
    listener is doing — and so they stay out of the agent's JSON stdout.
    """
    try:
        from lark_oapi.channel import FeishuChannel
    except ImportError as exc:
        raise RuntimeError("lark-oapi not installed: %s" % exc) from exc

    # Defense in depth: scrub app_secret from any lark log line in case lark
    # adds credential logging at INFO in a future version. We deliberately do
    # NOT elevate lark to DEBUG ourselves — its DEBUG output mixes WS frame
    # traces (which we'd like) with HTTP request bodies that contain the
    # App Secret in plaintext (which we very much do not). We instrument
    # what we need at our own layer below.
    _install_lark_secret_scrub()

    channel = FeishuChannel(app_id=app_id, app_secret=app_secret, domain=domain)
    events: List[Tuple[Any, str]] = []
    first_match = asyncio.Event()
    matched: Dict[str, Any] = {"meta": None}
    shutting_down: Dict[str, bool] = {"value": False}
    reconnect_count: Dict[str, int] = {"value": 0}

    async def on_message(msg: Any) -> None:
        text = str(get_nested(msg, "content_text") or "")
        meta = event_meta(msg)
        preview = text[:60].replace("\n", " ")
        _diag(
            "  received: sender=%s chat=%s text=%r"
            % (meta.sender_id or "<missing>", meta.chat_id or "<missing>", preview)
        )
        if not meta.sender_id or not meta.chat_id:
            _diag("  → dropped (missing sender or chat id)")
            return
        if handshake_token is not None:
            if handshake_token not in text:
                _diag("  → dropped (handshake token %r not in text)" % handshake_token)
                return
            if matched["meta"] is None:
                events.clear()
                events.append((meta, text))
                matched["meta"] = meta
                first_match.set()
            return
        if len(events) >= MAX_OBSERVED:
            events.pop(0)
        events.append((meta, text))
        first_match.set()

    async def on_error(err: Any) -> None:
        _diag("  channel error: %s" % redact(str(err), max_chars=200))

    def on_reconnecting(*_args: Any) -> None:
        # Suppress during our own clean shutdown — lark fires this callback
        # after our channel.disconnect() and it's not a real mid-flight event.
        if shutting_down["value"]:
            return
        # Frequent reconnects with no `received:` in between usually means
        # Feishu accepts the WS but never delivers events — common when the
        # released app version has long-connection enabled but no event
        # subscriptions wired up, so the server lets keepalive lapse.
        reconnect_count["value"] += 1
        _diag("  WS reconnecting (keepalive lapsed or transport reset)")

    def on_reconnected(*_args: Any) -> None:
        if shutting_down["value"]:
            return
        _diag("  WS reconnected")

    channel.on("message", on_message)
    channel.on("error", on_error)
    channel.on("reconnecting", on_reconnecting)
    channel.on("reconnected", on_reconnected)

    try:
        await channel.start_background(timeout=15)
    except Exception as exc:
        raise RuntimeError("connect failed: %s" % redact(str(exc), max_chars=200)) from exc

    # Surface bot identity so the user can verify which bot they're DMing.
    bot = channel.bot_identity
    if bot is not None:
        _diag(
            "  WS connected. Bot: open_id=%s name=%s"
            % (getattr(bot, "open_id", None) or "?", getattr(bot, "name", None) or "?")
        )
    else:
        _diag("  WS connected. (bot identity not yet resolved)")

    async def diagnostic_hint() -> None:
        await asyncio.sleep(30)
        if first_match.is_set():
            return
        _diag(
            "  no events yet (30s elapsed). If your message didn't show up "
            "as 'received' above, verify:"
        )
        _diag("    1. The bot you DMed matches the open_id printed above.")
        _diag("    2. Your app version is *published* (not draft) on open.feishu.cn / open.larksuite.com.")
        _diag("    3. Event `im.message.receive_v1` is subscribed in the *released* version.")
        _diag("    4. In groups: the bot is in the chat and you @mention it, OR the chat allows bot-receive.")
        _diag("  Look for 'WS reconnecting' above — if it keeps reconnecting without events,")
        _diag("  Feishu accepts the WS but isn't sending anything (usually cause #3).")

    async def ws_alive_heartbeat() -> None:
        # Independent of lark's reconnect events: poll the underlying WS
        # connection every 30s and report alive/dead. Gives the operator a
        # rhythm to compare against — if heartbeats stop, the WS is gone.
        while not first_match.is_set():
            await asyncio.sleep(30)
            if first_match.is_set():
                return
            ws = getattr(channel, "ws_client", None)
            conn = getattr(ws, "_conn", None) if ws is not None else None
            _diag("  ws-alive=%s" % ("yes" if conn is not None else "no"))

    hint_task = asyncio.create_task(diagnostic_hint())
    heartbeat_task = asyncio.create_task(ws_alive_heartbeat())

    try:
        try:
            await asyncio.wait_for(first_match.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return [], None

        if handshake_token is None:
            await asyncio.sleep(GRACE_WINDOW)
            return list(events), None

        # Handshake mode: bot replies in the chat to verify send permission.
        chat_id = matched["meta"].chat_id
        try:
            await channel.send(
                chat_id,
                {"text": "codex-lark: setup token %s verified." % handshake_token},
            )
            reply_result: Dict[str, Any] = {"ok": True}
        except Exception as exc:
            reply_result = {"ok": False, "error": redact(str(exc), max_chars=200)}
        return list(events), reply_result
    finally:
        # If we're returning empty-handed, print an actionable diagnosis based
        # on what we *did* see — the two failure modes look identical from
        # outside but point at different fixes.
        if not first_match.is_set():
            _diag("")
            if reconnect_count["value"] > 0:
                _diag(
                    "  Diagnosis: WS reconnected %d time(s) without delivering any events."
                    % reconnect_count["value"]
                )
                _diag(
                    "  Symptom matches: released app version has long-connection enabled"
                    " but no event subscriptions wired up, so Feishu accepts the WS and"
                    " then lets the keepalive lapse."
                )
                _diag("  Fix: in Lark portal → App Release → Version Management → released")
                _diag("  version detail → Events Configuration. If `im.message.receive_v1`")
                _diag("  isn't listed, add it in the main Events tab + Permissions, then")
                _diag("  create + publish a new version.")
            else:
                _diag(
                    "  Diagnosis: WS stayed connected for the full %ds, 0 events received."
                    % timeout
                )
                _diag(
                    "  This is almost certainly a Feishu-side configuration issue, not"
                    " network or code. Check (in order of likelihood):"
                )
                _diag(
                    "    a) You're DMing the bot from the Feishu (飞书) Chinese client?"
                )
                _diag(
                    "       The bot is registered on open.feishu.cn (CN). Messages sent"
                    " from Lark (international, open.larksuite.com) won't bridge — they're"
                    " separate products with separate message buses."
                )
                _diag(
                    "    b) The currently *released* version's Events Configuration"
                    " includes `im.message.receive_v1`?"
                )
                _diag(
                    "       Lark portal → App Release → Version Management → click the"
                    " released version → Events Configuration. The draft / current"
                    " settings don't count; only the released version's subscriptions"
                    " are wired to this WS."
                )
                _diag(
                    "    c) The released version's Availability scope (可用范围)"
                    " includes your user account?"
                )
                _diag(
                    "       Same page. If 'Specified members' / 指定成员, add yourself"
                    " or switch to 'All members of the tenant' and re-publish."
                )
        shutting_down["value"] = True
        for task in (hint_task, heartbeat_task):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await channel.disconnect()
        except Exception:
            pass
        # lark-oapi's WSClient spawns ping/receive loops and an expiring-cache
        # cron that disconnect() doesn't await. Without explicit cleanup,
        # asyncio.run() prints "Task was destroyed but it is pending" on exit,
        # which looks like setup failed. Cancel anything still alive in our
        # loop and drain it so the wizard returns to the shell cleanly.
        loop = asyncio.get_running_loop()
        leftover = [t for t in asyncio.all_tasks(loop) if t is not asyncio.current_task()]
        for task in leftover:
            task.cancel()
        if leftover:
            await asyncio.gather(*leftover, return_exceptions=True)


# ---------------------------------------------------------------------------
# Config file write
# ---------------------------------------------------------------------------


def build_append_block(values: dict) -> str:
    lines = ["", "# --- codex-lark setup %s ---" % date.today().isoformat()]
    for key, value in values.items():
        if value == "":
            continue
        lines.append("%s=%s" % (key, value))
    lines.append("# --- end ---")
    lines.append("")
    return "\n".join(lines)


def write_append_block(path: Path, block: str) -> None:
    # Atomic-ish: write tempfile next to target, fsync, rename. Preserves 600.
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    new_content = existing + block
    fd, tmp_path = tempfile.mkstemp(prefix=".config.env.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(new_content)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
