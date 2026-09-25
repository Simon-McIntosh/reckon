"""Focused tests for the coordinator-obligations hook installer.

Every settings file these tests write lives under the pytest temporary
directory. The one file outside it that this module reads is the real
user-scope settings file, and it is read only to prove the installer's default
call leaves it alone. The hook scripts the snippet names are resolved from this
file's own location, never through the installer's helper, so a snippet naming
a path that is not in this repository fails the test rather than agreeing with
whatever the installer computed. The merge cases cover a settings file that
already registers one of the entries: it is skipped, the rest are still added,
and an install with nothing to add leaves the file exactly as it was.
"""

from __future__ import annotations

import io
import json
import os
import shlex
from pathlib import Path
from pwd import getpwuid

import pytest

from reckon.hooks import install as installer

# A settings document with hooks of its own, so the merge can be shown to keep
# them, and with keys at other levels, so it can be shown to keep those too.
EXISTING_SETTINGS: dict = {
    "model": "opus",
    "permissions": {"allow": ["Bash(ls:*)"]},
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "Agent",
                "hooks": [{"type": "command", "command": "/opt/guard.py"}],
            }
        ],
        "Stop": [
            {
                "matcher": "",
                "hooks": [{"type": "command", "command": "say-done"}],
            }
        ],
        "PostToolUse": [
            {
                "matcher": "Edit",
            }
        ],
    },
}


# The hook scripts as this repository carries them, resolved from this file's
# own location: tests/ sits directly under the repository root, and both scripts
# ship in reckon/hooks/. Nothing here reads the installer's own path helper: a
# snippet whose command points somewhere else is meant to fail these tests.
REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = REPO_ROOT / "reckon" / "hooks"
COORDINATOR_HOOK = HOOKS_DIR / "coordinator_obligations.py"
WORKER_STOP_HOOK = HOOKS_DIR / "worker_stop.py"


def _encode(payload: dict) -> bytes:
    return (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode()


def _prompt_command() -> str:
    return shlex.join([str(installer.hook_script_path()), "--hook", "prompt"])


def _stop_command() -> str:
    return shlex.join([str(installer.hook_script_path()), "--hook", "stop"])


def _worker_stop_command() -> str:
    return shlex.join([str(installer.worker_stop_script_path())])


def _commands(payload: dict, event: str) -> list[str]:
    return [
        hook["command"]
        for group in payload["hooks"].get(event, [])
        for hook in group.get("hooks", [])
    ]


def _command_paths(payload: dict, event: str) -> list[Path]:
    """Return the script each command in ``event`` runs, as the command names it."""
    return [Path(shlex.split(command)[0]) for command in _commands(payload, event)]


def _fingerprint(path: Path) -> tuple[int, bytes] | None:
    try:
        status = path.stat()
    except FileNotFoundError:
        return None
    return status.st_mtime_ns, path.read_bytes()


def _write_settings_file(path: Path, payload: dict) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    original = _encode(payload)
    path.write_bytes(original)
    return original


def _expected_entries() -> tuple[str, ...]:
    """Name every snippet entry as the install result reports it."""
    return (
        f"UserPromptSubmit: {_prompt_command()}",
        f"SessionStart: {_prompt_command()}",
        f"Stop: {_stop_command()}",
        f"Stop: {_worker_stop_command()}",
    )


def _call(
    settings_path: Path | None, *, write: bool = False
) -> tuple[installer.HookInstallResult, str]:
    buffer = io.StringIO()
    result = installer.install_hook_settings(settings_path, write=write, stream=buffer)
    return result, buffer.getvalue()


def test_snippet_binds_both_modes_to_their_events() -> None:
    snippet = installer.build_hook_snippet()

    assert installer.hook_script_path().name == "coordinator_obligations.py"
    assert set(snippet["hooks"]) == {"UserPromptSubmit", "SessionStart", "Stop"}
    assert _commands(snippet, "UserPromptSubmit") == [_prompt_command()]
    assert _commands(snippet, "SessionStart") == [_prompt_command()]
    # Two entries under Stop: the coordinator's obligations hook in stop mode,
    # and the worker stop hook that binds a dispatched worker to its manifest.
    assert len(snippet["hooks"]["Stop"]) == 2
    assert _commands(snippet, "Stop") == [_stop_command(), _worker_stop_command()]


def test_snippet_commands_name_the_hook_scripts_this_repository_carries() -> None:
    """Every command names the hook script at its repository location."""
    expected = {
        "UserPromptSubmit": [COORDINATOR_HOOK],
        "SessionStart": [COORDINATOR_HOOK],
        "Stop": [COORDINATOR_HOOK, WORKER_STOP_HOOK],
    }

    snippet = installer.build_hook_snippet()

    observed = {event: _command_paths(snippet, event) for event in expected}
    assert observed == expected
    for paths in observed.values():
        for path in paths:
            assert path.is_file()


def test_dry_run_prints_the_fragment_and_leaves_the_file_untouched(
    tmp_path: Path,
) -> None:
    settings = tmp_path / "claude" / "settings.json"
    original = _write_settings_file(settings, EXISTING_SETTINGS)
    before = _fingerprint(settings)

    result, printed = _call(settings)

    assert result.document == installer.build_hook_snippet()
    assert json.loads(printed) == result.document
    # A dry run reads no settings file, so every entry reads as one a merge
    # would add and none is reported skipped.
    assert result.added == _expected_entries()
    assert result.skipped == ()
    # The fragment, not the merged document: an existing hook of the file's own
    # is absent from what a dry run prints.
    assert "say-done" not in printed
    assert settings.read_bytes() == original
    assert _fingerprint(settings) == before


def test_write_merges_the_entries_and_keeps_every_existing_hook_and_key(
    tmp_path: Path,
) -> None:
    settings = tmp_path / "claude" / "settings.json"
    _write_settings_file(settings, EXISTING_SETTINGS)

    result, printed = _call(settings, write=True)
    merged = result.document

    assert json.loads(printed) == merged
    assert json.loads(settings.read_bytes()) == merged
    assert result.added == _expected_entries()
    assert result.skipped == ()
    assert merged["model"] == EXISTING_SETTINGS["model"]
    assert merged["permissions"] == EXISTING_SETTINGS["permissions"]
    assert merged["hooks"]["PreToolUse"] == EXISTING_SETTINGS["hooks"]["PreToolUse"]
    assert merged["hooks"]["PostToolUse"] == EXISTING_SETTINGS["hooks"]["PostToolUse"]
    assert merged["hooks"]["Stop"][0] == EXISTING_SETTINGS["hooks"]["Stop"][0]
    assert (
        merged["hooks"]["Stop"][1:] == installer.build_hook_snippet()["hooks"]["Stop"]
    )
    assert _commands(merged, "Stop") == [
        "say-done",
        _stop_command(),
        _worker_stop_command(),
    ]
    assert _commands(merged, "UserPromptSubmit") == [_prompt_command()]
    assert _commands(merged, "SessionStart") == [_prompt_command()]


def test_write_creates_the_file_and_its_parent_directory(tmp_path: Path) -> None:
    settings = tmp_path / "nested" / "claude" / "settings.json"
    assert not settings.parent.exists()

    result, _ = _call(settings, write=True)

    assert settings.is_file()
    assert json.loads(settings.read_bytes()) == result.document
    assert set(result.document) == {"hooks"}
    assert set(result.document["hooks"]) == {
        "UserPromptSubmit",
        "SessionStart",
        "Stop",
    }


def test_a_second_install_adds_nothing_and_changes_nothing(tmp_path: Path) -> None:
    """Repeating an install leaves the file's bytes and its mode as they were."""
    settings = tmp_path / "settings.json"
    _call(settings, write=True)
    installed = _fingerprint(settings)

    result, printed = _call(settings, write=True)

    assert result.added == ()
    assert result.skipped == _expected_entries()
    assert json.loads(printed) == result.document
    assert _fingerprint(settings) == installed


def test_an_installed_worker_stop_entry_is_skipped_and_the_rest_added(
    tmp_path: Path,
) -> None:
    """A settings file already holding the worker stop hook takes only the rest."""
    settings = tmp_path / "settings.json"
    existing_stop = {
        "matcher": "",
        "hooks": [{"type": "command", "command": _worker_stop_command()}],
    }
    _write_settings_file(settings, {"hooks": {"Stop": [existing_stop]}})

    result, printed = _call(settings, write=True)

    assert result.added == (
        f"UserPromptSubmit: {_prompt_command()}",
        f"SessionStart: {_prompt_command()}",
        f"Stop: {_stop_command()}",
    )
    assert result.skipped == (f"Stop: {_worker_stop_command()}",)
    assert json.loads(printed) == result.document
    assert json.loads(settings.read_bytes()) == result.document
    # The registered entry is left exactly as it was, and the entries the file
    # lacks are appended after it rather than beside a second copy of it.
    assert result.document["hooks"]["Stop"][0] == existing_stop
    assert _commands(result.document, "Stop") == [
        _worker_stop_command(),
        _stop_command(),
    ]
    assert _commands(result.document, "UserPromptSubmit") == [_prompt_command()]
    assert _commands(result.document, "SessionStart") == [_prompt_command()]


def test_an_entry_registered_under_another_event_is_still_added(
    tmp_path: Path,
) -> None:
    """Registration is per event: a copy under another event suppresses nothing."""
    settings = tmp_path / "settings.json"
    _write_settings_file(
        settings,
        {
            "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": _prompt_command()}]}]
            }
        },
    )

    result, _ = _call(settings, write=True)

    assert result.skipped == ()
    assert f"Stop: {_stop_command()}" in result.added
    assert f"UserPromptSubmit: {_prompt_command()}" in result.added
    assert _commands(result.document, "Stop") == [
        _prompt_command(),
        _stop_command(),
        _worker_stop_command(),
    ]
    assert _commands(result.document, "UserPromptSubmit") == [_prompt_command()]


def test_dry_run_under_an_already_installed_hook_changes_nothing(
    tmp_path: Path,
) -> None:
    settings = tmp_path / "settings.json"
    _call(settings, write=True)
    installed = settings.read_bytes()

    result, printed = _call(settings)

    assert result.document == installer.build_hook_snippet()
    assert json.loads(printed) == result.document
    assert settings.read_bytes() == installed


def test_dry_run_opens_no_file_at_all(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    original = _write_settings_file(settings, EXISTING_SETTINGS)
    # A mode the owner has no read or write access through, so a dry run that
    # opened the file at all would raise instead of printing.
    settings.chmod(0o000)
    try:
        result, printed = _call(settings)
    finally:
        settings.chmod(0o600)

    assert json.loads(printed) == result.document
    assert settings.read_bytes() == original


def test_write_refuses_an_unparsable_file(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    unparsable = b"this is not JSON"
    settings.write_bytes(unparsable)

    with pytest.raises(installer.HookInstallError) as refusal:
        _call(settings, write=True)

    assert str(settings) in str(refusal.value)
    assert settings.read_bytes() == unparsable


def test_user_settings_path_is_the_user_scope_settings_file() -> None:
    assert installer.user_settings_path() == Path.home() / ".claude" / "settings.json"


def test_the_default_call_leaves_the_real_user_settings_file_untouched() -> None:
    real_settings = Path(getpwuid(os.getuid()).pw_dir) / ".claude" / "settings.json"
    before = _fingerprint(real_settings)
    buffer = io.StringIO()

    result = installer.install_hook_settings(stream=buffer)

    assert result.document == installer.build_hook_snippet()
    assert json.loads(buffer.getvalue()) == result.document
    assert _fingerprint(real_settings) == before
