"""Small redaction helpers for local logs and job records."""

from __future__ import annotations

import re
from typing import Optional

SECRET_RE = re.compile(r"(?i)(token|api[_-]?key|secret|password)=([^\s]+)")
BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
JSON_SECRET_RE = re.compile(
    r'(?i)("?(?:token|api[_-]?key|secret|password)"?\s*:\s*")([^"]+)(")'
)
PATH_RE = re.compile(r"(?<![\w.-])/(?:[^\s'\"`]+/?)+" )


def redact(value: str, max_chars: Optional[int] = None) -> str:
    redacted = SECRET_RE.sub(r"\1=<redacted>", value)
    redacted = BEARER_RE.sub("Bearer <redacted>", redacted)
    redacted = JSON_SECRET_RE.sub(r"\1<redacted>\3", redacted)
    redacted = PATH_RE.sub("<path>", redacted)
    if max_chars is not None and len(redacted) > max_chars:
        return redacted[: max_chars - 3] + "..."
    return redacted


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "****"
    return "*" * (len(value) - 4) + value[-4:]
