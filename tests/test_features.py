import os
from types import SimpleNamespace

import pytest

from opencode_xmpp import uploads
from opencode_xmpp.bot import Bridge
from opencode_xmpp.config import Rooms, load
from opencode_xmpp.policy import GUEST, OWNER


# -- attachments -------------------------------------------------------------

def test_extract_markers_keeps_existing_files_only(tmp_path):
    real = tmp_path / "log.txt"
    real.write_text("x")
    text = f"Here you go [[attach: {real}]] and also [[attach: /nope/missing.bin]]"
    cleaned, paths = uploads.extract_markers(text)
    assert paths == [real]
    assert "[[attach:" not in cleaned
    assert "Here you go" in cleaned


def test_extract_markers_without_markers_is_identity():
    cleaned, paths = uploads.extract_markers("plain reply")
    assert cleaned == "plain reply"
    assert paths == []


def test_find_links_only_returns_https_on_the_upload_host():
    text = (
        "see https://upload.example.org/abc/file.jpg and http://upload.example.org/insecure "
        "and https://evil.example/file.jpg and https://upload.example.org.evil/file"
    )
    assert uploads.find_links(text, "upload.example.org") == [
        "https://upload.example.org/abc/file.jpg"
    ]


def test_find_links_without_host_is_empty():
    assert uploads.find_links("https://upload.example.org/x", "") == []


def _gcm_pair(plain: bytes, iv: bytes, key: bytes):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    sealed = AESGCM(key).encrypt(iv, plain, None)
    return sealed[:-16], sealed[-16:]


def test_aesgcm_opens_every_layout_clients_send():
    import os
    from opencode_xmpp.uploads import _decrypt_aesgcm
    plain = b"\x89PNG\r\n\x1a\npayload"
    key, iv = os.urandom(32), os.urandom(12)
    body, tag = _gcm_pair(plain, iv, key)
    # XEP-0454: iv|key|tag
    assert _decrypt_aesgcm(body, iv + key + tag) == plain
    # Conversations: iv|key, tag appended to the file
    assert _decrypt_aesgcm(body + tag, iv + key) == plain
    # Ward-style: a few extra bytes before a standard blob (was "key length 66")
    assert _decrypt_aesgcm(body, b"v0.4\x00\x00" + iv + key + tag) == plain
    assert _decrypt_aesgcm(body, iv + key + tag + b"\x00\x00\x00") == plain
    # 16-byte IV
    iv16 = os.urandom(16)
    body16, tag16 = _gcm_pair(plain, iv16, key)
    assert _decrypt_aesgcm(body16 + tag16, iv16 + key) == plain
    with pytest.raises(ValueError):
        _decrypt_aesgcm(body, b"too-short")


def test_safe_name_strips_paths_and_odd_characters():
    assert uploads.safe_name("https://upload.example.org/a/b/report%20final.pdf") == "report_20final.pdf"
    assert uploads.safe_name("https://upload.example.org/") == "attachment"


# -- room addressing / tiering ----------------------------------------------

def _bridge(owners=("owner@example.org",), respond="mention", nick="opencode",
            trust_nicks=False, owner_nicks=("owner",)):
    fake = SimpleNamespace(cfg=SimpleNamespace(
        rooms=Rooms(join=(), nick=nick, respond=respond, owner_nicks=owner_nicks,
                    trust_nicks=trust_nicks),
        policy=SimpleNamespace(owners=owners),
    ))
    return fake


def test_room_addressed_by_prefix_or_mention():
    b = _bridge()
    assert Bridge._room_addressed(b, "opencode: deploy the app") == (True, "deploy the app")
    assert Bridge._room_addressed(b, "Opencode, status?") == (True, "status?")
    assert Bridge._room_addressed(b, "@opencode are you there") == (True, "are you there")
    assert Bridge._room_addressed(b, "unrelated chatter")[0] is False


def test_room_respond_always():
    b = _bridge(respond="always")
    assert Bridge._room_addressed(b, "just chatting") == (True, "just chatting")


def test_room_tier_uses_real_jid_not_nick_by_default():
    b = _bridge()
    assert Bridge._room_tier(b, "owner", "owner@example.org/phone") == OWNER
    # same nick, no real JID -> guest (anonymous room, nick is spoofable)
    assert Bridge._room_tier(b, "owner", "") == GUEST
    assert Bridge._room_tier(b, "stranger", "stranger@example.org") == GUEST


def test_room_tier_trust_nicks_opt_in():
    b = _bridge(trust_nicks=True)
    assert Bridge._room_tier(b, "owner", "") == OWNER


# -- forced commands / interrupt plumbing ------------------------------------

def test_stop_command_is_recognised_and_never_forwarded():
    from opencode_xmpp.bot import _is_stop_command
    assert _is_stop_command("stop")
    assert _is_stop_command("  STOP  ")
    assert not _is_stop_command("stop now")
    assert not _is_stop_command("please stop")
    assert not _is_stop_command(".stop")


def test_restart_command_is_bare_and_exact():
    from opencode_xmpp import approvals
    assert approvals.restart_command("restart")
    assert approvals.restart_command("  RESTART  ")
    assert not approvals.restart_command("restart the app")
    assert not approvals.restart_command("please restart")


def test_repair_command_takes_an_optional_focus():
    from opencode_xmpp import approvals
    assert approvals.repair_command("repair") == ""
    assert approvals.repair_command("repair omemo") == "omemo"
    assert approvals.repair_command("repair: sessions") == "sessions"
    assert approvals.repair_command("please repair") is None


def test_restart_unit_comes_from_config_env_or_jid(monkeypatch):
    from opencode_xmpp.bot import Bridge
    monkeypatch.delenv("OPENCODE_XMPP_INSTANCE", raising=False)
    b = SimpleNamespace(bot_bare="bot@example.org",
                        cfg=SimpleNamespace(policy=SimpleNamespace(restart_unit="")))
    assert Bridge._restart_unit(b) == "opencode-xmpp-bot.service"
    monkeypatch.setenv("OPENCODE_XMPP_INSTANCE", "ollama")
    assert Bridge._restart_unit(b) == "opencode-xmpp-ollama.service"
    b.cfg.policy = SimpleNamespace(restart_unit="custom.service")
    assert Bridge._restart_unit(b) == "custom.service"


def test_clear_command_is_bare_and_exact():
    from opencode_xmpp import approvals
    assert approvals.clear_command("clear")
    assert approvals.clear_command("  CLEAR  ")
    assert not approvals.clear_command("clear the chat")
    assert not approvals.clear_command("please clear")


def test_invite_command_parsing():
    from opencode_xmpp.approvals import invite_command
    assert invite_command("invite user@example.org") == "user@example.org"
    assert invite_command("  INVITE  User@Example.org.  ") == "User@Example.org"
    assert invite_command("invite") == ""
    assert invite_command(".invite a@b.c") is None
    assert invite_command("hello") is None


def test_invite_handler_sends_subscription_and_never_calls_the_model():
    import asyncio
    from types import SimpleNamespace
    from opencode_xmpp import bot as botmod

    sent, subscribed, roster, presence = [], [], [], []

    async def fake_reply(key, item, text):
        sent.append(text)

    fake_self = SimpleNamespace(
        bot_bare="bot@example.org",
        cfg=SimpleNamespace(policy=SimpleNamespace(ignore_jids=("ollama@example.org",))),
        update_roster=lambda jid, **kw: roster.append((jid, kw)),
        send_presence_subscription=lambda **kw: subscribed.append(kw),
        send_presence=lambda **kw: presence.append(kw),
        _reply=fake_reply,
    )
    asyncio.run(botmod.Bridge._handle_invite(
        fake_self, "owner@example.org", {}, "owner@example.org", "User@Example.org"))
    assert subscribed == [{"pto": "user@example.org", "ptype": "subscribe"}]
    assert roster == [("user@example.org", {"name": "user"})]
    assert "Invite sent to user@example.org" in sent[0]

    # refuses the sibling bot and the bot itself, and asks for a target
    for target, needle in (("ollama@example.org", "do-not-contact"),
                           ("bot@example.org", "me"),
                           ("", "Usage")):
        sent.clear(); subscribed.clear()
        asyncio.run(botmod.Bridge._handle_invite(
            fake_self, "owner@example.org", {}, "owner@example.org", target))
        assert subscribed == []
        assert needle in sent[0]


def test_join_command_parsing():
    from opencode_xmpp.approvals import join_command
    assert join_command("join room@conference.example.org") == "room@conference.example.org"
    assert join_command("  JOIN  Room@Conference.Example.org.  ") == "Room@Conference.Example.org"
    assert join_command("join") == ""
    assert join_command(".join room@conference.example.org") is None
    assert join_command("hello") is None


def test_join_handler_joins_the_room_without_a_model_turn():
    import asyncio
    from types import SimpleNamespace
    from opencode_xmpp import bot as botmod

    replies, joined = [], []

    async def fake_reply(key, item, text):
        replies.append(text)

    async def fake_join(room, nick, **kw):
        joined.append((str(room), nick, kw))

    fake_self = SimpleNamespace(
        cfg=SimpleNamespace(rooms=SimpleNamespace(nick="ollama")),
        plugin={"xep_0045": SimpleNamespace(join_muc_wait=fake_join)},
        _reply=fake_reply,
    )
    asyncio.run(botmod.Bridge._handle_join(
        fake_self, "owner@example.org", {}, "owner@example.org",
        "ai-conference@conference.example.org"))
    assert joined == [("ai-conference@conference.example.org", "ollama",
                       {"maxstanzas": 0, "timeout": 15})]
    assert "Joined ai-conference@conference.example.org as ollama" in replies[0]

    for target, needle in (("", "Usage"), ("example.org", "not a room JID")):
        replies.clear(); joined.clear()
        asyncio.run(botmod.Bridge._handle_join(
            fake_self, "owner@example.org", {}, "owner@example.org", target))
        assert joined == []
        assert needle in replies[0]


def test_dispatch_routes_join_to_the_bridge_not_the_model():
    import asyncio
    from types import SimpleNamespace
    from opencode_xmpp import bot as botmod

    calls = []

    async def fake_join(key, item, owner, target):
        calls.append((key, target))

    fake_self = SimpleNamespace(
        cfg=SimpleNamespace(policy=SimpleNamespace(owners=("owner@example.org",), ignore_jids=())),
        _chats={}, _handle_join=fake_join, _handle_invite=None, _handle_approval=None,
    )

    async def go():
        botmod.Bridge._dispatch(fake_self, "owner@example.org",
                                "join room@conference.example.org", encrypted=False)
        await asyncio.sleep(0)

    asyncio.run(go())
    assert calls == [("owner@example.org", "room@conference.example.org")]
    assert fake_self._chats == {}


def test_dispatch_routes_invite_to_the_bridge_not_the_model():
    import asyncio
    from types import SimpleNamespace
    from opencode_xmpp import bot as botmod

    calls = []

    async def fake_invite(key, item, owner, target):
        calls.append((key, target))

    fake_self = SimpleNamespace(
        cfg=SimpleNamespace(policy=SimpleNamespace(owners=("owner@example.org",), ignore_jids=())),
        _chats={},
        _handle_invite=fake_invite,
        _handle_approval=None,
    )

    async def go():
        botmod.Bridge._dispatch(fake_self, "owner@example.org", "invite user@example.org",
                                encrypted=False)
        await asyncio.sleep(0)

    asyncio.run(go())
    assert calls == [("owner@example.org", "user@example.org")]
    assert fake_self._chats == {}   # no per-chat runner, so no model turn



def test_merge_items_folds_interrupting_messages_into_one_turn():
    first = {"body": "write a poem", "encrypted": False, "room": None, "tier": None}
    second = {"body": "actually, write a haiku", "encrypted": True, "room": None, "tier": None}
    merged = Bridge._merge_items([first, second])
    assert merged["body"] == "write a poem\n\nactually, write a haiku"
    assert merged["encrypted"] is True
    assert Bridge._merge_items([first]) is first


# -- config ------------------------------------------------------------------

TOML = """
[account]
jid = "AI@example.org"
password_file = "secrets/account.secret"

[admin]
agent = "xmpp-admin"
timeout_s = 600

[guest]
agent = "xmpp-guest"

[policy]
owners = ["owner@example.org"]
session_id_version = "1"

[features]
uploads = false
upload_service = "upload.example.org"
max_upload_mb = 12

[rooms]
join = ["room@conference.example.org"]
nick = "opencode"
respond = "always"
"""


def test_relative_secret_paths_resolve_next_to_the_config(tmp_path):
    (tmp_path / "secrets").mkdir()
    for name, value in (("account.secret", "pw"),):
        path = tmp_path / "secrets" / name
        path.write_text(value)
        os.chmod(path, 0o600)
    path = tmp_path / "opencode.toml"
    path.write_text(TOML)

    cfg = load(path)
    assert cfg.account.password_file == str(tmp_path / "secrets" / "account.secret")
    assert cfg.admin.agent == "xmpp-admin"
    assert cfg.features.uploads is False
    assert cfg.features.max_upload_mb == 12
    assert cfg.rooms.respond == "always"
    assert cfg.rooms.nick == "opencode"


def test_composing_keepalive_refreshes_until_cancelled():
    """Chat states expire client-side; the keepalive must re-send composing."""
    import asyncio

    from opencode_xmpp.bot import Bridge

    class Stub:
        def __init__(self):
            self.calls = []

        def _chat_state(self, to, state):
            self.calls.append((to, state))

    stub = Stub()

    async def run():
        task = asyncio.create_task(
            Bridge._composing_keepalive(stub, "owner@example.org", interval=0.02))
        await asyncio.sleep(0.07)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert len(stub.calls) >= 2
    assert all(call == ("owner@example.org", "composing") for call in stub.calls)


def test_contact_requests_wait_for_owner_approval(tmp_path):
    from opencode_xmpp.approvals import PendingStore, parse_command

    assert parse_command("approve alice@example.org") == ("approve", "alice@example.org")
    assert parse_command("Deny Bob@Example.org.") == ("deny", "bob@example.org")
    assert parse_command("pending") == ("list", "")
    # a normal sentence never counts as an approval
    assert parse_command("yes please do that") is None
    assert parse_command("approve") == ("approve", "")
    assert parse_command("deny that claim") is None

    store = PendingStore(tmp_path / "pending.json")
    assert store.add("alice@example.org", "alice@example.org/phone") is True
    assert store.add("alice@example.org") is False  # idempotent
    assert len(store) == 1
    assert "alice@example.org" in store.describe()

    # survives a restart
    assert len(PendingStore(tmp_path / "pending.json")) == 1
    assert store.remove("alice@example.org") is True
    assert store.remove("alice@example.org") is False
    assert len(PendingStore(tmp_path / "pending.json")) == 0


