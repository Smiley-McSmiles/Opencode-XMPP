"""Default cards, in-place card edits, and the card-only chat commands."""

import asyncio
import json
from types import MethodType, SimpleNamespace

from opencode_xmpp import bot as botmod
from opencode_xmpp import cards


def _write(path, data):
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def test_load_default_reads_default_json(tmp_path):
    assert cards.load_default(tmp_path) is None
    _write(tmp_path / "default.json", {
        "id": "gemma-default",
        "name": "Gemma (Default)",
        "base_model_id": "gemma4:latest",
        "params": {"num_ctx": 32768},
    })
    card = cards.load_default(tmp_path)
    assert card is not None
    assert card.id == "gemma-default"
    assert card.base_model == "ollama/gemma4:latest"
    assert card.num_ctx == 32768


def test_set_param_edits_one_card_and_keeps_the_rest(tmp_path):
    path = _write(tmp_path / "cards.json", [
        {"id": "ada", "name": "Ada", "base_model_id": "gemma4:latest",
         "params": {"think": True}, "meta": {"description": "keep me"}},
        {"id": "bo", "name": "Bo", "base_model_id": "qwen3:14b",
         "params": {"think": True}},
    ])
    ada = cards.find_card(cards.load_cards(tmp_path), "ada")
    assert ada is not None

    updated = cards.set_param(ada, "think", False)
    assert updated.think is False

    text = path.read_text(encoding="utf-8")
    data = json.loads(text)
    assert data[0]["params"]["think"] is False
    assert data[0]["meta"]["description"] == "keep me"
    assert data[1]["params"]["think"] is True
    assert path.stat().st_mode & 0o777 == 0o600
    # Written for humans: two-space indentation, one value per line, trailing newline.
    assert text.startswith('[\n  {\n    "id": "ada"')
    assert '\n      "think": false\n' in text
    assert text.endswith("\n")


def test_set_param_refuses_a_card_that_is_gone(tmp_path):
    path = _write(tmp_path / "one.json", {
        "id": "ada", "name": "Ada", "base_model_id": "gemma4:latest"})
    card = cards.load_cards(tmp_path)[0]
    _write(path, {"id": "other", "base_model_id": "gemma4:latest"})
    try:
        cards.set_param(card, "think", False)
    except ValueError as exc:
        assert "no longer contains" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected a ValueError")


def _fake_bridge(tmp_path, cards_dir, config, card=None):
    replies = []

    async def fake_reply(key, item, text):
        replies.append(text)

    fake = SimpleNamespace(
        _chats={},
        _default_card=card,
        cfg=SimpleNamespace(
            admin=SimpleNamespace(model="ollama/gemma4:latest"),
            guest=SimpleNamespace(model=""),
        ),
        _reply=fake_reply,
        _model_allowed=lambda model: True,
        _allowed_provider=lambda: "ollama",
        _opencode_config_path=lambda: config,
        _reload_default_card=lambda: cards.load_default(cards_dir),
    )
    for name in ("_active_card", "_active_model", "_select_card"):
        setattr(fake, name, MethodType(getattr(botmod.Bridge, name), fake))
    # A staticmethod: call it with (path, card), never with self.
    fake._apply_card_config = botmod.Bridge._apply_card_config
    return fake, replies


def test_help_lists_cards_not_every_model(tmp_path, monkeypatch):
    monkeypatch.setattr(cards, "default_dir", lambda: tmp_path)
    _write(tmp_path / "default.json", {
        "id": "gemma-default", "name": "Gemma (Default)",
        "base_model_id": "gemma4:latest"})
    _write(tmp_path / "ada.json", {
        "id": "ada", "name": "Ada", "base_model_id": "qwen3:14b"})

    fake, replies = _fake_bridge(
        tmp_path, tmp_path, tmp_path / "missing.jsonc", card=cards.load_default(tmp_path))
    asyncio.run(botmod.Bridge._handle_help(fake, "local@example.org", {}))

    text = replies[-1]
    assert "Cards (" in text
    assert "gemma-default — Gemma (Default)  [default]" in text
    assert "ada — Ada" in text
    assert "Models:" not in text
    # The only mention of the model is the current card / its base, never a list.
    assert "  ollama/gemma4:latest\n" not in text


def test_model_accepts_a_card_and_rejects_a_bare_model(tmp_path, monkeypatch):
    monkeypatch.setattr(cards, "default_dir", lambda: tmp_path)
    _write(tmp_path / "ada.json", {
        "id": "ada", "name": "Ada", "base_model_id": "gemma4:latest",
        "params": {"num_ctx": 32768}})
    config = _write(tmp_path / "opencode.jsonc",
                    {"providers": {"ollama": {"models": {}}}})
    fake, replies = _fake_bridge(tmp_path, tmp_path, config)

    asyncio.run(botmod.Bridge._handle_model(fake, "local@example.org", {}, "ada"))
    assert "card Ada" in replies[-1]
    chat = fake._chats["local@example.org"]
    assert chat.card.id == "ada"
    assert chat.model_override == "ollama/gemma4:latest"
    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["providers"]["ollama"]["models"]["gemma4:latest"]["body"]["options"]["num_ctx"] == 32768

    replies.clear()
    asyncio.run(botmod.Bridge._handle_model(
        fake, "local@example.org", {}, "ollama/gemma4:latest"))
    assert "No card matches" in replies[-1]
    assert '"base_model_id"' in replies[-1]


def test_model_default_selects_default_json(tmp_path, monkeypatch):
    monkeypatch.setattr(cards, "default_dir", lambda: tmp_path)
    _write(tmp_path / "default.json", {
        "id": "gemma-default", "name": "Gemma (Default)",
        "base_model_id": "gemma4:latest"})
    fake, replies = _fake_bridge(
        tmp_path, tmp_path, tmp_path / "missing.jsonc", card=cards.load_default(tmp_path))

    asyncio.run(botmod.Bridge._handle_model(fake, "local@example.org", {}, "default"))
    assert fake._default_card.id == "gemma-default"
    assert fake._chats["local@example.org"].card.id == "gemma-default"
    assert "card Gemma (Default)" in replies[-1]


def test_think_commands_are_saved_to_the_active_card(tmp_path):
    path = _write(tmp_path / "ada.json", {
        "id": "ada", "name": "Ada", "base_model_id": "gemma4:latest",
        "params": {"think": True}})
    card = cards.load_cards(tmp_path)[0]
    chat = botmod._Chat(card=card, model_override=card.model)
    fake, replies = _fake_bridge(tmp_path, tmp_path, tmp_path / "missing.jsonc")
    fake._chats["local@example.org"] = chat

    asyncio.run(botmod.Bridge._handle_think(fake, "local@example.org", {}, "off"))
    assert json.loads(path.read_text(encoding="utf-8"))["params"]["think"] is False
    assert chat.card.think is False
    assert "saved to ada.json" in replies[-1]

    asyncio.run(botmod.Bridge._handle_think(fake, "local@example.org", {}, "on"))
    assert json.loads(path.read_text(encoding="utf-8"))["params"]["think"] is True
    assert chat.card.think is True


def test_think_without_a_card_explains_default_json(tmp_path):
    fake, replies = _fake_bridge(tmp_path, tmp_path, tmp_path / "missing.jsonc")
    asyncio.run(botmod.Bridge._handle_think(fake, "local@example.org", {}, "off"))
    assert "No model card is active" in replies[-1]
    assert "default.json" in replies[-1]


def test_open_webui_base_model_export_uses_its_id(tmp_path):
    """An Open WebUI export of a base model has base_model_id: null."""
    export = {
        "version_id": "00000000-0000-0000-0000-000000000001",
        "id": "gemma4:26b",
        "user_id": "00000000-0000-0000-0000-000000000002",
        "base_model_id": None,
        "name": "Gemma 4 26B",
        "params": {
            "top_k": 64,
            "top_p": 0.95,
            "temperature": 1,
            "num_ctx": 65536,
            "system": "you must never be dishonest.",
        },
        "meta": {"description": "Reasoning Model", "profile_image_url": "data:image/png;base64,AAAA"},
        "is_active": True,
    }
    _write(tmp_path / "gemma4.json", [export])

    card = cards.load_cards(tmp_path)[0]
    assert card.id == "gemma4:26b"
    assert card.base_model == "ollama/gemma4:26b"
    assert card.base_id == "gemma4:26b"
    assert card.num_ctx == 65536
    assert card.system == "you must never be dishonest."
    assert card.options["temperature"] == 1.0
    assert card.options["top_k"] == 64

    # The same export can be the default card.
    (tmp_path / "default.json").write_text((tmp_path / "gemma4.json").read_text())
    default = cards.load_default(tmp_path)
    assert default is not None and default.base_model == "ollama/gemma4:26b"

    updated = cards.set_param(card, "think", False)
    assert updated.think is False
    saved = json.loads((tmp_path / "gemma4.json").read_text(encoding="utf-8"))[0]
    assert saved["params"]["think"] is False
    assert saved["params"]["num_ctx"] == 65536
    assert saved["version_id"] == export["version_id"]
    assert saved["meta"]["description"] == "Reasoning Model"
    assert saved["is_active"] is True

