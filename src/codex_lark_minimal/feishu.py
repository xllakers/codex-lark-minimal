"""Feishu/Lark long-connection daemon."""

from __future__ import annotations

import asyncio
from typing import Any

from codex_lark_minimal.bridge import BridgeController, EventMeta
from codex_lark_minimal.config import Config
from codex_lark_minimal.redaction import redact


async def run_daemon(config: Config) -> None:
    try:
        from lark_oapi.channel import FeishuChannel
    except ImportError as exc:
        raise RuntimeError("Missing lark-oapi. Run ./install.sh or pip install -e .") from exc

    if not config.app_id or not config.app_secret:
        raise RuntimeError("FEISHU_APP_ID and FEISHU_APP_SECRET are required for daemon mode")

    channel = FeishuChannel(
        app_id=config.app_id,
        app_secret=config.app_secret,
        domain=config.domain,
    )
    controller = BridgeController(config)

    async def on_message(msg: Any) -> None:
        meta = event_meta(msg)
        text = str(get_nested(msg, "content_text") or "")
        if not text:
            return
        try:
            reply = controller.handle_text(text, meta)
        except Exception as exc:  # Keep the transport alive and report a redacted failure.
            reply = "Bridge error: %s" % redact(str(exc), max_chars=500)
        if reply and config.reply:
            await send_text(channel, meta.chat_id, reply)

    async def on_error(err: Any) -> None:
        print("Feishu channel error: %s" % redact(str(err), max_chars=800), flush=True)

    channel.on("message", on_message)
    channel.on("error", on_error)
    print("codex-lark-minimal long connection starting", flush=True)
    await channel.connect()


def run_daemon_sync(config: Config) -> None:
    asyncio.run(run_daemon(config))


async def send_text(channel: Any, chat_id: str, text: str) -> None:
    if not chat_id:
        return
    chunks = split_text(text, limit=3500)
    for chunk in chunks:
        await channel.send(chat_id, {"text": chunk})


def split_text(text: str, limit: int) -> list:
    if len(text) <= limit:
        return [text]
    chunks = []
    current = text
    while current:
        chunks.append(current[:limit])
        current = current[limit:]
    return chunks


def event_meta(msg: Any) -> EventMeta:
    chat_id = str(
        get_nested(msg, "chat_id")
        or get_nested(msg, "conversation.chat_id")
        or ""
    )
    return EventMeta(
        event_id=str(get_nested(msg, "event_id") or get_nested(msg, "raw.header.event_id") or ""),
        message_id=str(get_nested(msg, "message_id") or get_nested(msg, "id") or ""),
        chat_id=chat_id,
        sender_id=str(get_nested(msg, "sender_id") or get_nested(msg, "sender.open_id") or ""),
    )


def get_nested(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if current is None:
            return None
        if isinstance(current, dict):
            current = current.get(part)
        else:
            current = getattr(current, part, None)
    return current
