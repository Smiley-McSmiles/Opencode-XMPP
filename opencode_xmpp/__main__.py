"""Entry point: ``opencode-xmpp {run,check,init,unit,react,handoff}``."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import secrets
import shutil
import signal
import socket
import sys
from pathlib import Path

from . import __version__, approvals, policy, reactions
from . import opencode_cli as cli
from .bot import Bridge, _settings_for
from .config import ConfigError, DEFAULT_CONFIG_NAME, load, read_secret

log = logging.getLogger("opencode_xmpp")

CONFIG_TEMPLATE = """\
# ─────────────────────────────────────────────────────────────────────────────
#  opencode-xmpp — the XMPP bridge for the opencode CLI
#
#  Bot account:        {jid}
#  Password:           secrets/account.secret  (0600, not in git)
#  Admin users:        [policy] owners — currently {owners}
#                      → full tools: terminal, files, projects, web
#  Everyone else:      read-only tools (read, glob, grep, web)
# ─────────────────────────────────────────────────────────────────────────────

[account]
jid = "{jid}"
{host_line}port = {port}
resource = "opencode"
starttls = true
password_file = "secrets/account.secret"

# ── Admin tier: full toolset, one opencode agent ────────────────────────────
# The agent id must exist as a Markdown agent in opencode's agent directory
# (~/.config/opencode/agents/xmpp-admin.md by default).
[admin]
agent = "{admin_agent}"
model = "{model}"
timeout_s = 600
auto_approve = true

# ── Guest tier: read-only tools; no shell, edits or subagents ────────────────
[guest]
agent = "{guest_agent}"
model = "{model}"
timeout_s = 180
auto_approve = true

[opencode]
binary = "{binary}"
workspace = "{workspace}"
# 0 = rely on opencode's automatic compaction. N > 0 also asks opencode to
# compact after every N answered turns as a safety net.
compact_after_turns = 0

[policy]
# ── WHO GETS TOOL ACCESS ────────────────────────────────────────────────────
# Add your own JIDs here (comma separated). Matched case-insensitively and
# ignoring the /resource part. The admin agent runs with full tool access, so
# treat this list as shell access to the host.
owners = [{owners_toml}]
ignore_jids = []

max_chunk_chars = 3800
rate_limit_per_minute = 8
quiet_unknown_senders = false
on_new_message = "interrupt"
# Bump this to give every contact a clean conversation (no session deletion).
session_id_version = "1"
# Unit the owner-only "restart" chat command restarts. Empty derives it from
# OPENCODE_XMPP_INSTANCE or the JID's local part, e.g. opencode-xmpp-bot.service.
restart_unit = ""

[repair]
# Owner-only "repair" chat command. Deterministic diagnostics only: no model,
# no agent, no token cost.
# false: encrypt the report if the command was encrypted, and fall back to
# clearly marked plaintext if encryption fails. true: always send it plaintext.
plaintext = false
# Replace contact JIDs with per-report aliases. Disable only for a deliberate
# owner-only raw report.
redact_jids = true
# Window for the logs check.
log_minutes = 120

[features]
# OMEMO 0.3 (legacy) and OMEMO 2 / 0.8 are handled by the same plugin.
omemo = true
omemo_mode = "auto"
auto_accept_subscriptions = false
uploads = {uploads}
upload_service = "{upload_service}"
max_upload_mb = 40
inline_images = true
max_inline_image_mb = 8
# The CLI only reports completed events, so there is no true mid-turn text to
# forward. Set this to true for one-line "using <tool>" notices instead.
interim_messages = false
max_interim_messages = 3
spool_dir = "{spool_dir}"
omemo_storage = "{omemo_storage}"

[rooms]
join = [{rooms_toml}]
nick = "{nick}"
respond = "mention"
owner_nicks = []
trust_nicks = false
accept_invites = true
encrypt = false
plaintext_fallback = false
"""


def cmd_check(config_path: str) -> int:
    cfg = load(config_path)
    problems = 0
    print(f"config      {cfg.path}")
    print(f"account     {cfg.account.jid} via {cfg.account.host or 'DNS SRV'}:{cfg.account.port}")
    print(f"owners      {', '.join(cfg.policy.owners) or '(none - every sender is a guest)'}")
    if cfg.policy.ignore_jids:
        print(f"ignored     {', '.join(cfg.policy.ignore_jids)}")

    try:
        read_secret(cfg.account.password_file)
        print("password    ok (0600)")
    except ConfigError as exc:
        print(f"password    FAIL {exc}")
        problems += 1

    settings = _settings_for(cfg, cfg.admin)
    binary = cli.find_binary(settings)
    if binary:
        print(f"opencode    ok ({binary}, {cli.version(settings) or 'version unknown'})")
    else:
        print(f"opencode    FAIL not found ({cfg.opencode.binary!r}); set [opencode] binary")
        problems += 1

    workspace = Path(os.path.expanduser(cfg.opencode.workspace or "~"))
    if workspace.is_dir():
        writable = "writable" if os.access(workspace, os.W_OK) else "NOT writable"
        print(f"workspace   ok ({workspace}, {writable})")
        if not os.access(workspace, os.W_OK):
            problems += 1
    else:
        print(f"workspace   FAIL {workspace} is not a directory")
        problems += 1

    for label, tier in (("admin", cfg.admin), ("guest", cfg.guest)):
        found = _find_agent(tier.agent)
        print(f"{label:11} agent {tier.agent!r} "
              f"{'found at ' + str(found) if found else 'NOT FOUND in ~/.config/opencode/agents'}")
        if found is None:
            problems += 1

    print(f"repair      deterministic (redact_jids={cfg.repair.redact_jids}, "
          f"plaintext={cfg.repair.plaintext}, log_minutes={cfg.repair.log_minutes})")
    print(f"            restart unit {cfg.policy.restart_unit or '(derived from the JID/instance)'}")

    host = cfg.account.host or cfg.account.jid.rsplit("@", 1)[-1]
    try:
        with socket.create_connection((host, cfg.account.port), timeout=8):
            print(f"xmpp        ok ({host}:{cfg.account.port} reachable)")
    except OSError as exc:
        print(f"xmpp        FAIL {host}:{cfg.account.port}: {exc}")
        problems += 1

    print(f"omemo       {cfg.features.omemo}   uploads {cfg.features.uploads}"
          f"   rooms {', '.join(cfg.rooms.join) or '(none)'}")
    print(f"compaction  {'opencode automatic' if not cfg.opencode.compact_after_turns else str(cfg.opencode.compact_after_turns) + ' turns'}")
    return 0 if problems == 0 else 1


def _agent_dirs() -> list[Path]:
    """Where opencode looks for Markdown agents, most specific first."""
    dirs: list[Path] = []
    project = Path.cwd() / ".opencode" / "agents"
    dirs.append(project)
    dirs.append(Path(os.path.expanduser("~/.config/opencode/agents")))
    return dirs


def _find_agent(agent_id: str) -> Path | None:
    for base in _agent_dirs():
        candidate = base / f"{agent_id}.md"
        if candidate.is_file():
            return candidate
    return None


def _install_agents(target: Path, force: bool) -> list[Path]:
    """Copy the shipped Markdown agents into opencode's agent directory."""
    source = Path(__file__).resolve().parent.parent / "agents"
    written: list[Path] = []
    target.mkdir(parents=True, exist_ok=True)
    for path in sorted(source.glob("*.md")):
        destination = target / path.name
        if destination.exists() and not force:
            continue
        shutil.copyfile(path, destination)
        written.append(destination)
    return written


def _write_secret(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(value.strip() + "\n")
    os.chmod(path, 0o600)


def cmd_init(args: argparse.Namespace) -> int:
    target = Path(os.path.expanduser(args.dir)).resolve()
    config_path = target / DEFAULT_CONFIG_NAME
    if config_path.exists() and not args.force:
        print(f"{config_path} exists; pass --force to overwrite", file=sys.stderr)
        return 2

    jid = args.jid.strip()
    if "@" not in jid:
        print("--jid must look like bot@example.org", file=sys.stderr)
        return 2
    owners = [j.strip().lower() for j in (args.owner or []) if j.strip()]
    host = args.host or None

    password = os.environ.get("OPENCODE_XMPP_PASSWORD", "")
    if not password:
        if sys.stdin.isatty():
            password = getpass.getpass(f"XMPP password for {jid}: ")
        if not password:
            print("set OPENCODE_XMPP_PASSWORD (or run interactively) to store the account password",
                  file=sys.stderr)
            return 2

    target.mkdir(parents=True, exist_ok=True)
    (target / "secrets").mkdir(mode=0o700, exist_ok=True)
    _write_secret(target / "secrets" / "account.secret", password)

    spool = target / "spool"
    omemo = target / "omemo.json"
    config_path.write_text(CONFIG_TEMPLATE.format(
        jid=jid,
        host_line=f'host = "{host}"\n' if host else "",
        port=args.port,
        owners=", ".join(owners) or "(none)",
        owners_toml=", ".join(f'"{o}"' for o in owners),
        admin_agent=args.admin_agent,
        guest_agent=args.guest_agent,
        binary=args.binary,
        workspace=args.workspace,
        model=args.model,
        uploads="true" if args.upload_service else "false",
        upload_service=args.upload_service or "",
        rooms_toml=", ".join(f'"{r}"' for r in (args.room or [])),
        nick=args.nick or jid.split("@", 1)[0],
        spool_dir=str(spool),
        omemo_storage=str(omemo),
    ), encoding="utf-8")
    os.chmod(config_path, 0o644)

    agents_dir = Path(os.path.expanduser(args.agents_dir))
    written = _install_agents(agents_dir, args.force)
    print(f"wrote {config_path}")
    print(f"wrote {target / 'secrets'} (0600)")
    if written:
        for path in written:
            print(f"wrote agent {path}")
    else:
        print(f"agents already present in {agents_dir} (pass --force to overwrite)")

    print()
    print("Next:")
    print(f"  opencode auth login                     # if the provider is not configured yet")
    print(f"  opencode-xmpp check --config {config_path}")
    print(f"  opencode-xmpp unit --config {config_path} > ~/.config/systemd/user/opencode-xmpp.service")
    print("  systemctl --user daemon-reload && systemctl --user enable --now opencode-xmpp")
    print()
    print(f"Edit {config_path} to add owners and rooms. The admin agent runs with")
    print("full tool access: [policy] owners is shell access to this host.")
    return 0


def cmd_unit(args: argparse.Namespace) -> int:
    cfg = load(args.config)
    config_path = Path(cfg.path).resolve()
    executable = args.executable or sys.executable
    instance = (args.name or cfg.account.jid.split("@", 1)[0]).strip()
    unit_name = f"opencode-xmpp-{instance}"
    data_dirs = sorted({
        str(Path(cfg.features.omemo_storage).expanduser().parent),
        str(Path(cfg.features.spool_dir).expanduser()),
        str(Path(cfg.opencode.workspace).expanduser()),
        str(Path("~/.local/share/opencode").expanduser()),
        str(Path("~/.cache/opencode").expanduser()),
    })
    binary = cli.find_binary(_settings_for(cfg, cfg.admin)) or cfg.opencode.binary
    binary_dir = str(Path(binary).parent) if os.sep in binary else ""
    home = str(Path.home())
    path_parts = list(dict.fromkeys(
        p for p in (binary_dir, f"{home}/.local/bin", f"{home}/.opencode/bin",
                    "/usr/local/bin", "/usr/bin", "/bin") if p))
    print(f"# save as ~/.config/systemd/user/{unit_name}.service")
    print(f"# then: systemctl --user daemon-reload && systemctl --user enable --now {unit_name}")
    unit = f"""[Unit]
Description=opencode XMPP bridge — {instance} ({cfg.account.jid})
Documentation=https://github.com/Smiley-McSmiles/Opencode-XMPP
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
WorkingDirectory={config_path.parent}
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=HOME=%h
Environment=PATH={':'.join(path_parts)}
Environment=OPENCODE_XMPP_CONFIG={config_path}
Environment=OPENCODE_XMPP_INSTANCE={instance}
ExecStart={executable} -m opencode_xmpp run
Restart=always
RestartSec=5
TimeoutStopSec=20

# The bridge writes its spool, OMEMO store and opencode's own state. The admin
# agent runs with the user's full authority, so this unit deliberately does not
# set ProtectHome: it must be able to edit files in the workspace.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths={' '.join(data_dirs)}
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
RestrictNamespaces=true
LockPersonality=true

[Install]
WantedBy=default.target
"""
    print(unit, end="")
    return 0


async def cmd_run(config_path: str) -> int:
    cfg = load(config_path)
    bridge = Bridge(cfg)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, bridge.disconnect)
        except NotImplementedError:  # pragma: no cover - non-POSIX
            pass

    log.info("opencode-xmpp %s starting as %s", __version__, cfg.account.jid)
    await bridge.run_forever()
    log.info("stopped")
    # A turn may still be blocked in a worker thread waiting on the CLI. Normal
    # interpreter shutdown would wait for that thread and outlive TimeoutStopSec,
    # so leave without joining it: nothing here is persisted in memory.
    logging.shutdown()
    os._exit(0)


def cmd_react(args) -> int:
    """Hand a reaction to the running bridge: it owns the OMEMO sessions."""
    cfg = load(args.config)
    path = reactions.queue_reaction(cfg.features.spool_dir, args.jid, args.message_id, args.emoji)
    print(f"queued reaction {''.join(args.emoji)} to {args.jid} on message {args.message_id}")
    print(f"job written to {path}; the running bridge sends it within a few seconds")
    return 0


def cmd_handoff(args) -> int:
    """Seed a contact's opencode session from a CLI transcript and say so in chat."""
    cfg = load(args.config)
    to = policy.bare_jid(args.to)
    brief = args.brief if args.brief is not None else sys.stdin.read()
    if not brief.strip():
        print("empty handoff brief (pass --brief or pipe it on stdin)", file=sys.stderr)
        return 2
    scope = f"model:{cfg.admin.model}" if cfg.admin.model else ""
    sid = policy.session_id_for(cfg.account.jid, to, cfg.policy.session_id_version, scope)
    if args.dry_run:
        print(f"dry run: would seed session {sid} and queue {len(brief)} chars to {to}")
        return 0
    settings = _settings_for(cfg, cfg.admin)
    preamble = ("[Handoff] The operator is handing you context from their own opencode "
                "session. Treat it as background you now own; acknowledge it in one short "
                "line and do not act on it yet.\n\n")
    ack = ""
    try:
        answer = cli.run(settings, cli.Turn(user=preamble + brief, session_id=sid,
                                           agent=cfg.admin.agent),
                         stop_event=None)
        ack = answer.text.strip()
    except cli.CLIError as exc:
        print(f"could not seed {sid} ({exc.user_message}); sending the brief itself")
    path = reactions.queue_text(cfg.features.spool_dir, to, ack or brief)
    print(f"handoff written to session {sid}")
    print(f"queued {'the acknowledgement' if ack else 'the brief'} for {to} ({path.name})")
    return 0


def _default_config() -> str:
    return os.environ.get(
        "OPENCODE_XMPP_CONFIG",
        DEFAULT_CONFIG_NAME if os.path.exists(DEFAULT_CONFIG_NAME)
        else "~/.opencode-xmpp/opencode.toml",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="opencode-xmpp",
                                     description="XMPP bridge for the opencode CLI")
    parser.add_argument("--config", default=_default_config())
    parser.add_argument("--log-level", default=os.environ.get("OPENCODE_XMPP_LOG_LEVEL", "INFO"))
    parser.add_argument("--version", action="version", version=f"opencode-xmpp {__version__}")
    # --config is also accepted after the subcommand: `opencode-xmpp check --config X`.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS)
    common.add_argument("--log-level", default=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="run the bridge in the foreground", parents=[common])
    sub.add_parser("check", help="validate config, secrets, opencode and XMPP reachability",
                   parents=[common])

    init = sub.add_parser("init", help="create a config, secret and agent files",
                          parents=[common])
    init.add_argument("--dir", default="~/.opencode-xmpp", help="target directory")
    init.add_argument("--jid", required=True, help="bot JID, e.g. opencode@example.org")
    init.add_argument("--owner", action="append", help="admin JID (repeatable)")
    init.add_argument("--host", help="XMPP server host (default: derived from the JID)")
    init.add_argument("--port", type=int, default=5222)
    init.add_argument("--binary", default="opencode", help="path or name of the opencode CLI")
    init.add_argument("--workspace", default="~", help="working directory for opencode runs")
    init.add_argument("--admin-agent", default="xmpp-admin", help="agent id for the admin tier")
    init.add_argument("--guest-agent", default="xmpp-guest", help="agent id for the guest tier")
    init.add_argument("--model", default="", help="optional provider/model[#variant]")
    init.add_argument("--agents-dir", default="~/.config/opencode/agents",
                      help="where to install the Markdown agents")
    init.add_argument("--upload-service", default="", help="XEP-0363 upload host")
    init.add_argument("--room", action="append", help="MUC room to join (repeatable)")
    init.add_argument("--nick", default="", help="MUC nick (default: the JID's local part)")
    init.add_argument("--force", action="store_true", help="overwrite existing files")

    react = sub.add_parser("react", parents=[common],
                           help="queue a message reaction; the running bridge sends it")
    react.add_argument("jid", help="contact or room the reaction is for")
    react.add_argument("message_id", help="stanza id of the message being reacted to")
    react.add_argument("emoji", nargs="+", help="one or more reactions, e.g. 👍 ❤️")

    handoff = sub.add_parser("handoff", parents=[common],
                             help="seed a contact's opencode session from a CLI transcript")
    handoff.add_argument("--to", required=True, help="contact JID to hand the session to")
    handoff.add_argument("--brief", help="handoff text (default: read stdin)")
    handoff.add_argument("--timeout", type=float, default=180.0,
                         help="seconds to wait for the seeding run (default 180)")
    handoff.add_argument("--dry-run", action="store_true",
                         help="seed nothing and queue nothing; report only")

    unit = sub.add_parser("unit", help="print a systemd user unit for this config", parents=[common])
    unit.add_argument("--executable", help="python interpreter (default: the running one)")
    unit.add_argument("--name", help="instance name for the unit (default: the JID's local part)")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("slixmpp").setLevel(
        logging.DEBUG if os.environ.get("OPENCODE_XMPP_XMPP_DEBUG") else logging.WARNING)

    try:
        if args.command == "check":
            return cmd_check(args.config)
        if args.command == "init":
            return cmd_init(args)
        if args.command == "unit":
            return cmd_unit(args)
        if args.command == "react":
            return cmd_react(args)
        if args.command == "handoff":
            return cmd_handoff(args)
        return asyncio.run(cmd_run(args.config))
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
