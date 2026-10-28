"""Configuration loading and secret handling.

Settings live in a TOML file; the XMPP password lives in a separate file with
0600 permissions so the TOML can be committed and the secret never is. Model
credentials belong to ``opencode`` itself (``opencode auth login``), not to this
bridge: there are no API keys to copy into profiles.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("~/.opencode-xmpp/opencode.toml")
DEFAULT_CONFIG_NAME = "opencode.toml"


class ConfigError(Exception):
    """Invalid or missing configuration."""


@dataclass(frozen=True)
class Account:
    jid: str
    password_file: str = "~/.opencode-xmpp/account.secret"
    host: str | None = None
    port: int = 5222
    resource: str = "opencode"
    starttls: bool = True


@dataclass(frozen=True)
class Tier:
    """How one trust tier is served by the ``opencode`` CLI."""

    agent: str
    model: str = ""
    timeout_s: float = 300.0
    # --auto approves anything a rule does not explicitly deny. The agent's
    # permission rules still enforce read-only for guests.
    auto_approve: bool = True
    # Tell the model who it is talking to and where (prepended to the message).
    context_prefix: bool = True


@dataclass(frozen=True)
class Opencode:
    """The CLI invocation shared by both tiers."""

    binary: str = "opencode"
    # Every run uses this directory. Sessions are stored per Location, so keep
    # it stable: changing it changes where opencode looks for the session.
    workspace: str = "~"
    # 0 = leave compaction to opencode's automatic compaction. N > 0 asks
    # opencode to compact a conversation after every N answered turns, as a
    # belt-and-braces measure.
    compact_after_turns: int = 0
    # Extra environment for the CLI process (e.g. proxy settings).
    env: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Policy:
    owners: tuple[str, ...] = ()
    # Bare JIDs whose messages are never answered (other bridge bots: a reply
    # would make two bots talk to each other forever).
    ignore_jids: tuple[str, ...] = ()
    max_chunk_chars: int = 3800
    rate_limit_per_minute: int = 8
    quiet_unknown_senders: bool = False
    # What a message does to the answer already in flight: "interrupt" stops it
    # and answers the new text next, "queue" lets it finish and then answers.
    on_new_message: str = "interrupt"
    # Part of the derived session id. Bump it to give every contact a clean
    # conversation without deleting anything.
    session_id_version: str = "1"
    # The systemd unit the owner-only ``restart`` chat command restarts.
    # Empty means derive it from OPENCODE_XMPP_INSTANCE or the JID's local part.
    restart_unit: str = ""


@dataclass(frozen=True)
class Repair:
    """Settings for the deterministic, model-free ``repair`` diagnostics."""

    # False: encrypt the report when the command arrived encrypted, and fall
    # back to plaintext if encryption fails. True: always send it in the clear.
    plaintext: bool = False
    # Replace contact JIDs with stable per-report aliases. Disable only when the
    # owner explicitly wants raw JIDs in the report.
    redact_jids: bool = True
    # How far back the logs check reads journald.
    log_minutes: int = 120


@dataclass(frozen=True)
class Features:
    omemo: bool = True
    # "auto" encrypts to whatever versions the contact's devices speak; "2" and
    # "legacy" refuse to send when the contact has no device for that version.
    omemo_mode: str = "auto"
    auto_accept_subscriptions: bool = False  # False: ask the owners before adding a contact
    uploads: bool = True
    max_upload_mb: int = 40
    # Attach downloaded files to the opencode run (--file). Images reach a vision
    # model this way; any other file the agent can read from the spool path.
    inline_images: bool = True
    max_inline_image_mb: int = 8
    # Forward a one-line "using <tool>" notice while the agent works. The CLI
    # only emits completed events, so this replaces the interim-commentary stream.
    interim_messages: bool = False
    max_interim_messages: int = 3
    spool_dir: str = "~/.opencode-xmpp/spool"
    omemo_storage: str = "~/.opencode-xmpp/omemo.json"
    upload_service: str = ""


@dataclass(frozen=True)
class Rooms:
    join: tuple[str, ...] = ()
    nick: str = "opencode"
    respond: str = "mention"          # "mention" or "always"
    owner_nicks: tuple[str, ...] = ()
    trust_nicks: bool = False          # nick-only owner matching; spoofable in anonymous rooms
    encrypt: bool = False              # OMEMO 2 for room messages (needs member real JIDs)
    # When encryption to a room cannot be completed, send the answer in the clear
    # (marked "[unencrypted]") instead of staying silent. Off by default.
    plaintext_fallback: bool = False
    # Join a room when an owner invites the bot to it; strangers' invites are
    # forwarded to the owners as a question instead of being accepted.
    accept_invites: bool = True


@dataclass(frozen=True)
class Config:
    account: Account
    admin: Tier
    guest: Tier
    policy: Policy
    opencode: Opencode = field(default_factory=Opencode)
    features: Features = field(default_factory=Features)
    rooms: Rooms = field(default_factory=Rooms)
    repair: Repair = field(default_factory=Repair)
    path: Path = field(default=DEFAULT_CONFIG_PATH)


def _expand(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value)))


def read_secret(path: str | Path) -> str:
    """Read a secret file, trimmed. Refuses group/world-readable files."""
    p = _expand(str(path))
    try:
        st = p.stat()
    except OSError as exc:
        raise ConfigError(f"missing secret file {p}: {exc}") from exc
    if st.st_mode & 0o077:
        raise ConfigError(f"secret file {p} is accessible by other users (chmod 600)")
    try:
        value = p.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError(f"cannot read secret file {p}: {exc}") from exc
    if not value:
        raise ConfigError(f"secret file {p} is empty")
    return value


def _tier(raw: dict, default_agent: str, default_timeout: float) -> Tier:
    if not isinstance(raw, dict):
        raw = {}
    agent = str(raw.get("agent", default_agent)).strip()
    if not agent:
        raise ConfigError("each tier needs a non-empty agent id")
    return Tier(
        agent=agent,
        model=str(raw.get("model", "")).strip(),
        timeout_s=float(raw.get("timeout_s", default_timeout)),
        auto_approve=bool(raw.get("auto_approve", True)),
        context_prefix=bool(raw.get("context_prefix", True)),
    )


def _string_pairs(raw: object, label: str) -> tuple[tuple[str, str], ...]:
    """Accept ``[opencode.env] KEY = "value"`` as a flat table of strings."""
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise ConfigError(f"{label} must be a table of string values")
    pairs: list[tuple[str, str]] = []
    for key, value in raw.items():
        if not isinstance(value, (str, int, float, bool)):
            raise ConfigError(f"{label}.{key} must be a string")
        pairs.append((str(key), str(value)))
    return tuple(pairs)


def load(path: str | Path = DEFAULT_CONFIG_PATH) -> Config:
    p = _expand(str(path))
    base = p.parent
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {p}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{p} is not valid TOML: {exc}") from exc

    def resolve(value: str) -> str:
        """Relative paths are relative to the config file, not the CWD."""
        candidate = Path(os.path.expanduser(str(value)))
        return str(candidate if candidate.is_absolute() else (base / candidate))

    account_raw = raw.get("account") or {}
    jid = str(account_raw.get("jid", "")).strip()
    if "@" not in jid:
        raise ConfigError("account.jid must be a bare JID like opencode@example.org")
    account = Account(
        jid=jid,
        password_file=resolve(str(account_raw.get("password_file", Account.password_file))),
        host=account_raw.get("host") or None,
        port=int(account_raw.get("port", 5222)),
        resource=str(account_raw.get("resource", "opencode")),
        starttls=bool(account_raw.get("starttls", True)),
    )

    admin = _tier(raw.get("admin") or {}, "xmpp-admin", 600.0)
    guest = _tier(raw.get("guest") or {}, "xmpp-guest", 180.0)

    policy_raw = raw.get("policy") or {}
    owners = tuple(str(o).strip() for o in (policy_raw.get("owners") or ()) if str(o).strip())
    ignore_jids = tuple(str(o).strip() for o in (policy_raw.get("ignore_jids") or ()) if str(o).strip())
    policy = Policy(
        owners=owners,
        ignore_jids=ignore_jids,
        max_chunk_chars=int(policy_raw.get("max_chunk_chars", 3800)),
        rate_limit_per_minute=int(policy_raw.get("rate_limit_per_minute", 8)),
        quiet_unknown_senders=bool(policy_raw.get("quiet_unknown_senders", False)),
        on_new_message=str(policy_raw.get("on_new_message", "interrupt")).lower(),
        session_id_version=str(policy_raw.get("session_id_version", "1")).strip() or "1",
        restart_unit=str(policy_raw.get("restart_unit", "")).strip(),
    )

    oc_raw = raw.get("opencode") or {}
    opencode = Opencode(
        binary=str(oc_raw.get("binary", "opencode")).strip() or "opencode",
        workspace=str(oc_raw.get("workspace", "~")),
        compact_after_turns=max(0, int(oc_raw.get("compact_after_turns", 0))),
        env=_string_pairs(oc_raw.get("env"), "opencode.env"),
    )

    features_raw = raw.get("features") or {}
    features = Features(
        omemo=bool(features_raw.get("omemo", True)),
        omemo_mode=str(features_raw.get("omemo_mode", "auto")).strip().lower(),
        auto_accept_subscriptions=bool(features_raw.get("auto_accept_subscriptions", False)),
        uploads=bool(features_raw.get("uploads", True)),
        max_upload_mb=int(features_raw.get("max_upload_mb", 40)),
        inline_images=bool(features_raw.get("inline_images", True)),
        max_inline_image_mb=int(features_raw.get("max_inline_image_mb", 8)),
        interim_messages=bool(features_raw.get("interim_messages", False)),
        max_interim_messages=max(0, int(features_raw.get("max_interim_messages", 3))),
        spool_dir=resolve(str(features_raw.get("spool_dir", Features.spool_dir))),
        omemo_storage=resolve(str(features_raw.get("omemo_storage", Features.omemo_storage))),
        upload_service=str(features_raw.get("upload_service", "")),
    )

    rooms_raw = raw.get("rooms") or {}
    rooms = Rooms(
        join=tuple(str(r).strip() for r in (rooms_raw.get("join") or ()) if str(r).strip()),
        nick=str(rooms_raw.get("nick", "opencode")),
        respond=str(rooms_raw.get("respond", "mention")).lower(),
        owner_nicks=tuple(str(n).strip() for n in (rooms_raw.get("owner_nicks") or ()) if str(n).strip()),
        trust_nicks=bool(rooms_raw.get("trust_nicks", False)),
        accept_invites=bool(rooms_raw.get("accept_invites", True)),
        encrypt=bool(rooms_raw.get("encrypt", False)),
        plaintext_fallback=bool(rooms_raw.get("plaintext_fallback", False)),
    )
    if rooms.respond not in ("mention", "always"):
        raise ConfigError("rooms.respond must be 'mention' or 'always'")

    repair_raw = raw.get("repair") or {}
    repair = Repair(
        plaintext=bool(repair_raw.get("plaintext", Repair.plaintext)),
        redact_jids=bool(repair_raw.get("redact_jids", Repair.redact_jids)),
        log_minutes=max(1, int(repair_raw.get("log_minutes", Repair.log_minutes))),
    )

    if policy.max_chunk_chars < 500:
        raise ConfigError("policy.max_chunk_chars must be at least 500")
    if policy.on_new_message not in ("interrupt", "queue"):
        raise ConfigError("policy.on_new_message must be 'interrupt' or 'queue'")
    if features.omemo_mode not in ("auto", "2", "legacy"):
        raise ConfigError("features.omemo_mode must be 'auto', '2' or 'legacy'")
    if features.max_upload_mb < 1:
        raise ConfigError("features.max_upload_mb must be positive")
    if features.max_inline_image_mb < 1:
        raise ConfigError("features.max_inline_image_mb must be positive")

    return Config(account=account, admin=admin, guest=guest, policy=policy,
                  opencode=opencode, features=features, rooms=rooms, repair=repair,
                  path=p)
