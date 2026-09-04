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
    else:
        (project_state / "crew.json").unlink(missing_ok=True)

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


def _agent_hook_commands(payload: dict) -> list[str]:
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


def test_sync_cli_installs_guard_once_is_idempotent_and_removes_it(tmp_path: Path):
    settings_path = tmp_path / "claude" / "settings.json"
    original_payload = _settings_payload()
    original_bytes = _write_settings(settings_path, original_payload)

    first = _invoke_sync(tmp_path, settings_path)

    assert first.exit_code == 0, first.output
    installed_bytes = settings_path.read_bytes()
    installed = json.loads(installed_bytes)
    expected_guard = cli_module._native_agent_guard_path()
    assert _agent_hook_commands(installed) == [str(expected_guard)]
    assert installed["permissions"] == original_payload["permissions"]
    assert installed["autoMode"] == original_payload["autoMode"]
    assert (
        installed["hooks"]["PreToolUse"][0]
        == original_payload["hooks"]["PreToolUse"][0]
    )
    assert installed["hooks"]["PostToolUse"] == original_payload["hooks"]["PostToolUse"]

    second = _invoke_sync(tmp_path, settings_path)

    assert second.exit_code == 0, second.output
    assert settings_path.read_bytes() == installed_bytes

    removed = _invoke_sync(tmp_path, settings_path, remove_guard=True)

    assert removed.exit_code == 0, removed.output
    assert settings_path.read_bytes() == original_bytes


def test_sync_cli_refreshes_an_existing_guard_without_duplicating_it(tmp_path: Path):
    settings_path = tmp_path / "claude" / "settings.json"
    payload = _settings_payload()
    payload["hooks"]["PreToolUse"].append(
        {
            "matcher": "Agent",
            "hooks": [
                {
                    "type": "command",
                    "command": "/old/reckon/hooks/native_agent_guard.py",
                }
            ],
        }
    )
    _write_settings(settings_path, payload)

    result = _invoke_sync(tmp_path, settings_path)

    assert result.exit_code == 0, result.output
    installed = json.loads(settings_path.read_bytes())
    assert _agent_hook_commands(installed) == [
        str(cli_module._native_agent_guard_path())
    ]


def test_sync_cli_leaves_non_crew_repository_settings_untouched(tmp_path: Path):
    settings_path = tmp_path / "claude" / "settings.json"
    original = _write_settings(settings_path, _settings_payload())

    result = _invoke_sync(
        tmp_path,
        settings_path,
        crew_managed=False,
    )

    assert result.exit_code == 0, result.output
    assert "project has no crew state" in result.output
    assert settings_path.read_bytes() == original
