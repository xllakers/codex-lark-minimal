# Security Policy

This project is a local-first chat-to-Codex bridge that handles app secrets,
shell-side subprocess execution, and untrusted chat input. We take reports
seriously.

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Use [GitHub's private security advisory flow][advisory] for this repository.
That sends the report directly to the maintainer and stays private until a
fix is published. You can also email the maintainer through their GitHub
profile if private advisories are not available to you.

[advisory]: https://github.com/xllakers/codex-lark-minimal/security/advisories/new

Helpful detail to include:

- the version or commit SHA you tested
- a minimal reproduction (chat message, config snippet, command sequence)
- the impact you observed and the impact you believe is possible
- any redacted log excerpts (please scrub real tokens/IDs first)

You'll get an acknowledgement within a few days. This is a personal,
best-effort project — there is no SLA, but reports are taken seriously and
addressed quickly when actionable.

## What's in scope

- Authentication / allowlist bypass (e.g. unauthenticated sender or chat
  reaching `BridgeController.handle_text`)
- Workspace-confinement bypass (e.g. chat input causing Codex to run outside
  a configured workspace alias)
- Secret exposure via persisted state, logs, or chat replies (the bridge
  should never persist raw prompts, raw Codex JSONL, or secrets in clear)
- Subprocess argument injection (e.g. chat content reaching the Codex
  command line unsanitised)
- File handle / state corruption that could cause a denial of service or
  cross-job interference

## What's out of scope

- Compromising the host machine itself (the bridge assumes a trusted local
  host; if attackers already have shell access, they don't need the bridge)
- Vulnerabilities in `lark-oapi`, Codex, or other dependencies — please
  report those to the upstream projects. We will pick up upstream fixes
  when they ship.
- Findings only reproducible with `FEISHU_CODEX_DRY_RUN=1` +
  `FEISHU_CODEX_ALLOW_ALL=1`. That combination is the documented
  discovery-mode default and is not a trust boundary.
- Issues that require the maintainer to ship attacker-controlled config
  (e.g. malicious `FEISHU_CODEX_WORKSPACES` values).

## Supported versions

Only `main` is supported. Older tagged releases get fixes on a best-effort
basis. Pin a specific commit if you need stability.
