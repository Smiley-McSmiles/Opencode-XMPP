"""Run the ``opencode`` CLI for one XMPP turn.

One ``opencode run --format json --session <derived-id>`` subprocess per turn,
with the message written to stdin so it never appears in ``ps`` output. The
process is the unit of interruption: a new XMPP message stops it, and the next
message continues the same opencode session.

Why a subprocess and not the HTTP API: ``opencode run`` is the documented
automation surface and its flags survive opencode upgrades. The HTTP API is used
only for the two operations the CLI has no flag for — asking a session to compact
and asking a running session to interrupt — and both are best-effort.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger("opencode_xmpp.cli")

STDERR_TAIL_LINES = 40
STOP_GRACE_S = 5.0


class CLIError(Exception):
    """A turn failed. ``user_message`` is safe to send to the contact."""

    def __init__(self, user_message: str, detail: str = "") -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.detail = detail


@dataclass(frozen=True)
class RunSettings:
    binary: str = "opencode"
    workspace: str = "~"
    agent: str = ""
    model: str = ""
    auto_approve: bool = True
    timeout_s: float = 300.0
    env: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Turn:
    """One message for the CLI. ``user`` is the full text, prefix included."""

    user: str
    session_id: str = ""
    agent: str = ""
    files: tuple[str, ...] = ()
    title: str = ""


@dataclass
class Answer:
    text: str = ""
    status: str = "completed"      # completed | cancelled | failed
    exit_code: int = 0
    events: int = 0
    stderr_tail: str = ""


def find_binary(settings: RunSettings) -> str | None:
    """Absolute path to the CLI, or None when it cannot be found."""
    binary = (settings.binary or "opencode").strip()
    if os.sep in binary or binary.startswith("~"):
        candidate = Path(os.path.expanduser(binary))
        return str(candidate) if candidate.is_file() and os.access(candidate, os.X_OK) else None
    return shutil.which(binary)


def version(settings: RunSettings, timeout: float = 10.0) -> str | None:
    binary = find_binary(settings)
    if not binary:
        return None
    try:
        result = subprocess.run([binary, "--version"], capture_output=True, text=True,
                                timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (result.stdout or result.stderr or "").strip()
    return text.splitlines()[0] if text else None


def list_models(settings: RunSettings, timeout: float = 20.0) -> list[str]:
    """Model IDs from ``opencode models`` for the configured user/workspace."""
    binary = find_binary(settings)
    if not binary:
        raise CLIError("The opencode CLI is not installed or not on PATH.",
                       f"binary {settings.binary!r}")
    env = os.environ.copy()
    env.setdefault("HOME", str(Path.home()))
    for key, value in settings.env:
        env[str(key)] = str(value)
    try:
        result = subprocess.run(
            [binary, "models"], cwd=_workspace(settings), env=env,
            capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CLIError("I couldn't list the available models.", str(exc)) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:500]
        raise CLIError("I couldn't list the available models.", detail)
    models: list[str] = []
    for raw in (result.stdout or "").splitlines():
        line = raw.strip()
        if "/" in line and not line.lower().startswith(("name ", "provider ")):
            models.append(line)
    return models


def _workspace(settings: RunSettings) -> str:
    path = Path(os.path.expanduser(settings.workspace or "~"))
    if not path.is_dir():
        raise CLIError("The agent's workspace directory does not exist.",
                       f"workspace {path}")
    return str(path)


def _command(settings: RunSettings, turn: Turn) -> list[str]:
    binary = find_binary(settings)
    if not binary:
        raise CLIError("The opencode CLI is not installed or not on PATH.",
                       f"binary {settings.binary!r}")
    argv = [binary, "run", "--format", "json"]
    if turn.session_id:
        argv += ["--session", turn.session_id]
    agent = turn.agent or settings.agent
    if agent:
        argv += ["--agent", agent]
    if settings.model:
        argv += ["--model", settings.model]
    if settings.auto_approve:
        argv += ["--auto"]
    if turn.title:
        argv += ["--title", turn.title]
    for path in turn.files:
        argv += ["--file", str(path)]
    # The message goes in on stdin (see module docstring).
    return argv


def _terminate(proc: subprocess.Popen) -> None:
    """Stop the CLI and its children; escalate if it ignores SIGINT."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGINT)
    except (ProcessLookupError, PermissionError):
        try:
            proc.terminate()
        except OSError:
            return
    try:
        proc.wait(timeout=STOP_GRACE_S)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            proc.terminate()
        except OSError:
            return
    try:
        proc.wait(timeout=STOP_GRACE_S)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except OSError:
            pass


def _api_post(settings: RunSettings, path: str, payload: str = "{}",
              timeout: float = 15.0) -> bool:
    """Best-effort ``opencode api post``; False when the API is unavailable."""
    binary = find_binary(settings)
    if not binary:
        return False
    try:
        result = subprocess.run([binary, "api", "post", path, "--data", payload],
                                capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        log.debug("opencode api %s failed: %s", path, (result.stderr or "").strip()[:200])
        return False
    return True


def _api_get(settings: RunSettings, path: str, timeout: float = 15.0) -> dict | list | None:
    binary = find_binary(settings)
    if not binary:
        return None
    try:
        result = subprocess.run([binary, "api", "get", path], capture_output=True,
                                text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None


def list_sessions(settings: RunSettings, timeout: float = 20.0) -> list[dict]:
    """Top-level opencode sessions in the configured workspace, newest first."""
    binary = find_binary(settings)
    if not binary:
        raise CLIError("The opencode CLI is not installed or not on PATH.",
                       f"binary {settings.binary!r}")
    env = os.environ.copy()
    env.setdefault("HOME", str(Path.home()))
    for key, value in settings.env:
        env[str(key)] = str(value)
    try:
        result = subprocess.run(
            [binary, "session", "list", "--format", "json", "--max-count", "500"],
            cwd=_workspace(settings), env=env, capture_output=True, text=True,
            timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CLIError("I couldn't list the sessions.", str(exc)) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:500]
        raise CLIError("I couldn't list the sessions.", detail)
    try:
        data = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict) and item.get("id")]


def delete_session(settings: RunSettings, session_id: str, timeout: float = 30.0) -> tuple[bool, str]:
    """Delete one opencode session (and its children) as the bridge user.

    Returns ``(True, "")`` on success; otherwise a short, user-safe reason. The
    caller has already stopped any turn that was using the session.
    """
    binary = find_binary(settings)
    if not binary:
        return False, "the opencode CLI is not installed or not on PATH"
    try:
        result = subprocess.run(
            [binary, "session", "delete", session_id],
            cwd=_workspace(settings), capture_output=True, text=True,
            timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)[:200]
    if result.returncode == 0:
        return True, ""
    detail = (result.stderr or result.stdout or "").strip().splitlines()
    return False, (detail[0] if detail else f"exit {result.returncode}")[:200]


def interrupt(settings: RunSettings, session_id: str, proc: subprocess.Popen | None = None) -> None:
    """Stop a turn: signal the CLI first, then ask the server to abort it too."""
    if proc is not None:
        _terminate(proc)
    if session_id:
        _api_post(settings, f"/api/session/{session_id}/interrupt")


def compact(settings: RunSettings, session_id: str) -> bool:
    """Ask opencode to compact a session now (it runs at the next safe point)."""
    if not session_id:
        return False
    return _api_post(settings, f"/api/session/{session_id}/compact")


def context(settings: RunSettings, session_id: str) -> list | None:
    """The session's message list, or None when the API is unavailable."""
    payload = _api_get(settings, f"/api/session/{session_id}/context")
    if isinstance(payload, dict):
        data = payload.get("data")
        return data if isinstance(data, list) else None
    return payload if isinstance(payload, list) else None


def _friendly_error(stderr: str, events: list[str]) -> str:
    """A short sentence for the contact. Order matters: "session not found"
    must not be mistaken for a model problem."""
    text = " ".join([stderr or ""] + events).lower()
    if "unauthorized" in text or "not authenticated" in text or "auth" in text:
        return "opencode is not authenticated for that model provider."
    if "session" in text and ("not found" in text or "invalid" in text):
        return "The opencode session could not be opened."
    if "invalid image" in text or "image input" in text:
        return ("The model rejected that image. Ollama accepts JPEG and PNG; "
                "convert a GIF/WebP or send a smaller JPEG.")
    if "context" in text and ("exceed" in text or "too long" in text
                              or "limit" in text or "context size" in text):
        return ("That request is too long for the model's context window. "
                "Raise OLLAMA_CONTEXT_LENGTH on the Ollama service, send clear to "
                "start over, or send a smaller image.")
    if "rate limit" in text or "429" in text:
        return "The model provider is rate limiting right now. Try again shortly."
    if "not found" in text or "unknown model" in text or ("model" in text and "error" in text):
        return "The configured model is not available in opencode."
    return "The agent could not complete that turn."


def run(settings: RunSettings, turn: Turn,
        on_event: Callable[[dict], None] | None = None,
        on_start: Callable[[subprocess.Popen], None] | None = None,
        stop_event: threading.Event | None = None) -> Answer:
    """Run one turn to completion. Raises :class:`CLIError` on failure.

    ``on_event`` is called from the reader thread for every JSONL event, so it
    must be thread-safe. ``on_start`` receives the process as soon as it exists,
    which is what lets the bridge interrupt it.
    """
    if stop_event is None:
        stop_event = threading.Event()
    argv = _command(settings, turn)
    cwd = _workspace(settings)
    env = os.environ.copy()
    env.setdefault("HOME", str(Path.home()))
    for key, value in settings.env:
        env[str(key)] = str(value)

    log.info("opencode run: agent=%s session=%s files=%d chars=%d",
             turn.agent or settings.agent, turn.session_id, len(turn.files), len(turn.user))
    try:
        proc = subprocess.Popen(
            argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True, text=True,
            encoding="utf-8", errors="replace", bufsize=1,
        )
    except OSError as exc:
        raise CLIError("I could not start the agent.", str(exc)) from exc
    if on_start is not None:
        try:
            on_start(proc)
        except Exception:  # noqa: BLE001 - a callback must not break the run
            log.exception("on_start callback failed")

    timed_out = threading.Event()
    finished = threading.Event()

    def _watchdog() -> None:
        # A real timer: the deadline belongs to this turn, not to stop_event.
        deadline = time.monotonic() + max(1.0, settings.timeout_s)
        while time.monotonic() < deadline:
            if finished.is_set():
                return
            time.sleep(0.05)
        timed_out.set()
        _terminate(proc)

    watchdog = threading.Thread(target=_watchdog, name="opencode-timeout", daemon=True)
    watchdog.start()

    stderr_lines: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
    error_events: list[str] = []

    def _drain_stderr() -> None:
        assert proc.stderr is not None
        for raw in proc.stderr:
            line = raw.rstrip()
            if line:
                stderr_lines.append(line)
                log.debug("opencode stderr: %s", line[:300])

    err_thread = threading.Thread(target=_drain_stderr, name="opencode-stderr", daemon=True)
    err_thread.start()

    texts: list[str] = []
    events = 0
    try:
        assert proc.stdin is not None and proc.stdout is not None
        try:
            proc.stdin.write(turn.user)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass  # the process may have exited before reading; the exit code says why
        for raw in proc.stdout:
            if stop_event.is_set():
                _terminate(proc)
                break
            line = raw.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                log.debug("non-JSON output from opencode: %s", line[:200])
                continue
            events += 1
            etype = str(event.get("type") or "")
            part = event.get("part") if isinstance(event.get("part"), dict) else {}
            if etype == "text":
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    if not texts or texts[-1] != text:
                        texts.append(text)
            elif etype in ("error", "session.error"):
                detail = event.get("error") or part.get("error") or event.get("message")
                error_events.append(str(detail or "error")[:300])
            if on_event is not None:
                try:
                    on_event(event)
                except Exception:  # noqa: BLE001 - a callback must not break the run
                    log.exception("event callback failed")
    finally:
        finished.set()
        try:
            rc = proc.wait(timeout=STOP_GRACE_S + 5)
        except subprocess.TimeoutExpired:
            _terminate(proc)
            rc = proc.wait(timeout=STOP_GRACE_S)
        watchdog.join(timeout=0.2)
        err_thread.join(timeout=1.0)

    stderr_tail = "\n".join(stderr_lines).strip()
    text = "\n\n".join(t.strip() for t in texts if t.strip()).strip()

    if stop_event.is_set():
        return Answer(text="", status="cancelled", exit_code=rc, events=events,
                      stderr_tail=stderr_tail)
    if timed_out.is_set():
        raise CLIError("That took too long, so I stopped working on it.",
                       stderr_tail or f"timeout after {settings.timeout_s}s")
    if rc != 0:
        detail = stderr_tail or " ".join(error_events)
        log.error("opencode run failed (exit %s): %s", rc, detail[:500])
        raise CLIError(_friendly_error(stderr_tail, error_events),
                       f"exit={rc} {detail[:800]}")
    if error_events and not text:
        raise CLIError(_friendly_error(stderr_tail, error_events), " ".join(error_events)[:800])
    return Answer(text=text, status="completed", exit_code=rc, events=events,
                  stderr_tail=stderr_tail)
