"""Command line interface."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

# Pre-import lark_oapi.channel here, before any asyncio.run() can fire. The
# transitive load of lark_oapi/ws/client.py contains a module-level
# `loop = asyncio.get_event_loop()` — if that runs *inside* a running loop
# (e.g. when the wizard or `discover` lazily imports it from within
# `asyncio.run(...)`), lark captures our loop. WSClient.start() then runs in
# an executor thread and calls `loop.run_until_complete(...)` on that same
# (already-running) loop, raising "This event loop is already running".
# Pre-importing at module load time gives lark its own fresh loop.
try:
    import lark_oapi.channel  # noqa: F401
except ImportError:
    # lark-oapi is a required dep; if it's missing the doctor / daemon will
    # surface a clearer message at runtime.
    pass

from codex_lark_minimal.bridge import BridgeController, EventMeta
from codex_lark_minimal.codex import format_recent_sessions
from codex_lark_minimal.config import ConfigError, ensure_dirs, load_config, validate_config
from codex_lark_minimal.doctor import run_doctor
from codex_lark_minimal.feishu import run_daemon_sync
from codex_lark_minimal.state import StateStore, summarize_job, summarize_jobs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-lark")
    parser.add_argument("--config", help="Path to config.env")
    subcommands = parser.add_subparsers(dest="command", required=True)

    subcommands.add_parser("daemon")
    subcommands.add_parser("doctor")
    subcommands.add_parser("recent")
    subcommands.add_parser("setup")

    configure = subcommands.add_parser(
        "configure",
        help="Non-interactive config writer (intended for AI agents and scripts).",
    )
    configure.add_argument(
        "--set",
        action="append",
        default=[],
        dest="set_kv",
        metavar="KEY=VALUE",
        help="Set a config key. Repeatable. Avoid passing secrets here; use --stdin.",
    )
    configure.add_argument(
        "--stdin",
        action="store_true",
        help="Read additional KEY=VALUE lines from stdin (one per line). Use for secrets.",
    )

    discover = subcommands.add_parser(
        "discover",
        help="One-shot listener: capture sender/chat IDs from the next inbound Lark message.",
    )
    discover.add_argument("--timeout", type=int, default=180)
    discover.add_argument("--json", dest="as_json", action="store_true")
    discover.add_argument(
        "--handshake-token",
        default=None,
        help=(
            "Only capture events whose text contains this token, and reply in-chat to "
            "verify the bot's send permission. Use a short unguessable string and ask "
            "the human to include it in their test message."
        ),
    )

    status = subcommands.add_parser("status")
    status.add_argument("run_id", nargs="?")

    stop = subcommands.add_parser("stop")
    stop.add_argument("run_id")

    simulate = subcommands.add_parser("simulate")
    simulate.add_argument("text")
    simulate.add_argument("--sender", default="local")
    simulate.add_argument("--chat", default="local")

    service = subcommands.add_parser("service")
    service_subcommands = service.add_subparsers(dest="service_command", required=True)
    for name in ("install", "start", "stop", "status"):
        service_subcommands.add_parser(name)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(Path(args.config).expanduser() if args.config else None)
    except ConfigError as exc:
        print("config error: %s" % exc, file=sys.stderr)
        return 2
    ensure_dirs(config)

    if args.command == "doctor":
        ok, text = run_doctor(config)
        print(text)
        return 0 if ok else 2

    if args.command == "setup":
        from codex_lark_minimal.setup import run_setup

        return run_setup(config)

    if args.command == "daemon":
        errors = validate_config(config, for_daemon=True)
        if errors:
            for error in errors:
                print("config error: %s" % error, file=sys.stderr)
            return 2
        run_daemon_sync(config)
        return 0

    if args.command == "status":
        store = StateStore(config)
        if args.run_id:
            record = store.get(args.run_id)
            if record is None:
                print("No bridge job found for run_id: %s" % args.run_id)
                return 1
            print(summarize_job(record, include_tail=True))
        else:
            print(summarize_jobs(store.list(limit=10)))
        return 0

    if args.command == "stop":
        store = StateStore(config)
        try:
            record = store.stop(args.run_id)
        except KeyError:
            print("No bridge job found for run_id: %s" % args.run_id)
            return 1
        print("Stopped %s. Current status: %s" % (args.run_id, record.status))
        return 0

    if args.command == "recent":
        print(format_recent_sessions(config))
        return 0

    if args.command == "simulate":
        controller = BridgeController(config)
        meta = EventMeta(chat_id=args.chat, sender_id=args.sender, event_id="local", message_id="local")
        print(controller.handle_text(args.text, meta))
        return 0

    if args.command == "service":
        return service_command(config, args.service_command)

    if args.command == "configure":
        return configure_command(config, args.set_kv, args.stdin)

    if args.command == "discover":
        return discover_command(
            config, args.timeout, args.as_json, handshake_token=args.handshake_token
        )

    return 2


# Suffixes that indicate a value must never be passed via --set (which lands
# in the agent's tool-call log + shell history). Enforced by configure_command;
# the agent is told to route these through --stdin instead.
_SECRET_KEY_SUFFIXES = ("_SECRET", "_TOKEN", "_PASSWORD")


def _is_secret_key(key: str) -> bool:
    upper = key.upper()
    return any(upper.endswith(suffix) for suffix in _SECRET_KEY_SUFFIXES)


def configure_command(config, set_kv: list, from_stdin: bool) -> int:
    pairs: dict = {}
    for item in set_kv:
        if "=" not in item:
            print("--set requires KEY=VALUE: %s" % item, file=sys.stderr)
            return 2
        key, value = item.split("=", 1)
        key = key.strip()
        if _is_secret_key(key):
            print(
                "refusing --set for sensitive key %r — pass it via --stdin to keep "
                "it out of tool-call logs and shell history." % key,
                file=sys.stderr,
            )
            return 2
        pairs[key] = value
    if from_stdin:
        for raw in sys.stdin:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            pairs[key.strip()] = value.strip()
    if not pairs:
        print("No values to write.", file=sys.stderr)
        return 1
    # Fail fast on malformed workspaces so the agent doesn't write garbage and
    # discover the error two phases later via the doctor.
    if "FEISHU_CODEX_WORKSPACES" in pairs:
        from codex_lark_minimal.config import parse_workspaces

        try:
            parse_workspaces(pairs["FEISHU_CODEX_WORKSPACES"])
        except Exception as exc:
            print("invalid FEISHU_CODEX_WORKSPACES: %s" % exc, file=sys.stderr)
            return 2

    # Domain auto-detect: if creds are being written but the domain isn't, probe
    # Feishu CN and Lark global with the given credentials and adopt whichever
    # authenticates. Eliminates one question for the agent ⇄ human exchange.
    if (
        "FEISHU_APP_ID" in pairs
        and "FEISHU_APP_SECRET" in pairs
        and "FEISHU_DOMAIN" not in pairs
    ):
        from codex_lark_minimal.setup import feishu_token_check_proxy

        for candidate in ("https://open.feishu.cn", "https://open.larksuite.com"):
            ok, _ = feishu_token_check_proxy(
                pairs["FEISHU_APP_ID"], pairs["FEISHU_APP_SECRET"], candidate
            )
            if ok:
                pairs["FEISHU_DOMAIN"] = candidate
                print("Auto-detected domain: %s" % candidate)
                break
        else:
            print(
                "Could not auto-detect domain (both Feishu and Lark rejected the "
                "credentials). Either set FEISHU_DOMAIN explicitly or re-check the "
                "App ID / Secret.",
                file=sys.stderr,
            )

    from codex_lark_minimal.config import default_config_path
    from codex_lark_minimal.setup import build_append_block, write_append_block

    config_path = config.config_path or default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        config_path.touch(mode=0o600)
    write_append_block(config_path, build_append_block(pairs))
    print("Wrote %d key(s) to %s" % (len(pairs), config_path))
    return 0


def discover_command(
    config,
    timeout: int,
    as_json: bool,
    *,
    handshake_token: "str | None" = None,
) -> int:
    import asyncio
    import json as _json

    from codex_lark_minimal.redaction import redact
    from codex_lark_minimal.setup import _listen_for_events

    def emit_error(message: str, exit_code: int, *, events: "list | None" = None) -> int:
        payload = {"ok": False, "error": message, "timeout_seconds": timeout}
        if events is not None:
            payload["events"] = events
        if as_json:
            print(_json.dumps(payload))
        else:
            print(message, file=sys.stderr)
        return exit_code

    if not config.app_id or not config.app_secret:
        return emit_error(
            "FEISHU_APP_ID and FEISHU_APP_SECRET required (run `codex-lark configure` first)",
            2,
        )

    try:
        events, reply_test = asyncio.run(
            _listen_for_events(
                config.app_id,
                config.app_secret,
                config.domain,
                timeout=timeout,
                handshake_token=handshake_token,
            )
        )
    except RuntimeError as exc:
        return emit_error(str(exc), 2)

    if not events:
        if handshake_token:
            return emit_error(
                "no events containing handshake token within %ds" % timeout, 1
            )
        return emit_error("no events received within %ds" % timeout, 1)

    out_events = [
        {
            "sender_id": meta.sender_id,
            "chat_id": meta.chat_id,
            "text_preview": redact(text, max_chars=80),
        }
        for meta, text in events
    ]

    # Handshake mode: in-chat reply test result decides overall ok.
    if handshake_token and reply_test is not None and not reply_test.get("ok"):
        return emit_error(
            "handshake matched but reply send failed: %s" % reply_test.get("error", ""),
            2,
            events=out_events,
        )

    if as_json:
        payload = {"ok": True, "events": out_events, "timeout_seconds": timeout}
        if handshake_token and reply_test and reply_test.get("ok"):
            payload["reply_verified"] = True
        print(_json.dumps(payload))
        return 0
    for i, (meta, text) in enumerate(events, 1):
        preview = redact(text, max_chars=80).replace("\n", " ")
        print("[%d] sender=%s chat=%s text=%r" % (i, meta.sender_id, meta.chat_id, preview))
    if handshake_token and reply_test and reply_test.get("ok"):
        print("Reply sent: setup connection verified.")
    return 0


def service_command(config, action: str) -> int:
    if sys.platform != "darwin":
        print("service commands currently support macOS launchd only")
        return 2
    label = "local.codex-lark-minimal"
    plist = Path.home() / "Library" / "LaunchAgents" / (label + ".plist")
    target = "gui/%s/%s" % (os.getuid(), label)
    if action == "install":
        run_sh = config.home / "run.sh"
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(plist_text(label, run_sh, config.log_path), encoding="utf-8")
        print("Installed LaunchAgent: %s" % plist)
        return 0
    if action == "start":
        if not plist.exists():
            print("LaunchAgent not installed. Run: service install")
            return 1
        subprocess.run(["launchctl", "bootout", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        code = subprocess.run(["launchctl", "bootstrap", "gui/%s" % os.getuid(), str(plist)]).returncode
        if code == 0:
            subprocess.run(["launchctl", "kickstart", "-k", target])
        return code
    if action == "stop":
        subprocess.run(["launchctl", "bootout", target])
        return 0
    if action == "status":
        return subprocess.run(["launchctl", "print", target]).returncode
    return 2


def plist_text(label: str, run_sh: Path, log_path: Path) -> str:
    return """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{run_sh}</string>
    <string>daemon</string>
  </array>
  <key>RunAtLoad</key>
  <false/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>
  <key>StandardOutPath</key>
  <string>{log_path}</string>
  <key>StandardErrorPath</key>
  <string>{log_path}</string>
</dict>
</plist>
""".format(label=label, run_sh=run_sh, log_path=log_path)


if __name__ == "__main__":
    raise SystemExit(main())
