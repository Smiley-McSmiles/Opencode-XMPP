"""One session per contact, plus the sessions/session/close commands."""

import asyncio
from types import MethodType, SimpleNamespace

from opencode_xmpp import approvals, opencode_cli as cli
from opencode_xmpp import bot as botmod
from opencode_xmpp.policy import session_id_for


def _fake_bridge(chat, found=None):
    async def reply(key, item, text):
        replies.append(text)

    replies = []
    fake = SimpleNamespace(
        cfg=SimpleNamespace(
            account=SimpleNamespace(jid="bot@example.org"),
            policy=SimpleNamespace(owners=("local@example.org",),
                                   session_id_version="2"),
            opencode=SimpleNamespace(binary="opencode", workspace="~", env=()),
            admin=SimpleNamespace(model="ollama/gemma4:26b", agent="xmpp-admin",
                                  auto_approve=True, timeout_s=600),
            guest=SimpleNamespace(model="", agent="xmpp-guest",
                                  auto_approve=True, timeout_s=180),
        ),
        _chats={chat_key: chat} if chat else {},
        _reply=reply,
        _session_id=lambda key, chat, tier: chat.session_pin or "ses_derived",
        _existing_sessions=lambda key, chat, tier: list(found or []),
        _resolve_session=botmod.Bridge._resolve_session,
        _describe_session=botmod.Bridge._describe_session,
    )
    return fake, replies


chat_key = "local@example.org"


def _entry(sid, title="Chat", updated=1_700_000_000_000):
    return {"id": sid, "title": title, "updated": updated}


def test_session_commands_are_strict():
    assert approvals.sessions_command("sessions")
    assert approvals.sessions_command("sessions:") is True
    assert approvals.sessions_command("sessions list") is False

    assert approvals.session_command("session 2") == "2"
    assert approvals.session_command("session ses_abc") == "ses_abc"
    assert approvals.session_command("session") == ""
    assert approvals.session_command("session tonight") is None

    assert approvals.close_command("close 1") == "1"
    assert approvals.close_command("close ses_abc.") == "ses_abc"
    assert approvals.close_command("close the door") is None
    assert approvals.close_command("close") == ""


def test_one_session_per_contact_regardless_of_model_or_card():
    fake = SimpleNamespace(
        cfg=SimpleNamespace(
            account=SimpleNamespace(jid="bot@example.org"),
            policy=SimpleNamespace(session_id_version="2"),
        )
    )
    first = botmod._Chat(card=SimpleNamespace(id="one"), model_override="ollama/a")
    second = botmod._Chat(card=SimpleNamespace(id="two"), model_override="ollama/b")
    expected = session_id_for("bot@example.org", "local@example.org", "2")

    assert botmod.Bridge._session_id(fake, "local@example.org", first, "owner") == expected
    assert botmod.Bridge._session_id(fake, "local@example.org", second, "owner") == expected

    first.session_pin = "ses_chosen"
    assert botmod.Bridge._session_id(fake, "local@example.org", first, "owner") == "ses_chosen"


def test_candidates_include_the_old_model_and_card_scopes(monkeypatch):
    fake = SimpleNamespace(
        cfg=SimpleNamespace(
            account=SimpleNamespace(jid="bot@example.org"),
            policy=SimpleNamespace(session_id_version="2"),
            opencode=SimpleNamespace(binary="opencode", workspace="~", env=()),
            admin=SimpleNamespace(model="ollama/a", agent="xmpp-admin",
                                  auto_approve=True, timeout_s=600),
            guest=SimpleNamespace(model="", agent="xmpp-guest",
                                  auto_approve=True, timeout_s=180),
        ),
        _all_cards=lambda: [SimpleNamespace(id="story-card", model="ollama/a")],
    )
    for name in ("_session_versions", "_session_candidates", "_scoped_session_id"):
        setattr(fake, name, MethodType(getattr(botmod.Bridge, name), fake))
    monkeypatch.setattr(cli, "list_models", lambda settings: [])

    candidates = fake._session_candidates("local@example.org", botmod._Chat(), "owner")
    legacy = session_id_for("bot@example.org", "local@example.org", "2",
                            "model:ollama/a|card:story-card")
    assert legacy in candidates
    assert session_id_for("bot@example.org", "local@example.org", "2") in candidates
    assert session_id_for("bot@example.org", "local@example.org", "1") in candidates


def test_sessions_lists_numbered_entries_and_marks_the_active_one():
    found = [_entry("ses_second", "Second"), _entry("ses_first", "First")]
    chat = botmod._Chat(session_pin="ses_second")
    fake, replies = _fake_bridge(chat, found)

    asyncio.run(botmod.Bridge._handle_sessions(fake, chat_key, {}))
    text = replies[-1]
    assert "1. ses_second" in text and "[active]" in text
    assert "2. ses_first" in text
    assert "session <number|id>" in text


def test_session_switches_by_number():
    found = [_entry("ses_second"), _entry("ses_first")]
    chat = botmod._Chat()
    fake, replies = _fake_bridge(chat, found)

    asyncio.run(botmod.Bridge._handle_session(fake, chat_key, {}, "2"))
    assert chat.session_pin == "ses_first"
    assert "continues session ses_first" in replies[-1]

    asyncio.run(botmod.Bridge._handle_session(fake, chat_key, {}, "9"))
    assert "No session '9'" in replies[-1]


def test_close_deletes_one_session_and_clears_a_pin(monkeypatch):
    found = [_entry("ses_active"), _entry("ses_other")]
    chat = botmod._Chat(session_pin="ses_active")
    fake, replies = _fake_bridge(chat, found)
    deleted = []

    def fake_delete(settings, sid):
        deleted.append(sid)
        return True, ""

    monkeypatch.setattr(cli, "delete_session", fake_delete)

    asyncio.run(botmod.Bridge._handle_close(fake, chat_key, {}, "1"))
    assert deleted == ["ses_active"]
    assert chat.session_pin == ""
    assert "Closed session ses_active" in replies[-1]
    assert "fresh session" in replies[-1]

    asyncio.run(botmod.Bridge._handle_close(fake, chat_key, {}, "2"))
    assert deleted[-1] == "ses_other"
    assert "keeps its session" in replies[-1]


def test_list_sessions_parses_the_json_output(monkeypatch):
    monkeypatch.setattr(cli, "find_binary", lambda settings: "/usr/bin/opencode")

    class Result:
        returncode = 0
        stdout = '[{"id": "ses_a", "title": "A"}, {"bogus": true}]'
        stderr = ""

    monkeypatch.setattr(cli.subprocess, "run", lambda *args, **kwargs: Result())
    settings = cli.RunSettings(binary="opencode", workspace="~")
    assert cli.list_sessions(settings) == [{"id": "ses_a", "title": "A"}]


def test_first_turn_adopts_the_newest_legacy_session(monkeypatch):
    fake = SimpleNamespace(
        cfg=SimpleNamespace(
            account=SimpleNamespace(jid="bot@example.org"),
            policy=SimpleNamespace(session_id_version="2"),
        ),
        _existing_sessions=lambda key, chat, tier: [_entry("ses_legacy")],
    )
    fake._adopt_legacy_session = MethodType(botmod.Bridge._adopt_legacy_session, fake)
    chat = botmod._Chat()
    asyncio.run(fake._adopt_legacy_session("local@example.org", chat, "owner"))
    assert chat.session_pin == "ses_legacy"
    assert "ses_legacy" in chat.session_ids


def test_first_turn_keeps_the_derived_session_when_it_exists():
    derived = session_id_for("bot@example.org", "local@example.org", "2")
    fake = SimpleNamespace(
        cfg=SimpleNamespace(
            account=SimpleNamespace(jid="bot@example.org"),
            policy=SimpleNamespace(session_id_version="2"),
        ),
        _existing_sessions=lambda key, chat, tier: [_entry(derived)],
    )
    fake._adopt_legacy_session = MethodType(botmod.Bridge._adopt_legacy_session, fake)
    chat = botmod._Chat()
    asyncio.run(fake._adopt_legacy_session("local@example.org", chat, "owner"))
    assert chat.session_pin == ""
