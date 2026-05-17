"""Interactive setup wizard.

One command (`codex-lark setup`) walks the operator through:

  1. App ID / Secret / domain prompts, with a live token check.
  2. Workspace alias=path prompt.
  3. Long-connection "discovery" listener: capture inbound sender_id / chat_id
     from your first test message, with no side effects (no Codex job runs).
  4. Pick which observed identity to allowlist.
  5. Append a marked block to config.env (atomic write, 600 perms preserved).
  6. Optional LaunchAgent install on macOS.

The wizard never spawns a worker; the discovery listener has no BridgeController
attached. It exists purely to read identifiers off the wire.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import date
from getpass import getpass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from codex_lark_minimal.config import (
    Config,
    default_config_path,
    normalize_domain,
    parse_workspaces,
)
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
    print("Press Ctrl-C any time to abort. Defaults shown in brackets.")
    print()

    try:
        app_id, app_secret, domain = prompt_credentials(config)
        if app_id and app_secret:
            # Token check before opening the long connection — fail fast.
            ok, message = feishu_token_check_proxy(app_id, app_secret, domain)
            print(("OK:   " if ok else "FAIL: ") + message)
            if not ok:
                print("Fix credentials and re-run `codex-lark setup`.")
                return 2
        else:
            print("App ID / Secret skipped; discovery and real mode disabled.")

        workspaces_text = prompt_workspaces(config)

        senders, chats = "", ""
        if app_id and app_secret and prompt_yes_no(
            "Discover sender_id / chat_id by listening for one Lark message?",
            default=True,
        ):
            picked = discover_identity(app_id, app_secret, domain)
            if picked is not None:
                senders, chats = picked

        block = build_append_block({
            "FEISHU_APP_ID": app_id or "",
            "FEISHU_APP_SECRET": app_secret or "",
            "FEISHU_DOMAIN": domain,
            "FEISHU_CODEX_WORKSPACES": workspaces_text,
            "FEISHU_CODEX_ALLOWED_SENDERS": senders,
            "FEISHU_CODEX_ALLOWED_CHATS": chats,
        })
        write_append_block(config_path, block)

        allowlist_ok = bool(senders or chats)

        if not allowlist_ok:
            # Partial setup: credentials and workspaces written, but no
            # allowlist. The daemon will start in dry-run (allowlist-empty ⇒
            # dry-run by design). Skip the LaunchAgent prompt so the user
            # doesn't end up with an autostarting daemon that silently
            # ignores all messages, and tell them clearly what to do next.
            print()
            print("Wrote partial setup to %s (no allowlist captured)." % config_path)
            print("Daemon would start in dry-run — it logs events but doesn't spawn Codex.")
            print()
            print("To go live, either:")
            print("  - Re-run `codex-lark setup` (after fixing the discovery error if any)")
            print("  - Run `codex-lark discover --json --handshake-token …` and feed the")
            print("    result to `codex-lark configure --set FEISHU_CODEX_ALLOWED_SENDERS=…`")
            print()
            print("Verify partial state with: codex-lark doctor")
            return 1

        print("Wrote setup block to %s" % config_path)

        if sys.platform == "darwin" and prompt_yes_no(
            "Install macOS LaunchAgent for autostart?", default=False
        ):
            from codex_lark_minimal.cli import service_command

            service_command(config, "install")
            print("Run `codex-lark service start` when ready.")

        print()
        print("Verify with:  codex-lark doctor")
        print("Start with:   codex-lark daemon")
        return 0
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


def prompt_yes_no(label: str, *, default: bool) -> bool:
    suffix = " [Y/n]" if default else " [y/N]"
    raw = input("%s%s: " % (label, suffix)).strip().lower()
    if not raw:
        return default
    return raw[0] == "y"


def prompt_credentials(config: Config) -> Tuple[Optional[str], Optional[str], str]:
    app_id = prompt("Feishu/Lark App ID", default=config.app_id or "")
    app_secret = prompt_secret("Feishu/Lark App Secret", default=config.app_secret or "")
    domain_default = config.domain or "https://open.feishu.cn"
    choice = prompt(
        "Domain — [f]eishu CN / [l]ark global / paste URL",
        default="f" if "feishu" in domain_default else "l",
    ).strip().lower()
    if choice == "f":
        domain = "https://open.feishu.cn"
    elif choice == "l":
        domain = "https://open.larksuite.com"
    else:
        domain = normalize_domain(choice)
    return (app_id or None, app_secret or None, domain)


def prompt_workspaces(config: Config) -> str:
    if config.workspaces:
        existing = ",".join("%s=%s" % (alias, path) for alias, path in config.workspaces.items())
    else:
        existing = ""
    while True:
        raw = prompt("Workspaces (comma-separated alias=path)", default=existing)
        if not raw:
            print("  At least one workspace required.")
            continue
        try:
            parsed = parse_workspaces(raw)
        except Exception as exc:
            print("  Invalid: %s" % exc)
            continue
        missing = [p for p in parsed.values() if not p.is_dir()]
        if missing:
            print("  Warning: path(s) do not exist on disk:")
            for p in missing:
                print("    - %s" % p)
            if not prompt_yes_no("  Save anyway?", default=False):
                continue
        return raw


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def feishu_token_check_proxy(app_id: str, app_secret: str, domain: str) -> Tuple[bool, str]:
    """Token check that doesn't need a full Config object."""
    from types import SimpleNamespace

    stub = SimpleNamespace(app_id=app_id, app_secret=app_secret, domain=domain)
    return feishu_token_check(stub)  # type: ignore[arg-type]


def discover_identity(app_id: str, app_secret: str, domain: str) -> Optional[Tuple[str, str]]:
    """Run a one-shot listener. Returns (senders_csv, chats_csv) or None."""
    print()
    print("Open Lark, invite the bot to a chat (or DM it), and send any message.")
    print("Listening up to %ds for the first event..." % DISCOVERY_TIMEOUT)
    print("(Diagnostic activity is printed to stderr below.)")
    try:
        events, _reply_test = asyncio.run(
            _listen_for_events(app_id, app_secret, domain)
        )
    except RuntimeError as exc:
        print("Discovery failed: %s" % redact(str(exc), max_chars=200))
        return None
    if not events:
        print("No messages received. Checklist:")
        print("  - App version published?")
        print("  - Bot invited to the chat?")
        print("  - im.message.receive_v1 event subscribed?")
        return None
    print()
    print("Observed inbound events:")
    for i, (meta, text) in enumerate(events, 1):
        preview = redact(text, max_chars=60).replace("\n", " ")
        print("  [%d] sender=%s chat=%s text=%r" % (i, meta.sender_id, meta.chat_id, preview))
    while True:
        raw = prompt("Pick number to add to allowlist (or 's' to skip)", default="1")
        if raw.lower() == "s":
            return None
        try:
            idx = int(raw)
            if 1 <= idx <= len(events):
                break
        except ValueError:
            pass
        print("  Enter a number from 1 to %d, or 's'." % len(events))
    meta, _ = events[idx - 1]
    scope = prompt(
        "Allowlist scope — [s]ender / [c]hat / [b]oth",
        default="s",
    ).strip().lower()
    if scope.startswith("c"):
        return ("", meta.chat_id)
    if scope.startswith("b"):
        return (meta.sender_id, meta.chat_id)
    return (meta.sender_id, "")


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
