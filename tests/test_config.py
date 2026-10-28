import os

import pytest

from opencode_xmpp.config import ConfigError, load, read_secret

TOML = """
[account]
jid = "AI@example.org"
password_file = "{tmp}/account.secret"
host = "example.org"
port = 5222

[admin]
agent = "xmpp-admin"
timeout_s = 600

[guest]
agent = "xmpp-guest"
timeout_s = 180

[opencode]
binary = "opencode"
workspace = "~"
compact_after_turns = 3

[policy]
owners = ["owner@example.org"]
max_chunk_chars = 3000
rate_limit_per_minute = 5
session_id_version = "2"
"""


def _write(path, text, mode=0o600):
    path.write_text(text)
    os.chmod(path, mode)


@pytest.fixture()
def cfgfile(tmp_path):
    _write(tmp_path / "account.secret", "hunter2\n")
    path = tmp_path / "opencode.toml"
    _write(path, TOML.format(tmp=tmp_path), 0o644)
    return path


def test_load_reads_account_tiers_and_policy(cfgfile):
    cfg = load(cfgfile)
    assert cfg.account.jid == "AI@example.org"
    assert cfg.account.host == "example.org"
    assert cfg.admin.agent == "xmpp-admin"
    assert cfg.guest.agent == "xmpp-guest"
    assert cfg.admin.timeout_s == 600
    assert cfg.opencode.binary == "opencode"
    assert cfg.opencode.compact_after_turns == 3
    assert cfg.policy.owners == ("owner@example.org",)
    assert cfg.policy.max_chunk_chars == 3000
    assert cfg.policy.session_id_version == "2"
    assert read_secret(cfg.account.password_file) == "hunter2"


def test_world_readable_secret_is_refused(cfgfile):
    os.chmod(cfgfile.parent / "account.secret", 0o644)
    with pytest.raises(ConfigError, match="chmod 600"):
        read_secret(cfgfile.parent / "account.secret")


def test_missing_secret_is_reported(cfgfile):
    with pytest.raises(ConfigError, match="missing secret"):
        read_secret(str(cfgfile.parent / "nope"))


def test_jid_without_domain_is_refused(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text('[account]\njid = "opencode"\n')
    with pytest.raises(ConfigError, match="bare JID"):
        load(path)


def test_ignore_jids_are_loaded(cfgfile):
    text = cfgfile.read_text().replace(
        '[policy]\nowners = ["owner@example.org"]',
        '[policy]\nowners = ["owner@example.org"]\nignore_jids = ["ollama@example.org"]')
    cfgfile.write_text(text)
    assert load(cfgfile).policy.ignore_jids == ("ollama@example.org",)
    assert cfgfile.exists()


def test_omemo_mode_defaults_to_auto(cfgfile):
    assert load(cfgfile).features.omemo_mode == "auto"


def test_repair_defaults_are_model_free(cfgfile):
    cfg = load(cfgfile)
    assert cfg.repair.plaintext is False
    assert cfg.repair.redact_jids is True
    assert cfg.repair.log_minutes == 120
    assert cfg.policy.restart_unit == ""


def test_repair_and_restart_unit_are_loaded(cfgfile):
    text = cfgfile.read_text().replace(
        "[policy]",
        '[repair]\nplaintext = true\nredact_jids = false\nlog_minutes = 90\n\n'
        '[policy]\nrestart_unit = "custom.service"')
    cfgfile.write_text(text)
    cfg = load(cfgfile)
    assert cfg.repair.plaintext is True
    assert cfg.repair.redact_jids is False
    assert cfg.repair.log_minutes == 90
    assert cfg.policy.restart_unit == "custom.service"


def test_omemo_mode_accepts_2_and_legacy(cfgfile):
    base = cfgfile.read_text()
    for mode in ("2", "legacy"):
        cfgfile.write_text(base.replace(
            "[policy]", f'[features]\nomemo_mode = "{mode}"\n\n[policy]'))
        assert load(cfgfile).features.omemo_mode == mode


def test_omemo_mode_rejects_unknown_values(cfgfile):
    text = cfgfile.read_text().replace(
        "[policy]", '[features]\nomemo_mode = "both"\n\n[policy]')
    cfgfile.write_text(text)
    with pytest.raises(ConfigError, match="omemo_mode"):
        load(cfgfile)


def test_tier_defaults_are_used_when_sections_are_missing(tmp_path):
    _write(tmp_path / "account.secret", "hunter2\n")
    path = tmp_path / "minimal.toml"
    path.write_text('[account]\njid = "bot@example.org"\npassword_file = "account.secret"\n')
    cfg = load(path)
    assert cfg.admin.agent == "xmpp-admin"
    assert cfg.guest.agent == "xmpp-guest"
    assert cfg.admin.auto_approve is True
    assert cfg.guest.timeout_s == 180


def test_env_pairs_are_loaded(tmp_path):
    _write(tmp_path / "account.secret", "hunter2\n")
    path = tmp_path / "env.toml"
    path.write_text("""
[account]
jid = "bot@example.org"
password_file = "account.secret"

[opencode.env]
HTTPS_PROXY = "http://127.0.0.1:3128"
""")
    cfg = load(path)
    assert dict(cfg.opencode.env)["HTTPS_PROXY"] == "http://127.0.0.1:3128"
