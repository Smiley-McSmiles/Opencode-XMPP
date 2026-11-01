"""Image generation, cloud model cards, and the image chat command."""

import asyncio
import base64
from types import MethodType, SimpleNamespace

from opencode_xmpp import approvals, images
from opencode_xmpp import bot as botmod
from opencode_xmpp import opencode_cli as cli, uploads

PNG = b"\x89PNG\r\n\x1a\n" + b"image" * 8


def test_image_prompt_parsing():
    assert approvals.image_command("image a red fox") == "a red fox"
    assert approvals.image_command("draw a red fox") == "a red fox"
    assert approvals.image_command("image") == ""
    assert approvals.image_command("image quality is bad") == "quality is bad"
    assert approvals.image_command("imagine a fox") is None


def test_openrouter_extracts_an_inline_image(monkeypatch):
    payload = base64.b64encode(PNG).decode()
    monkeypatch.setattr(images, "_post_json", lambda *args, **kwargs: {
        "choices": [{"message": {"images": [
            {"image_url": {"url": f"data:image/png;base64,{payload}"}}
        ]}}]
    })
    assert images.generate("openrouter", "google/gemini-image", "a fox", "key") == PNG


def test_venice_accepts_json_or_binary(monkeypatch):
    payload = base64.b64encode(PNG).decode()
    monkeypatch.setattr(images, "_post_raw", lambda *args, **kwargs: (
        f'{{"images": ["{payload}"]}}'.encode(), "application/json"))
    assert images.generate("venice", "chroma", "a fox", "key") == PNG
    monkeypatch.setattr(images, "_post_raw", lambda *args, **kwargs: (PNG, "image/png"))
    assert images.generate("venice", "chroma", "a fox", "key") == PNG


def test_openrouter_text_without_an_image_is_an_error(monkeypatch):
    monkeypatch.setattr(images, "_post_json", lambda *args, **kwargs: {
        "choices": [{"message": {"content": "I cannot do that"}}]
    })
    try:
        images.generate("openrouter", "m", "p", "key")
    except images.ImageError as exc:
        assert "no image" in exc.user_message.lower()
    else:  # pragma: no cover
        raise AssertionError("expected ImageError")


def _fake(tmp_path):
    replies = []
    attachments = []

    async def reply(key, item, text):
        replies.append(text)

    async def send(key, item, url):
        attachments.append(url)

    cfg = SimpleNamespace(
        images=SimpleNamespace(provider="venice",
                               venice_model="grok-imagine-image",
                               openrouter_model="google/gemini-image",
                               timeout_s=180, safe_mode=True, hide_watermark=False),
        features=SimpleNamespace(uploads=True, max_upload_mb=40),
        admin=SimpleNamespace(model="venice/deepseek", agent="xmpp-admin",
                              auto_approve=True, timeout_s=600),
        guest=SimpleNamespace(model="", agent="xmpp-guest",
                              auto_approve=True, timeout_s=180),
        opencode=SimpleNamespace(binary="opencode", workspace="~", env=()),
    )
    fake = SimpleNamespace(
        cfg=cfg, _spool=tmp_path, _upload_host="upload.example.org",
        plugin={"xep_0363": object()}, _image_keys={},
        _reply=reply, _send_attachment=send,
    )
    fake._handle_image = MethodType(botmod.Bridge._handle_image, fake)
    return fake, replies, attachments


def test_image_command_generates_and_attaches(monkeypatch, tmp_path):
    fake, replies, attachments = _fake(tmp_path)

    async def fake_upload(plugin, path, max_mb):
        assert path.read_bytes() == PNG
        return "aesgcm://upload.example.org/gen.png#00"

    monkeypatch.setattr(cli, "export_credential", lambda settings, provider: "key")
    monkeypatch.setattr(images, "generate", lambda *args, **kwargs: PNG)
    monkeypatch.setattr(uploads, "upload_encrypted", fake_upload)

    asyncio.run(botmod.Bridge._handle_image(fake, "local@example.org", {}, "a fox"))
    assert attachments == ["aesgcm://upload.example.org/gen.png#00"]
    assert fake._image_keys["venice"] == "key"
    assert list(tmp_path.iterdir()) == []


def test_image_command_explains_missing_model(tmp_path):
    fake, replies, _ = _fake(tmp_path)
    fake.cfg.images.venice_model = ""
    asyncio.run(botmod.Bridge._handle_image(fake, "local@example.org", {}, "a fox"))
    assert "venice_model" in replies[-1]


def test_cloud_card_keeps_its_provider_prefix(tmp_path):
    from opencode_xmpp import cards

    import json

    path = tmp_path / "cloud-card.json"
    path.write_text(json.dumps({
        "id": "story-card",
        "name": "Story (Experimental)",
        "base_model_id": "openrouter/minimax/minimax-m3.1-flash",
        "params": {"system": "You are {{char}}."},
    }), encoding="utf-8")
    card = cards.load_cards(tmp_path, providers=("openrouter", "venice"))[0]
    assert card.provider == "openrouter"
    assert card.base_model == "openrouter/minimax/minimax-m3.1-flash"
    assert card.base_id == "minimax/minimax-m3.1-flash"
    assert cards.apply_params({}, card) == []


def test_export_credential_reads_the_key_from_the_store(monkeypatch):
    import json

    class Result:
        returncode = 0
        stdout = json.dumps([{"value": {"type": "key", "key": "secret-key"}}])
        stderr = ""

    monkeypatch.setattr(cli, "find_binary", lambda settings: "/usr/bin/opencode")
    monkeypatch.setattr(cli.subprocess, "run", lambda *args, **kwargs: Result())
    settings = cli.RunSettings(binary="opencode", workspace="~")
    assert cli.export_credential(settings, "venice") == "secret-key"


def test_venice_generation_sends_hide_watermark(monkeypatch):
    seen = {}

    def fake_post(url, body, key, timeout):
        seen.update(body)
        return PNG, "image/png"

    monkeypatch.setattr(images, "_post_raw", fake_post)
    images.generate("venice", "chroma", "a fox", "key", hide_watermark=True)
    assert seen["return_binary"] is True
    assert seen["hide_watermark"] is True


def test_venice_edit_posts_the_source_image(monkeypatch):
    seen = {}

    def fake_post(url, body, key, timeout):
        seen.update({"url": url, "body": body})
        return PNG, "image/png"

    monkeypatch.setattr(images, "_post_raw", fake_post)
    blob = images.edit("venice", "qwen-edit-uncensored", "make it blue", "key",
                       b"source", 10, False, "9:16")
    assert blob == PNG
    assert seen["body"]["image"] == "c291cmNl"
    assert seen["body"]["model"] == "qwen-edit-uncensored"
    assert seen["body"]["aspect_ratio"] == "9:16"
    assert seen["body"]["safe_mode"] is False


def test_image_edit_uses_the_attached_source(monkeypatch, tmp_path):
    fake, replies, attachments = _fake(tmp_path)
    fake.cfg.images.venice_edit_model = "qwen-edit-uncensored"
    fake.cfg.images.aspect_ratio = "9:16"
    source = tmp_path / "src.png"
    source.write_bytes(PNG)
    calls = []

    async def fake_download(url, directory, max_mb):
        calls.append((url, directory))
        return source

    async def fake_upload(plugin, path, max_mb):
        return "aesgcm://upload.example.org/edited.png#00"

    def fake_edit(*args, **kwargs):
        assert args[0] == "venice"
        assert args[4] == PNG
        return PNG

    monkeypatch.setattr(uploads, "download", fake_download)
    monkeypatch.setattr(uploads, "upload_encrypted", fake_upload)
    monkeypatch.setattr(cli, "export_credential", lambda settings, provider: "key")
    monkeypatch.setattr(images, "edit", fake_edit)

    asyncio.run(botmod.Bridge._handle_image(
        fake, "local@example.org", {"body": "image edit make it blue\nhttps://upload.example.org/src.png"},
        "edit make it blue\nhttps://upload.example.org/src.png"))
    assert attachments == ["aesgcm://upload.example.org/edited.png#00"]
    assert calls and calls[0][0] == "https://upload.example.org/src.png"


def test_keep_image_keeps_only_the_latest_five(tmp_path):
    from opencode_xmpp import bot as botmod

    fake = SimpleNamespace(_media=tmp_path / "media")
    fake._keep_image = MethodType(botmod.Bridge._keep_image, fake)
    fake._latest_image = MethodType(botmod.Bridge._latest_image, fake)
    for index in range(7):
        source = tmp_path / f"src-{index}.png"
        source.write_bytes(PNG + bytes([index]))
        kept = fake._keep_image("local@example.org", source)
        assert kept.stat().st_mode & 0o777 == 0o600
    folder = tmp_path / "media" / "local@example.org"
    assert len(list(folder.iterdir())) == 5
    assert fake._latest_image("local@example.org") is not None
    assert fake._latest_image("other@example.org") is None
