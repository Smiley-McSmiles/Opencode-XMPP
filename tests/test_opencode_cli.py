"""The CLI wrapper: argument building and the JSONL turn loop.

The real binary is never needed: a shell script stands in for ``opencode`` so the
subprocess path (stdin, JSONL parsing, exit codes, timeout, interruption) is
exercised for real.
"""

import json
import os
import stat
import sys
import textwrap
import threading
import time

import pytest

from opencode_xmpp import opencode_cli as cli

FAKE = textwrap.dedent("""\
    #!/bin/sh
    # Fake opencode. Args are logged; stdin is read only for `run`.
    case "$1" in
      run)
        printf '%s\\n' "$@" > "$FAKE_ARGV"
        cat > "$FAKE_STDIN"
        if [ -f "$FAKE_SLEEP" ]; then sleep "$(cat "$FAKE_SLEEP")"; fi
        if [ -f "$FAKE_JSONL" ]; then cat "$FAKE_JSONL"; fi
        if [ -f "$FAKE_STDERR" ]; then cat "$FAKE_STDERR" >&2; fi
        if [ -f "$FAKE_EXIT" ]; then exit "$(cat "$FAKE_EXIT")"; fi
        exit 0
        ;;
      --version)
        if [ -f "$FAKE_JSONL" ]; then cat "$FAKE_JSONL"; fi
        exit 0
        ;;
      *)
        if [ -f "$FAKE_JSONL" ]; then cat "$FAKE_JSONL"; fi
        if [ -f "$FAKE_STDERR" ]; then cat "$FAKE_STDERR" >&2; fi
        if [ -f "$FAKE_EXIT" ]; then exit "$(cat "$FAKE_EXIT")"; fi
        exit 0
        ;;
    esac
""")


@pytest.fixture()
def fake(tmp_path, monkeypatch):
    """A fake CLI on PATH plus the files its script consults."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    binary = bindir / "opencode"
    binary.write_text(FAKE)
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    for name in ("FAKE_ARGV", "FAKE_STDIN", "FAKE_JSONL", "FAKE_STDERR",
                 "FAKE_SLEEP", "FAKE_EXIT"):
        monkeypatch.setenv(name, str(tmp_path / name))
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    settings = cli.RunSettings(binary=str(binary), workspace=str(tmp_path))
    return settings, tmp_path


def _events(*events):
    return "\n".join(json.dumps(e) for e in events) + "\n"


def _write(path, text):
    path.write_text(text)


def _text_event(text):
    return {"type": "text", "part": {"type": "text", "text": text}}


def _settings(tmp_path, **kwargs):
    base = dict(binary=str(tmp_path / "bin" / "opencode"), workspace=str(tmp_path))
    base.update(kwargs)
    return cli.RunSettings(**base)


# -- binary discovery --------------------------------------------------------

def test_find_binary_resolves_a_path_on_path(fake):
    settings, _ = fake
    found = cli.find_binary(settings)
    assert found and os.path.isabs(found)


def test_find_binary_resolves_an_absolute_path(fake):
    settings, tmp_path = fake
    assert cli.find_binary(settings) == str(tmp_path / "bin" / "opencode")


def test_find_binary_returns_none_for_a_missing_path():
    assert cli.find_binary(cli.RunSettings(binary="/nope/opencode")) is None


def test_version_reads_the_first_line(fake):
    settings, tmp_path = fake
    _write(tmp_path / "FAKE_JSONL", "2.1.4\n")
    # The fake script's stdout doubles as --version output when no JSONL file has
    # JSON; the wrapper only needs a first line.
    _write(tmp_path / "FAKE_ARGV", "")
    text = cli.version(settings)
    assert text == "2.1.4"


# -- argument building -------------------------------------------------------

def test_command_carries_session_agent_model_auto_and_files(fake):
    settings, tmp_path = fake
    settings = cli.RunSettings(binary=settings.binary, workspace=settings.workspace,
                               agent="xmpp-admin", model="openrouter/x#fast",
                               auto_approve=True)
    turn = cli.Turn(user="hi", session_id="ses_abc", files=("/tmp/a.png",),
                    title="XMPP")
    argv = cli._command(settings, turn)
    assert argv[1:] == ["run", "--format", "json", "--session", "ses_abc",
                        "--agent", "xmpp-admin", "--model", "openrouter/x#fast",
                        "--auto", "--title", "XMPP", "--file", "/tmp/a.png"]


def test_turn_agent_overrides_the_settings_agent(fake):
    settings, _ = fake
    settings = cli.RunSettings(binary=settings.binary, workspace=settings.workspace,
                               agent="xmpp-guest")
    argv = cli._command(settings, cli.Turn(user="x", agent="xmpp-admin"))
    assert argv[argv.index("--agent") + 1] == "xmpp-admin"


def test_command_omits_optional_flags(fake):
    settings, _ = fake
    argv = cli._command(settings, cli.Turn(user="x"))
    assert "--agent" not in argv and "--model" not in argv and "--file" not in argv


def test_missing_workspace_is_a_friendly_error(fake):
    settings, _ = fake
    settings = cli.RunSettings(binary=settings.binary, workspace="/nope/nope")
    with pytest.raises(cli.CLIError, match="workspace"):
        cli.run(settings, cli.Turn(user="x"))


# -- the turn loop -----------------------------------------------------------

def test_run_returns_concatenated_text_and_writes_stdin(fake):
    settings, tmp_path = fake
    _write(tmp_path / "FAKE_JSONL", _events(
        {"type": "step_start"},
        _text_event("first part"),
        {"type": "tool_use", "part": {"tool": "read", "state": {}}},
        _text_event("second part"),
    ))
    answer = cli.run(settings, cli.Turn(user="hello there"))
    assert answer.status == "completed"
    assert answer.text == "first part\n\nsecond part"
    assert answer.events == 4
    assert (tmp_path / "FAKE_STDIN").read_text() == "hello there"


def test_run_passes_session_and_agent_on_the_command_line(fake):
    settings, tmp_path = fake
    _write(tmp_path / "FAKE_JSONL", _events(_text_event("ok")))
    cli.run(settings, cli.Turn(user="x", session_id="ses_abc", agent="xmpp-guest"))
    argv = (tmp_path / "FAKE_ARGV").read_text().split()
    assert argv[:5] == ["run", "--format", "json", "--session", "ses_abc"]
    assert "--agent" in argv and "xmpp-guest" in argv


def test_run_calls_the_event_callback_for_every_event(fake):
    settings, tmp_path = fake
    _write(tmp_path / "FAKE_JSONL", _events(
        {"type": "step_start"}, _text_event("hi"), {"type": "step_finish"}))
    seen = []
    cli.run(settings, cli.Turn(user="x"), on_event=seen.append)
    assert [e["type"] for e in seen] == ["step_start", "text", "step_finish"]


def test_run_hands_the_process_to_on_start(fake):
    settings, tmp_path = fake
    _write(tmp_path / "FAKE_JSONL", _events(_text_event("hi")))
    seen = []
    cli.run(settings, cli.Turn(user="x"), on_start=seen.append)
    assert seen and seen[0].pid > 0


def test_run_ignores_non_json_noise_on_stdout(fake):
    settings, tmp_path = fake
    _write(tmp_path / "FAKE_JSONL", "warning: something\n" + _events(_text_event("ok")))
    assert cli.run(settings, cli.Turn(user="x")).text == "ok"


def test_run_raises_with_stderr_when_the_exit_code_is_non_zero(fake):
    settings, tmp_path = fake
    _write(tmp_path / "FAKE_STDERR", "Error: not authenticated for openrouter\n")
    _write(tmp_path / "FAKE_EXIT", "1")
    with pytest.raises(cli.CLIError, match="not authenticated"):
        cli.run(settings, cli.Turn(user="x"))


def test_run_raises_on_an_error_event_without_text(fake):
    settings, tmp_path = fake
    _write(tmp_path / "FAKE_JSONL", _events(
        {"type": "error", "error": "unknown model openrouter/x"}))
    with pytest.raises(cli.CLIError, match="model is not available"):
        cli.run(settings, cli.Turn(user="x"))


def test_run_keeps_the_text_when_an_error_event_follows_it(fake):
    settings, tmp_path = fake
    _write(tmp_path / "FAKE_JSONL", _events(
        _text_event("partial answer"),
        {"type": "error", "error": "stream closed"}))
    answer = cli.run(settings, cli.Turn(user="x"))
    assert answer.text == "partial answer"


def test_timeout_is_reported_as_a_friendly_error(fake):
    settings, tmp_path = fake
    _write(tmp_path / "FAKE_SLEEP", "30")
    settings = _settings(tmp_path, timeout_s=1.0)
    started = time.monotonic()
    with pytest.raises(cli.CLIError, match="too long"):
        cli.run(settings, cli.Turn(user="x"))
    assert time.monotonic() - started < 15


def test_stop_event_cancels_the_turn(fake):
    settings, tmp_path = fake
    _write(tmp_path / "FAKE_SLEEP", "30")
    stop = threading.Event()

    def _stop_soon():
        time.sleep(0.7)
        stop.set()

    threading.Thread(target=_stop_soon, daemon=True).start()
    answer = cli.run(settings, cli.Turn(user="x"), stop_event=stop)
    assert answer.status == "cancelled"
    assert answer.text == ""


def test_interrupt_without_a_process_does_not_raise(fake):
    settings, _ = fake
    cli.interrupt(settings, "ses_abc", None)


def test_compact_and_context_fall_back_when_the_api_is_missing(fake):
    settings, tmp_path = fake
    _write(tmp_path / "FAKE_EXIT", "1")
    assert cli.compact(settings, "ses_abc") is False
    assert cli.context(settings, "ses_abc") is None


def test_context_reads_the_data_wrapper(fake):
    settings, tmp_path = fake
    _write(tmp_path / "FAKE_JSONL", json.dumps({"data": [{"type": "user"}]}))
    # `opencode api get` uses the same binary; the fake prints the same JSON.
    assert cli.context(settings, "ses_abc") == [{"type": "user"}]


# -- error wording -----------------------------------------------------------

@pytest.mark.parametrize("stderr,events,expected", [
    ("", ["unknown model"], "not available"),
    ("", ["not authenticated"], "not authenticated"),
    ("", ["session not found"], "could not be opened"),
    ("", ["context too long"], "too long"),
    ("", ["request (4461 tokens) exceeds the available context size (4096 tokens)"],
     "context window"),
    ("", ["invalid image input"], "JPEG and PNG"),
    ("", ["rate limit exceeded"], "rate limiting"),
    ("something else", [], "could not complete"),
])
def test_friendly_error_mapping(stderr, events, expected):
    assert expected in cli._friendly_error(stderr, events)


def test_answer_dataclass_defaults():
    answer = cli.Answer(text="x")
    assert answer.status == "completed" and answer.exit_code == 0
