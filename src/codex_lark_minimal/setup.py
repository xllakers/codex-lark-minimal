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
from typing import Any, List, Optional, Tuple

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
    try:
        events = asyncio.run(_listen_for_events(app_id, app_secret, domain))
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


async def _listen_for_events(app_id: str, app_secret: str, domain: str) -> List[Tuple[Any, str]]:
    try:
        from lark_oapi.channel import FeishuChannel
    except ImportError as exc:
        raise RuntimeError("lark-oapi not installed: %s" % exc) from exc

    channel = FeishuChannel(app_id=app_id, app_secret=app_secret, domain=domain)
    events: List[Tuple[Any, str]] = []
    first_event = asyncio.Event()

    async def on_message(msg: Any) -> None:
        text = str(get_nested(msg, "content_text") or "")
        meta = event_meta(msg)
        if not meta.sender_id or not meta.chat_id:
            return
        if len(events) >= MAX_OBSERVED:
            events.pop(0)
        events.append((meta, text))
        first_event.set()

    async def on_error(err: Any) -> None:
        # Surface but don't abort — discovery may still capture a valid event.
        print("  channel warning: %s" % redact(str(err), max_chars=200), flush=True)

    channel.on("message", on_message)
    channel.on("error", on_error)

    try:
        await channel.start_background(timeout=15)
    except Exception as exc:
        raise RuntimeError("connect failed: %s" % redact(str(exc), max_chars=200)) from exc

    try:
        try:
            await asyncio.wait_for(first_event.wait(), timeout=DISCOVERY_TIMEOUT)
        except asyncio.TimeoutError:
            return []
        # Brief grace window in case multiple messages arrived in quick succession.
        await asyncio.sleep(GRACE_WINDOW)
        return list(events)
    finally:
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
