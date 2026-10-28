"""Deterministic repair output must not leak identifiers or message text."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

from opencode_xmpp import diagnostics


def _cfg(redact_jids=True):
    return SimpleNamespace(
        path=Path("/home/local/.opencode-xmpp/bot/opencode.toml"),
        policy=SimpleNamespace(owners=("local@example.org",)),
        features=SimpleNamespace(spool_dir="/home/local/.opencode-xmpp/bot/spool"),
        opencode=SimpleNamespace(workspace="/home/local"),
        repair=SimpleNamespace(redact_jids=redact_jids),
    )


def _redactor(redact_jids=True):
    return diagnostics.Redactor(
        _cfg(redact_jids), "bot@example.org",
        "/home/local/.opencode-xmpp/bot/omemo-bot.json")


def test_contact_jids_become_stable_aliases():
    red = _redactor()
    text = "from alice@example.org/phone to bob@example.org"
    once = red.text(text)
    twice = red.text(text)
    assert "alice@example.org" not in once
    assert "bob@example.org" not in once
    assert "contact-1" in once and "contact-2" in once
    assert once == twice


def test_own_jid_and_owner_jid_are_named():
    red = _redactor()
    text = red.text("bot@example.org talking to local@example.org")
    assert "bot@example.org" not in text
    assert "local@example.org" not in text
    assert "this-bridge" in text
    assert "owner" in text


def test_paths_urls_ips_and_tokens_are_redacted():
    red = _redactor()
    text = red.text(
        "failed at /home/local/.opencode-xmpp/bot/omemo-bot.json "
        "see https://upload.example/a/b?token=x 10.0.0.5 "
        "token abcdefghijklmnopqrstuvwxyz0123456789")
    assert "/home/local" not in text
    assert "upload.example" not in text
    assert "10.0.0.5" not in text
    assert "abcdefghijklmnopqrstuvwxyz0123456789" not in text
    assert "$STATE" in text


def test_jid_redaction_can_be_disabled():
    red = _redactor(redact_jids=False)
    text = red.text("from alice@example.org/phone")
    assert "alice@example.org" in text
    assert "/home/local" not in red.text("/home/local/file")


def test_unknown_repair_focus_lists_checks_without_running_them():
    bridge = SimpleNamespace(bot_bare="bot@example.org",
                             _omemo_storage_path=lambda: "/tmp/omemo.json")
    report = asyncio.run(diagnostics.run_checks(_cfg(), bridge, "nonsense"))
    assert "service" in report and "omemo" in report
    assert "fail" not in report
