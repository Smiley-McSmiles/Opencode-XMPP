"""Deterministic diagnostics for the owner-only ``repair`` command.

There is deliberately no model here. Every check is fixed code that reads a
bounded, structured source and returns a short line. The report is redacted
before it leaves the process: contact JIDs become per-report aliases, home
paths become ``~``/``$CONFIG``/``$SPOOL``, and URLs, tokens, IP addresses and
raw stanza text are removed. This keeps ``repair`` useful even when OMEMO or the
model provider is the thing that is broken.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from .config import Config

JID_RE = re.compile(
    r"(?<![\w.+-])([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})(?:/[^\s,;:]+)?")
URL_RE = re.compile(r"https?://[^\s]+")
IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
TOKEN_RE = re.compile(r"\b[A-Za-z0-9+/_-]{32,}={0,2}\b")
HOME_RE = re.compile(r"/home/[^/\s]+")
RAW_STANZA_MARKERS = ("RECV:", "SEND:", "<message", "<body", "<encrypted", "<iq ")


class Redactor:
    """Turn identifiers into stable aliases for one report."""

    def __init__(self, cfg: Config, bot_bare: str, omemo_path: str) -> None:
        self.cfg = cfg
        self.bot_bare = bot_bare.lower()
        self.owners = {owner.lower().split("/", 1)[0] for owner in cfg.policy.owners}
        self.redact_jids = cfg.repair.redact_jids
        self._aliases: dict[str, str] = {}
        self._replacements = [
            (str(Path(omemo_path).parent), "$STATE"),
            (str(Path(cfg.features.spool_dir)), "$SPOOL"),
            (str(Path(cfg.opencode.workspace).expanduser()), "$WORKSPACE"),
            (str(Path(cfg.path).parent), "$CONFIG"),
            (str(Path.home()), "~"),
        ]

    def alias(self, jid: str) -> str:
        bare = (jid or "").strip().split("/", 1)[0].lower()
        if not bare:
            return "unknown"
        if bare == self.bot_bare:
            return "this-bridge"
        if bare in self.owners:
            return "owner"
        if bare not in self._aliases:
            self._aliases[bare] = f"contact-{len(self._aliases) + 1}"
        return self._aliases[bare]

    def path(self, value: str) -> str:
        text = str(value)
        for old, new in self._replacements:
            if old and old != "/":
                text = text.replace(old, new)
        return HOME_RE.sub("~", text)

    def text(self, value: str, *, limit: int = 240) -> str:
        text = self.path(str(value))
        if self.redact_jids:
            text = JID_RE.sub(lambda match: self.alias(match.group(0)), text)
        text = URL_RE.sub("[url]", text)
        text = IP_RE.sub("[ip]", text)
        text = TOKEN_RE.sub("[token]", text)
        text = " ".join(text.split())
        return text[:limit]


def _record(red: Redactor, level: str, line: str) -> str:
    return f"[{level}] {red.text(line)}"


async def _run(argv: list[str], timeout: float = 15.0,
               cwd: str | None = None) -> tuple[int, str, str]:
    try:
        result = await asyncio.to_thread(
            subprocess.run, argv, capture_output=True, text=True, timeout=timeout,
            check=False, cwd=cwd)
        return result.returncode, result.stdout or "", result.stderr or ""
    except FileNotFoundError:
        return 127, "", f"{argv[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{argv[0]} timed out"
    except OSError as exc:
        return 1, "", str(exc)


def _kv(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values[key.strip()] = value.strip()
    return values


async def check_service(cfg: Config, bridge: Any, red: Redactor) -> list[str]:
    unit = bridge._restart_unit()
    code, out, err = await _run([
        "systemctl", "--user", "show", unit,
        "-p", "ActiveState", "-p", "SubState", "-p", "NRestarts",
        "-p", "ExecMainStatus", "-p", "MemoryCurrent",
    ])
    if code != 0:
        return [_record(red, "fail", f"service: cannot read {unit}: {err or out or code}")]
    info = _kv(out)
    active = info.get("ActiveState", "unknown")
    sub = info.get("SubState", "unknown")
    restarts = info.get("NRestarts", "?")
    exit_code = info.get("ExecMainStatus", "?")
    memory = info.get("MemoryCurrent", "?")
    level = "ok" if active == "active" else "fail"
    return [_record(
        red, level,
        f"service: {unit} {active}/{sub}, restarts={restarts}, "
        f"last_exit={exit_code}, memory={memory}")]


async def check_logs(cfg: Config, bridge: Any, red: Redactor) -> list[str]:
    unit = bridge._restart_unit()
    minutes = cfg.repair.log_minutes
    code, out, err = await _run([
        "journalctl", "--user", "-u", unit, "--since", f"{minutes} min ago",
        "-o", "json", "--no-pager",
    ], timeout=20)
    if code != 0:
        return [_record(red, "fail", f"logs: cannot read {unit}: {err or out or code}")]
    errors = warnings = 0
    samples: list[str] = []
    for raw in out.splitlines():
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        message = str(entry.get("MESSAGE") or "")
        if not message or any(marker in message for marker in RAW_STANZA_MARKERS):
            continue
        try:
            priority = int(entry.get("PRIORITY", 6))
        except (TypeError, ValueError):
            priority = 6
        if priority <= 3:
            errors += 1
        elif priority == 4:
            warnings += 1
        else:
            continue
        if len(samples) < 3:
            samples.append(red.text(message, limit=180))
    level = "fail" if errors else ("warn" if warnings else "ok")
    summary = f"logs: {errors} errors, {warnings} warnings in {minutes} min"
    if samples:
        summary += "; " + " | ".join(samples)
    return [_record(red, level, summary)]


def _session_counts(data: dict[str, Any]) -> tuple[int, int, int]:
    sessions = devices = trust = 0
    for key in data:
        if key.endswith("/initiation") and key.startswith((
                "/eu.siacs.conversations.axolotl/", "/urn:xmpp:omemo:2/")):
            sessions += 1
        elif key.endswith("/identity_key") and key.startswith("/devices/"):
            devices += 1
        elif key.startswith("/trust/"):
            trust += 1
    return sessions, devices, trust


async def check_omemo(cfg: Config, bridge: Any, red: Redactor) -> list[str]:
    path = Path(bridge._omemo_storage_path())
    if not path.is_file():
        return [_record(red, "fail", f"omemo: store missing at {red.path(str(path))}")]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [_record(red, "fail", f"omemo: store unreadable: {exc}")]
    sessions, devices, trust = _session_counts(data)
    own = data.get("/own_device_id", "?")
    size = path.stat().st_size
    return [_record(
        red, "ok",
        f"omemo: own device this-bridge ({own}), {sessions} sessions, "
        f"{devices} devices, {trust} trust entries, store {size} bytes")]


def _agent_paths() -> list[Path]:
    return [
        Path.cwd() / ".opencode" / "agents",
        Path.home() / ".config" / "opencode" / "agents",
    ]


def _find_agent(agent: str) -> Path | None:
    for base in _agent_paths():
        candidate = base / f"{agent}.md"
        if candidate.is_file():
            return candidate
    return None


async def check_opencode(cfg: Config, bridge: Any, red: Redactor) -> list[str]:
    workspace = str(Path(cfg.opencode.workspace).expanduser())
    binary = cfg.opencode.binary
    code, out, err = await _run([binary, "service", "status"], timeout=10, cwd=workspace)
    service = "ok" if code == 0 else "not running"
    data_dir = Path.home() / ".local" / "share" / "opencode"
    db = data_dir / "opencode.db"
    wal = data_dir / "opencode.db-wal"
    db_size = db.stat().st_size if db.is_file() else 0
    wal_size = wal.stat().st_size if wal.is_file() else 0
    admin = _find_agent(cfg.admin.agent)
    guest = _find_agent(cfg.guest.agent)
    missing = []
    if admin is None:
        missing.append(f"admin agent {cfg.admin.agent!r}")
    if guest is None:
        missing.append(f"guest agent {cfg.guest.agent!r}")
    level = "warn" if missing or code != 0 else "ok"
    line = (f"opencode: service {service}, db {db_size} bytes, wal {wal_size} bytes, "
            f"agents {'ok' if not missing else 'missing ' + ', '.join(missing)}")
    return [_record(red, level, line)]


def _tree_size(path: Path) -> tuple[int, int]:
    count = size = 0
    try:
        for root, _dirs, files in os.walk(path):
            for name in files:
                try:
                    stat = (Path(root) / name).stat()
                except OSError:
                    continue
                count += 1
                size += stat.st_size
    except OSError:
        pass
    return count, size


async def check_storage(cfg: Config, bridge: Any, red: Redactor) -> list[str]:
    spool = Path(cfg.features.spool_dir)
    count, size = _tree_size(spool)
    free_bytes: int | None = None
    try:
        free_bytes = shutil.disk_usage(Path.home()).free
        free = f"{free_bytes / (1024 ** 3):.1f} GiB free"
    except OSError:
        free = "disk usage unavailable"
    level = "warn" if count > 20 or (free_bytes is not None and free_bytes < 1024 ** 3) else "ok"
    return [_record(red, level, f"storage: spool {count} files/{size} bytes, {free}")]


async def check_config(cfg: Config, bridge: Any, red: Redactor) -> list[str]:
    secret = Path(cfg.account.password_file)
    mode = "missing"
    if secret.is_file():
        permissions = secret.stat().st_mode & 0o777
        mode = f"{permissions:03o}" + ("" if not permissions & 0o077 else " (too open)")
    workspace = Path(cfg.opencode.workspace).expanduser()
    workspace_state = "missing"
    if workspace.is_dir():
        workspace_state = "writable" if os.access(workspace, os.W_OK) else "not writable"
    level = "ok" if secret.is_file() and not (secret.stat().st_mode & 0o077) and workspace.is_dir() else "warn"
    return [_record(
        red, level,
        f"config: secret {mode}, workspace {workspace_state}, "
        f"owners={len(cfg.policy.owners)}, omemo={cfg.features.omemo}")]


async def check_pending(cfg: Config, bridge: Any, red: Redactor) -> list[str]:
    count = len(getattr(bridge, "_pending", {}))
    return [_record(red, "ok", f"pending: {count} contact requests")]


async def check_network(cfg: Config, bridge: Any, red: Redactor) -> list[str]:
    upload = getattr(bridge, "_upload_host", "") or "not configured"
    return [_record(
        red, "ok",
        f"network: handling this command as this-bridge, upload service {upload}")]


CHECKS: dict[str, Callable[[Config, Any, Redactor], Awaitable[list[str]]]] = {
    "config": check_config,
    "service": check_service,
    "logs": check_logs,
    "omemo": check_omemo,
    "opencode": check_opencode,
    "storage": check_storage,
    "pending": check_pending,
    "network": check_network,
}
ORDER = tuple(CHECKS)


async def run_checks(cfg: Config, bridge: Any, focus: str = "") -> str:
    """Run one or all checks and return the already-redacted report."""
    focus = (focus or "").strip().lower()
    if not focus or focus == "all":
        selected = list(ORDER)
    elif focus in CHECKS:
        selected = [focus]
    else:
        return "Checks: " + ", ".join(("all",) + ORDER)
    red = Redactor(cfg, getattr(bridge, "bot_bare", cfg.account.jid),
                   bridge._omemo_storage_path())
    lines = [
        f"repair: {focus or 'all'}",
        f"redacted: {'yes' if cfg.repair.redact_jids else 'no'}",
    ]
    for name in selected:
        try:
            lines.extend(await CHECKS[name](cfg, bridge, red))
        except Exception as exc:  # noqa: BLE001 - one bad check must not hide the rest
            lines.append(f"[fail] {name}: {red.text(str(exc))}")
    lines.append(f"checked: {time.strftime('%Y-%m-%d %H:%M %Z')}")
    return "\n".join(lines)
