"""Configuration loading for codex-lark-minimal."""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

DEFAULT_HOME = Path.home() / ".codex" / "bridges" / "codex-lark-minimal"
DEFAULT_CODEX_HOME = Path.home() / ".codex"


class ConfigError(Exception):
    """Raised when config is missing or unsafe."""


@dataclass(frozen=True)
class Config:
    app_id: Optional[str]
    app_secret: Optional[str]
    domain: str = "open.feishu.cn"
    allow_all: bool = False
    dry_run: bool = True
    allowed_senders: frozenset = field(default_factory=frozenset)
    allowed_chats: frozenset = field(default_factory=frozenset)
    trigger_prefix: str = "codex"
    workspaces: Dict[str, Path] = field(default_factory=dict)
    default_workspace: str = ""
    codex_bin: str = "codex"
    codex_sandbox: str = "workspace-write"
    codex_approval: str = "never"
    codex_model: Optional[str] = None
    codex_profile: Optional[str] = None
    codex_extra_args: tuple = ()
    max_running: int = 2
    max_prompt_chars: int = 6000
    reply: bool = True
    home: Path = DEFAULT_HOME
    codex_home: Path = DEFAULT_CODEX_HOME
    config_path: Optional[Path] = None

    @property
    def state_dir(self) -> Path:
        return self.home / "state"

    @property
    def jobs_dir(self) -> Path:
        return self.state_dir / "jobs"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def log_path(self) -> Path:
        return self.logs_dir / "bridge.log"


def default_config_path() -> Path:
    return Path(os.environ.get("FEISHU_CODEX_CONFIG", str(DEFAULT_HOME / "config.env"))).expanduser()


def load_config(path: Optional[Path] = None) -> Config:
    config_path = path or default_config_path()
    env = parse_env_file(config_path)
    home = Path(env.get("FEISHU_CODEX_HOME", str(DEFAULT_HOME))).expanduser()
    codex_home = Path(env.get("CODEX_HOME", str(DEFAULT_CODEX_HOME))).expanduser()
    workspaces = parse_workspaces(env.get("FEISHU_CODEX_WORKSPACES", ""))
    default_workspace = env.get("FEISHU_CODEX_DEFAULT_WORKSPACE") or (next(iter(workspaces)) if workspaces else "")
    return Config(
        app_id=empty_to_none(first(env, "FEISHU_APP_ID", "LARK_APP_ID")),
        app_secret=empty_to_none(first(env, "FEISHU_APP_SECRET", "LARK_APP_SECRET")),
        domain=normalize_domain(env.get("FEISHU_DOMAIN") or env.get("LARK_DOMAIN") or "https://open.feishu.cn"),
        allow_all=env_bool(env, "FEISHU_CODEX_ALLOW_ALL", False),
        dry_run=env_bool(env, "FEISHU_CODEX_DRY_RUN", True),
        allowed_senders=frozenset(csv_values(first(env, "FEISHU_CODEX_ALLOWED_SENDERS", "FEISHU_ALLOWED_SENDER_IDS"))),
        allowed_chats=frozenset(csv_values(first(env, "FEISHU_CODEX_ALLOWED_CHATS", "FEISHU_ALLOWED_CHAT_IDS"))),
        trigger_prefix=(env.get("FEISHU_CODEX_TRIGGER_PREFIX") or "codex").strip(),
        workspaces=workspaces,
        default_workspace=default_workspace,
        codex_bin=env.get("FEISHU_CODEX_CODEX_BIN") or "codex",
        codex_sandbox=env.get("FEISHU_CODEX_SANDBOX") or "workspace-write",
        codex_approval=env.get("FEISHU_CODEX_APPROVAL") or "never",
        codex_model=empty_to_none(env.get("FEISHU_CODEX_MODEL")),
        codex_profile=empty_to_none(env.get("FEISHU_CODEX_PROFILE")),
        codex_extra_args=tuple(shlex.split(env.get("FEISHU_CODEX_EXTRA_ARGS", ""))),
        max_running=max(1, int(env.get("FEISHU_CODEX_MAX_RUNNING", "2"))),
        max_prompt_chars=max(100, int(env.get("FEISHU_CODEX_MAX_PROMPT_CHARS", "6000"))),
        reply=env_bool(env, "FEISHU_CODEX_REPLY", True),
        home=home,
        codex_home=codex_home,
        config_path=config_path,
    )


def parse_env_file(path: Path) -> Dict[str, str]:
    values = dict(os.environ)
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        values[key] = value
    return values


def parse_workspaces(value: str) -> Dict[str, Path]:
    workspaces: Dict[str, Path] = {}
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ConfigError("workspace entries must use alias=/path")
        alias, path = item.split("=", 1)
        alias = alias.strip()
        if not alias.replace("-", "").replace("_", "").replace(".", "").isalnum():
            raise ConfigError("workspace alias contains invalid characters: " + alias)
        workspaces[alias] = Path(path.strip()).expanduser()
    return workspaces


def validate_config(config: Config, *, for_daemon: bool = False) -> List[str]:
    errors: List[str] = []
    if not config.workspaces:
        errors.append("FEISHU_CODEX_WORKSPACES must configure at least one workspace")
    if config.default_workspace and config.default_workspace not in config.workspaces:
        errors.append("FEISHU_CODEX_DEFAULT_WORKSPACE is not in FEISHU_CODEX_WORKSPACES")
    for alias, path in config.workspaces.items():
        if not path.is_dir():
            errors.append("workspace does not exist: %s=%s" % (alias, path))
    if not config.dry_run:
        if not config.app_id or not config.app_secret:
            errors.append("real mode requires FEISHU_APP_ID and FEISHU_APP_SECRET")
        if config.allow_all:
            errors.append("real mode refuses FEISHU_CODEX_ALLOW_ALL=1")
        if not config.allowed_senders and not config.allowed_chats:
            errors.append("real mode requires FEISHU_CODEX_ALLOWED_SENDERS or FEISHU_CODEX_ALLOWED_CHATS")
    if for_daemon and (not config.app_id or not config.app_secret):
        errors.append("daemon long connection requires FEISHU_APP_ID and FEISHU_APP_SECRET")
    return errors


def csv_values(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def first(env: Dict[str, str], *keys: str) -> Optional[str]:
    for key in keys:
        if env.get(key):
            return env[key]
    return None


def env_bool(env: Dict[str, str], key: str, default: bool) -> bool:
    if key not in env:
        return default
    return env[key].strip().lower() in {"1", "true", "yes", "on"}


def empty_to_none(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    return value or None


def normalize_domain(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value:
        return "https://open.feishu.cn"
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return "https://" + value


def ensure_dirs(config: Config) -> None:
    config.home.mkdir(parents=True, exist_ok=True)
    config.jobs_dir.mkdir(parents=True, exist_ok=True)
    config.logs_dir.mkdir(parents=True, exist_ok=True)


def format_workspaces(config: Config) -> str:
    if not config.workspaces:
        return "No workspaces configured."
    lines = ["Workspaces:"]
    for alias, path in sorted(config.workspaces.items()):
        default = " (default)" if alias == config.default_workspace else ""
        exists = "ok" if path.is_dir() else "missing"
        lines.append("- %s%s: %s [%s]" % (alias, default, path, exists))
    return "\n".join(lines)


def config_permissions(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    return oct(path.stat().st_mode & 0o777)[2:]
