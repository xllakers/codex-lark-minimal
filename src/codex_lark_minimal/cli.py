"""Command line interface."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

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

    return 2


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
