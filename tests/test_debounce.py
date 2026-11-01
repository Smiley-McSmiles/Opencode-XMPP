"""A burst of messages becomes one turn, not one answer per message."""

import asyncio
from types import SimpleNamespace

from opencode_xmpp import bot as botmod


def test_burst_is_merged_into_one_turn(monkeypatch):
    chat = botmod._Chat(pending=[{"body": "one"}])
    seen = []

    async def handle(key, chat, item):
        seen.append(item["body"])

    async def sleep(delay):
        # Simulate a second message arriving during the debounce window.
        if not seen and len(chat.pending) == 1:
            chat.pending.append({"body": "two"})

    fake = SimpleNamespace(
        cfg=SimpleNamespace(
            policy=SimpleNamespace(message_debounce_ms=1200),
        ),
        _handle_turn=handle,
        _merge_items=botmod.Bridge._merge_items,
    )
    monkeypatch.setattr(botmod.asyncio, "sleep", sleep)

    asyncio.run(botmod.Bridge._run_chat(fake, "local@example.org", chat))
    assert seen == ["one\n\ntwo"]
