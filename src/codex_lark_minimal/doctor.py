"""Diagnostics for local installation and Feishu/Codex readiness."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from typing import List, Tuple

from codex_lark_minimal.config import Config, config_permissions, validate_config
from codex_lark_minimal.redaction import mask_secret, redact
from codex_lark_minimal.state import StateStore


def run_doctor(config: Config) -> Tuple[bool, str]:
    checks: List[Tuple[bool, str]] = []

    checks.append((sys.version_info >= (3, 9), "Python >= 3.9 (%s)" % sys.version.split()[0]))
    checks.append((import_ok("lark_oapi"), "lark-oapi importable"))
    codex_path = shutil.which(config.codex_bin)
    checks.append((bool(codex_path), "Codex CLI found (%s)" % (codex_path or config.codex_bin)))
    if codex_path:
        ok, out = command_ok([config.codex_bin, "--version"])
        checks.append((ok, "Codex version: %s" % redact(out.strip(), max_chars=120)))
        ok, out = command_ok([config.codex_bin, "login", "status"])
        checks.append((ok, "Codex login status: %s" % redact(out.strip(), max_chars=120)))

    if config.config_path:
        checks.append((config.config_path.exists(), "config exists: %s" % config.config_path))
        perms = config_permissions(config.config_path)
        checks.append((perms == "600", "config permissions are 600 (current: %s)" % (perms or "missing")))

    for error in validate_config(config, for_daemon=False):
        checks.append((False, error))

    checks.append((config.home.exists(), "bridge home exists: %s" % config.home))
    store = StateStore(config)
    checks.append((config.jobs_dir.is_dir(), "jobs dir exists: %s" % config.jobs_dir))
    checks.append((config.logs_dir.is_dir(), "logs dir exists: %s" % config.logs_dir))

    if config.app_id and config.app_secret:
        ok, message = feishu_token_check(config)
        checks.append((ok, message))
    else:
        checks.append((config.dry_run, "Feishu credentials missing (OK only in dry-run discovery mode)"))

    if config.allowed_senders or config.allowed_chats:
        checks.append((True, "allowlist configured"))
    else:
        checks.append((
            config.dry_run and config.allow_all,
            "allowlist missing (OK only with dry-run allow-all discovery)",
        ))

    active = store.active_jobs()
    checks.append((True, "active bridge jobs: %s" % len(active)))

    lines = []
    ok_count = 0
    fail_count = 0
    for ok, label in checks:
        if ok:
            ok_count += 1
            lines.append("[OK]   " + label)
        else:
            fail_count += 1
            lines.append("[FAIL] " + label)
    lines.append("")
    lines.append("Results: %s passed, %s failed" % (ok_count, fail_count))
    if config.app_id:
        lines.append("Feishu app id: %s" % mask_secret(config.app_id))
    return fail_count == 0, "\n".join(lines)


def import_ok(module: str) -> bool:
    try:
        __import__(module)
        return True
    except ImportError:
        return False


def command_ok(command: List[str]) -> Tuple[bool, str]:
    try:
        completed = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    return completed.returncode == 0, completed.stdout


def feishu_token_check(config: Config) -> Tuple[bool, str]:
    url = "%s/open-apis/auth/v3/tenant_access_token/internal" % config.domain.rstrip("/")
    payload = json.dumps({"app_id": config.app_id, "app_secret": config.app_secret}).encode("utf-8")
    request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return False, "Feishu token request failed: %s" % redact(str(exc), max_chars=200)
    if int(data.get("code", -1)) == 0:
        return True, "Feishu app credentials valid"
    return False, "Feishu app credentials rejected: %s" % redact(str(data.get("msg") or data), max_chars=200)
