"""The obligations hook speaks only for a session that is coordinating.

Every case here drives the hook as the harness does: a subprocess, hook JSON on
stdin, and the synthesised config home the payload's working directory resolves
through.  The two mapping rules are exercised separately, because a session
named verbatim after its crew session and a session whose follower was armed by
its own process resolve through different evidence -- and the second rule's
negative direction is what keeps one coordinator's list off another's screen.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from reckon.crew import runs

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "reckon" / "hooks" / "coordinator_obligations.py"
AUTHORITY_LINE = "mirror these into your task list; reckon's list is the authority"
PROJECT = "hook-fixture"
SESSION = "coordinator-hook-fixture"
RUN_ID = "run-hook-fixture"
NODE_ID = "hook-fixture-node"

# An age the hook renders from wall-clock distances, such as "2s" or "1h3m".
AGE = re.compile(r"\d+d\d+h|\d+h\d+m|\d+m\d+s|\d+s")


def _stamp(path: Path) -> tuple[int, int] | None:
    """A file's identity for an untouched check, or None when it is absent."""
    try:
        metadata = path.stat()
    except OSError:
        return None
    return (metadata.st_mtime_ns, metadata.st_size)


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.fixture()
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "config"
    home.mkdir()
    monkeypatch.setenv("RECKON_HOME", str(home))
    monkeypatch.setenv("RECKON_MOUNTS_PATH", str(home / "mounts.json"))
    return home


@pytest.fixture()
def repository(tmp_path: Path, config_home: Path) -> Path:
    root = tmp_path / "repo"
    (root / "docs" / "state" / PROJECT).mkdir(parents=True)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    for arguments in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "worker@example.invalid"),
        ("config", "user.name", "Worker"),
        ("add", "seed.txt"),
        ("commit", "-q", "-m", "test: seed hook fixture"),
    ):
        _git(root, *arguments)
    (config_home / "mounts.json").write_text(
        json.dumps({PROJECT: str(root / "docs")}), encoding="utf-8"
    )
    return root


def _blocked_run(repository: Path, tmp_path: Path) -> None:
    """Record one live run whose manifest holds its own turn open."""
    manifest = tmp_path / "manifests" / f"{RUN_ID}.md"
    manifest.parent.mkdir()
    manifest.write_text(f"node: {NODE_ID}\nstatus: blocked\n", encoding="utf-8")
    runs._write_json(
        runs.pointer_path(RUN_ID),
        {
            "run_id": RUN_ID,
            "project": PROJECT,
            "session": SESSION,
            "repo": str(repository),
            "worktree": str(repository),
            "base_sha": _git(repository, "rev-parse", "HEAD"),
            "process_alive": False,
            "role": "implement",
            "manifest_path": str(manifest),
            "node": {
                "id": NODE_ID,
                "plan": "fixture-plan",
                "section": "fixture-section",
                "time_budget": "20m",
                "write_paths": ["seed.txt"],
            },
        },
    )


def _hook(
    mode: str,
    payload: dict[str, object],
    *,
    claude_pid: int | None = None,
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT)
    if claude_pid is not None:
        environment["CLAUDE_PID"] = str(claude_pid)
    return subprocess.run(
        [sys.executable, str(HOOK), "--hook", mode],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )


def _prompt_payload(directory: Path, session_id: str) -> dict[str, object]:
    return {
        "session_id": session_id,
        "cwd": str(directory),
        "hook_event_name": "UserPromptSubmit",
    }


def _stop_payload(
    directory: Path, session_id: str, **extra: object
) -> dict[str, object]:
    return {"session_id": session_id, "cwd": str(directory), **extra}


def _expected_checklist(*, unreconciled: int) -> str:
    header = (
        f"reckon obligations for session {SESSION} (project {PROJECT}): "
        "1 outstanding, oldest <age>"
    )
    blocked = (
        f"- [blocked] {RUN_ID} ({NODE_ID}, <age> old): "
        "read <manifest>; resolve the blocker before resuming the run"
    )
    closure = (
        f"unreconciled runs: {unreconciled}; work the list to empty "
        "before ending the turn."
    )
    return f"{header}\n{blocked}\n{closure}\n{AUTHORITY_LINE}"


def _normalised(checklist: str, *, manifest: Path) -> str:
    """Replace the two wall-clock ages so the rest of the text compares exactly."""
    return AGE.sub("<age>", checklist).replace(str(manifest), "<manifest>")


def _registered_parent_pid() -> int:
    """The pid the follower registration names as the process that armed it."""
    for row in runs.list_followers(PROJECT):
        if row.get("session") == SESSION:
            record = row.get("follower") or {}
            return int(record["parent_pid"])
    raise AssertionError("the fixture registration is missing")


def test_prompt_mode_injects_the_checklist_for_a_coordinating_session(
    repository: Path, tmp_path: Path
) -> None:
    _blocked_run(repository, tmp_path)
    with runs.follower_claim(PROJECT, SESSION):
        completed = _hook("prompt", _prompt_payload(repository, "harness-session"))

    assert completed.returncode == 0
    assert completed.stderr == ""
    emitted = json.loads(completed.stdout)
    checklist = emitted["hookSpecificOutput"]["additionalContext"]
    assert set(emitted["hookSpecificOutput"]) == {
        "hookEventName",
        "additionalContext",
    }
    assert emitted["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert _normalised(checklist, manifest=tmp_path / "manifests" / f"{RUN_ID}.md") == (
        _expected_checklist(unreconciled=1)
    )
    assert checklist.endswith(AUTHORITY_LINE)


def test_stop_mode_blocks_while_obligations_remain_and_your_own_continuation_stops_it(
    repository: Path, tmp_path: Path
) -> None:
    _blocked_run(repository, tmp_path)
    manifest = tmp_path / "manifests" / f"{RUN_ID}.md"
    with runs.follower_claim(PROJECT, SESSION):
        first = _hook("stop", _stop_payload(repository, "harness-session"))
        continued = _hook(
            "stop",
            _stop_payload(repository, "harness-session", stop_hook_active=True),
        )

    assert first.returncode == 0
    decision = json.loads(first.stdout)
    assert set(decision) == {"decision", "reason"}
    assert decision["decision"] == "block"
    assert _normalised(decision["reason"], manifest=manifest) == (
        _expected_checklist(unreconciled=1)
    )

    assert continued.returncode == 0
    assert continued.stdout == ""
    assert continued.stderr == ""


def test_a_session_with_no_obligations_is_silent_in_both_modes(
    repository: Path,
) -> None:
    with runs.follower_claim(PROJECT, SESSION):
        prompting = _hook("prompt", _prompt_payload(repository, "harness-session"))
        stopping = _hook("stop", _stop_payload(repository, "harness-session"))

    assert prompting.returncode == 0
    assert prompting.stdout == ""
    assert stopping.returncode == 0
    assert stopping.stdout == ""


def test_a_directory_outside_the_mounts_is_silent(
    tmp_path: Path, config_home: Path
) -> None:
    outside = tmp_path / "not-a-checkout"
    outside.mkdir()

    prompting = _hook("prompt", _prompt_payload(outside, "harness-session"))
    stopping = _hook("stop", _stop_payload(outside, "harness-session"))

    assert prompting.returncode == 0
    assert prompting.stdout == ""
    assert stopping.returncode == 0
    assert stopping.stdout == ""


def test_a_follower_armed_by_this_session_names_it_and_another_sessions_does_not(
    repository: Path, tmp_path: Path
) -> None:
    _blocked_run(repository, tmp_path)
    with runs.follower_claim(PROJECT, SESSION):
        owner = _registered_parent_pid()
        ours = _hook(
            "prompt",
            _prompt_payload(repository, "harness-not-the-crew-session-name"),
            claude_pid=owner,
        )
        unrelated = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
        )
        try:
            theirs = _hook(
                "prompt",
                _prompt_payload(repository, "harness-not-the-crew-session-name"),
                claude_pid=unrelated.pid,
            )
        finally:
            unrelated.kill()
            unrelated.wait()

    assert owner > 1
    assert ours.returncode == 0
    assert json.loads(ours.stdout)["hookSpecificOutput"]["additionalContext"].endswith(
        AUTHORITY_LINE
    )
    assert theirs.returncode == 0
    assert theirs.stdout == ""


def test_the_real_settings_home_is_never_read_or_written(
    repository: Path, tmp_path: Path
) -> None:
    watched = [
        Path.home() / ".config" / "reckon",
        Path.home() / ".claude" / "settings.json",
        Path.home() / ".claude" / "settings.local.json",
    ]
    before = {path: _stamp(path) for path in watched}

    _blocked_run(repository, tmp_path)
    with runs.follower_claim(PROJECT, SESSION):
        _hook("prompt", _prompt_payload(repository, "harness-session"))
        _hook("stop", _stop_payload(repository, "harness-session"))

    after = {path: _stamp(path) for path in watched}
    assert after == before


def test_no_follower_leaves_the_hook_silent(repository: Path, tmp_path: Path) -> None:
    _blocked_run(repository, tmp_path)
    completed = _hook("prompt", _prompt_payload(repository, "harness-session"))

    assert completed.returncode == 0
    assert completed.stdout == ""
