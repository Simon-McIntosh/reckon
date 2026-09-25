"""Focused tests for the coordinator-obligations hook installer.

Every settings file these tests write lives under the pytest temporary
directory. The one file outside it that this module reads is the real
user-scope settings file, and it is read only to prove the installer's default
call leaves it alone.
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


def _encode(payload: dict) -> bytes:
    return (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode()


def _prompt_command() -> str:
    return shlex.join([str(installer.hook_script_path()), "--hook", "prompt"])


def _stop_command() -> str:
    return shlex.join([str(installer.hook_script_path()), "--hook", "stop"])


def _commands(payload: dict, event: str) -> list[str]:
    return [
        hook["command"]
        for group in payload["hooks"].get(event, [])
        for hook in group.get("hooks", [])
    ]


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


def _call(settings_path: Path | None, *, write: bool = False) -> tuple[dict, str]:
    buffer = io.StringIO()
    payload = installer.install_hook_settings(settings_path, write=write, stream=buffer)
    return payload, buffer.getvalue()


def test_snippet_binds_both_modes_to_their_events() -> None:
    snippet = installer.build_hook_snippet()

    assert installer.hook_script_path().name == "coordinator_obligations.py"
    assert set(snippet["hooks"]) == {"UserPromptSubmit", "SessionStart", "Stop"}
    assert _commands(snippet, "UserPromptSubmit") == [_prompt_command()]
    assert _commands(snippet, "SessionStart") == [_prompt_command()]
    assert _commands(snippet, "Stop") == [_stop_command()]


def test_dry_run_prints_the_fragment_and_leaves_the_file_untouched(
    tmp_path: Path,
) -> None:
    settings = tmp_path / "claude" / "settings.json"
    original = _write_settings_file(settings, EXISTING_SETTINGS)
    before = _fingerprint(settings)

    payload, printed = _call(settings)

    assert payload == installer.build_hook_snippet()
    assert json.loads(printed) == payload
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

    merged, printed = _call(settings, write=True)

    assert json.loads(printed) == merged
    assert json.loads(settings.read_bytes()) == merged
    assert merged["model"] == EXISTING_SETTINGS["model"]
    assert merged["permissions"] == EXISTING_SETTINGS["permissions"]
    assert merged["hooks"]["PreToolUse"] == EXISTING_SETTINGS["hooks"]["PreToolUse"]
    assert merged["hooks"]["PostToolUse"] == EXISTING_SETTINGS["hooks"]["PostToolUse"]
    assert merged["hooks"]["Stop"][0] == EXISTING_SETTINGS["hooks"]["Stop"][0]
    assert (
        merged["hooks"]["Stop"][1] == installer.build_hook_snippet()["hooks"]["Stop"][0]
    )
    assert _commands(merged, "Stop") == ["say-done", _stop_command()]
    assert _commands(merged, "UserPromptSubmit") == [_prompt_command()]
    assert _commands(merged, "SessionStart") == [_prompt_command()]


def test_write_creates_the_file_and_its_parent_directory(tmp_path: Path) -> None:
    settings = tmp_path / "nested" / "claude" / "settings.json"
    assert not settings.parent.exists()

    merged, _ = _call(settings, write=True)

    assert settings.is_file()
    assert json.loads(settings.read_bytes()) == merged
    assert set(merged) == {"hooks"}
    assert set(merged["hooks"]) == {"UserPromptSubmit", "SessionStart", "Stop"}


def test_second_write_is_refused_as_a_duplicate_of_the_installed_entry(
    tmp_path: Path,
) -> None:
    settings = tmp_path / "settings.json"
    _call(settings, write=True)
    installed = settings.read_bytes()

    with pytest.raises(installer.HookInstallError) as refusal:
        _call(settings, write=True)

    message = str(refusal.value)
    assert "UserPromptSubmit" in message
    assert _prompt_command() in message
    assert settings.read_bytes() == installed


def test_dry_run_refuses_nothing_under_an_already_installed_hook(
    tmp_path: Path,
) -> None:
    settings = tmp_path / "settings.json"
    _call(settings, write=True)
    installed = settings.read_bytes()

    payload, printed = _call(settings)

    assert payload == installer.build_hook_snippet()
    assert json.loads(printed) == payload
    assert settings.read_bytes() == installed


def test_dry_run_opens_no_file_at_all(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    original = _write_settings_file(settings, EXISTING_SETTINGS)
    # A mode the owner has no read or write access through, so a dry run that
    # opened the file at all would raise instead of printing.
    settings.chmod(0o000)
    try:
        payload, printed = _call(settings)
    finally:
        settings.chmod(0o600)

    assert json.loads(printed) == payload
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

    payload = installer.install_hook_settings(stream=buffer)

    assert payload == installer.build_hook_snippet()
    assert json.loads(buffer.getvalue()) == payload
    assert _fingerprint(real_settings) == before
