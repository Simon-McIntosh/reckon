"""Tests for the run inbox: its hook, its registration and its writer.

A coordinator's message reaches a worker only if three separate things hold: the
run home carries the hook, the hook claims a message atomically so parallel tool
calls deliver it once, and the writer refuses every run whose home carries no
hook rather than reporting a queue nobody will read. Each of the three has its
own cases here, and each is exercised through the artifact a reader would open —
the hook is run as a subprocess, a run home is seeded through the fence's own
seeder, and the writer is driven through the CLI verb.

Every case builds its run directory, its crew home and its harness homes under
``tmp_path``. Nothing here reads or writes the operator's own ``~/.claude`` or
the workstation's live crew pointers, and the real home is fingerprinted before
and after the whole module by ``_real_homes_untouched``.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from pwd import getpwuid

import pytest
from click.testing import CliRunner

from reckon import _worker_fence
from reckon.cli import main
from reckon.hooks import install as installer
from reckon.hooks import worker_inbox

# The declared negative control: the claiming rename is replaced by a plain
# read, so every concurrent hook process delivers the same message.
NEGATIVE_CONTROL_MUTATION = (
    "replace the claiming rename in worker_inbox.claim with a plain read; the "
    "three-concurrent-invocations test fails by delivering one message twice or "
    "more"
)

RUN_ID = "r-20261110T120000000000-inbox-node"


def _hook_env(manifest: Path | None, home: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key != "RECKON_MANIFEST"}
    env["RECKON_HOME"] = str(home)
    if manifest is not None:
        env["RECKON_MANIFEST"] = str(manifest)
    return env


def _hook_argv() -> list[str]:
    return [sys.executable, str(Path(worker_inbox.__file__).resolve())]


def _run_hook(payload: dict, *, manifest: Path | None, home: Path, cwd: Path) -> str:
    result = subprocess.run(
        _hook_argv(),
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=cwd,
        env=_hook_env(manifest, home),
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    return result.stdout


def _run_dir(tmp_path: Path) -> tuple[Path, Path]:
    """A run directory with its live pointer file, and the manifest path."""
    run = tmp_path / "runs" / RUN_ID
    run.mkdir(parents=True, exist_ok=True)
    manifest = run / "manifest.md"
    manifest.write_text("status: in-progress\n")
    return run, manifest


def _crew_home(tmp_path: Path, record: dict) -> Path:
    """A crew home holding one live pointer, and the home itself."""
    home = tmp_path / "crew-home"
    live = home / "crew" / "live"
    live.mkdir(parents=True)
    (live / f"{RUN_ID}.json").write_text(json.dumps(record))
    return home


def _record(tmp_path: Path, **overrides) -> dict:
    _, manifest = _run_dir(tmp_path)
    record = {
        "run_id": RUN_ID,
        "dialect": "claude",
        "fenced": True,
        "manifest_path": str(manifest),
        "launcher_host": socket.gethostname(),
        "pid": os.getpid(),
        "worktree": str(tmp_path / "worktree"),
    }
    record.update(overrides)
    return record


def _seeded_claude_home(tmp_path: Path, operator_settings: dict | None) -> Path:
    """Seed a Claude-dialect run home through the fence's own seeder."""
    operator = tmp_path / "operator"
    claude = operator / ".claude"
    claude.mkdir(parents=True)
    if operator_settings is not None:
        (claude / "settings.json").write_text(json.dumps(operator_settings))
    home = tmp_path / "run" / "harness"
    _worker_fence.seed_harness_home(
        home,
        dialect_name="claude",
        operator_home=operator,
        declaration=[{"path": "settings.json", "keys": ["hooks"]}],
    )
    return home


def _post_tool_use(home: Path) -> list[dict]:
    settings = json.loads((home / "settings.json").read_text())
    return settings.get("hooks", {}).get("PostToolUse", [])


def _inbox_commands(home: Path) -> list[str]:
    return [
        hook["command"]
        for group in _post_tool_use(home)
        for hook in group.get("hooks", [])
    ]


def _receipts(run: Path) -> list[dict]:
    return worker_inbox.receipts(run)


# ── the hook delivers ────────────────────────────────────────────────────────


def test_a_message_written_before_a_tool_call_is_delivered_once_with_a_receipt(
    tmp_path: Path,
):
    run, manifest = _run_dir(tmp_path)
    home = _crew_home(tmp_path, {})
    written = worker_inbox.write_message("codeword: pelican", run)

    payload = {"tool_name": "Bash", "tool_use_id": "toolu_1", "session_id": "s-1"}
    output = _run_hook(payload, manifest=manifest, home=home, cwd=tmp_path)

    delivered = json.loads(output)
    context = delivered["hookSpecificOutput"]["additionalContext"]
    assert delivered["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "codeword: pelican" in context
    assert written.name in context

    recorded = _receipts(run)
    assert len(recorded) == 1
    assert recorded[0]["message"] == written.name
    assert recorded[0]["tool_name"] == "Bash"
    assert recorded[0]["tool_use_id"] == "toolu_1"
    assert recorded[0]["delivered_at"].endswith("Z")
    # The message left the inbox: a second tool call has nothing to deliver.
    assert not written.exists()
    assert _run_hook(payload, manifest=manifest, home=home, cwd=tmp_path) == ""
    assert len(_receipts(run)) == 1


def test_three_concurrent_hook_invocations_deliver_one_message_exactly_once(
    tmp_path: Path,
):
    run, manifest = _run_dir(tmp_path)
    home = _crew_home(tmp_path, {})
    payload = {"tool_name": "Bash", "tool_use_id": "toolu_race", "session_id": "s-1"}
    env = _hook_env(manifest, home)

    written: list[Path] = []
    for index in range(3):
        message = f"only message {index}"
        written.append(worker_inbox.write_message(message, run))
        processes = [
            subprocess.Popen(
                _hook_argv(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=tmp_path,
                env=env,
            )
            for _ in range(3)
        ]
        outputs = []
        for process in processes:
            out, err = process.communicate(json.dumps(payload), timeout=60)
            assert process.returncode == 0, err
            outputs.append(out)
        # Exactly one of the three delivered it: the others lost the claim.
        delivering = [out for out in outputs if out.strip()]
        assert len(delivering) == 1, outputs
        assert message in delivering[0], delivering[0]
        assert written[-1].name in delivering[0], delivering[0]
        assert not written[-1].exists()

    assert sorted(entry["message"] for entry in _receipts(run)) == sorted(
        path.name for path in written
    )


def test_an_empty_inbox_and_a_missing_run_directory_print_nothing(tmp_path: Path):
    run, manifest = _run_dir(tmp_path)
    home = _crew_home(tmp_path, {})
    (run / "inbox").mkdir()
    payload = {"tool_name": "Bash"}

    assert _run_hook(payload, manifest=manifest, home=home, cwd=tmp_path) == ""
    assert _receipts(run) == []

    # No manifest variable and no live pointer naming this directory: the hook
    # resolves no run at all, which is every interactive session.
    nowhere = tmp_path / "elsewhere"
    nowhere.mkdir()
    assert _run_hook(payload, manifest=None, home=home, cwd=nowhere) == ""


def test_pending_messages_lists_what_no_tool_call_has_claimed(tmp_path: Path):
    run, _ = _run_dir(tmp_path)
    written = worker_inbox.write_message("not yet read", run)

    assert worker_inbox.pending_messages(run) == [written]
    worker_inbox.claim(written)
    assert worker_inbox.pending_messages(run) == []
    assert worker_inbox.claim(written) is None


# ── the writer refuses what it cannot deliver to ─────────────────────────────


def _tell(message: str = "steer left"):
    return CliRunner().invoke(
        main, ["crew", "tell", "--run", RUN_ID, message], catch_exceptions=False
    )


def _seeded_run(tmp_path: Path, operator_settings: dict | None = None) -> tuple:
    """A run directory whose own harness home carries the inbox hook."""
    run, manifest = _run_dir(tmp_path)
    operator = tmp_path / "operator"
    (operator / ".claude").mkdir(parents=True)
    if operator_settings is not None:
        (operator / ".claude" / "settings.json").write_text(
            json.dumps(operator_settings)
        )
    run_home = run / "harness"
    _worker_fence.seed_harness_home(
        run_home,
        dialect_name="claude",
        operator_home=operator,
        declaration=[{"path": "settings.json", "keys": ["hooks"]}],
    )
    return run, manifest, run_home


def test_crew_tell_writes_into_a_live_fenced_claude_run(tmp_path: Path, monkeypatch):
    run, manifest, _ = _seeded_run(tmp_path)
    home = _crew_home(tmp_path, _record(tmp_path, manifest_path=str(manifest)))
    monkeypatch.setenv("RECKON_HOME", str(home))

    result = _tell()
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["status"] == "queued_for_next_tool_call"
    assert payload["run_id"] == RUN_ID
    queued = worker_inbox.pending_messages(run)
    assert len(queued) == 1
    assert queued[0].read_text() == "steer left"


def test_crew_tell_refuses_an_ended_run_naming_resume(tmp_path: Path, monkeypatch):
    record = _record(tmp_path, pid=999_999_999)
    home = _crew_home(tmp_path, record)
    monkeypatch.setenv("RECKON_HOME", str(home))

    result = _tell()
    assert result.exit_code != 0, result.output
    assert "has ended" in result.output
    assert f"reckon crew resume --run {RUN_ID}" in result.output
    assert worker_inbox.pending_messages(Path(record["manifest_path"]).parent) == []


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"fenced": False}, "not fenced"),
        ({"fenced": None}, "not fenced"),
        ({"dialect": "codex", "fenced": True}, "Claude-dialect run homes only"),
    ],
)
def test_crew_tell_refuses_a_run_with_no_hook(
    tmp_path: Path, monkeypatch, overrides, expected
):
    record = _record(tmp_path, **overrides)
    if overrides.get("fenced") is None:
        record.pop("fenced")
    home = _crew_home(tmp_path, record)
    monkeypatch.setenv("RECKON_HOME", str(home))

    result = _tell()
    assert result.exit_code != 0, result.output
    assert expected in result.output
    assert "queued_for_next_tool_call" not in result.output
    assert worker_inbox.pending_messages(Path(record["manifest_path"]).parent) == []


def test_crew_tell_refuses_a_home_seeded_without_the_hook(tmp_path: Path, monkeypatch):
    run, manifest, run_home = _seeded_run(tmp_path)
    settings = json.loads((run_home / "settings.json").read_text())
    settings["hooks"].pop("PostToolUse", None)
    (run_home / "settings.json").write_text(json.dumps(settings))
    home = _crew_home(tmp_path, _record(tmp_path, manifest_path=str(manifest)))
    monkeypatch.setenv("RECKON_HOME", str(home))

    result = _tell()
    assert result.exit_code != 0, result.output
    assert "carries no PostToolUse entry" in result.output
    assert worker_inbox.pending_messages(run) == []


# ── the verb names its state, as the help contract requires ──────────────────


def test_the_tell_verb_names_its_state_in_its_help():
    """The verb's help opens by naming the state it acts on.

    Read the same way the crew help contract reads every verb — the command's
    own first help line — so a row added for this verb to that contract's table
    matches here without either asserting about the other's rendering.
    """
    from reckon import cli as cli_module

    command = cli_module.crew.commands["tell"]
    first_line = (command.help or command.short_help or "").strip().splitlines()[0]
    assert "a live run's inbox" in first_line

    result = CliRunner().invoke(main, ["crew", "tell", "--help"])
    assert result.exit_code == 0, result.output
    assert "a live run's inbox" in " ".join(result.output.split())


# ── the run home carries exactly one entry ───────────────────────────────────


def test_a_seeded_home_with_no_operator_settings_carries_one_entry(tmp_path: Path):
    home = _seeded_claude_home(tmp_path, None)

    commands = _inbox_commands(home)
    assert len(_post_tool_use(home)) == 1
    assert len(commands) == 1
    assert Path(commands[0].split()[-1]).name == "worker_inbox.py"
    assert installer.settings_carry_worker_inbox(home / "settings.json")


def test_a_seeded_home_keeps_the_operators_hooks_and_adds_one_entry(tmp_path: Path):
    operator_hook = {
        "hooks": [{"type": "command", "command": "/usr/bin/operator-hook"}]
    }
    home = _seeded_claude_home(tmp_path, {"hooks": {"Stop": [operator_hook]}})

    settings = json.loads((home / "settings.json").read_text())
    assert settings["hooks"]["Stop"] == [operator_hook]
    assert len(_post_tool_use(home)) == 1
    assert installer.settings_carry_worker_inbox(home / "settings.json")


def test_a_home_already_seeded_without_the_entry_gains_exactly_one(tmp_path: Path):
    home = tmp_path / "run" / "harness"
    home.mkdir(parents=True)
    existing = {"hooks": {"PostToolUse": [{"hooks": []}]}, "model": "x"}
    (home / "settings.json").write_text(json.dumps(existing))

    operator = tmp_path / "operator"
    (operator / ".claude").mkdir(parents=True)
    _worker_fence.seed_harness_home(
        home,
        dialect_name="claude",
        operator_home=operator,
        declaration=[{"path": "settings.json", "keys": [".claude", "hooks"]}],
    )
    _worker_fence.seed_harness_home(
        home,
        dialect_name="claude",
        operator_home=operator,
        declaration=[{"path": "settings.json", "keys": ["hooks"]}],
    )

    settings = json.loads((home / "settings.json").read_text())
    assert settings["model"] == "x"
    assert settings["hooks"]["PostToolUse"][0] == {"hooks": []}
    assert len(_post_tool_use(home)) == 2
    assert len(_inbox_commands(home)) == 1
    assert installer.settings_carry_worker_inbox(home / "settings.json")


def test_a_codex_home_is_written_nothing(tmp_path: Path):
    operator = tmp_path / "operator"
    (operator / ".codex").mkdir(parents=True)
    home = tmp_path / "run" / "codex-home"
    _worker_fence.seed_harness_home(
        home,
        dialect_name="codex",
        operator_home=operator,
        declaration=[{"path": "config.toml"}],
    )

    assert not (home / "settings.json").exists()


# ── the manifest reports what it received ────────────────────────────────────


def test_a_receipt_with_no_quoted_message_is_flagged(tmp_path: Path):
    run, _ = _run_dir(tmp_path)
    worker_inbox.write_message("grep the receipts first", run)
    claimed = worker_inbox.claim(worker_inbox.pending_messages(run)[0])
    worker_inbox.append_receipt(run, name=claimed.name, payload={"tool_name": "Bash"})

    finding = worker_inbox.manifest_message_finding("status: complete\n", run)

    assert finding is not None
    assert "delivery receipt" in finding
    assert "quotes none" in finding


def test_a_quoted_message_satisfies_the_check(tmp_path: Path):
    run, _ = _run_dir(tmp_path)
    worker_inbox.write_message("grep the receipts first", run)
    claimed = worker_inbox.claim(worker_inbox.pending_messages(run)[0])
    worker_inbox.append_receipt(run, name=claimed.name, payload={"tool_name": "Bash"})

    text = "status: complete\nreceived: grep the receipts first\n"
    assert worker_inbox.manifest_message_finding(text, run) is None


def test_a_run_with_no_receipt_must_say_none_was_received(tmp_path: Path):
    run, _ = _run_dir(tmp_path)

    finding = worker_inbox.manifest_message_finding("status: complete\n", run)
    assert finding is not None
    assert "messages_received: none" in finding

    assert (
        worker_inbox.manifest_message_finding(
            "status: complete\nmessages_received: none\n", run
        )
        is None
    )


# ── the writer's settings reader ─────────────────────────────────────────────


def test_settings_carry_worker_inbox_survives_a_missing_or_odd_file(tmp_path: Path):
    missing = tmp_path / "nope.json"
    assert installer.settings_carry_worker_inbox(missing) is False

    broken = tmp_path / "nope.json"
    broken.write_text("{not json")
    assert installer.settings_carry_worker_inbox(broken) is False

    absent = tmp_path / "absent.json"
    absent.write_text(json.dumps({"hooks": {"PostToolUse": []}}))
    assert installer.settings_carry_worker_inbox(absent) is False


@pytest.fixture(scope="module", autouse=True)
def _real_homes_untouched():
    """The operator's own harness home is never read into or written."""
    real = Path(getpwuid(os.getuid()).pw_dir) / ".claude" / "settings.json"

    def fingerprint() -> tuple[bytes, int] | None:
        try:
            return real.read_bytes(), real.stat().st_mtime_ns
        except OSError:
            return None

    before = fingerprint()
    yield
    assert fingerprint() == before
