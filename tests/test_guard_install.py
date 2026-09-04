from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from reckon import cli as cli_module


def _settings_payload() -> dict:
    return {
        "permissions": {
            "allow": ["Read", "Bash(git status:*)"],
            "deny": ["Read(.env)", "Bash(git stash:*)"],
        },
        "autoMode": {"enabled": True},
        "enabledPlugins": {"example@marketplace": True},
        "modelSettings": {"example": {"effort": "high", "label": "précis"}},
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "/usr/bin/check-shell"}],
                }
            ],
            "PostToolUse": [
                {
                    "matcher": "Write",
                    "hooks": [{"type": "command", "command": "/usr/bin/check-write"}],
                }
            ],
        },
    }


def _write_settings(path: Path, payload: dict) -> bytes:
    content = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode()
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    return content


def _invoke_sync(
    tmp_path: Path,
    settings_path: Path,
    *,
    crew_managed: bool = True,
    remove_guard: bool = False,
):
    docs = tmp_path / "checkout" / "docs"
    project_state = docs / "state" / "sample"
    project_state.mkdir(parents=True, exist_ok=True)
    if crew_managed:
        (project_state / "crew.json").write_text("{}\n")
    arguments = [
        "sync",
        str(docs),
        "--project",
        "sample",
        "--mounts",
        str(tmp_path / "mounts.json"),
        "--state-root",
        str(tmp_path / "config-state"),
        "--claude-settings",
        str(settings_path),
    ]
    if remove_guard:
        arguments.append("--remove-native-agent-guard")
    return CliRunner().invoke(cli_module.main, arguments)


def _guard_commands(payload: dict) -> list[str]:
    commands = []
    for group in payload.get("hooks", {}).get("PreToolUse", []):
        if group.get("matcher") != "Agent":
            continue
        commands.extend(
            hook.get("command", "")
            for hook in group.get("hooks", [])
            if hook.get("type") == "command"
        )
    return commands


def test_sync_registers_guard_idempotently_and_removes_cleanly(tmp_path: Path):
    settings_path = tmp_path / "claude" / "settings.json"
    original_payload = _settings_payload()
    original_bytes = _write_settings(settings_path, original_payload)
    settings_path.chmod(0o600)

    first = _invoke_sync(tmp_path, settings_path)

    assert first.exit_code == 0, first.output
    installed_bytes = settings_path.read_bytes()
    installed = json.loads(installed_bytes)
    assert list(installed) == list(original_payload)
    assert list(installed["hooks"]) == list(original_payload["hooks"])
    for key, value in original_payload.items():
        if key != "hooks":
            assert installed[key] == value
    assert installed["permissions"]["deny"] == original_payload["permissions"]["deny"]
    assert installed["hooks"]["PostToolUse"] == original_payload["hooks"]["PostToolUse"]
    assert (
        installed["hooks"]["PreToolUse"][0]
        == original_payload["hooks"]["PreToolUse"][0]
    )
    expected_guard = cli_module._native_agent_guard_path()
    assert expected_guard.is_absolute()
    assert _guard_commands(installed) == [str(expected_guard)]
    assert settings_path.stat().st_mode & 0o777 == 0o600

    second = _invoke_sync(tmp_path, settings_path)

    assert second.exit_code == 0, second.output
    assert settings_path.read_bytes() == installed_bytes

    removed = _invoke_sync(tmp_path, settings_path, remove_guard=True)

    assert removed.exit_code == 0, removed.output
    assert settings_path.read_bytes() == original_bytes


def test_sync_refuses_malformed_settings_without_truncating(tmp_path: Path):
    settings_path = tmp_path / "claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    malformed = b'{"permissions": {"deny": ["Read(.env)"]}'
    settings_path.write_bytes(malformed)

    result = _invoke_sync(tmp_path, settings_path)

    assert result.exit_code == 1
    assert "cannot parse harness settings" in result.output
    assert settings_path.read_bytes() == malformed


def test_sync_removal_restores_settings_without_preexisting_hooks(tmp_path: Path):
    settings_path = tmp_path / "claude" / "settings.json"
    original_payload = _settings_payload()
    original_payload.pop("hooks")
    original = _write_settings(settings_path, original_payload)

    installed = _invoke_sync(tmp_path, settings_path)
    removed = _invoke_sync(tmp_path, settings_path, remove_guard=True)

    assert installed.exit_code == removed.exit_code == 0
    assert settings_path.read_bytes() == original


def test_sync_leaves_unmanaged_repository_settings_untouched(tmp_path: Path):
    settings_path = tmp_path / "claude" / "settings.json"
    original = _write_settings(settings_path, _settings_payload())

    result = _invoke_sync(tmp_path, settings_path, crew_managed=False)

    assert result.exit_code == 0, result.output
    assert settings_path.read_bytes() == original
