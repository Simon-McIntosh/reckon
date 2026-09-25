"""The Stop hook that binds a worker's turn end to a terminal run manifest."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from reckon.hooks import worker_stop as hook

HOOK_PATH = Path(hook.__file__).resolve()
REAL_SETTINGS = Path.home() / ".claude" / "settings.json"


def _run_dir(tmp_path: Path, name: str = "run") -> Path:
    run = tmp_path / name
    run.mkdir(parents=True, exist_ok=True)
    return run


def _write_manifest(run: Path, status: str | None) -> Path:
    manifest = run / "manifest.md"
    if status is not None:
        manifest.write_text(f"node: sample\nstatus: {status}\ncheckpoint: x\n")
    return manifest


def _stop_payload(cwd: Path, active: bool = False) -> dict:
    return {
        "hook_event_name": "Stop",
        "session_id": "s-test",
        "transcript_path": str(cwd / "transcript.jsonl"),
        "cwd": str(cwd),
        "stop_hook_active": active,
    }


def _chain(cwd: Path, length: int) -> list[dict]:
    """A stop chain: one fresh stop, then its forced continuations."""
    return [_stop_payload(cwd, active=index > 0) for index in range(length)]


def _bind(monkeypatch, manifest: Path, home: Path) -> None:
    monkeypatch.setenv("RECKON_MANIFEST", str(manifest))
    monkeypatch.setenv("RECKON_HOME", str(home))


def test_absent_manifest_is_blocked(tmp_path, monkeypatch) -> None:
    run = _run_dir(tmp_path)
    manifest = run / "manifest.md"
    _bind(monkeypatch, manifest, tmp_path / "config")

    blocked, reason = hook.decide(_stop_payload(run))

    assert blocked is True
    assert reason is not None
    assert str(manifest) in reason
    assert "absent" in reason


def test_in_progress_manifest_is_blocked(tmp_path, monkeypatch) -> None:
    run = _run_dir(tmp_path)
    manifest = _write_manifest(run, "in-progress")
    _bind(monkeypatch, manifest, tmp_path / "config")

    blocked, reason = hook.decide(_stop_payload(run))

    assert blocked is True
    assert reason is not None
    assert str(manifest) in reason
    assert "in-progress" in reason


def test_complete_manifest_is_allowed(tmp_path, monkeypatch) -> None:
    run = _run_dir(tmp_path)
    manifest = _write_manifest(run, "complete")
    _bind(monkeypatch, manifest, tmp_path / "config")

    blocked, reason = hook.decide(_stop_payload(run))

    assert blocked is False
    assert reason is None


def test_blocked_manifest_is_allowed(tmp_path, monkeypatch) -> None:
    run = _run_dir(tmp_path)
    manifest = _write_manifest(run, "blocked")
    _bind(monkeypatch, manifest, tmp_path / "config")

    blocked, reason = hook.decide(_stop_payload(run))

    assert blocked is False
    assert reason is None


def test_failed_manifest_is_allowed(tmp_path, monkeypatch) -> None:
    run = _run_dir(tmp_path)
    manifest = _write_manifest(run, "failed")
    _bind(monkeypatch, manifest, tmp_path / "config")

    blocked, reason = hook.decide(_stop_payload(run))

    assert blocked is False
    assert reason is None


def test_fourth_stop_is_allowed_after_three_blocks(tmp_path, monkeypatch) -> None:
    run = _run_dir(tmp_path)
    manifest = _write_manifest(run, "in-progress")
    _bind(monkeypatch, manifest, tmp_path / "config")

    decisions = [hook.decide(payload)[0] for payload in _chain(run, 4)]

    assert decisions == [True, True, True, False]
    assert (run / hook.COUNTER_NAME).read_text().strip() == "3"

    text = manifest.read_text()
    assert hook.read_status(manifest) == "blocked"
    assert "blocker: turn ended without a terminal manifest after 3 refusals" in text
    assert "node: sample" in text
    assert "checkpoint: x" in text


def test_no_resolvable_run_allows_with_empty_output(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("RECKON_MANIFEST", raising=False)
    home = tmp_path / "home"
    monkeypatch.setenv("RECKON_HOME", str(home))
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "RECKON_HOME": str(home)}

    proc = subprocess.run(
        [sys.executable, str(HOOK_PATH)],
        input=json.dumps(_stop_payload(tmp_path)),
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )

    assert proc.returncode == 0
    assert proc.stdout == ""


# ── the registered command, wired as the install snippet would ──────────────


def _settings_with_hook(path: Path, command: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": command}]}]}
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")


def _registered_stop_commands(settings: Path) -> list[str]:
    data = json.loads(settings.read_text())
    commands = []
    for group in data.get("hooks", {}).get("Stop", []):
        for entry in group.get("hooks", []):
            command = entry.get("command")
            if command:
                commands.append(command)
    return commands


def _run_registered_hook(settings: Path, payload: dict, env: dict) -> list:
    return [
        subprocess.run(
            shlex.split(command),
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env=env,
            check=False,
        )
        for command in _registered_stop_commands(settings)
    ]


def _read_real_settings():
    if not REAL_SETTINGS.is_file():
        return None
    stat = REAL_SETTINGS.stat()
    return (REAL_SETTINGS.read_bytes(), stat.st_mtime_ns, stat.st_size)


def test_registered_hook_refuses_until_terminal(tmp_path) -> None:
    before = _read_real_settings()
    home = tmp_path / "home"
    run = _run_dir(tmp_path)
    manifest = _write_manifest(run, "in-progress")
    settings = home / ".claude" / "settings.json"
    _settings_with_hook(settings, shlex.join([str(HOOK_PATH)]))
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "RECKON_MANIFEST": str(manifest),
        "RECKON_HOME": str(home / "config"),
    }
    payload = _stop_payload(run)

    refused = _run_registered_hook(settings, payload, env)
    assert len(refused) == 1, "the hook must be registered and run"
    assert json.loads(refused[0].stdout)["decision"] == "block"

    _write_manifest(run, "complete")
    allowed = _run_registered_hook(settings, payload, env)
    assert allowed[0].stdout == ""

    assert _read_real_settings() == before


def test_negative_control_settings_without_hook_allows(tmp_path) -> None:
    home = tmp_path / "home"
    run = _run_dir(tmp_path)
    manifest = _write_manifest(run, "in-progress")
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"hooks": {}}, indent=2) + "\n")
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "RECKON_MANIFEST": str(manifest),
        "RECKON_HOME": str(home / "config"),
    }

    runs = _run_registered_hook(settings, _stop_payload(run), env)

    assert _registered_stop_commands(settings) == []
    assert runs == []


def test_capped_stop_creates_a_missing_manifest_with_the_record(
    tmp_path, monkeypatch
) -> None:
    run = _run_dir(tmp_path)
    manifest = run / "manifest.md"
    _bind(monkeypatch, manifest, tmp_path / "config")

    decisions = [hook.decide(payload)[0] for payload in _chain(run, 4)]

    assert decisions == [True, True, True, False]
    assert manifest.is_file()
    assert hook.read_status(manifest) == "blocked"
    assert (
        "blocker: turn ended without a terminal manifest after 3 refusals"
        in manifest.read_text()
    )


def test_a_fresh_stop_resets_the_refusal_count(tmp_path, monkeypatch) -> None:
    """A resumed run gets its own refusals, not its predecessor's spent cap."""
    run = _run_dir(tmp_path)
    manifest = _write_manifest(run, "in-progress")
    _bind(monkeypatch, manifest, tmp_path / "config")

    [hook.decide(payload) for payload in _chain(run, 4)]
    assert hook.read_status(manifest) == "blocked"

    # The resumed run reopens the manifest and stops afresh: stop_hook_active
    # False, so the count resets and this stop is refused like any first stop:
    # not silently rewritten to blocked on the predecessor's spent counter.
    _write_manifest(run, "in-progress")
    blocked, reason = hook.decide(_stop_payload(run))

    assert blocked is True
    assert str(manifest) in (reason or "")
    assert hook.read_status(manifest) == "in-progress"
    assert (run / hook.COUNTER_NAME).read_text().strip() == "1"


def test_indented_status_is_not_the_manifest_status(tmp_path, monkeypatch) -> None:
    run = _run_dir(tmp_path)
    manifest = run / "manifest.md"
    manifest.write_text("node: sample\nnested:\n  status: complete\n")
    _bind(monkeypatch, manifest, tmp_path / "config")

    assert hook.read_status(manifest) is None
    blocked, _ = hook.decide(_stop_payload(run))
    assert blocked is True


def test_subdirectory_cwd_resolves_the_run(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("RECKON_MANIFEST", raising=False)
    home = tmp_path / "home"
    monkeypatch.setenv("RECKON_HOME", str(home))
    worktree = tmp_path / "worktree"
    sub = worktree / "reckon" / "hooks"
    sub.mkdir(parents=True)
    manifest = _write_manifest(worktree, "in-progress")
    live = home / "crew" / "live"
    live.mkdir(parents=True)
    record = {"worktree": str(worktree), "manifest_path": str(manifest)}
    (live / "r-sub.json").write_text(json.dumps(record))

    blocked, reason = hook.decide(_stop_payload(sub))

    assert blocked is True
    assert str(manifest) in (reason or "")
